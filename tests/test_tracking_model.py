# SPDX-License-Identifier: AGPL-3.0-only
"""The journal event model: every kind applies the same live, on replay and from a checkpoint."""
import asyncio
import contextlib
import json
import os
import shutil
import tempfile
import unittest
import warnings
from unittest import mock

import support
from support import state  # noqa: F401 - MicroPython compatibility
import cfg
import pointstore
from pointstore import FlashPointStore
import tracking
from test_restore_entry import fix, rich_survey  # noqa: F401 - shared fixtures

CLOCK_START = 1_800_000_000


def record_every_kind(tracker, clock):
    """Every kind a live survey writes, a profile change and a capacity pause."""
    def at(): clock[0] += 1
    alpha = tracker.create_project("Alpha", "first")["id"]; at()
    tracker.start_line("A1", .2, {"k": "v"}, fix(0, 1)); at()
    for index in range(1, 4): tracker.add_vertex(fix(index, 1)); at()
    with mock.patch.dict(cfg.CONFIG, {"reference_frame": "ETRS89"}):
        tracker.add_asset("control", "K1", "", fix(50, 2)); at()
    tracker.set_line_state("pause"); at(); tracker.set_line_state("resume"); at()
    with mock.patch.dict(cfg.CONFIG, {"measurement_max_hdop": 1.4, "reference_frame": "ETRS89"}):
        tracker.add_vertex(fix(4, 1)); at()
    tracker.undo_last(); at()
    tracker.set_line_state("finish", {"note": "d"}); at()
    beta = tracker.create_project("Beta")["id"]; at()
    tracker.add_asset("tap", "", "", fix(60, 5), link_to_active_line=False); at()
    tracker.add_asset("tree", "", "", fix(61, 5), link_to_active_line=False); at()
    tracker.undo_last(); at()
    tracker.rename_project(beta, "Beta 2"); at()
    tracker.select_project(alpha); at()
    gamma = tracker.create_project("Gamma")["id"]; tracker.delete_project(gamma); at()
    tracker.select_project(beta); at()
    tracker.start_line("B1", .2, recording_mode="route"); at()
    for index in range(3): tracker.add_vertex(fix(20 + index, 4), source="automatic"); at()
    tracker._pause_for_capacity()
    return alpha, beta


def _without_volatile(value):
    """Drop the cached line summary and the activity time (both rebuilt on load)."""
    if isinstance(value, dict):
        return dict((key, _without_volatile(item)) for key, item in value.items()
                    if key not in ("_summary", "updated_at"))
    if isinstance(value, list): return [_without_volatile(item) for item in value]
    return value


def persistent(tracker):
    """What a reload must reproduce: backup, profiles, summaries and line lengths."""
    s = tracker.status()
    return _without_volatile(json.loads(json.dumps({
        "backup": tracker.backup(), "profiles": tracker.profiles,
        "projects": s["projects"], "active_project_lines": s["active_project_lines"],
        "active_line": dict(s["active_line"] or {}, recording_sec=None),
        "lengths": dict((key, round(value, 6)) for key, value in tracker._line_lengths.items())})))


def damage_checkpoint(path, variant):
    """Point a checkpoint record at a parent that does not exist.

    "line"/"asset": the first record of that kind names "project-nowhere";
    "vertex": a vertex for "line-nowhere" goes in before the footer.
    """
    with open(path) as source: rows = source.read().splitlines(True)
    if variant == "vertex":
        rows.insert(len(rows) - 1, json.dumps(
            {"record": "vertex", "line_id": "line-nowhere",
             "coordinate": [800000000, 5000000000, 1000], "measurement": {}}) + "\n")
    else:
        index = [json.loads(raw)["record"] for raw in rows].index(variant)
        record = json.loads(rows[index]); record["project_id"] = "project-nowhere"
        rows[index] = json.dumps(record) + "\n"
    with open(path, "w") as target: target.write("".join(rows))


def files(folder):
    """Relative path -> content of every file below folder."""
    found = {}
    for root, _, names in os.walk(folder):
        for name in names:
            full = os.path.join(root, name)
            with open(full, "rb") as source:
                found[os.path.relpath(full, folder)] = source.read()
    return found


