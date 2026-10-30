# SPDX-License-Identifier: AGPL-3.0-only
"""HTTP layer: URL decoding, POST parsing, headers, and setup page."""
import asyncio
import builtins
import contextlib
import copy
import inspect
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

import support
from support import access, cfg, exact_fix, state, web
import assets
import events
import web_routing
import tracking
import measurement
import stakeout
import stakeout_progress
from test_access_v11 import FakeReader
from test_events import BufferedWriter
from test_http_status_matrix import SIGN_IN
from test_measurement import held_app, attach_gst
from test_tracking import FIX
from test_stakeout_store import (P128, P256, compact, make_package, sample_package,
                                 wide_package)


EPOCH_2000 = 946_684_800
JUMP_26Y_S = 820_540_800  # 9497 days

_FIX_AND_GATE_FIELDS = ("_last_fix", "_fix_ticks_ms", "_fix_seq", "_fix_rev", "_fix_expired",
                        "_gate_quality", "_gate_accepted", "_gate_changed_ms",
                        "_gate_hold_since_ms", "_gate_receiver_accuracy")


def _save_fix_and_gate():
    return {name: getattr(state.app, name) for name in _FIX_AND_GATE_FIELDS}


def _restore_fix_and_gate(saved):
    for name, value in saved.items():
        setattr(state.app, name, value)


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


class TestHttpAnswer(unittest.TestCase):
    def test_started_from_the_first_write_attempt(self):
        answer = web._HttpAnswer(FakeWriter())
        self.assertIs(answer.started, False)
        answer.write(b"x")
        self.assertIs(answer.started, True)

        class Broken(FakeWriter):
            def write(self, data):
                raise OSError(104)

        failing = web._HttpAnswer(Broken())
        with self.assertRaises(OSError):
            failing.write(b"HTTP/1.0 200 OK\r\n\r\n")
        self.assertIs(failing.started, True)

    def test_head_passes_the_header_and_drops_the_body(self):
        inner = FakeWriter()
        answer = web._HttpAnswer(inner)
        answer.head = True
        web._http_send(answer, "200 OK", "application/json", '{"a": 1}')
        self.assertTrue(bytes(inner.buf).endswith(b"\r\n\r\n"), inner.buf)
        self.assertIn(b"Content-Length: 8\r\n", inner.buf)
        self.assertNotIn(b'{"a"', inner.buf)
        before = bytes(inner.buf)
        answer.write(b"more")
        self.assertEqual(bytes(inner.buf), before)

        fresh = FakeWriter()
        answer = web._HttpAnswer(fresh)
        answer.head = True
        answer.write(bytearray(b"HTTP/1.0 200 OK\r\n\r\nbody"))
        self.assertEqual(bytes(fresh.buf), b"HTTP/1.0 200 OK\r\n\r\n")

    def test_get_passes_everything(self):
        inner = FakeWriter()
        answer = web._HttpAnswer(inner)
        self.assertIs(answer.head, False)
        web._http_send(answer, "200 OK", "application/json", '{"a": 1}')
        self.assertTrue(bytes(inner.buf).endswith(b'\r\n\r\n{"a": 1}'), inner.buf)
        answer.write(b"more")
        self.assertTrue(bytes(inner.buf).endswith(b'{"a": 1}more'))

    def test_forwards_extra_info_and_close(self):
        class Closing(FakeWriter):
            closed = waited = False

            def close(self):
                self.closed = True

            async def wait_closed(self):
                self.waited = True

        inner = Closing()
        answer = web._HttpAnswer(inner)
        self.assertEqual(answer.get_extra_info("peername"), ("192.168.4.2", 12345))
        asyncio.run(answer.drain())
        answer.close()
        asyncio.run(answer.wait_closed())
        self.assertTrue(inner.closed)
        self.assertTrue(inner.waited)


class TestStaticAssets(unittest.TestCase):
    def test_streams_get_with_mime_cache_and_csp(self):
        with tempfile.NamedTemporaryFile(delete=False) as asset:
            asset.write(b"a" * 2500)
            filename = asset.name
        try:
            writer = FakeWriter()
            asyncio.run(web._http_send_file(writer, filename,
                                             "text/css; charset=utf-8", "GET"))
            header, body = bytes(writer.buf).split(b"\r\n\r\n", 1)
            self.assertIn(b"Content-Type: text/css; charset=utf-8", header)
            self.assertIn(b"Content-Length: 2500", header)
            self.assertIn(b"max-age=31536000, immutable", header)
            self.assertIn(b"Content-Security-Policy:", header)
            self.assertNotIn(b"unsafe-inline", header)
            self.assertNotIn(b"unsafe-eval", header)
            self.assertEqual(body, b"a" * 2500)
        finally:
            os.unlink(filename)

    def test_head_has_get_length_but_no_body(self):
        with tempfile.NamedTemporaryFile(delete=False) as asset:
            asset.write(b"dashboard")
            filename = asset.name
        try:
            writer = FakeWriter()
            asyncio.run(web._http_send_file(writer, filename,
                                             "application/javascript", "HEAD"))
            header, body = bytes(writer.buf).split(b"\r\n\r\n", 1)
            self.assertIn(b"Content-Length: 9", header)
            self.assertEqual(body, b"")
        finally:
            os.unlink(filename)

    def test_missing_asset_is_no_store_404(self):
        writer = FakeWriter()
        asyncio.run(web._http_send_file(writer, "/missing/asset.js",
                                         "application/javascript"))
        response = bytes(writer.buf)
        self.assertIn(b"404 Not Found", response)
        self.assertIn(b"Cache-Control: no-store", response)


class TestWifiScanContract(unittest.TestCase):
    def _request(self, active_line=None, measurement=False):
        manager = mock.Mock()
        manager.request_scan.return_value = ("ready", [{"ssid": "Field", "rssi": -50}], None)
        writer = FakeWriter()
        with mock.patch.object(web, "_admin_allowed", return_value=True), \
                mock.patch.object(web.tracker, "active_line", return_value=active_line), \
                mock.patch.object(web.measurement_session, "active", return_value=measurement), \
                mock.patch.dict(web._instances, {"network": manager}, clear=False):
            asyncio.run(web._handle_diagnostics_request(
                writer, "/api/wifi-scan", "", False, {}))
        return bytes(writer.buf), manager

    def test_scan_is_available_while_corrections_are_streaming(self):
        old = state.app.stats.get("ntrip_state")
        state.app.stats["ntrip_state"] = "streaming"
        try:
            response, manager = self._request()
        finally:
            state.app.stats["ntrip_state"] = old
        self.assertIn(b"200 OK", response)
        self.assertIn(b'"ssid": "Field"', response)
        manager.request_scan.assert_called_once_with()

    def test_scan_is_blocked_only_during_an_active_measurement(self):
        for active_line, measurement in (({"state": "recording"}, False),
                                         ({"state": "paused"}, True),
                                         (None, True)):
            response, manager = self._request(active_line, measurement)
            self.assertIn(b"409 Conflict", response)
            self.assertIn(b"wifi_scan_busy", response)
            manager.request_scan.assert_not_called()

    def test_paused_or_finished_line_does_not_block_the_scan(self):
        for line in ({"state": "paused"}, {"state": "finished"}, None):
            response, manager = self._request(line, False)
            self.assertIn(b"200 OK", response)
            manager.request_scan.assert_called_once_with()

    def test_recording_active_reads_line_state_and_session(self):
        cases = (({"state": "recording"}, False, True), ({"state": "paused"}, False, False),
                 ({"state": "paused"}, True, True), (None, False, False))
        for line, measuring, expected in cases:
            with mock.patch.object(web.tracker, "active_line", return_value=line), \
                    mock.patch.object(web.measurement_session, "active", return_value=measuring):
                self.assertIs(web.recording_active(), expected)

    def test_manifest_has_all_offline_assets(self):
        self.assertEqual(set(web.STATIC_ASSETS), {
            "/base.css", "/pages.css",
            "/pages.js", "/i18n.js", "/qr.js", "/app.css", "/app.js"})

    def test_shared_page_behaviour_uses_no_native_blocking_dialogs(self):
        with open("pages.js", encoding="utf-8") as source:
            javascript = source.read()
        for obsolete in ("prompt(", "confirm(", "alert("):
            self.assertNotIn(obsolete, javascript)


class TestFixViewPendingGst(unittest.TestCase):
    def test_fix_view_carries_the_held_accuracy_only_while_pending(self):
        app = held_app()
        with mock.patch.object(web, "app", app):
            current, _quality = web.current_fix_view()
            self.assertTrue(current["pending_gst"])
            self.assertEqual(current["held_receiver_accuracy"],
                             {"horizontal_sigma_m": .01, "altitude_sigma_m": .02,
                              "utc": "120000.00"})
            self.assertNotIn("receiver_accuracy", current)
            attach_gst(app)
            current, _quality = web.current_fix_view()
            self.assertFalse(current["pending_gst"])
            self.assertNotIn("held_receiver_accuracy", current)


class TestTrackingViews(unittest.TestCase):
    def setUp(self):
        # Saves and their failures log through cfg.log; keep the test output clean.
        patcher = mock.patch.object(cfg, "log")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_failed_gate_write_leaves_config_and_verdict_consistent(self):
        def call(method, payload=b""):
            writer = FakeWriter()
            with mock.patch.object(web, "_admin_allowed", return_value=True), \
                    mock.patch.object(cfg, "_atomic_dump", return_value=False):
                asyncio.run(web._handle_measurement_gate_request(
                    writer, method, payload, False, {}))
            return bytes(writer.buf)
        saved = dict(cfg.CONFIG)
        previous = _save_fix_and_gate()
        try:
            cfg.CONFIG["measurement_max_hdop"] = 2.0        # a softened gate on file
            state.app._last_fix = exact_fix({
                "utc": "1", "lat": 50.0, "lon": 8.0, "alt": 100.0,
                "qual": 4, "sats": 24, "hdop": 0.6,
                "correction_age_sec": 1.0, "fix_status_text": "RTK_FIXED",
                "receiver_accuracy": {"horizontal_sigma_m": .01,
                                      "altitude_sigma_m": .02}})
            state.app._fix_ticks_ms, state.app._fix_expired = time.ticks_ms(), False
            state.app.reevaluate_gate()
            changed_ms = state.app._gate_changed_ms
            before = {key: cfg.CONFIG[key] for key in cfg.CONFIG
                      if key.startswith("measurement_")}
            values = dict(tracking.measurement_gate(), min_satellites=30)
            for method, payload in (("PUT", json.dumps(values).encode()), ("DELETE", b"")):
                body = call(method, payload)
                self.assertIn(b"500 Internal Server Error", body, method)
                self.assertIn(b"save_failed", body, method)
                self.assertEqual({key: cfg.CONFIG[key] for key in cfg.CONFIG
                                  if key.startswith("measurement_")}, before, method)
                self.assertEqual(tracking.measurement_gate()["max_hdop"], 2.0, method)
                gate = state.app.gate_state()
                self.assertTrue(gate["accepted"], method)
                self.assertEqual(gate["quality"]["limits"]["max_hdop"], 2.0, method)
                self.assertEqual(gate["quality"], tracking.quality_check(state.app.last_fix),
                                 method)
                self.assertTrue(state.app.quality_streaks()["gate_accepted"], method)
                self.assertEqual(state.app._gate_changed_ms, changed_ms, method)
        finally:
            cfg.CONFIG.clear()
            cfg.CONFIG.update(saved)
            _restore_fix_and_gate(previous)

    def test_tcp_auth_endpoint_is_boolean_persistent_and_defaults_on(self):
        async def call(method, payload=b""):
            writer = FakeWriter()
            with mock.patch.object(web, "_admin_allowed", return_value=True), \
                    mock.patch.object(cfg, "_atomic_dump", return_value=True):
                await web._handle_tcp_auth_request(writer, method, payload, False, {})
            return bytes(writer.buf).split(b"\r\n\r\n", 1)[1]
        saved = dict(cfg.CONFIG)
        try:
            self.assertIn(b'"required": true', asyncio.run(call("GET")))
            self.assertIn(b'"required": false', asyncio.run(call(
                "PUT", b'{"required":false}')))
            self.assertFalse(cfg.CONFIG["nmea_tcp_auth_required"])
            invalid = asyncio.run(call("PUT", b'{"required":"no"}'))
            self.assertIn(b'invalid_tcp_auth_setting', invalid)
        finally:
            cfg.CONFIG.clear()
            cfg.CONFIG.update(saved)

    def test_failed_tcp_auth_write_keeps_the_requirement(self):
        # A real failing write (not a mocked save_config): the port must keep asking
        # for AUTH, otherwise the device runs on a value a reboot would undo.
        writer = FakeWriter()
        saved = dict(cfg.CONFIG)
        try:
            cfg.CONFIG["nmea_tcp_auth_required"] = True
            with mock.patch.object(web, "_admin_allowed", return_value=True), \
                    mock.patch.object(cfg, "_atomic_dump", return_value=False):
                asyncio.run(web._handle_tcp_auth_request(
                    writer, "PUT", b'{"required":false}', False, {}))
            raw = bytes(writer.buf)
            self.assertIn(b"500 Internal Server Error", raw)
            self.assertIn(b"save_failed", raw)
            self.assertTrue(cfg.CONFIG["nmea_tcp_auth_required"])
        finally:
            cfg.CONFIG.clear()
            cfg.CONFIG.update(saved)

    def test_field_setup_endpoint_reads_and_updates(self):
        async def call(method, payload=b""):
            writer = FakeWriter()
            with mock.patch.object(web, "_admin_allowed", return_value=True), \
                    mock.patch.object(cfg, "_atomic_dump", return_value=True):
                await web._handle_field_setup_request(writer, method, payload, False, {})
            return bytes(writer.buf)
        saved = dict(cfg.CONFIG)
        try:
            cfg.CONFIG["reference_frame"] = "KEEP"
            body = json.loads(asyncio.run(call("GET")).split(b"\r\n\r\n", 1)[1])
            self.assertEqual(set(body["values"]), set(tracking.FIELD_SETUP_KEYS))
            self.assertEqual(set(body["defaults"]), set(tracking.FIELD_SETUP_KEYS))
            body = json.loads(asyncio.run(call("PUT", b'{"antenna_height_m": 1.8}')).split(b"\r\n\r\n", 1)[1])
            self.assertEqual(cfg.CONFIG["antenna_height_m"], 1.8)
            self.assertEqual(cfg.CONFIG["reference_frame"], "KEEP")
            self.assertEqual(body["values"]["antenna_height_m"], 1.8)
            raw = asyncio.run(call("PUT", b'{"antenna_height_m": 9}'))
            self.assertIn(b"400 Bad Request", raw)
            self.assertIn(b"invalid_field_setup", raw)
            self.assertIn(b"antenna_height_m", raw)
            self.assertEqual(cfg.CONFIG["antenna_height_m"], 1.8)
            self.assertIn(b"invalid_field_setup", asyncio.run(call("PUT", b"not json")))
        finally:
            cfg.CONFIG.clear()
            cfg.CONFIG.update(saved)

    def test_tracking_get_lists_config_warnings(self):
        writer = FakeWriter()
        warning = {"key": "antenna_height_m", "message": "bad"}
        with mock.patch.object(web, "_read_allowed", return_value=True), \
                mock.patch.object(web, "_admin_allowed", return_value=True), \
                mock.patch.object(web, "_instances", {}), \
                mock.patch.object(web.tracker, "_loaded", True), \
                mock.patch.object(web.tracker, "status", return_value={}), \
                mock.patch.object(web, "board_metrics", return_value={}), \
                mock.patch.object(web, "CONFIG_WARNINGS", [warning]):
            asyncio.run(web._handle_tracking_request(writer, "/api/tracking", "GET", "", b"", False, {}))
        body = json.loads(bytes(writer.buf).split(b"\r\n\r\n", 1)[1])
        self.assertEqual(body["config_warnings"], [warning])

    def test_field_setup_save_clears_the_load_warning_for_saved_keys(self):
        writer = FakeWriter()
        saved = dict(cfg.CONFIG)
        cfg.CONFIG_WARNINGS[:] = [{"key": "antenna_height_m", "message": "bad"},
                                  {"key": "reference_frame", "message": "bad"}]
        try:
            with mock.patch.object(web, "_admin_allowed", return_value=True), \
                    mock.patch.object(cfg, "_atomic_dump", return_value=True):
                asyncio.run(web._handle_field_setup_request(
                    writer, "PUT", b'{"antenna_height_m": 1.8}', False, {}))
            self.assertEqual([w["key"] for w in cfg.CONFIG_WARNINGS], ["reference_frame"])
        finally:
            del cfg.CONFIG_WARNINGS[:]
            cfg.CONFIG.clear()
            cfg.CONFIG.update(saved)

    def test_gate_and_tcp_auth_saves_clear_the_load_warning_for_saved_keys(self):
        gate = dict(tracking.measurement_gate(), min_satellites=30)
        cases = ((web._handle_measurement_gate_request, json.dumps(gate).encode(),
                  "measurement_min_satellites"),
                 (web._handle_tcp_auth_request, b'{"required": false}', "nmea_tcp_auth_required"))
        saved = dict(cfg.CONFIG)
        previous = _save_fix_and_gate()
        try:
            for handler, body, key in cases:
                cfg.CONFIG_WARNINGS[:] = [{"key": key, "message": "bad"},
                                          {"key": "reference_frame", "message": "bad"}]
                writer = FakeWriter()
                with mock.patch.object(web, "_admin_allowed", return_value=True), \
                        mock.patch.object(web.app, "reevaluate_gate"), \
                        mock.patch.object(cfg, "_atomic_dump", return_value=True):
                    asyncio.run(handler(writer, "PUT", body, False, {}))
                with self.subTest(key=key):
                    self.assertIn(b"200 OK", bytes(writer.buf))
                    self.assertEqual([w["key"] for w in cfg.CONFIG_WARNINGS], ["reference_frame"])
        finally:
            del cfg.CONFIG_WARNINGS[:]
            cfg.CONFIG.clear()
            cfg.CONFIG.update(saved)
            _restore_fix_and_gate(previous)

    def test_failed_field_setup_write_restores_config(self):
        writer = FakeWriter()
        saved = dict(cfg.CONFIG)
        try:
            cfg.CONFIG["antenna_height_m"] = 1.5
            with mock.patch.object(web, "_admin_allowed", return_value=True), \
                    mock.patch.object(cfg, "_atomic_dump", return_value=False):
                asyncio.run(web._handle_field_setup_request(
                    writer, "PUT", b'{"antenna_height_m": 2.5}', False, {}))
            raw = bytes(writer.buf)
            self.assertIn(b"500 Internal Server Error", raw)
            self.assertIn(b"save_failed", raw)
            self.assertEqual(cfg.CONFIG["antenna_height_m"], 1.5)
        finally:
            cfg.CONFIG.clear()
            cfg.CONFIG.update(saved)

    def test_field_setup_route_policy_and_admin(self):
        self.assertEqual(web_routing.ROUTES["/api/field-setup"][0], ("GET", "PUT"))
        self.assertIs(web._settings_handler("/api/field-setup"),
                      web._handle_field_setup_request)
        writer = FakeWriter()
        with mock.patch.object(web, "_admin_allowed", return_value=False):
            asyncio.run(web._handle_field_setup_request(writer, "GET", b"", False, {}))
        self.assertIn(b"401 Unauthorized", bytes(writer.buf))

    def test_measurement_gate_endpoint_reads_updates_and_resets(self):
        values = tracking.measurement_gate()
        values["max_hdop"] = 2.0
        async def call(method, payload=b""):
            writer = FakeWriter()
            with mock.patch.object(web, "_admin_allowed", return_value=True), \
                    mock.patch.object(cfg, "_atomic_dump", return_value=True):
                await web._handle_measurement_gate_request(
                    writer, method, payload, False, {})
            return bytes(writer.buf).split(b"\r\n\r\n", 1)[1]
        def live_quality():
            writer = FakeWriter()
            compact = {"revision": 7, "device_time": 100,
                       "active_project": None, "active_line": None}
            with mock.patch.object(web, "_read_allowed", return_value=True), \
                    mock.patch.object(web.tracker, "live_status", return_value=compact), \
                    mock.patch.object(web, "board_metrics", return_value={"ram_free_bytes": 1}):
                asyncio.run(web._handle_ui_live_request(writer, "", False, {}))
            return json.loads(bytes(writer.buf).split(b"\r\n\r\n", 1)[1])["current_quality"]
        saved = dict(cfg.CONFIG)
        previous = _save_fix_and_gate()
        try:
            state.app._last_fix = exact_fix({
                "utc": "1", "lat": 50.0, "lon": 8.0, "alt": 100.0,
                "qual": 4, "sats": 24, "hdop": 0.6,
                "correction_age_sec": 1.0, "fix_status_text": "RTK_FIXED",
                "receiver_accuracy": {"horizontal_sigma_m": .01,
                                      "altitude_sigma_m": .02}})
            state.app._fix_ticks_ms, state.app._fix_expired = time.ticks_ms(), False
            state.app.reevaluate_gate()
            self.assertTrue(live_quality()["accepted"])
            body = asyncio.run(call("GET"))
            self.assertIn(b'"defaults"', body)
            self.assertEqual(json.loads(body)["deviations"], [])
            body = asyncio.run(call("PUT", json.dumps(values).encode()))
            self.assertEqual(cfg.CONFIG["measurement_max_hdop"], 2.0)
            self.assertIn(b'"values"', body)
            self.assertEqual(json.loads(body)["deviations"],
                             [{"key": "max_hdop", "value": 2.0, "default": 1.5}])
            asyncio.run(call("PUT", json.dumps(dict(values, min_satellites=30)).encode()))
            quality = live_quality()
            self.assertFalse(quality["accepted"])
            self.assertIn("too_few_satellites", quality["reasons"])
            body = asyncio.run(call("DELETE"))
            self.assertEqual(json.loads(body)["deviations"], [])
            self.assertEqual(cfg.CONFIG["measurement_max_hdop"], 1.5)
            self.assertTrue(live_quality()["accepted"])
        finally:
            cfg.CONFIG.clear()
            cfg.CONFIG.update(saved)
            _restore_fix_and_gate(previous)

    def test_gate_deviations_reach_live_status_and_tracking_get(self):
        loose = {"measurement_required_fix": "DGPS", "measurement_max_hdop": 2.0,
                 "measurement_max_gst_horizontal_sigma_m": 2.0,
                 "measurement_quality_window_samples": 3}
        def fetch(handler):
            writer = FakeWriter()
            with mock.patch.object(web, "_read_allowed", return_value=True), \
                    mock.patch.object(web, "_admin_allowed", return_value=True), \
                    mock.patch.object(web, "_instances", {}), \
                    mock.patch.object(web.tracker, "_loaded", True), \
                    mock.patch.object(web.tracker, "status", return_value={}), \
                    mock.patch.object(web.tracker, "live_status", return_value={"revision": 1}), \
                    mock.patch.object(web, "board_metrics", return_value={}):
                asyncio.run(handler(writer))
            return json.loads(bytes(writer.buf).split(b"\r\n\r\n", 1)[1])["gate_deviations"]
        handlers = (
            lambda w: web._handle_ui_live_request(w, "", False, {}),
            lambda w: web._handle_status_request(w, "/status", "", False, {}),
            lambda w: web._handle_tracking_request(w, "/api/tracking", "GET", "", b"", False, {}))
        for handler in handlers:
            self.assertEqual(fetch(handler), [])
        with mock.patch.dict(cfg.CONFIG, loose):
            for handler in handlers:
                self.assertEqual([item["key"] for item in fetch(handler)],
                                 ["required_fix", "max_hdop", "max_gst_horizontal_sigma_m",
                                  "quality_window_samples"])

    def test_ui_live_view_combines_only_field_loop_state(self):
        writer = FakeWriter()
        compact = {"revision": 7, "device_time": 100,
                   "active_project": None, "active_line": None}
        with mock.patch.object(web, "_read_allowed", return_value=True), \
                mock.patch.object(web.tracker, "live_status", return_value=compact), \
                mock.patch.object(web, "board_metrics", return_value={"ram_free_bytes": 1}):
            asyncio.run(web._handle_ui_live_request(writer, "", False, {}))
        _header, body = bytes(writer.buf).split(b"\r\n\r\n", 1)
        self.assertIn(b'"schema_version": 1', body)
        self.assertIn(b'"revision": 7', body)
        self.assertIn(b'"measurement_progress"', body)
        self.assertNotIn(b'"projects"', body)

    def test_live_snapshot_carries_the_exact_integer_fields(self):
        writer = FakeWriter()
        previous_fix = state.app._last_fix
        compact = {"revision": 7, "device_time": 100,
                   "active_project": None, "active_line": None}
        try:
            state.app._last_fix = {"utc": "120000.00", "lat": 50.0, "lon": 8.0, "alt": 100.0,
                                   "lat_e8": 5000000000, "lon_e8": 800000000, "alt_mm": 100000,
                                   "geoid_sep_mm": 45678, "qual": 4, "sats": 20, "hdop": 0.6,
                                   "fix_status_text": "RTK_FIXED"}
            with mock.patch.object(web, "_read_allowed", return_value=True), \
                    mock.patch.object(web.tracker, "live_status", return_value=compact), \
                    mock.patch.object(web, "board_metrics", return_value={"ram_free_bytes": 1}):
                asyncio.run(web._handle_ui_live_request(writer, "", False, {}))
        finally:
            state.app._last_fix = previous_fix
        _header, body = bytes(writer.buf).split(b"\r\n\r\n", 1)
        current = json.loads(body)["current_fix"]
        self.assertEqual(current["lat_e8"], 5000000000)
        self.assertEqual(current["lon_e8"], 800000000)
        self.assertEqual(current["alt_mm"], 100000)
        self.assertEqual(current["geoid_sep_mm"], 45678)

    def test_incremental_log_view_is_bounded_and_authenticated(self):
        writer = FakeWriter()
        payload = {"generation": 2, "cursor": "2:9", "entries": [],
                   "has_more": False}
        with mock.patch.object(web, "_admin_allowed", return_value=True), \
                mock.patch.object(web, "incremental_error_log",
                                  return_value=payload) as read:
            asyncio.run(web._handle_status_request(
                writer, "/api/log", "cursor=2%3A4&limit=999&level=WARN",
                False, {}))
        read.assert_called_once_with("2:4", 200, "WARN")
        self.assertIn(b'"cursor": "2:9"', bytes(writer.buf))

    def test_name_check_view_returns_compact_contract(self):
        writer = FakeWriter()
        with mock.patch.object(web, "_admin_allowed", return_value=True), \
                mock.patch.object(web.tracker, "_loaded", True), \
                mock.patch.object(web.tracker, "name_available",
                                  return_value={"available": False,
                                                "existing": "V-01"}) as check:
            asyncio.run(web._handle_tracking_request(
                writer, "/api/tracking", "GET",
                "view=name_check&kind=point&name=V-01", b"", False, {}))
        check.assert_called_once_with("V-01", "point")
        _header, body = bytes(writer.buf).split(b"\r\n\r\n", 1)
        self.assertIn(b'"available": false', body)
        self.assertIn(b'"existing": "V-01"', body)


