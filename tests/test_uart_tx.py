# SPDX-License-Identifier: AGPL-3.0-only
"""UartTx: the one writer for the receiver UART (#28 part b).

Commands reach the receiver only at an RTCM frame boundary of the bytes actually
written, or while no frame is open; a partial write is completed before anything
else; a write that stalls ends the stream; pump() never raises, RTCM errors are
raised by the NTRIP call that wrote the chunk.
"""
import ast
import asyncio
import inspect
import json
import os
import random
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

from support import REPO, ble, cfg, gnss, web, FakeUART, sentence
from test_http import FakeWriter

if REPO not in sys.path:
    sys.path.insert(0, REPO)
import push


def frame(n, fill=0x41):
    """An RTCM frame with an n-byte payload (CRC bytes are not checked)."""
    return b"\xd3" + bytes([(n >> 8) & 3, n & 0xFF]) + bytes([fill]) * n + b"\x00\x00\x00"


CMD = gnss.GST_ENABLE_COMMAND
WIRE_CMD = (CMD + "\r\n").encode()
RETURNS_NONE = object()   # accept-script marker: uart.write returns None


class ScriptedUART(FakeUART):
    """accept: one entry per write call - a number of bytes taken, None = all,
    RETURNS_NONE = return None (nothing taken), an exception = raise it; an
    empty list takes everything. The list is used as given, so a test can
    change it while a run is in progress (UART recovery)."""

    def __init__(self, accept=None):
        super().__init__()
        self.accept = [] if accept is None else accept
        self.calls = []

    def write(self, data):
        self.calls.append(data)
        step = self.accept.pop(0) if self.accept else None
        if isinstance(step, BaseException):
            raise step
        if step is RETURNS_NONE:
            return None
        n = len(data) if step is None else min(step, len(data))
        self.written.extend(bytes(data[:n]))
        return n


async def send(tx, data):
    if not tx.write_rtcm(data):
        await tx.drain_rtcm()


class UartTxCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = [0]
        patches = (mock.patch.object(gnss.time, "ticks_ms", lambda: self.clock[0]),
                   mock.patch.object(gnss.asyncio, "sleep_ms", self.fake_sleep),
                   mock.patch.object(gnss, "log", self.record_log))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.fresh()

    def fresh(self, accept=None):
        """A new transmitter on a new scripted UART; clock, sleeps and results reset."""
        self.clock[0] = 0
        self.sleeps = 0
        self.on_sleep = None
        self.results = []
        self.logs = []
        self.uart = ScriptedUART([] if accept is None else accept)
        self.tx = gnss.UartTx(self.uart)

    async def fake_sleep(self, ms):
        self.clock[0] += ms
        self.sleeps += 1
        if self.on_sleep is not None:
            self.on_sleep(self.sleeps)
        await asyncio.sleep(0)

    def record_log(self, level, tag, message):
        self.logs.append((level, tag, message))

    def done(self, ok, error, now_ms):
        self.results.append((ok, error))

    def warnings(self):
        return [line for line in self.logs if line[0] == "WARN"]


