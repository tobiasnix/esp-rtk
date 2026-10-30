# SPDX-License-Identifier: AGPL-3.0-only
"""Stakeout session endpoints through the real dispatcher."""
import ast
import asyncio
import builtins
import contextlib
import inspect
import json
import os
import tempfile
import shutil
import textwrap
import unittest
from unittest import mock

import support  # noqa: F401  (installs the MicroPython stubs)
import stakeout
import stakeout_session
import state
import tracking
import web
import web_routing
from support import cfg
from test_access_v11 import FakeHttpWriter, FakeReader
from test_http import StakeoutHttpFixture, _real_open
from test_stakeout_session import (GST_SENTENCE, P2, PACKET_KEYS, assert_packet_keys,
                                   fix_at, north_e8, on_p2)
from wrap_clock import WrapClock


class TestStakeoutHttpFixture(StakeoutHttpFixture, unittest.TestCase):
    def test_put_requests_carry_their_body(self):
        raw = self.raw_request("PUT", "/api/stakeout/settings", b'{"tolerance_m": 0.03}')
        self.assertIn(b"Content-Length: 21\r\n", raw)
        self.assertIn(b"Content-Type: application/json\r\n", raw)
        self.assertNotIn(b"Content-Length", self.raw_request("GET", "/api/stakeout"))


SETTINGS = "/api/stakeout/settings"
FACTORY = {"tolerance_m": 0.1, "fine_radius_m": 5.0}


class TestStakeoutSettingsHttp(StakeoutHttpFixture, unittest.TestCase):
    """GET/PUT /api/stakeout/settings against a private config.json."""

    def setUp(self):
        super().setUp()
        self.cwd = os.getcwd()
        self.config_dir = tempfile.mkdtemp(prefix="stakeout-config-")
        self.addCleanup(shutil.rmtree, self.config_dir, True)
        os.chdir(self.config_dir)
        self.addCleanup(os.chdir, self.cwd)
        for patch in (mock.patch.dict(cfg.CONFIG, {"stakeout_tolerance_m": 0.1,
                                                   "stakeout_fine_radius_m": 5.0,
                                                   "measurement_quality_window_samples": 5}),
                      mock.patch.object(cfg, "_CONFIG_FILE_PROTECTED", False)):
            patch.start()
            self.addCleanup(patch.stop)
        self.clock = WrapClock()
        patch = mock.patch.object(stakeout_session, "time", self.clock)
        patch.start()
        self.addCleanup(patch.stop)

    def put(self, payload):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return self.api("PUT", SETTINGS, body)

    def saved(self):
        with open("config.json") as source:
            return json.load(source)

    def assert_unchanged(self):
        self.assertEqual(self.api("GET", SETTINGS)[1]["values"], FACTORY)
        self.assertFalse(os.path.exists("config.json"))

    def test_settings_get_returns_values_and_defaults(self):
        status, answer = self.api("GET", SETTINGS)
        self.assertEqual(status, 200, answer)
        self.assertEqual(answer, {"values": FACTORY, "defaults": FACTORY})

    def test_settings_put_subset_saves_and_persists(self):
        status, answer = self.put({"tolerance_m": 0.03})
        self.assertEqual(status, 200, answer)
        self.assertEqual(answer["values"], {"tolerance_m": 0.03, "fine_radius_m": 5.0})
        self.assertEqual(answer["defaults"], FACTORY)
        self.assertEqual(self.saved()["stakeout_tolerance_m"], 0.03)
        self.assertEqual(self.api("GET", SETTINGS)[1]["values"]["tolerance_m"], 0.03)
        self.assertEqual(web._stakeout_settings_values()["tolerance_m"], 0.03)
        self.assertEqual(web._stakeout_settings_values(True), FACTORY)

    def test_settings_put_rounds_to_millimetres(self):
        status, answer = self.put({"tolerance_m": "0.0255", "fine_radius_m": 2})
        self.assertEqual(status, 200, answer)
        self.assertEqual(answer["values"], {"tolerance_m": 0.026, "fine_radius_m": 2.0})

    def test_settings_put_rejects_bad_input_and_changes_nothing(self):
        bad = (b"{}", b"[]", b'"x"', b"not json", b"",
               {"tolerance_m": 0.01}, {"tolerance_m": True}, {"radius": 5},
               {"tolerance_m": 6.0},                   # above its range
               {"fine_radius_m": 51}, {"fine_radius_m": 0.5},
               {"tolerance_m": 6.0, "fine_radius_m": 50.0},
               {"tolerance_m": None}, {"tolerance_m": [0.1]},
               {"tolerance_m": 5.0},                   # not below the radius of 5.0
               {"fine_radius_m": 0.05},                # not above the tolerance of 0.1
               {"tolerance_m": 0.5, "fine_radius_m": 0.5})
        for payload in bad:
            with self.subTest(payload=payload):
                status, answer = self.put(payload)
                self.assert_error((status, answer), 400, "invalid_stakeout_settings")
                self.assert_unchanged()

    def test_settings_put_refused_while_config_file_is_protected(self):
        with mock.patch.object(web, "config_file_protected", return_value=True):
            self.assert_error(self.put({"tolerance_m": 0.03}), 503, "config_file_unreadable")
        self.assert_unchanged()

    def test_settings_put_reports_a_failed_save(self):
        with mock.patch.object(web, "apply_live", return_value=False) as apply_live:
            self.assert_error(self.put({"tolerance_m": 0.03}), 500, "save_failed")
        apply_live.assert_called_once_with({"stakeout_tolerance_m": 0.03})
        self.assert_unchanged()

    def test_settings_methods_and_auth(self):
        self.assertEqual(self.call("GET", SETTINGS, csrf=False, cookie=False)[0], 401)
        self.assertEqual(self.call("PUT", SETTINGS, b'{"tolerance_m": 0.03}', csrf=False)[0], 403)
        status, _head, payload = self.call("POST", SETTINGS, b"{}")
        self.assertEqual(status, 405)
        self.assertIn(b"GET or PUT required", payload)
        self.assert_unchanged()

    def test_route_policy(self):
        self.assertEqual(web_routing.ROUTES[SETTINGS],
                         (("GET", "PUT"), "GET or PUT required", 4096))

    def test_settings_change_is_seen_without_a_new_fix(self):
        session = stakeout_session.StakeoutSession()
        session.select("p-0001", 1760000000, dict(P2))
        seq = 0
        for seq in range(1, 6):
            self.clock.advance(1000)
            fix = fix_at(P2["lat_e8"] - north_e8(0.08), P2["lon_e8"],
                         utc="1200%02d.00" % seq)
            session.on_fix(fix, tracking.quality_check(fix), False,
                           self.clock.ticks_ms(), seq, 0)
        now_ms = self.clock.ticks_ms()
        self.assertEqual(session.guidance(now_ms, True, seq, 0)["state"], "ready")
        status, answer = self.put({"tolerance_m": 0.05})
        self.assertEqual(status, 200, answer)
        view = session.guidance(now_ms, True, seq, 0)
        self.assertEqual(view["state"], "fine")
        self.assertEqual(view["tolerance_m"], 0.05)
        self.assertEqual(session.window_fixes(), [])


