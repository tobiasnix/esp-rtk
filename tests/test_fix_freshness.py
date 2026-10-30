# SPDX-License-Identifier: AGPL-3.0-only
"""Age, sequence and expiry of the last receiver fix."""
import time
import unittest
from unittest import mock

from support import state, cfg
import tracking

EPOCH_2000 = 946_684_800
JUMP_26Y_S = 820_540_800  # 9497 days
JUMP_26Y_MS = JUMP_26Y_S * 1000

GGA = {"utc": "120000.00", "lat": 50.0, "lon": 8.0, "alt": 100.0, "qual": 4, "sats": 20,
       "hdop": 0.6, "fix_status_text": "RTK_FIXED", "lat_e8": 5000000000, "lon_e8": 800000000,
       "alt_mm": 100000}


class TestFixFreshness(unittest.TestCase):
    def setUp(self):
        self.app = state.AppState()
        self.now = 10_000
        self.ticks = mock.patch.object(time, "ticks_ms", side_effect=lambda: self.now)
        self.ticks.start()
        self.addCleanup(self.ticks.stop)

    def publish(self, fix, advance_ms):
        # The tracker's enqueue_fix is exercised elsewhere; keep it out of the picture here.
        self.now += advance_ms
        with mock.patch("tracking.tracker") as tracker:
            tracker.enqueue_fix.return_value = False
            self.app.update_fix(dict(fix))

    def test_update_fix_never_reads_the_wall_clock(self):
        with mock.patch.object(state.time, "time", side_effect=AssertionError("wall clock")):
            self.publish(GGA, 1000)
            self.publish(GGA, 1000)
        self.assertFalse(hasattr(self.app, "last_fix_time"))
        self.assertNotIn("_last_fix_time", vars(self.app))

    def test_rtc_jump_of_26_years_between_two_fixes_keeps_the_age(self):
        with mock.patch.object(state.time, "time", return_value=EPOCH_2000):
            self.publish(GGA, 1000)
        with mock.patch.object(state.time, "time", return_value=EPOCH_2000 + JUMP_26Y_S):
            self.publish(GGA, 1000)
            self.now += 1500
            self.assertEqual(self.app.fix_age_ms(), 1500)
            self.assertFalse(self.app.fix_is_stale())

    def test_no_fix_means_no_age_and_not_stale(self):
        self.assertIsNone(self.app.fix_age_ms())
        self.assertFalse(self.app.fix_is_stale())
        self.assertEqual(self.app.fix_state(), {"fix_age_ms": None, "fix_seq": 0, "fix_rev": 0,
                                                "stale": False})

    def test_published_fix_is_fresh_then_expires_at_three_seconds(self):
        self.publish(GGA, 1000)
        self.assertEqual(self.app.fix_age_ms(), 0)
        self.assertEqual(self.app.fix_state()["fix_seq"], 1)
        self.now += 2999
        self.assertFalse(self.app.fix_is_stale())
        self.now += 1
        self.assertTrue(self.app.fix_is_stale())
        self.assertEqual(self.app.fix_state()["fix_age_ms"], 3000)
        self.assertFalse(self.app.quality_streaks()["gate_accepted"])
        self.assertIsNone(self.app.quality_streaks()["gate_streak_sec"])

    def test_wrapped_ticks_count_as_stale_not_fresh(self):
        self.publish(GGA, 1000)
        with mock.patch.object(time, "ticks_diff", side_effect=lambda a, b: -5):
            self.assertEqual(self.app.fix_age_ms(), 2 ** 29)
            self.assertTrue(self.app.fix_is_stale())

    def test_gst_after_expiry_is_ignored(self):
        self.publish(GGA, 1000)
        self.now += 3000
        with mock.patch("tracking.tracker") as tracker:
            self.assertFalse(self.app.attach_gst({"utc": "120000.00", "horizontal_sigma_m": 0.01}))
            tracker.enqueue_fix.assert_not_called()
        self.assertNotIn("receiver_accuracy", self.app.last_fix)

    def test_streak_restarts_after_a_stale_period(self):
        self.publish(dict(GGA, receiver_accuracy={"horizontal_sigma_m": 0.01, "altitude_sigma_m": 0.02},
                          correction_age_sec=1.0), 1000)
        self.assertTrue(self.app.quality_streaks()["gate_accepted"])
        self.now += 5000
        self.assertFalse(self.app.quality_streaks()["gate_accepted"])
        self.publish(dict(GGA, utc="120005.00", receiver_accuracy={"horizontal_sigma_m": 0.01,
                          "altitude_sigma_m": 0.02}, correction_age_sec=1.0), 0)
        self.assertTrue(self.app.quality_streaks()["gate_accepted"])
        self.assertEqual(self.app.quality_streaks()["gate_streak_sec"], 0)

    def test_sequence_counts_only_published_fixes(self):
        self.publish(GGA, 1000)
        self.publish(dict(GGA, utc="120000.20"), 200)      # inside the 950 ms throttle
        self.assertEqual(self.app.fix_state()["fix_seq"], 1)
        self.publish(dict(GGA, utc="120001.00"), 800)
        self.assertEqual(self.app.fix_state()["fix_seq"], 2)

    def test_gst_bumps_the_revision_only(self):
        self.publish(GGA, 1000)
        before = self.app.fix_state()
        self.now += 400
        with mock.patch("tracking.tracker") as tracker:
            tracker.enqueue_fix.return_value = False
            self.assertTrue(self.app.attach_gst({"utc": "120000.00", "horizontal_sigma_m": 0.01}))
        after = self.app.fix_state()
        self.assertEqual(after["fix_rev"], before["fix_rev"] + 1)
        self.assertEqual(after["fix_seq"], before["fix_seq"])
        self.assertEqual(after["fix_age_ms"], 400)

    def test_direct_assignment_without_stamp_is_fresh(self):
        self.app._last_fix = dict(GGA)
        self.assertFalse(self.app.fix_is_stale())
        self.assertIsNone(self.app.fix_age_ms())

    def test_expiry_limit_comes_from_config(self):
        self.publish(GGA, 1000)
        with mock.patch.dict(cfg.CONFIG, {"fix_stale_after_ms": 500}):
            self.now += 501
            self.assertTrue(self.app.fix_is_stale())

    # Sticky expiry, single-read snapshots and dwell accounting across silences.

    def test_full_tick_wrap_does_not_make_a_stale_fix_fresh_again(self):
        self.publish(GGA, 1000)
        self.now += 3000
        self.assertTrue(self.app.fix_is_stale())          # observed: the flag latches
        # A whole tick period later ticks_diff aliases to a small positive age.
        with mock.patch.object(time, "ticks_diff", side_effect=lambda a, b: 1000):
            self.assertTrue(self.app.fix_is_stale())
            self.assertEqual(self.app.fix_age_ms(), 2 ** 29)
            self.assertTrue(self.app.fix_state()["stale"])
            self.assertFalse(self.app.quality_streaks()["gate_accepted"])
        self.publish(dict(GGA, utc="120005.00"), 100)
        self.assertFalse(self.app.fix_is_stale())
        self.assertEqual(self.app.fix_age_ms(), 0)

    def run_led(self, steps, on_step):
        """Drive StatusLED.run for `steps` sleeps of 100 ms; return the LED value after each."""
        import asyncio
        import gnss
        led = gnss.StatusLED(0)
        values = []

        async def fake_sleep(seconds):
            values.append(led.led.value())
            self.now += int(seconds * 1000)
            on_step(len(values))
            if len(values) >= steps:
                gnss.shutdown_event.set()

        gnss.shutdown_event.clear()
        self.addCleanup(gnss.shutdown_event.clear)
        with mock.patch.object(gnss.asyncio, "sleep", fake_sleep), mock.patch.object(gnss, "log"), \
                mock.patch.object(gnss, "app", self.app):
            asyncio.run(led.run())
        return values

    def test_status_led_drops_to_no_fix_pattern_when_the_uart_goes_silent(self):
        self.publish(GGA, 1000)                      # RTK fixed: solid on
        stale_after = cfg.CONFIG["fix_stale_after_ms"]
        values = self.run_led(60, lambda step: None)
        self.assertEqual(set(values[:stale_after // 100 - 2]), {1})
        # Silent GNSS UART: no new GGA, the 0.2 s blink of "no fix" takes over
        # within fix_stale_after_ms (plus one slice).
        tail = values[stale_after // 100 + 2:]
        self.assertEqual(set(tail), {0, 1})
        flips = sum(1 for a, b in zip(tail, tail[1:]) if a != b)
        self.assertGreaterEqual(flips, 5)

    def test_status_led_returns_to_the_fix_pattern_with_the_next_gga(self):
        self.publish(GGA, 1000)

        def resume(step):
            if step == 50:
                self.publish(dict(GGA, utc="120005.00"), 100)
        values = self.run_led(60, resume)
        self.assertEqual(set(values[-5:]), {1})

    def test_snapshot_reads_the_clock_once(self):
        self.publish(GGA, 1000)
        reads = []
        values = iter([self.now + 2999, self.now + 3000, self.now + 3001, self.now + 9999])

        def ticks():
            reads.append(1)
            return next(values)
        with mock.patch.object(time, "ticks_ms", side_effect=ticks):
            state_view = self.app.fix_state()
        self.assertEqual(len(reads), 1)
        self.assertEqual(state_view["fix_age_ms"], 2999)
        self.assertFalse(state_view["stale"])

    def test_current_fix_view_reads_the_clock_once(self):
        import web
        self.publish(GGA, 1000)
        reads = []
        values = iter([self.now + 2999, self.now + 3000, self.now + 3001, self.now + 9999])

        def ticks():
            reads.append(1)
            return next(values)
        with mock.patch.object(web, "app", self.app), \
                mock.patch.object(time, "ticks_ms", side_effect=ticks):
            fix, quality = web.current_fix_view()
        self.assertEqual(len(reads), 1)
        self.assertEqual(fix["fix_age_ms"], 2999)
        self.assertFalse(fix["stale"])
        self.assertEqual(quality, tracking.quality_check(self.app.last_fix))

    def test_boundary_age_and_stale_agree(self):
        self.publish(GGA, 1000)
        for offset, stale in ((2999, False), (3000, True), (3001, True)):
            view = self.app.fix_state(self.now + offset)
            self.assertEqual((view["fix_age_ms"], view["stale"]), (offset, stale))
            self.app._fix_expired = False

    def test_silent_period_is_not_credited_to_the_last_quality(self):
        self.publish(GGA, 1000)
        self.publish(dict(GGA, utc="120001.00"), 1000)
        before = self.app.stats["fix_quality_seconds"]["4"]
        self.publish(dict(GGA, utc="121001.00"), 600_000)
        self.assertEqual(self.app.stats["fix_quality_seconds"]["4"], before)
        self.assertEqual(self.app.quality_streaks()["streak_sec"], 0)
        self.now += 5000
        self.assertEqual(self.app.quality_streaks()["streak_sec"], 5)

    def test_fresh_gap_is_still_credited(self):
        self.publish(GGA, 1000)
        self.publish(dict(GGA, utc="120002.00"), 2000)
        self.assertEqual(self.app.stats["fix_quality_seconds"]["4"], 2)

    def test_caster_gga_needs_a_published_fix(self):
        self.assertIsNone(self.app.gga_for_caster())

    def test_caster_gga_is_the_published_sentence_until_the_fix_is_stale(self):
        self.publish(dict(GGA, raw="$GNGGA,A"), 1000)
        self.assertEqual(self.app.gga_for_caster(), "$GNGGA,A")
        self.now += 2999
        self.assertEqual(self.app.gga_for_caster(), "$GNGGA,A")
        self.now += 1
        self.assertIsNone(self.app.gga_for_caster())
        self.assertEqual(self.app.last_fix["raw"], "$GNGGA,A")   # last_fix is never cleared

    def test_caster_gga_uses_the_given_clock(self):
        self.publish(dict(GGA, raw="$GNGGA,A"), 1000)
        # Fresh reading first: an expired reading sets the sticky _fix_expired flag.
        self.assertEqual(self.app.gga_for_caster(self.now + 2999), "$GNGGA,A")
        self.assertIsNone(self.app.gga_for_caster(self.now + 3000))

    def test_caster_gga_follows_the_published_fix_not_the_newest_sentence(self):
        self.publish(dict(GGA, raw="$GNGGA,A"), 1000)
        self.publish(dict(GGA, utc="120000.50", raw="$GNGGA,B"), 500)   # inside the 950 ms throttle
        self.assertEqual(self.app.gga_for_caster(), "$GNGGA,A")
        self.now += 2500                                               # 3000 ms after A
        self.assertIsNone(self.app.gga_for_caster())

    def test_caster_gga_is_withheld_after_fix_loss_and_returns_with_the_next_fix(self):
        self.publish(dict(GGA, raw="$GNGGA,A"), 1000)
        self.publish(dict(GGA, utc="120001.00", qual=0, fix_status_text="NO_FIX",
                          raw="$GNGGA,Z"), 1000)
        self.assertIsNone(self.app.gga_for_caster())
        self.assertEqual(self.app.last_fix["qual"], 0)
        self.publish(dict(GGA, utc="120002.00", qual=1, fix_status_text="GPS_FIX",
                          raw="$GNGGA,C"), 1000)
        self.assertEqual(self.app.gga_for_caster(), "$GNGGA,C")

    def test_caster_gga_returns_after_a_silent_period(self):
        self.publish(dict(GGA, raw="$GNGGA,A"), 1000)
        self.now += 5000
        self.assertIsNone(self.app.gga_for_caster())
        self.publish(dict(GGA, utc="120006.00", raw="$GNGGA,D"), 0)
        self.assertEqual(self.app.gga_for_caster(), "$GNGGA,D")

    def test_caster_gga_is_withheld_when_the_fix_is_lost_inside_the_throttle(self):
        self.publish(dict(GGA, raw="$GNGGA,A"), 1000)
        self.publish(dict(GGA, utc="120000.50", qual=0, fix_status_text="NO_FIX",
                          raw="$GNGGA,Z"), 500)                         # throttled, not published
        self.assertEqual(self.app.last_fix["raw"], "$GNGGA,A")
        self.assertEqual(self.app.last_fix["qual"], 4)
        self.assertIsNone(self.app.gga_for_caster())
        self.now += 2000                                               # silence, 2500 ms after A
        self.assertIsNone(self.app.gga_for_caster())
        self.now += 500
        self.assertTrue(self.app.fix_is_stale())
        self.assertIsNone(self.app.gga_for_caster())
        self.publish(dict(GGA, utc="120006.00", qual=1, fix_status_text="GPS_FIX",
                          raw="$GNGGA,D"), 2000)                        # return after silence
        self.assertEqual(self.app.gga_for_caster(), "$GNGGA,D")

    def test_caster_gga_returns_when_the_fix_returns_inside_the_throttle(self):
        self.publish(dict(GGA, raw="$GNGGA,A"), 1000)
        self.publish(dict(GGA, utc="120000.50", qual=0, fix_status_text="NO_FIX",
                          raw="$GNGGA,Z"), 500)
        self.assertIsNone(self.app.gga_for_caster())
        self.publish(dict(GGA, utc="120000.80", raw="$GNGGA,B"), 300)   # still throttled
        self.assertEqual(self.app.gga_for_caster(), "$GNGGA,A")          # published sentence, fix received
        self.publish(dict(GGA, utc="120001.00", raw="$GNGGA,C"), 200)   # 1000 ms after A: published
        self.assertEqual(self.app.gga_for_caster(), "$GNGGA,C")

    def test_caster_gga_needs_a_received_gga(self):
        self.app._last_fix = dict(GGA, raw="$GNGGA,A")   # assigned without update_fix: _fix_qual is None
        self.assertIsNone(self.app.gga_for_caster())

    def test_caster_gga_respects_the_wrap_guard(self):
        self.publish(dict(GGA, raw="$GNGGA,A"), 1000)
        with mock.patch.object(time, "ticks_diff", side_effect=lambda a, b: -5):
            self.assertIsNone(self.app.gga_for_caster())

    def test_caster_gga_survives_the_gst_attachment(self):
        self.publish(dict(GGA, raw="$GNGGA,A"), 1000)
        self.now += 400
        with mock.patch("tracking.tracker") as tracker:
            tracker.enqueue_fix.return_value = False
            self.assertTrue(self.app.attach_gst({"utc": "120000.00", "horizontal_sigma_m": 0.01}))
        self.assertEqual(self.app.gga_for_caster(), "$GNGGA,A")
