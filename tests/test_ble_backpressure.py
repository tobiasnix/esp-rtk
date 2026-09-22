# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded BLE retry behavior under controller congestion and disconnects."""
import asyncio
import unittest
from unittest import mock

from support import ble, fanout, state


class TestBleBackpressure(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.manager = ble.BLEManager("RTK-TEST")
        self.manager.encrypted_connections.add(7)
        self.manager.payload_sizes[7] = 8
        self.clock = 0
        self.patches = [
            mock.patch.dict(state.app.stats, {"ble_drops": 0, "ble_tx_bytes": 0,
                "ble_notify_retries": 0, "ble_notify_timeouts": 0,
                "ble_notify_errors": 0}),
            mock.patch.object(ble.time, "ticks_ms", lambda: self.clock),
            mock.patch.object(ble.asyncio, "sleep_ms", self.sleep_ms),
        ]
        for patch in self.patches:
            patch.start()

    async def asyncTearDown(self):
        for patch in reversed(self.patches):
            patch.stop()

    async def sleep_ms(self, delay):
        self.clock += delay
        await asyncio.sleep(0)

    async def test_busy_for_multiple_connection_intervals_recovers_without_loss(self):
        received = []
        attempts = []

        def notify(conn, handle, chunk):
            attempts.append(chunk)
            if self.clock < 80:
                raise OSError(12)
            received.append(chunk)

        data = b"$GNGGA,test\r\n"
        with mock.patch.object(self.manager.ble, "gatts_notify", notify):
            await self.manager.send(data)
        self.assertEqual(b"".join(received), data)
        self.assertEqual(attempts[:9], [data[:8]] * 9)
        self.assertEqual(state.app.stats["ble_tx_bytes"], len(data))
        self.assertEqual(state.app.stats["ble_drops"], 0)
        self.assertEqual(state.app.stats["ble_notify_retries"], 8)

    async def test_permanently_busy_client_is_bounded_and_other_client_receives(self):
        self.manager.encrypted_connections.add(8)
        received = []

        def notify(conn, handle, chunk):
            if conn == 7:
                raise OSError(16)
            received.append(chunk)

        with mock.patch.object(self.manager.ble, "gatts_notify", notify):
            await self.manager.send(b"$GNGGA,test\r\n")
        self.assertEqual(b"".join(received), b"$GNGGA,test\r\n")
        self.assertEqual(self.clock, ble._NOTIFY_TIMEOUT_MS)
        self.assertEqual(state.app.stats["ble_notify_timeouts"], 1)
        self.assertEqual(state.app.stats["ble_drops"], 1)

    async def test_nontransient_and_negative_nimble_codes_are_not_retried(self):
        for error in (OSError(107), OSError(-12), ValueError("invalid")):
            with mock.patch.object(self.manager.ble, "gatts_notify", side_effect=error) as notify:
                await self.manager.send(b"$GNGGA,test\r\n")
            self.assertEqual(notify.call_count, 1)
        self.assertEqual(self.clock, 0)
        self.assertEqual(state.app.stats["ble_notify_errors"], 3)

    async def test_disconnect_during_retry_stops_notifying(self):
        def notify(conn, handle, chunk):
            self.manager.encrypted_connections.clear()
            raise OSError(12)

        with mock.patch.object(self.manager.ble, "gatts_notify", side_effect=notify) as calls:
            await self.manager.send(b"$GNGGA,test\r\n")
        self.assertEqual(calls.call_count, 1)
        self.assertEqual(state.app.stats["ble_tx_bytes"], 0)
        self.assertEqual(state.app.stats["ble_drops"], 1)

    async def test_one_gnss_epoch_drains_despite_shared_scheduler_latency(self):
        self.manager.payload_sizes[7] = 20
        router = fanout.NmeaSenderTask(self.manager, state.SimpleQueue())
        sentence = b"$GNTXT," + b"x" * 70 + b"\r\n"
        epoch = [sentence] * 14
        stop = asyncio.Event()
        delivered = bytearray()
        peer_turns = 0

        async def loaded_scheduler(delay):
            # Model 20 ms of competing work whenever this worker yields.
            nonlocal peer_turns
            self.clock += 20 + delay
            peer_turns += 1
            await asyncio.sleep(0)

        def notify(conn, handle, chunk):
            delivered.extend(chunk)
            if len(delivered) == sum(map(len, epoch)):
                stop.set()

        for item in epoch:
            router.ble_queue.put_nowait(item)
        with mock.patch.object(ble.asyncio, "sleep_ms", loaded_scheduler), \
                mock.patch.object(self.manager.ble, "gatts_notify", notify), \
                mock.patch.object(fanout, "shutdown_event", stop):
            await asyncio.wait_for(router.run_ble(), 1)
        self.assertEqual(bytes(delivered), b"".join(epoch))
        self.assertLess(self.clock, 1000, "One GNSS epoch must drain before the next")
        self.assertGreater(peer_turns, 0, "Other services must still get CPU time")
        self.assertEqual(state.app.stats["ble_drops"], 0)
