# SPDX-License-Identifier: AGPL-3.0-only
"""Stakeout WP4 through the real dispatcher: the progress log wired into the
stakeout routes, the live packet, set removal, project deletion and archives."""
import ast
import asyncio
import contextlib
import inspect
import json
import os
import time
import unittest
from unittest import mock

import support  # noqa: F401  (installs the MicroPython stubs)
import measurement
import stakeout
import stakeout_progress
import stakeout_session
import tracking
import web
import web_routing
from support import cfg
from test_access_v11 import FakeHttpWriter, FakeReader
from test_http import _real_open
from test_stakeout_session import (LONG_FLOAT, P1, P2, STATS, device_bytes, fix_at,
                                   on_p2)
from test_stakeout_session_http import (OTHER_TID, SET_ID, TID, TRACKING, LiveFixture,
                                        SelectFixture)
from test_stakeout_store import compact, make_package
from test_tracking import FIX

CMD = "0123456789abcdef"
PROGRESS = "/api/stakeout/progress"
ZERO = {"confirmed": 0, "verified": 0, "manual": 0, "skipped": 0}


class PowerCut(BaseException):
    """The power fails: no handler catches it, nothing after it runs."""


class ProgressFixture(SelectFixture):
    """SelectFixture (the sample set imported into the active project) with
    helpers for the progress log of its own store (self.progress)."""

    def fp(self, project_id=None):
        project_id = project_id or self.pid
        for project in self.tracker.projects:
            if project["id"] == project_id:
                return project.get("created_at")

    def progress_path(self, project_id=None):
        return os.path.join(self.root, project_id or self.pid, "progress.jsonl")

    def events(self, project_id=None):
        """The events of the progress file (header dropped), [] without a file."""
        try:
            data = self.file_bytes(self.progress_path(project_id))
        except FileNotFoundError:
            return []
        return [json.loads(line) for line in data.decode().splitlines()[1:]]

    def fold(self, project_id=None):
        project_id = project_id or self.pid
        return self.progress.peek(project_id, self.fp(project_id))

    def append(self, event, project_id=None):
        """Append event to the progress of project_id under the change lock."""
        project_id = project_id or self.pid

        async def run():
            async with self.store.mutation():
                fold = await self.progress.load(project_id, self.fp(project_id),
                                                project_id == self.tracker.active_project_id)
                self.progress.append(fold, event)

        asyncio.run(run())

    def stakeout_point(self, tid, gen=0, mode="manual"):
        """A stakeout point of the active project (as a confirmation stores it)."""
        return self.tracker.add_asset("stakeout", "Target", "", dict(FIX), False, stakeout={
            "tid": tid, "gen": gen, "cmd": CMD, "mode": mode, "tolerance_m": None,
            "dn_m": 0.0, "de_m": 0.0, "dist_m": 0.0})

    def progress_list(self, project_id=None):
        query = "" if project_id is None else "?project_id=" + project_id
        return self.api("GET", PROGRESS + query)

    def restart(self):
        """A new store, progress log and session over the same files."""
        self.use_store(stakeout.TargetStore(self.root))
        self.so = stakeout_session.session = stakeout_session.StakeoutSession()

    def imported_at(self, set_id=SET_ID):
        status, answer = self.overview()
        self.assertEqual(status, 200, answer)
        for summary in answer["sets"]:
            if summary["set_id"] == set_id:
                return summary["imported_at"]

    def logged(self, module, level):
        return [call.args for call in module.log.call_args_list if call.args[0] == level]

    def second_set(self):
        """Import a second set of three targets into the active project; its set_id."""
        status, answer = self.import_(make_package(targets=3, project_id=self.pid))
        self.assertEqual(status, 200, answer)
        return answer["set_id"]

    def import_other(self, project_id, **shape):
        """Import a set into project_id (which need not be active); its answer."""
        package = make_package(project_id=project_id, **(shape or {"targets": 2}))
        return self.api("POST", "/api/stakeout/import", compact(package))


class _Opener:
    """open() for stakeout_progress whose append handle raises at stage."""

    def __init__(self, stage):
        self.stage = stage

    def __call__(self, name, mode="r", *args):
        handle = _real_open(name, mode, *args)
        if "a" not in mode:
            return handle
        stage = self.stage

        class Handle:
            def write(self, data):
                if stage == "write":
                    raise OSError(5, "write")
                return handle.write(data)

            def flush(self):
                handle.flush()
                if stage == "flush":
                    raise OSError(5, "flush")

            def close(self):
                handle.close()
                if stage == "close":
                    raise OSError(5, "close")

        return Handle()


def failing_append(stage):
    return mock.patch("stakeout_progress.open", _Opener(stage), create=True)


class TestProgressWiring(ProgressFixture, unittest.TestCase):
    def test_reset_actions_forget_the_fold(self):
        other = self.other_project()
        doomed = self.other_project("Doomed")
        archived = self.other_project("Archived")
        fields = {"create_project": {"name": "New"},
                  "select_project": {"project_id": self.pid},
                  "delete_project": {"project_id": doomed},
                  "archive_project": {"project_id": archived},
                  "reactivate_archive": {"archive_id": "archive-missing"},
                  "restore": {"backup": {"projects": "x"}}}
        self.assertEqual(set(fields), set(web._STAKEOUT_RESET_ACTIONS))
        raw = "".join(self.tracker.iter_backup_ndjson()).encode()
        for action in web._STAKEOUT_RESET_ACTIONS + ("ndjson",):
            with self.subTest(action=action):
                if self.tracker.active_project_id != self.pid:
                    self.tracker.select_project(self.pid)
                self.selected()
                self.assertIsNotNone(self.fold())
                # A survey restore is refused under the latch (E25): none for it.
                kept = None if action == "ndjson" else "kept"
                self.store.uncertain = kept
                if action == "ndjson":
                    status, _head, payload = self.call(
                        "POST", "/api/tracking/import", raw,
                        content_type="application/x-ndjson")
                    self.assertEqual(status, 200, payload)
                else:
                    self.track(action, **fields[action])
                self.assertIsNone(self.fold())
                self.assertIsNone(self.so.target)
                self.assertEqual(self.store.uncertain, kept)
                self.store.uncertain = None
        # Actions that keep the project keep the fold.
        self.tracker.select_project(self.pid)
        self.selected()
        fold = self.fold()
        for action, extra in (("cancel_measurement", {}),
                              ("rename_project", {"project_id": other, "name": "Renamed"})):
            with self.subTest(action=action):
                status, answer = self.track(action, **extra)
                self.assertEqual(status, 200, answer)
                self.assertIs(self.fold(), fold)
                self.assertEqual(self.so.target["tid"], TID)


class TestRouteLoadsProgressFirst(ProgressFixture, unittest.TestCase):
    def test_every_listing_route_loads_progress_first(self):
        second = self.second_set()
        # A removal marker of a set that never existed: after a load of the
        # progress the store knows the project's markers.
        self.append(stakeout_progress.set_removed_event("so-00000000", None, 1))
        routes = (("overview", self.overview),
                  ("set file", self.read_set),
                  ("select_target", self.select),
                  ("set_order", self.order),
                  ("delete_set", lambda: self.delete(second)),
                  ("import", lambda: self.import_(make_package(targets=2, offset=77))),
                  ("progress", self.progress_list),
                  ("export", lambda: self.call("GET", "/api/stakeout/export")),
                  ("csv export", lambda: self.call("GET", "/api/stakeout/export?format=csv")))
        for name, call in routes:
            with self.subTest(route=name):
                self.restart()
                store = self.store
                real = store._load
                seen = []

                async def checked(project_id, meter, real=real, store=store, seen=seen):
                    seen.append(project_id in store.removed)
                    return await real(project_id, meter)

                with mock.patch.object(store, "_load", checked):
                    response = call()
                self.assertEqual(response[0], 200, response)
                self.assertTrue(seen)
                self.assertTrue(all(seen), seen)

    def test_progress_actions_load_progress_first(self):
        self.append(stakeout_progress.set_removed_event("so-00000000", None, 1))
        fresh = (dict(FIX, stale=False), None)

        def action(action, tid, **fields):
            fields.update(action=action, tid=tid, cmd=CMD, expect_gen=0)
            return lambda: self.post(fields)

        # A confirmation in mode verified lists nothing (session.target).
        for name, call in (("skip", action("skip", P1["tid"])),
                           ("reopen", action("reopen", P1["tid"])),
                           ("confirm", action("confirm", TID, mode="manual"))):
            with self.subTest(route=name):
                self.restart()
                store = self.store
                real = store._load
                seen = []

                async def checked(project_id, meter, real=real, store=store, seen=seen):
                    seen.append(project_id in store.removed)
                    return await real(project_id, meter)

                with mock.patch.object(store, "_load", checked), \
                        mock.patch.object(web, "current_fix_view", return_value=fresh):
                    response = call()
                self.assertEqual(response[0], 200, response)
                self.assertTrue(seen)
                self.assertTrue(all(seen), seen)


class TestLiveStatus(ProgressFixture, unittest.TestCase):
    def test_status_comes_from_the_fold(self):
        self.selected()
        self.assertEqual(web.stakeout_live_snapshot()["target"]["status"], "open")
        self.append(stakeout_progress.skip_event(TID, 0, CMD))
        target = web.stakeout_live_snapshot()["target"]
        self.assertEqual(target["status"], "skipped")
        self.assertNotIn("gen", target)
        self.append(stakeout_progress.reopen_event(TID, 1, CMD))
        self.assertEqual(web.stakeout_live_snapshot()["target"]["status"], "open")
        self.stakeout_point(TID, 1)
        self.assertEqual(web.stakeout_live_snapshot()["target"]["status"], "confirmed")

    def test_cold_fold_is_unknown_not_open(self):
        self.selected()
        self.progress.forget(self.pid)
        self.assertEqual(web.stakeout_live_snapshot()["target"]["status"], "unknown")
        self.assertEqual(self.overview()[0], 200)
        self.assertEqual(web.stakeout_live_snapshot()["target"]["status"], "open")

    def test_two_projects_two_browsers(self):
        other = self.other_project()
        self.assertEqual(self.import_other(other)[0], 200)
        self.selected()
        self.append(stakeout_progress.skip_event(TID, 0, CMD))
        fold = self.fold()
        for call in (lambda: self.overview(other), lambda: self.progress_list(other)):
            status, answer = call()
            self.assertEqual(status, 200, answer)
            self.assertEqual(answer["project_id"], other)
            self.assertEqual(web.stakeout_live_snapshot()["target"]["status"], "skipped")
            self.assertIs(self.fold(), fold)
            self.assertIsNone(self.fold(other))


