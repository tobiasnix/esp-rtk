# SPDX-License-Identifier: AGPL-3.0-only
"""Unreferenced point-store segment files are swept at boot, and only then."""
import asyncio
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import support
from support import state  # noqa: F401 - MicroPython compatibility
import tracking
from pointstore import FlashPointStore
from test_tracking import FIX

ORPHAN = json.dumps([3, 1, "line-ghost", [800000000, 5000000000, 1000], [-1]]) + "\n"


class StoreFixture:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="esp-rtk-orphans-")
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "points.jsonl")
        store = FlashPointStore(self.path)
        with mock.patch("pointstore.POINT_SEGMENT_BYTES", 240):
            for order in range(1, 9):
                store.append(order, "line-a", [800000000 + order, 5000000000, 1000],
                             {"record_order": order, "precision": "e8"})
        store.close()
        self.generation, self.segments = store.generation, list(store.segments)
        self.live = sorted(store.sidecar_paths())
        self.assertGreater(len(self.live), 1)

    def write(self, name, text=ORPHAN):
        path = os.path.join(self.tmp.name, name)
        with open(path, "w", encoding="utf-8") as target: target.write(text)
        return path

    def orphan(self, generation, number):
        return self.write("points.jsonl.g%08x-%04d.pjs" % (generation, number))

    def files(self):
        return sorted(os.listdir(self.tmp.name))


