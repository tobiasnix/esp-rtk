# SPDX-License-Identifier: AGPL-3.0-only
"""Self-healing: supervisor, backoff, heartbeats, and boot counter."""
import asyncio
import inspect
import itertools
import json
import os
import tempfile
import unittest
from unittest import mock

import support
from support import cfg, state, web, fanout, main
import cfg_boot
import cfg_logging
import cfg_store
import measurement
from source_checks import called_names, imported_modules, source_tree
from test_config import record_file_events


class TestBackoff(unittest.TestCase):
    """Fixed 5-s clock said: If the password is wrong, the client hammers eternally against the caster and risks an IP lock."""

    def test_first_attempt_uses_the_base(self):
        self.assertEqual(cfg.backoff_delay(0, 5, 300), 5)
        self.assertEqual(cfg.backoff_delay(1, 5, 300), 5)

    def test_verdoppelt_sich(self):
        self.assertEqual(cfg.backoff_delay(2, 5, 300), 10)
        self.assertEqual(cfg.backoff_delay(3, 5, 300), 20)
        self.assertEqual(cfg.backoff_delay(4, 5, 300), 40)

    def test_deckel_greift(self):
        self.assertEqual(cfg.backoff_delay(10, 5, 300), 300)

    def test_very_many_attempts_do_not_overflow(self):
        # 2**10000 would be expensive to death on the device.
        self.assertEqual(cfg.backoff_delay(10000, 5, 300), 300)

    def test_negativer_versuch(self):
        self.assertEqual(cfg.backoff_delay(-1, 5, 300), 5)


class TestResetCause(unittest.TestCase):
    def test_bekannte_ursachen_haben_namen(self):
        self.assertEqual(cfg.reset_cause_name(1), "PWRON")
        self.assertEqual(cfg.reset_cause_name(3), "WDT")

    def test_unknown_cause_remains_legible(self):
        self.assertIn("99", cfg.reset_cause_name(99))


class TestBootCounter(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="esp-rtk-boot-")
        self.cwd = os.getcwd()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self.cwd)

    def test_first_boot(self):
        self.assertEqual(cfg.bump_boot_count(), 1)

    def test_counts_high(self):
        self.assertEqual(cfg.bump_boot_count(), 1)
        self.assertEqual(cfg.bump_boot_count(), 2)
        self.assertEqual(cfg.bump_boot_count(), 3)

    def test_broken_file_starts_all_over_again(self):
        with open(cfg.BOOTCOUNT_FILE, "w") as fh:
            fh.write("not json")
        self.assertEqual(cfg.bump_boot_count(), 1)

    def test_reset(self):
        cfg.bump_boot_count()
        cfg.bump_boot_count()
        cfg.reset_boot_count()
        self.assertEqual(cfg.bump_boot_count(), 1)

    def test_safe_mode_only_from_the_threshold(self):
        for expected in range(1, cfg.SAFE_MODE_BOOTS):
            self.assertFalse(cfg.safe_mode_active(expected), expected)
        self.assertTrue(cfg.safe_mode_active(cfg.SAFE_MODE_BOOTS))
        self.assertTrue(cfg.safe_mode_active(cfg.SAFE_MODE_BOOTS + 3))


class TestHeartbeat(unittest.TestCase):
    def setUp(self):
        state.app.heartbeat.clear()

    def tearDown(self):
        state.app.heartbeat.clear()

    def test_fresh_beat_is_healthy(self):
        state.app.beat("gnss")
        self.assertIsNone(state.stale_heartbeat(timeout_sec=60))

    def test_old_stroke_is_reported(self):
        import time
        state.app.heartbeat["gnss"] = time.ticks_ms() - 90_000
        self.assertEqual(state.stale_heartbeat(timeout_sec=60), "gnss")

    def test_without_strikes_no_message(self):
        # Before the first strike, the watchdog must not trigger.
        self.assertIsNone(state.stale_heartbeat(timeout_sec=60))

    def test_report_the_eldest(self):
        import time
        jetzt = time.ticks_ms()
        state.app.heartbeat["gnss"] = jetzt
        state.app.heartbeat["ntrip"] = jetzt - 120_000
        self.assertEqual(state.stale_heartbeat(timeout_sec=60), "ntrip")