class TestUartTx(UartTxCase):
    async def test_command_waits_for_the_end_of_a_frame(self):
        a, b = frame(20), frame(8, 0x42)
        self.assertTrue(self.tx.write_rtcm(a[:10]))
        self.assertEqual(self.tx.submit(CMD, self.done), (True, None))
        self.assertEqual(bytes(self.uart.written), a[:10])           # nothing inserted
        self.assertEqual(self.results, [])
        self.assertTrue(self.tx.write_rtcm(a[10:] + b))
        self.assertEqual(bytes(self.uart.written), a + WIRE_CMD + b)
        self.assertEqual(self.results, [(True, None)])
        c = self.tx.counters
        self.assertEqual((c["uart_commands_sent"], c["uart_commands_deferred"], c["uart_bytes"]),
                         (1, 1, len(a) + len(b)))

    def test_full_write_passes_the_chunk_without_a_payload_copy(self):
        a = frame(30)
        self.assertFalse(inspect.iscoroutinefunction(gnss.UartTx.write_rtcm))
        self.assertTrue(self.tx.write_rtcm(a))
        self.assertIs(self.uart.calls[0], a)
        self.assertTrue(self.tx.at_boundary())
        self.assertFalse(self.tx.busy)

    def test_counters_start_at_zero(self):
        self.assertEqual(self.tx.counters, dict.fromkeys((
            "uart_writes", "uart_bytes", "uart_short_writes", "uart_write_ms",
            "uart_write_max_ms", "uart_dropped_bytes", "uart_commands_sent",
            "uart_commands_deferred", "uart_commands_dropped",
            "uart_command_wait_max_ms"), 0))
        self.assertIs(self.tx.uart, self.uart)
        self.assertFalse(self.tx.busy)
        self.assertTrue(self.tx.at_boundary())

    def test_a_second_chunk_while_one_is_open_is_refused(self):
        self.uart.accept[:] = [5]
        self.assertFalse(self.tx.write_rtcm(frame(20)))
        with self.assertRaises(RuntimeError):
            self.tx.write_rtcm(frame(8))

    def test_idle_command_is_written_at_once(self):
        self.assertEqual(self.tx.submit(CMD, self.done), (True, None))
        self.assertEqual(bytes(self.uart.written), WIRE_CMD)
        self.assertEqual(self.results, [(True, None)])
        self.assertEqual(self.tx.counters["uart_commands_deferred"], 0)
        self.assertFalse(self.tx.busy)

    def test_command_without_done_logs_its_result(self):
        self.assertEqual(self.tx.submit(CMD), (True, None))
        self.assertIn(("INFO", "GNSS", "Command sent to module: %s" % CMD), self.logs)
        self.uart.accept[:] = [OSError("busy")]
        self.tx.submit(CMD)
        self.assertIn(("WARN", "GNSS", "Command not sent: UART error: busy"), self.logs)

    def test_invalid_command_is_refused_without_writing(self):
        self.assertEqual(self.tx.submit("$GPGGA,1*00", self.done),
                         (False, "Only $PQTM and $PAIR commands are allowed."))
        self.assertEqual(self.uart.calls, [])
        self.assertEqual(self.results, [])
        self.assertEqual(self.tx.submit(b"$PQTMVERNO*58", self.done), (True, None))   # BLE
        self.assertEqual(bytes(self.uart.written), b"$PQTMVERNO*58\r\n")

    def test_garbage_between_frames_is_a_boundary(self):
        self.assertTrue(self.tx.write_rtcm(b"\x00\x01"))
        self.tx.submit(CMD, self.done)
        self.assertEqual(bytes(self.uart.written), b"\x00\x01" + WIRE_CMD)
        self.assertEqual(self.results, [(True, None)])

    def test_false_sync_byte_is_not_a_frame(self):
        self.assertTrue(self.tx.write_rtcm(b"\xd3\xfc"))
        self.assertTrue(self.tx.at_boundary())
        self.assertTrue(self.tx.write_rtcm(b"\x00\xd3"))
        self.assertFalse(self.tx.at_boundary())

    def test_false_sync_releases_a_waiting_command_at_once(self):
        b = frame(8, 0x42)
        self.tx.write_rtcm(b"\xd3")                              # maybe a frame start
        self.tx.submit(CMD, self.done)
        self.assertEqual(self.results, [])
        self.tx.write_rtcm(b"\xfc" + b)                         # it was not
        self.assertEqual(bytes(self.uart.written), b"\xd3\xfc" + WIRE_CMD + b)

    def test_header_split_across_chunks(self):
        a, b = frame(20), frame(8, 0x42)
        self.tx.write_rtcm(a[:1])
        self.tx.submit(CMD, self.done)
        self.tx.write_rtcm(a[1:2])
        self.assertEqual(self.results, [])
        self.tx.write_rtcm(a[2:] + b)
        self.assertEqual(bytes(self.uart.written), a + WIRE_CMD + b)
        self.assertEqual(self.results, [(True, None)])

    def test_cursor_skips_payload_containing_d3(self):
        a, b = frame(40, 0xD3), frame(8, 0x42)
        self.tx.write_rtcm(a[:5])
        self.tx.submit(CMD, self.done)
        self.tx.write_rtcm(a[5:] + b)
        self.assertEqual(bytes(self.uart.written), a + WIRE_CMD + b)

    def test_maximum_frame_length(self):
        a = frame(1023)
        self.assertEqual(len(a), 1029)
        self.tx.write_rtcm(a[:400])
        self.tx.submit(CMD, self.done)
        self.tx.write_rtcm(a[400:800])
        self.assertEqual(self.results, [])
        self.tx.write_rtcm(a[800:])
        self.assertEqual(bytes(self.uart.written), a + WIRE_CMD)
        self.assertEqual(self.results, [(True, None)])

    async def test_short_write_rest_is_written_before_the_next_chunk_and_any_command(self):
        a = frame(20)
        self.uart.accept[:] = [5]
        self.on_sleep = lambda n: n == 1 and self.tx.submit(CMD, self.done)
        self.assertFalse(self.tx.write_rtcm(a))
        await self.tx.drain_rtcm()
        self.assertEqual(bytes(self.uart.written), a + WIRE_CMD)
        c = self.tx.counters
        self.assertEqual((c["uart_short_writes"], c["uart_bytes"], c["uart_dropped_bytes"]),
                         (1, len(a), 0))
        self.assertEqual(bytes(self.uart.calls[1]), a[5:])
        self.assertEqual(self.results, [(True, None)])
        self.assertFalse(self.tx.busy)

    async def test_rest_is_completed_first_even_when_it_starts_at_a_frame_end(self):
        a, b = frame(20), frame(8, 0x42)
        self.uart.accept[:] = [len(a), 0]
        self.assertFalse(self.tx.write_rtcm(a + b))
        self.assertTrue(self.tx.at_boundary())
        self.tx.submit(CMD, self.done)
        self.assertEqual(bytes(self.uart.written), a)            # rest before any command
        await self.tx.drain_rtcm()
        self.assertEqual(bytes(self.uart.written), a + b + WIRE_CMD)
        self.assertEqual(self.results, [(True, None)])

    async def test_uart_returning_none_counts_as_nothing_written(self):
        a = frame(20)
        self.uart.accept[:] = [RETURNS_NONE]
        self.assertFalse(self.tx.write_rtcm(a))
        await self.tx.drain_rtcm()
        self.assertEqual(bytes(self.uart.written), a)
        self.assertEqual(self.tx.counters["uart_short_writes"], 1)
        self.assertEqual(self.tx.counters["uart_bytes"], len(a))

    async def _stall_a_multi_frame_chunk(self):
        """Common part of the stall test: returns the chunk; the command waits."""
        a, b, c = frame(20), frame(8, 0x42), frame(5, 0x43)
        chunk = a + b + c
        self.fresh([4] + [0] * 200)
        self.on_sleep = lambda n: n == 1 and self.tx.submit(CMD, self.done)
        self.assertFalse(self.tx.write_rtcm(chunk))
        with self.assertRaises(gnss.UartStalled) as raised:
            await self.tx.drain_rtcm()
        dropped = len(chunk) - 4
        self.assertEqual(raised.exception.args[0],
                         "UART stalled; %d correction bytes dropped." % dropped)
        self.assertEqual(self.clock[0], gnss.UART_STALL_MS)    # checked before writing
        self.assertEqual(len(self.uart.calls), 1 + 99 + 1)     # first, 10..990 ms, submit
        self.assertEqual(self.tx.counters["uart_dropped_bytes"], dropped)
        self.assertEqual(bytes(self.uart.written), chunk[:4])
        self.assertFalse(self.tx.at_boundary())                 # no artificial boundary
        self.assertEqual(self.results, [])
        self.assertEqual(len(self.warnings()), 1)
        self.assertEqual(self.warnings()[0][1:],
                         ("GNSS", "UART stalled; %d correction bytes dropped." % dropped))
        await self.tx.drain_rtcm()                               # raised once only
        self.tx.pump()
        self.assertTrue(self.tx.busy)                            # the command still waits
        return chunk

    async def test_stalled_rest_aborts_the_chunk_and_reports_it(self):
        with self.subTest("UART recovers"):
            chunk = await self._stall_a_multi_frame_chunk()
            self.uart.accept.clear()
            self.tx.end_stream()
            self.assertEqual(bytes(self.uart.written), chunk[:4] + WIRE_CMD)
            self.assertEqual(self.results, [(True, None)])
            self.assertFalse(self.tx.busy)
            self.assertTrue(self.tx.at_boundary())
        with self.subTest("UART stays stuck"):
            chunk = await self._stall_a_multi_frame_chunk()
            self.uart.accept[:] = [0] * 300
            calls = len(self.uart.calls)
            self.tx.end_stream()
            self.assertEqual(len(self.uart.calls), calls + 1)    # command begun, 0 bytes
            self.assertEqual(self.results, [])
            self.assertTrue(self.tx.busy)
            self.tx.pump(self.clock[0] + gnss.UART_STALL_MS - 1)
            self.assertEqual(self.results, [])
            self.tx.pump(self.clock[0] + gnss.UART_STALL_MS)
            self.assertEqual(self.results, [(False, gnss.COMMAND_INCOMPLETE)])
            self.assertEqual(bytes(self.uart.written), chunk[:4])
            self.assertFalse(self.tx.busy)

    async def test_slow_progress_is_bounded_by_the_total_deadline(self):
        with self.subTest("RTCM"):
            self.fresh([1] * 400)
            a = frame(300)
            self.assertEqual(len(a), 306)
            self.assertFalse(self.tx.write_rtcm(a))
            with self.assertRaises(gnss.UartStalled):
                await self.tx.drain_rtcm()
            self.assertEqual(self.clock[0], gnss.UART_REST_MAX_MS)
            self.assertEqual(bytes(self.uart.written), a[:200])
            self.assertEqual(len(self.uart.calls), 200)
            self.assertEqual(self.tx.counters["uart_dropped_bytes"], 106)
        with self.subTest("command"):
            self.fresh([1] * 400)
            self.assertGreater(len(WIRE_CMD), 4)
            self.assertEqual(self.tx.submit(CMD, self.done, 0), (True, None))
            self.assertEqual(bytes(self.uart.written), WIRE_CMD[:1])
            for t, n in ((900, 2), (1800, 3), (1999, 4)):
                self.tx.pump(t)
                self.assertEqual(bytes(self.uart.written), WIRE_CMD[:n])
                self.assertEqual(self.results, [])
            self.tx.pump(2000)
            self.assertEqual(self.results, [(False, gnss.COMMAND_INCOMPLETE)])
            self.assertEqual(bytes(self.uart.written), WIRE_CMD[:4])
            self.assertEqual(len(self.uart.calls), 4)            # no fifth attempt
            self.assertFalse(self.tx.busy)

    async def test_expired_rtcm_rest_is_not_written_after_a_late_pump(self):
        cases = (("progress deadline", [5, 0],
                  (gnss.UART_STALL_MS - 1, gnss.UART_STALL_MS), 2, 5),
                 ("total deadline", [5, 1, 1], (900, 1800, 2000), 3, 7),
                 ("late call", [5], (2100,), 1, 5))
        for name, accept, times, calls, written in cases:
            with self.subTest(name):
                self.fresh(accept)
                a = frame(20)
                self.assertFalse(self.tx.write_rtcm(a))
                for t in times:
                    result = self.tx.pump(t)
                self.assertTrue(result)                          # nothing open any more
                self.assertEqual(self.uart.accept, [])           # the UART takes all again
                self.assertEqual(len(self.uart.calls), calls)
                self.assertEqual(bytes(self.uart.written), a[:written])
                self.assertEqual(self.tx.counters["uart_dropped_bytes"], len(a) - written)
                self.assertFalse(self.tx.at_boundary())
                with self.assertRaises(gnss.UartStalled):
                    await self.tx.drain_rtcm()
                self.assertEqual(self.sleeps, 0)
                await self.tx.drain_rtcm()

    async def test_expired_command_rest_is_not_written_after_a_late_pump(self):
        cases = (("progress deadline", [3], (gnss.UART_STALL_MS,), 1, 3),
                 ("total deadline", [3, 1, 1], (900, 1800, 2000), 3, 5))
        for name, accept, times, calls, written in cases:
            with self.subTest(name):
                self.fresh(accept)
                self.tx.submit(CMD, self.done, 0)
                for t in times:
                    self.tx.pump(t)
                self.assertEqual(self.results, [(False, gnss.COMMAND_INCOMPLETE)])
                self.assertEqual(len(self.uart.calls), calls)
                self.assertEqual(bytes(self.uart.written), WIRE_CMD[:written])
                self.assertEqual(self.tx.submit(CMD, self.done), (True, None))
                self.assertTrue(bytes(self.uart.written).endswith(WIRE_CMD))
                self.assertEqual(self.results[1], (True, None))

    async def test_rtcm_error_in_a_foreign_pump_reaches_the_waiting_writer(self):
        a = frame(20)
        self.uart.accept[:] = [5, OSError(5)]
        raised = []

        def gnss_loop_pump(n):
            if n == 1:
                try:
                    self.tx.pump()
                except Exception as e:      # pump() must never raise
                    raised.append(e)
        self.on_sleep = gnss_loop_pump
        self.assertFalse(self.tx.write_rtcm(a))
        with self.assertRaises(OSError) as error:
            await self.tx.drain_rtcm()
        self.assertEqual(error.exception.args[0], 5)
        self.assertEqual(raised, [])
        self.assertFalse(self.tx.busy)
        self.assertEqual(self.tx.counters["uart_dropped_bytes"], 0)
        await self.tx.drain_rtcm()                               # raised once only
        self.tx.pump()

    def test_uart_exception_on_rtcm_is_raised_by_write_rtcm(self):
        self.uart.accept[:] = [OSError(5)]
        with self.assertRaises(OSError):
            self.tx.write_rtcm(frame(20))
        self.assertFalse(self.tx.busy)
        self.tx.end_stream()
        self.assertTrue(self.tx.at_boundary())

    def test_short_command_write_is_completed_before_rtcm(self):
        a = frame(20)
        self.uart.accept[:] = [3]
        self.tx.submit(CMD, self.done)
        self.assertEqual(bytes(self.uart.written), WIRE_CMD[:3])
        self.assertEqual(self.results, [])
        self.assertTrue(self.tx.busy)
        self.assertTrue(self.tx.write_rtcm(a))
        self.assertEqual(bytes(self.uart.written), WIRE_CMD + a)
        self.assertEqual(self.results, [(True, None)])

    def test_command_rest_that_stalls_fails_as_incomplete(self):
        self.uart.accept[:] = [3] + [0] * 200
        self.tx.submit(CMD, self.done)
        self.tx.pump(gnss.UART_STALL_MS)
        self.assertEqual(self.results, [(False, gnss.COMMAND_INCOMPLETE)])
        self.assertFalse(self.tx.busy)
        self.assertEqual(len(self.uart.calls), 1)               # checked before writing

    def test_uart_exception_on_command_reports_uart_error(self):
        self.uart.accept[:] = [OSError("busy")]
        self.tx.submit(CMD, self.done)
        self.assertEqual(self.results, [(False, "UART error: busy")])
        self.tx.submit(CMD, self.done)
        self.assertEqual(bytes(self.uart.written), WIRE_CMD)
        self.assertEqual(self.results[1], (True, None))

    def test_end_stream_mid_frame_releases_commands(self):
        a = frame(20)
        self.tx.write_rtcm(a[:10])
        self.tx.submit(CMD, self.done)
        self.tx.end_stream()
        self.assertEqual(bytes(self.uart.written), a[:10] + WIRE_CMD)
        self.assertTrue(self.tx.at_boundary())
        self.assertEqual(self.results, [(True, None)])

    def test_end_stream_keeps_a_command_rest(self):
        self.uart.accept[:] = [3]
        self.tx.submit(CMD, self.done)
        self.tx.end_stream()
        self.assertEqual(bytes(self.uart.written), WIRE_CMD)
        self.assertEqual(self.results, [(True, None)])
        self.assertEqual(self.tx.counters["uart_dropped_bytes"], 0)

    async def test_end_stream_after_a_cancelled_drain_drops_the_open_chunk(self):
        a, b = frame(20), frame(8, 0x42)
        self.uart.accept[:] = [5] + [0] * 50
        self.assertFalse(self.tx.write_rtcm(a))
        task = asyncio.create_task(self.tx.drain_rtcm())
        while self.sleeps < 1:
            await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.tx.end_stream()
        self.assertEqual(self.tx.counters["uart_dropped_bytes"], len(a) - 5)
        self.assertFalse(self.tx.busy)
        self.uart.accept.clear()
        self.assertTrue(self.tx.write_rtcm(b))                  # no stored error left

    async def test_end_stream_discards_an_error_the_cancelled_writer_never_saw(self):
        a, b = frame(20), frame(8, 0x42)
        self.uart.accept[:] = [5, OSError(5)]
        self.on_sleep = lambda n: n == 1 and self.tx.pump()     # the GNSS loop hits the error
        self.assertFalse(self.tx.write_rtcm(a))
        task = asyncio.create_task(self.tx.drain_rtcm())
        while self.sleeps < 1:
            await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.tx.end_stream()
        self.assertTrue(self.tx.write_rtcm(b))                  # the next stream starts clean
        await self.tx.drain_rtcm()

    def test_command_waiting_too_long_is_dropped_as_busy(self):
        a = frame(20)
        self.tx.write_rtcm(a[:10])
        self.tx.submit(CMD, self.done)
        self.tx.pump(gnss.COMMAND_MAX_WAIT_MS - 1)
        self.assertEqual(self.results, [])
        self.tx.pump(gnss.COMMAND_MAX_WAIT_MS)
        self.assertEqual(self.results, [(False, gnss.COMMAND_BUSY)])
        self.assertEqual(self.tx.counters["uart_commands_dropped"], 1)
        self.assertEqual(bytes(self.uart.written), a[:10])
        self.assertFalse(self.tx.busy)

    def test_expired_command_is_removed_before_its_first_write(self):
        a, b = frame(20), frame(8, 0x42)
        self.tx.write_rtcm(a[:10])
        self.tx.submit(CMD, self.done)
        self.clock[0] = gnss.COMMAND_MAX_WAIT_MS
        self.tx.write_rtcm(a[10:] + b)
        self.assertEqual(bytes(self.uart.written), a + b)
        self.assertEqual(self.results, [(False, gnss.COMMAND_BUSY)])

    async def test_deadlines_survive_tick_wraparound(self):
        period = 1 << 30
        with mock.patch.object(gnss.time, "ticks_ms", lambda: self.clock[0] % period), \
                mock.patch.object(gnss.time, "ticks_diff",
                                  lambda a, b: (a - b + period // 2) % period - period // 2):
            with self.subTest("command wait"):
                self.fresh()
                self.clock[0] = period - 100
                self.tx.write_rtcm(frame(20)[:10])
                self.tx.submit(CMD, self.done)
                self.tx.pump((period - 100 + gnss.COMMAND_MAX_WAIT_MS - 1) % period)
                self.assertEqual(self.results, [])
                self.tx.pump((period - 100 + gnss.COMMAND_MAX_WAIT_MS) % period)
                self.assertEqual(self.results, [(False, gnss.COMMAND_BUSY)])
            with self.subTest("progress deadline"):
                self.fresh([5] + [0] * 200)
                self.clock[0] = period - 50
                self.assertFalse(self.tx.write_rtcm(frame(20)))
                with self.assertRaises(gnss.UartStalled):
                    await asyncio.wait_for(self.tx.drain_rtcm(), 5)
                self.assertEqual(self.clock[0], period - 50 + gnss.UART_STALL_MS)

    def test_command_queue_is_bounded(self):
        a = frame(20)
        commands = [sentence("PQTMVERNO"), sentence("PAIR001,0,0"),
                    sentence("PQTMCFGCNST,R"), CMD]
        self.tx.write_rtcm(a[:10])
        for command in commands:
            self.assertEqual(self.tx.submit(command, self.done), (True, None))
        self.assertEqual(self.tx.submit(CMD, self.done), (False, gnss.COMMAND_QUEUE_FULL))
        self.assertEqual(self.results, [])
        self.tx.write_rtcm(a[10:])
        self.assertEqual(bytes(self.uart.written),
                         a + b"".join((c + "\r\n").encode() for c in commands))
        self.assertEqual(self.results, [(True, None)] * 4)

    def test_command_wait_max_is_measured(self):
        a = frame(20)
        self.tx.write_rtcm(a[:10])
        self.tx.submit(CMD, self.done)
        self.clock[0] = 300
        self.tx.write_rtcm(a[10:])
        self.assertEqual(self.tx.counters["uart_command_wait_max_ms"], 300)

    async def test_await_command_returns_the_write_result(self):
        a = frame(20)
        with self.subTest("written at the frame end"):
            self.tx.write_rtcm(a[:10])
            task = asyncio.create_task(self.tx.command(CMD))
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            self.assertTrue(self.tx.write_rtcm(a[10:]))
            self.assertEqual(await asyncio.wait_for(task, 1), (True, None))
            self.assertEqual(bytes(self.uart.written), a + WIRE_CMD)
        with self.subTest("dropped after the wait deadline"):
            self.fresh()
            self.tx.write_rtcm(a[:10])
            result = await asyncio.wait_for(self.tx.command(CMD), 5)
            self.assertEqual(result, (False, gnss.COMMAND_BUSY))
            self.assertGreaterEqual(self.clock[0], gnss.COMMAND_MAX_WAIT_MS)
            self.assertEqual(bytes(self.uart.written), a[:10])

    async def test_await_command_returns_at_once_when_refused(self):
        with self.subTest("queue full"):
            a = frame(20)
            self.tx.write_rtcm(a[:10])
            for _ in range(gnss.COMMAND_QUEUE_MAX):
                self.assertEqual(self.tx.submit(CMD, self.done), (True, None))
            self.sleeps = 0
            result = await asyncio.wait_for(self.tx.command(CMD), 1)
            self.assertEqual(result, (False, gnss.COMMAND_QUEUE_FULL))
            self.assertEqual(self.sleeps, 0)
            self.assertEqual(self.results, [])
            self.assertEqual(bytes(self.uart.written), a[:10])
            self.tx.end_stream()
            self.assertEqual(self.results, [(True, None)] * 4)   # the queue was unchanged
        with self.subTest("invalid command"):
            self.fresh()
            result = await asyncio.wait_for(self.tx.command("$GPGGA,1*00"), 1)
            self.assertEqual(result, (False, gnss.check_command("$GPGGA,1*00")[1]))
            self.assertEqual(self.sleeps, 0)
            self.assertEqual(self.uart.calls, [])

    async def test_await_idle_command_does_not_sleep(self):
        result = await asyncio.wait_for(self.tx.command(CMD), 1)
        self.assertEqual(result, (True, None))
        self.assertEqual(bytes(self.uart.written), WIRE_CMD)
        self.assertEqual(self.sleeps, 0)

    async def test_random_chunking_never_splits_a_frame(self):
        rng = random.Random(28)
        fill = (0x00, 0x01, 0x03, 0x3F, 0x41, 0x7E, 0xD3, 0xFC, 0xFF)   # never 0x24 ("$")
        garbage = tuple(b for b in fill if b != 0xD3)
        for run in range(200):
            stream, boundaries = bytearray(), {0}
            for _ in range(rng.randint(1, 12)):
                for _ in range(rng.randint(0, 3)):
                    stream.append(rng.choice(garbage))
                    boundaries.add(len(stream))
                n = rng.randint(0, 300)
                stream += (b"\xd3" + bytes([n >> 8, n & 0xFF])
                           + bytes(rng.choice(fill) for _ in range(n + 3)))
                boundaries.add(len(stream))
            stream = bytes(stream)
            accept = [rng.randint(1, 64) if rng.random() < 0.2 else None
                      for _ in range(400)]
            self.fresh(accept)
            submitted, pos = 0, 0
            while pos < len(stream):
                piece = stream[pos:pos + rng.randint(1, 600)]
                pos += len(piece)
                if rng.random() < 0.4:
                    self.assertEqual(self.tx.submit(CMD, self.done), (True, None))
                    submitted += 1
                await send(self.tx, piece)
            self.tx.end_stream()
            for _ in range(100):
                if not self.tx.busy:
                    break
                self.tx.pump()
            wire = bytes(self.uart.written)
            msg = "run %d" % run
            self.assertEqual(wire.replace(WIRE_CMD, b""), stream, msg)
            found, at = 0, wire.find(WIRE_CMD)
            while at >= 0:
                self.assertIn(at - found * len(WIRE_CMD), boundaries, msg)
                found += 1
                at = wire.find(WIRE_CMD, at + len(WIRE_CMD))
            self.assertEqual(self.results, [(True, None)] * submitted, msg)
            self.assertEqual(found, submitted, msg)
            self.assertLess(self.clock[0], gnss.UART_STALL_MS, msg)


class TestTransmitterWiring(unittest.TestCase):
    def test_main_passes_the_transmitter_to_the_ntrip_client(self):
        with open(os.path.join(REPO, "main.py")) as f:
            tree = ast.parse(f.read())
        arguments = [ast.unparse(call.args[0]) for call in ast.walk(tree)
                     if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                     and call.func.id == "NtripClient" and call.args]
        self.assertEqual(arguments, ["gnss.tx"])


VERNO = b"$PQTMVERNO*58"
WIRE_VERNO = VERNO + b"\r\n"


class TestCommandCallers(unittest.IsolatedAsyncioTestCase):
    """POST /gnss and the BLE sink reach the receiver only through UartTx."""

    def setUp(self):
        self.uart = ScriptedUART()
        self.tx = gnss.UartTx(self.uart)
        patches = (mock.patch.object(web, "_admin_allowed", return_value=True),
                   mock.patch.dict(web._instances,
                                   {"gnss": SimpleNamespace(tx=self.tx)}),
                   mock.patch.object(gnss, "log"))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    async def post(self, sentence):
        """POST /api/gnss-command: (status line, body)."""
        writer = FakeWriter()
        body = json.dumps({"sentence": sentence}).encode()
        await web._handle_gnss_request(writer, "/api/gnss-command", "POST", "", body,
                                       False, {"content-type": "application/json"})
        head, _, payload = bytes(writer.buf).partition(b"\r\n\r\n")
        return head.split(b"\r\n")[0].decode(), payload.decode()

    async def test_gnss_command_is_written_when_idle(self):
        status, body = await self.post(VERNO.decode())
        self.assertEqual(status, "HTTP/1.0 200 OK")
        self.assertTrue(body.startswith("sent"), body)
        self.assertEqual(bytes(self.uart.written), WIRE_VERNO)

    async def test_gnss_command_waits_for_the_frame_boundary(self):
        data = frame(20)
        self.assertTrue(self.tx.write_rtcm(data[:10]))
        asyncio.get_running_loop().call_later(0.05, self.tx.write_rtcm, data[10:])
        status, body = await asyncio.wait_for(self.post(VERNO.decode()), 2)
        self.assertEqual(status, "HTTP/1.0 200 OK")
        self.assertEqual(bytes(self.uart.written), data + WIRE_VERNO)

    async def test_gnss_command_busy_answers_503(self):
        data = frame(20)
        self.assertTrue(self.tx.write_rtcm(data[:10]))
        with mock.patch.object(gnss, "COMMAND_MAX_WAIT_MS", 50):
            status, body = await asyncio.wait_for(self.post(VERNO.decode()), 2)
        self.assertEqual(status, "HTTP/1.0 503 Service Unavailable")
        self.assertEqual(body, gnss.COMMAND_BUSY)
        self.assertEqual(bytes(self.uart.written), data[:10])

    async def test_invalid_gnss_command_answers_400_as_before(self):
        status, body = await asyncio.wait_for(self.post("$GPGGA,1*00"), 1)
        self.assertEqual(status, "HTTP/1.0 400 Bad Request")
        self.assertEqual(body, gnss.check_command("$GPGGA,1*00")[1])
        self.assertEqual(self.uart.calls, [])

    async def test_gnss_command_queue_full_answers_503_at_once(self):
        data = frame(20)
        self.assertTrue(self.tx.write_rtcm(data[:10]))
        for _ in range(gnss.COMMAND_QUEUE_MAX):
            self.assertEqual(self.tx.submit(CMD), (True, None))
        with mock.patch.object(gnss.asyncio, "sleep_ms",
                               side_effect=AssertionError("slept")):
            status, body = await asyncio.wait_for(self.post(VERNO.decode()), 1)
        self.assertEqual(status, "HTTP/1.0 503 Service Unavailable")
        self.assertEqual(body, gnss.COMMAND_QUEUE_FULL)
        self.assertEqual(bytes(self.uart.written), data[:10])
        self.tx.end_stream()
        wire = bytes(self.uart.written)
        self.assertEqual(wire, data[:10] + WIRE_CMD * gnss.COMMAND_QUEUE_MAX)
        self.assertNotIn(b"$PQTMVERNO", wire)

    def test_ble_sink_goes_through_the_transmitter(self):
        with open(os.path.join(REPO, "main.py")) as f:
            tree = ast.parse(f.read())
        sinks = [ast.unparse(node.value) for node in ast.walk(tree)
                 if isinstance(node, ast.Assign) and len(node.targets) == 1
                 and ast.unparse(node.targets[0]) == "ble_mgr.command_sink"]
        self.assertEqual(sinks, ["gnss.tx.submit"])

        manager = ble.BLEManager("RTK-D4E5F6")
        conn = 7
        manager.connections.add(conn)
        manager.encrypted_connections.add(conn)
        manager.command_sink = self.tx.submit
        previous = cfg.CONFIG.get("ble_commands_enabled")
        cfg.CONFIG["ble_commands_enabled"] = True
        self.addCleanup(cfg.CONFIG.__setitem__, "ble_commands_enabled", previous)
        data = frame(20)
        self.assertTrue(self.tx.write_rtcm(data[:10]))
        manager.handle_write(manager.rx, conn, VERNO)
        self.assertEqual(bytes(self.uart.written), data[:10])
        self.tx.end_stream()
        self.assertEqual(bytes(self.uart.written), data[:10] + WIRE_VERNO)

    def test_no_direct_uart_writer_is_left(self):
        writers = []
        for name in push.BOARD_FILES:
            if not name.endswith(".py"):
                continue
            with open(os.path.join(REPO, name), encoding="utf-8") as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                named = (node.id if isinstance(node, ast.Name) else
                         node.attr if isinstance(node, ast.Attribute) else
                         node.name if isinstance(node, (ast.FunctionDef, ast.alias))
                         else None)
                self.assertNotEqual(named, "send_command", "%s:%s" % (
                    name, getattr(node, "lineno", "?")))
            classes = [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
            for call in ast.walk(tree):
                if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                        and call.func.attr == "write"):
                    continue
                target = call.func.value
                if not ((isinstance(target, ast.Attribute) and target.attr == "uart")
                        or (isinstance(target, ast.Name) and target.id == "uart")):
                    continue
                owner = [c.name for c in classes
                         if any(n is call for n in ast.walk(c))]
                writers.append((name, owner))
        self.assertTrue(writers)
        self.assertEqual(set((name, tuple(owner)) for name, owner in writers),
                         {("gnss.py", ("UartTx",))})


if __name__ == "__main__":
    unittest.main()
