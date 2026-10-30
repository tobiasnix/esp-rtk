# SPDX-License-Identifier: AGPL-3.0-only
"""Field-survey line and related-point recording."""
import asyncio
import hashlib
import json
import os
import re
import shutil
import struct
import tempfile
import copy
import inspect
import unittest
from unittest import mock

import support
from support import state
import geo
import gnss
import tracking
import tracking_reference
import cfg
from pointstore import _compact


FIX = {"lat": 50.0, "lon": 8.0, "alt": 123.4, "qual": 4,
       "lat_e8": 5000000000, "lon_e8": 800000000, "alt_mm": 123400,
       "geoid_sep_mm": 45600,
       "fix_status_text": "RTK_FIXED", "sats": 24, "hdop": 0.6,
       "correction_age_sec": 0.5, "station_id": "0054",
       "receiver_accuracy": {"source": "NMEA_GST", "semi_major_sigma_m": 0.02,
                             "altitude_sigma_m": 0.04}}
FIX_E8 = dict(FIX)  # FIX carries the exact fields; the name keeps the average_fixes tests readable
# A sample as an older caller would build it: floats only, no exact fields.
FIX_FLOAT = {key: value for key, value in FIX.items()
             if key not in ("lat_e8", "lon_e8", "alt_mm", "geoid_sep_mm")}