class TestUrlDecode(unittest.TestCase):
    def test_plus_becomes_space(self):
        self.assertEqual(web.url_decode("a+b"), "a b")

    def test_prozent_hex(self):
        self.assertEqual(web.url_decode("a%40b"), "a@b")

    def test_multiple_sequences(self):
        self.assertEqual(web.url_decode("%2Fmnt%2Fpnt"), "/mnt/pnt")

    def test_utf8_umlaut(self):
        # 'U' = C3 BC
        self.assertEqual(web.url_decode("Caf%C3%A9"), "Café")

    def test_cut_off_sequence_remains_literal(self):
        # No crash if % is not followed by enough signs.
        self.assertEqual(web.url_decode("abc%4"), "abc%4")

    def test_invalid_hex_remains_literal(self):
        self.assertEqual(web.url_decode("a%ZZb"), "a%ZZb")

    def test_empty_string(self):
        self.assertEqual(web.url_decode(""), "")


class TestParsePostData(unittest.TestCase):
    def test_einfaches_formular(self):
        got = web.parse_post_data(b"wifi_ssid=Home&ntrip_port=2101")
        self.assertEqual(got, {"wifi_ssid": "Home", "ntrip_port": "2101"})

    def test_field_without_equality_sign_is_skipped(self):
        # V9.0 crashed here.
        got = web.parse_post_data(b"a=1&kaputt&b=2")
        self.assertEqual(got, {"a": "1", "b": "2"})

    def test_empty_value(self):
        # An empty password field means leave it unchanged.
        self.assertEqual(web.parse_post_data(b"wifi_pass="), {"wifi_pass": ""})

    def test_the_value_contains_the_same_sign(self):
        self.assertEqual(web.parse_post_data(b"p=a=b"), {"p": "a=b"})

    def test_urlkodierte_werte(self):
        got = web.parse_post_data(b"ntrip_user=a%40b.de&wifi_ssid=my+network")
        self.assertEqual(got["ntrip_user"], "a@b.de")
        self.assertEqual(got["wifi_ssid"], "my network")

    def test_empty_body(self):
        self.assertEqual(web.parse_post_data(b""), {})
        self.assertEqual(web.parse_post_data(None), {})


class TestContentLength(unittest.TestCase):
    def parse(self, *lines):
        return web._parse_content_length([b"POST /save HTTP/1.1"] + list(lines))

    def test_is_found(self):
        self.assertEqual(self.parse(b"Content-Length: 42"), 42)

    def test_gross_klein_egal(self):
        self.assertEqual(self.parse(b"content-length: 7"), 7)

    def test_fehlt(self):
        self.assertEqual(self.parse(b"Host: 192.168.4.1"), 0)

    def test_no_numerical_value(self):
        self.assertEqual(self.parse(b"Content-Length: abc"), 0)

    def test_recall_line_is_not_read(self):
        # The slice [1:] should skip the request line.
        self.assertEqual(web._parse_content_length([b"Content-Length: 99"]), 0)


class TestHttpSend(unittest.TestCase):
    def test_header_and_body(self):
        w = FakeWriter()
        web._http_send(w, "200 OK", "text/plain", "hallo")
        out = bytes(w.buf)
        self.assertTrue(out.startswith(b"HTTP/1.0 200 OK\r\n"))
        self.assertIn(b"Content-Length: 5\r\n", out)
        self.assertTrue(out.endswith(b"\r\n\r\nhallo"))

    def test_content_length_does_not_count_bytes_of_characters(self):
        # The bug from V9.0: len(str) instead of len(bytes) for umlauten.
        w = FakeWriter()
        web._http_send(w, "200 OK", "text/html; charset=utf-8", "café")
        out = bytes(w.buf)
        self.assertIn(b"Content-Length: 5\r\n", out)   # c a f é(2)
        header, body = out.split(b"\r\n\r\n", 1)
        self.assertEqual(len(body), 5)

    def test_bytes_payload(self):
        w = FakeWriter()
        web._http_send(w, "404 Not Found", "text/plain", b"weg")
        self.assertIn(b"Content-Length: 3\r\n", bytes(w.buf))

    def test_json_helpers_enforce_no_store_and_stable_error_envelope(self):
        writer = FakeWriter()
        web._send_api_error(writer, "400 Bad Request", "invalid", "Invalid input.")
        header, body = bytes(writer.buf).split(b"\r\n\r\n", 1)
        self.assertIn(b"Content-Type: application/json", header)
        self.assertIn(b"Cache-Control: no-store", header)
        self.assertEqual(body, b'{"error": "invalid", "message": "Invalid input."}')


class TestRouteMethodPolicy(unittest.TestCase):
    def test_complete_route_method_matrix(self):
        usual = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE")
        for path, policy in web_routing.ROUTES.items():
            allowed, expected_error, _body_limit = policy
            for method in usual:
                error = web._route_method_error(path, method)
                if method in allowed:
                    self.assertIsNone(error, (path, method))
                else:
                    self.assertEqual(error, expected_error, (path, method))

    def test_read_only_routes_accept_get_and_head(self):
        for path in web.READ_ONLY_ROUTES:
            self.assertIsNone(web._route_method_error(path, "GET"), path)
            self.assertIsNone(web._route_method_error(path, "HEAD"), path)

    def test_read_only_routes_reject_mutating_methods(self):
        for path in web.READ_ONLY_ROUTES:
            self.assertEqual(web._route_method_error(path, "POST"),
                             "GET required", path)

    def test_declarative_action_route_methods(self):
        self.assertIsNone(web._route_method_error("/api/session", "POST"))
        self.assertIsNone(web._route_method_error("/api/config", "PUT"))
        self.assertIsNone(web._route_method_error("/save", "POST"))


class TestHttpDomainBoundaries(unittest.TestCase):
    def test_all_mutating_domains_have_dedicated_async_handlers(self):
        for name in ("_handle_session_request", "_handle_config_request",
                     "_handle_diagnostics_request", "_handle_status_request",
                     "_handle_ui_live_request",
                     "_handle_measurement_gate_request",
                     "_handle_field_setup_request",
                     "_handle_tcp_auth_request",
                     "_handle_maintenance_request", "_handle_gnss_request",
                     "_handle_tracking_request", "_serve_event_stream"):
            self.assertTrue(inspect.iscoroutinefunction(getattr(web, name)), name)

    def test_transport_dispatch_no_longer_contains_domain_implementations(self):
        source = inspect.getsource(web.handle_http_client)
        self.assertLess(len(source.splitlines()), 300)
        self.assertNotIn("validate_settings(", source)
        self.assertNotIn("send_command(", source)
        self.assertNotIn("read_flight_recorder(", source)
        self.assertNotIn("rotate_stream_token(", source)
        self.assertEqual(web._route_method_error("/api/config", "POST"),
                         "GET or PUT required")

    def test_unknown_routes_are_left_to_the_404_handler(self):
        self.assertIsNone(web._route_method_error("/missing", "POST"))

    def test_tracking_route_has_larger_declared_body_limit(self):
        self.assertEqual(web._route_body_limit("/api/tracking"),
                         web.MAX_TRACKING_BODY_BYTES)
        self.assertEqual(web._route_body_limit("/api/config"),
                         web.MAX_BODY_BYTES)

    def test_only_the_restore_route_has_the_larger_config_limit(self):
        self.assertEqual(web._route_body_limit("/api/config/restore"),
                         web_routing.CONFIG_RESTORE_BODY_LIMIT)
        for path in ("/api/config", "/save", "/api/config/apply", "/api/measurement-gate"):
            self.assertEqual(web._route_body_limit(path), web.MAX_BODY_BYTES, path)


class TestRenderSetup(unittest.TestCase):
    def setUp(self):
        self.saved = dict(cfg.CONFIG)
        self.saved_identity = web.app.identity

    def tearDown(self):
        cfg.CONFIG.clear()
        cfg.CONFIG.update(self.saved)
        web.app.identity = self.saved_identity

    def test_no_placeholders_remain_standing(self):
        # V9.0 used str.format() and failed on the CSS staples.
        html = web.render_setup()
        for key in ("version", "wifi_ssid", "ntrip_host", "ntrip_port",
                    "ntrip_mount", "ntrip_user", "ap_ssid", "ap_ip"):
            self.assertNotIn("{%s}" % key, html)

    def test_external_stylesheet_is_linked(self):
        html = web.render_setup()
        self.assertIn('href="/pages.css?v=%s"' % assets.ASSET_VERSION, html)
        self.assertNotIn("<style", html)

    def test_values_are_used(self):
        cfg.CONFIG["wifi_ssid"] = "MeinNetz"
        cfg.CONFIG["ntrip_mount"] = "SYNTH"
        html = web.render_setup()
        self.assertIn("MeinNetz", html)
        self.assertIn("SYNTH", html)

    def test_passwords_do_not_appear_in_the_html(self):
        cfg.CONFIG["wifi_pass"] = "streng-secret-wlan"
        cfg.CONFIG["ntrip_pass"] = "streng-secret-ntrip"
        cfg.CONFIG["ap_pass"] = "streng-secret-ap"
        html = web.render_setup()
        self.assertNotIn("streng-secret-wlan", html)
        self.assertNotIn("streng-secret-ntrip", html)
        self.assertNotIn("streng-secret-ap", html)

    def test_device_code_is_masked_by_default_and_switchable(self):
        web.app.identity = dict(web.app.identity or {}, device_code="CODE-1234")
        html = web.render_setup()
        self.assertIn('id="device-code" type="password"', html)
        self.assertIn('value="CODE-1234" readonly', html)
        self.assertIn("data-secret-toggle", html)
        with open("pages.js", encoding="utf-8") as source:
            self.assertIn('input.type = show ? "text" : "password"', source.read())
        self.assertIn('aria-pressed="false"', html)
        self.assertIn(">Show</button>", html)

    def test_form_fields_cover_the_configuration(self):
        html = web.render_setup()
        for field in ("wifi_ssid", "wifi_pass", "ntrip_host", "ntrip_port",
                     "ntrip_mount", "ntrip_user", "ntrip_pass"):
            self.assertIn('name="%s"' % field, html)