class ModelFixture:
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory(prefix="esp-rtk-tracking-model-")
        self.addCleanup(self.tmp.cleanup)
        catcher = warnings.catch_warnings()
        catcher.__enter__(); self.addCleanup(catcher.__exit__, None, None, None)
        warnings.simplefilter("ignore", ResourceWarning)
        self.clock = [CLOCK_START]
        for patch in (mock.patch.object(tracking.Tracker, "storage_capacity",
                                        return_value={"writable": True}),
                      support.patch_module_global("tracking", "log"),
                      mock.patch.object(tracking.time, "time", lambda: self.clock[0]),
                      mock.patch("uasyncio.sleep_ms", lambda ms: asyncio.sleep(0))):
            patch.start(); self.addCleanup(patch.stop)

    def tracker(self, name, **options):
        tracker = tracking.Tracker(os.path.join(self.tmp.name, name), **options)
        self.addCleanup(self.close, tracker)
        return tracker

    @staticmethod
    def close(tracker):
        try: tracker._sync_journal(close=True); tracker.point_store.close()
        except Exception: pass

    def reload(self, tracker):
        tracker._sync_journal(close=True); tracker.point_store.close()
        reloaded = tracking.Tracker(tracker.path)
        self.addCleanup(self.close, reloaded)
        return reloaded


class HeldGenerators:
    """Holds every generator of a chain so CPython cannot close it by reference counting.

    MicroPython has no reference counting: a generator dropped without close()
    keeps its file handles and finally blocks pending.
    """
    def setUp(self):
        super().setUp()
        self.held = []

    def hold(self, owner, names):
        for name in names:
            patch = mock.patch.object(owner, name, self._holding(name, getattr(owner, name)))
            patch.start(); self.addCleanup(patch.stop)

    def _holding(self, name, function):
        def wrapper(*args, **kwargs):
            generator = function(*args, **kwargs)
            self.held.append((name, generator))
            return generator
        return wrapper

    def open_generators(self):
        return [name for name, generator in self.held if generator.gi_frame is not None]