class TestTracking(unittest.TestCase):
    def setUp(self):
        # A CI host may legitimately have less than 20% disk free. Unit tests
        # model device flash explicitly so their result never depends on the
        # shared runner's current disk occupancy.
        self.storage_patch = mock.patch.object(
            tracking.Tracker, "storage_capacity", return_value={
                "free_flash_bytes": 8_000_000,
                "required_flash_reserve_bytes": 3_000_000,
                "estimated_temporary_bytes": 16_384,
                "writable": True})
        self.storage_patch.start()
        self.tmp = tempfile.TemporaryDirectory(prefix="esp-rtk-tracking-")
        self.path = os.path.join(self.tmp.name, "tracking.jsonl")
        self.tracker = tracking.Tracker(self.path)

    def tearDown(self):
        self.tmp.cleanup()
        self.storage_patch.stop()

    def test_exports_carry_frame_mount_and_station(self):
        self.tracker.create_project("Frames")
        with mock.patch.dict(cfg.CONFIG, {"reference_frame": "ETRS89 (synthetic)", "reference_frame_epoch": "2010.0",
                                          "ntrip_mount": "SYNTH1"}), \
                mock.patch.dict(state.app.stats, {"ntrip_state": "streaming", "ntrip_endpoint": "primary"}):
            self.tracker.add_asset("tap", "RTK", "", FIX, link_to_active_line=False)
        with mock.patch.dict(cfg.CONFIG, {"measurement_required_fix": "DGPS", "reference_frame": "ETRS89 (synthetic)"}):
            self.tracker.add_asset("tap", "SBAS", "", dict(FIX, qual=2, fix_status_text="DGPS"), link_to_active_line=False)
        with mock.patch.dict(cfg.CONFIG, {"reference_frame": "ETRS89 (synthetic)", "reference_frame_epoch": "2010.0"}):
            collection = json.loads("".join(self.tracker.iter_geojson()))
            self.assertEqual(tracking_reference.geojson(self.tracker), collection)
            csv = self.tracker.csv()
        rtk, sbas = (f["properties"] for f in collection["features"])
        self.assertEqual(rtk["reference_frame"], "ETRS89 (synthetic)"); self.assertEqual(rtk["reference_frame_epoch"], "2010.0")
        self.assertEqual(rtk["ntrip_mount"], "SYNTH1"); self.assertEqual(rtk["station_id"], "0054")
        self.assertEqual(sbas["reference_frame"], "WGS84/ITRF"); self.assertIsNone(sbas["reference_frame_epoch"])
        self.assertIn("not transformed", collection["crs_note"]); self.assertIn("MSL height", collection["crs_note"])
        self.assertEqual(collection["crs_note"], (
            "Coordinates are not transformed. Horizontal positions are in the frame named by reference_frame on each "
            "feature: RTK solutions (fix_quality 4 or 5) in the correction service's frame, other solutions in "
            "WGS84/ITRF at observation time, unknown where no frame was configured. The third coordinate is the MSL "
            "height of the antenna phase centre, not the ellipsoidal height RFC 7946 expects; see altitude_ellipsoid_m. "
            'reference_frame_source says how the frame is known: "recorded" with the measurement, "assigned" afterwards '
            "by the user for RTK points of the project that had none (a statement, not a measurement), null where it "
            "is unknown."))
        self.assertEqual((rtk["reference_frame_source"], sbas["reference_frame_source"]), ("recorded", "recorded"))
        header = csv.splitlines()[0].split(",")
        index = header.index("station_id")
        self.assertEqual(header[index:index + 5], ["station_id", "ntrip_mount", "reference_frame", "reference_frame_epoch",
                                                   "reference_frame_source"])
        self.assertEqual(header[index + 5], "gate_profile")
        self.assertIn('"0054","SYNTH1","ETRS89 (synthetic)","2010.0","recorded"', csv.splitlines()[1])
        self.assertIn('"WGS84/ITRF","","recorded"', csv.splitlines()[2])

    def test_csv_frame_cells_are_quoted_as_text_and_unknown_is_printed(self):
        self.tracker.create_project("Cells")
        self.tracker.add_asset("tap", "Before", "", dict(FIX, station_id="=1+1"), link_to_active_line=False)
        rows = self.tracker.csv().splitlines()
        self.assertIn('"\'=1+1","","unknown",""', rows[1])

    def test_line_features_summarise_their_frames(self):
        self.tracker.create_project("Lines")
        with mock.patch.dict(cfg.CONFIG, {"reference_frame": "ETRS89 (synthetic)"}):
            self.tracker.start_line("L", 1, fix=FIX)
            self.tracker.add_vertex(dict(FIX, lon_e8=800002000))
        line = json.loads("".join(self.tracker.iter_geojson()))["features"][0]["properties"]
        self.assertEqual(line["reference_frame"], "ETRS89 (synthetic)")
        self.assertEqual(line["reference_frames"], {"ETRS89 (synthetic)": 2})
        with mock.patch.dict(cfg.CONFIG, {"measurement_required_fix": "DGPS"}):
            self.tracker.add_vertex(dict(FIX, lon_e8=800004000, qual=2, fix_status_text="DGPS"))
        line = json.loads("".join(self.tracker.iter_geojson()))["features"][0]["properties"]
        self.assertEqual(line["reference_frame"], "mixed")
        self.assertEqual(line["reference_frames"], {"ETRS89 (synthetic)": 2, "WGS84/ITRF": 1})
        self.assertEqual(tracking_reference.geojson(self.tracker)["features"][0]["properties"]["reference_frames"],
                         line["reference_frames"])

    def test_line_frame_is_unknown_without_a_configured_frame_and_empty_without_vertices(self):
        self.tracker.create_project("Unknown")
        self.tracker.start_line("L", 1, fix=FIX)
        self.tracker.add_vertex(dict(FIX, lon_e8=800002000))
        self.tracker.set_line_state("finish")
        self.tracker.start_line("Empty", 0)
        lines = [f["properties"] for f in json.loads("".join(self.tracker.iter_geojson()))["features"]]
        self.assertEqual(lines[0]["reference_frame"], "unknown")
        self.assertEqual(lines[0]["reference_frames"], {"unknown": 2})
        self.assertEqual(lines[1]["reference_frame"], "unknown")
        self.assertEqual(lines[1]["reference_frames"], {})

    def test_changing_the_frame_does_not_rewrite_stored_points(self):
        self.tracker.create_project("Frames")
        self.tracker.add_asset("tap", "Before", "", FIX, link_to_active_line=False)       # frame unconfigured -> None
        with mock.patch.dict(cfg.CONFIG, {"reference_frame": "ETRS89 (synthetic)"}):
            self.tracker.add_asset("tap", "After", "", FIX, link_to_active_line=False)
            frames = [f["properties"]["reference_frame"] for f in json.loads("".join(self.tracker.iter_geojson()))["features"]]
        self.assertEqual(frames, ["unknown", "ETRS89 (synthetic)"])

    def test_project_frame_tally_and_mixing_flag(self):
        self.tracker.create_project("Mixed")
        with mock.patch.dict(cfg.CONFIG, {"reference_frame": "ETRS89 (synthetic)"}):
            self.tracker.start_line("L", 1, fix=FIX)
            self.tracker.add_vertex(dict(FIX, lon_e8=800002000))
        self.assertEqual(self.tracker.status()["projects"][0]["frames"], {"ETRS89 (synthetic)": 2})
        self.assertFalse(self.tracker.status()["projects"][0]["mixed_frames"])
        with mock.patch.dict(cfg.CONFIG, {"measurement_required_fix": "DGPS"}):
            self.tracker.add_asset("tap", "S", "", dict(FIX, qual=2, fix_status_text="DGPS"), link_to_active_line=False)
        project = self.tracker.status()["projects"][0]
        self.assertEqual(project["frames"], {"ETRS89 (synthetic)": 2, "WGS84/ITRF": 1})
        self.assertTrue(project["mixed_frames"])
        line = self.tracker.projects[0]["lines"][0]
        line.pop("_summary", None)                       # a stale summary is rebuilt with the tally
        for _ in self.tracker._iter_line_summary(line): pass
        self.assertEqual(line["_summary"]["frames"], {"ETRS89 (synthetic)": 2})
        self.assertEqual(self.tracker.live_status()["active_project"]["frames"], project["frames"])

    def test_unconfigured_frame_counts_as_unknown(self):
        self.tracker.create_project("Unknown")
        self.tracker.add_asset("tap", "U", "", FIX, link_to_active_line=False)
        project = self.tracker.status()["projects"][0]
        self.assertEqual(project["frames"], {"unknown": 1})
        self.assertFalse(project["mixed_frames"]); self.assertTrue(project["unknown_frames"])

    def test_undo_and_reload_keep_the_tally_consistent(self):
        self.tracker.create_project("Undo")
        with mock.patch.dict(cfg.CONFIG, {"reference_frame": "ETRS89 (synthetic)"}):
            self.tracker.start_line("L", 1, fix=FIX); self.tracker.add_vertex(dict(FIX, lon_e8=800002000))
        with mock.patch.dict(cfg.CONFIG, {"measurement_required_fix": "DGPS"}):
            self.tracker.add_asset("tap", "S", "", dict(FIX, qual=2, fix_status_text="DGPS"), link_to_active_line=False)
        self.tracker.undo_last()                                     # removes the SBAS asset
        self.assertEqual(self.tracker.status()["projects"][0]["frames"], {"ETRS89 (synthetic)": 2})
        self.tracker.compact()
        reloaded = tracking.Tracker(self.path)
        self.assertEqual(reloaded.status()["projects"][0]["frames"], {"ETRS89 (synthetic)": 2})
        # a checkpoint written before this change (summary without "frames") rebuilds lazily
        for line in reloaded.projects[0]["lines"]:
            line.get("_summary", {}).pop("frames", None)
        reloaded._project_frames.clear()
        self.assertEqual(reloaded.status()["projects"][0]["frames"], {"ETRS89 (synthetic)": 2})
        # The same without the status cache: the tally is rebuilt from the summaries.
        reloaded._status_cache = None
        for line in reloaded.projects[0]["lines"]:
            line.get("_summary", {}).pop("frames", None)
        reloaded._project_frames.clear()
        self.assertEqual(reloaded.status()["projects"][0]["frames"], {"ETRS89 (synthetic)": 2})
        self.assertEqual(reloaded.projects[0]["lines"][0]["_summary"]["frames"], {"ETRS89 (synthetic)": 2})

    def _frame_project(self, name="Frames"):
        """Two RTK vertices under a configured frame, one DGPS vertex, one unknown asset."""
        project = self.tracker.create_project(name)
        with mock.patch.dict(cfg.CONFIG, {"reference_frame": "ETRS89 (synthetic)"}):
            self.tracker.start_line("L", 1, fix=FIX)
            self.tracker.add_vertex(dict(FIX, lon_e8=800002000))
        with mock.patch.dict(cfg.CONFIG, {"measurement_required_fix": "DGPS"}):
            self.tracker.add_vertex(dict(FIX, lon_e8=800004000, qual=2, fix_status_text="DGPS"))
        self.tracker.add_asset("tap", "U", "", FIX, link_to_active_line=False)
        return project

    def test_status_and_live_status_read_the_kept_tally_without_scanning_lines(self):
        project = self._frame_project()
        expected = {"ETRS89 (synthetic)": 2, "WGS84/ITRF": 1, "unknown": 1}
        self.assertEqual(self.tracker.status()["projects"][0]["frames"], expected)
        self.tracker._status_cache = None
        with mock.patch.object(self.tracker, "_iter_line_summary",
                               side_effect=AssertionError("line scan")), \
                mock.patch.object(self.tracker, "_line_length",
                                  side_effect=AssertionError("line scan")):
            summary = self.tracker._project_summary(self.tracker._project(project["id"]))
            live = self.tracker.live_status()["active_project"]
        self.assertEqual(summary["frames"], expected)
        self.assertTrue(summary["mixed_frames"]); self.assertFalse(summary["unknown_frames"])
        self.assertEqual((live["frames"], live["mixed_frames"], live["unknown_frames"]),
                         (expected, True, False))
        # The answer is a copy: a caller cannot change the kept tally.
        summary["frames"]["unknown"] = 99
        self.assertEqual(self.tracker._project_frames[project["id"]], expected)

    def test_vertex_undo_decrements_the_tally_and_drops_zero_keys(self):
        project = self._frame_project()
        self.tracker.undo_last()                                     # the unknown asset
        self.tracker.undo_last()                                     # the DGPS vertex
        self.assertEqual(self.tracker._project_frames[project["id"]], {"ETRS89 (synthetic)": 2})
        self.assertEqual(self.tracker.status()["projects"][0]["frames"], {"ETRS89 (synthetic)": 2})
        self.assertFalse(self.tracker.status()["projects"][0]["mixed_frames"])
        # The line summary was dropped by the undo; the next vertex rebuilds it once.
        with mock.patch.dict(cfg.CONFIG, {"reference_frame": "ETRS89 (synthetic)"}):
            self.tracker.add_vertex(dict(FIX, lon_e8=800006000))
        line = self.tracker.projects[0]["lines"][0]
        self.assertEqual(line["_summary"]["frames"], {"ETRS89 (synthetic)": 3})
        self.assertEqual(self.tracker.status()["projects"][0]["frames"], {"ETRS89 (synthetic)": 3})

    def test_summary_without_frames_is_rebuilt_on_the_next_vertex_not_double_counted(self):
        self._frame_project()
        line = self.tracker.projects[0]["lines"][0]
        line["_summary"].pop("frames")                               # written before this change
        self.tracker.add_vertex(dict(FIX, lon_e8=800006000))
        self.assertEqual(line["_summary"]["frames"], {"ETRS89 (synthetic)": 2, "WGS84/ITRF": 1,
                                                      "unknown": 1})
        self.assertEqual(line["_summary"]["vertex_count"], 4)
        self.assertEqual(self.tracker.status()["projects"][0]["frames"],
                         {"ETRS89 (synthetic)": 2, "WGS84/ITRF": 1, "unknown": 2})

    def test_cached_first_summary_of_a_restored_line_never_seeds_the_tally(self):
        # A backup carries the line's cached summary into line_started; its counts
        # never leak into the tally of the restored or newly recorded vertices.
        self.tracker.create_project("Seeded")
        self.tracker.start_line("L", 1)
        line = self.tracker.projects[0]["lines"][0]
        line["_summary"] = {"vertex_count": 0, "length_m": 0.0, "quality_accepted_count": 0,
                            "large_gap_count": 0, "max_gap_m": 0.0, "large_vertical_step_count": 0,
                            "max_vertical_step_m": 0.0, "overlapping": False,
                            "legacy_vertex_count": 0, "frames": {"Invented": 5}}
        self.tracker._project_frames.clear()
        self.assertEqual(self.tracker.status()["projects"][0]["frames"], {})
        self.tracker.add_vertex(FIX)
        self.assertEqual(line["_summary"]["frames"], {"unknown": 1})

    def test_project_delete_restore_and_reactivation_keep_the_tally(self):
        first = self._frame_project("First")
        self.tracker.set_line_state("finish")
        second = self.tracker.create_project("Second")
        self.tracker.add_asset("tap", "T", "", FIX, link_to_active_line=False)
        expected = {"ETRS89 (synthetic)": 2, "WGS84/ITRF": 1, "unknown": 1}
        frames = lambda tracker: dict((item["id"], item["frames"])
                                      for item in tracker.status()["projects"])
        self.assertEqual(frames(self.tracker), {first["id"]: expected, second["id"]: {"unknown": 1}})
        backup = list(self.tracker.iter_backup_ndjson())
        archived = self.tracker.archive_project(first["id"])
        self.assertNotIn(first["id"], self.tracker._project_frames)
        self.assertEqual(frames(self.tracker), {second["id"]: {"unknown": 1}})
        self.tracker.reactivate_archive(archived["id"])
        self.assertEqual(frames(self.tracker), {first["id"]: expected, second["id"]: {"unknown": 1}})
        self.tracker.select_project(second["id"])
        self.tracker.delete_project(second["id"])
        self.assertNotIn(second["id"], self.tracker._project_frames)
        self.assertEqual(frames(self.tracker), {first["id"]: expected})
        # A restore replaces every tally, including ones for projects it does not contain.
        self.tracker.restore_ndjson(iter(backup))
        self.assertEqual(frames(self.tracker), {first["id"]: expected, second["id"]: {"unknown": 1}})
        other = tracking.Tracker(os.path.join(self.tmp.name, "other.jsonl"))
        other.restore_ndjson(iter(backup))
        self.assertEqual(frames(other), {first["id"]: expected, second["id"]: {"unknown": 1}})
        reloaded = tracking.Tracker(self.path)
        self.assertEqual(reloaded._project_frames, {first["id"]: expected, second["id"]: {"unknown": 1}})

    def test_line_export_tally_equals_the_summary_tally(self):
        self._frame_project()
        self.tracker.set_line_state("finish")
        self.tracker.start_line("Override", 1, {"measurements": "external"}, fix=FIX)
        self.tracker.add_vertex(dict(FIX, lon_e8=800002000))
        lines = self.tracker.projects[0]["lines"]
        expected = [dict(line["_summary"]["frames"]) for line in lines]
        self.assertEqual(expected, [{"ETRS89 (synthetic)": 2, "WGS84/ITRF": 1}, {"unknown": 2}])
        for stale in (False, True):
            if stale:                       # e.g. after an undo: the export counts by itself
                for line in lines:
                    line.pop("_summary", None)
            # A current summary answers for the override line without reading its vertices.
            with mock.patch.object(self.tracker, "_count_line_frames", wraps=self.tracker._count_line_frames) as scan, \
                    mock.patch.object(self.tracker, "_count_row_frame", wraps=self.tracker._count_row_frame) as rows:
                streamed = json.loads("".join(self.tracker.iter_geojson()))["features"]
            # The export never rescans a line; a stale one counts its 2 vertices in the geometry pass.
            self.assertEqual((scan.call_count, rows.call_count), (0, 2 if stale else 0))
            direct = tracking_reference.geojson(self.tracker)["features"]
            self.assertEqual([f["properties"]["reference_frames"] for f in streamed[:2]], expected)
            self.assertEqual([f["properties"]["reference_frames"] for f in direct[:2]], expected)
            if stale:                       # the export does not rebuild a summary
                self.assertEqual([line.get("_summary") for line in lines], [None, None])
        self.assertEqual([self.tracker._line_frames(line) for line in lines], expected)

    def test_gate_deviations_list_only_looser_limits(self):
        self.assertEqual(tracking.measurement_gate_deviations(), [])
        with mock.patch.dict(cfg.CONFIG, {"measurement_required_fix": "DGPS", "measurement_max_hdop": 2.0,
                                          "measurement_max_gst_horizontal_sigma_m": 2.0,
                                          "measurement_quality_window_samples": 3,
                                          "measurement_min_satellites": 12}):   # tighter, not a deviation
            deviations = tracking.measurement_gate_deviations()
        self.assertEqual([item["key"] for item in deviations],
                         ["required_fix", "max_hdop", "max_gst_horizontal_sigma_m", "quality_window_samples"])
        self.assertEqual(deviations[0], {"key": "required_fix", "value": "DGPS", "default": "RTK_FIXED"})

    def test_every_gate_key_has_a_looser_and_a_tighter_side(self):
        looser = {"measurement_required_fix": "RTK_FLOAT", "measurement_min_satellites": 9,
                  "measurement_max_hdop": 1.6, "measurement_max_correction_age_sec": 6.0,
                  "measurement_max_horizontal_spread_m": 0.06, "measurement_max_gst_horizontal_sigma_m": 0.06,
                  "measurement_max_gst_vertical_sigma_m": 0.11, "measurement_quality_window_samples": 4}
        tighter = {"measurement_required_fix": "RTK_FIXED", "measurement_min_satellites": 11,
                   "measurement_max_hdop": 1.4, "measurement_max_correction_age_sec": 4.0,
                   "measurement_max_horizontal_spread_m": 0.04, "measurement_max_gst_horizontal_sigma_m": 0.04,
                   "measurement_max_gst_vertical_sigma_m": 0.09, "measurement_quality_window_samples": 6}
        with mock.patch.dict(cfg.CONFIG, looser):
            self.assertEqual(len(tracking.measurement_gate_deviations()), 8)
        with mock.patch.dict(cfg.CONFIG, tighter):
            self.assertEqual(tracking.measurement_gate_deviations(), [])

    def test_records_line_and_parallel_asset(self):
        project = self.tracker.create_project("Water line")
        line = self.tracker.start_line("WL-01", 1, {"material": "PE"}, FIX)
        moved = dict(FIX, lon=8.00002, lon_e8=800002000)
        self.assertTrue(self.tracker.on_fix(moved))
        point = self.tracker.add_asset("tap", "T-01", "Pasture", moved)
        self.assertEqual(point["line_id"], line["id"])
        self.assertGreater(point["chainage_m"], 1)
        self.assertEqual(self.tracker.status()["active_project_id"], project["id"])

    def test_asset_can_be_recorded_independently_while_line_is_active(self):
        self.tracker.create_project("Markers")
        self.tracker.start_line("Optional relation", 0, fix=FIX)
        point = self.tracker.add_asset("other", "Tree", "", FIX,
                                       link_to_active_line=False)
        self.assertIsNone(point["line_id"])
        self.assertIsNone(point["chainage_m"])

    def test_gga_parser_height_reaches_recorded_coordinate(self):
        sentence = ("$GNGGA,123519.00,5000.0000,N,00800.0000,E,4,20,0.6,"
                    "123.456,M,46.9,M,0.5,0054*00")
        fix = gnss.GNSSHandler.parse_gga(object(), sentence)
        fix["receiver_accuracy"] = FIX["receiver_accuracy"]
        self.tracker.create_project("Parser integration")
        self.tracker.start_line("Height", 0, fix=fix)
        coordinate = self.tracker.map_data()["lines"][0]["coordinates"][0]
        self.assertEqual(coordinate, [800000000, 5000000000, 123456])

    def test_coordinate_is_the_exact_integer_triple(self):
        self.assertEqual(self.tracker._coordinate(FIX), [800000000, 5000000000, 123400])
        with self.assertRaisesRegex(ValueError, "exact position"):
            self.tracker._coordinate(dict(FIX, lat_e8=None))
        with self.assertRaisesRegex(ValueError, "exact position"):
            self.tracker._coordinate({"lat": 50.0, "lon": 8.0, "alt": 1.0})
        for bad in (dict(FIX, lat_e8=50.0), dict(FIX, lon_e8=True), dict(FIX, alt_mm=100000001),
                    dict(FIX, lat_e8=9000000001)):
            with self.assertRaisesRegex(ValueError, r"coordinate\["):
                self.tracker._coordinate(bad)
        self.tracker.create_project("Assets")
        # The gate rejects before _coordinate runs, so the user sees the gate reason.
        with self.assertRaisesRegex(ValueError, "no_position"):
            self.tracker.add_asset("tap", "T", "", dict(FIX, lat_e8=50.0), link_to_active_line=False)
        # A start position without exact fields must not leave an empty line behind.
        with self.assertRaisesRegex(ValueError, "no_position"):
            self.tracker.start_line("Bad start", 1, fix=dict(FIX, lon_e8=None))
        with self.assertRaisesRegex(ValueError, "altitude_required"):
            self.tracker.start_line("Bad start", 1, fix=dict(FIX, alt_mm=123400.0))
        self.assertEqual(self.tracker.projects[0]["lines"], [])
        # Every writer is gated and goes through _coordinate, so none stores a lossy position.
        self.tracker.start_line("Line", 1)
        with self.assertRaisesRegex(ValueError, "no_position"):
            self.tracker.add_vertex(dict(FIX, lon_e8=800002000.0))
        self.assertFalse(self.tracker.on_fix(dict(FIX, utc="1", lon_e8=800002000.5)))
        self.assertFalse(self.tracker.enqueue_fix(dict(FIX, utc="2", lat_e8=True)))
        self.assertEqual(self.tracker.feature_count(), 0)

    def test_schema5_coordinate_validation_names_the_component(self):
        tracking._validate_coordinate([800000000, 5000000000, 123400])
        for limit in ([18000000000, 9000000000, 100000000], [-18000000000, -9000000000, -20000000]):
            tracking._validate_coordinate(limit)
        for bad, text in (([8.0, 50.0, 123.4], r"coordinate\[0\]"), ([800000000, True, 1], r"coordinate\[1\]"),
                          ([18000000001, 0, 0], r"coordinate\[0\]"), ([0, 0, 100000001], r"coordinate\[2\]"),
                          ([0, -9000000001, 0], r"coordinate\[1\]"), ([0, 0, -20000001], r"coordinate\[2\]"),
                          ([0, 0, 1.5], r"coordinate\[2\]"), ([0, 0], "coordinate"), (None, "coordinate")):
            with self.assertRaisesRegex(ValueError, text):
                tracking._validate_coordinate(bad)
        with self.assertRaisesRegex(ValueError, r"projected_coordinate\[1\]"):
            tracking._validate_coordinate([0, 0.5, 0], "projected_coordinate")
        tracking._validate_legacy_coordinate([8.0, 50.0, 123.4])
        for bad in ([8.0, 95.0, 1.0], [181, 50, 1], [8, 50], [8.0, 50.0, float("nan")], [8, 50, True]):
            with self.assertRaises(ValueError):
                tracking._validate_legacy_coordinate(bad)

    def test_schema_constants_and_config_keys_for_schema5(self):
        self.assertEqual(tracking.SCHEMA_VERSION, 5)
        self.assertEqual(tracking.LEGACY_SCHEMA_VERSIONS, (2, 3, 4))
        for key, value in (("reference_frame", ""), ("reference_frame_epoch", ""), ("antenna_height_m", 0.0)):
            self.assertEqual(cfg.DEFAULT_CONFIG[key], value, key)
            self.assertEqual(cfg.CONFIG[key], value, key)

    def test_measurement_carries_schema5_fields(self):
        cfg.CONFIG["reference_frame"] = "ETRS89 (synthetic)"
        cfg.CONFIG["antenna_height_m"] = 1.8
        try:
            with mock.patch.dict(state.app.stats, {"ntrip_state": "streaming", "ntrip_endpoint": "fallback"}), \
                    mock.patch.dict(cfg.CONFIG, {"ntrip_mount": "PRIMARY1", "ntrip_fallback_mount": "FALLBACK1"}):
                measurement = self.tracker._measurement(FIX)
        finally:
            cfg.CONFIG["reference_frame"] = ""
            cfg.CONFIG["antenna_height_m"] = 0.0
        self.assertEqual(measurement["precision"], "e8")
        self.assertEqual(measurement["geoid_sep_mm"], 45600)
        self.assertEqual(measurement["alt_ellipsoid_mm"], 169000)
        self.assertEqual(measurement["reference_frame"], "ETRS89 (synthetic)")
        self.assertEqual(measurement["ntrip_mount"], "FALLBACK1")
        self.assertEqual(measurement["antenna_height_mm"], 1800)
        self.assertEqual(list(measurement)[-7:], ["precision", "geoid_sep_mm", "alt_ellipsoid_mm",
                                                   "reference_frame", "reference_frame_epoch", "ntrip_mount",
                                                   "antenna_height_mm"])
        self.assertEqual(list(measurement)[-8], "quality")
        self.assertIsNone(measurement["reference_frame_epoch"])        # not configured in this test
        with mock.patch.dict(cfg.CONFIG, {"reference_frame": "ETRS89 (synthetic)", "reference_frame_epoch": "2010.0"}):
            self.assertEqual(self.tracker._measurement(FIX)["reference_frame_epoch"], "2010.0")
            # A plain GPS fix is rejected by the default RTK gate, so a route-style call keeps it.
            self.assertIsNone(self.tracker._measurement(dict(FIX, qual=2), allow_rejected=True)
                              ["reference_frame_epoch"])
        self.assertIsNone(self.tracker._measurement(dict(FIX, geoid_sep_mm=None))["alt_ellipsoid_mm"])
        self.assertIsNone(self.tracker._measurement(dict(FIX, geoid_sep_mm=None))["geoid_sep_mm"])
        # Defaults: nothing configured, no caster streaming.
        with mock.patch.dict(state.app.stats, {"ntrip_state": "disabled"}):
            plain = self.tracker._measurement(FIX)
        self.assertEqual((plain["reference_frame"], plain["ntrip_mount"], plain["antenna_height_mm"]),
                         (None, None, 0))
        for key, item in plain.items():
            if key in ("geoid_sep_mm", "alt_ellipsoid_mm", "antenna_height_mm"):
                self.assertTrue(item is None or type(item) is int, key)
        tracking._validate_measurement(plain)

    def test_reference_frame_and_mount_follow_quality_and_stream_state(self):
        self.assertEqual(tracking._reference_frame(dict(FIX, qual=2)), "WGS84/ITRF")
        self.assertEqual(tracking._reference_frame(dict(FIX, qual=1)), "WGS84/ITRF")
        self.assertIsNone(tracking._reference_frame(dict(FIX, qual=0)))
        self.assertIsNone(tracking._reference_frame(dict(FIX, qual=4)))  # unconfigured
        self.assertIsNone(tracking._reference_frame_epoch(dict(FIX, qual=4)))
        with mock.patch.dict(cfg.CONFIG, {"reference_frame": "ETRS89 (synthetic)", "reference_frame_epoch": "2010.0"}):
            for qual in (4, 5):
                self.assertEqual(tracking._reference_frame(dict(FIX, qual=qual)), "ETRS89 (synthetic)")
                self.assertEqual(tracking._reference_frame_epoch(dict(FIX, qual=qual)), "2010.0")
            self.assertEqual(tracking._reference_frame(dict(FIX, qual=2)), "WGS84/ITRF")
            for qual in (0, 1, 2):
                self.assertIsNone(tracking._reference_frame_epoch(dict(FIX, qual=qual)))
        with mock.patch.dict(state.app.stats, {"ntrip_state": "backoff", "ntrip_endpoint": "primary"}), \
                mock.patch.dict(cfg.CONFIG, {"ntrip_mount": "PRIMARY1"}):
            self.assertIsNone(tracking._ntrip_mount())
        with mock.patch.dict(state.app.stats, {"ntrip_state": "streaming", "ntrip_endpoint": "primary"}), \
                mock.patch.dict(cfg.CONFIG, {"ntrip_mount": "PRIMARY1", "ntrip_fallback_mount": "FALLBACK1"}):
            self.assertEqual(tracking._ntrip_mount(), "PRIMARY1")
        with mock.patch.dict(state.app.stats, {"ntrip_state": "streaming", "ntrip_endpoint": "fallback"}), \
                mock.patch.dict(cfg.CONFIG, {"ntrip_mount": "PRIMARY1", "ntrip_fallback_mount": "FALLBACK1"}):
            self.assertEqual(tracking._ntrip_mount(), "FALLBACK1")
        with mock.patch.dict(state.app.stats, {"ntrip_state": "streaming", "ntrip_endpoint": "fallback"}), \
                mock.patch.dict(cfg.CONFIG, {"ntrip_fallback_mount": ""}):
            self.assertIsNone(tracking._ntrip_mount())

    def test_auto_distance_suppresses_close_points(self):
        self.tracker.create_project("Test")
        self.tracker.start_line("Line", 10, fix=FIX)
        self.assertFalse(self.tracker.on_fix(dict(FIX, lon=8.000001, lon_e8=800000100)))
        self.assertEqual(self.tracker.status()["active_line"]["vertex_count"], 1)

    def test_automatic_queue_deduplicates_epochs_and_is_bounded(self):
        self.tracker.create_project("Test")
        self.tracker.start_line("Line", 1, fix=dict(FIX, utc="1"))
        first = dict(FIX, utc="2", lon=8.00002, lon_e8=800002000)
        self.assertTrue(self.tracker.enqueue_fix(first))
        self.assertFalse(self.tracker.enqueue_fix(dict(first)))
        for index in range(3, 66):
            self.assertTrue(self.tracker.enqueue_fix(
                dict(FIX, utc=str(index), lon=8.0 + index * 0.00002,
                     lon_e8=800000000 + index * 2000)))
        self.assertEqual(len(self.tracker._pending_fixes), 64)
        self.assertIsNone(self.tracker.enqueue_fix(
            dict(FIX, utc="overflow", lon=8.01, lon_e8=801000000)))

    def test_capacity_levels_and_automatic_limit_pause(self):
        with support.patch_module_global("tracking", "MAX_FEATURES", 2):
            self.tracker.create_project("Capacity")
            self.tracker.start_line("Line", 1, fix=FIX)
            self.tracker.add_vertex(dict(FIX, lon=8.00002, lon_e8=800002000), source="automatic")
            capacity = self.tracker.capacity()
            self.assertEqual({key: capacity[key] for key in (
                "used", "limit", "remaining", "percent", "level")}, {
                "used": 2, "limit": 2, "remaining": 0,
                "percent": 100, "level": "full"})
            self.assertIn("required_flash_reserve_bytes", capacity)
            self.assertIn("estimated_temporary_bytes", capacity)
            with self.assertRaisesRegex(ValueError, "point limit"):
                self.tracker.add_vertex(dict(FIX, lon=8.00004, lon_e8=800004000),
                                        source="automatic")
            active = self.tracker.live_status()["active_line"]
            self.assertEqual(active["state"], "paused")
            self.assertEqual(active["pause_reason"], "capacity_limit")
            with self.assertRaisesRegex(ValueError, "Archive or delete"):
                self.tracker.set_line_state("resume")
            self.tracker.undo_last()
            self.tracker.set_line_state("resume")
            self.assertEqual(self.tracker.live_status()["active_line"]["state"],
                             "recording")

    def test_absolute_capacity_warning_boundaries_for_target_limit(self):
        expected = ((7999, "ok"), (8000, "notice"), (8999, "notice"),
                    (9000, "warning"), (9799, "warning"), (9800, "critical"),
                    (9999, "critical"), (10000, "full"))
        with support.patch_module_global("tracking", "MAX_FEATURES", 10000), \
                mock.patch.object(self.tracker, "feature_count") as count:
            for used, level in expected:
                count.return_value = used
                self.assertEqual(self.tracker.capacity()["level"], level)

    def test_flash_reserve_pauses_automatic_line_without_appending_point(self):
        self.tracker.create_project("Reserve")
        self.tracker.start_line("Line", 1, fix=FIX)
        low = {"free_flash_bytes": 100, "required_flash_reserve_bytes": 100,
               "estimated_temporary_bytes": 1, "writable": False}
        with mock.patch.object(self.tracker, "storage_capacity", return_value=low):
            with self.assertRaisesRegex(tracking.StorageReserveError, "Flash reserve"):
                self.tracker.add_vertex(dict(FIX, lon=8.00002, lon_e8=800002000), "automatic")
        self.assertTrue(issubclass(tracking.StorageReserveError, ValueError))
        self.assertEqual(self.tracker.feature_count(), 1)
        self.assertEqual(self.tracker.live_status()["active_line"]["pause_reason"],
                         "storage_reserve")

    def test_archive_reserve_refusal_is_a_storage_reserve_error(self):
        project = self.tracker.create_project("Reserve")
        self.tracker.start_line("L", 1, fix=FIX)
        self.tracker.add_vertex(dict(FIX, lon_e8=800002000))
        self.tracker.set_line_state("finish")
        before = ([(p["id"], p["name"]) for p in self.tracker.projects],
                  self.tracker.feature_count())
        low = {"free_flash_bytes": 100, "required_flash_reserve_bytes": 100,
               "estimated_temporary_bytes": 1, "writable": False}
        with mock.patch.object(self.tracker, "storage_capacity", return_value=low):
            with self.assertRaisesRegex(
                    tracking.StorageReserveError,
                    "Not enough flash to archive while preserving the reserve."):
                self.tracker.archive_project(project["id"])
        self.assertEqual(([(p["id"], p["name"]) for p in self.tracker.projects],
                          self.tracker.feature_count()), before)
        self.assertEqual(self.tracker.archives, [])
        left = os.listdir(self.tracker.archive_dir) if os.path.isdir(self.tracker.archive_dir) else []
        self.assertEqual([name for name in left if name.endswith((".ndjson", ".tmp"))], [])

    def test_flash_reserve_math_uses_available_blocks_and_temporary_space(self):
        self.storage_patch.stop()
        try:
            # 100 blocks total, 25 available: a six-block temporary write
            # would leave 19%, below the required 20% reserve.
            with mock.patch.object(tracking.os, "statvfs",
                                   return_value=(4096, 4096, 100, 30, 25,
                                                 0, 0, 0, 0, 255)):
                capacity = self.tracker.storage_capacity(6 * 4096)
            self.assertEqual(capacity["free_flash_bytes"], 25 * 4096)
            self.assertEqual(capacity["required_flash_reserve_bytes"], 20 * 4096)
            self.assertFalse(capacity["writable"])
        finally:
            self.storage_patch.start()

    def test_flash_estimate_reserves_log_and_segment_overlap(self):
        self.storage_patch.stop()
        try:
            with mock.patch.object(tracking.os, "statvfs",
                    return_value=(4096, 4096, 1000, 800, 800, 0, 0, 0, 0, 255)):
                capacity = self.tracker.storage_capacity(1234)
            self.assertEqual(capacity["estimated_temporary_bytes"],
                1234 + tracking.OPERATIONAL_FLASH_HEADROOM_BYTES)
        finally:
            self.storage_patch.start()

    def test_charge_storage_counts_whole_blocks_of_the_sample_plus_one(self):
        self.storage_patch.stop()
        try:
            def volume(block, free):
                return (block, block, 100000, free, free, 0, 0, 0, 0, 255)

            with mock.patch.object(tracking.os, "statvfs",
                                   return_value=volume(4096, 1000)) as stat:
                free = self.tracker.storage_capacity(force=True)["free_flash_bytes"]
                self.assertEqual(free, 1000 * 4096)
                # On top of a budget other writes already used.
                self.tracker._storage_budget_used = 2048
                self.tracker.charge_storage(4097)     # two blocks plus one
                self.assertEqual(self.tracker.storage_capacity()["free_flash_bytes"],
                                 free - 2048 - 3 * 4096)
                self.tracker.charge_storage(0)        # the metadata block alone
                self.assertEqual(self.tracker.storage_capacity()["free_flash_bytes"],
                                 free - 2048 - 4 * 4096)
                self.assertEqual(stat.call_count, 1)
                # A forced sample starts the budget over.
                self.tracker.storage_capacity(force=True)
                self.tracker.charge_storage(8192)
                self.assertEqual(self.tracker.storage_capacity()["free_flash_bytes"],
                                 free - 3 * 4096)
            # An intermediate sample with another block size: the charge
            # uses the block size of the sample it is counted against.
            with mock.patch.object(tracking.os, "statvfs", return_value=volume(512, 8000)):
                self.tracker.storage_capacity(force=True)
                self.tracker.charge_storage(1000)
                self.assertEqual(self.tracker.storage_capacity()["free_flash_bytes"],
                                 8000 * 512 - 3 * 512)
        finally:
            self.storage_patch.start()

    def test_charge_without_a_sample_waits_for_the_next_measurement(self):
        self.storage_patch.stop()
        try:
            with mock.patch.object(tracking.os, "statvfs",
                                   return_value=(4096, 4096, 1000, 800, 800,
                                                 0, 0, 0, 0, 255)) as stat:
                self.tracker.charge_storage(100000)
                self.assertEqual(stat.call_count, 0)
                self.assertEqual(self.tracker.storage_capacity()["free_flash_bytes"],
                                 800 * 4096)
                self.tracker.charge_storage(4096)
                self.tracker.invalidate_storage_sample()
                # The next check measures again, without the old charge.
                self.assertEqual(self.tracker.storage_capacity()["free_flash_bytes"],
                                 800 * 4096)
                self.assertEqual(stat.call_count, 2)
        finally:
            self.storage_patch.start()

    def test_flash_query_is_cached_for_configured_append_window(self):
        self.storage_patch.stop()
        try:
            with mock.patch.object(tracking.os, "statvfs",
                    return_value=(4096, 4096, 1000, 800, 800, 0, 0, 0, 0, 255)) as stat:
                for _ in range(tracking.FLASH_RECHECK_WRITES + 1):
                    self.tracker._require_write_capacity()
                self.assertEqual(stat.call_count, 1)
                self.tracker._require_write_capacity()
                self.assertEqual(stat.call_count, 2)
        finally:
            self.storage_patch.start()

    def test_append_profile_reports_individual_phases(self):
        self.tracker.create_project("Profile")
        self.tracker.start_line("Line", 0, fix=FIX)
        profile = self.tracker.append_profile()
        self.assertGreaterEqual(profile["append_count"], 3)
        for phase in ("json_encode", "journal_write", "journal_flush",
                      "pointstore_write", "pointstore_flush", "statvfs",
                      "quality_check", "profile_calculation", "index"):
            self.assertIn(phase, profile)

    def test_index_waits_for_recording_idle_but_each_point_is_durable(self):
        self.tracker.create_project("Idle index")
        self.tracker.start_line("Line", 0, fix=FIX)
        with mock.patch.object(self.tracker, "_write_index") as index, \
                mock.patch.object(self.tracker.point_store, "sync",
                                  wraps=self.tracker.point_store.sync) as flush:
            for n in range(1, 21):
                with mock.patch.object(tracking.time, "ticks_ms", return_value=n * 1000):
                    self.tracker.add_vertex(dict(FIX, lon=8.0 + n * 0.00002,
                                                 lon_e8=800000000 + n * 2000))
                self.assertEqual(self.tracker._index_dirty_since, n * 1000)
                self.assertEqual(flush.call_count, n)
            index.assert_not_called()
        restored = tracking.Tracker(self.path)
        try:
            self.assertEqual(restored.feature_count(), 21)
            self.assertEqual(restored.map_data()["lines"][0]["coordinates"],
                             self.tracker.map_data()["lines"][0]["coordinates"])
        finally:
            restored.point_store.close()

    def test_profile_separates_total_time_from_worst_single_operation(self):
        with mock.patch.object(self.tracker, "_ticks_us", side_effect=(110, 350)):
            self.tracker._profile_add("test_phase", 100)
            self.tracker._profile_add("test_phase", 100)
        profile = self.tracker.append_profile()
        self.assertEqual(profile["test_phase"], 260)
        self.assertEqual(profile["phase_max_us"]["test_phase"], 250)

    def test_profiles_are_deduplicated_and_historical_switch_is_preserved(self):
        saved = dict(cfg.CONFIG)
        try:
            self.tracker.create_project("Profiles")
            self.tracker.start_line("Line", 0, fix=FIX)
            first = self.tracker.projects[0]["lines"][0]["measurements"][0]
            cfg.CONFIG["measurement_max_hdop"] = 1.25
            self.tracker.add_vertex(dict(FIX, lon=8.00002, lon_e8=800002000))
            second = self.tracker.projects[0]["lines"][0]["measurements"][1]
            self.assertNotEqual(first["profile_id"], second["profile_id"])
            exported = self.tracker.backup()["projects"][0]["lines"][0]["measurements"]
            self.assertEqual(exported[0]["quality"]["limits"]["max_hdop"], 1.5)
            self.assertEqual(exported[1]["quality"]["limits"]["max_hdop"], 1.25)
        finally:
            cfg.CONFIG.clear(); cfg.CONFIG.update(saved)

    def test_archive_is_verified_removed_from_ram_and_reactivatable(self):
        project = self.tracker.create_project("Archive")
        self.tracker.start_line("Line", 0, fix=FIX)
        self.tracker.set_line_state("finish")
        metadata = self.tracker.archive_project(project["id"])
        self.assertEqual(self.tracker.feature_count(), 0)
        self.assertTrue(self.tracker.verify_archive(metadata["id"])["valid"])
        self.assertEqual(self.tracker.capacity()["archived_projects"], 1)
        restored = self.tracker.reactivate_archive(metadata["id"])
        self.assertEqual(restored["id"], project["id"])
        self.assertEqual(self.tracker.feature_count(), 1)

    def test_archive_other_project_preserves_capacity_paused_line_after_reload(self):
        with support.patch_module_global("tracking", "MAX_FEATURES", 3):
            old = self.tracker.create_project("Previous trip")
            self.tracker.start_line("Finished trip", 1, fix=FIX)
            self.tracker.add_vertex(dict(FIX, lon=8.00002, lon_e8=800002000))
            self.tracker.set_line_state("finish")
            current = self.tracker.create_project("Current trip")
            line = self.tracker.start_line("Continue this", 1, fix=FIX)
            with self.assertRaises(ValueError):
                self.tracker.add_vertex(dict(FIX, lon=8.00004, lon_e8=800004000), source="automatic")
            self.assertEqual(self.tracker.capacity()["used"], 3)
            summary = self.tracker.status()
            self.assertEqual([p["feature_count"] for p in summary["projects"]], [2, 1])
            self.assertEqual(summary["active_line"]["pause_reason"], "capacity_limit")
            with self.assertRaises(ValueError):
                self.tracker.archive_project(current["id"])
            archived = self.tracker.archive_project(old["id"])
            self.assertTrue(self.tracker.verify_archive(archived["id"])["valid"])
            restored = tracking.Tracker(self.path)
            self.assertEqual(restored.active_project_id, current["id"])
            self.assertEqual(restored.active_line_id, line["id"])
            self.assertEqual(restored.status()["active_line"]["state"], "paused")
            self.assertEqual(restored.capacity()["remaining"], 2)
            self.assertEqual(restored.status()["active_line"]["vertex_count"], 1)
            restored.set_line_state("resume")
            self.assertTrue(restored.on_fix(dict(FIX, lon=8.00006, lon_e8=800006000)))
            self.assertEqual(restored.status()["active_line"]["vertex_count"], 2)

    def test_corrupt_archive_is_rejected_without_changing_active_data(self):
        project = self.tracker.create_project("Corrupt archive")
        self.tracker.start_line("Line", 0, fix=FIX)
        self.tracker.set_line_state("finish")
        metadata = self.tracker.archive_project(project["id"])
        path = self.tracker.archive_dir + "/" + metadata["id"] + ".ndjson"
        with open(path, "a") as target:
            target.write("{}\n")
        before = self.tracker.feature_count()
        self.assertFalse(self.tracker.verify_archive(metadata["id"])["valid"])
        with self.assertRaisesRegex(ValueError, "verification failed"):
            self.tracker.reactivate_archive(metadata["id"])
        self.assertEqual(self.tracker.feature_count(), before)

    def test_project_archive_contains_only_selected_project(self):
        first = self.tracker.create_project("First")
        self.tracker.start_line("L1", 0, fix=FIX)
        self.tracker.set_line_state("finish")
        second = self.tracker.create_project("Second")
        records = list(self.tracker.iter_backup_ndjson(second["id"]))
        restored = tracking_reference.parse_backup_ndjson(records)
        self.assertEqual([project["id"] for project in restored["projects"]],
                         [second["id"]])
        self.assertNotEqual(first["id"], second["id"])

    def test_pause_resume_finish(self):
        self.tracker.create_project("Test")
        self.tracker.start_line("Line", 0, fix=FIX)
        self.tracker.set_line_state("pause")
        self.assertEqual(self.tracker.status()["active_line"]["state"], "paused")
        self.tracker.set_line_state("resume")
        self.tracker.set_line_state("finish")
        self.assertIsNone(self.tracker.status()["active_line"])

    def test_journal_survives_reload(self):
        project = self.tracker.create_project("Test")
        self.tracker.start_line("Line", 0, fix=FIX)
        self.tracker.add_asset("valve", "V-01", "", FIX)
        restored = tracking.Tracker(self.path)
        self.assertEqual(restored.status()["active_project_id"], project["id"])
        self.assertEqual(restored.status()["active_line"]["vertex_count"], 1)

    def test_streaming_checkpoint_survives_reload_without_monolithic_json(self):
        self.tracker.create_project("Checkpoint")
        self.tracker.start_line("Line", 0, fix=FIX)
        self.tracker.add_vertex(dict(FIX, lon=8.00002, lon_e8=800002000))
        self.tracker.compact()
        with open(self.tracker.checkpoint_path, encoding="utf-8") as source:
            header = json.loads(source.readline())
        self.assertEqual(header["kind"], "survey_checkpoint_stream")
        self.assertEqual(header["segment"], 1)
        self.assertGreater(header["offset"], 0)
        restored = tracking.Tracker(self.path)
        self.assertEqual(restored.feature_count(), 2)
        self.assertEqual(restored.status()["active_line"]["vertex_count"], 2)

    def test_async_checkpoint_yields_and_remains_recoverable(self):
        import asyncio
        self.tracker.create_project("Async checkpoint")
        self.tracker.start_line("Line", 0, fix=FIX)
        with mock.patch.object(tracking.FlashPointStore, "_scan_generation",
                               side_effect=AssertionError("Blocking checkpoint scan")):
            asyncio.run(self.tracker.compact_async())
        restored = tracking.Tracker(self.path)
        self.assertEqual(restored.feature_count(), 1)
        self.assertFalse(os.path.exists(self.tracker.checkpoint_path + ".tmp"))

    def test_failed_async_checkpoint_verification_retains_journal(self):
        import asyncio
        self.tracker.create_project("Failed checkpoint")
        self.tracker.start_line("Line", 0, fix=FIX)
        journal = self.tracker._segment_path(self.tracker._segment_number)
        with mock.patch.object(tracking.Tracker, "_load_checkpoint", return_value=0):
            with self.assertRaisesRegex(ValueError, "journal retained"):
                asyncio.run(self.tracker.compact_async())
        self.assertTrue(os.path.exists(journal))
        restored = tracking.Tracker(self.path)
        try:
            self.assertEqual(restored.feature_count(), 1)
        finally:
            restored.point_store.close()

    def test_recorded_at_is_seconds_since_2000_on_every_platform(self):
        with mock.patch("tracking.time.time", return_value=1_777_000_000):   # host clock: 2026-04-24T03:06:40Z
            self.tracker.create_project("Time")
            self.tracker.add_asset("tap", "T", "", FIX, link_to_active_line=False)
        stored = self.tracker.projects[0]["points"][0]["measurement"]["recorded_at"]
        self.assertEqual(stored, 1_777_000_000 - 946_684_800)
        self.assertEqual(tracking._utc_fields(stored)[0], "2026-04-24T03:06:40Z")
        self.assertEqual(tracking._utc_fields(841057764)[0], "2026-08-26T11:09:24Z")   # a device value
        self.assertFalse(tracking._utc_fields(841057764)[1])
        self.assertEqual(self.tracker.projects[0]["created_at"], stored)
        with mock.patch("tracking.time.time", return_value=1_777_000_060):
            self.assertEqual(tracking._now_seconds(), stored + 60)
            self.assertEqual(self.tracker.status()["device_time"], stored + 60)
            self.assertEqual(self.tracker.live_status()["device_time"], stored + 60)
            self.assertEqual(self.tracker.status()["projects"][0]["updated_at"], stored)

    def test_utc_formatting_from_a_micropython_style_tuple(self):
        with support.patch_module_global("tracking", "_utc_tuple", return_value=(2026, 8, 26, 11, 9, 24, 2, 238)):
            self.assertEqual(tracking._utc_fields(841057764)[0], "2026-08-26T11:09:24Z")

    def test_exports_carry_utc_time_and_unsynced_flag(self):
        with mock.patch("tracking.time.time", return_value=1_777_000_000):
            self.tracker.create_project("Time")
            self.tracker.add_asset("tap", "T", "", FIX, link_to_active_line=False)
        feature = json.loads("".join(self.tracker.iter_geojson()))["features"][0]
        self.assertEqual(feature["properties"]["measurement"]["recorded_at_utc"], "2026-04-24T03:06:40Z")
        self.assertFalse(feature["properties"]["measurement"]["clock_unsynced"])
        self.assertEqual(json.loads("".join(self.tracker.iter_geojson())), json.loads(json.dumps(tracking_reference.geojson(self.tracker))))
        header = self.tracker.csv().splitlines()[0].split(",")
        self.assertEqual(header[header.index("recorded_at"):header.index("recorded_at") + 4],
                         ["recorded_at", "recorded_at_utc", "clock_unsynced", "note"])
        self.assertEqual(header[header.index("precision") + 1], "recorded_at")
        self.assertIn('"2026-04-24T03:06:40Z"', self.tracker.csv())
        row = self.tracker.csv().splitlines()[1]
        self.assertIn('"%d","2026-04-24T03:06:40Z","false",' % (1_777_000_000 - 946_684_800), row)
        row = next(json.loads(l) for l in self.tracker.iter_backup_ndjson() if '"record": "asset"' in l)
        self.assertEqual(row["point"]["measurement"]["recorded_at_utc"], "2026-04-24T03:06:40Z")
        self.assertIs(row["point"]["measurement"]["clock_unsynced"], False)
        # The backup keeps the stored frame (None), never the export text "unknown".
        self.assertIsNone(row["point"]["measurement"]["reference_frame"])
        self.assertEqual(self.tracker.backup()["projects"][0]["points"][0]["measurement"]
                         ["recorded_at_utc"], "2026-04-24T03:06:40Z")
        preview = self.tracker.map_data()
        self.assertEqual(preview["points"][0]["measurement"]["recorded_at_utc"], "2026-04-24T03:06:40Z")
        self.assertFalse(preview["points"][0]["measurement"]["clock_unsynced"])
        self.tracker.add_asset("tap", "U", "", FIX, link_to_active_line=False)
        delta = self.tracker.map_data(since_revision=preview["revision"])
        change = next(item for item in delta["changes"] if item["op"] == "asset")
        self.assertIn("recorded_at_utc", change["point"]["measurement"])
        self.assertIn("clock_unsynced", change["point"]["measurement"])

    def test_line_exports_carry_utc_time_per_vertex(self):
        with mock.patch("tracking.time.time", return_value=1_777_000_000):
            self.tracker.create_project("Time")
            self.tracker.start_line("Line", 0, fix=FIX)
        streamed = json.loads("".join(self.tracker.iter_geojson()))
        measurement = streamed["features"][0]["properties"]["measurements"][0]
        self.assertEqual(measurement["recorded_at_utc"], "2026-04-24T03:06:40Z")
        self.assertFalse(measurement["clock_unsynced"])
        self.assertEqual(tracking_reference.geojson(self.tracker)["features"][0]["properties"]["measurements"][0]
                         ["recorded_at_utc"], "2026-04-24T03:06:40Z")
        vertex = next(json.loads(l) for l in self.tracker.iter_backup_ndjson() if '"record": "vertex"' in l)
        self.assertEqual(vertex["measurement"]["recorded_at_utc"], "2026-04-24T03:06:40Z")
        self.assertEqual(self.tracker.backup()["projects"][0]["lines"][0]["measurements"][0]
                         ["clock_unsynced"], False)

    def test_device_backup_keeps_its_utc_text_on_the_host(self):
        self.tracker.create_project("Time")
        self.tracker.add_asset("tap", "T", "", FIX, link_to_active_line=False)
        payload = self.tracker.backup()
        payload["projects"][0]["points"][0]["measurement"]["recorded_at"] = 841057764
        other = tracking.Tracker(os.path.join(self.tmp.name, "device.jsonl"))
        other.restore(payload)
        exported = json.loads("".join(other.iter_geojson()))["features"][0]["properties"]["measurement"]
        self.assertEqual(exported["recorded_at_utc"], "2026-08-26T11:09:24Z")
        self.assertFalse(exported["clock_unsynced"])

    def test_invalid_or_unsynced_timestamps_do_not_crash(self):
        self.assertEqual(tracking._utc_fields(0)[0], "2000-01-01T00:00:00Z")
        self.assertTrue(tracking._utc_fields(0)[1])
        for bad in (None, True, -1, 1.5, "abc", 2 ** 40):
            self.assertIsNone(tracking._utc_fields(bad)[0], repr(bad))
            self.assertTrue(tracking._utc_fields(bad)[1], repr(bad))
        with support.patch_module_global("tracking", "_utc_tuple", side_effect=OverflowError):
            self.assertIsNone(tracking._utc_fields(841057764)[0])
            self.assertTrue(tracking._utc_fields(841057764)[1])
        with support.patch_module_global("tracking", "_utc_tuple", side_effect=OSError):
            self.assertIsNone(tracking._utc_fields(841057764)[0])

    def test_time_fields_are_added_in_place_on_the_fresh_copy(self):
        # One dict copy per backup row (D1): the expanded measurement is
        # already fresh, so the derived keys go onto it directly.
        measurement = {"recorded_at": 841057764, "fix_status": "RTK_FIXED"}
        self.assertIs(tracking._add_time_fields(measurement), measurement)
        self.assertEqual(list(measurement), ["recorded_at", "fix_status",
                                             "recorded_at_utc", "clock_unsynced"])
        self.assertEqual(measurement["recorded_at_utc"], "2026-08-26T11:09:24Z")
        self.tracker.create_project("Time")
        self.tracker.add_asset("tap", "T", "", FIX, link_to_active_line=False)
        stored = self.tracker.projects[0]["points"][0]["measurement"]
        list(self.tracker.iter_backup_ndjson()); self.tracker.backup()
        self.assertNotIn("recorded_at_utc", stored)

    def test_restore_refuses_a_timestamp_that_is_not_a_bounded_integer(self):
        self.tracker.create_project("Time")
        self.tracker.add_asset("tap", "T", "", FIX, link_to_active_line=False)
        for bad in (841057764.0, True, -1, 2 ** 40, "841057764"):
            with self.subTest(bad=bad):
                payload = self.tracker.backup()
                payload["projects"][0]["points"][0]["measurement"]["recorded_at"] = bad
                other = tracking.Tracker(os.path.join(self.tmp.name, "bad.jsonl"))
                with self.assertRaisesRegex(ValueError, "recorded_at"):
                    other.restore(payload)
                rows = [json.loads(line) for line in self.tracker.iter_backup_ndjson()]
                for row in rows:
                    if row.get("record") == "asset":
                        row["point"]["measurement"]["recorded_at"] = bad
                with self.assertRaisesRegex(ValueError, "recorded_at"):
                    other.restore_ndjson(self._with_integrity(
                        [r for r in rows if r.get("record") != "integrity"]).splitlines(keepends=True))
        payload = self.tracker.backup()
        payload["projects"][0]["points"][0]["measurement"]["recorded_at"] = 2 ** 40 - 1
        tracking.Tracker(os.path.join(self.tmp.name, "edge.jsonl")).restore(payload)

    def test_csv_guards_a_non_integer_recorded_at_cell_as_text(self):
        self.tracker.create_project("Time")
        for name in ("A", "B"):
            self.tracker.add_asset("tap", name, "", FIX, link_to_active_line=False)
        points = self.tracker.projects[0]["points"]
        points[0]["measurement"]["recorded_at"] = "=HYPERLINK(1)"
        points[1]["measurement"]["recorded_at"] = -5
        rows = self.tracker.csv().splitlines()[1:]
        self.assertIn('"\'=HYPERLINK(1)","","true",', rows[0])
        self.assertIn('"-5","","true",', rows[1])

    def test_status_ages_ignore_a_non_integer_recorded_at(self):
        self.tracker.create_project("Time")
        self.tracker.start_line("Line", 0, fix=FIX)
        line = self.tracker.projects[0]["lines"][0]
        for bad in ("abc", 1.5, True):
            with self.subTest(bad=bad):
                line["measurements"] = [dict(line["measurements"][0], recorded_at=bad)]
                self.tracker._status_cache = None
                self.assertIsNone(self.tracker.status()["active_line"]["last_point"]["age_sec"])
                # The cached branch recomputes the age from the remembered value.
                self.assertIsNone(self.tracker.status()["active_line"]["last_point"]["age_sec"])
                self.assertIsNone(self.tracker.live_status()["active_line"]["last_point"]["age_sec"])

    def test_hand_edited_timestamps_export_without_crashing(self):
        self.tracker.create_project("Time")
        for name in ("A", "B", "C"):
            self.tracker.add_asset("tap", name, "", FIX, link_to_active_line=False)
        points = self.tracker.projects[0]["points"]
        points[0]["measurement"]["recorded_at"] = 0
        points[1]["measurement"]["recorded_at"] = "abc"
        points[2]["measurement"]["recorded_at"] = 2 ** 40
        exported = [feature["properties"]["measurement"]
                    for feature in json.loads("".join(self.tracker.iter_geojson()))["features"]]
        self.assertEqual([item["recorded_at_utc"] for item in exported],
                         ["2000-01-01T00:00:00Z", None, None])
        self.assertEqual([item["clock_unsynced"] for item in exported], [True, True, True])
        rows = self.tracker.csv().splitlines()[1:]
        self.assertIn('"0","2000-01-01T00:00:00Z","true",', rows[0])
        self.assertIn('"abc","","true",', rows[1])

    def test_restore_strips_the_derived_time_fields_on_every_path(self):
        self.tracker.create_project("Time")
        self.tracker.add_asset("tap", "T", "", FIX, link_to_active_line=False)
        text = "".join(self.tracker.iter_backup_ndjson())
        other = tracking.Tracker(os.path.join(self.tmp.name, "other.jsonl"))
        other.restore_ndjson(text.splitlines(keepends=True))
        stored = other.projects[0]["points"][0]["measurement"]
        self.assertNotIn("recorded_at_utc", stored); self.assertNotIn("clock_unsynced", stored)
        self.assertEqual("".join(other.iter_backup_ndjson()), text)
        payload = self.tracker.backup()
        payload["projects"][0]["points"][0]["measurement"]["recorded_at_utc"] = "1999-01-01T00:00:00Z"
        payload["projects"][0]["points"][0]["measurement"]["custom_key"] = "kept"
        third = tracking.Tracker(os.path.join(self.tmp.name, "third.jsonl"))
        third.restore(payload)
        stored = third.projects[0]["points"][0]["measurement"]
        self.assertNotIn("recorded_at_utc", stored); self.assertEqual(stored["custom_key"], "kept")

    def test_restore_strips_derived_fields_from_vertices_async_and_archives(self):
        self.tracker.create_project("Time")
        self.tracker.start_line("Line", 0, fix=FIX)
        self.tracker.add_vertex(dict(FIX, lon_e8=800002000))
        self.tracker.set_line_state("finish")
        self.tracker.add_asset("tap", "T", "", FIX, link_to_active_line=False)
        def clean(tracker):
            project = tracker.projects[0]
            measurements = list(project["lines"][0]["measurements"]) + [
                point["measurement"] for point in project["points"]]
            for measurement in measurements:
                self.assertNotIn("recorded_at_utc", measurement)
                self.assertNotIn("clock_unsynced", measurement)
            return len(measurements)
        text = "".join(self.tracker.iter_backup_ndjson())
        ndjson = tracking.Tracker(os.path.join(self.tmp.name, "ndjson.jsonl"))
        ndjson.restore_ndjson(text.splitlines(keepends=True))
        self.assertEqual(clean(ndjson), 3)
        self.assertEqual("".join(ndjson.iter_backup_ndjson()), text)
        payload = self.tracker.backup()
        payload["projects"][0]["lines"][0]["measurements"][0]["clock_unsynced"] = True
        import asyncio
        async_target = tracking.Tracker(os.path.join(self.tmp.name, "async.jsonl"))
        asyncio.run(async_target.restore_async(payload))
        self.assertEqual(clean(async_target), 3)
        # A JSON restore renumbers record_order, so it is no byte round trip; the
        # derived fields are regenerated on the next backup.
        regenerated = async_target.backup()["projects"][0]["lines"][0]["measurements"][0]
        self.assertEqual(regenerated["recorded_at_utc"], self.tracker.backup()["projects"][0]
                         ["lines"][0]["measurements"][0]["recorded_at_utc"])
        self.assertFalse(regenerated["clock_unsynced"])
        # Archive files never store the derived fields; reactivation restores clean rows.
        project_id = self.tracker.projects[0]["id"]
        archive = self.tracker.archive_project(project_id)
        with open(self.tracker.archive_dir + "/" + archive["id"] + ".ndjson") as source:
            self.assertNotIn("recorded_at_utc", source.read())
        self.tracker.reactivate_archive(archive["id"])
        self.assertEqual(clean(self.tracker), 3)
        self.assertEqual("".join(self.tracker.iter_backup_ndjson()), text)

    def test_status_reports_server_timer_and_last_vertex(self):
        # Host clock values; stored and reported times are seconds since 2000.
        base = tracking._EPOCH_OFFSET
        with mock.patch.object(tracking.time, "time", return_value=base + 1_000):
            self.tracker.create_project("Timed")
            self.tracker.start_line("Line", 0)
        fix = copy.deepcopy(FIX)
        fix["receiver_accuracy"] = {"horizontal_sigma_m": .013,
                                     "altitude_sigma_m": .02}
        with mock.patch.object(tracking.time, "time", return_value=base + 1_005):
            self.tracker.add_vertex(fix)
        with mock.patch.object(tracking.time, "time", return_value=base + 1_012):
            status = self.tracker.status()
        self.assertEqual(status["device_time"], 1_012)
        self.assertEqual(status["projects"][0]["created_at"], 1_000)
        self.assertEqual(status["active_line"]["recording_sec"], 12)
        self.assertEqual(status["active_line"]["last_point"],
                         {"label": "V-1", "age_sec": 7, "h_sigma_m": .013})

    def test_live_status_is_compact_and_keeps_incremental_length(self):
        self.tracker.create_project("Live")
        self.tracker.start_line("L-1", 0, fix=FIX)
        self.tracker.add_vertex(dict(FIX, lon=8.00002, lon_e8=800002000))
        live = self.tracker.live_status()
        self.assertEqual(live["active_project"]["name"], "Live")
        self.assertEqual(live["active_line"]["vertex_count"], 2)
        self.assertGreater(live["active_line"]["length_m"], 1)
        self.assertNotIn("projects", live)
        self.tracker.undo_last()
        self.assertEqual(self.tracker.live_status()["active_line"]["length_m"], 0)

    def test_last_point_is_removed_by_undo(self):
        self.tracker.create_project("Undo")
        self.tracker.start_line("Line", 0)
        self.assertIsNone(self.tracker.status()["active_line"]["last_point"])
        self.tracker.add_vertex(FIX)
        self.tracker.undo_last()
        self.assertIsNone(self.tracker.status()["active_line"]["last_point"])

    def test_project_activity_uses_events_and_survives_replay(self):
        base = tracking._EPOCH_OFFSET   # host clock values; stored times count from 2000
        with mock.patch.object(tracking.time, "time", return_value=base + 100):
            project = self.tracker.create_project("Activity")
        with mock.patch.object(tracking.time, "time", return_value=base + 120):
            self.tracker.start_line("Line", 0)
        self.assertEqual(self.tracker.status()["projects"][0]["updated_at"], 120)
        restored = tracking.Tracker(self.path)
        project_status = next(item for item in restored.status()["projects"]
                              if item["id"] == project["id"])
        self.assertEqual(project_status["updated_at"], 120)

    def test_name_availability_is_scoped_and_normalized(self):
        self.assertEqual(self.tracker.name_available("V-01", "point"),
                         {"available": True, "existing": None})
        self.tracker.create_project("Names")
        self.tracker.start_line(" Main line ", 0)
        self.tracker.add_asset("valve", " V-01 ", "", FIX)
        self.assertEqual(self.tracker.name_available("V-01", "point"),
                         {"available": False, "existing": "V-01"})
        self.assertEqual(self.tracker.name_available(" Main line ", "line"),
                         {"available": False, "existing": "Main line"})
        self.assertTrue(self.tracker.name_available("v-01", "point")["available"])
        with self.assertRaises(ValueError):
            self.tracker.name_available("", "point")
        with self.assertRaises(ValueError):
            self.tracker.name_available("V-01", "project")

    def test_exports_geojson_and_csv(self):
        project = self.tracker.create_project("Test")
        self.tracker.start_line("Line", 0, fix=FIX)
        self.tracker.add_asset("tap", "T-01", "quoted \"note\"", FIX)
        geojson = tracking_reference.geojson(self.tracker, project["id"])
        self.assertEqual(geojson["type"], "FeatureCollection")
        self.assertEqual([f["geometry"]["type"] for f in geojson["features"]],
                         ["Point", "Point"])
        self.assertEqual(geojson["features"][0]["properties"]["line_id"],
                         geojson["features"][1]["properties"]["line_id"])
        csv = self.tracker.csv(project["id"])
        self.assertIn('"T-01"', csv)
        self.assertIn('"quoted ""note"""', csv)
        self.assertIn("gst_horizontal_sigma_max_m", csv.splitlines()[0])
        self.assertIn('"0.02"', csv)
        self.assertIn("altitude_msl_m", csv.splitlines()[0])
        self.assertIn(",gst_vertical_sigma_max_m,station_id,ntrip_mount,reference_frame,reference_frame_epoch,reference_frame_source,gate_profile,precision,recorded_at,recorded_at_utc,clock_unsynced,note", csv.splitlines()[0])
        self.assertIn('"8.00000000","50.00000000"', csv)

    def test_single_sample_counts_follow_fix_quality_and_gst(self):
        # X1: a point that was not averaged is one sample; it counts as RTK
        # fixed only with fix quality 4 and as a GST sample only with GST.
        self.tracker.create_project("Counts")
        with mock.patch.dict(cfg.CONFIG, {"measurement_required_fix": "DGPS"}):
            self.tracker.add_asset("tap", "RTK", "", FIX, link_to_active_line=False)
            self.tracker.add_asset("tap", "DGPS", "", dict(FIX, qual=2, fix_status_text="DGPS"),
                                   link_to_active_line=False)
            self.tracker.add_asset("tap", "NOGST", "", FIX, link_to_active_line=False)
            self.tracker.add_asset("tap", "AVG", "", FIX, link_to_active_line=False)
        points = self.tracker.projects[0]["points"]
        # A route point or a hand-edited backup can lack the GST accuracy.
        points[2]["measurement"]["receiver_accuracy"] = None
        # An averaged point keeps its stored counts (a DGPS gate admits these).
        points[3]["measurement"]["accuracy_observation"] = {
            "samples": 5, "rtk_fixed_samples": 3, "accepted_fix_samples": 5,
            "horizontal_max_deviation_m": 0.01}
        points[3]["measurement"]["receiver_accuracy"] = {
            "source": "NMEA_GST", "samples": 4, "expected_samples": 5,
            "horizontal_sigma_max_m": 0.02, "vertical_sigma_max_m": 0.03}
        self.assertEqual([tracking._sample_counts(p["measurement"])
                          for p in points],
                         [(1, 1, 1), (1, 0, 1), (1, 1, 0), (5, 3, 4)])
        lines = self.tracker.csv().splitlines()
        header = lines[0].split(",")
        cells = [[cell.strip('"') for cell in line.split(",")] for line in lines[1:]]
        columns = [header.index(name) for name in ("samples", "rtk_fixed_samples", "gst_samples")]
        self.assertEqual([[row[i] for i in columns] for row in cells],
                         [["1", "1", "1"], ["1", "0", "1"], ["1", "1", "0"], ["5", "3", "4"]])
        # GeoJSON carries the stored objects and invents no counts.
        features = json.loads("".join(self.tracker.iter_geojson()))["features"]
        dgps = features[1]["properties"]
        self.assertIsNone(dgps["measurement"]["accuracy_observation"])
        self.assertNotIn("rtk_fixed_samples", dgps)
        self.assertNotIn("samples", dgps["measurement"]["receiver_accuracy"])

    def test_rejects_position_without_fix(self):
        self.tracker.create_project("Test")
        with self.assertRaisesRegex(ValueError, "Measurement quality rejected"):
            self.tracker.start_line("Line", 1, fix={"lat": None, "lon": None})

    def test_quality_gate_requires_precise_rtk(self):
        rejected = tracking.quality_check(dict(FIX, qual=5))
        self.assertFalse(rejected["accepted"])
        self.assertIn("rtk_fixed_required", rejected["reasons"])
        spread = dict(FIX, accuracy_observation={"samples": 5,
            "rtk_fixed_samples": 5, "horizontal_max_deviation_m": 0.08})
        self.assertIn("horizontal_spread_too_high",
                      tracking.quality_check(spread)["reasons"])
        self.assertIn("gst_required", tracking.quality_check(
            dict(FIX, receiver_accuracy=None))["reasons"])
        self.assertIn("gst_horizontal_error_too_high", tracking.quality_check(
            dict(FIX, receiver_accuracy={"semi_major_sigma_m": 0.08,
                                         "altitude_sigma_m": 0.04}))["reasons"])

    def test_route_starts_without_fix_and_preserves_rejected_positions(self):
        self.tracker.create_project("Route")
        line = self.tracker.start_line("Drive", 1, recording_mode="route")
        rejected = dict(FIX, utc="1", qual=1, fix_status_text="GPS_FIX",
                        receiver_accuracy=None, correction_age_sec=None)
        self.assertTrue(self.tracker.enqueue_fix(rejected))
        measurement = self.tracker.add_vertex(
            self.tracker._pending_fixes.pop(0), source="automatic")
        self.assertFalse(measurement["quality"]["accepted"])
        self.assertIn("rtk_fixed_required", measurement["quality"]["reasons"])
        self.assertIn("gst_required", measurement["quality"]["reasons"])
        self.assertEqual(line["recording_mode"], "route")
        self.assertEqual(self.tracker.live_status()["active_line"]["recording_mode"],
                         "route")
        restored = tracking.Tracker(self.path)
        restored_line = restored._line(restored._project())
        self.assertEqual(restored_line["recording_mode"], "route")
        self.assertFalse(restored_line["measurements"][0]["quality"]["accepted"])
        self.assertEqual(tracking_reference.geojson(restored)["features"][0]["properties"]
                         ["recording_mode"], "route")

    def test_route_skips_fix_without_complete_coordinate(self):
        self.tracker.create_project("Route")
        self.tracker.start_line("Drive", 1, recording_mode="route")
        self.assertFalse(self.tracker.enqueue_fix(dict(FIX, alt=None, alt_mm=None, utc="1")))
        self.assertEqual(self.tracker.feature_count(), 0)

    def test_survey_mode_still_rejects_bad_automatic_fix(self):
        self.tracker.create_project("Survey")
        self.tracker.start_line("Precise", 1, recording_mode="survey")
        rejected = dict(FIX, qual=1, fix_status_text="GPS_FIX")
        self.assertFalse(self.tracker.enqueue_fix(rejected))
        with self.assertRaisesRegex(ValueError, "Measurement quality rejected"):
            self.tracker.add_vertex(rejected, source="automatic")

    def test_averaging_respects_configured_fix_level(self):
        saved = dict(cfg.CONFIG)
        try:
            cfg.CONFIG["measurement_required_fix"] = "DGPS"
            fixes = [dict(FIX, qual=5, fix_status_text="RTK_FLOAT")
                     for _index in range(3)]
            averaged = tracking.average_fixes(fixes)
            self.assertEqual(averaged["accuracy_observation"]["rtk_fixed_samples"], 0)
            self.assertEqual(averaged["accuracy_observation"]["accepted_fix_samples"], 3)
            self.assertTrue(tracking.quality_check(averaged)["accepted"])
        finally:
            cfg.CONFIG.clear()
            cfg.CONFIG.update(saved)

    def test_field_setup_validation(self):
        clean, error = tracking.validate_field_setup({"antenna_height_m": 2.005})
        self.assertIsNone(error); self.assertEqual(clean, {"antenna_height_m": 2.01})
        self.assertEqual(tracking.validate_field_setup({"antenna_height_m": "1.995"})[0], {"antenna_height_m": 2.0})
        self.assertEqual(tracking.validate_field_setup({"antenna_height_m": 3})[0], {"antenna_height_m": 3.0})
        self.assertEqual(tracking.validate_field_setup({"antenna_height_m": "-0.004"})[0], {"antenna_height_m": 0.0})
        self.assertIsNone(tracking.validate_field_setup({"antenna_height_m": "-0.005"})[0])
        for bad in ({"antenna_height_m": 3.005}, {"antenna_height_m": -0.01}, {"antenna_height_m": "abc"},
                    {"antenna_height_m": True}, {"antenna_height_m": None}, {"antenna_height_m": float("nan")},
                    {"antenna_height_m": float("inf")}, {"reference_frame": "x" * 41}, {"reference_frame": "a\tb"},
                    {"reference_frame": "a\x00b"}, {"reference_frame": "a\x7fb"}, {"reference_frame": 5},
                    {"reference_frame": None}, {"reference_frame_epoch": "2010.0000000000001"},
                    {"reference_frame_epoch": "a\nb"}, {"unknown": 1}, "nope", None):
            clean, error = tracking.validate_field_setup(bad)
            self.assertIsNone(clean, repr(bad)); self.assertTrue(error, repr(bad))
        clean, error = tracking.validate_field_setup({"reference_frame": "ETRS89 (synthetic)", "reference_frame_epoch": ""})
        self.assertIsNone(error); self.assertEqual(clean, {"reference_frame": "ETRS89 (synthetic)", "reference_frame_epoch": ""})

    def test_field_setup_frame_text_is_stripped_but_not_case_folded(self):
        clean, error = tracking.validate_field_setup(
            {"reference_frame": " ETRS89 ", "reference_frame_epoch": "  2010.0 "})
        self.assertIsNone(error)
        self.assertEqual(clean, {"reference_frame": "ETRS89", "reference_frame_epoch": "2010.0"})
        self.assertEqual(tracking.validate_field_setup({"reference_frame": "etrs89"})[0], {"reference_frame": "etrs89"})
        self.assertEqual(tracking.validate_field_setup({"reference_frame": "   "})[0], {"reference_frame": ""})

    def test_stamped_frame_variants_tally_as_one_key(self):
        self.tracker.create_project("Spaces")
        self.tracker.start_line("L", 1, fix=FIX)
        for text in (" ETRS89", "ETRS89 ", "ETRS89"):
            with mock.patch.dict(cfg.CONFIG, {"reference_frame": text, "reference_frame_epoch": " 2010.0"}):
                self.tracker.add_vertex(dict(FIX, lon_e8=FIX["lon_e8"] + 2000))
        project = self.tracker.status()["projects"][0]
        self.assertEqual(project["frames"].get("ETRS89"), 3)
        self.assertFalse(any(key not in ("ETRS89", "unknown") for key in project["frames"]), project["frames"])
        epochs = {m["reference_frame_epoch"] for m in self.tracker.projects[0]["lines"][0]["measurements"]}
        self.assertEqual(epochs - {None}, {"2010.0"})
        with mock.patch.dict(cfg.CONFIG, {"reference_frame": "  ", "reference_frame_epoch": " "}):
            fix = dict(FIX)
            self.assertIsNone(tracking._reference_frame(fix))
            self.assertIsNone(tracking._reference_frame_epoch(fix))

    def test_empty_string_frame_reads_unknown_in_every_export_and_the_tally(self):
        self.tracker.create_project("Empty")
        self.tracker.start_line("L", 1, fix=FIX)
        self.tracker.add_vertex(dict(FIX, lon_e8=FIX["lon_e8"] + 2000))
        self.tracker.add_asset("tap", "P", "", FIX, link_to_active_line=False)
        project = self.tracker.projects[0]
        for item in [project["points"][0]["measurement"]] + list(project["lines"][0]["measurements"]):
            item["reference_frame"] = ""; item["reference_frame_epoch"] = ""
        self.tracker._project_frames.clear()
        for line in project["lines"]:
            line.pop("_summary", None)
        features = json.loads("".join(self.tracker.iter_geojson()))["features"]
        point = [f for f in features if f["geometry"]["type"] == "Point"][0]["properties"]
        line = [f for f in features if f["geometry"]["type"] == "LineString"][0]["properties"]
        self.assertEqual(point["reference_frame"], "unknown")
        self.assertIsNone(point["reference_frame_epoch"])
        self.assertEqual(point["measurement"]["reference_frame"], "unknown")
        self.assertEqual({m["reference_frame"] for m in line["measurements"]}, {"unknown"})
        self.assertEqual(line["reference_frame"], "unknown")
        self.assertEqual(line["reference_frames"], {"unknown": 2})
        self.assertEqual(self.tracker.status()["projects"][0]["frames"], {"unknown": 3})

    def test_field_setup_reads_config_and_defaults(self):
        self.assertEqual(tuple(tracking.field_setup()), tracking.FIELD_SETUP_KEYS)
        self.assertEqual(tracking.field_setup(True)["antenna_height_m"], 0.0)

    def test_measurement_gate_can_be_configured_and_reset_to_defaults(self):
        saved = dict(cfg.CONFIG)
        try:
            values = tracking.measurement_gate()
            values.update(required_fix="RTK_FLOAT", min_satellites=8,
                          max_hdop=2.0)
            clean, error = tracking.validate_measurement_gate(values)
            self.assertIsNone(error)
            cfg.CONFIG.update(clean)
            self.assertTrue(tracking.quality_check(dict(FIX, qual=5))["accepted"])
            self.assertEqual(tracking.measurement_gate(True)["required_fix"],
                             "RTK_FIXED")
        finally:
            cfg.CONFIG.clear()
            cfg.CONFIG.update(saved)

    def test_measurement_gate_rejects_partial_and_unsafe_values(self):
        self.assertIsNotNone(tracking.validate_measurement_gate({})[1])
        values = tracking.measurement_gate()
        values["max_hdop"] = 0
        self.assertIn("max_hdop", tracking.validate_measurement_gate(values)[1])

    def test_quality_ok_window_is_configurable_and_has_safe_default(self):
        self.assertEqual(tracking.measurement_gate(True)["quality_window_samples"], 5)
        values = tracking.measurement_gate()
        values["quality_window_samples"] = 12
        clean, error = tracking.validate_measurement_gate(values)
        self.assertIsNone(error)
        self.assertEqual(clean["measurement_quality_window_samples"], 12)
        values["quality_window_samples"] = 2
        self.assertIn("quality_window_samples",
                      tracking.validate_measurement_gate(values)[1])

    def test_asset_is_projected_to_line_with_offset(self):
        line = [[800000000, 5000000000, 0], [800100000, 5000000000, 0]]
        relation = tracking.project_to_line(line, [800050000, 5000001000, 0])
        self.assertGreater(relation["chainage_m"], 30)
        self.assertGreater(relation["lateral_offset_m"], 1)
        self.assertEqual(relation["side"], "left")

    def test_projection_on_integer_coordinates(self):
        line = [[800000000, 5000000000, 100000], [800100000, 5000000000, 100000]]
        relation = tracking.project_to_line(line, [800050000, 5000001000, 100000])
        self.assertAlmostEqual(relation["chainage_m"], 35.85, delta=0.05)
        self.assertAlmostEqual(abs(relation["lateral_offset_m"]), 1.112, delta=0.005)
        self.assertEqual(relation["side"], "left")
        self.assertEqual(relation["projected_coordinate"], [800050000, 5000000000, 100000])
        for value in relation["projected_coordinate"]:
            self.assertIs(type(value), int)

    def test_projection_wraps_at_the_antimeridian(self):
        # A segment from 179.9999 E to 179.9999 W is 22 m long, not 360 degrees.
        line = [[17999990000, 0, 0], [-17999990000, 0, 1000]]
        east = tracking.project_to_line(line, [17999995000, 1000, 0])
        self.assertAlmostEqual(east["chainage_m"], 5.566, delta=0.005)
        self.assertAlmostEqual(east["lateral_offset_m"], 1.106, delta=0.005)
        self.assertEqual(east["side"], "left")
        self.assertEqual(east["projected_coordinate"], [17999995000, 0, 250])
        west = tracking.project_to_line(line, [-17999995000, -1000, 0])
        self.assertAlmostEqual(west["chainage_m"], 16.698, delta=0.005)
        self.assertEqual(west["side"], "right")
        self.assertEqual(west["projected_coordinate"], [-17999995000, 0, 750])
        # A projection beyond the meridian lands in the valid range again.
        across = tracking.project_to_line([[17999990000, 1000, 0], [-17999990000, -1000, 0]],
                                          [17999999000, -1000, 0])
        self.assertLessEqual(abs(across["projected_coordinate"][0]), 18000000000)
        tracking._validate_coordinate(across["projected_coordinate"])
        self.assertLess(across["chainage_m"], 25)

    def test_projection_interpolates_the_altitude_in_millimetres(self):
        line = [[800000000, 5000000000, 100000], [800100000, 5000000000, 100999]]
        relation = tracking.project_to_line(line, [800050000, 5000001000, 0])
        self.assertEqual(relation["projected_coordinate"], [800050000, 5000000000, 100500])
        self.assertIs(type(relation["projected_coordinate"][2]), int)

    def test_projection_of_a_single_coordinate_and_an_empty_line(self):
        self.assertIsNone(tracking.project_to_line([], [800000000, 5000000000, 0]))
        relation = tracking.project_to_line([[800000000, 5000000000, 0]],
                                            [800004000, 5000000000, 7000])
        self.assertAlmostEqual(relation["chainage_m"], 2.868, delta=0.005)
        self.assertEqual(relation["side"], "on_line")
        self.assertEqual(relation["projected_coordinate"], [800004000, 5000000000, 7000])

    def test_vertex_chainage_equals_the_distance_prefix_sum(self):
        path = os.path.join(os.path.dirname(__file__), "fixtures", "stakeout_geometry_vectors.json")
        with open(path, encoding="utf-8") as handle:
            vertices = json.load(handle)["line_1km"]["vertices"]
        prefix = 0.0
        for index, vertex in enumerate(vertices):
            if index:
                prefix += tracking._distance_m(vertices[index - 1], vertex)
            relation = tracking.project_to_line(vertices, vertex)
            self.assertAlmostEqual(relation["chainage_m"], prefix, delta=0.001)
            self.assertAlmostEqual(relation["lateral_offset_m"], 0.0, delta=0.001)
            self.assertEqual(relation["projected_coordinate"], list(vertex))

    def test_backup_with_a_line_linked_asset_validates(self):
        self.tracker.create_project("Linked")
        self.tracker.start_line("L", 0, fix=FIX)
        self.tracker.add_vertex(dict(FIX, lon_e8=800010000, alt_mm=123500))
        point = self.tracker.add_asset("tap", "T-01", "", dict(FIX, lon_e8=800005000, lat_e8=5000000100))
        self.assertIsNotNone(point["line_id"])
        projected = point["projected_coordinate"]
        self.assertEqual(len(projected), 3)
        for value in projected:
            self.assertIs(type(value), int)
        tracking.validate_backup(self.tracker.backup())

    def test_vertical_step_and_gap_use_millimetres(self):
        self.tracker.create_project("Steps")
        self.tracker.start_line("L", 0.5, fix=FIX)
        moved = dict(FIX, lon=8.00004, lon_e8=800004000, alt=124.2, alt_mm=124200)
        self.tracker.add_vertex(moved)
        summary = self.tracker.status()["active_project_lines"][0]
        self.assertEqual(summary["large_vertical_step_count"], 1)
        self.assertAlmostEqual(summary["max_vertical_step_m"], 0.8, places=3)
        self.assertGreater(summary["max_gap_m"], 2.0)

    def test_reference_frame_text_is_clamped_to_what_a_backup_accepts(self):
        fix = dict(FIX, qual=4)
        with mock.patch.dict(tracking.CONFIG, {"reference_frame": "F" * 120,
                                               "reference_frame_epoch": "E" * 120}):
            self.assertEqual(tracking._reference_frame(fix), "F" * 80)
            self.assertEqual(tracking._reference_frame_epoch(fix), "E" * 80)
            self.tracker.create_project("Frame")
            self.tracker.start_line("L", 0, fix=fix)
            tracking.validate_backup(self.tracker.backup())
        stored = self.tracker.projects[0]["lines"][0]["measurements"][0]
        self.assertEqual((stored["reference_frame"], stored["reference_frame_epoch"]),
                         ("F" * 80, "E" * 80))

    def test_line_length_matches_vincenty_within_a_centimetre(self):
        path = os.path.join(os.path.dirname(__file__), "fixtures", "stakeout_geometry_vectors.json")
        with open(path, encoding="utf-8") as handle:
            line = json.load(handle)["line_1km"]
        vertices = line["vertices"]
        length = sum(tracking._distance_m(vertices[i - 1], vertices[i])
                     for i in range(1, len(vertices)))
        self.assertAlmostEqual(length, line["length_m"], delta=0.01)

    def test_distance_no_longer_uses_the_sphere(self):
        # 0.001 degrees of latitude at 50 N: sphere 111.19 m, ellipsoid 111.23 m.
        self.assertAlmostEqual(tracking._distance_m([800000000, 5000000000, 0],
                                                    [800000000, 5000100000, 0]), 111.229, delta=0.01)
        self.assertEqual(tracking._distance_m(None, [800000000, 5000000000, 0]), 0.0)

    def test_distance_of_stored_integer_coordinates_is_in_metres(self):
        # The same 0.001 degrees of latitude as above, and 0.00004 degrees of longitude,
        # as the e8 integers the tracker stores.
        self.assertAlmostEqual(tracking._distance_m([800000000, 5000000000, 0],
                                                    [800000000, 5000100000, 0]), 111.229, delta=0.01)
        self.assertAlmostEqual(tracking._distance_m([800000000, 5000000000, 0],
                                                    [800004000, 5000000000, 123400]), 2.868, delta=0.005)

    def test_projection_pins_the_latitude_scale_on_a_north_south_line(self):
        # The chainage runs along the meridian, so a latitude scale left on the
        # 6371 km sphere would give 55.597 m instead of 55.615 m.
        line = [[800000000, 5000000000, 0], [800000000, 5000100000, 0]]
        relation = tracking.project_to_line(line, [800001000, 5000050000, 0])
        self.assertAlmostEqual(relation["chainage_m"], 55.615, delta=0.005)
        self.assertAlmostEqual(abs(relation["lateral_offset_m"]), 0.717, delta=0.005)

    def test_average_records_observed_repeatability(self):
        fixes = [dict(FIX, lon=8.0), dict(FIX, lon=8.000001, lon_e8=800000100),
                 dict(FIX, lon=8.000002, lon_e8=800000200, qual=5, fix_status_text="RTK_FLOAT")]
        averaged = tracking.average_fixes(fixes)
        observed = averaged["accuracy_observation"]
        self.assertEqual(observed["samples"], 3)
        self.assertEqual(observed["rtk_fixed_samples"], 2)
        self.assertGreater(observed["horizontal_max_deviation_m"], 0)
        receiver = averaged["receiver_accuracy"]
        self.assertEqual(receiver["samples"], 3)
        self.assertEqual(receiver["horizontal_sigma_max_m"], 0.02)

    def test_receiver_sigma_stats_matches_independent_values(self):
        fixes = [{"receiver_accuracy": {"horizontal_sigma_m": .01, "altitude_sigma_m": .02}},
                 {"receiver_accuracy": {"semi_major_sigma_m": .03, "altitude_sigma_m": 0}},
                 {"lat": 50.0}]
        self.assertEqual(tracking.receiver_sigma_stats(fixes),
                         {"samples": 2, "horizontal_sigma_mean_m": .02,
                          "horizontal_sigma_max_m": .03, "vertical_sigma_mean_m": .02,
                          "vertical_sigma_max_m": .02})
        receiver = tracking.average_fixes([dict(FIX), dict(FIX), dict(FIX)])["receiver_accuracy"]
        self.assertEqual(list(receiver), ["source", "samples", "expected_samples",
                                          "horizontal_sigma_mean_m", "horizontal_sigma_max_m",
                                          "vertical_sigma_mean_m", "vertical_sigma_max_m"])
        self.assertEqual(receiver["expected_samples"], 3)

    def test_identical_e8_samples_have_exactly_zero_spread(self):
        averaged = tracking.average_fixes([dict(FIX_E8, geoid_sep_mm=45600 + i % 2) for i in range(5)])
        observed = averaged["accuracy_observation"]
        self.assertEqual(observed["horizontal_max_deviation_m"], 0.0)
        self.assertEqual(observed["vertical_max_deviation_m"], 0.0)
        self.assertEqual(averaged["lat_e8"], 5000000000)
        self.assertEqual(averaged["alt_mm"], 123400)
        self.assertEqual(averaged["lat"], 50.0)
        self.assertEqual(averaged["geoid_sep_mm"], 45600)  # mean of 45600,45601,45600,45601,45600
        self.assertNotIn("geoid_sep_mm", tracking.average_fixes([dict(FIX_E8), dict(FIX_E8, geoid_sep_mm=None)]))

    def test_e8_mean_is_within_a_millimetre_of_the_double_reference(self):
        fixes = [dict(FIX_E8, lat_e8=5000000000 + i * 3, lon_e8=800000000 + i * 5,
                      alt_mm=123400 + i) for i in range(5)]
        averaged = tracking.average_fixes(fixes)
        self.assertEqual(averaged["lat_e8"], 5000000006)
        self.assertEqual(averaged["lon_e8"], 800000010)
        self.assertEqual(averaged["alt_mm"], 123402)
        reference_lat = sum(f["lat_e8"] for f in fixes) / 5 / 1e8
        self.assertLess(abs(averaged["lat"] - reference_lat) * 111320, 0.001)
        self.assertEqual(averaged["lon"], 8.0000001)
        self.assertLess(averaged["accuracy_observation"]["horizontal_max_deviation_m"], 0.02)
        self.assertGreater(averaged["accuracy_observation"]["horizontal_max_deviation_m"], 0.0)

    def test_e8_mean_across_the_antimeridian_stays_on_the_short_arc(self):
        lons = [17999999998, 17999999999, 18000000000, -17999999999, -17999999998]
        fixes = [dict(FIX_E8, lon=lon / 1e8, lon_e8=lon) for lon in lons]
        averaged = tracking.average_fixes(fixes)
        self.assertEqual(averaged["lon_e8"], 18000000000)
        self.assertEqual(averaged["lon"], 180.0)
        spread = averaged["accuracy_observation"]["horizontal_max_deviation_m"]
        self.assertLess(spread, 0.01)
        self.assertGreater(spread, 0.0)

    def test_legacy_height_floats_follow_the_integer_means(self):
        # The sample floats differ from the means, so only a recomputation passes.
        low = dict(FIX_E8, geoid_sep_mm=40000, geoid_sep_m=40.0, altitude_ellipsoid_m=163.4)
        high = dict(FIX_E8, geoid_sep_mm=42000, geoid_sep_m=42.0, altitude_ellipsoid_m=165.4)
        averaged = tracking.average_fixes([low, high])
        self.assertEqual(averaged["geoid_sep_mm"], 41000)
        self.assertEqual(averaged["geoid_sep_m"], 41.0)
        self.assertEqual(averaged["altitude_ellipsoid_m"], 164.4)
        self.assertAlmostEqual(averaged["altitude_ellipsoid_m"], averaged["alt"] + 41.0, places=9)

    def test_legacy_height_floats_go_when_a_sample_has_no_separation(self):
        # Without a mean separation the floats would be the last sample's values.
        with_sep = dict(FIX_E8, geoid_sep_mm=40000, geoid_sep_m=40.0, altitude_ellipsoid_m=163.4)
        without = dict(FIX_E8, geoid_sep_mm=None, geoid_sep_m=None, altitude_ellipsoid_m=None)
        for order in ((with_sep, without), (without, with_sep)):
            averaged = tracking.average_fixes(list(order))
            for key in ("geoid_sep_mm", "geoid_sep_m", "altitude_ellipsoid_m"):
                self.assertNotIn(key, averaged, key)

    def test_float_valued_exact_fields_are_not_exact(self):
        # A float is no exact integer, even one with an integral value, and it
        # refuses the average whichever sample carries it.
        for bad in (dict(FIX_E8, alt_mm=123400.0), dict(FIX_E8, lat_e8=5000000000.0)):
            for order in ((dict(FIX_E8), bad), (bad, dict(FIX_E8))):
                with self.assertRaisesRegex(ValueError, "no exact position"):
                    tracking.average_fixes(list(order))
        # A bool is an int in Python but never a millimetre value; the geoid
        # separation is optional, so the mean just carries none.
        self.assertNotIn("geoid_sep_mm", tracking.average_fixes(
            [dict(FIX_E8, geoid_sep_mm=True), dict(FIX_E8, geoid_sep_mm=45600)]))

    def test_samples_without_exact_fields_are_refused(self):
        bad_values = {
            "lon_e8": (None, 800000000.0, True, 18000000001),
            "lat_e8": (None, 5000000000.0, True, 9000000001),
            "alt_mm": (None, 123400.0, True, 100000001),
        }
        for key, values in bad_values.items():
            for value in values:
                bad = dict(FIX_E8, **{key: value})
                for order in ((dict(FIX_E8), bad), (bad, dict(FIX_E8))):
                    with self.subTest(key=key, value=value, first=order[0] is bad):
                        with self.assertRaisesRegex(ValueError, "no exact position"):
                            tracking.average_fixes(list(order))
        # A float-only sample never reaches a float mean or a float distance.
        with mock.patch.object(geo, "distance_deg_m", side_effect=AssertionError):
            for order in ((dict(FIX_E8), dict(FIX_FLOAT)), (dict(FIX_FLOAT), dict(FIX_E8))):
                with self.assertRaisesRegex(ValueError, "no exact position"):
                    tracking.average_fixes(list(order))

    def test_a_missing_exact_component_refuses_the_average(self):
        # Either half of the horizontal position alone refuses the average.
        for key in ("lat_e8", "lon_e8"):
            with self.subTest(key=key):
                with self.assertRaisesRegex(ValueError, "no exact position"):
                    tracking.average_fixes([dict(FIX_E8), dict(FIX_E8, **{key: None})])

    def test_exact_means_and_spread_come_from_the_integers(self):
        # Height: alt and altitude_msl_m come from the millimetre mean (123401.5 rounds up).
        averaged = tracking.average_fixes([dict(FIX_E8), dict(FIX_E8, alt_mm=123403)])
        self.assertEqual(averaged["alt_mm"], 123402)
        self.assertEqual(averaged["alt"], 123.402)
        self.assertEqual(averaged["altitude_msl_m"], 123.402)
        self.assertEqual(averaged["accuracy_observation"]["vertical_max_deviation_m"], 0.002)
        # Both floats are 123.4, so only the integers give 2 mm and 1 mm (mean 1.5 mm).
        self.assertEqual(averaged["accuracy_observation"]["vertical_mean_deviation_m"], 0.0015)
        # One sample without alt_mm refuses the average, whichever sample is last.
        for order in ((dict(FIX_E8, alt_mm=None), dict(FIX_E8)),
                      (dict(FIX_E8), dict(FIX_E8, alt_mm=None))):
            with self.assertRaisesRegex(ValueError, "no exact position"):
                tracking.average_fixes(list(order))
        # The mirror of the float-only test: the last sample is the exact one.
        with self.assertRaisesRegex(ValueError, "no exact position"):
            tracking.average_fixes([dict(FIX_FLOAT, lat=50.000001), dict(FIX_E8)])
        # The spread comes from the integers: equal floats, e8 symmetric about the mean.
        averaged = tracking.average_fixes(
            [dict(FIX_E8, lat_e8=5000000000 + delta) for delta in (-2, 0, 2)])
        self.assertEqual(averaged["accuracy_observation"]["horizontal_max_deviation_m"], 0.0022)
        # A non-integer geoid separation does not produce an integer mean.
        self.assertNotIn("geoid_sep_mm", tracking.average_fixes(
            [dict(FIX_E8, geoid_sep_mm=45600.5), dict(FIX_E8, geoid_sep_mm=45603)]))

    def test_average_fixes_derives_the_floats_from_the_integers(self):
        # The sample floats are wrong on purpose, so only a derivation passes.
        fixes = [dict(FIX_E8, lat=0.0, lon=0.0, alt=0.0, lat_e8=5000000000 + delta,
                      lon_e8=800000000 + delta, alt_mm=123400 + delta) for delta in (0, 3, 6)]
        mean = tracking.average_fixes(fixes)
        self.assertEqual(mean["lat_e8"], 5000000003)
        self.assertEqual(mean["lat"], geo.e8_to_deg(mean["lat_e8"]))
        self.assertEqual(mean["lon"], geo.e8_to_deg(mean["lon_e8"]))
        self.assertEqual(mean["alt"], mean["alt_mm"] / 1000)
        self.assertEqual(mean["altitude_msl_m"], mean["alt_mm"] / 1000)

    def test_undo_removes_last_recorded_feature_and_survives_reload(self):
        self.tracker.create_project("Test")
        self.tracker.start_line("Line", 0, fix=FIX)
        self.tracker.add_asset("tap", "T-01", "", FIX)
        self.tracker.undo_last()
        restored = tracking.Tracker(self.path)
        self.assertEqual(restored.status()["projects"][0]["point_count"], 0)
        self.assertEqual(restored.status()["active_line"]["vertex_count"], 1)

    def test_object_points_receive_stable_automatic_names(self):
        self.tracker.create_project("Names")
        first = self.tracker.add_asset("tap", "", "", FIX)
        second = self.tracker.add_asset("tap", "", "", FIX)
        control = self.tracker.add_asset("control", "", "", FIX)
        self.assertEqual((first["name"], second["name"], control["name"]),
                         ("O-1", "O-2", "K-1"))

    def test_live_status_names_exact_undo_candidate(self):
        self.tracker.create_project("Undo candidate")
        point = self.tracker.add_asset("tap", "Valve A", "", FIX)
        candidate = self.tracker.live_status()["undo_candidate"]
        self.assertEqual(candidate["kind"], "asset")
        self.assertEqual(candidate["feature_id"], point["id"])
        self.assertEqual(candidate["label"], "Valve A")

    def test_project_summary_reports_progress_and_latest_quality(self):
        self.tracker.create_project("Progress")
        self.assertEqual(self.tracker.status()["projects"][0]["progress"], "empty")
        self.tracker.start_line("Line", 0, fix=FIX)
        summary = self.tracker.status()["projects"][0]
        self.assertEqual(summary["progress"], "recording")
        self.assertTrue(summary["last_quality_accepted"])

    def test_compaction_preserves_order_and_finished_lines_are_immutable(self):
        self.tracker.create_project("Test")
        self.tracker.start_line("Finished", 0, fix=FIX)
        self.tracker.set_line_state("finish")
        self.tracker.compact()
        self.tracker.start_line("Current", 0, fix=dict(FIX, lon=8.001, lon_e8=800100000))
        self.tracker.add_asset("tap", "Newest", "", FIX)
        self.tracker.undo_last()
        project = self.tracker.backup()["projects"][0]
        self.assertEqual(len(project["points"]), 0)
        self.assertEqual(len(project["lines"][0]["coordinates"]), 1)

    def test_missing_altitude_is_rejected(self):
        self.tracker.create_project("Test")
        with self.assertRaisesRegex(ValueError, "altitude_required"):
            self.tracker.start_line("Line", 0, fix=dict(FIX, alt=None, alt_mm=None))

    def test_map_data_contains_lines_and_assets(self):
        self.tracker.create_project("Test")
        self.tracker.start_line("Line", 0, fix=FIX)
        self.tracker.add_asset("valve", "V-01", "", FIX)
        preview = self.tracker.map_data()
        self.assertEqual(len(preview["lines"]), 1)
        self.assertEqual(preview["points"][0]["type"], "valve")
        self.assertEqual(preview["lines"][0]["vertex_count"], 1)
        self.assertEqual(preview["points"][0]["note"], "")
        measurement = preview["points"][0]["measurement"]
        self.assertEqual(measurement["fix_status"], "RTK_FIXED")
        self.assertEqual(measurement["satellites"], 24)
        self.assertEqual(measurement["hdop"], .6)
        self.assertEqual(measurement["correction_age_sec"], .5)
        self.assertEqual(measurement["station_id"], "0054")
        self.assertTrue(measurement["quality"]["accepted"])
        self.assertIn("receiver_accuracy", measurement)
        self.assertIn("accuracy_observation", measurement)
        self.assertFalse(preview["truncated"])
        self.assertNotIn("map", self.tracker.status())

    def test_map_and_line_detail_can_read_an_inactive_project(self):
        first = self.tracker.create_project("First")
        line = self.tracker.start_line("Line one", 0, fix=FIX)
        self.tracker.create_project("Second")
        preview = self.tracker.map_data(project_id=first["id"])
        self.assertEqual(preview["project_id"], first["id"])
        self.assertEqual(preview["lines"][0]["name"], "Line one")
        self.assertEqual(self.tracker.line_detail(
            line["id"], project_id=first["id"])["name"], "Line one")

    def test_status_describes_finished_lines_and_geometry_warnings(self):
        self.tracker.create_project("Test")
        self.tracker.start_line("Line", 0, fix=FIX)
        self.tracker.add_vertex(dict(FIX, alt=123.5, alt_mm=123500))
        self.tracker.set_line_state("finish")
        status = self.tracker.status()
        summary = status["active_project_lines"][0]
        self.assertIsNotNone(status["projects"][0]["created_at"])
        self.assertIsNotNone(summary["created_at"])
        self.assertIsNotNone(summary["finished_at"])
        self.assertEqual(summary["vertex_count"], 2)
        self.assertEqual(summary["quality_accepted_count"], 2)
        self.assertEqual(summary["quality_total_count"], 2)
        self.assertIn("overlapping_line_points", summary["warnings"])
        self.assertIn("line_has_no_horizontal_length", summary["warnings"])
        detail = self.tracker.line_detail(summary["id"])
        self.assertEqual(len(detail["vertices"]), 2)
        self.assertEqual(detail["vertices"][1]["segment_length_m"], 0.0)
        for vertex in detail["vertices"]:
            self.assertEqual(set(vertex), {
                "number", "coordinate", "precision", "segment_length_m", "vertical_step_m",
                "source", "fix_status", "satellites", "hdop", "correction_age_sec",
                "receiver_accuracy", "accuracy_observation", "quality",
                "recorded_at_utc", "clock_unsynced"})
            self.assertRegex(vertex["recorded_at_utc"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
            self.assertFalse(vertex["clock_unsynced"])
        self.assertNotIn("vertices", summary)
        first = self.tracker.line_detail(summary["id"], 0, 1)
        second = self.tracker.line_detail(summary["id"], 1, 1)
        self.assertTrue(first["has_more_vertices"])
        self.assertEqual(first["vertices"][0]["number"], 1)
        self.assertFalse(second["has_more_vertices"])
        self.assertEqual(second["vertices"][0]["number"], 2)

    def test_line_summary_flags_large_horizontal_and_vertical_steps(self):
        self.tracker.create_project("Test")
        line = self.tracker.start_line("Line", 0, fix=FIX)
        self.tracker.add_vertex(dict(FIX, lon=8.00004, lon_e8=800004000, alt=124.2, alt_mm=124200))
        summary = self.tracker.status()["active_project_lines"][0]
        self.assertIn("large_line_gaps", summary["warnings"])
        self.assertIn("large_vertical_steps", summary["warnings"])
        self.assertGreater(summary["max_gap_m"], 2)
        self.assertGreater(summary["max_vertical_step_m"], 0.5)
        # Millimetres in the store, metres in the report.
        self.assertAlmostEqual(summary["max_vertical_step_m"], 0.8, places=3)
        detail = self.tracker.line_detail(line["id"])
        self.assertEqual(detail["vertex_count"], 2)
        self.assertAlmostEqual(detail["vertices"][1]["vertical_step_m"], 0.8, places=3)
        self.assertAlmostEqual(detail["vertices"][1]["segment_length_m"], 2.868, delta=0.005)

    def test_repeated_named_control_reports_before_after_delta(self):
        self.tracker.create_project("Test")
        self.tracker.add_asset("control", "CP-01", "before", FIX)
        self.tracker.add_asset("control", "CP-01", "after",
                               dict(FIX, lon=8.000001, lon_e8=800000100, alt=123.45, alt_mm=123450))
        check = self.tracker.status()["control_checks"][0]
        self.assertEqual(check["name"], "CP-01")
        self.assertEqual(check["measurements"], 2)
        self.assertGreater(check["horizontal_delta_m"], 0)
        self.assertAlmostEqual(check["vertical_delta_m"], 0.05, places=3)

    def test_backup_restore_rename_delete_and_compact(self):
        project = self.tracker.create_project("Original")
        self.tracker.start_line("Line", 0, fix=FIX)
        self.tracker.set_line_state("finish")
        self.tracker.rename_project(project["id"], "Renamed")
        backup = self.tracker.backup()
        restored_path = os.path.join(self.tmp.name, "restored.jsonl")
        restored = tracking.Tracker(restored_path)
        restored.restore(backup)
        self.assertEqual(restored.status()["projects"][0]["name"], "Renamed")
        restored.compact()
        again = tracking.Tracker(restored_path)
        self.assertEqual(again.status()["projects"][0]["line_count"], 1)
        again.delete_project(project["id"])
        self.assertEqual(again.status()["projects"], [])

    def test_ndjson_backup_round_trip(self):
        self.tracker.create_project("Original")
        self.tracker.start_line("Line", 0, fix=FIX)
        self.tracker.add_asset("valve", "V", "note", FIX)
        records = list(self.tracker.iter_backup_ndjson())
        self.assertGreater(len(records), 4)
        restored_payload = tracking_reference.parse_backup_ndjson(records)
        restored = tracking.Tracker(os.path.join(self.tmp.name, "ndjson"))
        restored.restore(restored_payload)
        self.assertEqual(restored.feature_count(), 2)
        self.assertEqual(restored.backup()["schema_version"], 5)

    def test_ndjson_restore_stages_records_without_full_payload(self):
        self.tracker.create_project("Streaming")
        self.tracker.start_line("Line", 0, fix=FIX)
        self.tracker.add_vertex(dict(FIX, lon=8.00002, lon_e8=800002000))
        records = list(self.tracker.iter_backup_ndjson())
        restored = tracking.Tracker(os.path.join(self.tmp.name, "streamed"))
        result = restored.restore_ndjson(iter(records))
        self.assertEqual(result["features"], 2)
        self.assertEqual(restored.feature_count(), 2)
        self.assertEqual(restored.backup()["projects"][0]["lines"][0][
            "measurements"][1]["station_id"], FIX["station_id"])

    def test_failed_streaming_restore_keeps_existing_store(self):
        self.tracker.create_project("Existing")
        self.tracker.start_line("Line", 0, fix=FIX)
        before = self.tracker.backup()
        broken = list(self.tracker.iter_backup_ndjson())[:-1]
        with self.assertRaisesRegex(ValueError, "integrity"):
            self.tracker.restore_ndjson(iter(broken))
        self.assertEqual(self.tracker.backup(), before)

    def test_explicit_restore_upgrades_legacy_schema_one_coordinates(self):
        self.tracker.create_project("Legacy")
        self.tracker.start_line("Line", 0, fix=FIX)
        self.tracker.add_asset("valve", "V", "note", FIX)
        backup = self.tracker.backup()
        # Schema 1 stored float degrees and two-dimensional projections.
        backup["schema_version"] = 1
        line = backup["projects"][0]["lines"][0]
        line["coordinates"] = [[8.0, 50.0, 123.4]]
        point = backup["projects"][0]["points"][0]
        point["coordinate"] = [8.0, 50.0, 123.4]
        point["projected_coordinate"] = [8.0, 50.0]
        for measurement in line["measurements"] + [point["measurement"]]:
            measurement.pop("precision")

        restored = tracking.Tracker(os.path.join(self.tmp.name, "legacy"))
        restored.restore(backup)

        restored_point = restored.backup()["projects"][0]["points"][0]
        self.assertEqual(restored.backup()["schema_version"], 5)
        self.assertEqual(restored_point["projected_coordinate"], [800000000, 5000000000, 123400])
        self.assertEqual(restored_point["coordinate"], [800000000, 5000000000, 123400])
        self.assertEqual(restored_point["measurement"]["precision"], "legacy_float32")
        restored_line = restored.backup()["projects"][0]["lines"][0]
        self.assertEqual(restored_line["coordinates"], [[800000000, 5000000000, 123400]])
        self.assertEqual(restored_line["measurements"][0]["precision"], "legacy_float32")

    # Survey data as V15.8.1 (schema 4) wrote it: float degrees and metres.
    LEGACY_MEASUREMENT = {"recorded_at": 3, "fix_quality": 4, "fix_status": "RTK_FIXED",
                          "satellites": 24, "hdop": 0.6, "correction_age_sec": 0.5,
                          "station_id": "0054", "source": "start",
                          "quality": {"accepted": True, "reasons": []}}

    @staticmethod
    def _with_integrity(rows):
        """NDJSON text plus the integrity record, computed like iter_backup_ndjson does."""
        raws = [row if isinstance(row, str) else json.dumps(row) + "\n" for row in rows]
        digest = hashlib.sha256()
        for raw in raws:
            digest.update(raw.encode("utf-8"))
        return "".join(raws) + json.dumps({"record": "integrity", "algorithm": "sha256",
                                           "content_sha256": digest.hexdigest(),
                                           "records": len(raws)}) + "\n"

    def _legacy_asset(self, projected_altitude=100.0):
        return {"id": "point-1", "object_type": "valve", "name": "V-1", "note": "",
                "line_id": "l1", "chainage_m": 0.7, "lateral_offset_m": 0.25,
                "side": "left", "coordinate": [8.00001, 50.0, 100.0],
                "projected_coordinate": [8.00001, 50.0, projected_altitude],
                "measurement": dict(self.LEGACY_MEASUREMENT, recorded_at=5, source="asset")}

    def _schema4_backup_rows(self, projected_altitude=100.0):
        return [
            {"kind": "survey_backup", "schema_version": 4, "record": "header", "profiles": []},
            {"record": "project", "project": {"id": "p1", "name": "Old", "description": "",
                                              "created_at": 1}},
            {"record": "line", "project_id": "p1", "line": {
                "id": "l1", "name": "L", "state": "finished", "created_at": 2,
                "finished_at": 6, "properties": {},
                "_summary": {"vertex_count": 2, "length_m": 999.0}}},
            {"record": "vertex", "project_id": "p1", "line_id": "l1",
             "coordinate": [8.0, 50.0, 123.4], "measurement": dict(self.LEGACY_MEASUREMENT)},
            {"record": "vertex", "project_id": "p1", "line_id": "l1",
             "coordinate": [8.00002, 50.0, -2.5],
             "measurement": dict(self.LEGACY_MEASUREMENT, recorded_at=4, source="automatic")},
            {"record": "asset", "project_id": "p1", "point": self._legacy_asset(projected_altitude)},
            {"record": "footer", "active_project_id": "p1", "active_line_id": None,
             "auto_distance_m": 1.0}]

    def _write_schema4_archive(self, projected_altitude=100.0):
        """Write and register a compact project archive as V15.8.1 wrote it."""
        rows = self._schema4_backup_rows(projected_altitude)
        rows[0]["encoding"] = "compact-measurements-v1"
        compact = []
        for row in rows:
            if row.get("record") == "vertex":
                row = ["v", row["project_id"], row["line_id"], row["coordinate"],
                       _compact(row["measurement"])]
            elif row.get("record") == "asset":
                point = dict(row["point"], measurement=_compact(row["point"]["measurement"]))
                row = ["a", row["project_id"], point]
            compact.append(row)
        text = self._with_integrity(compact)
        os.makedirs(self.tracker.archive_dir, exist_ok=True)
        path = self.tracker.archive_dir + "/archive-legacy.ndjson"
        with open(path, "w", encoding="utf-8") as target:
            target.write(text)
        digest = hashlib.sha256("".join(text.splitlines(keepends=True)[:-1]).encode()).hexdigest()
        self.tracker.archives.append({
            "id": "archive-legacy", "project_id": "p1", "name": "Old", "created_at": 1,
            "archived_at": 7, "point_count": 3, "line_count": 1,
            "size_bytes": os.stat(path).st_size, "schema_version": 4, "sha256": digest})
        return path

    def test_height_chain_in_exports(self):
        self.tracker.create_project("Heights")
        with mock.patch.dict(cfg.CONFIG, {"antenna_height_m": 1.8}):
            self.tracker.add_asset("tap", "A", "", dict(FIX, alt_mm=125200), link_to_active_line=False)
        with mock.patch.dict(cfg.CONFIG, {"antenna_height_m": 2.0}):
            self.tracker.add_asset("tap", "B", "", dict(FIX, alt_mm=125400), link_to_active_line=False)
        self.tracker.add_asset("tap", "C", "", dict(FIX, geoid_sep_mm=None), link_to_active_line=False)
        self.tracker.add_asset("tap", "D", "", dict(FIX, alt_mm=-2500, geoid_sep_mm=-45600), link_to_active_line=False)
        features = json.loads("".join(self.tracker.iter_geojson()))["features"]
        a, b, c, d = (f["properties"] for f in features)
        self.assertEqual(a["ground_height_msl_m"], b["ground_height_msl_m"])
        self.assertEqual(a["ground_height_msl_m"], 123.4)
        self.assertEqual(a["altitude_msl_m"], 125.2)
        self.assertEqual(a["altitude_ellipsoid_m"], 170.8)
        self.assertEqual(a["geoid_separation_m"], 45.6)
        self.assertEqual(a["antenna_height_m"], 1.8)
        self.assertEqual(a["height_reference"], "antenna_phase_centre_msl")
        self.assertIsNone(c["altitude_ellipsoid_m"]); self.assertIsNone(c["geoid_separation_m"])
        self.assertEqual(c["ground_height_msl_m"], 123.4)      # configured antenna height 0 -> ground = MSL
        self.assertEqual(d["altitude_ellipsoid_m"], -48.1); self.assertEqual(d["ground_height_msl_m"], -2.5)
        self.assertEqual(json.loads("".join(self.tracker.iter_geojson())),
                         json.loads(json.dumps(tracking_reference.geojson(self.tracker))))
        header = self.tracker.csv().splitlines()[0].split(",")
        start = header.index("altitude_msl_m")
        self.assertEqual(header[start:start + 6], ["altitude_msl_m", "altitude_ellipsoid_m", "geoid_separation_m",
                                                    "antenna_height_m", "ground_height_msl_m", "height_reference"])
        # HEIGHT_KEYS is the single key list: the CSV block and the GeoJSON point properties follow it.
        self.assertEqual(tuple(header[start:start + len(tracking.HEIGHT_KEYS)]), tracking.HEIGHT_KEYS)
        point_properties = json.loads(json.dumps(tracking_reference.geojson(self.tracker)))["features"][0]["properties"]
        self.assertTrue(set(tracking.HEIGHT_KEYS) <= set(point_properties))
        self.assertEqual(header[-6:], ["gate_profile", "precision", "recorded_at", "recorded_at_utc", "clock_unsynced", "note"])
        rows = self.tracker.csv().splitlines()
        self.assertIn('"125.200","170.800","45.600","1.800","123.400","antenna_phase_centre_msl"', rows[1])
        self.assertIn('"123.400","","","0.000","123.400","antenna_phase_centre_msl"', rows[3])   # empty, never zeros
        self.assertIn('"-2.500","-48.100","-45.600","0.000","-2.500"', rows[4])                   # no formula prefix
        preview = self.tracker.map_data()
        self.assertEqual(preview["points"][0]["measurement"]["antenna_height_mm"], 1800)
        self.assertEqual(preview["points"][0]["measurement"]["alt_ellipsoid_mm"], 170800)
        self.assertEqual(preview["points"][0]["measurement"]["geoid_sep_mm"], 45600)
        revision = preview["revision"]
        with mock.patch.dict(cfg.CONFIG, {"antenna_height_m": 1.5}):
            self.tracker.add_asset("tap", "E", "", FIX, link_to_active_line=False)
        delta = self.tracker.map_data(since_revision=revision)["changes"][0]["point"]["measurement"]
        self.assertEqual((delta["antenna_height_mm"], delta["alt_ellipsoid_mm"], delta["geoid_sep_mm"]),
                         (1500, 169000, 45600))

    def _route_fix(self, **extra):
        return dict(FIX, utc="1", **extra)

    def test_queued_route_fix_keeps_the_setup_of_queue_time(self):
        self.tracker.create_project("Route")
        self.tracker.start_line("Drive", 1, recording_mode="route")
        with mock.patch.dict(cfg.CONFIG, {"antenna_height_m": 1.8, "reference_frame": "ETRS89 (synthetic)",
                                          "reference_frame_epoch": "2010.0", "ntrip_mount": "QUEUED1"}), \
                mock.patch.dict(state.app.stats, {"ntrip_state": "streaming", "ntrip_endpoint": "primary"}):
            self.assertTrue(self.tracker.enqueue_fix(self._route_fix(alt_mm=125200)))
        # The operator changes the setup before the worker gets to the fix.
        with mock.patch.dict(cfg.CONFIG, {"antenna_height_m": 2.0, "reference_frame": "OTHER",
                                          "reference_frame_epoch": "1999.0", "ntrip_mount": "LATER1"}), \
                mock.patch.dict(state.app.stats, {"ntrip_state": "disabled"}):
            measurement = self.tracker.add_vertex(self.tracker._pending_fixes.pop(0), source="automatic")
        self.assertEqual(measurement["antenna_height_mm"], 1800)
        self.assertEqual((measurement["reference_frame"], measurement["reference_frame_epoch"],
                          measurement["ntrip_mount"]), ("ETRS89 (synthetic)", "2010.0", "QUEUED1"))
        self.assertEqual([key for key in measurement if key.startswith("_")], [])
        stored = tracking_reference.geojson(self.tracker)["features"][0]["properties"]["measurements"][0]
        self.assertEqual((stored["antenna_height_m"], stored["ground_height_msl_m"]), (1.8, 123.4))
        self.assertNotIn("_stamps", json.dumps(tracking_reference.geojson(self.tracker)))
        self.assertNotIn("_stamps", self.tracker.csv())

    def test_queued_route_fix_keeps_a_zero_antenna_height(self):
        self.tracker.create_project("Route")
        self.tracker.start_line("Drive", 1, recording_mode="route")
        with mock.patch.dict(cfg.CONFIG, {"antenna_height_m": 0.0}):
            self.assertTrue(self.tracker.enqueue_fix(self._route_fix()))
        with mock.patch.dict(cfg.CONFIG, {"antenna_height_m": 2.0}):
            measurement = self.tracker.add_vertex(self.tracker._pending_fixes.pop(0), source="automatic")
        self.assertEqual(measurement["antenna_height_mm"], 0)

    def test_invalid_configured_antenna_height_stamps_none_and_logs_once(self):
        logged = []
        with mock.patch.object(state.app, "log_error", lambda *args: logged.append(args), create=True):
            for bad in (float("nan"), 5.0, -1.0, "abc", True, float("inf"), [1]):
                with mock.patch.dict(cfg.CONFIG, {"antenna_height_m": bad}):
                    measurement = self.tracker._measurement(FIX)
                self.assertIsNone(measurement["antenna_height_mm"], repr(bad))
                tracking._validate_measurement(measurement)
            self.assertIsNone(tracking._height_fields(measurement, 123400)["ground_height_msl_m"])
            first = len(logged)
            with mock.patch.dict(cfg.CONFIG, {"antenna_height_m": 5.0}):
                self.tracker._measurement(FIX); self.tracker._measurement(FIX)
                self.tracker._measurement(FIX)
            self.assertEqual(len(logged), first + 1)      # a value is logged once, not per measurement
        self.assertGreaterEqual(first, 1)
        with mock.patch.dict(cfg.CONFIG, {"antenna_height_m": "1.8"}):
            self.assertEqual(self.tracker._measurement(FIX)["antenna_height_mm"], 1800)
        with mock.patch.dict(cfg.CONFIG, {"antenna_height_m": None}):
            self.assertEqual(self.tracker._measurement(FIX)["antenna_height_mm"], 0)
        # An invalid value queued and processed later is stamped None as well, without raising.
        self.tracker.create_project("Route")
        self.tracker.start_line("Drive", 1, recording_mode="route")
        with mock.patch.dict(cfg.CONFIG, {"antenna_height_m": float("nan")}):
            self.assertTrue(self.tracker.enqueue_fix(self._route_fix()))
        measurement = self.tracker.add_vertex(self.tracker._pending_fixes.pop(0), source="automatic")
        self.assertIsNone(measurement["antenna_height_mm"])
        vertex = tracking_reference.geojson(self.tracker)["features"][0]["properties"]["measurements"][0]
        self.assertIsNone(vertex["ground_height_msl_m"])

    def test_control_check_compares_ground_heights(self):
        self.tracker.create_project("Controls")
        with mock.patch.dict(cfg.CONFIG, {"antenna_height_m": 1.8}):
            self.tracker.add_asset("control", "CP-01", "", dict(FIX, alt_mm=125200))
        with mock.patch.dict(cfg.CONFIG, {"antenna_height_m": 2.0}):
            self.tracker.add_asset("control", "CP-01", "", dict(FIX, alt_mm=125400))
        check = self.tracker.status()["control_checks"][0]
        self.assertEqual(check["vertical_reference"], "ground")
        self.assertEqual(check["vertical_delta_m"], 0.0)         # both marks export ground 123.400
        with mock.patch.dict(cfg.CONFIG, {"antenna_height_m": 2.0}):
            self.tracker.add_asset("control", "CP-01", "", dict(FIX, alt_mm=125450))
        check = self.tracker.status()["control_checks"][0]
        self.assertEqual((check["vertical_reference"], check["vertical_delta_m"]), ("ground", 0.05))

    def test_control_check_falls_back_to_phase_centre_without_antenna_heights(self):
        self.tracker.create_project("Controls")
        self.tracker.add_asset("control", "CP-02", "", FIX)
        self.tracker.add_asset("control", "CP-02", "", dict(FIX, alt_mm=123450))
        project = self.tracker._project()
        for point in project["points"]:
            point["measurement"]["antenna_height_mm"] = None       # legacy points
        self.tracker._status_cache = None
        check = self.tracker.status()["control_checks"][0]
        self.assertEqual((check["vertical_reference"], check["vertical_delta_m"]), ("phase_centre", 0.05))
        project["points"][1]["measurement"]["antenna_height_mm"] = 1800   # only one side known
        self.tracker._status_cache = None
        self.assertEqual(self.tracker.status()["control_checks"][0]["vertical_reference"], "phase_centre")

    def test_geojson_stream_and_dict_agree_on_a_misaligned_line(self):
        self.tracker.create_project("Misaligned")
        self.tracker.start_line("L", 0, fix=FIX)
        self.tracker.add_vertex(dict(FIX, lat_e8=5000000100, lat=50.000001), source="manual")
        self.tracker.set_line_state("finish")
        for name in ("measurements", "coordinates"):
            line = self.tracker._project()["lines"][0]
            original = line[name]
            try:
                line[name] = list(original) + [original[-1]]       # one extra entry on one side
                streamed = json.loads("".join(self.tracker.iter_geojson()))
                built = json.loads(json.dumps(tracking_reference.geojson(self.tracker)))
                self.assertEqual(streamed, built, name)
                self.assertEqual(len(streamed["features"][0]["properties"]["measurements"]), 2, name)
            finally:
                line[name] = original

    def test_line_vertices_export_the_height_chain(self):
        self.tracker.create_project("Heights")
        with mock.patch.dict(cfg.CONFIG, {"antenna_height_m": 1.8}):
            self.tracker.start_line("L", 0, fix=dict(FIX, alt_mm=125200))
        measurement = tracking_reference.geojson(self.tracker)["features"][0]["properties"]["measurements"][0]
        self.assertEqual((measurement["altitude_msl_m"], measurement["altitude_ellipsoid_m"],
                          measurement["geoid_separation_m"], measurement["antenna_height_m"],
                          measurement["ground_height_msl_m"], measurement["height_reference"]),
                         (125.2, 170.8, 45.6, 1.8, 123.4, "antenna_phase_centre_msl"))
        self.assertEqual(json.loads("".join(self.tracker.iter_geojson())),
                         json.loads(json.dumps(tracking_reference.geojson(self.tracker))))

    def test_legacy_points_keep_missing_antenna_height(self):
        result = self.tracker.restore_ndjson(self._with_integrity(self._schema4_backup_rows()).splitlines(keepends=True))
        self.assertEqual(result["legacy_values_skipped"], 0)
        with mock.patch.dict(cfg.CONFIG, {"antenna_height_m": 1.8}):   # must not be substituted
            features = json.loads("".join(self.tracker.iter_geojson()))["features"]
            rows = self.tracker.csv().splitlines()
        point = [f for f in features if f["id"] == "point-1"][0]["properties"]
        self.assertEqual(point["measurement"]["precision"], "legacy_float32")
        self.assertEqual(point["altitude_msl_m"], 100.0)
        for key in ("altitude_ellipsoid_m", "geoid_separation_m", "antenna_height_m", "ground_height_msl_m"):
            self.assertIsNone(point[key], key)
        self.assertEqual(point["height_reference"], "antenna_phase_centre_msl")
        line = [f for f in features if f["id"] == "l1"][0]["properties"]["measurements"]
        self.assertEqual([m["altitude_msl_m"] for m in line], [123.4, -2.5])
        for measurement in line:
            for key in ("altitude_ellipsoid_m", "geoid_separation_m", "antenna_height_m", "ground_height_msl_m"):
                self.assertIsNone(measurement[key], key)
        self.assertIn('"100.000","","","","","antenna_phase_centre_msl"', rows[1])
        self.assertNotIn("None", rows[1])
        self.assertIsNone(self.tracker.projects[0]["points"][0]["measurement"].get("antenna_height_mm"))

    def test_geojson_text_coordinates_are_exact_and_negative_safe(self):
        self.tracker.create_project("Exact")
        self.tracker.add_asset("tap", "T", "", dict(FIX, lon_e8=-65432100, lat_e8=-3350000000, alt_mm=-2500),
                               link_to_active_line=False)
        text = "".join(self.tracker.iter_geojson())
        self.assertIn('"coordinates":[-0.65432100,-33.50000000,-2.500]', text.replace(" ", ""))
        self.assertEqual(json.loads(text)["features"][0]["properties"]["measurement"]["precision"], "e8")
        self.assertEqual(json.loads(text), json.loads(json.dumps(tracking_reference.geojson(self.tracker))))

    def test_csv_has_a_precision_column_and_no_formula_prefix_on_numbers(self):
        self.tracker.create_project("Exact")
        self.tracker.add_asset("tap", "T-neg", "=note", dict(
            FIX, lon_e8=-65432100, lat_e8=-3350000000, alt_mm=-2500),
            link_to_active_line=False)
        lines = self.tracker.csv().splitlines()
        header, row = lines[0].split(","), lines[1]
        self.assertIn(",gate_profile,precision,recorded_at,recorded_at_utc,clock_unsynced,note", lines[0])
        self.assertEqual(header[-6:], ["gate_profile", "precision", "recorded_at", "recorded_at_utc", "clock_unsynced", "note"])
        self.assertEqual(len(header), 39)
        self.assertIn('"-0.65432100"', row)
        self.assertNotIn('"\'-0.65432100"', row)
        self.assertIn('"-33.50000000","-2.500"', row)
        self.assertNotIn('"\'-', row)
        # The profile cell precedes the precision cell, the time cells follow, the
        # note is last; a text cell that starts like a formula keeps its guard.
        self.assertRegex(row, r'"e8","\d+","\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ","false","\'=note"$')
        self.assertIn('"T-neg"', row)

    def test_exports_and_map_carry_the_gate_profile(self):
        self.tracker.create_project("Profiles")
        self.tracker.add_asset("tap", "T-default", "", FIX, link_to_active_line=False)
        with mock.patch.dict(cfg.CONFIG, {"measurement_max_hdop": 2.0}):
            self.tracker.add_asset("tap", "T-loose", "", dict(FIX, hdop=1.8), link_to_active_line=False)
        points = self.tracker.projects[0]["points"]
        ids = [p["measurement"]["profile_id"] for p in points]
        self.assertNotEqual(ids[0], ids[1])
        streamed = json.loads("".join(self.tracker.iter_geojson()))
        self.assertEqual([f["properties"]["gate_profile"] for f in streamed["features"]], ids)
        self.assertEqual(streamed["features"][1]["properties"]["gate_limits"]["max_hdop"], 2.0)
        self.assertEqual(streamed["features"][1]["properties"]["gate_limits"],
                         self.tracker.profiles[ids[1]]["limits"])
        self.assertEqual(streamed, json.loads(json.dumps(tracking_reference.geojson(self.tracker))))
        rows = [line.split('","') for line in self.tracker.csv().splitlines()]
        header = self.tracker.csv().splitlines()[0].split(",")
        column = header.index("gate_profile")
        self.assertEqual(header[column + 1], "precision")
        self.assertEqual(rows[1][column].strip('"'), ids[0])
        self.assertEqual(rows[2][column].strip('"'), ids[1])
        preview = self.tracker.map_data()
        self.assertEqual(preview["points"][1]["measurement"]["profile_id"], ids[1])
        self.assertEqual(preview["profiles"][ids[1]]["max_hdop"], 2.0)
        self.assertEqual(sorted(preview["profiles"]), sorted(ids))
        self.assertEqual(json.loads("".join(self.tracker.iter_map_data()))["profiles"], preview["profiles"])
        revision = preview["revision"]
        with mock.patch.dict(cfg.CONFIG, {"measurement_max_hdop": 3.0}):
            self.tracker.add_asset("tap", "T-looser", "", dict(FIX, hdop=2.5), link_to_active_line=False)
        delta = self.tracker.map_data(since_revision=revision)
        change = next(item for item in delta["changes"] if item["op"] == "asset")
        third = self.tracker.projects[0]["points"][2]["measurement"]["profile_id"]
        self.assertEqual(change["point"]["measurement"]["profile_id"], third)
        self.assertEqual(change["point"]["measurement"]["precision"], "e8")
        self.assertEqual(list(delta["profiles"]), [third])
        self.assertEqual(delta["profiles"][third]["max_hdop"], 3.0)
        # A delta without asset changes carries an empty table; an empty map too.
        empty = self.tracker.map_data(since_revision=delta["revision"])
        self.assertEqual(empty["profiles"], {})
        self.assertEqual(empty["changes"], [])

    def test_csv_keeps_the_formula_guard_on_text_cells(self):
        self.tracker.create_project("Guard")
        self.tracker.add_asset("tap", "-minus", "@at", FIX, link_to_active_line=False)
        row = self.tracker.csv().splitlines()[1]
        self.assertIn('"\'-minus"', row)
        self.assertRegex(row, r'"e8","\d+","[^"]+Z","false","\'@at"$')

    def test_geojson_with_several_features_stays_valid_json(self):
        self.tracker.create_project("Several")
        self.tracker.start_line("L", 0, fix=FIX)
        self.tracker.add_vertex(dict(FIX, lon_e8=800002000, alt_mm=-2500))
        self.tracker.add_asset("tap", "Linked", "", dict(FIX, lon_e8=800001000))
        self.tracker.add_asset("tap", "Free", "", FIX, link_to_active_line=False)
        text = "".join(self.tracker.iter_geojson())
        parsed = json.loads(text)
        self.assertEqual(len(parsed["features"]), 3)
        line, linked, free = parsed["features"]
        self.assertEqual(line["geometry"]["type"], "LineString")
        self.assertEqual(line["geometry"]["coordinates"],
                         [[8.0, 50.0, 123.4], [8.00002, 50.0, -2.5]])
        compact = text.replace(" ", "")
        self.assertIn('"coordinates":[[8.00000000,50.00000000,123.400],[8.00002000,50.00000000,-2.500]]',
                      compact)
        # The linked asset carries the projection as a text triple, the other null.
        self.assertIsNotNone(linked["properties"]["projected_coordinate"])
        self.assertRegex(compact, r'"projected_coordinate":\[\d+\.\d{8},\d+\.\d{8},-?\d+\.\d{3}\]')
        self.assertIsNone(free["properties"]["projected_coordinate"])
        self.assertIn('"projected_coordinate":null', compact)
        self.assertEqual(linked["properties"]["measurement"]["reference_frame"], "unknown")
        self.assertEqual(line["properties"]["measurements"][0]["reference_frame"], "unknown")
        self.assertEqual(json.loads(text), json.loads(json.dumps(tracking_reference.geojson(self.tracker))))
        # Storage keeps None for the unknown frame; only the export spells it out.
        stored = self.tracker.projects[0]["points"][0]["measurement"]
        self.assertIsNone(stored["reference_frame"])

    def test_map_and_line_detail_expose_precision_and_the_legacy_count(self):
        rows = self._schema4_backup_rows()
        rows[2]["line"]["state"] = "recording"
        rows[-1]["active_line_id"] = "l1"
        result = self.tracker.restore_ndjson(self._with_integrity(rows).splitlines(keepends=True))
        self.assertEqual(result["legacy_values_skipped"], 0)
        self.tracker.add_vertex(dict(FIX, lon_e8=800004000))
        line_id = self.tracker.projects[0]["lines"][0]["id"]
        detail = self.tracker.line_detail(line_id)
        self.assertEqual([row["precision"] for row in detail["vertices"]],
                         ["legacy_float32", "legacy_float32", "e8"])
        self.assertEqual(detail["legacy_vertex_count"], 2)
        # The count covers the whole line, not the requested page.
        page = self.tracker.line_detail(line_id, 2, 1)
        self.assertEqual([row["precision"] for row in page["vertices"]], ["e8"])
        self.assertEqual(page["legacy_vertex_count"], 2)
        self.assertEqual(self.tracker.status()["active_project_lines"][0]["legacy_vertex_count"], 2)
        # The incremental append kept the cached summary consistent with a rescan.
        line = self.tracker.projects[0]["lines"][0]
        self.assertEqual(line["_summary"]["legacy_vertex_count"], 2)
        cached = dict(line["_summary"])
        line.pop("_summary")
        self.assertEqual(self.tracker._line_summary(line)["legacy_vertex_count"], 2)
        self.assertEqual(line["_summary"]["legacy_vertex_count"], cached["legacy_vertex_count"])
        # A summary written before the count existed is rebuilt, not trusted.
        line["_summary"].pop("legacy_vertex_count")
        self.assertEqual(self.tracker.line_detail(line_id)["legacy_vertex_count"], 2)
        self.assertEqual(line["_summary"]["legacy_vertex_count"], 2)
        line["_summary"].pop("legacy_vertex_count")
        self.tracker.add_vertex(dict(FIX, lon_e8=800006000))
        self.assertEqual(line["_summary"]["legacy_vertex_count"], 2)
        # An incremental legacy append is not possible (new points are e8), but the
        # map payloads carry the precision of the asset measurement.
        mapped = self.tracker.map_data()
        self.assertEqual(mapped["points"][0]["measurement"]["precision"], "legacy_float32")
        revision = mapped["revision"]
        self.tracker.add_asset("tap", "New", "", FIX, link_to_active_line=False)
        delta = self.tracker.map_data(since_revision=revision)
        self.assertEqual(delta["changes"][0]["point"]["measurement"]["precision"], "e8")
        self.tracker.add_vertex(dict(FIX, lon_e8=800008000))
        delta = self.tracker.map_data(since_revision=revision)
        self.assertEqual([item["op"] for item in delta["changes"]], ["asset", "vertex"])
        self.assertEqual(delta["changes"][1]["precision"], "e8")
        self.assertEqual(self.tracker.map_data()["points"][1]["measurement"]["precision"], "e8")

    def test_mismatched_cached_summary_is_rebuilt_not_zeroed(self):
        # The reset branch in _apply is reachable: restores pass the backup's
        # cached _summary into line_started. With earlier vertices present it
        # must rebuild the counters instead of zeroing them.
        self.tracker.create_project("Cache")
        self.tracker.start_line("L", 0, fix=FIX)
        self.tracker.add_vertex(dict(FIX, lon_e8=800010000))
        line = self.tracker.projects[0]["lines"][0]
        line["_summary"]["vertex_count"] = 7
        self.tracker.add_vertex(dict(FIX, lon_e8=800020000))
        cached = dict(line["_summary"])
        line.pop("_summary")
        rebuilt = self.tracker._line_summary(line)
        self.assertEqual(cached["vertex_count"], 3)
        self.assertEqual(cached["quality_accepted_count"], rebuilt["quality_accepted_count"])
        self.assertEqual(cached["large_gap_count"], rebuilt["large_gap_count"])
        self.assertAlmostEqual(cached["length_m"], rebuilt["length_m"], delta=0.001)
        # The restore path itself ends with a consistent summary.
        other = tracking.Tracker(os.path.join(self.tmp.name, "restored"))
        other.restore_ndjson(list(self.tracker.iter_backup_ndjson()))
        restored = other.projects[0]["lines"][0]
        self.assertEqual(restored["_summary"]["vertex_count"], 3)
        self.assertEqual(restored["_summary"]["quality_accepted_count"],
                         rebuilt["quality_accepted_count"])

    def test_schema5_measurement_without_precision_is_rejected(self):
        # W4: every stored measurement is e8 or legacy_float32; a schema-5 input
        # without the tag (edited or third-party file) is not stored untagged.
        rows = [json.loads(raw) for raw in self._schema5_survey_rows()][:-1]
        for row in rows:
            measurement = row.get("measurement") or (row.get("point") or {}).get("measurement")
            if measurement is not None and row.get("record") == "asset":
                measurement.pop("precision")
        untagged_asset = self._with_integrity(rows).splitlines(keepends=True)
        rows = [json.loads(raw) for raw in self._schema5_survey_rows()][:-1]
        next(row for row in rows if row.get("record") == "vertex")["measurement"].pop("precision")
        untagged_vertex = self._with_integrity(rows).splitlines(keepends=True)
        self.tracker.create_project("Existing")
        before = self.tracker.backup()
        for lines in (untagged_asset, untagged_vertex):
            with self.assertRaisesRegex(ValueError, "precision is required"):
                tracking_reference.parse_backup_ndjson(lines)
            with self.assertRaisesRegex(ValueError, "precision is required"):
                self.tracker.restore_ndjson(lines)
            self.assertEqual(self.tracker.backup(), before)
        payload = tracking_reference.parse_backup_ndjson(self._schema5_survey_rows())
        payload["projects"][0]["points"][0]["measurement"].pop("precision")
        with self.assertRaisesRegex(ValueError, "precision is required"):
            self.tracker.restore(payload)
        self.assertEqual(self.tracker.backup(), before)
        # Legacy inputs are still tagged on the way in.
        result = self.tracker.restore_ndjson(
            self._with_integrity(self._schema4_backup_rows()).splitlines(keepends=True))
        self.assertEqual(result["features"], 3)

    def test_schema5_ndjson_round_trip_is_bit_exact(self):
        self.tracker.create_project("Exact")
        self.tracker.start_line("L", 1, fix=FIX)
        self.tracker.add_vertex(dict(FIX, lon_e8=800002000, alt_mm=-2500))
        self.tracker.add_asset("tap", "T", "", dict(FIX, lat_e8=5000000001))
        first = "".join(self.tracker.iter_backup_ndjson())
        other = tracking.Tracker(os.path.join(self.tmp.name, "other.jsonl"))
        result = other.restore_ndjson(first.splitlines(keepends=True))
        self.assertEqual(result["schema_version"], 5)
        self.assertEqual(result["legacy_values_skipped"], 0)
        self.assertEqual("".join(other.iter_backup_ndjson()), first)
        self.assertIn('"precision": "e8"', first)
        self.assertIn('"coordinate": [800002000, 5000000000, -2500]', first)

    def test_schema4_ndjson_restore_converts_and_tags(self):
        header = '{"kind": "survey_backup", "schema_version": 4, "record": "header", "profiles": []}\n'
        meas = json.dumps(self.LEGACY_MEASUREMENT)
        rows = header + ('{"record": "project", "project": {"id": "p1", "name": "Old", "description": "", "created_at": 1}}\n'
                         '{"record": "line", "project_id": "p1", "line": {"id": "l1", "name": "L", "state": "finished", "properties": {}}}\n'
                         '{"record": "vertex", "project_id": "p1", "line_id": "l1", "coordinate": [8.0, 50.0, 123.4], "measurement": ' + meas + '}\n'
                         '{"record": "footer", "active_project_id": null, "active_line_id": null, "auto_distance_m": 1.0}\n')
        rows_with_integrity = self._with_integrity(rows.splitlines(keepends=True))
        result = self.tracker.restore_ndjson(rows_with_integrity.splitlines(keepends=True))
        self.assertEqual(result, {"schema_version": 5, "projects": 1, "features": 1,
                                  "legacy_values_skipped": 0})
        line = self.tracker.projects[0]["lines"][0]
        self.assertEqual(list(line["coordinates"]), [[800000000, 5000000000, 123400]])
        self.assertEqual(line["measurements"][0]["precision"], "legacy_float32")
        self.assertEqual(self.tracker.backup()["schema_version"], 5)
        # The same rows through the payload parser give the same schema-5 data.
        payload = tracking_reference.parse_backup_ndjson(rows_with_integrity.splitlines(keepends=True))
        self.assertEqual(payload["schema_version"], 5)
        self.assertEqual(payload["projects"][0]["lines"][0]["coordinates"],
                         [[800000000, 5000000000, 123400]])
        self.assertEqual(payload["projects"][0]["lines"][0]["measurements"][0]["precision"],
                         "legacy_float32")

    def test_schema4_ndjson_backup_converts_vertices_assets_and_line_summary(self):
        text = self._with_integrity(self._schema4_backup_rows())
        result = self.tracker.restore_ndjson(text.splitlines(keepends=True))
        self.assertEqual((result["features"], result["legacy_values_skipped"]), (3, 0))
        project = self.tracker.projects[0]
        line = project["lines"][0]
        self.assertEqual(list(line["coordinates"]), [[800000000, 5000000000, 123400],
                                                     [800002000, 5000000000, -2500]])
        point = project["points"][0]
        self.assertEqual(point["coordinate"], [800001000, 5000000000, 100000])
        self.assertEqual(point["projected_coordinate"], [800001000, 5000000000, 100000])
        self.assertEqual(point["measurement"]["precision"], "legacy_float32")
        # The cached summary came from the former float model and is rebuilt.
        self.assertLess(self.tracker._line_summary(line)["length_m"], 2.0)
        backup = self.tracker.backup()
        self.assertEqual(backup["schema_version"], 5)
        tracking.validate_backup(backup)

    def test_integer_coordinates_survive_a_float32_json_round_trip(self):
        # Emulate the device's ujson (32-bit floats, exact ints) on a schema-5 backup line.
        f32 = lambda text: struct.unpack("f", struct.pack("f", float(text)))[0]
        self.tracker.create_project("Exact")
        self.tracker.add_asset("tap", "T", "", dict(FIX, lat_e8=5112345678, lon_e8=1012345678),
                               link_to_active_line=False)
        row = next(line for line in self.tracker.iter_backup_ndjson() if '"record": "asset"' in line)
        reparsed = json.loads(row, parse_float=f32)
        self.assertEqual(reparsed["point"]["coordinate"], [1012345678, 5112345678, 123400])
        self.assertNotEqual(f32("51.12345678"), 51.12345678)  # the float path would have lost it

    def test_schema4_json_backup_restore_upgrades_to_five(self):
        payload = {"kind": "survey_backup", "schema_version": 4, "profiles": [],
                   "projects": [{"id": "p1", "name": "Old", "description": "",
                                 "created_at": 1, "lines": [], "points": [
                                     dict(self._legacy_asset(), line_id=None,
                                          projected_coordinate=None, chainage_m=None,
                                          lateral_offset_m=None, side=None,
                                          coordinate=[8.0, 50.0, 100.0])]}],
                   "active_project_id": "p1", "active_line_id": None, "auto_distance_m": 1.0}
        result = self.tracker.restore(payload)
        self.assertEqual(result, {"schema_version": 5, "projects": 1, "features": 1,
                                  "legacy_values_skipped": 0})
        backup = self.tracker.backup()
        self.assertEqual(backup["schema_version"], 5)
        point = backup["projects"][0]["points"][0]
        self.assertEqual(point["coordinate"], [800000000, 5000000000, 100000])
        self.assertEqual(point["measurement"]["precision"], "legacy_float32")
        # Integers labelled as schema 5 are never converted again.
        self.assertEqual(tracking.upgrade_restore_backup(backup), backup)

    LIMITS = {"max_hdop": 2.0, "min_satellites": 6}

    def test_legacy_profile_references_survive_the_upgrade(self):
        # Codex #2: V15.8.1 compact archives and backups carry the profile table
        # in the header and only a profile_id in each measurement.
        legacy_id = tracking._profile_id(self.LIMITS)
        profile = {"id": legacy_id, "version": 1, "limits": self.LIMITS}
        rows = self._schema4_backup_rows()
        rows[0]["profiles"] = [profile]
        for row in rows:
            measurement = (row.get("measurement") or (row.get("point") or {}).get("measurement"))
            if measurement is not None:
                measurement["profile_id"] = legacy_id
        # One measurement still carries inline limits (an older writer).
        rows[4]["measurement"]["quality"] = {"accepted": True, "reasons": [],
                                             "limits": {"max_hdop": 1.0}}
        rows[4]["measurement"].pop("profile_id")
        text = self._with_integrity(rows)
        payload = tracking_reference.parse_backup_ndjson(text.splitlines(keepends=True))
        ids = sorted(item["id"] for item in payload["profiles"])
        inline_id = tracking._profile_id({"max_hdop": 1.0})
        self.assertEqual(ids, sorted([legacy_id, inline_id]))
        line = payload["projects"][0]["lines"][0]
        self.assertEqual([m["profile_id"] for m in line["measurements"]],
                         [legacy_id, inline_id])
        self.assertEqual(payload["projects"][0]["points"][0]["measurement"]["profile_id"],
                         legacy_id)
        # The compact archive of the same rows reactivates with its profiles.
        compact = [dict(rows[0], encoding="compact-measurements-v1")]
        for row in rows[1:]:
            if row.get("record") == "vertex":
                row = ["v", row["project_id"], row["line_id"], row["coordinate"],
                       _compact(row["measurement"])]
            elif row.get("record") == "asset":
                row = ["a", row["project_id"], dict(
                    row["point"], measurement=_compact(row["point"]["measurement"]))]
            compact.append(row)
        archived = tracking_reference.parse_backup_ndjson(
            self._with_integrity(compact).splitlines(keepends=True))
        self.assertEqual(sorted(item["id"] for item in archived["profiles"]), ids)
        # A schema-4 JSON payload with profile references restores as well.
        legacy = {"kind": "survey_backup", "schema_version": 4, "profiles": [profile],
                  "projects": [{"id": "p1", "name": "Old", "description": "",
                                "created_at": 1, "lines": [], "points": [
                                    dict(self._legacy_asset(), line_id=None,
                                         projected_coordinate=None)]}],
                  "active_project_id": "p1", "active_line_id": None, "auto_distance_m": 1.0}
        legacy["projects"][0]["points"][0]["measurement"]["profile_id"] = legacy_id
        result = self.tracker.restore(copy.deepcopy(legacy))
        self.assertEqual(result["features"], 1)
        self.assertIn(legacy_id, self.tracker.profiles)
        self.assertEqual(self.tracker.backup()["projects"][0]["points"][0]["measurement"]
                         ["profile_id"], legacy_id)
        # A profile whose ID does not derive from its limits is not registered,
        # but unreferenced it does not stop the restore.
        foreign = copy.deepcopy(legacy)
        foreign["profiles"].append({"id": "profile-foreign", "version": 1,
                                    "limits": {"max_hdop": 9.0}})
        other = tracking.Tracker(os.path.join(self.tmp.name, "foreign"))
        self.assertEqual(other.restore(foreign)["features"], 1)
        self.assertNotIn("profile-foreign", other.profiles)
        # An unknown reference is still rejected.
        legacy["profiles"] = []
        with self.assertRaisesRegex(ValueError, "unknown profile"):
            tracking.Tracker(os.path.join(self.tmp.name, "unknown")).restore(legacy)

    def test_corrupted_schema4_backup_is_rejected_by_both_parsers(self):
        text = self._with_integrity(self._schema4_backup_rows())
        self.assertIn("[8.0, 50.0, 123.4]", text)
        corrupted = text.replace("[8.0, 50.0, 123.4]", "[8.0, 50.0, 123.5]", 1)
        with self.assertRaisesRegex(ValueError, "integrity"):
            tracking_reference.parse_backup_ndjson(corrupted.splitlines(keepends=True))
        self.tracker.create_project("Existing")
        before = self.tracker.backup()
        with self.assertRaisesRegex(ValueError, "integrity"):
            self.tracker.restore_ndjson(corrupted.splitlines(keepends=True))
        self.assertEqual(self.tracker.backup(), before)
        # A schema-4 backup without its integrity record is rejected as well.
        missing = text.splitlines(keepends=True)[:-1]
        with self.assertRaisesRegex(ValueError, "integrity"):
            tracking_reference.parse_backup_ndjson(missing)
        with self.assertRaisesRegex(ValueError, "integrity"):
            self.tracker.restore_ndjson(missing)
        self.assertEqual(self.tracker.backup(), before)

    def _schema5_survey_rows(self):
        """A small schema-5 survey from another store as NDJSON rows (no integrity)."""
        source = tracking.Tracker(tempfile.mkdtemp(dir=self.tmp.name) + "/source.jsonl")
        source.create_project("Exact")
        source.start_line("L", 1, fix=FIX)
        source.add_vertex(dict(FIX, lon_e8=800002000))
        source.add_asset("tap", "T", "", FIX, link_to_active_line=False)
        return list(source.iter_backup_ndjson())

    def test_schema5_backup_relabelled_as_legacy_is_refused_by_every_restore(self):
        import asyncio
        # A header tampered down from 5 to 3 must not slip past the
        # integrity check, and a legacy restore that converts no feature at all
        # must not replace the store with an empty survey.
        lines = self._schema5_survey_rows()
        header = json.loads(lines[0])
        self.assertEqual(header["schema_version"], 5)
        tampered = [json.dumps(dict(header, schema_version=3)) + "\n"] + lines[1:]
        log = self.enterContext(support.patch_module_global("tracking", "log"))
        self.tracker.create_project("Existing")
        before = self.tracker.backup()
        # The original integrity record is still attached: it must be checked.
        with self.assertRaisesRegex(ValueError, "integrity"):
            tracking_reference.parse_backup_ndjson(tampered)
        with self.assertRaisesRegex(ValueError, "integrity"):
            self.tracker.restore_ndjson(tampered)
        self.assertEqual(self.tracker.backup(), before)
        # Without the integrity record the precision tags still identify the
        # integers as current data (see _relabelled_exact): nothing is lost.
        stripped = tampered[:-1]
        relabelled = tracking.Tracker(os.path.join(self.tmp.name, "relabelled"))
        self.assertEqual(relabelled.restore_ndjson(stripped)["features"], 3)
        self.assertEqual(relabelled.backup()["projects"],
                         tracking_reference.parse_backup_ndjson(lines)["projects"])
        # Untagged, every e8 integer fails the degree rule: refused, not emptied.
        stripped = [raw.replace('"precision": "e8", ', "").replace(', "precision": "e8"', "")
                    for raw in stripped]
        self.assertNotIn('"precision"', "".join(stripped))
        with self.assertRaisesRegex(ValueError, "No coordinate .* could be converted"):
            tracking_reference.parse_backup_ndjson(stripped)
        with self.assertRaisesRegex(ValueError, "No coordinate .* could be converted"):
            self.tracker.restore_ndjson(stripped)
        self.assertEqual(self.tracker.backup(), before)
        # The JSON restore path (the browser converts NDJSON to a payload and
        # drops the integrity record) refuses it the same way.
        payload = tracking_reference.parse_backup_ndjson(lines)
        payload["schema_version"] = 3
        for project in payload["projects"]:
            for line in project["lines"]:
                for measurement in line["measurements"]:
                    measurement.pop("precision")
            for point in project["points"]:
                point["measurement"].pop("precision")
        with self.assertRaisesRegex(ValueError, "No coordinate .* could be converted"):
            self.tracker.restore(json.loads(json.dumps(payload)))
        self.assertEqual(self.tracker.backup(), before)
        with self.assertRaisesRegex(ValueError, "No coordinate .* could be converted"):
            asyncio.run(self.tracker.restore_async(json.loads(json.dumps(payload))))
        self.assertEqual(self.tracker.backup(), before)
        self.assertTrue(any(call.args[0] == "ERROR" for call in log.call_args_list))

    def test_legacy_backup_with_some_features_converted_still_restores(self):
        # Only a restore that keeps nothing at all is refused.
        rows = self._schema4_backup_rows()
        rows[3]["coordinate"] = [8.0, 95.0, 123.4]
        with support.patch_module_global("tracking", "log"):
            result = self.tracker.restore_ndjson(
                self._with_integrity(rows).splitlines(keepends=True))
        self.assertEqual(result["features"], 2)
        # A legacy backup without any feature restores as an empty survey.
        empty = [rows[0], rows[1], rows[-1]]
        result = tracking.Tracker(os.path.join(self.tmp.name, "empty")).restore_ndjson(
            self._with_integrity(empty).splitlines(keepends=True))
        self.assertEqual((result["projects"], result["features"]), (1, 0))

    def test_schema4_archive_can_be_verified_and_reactivated(self):
        self._write_schema4_archive()
        verified = self.tracker.verify_archive("archive-legacy")
        self.assertTrue(verified["valid"], verified)
        self.assertEqual(verified["point_count"], 3)
        project = self.tracker.reactivate_archive("archive-legacy")
        self.assertEqual(project["id"], "p1")
        line = project["lines"][0]
        self.assertEqual(list(line["coordinates"]), [[800000000, 5000000000, 123400],
                                                     [800002000, 5000000000, -2500]])
        self.assertEqual([m["precision"] for m in line["measurements"]],
                         ["legacy_float32"] * 2)
        point = project["points"][0]
        self.assertEqual(point["coordinate"], [800001000, 5000000000, 100000])
        self.assertEqual(point["projected_coordinate"], [800001000, 5000000000, 100000])
        self.assertEqual(point["measurement"]["precision"], "legacy_float32")
        self.assertEqual(self.tracker.backup()["schema_version"], 5)
        reloaded = tracking.Tracker(self.path)
        self.assertEqual(reloaded.backup(), self.tracker.backup())

    def test_archive_verify_applies_the_rule_of_the_archive_schema(self):
        path = self._write_schema4_archive()
        with open(path, encoding="utf-8") as source:
            rows = [json.loads(raw) for raw in source][:-1]
        # A schema-5 header over float rows is not a valid archive.
        rows[0]["schema_version"] = 5
        with open(path, "w", encoding="utf-8") as target:
            target.write(self._with_integrity(rows))
        self.assertFalse(self.tracker.verify_archive_path(path)["valid"])
        # A legacy vertex outside the V15.8.1 range fails as it did then.
        rows[0]["schema_version"] = 4
        rows[3][3] = [8.0, 95.0, 123.4]
        with open(path, "w", encoding="utf-8") as target:
            target.write(self._with_integrity(rows))
        self.assertFalse(self.tracker.verify_archive_path(path)["valid"])
        rows[3][3] = [8.0, 50.0, 123.4]
        rows[0]["schema_version"] = 1
        with open(path, "w", encoding="utf-8") as target:
            target.write(self._with_integrity(rows))
        self.assertFalse(self.tracker.verify_archive_path(path)["valid"])

    def test_v15_8_1_extrapolated_projection_costs_only_that_value(self):
        # V15.8.1 stored an unbounded extrapolated projection (120100 m altitude).
        state.app.stats["legacy_conversion_errors"] = 0
        log = self.enterContext(support.patch_module_global("tracking", "log"))
        text = self._with_integrity(self._schema4_backup_rows(120100.0))
        result = self.tracker.restore_ndjson(text.splitlines(keepends=True))
        self.assertEqual(result, {"schema_version": 5, "projects": 1, "features": 3,
                                  "legacy_values_skipped": 1})
        point = self.tracker.projects[0]["points"][0]
        self.assertIsNone(point["projected_coordinate"])
        self.assertEqual(point["coordinate"], [800001000, 5000000000, 100000])
        self.assertEqual((point["line_id"], point["chainage_m"], point["side"]), ("l1", 0.7, "left"))
        self.assertEqual(len(self.tracker.projects[0]["lines"][0]["coordinates"]), 2)
        self.assertEqual(state.app.stats["legacy_conversion_errors"], 1)
        errors = [c.args[2] for c in log.call_args_list if c.args[0] == "ERROR"]
        self.assertEqual(len(errors), 1)
        self.assertIn("projected_coordinate", errors[0])
        self.assertNotIn("120100", errors[0])
        tracking.validate_backup(self.tracker.backup())
        # The JSON payload path keeps the asset the same way.
        payload = tracking_reference.parse_backup_ndjson(text.splitlines(keepends=True))
        self.assertIsNone(payload["projects"][0]["points"][0]["projected_coordinate"])
        legacy = json.loads(text.splitlines()[0])
        legacy.update(kind="survey_backup", projects=[dict(
            json.loads(text.splitlines()[1])["project"], lines=[], points=[
                self._legacy_asset(120100.0)])], active_project_id="p1",
            active_line_id=None, auto_distance_m=1.0)
        legacy["projects"][0]["points"][0]["line_id"] = None
        legacy.pop("record")
        other = tracking.Tracker(os.path.join(self.tmp.name, "json-restore"))
        self.assertEqual(other.restore(legacy)["legacy_values_skipped"], 1)
        self.assertIsNone(other.projects[0]["points"][0]["projected_coordinate"])
        # The project archive of the same data verifies and reactivates completely.
        self.tracker.delete_project("p1")
        self._write_schema4_archive(120100.0)
        state.app.stats["legacy_conversion_errors"] = 0
        self.assertTrue(self.tracker.verify_archive("archive-legacy")["valid"])
        project = self.tracker.reactivate_archive("archive-legacy")
        self.assertEqual(len(project["lines"][0]["coordinates"]), 2)
        self.assertEqual([p["id"] for p in project["points"]], ["point-1"])
        self.assertIsNone(project["points"][0]["projected_coordinate"])
        self.assertEqual(state.app.stats["legacy_conversion_errors"], 1)

    def test_invalid_legacy_vertex_in_a_backup_skips_only_that_vertex(self):
        rows = self._schema4_backup_rows()
        rows[3]["coordinate"] = [8.0, 95.0, 123.4]
        state.app.stats["legacy_conversion_errors"] = 0
        with support.patch_module_global("tracking", "log"):
            result = self.tracker.restore_ndjson(
                self._with_integrity(rows).splitlines(keepends=True))
        self.assertEqual((result["features"], result["legacy_values_skipped"]), (2, 1))
        line = self.tracker.projects[0]["lines"][0]
        self.assertEqual(list(line["coordinates"]), [[800002000, 5000000000, -2500]])
        self.assertEqual(line["measurements"][0]["recorded_at"], 4)
        # A schema-5 backup gets no such allowance: one float rejects it.
        current = self._with_integrity(
            [dict(rows[0], schema_version=5)] + rows[1:3] + [dict(rows[4])] + rows[6:])
        with self.assertRaisesRegex(ValueError, r"coordinate\[0\]"):
            tracking.Tracker(os.path.join(self.tmp.name, "strict")).restore_ndjson(
                current.splitlines(keepends=True))

    def test_async_restore_yields_and_completes(self):
        self.tracker.create_project("Async")
        self.tracker.start_line("Line", 0, fix=FIX)
        for offset in range(20):
            self.tracker.add_vertex(dict(FIX, lon=8 + offset / 100000.0,
                                         lon_e8=800000000 + offset * 1000))
        backup = self.tracker.backup()
        restored = tracking.Tracker(os.path.join(self.tmp.name, "async-restore"))

        import asyncio
        asyncio.run(restored.restore_async(backup))

        self.assertEqual(restored.feature_count(), 21)
        self.assertEqual(restored.operation_progress["state"], "complete")
        self.assertEqual(restored.operation_progress["processed"], 21)

    def test_async_restore_defers_staging_index_updates(self):
        self.tracker.create_project("Async")
        self.tracker.start_line("Line", 0, fix=FIX)
        for offset in range(20):
            self.tracker.add_vertex(dict(FIX, lon=8 + offset / 100000.0,
                                         lon_e8=800000000 + offset * 1000))
        restored = tracking.Tracker(os.path.join(self.tmp.name, "deferred"))
        writes = []
        original = tracking.Tracker._write_index

        def counted(instance):
            if instance.path.endswith(".restore"):
                writes.append(instance.path)
            return original(instance)

        with mock.patch.object(tracking.Tracker, "_write_index", counted):
            import asyncio
            asyncio.run(restored.restore_async(self.tracker.backup()))
        self.assertEqual(len(writes), 1)

    def test_restore_replaces_stale_checkpoint_with_restored_state(self):
        self.tracker.create_project("Old")
        self.tracker._write_checkpoint()
        source = tracking.Tracker(os.path.join(self.tmp.name, "source"))
        source.create_project("New")
        source.start_line("Line", 0, fix=FIX)

        self.tracker.restore(source.backup())
        reloaded = tracking.Tracker(self.path)

        self.assertEqual(reloaded.feature_count(), 1)
        self.assertEqual(reloaded.status()["projects"][0]["name"], "New")
        self.assertFalse(os.path.exists(self.tracker.checkpoint_path + ".previous"))

    def _asset_only_source(self):
        source = tracking.Tracker(os.path.join(os.path.dirname(self.path), "assets-only"))
        source.create_project("Assets")
        source.add_asset("tap", "A", "", FIX, link_to_active_line=False)
        return source

    def _restore_asset_only(self, path):
        """Restore an asset-only backup into self.tracker by one restore path."""
        source = self._asset_only_source()
        if path == "ndjson":
            self.tracker.restore_ndjson(list(source.iter_backup_ndjson()))
        elif path == "json_async":
            import asyncio
            asyncio.run(self.tracker.restore_async(source.backup()))
        elif path == "json":
            self.tracker.restore(source.backup())
        elif path == "reactivation":
            self.tracker.restore_ndjson(list(source.iter_backup_ndjson()))
            archive = self.tracker.archive_project(self.tracker.projects[0]["id"])
            self.tracker.reactivate_archive(archive["id"])
        else:
            raise AssertionError(path)
        self.tracker.select_project(self.tracker.projects[0]["id"])

    def _record_and_reload(self):
        self.tracker.start_line("After restore", 0, fix=FIX)
        for step in (1, 2, 3):
            self.tracker.add_vertex(dict(FIX, lon_e8=800000000 + step * 1000))
        expected = [list(c) for c in self.tracker.projects[0]["lines"][0]["coordinates"]]
        self.tracker._sync_journal(close=True); self.tracker.point_store.close()
        reloaded = tracking.Tracker(self.path)
        line = reloaded.projects[0]["lines"][0]
        return expected, [list(c) for c in line["coordinates"]], reloaded

    def test_line_recorded_after_an_asset_only_restore_survives_reload(self):
        # Codex P1: the activated empty store had generation != 0 and no
        # segment, so the first append went to an unregistered segment 0000.
        for path in ("ndjson", "json_async", "json", "reactivation"):
            with self.subTest(path=path):
                os.makedirs(os.path.join(self.tmp.name, path))
                self.tracker = tracking.Tracker(os.path.join(self.tmp.name, path, "t.jsonl"))
                self.path = self.tracker.path
                self._restore_asset_only(path)
                expected, stored, reloaded = self._record_and_reload()
                self.assertEqual(len(expected), 4)
                self.assertEqual(expected[-1][0], 800003000)
                self.assertEqual(stored, expected)
                self.assertEqual(reloaded.feature_count(), 5)
                self.assertEqual(len(reloaded.projects[0]["points"]), 1)

    def test_compact_and_archive_right_after_an_asset_only_restore(self):
        # Codex P2: the activated empty manifest {G, []} was rejected by the
        # checkpoint probe, so compaction and archiving failed after a restore.
        import asyncio
        for path in ("ndjson", "json_async", "json", "reactivation"):
            for action in ("compact_async", "compact", "archive_async"):
                with self.subTest(path=path, action=action):
                    folder = os.path.join(self.tmp.name, "p2-%s-%s" % (path, action))
                    os.makedirs(folder)
                    self.tracker = tracking.Tracker(os.path.join(folder, "t.jsonl"))
                    self.path = self.tracker.path
                    self._restore_asset_only(path)
                    project_id = self.tracker.projects[0]["id"]
                    if action == "compact_async":
                        asyncio.run(self.tracker.compact_async())
                    elif action == "compact":
                        self.tracker.compact()
                    else:
                        archive = asyncio.run(self.tracker.archive_project_async(project_id))
                        self.assertEqual(self.tracker.projects, [])
                        archives = [a["id"] for a in self.tracker.archives]
                        self.assertIn(archive["id"], archives)
                        self.tracker._sync_journal(close=True); self.tracker.point_store.close()
                        reloaded = tracking.Tracker(self.path)
                        self.assertEqual(reloaded.projects, [])
                        self.assertEqual([a["id"] for a in reloaded.archives], archives)
                        reloaded.reactivate_archive(archive["id"])
                        self.assertEqual(len(reloaded.projects[0]["points"]), 1)
                        continue
                    self.tracker.select_project(project_id)
                    expected, stored, reloaded = self._record_and_reload()
                    self.assertEqual(stored, expected)
                    self.assertEqual(reloaded.feature_count(), 5)

    def test_stale_first_segment_never_precedes_new_vertices(self):
        # S1: an interrupted cleanup can leave the file a restored generation's
        # first segment will use. New vertices must not land behind its records.
        for path, stale_order in (("ndjson", 1), ("json_async", 999999), ("reactivation", 1)):
            with self.subTest(path=path, stale_order=stale_order):
                os.makedirs(os.path.join(self.tmp.name, "stale-" + path))
                self.tracker = tracking.Tracker(
                    os.path.join(self.tmp.name, "stale-" + path, "t.jsonl"))
                self.path = self.tracker.path
                self._restore_asset_only(path)
                store = self.tracker.point_store
                self.assertEqual(store.segments, [])
                stale = store._segment_path(1, store.generation)
                with open(stale, "w") as target:
                    target.write(json.dumps([3, stale_order, "line-ghost",
                                             [800000000, 5000000000, 1000], [-1]]) + "\n")
                expected, stored, reloaded = self._record_and_reload()
                self.assertEqual(stored, expected)
                self.assertEqual(len(stored), 4)
                self.assertNotIn("line-ghost", reloaded.point_store.offsets)
                self.assertEqual(reloaded.feature_count(), 5)

    def test_durable_point_store_recovers_a_batched_journal_tail(self):
        self.tracker.create_project("Power")
        self.tracker.start_line("Line", 0, fix=FIX)
        self.tracker._sync_journal(close=False)
        segment = self.tracker._segment_path()
        before = os.path.getsize(segment)
        self.tracker.add_vertex(dict(FIX, lon=8.00002, lon_e8=800002000))
        self.tracker._sync_journal(close=True)
        with open(segment, "r+b") as target:
            target.truncate(before)
        recovered = tracking.Tracker(self.path)
        self.assertEqual(recovered.feature_count(), 2)
        measurements = recovered.backup()["projects"][0]["lines"][0]["measurements"]
        self.assertIn("limits", measurements[-1]["quality"])

    def test_repeated_vertices_are_not_duplicated_in_event_journal(self):
        self.tracker.create_project("Single write")
        self.tracker.start_line("Line", 0, fix=FIX)
        self.tracker.add_vertex(dict(FIX, lon=8.00002, lon_e8=800002000))
        self.tracker._sync_journal(close=True)
        parts = []
        for _number, path in self.tracker._segment_files():
            with open(path, encoding="utf-8") as source:
                parts.append(source.read())
        journal = "".join(parts)
        self.assertNotIn('"event": "vertex_added"', journal)
        self.assertIn('"event": "profile_registered"', journal)
        self.assertEqual(tracking.Tracker(self.path).feature_count(), 2)

    def test_restore_rejects_nested_invalid_or_inconsistent_data(self):
        self.tracker.create_project("Test")
        self.tracker.start_line("Line", 0, fix=FIX)
        for mutate in (
                lambda data: data["projects"][0]["lines"][0]["coordinates"].append([8, 95]),
                lambda data: data["projects"][0]["lines"][0]["coordinates"].append(
                    [800000000, 9500000000, 123400]),
                lambda data: data["projects"][0]["lines"][0]["coordinates"].append([8.0, 50.0, 123.4]),
                lambda data: data["projects"][0]["lines"][0].update(state="invalid"),
                lambda data: data.update(active_project_id="unknown")):
            backup = json.loads(json.dumps(self.tracker.backup()))
            mutate(backup)
            with self.assertRaises(ValueError):
                tracking.Tracker(os.path.join(self.tmp.name, "target.jsonl")).restore(backup)

    def test_backup_rejects_invalid_schema5_measurement_fields(self):
        self.tracker.create_project("Fields")
        self.tracker.add_asset("tap", "T", "", FIX)
        valid = json.loads(json.dumps(self.tracker.backup()))
        tracking.validate_backup(valid)
        for key, bad in (("precision", "float64"), ("precision", None), ("geoid_sep_mm", 1.5),
                         ("geoid_sep_mm", True), ("alt_ellipsoid_mm", 1.5), ("antenna_height_mm", 1.5),
                         ("antenna_height_mm", "1800"), ("reference_frame", "x" * 81),
                         ("reference_frame_epoch", "x" * 81), ("ntrip_mount", "x" * 81), ("ntrip_mount", 5)):
            backup = json.loads(json.dumps(valid))
            backup["projects"][0]["points"][0]["measurement"][key] = bad
            with self.assertRaisesRegex(ValueError, key):
                tracking.validate_backup(backup)
        for key, good in (("precision", "legacy_float32"), ("geoid_sep_mm", None), ("alt_ellipsoid_mm", -5),
                          ("antenna_height_mm", 1800), ("reference_frame", "x" * 80),
                          ("reference_frame_epoch", None), ("ntrip_mount", "MOUNT1")):
            backup = json.loads(json.dumps(valid))
            backup["projects"][0]["points"][0]["measurement"][key] = good
            tracking.validate_backup(backup)

    def test_index_recovers_after_interrupted_replacement(self):
        self.tracker.create_project("Original")
        original = self.tracker.backup()
        index = self.path + ".index.json"
        previous = index + ".previous"
        os.rename(index, previous)
        recovered = tracking.Tracker(self.path)
        self.assertEqual(recovered.backup()["projects"], original["projects"])

    def test_torn_segment_tail_preserves_complete_events(self):
        self.tracker.create_project("Original")
        with open(self.tracker._segment_path(), "a") as segment:
            segment.write('{"schema_version":2,"event":"project_created"')
        recovered = tracking.Tracker(self.path)
        self.assertEqual(recovered.status()["projects"][0]["name"], "Original")
        self.assertTrue(os.path.exists(self.tracker._segment_path() + ".corrupt"))

    def _legacy_journal(self, events):
        """Replace the store with a journal written by schema <= 4 firmware."""
        self.tracker._sync_journal(close=True); self.tracker.point_store.close()
        self.tmp.cleanup(); os.makedirs(self.tmp.name)
        with open(self.path + ".segment-000001.jsonl", "w", encoding="utf-8") as target:
            for event in events:
                target.write(json.dumps(event) + "\n")

    @staticmethod
    def _legacy_events(schema):
        measurement = {"recorded_at": 3, "fix_quality": 4, "fix_status": "RTK_FIXED",
                       "satellites": 24, "hdop": 0.6, "station_id": "0054",
                       "source": "start", "quality": {"accepted": True, "reasons": []}}
        return [
            {"event": "project_created", "order": 1, "schema_version": schema,
             "project": {"id": "p", "name": "Legacy", "description": "",
                         "created_at": 1, "lines": [], "points": []}},
            {"event": "line_started", "order": 2, "schema_version": schema,
             "project_id": "p", "auto_distance_m": 1.0,
             "line": {"id": "l", "name": "L", "state": "recording", "created_at": 2,
                      "finished_at": None, "properties": {},
                      "coordinates": [], "measurements": []}},
            {"event": "vertex_added", "order": 3, "schema_version": schema,
             "project_id": "p", "line_id": "l", "coordinate": [8.0, 50.0, 123.4],
             "measurement": measurement}]

    def test_schema3_journal_vertex_replays_into_the_point_store_as_integers(self):
        self._legacy_journal(self._legacy_events(3))
        recovered = tracking.Tracker(self.path)
        line = recovered.projects[0]["lines"][0]
        self.assertEqual(list(line["coordinates"]), [[800000000, 5000000000, 123400]])
        self.assertEqual(line["measurements"][0]["precision"], "legacy_float32")
        self.assertEqual(line["measurements"][0]["record_order"], 3)
        store = recovered.point_store
        with open(store._segment_path(store.segments[-1]), encoding="utf-8") as source:
            rows = [json.loads(raw) for raw in source if raw.strip()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][:4], [3, 3, "l", [800000000, 5000000000, 123400]])
        backup = recovered.backup()
        tracking.validate_backup(copy.deepcopy(backup))
        recovered._sync_journal(close=True); recovered.point_store.close()
        # The journal line stays schema 3 text; replaying it again appends nothing.
        again = tracking.Tracker(self.path)
        self.assertEqual(again.backup(), backup)
        self.assertEqual(len(list(again.point_store.iter_line("l"))), 1)
        again._sync_journal(close=True); again.point_store.close()

    def test_schema4_line_started_with_embedded_legacy_vertices_is_converted(self):
        events = self._legacy_events(4)[:2]
        embedded = events[1]["line"]
        embedded["coordinates"] = [[8.0, 50.0, 100.0], [8.00002, 50.0, -2.5]]
        embedded["measurements"] = [
            dict(self._legacy_events(4)[2]["measurement"], record_order=2),
            dict(self._legacy_events(4)[2]["measurement"], record_order=3,
                 precision="legacy_float32")]
        embedded["_summary"] = {"vertex_count": 2, "length_m": 999.0}
        events[1]["order"] = 4
        self._legacy_journal(events)
        recovered = tracking.Tracker(self.path)
        line = recovered.projects[0]["lines"][0]
        self.assertEqual(list(line["coordinates"]), [[800000000, 5000000000, 100000],
                                                     [800002000, 5000000000, -2500]])
        self.assertEqual([m["precision"] for m in line["measurements"]],
                         ["legacy_float32", "legacy_float32"])
        self.assertAlmostEqual(recovered._line_summary(line)["length_m"],
                               recovered._line_length(line), places=3)
        self.assertLess(recovered._line_summary(line)["length_m"], 999.0)

    def test_upgrade_event_leaves_current_events_untouched(self):
        event = {"event": "vertex_added", "order": 9, "schema_version": 5,
                 "coordinate": [800000000, 5000000000, 123400],
                 "measurement": {"precision": "e8"}}
        before = copy.deepcopy(event)
        with mock.patch.object(tracking.geo, "legacy_coordinate_to_e8",
                               side_effect=AssertionError("converted twice")):
            self.assertIs(tracking._upgrade_event(event), event)
        self.assertEqual(event, before)
        legacy = {"event": "asset_added", "order": 9, "schema_version": 4,
                  "point": {"coordinate": [8, 50, 100], "projected_coordinate": None,
                            "measurement": {"source": "asset"}}}
        tracking._upgrade_event(legacy)
        self.assertEqual(legacy["schema_version"], 5)
        self.assertEqual(legacy["point"]["coordinate"], [800000000, 5000000000, 100000])
        self.assertIsNone(legacy["point"]["projected_coordinate"])
        self.assertEqual(legacy["point"]["measurement"]["precision"], "legacy_float32")
        converted = copy.deepcopy(legacy)
        tracking._upgrade_event(legacy)  # now schema 5: a second call is a no-op
        self.assertEqual(legacy, converted)

    def test_invalid_legacy_vertex_is_skipped_and_broken_lines_quarantined(self):
        events = self._legacy_events(4)
        corrupt = copy.deepcopy(events[2])
        corrupt["coordinate"] = [8.0, 95.0, 123.4]
        valid = copy.deepcopy(events[2])
        valid["order"] = 4
        valid["coordinate"] = [8.00002, 50.0, 123.4]
        valid["measurement"]["recorded_at"] = 4
        events[2] = corrupt
        events.append(valid)
        self._legacy_journal(events)
        segment = self.path + ".segment-000001.jsonl"
        with open(segment, "a", encoding="utf-8") as target:
            target.write('{"schema_version":4,"event":"line_paused"\n')
        state.app.stats["legacy_conversion_errors"] = 0
        with support.patch_module_global("tracking", "log") as log:
            recovered = tracking.Tracker(self.path)
        self.assertEqual(recovered.projects[0]["name"], "Legacy")
        line = recovered.projects[0]["lines"][0]
        # Only the vertex with the invalid value is left out.
        self.assertEqual(list(line["coordinates"]), [[800002000, 5000000000, 123400]])
        self.assertEqual(line["measurements"][0]["record_order"], 4)
        self.assertEqual(state.app.stats["legacy_conversion_errors"], 1)
        errors = [c.args for c in log.call_args_list if c.args[0] == "ERROR"]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0][1], "TRACK")
        self.assertIn("line l vertex 3", errors[0][2])
        self.assertNotIn("95", errors[0][2])
        # Broken JSON still takes the malformed-line path; every parseable event
        # stays in the rewritten segment as its original schema-4 text.
        self.assertTrue(os.path.exists(segment + ".corrupt"))
        with open(segment, encoding="utf-8") as source:
            kept = [json.loads(raw) for raw in source if raw.strip()]
        self.assertEqual([(e["order"], e["schema_version"]) for e in kept],
                         [(1, 4), (2, 4), (3, 4), (4, 4)])
        self.assertEqual(kept[2]["coordinate"], [8.0, 95.0, 123.4])
        recovered._sync_journal(close=True); recovered.point_store.close()
        again = tracking.Tracker(self.path)
        self.assertEqual(list(again.projects[0]["lines"][0]["coordinates"]),
                         [[800002000, 5000000000, 123400]])

    def test_legacy_asset_with_an_extrapolated_projection_is_kept_from_the_journal(self):
        events = self._legacy_events(4)[:2]
        point = {"id": "point-far", "object_type": "valve", "name": "V-far", "note": "",
                 "line_id": "l", "chainage_m": 1300.5, "lateral_offset_m": 0.25,
                 "side": "left", "coordinate": [8.0, 50.0, 100.0],
                 "projected_coordinate": [8.0, 50.0, 120100.0],
                 "measurement": dict(self._legacy_events(4)[2]["measurement"],
                                     source="asset")}
        events.append({"event": "asset_added", "order": 3, "schema_version": 4,
                       "project_id": "p", "point": point})
        self._legacy_journal(events)
        state.app.stats["legacy_conversion_errors"] = 0
        with support.patch_module_global("tracking", "log") as log:
            recovered = tracking.Tracker(self.path)
        kept = recovered.projects[0]["points"][0]
        self.assertEqual(kept["id"], "point-far")
        self.assertIsNone(kept["projected_coordinate"])
        self.assertEqual(kept["coordinate"], [800000000, 5000000000, 100000])
        self.assertEqual((kept["chainage_m"], kept["lateral_offset_m"], kept["side"]),
                         (1300.5, 0.25, "left"))
        self.assertEqual(kept["measurement"]["precision"], "legacy_float32")
        self.assertEqual(state.app.stats["legacy_conversion_errors"], 1)
        errors = [c.args[2] for c in log.call_args_list if c.args[0] == "ERROR"]
        self.assertEqual(len(errors), 1)
        self.assertIn("point point-far", errors[0])
        self.assertIn("projected_coordinate", errors[0])
        self.assertNotIn("120100", errors[0])
        self.assertFalse(os.path.exists(self.path + ".segment-000001.jsonl.corrupt"))

    def test_legacy_index_is_accepted_and_rewritten(self):
        self.tracker.create_project("Index")
        with open(self.tracker.index_path, encoding="utf-8") as source:
            index = json.load(source)
        index["schema_version"] = 4
        with open(self.tracker.index_path, "w", encoding="utf-8") as target:
            json.dump(index, target)
        older = dict(index, schema_version=5, active_segment=99)
        with open(self.tracker.index_path + ".previous", "w", encoding="utf-8") as target:
            json.dump(older, target)
        # The schema-4 index is the valid current one: the stale previous copy goes
        # instead of replacing it.
        with mock.patch.object(tracking.Tracker, "_write_index"):
            recovered = tracking.Tracker(self.path)
        self.assertEqual(recovered.projects[0]["name"], "Index")
        self.assertFalse(os.path.exists(self.tracker.index_path + ".previous"))
        with open(self.tracker.index_path, encoding="utf-8") as source:
            self.assertEqual(json.load(source), index)
        # A normal boot rewrites the stale index with the current schema.
        recovered = tracking.Tracker(self.path)
        with open(self.tracker.index_path, encoding="utf-8") as source:
            self.assertEqual(json.load(source)["schema_version"], 5)

    def test_schema_two_rotates_bounded_segments_and_replays_5000_features(self):
        measurement = self.tracker._measurement(FIX)
        measurement["source"] = "automatic"
        coordinates, measurements = [], []
        for index in range(5000):
            coordinates.append([800000000 + index * 100, 5000000000, 123400])
            item = copy.deepcopy(measurement)
            item["recorded_at"] = index + 1
            item["record_order"] = index + 1
            measurements.append(item)
        payload = {"schema_version": tracking.SCHEMA_VERSION,
                   "kind": "survey_backup", "active_project_id": "p",
                   "active_line_id": None, "auto_distance_m": 1,
                   "projects": [{"id": "p", "name": "Load", "description": "",
                       "created_at": 1, "points": [], "lines": [{"id": "l",
                           "name": "Load", "state": "finished", "created_at": 1,
                           "finished_at": 5001, "coordinates": coordinates,
                           "measurements": measurements, "properties": {}}]}]}
        self.tracker.restore(payload)
        segments = self.tracker._segment_files()
        self.assertEqual(len(segments), 1)
        self.assertGreater(len(self.tracker.point_store.segments), 1)
        self.assertTrue(all(os.stat(path).st_size <= tracking.SEGMENT_MAX_BYTES
                            for _number, path in segments))
        replayed = tracking.Tracker(self.path)
        self.assertEqual(replayed.feature_count(), 5000)
        self.assertEqual(replayed.backup()["schema_version"], 5)

    def test_oversized_segment_is_quarantined_without_blocking_boot(self):
        path = self.tracker._segment_path()
        with open(path, "w") as segment:
            segment.write("x" * (tracking.SEGMENT_HARD_LIMIT_BYTES + 1))
        recovered = tracking.Tracker(self.path)
        self.assertEqual(recovered.feature_count(), 0)
        self.assertTrue(os.path.exists(path + ".oversize"))

    def test_checkpoint_replays_only_later_events(self):
        self.tracker.create_project("Checkpoint")
        self.tracker.start_line("Tail", 0, fix=FIX)
        self.tracker.compact()
        self.tracker.add_vertex(dict(FIX, lon=8.00002, lon_e8=800002000))
        self.tracker._sync_journal(close=False)
        recovered = tracking.Tracker(self.path, autoload=False)
        applied = []
        original = recovered._apply
        recovered._apply = lambda event: (applied.append(event["event"]),
                                           original(event))[1]
        recovered.initialize()
        self.assertEqual(applied, [])
        self.assertEqual(recovered.feature_count(), 2)

    def test_vertex_index_updates_are_batched_but_journal_is_authoritative(self):
        self.tracker.create_project("Batch")
        self.tracker.start_line("Line", 0, fix=FIX)
        with open(self.tracker.index_path) as source:
            before = json.load(source)["global_sequence"]
        self.tracker.add_vertex(dict(FIX, lon=8.00002, lon_e8=800002000))
        with open(self.tracker.index_path) as source:
            self.assertEqual(json.load(source)["global_sequence"], before)
        self.assertEqual(tracking.Tracker(self.path).feature_count(), 2)

    def test_asset_index_updates_are_batched_but_journal_is_authoritative(self):
        self.tracker.create_project("Batch assets")
        with open(self.tracker.index_path) as source:
            before = json.load(source)["global_sequence"]
        self.tracker.add_asset("tap", "A-1", "", FIX)
        with open(self.tracker.index_path) as source:
            self.assertEqual(json.load(source)["global_sequence"], before)
        recovered = tracking.Tracker(self.path)
        self.assertEqual(recovered.feature_count(), 1)
        self.assertEqual(recovered.projects[0]["points"][0]["name"], "A-1")

    def test_map_delta_contains_only_changes_after_revision(self):
        project = self.tracker.create_project("Delta")
        baseline = self.tracker.map_data(project_id=project["id"])
        self.tracker.start_line("L-1", 0, fix=FIX)
        delta = self.tracker.map_data(project_id=project["id"],
                                      since_revision=baseline["revision"])
        self.assertTrue(delta["delta"])
        self.assertEqual([item["op"] for item in delta["changes"]],
                         ["line", "vertex"])
        self.assertNotIn("points", delta)

    def test_repeated_status_without_a_mutation_uses_cached_summaries(self):
        self.tracker.create_project("Cached")
        first = self.tracker.status()
        cached = self.tracker._status_cache
        second = self.tracker.status()
        self.assertIs(cached, self.tracker._status_cache)
        self.assertEqual(first["projects"], second["projects"])

    AVERAGED_FIX = dict(FIX, accuracy_observation={
        "samples": 5, "rtk_fixed_samples": 5, "accepted_fix_samples": 5,
        "horizontal_mean_deviation_m": 0.0012, "horizontal_max_deviation_m": 0.0031,
        "vertical_mean_deviation_m": 0.002, "vertical_max_deviation_m": 0.004,
        "duration_sec": 6.5}, receiver_accuracy={
        "source": "NMEA_GST", "samples": 5, "expected_samples": 5,
        "horizontal_sigma_mean_m": 0.0081, "horizontal_sigma_max_m": 0.0093,
        "vertical_sigma_mean_m": 0.0142, "vertical_sigma_max_m": 0.0161})

    def _archive_vertex_row(self, frame, epoch, mount):
        tracker = tracking.Tracker(os.path.join(self.tmp.name, "budget-%d" % len(frame)))
        tracker.create_project("Budget")
        with mock.patch.dict(state.app.stats, {"ntrip_state": "streaming",
                                               "ntrip_endpoint": "primary"}), \
                mock.patch.dict(cfg.CONFIG, {"reference_frame": frame,
                                             "reference_frame_epoch": epoch,
                                             "ntrip_mount": mount}):
            tracker.start_line("L", 1, fix=self.AVERAGED_FIX)
        rows = [row for row in tracker.iter_compact_archive(tracker.active_project_id)
                if row.startswith('["v"')]
        tracker.point_store.close()
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_compact_archive_row_stays_within_the_reserve_estimate(self):
        # Sizes as emitted: the host shim's json.dumps and MicroPython's ujson
        # both separate with ", " and ": " (no compact separators are passed).
        self.assertEqual(json.dumps({"a": 1, "b": [1, 2]}), '{"a": 1, "b": [1, 2]}')
        typical = self._archive_vertex_row("ETRS89 (synthetic frame)", "",
                                           "SYNTHETIC_MOUNT_NAME_024")
        self.assertIn('", "', typical)
        self.assertLessEqual(len(typical.encode("utf-8")), tracking.ARCHIVE_VERTEX_BYTES)
        # The longest configured texts: 80-character frame, epoch and mount.
        longest = self._archive_vertex_row("F" * 80, "E" * 80, "M" * 80)
        self.assertGreater(len(longest), len(typical))
        self.assertLessEqual(len(longest.encode("utf-8")), tracking.ARCHIVE_VERTEX_BYTES,
                             longest)

    def test_integration_contract_documents_the_time_base(self):
        path = os.path.join(os.path.dirname(__file__), "..", "docs", "integration.md")
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        for needle in ("## Timestamps", "2000-01-01", "946684800", "recorded_at_utc", "clock_unsynced", "86400"):
            self.assertIn(needle, text)
        # The example line must state what the firmware computes.
        example = re.search(r"Example: `recorded_at` `(\d+)` is `([^`]+)`", text)
        self.assertIsNotNone(example)
        self.assertEqual(tracking._utc_fields(int(example.group(1)))[0], example.group(2))
        self.assertIn("non-negative integer below 2**40", " ".join(text.split()))


