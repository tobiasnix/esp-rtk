# SPDX-License-Identifier: AGPL-3.0-only
"""Purpose: The README determines that the ReNEP network transmits MSM5 (1075/1085/1095/1125) while Quectel documents the LC29H MSM4 and MSM7 fix, and that this is the most likely reason why the fix stops on FLOAT. So far, the NTRIP client pushed the bytes unseen into the UART. With this numerator, the device itself tells which types are really arriving. Frame format: D3 | 6 bits reserved + 10 bits long | payload | 3 bytes CRC. The message number is the first 12 bits of the payload.
"""
import unittest

from support import net, cfg


class TestNtripEndpoints(unittest.TestCase):
    def setUp(self):
        self.saved = dict(cfg.CONFIG)

    def tearDown(self):
        cfg.CONFIG.clear(); cfg.CONFIG.update(self.saved)

    def test_disabled_means_no_connections_without_deleting_credentials(self):
        cfg.CONFIG.update({"ntrip_enabled": False, "ntrip_host": "primary.invalid",
                           "ntrip_mount": "SYNTH"})
        self.assertEqual(net.NtripClient._configured_endpoints(), [])
        self.assertEqual(cfg.CONFIG["ntrip_host"], "primary.invalid")

    def test_primary_and_fallback_have_independent_credentials(self):
        cfg.CONFIG.update({"ntrip_enabled": True, "ntrip_host": "primary.invalid",
            "ntrip_mount": "ONE", "ntrip_user": "p", "ntrip_pass": "p-secret",
            "ntrip_fallback_host": "fallback.invalid", "ntrip_fallback_port": 2201,
            "ntrip_fallback_mount": "TWO", "ntrip_fallback_user": "f",
            "ntrip_fallback_pass": "f-secret"})
        endpoints = net.NtripClient._configured_endpoints()
        self.assertEqual([item["label"] for item in endpoints],
                         ["primary", "fallback"])
        self.assertEqual(endpoints[1]["port"], 2201)
        request = net.NtripClient._build_request(endpoints[1])
        self.assertIn(b"GET /TWO ", request)
        self.assertNotIn(b"f-secret", request)


class TestNtripStatusLine(unittest.TestCase):
    def test_accepts_icy_and_http_200(self):
        self.assertEqual(net.NtripClient._parse_status_line(b"ICY 200 OK\r\n"),
                         (True, None, 200))
        self.assertEqual(net.NtripClient._parse_status_line(b"HTTP/1.1 200 OK\r\n"),
                         (True, None, 200))

    def test_classifies_auth_mount_and_sourcetable(self):
        self.assertEqual(net.NtripClient._parse_status_line(
            b"HTTP/1.1 401 Unauthorized\r\n")[1], "auth_error")
        self.assertEqual(net.NtripClient._parse_status_line(
            b"HTTP/1.1 404 Not Found\r\n")[1], "mount_error")
        self.assertEqual(net.NtripClient._parse_status_line(
            b"SOURCETABLE 200 OK\r\n")[1], "mount_error")

    def test_header_substrings_cannot_fake_a_status(self):
        self.assertFalse(net.NtripClient._parse_status_line(
            b"Server: misleading 200 OK\r\n")[0])


def frame(number, payload_length=20):
    """Builds an RTCM3 frame with the specified message number."""
    nutzlast = bytearray(payload_length)
    nutzlast[0] = (number >> 4) & 0xFF
    nutzlast[1] = (number & 0x0F) << 4
    kopf = bytes([0xD3, (payload_length >> 8) & 0x03, payload_length & 0xFF])
    return kopf + bytes(nutzlast) + b"\xAA\xBB\xCC"      # CRC24Q (not checked)


