# SPDX-License-Identifier: AGPL-3.0-only
"""Storage guards: maintenance registry, reader/maintenance guards, public queries."""
import ast
import asyncio
import os
import tempfile
import unittest
from unittest import mock

from support import web, state  # noqa: F401  (support sets up the MicroPython shims)
import tracking
from tracking import Tracker, StorageBusy
from test_tracking import FIX

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestMaintenanceRegistry(unittest.TestCase):
    def test_tokens_end_exactly_their_own_entry(self):
        app = state.AppState()
        self.assertEqual(app.begin_maintenance("a"), 1)
        self.assertEqual(app.maintenance_reason, "a")
        self.assertEqual(app.begin_maintenance("b"), 2)
        self.assertEqual(app.maintenance_reason, "b")
        app.end_maintenance(1)
        self.assertEqual(app.maintenance_reason, "b")
        app.end_maintenance(1)
        self.assertEqual(app.maintenance_reason, "b")
        app.end_maintenance(99)
        self.assertEqual(app.maintenance_reason, "b")
        app.end_maintenance(2)
        self.assertIsNone(app.maintenance_reason)

    def test_newest_first_order_falls_back_to_the_older_reason(self):
        app = state.AppState()
        first, second = app.begin_maintenance("a"), app.begin_maintenance("b")
        app.end_maintenance(second)
        self.assertEqual(app.maintenance_reason, "a")
        app.end_maintenance(first)
        self.assertIsNone(app.maintenance_reason)

    def test_fresh_state_has_no_maintenance(self):
        self.assertIsNone(state.AppState().maintenance_reason)


class GuardTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.app = state.AppState()
        patcher = mock.patch.object(state, "app", self.app)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.t = self.make_tracker("survey")
        os.mkdir(os.path.join(self.folder.name, "second"))
        self.t2 = self.make_tracker(os.path.join("second", "survey"))

    def make_tracker(self, name):
        tracker = Tracker(os.path.join(self.folder.name, name))
        self.addCleanup(tracker.point_store.close)
        self.addCleanup(tracker._sync_journal, True)
        return tracker

    def open_line(self, tracker):
        tracker.create_project("P")
        tracker.start_line("L", .2)

    def assert_busy(self, error, code):
        self.assertIsInstance(error.exception, StorageBusy)
        self.assertIsInstance(error.exception, ValueError)
        self.assertEqual(error.exception.code, code)


class TestReaderGuard(GuardTestCase):
    async def test_reader_counts_and_releases(self):
        t = self.t
        async with t.reader() as handle:
            self.assertIsNotNone(handle)
            self.assertEqual(t.storage_readers, 1)
        self.assertEqual(t.storage_readers, 0)
        with self.assertRaises(KeyError):
            async with t.reader():
                raise KeyError("boom")
        self.assertEqual(t.storage_readers, 0)

    async def test_reader_is_refused_during_maintenance(self):
        t = self.t
        async with t.maintenance("tracking_storage"):
            with self.assertRaises(StorageBusy) as error:
                async with t.reader():
                    self.fail("read during maintenance")
            self.assert_busy(error, "storage_busy")
            self.assertEqual(t.storage_readers, 0)
        t._compacting = True
        with self.assertRaises(StorageBusy) as error:
            async with t.reader():
                self.fail("read during compaction")
        self.assert_busy(error, "storage_busy")
        self.assertEqual(t.storage_readers, 0)


