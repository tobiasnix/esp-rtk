# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for the $PESPS contract and its non-blocking queue."""
import unittest

from support import cfg, state
import esptel


class TestPESPS(unittest.TestCase):
    def test_exact_versioned_set_with_checksum(self):
        result = esptel.build_pesps("A1B2C3D4E5F6", 42, "streaming", 0.4,
                                    12345, ["MSM7", "MSM4"], -61,
                                    "STA+AP", 2)
        self.assertEqual(result,
            "$PESPS,1,A1B2C3D4E5F6,42,streaming,0.4,12345,4+7,-61,sta_ap,2*0D\r\n")

    def test_unknown_optional_fields_remain_empty(self):
        result = esptel.build_pesps("A", 0, "disabled", None, 0, [], None,
                                    "INIT", 0)
        self.assertIn(",disabled,,0,,,down,0*", result)
        payload, checksum = result[1:].strip().split("*")
        self.assertEqual(checksum, esptel.xor_checksum(payload))

    def test_q_put_nowait_does_not_block(self):
        queue = state.SimpleQueue(maxsize=1)
        self.assertTrue(queue.put_nowait(b"one"))
        self.assertFalse(queue.put_nowait(b"two"))

    def test_the_rate_remains_below_the_transport_limit(self):
        result = esptel.build_pesps("F" * 12, 999999999, "auth_error", 9999.9,
                                    999999999, ["MSM4", "MSM7"], -127,
                                    "STA+AP", 999999)
        self.assertLessEqual(len(result.encode()), 120)

if __name__ == "__main__":
    unittest.main()
