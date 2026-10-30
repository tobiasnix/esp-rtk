# SPDX-License-Identifier: AGPL-3.0-only
"""One gate verdict per published fix version, reused by the queue and the write (#28c)."""
import asyncio
import copy
import json
import os
import tempfile
import time
import unittest
from unittest import mock

import support
from support import cfg, exact_fix, state
import measurement
import tracking
from measurement import MeasurementSession

GGA = {"lat": 50.0, "lon": 8.0, "alt": 123.4, "lat_e8": 5000000000, "lon_e8": 800000000,
       "alt_mm": 123400, "qual": 4, "fix_status_text": "RTK_FIXED", "sats": 24, "hdop": 0.6,
       "correction_age_sec": 0.5, "station_id": "0054"}
GST = {"source": "NMEA_GST", "horizontal_sigma_m": 0.01, "altitude_sigma_m": 0.02}
# 0.5 m of latitude per epoch in e8 degrees (1e-8 degree is about 1.11 mm)
STEP_E8 = 449
EPOCHS = 20
# quality_check(None)["limits"] at 08c5928: MEASUREMENT_GATE_KEYS without
# required_fix, then fix_quality.
RESULT_LIMIT_ORDER = ["min_satellites", "max_hdop", "max_correction_age_sec",
                      "max_horizontal_spread_m", "max_gst_horizontal_sigma_m",
                      "max_gst_vertical_sigma_m", "quality_window_samples", "fix_quality"]


def fresh_gate():
    return {key.replace("measurement_", ""): cfg.CONFIG[key]
            for key in tracking.MEASUREMENT_GATE_KEYS}


class FakeReceiver:
    """The receiver facade a MeasurementSession reads, without NMEA or a gate."""

    def __init__(self, last_fix):
        self.last_fix = last_fix

    def fix_is_stale(self, now_ms=None):
        return False

    def fix_age_ms(self, now_ms=None):
        return 0

    def gate_state(self, now_ms=None):
        return {"pending_gst": False}


