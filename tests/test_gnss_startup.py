# SPDX-License-Identifier: AGPL-3.0-only
"""Receiver configuration, offline UTC, and cold-start route qualification."""
import sys
import unittest
from unittest import mock

from support import gnss, state, sentence


def position(second, **changes):
    fix = {"utc": "1200%02d.000" % second, "lat": 50.0, "lon": 8.0,
           "alt": 100.0, "lat_e8": 5000000000, "lon_e8": 800000000, "alt_mm": 100000,
           "qual": 1, "sats": 10, "hdop": 1.0}
    fix.update(changes)
    return fix


def frame(n):
    """An RTCM frame with an n-byte payload (CRC bytes are not checked)."""
    return b"\xd3" + bytes([(n >> 8) & 3, n & 0xFF]) + b"A" * n + b"\x00\x00\x00"


class TestRouteStartup(unittest.TestCase):
    def test_ordinary_gps_starts_offline_after_ten_seconds(self):
        guard = gnss.RouteStartupGuard()
        for second in range(11):
            # A moving route must not require a stationary antenna or corrections.
            self.assertEqual(guard.check(position(second, lon_e8=800000000 + second * 20000),
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
        self.assertFalse(guard.check(position(1, lat_e8=5000113300), 1000, True))
        self.assertFalse(guard.check(position(4, lat_e8=5000328200), 4000, True))
        self.assertEqual(guard.status()["stable_sec"], 0)
        for second in range(5, 15):
            self.assertEqual(guard.check(position(second, lat_e8=5000328200), second * 1000, True), second == 14)

    def test_invalid_or_weak_startup_positions_do_not_accumulate_time(self):
        for changes in ({"qual": 0}, {"sats": 5}, {"hdop": 4}, {"hdop": None},
                        {"hdop": float("nan")}, {"lat_e8": float("inf")}, {"alt_mm": None},
                        {"lon_e8": 18100000000}, {"lat_e8": 9100000000},
                        {"lat_e8": 5000000000.0}, {"lon_e8": True}, {"qual": 6}, {"utc": ""}):
            with self.subTest(changes=changes):
                guard = gnss.RouteStartupGuard()
                for second in range(15):
                    self.assertFalse(guard.check(position(second, **changes), second * 1000, True))

    def test_coordinate_limits_are_inclusive_and_precomputed(self):
        for changes, valid in (({"lon_e8": 18000000000}, True), ({"lon_e8": -18000000000}, True),
                               ({"lon_e8": 18000000001}, False), ({"lon_e8": -18000000001}, False),
                               ({"lat_e8": 9000000000}, True), ({"lat_e8": -9000000000}, True),
                               ({"lat_e8": 9000000001}, False), ({"lat_e8": -9000000001}, False)):
            with self.subTest(changes=changes):
                guard = gnss.RouteStartupGuard()
                results = [guard.check(position(second, **changes), second * 1000, True)
                           for second in range(11)]
                self.assertEqual(results[-1], valid)
        # W16: no bigint products or abs() per GGA on the device (small ints
        # end at 2**30 there); the limits are module constants.
        import inspect
        source = inspect.getsource(gnss.RouteStartupGuard.check)
        self.assertNotIn("COORD_SCALE", source)
        self.assertNotIn("abs(", source)
        self.assertNotIn("-_L", source)   # a negated bigint allocates as well
        self.assertEqual((gnss._LON_LIMIT_E8, gnss._LAT_LIMIT_E8), (18000000000, 9000000000))

    def test_a_fix_without_the_exact_fields_waits_for_a_position(self):
        for key in ("lat_e8", "lon_e8", "alt_mm"):
            with self.subTest(missing=key):
                guard = gnss.RouteStartupGuard()
                fix = position(0)
                del fix[key]
                self.assertFalse(guard.check(fix, 0, True))
                self.assertEqual(guard.status()["reason"], "waiting_position")

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
    WIRE_CMD = (gnss.GST_ENABLE_COMMAND + "\r\n").encode()

    def setUp(self):
        # pump() calls without a time argument (end_stream, write_rtcm) read this clock.
        self.clock = [0]
        patch = mock.patch.object(gnss.time, "ticks_ms", lambda: self.clock[0])
        patch.start()
        self.addCleanup(patch.stop)
        self.handler = gnss.GNSSHandler(object())

    def stats(self, **values):
        values.setdefault("gst_output_state", "waiting")
        return mock.patch.dict(gnss.app.stats, values, clear=True)

    def open_frame(self):
        """Write the first half of an RTCM frame: the receiver input is inside a frame."""
        self.handler.tx.write_rtcm(frame(20)[:10])

    def scripted_write(self, answers):
        """uart.write takes answers[i] bytes on call i, everything once they run out."""
        uart = self.handler.uart

        def write(data):
            n = answers.pop(0) if answers else len(data)
            uart.written.extend(bytes(data[:n]))
            return n
        return mock.patch.object(uart, "write", side_effect=write)

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

    def test_partial_command_is_completed_before_success(self):
        with self.scripted_write([3]), self.stats(), mock.patch.object(gnss, "log"):
            self.handler._ensure_gst_output(0)
            self.assertEqual(gnss.app.stats["gst_output_state"], "waiting")
            self.assertEqual(gnss.app.stats.get("gst_enable_attempts", 0), 0)
            self.handler.tx.pump(10)
            self.assertEqual(gnss.app.stats["gst_output_state"], "requested")
            self.assertEqual(gnss.app.stats["gst_enable_attempts"], 1)
            self.assertIsNone(gnss.app.stats["gst_output_error"])
            self.assertEqual(bytes(self.handler.uart.written), self.WIRE_CMD)
        self.handler = gnss.GNSSHandler(object())
        with self.scripted_write([3] + [0] * 10), self.stats(), mock.patch.object(gnss, "log"):
            self.handler._ensure_gst_output(0)
            self.assertEqual(gnss.app.stats["gst_output_state"], "waiting")
            self.handler.tx.pump(gnss.UART_STALL_MS)
            self.assertEqual(gnss.app.stats["gst_output_state"], "uart_error")
            self.assertIn("incomplete", gnss.app.stats["gst_output_error"])
            self.assertEqual(gnss.app.stats["gst_enable_attempts"], 1)
            self.assertEqual(bytes(self.handler.uart.written), self.WIRE_CMD[:3])

    def test_deferred_enable_keeps_the_state_until_written(self):
        tx = self.handler.tx
        with self.stats(), mock.patch.object(gnss, "log"):
            self.open_frame()
            self.handler._ensure_gst_output(0)
            self.assertEqual(gnss.app.stats["gst_output_state"], "waiting")
            self.assertEqual(gnss.app.stats.get("gst_enable_attempts", 0), 0)
            self.assertEqual(bytes(self.handler.uart.written), frame(20)[:10])
            # The retry step is due, but the first request still waits: no second one.
            self.handler._ensure_gst_output(2000)
            self.assertEqual(tx.counters["uart_commands_deferred"], 1)
            self.clock[0] = 2500
            tx.end_stream()
            self.assertEqual(gnss.app.stats["gst_output_state"], "requested")
            self.assertEqual(gnss.app.stats["gst_enable_attempts"], 1)
            self.assertEqual(bytes(self.handler.uart.written), frame(20)[:10] + self.WIRE_CMD)

    def test_stale_receiving_turns_to_waiting_while_the_request_waits(self):
        self.handler.last_gst_time = 0
        with self.stats(gst_output_state="receiving"), mock.patch.object(gnss, "log"):
            self.open_frame()
            self.handler._ensure_gst_output(gnss.GST_FRESH_MS)
            self.assertEqual(gnss.app.stats["gst_output_state"], "waiting")
            self.assertTrue(self.handler._gst_pending)
            self.assertEqual(gnss.app.stats.get("gst_enable_attempts", 0), 0)

    def test_late_write_keeps_receiving_with_a_fresh_gst(self):
        self.handler.last_gst_time = 0
        with self.stats(), mock.patch.object(gnss, "log"):
            self.open_frame()
            self.handler._ensure_gst_output(5000)
            self.assertTrue(self.handler._gst_pending)
            self.handler.last_gst_time = 5200   # GST arrives while the request waits
            self.handler._ensure_gst_output(5300)
            self.assertEqual(gnss.app.stats["gst_output_state"], "receiving")
            self.clock[0] = 5500
            self.handler.tx.end_stream()
            self.assertEqual(gnss.app.stats["gst_output_state"], "receiving")
            self.assertEqual(gnss.app.stats["gst_enable_attempts"], 1)
            self.assertIsNone(gnss.app.stats["gst_output_error"])
            self.assertTrue(bytes(self.handler.uart.written).endswith(self.WIRE_CMD))

    def test_late_write_after_gst_stopped_reports_requested(self):
        self.handler.last_gst_time = 0
        with self.stats(), mock.patch.object(gnss, "log"):
            self.open_frame()
            self.handler._ensure_gst_output(5000)
            self.assertEqual(gnss.app.stats.get("gst_enable_attempts", 0), 0)
            self.assertEqual(bytes(self.handler.uart.written), frame(20)[:10])
            self.clock[0] = 9000   # GST 9 s old, the request has waited 4 s
            self.handler.tx.end_stream()
            self.assertEqual(gnss.app.stats["gst_output_state"], "requested")
            self.assertEqual(gnss.app.stats["gst_enable_attempts"], 1)
            self.assertTrue(bytes(self.handler.uart.written).endswith(self.WIRE_CMD))

    def test_dropped_enable_is_neither_an_error_nor_an_attempt(self):
        tx = self.handler.tx
        with self.stats(gst_output_error="UART error: earlier"), mock.patch.object(gnss, "log"):
            self.open_frame()
            self.handler._ensure_gst_output(0)
            tx.pump(gnss.COMMAND_MAX_WAIT_MS)
            self.assertEqual(tx.counters["uart_commands_dropped"], 1)
            self.assertEqual(gnss.app.stats["gst_output_state"], "waiting")
            self.assertEqual(gnss.app.stats["gst_output_error"], "UART error: earlier")
            self.assertEqual(gnss.app.stats.get("gst_enable_attempts", 0), 0)
            self.assertFalse(self.handler._gst_pending)
            self.assertEqual(bytes(self.handler.uart.written), frame(20)[:10])
            tx.end_stream()
            self.handler._ensure_gst_output(gnss.COMMAND_MAX_WAIT_MS + gnss.GST_RETRY_MS[0])
            self.assertEqual(bytes(self.handler.uart.written), frame(20)[:10] + self.WIRE_CMD)
            self.assertEqual(gnss.app.stats["gst_output_state"], "requested")
            self.assertEqual(gnss.app.stats["gst_enable_attempts"], 1)
            self.assertIsNone(gnss.app.stats["gst_output_error"])

    def test_gnss_loop_drives_the_transmitter(self):
        import inspect
        source = inspect.getsource(gnss.GNSSHandler.run)
        self.assertIn("self.tx.pump(", source)
        self.assertLess(source.index("self.tx.pump("), source.index("self._ensure_gst_output("))

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

    LINE = "GNRMC,082700,A,5000.000,N,00800.000,E,0,0,110926,,,A"

    def sync(self, handler, device_tm, now_ms=0, valid=True):
        """Run _sync_rmc_clock against a simulated RTC reading device_tm."""
        with mock.patch.object(gnss, "clock_is_valid", return_value=valid), \
                mock.patch.object(gnss.time, "gmtime", return_value=device_tm), \
                mock.patch.object(gnss.time, "ticks_ms", return_value=now_ms), \
                mock.patch.object(gnss, "RTC") as rtc, mock.patch.object(gnss, "log"), \
                mock.patch.object(gnss.field_recorder, "event") as event:
            handler._sync_rmc_clock(sentence(self.LINE))
        return rtc.return_value.datetime, event

    def test_utc_seconds_matches_the_calendar(self):
        import calendar
        for tm in ((2026, 9, 11, 8, 27, 0), (2024, 2, 29, 23, 59, 59),
                   (2000, 1, 1, 0, 0, 0), (2099, 12, 31, 1, 2, 3)):
            self.assertEqual(gnss._utc_seconds(tm), calendar.timegm(tm) - 946684800)

    def test_receiver_time_sets_an_uninitialized_clock(self):
        handler = gnss.GNSSHandler(object())
        before = state.app.clock_steps
        setter, event = self.sync(handler, (2000, 1, 1, 5, 0, 3, 5, 1), valid=False)
        setter.assert_called_once_with((2026, 9, 11, 0, 8, 27, 0, 0))
        event.assert_not_called()
        self.assertEqual(state.app.clock_steps, before)   # first set is not a step

    def test_drift_over_one_second_is_stepped_and_recorded(self):
        handler = gnss.GNSSHandler(object())
        before = state.app.clock_steps
        # RTC runs 3 s ahead of the receiver.
        setter, event = self.sync(handler, (2026, 9, 11, 8, 27, 3, 4, 254))
        setter.assert_called_once_with((2026, 9, 11, 0, 8, 27, 0, 0))
        event.assert_called_once_with("clock_resync", {
            "reason": "drift", "delta_s": -3,
            "old": "2026-09-11T08:27:03Z", "new": "2026-09-11T08:27:00Z"})
        self.assertEqual(state.app.clock_steps, before + 1)
        self.assertEqual(state.app.stats["clock_last_delta_s"], -3)

    def test_sub_second_phase_does_not_step_between_hourly_checks(self):
        handler = gnss.GNSSHandler(object())
        self.sync(handler, (2026, 9, 11, 8, 27, 0, 4, 254), now_ms=0)    # in sync: arms the timer
        for device in ((2026, 9, 11, 8, 26, 59, 4, 254), (2026, 9, 11, 8, 27, 1, 4, 254)):
            setter, event = self.sync(handler, device, now_ms=1000)
            setter.assert_not_called()
            event.assert_not_called()

    def test_hourly_check_ignores_sub_second_phase(self):
        # RTC = real - L, so the whole-second delta is 0 or +-1 at random; that is
        # phase, not drift, and must not step, count or blank the epoch age.
        handler = gnss.GNSSHandler(object())
        self.sync(handler, (2026, 9, 11, 8, 27, 0, 4, 254), now_ms=0)
        before = state.app.clock_steps
        setter, event = self.sync(handler, (2026, 9, 11, 8, 26, 59, 4, 254), now_ms=3600000)
        setter.assert_not_called()
        event.assert_not_called()
        self.assertEqual(state.app.clock_steps, before)
        # The check counts as done: the next one is an hour away.
        self.assertEqual(handler._clock_checked_ms, 3600000)

    def test_hourly_check_still_steps_a_real_deviation(self):
        handler = gnss.GNSSHandler(object())
        self.sync(handler, (2026, 9, 11, 8, 27, 0, 4, 254), now_ms=0)
        setter, event = self.sync(handler, (2026, 9, 11, 8, 26, 58, 4, 254), now_ms=3600000)
        setter.assert_called_once()
        self.assertEqual(event.call_args[0][1]["reason"], "drift")
        self.assertEqual(event.call_args[0][1]["delta_s"], 2)

    def test_backward_step_gives_no_negative_ticks_age_and_clears_the_epoch_cache(self):
        import net
        from unittest import mock as m
        monitor = net.RtcmCounter()
        monitor.last_epoch_age_ms = 800
        monitor.last_epoch_ticks_ms = 1000
        monitor._epoch_clock_steps = state.app.clock_steps
        with m.patch.object(net.time, "ticks_ms", return_value=2000), \
                m.patch.object(net, "_clock_valid", return_value=True):
            self.assertEqual(monitor.epoch_age_ms(), 1800)
            state.app.clock_steps += 1          # RMC re-sync stepped the RTC
            self.assertIsNone(monitor.epoch_age_ms())
            self.assertFalse(monitor.epoch_stale())

    def test_gga_interval_follows_ticks_not_the_wall_clock(self):
        import asyncio
        import net
        client = net.NtripClient.__new__(net.NtripClient)
        client._last_gga = None
        sent = []

        async def send(writer):
            sent.append(1)
            return True
        client._sende_gga = send
        now = [5000]
        with mock.patch.dict(net.CONFIG, {"ntrip_gga_interval_sec": 10}), \
                mock.patch.object(net.time, "ticks_ms", side_effect=lambda: now[0]), \
                mock.patch.object(net.time, "time", side_effect=AssertionError("wall clock")):
            for step, due in ((0, True), (9000, False), (1000, True)):
                now[0] += step
                self.assertEqual(asyncio.run(client._gga_due(None)), due)
        self.assertEqual(len(sent), 2)

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


class TestNtpClockStep(unittest.TestCase):
    def test_clock_validity_has_one_definition(self):
        import net
        import gnss
        self.assertIs(net._clock_valid, state.clock_is_valid)
        self.assertIs(gnss.clock_is_valid, state.clock_is_valid)
        for year, valid in ((2023, False), (2024, True), (2099, True), (2100, False)):
            self.assertIs(state.clock_is_valid((year, 1, 1, 0, 0, 0, 0, 1)), valid)

    def run_sync(self, valid_before, before_s, after_s, ticks=(0, 0)):
        import net
        import types
        times = iter([before_s, after_s])
        fake = types.SimpleNamespace(settime=lambda: None)
        with mock.patch.dict(sys.modules, {"ntptime": fake}), \
                mock.patch.dict(net.CONFIG, {"ntp_sync": True}), \
                mock.patch.object(net, "_clock_valid", return_value=valid_before), \
                mock.patch.object(net.time, "time", side_effect=lambda: next(times)), \
                mock.patch.object(net.time, "ticks_ms", side_effect=list(ticks)), \
                mock.patch.object(net, "log"), \
                mock.patch.object(net.field_recorder, "event") as event:
            net._zeit_holen()
        return event

    def test_ntp_step_of_a_valid_clock_counts_and_is_recorded(self):
        before = state.app.clock_steps
        event = self.run_sync(True, 1000, 1003)
        self.assertEqual(state.app.clock_steps, before + 1)
        event.assert_any_call("clock_resync", {"reason": "ntp", "delta_s": 3})
        self.assertEqual(state.app.stats["clock_last_delta_s"], 3)

    def test_request_latency_and_phase_are_not_a_step(self):
        before = state.app.clock_steps
        # 3 s elapsed on the wall clock, all of it spent in the request.
        event = self.run_sync(True, 1000, 1003, ticks=(0, 3000))
        self.assertEqual(state.app.clock_steps, before)
        self.assertNotIn("clock_resync", [c[0][0] for c in event.call_args_list])
        event = self.run_sync(True, 1000, 1001)      # one second: RTC phase
        self.assertEqual(state.app.clock_steps, before)

    def test_first_ntp_set_of_an_invalid_clock_is_not_a_step(self):
        before = state.app.clock_steps
        event = self.run_sync(False, 100, 841057764)
        self.assertEqual(state.app.clock_steps, before)
        self.assertNotIn("clock_resync", [c[0][0] for c in event.call_args_list])


class TestApPasswordRollback(unittest.TestCase):
    def test_failed_save_still_replaces_a_weak_password_for_this_boot(self):
        import net
        manager = net.NetworkManager.__new__(net.NetworkManager)
        manager.wlan_ap = mock.Mock()
        manager.wlan_ap.config.return_value = b"\x00" * 6
        with mock.patch.dict(net.CONFIG, {"ap_pass": ""}), \
                mock.patch.object(net, "generate_ap_password", return_value="gnss-new"), \
                mock.patch.object(net, "save_config", return_value=False) as save, \
                mock.patch.object(net, "log"):
            manager._ensure_ap_password()
            self.assertEqual(save.call_args[0][0], {"ap_pass": "gnss-new"})
            # The old value was empty: an unsaved random password beats an open AP.
            self.assertEqual(net.CONFIG["ap_pass"], "gnss-new")