class TestMaintenanceGuard(GuardTestCase):
    async def test_maintenance_refuses_each_protected_state(self):
        def install_pending(t): t._install_pending = True
        def not_loaded(t): t._loaded = False
        def compacting(t): t._compacting = True
        def recording(t): self.open_line(t)
        def paused(t):
            self.open_line(t)
            t.set_line_state("pause")
        def pending_fix(t): t._pending_fixes.append({})
        cases = [("install_pending", install_pending, "restore_install_pending"),
                 ("not_loaded", not_loaded, "survey_loading"),
                 ("compacting", compacting, "storage_busy"),
                 ("recording", recording, "recording_active"),
                 ("paused", paused, "recording_active"),
                 ("pending_fix", pending_fix, "recording_active")]
        for index, (name, prepare, code) in enumerate(cases):
            with self.subTest(name):
                t = self.make_tracker("case-%d" % index)
                prepare(t)
                active = t.maintenance_active()
                with self.assertRaises(StorageBusy) as error:
                    async with t.maintenance("tracking_storage"):
                        self.fail("entered a protected state")
                self.assert_busy(error, code)
                self.assertEqual(t.maintenance_active(), active)
                self.assertIsNone(self.app.maintenance_reason)
                self.assertEqual(self.app._maintenance, [])
        with self.subTest("inside_maintenance"):
            t = self.make_tracker("case-outer")
            async with t.maintenance("outer"):
                registry = list(self.app._maintenance)
                with self.assertRaises(StorageBusy) as error:
                    async with t.maintenance("tracking_storage"):
                        self.fail("entered a running maintenance")
                self.assert_busy(error, "storage_busy")
                self.assertTrue(t.maintenance_active())
                self.assertEqual(self.app.maintenance_reason, "outer")
                self.assertEqual(self.app._maintenance, registry)
            self.assertFalse(t.maintenance_active())

    async def test_checkpoint_exception_is_narrow(self):
        t = self.t
        self.open_line(t)
        entered = False
        async with t.maintenance("tracking_checkpoint", allow_recording=True):
            entered = True
            self.assertEqual(self.app.maintenance_reason, "tracking_checkpoint")
        self.assertTrue(entered)
        t._loaded = False
        with self.assertRaises(StorageBusy) as error:
            async with t.maintenance("tracking_checkpoint", allow_recording=True):
                self.fail("checkpoint before load")
        self.assert_busy(error, "survey_loading")
        t._loaded = True
        t._install_pending = True
        with self.assertRaises(StorageBusy) as error:
            async with t.maintenance("tracking_checkpoint", allow_recording=True):
                self.fail("checkpoint while an install is pending")
        self.assert_busy(error, "restore_install_pending")
        t._install_pending = False
        async with t.maintenance("tracking_storage", allow_recording=True):
            with self.assertRaises(StorageBusy) as error:
                async with t.maintenance("tracking_checkpoint", allow_recording=True):
                    self.fail("checkpoint inside maintenance")
            self.assert_busy(error, "storage_busy")
        self.assertEqual(self.app._maintenance, [])

    async def test_maintenance_waits_for_readers(self):
        t = self.t
        release, entered = asyncio.Event(), []

        async def hold():
            async with t.reader():
                await release.wait()

        async def maintain():
            async with t.maintenance("tracking_storage"):
                entered.append(True)

        holder = asyncio.create_task(hold())
        await asyncio.sleep(0)
        waiting = asyncio.create_task(maintain())
        await asyncio.sleep(.05)
        self.assertTrue(t.maintenance_active())
        self.assertEqual(entered, [])
        release.set()
        await holder
        await waiting
        self.assertEqual(entered, [True])
        self.assertFalse(t.maintenance_active())

    async def test_maintenance_sets_reason_and_progress(self):
        t = self.t
        progress = {"operation": "compact", "state": "running", "error": None}
        async with t.maintenance("tracking_storage", progress) as job:
            self.assertTrue(job.entered)
            self.assertEqual(self.app.maintenance_reason, "tracking_storage")
            self.assertEqual(t.operation_snapshot()["operation"], "compact")
            job.update(state="complete")
            self.assertEqual(t.operation_snapshot()["state"], "complete")
        self.assertFalse(job.entered)
        self.assertIsNone(self.app.maintenance_reason)
        self.assertFalse(t.maintenance_active())
        snapshot = t.operation_snapshot()
        self.assertIsNot(snapshot, t.operation_progress)
        self.assertEqual(snapshot, t.operation_progress)

    async def test_nested_maintenance_returns_to_the_outer_reason(self):
        t = self.t
        async with t.maintenance("tracking_storage") as outer:
            async with t.maintenance("tracking_restore", parent=outer):
                self.assertEqual(self.app.maintenance_reason, "tracking_restore")
            self.assertEqual(self.app.maintenance_reason, "tracking_storage")
            self.assertTrue(t.maintenance_active())
        self.assertIsNone(self.app.maintenance_reason)
        self.assertFalse(t.maintenance_active())
        async with t.maintenance("tracking_storage") as outer:
            try:
                async with t.maintenance("tracking_restore", parent=outer):
                    raise ValueError("bad backup")
            except ValueError:
                pass
            self.assertEqual(self.app.maintenance_reason, "tracking_storage")
            self.assertTrue(t.maintenance_active())
        self.assertIsNone(self.app.maintenance_reason)
        self.assertFalse(t.maintenance_active())
        self.assertEqual(t._maintenance_depth, 0)

    async def test_a_foreign_task_cannot_enter_a_running_maintenance(self):
        t = self.t
        stop, started = asyncio.Event(), asyncio.Event()

        async def task_a():
            async with t.maintenance("a"):
                started.set()
                await stop.wait()

        holder = asyncio.create_task(task_a())
        await started.wait()
        entered_b = False
        with self.assertRaises(StorageBusy) as error:
            async with t.maintenance("b"):
                entered_b = True
        self.assert_busy(error, "storage_busy")
        self.assertIs(entered_b, False)
        self.assertEqual(self.app.maintenance_reason, "a")
        stop.set()
        await holder
        self.assertIsNone(self.app.maintenance_reason)

    async def test_a_child_of_a_waiting_parent_is_refused(self):
        t = self.t
        release = asyncio.Event()

        async def hold():
            async with t.reader():
                await release.wait()

        holder = asyncio.create_task(hold())
        await asyncio.sleep(0)
        outer = t.maintenance("a")
        waiting = asyncio.create_task(outer.__aenter__())
        await asyncio.sleep(.05)
        self.assertIs(outer.entered, False)
        foreign = self.t2.maintenance("foreign")
        await foreign.__aenter__()
        registry = list(self.app._maintenance)
        for name, parent in (("waiting", outer), ("other_tracker", foreign),
                             ("not_a_guard", object())):
            with self.subTest(name):
                entered = False
                with self.assertRaises(StorageBusy) as error:
                    async with t.maintenance("child", parent=parent):
                        entered = True
                self.assert_busy(error, "storage_busy")
                self.assertIs(entered, False)
                self.assertEqual(t.storage_readers, 1)
                self.assertEqual(self.app._maintenance, registry)
                self.assertEqual(t._maintenance_depth, 1)
        await foreign.__aexit__(None, None, None)
        self.assertEqual(self.app.maintenance_reason, "a")
        left = t.maintenance("left")
        release.set()
        await holder
        self.assertIs(await waiting, outer)
        self.assertIs(outer.entered, True)
        await outer.__aexit__(None, None, None)
        self.assertFalse(t.maintenance_active())
        await left.__aenter__()
        await left.__aexit__(None, None, None)
        with self.assertRaises(StorageBusy) as error:
            async with t.maintenance("child", parent=left):
                self.fail("entered below a guard that already left")
        self.assert_busy(error, "storage_busy")
        self.assertEqual(self.app._maintenance, [])
        self.assertFalse(t.maintenance_active())

    async def test_failure_after_token_registration_is_fully_undone(self):
        t = self.t
        failing = mock.MagicMock(update=mock.Mock(side_effect=RuntimeError("boom")))
        with mock.patch.object(t, "operation_progress", failing):
            with self.assertRaises(RuntimeError):
                async with t.maintenance("x", {"state": "running"}):
                    self.fail("entered after a failed progress update")
        self.assertEqual(self.app._maintenance, [])
        self.assertIsNone(self.app.maintenance_reason)
        self.assertEqual(t._maintenance_depth, 0)
        self.assertIs(t.storage_busy, False)
        self.assertFalse(t.maintenance_active())
        async with t.maintenance("x", {"operation": "compact", "state": "running",
                                       "error": None}):
            pass
        self.assertEqual(self.app._maintenance, [])
        async with t.maintenance("parent") as parent:
            registry = list(self.app._maintenance)
            with mock.patch.object(t, "operation_progress", failing):
                with self.assertRaises(RuntimeError):
                    async with t.maintenance("child", {"state": "running"}, parent=parent):
                        self.fail("child entered after a failed progress update")
            self.assertEqual(self.app._maintenance, registry)
            self.assertEqual(len(registry), 1)
            self.assertEqual(t._maintenance_depth, 1)
            self.assertIs(t.storage_busy, True)
            self.assertIs(parent.entered, True)
            self.assertEqual(self.app.maintenance_reason, "parent")
        self.assertEqual(self.app._maintenance, [])
        self.assertEqual(t._maintenance_depth, 0)

    async def test_cancel_while_waiting_for_readers_unlocks(self):
        t = self.t
        release = asyncio.Event()

        async def hold():
            async with t.reader():
                await release.wait()

        holder = asyncio.create_task(hold())
        await asyncio.sleep(0)

        async def maintain():
            async with t.maintenance("tracking_storage"):
                self.fail("entered while a reader was active")

        waiting = asyncio.create_task(maintain())
        await asyncio.sleep(.05)
        self.assertTrue(t.maintenance_active())
        self.assertEqual(self.app.maintenance_reason, "tracking_storage")
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        self.assertFalse(t.maintenance_active())
        self.assertIsNone(self.app.maintenance_reason)
        self.assertEqual(t._maintenance_depth, 0)
        self.assertEqual(t.storage_readers, 1)
        release.set()
        await holder
        async with t.maintenance("tracking_storage"):
            pass


