# SPDX-License-Identifier: AGPL-3.0-only
"""Restore entries keep the backup content; the M1 recovery guarantees hold."""
import ast
import asyncio
import hashlib
import json
import os
import shutil
import tempfile
import types
import unittest
import warnings
from unittest import mock

import support
from support import REPO, web, state  # noqa: F401 - MicroPython compatibility
import tracking
from pointstore import FlashPointStore
from test_tracking import FIX


def tracking_tree():
    """Every module of tracking/ parsed into one syntax tree."""
    folder, body = os.path.join(REPO, "tracking"), []
    for name in sorted(os.listdir(folder)):
        if name.endswith(".py"):
            with open(os.path.join(folder, name), encoding="utf-8") as source:
                body += ast.parse(source.read()).body
    return ast.Module(body=body, type_ignores=[])


def fix(index, step=0):
    return dict(FIX, lon=8 + index * .00002, lon_e8=800000000 + index * 2000,
                lat_e8=5000000000 + step * 1000)


def rich_survey(tracker, paused=False):
    tracker.create_project("Alpha", "first")
    tracker.start_line("A1", .2, {"k": "v"})
    for index in range(4): tracker.add_vertex(fix(index, 1))
    tracker.add_asset("tree", "T1", "on A1", fix(50, 2))
    tracker.add_vertex(fix(4, 1))
    tracker.set_line_state("finish")
    tracker.start_line("A2", .5)
    for index in range(3): tracker.add_vertex(fix(10 + index, 3))
    tracker.set_line_state("finish")
    tracker.create_project("Beta")
    tracker.add_asset("tap", "T2", "", fix(60, 5), link_to_active_line=False)
    tracker.start_line("B1", .2)
    for index in range(3): tracker.add_vertex(fix(20 + index, 4))
    if paused: tracker._pause_for_capacity()
    return tracker


def without_order(value):
    if isinstance(value, dict):
        return dict((key, without_order(item)) for key, item in value.items()
                    if key != "record_order")
    if isinstance(value, list): return [without_order(item) for item in value]
    return value


def invariants(backup):
    """What a restore must keep: features, line lengths, profile IDs, selection."""
    return {"profiles": sorted(profile["id"] for profile in backup["profiles"]),
            "selection": (backup["active_project_id"], backup["active_line_id"],
                          backup["auto_distance_m"]),
            "projects": [(project["id"], project["name"], project.get("description"),
                          [(line["id"], line["name"], line["state"], line.get("properties"),
                            line.get("recording_mode"), line["coordinates"],
                            without_order(line["measurements"]),
                            line["_summary"]["vertex_count"],
                            round(line["_summary"]["length_m"], 6))
                           for line in project["lines"]],
                          without_order(project["points"]))
                         for project in backup["projects"]]}


def with_integrity(rows):
    rows = [raw for raw in rows if not (isinstance(json.loads(raw), dict) and
                                        json.loads(raw).get("record") == "integrity")]
    digest = hashlib.sha256()
    for raw in rows: digest.update(raw.encode("utf-8"))
    return rows + [json.dumps({"record": "integrity", "algorithm": "sha256",
                               "content_sha256": digest.hexdigest(),
                               "records": len(rows)}) + "\n"]


def restore(kind, tracker, source):
    if kind == "json": return tracker.restore(source.backup())
    if kind == "json_async": return asyncio.run(tracker.restore_async(source.backup()))
    rows = list(source.iter_backup_ndjson())
    if kind == "ndjson": return tracker.restore_ndjson(iter(rows))
    return asyncio.run(tracker.restore_ndjson_async(iter(rows)))


ENTRIES = ("json", "json_async", "ndjson", "ndjson_async")


class RestoreFixture:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="esp-rtk-restore-entry-")
        self.addCleanup(self.tmp.cleanup)
        catcher = warnings.catch_warnings()
        catcher.__enter__(); self.addCleanup(catcher.__exit__, None, None, None)
        warnings.simplefilter("ignore", ResourceWarning)
        for patch in (mock.patch.object(tracking.Tracker, "storage_capacity",
                                        return_value={"writable": True}),
                      mock.patch("uasyncio.sleep_ms", lambda ms: asyncio.sleep(0)),
                      support.patch_module_global("tracking", "log")):
            patch.start(); self.addCleanup(patch.stop)

    def tracker(self, name, **options):
        tracker = tracking.Tracker(os.path.join(self.tmp.name, name), **options)
        self.addCleanup(self.close, tracker)
        return tracker

    @staticmethod
    def close(tracker):
        try: tracker.point_store.close(); tracker._sync_journal(True)
        except Exception: pass


