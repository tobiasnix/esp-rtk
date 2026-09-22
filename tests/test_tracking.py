# SPDX-License-Identifier: AGPL-3.0-only
"""Field-survey line and related-point recording."""
import json
import os
import tempfile
import copy
import unittest
from unittest import mock

from support import state
import gnss
import tracking
import cfg


FIX = {"lat": 50.0, "lon": 8.0, "alt": 123.4, "qual": 4,
       "fix_status_text": "RTK_FIXED", "sats": 24, "hdop": 0.6,
       "correction_age_sec": 0.5, "station_id": "0054",
       "receiver_accuracy": {"source": "NMEA_GST", "semi_major_sigma_m": 0.02,
                             "altitude_sigma_m": 0.04}}


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

    def test_records_line_and_parallel_asset(self):
        project = self.tracker.create_project("Water line")
        line = self.tracker.start_line("WL-01", 1, {"material": "PE"}, FIX)
        moved = dict(FIX, lon=8.00002)
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
        self.assertAlmostEqual(coordinate[2], 123.456)

    def test_auto_distance_suppresses_close_points(self):
        self.tracker.create_project("Test")
        self.tracker.start_line("Line", 10, fix=FIX)
        self.assertFalse(self.tracker.on_fix(dict(FIX, lon=8.000001)))
        self.assertEqual(self.tracker.status()["active_line"]["vertex_count"], 1)

    def test_automatic_queue_deduplicates_epochs_and_is_bounded(self):
        self.tracker.create_project("Test")
        self.tracker.start_line("Line", 1, fix=dict(FIX, utc="1"))
        first = dict(FIX, utc="2", lon=8.00002)
        self.assertTrue(self.tracker.enqueue_fix(first))
        self.assertFalse(self.tracker.enqueue_fix(dict(first)))
        for index in range(3, 66):
            self.assertTrue(self.tracker.enqueue_fix(
                dict(FIX, utc=str(index), lon=8.0 + index * 0.00002)))
        self.assertEqual(len(self.tracker._pending_fixes), 64)
        self.assertIsNone(self.tracker.enqueue_fix(
            dict(FIX, utc="overflow", lon=8.01)))

    def test_capacity_levels_and_automatic_limit_pause(self):
        with mock.patch.object(tracking, "MAX_FEATURES", 2):
            self.tracker.create_project("Capacity")
            self.tracker.start_line("Line", 1, fix=FIX)
            self.tracker.add_vertex(dict(FIX, lon=8.00002), source="automatic")
            capacity = self.tracker.capacity()
            self.assertEqual({key: capacity[key] for key in (
                "used", "limit", "remaining", "percent", "level")}, {
                "used": 2, "limit": 2, "remaining": 0,
                "percent": 100, "level": "full"})
            self.assertIn("required_flash_reserve_bytes", capacity)
            self.assertIn("estimated_temporary_bytes", capacity)
            with self.assertRaisesRegex(ValueError, "point limit"):
                self.tracker.add_vertex(dict(FIX, lon=8.00004),
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
        with mock.patch.object(tracking, "MAX_FEATURES", 10000), \
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
            with self.assertRaisesRegex(ValueError, "Flash reserve"):
                self.tracker.add_vertex(dict(FIX, lon=8.00002), "automatic")
        self.assertEqual(self.tracker.feature_count(), 1)
        self.assertEqual(self.tracker.live_status()["active_line"]["pause_reason"],
                         "storage_reserve")

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
                    self.tracker.add_vertex(dict(FIX, lon=8.0 + n * 0.00002))
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
            self.tracker.add_vertex(dict(FIX, lon=8.00002))
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
        restored = tracking.Tracker.parse_backup_ndjson(records)
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
        self.tracker.add_vertex(dict(FIX, lon=8.00002))
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

    def test_status_reports_server_timer_and_last_vertex(self):
        with mock.patch.object(tracking.time, "time", return_value=1_000):
            self.tracker.create_project("Timed")
            self.tracker.start_line("Line", 0)
        fix = copy.deepcopy(FIX)
        fix["receiver_accuracy"] = {"horizontal_sigma_m": .013,
                                     "altitude_sigma_m": .02}
        with mock.patch.object(tracking.time, "time", return_value=1_005):
            self.tracker.add_vertex(fix)
        with mock.patch.object(tracking.time, "time", return_value=1_012):
            status = self.tracker.status()
        self.assertEqual(status["device_time"], 1_012)
        self.assertEqual(status["active_line"]["recording_sec"], 12)
        self.assertEqual(status["active_line"]["last_point"],
                         {"label": "V-1", "age_sec": 7, "h_sigma_m": .013})

    def test_live_status_is_compact_and_keeps_incremental_length(self):
        self.tracker.create_project("Live")
        self.tracker.start_line("L-1", 0, fix=FIX)
        self.tracker.add_vertex(dict(FIX, lon=8.00002))
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
        with mock.patch.object(tracking.time, "time", return_value=100):
            project = self.tracker.create_project("Activity")
        with mock.patch.object(tracking.time, "time", return_value=120):
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
        geojson = self.tracker.geojson(project["id"])
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
        self.assertEqual(restored.geojson()["features"][0]["properties"]
                         ["recording_mode"], "route")

    def test_route_skips_fix_without_complete_coordinate(self):
        self.tracker.create_project("Route")
        self.tracker.start_line("Drive", 1, recording_mode="route")
        self.assertFalse(self.tracker.enqueue_fix(dict(FIX, alt=None, utc="1")))
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
        line = [[8.0, 50.0, 0], [8.001, 50.0, 0]]
        relation = tracking.project_to_line(line, [8.0005, 50.00001, 0])
        self.assertGreater(relation["chainage_m"], 30)
        self.assertGreater(relation["lateral_offset_m"], 1)
        self.assertEqual(relation["side"], "left")

    def test_average_records_observed_repeatability(self):
        fixes = [dict(FIX, lon=8.0), dict(FIX, lon=8.000001),
                 dict(FIX, lon=8.000002, qual=5, fix_status_text="RTK_FLOAT")]
        averaged = tracking.average_fixes(fixes)
        observed = averaged["accuracy_observation"]
        self.assertEqual(observed["samples"], 3)
        self.assertEqual(observed["rtk_fixed_samples"], 2)
        self.assertGreater(observed["horizontal_max_deviation_m"], 0)
        receiver = averaged["receiver_accuracy"]
        self.assertEqual(receiver["samples"], 3)
        self.assertEqual(receiver["horizontal_sigma_max_m"], 0.02)

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
        self.tracker.start_line("Current", 0, fix=dict(FIX, lon=8.001))
        self.tracker.add_asset("tap", "Newest", "", FIX)
        self.tracker.undo_last()
        project = self.tracker.backup()["projects"][0]
        self.assertEqual(len(project["points"]), 0)
        self.assertEqual(len(project["lines"][0]["coordinates"]), 1)

    def test_missing_altitude_is_rejected(self):
        self.tracker.create_project("Test")
        with self.assertRaisesRegex(ValueError, "altitude_required"):
            self.tracker.start_line("Line", 0, fix=dict(FIX, alt=None))

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
        self.tracker.add_vertex(dict(FIX, alt=123.5))
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
        self.tracker.add_vertex(dict(FIX, lon=8.00004, alt=124.2))
        summary = self.tracker.status()["active_project_lines"][0]
        self.assertIn("large_line_gaps", summary["warnings"])
        self.assertIn("large_vertical_steps", summary["warnings"])
        self.assertGreater(summary["max_gap_m"], 2)
        self.assertGreater(summary["max_vertical_step_m"], 0.5)
        self.assertEqual(self.tracker.line_detail(line["id"])["vertex_count"], 2)

    def test_repeated_named_control_reports_before_after_delta(self):
        self.tracker.create_project("Test")
        self.tracker.add_asset("control", "CP-01", "before", FIX)
        self.tracker.add_asset("control", "CP-01", "after",
                               dict(FIX, lon=8.000001, alt=123.45))
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
        restored_payload = tracking.Tracker.parse_backup_ndjson(records)
        restored = tracking.Tracker(os.path.join(self.tmp.name, "ndjson"))
        restored.restore(restored_payload)
        self.assertEqual(restored.feature_count(), 2)
        self.assertEqual(restored.backup()["schema_version"], 4)

    def test_ndjson_restore_stages_records_without_full_payload(self):
        self.tracker.create_project("Streaming")
        self.tracker.start_line("Line", 0, fix=FIX)
        self.tracker.add_vertex(dict(FIX, lon=8.00002))
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
        backup["schema_version"] = 1
        point = backup["projects"][0]["points"][0]
        point["projected_coordinate"] = point["projected_coordinate"][:2]

        restored = tracking.Tracker(os.path.join(self.tmp.name, "legacy"))
        restored.restore(backup)

        restored_point = restored.backup()["projects"][0]["points"][0]
        self.assertEqual(restored.backup()["schema_version"], 4)
        self.assertEqual(restored_point["projected_coordinate"][2],
                         restored_point["coordinate"][2])

    def test_async_restore_yields_and_completes(self):
        self.tracker.create_project("Async")
        self.tracker.start_line("Line", 0, fix=FIX)
        for offset in range(20):
            self.tracker.add_vertex(dict(FIX, lon=8 + offset / 100000.0))
        backup = self.tracker.backup()
        restored = tracking.Tracker(os.path.join(self.tmp.name, "async-restore"))

        import asyncio
        asyncio.run(restored.restore_async(backup, batch_size=2))

        self.assertEqual(restored.feature_count(), 21)
        self.assertEqual(restored.operation_progress["state"], "complete")
        self.assertEqual(restored.operation_progress["processed"], 21)

    def test_async_restore_defers_staging_index_updates(self):
        self.tracker.create_project("Async")
        self.tracker.start_line("Line", 0, fix=FIX)
        for offset in range(20):
            self.tracker.add_vertex(dict(FIX, lon=8 + offset / 100000.0))
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
        self.assertEqual(len(writes), 2)

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

    def test_durable_point_store_recovers_a_batched_journal_tail(self):
        self.tracker.create_project("Power")
        self.tracker.start_line("Line", 0, fix=FIX)
        self.tracker._sync_journal(close=False)
        segment = self.tracker._segment_path()
        before = os.path.getsize(segment)
        self.tracker.add_vertex(dict(FIX, lon=8.00002))
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
        self.tracker.add_vertex(dict(FIX, lon=8.00002))
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
                lambda data: data["projects"][0]["lines"][0].update(state="invalid"),
                lambda data: data.update(active_project_id="unknown")):
            backup = json.loads(json.dumps(self.tracker.backup()))
            mutate(backup)
            with self.assertRaises(ValueError):
                tracking.Tracker(os.path.join(self.tmp.name, "target.jsonl")).restore(backup)

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

    def test_schema_two_rotates_bounded_segments_and_replays_5000_features(self):
        measurement = self.tracker._measurement(FIX)
        measurement["source"] = "automatic"
        coordinates, measurements = [], []
        for index in range(5000):
            coordinates.append([8.0 + index * 0.000001, 50.0, 123.4])
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
        self.assertEqual(replayed.backup()["schema_version"], 4)

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
        self.tracker.add_vertex(dict(FIX, lon=8.00002))
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
        self.tracker.add_vertex(dict(FIX, lon=8.00002))
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


if __name__ == "__main__":
    unittest.main()
