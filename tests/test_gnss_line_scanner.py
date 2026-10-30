# SPDX-License-Identifier: AGPL-3.0-only
"""The NMEA line scanner of GNSSHandler.run against the rules of 08c5928 (#28c).

The scanner reads lines through a position in one bytes buffer instead of
splitting off the rest per line, and hands clean CRLF lines to the queue as
received. Every sentence must still be accepted, rejected, counted and queued
exactly as 08c5928 did, also when the stream arrives in odd chunk sizes, when
the loop reads more bytes between lines and when the buffer overflows.
"""
import asyncio
import random
import tracemalloc
import unittest
from unittest import mock

from support import gnss, sentence
import nmea_reference

CLOCK_MS = 5000
CHUNK_SIZES = (1, 7, 64, 333, 512, 4096)
GGA_BODY = "GNGGA,%s,4807.038123,N,01131.000456,E,4,24,0.6,545.412,M,46.903,M,1.0,0001"


def _line(body, end=b"\r\n", lead=b"", trail=b""):
    """One sentence as bytes; ``body`` may hold raw non-ASCII bytes."""
    data = body if isinstance(body, bytes) else body.encode()
    value = 0
    for byte in data:
        value ^= byte
    return lead + b"$" + data + (b"*%02X" % value) + trail + end


def _vtg(index):
    return "GNVTG,%d.00,T,,M,0.02,N,0.04,K,R" % index


def _corpus():
    """Every filter and error path of the line loop, then 40 GSV for the limit."""
    lines = []
    for index in range(12):
        utc = "1235%02d.00" % index
        lines += [
            _line(GGA_BODY % utc),
            _line("GNRMC,%s,A,4807.038123,N,01131.000456,E,0.02,45.00,011026,,,R,V" % utc),
            _line("GNGSA,A,3,01,02,03,04,05,06,07,08,09,10,11,12,1.2,0.6,1.0,1"),
            _line("GPGSV,3,1,12,%02d,30,100,40" % index),
            _line("GNGST,%s,0.8,0.012,0.010,45.0,0.011,0.013,0.020" % utc),
            _line(_vtg(index)),
            _line(_vtg(index), end=b"\n"),                      # LF only
            _line(_vtg(index), end=b"\r\r\n"),
            _line(_vtg(index), lead=b"  ", trail=b" \t"),       # spaces around
            _line(_vtg(index), lead=b" ", end=b"\n"),           # two bytes stripped, no CRLF
            b"\r\n", b"\n", b"   \r\n",                         # empty lines
            _line("PQTMVERNO,LC29H,%d,OK" % index),              # command reply
            _line("PAIR001,%03d,0" % index),
            _line("GNVTG,1.00,T,,M,0.02,N,0.04,K,R")[:-5] + b"00\r\n",   # wrong CRC
            _line(b"GNGGA,\xff" + (GGA_BODY % utc).encode()[6:]),         # 0xFF whitelist
            _line(b"GNGSA,A,3,01,\xc3\xa4,03,04,05,06,07,08,09,10,11,12,1.2,0.6,1.0,1"),
            _line(b"GNXYZ,\xff,1"),                               # 0xFF foreign
            _line(b"PQTMXYZ,\xc3\xa4,1"),                         # UTF-8 pair, foreign reply
            _line(b"G\xffRMC,%s,V,,,,,,,011026,,,N,V" % utc.encode()),   # bad talker byte
            _line(b"G\xffGGA,%s,,,,,0,00,,,M,,M,," % utc.encode()),     # type shifts by one
            b"garbage without a sentence start\r\n",
            _line("GNVTG,1.00,T,,M,0.02,N,0.04,K,R").replace(b"*", b"**", 1),  # two stars
        ]
    lines += [_line("GLGSV,3,2,12,%02d,30,100,40" % index) for index in range(40)]
    lines.append(_line(GGA_BODY % "123600.00"))
    # The unterminated tail never reaches the queue.
    return b"".join(lines) + b"$GNGGA,123601.00,4807"