class EventModelTests(ModelFixture, unittest.TestCase):
    def test_live_replay_and_checkpoint_agree(self):
        t = self.tracker("survey")
        record_every_kind(t, self.clock)
        live = persistent(t)
        self.assertEqual([p["frames"] for p in live["projects"]],
                         [{"unknown": 4, "ETRS89": 1}, {"unknown": 4}])
        self.assertEqual(len(live["profiles"]), 2)
        self.assertEqual(live["active_line"]["pause_reason"], "capacity_limit")
        r = self.reload(t)
        self.assertEqual(persistent(r), live)
        asyncio.run(r.compact_async())
        c = self.reload(r)
        self.assertEqual(persistent(c), live)
        # A checkpoint followed by a journal rest.
        c.set_line_state("resume")
        c.add_vertex(fix(30, 4))
        c.add_asset("tap", "", "", fix(31, 5))
        c.set_line_state("finish")
        expected = persistent(c)
        self.assertEqual(persistent(self.reload(c)), expected)

    def test_live_events_publish_map_deltas_and_a_load_starts_over(self):
        t = self.tracker("survey")
        _alpha, beta = record_every_kind(t, self.clock)
        self.assertEqual([c["op"] for c in t._map_changes],
                         ["line", "vertex", "vertex", "vertex", "vertex", "asset", "line",
                          "line", "vertex", "remove", "line", "asset", "asset", "remove",
                          "line", "vertex", "vertex", "vertex", "line"])
        self.assertEqual([c["revision"] for c in t._map_changes],
                         [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 14, 15, 16, 22, 23, 24, 25, 26])
        self.assertEqual(t._event_sequence, 26)
        delta = json.loads("".join(t.iter_map_data(500, beta, 16)))
        self.assertIs(delta["delta"], True)
        self.assertEqual([c["op"] for c in delta["changes"]],
                         ["line", "vertex", "vertex", "vertex", "line"])
        r = self.reload(t)
        self.assertEqual(r._map_changes, [])
        self.assertEqual(r._map_change_floor, r._event_sequence)
        self.assertNotIn("delta", json.loads("".join(r.iter_map_data(500, beta, 0))))

    def test_activity_follows_the_kinds_that_count(self):
        t = self.tracker("survey")
        holder = {}

        def updated(project_id):
            return [p["updated_at"] for p in t.status()["projects"] if p["id"] == project_id][0]

        def create_alpha(): holder["alpha"] = t.create_project("Alpha")["id"]
        steps = [("create_project", create_alpha, True),
                 ("start_line", lambda: t.start_line("L", .2), True),
                 ("add_vertex", lambda: t.add_vertex(fix(1, 1)), True),
                 ("add_asset", lambda: t.add_asset("tree", "T", "", fix(2, 1)), True),
                 ("pause", lambda: t.set_line_state("pause"), True),
                 ("resume", lambda: t.set_line_state("resume"), True),
                 ("finish", lambda: t.set_line_state("finish"), True),
                 ("undo_last", t.undo_last, True),
                 ("rename_project", lambda: t.rename_project(holder["alpha"], "Alpha 2"), True),
                 ("create other project", lambda: t.create_project("Beta"), False),
                 ("select_project", lambda: t.select_project(holder["alpha"]), False)]
        for label, step, counts in steps:
            with self.subTest(label):
                self.clock[0] += 100
                before = updated(holder["alpha"]) if "alpha" in holder else None
                step()
                self.assertEqual(updated(holder["alpha"]),
                                 tracking._now_seconds() if counts else before)

    def test_events_without_a_target_change_nothing(self):
        t = self.tracker("survey")
        project_id = t.create_project("P")["id"]
        t.status()
        # (a) A vertex for a line that does not exist stores nothing.
        features = t.feature_count()
        t._apply({"event": "vertex_added", "project_id": project_id, "line_id": "missing",
                  "coordinate": [800000000, 5000000000, 1000],
                  "measurement": {"recorded_at": 8, "precision": "e8"},
                  "order": t._event_sequence + 1})
        self.assertEqual(t.feature_count(), features)
        # (b) A removal in a project that does not exist publishes nothing.
        t.status()
        cache = t._status_cache
        changes = list(t._map_changes)
        t._apply({"event": "feature_removed", "project_id": "nowhere",
                  "feature_kind": "asset", "order": 99})
        self.assertIs(t._status_cache, cache)
        self.assertEqual(t._map_changes, changes)
        # (c) A second creation of the same project keeps the first one.
        t._apply({"event": "project_created",
                  "project": {"id": project_id, "name": "Other", "description": "",
                              "created_at": 5, "lines": [], "points": []},
                  "order": t._event_sequence + 1})
        self.assertEqual(len(t.projects), 1)
        self.assertEqual(t.projects[0]["name"], "P")
        self.assertEqual(t.active_project_id, project_id)

    def test_events_of_odd_kinds_change_nothing_live_or_on_replay(self):
        t = self.tracker("survey")
        project_id = t.create_project("P")["id"]
        odd = [dict(entry, project_id=project_id, at=CLOCK_START + 10 ** 6)
               for entry in ({}, {"event": "bogus_kind"}, {"event": 7}, {"event": []},
                             {"event": {}})]
        for entry in odd:
            with self.subTest("live", kind=entry.get("event", "<missing>")):
                t.status()
                projects = json.dumps(t.projects, sort_keys=True)
                activity = dict(t._project_activity)
                changes = list(t._map_changes)
                t._apply(dict(entry, order=t._event_sequence + 1))
                self.assertIsNone(t._status_cache)
                self.assertEqual(t._map_changes, changes)
                self.assertEqual(t._project_activity, activity)
                self.assertEqual(json.dumps(t.projects, sort_keys=True), projects)
        r1 = self.reload(t)
        expected = persistent(r1)
        updated = [p["updated_at"] for p in r1.status()["projects"]]
        r1._sync_journal(close=True)
        segment = r1._segment_files()[-1][1]
        with open(segment, "a") as journal:
            for offset, entry in enumerate(odd, 1):
                journal.write(json.dumps(dict(entry, schema_version=5,
                                              order=r1._event_sequence + offset)) + "\n")
        with open(segment) as journal: content = journal.read()
        r2 = self.reload(r1)
        self.assertEqual(persistent(r2), expected)
        self.assertEqual([p["updated_at"] for p in r2.status()["projects"]], updated)
        self.assertEqual(r2._event_sequence, r1._event_sequence + 5)
        with open(segment) as journal: self.assertEqual(journal.read(), content)
        self.assertEqual([name for name in os.listdir(self.tmp.name) if name.endswith(".corrupt")],
                         [])

    def test_legacy_kinds_still_apply(self):
        t = self.tracker("survey")
        project_id = t.create_project("P")["id"]
        first = t.start_line("A", .2)["id"]
        t.add_vertex(fix(1, 1))
        t.set_line_state("finish")
        second = t.start_line("B", .2)["id"]
        t.add_vertex(fix(2, 1))
        t.set_line_state("pause")
        t._apply({"event": "project_selected", "project_id": project_id})
        self.assertIsNone(t.active_line_id)
        t._apply({"event": "line_selected", "project_id": project_id, "line_id": second})
        self.assertEqual(t.active_line_id, second)
        t._apply({"event": "line_selected", "project_id": project_id, "line_id": first})
        self.assertEqual(t.active_line_id, second)
        t._apply({"event": "snapshot", "projects": [], "active_project_id": None})
        self.assertEqual(t.projects, [])
        self.assertEqual(t._project_frames, {})
        self.assertEqual(t._project_activity, {})

    def test_a_registered_handler_runs_for_its_kind(self):
        t = self.tracker("survey")
        t.create_project("P")
        calls = []
        result = [None]

        def handler(tracker, event):
            calls.append((tracker, event))
            return result[0]

        with mock.patch.dict(tracking.EVENT_HANDLERS, {"probe_kind": handler}):
            t.status()
            self.assertIsNotNone(t._status_cache)
            event = {"event": "probe_kind", "order": 1}
            t._apply(event)
            self.assertEqual(len(calls), 1)
            self.assertIs(calls[0][0], t)
            self.assertIs(calls[0][1], event)
            self.assertIsNone(t._status_cache)
            # A handler returning False ends the event: no cache reset, no map delta.
            result[0] = False
            t.status()
            cache = t._status_cache
            changes = list(t._map_changes)
            t._apply({"event": "probe_kind", "order": 2})
            self.assertEqual(len(calls), 2)
            self.assertIs(t._status_cache, cache)
            self.assertEqual(t._map_changes, changes)



