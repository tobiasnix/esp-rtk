# SPDX-License-Identifier: AGPL-3.0-only
"""Receiver configuration, offline UTC, and cold-start route qualification."""
import unittest
from unittest import mock

from support import gnss, state, sentence


def position(second, **changes):
    fix = {"utc": "1200%02d.000" % second, "lat": 50.0, "lon": 8.0,
           "alt": 100.0, "qual": 1, "sats": 10, "hdop": 1.0}
    fix.update(changes)
    return fix


class TestRouteStartup(unittest.TestCase):
    def test_ordinary_gps_starts_offline_after_ten_seconds(self):
        guard = gnss.RouteStartupGuard()
        for second in range(11):
            # A moving route must not require a stationary antenna or corrections.
            self.assertEqual(guard.check(position(second, lon=8 + second * .0002),
                                         second * 1000, True), second == 10)
        self.assertEqual(guard.status()["reason"], "ready")
        self.assertTrue(guard.check(position(11, hdop=5, sats=4), 11000, True))

    def test_unset_clock_blocks_even_a_fixed_solution(self):
        guard = gnss.RouteStartupGuard()
        for second in range(30):
            self.assertFalse(guard.check(position(second, qual=4), second * 1000, False))
        self.assertEqual(guard.status()["reason"], "waiting_clock")
        for second in range(11):
            self.assertEqual(guard.check(position(second), 30000 + second * 1000, True), second == 10)

    def test_cold_start_jumps_restart_the_settle_window(self):
        guard = gnss.RouteStartupGuard()
        guard.check(position(0), 0, True)
        # Approximately 126 m in 1 s, then 239 m in 3 s: the reported failure shape.
        self.assertFalse(guard.check(position(1, lat=50.001133), 1000, True))
        self.assertFalse(guard.check(position(4, lat=50.003282), 4000, True))
        self.assertEqual(guard.status()["stable_sec"], 0)
        for second in range(5, 15):
            self.assertEqual(guard.check(position(second, lat=50.003282), second * 1000, True), second == 14)

    def test_invalid_or_weak_startup_positions_do_not_accumulate_time(self):
        for changes in ({"qual": 0}, {"sats": 5}, {"hdop": 4}, {"hdop": None},
                        {"hdop": float("nan")}, {"lat": float("inf")}, {"alt": None},
                        {"lon": 181}, {"qual": 6}, {"utc": ""}):
            with self.subTest(changes=changes):
                guard = gnss.RouteStartupGuard()
                for second in range(15):
                    self.assertFalse(guard.check(position(second, **changes), second * 1000, True))

    def test_duplicate_epochs_and_buffered_bursts_cannot_satisfy_settling(self):
        guard = gnss.RouteStartupGuard()
        for second in range(20):
            self.assertFalse(guard.check(position(0), second * 1000, True))
        for second in range(20):
            self.assertFalse(guard.check(position(second), 20000, True))

    def test_receiver_gap_and_loss_of_fix_rearm_after_a_good_start(self):
        guard = gnss.RouteStartupGuard()
        for second in range(11):
            guard.check(position(second), second * 1000, True)
        self.assertTrue(guard.ready)
        self.assertFalse(guard.check(position(40), 40000, True))
        for second in range(41, 51):
            guard.check(position(second), second * 1000, True)
        self.assertTrue(guard.ready)
        self.assertFalse(guard.check(position(51, qual=0), 51000, True))
        self.assertFalse(guard.check(position(52), 52000, True))

    def test_tick_wrap_does_not_shorten_the_window(self):
        period = 1 << 20
        with mock.patch.object(gnss.time, "ticks_diff",
                               side_effect=lambda a, b: (a - b + period // 2) % period - period // 2):
            guard = gnss.RouteStartupGuard()
            for second in range(11):
                now = (period - 5000 + second * 1000) % period
                self.assertEqual(guard.check(position(second), now, True), second == 10)


class TestGstConfiguration(unittest.TestCase):
    def setUp(self):
        self.handler = gnss.GNSSHandler(object())

    def test_enable_is_checksummed_and_retried_without_flash_save(self):
        self.assertEqual(sentence("PQTMCFGMSGRATE,W,GST,1"), gnss.GST_ENABLE_COMMAND)
        with mock.patch.object(gnss, "log"), mock.patch.dict(gnss.app.stats, {}, clear=True):
            for now in (0, 1, 1999, 2000, 2001, 6999, 7000):
                self.handler._ensure_gst_output(now)
            self.assertEqual(self.handler.uart.written,
                             ((gnss.GST_ENABLE_COMMAND + "\r\n") * 3).encode())
            self.assertEqual(gnss.app.stats["gst_output_state"], "requested")
            self.assertNotIn(b"SAVEPAR", self.handler.uart.written)

    def test_received_gst_stops_commands_and_silence_reenables_output(self):
        with mock.patch.object(gnss, "log"), mock.patch.dict(gnss.app.stats, {}, clear=True):
            self.handler._ensure_gst_output(0)
            self.handler.last_gst_time = 1000
            self.handler._gst_attempts = 0
            self.handler._ensure_gst_output(5999)
            self.assertEqual(gnss.app.stats["gst_output_state"], "receiving")
            self.assertEqual(gnss.app.stats["gst_enable_attempts"], 1)
            self.handler._ensure_gst_output(6000)
            self.assertEqual(gnss.app.stats["gst_enable_attempts"], 2)
            self.assertEqual(gnss.app.stats["gst_output_state"], "requested")

    def test_uart_error_is_visible_and_retried(self):
        with mock.patch.object(self.handler.uart, "write", side_effect=OSError("busy")), \
                mock.patch.dict(gnss.app.stats, {}, clear=True):
            self.handler._ensure_gst_output(0)
            self.assertEqual(gnss.app.stats["gst_output_state"], "uart_error")
            self.assertIn("busy", gnss.app.stats["gst_output_error"])
        with mock.patch.object(gnss, "log"):
            self.handler._ensure_gst_output(2000)
        self.assertTrue(self.handler.uart.written)

    def test_partial_command_is_not_reported_as_success(self):
        with mock.patch.object(self.handler.uart, "write", return_value=3), \
                mock.patch.dict(gnss.app.stats, {}, clear=True):
            self.handler._ensure_gst_output(0)
            self.assertEqual(gnss.app.stats["gst_output_state"], "uart_error")
            self.assertIn("incomplete", gnss.app.stats["gst_output_error"])

    def test_nonfinite_gst_is_not_an_accuracy_estimate(self):
        for value in ("nan", "inf", "-inf"):
            self.assertIsNone(gnss.GNSSHandler.parse_gst(
                sentence("GNGST,120010.000,0.01,0.02,0.01,0,%s,0.01,0.02" % value)))


class TestGnssClock(unittest.TestCase):
    def test_valid_utc_calendar_and_active_solution_are_required(self):
        def rmc(stamp="082700.125", date="110926", status="A"):
            return sentence("GNRMC,%s,%s,5000.000,N,00800.000,E,0,0,%s,,,A" % (stamp, status, date))
        self.assertEqual(gnss.rmc_datetime(rmc()), (2026, 9, 11, 0, 8, 27, 0, 0))
        self.assertIsNotNone(gnss.rmc_datetime(rmc(date="290224")))
        for changes in ({"date": "290225"}, {"date": "310926"}, {"date": "110900"},
                        {"date": "1109260"}, {"stamp": "246000"}, {"stamp": "082700.x"},
                        {"status": "V"}, {"stamp": ""}):
            self.assertIsNone(gnss.rmc_datetime(rmc(**changes)), changes)

    def test_receiver_time_sets_only_an_uninitialized_clock(self):
        handler = gnss.GNSSHandler(object())
        line = sentence("GNRMC,082700,A,5000.000,N,00800.000,E,0,0,110926,,,A")
        with mock.patch.object(gnss, "clock_is_valid", return_value=False), \
                mock.patch.object(gnss, "RTC") as rtc, mock.patch.object(gnss, "log"):
            handler._sync_rmc_clock(line)
            rtc.return_value.datetime.assert_called_once_with((2026, 9, 11, 0, 8, 27, 0, 0))
        with mock.patch.object(gnss, "clock_is_valid", return_value=True), \
                mock.patch.object(gnss, "RTC") as rtc:
            handler._sync_rmc_clock(line)
            rtc.assert_not_called()

    def test_uptime_is_independent_of_rtc_sync_and_tick_wrap(self):
        now = [1048000]
        period = 1 << 20
        with mock.patch.object(state.time, "ticks_ms", side_effect=lambda: now[0]), \
                mock.patch.object(state.time, "ticks_diff",
                                  side_effect=lambda a, b: (a - b + period // 2) % period - period // 2):
            app = state.AppState()
            now[0] = (now[0] + 2500) % period
            with mock.patch.object(state.time, "time", return_value=900000000):
                self.assertEqual(app.uptime_seconds(), 2)
            now[0] += 500
            with mock.patch.object(state.time, "time", return_value=0):
                self.assertEqual(app.uptime_seconds(), 3)


if __name__ == "__main__":
    unittest.main()