class TestGateSingleEvaluation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="esp-rtk-gate-once-")
        self.addCleanup(self.tmp.cleanup)
        self.now = 100_000
        for patcher in (
                mock.patch.object(time, "ticks_ms", side_effect=lambda: self.now),
                mock.patch.object(tracking.time, "time", return_value=1700000000),
                support.patch_module_global("tracking", "_now_seconds", return_value=1700000000),
                support.patch_module_global("state", "log"),
                mock.patch.object(tracking.Tracker, "storage_capacity", return_value={
                    "free_flash_bytes": 8_000_000, "required_flash_reserve_bytes": 3_000_000,
                    "estimated_temporary_bytes": 16_384, "writable": True})):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.real_quality_check = tracking.quality_check

    def make_app(self):
        app = state.AppState()
        app.stats["gst_output_state"] = "receiving"
        return app

    def make_tracker(self, name, mode):
        directory = os.path.join(self.tmp.name, name)
        os.mkdir(directory)
        tracker = tracking.Tracker(os.path.join(directory, "tracking.jsonl"))
        self.addCleanup(tracker.point_store.close)
        tracker.create_project("Gate once")
        tracker.start_line("Line", 0.2, recording_mode=mode)
        return tracker

    @staticmethod
    def epoch_fix(epoch, route, **changes):
        lat_e8 = GGA["lat_e8"] + STEP_E8 * epoch
        fix = dict(GGA, utc="%06d.00" % (120000 + epoch), lat_e8=lat_e8, lat=lat_e8 / 1e8,
                   raw="$GNGGA,%06d.00*00" % (120000 + epoch))
        if route:
            fix["route_ready"] = True
        fix.update(changes)
        return fix

    @staticmethod
    def drain(tracker):
        while tracker._pending_fixes:
            tracker.add_vertex(tracker._pending_fixes.pop(0), source="automatic")

    def run_epochs(self, name, mode, listener=None):
        """GGA + GST per epoch, the queue emptied after the GST like the worker does."""
        self.now = 100_000
        app, tracker = self.make_app(), self.make_tracker(name, mode)
        if listener is not None:
            app.set_fix_listener(listener)
        route = mode == "route"
        with mock.patch.object(tracking, "tracker", tracker), \
                support.patch_module_global("tracking", "quality_check",
                                  wraps=self.real_quality_check) as counted:
            for epoch in range(EPOCHS):
                self.now += 1000
                fix = self.epoch_fix(epoch, route)
                app.update_fix(fix)
                app.attach_gst(dict(GST, utc=fix["utc"]))
                self.drain(tracker)
        return app, tracker, counted.call_count

    def stored_measurements(self, tracker):
        line = tracker._line(tracker._project())
        return [tracker._expand_measurement(value) for value in line["measurements"]]

    def assert_same_backup(self, mode, first):
        with mock.patch.object(tracking.Tracker, "_reusable_verdict", return_value=None):
            _app, second, _count = self.run_epochs(mode + "-b", mode)
        self.assertEqual(json.dumps(first.backup(), sort_keys=True),
                         json.dumps(second.backup(), sort_keys=True))

    def test_one_quality_check_per_published_fix_version_while_recording(self):
        _app, tracker, count = self.run_epochs("survey-a", "survey")
        self.assertEqual(40, count)
        self.assertEqual(EPOCHS, len(self.stored_measurements(tracker)))
        self.assert_same_backup("survey", tracker)

    def test_route_gst_enrichment_adopts_the_verdict_of_the_same_fix(self):
        _app, tracker, count = self.run_epochs("route-a", "route")
        self.assertEqual(40, count)
        stored = self.stored_measurements(tracker)
        self.assertEqual(EPOCHS, len(stored))
        for epoch, value in enumerate(stored):
            enriched = dict(self.epoch_fix(epoch, True), receiver_accuracy=dict(
                GST, utc=self.epoch_fix(epoch, True)["utc"]))
            self.assertEqual(enriched["receiver_accuracy"], value["receiver_accuracy"])
            self.assertEqual(self.real_quality_check(enriched), value["quality"], epoch)
            self.assertTrue(value["quality"]["accepted"], epoch)
        self.assert_same_backup("route", tracker)

    def test_listener_gets_one_verdict_per_fix_version(self):
        calls = []
        _app, _tracker, count = self.run_epochs(
            "listener", "survey", lambda fix, quality, *rest: calls.append(
                (dict(fix), copy.deepcopy(quality))))
        self.assertEqual(40, len(calls))
        for fix, quality in calls:
            self.assertEqual(self.real_quality_check(fix), quality)
        self.assertEqual(len(calls), count)

    def queue_one_survey_epoch(self, name, **changes):
        app, tracker = self.make_app(), self.make_tracker(name, "survey")
        with mock.patch.object(tracking, "tracker", tracker):
            self.now += 1000
            fix = self.epoch_fix(0, False, **changes)
            app.update_fix(fix)
            app.attach_gst(dict(GST, utc=fix["utc"]))
        self.assertEqual(1, len(tracker._pending_fixes))
        return tracker

    def test_limits_changed_between_queue_and_write_apply_at_write(self):
        tracker = self.queue_one_survey_epoch("tighter-satellites")
        with mock.patch.dict(cfg.CONFIG, {"measurement_min_satellites": 12}):
            self.drain(tracker)
        measurements = tracker._line(tracker._project())["measurements"]
        self.assertEqual(1, len(measurements))
        profile = tracker.profiles[measurements[0]["profile_id"]]
        self.assertEqual(12, profile["limits"]["min_satellites"])
        # Second case: the tighter limit rejects the queued point at the write.
        self.now += 10_000
        tracker = self.queue_one_survey_epoch("tighter-hdop", hdop=0.6)
        with mock.patch.dict(cfg.CONFIG, {"measurement_max_hdop": 0.5}):
            with self.assertRaisesRegex(
                    ValueError, r"^Measurement quality rejected: hdop_too_high\.$"):
                tracker.add_vertex(tracker._pending_fixes.pop(0), source="automatic")
        self.assertEqual(0, len(tracker._line(tracker._project())["measurements"]))
        self.assertEqual(0, tracker.feature_count())

    def test_repeated_epoch_in_the_route_queue_is_judged_again(self):
        app, tracker = self.make_app(), self.make_tracker("repeat", "route")
        with mock.patch.object(tracking, "tracker", tracker):
            self.now += 1000
            first = self.epoch_fix(0, True)
            app.update_fix(first)
            self.assertEqual(1, len(tracker._pending_fixes))
            self.assertIn("_gst_wait_started_ms", tracker._pending_fixes[0])
            seq = app.fix_state()["fix_seq"]
            self.now += 1000
            app.update_fix(dict(first, qual=1, fix_status_text="GPS",
                                lat_e8=first["lat_e8"] + 3 * STEP_E8))
            self.assertEqual(seq + 1, app.fix_state()["fix_seq"])
            self.assertEqual(1, app.last_fix["qual"])
            self.assertTrue(app.attach_gst(dict(GST, utc=first["utc"])))
            queued = tracker._pending_fixes[0]
            self.assertEqual(1, len(tracker._pending_fixes))
            self.assertEqual(4, queued["qual"])
            self.assertIn("receiver_accuracy", queued)
            self.assertNotIn("_verdict", queued)
            self.drain(tracker)
        stored = self.stored_measurements(tracker)
        self.assertEqual(1, len(stored))
        self.assertIs(True, stored[0]["quality"]["accepted"])
        self.assertEqual(4, stored[0]["fix_quality"])

    def test_measurement_gate_cache_follows_every_config_write(self):
        self.assertEqual(fresh_gate(), tracking.measurement_gate())
        generation = tracking._gate_current()[0]
        self.assertEqual(generation, tracking._gate_current()[0])
        with mock.patch.dict(cfg.CONFIG, {"measurement_max_hdop": 1.7}):
            self.assertEqual(fresh_gate(), tracking.measurement_gate())
            self.assertEqual(1.7, tracking.measurement_gate()["max_hdop"])
        self.assertEqual(fresh_gate(), tracking.measurement_gate())
        with mock.patch.dict(cfg.CONFIG):
            generation = tracking._gate_current()[0]
            value = cfg.CONFIG["measurement_max_hdop"]
            cfg.CONFIG["measurement_max_hdop"] = float(str(value))
            self.assertIsNot(value, cfg.CONFIG["measurement_max_hdop"])
            self.assertEqual(fresh_gate(), tracking.measurement_gate())
            self.assertEqual(generation + 1, tracking._gate_current()[0])
            cfg.CONFIG["measurement_max_hdop"] = 2.0
            self.assertIs(float, type(tracking.measurement_gate()["max_hdop"]))
            cfg.CONFIG["measurement_max_hdop"] = 2
            self.assertIs(int, type(tracking.measurement_gate()["max_hdop"]))
            self.assertIs(int, type(tracking.quality_check(None)["limits"]["max_hdop"]))
            self.assertEqual(fresh_gate(), tracking.measurement_gate())
        self.assertEqual(fresh_gate(), tracking.measurement_gate())
        returned = tracking.measurement_gate()
        returned["max_hdop"] = 99
        returned["extra"] = True
        del returned["required_fix"]
        self.assertEqual(fresh_gate(), tracking.measurement_gate())
        self.assertEqual(RESULT_LIMIT_ORDER, list(tracking.quality_check(None)["limits"]))

    def test_quality_results_share_no_state(self):
        fix = dict(self.epoch_fix(0, False), receiver_accuracy=dict(GST))
        first, second = tracking.quality_check(fix), tracking.quality_check(fix)
        expected = copy.deepcopy(second)
        first["limits"]["max_hdop"] = 99
        first["reasons"].append("x")
        first["limits"].pop("fix_quality")
        self.assertEqual(expected, second)
        self.assertEqual(expected, tracking.quality_check(fix))
        app, tracker, _count = self.run_epochs("shared", "survey")
        self.assertEqual(EPOCHS, len(self.stored_measurements(tracker)))
        limits = app.gate_state()["quality"]["limits"]
        self.assertEqual(RESULT_LIMIT_ORDER, list(limits))
        self.assertEqual(tracking.quality_check(None)["limits"], limits)

    def test_sampling_judges_each_new_fix_once(self):
        fix = exact_fix(dict(GGA, utc="120000.00", receiver_accuracy=dict(GST)))
        receiver = FakeReceiver(dict(fix))
        session = MeasurementSession(receiver)
        clock = {"now": 100_000, "sleeps": 0}
        real_sleep = asyncio.sleep

        async def sleep_ms(ms):
            clock["now"] += ms
            clock["sleeps"] += 1
            if clock["sleeps"] == 4:      # four polls saw the first fix
                receiver.last_fix = exact_fix(dict(fix, utc="120001.00",
                                                   lat=50.0 + 0.000001))
            await real_sleep(0)

        self.now = clock["now"]
        with mock.patch.object(time, "ticks_ms", side_effect=lambda: clock["now"]), \
                mock.patch.object(measurement.asyncio, "sleep_ms", sleep_ms), \
                mock.patch.object(measurement, "quality_check",
                                  wraps=measurement.quality_check) as counted:
            result = asyncio.run(session.collect(2))
        self.assertEqual(2, result["accuracy_observation"]["samples"])
        self.assertEqual(4, clock["sleeps"])
        self.assertEqual(2, counted.call_count)


if __name__ == "__main__":
    unittest.main()