class SyncWrapperTests(ModelFixture, unittest.TestCase):
    def test_on_fix_goes_through_the_queue(self):
        t = self.tracker("survey")
        t.create_project("P")
        t.start_line("L", .2)
        features = t.feature_count()
        with mock.patch.object(t, "enqueue_fix", wraps=t.enqueue_fix) as enqueue:
            self.assertIs(t.on_fix(dict(fix(5, 0), utc="5")), True)
            self.assertEqual(t.feature_count(), features + 1)
            self.assertEqual(enqueue.call_count, 1)
            self.assertEqual(t.pending_count(), 0)
            # The same epoch at another position is the queue's duplicate.
            self.assertIs(t.on_fix(dict(fix(9, 0), utc="5")), False)
            self.assertEqual(t.feature_count(), features + 1)

    def test_compact_runs_the_verified_compaction(self):
        t = self.tracker("survey")
        t.create_project("P")
        t.start_line("L", .2)
        for index in range(3): t.add_vertex(fix(index, 1))
        original = tracking.Tracker.compact_async
        with mock.patch.object(tracking.Tracker, "compact_async", autospec=True,
                               side_effect=original) as compact_async:
            t.compact()
        compact_async.assert_called_once_with(t, 0)
        t.add_vertex(fix(3, 1))
        t.add_asset("tree", "T", "", fix(4, 1))  # a journal segment after the checkpoint
        segments = t._segment_files()
        self.assertTrue(segments)
        # The strict point-store check of the verification refuses the compaction.
        with mock.patch.object(pointstore.FlashPointStore, "load_async",
                               side_effect=ValueError("probe")):
            with self.assertRaisesRegex(ValueError, "probe"):
                t.compact()
        self.assertEqual(t._segment_files(), segments)
        for _number, path in segments: self.assertTrue(os.path.exists(path), path)

    def test_sync_and_async_archive_calls_agree(self):
        t = self.tracker("survey")
        project_a = t.create_project("A")["id"]
        t.start_line("A1", .2)
        for index in range(3): t.add_vertex(fix(index, 1))
        t.set_line_state("finish")
        project_b = t.create_project("B")["id"]
        t.add_asset("tree", "T", "", fix(10, 2), link_to_active_line=False)
        a = t.archive_project(project_a)["id"]
        b = t.archive_project(project_b)["id"]
        verified = t.verify_archive(a)
        self.assertTrue(verified["valid"])
        self.assertEqual(asyncio.run(t.verify_archive_async(a)), verified)
        entry = [item for item in t.archives if item["id"] == a][0]
        digest, entry["sha256"] = entry["sha256"], "0" * 64
        mismatch = {"valid": False, "error": "index_checksum_mismatch"}
        self.assertEqual(t.verify_archive(a), mismatch)
        self.assertEqual(asyncio.run(t.verify_archive_async(a)), mismatch)
        entry["sha256"] = digest
        self.assertEqual(t.reactivate_archive(a)["id"], project_a)
        self.assertEqual(asyncio.run(t.reactivate_archive_async(b)),
                         {"id": project_b, "legacy_values_skipped": 0})
        for call in (t.verify_archive, t.reactivate_archive,
                     lambda archive_id: asyncio.run(t.verify_archive_async(archive_id)),
                     lambda archive_id: asyncio.run(t.reactivate_archive_async(archive_id))):
            with self.assertRaisesRegex(ValueError, "Archive not found."):
                call("archive-missing")


