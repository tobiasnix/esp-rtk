# SPDX-License-Identifier: AGPL-3.0-only
"""Independent BLE/TCP NMEA distribution and optional TCP authentication."""
import asyncio
import inspect
import unittest
from unittest import mock

import support
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


class MpWriter(FakeWriter):
    """MicroPython-style stream: close() is a no-op, wait_closed() closes the socket.
    drain number n in block_drains waits on gate (default: none blocks)."""
    def __init__(self, block_drains=()):
        super().__init__()
        self.block_drains = set(block_drains)
        self.gate = asyncio.Event()
        self.drains = 0
        self.fail_drains = set()
        self.closes = 0

    async def drain(self):
        self.drains += 1
        if self.drains in self.fail_drains:
            raise OSError(104, "Connection reset by peer")
        if self.drains in self.block_drains or 0 in self.block_drains:
            await self.gate.wait()

    def close(self):
        pass

    async def wait_closed(self):
        self.geschlossen = True
        self.closes += 1


class DripReader:
    """Sends the bytes of data (default: endless b"A") one every gap seconds."""
    def __init__(self, gap, data=None):
        self.gap, self.data, self.reads = gap, data, 0

    async def read(self, amount):
        await asyncio.sleep(self.gap)
        self.reads += 1
        if self.data is None:
            return b"A"
        chunk, self.data = self.data[:1], self.data[1:]
        return chunk


class ControlledReader:
    """Returns scripted bytes one read at a time, then blocks until eof() or forever."""
    def __init__(self, data=b""):
        self.data = bytearray(data)
        self.ended = asyncio.Event()

    async def read(self, amount):
        if self.data:
            chunk = bytes(self.data[:amount]); del self.data[:amount]
            return chunk
        await self.ended.wait()
        return b""

    def eof(self):
        self.ended.set()


async def until(predicate, timeout=1.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not predicate():
        if loop.time() > end:
            raise AssertionError("condition not reached within %.2f s" % timeout)
        await asyncio.sleep(0.001)


def lauf(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        # Session writer tasks outlive the coroutine; settle them before closing.
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending,
                                                   return_exceptions=True))
        loop.close()


def start_client_patches(test):
    """Isolate fanout's module state; the patches stop in tearDown."""
    test.patches = [mock.patch.object(fanout, "clients", []),
                    mock.patch.object(fanout, "_sessions", {}),
                    mock.patch.dict(state.app.stats, {"nmea_tcp_client_stats": []})]
    for patch in test.patches:
        patch.start()


def stop_client_patches(test):
    for patch in reversed(test.patches):
        patch.stop()


class TestClientManagement(unittest.TestCase):
    def setUp(self):
        start_client_patches(self)
        self.saved = dict(cfg.CONFIG)

    def tearDown(self):
        stop_client_patches(self)
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

    def test_synchronous_removal_cancels_tasks_without_running_loop(self):
        # MicroPython's current_task() raises RuntimeError outside a task.
        loop = asyncio.new_event_loop()
        try:
            reader = loop.create_task(asyncio.Event().wait())
            w = FakeWriter()
            fanout.add_client(w, reader)
            self.assertTrue(fanout.remove_client(w))
            loop.run_until_complete(asyncio.gather(reader, return_exceptions=True))
            self.assertTrue(reader.cancelled())
        finally:
            loop.close()

    def test_tcp_authentication_is_enabled_by_default(self):
        self.assertTrue(cfg.DEFAULT_CONFIG["nmea_tcp_auth_required"])


class TestVerteilung(unittest.TestCase):
    def setUp(self):
        start_client_patches(self)
        self.saved = dict(cfg.CONFIG)

    def tearDown(self):
        stop_client_patches(self)
        cfg.CONFIG.clear()
        cfg.CONFIG.update(self.saved)

    def test_without_clients_nothing_happens(self):
        lauf(fanout.broadcast(b"$GNGGA,1\r\n"))

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


