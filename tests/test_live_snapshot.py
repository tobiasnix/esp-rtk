# SPDX-License-Identifier: AGPL-3.0-only
"""Live snapshot contract: /api/ui/live and /status against a golden file of the base.

The golden file tests/fixtures/live_contract_9183a58.json was written by
write_golden() at 9183a58, before any #20a change, in the deterministic
environment of contract_env(). A current body must equal it in values and types
once exactly the allowed additions (ADDED_*_PATHS) are removed.
Regenerate it only for a deliberate change of existing fields:
PYTHONDONTWRITEBYTECODE=1 python3 -c "import sys; sys.path.insert(0, 'tests'); \
import test_live_snapshot as t; t.write_golden()"
"""
import asyncio
import contextlib
import copy
import json
import os
import tempfile
import time
import types
import unittest
from unittest import mock

import support
from support import FakeUART, ble, cfg, exact_fix, fanout, gnss, state, web
import measurement
import net
import tracking
from test_receiver_link import Clock, FreshApp, Queue, StopAfter


GOLDEN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures",
                           "live_contract_9183a58.json")
# One fixed tick reading for the whole environment, far from the tick wrap.
NOW = 500_000_000
# Fixed device time in seconds since 2000-01-01 (2026-10-11T08:53:20Z).
DEVICE_TIME = 845_024_000
# The firmware version the golden file was written with; pinned so that a
# release bump does not change the contract.
CONTRACT_VERSION = "ESP-RTK V15.10.4"

# Paths that #20a adds on top of the base; later tasks extend these sets.
ADDED_LIVE_PATHS = frozenset()
ADDED_STATUS_PATHS = frozenset()
# Task 2: NTRIP causes, the GST error text and their stats initial values.
ADDED_LIVE_PATHS |= {("network", "last_ntrip_error_code"), ("network", "ntrip_config"),
                     ("gnss_accuracy", "error")}
ADDED_STATUS_PATHS |= {("stats", k) for k in ("ntrip_config", "gst_output_state",
                                              "gst_output_error", "gst_enable_attempts")}
# Task 4: receiver and correction link health.
ADDED_LIVE_PATHS |= {("link",)}
ADDED_STATUS_PATHS |= {("link",)}
# Task 5: one meaning per counter (new single counters next to their aliases).
ADDED_STATUS_PATHS |= {("stats", k) for k in ("ble_notify_drops", "ble_queue_overflows",
                                              "gga_parsed", "wifi_ssid_active")}
# #24b: device-wide defaults for new measurements.
ADDED_LIVE_PATHS |= {("measurement_defaults",)}
# Named parts, each with its own budget; #21c Task 1: undo identity, request tag and inventory epoch.
LIVE_PATHS_24B = frozenset({("measurement_defaults",)})
LIVE_PATHS_20A = ADDED_LIVE_PATHS - LIVE_PATHS_24B
LIVE_PATHS_21C = frozenset(("undo_candidate", key) for key in ("index", "record_order", "request_id", "inventory_epoch"))
ADDED_LIVE_PATHS |= LIVE_PATHS_21C
# #23c: boot identity
LIVE_PATHS_23C = frozenset({("system", "boot_id")})
ADDED_LIVE_PATHS |= LIVE_PATHS_23C
# #28b: transmitter counters
ADDED_STATUS_PATHS |= {("ntrip_flow", k) for k in (
    "uart_dropped_bytes", "uart_commands_sent", "uart_commands_deferred",
    "uart_commands_dropped", "uart_command_wait_max_ms")}
# #38b: service CRS suggestion and raw CRS fields
ADDED_38B_LIVE_PATHS = frozenset({("frame_suggestion",)})
ADDED_LIVE_PATHS |= ADDED_38B_LIVE_PATHS
ADDED_STATUS_PATHS |= {("rtcm", "service_crs"), ("rtcm", "rtcm_crs"), ("rtcm", "crs_frames")}
# #27: connection and event stream counters
ADDED_STATUS_PATHS |= {("stats", k) for k in (
    "http_connections_total", "sse_streams", "sse_open", "sse_evictions", "sse_refusals",
    "sse_write_timeouts", "sse_stream_errors", "sse_frames", "sse_frame_bytes_max",
    "sse_tick_max_ms", "sse_snapshot_errors")}
# #39: the active project's frame assignment (None without one); only a loaded
# survey has an active project.
ADDED_LOADED_LIVE_PATHS = ADDED_LIVE_PATHS | {("active_project", "frame_assignment")}
# The largest ASCII values the #24b validators accept; quotes escape to two bytes.
WORST_ASCII_DEFAULTS = {"measurement_samples": "quality", "antenna_height_m": 3.0,
                        "reference_frame": '"' * 40, "reference_frame_epoch": '"' * 16}

