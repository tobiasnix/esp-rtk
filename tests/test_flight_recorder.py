# SPDX-License-Identifier: AGPL-3.0-only
"""Three-tier logging: RAM, RTC memory, and flash. The RTC memory of the ESP32 survives every reset except power failure - measured on the device versus a hard reset - and costs no flash wear, so it records the full history, while the flash only starters and errors go.
"""
import os
import sys
import tempfile
import unittest

from support import cfg


class TestFlightRecorder(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="esp-rtk-fs-")
        self.cwd = os.getcwd()
        os.chdir(self.tmp)
        self.saved = dict(cfg.CONFIG)
        sys.modules["machine"].RTC().memory(b"")
        cfg.flight_recorder_reset()
        cfg.errorlog_reset_state()

    def tearDown(self):
        os.chdir(self.cwd)
        cfg.CONFIG.clear()
        cfg.CONFIG.update(self.saved)

    def test_each_message_lands_in_rtc_memory(self):
        cfg.log("INFO", "WIFI", "STA connected: 192.0.2.5")
        cfg.log("INFO", "NTRIP", "Stream connected")
        text = cfg.read_flight_recorder()
        self.assertIn("STA connected", text)
        self.assertIn("Stream connected", text)

    def test_info_does_not_go_to_flash(self):
        """Exactly the point: in the RTC memory you can afford detail."""
        cfg.log("INFO", "WIFI", "STA connected: 192.0.2.5")
        self.assertIn("STA connected", cfg.read_flight_recorder())
        self.assertNotIn("STA connected", cfg.read_error_log())

    def test_errors_are_written_to_both_stores(self):
        cfg.log("ERROR", "NTRIP", "401 Unauthorized")
        self.assertIn("401", cfg.read_flight_recorder())
        self.assertIn("401", cfg.read_error_log())

    def test_remains_under_the_rtc_boundary(self):
        for i in range(500):
            cfg.log("INFO", "SYS", "Event %d followed by text" % i)
        self.assertLessEqual(len(cfg.read_flight_recorder()), cfg.RTC_LOG_MAX_BYTES)

    def test_recent_events_survive(self):
        for i in range(500):
            cfg.log("INFO", "SYS", "Ereignis %d" % i)
        text = cfg.read_flight_recorder()
        self.assertIn("Ereignis 499", text)
        self.assertNotIn("Ereignis 0 ", text)

    def test_filtered_messages_do_not_come_in(self):
        cfg.CONFIG["log_level"] = "ERROR"
        cfg.log("INFO", "SYS", "unwichtig")
        self.assertNotIn("unwichtig", cfg.read_flight_recorder())

    def test_silent_tags_don_t_come_in(self):
        cfg.CONFIG["log_mute"] = "GC"
        cfg.log("DEBUG", "GC", "Freier RAM")
        self.assertNotIn("Freier RAM", cfg.read_flight_recorder())


class TestPreviousSession(unittest.TestCase):
    """After a crash you want to see what happened BEFORE."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="esp-rtk-fs2-")
        self.cwd = os.getcwd()
        os.chdir(self.tmp)
        sys.modules["machine"].RTC().memory(b"")
        cfg.flight_recorder_reset()
        cfg.errorlog_reset_state()

    def tearDown(self):
        os.chdir(self.cwd)

    def test_it_will_be_taken_over_at_the_start(self):
        cfg.log("INFO", "GNSS", "Fix: RTK_FIXED")
        cfg.log("ERROR", "SYS", "Task gnss has no heartbeat")
        # Simulate restart: same RTC contents, new session
        cfg.flight_recorder_adopt()
        self.assertIn("RTK_FIXED", cfg.previous_flight_recorder())
        self.assertIn("has no heartbeat", cfg.previous_flight_recorder())

    def test_new_session_starts_empty(self):
        cfg.log("INFO", "SYS", "old session")
        cfg.flight_recorder_adopt()
        self.assertNotIn("old session", cfg.read_flight_recorder())

    def test_without_prior_history_it_is_empty(self):
        cfg.flight_recorder_adopt()
        self.assertEqual(cfg.previous_flight_recorder(), "")

    def test_save_writes_history_to_flash(self):
        """If the reset looked like a crash, the course should also survive a later power failure."""
        cfg.log("INFO", "GNSS", "Fix: RTK_FIXED")
        cfg.flight_recorder_adopt()
        cfg.save_flight_recorder()
        self.assertIn("RTK_FIXED", cfg.read_error_log())

    def test_securing_without_prior_history_does_not_write_anything(self):
        cfg.flight_recorder_adopt()
        cfg.save_flight_recorder()
        self.assertEqual(cfg.read_error_log(), "")

    def test_safeguarding_only_works_once(self):
        cfg.log("INFO", "SYS", "einmalig")
        cfg.flight_recorder_adopt()
        cfg.save_flight_recorder()
        cfg.save_flight_recorder()
        self.assertEqual(cfg.read_error_log().count("einmalig"), 1)


class TestOhneRtc(unittest.TestCase):
    """On firmware without RTC memory, nothing must break."""

    def test_log_funktioniert_weiter(self):
        echt = sys.modules["machine"].RTC
        sys.modules["machine"].RTC = None
        try:
            cfg.flight_recorder_reset()
            cfg.log("ERROR", "SYS", "geht trotzdem")
            self.assertEqual(cfg.read_flight_recorder(), "")
        finally:
            sys.modules["machine"].RTC = echt
            cfg.flight_recorder_reset()


if __name__ == "__main__":
    unittest.main()


class TestHistorySurvivesToolSessions(unittest.TestCase):
    """Each mpexec session takes over the RTC content and then writes the first log line of its own -- which wrote over the history -- and two queries in a row eliminated hours of history."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="esp-rtk-fs3-")
        self.cwd = os.getcwd()
        os.chdir(self.tmp)
        sys.modules["machine"].RTC().memory(b"")
        cfg.flight_recorder_reset()
        cfg.errorlog_reset_state()

    def tearDown(self):
        os.chdir(self.cwd)

    def session(self, lines):
        """Resumes a restart with subsequent log lines."""
        cfg.flight_recorder_adopt()
        for z in lines:
            cfg.log("INFO", "SYS", z)

    def test_short_session_does_not_delete_the_history(self):
        self.session(["long session A", "long session B", "long session C"])
        self.session(["Tool: configuration loaded"])
        # After the short session, the old course must still be in the RTC.
        raw = sys.modules["machine"].RTC().memory().decode()
        self.assertIn("long session A", raw)
        self.assertIn("Tool", raw)

    def test_zwei_abfragen_hintereinander(self):
        self.session(["wichtig"])
        self.session(["Abfrage 1"])
        self.session(["Abfrage 2"])
        self.assertIn("wichtig", sys.modules["machine"].RTC().memory().decode())

    def test_ongoing_session_remains_distinguishable(self):
        self.session(["old"])
        self.session(["new"])
        self.assertIn("new", cfg.read_flight_recorder())
        self.assertNotIn("old", cfg.read_flight_recorder())

    def test_previous_history_continues_to_be_retrievable(self):
        self.session(["before"])
        self.session(["jetzt"])
        self.assertIn("before", cfg.previous_flight_recorder())

    def test_old_history_is_evicted_when_space_is_low(self):
        self.session(["ganz old"])
        self.session(["x" * 200 for _ in range(30)])
        raw = sys.modules["machine"].RTC().memory().decode()
        self.assertLessEqual(len(raw), cfg.RTC_LOG_MAX_BYTES)
        self.assertNotIn("ganz old", raw)