def reference_outputs(stream, parse_gga):
    """The rules of 08c5928 applied to the whole stream at once."""
    out = {"queue": [], "crc": [], "replies": [], "utc": [], "rmc": []}
    gsv_count = 0
    pieces = stream.split(b"\n")
    offset = 0
    for raw_line in pieces[:-1]:
        offset += len(raw_line) + 1
        ascii_error = any(byte > 127 for byte in raw_line)
        line_str = raw_line.decode("ascii", "ignore").strip()
        if not line_str or not line_str.startswith("$"):
            continue
        nmea_type = line_str[3:6]
        if ascii_error and nmea_type not in gnss.NMEA_WHITELIST:
            raw_type = raw_line.strip()[3:6].decode("ascii", "ignore")
            if raw_type in gnss.NMEA_WHITELIST:
                nmea_type = raw_type
        if nmea_type not in gnss.NMEA_WHITELIST:
            if line_str.startswith(gnss.COMMAND_PREFIXES):
                out["replies"] = (out["replies"] + [line_str])[-gnss.REPLY_MAX:]
            continue
        if ascii_error or not nmea_reference.check_nmea_checksum(line_str):
            out["crc"].append((raw_line, len(stream) - offset))
            continue
        if nmea_type == "GSV":
            # The clock stands still: the one-second window never reopens.
            if gsv_count >= gnss.CONFIG["gsv_limit_per_sec"]:
                continue
            gsv_count += 1
        out["queue"].append((line_str + "\r\n").encode())
        if nmea_type == "GGA":
            fix = parse_gga(line_str)
            if fix:
                out["utc"].append(fix["utc"])
        elif nmea_type == "RMC":
            out["rmc"].append(line_str)
    return out


class ChunkUART:
    """Delivers one prepared chunk per read, also to reads inside the loop."""

    def __init__(self, chunks):
        self.chunks = list(chunks)

    def any(self):
        return len(self.chunks[0]) if self.chunks else 0

    def read(self, n=None):
        return self.chunks.pop(0) if self.chunks else None

    def write(self, data):
        return len(data)


class Queue:
    def __init__(self):
        self.items = []

    def put_nowait(self, data):
        self.items.append(data)
        return True

    def qsize(self):
        return len(self.items)


def _chunks(stream, sizes=CHUNK_SIZES):
    chunks, pos, index = [], 0, 0
    while pos < len(stream):
        size = sizes[index % len(sizes)]
        chunks.append(stream[pos:pos + size])
        pos += size
        index += 1
    return chunks