class TestSupervisor(unittest.TestCase):
    """An exception in a task must not entrain the entire program via gather()."""

    def setUp(self):
        state.shutdown_event.clear()
        state.app.stats["task_restarts"] = 0

    def tearDown(self):
        state.shutdown_event.clear()

    def lauf(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_cleanly_completed_task_will_not_restart(self):
        runs = []

        async def einmal():
            runs.append(1)

        self.lauf(state.supervise("test", einmal, restart_delay=0))
        self.assertEqual(len(runs), 1)

    def test_the_dead_task_is_restarted(self):
        runs = []

        async def stirbt_zweimal():
            runs.append(1)
            if len(runs) < 3:
                raise RuntimeError("kaputt")

        self.lauf(state.supervise("test", stirbt_zweimal, restart_delay=0))
        self.assertEqual(len(runs), 3)
        self.assertEqual(state.app.stats["task_restarts"], 2)

    def test_restart_is_logged(self):
        async def stirbt():
            state.shutdown_event.set()
            raise ValueError("absicht")

        before = len(state.app.errors)
        self.lauf(state.supervise("test", stirbt, restart_delay=0))
        self.assertGreater(len(state.app.errors), before)
        self.assertEqual(state.app.errors[-1]["source"], "test")

    def test_shutdown_ends_the_loop(self):
        runs = []

        async def always_crashes():
            runs.append(1)
            if len(runs) >= 2:
                state.shutdown_event.set()
            raise RuntimeError("kaputt")

        self.lauf(state.supervise("test", always_crashes, restart_delay=0))
        self.assertEqual(len(runs), 2)

    def test_cancelled_is_passed_through(self):
        async def wird_abgebrochen():
            raise asyncio.CancelledError()

        with self.assertRaises(asyncio.CancelledError):
            self.lauf(state.supervise("test", wird_abgebrochen, restart_delay=0))


class WrapClock:
    """MicroPython-like ticks: wrap at 2**30 ms, ticks_diff is signed."""
    PERIOD = 1 << 30

    def __init__(self):
        self.now = 0

    def ticks_ms(self):
        return self.now % self.PERIOD

    def ticks_diff(self, a, b):
        half = self.PERIOD // 2
        return ((a - b + half) % self.PERIOD) - half


class _SupervisorIsolation(unittest.TestCase):
    """Saves and restores the shared state that supervisor tests touch."""

    def setUp(self):
        app = state.app
        self._saved = (app._uptime_at, app._uptime_s, app._uptime_rem_ms,
                       list(app.errors),
                       app.stats["task_restarts"], dict(app.heartbeat))
        state.shutdown_event.clear()
        app.stats["task_restarts"] = 0
        app.heartbeat.clear()

    def tearDown(self):
        app = state.app
        (app._uptime_at, app._uptime_s, app._uptime_rem_ms, errors, restarts,
         beats) = self._saved
        app.errors[:] = errors
        app.stats["task_restarts"] = restarts
        app.heartbeat.clear()
        app.heartbeat.update(beats)
        state.shutdown_event.clear()


class TestSupervisorBackoff(_SupervisorIsolation):
    """Crash loops are throttled from 2 s to 60 s; a stable minute resets the backoff."""

    def setUp(self):
        super().setUp()
        patcher = support.patch_module_global("state", "log")      # keeps the test output quiet
        self.log = patcher.start()
        self.addCleanup(patcher.stop)

    def run_supervised(self, runtimes, restart_delay=2, name="test", beat=False,
                       on_wait=None):
        """Run supervise() on a fake clock; return the waits it requested."""
        clock = WrapClock()
        delays = []
        self.stamps = []
        runs = []

        async def fake_sleep(sec):
            delays.append(sec)
            clock.now += int(sec * 1000)
            if on_wait is not None:
                on_wait()

        async def task():
            i = len(runs)
            runs.append(1)
            if beat:
                state.app.beat(name)
                self.stamps.append(state.app.heartbeat[name])
            left = runtimes[i]
            while left > 0:
                step = min(left, 3_600_000)
                clock.now += step
                left -= step
                state.app.uptime_seconds()      # what watchdog_task does
            if i == len(runtimes) - 1:
                state.shutdown_event.set()
            raise RuntimeError("boom")

        with support.patch_module_global("state", "time", clock):
            state.app._uptime_at, state.app._uptime_s, state.app._uptime_rem_ms = 0, 0, 0
            with mock.patch.object(state.asyncio, "sleep", fake_sleep):
                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(
                        state.supervise(name, task, restart_delay=restart_delay))
                finally:
                    loop.close()
        return delays

    def test_crash_loop_backs_off_from_two_to_sixty_seconds(self):
        delays = self.run_supervised([0] * 8)
        self.assertEqual(delays, [2, 4, 8, 16, 32, 60, 60])
        self.assertEqual(state.app.stats["task_restarts"], 8)

    def test_backoff_resets_after_a_stable_run(self):
        delays = self.run_supervised([0, 0, 0, 60000, 0, 0])
        self.assertEqual(delays, [2, 4, 8, 2, 4])
        self.assertEqual(state.app.stats["task_restarts"], 6)

    def test_a_run_just_below_sixty_seconds_keeps_the_backoff(self):
        delays = self.run_supervised([0, 0, 59999, 0])
        self.assertEqual(delays, [2, 4, 8])

    def test_stability_survives_a_tick_wraparound(self):
        # 7 days exceed half the tick period; ticks_diff over the run would flip sign.
        delays = self.run_supervised([0, 0, 0, 7 * 24 * 3600 * 1000, 0, 0])
        self.assertEqual(delays, [2, 4, 8, 2, 4])

    def test_zero_restart_delay_never_waits(self):
        # backoff_delay would return the cap from attempt 22 on, even for base 0.
        delays = self.run_supervised([0] * 23, restart_delay=0)
        self.assertEqual(delays, [0] * 22)

    def test_crash_log_names_the_wait(self):
        self.run_supervised([0, 0, 0])
        messages = [c.args[2] for c in self.log.call_args_list
                    if c.args[0] == "ERROR"]
        self.assertIn("Task test crashed: boom; restart in 4 s", messages)

    def test_watched_task_wait_stays_below_the_heartbeat_deadline(self):
        findings = []
        with mock.patch.dict(state.CONFIG, {"heartbeat_timeout_sec": 60}):
            delays = self.run_supervised(
                [0] * 8, name="gnss", beat=True,
                on_wait=lambda: findings.append(state.stale_heartbeat(60)))
        self.assertEqual(delays, [2, 4, 8, 16, 30, 30, 30])
        self.assertEqual(findings, [None] * 7)

    def test_disabled_heartbeat_check_keeps_the_sixty_second_cap(self):
        with mock.patch.dict(state.CONFIG, {"heartbeat_timeout_sec": 0}):
            delays = self.run_supervised([0] * 8, name="gnss", beat=True)
        self.assertEqual(delays, [2, 4, 8, 16, 32, 60, 60])

    def test_supervisor_never_beats_for_the_task(self):
        checks = []
        self.run_supervised(
            [0] * 4, name="gnss", beat=True,
            on_wait=lambda: checks.append(
                state.app.heartbeat["gnss"] == self.stamps[-1]))
        self.assertEqual(checks, [True] * 3)


class FakeServer:
    def __init__(self):
        self.closed = False
        self.waited = False

    def close(self):
        self.closed = True

    async def wait_closed(self):
        self.waited = True


class TestServerTasksRestart(_SupervisorIsolation):
    """A server failure must reach supervise(); a normal return would leave the service down."""

    MAX_TICKS = 20          # safety stop so a broken implementation cannot spin forever

    def setUp(self):
        super().setUp()
        for module in (state, web, fanout):
            patcher = mock.patch.object(module, "log")     # keeps the test output quiet
            patched = patcher.start()
            self.addCleanup(patcher.stop)
            if module is fanout:
                self.fanout_log = patched
        self.tick = 0
        self.on_tick = lambda tick: None

    def serve(self, name, task, start_server):
        """Run supervise() around task; the server wait loop (sleep 1) calls on_tick."""
        async def fake_sleep(sec):
            if sec == 1:
                self.tick += 1
                if self.tick > self.MAX_TICKS:
                    state.shutdown_event.set()
                else:
                    self.on_tick(self.tick)

        self.start_server = mock.AsyncMock(side_effect=start_server)
        with mock.patch.object(web.asyncio, "start_server", self.start_server), \
                mock.patch.object(web.asyncio, "sleep", fake_sleep):
            asyncio.run(state.supervise(name, task, restart_delay=0))

    def ports(self):
        return [c.args[2] for c in self.start_server.call_args_list]

    # --- portal -----------------------------------------------------------

    def test_http_server_failure_is_restarted(self):
        srv = FakeServer()
        seen = []

        def start(handler, host, port):
            if self.start_server.call_count == 1:
                raise OSError(112, "EADDRINUSE")
            return srv

        def on_tick(tick):
            seen.append(srv.closed)
            if tick == 2:
                state.shutdown_event.set()

        self.on_tick = on_tick
        self.serve("http", web.http_server_task, start)
        self.assertEqual(self.start_server.call_count, 2)
        self.assertIs(self.start_server.call_args_list[1].args[0], web.handle_http_client)
        self.assertEqual(state.app.stats["task_restarts"], 1)
        self.assertEqual(state.app.errors[-1]["source"], "http")
        self.assertEqual(seen, [False, False])
        self.assertTrue(srv.closed and srv.waited)

    def test_http_loop_failure_closes_the_listener_before_restart(self):
        a, b = FakeServer(), FakeServer()
        at_restart = []

        def start(handler, host, port):
            if self.start_server.call_count == 1:
                return a
            at_restart.append((a.closed, a.waited))
            return b

        def on_tick(tick):
            if tick == 1:
                raise RuntimeError("loop")
            state.shutdown_event.set()

        self.on_tick = on_tick
        self.serve("http", web.http_server_task, start)
        self.assertEqual(at_restart, [(True, True)])
        self.assertEqual(state.app.stats["task_restarts"], 1)
        self.assertTrue(b.closed and b.waited)

    def test_http_shutdown_does_not_restart(self):
        srv = FakeServer()
        seen = []

        def on_tick(tick):
            seen.append(srv.closed)
            if tick == 2:
                state.shutdown_event.set()

        self.on_tick = on_tick
        self.serve("http", web.http_server_task, lambda handler, host, port: srv)
        self.assertEqual(self.start_server.call_count, 1)
        self.assertEqual(state.app.stats["task_restarts"], 0)
        self.assertEqual(seen, [False, False])
        self.assertTrue(srv.closed and srv.waited)

    # --- NMEA-TCP ---------------------------------------------------------

    def nmea_config(self, port, legacy_on, legacy_port=2947):
        return mock.patch.dict(fanout.CONFIG, {
            "nmea_tcp_port": port, "nmea_tcp_legacy_enabled": legacy_on,
            "nmea_tcp_legacy_port": legacy_port})

    def legacy_warned(self):
        return [c for c in self.fanout_log.call_args_list
                if c.args[0] == "WARN"
                and "Legacy-NMEA-Port 2947 unavailable" in c.args[2]]

    def test_disabled_nmea_tcp_returns_without_restart(self):
        with self.nmea_config(0, False):
            self.serve("nmea_tcp", fanout.nmea_tcp_task, lambda handler, host, port: FakeServer())
        self.assertEqual(self.start_server.call_count, 0)
        self.assertEqual(state.app.stats["task_restarts"], 0)
        self.assertEqual(self.tick, 0)

    def test_canonical_nmea_port_failure_is_restarted(self):
        srv = FakeServer()

        def start(handler, host, port):
            if self.start_server.call_count == 1:
                raise OSError(12, "ENOMEM")
            return srv

        self.on_tick = lambda tick: state.shutdown_event.set() if tick == 2 else None
        with self.nmea_config(10110, False):
            self.serve("nmea_tcp", fanout.nmea_tcp_task, start)
        self.assertEqual(self.ports(), [10110, 10110])
        self.assertEqual(state.app.stats["task_restarts"], 1)
        self.assertEqual(state.app.errors[-1]["source"], "nmea_tcp")

    def test_failing_legacy_port_only_warns(self):
        canon = FakeServer()
        seen = []

        def start(handler, host, port):
            if port == 2947:
                raise OSError(112, "EADDRINUSE")
            return canon

        def on_tick(tick):
            seen.append((canon.closed, len(self.legacy_warned())))
            if tick == 2:
                state.shutdown_event.set()

        self.on_tick = on_tick
        with self.nmea_config(10110, True):
            self.serve("nmea_tcp", fanout.nmea_tcp_task, start)
        self.assertEqual(seen[0], (False, 1))
        self.assertEqual(self.start_server.call_count, 2)
        self.assertEqual(state.app.stats["task_restarts"], 0)
        self.assertTrue(canon.closed and canon.waited)

    def test_legacy_only_listener_failure_does_not_restart(self):
        seen = []

        def start(handler, host, port):
            raise OSError(112, "EADDRINUSE")

        def on_tick(tick):
            seen.append(len(self.legacy_warned()))
            if tick == 2:
                state.shutdown_event.set()

        self.on_tick = on_tick
        with self.nmea_config(0, True):
            self.serve("nmea_tcp", fanout.nmea_tcp_task, start)
        self.assertEqual(seen[0], 1)
        self.assertEqual(self.start_server.call_count, 1)
        self.assertEqual(state.app.stats["task_restarts"], 0)

    def test_nmea_loop_failure_closes_both_listeners_before_restart(self):
        a1, a2, b1, b2 = (FakeServer() for _ in range(4))
        at_restart = []

        def start(handler, host, port):
            n = self.start_server.call_count
            if n == 3:
                at_restart.append((a1.closed, a1.waited, a2.closed, a2.waited))
            return (a1, a2, b1, b2)[n - 1]

        def on_tick(tick):
            if tick == 1:
                raise RuntimeError("loop")
            state.shutdown_event.set()

        self.on_tick = on_tick
        with self.nmea_config(10110, True):
            self.serve("nmea_tcp", fanout.nmea_tcp_task, start)
        self.assertEqual(at_restart, [(True, True, True, True)])
        self.assertEqual(state.app.stats["task_restarts"], 1)
        self.assertEqual(self.start_server.call_count, 4)
        self.assertEqual(self.ports(), [10110, 2947, 10110, 2947])

    def test_nmea_shutdown_does_not_restart(self):
        srv = FakeServer()
        seen = []

        def on_tick(tick):
            seen.append(srv.closed)
            if tick == 2:
                state.shutdown_event.set()

        self.on_tick = on_tick
        with self.nmea_config(10110, False):
            self.serve("nmea_tcp", fanout.nmea_tcp_task, lambda handler, host, port: srv)
        self.assertEqual(self.start_server.call_count, 1)
        self.assertEqual(state.app.stats["task_restarts"], 0)
        self.assertEqual(seen, [False, False])
        self.assertTrue(srv.closed and srv.waited)


if __name__ == "__main__":
    unittest.main()


class TestWatchedTasksOnly(unittest.TestCase):
    """Tasks without a limited waiting loop are not allowed to reset."""

    def setUp(self):
        state.app.heartbeat.clear()

    def tearDown(self):
        state.app.heartbeat.clear()

    def test_ble_sender_does_not_trigger_a_reset(self):
        # The BLE transmitter hangs on a queue that can legitimately remain empty.
        import time
        state.app.heartbeat["ble"] = time.ticks_ms() - 600_000
        self.assertIsNone(state.stale_heartbeat(timeout_sec=60))

    def test_the_monitored_amount_is_deliberately_small(self):
        self.assertEqual(set(cfg.WATCHED_TASKS), {"gnss", "ntrip", "network"})

    def test_unstarted_task_does_not_count(self):
        # Before the first strike there is no entry - no stalled task.
        self.assertIsNone(state.stale_heartbeat(timeout_sec=60))


class TestCountOnlyCrashes(unittest.TestCase):
    """Deliberate disengagement and insertion must not disengage Safe Mode."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="esp-rtk-boot2-")
        self.cwd = os.getcwd()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self.cwd)

    def test_plugging_in_resets(self):
        for _ in range(4):
            cfg.bump_boot_count(3)                    # WDT-Resets
        self.assertEqual(cfg.bump_boot_count(1), 0)   # PWRON
        self.assertEqual(cfg.bump_boot_count(3), 1)   # Serie starts new an

    def test_planned_reset_resets(self):
        cfg.bump_boot_count(3)
        cfg.bump_boot_count(3)
        self.assertEqual(cfg.bump_boot_count(5), 0)   # SOFT, z.B. nach /save

    def test_watchdog_loop_is_counted(self):
        for expected in range(1, cfg.SAFE_MODE_BOOTS + 1):
            self.assertEqual(cfg.bump_boot_count(3), expected)
        self.assertTrue(cfg.safe_mode_active(cfg.SAFE_MODE_BOOTS))

    def test_panic_reset_is_counted(self):
        self.assertEqual(cfg.bump_boot_count(2), 1)

    def test_no_information_is_counted(self):
        # Backward-compatible: always count without cause.
        self.assertEqual(cfg.bump_boot_count(), 1)


class TestPlannedRestart(unittest.TestCase):
    """machine.reset() logs on the ESP32-S3 as HARD - just like a crash. A self-discharged restart (about after /save) must therefore not count as a false start; it leaves a marker beforehand."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="esp-rtk-plan-")
        self.cwd = os.getcwd()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self.cwd)

    def test_a_marked_restart_does_not_count(self):
        cfg.bump_boot_count(3)
        cfg.bump_boot_count(3)
        cfg.mark_planned_reset()
        self.assertEqual(cfg.bump_boot_count(2), 0)

    def test_marking_applies_only_once(self):
        cfg.mark_planned_reset()
        self.assertEqual(cfg.bump_boot_count(2), 0)
        self.assertEqual(cfg.bump_boot_count(2), 1)   # next HARD reset counts

    def test_unmarked_crash_continues_to_count(self):
        cfg.bump_boot_count(3)
        self.assertEqual(cfg.bump_boot_count(2), 2)

    def test_marking_without_file_is_harmless(self):
        self.assertEqual(cfg.bump_boot_count(2), 1)