class TestTwoTrackers(GuardTestCase):
    async def test_two_trackers_end_in_either_order(self):
        t, t2 = self.t, self.t2
        g1, g2 = t.maintenance("a"), t2.maintenance("b")
        await g1.__aenter__()
        await g2.__aenter__()
        self.assertEqual(self.app.maintenance_reason, "b")
        await g1.__aexit__(None, None, None)
        self.assertEqual(self.app.maintenance_reason, "b")
        self.assertFalse(t.maintenance_active())
        self.assertTrue(t2.maintenance_active())
        await g2.__aexit__(None, None, None)
        self.assertIsNone(self.app.maintenance_reason)
        g1, g2 = t.maintenance("a"), t2.maintenance("b")
        await g1.__aenter__()
        await g2.__aenter__()
        await g2.__aexit__(None, None, None)
        self.assertEqual(self.app.maintenance_reason, "a")
        self.assertTrue(t.maintenance_active())
        self.assertFalse(t2.maintenance_active())
        await g1.__aexit__(None, None, None)
        self.assertIsNone(self.app.maintenance_reason)
        self.assertEqual(self.app._maintenance, [])

    async def run_crossed(self, a_fails_first):
        t, t2 = self.t, self.t2
        stop_a, started_a, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def task_a():
            async with t.maintenance("a"):
                started_a.set()
                await stop_a.wait()
                raise OSError("flash")

        async def hold():
            async with t2.reader():
                await release.wait()

        async def task_b():
            async with t2.maintenance("b"):
                self.fail("entered while a reader was active")

        a = asyncio.create_task(task_a())
        await started_a.wait()
        holder = asyncio.create_task(hold())
        await asyncio.sleep(0)
        b = asyncio.create_task(task_b())
        await asyncio.sleep(.05)
        self.assertEqual(self.app.maintenance_reason, "b")
        if a_fails_first:
            stop_a.set()
            with self.assertRaises(OSError):
                await a
            self.assertEqual(self.app.maintenance_reason, "b")
            self.assertFalse(t.maintenance_active())
        b.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await b
        self.assertFalse(t2.maintenance_active())
        if not a_fails_first:
            self.assertEqual(self.app.maintenance_reason, "a")
            stop_a.set()
            with self.assertRaises(OSError):
                await a
        self.assertIsNone(self.app.maintenance_reason)
        self.assertEqual(self.app._maintenance, [])
        release.set()
        await holder
        self.assertEqual(t2.storage_readers, 0)

    async def test_two_trackers_with_exception_and_cancellation(self):
        await self.run_crossed(a_fails_first=False)

    async def test_two_trackers_exception_first_then_cancellation(self):
        await self.run_crossed(a_fails_first=True)


