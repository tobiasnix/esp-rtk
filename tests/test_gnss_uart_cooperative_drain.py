# SPDX-License-Identifier: AGPL-3.0-only
"""Model finite UART retention while a parser shares time with flash writes."""
import asyncio
import unittest
from unittest import mock

from support import gnss, sentence


class TestGnssCooperativeDrain(unittest.IsolatedAsyncioTestCase):
    async def test_sustained_refill_overload_is_bounded_and_visible(self):
        frame = (sentence("GNRMC,123456,A,5000.000000,N,00800.0000,E,0.0,0.0,080926,,,A")
                 + "\r\n").encode()
        stop, received, sizes = asyncio.Event(), [], []

        class Queue:
            def put_nowait(self, value): received.append(value); return True

        handler = gnss.GNSSHandler(Queue())
        handler.uart.rx.extend(frame * 4)
        append = handler._append_uart_chunk
        refills = [0]

        def bounded_append(pending, chunk):
            result = append(pending, chunk)
            sizes.append(len(result))
            return result

        async def excessive_producer(ms):
            if refills[0] < 17:
                # A finite overload independent of the realistic nominal model.
                handler.uart.rx.extend(frame * 120)
                refills[0] += 1
            if refills[0] == 17 and ms != 0 and not handler.uart.any():
                stop.set()
            await asyncio.sleep(0)

        with mock.patch.dict(gnss.app.stats, {
                "crc_errors": 0, "queue_overflows": 0, "uart_buffer_overflows": 0}, clear=True), \
                mock.patch.object(gnss, "shutdown_event", stop), \
                mock.patch.object(gnss.asyncio, "sleep_ms", excessive_producer), \
                mock.patch.object(handler, "_append_uart_chunk", side_effect=bounded_append), \
                mock.patch.object(gnss, "log"):
            await asyncio.wait_for(handler.run(), 2)
            self.assertGreater(gnss.app.stats["uart_buffer_overflows"], 0)
            self.assertGreater(gnss.app.stats["uart_last_buffer_overflow"]["discarded_bytes"], 0)
        self.assertLessEqual(max(sizes), gnss.MAX_NMEA_BUFFER_BYTES)
        self.assertLess(len(received), 4 + 17 * 120)
        self.assertTrue(all(value == frame for value in received))

    async def test_oversized_unterminated_sentence_cannot_survive_resynchronization(self):
        handler = gnss.GNSSHandler(object())
        limit = gnss.MAX_NMEA_BUFFER_BYTES
        with mock.patch.dict(gnss.app.stats, {"uart_buffer_overflows": 0}, clear=True):
            exact = b"$" + b"x" * (limit - 1)
            self.assertEqual(handler._append_uart_chunk(b"", exact), exact)
            self.assertEqual(gnss.app.stats["uart_buffer_overflows"], 0)
            self.assertEqual(handler._append_uart_chunk(b"", b"$" + b"x" * limit), b"")
            self.assertEqual(gnss.app.stats["uart_buffer_overflows"], 1)
            kept = handler._append_uart_chunk(b"x" * limit, b"garbage$GNRMC,partial")
            self.assertEqual(kept, b"$GNRMC,partial")
            self.assertEqual(gnss.app.stats["uart_buffer_overflows"], 2)

    async def test_refilled_batch_refreshes_gsv_time_window_and_gnss_heartbeat(self):
        batch = gnss.NMEA_YIELD_INTERVAL
        expected = [(sentence("GPGSV,3,1,12,%02d,30,100,40" % index) + "\r\n").encode()
                    for index in range(5 * batch)]
        stop, received, now, beats = asyncio.Event(), [], [0], []

        class Queue:
            def put_nowait(self, value): received.append(value); return True

        handler = gnss.GNSSHandler(Queue())
        handler.gsv_reset_time = 0
        handler._uart_rate_started = 0
        handler.uart.rx.extend(b"".join(expected[:2 * batch]))
        refilled = False

        async def shared_scheduler(ms):
            nonlocal refilled
            if ms == 0:
                now[0] += 1100
                if not refilled:
                    handler.uart.rx.extend(b"".join(expected[2 * batch:]))
                    refilled = True
            else:
                stop.set()
            await asyncio.sleep(0)

        with mock.patch.dict(gnss.app.stats, {"crc_errors": 0, "queue_overflows": 0}, clear=True), \
                mock.patch.dict(gnss.CONFIG, {"gsv_limit_per_sec": batch}), \
                mock.patch.object(gnss, "shutdown_event", stop), \
                mock.patch.object(gnss.time, "ticks_ms", side_effect=lambda: now[0]), \
                mock.patch.object(gnss.asyncio, "sleep_ms", shared_scheduler), \
                mock.patch.object(gnss.app, "beat", side_effect=lambda key: beats.append((key, now[0]))), \
                mock.patch.object(gnss, "log"):
            await asyncio.wait_for(handler.run(), 2)
            self.assertEqual(gnss.app.stats["crc_errors"], 0)
            self.assertEqual(gnss.app.stats["uart_rx_bytes"], sum(map(len, expected)))
        self.assertEqual(received, expected)
        for tick in (1100, 2200, 3300, 4400, 5500):
            self.assertIn(("gnss", tick), beats)
        self.assertEqual(handler.gsv_reset_time, 5500)

    async def test_repeated_flash_pauses_do_not_accumulate_between_uart_reads(self):
        # Measured hardware raw throughput was about 1.82 kB/s. These synthetic
        # RMC frames model that byte rate, not the receiver's exact sentence mix.
        expected = [(sentence("GNRMC,%06d,A,5000.000000,N,00800.0000,E,0.0,0.0,080926,,,A"
                             % index) + "\r\n").encode() for index in range(500)]
        wire = b"".join(expected)
        pauses = [3953, 100, 150, 100, 3000, 100, 100, 100, 2650,
                  100, 100, 100, 2200, 100, 100, 100, 100]
        self.assertEqual(len(pauses), 17)
        self.assertLess(max(pauses) * 1820 // 1000, 16384)
        self.assertGreater(sum(pauses) * 1820 // 1000, 16384)
        stop, received, now, turn_sizes = asyncio.Event(), [], [0], []
        previous_received = 0

        class UART:
            """A possible drop-new ring policy; not an ESP-IDF implementation."""
            def __init__(self):
                self.buf = bytearray(wire[:3000])
                self.source_offset, self.dropped, self.highwater = 3000, 0, 3000
                self.read_times = []

            def advance(self, milliseconds):
                now[0] += milliseconds
                end = min(len(wire), 3000 + (now[0] * 1820) // 1000)
                arrived = wire[self.source_offset:end]
                room = max(0, 16384 - len(self.buf))
                self.buf.extend(arrived[:room])
                self.dropped += max(0, len(arrived) - room)
                self.source_offset = end
                self.highwater = max(self.highwater, len(self.buf))

            def any(self): return len(self.buf)

            def read(self):
                self.read_times.append(now[0])
                value = bytes(self.buf)
                self.buf.clear()
                return value

        class Queue:
            def put_nowait(self, value): received.append(value); return True
            def qsize(self): return 0

        uart = UART()
        handler = gnss.GNSSHandler(Queue())
        handler.uart = uart
        handler._uart_rate_started = 0

        async def shared_scheduler(ms):
            nonlocal previous_received
            turn_sizes.append(len(received) - previous_received)
            previous_received = len(received)
            delay = pauses.pop(0) if ms == 0 and pauses else max(1, ms)
            uart.advance(delay)
            if uart.source_offset == len(wire) and not uart.buf:
                stop.set()
            await asyncio.sleep(0)

        initial = {"crc_errors": 0, "queue_overflows": 0, "uart_buffer_overflows": 0}
        with mock.patch.dict(gnss.app.stats, initial, clear=True), \
                mock.patch.object(gnss, "shutdown_event", stop), \
                mock.patch.object(gnss.time, "ticks_ms", side_effect=lambda: now[0]), \
                mock.patch.object(gnss.asyncio, "sleep_ms", shared_scheduler), \
                mock.patch.object(gnss, "log"):
            await asyncio.wait_for(handler.run(), 3)
            stats = dict(gnss.app.stats)
        max_read_gap = max(b - a for a, b in zip(uart.read_times, uart.read_times[1:]))
        evidence = {"received": len(received), "expected": len(expected),
            "dropped_before_python": uart.dropped, "uart_highwater": uart.highwater,
            "max_read_gap_ms": max_read_gap, "crc_errors": stats["crc_errors"],
            "uart_buffer_overflows": stats["uart_buffer_overflows"]}
        self.assertEqual(uart.dropped, 0, evidence)
        self.assertEqual(received, expected)
        self.assertEqual(stats["crc_errors"], 0)
        self.assertEqual(stats["uart_buffer_overflows"], 0)
        self.assertEqual(stats["queue_overflows"], 0)
        self.assertEqual(stats["uart_rx_bytes"], len(wire))
        self.assertLessEqual(max_read_gap, 3953)
        self.assertEqual(stats["uart_read_gap_max_ms"], max_read_gap)
        self.assertLessEqual(max(turn_sizes), gnss.NMEA_YIELD_INTERVAL)
        self.assertEqual(pauses, [])


if __name__ == "__main__":
    unittest.main()
