# SPDX-License-Identifier: AGPL-3.0-only
import unittest

import device_tracking_benchmark as benchmark


class TestDeviceTrackingBenchmark(unittest.TestCase):
    def test_generated_program_compiles_and_formats_metrics(self):
        source=benchmark.program(5000,True)
        compile(source,"<device-benchmark>","exec")
        self.assertNotIn("%%d",source)
        self.assertIn("'points':done",source)
        self.assertIn("'append_profile_us':t.append_profile()", source)
        self.assertIn("POINT_SEGMENT_BYTES=128*1024", source)
        self.assertIn("MAX_FEATURES=max(tracking.TARGET_MAX_FEATURES,5000)", source)
        self.assertIn("scale-benchmark-v15",source)

    def test_experimental_target_raises_only_benchmark_limit(self):
        source = benchmark.program(20000, True)
        self.assertIn("MAX_FEATURES=max(tracking.TARGET_MAX_FEATURES,20000)", source)


if __name__ == "__main__":
    unittest.main()