class TestQueries(GuardTestCase):
    async def test_queries(self):
        t = self.t
        self.assertIs(t.is_loaded(), True)
        self.assertIs(t.is_install_pending(), False)
        self.assertEqual(t.pending_count(), 0)
        self.assertIs(t.has_open_recording(), False)
        self.assertIs(t.maintenance_active(), False)
        self.assertIsNone(t.active_line())
        self.assertIsInstance(t.feature_limit(), int)
        self.open_line(t)
        self.assertIs(t.has_open_recording(), True)
        t.set_line_state("pause")
        self.assertIs(t.has_open_recording(), True)
        self.assertEqual(t.active_line()["name"], "L")
        t.set_line_state("finish")
        self.assertIs(t.has_open_recording(), False)
        t._pending_fixes.append({})
        self.assertEqual(t.pending_count(), 1)
        self.assertIs(t.has_open_recording(), True)
        t._install_pending = True
        self.assertIs(t.is_install_pending(), True)

    async def test_load_without_recovery_reads_the_survey(self):
        t = self.t
        self.open_line(t)
        for index in range(3):
            t.add_vertex(dict(FIX, lon=8 + index * .00002,
                              lon_e8=800000000 + index * 2000))
        t.set_line_state("finish")
        self.assertEqual(t.feature_count(), 3)
        t._sync_journal(close=True)
        t.point_store.close()
        other = Tracker(t.path, autoload=False, point_store_autoload=False)
        self.addCleanup(other.point_store.close)
        await other.load_without_recovery_async()
        self.assertEqual(other.feature_count(), 3)
        self.assertIs(other.is_loaded(), False)