class TestPerClientFanout(unittest.IsolatedAsyncioTestCase):
    """Each TCP client has its own bounded queue and writer task."""

    async def asyncSetUp(self):
        self.sessions = []
        self.patches = [
            mock.patch.object(fanout, "clients", []),
            mock.patch.object(fanout, "_sessions", {}),
            mock.patch.object(fanout, "CLIENT_WRITE_TIMEOUT_SEC", 0.2),
            mock.patch.dict(state.app.stats, {
                "nmea_tcp_drops": 0, "tcp_queue_overflows": 0,
                "tcp_client_queue_max": 0, "nmea_tcp_client_stats": [],
                "tcp_write_timeouts": 0, "tcp_write_errors": 0,
                "tcp_tx_bytes": 0, "ble_drops": 0}),
        ]
        for patch in self.patches:
            patch.start()

    async def asyncTearDown(self):
        tasks = []
        for session in self.sessions:
            gate = getattr(session.writer, "gate", None)
            if gate is not None:
                gate.set()
            for task in (session.reader_task, session.writer_task):
                if task is not None and not task.done():
                    task.cancel()
                    tasks.append(task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for patch in reversed(self.patches):
            patch.stop()

    async def connect(self, writer, reader_task=None):
        session = fanout.add_client(writer, reader_task)
        session.start()
        self.sessions.append(session)
        return session

    async def test_writer_of_a_removed_session_ends_on_the_next_wake(self):    # #28a M-1
        # uasyncio's wait_for can swallow the cancel of remove_client; the
        # writer must not keep serving a session that is gone.
        writer = FakeWriter()
        session = await self.connect(writer)
        await asyncio.sleep(0)                                  # writer waits on ready
        self.assertFalse(session.writer_task.done())
        with mock.patch.object(fanout._ClientSession, "cancel_tasks"):   # cancel lost
            self.assertTrue(fanout.remove_client(writer))
        session.offer(b"$GNGGA,late\r\n")                     # a stale wake with data
        await until(lambda: session.writer_task.done(), timeout=0.2)
        self.assertEqual(bytes(writer.buf), b"")

    async def test_removal_wakes_a_waiting_writer_even_if_its_cancel_was_lost(self):  # M-5
        writer = FakeWriter()
        session = await self.connect(writer)
        await asyncio.sleep(0)                                  # writer waits on ready
        with mock.patch.object(fanout._ClientSession, "cancel_tasks"):   # cancel lost
            self.assertTrue(fanout.remove_client(writer))
        await until(lambda: session.writer_task.done(), timeout=0.2)    # no offer() needed
        self.assertEqual(bytes(writer.buf), b"")

    async def test_blocked_client_does_not_delay_other_client(self):
        a, b = MpWriter(block_drains={0}), FakeWriter()
        reader_a = asyncio.create_task(asyncio.Event().wait())
        items = [b"$GNGGA,%02d\r\n" % n for n in range(20)]
        with mock.patch.object(fanout, "CLIENT_QUEUE_ITEMS", 3):
            sa = await self.connect(a, reader_a)
            sb = await self.connect(b)
            await fanout.broadcast(items[0])
            await until(lambda: a.drains == 1)
            for item in items[1:]:
                await fanout.broadcast(item)
                await asyncio.sleep(0)
            await until(lambda: bytes(b.buf) == b"".join(items), timeout=0.1)
            self.assertIn(a, fanout.clients)            # B was complete while A still blocked
            self.assertEqual(sa.stats["drops"], 16)     # 1 in drain, 3 queued, 16 dropped
            self.assertEqual(sb.stats["drops"], 0)
            self.assertEqual(state.app.stats["nmea_tcp_drops"], 16)
            self.assertEqual(state.app.stats["tcp_queue_overflows"], 16)
            await until(lambda: a not in fanout.clients)
        self.assertTrue(a.geschlossen)
        with self.assertRaises(asyncio.CancelledError):
            await reader_a
        await until(lambda: sa.writer_task.done())
        self.assertEqual(state.app.stats["tcp_write_timeouts"], 1)
        self.assertEqual(sa.stats["drops"], 20)         # + in-flight item and 3 queued items
        self.assertEqual(state.app.stats["nmea_tcp_drops"], 20)
        self.assertEqual(fanout.clients, [b])
        self.assertEqual(state.app.stats["nmea_tcp_client_stats"], [sb.stats])

    async def test_client_recovering_within_write_timeout_receives_everything(self):
        a = MpWriter(block_drains={1})
        sa = await self.connect(a)
        items = [b"$GNRMC,%02d\r\n" % n for n in range(20)]
        for item in items:
            await fanout.broadcast(item)
        await asyncio.sleep(0.05)                        # < CLIENT_WRITE_TIMEOUT_SEC (0.2)
        a.gate.set()
        await until(lambda: bytes(a.buf) == b"".join(items))
        self.assertEqual(sa.stats["drops"], 0)
        self.assertEqual(sa.stats["queue_max"], 20)      # all 20 queued before the writer ran
        self.assertIsNone(sa.inflight)
        self.assertIn(a, fanout.clients)

    async def test_client_queue_is_bounded_by_bytes(self):
        session = fanout.add_client(FakeWriter())
        item = (b"$GNTXT," + b"x" * 119 + b"\r\n") * 16
        self.assertEqual(len(item), 2048)
        self.assertEqual([session.offer(item) for _ in range(5)],
                         [True, True, True, True, False])
        self.assertEqual(session.queued_bytes, 8192)
        self.assertEqual(session.stats["drops"], 16)
        self.assertEqual(session.stats["queue_max"], 4)
        self.assertEqual(state.app.stats["tcp_client_queue_max"], 4)

    async def test_removing_a_session_counts_discarded_sentences(self):
        w = FakeWriter()
        session = fanout.add_client(w)
        item = (b"$GNTXT," + b"x" * 119 + b"\r\n") * 16
        session.offer(item)
        session.offer(item)
        self.assertIs(session.take(), item)
        self.assertIs(session.inflight, item)
        self.assertTrue(fanout.remove_client(w))
        self.assertEqual(session.stats["drops"], 32)
        self.assertEqual(state.app.stats["nmea_tcp_drops"], 32)
        self.assertEqual(session.items, [])
        self.assertIsNone(session.inflight)
        self.assertFalse(fanout.remove_client(w))
        self.assertEqual(session.stats["drops"], 32)
        self.assertEqual(state.app.stats["nmea_tcp_drops"], 32)

    async def test_all_clients_get_the_phrase(self):
        a, b = FakeWriter(), FakeWriter()
        await self.connect(a)
        await self.connect(b)
        await fanout.broadcast(b"$GNGGA,1\r\n")
        await until(lambda: bytes(a.buf) == b"$GNGGA,1\r\n"
                    and bytes(b.buf) == b"$GNGGA,1\r\n")

    async def test_broken_client_will_be_removed(self):
        accepted, kaputt = FakeWriter(), FakeWriter(fails=True)
        await self.connect(accepted)
        await self.connect(kaputt)
        await fanout.broadcast(b"$GNRMC,1\r\n")
        await until(lambda: kaputt not in fanout.clients)
        self.assertIn(accepted, fanout.clients)
        await until(lambda: bytes(accepted.buf) == b"$GNRMC,1\r\n")
        self.assertEqual(state.app.stats["tcp_write_errors"], 1)
        self.assertEqual(state.app.stats["nmea_tcp_drops"], 1)

    async def test_distribution_survives_a_total_failure(self):
        kaputt = FakeWriter(fails=True)
        await self.connect(kaputt)
        await fanout.broadcast(b"x")
        await until(lambda: fanout.clients == [])
        self.assertEqual(fanout._sessions, {})

    async def test_timeout_closes_micropython_socket_and_cancels_reader(self):
        stalled, healthy = MpWriter(block_drains={0}), FakeWriter()
        reader = asyncio.create_task(asyncio.Event().wait())
        await self.connect(stalled, reader)
        await self.connect(healthy)
        await fanout.broadcast(b"$GNGGA,1\r\n")
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(reader, 1)
        self.assertTrue(stalled.geschlossen)
        self.assertEqual(fanout.clients, [healthy])
        self.assertNotIn(id(stalled), fanout._sessions)
        self.assertEqual(bytes(healthy.buf), b"$GNGGA,1\r\n")
        self.assertEqual(state.app.stats["tcp_write_timeouts"], 1)
        self.assertEqual(state.app.stats["nmea_tcp_drops"], 1)

    async def test_writer_crash_closes_client_and_cancels_reader(self):
        with mock.patch.object(fanout, "_deliver",
                               mock.AsyncMock(side_effect=RuntimeError("boom"))), \
                mock.patch.object(fanout, "_log_send_failure") as logged:
            a = MpWriter()
            reader = asyncio.create_task(asyncio.Event().wait())
            sa = await self.connect(a, reader)
            await fanout.broadcast(b"$GNGGA,1\r\n")
            await until(lambda: a not in fanout.clients)
            await until(lambda: reader.done() and sa.writer_task.done())
        self.assertTrue(reader.cancelled())
        self.assertTrue(a.geschlossen)
        logged.assert_called_once_with("RuntimeError")
        self.assertIsNone(sa.writer_task.exception())
        self.assertEqual(sa.stats["drops"], 1)

    async def test_stalled_client_keeps_ble_and_healthy_client_complete(self):
        ble_received = []

        class Ble:
            async def send(self, data):
                ble_received.append(data)

        with mock.patch.object(fanout, "CLIENT_WRITE_TIMEOUT_SEC", 5), \
                mock.patch.object(fanout, "CLIENT_QUEUE_ITEMS", 3):
            queue = state.SimpleQueue(maxsize=512)
            router = fanout.NmeaSenderTask(Ble(), queue)
            a, b = MpWriter(block_drains={0}), FakeWriter()
            sa = await self.connect(a)
            sb = await self.connect(b)
            tasks = [asyncio.create_task(router.run()),
                     asyncio.create_task(router.run_ble()),
                     asyncio.create_task(router.run_tcp())]
            try:
                sentences = [b"$GNGGA,%04d\r\n" % n for n in range(300)]
                for n, sentence in enumerate(sentences, 1):
                    self.assertTrue(queue.put_nowait(sentence))
                    if n % 10 == 0:
                        await asyncio.sleep(0.001)
                await until(lambda: bytes(b.buf) == b"".join(sentences)
                            and ble_received == sentences, timeout=1)
                self.assertEqual(state.app.stats["ble_drops"], 0)
                self.assertEqual(sb.stats["drops"], 0)
                self.assertGreater(sa.stats["drops"], 0)
                self.assertEqual(state.app.stats["nmea_tcp_drops"], sa.stats["drops"])
                self.assertIn(a, fanout.clients)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)