class TestLiveSize(LiveFixture, unittest.TestCase):
    def test_live_does_no_file_io_with_a_fold(self):
        self.ready()
        with self.no_file_io():
            status, answer = self.api("GET", "/api/stakeout/live")
        self.assertEqual(status, 200, answer)
        self.assertEqual(answer["target"]["status"], "open")
        self.assertEqual(answer["guidance"]["state"], "ready")

    def test_size_budget_in_device_bytes(self):
        so = stakeout_session.StakeoutSession()
        # The WP2 worst case: every gate reason, the longest session reasons,
        # extreme numbers and the longest tid; then the write_uncertain reason.
        so.select(self.pid, 1, dict(P2, tid="so-ffffffff/500/1000000",
                                    lat_e8=-9000000000, lon_e8=-18000000000))
        fix = {"lat_e8": -9000000000, "lon_e8": -18000000000, "alt_mm": -2147483648,
               "qual": 2, "fix_status_text": "GPS_FIX_2D/3D", "sats": 99,
               "hdop": LONG_FLOAT, "correction_age_sec": LONG_FLOAT,
               "receiver_accuracy": {"horizontal_sigma_m": LONG_FLOAT,
                                     "altitude_sigma_m": LONG_FLOAT}}
        view = dict(fix, fix_seq=2 ** 30, fix_rev=2 ** 30, fix_age_ms=2 ** 29, stale=True,
                    pending_gst=False)
        gate_reasons = ["no_position", "required_fix_not_met", "too_few_satellites",
                        "hdop_too_high", "altitude_required", "corrections_too_old",
                        "gst_horizontal_error_too_high", "gst_vertical_error_too_high"]
        guidance = {"state": "no_position", "dn_m": LONG_FLOAT, "de_m": LONG_FLOAT,
                    "dist_m": LONG_FLOAT, "bearing_deg": LONG_FLOAT,
                    "tolerance_m": LONG_FLOAT, "fine_radius_m": LONG_FLOAT,
                    "window": {"have": 30, "need": 30, "spread_m": LONG_FLOAT},
                    "reasons": ["horizontal_spread_too_high"]}
        motion = {"speed_mps": LONG_FLOAT, "course_deg": LONG_FLOAT, "valid": False}

        def worst(*args):
            with mock.patch.object(so, "guidance", return_value=dict(
                    guidance, reasons=list(guidance["reasons"]))), \
                    mock.patch.object(so, "motion", return_value=motion):
                return so.live_packet(
                    1000, (view, {"accepted": False, "reasons": list(gate_reasons)}),
                    {"epoch_age_sec": LONG_FLOAT, "stale": True}, {"corrections": "stale"},
                    {"access_state": "CONNECTING", "ntrip_state": "waiting_wifi"},
                    "critical", True, 2 ** 31 - 1)

        # The example values of spec section 8 (WP2: at most 900 bytes).
        example = stakeout_session.StakeoutSession()
        example.select(self.pid, 1, dict(P2, tid="so-3f9a1c2b/1/3", lat_e8=5100009261,
                                         lon_e8=999999552))
        example_fix = dict(fix_at(5100008991, 1000000012), alt_mm=412345, sats=21,
                           receiver_accuracy={"horizontal_sigma_m": 0.011,
                                              "altitude_sigma_m": 0.018},
                           fix_seq=4711, fix_rev=2, fix_age_ms=320, stale=False,
                           pending_gst=False)
        settling = {"state": "settling", "dn_m": 0.3, "de_m": -0.29, "dist_m": 0.42,
                    "bearing_deg": 316.0, "tolerance_m": 0.1, "fine_radius_m": 5.0,
                    "window": {"have": 3, "need": 5, "spread_m": 0.012}, "reasons": []}

        def typical(*args):
            with mock.patch.object(example, "guidance", return_value=dict(
                    settling, reasons=[])):
                return example.live_packet(
                    1000, (example_fix, tracking.quality_check(example_fix)),
                    {"epoch_age_sec": 0.8, "stale": False},
                    {"corrections": "ok", "receiver": "ok"}, dict(STATS), "ok", False,
                    1760000123)

        # The worst case under the latch (at most 1400 bytes), the example
        # without it (at most 900 bytes; the latch adds 19 bytes).
        for status in ("unknown", "confirmed"):
            for build, uncertain, reasons, limit in (
                    (worst, "test", ["horizontal_spread_too_high", "recording_active",
                                     "write_uncertain"], 1400),
                    (typical, None, [], 900)):
                self.store.uncertain = uncertain
                with self.subTest(status=status, limit=limit), \
                        mock.patch.object(stakeout_session, "status_lookup",
                                          lambda project_id, tid, status=status: status), \
                        mock.patch.object(self.so, "live_packet", build):
                    packet = web.stakeout_live_snapshot()
                    self.assertEqual(packet["target"]["status"], status)
                    self.assertEqual(packet["guidance"]["reasons"], reasons)
                    self.assertLessEqual(device_bytes(packet), limit)


class TestUncertainReaders(LiveFixture, ProgressFixture, unittest.TestCase):
    def test_readers_warn_and_change_nothing(self):
        self.stakeout_point(P1["tid"])
        self.ready()
        self.append(stakeout_progress.skip_event(SET_ID + "/1/2", 0, CMD))

        def read():
            overview = self.overview()
            listing = self.progress_list()
            self.assertEqual((overview[0], listing[0]), (200, 200), (overview, listing))
            return overview[1], listing[1], self.snapshot()

        overview, listing, live = read()
        self.assertIs(overview["progress"]["write_uncertain"], False)
        self.assertIs(listing["write_uncertain"], False)
        self.assertNotIn("write_uncertain", live["guidance"]["reasons"])
        self.assertEqual(overview["sets"][0]["progress"],
                         {"confirmed": 1, "verified": 0, "manual": 1, "skipped": 1})
        self.store.uncertain = "test"
        warned_overview, warned_listing, warned_live = read()
        self.assertIs(warned_overview["progress"]["write_uncertain"], True)
        self.assertEqual(warned_overview["sets"], overview["sets"])
        self.assertIs(warned_listing["write_uncertain"], True)
        self.assertEqual(warned_listing["states"], listing["states"])
        self.assertIn("write_uncertain", warned_live["guidance"]["reasons"])
        self.assertEqual(warned_live["target"], live["target"])
        self.assertEqual(warned_live["guidance"]["state"], live["guidance"]["state"])


class TestActivePersistence(ProgressFixture, unittest.TestCase):
    def test_selection_and_order_are_remembered(self):
        status, answer = self.select()
        self.assertEqual(status, 200, answer)
        self.assertEqual(answer, {"selected": P2, "changed": True, "remembered": True})
        self.assertEqual(self.events()[-1]["ev"], "active")
        self.assertEqual(self.events()[-1]["tid"], TID)
        status, answer = self.order()
        self.assertEqual(answer, {"part_id": SET_ID + "/1", "reversed": True,
                                  "remembered": True})
        self.assertEqual({key: self.events()[-1][key] for key in ("ev", "part_id", "reversed")},
                         {"ev": "order", "part_id": SET_ID + "/1", "reversed": True})
        self.restart()
        status, answer = self.overview()
        self.assertEqual(status, 200, answer)
        self.assertEqual(answer["progress"]["active_tid"], TID)
        self.assertEqual(self.so.orders(), {SET_ID: [1]})
        self.assertIsNone(self.so.target)
        # The remembered target again: selected, nothing appended.
        count = len(self.events())
        status, answer = self.select()
        self.assertEqual(answer, {"selected": P2, "changed": True, "remembered": True})
        self.assertEqual(len(self.events()), count)
        self.assertEqual(self.so.orders(), {SET_ID: [1]})

    def test_write_failure_or_lock_keeps_the_selection(self):
        self.assertEqual(self.overview()[0], 200)
        full = {"writable": False}
        cases = (("reserve", mock.patch.object(self.tracker, "storage_capacity",
                                               return_value=full)),
                 ("lock", mock.patch.object(self.store, "busy", True)))
        for name, patch in cases:
            with self.subTest(case=name):
                self.select(None)
                count = len(self.events())
                web.log.reset_mock()
                with patch:
                    status, answer = self.select()
                    self.assertEqual((status, answer["remembered"]), (200, False), answer)
                    status, answer = self.order(reversed=name == "lock")
                    self.assertEqual((status, answer["remembered"]), (200, False), answer)
                self.assertEqual(self.so.target["tid"], TID)
                self.assertEqual(len(self.events()), count)
                self.assertEqual(len(self.logged(web, "WARN")), 2)
                self.assertIsNone(self.store.uncertain)
        self.select(None)
        with failing_append("close"):
            status, answer = self.select()
        self.assertEqual((status, answer["remembered"]), (200, False), answer)
        self.assertEqual(self.so.target["tid"], TID)
        self.assertIsNotNone(self.store.uncertain)

    def test_selection_under_the_latch(self):
        self.assertEqual(self.overview()[0], 200)
        self.store.uncertain = "test"
        with mock.patch.object(stakeout_progress.ProgressLog, "append") as append:
            status, answer = self.select()
            self.assertEqual((status, answer["remembered"]), (200, False), answer)
            status, answer = self.order()
            self.assertEqual((status, answer["remembered"]), (200, False), answer)
        append.assert_not_called()
        self.assertEqual(self.so.target["tid"], TID)
        self.assertEqual(self.so.orders(), {SET_ID: [1]})

    def test_reserve_measures_once_then_refuses(self):
        samples = []
        sampled = []

        def capacity(size=16384, force=False):
            samples.append(force)
            if force:
                sampled.append(True)
            return {"writable": bool(sampled) and size < 1000}

        with mock.patch.object(self.tracker, "storage_capacity", capacity):
            asyncio.run(web._stakeout_reserve(500))
            self.assertEqual(samples, [False, True, False])
            asyncio.run(web._stakeout_reserve(500))
            self.assertEqual(samples, [False, True, False, False])
            del samples[:]
            del sampled[:]
            with self.assertRaises(stakeout.StakeoutError) as caught:
                asyncio.run(web._stakeout_reserve(5000))
        self.assertEqual((caught.exception.code, caught.exception.status),
                         ("storage_reserve", "507 Insufficient Storage"))
        self.assertEqual(samples, [False, True, False])


class TestOverviewProgress(ProgressFixture, unittest.TestCase):
    def test_overview_without_progress(self):
        status, answer = self.overview()
        self.assertEqual(status, 200, answer)
        self.assertEqual(answer["progress"], {"active_tid": None, "damaged_lines": 0,
                                              "write_uncertain": False})
        self.assertEqual([summary["progress"] for summary in answer["sets"]], [ZERO])
        self.assertEqual(answer["unowned_folders"], 0)
        self.assertFalse(os.path.exists(self.progress_path()))

    def test_overview_counts_the_progress(self):
        self.stakeout_point(P1["tid"])
        self.stakeout_point(SET_ID + "/1/2", mode="verified")
        self.append(stakeout_progress.skip_event(TID, 0, CMD))
        self.append(stakeout_progress.skip_event(SET_ID + "/1/3", 0, CMD))
        self.append(stakeout_progress.reopen_event(SET_ID + "/1/3", 1, CMD))
        with open(self.progress_path(), "ab") as handle:
            handle.write(b"garbage\n")
        self.restart()
        status, answer = self.overview()
        self.assertEqual(status, 200, answer)
        self.assertEqual(answer["progress"], {"active_tid": None, "damaged_lines": 1,
                                              "write_uncertain": False})
        self.assertEqual(answer["sets"][0]["progress"],
                         {"confirmed": 2, "verified": 1, "manual": 1, "skipped": 1})

    def test_cold_overview_under_the_lock_is_busy(self):
        self.restart()
        self.store.busy = True
        self.assert_error(self.overview(), 503, "stakeout_busy")
        self.assert_error(self.progress_list(), 503, "stakeout_busy")
        status, _head, payload = self.read_set()
        self.assertEqual((status, json.loads(payload)["error"]), (503, "stakeout_busy"))
        self.store.busy = False
        self.assertEqual(self.overview()[0], 200)

    def test_remnants_are_cleared_after_the_answer(self):
        self.append(stakeout_progress.set_removed_event(SET_ID, self.imported_at(), 1))
        self.restart()
        clearing = []
        real_clear = self.store.clear_removed

        async def clear_removed(project_id):
            clearing.append(self.store.busy)
            return await real_clear(project_id)

        with mock.patch.object(self.store, "clear_removed", clear_removed):
            status, answer = self.overview()
        self.assertEqual(status, 200, answer)
        self.assertEqual(answer["sets"], [])
        self.assertEqual(clearing, [True])
        self.assertFalse(os.path.exists(self.path()))
        self.assertFalse(self.store.remnants(self.pid))
        self.assertIs(self.store.busy, False)

    def test_remnants_stay_while_the_lock_is_held(self):
        self.append(stakeout_progress.set_removed_event(SET_ID, self.imported_at(), 1))
        self.restart()
        self.assertEqual(self.overview()[0], 200)
        with open(self.path(), "wb") as handle:
            handle.write(b"remnant")
        self.store._remnants[self.pid] = {self.path()}
        with mock.patch.object(self.store, "busy", True):
            self.assertEqual(self.overview()[0], 200)
        self.assertTrue(os.path.exists(self.path()))
        self.store.uncertain = "test"
        self.assertEqual(self.overview()[0], 200)
        self.assertTrue(os.path.exists(self.path()))


