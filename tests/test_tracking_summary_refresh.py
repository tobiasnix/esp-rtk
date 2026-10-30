# SPDX-License-Identifier: AGPL-3.0-only
"""A line summary an undo dropped is rebuilt cooperatively before status and append (#28c).

The undo itself keeps the cold state (no summary); GET /api/tracking,
diagnostics, compact, restore, the manual point and the worker append rebuild
it in batches of at most SUMMARY_SCAN_BATCH reads under a reader (or their own
maintenance) and answer busy instead of falling back to a synchronous scan.
"""
import asyncio
import hashlib
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

import support
from support import state, web
import measurement
import tracking
import test_tracking_summary_read_load as read_load
from test_measurement import FakeReceiver
from test_tracking_model import HeldGenerators


FIX = read_load.FIX
Writer = read_load.Writer
reference_summary = read_load.TestTrackingSummaryReadLoad.reference_summary
uasyncio = sys.modules["uasyncio"]
# Taken before any test patches it.
line_summary = tracking.Tracker._line_summary
CLOCK_UNIX = 1761552000
CLOCK_SECONDS = CLOCK_UNIX - 946684800
# SHA-256 of the backup, the NDJSON backup and the checkpoint file right after
# the undo of make_undone_line, determined at 08c5928 with both clocks and the
# ticks fixed (Ruling V1: the undo keeps today's bytes).
BACKUP_SHA256 = "0da19e932ecf8a8beb408f39300b425d6323ba9120518bff0b1606fb6b6d7dbb"
NDJSON_SHA256 = "accbd674212bb80764bcd3e8d4a39045ddc48c6ef2a5a5c39b5f7b72092bd1c5"
CHECKPOINT_SHA256 = "b8041e9672a979ca078396b065b320d04341e2a2816b8e60e9defc8b06a0da5b"


def answer(writer):
    head, _, body = bytes(writer.buf).partition(b"\r\n\r\n")
    return head.split(b"\r\n", 1)[0].decode(), json.loads(body)


def further(line, metres=0.5):
    """FIX moved metres east of the line's last vertex."""
    last = line["coordinates"][-1]
    lon_e8 = last[0] + int(round(metres / (111320.0 * 0.6427876) * 1e8))
    return dict(FIX, lat_e8=last[1], lon_e8=lon_e8, alt_mm=last[2], lon=lon_e8 / 1e8)


class Scan:
    """Counts point-store reads per scheduler turn of a heartbeat task.

    batches holds the reads seen between two heartbeat turns (turns without a
    read are left out); on_batch(scan) runs after each recorded batch.
    """

    def __init__(self, tracker, on_batch=None):
        self.tracker, self.on_batch = tracker, on_batch
        self.batches, self.reads, self.total = [], 0, 0

    async def __aenter__(self):
        original = self.tracker.point_store._decode

        def decode(value, include_measurement=True):
            self.reads += 1
            self.total += 1
            return original(value, include_measurement)

        self._patch = mock.patch.object(self.tracker.point_store, "_decode", side_effect=decode)
        self._patch.start()
        self._task = asyncio.create_task(self._beat())
        return self

    async def _beat(self):
        while True:
            if self.reads:
                self.batches.append(self.reads)
                self.reads = 0
                if self.on_batch is not None:
                    self.on_batch(self)
            await asyncio.sleep(0)

    async def __aexit__(self, exc_type, exc, tb):
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        if self.reads:
            self.batches.append(self.reads)
            self.reads = 0
        self._patch.stop()
        return False


