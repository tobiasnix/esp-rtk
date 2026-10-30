# SPDX-License-Identifier: AGPL-3.0-only
"""The gate verdict and streak tolerate the receiver's GGA-before-GST ordering."""
import time
import unittest
from unittest import mock

from support import state, cfg
import tracking

GGA = {"lat": 50.0, "lon": 8.0, "alt": 123.4, "lat_e8": 5000000000, "lon_e8": 800000000,
       "alt_mm": 123400, "qual": 4, "fix_status_text": "RTK_FIXED", "sats": 24, "hdop": 0.6,
       "correction_age_sec": 0.5, "station_id": "0054"}
GST = {"source": "NMEA_GST", "horizontal_sigma_m": 0.01, "altitude_sigma_m": 0.02}


class TestGateConsistency(unittest.TestCase):
    def setUp(self):
        self.app = state.AppState()
        self.app.stats["gst_output_state"] = "receiving"
        self.now = 100_000
        patcher = mock.patch.object(time, "ticks_ms", side_effect=lambda: self.now)
        patcher.start(); self.addCleanup(patcher.stop)
        tracker = mock.patch("tracking.tracker"); self.tracker = tracker.start(); self.addCleanup(tracker.stop)
        self.tracker.enqueue_fix.return_value = False

    def gga(self, utc, at_ms, **changes):
        self.now = 100_000 + at_ms
        self.app.update_fix(dict(GGA, utc=utc, **changes))

    def gst(self, utc, at_ms):
        self.now = 100_000 + at_ms
        return self.app.attach_gst(dict(GST, utc=utc))

    def assertConsistent(self):
        # The invariant holds from the first published fix on; before that gate_accepted is None.
        if self.app.last_fix is not None:
            self.assertEqual(self.app.quality_streaks()["gate_accepted"], self.app.gate_state()["accepted"])

    def test_gga_then_gst_at_one_hertz_counts_a_continuous_streak(self):
        for second in range(3):
            self.gga("12000%d.00" % second, second * 1000)
            self.assertConsistent()
            self.assertTrue(self.gst("12000%d.00" % second, second * 1000 + 100))
            self.assertConsistent()
            self.assertTrue(self.app.gate_state()["accepted"])
        self.assertEqual(self.app.quality_streaks()["gate_streak_sec"], 2)
        self.assertEqual(self.app.last_fix["utc"], "120002.00")      # every epoch was admitted
        self.assertEqual(self.app.gate_state()["quality"]["reasons"], [])

    def test_bare_gga_during_a_hold_keeps_the_previous_verdict(self):
        self.gga("120000.00", 0); self.gst("120000.00", 100)
        self.gga("120001.00", 1000)
        self.assertTrue(self.app.gate_state()["accepted"])             # held, not reset
        self.assertTrue(self.app.quality_streaks()["gate_accepted"])
        self.assertConsistent()

    def test_gst_then_gga_behaves_as_today(self):
        self.now = 100_000
        self.app.update_fix(dict(GGA, utc="120000.00", receiver_accuracy=dict(GST)))
        self.assertTrue(self.app.gate_state()["accepted"])
        self.assertConsistent()

    def test_hold_expires_into_gst_required(self):
        self.gga("120000.00", 0); self.gst("120000.00", 100)
        self.gga("120001.00", 1000)                                   # no GST follows
        self.now = 100_000 + 2999
        self.assertTrue(self.app.gate_state()["accepted"])
        self.now = 100_000 + 3000
        self.assertFalse(self.app.gate_state()["accepted"])
        self.assertEqual(self.app.gate_state()["quality"]["reasons"], ["gst_required"])
        self.assertFalse(self.app.quality_streaks()["gate_accepted"])
        self.assertConsistent()

    def test_quality_drop_is_not_held(self):
        self.gga("120000.00", 0); self.gst("120000.00", 100)
        self.gga("120001.00", 1000, qual=1, fix_status_text="GPS")
        self.assertFalse(self.app.gate_state()["accepted"])
        self.assertIn("rtk_fixed_required", self.app.gate_state()["quality"]["reasons"])
        self.assertConsistent()

    def test_no_hold_when_the_receiver_is_not_sending_gst(self):
        self.app.stats["gst_output_state"] = "requested"
        self.gga("120000.00", 0)
        self.assertFalse(self.app.gate_state()["accepted"])
        self.assertEqual(self.app.gate_state()["quality"]["reasons"], ["gst_required"])

    def test_gst_for_an_unknown_epoch_is_ignored(self):
        self.gga("120000.00", 0)
        self.assertFalse(self.gst("119999.00", 50))
        self.assertNotIn("receiver_accuracy", self.app.last_fix)

    def test_gst_returns_after_a_gap(self):
        self.gga("120000.00", 0); self.gst("120000.00", 100)
        self.gga("120001.00", 1000); self.gga("120002.00", 2000); self.gga("120003.00", 3000)
        self.assertFalse(self.app.gate_state()["accepted"])          # hold expired at 3000
        self.gga("120004.00", 4000); self.gst("120004.00", 4100)
        self.assertTrue(self.app.gate_state()["accepted"])
        self.assertEqual(self.app.quality_streaks()["gate_streak_sec"], 0)

    def test_late_gst_after_the_grace_settles_the_expiry_first(self):
        self.gga("120000.00", 0); self.gst("120000.00", 100)
        self.gga("120001.00", 1000)                                   # hold starts
        self.assertTrue(self.gst("120001.00", 3100))                  # no read in between
        self.assertTrue(self.app.gate_state()["accepted"])
        self.assertEqual(self.app.quality_streaks()["gate_streak_sec"], 0)   # streak restarted at 3000

    def test_first_bare_gga_is_evaluated_for_real(self):
        self.gga("120000.00", 0)
        self.assertFalse(self.app.gate_state()["accepted"])
        self.assertFalse(self.app.gate_state()["pending_gst"])
        self.assertEqual(self.app.gate_state()["quality"]["reasons"], ["gst_required"])
        self.assertConsistent()

    def test_pending_gst_is_visible_and_stale_overrides_the_cache(self):
        self.gga("120000.00", 0); self.gst("120000.00", 100)
        self.gga("120001.00", 1000)
        self.assertTrue(self.app.gate_state()["pending_gst"])
        self.now = 100_000 + 1000 + cfg.CONFIG["fix_stale_after_ms"]
        self.assertFalse(self.app.gate_state()["accepted"])          # stale wins over the cache
        self.assertFalse(self.app.gate_state()["pending_gst"])
        self.assertIn("no_position", self.app.gate_state()["quality"]["reasons"])
        self.assertConsistent()

    def test_reevaluate_follows_changed_limits(self):
        self.gga("120000.00", 0); self.gst("120000.00", 100)
        with mock.patch.dict(cfg.CONFIG, {"measurement_min_satellites": 30}):
            self.app.reevaluate_gate()
            self.assertFalse(self.app.gate_state()["accepted"])
            self.assertIn("too_few_satellites", self.app.gate_state()["quality"]["reasons"])
        self.app.reevaluate_gate()
        self.assertTrue(self.app.gate_state()["accepted"])

    def test_defaults_accept_fresh_fixed_and_reject_old_corrections(self):
        self.gga("120000.00", 0); self.gst("120000.00", 100)
        self.assertTrue(self.app.gate_state()["accepted"])
        stale = dict(self.app.last_fix, correction_age_sec=30.0,
                     receiver_accuracy=dict(GST, horizontal_sigma_m=0.8, altitude_sigma_m=1.3))
        reasons = tracking.quality_check(stale)["reasons"]
        self.assertIn("corrections_too_old", reasons)
        self.assertIn("gst_horizontal_error_too_high", reasons)

    def test_no_hold_once_the_receiver_stops_sending_gst(self):
        self.gga("120000.00", 0); self.gst("120000.00", 100)
        self.app.stats["gst_output_state"] = "requested"
        self.gga("120001.00", 1000)
        self.assertFalse(self.app.gate_state()["accepted"])
        self.assertFalse(self.app.gate_state()["pending_gst"])
        self.assertConsistent()

    def test_expiry_first_seen_by_the_streak_is_stamped_at_the_deadline(self):
        self.gga("120000.00", 0); self.gst("120000.00", 100)
        self.gga("120001.00", 1000); self.gga("120002.00", 2000)    # hold from 1000
        self.now = 100_000 + 4500
        streaks = self.app.quality_streaks()                          # the first read
        self.assertFalse(streaks["gate_accepted"])
        self.assertEqual(streaks["gate_streak_sec"], 1)
        self.assertEqual(self.app._gate_changed_ms, 103_000)

    def test_accepted_gga_after_the_grace_settles_the_expiry_first(self):
        self.gga("120000.00", 0); self.gst("120000.00", 100)
        self.gga("120001.00", 1000)                                   # hold from 1000
        self.gga("120003.00", 3500, receiver_accuracy=dict(GST))      # no read in between
        self.now = 100_000 + 4600
        self.assertTrue(self.app.gate_state()["accepted"])
        self.assertEqual(self.app.quality_streaks()["gate_streak_sec"], 1)

    def test_first_bare_gga_after_a_stale_gap_is_evaluated_for_real(self):
        self.gga("120000.00", 0); self.gst("120000.00", 100)
        self.gga("120005.00", 5000)                                   # previous fix was stale
        self.assertFalse(self.app.gate_state()["accepted"])
        self.assertFalse(self.app.gate_state()["pending_gst"])
        self.assertEqual(self.app.gate_state()["quality"]["reasons"], ["gst_required"])
        self.assertConsistent()

    # Fix wave: held accuracy, limit changes during a hold, no hold in a GST dropout.
    def test_held_accuracy_is_reported_only_while_the_hold_is_active(self):
        self.gga("120000.00", 0); self.gst("120000.00", 100)
        self.assertIsNone(self.app.gate_state()["held_receiver_accuracy"])
        self.gga("120001.00", 1000)                                   # hold starts
        held = self.app.gate_state()["held_receiver_accuracy"]
        self.assertEqual((held["horizontal_sigma_m"], held["altitude_sigma_m"]), (0.01, 0.02))
        held["horizontal_sigma_m"] = 9.9                              # a copy, not the cache
        self.assertEqual(self.app.gate_state()["held_receiver_accuracy"]["horizontal_sigma_m"], 0.01)
        self.gst("120001.00", 1100)
        self.assertIsNone(self.app.gate_state()["held_receiver_accuracy"])

    def test_held_accuracy_is_absent_when_the_verdict_fix_had_none(self):
        self.gga("120000.00", 0)
        self.assertIsNone(self.app._gate_receiver_accuracy)
        self.assertIsNone(self.app.gate_state()["held_receiver_accuracy"])

    def test_held_accuracy_is_dropped_by_stale_and_by_the_hold_expiry(self):
        self.gga("120000.00", 0); self.gst("120000.00", 100)
        self.gga("120001.00", 1000)
        self.now = 100_000 + 1000 + cfg.CONFIG["fix_stale_after_ms"]
        self.assertIsNone(self.app.gate_state()["held_receiver_accuracy"])
        self.gga("120005.00", 5000)                                   # stale reset, then a bare fix
        self.assertIsNone(self.app._gate_receiver_accuracy)
        self.assertIsNone(self.app.gate_state()["held_receiver_accuracy"])
        self.gga("120006.00", 6000); self.gst("120006.00", 6100)
        self.gga("120007.00", 7000)
        self.now = 100_000 + 9000                                     # hold expired at 9000
        self.assertIsNone(self.app.gate_state()["held_receiver_accuracy"])
        self.assertIsNone(self.app._gate_receiver_accuracy)

    def test_changing_limits_during_a_hold_keeps_the_streak(self):
        for second in range(10):
            self.gga("1200%02d.00" % second, second * 1000)
            self.gst("1200%02d.00" % second, second * 1000 + 100)
        self.gga("120010.00", 10_000)                                 # bare, hold starts
        self.now = 100_000 + 10_050
        with mock.patch.dict(cfg.CONFIG, {"measurement_max_hdop": 2.0}):
            self.app.reevaluate_gate()
            self.assertTrue(self.app.gate_state()["accepted"])        # no flash to gst_required
            self.assertTrue(self.app.gate_state()["pending_gst"])
            self.assertConsistent()
            self.gst("120010.00", 10_100)
            self.assertTrue(self.app.gate_state()["accepted"])
            self.assertEqual(self.app.gate_state()["quality"]["limits"]["max_hdop"], 2.0)
            self.assertEqual(self.app.quality_streaks()["gate_streak_sec"], 10)
            self.assertConsistent()

    def test_changing_limits_during_a_hold_applies_other_failures_at_once(self):
        self.gga("120000.00", 0); self.gst("120000.00", 100)
        self.gga("120001.00", 1000)
        self.now = 100_000 + 1050
        with mock.patch.dict(cfg.CONFIG, {"measurement_min_satellites": 30}):
            self.app.reevaluate_gate()
            self.assertFalse(self.app.gate_state()["accepted"])
            self.assertFalse(self.app.gate_state()["pending_gst"])
            self.assertIn("too_few_satellites", self.app.gate_state()["quality"]["reasons"])
            self.assertConsistent()

    def test_changing_limits_after_the_hold_expired_applies_at_once(self):
        self.gga("120000.00", 0); self.gst("120000.00", 100)
        self.gga("120001.00", 1000)
        self.now = 100_000 + 3500                                     # expired at 3000, unread
        self.app.reevaluate_gate()
        self.assertFalse(self.app.gate_state()["accepted"])
        self.assertFalse(self.app.gate_state()["pending_gst"])
        self.assertEqual(self.app._gate_changed_ms, 103_000)
        self.assertConsistent()

    def test_no_new_hold_during_a_gst_dropout(self):
        self.gga("120000.00", 0); self.gst("120000.00", 100)
        self.gga("120001.00", 1000); self.gga("120002.00", 2000)
        self.gga("120003.00", 3000)                                   # hold expired at 3000
        self.assertEqual(self.app.gate_state()["quality"]["reasons"], ["gst_required"])
        self.assertFalse(self.app.gate_state()["pending_gst"])
        self.gga("120004.00", 4000)
        self.assertFalse(self.app.gate_state()["pending_gst"])
        self.assertFalse(self.app.gate_state()["accepted"])
        self.assertConsistent()
        self.gst("120004.00", 4100)                                   # GST is back
        self.assertTrue(self.app.gate_state()["accepted"])
        self.assertEqual(self.app.quality_streaks()["gate_streak_sec"], 0)
