# SPDX-License-Identifier: AGPL-3.0-only
"""GeoJSON export from persisted point sequences, with bounded iterator work."""
import copy
import json
import os
import tempfile
import unittest
from unittest import mock

import support  # noqa: F401; MicroPython modules on the host.
from pointstore import FlashSequence
import tracking


FIX = {"lat": 50.0, "lon": 8.0, "alt": 123.4, "qual": 4,
       "fix_status_text": "RTK_FIXED", "sats": 24, "hdop": 0.6,
       "correction_age_sec": 0.5, "station_id": "0054",
       "receiver_accuracy": {"source": "NMEA_GST", "semi_major_sigma_m": 0.02,
                             "altitude_sigma_m": 0.04}}


class TestTrackingGeoJSONStream(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="esp-rtk-geojson-")
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(tracking.Tracker, "storage_capacity", return_value={
            "free_flash_bytes": 8_000_000, "required_flash_reserve_bytes": 3_000_000,
            "estimated_temporary_bytes": 16_384, "writable": True})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.path = os.path.join(self.tmp.name, "tracking")
        self.tracker = tracking.Tracker(self.path)
        self.addCleanup(self.tracker.point_store.close)
        self.addCleanup(self.tracker._sync_journal, True)

    @staticmethod
    def expected_collection(tracker):
        """Reference the public export fields without using export helpers."""
        project = tracker._project()

        def measurement(raw):
            value = copy.deepcopy(raw)
            profile = tracker.profiles.get(value.get("profile_id"))
            value["quality"] = dict(value.get("quality") or {})
            if profile:
                value["quality"]["limits"] = copy.deepcopy(profile["limits"])
            return value

        features = []
        for line in project["lines"]:
            coordinates = list(line["coordinates"])
            length = sum(tracking._distance_m(a, b)
                         for a, b in zip(coordinates, coordinates[1:]))
            properties = {"line_id": line["id"], "name": line["name"],
                "object_type": "survey_line", "recording_mode": line["recording_mode"],
                "state": line["state"], "length_m": round(length, 3),
                "measurements": [measurement(item) for item in line["measurements"]]}
            properties.update(line["properties"])
            geometry = ({"type": "LineString", "coordinates": coordinates}
                        if len(coordinates) >= 2 else
                        {"type": "Point", "coordinates": coordinates[0]}
                        if coordinates else None)
            features.append({"type": "Feature", "id": line["id"],
                             "geometry": geometry, "properties": properties})
        for point in project["points"]:
            properties = copy.deepcopy(point)
            coordinate = properties.pop("coordinate")
            properties["measurement"] = measurement(properties["measurement"])
            features.append({"type": "Feature", "id": point["id"],
                "geometry": {"type": "Point", "coordinates": coordinate},
                "properties": properties})
        return {"type": "FeatureCollection", "name": project["name"],
                "schema_version": 4, "features": features}

    def make_mixed_project(self):
        project = self.tracker.create_project('Water "\u00dc" / network')
        self.tracker.start_line("Empty", 0)
        self.tracker.set_line_state("finish")
        self.tracker.start_line("Single", 0, fix=FIX)
        self.tracker.set_line_state("finish")
        self.tracker.start_line("Route", 0, {"material": 'PE "quoted"',
            "visible": True, "count": 3, "diameter": 1.25, "optional": None},
            fix=FIX, recording_mode="route")
        self.tracker.add_vertex(dict(FIX, lon=8.00002, alt=-2.5), "automatic")
        self.tracker.add_vertex(dict(FIX, lon=8.00004, qual=1,
            fix_status_text="GPS_FIX", receiver_accuracy=None,
            correction_age_sec=None), "automatic")
        self.tracker.add_asset("valve", 'Valve "\u00df"', "first\nsecond", FIX)
        return project

    def assert_export(self, tracker, expected):
        for line in tracker._project()["lines"]:
            self.assertIsInstance(line["coordinates"], FlashSequence)
            self.assertIsInstance(line["measurements"], FlashSequence)
        direct = tracker.geojson()
        self.assertEqual(json.loads(json.dumps(direct)), expected)
        self.assertEqual(json.loads("".join(tracker.iter_geojson())), expected)
        self.assertIsInstance(direct["features"][2]["geometry"]["coordinates"], list)
        measurements = direct["features"][2]["properties"]["measurements"]
        self.assertIs(measurements[0]["quality"]["accepted"], True)
        self.assertIs(measurements[2]["quality"]["accepted"], False)
        self.assertIn("rtk_fixed_required", measurements[2]["quality"]["reasons"])
        self.assertIn("limits", measurements[0]["quality"])
        self.assertIsInstance(measurements[0]["satellites"], int)
        self.assertIsNone(measurements[2]["correction_age_sec"])
        self.assertNotIn("limits", tracker._project()["lines"][2]
                         ["measurements"][0]["quality"])

    def test_flash_backed_export_and_checkpoint_restart_preserve_features(self):
        self.make_mixed_project()
        expected = self.expected_collection(self.tracker)
        self.assert_export(self.tracker, expected)
        self.tracker.compact()
        self.tracker.point_store.close()
        self.tracker._sync_journal(close=True)
        restored = tracking.Tracker(self.path)
        self.addCleanup(restored.point_store.close)
        self.addCleanup(restored._sync_journal, True)
        self.assert_export(restored, expected)

    def test_custom_scalar_properties_keep_override_semantics(self):
        self.tracker.create_project("Overrides")
        self.tracker.start_line("Line", 0, {"name": "exported name", "length_m": None,
            "measurements": "external", "state": False}, fix=FIX)
        self.tracker.add_vertex(dict(FIX, lon=8.00002))
        expected = self.expected_collection(self.tracker)
        self.assertEqual(self.tracker.geojson(), expected)
        self.assertEqual(json.loads("".join(self.tracker.iter_geojson())), expected)

    def test_empty_collection_and_missing_project(self):
        self.tracker.create_project("Empty")
        self.assertEqual(json.loads("".join(self.tracker.iter_geojson())), {
            "type": "FeatureCollection", "name": "Empty", "schema_version": 4,
            "features": []})
        for export in (self.tracker.geojson, lambda key: list(self.tracker.iter_geojson(key))):
            with self.assertRaisesRegex(ValueError, "Project not found"):
                export("missing")

    def test_iterator_never_materializes_or_scans_a_whole_flash_sequence(self):
        self.tracker.create_project("Streaming")
        self.tracker.start_line("Long", 0)
        for index in range(257):
            self.tracker.add_vertex(dict(FIX, lon=8.0 + index * 0.00002))
        expected = self.expected_collection(self.tracker)
        get_item = FlashSequence.__getitem__

        def scalar_item(sequence, index):
            self.assertIsInstance(index, int, "A slice materializes flash coordinates")
            return get_item(sequence, index)

        with mock.patch.object(FlashSequence, "__getitem__", scalar_item), \
                mock.patch.object(FlashSequence, "__iter__", side_effect=AssertionError(
                    "Streaming must not copy a flash sequence")), \
                mock.patch.object(self.tracker, "_line_length", side_effect=AssertionError(
                    "Streaming must not scan an entire line between yields")), \
                mock.patch.object(self.tracker.point_store, "read",
                    wraps=self.tracker.point_store.read) as read, \
                mock.patch.object(self.tracker, "_expand_measurement",
                    wraps=self.tracker._expand_measurement) as expand:
            chunks, stream = [], self.tracker.iter_geojson()
            while True:
                previous_reads, previous_expands = read.call_count, expand.call_count
                try:
                    chunk = next(stream)
                except StopIteration:
                    break
                self.assertLessEqual(read.call_count - previous_reads, 1)
                self.assertLessEqual(expand.call_count - previous_expands, 1)
                self.assertIsInstance(chunk, str)
                self.assertLess(len(chunk), 4096)
                chunks.append(chunk)
            self.assertEqual(read.call_count, 514)
            self.assertEqual(expand.call_count, 257)
        self.assertEqual(json.loads("".join(chunks)), expected)

    def test_append_during_line_stream_keeps_initial_coordinate_measurement_counts(self):
        self.tracker.create_project("Append")
        self.tracker.start_line("Line", 0, fix=FIX)
        self.tracker.add_vertex(dict(FIX, lon=8.00002))
        expected = self.expected_collection(self.tracker)
        stream = self.tracker.iter_geojson()
        chunks = [next(stream), next(stream)]  # The line's counts are now fixed.
        self.tracker.add_vertex(dict(FIX, lon=8.00004))
        chunks.extend(stream)
        self.assertEqual(json.loads("".join(chunks)), expected)
        self.assertEqual(self.tracker.feature_count(), 3)


if __name__ == "__main__":
    unittest.main()