class RestoreRoundTripTests(RestoreFixture, unittest.TestCase):
    def test_every_entry_keeps_features_lengths_profiles_and_selection(self):
        source = rich_survey(self.tracker("source"))
        expected = invariants(source.backup())
        self.assertEqual(expected["selection"][0], source.projects[1]["id"])
        for kind in ENTRIES:
            with self.subTest(kind):
                target = self.tracker(kind)
                result = restore(kind, target, source)
                self.assertEqual(result, {"schema_version": 5, "projects": 2,
                                          "features": 13, "legacy_values_skipped": 0})
                self.assertEqual(invariants(target.backup()), expected)
                reloaded = self.tracker(kind, autoload=True)
                self.assertEqual(reloaded.backup(), target.backup())

    def test_a_second_round_trip_is_byte_identical_on_the_host(self):
        source = rich_survey(self.tracker("source"))
        for kind in ENTRIES:
            with self.subTest(kind):
                first = self.tracker(kind + "-1"); restore(kind, first, source)
                second = self.tracker(kind + "-2"); restore(kind, second, first)
                self.assertEqual("".join(second.iter_backup_ndjson()),
                                 "".join(first.iter_backup_ndjson()))
                self.assertEqual(json.dumps(second.backup()), json.dumps(first.backup()))

    def test_a_json_restore_leaves_the_callers_payload_alone(self):
        payload = rich_survey(self.tracker("source")).backup()
        before = json.dumps(payload)
        self.tracker("target").restore(payload)
        self.assertEqual(json.dumps(payload), before)

    def test_each_entry_publishes_exactly_once(self):
        source = rich_survey(self.tracker("source"))
        for kind in ENTRIES:
            with self.subTest(kind):
                activate = mock.patch.object(tracking.Tracker, "_activate_staged_restore_steps",
                    autospec=True, side_effect=tracking.Tracker._activate_staged_restore_steps)
                publish = mock.patch.object(tracking.Tracker, "_write_restore_commit",
                    autospec=True, side_effect=tracking.Tracker._write_restore_commit)
                with activate as activated, publish as published:
                    restore(kind, self.tracker(kind), source)
                self.assertEqual(activated.call_count, 1)
                self.assertEqual(published.call_count, 1)

    def test_json_progress_reports_the_restore_and_its_failure(self):
        source = rich_survey(self.tracker("source"))
        target = self.tracker("target")
        asyncio.run(target.restore_async(source.backup()))
        self.assertEqual(target.operation_snapshot(), {"operation": "restore",
            "state": "complete", "processed": 13, "total": 13, "error": None})
        broken = source.backup(); broken["projects"][0]["lines"][0]["state"] = "lost"
        with self.assertRaisesRegex(ValueError, "line state is invalid"):
            asyncio.run(target.restore_async(broken))
        progress = target.operation_snapshot()
        self.assertEqual((progress["state"], progress["error"]),
                         ("failed", "A survey line state is invalid."))

    def test_an_ndjson_restore_leaves_the_progress_to_its_caller(self):
        source = rich_survey(self.tracker("source"))
        target = self.tracker("target")
        before = target.operation_snapshot()
        restore("ndjson_async", target, source)
        self.assertEqual(target.operation_snapshot(), before)

    def test_ndjson_rows_are_read_as_they_are_written(self):
        source = rich_survey(self.tracker("source"))
        rows = list(source.iter_backup_ndjson())
        vertex_rows = [index for index, raw in enumerate(rows)
                       if json.loads(raw)["record"] == "vertex"]
        pulled, seen = [0], []
        def stream():
            for raw in rows:
                pulled[0] += 1
                yield raw
        append = tracking.Tracker._append
        def observe(tracker, event, defer_index=False):
            if event.get("event") == "vertex_added" and tracker.path.endswith(".restore"):
                seen.append(pulled[0])
            return append(tracker, event, defer_index)
        with mock.patch.object(tracking.Tracker, "_append", observe):
            self.tracker("target").restore_ndjson(stream())
        self.assertEqual(len(seen), len(vertex_rows))
        for row, count in zip(vertex_rows, seen):
            self.assertLessEqual(count, row + 2)

    def old_survey(self):
        """A template store with its own survey; returns (folder, its backup)."""
        template = os.path.join(self.tmp.name, "template"); os.mkdir(template)
        tracker = tracking.Tracker(os.path.join(template, "survey"))
        tracker.create_project("Old"); tracker.start_line("Old line", .2)
        for index in range(3): tracker.add_vertex(fix(index, 9))
        tracker.set_line_state("finish")
        old = tracker.backup(); self.close(tracker)
        return template, old

    @staticmethod
    def async_restore(kind, target, source):
        if kind == "json_async": return target.restore_async(source.backup())
        return target.restore_ndjson_async(iter(list(source.iter_backup_ndjson())))

    def test_a_cancelled_async_restore_keeps_the_old_or_the_new_survey(self):
        source = rich_survey(self.tracker("source"))
        template, old = self.old_survey()
        for kind in ("json_async", "ndjson_async"):
            with self.subTest(kind):
                self.cancel_at_every_turn(kind, source, template, old)

    def cancel_at_every_turn(self, kind, source, template, old):
        def attempt(name, turns):
            folder = os.path.join(self.tmp.name, name); shutil.copytree(template, folder)
            target = tracking.Tracker(os.path.join(folder, "survey"))
            async def run():
                task = asyncio.ensure_future(self.async_restore(kind, target, source))
                for _ in range(turns):
                    if task.done(): return True
                    await asyncio.sleep(0)
                if task.done(): return True
                task.cancel()
                try: await task
                except asyncio.CancelledError: pass
                return False
            finished = asyncio.run(run())
            state = target.backup(); self.close(target)
            # The session itself must not leave staging behind; a reboot would hide it.
            self.assertEqual([n for n in os.listdir(folder) if ".restore" in n], [],
                             (kind, turns))
            rebooted = tracking.Tracker(os.path.join(folder, "survey"))
            self.assertEqual(rebooted.backup(), state, (kind, turns))
            self.close(rebooted)
            self.assertEqual([n for n in os.listdir(folder) if ".restore" in n], [],
                             (kind, turns))
            return finished, state
        finished, new = attempt(kind + "-reference", 100000)
        self.assertTrue(finished); self.assertNotEqual(new, old)
        self.assertEqual(invariants(new), invariants(source.backup()))
        outcomes = []
        for turns in range(1, 100000):
            finished, state = attempt("%s-%d" % (kind, turns), turns)
            self.assertTrue(state == old or state == new, (kind, turns))
            outcomes.append(state == new)
            if finished: break
        self.assertFalse(outcomes[0]); self.assertTrue(outcomes[-1])
        self.assertEqual(outcomes, sorted(outcomes))

    def service_log(self, kind, source):
        """Run one async restore; return its sleeps, observer turns and writes in order."""
        log, append = [], tracking.Tracker._append
        async def sleep_ms(ms):
            log.append(("sleep", ms)); await asyncio.sleep(0)
        def staged(name, original):
            def wrapper(tracker, *args, **kwargs):
                if tracker.path.endswith(".restore"): log.append(name)
                return original(tracker, *args, **kwargs)
            return wrapper
        def vertex(tracker, event, defer_index=False):
            if tracker.path.endswith(".restore") and event.get("event") == "vertex_added":
                log.append("vertex")
            return append(tracker, event, defer_index)
        upgrade, validate = tracking.upgrade_restore_backup, tracking.validate_backup
        def upgraded(*args, **kwargs):
            log.append("upgrade"); return upgrade(*args, **kwargs)
        def validated(*args, **kwargs):
            log.append("validate"); return validate(*args, **kwargs)
        install, remove = tracking.Tracker._install_file, tracking.Tracker._remove_present
        sync = FlashPointStore.sync
        def synced(store):
            if ".restore." in store.path: log.append("sync")
            return sync(store)
        def installed(tracker, source, target):
            log.append("install"); return install(tracker, source, target)
        def removed(tracker, path):
            if path.endswith(".restore.commit.json"): log.append("install")
            return remove(tracker, path)
        async def run():
            done = []
            async def observe():
                while not done:
                    log.append("turn"); await asyncio.sleep(0)
            observer = asyncio.ensure_future(observe())
            await asyncio.sleep(0)
            log.append("start")
            try:
                await self.async_restore(kind, self.tracker("service-" + kind), source)
                log.append("end")
            finally: done.append(True); await observer
        with mock.patch("uasyncio.sleep_ms", sleep_ms), \
                support.patch_module_global("tracking", "upgrade_restore_backup", upgraded), \
                support.patch_module_global("tracking", "validate_backup", validated), \
                mock.patch.object(tracking.Tracker, "_install_file", installed), \
                mock.patch.object(tracking.Tracker, "_remove_present", removed), \
                mock.patch.object(tracking.Tracker, "_append", vertex), \
                mock.patch.object(FlashPointStore, "sync", synced), \
                mock.patch.object(tracking.Tracker, "_write_checkpoint",
                                  staged("checkpoint", tracking.Tracker._write_checkpoint)), \
                mock.patch.object(tracking.Tracker, "_write_index",
                                  staged("index", tracking.Tracker._write_index)):
            asyncio.run(run())
        return log

    MARKERS = ("upgrade", "validate", "checkpoint", "index", "vertex", "install", "end")

    def assert_window_after(self, log, position, ms, label):
        """Before the next marker the loop sleeps ms and then turns, so others get to run."""
        rest = log[position + 1:]
        marks = [i for i, entry in enumerate(rest) if entry in self.MARKERS]
        window = rest[:marks[0]] if marks else rest
        self.assertIn(("sleep", ms), window, label)
        self.assertIn("turn", window[window.index(("sleep", ms)):], label)

    def test_a_json_restore_keeps_its_service_windows(self):
        source = rich_survey(self.tracker("source"))
        log = self.service_log("json_async", source)
        last = lambda name: max(i for i, entry in enumerate(log) if entry == name)
        self.assertEqual(log.count("end"), 1)
        windows = [("start", log.index("start")), ("upgrade", log.index("upgrade")),
                   ("validate", log.index("validate")), ("checkpoint", log.index("checkpoint")),
                   ("last index", last("index")),
                   # The commit record and the installation are the last writes.
                   ("published restore", last("install"))]
        for label, position in windows:
            self.assert_window_after(log, position, 10, label)
        self.assertLess(last("install"), log.index("end"))
        # The point-store sync and checkpoint are long writes: the last staged
        # event must be followed by a service window before the checkpoint.
        before_checkpoint = log[last("vertex") + 1:log.index("checkpoint")]
        self.assertIn(("sleep", 10), before_checkpoint)
        self.assertIn("turn", before_checkpoint[before_checkpoint.index(("sleep", 10)):])
        run = 0
        for entry in log:
            if entry == "vertex": run += 1; self.assertLessEqual(run, 4)
            elif entry == "turn": run = 0

