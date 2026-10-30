# SPDX-License-Identifier: AGPL-3.0-only
"""Purpose: The README determines that the ReNEP network transmits MSM5 (1075/1085/1095/1125) while Quectel documents the LC29H MSM4 and MSM7 fix, and that this is the most likely reason why the fix stops on FLOAT. So far, the NTRIP client pushed the bytes unseen into the UART. With this numerator, the device itself tells which types are really arriving. Frame format: D3 | 6 bits reserved + 10 bits long | payload | 3 bytes CRC. The message number is the first 12 bits of the payload.
"""
import time
import unittest
from unittest import mock

from support import net, cfg, state
from test_field_diagnostics import crc24, frame as payload_frame


class TestNtripEndpoints(unittest.TestCase):
    def setUp(self):
        self.saved = dict(cfg.CONFIG)

    def tearDown(self):
        cfg.CONFIG.clear(); cfg.CONFIG.update(self.saved)

    def test_disabled_means_no_connections_without_deleting_credentials(self):
        cfg.CONFIG.update({"ntrip_enabled": False, "ntrip_host": "primary.invalid",
                           "ntrip_mount": "SYNTH"})
        self.assertEqual(net.NtripMachine.configured_endpoints(), [])
        self.assertEqual(cfg.CONFIG["ntrip_host"], "primary.invalid")

    def test_primary_and_fallback_have_independent_credentials(self):
        cfg.CONFIG.update({"ntrip_enabled": True, "ntrip_host": "primary.invalid",
            "ntrip_mount": "ONE", "ntrip_user": "p", "ntrip_pass": "p-secret",
            "ntrip_fallback_host": "fallback.invalid", "ntrip_fallback_port": 2201,
            "ntrip_fallback_mount": "TWO", "ntrip_fallback_user": "f",
            "ntrip_fallback_pass": "f-secret"})
        endpoints = net.NtripMachine.configured_endpoints()
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
        self.assertLessEqual(len(self.z._buf), 8)