class TestBootCounterAtomic(unittest.TestCase):
    """bootcount.json decides about safe mode: a power cut while it is written
    must neither lose the count nor resurrect a stale one (#19)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="esp-rtk-bootatomic-")
        self.cwd = os.getcwd()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self.cwd)

    def write(self, name, text):
        with open(name, "w") as fh:
            fh.write(text)

    def count(self):
        with open("bootcount.json") as fh:
            return json.load(fh)["n"]

    def exists(self, name):
        return os.path.exists(name)

    def assert_no_leftovers(self, *names):
        for name in names or ("bootcount.json.tmp", "bootcount.json.previous"):
            self.assertFalse(self.exists(name), name)

    def test_count_goes_through_tmp_and_rename(self):
        with mock.patch.object(cfg_boot.os, "rename", wraps=os.rename) as renamed:
            self.assertEqual(cfg.bump_boot_count(3), 1)
            self.assertEqual(cfg.bump_boot_count(3), 2)
        self.assertIn(mock.call("bootcount.json.tmp", "bootcount.json"),
                      renamed.call_args_list)
        self.assertEqual(self.count(), 2)
        self.assert_no_leftovers()

    def test_bootcount_write_flushes_before_rename(self):
        events = record_file_events(self)
        cfg.bump_boot_count(3)
        flushed = events.index(("flush", "bootcount.json.tmp"))
        closed = events.index(("close", "bootcount.json.tmp"))
        renamed = events.index(("rename", "bootcount.json.tmp", "bootcount.json"))
        self.assertLess(flushed, closed)
        self.assertLess(closed, renamed)

    def test_interrupted_rename_keeps_previous_count(self):
        self.write("bootcount.json", '{"n": 2}')
        real_rename = os.rename

        def power_loss(source, target):
            if (source, target) == ("bootcount.json.tmp", "bootcount.json"):
                raise OSError("simulated power loss")
            return real_rename(source, target)

        with mock.patch.object(cfg_boot.os, "rename", side_effect=power_loss), \
                mock.patch.object(cfg, "log"):
            self.assertEqual(cfg.bump_boot_count(3), 3)
        self.assertEqual(self.count(), 2)
        self.assertFalse(self.exists("bootcount.json.tmp"))
        self.assertEqual(cfg.bump_boot_count(3), 3)

    def test_failed_write_keeps_file_and_logs(self):
        self.write("bootcount.json", '{"n": 2}')
        with mock.patch.object(cfg_store.ujson, "dump",
                               side_effect=OSError(28, "no space")), \
                mock.patch.object(cfg, "log") as logged:
            self.assertEqual(cfg.bump_boot_count(3), 3)
        self.assertEqual(self.count(), 2)
        self.assertFalse(self.exists("bootcount.json.tmp"))
        errors = [c for c in logged.call_args_list
                  if c.args[:2] == ("ERROR", "SYS")
                  and "Saving bootcount.json failed" in c.args[2]]
        self.assertEqual(len(errors), 1, logged.call_args_list)

    def test_power_loss_after_tmp_write_is_recovered(self):
        self.write("bootcount.json.tmp", '{"n": 3}')
        self.assertEqual(cfg.bump_boot_count(3), 4)
        self.assertEqual(self.count(), 4)
        self.assertFalse(self.exists("bootcount.json.tmp"))

    def test_power_loss_between_renames_is_recovered(self):
        self.write("bootcount.json.previous", '{"n": 2}')
        self.write("bootcount.json.tmp", '{"n": ')
        self.assertEqual(cfg.bump_boot_count(3), 3)
        self.assertEqual(self.count(), 3)
        self.assert_no_leftovers()

    def test_truncated_tmp_next_to_valid_file_is_ignored(self):
        self.write("bootcount.json", '{"n": 2}')
        self.write("bootcount.json.tmp", '{"n": 9')
        self.assertEqual(cfg.bump_boot_count(3), 3)
        self.assertEqual(self.count(), 3)
        self.assertFalse(self.exists("bootcount.json.tmp"))

    def test_empty_object_target_does_not_hide_a_valid_previous(self):
        self.write("bootcount.json", "{}")
        self.write("bootcount.json.previous", '{"n": 4}')
        self.assertEqual(cfg.bump_boot_count(3), 5)
        self.assertEqual(self.count(), 5)

    def test_invalid_n_does_not_hide_a_valid_previous(self):
        for target in ('{"n": "x"}', '{"n": true}', '{"n": -1}', '{"n": 2.5}'):
            with self.subTest(target=target):
                cfg.reset_boot_count()
                self.write("bootcount.json", target)
                self.write("bootcount.json.previous", '{"n": 4}')
                self.assertEqual(cfg.bump_boot_count(3), 5)
                self.assertEqual(self.count(), 5)

    def test_invalid_tmp_is_skipped_for_a_valid_previous(self):
        self.write("bootcount.json.tmp", "{}")
        self.write("bootcount.json.previous", '{"n": 4}')
        self.assertEqual(cfg.bump_boot_count(3), 5)
        self.assertEqual(self.count(), 5)

    def test_failed_recovery_keeps_candidates_and_skips_the_write(self):
        self.write("bootcount.json.previous", '{"n": 4}')
        real_rename = os.rename
        calls = []

        def fail_first(source, target):
            calls.append((source, target))
            if len(calls) == 1:
                raise OSError("simulated rename failure")
            return real_rename(source, target)

        with mock.patch.object(cfg_boot.os, "rename", side_effect=fail_first), \
                mock.patch.object(cfg, "_atomic_dump", wraps=cfg._atomic_dump) as dumped:
            count = cfg.bump_boot_count(3)
        self.assertEqual(count, 5)
        self.assertTrue(cfg.safe_mode_active(count))
        self.assertEqual(dumped.call_count, 0)
        with open("bootcount.json.previous") as fh:
            self.assertEqual(json.load(fh), {"n": 4})
        self.assertEqual(cfg.bump_boot_count(3), 5)

    def test_reset_removes_tmp_and_previous(self):
        for name in ("bootcount.json", "bootcount.json.tmp", "bootcount.json.previous"):
            self.write(name, '{"n": 4}')
        cfg.reset_boot_count()
        self.assert_no_leftovers("bootcount.json", "bootcount.json.tmp",
                                 "bootcount.json.previous")
        self.assertEqual(cfg.bump_boot_count(3), 1)

    def test_power_on_clears_stale_tmp_and_previous(self):
        all_files = ("bootcount.json", "bootcount.json.tmp", "bootcount.json.previous")
        for harmless in ("power on", "planned reset"):
            with self.subTest(harmless):
                cfg.reset_boot_count()
                self.write("bootcount.json.tmp", '{"n": 4}')
                self.write("bootcount.json.previous", '{"n": 4}')
                if harmless == "power on":
                    self.assertEqual(cfg.bump_boot_count(1), 0)
                else:
                    cfg.mark_planned_reset()
                    self.assertEqual(cfg.bump_boot_count(2), 0)
                self.assert_no_leftovers(*all_files)
                self.assertEqual(cfg.bump_boot_count(3), 1)

    def test_config_recovery_is_unchanged(self):
        # The config keeps the looser rule "any valid JSON object"; only the
        # boot counter demands an integer n >= 0.
        self.write("config.json", "{}")
        self.write("config.json.previous", '{"a": 1}')
        self.assertTrue(cfg_store.recover_atomic("config.json"))
        with open("config.json") as fh:
            self.assertEqual(json.load(fh), {})
        self.assertFalse(self.exists("config.json.previous"))

    def test_cfg_boot_stays_free_of_cfg_store(self):
        tree = source_tree(cfg_boot)
        self.assertNotIn("cfg_store", imported_modules(tree))
        calls = called_names(tree)
        self.assertNotIn("ujson.dump", calls)
        self.assertNotIn("recover_atomic", calls)


class TestErrorLogRotation(unittest.TestCase):
    """The error log must not complete the flash."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="esp-rtk-log-")
        self.cwd = os.getcwd()
        os.chdir(self.tmp)
        self.saved = dict(cfg.CONFIG)
        cfg.CONFIG["log_level"] = "DEBUG"
        cfg.errorlog_reset_state()
        # These tests exercise rotation, not the rate limit: every bucket
        # lookup sees one refill interval more, so no line is suppressed.
        clock = mock.patch.object(
            cfg._logger, "clock",
            side_effect=itertools.count(0, cfg_logging.LOG_RATE_REFILL_MS).__next__)
        clock.start()
        self.addCleanup(clock.stop)

    def tearDown(self):
        os.chdir(self.cwd)
        cfg.CONFIG.clear()
        cfg.CONFIG.update(self.saved)
        cfg.errorlog_reset_state()

    def belegt(self):
        gesamt = 0
        for name in (cfg.ERRORLOG_FILE, cfg.ERRORLOG_FILE + ".1"):
            try:
                gesamt += os.stat(name)[6]
            except OSError:
                pass
        return gesamt

    def test_rotates_and_remains_capped(self):
        for i in range(4000):
            cfg.log("ERROR", "SYS", "Error number %d followed by text" % i)
        self.assertLessEqual(self.belegt(), 2 * cfg.ERRORLOG_MAX_BYTES + 4096)
        self.assertTrue(os.path.exists(cfg.ERRORLOG_FILE))
        self.assertTrue(os.path.exists(cfg.ERRORLOG_FILE + ".1"))

    def test_recent_entries_survive(self):
        for i in range(4000):
            cfg.log("ERROR", "SYS", "Error number %d followed by text" % i)
        self.assertIn("3999", cfg.read_error_log())

    def test_only_warning_and_error_land_in_the_log(self):
        cfg.log("DEBUG", "SYS", "debug")
        cfg.log("INFO", "SYS", "info")
        self.assertEqual(self.belegt(), 0)
        cfg.log("WARN", "SYS", "warnung")
        self.assertGreater(self.belegt(), 0)

    def test_read_returns_oldest_first(self):
        for i in range(4000):
            cfg.log("ERROR", "SYS", "Error number %d followed by text" % i)
        text = cfg.read_error_log()
        first = text.index("Error number")
        # The file .1 is the older one and must be in the front.
        old = int(text[first:].split("Error number ")[1].split(" ")[0])
        new = int(text.rsplit("Error number ", 1)[1].split(" ")[0])
        self.assertLess(old, new)


