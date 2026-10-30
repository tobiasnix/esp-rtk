# SPDX-License-Identifier: AGPL-3.0-only
"""SimpleQueue (#28c): bounded queues sit on a deque, waiting reuses two events, the public semantics stay as at 08c5928 (FIFO, put_nowait refuses a full queue, maxsize <= 0 is unbounded, shutdown ends get and refuses put)."""
import asyncio
import collections
import unittest
from unittest import mock

from support import state


class SimpleQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        patcher = mock.patch.object(state, "shutdown_event", asyncio.Event())
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_waiting_does_not_create_events(self):
        for maxsize in (1, 0):
            queue = state.SimpleQueue(maxsize=maxsize)
            received = []
            expected = list(range(100))
            with mock.patch.object(state.asyncio, "Event", side_effect=AssertionError):
                for i in range(100):
                    getter = asyncio.create_task(queue.get())
                    await asyncio.sleep(0)
                    self.assertTrue(queue.put_nowait(i))
                    received.append(await getter)
                if maxsize:
                    for i in range(20):
                        self.assertTrue(queue.put_nowait(("held", i)))
                        putter = asyncio.create_task(queue.put(("next", i)))
                        await asyncio.sleep(0)
                        self.assertFalse(putter.done())
                        received.append(await queue.get())
                        await putter
                        received.append(await queue.get())
                        expected += [("held", i), ("next", i)]
            self.assertEqual(received, expected, maxsize)
            self.assertEqual(queue.qsize(), 0)

    def test_bounded_queue_is_backed_by_a_deque(self):
        self.assertIs(type(state.SimpleQueue(maxsize=512)._queue), collections.deque)
        self.assertIs(type(state.SimpleQueue()._queue), list)

    async def test_unbounded_queue_stays_unbounded(self):
        for maxsize in (0, -1):
            queue = state.SimpleQueue(maxsize=maxsize)
            self.assertTrue(all(queue.put_nowait(i) for i in range(2000)))
            self.assertFalse(queue.full())
            self.assertEqual(queue.qsize(), 2000)
            self.assertEqual([await queue.get() for _ in range(2000)], list(range(2000)))
            self.assertEqual(queue.qsize(), 0)

    async def test_full_queue_refuses_new_items_and_keeps_the_oldest(self):
        queue = state.SimpleQueue(maxsize=3)
        self.assertEqual([queue.put_nowait(i) for i in (1, 2, 3, 4)], [True, True, True, False])
        self.assertTrue(queue.full())
        self.assertEqual([await queue.get() for _ in range(3)], [1, 2, 3])
        self.assertEqual(queue.qsize(), 0)

    async def test_two_waiting_getters_each_get_one_item_in_order(self):
        queue = state.SimpleQueue(maxsize=8)
        first = asyncio.create_task(queue.get())
        second = asyncio.create_task(queue.get())
        await asyncio.sleep(0)
        self.assertFalse(first.done() or second.done())
        self.assertTrue(queue.put_nowait("a"))
        self.assertTrue(queue.put_nowait("b"))
        results = await asyncio.wait_for(asyncio.gather(first, second), 1)
        self.assertEqual(results, ["a", "b"])
        self.assertEqual(queue.qsize(), 0)

    async def test_cancelled_getter_does_not_swallow_an_item(self):
        queue = state.SimpleQueue(maxsize=8)
        cancelled = asyncio.create_task(queue.get())
        await asyncio.sleep(0)
        cancelled.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancelled
        waiting = asyncio.create_task(queue.get())
        await asyncio.sleep(0)
        self.assertFalse(waiting.done())
        self.assertTrue(queue.put_nowait("x"))
        self.assertEqual(await asyncio.wait_for(waiting, 1), "x")
        self.assertEqual(queue.qsize(), 0)

    async def test_blocked_put_resumes_after_a_get(self):
        queue = state.SimpleQueue(maxsize=1)
        self.assertTrue(queue.put_nowait(1))
        putter = asyncio.create_task(queue.put(2))
        await asyncio.sleep(0)
        self.assertFalse(putter.done())
        self.assertEqual(await queue.get(), 1)
        await asyncio.wait_for(putter, 1)
        self.assertEqual(queue.qsize(), 1)
        self.assertEqual(await queue.get(), 2)

    async def test_shutdown_refuses_puts_and_ends_gets_as_today(self):
        idle = state.SimpleQueue(maxsize=8)
        waiting = asyncio.create_task(idle.get())
        await asyncio.sleep(0)
        queue = state.SimpleQueue(maxsize=8)
        self.assertTrue(queue.put_nowait(1))
        state.shutdown_event.set()
        self.assertFalse(queue.put_nowait(2))
        await queue.put(3)
        self.assertEqual(queue.qsize(), 1)
        self.assertIsNone(await queue.get())
        self.assertEqual(queue.qsize(), 1)
        # A getter that waited before the shutdown is not woken by it; only cancellation ends it.
        for _ in range(5):
            await asyncio.sleep(0)
        self.assertFalse(waiting.done())
        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting


if __name__ == "__main__":
    unittest.main()
