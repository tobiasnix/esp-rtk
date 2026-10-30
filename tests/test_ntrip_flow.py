# SPDX-License-Identifier: AGPL-3.0-only
"""Exercise the real NTRIP loop against a caster waiting for periodic GGA."""
import ast
import asyncio
import contextlib
import os
import tempfile
import time
import types
import unittest
from unittest import mock

import support
from support import REPO, net, cfg, gnss, web, state, FakeUART
from field_diagnostics import FieldRecorder
from test_rtcm import SUNDAY_0001, msm_epoch
from test_uart_tx import CMD, WIRE_CMD, frame

JUMP_26Y_MS = 820_540_800_000  # 9497 days
INVALID_CLOCK = (2000, 1, 1, 0, 1, 0, 5, 1)       # before NTP/RMC sync
# 5 events per failed attempt, measured on b23f4ef; with six samples 31 of MAX_EVENTS = 32
EXPECTED_EVENTS_FOR_FIVE_FAILED_ATTEMPTS = 25
# Scripted connection attempts for run_caster(attempts=...); the harness copies the lists.
SCRIPT_500 = {"headers": [b"ICY 500 Internal Server Error\r\n"]}
IDLE_WITHOUT_DATA = {"chunks": [(5000, asyncio.TimeoutError())] * 2}   # with idle_timeout=10
FALLBACK = {"ntrip_fallback_host": "fallback.invalid", "ntrip_fallback_mount": "FB"}
# A saved caster change for TestNtripReload: a new primary, or only a new fallback.
NEW = {"ntrip_host": "new.invalid", "ntrip_mount": "NEW"}
OTHER_FALLBACK = {"ntrip_fallback_host": "other.invalid", "ntrip_fallback_mount": "FB2"}
# RTCM frames for the UartTx paths (#28b): 26, 14 and 11 bytes on the wire.
A = frame(20)
B = frame(8, 0x42)
C = frame(5, 0x43)
# The ten ntrip_flow keys the transmitter owns (#28b E9).
TX_COUNTERS = ("uart_writes", "uart_bytes", "uart_short_writes", "uart_write_ms",
               "uart_write_max_ms", "uart_dropped_bytes", "uart_commands_sent",
               "uart_commands_deferred", "uart_commands_dropped", "uart_command_wait_max_ms")