class TestUndoIdentityAndRequestTags(unittest.TestCase):
    """#21c Task 1: rejection reasons, an undo that checks its candidate, RAM request tags."""

    def setUp(self):
        self.storage_patch = mock.patch.object(
            tracking.Tracker, "storage_capacity", return_value={
                "free_flash_bytes": 8_000_000,
                "required_flash_reserve_bytes": 3_000_000,
                "estimated_temporary_bytes": 16_384,
                "writable": True})
        self.storage_patch.start()
        self.tmp = tempfile.TemporaryDirectory(prefix="esp-rtk-undo-identity-")
        self.path = os.path.join(self.tmp.name, "tracking.jsonl")
        self.tracker = tracking.Tracker(self.path)

    def tearDown(self):
        self.tmp.cleanup()
        self.storage_patch.stop()

    def candidate(self):
        return self.tracker.live_status()["undo_candidate"]

    def stored_bytes(self):
        """Every file below the temporary folder, as one byte string."""
        data = b""
        for folder, _dirs, names in os.walk(self.tmp.name):
            for name in names:
                with open(os.path.join(folder, name), "rb") as handle:
                    data += handle.read()
        return data

    def test_a_rejected_measurement_names_its_reason_codes(self):
        self.tracker.create_project("Rejected")
        fix = dict(FIX, lat_e8=None, lon_e8=None)
        with self.assertRaises(tracking.MeasurementRejected) as caught:
            self.tracker._measurement(fix)
        self.assertIsInstance(caught.exception, ValueError)
        self.assertEqual(caught.exception.reasons, tuple(tracking.quality_check(fix)["reasons"]))
        self.assertIn("no_position", caught.exception.reasons)
        self.assertEqual(str(caught.exception), "Measurement quality rejected: %s." %
                         ", ".join(caught.exception.reasons))

    def test_undo_candidate_carries_its_identity(self):
        self.tracker.create_project("Identity")
        self.tracker.start_line("L", 0)
        self.tracker.add_vertex(FIX)
        first = self.candidate()
        self.tracker.add_vertex(FIX)
        second = self.candidate()
        self.assertEqual((second["kind"], second["index"]), ("vertex", 2))
        self.assertTrue(second["label"].endswith("V-2"))
        self.assertIsInstance(second["record_order"], int)
        self.assertGreater(second["record_order"], first["record_order"])
        self.assertIsNone(second["request_id"])
        self.assertEqual(second["inventory_epoch"], self.tracker.inventory_epoch)
        self.assertTrue(re.fullmatch("[0-9a-f]{8}", second["inventory_epoch"]))
        self.tracker.add_asset("tap", "A1", "", FIX)
        asset = self.candidate()
        self.assertEqual(asset["kind"], "asset")
        self.assertIsNone(asset["index"])
        self.assertEqual(asset["inventory_epoch"], second["inventory_epoch"])

    def test_inventory_epoch_is_random_per_boot_and_stays_in_ram(self):
        self.tracker._sync_journal(True)
        self.tracker.point_store.close()
        with mock.patch.object(tracking.os, "urandom",
                               side_effect=[b"\x01\x02\x03\x04", b"\xaa\xbb\xcc\xdd"]):
            first = tracking.Tracker(self.path)
            first._sync_journal(True)
            first.point_store.close()
            self.tracker = tracking.Tracker(self.path)               # a restart
        self.assertEqual(first.inventory_epoch, "01020304")
        self.assertEqual(self.tracker.inventory_epoch, "aabbccdd")
        self.tracker._sync_journal(True)
        self.tracker.point_store.close()
        self.tracker = tracking.Tracker(self.path)                   # unpatched draw
        self.tracker.create_project("RAM")
        point = self.tracker.add_asset("tap", "A1", "", FIX)
        self.assertTrue(self.tracker.tag_request("r-a1", "asset", point["id"]))
        self.tracker._sync_journal(True)
        self.assertNotIn(self.tracker.inventory_epoch.encode(), self.stored_bytes())

    def test_renew_inventory_draws_a_new_epoch_and_clears_the_tags(self):
        self.tracker.create_project("Renew")
        point = self.tracker.add_asset("tap", "A1", "", FIX)
        self.assertTrue(self.tracker.tag_request("r-a1", "asset", point["id"]))
        self.tracker.inventory_epoch = "01020304"
        with mock.patch.object(tracking.os, "urandom",
                               side_effect=[b"\x01\x02\x03\x04", b"\x05\x06\x07\x08"]):
            self.assertIsNone(self.tracker._renew_inventory())
        self.assertEqual(self.tracker.inventory_epoch, "05060708")
        self.assertEqual(self.tracker._request_tags, [])
        self.assertIsNone(self.candidate()["request_id"])

    def test_json_restore_renews_the_epoch_and_refuses_a_stale_undo(self):
        self.tracker.create_project("P")
        point = self.tracker.add_asset("tap", "A1", "", FIX)
        self.assertTrue(self.tracker.tag_request("r-a1", "asset", point["id"]))
        old = dict(self.candidate())
        self.assertEqual(old["request_id"], "r-a1")
        count = self.tracker.feature_count()
        self.tracker.restore(self.tracker.backup())
        new = self.candidate()
        self.assertEqual((new["kind"], new["feature_id"], new["index"]),
                         (old["kind"], old["feature_id"], old["index"]))
        self.assertNotEqual(new["inventory_epoch"], old["inventory_epoch"])
        self.assertIsNone(new["request_id"])
        self.assertEqual(self.tracker._request_tags, [])
        stale = dict(old, record_order=new["record_order"])
        sequence = self.tracker._event_sequence
        with self.assertRaises(tracking.UndoCandidateChanged):
            self.tracker.undo_last(stale)
        self.assertEqual(self.tracker.feature_count(), count)
        self.assertEqual(self.tracker._event_sequence, sequence)
        self.assertEqual(self.candidate(), new)
        self.tracker.undo_last(new)
        self.assertEqual(self.tracker.status()["projects"][0]["point_count"], 0)

    def test_ndjson_restore_renews_the_epoch_and_refuses_a_stale_undo(self):
        self.tracker.create_project("P")
        line = self.tracker.start_line("L", 0)
        self.tracker.add_vertex(FIX)
        self.tracker.add_vertex(FIX)
        self.assertTrue(self.tracker.tag_request("r-v2", "vertex"))
        old = dict(self.candidate())
        self.assertEqual(old["request_id"], "r-v2")
        lines = list(self.tracker.iter_backup_ndjson())
        self.tracker.restore_ndjson(iter(lines))
        new = self.candidate()
        self.assertEqual((new["kind"], new["feature_id"], new["index"]),
                         ("vertex", line["id"], 2))
        self.assertNotEqual(new["inventory_epoch"], old["inventory_epoch"])
        self.assertIsNone(new["request_id"])
        sequence = self.tracker._event_sequence
        with self.assertRaises(tracking.UndoCandidateChanged):
            self.tracker.undo_last(dict(old, record_order=new["record_order"]))
        self.assertEqual(self.tracker.live_status()["active_line"]["vertex_count"], 2)
        self.assertEqual(self.tracker._event_sequence, sequence)

    def test_archive_reactivation_renews_the_epoch_archiving_does_not(self):
        first = self.tracker.create_project("A")
        point = self.tracker.add_asset("tap", "A1", "", FIX)
        self.assertTrue(self.tracker.tag_request("r-a1", "asset", point["id"]))
        self.tracker.create_project("B")
        epoch = self.tracker.inventory_epoch
        archived = self.tracker.archive_project(first["id"])
        self.assertEqual(self.tracker.inventory_epoch, epoch)
        self.assertEqual(len(self.tracker._request_tags), 1)
        self.tracker.reactivate_archive(archived["id"])
        self.assertNotEqual(self.tracker.inventory_epoch, epoch)
        self.assertEqual(self.tracker._request_tags, [])

    def test_a_refused_restore_keeps_the_epoch_and_the_tags(self):
        self.tracker.create_project("Refused")
        point = self.tracker.add_asset("tap", "A1", "", FIX)
        self.assertTrue(self.tracker.tag_request("r-a1", "asset", point["id"]))
        epoch, tags = self.tracker.inventory_epoch, list(self.tracker._request_tags)
        with self.assertRaises((ValueError, TypeError)):
            self.tracker.restore({"schema_version": 5, "projects": "x"})
        self.assertEqual(self.tracker.inventory_epoch, epoch)
        self.assertEqual(self.tracker._request_tags, tags)
        self.assertEqual(self.candidate()["request_id"], "r-a1")

    def test_undo_without_the_epoch_is_a_change(self):
        self.tracker.create_project("Epoch")
        self.tracker.add_asset("tap", "A1", "", FIX)
        expect = dict(self.candidate())
        del expect["inventory_epoch"]
        with self.assertRaises(tracking.UndoCandidateChanged):
            self.tracker.undo_last(expect)
        expect["inventory_epoch"] = "00000000"
        with self.assertRaises(tracking.UndoCandidateChanged):
            self.tracker.undo_last(expect)
        self.assertEqual(self.tracker.status()["projects"][0]["point_count"], 1)

    def test_undo_with_a_changed_candidate_removes_nothing(self):
        self.tracker.create_project("Changed")
        self.tracker.start_line("L", 0)
        self.tracker.add_vertex(FIX)
        first = dict(self.candidate())
        self.tracker.add_vertex(FIX)
        sequence = self.tracker._event_sequence
        with self.assertRaises(tracking.UndoCandidateChanged) as caught:
            self.tracker.undo_last(first)
        self.assertEqual(str(caught.exception),
                         "The last recorded point changed. Nothing was removed.")
        self.assertEqual(self.tracker.live_status()["active_line"]["vertex_count"], 2)
        self.assertEqual(self.tracker._event_sequence, sequence)
        self.tracker.undo_last(self.candidate())
        self.assertEqual(self.tracker.live_status()["active_line"]["vertex_count"], 1)

    def test_undo_with_an_explicit_null_expectation_removes_nothing(self):
        self.tracker.create_project("Null")
        self.tracker.start_line("L", 0, fix=FIX)
        self.tracker.add_vertex(FIX)
        self.tracker.set_line_state("finish")
        self.assertIsNone(self.candidate())                   # browser A sees no candidate
        sequence = self.tracker._event_sequence
        with self.assertRaises(tracking.UndoCandidateChanged):
            self.tracker.undo_last(None)
        self.assertEqual(self.tracker._event_sequence, sequence)
        self.tracker.add_asset("tap", "B1", "", FIX)          # browser B records a point
        sequence = self.tracker._event_sequence
        with self.assertRaises(tracking.UndoCandidateChanged):
            self.tracker.undo_last(None)
        self.assertEqual(self.tracker._event_sequence, sequence)
        self.assertEqual(self.candidate()["label"], "B1")

    def test_undo_without_expectation_keeps_the_old_behaviour(self):
        self.tracker.create_project("Old")
        self.tracker.add_asset("tap", "A1", "", FIX)
        self.tracker.add_asset("tap", "B1", "", FIX)
        self.tracker.undo_last()
        self.assertEqual(self.candidate()["label"], "A1")

    def test_undo_rejects_a_malformed_expectation(self):
        self.tracker.create_project("Malformed")
        self.tracker.add_asset("tap", "A1", "", FIX)
        for expect in ("x", 3):
            with self.subTest(expect=expect):
                with self.assertRaises(ValueError) as caught:
                    self.tracker.undo_last(expect)
                self.assertNotIsInstance(caught.exception, tracking.UndoCandidateChanged)
                self.assertEqual(str(caught.exception), "The undo expectation must be an object.")
        self.assertEqual(self.tracker.status()["projects"][0]["point_count"], 1)

    def test_undo_last_stays_synchronous(self):
        for function in (tracking.Tracker.undo_last, tracking.Tracker.tag_request):
            with self.subTest(function=function.__name__):
                self.assertFalse(inspect.iscoroutinefunction(function))
                self.assertFalse(inspect.isgeneratorfunction(function))
                source = inspect.getsource(function)
                self.assertNotIn("await", source)
                self.assertNotIn("yield", source)

    def test_a_request_tag_marks_only_the_new_point(self):
        self.tracker.create_project("Tags")
        first = self.tracker.add_asset("tap", "A1", "", FIX)
        self.assertIs(self.tracker.tag_request("r-a1", "asset", first["id"]), True)
        self.assertEqual(self.candidate()["request_id"], "r-a1")
        self.tracker.add_asset("tap", "B1", "", FIX)
        self.assertEqual((self.candidate()["label"], self.candidate()["request_id"]),
                         ("B1", None))
        self.tracker.undo_last()
        self.assertEqual((self.candidate()["label"], self.candidate()["request_id"]),
                         ("A1", "r-a1"))
        tags = list(self.tracker._request_tags)
        self.assertIs(self.tracker.tag_request("r-x", "asset", "point-unknown"), False)
        self.assertEqual(self.tracker._request_tags, tags)

    def test_a_route_start_without_a_vertex_is_not_tagged(self):
        self.tracker.create_project("Route")
        self.tracker.add_asset("tap", "A1", "", FIX)
        line = self.tracker.start_line("Route", 0, fix=None, recording_mode="route")
        self.assertIs(self.tracker.tag_request("r-route", "vertex", line["id"]), False)
        self.assertEqual((self.candidate()["label"], self.candidate()["request_id"]),
                         ("A1", None))

    def test_an_added_vertex_is_tagged_on_the_active_line(self):
        self.tracker.create_project("Vertex")
        self.tracker.start_line("L", 0)
        self.tracker.add_vertex(FIX)
        self.assertIs(self.tracker.tag_request("r-v", "vertex"), True)
        self.assertEqual((self.candidate()["kind"], self.candidate()["request_id"]),
                         ("vertex", "r-v"))
        self.tracker.set_line_state("finish")
        self.assertIs(self.tracker.tag_request("r-w", "vertex"), False)

    def test_request_tags_stay_in_ram(self):
        self.tracker.create_project("RAM")
        point = self.tracker.add_asset("tap", "A1", "", FIX)
        self.assertTrue(self.tracker.tag_request("r-a1", "asset", point["id"]))
        self.tracker._sync_journal(True)
        self.tracker.point_store.close()
        self.tracker = tracking.Tracker(self.path)
        self.assertEqual(self.candidate()["feature_id"], point["id"])
        self.assertIsNone(self.candidate()["request_id"])
        self.tracker._sync_journal(True)
        self.assertNotIn(b"r-a1", self.stored_bytes())

    def test_request_tags_are_bounded(self):
        self.tracker.create_project("Bounded")
        for number in range(10):
            point = self.tracker.add_asset("tap", "P%d" % number, "", FIX)
            self.assertTrue(self.tracker.tag_request("r-%d" % number, "asset", point["id"]))
        self.assertEqual(tracking.REQUEST_TAG_LIMIT, 8)
        self.assertEqual(len(self.tracker._request_tags), tracking.REQUEST_TAG_LIMIT)
        for _ in range(7):
            self.tracker.undo_last()
        self.assertEqual((self.candidate()["label"], self.candidate()["request_id"]),
                         ("P2", "r-2"))
        self.tracker.undo_last()
        self.assertEqual((self.candidate()["label"], self.candidate()["request_id"]),
                         ("P1", None))