class _CountingWriter(FakeHttpWriter):
    def __init__(self):
        super().__init__()
        self.writes = 0

    def write(self, data):
        self.writes += 1
        super().write(data)


class TestProgressList(ProgressFixture, unittest.TestCase):
    def test_progress_list_follows_e18(self):
        second = self.second_set()
        point = self.stakeout_point(P1["tid"])
        self.append(stakeout_progress.skip_event(TID, 0, CMD))
        self.append(stakeout_progress.skip_event(second + "/1/2", 0, CMD))
        self.append(stakeout_progress.reopen_event(second + "/1/2", 1, CMD))
        self.select(second + "/1/1")
        self.order(second + "/1")
        status, answer = self.progress_list()
        self.assertEqual(status, 200, answer)
        order = [summary["set_id"] for summary in self.overview()[1]["sets"]]
        self.assertEqual(set(order), {SET_ID, second})
        rows = {SET_ID: [[P1["tid"], "confirmed", 0, "manual", point["id"]],
                         [TID, "skipped", 0, None, None]],
                second: [[second + "/1/2", "open", 1, None, None]]}
        self.assertEqual(answer, {
            "schema": 1, "project_id": self.pid, "active_tid": second + "/1/1",
            "write_uncertain": False, "orders": {second: [1]},
            "floors": {SET_ID: 0, second: 0},
            "counts": {SET_ID: {"confirmed": 1, "verified": 0, "manual": 1, "skipped": 1},
                       second: ZERO},
            "states": rows[order[0]] + rows[order[1]]})

    def test_restored_floor_applies_to_every_set(self):
        second = self.second_set()
        self.append(stakeout_progress.skip_event(TID, 0, CMD))
        self.append(stakeout_progress.restored_event(3))
        status, answer = self.progress_list()
        self.assertEqual(status, 200, answer)
        self.assertEqual(answer["floors"], {SET_ID: 3, second: 3})
        self.assertEqual(answer["states"], [])

    def test_many_rows_stream_in_pieces(self):
        lines = [json.dumps({"kind": "stakeout_progress", "schema": 1,
                             "project_id": self.pid, "project_created_at": self.fp()})]
        lines += [json.dumps({"ev": "skip", "tid": "%s/1/%d" % (SET_ID, seq), "gen": 0})
                  for seq in range(1, 2001)]
        with open(self.progress_path(), "w") as handle:
            handle.write("\n".join(lines) + "\n")
        self.restart()
        writer = self.handle(_CountingWriter(), "GET", PROGRESS)
        head, _sep, body = bytes(writer.data).partition(b"\r\n\r\n")
        self.assertTrue(head.startswith(b"HTTP/1.0 200 OK"), head)
        answer = json.loads(body)
        self.assertEqual(len(answer["states"]), 2000)
        self.assertEqual(answer["states"][0], [SET_ID + "/1/1", "skipped", 0, None, None])
        self.assertGreater(writer.writes, 10)

    def test_route_policy_and_refusals(self):
        self.assertEqual(web_routing.ROUTES[PROGRESS], (("GET", "HEAD"), "GET required", 4096))
        status, _head, payload = self.call("GET", PROGRESS, cookie=False)
        self.assertEqual(status, 401, payload)
        status, _head, payload = self.call("POST", PROGRESS, b"{}")
        self.assertEqual(status, 405, payload)
        status, head, payload = self.call("HEAD", PROGRESS)
        self.assertEqual((status, payload), (200, b""))
        self.assert_error(self.progress_list("p-unknown"), 404, "project_not_found")

    def test_inactive_project_gets_a_transient_fold(self):
        other = self.other_project()
        self.assertEqual(self.import_other(other)[0], 200)
        status, answer = self.progress_list(other)
        self.assertEqual(status, 200, answer)
        self.assertEqual((answer["project_id"], answer["states"], answer["active_tid"]),
                         (other, [], None))
        self.assertEqual(len(answer["floors"]), 1)
        self.assertIsNone(self.fold(other))


class TestDeleteWithProgress(ProgressFixture, unittest.TestCase):
    def test_delete_commits_set_removed(self):
        self.stakeout_point(P1["tid"])
        self.append(stakeout_progress.skip_event(TID, 0, CMD))
        imported_at = self.imported_at()
        status, answer = self.delete(expected_confirmed=1)
        self.assertEqual((status, answer), (200, {"deleted": SET_ID, "cleanup": "done"}))
        event = self.events()[-1]
        self.assertEqual({key: event[key] for key in ("ev", "set_id", "imported_at", "floor")},
                         {"ev": "set_removed", "set_id": SET_ID, "imported_at": imported_at,
                          "floor": 1})
        self.assertNotIn(SET_ID, self.fold().entries)
        self.assertEqual(self.files(), [self.pid + "/progress.jsonl"])

    def test_wrong_count_changes_nothing(self):
        self.stakeout_point(P1["tid"])
        self.assertEqual(self.overview()[0], 200)
        self.assert_error(self.delete(expected_confirmed=0), 409, "confirmed_changed")
        self.assertEqual(self.events(), [])
        self.assertTrue(os.path.exists(self.path()))

    def test_commit_failure_latches(self):
        self.assertEqual(self.overview()[0], 200)
        with failing_append("flush"):
            self.assert_error(self.delete(), 503, "stakeout_write_uncertain")
        self.assertTrue(os.path.exists(self.path()))
        self.assertIsNotNone(self.store.uncertain)
        self.assert_error(self.delete(), 503, "stakeout_write_uncertain")

    def test_delete_without_reserve(self):
        with mock.patch.object(self.tracker, "storage_capacity",
                               return_value={"writable": False}):
            status, answer = self.delete()
        self.assertEqual((status, answer), (200, {"deleted": SET_ID, "cleanup": "done"}))
        self.assertFalse(os.path.exists(self.path()))

    def test_partial_cleanup_is_pending(self):
        real_remove = os.remove

        def remove(name):
            if str(name).endswith(".json"):
                raise OSError(5, "io")
            real_remove(name)

        with mock.patch.object(stakeout.os, "remove", remove):
            status, answer = self.delete()
        self.assertEqual((status, answer), (200, {"deleted": SET_ID, "cleanup": "pending"}))
        self.assertTrue(os.path.exists(self.path()))
        status, answer = self.overview()
        self.assertEqual((status, answer["sets"]), (200, []))
        self.assertFalse(os.path.exists(self.path()))

    def test_power_cut_after_the_commit_then_reimport(self):
        point = self.stakeout_point(P1["tid"])
        package = self.package()
        real_remove = os.remove

        def remove(name):
            if str(name).endswith(".json"):
                raise PowerCut()
            real_remove(name)

        with mock.patch.object(stakeout.os, "remove", remove):
            with self.assertRaises(PowerCut):
                self.delete(expected_confirmed=1)
        self.assertTrue(os.path.exists(self.path()))
        self.assertEqual(self.events()[-1]["ev"], "set_removed")
        self.restart()
        status, answer = self.overview()
        self.assertEqual((status, answer["sets"]), (200, []))
        self.assertFalse(os.path.exists(self.path()))
        status, answer = self.import_(package)
        self.assertEqual(status, 200, answer)
        status, answer = self.progress_list()
        self.assertEqual(status, 200, answer)
        self.assertEqual(answer["floors"], {SET_ID: 1})
        self.assertEqual(answer["states"], [])
        self.assertEqual(answer["counts"], {SET_ID: ZERO})
        self.assertIn(point, self.tracker.projects[0]["points"])


class TestImportWithProgress(ProgressFixture, unittest.TestCase):
    def test_import_loads_progress_and_clears_remnants(self):
        self.append(stakeout_progress.set_removed_event(SET_ID, self.imported_at(), 1))
        self.restart()
        status, answer = self.import_(make_package(targets=2, offset=55))
        self.assertEqual(status, 200, answer)
        self.assertFalse(os.path.exists(self.path()))
        self.assertEqual(self.files(), sorted([self.pid + "/progress.jsonl",
                                               self.name(answer["set_id"])]))

    def test_import_under_the_latch(self):
        files = self.files()
        self.store.uncertain = "test"
        self.assert_error(self.import_(make_package(targets=2, offset=55)), 503,
                          "stakeout_write_uncertain")
        self.assertEqual(self.files(), files)


class TestProjectDeletion(ProgressFixture, unittest.TestCase):
    def test_delete_project_removes_the_progress_file(self):
        self.append(stakeout_progress.skip_event(TID, 0, CMD))
        fp = self.fp()
        self.assertIsNotNone(self.progress.peek(self.pid, fp))
        status, answer = self.track("delete_project", project_id=self.pid)
        self.assertEqual(status, 200, answer)
        self.assertEqual(os.listdir(self.root), [])
        self.assertIsNone(self.progress.peek(self.pid, fp))

    def test_project_deletion_waits_for_no_change(self):
        self.append(stakeout_progress.skip_event(TID, 0, CMD))
        files = self.files()
        with mock.patch.object(self.store, "busy", True):
            self.assert_error(self.track("delete_project", project_id=self.pid), 409,
                              "storage_busy")
        self.assertEqual(self.files(), files)
        self.assertTrue(web._project_live(self.pid))

    def test_project_deletion_under_the_latch(self):
        self.append(stakeout_progress.skip_event(TID, 0, CMD))
        self.store.uncertain = "test"
        status, answer = self.track("delete_project", project_id=self.pid)
        self.assertEqual(status, 200, answer)
        self.assertEqual(os.listdir(self.root), [])


class TestUnownedFolders(ProgressFixture, unittest.TestCase):
    def test_unowned_folders_stay_and_are_counted(self):
        archived = self.other_project("Archived")
        self.assertEqual(self.import_other(archived)[0], 200)
        status, answer = self.track("archive_project", project_id=archived)
        self.assertEqual(status, 200, answer)
        remaining = self.other_project("Remaining")
        self.append(stakeout_progress.skip_event(TID, 0, CMD))
        gone = os.path.join(self.root, "project-gone")
        os.makedirs(gone)
        for name in ("so-00000001.json", "progress.jsonl"):
            with open(os.path.join(gone, name), "wb") as handle:
                handle.write(b"kept " + name.encode())
        for name in ("project-gone2.deleting", "bad id!"):
            os.makedirs(os.path.join(self.root, name))
        with open(os.path.join(self.root, "stray.json"), "wb") as handle:
            handle.write(b"{}")

        def snapshot():
            found = {}
            for folder, _dirs, names in os.walk(self.root):
                for name in names:
                    path = os.path.join(folder, name)
                    found[os.path.relpath(path, self.root)] = self.file_bytes(path)
            return found, sorted(os.listdir(self.root))

        before = snapshot()
        stakeout_progress.log.reset_mock()
        status, answer = self.overview()
        self.assertEqual(status, 200, answer)
        self.assertEqual(answer["unowned_folders"], 1)
        self.assertIn(("INFO", "STAKE", "Stakeout folders without a project: 1."),
                      self.logged(stakeout_progress, "INFO"))
        self.assertEqual(snapshot(), before)
        # A restore of a backup without the project: its folder stays, counted.
        raw = "".join(self.tracker.iter_backup_ndjson(remaining)).encode()
        status, _head, payload = self.call("POST", "/api/tracking/import", raw,
                                           content_type="application/x-ndjson")
        self.assertEqual(status, 200, payload)
        self.assertFalse(web._project_live(self.pid))
        status, answer = self.overview(remaining)
        self.assertEqual(status, 200, answer)
        self.assertEqual(answer["unowned_folders"], 2)
        # Nothing is removed; the restore only wrote the floor of the project
        # it dropped in front of it (E26).
        progress = os.path.join(self.pid, "progress.jsonl")
        files, listing = snapshot()
        kept = dict(before[0])
        old, grown = kept.pop(progress), files.pop(progress)
        self.assertEqual((files, listing), (kept, before[1]))
        self.assertTrue(grown.startswith(old))
        self.assertEqual(json.loads(grown[len(old):])["ev"], "restored")


