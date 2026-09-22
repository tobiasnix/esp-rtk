# SPDX-License-Identifier: AGPL-3.0-only
"""Self-healing: supervisor, backoff, heartbeats, and boot counter."""
import asyncio
import json
import os
import tempfile
import unittest

from support import cfg, state


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


class TestErrorLogRotation(unittest.TestCase):
    """The error log must not complete the flash."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="esp-rtk-log-")
        self.cwd = os.getcwd()
        os.chdir(self.tmp)
        self.saved = dict(cfg.CONFIG)
        cfg.CONFIG["log_level"] = "DEBUG"

    def tearDown(self):
        os.chdir(self.cwd)
        cfg.CONFIG.clear()
        cfg.CONFIG.update(self.saved)

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