class TestNtripFlow(unittest.IsolatedAsyncioTestCase):
    async def run_caster(self, *, require_gga=True, chunks=None, clock_start=100000,
                         uart_delay=0, uart_accept=None, on_uart_sleep=None, gga_delay=0,
                         sentence="$GNGGA,synthetic", gga_interval=10, gga_source=None,
                         fail_gga=False, idle_timeout=60, wall_jump_per_read=0,
                         extra_config=None, gmtime=SUNDAY_0001,
                         dns_delay=0, dns_sleep=0.0, connect_delay=0, connect_sleep=0.0,
                         addrinfo=None, resolve_error=None, connect_error=None,
                         backoffs=None, record_events=True, attempts=None, stop_when=None,
                         supervised=False, on_read=None, wifi_up=True, on_block=None,
                         writer_style="cpython"):
        """Run the real client against a scripted caster.

        Without attempts, one connection serves chunks and the end of the chunks sets stop.
        With attempts, every connect takes the next script once, at its start (keys
        connect_sleep, connect_error, stop_first, request_error, headers, chunks, block,
        block_end); an exhausted chunk list reads as EOF without stop.
        stop_when is checked on every patched asyncio.sleep; supervised runs the client
        under state.supervise (without relying on its restart delay). on_read(n) runs at
        the start of every read with its number over all connections; a callable entry in
        a script's headers runs at that readline, which then returns the next entry. Hooks
        may change cfg.CONFIG (restored afterwards) and call self.client.reload().

        block ("request_drain", "status_line", "read" once the chunks are used up,
        "gga_drain") makes that operation wait until the connection is closed; on_block
        runs soon after it starts waiting. The close then ends it with block_end:
        "oserror" (default) or "eof" (b"" from readline/read). writer_style "cpython"
        closes with Writer.close(); with "micropython", Writer.close() is a no-op and only
        Writer.s.close() closes (as wait_closed() does). Once closed, every further
        read/readline/drain raises OSError(9). A block not closed within 1 s real time
        is noted in self.unclosed_blocks and raises asyncio.TimeoutError.

        The client writes through a real gnss.UartTx (self.tx). uart_accept has one entry
        per uart.write call: a number of bytes taken, None = all, an exception = raise it;
        exhausted = all. The list itself is used, not a copy, so a hook can change what the
        UART takes from then on. Only taken bytes reach the wire (b"".join(delivered),
        receiver commands included). net.asyncio.sleep_ms (the UartTx waits; sleep stays
        the backoff) advances the clock by ms, then runs on_uart_sleep(n) with its number
        over the whole run, before the pump of that step."""
        if backoffs is None:
            backoffs = 1000 if stop_when is not None else 1
        # The fake getaddrinfo really blocks the loop; that is not a debug-mode finding here.
        asyncio.get_running_loop().slow_callback_duration = 1.0
        stop = asyncio.Event()
        wifi = asyncio.Event()
        if wifi_up:
            wifi.set()
        clock = [clock_start]
        reads = [0]
        wall_offset = [0]
        writes = []
        delivered = []
        chunks = list(chunks or [])
        attempts = None if attempts is None else list(attempts)
        self.wifi = wifi
        self.clock = clock
        self.writes = writes
        self.connect_clocks = []    # harness clock at every connect
        self.states = []        # every app.set_ntrip_state argument
        self.state_codes = []   # (state, last_ntrip_error_code) at every set_ntrip_state
        self.beats = []         # harness clock at every app.beat("ntrip")
        self.events = []        # (kind, data, stats snapshot) from field_recorder.event
        self.snapshots = []     # app.stats["ntrip_substate"] at the start of every read
        self.resolved = []      # (host, port) handed to getaddrinfo
        self.connected = []     # (host, port) handed to open_connection
        self.resets = []        # RtcmCounter.reset_stream calls, with the counts at that time
        self.connect_times = []     # time.monotonic() at every connect start
        self.closes = []        # per connection: {"calls": close calls, "phase": flow phase at the first}
        self.unclosed_blocks = []   # scripted blocks that no close ended
        original_reset = net.RtcmCounter.reset_stream

        def spy_reset(counter):
            self.resets.append(dict(counter.counts))
            original_reset(counter)

        def record_event(kind, data=None):
            self.events.append((kind, data, {
                key: net.app.stats.get(key) for key in
                ("ntrip_retries", "last_ntrip_error_code", "ntrip_substate",
                 "ntrip_backoff_sec")}))

        original_set_ntrip_state = net.app.set_ntrip_state
        original_beat = net.app.beat

        def spy_set_ntrip_state(value):
            self.states.append(value)
            self.state_codes.append((value, net.app.stats.get("last_ntrip_error_code")))
            original_set_ntrip_state(value)

        def spy_beat(name):
            if name == "ntrip":
                self.beats.append(clock[0])
            original_beat(name)

        self_outer = self

        class Connection:
            """What a connection's Reader and Writer share: its script and its close."""

            def __init__(self, script):
                self.script = script or {}
                self.closed = asyncio.Event()
                self.record = {"calls": 0, "phase": None}
                self_outer.closes.append(self.record)

            def close(self, closing):
                if not self.record["calls"]:
                    self.record["phase"] = self_outer.client._flow["phase"]
                self.record["calls"] += 1
                if closing:
                    self.closed.set()

            def check_open(self):
                if self.closed.is_set():
                    raise OSError(9, "closed")

            def blocks(self, operation):
                return self.script.get("block") == operation

            async def blocked(self, operation):
                if on_block is not None:
                    asyncio.get_running_loop().call_soon(on_block)
                try:
                    await asyncio.wait_for(self.closed.wait(), 1.0)
                except asyncio.TimeoutError:
                    self_outer.unclosed_blocks.append(operation)
                    raise
                if self.script.get("block_end", "oserror") == "oserror":
                    raise OSError(9, "closed")
                return b""

        class Reader:
            def __init__(self, connection):
                self.connection = connection
                script = connection.script
                self.scripted = attempts is not None
                self.headers = list(script.get("headers", [b"ICY 200 OK\r\n", b"\r\n"]))
                self.chunks = list(script.get("chunks", [])) if self.scripted else chunks
                self.lines = 0

            async def readline(self):
                self.connection.check_open()
                self.lines += 1
                if self.lines == 1 and self.connection.blocks("status_line"):
                    return await self.connection.blocked("status_line")
                line = self.headers.pop(0)
                if callable(line):
                    line()
                    line = self.headers.pop(0)
                if isinstance(line, BaseException):
                    raise line
                return line

            async def read(self, size):
                self.connection.check_open()
                reads[0] += 1
                if on_read is not None:
                    on_read(reads[0])
                wall_offset[0] += wall_jump_per_read
                self_outer.snapshots.append(net.app.stats.get("ntrip_substate"))
                if require_gga:
                    if len([v for v in writes if v.startswith(b"$GNGGA")]) < 2:
                        clock[0] += 5000
                        if clock[0] - clock_start >= 30000:
                            stop.set()
                        raise asyncio.TimeoutError
                    stop.set()
                    return b"corrections-after-gga"
                if not self.chunks:
                    if self.connection.blocks("read"):
                        return await self.connection.blocked("read")
                    if not self.scripted:
                        stop.set()
                    return b""
                delay, data = self.chunks.pop(0)
                clock[0] += delay
                if isinstance(data, BaseException):
                    raise data
                return data

        class Writer:
            def __init__(self, connection):
                self.connection = connection
                self.request_error = connection.script.get("request_error")
                if writer_style == "micropython":
                    # Stream.close() of MicroPython 1.28 is a no-op; the socket closes.
                    self.s = types.SimpleNamespace(close=lambda: connection.close(True))

            def write(self, data):
                writes.append(bytes(data))

            async def drain(self):
                self.connection.check_open()
                if writes[-1].startswith(b"GET "):
                    if self.request_error is not None:
                        raise self.request_error
                    if self.connection.blocks("request_drain"):
                        await self.connection.blocked("request_drain")
                if writes[-1].startswith(b"$GNGGA"):
                    if self.connection.blocks("gga_drain"):
                        await self.connection.blocked("gga_drain")
                    clock[0] += gga_delay
                    if fail_gga:
                        raise OSError("synthetic GGA send failure")

            def close(self):
                self.connection.close(writer_style == "cpython")

            async def wait_closed(self):
                if writer_style == "micropython":
                    self.s.close()

        class UART(FakeUART):
            def write(self, data):
                clock[0] += uart_delay
                step = accept.pop(0) if accept else None
                if isinstance(step, BaseException):
                    raise step
                taken = len(data) if step is None else min(step, len(data))
                if taken:
                    delivered.append(bytes(data[:taken]))
                return taken

        def getaddrinfo(host, port, *args):
            clock[0] += dns_delay
            time.sleep(dns_sleep)       # a real blocking call, as on the device
            self.resolved.append((host, port))
            if resolve_error is not None:
                raise resolve_error
            return [(2, 1, 0, "", ("192.0.2.10", port))] if addrinfo is None else addrinfo

        async def connect(host, port):
            self.connect_times.append(time.monotonic())
            self.connect_clocks.append(clock[0])
            clock[0] += connect_delay
            self.connected.append((host, port))
            # Taken once, at the start, and kept: a connect that its deadline cancels
            # must not leave its script to the next attempt.
            script = attempts.pop(0) if attempts else None
            sleep = connect_sleep if script is None else script.get("connect_sleep", connect_sleep)
            await asyncio.sleep(sleep)      # the real asyncio, not net.asyncio.sleep
            if connect_error is not None:
                raise connect_error
            if attempts is None:
                connection = Connection(None)
                return Reader(connection), Writer(connection)
            if script is None:
                stop.set()
                raise OSError("no scripted attempt")
            if script.get("stop_first"):
                stop.set()
            if script.get("connect_error") is not None:
                raise script["connect_error"]
            connection = Connection(script)
            return Reader(connection), Writer(connection)

        backoff_calls = [0]

        async def backoff(seconds):
            clock[0] += int(seconds * 1000)
            backoff_calls[0] += 1
            if backoff_calls[0] >= backoffs:
                stop.set()
            if stop_when is not None and stop_when():
                stop.set()

        accept = [] if uart_accept is None else uart_accept
        uart_sleeps = [0]

        async def uart_sleep(ms):
            clock[0] += ms
            uart_sleeps[0] += 1
            if on_uart_sleep is not None:
                on_uart_sleep(uart_sleeps[0])
            await asyncio.sleep(0)          # the real asyncio, not net.asyncio.sleep

        def spies_and_supervisor():
            # One stack: a single with statement allows at most 20 nested blocks.
            stack = contextlib.ExitStack()
            stack.enter_context(mock.patch.object(net.asyncio, "sleep_ms", uart_sleep))
            stack.enter_context(mock.patch.object(gnss, "log"))     # UartTx log lines
            stack.enter_context(mock.patch.dict(net.app.heartbeat, {}, clear=True))
            stack.enter_context(mock.patch.object(net.app, "set_ntrip_state", spy_set_ntrip_state))
            stack.enter_context(mock.patch.object(net.app, "beat", spy_beat))
            if supervised:
                stack.enter_context(mock.patch.object(state, "shutdown_event", stop))
                stack.enter_context(support.patch_module_global("state", "log"))
            return stack

        period = 1 << 30
        with mock.patch.dict(cfg.CONFIG, {
                "ntrip_enabled": True, "ntrip_host": "caster.invalid",
                "ntrip_mount": "SYNTHETIC", "ntrip_user": "", "ntrip_pass": "",
                "ntrip_fallback_host": "", "ntrip_gga_interval_sec": gga_interval,
                "ntrip_idle_timeout_sec": idle_timeout, "ntrip_read_timeout_sec": 5,
                **(extra_config or {})}), \
                mock.patch.object(net.time, "gmtime", return_value=gmtime), \
                (mock.patch.object(net.field_recorder, "event", side_effect=record_event)
                 if record_events else contextlib.nullcontext()), \
                mock.patch.object(net, "socket", types.SimpleNamespace(
                    getaddrinfo=getaddrinfo, SOCK_STREAM=1), create=True), \
                mock.patch.object(net.RtcmCounter, "reset_stream", spy_reset), \
                mock.patch.dict(net.app.stats, {"ntrip_bytes": 0, "ntrip_gga_sent": 0,
                                              "ntrip_retries": 0, "task_restarts": 0},
                                clear=True), \
                spies_and_supervisor(), \
                mock.patch.object(net, "shutdown_event", stop), \
                mock.patch.object(net.app, "wifi_connected_event", wifi), \
                mock.patch.object(net.app, "gga_for_caster",
                                  gga_source or (lambda now_ms=None: sentence)), \
                mock.patch.object(net.asyncio, "open_connection", connect), \
                mock.patch.object(net.asyncio, "sleep", backoff), \
                mock.patch.object(net.time, "time", lambda: (clock[0] + wall_offset[0]) // 1000), \
                mock.patch.object(net.time, "ticks_ms", lambda: clock[0] % period), \
                mock.patch.object(net.time, "ticks_diff",
                                  lambda a, b: (a - b + period // 2) % period - period // 2), \
                mock.patch.object(net, "log") as net_log, mock.patch.object(net.app, "log_error"):
            self.net_log = net_log
            self.tx = gnss.UartTx(UART())
            client = net.NtripClient(self.tx)
            self.client = client
            try:
                if supervised:
                    await asyncio.wait_for(state.supervise("ntrip", client.run), 2)
                else:
                    await asyncio.wait_for(client.run(), 2)
            finally:
                self.final_stats = dict(net.app.stats)     # also when run() raises
            flow = client.flow_summary() if hasattr(client, "flow_summary") else None
            stats = dict(net.app.stats)
            return delivered, writes, stats, flow

    async def test_caster_receives_periodic_gga_while_corrections_are_silent(self):
        delivered, writes, stats, _ = await self.run_caster()
        self.assertEqual(delivered, [b"corrections-after-gga"])
        self.assertEqual(stats["ntrip_bytes"], len(delivered[0]))
        self.assertEqual(stats["ntrip_retries"], 0)
        self.assertEqual(len([v for v in writes if v.startswith(b"$GNGGA")]), 2)

    async def test_receive_gap_includes_timeout_and_uart_delay(self):
        delivered, _, _, flow = await self.run_caster(require_gga=False,
            chunks=[(30, b"abc"), (5000, asyncio.TimeoutError()), (7000, b"defg")],
            uart_delay=20)
        self.assertEqual(delivered, [b"abc", b"defg"])
        self.assertEqual(flow["rx_gap_max_ms"], 12020)
        self.assertEqual(flow["read_wait_max_ms"], 7000)
        self.assertEqual(flow["read_timeouts"], 1)
        self.assertEqual(flow["rx_bytes"], 7)
        self.assertEqual(flow["uart_bytes"], 7)
        self.assertEqual(flow["uart_short_writes"], 0)

    async def test_uart_delay_is_measured_separately_from_read_wait(self):
        _, _, _, flow = await self.run_caster(require_gga=False,
            chunks=[(10, b"abc"), (25, b"de")], uart_delay=70)
        self.assertEqual(flow["uart_write_max_ms"], 70)
        self.assertEqual(flow["read_wait_max_ms"], 25)
        self.assertEqual(flow["rx_gap_max_ms"], 95)
        self.assertEqual(flow["uart_writes"], 2)
        self.assertEqual(flow["rx_chunks"], 2)

    async def test_gga_drain_time_and_failures_remain_visible(self):
        _, _, _, flow = await self.run_caster(require_gga=False,
            chunks=[(30, b"abc")], gga_delay=250, fail_gga=True)
        self.assertEqual(flow["gga_write_max_ms"], 250)
        # The failed initial upload is retried after the received correction.
        self.assertEqual(flow["gga_failures"], 2)
        self.assertEqual(flow["gga_writes"], 0)
        self.assertIsNone(flow["last_gga_ticks_ms"])

    async def test_tick_wrap_does_not_hide_receive_gap(self):
        _, _, _, flow = await self.run_caster(require_gga=False,
            chunks=[(30, b"a"), (60, b"b")], clock_start=(1 << 30) - 50)
        self.assertEqual(flow["rx_gap_max_ms"], 60)
        self.assertEqual(flow["read_wait_max_ms"], 60)
        self.assertEqual(flow["last_rx_ticks_ms"], 40)
        self.assertEqual(flow["rx_age_ms"], 0)

    async def test_short_uart_write_is_completed_before_the_next_chunk(self):
        delivered, _, stats, flow = await self.run_caster(require_gga=False,
            chunks=[(10, b"abc"), (10, b"de")], uart_accept=[2])
        self.assertEqual(b"".join(delivered), b"abcde")
        self.assertEqual(flow["uart_bytes"], 5)
        self.assertEqual(flow["uart_short_writes"], 1)
        self.assertEqual(flow["uart_dropped_bytes"], 0)
        self.assertEqual(stats["ntrip_bytes"], 5)
        self.assertEqual(flow["rx_bytes"], 5)

    async def test_command_never_lands_inside_a_frame_split_over_two_reads(self):
        def on_read(n):
            if n == 2:
                self.assertEqual(self.tx.submit(CMD), (True, None))
        delivered, _, _, flow = await self.run_caster(require_gga=False,
            chunks=[(10, A[:10]), (10, A[10:] + B)], on_read=on_read)
        self.assertEqual(b"".join(delivered), A + WIRE_CMD + B)
        self.assertEqual(flow["uart_commands_deferred"], 1)

    async def test_stream_end_mid_frame_releases_a_waiting_command(self):
        results = []

        def on_read(n):
            if n == 2:
                self.tx.submit(CMD, lambda ok, error, now_ms: results.append((ok, error)))
        delivered, _, _, _ = await self.run_caster(require_gga=False,
            chunks=[(10, A[:10])], on_read=on_read)
        self.assertEqual(b"".join(delivered), A[:10] + WIRE_CMD)
        self.assertEqual(results, [(True, None)])
        self.assertTrue(self.tx.at_boundary())

    async def test_uart_error_while_writing_corrections_counts_as_before(self):
        _, _, stats, _ = await self.run_caster(require_gga=False,
            chunks=[(10, A)], uart_accept=[OSError(5)])
        self.assertEqual([e["errno"] for e in self.errors()], [5])
        self.assertEqual(stats["ntrip_retries"], 1)
        self.assertEqual(stats["last_ntrip_error_code"], "network_error")
        self.assertFalse(self.tx.busy)

    async def test_uart_error_in_the_gnss_pump_is_accounted_to_the_ntrip_attempt(self):
        pumped = []

        def on_uart_sleep(n):
            if n == 1:
                self.tx.pump()          # the GNSS loop writes the rest; it must not raise
                pumped.append(n)
        delivered, _, stats, _ = await self.run_caster(require_gga=False,
            chunks=[(10, A)], uart_accept=[5, OSError(5)], on_uart_sleep=on_uart_sleep)
        self.assertEqual(pumped, [1])
        self.assertEqual([(e["error_type"], e["errno"]) for e in self.errors()],
                         [("OSError", 5)])
        self.assertEqual(stats["ntrip_retries"], 1)
        self.assertEqual(stats["last_ntrip_error_code"], "network_error")
        self.assertEqual(b"".join(delivered), A[:5])

    async def test_stalled_correction_write_ends_the_stream(self):
        accept = [5] + [0] * 500
        results = []

        def on_uart_sleep(n):
            if n == 1:
                self.tx.submit(CMD, lambda ok, error, now_ms: results.append((ok, error)))
            if n == gnss.UART_STALL_MS // gnss.UART_RETRY_MS:
                accept.clear()          # the UART recovers exactly at the progress deadline
        delivered, _, stats, flow = await self.run_caster(require_gga=False,
            chunks=[(10, A + B + C)], uart_accept=accept, on_uart_sleep=on_uart_sleep)
        # No further byte of the chunk although the UART takes everything again: the
        # deadline is checked before writing. The command goes out through end_stream().
        self.assertEqual(b"".join(delivered), (A + B + C)[:5] + WIRE_CMD)
        self.assertEqual(results, [(True, None)])
        self.assertEqual(flow["uart_dropped_bytes"], len(A + B + C) - 5)
        self.assertEqual(flow["uart_commands_sent"], 1)
        self.assertEqual([e["error_type"] for e in self.errors()], ["UartStalled"])
        self.assertEqual(stats["last_ntrip_error_code"], "unexpected_error")
        self.assertEqual(stats["ntrip_retries"], 1)
        self.assertEqual(len(self.backoff_delays()), 1)
        self.assertFalse(self.tx.busy)

    async def test_full_writes_never_wait_on_the_transmitter(self):
        with mock.patch.object(gnss.UartTx, "drain_rtcm",
                               side_effect=AssertionError("drained")):
            delivered, _, _, _ = await self.run_caster(require_gga=False,
                chunks=[(10, A), (10, B)])
        self.assertEqual(self.errors(), [])
        self.assertEqual(b"".join(delivered), A + B)

    async def test_flow_summary_carries_the_transmitter_counters(self):
        _, _, _, flow = await self.run_caster(require_gga=False, chunks=[(10, A)])
        for key in TX_COUNTERS:
            self.assertIn(key, flow)
        self.assertEqual(flow["uart_bytes"], len(A))
        self.assertEqual([k for k in self.client._flow if k.startswith("uart_")], [])

    async def test_no_position_or_disabled_gga_does_not_send_invalid_position(self):
        for options in ({"sentence": None}, {"gga_interval": 0}):
            with self.subTest(options=options):
                delivered, writes, _, flow = await self.run_caster(**options)
                self.assertEqual(delivered, [])
                self.assertFalse(any(v.startswith(b"$GNGGA") for v in writes))
                self.assertEqual(flow["gga_writes"], 0)
                self.assertEqual(flow["read_timeouts"], 6)

    async def test_gga_during_timeouts_does_not_extend_idle_timeout(self):
        _, _, _, flow = await self.run_caster(require_gga=False,
            chunks=[(5000, asyncio.TimeoutError())] * 8, idle_timeout=12)
        self.assertEqual(flow["read_timeouts"], 3)
        self.assertEqual(flow["rx_chunks"], 0)
        self.assertEqual(flow["gga_writes"], 2)

    async def test_caster_gets_no_gga_once_the_fix_is_withheld(self):
        calls = []

        def source(now_ms=None):
            calls.append(now_ms)
            return "$GNGGA,synthetic" if len(calls) == 1 else None
        _, writes, stats, flow = await self.run_caster(
            require_gga=False, chunks=[(5000, asyncio.TimeoutError())] * 4, gga_source=source)
        self.assertEqual([v for v in writes if v.startswith(b"$GNGGA")], [b"$GNGGA,synthetic\r\n"])
        self.assertEqual(flow["gga_writes"], 1)
        self.assertEqual(stats["ntrip_gga_sent"], 1)
        self.assertGreaterEqual(len(calls), 2)          # the due interval asked again

    async def test_caster_gets_gga_once_a_fresh_fix_returns(self):
        calls = []

        def source(now_ms=None):
            calls.append(now_ms)
            return None if len(calls) <= 2 else "$GNGGA,back"
        _, writes, _, flow = await self.run_caster(
            require_gga=False, chunks=[(5000, asyncio.TimeoutError())] * 4, gga_source=source)
        self.assertEqual([v for v in writes if v.startswith(b"$GNGGA")], [b"$GNGGA,back\r\n"])
        self.assertEqual(flow["gga_writes"], 1)
        self.assertEqual(flow["gga_failures"], 0)

    async def test_caster_gets_gga_at_the_next_due_check_after_the_fix_returns(self):
        # Irregular read completions; the fix returns at t=104000, right after a due check.
        calls = []

        def source(now_ms=None):
            now = time.ticks_ms()                      # the harness clock (shared time module)
            calls.append(now)
            return "$GNGGA,back" if now >= 104000 else None
        _, writes, _, flow = await self.run_caster(require_gga=False, gga_source=source,
            chunks=[(3000, b"a"), (7000, asyncio.TimeoutError()), (1500, b"b"),
                    (4000, asyncio.TimeoutError()), (2500, b"c")])
        self.assertEqual([v for v in writes if v.startswith(b"$GNGGA")], [b"$GNGGA,back\r\n"])
        # handshake (none), first data block (due: _last_gga None, none), timeout at 115500
        # (10 s after the last attempt) -> sent 11.5 s after the fix returned
        self.assertEqual(calls, [100000, 103000, 115500])
        self.assertEqual(flow["last_gga_ticks_ms"], 115500)
        self.assertEqual(flow["gga_writes"], 1)

    async def test_sende_gga_sends_only_the_sentence_of_a_fresh_fix(self):
        now = [10000]
        app = state.AppState()
        data = []

        class Writer:
            def write(self, d):
                data.append(bytes(d))

            async def drain(self):
                pass

        def publish(raw, qual):
            fix = {"utc": "120000.00", "lat": 50.0, "lon": 8.0, "alt": 100.0, "qual": qual,
                   "sats": 20, "hdop": 0.6, "fix_status_text": "RTK_FIXED",
                   "lat_e8": 5000000000, "lon_e8": 800000000, "alt_mm": 100000, "raw": raw}
            with mock.patch("tracking.tracker") as tracker:
                tracker.enqueue_fix.return_value = False
                app.update_fix(fix)

        with mock.patch.object(time, "ticks_ms", side_effect=lambda: now[0]), \
                mock.patch.object(net, "app", app), \
                mock.patch.dict(cfg.CONFIG, {"ntrip_gga_interval_sec": 10}), \
                mock.patch.object(net, "log"), support.patch_module_global("state", "log"):
            client = net.NtripClient(gnss.UartTx(FakeUART()))
            writer = Writer()
            publish("$GNGGA,A", 4)
            self.assertTrue(await client._sende_gga(writer))
            self.assertEqual(data, [b"$GNGGA,A\r\n"])
            now[0] += 3000                              # fix_stale_after_ms
            self.assertFalse(await client._sende_gga(writer))
            self.assertEqual(data, [b"$GNGGA,A\r\n"])
            self.assertEqual(app.stats["ntrip_gga_sent"], 1)
            self.assertEqual(client._flow["gga_writes"], 1)
            self.assertEqual(client._flow["gga_failures"], 0)
            publish("$GNGGA,B", 4)
            self.assertTrue(await client._sende_gga(writer))
            self.assertEqual(data[-1], b"$GNGGA,B\r\n")
            now[0] += 1000
            publish("$GNGGA,Z", 0)
            self.assertFalse(await client._sende_gga(writer))
            now[0] += 1000
            publish("$GNGGA,C", 1)
            self.assertTrue(await client._sende_gga(writer))
            self.assertEqual(data[-1], b"$GNGGA,C\r\n")
            self.assertEqual(app.stats["ntrip_gga_sent"], 3)
            self.assertIsNotNone(app.last_fix)

    async def test_summary_does_not_publish_payload_or_share_mutable_state(self):
        client = net.NtripClient(gnss.UartTx(FakeUART()))
        original = client.flow_summary()
        original["rx_bytes"] = 123
        self.assertEqual(client.flow_summary()["rx_bytes"], 0)
        self.assertNotIn("host", original)
        self.assertNotIn("mount", original)
        self.assertNotIn("sentence", original)

    async def test_idle_timeout_uses_elapsed_time_despite_rtc_jumps_and_tick_wrap(self):
        for wall_jump in (-3600000, 0, 3600000):
            with self.subTest(wall_jump=wall_jump):
                _, _, _, flow = await self.run_caster(require_gga=False,
                    chunks=[(5000, asyncio.TimeoutError())] * 8,
                    idle_timeout=15, clock_start=(1 << 30) - 6000,
                    wall_jump_per_read=wall_jump)
                self.assertEqual(flow['read_timeouts'], 3)
                self.assertEqual(flow['rx_chunks'], 0)

    async def test_gga_interval_ignores_a_26_year_rtc_jump(self):
        for jump in (0, JUMP_26Y_MS):
            with self.subTest(jump=jump):
                _, _, _, flow = await self.run_caster(require_gga=False,
                    chunks=[(5000, asyncio.TimeoutError())] * 8, wall_jump_per_read=jump)
                self.assertEqual(flow["gga_writes"], 5)
                self.assertEqual(flow["last_gga_ticks_ms"], 140000)

    async def test_received_bytes_restart_the_idle_deadline(self):
        delivered, _, _, flow = await self.run_caster(require_gga=False,
            chunks=[(5000, asyncio.TimeoutError()), (5000, b'corrections'),
                    (5000, asyncio.TimeoutError()), (5000, asyncio.TimeoutError()),
                    (5000, asyncio.TimeoutError()), (5000, b'too-late')],
            idle_timeout=15)
        self.assertEqual(delivered, [b'corrections'])
        self.assertEqual(flow['read_timeouts'], 4)

    def substate_events(self):
        return [(data["previous"], data["state"], data["epoch_age_sec"])
                for kind, data, _ in self.events if kind == "ntrip_substate"]

    def end_events(self):
        return [(data, snapshot) for kind, data, snapshot in self.events if kind == "ntrip_end"]

    def reload_end_snapshots(self):
        return [snapshot for data, snapshot in self.end_events() if data["reason"] == "reload"]

    async def test_fresh_epochs_never_raise_the_stale_substate(self):
        _, _, stats, _ = await self.run_caster(require_gga=False, chunks=[
            (10, msm_epoch(1075, 78_000)), (1000, msm_epoch(1075, 79_000))])
        self.assertEqual(self.substate_events(), [])
        self.assertEqual(self.snapshots, [None, None, None])
        self.assertIsNone(stats["ntrip_substate"])

    async def test_old_epochs_flag_stale_once_while_bytes_keep_arriving(self):
        _, _, stats, _ = await self.run_caster(require_gga=False, chunks=[
            (10, msm_epoch(1075, 18_000)), (1000, b"\x00"), (1000, b"\x00")])
        self.assertEqual(self.substate_events(), [(None, "stale", 60.0)])
        self.assertEqual(self.snapshots, [None, "stale", "stale", "stale"])
        self.assertNotEqual(stats["ntrip_state"], "stale")         # the published states stay seven
        self.assertIsNone(stats["ntrip_substate"])                 # cleared when the connection ends
        self.assertEqual(self.client.rtcm.counts, {1075: 1})       # counts survive the end

    async def test_stale_substate_samples_the_epoch_age_once(self):
        client = net.NtripClient(gnss.UartTx(FakeUART()))
        with mock.patch.object(net.time, "ticks_ms", return_value=500_000), \
                mock.patch.object(net.time, "gmtime", return_value=SUNDAY_0001):
            client.rtcm.feed(msm_epoch(1075, 18_000))
            self._sample_once(client)

    def _sample_once(self, client):
        with mock.patch.object(client.rtcm, "epoch_age_ms", wraps=client.rtcm.epoch_age_ms) as age, \
                mock.patch.object(client.rtcm, "epoch_stale", wraps=client.rtcm.epoch_stale) as stale:
            client.machine.update_stale(500_000)
        self.assertEqual(age.call_count, 1)
        self.assertEqual(stale.call_count, 0)
        self.assertEqual(net.app.stats["ntrip_substate"], "stale")
        net.app.stats["ntrip_substate"] = None

    async def test_a_silent_stream_keeps_ageing_through_read_timeouts(self):
        await self.run_caster(require_gga=False, chunks=[
            (0, msm_epoch(1075, 68_000)),                  # 10 s old
            (5000, asyncio.TimeoutError()),                # 15 s: not yet stale
            (5000, asyncio.TimeoutError()),                # 20 s: stale without any new byte
            (0, b"")])
        self.assertEqual(self.substate_events(), [(None, "stale", 20.0)])
        self.assertEqual(self.snapshots, [None, None, None, "stale"])

    async def test_fresh_epoch_after_a_stale_one_clears_the_substate(self):
        await self.run_caster(require_gga=False, chunks=[
            (0, msm_epoch(1075, 18_000)), (1000, msm_epoch(1075, 79_000))])
        self.assertEqual([(a, b) for a, b, _ in self.substate_events()],
                         [(None, "stale"), ("stale", None)])

    async def test_stale_backlog_reconnects_when_configured(self):
        _, _, stats, _ = await self.run_caster(
            require_gga=False, extra_config={"ntrip_reconnect_on_stale_sec": 1},
            chunks=[(10, msm_epoch(1075, 18_000)), (900, b"\x00"),
                    (200, b"\x00"), (0, b"never read")])
        ends = self.end_events()
        self.assertEqual(len(ends), 1)
        data, snapshot = ends[0]
        self.assertEqual(data["reason"], "stale_backlog")
        self.assertEqual(data["epoch_age_sec"], 61.1)
        self.assertEqual(snapshot["ntrip_retries"], 1)
        self.assertEqual(snapshot["last_ntrip_error_code"], "stale_backlog")
        self.assertEqual(stats["ntrip_retries"], 1)
        self.assertEqual(stats["last_ntrip_error_code"], "stale_backlog")
        self.assertEqual(stats["ntrip_state"], "backoff")
        self.assertEqual(self.snapshots, [None, "stale", "stale"])     # the third read never happens
        backoff = [d for kind, d, _ in self.events if kind == "ntrip_backoff"]
        self.assertEqual(backoff, [{"delay_sec": net.backoff_delay(
            0, cfg.CONFIG["ntrip_retry_delay_sec"], cfg.CONFIG.get("ntrip_retry_max_sec", 300))}])

    async def test_stale_reconnect_is_judged_on_the_ticks_timer_during_timeouts(self):
        _, _, stats, _ = await self.run_caster(
            require_gga=False, extra_config={"ntrip_reconnect_on_stale_sec": 8},
            chunks=[(0, msm_epoch(1075, 18_000)),
                    (5000, asyncio.TimeoutError()), (5000, asyncio.TimeoutError()),
                    (0, b"never read")])
        self.assertEqual([d["reason"] for d, _ in self.end_events()], ["stale_backlog"])
        self.assertEqual(stats["ntrip_retries"], 1)

    async def test_default_never_reconnects_because_of_staleness(self):
        self.assertEqual(cfg.CONFIG["ntrip_reconnect_on_stale_sec"], 0)
        _, _, stats, _ = await self.run_caster(require_gga=False, chunks=[
            (0, msm_epoch(1075, 18_000)), (30_000, b"\x00"), (30_000, b"\x00")])
        self.assertEqual([d["reason"] for d, _ in self.end_events()], ["eof"])
        self.assertEqual(stats["ntrip_retries"], 0)
        self.assertIsNone(stats["last_ntrip_error_code"])
        self.assertEqual(self.snapshots[-1], "stale")

    async def test_idle_timeout_still_ends_a_silent_stale_connection_first(self):
        await self.run_caster(
            require_gga=False, idle_timeout=10,
            extra_config={"ntrip_reconnect_on_stale_sec": 600},
            chunks=[(0, msm_epoch(1075, 18_000)),
                    (5000, asyncio.TimeoutError()), (5000, asyncio.TimeoutError())])
        self.assertEqual([d["reason"] for d, _ in self.end_events()], ["idle_timeout"])

    async def test_invalid_device_clock_changes_nothing(self):
        _, _, stats, _ = await self.run_caster(
            require_gga=False, gmtime=INVALID_CLOCK,
            extra_config={"ntrip_reconnect_on_stale_sec": 1},
            chunks=[(0, msm_epoch(1075, 18_000)), (5000, b"\x00"), (5000, b"\x00")])
        self.assertEqual(self.substate_events(), [])
        self.assertEqual(self.snapshots, [None, None, None, None])
        self.assertEqual([d["reason"] for d, _ in self.end_events()], ["eof"])
        self.assertEqual(stats["ntrip_retries"], 0)

    async def test_handshake_resets_the_stream_state_but_not_the_statistics(self):
        _, _, _, _ = await self.run_caster(require_gga=False, chunks=[
            (0, msm_epoch(1075, 18_000))])
        self.assertEqual(self.resets, [{}])                 # once, right after the handshake
        self.assertEqual(self.client.rtcm.counts, {1075: 1})
        self.assertIsNotNone(self.client.rtcm.last_msg_ms)
        self.assertEqual(self.snapshots[0], None)

    def errors(self):
        return [d for k, d, _ in self.events if k == "ntrip_error"]

    def backoff_delays(self):
        return [d["delay_sec"] for k, d, _ in self.events if k == "ntrip_backoff"]

    def backoff_snapshots(self):
        return [snapshot for k, _, snapshot in self.events if k == "ntrip_backoff"]

    def hosts(self):
        return [host for host, _ in self.resolved]

    def first(self):
        return [d for k, d, _ in self.events if k == "ntrip_first_data"]

    async def test_name_resolution_and_tcp_connect_are_timed_separately(self):
        _, _, _, flow = await self.run_caster(require_gga=False, chunks=[(0, b"abc")],
                                              dns_delay=40, connect_delay=250)
        self.assertEqual((flow["dns_ms"], flow["dns_max_ms"], flow["connect_ms"], flow["connect_max_ms"]),
                         (40, 40, 250, 250))
        self.assertEqual(self.resolved[0][0], "caster.invalid")
        self.assertEqual(self.connected[0], ("192.0.2.10", self.resolved[0][1]))
        self.assertEqual({k: self.first()[0][k] for k in ("attempt", "dns_ms", "connect_ms", "attempt_age_ms")},
                         {"attempt": 1, "dns_ms": 40, "connect_ms": 250, "attempt_age_ms": 290})

    async def test_unknown_address_format_publishes_the_whole_attempt_as_connect(self):
        _, _, _, flow = await self.run_caster(require_gga=False, chunks=[(0, b"abc")],
            addrinfo=[(2, 1, 0, "", b"\x00\x02")], dns_delay=30, connect_delay=70)
        self.assertEqual(self.connected[0][0], "caster.invalid")
        self.assertIsNone(flow["dns_ms"])
        self.assertEqual((flow["dns_max_ms"], flow["dns_unseparated"], flow["connect_ms"]), (0, 1, 100))
        self.assertEqual((self.first()[0]["dns_ms"], self.first()[0]["connect_ms"]), (None, 100))

    async def test_failed_resolution_is_timed_on_the_existing_error_event(self):
        _, _, stats, flow = await self.run_caster(require_gga=False, resolve_error=OSError(-202),
                                                  dns_delay=3000)
        self.assertEqual((flow["dns_ms"], flow["connect_ms"]), (3000, 0))
        self.assertEqual((self.connected, stats["ntrip_retries"]), ([], 1))
        self.assertEqual(stats["last_ntrip_error_code"], "network_error")
        e = self.errors()[0]
        self.assertEqual((e["error_type"], e["errno"]), ("OSError", -202))
        self.assertEqual((e["attempt"], e["dns_ms"], e["connect_ms"], e["attempt_age_ms"]),
                         (1, 3000, None, 3000))

    async def test_empty_resolution_is_an_os_error(self):
        await self.run_caster(require_gga=False, addrinfo=[])
        self.assertEqual((self.resolved[0][0], self.connected), ("caster.invalid", []))
        self.assertEqual(self.errors()[0]["error_type"], "OSError")

    async def test_connect_failure_is_timed_on_the_error_event(self):
        _, _, _, flow = await self.run_caster(require_gga=False, connect_error=OSError(113),
                                              connect_delay=4000)
        self.assertEqual((flow["connect_ms"], flow["connect_max_ms"]), (4000, 4000))
        e = self.errors()[0]
        self.assertEqual((e["errno"], e["dns_ms"], e["connect_ms"], e["attempt_age_ms"]),
                         (113, 0, 4000, 4000))

    async def test_resolution_counts_against_the_connect_deadline(self):
        # each part 0.2 s, together 0.4 s > deadline 0.3 s -> timeout exactly as before
        await self.run_caster(require_gga=False, dns_sleep=0.2, connect_sleep=0.2,
                              extra_config={"ntrip_connect_timeout_sec": 0.3})
        self.assertEqual(self.errors()[0]["error_type"], "TimeoutError")
        self.assertIsNotNone(self.errors()[0]["connect_ms"])

    async def test_parts_within_the_deadline_still_connect(self):
        await self.run_caster(require_gga=False, chunks=[(0, b"abc")], dns_sleep=0.05,
                              connect_sleep=0.05, extra_config={"ntrip_connect_timeout_sec": 0.5})
        self.assertEqual(self.errors(), [])
        self.assertEqual(len(self.first()), 1)

    async def test_error_before_the_connect_carries_no_times_of_the_previous_attempt(self):
        original = net.NtripClient._flow_phase
        connecting = [0]

        def flow_phase(client, phase):
            if phase == "connecting":
                connecting[0] += 1
                if connecting[0] == 2:
                    raise RuntimeError("synthetic failure before the connect")
            original(client, phase)

        with mock.patch.object(net.NtripClient, "_flow_phase", flow_phase):
            await self.run_caster(require_gga=False, connect_error=OSError(113), connect_delay=4000,
                                  dns_delay=40, backoffs=2)
        first, second = self.errors()
        self.assertEqual((first["attempt"], first["dns_ms"], first["connect_ms"], first["attempt_age_ms"]),
                         (1, 40, 4000, 4040))
        self.assertEqual((second["error_type"], second["attempt"], second["dns_ms"], second["connect_ms"],
                          second["attempt_age_ms"]), ("RuntimeError", 2, None, None, None))
        self.assertEqual(len(self.connected), 1)

    async def test_failed_attempts_emit_as_many_events_as_before(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        recorder = FieldRecorder(tmp.name + "/diag", segment_bytes=65536, max_segments=3)
        recorder.start(); base = len(recorder.events)
        with mock.patch.object(net, "field_recorder", recorder), \
                mock.patch.dict(state._instances, {"field_diagnostics": recorder}):
            # cap the now growing backoff so five attempts still fit five one-slice sleeps
            await self.run_caster(require_gga=False, connect_error=OSError(113), backoffs=5,
                                  record_events=False, extra_config={"ntrip_retry_max_sec": 5})
        kinds = [e["kind"] for e in recorder.events[base:]]
        self.assertEqual(len(kinds), EXPECTED_EVENTS_FOR_FIVE_FAILED_ATTEMPTS)
        self.assertEqual(recorder.dropped_events, 0)
        self.assertEqual([e["data"]["attempt"] for e in recorder.events if e["kind"] == "ntrip_error"],
                         [1, 2, 3, 4, 5])

    async def test_backoff_grows_over_mixed_failures(self):
        await self.run_caster(require_gga=False, idle_timeout=10, attempts=[
            {"connect_error": asyncio.TimeoutError()}, SCRIPT_500, IDLE_WITHOUT_DATA,
            {"connect_error": OSError(113)}], stop_when=lambda: len(self.backoff_delays()) == 4)
        self.assertEqual(self.backoff_delays(), [5, 10, 20, 40])
        self.assertEqual([s["ntrip_retries"] for s in self.backoff_snapshots()], [1, 2, 3, 4])
        self.assertEqual([s["last_ntrip_error_code"] for s in self.backoff_snapshots()],
                         ["timeout", "caster_error", "idle_timeout", "network_error"])

    async def test_two_endpoints_alternate_with_the_same_per_attempt_backoff(self):
        await self.run_caster(require_gga=False, extra_config=FALLBACK,
            attempts=[{"connect_error": OSError(113)}] * 7,
            stop_when=lambda: len(self.backoff_delays()) == 7)
        self.assertEqual(self.backoff_delays(), [5, 10, 20, 40, 80, 160, 300])
        self.assertEqual(self.hosts(), ["caster.invalid", "fallback.invalid"] * 3 + ["caster.invalid"])

    async def test_each_handshake_failure_counts_exactly_once(self):
        cases = {"header timeout before 200": ({"headers": [asyncio.TimeoutError()]}, "timeout"),
                 "header timeout after 200": ({"headers": [b"ICY 200 OK\r\n", asyncio.TimeoutError()]},
                                              "timeout"),
                 "request write failure": ({"request_error": OSError(104)}, "network_error")}
        for name, (attempt, code) in cases.items():
            with self.subTest(name):
                await self.run_caster(require_gga=False, attempts=[attempt],
                                      stop_when=lambda: len(self.backoff_delays()) == 1)
                self.assertEqual([(s["ntrip_retries"], s["last_ntrip_error_code"])
                                  for s in self.backoff_snapshots()], [(1, code)])
                self.assertEqual(len(self.errors()), 1)
                self.assertEqual(self.backoff_delays(), [5])

    async def test_shutdown_during_a_failing_attempt_neither_counts_nor_backs_off(self):
        _, _, stats, _ = await self.run_caster(require_gga=False,
            attempts=[{"connect_error": OSError(113), "stop_first": True}])
        self.assertEqual(self.backoff_delays(), [])
        self.assertEqual((stats["ntrip_retries"], stats.get("last_ntrip_error_code")), (0, None))
        self.assertEqual(len(self.errors()), 1)

    async def test_cancellation_during_a_stream_neither_counts_nor_backs_off(self):
        # CancelledError is a BaseException: the except clause misses it and outcome stays empty.
        with self.assertRaises(asyncio.CancelledError):
            await self.run_caster(require_gga=False, attempts=[
                {"connect_error": OSError(113)},
                {"chunks": [(0, b"abc"), (0, asyncio.CancelledError())]}],
                stop_when=lambda: False)
        self.assertEqual(self.backoff_delays(), [5])
        self.assertEqual(self.final_stats["ntrip_retries"], 1)
        self.assertEqual(self.states, ["waiting_wifi", "connecting", "backoff",
                                       "waiting_wifi", "connecting", "streaming"])

    async def test_every_error_path_records_a_code_and_never_an_unknown_state(self):
        script = [{"connect_error": asyncio.TimeoutError()}, {"connect_error": OSError(113)},
                  SCRIPT_500, {"headers": [b"ICY 401 Unauthorized\r\n"]},
                  {"headers": [b"HTTP/1.1 404 Not Found\r\n"]}, {"headers": [b"SOURCETABLE 200 OK\r\n"]},
                  {"headers": [b"garbage\r\n"]}, IDLE_WITHOUT_DATA, {"chunks": [(0, b"abc")]},
                  {"connect_error": RuntimeError("synthetic")}]
        _, _, stats, _ = await self.run_caster(require_gga=False, idle_timeout=10, attempts=script,
            supervised=True, stop_when=lambda: len(self.backoff_delays()) == len(script))
        self.assertEqual([s["last_ntrip_error_code"] for s in self.backoff_snapshots()],
            ["timeout", "network_error", "caster_error", "auth_error", "mount_error", "mount_error",
             "protocol_error", "idle_timeout", "eof", "unexpected_error"])
        self.assertEqual([s["ntrip_retries"] for s in self.backoff_snapshots()], list(range(1, 11)))
        self.assertTrue(set(self.states) <= set(state.NTRIP_STATES))
        self.assertNotIn("ValueError", [e["error_type"] for e in self.errors()])
        self.assertEqual(stats["task_restarts"], 0)

    async def test_a_flapping_caster_backs_off_like_a_failure(self):
        await self.run_caster(require_gga=False, attempts=[{"chunks": [(0, b"abc")]}] * 3,
                              stop_when=lambda: len(self.backoff_delays()) == 3)
        self.assertEqual(self.backoff_delays(), [5, 10, 20])
        self.assertEqual([d["reason"] for d, _ in self.end_events()], ["eof"] * 3)

    async def test_sixty_seconds_of_data_make_a_stream_stable(self):
        for clock_start in (100000, (1 << 30) - 30000):          # also across the tick wrap
            for last_gap, expected in ((60000, [5, 5]), (59999, [5, 10])):
                with self.subTest(clock_start=clock_start, last_gap=last_gap):
                    _, _, stats, _ = await self.run_caster(require_gga=False, clock_start=clock_start,
                        attempts=[{"connect_error": OSError(113)},
                                  {"chunks": [(0, b"a"), (last_gap, b"b")]}],
                        stop_when=lambda: len(self.backoff_delays()) == 2)
                    self.assertEqual(self.backoff_delays(), expected)
                    self.assertEqual(stats["ntrip_retries"], 1 if last_gap == 60000 else 2)
                    self.assertEqual(stats["last_ntrip_error_code"], "eof")

    async def test_stable_stream_ending_with_an_error_does_not_count(self):
        _, _, stats, _ = await self.run_caster(require_gga=False, attempts=[
            {"connect_error": OSError(113)},
            {"chunks": [(0, b"a"), (60000, b"b"), (0, OSError(104))]}],
            stop_when=lambda: len(self.backoff_delays()) == 2)
        self.assertEqual(self.backoff_delays(), [5, 5])
        self.assertEqual((stats["ntrip_retries"], stats["last_ntrip_error_code"]), (1, "network_error"))

    async def test_stale_backlog_reconnect_resets_earlier_failures(self):
        stale = {"chunks": [(10, msm_epoch(1075, 18_000)), (900, b"\x00"), (200, b"\x00"),
                            (0, b"never read")]}
        await self.run_caster(require_gga=False, extra_config={"ntrip_reconnect_on_stale_sec": 1},
            attempts=[{"connect_error": OSError(113)}] * 2 + [stale, {"connect_error": OSError(113)}],
            stop_when=lambda: len(self.backoff_delays()) == 4)
        self.assertEqual(self.backoff_delays(), [5, 10, 5, 5])
        self.assertEqual([s["ntrip_retries"] for s in self.backoff_snapshots()], [1, 2, 3, 4])

    async def test_endpoint_follows_the_success_rule(self):
        stable = {"chunks": [(0, b"a"), (60000, b"b")]}
        stale = {"chunks": [(10, msm_epoch(1075, 18_000)), (900, b"\x00"), (200, b"\x00"), (0, b"x")]}
        cases = {"idle without data": (IDLE_WITHOUT_DATA, "fallback.invalid"),
                 "eof after a few bytes": ({"chunks": [(0, b"abc")]}, "fallback.invalid"),
                 "header timeout after 200": ({"headers": [b"ICY 200 OK\r\n", asyncio.TimeoutError()]},
                                              "fallback.invalid"),
                 "stable stream (failback)": (stable, "caster.invalid"),
                 "stale backlog": (stale, "caster.invalid")}
        for name, (primary, second_host) in cases.items():
            with self.subTest(name):
                await self.run_caster(require_gga=False, idle_timeout=10,
                    extra_config={**FALLBACK, "ntrip_reconnect_on_stale_sec": 1},
                    attempts=[primary, {"connect_error": OSError(113)}],
                    stop_when=lambda: len(self.backoff_delays()) == 2)
                self.assertEqual(self.hosts(), ["caster.invalid", second_host])

    async def test_retry_limit_parks_safely_over_several_intervals(self):
        cases = {"network": ({"ntrip_max_retries": 2}, [{"connect_error": OSError(113)}] * 2, [5, 10],
                             "backoff"),
                 "auth": ({"ntrip_max_retries": 2}, [{"headers": [b"ICY 401 Unauthorized\r\n"]}] * 2,
                          [300, 300], "auth_error")}
        for name, (config, script, delays, parked_state) in cases.items():
            with self.subTest(name):
                parked = []          # (connections, ntrip beats) at every parked sleep

                def hook():
                    if net.app.stats.get("last_ntrip_error_code") == "retries_exhausted":
                        parked.append((len(self.resolved), len(self.beats)))
                    return len(parked) == 3
                _, _, stats, _ = await self.run_caster(require_gga=False, extra_config=config,
                    attempts=script, supervised=True, stop_when=hook)
                self.assertEqual([c for c, _ in parked], [2, 2, 2])        # no new attempt while parked
                self.assertTrue(parked[0][1] < parked[1][1] < parked[2][1]) # heartbeats keep coming
                self.assertEqual(self.backoff_delays(), delays)             # no backoff while parked
                self.assertEqual((stats["ntrip_state"], stats["ntrip_backoff_sec"]), (parked_state, 0))
                self.assertEqual(self.states[-2:], ["waiting_wifi", parked_state])
                self.assertEqual(stats["task_restarts"], 0)

    async def test_permanent_error_keeps_its_state_while_parked(self):
        cases = {"401": ([b"ICY 401 Unauthorized\r\n"], "auth_error"),
                 "404": ([b"HTTP/1.1 404 Not Found\r\n"], "mount_error"),
                 "sourcetable": ([b"SOURCETABLE 200 OK\r\n"], "mount_error")}
        for name, (headers, expected) in cases.items():
            with self.subTest(name):
                parked = [0]

                def hook():
                    if net.app.stats.get("last_ntrip_error_code") == "retries_exhausted":
                        parked[0] += 1
                    return parked[0] == 2
                _, _, stats, _ = await self.run_caster(require_gga=False,
                    extra_config={"ntrip_max_retries": 2}, attempts=[{"headers": headers}] * 2,
                    supervised=True, stop_when=hook)
                self.assertEqual(len(self.resolved), 2)                    # parked, no third attempt
                self.assertEqual((stats["ntrip_state"], stats["last_ntrip_error_code"]),
                                 (expected, "retries_exhausted"))
                self.assertEqual(stats["ntrip_backoff_sec"], 0)

    async def test_wifi_loss_releases_a_parked_client_with_a_fresh_count(self):
        parked = [0]

        def hook():
            if net.app.stats.get("last_ntrip_error_code") == "retries_exhausted":
                parked[0] += 1
                if parked[0] == 2:                    # two parked intervals first, then the loss
                    self.wifi.clear()
                    asyncio.get_running_loop().call_later(0.01, self.wifi.set)
            return len(self.backoff_delays()) == 3

        _, _, stats, _ = await self.run_caster(require_gga=False, extra_config={"ntrip_max_retries": 2},
            attempts=[{"connect_error": OSError(113)}] * 3, supervised=True, stop_when=hook)
        self.assertEqual(parked[0], 2)
        self.assertEqual(len(self.resolved), 3)
        self.assertEqual(self.backoff_delays(), [5, 10, 5])
        self.assertEqual(self.states, ["waiting_wifi", "connecting", "backoff"] * 2
                         + ["waiting_wifi", "backoff"]                       # parked
                         + ["waiting_wifi", "connecting", "backoff"])       # released
        self.assertEqual(stats["task_restarts"], 0)
        # The release clears the parked reason: no "retries exhausted" next to connecting.
        connecting = [code for value, code in self.state_codes if value == "connecting"]
        self.assertEqual(connecting, [None, "network_error", None])

    async def test_retry_limit_tries_the_fallback_first(self):
        await self.run_caster(require_gga=False, idle_timeout=10,
            extra_config={**FALLBACK, "ntrip_max_retries": 1},
            attempts=[IDLE_WITHOUT_DATA, {"connect_error": OSError(113)}], supervised=True,
            stop_when=lambda: net.app.stats.get("last_ntrip_error_code") == "retries_exhausted")
        self.assertEqual(self.hosts(), ["caster.invalid", "fallback.invalid"])
        self.assertEqual(self.backoff_delays(), [5, 10])

    async def test_permanent_error_waits_the_cap_only_when_it_ends_the_round(self):
        unauthorized = {"headers": [b"ICY 401 Unauthorized\r\n"]}
        not_found = {"headers": [b"HTTP/1.1 404 Not Found\r\n"]}
        sourcetable = {"headers": [b"SOURCETABLE 200 OK\r\n"]}
        cases = {"P=401, F=500": ([unauthorized, SCRIPT_500], [5, 10]),
                 "P=500, F=401 twice": ([SCRIPT_500, unauthorized] * 2, [5, 300, 20, 300]),
                 "P=500, F=404": ([SCRIPT_500, not_found], [5, 300]),
                 "P=500, F=SOURCETABLE": ([SCRIPT_500, sourcetable], [5, 300]),
                 "P=404, F=500": ([not_found, SCRIPT_500], [5, 10])}
        for name, (script, delays) in cases.items():
            with self.subTest(name):
                await self.run_caster(require_gga=False, extra_config=FALLBACK, attempts=script,
                                      stop_when=lambda: len(self.backoff_delays()) == len(script))
                self.assertEqual(self.hosts(), ["caster.invalid", "fallback.invalid"] * (len(script) // 2))
                self.assertEqual(self.backoff_delays(), delays)

    async def test_success_on_the_fallback_returns_to_the_primary(self):
        stable = {"chunks": [(0, b"a"), (60000, b"b")]}
        stale = {"chunks": [(10, msm_epoch(1075, 18_000)), (900, b"\x00"), (200, b"\x00"), (0, b"x")]}
        for name, fallback in (("stable stream", stable), ("stale backlog", stale)):
            with self.subTest(name):
                await self.run_caster(require_gga=False,
                    extra_config={**FALLBACK, "ntrip_reconnect_on_stale_sec": 1},
                    attempts=[{"connect_error": OSError(113)}, fallback, {"connect_error": OSError(113)}],
                    stop_when=lambda: len(self.backoff_delays()) == 3)
                self.assertEqual(self.hosts(), ["caster.invalid", "fallback.invalid", "caster.invalid"])
                self.assertEqual(self.backoff_delays(), [5, 5, 5])

    async def test_status_publishes_receive_timing_from_the_active_client(self):
        client = net.NtripClient(gnss.UartTx(FakeUART()))
        with mock.patch.dict(web._instances, {"ntrip": client}, clear=True), \
                mock.patch.object(web, "_read_allowed", return_value=True), \
                mock.patch.object(web, "board_metrics", return_value={}), \
                mock.patch.object(web, "_send_json") as send:
            await web._handle_status_request(None, "/status", "", False, {})
        report = send.call_args.args[1]["ntrip_flow"]
        self.assertEqual(report["schema"], 1)
        self.assertEqual(report["rx_chunks"], 0)
        self.assertEqual((report["dns_max_ms"], report["connect_max_ms"], report["dns_unseparated"]),
                         (0, 0, 0))
        self.assertIsNone(report["rx_age_ms"])
        self.assertGreaterEqual(report["phase_age_ms"], 0)


class TestNtripReload(unittest.IsolatedAsyncioTestCase):
    """A saved caster change takes effect in the running client (#23 part b)."""

    # The scripted caster of TestNtripFlow and its helpers, without running its tests twice.
    run_caster = TestNtripFlow.run_caster
    end_events = TestNtripFlow.end_events
    reload_end_snapshots = TestNtripFlow.reload_end_snapshots
    errors = TestNtripFlow.errors
    backoff_delays = TestNtripFlow.backoff_delays
    backoff_snapshots = TestNtripFlow.backoff_snapshots
    hosts = TestNtripFlow.hosts

    def reload_new(self):
        cfg.CONFIG.update(NEW)
        self.client.reload()

    def reload_on_read(self, number, change=None):
        def on_read(n):
            if n == number:
                (change or self.reload_new)()
        return on_read

    def info_lines(self, text):
        return [c.args[2] for c in self.net_log.call_args_list
                if c.args[:2] == ("INFO", "NTRIP") and text in c.args[2]]

    def reasons(self):
        return [d["reason"] for d, _ in self.end_events()]

    async def test_reload_switches_a_streaming_client_to_the_new_caster(self):
        def on_read(n):
            if n == 2:
                cfg.CONFIG.update(NEW); self.reload_at = self.clock[0]; self.client.reload()
        _, _, stats, _ = await self.run_caster(require_gga=False, on_read=on_read,
            attempts=[{"chunks": [(0, b"a"), (1000, b"b"), (1000, b"c")]},
                      {"chunks": [(0, b"d")]}],
            stop_when=lambda: len(self.connected) == 2 or len(self.backoff_delays()) == 1)
        self.assertEqual(self.hosts(), ["caster.invalid", "new.invalid"])
        self.assertLessEqual(self.connect_clocks[1] - self.reload_at, 1000)   # the read in progress
        ends = self.end_events()
        self.assertEqual([d["reason"] for d, _ in ends], ["reload", "eof"])
        # Checked at the transition: the reload neither counts nor backs off ...
        self.assertEqual([s["ntrip_retries"] for s in self.reload_end_snapshots()], [0])
        kinds = [k for k, _, _ in self.events]
        self.assertNotIn("ntrip_backoff", kinds[:kinds.index("ntrip_end")])
        self.assertNotIn("ntrip_error", kinds)
        # ... while the EOF of the new caster's stream counts as #16 requires.
        self.assertEqual((stats["ntrip_retries"], self.backoff_delays()), (1, [5]))
        self.assertEqual(stats["ntrip_config"], "ready")

    async def test_reload_wakes_a_switched_off_client(self):
        seen = []

        def hook():
            if not seen:                    # the first idle slice
                seen.append(net.app.stats.get("ntrip_config"))
                cfg.CONFIG["ntrip_enabled"] = True
                self.client.reload()
            return len(self.backoff_delays()) == 1
        _, _, stats, _ = await self.run_caster(require_gga=False,
            extra_config={"ntrip_enabled": False}, attempts=[{"chunks": [(0, b"a")]}],
            stop_when=hook)
        self.assertEqual(self.hosts(), ["caster.invalid"])
        self.assertLessEqual(self.connect_clocks[0] - 100000, 5000)
        self.assertEqual((seen, stats["ntrip_config"]), (["switched_off"], "ready"))
        self.assertEqual(self.states[:3], ["disabled", "waiting_wifi", "connecting"])

    async def test_reload_switches_a_streaming_client_off_and_on(self):
        idle = []       # (ntrip beats, ntrip_config, ntrip_endpoint, ntrip_substate) per idle slice

        def switch_off():
            cfg.CONFIG["ntrip_enabled"] = False
            self.client.reload()

        def hook():
            if net.app.stats.get("ntrip_state") == "disabled":
                idle.append((len(self.beats),) + tuple(net.app.stats.get(key) for key in
                            ("ntrip_config", "ntrip_endpoint", "ntrip_substate")))
                if len(idle) == 3:
                    cfg.CONFIG["ntrip_enabled"] = True
                    self.client.reload()
            return len(self.backoff_delays()) == 1
        _, _, stats, _ = await self.run_caster(require_gga=False, supervised=True,
            on_read=self.reload_on_read(2, switch_off), stop_when=hook,
            attempts=[{"chunks": [(0, msm_epoch(1075, 18_000)), (0, b"\x00")]},   # stale
                      {"chunks": [(0, b"c")]}])
        self.assertEqual(self.states, ["waiting_wifi", "connecting", "streaming", "disabled",
                                       "waiting_wifi", "connecting", "streaming", "backoff"])
        self.assertEqual([entry[1:] for entry in idle], [("switched_off", None, None)] * 3)
        self.assertTrue(idle[0][0] < idle[1][0] < idle[2][0])          # heartbeats keep coming
        self.assertEqual(self.hosts(), ["caster.invalid"] * 2)          # none while switched off
        self.assertEqual(self.reasons(), ["reload", "eof"])
        self.assertEqual((stats["task_restarts"], stats["ntrip_config"]), (0, "ready"))

    async def test_reload_cuts_a_backoff_short_with_a_fresh_round(self):
        reload_at = []

        def hook():
            if len(self.backoff_delays()) == 3 and not reload_at:     # first slice of the 20 s
                reload_at.append(self.clock[0])
                self.reload_new()
            return len(self.backoff_delays()) == 4
        await self.run_caster(require_gga=False, extra_config=FALLBACK,
            attempts=[{"connect_error": OSError(113)}] * 4, stop_when=hook)
        self.assertEqual(self.hosts(), ["caster.invalid", "fallback.invalid", "caster.invalid",
                                        "new.invalid"])
        self.assertEqual(self.connect_clocks[3], reload_at[0])          # no further slice
        self.assertEqual(self.backoff_delays(), [5, 10, 20, 5])
        error_snapshots = [s for k, _, s in self.events if k == "ntrip_error"]
        self.assertEqual(error_snapshots[3]["ntrip_backoff_sec"], 0)
        self.assertEqual(self.backoff_snapshots()[3]["ntrip_retries"], 4)   # cumulative

    async def test_reload_releases_a_parked_client_with_a_fresh_round(self):
        parked = []

        def hook():
            if net.app.stats.get("last_ntrip_error_code") == "retries_exhausted":
                parked.append(net.app.stats["ntrip_state"])
                if len(parked) == 2:
                    cfg.CONFIG["ntrip_pass"] = "new"
                    self.client.reload()
            return len(self.backoff_delays()) == 3
        unauthorized = {"headers": [b"ICY 401 Unauthorized\r\n"]}
        _, _, stats, _ = await self.run_caster(require_gga=False,
            extra_config={"ntrip_max_retries": 2}, supervised=True, stop_when=hook,
            attempts=[unauthorized, unauthorized, {"chunks": [(0, b"a")]}])
        self.assertEqual(parked, ["auth_error"] * 2)
        self.assertEqual(len(self.resolved), 3)
        connecting = [code for value, code in self.state_codes if value == "connecting"]
        self.assertEqual(connecting, [None, "auth_error", None])
        self.assertEqual(self.states[-4:], ["waiting_wifi", "connecting", "streaming", "backoff"])
        self.assertEqual(self.backoff_delays(), [300, 300, 5])
        self.assertEqual(stats["task_restarts"], 0)

    async def test_reload_keeps_a_stream_whose_endpoint_is_unchanged(self):
        def change_fallback():
            cfg.CONFIG.update(OTHER_FALLBACK)
            self.client.reload()
        delivered, _, _, flow = await self.run_caster(require_gga=False,
            on_read=self.reload_on_read(2, change_fallback),
            attempts=[{"chunks": [(0, b"a"), (1000, b"b"), (1000, b"c")]},
                      {"connect_error": OSError(113)}],
            stop_when=lambda: len(self.backoff_delays()) == 2)
        self.assertEqual(self.reasons(), ["eof"])
        self.assertEqual(flow["connections"], 1)
        self.assertEqual(delivered, [b"a", b"b", b"c"])
        self.assertEqual(len(self.info_lines("the active caster is unchanged")), 1)
        self.assertEqual(self.hosts(), ["caster.invalid", "other.invalid"])

    async def test_reload_while_waiting_for_wifi_uses_the_new_endpoints(self):
        loop = asyncio.get_running_loop()
        loop.call_later(0.01, self.reload_new)
        loop.call_later(0.03, lambda: self.wifi.set())
        await self.run_caster(require_gga=False, wifi_up=False,
            attempts=[{"chunks": [(0, b"a")]}], stop_when=lambda: len(self.backoff_delays()) == 1)
        self.assertEqual(self.hosts(), ["new.invalid"])
        self.assertEqual(self.reasons(), ["eof"])

    async def test_reload_during_the_connect_ends_the_attempt_when_it_returns(self):
        asyncio.get_running_loop().call_later(0.005, self.reload_new)
        _, writes, _, _ = await self.run_caster(require_gga=False, connect_sleep=0.02,
            attempts=[{"chunks": [(0, b"a")]}, {"stop_first": True}])
        self.assertEqual(len([w for w in writes if w.startswith(b"GET ")]), 1)   # no request first
        self.assertEqual(self.reasons(), ["reload"])
        self.assertEqual((self.errors(), self.backoff_delays()), ([], []))
        self.assertEqual(self.hosts(), ["caster.invalid", "new.invalid"])

    async def test_reload_between_headers_ends_the_attempt_uncounted(self):
        await self.run_caster(require_gga=False,
            attempts=[{"headers": [b"ICY 200 OK\r\n", self.reload_new, b"X: y\r\n", b"\r\n"]},
                      {"chunks": [(0, b"d")]}],
            stop_when=lambda: len(self.backoff_delays()) == 1)
        self.assertEqual(self.reasons(), ["reload", "eof"])
        self.assertEqual([s["ntrip_retries"] for s in self.reload_end_snapshots()], [0])
        self.assertEqual(self.states[:4], ["waiting_wifi", "connecting", "waiting_wifi", "connecting"])
        self.assertEqual(self.hosts(), ["caster.invalid", "new.invalid"])

    async def test_reload_before_a_status_line_timeout_is_not_counted(self):
        cases = {"status line timeout": [self.reload_new, asyncio.TimeoutError()],
                 "header line timeout": [b"ICY 200 OK\r\n", self.reload_new, asyncio.TimeoutError()],
                 "401 after the change": [self.reload_new, b"ICY 401 Unauthorized\r\n"]}
        for name, headers in cases.items():
            with self.subTest(name):
                await self.run_caster(require_gga=False,
                                      attempts=[{"headers": headers}, {"stop_first": True}])
                self.assertEqual(self.errors(), [])
                self.assertEqual(self.backoff_delays(), [])
                self.assertEqual([(s["ntrip_retries"], s["last_ntrip_error_code"])
                                  for s in self.reload_end_snapshots()], [(0, None)])
                self.assertEqual(self.reasons(), ["reload"])
                self.assertEqual(self.hosts(), ["caster.invalid", "new.invalid"])
                self.assertNotIn("auth_error", self.states)

    async def test_reload_before_a_read_timeout_wins_over_idle_and_stale(self):
        stale = [(0, msm_epoch(1075, 18_000)), (5000, asyncio.TimeoutError()),
                 (5000, asyncio.TimeoutError()), (0, b"never read")]
        # (read with the hook, idle timeout, extra config, chunks); every hooked read times out
        # where the idle limit, a due GGA or the stale limit would otherwise act.
        cases = {"idle limit": (2, 10, {}, IDLE_WITHOUT_DATA["chunks"]),
                 "GGA due": (2, 60, {}, IDLE_WITHOUT_DATA["chunks"]),
                 "stale limit": (3, 60, {"ntrip_reconnect_on_stale_sec": 8}, stale)}
        for name, (number, idle_timeout, extra, chunks) in cases.items():
            with self.subTest(name):
                hook_at = []

                def on_read(n, number=number):
                    if n == number:
                        hook_at.append(len(self.writes))
                        self.reload_new()
                _, writes, stats, flow = await self.run_caster(require_gga=False,
                    idle_timeout=idle_timeout, extra_config=extra, on_read=on_read,
                    attempts=[{"chunks": chunks}, {"stop_first": True}])
                self.assertEqual(self.reasons(), ["reload"])
                after = writes[hook_at[0]:]
                after = after[:[w[:4] for w in after].index(b"GET ")]   # up to the next request
                self.assertEqual(after, [])                              # no GGA after the hook
                self.assertEqual(flow["read_timeouts"], 2)               # the hooked one counts
                self.assertEqual([s["ntrip_retries"] for s in self.reload_end_snapshots()], [0])
                codes = [c for _, c in self.state_codes] + [
                    s["last_ntrip_error_code"] for _, _, s in self.events]
                self.assertNotIn("stale_backlog", codes + [stats["last_ntrip_error_code"]])
                self.assertEqual(stats["ntrip_retries"], 0)

    async def test_reload_during_a_gga_upload_ends_the_attempt_before_the_next_read(self):
        # (GGA call that brings the reload, chunks, reads, delivered); the GGA goes out at
        # the stream start and is due again 10 s later, in silence or with data.
        cases = {"stream start": (1, [(0, b"a")], 0, []),
                 "silence": (2, [(0, b"a"), (5000, asyncio.TimeoutError()),
                                 (5000, asyncio.TimeoutError()), (0, b"b")], 3, [b"a"]),
                 "data": (2, [(0, b"a"), (10000, b"b"), (0, b"c")], 2, [b"a", b"b"])}
        for name, (number, chunks, reads, expected) in cases.items():
            with self.subTest(name):
                calls = []

                def source(now_ms=None, number=number):
                    calls.append(now_ms)
                    if len(calls) == number:
                        self.reload_new()       # arrives while the sentence is sent
                    return "$GNGGA,synthetic"
                delivered, _, _, _ = await self.run_caster(require_gga=False, gga_source=source,
                    attempts=[{"chunks": chunks}, {"stop_first": True}])
                self.assertEqual(len(self.snapshots), reads)        # no read after the upload
                self.assertEqual(delivered, expected)
                self.assertEqual(self.reasons(), ["reload"])
                self.assertEqual(self.hosts(), ["caster.invalid", "new.invalid"])

    async def test_reload_before_eof_or_a_connection_error_is_not_counted(self):
        cases = {"eof": [(0, b"a")], "connection error": [(0, b"a"), (0, OSError(104))]}
        for name, chunks in cases.items():
            with self.subTest(name):
                _, _, stats, _ = await self.run_caster(require_gga=False,
                    on_read=self.reload_on_read(2),
                    attempts=[{"chunks": chunks}, {"stop_first": True}])
                self.assertEqual(self.reasons(), ["reload"])
                self.assertEqual(self.errors(), [])
                self.assertIsNone(stats.get("last_ntrip_error"))
                self.assertEqual(self.backoff_delays(), [])
                self.assertEqual([s["ntrip_retries"] for s in self.reload_end_snapshots()], [0])
                self.assertEqual(self.hosts(), ["caster.invalid", "new.invalid"])

    async def test_unchanged_endpoint_reload_keeps_normal_error_accounting(self):
        def change_fallback():
            cfg.CONFIG.update(OTHER_FALLBACK)
            self.client.reload()
        await self.run_caster(require_gga=False, idle_timeout=10,
            on_read=self.reload_on_read(2, change_fallback), attempts=[IDLE_WITHOUT_DATA],
            stop_when=lambda: len(self.backoff_delays()) == 1)
        self.assertEqual(self.reasons(), ["idle_timeout"])
        self.assertEqual([s["ntrip_retries"] for s in self.backoff_snapshots()], [1])
        self.assertEqual(self.backoff_delays(), [5])
        self.assertEqual(len(self.info_lines("the active caster is unchanged")), 1)

    async def test_reload_requests_coalesce(self):
        def twice():
            self.reload_new()
            self.client.reload()
        await self.run_caster(require_gga=False, on_read=self.reload_on_read(2, twice),
            attempts=[{"chunks": [(0, b"a"), (0, b"b")]}, {"stop_first": True}])
        self.assertEqual(len(self.info_lines("Configuration changed")), 1)
        self.assertEqual(self.hosts(), ["caster.invalid", "new.invalid"])
        self.assertEqual(self.reasons(), ["reload"])

    async def test_shutdown_wins_over_a_pending_reload(self):
        def reload_and_stop():
            self.reload_new()
            net.shutdown_event.set()
        _, _, stats, _ = await self.run_caster(require_gga=False,
            on_read=self.reload_on_read(2, reload_and_stop),
            attempts=[{"chunks": [(0, b"a"), (0, b"b"), (0, b"c")]}])
        self.assertEqual(self.hosts(), ["caster.invalid"])
        self.assertEqual(self.reload_end_snapshots(), [])
        self.assertEqual((stats["ntrip_retries"], self.backoff_delays()), (0, []))
        self.assertEqual(self.info_lines("Configuration changed"), [])
        self.assertTrue(self.client.machine.reload_requested)          # never evaluated

    async def test_shutdown_wins_over_a_reload_in_the_error_path(self):
        def reload_and_stop():
            self.reload_new()
            net.shutdown_event.set()
        # (script, ntrip_error events): an exception reaching the except clause is recorded
        # as on every shutdown, a read timeout never reaches it.
        cases = {"connection error": ({"chunks": [(0, b"a"), (0, OSError(104))]}, 1),
                 "header timeout": ({"headers": [b"ICY 200 OK\r\n", reload_and_stop,
                                                 asyncio.TimeoutError()]}, 1),
                 "read timeout": ({"chunks": [(0, b"a"), (5000, asyncio.TimeoutError())]}, 0)}
        for name, (script, errors) in cases.items():
            with self.subTest(name):
                _, _, stats, _ = await self.run_caster(require_gga=False,
                    on_read=self.reload_on_read(2, reload_and_stop), attempts=[script])
                self.assertEqual(self.hosts(), ["caster.invalid"])
                self.assertEqual(self.reload_end_snapshots(), [])
                self.assertEqual(len(self.errors()), errors)
                self.assertEqual((stats["ntrip_retries"], self.backoff_delays()), (0, []))
                self.assertIsNone(stats.get("last_ntrip_error_code"))
                self.assertEqual(self.info_lines("Configuration changed"), [])
                self.assertTrue(self.client.machine.reload_requested)

    async def test_reload_mid_frame_resets_the_cursor_for_the_next_stream(self):
        def on_read(n):
            if n == 2:          # this read still returns b"x"; the reload ends it unwritten
                self.reload_new()
                self.tx.submit(CMD)
        delivered, _, _, flow = await self.run_caster(require_gga=False, on_read=on_read,
            attempts=[{"chunks": [(0, A[:10]), (1000, b"x")]}, {"chunks": [(0, B)]}],
            stop_when=lambda: len(self.backoff_delays()) == 1)
        self.assertEqual(self.reasons(), ["reload", "eof"])
        self.assertEqual(b"".join(delivered), A[:10] + WIRE_CMD + B)
        self.assertEqual((flow["uart_commands_deferred"], flow["uart_commands_dropped"]), (1, 0))
        self.assertEqual(self.hosts()[-1], "new.invalid")

    # A chunk that the UART takes only partly at the stale limit: the epoch is 60 s old,
    # the stale grace 1 s; the rest of B goes out with the 12th UART wait, 1020 ms after
    # the first stale check.
    STALE_AT_A_PARTIAL_WRITE = {"ntrip_reconnect_on_stale_sec": 1}

    async def test_reload_during_a_partial_write_ends_the_attempt_without_stale_accounting(self):
        def on_uart_sleep(n):
            if n == 1:
                self.reload_new()
        _, _, _, _ = await self.run_caster(require_gga=False,
            extra_config=self.STALE_AT_A_PARTIAL_WRITE,
            attempts=[{"chunks": [(10, msm_epoch(1075, 18_000)), (900, B)]}, {"chunks": []}],
            uart_accept=[None, 2] + [0] * 11, on_uart_sleep=on_uart_sleep,
            stop_when=lambda: len(self.backoff_delays()) == 1)
        self.assertEqual(self.reasons(), ["reload", "eof"])
        self.assertEqual(self.reload_end_snapshots()[0]["ntrip_retries"], 0)
        self.assertEqual(len(self.backoff_delays()), 1)         # from the second attempt's EOF
        self.assertEqual(self.hosts(), ["caster.invalid", "new.invalid"])

    async def test_reload_with_the_same_endpoint_during_a_partial_write_keeps_processing(self):
        def on_uart_sleep(n):
            if n == 1:
                self.client.reload()            # configuration unchanged
        _, _, _, _ = await self.run_caster(require_gga=False,
            extra_config=self.STALE_AT_A_PARTIAL_WRITE,
            attempts=[{"chunks": [(10, msm_epoch(1075, 18_000)), (900, B)]}, {"chunks": []}],
            uart_accept=[None, 2] + [0] * 11, on_uart_sleep=on_uart_sleep,
            stop_when=lambda: len(self.backoff_delays()) == 1)
        self.assertEqual(self.reasons()[0], "stale_backlog")    # as before #28b
        self.assertEqual(self.end_events()[0][1]["ntrip_retries"], 1)
        self.assertNotIn("reload", self.reasons())

    async def test_shutdown_during_a_partial_write_ends_without_a_reload_end(self):
        def stop():
            net.shutdown_event.set()

        def reload_and_stop():
            self.reload_new()
            net.shutdown_event.set()
        epoch = msm_epoch(1075, 18_000)
        for name, hook in (("shutdown", stop), ("shutdown plus reload", reload_and_stop)):
            with self.subTest(name):
                def on_uart_sleep(n, hook=hook):
                    if n == 1:
                        hook()
                delivered, _, stats, flow = await self.run_caster(require_gga=False,
                    extra_config=self.STALE_AT_A_PARTIAL_WRITE,
                    attempts=[{"chunks": [(10, epoch), (900, B), (0, C)]},
                              {"chunks": [(0, A)]}],
                    uart_accept=[None, 2] + [0] * 11, on_uart_sleep=on_uart_sleep)
                self.assertEqual(self.hosts(), ["caster.invalid"])
                # Without the shutdown check right after drain_rtcm, check_stale would
                # run 1020 ms after the first one, beyond the stale limit.
                self.assertEqual(self.reasons(), [])
                self.assertEqual(self.reload_end_snapshots(), [])
                self.assertEqual(self.errors(), [])
                self.assertEqual((stats["ntrip_retries"], self.backoff_delays()), (0, []))
                self.assertIsNone(stats.get("last_ntrip_error_code"))
                self.assertEqual(b"".join(delivered), epoch + B)
                self.assertEqual(flow["uart_bytes"], len(epoch) + len(B))
                self.assertFalse(self.tx.busy)
                self.assertTrue(self.tx.at_boundary())
                if hook is reload_and_stop:
                    self.assertTrue(self.client.machine.reload_requested)  # never evaluated
                    self.assertEqual(self.info_lines("Configuration changed"), [])

    async def test_a_writer_closed_by_a_reload_always_starts_a_fresh_round(self):
        def change_and_change_back():
            cfg.CONFIG.update(OTHER_FALLBACK)
            self.client.reload()                 # closes: the streaming fallback is gone
            cfg.CONFIG.update(FALLBACK)          # back before the read returns
            self.client.reload()
        _, _, stats, _ = await self.run_caster(require_gga=False, extra_config=FALLBACK,
            on_read=self.reload_on_read(2, change_and_change_back),
            attempts=[{"connect_error": OSError(113)}, {"chunks": [(0, b"a"), (0, b"b")]},
                      {"stop_first": True}], stop_when=lambda: False)
        self.assertEqual(self.hosts(), ["caster.invalid", "fallback.invalid", "caster.invalid"])
        self.assertEqual(self.backoff_delays(), [5])                    # none for the reload
        self.assertEqual([(s["ntrip_retries"], s["last_ntrip_error_code"])
                          for s in self.reload_end_snapshots()], [(1, None)])
        self.assertEqual((self.client.machine.index, self.client.machine.retry_count), (0, 0))
        self.assertEqual(stats["ntrip_retries"], 1)                     # cumulative, unchanged
        self.assertEqual(len(self.info_lines("reconnecting with the saved caster settings")), 1)

    def attempt_kinds(self):
        return [k for k, _, _ in self.events if k in ("ntrip_end", "ntrip_backoff", "ntrip_error")]

    def assert_closed_by_the_reload(self, phase):
        """The first connection ended through the close in reload(), uncounted, and the
        second one (to the new caster) streamed until its EOF."""
        self.assertEqual(self.unclosed_blocks, [])
        self.assertEqual(self.reasons(), ["reload", "eof"])
        self.assertEqual([s["ntrip_retries"] for s in self.reload_end_snapshots()], [0])
        self.assertEqual(self.attempt_kinds(), ["ntrip_end", "ntrip_end", "ntrip_backoff"])
        self.assertEqual(self.hosts(), ["caster.invalid", "new.invalid"])
        self.assertEqual(self.closes[0]["phase"], phase)     # not first in the finally

    async def test_reload_ends_a_blocked_request_drain_uncounted(self):
        for style in ("cpython", "micropython"):
            with self.subTest(style):
                await self.run_caster(require_gga=False, writer_style=style,
                    on_block=self.reload_new,
                    attempts=[{"block": "request_drain"}, {"chunks": [(0, b"d")]}])
                self.assert_closed_by_the_reload("connecting")

    async def test_reload_ends_a_blocked_status_line_readline(self):
        for end in ("oserror", "eof"):
            with self.subTest(end):
                _, _, stats, _ = await self.run_caster(require_gga=False,
                    on_block=self.reload_new,
                    attempts=[{"block": "status_line", "block_end": end},
                              {"chunks": [(0, b"d")]}])
                self.assert_closed_by_the_reload("headers")
                self.assertIsNone(stats.get("last_ntrip_error"))      # no HTTP Caster Error
                self.assertEqual(self.states[:4],
                                 ["waiting_wifi", "connecting", "waiting_wifi", "connecting"])

    async def test_reload_ends_a_blocked_read_in_the_stream(self):
        delivered, _, _, _ = await self.run_caster(require_gga=False, on_block=self.reload_new,
            attempts=[{"chunks": [(0, b"a")], "block": "read"}, {"chunks": [(0, b"d")]}])
        self.assert_closed_by_the_reload("read")
        self.assertEqual(delivered, [b"a", b"d"])

    async def test_reload_ends_a_blocked_gga_drain_quietly(self):
        # A position only for the first connection: the second one sends no GGA.
        source = lambda now_ms=None: "$GNGGA,synthetic" if len(self.connected) == 1 else None
        _, _, stats, flow = await self.run_caster(require_gga=False, gga_source=source,
            on_block=self.reload_new,
            attempts=[{"chunks": [(0, b"a")], "block": "gga_drain"}, {"stop_first": True}])
        self.assertEqual(self.unclosed_blocks, [])
        warnings = [c for c in self.net_log.call_args_list
                    if c.args[:2] == ("WARN", "NTRIP") and "GGA upload failed" in c.args[2]]
        self.assertEqual(warnings, [])
        self.assertEqual((flow["gga_failures"], stats["ntrip_gga_sent"]), (0, 0))
        self.assertEqual(self.reasons(), ["reload"])
        self.assertEqual(self.errors(), [])
        self.assertEqual(self.closes[0]["phase"], "gga")

    async def test_reload_with_an_unchanged_endpoint_never_closes(self):
        closed_by_reload = []

        def change_fallback():
            cfg.CONFIG.update(OTHER_FALLBACK)
            self.client.reload()
            closed_by_reload.append(self.client._reload_closed)
        delivered, _, _, _ = await self.run_caster(require_gga=False,
            on_read=self.reload_on_read(2, change_fallback),
            attempts=[{"chunks": [(0, b"a"), (1000, b"b"), (1000, b"c")]}])
        self.assertEqual(closed_by_reload, [False])
        self.assertEqual(self.closes, [{"calls": 1, "phase": "closing"}])    # the finally only
        self.assertEqual(delivered, [b"a", b"b", b"c"])
        self.assertEqual(self.reasons(), ["eof"])

    async def test_reload_during_connect_takes_effect_when_the_connect_returns(self):
        reload_at = []

        def reload_later():
            reload_at.append(time.monotonic())
            self.reload_new()
        asyncio.get_running_loop().call_later(0.05, reload_later)
        _, writes, _, _ = await self.run_caster(require_gga=False,
            attempts=[{"connect_sleep": 0.3}, {"stop_first": True}])
        # The connect is not cut short, and the next one follows without backoff or request.
        self.assertGreaterEqual(self.connect_times[1] - reload_at[0], 0.2)
        self.assertLess(self.connect_times[1] - reload_at[0], 0.5)
        self.assertEqual(len([w for w in writes if w.startswith(b"GET ")]), 1)   # the second
        self.assertEqual(self.reasons(), ["reload"])
        self.assertEqual([s["ntrip_retries"] for s in self.reload_end_snapshots()], [0])
        self.assertEqual(self.errors(), [])
        self.assertEqual(self.hosts(), ["caster.invalid", "new.invalid"])

    async def test_reload_during_a_connect_timeout_is_not_counted(self):
        asyncio.get_running_loop().call_later(0.05, self.reload_new)
        _, _, stats, _ = await self.run_caster(require_gga=False,
            extra_config={"ntrip_connect_timeout_sec": 0.2},
            attempts=[{"connect_sleep": 0.5}, {"connect_sleep": 0, "stop_first": True}])
        self.assertEqual(self.errors(), [])
        self.assertEqual([(s["ntrip_retries"], s["last_ntrip_error_code"])
                          for s in self.reload_end_snapshots()], [(0, None)])
        self.assertIsNone(stats.get("last_ntrip_error_code"))           # no "timeout"
        self.assertEqual(self.reasons(), ["reload"])
        self.assertEqual(self.hosts(), ["caster.invalid", "new.invalid"])

    async def test_saved_caster_change_reaches_the_running_client(self):
        # Host test (d) of the issue: a change saved through cfg reaches the client.
        directory = tempfile.TemporaryDirectory(prefix="esp-rtk-23b-")
        self.addCleanup(directory.cleanup)
        self.addCleanup(cfg.set_task_reloader, None)
        saved = []

        def on_read(n):
            if n == 1:
                cfg.CONFIG["config_file"] = directory.name + "/config.json"
                cfg.set_task_reloader(self.client.reload)
            if n == 2:
                self.reload_at = self.clock[0]
                saved.append(cfg.apply_live(NEW))
                saved.append(cfg.config_restart_required())
        with mock.patch.object(cfg, "log") as cfg_log:
            await self.run_caster(require_gga=False, on_read=on_read,
                attempts=[{"chunks": [(0, b"a"), (1000, b"b"), (1000, b"c")]},
                          {"chunks": [(0, b"d")]}],
                stop_when=lambda: len(self.connected) == 2 or len(self.backoff_delays()) == 1)
        self.assertEqual(saved, [True, False])
        self.assertEqual([c.args[0] for c in cfg_log.call_args_list], ["INFO"])    # the save
        self.assertEqual(self.hosts(), ["caster.invalid", "new.invalid"])
        self.assertLessEqual(self.connect_clocks[1] - self.reload_at, 1000)   # within 10 s
        self.assertEqual(self.reasons(), ["reload", "eof"])
        self.assertEqual(self.errors(), [])
        lines = [c.args[2] for c in self.net_log.call_args_list if c.args[:2] == ("INFO", "NTRIP")]
        changed = lines.index("Configuration changed; reconnecting with the saved caster settings.")
        self.assertIn("Connecting to configured primary caster...", lines[changed + 1:])

    def test_abort_io_survives_close_errors_and_closes_once(self):
        calls = {"close": 0, "s.close": 0}

        def failing(name):
            def close():
                calls[name] += 1
                raise OSError(9, "closed")
            return close
        client = net.NtripClient(gnss.UartTx(FakeUART()))
        client._attempt_writer = types.SimpleNamespace(
            close=failing("close"), s=types.SimpleNamespace(close=failing("s.close")))
        with mock.patch.dict(cfg.CONFIG, {"ntrip_enabled": True, "ntrip_host": "caster.invalid",
                                          "ntrip_mount": "SYNTHETIC", "ntrip_user": "",
                                          "ntrip_pass": "", "ntrip_fallback_host": ""}):
            client._attempt_endpoint = net.NtripMachine.configured_endpoints()[0]
            cfg.CONFIG.update(NEW)
            self.assertEqual((client.reload(), client.reload()), (True, True))
        self.assertEqual(calls, {"close": 1, "s.close": 1})
        self.assertTrue(client._reload_closed)
        self.assertTrue(client.machine.reload_requested)


class TestTaskReloadWiring(unittest.TestCase):
    """main.py hands the running NTRIP client to cfg as the task reloader (#23 part b)."""

    @staticmethod
    def calls(node, name):
        return [n for n in ast.walk(node) if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name) and n.func.id == name]

    def test_main_registers_the_ntrip_reloader_only_in_normal_mode(self):
        with open(os.path.join(REPO, "main.py")) as fh:
            tree = ast.parse(fh.read())
        imported = [alias.name for node in ast.walk(tree)
                    if isinstance(node, ast.ImportFrom) and node.module == "cfg"
                    for alias in node.names]
        self.assertIn("set_task_reloader", imported)
        # The block that creates the client registers its reload() after the assignment.
        blocks = [node.body for node in ast.walk(tree) if isinstance(getattr(node, "body", None), list)
                  and any(isinstance(s, ast.Assign) and isinstance(s.value, ast.Call)
                          and getattr(s.value.func, "id", None) == "NtripClient"
                          and [getattr(t, "id", None) for t in s.targets] == ["ntrip"]
                          for s in node.body)]
        self.assertEqual(len(blocks), 1)
        body = blocks[0]
        created = next(i for i, s in enumerate(body) if isinstance(s, ast.Assign)
                       and getattr(s.value, "func", None) is not None
                       and getattr(s.value.func, "id", None) == "NtripClient")
        registered = [i for i, s in enumerate(body) for call in self.calls(s, "set_task_reloader")
                      if len(call.args) == 1 and isinstance(call.args[0], ast.Attribute)
                      and call.args[0].attr == "reload"
                      and getattr(call.args[0].value, "id", None) == "ntrip"]
        self.assertEqual(len(registered), 1)
        self.assertGreater(registered[0], created)
        # The safe mode block (ending in its return) registers nothing.
        safe = [node for node in ast.walk(tree) if isinstance(node, ast.If)
                and isinstance(node.test, ast.Name) and node.test.id == "safe"
                and isinstance(node.body[-1], ast.Return)]
        self.assertEqual(len(safe), 1)
        self.assertEqual(self.calls(safe[0], "set_task_reloader"), [])
        self.assertEqual(len(self.calls(tree, "set_task_reloader")), 1)


class TestNtripRetryRules(unittest.TestCase):
    """Socket-free rules for error code, wait time, counting and success (#16)."""

    def setUp(self):
        for patcher in (
                mock.patch.dict(net.app.stats, {"ntrip_retries": 0, "last_ntrip_error_code": None},
                                clear=True),
                mock.patch.dict(cfg.CONFIG, {"ntrip_retry_delay_sec": 5, "ntrip_retry_max_sec": 300})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_error_codes(self):
        code = net.NtripMachine.error_code
        self.assertEqual(code(net.asyncio.TimeoutError()), "timeout")
        self.assertEqual(code(OSError(113)), "network_error")
        self.assertEqual(code(OSError(-202)), "network_error")
        self.assertEqual(code(RuntimeError("x")), "unexpected_error")
        self.assertEqual(code(Exception("HTTP Caster Error (not 200 OK)")), "unexpected_error")

    def test_backoff_doubles_per_attempt_up_to_the_cap(self):
        wait = net.NtripMachine.retry_wait
        self.assertEqual([wait(n, True, False) for n in range(0, 9)],
                         [5, 5, 10, 20, 40, 80, 160, 300, 300])
        self.assertEqual([wait(n, False, False) for n in range(1, 5)], [5, 10, 20, 40])

    def test_permanent_error_waits_the_cap_only_at_the_end_of_a_round(self):
        wait = net.NtripMachine.retry_wait
        self.assertEqual(wait(1, True, True), 300)
        self.assertEqual(wait(1, False, True), 5)
        self.assertEqual(wait(3, False, True), 20)

    def test_settle_counts_a_failed_attempt(self):
        self.assertEqual(net.NtripMachine.settle_attempt(2, "timeout", False), (3, False))
        self.assertEqual(net.app.stats, {"ntrip_retries": 1, "last_ntrip_error_code": "timeout"})

    def test_settle_treats_a_stable_stream_as_success(self):
        self.assertEqual(net.NtripMachine.settle_attempt(3, "eof", True), (0, True))
        self.assertEqual(net.app.stats, {"ntrip_retries": 0, "last_ntrip_error_code": "eof"})

    def test_settle_treats_a_stale_backlog_reconnect_as_success(self):
        self.assertEqual(net.NtripMachine.settle_attempt(3, "stale_backlog", False), (0, True))
        self.assertEqual(net.app.stats["ntrip_retries"], 0)


class TestNtripMachine(unittest.TestCase):
    """The connection rules without a socket (#23 part b)."""

    def setUp(self):
        self.states = []

        def set_ntrip_state(value):
            self.states.append(value)
            net.app.stats["ntrip_state"] = value

        for patcher in (
                mock.patch.dict(net.app.stats, {
                    "ntrip_retries": 0, "last_ntrip_error_code": None, "ntrip_state": "idle",
                    "ntrip_backoff_sec": 0, "ntrip_config": None}, clear=True),
                mock.patch.dict(cfg.CONFIG, {
                    "ntrip_retry_delay_sec": 5, "ntrip_retry_max_sec": 300, "ntrip_max_retries": 0}),
                mock.patch.object(net.app, "set_ntrip_state", set_ntrip_state)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def machine(self, *, max_retries=0, fallback=True, enabled=True):
        cfg.CONFIG.update({
            "ntrip_enabled": enabled, "ntrip_max_retries": max_retries,
            "ntrip_host": "caster.invalid", "ntrip_mount": "SYNTHETIC",
            "ntrip_fallback_host": "fallback.invalid" if fallback else "",
            "ntrip_fallback_mount": "FB" if fallback else ""})
        m = net.NtripMachine(net.RtcmCounter())
        m.load()
        return m

    def test_a_failed_round_alternates_and_doubles(self):
        m = self.machine()
        self.assertEqual([e["label"] for e in m.endpoints], ["primary", "fallback"])
        self.assertEqual(net.app.stats["ntrip_config"], "ready")
        waits = []
        for _ in range(3):
            m.begin_attempt()
            waits.append((m.endpoint()["label"], m.settle("timeout", False)))
        self.assertEqual(waits, [("primary", 5), ("fallback", 10), ("primary", 20)])
        self.assertEqual((m.retry_count, net.app.stats["ntrip_retries"]), (3, 3))

    def test_a_stable_stream_fails_back_to_the_primary(self):
        m = self.machine()
        m.begin_attempt()
        m.settle("timeout", False)                      # now the fallback
        m.begin_attempt()
        m.ok_data(1000)
        m.ok_data(61000)
        self.assertTrue(m.stable)
        self.assertEqual((m.settle("eof", False), m.index, m.retry_count), (5, 0, 0))

    def test_a_permanent_error_at_the_end_of_the_round_parks_with_its_state(self):
        m = self.machine(max_retries=1, fallback=False)
        m.begin_attempt()
        net.app.set_ntrip_state("auth_error")
        self.assertEqual(m.settle("auth_error", True), 300)
        self.assertTrue(m.retry_limit_reached())
        m.park()
        self.assertEqual((net.app.stats["ntrip_state"], net.app.stats["last_ntrip_error_code"],
                          net.app.stats["ntrip_backoff_sec"]), ("auth_error", "retries_exhausted", 0))
        m.release()
        self.assertEqual((m.retry_count, net.app.stats["last_ntrip_error_code"]), (0, None))

    def test_load_reads_endpoints_and_config_state_together(self):
        m = self.machine(enabled=False)
        self.assertEqual((m.endpoints, net.app.stats["ntrip_config"]), ([], "switched_off"))

    def test_load_keeps_an_unchanged_endpoint_and_resets_otherwise(self):
        m = self.machine()
        m.begin_attempt()
        m.settle("timeout", False)                      # now the fallback, one failure
        current = m.endpoint()
        cfg.CONFIG["ntrip_host"] = "new.invalid"        # only the primary changed
        m.request_reload()
        self.assertTrue(net.NtripMachine.keeps(current))
        self.assertTrue(m.load(current=current))
        self.assertEqual((m.index, m.retry_count, m.reload_requested), (1, 1, False))
        self.assertEqual(net.app.stats["ntrip_backoff_sec"], 5)
        cfg.CONFIG["ntrip_fallback_mount"] = "FB2"      # now the active endpoint changed
        m.request_reload()
        self.assertFalse(net.NtripMachine.keeps(current))
        self.assertFalse(m.load(current=current))
        self.assertEqual((m.endpoint()["host"], m.retry_count, m.reload_requested),
                         ("new.invalid", 0, False))
        self.assertEqual((net.app.stats["last_ntrip_error_code"], net.app.stats["ntrip_backoff_sec"]),
                         (None, 0))
        cfg.CONFIG["ntrip_enabled"] = False
        self.assertFalse(m.load(current=m.endpoint()))
        self.assertEqual((m.endpoints, net.app.stats["ntrip_endpoint"], net.app.stats["ntrip_config"]),
                         ([], None, "switched_off"))



class TestNtripConfigState(unittest.IsolatedAsyncioTestCase):
    """The effective NTRIP configuration the running client read (#20a E4)."""

    ENDPOINTS = (("h", "m"), ("h", ""), ("", "m"), ("", ""))

    def test_config_state_matches_the_endpoints(self):
        for enabled in (True, False):
            for host, mount in self.ENDPOINTS:
                for fallback_host, fallback_mount in self.ENDPOINTS:
                    values = {"ntrip_enabled": enabled, "ntrip_host": host,
                              "ntrip_mount": mount, "ntrip_fallback_host": fallback_host,
                              "ntrip_fallback_mount": fallback_mount}
                    with self.subTest(**values), mock.patch.dict(net.CONFIG, values):
                        config_state = net.NtripMachine.config_state()
                        endpoints = net.NtripMachine.configured_endpoints()
                        self.assertEqual(config_state == "ready", bool(endpoints))
                        if not enabled:
                            self.assertEqual(config_state, "switched_off")
                        elif not endpoints:
                            self.assertEqual(config_state, "incomplete")

    async def _run_until_first_sleep(self, values):
        stop = asyncio.Event()

        async def sleep(seconds):
            stop.set()

        with mock.patch.dict(net.CONFIG, values), \
                mock.patch.dict(net.app.stats, {"ntrip_state": "streaming",
                                                "ntrip_config": None}), \
                mock.patch.dict(net.app.heartbeat, {}, clear=True), \
                mock.patch.object(net, "shutdown_event", stop), \
                mock.patch.object(net.asyncio, "sleep", sleep), \
                mock.patch.object(net, "log"):
            await asyncio.wait_for(net.NtripClient(gnss.UartTx(FakeUART())).run(), 2)
            return dict(net.app.stats)

    async def test_disabled_client_publishes_why(self):
        stats = await self._run_until_first_sleep({"ntrip_enabled": False,
                                                   "ntrip_host": "h", "ntrip_mount": "m"})
        self.assertEqual(stats["ntrip_state"], "disabled")
        self.assertEqual(stats["ntrip_config"], "switched_off")
        stats = await self._run_until_first_sleep({
            "ntrip_enabled": True, "ntrip_host": "h", "ntrip_mount": "",
            "ntrip_fallback_host": "", "ntrip_fallback_mount": ""})
        self.assertEqual(stats["ntrip_state"], "disabled")
        self.assertEqual(stats["ntrip_config"], "incomplete")

    async def test_running_client_publishes_ready(self):
        # The scripted caster of TestNtripFlow, run on this test case.
        _, _, stats, _ = await TestNtripFlow.run_caster(self, require_gga=False,
                                                        chunks=[(10, b"rtcm")])
        self.assertEqual(self.resolved, [("caster.invalid", 2101)])
        self.assertIn("streaming", self.states)
        self.assertEqual(stats["ntrip_config"], "ready")


if __name__ == "__main__":
    unittest.main()
