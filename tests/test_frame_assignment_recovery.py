# SPDX-License-Identifier: AGPL-3.0-only
"""#39: the frame assignment through backup, restore, archives and power cuts."""
import csv
import hashlib
import io
import json
import os
import shutil
import unittest
from unittest import mock

import support
from support import state  # noqa: F401 - MicroPython compatibility
import cfg
import tracking
import test_frame_assignment as base
from powercut import Injector, PowerCut
from test_frame_assignment import (DGPS_FIX, EXAMPLE, SYNTHETIC, FrameFixture, digests,
                                   legacy_backup)
from test_restore_entry import fix

INVALID = "A frame assignment is invalid."
DGPS_LEGACY = {"fix_quality": 2, "fix_status": "DGPS/SBAS"}
FLOAT_LEGACY = {"fix_quality": 5, "fix_status": "RTK_FLOAT"}
# The clock while a power-cut test assigns or revokes, so every run writes the same bytes.
FROZEN_NOW = 846_400_000


def without_order(value):
    """A copy of an export without record_order, which a restore renumbers."""
    if isinstance(value, dict):
        return dict((key, without_order(item)) for key, item in value.items()
                    if key != "record_order")
    if isinstance(value, list):
        return [without_order(item) for item in value]
    return value


def backup_text(tracker, project_id=None):
    return "".join(tracker.iter_backup_ndjson(project_id))


def records(text):
    return [json.loads(raw) for raw in text.splitlines() if raw.strip()]


def with_integrity(rows):
    """NDJSON text of rows (integrity record dropped) with a newly computed integrity record."""
    raws = [json.dumps(row) + "\n" for row in rows if row.get("record") != "integrity"]
    digest = hashlib.sha256("".join(raws).encode("utf-8")).hexdigest()
    return "".join(raws) + json.dumps({"record": "integrity", "algorithm": "sha256",
                                       "content_sha256": digest, "records": len(raws)}) + "\n"


class RecoveryFixture(FrameFixture):
    def tracker_in(self, name):
        """A tracker in its own folder below the temporary folder."""
        folder = os.path.join(self.tmp, name)
        os.mkdir(folder)
        tracker = tracking.Tracker(os.path.join(folder, "survey"))
        self.addCleanup(self.close, tracker)
        return tracker

    def holding(self, tracker, pid):
        """The survey as #39 sees it: assignment, frame fields, GeoJSON without record_order."""
        project = tracker._project(pid)
        return (json.loads(json.dumps(project.get("frame_assignment"))),
                tracking._frame_fields(tracker._project_frame_tally(project)),
                without_order(json.loads("".join(tracker.iter_geojson(pid)))))

    def restored(self, text, name):
        tracker = self.tracker_in(name)
        self.run_async(tracker.restore_ndjson_async(text.splitlines(keepends=True)))
        return tracker

    def target(self, name):
        """A tracker with its own survey (project "Target", one line), for refused restores."""
        tracker = self.tracker_in(name)
        tracker.create_project("Target")
        tracker.start_line("T"); tracker.add_vertex(fix(70)); tracker.add_vertex(fix(71))
        tracker.set_line_state("finish")
        tracker._sync_journal(close=False); tracker.point_store.sync()
        return tracker

    def assert_refused(self, tracker, text, message=None):
        """A restore of text into tracker fails and leaves its survey and files byte for byte."""
        folder = os.path.dirname(tracker.path)
        files, before = digests(folder), backup_text(tracker)
        with self.assertRaises(ValueError) as caught:
            self.run_async(tracker.restore_ndjson_async(text.splitlines(keepends=True)))
        if message is not None:
            self.assertEqual(str(caught.exception), message)
        self.assertEqual(backup_text(tracker), before)
        self.assertEqual(digests(folder), files)
        return caught.exception