CONTRACT_FIX = {
    "utc": "120000.00", "lat": 50.0, "lon": 8.0, "alt": 100.0,
    "qual": 4, "sats": 24, "hdop": 0.6,
    "correction_age_sec": 1.0, "fix_status_text": "RTK_FIXED",
    "receiver_accuracy": {"horizontal_sigma_m": .01, "altitude_sigma_m": .02}}


_FIX_AND_GATE_FIELDS = ("_last_fix", "_fix_ticks_ms", "_fix_seq", "_fix_rev", "_fix_expired",
                        "_gate_quality", "_gate_accepted", "_gate_changed_ms",
                        "_gate_hold_since_ms", "_gate_receiver_accuracy")


class FakeWriter:
    """Collects data written by _http_send."""

    def __init__(self):
        self.buf = bytearray()

    def write(self, data):
        self.buf.extend(data)

    def get_extra_info(self, key):
        return ("192.168.4.2", 12345)

    async def drain(self):
        pass


def response_body(writer):
    return json.loads(bytes(writer.buf).split(b"\r\n\r\n", 1)[1])


def normalised(value):
    """The value as a JSON client sees it."""
    return json.loads(json.dumps(value))


@contextlib.contextmanager
def _fresh_app():
    """Run with state.app reset to a newly constructed AppState, restored afterwards."""
    saved = dict(vars(state.app))
    fresh = vars(state.AppState())
    vars(state.app).clear()
    vars(state.app).update(fresh)
    try:
        yield state.app
    finally:
        vars(state.app).clear()
        vars(state.app).update(saved)


def _contract_ntrip():
    """A real NtripClient whose RTCM counter has seen one MSM epoch."""
    client = net.NtripClient(gnss.UartTx(FakeUART()))
    client.rtcm.counts = {1005: 1, 1077: 3, 1087: 3}
    client.rtcm.bytes_total = 1840
    client.rtcm.last_msg_ms = NOW - 1200
    client.rtcm.last_epoch_age_ms = 800
    client.rtcm.last_epoch_ticks_ms = NOW - 1200
    client.rtcm.last_epoch_source = "gps"
    return client


@contextlib.contextmanager
def contract_env():
    """The deterministic environment of the golden file; yields NOW.

    Fixed clocks, a fresh AppState with a published RTK fix (GST attached, gate
    accepted), a real Tracker in a temporary directory with one project, one
    recording line and one vertex (patched as web.tracker), a real NtripClient and
    fixed board, recorder, heartbeat, error and log values.
    """
    with contextlib.ExitStack() as stack:
        folder = stack.enter_context(tempfile.TemporaryDirectory(prefix="esp-rtk-contract-"))
        stack.enter_context(mock.patch.object(time, "ticks_ms", lambda: NOW))
        stack.enter_context(mock.patch.object(time, "ticks_us", lambda: NOW * 1000,
                                              create=True))
        stack.enter_context(mock.patch.object(
            time, "time", lambda: DEVICE_TIME + tracking._EPOCH_OFFSET))
        stack.enter_context(support.patch_module_global("tracking", "_now_seconds",
                                              lambda: DEVICE_TIME))
        stack.enter_context(mock.patch.dict(
            cfg.CONFIG, dict(cfg.DEFAULT_CONFIG, version=CONTRACT_VERSION), clear=True))
        stack.enter_context(mock.patch.object(web, "CONFIG_WARNINGS", []))
        app = stack.enter_context(_fresh_app())
        stack.enter_context(mock.patch.object(web, "board_metrics", lambda fresh=False: {
            "cpu_hz": 160000000, "ram_free_bytes": 8300000, "ram_alloc_bytes": 40000,
            "flash_bytes": 8388608, "fs_total_bytes": 6291456,
            "fs_free_bytes": 5242880, "mcu_temp_c": 46, "rssi_dbm": -61}))
        stack.enter_context(mock.patch.object(web.gc, "mem_free", lambda: 8_300_000))
        stack.enter_context(mock.patch.object(web.field_recorder, "status", lambda: {
            "enabled": True, "session": 3, "boot": 12, "error": None,
            "samples_this_boot": 40, "stored_bytes": 20480, "limit_bytes": 1048576,
            "segments": 2, "rotations_this_boot": 0, "dropped_events": 0,
            "dropped_nmea": 0, "dropped_rtcm_bytes": 0, "max_write_ms": 12,
            "sample_interval_ms": 1000, "flush_interval_ms": 5000,
            "pending_records": 1, "pending_nmea": 0}))
        stack.enter_context(mock.patch.object(web, "error_log_entries", lambda: 7))
        stack.enter_context(mock.patch.object(
            measurement.measurement_session, "progress", measurement.idle_progress()))
        stack.enter_context(mock.patch.dict(web._instances, {
            "ntrip": _contract_ntrip(),
            "ble": types.SimpleNamespace(connections=[1], encrypted_connections=[1],
                                         payload_size=180)}, clear=True))
        stack.enter_context(mock.patch.object(web, "fanout_clients", []))
        app.heartbeat = {"gnss": NOW - 400, "ntrip": NOW - 1500, "web": NOW - 100}
        app.errors = [{"time": 41, "source": "NTRIP", "error": "timeout"}]
        app.identity = {"device_id": "esp-rtk-a1b2c3", "display_name": "Rover 1",
                        "hostname": "esp-rtk"}
        app.stats.update({"uptime_sec": 3600, "boot_count": 12, "reset_cause": "PWRON"})
        app.track_fix_quality(4, NOW - 65_000)
        app._last_fix = exact_fix(CONTRACT_FIX)
        app._fix_ticks_ms, app._fix_expired = NOW - 200, False
        app.reevaluate_gate()
        stack.enter_context(mock.patch.object(
            tracking.Tracker, "storage_capacity", return_value={
                "free_flash_bytes": 8_000_000, "required_flash_reserve_bytes": 3_000_000,
                "estimated_temporary_bytes": 16_384, "writable": True}))
        tracker = tracking.Tracker(os.path.join(folder, "tracking.jsonl"))
        tracker.create_project("Contract")
        tracker.start_line("L-1")
        tracker.add_vertex(exact_fix(CONTRACT_FIX))
        stack.enter_context(mock.patch.object(web, "tracker", tracker))
        try:
            yield NOW
        finally:
            tracker._sync_journal(close=True)
            tracker.point_store.close()