class TestBackgroundCheckpoint(GuardTestCase):
    async def test_background_checkpoint_runs_as_maintenance_during_recording(self):
        t = self.t
        self.open_line(t)
        t._checkpoint_dirty_events = tracking.CHECKPOINT_BATCH_EVENTS
        stop, seen = asyncio.Event(), []

        async def probe():
            seen.append((self.app.maintenance_reason, t.maintenance_active()))
            stop.set()

        with mock.patch.object(t, "compact_async", side_effect=probe), \
                mock.patch.object(state, "shutdown_event", stop):
            await asyncio.wait_for(t.run_worker(), 5)
        self.assertEqual(seen, [("tracking_checkpoint", True)])
        self.assertIsNone(self.app.maintenance_reason)
        self.assertFalse(t.maintenance_active())
        self.assertEqual(t._checkpoint_dirty_events, 0)

    async def test_failed_background_checkpoint_releases_its_token(self):
        t = self.t
        self.open_line(t)
        t._checkpoint_dirty_events = tracking.CHECKPOINT_BATCH_EVENTS
        before = self.app.stats.get("tracking_write_errors", 0)
        stop = asyncio.Event()

        async def probe():
            stop.set()
            raise OSError("flash")

        with mock.patch.object(t, "compact_async", side_effect=probe), \
                mock.patch.object(state, "shutdown_event", stop):
            await asyncio.wait_for(t.run_worker(), 5)
        self.assertIsNone(self.app.maintenance_reason)
        self.assertEqual(self.app._maintenance, [])
        self.assertFalse(t.maintenance_active())
        self.assertEqual(self.app.stats["tracking_write_errors"], before + 1)

    async def test_no_background_checkpoint_before_the_survey_is_loaded(self):
        t = self.t
        t._loaded = False
        t._checkpoint_dirty_events = tracking.CHECKPOINT_BATCH_EVENTS
        self.app.stats["tracking_write_errors"] = 0
        stop = asyncio.Event()

        async def stop_later():
            await asyncio.sleep(.2)
            stop.set()

        with mock.patch.object(t, "compact_async") as compact, \
                mock.patch.object(state, "shutdown_event", stop):
            stopper = asyncio.create_task(stop_later())
            await asyncio.wait_for(t.run_worker(), 5)
            await stopper
        compact.assert_not_called()
        self.assertEqual(self.app.stats["tracking_write_errors"], 0)
        self.assertEqual(self.app._maintenance, [])