class TestArchiveFlag(ProgressFixture, unittest.TestCase):
    def archive(self, project_id):
        status, answer = self.track("archive_project", project_id=project_id)
        self.assertEqual(status, 200, answer)
        status, answer = self.api("GET", TRACKING)
        self.assertEqual(status, 200, answer)
        return answer["archives"][-1]

    def test_archiving_records_stakeout_data(self):
        with_sets = self.other_project("With sets")
        self.assertEqual(self.import_other(with_sets)[0], 200)
        self.assertIs(self.archive(with_sets)["stakeout_data"], True)
        self.assertTrue(os.path.isdir(os.path.join(self.root, with_sets)))
        without = self.other_project("Without")
        self.assertIs(self.archive(without)["stakeout_data"], False)
        unknown = self.other_project("Unknown")
        web.log.reset_mock()
        with mock.patch.object(self.store, "has_data", side_effect=OSError(5, "io")):
            archive = self.archive(unknown)
        self.assertNotIn("stakeout_data", archive)
        self.assertEqual(len(self.logged(web, "WARN")), 1)


# --- confirm, skip and reopen (Task A6) ----------------------------------------

ANSWER_KEYS = {"persisted", "repeated", "tid", "gen", "status", "mode", "point_id",
               "dn_m", "de_m", "dist_m", "tolerance_m"}
UNCERTAIN = "stakeout_write_uncertain"


class ConfirmFixture(ProgressFixture, LiveFixture):
    """The progress fixture with a live fix feed (LiveFixture): the stakeout
    actions, races through the dispatcher and a restart of tracker, store,
    progress log and session over the same files."""

    def action_body(self, action, tid=TID, expect_gen=0, **fields):
        fields.update(action=action, tid=tid, cmd=CMD, expect_gen=expect_gen)
        return fields

    def assert_refusal(self, response, http_status, code, **fields):
        """assert_error whose fields may name the extra "status" too."""
        self.assert_error(response, http_status, code)
        for key, value in fields.items():
            self.assertEqual(response[1].get(key, "missing"), value, response)

    def act(self, action, tid=TID, expect_gen=0, **fields):
        return self.post(self.action_body(action, tid, expect_gen, **fields))

    def confirm_target(self, mode="verified", tid=TID, expect_gen=0, **fields):
        return self.act("confirm", tid, expect_gen, mode=mode, **fields)

    def skip(self, tid=TID, expect_gen=0):
        return self.act("skip", tid, expect_gen)

    def reopen(self, tid=TID, expect_gen=0):
        return self.act("reopen", tid, expect_gen)

    def points(self, tid=None):
        """The stakeout points of the active project (of target tid), from RAM."""
        return [point for point in web._stakeout_points(self.pid)
                if point["object_type"] == "stakeout"
                and tid in (None, point["stakeout_tid"])]

    def row(self, tid=TID):
        """The progress list row of tid; None while it is open at its floor."""
        status, answer = self.progress_list()
        self.assertEqual(status, 200, answer)
        for row in answer["states"]:
            if row[0] == tid:
                return row
        return None

    def fresh_fix(self, qual=4):
        """One fresh fix on P2: with its GST for quality 4, else without."""
        if qual == 4:
            self.pairs(1)
            return
        self.gga_ms += 1000
        self.now = self.gga_ms
        self.app.update_fix(on_p2(utc=self.utc(self.gga_ms), qual=qual, gst=False))

    def journal_size(self):
        self.tracker._sync_journal()
        return os.path.getsize(self.tracker._segment_path())

    def progress_size(self):
        try:
            return os.path.getsize(self.progress_path())
        except OSError:
            return 0

    def reboot(self, journal_size=None, progress_size=None):
        """A restart: the old tracker closes (its buffered journal reaches the
        file), journal or progress file are cut to the given size, then a new
        tracker, store, progress log and session load the same files."""
        old = self.tracker
        old._sync_journal(close=True)
        old.point_store.close()
        if journal_size is not None:
            with _real_open(old._segment_path(), "r+b") as handle:
                handle.truncate(journal_size)
        if progress_size is not None:
            with _real_open(self.progress_path(), "r+b") as handle:
                handle.truncate(progress_size)
        tracker = tracking.Tracker(old.path)
        self.addCleanup(tracker.point_store.close)
        self.addCleanup(tracker._sync_journal, True)
        self.tracker = web.tracker = tracker
        self.restart()
        self.app.set_fix_listener(self.so.on_fix)
        self.assertEqual(tracker.active_project_id, self.pid)

    async def dispatch(self, path, payload, content_type="application/json"):
        writer = FakeHttpWriter()
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        raw = self.raw_request("POST", path, body, content_type=content_type)
        await web.handle_http_client(FakeReader(raw), writer)
        head, _, body = bytes(writer.data).partition(b"\r\n\r\n")
        return int(head.split(b" ", 2)[1]), json.loads(body)

    def race(self, request, *disturbances, hold="load"):
        """(answer of request, answers of the disturbances): request (a POST
        /api/stakeout) waits after its progress load (hold "load", before the
        commit core of a verified confirmation) or before its target lookup
        (hold "find") while each disturbance runs: a callable, or the
        (path, payload[, content type]) of a request."""
        async def scenario():
            entered, release = asyncio.Event(), asyncio.Event()

            async def wait():
                if not entered.is_set():
                    entered.set()
                    await release.wait()

            if hold == "load":
                real = web._stakeout_load

                async def held(*args):
                    fold = await real(*args)
                    await wait()
                    return fold

                patch = mock.patch.object(web, "_stakeout_load", held)
            else:
                real = self.store.find_target

                async def held(*args, **kwargs):
                    await wait()
                    return await real(*args, **kwargs)

                patch = mock.patch.object(self.store, "find_target", held)
            with patch:
                task = asyncio.ensure_future(self.dispatch("/api/stakeout", request))
                await asyncio.wait_for(entered.wait(), 5)
                answers = []
                for item in disturbances:
                    answers.append(item() if callable(item) else await self.dispatch(*item))
                release.set()
                return await asyncio.wait_for(task, 5), answers

        return asyncio.run(scenario())


class TestConfirmVerified(ConfirmFixture, unittest.TestCase):
    def test_ready_target_becomes_a_stakeout_point(self):
        self.ready(0.05)
        mean = tracking.average_fixes(self.so.window_fixes())
        before = self.events()
        status, answer = self.confirm_target()
        self.assertEqual(status, 200, answer)
        self.assertEqual(set(answer), ANSWER_KEYS)
        point, = self.points()
        self.assertEqual(answer, {"persisted": True, "repeated": False, "tid": TID, "gen": 0,
                                  "status": "confirmed", "mode": "verified",
                                  "point_id": point["id"], "dn_m": point["stakeout_dn_m"],
                                  "de_m": point["stakeout_de_m"],
                                  "dist_m": point["stakeout_dist_m"], "tolerance_m": 0.1})
        self.assertEqual((point["stakeout_tid"], point["stakeout_gen"], point["stakeout_cmd"],
                          point["name"], point["line_id"]), (TID, 0, CMD, P2["name"], None))
        self.assertEqual(point["coordinate"], [mean["lon_e8"], mean["lat_e8"], mean["alt_mm"]])
        self.assertEqual(point["measurement"]["source"], "stakeout_verified")
        # The offsets are the target minus the rover, rounded to millimetres.
        self.assertAlmostEqual(answer["dn_m"], -0.05, places=3)
        self.assertEqual(answer["de_m"], 0.0)
        self.assertEqual(answer["dist_m"], abs(answer["dn_m"]))
        self.assertLessEqual(answer["dist_m"], 0.1)
        # A confirmation writes no progress line; the point is the commit.
        self.assertEqual(self.events(), before)
        self.assertEqual(self.live()[0]["status"], "confirmed")
        self.assertEqual(self.row(), [TID, "confirmed", 0, "verified", point["id"]])

    def test_not_ready_is_refused_with_reasons(self):
        self.selected()
        self.pairs(4)
        response = self.confirm_target()
        self.assert_error(response, 409, "not_ready", state="settling")
        self.assertIsInstance(response[1]["reasons"], list)
        self.pairs(1)
        self.assert_error(self.confirm_target(tid=P1["tid"]), 409, "not_ready",
                          reasons=["target_not_selected"])
        self.assertEqual(self.points(), [])
        self.assertIsNone(self.row())

    def test_measurement_blocks_every_action(self):
        self.ready()
        with mock.patch.object(web.measurement_session, "active", return_value=True):
            for action, fields in (("confirm", {"mode": "verified"}),
                                   ("confirm", {"mode": "manual"}), ("skip", {}),
                                   ("reopen", {})):
                with self.subTest(action=action, **fields):
                    self.assert_error(self.act(action, **fields), 409, "measurement_active")
        self.assertEqual(self.points(), [])
        self.assertIsNone(self.row())

    def test_clear_refusals_do_not_latch(self):
        self.ready()
        full = dict(self.tracker.storage_capacity(), writable=False)
        real_check = tracking.quality_check
        refusals = (
            ("point_limit_reached", 409, lambda: mock.patch.object(
                self.tracker, "feature_limit", return_value=self.tracker.feature_count())),
            ("storage_reserve", 507, lambda: mock.patch.object(
                self.tracker, "storage_capacity", lambda *args, **kwargs: dict(full))),
            ("quality_rejected", 409, lambda: support.patch_module_global(
                "tracking", "quality_check", lambda fix: dict(
                    real_check(fix), accepted=False, reasons=["hdop_too_high"]))),
            ("tracking_storage_failed", 507, lambda: mock.patch.object(
                tracking.Tracker, "_measurement", side_effect=OSError(5, "io"))))
        for gen, (code, status, refusal) in enumerate(refusals):
            with self.subTest(code=code):
                with refusal():
                    response = self.confirm_target(expect_gen=gen)
                self.assert_error(response, status, code)
                if code == "quality_rejected":
                    self.assertEqual(response[1]["reasons"], ["hdop_too_high"])
                self.assertEqual(len(self.points()), gen)
                self.assertIsNone(self.store.uncertain)
                self.assertEqual(self.row(), None if gen == 0 else [TID, "open", gen, None, None])
                status, answer = self.confirm_target(expect_gen=gen)
                self.assertEqual((status, answer.get("repeated"), answer.get("gen")),
                                 (200, False, gen), answer)
                self.assertEqual(self.reopen(expect_gen=gen)[0], 200)


