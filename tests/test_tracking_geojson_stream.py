# SPDX-License-Identifier: AGPL-3.0-only
"""GeoJSON export from persisted point sequences, with bounded iterator work."""
import asyncio
import copy
import datetime
import json
import os
import tempfile
import unittest
from unittest import mock

import support  # noqa: F401; MicroPython modules on the host.
import cfg
from pointstore import FlashPointStore, FlashSequence
import tracking
import tracking_reference
from test_frame_assignment import legacy_backup


FIX = {"lat": 50.0, "lon": 8.0, "alt": 123.4, "qual": 4,
       "lat_e8": 5000000000, "lon_e8": 800000000, "alt_mm": 123400,
       "geoid_sep_mm": 45600,
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
        assignment = project.get("frame_assignment")

        def frame(raw, in_scope):
            # The #39 rule, written out independently: a stored frame is
            # "recorded"; an RTK point in the assignment's scope without one is
            # "assigned"; anything else is unknown.
            if raw.get("reference_frame"):
                return raw["reference_frame"], raw.get("reference_frame_epoch") or None, "recorded"
            quality = raw.get("fix_quality")
            rtk = (quality in (4, 5) if quality is not None else
                   raw.get("fix_status") in ("RTK_FIXED", "RTK_FLOAT"))
            if assignment and in_scope and rtk:
                return (assignment["reference_frame"], assignment["reference_frame_epoch"],
                        "assigned")
            return "unknown", raw.get("reference_frame_epoch") or None, None

        def heights(raw, altitude_mm):
            # Written out independently of the export helper.
            antenna = raw.get("antenna_height_mm")
            metres = lambda mm: None if mm is None else mm / 1000
            return {"altitude_msl_m": altitude_mm / 1000,
                    "altitude_ellipsoid_m": metres(raw.get("alt_ellipsoid_mm")),
                    "geoid_separation_m": metres(raw.get("geoid_sep_mm")),
                    "antenna_height_m": metres(antenna),
                    "ground_height_msl_m": None if antenna is None else (altitude_mm - antenna) / 1000,
                    "height_reference": "antenna_phase_centre_msl"}

        def measurement(raw, altitude_mm=None, in_scope=False):
            value = copy.deepcopy(raw)
            if altitude_mm is not None:
                value.update(heights(raw, altitude_mm))
            profile = tracker.profiles.get(value.get("profile_id"))
            value["quality"] = dict(value.get("quality") or {})
            if profile:
                value["quality"]["limits"] = copy.deepcopy(profile["limits"])
            shown = frame(raw, in_scope)
            value["reference_frame"], value["reference_frame_source"] = shown[0], shown[2]
            if shown[2] == "assigned" or "reference_frame_epoch" in value:
                value["reference_frame_epoch"] = shown[1]
            # recorded_at counts seconds since 2000-01-01T00:00:00Z; derived independently.
            stamp = datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc) + \
                datetime.timedelta(seconds=raw["recorded_at"])
            value["recorded_at_utc"] = stamp.strftime("%Y-%m-%dT%H:%M:%SZ")
            value["clock_unsynced"] = not 2024 <= stamp.year <= 2099
            return value

        def degrees(coordinate):
            # The export is decimal text of the stored integers, read back as floats.
            return [coordinate[0] / 10 ** 8, coordinate[1] / 10 ** 8, coordinate[2] / 1000]

        scopes = assignment["lines"] if assignment else {}
        features = []
        for line in project["lines"]:
            coordinates = list(line["coordinates"])
            length = sum(tracking._distance_m(a, b)
                         for a, b in zip(coordinates, coordinates[1:]))
            coordinates = [degrees(item) for item in coordinates]
            in_scope = scopes.get(line["id"], [0, 0])[0]
            properties = {"line_id": line["id"], "name": line["name"],
                "object_type": "survey_line",
                "recording_mode": line.get("recording_mode", "survey"),
                "state": line["state"], "length_m": round(length, 3),
                "measurements": [measurement(item, coordinate[2], index < in_scope)
                                 for index, (item, coordinate) in
                                 enumerate(zip(line["measurements"], line["coordinates"]))]}
            # Tallied independently of the export helpers; custom properties win.
            tally = {}
            for index, item in enumerate(line["measurements"]):
                shown = frame(item, index < in_scope)[0]
                tally[shown] = tally.get(shown, 0) + 1
            for key, value in (("reference_frame", next(iter(tally)) if len(tally) == 1
                                else "mixed" if tally else "unknown"),
                               ("reference_frames", tally)):
                properties[key] = value
            properties.update(line["properties"])
            geometry = ({"type": "LineString", "coordinates": coordinates}
                        if len(coordinates) >= 2 else
                        {"type": "Point", "coordinates": coordinates[0]}
                        if coordinates else None)
            features.append({"type": "Feature", "id": line["id"],
                             "geometry": geometry, "properties": properties})
        points_in_scope = assignment["points"][0] if assignment else 0
        for index, point in enumerate(project["points"]):
            properties = copy.deepcopy(point)
            properties.update(heights(point["measurement"], point["coordinate"][2]))
            coordinate = degrees(properties.pop("coordinate"))
            # Always present in the export; a legacy point never stored one.
            projected = properties.get("projected_coordinate")
            properties["projected_coordinate"] = None if projected is None else degrees(projected)
            properties["measurement"] = measurement(properties["measurement"],
                                                    in_scope=index < points_in_scope)
            for key in ("station_id", "ntrip_mount"):
                properties[key] = point["measurement"].get(key)
            (properties["reference_frame"], properties["reference_frame_epoch"],
             properties["reference_frame_source"]) = frame(point["measurement"],
                                                           index < points_in_scope)
            # A legacy point has no gate profile.
            profile_id = point["measurement"].get("profile_id")
            properties["gate_profile"] = profile_id
            properties["gate_limits"] = (copy.deepcopy(tracker.profiles[profile_id]["limits"])
                                         if profile_id else None)
            features.append({"type": "Feature", "id": point["id"],
                "geometry": {"type": "Point", "coordinates": coordinate},
                "properties": properties})
        collection = {"type": "FeatureCollection", "name": project["name"],
                      "schema_version": 5, "crs_note": tracking.CRS_NOTE, "features": features}
        if assignment:
            stamp = datetime.datetime(2000, 1, 1, tzinfo=datetime.timezone.utc) + \
                datetime.timedelta(seconds=assignment["assigned_at"])
            collection["frame_assignment"] = {
                "reference_frame": assignment["reference_frame"],
                "reference_frame_epoch": assignment["reference_frame_epoch"],
                "assigned_at_utc": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "count": sum(value[1] for value in assignment["lines"].values()) +
                assignment["points"][1]}
        return collection

    def make_mixed_project(self):
        project = self.tracker.create_project('Water "\u00dc" / network')
        self.tracker.start_line("Empty", 0)
        self.tracker.set_line_state("finish")
        self.tracker.start_line("Single", 0, fix=FIX)
        self.tracker.set_line_state("finish")
        self.tracker.start_line("Route", 0, {"material": 'PE "quoted"',
            "visible": True, "count": 3, "diameter": 1.25, "optional": None},
            fix=FIX, recording_mode="route")
        self.tracker.add_vertex(dict(FIX, lon=8.00002, lon_e8=800002000,
                                     alt=-2.5, alt_mm=-2500), "automatic")
        self.tracker.add_vertex(dict(FIX, lon=8.00004, lon_e8=800004000, qual=1,
            fix_status_text="GPS_FIX", receiver_accuracy=None,
            correction_age_sec=None), "automatic")
        self.tracker.add_asset("valve", 'Valve "\u00df"', "first\nsecond", FIX)
        return project

    def assert_export(self, tracker, expected):
        for line in tracker._project()["lines"]:
            self.assertIsInstance(line["coordinates"], FlashSequence)
            self.assertIsInstance(line["measurements"], FlashSequence)
        direct = tracking_reference.geojson(tracker)
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

    def test_stakeout_point_is_marked_in_the_export(self):
        self.tracker.create_project("Stakeout")
        point = self.tracker.add_asset("stakeout", "Line A / P001", "", FIX, False, {
            "tid": "so-3a4739b9/1/1", "gen": 2, "cmd": "0123456789abcdef", "mode": "verified",
            "tolerance_m": 0.1, "dn_m": 0.012, "de_m": -0.004, "dist_m": 0.013})
        collection = json.loads("".join(self.tracker.iter_geojson()))
        feature = next(item for item in collection["features"] if item["id"] == point["id"])
        properties = feature["properties"]
        self.assertEqual(properties["object_type"], "stakeout")
        self.assertEqual(properties["stakeout_tid"], "so-3a4739b9/1/1")
        self.assertEqual(properties["stakeout_gen"], 2)
        self.assertEqual(properties["measurement"]["source"], "stakeout_verified")
        rows = self.tracker.csv().splitlines()
        self.assertTrue(rows[1].startswith('"%s","stakeout","Line A / P001",' % point["id"]), rows[1])

    def test_map_stream_matches_the_dict_and_lists_each_profile_once(self):
        self.make_mixed_project()
        with mock.patch.dict(cfg.CONFIG, {"measurement_max_hdop": 2.0}):
            self.tracker.add_asset("tap", "Loose", "", dict(FIX, hdop=1.8),
                                   link_to_active_line=False)
        text = "".join(self.tracker.iter_map_data())
        streamed = json.loads(text)
        self.assertEqual(streamed, self.tracker.map_data())
        self.assertEqual(text.count('"profiles"'), 1)
        used = [point["measurement"]["profile_id"] for point in streamed["points"]]
        self.assertEqual(len(set(used)), 2)
        self.assertEqual(sorted(streamed["profiles"]), sorted(set(used)))
        for profile_id, limits in streamed["profiles"].items():
            self.assertEqual(limits, self.tracker.profiles[profile_id]["limits"])
            self.assertIn("fix_quality", limits)
        # The table follows the points; a truncated map lists only emitted points.
        self.assertGreater(text.index('"profiles"'), text.index('"points"'))
        short = json.loads("".join(self.tracker.iter_map_data(limit=1)))
        self.assertEqual(list(short["profiles"]), [short["points"][0]["measurement"]["profile_id"]])

    def test_custom_scalar_properties_keep_override_semantics(self):
        self.tracker.create_project("Overrides")
        self.tracker.start_line("Line", 0, {"name": "exported name", "length_m": None,
            "measurements": "external", "state": False}, fix=FIX)
        self.tracker.add_vertex(dict(FIX, lon=8.00002, lon_e8=800002000))
        expected = self.expected_collection(self.tracker)
        self.assertEqual(tracking_reference.geojson(self.tracker), expected)
        self.assertEqual(json.loads("".join(self.tracker.iter_geojson())), expected)

    def test_empty_collection_and_missing_project(self):
        self.tracker.create_project("Empty")
        self.assertEqual(json.loads("".join(self.tracker.iter_geojson())), {
            "type": "FeatureCollection", "name": "Empty", "schema_version": 5,
            "crs_note": tracking.CRS_NOTE, "features": []})
        for export in (lambda key: tracking_reference.geojson(self.tracker, key),
                       lambda key: list(self.tracker.iter_geojson(key))):
            with self.assertRaisesRegex(ValueError, "Project not found"):
                export("missing")

    def test_iterator_never_materializes_or_scans_a_whole_flash_sequence(self):
        self.tracker.create_project("Streaming")
        self.tracker.start_line("Long", 0)
        for index in range(257):
            self.tracker.add_vertex(dict(FIX, lon=8.0 + index * 0.00002,
                                         lon_e8=800000000 + index * 2000))
        expected = self.expected_collection(self.tracker)
        get_item = FlashSequence.__getitem__
        original_decode, decode_calls = FlashPointStore._decode, []

        def counting_decode(store, *args, **kwargs):
            decode_calls.append(1)
            return original_decode(store, *args, **kwargs)

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
                    wraps=self.tracker._expand_measurement) as expand, \
                mock.patch.object(FlashPointStore, "_decode", counting_decode):
            chunks, stream = [], self.tracker.iter_geojson()
            while True:
                previous_reads, previous_expands = read.call_count, expand.call_count
                previous_decodes = len(decode_calls)
                try:
                    chunk = next(stream)
                except StopIteration:
                    break
                self.assertLessEqual(read.call_count - previous_reads, 2)
                self.assertLessEqual(len(decode_calls) - previous_decodes, 2)
                self.assertLessEqual(expand.call_count - previous_expands, 1)
                self.assertIsInstance(chunk, str)
                self.assertLess(len(chunk), 4096)
                chunks.append(chunk)
            # Line vertices are read sequentially, never through the random-access cache.
            self.assertEqual(read.call_count, 0)
            self.assertEqual(len(decode_calls), 514)
            self.assertEqual(expand.call_count, 257)
        self.assertEqual(json.loads("".join(chunks)), expected)

    def test_append_during_line_stream_keeps_initial_coordinate_measurement_counts(self):
        self.tracker.create_project("Append")
        self.tracker.start_line("Line", 0, fix=FIX)
        self.tracker.add_vertex(dict(FIX, lon=8.00002, lon_e8=800002000))
        expected = self.expected_collection(self.tracker)
        stream = self.tracker.iter_geojson()
        chunks = [next(stream), next(stream)]  # The line's counts are now fixed.
        self.tracker.add_vertex(dict(FIX, lon=8.00004, lon_e8=800004000))
        chunks.extend(stream)
        self.assertEqual(json.loads("".join(chunks)), expected)
        self.assertEqual(self.tracker.feature_count(), 3)


    def test_stream_matches_the_reference_with_an_assignment(self):
        # A legacy line and legacy points: RTK and DGPS without a frame; then RTK
        # recorded with a frame, the assignment, and RTK recorded after it.
        dgps = {"fix_quality": 2, "fix_status": "DGPS/SBAS"}
        with mock.patch.dict(cfg.CONFIG, {"reference_frame": "", "reference_frame_epoch": ""}):
            self.tracker.restore_ndjson(legacy_backup(
                [("l0", "L", [{}, dgps])], [{}, dgps], line_state="paused", active_line="l0"))
            with mock.patch.dict(cfg.CONFIG, {"reference_frame": "ETRS89 (synthetic)",
                                              "reference_frame_epoch": "2010.0"}):
                self.tracker.set_line_state("resume")
                self.tracker.add_vertex(dict(FIX, lon_e8=800010000))
                self.tracker.set_line_state("pause")
                self.tracker.add_asset("tap", "Known", "", dict(FIX, lon_e8=800012000),
                                       link_to_active_line=False)
            shown = asyncio.run(self.tracker.frame_assignment_preview_async("p1"))
            asyncio.run(self.tracker.assign_frame_async(
                "p1", "ETRS89 (example)", "2016.0", shown["total"], shown["scope_token"]))
            self.tracker.set_line_state("resume")
            self.tracker.add_vertex(dict(FIX, lon_e8=800014000))
            self.tracker.set_line_state("pause")
            self.tracker.add_asset("tap", "Later", "", dict(FIX, lon_e8=800016000),
                                   link_to_active_line=False)
        expected = self.expected_collection(self.tracker)
        self.assertEqual(json.loads("".join(self.tracker.iter_geojson())), expected)
        self.assertEqual(json.loads(json.dumps(tracking_reference.geojson(self.tracker))), expected)
        line, points = expected["features"][0]["properties"], expected["features"][1:]
        self.assertEqual([item["reference_frame_source"] for item in line["measurements"]],
                         ["assigned", None, "recorded", None])
        self.assertEqual([point["properties"]["reference_frame_source"] for point in points],
                         ["assigned", None, "recorded", None])
        self.assertEqual(line["reference_frames"], {"ETRS89 (example)": 1, "unknown": 2,
                                                    "ETRS89 (synthetic)": 1})
        self.assertEqual(expected["frame_assignment"]["count"], 2)

    def clocked_vertices(self, count, first=0, **config):
        """Record count vertices, each with its own longitude and recorded_at second."""
        for index in range(first, first + count):
            with mock.patch.object(tracking.time, "time", return_value=1_800_000_000 + index), \
                    mock.patch.dict(cfg.CONFIG, config):
                self.tracker.add_vertex(dict(FIX, lon=8.0 + index * 0.00002,
                                             lon_e8=800000000 + index * 2000))

    @staticmethod
    def pull_until(stream, end, after=0):
        """Chunks up to and including the first chunk equal to end after after chunks."""
        chunks = [next(stream) for _ in range(after)]
        for chunk in stream:
            chunks.append(chunk)
            if chunk == end:
                return "".join(chunks)
        raise AssertionError("The stream ended before " + end)

    @staticmethod
    def vertex_pairs(line):
        """{lon_e8: recorded_at} of the line's stored vertices."""
        return {coordinate[0]: measurement["recorded_at"] for coordinate, measurement in
                zip(list(line["coordinates"]), list(line["measurements"]))}

    def test_undo_and_append_between_the_passes_keep_pairs_together(self):
        self.tracker.create_project("Pairs")
        self.tracker.start_line("Line", 0, fix=FIX)
        self.clocked_vertices(3, first=1)
        line = self.tracker._project()["lines"][0]
        pairs = self.vertex_pairs(line)
        self.assertEqual(len(set(pairs.values())), 4)
        stream = self.tracker.iter_geojson()
        text = self.pull_until(stream, "]}")
        self.tracker.undo_last()
        self.clocked_vertices(1, first=9)
        self.assertEqual(len(line["coordinates"]), 4)
        feature = json.loads(text + "".join(stream))["features"][0]
        coordinates = feature["geometry"]["coordinates"]
        measurements = feature["properties"]["measurements"]
        self.assertEqual((len(coordinates), len(measurements)), (4, 4))
        for index, (coordinate, measurement) in enumerate(zip(coordinates, measurements)):
            with self.subTest(index=index):
                self.assertEqual(measurement["recorded_at"],
                                 pairs[round(coordinate[0] * 10 ** 8)])
        # The vertex recorded meanwhile is not in this file (E2).
        self.assertNotIn(800000000 + 9 * 2000, [round(c[0] * 10 ** 8) for c in coordinates])

    def test_undo_during_the_geometry_pass_completes_the_line(self):
        self.tracker.create_project("Undo")
        self.tracker.start_line("Line", 0, fix=FIX)
        self.clocked_vertices(3, first=1)
        expected = json.loads(json.dumps(tracking_reference.geojson(self.tracker)))
        stream = self.tracker.iter_geojson()
        # Head, feature start, LineString start and two coordinates.
        chunks = [next(stream) for _ in range(5)]
        self.assertEqual(chunks[-1].count("["), 1)
        self.tracker.undo_last()
        self.assertEqual(json.loads("".join(chunks) + "".join(stream)), expected)

    def test_overridden_measurements_with_a_stale_summary_count_the_captured_rows(self):
        def build(name, frames, assign=False):
            if self.tracker.active_line_id:
                self.tracker.set_line_state("finish")
            self.tracker.create_project(name)
            with mock.patch.dict(cfg.CONFIG, {"reference_frame": frames[0],
                                              "reference_frame_epoch": ""}):
                self.tracker.start_line("Line", 0, {"measurements": "external"}, fix=FIX)
            for index, frame in enumerate(frames[1:], 1):
                self.clocked_vertices(1, first=index, reference_frame=frame,
                                      reference_frame_epoch="")
            if assign:
                self.tracker.set_line_state("pause")
                shown = asyncio.run(self.tracker.frame_assignment_preview_async(
                    self.tracker.active_project_id))
                asyncio.run(self.tracker.assign_frame_async(
                    self.tracker.active_project_id, "ETRS89 (example)", "2010.0",
                    shown["total"], shown["scope_token"]))
                # Behind the scope: undoing it leaves the assignment as it is.
                self.tracker.set_line_state("resume")
                self.clocked_vertices(1, first=8, reference_frame="", reference_frame_epoch="")
            line = self.tracker._project()["lines"][0]
            line.pop("_summary")
            return line

        def export_with_undo(replacement=None):
            expected = json.loads(json.dumps(tracking_reference.geojson(self.tracker)))
            store = self.tracker.point_store
            with mock.patch.object(store, "read", wraps=store.read) as read:
                stream = self.tracker.iter_geojson()
                text = self.pull_until(stream, "]}")
            self.tracker.undo_last()
            if replacement is not None:
                self.clocked_vertices(1, first=9, reference_frame=replacement,
                                      reference_frame_epoch="")
            with mock.patch.object(store, "read", wraps=store.read) as more:
                text += "".join(stream)
            return (json.loads(text)["features"][0]["properties"], expected,
                    read.call_count + more.call_count)

        for case, replacement in (("undo", None), ("undo and a replacement", "C")):
            with self.subTest(case):
                build(case, ["A", "A", "B", "B"])
                properties, _expected, reads = export_with_undo(replacement)
                self.assertEqual(properties["reference_frames"], {"A": 2, "B": 2})
                self.assertEqual(properties["reference_frame"], "mixed")
                self.assertEqual(properties["measurements"], "external")
                # The tally rides along the geometry pass; no second scan of the line.
                self.assertEqual(reads, 0)
        with self.subTest("assigned"):
            with mock.patch.dict(cfg.CONFIG, {"reference_frame": "",
                                              "reference_frame_epoch": ""}):
                build("assigned", ["A", "", "", "B"], assign=True)
                properties, expected, reads = export_with_undo()
            frames = expected["features"][0]["properties"]["reference_frames"]
            self.assertEqual(frames, {"A": 1, "ETRS89 (example)": 2, "B": 1, "unknown": 1})
            self.assertEqual(properties["reference_frames"], frames)
            self.assertEqual(properties["measurements"], "external")
            self.assertEqual(reads, 0)

    def test_recording_into_a_new_segment_during_export_keeps_the_captured_line(self):
        with mock.patch("pointstore.POINT_SEGMENT_BYTES", 2048):
            self.tracker.create_project("Segments")
            self.tracker.start_line("Line", 0, fix=FIX)
            self.clocked_vertices(19, first=1)
            store = self.tracker.point_store
            segments = len(store.segments)
            self.assertGreater(segments, 1)
            expected = json.loads(json.dumps(tracking_reference.geojson(self.tracker)))
            stream = self.tracker.iter_geojson()
            # Head, feature start, LineString start and the first coordinate.
            chunks = [next(stream) for _ in range(4)]
            self.clocked_vertices(10, first=20)
            self.assertGreater(len(store.segments), segments)
            collection = json.loads("".join(chunks) + "".join(stream))
        feature = collection["features"][0]
        self.assertEqual(len(feature["geometry"]["coordinates"]), 20)
        self.assertEqual(len(feature["properties"]["measurements"]), 20)
        self.assertEqual(collection, expected)

if __name__ == "__main__":
    unittest.main()
