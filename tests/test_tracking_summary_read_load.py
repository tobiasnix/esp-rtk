# SPDX-License-Identifier: AGPL-3.0-only
"""Cold line summaries must stay cooperative after an ordinary undo."""
import asyncio
import copy
import json
import os
import tempfile
import unittest
from unittest import mock

from support import web
from pointstore import FlashSequence
import tracking


FIX = {"lat": 50.0, "lon": 8.0, "alt": 123.4, "qual": 4,
       "fix_status_text": "RTK_FIXED", "sats": 24, "hdop": 0.6,
       "correction_age_sec": 0.5, "station_id": "0054",
       "receiver_accuracy": {"source": "NMEA_GST", "semi_major_sigma_m": 0.02,
                             "altitude_sigma_m": 0.04}}


class Writer:
    def __init__(self): self.buf = bytearray()
    def write(self, value): self.buf.extend(value)
    async def drain(self): pass  # A writable MicroPython socket may not yield.


class TestTrackingSummaryReadLoad(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="esp-rtk-summary-")
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(tracking.Tracker, "storage_capacity", return_value={
            "free_flash_bytes": 8_000_000, "required_flash_reserve_bytes": 3_000_000,
            "estimated_temporary_bytes": 16_384, "writable": True})
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_undone_line(self, name):
        tracker = tracking.Tracker(os.path.join(self.tmp.name, name))
        self.addCleanup(tracker.point_store.close)
        self.addCleanup(tracker._sync_journal, True)
        tracker.create_project("Cold summary")
        line = tracker.start_line("Route", 0.2, recording_mode="route")
        previous_lon = FIX["lon"]
        for index in range(129):
            lon = FIX["lon"] + index * 0.00002
            if index % 11 == 0:
                lon += 0.0001
            if index % 7 == 0 and index:
                lon = previous_lon
            fix = dict(FIX, lon=lon, alt=FIX["alt"] + (index % 3) * 0.8)
            if index % 13 == 0:
                fix.update(qual=1, fix_status_text="GPS_FIX", receiver_accuracy=None)
            tracker.add_vertex(fix, "automatic")
            previous_lon = lon
        tracker.undo_last()
        self.assertNotIn("_summary", line)
        self.assertEqual(len(line["coordinates"]), 128)
        return tracker, line

    @staticmethod
    def reference_summary(line, coordinates, measurements):
        gaps = [tracking._distance_m(a, b) for a, b in zip(coordinates, coordinates[1:])]
        steps = [abs(b[2] - a[2]) for a, b in zip(coordinates, coordinates[1:])]
        length = sum(gaps)
        warnings = []
        if len(coordinates) < 2: warnings.append("not_enough_line_points")
        if any(gap < 0.05 for gap in gaps): warnings.append("overlapping_line_points")
        if len(coordinates) >= 2 and length < 0.05:
            warnings.append("line_has_no_horizontal_length")
        if any(gap > tracking.MAX_LINE_GAP_M for gap in gaps):
            warnings.append("large_line_gaps")
        if any(step > tracking.MAX_VERTICAL_STEP_M for step in steps):
            warnings.append("large_vertical_steps")
        return {"id": line["id"], "name": line["name"], "state": line["state"],
            "created_at": line.get("created_at"), "finished_at": line.get("finished_at"),
            "recording_mode": line["recording_mode"], "vertex_count": len(coordinates),
            "length_m": round(length, 3), "quality_accepted_count": sum(
                bool((item.get("quality") or {}).get("accepted")) for item in measurements),
            "quality_total_count": len(measurements), "warnings": warnings,
            "large_gap_count": sum(gap > tracking.MAX_LINE_GAP_M for gap in gaps),
            "max_gap_m": round(max(gaps, default=0.0), 3),
            "large_vertical_step_count": sum(
                step > tracking.MAX_VERTICAL_STEP_M for step in steps),
            "max_vertical_step_m": round(max(steps, default=0.0), 3),
            "finish_diagnostics": line.get("finish_diagnostics")}

    def reference_detail(self, tracker, line, coordinates, measurements, offset, limit):
        result = self.reference_summary(line, coordinates, measurements)
        end = min(len(coordinates), len(measurements), offset + limit)
        vertices = []
        for index in range(offset, end):
            coordinate, value = coordinates[index], measurements[index]
            previous = coordinates[index - 1] if index else None
            quality = copy.deepcopy(value.get("quality") or {})
            profile = tracker.profiles.get(value.get("profile_id"))
            if profile:
                quality["limits"] = copy.deepcopy(profile["limits"])
            vertex = {"number": index + 1, "coordinate": coordinate,
                "segment_length_m": round(tracking._distance_m(previous, coordinate), 3)
                    if previous else None,
                "vertical_step_m": round(coordinate[2] - previous[2], 3)
                    if previous else None, "quality": quality}
            vertex.update((key, value.get(key)) for key in (
                "source", "fix_status", "satellites", "hdop", "correction_age_sec",
                "receiver_accuracy", "accuracy_observation"))
            vertices.append(vertex)
        result.update(vertices=vertices, vertex_offset=offset,
                      vertices_returned=len(vertices), has_more_vertices=end < len(coordinates))
        return result

    async def test_map_and_detail_after_undo_stream_before_cold_summary_scan(self):
        for route in ("map", "line"):
            tracker, line = self.make_undone_line(route)
            coordinates, measurements = list(line["coordinates"]), list(line["measurements"])
            summary = self.reference_summary(line, coordinates, measurements)
            if route == "map":
                expected = {"project_id": tracker.active_project_id,
                    "revision": tracker._event_sequence, "lines": [{
                        "id": line["id"], "name": line["name"], "state": line["state"],
                        "vertex_count": 128, "length_m": summary["length_m"],
                        "coordinates": coordinates[::2][-50:]}], "points": [], "truncated": True}
                query = "limit=50"
            else:
                expected = self.reference_detail(tracker, line, coordinates, measurements, 78, 50)
                query = "line_id=%s&offset=78&limit=50" % line["id"]
            self.assertNotIn("_summary", line)  # Reference construction does not warm it.
            tracker.point_store.close_cache()
            writer, batches, reads = Writer(), [], [0]
            original_decode = tracker.point_store._decode

            def decode(value, include_measurement=True):
                self.assertIn(b"HTTP/1.0 200 OK", writer.buf)
                self.assertIn(b"\r\n\r\n", writer.buf)
                self.assertTrue(bytes(writer.buf).split(b"\r\n\r\n", 1)[1])
                reads[0] += 1
                return original_decode(value, include_measurement)

            async def heartbeat():
                while True:
                    batches.append(reads[0])
                    reads[0] = 0
                    await asyncio.sleep(0)

            task = asyncio.create_task(heartbeat())
            try:
                with mock.patch.object(web, "tracker", tracker), \
                        mock.patch.object(web, "_admin_allowed", return_value=True), \
                        mock.patch.object(tracker.point_store, "_decode", side_effect=decode):
                    await web._handle_tracking_request(writer, "/api/tracking/" + route,
                                                       "GET", query, b"", False, {})
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            batches.append(reads[0])
            self.assertEqual(json.loads(bytes(writer.buf).split(b"\r\n\r\n", 1)[1]), expected)
            self.assertGreaterEqual(sum(batches), 128)
            self.assertLessEqual(max(batches), 5)
            self.assertGreater(len(batches), 30)
            self.assertEqual(tracker._line_summary(line), summary)

    async def test_summary_is_single_pass_and_cached_result_needs_no_flash_reads(self):
        tracker, line = self.make_undone_line("single-pass")
        coordinates, measurements = list(line["coordinates"]), list(line["measurements"])
        expected = self.reference_summary(line, coordinates, measurements)
        tracker.point_store.close_cache()
        original_getitem = FlashSequence.__getitem__

        def scalar_item(sequence, index):
            self.assertIsInstance(index, int)
            return original_getitem(sequence, index)

        with mock.patch.object(FlashSequence, "__getitem__", scalar_item), \
                mock.patch.object(tracker.point_store, "read", side_effect=AssertionError("Random read")), \
                mock.patch.object(tracker.point_store, "_decode",
                                  wraps=tracker.point_store._decode) as read:
            stream = tracker._iter_line_summary(line)
            self.assertEqual(read.call_count, 0)
            chunks = []
            for chunk in stream:
                chunks.append(chunk)
                self.assertLessEqual(read.call_count, min(len(chunks), 128))
            self.assertEqual(read.call_count, 128)
        self.assertEqual(chunks[:-1], [None] * 128)
        self.assertEqual(chunks[-1], expected)
        with mock.patch.object(tracker.point_store, "read", side_effect=AssertionError("Cache miss")):
            self.assertEqual(list(tracker._iter_line_summary(line)), [expected])
            self.assertEqual(tracker._line_summary(line), expected)

    async def test_append_during_scan_preserves_snapshot_and_newer_cache(self):
        tracker, line = self.make_undone_line("append")
        coordinates, measurements = list(line["coordinates"]), list(line["measurements"])
        expected = self.reference_summary(line, coordinates, measurements)
        stream = tracker._iter_line_summary(line)
        self.assertIsNone(next(stream))
        tracker.add_vertex(dict(FIX, lon=8.01), "automatic")
        current = tracker._line_summary(line)
        current_cache = dict(line["_summary"])
        self.assertEqual(list(stream)[-1], expected)
        self.assertEqual(line["_summary"], current_cache)
        self.assertEqual(tracker._line_summary(line), current)
        self.assertEqual(current["vertex_count"], 129)

    async def test_line_length_reads_each_coordinate_once_without_slicing(self):
        tracker, line = self.make_undone_line("length")
        coordinates = list(line["coordinates"])
        expected = sum(tracking._distance_m(a, b) for a, b in zip(coordinates, coordinates[1:]))
        original_getitem = FlashSequence.__getitem__

        def scalar_item(sequence, index):
            self.assertIsInstance(index, int)
            return original_getitem(sequence, index)

        with mock.patch.object(FlashSequence, "__getitem__", scalar_item), \
                mock.patch.object(tracker.point_store, "read",
                                  wraps=tracker.point_store.read) as read:
            self.assertAlmostEqual(tracker._line_length(line), expected, places=10)
            self.assertEqual(read.call_count, 128)

    async def test_append_after_undo_retains_complete_rebuilt_quality_summary(self):
        tracker, line = self.make_undone_line("undo-append")
        tracker.add_vertex(dict(FIX, lon=8.01), "automatic")
        coordinates, measurements = list(line["coordinates"]), list(line["measurements"])
        expected = self.reference_summary(line, coordinates, measurements)
        # Check the cache immediately: a later read must not hide a bad rebuild.
        self.assertEqual(line["_summary"]["vertex_count"], 129)
        self.assertEqual(line["_summary"]["quality_accepted_count"],
                         expected["quality_accepted_count"])
        self.assertAlmostEqual(line["_summary"]["length_m"], expected["length_m"], places=3)
        self.assertEqual(tracker._line_summary(line), expected)
        tracker.add_vertex(dict(FIX, lon=8.01002), "automatic")
        expected = self.reference_summary(line, list(line["coordinates"]),
                                          list(line["measurements"]))
        self.assertEqual(tracker._line_summary(line), expected)

    async def test_undo_uses_cached_length_without_eager_full_scan(self):
        tracker, line = self.make_undone_line("undo-cache")
        coordinates = list(line["coordinates"])
        expected = sum(tracking._distance_m(a, b)
                       for a, b in zip(coordinates[:-1], coordinates[1:-1]))
        tracker.point_store.close_cache()
        with mock.patch.object(tracker, "_line_length", side_effect=AssertionError(
                "A cached line length must not trigger a full undo scan")):
            tracker.undo_last()
        self.assertEqual(len(line["coordinates"]), 127)
        self.assertAlmostEqual(tracker._line_lengths[line["id"]], expected, places=10)


if __name__ == "__main__":
    unittest.main()