class ReactivationCloseTests(ModelFixture, HeldGenerators, unittest.TestCase):
    CHAIN = ("_reactivate_archive_steps", "_verify_archive_steps", "_verify_archive_path_steps",
             "_restore_steps", "_ndjson_records", "_merged_archive_stream")

    def copy(self, template, name):
        folder = os.path.join(self.tmp.name, name)
        shutil.copytree(template, folder)
        return os.path.join(folder, "survey")

    def test_aborting_a_reactivation_at_any_step_closes_its_chain(self):
        template = os.path.join(self.tmp.name, "template"); os.mkdir(template)
        t = rich_survey(tracking.Tracker(os.path.join(template, "survey")))
        alpha = [project["id"] for project in t.projects if project["name"] == "Alpha"][0]
        archive_id = t.archive_project(alpha)["id"]
        old = t.backup(); self.close(t)
        reference = self.tracker(self.copy(template, "reference"))
        reference.reactivate_archive(archive_id)
        new = reference.backup()
        self.assertNotEqual(new, old)
        total = sum(1 for _ in self.tracker(self.copy(template, "count"))
                    ._reactivate_archive_steps(archive_id))
        self.hold(tracking.Tracker, self.CHAIN)
        outcomes, names = [], set()
        for steps in range(1, total + 1):
            with self.subTest(steps=steps):
                path = self.copy(template, "abort-%d" % steps)
                self.held[:] = []
                t = self.tracker(path)
                reactivation = t._reactivate_archive_steps(archive_id)
                for _ in range(steps): next(reactivation)
                reactivation.close()
                names.update(name for name, _generator in self.held)
                self.assertEqual(self.open_generators(), [])
                self.assertEqual([name for name in os.listdir(os.path.dirname(path))
                                  if ".restore" in name], [])
                state = t.backup()
                self.assertIn(state, (old, new))
                outcomes.append(int(state == new))
                self.close(t)
                self.assertEqual(self.tracker(path).backup(), state)
        self.assertEqual((outcomes[0], outcomes[-1]), (0, 1))
        self.assertEqual(outcomes, sorted(outcomes))
        self.assertEqual(names, set(self.CHAIN))


class CheckpointFallbackTests(ModelFixture, unittest.TestCase):
    def copy(self, template, name):
        folder = os.path.join(self.tmp.name, name)
        shutil.copytree(template, folder)
        return os.path.join(folder, "survey")

    def test_a_checkpoint_left_as_tmp_or_previous_is_loaded(self):
        template = os.path.join(self.tmp.name, "template"); os.mkdir(template)
        t = rich_survey(tracking.Tracker(os.path.join(template, "survey")))
        t.compact()
        t.add_asset("tree", "T3", "", fix(70, 6), link_to_active_line=False)
        reference = t.backup(); self.close(t)
        for suffix in (".tmp", ".previous"):
            with self.subTest(suffix=suffix):
                path = self.copy(template, "left" + suffix)
                checkpoint = path + ".checkpoint.json"
                os.rename(checkpoint, checkpoint + suffix)
                self.assertEqual(self.tracker(path).backup(), reference)
                self.assertTrue(os.path.exists(checkpoint))
                self.assertFalse(os.path.exists(checkpoint + suffix))
        # The verification reader takes only the main file.
        path = self.copy(template, "verify")
        checkpoint = path + ".checkpoint.json"
        os.rename(checkpoint, checkpoint + ".tmp")
        self.assertEqual(self.tracker(path, autoload=False)._load_checkpoint(readonly=True), 0)
        # A stream without its footer gives way to the older copy.
        path = self.copy(template, "footless")
        checkpoint = path + ".checkpoint.json"
        with open(checkpoint) as source: complete = source.read()
        with open(checkpoint + ".previous", "w") as target: target.write(complete)
        rows = complete.splitlines(True)
        self.assertEqual(json.loads(rows[-1])["record"], "footer")
        with open(checkpoint, "w") as target: target.write("".join(rows[:-1]))
        self.assertEqual(self.tracker(path).backup(), reference)
        with open(checkpoint) as source: self.assertEqual(source.read(), complete)
        self.assertFalse(os.path.exists(checkpoint + ".previous"))

    def test_a_broken_line_between_events_keeps_both_events(self):
        t = self.tracker("survey")
        alpha = t.create_project("Alpha")["id"]
        t.create_project("Beta")
        t._sync_journal(close=True)
        segment = t._segment_files()[-1][1]
        with open(segment) as source: original = source.read()
        rows = original.splitlines(True)
        self.assertEqual([(json.loads(raw)["event"], json.loads(raw)["project"]["name"])
                          for raw in rows[-2:]],
                         [("project_created", "Alpha"), ("project_created", "Beta")])
        written = "".join(rows[:-1] + ['{"event": "project_cre\n'] + rows[-1:])
        with open(segment, "w") as target: target.write(written)
        folder = os.path.dirname(segment)

        def corrupt_files():
            return sorted(name for name in os.listdir(folder) if name.endswith(".corrupt"))

        def read(path):
            with open(path) as source: return source.read()

        r1 = self.reload(t)
        self.assertEqual([project["name"] for project in r1.projects], ["Alpha", "Beta"])
        self.assertEqual(r1.projects[0]["id"], alpha)
        self.assertEqual(read(segment + ".corrupt"), written)
        self.assertEqual(read(segment), original)
        r2 = self.reload(r1)
        self.assertEqual([project["name"] for project in r2.projects], ["Alpha", "Beta"])
        self.assertEqual(read(segment + ".corrupt"), written)
        self.assertEqual(read(segment), original)
        self.assertEqual(corrupt_files(), [os.path.basename(segment) + ".corrupt"])


