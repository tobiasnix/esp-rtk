# SPDX-License-Identifier: AGPL-3.0-only
import json
import re
import unittest

import device_tracking_realtime_benchmark as benchmark
from test_device_tracking_benchmark import BOUND, fake_device_modules, real_bindings, run_isolated


class TestRealtimeBenchmark(unittest.TestCase):
    def test_generated_program_compiles_and_contains_latency_contract(self):
        source = benchmark.program(60, True)
        compile(source, "<realtime-benchmark>", "exec")
        self.assertIn("append_p99_ms", source)
        self.assertIn("loop_lag_max_ms", source)
        self.assertIn("await t.compact_async()", source)
        self.assertIn(benchmark.PREFIX, source)

    def test_program_sets_tracking_limits_in_every_module(self):
        before = real_bindings()
        source = benchmark.program(2, False)
        self.assertIsNone(re.search(r"tracking\.[A-Z_]+\s*=", source))
        mods = fake_device_modules()
        ns = run_isolated(source, mods)
        seen = mods["tracking"].seen
        self.assertEqual(seen[0][0], "new")
        self.assertEqual(seen[1][0], "add")
        for name in BOUND:
            self.assertEqual(seen[0][1][name][0], 5000, name)
            self.assertEqual(seen[1][1][name][0], 10000, name)
        self.assertEqual(seen[1][1]["tracking.frames"], (None, None))
        self.assertEqual(seen[1][1]["trackingx"], (5000, 131072))
        self.assertEqual(json.loads(ns["_printed"][-1])["points"], 2)
        self.assertEqual(real_bindings(), before)


if __name__ == "__main__":
    unittest.main()
