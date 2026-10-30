# SPDX-License-Identifier: AGPL-3.0-only
"""The synthetic export cases, the byte pins and the sender of the GeoJSON export (#42)."""
import asyncio
import json
import os
import tempfile
import time
import unittest
from unittest import mock

import support  # noqa: F401; MicroPython modules on the host.
from support import web
import benchmark_export
import export_case
import tracking
from pointstore import FlashPointStore
from test_tracking_model import HeldGenerators


class ExportCaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Two complete runs under different outer clocks; the byte pins reuse
        # the first run so the module stays well below 20 s on the host.
        cls.runs = []
        for wall, ticks in ((1_700_000_000, 0), (1_900_000_000, 10 ** 9)):
            with mock.patch("time.time", return_value=wall), \
                    mock.patch.object(time, "ticks_ms", return_value=ticks, create=True):
                cls.runs.append(export_case.golden_entries())

    @staticmethod
    def golden():
        with open(export_case.GOLDEN_PATH, encoding="utf-8") as source:
            return json.load(source)

    def export(self, case):
        with tempfile.TemporaryDirectory(prefix="esp-rtk-export-case-") as directory:
            tracker = export_case.build(directory, export_case.large_export_payload(**case))
            try:
                with export_case.fixed_clock():
                    raw = "".join(tracker.iter_geojson(export_case.PROJECT_ID))
            finally:
                export_case.close(tracker)
        return raw

    def test_large_case_is_at_least_the_device_case(self):
        large = export_case.LARGE
        self.assertEqual(large["lines"] * large["vertices_per_line"] + large["points"], 3531)
        raw = self.export(large)
        self.assertGreaterEqual(len(raw.encode("utf-8")), 3_701_233)
        features = json.loads(raw)["features"]
        self.assertEqual(len(features), 4 + 131)
        self.assertEqual([len(feature["geometry"]["coordinates"]) for feature in features[:4]],
                         [850] * 4)
        long_features = json.loads(self.export(export_case.LONG))["features"]
        self.assertEqual(len(long_features), 1)
        self.assertEqual(len(long_features[0]["geometry"]["coordinates"]), 3400)

    def test_golden_cases_do_not_depend_on_the_clock(self):
        first, second = self.runs
        self.assertEqual(first, second)
        self.assertEqual(first["assigned"], self.golden()["assigned"])

    def test_geojson_bytes_match_the_pinned_export(self):
        golden = self.golden()
        self.assertEqual(sorted(golden),
                         ["assigned", "large", "long", "mixed", "mixed_stale_summary"])
        for case in sorted(golden):
            with self.subTest(case=case):
                self.assertEqual(self.runs[0][case], golden[case])


class ExportReadTests(unittest.TestCase):
    """How the GeoJSON export reads the flash in the large and the long case (E1, E17)."""
    CASES = {"large": export_case.LARGE, "long": export_case.LONG}

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.TemporaryDirectory(prefix="esp-rtk-export-read-")
        cls.trackers = {}
        for name, case in cls.CASES.items():
            directory = os.path.join(cls.root.name, name)
            os.mkdir(directory)
            cls.trackers[name] = export_case.build(
                directory, export_case.large_export_payload(**case))

    @classmethod
    def tearDownClass(cls):
        for tracker in cls.trackers.values():
            export_case.close(tracker)
        cls.root.cleanup()

    def test_geojson_reads_each_segment_once_per_pass(self):
        for name, case in self.CASES.items():
            with self.subTest(case=name):
                tracker = self.trackers[name]
                tracker.point_store.close_cache()
                decode, decodes = FlashPointStore._decode, [0]

                def counted(store, value, include_measurement=True):
                    decodes[0] += 1
                    return decode(store, value, include_measurement)

                with mock.patch.object(FlashPointStore, "_decode", autospec=True,
                                       side_effect=counted), \
                        mock.patch("pointstore.open", wraps=open, create=True) as opened, \
                        export_case.fixed_clock():
                    for _chunk in tracker.iter_geojson(export_case.PROJECT_ID):
                        pass
                lines, vertices = case["lines"], case["lines"] * case["vertices_per_line"]
                self.assertLessEqual(opened.call_count,
                                     2 * len(tracker.point_store.segments) + 2 * lines)
                self.assertEqual(decodes[0], 2 * vertices)

    def test_geojson_heap_peak_stays_at_backup_level(self):
        for name in self.CASES:
            with self.subTest(case=name):
                tracker = self.trackers[name]
                geojson = benchmark_export.measure(tracker, export_case.PROJECT_ID, "geojson")
                backup = benchmark_export.measure(tracker, export_case.PROJECT_ID, "backup")
                self.assertLessEqual(geojson["tracemalloc_peak_bytes"],
                                     1.25 * backup["tracemalloc_peak_bytes"])


