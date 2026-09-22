# SPDX-License-Identifier: AGPL-3.0-only
"""Model the observed RX read delay separately from flash scheduler pauses."""
import asyncio
import unittest
from unittest import mock

from support import gnss, sentence


class TestGnssUartNonblocking(unittest.IsolatedAsyncioTestCase):
    async def receive_timed_wire(self, force_legacy_delay=False):
        # Match the measured 1.82 kB/s byte rate, not the exact receiver mix.
        # A 255 ms read is the observed duration of the old timeout=100 path;
        # this is a timing model, not an emulation of the MicroPython driver.
        expected = [(sentence("GNRMC,%06d,A,5000.000000,N,00800.0000,E,0.0,0.0,080926,,,A"
                              % index) + "\r\n").encode() for index in range(2000)]
        wire = b"".join(expected)
        pauses = [3953, 100, 150, 100, 3000, 100, 100, 100, 2650,
                  100, 100, 100, 2200, 100, 100, 100, 100]
        stop, received, now, retained_sizes = asyncio.Event(), [], [0], []
        uarts = []

        class UART:
            """Finite drop-new ring; only observed elapsed times are modeled."""
            def __init__(self, *args, **options):
                self.capacity = options["rxbuf"]
                self.delay = (255 if force_legacy_delay or options.get("timeout", 0)
                              or options.get("timeout_char", 0) else 0)
                self.buf = bytearray(wire[:3000])
                self.source_offset = 3000
                self.dropped = 0
                self.highwater = 3000
                self.read_durations = []
                uarts.append(self)

            def advance(self, milliseconds):
                now[0] += milliseconds
                end = min(len(wire), 3000 + now[0] * 1820 // 1000)
                arrived = wire[self.source_offset:end]
                room = max(0, self.capacity - len(self.buf))
                self.buf.extend(arrived[:room])
                self.dropped += max(0, len(arrived) - room)
                self.source_offset = end
                self.highwater = max(self.highwater, len(self.buf))

            def any(self):
                return len(self.buf)

            def read(self):
                # Time passes and bytes arrive while the read blocks, without
                # granting the parser or any other asyncio task a turn.
                self.advance(self.delay)
                self.read_durations.append(self.delay)
                value = bytes(self.buf)
                self.buf.clear()
                return value

        class Queue:
            def put_nowait(self, value):
                received.append(value)
                return True

            def qsize(self):
                return 0

        async def shared_scheduler(milliseconds):
            uart = uarts[0]
            # Use 10 ms host-model scheduler quanta after the explicit pauses;
            # thousands of real coroutine polls need not model sub-tick work.
            delay = pauses.pop(0) if milliseconds == 0 and pauses else max(10, milliseconds)
            uart.advance(delay)
            if uart.source_offset == len(wire) and not uart.buf:
                stop.set()
            await asyncio.sleep(0)

        initial = {"crc_errors": 0, "queue_overflows": 0, "uart_buffer_overflows": 0}
        with mock.patch.object(gnss, "UART", UART), \
                mock.patch.dict(gnss.app.stats, initial, clear=True), \
                mock.patch.object(gnss, "shutdown_event", stop), \
                mock.patch.object(gnss.time, "ticks_ms", side_effect=lambda: now[0]), \
                mock.patch.object(gnss.asyncio, "sleep_ms", shared_scheduler), \
                mock.patch.object(gnss, "log"):
            handler = gnss.GNSSHandler(Queue())
            append = handler._append_uart_chunk

            def observe_append(pending, chunk):
                result = append(pending, chunk)
                retained_sizes.append(len(result))
                return result

            with mock.patch.object(handler, "_append_uart_chunk", side_effect=observe_append):
                await asyncio.wait_for(handler.run(), 5)
            stats = dict(gnss.app.stats)
        uart = uarts[0]
        self.assertEqual(pauses, [])
        self.assertLessEqual(max(retained_sizes), gnss.MAX_NMEA_BUFFER_BYTES)
        return expected, received, stats, {
            "wire_bytes": len(wire), "wire_rate_bytes_sec": 1820,
            "read_duration_ms": max(uart.read_durations),
            "received": len(received), "dropped_before_python": uart.dropped,
            "driver_highwater": uart.highwater, "python_highwater": max(retained_sizes),
            "python_overflows": stats["uart_buffer_overflows"],
            "read_gap_max_ms": stats["uart_read_gap_max_ms"],
            "crc_errors": stats["crc_errors"], "elapsed_ms": now[0],
        }

    async def test_measured_blocking_reads_overload_python_without_driver_loss(self):
        # Reproduce the historical four-sentence reader and its blocking read.
        with mock.patch.object(gnss, "NMEA_YIELD_INTERVAL", 4):
            expected, received, stats, evidence = await self.receive_timed_wire(True)
        self.assertEqual(evidence["read_duration_ms"], 255)
        self.assertEqual(evidence["dropped_before_python"], 0, evidence)
        self.assertGreater(stats["uart_buffer_overflows"], 0, evidence)
        self.assertGreater(stats["uart_last_buffer_overflow"]["discarded_bytes"], 0)
        self.assertLess(len(received), len(expected), evidence)
        # This overload discards complete sentences at the explicit Python
        # resynchronization boundary, so CRC alone must not qualify the run.
        self.assertEqual(stats["crc_errors"], 0, evidence)

    async def test_configured_reads_keep_up_with_same_timed_wire_and_flash_pauses(self):
        expected, received, stats, evidence = await self.receive_timed_wire()
        self.assertEqual(received, expected, evidence)
        self.assertEqual(stats["uart_rx_bytes"], evidence["wire_bytes"])
        self.assertEqual(evidence["dropped_before_python"], 0, evidence)
        self.assertEqual(stats["uart_buffer_overflows"], 0, evidence)
        self.assertEqual(stats["crc_errors"], 0, evidence)
        self.assertEqual(stats["queue_overflows"], 0, evidence)
        self.assertEqual(stats["uart_last_read_duration_ms"], 0, evidence)
        self.assertLessEqual(evidence["read_gap_max_ms"], 3953)


if __name__ == "__main__":
    unittest.main()
