# SPDX-License-Identifier: AGPL-3.0-only
"""Time spent at each fix quality. Open point from the original plan (3.5). The log only reports change; the question "was I ever on 4?" could only be answered by continuous observation.
"""
import unittest
from unittest import mock

from support import cfg, state


class TestFixQualitySeconds(unittest.TestCase):
    def setUp(self):
        self.app = state.AppState()

    def fix(self, qual):
        return {"qual": qual, "sats": 10, "hdop": 1.0,
                "fix_status_text": cfg.FIX_STATUS.get(qual, "UNKNOWN"),
                "lat": 1.0, "lon": 2.0, "alt": 100.0, "raw": "$GNGGA,x*00"}

    def test_begins_empty(self):
        self.assertEqual(self.app.stats["fix_quality_seconds"], {})

    def test_counts_the_first_quality(self):
        self.app.track_fix_quality(1, 10_000)
        self.app.track_fix_quality(1, 15_000)
        self.assertEqual(self.app.stats["fix_quality_seconds"], {"1": 5})

    def test_sums_up_over_changes(self):
        self.app.track_fix_quality(1, 0)
        self.app.track_fix_quality(5, 10_000)     # 10 s to 1
        self.app.track_fix_quality(4, 13_000)     # 3 seconds to 5
        self.app.track_fix_quality(4, 20_000)     # 7 seconds to 4
        self.assertEqual(self.app.stats["fix_quality_seconds"],
                         {"1": 10, "5": 3, "4": 7})

    def test_return_to_a_quality_sums_up(self):
        self.app.track_fix_quality(5, 0)
        self.app.track_fix_quality(4, 10_000)
        self.app.track_fix_quality(5, 12_000)
        self.app.track_fix_quality(5, 20_000)
        self.assertEqual(self.app.stats["fix_quality_seconds"]["5"], 18)

    def test_no_fix_is_counted(self):
        # Also "how long I had nothing" is an answer.
        self.app.track_fix_quality(0, 0)
        self.app.track_fix_quality(1, 30_000)
        self.assertEqual(self.app.stats["fix_quality_seconds"]["0"], 30)

    def test_update_fix_counts(self):
        self.app.update_fix(self.fix(5))
        self.assertIn("5", self.app.stats["fix_quality_seconds"])

    def test_overflow_of_the_clock_does_not_produce_negative_times(self):
        # ticks_ms runs over to MicroPython; ticks_diff does this, but a jump must not
        # break anything.
        self.app.track_fix_quality(1, 0)
        self.app.track_fix_quality(1, -5_000)
        for value in self.app.stats["fix_quality_seconds"].values():
            self.assertGreaterEqual(value, 0)

    def test_quality_streak_tracks_changes_and_last_fixed(self):
        self.app.track_fix_quality(5, 1_000)
        self.assertEqual(self.app.quality_streaks(6_000), {
            "fix_quality": 5, "streak_sec": 5, "gate_accepted": None,
            "gate_streak_sec": None, "since_rtk_fixed_sec": None})
        self.app.track_fix_quality(4, 7_000)
        streak = self.app.quality_streaks(10_000)
        self.assertEqual(streak["fix_quality"], 4)
        self.assertEqual(streak["streak_sec"], 3)
        self.assertEqual(streak["since_rtk_fixed_sec"], 3)
        self.app.track_fix_quality(5, 11_000)
        self.assertEqual(self.app.quality_streaks(15_000)["since_rtk_fixed_sec"], 8)

    def test_gate_streak_changes_only_with_gate_state(self):
        with mock.patch.object(state.time, "ticks_ms", return_value=2_000):
            self.app.update_fix(self.fix(5))
            first = self.app.quality_streaks(3_000)
            self.assertFalse(first["gate_accepted"])
            self.assertEqual(first["gate_streak_sec"], 1)
            fixed = self.fix(4)
            fixed["receiver_accuracy"] = {"horizontal_sigma_m": .01,
                                            "altitude_sigma_m": .02}
            fixed["correction_age_sec"] = .2
        with mock.patch.object(state.time, "ticks_ms", return_value=4_000):
            self.app.update_fix(fixed)
            second = self.app.quality_streaks(5_000)
            self.assertTrue(second["gate_accepted"])
            self.assertEqual(second["gate_streak_sec"], 1)


if __name__ == "__main__":
    unittest.main()