class UnloadedCompactionTests(ModelFixture, unittest.TestCase):
    def template(self, name, compact):
        folder = os.path.join(self.tmp.name, name); os.mkdir(folder)
        t = rich_survey(tracking.Tracker(os.path.join(folder, "survey")))
        if compact:
            t.compact()
        else:
            t._write_checkpoint(); t._write_index()
        self.close(t)
        return folder

    def copy(self, template, name):
        folder = os.path.join(self.tmp.name, name)
        shutil.copytree(template, folder)
        return folder, os.path.join(folder, "survey")

    def test_a_start_up_stopped_by_an_unknown_parent_is_never_compacted(self):
        compacted = self.template("compacted", compact=True)
        journal = self.template("journal", compact=False)
        checkpoint = "survey.checkpoint.json"
        names = os.listdir(compacted)
        self.assertIn(checkpoint, names)
        self.assertNotIn(checkpoint + ".tmp", names)
        self.assertNotIn(checkpoint + ".previous", names)
        self.assertEqual([name for name in names if ".segment-" in name], [])
        self.assertNotEqual([name for name in os.listdir(journal) if ".segment-" in name], [])
        cases = (("line", "project-nowhere", KeyError, compacted, "line", False),
                 ("asset", "project-nowhere", KeyError, compacted, "asset", False),
                 ("vertex", "line-nowhere", 0, compacted, "vertex", False),
                 ("older copy present", "project-nowhere", KeyError, compacted, "line", True),
                 ("complete journal", "project-nowhere", KeyError, journal, "asset", False))
        for name, key, readonly_result, template, variant, older_copy in cases:
            with self.subTest(case=name):
                folder, path = self.copy(template, name.replace(" ", "-"))
                if older_copy:
                    shutil.copyfile(path + ".checkpoint.json",
                                    path + ".checkpoint.json.previous")
                damage_checkpoint(path + ".checkpoint.json", variant)
                before = files(folder)
                # 1. The start-up stops as before.
                with self.assertRaises(KeyError) as caught:
                    tracking.Tracker(path)
                self.assertEqual(caught.exception.args, (key,))
                failed = tracking.Tracker(path, autoload=False, point_store_autoload=False)
                self.addCleanup(self.close, failed)
                with self.assertRaises(KeyError):
                    asyncio.run(failed.initialize_async())
                self.assertIs(failed.is_loaded(), False)
                # 2. Neither entry compacts the partial state.
                with self.assertRaisesRegex(ValueError, "not loaded"):
                    asyncio.run(failed.compact_async())
                with self.assertRaisesRegex(ValueError, "not loaded"):
                    failed.compact()
                failed.point_store.close()
                self.assertEqual(files(folder), before)
                # 3. A second start-up and the verification reader see the same files.
                with self.assertRaises(KeyError):
                    tracking.Tracker(path)
                reader = tracking.Tracker(path, autoload=False)
                self.addCleanup(self.close, reader)
                if readonly_result is KeyError:
                    with self.assertRaises(KeyError):
                        reader._load_checkpoint(readonly=True)
                else:
                    self.assertEqual(reader._load_checkpoint(readonly=True), readonly_result)
                reader.point_store.close()
                self.assertEqual(files(folder), before)