class FrameAssignmentRestoreTests(RecoveryFixture, unittest.TestCase):
    """Backup, restore and archives carry the assignment exactly (E8, E10, E16)."""

    def test_backup_and_restore_keep_the_assignment(self):
        pid = self.legacy_project()
        line1 = self.t._project(pid)["lines"][1]["id"]
        self.assign(pid, EXAMPLE, "2010.0")
        self.t.undo_last()
        self.assertEqual(self.t._project(pid)["frame_assignment"]["lines"],
                         {"l0": [4, 3], line1: [1, 1]})
        expected = self.holding(self.t, pid)
        self.assertEqual(expected[1]["frames"], {EXAMPLE: 5, "unknown": 1, SYNTHETIC: 1,
                                                 "WGS84/ITRF": 1})
        copy = self.restored(backup_text(self.t), "ndjson")
        self.assertEqual(self.holding(copy, pid), expected)
        self.assertEqual(self.holding(self.reload(copy), pid), expected)
        copy = self.tracker_in("json")
        self.run_async(copy.restore_async(json.loads(json.dumps(self.t.backup()))))
        self.assertEqual(self.holding(copy, pid), expected)
        self.assertEqual(self.holding(self.reload(copy), pid), expected)

    def test_archive_and_reactivation_keep_the_assignment(self):
        pid = self.legacy_project()
        self.assign(pid, EXAMPLE)
        self.t.set_line_state("finish")
        expected = self.holding(self.t, pid)
        archive = self.run_async(self.t.archive_project_async(pid))
        self.assertIsNone(self.t._project(pid))
        self.assertTrue(self.run_async(self.t.verify_archive_async(archive["id"]))["valid"])
        self.run_async(self.t.reactivate_archive_async(archive["id"]))
        self.assertEqual(self.holding(self.t, pid), expected)
        self.assertEqual(self.holding(self.reload(self.t), pid), expected)

    def test_deleting_or_archiving_a_project_drops_its_assignment_token(self):
        with self.subTest("delete"):
            t = self.tracker_in("delete"); pid = self.legacy_project(t)
            self.assign(pid, EXAMPLE, tracker=t)
            self.assertIn(pid, t._frame_tokens)
            t.set_line_state("finish"); t.delete_project(pid)
            self.assertNotIn(pid, t._frame_tokens)
            # The replay of assignment and deletion leaves no entry either.
            self.assertEqual(self.reload(t)._frame_tokens, {})
        with self.subTest("archive and reactivate"):
            t = self.tracker_in("archive"); pid = self.legacy_project(t)
            token = self.assign(pid, EXAMPLE, tracker=t)["frame_assignment"]["token"]
            t.set_line_state("finish")
            archive = self.run_async(t.archive_project_async(pid))
            self.assertNotIn(pid, t._frame_tokens)
            other = t.create_project("Other")["id"]
            t.add_asset("tap", "", "", fix(46), link_to_active_line=False)
            kept = self.assign(other, "B", tracker=t)["frame_assignment"]["token"]
            self.assertEqual(list(t._frame_tokens), [other])
            # Reactivation replaces the whole survey (a restore): the cache is emptied.
            self.run_async(t.reactivate_archive_async(archive["id"]))
            self.assertEqual(t._frame_tokens, {})
            # Each assignment says the same about the same points as before: same token.
            t.revoke_frame_assignment(pid, token)
            t.revoke_frame_assignment(other, kept)
            self.assertNotIn("frame_assignment", t._project(pid))
            self.assertNotIn("frame_assignment", t._project(other))

    def test_a_restore_empties_the_token_cache(self):
        t = self.tracker_in("restore"); pid = self.legacy_project(t)
        unassigned = backup_text(t)
        first = self.assign(pid, EXAMPLE, tracker=t)["frame_assignment"]["token"]
        assigned = backup_text(t)
        t.revoke_frame_assignment(pid, first)
        second = self.assign(pid, "B", tracker=t)["frame_assignment"]["token"]
        self.assertNotEqual(second, first)

        def refused(tracker, token, code):
            journal = base.journal_text(tracker)
            held = json.dumps(tracker._project(pid).get("frame_assignment"))
            with self.assertRaises(tracking.FrameAssignmentError) as caught:
                tracker.revoke_frame_assignment(pid, token)
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(json.dumps(tracker._project(pid).get("frame_assignment")), held)
            self.assertEqual(base.journal_text(tracker), journal)
        self.run_async(t.restore_ndjson_async(unassigned.splitlines(keepends=True)))
        self.assertEqual(t._frame_tokens, {})
        refused(t, first, "no_frame_assignment"); refused(t, second, "no_frame_assignment")
        # A restored assignment: only a token of the same statement revokes it,
        # after a restart (replay) too.
        self.run_async(t.restore_ndjson_async(assigned.splitlines(keepends=True)))
        self.assertEqual(t._frame_tokens, {})
        refused(t, second, "frame_assignment_changed")
        t = self.reload(t)
        refused(t, second, "frame_assignment_changed")
        t.revoke_frame_assignment(pid, first)
        self.assertNotIn("frame_assignment", t._project(pid))

    def test_revoking_gives_back_the_backup_bytes_of_before(self):
        pid = self.legacy_project()
        self.t.status()
        b0 = backup_text(self.t)
        summary = self.assign(pid, EXAMPLE)["frame_assignment"]
        b1 = backup_text(self.t)
        self.assertNotEqual(b1, b0)
        old, new = b0.splitlines(), b1.splitlines()
        self.assertEqual(len(old), len(new))
        self.assertEqual(set(json.loads(a)["record"] for a, b in zip(old, new) if a != b),
                         {"project", "integrity"})
        self.t.revoke_frame_assignment(pid, summary["token"])
        self.assertEqual(backup_text(self.t), b0)
        # The same with a restart on both sides.
        t = self.tracker_in("restarts")
        self.legacy_project(t)
        first = self.reload(t)
        first.status()
        c0 = backup_text(first)
        self.assign(pid, EXAMPLE, tracker=first)
        second = self.reload(first)
        token = second.status()["projects"][0]["frame_assignment"]["token"]
        self.assertNotEqual(backup_text(second), c0)
        second.revoke_frame_assignment(pid, token)
        third = self.reload(second)
        third.status()
        self.assertEqual(backup_text(third), c0)

    def stored_points(self, tracker, pid):
        """Bytes of the point-store files, the object-point measurements, the backup's points."""
        tracker.point_store.sync()
        files = {}
        for name in sorted(os.listdir(self.tmp)):
            if name.endswith(".pjs"):
                with open(os.path.join(self.tmp, name), "rb") as source:
                    files[name] = source.read()
        measurements = [json.dumps(point["measurement"], sort_keys=True)
                        for point in tracker._project(pid)["points"]]
        rows = [row for row in records(backup_text(tracker))
                if row["record"] in ("vertex", "asset")]
        return files, measurements, rows

    def test_assigning_never_rewrites_stored_points(self):
        pid = self.legacy_project()
        before = self.stored_points(self.t, pid)
        self.assertTrue(before[0])
        self.assertEqual(len(before[2]), 9)
        summary = self.assign(pid, EXAMPLE, "2010.0")["frame_assignment"]
        self.assertEqual(self.stored_points(self.t, pid), before)
        self.t.revoke_frame_assignment(pid, summary["token"])
        self.assertEqual(self.stored_points(self.t, pid), before)

    def test_a_restore_with_an_invalid_assignment_is_refused_and_keeps_the_survey(self):
        pid = self.legacy_project()
        line1 = self.t._project(pid)["lines"][1]["id"]
        self.assign(pid, EXAMPLE)
        rows = records(backup_text(self.t))
        project = next(row for row in rows if row["record"] == "project")["project"]
        valid = project["frame_assignment"]
        # l0 = 3 RTK + 1 DGPS vertex, L1 = 2 RTK vertices; points: RTK, RTK with a
        # frame, DGPS (WGS84/ITRF).
        self.assertEqual((valid["lines"], valid["points"]), ({"l0": [4, 3], line1: [2, 2]}, [3, 1]))

        def lines(**changed):
            return dict(valid, lines=dict({"l0": [4, 3], line1: [2, 2]}, **changed))
        cases = {
            "unknown line id": dict(valid, lines={"l0": [4, 3], "line-unknown": [2, 2]}),
            "n beyond the vertices": lines(l0=[5, 3]),
            "c beyond n": lines(l0=[4, 5]),
            "c is 0": lines(l0=[4, 0]),
            "c too large within the bounds": lines(l0=[4, 4]),
            "c too small": lines(l0=[4, 2]),
            "line entry of three": lines(l0=[4, 3, 0]),
            "line entry as text": lines(l0="4,3"),
            "point c too large": dict(valid, points=[3, 2]),
            "point c too small": dict(valid, points=[3, 0]),
            "k beyond the points": dict(valid, points=[4, 1]),
            "points as a number": dict(valid, points=3),
            "frame unknown": dict(valid, reference_frame="unknown"),
            "frame Mixed": dict(valid, reference_frame="Mixed"),
            "frame empty": dict(valid, reference_frame=""),
            "frame untrimmed": dict(valid, reference_frame=" " + EXAMPLE),
            "epoch of 17 characters": dict(valid, reference_frame_epoch="2010.012345678901"),
            "epoch empty text": dict(valid, reference_frame_epoch=""),
            "assigned_at as text": dict(valid, assigned_at="846418467"),
            "an extra key": dict(valid, count=6),
            "a key missing": dict((key, value) for key, value in valid.items()
                                  if key != "reference_frame_epoch"),
            "lines as a list": dict(valid, lines=[["l0", 4, 3], [line1, 2, 2]]),
            "assignment as null": None,
        }
        target = self.target("target")
        for name, assignment in cases.items():
            with self.subTest(name):
                project["frame_assignment"] = assignment
                self.assert_refused(target, with_integrity(rows), INVALID)
        # The same records with the valid assignment restore (the integrity is recomputed right).
        project["frame_assignment"] = valid
        copy = self.restored(with_integrity(rows), "valid")
        self.assertEqual(copy._project(pid)["frame_assignment"], valid)
        self.assertEqual(self.holding(copy, pid), self.holding(self.t, pid))
        # An assignment emptied by undo (lines {}, points [0, 0]) is valid too.
        project["frame_assignment"] = dict(valid, lines={}, points=[0, 0])
        copy = self.restored(with_integrity(rows), "emptied")
        self.assertEqual(copy._project(pid)["frame_assignment"], project["frame_assignment"])
        self.assertEqual(copy.status()["projects"][0]["frame_assignment"]["count"], 0)
        self.assertEqual(self.frames(copy), {"unknown": 7, SYNTHETIC: 1, "WGS84/ITRF": 1})

    def counts_project(self):
        """p1 with L0 (schema 4) and L1 (live), assigned EXAMPLE; see the test below."""
        t = self.t
        t.restore_ndjson(legacy_backup([("l0", "L0", [{}, FLOAT_LEGACY, DGPS_LEGACY])], [{}]))
        with mock.patch.dict(cfg.CONFIG, {"reference_frame": EXAMPLE}):
            t.start_line("L1"); t.add_vertex(fix(42))
            t.add_asset("tap", "R", "", fix(45), link_to_active_line=False)
        t.add_vertex(fix(43)); t.add_vertex(fix(44))
        with mock.patch.dict(cfg.CONFIG, {"measurement_required_fix": "DGPS"}):
            t.add_asset("tap", "D", "", dict(DGPS_FIX, lon_e8=800094000),
                        link_to_active_line=False)
        t.add_asset("tap", "E", "", fix(47), link_to_active_line=False)
        t.set_line_state("pause")
        self.assertEqual(self.frames(), {"unknown": 7, EXAMPLE: 2, "WGS84/ITRF": 1})
        self.assign("p1", EXAMPLE)
        return t._project("p1")["lines"][1]["id"]

    def assert_counts(self, tracker, line1, expected):
        project = tracker._project("p1")
        assignment = project["frame_assignment"]
        # (1) The assignment, scope and count.
        self.assertEqual(json.loads(json.dumps(assignment)), expected)
        self.assertEqual((assignment["lines"], assignment["points"]),
                         ({"l0": [3, 2], line1: [3, 2]}, [4, 2]))
        self.assertEqual(tracker.status()["projects"][0]["frame_assignment"]["count"], 6)
        # (2) The project total against the GeoJSON.
        tally = tracker._project_frame_tally(project)
        self.assertEqual(tally, {EXAMPLE: 8, "unknown": 1, "WGS84/ITRF": 1})
        features = json.loads("".join(tracker.iter_geojson("p1")))["features"]
        exported = [values for feature in features
                    for values in (feature["properties"].get("measurements") or
                                   [feature["properties"]])]
        self.assertEqual(len(exported), 10)
        sources = [values["reference_frame_source"] for values in exported
                   if values["reference_frame"] == EXAMPLE]
        self.assertEqual(tally[EXAMPLE] - 2, 6)
        self.assertEqual(sources.count("assigned"), 6)
        self.assertEqual(sources.count("recorded"), 2)
        # (3) The CSV (object points only) against points[1].
        rows = list(csv.DictReader(io.StringIO(tracker.csv("p1"))))
        self.assertEqual(len(rows), 4)
        self.assertEqual(sum(row["reference_frame_source"] == "assigned" for row in rows),
                         assignment["points"][1])
        self.assertEqual([row["reference_frame_source"] for row in rows
                          if row["reference_frame"] == EXAMPLE],
                         ["assigned", "recorded", "assigned"])
        # (4) Each line on its own.
        for feature, line_id, frames in ((features[0], "l0", {EXAMPLE: 2, "unknown": 1}),
                                         (features[1], line1, {EXAMPLE: 3})):
            properties = feature["properties"]
            self.assertEqual(feature["id"], line_id)
            self.assertEqual(sum(values["reference_frame_source"] == "assigned"
                                 for values in properties["measurements"]),
                             assignment["lines"][line_id][1])
            self.assertEqual(properties["reference_frames"], frames)

    def test_restored_counts_match_the_exported_measurements(self):
        line1 = self.counts_project()
        expected = json.loads(json.dumps(self.t._project("p1")["frame_assignment"]))
        self.assert_counts(self.t, line1, expected)
        copy = self.restored(backup_text(self.t), "first")
        self.assert_counts(copy, line1, expected)
        again = self.restored(backup_text(copy), "second")
        self.assert_counts(again, line1, expected)

    # --- a backup while the assignment changes (E16, R2-2) ---

    def two_lines(self, name, active="lb"):
        """Task 3's fixture: A = [RTK, RTK], B = [RTK, DGPS, RTK], assigned {A: [2, 2], B: [3, 2]}.

        A live object point, recorded and undone again behind the scope,
        registers the measurement profile before a backup starts: a backup
        lists the profiles once, in its header, and a replacement vertex
        names that profile.
        """
        t = base.FrameAssignmentExportTests.two_lines(self, name, active=active)
        assignment = t._project("p1")["frame_assignment"]
        t.add_asset("tap", "", "", fix(85), link_to_active_line=False)
        t.undo_last()
        self.assertIs(t._project("p1")["frame_assignment"], assignment)
        self.assertEqual(len(t.profiles), 1)
        return t

    @staticmethod
    def line_b(tracker):
        line = tracker._project("p1")["lines"][1]
        return line["id"], [list(value) for value in line["coordinates"]]

    def pull_with_a_change(self, chunks, count, change):
        """Pull count chunks, run change, pull the rest: (text, text before the change, error)."""
        text = "".join(next(chunks) for _ in range(count))
        pulled, error = text, None
        change()
        try:
            for chunk in chunks:
                text += chunk
        except tracking.FrameAssignmentError as caught:
            error = caught
        finally:
            chunks.close()
        return text, pulled, error

    def backup_with_a_change(self, position, replace):
        """Pull position + 1 backup records, undo B's last vertex (and replace it), pull the rest.

        Returns (text, text before the change, error, B before, B after the change).
        """
        t = self.two_lines("change-%d-%s" % (position, replace))
        old = (self.line_b(t)[1], [3, 2])
        text, pulled, error = self.pull_with_a_change(
            t.iter_backup_ndjson(), position + 1,
            lambda: (base.FrameAssignmentExportTests.replace_last_vertex(t) if replace
                     else t.undo_last()))
        new = (self.line_b(t)[1], t._project("p1")["frame_assignment"]["lines"]["lb"])
        self.assertEqual(new[1], [2, 1])
        return text, pulled, error, old, new

    def test_a_backup_during_a_change_stops_and_never_restores_wrongly(self):
        # header, project, line A, 2 vertices, line B, 3 vertices, footer, integrity.
        self.assertEqual(len(records(backup_text(self.two_lines("count")))), 11)
        target = self.target("target")
        for replace in (True, False):
            outcomes = []
            for position in range(11):
                label = "position %d, replacement %s" % (position, replace)
                with self.subTest(label):
                    text, pulled, error, old, new = self.backup_with_a_change(position, replace)
                    kinds = [row["record"] for row in records(text)]
                    if error is not None:
                        self.assertEqual(error.code, "frame_export_changed")
                        # At once: no record after the change leaves the stream.
                        self.assertEqual(text, pulled)
                        self.assertNotIn("integrity", kinds)
                        self.assert_refused(target, text)
                        outcomes.append("stopped")
                        continue
                    self.assertEqual(kinds[-1], "integrity")
                    copy = self.restored(text, "copy-%d-%s" % (position, replace))
                    shown = (self.line_b(copy)[1],
                             copy._project("p1")["frame_assignment"]["lines"]["lb"])
                    # Complete: entirely before the change, or (the change came
                    # before the project record) entirely after it.
                    if shown == new and position == 0:
                        outcomes.append("after")
                    else:
                        self.assertEqual(shown, old)
                        outcomes.append("before")
            self.assertEqual(outcomes, ["after"] + ["stopped"] * 8 + ["before"] * 2,
                             "replacement %s" % replace)
        # (b) A backup made completely after the change restores the shrunk scope.
        t = self.two_lines("after")
        base.FrameAssignmentExportTests.replace_last_vertex(t)
        copy = self.restored(backup_text(t), "after-copy")
        self.assertEqual((copy._project("p1")["frame_assignment"]["lines"],
                          copy._project("p1")["frame_assignment"]["points"]),
                         ({"la": [2, 2], "lb": [2, 1]}, [0, 0]))
        self.assertEqual(self.line_b(copy), self.line_b(t))
        self.assertEqual(self.frames(copy), self.frames(t))
        self.assertEqual(self.frames(copy), {EXAMPLE: 3, "unknown": 2})
        features = json.loads("".join(copy.iter_geojson("p1")))["features"]
        self.assertEqual([values["reference_frame_source"]
                          for values in features[1]["properties"]["measurements"]],
                         ["assigned", None, None])
        # (c) An archive stops the same way and is not made.
        self.archive_during_a_change()
        # (d) A full backup: p1 changes while the next project is written;
        # the check before the footer stops it.
        t = self.two_lines("full", active=None)
        t.create_project("Other")
        t.add_asset("tap", "", "", fix(86), link_to_active_line=False)
        token = t.status()["projects"][0]["frame_assignment"]["token"]
        # header, p1 (project, 2 lines, 5 vertices), the project record of Other.
        text, pulled, error = self.pull_with_a_change(
            t.iter_backup_ndjson(), 10, lambda: t.revoke_frame_assignment("p1", token))
        self.assertEqual(json.loads(pulled.splitlines()[-1])["project"]["name"], "Other")
        self.assertEqual(error.code, "frame_export_changed")
        self.assertEqual([row["record"] for row in records(text)][10:], ["asset"])
        self.assert_refused(target, text)

    def archive_during_a_change(self):
        """Archive p1 (not the active project) while a second client revokes its assignment."""
        t = self.two_lines("archive", active=None)
        t.create_project("Other")
        token = t.status()["projects"][0]["frame_assignment"]["token"]
        archives = list(t.archives)
        os.makedirs(t.archive_dir, exist_ok=True)
        files = sorted(os.listdir(t.archive_dir))
        original = tracking.Tracker._finish_steps_async

        async def finish(steps):
            def wrapped():
                done = False
                try:
                    for item in steps:
                        yield item
                        if item is None and not done:
                            # A second client revokes the assignment.
                            done = True; t.revoke_frame_assignment("p1", token)
                finally:
                    steps.close()
            return await original(wrapped())
        with mock.patch.object(tracking.Tracker, "_finish_steps_async", staticmethod(finish)):
            with self.assertRaises(tracking.FrameAssignmentError) as caught:
                self.run_async(t.archive_project_async("p1"))
        self.assertEqual(caught.exception.code, "frame_export_changed")
        self.assertIsNotNone(t._project("p1"))
        self.assertNotIn("frame_assignment", t._project("p1"))
        self.assertEqual(t.archives, archives)
        self.assertEqual(sorted(os.listdir(t.archive_dir)), files)

    def test_a_damaged_assignment_in_the_checkpoint_is_ignored_on_start(self):
        pid = self.legacy_project()
        unassigned = self.frames()
        self.assign(pid, EXAMPLE)
        self.run_async(self.t.compact_async())
        project = self.t._project(pid)
        project["frame_assignment"] = dict(project["frame_assignment"], lines="x")
        self.t._write_checkpoint(); self.t._write_index()
        restarted = self.reload(self.t)
        self.assertEqual(restarted._project(pid)["frame_assignment"]["lines"], "x")
        self.assertEqual(self.frames(restarted), unassigned)
        self.assertNotIn("frame_assignment", restarted.status()["projects"][0])
        self.assertIsNone(restarted.live_status()["active_project"]["frame_assignment"])
        self.assertNotIn('"assigned"', "".join(restarted.iter_geojson(pid)))
        self.assertNotIn('"assigned"', restarted.csv(pid))