class PointStoreSweepTests(StoreFixture, unittest.TestCase):

    def test_boot_sweep_removes_only_unnamed_segments_of_this_store(self):
        orphans = [self.orphan(self.generation - 2, number) for number in (2, 3, 9)]
        orphans.append(self.orphan(self.generation, 40))
        kept = [self.write("other.jsonl.g%08x-0001.pjs" % (self.generation - 2)),
                self.write("points.jsonl.restore.points-v1.jsonl.g00000005-0001.pjs"),
                self.write("points.jsonl.gnothex1-0001.pjs"),
                self.write("points.jsonl.g%08x-0002.pjs.bak" % (self.generation - 2))]
        store = FlashPointStore(self.path)
        try:
            self.assertEqual(store.remove_unreferenced(), (4, 4 * len(ORPHAN)))
            self.assertEqual(sum(len(v) for v in store.offsets.values()), 8)
        finally: store.close()
        for path in orphans: self.assertFalse(os.path.exists(path), path)
        for path in kept + self.live: self.assertTrue(os.path.exists(path), path)
        reloaded = FlashPointStore(self.path)
        try:
            self.assertEqual(len(reloaded.offsets["line-a"]), 8)
            self.assertEqual(reloaded.remove_unreferenced(), (0, 0))
        finally: reloaded.close()

    def test_a_relative_dot_path_keeps_the_active_segments(self):
        orphan = self.orphan(self.generation - 2, 2)
        cwd = os.getcwd()
        os.chdir(self.tmp.name); self.addCleanup(os.chdir, cwd)
        store = FlashPointStore("./points.jsonl")
        try: self.assertEqual(store.remove_unreferenced(), (1, len(ORPHAN)))
        finally: store.close()
        for path in self.live: self.assertTrue(os.path.exists(path), path)
        self.assertFalse(os.path.exists(orphan))

    def test_segments_of_the_previous_manifest_are_kept(self):
        previous = [self.orphan(self.generation - 1, number) for number in (1, 2)]
        stray = self.orphan(self.generation - 1, 3)
        with open(self.path + ".segments.json.previous", "w", encoding="utf-8") as target:
            json.dump({"kind": "point_store", "schema": 4, "generation": self.generation - 1,
                       "segments": [1, 2], "strings": []}, target)
        store = FlashPointStore(self.path)
        try: self.assertEqual(store.remove_unreferenced(), (1, len(ORPHAN)))
        finally: store.close()
        for path in previous + self.live: self.assertTrue(os.path.exists(path), path)
        self.assertFalse(os.path.exists(stray))

    def test_missing_invalid_or_recovered_manifest_deletes_nothing(self):
        orphan = self.orphan(self.generation - 2, 2)
        manifest = self.path + ".segments.json"
        with open(manifest, encoding="utf-8") as source: original = source.read()
        variants = {
            "missing": None,
            "invalid json": "{",
            "foreign kind": json.dumps({"kind": "other", "generation": 1, "segments": []}),
            "names a missing segment": json.dumps({"kind": "point_store", "schema": 4,
                "generation": self.generation, "segments": self.segments + [99],
                "strings": []}),
            # The main manifest is damaged and .previous is older: its files may
            # be the only copy of newer data, so nothing is deleted.
            "recovered from previous": "{",
        }
        for label, text in variants.items():
            with self.subTest(label):
                if label == "recovered from previous":
                    with open(manifest + ".previous", "w", encoding="utf-8") as target:
                        target.write(original)
                before = self.files()
                if text is None: os.remove(manifest)
                else:
                    with open(manifest, "w", encoding="utf-8") as target: target.write(text)
                store = FlashPointStore(self.path)
                try: self.assertEqual(store.remove_unreferenced(), (0, 0))
                finally: store.close()
                self.assertTrue(os.path.exists(orphan))
                for path in self.live: self.assertTrue(os.path.exists(path), path)
                if label != "recovered from previous":
                    self.assertEqual(sorted(set(before) - {"points.jsonl.segments.json"}),
                                     sorted(set(self.files()) - {"points.jsonl.segments.json"}))
                with open(manifest, "w", encoding="utf-8") as target: target.write(original)

    def test_only_an_explicit_call_after_a_recovering_load_sweeps(self):
        orphan = self.orphan(self.generation - 2, 2)
        # Constructing or loading a store (compaction probes, export readers,
        # restore verification) must never delete: a writer of the same prefix
        # creates a segment before its manifest names it.
        FlashPointStore(self.path).close()
        unloaded = FlashPointStore(self.path, autoload=False)
        self.assertEqual(unloaded.remove_unreferenced(), (0, 0))
        probe = FlashPointStore(self.path, autoload=False)
        self.assertTrue(probe._load_manifest(recover=False))
        self.assertEqual(probe.remove_unreferenced(), (0, 0))
        live = FlashPointStore(self.path)
        verifier = FlashPointStore(self.path, autoload=False)
        try:
            asyncio.run(verifier.load_async(verified_store=live))
            self.assertEqual(verifier.remove_unreferenced(), (0, 0))
            self.assertTrue(os.path.exists(orphan))
            booted = FlashPointStore(self.path, autoload=False)
            asyncio.run(booted.load_async())
            self.assertEqual(booted.remove_unreferenced(), (1, len(ORPHAN)))
        finally: live.close(); verifier.close()
        self.assertFalse(os.path.exists(orphan))

    def test_a_stale_manifest_tmp_goes_with_the_empty_segment_it_names(self):
        # Reset between writing .tmp and the switch: the main manifest still
        # rules, and the .tmp would later be a fallback into a state that
        # was never active.
        empty = self.write("points.jsonl.g%08x-%04d.pjs" % (self.generation,
                                                          max(self.segments) + 1), "")
        with open(self.path + ".segments.json.tmp", "w", encoding="utf-8") as target:
            json.dump({"kind": "point_store", "schema": 4, "generation": self.generation,
                       "segments": self.segments + [max(self.segments) + 1],
                       "strings": []}, target)
        size = os.stat(self.path + ".segments.json.tmp").st_size
        store = FlashPointStore(self.path)
        try:
            self.assertEqual(len(store.offsets["line-a"]), 8)
            self.assertEqual(store.remove_unreferenced(), (2, size))
        finally: store.close()
        self.assertFalse(os.path.exists(empty))
        self.assertFalse(os.path.exists(self.path + ".segments.json.tmp"))
        for path in self.live: self.assertTrue(os.path.exists(path), path)


