# SPDX-License-Identifier: AGPL-3.0-only
"""#39: assign a reference frame to a project's RTK points that have none."""
import asyncio
import csv
import hashlib
import inspect
import io
import json
import math
import os
import shutil
import tempfile
import unittest
import warnings
from unittest import mock

import support
from support import state  # noqa: F401 - MicroPython compatibility
import cfg
import cfg_validation
import pointstore
import tracking
from powercut import Injector, PowerCut
from test_restore_entry import fix
from test_tracking import FIX
import tracking_reference

EXAMPLE = "ETRS89 (example)"
SYNTHETIC = "ETRS89 (synthetic)"
RESERVED = 'The frame names "unknown" and "mixed" are reserved.'
STORAGE = {"free_flash_bytes": 8_000_000, "required_flash_reserve_bytes": 3_000_000,
           "estimated_temporary_bytes": 16_384, "writable": True}
# Survey data as V15.8.1 (schema 4) wrote it: float degrees, no reference frame.
LEGACY_MEASUREMENT = {"recorded_at": 3, "fix_quality": 4, "fix_status": "RTK_FIXED",
                      "satellites": 24, "hdop": 0.6, "correction_age_sec": 0.5,
                      "station_id": "0054", "source": "start",
                      "quality": {"accepted": True, "reasons": []}}
DGPS_FIX = dict(FIX, qual=2, fix_status_text="DGPS")
READ_PATHS = ("read", "iter_line", "_iter_line_locators", "_decode")


def legacy_backup(lines, points=(), line_state="finished", active_line=None):
    """The NDJSON lines of a schema-4 backup of project p1, integrity record included.

    lines holds (line_id, name, [measurement fields per vertex]); points holds
    the measurement fields per object point. Every vertex and point gets its
    own float position; all lines are in line_state (finished by default) and
    active_line names the active line (none by default).
    """
    rows = [{"kind": "survey_backup", "schema_version": 4, "record": "header", "profiles": []},
            {"record": "project", "project": {"id": "p1", "name": "Old", "description": "",
                                              "created_at": 1}}]
    positions = iter(range(len(points) + sum(len(vertices) for _id, _name, vertices in lines)))

    def measured(fields):
        index = next(positions)
        return ([8.0 + index * 0.00002, 50.0, 100.0],
                dict(LEGACY_MEASUREMENT, recorded_at=3 + index, **fields))
    for line_id, name, vertices in lines:
        rows.append({"record": "line", "project_id": "p1", "line": {
            "id": line_id, "name": name, "state": line_state, "created_at": 2,
            "finished_at": 6 if line_state == "finished" else None, "properties": {}}})
        for fields in vertices:
            coordinate, measurement = measured(fields)
            rows.append({"record": "vertex", "project_id": "p1", "line_id": line_id,
                         "coordinate": coordinate, "measurement": measurement})
    for number, fields in enumerate(points, 1):
        coordinate, measurement = measured(fields)
        rows.append({"record": "asset", "project_id": "p1", "point": {
            "id": "point-%d" % number, "object_type": "valve", "name": "V-%d" % number,
            "note": "", "line_id": None, "chainage_m": None, "coordinate": coordinate,
            "measurement": dict(measurement, source="asset")}})
    rows.append({"record": "footer", "active_project_id": "p1", "active_line_id": active_line,
                 "auto_distance_m": 1.0})
    raws = [json.dumps(row) + "\n" for row in rows]
    digest = hashlib.sha256("".join(raws).encode("utf-8")).hexdigest()
    return raws + [json.dumps({"record": "integrity", "algorithm": "sha256",
                               "content_sha256": digest, "records": len(raws)}) + "\n"]


def digests(folder):
    """Relative path -> SHA-256 of every file below folder."""
    found = {}
    for root, _, names in os.walk(folder):
        for name in names:
            full = os.path.join(root, name)
            with open(full, "rb") as source:
                found[os.path.relpath(full, folder)] = hashlib.sha256(source.read()).hexdigest()
    return found


def journal_text(tracker):
    """The tracker's journal segments as one text."""
    tracker._sync_journal(close=False)
    text = ""
    for _number, path in tracker._segment_files():
        with open(path, encoding="utf-8") as source:
            text += source.read()
    return text


def journal_events(tracker):
    return [json.loads(raw) for raw in journal_text(tracker).splitlines() if raw.strip()]