def _live_body():
    writer = FakeWriter()
    with mock.patch.object(web, "_read_allowed", return_value=True):
        asyncio.run(web._handle_ui_live_request(writer, "", False, {}))
    return response_body(writer)


def _status_body():
    writer = FakeWriter()
    with mock.patch.object(web, "_read_allowed", return_value=True):
        asyncio.run(web._handle_status_request(writer, "/status", "", False, {}))
    return response_body(writer)


def build_contract_bodies():
    """{"live_loaded", "live_loading", "status"} in contract_env(), JSON-normalised."""
    with contract_env():
        live_loaded = _live_body()
        with mock.patch.object(web.tracker, "_loaded", False):
            live_loading = _live_body()
        status = _status_body()
    return normalised({"live_loaded": live_loaded, "live_loading": live_loading,
                       "status": status})


def write_golden(path=GOLDEN_PATH):
    with open(path, "w") as target:
        json.dump(build_contract_bodies(), target, sort_keys=True, indent=1)
        target.write("\n")


def load_golden(path=GOLDEN_PATH):
    with open(path) as source:
        return json.load(source)


def size(value):
    """Serialised bytes of value with the default json separators."""
    return len(json.dumps(value).encode("utf-8"))


def part_size(snap, paths, added=ADDED_LIVE_PATHS):
    """The bytes that exactly the allowed additions paths contribute to snap.

    added is every addition snap still carries (default: all of them)."""
    return size(strip_added(snap, added - paths)) - size(strip_added(snap, added))


@contextlib.contextmanager
def undo_candidate_override(override):
    """Within contract_env(): the real undo candidate with the fields of override replaced."""
    with mock.patch.object(web.tracker, "_undo_candidate",
                           side_effect=lambda project, real=web.tracker._undo_candidate:
                           dict(real(project), **override)):
        yield


def strip_added(body, paths):
    """A copy of body without exactly the allowed added paths.

    Each path is a tuple of keys, e.g. ("link",) or ("network", "ntrip_config").
    A missing path raises AssertionError: the addition must be present.
    """
    result = copy.deepcopy(body)
    for path in sorted(paths):
        parent = result
        for key in path[:-1]:
            if not isinstance(parent, dict) or key not in parent:
                raise AssertionError("allowed addition %r is missing" % (path,))
            parent = parent[key]
        if not isinstance(parent, dict) or path[-1] not in parent:
            raise AssertionError("allowed addition %r is missing" % (path,))
        del parent[path[-1]]
    return result


