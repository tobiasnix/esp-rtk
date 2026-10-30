# SPDX-License-Identifier: AGPL-3.0-only
"""#38b Task 1: RtcmCounter.feed parses the correction stream without a buffer per
frame and behaves byte for byte like the frozen reference copy of 08c5928
(tests/rtcm_reference.py) for every input and every chunking."""
import random
import unittest
from unittest import mock

from support import net
import rtcm_reference
from test_field_diagnostics import frame as crc_frame
from test_rtcm import SUNDAY_0001, msm_epoch

MSM_NUMBERS = (1075, 1077, 1085, 1095, 1125, 1137)
MSM_PAYLOAD_LENGTHS = (2, 6, 7, 8, 40, 300)
OTHER_NUMBERS = (1005, 1006, 1008, 1013, 1033, 1230, 1300, 1301, 1302)


def raw_frame(rng, number, length, valid_crc):
    """Frame with message number `number` and `length` payload bytes; payload bytes
    after the number are random (0xD3 included), the CRC valid or random."""
    payload = bytearray(rng.getrandbits(8) for _ in range(length))
    if length >= 1:
        payload[0] = (number >> 4) & 0xFF
    if length >= 2:
        payload[1] = ((number & 0x0F) << 4) | (payload[1] & 0x0F)
    if valid_crc:
        return crc_frame(bytes(payload))
    reserved = rng.getrandbits(6) << 2 if rng.random() < 0.2 else 0
    head = bytes([0xD3, reserved | ((length >> 8) & 0x03), length & 0xFF])
    return head + bytes(payload) + bytes(rng.getrandbits(8) for _ in range(3))


def msm_frame(rng, number, length):
    """MSM frame with a random epoch: mostly within a week (or a GLONASS day/tod
    pair), sometimes any 30-bit value; truncated to `length` payload bytes."""
    if rng.random() < 0.2:
        epoch = rng.getrandbits(30)
    elif number // 10 == 108:
        epoch = (rng.randrange(8) << 27) | rng.randrange(net.DAY_MS)
    else:
        epoch = rng.randrange(net.WEEK_MS)
    full = msm_epoch(number, epoch)[3:-3]
    payload = bytearray(full[:length])
    while len(payload) < length:
        payload.append(rng.getrandbits(8))
    if rng.random() < 0.5:
        return crc_frame(bytes(payload))
    head = bytes([0xD3, (length >> 8) & 0x03, length & 0xFF])
    return head + bytes(payload) + bytes(rng.getrandbits(8) for _ in range(3))


def random_piece(rng):
    kind = rng.random()
    if kind < 0.45:
        return msm_frame(rng, rng.choice(MSM_NUMBERS), rng.choice(MSM_PAYLOAD_LENGTHS))
    if kind < 0.75:
        return raw_frame(rng, rng.choice(OTHER_NUMBERS), rng.randrange(2, 80),
                         rng.random() < 0.5)
    if kind < 0.82:
        # Length field 0 or 1: too short for a message number.
        length = rng.choice((0, 1))
        return bytes([0xD3, 0x00, length]) + bytes(rng.getrandbits(8)
                                                    for _ in range(length + 3))
    if kind < 0.92:
        # Filler with plenty of 0xD3.
        return bytes(0xD3 if rng.random() < 0.3 else rng.getrandbits(8)
                     for _ in range(rng.randrange(1, 40)))
    whole = msm_frame(rng, rng.choice(MSM_NUMBERS), rng.choice(MSM_PAYLOAD_LENGTHS))
    return whole[:rng.randrange(1, len(whole))]        # truncated frame


def random_stream(rng, pieces):
    return b"".join(random_piece(rng) for _ in range(pieces))


def observable(counter):
    return {
        "counts": dict(counter.counts),
        "bytes_total": counter.bytes_total,
        "last_msg_ms": counter.last_msg_ms,
        "_msg_uptime_s": counter._msg_uptime_s,
        "_msg_uptime_ms": counter._msg_uptime_ms,
        "last_epoch_age_ms": counter.last_epoch_age_ms,
        "last_epoch_ticks_ms": counter.last_epoch_ticks_ms,
        "last_epoch_source": counter.last_epoch_source,
        "summary": counter.summary(),
        "epoch_age_ms": counter.epoch_age_ms(),
    }


class TestFeedMatchesTheFrozenReference(unittest.TestCase):
    def setUp(self):
        self.now = 500_000
        for patch in (mock.patch.object(net.time, "gmtime", return_value=SUNDAY_0001),
                      mock.patch.object(net.time, "ticks_ms",
                                        side_effect=lambda: self.now)):
            patch.start()
            self.addCleanup(patch.stop)

    def test_random_streams_match_the_frozen_reference(self):
        rng = random.Random(3801)
        for index in range(300):
            stream = random_stream(rng, rng.randrange(1, 60))
            new, old = net.RtcmCounter(), rtcm_reference.RtcmCounter()
            position = 0
            while position < len(stream):
                if rng.random() < 0.05:
                    new.reset_stream()
                    old.reset_stream()
                size = rng.randrange(1, 701)
                chunk = stream[position:position + size]
                position += size
                self.now += rng.randrange(0, 2000)
                new.feed(chunk)
                old.feed(chunk)
                self.assertEqual(observable(new), observable(old),
                                 "stream %d at byte %d" % (index, position))

    def test_one_byte_and_whole_stream_feeding_agree(self):
        rng = random.Random(3802)
        stream = b""
        while len(stream) < 20_000:
            stream += random_stream(rng, 20)
        stream = stream[:20_000]
        whole, bytewise = net.RtcmCounter(), net.RtcmCounter()
        reference = rtcm_reference.RtcmCounter()
        whole.feed(stream)
        reference.feed(stream)
        for byte in stream:
            bytewise.feed(bytes([byte]))
        self.assertGreater(sum(whole.counts.values()), 100)
        self.assertEqual(observable(whole), observable(reference))
        self.assertEqual(observable(bytewise), observable(reference))


class TestFeedBuffers(unittest.TestCase):
    def test_feed_allocates_no_buffer_per_frame(self):
        """No new frame buffers; the gmtime tuple per MSM is outside this contract."""
        rng = random.Random(3803)
        frames = []
        for index in range(500):
            if index % 2:
                frames.append(msm_frame(rng, rng.choice(MSM_NUMBERS), 120))
            else:
                frames.append(raw_frame(rng, rng.choice(OTHER_NUMBERS), 30, True))
        stream = b"".join(frames)
        chunks = [stream[i:i + 512] for i in range(0, len(stream), 512)]
        counter = net.RtcmCounter()
        with mock.patch.object(net, "bytearray", side_effect=bytearray,
                               create=True) as made_bytearray, \
                mock.patch.object(net, "bytes", side_effect=bytes,
                                  create=True) as made_bytes:
            for chunk in chunks:
                counter.feed(chunk)
        self.assertEqual(sum(counter.counts.values()), 500)
        self.assertEqual(made_bytearray.call_count, 0)
        self.assertEqual(made_bytes.call_count, 0)

    def test_header_buffer_is_fixed(self):
        counter = net.RtcmCounter()
        buf0 = counter._buf
        for byte in msm_epoch(1075, 78_000):
            counter.feed(bytes([byte]))
            self.assertIs(counter._buf, buf0)
            self.assertEqual(len(counter._buf), 7)
        self.assertEqual(counter.counts, {1075: 1})


if __name__ == "__main__":
    unittest.main()