# --- select_target, set_order and the session in the overview (Task 7) ---

SET_ID = P2["set_id"]
TID = P2["tid"]
TRACKING = "/api/tracking"
# The tracking actions that leave the stakeout session alone (E15); every
# other action of web._dispatch_tracking_request is in _STAKEOUT_RESET_ACTIONS.
KEEP = {"cancel_measurement", "start_line", "add_vertex", "add_asset", "undo",
        "rename_project", "verify_archive", "compact", "pause", "resume", "finish",
        # #39: a frame assignment changes neither the active project nor the survey.
        "frame_assignment_preview", "assign_reference_frame", "revoke_reference_frame"}


def _is_action(node):
    return isinstance(node, ast.Name) and node.id == "action"


def _is_text(node):
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _action_literals(func):
    """The action names that func compares the name ``action`` with.

    Read are ``action == "x"`` (either side) and ``action in`` a literal
    tuple, list or set of strings only. ``action in`` a name or attribute
    reads the strings of the web module tuple of that name (such as
    FRAME_ASSIGNMENT_ACTIONS) and is skipped only when web has no such
    tuple (the samples below). Any other comparison of
    ``action`` with a string literal (!=, not in, is, a mixed set) raises
    AssertionError, so no action can escape the inventory. func may also be
    source text.
    """
    source = func if isinstance(func, str) else inspect.getsource(func)
    found = set()
    for node in ast.walk(ast.parse(textwrap.dedent(source))):
        if not isinstance(node, ast.Compare):
            continue
        operands = [node.left] + node.comparators
        for index, op in enumerate(node.ops):
            left, right = operands[index], operands[index + 1]
            if _is_action(left):
                other = right
            elif _is_action(right):
                other = left
            else:
                continue
            if isinstance(op, ast.Eq) and _is_text(other):
                found.add(other.value)
                continue
            if isinstance(op, ast.In) and other is right:
                if isinstance(other, (ast.Name, ast.Attribute)):
                    name = other.id if isinstance(other, ast.Name) else other.attr
                    group = getattr(web, name, None)
                    if isinstance(group, tuple) and all(isinstance(item, str) for item in group):
                        found.update(group)
                    continue
                if (isinstance(other, (ast.Tuple, ast.List, ast.Set)) and other.elts and
                        all(_is_text(item) for item in other.elts)):
                    found.update(item.value for item in other.elts)
                    continue
            if any(_is_text(item) for item in ast.walk(other)):
                raise AssertionError("unclassified action comparison at line %d"
                                     % node.lineno)
    return found


class TestActionLiterals(unittest.TestCase):
    def test_extractor_reads_equality_and_group_comparisons(self):
        source = """
            def sample(action, other, RESET):
                if action == "a":
                    pass
                elif "b" == action:
                    pass
                elif action in ("c", "d"):
                    pass
                elif action in ["e"]:
                    pass
                elif action in {"f"}:
                    pass
                x = action in RESET
                y = action in web.RESET
                if other == "z":
                    pass
        """
        self.assertEqual(_action_literals(source), {"a", "b", "c", "d", "e", "f"})

    def test_extractor_refuses_unclassified_forms(self):
        for test in ('action != "g"', 'action not in ("h",)', 'action is "i"',
                     'action in ("j", NAME)', '"k" in action'):
            with self.subTest(test=test):
                source = "def sample(action):\n    if %s:\n        pass\n" % test
                with self.assertRaisesRegex(AssertionError,
                                            "unclassified action comparison at line 2"):
                    _action_literals(source)

    def test_extractor_finds_the_grouped_line_actions(self):
        self.assertTrue(_action_literals(web._dispatch_tracking_request) >=
                        {"pause", "resume", "finish", "cancel_measurement", "restore"})

    def test_extractor_reads_named_groups_of_the_web_module(self):
        source = "def sample(action):\n    if action in FRAME_ASSIGNMENT_ACTIONS:\n        pass\n"
        self.assertEqual(_action_literals(source), set(web.FRAME_ASSIGNMENT_ACTIONS))


class SelectFixture(StakeoutHttpFixture):
    """The stakeout HTTP fixture with the sample set imported, a fresh
    stakeout session and the factory stakeout settings."""

    def setUp(self):
        super().setUp()
        self.so = stakeout_session.StakeoutSession()
        for patch in (mock.patch.object(stakeout_session, "session", self.so),
                      mock.patch.dict(cfg.CONFIG, {"stakeout_tolerance_m": 0.1,
                                                   "stakeout_fine_radius_m": 5.0,
                                                   "measurement_quality_window_samples": 5})):
            patch.start()
            self.addCleanup(patch.stop)
        status, answer = self.import_()
        self.assertEqual(status, 200, answer)

    def post(self, payload):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return self.api("POST", "/api/stakeout", body)

    def select(self, tid=TID, **fields):
        fields.update(action="select_target", tid=tid)
        return self.post(fields)

    def order(self, part_id=SET_ID + "/1", reversed=True, **fields):
        fields.update(action="set_order", part_id=part_id, reversed=reversed)
        return self.post(fields)

    def track(self, action, **fields):
        fields["action"] = action
        return self.api("POST", TRACKING, json.dumps(fields).encode())

    def selected(self):
        """Select the sample target P2 and check the answer."""
        status, answer = self.select()
        self.assertEqual(status, 200, answer)
        self.assertEqual(self.so.target["tid"], TID)

    def other_project(self, name="Other"):
        """A second, inactive project; returns its id."""
        project_id = self.tracker.create_project(name)["id"]
        self.tracker.select_project(self.pid)
        return project_id

    def session_view(self, project_id=None):
        status, answer = self.overview(project_id)
        self.assertEqual(status, 200, answer)
        return answer["session"]

    def live(self):
        """(target, guidance state) of the live packet (GET /api/stakeout/live)."""
        packet = web.stakeout_live_snapshot()
        return packet["target"], packet["guidance"]["state"]

    @contextlib.contextmanager
    def no_file_io(self):
        """Every file system call and every store read raises AssertionError."""
        def refuse(*args, **kwargs):
            raise AssertionError("file access")
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(builtins, "open", refuse))
            for name in ("stat", "statvfs", "listdir", "ilistdir"):
                if hasattr(os, name):
                    stack.enter_context(mock.patch.object(os, name, refuse))
            for name in ("find_target", "sets", "send_set"):
                stack.enter_context(mock.patch.object(self.store, name, refuse))
            yield

    def rewrite(self, transform):
        """Replace the set file by transform(its bytes); returns the original bytes."""
        data = self.file_bytes(self.path())
        changed = transform(data)
        self.assertEqual(len(changed), len(data))
        self.assertNotEqual(changed, data)
        with _real_open(self.path(), "wb") as target:
            target.write(changed)
        return data


