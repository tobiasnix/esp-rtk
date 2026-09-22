# SPDX-License-Identifier: AGPL-3.0-only
"""Exercise the real NTRIP loop against a caster waiting for periodic GGA."""
import asyncio
import unittest
from unittest import mock

from support import net, cfg, web, FakeUART


class TestNtripFlow(unittest.IsolatedAsyncioTestCase):
    async def run_caster(self, *, require_gga=True, chunks=None, clock_start=100000,
                         uart_delay=0, short_write=False, gga_delay=0,
                         sentence="$GNGGA,synthetic", gga_interval=10,
                         fail_gga=False, idle_timeout=60, wall_jump_per_read=0):
        stop = asyncio.Event()
        wifi = asyncio.Event()
        wifi.set()
        clock = [clock_start]
        wall_offset = [0]
        writes = []
        delivered = []
        chunks = list(chunks or [])

        class Reader:
            headers = [b"ICY 200 OK\r\n", b"\r\n"]

            async def readline(self):
                return self.headers.pop(0)

            async def read(self, size):
                wall_offset[0] += wall_jump_per_read
                if require_gga:
                    if len([v for v in writes if v.startswith(b"$GNGGA")]) < 2:
                        clock[0] += 5000
                        if clock[0] - clock_start >= 30000:
                            stop.set()
                        raise asyncio.TimeoutError
                    stop.set()
                    return b"corrections-after-gga"
                if not chunks:
                    stop.set()
                    return b""
                delay, data = chunks.pop(0)
                clock[0] += delay
                if isinstance(data, BaseException):
                    raise data
                return data

        class Writer:
            def write(self, data):
                writes.append(bytes(data))

            async def drain(self):
                if writes[-1].startswith(b"$GNGGA"):
                    clock[0] += gga_delay
                    if fail_gga:
                        raise OSError("synthetic GGA send failure")

            def close(self):
                pass

            async def wait_closed(self):
                pass

        class UART(FakeUART):
            def write(self, data):
                clock[0] += uart_delay
                delivered.append(bytes(data))
                return len(data) - 1 if short_write else len(data)

        async def connect(*args):
            return Reader(), Writer()

        async def backoff(seconds):
            clock[0] += int(seconds * 1000)
            stop.set()

        period = 1 << 30
        with mock.patch.dict(cfg.CONFIG, {
                "ntrip_enabled": True, "ntrip_host": "caster.invalid",
                "ntrip_mount": "SYNTHETIC", "ntrip_user": "", "ntrip_pass": "",
                "ntrip_fallback_host": "", "ntrip_gga_interval_sec": gga_interval,
                "ntrip_idle_timeout_sec": idle_timeout, "ntrip_read_timeout_sec": 5}), \
                mock.patch.dict(net.app.stats, {"ntrip_bytes": 0, "ntrip_gga_sent": 0,
                                              "ntrip_retries": 0}, clear=True), \
                mock.patch.object(net, "shutdown_event", stop), \
                mock.patch.object(net.app, "wifi_connected_event", wifi), \
                mock.patch.object(net.app, "last_gga_raw", sentence), \
                mock.patch.object(net.asyncio, "open_connection", connect), \
                mock.patch.object(net.asyncio, "sleep", backoff), \
                mock.patch.object(net.time, "time", lambda: (clock[0] + wall_offset[0]) // 1000), \
                mock.patch.object(net.time, "ticks_ms", lambda: clock[0] % period), \
                mock.patch.object(net.time, "ticks_diff",
                                  lambda a, b: (a - b + period // 2) % period - period // 2), \
                mock.patch.object(net, "log"), mock.patch.object(net.app, "log_error"):
            client = net.NtripClient(UART())
            await asyncio.wait_for(client.run(), 2)
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

    async def test_short_uart_write_cannot_look_like_complete_forwarding(self):
        _, _, stats, flow = await self.run_caster(require_gga=False,
            chunks=[(10, b"abc")], short_write=True)
        self.assertEqual(stats["ntrip_bytes"], 3)
        self.assertEqual(flow["rx_bytes"], 3)
        self.assertEqual(flow["uart_bytes"], 2)
        self.assertEqual(flow["uart_short_writes"], 1)

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

    async def test_summary_does_not_publish_payload_or_share_mutable_state(self):
        client = net.NtripClient(FakeUART())
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

    async def test_received_bytes_restart_the_idle_deadline(self):
        delivered, _, _, flow = await self.run_caster(require_gga=False,
            chunks=[(5000, asyncio.TimeoutError()), (5000, b'corrections'),
                    (5000, asyncio.TimeoutError()), (5000, asyncio.TimeoutError()),
                    (5000, asyncio.TimeoutError()), (5000, b'too-late')],
            idle_timeout=15)
        self.assertEqual(delivered, [b'corrections'])
        self.assertEqual(flow['read_timeouts'], 4)

    async def test_status_publishes_receive_timing_from_the_active_client(self):
        client = net.NtripClient(FakeUART())
        with mock.patch.dict(web._instances, {"ntrip": client}, clear=True), \
                mock.patch.object(web, "_read_allowed", return_value=True), \
                mock.patch.object(web, "board_metrics", return_value={}), \
                mock.patch.object(web, "_send_json") as send:
            await web._handle_status_request(None, "/status", "", False, {})
        report = send.call_args.args[1]["ntrip_flow"]
        self.assertEqual(report["schema"], 1)
        self.assertEqual(report["rx_chunks"], 0)
        self.assertIsNone(report["rx_age_ms"])
        self.assertGreaterEqual(report["phase_age_ms"], 0)


if __name__ == "__main__":
    unittest.main()
