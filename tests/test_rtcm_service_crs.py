# SPDX-License-Identifier: AGPL-3.0-only
"""#38b Task 2: RtcmCounter decodes the service CRS of RTCM 1300 and the RTCM CRS
of 1302 from CRC-valid frames only. Field layout (payload from bit 0):
1300 = DF002 12 | DF562 N 5 | DF563 8N | DF564 CE 16;
1302 = DF002 12 | DF565 N 5 | DF566 8N | DF567 1 | DF149 5 | DF568 3 | links."""
import random
import time
import unittest
from unittest import mock

from support import net, state
import cfg_validation
import tracking
from test_field_diagnostics import crc24, frame
from test_rtcm import SUNDAY_0001, msm_epoch


def _pack(bits):
    bits += "0" * ((-len(bits)) % 8)
    return int(bits, 2).to_bytes(len(bits) // 8, "big")


def _chars(name):
    raw = name.encode("latin-1") if isinstance(name, str) else bytes(name)
    return format(len(raw), "05b") + "".join(format(c, "08b") for c in raw)


def _finish(payload, payload_len):
    if payload_len is not None:
        payload = payload[:payload_len]
    return frame(payload)


def crs_1300(name, ce, extra=b"", payload_len=None):
    """CRC-valid 1300 frame; `extra` appends payload bytes, `payload_len` cuts the
    payload before the CRC."""
    bits = format(1300, "012b") + _chars(name) + format(ce, "016b")
    return _finish(_pack(bits) + extra, payload_len)


def crs_1302(name, anchor, plate, links=(), payload_len=None):
    """CRC-valid 1302 frame; each database link is a 5-bit length plus characters."""
    bits = (format(1302, "012b") + _chars(name) + str(anchor) + format(plate, "05b")
            + format(len(links), "03b"))
    for link in links:
        bits += _chars(link)
    return _finish(_pack(bits), payload_len)


def flip_payload_bit(data, index, bit=0):
    """Copy of frame `data` with one bit of payload byte `index` inverted."""
    bad = bytearray(data)
    bad[3 + index] ^= 1 << bit
    return bytes(bad)


LONG_1302_LINKS = ("EPSG::4258",) + ("X" * 31,) * 6

# CRC24Q, byte table, for the long generated stream (the bitwise helper is too slow).
_CRC_TABLE = []
for _value in range(256):
    _crc = _value << 16
    for _ in range(8):
        _crc <<= 1
        if _crc & 0x1000000:
            _crc ^= 0x1864CFB
    _CRC_TABLE.append(_crc & 0xFFFFFF)


def _fast_frame(payload):
    prefix = b"\xd3" + len(payload).to_bytes(2, "big") + payload
    crc = 0
    for byte in prefix:
        crc = ((crc << 8) & 0xFFFFFF) ^ _CRC_TABLE[(crc >> 16) ^ byte]
    return prefix + crc.to_bytes(3, "big")


# Message types of today's caster with plausible payload lengths in bytes.
TODAYS_CASTER_TYPES = ((1006, (21, 21)), (1008, (6, 40)), (1013, (9, 70)),
                       (1033, (20, 80)), (1075, (100, 300)), (1085, (100, 300)),
                       (1095, (100, 300)), (1125, (100, 300)))


def todays_caster_stream(seed, rounds=2000):
    """`rounds` x each type of today's caster (no 1300/1302), CRC-valid frames with
    random payload after the message number (0xD3 included), as one byte string."""
    rng = random.Random(seed)
    frames = []
    for _ in range(rounds):
        for number, (low, high) in TODAYS_CASTER_TYPES:
            payload = bytearray(rng.randbytes(rng.randint(low, high)))
            payload[0] = number >> 4
            payload[1] = ((number & 0x0F) << 4) | (payload[1] & 0x0F)
            frames.append(_fast_frame(bytes(payload)))
    return b"".join(frames)


def random_chunks(rng, data, largest=1024):
    position = 0
    while position < len(data):
        size = rng.randint(1, largest)
        yield data[position:position + size]
        position += size


NO_FAILURES = {"crc_failures": 0, "rejected": 0}


class CrsCase(unittest.TestCase):
    def setUp(self):
        self.now = 500_000
        for patcher in (mock.patch.object(time, "gmtime", return_value=SUNDAY_0001),
                        mock.patch.object(time, "ticks_ms",
                                          side_effect=lambda: self.now)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.app = state.AppState()
        patcher = mock.patch.object(net, "app", self.app)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.counter = net.RtcmCounter()

    def frames(self):
        return self.counter.crs_summary()["crs_frames"]


class TestServiceCrsDecoding(CrsCase):
    def test_service_crs_is_decoded_from_a_crc_valid_1300(self):
        self.counter.feed(crs_1300("ETRS89", 11000))
        self.assertEqual(self.counter.service_crs,
                         {"name": "ETRS89", "epoch": "2010.0", "epoch_raw": 11000})
        self.assertEqual(self.counter.counts, {1300: 1})
        self.assertEqual(self.frames(), NO_FAILURES)
        # The CRC covers the reserved bits of the length field as sent.
        payload = crs_1300("ITRF2014", 11000)[3:-3]
        prefix = bytes([0xD3, 0xA8, len(payload)]) + payload
        counter = net.RtcmCounter()
        counter.feed(prefix + crc24(prefix).to_bytes(3, "big"))
        self.assertEqual(counter.service_crs["name"], "ITRF2014")
        self.assertEqual(counter.crs_summary()["crs_frames"], NO_FAILURES)

    def test_epoch_text_and_bounds(self):
        cases = ((11025, "2010.25"), (11050, "2010.5"), (0, None), (8799, None),
                 (8800, "1988.0"), (20000, "2100.0"), (20001, None))
        for ce, text in cases:
            counter = net.RtcmCounter()
            counter.feed(crs_1300("ETRS89", ce))
            self.assertEqual(counter.service_crs,
                             {"name": "ETRS89", "epoch": text, "epoch_raw": ce}, ce)
            self.assertEqual(net._crs_epoch_text(ce), text)

    def test_rtcm_crs_is_decoded_from_1302_with_long_database_links(self):
        long_frame = crs_1302("ETRS89", 1, 7, links=LONG_1302_LINKS)
        self.assertGreater(len(long_frame) - 6, 200)
        self.counter.feed(long_frame)
        self.assertEqual(self.counter.rtcm_crs, {"name": "ETRS89", "anchor": "plate_fixed",
                                                 "plate": 7, "database_links": 7})
        self.assertIsNone(self.counter.service_crs)
        self.counter.feed(crs_1302("ITRF2014", 0, 0))
        self.assertEqual(self.counter.rtcm_crs, {"name": "ITRF2014", "anchor": "global",
                                                 "plate": 0, "database_links": 0})
        self.assertEqual(self.counter.counts, {1302: 2})
        self.assertEqual(self.frames(), NO_FAILURES)

    def test_external_vectors_decode_as_the_independent_decoder_reports(self):
        # Origin (E14, ruling F6): no capture of a real 1300 service was available.
        # Both frames were built outside the repository with a bit encoder of the
        # layout above and decoded once with pyrtcm 1.2.1 (pip download pyrtcm==1.2.1
        # and pynmeagps 1.1.8 --no-deps into a scratch directory, python3 -I, CRC
        # validation on); its output, copied unchanged:
        #   <RTCM(1300, DF002=1300, DF562=8, DF563=ETRF2000, DF564=120.25)>
        #   <RTCM(1302, DF002=1302, DF565=8, DF566=ETRF2000, DF567=1, DF149=7,
        #    DF568=1, DF569_01=10, DF570=EPSG::4258)>
        # pyrtcm scales DF564 by 0.01, so 120.25 is the raw value CE = 12025.
        vector_1300 = bytes.fromhex("d3000d514422aa292319181818177c800e2cde")
        vector_1302 = bytes.fromhex(
            "d30016516422aa2923191818184e548aa0a68e747468646a7072af1d")
        self.counter.feed(vector_1300 + vector_1302)
        self.assertEqual(self.counter.service_crs,
                         {"name": "ETRF2000", "epoch": "2020.25", "epoch_raw": 12025})
        self.assertEqual(self.counter.rtcm_crs, {"name": "ETRF2000", "anchor": "plate_fixed",
                                                 "plate": 7, "database_links": 1})
        self.assertEqual(self.counter.counts, {1300: 1, 1302: 1})
        self.assertEqual(self.frames(), NO_FAILURES)
        # The test helpers encode the same layout.
        self.assertEqual(crs_1300("ETRF2000", 12025), vector_1300)
        self.assertEqual(crs_1302("ETRF2000", 1, 7, links=("EPSG::4258",)), vector_1302)

    def test_byte_by_byte_feeding_decodes_the_same(self):
        msm_a, msm_b = msm_epoch(1075, 78_000), msm_epoch(1085, 3_600_000)
        stream = (msm_a + crs_1300("ETRS89", 11050) + msm_b
                  + crs_1302("ETRS89", 1, 7, links=LONG_1302_LINKS) + msm_a)
        whole, bytewise, plain = net.RtcmCounter(), net.RtcmCounter(), net.RtcmCounter()
        whole.feed(stream)
        for byte in stream:
            bytewise.feed(bytes([byte]))
        plain.feed(msm_a + msm_b + msm_a)
        self.assertEqual(whole.service_crs,
                         {"name": "ETRS89", "epoch": "2010.5", "epoch_raw": 11050})
        self.assertEqual(whole.rtcm_crs["database_links"], 7)
        self.assertEqual(bytewise.service_crs, whole.service_crs)
        self.assertEqual(bytewise.rtcm_crs, whole.rtcm_crs)
        self.assertEqual(bytewise.counts, whole.counts)
        self.assertEqual(whole.counts, {1075: 2, 1085: 1, 1300: 1, 1302: 1})
        self.assertIsNotNone(plain.epoch_age_ms())
        for counter in (whole, bytewise):
            self.assertEqual(counter.epoch_age_ms(), plain.epoch_age_ms())
            self.assertEqual(counter.last_epoch_source, plain.last_epoch_source)
            self.assertEqual(counter.crs_summary()["crs_frames"], NO_FAILURES)


class TestCrsFrameChecks(CrsCase):
    def test_crc_failure_is_counted_and_ignored(self):
        good = crs_1300("ETRS89", 11000)
        self.counter.feed(flip_payload_bit(good, 4))
        self.assertIsNone(self.counter.service_crs)
        self.assertEqual(self.frames(), {"crc_failures": 1, "rejected": 0})
        self.assertEqual(self.counter.counts, {1300: 1})
        self.counter.feed(good)
        self.assertEqual(self.counter.service_crs["name"], "ETRS89")
        self.assertEqual(self.frames(), {"crc_failures": 1, "rejected": 0})

    def test_crc_failure_behind_the_capture_window_of_1302(self):
        long_frame = crs_1302("ETRS89", 1, 7, links=LONG_1302_LINKS)
        self.counter.feed(flip_payload_bit(long_frame, 100, 3))
        self.assertIsNone(self.counter.rtcm_crs)
        self.assertEqual(self.frames(), {"crc_failures": 1, "rejected": 0})
        self.assertEqual(self.counter.counts, {1302: 1})

    def test_malformed_frames_are_rejected(self):
        malformed = (
            crs_1300("", 11000),
            crs_1300(b"ETRS\x0789", 11000),
            crs_1300(b"ETRS\xc389", 11000),
            crs_1300("   ", 11000),
            crs_1300("Unknown", 11000),
            crs_1300("MIXED", 11000),
            crs_1300("ETRS89", 11000, payload_len=10),     # 80 < 33 + 8 * 6 bits
            crs_1302("A" * 31, 1, 7, payload_len=34),       # 272 < 26 + 8 * 31 bits
            crs_1302("unknown", 0, 0),
            crs_1300("ETRS89", 11000, extra=bytes(30)),     # 41-byte payload
        )
        self.assertEqual(len(crs_1300("ETRS89", 11000, extra=bytes(30))) - 6, 41)
        for count, data in enumerate(malformed, 1):
            self.counter.feed(data)
            self.assertIsNone(self.counter.service_crs, count)
            self.assertIsNone(self.counter.rtcm_crs, count)
            self.assertEqual(self.frames(), {"crc_failures": 0, "rejected": count})
        self.assertEqual(self.counter.counts, {1300: 8, 1302: 2})

    def test_a_valid_but_unusable_announcement_withdraws_the_previous_one(self):
        self.counter.feed(crs_1300("ETRS89", 11000) + crs_1302("ETRS89", 1, 7))
        self.assertIsNotNone(self.counter.service_crs)
        self.assertIsNotNone(self.counter.rtcm_crs)
        self.counter.feed(crs_1300("unknown", 11000))
        self.assertIsNone(self.counter.service_crs)
        self.assertIsNotNone(self.counter.rtcm_crs)
        self.assertEqual(self.frames(), {"crc_failures": 0, "rejected": 1})
        self.counter.feed(crs_1302("unknown", 1, 7))
        self.assertIsNone(self.counter.rtcm_crs)
        self.assertEqual(self.frames(), {"crc_failures": 0, "rejected": 2})
        self.assertIsNone(self.counter.crs_summary()["service_crs"])
        # The withdrawn announcement comes back with the next valid frame.
        self.counter.feed(crs_1300("ETRS89", 11000))
        self.assertEqual(self.counter.service_crs["name"], "ETRS89")

    def test_a_corrupted_announcement_keeps_the_previous_one(self):
        good = crs_1300("ETRS89", 11000)
        self.counter.feed(good)
        previous = self.counter.service_crs
        self.counter.feed(flip_payload_bit(good, 3, 5))
        self.assertIs(self.counter.service_crs, previous)
        self.assertEqual(self.frames(), {"crc_failures": 1, "rejected": 0})

    def test_identical_frames_keep_the_object_and_a_change_replaces_it(self):
        self.counter.feed(crs_1300("ETRS89", 11000))
        first = self.counter.service_crs
        self.now += 4000
        self.assertEqual(self.counter.crs_summary()["service_crs"]["age_sec"], 4.0)
        self.counter.feed(crs_1300("ETRS89", 11000))
        self.assertIs(self.counter.service_crs, first)
        self.assertEqual(self.counter.crs_summary()["service_crs"]["age_sec"], 0.0)
        self.now += 1300
        self.assertEqual(self.counter.crs_summary()["service_crs"]["age_sec"], 1.3)
        self.counter.feed(crs_1300("ETRS89/DREF91", 11000))
        self.assertIsNot(self.counter.service_crs, first)
        self.assertEqual(self.counter.service_crs,
                         {"name": "ETRS89/DREF91", "epoch": "2010.0", "epoch_raw": 11000})

    def test_reset_stream_and_drop_crs_clear_the_announcements_but_keep_counters(self):
        good = crs_1300("ETRS89", 11000)
        for clear in ("drop_crs", "reset_stream"):
            counter = net.RtcmCounter()
            counter.feed(good + crs_1302("ETRS89", 1, 7) + flip_payload_bit(good, 4))
            counts = dict(counter.counts)
            getattr(counter, clear)()
            self.assertIsNone(counter.service_crs, clear)
            self.assertIsNone(counter.rtcm_crs, clear)
            self.assertEqual(counter.counts, counts)
            summary = counter.crs_summary()
            self.assertEqual(summary, {"service_crs": None, "rtcm_crs": None,
                                       "crs_frames": {"crc_failures": 1, "rejected": 0}})
            # The same announcement on the next stream is decoded again.
            counter.feed(good)
            self.assertEqual(counter.service_crs["name"], "ETRS89", clear)
        counter = net.RtcmCounter()
        counter.feed(good[:-5])
        counter.reset_stream()
        counter.feed(good[-5:])
        self.assertIsNone(counter.service_crs)
        self.assertEqual(counter.crs_summary()["crs_frames"], NO_FAILURES)

    def test_todays_caster_stream_yields_no_crs(self):
        rng = random.Random(3811)
        stream = todays_caster_stream(3810)
        for chunk in random_chunks(rng, stream):
            self.counter.feed(chunk)
        self.assertEqual(self.counter.counts,
                         dict((number, 2000) for number, _ in TODAYS_CASTER_TYPES))
        self.assertIsNone(self.counter.service_crs)
        self.assertIsNone(self.counter.rtcm_crs)
        self.assertEqual(self.counter.crs_summary(),
                         {"service_crs": None, "rtcm_crs": None, "crs_frames": NO_FAILURES})

    def test_no_buffer_allocation_for_repeated_crs_frames(self):
        pair = crs_1300("ETRS89", 11000) + msm_epoch(1075, 78_000)
        self.counter.feed(pair)
        first = self.counter.service_crs
        stream = pair * 200
        chunks = [stream[i:i + 512] for i in range(0, len(stream), 512)]
        with mock.patch.object(net, "bytearray", side_effect=bytearray,
                               create=True) as made_bytearray, \
                mock.patch.object(net, "bytes", side_effect=bytes,
                                  create=True) as made_bytes, \
                mock.patch.object(net, "_crs_name", wraps=net._crs_name) as name:
            for chunk in chunks:
                self.counter.feed(chunk)
        self.assertEqual(self.counter.counts, {1300: 201, 1075: 201})
        self.assertIs(self.counter.service_crs, first)
        self.assertEqual(made_bytearray.call_count, 0)
        self.assertEqual(made_bytes.call_count, 0)
        self.assertEqual(name.call_count, 0)

    def test_accepted_names_pass_the_field_setup_validator(self):
        for name in ("ETRS89", '"' * 31, "\\" * 31, "<b>x</b>", " ITRF2014 "):
            counter = net.RtcmCounter()
            counter.feed(crs_1300(name, 11000))
            decoded = counter.service_crs["name"]
            self.assertEqual(decoded, name.strip())
            clean, error = cfg_validation.validate_field_setup({"reference_frame": decoded})
            self.assertIsNone(error, name)
            self.assertEqual(clean["reference_frame"], decoded)

    def test_reserved_names_match_tracking(self):
        self.assertEqual(net._RESERVED_FRAME_NAMES, tracking.RESERVED_FRAME_NAMES)


if __name__ == "__main__":
    unittest.main()