class TestMeasurementEndpoints(unittest.TestCase):
    PROGRESS_KEYS = ["active", "collected", "total", "mean_h_sigma_m", "mean_v_sigma_m",
                     "mean_alt_m", "cancelled", "waiting_reasons", "rejected_samples",
                     "mode", "phase", "elapsed_sec"]

    def post(self, payload):
        writer = FakeWriter()
        with mock.patch.object(web, "_admin_allowed", return_value=True), \
                mock.patch.object(web.tracker, "_loaded", True), \
                mock.patch.object(web.tracker, "live_status", return_value={}), \
                mock.patch.object(web, "log"):
            asyncio.run(web._handle_tracking_request(
                writer, "/api/tracking", "POST", "", json.dumps(payload).encode(), False, {}))
        header, body = bytes(writer.buf).split(b"\r\n\r\n", 1)
        return header.split(b"\r\n", 1)[0], json.loads(body)

    def post_real(self, tracker, payload):
        """One tracking POST against a real tracker, failures answered as handle_http_client does."""
        raw = FakeWriter()
        writer = web._HttpAnswer(raw)

        async def run():
            try:
                await web._handle_tracking_request(
                    writer, "/api/tracking", "POST", "", json.dumps(payload).encode(), False, {})
            except Exception as error:
                await web._answer_failure(writer, error)
        with mock.patch.object(web, "tracker", tracker), \
                mock.patch.object(web, "_admin_allowed", return_value=True), \
                mock.patch.object(web, "log"):
            asyncio.run(run())
        header, body = bytes(raw.buf).split(b"\r\n\r\n", 1)
        return header.split(b"\r\n", 1)[0], json.loads(body)

    def real_tracker(self):
        """A real tracker in a temporary folder with one active project."""
        folder = tempfile.mkdtemp(prefix="esp-rtk-http-undo-")
        self.addCleanup(shutil.rmtree, folder, True)
        storage = mock.patch.object(tracking.Tracker, "storage_capacity", return_value={
            "free_flash_bytes": 8_000_000, "required_flash_reserve_bytes": 3_000_000,
            "estimated_temporary_bytes": 16_384, "writable": True})
        storage.start()
        self.addCleanup(storage.stop)
        path = os.path.join(folder, "survey")
        tracker = tracking.Tracker(path)
        self.addCleanup(tracker.point_store.close)
        self.addCleanup(tracker._sync_journal, True)
        tracker.create_project("HTTP")
        self.tracker_path = path
        # A failure answer counts and lists the error; keep both out of the shared state.
        for patch in (mock.patch.dict(state.app.stats), mock.patch.object(state.app, "errors", [])):
            patch.start()
            self.addCleanup(patch.stop)
        return tracker

    def live_candidate(self, tracker):
        with mock.patch.object(web, "tracker", tracker):
            return web.ui_live_snapshot()["undo_candidate"]

    def durable_count(self):
        """The feature count of a tracker freshly opened from the same path."""
        reopened = tracking.Tracker(self.tracker_path)
        self.addCleanup(reopened.point_store.close)
        return reopened.feature_count()

    def test_post_real_answers_a_real_action(self):
        tracker = self.real_tracker()
        status, body = self.post_real(tracker, {"action": "create_project", "name": "Second"})
        self.assertIn(b"200 OK", status)
        self.assertEqual(body["name"], "Second")
        self.assertEqual(body["tracking_live"]["active_project"]["name"], "Second")

    def test_rejected_measurement_answers_reason_codes(self):
        rejected = tracking.MeasurementRejected(
            "Measurement quality rejected: no_position, too_few_satellites.",
            ["no_position", "too_few_satellites"])
        with mock.patch.object(measurement.measurement_session, "collect",
                               mock.AsyncMock(side_effect=rejected)):
            status, body = self.post({"action": "add_vertex"})
        self.assertIn(b"400 Bad Request", status)
        self.assertEqual(body, {"error": "invalid_tracking_request",
                                "message": "Measurement quality rejected: "
                                           "no_position, too_few_satellites.",
                                "reasons": ["no_position", "too_few_satellites"]})

    def test_undo_passes_the_expectation_and_answers_409_on_change(self):
        expect = {"kind": "vertex", "feature_id": "l1", "index": 3, "record_order": 9}
        for payload, args in (({"action": "undo", "expect": expect}, (expect,)),
                              ({"action": "undo", "expect": None}, (None,)),
                              ({"action": "undo"}, (tracking.UNDO_UNCHECKED,))):
            with self.subTest(payload=payload), \
                    mock.patch.object(web.tracker, "undo_last") as undo_last:
                status, body = self.post(payload)
                self.assertIn(b"200 OK", status)
                undo_last.assert_called_once_with(*args)
        with mock.patch.object(web.tracker, "undo_last",
                               side_effect=tracking.UndoCandidateChanged()):
            status, body = self.post({"action": "undo", "expect": expect})
        self.assertIn(b"409 Conflict", status)
        self.assertEqual(body, {"error": "undo_candidate_changed",
                                "message": "The last recorded point changed. Nothing was removed."})

    def test_an_explicit_null_undo_answers_409_end_to_end(self):
        tracker = self.real_tracker()
        tracker.add_asset("tap", "A1", "", FIX)
        count = tracker.feature_count()
        status, body = self.post_real(tracker, {"action": "undo", "expect": None})
        self.assertIn(b"409 Conflict", status)
        self.assertEqual(body["error"], "undo_candidate_changed")
        self.assertEqual(tracker.feature_count(), count)
        status, body = self.post_real(tracker, {"action": "undo",
                                                "expect": self.live_candidate(tracker)})
        self.assertIn(b"200 OK", status)
        self.assertEqual(tracker.feature_count(), count - 1)

    def test_a_stale_undo_after_a_restore_answers_409_end_to_end(self):
        tracker = self.real_tracker()
        with mock.patch.object(measurement.measurement_session, "collect",
                               mock.AsyncMock(return_value=dict(FIX))):
            status, _body = self.post_real(tracker, {"action": "add_asset", "name": "A1",
                                                     "object_type": "tap",
                                                     "request_id": "r-old"})
        self.assertIn(b"200 OK", status)
        old = self.live_candidate(tracker)
        self.assertEqual(old["request_id"], "r-old")
        tracker.restore(tracker.backup())
        new = self.live_candidate(tracker)
        self.assertEqual(new["feature_id"], old["feature_id"])
        self.assertIsNone(new["request_id"])
        self.assertNotEqual(new["inventory_epoch"], old["inventory_epoch"])
        count = tracker.feature_count()
        stale = {key: old[key] for key in ("inventory_epoch", "kind", "feature_id", "index")}
        stale["record_order"] = new["record_order"]
        status, body = self.post_real(tracker, {"action": "undo", "expect": stale})
        self.assertIn(b"409 Conflict", status)
        self.assertEqual(body["error"], "undo_candidate_changed")
        self.assertEqual(tracker.feature_count(), count)
        status, body = self.post_real(tracker, {"action": "undo", "expect": new})
        self.assertIn(b"200 OK", status)
        self.assertEqual(tracker.feature_count(), count - 1)

    def test_capture_posts_tag_and_echo_their_request_id(self):
        tracker = self.real_tracker()
        with mock.patch.object(measurement.measurement_session, "collect",
                               mock.AsyncMock(return_value=dict(FIX))):
            status, body = self.post_real(tracker, {"action": "add_asset", "name": "A1",
                                                    "object_type": "tap",
                                                    "request_id": "r7k2m9x"})
            self.assertIn(b"200 OK", status)
            self.assertEqual(body["request_id"], "r7k2m9x")
            self.assertEqual(body["tracking_live"]["undo_candidate"]["request_id"], "r7k2m9x")
            self.assertEqual(self.live_candidate(tracker)["request_id"], "r7k2m9x")
            status, body = self.post_real(tracker, {"action": "start_line", "name": "L1",
                                                    "auto_distance_m": 0,
                                                    "recording_mode": "survey",
                                                    "request_id": "r-line1"})
            self.assertIn(b"200 OK", status)
            candidate = self.live_candidate(tracker)
            self.assertEqual((candidate["kind"], candidate["feature_id"],
                              candidate["request_id"]), ("vertex", body["id"], "r-line1"))
            status, body = self.post_real(tracker, {"action": "add_vertex",
                                                    "request_id": "r-v2"})
            self.assertIn(b"200 OK", status)
            self.assertEqual(body["request_id"], "r-v2")
            self.assertEqual((self.live_candidate(tracker)["index"],
                              self.live_candidate(tracker)["request_id"]), (2, "r-v2"))
            tracker.set_line_state("finish")
            tracker.add_asset("tap", "B1", "", FIX)
            self.assertTrue(tracker.tag_request("r-b1", "asset",
                                                self.live_candidate(tracker)["feature_id"]))
            status, body = self.post_real(tracker, {"action": "start_line", "name": "Route",
                                                    "recording_mode": "route",
                                                    "request_id": "r-route"})
            self.assertIn(b"200 OK", status)
            self.assertEqual(body["request_id"], "r-route")
            self.assertEqual((self.live_candidate(tracker)["label"],
                              self.live_candidate(tracker)["request_id"]), ("B1", "r-b1"))
            tracker.set_line_state("finish")
            status, body = self.post_real(tracker, {"action": "add_asset", "name": "C1",
                                                    "object_type": "tap"})
            self.assertIn(b"200 OK", status)
            self.assertNotIn("request_id", body)
            self.assertIsNone(self.live_candidate(tracker)["request_id"])

    def test_an_invalid_request_id_is_refused_before_measuring(self):
        tracker = self.real_tracker()
        collect = mock.AsyncMock(return_value=dict(FIX))
        count = tracker.feature_count()
        with mock.patch.object(measurement.measurement_session, "collect", collect):
            for request_id in (123, "", "x" * 33, "a b", chr(0xE4) + "1"):
                with self.subTest(request_id=request_id):
                    status, body = self.post_real(tracker, {
                        "action": "add_asset", "name": "A", "object_type": "tap",
                        "request_id": request_id})
                    self.assertIn(b"400 Bad Request", status)
                    self.assertEqual(body, {"error": "invalid_tracking_request",
                                            "message": "The request id is invalid."})
            collect.assert_not_called()
            self.assertEqual(tracker.feature_count(), count)
            for request_id in ("x" * 32, "r-_A9"):
                with self.subTest(request_id=request_id):
                    status, body = self.post_real(tracker, {
                        "action": "add_asset", "name": "", "object_type": "tap",
                        "request_id": request_id})
                    self.assertIn(b"200 OK", status)
                    self.assertEqual(body["request_id"], request_id)

    def capture_after(self, tracker, request_id):
        with mock.patch.object(measurement.measurement_session, "collect",
                               mock.AsyncMock(return_value=dict(FIX))):
            return self.post_real(tracker, {"action": "add_asset", "name": "A1",
                                            "object_type": "tap", "request_id": request_id})

    def test_a_failure_after_the_durable_append_answers_500_and_keeps_the_point(self):
        tracker = self.real_tracker()
        before = tracker.feature_count()
        real = tracker._append

        def failing(event):
            real(event)
            raise RuntimeError("after append")
        with mock.patch.object(tracker, "_append", side_effect=failing):
            status, body = self.capture_after(tracker, "r-after")
        self.assertEqual(status, b"HTTP/1.0 500 Internal Server Error")
        self.assertEqual(body, {"error": "internal_error",
                                "message": "The device encountered an internal error."})
        self.assertEqual(tracker.feature_count(), before + 1)
        tracker._sync_journal(True)
        self.assertEqual(self.durable_count(), before + 1)
        self.assertIsNone(self.live_candidate(tracker)["request_id"])

    def test_a_failure_while_tagging_answers_500_and_keeps_the_point(self):
        tracker = self.real_tracker()
        before = tracker.feature_count()
        with mock.patch.object(tracker, "tag_request", side_effect=RuntimeError("tag")):
            status, body = self.capture_after(tracker, "r-tag")
        self.assertEqual(status, b"HTTP/1.0 500 Internal Server Error")
        self.assertEqual(body["error"], "internal_error")
        self.assertEqual(tracker.feature_count(), before + 1)
        tracker._sync_journal(True)
        self.assertEqual(self.durable_count(), before + 1)
        self.assertIsNone(self.live_candidate(tracker)["request_id"])

    def test_a_failure_while_answering_answers_503_and_keeps_the_tag(self):
        tracker = self.real_tracker()
        before = tracker.feature_count()
        with mock.patch.object(tracker, "live_status", side_effect=MemoryError()):
            status, body = self.capture_after(tracker, "r-answer")
        self.assertEqual(status, b"HTTP/1.0 503 Service Unavailable")
        self.assertEqual(body, {"error": "device_busy",
                                "message": "Not enough memory on the device; try again."})
        self.assertEqual(tracker.feature_count(), before + 1)
        tracker._sync_journal(True)
        self.assertEqual(self.durable_count(), before + 1)
        self.assertEqual(self.live_candidate(tracker)["request_id"], "r-answer")

    def test_a_failure_before_measuring_answers_503_without_a_point(self):
        tracker = self.real_tracker()
        before = tracker.feature_count()
        with mock.patch.object(measurement.measurement_session, "collect",
                               mock.AsyncMock(side_effect=MemoryError())):
            status, body = self.post_real(tracker, {"action": "add_asset", "name": "A1",
                                                    "object_type": "tap",
                                                    "request_id": "r-before"})
        self.assertEqual(status, b"HTTP/1.0 503 Service Unavailable")
        self.assertEqual(body["error"], "device_busy")
        self.assertEqual(tracker.feature_count(), before)
        tracker._sync_journal(True)
        self.assertEqual(self.durable_count(), before)

    def test_survey_loading_is_refused_before_any_mutation(self):
        tracker = self.real_tracker()
        before = tracker.feature_count()
        collect = mock.AsyncMock(return_value=dict(FIX))
        with mock.patch.object(measurement.measurement_session, "collect", collect), \
                mock.patch.object(tracker, "_loaded", False):
            status, body = self.post_real(tracker, {"action": "add_asset", "name": "A1",
                                                    "object_type": "tap",
                                                    "request_id": "r-loading"})
        self.assertEqual(status, b"HTTP/1.0 503 Service Unavailable")
        self.assertEqual(body["error"], "survey_loading")
        collect.assert_not_called()
        self.assertEqual(tracker.feature_count(), before)
        tracker._sync_journal(True)
        self.assertEqual(self.durable_count(), before)

    def test_other_tracking_errors_keep_the_envelope(self):
        with mock.patch.object(web.tracker, "create_project",
                               side_effect=ValueError("Name exists.")):
            status, body = self.post({"action": "create_project", "name": "X"})
        self.assertIn(b"400 Bad Request", status)
        self.assertEqual(body, {"error": "invalid_tracking_request", "message": "Name exists."})

    def test_progress_shape_is_unchanged(self):
        for query in ("view=progress", ""):
            writer = FakeWriter()
            with mock.patch.object(web, "_admin_allowed", return_value=True), \
                    mock.patch.object(web.tracker, "_loaded", True), \
                    mock.patch.object(web.tracker, "status", return_value={}):
                asyncio.run(web._handle_tracking_request(
                    writer, "/api/tracking", "GET", query, b"", False, {}))
            body = json.loads(bytes(writer.buf).split(b"\r\n\r\n", 1)[1])
            self.assertEqual(sorted(body["measurement_progress"]), sorted(self.PROGRESS_KEYS), query)
            self.assertEqual(body["measurement_progress"],
                             measurement.measurement_session.snapshot(), query)
            self.assertEqual(sorted(body["storage_operation"]),
                             ["error", "operation", "processed", "state", "total"], query)
        writer = FakeWriter()
        compact = {"revision": 7, "device_time": 100, "active_project": None, "active_line": None}
        with mock.patch.object(web, "_read_allowed", return_value=True), \
                mock.patch.object(web.tracker, "live_status", return_value=compact), \
                mock.patch.object(web, "board_metrics", return_value={"ram_free_bytes": 1}):
            asyncio.run(web._handle_ui_live_request(writer, "", False, {}))
        body = json.loads(bytes(writer.buf).split(b"\r\n\r\n", 1)[1])
        self.assertEqual(sorted(body["measurement_progress"]), sorted(self.PROGRESS_KEYS))
        self.assertEqual(body["measurement_progress"], measurement.measurement_session.snapshot())

    NETWORK_KEYS = ["access_state", "last_ntrip_error_code", "last_wifi_error",
                    "net_mode", "ntrip_config", "ntrip_state", "ntrip_substate",
                    "reconnects", "wifi_status_code"]

    def get_progress(self):
        writer = FakeWriter()
        with mock.patch.object(web, "_admin_allowed", return_value=True), \
                mock.patch.object(web.tracker, "_loaded", True), \
                mock.patch.object(web.tracker, "status", return_value={}):
            asyncio.run(web._handle_tracking_request(
                writer, "/api/tracking", "GET", "view=progress", b"", False, {}))
        return json.loads(bytes(writer.buf).split(b"\r\n\r\n", 1)[1])

    def test_progress_carries_the_live_network_block(self):
        stats = {"ntrip_state": "auth_error", "ntrip_config": "ready",
                 "last_ntrip_error_code": "auth_error"}
        with mock.patch.dict(state.app.stats, stats):
            body = self.get_progress()
            self.assertEqual(body["network"], web.network_view())
            live_network = web.ui_live_snapshot()["network"]
        self.assertEqual(sorted(body["network"]), sorted(live_network))
        self.assertEqual(sorted(body["network"]), self.NETWORK_KEYS)
        self.assertEqual(body["network"], live_network)
        self.assertEqual((body["network"]["ntrip_state"], body["network"]["ntrip_config"],
                          body["network"]["last_ntrip_error_code"]),
                         ("auth_error", "ready", "auth_error"))

    def test_network_view_is_small(self):
        # The longest known value of each closed set; the two error codes at 32 characters.
        stats = {"access_state": "CONNECTING", "ntrip_state": "waiting_wifi",
                 "ntrip_substate": "stale", "ntrip_config": "switched_off",
                 "net_mode": "AP_RECOVERY", "last_ntrip_error_code": "n" * 32,
                 "last_wifi_error": "w" * 32, "wifi_status_code": -1000,
                 "wifi_reconnects": 99999}
        with mock.patch.dict(state.app.stats, stats):
            view = web.network_view()
        self.assertEqual(view["last_wifi_error"], "w" * 32)
        self.assertEqual(view["wifi_status_code"], -1000)
        self.assertLessEqual(len(json.dumps(view)), 400)

    def test_cancel_action_answers_cancelled(self):
        with mock.patch.object(measurement.measurement_session, "cancel",
                               return_value={"cancelled": True}) as cancel:
            status, body = self.post({"action": "cancel_measurement"})
        self.assertIn(b"200 OK", status)
        self.assertIs(body["cancelled"], True)
        cancel.assert_called_once_with()

    def test_measuring_actions_use_the_session(self):
        collect = mock.AsyncMock(side_effect=ValueError("Measurement cancelled."))
        with mock.patch.object(measurement.measurement_session, "collect", collect):
            status, body = self.post({"action": "add_vertex", "samples": 3})
        self.assertIn(b"400 Bad Request", status)
        self.assertEqual(body["error"], "invalid_tracking_request")
        collect.assert_awaited_once_with(3)

    def test_flash_reserve_refusal_is_507(self):
        message = "Flash reserve would fall below 20 percent."
        with mock.patch.object(web.tracker, "create_project",
                               side_effect=tracking.StorageReserveError(message)):
            status, body = self.post({"action": "create_project", "name": "X"})
        self.assertIn(b"507 Insufficient Storage", status)
        self.assertEqual(body, {"error": "storage_reserve", "message": message})
        with mock.patch.object(web.tracker, "create_project",
                               side_effect=ValueError("Name exists.")):
            status, body = self.post({"action": "create_project", "name": "X"})
        self.assertIn(b"400 Bad Request", status)
        self.assertEqual(body["error"], "invalid_tracking_request")


class TestFixFreshnessContract(unittest.TestCase):
    def setUp(self):
        self.previous = _save_fix_and_gate()
        self.now = 50_000
        patcher = mock.patch.object(time, "ticks_ms", side_effect=lambda: self.now)
        patcher.start(); self.addCleanup(patcher.stop)
        state.app._last_fix = exact_fix({
            "utc": "1", "lat": 50.0, "lon": 8.0, "alt": 100.0, "qual": 4,
            "sats": 20, "hdop": 0.6, "correction_age_sec": 1.0,
            "fix_status_text": "RTK_FIXED",
            "receiver_accuracy": {"horizontal_sigma_m": .01, "altitude_sigma_m": .02}})
        state.app._fix_ticks_ms, state.app._fix_seq, state.app._fix_rev = self.now, 7, 2
        state.app._fix_expired = False
        state.app.reevaluate_gate()

    def tearDown(self):
        _restore_fix_and_gate(self.previous)

    def live(self):
        writer = FakeWriter()
        compact = {"revision": 7, "device_time": 100, "active_project": None, "active_line": None}
        with mock.patch.object(web, "_read_allowed", return_value=True), \
                mock.patch.object(web.tracker, "live_status", return_value=compact), \
                mock.patch.object(web, "board_metrics", return_value={"ram_free_bytes": 1}):
            asyncio.run(web._handle_ui_live_request(writer, "", False, {}))
        return json.loads(bytes(writer.buf).split(b"\r\n\r\n", 1)[1])

    def test_fresh_fix_carries_age_sequence_and_revision(self):
        self.now += 1200
        body = self.live()
        self.assertEqual(body["current_fix"]["fix_age_ms"], 1200)
        self.assertEqual(body["current_fix"]["fix_seq"], 7)
        self.assertEqual(body["current_fix"]["fix_rev"], 2)
        self.assertFalse(body["current_fix"]["stale"])
        self.assertTrue(body["current_quality"]["accepted"])
        self.assertEqual(body["system"]["boot_count"], state.app.stats["boot_count"])

    def test_stale_fix_keeps_the_position_but_reports_no_position(self):
        self.now += 3000
        body = self.live()
        self.assertTrue(body["current_fix"]["stale"])
        self.assertEqual(body["current_fix"]["lat"], 50.0)
        self.assertFalse(body["current_quality"]["accepted"])
        self.assertIn("no_position", body["current_quality"]["reasons"])

    def test_status_and_live_report_the_same_age(self):
        self.now += 2500
        live = self.live()
        writer = FakeWriter()
        with mock.patch.object(web, "_read_allowed", return_value=True), \
                mock.patch.object(web, "_instances", {}), \
                mock.patch.object(web, "board_metrics", return_value={}):
            asyncio.run(web._handle_status_request(writer, "/status", "", False, {}))
        status = json.loads(bytes(writer.buf).split(b"\r\n\r\n", 1)[1])
        self.assertEqual(status["fix_age_sec"], 2.5)
        self.assertEqual(status["fix"]["fix_age_ms"], live["current_fix"]["fix_age_ms"])
        self.assertEqual(status["fix"]["fix_seq"], 7)

    def test_status_age_ignores_a_26_year_rtc_jump(self):
        self.now += 1500
        writer = FakeWriter()
        with mock.patch.object(time, "time", return_value=EPOCH_2000 + JUMP_26Y_S), \
                mock.patch.object(web, "_read_allowed", return_value=True), \
                mock.patch.object(web, "_instances", {}), \
                mock.patch.object(web, "board_metrics", return_value={}):
            asyncio.run(web._handle_status_request(writer, "/status", "", False, {}))
        status = json.loads(bytes(writer.buf).split(b"\r\n\r\n", 1)[1])
        self.assertEqual(status["fix_age_sec"], 1.5)

    def status(self, instances):
        writer = FakeWriter()
        with mock.patch.object(web, "_read_allowed", return_value=True), \
                mock.patch.object(web, "_instances", instances), \
                mock.patch.object(web, "board_metrics", return_value={}):
            asyncio.run(web._handle_status_request(writer, "/status", "", False, {}))
        return json.loads(bytes(writer.buf).split(b"\r\n\r\n", 1)[1])

    def test_live_and_status_carry_epoch_age_stale_flag_and_substate(self):
        summary = {"messages": {}, "msm": [], "bytes_total": 1, "last_msg_age_sec": 0.3,
                   "arrival_age_sec": 0.3, "epoch_age_sec": 60.0, "epoch_source": "GPS",
                   "stale": True}
        ntrip = mock.Mock(spec=["rtcm"])
        ntrip.rtcm.summary.return_value = summary
        ntrip.rtcm.silence_ms.return_value = 1200
        # #38b: no service CRS announced.
        ntrip.rtcm.frame_suggestion.return_value = None
        ntrip.rtcm.crs_summary.return_value = {
            "service_crs": None, "rtcm_crs": None,
            "crs_frames": {"crc_failures": 0, "rejected": 0}}
        stats = {"ntrip_state": "streaming", "ntrip_substate": "stale"}
        with mock.patch.dict(state.app.stats, stats), \
                mock.patch.dict(web._instances, {"ntrip": ntrip}):
            live = self.live()
            status = self.status({"ntrip": ntrip})
        self.assertEqual(live["link"]["rtcm_silence_ms"], 1200)
        self.assertEqual(live["link"]["corrections"], "stale")
        self.assertEqual(status["link"], live["link"])
        self.assertEqual(live["rtcm"]["epoch_age_sec"], 60.0)
        self.assertEqual(live["rtcm"]["arrival_age_sec"], 0.3)
        self.assertIs(live["rtcm"]["stale"], True)
        self.assertEqual(live["network"]["ntrip_state"], "streaming")
        self.assertEqual(live["network"]["ntrip_substate"], "stale")
        self.assertEqual(live["current_fix"]["correction_age_sec"], 1.0)
        self.assertEqual(status["rtcm"]["epoch_age_sec"], 60.0)
        self.assertEqual(status["fix"]["correction_age_sec"], 1.0)
        self.assertEqual(status["stats"]["ntrip_substate"], "stale")
        ntrip.rtcm.silence_ms.return_value = None        # no message number seen yet
        with mock.patch.dict(state.app.stats, stats), \
                mock.patch.dict(web._instances, {"ntrip": ntrip}):
            live = self.live()
            status = self.status({"ntrip": ntrip})
        self.assertIsNone(live["link"]["rtcm_silence_ms"])
        self.assertIsNone(status["link"]["rtcm_silence_ms"])

    def test_live_and_status_without_an_ntrip_client_have_no_epoch_age(self):
        with mock.patch.dict(web._instances, {}, clear=True):
            live = self.live()
            status = self.status({})
        self.assertNotIn("epoch_age_sec", live["rtcm"])
        self.assertIsNone(live["network"]["ntrip_substate"])
        self.assertIn("ntrip_state", live["network"])
        self.assertIsNone(status["stats"].get("ntrip_substate"))
        self.assertEqual(status["fix"]["correction_age_sec"], 1.0)

    def test_fresh_stream_ages_agree_within_three_seconds(self):
        from test_rtcm import SUNDAY_0001, msm_epoch
        from support import net
        counter = net.RtcmCounter()
        # A fresh net.app built under the test clock: the RTCM stamp and the link's
        # rtcm_silence_ms read its uptime, the shared app's must stay untouched.
        with mock.patch.object(net, "app", state.AppState()), \
                mock.patch.object(time, "gmtime", return_value=SUNDAY_0001):
            counter.feed(msm_epoch(1075, 77_000))            # 1 s old on the 78 s GPS time of week
            self.now += 300                                   # the frame arrived 300 ms ago
            ntrip = mock.Mock(spec=["rtcm"])
            ntrip.rtcm = counter
            with mock.patch.dict(web._instances, {"ntrip": ntrip}):
                body = self.live()
        ages = [body["rtcm"]["arrival_age_sec"], body["rtcm"]["epoch_age_sec"],
                body["current_fix"]["correction_age_sec"]]
        self.assertEqual(ages, [0.3, 1.3, 1.0])
        for first in ages:
            for second in ages:
                self.assertLessEqual(abs(first - second), 3.0)
        self.assertIs(body["rtcm"]["stale"], False)
        self.assertEqual(body["link"]["rtcm_silence_ms"], 300)

    def test_status_reads_the_clock_once(self):
        start = self.now + 2900
        clock = {"now": start}
        def ticking():
            value = clock["now"]
            clock["now"] += 200                  # every read moves the clock on
            return value
        writer = FakeWriter()
        with mock.patch.object(time, "ticks_ms", side_effect=ticking), \
                mock.patch.object(web, "_read_allowed", return_value=True), \
                mock.patch.object(web, "_instances", {}), \
                mock.patch.object(web, "board_metrics", return_value={}):
            asyncio.run(web._handle_status_request(writer, "/status", "", False, {}))
        status = json.loads(bytes(writer.buf).split(b"\r\n\r\n", 1)[1])
        self.assertEqual(status["fix"]["fix_age_ms"], 2900)
        self.assertFalse(status["fix"]["stale"])
        self.assertTrue(status["quality_streak"]["gate_accepted"])

    def test_tracking_progress_and_default_get_use_the_fix_view(self):
        self.now += 3000
        for query in ("view=progress", ""):
            writer = FakeWriter()
            with mock.patch.object(web, "_admin_allowed", return_value=True), \
                    mock.patch.object(web.tracker, "_loaded", True), \
                    mock.patch.object(web.tracker, "status", return_value={}):
                asyncio.run(web._handle_tracking_request(
                    writer, "/api/tracking", "GET", query, b"", False, {}))
            body = json.loads(bytes(writer.buf).split(b"\r\n\r\n", 1)[1])
            self.assertTrue(body["current_fix"]["stale"], query)
            self.assertEqual(body["current_fix"]["fix_seq"], 7, query)
            self.assertIn("no_position", body["current_quality"]["reasons"], query)

    def test_diagnostics_snapshot_marks_the_fix_as_stale(self):
        self.now += 4000
        fix = web.diagnostics_snapshot()["fix"]
        self.assertTrue(fix["stale"])
        self.assertEqual(fix["fix_age_ms"], 4000)
        self.assertEqual((fix["fix_seq"], fix["fix_rev"]), (7, 2))
        self.assertEqual(fix["lat"], 50.0)

    def test_post_add_vertex_with_a_single_stale_sample_is_rejected(self):
        # Fresh case first: once the stale reading has set the sticky _fix_expired flag,
        # stepping the clock back would still count as stale (wrap guard).
        # Third case: the counter-check. With the stale check disabled test-locally the same
        # stale request succeeds, so the 400 really comes from fix_is_stale.
        for advance, status, stale_check in ((0, b"200 OK", True), (3000, b"400 Bad Request", True),
                                             (3000, b"200 OK", False)):
            with self.subTest(advance=advance, stale_check=stale_check):
                self.now = 50_000 + advance          # fix stamped at 50_000 in setUp
                writer = FakeWriter()
                with mock.patch.object(web, "_admin_allowed", return_value=True), \
                        mock.patch.object(web.tracker, "_loaded", True), \
                        mock.patch.object(web.tracker, "add_vertex", return_value={"id": "v"}) as add, \
                        mock.patch.object(web, "log"), \
                        (contextlib.nullcontext() if stale_check else
                         mock.patch.object(state.app, "fix_is_stale", return_value=False)):
                    asyncio.run(web._handle_tracking_request(
                        writer, "/api/tracking", "POST", "",
                        b'{"action":"add_vertex","samples":1}', False, {}))
                header, body = bytes(writer.buf).split(b"\r\n\r\n", 1)
                self.assertIn(status, header.split(b"\r\n", 1)[0])
                if status.startswith(b"400"):
                    payload = json.loads(body)
                    self.assertEqual(payload["error"], "invalid_tracking_request")
                    self.assertRegex(payload["message"], r"stale \(no GGA for 3 s\)")
                    add.assert_not_called()
                    self.assertEqual(measurement.measurement_session.progress["phase"], "failed")
                else:
                    add.assert_called_once()
                self.assertFalse(measurement.measurement_session.progress["active"])


