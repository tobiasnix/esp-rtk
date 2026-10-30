# SPDX-License-Identifier: AGPL-3.0-only
import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

import support  # noqa: F401 - installs the MicroPython compatibility modules
import device_nmea_benchmark as benchmark
import benchmark_nmea_hotpath
import gnss
import state
import uasyncio

# Measured at 08c5928 with the host program; every later task keeps them (the
# scanner must queue, drop and count exactly the same bytes).
CORPUS_BASELINE = {"queue_items": 13, "queue_bytes": 843, "crc_errors": 4, "digest": 305958890}

COMPONENTS = ("parse_gga", "coord_e8", "decimal_mm", "quality_check", "checksum",
              "scanner", "scanner_corpus", "rtcm_feed", "observe_rtcm",
              "uart_tx_write_rtcm")


def run_on_host(source):
    """exec the generated program in an empty directory; return its JSON line."""
    previous, output = os.getcwd(), io.StringIO()
    with tempfile.TemporaryDirectory() as directory:
        os.chdir(directory)
        try:
            with contextlib.redirect_stdout(output):
                exec(compile(source, "<device-nmea-benchmark>", "exec"), {"__name__": "bench"})
        finally:
            os.chdir(previous)
    return json.loads(output.getvalue().strip().splitlines()[-1])