class TestStakeoutSelectHttp(SelectFixture, unittest.TestCase):
    def test_select_target_answers_the_stored_target(self):
        status, answer = self.select()
        self.assertEqual(status, 200, answer)
        self.assertEqual(answer, {"selected": P2, "changed": True, "remembered": True})
        self.assertEqual((self.so.project_id, self.so.target), (self.pid, P2))
        self.assertEqual(self.so.fingerprint, self.tracker.projects[0]["created_at"])
        # Again (a reloading browser): unchanged, the window stays.
        self.so._window.append(["kept"])
        status, answer = self.select(project_id=self.pid)
        self.assertEqual((status, answer),
                         (200, {"selected": P2, "changed": False, "remembered": True}))
        self.assertEqual(self.so._window, [["kept"]])
        status, answer = self.select(None)
        self.assertEqual((status, answer),
                         (200, {"selected": None, "changed": True, "remembered": True}))
        self.assertIsNone(self.so.target)
        status, answer = self.select(None)
        self.assertEqual((status, answer),
                         (200, {"selected": None, "changed": False, "remembered": True}))

    def test_select_target_refusals(self):
        other = self.other_project()
        self.assert_error(self.select(project_id=other), 409, "project_not_active")
        self.assert_error(self.select(project_id="p-unknown"), 404, "project_not_found")
        self.assert_error(self.select(project_id=5), 400, "invalid_request")
        for tid in ("bad", "so-3a4739b9/1", "so-3a4739b9/01/1", 7, ["x"]):
            with self.subTest(tid=tid):
                self.assert_error(self.select(tid), 400, "invalid_request")
        self.assert_error(self.post({"action": "select_target"}), 400, "invalid_request")
        self.assert_error(self.select("so-3a4739b9/1/99"), 404, "target_not_found")
        self.assert_error(self.select("so-3a4739b9/9/1"), 404, "target_not_found")
        self.assert_error(self.select("so-00000000/1/1"), 404, "set_not_found")
        with mock.patch.object(self.tracker, "_loaded", False):
            self.assert_error(self.select(), 503, "survey_loading")
        self.assertIsNone(self.so.target)
        # A running measurement is allowed (on_fix is passive), a recording line is not.
        with mock.patch.object(web.measurement_session, "active", return_value=True):
            self.selected()
        self.select(None)
        self.tracker.start_line("L", .2)
        self.assertEqual(self.tracker.active_line()["state"], "recording")
        self.assert_error(self.select(), 409, "recording_active")
        self.assert_error(self.select(None), 409, "recording_active")
        self.tracker.set_line_state("pause", None)
        self.selected()
        self.select(None)
        # A damaged set: the first lookup checks the body digest.
        self.cold()
        self.rewrite(lambda data: data.replace(b'"Line A / P001"', b'"Line A / P00X"'))
        self.assert_error(self.select(), 409, "set_damaged", set_id=SET_ID)
        self.assertIsNone(self.so.target)
        self.assert_error(self.select(), 409, "set_damaged")

    def test_unknown_action_still_refused(self):
        self.assert_error(self.post({"action": "confirm_all"}), 400, "unknown_action")
        self.assert_error(self.post({"action": "confirm"}), 400, "invalid_request")
        self.assert_error(self.post({"action": ["select_target"]}), 400, "unknown_action")
        self.assert_error(self.post(b"[]"), 400, "invalid_request")

    def test_set_order_validates_parts_without_reading_the_set(self):
        self.assertEqual(self.overview()[0], 200)
        path = self.path()

        def guarded_open(name, *args, **kwargs):
            if str(name).startswith(path):
                raise AssertionError("set file opened")
            return _real_open(name, *args, **kwargs)

        with mock.patch.object(builtins, "open", guarded_open):
            status, answer = self.order()
            self.assertEqual((status, answer), (200, {"part_id": SET_ID + "/1",
                                                      "reversed": True,
                                                      "remembered": True}))
            self.assertEqual(self.so.orders(), {SET_ID: [1]})
            self.assert_error(self.order(SET_ID + "/9"), 404, "part_not_found")
            self.assert_error(self.order("so-00000000/1"), 404, "part_not_found")
            for part_id in ("bad", SET_ID, SET_ID + "/0", SET_ID + "/1/1", 5, None):
                with self.subTest(part_id=part_id):
                    self.assert_error(self.order(part_id), 400, "invalid_request")
            for value in ("yes", 1, None):
                with self.subTest(reversed=value):
                    self.assert_error(self.order(reversed=value), 400, "invalid_request")
            other = self.other_project()
            self.assert_error(self.order(project_id=other), 409, "project_not_active")
            self.assert_error(self.order(project_id="p-unknown"), 404, "project_not_found")
            # Recording does not block an order: it changes no measurement.
            self.tracker.start_line("L", .2)
            status, answer = self.order(SET_ID + "/2", False)
            self.assertEqual((status, answer), (200, {"part_id": SET_ID + "/2",
                                                      "reversed": False,
                                                      "remembered": True}))
        self.assertEqual(self.so.orders(), {SET_ID: [1]})

    def test_overview_reports_settings_and_session(self):
        self.selected()
        self.assertEqual(self.order()[0], 200)
        status, answer = self.overview()
        self.assertEqual(status, 200, answer)
        self.assertEqual(answer["settings"], {"tolerance_m": 0.1, "fine_radius_m": 5.0})
        session = answer["session"]
        self.assertEqual(session["target"], P2)
        self.assertEqual(session["orders"], {SET_ID: [1]})
        self.assertIsInstance(session["on_fix_us_max"], int)
        self.assertIsInstance(session["fixes_seen"], int)
        other = self.other_project()
        session = self.session_view(other)
        self.assertIsNone(session["target"])
        self.assertEqual(session["orders"], {})
        # Reading another project keeps the session.
        self.assertEqual(self.session_view()["target"], P2)

    def test_deleting_the_set_clears_the_selection(self):
        self.selected()
        self.assertEqual(self.order()[0], 200)
        self.assert_error(self.delete(expected_confirmed=1), 409, "confirmed_changed")
        self.assertEqual(self.session_view()["target"], P2)
        status, answer = self.delete()
        self.assertEqual(status, 200, answer)
        session = self.session_view()
        self.assertIsNone(session["target"])
        self.assertEqual(session["orders"], {})

    def test_partial_delete_still_clears_the_selection(self):
        self.selected()
        self.assertEqual(self.order()[0], 200)
        # A refusal before the removal keeps the selection.
        self.assert_error(self.delete(expected_confirmed=1), 409, "confirmed_changed")
        self.assertEqual(self.so.target["tid"], TID)
        real_remove = os.remove

        def remove(name):
            if str(name).endswith(".json.tmp"):
                raise OSError(5, "io")
            real_remove(name)

        real_present = stakeout._present

        def present(name):
            return str(name).endswith(".json.tmp") or real_present(name)

        with mock.patch.object(stakeout.os, "remove", remove), \
                mock.patch.object(stakeout, "_present", present):
            # The removal is committed in the progress: a .tmp left is a remnant.
            self.assertEqual(self.delete(), (200, {"deleted": SET_ID, "cleanup": "pending"}))
        self.assertFalse(os.path.exists(self.path()))
        self.assertIsNone(self.so.target)
        self.assertEqual(self.so.orders(), {})
        self.assertEqual(self.live(), (None, "no_target"))

    def test_an_unexpected_delete_failure_clears_the_selection(self):
        self.selected()
        with mock.patch.object(self.store, "delete_set", side_effect=MemoryError):
            self.assert_error(self.delete(), 503, "stakeout_busy")
        self.assertIsNone(self.so.target)
        self.selected()
        with mock.patch.object(self.store, "delete_set", side_effect=stakeout.StakeoutError(
                "set_not_found", "No such target set.", "404 Not Found")):
            self.assert_error(self.delete(), 404, "set_not_found")
        self.assertIsNone(self.so.target)
        self.selected()
        with mock.patch.object(self.store, "delete_set", side_effect=stakeout.StakeoutError(
                "storage_busy", "Busy.", "409 Conflict")):
            self.assert_error(self.delete(), 409, "storage_busy")
        self.assertEqual(self.so.target["tid"], TID)

    def test_damage_mark_reaches_the_session(self):
        self.selected()
        self.rewrite(lambda data: data.replace(b'"Line A / P001"', b'"Line A / P00X"'))
        status, _head, _payload = self.read_set()
        self.assertIn(self.path(), self.store._damaged)
        self.assertEqual(self.so.target["tid"], TID)
        with self.no_file_io():
            self.assertEqual(self.live(), (None, "no_target"))
        self.assert_error(self.select(), 409, "set_damaged")
        self.assertIsNone(self.session_view()["target"])

    def test_cached_damage_after_a_cold_listing_clears_the_selection(self):
        def bad_trailer(data):
            return data[:-8] + b"x" * 8

        # Overview first: the check after its cold listing clears the selection.
        self.selected()
        self.assertEqual(self.order()[0], 200)
        original = self.rewrite(bad_trailer)
        self.store.invalidate(self.pid)
        self.assertFalse(self.store._damaged)
        status, answer = self.overview()
        self.assertEqual(status, 200, answer)
        self.assertEqual([item["state"] for item in answer["sets"]], ["damaged"])
        self.assertIsNone(answer["session"]["target"])
        self.assertEqual(answer["session"]["orders"], {})
        with self.no_file_io():
            self.assertEqual(self.live(), (None, "no_target"))
        # Live first: it never lists, so the cold cache keeps the selection
        # until the next overview clears it.
        self.rewrite(lambda data: original)
        self.store.invalidate(self.pid)
        self.selected()
        self.rewrite(bad_trailer)
        self.store.invalidate(self.pid)
        with self.no_file_io():
            target, state = self.live()
        self.assertEqual((target["tid"], state), (TID, "no_position"))
        self.assertIsNone(self.session_view()["target"])
        # set_order after a cold listing of the damaged set: 409, nothing kept.
        self.rewrite(lambda data: original)
        self.store.invalidate(self.pid)
        self.selected()
        self.rewrite(bad_trailer)
        self.store.invalidate(self.pid)
        self.assert_error(self.order(), 409, "set_damaged", set_id=SET_ID)
        self.assertEqual(self.so.orders(), {})
        self.assertIsNone(self.so.target)

    def test_project_switch_or_deletion_clears_the_selection(self):
        other = self.other_project()
        self.selected()
        status, answer = self.track("select_project", project_id=other)
        self.assertEqual(status, 200, answer)
        self.assertIsNone(self.session_view(self.pid)["target"])
        self.assertIsNone(self.so.target)
        self.assertEqual(self.track("select_project", project_id=self.pid)[0], 200)
        self.selected()
        status, answer = self.track("create_project", name="Third")
        self.assertEqual(status, 200, answer)
        self.assertIsNone(self.session_view(self.pid)["target"])
        self.assertEqual(self.track("select_project", project_id=self.pid)[0], 200)
        self.selected()
        self.assertEqual(self.order()[0], 200)
        status, answer = self.track("delete_project", project_id=self.pid)
        self.assertEqual(status, 200, answer)
        self.assertIsNone(self.so.target)
        self.assertEqual(self.so.orders(), {})
        self.assertEqual(self.live(), (None, "no_target"))

    def test_switch_away_and_back_clears_the_selection(self):
        other = self.other_project()
        self.selected()
        epoch = self.so.epoch
        for project_id in (other, self.pid):
            self.assertEqual(self.track("select_project", project_id=project_id)[0], 200)
        self.assertIsNone(self.session_view()["target"])
        self.assertGreaterEqual(self.so.epoch, epoch + 2)
        self.selected()
        epoch = self.so.epoch
        status, answer = self.track("archive_project", project_id=other)
        self.assertEqual(status, 200, answer)
        self.assertEqual(self.tracker.active_project_id, self.pid)
        self.assertIsNone(self.so.target)
        self.assertEqual(self.so.epoch, epoch + 2)
        self.selected()
        status, answer = self.track("reactivate_archive", archive_id=answer["id"])
        self.assertEqual(status, 200, answer)
        self.assertIsNone(self.so.target)

    def test_restore_with_the_same_fingerprint_clears_the_selection(self):
        fingerprint = self.tracker.projects[0]["created_at"]
        backup = self.tracker.backup()
        self.selected()
        status, answer = self.track("restore", backup=backup)
        self.assertEqual(status, 200, answer)
        self.assertEqual((self.tracker.active_project_id,
                          self.tracker.projects[0]["created_at"]), (self.pid, fingerprint))
        self.assertIsNone(self.session_view()["target"])
        # The NDJSON restore.
        self.selected()
        raw = "".join(self.tracker.iter_backup_ndjson()).encode()
        status, _head, payload = self.call("POST", "/api/tracking/import", raw,
                                           content_type="application/x-ndjson")
        self.assertEqual(status, 200, payload)
        self.assertEqual(self.tracker.active_project_id, self.pid)
        self.assertIsNone(self.session_view()["target"])
        # A failed restore clears as well: its outcome is not known.
        self.selected()
        self.assert_error(self.track("restore", backup={"projects": "x"}), 400,
                          "invalid_tracking_request")
        self.assertIsNone(self.so.target)
        self.selected()
        status, _head, payload = self.call("POST", "/api/tracking/import", b"{}\n",
                                           content_type="application/x-ndjson")
        self.assertEqual(status, 400, payload)
        self.assertIsNone(self.so.target)

    def test_every_tracking_action_is_classified(self):
        reset = set(web._STAKEOUT_RESET_ACTIONS)
        self.assertEqual(reset, {"create_project", "select_project", "delete_project",
                                 "archive_project", "reactivate_archive", "restore"})
        self.assertEqual(reset & KEEP, set())
        self.assertEqual(_action_literals(web._dispatch_tracking_request), reset | KEEP)
        # Two of the kept actions through the dispatcher.
        self.selected()
        epoch = self.so.epoch
        status, answer = self.track("rename_project", project_id=self.pid, name="Renamed")
        self.assertEqual(status, 200, answer)
        self.tracker.start_line("L", .2)
        fix = fix_at(P2["lat_e8"], P2["lon_e8"])
        with mock.patch.object(web.measurement_session, "collect",
                               mock.AsyncMock(return_value=fix)):
            status, answer = self.track("add_vertex")
        self.assertEqual(status, 200, answer)
        self.assertEqual((self.so.target["tid"], self.so.epoch), (TID, epoch))
        self.assertEqual(self.session_view()["target"], P2)

    def test_malformed_tracking_requests_answer_400_without_invalidation(self):
        self.selected()
        epoch = self.so.epoch
        headers = {"content-type": "application/json"}
        for body in (b"{", b"\xff", b"[]", b'"restore"', b"5", b"null", b"{}",
                     b'{"action": ["restore"]}', b'{"action": {"restore": 1}}'):
            with self.subTest(body=body):
                status, head, payload = self.call("POST", TRACKING, body)
                self.assertEqual(head.count(b"HTTP/1.0 "), 1, head)
                self.assert_error((status, json.loads(payload)), 400,
                                  "invalid_tracking_request")
                writer = web._HttpAnswer(FakeHttpWriter())
                with mock.patch.object(web, "_admin_allowed", return_value=True):
                    asyncio.run(web._dispatch_tracking_request(
                        writer, TRACKING, "POST", "", body, False, headers))
                self.assertTrue(bytes(writer.writer.data).startswith(
                    b"HTTP/1.0 400 Bad Request\r\n"))
                self.assertEqual(self.so.epoch, epoch)
                self.assertEqual(self.so.target, P2)
        # Counter-check: a reset action clears before and after.
        status, answer = self.track("select_project", project_id=self.pid)
        self.assertEqual(status, 200, answer)
        self.assertEqual(self.so.epoch, epoch + 2)
        self.assertIsNone(self.so.target)

    def test_import_refusals_before_the_restore_keep_the_selection(self):
        self.selected()
        epoch = self.so.epoch
        status, _head, payload = self.call("POST", "/api/tracking/import", b"{}\n")
        self.assertEqual((status, json.loads(payload)["error"]), (415, "ndjson_required"))
        status, _head, payload = self.call("POST", "/api/tracking/import", b"",
                                           content_type="application/x-ndjson")
        self.assertEqual((status, json.loads(payload)["error"]), (400, "empty_backup"))
        self.assertEqual((self.so.epoch, self.so.target), (epoch, P2))


