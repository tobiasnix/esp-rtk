# SPDX-License-Identifier: AGPL-3.0-only
"""Hot-path failures log once per transition, with a count of what was held back."""
import ast
import asyncio
import inspect
import json
import unittest
from unittest import mock

import cfg_logging
from source_checks import called_names, source_tree, string_constants
from support import cfg, fanout, net, web, gnss, state
from test_fanout import FakeWriter
from test_http import FakeWriter as HttpWriter


def make_throttle(now, **kwargs):
    return cfg_logging.LogThrottle(cfg_logging.HOT_PATH_LOG_INTERVAL_MS,
                                   clock=lambda: now[0], **kwargs)


class TestLogThrottle(unittest.TestCase):
    def test_first_hit_reports_then_counts(self):
        now = [0]
        t = make_throttle(now)
        self.assertEqual(t.hit(), 1)
        self.assertEqual([t.hit() for _ in range(99)], [0] * 99)
        now[0] = 60000
        self.assertEqual(t.hit(), 100)

    def test_keys_are_independent(self):
        t = make_throttle([0])
        self.assertEqual(t.hit("a"), 1)
        self.assertEqual(t.hit("b"), 1)

    def test_many_keys_share_the_overflow_without_losing_counts(self):
        now = [0]
        t = make_throttle(now)
        reports, total = 0, 0
        for second in range(60):
            now[0] = second * 1000
            for index in range(10):
                n = t.hit("k%d" % index)
                total += n
                reports += 1 if n else 0
        self.assertGreater(reports, 0)
        self.assertLessEqual(reports, 9)
        self.assertIsNotNone(t.overflow)
        self.assertNotIn("k8", t.entries)
        self.assertNotIn("k9", t.entries)
        now[0] = 120000
        for index in range(10):
            total += t.hit("k%d" % index)
        self.assertEqual(total + t.pending_total(), 600 + 10)

    def test_idle_keys_make_room(self):
        now = [0]
        t = make_throttle(now, max_keys=2)
        t.hit("a")
        t.hit("b")
        now[0] = 60000
        self.assertEqual(t.hit("c"), 1)
        self.assertIn("c", t.entries)
        self.assertIsNone(t.overflow)

    def test_busy_keys_are_never_dropped(self):
        now = [0]
        t = make_throttle(now, max_keys=2)
        t.hit("a")
        t.hit("b")
        now[0] = 10
        t.hit("a")
        t.hit("b")
        self.assertEqual(t.pending_total(), 2)
        now[0] = 60000
        self.assertEqual(t.hit("c"), 1)
        self.assertIn("a", t.entries)
        self.assertIn("b", t.entries)
        self.assertNotIn("c", t.entries)
        self.assertEqual(t.pending_total(), 2)

    def test_negative_tick_difference_reports(self):
        now = [2 ** 29]
        t = make_throttle(now)
        self.assertEqual(t.hit("a"), 1)
        now[0] = 0
        self.assertGreaterEqual(t.hit("a"), 1)

    def test_reset_clears_everything(self):
        now = [0]
        t = make_throttle(now, max_keys=1)
        t.hit("a")
        t.hit("b")
        t.hit("b")
        t.reset()
        self.assertEqual(t.pending_total(), 0)
        self.assertIsNone(t.overflow)
        self.assertEqual(t.hit("a"), 1)

    def test_note_format(self):
        self.assertEqual(cfg_logging.throttle_note(1), "")
        self.assertEqual(cfg_logging.throttle_note(5), " [4 more since the last report]")


class FailingWriter(FakeWriter):
    """Fails with a plain OSError (an errno 104 would become ConnectionResetError on CPython)."""

    def __init__(self):
        super().__init__(fails=False)

    def write(self, data):
        raise OSError("send failed")


class TestFanoutSendFailure(unittest.TestCase):
    def test_burst_of_failing_clients_logs_once(self):
        now = [0]

        async def fail_deliveries(count, data):
            for _ in range(count):
                session = fanout.add_client(FailingWriter())
                session.inflight = data
                self.assertFalse(await fanout._deliver(session, data))

        with mock.patch.object(fanout, "clients", []), \
                mock.patch.object(fanout, "_sessions", {}), \
                mock.patch.dict(fanout.CONFIG, {"nmea_tcp_max_clients": 64}), \
                mock.patch.dict(state.app.stats, {"tcp_write_errors": 0, "nmea_tcp_drops": 0,
                                                  "nmea_tcp_client_stats": []}), \
                mock.patch.object(fanout, "_SEND_FAILURE_LOG", make_throttle(now)), \
                mock.patch.object(fanout, "log") as logged:
            asyncio.run(fail_deliveries(50, b"$GNGGA,1\r\n"))
            logged.assert_called_once_with(
                "WARN", "GNSS", "NMEA-TCP send failed (OSError); closing client.")
            self.assertEqual(state.app.stats["tcp_write_errors"], 50)
            self.assertEqual(state.app.stats["nmea_tcp_drops"], 50)
            now[0] = 60000
            asyncio.run(fail_deliveries(1, b"$GNGGA,2\r\n"))
            self.assertEqual(logged.call_count, 2)
            self.assertTrue(logged.call_args[0][2].endswith(" [49 more since the last report]"))


