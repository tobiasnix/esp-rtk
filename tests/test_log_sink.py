# SPDX-License-Identifier: AGPL-3.0-only
"""Rate limit of the persistent log sink: a flood of WARN/ERROR lines from one
source costs a bounded number of flash appends, other sources keep their own
budget, and suppressed lines show up as a count without their text."""
import os
import re
import shutil
import tempfile
import unittest
from unittest import mock

from support import cfg
from source_checks import called_names, source_tree
import cfg_logging
import cfg_store

SUMMARY = r"----- (\d+) more (\w+ \w+) lines suppressed \(rate limit\)"


class TestRateLimit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="esp-rtk-sink-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "errors.log")
        self.now = [0]
        self.logger = cfg_logging.Logger(
            {"log_level": "INFO", "log_mute": ""}, cfg.SEVERITIES,
            error_file=self.path, clock=lambda: self.now[0])
        self.appends = []
        original = self.logger.append_file

        def counting(line):
            self.appends.append((self.now[0], line))
            return original(line)

        self.logger.append_file = counting
        patcher = mock.patch("builtins.print")
        patcher.start()
        self.addCleanup(patcher.stop)

    def log(self, sev, tag, message):
        self.logger.log(sev, tag, message)

    def summaries(self, text):
        return re.findall(SUMMARY, text)

    def message_lines(self, text):
        return [line for line in text.splitlines() if "-----" not in line]

    def drain(self, tag="HTTP", count=7):
        for i in range(count):
            self.log("WARN", tag, "drain %d" % i)

    def test_thousand_varying_warn_lines_in_ten_seconds_cost_at_most_ten_appends(self):
        for i in range(1000):
            self.now[0] = i * 10
            self.log("WARN", "HTTP", "Request rejected: client %d" % i)
        self.assertLessEqual(len(self.appends), 10)
        self.now[0] = 20000
        text = self.logger.read_error()
        kept = text.count("Request rejected: client")
        self.assertEqual(kept, cfg_logging.LOG_RATE_BURST)
        found = self.summaries(text)
        self.assertTrue(found)
        self.assertEqual(sum(int(n) for n, _ in found) + kept, 1000)
        self.assertEqual({key for _, key in found}, {"WARN HTTP"})

    def test_each_source_has_its_own_budget(self):
        sources = (("WARN", "HTTP"), ("WARN", "GNSS"), ("ERROR", "HTTP"))
        for i in range(1000):
            self.now[0] = i * 10
            sev, tag = sources[i % len(sources)]
            self.log(sev, tag, "event %d" % i)
        for sev, tag in sources:
            prefix = " %-5s %-5s " % (sev, tag)
            summary = "more %s %s lines" % (sev, tag)
            own = [line for _, line in self.appends
                   if prefix in line or summary in line]
            self.assertLessEqual(len(own), 10, (sev, tag))
        text = self.logger.read_error()
        for index, (sev, tag) in enumerate(sources):
            self.assertIn("%-5s %-5s event %d\n" % (sev, tag, index), text)

    def test_flood_does_not_hide_another_source(self):
        for i in range(1000):
            self.log("WARN", "HTTP", "flood %d" % i)
        self.log("WARN", "NTRIP", "Caster unreachable")
        self.assertIn("Caster unreachable", self.logger.read_error())

    def test_any_ten_second_window_stays_within_ten_appends(self):
        for i in range(12000):
            self.now[0] = i * 5
            self.log("WARN", "HTTP", "flood %d" % i)
            if self.now[0] % 1000 == 0:
                self.logger.read_error()
        stamps = [ts for ts, _ in self.appends]
        for t in stamps:
            window = [ts for ts in stamps if t <= ts < t + 10000]
            self.assertLessEqual(len(window), 10, t)

    def test_reads_during_a_flood_stay_within_the_budget(self):
        for i in range(1000):
            self.now[0] = i * 10
            self.log("WARN", "HTTP", "flood %d" % i)
            self.logger.read_entries(limit=20)
        self.assertLessEqual(len(self.appends), 10)

    def test_tokens_refill_one_per_interval(self):
        for i in range(7):
            self.log("WARN", "HTTP", "line %d" % i)
        self.assertEqual(len(self.appends), 6)
        self.now[0] = cfg_logging.LOG_RATE_REFILL_MS
        self.log("WARN", "HTTP", "line 7")
        summary, line = self.appends[-2][1], self.appends[-1][1]
        self.assertIn("----- 1 more WARN HTTP lines suppressed (rate limit)", summary)
        self.assertIn("line 7", line)

    def test_negative_tick_difference_refills(self):
        self.now[0] = 2 ** 29
        self.drain()
        self.assertNotIn("drain 6", self.logger.read_error())
        self.now[0] = 0
        self.log("WARN", "HTTP", "after the wrap")
        self.assertIn("after the wrap", self.logger.read_error())

    def test_repeats_of_a_suppressed_line_count_as_suppressed(self):
        for i in range(6):
            self.log("WARN", "HTTP", "line %d" % i)
        for _ in range(6):
            self.log("WARN", "HTTP", "seventh")
        self.now[0] = 20000
        text = self.logger.read_error()
        self.assertNotIn("wiederholt", text)
        self.assertNotIn("seventh", text)
        self.assertEqual([int(n) for n, _ in self.summaries(text)], [6])

    def test_refused_line_is_written_once_a_token_returns(self):
        for i in range(6):
            self.log("WARN", "HTTP", "line %d" % i)
        self.log("WARN", "HTTP", "X")
        for step in range(1, 4):
            self.now[0] = step * (cfg_logging.LOG_RATE_REFILL_MS + 1000)
            self.log("WARN", "HTTP", "X")
        text = self.logger.read_error()
        written = [line for line in self.message_lines(text)
                   if line.endswith("WARN  HTTP  X")]
        self.assertEqual(len(written), 1)
        lines = text.splitlines()
        index = lines.index(written[0])
        self.assertIn("----- 1 more WARN HTTP lines suppressed (rate limit)",
                      lines[index - 1])
        self.assertIn("----- previous message 2x wiederholt", lines[index + 1])

    def test_retried_repeats_stay_within_nine_appends_per_window(self):
        for i in range(6):
            self.log("WARN", "HTTP", "line %d" % i)
        for i in range(24000):
            self.now[0] = i * 5
            self.log("WARN", "HTTP", "X")
            if self.now[0] % 1000 == 0:
                self.logger.read_error()
        stamps = [ts for ts, _ in self.appends]
        for t in stamps:
            window = [ts for ts in stamps if t <= ts < t + 10000]
            self.assertLessEqual(len(window), 9, t)
        self.assertIn("WARN  HTTP  X", self.logger.read_error())

    def test_identical_repeats_keep_the_legacy_line(self):
        for _ in range(3):
            self.log("WARN", "HTTP", "same")
        self.log("WARN", "HTTP", "other")
        lines = self.logger.read_error().splitlines()
        repeat = [i for i, line in enumerate(lines)
                  if "----- previous message 2x wiederholt" in line]
        self.assertEqual(len(repeat), 1)
        self.assertTrue(lines[repeat[0] - 1].endswith(" same"))

    def test_repeats_after_a_foreign_summary_become_suppressed(self):
        for i in range(7):
            self.log("WARN", "HTTP", "http %d" % i)
        self.log("WARN", "GNSS", "A")
        self.now[0] = 10000
        first = self.logger.read_error()
        self.assertIn("----- 1 more WARN HTTP lines suppressed (rate limit)", first)
        for _ in range(3):
            self.log("WARN", "GNSS", "A")
        self.now[0] = 20000
        text = self.logger.read_error()
        self.assertNotIn("wiederholt", text)
        self.assertIn("----- 3 more WARN GNSS lines suppressed (rate limit)", text)
        self.assertEqual(len([line for line in self.message_lines(text)
                              if line.endswith("WARN  GNSS  A")]), 1)

    def test_failed_append_never_anchors_a_repeat_line(self):
        self.log("WARN", "SYS", "A")
        with mock.patch.object(cfg_logging, "open", side_effect=OSError(5, "EIO"),
                               create=True):
            self.log("WARN", "SYS", "B")
        for _ in range(3):
            self.log("WARN", "SYS", "B")
        self.log("WARN", "SYS", "C")
        self.now[0] = 20000
        text = self.logger.read_error()
        lines = text.splitlines()
        index = [i for i, line in enumerate(lines) if line.endswith(" A")][0]
        after = lines[index + 1]
        self.assertTrue(after.endswith(" C") or re.search(SUMMARY, after), after)
        # The first repeat retries the failed line; only that written line
        # anchors the repeat count of the two repeats after it.
        self.assertIn("----- 1 more WARN SYS lines suppressed (rate limit)", text)
        repeat = [i for i, line in enumerate(lines) if "wiederholt" in line]
        self.assertEqual(len(repeat), 1)
        self.assertIn("previous message 2x wiederholt", lines[repeat[0]])
        self.assertTrue(lines[repeat[0] - 1].endswith(" B"))
        self.assertIs(self.logger.append_file("x"), True)
        with mock.patch.object(cfg_logging, "open", side_effect=OSError(5, "EIO"),
                               create=True):
            self.assertIs(self.logger.append_file("x"), False)

    def test_read_entries_classifies_summaries(self):
        self.log("WARN", "SYS", "same")
        self.log("WARN", "SYS", "same")
        self.log("WARN", "SYS", "other")
        for i in range(10):
            self.log("ERROR", "HTTP", "error %d" % i)
        for i in range(10):
            self.log("WARN", "GNSS", "warn %d" % i)
        self.now[0] = 20000
        text = self.logger.read_error()
        self.assertRegex(text, r"\] ----- 4 more ERROR HTTP lines suppressed \(rate limit\)\n")
        self.assertRegex(text, r"\] ----- 4 more WARN GNSS lines suppressed \(rate limit\)\n")
        errors = self.logger.read_entries(level="ERROR")["entries"]
        self.assertIn(("ERROR", "HTTP", "4 more ERROR HTTP lines suppressed (rate limit)"),
                      [(e["level"], e["source"], e["message"]) for e in errors])
        self.assertNotIn("GNSS", [e["source"] for e in errors])
        warns = self.logger.read_entries(level="WARN")["entries"]
        self.assertIn(("WARN", "GNSS", "4 more WARN GNSS lines suppressed (rate limit)"),
                      [(e["level"], e["source"], e["message"]) for e in warns])
        legacy = [e for e in self.logger.read_entries()["entries"]
                  if "wiederholt" in e["message"]]
        self.assertEqual(len(legacy), 1)
        self.assertEqual(legacy[0]["level"], "")
        self.assertEqual(legacy[0]["source"], "")

    def test_boot_marks_bypass_the_limit(self):
        for i in range(1000):
            self.log("WARN", "SYS", "flood %d" % i)
        self.logger.log_boot("Start X")
        self.assertIn("Start X", self.logger.read_error())

    def test_summary_never_contains_message_text(self):
        for i in range(50):
            self.log("ERROR", "WIFI", "AP password was not configured - could not be "
                     "saved, valid until the next boot: secret-%d" % i)
        self.now[0] = 20000
        text = self.logger.read_error()
        lines = text.splitlines()
        summaries = [line for line in lines if re.search(SUMMARY, line)]
        self.assertTrue(summaries)
        for line in summaries:
            self.assertNotIn("secret", line)
        self.assertEqual(len([line for line in lines if "secret-" in line]),
                         cfg_logging.LOG_RATE_BURST)

    def test_every_flash_write_goes_through_append_file(self):
        self.logger.append_file = lambda line: None
        for i in range(100):
            self.log("WARN", "HTTP", "varying %d" % i)
        for _ in range(100):
            self.log("WARN", "HTTP", "identical")
        self.now[0] = 20000
        self.logger.read_error()
        self.logger.read_entries()
        self.assertFalse(os.path.exists(self.path))

    def test_reset_error_state_clears_budgets(self):
        self.drain()
        self.assertNotIn("drain 6", self.logger.read_error())
        self.logger.reset_error_state()
        self.assertEqual(self.logger.buckets, {})
        self.log("WARN", "HTTP", "after reset")
        self.assertIn("after reset", self.logger.read_error())