def msm_epoch(number, epoch):
    """MSM frame whose 30-bit epoch field carries `epoch` (CRC is valid but unchecked)."""
    bits = format(number, '012b') + format(54, '012b') + format(epoch, '030b')
    bits += '0' * 19 + '0' * 64 + '0' * 32
    bits += '0' * ((-len(bits)) % 8)
    return payload_frame(int(bits, 2).to_bytes(len(bits) // 8, 'big'))


# Plain 8-tuples like MicroPython's gmtime(); the code under test must index them.
SUNDAY_0001 = (2026, 10, 18, 0, 1, 0, 6, 291)          # a Sunday, 00:01:00 UTC
SATURDAY_235959 = (2026, 10, 17, 23, 59, 59, 5, 290)
SATURDAY_210030 = (2026, 10, 17, 21, 0, 30, 5, 290)    # Moscow: Sunday 00:00:30


class TestEpochAge(unittest.TestCase):
    def setUp(self):
        self.now = 500_000
        self.ticks = mock.patch.object(time, "ticks_ms", side_effect=lambda: self.now)
        self.ticks.start()
        self.addCleanup(self.ticks.stop)
        self.clock = mock.patch.object(time, "gmtime", return_value=SUNDAY_0001)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.saved = dict(cfg.CONFIG)
        self.addCleanup(lambda: (cfg.CONFIG.clear(), cfg.CONFIG.update(self.saved)))
        self.counter = net.RtcmCounter()

    def test_gps_epoch_matches_the_device_clock(self):
        self.counter.feed(msm_epoch(1075, 78_000))            # UTC 60 s + 18 s leap
        self.assertEqual(self.counter.epoch_age_ms(), 0)
        self.assertEqual(self.counter.summary()["epoch_source"], "GPS")
        self.assertEqual(self.counter.summary()["epoch_age_sec"], 0.0)
        self.assertFalse(self.counter.summary()["stale"])

    def test_old_epochs_age_and_go_stale(self):
        self.counter.feed(msm_epoch(1075, 18_000))            # 60 s old
        self.assertEqual(self.counter.epoch_age_ms(), 60_000)
        self.now += 4_000
        self.assertEqual(self.counter.summary()["epoch_age_sec"], 64.0)
        self.assertTrue(self.counter.summary()["stale"])
        self.assertTrue(self.counter.epoch_stale())
        self.assertAlmostEqual(self.counter.summary()["arrival_age_sec"], 4.0)
        self.assertEqual(self.counter.summary()["arrival_age_sec"],
                         self.counter.summary()["last_msg_age_sec"])

    def test_a_negative_tick_difference_means_the_cached_epoch_expired(self):
        self.counter.feed(msm_epoch(1075, 78_000))
        self.now -= 1_000            # ticks wrapped: ticks_diff(now, then) < 0
        self.assertIsNone(self.counter.epoch_age_ms())
        self.assertFalse(self.counter.epoch_stale())
        self.assertIsNone(self.counter.summary()["epoch_age_sec"])

    def test_stale_threshold_follows_the_config(self):
        self.counter.feed(msm_epoch(1075, 18_000))
        cfg.CONFIG["ntrip_max_epoch_age_sec"] = 120
        self.assertFalse(self.counter.epoch_stale())
        cfg.CONFIG["ntrip_max_epoch_age_sec"] = 59
        self.assertTrue(self.counter.epoch_stale())

    def test_leap_second_offset_follows_the_config(self):
        cfg.CONFIG["gps_utc_offset_sec"] = 19
        self.counter.feed(msm_epoch(1075, 78_000))
        self.assertEqual(self.counter.epoch_age_ms(), 1_000)

    def test_galileo_beidou_and_glonass_scales(self):
        self.counter.feed(msm_epoch(1095, 78_000)); self.assertEqual(self.counter.epoch_age_ms(), 0)
        self.counter.feed(msm_epoch(1125, 64_000)); self.assertEqual(self.counter.epoch_age_ms(), 0)
        self.assertEqual(self.counter.summary()["epoch_source"], "BDS")
        self.counter.feed(msm_epoch(1085, (0 << 27) | 10_860_000)); self.assertEqual(self.counter.epoch_age_ms(), 0)
        self.counter.feed(msm_epoch(1085, (7 << 27) | 10_800_000)); self.assertEqual(self.counter.epoch_age_ms(), 60_000)
        self.assertEqual(self.counter.summary()["epoch_source"], "GLO")

    def test_every_msm_family_has_its_source_name(self):
        for number, name in ((1071, "GPS"), (1077, "GPS"), (1081, "GLO"), (1091, "GAL"),
                             (1101, "SBAS"), (1111, "QZSS"), (1121, "BDS"), (1137, "IRNSS")):
            counter = net.RtcmCounter()
            counter.feed(msm_epoch(number, 0))
            self.assertEqual(counter.summary()["epoch_source"], name, number)

    def test_future_and_implausible_epochs(self):
        self.counter.feed(msm_epoch(1075, 90_000))            # 12 s in the future: clamp
        self.assertEqual(self.counter.epoch_age_ms(), 0)
        self.counter.feed(msm_epoch(1075, 18_000))
        self.counter.feed(msm_epoch(1075, 700_000_000))        # beyond a week: ignored
        self.assertEqual(self.counter.epoch_age_ms(), 60_000)
        self.counter.feed(msm_epoch(1085, (3 << 27) | 90_000_000))   # tod beyond a day: ignored
        self.assertEqual(self.counter.epoch_age_ms(), 60_000)

    def test_week_rollover_does_not_produce_a_week(self):
        with mock.patch.object(time, "gmtime", return_value=SATURDAY_235959):
            # Device GPS TOW = 604799 + 18 = 604817 -> 17 s into the new week.
            self.counter.feed(msm_epoch(1075, 17_000)); self.assertEqual(self.counter.epoch_age_ms(), 0)
            self.counter.feed(msm_epoch(1075, 604_757_000)); self.assertEqual(self.counter.epoch_age_ms(), 60_000)
            self.counter.feed(msm_epoch(1125, 604_743_000)); self.assertEqual(self.counter.epoch_age_ms(), 60_000)
            # Moscow time is Sunday 02:59:59: day 0, tod 10_799_000 ms.
            self.counter.feed(msm_epoch(1085, (0 << 27) | 10_739_000)); self.assertEqual(self.counter.epoch_age_ms(), 60_000)

    def test_glonass_moscow_week_rollover(self):
        # Moscow is Sunday 00:00:30 (day 0, tod 30_000); 60 s earlier was Saturday (day 6).
        with mock.patch.object(time, "gmtime", return_value=SATURDAY_210030):
            self.counter.feed(msm_epoch(1085, (6 << 27) | 86_370_000)); self.assertEqual(self.counter.epoch_age_ms(), 60_000)
            self.counter.feed(msm_epoch(1085, (7 << 27) | 86_370_000)); self.assertEqual(self.counter.epoch_age_ms(), 60_000)

    def test_invalid_clock_gives_no_age(self):
        for clock in ((2000, 1, 1, 0, 0, 5, 5, 1), (2100, 1, 1, 0, 0, 5, 4, 1)):
            with mock.patch.object(time, "gmtime", return_value=clock):
                self.counter.feed(msm_epoch(1075, 78_000))
                self.assertIsNone(self.counter.epoch_age_ms())
                self.assertIsNone(self.counter.summary()["epoch_age_sec"])
                self.assertFalse(self.counter.summary()["stale"])

    def test_clock_turning_invalid_hides_a_cached_age(self):
        self.counter.feed(msm_epoch(1075, 18_000))
        with mock.patch.object(time, "gmtime", return_value=(2000, 1, 1, 0, 0, 5, 5, 1)):
            self.assertIsNone(self.counter.epoch_age_ms())
            self.assertFalse(self.counter.epoch_stale())
        self.assertEqual(self.counter.epoch_age_ms(), 60_000)

    def test_short_and_non_msm_frames_are_counted_but_not_decoded(self):
        self.counter.feed(payload_frame(b"\x43\x20\x00\x36"))         # 1074-ish, 4-byte payload -> no epoch
        self.counter.feed(payload_frame(b"\x3e\xe0" + b"\x00" * 10))   # 1006
        self.assertIsNone(self.counter.epoch_age_ms())
        self.assertEqual(self.counter.summary()["messages"]["1006"], 1)
        self.assertEqual(self.counter.summary()["messages"]["1074"], 1)

    def test_fragmented_and_concatenated_frames_decode_the_same(self):
        whole = msm_epoch(1075, 78_000) + payload_frame(b"\x3e\xe0\xd3\x00" + b"\x00" * 8) + msm_epoch(1095, 18_000)
        one = net.RtcmCounter(); one.feed(whole)
        other = net.RtcmCounter()
        for byte in whole:
            other.feed(bytes([byte]))
        for counter in (one, other):
            self.assertEqual(counter.epoch_age_ms(), 60_000)
            self.assertEqual(counter.summary()["messages"], {"1075": 1, "1006": 1, "1095": 1})
        for length in range(2, 8):      # only a payload with the full 7-byte MSM prefix has an epoch
            counter = net.RtcmCounter()
            counter.feed(payload_frame(b"\x43\x30" + b"\x00" * (length - 2)))
            self.assertEqual(counter.counts, {1075: 1})
            if length < 7:
                self.assertIsNone(counter.epoch_age_ms())
            else:
                self.assertEqual(counter.epoch_age_ms(), 78_000)   # epoch field 0 vs 78 s GPS time of week

    def test_a_crc_ending_in_the_sync_byte_does_not_swallow_the_next_frame(self):
        # The frame ends in 0xD3; if the automaton stops skipping one byte early it
        # treats that byte as a new frame start and loses the following frame.
        for filler in range(65536):
            payload = b"\x43\x30" + b"\x00" * 5 + filler.to_bytes(2, "big")
            if crc24(b"\xd3" + len(payload).to_bytes(2, "big") + payload) & 0xFF == 0xD3:
                break
        else:
            self.fail("no payload with a CRC ending in 0xD3")
        first = payload_frame(payload)
        self.assertEqual(first[-1], 0xD3)
        self.counter.feed(first + msm_epoch(1095, 78_000))
        self.assertEqual(self.counter.counts, {1075: 1, 1095: 1})
        self.assertEqual(self.counter.epoch_age_ms(), 0)

    def test_parser_buffers_at_most_eight_bytes(self):
        for byte in msm_epoch(1075, 78_000):
            self.counter.feed(bytes([byte]))
            self.assertLessEqual(len(self.counter._buf), 8)

    def test_reset_stream_drops_parser_and_epoch_but_keeps_statistics(self):
        frame_bytes = msm_epoch(1075, 18_000)
        self.counter.feed(frame_bytes)
        self.counter.feed(frame_bytes[:9])                   # cut in the middle of a frame
        self.counter.reset_stream()
        self.assertIsNone(self.counter.epoch_age_ms())
        self.assertIsNone(self.counter.summary()["epoch_source"])
        self.assertEqual(self.counter.counts, {1075: 2})
        self.assertEqual(self.counter.bytes_total, len(frame_bytes) + 9)
        self.assertIsNotNone(self.counter.last_msg_ms)
        self.counter.feed(msm_epoch(1095, 78_000))           # parser is back in sync
        self.assertEqual(self.counter.epoch_age_ms(), 0)
        self.assertEqual(self.counter.counts, {1075: 2, 1095: 1})


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


class TestRtcmSilence(unittest.TestCase):
    """rtcm_silence_ms (#20a E6): monotonic uptime since the last recognised message
    number, the same moment arrival_age_sec counts from."""

    def start(self, period=1 << 30, uptime_s=0):
        """A wrapping test clock and a fresh net.app built under it (never the shared one)."""
        self.now, self.period = 500_000, period
        for patcher in (
                mock.patch.object(time, "ticks_ms", side_effect=lambda: self.now % self.period),
                mock.patch.object(time, "ticks_diff", side_effect=lambda a, b: (
                    (a - b + self.period // 2) % self.period - self.period // 2))):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.app = state.AppState()
        self.app._uptime_s = uptime_s
        patcher = mock.patch.object(net, "app", self.app)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.counter = net.RtcmCounter()

    def stamps(self):
        return self.counter.last_msg_ms, self.counter.last_msg_uptime_ms

    def test_silence_counts_from_the_last_recognised_message_number(self):
        self.start()
        self.assertIsNone(self.counter.silence_ms())
        self.assertIsNone(self.counter.last_msg_uptime_ms)
        data = frame(1005, 19)
        self.assertEqual(data[:3], b"\xD3\x00\x13")
        self.now += 2000
        # D3, both length bytes and the first byte of the number: not recognised yet.
        for byte in data[:4]:
            self.counter.feed(bytes([byte]))
            self.assertIsNone(self.counter.last_msg_uptime_ms)
        self.counter.feed(data[4:5])
        self.assertEqual(self.counter.last_msg_uptime_ms, 2000)
        self.assertEqual(self.counter.counts, {1005: 1})
        self.counter.feed(data[5:])
        self.assertEqual(self.counter.last_msg_uptime_ms, 2000)
        # The same frame in three feed parts at the same moment gives the same stamp.
        for part in (data[:3], data[3:10], data[10:]):
            self.counter.feed(part)
        self.assertEqual(self.counter.last_msg_uptime_ms, 2000)
        self.assertEqual(self.counter.counts, {1005: 2})
        self.now += 3500
        self.assertEqual(self.counter.silence_ms(), 3500)
        self.assertEqual(self.counter.summary()["arrival_age_sec"], 3.5)

    def test_too_short_and_unfinished_frames_leave_the_stamps(self):
        self.start()
        self.counter.feed(b"\xD3\x00\x01")            # length 1: too short for a number
        self.counter.feed(b"\xD3\x00\x13")            # header only, then the stream ends
        self.assertEqual(self.stamps(), (None, None))
        self.assertEqual(self.counter.counts, {})
        self.assertIsNone(self.counter.summary()["arrival_age_sec"])
        self.assertIsNone(self.counter.silence_ms())

        self.start()
        self.counter.feed(frame(1005, 19))
        before = self.stamps()
        self.now += 1000
        self.counter.feed(b"\xD3\x00\x01")
        self.counter.feed(b"\xD3\x00\x13\x3E")        # ends after the first number byte
        self.assertEqual(self.stamps(), before)
        self.assertEqual(self.counter.counts, {1005: 1})
        self.assertEqual(self.counter.summary()["arrival_age_sec"], 1.0)
        self.assertEqual(self.counter.silence_ms(), 1000)

    def test_bytes_after_an_unfinished_header_are_its_message_number(self):
        # No validation beyond the length field: the next two bytes of the same
        # stream are read as the number, and both stamps move with it.
        self.start()
        self.counter.feed(frame(1005, 19))
        self.counter.feed(b"\xD3\x00\x13")
        self.now += 1000
        self.counter.feed(b"XY")
        self.assertEqual(self.counter.counts, {1005: 1, 1413: 1})
        self.assertEqual(self.stamps(), (501_000, 1000))
        self.assertEqual(self.counter.silence_ms(), 0)
        self.assertEqual(self.counter.summary()["arrival_age_sec"], 0.0)

    def test_reset_stream_drops_a_partial_frame_and_keeps_the_stamps(self):
        self.start()
        self.counter.feed(frame(1005, 19))
        before = self.stamps()
        self.counter.feed(b"\xD3\x00\x13")
        self.now += 1000
        self.counter.reset_stream()                    # a new connection
        self.counter.feed(b"XY")
        self.assertEqual(self.counter.counts, {1005: 1})
        self.assertEqual(self.stamps(), before)
        self.assertEqual(self.counter.silence_ms(), 1000)
        self.assertEqual(self.counter.summary()["arrival_age_sec"], 1.0)

    def test_rtcm_silence_survives_a_full_tick_cycle(self):
        period = 1 << 20
        # 23 days up: the uptime in ms is past 2**30, MicroPython's small-int range.
        self.start(period=period, uptime_s=2_000_000)
        self.counter.feed(frame(1005, 19))
        self.assertEqual(self.counter.last_msg_uptime_ms, 2_000_000_000)

        def small_ints_only():
            # The per-message stamp is held in small ints: no bigint per message.
            for value in vars(self.counter).values():
                if isinstance(value, int):
                    self.assertLess(abs(value), 1 << 30)
        small_ints_only()
        previous = self.counter.silence_ms()
        self.assertEqual(previous, 0)
        for _ in range(3 * period // 2 // 1000 + 1):
            self.now += 1000
            self.app.uptime_ms()                       # what the watchdog task does
            silence = self.counter.silence_ms()
            self.assertIsNotNone(silence)
            self.assertGreater(silence, previous)
            previous = silence
        self.assertGreaterEqual(previous, 3 * period // 2)
        small_ints_only()


if __name__ == "__main__":
    unittest.main()