class NdjsonRejectionTests(RestoreFixture, unittest.TestCase):
    def cases(self, source):
        rows = list(source.iter_backup_ndjson())
        body, integrity = rows[:-1], rows[-1]
        values = [json.loads(raw) for raw in body]
        kinds = [value["record"] for value in values]
        def row(value): return json.dumps(value) + "\n"
        def index(kind, nth=0): return [i for i, k in enumerate(kinds) if k == kind][nth]
        def edit(position, **changes):
            changed = list(body); changed[position] = row(dict(values[position], **changes))
            return with_integrity(changed)
        vertex = index("vertex")
        unknown = json.loads(body[vertex]); unknown["measurement"]["profile_id"] = "profile-0000ffff"
        unknown["measurement"]["quality"].pop("limits", None)
        return [
            ("Duplicate integrity record.", rows + [integrity]),
            ("Records follow integrity record.", rows + [body[1]]),
            ("Survey backup header must come first.", with_integrity(body[1:])),
            ("Duplicate backup header.", with_integrity(body[:1] + body)),
            ("Survey backup header is invalid.", edit(0, schema_version=9)),
            ("Records follow the backup footer.", with_integrity(body + [body[index("asset")]])),
            ("Duplicate project ID.", with_integrity(body[:2] + body[1:])),
            ("Duplicate line ID.", with_integrity(body[:index("line") + 1] + [body[index("line")]] + body[index("line") + 1:])),
            ("Duplicate asset ID.", with_integrity(body[:index("asset") + 1] + [body[index("asset")]] + body[index("asset") + 1:])),
            ("Unknown project.", edit(index("line"), project_id="project-nowhere")),
            ("A vertex precedes its line.", edit(vertex, line_id=values[index("line", 1)]["line"]["id"])),
            ("Vertex belongs to another project.", edit(vertex, project_id="project-nowhere")),
            ("Unknown measurement profile.", with_integrity(body[:vertex] + [row(unknown)] + body[vertex + 1:])),
            ("Survey backup record is invalid.", with_integrity(body[:2] + [row({"record": "bogus"})] + body[2:])),
            ("Survey backup is incomplete.", with_integrity(body[:-1])),
            ("Survey backup integrity check failed.", body[:-1] + [body[-1].replace('"active_line_id": "', '"active_line_id": "x')] + [integrity]),
        ]

    def test_a_broken_ndjson_backup_is_refused_with_its_reason(self):
        source = rich_survey(self.tracker("source"))
        for message, rows in self.cases(source):
            for asynchronous in (False, True):
                with self.subTest(message, asynchronous=asynchronous):
                    target = self.tracker("target-%d" % asynchronous)
                    target.create_project("Kept"); before = target.backup()
                    with self.assertRaises(ValueError) as refused:
                        if asynchronous: asyncio.run(target.restore_ndjson_async(iter(rows)))
                        else: target.restore_ndjson(iter(rows))
                    self.assertEqual(str(refused.exception), message)
                    self.assertEqual(target.backup(), before)
                    self.assertEqual([n for n in os.listdir(self.tmp.name)
                                      if "target-%d.restore." % asynchronous in n], [])
                    self.close(target)
                    for name in os.listdir(self.tmp.name):
                        if name.startswith("target-%d" % asynchronous): os.remove(os.path.join(self.tmp.name, name))

    def test_limits_are_checked_before_anything_is_published(self):
        source = rich_survey(self.tracker("source"))
        rows = list(source.iter_backup_ndjson())
        def refuse(tracker, message):
            tracker.create_project("Kept"); before = tracker.backup()
            with self.assertRaises(ValueError) as refused:
                tracker.restore_ndjson(iter(rows))
            self.assertEqual(str(refused.exception), message)
            self.assertEqual(tracker.backup(), before)
            folder = os.path.dirname(tracker.path)
            self.assertEqual([n for n in os.listdir(folder) if ".restore" in n], [])
            self.assertFalse(os.path.exists(tracker.restore_commit_path))
        refuse(self.tracker("small", max_features=5), "Survey backup exceeds active limit.")
        with support.patch_module_global("tracking", "MAX_LINES_PER_PROJECT", 1), \
                support.patch_module_global("tracking", "MAX_PROJECTS", 1):
            refuse(self.tracker("tiny", max_features=1),
                   "Survey backup contains too many records.")