class Fixture:
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory(prefix="esp-rtk-refresh-")
        self.addCleanup(self.tmp.cleanup)
        for patcher in (
                mock.patch.object(tracking.Tracker, "storage_capacity", return_value={
                    "free_flash_bytes": 8_000_000, "required_flash_reserve_bytes": 3_000_000,
                    "estimated_temporary_bytes": 16_384, "writable": True}),
                mock.patch.object(tracking.time, "time", return_value=CLOCK_UNIX),
                support.patch_module_global("tracking", "_now_seconds", return_value=CLOCK_SECONDS)):
            patcher.start()
            self.addCleanup(patcher.stop)
        # Counts every synchronous summary scan entry; tests may forbid it.
        summary_patch = mock.patch.object(
            tracking.Tracker, "_line_summary", autospec=True,
            side_effect=line_summary)
        self.line_summary = summary_patch.start()
        self.addCleanup(summary_patch.stop)

    def make_undone_line(self, name):
        """129 route vertices and one undo: 128 left, "_summary" not in line."""
        return read_load.TestTrackingSummaryReadLoad.make_undone_line(self, name)


class TestSummaryRefresh(Fixture, HeldGenerators, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Building 129 vertices blocks the loop on purpose; not a debug-mode finding here.
        asyncio.get_running_loop().slow_callback_duration = 1.0

    def cold(self, name, finish=False):
        tracker, line = self.make_undone_line(name)
        if finish:
            tracker.set_line_state("finish")
            self.assertFalse(tracker.has_open_recording())
        self.assertNotIn("_summary", line)
        self.hold(tracker, ("_iter_line_summary",))
        self.hold(tracker.point_store, ("iter_line",))
        coordinates, measurements = list(line["coordinates"]), list(line["measurements"])
        tracker.point_store.close_cache()
        self.line_summary.reset_mock()
        return tracker, line, reference_summary(line, coordinates, measurements)

    def forbid_line_summary(self):
        self.line_summary.side_effect = AssertionError("synchronous summary scan")

    async def request(self, tracker, method="GET", body=b"", path="/api/tracking"):
        writer = Writer()
        with mock.patch.object(web, "tracker", tracker), \
                mock.patch.object(web, "_admin_allowed", return_value=True):
            if path == "/api/diagnostics":
                await web._handle_diagnostics_request(writer, path, "", False, {})
            else:
                await web._handle_tracking_request(writer, path, method, "", body, False, {})
        return answer(writer)

    def post(self, tracker, payload):
        return self.request(tracker, "POST", json.dumps(payload).encode())

    async def hold_maintenance(self, tracker, entered, release, events=None, **options):
        async with tracker.maintenance("test", **options):
            if events is not None:
                events.append(("maintenance entered", tracker.storage_readers))
            entered.set()
            await release.wait()

    def released_readers(self, events):
        original = tracking._ReaderGuard.__aexit__

        async def aexit(guard, exc_type, exc, tb):
            result = await original(guard, exc_type, exc, tb)
            events.append(("reader released", guard._tracker.storage_readers))
            return result
        return mock.patch.object(tracking._ReaderGuard, "__aexit__", aexit)

    def assert_clean(self, tracker):
        self.assertEqual(tracker.storage_readers, 0)
        self.assertEqual(self.open_generators(), [])

    async def test_get_tracking_after_an_undo_rebuilds_in_bounded_batches(self):
        tracker, line, expected = self.cold("get", finish=True)
        async with Scan(tracker) as scan:
            status, body = await self.request(tracker)
        self.assertEqual(status, "HTTP/1.0 200 OK")
        self.assertLessEqual(max(scan.batches), tracking.SUMMARY_SCAN_BATCH)
        self.assertGreaterEqual(sum(scan.batches), 128)
        self.assertEqual(body["active_project_lines"][0], expected)
        self.assert_clean(tracker)
        async with Scan(tracker) as again:
            status, body = await self.request(tracker)
        self.assertEqual(status, "HTTP/1.0 200 OK")
        self.assertEqual(again.total, 0)
        self.assertEqual(body["active_project_lines"][0], expected)

    async def test_get_tracking_during_maintenance_with_a_stale_summary_answers_busy(self):
        tracker, line, _expected = self.cold("busy", finish=True)
        entered, release = asyncio.Event(), asyncio.Event()
        holder = asyncio.create_task(self.hold_maintenance(tracker, entered, release))
        await asyncio.wait_for(entered.wait(), 5)
        try:
            async with Scan(tracker) as scan:
                status, body = await self.request(tracker)
        finally:
            release.set()
            await holder
        self.assertEqual(status, "HTTP/1.0 503 Service Unavailable")
        self.assertEqual(body["error"], "storage_busy")
        self.assertEqual(scan.total, 0)
        self.assertEqual(self.line_summary.call_count, 0)
        self.assertNotIn("_summary", line)

    async def test_get_tracking_during_maintenance_with_current_summaries_answers_as_today(self):
        tracker, line, expected = self.cold("warm", finish=True)
        tracker.status()  # the synchronous tool path warms the cache
        status, today = await self.request(tracker)
        self.assertEqual(status, "HTTP/1.0 200 OK")
        entered, release = asyncio.Event(), asyncio.Event()
        holder = asyncio.create_task(self.hold_maintenance(tracker, entered, release))
        await asyncio.wait_for(entered.wait(), 5)
        try:
            async with Scan(tracker) as scan:
                status, body = await self.request(tracker)
        finally:
            release.set()
            await holder
        self.assertEqual(status, "HTTP/1.0 200 OK")
        self.assertEqual(scan.total, 0)
        self.assertEqual(body, today)
        self.assertEqual(body["active_project_lines"][0], expected)

    async def test_maintenance_started_during_the_refresh_wins(self):
        tracker, line, _expected = self.cold("wins", finish=True)
        events, tasks = [], []
        entered, release = asyncio.Event(), asyncio.Event()

        def start_maintenance(scan):
            if not tasks:
                tasks.append(asyncio.create_task(
                    self.hold_maintenance(tracker, entered, release, events)))

        with self.released_readers(events):
            async with Scan(tracker, start_maintenance) as scan:
                status, body = await self.request(tracker)
                await asyncio.wait_for(entered.wait(), 5)
            release.set()
            await asyncio.wait_for(tasks[0], 5)
        self.assertEqual(status, "HTTP/1.0 503 Service Unavailable")
        self.assertEqual(body["error"], "storage_busy")
        self.assertEqual(events, [("reader released", 0), ("maintenance entered", 0)])
        self.assertLessEqual(max(scan.batches), tracking.SUMMARY_SCAN_BATCH)
        self.assert_clean(tracker)
        self.assertNotIn("_summary", line)

    async def test_cancelled_refresh_releases_the_reader(self):
        tracker, line, _expected = self.cold("cancel", finish=True)
        tasks = []

        def cancel(scan):
            tasks[0].cancel()

        async with Scan(tracker, cancel):
            tasks.append(asyncio.create_task(self.request(tracker)))
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(tasks[0], 5)
        self.assert_clean(tracker)
        self.assertNotIn("_summary", line)
        sleeps = []
        real_sleep = asyncio.sleep

        async def sleep_ms(ms):
            sleeps.append(ms)
            await real_sleep(0)

        with mock.patch.object(uasyncio, "sleep_ms", sleep_ms):
            async with tracker.maintenance("test") as job:
                self.assertTrue(job.entered)
        self.assertEqual(sleeps, [])

    async def test_compact_and_restore_refresh_under_their_own_maintenance(self):
        for action in ("compact", "restore"):
            with self.subTest(action=action):
                tracker, line, expected = self.cold(action, finish=True)
                payload = {"action": action}
                if action == "restore":
                    payload["backup"] = tracker.backup()
                reads_in_status = []
                def counted(owner, summarized):
                    before = self.scan.total
                    result = line_summary(owner, summarized)
                    reads_in_status.append(self.scan.total - before)
                    return result

                self.line_summary.side_effect = counted
                async with Scan(tracker) as self.scan:
                    status, body = await self.post(tracker, payload)
                self.assertEqual(status, "HTTP/1.0 200 OK", body)
                self.assertEqual(body["active_project_lines"][0], expected)
                if action == "compact":
                    self.assertGreaterEqual(sum(self.scan.batches), 128)
                    self.assertGreater(self.line_summary.call_count, 0)
                # A restore appends its vertices one by one and leaves no summary
                # stale; the batches then stay empty.
                self.assertLessEqual(max(self.scan.batches, default=0),
                                     tracking.SUMMARY_SCAN_BATCH)
                self.assertEqual(reads_in_status, [0] * self.line_summary.call_count)
                self.assertEqual(tracker.storage_readers, 0)
                self.line_summary.side_effect = line_summary
        stale = self.cold("refused", finish=True)[0]
        other = self.make_undone_line("other")[0]
        async with other.maintenance("other", allow_recording=True) as foreign:
            with self.assertRaises(tracking.StorageBusy) as caught:
                await stale.refresh_summaries_async(owner=foreign)
            self.assertEqual(caught.exception.code, "storage_busy")
        with self.assertRaises(tracking.StorageBusy) as caught:
            await stale.refresh_summaries_async(owner=stale.maintenance("not entered"))
        self.assertEqual(caught.exception.code, "storage_busy")

    async def test_diagnostics_after_an_undo_rebuilds_in_bounded_batches(self):
        tracker, line, expected = self.cold("diagnostics", finish=True)
        async with Scan(tracker) as scan:
            status, body = await self.request(tracker, path="/api/diagnostics")
        self.assertEqual(status, "HTTP/1.0 200 OK")
        self.assertLessEqual(max(scan.batches), tracking.SUMMARY_SCAN_BATCH)
        self.assertEqual(body["tracking"]["active_project_lines"][0], expected)
        self.assertEqual(tracker.storage_readers, 0)
        tracker, line, _expected = self.cold("diagnostics-busy", finish=True)
        entered, release = asyncio.Event(), asyncio.Event()
        holder = asyncio.create_task(self.hold_maintenance(tracker, entered, release))
        await asyncio.wait_for(entered.wait(), 5)
        try:
            status, body = await self.request(tracker, path="/api/diagnostics")
        finally:
            release.set()
            await holder
        self.assertEqual(status, "HTTP/1.0 503 Service Unavailable")
        self.assertEqual(body["error"], "storage_busy")
        self.assertNotIn("_summary", line)

    async def run_worker_until(self, tracker, done, timeout=10.0):
        stop = [False]
        fake = mock.Mock()
        fake.is_set.side_effect = lambda: stop[0]
        with mock.patch.object(state, "shutdown_event", fake):
            worker = asyncio.create_task(tracker.run_worker())
            try:
                for _round in range(int(timeout / 0.005)):
                    if done():
                        break
                    await asyncio.sleep(0.005)
            finally:
                stop[0] = True
                await asyncio.wait_for(worker, 5)
        self.assertTrue(done(), "the worker did not finish in time")

    def assert_rebuilt_over_129(self, tracker, line):
        self.assertEqual(self.line_summary.call_count, 0)
        self.assertEqual(line["_summary"]["vertex_count"], 129)
        self.assertEqual(tracker._line_lengths[line["id"]], line["_summary"]["length_m"])
        self.line_summary.side_effect = line_summary
        self.assertEqual(tracker._line_summary(line), reference_summary(
            line, list(line["coordinates"]), list(line["measurements"])))

    async def test_worker_append_after_an_undo_rebuilds_cooperatively(self):
        tracker, line, _expected = self.cold("worker")
        self.assertTrue(tracker.enqueue_fix(further(line)))
        tracker.point_store.close_cache()
        async with Scan(tracker) as scan:
            await self.run_worker_until(tracker, lambda: len(line["coordinates"]) == 129)
        self.assertLessEqual(max(scan.batches), tracking.SUMMARY_SCAN_BATCH)
        self.assertEqual(tracker.storage_readers, 0)
        self.assert_rebuilt_over_129(tracker, line)

    def logged_sleeps(self, log):
        real_sleep = asyncio.sleep

        async def sleep_ms(ms):
            log.append(("sleep", ms))
            await real_sleep(0)
        return mock.patch.object(uasyncio, "sleep_ms", sleep_ms)

    def logged_refresh(self, tracker, log):
        original = tracker.refresh_summaries_async

        async def refresh(*args, **kwargs):
            log.append(("refresh", len(tracker._pending_fixes), len(tracker._line(
                tracker._project())["coordinates"])))
            result = await original(*args, **kwargs)
            log.append(("refreshed", result))
            return result
        return mock.patch.object(tracker, "refresh_summaries_async", refresh)

    def assert_paused_between_attempts(self, log):
        attempts = [index for index, item in enumerate(log) if item[0] == "refresh"]
        self.assertGreaterEqual(len(attempts), 2)
        for first, second in zip(attempts, attempts[1:]):
            self.assertTrue(any(item[0] == "sleep" and item[1] >= 50
                                for item in log[first:second]), log[first:second])

    async def test_worker_waits_out_maintenance_that_interrupts_the_refresh(self):
        tracker, line, _expected = self.cold("worker-busy")
        self.assertTrue(tracker.enqueue_fix(further(line)))
        pending = list(tracker._pending_fixes)
        tracker.point_store.close_cache()
        log, tasks, during = [], [], []

        async def maintain():
            async with tracker.maintenance("test", allow_recording=True):
                log.append(("maintenance entered",))
                start = len(log)
                while sum(1 for item in log[start:] if item == ("sleep", 50)) < 3:
                    during.append((len(line["coordinates"]), list(tracker._pending_fixes)))
                    await asyncio.sleep(0)
            log.append(("maintenance left",))

        def start_maintenance(scan):
            if not tasks:
                tasks.append(asyncio.create_task(maintain()))

        add_vertex = tracker.add_vertex

        def logged_add(*args, **kwargs):
            log.append(("add_vertex",))
            return add_vertex(*args, **kwargs)

        with self.logged_sleeps(log), self.logged_refresh(tracker, log), \
                mock.patch.object(tracker, "add_vertex", logged_add):
            async with Scan(tracker, start_maintenance) as scan:
                await self.run_worker_until(tracker, lambda: len(line["coordinates"]) == 129)
            await asyncio.wait_for(tasks[0], 5)
        self.assertTrue(during)
        self.assertEqual(during, [(128, pending)] * len(during))
        names = [item[0] for item in log]
        self.assertLess(names.index("maintenance left"), names.index("add_vertex"))
        self.assertEqual(names.count("add_vertex"), 1)
        self.assertEqual(log[names.index("refreshed")], ("refreshed", False))
        self.assert_paused_between_attempts(log)
        self.assertLessEqual(max(scan.batches), tracking.SUMMARY_SCAN_BATCH)
        self.assertEqual(tracker.storage_readers, 0)
        self.assert_rebuilt_over_129(tracker, line)

    async def test_manual_point_after_an_undo_rebuilds_cooperatively(self):
        tracker, line, _expected = self.cold("manual")
        fix = further(line)
        tracker.point_store.close_cache()

        async def collect(samples):
            return dict(fix)

        with mock.patch.object(web.measurement_session, "collect", collect):
            async with Scan(tracker) as scan:
                status, body = await self.post(tracker, {"action": "add_vertex"})
        self.assertEqual(status, "HTTP/1.0 200 OK", body)
        self.assertLessEqual(max(scan.batches), tracking.SUMMARY_SCAN_BATCH)
        self.assertEqual(len(line["coordinates"]), 129)
        self.assert_rebuilt_over_129(tracker, line)

    async def test_cancelled_or_restarted_measurement_during_the_refresh_stores_no_point(self):
        for variant in ("cancel", "restart"):
            with self.subTest(variant=variant):
                tracker, line, _expected = self.cold("measurement-" + variant)
                session = measurement.MeasurementSession(FakeReceiver(last_fix=further(line)))
                tracker.point_store.close_cache()
                sequence = tracker._event_sequence
                tasks = []

                def interrupt(scan):
                    if tasks:
                        return
                    if variant == "cancel":
                        tasks.append(asyncio.create_task(
                            self.post(tracker, {"action": "cancel_measurement"})))
                    else:
                        tasks.append(asyncio.create_task(session.collect(1)))

                with mock.patch.object(web, "measurement_session", session):
                    async with Scan(tracker, interrupt):
                        status, body = await self.post(tracker, {"action": "add_vertex"})
                    self.assertTrue(tasks)
                    await asyncio.wait_for(tasks[0], 5)
                self.assertEqual(status, "HTTP/1.0 400 Bad Request", body)
                self.assertEqual(body["error"], "invalid_tracking_request")
                self.assertEqual(body["message"], "Measurement cancelled.")
                self.assertEqual(len(line["coordinates"]), 128)
                self.assertEqual(tracker._event_sequence, sequence)
                self.assertEqual(tracker.storage_readers, 0)

    def rename_after_first_batch(self, tracker, passes):
        """A heartbeat hook renaming the project after the first batch of the given passes."""
        renamed = []

        def rename(scan):
            # Pass p reads vertices 128 * (p - 1) + 1 ... 128 * p.
            if scan.total % 128 == 64 and scan.total // 128 < passes:
                renamed.append(scan.total)
                tracker.rename_project(tracker.active_project_id, "Renamed %d" % len(renamed))
        return rename, renamed

    async def test_event_during_refresh_is_not_cached_and_the_second_pass_completes(self):
        tracker, line, expected = self.cold("event")
        self.forbid_line_summary()
        rename, renamed = self.rename_after_first_batch(tracker, 1)
        async with Scan(tracker, rename) as scan:
            self.assertTrue(await tracker.refresh_summaries_async())
        self.assertEqual(renamed, [64])
        self.assertEqual(line["_summary"]["vertex_count"], 128)
        self.assertGreaterEqual(scan.total, 2 * 128)
        self.assertLessEqual(max(scan.batches), tracking.SUMMARY_SCAN_BATCH)
        self.assert_clean(tracker)
        self.line_summary.side_effect = line_summary
        self.assertEqual(tracker._line_summary(line), expected)

    async def test_two_failed_passes_answer_busy_and_the_worker_retries(self):
        tracker, line, _expected = self.cold("passes-get", finish=True)
        self.forbid_line_summary()
        rename, renamed = self.rename_after_first_batch(tracker, tracking.SUMMARY_REFRESH_PASSES)
        async with Scan(tracker, rename) as scan:
            status, body = await self.request(tracker)
        self.assertEqual(status, "HTTP/1.0 503 Service Unavailable")
        self.assertEqual(body["error"], "storage_busy")
        self.assertEqual(renamed, [64, 192])
        self.assertEqual(scan.total, tracking.SUMMARY_REFRESH_PASSES * 128)
        self.assertNotIn("_summary", line)
        self.assert_clean(tracker)

        tracker, line, _expected = self.cold("passes-worker")
        self.assertTrue(tracker.enqueue_fix(further(line)))
        pending = list(tracker._pending_fixes)
        tracker.point_store.close_cache()
        self.forbid_line_summary()
        rename, renamed = self.rename_after_first_batch(tracker, tracking.SUMMARY_REFRESH_PASSES)
        log = []
        with self.logged_sleeps(log), self.logged_refresh(tracker, log):
            async with Scan(tracker, rename) as scan:
                await self.run_worker_until(tracker, lambda: len(line["coordinates"]) == 129)
        self.assertEqual(renamed, [64, 192])
        refreshes = [item for item in log if item[0] in ("refresh", "refreshed")]
        self.assertEqual(refreshes[:4], [("refresh", 1, 128), ("refreshed", False),
                                         ("refresh", 1, 128), ("refreshed", True)])
        first_failure = log.index(("refreshed", False))
        self.assertEqual(log[first_failure + 1], ("sleep", 50))
        self.assertEqual(tracker._pending_fixes, [])
        self.assertTrue(pending)
        self.assert_paused_between_attempts(log)
        self.assertLessEqual(max(scan.batches), tracking.SUMMARY_SCAN_BATCH)
        self.assertEqual(tracker.storage_readers, 0)
        self.assert_rebuilt_over_129(tracker, line)

    async def test_status_is_unchanged_by_the_refresh(self):
        results = []
        for name, refresh in (("refreshed", True), ("synchronous", False)):
            # The inventory epoch (#21c) is random per tracker; both runs use one value.
            with mock.patch.object(tracking.time, "ticks_ms", return_value=5000), \
                    mock.patch.object(tracking.time, "ticks_us", return_value=5000000,
                                      create=True), \
                    support.patch_module_global("tracking", "_new_inventory_epoch", return_value="0badcafe"):
                tracker, line, _expected = self.cold(name)
                if refresh:
                    self.assertTrue(await tracker.refresh_summaries_async())
                    self.assertEqual(self.line_summary.call_count, 0)
                results.append((json.dumps(tracker.status(), sort_keys=True),
                                json.dumps(tracker.live_status(), sort_keys=True),
                                "".join(tracker.iter_geojson(tracker.active_project_id))))
        self.assertEqual(results[0], results[1])

    async def test_an_empty_line_needs_no_reader(self):
        # A line without vertices has nothing to read: its summary is built
        # without a reader, so a status during maintenance answers as today.
        tracker, line, _expected = self.cold("empty", finish=True)
        self.assertTrue(await tracker.refresh_summaries_async())
        empty = tracker.start_line("Empty", 0.2, recording_mode="route")
        self.assertNotIn("_summary", empty)
        entered, release = asyncio.Event(), asyncio.Event()
        holder = asyncio.create_task(self.hold_maintenance(
            tracker, entered, release, allow_recording=True))
        await asyncio.wait_for(entered.wait(), 5)
        try:
            async with Scan(tracker) as scan:
                status, body = await self.request(tracker)
        finally:
            release.set()
            await holder
        self.assertEqual(status, "HTTP/1.0 200 OK")
        # The one read is status() taking the latest measurement of the first line.
        self.assertEqual(scan.total, 1)
        self.assertEqual(self.line_summary.call_count, 2)
        self.assertEqual(body["active_project_lines"][1]["vertex_count"], 0)


class TestUndoBytes(Fixture, unittest.TestCase):
    def test_backup_and_checkpoint_right_after_undo_match_08c5928(self):
        with mock.patch.object(tracking.time, "ticks_ms", return_value=5000), \
                mock.patch.object(tracking.time, "ticks_us", return_value=5000000,
                                      create=True):
            tracker, line = self.make_undone_line("bytes")
            self.assertNotIn("_summary", line)
            backup = hashlib.sha256(json.dumps(tracker.backup(), sort_keys=True).encode())
            ndjson = hashlib.sha256("".join(tracker.iter_backup_ndjson()).encode())
            tracker.compact()
            with open(tracker.checkpoint_path, "rb") as source:
                checkpoint = hashlib.sha256(source.read())
        self.assertNotIn("_summary", line)
        self.assertEqual((backup.hexdigest(), ndjson.hexdigest(), checkpoint.hexdigest()),
                         (BACKUP_SHA256, NDJSON_SHA256, CHECKPOINT_SHA256))
        self.assertEqual(self.line_summary.call_count, 0)


if __name__ == "__main__":
    unittest.main()
