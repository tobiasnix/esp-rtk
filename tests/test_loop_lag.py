# SPDX-License-Identifier: AGPL-3.0-only
"""Event-loop lag ticker: per-tick lag, boot maximum, recorder window maximum."""
import asyncio
import inspect
import unittest
from unittest import mock

from support import main, state, cfg


class TestLoopLag(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for patcher in (mock.patch.dict(state.app.stats, {"loop_lag_ms": 0, "loop_lag_max_ms": 0}),
                        mock.patch.object(state.app, "_loop_lag_window_max_ms", None)):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def run_ticker(self, intervals, start=1000, period=None):
        clock = [start]
        stop = asyncio.Event()

        async def sleep_ms(ms):
            self.assertEqual(ms, 100)
            clock[0] += intervals.pop(0)
            if not intervals:
                stop.set()

        patches = [mock.patch.object(main, "shutdown_event", stop),
                   mock.patch.object(main.asyncio, "sleep_ms", sleep_ms, create=True),
                   mock.patch.object(main.time, "ticks_ms",
                                     lambda: clock[0] % period if period else clock[0], create=True)]
        if period:
            patches.append(mock.patch.object(
                main.time, "ticks_diff",
                lambda a, b: (a - b + period // 2) % period - period // 2, create=True))
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        await main.loop_lag_task()

    def test_note_clamps_and_keeps_boot_maximum(self):
        state.app.note_loop_lag(-5)
        self.assertEqual(state.app.stats["loop_lag_ms"], 0)
        self.assertEqual(state.app.take_loop_lag_window_max(), 0)
        state.app.note_loop_lag(700); state.app.note_loop_lag(20)
        self.assertEqual((state.app.stats["loop_lag_ms"], state.app.stats["loop_lag_max_ms"]), (20, 700))
        self.assertEqual(state.app.take_loop_lag_window_max(), 700)
        self.assertEqual(state.app.stats["loop_lag_max_ms"], 700)

    def test_window_is_none_without_a_tick(self):
        self.assertIsNone(state.app.take_loop_lag_window_max())
        state.app.note_loop_lag(30); state.app.take_loop_lag_window_max()
        self.assertIsNone(state.app.take_loop_lag_window_max())

    async def test_ticker_reports_interval_minus_tick(self):
        await self.run_ticker([100, 130, 100, 1600, 90])
        self.assertEqual((state.app.stats["loop_lag_max_ms"], state.app.stats["loop_lag_ms"]), (1500, 0))
        self.assertEqual(state.app.take_loop_lag_window_max(), 1500)

    async def test_ticker_survives_tick_wrap(self):
        await self.run_ticker([130, 100], start=(1 << 30) - 50, period=1 << 30)
        self.assertEqual(state.app.stats["loop_lag_max_ms"], 30)

    def test_ticker_runs_in_normal_and_safe_mode_but_is_not_watched(self):
        self.assertEqual(inspect.getsource(main.main).count('supervise("loop_lag", loop_lag_task)'), 2)
        self.assertNotIn("loop_lag", cfg.WATCHED_TASKS)


if __name__ == "__main__":
    unittest.main()