class NdjsonEntryTests(RestoreFixture, unittest.TestCase):
    # The scheduling probe of the JSON restore serves the NDJSON restore too.
    service_log = RestoreRoundTripTests.service_log
    assert_window_after = RestoreRoundTripTests.assert_window_after
    async_restore = staticmethod(RestoreRoundTripTests.async_restore)
    MARKERS = RestoreRoundTripTests.MARKERS

    def test_an_ndjson_restore_keeps_the_backup_content(self):
        source = rich_survey(self.tracker("source"), paused=True)
        self.assertEqual(source.projects[1]["lines"][0]["pause_reason"], "capacity_limit")
        for kind in ("ndjson", "ndjson_async"):
            with self.subTest(kind):
                target = self.tracker(kind); restore(kind, target, source)
                self.assertEqual(without_order(target.backup()), without_order(source.backup()))

    def test_untrusted_profile_ids_in_an_ndjson_header(self):
        source = rich_survey(self.tracker("source"))
        rows = list(source.iter_backup_ndjson())
        header = json.loads(rows[0]); canonical = header["profiles"][0]["id"]
        def foreign(text): return with_integrity(
            text.replace(canonical, "profile-foreign").splitlines(keepends=True))
        extra = {"id": "profile-foreign", "version": 1, "limits": {"max_hdop": 9.0}}
        unreferenced = with_integrity(
            [json.dumps(dict(header, profiles=header["profiles"] + [extra])) + "\n"] + rows[1:])
        target = self.tracker("unreferenced"); target.restore_ndjson(iter(unreferenced))
        self.assertEqual(sorted(target.profiles), [canonical])
        # A reference to an untrusted ID is derived anew from its inline limits.
        target = self.tracker("referenced"); target.restore_ndjson(iter(foreign("".join(rows))))
        self.assertEqual(sorted(target.profiles), [canonical])
        self.assertEqual(set(measurement["profile_id"]
                             for line in target.backup()["projects"][0]["lines"]
                             for measurement in line["measurements"]), {canonical})
        # Compact archive rows carry no limits: such a reference stays unknown.
        compact = "".join(source.iter_compact_archive(source.projects[0]["id"]))
        target = self.tracker("compact"); before = target.backup()
        with self.assertRaises(ValueError) as refused:
            target.restore_ndjson(iter(foreign(compact)))
        self.assertEqual(str(refused.exception), "Unknown measurement profile.")
        self.assertEqual(target.backup(), before)
        # The JSON entries follow the same rule: a measurement that refers to an
        # untrusted header ID but carries no limits is refused with the same text.
        payload = json.loads(json.dumps(source.backup()).replace(canonical, "profile-foreign"))
        for project in payload["projects"]:
            for measurement in ([m for line in project["lines"] for m in line["measurements"]] +
                                [point["measurement"] for point in project["points"]]):
                measurement["quality"].pop("limits")
        for kind in ("json", "json_async"):
            with self.subTest(kind):
                target = self.tracker("no-limits-" + kind); before = target.backup()
                with self.assertRaises(ValueError) as refused:
                    if kind == "json": target.restore(json.loads(json.dumps(payload)))
                    else: asyncio.run(target.restore_async(json.loads(json.dumps(payload))))
                self.assertEqual(str(refused.exception), "Unknown measurement profile.")
                self.assertEqual(target.backup(), before)

    def test_untrusted_profile_ids_on_assets(self):
        source = self.tracker("source"); source.create_project("Assets only")
        source.add_asset("tree", "T1", "", fix(1, 1), link_to_active_line=False)
        rows = list(source.iter_backup_ndjson())
        canonical = json.loads(rows[0])["profiles"][0]["id"]
        def foreign(rows): return with_integrity(
            "".join(rows).replace(canonical, "profile-foreign").splitlines(keepends=True))
        # Like a vertex: an asset's reference is derived anew from its inline limits.
        target = self.tracker("full")
        self.assertEqual(target.restore_ndjson(iter(foreign(rows))),
                         {"schema_version": 5, "projects": 1, "features": 1,
                          "legacy_values_skipped": 0})
        self.assertEqual(sorted(target.profiles), [canonical])
        self.assertEqual(target.backup()["projects"][0]["points"][0]["measurement"]["profile_id"],
                         canonical)
        # Without limits it is refused at its row, with the vertex's reason.
        compact = foreign(source.iter_compact_archive(source.projects[0]["id"]))
        target = self.tracker("compact"); before = target.backup()
        appended, append = [], tracking.Tracker._append
        def observe(tracker, event, defer_index=False):
            appended.append(event["event"]); return append(tracker, event, defer_index)
        with mock.patch.object(tracking.Tracker, "_append", observe), \
                self.assertRaises(ValueError) as refused:
            target.restore_ndjson(iter(compact))
        self.assertEqual(str(refused.exception), "Unknown measurement profile.")
        self.assertNotIn("asset_added", appended)
        self.assertEqual(target.backup(), before)
        self.assertEqual([n for n in os.listdir(self.tmp.name) if "compact.restore" in n], [])

    def test_an_ndjson_restore_pauses_after_each_long_write(self):
        source = rich_survey(self.tracker("source"))
        log = self.service_log("ndjson_async", source)
        self.assertEqual(log.count("end"), 1)
        last_index = max(i for i, entry in enumerate(log) if entry == "index")
        checkpoints = [i for i, entry in enumerate(log) if entry == "checkpoint"]
        self.assertTrue(checkpoints)
        for position in checkpoints: self.assert_window_after(log, position, 10, "checkpoint")
        self.assert_window_after(log, last_index, 10, "last index")

    def test_every_restore_pauses_after_the_point_store_sync(self):
        source = rich_survey(self.tracker("source"))
        for kind in ("json_async", "ndjson_async"):
            with self.subTest(kind):
                log = self.service_log(kind, source)
                synced = [i for i, entry in enumerate(log[:log.index("checkpoint")])
                          if entry == "sync"]
                self.assertTrue(synced)
                self.assert_window_after(log, synced[-1], 10, "point-store sync")

    def test_a_reference_outside_the_header_is_refused_despite_its_limits(self):
        source = rich_survey(self.tracker("source"))
        rows = list(source.iter_backup_ndjson())
        for record in ("vertex", "asset"):
            with self.subTest(record):
                position = [json.loads(raw)["record"] for raw in rows].index(record)
                value = json.loads(rows[position])
                measurement = (value["measurement"] if record == "vertex"
                               else value["point"]["measurement"])
                self.assertTrue(measurement["quality"]["limits"])
                measurement["profile_id"] = "profile-elsewhere"
                changed = with_integrity(rows[:position] + [json.dumps(value) + "\n"] +
                                         rows[position + 1:])
                target = self.tracker("target-" + record); before = target.backup()
                with self.assertRaises(ValueError) as refused:
                    target.restore_ndjson(iter(changed))
                self.assertEqual(str(refused.exception), "Unknown measurement profile.")
                self.assertEqual(target.backup(), before)

    def test_events_for_a_backup_are_produced_lazily(self):
        source = rich_survey(self.tracker("source"))
        events = self.tracker("staging")._events_for_backup(source.backup())
        self.assertIsInstance(events, types.GeneratorType)
        events.close()

    def test_restore_functions_stay_short(self):
        tree = tracking_tree()
        named = ("_payload_records", "_events_for_backup", "_ndjson_records",
                 "_check_integrity", "_finish_backup_events", "_line_end_event")
        long = [(node.name, node.end_lineno - node.lineno + 1) for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and
                (node.name.startswith(("restore", "_restore")) or node.name in named) and
                node.end_lineno - node.lineno + 1 > 80]
        self.assertEqual(long, [])