class TestNoFalseErrors(unittest.TestCase):
    """Browsers open connections as a precautionary measure over which a request never comes. When ERROR was reported, it landed in the flash and clogged /errors."""

    def test_typical_client_abortions_are_not_server_errors(self):
        for errno in (32, 54, 103, 104, 107, 128):
            self.assertTrue(web._client_disconnected(OSError(errno, "weg")))
        self.assertFalse(web._client_disconnected(OSError(12, "Memory")))


class ResetWriter(FakeWriter):
    """A client gone before the answer: every write attempt fails with ECONNRESET."""

    def __init__(self):
        super().__init__()
        self.attempts = []

    def write(self, data):
        self.attempts.append(bytes(data))
        raise OSError(104, "reset")

    def close(self):
        pass

    async def wait_closed(self):
        pass


class TestFailureAnswers(unittest.TestCase):
    """A failure in a handler is answered once, before the answer began, and never escapes."""

    INTERNAL = {"error": "internal_error",
                "message": "The device encountered an internal error."}

    def setUp(self):
        from test_access_v11 import FakeReader, http_request
        self.reader, self.http_request = FakeReader, http_request
        self.log = self.patch(web, "log")
        self.log_error = self.patch(web.app, "log_error")
        self.errors = state.app.stats["http_errors"]
        self.connections = web._http_connections
        access.reset()
        self.addCleanup(access.reset)

    def patch(self, target, name, **kwargs):
        patcher = mock.patch.object(target, name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def answer_failure(self, error, writer=None):
        """(written bytes, writer) after _answer_failure on a wrapped FakeWriter."""
        writer = writer or FakeWriter()
        asyncio.run(web._answer_failure(web._HttpAnswer(writer), error))
        return bytes(writer.buf), writer

    def split(self, raw):
        head, _sep, body = raw.partition(b"\r\n\r\n")
        return head, json.loads(body)

    def test_key_error_in_a_handler_answers_500_json(self):
        with mock.patch.object(web, "access_info", side_effect=KeyError("hostname")):
            response = self.http_request(b"GET /api/access HTTP/1.0\r\n\r\n")
        self.assertTrue(response.startswith(b"HTTP/1.0 500 Internal Server Error\r\n"), response)
        head, body = self.split(response)
        self.assertIn(b"Content-Type: application/json", head)
        self.assertEqual(body, self.INTERNAL)
        self.assertEqual(state.app.stats["http_errors"], self.errors + 1)
        self.assertEqual(self.log_error.call_args[0][0], "HTTP_Handler")
        self.assertEqual(web._http_connections, self.connections)

    def test_failure_after_the_header_appends_nothing(self):
        async def broken(writer, query, is_ap_request, headers):
            writer.write(b"HTTP/1.0 200 OK\r\n\r\n{")
            raise KeyError("x")

        with mock.patch.object(web, "_handle_ui_live_request", broken):
            response = self.http_request(b"GET /api/ui/live HTTP/1.0\r\n\r\n")
        self.assertEqual(response, b"HTTP/1.0 200 OK\r\n\r\n{")
        self.assertIn("ERROR", [call.args[0] for call in self.log.call_args_list])

    def test_failure_answer_survives_a_closed_client(self):
        written, writer = self.answer_failure(KeyError("x"), ResetWriter())
        self.assertEqual(len(writer.attempts), 1)
        from test_access_v11 import FakeHttpWriter

        class Reset(FakeHttpWriter):
            def write(self, data):
                raise OSError(104, "reset")

        with mock.patch.object(web, "access_info", side_effect=KeyError("hostname")):
            asyncio.run(web.handle_http_client(
                self.reader(b"GET /api/access HTTP/1.0\r\n\r\n"), Reset()))
        self.assertEqual(web._http_connections, self.connections)

    def test_failure_answer_survives_failing_diagnostics(self):
        cases = (("log", KeyError("x"), b"500 Internal Server Error", "internal_error"),
                 ("log_error", KeyError("x"), b"500 Internal Server Error", "internal_error"),
                 ("log", MemoryError(), b"503 Service Unavailable", "device_busy"),
                 ("log_error", MemoryError(), b"503 Service Unavailable", "device_busy"))
        for name, error, status, code in cases:
            with self.subTest(failing=name, error=type(error).__name__):
                target = web if name == "log" else web.app
                before = state.app.stats["http_errors"]
                with mock.patch.object(target, name, side_effect=MemoryError()):
                    written, _writer = self.answer_failure(error)
                self.assertTrue(written.startswith(b"HTTP/1.0 " + status), written[:60])
                self.assertEqual(self.split(written)[1]["error"], code)
                # The counter comes before the log call that fails.
                self.assertEqual(state.app.stats["http_errors"], before + 1)

        class FailingDrain(FakeWriter):
            async def drain(self):
                raise OSError(104, "reset")

        written, _writer = self.answer_failure(KeyError("x"), FailingDrain())
        self.assertIn(b"internal_error", written)
        with mock.patch.object(web, "_send_api_error", side_effect=MemoryError()):
            written, _writer = self.answer_failure(KeyError("x"))
        self.assertEqual(written, web._DEVICE_BUSY_ANSWER)

    def test_device_busy_answer_is_prebuilt(self):
        answer = web._DEVICE_BUSY_ANSWER
        self.assertIsInstance(answer, bytes)
        self.assertTrue(answer.startswith(b"HTTP/1.0 503 Service Unavailable\r\n"))
        head, body = self.split(answer)
        self.assertIn(b"Content-Security-Policy: ", head)
        self.assertIn(b"Cache-Control: no-store", head)
        self.assertEqual(body, {"error": "device_busy",
                                "message": "Not enough memory on the device; try again."})

        class Recorder(FakeWriter):
            def __init__(self):
                super().__init__()
                self.objects = []

            def write(self, data):
                self.objects.append(data)

        recorder = Recorder()
        self.answer_failure(MemoryError(), recorder)
        self.assertEqual(len(recorder.objects), 1)
        self.assertIs(recorder.objects[0], web._DEVICE_BUSY_ANSWER)

        class Unwritable(Recorder):
            # Refuses the prebuilt answer without counting as a begun answer.
            def write(self, data):
                self.objects.append(data)
                raise MemoryError()

        refusing = Unwritable()   # a plain writer: no started flag
        asyncio.run(web._answer_failure(refusing, MemoryError()))
        self.assertEqual(refusing.objects, [web._DEVICE_BUSY_ANSWER])

    def test_streaming_read_error_sends_exactly_one_status_line(self):
        with self.subTest("L1 static asset"):
            class FailingAsset:
                reads = 0

                def read(self, size):
                    self.reads += 1
                    if self.reads == 1:
                        return b"a" * 1024
                    raise OSError(5, "io")

                def close(self):
                    pass

            with mock.patch.object(web, "open", create=True, return_value=FailingAsset()):
                response = self.http_request(b"GET /app.js HTTP/1.0\r\n\r\n")
            self.assertEqual(response.count(b"HTTP/1.0 "), 1)
            self.assertTrue(response.startswith(b"HTTP/1.0 200 OK\r\n"), response[:60])
            self.assertNotIn(b"404", response)
            self.assertNotIn(b"Local asset missing", response)
            self.assertNotIn(b"internal_error", response)
            self.assertEqual(state.app.stats["http_errors"], self.errors + 1)

        with self.subTest("L1 failing header write, without the wrapper"):
            writer = ResetWriter()
            with self.assertRaises(OSError):
                asyncio.run(web._http_send_file(writer, "app.js", "text/javascript"))
            self.assertEqual(len(writer.attempts), 1)
            self.assertTrue(writer.attempts[0].startswith(b"HTTP/1.0 200 OK\r\n"))

        with self.subTest("L3 backup export"):
            session, _error = access.login("code", "code")

            def broken_backup(project_id=None):
                yield '{"schema_version": 5}\n'
                raise ValueError("broken")

            with mock.patch.object(web.tracker, "_loaded", True), \
                    mock.patch.object(web.tracker, "iter_backup_ndjson", broken_backup):
                response = self.http_request(
                    b"GET /api/tracking/export?format=backup HTTP/1.0\r\nCookie: rtk_session="
                    + session["token"].encode() + b"\r\n\r\n")
            self.assertEqual(response.count(b"HTTP/1.0 "), 1)
            self.assertTrue(response.startswith(b"HTTP/1.0 200 OK\r\n"), response[:60])
            self.assertNotIn(b"project_not_found", response)

        with self.subTest("L2 import answer"):
            folder = tempfile.mkdtemp(prefix="failure-import-")
            self.addCleanup(shutil.rmtree, folder, True)
            source = tracking.Tracker(os.path.join(folder, "source"))
            self.addCleanup(source.point_store.close)
            self.addCleanup(source._sync_journal, True)
            source.create_project("Import")
            raw = "".join(source.iter_backup_ndjson()).encode()
            target = tracking.Tracker(os.path.join(folder, "target"))
            self.addCleanup(target.point_store.close)
            self.addCleanup(target._sync_journal, True)
            writer = ResetWriter()
            with mock.patch.object(web, "tracker", target), \
                    mock.patch.object(web, "_admin_allowed", return_value=True), \
                    mock.patch.object(target, "storage_capacity",
                                      return_value={"writable": True}):
                with self.assertRaises(OSError):
                    asyncio.run(web._handle_tracking_import(
                        self.reader(b""), web._HttpAnswer(writer), raw, len(raw), False,
                        {"content-type": "application/x-ndjson"}))
            self.assertEqual(len(writer.attempts), 1)
            self.assertNotIn(b"restore_failed", b"".join(writer.attempts))

        with self.subTest("L4 delete answer"):
            writer = ResetWriter()
            refused = mock.AsyncMock(return_value=({"error": "delete_failed"}, None))
            self.log.reset_mock()
            with mock.patch.object(web, "_delete_project", refused), \
                    mock.patch.object(web, "_admin_allowed", return_value=True), \
                    mock.patch.object(web.tracker, "_loaded", True):
                with self.assertRaises(OSError):
                    asyncio.run(web._handle_tracking_request(
                        web._HttpAnswer(writer), "/api/tracking", "POST", "",
                        b'{"action": "delete_project", "project_id": "p"}', False, {}))
            self.assertEqual(len(writer.attempts), 1)
            self.assertNotIn("Could not persist tracking data",
                             " ".join(str(call.args) for call in self.log.call_args_list))

    def test_mapped_failures(self):
        stakeout_error = stakeout.StakeoutError("already_imported", "Stored.", "409 Conflict")
        cases = ((tracking.StorageBusy("storage_busy", "Busy."), b"409 Conflict", "storage_busy", 0),
                 (tracking.StorageBusy("survey_loading", "Loading."), b"503 Service Unavailable",
                  "survey_loading", 0),
                 (tracking.StorageReserveError("Flash reserve would fall below 20 percent."),
                  b"507 Insufficient Storage", "storage_reserve", 0),
                 (stakeout_error, b"409 Conflict", "already_imported", 0),
                 (MemoryError(), b"503 Service Unavailable", "device_busy", 1),
                 (OSError(5, "io"), b"500 Internal Server Error", "internal_error", 1))
        for error, status, code, counted in cases:
            with self.subTest(error=repr(error)):
                before = state.app.stats["http_errors"]
                written, _writer = self.answer_failure(error)
                self.assertTrue(written.startswith(b"HTTP/1.0 " + status), written[:60])
                body = self.split(written)[1]
                self.assertEqual(body["error"], code)
                if error is stakeout_error:
                    self.assertEqual(body, stakeout_error.payload())
                self.assertEqual(state.app.stats["http_errors"], before + counted)

    def test_timeouts_and_disconnects_are_not_errors(self):
        for error in (asyncio.TimeoutError(), OSError(104, "reset")):
            with self.subTest(error=repr(error)):
                self.log.reset_mock()
                written, _writer = self.answer_failure(error)
                self.assertEqual(written, b"")
                self.assertEqual(state.app.stats["http_errors"], self.errors)
                self.log_error.assert_not_called()
                self.assertEqual(self.log.call_args[0][0], "DEBUG")

    def test_malformed_request_line_is_400(self):
        malformed = (b"\r\n\r\n", b"GARBAGE\r\n\r\n", b"GET\r\n\r\n", b"GET \r\n\r\n",
                     b"GET  HTTP/1.1\r\n\r\n", b" /status HTTP/1.1\r\n\r\n",
                     b"GET /status\r\n\r\n", b"GET status HTTP/1.1\r\n\r\n",
                     b"get /status HTTP/1.1\r\n\r\n", b"GET /status HTTP/2.0\r\n\r\n",
                     b"GET /status HTTP/1.1 x\r\n\r\n", b"G\xc3\xa9T /status HTTP/1.1\r\n\r\n")
        for raw in malformed:
            with self.subTest(raw=raw):
                rejected = state.app.stats.get("http_rejected_requests", 0)
                response = self.http_request(raw)
                self.assertTrue(response.startswith(b"HTTP/1.0 400 Bad Request"), response[:60])
                self.assertEqual(state.app.stats["http_rejected_requests"], rejected + 1)
                self.assertEqual(asyncio.run(web._read_request(self.reader(raw)))[2],
                                 "request_line_invalid")
        for raw in (b"GET /status HTTP/1.1\r\n\r\n", b"HEAD /status HTTP/1.0\r\n\r\n"):
            with self.subTest(raw=raw):
                self.assertIsNone(asyncio.run(web._read_request(self.reader(raw)))[2])
                self.assertTrue(self.http_request(raw).startswith(b"HTTP/1.0 401"))


class TestParallelBenchmarkApi(unittest.TestCase):
    def test_authentication_is_required_for_read_and_mutation(self):
        import parallel_benchmark
        for method in ("GET", "POST", "DELETE"):
            writer = FakeWriter()
            with mock.patch.object(web, "_admin_allowed", return_value=False), \
                    mock.patch.object(parallel_benchmark, "control") as control:
                asyncio.run(web._handle_parallel_benchmark_request(
                    writer, method, b'{}', False, {}))
            self.assertIn(b"401 Unauthorized", writer.buf)
            control.assert_not_called()

    def test_start_checks_csrf_and_schedules_restart_after_success(self):
        import parallel_benchmark
        writer = FakeWriter()
        headers = {"cookie": "synthetic", "x-csrf-token": "synthetic"}
        body = b'{"action":"start","target_points":10000}'
        def discard(coro): coro.close()
        with mock.patch.object(web, "_admin_allowed", return_value=True) as allowed, \
                mock.patch.object(web.tracker, "_loaded", True), \
                mock.patch.object(parallel_benchmark, "control", return_value={
                    "state": "armed", "reboot_required": True}) as control, \
                mock.patch.object(web.asyncio, "create_task", side_effect=discard) as task:
            asyncio.run(web._handle_parallel_benchmark_request(writer, "POST", body, False, headers))
        allowed.assert_called_once_with(False, headers, "POST", "", body)
        control.assert_called_once_with("start", web.tracker, 10000, None, profile="parallel")
        task.assert_called_once()
        self.assertIn(b"200 OK", writer.buf)

    def test_refusal_does_not_restart(self):
        import parallel_benchmark
        writer = FakeWriter()
        with mock.patch.object(web, "_admin_allowed", return_value=True), \
                mock.patch.object(web.tracker, "_loaded", True), \
                mock.patch.object(parallel_benchmark, "control", side_effect=ValueError("Active line")), \
                mock.patch.object(web.asyncio, "create_task") as task:
            asyncio.run(web._handle_parallel_benchmark_request(
                writer, "POST", b'{"action":"start","target_points":10000}', False, {}))
        self.assertIn(b"409 Conflict", writer.buf)
        task.assert_not_called()

    def test_reserve_refusal_is_507(self):
        import parallel_benchmark
        writer = FakeWriter()
        message = "Insufficient flash reserve for the benchmark."
        with mock.patch.object(web, "_admin_allowed", return_value=True), \
                mock.patch.object(web.tracker, "_loaded", True), \
                mock.patch.object(parallel_benchmark, "control",
                                  side_effect=tracking.StorageReserveError(message)), \
                mock.patch.object(web.asyncio, "create_task") as task:
            asyncio.run(web._handle_parallel_benchmark_request(
                writer, "POST", b'{"action":"start","target_points":10000}', False, {}))
        header, body = bytes(writer.buf).split(b"\r\n\r\n", 1)
        self.assertTrue(header.startswith(b"HTTP/1.0 507 Insufficient Storage"), header)
        self.assertEqual(json.loads(body), {"error": "storage_reserve", "message": message})
        task.assert_not_called()

    def test_production_writes_are_blocked_during_benchmark(self):
        writer = FakeWriter()
        with mock.patch.object(web, "_admin_allowed", return_value=True), \
                mock.patch.dict(web._instances, {"parallel_tracker": object()}):
            asyncio.run(web._handle_tracking_request(
                writer, "/api/tracking", "POST", "", b'{"action":"delete_project"}', False, {}))
        self.assertIn(b"409 Conflict", writer.buf)
        self.assertIn(b"benchmark_active", writer.buf)


_real_open = builtins.open
_real_remove = os.remove


class _RecordingWriter:
    """Records writes and drains; the write attempt number fail_at raises error."""

    def __init__(self, fail_at=None, error=None):
        self.writes = []
        self.attempts = 0
        self.drains = 0
        self.fail_at = fail_at
        self.error = error

    def write(self, data):
        self.attempts += 1
        if self.attempts == self.fail_at:
            raise self.error
        self.writes.append(bytes(data))

    async def drain(self):
        self.drains += 1

    @property
    def data(self):
        return b"".join(self.writes)


class StakeoutHttpFixture:
    """A device session, a tracker and a stakeout store behind the real dispatcher.

    A mixin without tests: TestStakeoutHttp and the stakeout session tests add
    unittest.TestCase after it.
    """

    HOST = "rtk-d4e5f6.local"
    ORIGIN = "http://rtk-d4e5f6.local"
    SET_ID = "so-3a4739b9"

    def setUp(self):
        from test_access_v11 import http_request
        self.http_request = http_request
        self.folder = tempfile.mkdtemp(prefix="stakeout-http-")
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.tracker = tracking.Tracker(os.path.join(self.folder, "survey"))
        self.addCleanup(self.tracker.point_store.close)
        self.addCleanup(self.tracker._sync_journal, True)
        self.pid = self.tracker.create_project("Stakeout")["id"]
        self.root = os.path.join(self.folder, "stakeout")
        self.store = stakeout.TargetStore(self.root)
        # Each test has its own progress log over its own store (stakeout WP4).
        self.progress = stakeout_progress.ProgressLog(self.store)
        for patch in (mock.patch.object(web, "tracker", self.tracker),
                      mock.patch.object(web.stakeout, "store", self.store),
                      mock.patch.object(stakeout_progress, "progress", self.progress),
                      mock.patch.object(web, "log"),
                      mock.patch.object(stakeout, "log"),
                      mock.patch.object(stakeout_progress, "log")):
            patch.start()
            self.addCleanup(patch.stop)
        access.reset()
        self.addCleanup(access.reset)
        self.session, error = access.login("ABCDEF123456", "ABCDEF123456",
                                           "203.0.113.9")
        self.assertIsNone(error)

    # --- helpers ---------------------------------------------------------

    def raw_request(self, method, path, body=b"", content_type="application/json",
                    csrf=True, origin=ORIGIN, cookie=True, length=None):
        """The bytes of one request with the fixture's real session."""
        lines = ["%s %s HTTP/1.0" % (method, path), "Host: " + self.HOST]
        if origin:
            lines.append("Origin: " + origin)
        if cookie:
            lines.append("Cookie: rtk_session=" + self.session["token"])
        if csrf:
            lines.append("X-CSRF-Token: " +
                         (csrf if isinstance(csrf, str) else self.session["csrf"]))
        if method in ("POST", "PUT"):
            lines.append("Content-Type: " + content_type)
            lines.append("Content-Length: %d" % (len(body) if length is None else length))
        return ("\r\n".join(lines) + "\r\n\r\n").encode() + body

    def request(self, method, path, body=b"", **kwargs):
        """The raw response of one request with the fixture's real session."""
        return self.http_request(self.raw_request(method, path, body, **kwargs))

    def call(self, method, path, body=b"", **kwargs):
        """(status, headers, body bytes) of one request."""
        raw = self.request(method, path, body, **kwargs)
        head, _, payload = raw.partition(b"\r\n\r\n")
        status = int(head.split(b" ", 2)[1]) if head else None
        return status, head, payload

    def api(self, method, path, body=b"", **kwargs):
        """(status, parsed JSON body) of one request."""
        status, _head, payload = self.call(method, path, body, **kwargs)
        return status, json.loads(payload)

    def package(self, pkg=None):
        pkg = copy.deepcopy(pkg or sample_package(self.pid))
        pkg["project_id"] = self.pid
        return pkg

    def import_(self, pkg=None, **kwargs):
        body = pkg if isinstance(pkg, bytes) else compact(self.package(pkg))
        return self.api("POST", "/api/stakeout/import", body, **kwargs)

    def overview(self, project_id=None):
        query = "" if project_id is None else "?project_id=" + project_id
        return self.api("GET", "/api/stakeout" + query)

    def read_set(self, set_id=SET_ID):
        return self.call("GET", "/api/stakeout?project_id=%s&set_id=%s"
                         % (self.pid, set_id))

    def delete(self, set_id=SET_ID, **fields):
        action = {"action": "delete_set", "set_id": set_id, "expected_confirmed": 0}
        action.update(fields)
        return self.api("POST", "/api/stakeout", json.dumps(action).encode())

    def path(self, set_id=SET_ID):
        return os.path.join(self.root, self.pid, set_id + ".json")

    def files(self):
        """The files of the stakeout store, as <project_id>/<name>."""
        found = []
        for folder, _dirs, names in os.walk(self.root):
            found += [os.path.relpath(os.path.join(folder, name), self.root)
                      for name in names]
        return sorted(found)

    def name(self, set_id=SET_ID):
        return "%s/%s.json" % (self.pid, set_id)

    def file_bytes(self, path):
        with _real_open(path, "rb") as source:
            return source.read()

    def handle(self, writer, method, path, query="", body=b""):
        """Run the stakeout handler directly on writer with the fixture's session."""
        headers = {"host": self.HOST, "origin": self.ORIGIN,
                   "cookie": "rtk_session=" + self.session["token"],
                   "x-csrf-token": self.session["csrf"],
                   "content-type": "application/json"}
        asyncio.run(web._handle_stakeout_request(writer, path, method, query, body,
                                                 False, headers))
        return writer

    def cold(self):
        """A new store over the same files: the next access lists them again."""
        self.use_store(stakeout.TargetStore(self.root))

    def use_store(self, store):
        """Make store the stakeout store, with a new progress log over it (a restart)."""
        self.store = web.stakeout.store = store
        self.progress = stakeout_progress.progress = stakeout_progress.ProgressLog(store)

    def assert_error(self, response, status, code, **fields):
        self.assertEqual(response[0], status, response)
        self.assertEqual(response[1]["error"], code, response)
        self.assertIsInstance(response[1]["message"], str)
        for key, value in fields.items():
            self.assertEqual(response[1].get(key), value, response)

    def assert_nothing_written(self):
        self.assertEqual(self.files(), [])
        self.assertIs(self.store.busy, False)

    def two_sets(self):
        """The sample set and a second set; returns the second set's id."""
        self.assertEqual(self.import_()[0], 200)
        status, answer = self.import_(make_package(targets=3, project_id=self.pid))
        self.assertEqual(status, 200, answer)
        return answer["set_id"]

    def states(self):
        status, answer = self.overview()
        self.assertEqual(status, 200, answer)
        return dict((item["set_id"], item["state"]) for item in answer["sets"])


class TestStakeoutHttp(StakeoutHttpFixture, unittest.TestCase):
    """The stakeout endpoints through the real dispatcher, sessions and store."""

    # --- routes and dispatcher -------------------------------------------

    def test_route_policies(self):
        self.assertEqual(web_routing.ROUTES["/api/stakeout"],
                         (("GET", "POST"), "GET or POST required", 4096))
        self.assertEqual(web_routing.ROUTES["/api/stakeout/import"],
                         (("POST",), "POST required", 131072))
        self.assertEqual(web_routing.STAKEOUT_IMPORT_LIMIT, 131072)
        # The transport and the direct store entry share one bound.
        self.assertEqual(web_routing.STAKEOUT_IMPORT_LIMIT, stakeout.MAX_IMPORT_BYTES)
        self.assertEqual(self.call("HEAD", "/api/stakeout")[0], 405)
        self.assertEqual(self.call("GET", "/api/stakeout/import")[0], 405)

    def test_dispatcher_has_one_stakeout_entry(self):
        source = inspect.getsource(web.handle_http_client)
        self.assertEqual(source.count("_handle_stakeout_request"), 1)
        self.assertLess(len(source.splitlines()), 300)
        self.assertTrue(inspect.iscoroutinefunction(web._handle_stakeout_request))

    # --- import, overview, set file --------------------------------------

    def test_import_overview_and_set_read(self):
        status, answer = self.import_()
        self.assertEqual(status, 200, answer)
        self.assertEqual(answer["set_id"], self.SET_ID)
        self.assertEqual(answer["project_id"], self.pid)
        self.assertEqual(answer["targets"], 12)
        self.assertEqual(set(answer["timing_ms"]), set(stakeout.TIMING_KEYS))
        status, overview = self.overview(self.pid)
        self.assertEqual(status, 200, overview)
        self.assertEqual(overview["schema"], 1)
        self.assertEqual(overview["project_id"], self.pid)
        self.assertEqual(len(overview["sets"]), 1)
        self.assertEqual(overview["sets"][0]["set_id"], self.SET_ID)
        self.assertEqual(overview["sets"][0]["state"], "ok")
        self.assertEqual(overview["sets"][0]["targets"], 12)
        self.assertEqual(overview["totals"], {"sets": 1, "targets": 12, "orphaned": 0})
        self.assertEqual(overview["limits"], {"sets": 4, "targets_per_set": 500,
                                              "coords_per_set": 2000,
                                              "import_bytes": 131072,
                                              "set_file_bytes": 393216})
        self.assertIs(type(overview["load_ms"]), int)
        self.assertIs(type(overview["load_block_ms"]), int)
        status, active = self.overview()
        self.assertEqual(status, 200, active)
        overview.pop("load_ms")
        overview.pop("load_block_ms")
        # Served from the cache.
        self.assertEqual((active.pop("load_ms"), active.pop("load_block_ms")), (0, 0))
        self.assertEqual(active, overview)
        status, head, data = self.read_set()
        self.assertEqual(status, 200)
        self.assertEqual(data, self.file_bytes(self.path()))
        self.assertIn(b"Content-Type: application/json", head)
        self.assertIn(b"Cache-Control: no-store", head)

    def test_set_file_is_streamed_in_small_pieces(self):
        self.assertEqual(self.import_(make_package(targets=64, project_id=self.pid))[0], 200)
        set_id = self.overview()[1]["sets"][0]["set_id"]
        stored = self.file_bytes(self.path(set_id))
        self.assertGreater(len(stored), 4096)
        writer = self.handle(_RecordingWriter(), "GET", "/api/stakeout",
                             "project_id=%s&set_id=%s" % (self.pid, set_id))
        head, body = writer.writes[0], writer.writes[1:]
        self.assertTrue(head.startswith(b"HTTP/1.0 200 OK\r\n"), head)
        self.assertTrue(head.endswith(b"\r\n\r\n"), head)
        self.assertIn(b"Content-Type: application/json\r\n", head)
        self.assertIn(b"Cache-Control: no-store\r\n", head)
        self.assertIn(b"Content-Length: %d\r\n" % len(stored), head)
        self.assertEqual(b"".join(body), stored)
        self.assertLessEqual(max(len(piece) for piece in body), 1024)
        self.assertGreaterEqual(writer.drains, len(body))

    def test_broken_set_answer_gets_no_second_status_line(self):
        self.assertEqual(self.import_(make_package(targets=64, project_id=self.pid))[0], 200)
        set_id = self.overview()[1]["sets"][0]["set_id"]
        for error in (OSError(104, "reset"), MemoryError()):
            for fail_at in (1, 3):
                with self.subTest(error=error, fail_at=fail_at):
                    web.log.reset_mock()
                    writer = self.handle(_RecordingWriter(fail_at, error), "GET",
                                         "/api/stakeout",
                                         "project_id=%s&set_id=%s" % (self.pid, set_id))
                    self.assertLessEqual(writer.data.count(b"HTTP/1.0 "), 1)
                    self.assertNotIn(b"stakeout_read_failed", writer.data)
                    self.assertNotIn(b"stakeout_busy", writer.data)
                    self.assertEqual(writer.attempts, fail_at)
                    self.assertEqual(web.log.call_args[0][0], "DEBUG")
        self.assertEqual(self.states(), {set_id: "ok"})

    def test_broken_import_answer_gets_no_second_status_line(self):
        body = compact(self.package())
        for error in (OSError(104, "reset"), MemoryError()):
            with self.subTest(error=error):
                web.log.reset_mock()
                # Write 1 is the header of the 200, write 2 its body.
                writer = self.handle(_RecordingWriter(2, error), "POST",
                                     "/api/stakeout/import", body=body)
                self.assertEqual(writer.data.count(b"HTTP/1.0 "), 1)
                self.assertTrue(writer.data.startswith(b"HTTP/1.0 200 OK"))
                self.assertNotIn(b"import_committed_response_failed", writer.data)
                self.assertEqual(web.log.call_args[0][0], "DEBUG")
                self.assertNotIn(self.pid, self.store._cache)
                self.assertTrue(os.path.exists(self.path()))
                self.assert_error(self.import_(), 409, "already_imported",
                                  set_id=self.SET_ID)
                self.assertEqual(self.delete(),
                                 (200, {"deleted": self.SET_ID, "cleanup": "done"}))

    def test_cold_overview_reports_its_load_time_and_longest_block(self):
        self.import_()
        self.cold()
        real_trailer = stakeout._read_trailer

        def slow_trailer(*args):
            time.sleep(0.03)
            return real_trailer(*args)

        with mock.patch.object(stakeout, "_read_trailer", slow_trailer):
            status, overview = self.overview()
        self.assertEqual(status, 200, overview)
        self.assertGreaterEqual(overview["load_ms"], 25)
        self.assertGreaterEqual(overview["load_block_ms"], 25)
        self.assertLessEqual(overview["load_block_ms"], overview["load_ms"])
        self.assertEqual((overview["load_ms"], overview["load_block_ms"]),
                         (self.store.last_load_ms, self.store.last_load_block_ms))
        self.assertEqual((self.overview()[1]["load_ms"], self.overview()[1]["load_block_ms"]),
                         (0, 0))

    def test_reimport_answers_already_imported(self):
        self.assertEqual(self.import_()[0], 200)
        before = self.file_bytes(self.path())
        self.assert_error(self.import_(), 409, "already_imported", set_id=self.SET_ID)
        self.assertEqual(self.file_bytes(self.path()), before)

    def test_import_errors_are_stable(self):
        with self.subTest("content type"):
            self.assert_error(self.import_(content_type="text/plain"), 415, "json_required")
            self.assert_nothing_written()
        with self.subTest("not JSON"):
            self.assert_error(self.import_(b"{"), 400, "invalid_stakeout_import")
            self.assert_nothing_written()
        with self.subTest("coordinate"):
            pkg = self.package()
            pkg["targets"][4]["lat_e8"] = 9000000001
            self.assert_error(self.import_(pkg), 400, "coordinate_out_of_range",
                              path="targets[4].lat_e8")
            self.assert_nothing_written()
        with self.subTest("unknown project"):
            body = compact(sample_package("project-ffffffff-ffff"))
            self.assert_error(self.import_(body), 404, "project_not_found")
            self.assert_nothing_written()
        with self.subTest("declared length over the limit"):
            status, _head, payload = self.call("POST", "/api/stakeout/import",
                                               compact(self.package()), length=131073)
            self.assertEqual(status, 413)
            self.assertEqual(payload, b"Request body is too large.")
            self.assert_nothing_written()

    def padded(self, pkg, length):
        """The compact package plus JSON whitespace up to exactly length bytes."""
        body = compact(self.package(pkg))
        return body + b" " * (length - len(body))

    def test_body_of_exactly_the_limit_is_imported(self):
        body = self.padded(P128, web_routing.STAKEOUT_IMPORT_LIMIT)
        self.assertEqual(len(body), 131072)
        status, answer = self.import_(body)
        self.assertEqual(status, 200, answer)
        self.assertEqual(answer["targets"], 500)

    def test_body_over_the_limit_is_413_without_parsing(self):
        bodies = (("one byte over", self.padded(P128, 131073)),
                  ("P256", compact(self.package(P256))),
                  ("wide", compact(self.package(wide_package(0, 4)))),
                  ("wider", compact(self.package(wide_package(80, 3)))))
        for label, body in bodies:
            with self.subTest(label), \
                    mock.patch.object(stakeout.ujson, "loads") as parse, \
                    mock.patch.object(stakeout.gc, "collect") as collect, \
                    mock.patch.object(web.stakeout.store, "import_package") as entry:
                status, head, payload = self.call("POST", "/api/stakeout/import", body)
                self.assertEqual(status, 413)
                self.assertEqual(payload, b"Request body is too large.")
                for untouched in (parse, collect, entry):
                    self.assertFalse(untouched.called)
                self.assert_nothing_written()

    def test_import_through_the_route_records_the_timed_collection(self):
        def slow_collect():
            time.sleep(0.06)

        with mock.patch.object(stakeout.gc, "collect", slow_collect):
            status, answer = self.import_()
        self.assertEqual(status, 200, answer)
        timing = answer["timing_ms"]
        self.assertGreaterEqual(timing["gc"], 60)
        # The collection is local work: part of the longest block, never more.
        self.assertGreaterEqual(timing["longest_block"], timing["gc"])

    # --- the event loop stays free during an import ----------------------

    def serve(self, raw, reader=None):
        """Task running one connection through the real handler; (task, writer)."""
        from test_access_v11 import FakeHttpWriter, FakeReader
        writer = FakeHttpWriter()
        return asyncio.ensure_future(
            web.handle_http_client(reader or FakeReader(raw), writer)), writer

    def run_suspended_import(self, body, during, expected_status=b" 200 "):
        """Run an import, hold it at its first service pause and run during()
        meanwhile (a coroutine function of the event loop's tasks)."""
        base = web._http_connections
        released = asyncio.Event()
        suspended = asyncio.Event()
        real_pause = stakeout._pause
        gated = []

        async def pause(meter):
            if not gated:
                gated.append(True)
                suspended.set()
                await released.wait()
            await real_pause(meter)

        async def scenario():
            task, writer = self.serve(self.raw_request("POST", "/api/stakeout/import",
                                                       body))
            await asyncio.wait_for(suspended.wait(), 5)
            self.assertEqual(web._http_connections, base + 1)
            self.assertIs(self.store.busy, True)
            try:
                await during()
            finally:
                released.set()
            await asyncio.wait_for(task, 10)
            self.assertIn(expected_status, bytes(writer.data).split(b"\r\n", 1)[0] + b" ")
            return bytes(writer.data)

        with mock.patch.object(stakeout, "_pause", pause):
            data = asyncio.run(scenario())
        self.assertEqual(web._http_connections, base)
        self.assertIs(self.store.busy, False)
        return data

    def test_live_handler_completes_while_an_import_is_suspended(self):
        answered = []

        async def during():
            task, writer = self.serve(self.raw_request("GET", "/api/ui/live"))
            await asyncio.wait_for(task, 5)
            answered.append(bytes(writer.data))

        data = self.run_suspended_import(compact(self.package()), during)
        self.assertEqual(len(answered), 1)
        self.assertTrue(answered[0].startswith(b"HTTP/1.0 200"), answered[0][:80])
        self.assertIn(b'"schema_version"', answered[0])
        self.assertIn(self.SET_ID.encode(), data)
        self.assertEqual(self.states(), {self.SET_ID: "ok"})

    def test_seventh_connection_is_refused_while_an_import_holds_six_open(self):
        from test_access_v11 import FakeReader

        class Silent(FakeReader):
            async def read(self, size=-1):
                await asyncio.Event().wait()

        refused = []

        async def during():
            idle = [self.serve(b"", Silent(b""))[0] for _ in range(web.HTTP_MAX_CONNECTIONS - 1)]
            await asyncio.sleep(0)
            self.assertEqual(web._http_connections, web.HTTP_MAX_CONNECTIONS)
            task, writer = self.serve(self.raw_request("GET", "/api/ui/live"))
            await asyncio.wait_for(task, 5)
            refused.append(bytes(writer.data))
            for waiting in idle:
                waiting.cancel()
            await asyncio.gather(*idle, return_exceptions=True)

        self.run_suspended_import(compact(self.package()), during)
        self.assertTrue(refused[0].startswith(b"HTTP/1.0 503"), refused[0][:80])
        head, _sep, payload = refused[0].partition(b"\r\n\r\n")
        self.assertIn(b"Retry-After: 2", head)
        self.assertIn(b"Content-Length: 0", head)
        self.assertEqual(payload, b"")

    def test_connection_permit_is_released_when_the_import_fails(self):
        base = web._http_connections
        out_of_range = self.package()
        out_of_range["targets"][4]["lat_e8"] = 9000000001
        failures = (("invalid package", b"{", b" 400 "),
                    ("failed validation", compact(out_of_range), b" 400 "),
                    ("rejected project", compact(sample_package("project-ffffffff-ffff")),
                     b" 404 "))
        for label, body, status in failures:
            with self.subTest(label):
                async def idle():
                    return None
                data = self.run_suspended_import(body, idle, expected_status=status)
                self.assertEqual(web._http_connections, base)
                self.assertIn(status.strip(), data.split(b"\r\n", 1)[0])
        with self.subTest("store out of memory"):
            with mock.patch.object(stakeout.TargetStore, "add_set",
                                   side_effect=MemoryError):
                status, _ = self.import_()
            self.assertEqual(status, 503)
            self.assertEqual(web._http_connections, base)

    def test_response_failure_after_commit_is_500_with_set_id(self):
        real = web._send_json
        calls = []

        def answer_runs_out_of_memory(*args, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                raise MemoryError
            return real(*args, **kwargs)

        with mock.patch.object(web, "_send_json", side_effect=answer_runs_out_of_memory):
            response = self.import_()
        self.assert_error(response, 500, "import_committed_response_failed",
                          set_id=self.SET_ID)
        self.assertNotIn(self.pid, self.store._cache)
        self.assertTrue(os.path.exists(self.path()))
        self.assertEqual(self.states(), {self.SET_ID: "ok"})
        self.assert_error(self.import_(), 409, "already_imported", set_id=self.SET_ID)

    def test_os_error_while_answering_after_commit_is_500_with_set_id(self):
        real = web._send_json
        calls = []

        def answer_fails(*args, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                raise OSError(104, "reset")
            return real(*args, **kwargs)

        with mock.patch.object(web, "_send_json", side_effect=answer_fails):
            response = self.import_()
        self.assert_error(response, 500, "import_committed_response_failed",
                          set_id=self.SET_ID)
        self.assertEqual(self.states(), {self.SET_ID: "ok"})

    def test_listing_errors_get_a_stable_response(self):
        second = self.two_sets()

        def reading_set_files(path, mode="r", *args, **kwargs):
            if "r" in mode and str(path).startswith(self.root):
                raise OSError(5, "io")
            return _real_open(path, mode, *args, **kwargs)

        real_load = stakeout.TargetStore._load

        async def always_stale(store, project_id, meter):
            entries = await real_load(store, project_id, meter)
            store._changed(project_id)
            return entries

        cases = [
            ("open", mock.patch("builtins.open", reading_set_files),
             507, "stakeout_read_failed"),
            ("listing", mock.patch.object(stakeout.os, "ilistdir",
                                          side_effect=OSError(5, "io")),
             507, "stakeout_read_failed"),
            ("trailer out of memory",
             mock.patch.object(stakeout.ujson, "loads", side_effect=MemoryError),
             503, "stakeout_busy"),
            ("changed while listing twice",
             mock.patch.object(stakeout.TargetStore, "_load", always_stale),
             503, "stakeout_busy"),
        ]
        before = self.snapshot()
        for label, patch, status, code in cases:
            with self.subTest(label):
                self.cold()
                with patch:
                    self.assert_error(self.overview(), status, code)
                    self.assertEqual(self.read_set()[0], status)
                self.assertNotIn(self.pid, self.store._cache)
                self.assertEqual(self.states(), {self.SET_ID: "ok", second: "ok"})
        self.assertEqual(self.snapshot(), before)

    def snapshot(self):
        return {name: self.file_bytes(os.path.join(self.root, name))
                for name in self.files()}

    def test_get_during_recording_or_maintenance_lists_without_changes(self):
        second = self.two_sets()
        os.rename(self.path(second), self.path(second) + ".tmp")
        with _real_open(self.path("so-00000001"), "wb") as target:
            target.write(b"{}")
        before = self.snapshot()
        tracker = self.tracker

        @contextlib.contextmanager
        def recording():
            tracker.start_line("L", .2)
            try:
                yield
            finally:
                tracker.set_line_state("pause")

        expected = {self.SET_ID: "ok", second: "incomplete", "so-00000001": "damaged"}
        conditions = [
            ("recording", recording),
            ("maintenance", lambda: mock.patch.object(tracker, "maintenance_active",
                                                      return_value=True)),
            ("measurement", lambda: mock.patch.object(web.measurement_session, "active",
                                                      return_value=True)),
            ("idle", contextlib.nullcontext),
        ]
        for label, condition in conditions:
            with self.subTest(label):
                self.cold()
                with condition():
                    self.assertEqual(self.states(), expected)
                    status, _head, payload = self.read_set(second)
                    self.assertEqual(status, 409)
                    self.assertEqual(json.loads(payload)["error"], "set_damaged")
                    self.assertEqual(self.read_set()[0], 200)
                self.assertEqual(self.snapshot(), before)
        # The recovery_pending state is gone from the store (the project
        # block's stakeout_recovery_pending is another code).
        self.assertNotRegex(inspect.getsource(stakeout), r"(?<!stakeout_)recovery_pending")

    def test_a_download_with_a_changed_interior_byte_withholds_the_trailer(self):
        self.assertEqual(self.import_(make_package(targets=64, project_id=self.pid))[0], 200)
        set_id = self.overview()[1]["sets"][0]["set_id"]
        stored = bytearray(self.file_bytes(self.path(set_id)))
        body_bytes = json.loads(bytes(stored))["summary"]["body_bytes"]
        stored[body_bytes // 2] ^= 1
        with _real_open(self.path(set_id), "wb") as target:
            target.write(bytes(stored))
        stakeout.log.reset_mock()
        status, head, data = self.read_set(set_id)
        self.assertEqual(status, 200)
        self.assertEqual(head.count(b"HTTP/1.0 "), 1)
        self.assertIn(b"Content-Length: %d\r\n" % len(stored), head + b"\r\n")
        # Closed after the body: the short answer fails for every client.
        self.assertEqual(data, bytes(stored[:body_bytes]))
        self.assertNotIn(b"HTTP/1.0", data)
        self.assertEqual(stakeout.log.call_args[0][0], "ERROR")
        self.assertEqual(self.states(), {set_id: "damaged"})
        self.assert_error(self.api("GET", "/api/stakeout?project_id=%s&set_id=%s"
                                   % (self.pid, set_id)), 409, "set_damaged", set_id=set_id)
        self.assertEqual(self.store.readers(self.pid), 0)
        self.assertEqual(self.delete(set_id), (200, {"deleted": set_id, "cleanup": "done"}))

    def test_a_delete_while_a_download_is_open_is_busy(self):
        self.assertEqual(self.import_(make_package(targets=64, project_id=self.pid))[0], 200)
        set_id = self.overview()[1]["sets"][0]["set_id"]
        answers = []

        class Held(_RecordingWriter):
            async def drain(self):
                # While the download is open (after its first piece).
                if not answers:
                    answers.append(await _delete_now())
                await super().drain()

        async def _delete_now():
            writer = _RecordingWriter()
            body = json.dumps({"action": "delete_set", "set_id": set_id,
                               "expected_confirmed": 0}).encode()
            await web._handle_stakeout_request(
                writer, "/api/stakeout", "POST", "", body, False,
                {"host": self.HOST, "origin": self.ORIGIN,
                 "cookie": "rtk_session=" + self.session["token"],
                 "x-csrf-token": self.session["csrf"],
                 "content-type": "application/json"})
            return writer.data

        writer = self.handle(Held(), "GET", "/api/stakeout",
                             "project_id=%s&set_id=%s" % (self.pid, set_id))
        self.assertIn(b" 409 ", answers[0].split(b"\r\n", 1)[0] + b" ")
        self.assertIn(b"storage_busy", answers[0])
        self.assertIn(b"download", answers[0])
        self.assertEqual(b"".join(writer.writes[1:]), self.file_bytes(self.path(set_id)))
        self.assertEqual(self.store.readers(self.pid), 0)
        self.assertEqual(self.delete(set_id), (200, {"deleted": set_id, "cleanup": "done"}))

    def test_a_stalled_download_ends_without_a_second_status_line(self):
        self.assertEqual(self.import_()[0], 200)
        # CPython's TimeoutError is an OSError, MicroPython's is not.
        device_timeout = type("TimeoutError", (Exception,), {})
        for timeout in (asyncio.TimeoutError, device_timeout):
            calls = []

            async def wait_for(awaitable, seconds, timeout=timeout):
                calls.append(seconds)
                awaitable.close()
                raise timeout()

            with self.subTest(timeout=timeout), \
                    mock.patch.object(web.asyncio, "TimeoutError", timeout), \
                    mock.patch.object(stakeout.asyncio, "wait_for", wait_for):
                writer = self.handle(_RecordingWriter(), "GET", "/api/stakeout",
                                     "project_id=%s&set_id=%s" % (self.pid, self.SET_ID))
                self.assertEqual(calls, [10])
                # The head and the first piece; no error answer after them.
                self.assertEqual(len(writer.writes), 2)
                self.assertEqual(writer.data.count(b"HTTP/1."), 1)
                self.assertEqual(self.store.readers(self.pid), 0)
        self.assertEqual(self.delete(), (200, {"deleted": self.SET_ID, "cleanup": "done"}))

    def test_body_read_memory_error_answers_device_busy_and_writes_nothing(self):
        # #19a E6: the dispatcher answers a MemoryError before the answer began.
        with mock.patch.object(web, "_read_body", side_effect=MemoryError), \
                mock.patch.object(web.app, "log_error"):
            raw = self.request("POST", "/api/stakeout/import", compact(self.package()))
        self.assertEqual(raw, web._DEVICE_BUSY_ANSWER)
        self.assert_nothing_written()

    # --- refusals before writing ------------------------------------------

    def test_import_refused_while_recording_or_measuring(self):
        self.tracker.start_line("L", .2)
        self.assertEqual(self.tracker.active_line()["state"], "recording")
        self.assert_error(self.import_(), 409, "recording_active")
        self.assert_nothing_written()
        self.tracker.set_line_state("pause")
        self.assertEqual(self.import_()[0], 200)
        with mock.patch.object(web.measurement_session, "active", return_value=True):
            self.assert_error(self.import_(make_package(targets=3, project_id=self.pid)),
                              409, "recording_active")
            self.assert_error(self.delete(), 409, "recording_active")
        self.assertEqual(self.files(), [self.name()])

    def test_import_and_delete_refused_while_a_parallel_benchmark_runs(self):
        self.assertEqual(self.import_()[0], 200)
        conditions = (("tracker", mock.patch.dict(web._instances,
                                                   {"parallel_tracker": object()})),
                      ("armed", mock.patch.dict(web.app.stats,
                                                {"parallel_benchmark_state": "armed"})),
                      ("stopping", mock.patch.dict(web.app.stats,
                                                   {"parallel_benchmark_state": "stopping"})))
        for label, condition in conditions:
            with self.subTest(label), condition:
                self.assert_error(self.import_(make_package(targets=3, project_id=self.pid)),
                                  409, "benchmark_active")
                self.assert_error(self.delete(), 409, "benchmark_active")
                self.assertEqual(self.overview()[0], 200)
                self.assertEqual(self.files(), [self.name()])

    def test_benchmark_armed_during_validation_writes_nothing(self):
        stats = web.app.stats
        self.addCleanup(stats.pop, "parallel_benchmark_state", None)

        async def pause(meter):
            stats["parallel_benchmark_state"] = "armed"

        with mock.patch.object(stakeout, "_pause", pause):
            response = self.import_(make_package(targets=64, project_id=self.pid))
        self.assert_error(response, 409, "benchmark_active")
        self.assert_nothing_written()

    def test_recording_started_during_validation_writes_nothing(self):
        tracker = self.tracker
        started = []

        async def pause(meter):
            if not started:
                started.append(tracker.start_line("L", .2))

        with mock.patch.object(stakeout, "_pause", pause):
            response = self.import_(make_package(targets=64, project_id=self.pid))
        self.assertTrue(started)
        self.assert_error(response, 409, "recording_active")
        self.assert_nothing_written()

    def test_import_refused_while_storage_is_busy(self):
        with mock.patch.object(self.tracker, "maintenance_active", return_value=True):
            self.assert_error(self.import_(), 409, "storage_busy")
        self.assert_nothing_written()
        self.store.busy = True
        try:
            self.assert_error(self.import_(), 409, "storage_busy")
        finally:
            self.store.busy = False
        self.assertEqual(self.files(), [])
        with mock.patch.object(self.tracker, "is_loaded", return_value=False):
            self.assert_error(self.import_(), 503, "survey_loading")
            self.assert_error(self.overview(), 503, "survey_loading")
        self.assert_nothing_written()

    def test_project_deleted_during_validation_writes_nothing(self):
        tracker, pid = self.tracker, self.pid
        deleted = []

        async def pause(meter):
            if not deleted:
                tracker.delete_project(pid)
                deleted.append(pid)

        with mock.patch.object(stakeout, "_pause", pause):
            response = self.import_(make_package(targets=64, project_id=pid))
        self.assertTrue(deleted)
        self.assert_error(response, 404, "project_not_found")
        self.assert_nothing_written()

    def test_reserve_refusal_is_507(self):
        with mock.patch.object(self.tracker, "storage_capacity",
                               return_value={"writable": False}) as capacity:
            self.assert_error(self.import_(), 507, "storage_reserve")
        capacity.assert_called_once_with(2 * 393216 + 32768, force=True)
        self.assert_nothing_written()

    # --- deleting ---------------------------------------------------------

    def test_delete_set_flow_and_required_fields(self):
        self.assertEqual(self.import_()[0], 200)
        self.assertEqual(self.delete(), (200, {"deleted": self.SET_ID, "cleanup": "done"}))
        status, overview = self.overview()
        self.assertEqual((status, overview["sets"]), (200, []))
        # The removal is committed in the progress (stakeout WP4 E14).
        self.assertEqual(self.files(), [self.pid + "/progress.jsonl"])
        self.assert_error(self.delete(), 404, "set_not_found")
        self.assertEqual(self.import_()[0], 200)
        refusals = [
            ({"action": "delete_set", "expected_confirmed": 0}, "invalid_request"),
            ({"action": "delete_set", "set_id": self.SET_ID}, "invalid_request"),
            ({"action": "delete_set", "set_id": self.SET_ID, "expected_confirmed": True},
             "invalid_request"),
            ({"action": "delete_set", "project_id": 5, "set_id": self.SET_ID,
              "expected_confirmed": 0}, "invalid_request"),
            ({"action": "confirm_all"}, "unknown_action"),
            ({"action": "confirm"}, "invalid_request"),
            ([], "invalid_request"),
        ]
        for action, code in refusals:
            with self.subTest(action):
                self.assert_error(self.api("POST", "/api/stakeout",
                                           json.dumps(action).encode()), 400, code)
        for body in (b"x", b""):
            with self.subTest(body):
                self.assert_error(self.api("POST", "/api/stakeout", body),
                                  400, "invalid_request")
        self.assertEqual(self.files(), [self.pid + "/progress.jsonl", self.name()])
        self.assertEqual(self.delete(project_id=self.pid),
                         (200, {"deleted": self.SET_ID, "cleanup": "done"}))
        self.assert_error(self.delete(project_id="project-ffffffff-ffff"),
                          404, "project_not_found")

    def volume(self, free_blocks=100000):
        """Patches tracking's statvfs (4 KiB blocks); returns its mock."""
        patch = mock.patch.object(tracking.os, "statvfs", return_value=(
            4096, 4096, 200000, free_blocks, free_blocks, 0, 0, 0, 0, 255))
        statvfs = patch.start()
        self.addCleanup(patch.stop)
        return statvfs

    @staticmethod
    def charged(size):
        """What charge_storage counts for a file of size bytes on 4 KiB blocks."""
        return (-(-size // 4096) + 1) * 4096

    def test_an_import_samples_the_flash_once_and_charges_its_file(self):
        statvfs = self.volume()
        tracker = self.tracker
        tracker.storage_capacity(force=True)
        statvfs.reset_mock()
        # A budget of other writes since that sample: the import's own forced
        # sample resets it; the folders are made before that sample.
        tracker._storage_budget_used = 12345
        made = []
        real_capacity = tracker.storage_capacity

        def capacity(*args, **kwargs):
            made.append(os.path.isdir(os.path.join(self.root, self.pid)))
            return real_capacity(*args, **kwargs)

        with mock.patch.object(tracker, "storage_capacity", capacity):
            status, answer = self.import_()
        self.assertEqual(status, 200, answer)
        self.assertEqual(statvfs.call_count, 1)
        self.assertEqual(made, [True])
        self.assertEqual(tracker._storage_budget_used, self.charged(answer["bytes"]))
        self.assertEqual(tracker.storage_capacity()["free_flash_bytes"],
                         100000 * 4096 - self.charged(answer["bytes"]))
        self.assertIs(type(answer["timing_ms"]["capacity_refresh"]), int)
        # A second import: its own sample starts over, then one charge.
        status, second = self.import_(make_package(targets=3, project_id=self.pid))
        self.assertEqual(status, 200, second)
        self.assertEqual(statvfs.call_count, 2)
        self.assertEqual(tracker._storage_budget_used, self.charged(second["bytes"]))
        # An intermediate sample (a delete refreshes it) starts over too.
        self.assertEqual(self.delete(second["set_id"])[0], 200)
        self.assertEqual(statvfs.call_count, 3)
        self.assertEqual(tracker._storage_budget_used, 0)

    def test_the_committed_import_is_charged_when_its_answer_fails(self):
        self.volume()
        with mock.patch.object(stakeout.TargetStore, "_remember", side_effect=MemoryError):
            response = self.import_()
        self.assert_error(response, 500, "import_committed_response_failed",
                          set_id=self.SET_ID)
        self.assertEqual(self.tracker._storage_budget_used,
                         self.charged(os.path.getsize(self.path())))

    def test_an_unremovable_tmp_makes_the_next_flash_check_measure(self):
        statvfs = self.volume()
        with mock.patch.object(stakeout.os, "rename", side_effect=OSError(5, "io")), \
                mock.patch.object(stakeout.os, "remove", side_effect=OSError(5, "io")):
            self.assert_error(self.import_(), 507, "stakeout_write_failed")
        self.assertEqual(self.files(), [self.name() + ".tmp"])
        self.assertIsNone(self.tracker._storage_sample)
        self.tracker.storage_capacity()
        self.assertEqual(statvfs.call_count, 2)
        # A failure that removes its .tmp keeps the sample.
        os.remove(self.path() + ".tmp")
        with mock.patch.object(stakeout.os, "rename", side_effect=OSError(5, "io")):
            self.assert_error(self.import_(), 507, "stakeout_write_failed")
        self.assertIsNotNone(self.tracker._storage_sample)

    def test_a_delete_refreshes_the_flash_sample_after_its_answer(self):
        def statvfs(volume):
            with_set = any(name.endswith(".json") for name in self.files())
            free = 250 if with_set else 500
            return (4096, 4096, 1000, free, free, 0, 0, 0, 0, 255)

        self.assertEqual(self.import_()[0], 200)
        tracker = self.tracker
        with mock.patch.object(tracking.os, "statvfs", side_effect=statvfs):
            tracker.storage_capacity(force=True)
            self.assertEqual(tracker.storage_capacity()["free_flash_bytes"], 250 * 4096)
            self.assertEqual(self.delete(), (200, {"deleted": self.SET_ID, "cleanup": "done"}))
            self.assertEqual(tracker.storage_capacity()["free_flash_bytes"], 500 * 4096)

    def _record_commit_and_refresh(self):
        """Patches rename, remove, the yield and the flash check to log events."""
        events = []
        real_rename, real_capacity = os.rename, self.tracker.storage_capacity

        def rename(source, target):
            events.append("commit")
            return real_rename(source, target)

        def remove(path):
            events.append("remove")
            return _real_remove(path)

        async def sleep_ms(ms):
            events.append("yield")

        def capacity(*args, **kwargs):
            events.append("capacity")
            return real_capacity(*args, **kwargs)

        for patch in (mock.patch.object(stakeout.os, "rename", rename),
                      mock.patch.object(stakeout.os, "remove", remove),
                      mock.patch.object(web.asyncio, "sleep_ms", sleep_ms),
                      mock.patch.object(self.tracker, "storage_capacity", capacity)):
            patch.start()
            self.addCleanup(patch.stop)
        return events

    def test_no_flash_check_after_the_commit_and_a_refresh_after_a_delete(self):
        events = self._record_commit_and_refresh()
        status, answer = self.import_()
        self.assertEqual(status, 200, answer)
        self.assertEqual(events.count("capacity"), 1)
        self.assertEqual(events[events.index("commit") + 1:], [])
        del events[:]
        self.assertEqual(self.delete(), (200, {"deleted": self.SET_ID, "cleanup": "done"}))
        # The progress load yields; after the committed removal (stakeout WP4
        # E14) the set file and its .tmp go with a pause after each.
        self.assertEqual(events, ["yield", "remove", "yield", "remove", "yield", "yield",
                                  "capacity"])

    def test_delete_io_error_leaves_a_hidden_remnant(self):
        # The removal commits in the progress first (stakeout WP4 E14): a set
        # file that cannot be removed stays hidden until the next overview.
        self.assertEqual(self.import_()[0], 200)
        target = self.path()

        def remove(path):
            if path == target:
                raise OSError(5, "io")
            return _real_remove(path)

        with mock.patch.object(stakeout.os, "remove", side_effect=remove):
            self.assertEqual(self.delete(), (200, {"deleted": self.SET_ID,
                                                   "cleanup": "pending"}))
        self.assertTrue(os.path.exists(target))
        self.assertEqual(self.states(), {})
        self.assertFalse(os.path.exists(target))

    def test_unexpected_store_errors_still_get_an_envelope(self):
        self.assertEqual(self.import_()[0], 200)
        delete = json.dumps({"action": "delete_set", "set_id": self.SET_ID,
                             "expected_confirmed": 0}).encode()
        requests = (("sets", "GET", "/api/stakeout", b""),
                    ("send_set", "GET", "/api/stakeout?set_id=" + self.SET_ID, b""),
                    ("delete_set", "POST", "/api/stakeout", delete))
        for error, status, code in ((MemoryError(), 503, "stakeout_busy"),
                                    (OSError(5, "io"), 507, "stakeout_read_failed")):
            for name, method, path, body in requests:
                with self.subTest(name=name, error=error):
                    with mock.patch.object(self.store, name, side_effect=error):
                        self.assert_error(self.api(method, path, body), status, code)
        self.assertEqual(self.states(), {self.SET_ID: "ok"})

    def test_unknown_stakeout_subpath_is_404(self):
        self.assert_error(self.api("GET", "/api/stakeout/nothing"), 404, "not_found")

    # --- project deletion (C6) --------------------------------------------

    def track(self, action, **fields):
        """(status, answer) of one tracking action through the dispatcher."""
        fields["action"] = action
        return self.api("POST", "/api/tracking", json.dumps(fields).encode())

    def create(self, name):
        """A new project whose ID no earlier project had (a restart starts the
        tracker's ID sequence over)."""
        self.sequence = getattr(self, "sequence", 0x100) + 1
        self.tracker._sequence = self.sequence
        return self.tracker.create_project(name)["id"]

    def new_project(self, name, targets=3):
        """A second project with one set; returns its id."""
        project_id = self.create(name)
        status, answer = self.import_(compact(make_package(targets=targets,
                                                           project_id=project_id)))
        self.assertEqual(status, 200, answer)
        return project_id

    def again(self):
        """A new fixture project with the sample set, a .tmp and a stray file."""
        self.pid = self.create("Again")
        self.assertEqual(self.import_()[0], 200)
        for name, data in ((self.SET_ID + ".json.tmp", b"x"), ("notes.txt", b"{}")):
            with _real_open(os.path.join(self.root, self.pid, name), "wb") as target:
                target.write(data)
        return self.project_files()

    def store_os(self, name, fake):
        """Patch os.<name> for paths in the stakeout store only (the tracker
        uses the same os); fake(real, path, *args) runs instead."""
        real, root = getattr(os, name), self.root

        def call(path, *args, **kwargs):
            if str(path).startswith(root):
                return fake(real, path, *args, **kwargs)
            return real(path, *args, **kwargs)

        return mock.patch.object(stakeout.os, name, call)

    @staticmethod
    def failing(error=None):
        def fake(real, *args):
            raise error or OSError(5, "io")
        return fake

    def project_files(self, project_id=None):
        """The files of one project folder (and of its set-aside folder), with bytes."""
        project_id = project_id or self.pid
        return {name: data for name, data in self.snapshot().items()
                if name.split("/")[0] in (project_id, project_id + ".deleting")}

    def others(self, project_id=None):
        project_id = project_id or self.pid
        return {name: data for name, data in self.snapshot().items()
                if name.split("/")[0] not in (project_id, project_id + ".deleting")}

    @staticmethod
    def without_floors(files):
        """files without the progress files that hold nothing but the floors a
        survey restore writes for each live project with stakeout data
        (stakeout WP4 E26)."""
        return {name: data for name, data in files.items()
                if not (name.endswith("/progress.jsonl") and all(
                    json.loads(line).get("ev") == "restored"
                    for line in data.decode().splitlines()[1:]))}

    def with_sets(self):
        """The fixture project with its sample set, a .tmp and a stray file; a
        neighbour project, an archived project with a set and an unrelated
        root file. Returns (files of the project, every other file)."""
        self.assertEqual(self.import_()[0], 200)
        for name, data in ((self.SET_ID + ".json.tmp", b"x"), ("notes.txt", b"{}")):
            with _real_open(os.path.join(self.root, self.pid, name), "wb") as target:
                target.write(data)
        self.neighbour = self.new_project("Neighbour")
        archived = self.new_project("Archived", targets=4)
        status, answer = self.track("archive_project", project_id=archived)
        self.assertEqual(status, 200, answer)
        self.archive_id = answer["id"]
        self.archived = archived
        with _real_open(os.path.join(self.root, "readme.txt"), "wb") as target:
            target.write(b"keep")
        others = self.others()
        self.assertEqual(len(others), 3)
        return self.project_files(), others

    def live(self, project_id=None):
        return web._project_live(project_id or self.pid)

    def restart(self, reconcile=True):
        """A new start over the same files, with the store's start-up hook."""
        self.tracker._sync_journal(True)
        self.tracker.point_store.close()
        tracker = tracking.Tracker(os.path.join(self.folder, "survey"), autoload=False,
                                   point_store_autoload=False)
        self.addCleanup(tracker.point_store.close)
        self.addCleanup(tracker._sync_journal, True)
        store = stakeout.TargetStore(self.root)
        asyncio.run(tracker.initialize_async(store.reconcile_deletions if reconcile
                                             else None))
        self.tracker = web.tracker = tracker
        self.use_store(store)

    def assert_project_kept(self, files):
        self.assertTrue(self.live())
        self.assertEqual(self.project_files(), files)
        self.assertFalse(os.path.exists(os.path.join(self.root, self.pid + ".deleting")))
        self.assertEqual(self.states_of(self.pid), ["ok"])

    def assert_project_gone(self, others):
        self.assertFalse(self.live())
        self.assertEqual(self.project_files(), {})
        self.assertEqual(sorted(os.listdir(self.root)),
                         sorted([self.neighbour, self.archived, "readme.txt"]))
        self.assertEqual(self.others(), others)

    def test_deleting_a_project_removes_its_sets_and_nothing_else(self):
        _files, others = self.with_sets()
        status, answer = self.track("delete_project", project_id=self.pid)
        self.assertEqual(status, 200, answer)
        self.assertEqual(answer["stakeout_cleanup"], "done")
        self.assert_project_gone(others)
        self.assertEqual(self.states_of(self.neighbour), ["ok"])
        # The same ID again (a new created_at) sees no old set.
        with mock.patch.object(self.tracker, "_id", return_value=self.pid), \
                support.patch_module_global("tracking", "_now_seconds", return_value=900000000):
            self.assertEqual(self.tracker.create_project("Again")["id"], self.pid)
        status, overview = self.overview(self.pid)
        self.assertEqual((status, overview["sets"], overview["totals"]["orphaned"]),
                         (200, [], 0))
        self.restart()
        self.assertEqual(self.others(), others)
        self.assertEqual(self.overview(self.pid)[1]["sets"], [])

    def states_of(self, project_id):
        status, answer = self.overview(project_id)
        self.assertEqual(status, 200, answer)
        return [item["state"] for item in answer["sets"]]

    def test_deleting_a_project_without_sets_answers_done(self):
        status, answer = self.track("delete_project", project_id=self.pid)
        self.assertEqual((status, answer["stakeout_cleanup"]), (200, "done"))
        self.assertFalse(os.path.exists(self.root))

    def test_project_deletion_refusals_change_nothing(self):
        files, _others = self.with_sets()
        before = self.snapshot()

        @contextlib.contextmanager
        def busy():
            self.store.busy = True
            try:
                yield
            finally:
                self.store.busy = False

        @contextlib.contextmanager
        def reading():
            self.store._readers[self.pid] = 1
            try:
                yield
            finally:
                self.store._readers[self.pid] = 0

        @contextlib.contextmanager
        def pending():
            aside = os.path.join(self.root, self.pid + ".deleting")
            os.mkdir(aside)
            try:
                yield
            finally:
                os.rmdir(aside)

        cases = ((busy, 409, "storage_busy"), (reading, 409, "storage_busy"),
                 (pending, 409, "stakeout_cleanup_pending"),
                 (lambda: self.store_os("rename", self.failing()),
                  507, "stakeout_delete_failed"))
        for condition, status, code in cases:
            with self.subTest(code), condition():
                self.assert_error(self.track("delete_project", project_id=self.pid),
                                  status, code)
                self.assertTrue(self.live())
            self.assertEqual(self.snapshot(), before)
        # The tracker's own preconditions come first, unchanged.
        self.assert_error(self.track("delete_project", project_id="project-none"),
                          400, "invalid_tracking_request")
        self.tracker.select_project(self.pid)
        self.tracker.start_line("Line", 1, {}, None, "route")
        with self.store_os("rename", self.failing(AssertionError())):
            self.assert_error(self.track("delete_project", project_id=self.pid),
                              400, "invalid_tracking_request")
        self.tracker.set_line_state("finish")
        self.assertEqual(self.snapshot(), before)
        self.assert_project_kept(files)

    def test_a_rename_that_moves_and_then_raises_deletes_the_project(self):
        _files, others = self.with_sets()
        def moved_then_raised(real, source, target):
            real(source, target)
            raise OSError(5, "io")

        with self.store_os("rename", moved_then_raised):
            status, answer = self.track("delete_project", project_id=self.pid)
        self.assertEqual((status, answer["stakeout_cleanup"]), (200, "done"))
        self.assert_project_gone(others)

    def test_tracker_failures_are_read_from_the_survey_in_ram(self):
        aside = os.path.join(self.root, "%s.deleting")
        files, others = self.with_sets()
        # Before the journal write: nothing applied, the folder comes back.
        with mock.patch.object(self.tracker, "_write_journal_event",
                               side_effect=OSError(28, "no space")):
            response = self.track("delete_project", project_id=self.pid)
        self.assert_error(response, 507, "tracking_storage_failed")
        self.assertNotIn("stakeout_cleanup", response[1])
        self.assert_project_kept(files)
        self.assertEqual(self.store._blocked, set())
        # The journal written but its sync or the index after it failing:
        # the project is gone in RAM, so the folder stays for the next start.
        sync = self.tracker._sync_journal

        def failing_sync(close=False):
            # Only the sync after the event is applied (the write may close
            # a full segment first).
            if not self.live():
                raise OSError(5, "io")
            return sync(close)

        for name in ("_sync_journal", "_write_index"):
            with self.subTest(name):
                if not self.live():
                    self.restart()
                    self.assert_project_gone(others)
                    files = self.again()
                web.log.reset_mock()
                failure = (failing_sync if name == "_sync_journal"
                           else mock.Mock(side_effect=OSError(5, "io")))
                with mock.patch.object(self.tracker, name, failure):
                    response = self.track("delete_project", project_id=self.pid)
                self.assert_error(response, 507, "tracking_storage_failed",
                                  stakeout_cleanup="pending")
                self.assertFalse(self.live())
                self.assertEqual(self.project_files(),
                                 {name.replace(self.pid, self.pid + ".deleting", 1): data
                                  for name, data in files.items()})
                self.assertTrue(os.path.isdir(aside % self.pid))
                self.assertEqual(self.others(), others)
                self.assertEqual(web.log.call_args[0][0], "ERROR")
                self.assertEqual(self.store._blocked, set())
        # The next start decides from the durable journal: deleted.
        self.restart()
        self.assert_project_gone(others)

    def test_a_failed_rename_back_blocks_the_project_until_the_next_start(self):
        files, others = self.with_sets()
        renames = []

        def first_only(real, source, target):
            renames.append((source, target))
            if len(renames) > 1:
                raise OSError(5, "io")
            real(source, target)

        web.log.reset_mock()
        with mock.patch.object(self.tracker, "_write_journal_event",
                               side_effect=OSError(28, "no space")), \
                self.store_os("rename", first_only):
            self.assert_error(self.track("delete_project", project_id=self.pid),
                              507, "tracking_storage_failed")
        self.assertEqual(len(renames), 2)
        self.assertTrue(self.live())
        self.assertEqual(self.store._blocked, {self.pid})
        self.assertTrue(os.path.isdir(os.path.join(self.root, self.pid + ".deleting")))
        # Stakeout requests of the project: 503.
        for response in (self.overview(self.pid), self.import_(),
                         self.delete(project_id=self.pid)):
            self.assert_error(response, 503, "stakeout_recovery_pending")
        status, _head, _payload = self.read_set()
        self.assertEqual(status, 503)
        # Other projects keep working.
        self.assertEqual(self.states_of(self.neighbour), ["ok"])
        # Archive, every restore entry and a second deletion: 409.
        backup = self.tracker.backup()
        for action, fields in (("archive_project", {"project_id": self.pid}),
                               ("delete_project", {"project_id": self.pid}),
                               ("restore", {"backup": backup}),
                               ("reactivate_archive", {"archive_id": self.archive_id})):
            with self.subTest(action):
                self.assert_error(self.track(action, **fields), 409,
                                  "stakeout_recovery_pending")
        status, _head, payload = self.call("POST", "/api/tracking/import", b"{}\n",
                                           content_type="application/x-ndjson")
        self.assertEqual((status, json.loads(payload)["error"]),
                         (409, "stakeout_recovery_pending"))
        self.assertTrue(self.live())
        self.assertIn(self.archive_id, [item["id"] for item in self.tracker.archives])
        self.assertEqual(self.others(), others)
        # The next start puts the folder back; archive and reactivation then
        # keep the sets.
        self.restart()
        self.assertEqual(self.store._blocked, set())
        self.assert_project_kept(files)
        status, answer = self.track("archive_project", project_id=self.pid)
        self.assertEqual(status, 200, answer)
        self.assertFalse(self.live())
        self.restart()
        self.assertEqual(self.project_files(), files)
        status, answer = self.track("reactivate_archive", archive_id=answer["id"])
        self.assertEqual(status, 200, answer)
        self.assert_project_kept(files)

    def test_an_unlisted_store_root_blocks_every_project_until_the_next_start(self):
        files, others = self.with_sets()
        # A cut between the set-aside rename and the journal append leaves
        # the live project's folder set aside.
        os.rename(os.path.join(self.root, self.pid),
                  os.path.join(self.root, self.pid + ".deleting"))

        def root_fails(real, path):
            if path == self.root:
                raise OSError(5, "io")
            return real(path)

        stakeout.log.reset_mock()
        with self.store_os("listdir", root_fails):
            self.restart()
        self.assertTrue(self.tracker.is_loaded())
        self.assertEqual(stakeout.log.call_args[0][0], "WARN")
        before = self.snapshot()
        backup = self.tracker.backup()
        for action, fields in (("archive_project", {"project_id": self.pid}),
                               ("archive_project", {"project_id": self.neighbour}),
                               ("delete_project", {"project_id": self.pid}),
                               ("delete_project", {"project_id": self.neighbour}),
                               ("restore", {"backup": backup}),
                               ("reactivate_archive", {"archive_id": self.archive_id})):
            with self.subTest(action, **fields):
                self.assert_error(self.track(action, **fields), 409,
                                  "stakeout_recovery_pending")
        status, _head, payload = self.call("POST", "/api/tracking/import", b"{}\n",
                                           content_type="application/x-ndjson")
        self.assertEqual((status, json.loads(payload)["error"]),
                         (409, "stakeout_recovery_pending"))
        self.assert_error(self.overview(self.neighbour), 503, "stakeout_recovery_pending")
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(self.live())
        self.assertTrue(self.live(self.neighbour))
        # The next start lists the root and puts the folder back, sets intact.
        self.restart()
        self.assert_project_kept(files)
        self.assertEqual(self.others(), others)
        status, answer = self.track("archive_project", project_id=self.pid)
        self.assertEqual(status, 200, answer)

    def test_a_cleanup_failure_at_any_file_still_answers_200(self):
        _files, others = self.with_sets()
        count = len(self.project_files()) + 1
        for failing in range(count):
            with self.subTest(failing=failing):
                if failing:
                    self.restart()
                    self.assert_project_gone(others)
                    self.again()
                calls = []

                def fails(real, path):
                    calls.append(path)
                    if len(calls) == failing + 1:
                        raise OSError(5, "io")
                    real(path)

                stakeout.log.reset_mock()
                with self.store_os("remove", fails), self.store_os("rmdir", fails):
                    status, answer = self.track("delete_project", project_id=self.pid)
                self.assertEqual((status, answer["stakeout_cleanup"]), (200, "pending"))
                self.assertEqual(stakeout.log.call_args[0][0], "WARN")
                self.assertFalse(self.live())
                self.assertEqual(len(self.project_files()), count - 1 - failing)
                self.assertEqual(self.others(), others)
        self.restart()
        self.assert_project_gone(others)

    def test_a_deletion_measures_the_flash_after_its_answer(self):
        self.with_sets()
        order = []
        send, capacity = web._send_json, self.tracker.storage_capacity

        def answer(writer, payload, *args, **kwargs):
            order.append("answer")
            return send(writer, payload, *args, **kwargs)

        def measured(*args, **kwargs):
            order.append(kwargs)
            return capacity(*args, **kwargs)

        empty = self.create("Empty")
        with mock.patch.object(web, "_send_json", answer), \
                mock.patch.object(self.tracker, "storage_capacity", measured):
            status, _answer = self.track("delete_project", project_id=self.pid)
            self.assertEqual(status, 200)
            # The journal append checks the cached sample; one forced sample
            # follows the answer.
            self.assertNotIn({"force": True}, order[:order.index("answer")])
            self.assertEqual(order[order.index("answer"):], ["answer", {"force": True}])
            # Without set files there is nothing to measure.
            order.clear()
            self.assertEqual(self.track("delete_project", project_id=empty)[0], 200)
            self.assertEqual(order[order.index("answer"):], ["answer"])
            self.assertNotIn({"force": True}, order)

    def test_a_power_loss_at_any_step_is_settled_by_the_next_start(self):
        class PowerLoss(BaseException):
            pass

        files, others = self.with_sets()
        steps = [("rename", "before", True), ("rename", "after", True),
                 ("journal", "before", True), ("journal", "after", False)]
        steps += [(name, when, False) for name in ["remove"] * (len(files)) + ["rmdir"]
                  for when in ("before", "after")]
        counts = {}
        self.assertEqual(len(files), 3)
        for index, (name, when, kept) in enumerate(steps):
            nth = sum(1 for step in steps[:index] if step[:2] == (name, when))
            with self.subTest(step=name, when=when, nth=nth):
                if not self.live():
                    files = self.again()
                counts.clear()

                def dying(label):
                    def call(real, *args):
                        counts[label] = counts.get(label, 0) + 1
                        if label == name and counts[label] == nth + 1:
                            if when == "after":
                                real(*args)
                            raise PowerLoss()
                        return real(*args)
                    return call

                journal = dying("journal")
                delete = self.tracker.delete_project
                with self.store_os("rename", dying("rename")), \
                        mock.patch.object(self.tracker, "delete_project",
                                          lambda pid: journal(delete, pid)), \
                        self.store_os("remove", dying("remove")), \
                        self.store_os("rmdir", dying("rmdir")):
                    with self.assertRaises(PowerLoss):
                        asyncio.run(web._delete_project(self.pid))
                self.restart()
                if kept:
                    self.assert_project_kept(files)
                else:
                    self.assertFalse(self.live())
                    self.assertEqual(self.project_files(), {})
                self.assertEqual(self.others(), others)
                self.assertEqual(self.store._blocked, set())

    def test_mutations_are_refused_while_the_start_up_hook_runs(self):
        files, _others = self.with_sets()
        self.tracker._sync_journal(True)
        self.tracker.point_store.close()
        tracker = tracking.Tracker(os.path.join(self.folder, "survey"), autoload=False,
                                   point_store_autoload=False)
        self.addCleanup(tracker.point_store.close)
        self.addCleanup(tracker._sync_journal, True)
        store = stakeout.TargetStore(self.root)
        web.tracker = tracker
        self.use_store(store)
        headers = {"host": self.HOST, "origin": self.ORIGIN,
                   "cookie": "rtk_session=" + self.session["token"],
                   "x-csrf-token": self.session["csrf"],
                   "content-type": "application/json"}
        answers = []

        async def scenario():
            reached, release = asyncio.Event(), asyncio.Event()

            async def hook(given):
                reached.set()
                await release.wait()
                await store.reconcile_deletions(given)

            start = asyncio.ensure_future(tracker.initialize_async(hook))
            await reached.wait()
            requests = [
                (web._handle_tracking_request, "/api/tracking",
                 {"action": "delete_project", "project_id": self.pid}),
                (web._handle_tracking_request, "/api/tracking",
                 {"action": "archive_project", "project_id": self.pid}),
                (web._handle_tracking_request, "/api/tracking",
                 {"action": "create_project", "name": "Late"}),
                (web._handle_stakeout_request, "/api/stakeout",
                 {"action": "delete_set", "project_id": self.pid, "set_id": self.SET_ID,
                  "expected_confirmed": 0}),
                (web._handle_stakeout_request, "/api/stakeout/import",
                 self.package())]
            for handler, path, body in requests:
                writer = FakeWriter()
                await handler(writer, path, "POST", "", json.dumps(body).encode(), False,
                              headers)
                answers.append(bytes(writer.buf))
            self.assertFalse(start.done())
            release.set()
            await start

        asyncio.run(scenario())
        for answer in answers:
            self.assertIn(b"503 Service Unavailable", answer)
            self.assertIn(b"survey_loading", answer)
        self.assertTrue(tracker.is_loaded())
        self.assert_project_kept(files)
        self.assertEqual([p["name"] for p in tracker.projects].count("Late"), 0)

    def test_archive_reactivation_and_restore_never_sweep_project_folders(self):
        files, others = self.with_sets()
        archived_files = {name: data for name, data in others.items()
                          if name.startswith(self.archived + "/")}
        self.assertEqual(len(archived_files), 1)
        # An archived project keeps its folder across starts.
        self.restart()
        self.assertEqual(self.others(), others)
        status, answer = self.track("reactivate_archive", archive_id=self.archive_id)
        self.assertEqual(status, 200, answer)
        self.assertEqual(self.states_of(self.archived), ["ok"])
        # A restore without the project leaves its folder alone, also at the
        # next start.
        backup = self.tracker.backup()
        self.assertEqual(self.track("delete_project", project_id=self.neighbour)[0], 200)
        others = self.others()
        backup["projects"] = [project for project in backup["projects"]
                              if project["id"] != self.pid]
        status, answer = self.track("restore", backup=backup)
        self.assertEqual(status, 200, answer)
        self.assertFalse(self.live())
        self.restart()
        self.assertEqual(self.without_floors(self.project_files()), files)
        self.assertEqual(self.without_floors(self.others()), others)

    def test_a_restored_project_id_without_stakeout_form_is_deleted_as_before(self):
        backup = self.tracker.backup()
        restored = copy.deepcopy(backup["projects"][0])
        restored.update(id="Projekt 1", name="Restored")
        backup["projects"].append(restored)
        status, answer = self.track("restore", backup=backup)
        self.assertEqual(status, 200, answer)
        self.assertTrue(self.live("Projekt 1"))
        self.assertEqual(self.import_()[0], 200)
        before = self.snapshot()
        status, answer = self.track("delete_project", project_id="Projekt 1")
        self.assertEqual((status, answer.get("stakeout_cleanup")), (200, "done"), answer)
        self.assertFalse(self.live("Projekt 1"))
        self.assertEqual(self.snapshot(), before)
        # Archive, start-up and the stakeout guards handle such IDs too.
        status, answer = self.track("restore", backup=backup)
        self.assertEqual(status, 200, answer)
        self.assert_error(self.overview("Projekt%201"), 400, "invalid_request")
        status, answer = self.track("archive_project", project_id="Projekt 1")
        self.assertEqual(status, 200, answer)
        self.restart()
        self.assertEqual(self.without_floors(self.snapshot()), before)
        self.assertEqual(self.states_of(self.pid), ["ok"])

    def test_a_pending_cleanup_blocks_set_changes_until_the_next_start(self):
        files, others = self.with_sets()
        backup = self.tracker.backup()
        calls = []

        def first_fails(real, path):
            calls.append(path)
            if len(calls) == 1:
                raise OSError(5, "io")
            real(path)

        with self.store_os("remove", first_fails):
            status, answer = self.track("delete_project", project_id=self.pid)
        self.assertEqual((status, answer["stakeout_cleanup"]), (200, "pending"))
        aside = self.project_files()
        self.assertTrue(aside)
        # A restore brings the project back (it is not blocked) ...
        status, answer = self.track("restore", backup=backup)
        self.assertEqual(status, 200, answer)
        self.assertTrue(self.live())
        # ... but its sets cannot change while the leftover exists.
        other = compact(self.package(make_package(targets=5, project_id=self.pid)))
        self.assert_error(self.import_(other), 409, "stakeout_cleanup_pending")
        self.assertFalse(os.path.exists(os.path.join(self.root, self.pid)))
        self.assertEqual(self.project_files(), aside)
        self.assertEqual(self.without_floors(self.others()), others)
        # The next start puts the folder back (live project); nothing is blocked.
        self.restart()
        self.assertEqual(self.store._blocked, set())
        self.assertFalse(os.path.exists(os.path.join(self.root, self.pid + ".deleting")))
        self.assertEqual(self.project_files(), files)
        self.assertEqual(self.states_of(self.pid), ["ok"])
        status, answer = self.import_(other)
        self.assertEqual(status, 200, answer)
        self.assertEqual(self.states_of(self.pid), ["ok", "ok"])
        # A later start finds one folder and blocks nothing.
        self.restart()
        self.assertEqual(self.store._blocked, set())
        self.assertEqual(self.track("restore", backup=self.tracker.backup())[0], 200)

    def test_main_passes_the_reconciliation_to_the_tracker(self):
        from support import main
        self.with_sets()
        self.assertTrue(self.store.begin_project_delete(self.neighbour))
        self.tracker.delete_project(self.neighbour)
        self.tracker._sync_journal(True)
        self.tracker.point_store.close()
        tracker = tracking.Tracker(os.path.join(self.folder, "survey"), autoload=False,
                                   point_store_autoload=False)
        self.addCleanup(tracker.point_store.close)
        self.addCleanup(tracker._sync_journal, True)
        aside = os.path.join(self.root, self.neighbour + ".deleting")
        self.assertTrue(os.path.isdir(aside))
        with mock.patch.object(main, "tracker", tracker), \
                mock.patch.object(main, "prepare_parallel_benchmark",
                                  return_value=(None, None)), \
                mock.patch.object(tracker, "run_worker", mock.AsyncMock()), \
                mock.patch.object(main, "log"):
            asyncio.run(main.tracking_startup_task())
        self.assertTrue(tracker.is_loaded())
        self.assertFalse(os.path.exists(aside))
        self.assertEqual(state.app.stats["tracking_startup"]["state"], "ready")

    # --- session and CSRF -------------------------------------------------

    def test_session_and_csrf_are_enforced(self):
        never = mock.patch.object(web, "parse_post_data",
                                  side_effect=AssertionError("form parser ran"))
        refusals = [
            ("no cookie", {"cookie": False}, 401, "admin_session_required"),
            ("no token", {"csrf": False}, 403, "csrf_rejected"),
            ("wrong token", {"csrf": "0" * 32}, 403, "csrf_rejected"),
            ("foreign origin", {"origin": "http://evil.example"}, 403, "origin_rejected"),
        ]
        for label, options, status, code in refusals:
            with self.subTest("import " + label):
                with never:
                    response = self.import_(**options)
                self.assert_error(response, status, code)
                self.assert_nothing_written()
        with never:
            self.assertEqual(self.import_()[0], 200)
        before = self.files()
        action = json.dumps({"action": "delete_set", "set_id": self.SET_ID,
                             "expected_confirmed": 0}).encode()
        for label, options, status, code in refusals:
            with self.subTest("delete " + label):
                with never:
                    response = self.api("POST", "/api/stakeout", action, **options)
                self.assert_error(response, status, code)
                self.assertEqual(self.files(), before)
                self.assertEqual(self.states(), {self.SET_ID: "ok"})
        with never:
            self.assertEqual(self.api("POST", "/api/stakeout", action),
                             (200, {"deleted": self.SET_ID, "cleanup": "done"}))
            status, overview = self.api("GET", "/api/stakeout", csrf=False)
            self.assertEqual((status, overview["sets"]), (200, []))
            self.assert_error(self.api("GET", "/api/stakeout", cookie=False, csrf=False),
                              401, "admin_session_required")


class EventWriter(BufferedWriter):
    """The event stream test writer, reached on the STA address of the test device."""

    def get_extra_info(self, key):
        if key == "peername":
            return ("203.0.113.10", 43210)
        if key == "sockname":
            return ("203.0.113.20", 80)
        return None


class TestEventsEndpoint(unittest.TestCase):
    """GET/HEAD /api/events through handle_http_client (#27 E6-E9), on a real event
    loop and clock with the stream constants scaled down like the event stream tests."""

    HOST = "rtk-d4e5f6.local"

    def setUp(self):
        access.reset()
        self.addCleanup(access.reset)
        identity = {"device_id": "A1B2C3D4E5F6", "display_name": "RTK-D4E5F6",
                    "ble_name": "RTK-D4E5F6", "hostname": "rtk-d4e5f6",
                    "device_code": "ABCDEF123456", "stream_token": "0" * 32}
        patchers = [mock.patch.object(state.app, "identity", identity),
                    mock.patch.dict(state.app.stats),
                    mock.patch.object(web, "_http_connections", 0),
                    mock.patch.object(events, "hub", events.EventHub()),
                    mock.patch.object(events, "WRITE_TIMEOUT_SEC", 0.25),
                    mock.patch.object(events, "HEARTBEAT_SEC", 0.1),
                    mock.patch.object(events, "SESSION_CHECK_MS", 50),
                    mock.patch.object(events, "log")]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        state.app.stats["sta_ip"] = "203.0.113.20"
        self.hub = events.hub
        self.version = 0

    def login(self):
        session, error = access.login("ABCDEF123456", "ABCDEF123456", "203.0.113.9")
        self.assertIsNone(error)
        return "rtk_session=" + session["token"], session["csrf"], session["token"]

    def request(self, writer, method="GET", path="/api/events", cookie=None, csrf=None):
        """The dispatcher task of one request answered on writer."""
        lines = ["%s %s HTTP/1.0" % (method, path), "Host: " + self.HOST,
                 "Origin: http://" + self.HOST]
        if cookie:
            lines.append("Cookie: " + cookie)
        if csrf:
            lines.append("X-CSRF-Token: " + csrf)
        body = b""
        if method not in ("GET", "HEAD"):
            body = b"{}"
            lines += ["Content-Type: application/json", "Content-Length: %d" % len(body)]
        raw = ("\r\n".join(lines) + "\r\n\r\n").encode() + body
        return asyncio.ensure_future(web.handle_http_client(FakeReader(raw), writer))

    def stat(self, name):
        return state.app.stats[name]

    @staticmethod
    def status(data):
        return int(bytes(data).split(b" ", 2)[1])

    @staticmethod
    def payload(data):
        return json.loads(bytes(data).partition(b"\r\n\r\n")[2])

    def tick(self, offset_ms=0):
        """One hub tick with a changed fix version (a frame when the cadence allows)."""
        self.version += 1
        self.hub.tick(time.ticks_ms() + offset_ms, lambda now_ms: {"live": self.version},
                      lambda: (self.version, 0))

    async def finish(self, task, within):
        """Wait for the handler to end by itself; returns the monotonic end time."""
        await asyncio.wait_for(asyncio.shield(task), within)
        self.assertIsNone(task.result())
        return time.monotonic()

    async def end_by_logout(self, task, cookie):
        # Streams end by themselves; client tasks are never cancelled on the device.
        access.logout(cookie)
        await self.finish(task, 2 * events.SESSION_CHECK_MS / 1000)

    def assert_closed_and_released(self, writer):
        self.assertTrue(writer.closed)
        self.assertEqual(web._http_connections, 0)
        self.assertEqual(self.hub.open, 0)
        self.assertEqual(self.hub.subs, [])

    # --- answers that keep their request slot --------------------------------

    def test_events_without_session_is_401_json(self):
        async def run():
            writer = EventWriter()
            await self.finish(self.request(writer), 0.1)
            self.assertEqual(self.status(writer.sent), 401)
            self.assertEqual(self.payload(writer.sent), SIGN_IN)
            self.assertEqual(writer.pending_at_close, b"")
            self.assert_closed_and_released(writer)
        asyncio.run(run())

    def test_blocked_refusal_keeps_its_request_slot(self):
        async def run():
            writer = EventWriter()
            writer.block.set()
            started = time.monotonic()
            task = self.request(writer)
            await asyncio.sleep(0.05)
            self.assertFalse(task.done())
            self.assertEqual(web._http_connections, 1)
            ended = await self.finish(task, events.WRITE_TIMEOUT_SEC + 0.1)
            self.assertLessEqual(ended - started, events.WRITE_TIMEOUT_SEC + 0.1)
            self.assertEqual(writer.sent, b"")
            self.assertEqual(self.status(writer.pending_at_close), 401)
            self.assert_closed_and_released(writer)
        asyncio.run(run())

    def test_head_sends_only_the_stream_header(self):
        cookie = self.login()[0]

        async def run():
            writer = EventWriter()
            writer.block.set()
            task = self.request(writer, "HEAD", cookie=cookie)
            await asyncio.sleep(0.03)
            self.assertEqual(web._http_connections, 1)
            self.assertEqual((self.hub.open, self.hub.subs), (0, []))
            writer.block.clear()
            await self.finish(task, 0.1)
            self.assertEqual(bytes(writer.sent), events.HEADER)
            self.assertEqual(writer.pending_at_close, b"")
            self.assertEqual(self.stat("sse_refusals"), 0)
            self.assert_closed_and_released(writer)
        asyncio.run(run())

    def test_kill_switch_answers_503(self):
        cookie = self.login()[0]

        async def run():
            writer = EventWriter()
            await self.finish(self.request(writer, cookie=cookie), 0.1)
            self.assertEqual(self.status(writer.sent), 503)
            self.assertEqual(self.payload(writer.sent)["error"], "events_disabled")
            self.assertEqual(writer.pending_at_close, b"")
            self.assert_closed_and_released(writer)
        with mock.patch.object(events, "MAX_STREAMS", 0):
            asyncio.run(run())

    def test_full_hub_answers_503_busy(self):
        cookie = self.login()[0]

        async def run():
            for _ in range(events.MAX_OPEN_STREAMS):
                self.assertIsNotNone(self.hub.reserve(lambda: True))
            self.assertEqual(self.hub.open, 4)
            writer = EventWriter()
            writer.block.set()
            task = self.request(writer, cookie=cookie)
            await asyncio.sleep(0.03)
            self.assertEqual(web._http_connections, 1)
            writer.block.clear()
            await self.finish(task, 0.1)
            self.assertEqual(self.status(writer.sent), 503)
            self.assertEqual(self.payload(writer.sent)["error"], "events_busy")
            self.assertEqual(writer.pending_at_close, b"")
            self.assertEqual(self.stat("sse_refusals"), 1)
            self.assertEqual(web._http_connections, 0)
            self.assertEqual(self.hub.open, 4)
            self.assertTrue(writer.closed)
        asyncio.run(run())

    # --- admitted streams -------------------------------------------------------

    def test_stream_releases_its_request_slot(self):
        cookie = self.login()[0]
        total = self.stat("http_connections_total")

        async def run():
            self.assertEqual(web._http_connections, 0)
            writer = EventWriter()
            task = self.request(writer, cookie=cookie)
            await asyncio.sleep(0.02)
            self.assertTrue(bytes(writer.sent).startswith(events.HEADER + events.PREAMBLE))
            self.assertEqual(self.hub.open, 1)
            writer.block.set()    # every later drain hangs
            samples = []
            deadline = time.monotonic() + 1
            while not task.done() and time.monotonic() < deadline:
                samples.append(web._http_connections)
                await asyncio.sleep(0.005)
            await self.finish(task, 0.01)
            self.assertEqual(set(samples), {0})
            self.assertEqual(self.stat("http_connections_total"), total + 1)
            self.assertEqual(self.stat("sse_write_timeouts"), 1)
            self.assert_closed_and_released(writer)
        asyncio.run(run())

    def test_open_streams_stay_bounded_under_repeated_joins(self):
        cookie = self.login()[0]
        real_serve = self.hub.serve
        running = [0]
        peak = {"open": 0, "serve": 0}

        async def counting_serve(writer, sub):
            running[0] += 1
            peak["serve"] = max(peak["serve"], running[0])
            try:
                await real_serve(writer, sub)
            finally:
                running[0] -= 1

        async def run():
            writers, tasks = [], []

            async def sample():
                while len(tasks) < 10 or not all(task.done() for task in tasks):
                    peak["open"] = max(peak["open"], self.hub.open)
                    await asyncio.sleep(0.001)

            sampler = asyncio.ensure_future(sample())
            for _ in range(10):
                writer = EventWriter()
                writer.block.set()
                writers.append(writer)
                tasks.append(self.request(writer, cookie=cookie))
                await asyncio.sleep(0.005)
                peak["open"] = max(peak["open"], self.hub.open)
            await asyncio.wait_for(asyncio.gather(*tasks), 1.0)
            await sampler
            return writers

        with mock.patch.object(self.hub, "serve", counting_serve):
            writers = asyncio.run(run())
        self.assertEqual(peak["open"], 4)
        self.assertEqual(peak["serve"], 4)
        for index, writer in enumerate(writers):
            with self.subTest(request=index + 1):
                self.assertTrue(writer.closed)
                self.assertEqual(writer.sent, b"")
                if index < 4:
                    self.assertTrue(writer.pending_at_close.startswith(events.HEADER))
                else:
                    self.assertEqual(self.status(writer.pending_at_close), 503)
                    self.assertEqual(self.payload(writer.pending_at_close)["error"],
                                     "events_busy")
        self.assertEqual((self.hub.open, self.hub.subs), (0, []))
        self.assertEqual(web._http_connections, 0)
        self.assertEqual(self.stat("sse_refusals"), 6)
        self.assertEqual(self.stat("sse_evictions"), 1)
        self.assertEqual(self.stat("sse_write_timeouts"), 4)

    def test_logout_ends_the_stream_within_a_second(self):
        cookie = self.login()[0]

        async def run():
            writer = EventWriter()
            task = self.request(writer, cookie=cookie)
            await asyncio.sleep(0.02)
            self.assertFalse(task.done())
            access.logout(cookie)        # no hub tick: only the session timer notices
            await self.finish(task, 2 * events.SESSION_CHECK_MS / 1000)
            self.assertTrue(bytes(writer.sent).endswith(events.bye_frame("session")))
            self.assertNotIn(b"event: live", bytes(writer.sent))
            self.assert_closed_and_released(writer)
        asyncio.run(run())

    def test_logout_during_a_blocked_write_ends_within_the_write_deadline(self):
        cookie = self.login()[0]

        async def run():
            writer = EventWriter()
            task = self.request(writer, cookie=cookie)
            await asyncio.sleep(0.02)
            writer.block.set()
            self.tick()                  # the live frame now hangs in its drain
            await asyncio.sleep(0.01)
            self.assertIn(b"event: live", bytes(writer.pending))
            sent = bytes(writer.sent)
            access.logout(cookie)
            logged_out = time.monotonic()
            ended = await self.finish(task, events.WRITE_TIMEOUT_SEC + 0.1)
            self.assertLessEqual(ended - logged_out, events.WRITE_TIMEOUT_SEC + 0.1)
            self.assertEqual(bytes(writer.sent), sent)
            self.assertEqual(self.stat("sse_write_timeouts"), 1)
            self.assert_closed_and_released(writer)
        asyncio.run(run())

    def test_stream_survives_stream_token_rotation(self):
        cookie, csrf, _token = self.login()

        async def run():
            writer = EventWriter()
            task = self.request(writer, cookie=cookie)
            await asyncio.sleep(0.02)
            rotation = EventWriter()
            await self.finish(self.request(rotation, "POST", "/api/stream-token/rotate",
                                           cookie=cookie, csrf=csrf), 0.1)
            self.assertEqual(self.status(rotation.sent), 200)
            self.assertEqual(self.payload(rotation.sent), {"stream_token": "f" * 32})
            disconnect.assert_awaited_once()
            self.tick()
            await asyncio.sleep(3 * events.SESSION_CHECK_MS / 1000)   # session checks pass
            self.tick(events.LIVE_INTERVAL_MS)
            await asyncio.sleep(0.02)
            self.assertEqual(bytes(writer.sent).count(b"event: live"), 2)
            self.assertNotIn(b"event: bye", bytes(writer.sent))
            self.assertEqual([sub.bye for sub in self.hub.subs], [None])
            self.assertFalse(task.done())
            await self.end_by_logout(task, cookie)
            self.assert_closed_and_released(writer)
        with mock.patch.object(web, "rotate_stream_token", return_value="f" * 32), \
                mock.patch.object(web, "disconnect_all_clients") as disconnect:
            asyncio.run(run())

    def test_stream_session_check_extends_the_session(self):
        cookie, _csrf, token = self.login()

        async def run():
            writer = EventWriter()
            task = self.request(writer, cookie=cookie)
            await asyncio.sleep(0.01)
            admitted = access._sessions[token]["last"]
            await asyncio.sleep(3 * events.SESSION_CHECK_MS / 1000)
            self.assertGreater(access._sessions[token]["last"], admitted)
            await self.end_by_logout(task, cookie)
            self.assert_closed_and_released(writer)
        asyncio.run(run())

    # --- failures stay inside the event branch ----------------------------------

    def contained_failure(self, writer, method="GET", collect_effect=None):
        """Run one request whose event branch fails; check what every such end shares.

        The writer blocks every drain without a script step, so any drain after
        the failure (e.g. _answer_failure's) would hang the handler.
        """
        cookie = self.login()[0]
        writer.block.set()

        async def run():
            started = time.monotonic()
            ended = await self.finish(self.request(writer, method, cookie=cookie), 0.1)
            return ended - started

        with mock.patch.object(web, "_answer_failure") as answer_failure, \
                mock.patch.object(events.gc, "collect", side_effect=collect_effect) as collect:
            elapsed = asyncio.run(run())
        answer_failure.assert_not_called()
        collect.assert_called_once_with()
        self.assertLess(elapsed, 0.1)
        self.assertEqual(self.stat("sse_stream_errors"), 1)
        self.assert_closed_and_released(writer)

    def test_stream_error_never_reaches_answer_failure(self):
        writer = EventWriter()
        writer.fail_write_at = 2         # the preamble after the header
        self.contained_failure(writer)
        self.assertEqual(writer.drain_calls, 0)
        self.assertEqual(writer.pending_at_close, events.HEADER)

    def test_memory_error_writing_head_never_reaches_answer_failure(self):
        writer = EventWriter()
        writer.fail_write_at = 1
        self.contained_failure(writer, "HEAD")
        self.assertEqual(writer.drain_calls, 0)
        self.assertEqual(writer.pending_at_close, b"")

    def test_memory_error_before_serve_returns_the_reservation(self):
        writer = EventWriter()
        with mock.patch.object(self.hub, "serve", side_effect=MemoryError) as serve:
            self.contained_failure(writer)
        serve.assert_called_once()
        self.assertEqual(writer.drain_calls, 0)
        self.assertEqual(writer.write_calls, 0)

    def test_memory_error_in_the_initial_flush_closes_without_a_drain(self):
        writer = EventWriter()
        writer.drain_script = {1: MemoryError()}
        self.contained_failure(writer)
        self.assertEqual(writer.drain_calls, 1)
        self.assertEqual(writer.pending_at_close, events.HEADER + events.PREAMBLE)

    def test_failure_diagnosis_never_raises(self):
        writer = EventWriter()
        writer.fail_write_at = 1
        with mock.patch.object(events, "log", side_effect=RuntimeError):
            self.contained_failure(writer, "HEAD", collect_effect=RuntimeError)
        self.assertEqual(writer.drain_calls, 0)

    # --- route and dispatcher -----------------------------------------------------

    def test_events_route_is_read_only(self):
        self.assertEqual(web_routing.ROUTES["/api/events"], web_routing._GET)
        self.assertIn("/api/events", web.READ_ONLY_ROUTES)
        cookie, csrf, _token = self.login()

        async def run():
            writer = EventWriter()
            await self.finish(self.request(writer, "POST", cookie=cookie, csrf=csrf), 0.1)
            self.assertEqual(self.status(writer.sent), 405)
            self.assert_closed_and_released(writer)
        asyncio.run(run())

    def test_dispatcher_has_one_events_entry(self):
        source = inspect.getsource(web.handle_http_client)
        self.assertEqual(source.count("_serve_event_stream"), 1)
        self.assertNotIn("events.hub.serve", source)
        self.assertEqual(source.count("_http_connections -= 1"), 1)
        self.assertIn("    finally:\n        if admitted:\n            _http_connections -= 1\n",
                      source)
        self.assertEqual(source.count('"http_connections_total"'), 1)
        self.assertLess(len(source.splitlines()), 300)
        branch = inspect.getsource(web._serve_event_stream)
        self.assertEqual(branch.count("events.hub.serve"), 1)
        self.assertEqual(branch.count("events.hub.release"), 1)


if __name__ == "__main__":
    unittest.main()