class PointStoreQuarantineTests(StoreFixture, unittest.TestCase):
    """Files a fallback recovery leaves unnamed survive every later boot."""
    def setUp(self):
        super().setUp()
        self.manifest = self.path + ".segments.json"
        with open(self.manifest, encoding="utf-8") as source: self.original = source.read()
        # The damaged main manifest named a newer generation; its files may
        # hold the only copy of points recorded after the older manifest.
        self.newer = self.orphan(self.generation + 1, 1)

    def damage_main(self, fallback=".previous"):
        with open(self.manifest + fallback, "w", encoding="utf-8") as target:
            target.write(self.original)
        with open(self.manifest, "w", encoding="utf-8") as target: target.write("{")

    def boot(self):
        store = FlashPointStore(self.path, autoload=False)
        asyncio.run(store.load_async())
        try:
            self.assertEqual(len(store.offsets["line-a"]), 8)
            return store.remove_unreferenced(), store.quarantine()
        finally: store.close()

    def test_recovery_protects_unnamed_files_on_every_later_boot(self):
        for fallback in (".previous", ".tmp"):
            with self.subTest(fallback):
                self.damage_main(fallback)
                self.assertEqual(self.boot(), ((0, 0), {"files": 1, "bytes": len(ORPHAN)}))
                # A file orphaned after the recovery is no evidence and goes.
                later = self.orphan(self.generation - 2, 7)
                for _boot in range(3):
                    swept, quarantine = self.boot()
                    self.assertEqual(quarantine, {"files": 1, "bytes": len(ORPHAN)})
                    self.assertTrue(os.path.exists(self.newer))
                    for path in self.live: self.assertTrue(os.path.exists(path), path)
                self.assertFalse(os.path.exists(later))
                with open(self.newer, encoding="utf-8") as source:
                    self.assertEqual(source.read(), ORPHAN)
                FlashPointStore(self.path, autoload=False).release_quarantine()

    def test_only_an_explicit_release_lets_the_sweep_remove_them(self):
        self.damage_main()
        self.boot(); self.boot()
        self.assertTrue(os.path.exists(self.newer))
        FlashPointStore(self.path, autoload=False).release_quarantine()
        self.assertEqual(self.boot(), ((1, len(ORPHAN)), None))
        self.assertFalse(os.path.exists(self.newer))

    def test_quarantine_ends_by_itself_once_its_files_are_gone(self):
        self.damage_main()
        self.boot()
        os.remove(self.newer)  # copied off and deleted by hand
        self.assertEqual(self.boot(), ((0, 0), None))
        self.assertFalse(os.path.exists(self.path + ".recovered.json"))

    def test_recording_after_recovery_never_reuses_a_protected_segment(self):
        # The damaged manifest had just added the last segment of the same
        # generation; .previous does not name it.
        with open(self.manifest + ".previous", "w", encoding="utf-8") as target:
            json.dump({"kind": "point_store", "schema": 4, "generation": self.generation,
                       "segments": self.segments[:-1], "strings": []}, target)
        with open(self.manifest, "w", encoding="utf-8") as target: target.write("{")
        protected = self.live[-1]
        with open(protected, encoding="utf-8") as source: evidence = source.read()
        store = FlashPointStore(self.path)
        try:
            with mock.patch("pointstore.POINT_SEGMENT_BYTES", 240):
                for order in range(100, 106):
                    store.append(order, "line-b", [800000000 + order, 5000000000, 1000],
                                 {"record_order": order, "precision": "e8"})
            self.assertNotIn(self.segments[-1], store.segments)
        finally: store.close()
        with open(protected, encoding="utf-8") as source: self.assertEqual(source.read(), evidence)
        reloaded = FlashPointStore(self.path)
        try:
            self.assertEqual(len(reloaded.offsets["line-b"]), 6)
            reloaded.remove_unreferenced()
        finally: reloaded.close()
        self.assertTrue(os.path.exists(protected))

    def test_a_marker_that_fails_to_open_is_not_taken_for_a_missing_one(self):
        self.damage_main()
        self.boot()
        import errno
        real_open = open
        def failing(path, mode="r", *args, **kwargs):
            if str(path).endswith(".recovered.json"): raise OSError(errno.EIO, "read error")
            return real_open(path, mode, *args, **kwargs)
        real_stat = os.stat
        def stat(path, *args):
            if str(path).endswith(".recovered.json"): raise OSError(errno.EIO, "read error")
            return real_stat(path, *args)
        stray = self.orphan(self.generation - 2, 7)
        store = FlashPointStore(self.path, autoload=False)
        asyncio.run(store.load_async())
        try:
            with mock.patch("pointstore.open", failing, create=True), \
                    mock.patch("pointstore.os.stat", stat):
                self.assertEqual(store.remove_unreferenced(), (0, 0))
                self.assertNotEqual(store._next_generation(self.generation), self.generation + 1)
        finally: store.close()
        for path in [self.newer, stray]: self.assertTrue(os.path.exists(path), path)

    def test_an_unreadable_marker_deletes_nothing(self):
        self.damage_main()
        self.boot()
        with open(self.path + ".recovered.json", "w", encoding="utf-8") as target:
            target.write('{"files": [')
        stray = self.orphan(self.generation - 2, 7)
        swept, quarantine = self.boot()
        self.assertEqual(swept, (0, 0))
        self.assertEqual(quarantine, {"files": 2, "bytes": 2 * len(ORPHAN)})
        for path in [self.newer, stray] + self.live: self.assertTrue(os.path.exists(path), path)

    def test_a_stale_tmp_stays_while_files_are_protected(self):
        self.damage_main()
        self.boot()
        tmp = self.manifest + ".tmp"
        with open(tmp, "w", encoding="utf-8") as target: target.write(self.original)
        self.boot()
        self.assertTrue(os.path.exists(tmp))

    def test_a_failed_marker_write_keeps_the_damaged_manifest_and_every_file(self):
        self.damage_main()
        real_open = open
        def refuse(path, mode="r", *args, **kwargs):
            if str(path).endswith(".recovered.json") and "w" in mode:
                raise OSError(28, "No space left on device")
            return real_open(path, mode, *args, **kwargs)
        with mock.patch("builtins.open", refuse):
            with self.assertRaises(OSError):
                FlashPointStore(self.path)
        with open(self.manifest, encoding="utf-8") as source: self.assertEqual(source.read(), "{")
        self.assertTrue(os.path.exists(self.manifest + ".previous"))
        self.assertEqual(self.boot(), ((0, 0), {"files": 1, "bytes": len(ORPHAN)}))
        self.assertTrue(os.path.exists(self.newer))

    def test_a_reset_at_any_step_of_the_recovering_boot_keeps_the_newer_files(self):
        from powercut import Injector, PowerCut
        self.damage_main()
        base = tempfile.mkdtemp(prefix="esp-rtk-quarantine-")
        self.addCleanup(shutil.rmtree, base, True)
        def stage(name):
            folder = os.path.join(base, name)
            if os.path.exists(folder): shutil.rmtree(folder)
            shutil.copytree(self.tmp.name, folder)
            return folder
        def boot(folder):
            store = FlashPointStore(os.path.join(folder, "points.jsonl"), autoload=False)
            asyncio.run(store.load_async())
            try:
                self.assertEqual(len(store.offsets["line-a"]), 8)
                store.remove_unreferenced()
            finally: store.close()
        with Injector(stage("count")) as injector: boot(injector.folder)
        self.assertGreater(injector.count, 1)
        newer = os.path.basename(self.newer)
        for crash_at in range(1, injector.count + 1):
            with self.subTest(crash_at=crash_at):
                image = os.path.join(base, "image")
                if os.path.exists(image): shutil.rmtree(image)
                with Injector(stage("run"), image, crash_at):
                    with self.assertRaises(PowerCut): boot(os.path.join(base, "run"))
                for _boot in range(3): boot(image)
                with open(os.path.join(image, newer), encoding="utf-8") as source:
                    self.assertEqual(source.read(), ORPHAN)

    def test_a_compaction_never_reuses_a_protected_generation(self):
        self.damage_main()
        self.boot()
        for compact in ("rewrite", "rewrite_async"):
            with self.subTest(compact):
                store = FlashPointStore(self.path)
                try:
                    before = store.generation
                    if compact == "rewrite": store.rewrite([])
                    else: asyncio.run(store.rewrite_async([]))
                    self.assertNotEqual(store.generation, self.generation + 1)
                    self.assertGreater(store.generation, before)
                finally: store.close()
                with open(self.newer, encoding="utf-8") as source:
                    self.assertEqual(source.read(), ORPHAN)
        store = FlashPointStore(self.path, autoload=False)
        asyncio.run(store.load_async())
        try:
            store.remove_unreferenced()
            self.assertEqual(store.quarantine(), {"files": 1, "bytes": len(ORPHAN)})
        finally: store.close()


class TrackerBootSweepTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="esp-rtk-orphans-")
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "survey")
        capacity = mock.patch.object(tracking.Tracker, "storage_capacity",
                                     return_value={"writable": True})
        capacity.start(); self.addCleanup(capacity.stop)
        tracker = tracking.Tracker(self.path)
        tracker.create_project("Field")
        tracker.start_line("Route", .2)
        with mock.patch("pointstore.POINT_SEGMENT_BYTES", 600):
            for index in range(12):
                tracker.add_vertex(dict(FIX, lon=8 + index * .00002,
                                        lon_e8=800000000 + index * 2000))
        tracker.compact()
        self.backup = tracker.backup()
        self.generation = tracker.point_store.generation
        self.live = tracker.point_store.sidecar_paths()
        tracker.point_store.close(); tracker._sync_journal(True)

    def plant(self, count=3):
        paths = []
        for number in range(2, 2 + count):
            path = "%s.points-v1.jsonl.g%08x-%04d.pjs" % (self.path, self.generation - 2, number)
            with open(path, "w", encoding="utf-8") as target: target.write(ORPHAN * number)
            paths.append(path)
        return paths, sum(len(ORPHAN) * number for number in range(2, 2 + count))

    def boot(self):
        tracker = tracking.Tracker(self.path, autoload=False, point_store_autoload=False)
        return tracker

    def close(self, tracker):
        tracker.point_store.close(); tracker._sync_journal(True)

    async def test_boot_removes_orphans_reports_bytes_and_keeps_features(self):
        orphans, size = self.plant()
        tracker = self.boot()
        with support.patch_module_global("tracking", "log") as log:
            await tracker.initialize_async()
        try:
            self.assertEqual(tracker.backup(), self.backup)
            self.assertEqual(tracker.storage_recovery["unreferenced_point_files"], 3)
            self.assertEqual(tracker.storage_recovery["unreferenced_point_bytes"], size)
            self.assertTrue(any("%d bytes" % size in str(call) for call in log.call_args_list),
                            log.call_args_list)
        finally: self.close(tracker)
        for path in orphans: self.assertFalse(os.path.exists(path))
        for path in self.live: self.assertTrue(os.path.exists(path))
        again = tracking.Tracker(self.path)
        try:
            self.assertEqual(again.backup(), self.backup)
            self.assertEqual(again.storage_recovery["unreferenced_point_files"], 0)
        finally: self.close(again)

    async def test_boot_with_a_damaged_manifest_deletes_nothing(self):
        orphans, _size = self.plant()
        manifest = self.path + ".points-v1.jsonl.segments.json"
        with open(manifest, "w", encoding="utf-8") as target: target.write("{")
        tracker = self.boot()
        try:
            try: await tracker.initialize_async()
            except Exception: pass
            self.assertEqual(tracker.storage_recovery["unreferenced_point_files"], 0)
        finally: self.close(tracker)
        for path in orphans + self.live: self.assertTrue(os.path.exists(path), path)

    async def test_a_recovered_store_keeps_newer_files_across_boots_and_reports_them(self):
        from support import main, state as app_state
        manifest = self.path + ".points-v1.jsonl.segments.json"
        with open(manifest, encoding="utf-8") as source: original = source.read()
        with open(manifest + ".previous", "w", encoding="utf-8") as target:
            target.write(original)
        with open(manifest, "w", encoding="utf-8") as target: target.write("{")
        newer = "%s.points-v1.jsonl.g%08x-0001.pjs" % (self.path, self.generation + 1)
        with open(newer, "w", encoding="utf-8") as target: target.write(ORPHAN)
        for _boot in range(3):
            tracker = self.boot()
            app_state.app.stats.pop("storage_recovery", None)
            with mock.patch.object(main, "tracker", tracker), \
                    mock.patch.object(main, "prepare_parallel_benchmark",
                                      return_value=(None, None)), \
                    mock.patch.object(tracker, "run_worker", mock.AsyncMock()), \
                    support.patch_module_global("tracking", "log") as log:
                await main.tracking_startup_task()
            try:
                self.assertEqual(tracker.backup(), self.backup)
                recovery = app_state.app.stats["storage_recovery"]
                self.assertEqual((recovery["quarantined_point_files"],
                                  recovery["quarantined_point_bytes"]), (1, len(ORPHAN)))
                self.assertTrue(any("recovered.json" in str(call)
                                    for call in log.call_args_list), log.call_args_list)
            finally: self.close(tracker)
            self.assertTrue(os.path.exists(newer))
        tracker.point_store.release_quarantine()
        tracker = self.boot()
        await tracker.initialize_async()
        try:
            self.assertEqual(tracker.storage_recovery["unreferenced_point_files"], 1)
            self.assertIsNone(tracker.storage_recovery["quarantined_point_files"])
        finally: self.close(tracker)
        self.assertFalse(os.path.exists(newer))

    async def test_the_boot_sweep_feeds_the_watchdog_between_files(self):
        from support import state as app_state
        self.plant(6)
        events = []
        real_remove = os.remove
        def remove(path):
            if str(path).endswith(".pjs"): events.append("remove")
            return real_remove(path)
        tracker = self.boot()
        with mock.patch.object(app_state.app, "beat",
                               side_effect=lambda name: events.append("beat")), \
                mock.patch("pointstore.os.remove", side_effect=remove):
            await tracker.initialize_async()
        try: self.assertEqual(tracker.storage_recovery["unreferenced_point_files"], 6)
        finally: self.close(tracker)
        first = events.index("remove")
        self.assertNotIn("remove remove", " ".join(events[first:]))
        self.assertEqual(events[first:].count("remove"), 6)

    async def test_status_carries_the_boot_cleanup(self):
        from support import main, state as app_state
        self.plant(2)
        tracker = self.boot()
        app_state.app.stats.pop("storage_recovery", None)
        with mock.patch.object(main, "tracker", tracker), \
                mock.patch.object(main, "prepare_parallel_benchmark", return_value=(None, None)), \
                mock.patch.object(tracker, "run_worker", mock.AsyncMock()):
            await main.tracking_startup_task()
        try:
            self.assertEqual(app_state.app.stats["storage_recovery"]["unreferenced_point_files"], 2)
            self.assertEqual(app_state.app.stats["tracking_startup"]["state"], "ready")
        finally: self.close(tracker)


if __name__ == "__main__":
    unittest.main()