class OneEntryTests(RestoreFixture, unittest.TestCase):
    def test_every_entry_restores_the_same_backup(self):
        source = rich_survey(self.tracker("source"), paused=True)
        restored = {}
        for kind in ENTRIES:
            with self.subTest(kind):
                target = self.tracker(kind); restore(kind, target, source)
                restored[kind] = json.dumps(target.backup())
                self.assertEqual(without_order(target.backup()), without_order(source.backup()))
        self.assertEqual(len(set(restored.values())), 1)

    def test_a_json_restore_keeps_an_empty_selection(self):
        payload = rich_survey(self.tracker("source")).backup()
        payload["active_project_id"] = payload["active_line_id"] = None
        for kind in ("json", "json_async"):
            with self.subTest(kind):
                target = self.tracker(kind)
                if kind == "json": target.restore(json.loads(json.dumps(payload)))
                else: asyncio.run(target.restore_async(json.loads(json.dumps(payload))))
                backup = target.backup()
                self.assertEqual((backup["active_project_id"], backup["active_line_id"]),
                                 (None, None))
                self.assertEqual(without_order(backup), without_order(payload))

    def test_a_json_restore_does_not_copy_the_measurements(self):
        payload = rich_survey(self.tracker("source")).backup()
        supplied = [measurement for project in payload["projects"]
                    for line in project["lines"] for measurement in line["measurements"]]
        supplied_points = [point for project in payload["projects"]
                           for point in project["points"]]
        seen, seen_points, append = [], [], tracking.Tracker._append
        def observe(tracker, event, defer_index=False):
            if event.get("event") == "vertex_added": seen.append(event["measurement"])
            if event.get("event") == "asset_added": seen_points.append(event["point"])
            return append(tracker, event, defer_index)
        with mock.patch.object(tracking.Tracker, "_append", observe):
            asyncio.run(self.tracker("target").restore_async(payload))
        self.assertEqual(len(seen), len(supplied))
        self.assertTrue(all(a is b for a, b in zip(seen, supplied)))
        self.assertTrue(supplied_points)
        self.assertEqual(len(seen_points), len(supplied_points))
        self.assertTrue(all(a is b for a, b in zip(seen_points, supplied_points)))

    def test_a_restore_steps_after_each_event_but_vertices_by_four(self):
        source = rich_survey(self.tracker("source"), paused=True)
        payload, rows = source.backup(), list(source.iter_backup_ndjson())
        starts = (("json", lambda target: target._restore_payload_steps(payload, True)),
                  ("ndjson", lambda target: target._restore_ndjson_steps(iter(rows))))
        for kind, start in starts:
            with self.subTest(kind):
                log, append = [], tracking.Tracker._append
                def observe(tracker, event, defer_index=False):
                    if tracker.path.endswith(".restore"): log.append(event["event"])
                    return append(tracker, event, defer_index)
                with mock.patch.object(tracking.Tracker, "_append", observe):
                    for _ in start(self.tracker(kind)): log.append("step")
                vertices = 0
                self.assertEqual(log.count("vertex_added"), 11)
                # The pairs below say nothing about the last entry: it must not
                # be a vertex, or its step (after every fourth) would go unchecked.
                self.assertNotEqual([entry for entry in log if entry != "step"][-1],
                                    "vertex_added")
                for entry, following in zip(log, log[1:]):
                    if entry == "step": continue
                    vertices += entry == "vertex_added"
                    self.assertEqual(following == "step",
                                     entry != "vertex_added" or vertices % 4 == 0,
                                     (entry, vertices))

    def test_one_restore_entry(self):
        tree = tracking_tree()
        callers, defined = {}, set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defined.add(node.name)
                for call in ast.walk(node):
                    if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute):
                        callers.setdefault(call.func.attr, set()).add(node.name)
        self.assertEqual(callers["_fresh_restore_store"], {"_restore_steps"})
        self.assertEqual(callers["_commit_staged_restore_steps"], {"_restore_steps"})
        self.assertEqual(callers["_activate_staged_restore_steps"],
                         {"_commit_staged_restore_steps", "_commit_staged_restore"})
        self.assertEqual(callers["_restore_steps"],
                         {"_restore_payload_steps", "_restore_ndjson_steps"})
        self.assertEqual(callers.get("_restore_payload_steps"), {"restore", "restore_async"})
        self.assertEqual(callers["_restore_ndjson_steps"],
                         {"restore_ndjson", "restore_ndjson_async", "_reactivate_archive_steps"})
        self.assertFalse({"_replace_with_events", "_strip_payload_derived_fields",
                          "_payload_events"} & defined)