class TestStartupHook(unittest.IsolatedAsyncioTestCase):
    """initialize_async awaits an optional hook before the survey is loaded."""

    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="tracking-hook-")
        self.addCleanup(shutil.rmtree, self.folder, True)
        first = tracking.Tracker(os.path.join(self.folder, "survey"))
        self.project_id = first.create_project("Hook")["id"]
        first._sync_journal(close=True)
        first.point_store.close()

    def tracker(self):
        tracker = tracking.Tracker(os.path.join(self.folder, "survey"), autoload=False,
                                   point_store_autoload=False)
        self.addCleanup(tracker.point_store.close)
        self.addCleanup(tracker._sync_journal, True)
        return tracker

    async def test_the_hook_runs_on_the_loaded_survey_before_it_counts_as_loaded(self):
        tracker, seen = self.tracker(), []

        async def hook(given):
            seen.append((given is tracker, given.is_loaded(),
                         [project["id"] for project in given.projects],
                         list(given.archives)))
            await asyncio.sleep(0)
            seen.append(given.is_loaded())

        with mock.patch.object(tracker, "_load_archives",
                               side_effect=lambda: setattr(tracker, "archives", ["a"])):
            self.assertIs(await tracker.initialize_async(hook), tracker)
        self.assertEqual(seen, [(True, False, [self.project_id], ["a"]), False])
        self.assertTrue(tracker.is_loaded())

    async def test_hook_errors_are_logged_and_never_prevent_loading(self):
        for error in (OSError(5, "io"), ValueError("bad"), MemoryError()):
            with self.subTest(error=type(error).__name__):
                tracker = self.tracker()

                async def hook(given):
                    raise error

                with support.patch_module_global("tracking", "log") as log:
                    await tracker.initialize_async(hook)
                self.assertTrue(tracker.is_loaded())
                self.assertEqual(log.call_args[0][:2], ("WARN", "TRACK"))
        tracker = self.tracker()
        await tracker.initialize_async()
        self.assertTrue(tracker.is_loaded())

    async def test_the_hook_does_not_run_when_the_recovery_fails(self):
        tracker = self.tracker()
        hook = mock.AsyncMock()
        with mock.patch.object(tracker, "_load_steps", side_effect=OSError(5, "io")):
            with self.assertRaises(OSError):
                await tracker.initialize_async(hook)
        hook.assert_not_called()
        self.assertFalse(tracker.is_loaded())

        def failing_recovery():
            raise OSError(5, "io")
            yield None

        tracker = self.tracker()
        with mock.patch.object(tracker, "_recover_restore_steps", failing_recovery):
            with self.assertRaises(OSError):
                await tracker.initialize_async(hook)
        hook.assert_not_called()
        self.assertFalse(tracker.is_loaded())

    def test_check_deletable_is_the_precondition_of_delete_project(self):
        tracker = tracking.Tracker(os.path.join(self.folder, "survey"))
        self.addCleanup(tracker.point_store.close)
        self.addCleanup(tracker._sync_journal, True)
        with self.assertRaisesRegex(ValueError, "Project not found"):
            tracker.check_deletable("project-none")
        tracker.check_deletable(self.project_id)
        tracker.select_project(self.project_id)
        tracker.start_line("Line", 1, {}, None, "route")
        with self.assertRaisesRegex(ValueError, "Finish the active line"):
            tracker.check_deletable(self.project_id)
        with self.assertRaisesRegex(ValueError, "Finish the active line"):
            tracker.delete_project(self.project_id)


