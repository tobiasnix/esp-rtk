# SPDX-License-Identifier: AGPL-3.0-only
"""State package safety net (#20b): bytes of /status and /api/ui/live and the AppState surface.

tests/fixtures/state_bodies_fc83baa.json was written by write_state_fixture() at
fc83baa, before state.py became the package state/. The raw response texts are
compared, not parsed values, so a changed key order or separator fails too. Run
the same fixture before and after every step of the move.
"""
import ast
import asyncio
import contextlib
import importlib
import json
import os
import re
import sys
import time
import unittest
from unittest import mock

import support
from support import REPO, cfg, state, web
import push
import test_live_snapshot as live
from source_checks import production_packages


STATE_FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures",
                             "state_bodies_fc83baa.json")
# boot_id is random per import of cfg; fixed so that two processes produce the same bytes.
FIXED_BOOT_ID = "20b0c0de"
# Member-owning modules of the moved parts, filled by the tasks that move them:
# {module: (class, members, instance fields its _init_<part>() creates)}.
PARTS = {
    "state.receiver": ("ReceiverState",
                       frozenset({"set_fix_listener", "_notify_fix", "last_fix", "update_fix",
                                  "fix_age_ms", "fix_is_stale", "fix_state", "fix_version",
                                  "gga_for_caster", "attach_gst", "_apply_gate", "_awaits_gst",
                                  "_evaluate_gate", "_settle_gate_hold", "reevaluate_gate",
                                  "gate_state", "track_fix_quality", "quality_streaks",
                                  "note_tracking_queue_overflow"}),
                       frozenset({"_last_fix", "_last_update_ms", "_last_fix_status",
                                  "_fix_qual", "_fix_qual_seit", "_fix_qual_changed_ms",
                                  "_last_rtk_fixed_ms", "_gate_accepted", "_gate_changed_ms",
                                  "_gate_quality", "_gate_receiver_accuracy",
                                  "_gate_hold_since_ms", "_fix_ticks_ms", "_fix_expired",
                                  "_fix_seq", "_fix_rev", "_fix_listener",
                                  "_fix_listener_errors"})),
    "state.link": ("LinkState",
                   frozenset({"set_access_state", "set_ntrip_state", "start_receiver_watch",
                              "note_uart_data", "uart_silence_ms", "receiver_link",
                              "check_receiver_link"}),
                   frozenset({"_receiver_watch_ms", "_uart_data_ticks_ms",
                              "_receiver_link_reported", "_receiver_silent_stamp",
                              "_uart_data_since_silent", "wifi_connected_event"})),
    "state.system": ("SystemState",
                     frozenset({"_advance_uptime", "uptime_ms", "uptime_seconds", "beat",
                                "note_loop_lag", "take_loop_lag_window_max", "log_error"}),
                     frozenset({"_uptime_at", "_uptime_s", "_uptime_rem_ms", "clock_steps",
                                "_loop_lag_window_max_ms", "heartbeat", "errors", "identity"})),
    "state.storage": ("StorageState",
                      frozenset({"begin_maintenance", "end_maintenance", "maintenance_reason",
                                 "set_recording_probe", "recording_active"}),
                      frozenset({"_maintenance", "_maintenance_token", "_recording_probe"})),
}
# Members added on top of the fc83baa surface (Task 7 adds note_tracking_queue_overflow).
ADDED_MEMBERS = frozenset({"note_tracking_queue_overflow"})
# Per-part initialisers that replace the single AppState.__init__ body.
INIT_HELPERS = frozenset({"_init_receiver", "_init_link", "_init_system", "_init_storage"})

_INVENTORY_EPOCH = re.compile(rb'"inventory_epoch": "[0-9a-f]{8}"')


@contextlib.contextmanager
def state_body_env():
    """contract_env() of the live contract plus a fixed boot_id and open read access."""
    with live.contract_env(), \
            mock.patch.object(web, "BOOT_ID", FIXED_BOOT_ID), \
            mock.patch.object(cfg, "BOOT_ID", FIXED_BOOT_ID), \
            mock.patch.object(web, "_read_allowed", return_value=True):
        yield


def _raw_body(writer):
    return bytes(writer.buf).split(b"\r\n\r\n", 1)[1]


def _live_raw():
    writer = live.FakeWriter()
    asyncio.run(web._handle_ui_live_request(writer, "", False, {}))
    return _raw_body(writer)


def _status_raw():
    writer = live.FakeWriter()
    asyncio.run(web._handle_status_request(writer, "/status", "", False, {}))
    return _raw_body(writer)


def state_bodies():
    """{"live_loaded", "live_loading", "status"}: the raw response texts, epoch masked.

    inventory_epoch is 8 random hex digits per undo candidate; it is replaced by
    8 asterisks (same length) before the comparison.
    """
    with state_body_env():
        live_loaded = _live_raw()
        with mock.patch.object(web.tracker, "_loaded", False):
            live_loading = _live_raw()
        status = _status_raw()
    bodies = {"live_loaded": live_loaded, "live_loading": live_loading, "status": status}
    return {key: _INVENTORY_EPOCH.sub(b'"inventory_epoch": "********"', raw).decode("utf-8")
            for key, raw in bodies.items()}


