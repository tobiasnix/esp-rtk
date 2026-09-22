# SPDX-License-Identifier: AGPL-3.0-only
"""Independent BLE/TCP NMEA distribution and optional TCP authentication."""
import asyncio
import inspect
import unittest
from unittest import mock

from support import cfg, fanout, state


class FakeReader:
    def __init__(self, line):
        self.line = line

    async def read(self, amount):
        result, self.line = self.line[:amount], self.line[amount:]
        return result


class FakeWriter:
    def __init__(self, fails=False):
        self.buf = bytearray()
        self.geschlossen = False
        self.fails = fails

    def write(self, data):
        if self.fails:
            raise OSError(104, "Connection reset by peer")
        self.buf.extend(data)

    async def drain(self):
        if self.fails:
            raise OSError(104, "Connection reset by peer")

    def close(self):
        self.geschlossen = True

    async def wait_closed(self):
        pass

    def get_extra_info(self, key):
        return ("192.168.4.9", 5555)


def lauf(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestClientManagement(unittest.TestCase):
    def setUp(self):
        fanout.clients.clear()
        self.saved = dict(cfg.CONFIG)

    def tearDown(self):
        fanout.clients.clear()
        cfg.CONFIG.clear()
        cfg.CONFIG.update(self.saved)

    def test_client_is_added(self):
        w = FakeWriter()
        self.assertTrue(fanout.add_client(w))
        self.assertIn(w, fanout.clients)

    def test_the_upper_limit_shall_be_respected(self):
        cfg.CONFIG["nmea_tcp_max_clients"] = 2
        a, b, c = FakeWriter(), FakeWriter(), FakeWriter()
        self.assertTrue(fanout.add_client(a))
        self.assertTrue(fanout.add_client(b))
        self.assertFalse(fanout.add_client(c))
        self.assertEqual(len(fanout.clients), 2)

    def test_entfernen(self):
        w = FakeWriter()
        fanout.add_client(w)
        fanout.remove_client(w)
        self.assertNotIn(w, fanout.clients)

    def test_double_removal_is_harmless(self):
        w = FakeWriter()
        fanout.add_client(w)
        fanout.remove_client(w)
        fanout.remove_client(w)

    def test_tcp_authentication_only_accepts_stream_tokens(self):
        old = state.app.identity
        state.app.identity = {"stream_token": "STREAM123"}
        try:
            accepted, rejected = FakeWriter(), FakeWriter()
            self.assertTrue(lauf(fanout._authenticate_client(
                FakeReader(b"AUTH STREAM123\r\n"), accepted)))
            self.assertFalse(lauf(fanout._authenticate_client(
                FakeReader(b"AUTH WRONG\r\n"), rejected)))
        finally:
            state.app.identity = old
        self.assertEqual(bytes(accepted.buf), b"AUTH REQUIRED\r\nOK AUTH\r\n")
        self.assertEqual(bytes(rejected.buf), b"AUTH REQUIRED\r\nERR AUTH\r\n")

    def test_authentication_stops_at_byte_97(self):
        old = state.app.identity
        state.app.identity = {"stream_token": "STREAM123"}
        try:
            writer = FakeWriter()
            self.assertFalse(lauf(fanout._authenticate_client(
                FakeReader(b"A" * 97 + b"\n"), writer)))
        finally:
            state.app.identity = old

    def test_tcp_authentication_can_be_disabled_without_sending_auth_bytes(self):
        cfg.CONFIG["nmea_tcp_auth_required"] = False
        writer = FakeWriter()
        lauf(fanout._handle_client(FakeReader(b""), writer))
        self.assertEqual(bytes(writer.buf), b"")
        self.assertTrue(writer.geschlossen)

    def test_tcp_authentication_is_enabled_by_default(self):
        self.assertTrue(cfg.DEFAULT_CONFIG["nmea_tcp_auth_required"])


class TestVerteilung(unittest.TestCase):
    def setUp(self):
        fanout.clients.clear()
        self.saved = dict(cfg.CONFIG)

    def tearDown(self):
        fanout.clients.clear()
        cfg.CONFIG.clear()
        cfg.CONFIG.update(self.saved)

    def test_all_clients_get_the_phrase(self):
        a, b = FakeWriter(), FakeWriter()
        fanout.add_client(a)
        fanout.add_client(b)
        lauf(fanout.broadcast(b"$GNGGA,1\r\n"))
        self.assertEqual(bytes(a.buf), b"$GNGGA,1\r\n")
        self.assertEqual(bytes(b.buf), b"$GNGGA,1\r\n")

    def test_broken_client_will_be_removed(self):
        accepted, kaputt = FakeWriter(), FakeWriter(fails=True)
        fanout.add_client(accepted)
        fanout.add_client(kaputt)
        lauf(fanout.broadcast(b"$GNRMC,1\r\n"))
        self.assertIn(accepted, fanout.clients)
        self.assertNotIn(kaputt, fanout.clients)
        self.assertEqual(bytes(accepted.buf), b"$GNRMC,1\r\n")

    def test_without_clients_nothing_happens(self):
        lauf(fanout.broadcast(b"$GNGGA,1\r\n"))

    def test_distribution_survives_a_total_failure(self):
        kaputt = FakeWriter(fails=True)
        fanout.add_client(kaputt)
        lauf(fanout.broadcast(b"x"))
        self.assertEqual(fanout.clients, [])

    def test_router_decouples_ble_and_tcp_with_separate_queues(self):
        class Ble:
            pass
        router = fanout.NmeaSenderTask(Ble(), state.SimpleQueue(maxsize=1))
        router.route(b"one")
        self.assertEqual(router.ble_queue.qsize(), 1)
        self.assertEqual(router.tcp_queue.qsize(), 1)
        self.assertEqual(lauf(router.ble_queue.get()), b"one")
        self.assertEqual(router.tcp_queue.qsize(), 1)

    def test_full_transport_only_loses_its_own_copy(self):
        class Ble:
            pass
        router = fanout.NmeaSenderTask(Ble(), state.SimpleQueue(maxsize=1))
        old_ble = state.app.stats["ble_drops"]
        router.ble_queue = state.SimpleQueue(maxsize=1)
        router.ble_queue.put_nowait(b"occupied")
        router.route(b"new")
        self.assertEqual(state.app.stats["ble_drops"], old_ble + 1)
        self.assertEqual(lauf(router.tcp_queue.get()), b"new")

    def test_ble_worker_yields_between_bounded_sentence_batches(self):
        source = inspect.getsource(fanout.NmeaSenderTask.run_ble)
        self.assertIn("await asyncio.sleep_ms(0)", source)


class TestTransportRecovery(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_closes_micropython_socket_and_cancels_reader(self):
        class StalledWriter(FakeWriter):
            def close(self):
                pass  # MicroPython closes the socket in wait_closed().

            async def wait_closed(self):
                self.geschlossen = True

            async def drain(self):
                await asyncio.sleep(10)

        stalled, healthy = StalledWriter(), FakeWriter()
        reader = asyncio.create_task(asyncio.Event().wait())
        await asyncio.sleep(0)
        with mock.patch.object(fanout, "clients", []), \
                mock.patch.object(fanout, "_reader_tasks", {}), \
                mock.patch.object(fanout, "CLIENT_WRITE_TIMEOUT_SEC", 0.01), \
                mock.patch.dict(state.app.stats, {"tcp_write_timeouts": 0}):
            fanout.add_client(stalled, reader)
            fanout.add_client(healthy)
            await fanout.broadcast(b"$GNGGA,1\r\n")
            with self.assertRaises(asyncio.CancelledError):
                await reader
            self.assertTrue(stalled.geschlossen)
            self.assertEqual(fanout.clients, [healthy])
            self.assertFalse(fanout._reader_tasks)
            self.assertEqual(bytes(healthy.buf), b"$GNGGA,1\r\n")
            self.assertEqual(state.app.stats["tcp_write_timeouts"], 1)

    async def test_tcp_burst_preserves_complete_sentences_and_order(self):
        router = fanout.NmeaSenderTask(None, state.SimpleQueue())
        sentences = [("$GNTXT,%d," % n).encode() + b"x" * 75 + b"\r\n"
                     for n in range(40)]
        for sentence in sentences:
            self.assertTrue(router.tcp_queue.put_nowait(sentence))
        batches = []
        stop = asyncio.Event()

        async def receive(data):
            batches.append(data)
            if sum(map(len, batches)) == sum(map(len, sentences)):
                stop.set()

        with mock.patch.object(fanout, "broadcast", receive), \
                mock.patch.object(fanout, "shutdown_event", stop):
            await asyncio.wait_for(router.run_tcp(), 1)
        self.assertEqual(b"".join(batches), b"".join(sentences))
        self.assertLess(len(batches), len(sentences))
        for batch in batches:
            self.assertLessEqual(len(batch), fanout.TCP_BATCH_BYTES)
            self.assertTrue(batch.endswith(b"\r\n"))
            self.assertLessEqual(batch.count(b"\r\n"), fanout.TCP_BATCH_SENTENCES)
        self.assertEqual(router.tcp_queue.qsize(), 0)


if __name__ == "__main__":
    unittest.main()