class TestConfirmManual(ConfirmFixture, unittest.TestCase):
    def test_found_with_any_fresh_position(self):
        self.fresh_fix(qual=1)
        status, answer = self.confirm_target("manual")
        self.assertEqual(status, 200, answer)
        self.assertEqual((answer["status"], answer["mode"], answer["gen"], answer["tolerance_m"]),
                         ("confirmed", "manual", 0, None))
        point, = self.points(TID)
        self.assertEqual(point["id"], answer["point_id"])
        self.assertEqual(point["measurement"]["source"], "stakeout_manual")
        self.assertIs(point["measurement"]["quality"]["accepted"], False)
        self.fresh_fix()
        status, answer = self.confirm_target("manual", P1["tid"])
        self.assertEqual(status, 200, answer)
        point, = self.points(P1["tid"])
        self.assertIs(point["measurement"]["quality"]["accepted"], True)
        self.assertEqual(self.row(P1["tid"]), [P1["tid"], "confirmed", 0, "manual", point["id"]])

    def test_found_needs_a_position(self):
        self.assert_error(self.confirm_target("manual"), 409, "no_position")
        self.fresh_fix()
        self.now += cfg.CONFIG["fix_stale_after_ms"] + 1
        self.assert_error(self.confirm_target("manual"), 409, "no_position")
        self.assertEqual(self.points(), [])
        self.assertIsNone(self.row())

    def test_manual_needs_no_selected_target(self):
        self.selected()
        self.fresh_fix()
        status, answer = self.confirm_target("manual", P1["tid"])
        self.assertEqual((status, answer.get("status")), (200, "confirmed"), answer)
        self.assertEqual(self.so.target["tid"], TID)

    def test_note_limit(self):
        self.fresh_fix()
        self.assert_error(self.confirm_target("manual", note="a" * 501), 400, "invalid_request")
        self.assert_error(self.confirm_target("manual", note=5), 400, "invalid_request")
        self.assertEqual(self.points(), [])
        note = "\U0001F600" * 500
        body = json.dumps(self.action_body("confirm", mode="manual", note=note),
                          ensure_ascii=False).encode()
        status, answer = self.post(body)
        self.assertEqual(status, 200, answer)
        point, = self.points()
        self.assertEqual(point["note"], note)


class TestSkipReopen(ConfirmFixture, unittest.TestCase):
    def test_skip_then_reopen(self):
        status, answer = self.skip()
        self.assertEqual(status, 200, answer)
        self.assertEqual(answer, {"persisted": True, "repeated": False, "tid": TID, "gen": 0,
                                  "status": "skipped", "mode": None, "point_id": None,
                                  "dn_m": None, "de_m": None, "dist_m": None,
                                  "tolerance_m": None})
        event = self.events()[-1]
        self.assertEqual((event["ev"], event["tid"], event["gen"], event["cmd"]),
                         ("skip", TID, 0, CMD))
        status, answer = self.reopen()
        self.assertEqual((status, answer["status"], answer["gen"], answer["repeated"]),
                         (200, "open", 1, False), answer)
        event = self.events()[-1]
        self.assertEqual((event["ev"], event["tid"], event["gen"], event["cmd"]),
                         ("reopen", TID, 1, CMD))
        self.assert_refusal(self.reopen(expect_gen=1), 409, "target_open", status="open", gen=1)
        self.assertEqual(self.row(), [TID, "open", 1, None, None])

    def test_reopen_after_a_confirmation_keeps_the_point(self):
        self.ready()
        self.assertEqual(self.confirm_target()[0], 200)
        status, answer = self.reopen()
        self.assertEqual((status, answer["status"], answer["gen"]), (200, "open", 1), answer)
        self.assertEqual(len(self.points()), 1)
        self.assertEqual(self.live()[0]["status"], "open")
        status, answer = self.confirm_target(expect_gen=1)
        self.assertEqual((status, answer.get("gen")), (200, 1), answer)
        self.assertEqual(sorted(point["stakeout_gen"] for point in self.points()), [0, 1])

    def test_unknown_target_and_no_reserve(self):
        self.assert_error(self.skip(SET_ID + "/1/99"), 404, "target_not_found")
        before = self.events()
        full = dict(self.tracker.storage_capacity(), writable=False)
        with mock.patch.object(self.tracker, "storage_capacity",
                               lambda *args, **kwargs: dict(full)):
            self.assert_error(self.skip(), 507, "storage_reserve")
            self.assert_error(self.reopen(), 507, "storage_reserve")
        self.assertEqual(self.events(), before)
        self.assertIsNone(self.store.uncertain)
        self.assertEqual(self.skip()[0], 200)

    def test_a_repeat_needs_no_reserve(self):
        # The answer of a stored skip or reopen is lost; the browser sends the
        # same request while the flash reserve is exhausted.
        other = P1["tid"]
        status, skipped = self.skip()
        self.assertEqual(status, 200, skipped)
        self.assertEqual(self.skip(other)[0], 200)
        status, reopened = self.reopen(other)
        self.assertEqual(status, 200, reopened)
        before = self.events()
        full = dict(self.tracker.storage_capacity(), writable=False)
        with mock.patch.object(self.tracker, "storage_capacity",
                               lambda *args, **kwargs: dict(full)):
            status, answer = self.skip()
            self.assertEqual((status, answer), (200, dict(skipped, repeated=True)))
            status, answer = self.reopen(other)
            self.assertEqual((status, answer), (200, dict(reopened, repeated=True)))
            # An action that writes still needs the reserve.
            self.assert_error(self.skip(other, expect_gen=1), 507, "storage_reserve")
        self.assertEqual(self.events(), before)


class TestIdempotency(ConfirmFixture, unittest.TestCase):
    def test_same_request_gets_the_same_answer(self):
        self.ready()
        status, first = self.confirm_target()
        self.assertEqual(status, 200, first)
        status, second = self.confirm_target()
        self.assertEqual(status, 200, second)
        self.assertEqual(second, dict(first, repeated=True))
        self.assertEqual(len(self.points()), 1)
        status, first = self.skip(P1["tid"])
        status, second = self.skip(P1["tid"])
        self.assertEqual((status, second), (200, dict(first, repeated=True)))
        self.assertEqual([event["ev"] for event in self.events()].count("skip"), 1)

    def test_overlapping_requests_in_both_orders(self):
        self.fresh_fix()
        for request in (self.action_body("confirm", mode="manual"),
                        self.action_body("skip", P1["tid"])):
            with self.subTest(action=request["action"]):
                points, events = len(self.points()), self.events()
                (status, answer), (second,) = self.race(
                    request, ("/api/stakeout", request), hold="find")
                self.assert_error(second, 409, "storage_busy")
                self.assertEqual((status, answer.get("repeated")), (200, False), answer)
                self.assertEqual(len(self.points()), points + (request["action"] == "confirm"))
                self.assertEqual(len(self.events()), len(events) + (request["action"] == "skip"))
                status, again = self.post(request)
                self.assertEqual((status, again), (200, dict(answer, repeated=True)))

    def test_retry_after_restart(self):
        self.ready()
        status, first = self.confirm_target()
        self.assertEqual(status, 200, first)
        status, skipped = self.skip(P1["tid"])
        self.assertEqual(status, 200, skipped)
        self.reboot()
        self.assertEqual(self.confirm_target(), (200, dict(first, repeated=True)))
        self.assertEqual(len(self.points()), 1)
        self.assertEqual(self.skip(P1["tid"]), (200, dict(skipped, repeated=True)))
        status, reopened = self.reopen(P1["tid"])
        self.assertEqual((status, reopened["gen"]), (200, 1), reopened)
        self.reboot()
        self.assertEqual(self.reopen(P1["tid"]), (200, dict(reopened, repeated=True)))
        self.assertEqual([event["ev"] for event in self.events()].count("reopen"), 1)

    def test_late_confirm_after_reopen_is_refused(self):
        self.ready()
        self.assertEqual(self.confirm_target()[0], 200)
        self.assertEqual(self.reopen()[0], 200)
        self.reboot()
        self.ready()
        self.assert_refusal(self.confirm_target(), 409, "target_changed", status="open", gen=1,
                            mode=None)
        self.assertEqual(len(self.points()), 1)

    def test_other_mode_is_not_a_repeat(self):
        self.ready()
        self.assertEqual(self.confirm_target()[0], 200)
        self.assert_refusal(self.confirm_target("manual"), 409, "target_not_open",
                            status="confirmed", mode="verified", gen=0)
        self.assert_refusal(self.skip(), 409, "target_not_open", status="confirmed")
        self.assertEqual(len(self.points()), 1)

    def test_command_is_not_an_identity(self):
        self.ready()
        status, answer = self.confirm_target()
        self.assertEqual((status, answer.get("repeated")), (200, False), answer)
        # The same command for another target, then another action.
        status, answer = self.confirm_target("manual", P1["tid"])
        self.assertEqual((status, answer.get("repeated"), answer.get("tid")),
                         (200, False, P1["tid"]), answer)
        status, answer = self.reopen(P1["tid"])
        self.assertEqual((status, answer.get("repeated"), answer.get("gen")), (200, False, 1),
                         answer)
        # ... and for another project.
        other = self.other_project()
        status, answer = self.import_other(other)
        self.assertEqual(status, 200, answer)
        self.assertEqual(self.track("select_project", project_id=other)[0], 200)
        self.fresh_fix()
        status, answer = self.confirm_target("manual", answer["set_id"] + "/1/1",
                                             project_id=other)
        self.assertEqual((status, answer.get("repeated")), (200, False), answer)

    def test_refusals_change_nothing(self):
        self.selected()
        self.pairs(4)
        self.assert_error(self.confirm_target(), 409, "not_ready")
        self.pairs(1)
        status, answer = self.confirm_target()
        self.assertEqual((status, answer.get("repeated")), (200, False), answer)
        self.assertEqual(len(self.points()), 1)

    def test_invalid_requests(self):
        before = self.events()
        base = self.action_body("skip")
        for key, value in (("cmd", None), ("cmd", "0123456789abcde"),
                           ("cmd", "0123456789ABCDEF"), ("cmd", 1234567890123456),
                           ("expect_gen", None), ("expect_gen", -1), ("expect_gen", True),
                           ("expect_gen", "0"), ("expect_gen", 0.0), ("tid", None),
                           ("tid", "x")):
            with self.subTest(key=key, value=value):
                payload = dict(base)
                if value is None:
                    del payload[key]
                else:
                    payload[key] = value
                self.assert_error(self.post(payload), 400, "invalid_request")
        for mode in ("auto", None, 1):
            with self.subTest(mode=mode):
                self.assert_error(self.confirm_target(mode), 400, "invalid_request")
        self.assertEqual(self.events(), before)
        self.assertEqual(self.points(), [])

    def test_largest_request_fits_the_body_limit(self):
        request = self.action_body("confirm", "so-ffffffff/500/1000000",
                               stakeout_progress.GEN_LIMIT - 1, mode="manual",
                               project_id=self.pid, note="\U0001F600" * 500)
        self.assertLess(device_bytes(request), web_routing.DEFAULT_BODY_LIMIT)
        self.assert_error(self.post(json.dumps(request, ensure_ascii=False).encode()), 404,
                          "set_not_found")


