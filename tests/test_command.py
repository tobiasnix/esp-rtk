# SPDX-License-Identifier: AGPL-3.0-only
"""Purpose: The README chapter on the LC29H interrogates version and configuration with $PQTM sets - each time via mpexec.py, which holds for the application. NTRIP and BLE are eliminated and the fix is removed. With this channel, the same goes on in operation. What goes through is narrow: everything that goes in here ends up in the UART of the receiver.
"""
import unittest

from support import gnss, sentence


class TestCommandValidation(unittest.TestCase):
    def accepted(self, sentence):
        ok, error = gnss.check_command(sentence)
        self.assertIsNone(error, "unexpectedly rejected: %s" % error)
        return ok

    def rejected(self, sentence):
        ok, error = gnss.check_command(sentence)
        self.assertIsNone(ok)
        self.assertTrue(error)
        return error

    def test_quectel_version_query(self):
        # Exactly the sentence from the README.
        self.assertEqual(self.accepted("$PQTMVERNO*58"), "$PQTMVERNO*58")

    def test_q_ctel_configuration_query(self):
        self.accepted(sentence("PQTMCFGRCVRMODE,R"))
        self.accepted(sentence("PQTMCFGCNST,R"))

    def test_pair_is_accepted(self):
        self.accepted(sentence("PAIR001,0,0"))

    def test_surrounding_spaces_are_removed(self):
        self.assertEqual(self.accepted("  $PQTMVERNO*58 \r\n"), "$PQTMVERNO*58")

    def test_wrong_checksum(self):
        self.rejected("$PQTMVERNO*00")

    def test_no_checksum(self):
        self.rejected("$PQTMVERNO")

    def test_foreign_prefix_is_rejected(self):
        # Only proprietary configuration sets, no position data.
        self.rejected(sentence("GNGGA,1"))

    def test_without_dollars(self):
        self.rejected("PQTMVERNO*58")

    def test_empty(self):
        self.rejected("")
        self.rejected("   ")

    def test_zeilenumbruch_mitten_drin(self):
        # Otherwise, two orders could be placed in one.
        self.rejected("$PQTMVERNO*58\r\n$PQTMSRR*4B")

    def test_too_long(self):
        self.rejected("$PQTM" + "A" * 200 + "*00")

    def test_non_printable_marks(self):
        self.rejected("$PQTM\x00VERNO*58")


class TestAntwortpuffer(unittest.TestCase):
    def setUp(self):
        gnss.replies.clear()

    def tearDown(self):
        gnss.replies.clear()

    def test_response_is_recorded(self):
        gnss.remember_reply("$PQTMVERNO,LC29HDANR11A03S_RSA*07")
        self.assertEqual(len(gnss.replies), 1)

    def test_buffers_remain_limited(self):
        for i in range(50):
            gnss.remember_reply("$PQTMX,%d*00" % i)
        self.assertLessEqual(len(gnss.replies), gnss.REPLY_MAX)
        # The youngest survived.
        self.assertIn("49", gnss.replies[-1])


if __name__ == "__main__":
    unittest.main()