def contract_differences(expected, actual, path=()):
    """Every place where actual differs from expected in type or value.

    Compares exact types recursively, because Python equality treats 0 == False
    and 1 == 1.0, which would let a type change of a contract field pass.
    """
    if type(expected) is not type(actual):
        return ["%s: type %s -> %s" % ("/".join(map(str, path)) or "<root>",
                                       type(expected).__name__, type(actual).__name__)]
    if isinstance(expected, dict):
        differences = []
        for key in sorted(set(expected) | set(actual)):
            where = "/".join(map(str, path + (key,)))
            if key not in actual:
                differences.append("%s: missing" % where)
            elif key not in expected:
                differences.append("%s: unexpected" % where)
            else:
                differences.extend(contract_differences(expected[key], actual[key],
                                                        path + (key,)))
        return differences
    if isinstance(expected, list):
        if len(expected) != len(actual):
            return ["%s: length %d -> %d" % ("/".join(map(str, path)) or "<root>",
                                             len(expected), len(actual))]
        differences = []
        for index, (left, right) in enumerate(zip(expected, actual)):
            differences.extend(contract_differences(left, right, path + (index,)))
        return differences
    if expected != actual:
        return ["%s: %r -> %r" % ("/".join(map(str, path)) or "<root>", expected, actual)]
    return []


class TestContractComparison(unittest.TestCase):
    def test_type_changes_are_differences(self):
        golden = {"a": 0, "b": 1, "c": [1, {"d": 2}]}
        self.assertEqual(contract_differences(golden, copy.deepcopy(golden)), [])
        self.assertEqual(contract_differences(golden, dict(golden, a=False)),
                         ["a: type int -> bool"])
        self.assertEqual(contract_differences(golden, dict(golden, b=1.0)),
                         ["b: type int -> float"])
        self.assertEqual(contract_differences(golden, dict(golden, c=[1, {"d": 2.0}])),
                         ["c/1/d: type int -> float"])
        # Plain equality would have accepted both.
        self.assertEqual(golden, dict(golden, a=False, b=1.0))

    def test_value_key_and_length_changes_are_differences(self):
        golden = {"a": "x", "b": [1, 2], "c": None}
        self.assertEqual(contract_differences(golden, dict(golden, a="y")),
                         ["a: 'x' -> 'y'"])
        self.assertEqual(contract_differences(golden, dict(golden, b=[1])),
                         ["b: length 2 -> 1"])
        self.assertEqual(contract_differences(golden, {"a": "x", "b": [1, 2]}),
                         ["c: missing"])
        self.assertEqual(contract_differences(golden, dict(golden, d=1)),
                         ["d: unexpected"])

    def test_strip_added_removes_exactly_the_allowed_paths(self):
        body = {"link": {"receiver": "ok"}, "network": {"ntrip_state": "streaming",
                                                       "ntrip_config": "ready"}}
        stripped = strip_added(body, {("link",), ("network", "ntrip_config")})
        self.assertEqual(stripped, {"network": {"ntrip_state": "streaming"}})
        self.assertIn("link", body)
        with self.assertRaises(AssertionError):
            strip_added(body, {("stats", "gga_parsed")})
        with self.assertRaises(AssertionError):
            strip_added(body, {("network", "missing")})


class TestLiveSnapshotContract(unittest.TestCase):
    def setUp(self):
        saved = dict((name, getattr(state.app, name)) for name in _FIX_AND_GATE_FIELDS)

        def restore():
            for name, value in saved.items():
                setattr(state.app, name, value)
        self.addCleanup(restore)

    def test_bodies_match_the_base_except_allowed_additions(self):
        golden = load_golden()
        current = build_contract_bodies()
        # While loading, undo_candidate is null and carries no #21c fields.
        allowed = {"live_loaded": ADDED_LOADED_LIVE_PATHS,
                   "live_loading": ADDED_LIVE_PATHS - LIVE_PATHS_21C,
                   "status": ADDED_STATUS_PATHS}
        self.assertEqual(sorted(current), sorted(golden))
        for name in sorted(golden):
            with self.subTest(body=name):
                self.assertEqual(
                    contract_differences(golden[name],
                                         strip_added(current[name], allowed[name])), [])

    def test_golden_file_holds_the_full_contract(self):
        golden = load_golden()
        for key in ("capacity", "persistence", "undo_candidate", "active_line"):
            self.assertIsNotNone(golden["live_loaded"][key], key)
        for key in ("projects", "archives"):
            self.assertIn(key, golden["live_loading"])
        self.assertEqual(sorted(golden["status"]), [
            "ble", "board", "config_warnings", "field_diagnostics", "fix", "fix_age_sec",
            "gate_deviations", "heartbeat_age_sec", "log_entries", "nmea_tcp", "ntrip_flow",
            "quality_streak", "recent_errors", "rtcm", "stats", "system"])

    def test_handler_sends_exactly_the_snapshot(self):
        with contract_env() as now:
            sent = _live_body()
            snapshot = normalised(web.ui_live_snapshot(now))
        self.assertEqual(contract_differences(snapshot, sent), [])

    def test_snapshot_needs_no_writer_and_writes_no_stats(self):
        before = copy.deepcopy(state.app.stats)
        with mock.patch.object(web, "log") as web_log, \
                support.patch_module_global("state", "log") as state_log:
            snap = web.ui_live_snapshot()
        json.dumps(snap)
        self.assertEqual(state.app.stats, before)
        web_log.assert_not_called()
        state_log.assert_not_called()

    def test_unauthorised_live_request_never_builds_a_snapshot(self):
        writer = FakeWriter()
        with mock.patch.object(web, "_read_allowed", return_value=False), \
                mock.patch.object(web, "ui_live_snapshot") as snapshot:
            asyncio.run(web._handle_ui_live_request(writer, "", False, {}))
        snapshot.assert_not_called()
        self.assertIn(b"401", bytes(writer.buf))