class RestoreCloseTests(RestoreFixture, unittest.TestCase):
    """Closing a restore closes every generator of its chain explicitly.

    MicroPython has no reference counting: a generator that is dropped
    without close() keeps its finally blocks pending. Every generator is held
    here, so CPython cannot close it either.
    """
    CHAIN = ((tracking.Tracker, ("_restore_payload_steps", "_restore_steps", "_ndjson_records",
                                 "_events_for_backup", "_commit_staged_restore_steps",
                                 "_activate_staged_restore_steps", "_restore_roll_forward_steps")),
             (FlashPointStore, ("_scan_generation_steps",)),
             (tracking.validation, ("_payload_records",)))

    def setUp(self):
        super().setUp()
        self.held = []
        for owner, names in self.CHAIN:
            for name in names:
                held = self.holding(getattr(owner, name))
                patch = (support.patch_module_global("tracking", name, held)
                         if owner is tracking.validation
                         else mock.patch.object(owner, name, held))
                patch.start(); self.addCleanup(patch.stop)

    def holding(self, function):
        def wrapper(*args, **kwargs):
            generator = function(*args, **kwargs)
            self.held.append((function.__name__, generator))
            return generator
        return wrapper

    def entries(self, source):
        """Per format a start(target, closed) that returns the outer restore generator."""
        payload, rows = source.backup(), list(source.iter_backup_ndjson())
        def lines(closed):
            try:
                for raw in rows: yield raw
            finally: closed.append(True)
        def json_steps(target, closed):
            return target._restore_payload_steps(json.loads(json.dumps(payload)), False)
        def ndjson_steps(target, closed):
            self.held.append(("lines", lines(closed)))
            return target._restore_ndjson_steps(self.held[-1][1])
        return (("json", json_steps), ("ndjson", ndjson_steps))

    def test_closing_a_restore_at_any_step_closes_its_whole_chain(self):
        source = rich_survey(self.tracker("source"), paused=True)
        for kind, start in self.entries(source):
            total = sum(1 for _ in start(self.tracker("count-" + kind), []))
            names = set()
            for steps in range(1, total + 1):
                with self.subTest(kind, steps=steps):
                    folder = os.path.join(self.tmp.name, "%s-%d" % (kind, steps))
                    os.mkdir(folder); del self.held[:]
                    target = self.tracker(os.path.join(folder, "survey")); closed = []
                    restore = start(target, closed)
                    for _ in range(steps): next(restore)
                    restore.close()
                    names.update(name for name, _generator in self.held)
                    self.assertEqual([name for name, generator in self.held
                                      if generator.gi_frame is not None], [])
                    self.assertEqual(closed, [] if kind == "json" else [True])
                    self.assertEqual([n for n in os.listdir(folder) if ".restore" in n], [])
            # Every generator of the chain was reached, so each close() is checked.
            expected = set(name for _owner, chain in self.CHAIN for name in chain)
            if kind == "ndjson": expected ^= {"_restore_payload_steps", "_payload_records", "lines"}
            else: expected -= {"_ndjson_records"}
            self.assertEqual(names, expected, kind)


