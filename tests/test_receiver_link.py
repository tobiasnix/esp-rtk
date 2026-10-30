# SPDX-License-Identifier: AGPL-3.0-only
"""Receiver UART silence: link state, one log line per transition, GNSS loop wiring."""
import asyncio
import unittest
from unittest import mock

import support
from support import gnss, state, sentence


GGA = sentence("GNGGA,120000.00,5000.0000000,N,00800.0000000,E,1,10,1.0,100.0,M,47.0,M,,")
RMC = sentence("GNRMC,120000.00,A,5000.0000000,N,00800.0000000,E,0.0,0.0,080926,,,A")
CHUNK = (GGA + "\r\n" + RMC + "\r\n").encode()


def wrapping_diff(period):
    """ticks_diff of a tick counter with the given period (MicroPython semantics)."""
    return lambda a, b: (a - b + period // 2) % period - period // 2


def time_diff(a, b, period):
    return wrapping_diff(period)(a, b)


class FreshApp:
    """A newly constructed AppState as state.app and gnss.app, with log and recorder spies."""

    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        self.log = support.patch_module_global("state", "log").start()
        self.recorder = mock.Mock()
        mock.patch.dict(state._instances, {"field_diagnostics": self.recorder}).start()
        self.use_fresh_app()

    def use_fresh_app(self):
        """Start over with another fresh AppState (also between subtests)."""
        self.app = state.AppState()
        mock.patch.object(state, "app", self.app).start()
        mock.patch.object(gnss, "app", self.app).start()
        self.log.reset_mock()
        self.recorder.reset_mock()

    def levels(self, level):
        return [call for call in self.log.call_args_list if call.args[0] == level]

    def link_events(self):
        return [call.args for call in self.recorder.event.call_args_list
                if call.args[0] == "receiver_link"]


class TestReceiverLinkState(FreshApp, unittest.TestCase):
    def go_silent_from_boot(self):
        self.app.start_receiver_watch(1000)
        for now in range(1000, 13000, 100):
            before = self.log.call_count
            self.assertEqual(self.app.check_receiver_link(now), "silent" if now >= 4000 else "ok")
            if self.log.call_count != before:
                self.assertEqual(now, 4000)

    def test_unknown_until_the_gnss_handler_watches(self):
        self.assertEqual(self.app.receiver_link(), "unknown")
        self.assertIsNone(self.app.uart_silence_ms())
        self.assertEqual(self.app.check_receiver_link(), "unknown")
        self.log.assert_not_called()
        self.recorder.event.assert_not_called()

    def test_silent_uart_from_boot_warns_once_and_keeps_beating(self):
        self.go_silent_from_boot()
        self.assertEqual(len(self.log.call_args_list), 1)
        level, tag, text = self.log.call_args.args
        self.assertEqual((level, tag), ("WARN", "GNSS"))
        self.assertIn("silent for 3 s", text)
        self.assertEqual(self.link_events(), [
            ("receiver_link", {"previous": None, "state": "silent", "silence_ms": 3000})])
        self.assertEqual(self.app.stats["task_restarts"], 0)

    def test_data_resumes_with_one_info_line(self):
        self.go_silent_from_boot()
        self.app.note_uart_data(13050)
        self.assertEqual(self.app.receiver_link(13100), "ok")
        self.assertEqual(self.app.check_receiver_link(13100), "ok")
        self.assertEqual(len(self.log.call_args_list), 2)
        level, tag, text = self.log.call_args.args
        self.assertEqual((level, tag), ("INFO", "GNSS"))
        self.assertIn("resumed after 12 s", text)
        self.assertEqual(self.link_events()[-1], (
            "receiver_link", {"previous": "silent", "state": "ok", "silence_ms": 12050}))
        for now in range(13200, 20000, 100):
            self.app.note_uart_data(now)
            self.assertEqual(self.app.check_receiver_link(now + 50), "ok")
        self.assertEqual(len(self.log.call_args_list), 2)
        self.assertEqual(len(self.link_events()), 2)

    def test_regular_data_never_logs(self):
        self.app.start_receiver_watch(0)
        for now in range(0, 60000, 100):
            if now % 1000 == 0:
                self.app.note_uart_data(now)
            self.assertEqual(self.app.check_receiver_link(now), "ok")
        self.log.assert_not_called()
        self.recorder.event.assert_not_called()

    def test_threshold_follows_fix_stale_after_ms(self):
        with mock.patch.dict(state.CONFIG, {"fix_stale_after_ms": 5000}):
            self.app.start_receiver_watch(0)
            self.app.note_uart_data(1000)
            self.assertEqual(self.app.uart_silence_ms(5999), 4999)
            self.assertEqual(self.app.receiver_link(5999), "ok")
            self.assertEqual(self.app.check_receiver_link(5999), "ok")
            self.assertEqual(self.app.receiver_link(6000), "silent")
            self.assertEqual(self.app.check_receiver_link(6000), "silent")
        self.assertEqual(len(self.levels("WARN")), 1)

    def test_watch_start_is_kept_across_task_restarts(self):
        self.app.start_receiver_watch(1000)
        self.app.start_receiver_watch(9000)
        self.assertEqual(self.app.uart_silence_ms(9000), 8000)

    def silent_across_a_full_wrap(self, period):
        """Silent at 4000, then checked about once a second for a full tick period."""
        self.app.start_receiver_watch(0)
        self.app.note_uart_data(1000)
        self.assertEqual(self.app.check_receiver_link(4000), "silent")
        for elapsed in range(1000, period, 1000):
            self.assertEqual(self.app.check_receiver_link((4000 + elapsed) % period), "silent")
        # One full period after the last byte the counter reads 1100 again.
        self.assertEqual(time_diff(1100, 1000, period), 100)
        self.assertEqual(self.app.receiver_link(1100), "silent")
        self.assertGreaterEqual(self.app.uart_silence_ms(1100), 2 ** 29)
        self.assertEqual(self.app.check_receiver_link(1100), "silent")
        self.assertEqual(len(self.levels("WARN")), 1)

    def test_tick_wrap_keeps_a_silent_receiver_silent(self):
        period = 1 << 20
        with mock.patch.object(state.time, "ticks_diff", side_effect=wrapping_diff(period)):
            self.silent_across_a_full_wrap(period)
            self.app.note_uart_data(1150)
            self.assertEqual(self.app.receiver_link(1200), "ok")
            self.assertEqual(self.app.check_receiver_link(1200), "ok")
        self.assertEqual(len(self.levels("WARN")), 1)
        self.assertEqual(len(self.levels("INFO")), 1)

    def test_a_byte_with_the_old_tick_value_after_a_full_wrap_clears_silence(self):
        # Every delivered byte resets the silence, even one whose tick value equals
        # the stamp the silence was measured from (exactly one tick cycle later).
        period = 1 << 20
        with mock.patch.object(state.time, "ticks_diff", side_effect=wrapping_diff(period)):
            self.silent_across_a_full_wrap(period)
            self.app.note_uart_data(1000)
            self.assertEqual(self.app.uart_silence_ms(1100), 100)
            self.assertEqual(self.app.receiver_link(1100), "ok")
            self.assertEqual(self.app.check_receiver_link(1100), "ok")
            # The recovery line measures from the old reference; the aliased length
            # is reported as 2**29 like fix_age_ms does.
            self.assertEqual(self.link_events()[-1], (
                "receiver_link", {"previous": "silent", "state": "ok", "silence_ms": 2 ** 29}))
            self.app.note_uart_data(2000)
            self.assertEqual(self.app.check_receiver_link(2100), "ok")
        self.assertEqual(len(self.levels("WARN")), 1)
        self.assertEqual(len(self.levels("INFO")), 1)


class Clock:
    """ticks_ms that advances by a fixed step on every reading."""

    def __init__(self, start=0, step=7, period=None):
        self.now, self.step, self.period = start, step, period

    def advance(self, ms):
        self.now += ms

    def peek(self):
        return self.now % self.period if self.period else self.now

    def ticks_ms(self):
        self.now += self.step
        return self.peek()


class StopAfter:
    """sleep_ms/sleep double: advances the clock and stops the loop on a condition."""

    def __init__(self, stop, clock, advance_ms, done):
        self.stop, self.clock, self.advance_ms, self.done = stop, clock, advance_ms, done
        self.calls = 0

    async def __call__(self, delay):
        self.calls += 1
        self.clock.advance(self.advance_ms)
        if self.done(self):
            self.stop.set()
        await asyncio.sleep(0)


class Queue:
    def put_nowait(self, data): return True
    def qsize(self): return 0


class TestGnssLoopReceiverLink(FreshApp, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stop = asyncio.Event()
        FreshApp.setUp(self)
        mock.patch.object(gnss, "log").start()
        mock.patch.object(gnss, "shutdown_event", self.stop).start()
        mock.patch.object(gnss, "replies", []).start()

    def use_fresh_app(self):
        FreshApp.use_fresh_app(self)
        self.stop.clear()
        mock.patch.object(self.app, "update_fix").start()
        mock.patch.object(self.app, "attach_gst").start()
        # Every check the loop makes, with the silence it saw at that moment.
        self.checked = []
        real_check = self.app.check_receiver_link

        def spy(now_ms=None):
            link = real_check(now_ms)
            self.checked.append((link, self.app.uart_silence_ms(now_ms)))
            return link
        mock.patch.object(self.app, "check_receiver_link", side_effect=spy).start()

    async def run_loop(self, clock, uart, sleep_ms=None, sleep=None, period=None):
        patches = [mock.patch.object(gnss.time, "ticks_ms", clock.ticks_ms)]
        if period:
            patches.append(mock.patch.object(gnss.time, "ticks_diff",
                                             side_effect=wrapping_diff(period)))
        if sleep_ms:
            patches.append(mock.patch.object(gnss.asyncio, "sleep_ms", sleep_ms))
        if sleep:
            patches.append(mock.patch.object(gnss.asyncio, "sleep", sleep))
        for patcher in patches:
            patcher.start()
        try:
            handler = gnss.GNSSHandler(Queue())
            handler.uart = uart
            await asyncio.wait_for(handler.run(), 5)
        finally:
            for patcher in reversed(patches):
                patcher.stop()
        return handler

    def warnings(self):
        return [call for call in self.levels("WARN") if "Receiver UART silent" in call.args[2]]

    def assert_fresh_heartbeat(self, clock):
        self.assertLessEqual(clock.peek() - self.app.heartbeat["gnss"], 1100)
        with mock.patch.object(state.time, "ticks_ms", clock.peek):
            self.assertIsNone(state.stale_heartbeat(60))

    async def test_advancing_clock_during_reads_never_reports_silence(self):
        for start, period in ((0, None), ((1 << 16) - 500, 1 << 16)):
            with self.subTest(start=start, period=period):
                self.use_fresh_app()
                clock = Clock(start=start, period=period)

                class UART:
                    # Sending the GST request takes time on the wire as well.
                    def write(self, data): clock.advance(40); return len(data)
                    def any(self): return len(CHUNK)
                    def read(self): clock.advance(3); return CHUNK

                sleeper = StopAfter(self.stop, clock, 1, lambda s: s.calls >= 2000)
                await self.run_loop(clock, UART(), sleep_ms=sleeper, period=period)
                self.assertGreaterEqual(len(self.checked), 2000)
                self.assertTrue(all(link == "ok" for link, _ in self.checked))
                self.assertLess(max(silence for _, silence in self.checked), 1000)
                self.assertGreater(self.app.stats["gst_enable_attempts"], 1)
                self.assertEqual(self.warnings(), [])

    async def test_starved_loop_with_buffered_bytes_does_not_warn(self):
        clock = Clock(start=10000)
        self.app.start_receiver_watch(0)
        self.app.note_uart_data(0)
        self.assertEqual(self.app.check_receiver_link(0), "ok")
        uart = gnss.UART()
        uart.rx.extend((GGA + "\r\n").encode())
        sleeper = StopAfter(self.stop, clock, 1, lambda s: True)
        await self.run_loop(clock, uart, sleep_ms=sleeper)
        self.assertEqual(self.checked[-1][0], "ok")
        self.assertEqual(self.app.receiver_link(clock.peek()), "ok")
        self.assertEqual(self.levels("WARN"), [])

    async def test_empty_reads_do_not_count_as_data(self):
        clock = Clock(start=0)
        reads = []

        class UART:
            def write(self, data): return len(data)
            def any(self): return 5
            def read(self):
                reads.append(clock.peek())
                return None if len(reads) % 2 else b""

        sleeper = StopAfter(self.stop, clock, 100, lambda s: clock.peek() >= 5000)
        await self.run_loop(clock, UART(), sleep_ms=sleeper)
        self.assertEqual(self.app.receiver_link(clock.peek()), "silent")
        self.assertEqual(len(self.warnings()), 1)
        self.assertGreater(self.app.stats["uart_rx_reads"], 0)
        self.assertEqual(self.app.stats["uart_rx_bytes"], 0)
        # The read stamp keeps its meaning: every read, empty or not.
        self.assertGreaterEqual(self.app.stats["uart_last_read_ticks_ms"], 4500)

    async def test_repeated_read_errors_still_report_silence(self):
        for failing in ("any", "read"):
            with self.subTest(failing=failing):
                self.use_fresh_app()
                clock = Clock(start=0)

                class UART:
                    def write(self, data): return len(data)
                    def any(self):
                        if failing == "any":
                            raise OSError(5)
                        return 5
                    def read(self): raise OSError(5)

                sleeper = StopAfter(self.stop, clock, 1000, lambda s: s.calls >= 6)
                await self.run_loop(clock, UART(), sleep=sleeper)
                self.assertEqual(sleeper.calls, 6)
                self.assertEqual(len(self.warnings()), 1)
                self.assertEqual(self.app.stats["task_restarts"], 0)
                self.assert_fresh_heartbeat(clock)

    async def test_rate_window_closes_without_reads_and_during_read_errors(self):
        # A rate measured while data flowed must fall to 0 when reads stop
        # delivering, whether the UART is quiet or every access raises.
        for mode in ("quiet", "any", "read"):
            with self.subTest(mode=mode):
                self.use_fresh_app()
                clock = Clock(start=0)
                seen = {}

                class UART:
                    def write(self, data): return len(data)
                    def flowing(self):
                        if clock.peek() < 1500:
                            return True
                        seen.setdefault("rate", gnss.app.stats["uart_rx_rate_bytes_sec"])
                        return False
                    def any(self):
                        if self.flowing():
                            return len(CHUNK)
                        if mode == "any":
                            raise OSError(5)
                        return 0 if mode == "quiet" else 5
                    def read(self):
                        if self.flowing():
                            return CHUNK
                        raise OSError(5)

                def done(_):
                    return clock.peek() >= 6000
                sleeper = StopAfter(self.stop, clock, 100, done)
                waiter = StopAfter(self.stop, clock, 1000, done)
                await self.run_loop(clock, UART(), sleep_ms=sleeper, sleep=waiter)
                self.assertGreater(seen["rate"], 0)
                self.assertEqual(self.app.stats["uart_rx_rate_bytes_sec"], 0)
                self.assertGreaterEqual(self.app.stats["uart_rx_rate_window_ms"], 1000)
                self.assertEqual(len(self.warnings()), 1)

    async def test_gnss_loop_reports_silence_without_restart(self):
        clock = Clock(start=0)
        sleeper = StopAfter(self.stop, clock, 100, lambda s: clock.peek() >= 10000)
        await self.run_loop(clock, gnss.UART(), sleep_ms=sleeper)
        self.assertEqual(self.app.receiver_link(clock.peek()), "silent")
        self.assertEqual(len(self.warnings()), 1)
        self.assertEqual(self.app.stats["task_restarts"], 0)
        self.assert_fresh_heartbeat(clock)


if __name__ == "__main__":
    unittest.main()