class TestNtripUnexpectedError(unittest.TestCase):
    def test_repeated_error_logs_once_with_traceback(self):
        error = OSError(113, "EHOSTUNREACH")
        with mock.patch.object(net, "_UNEXPECTED_ERROR_LOG", make_throttle([0])), \
                mock.patch.object(net, "log") as logged:
            for _ in range(5):
                net._log_unexpected_error(error, "[Errno 113] EHOSTUNREACH")
        logged.assert_called_once()
        self.assertEqual(logged.call_args[0],
                         ("ERROR", "NTRIP", "Unexpected NTRIP error: [Errno 113] EHOSTUNREACH"))
        self.assertIs(logged.call_args[1]["print_traceback"], True)
        self.assertIs(logged.call_args[1]["exception"], error)

    def test_new_error_type_logs_immediately(self):
        with mock.patch.object(net, "_UNEXPECTED_ERROR_LOG", make_throttle([0])), \
                mock.patch.object(net, "log") as logged:
            net._log_unexpected_error(OSError(113, "EHOSTUNREACH"), "a")
            net._log_unexpected_error(KeyError("x"), "'x'")
        self.assertEqual(logged.call_count, 2)

    def test_run_uses_the_helper(self):
        tree = source_tree(net.NtripClient.run)
        self.assertIn("_log_unexpected_error", called_names(tree))
        self.assertFalse([s for s in string_constants(tree)
                          if s.startswith("Unexpected NTRIP error")])


class TestHttpRequestRejected(unittest.TestCase):
    def test_same_reason_logs_once_per_interval(self):
        now = [0]
        with mock.patch.dict(state.app.stats, {"http_rejected_requests": 0}), \
                mock.patch.object(web, "_REQUEST_REJECT_LOG", make_throttle(now)), \
                mock.patch.object(web, "log") as logged:
            for _ in range(20):
                web._log_request_rejected("header_timeout")
            logged.assert_called_once_with("WARN", "HTTP", "Request rejected: header_timeout.")
            web._log_request_rejected("header_too_large")
            self.assertEqual(logged.call_count, 2)
            self.assertEqual(logged.call_args[0][2], "Request rejected: header_too_large.")
            now[0] = 60000
            web._log_request_rejected("header_timeout")
            self.assertEqual(logged.call_count, 3)
            self.assertTrue(logged.call_args[0][2].endswith(" [19 more since the last report]"))
            self.assertEqual(state.app.stats["http_rejected_requests"], 22)

    def test_dispatcher_uses_the_helper_and_stays_short(self):
        tree = source_tree(web.handle_http_client)
        self.assertIn("_log_request_rejected", called_names(tree))
        self.assertFalse([s for s in string_constants(tree) if "Request rejected" in s])
        self.assertLess(len(inspect.getsource(web.handle_http_client).splitlines()), 300)

    def test_status_reports_zero_rejections_after_start(self):
        fresh = state.AppState()
        writer = HttpWriter()
        with mock.patch.object(web, "app", fresh), \
                mock.patch.object(web, "_read_allowed", return_value=True), \
                mock.patch.object(web, "_instances", {}), \
                mock.patch.object(web, "board_metrics", return_value={}):
            asyncio.run(web._handle_status_request(writer, "/status", "", False, {}))
        status = json.loads(bytes(writer.buf).split(b"\r\n\r\n", 1)[1])
        self.assertEqual(status["stats"]["http_rejected_requests"], 0)


class TestQueueOverflow(unittest.TestCase):
    def test_first_overflow_is_logged_then_throttled(self):
        now = [0]
        stats = state.app.stats
        with mock.patch.dict(stats, {"queue_overflows": 0}), \
                mock.patch.object(state.app, "errors", []), \
                mock.patch.object(gnss, "_QUEUE_OVERFLOW_LOG", make_throttle(now)), \
                mock.patch.object(gnss, "log") as logged:
            for _ in range(250):
                stats["queue_overflows"] += 1
                gnss._note_queue_overflow()
            logged.assert_called_once_with("ERROR", "GNSS", "NMEA queue full; 1 overflows so far.")
            self.assertEqual(len(state.app.errors), 1)
            self.assertEqual(state.app.errors[0]["source"], "GNSS")
            now[0] = 60000
            stats["queue_overflows"] = 251
            gnss._note_queue_overflow()
            self.assertEqual(logged.call_count, 2)
            self.assertEqual(logged.call_args[0],
                             ("ERROR", "GNSS", "NMEA queue full; 251 overflows so far."))
            self.assertEqual(len(state.app.errors), 2)

    def test_run_has_no_modulo_rule(self):
        tree = source_tree(gnss.GNSSHandler.run)
        self.assertIn("_note_queue_overflow", called_names(tree))
        self.assertFalse([n for n in ast.walk(tree)
                          if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Mod)
                          and isinstance(n.right, ast.Constant) and n.right.value == 100])


if __name__ == "__main__":
    unittest.main()