class RestoreRecoveryTests(RestoreFixture, unittest.IsolatedAsyncioTestCase):
    async def survey(self):
        self.path = os.path.join(self.tmp.name, "survey")
        live = tracking.Tracker(self.path)
        live.create_project("Old")
        live.start_line("Old line", .2)
        for index in range(6): live.add_vertex(fix(index, 8))
        live.set_line_state("finish")
        await live.compact_async()
        self.source = rich_survey(self.tracker("source"))
        return live

    async def published(self):
        """A restore whose record is in place; flash refused its installation."""
        live = await self.survey()
        with mock.patch.object(tracking.Tracker, "_install_file",
                               side_effect=OSError("flash broken")):
            with self.assertRaisesRegex(OSError, "flash broken"):
                live.restore_ndjson(iter(list(self.source.iter_backup_ndjson())))
        self.assertTrue(os.path.exists(live.restore_commit_path))
        return live

    def plant_orphan(self):
        path = "%s.points-v1.jsonl.g%08x-0001.pjs" % (self.path, 0x00ffff00)
        with open(path, "w") as target: target.write("orphan\n")
        return path

    async def boot_through_main(self):
        from support import main, state as app_state
        tracker = tracking.Tracker(self.path, autoload=False, point_store_autoload=False)
        self.addCleanup(self.close, tracker)
        app_state.app.stats.pop("storage_recovery", None)
        stop = asyncio.Event(); stop.set()
        with mock.patch.object(main, "tracker", tracker), \
                mock.patch.object(main, "prepare_parallel_benchmark", return_value=(None, None)), \
                mock.patch.object(main, "shutdown_event", stop), \
                mock.patch.object(tracker, "run_worker", mock.AsyncMock()), \
                mock.patch.object(main, "log"), support.patch_module_global("tracking", "log") as log:
            await main.tracking_startup_task()
        return tracker, dict(app_state.app.stats), log

    async def test_a_published_restore_is_completed_and_reported_at_boot(self):
        self.close(await self.published())
        tracker, stats, log = await self.boot_through_main()
        self.assertEqual(stats["tracking_startup"]["state"], "ready")
        self.assertEqual(stats["storage_recovery"]["restore"], "completed")
        self.assertIn("An interrupted survey restore was completed.", str(log.call_args_list))
        self.assertEqual(invariants(tracker.backup()), invariants(self.source.backup()))
        self.assertFalse(os.path.exists(tracker.restore_commit_path))
        self.close(tracker)
        again, stats, _log = await self.boot_through_main()
        self.assertIsNone(stats["storage_recovery"]["restore"])
        self.assertEqual(invariants(again.backup()), invariants(self.source.backup()))

    async def test_an_unpublished_staging_is_discarded_and_reported_at_boot(self):
        live = await self.survey(); old = live.backup()
        staging = live._fresh_restore_store()
        for event in staging._events_for_backup(self.source.backup()):
            staging._append(event, defer_index=True)
        staging._write_checkpoint(); staging._write_index()
        self.close(staging); self.close(live)
        tracker, stats, log = await self.boot_through_main()
        self.assertEqual(stats["storage_recovery"]["restore"], "discarded")
        self.assertIn("An unfinished survey restore was discarded", str(log.call_args_list))
        self.assertEqual(tracker.backup(), old)
        self.assertEqual([n for n in os.listdir(self.tmp.name) if "survey.restore." in n], [])

    async def test_the_boot_sweep_runs_against_the_restored_point_store(self):
        self.close(await self.published())
        image = os.path.join(self.tmp.name, "image"); os.mkdir(image)
        for name in os.listdir(self.tmp.name):
            if name.startswith("survey."): os.replace(os.path.join(self.tmp.name, name),
                                                     os.path.join(image, name))
        boots = {"autoload": lambda t: None,
                 "store_autoloaded": lambda t: t.initialize(),
                 "store_autoloaded_async": lambda t: t.initialize_async(),
                 "production_async": lambda t: t.initialize_async()}
        options = {"autoload": {}, "store_autoloaded": {"autoload": False},
                   "store_autoloaded_async": {"autoload": False},
                   "production_async": {"autoload": False, "point_store_autoload": False}}
        for name, boot in boots.items():
            with self.subTest(name):
                folder = os.path.join(self.tmp.name, name)
                shutil.copytree(image, folder)
                self.path = os.path.join(folder, "survey")
                orphan = self.plant_orphan()
                tracker = tracking.Tracker(self.path, **options[name])
                self.addCleanup(self.close, tracker)
                pending = boot(tracker)
                if pending is not None and asyncio.iscoroutine(pending): await pending
                self.assertEqual(invariants(tracker.backup()), invariants(self.source.backup()))
                self.assertEqual(tracker.storage_recovery["restore"], "completed")
                self.assertEqual(tracker.storage_recovery["unreferenced_point_files"], 1)
                self.assertFalse(os.path.exists(orphan))
                for path in tracker.point_store.sidecar_paths():
                    self.assertTrue(os.path.exists(path), path)

    async def test_boot_recovers_then_loads_then_sweeps(self):
        self.close(await self.survey())
        for asynchronous in (False, True):
            calls = []
            def spy(owner, name, label):
                original = getattr(owner, name)
                def wrapper(instance, *args, **kwargs):
                    calls.append(label)
                    return original(instance, *args, **kwargs)
                return mock.patch.object(owner, name, wrapper)
            patches = [spy(tracking.Tracker, "_recover_restore_steps", "recover"),
                       spy(FlashPointStore, "load", "load"),
                       spy(FlashPointStore, "load_async", "load"),
                       spy(tracking.Tracker, "_load_steps", "replay"),
                       spy(FlashPointStore, "remove_unreferenced_steps", "sweep"),
                       spy(tracking.Tracker, "_load_archives", "archives")]
            tracker = tracking.Tracker(self.path, autoload=False, point_store_autoload=False)
            self.addCleanup(self.close, tracker)
            for patch in patches: patch.start()
            try:
                if asynchronous: await tracker.initialize_async()
                else: tracker.initialize()
            finally:
                for patch in patches: patch.stop()
            with self.subTest(asynchronous=asynchronous):
                self.assertEqual(calls,
                                 ["recover", "load", "replay", "sweep", "archives"])
                self.assertTrue(tracker.is_loaded())

    async def test_a_restore_keeps_the_files_a_recovery_protects(self):
        live = await self.survey(); generation = live.point_store.generation; self.close(live)
        manifest = self.path + ".points-v1.jsonl.segments.json"
        with open(manifest) as source: original = source.read()
        with open(manifest + ".previous", "w") as target: target.write(original)
        with open(manifest, "w") as target: target.write("{")
        newer = "%s.points-v1.jsonl.g%08x-0001.pjs" % (self.path, generation + 1)
        with open(newer, "w") as target: target.write("protected\n")
        live = tracking.Tracker(self.path); self.addCleanup(self.close, live)
        self.assertEqual(live.point_store.quarantine()["files"], 1)
        live.restore_ndjson(iter(list(self.source.iter_backup_ndjson())))
        self.assertNotEqual(live.point_store.generation, generation + 1)
        self.assertEqual(live.point_store.quarantine()["files"], 1)
        with open(newer) as source: self.assertEqual(source.read(), "protected\n")
        self.close(live)
        tracker, stats, _log = await self.boot_through_main()
        self.assertEqual(invariants(tracker.backup()), invariants(self.source.backup()))
        self.assertEqual(stats["storage_recovery"]["quarantined_point_files"], 1)
        self.assertEqual(stats["storage_recovery"]["unreferenced_point_files"], 0)
        with open(newer) as source: self.assertEqual(source.read(), "protected\n")

    async def test_an_install_refused_in_session_stays_visible_until_boot(self):
        from support import state as app_state
        live = await self.published()
        self.assertTrue(live.is_install_pending())
        self.assertFalse(live.is_loaded())
        self.assertEqual(live.status()["tracking_startup"]["code"], "restore_install_pending")
        with self.assertRaises(tracking.StorageBusy) as refused:
            async with live.maintenance("tracking_restore"): pass
        self.assertEqual(refused.exception.code, "restore_install_pending")
        from test_http import FakeWriter
        writer = FakeWriter()
        with mock.patch.object(web, "tracker", live), \
                mock.patch.object(web, "_admin_allowed", return_value=True):
            await web._handle_tracking_request(writer, "/api/tracking", "POST", "",
                b'{"action":"restore","backup":{}}', False, {})
        # #19a E8: an action answers like the import, only a restart clears the state.
        self.assertIn(b"409 Conflict", writer.buf)
        self.assertIn(b"restore_install_pending", writer.buf)


if __name__ == "__main__":
    unittest.main()