class TestErrorLogWriteProtection(unittest.TestCase):
    """It's not the risk, it's the write frequency. A task restart loop would write the same line every two seconds -- 1800 flashes per hour for zero cognition gain.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="esp-rtk-wear-")
        self.cwd = os.getcwd()
        os.chdir(self.tmp)
        self.saved = dict(cfg.CONFIG)
        cfg.errorlog_reset_state()

    def tearDown(self):
        os.chdir(self.cwd)
        cfg.CONFIG.clear()
        cfg.CONFIG.update(self.saved)
        cfg.errorlog_reset_state()

    def count_write_operations(self, operation):
        import builtins
        echtes_open = builtins.open
        counters = {"n": 0}

        def counted(*a, **kw):
            if a and str(a[0]).startswith("errors.log") and "a" in str(kw.get("mode", a[1] if len(a) > 1 else "r")):
                counters["n"] += 1
            return echtes_open(*a, **kw)

        builtins.open = counted
        try:
            operation()
        finally:
            builtins.open = echtes_open
        return counters["n"]

    def test_repeats_are_not_written_individually(self):
        def operation():
            for _ in range(500):
                cfg.log("ERROR", "SYS", "Task gnss gestorben: kaputt")
        n = self.count_write_operations(operation)
        self.assertLessEqual(n, 3, "500 identical messages -> %d write operations" % n)

    def test_repeats_are_reported_counted(self):
        for _ in range(500):
            cfg.log("ERROR", "SYS", "Task gnss gestorben: kaputt")
        cfg.log("ERROR", "SYS", "etwas anderes")
        text = cfg.read_error_log()
        self.assertIn("499", text)
        self.assertIn("wiederholt", text)

    def test_first_message_is_immediately_in_it(self):
        cfg.log("ERROR", "SYS", "one-time error")
        self.assertIn("one-time error", cfg.read_error_log())

    def test_different_messages_are_all_written(self):
        for i in range(5):
            cfg.log("ERROR", "SYS", "Error %d" % i)
        text = cfg.read_error_log()
        for i in range(5):
            self.assertIn("Error %d" % i, text)

    def test_reading_makes_open_repetitions_visible(self):
        # Otherwise, when you look it up, you don't see that it's rattling.
        for _ in range(100):
            cfg.log("ERROR", "SYS", "es rattert")
        self.assertIn("99", cfg.read_error_log())

    def test_entry_count_is_cached_and_tracks_appends(self):
        with open(cfg.ERRORLOG_FILE + ".1", "w") as target:
            target.write("old one\nold two\n")
        with open(cfg.ERRORLOG_FILE, "w") as target:
            target.write("current")
        cfg.errorlog_reset_state()
        self.assertEqual(cfg.error_log_entries(), 3)
        cfg.log("ERROR", "SYS", "new entry")
        self.assertEqual(cfg.error_log_entries(), 4)

    def test_repetition_flush_counts_as_one_entry(self):
        cfg.log("ERROR", "SYS", "same")
        cfg.log("ERROR", "SYS", "same")
        cfg.log("ERROR", "SYS", "different")
        self.assertEqual(cfg.error_log_entries(), 3)

    def test_incremental_log_cursor_returns_only_new_entries(self):
        cfg.log("ERROR", "SYS", "first")
        initial = cfg.incremental_error_log(limit=20)
        self.assertTrue(initial["entries"])
        cfg.log("WARN", "WIFI", "second")
        delta = cfg.incremental_error_log(cursor=initial["cursor"], limit=20)
        self.assertEqual([entry["message"] for entry in delta["entries"]],
                         ["second"])
        self.assertIn(":", delta["cursor"])

    def test_boat_brand_is_always_written(self):
        cfg.log_boot("Start A")
        cfg.log_boot("Start B")
        text = cfg.read_error_log()
        self.assertIn("Start A", text)
        self.assertIn("Start B", text)


class FakeWriter:
    """Collects data written by _http_send."""

    def __init__(self):
        self.buf = bytearray()

    def write(self, data):
        self.buf.extend(data)

    def get_extra_info(self, key):
        return ("192.168.4.2", 12345)

    async def drain(self):
        pass


class TestSafeModeServices(_SupervisorIsolation):
    def test_recording_probe_is_registered_before_the_safe_mode_branch(self):
        source = inspect.getsource(main.main)
        self.assertLess(source.index("app.set_recording_probe(recording_active)"),
                        source.index("if safe:"))

    def test_fix_listener_is_registered_with_the_recording_probe(self):
        source = inspect.getsource(main.main)
        probe = "app.set_recording_probe(recording_active)"
        listener = "app.set_fix_listener(stakeout_session.session.on_fix)"
        self.assertIn(listener, source)
        # Directly after the probe: nothing but whitespace in between.
        self.assertEqual(source[source.index(probe) + len(probe):source.index(listener)].strip(), "")
        self.assertLess(source.index(listener), source.index("if safe:"))

    def test_safe_mode_registers_the_network_manager_for_the_wifi_scan(self):
        manager = mock.Mock()
        manager.request_scan.return_value = ("pending", None, None)
        started = []

        async def idle():
            pass

        def fake_supervise(name, factory, restart_delay=2):
            started.append(name)
            return idle()

        progress = dict(measurement.measurement_session.progress)
        measurement.measurement_session.progress["active"] = False
        try:
            with mock.patch.dict(state._instances, {}, clear=True), \
                    mock.patch.dict(state.app.stats), \
                    mock.patch.object(main, "bump_boot_count", return_value=cfg.SAFE_MODE_BOOTS), \
                    mock.patch.object(main, "safe_mode_active", return_value=True), \
                    mock.patch.object(main, "log_boot"), mock.patch.object(main, "log"), \
                    mock.patch.object(main, "check_access_button", mock.AsyncMock(return_value=None)), \
                    mock.patch.object(main, "previous_flight_recorder", return_value=False), \
                    mock.patch.object(main, "field_recorder", mock.Mock()), \
                    mock.patch.object(main, "NetworkManager", return_value=manager), \
                    mock.patch.object(main, "supervise", fake_supervise), \
                    mock.patch.object(state.app, "set_recording_probe") as register:
                asyncio.run(main.main())
                self.assertIs(state._instances.get("network"), manager)
                register.assert_called_once_with(web.recording_active)
                writer = FakeWriter()
                with mock.patch.object(web, "_admin_allowed", return_value=True), \
                        mock.patch.object(web.tracker, "active_line", return_value=None):
                    asyncio.run(web._handle_diagnostics_request(
                        writer, "/api/wifi-scan", "", False, {}))
        finally:
            measurement.measurement_session.progress.clear()
            measurement.measurement_session.progress.update(progress)
        response = bytes(writer.buf)
        self.assertIn(b"202 Accepted", response)
        self.assertNotIn(b"503", response)
        manager.request_scan.assert_called_once_with()
        self.assertEqual(started, ["field_diagnostics", "watchdog", "gc", "loop_lag",
                                   "network", "http", "sse", "discovery"])