class TestStakeoutSelectRace(SelectFixture, unittest.TestCase):
    """A disturbance through the dispatcher while a target lookup or a cold
    listing waits in its first stakeout pause."""

    async def dispatch(self, path, payload):
        writer = FakeHttpWriter()
        raw = self.raw_request("POST", path, json.dumps(payload).encode())
        await web.handle_http_client(FakeReader(raw), writer)
        head, _, body = bytes(writer.data).partition(b"\r\n\r\n")
        return int(head.split(b" ", 2)[1]), json.loads(body)

    def race(self, request, *disturbances):
        """(answer of request, answers of the disturbances)."""
        real_pause = stakeout._pause

        async def scenario():
            entered, release = asyncio.Event(), asyncio.Event()

            async def pause(meter):
                if not entered.is_set():
                    entered.set()
                    await release.wait()
                await real_pause(meter)

            with mock.patch.object(stakeout, "_pause", pause):
                task = asyncio.ensure_future(self.dispatch("/api/stakeout", request))
                await asyncio.wait_for(entered.wait(), 5)
                answers = []
                for path, payload in disturbances:
                    answers.append(await self.dispatch(path, payload))
                release.set()
                return await asyncio.wait_for(task, 5), answers

        answer, answers = asyncio.run(scenario())
        for status, body in answers:
            self.assertEqual(status, 200, body)
        return answer

    def select_request(self):
        return {"action": "select_target", "tid": TID}

    def test_control_without_disturbance(self):
        self.assertEqual(self.race(self.select_request()),
                         (200, {"selected": P2, "changed": True, "remembered": True}))
        self.store.invalidate(self.pid)
        self.assertEqual(self.race({"action": "set_order", "part_id": SET_ID + "/2",
                                    "reversed": True}),
                         (200, {"part_id": SET_ID + "/2", "reversed": True,
                                "remembered": True}))
        self.assertEqual(self.so.orders(), {SET_ID: [2]})

    def test_project_switch_during_the_scan_is_refused(self):
        other = self.other_project()
        self.assert_error(self.race(self.select_request(),
                                    (TRACKING, {"action": "select_project",
                                                "project_id": other})),
                          409, "project_not_active")
        self.assertIsNone(self.so.target)

    def test_recording_start_during_the_scan_is_refused(self):
        self.assert_error(self.race(self.select_request(),
                                    (TRACKING, {"action": "start_line", "name": "L",
                                                "recording_mode": "route"})),
                          409, "recording_active")
        self.assertEqual(self.tracker.active_line()["state"], "recording")
        self.assertIsNone(self.so.target)

    def test_switch_away_and_back_during_the_scan_is_refused(self):
        other = self.other_project()
        self.assert_error(self.race(self.select_request(),
                                    (TRACKING, {"action": "select_project",
                                                "project_id": other}),
                                    (TRACKING, {"action": "select_project",
                                                "project_id": self.pid})),
                          409, "project_changed")
        self.assertEqual(self.tracker.active_project_id, self.pid)
        self.assertIsNone(self.so.target)

    def test_restore_during_the_scan_is_refused(self):
        backup = self.tracker.backup()
        self.assert_error(self.race(self.select_request(),
                                    (TRACKING, {"action": "restore", "backup": backup})),
                          409, "project_changed")
        self.assertEqual(self.tracker.active_project_id, self.pid)
        self.assertIsNone(self.so.target)

    def test_a_selection_made_during_a_restore_is_cleared_after_it(self):
        backup = self.tracker.backup()
        real_restore = self.tracker.restore_async
        self.selected()
        answers = []

        async def restore(value):
            # The selection made before the restore is gone while it runs.
            self.assertIsNone(self.so.target)
            # A target selection while the restore runs: the restore holds the
            # change lock and the progress is forgotten, so it is refused
            # (stakeout WP4 E21, E26).
            answers.append(await self.dispatch("/api/stakeout", self.select_request()))
            self.assertIsNone(self.so.target)
            return await real_restore(value)

        async def scenario():
            return await self.dispatch(TRACKING, {"action": "restore", "backup": backup})

        with mock.patch.object(self.tracker, "restore_async", restore):
            status, answer = asyncio.run(scenario())
        self.assertEqual(status, 200, answer)
        self.assertEqual([(status, answer["error"]) for status, answer in answers],
                         [(503, "stakeout_busy")])
        self.assertIsNone(self.so.target)

    def test_a_selection_made_during_an_ndjson_restore_is_cleared_after_it(self):
        raw = "".join(self.tracker.iter_backup_ndjson()).encode()
        real_restore = self.tracker.restore_ndjson_async
        self.selected()
        answers = []

        async def restore(lines):
            # The selection made before the restore is gone while it runs.
            self.assertIsNone(self.so.target)
            # Refused as in the JSON restore (stakeout WP4 E21, E26).
            answers.append(await self.dispatch("/api/stakeout", self.select_request()))
            self.assertIsNone(self.so.target)
            return await real_restore(lines)

        with mock.patch.object(self.tracker, "restore_ndjson_async", restore):
            status, _head, payload = self.call("POST", "/api/tracking/import", raw,
                                               content_type="application/x-ndjson")
        self.assertEqual(status, 200, payload)
        self.assertEqual([(status, answer["error"]) for status, answer in answers],
                         [(503, "stakeout_busy")])
        self.assertIsNone(self.so.target)

    def test_damage_marked_during_the_lookup_is_refused(self):
        real_find = self.store.find_target

        async def find_target(*args):
            target = await real_find(*args)
            # Another request (a download) found the set damaged meanwhile.
            self.store._damage(self.pid, self.path(), SET_ID)
            return target

        with mock.patch.object(self.store, "find_target", find_target):
            self.assert_error(self.select(), 409, "set_damaged", set_id=SET_ID)
        self.assertIsNone(self.so.target)

    def test_set_order_after_a_cold_listing_rechecks(self):
        other = self.other_project()
        self.store.invalidate(self.pid)
        self.assert_error(self.race({"action": "set_order", "part_id": SET_ID + "/1",
                                     "reversed": True},
                                    (TRACKING, {"action": "select_project",
                                                "project_id": other})),
                          409, "project_not_active")
        self.assertEqual(self.so.orders(), {})


