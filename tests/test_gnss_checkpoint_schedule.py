# SPDX-License-Identifier: AGPL-3.0-only
"""Explore measured checkpoint service budgets without claiming device timing."""
import asyncio
import math
import unittest
from unittest import mock

from support import gnss, sentence
import pointstore


# A field diagnostic run plateaued at 3630 points while verification
# took 25047 ms. The later isolated 3654-point probe measured only 10307159 us
# of scan work, with median 87011 us per 32-record step. The remaining wall
# time belongs to other tasks/scheduler waits, not to point decoding.
OBSERVED_SCAN_POINTS = 3630
OBSERVED_VERIFY_MS = 25047
OBSERVED_CHECKPOINT_MS = 33807
OBSERVED_FIRST_PHASES_MS = (2112, 4379)
PROBE_POINTS = 3654
PROBE_SCAN_WORK_US = 10307159
PROBE_STEP_MEDIAN_US = 87011
OBSERVED_OTHER_TASK_WAIT_MS = (
    OBSERVED_VERIFY_MS - OBSERVED_SCAN_POINTS * PROBE_SCAN_WORK_US // PROBE_POINTS // 1000)


class TestGnssCheckpointSchedule(unittest.IsolatedAsyncioTestCase):
    async def receive_during_checkpoint(self, batch, records=OBSERVED_SCAN_POINTS,
                                        batches_per_gnss_turn=1, include_other_task_wait=True,
                                        parser_sentences=4):
        # At 59 bytes each, synthetic frames model both measured raw throughput
        # (~1820 B/s) and approximate sentence rate (~31/s). RMC is deliberately
        # used for all frames: sentence framing/yield costs apply before filters.
        scan_work_ms = records * PROBE_SCAN_WORK_US // PROBE_POINTS // 1000
        other_task_wait_ms = (records * OBSERVED_OTHER_TASK_WAIT_MS // OBSERVED_SCAN_POINTS
                              if include_other_task_wait else 0)
        scan_ms = scan_work_ms + other_task_wait_ms
        first_phases = list(OBSERVED_FIRST_PHASES_MS)
        final_phase_ms = (OBSERVED_CHECKPOINT_MS - OBSERVED_VERIFY_MS
                          - sum(OBSERVED_FIRST_PHASES_MS))
        total_ms = scan_ms + sum(first_phases) + final_phase_ms + 10000
        count = math.ceil((3000 + total_ms * 1820 / 1000) / 59)
        expected = [(sentence("GNRMC,%06d,A,5000.00000,N,00800.0000,E,0,0,080926,A"
                              % index) + "\r\n").encode() for index in range(count)]
        self.assertTrue(all(len(frame) == 59 for frame in expected))
        wire = b"".join(expected)
        now, stop, received, retained_sizes = [0], asyncio.Event(), [], []
        scan_progress, scan_elapsed, scan_turns = 0, 0, 0
        work_elapsed, waiting_elapsed = 0, 0

        class UART:
            def __init__(self):
                self.buf = bytearray(wire[:3000])
                self.source_offset, self.dropped, self.highwater = 3000, 0, 3000

            def advance(self, milliseconds):
                now[0] += milliseconds
                end = min(len(wire), 3000 + now[0] * 1820 // 1000)
                arrived = wire[self.source_offset:end]
                room = max(0, 16384 - len(self.buf))
                self.buf.extend(arrived[:room])
                self.dropped += max(0, len(arrived) - room)
                self.source_offset = end
                self.highwater = max(self.highwater, len(self.buf))

            def any(self): return len(self.buf)

            def read(self):
                result = bytes(self.buf)
                self.buf.clear()
                return result

        class Queue:
            def put_nowait(self, value): received.append(value); return True
            def qsize(self): return 0

        uart = UART()

        async def shared_scheduler(milliseconds):
            nonlocal scan_progress, scan_elapsed, scan_turns, final_phase_ms
            nonlocal work_elapsed, waiting_elapsed
            if first_phases:
                delay = first_phases.pop(0)
            elif scan_progress < records:
                # Scan work and time spent on other tasks remain separate.
                # Group=1 assumes each scanner yield also serves GNSS. Group=3
                # reproduces the observed 609-835 ms RX service gaps using the
                # same total work/wait budgets; it does not explain the exact
                # task order. Assuming exclusive 87 ms scan work was the whole
                # service interval would erase the observed overload.
                scan_progress = min(records, scan_progress + batch * batches_per_gnss_turn)
                work_elapsed = scan_progress * PROBE_SCAN_WORK_US // PROBE_POINTS // 1000
                waiting_elapsed = (scan_progress * OBSERVED_OTHER_TASK_WAIT_MS // OBSERVED_SCAN_POINTS
                                   if include_other_task_wait else 0)
                elapsed = work_elapsed + waiting_elapsed
                delay, scan_elapsed = elapsed - scan_elapsed, elapsed
                scan_turns += 1
            elif final_phase_ms:
                delay, final_phase_ms = final_phase_ms, 0
            else:
                delay = max(10, milliseconds)
            uart.advance(delay)
            if uart.source_offset == len(wire) and not uart.buf:
                stop.set()
            await asyncio.sleep(0)

        initial = {"crc_errors": 0, "queue_overflows": 0, "uart_buffer_overflows": 0}
        with mock.patch.dict(gnss.app.stats, initial, clear=True), \
                mock.patch.object(gnss, "NMEA_YIELD_INTERVAL", parser_sentences), \
                mock.patch.object(gnss, "shutdown_event", stop), \
                mock.patch.object(gnss.time, "ticks_ms", side_effect=lambda: now[0]), \
                mock.patch.object(gnss.asyncio, "sleep_ms", shared_scheduler), \
                mock.patch.object(gnss, "log"):
            handler = gnss.GNSSHandler(Queue())
            handler.uart = uart
            append = handler._append_uart_chunk

            def observe_append(pending, chunk):
                result = append(pending, chunk)
                retained_sizes.append(len(result))
                return result

            with mock.patch.object(handler, "_append_uart_chunk", side_effect=observe_append):
                await asyncio.wait_for(handler.run(), 5)
            stats = dict(gnss.app.stats)
        self.assertEqual(scan_progress, records)
        self.assertEqual(scan_elapsed, scan_ms)
        self.assertEqual(work_elapsed, scan_work_ms)
        self.assertEqual(waiting_elapsed, other_task_wait_ms)
        self.assertEqual(uart.dropped, 0)
        self.assertEqual(stats["crc_errors"], 0)
        self.assertEqual(stats["uart_rx_bytes"], len(wire))
        self.assertLessEqual(max(retained_sizes), gnss.MAX_NMEA_BUFFER_BYTES)
        return {
            "scan_records": records, "scan_batch_records": batch,
            "batches_per_gnss_turn": batches_per_gnss_turn,
            "scan_ms": scan_ms, "scan_work_ms": scan_work_ms,
            "other_task_wait_ms": other_task_wait_ms,
            "probe_median_work_per_32_records_us": PROBE_STEP_MEDIAN_US,
            "gnss_turns_during_scan": scan_turns,
            "expected_sentences": len(expected), "received_sentences": len(received),
            "all_sentences_equal_in_order": received == expected,
            "python_overflows": stats["uart_buffer_overflows"],
            "python_highwater": max(retained_sizes), "driver_highwater": uart.highwater,
            "driver_dropped": uart.dropped, "crc_errors": stats["crc_errors"],
        }

    async def test_sustained_measured_work_and_wait_require_smaller_turns(self):
        for group in (1, 3):
            with self.subTest(batches_per_gnss_turn=group):
                old = await self.receive_during_checkpoint(32, batches_per_gnss_turn=group)
                smaller = await self.receive_during_checkpoint(8, batches_per_gnss_turn=group)
                self.assertGreater(old["python_overflows"], 0, old)
                self.assertFalse(old["all_sentences_equal_in_order"], old)
                self.assertEqual(smaller["python_overflows"], 0, smaller)
                self.assertTrue(smaller["all_sentences_equal_in_order"], smaller)

    async def test_exclusive_probe_does_not_replace_concurrent_failure_evidence(self):
        exclusive = await self.receive_during_checkpoint(32, include_other_task_wait=False)
        concurrent = await self.receive_during_checkpoint(32, batches_per_gnss_turn=3)
        self.assertEqual(exclusive["scan_work_ms"], concurrent["scan_work_ms"])
        self.assertEqual(exclusive["other_task_wait_ms"], 0)
        self.assertEqual(concurrent["other_task_wait_ms"], OBSERVED_OTHER_TASK_WAIT_MS)
        self.assertTrue(exclusive["all_sentences_equal_in_order"], exclusive)
        self.assertGreater(concurrent["python_overflows"], 0, concurrent)
        self.assertFalse(concurrent["all_sentences_equal_in_order"], concurrent)

    async def test_configured_scan_turns_preserve_stream_during_longer_wait_projection(self):
        # Linear timing extrapolation only: this is not 10000-point hardware
        # qualification or proof of why three scanner batches shared one turn.
        eight = await self.receive_during_checkpoint(8, 10000, 3)
        configured = await self.receive_during_checkpoint(pointstore.SCAN_BATCH_RECORDS, 10000, 3)
        self.assertGreater(eight["python_overflows"], 0, eight)
        self.assertEqual(configured["python_overflows"], 0, configured)
        self.assertTrue(configured["all_sentences_equal_in_order"], configured)

    async def test_configured_parser_catches_up_with_more_work_between_turns(self):
        # The 8961-point hardware failure counted a Python UART-buffer overflow.
        # Six scanner batches per turn model an adverse schedule, not its exact
        # unrecorded interleaving. Keep the old four-sentence parser as a control.
        legacy = await self.receive_during_checkpoint(
            pointstore.SCAN_BATCH_RECORDS, 10000, 6, parser_sentences=4)
        current = await self.receive_during_checkpoint(
            pointstore.SCAN_BATCH_RECORDS, 10000, 6,
            parser_sentences=gnss.NMEA_YIELD_INTERVAL)
        self.assertGreater(legacy["python_overflows"], 0, legacy)
        self.assertFalse(legacy["all_sentences_equal_in_order"], legacy)
        self.assertEqual(current["python_overflows"], 0, current)
        self.assertTrue(current["all_sentences_equal_in_order"], current)
        self.assertEqual(current["driver_dropped"], 0, current)


if __name__ == "__main__":
    unittest.main()