STAKEOUT_TID = "so-3a4739b9/1/1"
STAKEOUT_CMD = "0123456789abcdef"
STAKEOUT_ONLY = "^Stakeout points come only from a stakeout confirmation\\.$"
STAKEOUT_KEYS = ("object_type", "name", "line_id", "coordinate", "stakeout_tid", "stakeout_gen",
                 "stakeout_cmd", "stakeout_tolerance_m", "stakeout_dn_m", "stakeout_de_m",
                 "stakeout_dist_m")


def stakeout_confirmation(**fields):
    """The stakeout argument of add_asset for a valid confirmation."""
    value = {"tid": STAKEOUT_TID, "gen": 2, "cmd": STAKEOUT_CMD, "mode": "verified",
             "tolerance_m": 0.1, "dn_m": 0.012, "de_m": -0.004, "dist_m": 0.013}
    value.update(fields)
    return value


class _Sink:
    """Collects what an HTTP answer writes."""

    def __init__(self):
        self.buf = bytearray()

    def write(self, data):
        self.buf.extend(data)

    def get_extra_info(self, key):
        return ("192.168.4.2", 12345)

    async def drain(self):
        pass


class StakeoutTrackerCase(unittest.TestCase):
    """A real tracker in a temporary folder with modelled free flash (WP4 A1)."""

    def setUp(self):
        self.storage_patch = mock.patch.object(
            tracking.Tracker, "storage_capacity", return_value={
                "free_flash_bytes": 8_000_000,
                "required_flash_reserve_bytes": 3_000_000,
                "estimated_temporary_bytes": 16_384,
                "writable": True})
        self.storage_patch.start()
        self.addCleanup(self.storage_patch.stop)
        self.tmp = tempfile.TemporaryDirectory(prefix="esp-rtk-stakeout-point-")
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "tracking.jsonl")
        self.tracker = self.open_tracker(self.path)

    def open_tracker(self, path):
        tracker = tracking.Tracker(path)
        self.addCleanup(tracker.point_store.close)
        self.addCleanup(tracker._sync_journal, True)
        return tracker

    def reopen(self, tracker):
        tracker.point_store.close()
        tracker._sync_journal(close=True)
        return self.open_tracker(tracker.path)

    def add_stakeout(self, fix=FIX, **fields):
        return self.tracker.add_asset("stakeout", "Line A / P001", "", fix, False,
                                      stakeout_confirmation(**fields))

    @staticmethod
    def stakeout_fields(point):
        fields = dict((key, point.get(key)) for key in STAKEOUT_KEYS)
        fields["source"] = point["measurement"].get("source")
        fields["accepted"] = point["measurement"]["quality"]["accepted"]
        return fields

    @staticmethod
    def find_point(tracker, point_id):
        return next(point for project in tracker.projects for point in project["points"]
                    if point["id"] == point_id)