# --- GET /api/stakeout/live and the confirmation window (Task 8) ---

LIVE = "/api/stakeout/live"
# The other target of the sample set (part 1).
OTHER_TID = SET_ID + "/1/1"


class LiveFixture(SelectFixture):
    """SelectFixture with a fresh AppState as web.app, a test tick clock and
    the stakeout session registered as its fix listener."""

    def setUp(self):
        super().setUp()
        # Start at the real tick reading, so the fixture's login stays valid.
        self.now = time_ticks_ms()
        for patch in (mock.patch.object(state.time, "ticks_ms", lambda: self.now),
                      support.patch_module_global("state", "log"),
                      mock.patch.dict(cfg.CONFIG, {"fix_update_interval_ms": 950,
                                                   "fix_stale_after_ms": 3000}),
                      mock.patch.object(tracking.tracker, "enqueue_fix",
                                        return_value=False)):
            patch.start()
            self.addCleanup(patch.stop)
        self.app = state.AppState()
        self.app.stats["gst_output_state"] = "receiving"
        self.app.set_fix_listener(self.so.on_fix)
        patch = mock.patch.object(web, "app", self.app)
        patch.start()
        self.addCleanup(patch.stop)
        self.gga_ms = self.now

    @staticmethod
    def utc(at_ms):
        return "12%04d.00" % (at_ms // 1000 % 10000)

    def pairs(self, count, north_m=0.0):
        """count GGA+GST pairs north_m north of P2, one per second, each GST
        100 ms after its GGA; the clock stops at the last GST."""
        for _ in range(count):
            self.gga_ms += 1000
            self.now = self.gga_ms
            self.app.update_fix(on_p2(north_m, utc=self.utc(self.gga_ms), gst=False))
            self.now = self.gga_ms + 100
            self.assertTrue(self.app.attach_gst(dict(GST_SENTENCE,
                                                     utc=self.utc(self.gga_ms))))

    def snapshot(self, now_ms=None):
        return web.stakeout_live_snapshot(self.now if now_ms is None else now_ms)

    def state_now(self):
        return self.snapshot()["guidance"]["state"]

    def confirm(self, tid=TID, now_ms=None):
        return web._stakeout_confirm_window(tid, self.now if now_ms is None else now_ms)

    def ready(self, north_m=0.0):
        """Select P2 (if not selected) and reach ready with five pairs."""
        if self.so.target is None:
            self.selected()
        self.pairs(5, north_m)
        self.assertEqual(self.state_now(), "ready")


def time_ticks_ms():
    return state.time.ticks_ms()


class TestStakeoutLiveHttp(LiveFixture, unittest.TestCase):
    def test_route_policy(self):
        self.assertEqual(web_routing.ROUTES[LIVE], (("GET", "HEAD"), "GET required", 4096))

    def test_live_shares_the_ui_snapshot_sources(self):
        self.selected()
        self.pairs(3, 0.02)
        # A bare GGA whose GST is still on its way: the held verdict and accuracy.
        self.gga_ms += 1000
        self.now = self.gga_ms + 40
        self.app.update_fix(on_p2(0.02, utc=self.utc(self.gga_ms), gst=False))
        live = self.snapshot()
        self.assertTrue(self.app.gate_state(self.now)["pending_gst"])
        self.assertEqual(live["guidance"]["reasons"], ["gst_pending"])
        self.assertEqual((live["position"]["gst_h_sigma_m"], live["position"]["gst_v_sigma_m"]),
                         (0.01, 0.02))
        self.assertEqual(live["target"], {"tid": TID, "lat_e8": P2["lat_e8"],
                                          "lon_e8": P2["lon_e8"], "status": "open"})
        # The endpoint serves the same packet.
        status, answer = self.api("GET", LIVE)
        self.assertEqual(status, 200, answer)
        self.assertLessEqual(abs(answer.pop("device_time") - live.pop("device_time")), 1)
        self.assertEqual(answer, live)
        # Fresh, then stale (a stale reading marks the fix expired for good).
        for now_ms in (self.now, self.now + 5000):
            with self.subTest(now_ms=now_ms):
                live = self.snapshot(now_ms)
                ui = web.ui_live_snapshot(now_ms)
                fix, quality = ui["current_fix"], ui["current_quality"]
                for key in ("fix_seq", "fix_rev", "fix_age_ms", "stale"):
                    self.assertEqual(live[key], fix[key], key)
                for key in ("lat_e8", "lon_e8", "qual"):
                    self.assertEqual(live["position"][key], fix[key], key)
                self.assertEqual(live["gate"]["accepted"], quality["accepted"])
                display = ("fix_stale", "corrections_missing")
                self.assertEqual([reason for reason in live["gate"]["reasons"]
                                  if reason not in display], quality["reasons"])
                self.assertEqual("fix_stale" in live["gate"]["reasons"], fix["stale"])
                self.assertEqual(live["corrections"]["link"], ui["link"]["corrections"])
                self.assertEqual(live["corrections"]["rtcm_age_sec"],
                                 ui["rtcm"].get("epoch_age_sec"))
                self.assertEqual(live["corrections"]["ntrip_state"],
                                 ui["network"]["ntrip_state"])
                self.assertEqual(live["header"]["access_state"],
                                 ui["network"]["access_state"])
                self.assertLessEqual(abs(live["device_time"] - ui["device_time"]), 1)
        self.assertEqual(live["guidance"]["state"], "no_position")

    def test_live_with_a_rtcm_summary(self):
        ntrip = mock.Mock()
        ntrip.rtcm.summary.return_value = {"epoch_age_sec": 0.8, "stale": False}
        ntrip.rtcm.silence_ms.return_value = 200
        self.app.stats["ntrip_state"] = "streaming"
        with mock.patch.dict(state._instances, {"ntrip": ntrip}):
            live = self.snapshot()
            ui = web.ui_live_snapshot(self.now)
        self.assertEqual(live["corrections"], {"ntrip_state": "streaming",
                                               "rtcm_age_sec": 0.8, "link": "ok"})
        self.assertEqual(live["corrections"]["link"], ui["link"]["corrections"])
        self.assertNotIn("corrections_missing", live["gate"]["reasons"])

    def test_live_without_target_and_without_fix(self):
        status, answer = self.api("GET", LIVE)
        self.assertEqual(status, 200, answer)
        assert_packet_keys(self, answer, False, False)
        self.assertEqual(set(answer), PACKET_KEYS)
        self.assertEqual(answer["guidance"]["state"], "no_target")
        self.assertEqual((answer["fix_seq"], answer["fix_rev"], answer["fix_age_ms"],
                          answer["stale"]), (0, 0, None, False))
        self.assertIsInstance(answer["device_time"], int)
        self.assertEqual(answer["schema"], 1)

    def test_live_does_no_file_io(self):
        self.ready()
        with self.no_file_io():
            status, answer = self.api("GET", LIVE)
            self.tracker._storage_sample = None
            packet = self.snapshot()
        self.assertEqual(status, 200, answer)
        self.assertEqual(answer["guidance"]["state"], "ready")
        self.assertEqual((answer["target"]["lat_e8"], answer["target"]["lon_e8"]),
                         (P2["lat_e8"], P2["lon_e8"]))
        self.assertEqual(packet["guidance"]["state"], "ready")
        self.assertIsNone(packet["header"]["storage_level"])

    def test_live_header_never_samples_the_flash(self):
        self.tracker._storage_sample = None
        with mock.patch.object(os, "statvfs", side_effect=AssertionError("statvfs")):
            status, answer = self.api("GET", LIVE)
        self.assertEqual(status, 200, answer)
        self.assertIsNone(answer["header"]["storage_level"])
        self.assertIsNone(self.tracker._storage_sample)
        self.tracker.storage_capacity(force=True)
        expected = self.tracker.capacity()["level"]
        with mock.patch.object(os, "statvfs", side_effect=AssertionError("statvfs")) as statvfs:
            status, answer = self.api("GET", LIVE)
            self.assertEqual(web._stakeout_storage_level(), expected)
        self.assertEqual(status, 200, answer)
        self.assertEqual(answer["header"]["storage_level"], expected)
        statvfs.assert_not_called()
        with mock.patch.object(self.tracker, "_loaded", False):
            self.assertIsNone(web._stakeout_storage_level())

    def test_live_after_listener_failure_is_never_ready(self):
        self.ready()
        self.gga_ms += 1000
        self.now = self.gga_ms
        with mock.patch.object(stakeout_session.geo, "offsets_e8_m",
                               side_effect=RuntimeError("injected")) as patched:
            self.app.update_fix(on_p2(utc=self.utc(self.gga_ms)))
        self.assertTrue(patched.called)
        status, answer = self.api("GET", LIVE)
        self.assertEqual(status, 200, answer)
        self.assertEqual(answer["guidance"]["state"], "no_position")
        self.assertIn("session_error", answer["guidance"]["reasons"])
        self.assertIsNone(answer["guidance"]["dist_m"])
        self.assertIsNone(self.confirm())

    def test_live_after_project_switch_is_no_target(self):
        self.ready()
        other = self.other_project()
        # Directly in the tracker: only the live packet's own session check sees it.
        self.tracker.select_project(other)
        self.assertEqual(self.so.target["tid"], TID)
        packet = self.snapshot()
        self.assertIsNone(packet["target"])
        self.assertEqual(packet["guidance"]["state"], "no_target")
        self.assertIsNone(self.so.target)

    def test_live_reports_recording(self):
        self.ready()
        self.assertNotIn("recording_active", self.snapshot()["guidance"]["reasons"])
        self.tracker.start_line("L", .2)
        self.assertEqual(self.tracker.active_line()["state"], "recording")
        status, answer = self.api("GET", LIVE)
        self.assertEqual(status, 200, answer)
        self.assertEqual(answer["guidance"]["state"], "ready")
        self.assertEqual(answer["guidance"]["reasons"], ["recording_active"])
        self.tracker.set_line_state("pause", None)
        self.assertEqual(self.snapshot()["guidance"]["reasons"], [])

    def test_live_auth_and_head(self):
        status, _head, payload = self.call("GET", LIVE, csrf=False, cookie=False)
        self.assertEqual(status, 401, payload)
        status, head, payload = self.call("HEAD", LIVE)
        self.assertEqual((status, payload), (200, b""))
        self.assertIn(b"application/json", head)
        status, _head, payload = self.call("POST", LIVE, b"{}")
        self.assertEqual(status, 405)
        self.assertIn(b"GET required", payload)


class TestStakeoutConfirmWindowHttp(LiveFixture, unittest.TestCase):
    def test_confirm_window_agrees_with_the_live_packet(self):
        self.ready()
        window = self.confirm()
        self.assertEqual(len(window), 5)
        self.assertEqual(window, self.so.window_fixes())
        self.assertEqual(self.snapshot()["guidance"]["state"], "ready")
        later = self.now + cfg.CONFIG["fix_stale_after_ms"]
        self.assertIsNone(self.confirm(now_ms=later))
        self.assertEqual(self.snapshot(later)["guidance"]["state"], "no_position")
        self.assertIsNone(self.confirm(OTHER_TID))

    def test_confirm_window_after_a_config_change_and_new_ready(self):
        cwd = os.getcwd()
        folder = tempfile.mkdtemp(prefix="stakeout-config-")
        self.addCleanup(shutil.rmtree, folder, True)
        os.chdir(folder)
        self.addCleanup(os.chdir, cwd)
        patch = mock.patch.object(cfg, "_CONFIG_FILE_PROTECTED", False)
        patch.start()
        self.addCleanup(patch.stop)
        self.ready(0.08)
        before = self.confirm()
        self.assertEqual(len(before), 5)
        status, answer = self.api("PUT", SETTINGS, b'{"tolerance_m": 0.02}')
        self.assertEqual(status, 200, answer)
        self.assertIsNone(self.confirm())
        self.assertEqual(self.state_now(), "fine")
        self.pairs(4)
        self.assertIsNone(self.confirm())
        self.pairs(1)
        after = self.confirm()
        self.assertEqual(len(after), 5)
        self.assertEqual(self.state_now(), "ready")
        self.assertTrue({id(fix) for fix in after}.isdisjoint({id(fix) for fix in before}))

    def test_confirm_window_after_a_target_or_project_change(self):
        self.ready()
        self.assertEqual(len(self.confirm()), 5)
        status, answer = self.select(OTHER_TID)
        self.assertEqual((status, answer["changed"]), (200, True), answer)
        self.assertIsNone(self.confirm())
        status, answer = self.select()
        self.assertEqual((status, answer["changed"]), (200, True), answer)
        self.assertIsNone(self.confirm())
        self.pairs(4)
        self.assertIsNone(self.confirm())
        self.pairs(1)
        self.assertEqual(len(self.confirm()), 5)
        # A switch to another project and back clears the session.
        other = self.other_project()
        for project_id in (other, self.pid):
            self.assertEqual(self.track("select_project", project_id=project_id)[0], 200)
        self.assertIsNone(self.confirm())
        self.assertIsNone(self.so.target)
        # A set found damaged (a download with a changed byte) clears it as well.
        self.ready()
        self.assertEqual(len(self.confirm()), 5)
        self.rewrite(lambda data: data.replace(b'"Line A / P001"', b'"Line A / P00X"'))
        self.read_set()
        self.assertIn(self.path(), self.store._damaged)
        self.assertIsNone(self.confirm())
        self.assertIsNone(self.so.target)

    def test_confirm_window_is_synchronous_and_does_no_file_io(self):
        self.assertIs(inspect.iscoroutinefunction(web._stakeout_confirm_window), False)
        self.ready()
        with self.no_file_io():
            window = self.confirm()
        self.assertIsInstance(window, list)
        self.assertEqual(len(window), 5)


def high_sequence_package(project_id="project-00000001-0001"):
    """A package with one thinned line of 5000 vertices (three stored) whose
    targets sit at coord_index 0, 2500 and 4999, so their sequences are
    1, 2501 and 5000."""
    coords = [[1000000000, 5100000000, None], [1000100000, 5100100000, None],
              [1000200000, 5100200000, None]]
    rows = [{"part_index": 0, "coord_index": index, "name": "High %d" % (number + 1),
             "lat_e8": 5100000000 + 1000 * number, "lon_e8": 1000000000 + 1000 * number,
             "alt_import": None, "props": {}}
            for number, index in enumerate((0, 2500, 4999))]
    return {"schema": 1, "project_id": project_id,
            "source": {"filename": "high.geojson", "bytes": 0, "features": 1,
                       "skipped": [], "skipped_total": 0},
            "parts": [{"kind": "line", "name": "Long line", "feature_index": 0,
                       "geometry_index": 0, "coords_total": 5000,
                       "coords_thinned": True, "coords_e8": coords}],
            "targets": rows}


class TestHighSequenceTarget(LiveFixture, unittest.TestCase):
    def test_thinned_line_target_beyond_2000_is_selectable(self):
        status, answer = self.import_(high_sequence_package())
        self.assertEqual(status, 200, answer)
        set_id = answer["set"]["set_id"] if "set" in answer else answer["set_id"]
        tids = ["%s/1/%d" % (set_id, seq) for seq in (1, 2501, 5000)]
        status, listing = self.api("GET", "/api/stakeout?project_id=%s&set_id=%s"
                                   % (self.pid, set_id))
        self.assertEqual(status, 200, listing)
        self.assertEqual([target["tid"] for target in listing["targets"]], tids)
        status, answer = self.select(tids[2])
        self.assertEqual(status, 200, answer)
        self.assertEqual(answer["selected"]["tid"], tids[2])
        self.assertEqual(web.stakeout_live_snapshot()["target"]["tid"], tids[2])

    def test_target_beyond_2000_confirms_and_survives_a_reload(self):
        status, answer = self.import_(high_sequence_package())
        self.assertEqual(status, 200, answer)
        tid = "%s/1/5000" % answer["set_id"]
        self.assertEqual(self.select(tid)[0], 200)
        target = self.so.target
        for _ in range(5):
            self.gga_ms += 1000
            self.now = self.gga_ms
            self.app.update_fix(fix_at(target["lat_e8"], target["lon_e8"],
                                       utc=self.utc(self.gga_ms), gst=False))
            self.now = self.gga_ms + 100
            self.assertTrue(self.app.attach_gst(dict(GST_SENTENCE,
                                                     utc=self.utc(self.gga_ms))))
        self.assertEqual(self.state_now(), "ready")
        status, answer = self.post({"action": "confirm", "mode": "verified", "tid": tid,
                                    "cmd": "0123456789abcdef", "expect_gen": 0})
        self.assertEqual((status, answer.get("status")), (200, "confirmed"), answer)
        # A restart: new tracker, store, progress log and session over the same files.
        old = self.tracker
        old._sync_journal(close=True)
        old.point_store.close()
        self.tracker = web.tracker = tracking.Tracker(old.path)
        self.addCleanup(self.tracker.point_store.close)
        self.addCleanup(self.tracker._sync_journal, True)
        self.use_store(stakeout.TargetStore(self.root))
        stakeout_session.session = stakeout_session.StakeoutSession()
        status, listing = self.api("GET", "/api/stakeout/progress")
        self.assertEqual(status, 200, listing)
        self.assertIn([tid, "confirmed", 0, "verified", answer["point_id"]], listing["states"])

    def test_target_beyond_2000_exports(self):
        status, answer = self.import_(high_sequence_package())
        self.assertEqual(status, 200, answer)
        set_id = answer["set_id"]
        tid = "%s/1/5000" % set_id
        self.assertEqual(self.select(tid)[0], 200)
        target = self.so.target
        for _ in range(5):
            self.gga_ms += 1000
            self.now = self.gga_ms
            self.app.update_fix(fix_at(target["lat_e8"], target["lon_e8"],
                                       utc=self.utc(self.gga_ms), gst=False))
            self.now = self.gga_ms + 100
            self.assertTrue(self.app.attach_gst(dict(GST_SENTENCE,
                                                     utc=self.utc(self.gga_ms))))
        status, confirmed = self.post({"action": "confirm", "mode": "verified", "tid": tid,
                                       "cmd": "0123456789abcdef", "expect_gen": 0})
        self.assertEqual(status, 200, confirmed)
        status, doc = self.api("GET", "/api/stakeout/export?set_id=" + set_id)
        self.assertEqual(status, 200, doc)
        features = dict((feature["id"], feature) for feature in doc["features"])
        self.assertEqual(sorted(features), sorted("%s/1/%d" % (set_id, seq)
                                                  for seq in (1, 2501, 5000)))
        properties = features[tid]["properties"]
        self.assertEqual((properties["seq"], properties["status"], properties["gen"],
                          properties["observation"]["point_id"]),
                         (5000, "confirmed", 0, confirmed["point_id"]))


if __name__ == "__main__":
    unittest.main()