class TestDeviceNmeaBenchmark(unittest.TestCase):
    def test_program_compiles_and_contains_its_guards(self):
        source = benchmark.program()
        compile(source, "<device-nmea-benchmark>", "exec")
        for text in ("WDT(0,600000)", "gc.mem_alloc()", "shutdown_event.clear()",
                     benchmark.GGA_BODY, "unavailable"):
            self.assertIn(text, source, text)
        self.assertNotIn("%%", source)
        self.assertNotIn("@", source)

    def test_every_gc_disable_is_undone_in_a_finally(self):
        # A MemoryError inside a measuring block must not leave gc disabled on
        # the device; every gc.disable() needs a finally that enables it again.
        import ast
        tree = ast.parse(benchmark.program())

        def calls(node, name):
            return [n for n in ast.walk(node) if isinstance(n, ast.Call)
                    and ast.unparse(n.func) == name]

        disabled = calls(tree, "gc.disable")
        guarded = [n for n in ast.walk(tree) if isinstance(n, ast.Try)
                   and any(calls(stmt, "gc.enable") for stmt in n.finalbody)]
        self.assertTrue(disabled)
        self.assertEqual(len(disabled), len(guarded))

    def test_program_runs_on_the_host_and_reports_every_component(self):
        result = run_on_host(benchmark.program(sentences=320, repeat=20, rtcm_bytes=4096))
        self.assertEqual("cpython", result["runtime"])
        self.assertIn("version", result)
        for name in COMPONENTS:
            self.assertIn(name, result)
            self.assertNotIn("unavailable", result[name], name)
        self.assertEqual(32, result["scanner"]["gga_parsed"])
        self.assertEqual(0, result["scanner"]["crc_errors"])
        self.assertEqual(320, result["scanner"]["queue_items"])
        self.assertEqual(320, result["scanner"]["sentences"])
        self.assertIsNone(result["parse_gga"]["alloc_bytes_per_call"])
        self.assertEqual(20, result["parse_gga"]["calls"])
        self.assertEqual(4096, result["rtcm_feed"]["bytes"])
        for name in COMPONENTS:
            for key, value in result[name].items():
                if key.startswith("us_per_"):
                    self.assertGreater(value, 0, (name, key))
        self.assertEqual({"queue_items", "queue_bytes", "crc_errors", "digest"},
                         set(result["scanner_corpus"]))

    def test_program_reports_unavailable_components_instead_of_failing(self):
        saved = gnss.UartTx
        delattr(gnss, "UartTx")
        try:
            result = run_on_host(benchmark.program(sentences=40, repeat=5, rtcm_bytes=2048))
        finally:
            gnss.UartTx = saved
        self.assertEqual({"unavailable": "gnss.UartTx"}, result["uart_tx_write_rtcm"])
        for name in ("parse_gga", "coord_e8", "decimal_mm", "quality_check", "checksum",
                     "rtcm_feed", "observe_rtcm"):
            self.assertNotIn("unavailable", result[name], name)
        # GNSSHandler builds its UartTx, so the scanner cannot run without the class.
        for name in ("scanner", "scanner_corpus"):
            self.assertIn("unavailable", result[name], name)
        self.assertIs(saved, gnss.UartTx)
        self.assertEqual(8.0, benchmark_nmea_hotpath.compare(
            {"scanner": {"us_per_sentence": 80.0}},
            {"scanner": {"us_per_sentence": 10.0}})["scanner"])
        self.assertIsNone(benchmark_nmea_hotpath.compare(
            {"uart_tx_write_rtcm": {"us_per_kb": 5.0}},
            result)["uart_tx_write_rtcm"])

    def test_scanner_corpus_digest_is_fixed(self):
        first = run_on_host(benchmark.program(sentences=10, repeat=1, rtcm_bytes=1024))
        second = run_on_host(benchmark.program(sentences=10, repeat=1, rtcm_bytes=1024))
        self.assertEqual(first["scanner_corpus"], second["scanner_corpus"])
        self.assertEqual(CORPUS_BASELINE, first["scanner_corpus"])
        self.assertTrue(benchmark.SCANNER_CORPUS.endswith(b"$GNGGA,123519.00,4807"))

    def test_program_restores_every_patched_global(self):
        sleep = uasyncio.sleep_ms
        limit = gnss.CONFIG["gsv_limit_per_sec"]
        stats = dict(state.app.stats)
        run_on_host(benchmark.program(sentences=40, repeat=2, rtcm_bytes=1024))
        self.assertIs(support.FakeUART, gnss.UART)
        self.assertIs(sleep, uasyncio.sleep_ms)
        self.assertEqual(limit, gnss.CONFIG["gsv_limit_per_sec"])
        self.assertFalse(state.shutdown_event.is_set())
        self.assertNotIn("update_fix", vars(state.app))
        self.assertNotIn("attach_gst", vars(state.app))
        self.assertEqual(stats, dict(state.app.stats))

    def test_host_run_counts_quality_checks_per_epoch(self):
        result = benchmark_nmea_hotpath.run(sentences=320, repeat=20, epochs=20)
        for path in ("gate_survey", "gate_route"):
            self.assertEqual(20, result[path]["epochs"], path)
            self.assertEqual(40, result[path]["fix_versions"], path)
            self.assertEqual(40, result[path]["listener_calls"], path)
        # One verdict per published fix version (GGA, then its GST): enqueue_fix and
        # the worker's write reuse it. 08c5928 judged 100 (survey) and 60 (route).
        self.assertEqual(40, result["gate_survey"]["quality_checks"])
        self.assertEqual(40, result["gate_route"]["quality_checks"])
        parse_us = result["parse_gga"]["us_per_call"]
        check_us = result["quality_check"]["us_per_call"]
        cost = result["epoch_cost_us"]
        self.assertEqual(round(parse_us + result["gate_survey"]["per_epoch"] * check_us),
                         cost["K_survey"])
        self.assertEqual(round(parse_us + result["gate_route"]["per_epoch"] * check_us),
                         cost["K_route"])
        self.assertEqual(round(parse_us + 4 * check_us), cost["K_n4"])
        self.assertEqual(round(5 * parse_us + 2 * check_us), cost["per_second_5hz_survey"])
        self.assertEqual(round(5 * parse_us + 2 * check_us), cost["per_second_5hz_route"])
        for name in ("parse_gga", "coord_e8", "decimal_mm", "quality_check", "checksum",
                     "scanner", "rtcm_feed"):
            self.assertGreaterEqual(result["host_peak_bytes"][name], 0, name)

    def test_compare_reports_factors(self):
        factors = benchmark_nmea_hotpath.compare({"parse_gga": {"us_per_call": 400}},
                                                 {"parse_gga": {"us_per_call": 200}})
        self.assertEqual(2.0, factors["parse_gga"])
        self.assertIsNone(factors["scanner"])
        factors = benchmark_nmea_hotpath.compare(
            {"parse_gga": {"us_per_call": 400, "alloc_bytes_per_call": 3000}},
            {"parse_gga": {"us_per_call": 200, "alloc_bytes_per_call": 1000}})
        self.assertEqual(3.0, factors["parse_gga_alloc"])

    def test_main_requires_a_port_and_never_opens_a_device(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                benchmark.main([])
            with self.assertRaises(SystemExit):
                benchmark.main(["--port", "/dev/ttyNONEXISTENT-28c", "--sentences", "5"])
        output = io.StringIO()
        with mock.patch.object(benchmark, "with_retry",
                               return_value=b'{"runtime":"micropython"}\n') as retry, \
                contextlib.redirect_stdout(output):
            benchmark.main(["--port", "/dev/ttyNONEXISTENT-28c"])
        self.assertIn('"runtime"', output.getvalue())
        retry.assert_called_once()
        self.assertEqual("/dev/ttyNONEXISTENT-28c", retry.call_args.kwargs["port"])
        self.assertEqual(600000, retry.call_args.kwargs["wdt_timeout_ms"])
        self.assertEqual(1, retry.call_args.kwargs["versuche"])


if __name__ == "__main__":
    unittest.main()