def _members():
    """Public and private members of AppState, inherited ones included, without dunders."""
    return {name for name in dir(state.AppState)
            if not name.startswith("__") and not hasattr(object, name)}


def write_state_fixture(path=STATE_FIXTURE):
    """Pins the bytes before the package move (#20b); regenerate only for a deliberate change of the bodies."""
    data = state_bodies()
    data["stats_keys"] = list(state.AppState().stats)
    data["instance_fields"] = sorted(vars(state.AppState()))
    data["members"] = sorted(_members() - INIT_HELPERS)
    data["facade_names"] = sorted(FACADE_NAMES)
    with open(path, "w") as target:
        json.dump(data, target, indent=1, sort_keys=True)
        target.write("\n")


# Every module-level name of state.py at fc83baa (the facade keeps all of them).
FACADE_NAMES = frozenset({
    "AppState", "CONFIG", "GST_GRACE_MS", "HOT_PATH_LOG_INTERVAL_MS", "LogThrottle",
    "NTRIP_STATES", "Queue", "SUPERVISE_MAX_DELAY_SEC", "SUPERVISE_STABLE_SEC",
    "SimpleQueue", "WATCHED_TASKS", "_instances", "_restart_wait", "app", "asyncio",
    "backoff_delay", "clock_is_valid", "corrections_health", "deque", "log",
    "shutdown_event", "stale_heartbeat", "supervise", "time"})


def load_state_fixture(path=STATE_FIXTURE):
    with open(path) as source:
        return json.load(source)


