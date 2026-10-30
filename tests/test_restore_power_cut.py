# SPDX-License-Identifier: AGPL-3.0-only
"""A reset at any write step of a restore leaves the old or the new survey.

The restore stages a complete store under its own prefix and publishes it by
one rename of a commit record; boot completes a committed restore and drops
an uncommitted one. Each test replays one restore and cuts the power at every
crash point (see powercut.py), then boots the frozen image and requires either
the complete original or the complete restored survey, never a mix.
"""
import asyncio
import json
import os
import shutil
import tempfile
import unittest
import warnings
from unittest import mock

import support
from support import state  # noqa: F401 - MicroPython compatibility
import tracking
from powercut import Injector, PowerCut
from test_tracking import FIX

try:
    import littlefs
except ImportError:  # pragma: no cover - optional host dependency
    littlefs = None

SEGMENT_BYTES = 600
# Every crash point everywhere; by default the staging phase, which writes only
# under the staging prefix, is sampled and the publication window is complete.
FULL = os.environ.get("ESP_RTK_POWER_CUT_FULL") == "1"


def fix(index, lat_step=0):
    return dict(FIX, lon=8 + index * .00002, lon_e8=800000000 + index * 2000,
                lat_e8=5000000000 + lat_step * 1000)


def survey(tracker, name, lines, assets, tail):
    """A project with closed lines, object points and journal-only vertices."""
    tracker.create_project(name)
    for line_index, count in enumerate(lines):
        tracker.start_line("%s line %d" % (name, line_index), .2)
        for index in range(count):
            tracker.add_vertex(fix(index, line_index))
        tracker.set_line_state("finish")
    for index in range(assets):
        tracker.add_asset("tree", "%s asset %d" % (name, index), "", fix(100 + index, 9))
    tracker.compact()
    if tail:
        tracker.start_line("%s open line" % name, .2)
        for index in range(tail):
            tracker.add_vertex(fix(200 + index, 7))


def close(tracker):
    tracker.point_store.close(); tracker._sync_journal(True)


def leftovers(folder):
    return sorted(name for name in os.listdir(folder) if ".restore." in name)


class RestorePowerCutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="esp-rtk-powercut-")
        self.addCleanup(self.tmp.cleanup)
        # Handles abandoned at a cut are closed by the collector, by design.
        catcher = warnings.catch_warnings()
        catcher.__enter__(); self.addCleanup(catcher.__exit__, None, None, None)
        warnings.simplefilter("ignore", ResourceWarning)
        for patch in (mock.patch.object(tracking.Tracker, "storage_capacity",
                                        return_value={"writable": True}),
                      mock.patch("pointstore.POINT_SEGMENT_BYTES", SEGMENT_BYTES),
                      # Yield to the loop without waiting: thousands of runs.
                      mock.patch("uasyncio.sleep_ms", lambda ms: asyncio.sleep(0)),
                      support.patch_module_global("tracking", "log")):
            patch.start(); self.addCleanup(patch.stop)
        self.initial = os.path.join(self.tmp.name, "initial")
        os.mkdir(self.initial)
        original = tracking.Tracker(os.path.join(self.initial, "survey"))
        survey(original, "Old", (6, 4), 2, 3)
        close(original)
        source_folder = os.path.join(self.tmp.name, "source")
        os.mkdir(source_folder)
        source = tracking.Tracker(os.path.join(source_folder, "survey"))
        survey(source, "New", (5, 4, 3), 3, 0)
        self.payload = source.backup()
        self.rows = list(source.iter_backup_ndjson())
        close(source)
        self.old = self.boot(self.initial)

    def copy(self, name):
        target = os.path.join(self.tmp.name, name)
        if os.path.exists(target): shutil.rmtree(target)
        shutil.copytree(self.initial, target)
        return target

    def boot(self, folder, asynchronous=False):
        tracker = tracking.Tracker(os.path.join(folder, "survey"), autoload=False,
                                   point_store_autoload=False)
        try:
            if asynchronous: asyncio.run(tracker.initialize_async())
            else: tracker.initialize()
            return tracker.backup()
        finally: close(tracker)

    def run_restore(self, kind, folder):
        tracker = tracking.Tracker(os.path.join(folder, "survey"))
        try:
            if kind == "json":
                tracker.restore(json.loads(json.dumps(self.payload)))
            elif kind == "ndjson":
                tracker.restore_ndjson(iter(self.rows))
            elif kind == "json_async":
                asyncio.run(tracker.restore_async(json.loads(json.dumps(self.payload))))
            else:
                asyncio.run(tracker.restore_ndjson_async(iter(self.rows)))
            return tracker.backup()
        finally:
            try: close(tracker)
            except BaseException: pass

    def crash_points(self, kind, stride):
        """Restore once without a cut; set self.new and return the crash points.

        Also sets self.publication, the crash point at which the commit record
        is written.
        """
        folder = self.copy("count")
        marks = []
        publish = tracking.Tracker._write_restore_commit
        def mark(tracker, staging):
            marks.append(injector.count)
            return publish(tracker, staging)
        with Injector(folder) as injector, \
                mock.patch.object(tracking.Tracker, "_write_restore_commit", mark):
            restored = self.run_restore(kind, folder)
        self.new = self.boot(folder)
        self.assertEqual(restored, self.new)
        self.assertNotEqual(self.old, self.new)
        self.assertEqual(sum(len(p["points"]) + sum(len(l["coordinates"]) for l in p["lines"])
                             for p in self.new["projects"]), 15)
        self.assertEqual(len(marks), 1)
        self.publication, total = marks[0], injector.count
        self.assertGreater(total, self.publication + 20)
        window = range(max(1, self.publication - 20), total + 1)
        sampled = range(1, total + 1, 1 if FULL else stride)
        return sorted(set(window) | set(sampled))

    def cut(self, kind, crash_at, name="image"):
        """Run the restore, cut the power at crash_at; return the frozen image."""
        folder, image = self.copy("run"), os.path.join(self.tmp.name, name)
        if os.path.exists(image): shutil.rmtree(image)
        with Injector(folder, image, crash_at):
            with self.assertRaises(PowerCut):
                self.run_restore(kind, folder)
        return image

    def assert_consistent(self, image, label, asynchronous=False, reboot=False):
        state = self.boot(image, asynchronous)
        self.assertIn(state, (self.old, self.new), label)
        self.assertEqual(leftovers(image), [], label)
        if reboot or FULL:
            # Recovery is complete: a second boot sees the same survey.
            self.assertEqual(self.boot(image), state, label)
        return state

    def replay(self, kind, stride):
        points = self.crash_points(kind, stride)
        outcomes, committed = [], []
        for crash_at in points:
            image = self.cut(kind, crash_at)
            published = os.path.exists(os.path.join(image, "survey.restore.commit.json"))
            if published: committed.append(crash_at)
            state = self.assert_consistent(image, "%s crash at %d of %d" % (
                kind, crash_at, points[-1]), asynchronous=crash_at % 2 == 0, reboot=published)
            outcomes.append(state == self.new)
        # Early cuts keep the original, late cuts the restore; once published
        # the restore is never undone.
        self.assertFalse(outcomes[0]); self.assertTrue(outcomes[-1])
        self.assertEqual(outcomes, sorted(outcomes))
        self.assertTrue(committed)
        # The rename of the record is the single switch between both states.
        self.assertEqual(outcomes.index(True), points.index(committed[0]))

    def test_json_restore_survives_a_reset_at_every_write(self):
        self.replay("json", 5)

    def test_ndjson_restore_survives_a_reset_at_every_write(self):
        self.replay("ndjson", 5)

    def test_async_restores_survive_a_reset_at_every_write(self):
        for kind in ("json_async", "ndjson_async"):
            with self.subTest(kind):
                self.replay(kind, 11)

    def test_reset_during_boot_completion_still_completes_the_restore(self):
        points = self.crash_points("ndjson", 1)
        pending = [crash_at for crash_at in points if crash_at > self.publication and
                   os.path.exists(os.path.join(self.cut("ndjson", crash_at, "probe"),
                                               "survey.restore.commit.json"))]
        self.assertTrue(pending)
        for crash_at in ((pending[0], pending[len(pending) // 2]) if FULL else pending[:1]):
            image = self.cut("ndjson", crash_at, "pending")
            counter = os.path.join(self.tmp.name, "boot-count")
            if os.path.exists(counter): shutil.rmtree(counter)
            shutil.copytree(image, counter)
            with Injector(counter) as injector:
                self.assertEqual(self.boot(counter), self.new)
            for boot_crash in range(1, injector.count + 1):
                second = os.path.join(self.tmp.name, "boot-run")
                frozen = os.path.join(self.tmp.name, "boot-image")
                for path in (second, frozen):
                    if os.path.exists(path): shutil.rmtree(path)
                shutil.copytree(image, second)
                with Injector(second, frozen, boot_crash):
                    with self.assertRaises(PowerCut): self.boot(second)
                self.assertEqual(self.assert_consistent(
                    frozen, "restore cut at %d, boot cut at %d" % (crash_at, boot_crash)),
                    self.new)

    def test_uncommitted_staging_is_discarded_at_boot(self):
        folder = self.copy("staged")
        tracker = tracking.Tracker(os.path.join(folder, "survey"))
        staging = tracker._fresh_restore_store()
        for event in staging._events_for_backup(self.payload):
            staging._append(event, defer_index=True)
        staging._write_checkpoint(); staging._write_index()
        close(staging); close(tracker)
        self.assertTrue(leftovers(folder))
        restarted = tracking.Tracker(os.path.join(folder, "survey"), autoload=False,
                                     point_store_autoload=False)
        restarted.initialize()
        try:
            self.assertEqual(restarted.backup(), self.old)
            self.assertEqual(restarted.storage_recovery["restore"], "discarded")
        finally: close(restarted)
        self.assertEqual(leftovers(folder), [])


@unittest.skipUnless(littlefs, "littlefs-python not installed")
class LittleFsRestorePowerCutTests(unittest.TestCase):
    """The same guarantee on a real LittleFS image, cut at block writes.

    A cut is the image just before one block program or erase (see
    lfs_image.py). The publication window is cut before every block write,
    the staging phase is sampled unless ESP_RTK_POWER_CUT_FULL=1.
    """

    def setUp(self):
        import lfs_image
        self.Volume = lfs_image.Volume
        for patch in (mock.patch.object(tracking.Tracker, "storage_capacity",
                                        return_value={"writable": True}),
                      mock.patch("pointstore.POINT_SEGMENT_BYTES", SEGMENT_BYTES),
                      support.patch_module_global("tracking", "log")):
            patch.start(); self.addCleanup(patch.stop)
        folder = tempfile.TemporaryDirectory(prefix="esp-rtk-powercut-")
        self.addCleanup(folder.cleanup)
        source = tracking.Tracker(os.path.join(folder.name, "survey"))
        survey(source, "New", (5, 4, 3), 3, 0)
        self.rows = list(source.iter_backup_ndjson())
        close(source)
        with self.Volume() as volume:
            original = tracking.Tracker("survey")
            survey(original, "Old", (6, 4), 2, 3)
            close(original)
            self.base = volume.image()
        self.old = self.boot(self.base)[0]

    def boot(self, image, cuts=()):
        """Boot from image; return (survey, image after boot, {write: image at cut})."""
        volume = self.Volume(image)
        volume.cut_before(*cuts)
        with volume:
            tracker = tracking.Tracker("survey", autoload=False, point_store_autoload=False)
            try:
                tracker.initialize()
                state = tracker.backup()
                self.assertEqual([name for name in volume.listdir("/") if ".restore." in name], [])
            finally: close(tracker)
        return state, volume.image(), volume.images()

    def restore(self, cuts=()):
        """Restore on the base image; return (block writes, publication, {write: image})."""
        volume, marks = self.Volume(self.base), []
        volume.cut_before(*cuts)
        publish = tracking.Tracker._write_restore_commit
        def mark(tracker, staging):
            marks.append(volume.context.writes)
            return publish(tracker, staging)
        with volume, mock.patch.object(tracking.Tracker, "_write_restore_commit", mark):
            tracker = tracking.Tracker("survey")
            try: tracker.restore_ndjson(iter(self.rows))
            finally: close(tracker)
        return volume.context.writes, marks[0], volume.images()

    def published(self, image):
        with self.Volume(image) as volume:
            return "survey.restore.commit.json" in volume.listdir("/")

    def test_restore_on_littlefs_survives_a_reset_at_every_block_write(self):
        total, publication, _images = self.restore()
        self.assertGreater(total, publication + 20)
        window = range(max(0, publication - 20), total + 1)
        points = sorted(set(window) | set(range(0, total, 1 if FULL else 5)))
        _total, _publication, images = self.restore(points)
        new = self.boot(images[total])[0]
        self.assertNotEqual(new, self.old)
        outcomes, pending = [], []
        for write in points:
            label = "cut before block write %d of %d" % (write, total)
            state, booted, _cuts = self.boot(images[write])
            self.assertIn(state, (self.old, new), label)
            published = self.published(images[write])
            if published or FULL:
                # Once recovered, a second boot changes nothing.
                self.assertEqual(self.boot(booted)[0], state, label)
            outcomes.append(state == new)
            if published: pending.append(images[write])
        self.assertFalse(outcomes[0]); self.assertTrue(outcomes[-1])
        self.assertEqual(outcomes, sorted(outcomes))
        self.assertTrue(pending)
        # A reset during the boot that completes a published restore: the
        # next boot still completes it.
        for image in ((pending[0], pending[len(pending) // 2]) if FULL else pending[:1]):
            volume = self.Volume(image)
            with volume:
                tracker = tracking.Tracker("survey", autoload=False, point_store_autoload=False)
                tracker.initialize(); close(tracker)
            boot_writes = volume.context.writes
            self.assertGreater(boot_writes, 0)
            _state, _after, frozen = self.boot(image, range(boot_writes))
            for write in range(boot_writes):
                self.assertEqual(self.boot(frozen[write])[0], new,
                                 "boot cut before block write %d" % write)


if __name__ == "__main__":
    unittest.main()