class FakeRtc:
    """RTC.memory() without offset: stores a copy, counts writes, 2048 B limit."""

    def __init__(self, data=b""):
        self.data, self.writes = data, 0

    def memory(self, data=None):
        if data is None:
            return self.data
        if len(data) > 2048:
            raise ValueError("RTC memory is limited to 2048 bytes")
        self.data = bytes(data)
        self.writes += 1


class TestRtcBuffer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="esp-rtk-rtc-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.logger = cfg_logging.Logger(
            {"log_level": "INFO", "log_mute": ""}, cfg.SEVERITIES,
            error_file=os.path.join(self.tmp, "errors.log"), clock=lambda: 0)
        self.fake = FakeRtc()
        self.logger.rtc = self.fake
        self.logger.adopt()
        patcher = mock.patch("builtins.print")
        patcher.start()
        self.addCleanup(patcher.stop)

    def mirror(self):
        return "\n".join(self.logger.lines).encode()

    def test_buffer_mirrors_lines_with_non_ascii_text(self):
        for i in range(300):
            self.logger.log("INFO", "SYS", "Line %d Gr\u00f6\u00dfe \u00e4 \u00f6 \u00fc %s" % (i, "x" * (i % 70)))
            self.assertEqual(self.fake.data, self.mirror())
            self.assertEqual(bytes(self.logger._rtc), self.mirror())
            self.assertLessEqual(len(self.fake.data), 1900)

    def test_trim_happens_in_one_step_to_three_quarters(self):
        trimmed = False
        for i in range(40):
            before = len(self.logger.lines)
            self.logger.log("INFO", "SYS", "%03d %s" % (i, "y" * 80))
            if len(self.logger.lines) <= before:
                trimmed = True
                break
        self.assertTrue(trimmed)
        self.assertLessEqual(len(self.logger._rtc), 1900 * 3 // 4)
        count = len(self.logger.lines)
        for i in range(4):
            self.logger.log("INFO", "SYS", "%03d %s" % (i, "z" * 80))
        self.assertEqual(len(self.logger.lines), count + 4)

    def test_one_rtc_write_per_line(self):
        for i in range(50):
            self.logger.log("INFO", "SYS", "line %d" % i)
        self.assertEqual(self.fake.writes, 50)

    def test_append_does_not_rejoin_lines(self):
        self.assertNotIn("join", called_names(source_tree(cfg_logging.Logger.append)))

    def test_session_start_follows_trimming(self):
        self.fake.data = b"old A\nold B"
        self.logger.adopt()
        for i in range(1, 4):
            self.logger.log("INFO", "SYS", "new %d" % i)
        session = self.logger.lines[self.logger.start:]
        self.assertEqual(len(session), 3)
        for i, line in enumerate(session, 1):
            self.assertTrue(line.endswith("new %d" % i), line)
        for i in range(200):
            self.logger.log("INFO", "SYS", "%03d %s" % (i, "q" * 60))
        session = self.logger.lines[self.logger.start:]
        self.assertTrue(session[-1].endswith("199 " + "q" * 60))
        self.assertFalse([line for line in self.logger.lines if "old A" in line])
        self.assertGreaterEqual(self.logger.start, 0)

    def test_clear_rtc_then_append(self):
        for i in range(5):
            self.logger.log("INFO", "SYS", "line %d" % i)
        self.logger.previous = "old"
        self.logger.clear_rtc()
        self.assertEqual(self.fake.data, b"")
        self.assertEqual(self.logger.lines, [])
        self.assertEqual(self.logger.start, 0)
        self.assertEqual(bytes(self.logger._rtc), b"")
        self.assertEqual(self.logger.previous, "old")
        self.logger.append("x")
        self.assertTrue(self.fake.data.endswith(b"x"))
        self.assertNotIn(b"\n", self.fake.data)
        self.assertEqual(len(self.logger.lines), 1)
        self.assertEqual(self.logger.lines[self.logger.start:], self.logger.lines)

    def test_start_new_then_append(self):
        for i in range(5):
            self.logger.log("INFO", "SYS", "line %d" % i)
        before = self.fake.data
        self.logger.start_new()
        self.assertEqual(self.fake.data, before)
        self.assertEqual(self.logger.lines, [])
        self.assertEqual(bytes(self.logger._rtc), b"")
        self.logger.append("y")
        self.assertTrue(self.fake.data.endswith(b"y"))
        self.assertNotIn(b"\n", self.fake.data)
        self.assertEqual(self.fake.data, self.mirror())

    def test_migration_path_clears_history(self):
        for i in range(5):
            self.logger.log("INFO", "SYS", "history %d" % i)
        marker = os.path.join(self.tmp, "migrated")
        self.assertTrue(cfg_store.migrate_runtime_history(marker, (), self.logger))
        self.logger.append("fresh")
        self.assertEqual(self.fake.data, b"fresh")

    def test_overlong_single_line_is_kept_alone(self):
        self.logger.append("a" * 2100)
        self.assertEqual(len(self.logger.lines), 1)
        self.logger.append("b" * 2100)
        self.assertEqual(len(self.logger.lines), 1)
        self.assertEqual(bytes(self.logger._rtc), self.mirror())

    def test_without_rtc_nothing_is_mirrored(self):
        self.logger.rtc = None
        self.logger.adopt()
        self.logger.append("z")
        self.assertEqual(self.logger.lines, [])
        self.assertEqual(bytes(self.logger._rtc), b"")


if __name__ == "__main__":
    unittest.main()