class BootCloseTests(ModelFixture, HeldGenerators, unittest.TestCase):
    CHAIN = ("_replay_segment_steps", "_refresh_summaries_steps", "_iter_line_summary")

    def copy(self, template, name):
        folder = os.path.join(self.tmp.name, name)
        shutil.copytree(template, folder)
        return os.path.join(folder, "survey")

    def start(self, path):
        """The boot replay as _load drives it."""
        t = tracking.Tracker(path, autoload=False)
        self.addCleanup(self.close, t)
        t._replaying = True
        return t, t._load_steps()

    def test_aborting_the_boot_replay_at_any_step_closes_its_steps(self):
        template = os.path.join(self.tmp.name, "template"); os.mkdir(template)
        t = rich_survey(tracking.Tracker(os.path.join(template, "survey")))
        t.compact()
        t.add_vertex(fix(23, 4))  # the checkpoint's summary of B1 is outdated
        t.add_asset("tap", "", "", fix(31, 5))  # a journal rest to replay
        self.close(t)
        reference = self.tracker(self.copy(template, "reference")).backup()
        total = sum(1 for _ in self.start(self.copy(template, "count"))[1])
        self.hold(tracking.Tracker, self.CHAIN)
        names = set()
        for steps in range(1, total + 1):
            with self.subTest(steps=steps):
                path = self.copy(template, "abort-%d" % steps)
                self.held[:] = []
                t, load = self.start(path)
                for _ in range(steps): next(load)
                load.close()
                names.update(name for name, _generator in self.held)
                self.assertEqual(self.open_generators(), [])
                self.close(t)
                self.assertEqual(self.tracker(path).backup(), reference)
        self.assertEqual(names, set(self.CHAIN))


class BootCancelTests(ModelFixture, HeldGenerators, unittest.TestCase):
    CHAIN = ("_load_steps", "_replay_segment_steps", "_refresh_summaries_steps",
             "_iter_line_summary")

    def copy(self, template, name):
        folder = os.path.join(self.tmp.name, name)
        shutil.copytree(template, folder)
        return os.path.join(folder, "survey")

    async def sleep(self, ms):
        # uasyncio delivers Task.cancel() to a task waiting in sleep_ms.
        self.sleeps += 1
        if self.sleeps == self.cancel_at:
            self.alive.update(self.open_generators())
            asyncio.current_task().cancel()
        await asyncio.sleep(0)

    def boot(self, path):
        t = tracking.Tracker(path, autoload=False, point_store_autoload=False)
        self.addCleanup(self.close, t)
        with mock.patch("uasyncio.sleep_ms", self.sleep):
            try: asyncio.run(t.initialize_async())
            finally: self.booted = t
        return t

    def test_cancelling_initialize_async_at_any_sleep_closes_the_load_chain(self):
        template = os.path.join(self.tmp.name, "template"); os.mkdir(template)
        t = rich_survey(tracking.Tracker(os.path.join(template, "survey")))
        t.compact()
        beta = t.active_project_id
        for index in range(40): t.rename_project(beta, "Beta %d" % index)  # journal rest
        for index in range(40): t.add_vertex(fix(30 + index, 4))  # outdated B1 summary
        self.close(t)
        reference = self.tracker(self.copy(template, "reference")).backup()
        self.hold(tracking.Tracker, self.CHAIN)
        self.alive, self.sleeps, self.cancel_at = set(), 0, 0
        self.assertTrue(self.boot(self.copy(template, "count")).is_loaded())
        total = self.sleeps
        for cancel_at in range(1, total + 1):
            with self.subTest(cancel_at=cancel_at):
                path = self.copy(template, "cancel-%d" % cancel_at)
                self.held[:] = []
                self.sleeps, self.cancel_at = 0, cancel_at
                with self.assertRaises(asyncio.CancelledError):
                    self.boot(path)
                t = self.booted
                self.assertEqual(self.open_generators(), [])
                self.assertIs(t.is_loaded(), False)
                self.assertIs(t._replaying, False)
                self.close(t)
                self.assertEqual(self.tracker(path).backup(), reference)
        self.assertGreaterEqual(self.alive, set(self.CHAIN))