class TestConfirmRaces(ConfirmFixture, unittest.TestCase):
    def assert_no_point(self, response, status, *codes):
        self.assertEqual(response[0], status, response)
        self.assertIn(response[1]["error"], codes, response)
        self.assertEqual(self.points(), [])

    def test_target_switch_is_refused(self):
        for path in ([OTHER_TID], [OTHER_TID, TID]):
            with self.subTest(path=path):
                if (self.so.target or {}).get("tid") != TID:
                    self.selected()
                self.ready()
                answer, answers = self.race(self.action_body("confirm", mode="verified"), *[
                    ("/api/stakeout", {"action": "select_target", "tid": tid}) for tid in path])
                for response in answers:
                    self.assertEqual(response[0], 200, response)
                self.assert_no_point(answer, 409, "not_ready")

    def test_project_switch_is_refused(self):
        other = self.other_project()
        for hold, mode in (("load", "verified"), ("find", "manual")):
            with self.subTest(mode=mode):
                self.assertEqual(self.track("select_project", project_id=self.pid)[0], 200)
                self.ready()
                answer, (switched,) = self.race(
                    self.action_body("confirm", mode=mode),
                    (TRACKING, {"action": "select_project", "project_id": other}), hold=hold)
                self.assertEqual(switched[0], 200, switched)
                self.assert_no_point(answer, 409, "project_not_active", "project_changed")

    def test_line_start_is_refused(self):
        self.ready()
        answer, _ = self.race(self.action_body("confirm", mode="verified"),
                              lambda: self.tracker.start_line("Line", 0, fix=dict(FIX)))
        self.assert_no_point(answer, 409, "recording_active")

    def test_measurement_start_is_refused(self):
        self.ready()
        patch = mock.patch.object(web.measurement_session, "active", return_value=True)
        self.addCleanup(patch.stop)
        answer, _ = self.race(self.action_body("confirm", mode="verified"), patch.start)
        self.assert_no_point(answer, 409, "measurement_active")

    def test_tolerance_change_binds_the_new_window(self):
        self.ready(0.08)
        before = self.so.window_fixes()
        after = []

        def change():
            cfg.CONFIG["stakeout_tolerance_m"] = 0.02
            self.assertEqual(self.state_now(), "fine")
            self.pairs(5)
            after.extend(self.so.window_fixes())

        (status, answer), _ = self.race(self.action_body("confirm", mode="verified"), change)
        self.assertEqual(status, 200, answer)
        self.assertEqual(len(after), 5)
        self.assertTrue({id(fix) for fix in after}.isdisjoint({id(fix) for fix in before}))
        mean = tracking.average_fixes(after)
        point, = self.points()
        self.assertEqual(point["coordinate"], [mean["lon_e8"], mean["lat_e8"], mean["alt_mm"]])
        self.assertEqual(answer["tolerance_m"], 0.02)

    def test_set_deletion_race_both_answers(self):
        self.fresh_fix()
        delete = {"action": "delete_set", "set_id": SET_ID, "expected_confirmed": 0}
        # (a) The confirmation holds the lock: the deletion is refused.
        (status, answer), (deleted,) = self.race(
            self.action_body("confirm", mode="manual"), ("/api/stakeout", delete), hold="find")
        self.assert_error(deleted, 409, "storage_busy")
        self.assertEqual((status, answer.get("status")), (200, "confirmed"), answer)
        self.assertTrue(os.path.exists(self.path()))
        # (b) The deletion holds the lock after its progress load: the
        # confirmation is refused, and its retry finds no set.
        request = self.action_body("confirm", P1["tid"], mode="manual")
        (status, answer), (confirmed,) = self.race(
            dict(delete, expected_confirmed=1), ("/api/stakeout", request))
        self.assert_error(confirmed, 409, "storage_busy")
        self.assertEqual((status, answer.get("deleted")), (200, SET_ID), answer)
        self.assert_error(self.post(request), 404, "set_not_found")
        self.assertEqual([point["stakeout_tid"] for point in self.points()], [TID])