class TestNtripLiveFields(unittest.TestCase):
    """network.ntrip_config/last_ntrip_error_code and gnss_accuracy.error (#20a E4/E5)."""

    def test_network_block_tells_the_ntrip_causes_apart(self):
        for ntrip_state, ntrip_config, code in (
                ("disabled", "switched_off", None), ("disabled", "incomplete", None),
                ("disabled", None, None), ("waiting_wifi", "ready", None),
                ("connecting", "ready", "timeout"), ("auth_error", "ready", "auth_error"),
                ("backoff", "ready", "retries_exhausted"), ("streaming", "ready", None)):
            with self.subTest(ntrip_state=ntrip_state, ntrip_config=ntrip_config), \
                    mock.patch.dict(state.app.stats, {
                        "ntrip_state": ntrip_state, "ntrip_config": ntrip_config,
                        "last_ntrip_error_code": code}):
                network = web.ui_live_snapshot()["network"]
            self.assertEqual((network["ntrip_state"], network["ntrip_config"],
                              network["last_ntrip_error_code"]),
                             (ntrip_state, ntrip_config, code))

    def test_ntrip_config_is_null_before_the_client_ran(self):
        fresh = state.AppState()
        self.assertIsNone(fresh.stats["ntrip_config"])
        with mock.patch.object(web, "app", fresh):
            network = web.ui_live_snapshot()["network"]
        self.assertIsNone(network["ntrip_config"])
        self.assertEqual(network["ntrip_state"], "disabled")

    def test_ntrip_config_is_the_running_clients_view(self):
        # Saved as switched off, but the client still runs with what it read at start.
        with mock.patch.dict(state.app.stats, {"ntrip_config": "ready",
                                               "ntrip_state": "streaming"}), \
                mock.patch.dict(cfg.CONFIG, {"ntrip_enabled": False}):
            network = web.ui_live_snapshot()["network"]
        self.assertEqual((network["ntrip_config"], network["ntrip_state"]),
                         ("ready", "streaming"))

    def test_gst_error_is_clean_short_and_absent_when_fine(self):
        long_text = web._short_text("UART error: " + "x" * 200)
        self.assertEqual(len(long_text), 80)
        self.assertTrue(long_text.startswith("UART error: "))
        self.assertEqual(web._short_text('"' * 200), "?" * 80)
        self.assertEqual(web._short_text("a\\b\n\u00fc\u20ac"), "a?b???")
        self.assertIsNone(web._short_text(None))
        self.assertEqual(web._short_text(OSError(5)), "5")
        with mock.patch.dict(state.app.stats, {
                "gst_output_state": "uart_error", "gst_enable_attempts": 2,
                "gst_output_error": "UART error: " + "x" * 200}):
            accuracy = web.ui_live_snapshot()["gnss_accuracy"]
        self.assertEqual(accuracy, {"state": "uart_error", "attempts": 2,
                                    "error": long_text})
        with mock.patch.dict(state.app.stats):
            for key in ("gst_output_state", "gst_output_error", "gst_enable_attempts"):
                state.app.stats.pop(key, None)
            accuracy = web.ui_live_snapshot()["gnss_accuracy"]
        self.assertEqual(accuracy, {"state": "waiting", "attempts": 0, "error": None})

    def test_fresh_state_initialises_gst_output(self):
        stats = state.AppState().stats
        self.assertEqual(stats["gst_output_state"], "waiting")
        self.assertIsNone(stats["gst_output_error"])
        self.assertEqual(stats["gst_enable_attempts"], 0)