class FrameAssignmentPowerCutTests(RecoveryFixture, unittest.TestCase):
    """A power cut at any write of an assignment, a revocation or a compaction (E3, E7)."""

    def setUp(self):
        super().setUp()
        patch = support.patch_module_global("tracking", "_now_seconds", return_value=FROZEN_NOW)
        patch.start(); self.addCleanup(patch.stop)

    def prepared(self, assigned):
        """A closed survey folder with legacy_project, assigned EXAMPLE when assigned is true."""
        folder = os.path.join(self.tmp, "prepared")
        os.mkdir(folder)
        t = tracking.Tracker(os.path.join(folder, "survey"))
        try:
            self.legacy_project(t)
            if assigned:
                self.assign("p1", EXAMPLE, tracker=t)
        finally:
            self.close(t)
        return folder

    def copy(self, folder, name):
        target = os.path.join(self.tmp, name)
        if os.path.exists(target):
            shutil.rmtree(target)
        shutil.copytree(folder, target)
        return target

    def view(self, folder):
        """Backup bytes after a restart of folder (line summaries filled first)."""
        t = tracking.Tracker(os.path.join(folder, "survey"))
        try:
            t.status()
            return backup_text(t)
        finally:
            self.close(t)

    def cut_everywhere(self, folder, action):
        """Run action(tracker) on a copy of folder, cut at each of its writes; the outcomes seen."""
        before = self.view(self.copy(folder, "before"))
        done = self.copy(folder, "after")
        t = tracking.Tracker(os.path.join(done, "survey"))
        try: action(t)
        finally: self.close(t)
        after = self.view(done)
        self.assertNotEqual(after, before)
        probe = self.copy(folder, "probe")
        with Injector(probe) as injector:
            t = tracking.Tracker(os.path.join(probe, "survey"))
            start = injector.count
            action(t)
            total = injector.count
            self.close(t)
        self.assertGreater(total, start)
        outcomes = set()
        for crash_at in range(start + 1, total + 1):
            label = "crash at write %d (%d..%d)" % (crash_at, start + 1, total)
            run, image = self.copy(folder, "run"), os.path.join(self.tmp, "image")
            if os.path.exists(image):
                shutil.rmtree(image)
            with Injector(run, image, crash_at):
                t = tracking.Tracker(os.path.join(run, "survey"))
                with self.assertRaises(PowerCut, msg=label):
                    action(t)
            restarted = self.view(image)
            self.assertIn(restarted, (before, after), label)
            outcomes.add("before" if restarted == before else "after")
            # The start completes the recovery: a second one agrees.
            self.assertEqual(self.view(image), restarted, label)
        return outcomes

    def test_a_power_cut_while_assigning_leaves_before_or_after(self):
        folder = self.prepared(False)

        def assign(t):
            shown = self.preview("p1", t)
            self.run_async(t.assign_frame_async("p1", EXAMPLE, "", shown["total"],
                                                shown["scope_token"]))
        self.assertEqual(self.cut_everywhere(folder, assign), {"before", "after"})

    def test_a_power_cut_while_revoking_leaves_before_or_after(self):
        folder = self.prepared(True)

        def revoke(t):
            t.revoke_frame_assignment(
                "p1", t.status()["projects"][0]["frame_assignment"]["token"])
        self.assertEqual(self.cut_everywhere(folder, revoke), {"before", "after"})

    def test_a_power_cut_while_compacting_keeps_the_assignment(self):
        folder = self.prepared(True)
        reference = self.view(self.copy(folder, "reference"))
        t = tracking.Tracker(os.path.join(self.copy(folder, "check"), "survey"))
        try:
            expected = self.holding(t, "p1")
            self.assertEqual(expected[0]["lines"]["l0"], [4, 3])
        finally:
            self.close(t)
        probe = self.copy(folder, "probe")
        with Injector(probe) as injector:
            t = tracking.Tracker(os.path.join(probe, "survey"))
            start = injector.count
            self.run_async(t.compact_async())
            total = injector.count
            self.close(t)
        self.assertEqual(self.view(probe), reference)
        self.assertGreater(total, start + 5)
        for crash_at in range(start + 1, total + 1):
            label = "crash at write %d (%d..%d)" % (crash_at, start + 1, total)
            run, image = self.copy(folder, "run"), os.path.join(self.tmp, "image")
            if os.path.exists(image):
                shutil.rmtree(image)
            with Injector(run, image, crash_at):
                t = tracking.Tracker(os.path.join(run, "survey"))
                with self.assertRaises(PowerCut, msg=label):
                    self.run_async(t.compact_async())
            self.assertEqual(self.view(image), reference, label)
            t = tracking.Tracker(os.path.join(image, "survey"))
            try:
                self.assertEqual(self.holding(t, "p1"), expected, label)
            finally:
                self.close(t)


if __name__ == "__main__":
    unittest.main()
