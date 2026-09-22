# SPDX-License-Identifier: AGPL-3.0-only
"""RX diagnostics distinguish delivered bytes from a simulated upstream loss."""
import asyncio
import json
import unittest
from unittest import mock

from support import gnss, sentence


class TestGnssUartDiagnostics(unittest.IsolatedAsyncioTestCase):
    async def receive(self, chunks):
        chunks = list(chunks)
        stop = asyncio.Event()
        received = []

        class Queue:
            def put_nowait(self, data): received.append(data); return True
            def qsize(self): return 3

        class UART:
            def any(self): return len(chunks[0]) if chunks else 0
            def read(self): return chunks.pop(0)

        async def schedule(ms):
            if not chunks: stop.set()
            await asyncio.sleep(0)

        handler = gnss.GNSSHandler(Queue())
        handler.uart = UART()
        initial = {"crc_errors": 0, "queue_overflows": 0, "uart_buffer_overflows": 0,
                   "tracking_max_append_ms": 3953, "tracking_queue_depth": 17,
                   "parallel_benchmark_queue_max": 17}
        with mock.patch.dict(gnss.app.stats, initial, clear=True), \
                mock.patch.object(gnss, "shutdown_event", stop), \
                mock.patch.object(gnss.asyncio, "sleep_ms", schedule), \
                mock.patch.object(gnss.app, "update_fix"), \
                mock.patch.object(gnss.app, "attach_gst"), \
                mock.patch.object(gnss, "replies", []), \
                mock.patch.object(gnss, "log") as log:
            await asyncio.wait_for(handler.run(), 2)
            stats = dict(gnss.app.stats)
            replies = list(gnss.replies)
            self.assertFalse(any(call.args[0] in ("WARN", "ERROR") for call in log.call_args_list))
        return received, stats, replies

    async def test_lossless_fragmentation_preserves_every_sentence_and_raw_byte_count(self):
        expected = [(sentence("GNRMC,%06d,A" % index) + "\r\n").encode()
                    for index in range(12)]
        wire = b"".join(expected)
        for width in (1, 2, 7, 19, len(wire)):
            chunks = [wire[index:index + width] for index in range(0, len(wire), width)]
            received, stats, _ = await self.receive(chunks)
            self.assertEqual(received, expected)
            self.assertEqual(stats.get("crc_errors"), 0)
            self.assertIsNone(stats.get("nmea_last_crc_error"))
            self.assertEqual(stats["uart_rx_bytes"], len(wire))
            self.assertEqual(stats["uart_rx_reads"], len(chunks))
            self.assertEqual(stats["uart_rx_chunk_max"], min(width, len(wire)))
            self.assertEqual(stats["uart_rx_available_max"], min(width, len(wire)))

    async def test_non_ascii_bytes_are_not_silently_repaired_into_valid_nmea(self):
        valid = (sentence("GNRMC,123456,A") + "\r\n").encode()
        inserted = valid[:10] + b"\x80" + valid[10:]
        replaced_talker = valid[:2] + b"\xff" + valid[3:]
        for damaged in (inserted, replaced_talker):
            received, stats, _ = await self.receive([damaged, valid])
            self.assertEqual(received, [valid])
            self.assertEqual(stats["crc_errors"], 1)
            error = stats["nmea_last_crc_error"]
            self.assertTrue(error["ascii_error"])
            self.assertEqual(error["sentence_type"], "RMC")
            self.assertEqual(bytes.fromhex(error["raw_hex"]), damaged[:-1])
            self.assertEqual(error["raw_length"], len(damaged) - 1)
            self.assertEqual(error["tracking_max_append_ms"], 3953)
            self.assertEqual(error["tracking_queue_depth"], 17)
            self.assertEqual(error["nmea_queue_depth"], 3)

    async def test_malformed_checksums_are_counted_and_only_latest_record_is_retained(self):
        bad = [b"$GNRMC,missing-star\r\n", b"$GNRMC,1*XX\r\n",
               b"$GNRMC,2*01*02\r\n", b"$GNRMC,3$GNRMC,4*00\r\n"]
        received, stats, _ = await self.receive(bad)
        self.assertEqual(received, [])
        self.assertEqual(stats["crc_errors"], 4)
        error = stats["nmea_last_crc_error"]
        self.assertEqual(error["crc_error_count"], 4)
        self.assertEqual(error["dollar_count"], 2)
        self.assertEqual(error["star_count"], 1)
        self.assertEqual(error["checksum_provided"], "00")
        checksum = 0
        for byte in bad[-1][1:bad[-1].index(b"*")]: checksum ^= byte
        self.assertEqual(error["checksum_calculated"], checksum)
        self.assertEqual(bytes.fromhex(error["raw_hex"]), bad[-1][:-1])
        self.assertIsInstance(error["ticks_ms"], int)
        self.assertIsInstance(error["device_time_sec"], int)
        self.assertIsInstance(error["since_last_uart_read_ms"], int)
        self.assertNotIn("parse_delay_ms", error)
        self.assertLess(len(json.dumps(error)), 2000)

    async def test_large_invalid_raw_sentence_is_bounded_without_losing_total_length(self):
        wire = b"$GNRMC," + b"x" * 4096 + b"\r\n"
        received, stats, _ = await self.receive([wire])
        error = stats["nmea_last_crc_error"]
        self.assertEqual(received, [])
        self.assertEqual(error["raw_length"], len(wire) - 1)
        self.assertEqual(len(bytes.fromhex(error["raw_hex"])), 256)
        self.assertTrue(error["raw_truncated"])
        self.assertIsNone(error["checksum_provided"])
        self.assertIsNone(error["checksum_calculated"])

    async def test_well_formed_unknown_sentences_and_command_replies_keep_filtering(self):
        reply = sentence("PQTMVERNO,Example")
        unknown = sentence("GNXYZ,123456")
        received, stats, replies = await self.receive([(reply + "\r\n" + unknown + "\r\n").encode()])
        self.assertEqual(received, [])
        self.assertEqual(replies, [reply])
        self.assertEqual(stats["crc_errors"], 0)
        self.assertIsNone(stats.get("nmea_last_crc_error"))

    async def test_read_metrics_report_elapsed_window_not_assumed_one_second_burst(self):
        handler = gnss.GNSSHandler(object())
        handler._uart_rate_started = 0
        with mock.patch.dict(gnss.app.stats, {}, clear=True):
            handler._record_uart_read(120, b"a" * 100, 100, 110)
            handler._record_uart_read(250, b"b" * 200, 1090, 1100)
            stats = gnss.app.stats
            self.assertEqual(stats["uart_rx_bytes"], 300)
            self.assertEqual(stats["uart_rx_reads"], 2)
            self.assertEqual(stats["uart_rx_available_max"], 250)
            self.assertEqual(stats["uart_rx_chunk_max"], 200)
            self.assertEqual(stats["uart_read_gap_ms"], 990)
            self.assertEqual(stats["uart_last_read_duration_ms"], 10)
            self.assertEqual(stats["uart_last_read_ticks_ms"], 1090)
            self.assertEqual(stats["uart_rx_rate_bytes_sec"], 272)
            self.assertEqual(stats["uart_rx_rate_window_ms"], 1100)
            handler._record_uart_read(1000, b"c" * 1000, 5050, 5100)
            self.assertEqual(stats["uart_read_gap_max_ms"], 3960)
            self.assertEqual(stats["uart_rx_rate_bytes_sec"], 250)
            self.assertEqual(stats["uart_rx_rate_max_bytes_sec"], 272)
            self.assertEqual(stats["uart_rx_rate_window_ms"], 4000)

    async def test_simulated_full_uart_ring_can_drop_bytes_without_python_overflow(self):
        # This models one possible upstream drop policy, not the ESP-IDF driver.
        expected = [(sentence("GNRMC,%06d,A,5000.000000,N,00800.0000,E,0.0,0.0,080926,,,A"
                             % index) + "\r\n").encode() for index in range(1000)]
        wire = b"".join(expected)
        produced_during_pause = int(115200 / 10 * 3.953)  # 8N1 maximum wire load.
        capacity = 16384
        retained = [wire[:capacity], wire[produced_during_pause:]]
        # Deliver the retained stream in scheduler-sized chunks so this test
        # isolates the upstream gap from the separate Python backlog limit.
        fragmented = [part[index:index + 264] for part in retained
                      for index in range(0, len(part), 264)]
        received, stats, _ = await self.receive(fragmented)
        self.assertLess(len(received), len(expected))
        self.assertEqual(stats["crc_errors"], 1)
        self.assertEqual(stats["uart_buffer_overflows"], 0)
        self.assertEqual(stats["queue_overflows"], 0)
        self.assertEqual(stats["uart_rx_bytes"], sum(map(len, retained)))
        self.assertEqual(stats["nmea_last_crc_error"]["raw_hex"],
                         b"$GNRMC,000248,A,\r".hex())
        received, control, _ = await self.receive(
            [wire[index:index + 264] for index in range(0, len(wire), 264)])
        self.assertEqual(received, expected)
        self.assertEqual(control["crc_errors"], 0)
        self.assertEqual(control["uart_rx_bytes"], len(wire))


if __name__ == "__main__":
    unittest.main()