class TestModuleBoundary(unittest.TestCase):
    def firmware_files(self):
        for folder, dirs, files in os.walk(ROOT):
            dirs[:] = sorted(name for name in dirs
                             if not name.startswith(".") and name != "tests"
                             and name != "__pycache__")
            for name in sorted(files):
                if name.endswith(".py"):
                    yield os.path.join(folder, name)

    def test_recording_exception_is_used_once(self):
        found = []
        for path in self.firmware_files():
            with open(path, encoding="utf-8") as source:
                tree = ast.parse(source.read(), path)
            stack = []

            def visit(node):
                is_function = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                if is_function:
                    stack.append(node.name)
                if (isinstance(node, ast.keyword) and node.arg == "allow_recording" and
                        isinstance(node.value, ast.Constant) and node.value.value is True):
                    found.append((os.path.relpath(path, ROOT), tuple(stack)))
                for child in ast.iter_child_nodes(node):
                    visit(child)
                if is_function:
                    stack.pop()
            visit(tree)
        self.assertEqual(len(found), 1, found)
        self.assertEqual(found[0][0], os.path.join("tracking", "recording.py"))
        self.assertEqual(found[0][1][-1:], ("run_worker",))

    def test_measurement_does_not_import_web(self):
        with open(os.path.join(ROOT, "measurement.py"), encoding="utf-8") as source:
            tree = ast.parse(source.read(), "measurement.py")
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertNotIn("web", imported)
        self.assertNotIn("web_routing", imported)

    def test_web_no_longer_owns_the_measurement(self):
        with open(os.path.join(ROOT, "web.py"), encoding="utf-8") as source:
            tree = ast.parse(source.read(), "web.py")
        old_names = {"MEASUREMENT_PROGRESS", "averaged_current_fix", "_measurement_generation",
                     "cancel_measurement", "_stale_fix_error"}
        found, session_uses = [], 0
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                if node.id in old_names:
                    found.append((node.lineno, node.id))
                if node.id == "measurement_session":
                    session_uses += 1
            elif isinstance(node, ast.Attribute) and node.attr in old_names:
                found.append((node.lineno, node.attr))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in old_names:
                found.append((node.lineno, node.name))
            elif isinstance(node, ast.Global):
                found.extend((node.lineno, name) for name in node.names if name in old_names)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                found.extend((node.lineno, alias.name) for alias in node.names
                             if alias.name in old_names)
        # The string constant of the HTTP action "cancel_measurement" is not a name.
        self.assertEqual(found, [])
        self.assertGreaterEqual(session_uses, 1)

    def test_web_has_no_private_member_access(self):
        with open(os.path.join(ROOT, "web.py"), encoding="utf-8") as source:
            tree = ast.parse(source.read(), "web.py")
        old_storage_members = ("storage_busy", "storage_readers", "operation_progress",
                               "maintenance_reason", "begin_maintenance", "end_maintenance")
        found, old_members = [], []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            if (isinstance(node.value, ast.Name) and node.attr.startswith("_") and
                    not node.attr.startswith("__")):
                found.append("%s.%s:%d" % (node.value.id, node.attr, node.lineno))
            if node.attr in old_storage_members:
                old_members.append("%s:%d" % (node.attr, node.lineno))
        self.assertEqual([], found)
        self.assertEqual([], old_members)

    def test_benchmark_uses_only_its_own_private_state(self):
        with open(os.path.join(ROOT, "parallel_benchmark.py"), encoding="utf-8") as source:
            tree = ast.parse(source.read(), "parallel_benchmark.py")
        found = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and node.attr.startswith("_") and
                    not node.attr.startswith("__") and not node.attr.startswith("_parallel_")):
                found.append("%s:%d" % (node.attr, node.lineno))
        self.assertEqual([], found)


if __name__ == "__main__":
    unittest.main()