class TestRahmenerkennung(unittest.TestCase):
    def setUp(self):
        self.z = net.RtcmCounter()

    def test_einzelner_rahmen(self):
        self.z.feed(frame(1005))
        self.assertEqual(self.z.counts, {1005: 1})

    def test_multiple_types(self):
        for nr in (1005, 1075, 1085, 1075):
            self.z.feed(frame(nr))
        self.assertEqual(self.z.counts, {1005: 1, 1075: 2, 1085: 1})

    def test_msm5_sentence_as_in_the_readme(self):
        for nr in (1075, 1085, 1095, 1125):
            self.z.feed(frame(nr))
        self.assertEqual(sorted(self.z.counts), [1075, 1085, 1095, 1125])

    def test_fragmented_byte_by_byte(self):
        """The electricity comes out of the socket in arbitrary bits."""
        data = frame(1077) + frame(1087)
        for b in data:
            self.z.feed(bytes([b]))
        self.assertEqual(self.z.counts, {1077: 1, 1087: 1})

    def test_split_at_adverse_boundaries(self):
        data = frame(1230)
        for split in range(1, len(data)):
            z = net.RtcmCounter()
            z.feed(data[:split])
            z.feed(data[split:])
            self.assertEqual(z.counts, {1230: 1}, "Schnitt bei %d" % split)

    def test_garbage_in_front_of_the_first_frame(self):
        # The caster first sends HTTP headers; then RTCM begins.
        self.z.feed(b"ICY 200 OK\r\n\r\n" + frame(1005))
        self.assertEqual(self.z.counts, {1005: 1})

    def test_maximum_length(self):
        self.z.feed(frame(1077, 1023))
        self.assertEqual(self.z.counts, {1077: 1})

    def test_empty_payload_is_discarded(self):
        # Length 0 cannot contain a message number.
        self.z.feed(bytes([0xD3, 0x00, 0x00]) + b"\xAA\xBB\xCC")
        self.assertEqual(self.z.counts, {})

    def test_bytes_are_counted(self):
        data = frame(1005)
        self.z.feed(data)
        self.assertEqual(self.z.bytes_total, len(data))

    def test_counters_remain_small(self):
        """During continuous operation, the condition shall not grow."""
        for _ in range(200):
            self.z.feed(frame(1075))
        self.assertEqual(self.z.counts, {1075: 200})
        self.assertLessEqual(len(self.z._buf), 4)


class TestZusammenfassung(unittest.TestCase):
    def test_empty_state(self):
        z = net.RtcmCounter()
        bericht = z.summary()
        self.assertEqual(bericht["messages"], {})
        self.assertEqual(bericht["bytes_total"], 0)
        self.assertIsNone(bericht["last_msg_age_sec"])

    def test_report_calls_types_and_bytes(self):
        z = net.RtcmCounter()
        z.feed(frame(1075) + frame(1085))
        bericht = z.summary()
        self.assertEqual(bericht["messages"], {"1075": 1, "1085": 1})
        self.assertGreater(bericht["bytes_total"], 0)
        self.assertIsNotNone(bericht["last_msg_age_sec"])

    def test_msm_einordnung(self):
        """The real question: which MSM does the caster deliver?"""
        self.assertEqual(net.msm_familie(1074), "MSM4")
        self.assertEqual(net.msm_familie(1075), "MSM5")
        self.assertEqual(net.msm_familie(1077), "MSM7")
        self.assertEqual(net.msm_familie(1085), "MSM5")
        self.assertEqual(net.msm_familie(1127), "MSM7")
        self.assertIsNone(net.msm_familie(1005))

    def test_the_report_calls_the_msm_families(self):
        z = net.RtcmCounter()
        for nr in (1005, 1075, 1085, 1095, 1125):
            z.feed(frame(nr))
        self.assertEqual(z.summary()["msm"], ["MSM5"])

    def test_gemischte_familien(self):
        z = net.RtcmCounter()
        z.feed(frame(1074) + frame(1077))
        self.assertEqual(sorted(z.summary()["msm"]), ["MSM4", "MSM7"])


if __name__ == "__main__":
    unittest.main()