class _HandshakeTestCase(unittest.IsolatedAsyncioTestCase):
    """Fanout state isolated for AUTH handshake and client-limit tests."""
    max_clients = 4

    async def asyncSetUp(self):
        self.old_identity = state.app.identity
        state.app.identity = {"stream_token": "STREAM123"}
        self.writers = []
        self.tasks = []
        self.patches = [
            mock.patch.object(fanout, "AUTH_TIMEOUT_SEC", 0.05),
            mock.patch.object(fanout, "AUTH_DEADLINE_SEC", 0.2),
            mock.patch.object(fanout, "clients", []),
            mock.patch.object(fanout, "_sessions", {}),
            mock.patch.object(fanout, "pending_auth", 0),
            mock.patch.object(fanout, "log"),
            mock.patch.dict(fanout.CONFIG, {"nmea_tcp_auth_required": True,
                                            "nmea_tcp_max_clients": self.max_clients}),
            mock.patch.dict(state.app.stats, {
                "nmea_tcp_auth_failures": 0, "tcp_auth_failures": 0,
                "nmea_tcp_rejected_full": 0, "nmea_tcp_client_stats": []}),
        ]
        for patch in self.patches:
            patch.start()

    async def asyncTearDown(self):
        for writer in self.writers:
            gate = getattr(writer, "gate", None)
            if gate is not None:
                gate.set()
        pending = [task for task in self.tasks if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for patch in reversed(self.patches):
            patch.stop()
        state.app.identity = self.old_identity

    def mp_writer(self, block_drains=()):
        writer = MpWriter(block_drains)
        self.writers.append(writer)
        return writer

    def handler(self, reader, writer):
        task = asyncio.create_task(fanout._handle_client(reader, writer))
        self.tasks.append(task)
        return task

    async def timed(self, coro):
        loop = asyncio.get_running_loop()
        started = loop.time()
        result = await asyncio.wait_for(coro, 1)
        return result, loop.time() - started


class TestAuthDeadline(_HandshakeTestCase):
    """The AUTH handshake, its drains included, ends within one total deadline."""

    async def test_auth_without_bytes_ends_after_read_timeout(self):
        w = FakeWriter()
        ok, elapsed = await self.timed(
            fanout._authenticate_client(ControlledReader(), w))
        self.assertFalse(ok)
        self.assertGreaterEqual(elapsed, 0.04)
        self.assertLess(elapsed, 0.15)
        self.assertEqual(bytes(w.buf), b"AUTH REQUIRED\r\n")

    async def test_slow_drip_ends_at_total_deadline(self):
        reader = DripReader(0.03)
        ok, elapsed = await self.timed(
            fanout._authenticate_client(reader, FakeWriter()))
        self.assertFalse(ok)
        self.assertGreaterEqual(elapsed, 0.18)
        self.assertLess(elapsed, 0.35)
        self.assertLess(reader.reads, fanout.AUTH_MAX_BYTES)

    async def test_greeting_drain_counts_against_deadline(self):
        ok, elapsed = await self.timed(fanout._authenticate_client(
            ControlledReader(), self.mp_writer(block_drains={1})))
        self.assertFalse(ok)
        self.assertLess(elapsed, 0.35)

    async def test_ok_drain_counts_against_deadline(self):
        w = self.mp_writer(block_drains={2})
        ok, elapsed = await self.timed(fanout._authenticate_client(
            FakeReader(b"AUTH STREAM123\r\n"), w))
        self.assertFalse(ok)
        self.assertLess(elapsed, 0.35)
        self.assertEqual(bytes(w.buf), b"AUTH REQUIRED\r\nOK AUTH\r\n")

    async def test_err_drain_counts_against_deadline(self):
        w = self.mp_writer(block_drains={2})
        ok, elapsed = await self.timed(fanout._authenticate_client(
            FakeReader(b"AUTH WRONG\r\n"), w))
        self.assertFalse(ok)
        self.assertLess(elapsed, 0.35)
        self.assertEqual(state.app.stats["tcp_auth_failures"], 1)

    async def test_read_and_ok_drain_share_one_budget(self):
        w = self.mp_writer(block_drains={2})
        loop = asyncio.get_running_loop()
        with mock.patch.object(fanout, "AUTH_TIMEOUT_SEC", 0.1), \
                mock.patch.object(fanout, "AUTH_DEADLINE_SEC", 0.5):
            started = loop.time()
            task = asyncio.create_task(fanout._authenticate_client(
                DripReader(0.01, b"AUTH STREAM123\r\n"), w))
            self.tasks.append(task)
            await until(lambda: w.drains == 2)          # OK AUTH drain hangs
            read_done = loop.time() - started
            ok = await asyncio.wait_for(task, 1)
            elapsed = loop.time() - started
        self.assertFalse(ok)
        self.assertTrue(bytes(w.buf).endswith(b"OK AUTH\r\n"))
        # 16 bytes take about 0.16 s, leaving about 0.34 s of the 0.5 s for the drain.
        self.assertLess(read_done, 0.4)
        self.assertGreaterEqual(elapsed, 0.48)
        self.assertLess(elapsed, 0.65)
        # A fresh full drain budget would end at read_done + 0.5 s or later.
        self.assertLess(elapsed, read_done + 0.45)

    async def test_passed_connection_start_is_honoured(self):
        with mock.patch.object(fanout, "AUTH_TIMEOUT_SEC", 0.3), \
                mock.patch.object(fanout, "AUTH_DEADLINE_SEC", 0.4):
            ok, elapsed = await self.timed(fanout._authenticate_client(
                ControlledReader(), FakeWriter(),
                started_ms=fanout.time.ticks_ms() - 300))
        self.assertFalse(ok)
        self.assertLess(elapsed, 0.2)            # 0.1 s left, not the 0.3 s read timeout

    async def test_handle_client_closes_slow_drip_and_releases_slot(self):
        w = self.mp_writer()
        _result, elapsed = await self.timed(
            fanout._handle_client(DripReader(0.03), w))
        self.assertLess(elapsed, 0.35)
        self.assertTrue(w.geschlossen)
        self.assertEqual(fanout.pending_auth, 0)
        self.assertEqual(fanout.clients, [])
        self.assertEqual(state.app.stats["nmea_tcp_auth_failures"], 1)

    async def test_cancel_during_handshake_closes_socket(self):
        phases = {
            "greeting_drain": ({1}, b"", lambda w: w.drains == 1),
            "byte_read": ((), b"", lambda w: bytes(w.buf) == b"AUTH REQUIRED\r\n"
                          and fanout.pending_auth == 1),
            "ok_drain": ({2}, b"AUTH STREAM123\r\n", lambda w: w.drains == 2),
        }
        # No deadline or read timeout may expire here; only the cancellation ends it.
        with mock.patch.object(fanout, "AUTH_DEADLINE_SEC", 5), \
                mock.patch.object(fanout, "AUTH_TIMEOUT_SEC", 5):
            for phase, (blocked, data, reached) in phases.items():
                with self.subTest(phase=phase):
                    w = self.mp_writer(block_drains=blocked)
                    task = self.handler(ControlledReader(data), w)
                    await until(lambda: reached(w))
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    self.assertTrue(w.geschlossen)     # only wait_closed() sets it
                    self.assertEqual(fanout.pending_auth, 0)
                    self.assertNotIn(w, fanout.clients)


class TestClientLimit(_HandshakeTestCase):
    """Open handshakes and established clients share nmea_tcp_max_clients."""
    max_clients = 2

    async def test_pending_and_established_share_the_limit(self):
        fanout.add_client(FakeWriter())
        w1, w2 = self.mp_writer(), self.mp_writer()
        with mock.patch.object(fanout, "AUTH_TIMEOUT_SEC", 5), \
                mock.patch.object(fanout, "AUTH_DEADLINE_SEC", 5):
            t1 = self.handler(ControlledReader(), w1)
            self.handler(ControlledReader(), w2)
            for _ in range(3):
                await asyncio.sleep(0)
            self.assertEqual(bytes(w1.buf), b"AUTH REQUIRED\r\n")
            self.assertEqual(bytes(w2.buf), b"")
            self.assertTrue(w2.geschlossen)
            self.assertEqual(fanout.pending_auth, 1)
            self.assertEqual(state.app.stats["nmea_tcp_rejected_full"], 1)
            self.assertEqual(fanout.pending_auth + len(fanout.clients), 2)
            t1.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await t1
        self.assertEqual(fanout.pending_auth, 0)
        self.assertTrue(w1.geschlossen)

    async def test_limit_applies_without_authentication(self):
        fanout.CONFIG["nmea_tcp_auth_required"] = False
        fanout.CONFIG["nmea_tcp_max_clients"] = 1
        fanout.add_client(FakeWriter())
        w = FakeWriter()
        await asyncio.wait_for(fanout._handle_client(FakeReader(b""), w), 1)
        self.assertEqual(bytes(w.buf), b"")
        self.assertTrue(w.geschlossen)
        self.assertEqual(state.app.stats["nmea_tcp_rejected_full"], 1)
        self.assertEqual(len(fanout.clients), 1)



class FakeServer:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


class TestSessionLifetime(unittest.IsolatedAsyncioTestCase):
    """Sessions with the real connection handler: rotation, peer end, listener restart."""

    async def asyncSetUp(self):
        self.old_identity = state.app.identity
        state.app.identity = {"stream_token": "OLD"}
        self.writers = []
        self.tasks = []
        self.patches = [
            mock.patch.object(fanout, "clients", []),
            mock.patch.object(fanout, "_sessions", {}),
            mock.patch.object(fanout, "pending_auth", 0),
            mock.patch.object(fanout, "CLIENT_WRITE_TIMEOUT_SEC", 0.2),
            mock.patch.object(fanout, "log"),
            mock.patch.dict(fanout.CONFIG, {"nmea_tcp_auth_required": True,
                                            "nmea_tcp_max_clients": 4}),
            mock.patch.dict(state.app.stats, {
                "nmea_tcp_drops": 0, "tcp_queue_overflows": 0,
                "tcp_client_queue_max": 0, "nmea_tcp_client_stats": [],
                "tcp_write_timeouts": 0, "tcp_write_errors": 0,
                "tcp_tx_bytes": 0, "ble_drops": 0}),
        ]
        for patch in self.patches:
            patch.start()

    async def asyncTearDown(self):
        for writer in self.writers:
            writer.gate.set()
        tasks = list(self.tasks)
        for session in list(fanout._sessions.values()):
            tasks += [session.reader_task, session.writer_task]
        pending = [task for task in tasks if task is not None and not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for patch in reversed(self.patches):
            patch.stop()
        state.app.identity = self.old_identity

    def mp_writer(self, block_drains=()):
        writer = MpWriter(block_drains)
        self.writers.append(writer)
        return writer

    def start_handler(self, reader, writer, handler=None):
        task = asyncio.create_task((handler or fanout._handle_client)(reader, writer))
        self.tasks.append(task)
        return task

    async def open_client(self, writer, reader, handler=None):
        """Run the real handler until the client is admitted; returns (task, session)."""
        task = self.start_handler(reader, writer, handler)
        await until(lambda: writer in fanout.clients)
        return task, fanout._sessions[id(writer)]

    async def test_disconnect_all_closes_sockets_and_ends_both_tasks(self):
        # Drains 1-2: greeting and OK AUTH; drain 3: first NMEA payload hangs.
        a, b = self.mp_writer(block_drains={3}), self.mp_writer()
        ta, sa = await self.open_client(a, ControlledReader(b"AUTH OLD\r\n"))
        tb, sb = await self.open_client(b, ControlledReader(b"AUTH OLD\r\n"))
        await fanout.broadcast(b"$GNGGA,1\r\n")
        await fanout.broadcast(b"$GNGGA,2\r\n")
        await until(lambda: a.drains == 3
                    and bytes(b.buf).endswith(b"$GNGGA,1\r\n$GNGGA,2\r\n"))
        n = await fanout.disconnect_all_clients()
        self.assertEqual(n, 2)
        await asyncio.wait_for(asyncio.gather(ta, tb, return_exceptions=True), 1)
        self.assertTrue(ta.cancelled())
        self.assertTrue(tb.cancelled())
        await asyncio.wait_for(asyncio.gather(sa.writer_task, sb.writer_task,
                                              return_exceptions=True), 1)
        self.assertTrue(sa.writer_task.done())
        self.assertTrue(sb.writer_task.done())
        self.assertTrue(a.geschlossen and b.geschlossen)   # only wait_closed() sets it
        self.assertEqual((a.closes, b.closes), (1, 1))
        self.assertEqual(fanout.clients, [])
        self.assertEqual(fanout._sessions, {})
        self.assertEqual(state.app.stats["nmea_tcp_client_stats"], [])
        self.assertEqual(state.app.stats["tcp_write_timeouts"], 0)
        self.assertEqual(sa.stats["drops"], 2)             # in flight + queued
        self.assertEqual(sb.stats["drops"], 0)
        self.assertEqual(state.app.stats["nmea_tcp_drops"], 2)

    async def test_disconnect_all_keeps_the_list_object(self):
        before = fanout.clients
        await self.open_client(self.mp_writer(), ControlledReader(b"AUTH OLD\r\n"))
        await fanout.disconnect_all_clients()
        self.assertIs(fanout.clients, before)
        self.assertEqual(before, [])

    async def test_rotation_during_ok_drain_rejects_old_handshake(self):
        reader = ControlledReader(b"AUTH OLD\r\n")      # then blocks, no EOF
        w = self.mp_writer(block_drains={2})
        with mock.patch.object(fanout, "add_client", wraps=fanout.add_client) as spy:
            handler = self.start_handler(reader, w)
            await until(lambda: w.drains == 2)          # OK AUTH drain hangs
            state.app.identity["stream_token"] = "NEW"
            await fanout.disconnect_all_clients()
            w.gate.set()
            await until(lambda: handler.done() or w in fanout.clients)
            self.assertFalse([c for c in spy.call_args_list
                              if c.args and c.args[0] is w])
            self.assertNotIn(w, fanout.clients)
            await asyncio.wait_for(handler, 1)
        # The handshake itself succeeded; the epoch check rejected it.
        self.assertEqual(state.app.stats.get("nmea_tcp_auth_failures", 0), 0)
        self.assertTrue(w.geschlossen)
        self.assertEqual(fanout.pending_auth, 0)

    async def test_handshake_after_rotation_uses_new_token(self):
        await fanout.disconnect_all_clients()
        state.app.identity = {"stream_token": "NEW"}
        self.assertFalse(await fanout._authenticate_client(
            FakeReader(b"AUTH OLD\r\n"), FakeWriter()))
        self.assertTrue(await fanout._authenticate_client(
            FakeReader(b"AUTH NEW\r\n"), FakeWriter()))

    async def test_reader_eof_during_blocked_drain_closes_socket_and_writer(self):
        reader = ControlledReader(b"AUTH OLD\r\n")
        w = self.mp_writer(block_drains={3})
        task, session = await self.open_client(w, reader)
        await fanout.broadcast(b"$GNGGA,1\r\n")
        await until(lambda: w.drains == 3)
        reader.eof()
        await asyncio.wait_for(task, 1)                  # normal end, not cancelled
        await asyncio.wait_for(asyncio.gather(session.writer_task,
                                              return_exceptions=True), 1)
        self.assertTrue(session.writer_task.cancelled())
        self.assertTrue(w.geschlossen)
        self.assertEqual(fanout.clients, [])
        self.assertEqual(state.app.stats["tcp_write_timeouts"], 0)
        self.assertEqual(session.stats["drops"], 1)
        self.assertEqual(state.app.stats["nmea_tcp_drops"], 1)

    async def test_write_error_closes_socket_exactly_once(self):
        w = self.mp_writer()
        w.fail_drains = {3}                              # first NMEA payload fails
        task, session = await self.open_client(w, ControlledReader(b"AUTH OLD\r\n"))
        await fanout.broadcast(b"$GNGGA,1\r\n")
        await asyncio.wait_for(asyncio.gather(task, session.writer_task,
                                              return_exceptions=True), 1)
        self.assertTrue(task.cancelled())                # ended by the writer's removal
        self.assertEqual(w.closes, 1)
        self.assertEqual(state.app.stats["tcp_write_errors"], 1)
        self.assertEqual(fanout.clients, [])

    async def test_listener_restart_keeps_sessions_delivering_in_order(self):
        servers, handlers = [], []

        async def start_server(cb, host, port):
            handlers.append(cb)
            servers.append(FakeServer())
            return servers[-1]

        tick = asyncio.Event()
        fail = [False]

        async def fake_sleep(sec):
            if sec == 1:
                await tick.wait()
                tick.clear()
                if fail[0]:
                    fail[0] = False
                    raise RuntimeError("listener")
            else:
                await asyncio.sleep(0)

        # fanout.asyncio is the uasyncio stub module, not the asyncio used here.
        with mock.patch.object(fanout.asyncio, "start_server", start_server), \
                mock.patch.object(fanout.asyncio, "sleep", fake_sleep), \
                mock.patch.dict(fanout.CONFIG, {"nmea_tcp_port": 10110,
                                                "nmea_tcp_legacy_enabled": False}), \
                support.patch_module_global("state", "log"), \
                mock.patch.dict(state.app.stats, {"task_restarts": 0}):
            sup = asyncio.create_task(state.supervise(
                "nmea_tcp", fanout.nmea_tcp_task, restart_delay=0))
            self.tasks.append(sup)
            await until(lambda: len(handlers) == 1)
            a, b = self.mp_writer(), self.mp_writer()
            ta, sa = await self.open_client(a, ControlledReader(b"AUTH OLD\r\n"),
                                            handlers[0])
            await fanout.broadcast(b"one\r\n")
            await until(lambda: bytes(a.buf).endswith(b"one\r\n"))
            fail[0] = True
            tick.set()
            await until(lambda: len(handlers) == 2)
            self.assertTrue(servers[0].closed)
            self.assertEqual(state.app.stats["task_restarts"], 1)
            await fanout.broadcast(b"two\r\n")
            _tb, _sb = await self.open_client(b, ControlledReader(b"AUTH OLD\r\n"),
                                              handlers[1])
            await fanout.broadcast(b"three\r\n")
            await until(lambda: bytes(a.buf) == b"AUTH REQUIRED\r\nOK AUTH\r\n"
                        b"one\r\ntwo\r\nthree\r\n"
                        and bytes(b.buf) == b"AUTH REQUIRED\r\nOK AUTH\r\nthree\r\n")
            self.assertFalse(ta.done())
            self.assertFalse(sa.writer_task.done())
            self.assertEqual(fanout.clients, [a, b])
            sup.cancel()
            await asyncio.gather(sup, return_exceptions=True)
            await fanout.disconnect_all_clients()
            await asyncio.gather(*self.tasks, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