class FrameFixture:
    """A tracker in a temporary folder with an empty field setup (no frame configured)."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="esp-rtk-frames-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        catcher = warnings.catch_warnings()
        catcher.__enter__(); self.addCleanup(catcher.__exit__, None, None, None)
        warnings.simplefilter("ignore", ResourceWarning)
        for patch in (mock.patch.object(tracking.Tracker, "storage_capacity",
                                        return_value=dict(STORAGE)),
                      support.patch_module_global("tracking", "log"),
                      mock.patch.dict(cfg.CONFIG, {"reference_frame": "",
                                                   "reference_frame_epoch": ""}),
                      mock.patch("uasyncio.sleep_ms", lambda ms: asyncio.sleep(0))):
            patch.start(); self.addCleanup(patch.stop)
        self.t = self.tracker("survey")

    def tracker(self, name):
        tracker = tracking.Tracker(os.path.join(self.tmp, name))
        self.addCleanup(self.close, tracker)
        return tracker

    @staticmethod
    def close(tracker):
        try: tracker._sync_journal(close=True); tracker.point_store.close()
        except Exception: pass

    def reload(self, tracker):
        self.close(tracker)
        reloaded = tracking.Tracker(tracker.path)
        self.addCleanup(self.close, reloaded)
        return reloaded

    @staticmethod
    def run_async(coro):
        return asyncio.run(coro)

    def legacy_project(self, tracker=None):
        """Project p1 as after an upgrade from V15.8.1, then measured on with an empty field setup.

        Line l0 "L" (restored, finished): 3 RTK vertices and 1 DGPS vertex, no
        frame. Object points: a restored RTK point without a frame, an RTK
        point recorded in "ETRS89 (synthetic)", a DGPS point (WGS84/ITRF).
        Line "L1" (paused, the active line): 2 RTK vertices without a frame,
        the latest records.
        """
        t = tracker or self.t
        t.restore_ndjson(legacy_backup(
            [("l0", "L", [{}, {}, {}, {"fix_quality": 2, "fix_status": "DGPS/SBAS"}])],
            [{"fix_quality": 5, "fix_status": "RTK_FLOAT"}]))
        with mock.patch.dict(cfg.CONFIG, {"reference_frame": SYNTHETIC}):
            t.add_asset("tap", "R", "", fix(40), link_to_active_line=False)
        with mock.patch.dict(cfg.CONFIG, {"measurement_required_fix": "DGPS"}):
            t.add_asset("tap", "D", "", dict(DGPS_FIX, lon_e8=800082000), link_to_active_line=False)
        t.start_line("L1")
        t.add_vertex(fix(42)); t.add_vertex(fix(43))
        t.set_line_state("pause")
        return "p1"

    def preview(self, pid, tracker=None):
        return self.run_async((tracker or self.t).frame_assignment_preview_async(pid))

    def assign(self, pid, frame, epoch="", tracker=None):
        t = tracker or self.t
        shown = self.preview(pid, t)
        return self.run_async(t.assign_frame_async(pid, frame, epoch, shown["total"],
                                                   shown["scope_token"]))

    def frames(self, tracker=None):
        return (tracker or self.t).status()["projects"][0]["frames"]

    def add_rtk_vertex(self, index, tracker=None):
        """One more RTK vertex at the end of the paused active line, which stays paused."""
        t = tracker or self.t
        t.set_line_state("resume"); t.add_vertex(fix(index)); t.set_line_state("pause")

    def other_project_recording(self, tracker):
        """Finish L1 and start recording in a second project (the active one now)."""
        tracker.set_line_state("finish")
        tracker.create_project("Other")
        tracker.start_line("O1"); tracker.add_vertex(fix(70))

    def scan_with(self, action, windows=1):
        """Run action after the first batch of every frame scan, one window per batch."""
        original = tracking.Tracker._frame_scope_steps

        def steps(tracker, project):
            inner, done = original(tracker, project), False
            try:
                for item in inner:
                    yield item
                    if item is None and not done:
                        done = True; action()
            finally:
                inner.close()
        return [mock.patch.object(tracking.Tracker, "_frame_scope_steps", steps),
                support.patch_module_global("tracking", "FRAME_SCAN_WINDOWS", windows)]

    def assert_scope_refused(self, tracker, pid, shown, code="frame_scope_changed"):
        before = json.dumps(tracker._project(pid).get("frame_assignment"))
        with self.assertRaises(tracking.FrameAssignmentError) as caught:
            self.run_async(tracker.assign_frame_async(pid, "A", "", shown["total"],
                                                      shown["scope_token"]))
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(json.dumps(tracker._project(pid).get("frame_assignment")), before)


class FrameAssignmentModelTests(FrameFixture, unittest.TestCase):
    def test_preview_counts_only_rtk_points_without_a_frame(self):
        pid = self.legacy_project()
        line1 = self.t._project(pid)["lines"][1]["id"]
        before = digests(self.tmp)
        shown = self.preview(pid)
        self.assertEqual(digests(self.tmp), before)
        self.assertEqual(dict((key, shown[key]) for key in (
            "project_id", "total", "new_unknown", "vertices", "points", "other_unknown",
            "more_lines")), {"project_id": pid, "total": 6, "new_unknown": 6, "vertices": 5,
                             "points": 1, "other_unknown": 1, "more_lines": 0})
        self.assertEqual(shown["lines"], [{"id": "l0", "name": "L", "count": 3},
                                          {"id": line1, "name": "L1", "count": 2}])
        self.assertIsNone(shown["assignment"])
        self.assertRegex(shown["scope_token"], r"^[0-9a-f]{16}$")

    def test_assigning_turns_unknown_rtk_points_into_the_frame(self):
        pid = self.legacy_project()
        project = self.t._project(pid)
        line1 = project["lines"][1]["id"]
        self.assertEqual(self.frames(), {"unknown": 7, SYNTHETIC: 1, "WGS84/ITRF": 1})
        result = self.assign(pid, " ETRS89 (example) ")
        frames = {EXAMPLE: 6, "unknown": 1, SYNTHETIC: 1, "WGS84/ITRF": 1}
        self.assertEqual((result["project_id"], result["frames"], result["mixed_frames"],
                          result["unknown_frames"]), (pid, frames, True, False))
        summary = result["frame_assignment"]
        self.assertEqual((summary["reference_frame"], summary["reference_frame_epoch"],
                          summary["count"]), (EXAMPLE, None, 6))
        self.assertEqual(project["frame_assignment"]["points"], [3, 1])
        self.assertEqual(project["frame_assignment"]["lines"], {"l0": [4, 3], line1: [2, 2]})
        self.assertEqual(self.t.status()["projects"][0]["frame_assignment"], summary)
        self.assertEqual(self.frames(), frames)
        self.assertEqual(self.t.live_status()["active_project"]["frames"], frames)
        # Stored measurements keep their empty frame (E1).
        self.assertIsNone(project["points"][0]["measurement"].get("reference_frame"))
        self.assertIsNone(project["lines"][1]["measurements"][0].get("reference_frame"))
        # A project of RTK points only loses its warning entirely.
        self.t.set_line_state("finish")
        other = self.t.create_project("Only RTK")["id"]
        self.t.start_line("R1"); self.t.add_vertex(fix(60)); self.t.add_vertex(fix(61))
        self.t.set_line_state("pause")
        self.t.add_asset("tap", "", "", fix(62), link_to_active_line=False)
        before = self.t.status()["projects"][1]
        self.assertEqual((before["frames"], before["unknown_frames"]), ({"unknown": 3}, True))
        result = self.assign(other, "ETRS89 (other)")
        self.assertEqual((result["frames"], result["mixed_frames"], result["unknown_frames"]),
                         ({"ETRS89 (other)": 3}, False, False))
        self.assertEqual(self.frames(), frames)

    def test_points_recorded_after_the_assignment_stay_unknown(self):
        pid = self.legacy_project()
        self.assign(pid, EXAMPLE)
        self.add_rtk_vertex(44)
        self.t.add_asset("tap", "", "", fix(45), link_to_active_line=False)
        self.assertEqual(self.frames()["unknown"], 3)
        self.assertEqual(self.frames()[EXAMPLE], 6)
        # The kept tally and one rebuilt from the line summaries agree.
        self.t._project_frames.clear(); self.t._status_cache = None
        self.assertEqual(self.frames(), {EXAMPLE: 6, "unknown": 3, SYNTHETIC: 1, "WGS84/ITRF": 1})
        shown = self.preview(pid)
        self.assertEqual((shown["total"], shown["new_unknown"], shown["assignment"]["count"]),
                         (8, 2, 6))

    def test_a_second_assignment_replaces_the_first_and_records_the_previous(self):
        pid = self.legacy_project()
        first = self.assign(pid, "A")["frame_assignment"]
        self.add_rtk_vertex(44)
        shown = self.preview(pid)
        self.assertEqual((shown["total"], shown["new_unknown"]), (7, 1))
        result = self.run_async(self.t.assign_frame_async(pid, "B", "", 7, shown["scope_token"]))
        self.assertEqual(result["frames"], {"B": 7, "unknown": 1, SYNTHETIC: 1, "WGS84/ITRF": 1})
        self.assertEqual(result["frame_assignment"]["count"], 7)
        event = journal_events(self.t)[-1]
        self.assertEqual(event["event"], "project_frame_assigned")
        self.assertEqual(event["previous"], {"reference_frame": "A", "reference_frame_epoch": None,
                                             "assigned_at": first["assigned_at"], "count": 6})
        self.assertEqual((event["count"], event["assignment"]["reference_frame"]), (7, "B"))

    def test_revoking_brings_back_the_recorded_frames(self):
        pid = self.legacy_project()
        before = self.frames()
        self.assign(pid, EXAMPLE)
        token = self.t.status()["projects"][0]["frame_assignment"]["token"]
        result = self.t.revoke_frame_assignment(pid, token)
        self.assertEqual((result["frames"], result["frame_assignment"]), (before, None))
        self.assertNotIn("frame_assignment", self.t._project(pid))
        self.assertNotIn("frame_assignment", self.t.status()["projects"][0])
        self.assertEqual(self.frames(), before)
        event = journal_events(self.t)[-1]
        self.assertEqual((event["event"], event["assignment"], event["previous"]["count"],
                          event["count"]), ("project_frame_assigned", None, 6, 0))

    def test_the_assignment_survives_restart_and_compaction(self):
        pid = self.legacy_project()
        unassigned = self.frames()
        summary = self.assign(pid, EXAMPLE)["frame_assignment"]
        frames = self.frames()
        stored = json.loads(json.dumps(self.t._project(pid)["frame_assignment"]))
        replayed = self.reload(self.t)
        self.assertEqual(replayed._project(pid)["frame_assignment"], stored)
        self.assertEqual(self.frames(replayed), frames)
        self.assertEqual(replayed.status()["projects"][0]["frame_assignment"], summary)
        self.run_async(replayed.compact_async())
        compacted = self.reload(replayed)
        self.assertEqual(compacted._project(pid)["frame_assignment"], stored)
        self.assertEqual(self.frames(compacted), frames)
        self.assertEqual(compacted.status()["projects"][0]["frame_assignment"], summary)
        compacted.revoke_frame_assignment(pid, summary["token"])
        revoked = self.reload(compacted)
        self.assertNotIn("frame_assignment", revoked._project(pid))
        self.assertEqual(self.frames(revoked), unassigned)

    def test_refused_assignments_change_nothing(self):
        pid = self.legacy_project()
        shown = self.preview(pid)

        def assign(project_id=pid, frame=EXAMPLE, epoch="", count=shown["total"],
                   scope=shown["scope_token"], tracker=self.t):
            return self.run_async(tracker.assign_frame_async(project_id, frame, epoch, count, scope))

        def refused(name, call, code=None, message=None, tracker=self.t, project_id=pid):
            with self.subTest(name):
                files = digests(self.tmp)
                kept = json.dumps(tracker._project(project_id).get("frame_assignment"))
                with self.assertRaises(ValueError) as caught:
                    call()
                if code is None:
                    self.assertNotIsInstance(caught.exception, tracking.FrameAssignmentError)
                    self.assertEqual(str(caught.exception), message)
                else:
                    self.assertIsInstance(caught.exception, tracking.FrameAssignmentError)
                    self.assertEqual(caught.exception.code, code)
                self.assertEqual(digests(self.tmp), files)
                self.assertEqual(json.dumps(tracker._project(project_id).get("frame_assignment")), kept)
        refused("unknown project", lambda: assign("project-none"), message="Project not found.")
        refused("no project", lambda: assign(None), message="Project not found.")
        refused("empty frame", lambda: assign(frame=""), message="A reference frame is required.")
        refused("blank frame", lambda: assign(frame="   "), message="A reference frame is required.")
        refused("Unknown", lambda: assign(frame="Unknown"), message=RESERVED)
        refused("MIXED", lambda: assign(frame=" MIXED "), message=RESERVED)
        refused("41 characters", lambda: assign(frame="x" * 41),
                message="reference_frame must be at most 40 characters.")
        refused("17 character epoch", lambda: assign(epoch="1" * 17),
                message="reference_frame_epoch must be at most 16 characters.")
        refused("control character", lambda: assign(frame="ETRS\n89"),
                message="reference_frame contains control characters.")
        refused("frame not text", lambda: assign(frame=89), message="reference_frame must be text.")
        refused("count 5", lambda: assign(count=5), "frame_scope_changed")
        refused("count text", lambda: assign(count="6"), message="expected_count must be an integer.")
        refused("count bool", lambda: assign(count=True), message="expected_count must be an integer.")
        refused("token missing", lambda: assign(scope=None), message="scope_token must be a text.")
        refused("token number", lambda: assign(scope=6), message="scope_token must be a text.")
        refused("wrong token", lambda: assign(scope="0" * 16), "frame_scope_changed")
        refused("revoke without assignment",
                lambda: self.t.revoke_frame_assignment(pid, "0" * 16), "no_frame_assignment")
        empty = self.tracker("empty")
        empty_id = empty.create_project("DGPS only")["id"]
        with mock.patch.dict(cfg.CONFIG, {"measurement_required_fix": "DGPS"}):
            empty.add_asset("tap", "", "", DGPS_FIX, link_to_active_line=False)
        nothing = self.preview(empty_id, empty)
        self.assertEqual((nothing["total"], nothing["other_unknown"]), (0, 0))
        refused("nothing to assign", lambda: assign(empty_id, count=0, scope=nothing["scope_token"],
                                                    tracker=empty),
                "nothing_to_assign", tracker=empty, project_id=empty_id)
        token = self.assign(pid, EXAMPLE)["frame_assignment"]["token"]
        refused("revoke with another token",
                lambda: self.t.revoke_frame_assignment(pid, "0" * 16), "frame_assignment_changed")
        refused("revoke token missing", lambda: self.t.revoke_frame_assignment(pid, None),
                message="assignment_token must be a text.")
        refused("revoke token number", lambda: self.t.revoke_frame_assignment(pid, 6),
                message="assignment_token must be a text.")
        refused("revoke unknown project",
                lambda: self.t.revoke_frame_assignment("project-none", token),
                message="Project not found.")
        self.t.revoke_frame_assignment(pid, token)
        shown = self.preview(pid)
        self.t.set_line_state("resume")
        refused("recording line", lambda: assign(scope=shown["scope_token"]), "recording_active")
        self.t.set_line_state("pause")
        result = assign(scope=shown["scope_token"])   # a paused line allows it
        self.assertEqual(result["frame_assignment"]["count"], 6)

    def test_a_change_between_preview_and_assign_is_refused(self):
        with self.subTest("undo and a new vertex, same count"):
            t = self.tracker("a"); pid = self.legacy_project(t)
            shown = self.preview(pid, t)
            t.undo_last(); self.add_rtk_vertex(44, t)
            self.assertEqual(self.preview(pid, t)["total"], shown["total"])
            self.assert_scope_refused(t, pid, shown)
        with self.subTest("undo and a new object point, same count"):
            t = self.tracker("b"); pid = self.legacy_project(t)
            t.add_asset("tap", "", "", fix(46), link_to_active_line=False)
            shown = self.preview(pid, t)
            t.undo_last(); t.add_asset("tap", "", "", fix(47), link_to_active_line=False)
            self.assertEqual(self.preview(pid, t)["total"], shown["total"])
            self.assert_scope_refused(t, pid, shown)
        with self.subTest("another client assigns meanwhile"):
            t = self.tracker("c"); pid = self.legacy_project(t)
            shown = self.preview(pid, t)
            self.assign(pid, "B", tracker=t)
            self.assert_scope_refused(t, pid, shown)
            self.assertEqual(t._project(pid)["frame_assignment"]["reference_frame"], "B")
        with self.subTest("another client revokes meanwhile"):
            t = self.tracker("d"); pid = self.legacy_project(t)
            token = self.assign(pid, "B", tracker=t)["frame_assignment"]["token"]
            shown = self.preview(pid, t)
            t.revoke_frame_assignment(pid, token)
            self.assert_scope_refused(t, pid, shown)
            self.assertNotIn("frame_assignment", t._project(pid))
        with self.subTest("backup and restore of the same survey"):
            t = self.tracker("e"); pid = self.legacy_project(t)
            shown = self.preview(pid, t)
            project = t._project(pid)
            t.restore_ndjson(list(t.iter_backup_ndjson()))
            self.assertIsNot(t._project(pid), project)
            self.assert_scope_refused(t, pid, shown)
        with self.subTest("backup and restore of object points only (a new point-store generation)"):
            t = self.tracker("e2")
            pid = t.create_project("Points")["id"]
            t.add_asset("tap", "", "", fix(46), link_to_active_line=False)
            shown = self.preview(pid, t)
            self.assertEqual(shown["total"], 1)
            t.restore_ndjson(list(t.iter_backup_ndjson()))
            self.assert_scope_refused(t, pid, shown)
        with self.subTest("a new point in another project does not matter"):
            t = self.tracker("f"); pid = self.legacy_project(t)
            self.other_project_recording(t)
            shown = self.preview(pid, t)
            t.add_vertex(fix(71)); t.add_asset("tap", "", "", fix(72))
            result = self.run_async(t.assign_frame_async(pid, "A", "", shown["total"],
                                                         shown["scope_token"]))
            self.assertEqual(result["frame_assignment"]["count"], 6)

    def test_a_change_during_the_check_is_refused(self):
        def resume_add_pause(t):
            return lambda: self.add_rtk_vertex(44, t)
        cases = (("undo", lambda t: t.undo_last, "frame_scope_changed"),
                 ("undo and a new vertex",
                  lambda t: lambda: (t.undo_last(), self.add_rtk_vertex(44, t)),
                  "frame_scope_changed"),
                 ("line resumed", lambda t: lambda: t.set_line_state("resume"), "recording_active"),
                 ("resumed and a new vertex", resume_add_pause, "frame_scope_changed"))
        for number, (name, action, code) in enumerate(cases):
            with self.subTest(name):
                t = self.tracker("case-%d" % number); pid = self.legacy_project(t)
                shown = self.preview(pid, t)
                patches = self.scan_with(action(t))
                for patch in patches: patch.start()
                try:
                    self.assert_scope_refused(t, pid, shown, code)
                finally:
                    for patch in patches: patch.stop()
                self.assertNotIn("frame_assignment", t._project(pid))
        with self.subTest("a new vertex in another project does not matter"):
            t = self.tracker("other"); pid = self.legacy_project(t)
            self.other_project_recording(t)
            shown = self.preview(pid, t)
            patches = self.scan_with(lambda: t.add_vertex(fix(71)))
            for patch in patches: patch.start()
            try:
                result = self.run_async(t.assign_frame_async(pid, "A", "", shown["total"],
                                                             shown["scope_token"]))
            finally:
                for patch in patches: patch.stop()
            self.assertEqual(result["frame_assignment"]["count"], 6)
        with self.subTest("the preview"):
            t = self.tracker("preview"); pid = self.legacy_project(t)
            patches = self.scan_with(t.undo_last)
            for patch in patches: patch.start()
            try:
                with self.assertRaises(tracking.FrameAssignmentError) as caught:
                    self.preview(pid, t)
            finally:
                for patch in patches: patch.stop()
            self.assertEqual(caught.exception.code, "frame_scope_changed")

    def test_field_setup_changes_never_assign_a_frame(self):
        pid = self.legacy_project()
        frames, shown = self.frames(), self.preview(pid)
        with mock.patch.dict(cfg.CONFIG, {"reference_frame": "ETRS89",
                                          "reference_frame_epoch": "2010.0"}):
            self.t._project_frames.clear(); self.t._status_cache = None
            self.assertEqual(self.frames(), frames)
            later = self.preview(pid)
            self.assertEqual((later["total"], later["scope_token"]),
                             (shown["total"], shown["scope_token"]))
            self.t.add_asset("tap", "", "", fix(48), link_to_active_line=False)
        self.assertNotIn("project_frame_assigned", [e["event"] for e in journal_events(self.t)])
        measurement = self.t._project(pid)["points"][-1]["measurement"]
        self.assertEqual((measurement["reference_frame"], measurement["reference_frame_epoch"]),
                         ("ETRS89", "2010.0"))
        self.assertEqual(tracking._effective_frame(measurement, None),
                         ("ETRS89", "2010.0", "recorded"))
        self.assertEqual(self.frames(), dict(frames, ETRS89=1))

    def test_the_check_reads_in_batches_and_writes_nothing(self):
        self.t.restore_ndjson(legacy_backup([("long", "Long", [{}] * 200)]))
        project = self.t._project("p1")
        before = digests(self.tmp)
        items = list(self.t._frame_scope_steps(project))
        self.assertEqual(items[:-1], [None] * math.ceil(200 / 64))
        self.assertEqual((items[-1]["lines"], items[-1]["total"]), ({"long": [200, 200]}, 200))
        held, original = [], pointstore.FlashPointStore.iter_line

        def holding(store, *args, **kwargs):
            generator = original(store, *args, **kwargs)
            held.append(generator)
            return generator
        with mock.patch.object(pointstore.FlashPointStore, "iter_line", holding):
            steps = self.t._frame_scope_steps(project)
            self.assertIsNone(next(steps)); self.assertIsNone(next(steps))
            steps.close()
        self.assertEqual(len(held), 2)
        self.assertEqual([generator.gi_frame for generator in held], [None, None])
        self.assertEqual(digests(self.tmp), before)

    def counting_read_paths(self):
        """Mocks counting every point-store read path (A10) and the copied locator counts."""
        counters = {}
        for name in READ_PATHS:
            patch = mock.patch.object(pointstore.FlashPointStore, name, autospec=True,
                                      side_effect=getattr(pointstore.FlashPointStore, name))
            counters[name] = patch.start(); self.addCleanup(patch.stop)
        copies, original = [], pointstore._copy_offsets

        def copied(values):
            result = original(values)
            copies.append(len(result))
            return result
        patch = mock.patch.object(pointstore, "_copy_offsets", copied)
        patch.start(); self.addCleanup(patch.stop)
        return counters, copies

    def assert_bounded_scan(self, tracker, project, vertices, minimum_batches, counters, copies):
        del copies[:]
        for counter in counters.values(): counter.reset_mock()
        batches, cached_reads = [], 0
        for item in tracker._frame_scope_steps(project):
            batches.append((counters["_decode"].call_count, counters["iter_line"].call_count))
            cached_reads += counters["read"].call_count
            for counter in counters.values(): counter.reset_mock()
            if item is not None:
                result = item
        self.assertEqual(cached_reads, 0)   # windows only, no single-vertex reads
        self.assertEqual(batches[-1], (0, 0))   # nothing is read for the result
        self.assertTrue(all(decoded <= 64 and windows <= 8 for decoded, windows in batches))
        self.assertEqual(sum(decoded for decoded, _windows in batches), vertices)
        self.assertGreaterEqual(len(batches) - 1, minimum_batches)
        self.assertLessEqual(max(copies), 64)
        self.assertEqual(result["total"], vertices)
        return len(batches) - 1

    def assert_flash_free_commit(self, tracker, project, counters):
        """The token, the checks after the last batch and the summaries read no flash."""
        original_steps, original_append = tracking.Tracker._frame_scope_steps, tracking.Tracker._append
        reads_at_append, events = [], []

        def steps(tracker, project):
            inner = original_steps(tracker, project)
            try:
                for item in inner:
                    if item is not None:
                        for counter in counters.values(): counter.reset_mock()
                    yield item
            finally:
                inner.close()

        def append(tracker, event, *args, **kwargs):
            if event.get("event") == "project_frame_assigned":
                reads_at_append.append(sum(counter.call_count for counter in counters.values()))
            return original_append(tracker, event, *args, **kwargs)
        shown = self.preview("p1", tracker)
        for counter in counters.values(): counter.reset_mock()
        tracker._frame_scope_token(project)
        self.assertEqual([counters[name].call_count for name in READ_PATHS], [0, 0, 0, 0])
        with mock.patch.object(tracking.Tracker, "_frame_scope_steps", steps), \
                mock.patch.object(tracking.Tracker, "_append", append):
            self.run_async(tracker.assign_frame_async("p1", EXAMPLE, "", shown["total"],
                                                      shown["scope_token"]))
        self.assertEqual(reads_at_append, [0])
        for counter in counters.values(): counter.reset_mock()
        tracker._frame_tokens.clear()
        self.assertRegex(tracker._assignment_token(project), r"^[0-9a-f]{16}$")
        self.assertEqual(tracker._assignment_summary(project)["count"], shown["total"])
        self.assertEqual([counters[name].call_count for name in READ_PATHS], [0, 0, 0, 0])
        event = journal_text(tracker).splitlines()[-1]
        self.assertLessEqual(len(event.encode("utf-8")), 24 * 1024)
        return shown

    def test_the_check_bounds_work_for_many_short_lines_and_one_long_line(self):
        short = self.tracker("short")
        short.restore_ndjson(legacy_backup([("line-%08x-%04x" % (index, index), "S%d" % index, [{}, {}])
                                            for index in range(500)]))
        project = short._project("p1")
        counters, copies = self.counting_read_paths()
        self.assert_bounded_scan(short, project, 1000, math.ceil(500 / 8), counters, copies)
        shown = self.assert_flash_free_commit(short, project, counters)
        self.assertEqual((len(shown["lines"]), shown["more_lines"]), (5, 495))
        self.assertEqual(shown["lines"][0], {"id": "line-00000000-0000", "name": "S0", "count": 2})
        self.assertEqual(len(project["frame_assignment"]["lines"]), 500)
        # The largest line the feature limit leaves room for, behind a short line:
        # a window never reads past the batch budget the short line left.
        vertices = tracking.MAX_FEATURES - 1
        long = self.tracker("long")
        long.restore_ndjson(legacy_backup([("short", "Short", [{}, {}]),
                                           ("long", "Long", [{}] * (vertices - 2))]))
        project = long._project("p1")
        batches = self.assert_bounded_scan(long, project, vertices, 0, counters, copies)
        self.assertEqual(batches, math.ceil(vertices / 64))
        self.assert_flash_free_commit(long, project, counters)

    @staticmethod
    def reads_after_the_scan(counters, action):
        """(result of action, point-store reads after its frame scan's last batch).

        Without a frame scan (a revocation) every read of the action counts.
        """
        original = tracking.Tracker._frame_scope_steps

        def steps(tracker, project):
            inner = original(tracker, project)
            try:
                for item in inner:
                    if item is not None:
                        for counter in counters.values(): counter.reset_mock()
                    yield item
            finally:
                inner.close()
        for counter in counters.values(): counter.reset_mock()
        with mock.patch.object(tracking.Tracker, "_frame_scope_steps", steps):
            result = action()
        return result, sum(counters[name].call_count for name in READ_PATHS)

    def test_assigning_and_revoking_after_an_undo_keep_the_tally_without_a_rescan(self):
        # One long paused line without frames: every tenth vertex DGPS, the others RTK.
        vertices = [{"fix_quality": 2, "fix_status": "DGPS/SBAS"} if index % 10 == 0 else {}
                    for index in range(1000)]
        self.t.restore_ndjson(legacy_backup([("long", "Long", vertices)], line_state="paused",
                                            active_line="long"))
        project = self.t._project("p1")
        self.assertEqual(self.frames(), {"unknown": 1000})
        # An undo leaves the long line's cached summary stale (status() would rebuild it).
        self.t.undo_last()
        self.assertNotIn("_summary", project["lines"][0])
        counters, _copies = self.counting_read_paths()

        def assign():
            shown = self.preview("p1")
            return self.reads_after_the_scan(counters, lambda: self.run_async(
                self.t.assign_frame_async("p1", EXAMPLE, "", shown["total"],
                                          shown["scope_token"])))
        result, reads = assign()
        self.assertEqual((reads, result["frames"]), (0, {EXAMPLE: 899, "unknown": 100}))
        # Undo an assigned RTK vertex (the summary is stale again), then revoke.
        self.t.undo_last()
        self.assertNotIn("_summary", project["lines"][0])
        token = self.t._assignment_token(project)
        result, reads = self.reads_after_the_scan(
            counters, lambda: self.t.revoke_frame_assignment("p1", token))
        self.assertEqual((reads, result["frames"]), (0, {"unknown": 998}))
        # A missing tally (a restore, before the first status read) comes from the scan.
        self.t._project_frames.pop("p1")
        result, reads = assign()
        self.assertEqual((reads, result["frames"]), (0, {EXAMPLE: 898, "unknown": 100}))
        # The kept tally is exact: a restart rebuilds the same one from the stored points.
        kept = dict(self.t._project_frames["p1"])
        reloaded = self.reload(self.t)
        self.assertEqual(reloaded._project_frame_tally(reloaded._project("p1")), kept)

    def test_revoking_requires_the_shown_assignment(self):
        with support.patch_module_global("tracking", "_now_seconds", return_value=1793000000):
            pid = self.legacy_project()
            first = self.assign(pid, EXAMPLE)["frame_assignment"]
            self.assertEqual(first["count"], 6)
            self.assertRegex(first["token"], r"^[0-9a-f]{16}$")
            self.add_rtk_vertex(44)
            second = self.assign(pid, EXAMPLE)["frame_assignment"]
            self.assertEqual((second["assigned_at"], second["count"]), (first["assigned_at"], 7))
            self.assertNotEqual(second["token"], first["token"])
            journal = journal_text(self.t)
            with self.assertRaises(tracking.FrameAssignmentError) as caught:
                self.t.revoke_frame_assignment(pid, first["token"])
            self.assertEqual(caught.exception.code, "frame_assignment_changed")
            self.assertEqual(self.t.status()["projects"][0]["frame_assignment"], second)
            self.assertEqual(journal_text(self.t), journal)
            # The token is canonical: the key order of the lines does not matter.
            project = self.t._project(pid)
            assignment = project["frame_assignment"]
            self.assertEqual(len(assignment["lines"]), 2)
            project["frame_assignment"] = json.loads(json.dumps(dict(
                assignment, lines=dict(reversed(list(assignment["lines"].items()))))))
            self.assertNotEqual(list(project["frame_assignment"]["lines"]), list(assignment["lines"]))
            self.assertEqual(self.t._assignment_token(project), second["token"])
            replayed = self.reload(self.t)
            self.assertEqual(replayed._assignment_token(replayed._project(pid)), second["token"])
            replayed.revoke_frame_assignment(pid, second["token"])
            self.assertNotIn("frame_assignment", replayed._project(pid))

    def test_live_status_carries_the_assignment_and_its_removal(self):
        pid = self.legacy_project()
        live = self.t.live_status()["active_project"]
        self.assertEqual(live["id"], pid)
        self.assertIn("frame_assignment", live)
        self.assertIsNone(live["frame_assignment"])
        summary = self.assign(pid, EXAMPLE)["frame_assignment"]
        live = self.t.live_status()["active_project"]["frame_assignment"]
        self.assertEqual(live, self.t._assignment_summary(self.t._project(pid)))
        self.assertEqual((live, live["count"]), (summary, 6))
        self.assertEqual(set(live), {"reference_frame", "reference_frame_epoch", "assigned_at",
                                     "assigned_at_utc", "count", "token"})
        self.assertEqual(live["assigned_at_utc"], tracking._utc_fields(live["assigned_at"])[0])
        # Undoing an assigned vertex (the last of L1) shrinks the live count at once.
        self.t.undo_last()
        live = self.t.live_status()["active_project"]["frame_assignment"]
        self.assertEqual(live["count"], 5)
        self.assertNotEqual(live["token"], summary["token"])
        summary = live
        self.t.set_line_state("finish")
        self.t.create_project("Other")
        self.t.select_project(pid)
        self.assertEqual(self.t.live_status()["active_project"]["frame_assignment"], summary)
        projects = self.t.status()["projects"]
        self.assertEqual(projects[0]["frame_assignment"], summary)
        self.assertNotIn("frame_assignment", projects[1])
        self.t.revoke_frame_assignment(pid, summary["token"])
        live = self.t.live_status()["active_project"]
        self.assertIn("frame_assignment", live)
        self.assertIsNone(live["frame_assignment"])

    def test_assigned_text_rules_match_the_field_setup(self):
        inputs = (("ETRS89", ""), (" etrs89 ", " 2010.0 "), ("x" * 40, "1" * 16),
                  ("x" * 41, ""), ("ETRS89", "1" * 17), ("a\tb", ""), ("ETRS89", "2010\x7f"),
                  ("Frame \u00e4\u00f6\u00fc", "2010.0"), (89, ""), ("ETRS89", None),
                  ("  ETRS89/DREF91 (R2016)  ", "  "))
        for frame, epoch in inputs:
            with self.subTest(frame=frame, epoch=epoch):
                clean, error = cfg_validation.validate_field_setup(
                    {"reference_frame": frame, "reference_frame_epoch": epoch})
                if error:
                    with self.assertRaises(ValueError) as caught:
                        tracking._clean_assigned_frame(frame, epoch)
                    self.assertEqual(str(caught.exception), error)
                else:
                    self.assertEqual(tracking._clean_assigned_frame(frame, epoch),
                                     (clean["reference_frame"],
                                      clean["reference_frame_epoch"] or None))
        self.assertEqual(tracking._clean_assigned_frame(" etrs89 ", ""), ("etrs89", None))


class FrameAssignmentUndoTests(FrameFixture, unittest.TestCase):
    """Undo inside an assignment's scope shrinks it exactly (E7, E16)."""

    def rtk_line(self, vertices, tracker=None):
        """A new project P with one paused line L of RTK vertices (empty field setup)."""
        t = tracker or self.t
        pid = t.create_project("P")["id"]
        t.start_line("L")
        for index in range(vertices):
            t.add_vertex(fix(50 + index))
        t.set_line_state("pause")
        return pid, t._project(pid)["lines"][0]["id"]

    def mixed_project(self, tracker=None):
        """P with line L (paused): two RTK vertices, two RTK object points, one more vertex."""
        t = tracker or self.t
        pid, line = self.rtk_line(2, t)
        self.add_point(52, t); self.add_point(53, t)
        self.add_rtk_vertex(54, t)
        return pid, line

    @staticmethod
    def add_point(index, tracker):
        tracker.add_asset("tap", "", "", fix(index), link_to_active_line=False)

    def scope(self, pid, tracker=None):
        return (tracker or self.t)._project(pid)["frame_assignment"]

    def summary(self, tracker=None):
        return (tracker or self.t).status()["projects"][0]["frame_assignment"]

    def last_removal(self, tracker=None):
        return [event for event in journal_events(tracker or self.t)
                if event["event"] == "feature_removed"][-1]

    def assert_scope(self, tracker, pid, lines, points, count, frames):
        assignment = self.scope(pid, tracker)
        self.assertEqual((assignment["lines"], assignment["points"]), (lines, points))
        self.assertEqual(self.summary(tracker)["count"], count)
        self.assertEqual(self.frames(tracker), frames)

    def test_undoing_an_assigned_vertex_shrinks_the_scope(self):
        pid, line = self.rtk_line(3)
        self.assign(pid, "X")
        self.assertEqual(self.scope(pid)["lines"], {line: [3, 3]})
        self.t.undo_last()
        self.assertEqual(self.scope(pid)["lines"], {line: [2, 2]})
        self.assertEqual(self.frames(), {"X": 2})
        self.assertEqual(self.last_removal()["frame_scope"], 1)
        self.t.undo_last()
        self.assertEqual(self.scope(pid)["lines"], {line: [1, 1]})
        self.t.undo_last()
        assignment = self.scope(pid)
        self.assertEqual((assignment["lines"], assignment["points"]), ({}, [0, 0]))
        self.assertEqual(self.summary()["count"], 0)
        self.assertEqual(self.frames(), {})
        self.add_rtk_vertex(60)
        self.assertEqual(self.frames(), {"unknown": 1})
        # The kept tally and one rebuilt from the line summaries agree.
        self.t._project_frames.clear(); self.t._status_cache = None
        self.assertEqual(self.frames(), {"unknown": 1})
        shown = self.preview(pid)
        self.assertEqual((shown["total"], shown["new_unknown"]), (1, 1))

    def test_undoing_an_assigned_object_point_shrinks_the_scope(self):
        pid = self.t.create_project("P")["id"]
        self.add_point(50, self.t); self.add_point(51, self.t)
        self.assign(pid, "X")
        self.assertEqual(self.scope(pid)["points"], [2, 2])
        self.t.undo_last()
        self.assert_scope(self.t, pid, {}, [1, 1], 1, {"X": 1})
        self.assertNotIn("frame_scope", self.last_removal())
        self.add_point(52, self.t)
        self.assertEqual(self.frames(), {"X": 1, "unknown": 1})

    def assert_kept(self, t, pid, expected, frame_scope):
        """The scope after an undo, the event's frame_scope, and the same after a restart."""
        removal = self.last_removal(t)
        if frame_scope is None:
            self.assertNotIn("frame_scope", removal)
        else:
            self.assertEqual(removal["frame_scope"], frame_scope)
        self.assert_scope(t, pid, *expected)
        self.assert_scope(self.reload(t), pid, *expected)

    def test_undo_of_a_known_or_non_rtk_vertex_in_the_prefix_keeps_the_count(self):
        dgps = {"fix_quality": 2, "fix_status": "DGPS/SBAS"}
        with self.subTest("a non-RTK vertex without a frame"):
            t = self.tracker("dgps")
            t.restore_ndjson(legacy_backup([("l0", "L", [{}, {}, {}, dgps])], line_state="paused"))
            self.assign("p1", "X", tracker=t)
            self.assert_scope(t, "p1", {"l0": [4, 3]}, [0, 0], 3, {"X": 3, "unknown": 1})
            t.undo_last()
            self.assert_kept(t, "p1", ({"l0": [3, 3]}, [0, 0], 3, {"X": 3}), 0)
        with self.subTest("a vertex with a recorded frame"):
            t = self.tracker("known")
            pid, line = self.rtk_line(3, t)
            with mock.patch.dict(cfg.CONFIG, {"reference_frame": SYNTHETIC}):
                self.add_rtk_vertex(53, t)
            self.assign(pid, "X", tracker=t)
            self.assert_scope(t, pid, {line: [4, 3]}, [0, 0], 3, {"X": 3, SYNTHETIC: 1})
            t.undo_last()
            self.assert_kept(t, pid, ({line: [3, 3]}, [0, 0], 3, {"X": 3}), 0)
        with self.subTest("a non-RTK object point without a frame"):
            t = self.tracker("point")
            t.restore_ndjson(legacy_backup([], [{}, {}, dgps]))
            self.assign("p1", "X", tracker=t)
            self.assert_scope(t, "p1", {}, [3, 2], 2, {"X": 2, "unknown": 1})
            t.undo_last()
            self.assert_kept(t, "p1", ({}, [2, 2], 2, {"X": 2}), None)

    def test_emptying_a_line_before_a_restart_drops_its_scope(self):
        pid = self.t.create_project("P")["id"]
        self.add_point(49, self.t)
        self.t.start_line("L"); self.t.add_vertex(fix(50)); self.t.add_vertex(fix(51))
        self.t.set_line_state("pause")
        line = self.t._project(pid)["lines"][0]["id"]
        self.assign(pid, "X")
        self.assertEqual(self.scope(pid)["lines"], {line: [2, 2]})
        self.t.undo_last(); self.t.undo_last()
        self.assert_scope(self.t, pid, {}, [1, 1], 1, {"X": 1})
        # The replay meets an empty line at both events (A7); the scope still shrinks.
        replayed = self.reload(self.t)
        self.assertEqual(len(replayed._project(pid)["lines"][0]["coordinates"]), 0)
        self.assert_scope(replayed, pid, {}, [1, 1], 1, {"X": 1})
        self.add_rtk_vertex(60, replayed)
        self.assertEqual(self.frames(replayed), {"X": 1, "unknown": 1})

    def replay_view(self, tracker, pid):
        """Assignment, tally and preview (without its RAM-only scope token) of pid."""
        project = tracker._project(pid)
        shown = self.preview(pid, tracker)
        shown.pop("scope_token")
        return (json.loads(json.dumps(project["frame_assignment"])),
                dict(tracker._project_frame_tally(project)), shown)

    def test_undo_after_the_assignment_replays_the_same_scope(self):
        pid, line = self.mixed_project()
        self.assign(pid, "X")
        self.assertEqual((self.scope(pid)["lines"], self.scope(pid)["points"]),
                         ({line: [3, 3]}, [2, 2]))
        self.t.undo_last()   # the vertex
        self.t.undo_last()   # the newer object point
        self.add_rtk_vertex(56); self.add_point(57, self.t)
        live = self.replay_view(self.t, pid)
        self.assertEqual((live[0]["lines"], live[0]["points"]), ({line: [2, 2]}, [1, 1]))
        self.assertEqual(live[1], {"X": 3, "unknown": 2})
        self.assertEqual((live[2]["total"], live[2]["new_unknown"]), (5, 2))
        replayed = self.reload(self.t)
        self.assertEqual(self.replay_view(replayed, pid), live)
        self.run_async(replayed.compact_async())
        self.assertEqual(self.replay_view(self.reload(replayed), pid), live)

    def test_undoing_a_later_point_keeps_the_scope(self):
        pid, line = self.rtk_line(2)
        self.assign(pid, "X")
        before = self.scope(pid)
        self.add_rtk_vertex(60)
        self.t.undo_last()
        self.assertIs(self.scope(pid), before)
        self.assertEqual(before["lines"], {line: [2, 2]})
        self.assertNotIn("frame_scope", self.last_removal())
        self.assertEqual(self.frames(), {"X": 2})
        # A line without a scope entry: it holds recorded frames only.
        t = self.tracker("known")
        pid = t.create_project("P")["id"]
        self.add_point(49, t)
        with mock.patch.dict(cfg.CONFIG, {"reference_frame": SYNTHETIC}):
            t.start_line("K"); t.add_vertex(fix(50)); t.add_vertex(fix(51))
        t.set_line_state("pause")
        self.assign(pid, "X", tracker=t)
        before = self.scope(pid, t)
        self.assertEqual((before["lines"], before["points"]), ({}, [1, 1]))
        t.undo_last()
        self.assertIs(self.scope(pid, t), before)
        self.assertNotIn("frame_scope", self.last_removal(t))
        self.assertEqual(self.frames(t), {"X": 1, SYNTHETIC: 1})

    def test_undo_replaces_the_assignment_instead_of_changing_it(self):
        pid, line = self.mixed_project()
        self.assign(pid, "X")
        steps = (("a vertex", {line: [2, 2]}, [2, 2]), ("an object point", {line: [2, 2]}, [1, 1]),
                 ("a new assignment", {line: [2, 2]}, [1, 1]))
        for name, lines, points in steps:
            with self.subTest(name):
                before = self.scope(pid)
                snapshot = json.dumps(before, sort_keys=True)
                if name == "a new assignment":
                    self.assign(pid, "Y")
                else:
                    self.t.undo_last()
                after = self.scope(pid)
                self.assertEqual((after["lines"], after["points"]), (lines, points))
                self.assertIsNot(after, before)
                self.assertIsNot(after["lines"], before["lines"])
                self.assertIsNot(after["points"], before["points"])
                for line_id, value in after["lines"].items():
                    self.assertIsNot(value, before["lines"].get(line_id))
                self.assertEqual(json.dumps(before, sort_keys=True), snapshot)

    def assert_revoke_refused(self, pid, token):
        journal = journal_text(self.t)
        kept = json.dumps(self.scope(pid), sort_keys=True)
        with self.assertRaises(tracking.FrameAssignmentError) as caught:
            self.t.revoke_frame_assignment(pid, token)
        self.assertEqual(caught.exception.code, "frame_assignment_changed")
        self.assertEqual(json.dumps(self.scope(pid), sort_keys=True), kept)
        self.assertEqual(journal_text(self.t), journal)

    def test_a_revoke_after_an_undo_is_refused(self):
        pid, line = self.rtk_line(3)
        self.add_point(53, self.t)
        first = self.assign(pid, "X")["frame_assignment"]
        self.assertEqual(first["count"], 4)
        # (a) The object point is undone after the assignment was shown.
        self.t.undo_last()
        second = self.summary()
        self.assertEqual(second["count"], 3)
        self.assertNotEqual(second["token"], first["token"])
        self.assert_revoke_refused(pid, first["token"])
        self.assertEqual((self.scope(pid)["points"], self.scope(pid)["lines"]),
                         ([0, 0], {line: [3, 3]}))
        # (b) Then a vertex.
        self.t.undo_last()
        third = self.summary()
        self.assertEqual(third["count"], 2)
        self.assertNotIn(third["token"], (first["token"], second["token"]))
        self.assert_revoke_refused(pid, second["token"])
        # (c) The token shown last revokes it.
        self.t.revoke_frame_assignment(pid, third["token"])
        event = journal_events(self.t)[-1]
        self.assertEqual((event["event"], event["assignment"], event["previous"]["count"]),
                         ("project_frame_assigned", None, 2))
        # (d) Undoing a point recorded later changes neither the scope nor the token.
        shown = self.assign(pid, "X")["frame_assignment"]
        self.assertEqual(self.scope(pid)["lines"], {line: [2, 2]})
        self.add_rtk_vertex(60)
        self.t.undo_last()
        self.assertNotIn("frame_scope", self.last_removal())
        self.assertEqual(self.summary()["token"], shown["token"])
        self.t.revoke_frame_assignment(pid, shown["token"])
        self.assertNotIn("frame_assignment", self.t._project(pid))

    def test_an_assignment_emptied_by_undo_stays_and_can_be_revoked(self):
        pid, line = self.rtk_line(2)
        self.add_point(52, self.t)
        self.assign(pid, "X")
        self.assert_scope(self.t, pid, {line: [2, 2]}, [1, 1], 3, {"X": 3})
        for _ in range(3):
            self.t.undo_last()
        assignment = self.scope(pid)
        self.assertEqual(set(assignment), {"reference_frame", "reference_frame_epoch",
                                           "assigned_at", "lines", "points"})
        self.assertEqual(assignment["reference_frame"], "X")
        summary = self.summary()
        self.assertRegex(summary["token"], r"^[0-9a-f]{16}$")
        self.assertEqual(self.t.live_status()["active_project"]["frame_assignment"], summary)
        stored = json.loads(json.dumps(assignment))

        def unchanged(tracker):
            self.assertEqual(self.scope(pid, tracker), stored)
            self.assert_scope(tracker, pid, {}, [0, 0], 0, {})
            self.assertEqual(self.summary(tracker), summary)
            self.assertEqual(tracker.live_status()["active_project"]["frame_assignment"], summary)
        unchanged(self.t)
        replayed = self.reload(self.t)
        unchanged(replayed)
        self.run_async(replayed.compact_async())
        t = self.reload(replayed)
        unchanged(t)
        shown = self.preview(pid, t)
        self.assertEqual((shown["total"], shown["assignment"]["count"]), (0, 0))
        with self.assertRaises(tracking.FrameAssignmentError) as caught:
            self.run_async(t.assign_frame_async(pid, "Y", "", 0, shown["scope_token"]))
        self.assertEqual(caught.exception.code, "nothing_to_assign")
        unchanged(t)
        t.revoke_frame_assignment(pid, summary["token"])
        event = journal_events(t)[-1]
        self.assertEqual((event["event"], event["assignment"], event["previous"]["count"]),
                         ("project_frame_assigned", None, 0))
        self.assertNotIn("frame_assignment", t._project(pid))


class FrameAssignmentUndoPowerCutTests(FrameFixture, unittest.TestCase):
    """A power cut at any write step of an undo inside the scope (E7, R2-1).

    The restart shows the survey before or after the undo, never the vertex
    gone with the scope unchanged, so a replacement point stays unknown.
    """

    def prepared(self, third_frame):
        """A closed survey: P with paused line L of three RTK vertices, assigned "X".

        The third vertex is recorded with the field setup's frame third_frame.
        """
        folder = os.path.join(self.tmp, "prepared")
        os.mkdir(folder)
        t = tracking.Tracker(os.path.join(folder, "survey"))
        try:
            pid = t.create_project("P")["id"]
            t.start_line("L"); t.add_vertex(fix(50)); t.add_vertex(fix(51))
            with mock.patch.dict(cfg.CONFIG, {"reference_frame": third_frame}):
                t.add_vertex(fix(52))
            t.set_line_state("pause")
            self.assign(pid, "X", tracker=t)
        finally:
            self.close(t)
        return folder

    def copy(self, folder, name):
        target = os.path.join(self.tmp, name)
        if os.path.exists(target):
            shutil.rmtree(target)
        shutil.copytree(folder, target)
        return target

    def view(self, folder, label=None):
        """(vertices of L, scope entry of L, frames) after a restart of folder.

        Also checks the invariant n <= vertices for every scope entry.
        """
        t = tracking.Tracker(os.path.join(folder, "survey"))
        try:
            project = t.projects[0]
            lines = project["frame_assignment"]["lines"]
            for line in project["lines"]:
                if line["id"] in lines:
                    self.assertLessEqual(lines[line["id"]][0], len(line["coordinates"]), label)
            line = project["lines"][0]
            return len(line["coordinates"]), lines.get(line["id"]), self.frames(t)
        finally:
            self.close(t)

    def replace_vertex(self, folder, label):
        """Record a replacement RTK vertex on L; it must lie behind the scope."""
        t = tracking.Tracker(os.path.join(folder, "survey"))
        try:
            self.add_rtk_vertex(60, t)
            project = t.projects[0]
            line = project["lines"][0]
            entry = project["frame_assignment"]["lines"].get(line["id"])
            self.assertGreaterEqual(len(line["coordinates"]) - 1, entry[0], label)
            return self.frames(t)
        finally:
            self.close(t)

    def cut_every_undo_write(self, third_frame, before, after, frame_scope, replaced):
        folder = self.prepared(third_frame)
        self.assertEqual(self.view(self.copy(folder, "before")), before)
        done = self.copy(folder, "after")
        t = tracking.Tracker(os.path.join(done, "survey"))
        try:
            t.undo_last()
            removal = [event for event in journal_events(t) if event["event"] == "feature_removed"]
        finally:
            self.close(t)
        self.assertEqual(removal[-1]["frame_scope"], frame_scope)
        self.assertEqual(self.view(done), after)
        probe = self.copy(folder, "probe")
        with Injector(probe) as injector:
            t = tracking.Tracker(os.path.join(probe, "survey"))
            start = injector.count
            t.undo_last()
            total = injector.count
            self.close(t)
        self.assertGreater(total, start)
        outcomes = set()
        for crash_at in range(start + 1, total + 1):
            label = "crash at write %d of the undo (%d..%d)" % (crash_at, start + 1, total)
            run, image = self.copy(folder, "run"), os.path.join(self.tmp, "image")
            if os.path.exists(image):
                shutil.rmtree(image)
            with Injector(run, image, crash_at):
                t = tracking.Tracker(os.path.join(run, "survey"))
                with self.assertRaises(PowerCut, msg=label):
                    t.undo_last()
            state = self.view(image, label)
            self.assertIn(state, (before, after), label)
            outcome = "before" if state == before else "after"
            outcomes.add(outcome)
            self.assertEqual(self.replace_vertex(image, label), replaced[outcome], label)
            self.assertEqual(self.view(image, label)[2], replaced[outcome], label)
        self.assertEqual(outcomes, {"before", "after"})

    def test_a_power_cut_while_undoing_an_assignable_vertex_keeps_the_replacement_unknown(self):
        self.cut_every_undo_write(
            "", (3, [3, 3], {"X": 3}), (2, [2, 2], {"X": 2}), 1,
            {"before": {"X": 3, "unknown": 1}, "after": {"X": 2, "unknown": 1}})

    def test_a_power_cut_while_undoing_a_known_vertex_keeps_the_replacement_unknown(self):
        self.cut_every_undo_write(
            SYNTHETIC, (3, [3, 2], {"X": 2, SYNTHETIC: 1}), (2, [2, 2], {"X": 2}), 0,
            {"before": {"X": 2, SYNTHETIC: 1, "unknown": 1}, "after": {"X": 2, "unknown": 1}})


class FrameAssignmentExportTests(FrameFixture, unittest.TestCase):
    """GeoJSON and CSV say where a frame comes from (E9); a running export stops on a change (E16)."""

    ASSIGNED = (EXAMPLE, "2010.0", "assigned")
    UNKNOWN = ("unknown", None, None)
    FRAME_KEYS = ("reference_frame", "reference_frame_epoch", "reference_frame_source",
                  "reference_frames", "frame_assignment")

    def collection(self, pid, tracker=None):
        return json.loads("".join((tracker or self.t).iter_geojson(pid)))

    @staticmethod
    def frame_of(values):
        """(frame, epoch, source) of exported values; the source key must be present."""
        return (values["reference_frame"], values.get("reference_frame_epoch"),
                values["reference_frame_source"])

    def without_frames(self, value):
        """A copy of an export without any frame field."""
        if isinstance(value, dict):
            return dict((key, self.without_frames(item)) for key, item in value.items()
                        if key not in self.FRAME_KEYS)
        if isinstance(value, list):
            return [self.without_frames(item) for item in value]
        return value

    def test_geojson_marks_assigned_points_and_keeps_recorded_ones(self):
        pid = self.legacy_project()
        unassigned = self.collection(pid)
        for feature in unassigned["features"]:
            properties = feature["properties"]
            for values in properties.get("measurements") or (properties["measurement"], properties):
                self.assertEqual(values["reference_frame_source"],
                                 None if values["reference_frame"] == "unknown" else "recorded")
        self.assign(pid, EXAMPLE, "2010.0")
        assigned = self.collection(pid)
        # Only the frame fields change.
        self.assertEqual(self.without_frames(assigned), self.without_frames(unassigned))
        self.assertNotEqual(assigned, unassigned)
        self.add_rtk_vertex(44)
        collection = self.collection(pid)
        self.assertEqual(collection, json.loads(json.dumps(tracking_reference.geojson(self.t, pid))))
        l0, l1 = (feature["properties"] for feature in collection["features"][:2])
        self.assertEqual([self.frame_of(values) for values in l0["measurements"]],
                         [self.ASSIGNED] * 3 + [self.UNKNOWN])
        self.assertEqual([self.frame_of(values) for values in l1["measurements"]],
                         [self.ASSIGNED, self.ASSIGNED, self.UNKNOWN])
        points = [feature["properties"] for feature in collection["features"][2:]]
        expected = [self.ASSIGNED, (SYNTHETIC, None, "recorded"), ("WGS84/ITRF", None, "recorded")]
        self.assertEqual([self.frame_of(point) for point in points], expected)
        self.assertEqual([self.frame_of(point["measurement"]) for point in points], expected)
        self.assertEqual((l0["reference_frame"], l0["reference_frames"]),
                         ("mixed", {EXAMPLE: 3, "unknown": 1}))
        self.assertEqual((l1["reference_frame"], l1["reference_frames"]),
                         ("mixed", {EXAMPLE: 2, "unknown": 1}))
        # Stored measurements keep their empty frame (E1).
        self.assertIsNone(self.t._project(pid)["lines"][0]["measurements"][0].get("reference_frame"))

    def test_line_tally_is_the_same_with_and_without_a_summary(self):
        pid = self.legacy_project()
        self.t.set_line_state("finish")
        # A custom "measurements" property overrides the vertex list (_count_row_frame).
        self.t.start_line("Override", 1, {"measurements": "external"}, fix=fix(46))
        self.t.add_vertex(fix(47))
        with mock.patch.dict(cfg.CONFIG, {"measurement_required_fix": "DGPS"}):
            self.t.add_vertex(dict(DGPS_FIX, lon_e8=800096000))
        self.t.set_line_state("pause")
        self.assign(pid, EXAMPLE)
        self.add_rtk_vertex(49)
        expected = [("mixed", {EXAMPLE: 3, "unknown": 1}), (EXAMPLE, {EXAMPLE: 2}),
                    ("mixed", {EXAMPLE: 2, "WGS84/ITRF": 1, "unknown": 1})]
        lines = self.t._project(pid)["lines"]
        for stale in (False, True):
            with self.subTest(stale=stale):
                if stale:
                    for line in lines:
                        line.pop("_summary", None)
                with mock.patch.object(self.t, "_count_row_frame",
                                       wraps=self.t._count_row_frame) as counted:
                    features = self.collection(pid)["features"][:3]
                # Stale: the geometry pass counts each of the override line's 4 vertices.
                self.assertEqual(counted.call_count, 4 if stale else 0)
                self.assertEqual([(feature["properties"]["reference_frame"],
                                   feature["properties"]["reference_frames"])
                                  for feature in features], expected)
                self.assertEqual(features[2]["properties"]["measurements"], "external")

    def test_csv_carries_the_frame_source(self):
        pid = self.legacy_project()
        self.assign(pid, EXAMPLE, "2010.0")
        self.t.add_asset("tap", "Later", "", fix(45), link_to_active_line=False)
        text = self.t.csv(pid)
        lines = text.splitlines()
        self.assertIn(",reference_frame,reference_frame_epoch,reference_frame_source,gate_profile,",
                      lines[0])
        for number, cells in ((1, '"ETRS89 (example)","2010.0","assigned"'),
                              (2, '"ETRS89 (synthetic)","","recorded"'),
                              (3, '"WGS84/ITRF","","recorded"'), (4, '"unknown","",""')):
            self.assertIn(cells, lines[number])
        rows = list(csv.reader(io.StringIO(text)))
        index = rows[0].index("reference_frame")
        self.assertEqual([row[index:index + 4] for row in rows[1:]], [
            [EXAMPLE, "2010.0", "assigned", rows[1][index + 3]],
            [SYNTHETIC, "", "recorded", rows[2][index + 3]],
            ["WGS84/ITRF", "", "recorded", rows[3][index + 3]],
            ["unknown", "", "", rows[4][index + 3]]])
        self.assertEqual(set(len(row) for row in rows), {len(rows[0])})
        self.assertEqual(rows[0][index + 3], "gate_profile")

    def test_header_and_crs_note_name_the_assignment(self):
        self.assertIn("reference_frame_source", tracking.CRS_NOTE)
        self.assertIn('"assigned"', tracking.CRS_NOTE)
        pid = self.legacy_project()
        self.assertNotIn("frame_assignment", self.collection(pid))
        summary = self.assign(pid, EXAMPLE, "2010.0")["frame_assignment"]
        collection = self.collection(pid)
        self.assertEqual(collection["frame_assignment"], {
            "reference_frame": EXAMPLE, "reference_frame_epoch": "2010.0",
            "assigned_at_utc": summary["assigned_at_utc"], "count": 6})
        self.assertRegex(summary["assigned_at_utc"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(list(collection)[3:5], ["crs_note", "frame_assignment"])
        self.t.revoke_frame_assignment(pid, summary["token"])
        self.assertNotIn("frame_assignment", self.collection(pid))

    def two_lines(self, name, points=0, active="lb"):
        """p1 from a schema-4 backup: paused lines A = [RTK, RTK] and B = [RTK, DGPS, RTK].

        points RTK object points are recorded afterwards with the empty field
        setup (the latest records); then everything is assigned EXAMPLE.
        """
        t = self.tracker(name)
        t.restore_ndjson(legacy_backup(
            [("la", "A", [{}, {}]),
             ("lb", "B", [{}, {"fix_quality": 2, "fix_status": "DGPS/SBAS"},
                          {"fix_quality": 5, "fix_status": "RTK_FLOAT"}])],
            line_state="paused", active_line=active))
        for index in range(points):
            t.add_asset("tap", "", "", fix(80 + index), link_to_active_line=False)
        self.assign("p1", EXAMPLE, tracker=t)
        assignment = t._project("p1")["frame_assignment"]
        self.assertEqual((assignment["lines"], assignment["points"]),
                         ({"la": [2, 2], "lb": [3, 2]}, [points, points]))
        return t

    @staticmethod
    def pull(chunks, feature_id, end=None):
        """Text up to the chunk end after feature_id's feature began (its first chunk without end)."""
        text, inside = "", False
        for chunk in chunks:
            text += chunk
            inside = inside or ('"id":' + json.dumps(feature_id)) in chunk
            if inside and (end is None or chunk == end):
                return text
        raise AssertionError("The export ended before " + feature_id)

    def assert_stopped(self, chunks, text, at_once=True):
        """The export stops with frame_export_changed (at_once: on the next chunk); returns its text."""
        with self.assertRaises(tracking.FrameAssignmentError) as caught:
            if at_once:
                text += next(chunks)
            else:
                for chunk in chunks:
                    text += chunk
        self.assertEqual(caught.exception.code, "frame_export_changed")
        self.assertEqual(str(caught.exception), tracking._FRAME_ASSIGNMENT_MESSAGES[
            "frame_export_changed"])
        with self.assertRaises(ValueError):
            json.loads(text)
        chunks.close()
        return text

    @staticmethod
    def replace_last_vertex(t):
        """Undo the last vertex of B and record an RTK vertex with the empty field setup."""
        t.undo_last()
        t.set_line_state("resume"); t.add_vertex(fix(90)); t.set_line_state("pause")

    def test_a_running_export_stops_when_the_assignment_changes(self):
        with self.subTest("a mixed prefix shrinks"):
            t = self.two_lines("a")
            chunks = t.iter_geojson("p1")
            text = self.pull(chunks, "la", "}}")
            t.undo_last()
            self.assertEqual(t._project("p1")["frame_assignment"]["lines"]["lb"], [2, 1])
            self.assertNotIn('"id":"lb"', self.assert_stopped(chunks, text))
        with self.subTest("undo and a replacement vertex"):
            t = self.two_lines("b")
            chunks = t.iter_geojson("p1")
            text = self.pull(chunks, "la", "}}")
            self.replace_last_vertex(t)
            self.assertNotIn('"id":"lb"', self.assert_stopped(chunks, text))
            collection = self.collection("p1", t)
            self.assertEqual([self.frame_of(values) for values in
                              collection["features"][1]["properties"]["measurements"]],
                             [(EXAMPLE, None, "assigned"), self.UNKNOWN, self.UNKNOWN])
            self.assertEqual(collection["frame_assignment"]["count"], 3)
        with self.subTest("in the middle of a line"):
            t = self.two_lines("c")
            chunks = t.iter_geojson("p1")
            text = self.pull(chunks, "lb", "]}")
            self.replace_last_vertex(t)
            rest = self.assert_stopped(chunks, text, at_once=False)[len(text):]
            self.assertIn('"measurements":[', rest)
            self.assertNotIn("fix_quality", rest)
        for name, change in (
                ("assigned again", lambda t: self.assign("p1", "ETRS89 (other)", tracker=t)),
                ("revoked", lambda t: t.revoke_frame_assignment(
                    "p1", t.status()["projects"][0]["frame_assignment"]["token"]))):
            with self.subTest(name):
                t = self.two_lines(name.replace(" ", "-"))
                chunks = t.iter_geojson("p1")
                text = self.pull(chunks, "la", "}}")
                change(t)
                self.assert_stopped(chunks, text)
        for name, moment in (("before the object points", "lb"), ("after one", None)):
            with self.subTest("object points " + name):
                t = self.two_lines("e-" + name.replace(" ", "-"), points=2)
                self.assertEqual(t.status()["projects"][0]["frame_assignment"]["count"], 6)
                first = t._project("p1")["points"][0]["id"]
                chunks = t.iter_geojson("p1")
                text = (self.pull(chunks, "lb", "}}") if moment else self.pull(chunks, first))
                t.undo_last()
                self.assertEqual(t._project("p1")["frame_assignment"]["points"], [1, 1])
                text = self.assert_stopped(chunks, text)
                self.assertEqual(text.count('"id":' + json.dumps(first)), 0 if moment else 1)
        # Changes outside the scope leave the running export as it was.
        for name, active, at in (("another project", None, ("la", "}}")),
                                 ("behind the scope", "la", ("la", "]}"))):
            with self.subTest(name):
                t = self.two_lines(name.replace(" ", "-"), active=active)
                before = self.collection("p1", t)
                chunks = t.iter_geojson("p1")
                text = self.pull(chunks, *at)
                if active is None:
                    t.create_project("Other"); t.start_line("O"); t.add_vertex(fix(95))
                else:
                    t.set_line_state("resume"); t.add_vertex(fix(95)); t.set_line_state("pause")
                    self.assertEqual(len(t._project("p1")["lines"][0]["coordinates"]), 3)
                text += "".join(chunks)
                self.assertEqual(json.loads(text), before)
        # The CSV is built in one step without a yield, so no change can reach into it.
        self.assertIs(inspect.isgeneratorfunction(tracking.Tracker.csv), False)


if __name__ == "__main__":
    unittest.main()