class TestStakeoutPoint(StakeoutTrackerCase):
    def test_verified_point_carries_target_generation_and_command(self):
        self.tracker.create_project("Stakeout")
        point = self.add_stakeout()
        self.assertEqual(point["object_type"], "stakeout")
        self.assertEqual(point["name"], "Line A / P001")
        self.assertEqual(point["stakeout_tid"], STAKEOUT_TID)
        self.assertEqual(point["stakeout_gen"], 2)
        self.assertEqual(point["stakeout_cmd"], STAKEOUT_CMD)
        self.assertEqual(point["stakeout_tolerance_m"], 0.1)
        self.assertEqual((point["stakeout_dn_m"], point["stakeout_de_m"], point["stakeout_dist_m"]),
                         (0.012, -0.004, 0.013))
        self.assertEqual(point["measurement"]["source"], "stakeout_verified")
        self.assertTrue(point["measurement"]["quality"]["accepted"])
        self.assertIsNone(point["line_id"])
        self.assertEqual(point["coordinate"], [FIX["lon_e8"], FIX["lat_e8"], FIX["alt_mm"]])
        self.assertEqual(self.tracker._project()["points"][-1], point)

    def test_manual_point_keeps_a_rejected_fix_honestly(self):
        self.tracker.create_project("Manual")
        rejected = dict(FIX, qual=1, fix_status_text="GPS")
        point = self.add_stakeout(rejected, mode="manual", tolerance_m=None)
        self.assertIs(point["measurement"]["quality"]["accepted"], False)
        self.assertEqual(point["measurement"]["source"], "stakeout_manual")
        self.assertIsNone(point["stakeout_tolerance_m"])
        before = (self.tracker.event_sequence, self.tracker.feature_count())
        with self.assertRaises(tracking.MeasurementRejected):
            self.add_stakeout(rejected, gen=3)
        self.assertEqual((self.tracker.event_sequence, self.tracker.feature_count()), before)

    def test_stakeout_type_needs_a_confirmation(self):
        self.tracker.create_project("Refused")
        self.tracker.add_asset("tap", "T", "", FIX, link_to_active_line=False)
        before = (self.tracker.event_sequence, self.tracker.feature_count(),
                  [point["id"] for point in self.tracker._project()["points"]])
        cases = [("stakeout", None), ("valve", stakeout_confirmation()),
                 ("stakeout", "verified"),
                 ("stakeout", stakeout_confirmation(gen=-1)),
                 ("stakeout", stakeout_confirmation(gen=True)),
                 ("stakeout", stakeout_confirmation(gen="1")),
                 ("stakeout", stakeout_confirmation(gen=tracking_progress_limit())),
                 ("stakeout", stakeout_confirmation(mode="x")),
                 ("stakeout", stakeout_confirmation(tid=7)),
                 ("stakeout", stakeout_confirmation(cmd="0123456789ABCDEF")),
                 ("stakeout", stakeout_confirmation(dist_m=-0.1))]
        for object_type, confirmation in cases:
            with self.subTest(object_type=object_type, confirmation=confirmation):
                with self.assertRaisesRegex(ValueError, STAKEOUT_ONLY):
                    self.tracker.add_asset(object_type, "P", "", FIX, False, confirmation)
                self.assertEqual((self.tracker.event_sequence, self.tracker.feature_count(),
                                  [point["id"] for point in self.tracker._project()["points"]]),
                                 before)
        # A linked stakeout point fails the field check (line_id must be None).
        self.tracker.start_line("L", 0, fix=FIX)
        sequence = self.tracker.event_sequence
        with self.assertRaisesRegex(ValueError, STAKEOUT_ONLY):
            self.tracker.add_asset("stakeout", "P", "", FIX, True, stakeout_confirmation())
        self.assertEqual(self.tracker.event_sequence, sequence)

    def test_tracking_post_refuses_a_stakeout_object_point(self):
        import measurement
        from support import web
        self.tracker.create_project("Dispatcher")
        count = self.tracker.feature_count()
        raw = _Sink()
        writer = web._HttpAnswer(raw)
        payload = {"action": "add_asset", "object_type": "stakeout", "name": "P"}

        async def run():
            try:
                await web._handle_tracking_request(
                    writer, "/api/tracking", "POST", "", json.dumps(payload).encode(), False, {})
            except Exception as error:
                await web._answer_failure(writer, error)
        with mock.patch.object(web, "tracker", self.tracker), \
                mock.patch.object(web, "_admin_allowed", return_value=True), \
                mock.patch.object(web, "log"), \
                mock.patch.object(measurement.measurement_session, "collect",
                                  mock.AsyncMock(return_value=dict(FIX))):
            asyncio.run(run())
        header, body = bytes(raw.buf).split(b"\r\n\r\n", 1)
        self.assertIn(b"400 Bad Request", header.split(b"\r\n", 1)[0])
        self.assertEqual(json.loads(body)["message"],
                         "Stakeout points come only from a stakeout confirmation.")
        self.assertEqual(self.tracker.feature_count(), count)

    def test_stakeout_fields_survive_reload_checkpoint_backup_and_archive(self):
        project = self.tracker.create_project("Durable")
        point = self.add_stakeout()
        expected = self.stakeout_fields(point)
        reloaded = self.reopen(self.tracker)
        self.assertEqual(self.stakeout_fields(self.find_point(reloaded, point["id"])), expected)
        reloaded.compact()
        reloaded = self.reopen(reloaded)
        self.assertEqual(self.stakeout_fields(self.find_point(reloaded, point["id"])), expected)
        tracking.validate_backup(reloaded.backup())
        fresh = self.open_tracker(os.path.join(self.tmp.name, "fresh.jsonl"))
        fresh.restore_ndjson(list(reloaded.iter_backup_ndjson()))
        self.assertEqual(self.stakeout_fields(self.find_point(fresh, point["id"])), expected)
        archived = reloaded.archive_project(project["id"])
        self.assertEqual(reloaded.projects, [])
        reloaded.reactivate_archive(archived["id"])
        self.assertEqual(self.stakeout_fields(self.find_point(reloaded, point["id"])), expected)


