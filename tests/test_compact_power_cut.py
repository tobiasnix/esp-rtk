# SPDX-License-Identifier: AGPL-3.0-only
"""A reset at any write step of a compaction leaves the same survey.

A compaction writes the checkpoint and the index, reads the checkpoint back,
removes the covered journal segments and rewrites the point store. It never
changes the survey, so every crash point (see powercut.py) must boot to the
survey as it was before, and a second boot must agree.
"""
import asyncio
import os
import shutil
import tempfile
import unittest
import warnings
from unittest import mock

import support
from support import state  # noqa: F401 - MicroPython compatibility
import tracking
from pointstore import FlashPointStore
from powercut import Injector, PowerCut
from test_restore_power_cut import SEGMENT_BYTES, close, fix, survey

# Entered at most once per phase of a compaction; the first entry after the
# compaction started marks where that phase begins.
PHASES = (("checkpoint", tracking.Tracker, "_write_checkpoint"),
          ("index", tracking.Tracker, "_write_index"),
          ("verification", tracking.Tracker, "_load_checkpoint"),
          ("rewrite", FlashPointStore, "rewrite_async"),
          ("rewrite", FlashPointStore, "rewrite"))


class CompactPowerCutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="esp-rtk-compact-powercut-")
        self.addCleanup(self.tmp.cleanup)
        # Handles abandoned at a cut are closed by the collector, by design.
        catcher = warnings.catch_warnings()
        catcher.__enter__(); self.addCleanup(catcher.__exit__, None, None, None)
        warnings.simplefilter("ignore", ResourceWarning)
        for patch in (mock.patch.object(tracking.Tracker, "storage_capacity",
                                        return_value={"writable": True}),
                      mock.patch("pointstore.POINT_SEGMENT_BYTES", SEGMENT_BYTES),
                      # Yield to the loop without waiting: hundreds of runs.
                      mock.patch("uasyncio.sleep_ms", lambda ms: asyncio.sleep(0)),
                      support.patch_module_global("tracking", "log")):
            patch.start(); self.addCleanup(patch.stop)
        self.initial = os.path.join(self.tmp.name, "initial")
        os.mkdir(self.initial)
        t = tracking.Tracker(os.path.join(self.initial, "survey"))
        # A journal rest after a checkpoint; the undo and the deleted project
        # leave points that the point-store rewrite drops.
        survey(t, "Old", (6, 4), 2, 3)
        t.add_asset("tree", "late", "", fix(150, 9))
        t.set_line_state("finish")
        second = t.create_project("Second")["id"]
        t.start_line("S", .2)
        for index in range(4): t.add_vertex(fix(300 + index, 5))
        t.undo_last(); t.undo_last()
        t.add_vertex(fix(310, 5))
        t.set_line_state("finish")
        gone = t.create_project("Gone")["id"]
        t.start_line("G", .2)
        for index in range(3): t.add_vertex(fix(400 + index, 6))
        t.set_line_state("finish")
        t.select_project(second)
        t.delete_project(gone)
        close(t)
        self.reference = self.boot(self.initial)

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

    def compact(self, kind, folder):
        tracker = tracking.Tracker(os.path.join(folder, "survey"))
        try:
            if kind == "compact_async": asyncio.run(tracker.compact_async())
            else: tracker.compact()
            return tracker.backup()
        finally:
            try: close(tracker)
            except BaseException: pass

    def phases(self, kind, folder):
        """Compact once without a cut; return the first crash point of each phase."""
        marks, started = [], []
        patches = []
        def marker(name, original, start=False):
            def wrapper(*args, **kwargs):
                if start: started.append(len(marks))
                marks.append((name, injector.count))
                return original(*args, **kwargs)
            return wrapper
        entry = getattr(tracking.Tracker, kind)
        patches.append(mock.patch.object(tracking.Tracker, kind, marker("start", entry, True)))
        for name, owner, attribute in PHASES:
            patches.append(mock.patch.object(owner, attribute,
                                             marker(name, getattr(owner, attribute))))
        with Injector(folder) as injector:
            for patch in patches: patch.start()
            try: result = self.compact(kind, folder)
            finally:
                for patch in reversed(patches): patch.stop()
        self.assertEqual(result, self.reference)
        self.assertEqual(self.boot(folder), self.reference)
        self.assertTrue(started)
        first = {}
        for name, count in marks[started[0]:]:
            first.setdefault(name, count)
        return first, injector.count

    def replay(self, kind):
        first, total = self.phases(kind, self.copy("count"))
        # Every phase holds at least one crash point; the points between the
        # verification and the rewrite are the journal removals (the
        # verification itself writes nothing).
        self.assertLess(first["checkpoint"], first["index"])
        self.assertLess(first["index"], first["verification"])
        self.assertLess(first["verification"], first["rewrite"])
        self.assertLess(first["rewrite"], total)
        for crash_at in range(1, total + 1):
            label = "%s crash at %d of %d" % (kind, crash_at, total)
            folder, image = self.copy("run"), os.path.join(self.tmp.name, "image")
            if os.path.exists(image): shutil.rmtree(image)
            with Injector(folder, image, crash_at):
                with self.assertRaises(PowerCut, msg=label):
                    self.compact(kind, folder)
            self.assertEqual(self.boot(image, asynchronous=crash_at % 2 == 0),
                             self.reference, label)
            # Recovery is complete: a second boot sees the same survey.
            self.assertEqual(self.boot(image), self.reference, label)

    def test_a_reset_at_any_write_of_compact_async_keeps_the_survey(self):
        self.replay("compact_async")

    def test_a_reset_at_any_write_of_compact_keeps_the_survey(self):
        self.replay("compact")


if __name__ == "__main__":
    unittest.main()
