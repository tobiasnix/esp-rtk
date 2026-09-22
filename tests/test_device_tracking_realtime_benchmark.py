# SPDX-License-Identifier: AGPL-3.0-only
import unittest

import device_tracking_realtime_benchmark as benchmark


class TestRealtimeBenchmark(unittest.TestCase):
    def test_generated_program_compiles_and_contains_latency_contract(self):
        source = benchmark.program(60, True)
        compile(source, "<realtime-benchmark>", "exec")
        self.assertIn("append_p99_ms", source)
        self.assertIn("loop_lag_max_ms", source)
        self.assertIn("await t.compact_async()", source)
        self.assertIn(benchmark.PREFIX, source)


if __name__ == "__main__":
    unittest.main()
