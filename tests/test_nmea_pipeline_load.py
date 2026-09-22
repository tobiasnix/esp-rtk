# SPDX-License-Identifier: AGPL-3.0-only
"""Exercise the real queue workers with delayed cooperative scheduling."""
import asyncio
import unittest
from unittest import mock

from support import fanout, gnss, sentence, state


class TestNmeaPipelineLoad(unittest.IsolatedAsyncioTestCase):
    async def receive_pipeline(self, long_sentences=False):
        payload = ('GNRMC,%04d,A,5000.000000,N,00800.0000,E,0.0,0.0,080926,,,A'
                   if long_sentences else 'GNRMC,%04d,A')
        expected = [(sentence(payload % index) + '\r\n').encode()
                    for index in range(1000)]
        ble_received, tcp_received = [], bytearray()
        ble_done, tcp_done, stop = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class Ble:
            async def send(self, data):
                ble_received.append(data)
                if len(ble_received) == len(expected):
                    ble_done.set()

        async def tcp_send(data):
            self.assertLessEqual(len(data), fanout.TCP_BATCH_BYTES)
            tcp_received.extend(data)
            if len(tcp_received) == sum(map(len, expected)):
                tcp_done.set()

        async def delayed_turn(ms):
            # Ready workers each get a turn while another task occupies time.
            await asyncio.sleep(max(ms / 1000, 0.001))

        queue = state.SimpleQueue(maxsize=512)
        router = fanout.NmeaSenderTask(Ble(), queue)

        async def uart_bursts():
            for index, data in enumerate(expected):
                if not queue.put_nowait(data):
                    state.app.stats['queue_overflows'] += 1
                if (index + 1) % gnss.NMEA_YIELD_INTERVAL == 0:
                    await delayed_turn(0)

        with mock.patch.object(state, 'shutdown_event', stop), \
                mock.patch.object(fanout, 'shutdown_event', stop), \
                mock.patch.object(fanout, 'broadcast', tcp_send), \
                mock.patch.object(fanout.asyncio, 'sleep_ms', delayed_turn), \
                mock.patch.dict(state.app.stats, {'queue_overflows': 0, 'ble_drops': 0,
                                                  'nmea_tcp_drops': 0}):
            tasks = [asyncio.create_task(worker()) for worker in
                     (router.run, router.run_ble, router.run_tcp)]
            try:
                await uart_bursts()
                self.assertEqual(state.app.stats['queue_overflows'], 0)
                await asyncio.wait_for(asyncio.gather(ble_done.wait(), tcp_done.wait()), 2)
                self.assertEqual(ble_received, expected)
                self.assertEqual(bytes(tcp_received), b''.join(expected))
                self.assertEqual(state.app.stats['ble_drops'], 0)
                self.assertEqual(state.app.stats['nmea_tcp_drops'], 0)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def test_complete_pipeline_catches_up_without_losing_or_reordering_sentences(self):
        await self.receive_pipeline()

    async def test_long_sentences_catch_up_through_all_bounded_output_workers(self):
        await self.receive_pipeline(long_sentences=True)

    async def test_filtered_batch_boundary_lines_cannot_skip_uart_yields(self):
        valid = (sentence('GNGGA,123519,4807.038,N,01131.000,E,4,24,0.6,100,M,40,M,1,0001')
                 + '\r\n').encode()
        received, turn_sizes = [], []
        stop = asyncio.Event()
        previous = 0

        class Queue:
            def put_nowait(self, data):
                received.append(data)
                return True

        handler = gnss.GNSSHandler(Queue())
        handler.uart.rx.extend((valid * 3 + b'ignored\r\n') * 64)

        async def schedule_other_services(ms):
            nonlocal previous
            turn_sizes.append(len(received) - previous)
            previous = len(received)
            if len(received) == 192:
                stop.set()
            await asyncio.sleep(0)

        with mock.patch.object(gnss, 'shutdown_event', stop), \
                mock.patch.object(gnss.asyncio, 'sleep_ms', schedule_other_services), \
                mock.patch.object(gnss.app, 'update_fix'):
            await asyncio.wait_for(handler.run(), 2)
        self.assertEqual(received, [valid] * 192)
        self.assertLessEqual(max(turn_sizes), gnss.NMEA_YIELD_INTERVAL)


if __name__ == '__main__':
    unittest.main()