class _Writer:
    """A client socket; drain number fail_at (1-based) runs failure()."""

    def __init__(self, fail_at=None, failure=None):
        self.buf = bytearray()
        self.drains = 0
        self.fail_at, self.failure = fail_at, failure

    def write(self, value):
        self.buf.extend(value)

    async def drain(self):
        self.drains += 1
        if self.drains == self.fail_at:
            await self.failure()

    def body(self):
        return bytes(self.buf).split(b"\r\n\r\n", 1)[1]


class GeojsonSenderTests(HeldGenerators, unittest.IsolatedAsyncioTestCase):
    """Time pacing, the drain deadline and the log line of the GeoJSON sender (E4, E5, E18)."""

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.TemporaryDirectory(prefix="esp-rtk-export-sender-")
        cls.tracker = export_case.build(cls.root.name, export_case.large_export_payload(
            lines=2, vertices_per_line=40, points=3))

    @classmethod
    def tearDownClass(cls):
        export_case.close(cls.tracker)
        cls.root.cleanup()

    def setUp(self):
        super().setUp()
        for patch in (mock.patch.object(web, "tracker", self.tracker),
                      mock.patch.object(web, "_admin_allowed", return_value=True),
                      mock.patch.object(web, "log")):
            self.logged = patch.start()
            self.addCleanup(patch.stop)

    def export_lines(self):
        return [call.args[2] for call in self.logged.call_args_list
                if call.args[:2] == ("INFO", "EXPORT")]

    async def export(self, writer, method="GET"):
        await web._handle_tracking_request(writer, "/api/tracking/export", method, "", b"",
                                           False, {})

    async def interrupted_export(self, writer, run):
        """run(export) with every generator and point file held; returns the files."""
        self.hold(tracking.Tracker, ("_iter_geojson_line", "_line_rows"))
        self.hold(FlashPointStore, ("iter_locators",))
        handles = []

        def open_point_file(*args, **kwargs):
            handle = open(*args, **kwargs)
            handles.append(handle)
            return handle

        with mock.patch("pointstore.open", side_effect=open_point_file, create=True):
            await run(self.export(writer))
        return handles

    def assert_released(self, handles, error_name):
        self.assertEqual(self.tracker.storage_readers, 0)
        self.assertEqual({name for name, _generator in self.held},
                         {"_iter_geojson_line", "_line_rows", "iter_locators"})
        self.assertEqual(self.open_generators(), [])
        self.assertTrue(handles)
        self.assertTrue(all(handle.closed for handle in handles))
        lines = self.export_lines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("geojson interrupted " + error_name), lines[0])

    async def test_paced_sender_yields_by_time_even_with_frequent_drains(self):
        clock, sleeps, produced = [0], [], [0]

        def chunks():
            for _ in range(200):
                clock[0] += 5
                produced[0] += 1
                yield "x" * 1100

        async def sleep_ms(ms):
            sleeps.append((ms, produced[0]))
            await asyncio.sleep(0)

        writer, report = _Writer(), {}
        with mock.patch.object(web.time, "ticks_ms", new=lambda: clock[0]), \
                mock.patch.object(web.asyncio, "sleep_ms", side_effect=sleep_ms):
            await web._send_tracking_json_stream(writer, chunks(), "GET", first="[",
                                                 pace_ms=20, report=report)
        # Every chunk was written and drained on its own.
        self.assertGreaterEqual(writer.drains, 200)
        own = [at for ms, at in sleeps if ms == 0]
        self.assertEqual(len(own), len(sleeps))
        self.assertTrue(35 <= len(own) <= 45, len(own))
        self.assertLessEqual(max(b - a for a, b in zip([0] + own, own + [200])), 5)
        self.assertEqual(report["yields"], len(own))
        self.assertEqual(report["bytes"], 200 * 1100 + len("["))
        self.assertEqual(len(writer.body()), report["bytes"])

    async def test_complete_geojson_export_logs_one_line(self):
        writer = _Writer()
        await self.export(writer)
        body = writer.body()
        self.assertEqual(json.loads(body)["type"], "FeatureCollection")
        lines = self.export_lines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("geojson complete bytes=%d " % len(body)), lines[0])
        for field in ("read_ms=", "decode_ms=", "produce_ms=", "drain_ms=", "yield_ms="):
            self.assertIn(field, lines[0])
        self.assertIn("ram_free_min=8300000", lines[0])
        self.assertEqual(self.logged.call_count, 1)

    async def test_client_gone_during_geojson_export_releases_the_reader_and_logs(self):
        async def reset():
            raise OSError(104)   # as MicroPython raises it; no errno subclass

        async def run(export):
            with self.assertRaises(OSError):
                await export

        handles = await self.interrupted_export(_Writer(3, reset), run)
        self.assert_released(handles, "OSError")
        writer = _Writer()
        await self.export(writer)
        self.assertTrue(bytes(writer.buf).startswith(b"HTTP/1.0 200 OK"))
        json.loads(writer.body())

    async def test_cancel_during_a_blocked_drain_releases_everything(self):
        blocked, never = asyncio.Event(), asyncio.Event()

        async def hang():
            blocked.set()
            await never.wait()

        async def run(export):
            task = asyncio.create_task(export)
            await asyncio.wait_for(blocked.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        handles = await self.interrupted_export(_Writer(3, hang), run)
        self.assert_released(handles, "CancelledError")
        writer = _Writer()
        await asyncio.wait_for(web._handle_tracking_request(
            writer, "/api/tracking", "POST", "", b'{"action":"compact"}', False, {}), 2)
        self.assertTrue(bytes(writer.buf).startswith(b"HTTP/1.0 200 OK"), bytes(writer.buf)[:80])
        self.assertFalse(self.tracker.maintenance_active())

    async def test_a_blocked_drain_ends_the_export_after_the_deadline(self):
        async def hang():
            await asyncio.Event().wait()

        async def run(export):
            with self.assertRaises(asyncio.TimeoutError):
                # The test's own bound; the export must end by its drain deadline.
                await asyncio.wait_for(export, 2)

        with mock.patch.object(web, "EXPORT_DRAIN_TIMEOUT_SEC", 0.05):
            handles = await self.interrupted_export(_Writer(3, hang), run)
        self.assert_released(handles, "TimeoutError")

    async def test_head_geojson_export_sends_headers_only_without_a_log_line(self):
        writer = _Writer()
        await self.export(writer, "HEAD")
        self.assertTrue(bytes(writer.buf).startswith(b"HTTP/1.0 200 OK"))
        self.assertTrue(bytes(writer.buf).endswith(b"\r\n\r\n"))
        self.assertIn(b"application/geo+json", writer.buf)
        self.logged.assert_not_called()

    async def test_map_and_line_streams_keep_the_chunk_rule(self):
        sleeps = []

        async def sleep_ms(ms):
            sleeps.append(ms)
            await asyncio.sleep(0)

        writer = _Writer()
        with mock.patch.object(web.asyncio, "sleep_ms", side_effect=sleep_ms), \
                mock.patch.object(web.asyncio, "wait_for", wraps=asyncio.wait_for) as bounded:
            await web._send_tracking_json_stream(writer, iter(["1,"] * 12), "GET", first="[")
        self.assertEqual(sleeps, [1, 1, 1])
        bounded.assert_not_called()
        self.assertEqual(writer.body(), b"[" + b"1," * 12)


if __name__ == "__main__":
    unittest.main()