class TestStateSurface(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = load_state_fixture()

    def test_status_and_live_bodies_are_byte_identical(self):
        bodies = state_bodies()
        for key in ("live_loaded", "live_loading", "status"):
            with self.subTest(body=key):
                self.assertEqual(bodies[key], self.fixture[key])

    def test_stats_keys_keep_their_order(self):
        self.assertEqual(list(state.AppState().stats), self.fixture["stats_keys"])

    def test_instance_fields_are_unchanged(self):
        self.assertEqual(sorted(vars(state.AppState())), self.fixture["instance_fields"])

    def test_appstate_members_are_unchanged(self):
        members = _members()
        helpers = members & INIT_HELPERS
        self.assertEqual(helpers, {"_init_" + m.split(".")[1] for m in PARTS})
        self.assertEqual(sorted(members - helpers),
                         sorted(set(self.fixture["members"]) | ADDED_MEMBERS))

    def test_facade_exports_every_name_of_the_module(self):
        for name in self.fixture["facade_names"]:
            with self.subTest(name=name):
                self.assertTrue(hasattr(state, name))
        self.assertIs(state.CONFIG, cfg.CONFIG)
        self.assertIs(state.log, cfg.log)
        self.assertIs(state._instances, web._instances)
        self.assertIs(state.Queue, state.SimpleQueue)


class TestPatchModuleGlobal(unittest.TestCase):
    """support.patch_module_global reaches every module of the state package."""

    @staticmethod
    def _state_modules():
        return [m for k, m in list(sys.modules.items())
                if m is not None and (k == "state" or k.startswith("state."))]

    def test_patch_module_global_reaches_every_state_module(self):
        with support.patch_module_global("state", "log") as spy:
            for m in self._state_modules():
                if "log" in vars(m):
                    with self.subTest(module=m.__name__):
                        self.assertIs(m.log, spy)
        for m in self._state_modules():
            if "log" in vars(m):
                with self.subTest(module=m.__name__, after=True):
                    self.assertIs(m.log, cfg.log)

    def test_patch_module_global_is_undone_by_stop_and_by_stopall(self):
        sentinel = object()
        p = support.patch_module_global("state", "time", sentinel)
        self.assertIs(p.start(), sentinel)
        self.assertIs(state.time, sentinel)
        mock.patch.stopall()
        self.assertIs(state.time, time)
        p.stop()

    def test_no_test_patches_a_state_global_directly(self):
        here = os.path.abspath(__file__)
        pats = [re.compile(r"patch\.object\(\s*(state|app_state)\s*,\s*[\"'](log|time)[\"']"),
                re.compile(r"patch\(\s*[\"']state\.(log|time)[\"']")]
        hits = []
        tests_dir = os.path.dirname(here)
        for name in sorted(os.listdir(tests_dir)):
            path = os.path.join(tests_dir, name)
            if not name.endswith(".py") or os.path.abspath(path) == here:
                continue
            with open(path, encoding="utf-8") as fh:
                for no, line in enumerate(fh, 1):
                    if any(p.search(line) for p in pats):
                        hits.append("%s:%d" % (name, no))
        self.assertEqual(hits, [])


class TestStateParts(unittest.TestCase):
    """AppState is one object made of part classes, each owning its members and fields."""

    def test_parts_own_their_members(self):
        for module_name, (class_name, members, fields) in PARTS.items():
            with self.subTest(part=module_name):
                module = importlib.import_module(module_name)
                cls = getattr(module, class_name)
                init = "_init_" + module_name.split(".")[1]
                self.assertEqual({n for n in vars(cls) if not n.startswith("__")},
                                 set(members) | {init})
                obj = object.__new__(state.AppState)
                getattr(cls, init)(obj)
                self.assertEqual(set(vars(obj)), set(fields))
                self.assertIn(cls, state.AppState.__mro__)

    def test_overflow_counter_has_one_writer_method(self):
        a = state.AppState()
        a.note_tracking_queue_overflow()
        a.note_tracking_queue_overflow()
        self.assertEqual(a.stats["tracking_queue_overflows"], 2)

    def test_no_member_is_defined_twice(self):
        owners = {}
        for cls in state.AppState.__mro__[:-1]:
            for name in vars(cls):
                if not name.startswith("__"):
                    owners.setdefault(name, []).append(cls.__name__)
        self.assertEqual({n: c for n, c in owners.items() if len(c) > 1}, {})

    def test_parts_have_no_init_and_no_slots(self):
        self.assertGreater(len(state.AppState.__bases__), 1)
        for base in state.AppState.__bases__:
            with self.subTest(part=base.__name__):
                self.assertNotIn("__init__", vars(base))
                self.assertNotIn("__slots__", vars(base))
        self.assertNotIn("__slots__", vars(state.AppState))

    def test_parts_import_nothing_from_the_facade(self):
        folder = os.path.join(REPO, "state")
        parts = sorted(n for n in os.listdir(folder) if n.endswith(".py") and n != "__init__.py")
        self.assertTrue(parts)
        for name in parts:
            with open(os.path.join(folder, name), encoding="utf-8") as source:
                tree = ast.parse(source.read())
            facade = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    facade += [a.name for a in node.names
                               if a.name == "state" or a.name.startswith("state.")]
                elif isinstance(node, ast.ImportFrom) and (node.level or node.module == "state"):
                    facade.append("." * node.level + (node.module or ""))
            with self.subTest(module=name):
                self.assertEqual(facade, [])

    def test_facade_class_holds_only_the_composition(self):
        self.assertEqual([b.__name__ for b in state.AppState.__bases__],
                         ["ReceiverState", "LinkState", "SystemState", "StorageState"])
        self.assertEqual({n for n in vars(state.AppState) if not n.startswith("__")}, set())

    def test_shared_objects_are_one_object(self):
        system = importlib.import_module("state.system")
        link = importlib.import_module("state.link")
        receiver = importlib.import_module("state.receiver")
        self.assertEqual(state.GST_GRACE_MS, receiver.GST_GRACE_MS)
        self.assertEqual(state.GST_GRACE_MS, 2000)
        self.assertIs(state.NTRIP_STATES, link.NTRIP_STATES)
        self.assertIs(state.corrections_health, link.corrections_health)
        self.assertIs(state._instances, system._instances)
        self.assertIs(state.clock_is_valid, system.clock_is_valid)


class TestPackageLayout(unittest.TestCase):
    """state is the package state/ and every package module goes to the board."""

    def test_state_is_a_package(self):
        self.assertTrue(hasattr(state, "__path__"))
        self.assertTrue(state.__file__.endswith(os.path.join("state", "__init__.py")),
                        state.__file__)
        self.assertFalse(os.path.exists(os.path.join(REPO, "state.py")))

    def test_every_package_module_is_a_board_file(self):
        packages = production_packages(REPO)
        self.assertIn("state", packages)
        for package in packages:
            for name in sorted(os.listdir(os.path.join(REPO, package))):
                if name.endswith(".py"):
                    with self.subTest(module=package + "/" + name):
                        self.assertIn(package + "/" + name, push.BOARD_FILES)
        for entry in push.BOARD_FILES:
            if "/" in entry:
                with self.subTest(entry=entry):
                    self.assertIn(entry.split("/", 1)[0], packages)


    def test_no_code_points_at_a_finished_issue(self):
        for entry in push.BOARD_FILES:
            if entry.endswith(".py"):
                with self.subTest(file=entry):
                    with open(os.path.join(REPO, entry), encoding="utf-8") as f:
                        self.assertNotIn("#20b", f.read())
        with open(os.path.join(REPO, "tests", "test_stats_writers.py"), encoding="utf-8") as f:
            self.assertNotIn("#20b", f.read())
        with open(os.path.join(REPO, "docs", "technical.md"), encoding="utf-8") as f:
            technical = f.read()
        self.assertNotIn("`state.py`", technical)
        for name in sorted(os.listdir(os.path.join(REPO, "state"))):
            if name.endswith(".py"):
                with self.subTest(doc="state/" + name):
                    self.assertIn("`state/" + name + "`", technical)


if __name__ == "__main__":
    unittest.main()