def _wrapping_clock(test, now, period):
    """Patch ticks_ms/ticks_diff with a wrapping counter read from now[0]."""
    for patcher in (
            mock.patch.object(time, "ticks_ms", side_effect=lambda: now[0] % period),
            mock.patch.object(time, "ticks_diff", side_effect=lambda a, b: (
                (a - b + period // 2) % period - period // 2))):
        patcher.start()
        test.addCleanup(patcher.stop)


class TestUptime(unittest.TestCase):
    def test_uptime_ms_and_seconds_share_one_clock(self):
        period = 1 << 20
        now = [period - 1500]
        _wrapping_clock(self, now, period)
        app = state.AppState()
        total = 0
        # Small steps, a tick wrap, and steps far past 2**30 ms of uptime in total.
        for step in [1, 998, 1, 2500, 999, 1000, 1001] + [400_000] * 3000:
            now[0] += step
            total += step
            self.assertEqual(app.uptime_ms(), total)
            self.assertEqual(app.uptime_seconds(), app.uptime_ms() // 1000)
        self.assertGreater(total, 1 << 30)


class TestLinkHealth(unittest.TestCase):
    """link (#20a E6) in the live snapshot, /status and the diagnostics package."""

    def test_corrections_follow_state_and_the_one_stale_rule(self):
        cases = [(("disabled", False), "off"), (("disabled", True), "off"),
                 (("streaming", False), "ok"), (("streaming", True), "stale"),
                 (("no_such_state", False), "down")]
        cases += [((name, False), "down") for name in
                  ("waiting_wifi", "connecting", "backoff", "auth_error", "mount_error")]
        for args, expected in cases:
            with self.subTest(args=args):
                self.assertEqual(state.corrections_health(*args), expected)

    def test_snapshot_link_agrees_with_rtcm_stale(self):
        counter = net.RtcmCounter()
        ntrip = types.SimpleNamespace(rtcm=counter)
        for age, stale in ((15000, False), (15001, True)):
            with self.subTest(age=age), \
                    mock.patch.object(counter, "epoch_age_ms", return_value=age), \
                    mock.patch.dict(cfg.CONFIG, {"ntrip_max_epoch_age_sec": 15}), \
                    mock.patch.dict(state.app.stats, {"ntrip_state": "streaming"}), \
                    mock.patch.dict(web._instances, {"ntrip": ntrip}):
                snap = web.ui_live_snapshot()
            self.assertIs(snap["rtcm"]["stale"], stale)
            self.assertEqual(snap["link"]["corrections"],
                             "stale" if snap["rtcm"]["stale"] else "ok")

    def test_safe_mode_link_is_unknown_and_off(self):
        with mock.patch.object(web, "app", state.AppState()), \
                mock.patch.dict(web._instances, {}, clear=True):
            link = web.ui_live_snapshot()["link"]
        self.assertEqual(link, {"receiver": "unknown", "uart_silence_ms": None,
                                "corrections": "off", "rtcm_silence_ms": None})

    def test_new_fields_fit_the_one_hertz_budget(self):
        # Budget for everything #20a adds to one live frame, at worst-case values:
        # link about 120 B, network.last_ntrip_error_code + ntrip_config about 76 B, the
        # sanitised gnss_accuracy.error at most 91 B ("error": plus 80 characters in
        # quotes), separators: measured 291 B. The loaded snapshot of contract_env()
        # measures 3606 B with these worst-case additions (3315 B without, 3485 B at
        # the contract values), so the additions are about 8 % of a loaded frame.
        # #24b adds measurement_defaults, at most 239 B with ASCII values (measured 237 B,
        # total 528 B); the #20a part keeps its own 320 B (measured 291 B).
        # #21c adds index, record_order, request_id and inventory_epoch to undo_candidate:
        # at worst 128 B (index 99999, record_order 10**12, a 32-character request id),
        # within its own 140 B. #23c adds system.boot_id: 23 B for 8 hex characters, within
        # its own 24 B; all parts together measure 679 B, within 723 B.
        errors = ['"' * 200, "\\" * 200, "\u00fc" * 200,
                  "".join(chr(code % 32) for code in range(200))]
        override = {"index": 99999, "record_order": 10 ** 12, "request_id": "x" * 32}
        for error in errors:
            with self.subTest(error=error[:3]), contract_env() as now, \
                    mock.patch.dict(web.CONFIG, WORST_ASCII_DEFAULTS), \
                    undo_candidate_override(override):
                app = state.app
                app.stats.update({"last_ntrip_error_code": "retries_exhausted",
                                  "ntrip_config": "switched_off", "ntrip_state": "streaming",
                                  "gst_output_error": error})
                rtcm = web._instances["ntrip"].rtcm
                with mock.patch.object(app, "receiver_link", return_value="unknown"), \
                        mock.patch.object(app, "uart_silence_ms", return_value=2 ** 29), \
                        mock.patch.object(rtcm, "silence_ms", return_value=10 ** 11), \
                        mock.patch.object(rtcm, "epoch_age_ms", return_value=10 ** 6):
                    snap = strip_added(normalised(web.ui_live_snapshot(now)),
                                       ADDED_38B_LIVE_PATHS)
            # frame_suggestion (#38b) has its own budget test and stays out of this total.
            added = ADDED_LIVE_PATHS - ADDED_38B_LIVE_PATHS
            self.assertEqual(snap["link"], {"receiver": "unknown", "uart_silence_ms": 2 ** 29,
                                            "corrections": "stale",
                                            "rtcm_silence_ms": 10 ** 11})
            self.assertLessEqual(part_size(snap, LIVE_PATHS_20A, added), 320)    # #20a part
            self.assertLessEqual(part_size(snap, LIVE_PATHS_24B, added), 240)    # #24b part
            self.assertLessEqual(part_size(snap, LIVE_PATHS_21C, added), 140)    # #21c part
            self.assertLessEqual(part_size(snap, LIVE_PATHS_23C, added), 24)     # #23c part, measured 23 B
            self.assertLessEqual(size(snap) - size(strip_added(snap, added)), 723)
            self.assertLessEqual(len(json.dumps(snap["gnss_accuracy"]["error"]).encode()), 82)

    def test_undo_identity_fits_its_budget(self):
        worst = {"index": 99999, "record_order": 10 ** 12, "request_id": "x" * 32}
        for name, override in (("worst", worst), ("null", {"index": None, "request_id": None}),
                               ("real", None)):
            with self.subTest(case=name), contract_env() as now, \
                    (undo_candidate_override(override) if override
                     else contextlib.nullcontext()):
                snap = normalised(web.ui_live_snapshot(now))
                epoch = web.tracker.inventory_epoch
            candidate = snap["undo_candidate"]
            self.assertLessEqual(part_size(snap, LIVE_PATHS_21C), 140)
            self.assertIsInstance(candidate["inventory_epoch"], str)
            self.assertEqual(len(candidate["inventory_epoch"]), 8)
            if name == "null":
                self.assertIsNone(candidate["index"])
                self.assertIsNone(candidate["request_id"])
            if name == "real":
                self.assertEqual((candidate["kind"], candidate["index"]), ("vertex", 1))
                self.assertIsNone(candidate["request_id"])
                self.assertEqual(candidate["inventory_epoch"], epoch)
                self.assertRegex(candidate["inventory_epoch"], "^[0-9a-f]{8}$")
        with contract_env() as now, mock.patch.object(web.tracker, "_loaded", False):
            snap = normalised(web.ui_live_snapshot(now))
        self.assertIsNone(snap["undo_candidate"])                 # no #21c part while loading

    def test_measurement_defaults_fit_their_budget(self):
        # Device bytes: MicroPython ujson writes '"' and '\\' as two bytes and non-ASCII as
        # raw UTF-8 (up to four bytes per character); CPython's default would escape it.
        def device_size(value):
            return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
        for char, limit in (('"', 240), ("\\", 240), ("\u00fc", 240), ("\U0001F600", 360)):
            worst = dict(WORST_ASCII_DEFAULTS, reference_frame=char * 40,
                         reference_frame_epoch=char * 16)
            with self.subTest(char=char), contract_env() as now, \
                    mock.patch.dict(web.CONFIG, worst):
                snap = strip_added(normalised(web.ui_live_snapshot(now)),
                                   ADDED_38B_LIVE_PATHS)
                self.assertEqual(snap["measurement_defaults"]["samples"], "quality")
                self.assertLessEqual(device_size(snap) - device_size(
                    strip_added(snap, {("measurement_defaults",)})), limit)

    def test_frame_suggestion_fits_its_budget(self):
        # Device bytes of frame_suggestion (#38b E9) at worst-case ASCII: a 31-character
        # name of quotes (two bytes each), the longest plausible epoch, a matching
        # plate-fixed 1302 and "differs": measured 187 B; null without a 1300: 26 B.
        def device_size(value):
            return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
        for announced in (True, False):
            with self.subTest(announced=announced), contract_env() as now, \
                    mock.patch.dict(web.CONFIG, WORST_ASCII_DEFAULTS):
                if announced:
                    rtcm = web._instances["ntrip"].rtcm
                    rtcm.service_crs = {"name": '"' * 31, "epoch": "2100.0",
                                        "epoch_raw": 20000}
                    rtcm.rtcm_crs = {"name": '"' * 31, "anchor": "plate_fixed",
                                     "plate": 31, "database_links": 7}
                snap = normalised(web.ui_live_snapshot(now))
                added = device_size(snap) - device_size(strip_added(snap,
                                                                    ADDED_38B_LIVE_PATHS))
            if announced:
                self.assertEqual((snap["frame_suggestion"]["state"],
                                  snap["frame_suggestion"]["anchor"]),
                                 ("differs", "plate_fixed"))
                self.assertLessEqual(added, 220)
            else:
                self.assertIsNone(snap["frame_suggestion"])
                self.assertLessEqual(added, 26)

    def test_status_and_diagnostics_share_the_snapshot_sources(self):
        with contract_env() as now:
            state.app.stats["ntrip_state"] = "streaming"
            self.assertEqual(_status_body()["link"],
                             normalised(web.ui_live_snapshot(now)["link"]))
            diagnostics = web.diagnostics_snapshot()
            self.assertEqual(normalised(diagnostics["fix"]),
                             normalised(web.current_fix_view(now)[0]))
            self.assertEqual(normalised(diagnostics["current_quality"]),
                             normalised(web.current_fix_view(now)[1]))
            self.assertEqual(diagnostics["link"], web.ui_live_snapshot(now)["link"])
            self.assertEqual(diagnostics["link"]["corrections"], "ok")

            state.app._last_fix = None
            state.app.stats["ntrip_state"] = "connecting"
            with mock.patch.dict(web._instances, {}, clear=True):
                diagnostics = web.diagnostics_snapshot()
        fix = diagnostics["fix"]
        self.assertEqual((fix["fix_status_text"], fix["qual"], fix["sats"]), ("NO_FIX", 0, 0))
        self.assertEqual(diagnostics["link"]["corrections"], "down")
        self.assertIsNone(diagnostics["link"]["rtcm_silence_ms"])


class TestCounterCauses(unittest.IsolatedAsyncioTestCase):
    async def test_ble_drops_is_the_sum_of_its_causes(self):
        counters = {"ble_drops": 0, "ble_queue_overflows": 0, "ble_notify_drops": 0}
        with mock.patch.dict(state.app.stats, counters):
            router = fanout.NmeaSenderTask(None, state.SimpleQueue())
            router.ble_queue = state.SimpleQueue(maxsize=1)
            router.ble_queue.put_nowait(b"occupied")
            router.route(b"new")
            manager = ble.BLEManager("RTK-TEST")
            manager.encrypted_connections.add(7)
            with mock.patch.object(manager.ble, "gatts_notify", side_effect=OSError(107)):
                await manager.send(b"$GNGGA,test\r\n")
            stats = dict(state.app.stats)
        self.assertGreaterEqual(stats["ble_queue_overflows"], 1)
        self.assertGreaterEqual(stats["ble_notify_drops"], 1)
        self.assertEqual(stats["ble_drops"],
                         stats["ble_queue_overflows"] + stats["ble_notify_drops"])


class TestReceiverSilenceEndToEnd(FreshApp, unittest.IsolatedAsyncioTestCase):
    """Issue acceptance: a fake UART without data shows up as a silent receiver."""

    def setUp(self):
        self.stop = asyncio.Event()
        FreshApp.setUp(self)
        mock.patch.object(web, "app", self.app).start()
        mock.patch.dict(web._instances, {"field_diagnostics": self.recorder}, clear=True).start()
        mock.patch.object(gnss, "log").start()
        mock.patch.object(gnss, "shutdown_event", self.stop).start()
        mock.patch.object(gnss, "replies", []).start()

    async def test_fake_uart_without_data_reports_silent_receiver(self):
        restarts = self.app.stats["task_restarts"]
        clock = Clock(start=0)
        sleeper = StopAfter(self.stop, clock, 100, lambda s: clock.peek() >= 10000)
        with mock.patch.object(gnss.time, "ticks_ms", clock.ticks_ms), \
                mock.patch.object(gnss.asyncio, "sleep_ms", sleeper):
            handler = gnss.GNSSHandler(Queue())
            handler.uart = gnss.UART()
            await asyncio.wait_for(handler.run(), 5)
        now = clock.peek()
        with mock.patch.object(time, "ticks_ms", lambda: now):
            link = web.ui_live_snapshot(now)["link"]
        self.assertEqual(link["receiver"], "silent")
        self.assertGreaterEqual(link["uart_silence_ms"], 3000)
        warnings = [call for call in self.levels("WARN")
                    if "Receiver UART silent" in call.args[2]]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(self.app.stats["task_restarts"], restarts)


if __name__ == "__main__":
    unittest.main()