class TestGnssLineScanner(unittest.IsolatedAsyncioTestCase):
    async def scan(self, chunks, update_fix=None):
        """Run the real handler until the prepared chunks are consumed."""
        stop = asyncio.Event()
        uart = ChunkUART(chunks)
        queue = Queue()
        fixes, rmc = [], []
        replies = []

        async def turn(ms):
            # A read in the loop yields with 0; the outer loop sleeps longer.
            if ms and not uart.chunks:
                stop.set()
            await asyncio.sleep(0)

        async def no_pause(seconds):
            await asyncio.sleep(0)

        def record_fix(fix):
            fixes.append(fix["utc"])
            if update_fix:
                update_fix(fix)

        base = {"crc_errors": 0, "queue_overflows": 0, "gga_parsed": 0, "gnss_msgs": 0}
        with mock.patch.dict(gnss.app.stats, base, clear=True), \
                mock.patch.object(gnss, "shutdown_event", stop), \
                mock.patch.object(gnss, "replies", replies), \
                mock.patch.object(gnss, "log"), \
                mock.patch.object(gnss.time, "ticks_ms", return_value=CLOCK_MS), \
                mock.patch.object(gnss.asyncio, "sleep_ms", turn), \
                mock.patch.object(gnss.asyncio, "sleep", no_pause), \
                mock.patch.object(gnss.app, "update_fix", side_effect=record_fix), \
                mock.patch.object(gnss.app, "attach_gst"), \
                mock.patch.object(gnss.app, "log_error"), \
                mock.patch.object(gnss.field_recorder, "observe_nmea"):
            handler = gnss.GNSSHandler(queue)
            handler.uart = uart
            with mock.patch.object(handler, "_sync_rmc_clock", side_effect=rmc.append):
                await asyncio.wait_for(handler.run(), 5)
            return {"queue": queue.items, "utc": fixes, "rmc": rmc, "replies": replies,
                    "stats": dict(gnss.app.stats)}

    def reference(self, stream):
        with mock.patch.dict(gnss.app.stats, {}, clear=True):
            handler = gnss.GNSSHandler(Queue())
            return reference_outputs(stream, handler.parse_gga)

    async def test_scanner_matches_the_line_split_reference(self):
        stream = _corpus()
        expected = self.reference(stream)
        self.assertGreater(len(expected["crc"]), 50)
        self.assertGreater(len(expected["replies"]), 5)
        self.assertEqual(len(expected["utc"]), 13)
        last_raw, last_pending = expected["crc"][-1]
        for name, chunks in (("cycled sizes", _chunks(stream)), ("one chunk", [stream])):
            with self.subTest(name):
                result = await self.scan(chunks)
                stats = result["stats"]
                self.assertEqual(result["queue"], expected["queue"])
                self.assertEqual(stats["crc_errors"], len(expected["crc"]))
                self.assertEqual(stats["nmea_last_crc_error"]["raw_hex"],
                                 last_raw[:gnss.CRC_RAW_CAPTURE_BYTES].hex())
                self.assertEqual(result["replies"], expected["replies"])
                self.assertEqual(result["utc"], expected["utc"])
                self.assertEqual(result["rmc"], expected["rmc"])
                self.assertEqual(stats.get("uart_buffer_overflows", 0), 0)
                if name == "one chunk":
                    # Bytes after the failing line; only fixed for a single chunk.
                    self.assertEqual(stats["nmea_last_crc_error"]["pending_python_bytes"],
                                     last_pending)

    def test_checksum_matches_the_reference(self):
        rng = random.Random(28)
        alphabet = "GNPAQTM0123456789.,-+*$ ABCDEFabcdef_x\u00e4\u0661"
        valid = [sentence(GGA_BODY % "123519.00"), sentence(_vtg(4)),
                 sentence("PQTMVERNO,LC29H,1,OK"), sentence("GNGSA,A,3,01,02,,1.2,0.6,1.0,1")]
        cases = ["", "$", "*", "$*", "$*00", "*00", "  ", "$GNVTG", "$GNVTG*",
                 "$GNVTG**00", "$GNVTG*00*", "$GNVTG,1*5b", "$GNVTG,1*0x5B", "$GNVTG,1*5_B",
                 "$GNVTG,1* 5B", "$GNVTG,1*5B ", " $GNVTG,1*5B", "GNVTG,1*5B"]
        for text in valid:
            star = text.index("*")
            hex_sum = text[star + 1:]
            cases += [text, " " + text + " \r\n", text[1:], text.replace("*", ""),
                      text.replace("*", "**"), text + "*", text[:star + 1] + hex_sum.lower(),
                      text[:star + 1] + "0x" + hex_sum, text[:star + 1] + hex_sum[0] + "_"
                      + hex_sum[1], text[:star + 1] + " " + hex_sum,
                      text[:star + 1] + "+" + hex_sum, text[:star + 1] + "-" + hex_sum]
        while len(cases) < 20000:
            text = rng.choice(valid)
            kind = rng.randrange(4)
            if kind == 0:
                # Flip one bit of one character (ASCII and beyond).
                index = rng.randrange(len(text))
                char = chr(ord(text[index]) ^ (1 << rng.randrange(9)))
                text = text[:index] + char + text[index + 1:]
            elif kind == 1:
                # Insert a random character anywhere.
                index = rng.randrange(len(text) + 1)
                text = text[:index] + rng.choice(alphabet) + text[index:]
            elif kind == 2:
                # Random text between "$" and a random checksum field.
                body = "".join(rng.choice(alphabet) for _ in range(rng.randrange(12)))
                text = "$" + body + "*" + "".join(
                    rng.choice(alphabet) for _ in range(rng.randrange(4)))
            else:
                # Cut the sentence at a random place.
                text = text[:rng.randrange(len(text) + 1)]
            cases.append(text)
        accepted = 0
        for text in cases:
            result = gnss.GNSSHandler.check_nmea_checksum(text)
            self.assertEqual(result, nmea_reference.check_nmea_checksum(text), repr(text))
            accepted += result
        self.assertGreater(accepted, 100)
        self.assertLess(accepted, len(cases) - 1000)

    async def test_clean_lines_reach_the_queue_as_received(self):
        clean = _line(GGA_BODY % "123519.00")
        spaced = _line(_vtg(1), lead=b" ")
        result = await self.scan([clean + spaced])
        self.assertEqual(result["queue"][0], clean)
        self.assertEqual(result["queue"][1], (spaced.decode().strip() + "\r\n").encode())
        self.assertEqual(len(result["queue"]), 2)

    async def test_overflow_while_reading_more_with_an_advanced_position(self):
        vtgs = [_line(_vtg(index)) for index in range(3)]
        gga = _line(GGA_BODY % "123519.00")
        tail = [_line(_vtg(7)), _line(_vtg(8))]
        first = b"".join(vtgs) + gga[:40]
        rest = gga[40:] + b"xx"
        second = rest + b"y" * (300 - len(rest) - len(b"".join(tail))) + b"".join(tail)
        self.assertEqual(len(second), 300)
        with mock.patch.object(gnss, "MAX_NMEA_BUFFER_BYTES", 256), \
                mock.patch.object(gnss, "NMEA_YIELD_INTERVAL", 2):
            result = await self.scan([first, second])
        stats = result["stats"]
        overflow = dict(stats["uart_last_buffer_overflow"])
        overflow.pop("ticks_ms")
        # Literals measured at 08c5928 (characterization).
        self.assertEqual(result["queue"], [vtgs[0], vtgs[1], tail[1]])
        self.assertEqual(stats["uart_buffer_overflows"], 1)
        self.assertEqual(overflow, {"pending_bytes": 77, "incoming_bytes": 300,
                                    "retained_bytes": 37, "discarded_bytes": 340})
        self.assertEqual(stats["uart_buffer_peak_bytes"], 377)
        self.assertEqual(stats["crc_errors"], 0)

    async def test_incomplete_sentence_across_reads_is_delivered_once(self):
        gga = _line(GGA_BODY % "123519.00")
        # The empty line makes the loop read more before the first sentence ends;
        # the third part arrives through the outer loop.
        parts = [b"\n" + gga[:20], gga[20:50], gga[50:]]
        with mock.patch.object(gnss, "NMEA_YIELD_INTERVAL", 1):
            result = await self.scan(parts)
        stats = result["stats"]
        self.assertEqual(result["queue"], [gga])
        self.assertEqual(result["utc"], ["123519.00"])
        self.assertEqual(stats["crc_errors"], 0)
        self.assertEqual(stats.get("uart_buffer_overflows", 0), 0)
        self.assertEqual(stats["uart_rx_reads"], 3)

    async def test_scanning_restarts_cleanly_after_an_overflow(self):
        garbage = b"x" * 570 + b"$GNGGA,123519.00,48" + b"z" * 11
        self.assertEqual(len(garbage), 600)
        valid = [_line(_vtg(index)) for index in range(3)] + [_line(GGA_BODY % "123520.00")]
        with mock.patch.object(gnss, "MAX_NMEA_BUFFER_BYTES", 256):
            result = await self.scan([garbage, b"\r\n" + b"".join(valid)])
        stats = result["stats"]
        overflow = dict(stats["uart_last_buffer_overflow"])
        overflow.pop("ticks_ms")
        # Literals measured at 08c5928 (characterization).
        self.assertEqual(stats["uart_buffer_overflows"], 1)
        self.assertEqual(overflow, {"pending_bytes": 0, "incoming_bytes": 600,
                                    "retained_bytes": 30, "discarded_bytes": 570})
        self.assertEqual(stats["uart_buffer_peak_bytes"], 600)
        # The retained sentence start without a checksum fails as one line.
        self.assertEqual(stats["crc_errors"], 1)
        self.assertEqual(stats["nmea_last_crc_error"]["raw_length"], 31)
        self.assertEqual(result["queue"], valid)
        self.assertEqual(result["utc"], ["123520.00"])

    async def test_a_handler_error_mid_chunk_does_not_repeat_lines(self):
        before = [_line(_vtg(index)) for index in range(3)]
        gga = _line(GGA_BODY % "123519.00")
        after = [_line(_vtg(index)) for index in range(3, 6)]
        later = _line(_vtg(9))
        calls = []

        def fail_once(fix):
            calls.append(fix["utc"])
            if len(calls) == 1:
                raise RuntimeError("listener failed")

        # The rest of the failed chunk waits in the buffer for the next read.
        result = await self.scan([b"".join(before) + gga + b"".join(after), later],
                                 update_fix=fail_once)
        self.assertEqual(result["queue"], before + [gga] + after + [later])
        self.assertEqual(calls, ["123519.00"])

    async def test_a_large_burst_is_not_copied_per_line(self):
        sentences = [_line("GNVTG,%03d.000000000000000000,T,%s,M,0.02,N,0.04,K,R"
                           % (index, "1" * 27)) for index in range(200)]
        chunk = b"".join(sentences)
        self.assertGreater(len(chunk), 16384)
        stop = asyncio.Event()
        reads = [chunk]
        count = [0]

        class CountingQueue:
            def put_nowait(self, data):
                count[0] += 1
                return True

        async def turn(ms):
            # No real suspension: the test loop runs in debug mode, which
            # records a traceback for every scheduled callback.
            if ms:
                stop.set()

        def read_uart():
            return reads.pop() if reads else None

        with mock.patch.dict(gnss.app.stats, {"crc_errors": 0, "queue_overflows": 0},
                             clear=True), \
                mock.patch.object(gnss, "shutdown_event", stop), \
                mock.patch.object(gnss, "log", lambda *args, **kwargs: None), \
                mock.patch.object(gnss.time, "ticks_ms", lambda: CLOCK_MS), \
                mock.patch.object(gnss.asyncio, "sleep_ms", turn):
            # Plain functions, not mocks: a mock keeps every call it records.
            handler = gnss.GNSSHandler(CountingQueue())
            handler._read_uart = read_uart
            tracemalloc.start()
            try:
                current = tracemalloc.get_traced_memory()[0]
                # Without wait_for: its timer handle carries a debug traceback.
                # run() ends at its first outer pause, which sets stop.
                await handler.run()
                peak = tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()
        self.assertEqual(count[0], 200)
        self.assertLess(peak - current, 0.25 * len(chunk))


if __name__ == "__main__":
    unittest.main()
