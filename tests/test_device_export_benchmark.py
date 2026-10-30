# SPDX-License-Identifier: AGPL-3.0-only
import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

import support  # noqa: F401 - installs the MicroPython compatibility modules
import tracking  # noqa: F401 - imported before the benchmark program changes directory
import pointstore  # noqa: F401
import device_export_benchmark as benchmark


def run_on_host(source):
    """exec the generated program in an empty directory; return (JSON line, leftovers)."""
    previous, output = os.getcwd(), io.StringIO()
    with tempfile.TemporaryDirectory() as directory:
        os.chdir(directory)
        try:
            with contextlib.redirect_stdout(output):
                exec(compile(source, "<device-export-benchmark>", "exec"), {"__name__": "bench"})
            leftovers = [n for n in os.listdir(directory) if n.startswith(benchmark.PREFIX)]
        finally:
            os.chdir(previous)
    return json.loads(output.getvalue().strip().splitlines()[-1]), leftovers


class TestDeviceExportBenchmark(unittest.TestCase):
    def test_program_compiles_and_measures_both_formats(self):
        for case in ("large", "long"):
            source = benchmark.program(case, True)
            compile(source, "<device-export-benchmark>", "exec")
            for text in ("export-benchmark-v1", "WDT(0,3600000)", "iter_geojson(",
                         "iter_backup_ndjson(", "gc.mem_free()", "profile_us",
                         "'insufficient_space'"):
                self.assertIn(text, source, (case, text))
            self.assertNotIn("%%d", source)
        self.assertEqual({"large": (4, 850, 131), "long": (1, 3400, 0)}, benchmark.CASES)
        # The heap probe lives only in the heap run, never in the timing loop.
        source = benchmark.program("large", True)
        timed = source[source.index("def timed("):source.index("def heap(")]
        self.assertNotIn("mem_free", timed)
        self.assertIn("profile_us", timed)
        self.assertIn("mem_free", source[source.index("def heap("):])

    def test_program_runs_on_the_host_against_the_synthetic_case(self):
        with mock.patch.dict(benchmark.CASES, {"long": (1, 120, 0)}):
            source = benchmark.program("long", True, sample_every=50)
        result, leftovers = run_on_host(source)
        self.assertEqual("long", result["case"])
        self.assertEqual(120, result["vertices"])
        self.assertEqual(120, result["longest_line"])
        self.assertGreater(result["geojson"]["bytes"], 0)
        self.assertGreater(result["backup"]["bytes"], 0)
        self.assertGreater(result["geojson"]["chunks"], 120)
        self.assertGreaterEqual(result["geojson"]["heap_peak_bytes"], 0)
        self.assertGreaterEqual(result["backup"]["heap_peak_bytes"], 0)
        self.assertGreaterEqual(result["serialize_ms_geojson"], 0)
        for key in ("ratio_ms", "heap_ratio"):
            self.assertIn(key, result)
        self.assertEqual([], leftovers)
        # Several lines plus object points: every line is built and the longest reported.
        with mock.patch.dict(benchmark.CASES, {"large": (2, 6, 3)}):
            result, leftovers = run_on_host(benchmark.program("large", True))
        self.assertEqual(12, result["vertices"])
        self.assertEqual(6, result["longest_line"])
        self.assertEqual([], leftovers)
        self.check_keep_data_reuses_the_files()
        self.check_low_flash_writes_nothing()

    def check_keep_data_reuses_the_files(self):
        previous = os.getcwd()
        with mock.patch.dict(benchmark.CASES, {"long": (1, 30, 0)}), \
                tempfile.TemporaryDirectory() as directory:
            os.chdir(directory)
            try:
                for _ in range(2):
                    with contextlib.redirect_stdout(io.StringIO()) as output:
                        exec(compile(benchmark.program("long", False), "<keep>", "exec"), {})
                    result = json.loads(output.getvalue().strip().splitlines()[-1])
                    self.assertEqual(30, result["vertices"])
                self.assertTrue([n for n in os.listdir(directory)
                                 if n.startswith(benchmark.PREFIX)])
            finally:
                os.chdir(previous)

    def check_low_flash_writes_nothing(self):
        source = benchmark.program("large", True)
        tiny = os.statvfs_result((4096, 4096, 1000, 10, 10, 0, 0, 0, 0, 255))
        previous, output = os.getcwd(), io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            os.chdir(directory)
            try:
                with mock.patch("os.statvfs", return_value=tiny), \
                        contextlib.redirect_stdout(output):
                    exec(compile(source, "<low-flash>", "exec"), {})
                written = os.listdir(directory)
            finally:
                os.chdir(previous)
        self.assertEqual({"insufficient_space": True},
                         json.loads(output.getvalue().strip().splitlines()[-1]))
        self.assertEqual([], written)

    def test_main_requires_a_port_and_never_opens_a_device(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                benchmark.main(["long"])
            with self.assertRaises(SystemExit):
                benchmark.main(["long", "--port", "/dev/ttyNONEXISTENT0", "--sample-every", "0"])
            with self.assertRaises(SystemExit):
                benchmark.main(["long", "--port", "/dev/ttyNONEXISTENT0", "--sample-every", "101"])
        output = io.StringIO()
        with mock.patch.object(benchmark, "with_retry",
                               return_value=b'{"case":"long"}\n') as retry, \
                contextlib.redirect_stdout(output):
            benchmark.main(["long", "--port", "/dev/ttyNONEXISTENT0"])
        self.assertIn('"case"', output.getvalue())
        retry.assert_called_once()
        self.assertEqual("/dev/ttyNONEXISTENT0", retry.call_args.kwargs["port"])
        self.assertEqual(3600000, retry.call_args.kwargs["wdt_timeout_ms"])
        self.check_a_failed_run_removes_the_files_before_re_raising()

    def check_a_failed_run_removes_the_files_before_re_raising(self):
        calls = []

        def failing(operation, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise RuntimeError("device error")

        with mock.patch.object(benchmark, "with_retry", side_effect=failing):
            with self.assertRaises(RuntimeError):
                benchmark.main(["large", "--port", "/dev/ttyNONEXISTENT0"])
            self.assertEqual(2, len(calls))
            self.assertTrue(all(c["port"] == "/dev/ttyNONEXISTENT0" for c in calls))
            calls.clear()
            with self.assertRaises(RuntimeError):
                benchmark.main(["large", "--port", "/dev/ttyNONEXISTENT0", "--keep-data"])
            self.assertEqual(1, len(calls))


if __name__ == "__main__":
    unittest.main()