class ValidatorOrderTests(ModelFixture, unittest.TestCase):
    def test_validate_backup_reports_the_first_problem_in_order(self):
        """Two problems per case: the error names the one checked first."""
        payload = json.loads(json.dumps(rich_survey(self.tracker("survey")).backup()))
        tracking.validate_backup(payload)
        alpha, beta = payload["projects"]
        a1, a2 = alpha["lines"]
        b1 = beta["lines"][0]

        def profile_then_project(p):
            p["profiles"].append({"id": 5}); p["projects"][0]["id"] = ""
        def project_count_then_line(p):
            p["projects"][0]["lines"][0]["state"] = "lost"
        def asset_type_then_line_id(p):
            p["projects"][0]["points"][0]["object_type"] = "ufo"
            p["projects"][1]["lines"][0]["id"] = a1["id"]
        def line_state_then_coordinate(p):
            line = p["projects"][0]["lines"][0]
            line["state"] = "lost"; line["coordinates"][0] = [1.5, 2, 3]
        def coordinate_then_precision(p):
            line = p["projects"][0]["lines"][0]
            line["coordinates"][0] = [1.5, 2, 3]; del line["measurements"][1]["precision"]
        def precision_then_profile(p):
            measurements = p["projects"][0]["lines"][0]["measurements"]
            del measurements[0]["precision"]; measurements[1]["profile_id"] = "profile-nowhere"
        def profile_then_precision(p):
            measurements = p["projects"][0]["lines"][0]["measurements"]
            measurements[0]["profile_id"] = "profile-nowhere"; del measurements[1]["precision"]
        def properties_then_asset_id(p):
            p["projects"][0]["lines"][0]["properties"] = {"a": [1]}
            p["projects"][0]["points"][0]["id"] = None
        def features_then_active_line(p):
            p["active_line_id"] = "line-nowhere"
        def active_line_elsewhere(p):
            p["active_project_id"], p["active_line_id"] = alpha["id"], b1["id"]
        def finished_active_line(p):
            p["active_project_id"], p["active_line_id"] = alpha["id"], a2["id"]
        def distance(p):
            p["auto_distance_m"] = 0.1

        cases = (
            (profile_then_project, {}, "A measurement profile is invalid or duplicated."),
            (project_count_then_line, {"MAX_PROJECTS": 1},
             "Survey backup contains too many projects."),
            (asset_type_then_line_id, {}, "An asset type is invalid."),
            (line_state_then_coordinate, {}, "A survey line state is invalid."),
            (coordinate_then_precision, {}, "coordinate[0] must be an integer in 1e-8 degrees."),
            (precision_then_profile, {}, "Measurement field precision is required."),
            (profile_then_precision, {}, "Measurement field precision is required."),
            (properties_then_asset_id, {}, "Line properties must contain scalar values."),
            (features_then_active_line, {"MAX_FEATURES": 5},
             "Survey backup contains too many features."),
            (active_line_elsewhere, {}, "The active line does not belong to the active project."),
            (finished_active_line, {}, "A finished line cannot be active."),
            (distance, {}, "The automatic point distance is invalid."))
        for damage, limits, expected in cases:
            with self.subTest(case=damage.__name__):
                broken = json.loads(json.dumps(payload))
                damage(broken)
                limits = dict({"MAX_PROJECTS": tracking.MAX_PROJECTS,
                               "MAX_FEATURES": tracking.MAX_FEATURES}, **limits)
                with contextlib.ExitStack() as stack:
                    for name, value in limits.items():
                        stack.enter_context(support.patch_module_global("tracking", name, value))
                    with self.assertRaises(ValueError) as caught:
                        tracking.validate_backup(broken)
                self.assertEqual(str(caught.exception), expected)


class GeojsonCloseTests(ModelFixture, HeldGenerators, unittest.TestCase):
    def test_aborting_a_geojson_stream_closes_the_line_stream(self):
        t = rich_survey(self.tracker("survey"))
        self.hold(tracking.Tracker, ("_iter_geojson_line", "_line_rows"))
        self.hold(FlashPointStore, ("iter_locators",))
        names = set()
        for project in t.projects:
            pid = project["id"]
            total = sum(1 for _ in t.iter_geojson(pid))
            for steps in range(1, total + 1):
                with self.subTest(project=project["name"], steps=steps):
                    self.held[:] = []
                    stream = t.iter_geojson(pid)
                    for _ in range(steps): next(stream)
                    stream.close()
                    names.update(name for name, _generator in self.held)
                    self.assertEqual(self.open_generators(), [])
        self.assertEqual(names, {"_iter_geojson_line", "_line_rows", "iter_locators"})


if __name__ == "__main__":
    unittest.main()