def tracking_progress_limit():
    import stakeout_progress
    return stakeout_progress.GEN_LIMIT


class TestStakeoutBackupValidation(StakeoutTrackerCase):
    def strip_integrity(self, rows):
        return [row for row in rows if json.loads(row).get("record") != "integrity"]

    def test_restore_refuses_invalid_stakeout_points(self):
        self.tracker.create_project("Backup")
        self.tracker.add_asset("tap", "T", "", FIX, link_to_active_line=False)
        point = self.add_stakeout()
        payload = self.tracker.backup()
        rows = list(self.tracker.iter_backup_ndjson())
        target = self.open_tracker(os.path.join(self.tmp.name, "target.jsonl"))
        target.restore(copy.deepcopy(payload))
        self.assertEqual(self.stakeout_fields(self.find_point(target, point["id"])),
                         self.stakeout_fields(point))
        before = (target.feature_count(),
                  [item["id"] for project in target.projects for item in project["points"]])

        def remove(key):
            return lambda item: item.pop(key)

        def assign(key, value):
            return lambda item: item.__setitem__(key, value)
        cases = [("no tid", remove("stakeout_tid")), ("gen text", assign("stakeout_gen", "0")),
                 ("gen bool", assign("stakeout_gen", True)), ("gen negative", assign("stakeout_gen", -1)),
                 ("tid leading zero", assign("stakeout_tid", "so-3a4739b9/1/01")),
                 ("no source", lambda item: item["measurement"].pop("source")),
                 ("survey source", lambda item: item["measurement"].__setitem__("source", "survey"))]
        for label, change in cases:
            with self.subTest(case=label):
                bad = copy.deepcopy(payload)
                stored = next(item for item in bad["projects"][0]["points"]
                              if item["id"] == point["id"])
                change(stored)
                with self.assertRaisesRegex(ValueError, "^A stakeout point is invalid\\.$"):
                    tracking.validate_backup(bad)
                with self.assertRaisesRegex(ValueError, "^A stakeout point is invalid\\.$"):
                    target.restore(bad)
                lines = []
                for raw in self.strip_integrity(rows):
                    value = json.loads(raw)
                    if value.get("record") == "asset" and value["point"]["id"] == point["id"]:
                        change(value["point"])
                        raw = json.dumps(value) + "\n"
                    lines.append(raw)
                text = TestTracking._with_integrity(lines)
                with self.assertRaisesRegex(ValueError, "^A stakeout point is invalid\\.$"):
                    target.restore_ndjson(text.splitlines(keepends=True))
                self.assertEqual((target.feature_count(),
                                  [item["id"] for project in target.projects
                                   for item in project["points"]]), before)