class TestConfirmCrash(ConfirmFixture, unittest.TestCase):
    def test_power_cut_before_the_journal_write(self):
        self.ready()
        with mock.patch.object(tracking.Tracker, "_write_journal_event",
                               side_effect=PowerCut()):
            with self.assertRaises(PowerCut):
                self.confirm_target()
        self.reboot()
        self.assertIsNone(self.row())
        self.assertEqual(self.points(), [])
        self.ready()
        status, answer = self.confirm_target()
        self.assertEqual((status, answer.get("repeated")), (200, False), answer)
        self.assertEqual(len(self.points(TID)), 1)

    def test_power_cut_after_the_flushed_journal(self):
        self.ready()
        real = tracking.Tracker._apply

        def apply(tracker, event):
            if event.get("event") == "asset_added":
                tracker._sync_journal()
                raise PowerCut()
            return real(tracker, event)

        with mock.patch.object(tracking.Tracker, "_apply", apply):
            with self.assertRaises(PowerCut):
                self.confirm_target()
        self.reboot()
        point, = self.points()
        self.assertEqual(self.row(), [TID, "confirmed", 0, "verified", point["id"]])
        status, answer = self.confirm_target()
        self.assertEqual((status, answer.get("repeated"), answer.get("point_id")),
                         (200, True, point["id"]), answer)
        self.assertEqual(len(self.points()), 1)

    def test_power_cut_in_the_skip_append(self):
        self.selected()

        def torn(name, mode="r", *args):
            handle = _real_open(name, mode, *args)
            if "a" not in mode:
                return handle

            class Handle:
                def write(self, data):
                    handle.write(data[:len(data) // 2])
                    handle.close()
                    raise PowerCut()

                def close(self):
                    handle.close()

            return Handle()

        with mock.patch("stakeout_progress.open", torn, create=True):
            with self.assertRaises(PowerCut):
                self.skip()
        self.reboot()
        self.assertIsNone(self.row())
        status, answer = self.skip()
        self.assertEqual((status, answer.get("repeated")), (200, False), answer)
        self.assertEqual(self.row(), [TID, "skipped", 0, None, None])
        self.assertEqual(self.skip(), (200, dict(answer, repeated=True)))


class TestUncertainWrites(ConfirmFixture, unittest.TestCase):
    def assert_locked(self):
        self.assert_error(self.confirm_target(), 503, UNCERTAIN)
        self.assert_error(self.confirm_target("manual", P1["tid"]), 503, UNCERTAIN)
        self.assert_error(self.skip(P1["tid"]), 503, UNCERTAIN)
        self.assert_error(self.reopen(), 503, UNCERTAIN)

    def test_journal_write_failure_latches_until_restart(self):
        self.ready()
        with mock.patch.object(tracking.Tracker, "_write_journal_event",
                               side_effect=OSError(5, "io")):
            self.assert_error(self.confirm_target(), 503, UNCERTAIN)
        self.assertIsNotNone(self.store.uncertain)
        self.assertEqual(len(self.logged(stakeout_progress, "ERROR")), 1)
        self.assert_error(self.confirm_target(), 503, UNCERTAIN)
        self.assertEqual(self.points(), [])
        self.assert_locked()
        self.assert_error(self.import_(make_package(targets=2, offset=55)), 503, UNCERTAIN)
        self.assert_error(self.delete(), 503, UNCERTAIN)
        status, answer = self.select(P1["tid"])
        self.assertEqual((status, answer.get("remembered")), (200, False), answer)
        with mock.patch.object(measurement.measurement_session, "collect",
                               mock.AsyncMock(return_value=dict(FIX))):
            status, answer = self.track("add_asset", object_type="valve", name="V1")
        self.assertEqual(status, 200, answer)
        self.assertEqual(self.points(), [])
        self.reboot()
        self.assertIsNone(self.store.uncertain)
        self.ready()
        status, answer = self.confirm_target()
        self.assertEqual((status, answer.get("repeated")), (200, False), answer)
        self.assertEqual(len(self.points(TID)), 1)

    def test_apply_failure_after_the_journal(self):
        self.ready()
        real = tracking.Tracker._apply

        def apply(tracker, event):
            if event.get("event") == "asset_added":
                raise RuntimeError("apply")
            return real(tracker, event)

        with mock.patch.object(tracking.Tracker, "_apply", apply):
            self.assert_error(self.confirm_target(), 503, UNCERTAIN)
        self.assert_error(self.confirm_target(), 503, UNCERTAIN)
        self.assertEqual(self.points(), [])
        self.reboot()
        point, = self.points()
        self.assertEqual(self.row(), [TID, "confirmed", 0, "verified", point["id"]])
        status, answer = self.confirm_target()
        self.assertEqual((status, answer.get("repeated"), answer.get("point_id")),
                         (200, True, point["id"]), answer)
        self.assertEqual(len(self.points()), 1)

    def test_flush_failure_never_answers_from_ram(self):
        # Restart A keeps the journal line; restart B cuts the journal before it.
        for gen, cut in ((0, False), (1, True)):
            with self.subTest(cut=cut):
                self.ready()
                journal = self.journal_size()
                real = tracking.Tracker._sync_journal

                def sync(tracker, close=False, real=real):
                    # The flush after _apply fails; a close of no writer passes.
                    if not close:
                        raise OSError(5, "flush")
                    return real(tracker, close)

                with mock.patch.object(tracking.Tracker, "_sync_journal", sync):
                    self.assert_error(self.confirm_target(expect_gen=gen), 503, UNCERTAIN)
                    # The point is in RAM, the answer never comes from there.
                    self.assertEqual(len(self.points(TID)), gen + 1)
                    self.assert_error(self.confirm_target(expect_gen=gen), 503, UNCERTAIN)
                self.assertIn("write_uncertain", self.snapshot()["guidance"]["reasons"])
                self.reboot(journal if cut else None)
                self.ready()
                status, answer = self.confirm_target(expect_gen=gen)
                self.assertEqual((status, answer.get("repeated")), (200, not cut), answer)
                self.assertEqual([point["stakeout_gen"] for point in self.points(TID)],
                                 list(range(gen + 1)))
                if not cut:
                    self.assertEqual(self.reopen()[0], 200)

    def test_progress_append_failure_latches(self):
        for tid, complete in ((TID, True), (P1["tid"], False)):
            with self.subTest(complete=complete):
                size = self.progress_size()
                with failing_append("close"):
                    self.assert_error(self.skip(tid), 503, UNCERTAIN)
                self.assertIsNone(self.row(tid))
                self.assert_error(self.skip(tid), 503, UNCERTAIN)
                self.reboot(progress_size=None if complete else size)
                self.assertEqual(self.row(tid), [tid, "skipped", 0, None, None] if complete
                                 else None)
                status, answer = self.skip(tid)
                self.assertEqual((status, answer.get("repeated")), (200, complete), answer)

    def test_latch_survives_project_switches(self):
        self.ready()
        with failing_append("close"):
            self.assert_error(self.skip(P1["tid"]), 503, UNCERTAIN)
        other = self.other_project()
        for project_id in (other, self.pid):
            self.assertEqual(self.track("select_project", project_id=project_id)[0], 200)
        self.ready()
        self.assert_locked()
        self.assertIsNotNone(self.store.uncertain)


class TestConfirmShape(ConfirmFixture, unittest.TestCase):
    def test_commit_core_has_no_await(self):
        self.assertIs(inspect.iscoroutinefunction(web._stakeout_commit), False)
        core = ast.parse(inspect.getsource(web._stakeout_commit)).body[0]
        self.assertFalse([node for node in ast.walk(core)
                          if isinstance(node, (ast.Await, ast.AsyncFor, ast.AsyncWith))])
        calls = {}
        for node in ast.walk(core):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                calls.setdefault(node.func.attr, []).append(node.lineno)
        self.assertLess(max(calls["packet_window"]), min(calls["add_asset"]))

    def test_one_flash_write_per_block(self):
        counts = {"now": 0, "most": 0, "all": 0}

        def counted(real):
            def call(*args, **kwargs):
                counts["now"] += 1
                counts["all"] += 1
                counts["most"] = max(counts["most"], counts["now"])
                return real(*args, **kwargs)
            return call

        real_sleep = stakeout.asyncio.sleep_ms

        def sleep_ms(ms):
            counts["now"] = 0
            return real_sleep(ms)

        self.ready()
        self.assertEqual(self.skip(P1["tid"])[0], 200)
        steps = (("confirm verified", self.confirm_target),
                 ("reopen", self.reopen),
                 ("confirm manual", lambda: self.confirm_target("manual", expect_gen=1)),
                 ("reopen skipped", lambda: self.reopen(P1["tid"])),
                 ("skip", lambda: self.skip(P1["tid"], 1)),
                 ("delete_set", lambda: self.delete(expected_confirmed=1)))
        with mock.patch.object(tracking.Tracker, "_write_journal_event",
                               counted(tracking.Tracker._write_journal_event)), \
                mock.patch.object(stakeout_progress.ProgressLog, "append",
                                  counted(stakeout_progress.ProgressLog.append)), \
                mock.patch.object(os, "remove", counted(os.remove)), \
                mock.patch.object(os, "rename", counted(os.rename)), \
                mock.patch.object(os, "rmdir", counted(os.rmdir)), \
                mock.patch.object(stakeout.asyncio, "sleep_ms", sleep_ms):
            for name, step in steps:
                with self.subTest(step=name):
                    counts["now"] = counts["most"] = 0
                    response = step()
                    self.assertEqual(response[0], 200, response)
                    self.assertEqual(counts["most"], 1)
        self.assertGreaterEqual(counts["all"], len(steps))



# --- undo and survey restore reopen targets (Task A7) ----------------------------

NDJSON = "application/x-ndjson"


class _Recorder:
    """open() for stakeout_progress that records each appended write as
    (path, data); an append handle of a path ending in fail raises on close
    (after the real close, so the bytes are in the file)."""

    def __init__(self, fail=None):
        self.fail = fail
        self.writes = []

    def __call__(self, name, mode="r", *args):
        handle = _real_open(name, mode, *args)
        if "a" not in mode:
            return handle
        writes, failing = self.writes, self.fail is not None and name.endswith(self.fail)

        class Handle:
            def write(self, data):
                writes.append((name, data))
                return handle.write(data)

            def flush(self):
                handle.flush()

            def close(self):
                handle.close()
                if failing:
                    raise OSError(5, "close")

        return Handle()


class ReopenFixture(ConfirmFixture):
    """The confirm fixture with a write protocol, undo and both survey restore
    paths (NDJSON import and the restore action)."""

    @contextlib.contextmanager
    def writes(self, during_pause=None):
        """One list of the writes in order: ("progress", ev, event) for each
        ProgressLog.append (after it returned), ("journal", event name, event)
        for each journal event, ("sleep",) for each service pause.
        during_pause(log), if given, runs at each pause (a coroutine
        function: it may dispatch a request)."""
        log = []
        append = stakeout_progress.ProgressLog.append
        journal = tracking.Tracker._write_journal_event
        sleep_ms = stakeout.asyncio.sleep_ms

        def appended(progress, fold, event, *args, **kwargs):
            try:
                return append(progress, fold, event, *args, **kwargs)
            finally:
                log.append(("progress", event["ev"], event))

        def journaled(tracker, event):
            log.append(("journal", event.get("event"), event))
            return journal(tracker, event)

        async def slept(ms):
            log.append(("sleep", time.perf_counter()))
            if during_pause is not None:
                await during_pause(log)
            await sleep_ms(ms)

        with mock.patch.object(stakeout_progress.ProgressLog, "append", appended), \
                mock.patch.object(tracking.Tracker, "_write_journal_event", journaled), \
                mock.patch.object(stakeout.asyncio, "sleep_ms", slept):
            yield log

    @staticmethod
    def names(log):
        return [entry[1] if entry[0] != "sleep" else "sleep" for entry in log]

    def found(self, tid=TID, expect_gen=0):
        """A manual confirmation of tid at a fresh position; its answer."""
        self.fresh_fix()
        status, answer = self.confirm_target("manual", tid, expect_gen)
        self.assertEqual(status, 200, answer)
        return answer

    def undo(self, **fields):
        return self.track("undo", **fields)

    def expect(self):
        """The undo candidate a client sees (GET /api/tracking undo_candidate)."""
        return self.tracker.undo_candidate()

    def valve(self):
        return self.tracker.add_asset("valve", "V1", "", dict(FIX), False)

    def survey_ids(self):
        return [point["id"] for point in web._stakeout_points(self.pid)]

    def ndjson(self):
        return "".join(self.tracker.iter_backup_ndjson()).encode()

    def restore_ndjson(self, raw):
        status, _head, payload = self.call("POST", "/api/tracking/import", raw,
                                           content_type=NDJSON)
        return status, json.loads(payload)

    def restore_json(self, backup):
        return self.track("restore", backup=backup)

    def restored(self, project_id=None):
        """The floors of the restored events in the progress of project_id."""
        return [event["floor"] for event in self.events(project_id)
                if event["ev"] == "restored"]

    def floors(self):
        status, answer = self.progress_list()
        self.assertEqual(status, 200, answer)
        return answer["floors"]

    def refused_again(self, gen, expect_gen=0, tid=TID):
        """The lost manual confirmation at expect_gen again: 409
        target_changed at gen, and no point."""
        count = len(self.points(tid))
        self.fresh_fix()
        self.assert_refusal(self.confirm_target("manual", tid, expect_gen), 409,
                            "target_changed", gen=gen, status="open")
        self.assertEqual(len(self.points(tid)), count)

    def held(self, *requests):
        """The answers of requests ((path, payload[, content type]) each) sent
        while the change lock is held."""
        async def scenario():
            async with self.store.mutation():
                return [await self.dispatch(*request) for request in requests]

        return asyncio.run(scenario())


class TestUndoReopens(ReopenFixture, unittest.TestCase):
    def test_lost_answer_then_undo_then_same_request(self):
        cases = (("manual", False), ("manual", True), ("verified", False))
        for gen, (mode, restart) in enumerate(cases):
            with self.subTest(mode=mode, restart=restart):
                if mode == "manual":
                    self.fresh_fix()
                else:
                    self.ready()
                request = self.action_body("confirm", expect_gen=gen, mode=mode)
                # The answer is lost; the browser sends the same request later.
                status, answer = self.post(request)
                self.assertEqual((status, answer.get("gen")), (200, gen), answer)
                with self.writes() as log:
                    status, answer = self.undo(expect=self.expect())
                self.assertEqual(status, 200, answer)
                writes = [entry for entry in log if entry[0] != "sleep"]
                self.assertEqual([entry[1] for entry in writes], ["reopen", "feature_removed"])
                reopen = writes[0][2]
                self.assertEqual((reopen["tid"], reopen["gen"], reopen["by"]),
                                 (TID, gen + 1, "undo"))
                self.assertNotIn("cmd", reopen)
                self.assertEqual(self.events()[-1], reopen)
                self.assertEqual(self.row(), [TID, "open", gen + 1, None, None])
                if restart:
                    self.reboot()
                    self.assertEqual(self.row(), [TID, "open", gen + 1, None, None])
                status, answer = self.post(request)
                self.assert_refusal((status, answer), 409, "target_changed", gen=gen + 1,
                                    status="open")
                self.assertEqual(self.points(), [])

    def test_legacy_undo_without_expect_uses_the_checked_candidate(self):
        self.found()
        with self.writes() as log:
            self.assertEqual(self.undo()[0], 200)
        self.assertEqual([name for name in self.names(log) if name != "sleep"],
                         ["reopen", "feature_removed"])
        self.assertEqual(self.points(), [])
        self.assertEqual(self.row(), [TID, "open", 1, None, None])
        # A second request records a valve point in the pause after the
        # reopen append: the candidate changed, nothing is removed.
        self.found(expect_gen=1)
        valves = []

        async def record_valve(log):
            if "reopen" in self.names(log) and not valves:
                valves.append(self.valve())

        with self.writes(record_valve) as log:
            self.assert_error(self.undo(), 409, "undo_candidate_changed")
        self.assertEqual(len(valves), 1)
        self.assertNotIn("feature_removed", self.names(log))
        point, = self.points(TID)
        self.assertEqual(point["stakeout_gen"], 1)
        self.assertIn(valves[0]["id"], self.survey_ids())
        # The target is open at the new generation; the old point does not count.
        self.assertEqual(self.row(), [TID, "open", 2, None, None])
        self.refused_again(2, 1)

    def test_stale_expectation_appends_nothing(self):
        answer = self.found()
        before = self.events()
        expect = dict(self.expect())
        expect["record_order"] += 1
        self.assert_error(self.undo(expect=expect), 409, "undo_candidate_changed")
        self.assertEqual(self.events(), before)
        self.assertEqual([point["id"] for point in self.points(TID)], [answer["point_id"]])
        self.assertEqual(self.row(), [TID, "confirmed", 0, "manual", answer["point_id"]])

    def test_undo_of_a_history_point_appends_nothing(self):
        self.found()
        self.assertEqual(self.reopen()[0], 200)
        before = self.events()
        self.assertEqual(self.undo(expect=self.expect())[0], 200)
        self.assertEqual(self.events(), before)
        self.assertEqual(self.points(), [])
        self.assertEqual(self.row(), [TID, "open", 1, None, None])

    def test_undo_of_other_points_needs_no_lock(self):
        answer = self.found()
        first, second = self.valve(), self.valve()
        (response,) = self.held((TRACKING, {"action": "undo"}))
        self.assertEqual(response[0], 200, response)
        self.store.uncertain = "test"
        self.assertEqual(self.undo(expect=self.expect())[0], 200)
        ids = self.survey_ids()
        self.assertNotIn(first["id"], ids)
        self.assertNotIn(second["id"], ids)
        self.assertIn(answer["point_id"], ids)

    def test_stakeout_undo_needs_the_lock_and_no_latch(self):
        answer = self.found()
        before = self.events()
        (response,) = self.held((TRACKING, {"action": "undo", "expect": self.expect()}))
        self.assert_error(response, 409, "storage_busy")
        self.store.uncertain = "test"
        self.assert_error(self.undo(expect=self.expect()), 503, UNCERTAIN)
        self.store.uncertain = None
        self.assertEqual(self.events(), before)
        self.assertIn(answer["point_id"], self.survey_ids())
        with self.writes() as log, failing_append("flush"):
            self.assert_error(self.undo(expect=self.expect()), 503, UNCERTAIN)
        self.assertIsNotNone(self.store.uncertain)
        self.assertIn(answer["point_id"], self.survey_ids())
        self.assertNotIn("feature_removed", self.names(log))

    def test_failed_undo_after_the_reopen_leaves_the_target_open(self):
        answer = self.found()
        with mock.patch.object(tracking.Tracker, "undo_last", side_effect=OSError(5, "io")):
            self.assert_error(self.undo(expect=self.expect()), 507, "tracking_storage_failed")
        self.assertEqual(self.row(), [TID, "open", 1, None, None])
        self.assertIn(answer["point_id"], self.survey_ids())
        self.refused_again(1)

    def undo_disturbed(self, hold, disturb):
        """(undo answer, what disturb returned): the undo of the candidate
        while disturb() (a coroutine function) runs once at hold: "load"
        right after the progress load, "pause" in the pause after the
        reopen append."""
        async def scenario():
            done = []

            async def once():
                if not done:
                    done.append(await disturb())

            real = web._stakeout_load

            async def loaded(*args):
                fold = await real(*args)
                if hold == "load":
                    await once()
                return fold

            async def paused(log):
                if hold == "pause" and "reopen" in self.names(log):
                    await once()

            expect = self.expect()
            with mock.patch.object(web, "_stakeout_load", loaded), self.writes(paused):
                answer = await self.dispatch(TRACKING, {"action": "undo", "expect": expect})
                self.assertEqual(len(done), 1)
                result = done[0]
                if isinstance(result, tuple):
                    task, release = result
                    release.set()
                    result = await asyncio.wait_for(task, 5)
                elif isinstance(result, asyncio.Future):
                    result = await asyncio.wait_for(result, 5)
            return answer, result

        return asyncio.run(scenario())

    def test_maintenance_requested_during_a_stakeout_undo_is_refused(self):
        gen = 0
        for hold in ("load", "pause"):
            for action in ("compact", "reactivate_archive"):
                with self.subTest(hold=hold, action=action):
                    answer = self.found(expect_gen=gen)
                    gen += 1

                    async def maintenance():
                        task = asyncio.ensure_future(self.dispatch(
                            TRACKING, {"action": action, "archive_id": "a"}))
                        for _ in range(100):
                            if task.done() or self.tracker.maintenance_active():
                                break
                            await asyncio.sleep(0)
                        return task

                    response, refused = self.undo_disturbed(hold, maintenance)
                    self.assert_error(refused, 409, "storage_busy")
                    self.assertEqual(response[0], 200, response)
                    self.assertNotIn(answer["point_id"], self.survey_ids())
                    self.assertEqual(self.row(), [TID, "open", gen, None, None])

    def test_undo_refuses_when_maintenance_began_during_a_yield(self):
        for hold, gen in (("load", 0), ("pause", 1)):
            with self.subTest(hold=hold):
                answer = self.found(expect_gen=gen)
                before = self.events()

                async def maintenance():
                    entered, release = asyncio.Event(), asyncio.Event()

                    async def run():
                        async with self.tracker.maintenance("test"):
                            entered.set()
                            await release.wait()

                    task = asyncio.ensure_future(run())
                    await asyncio.wait_for(entered.wait(), 5)
                    return task, release

                response, _ = self.undo_disturbed(hold, maintenance)
                self.assert_error(response, 409, "storage_busy")
                self.assertIn(answer["point_id"], self.survey_ids())
                if hold == "load":
                    self.assertEqual(self.events(), before)
                else:
                    # Like a failing undo: the reopen stays, the target is open.
                    self.assertEqual(self.row(), [TID, "open", gen + 1, None, None])
                self.assertEqual(self.undo(expect=self.expect())[0], 200)
                self.assertNotIn(answer["point_id"], self.survey_ids())

    def test_undo_paces(self):
        self.found()
        with self.writes() as log:
            self.assertEqual(self.undo(expect=self.expect())[0], 200)
        names = self.names(log)
        self.assertIn("sleep", names[names.index("reopen"):names.index("feature_removed")])


class TestRestoreFloor(ReopenFixture, unittest.TestCase):
    def test_lost_answer_then_restore_then_same_request(self):
        # The backups hold no stakeout point.
        raw, backup = self.ndjson(), self.tracker.backup()
        cases = (("ndjson", False), ("ndjson", True), ("json", False))
        for gen, (path, restart) in enumerate(cases):
            with self.subTest(path=path, restart=restart):
                # The answer is lost; the browser sends the same request later.
                self.found(expect_gen=gen)
                response = (self.restore_ndjson(raw) if path == "ndjson"
                            else self.restore_json(backup))
                self.assertEqual(response[0], 200, response)
                last = self.events()[-1]
                self.assertEqual((last["ev"], last["floor"]), ("restored", gen + 1))
                if restart:
                    self.reboot()
                self.assertEqual(self.points(), [])
                self.assertIsNone(self.row())
                self.assertEqual(self.floors(), {SET_ID: gen + 1})
                self.refused_again(gen + 1, gen)

    def test_floor_exceeds_every_known_generation(self):
        t1 = P1["tid"]
        for gen in (0, 1):
            self.assertEqual(self.skip(t1, gen)[0], 200)
            self.assertEqual(self.reopen(t1, gen)[0], 200)
        for gen in (0, 1):
            self.found(expect_gen=gen)
            self.assertEqual(self.reopen(TID, gen)[0], 200)
        answer = self.found(expect_gen=2)
        backup = self.tracker.backup()
        second = self.second_set()
        for gen in (0, 1):
            self.assertEqual(self.skip(second + "/1/1", gen)[0], 200)
            self.assertEqual(self.reopen(second + "/1/1", gen)[0], 200)
        self.assertEqual(self.delete(second)[0], 200)
        self.assertEqual([event["floor"] for event in self.events()
                          if event["ev"] == "set_removed"], [3])
        self.assertEqual(self.restore_json(backup)[0], 200)
        self.assertEqual(self.restored(), [4])
        status, listed = self.progress_list()
        self.assertEqual((status, listed["floors"], listed["states"]),
                         (200, {SET_ID: 4}, []))
        point = [point for point in self.points(TID) if point["id"] == answer["point_id"]]
        self.assertEqual([item["stakeout_gen"] for item in point], [2])
        self.refused_again(4, 2)

    def test_affected_projects_only(self):
        self.assertEqual(self.skip(P1["tid"])[0], 200)
        with_set = self.other_project("Set only")
        self.assertEqual(self.import_other(with_set)[0], 200)
        self.assertFalse(os.path.exists(self.progress_path(with_set)))
        without = self.other_project("No folder")
        raw = self.ndjson()
        # A project the backup does not hold.
        dropped = self.other_project("Dropped")
        status, imported = self.import_other(dropped)
        self.assertEqual(status, 200, imported)
        recorder = _Recorder()
        with mock.patch("stakeout_progress.open", recorder, create=True):
            response = self.restore_ndjson(raw)
        self.assertEqual(response[0], 200, response)
        self.assertEqual((self.restored(), self.restored(with_set), self.restored(dropped)),
                         ([1], [1], [1]))
        writes = [data for name, data in recorder.writes
                  if name.endswith("/%s/progress.jsonl" % with_set)]
        self.assertEqual(len(writes), 1)
        header, event = [json.loads(line) for line in writes[0].decode().splitlines()]
        self.assertEqual(header, {"kind": "stakeout_progress", "schema": 1,
                                  "project_id": with_set,
                                  "project_created_at": self.fp(with_set)})
        self.assertEqual((event["ev"], event["floor"]), ("restored", 1))
        self.assertFalse(os.path.exists(os.path.join(self.root, without)))
        self.assertNotIn(dropped, [project["id"] for project in self.tracker.projects])
        self.assertTrue(os.path.isfile(os.path.join(self.root, dropped,
                                                    imported["set_id"] + ".json")))

    def test_invalid_backup_writes_no_floor(self):
        self.assertEqual(self.skip(P1["tid"])[0], 200)
        before = self.events()
        lines = self.ndjson().decode().splitlines()
        integrity = json.loads(lines[-1])
        integrity["content_sha256"] = "0" * 64
        broken = ("\n".join(lines[:-1] + [json.dumps(integrity)]) + "\n").encode()
        for name, raw in (("integrity", broken), ("json", b"{nope\n")):
            with self.subTest(backup=name):
                self.assert_error(self.restore_ndjson(raw), 400, "invalid_backup")
        self.assert_error(self.restore_json({"projects": "x"}), 400,
                          "invalid_tracking_request")
        self.assertEqual(self.events(), before)
        self.assertEqual(self.row(P1["tid"]), [P1["tid"], "skipped", 0, None, None])

    def test_floor_is_durable_before_publication(self):
        raw = self.ndjson()
        answer = self.found()
        with mock.patch.object(tracking.Tracker, "_write_restore_commit",
                               side_effect=PowerCut()):
            with self.assertRaises(PowerCut):
                self.restore_ndjson(raw)
        self.reboot()
        # The old survey, the floor in front of it (accepted cost, open question 1).
        self.assertEqual([point["id"] for point in self.points(TID)], [answer["point_id"]])
        self.assertEqual(self.restored(), [1])
        self.assertIsNone(self.row())
        self.assertEqual(self.floors(), {SET_ID: 1})
        self.refused_again(1)

    def test_floor_append_failure_aborts_the_restore(self):
        self.assertEqual(self.skip(P1["tid"])[0], 200)
        other = self.other_project("B")
        self.assertEqual(self.import_other(other)[0], 200)
        raw, backup = self.ndjson(), self.tracker.backup()
        valve = self.valve()
        for path in ("ndjson", "json"):
            with self.subTest(path=path):
                recorder = _Recorder("/%s/progress.jsonl" % other)
                with mock.patch("stakeout_progress.open", recorder, create=True):
                    response = (self.restore_ndjson(raw) if path == "ndjson"
                                else self.restore_json(backup))
                self.assert_error(response, 503, UNCERTAIN)
                self.assertTrue(recorder.writes[-1][0].endswith(recorder.fail))
                self.assertFalse(self.tracker._restore_published())
                self.assertIn(valve["id"], self.survey_ids())
                self.assertIsNotNone(self.store.uncertain)
                # A new start clears the latch.
                self.restart()

    def test_reactivation_writes_no_floor(self):
        self.assertEqual(self.skip(P1["tid"])[0], 200)
        other = self.other_project()
        self.assertEqual(self.track("select_project", project_id=other)[0], 200)
        self.assertEqual(self.track("archive_project", project_id=self.pid)[0], 200)
        before = self.events()
        self.assertTrue(before)
        hooks = []
        real = tracking.Tracker._write_restore_commit

        def commit(tracker, staging):
            hooks.append(tracker.before_restore_publish)
            return real(tracker, staging)

        with mock.patch.object(tracking.Tracker, "_write_restore_commit", commit):
            status, answer = self.track("reactivate_archive",
                                        archive_id=self.tracker.archives[-1]["id"])
        self.assertEqual(status, 200, answer)
        self.assertEqual(hooks, [None])
        self.assertEqual(self.events(), before)

    def test_restore_under_the_latch_or_lock_is_refused(self):
        raw, backup = self.ndjson(), self.tracker.backup()
        valve = self.valve()
        self.store.uncertain = "test"
        self.assert_error(self.restore_ndjson(raw), 503, UNCERTAIN)
        self.assert_error(self.restore_json(backup), 503, UNCERTAIN)
        self.assertIn(valve["id"], self.survey_ids())
        self.store.uncertain = None
        # The lock as an import holds it.
        for response in self.held(("/api/tracking/import", raw, NDJSON),
                                  (TRACKING, {"action": "restore", "backup": backup})):
            self.assert_error(response, 409, "storage_busy")
        self.assertIn(valve["id"], self.survey_ids())
        self.assertEqual(self.restored(), [])

    def test_restore_holds_the_lock(self):
        raw = self.ndjson()
        self.assertEqual(self.skip(P1["tid"])[0], 200)
        answers = []

        async def confirm(log):
            if "restored" in self.names(log) and not answers:
                self.fresh_fix()
                answers.append(await self.dispatch(
                    "/api/stakeout", self.action_body("confirm", mode="manual")))

        with self.writes(confirm):
            status, answer = asyncio.run(self.dispatch("/api/tracking/import", raw, NDJSON))
        self.assertEqual(status, 200, answer)
        self.assertEqual(len(answers), 1)
        self.assert_error(answers[0], 409, "storage_busy")
        self.assertEqual(self.points(), [])
        self.assertIsNone(self.tracker.before_restore_publish)
        self.assertIsNone(self.fold())

    def test_restore_during_a_confirm_both_answers(self):
        raw = self.ndjson()
        self.fresh_fix()
        (status, answer), (restored,) = self.race(
            self.action_body("confirm", mode="manual"),
            ("/api/tracking/import", raw, NDJSON), hold="find")
        self.assert_error(restored, 409, "storage_busy")
        self.assertEqual(self.restored(), [])
        self.assertEqual((status, answer.get("status")), (200, "confirmed"), answer)
        self.assertEqual(len(self.points(TID)), 1)

    def test_floor_steps_pace(self):
        for number in range(19):
            project_id = self.other_project("Project %d" % number)
            self.assertEqual(self.import_other(project_id)[0], 200)
        raw = self.ndjson()
        with self.writes() as log:
            self.assertEqual(self.restore_ndjson(raw)[0], 200)
        appends = [index for index, entry in enumerate(log) if entry[0] == "progress"]
        self.assertEqual([log[index][1] for index in appends], ["restored"] * 20)
        longest = 0
        for index in appends:
            before = max(i for i in range(index) if log[i][0] == "sleep")
            after = min(i for i in range(index + 1, len(log)) if log[i][0] == "sleep")
            # One append per block, between two pauses.
            self.assertEqual([entry[0] for entry in log[before + 1:after]].count("progress"), 1)
            longest = max(longest, log[after][1] - log[before][1])
        self.assertLess(longest, 0.25)


if __name__ == "__main__":
    unittest.main()