class TestUndoCandidate(StakeoutTrackerCase):
    def test_candidate_is_public(self):
        self.assertIsNone(self.tracker.undo_candidate())
        self.tracker.create_project("Undo")
        self.assertIsNone(self.tracker.undo_candidate())
        point = self.tracker.add_asset("tap", "T", "", FIX, link_to_active_line=False)
        candidate = self.tracker.undo_candidate()
        self.assertEqual((candidate["kind"], candidate["feature_id"]), ("asset", point["id"]))
        self.assertEqual(candidate, self.tracker.live_status()["undo_candidate"])
        stakeout_point = self.add_stakeout()
        self.assertEqual(self.tracker.undo_candidate()["feature_id"], stakeout_point["id"])


class TestEventSequence(StakeoutTrackerCase):
    def test_sequence_grows_only_with_an_append(self):
        self.tracker.create_project("Sequence")
        start = self.tracker.event_sequence
        self.tracker.add_asset("tap", "T", "", FIX, link_to_active_line=False)
        self.assertEqual(self.tracker.event_sequence, start + 1)
        self.add_stakeout()
        self.assertEqual(self.tracker.event_sequence, start + 2)
        with self.assertRaises(AttributeError):
            self.tracker.event_sequence = 0
        sequence = self.tracker.event_sequence
        low = {"free_flash_bytes": 100, "required_flash_reserve_bytes": 100,
               "estimated_temporary_bytes": 1, "writable": False}
        with mock.patch.object(self.tracker, "feature_limit", return_value=0):
            with self.assertRaisesRegex(ValueError, "Tracking point limit reached."):
                self.tracker.add_asset("tap", "T", "", FIX, False)
        with self.assertRaisesRegex(ValueError, "Unknown object type."):
            self.tracker.add_asset("pipe", "T", "", FIX, False)
        with self.assertRaises(tracking.MeasurementRejected):
            self.tracker.add_asset("tap", "T", "", dict(FIX, qual=1, fix_status_text="GPS"), False)
        with mock.patch.object(self.tracker, "storage_capacity", return_value=low):
            with self.assertRaises(tracking.StorageReserveError):
                self.tracker.add_asset("tap", "T", "", FIX, False)
            with self.assertRaises(tracking.StorageReserveError):
                self.add_stakeout(gen=3)
        with self.assertRaisesRegex(ValueError, STAKEOUT_ONLY):
            self.add_stakeout(gen=-1)
        self.assertEqual(self.tracker.event_sequence, sequence)
        with mock.patch.object(tracking.Tracker, "_write_journal_event",
                               side_effect=OSError(28, "no space")):
            with self.assertRaises(OSError):
                self.add_stakeout(gen=4)
        self.assertEqual(self.tracker.event_sequence, sequence + 1)


class TestRestorePublishHook(StakeoutTrackerCase):
    def setUp(self):
        super().setUp()
        self.tracker.create_project("Restore")
        self.tracker.add_asset("tap", "T", "", FIX, link_to_active_line=False)
        self.ndjson = list(self.tracker.iter_backup_ndjson())
        self.payload = self.tracker.backup()
        self.target = self.open_tracker(os.path.join(self.tmp.name, "target.jsonl"))
        self.target.create_project("Before")
        self.target.add_asset("valve", "V", "", FIX, link_to_active_line=False)
        self.calls = []
        real = self.target._write_restore_commit

        def commit(staging):
            self.calls.append("commit")
            return real(staging)
        patcher = mock.patch.object(self.target, "_write_restore_commit", side_effect=commit)
        patcher.start()
        self.addCleanup(patcher.stop)

    def survey(self, tracker):
        return (tracker.feature_count(),
                [(project["name"], [point["id"] for point in project["points"]])
                 for project in tracker.projects])

    def hook(self, fail=False):
        def steps():
            self.calls.append(("hook", self.target._restore_published()))
            yield None
            self.calls.append("step")
            if fail:
                raise OSError(5, "io")
            yield None
            self.calls.append("end")
        return steps

    def test_hook_runs_between_verification_and_publication(self):
        self.target.before_restore_publish = self.hook()
        self.target.restore_ndjson(list(self.ndjson))
        self.assertEqual(self.calls, [("hook", False), "step", "end", "commit"])
        self.assertEqual(self.survey(self.target), self.survey(self.tracker))
        self.calls[:] = []
        asyncio.run(self.target.restore_async(copy.deepcopy(self.payload)))
        self.assertEqual(self.calls, [("hook", False), "step", "end", "commit"])

    def test_a_failing_hook_publishes_nothing(self):
        before = self.survey(self.target)
        self.target.before_restore_publish = self.hook(fail=True)
        with self.assertRaises(OSError):
            self.target.restore_ndjson(list(self.ndjson))
        self.assertEqual(self.calls, [("hook", False), "step"])
        self.assertFalse(self.target._restore_published())
        self.assertEqual(self.target._restore_staging_files(), [])
        self.assertEqual(self.survey(self.target), before)
        reloaded = self.reopen(self.target)
        self.assertEqual(self.survey(reloaded), before)

    def test_an_invalid_backup_never_calls_the_hook(self):
        self.target.before_restore_publish = self.hook()
        rows = [json.loads(raw) for raw in self.ndjson]
        rows[-1]["content_sha256"] = "0" * 64
        with self.assertRaises(ValueError):
            self.target.restore_ndjson([json.dumps(row) + "\n" for row in rows])
        self.assertEqual(self.calls, [])

    def test_without_a_hook_restore_works_as_before(self):
        self.assertIsNone(self.target.before_restore_publish)
        self.target.restore_ndjson(list(self.ndjson))
        self.assertEqual(self.calls, ["commit"])
        self.assertEqual(self.survey(self.target), self.survey(self.tracker))
        self.assertIsNone(self.target.before_restore_publish)


class TestArchiveExtra(StakeoutTrackerCase):
    def archive(self, name, extra=None):
        project = self.tracker.create_project(name)
        self.tracker.add_asset("tap", "T", "", FIX, link_to_active_line=False)
        if extra is None:
            return asyncio.run(self.tracker.archive_project_async(project["id"]))
        return asyncio.run(self.tracker.archive_project_async(project["id"], extra))

    def test_archive_metadata_takes_the_stakeout_flag(self):
        flagged = self.archive("Flagged", {"stakeout_data": True})
        self.assertIs(flagged["stakeout_data"], True)
        self.assertIs(self.tracker.archives[-1]["stakeout_data"], True)
        cleared = self.archive("Cleared", {"stakeout_data": False})
        self.assertIs(self.tracker.archives[-1]["stakeout_data"], False)
        plain = self.archive("Plain")
        self.assertNotIn("stakeout_data", self.tracker.archives[-1])
        other = self.archive("Other", {"x": 1})
        self.assertNotIn("x", self.tracker.archives[-1])
        self.assertNotIn("stakeout_data", self.tracker.archives[-1])
        reloaded = self.reopen(self.tracker)
        by_id = dict((item["id"], item) for item in reloaded.archives)
        self.assertIs(by_id[flagged["id"]]["stakeout_data"], True)
        self.assertIs(by_id[cleared["id"]]["stakeout_data"], False)
        self.assertNotIn("stakeout_data", by_id[plain["id"]])
        self.assertNotIn("x", by_id[other["id"]])


if __name__ == "__main__":
    unittest.main()
