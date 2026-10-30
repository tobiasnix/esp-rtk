# SPDX-License-Identifier: AGPL-3.0-only
"""Distribute validated NMEA sentences to independent BLE and TCP queues.

TCP authentication is enabled by default and may be disabled explicitly. A
client must therefore follow the current access contract instead of assuming
that the first bytes are always NMEA or always an AUTH greeting.

Each TCP client has its own bounded queue and writer task, so one stalled
socket cannot delay the others.
"""
import uasyncio as asyncio
import time

from cfg import CONFIG, log
from cfg_logging import HOT_PATH_LOG_INTERVAL_MS, LogThrottle, throttle_note
from state import SimpleQueue, app, shutdown_event

# Open TCP clients. One list, no amount: StreamWriters aren't necessarily hashable, and
# more than a handful of clients never exist here.
clients = []
# Delivery state of each open client (_ClientSession), keyed by id(writer).
_sessions = {}
_next_client_id = 0
pending_auth = 0
# Bumped by every stream-token rotation; a handshake that started in an older
# epoch was checked against the old token and is not admitted.
_rotation_epoch = 0
AUTH_TIMEOUT_SEC = 5
AUTH_MAX_BYTES = 96
# Whole handshake incl. the server's drains, from connection start.
AUTH_DEADLINE_SEC = 10
# A single measured flash commit can take about 2.5 s. Keep a finite socket
# deadline that accommodates it; each client queue holds about 7 s of NMEA;
# a single drain must finish within this deadline.
CLIENT_WRITE_TIMEOUT_SEC = 5
# Per-client queue bound, whichever limit is reached first. A full queue drops
# the new payload (tail drop); payloads are shared references, not copies.
CLIENT_QUEUE_ITEMS = 100
CLIENT_QUEUE_BYTES = 8192
TCP_BATCH_BYTES = 2048
TCP_BATCH_SENTENCES = 16
# Match the UART parser's bounded catch-up turns. TCP can hold sixteen normal
# NMEA sentences in one write, including sentences longer than 64 bytes.
NMEA_WORK_BATCH_SENTENCES = 16


def _secret_equal(left, right):
    """Comparison without premature termination in the case of a wrong sign."""
    left = left if isinstance(left, bytes) else str(left or "").encode()
    right = right if isinstance(right, bytes) else str(right or "").encode()
    difference = len(left) ^ len(right)
    for index in range(max(len(left), len(right))):
        a = left[index] if index < len(left) else 0
        b = right[index] if index < len(right) else 0
        difference |= a ^ b
    return difference == 0


def _max_clients():
    """Shared limit for open AUTH handshakes and established clients."""
    return int(CONFIG.get("nmea_tcp_max_clients", 4))


def _auth_budget(started_ms, cap_sec):
    """Seconds left for the next handshake step, at most cap_sec.

    Raises asyncio.TimeoutError once AUTH_DEADLINE_SEC since started_ms is used up.
    """
    left = AUTH_DEADLINE_SEC - time.ticks_diff(time.ticks_ms(), started_ms) / 1000
    budget = min(cap_sec, left)
    if budget <= 0:
        raise asyncio.TimeoutError()
    return budget


async def _authenticate_client(reader, writer, started_ms=None):
    """Expects as the first line ``AUTH <stream_token>``.

    Every step, the server's drains included, shares one AUTH_DEADLINE_SEC
    budget counted from started_ms (default: now).
    """
    started = time.ticks_ms() if started_ms is None else started_ms
    try:
        writer.write(b"AUTH REQUIRED\r\n")
        await asyncio.wait_for(writer.drain(), _auth_budget(started, AUTH_DEADLINE_SEC))
        raw = bytearray()
        # Do not use readline(): on some MicroPython stream implementations it
        # allocates the complete attacker-controlled line before returning.
        while len(raw) <= AUTH_MAX_BYTES:
            chunk = await asyncio.wait_for(reader.read(1),
                                           _auth_budget(started, AUTH_TIMEOUT_SEC))
            if not chunk:
                break
            raw.extend(chunk)
            if chunk == b"\n":
                break
        if not raw or len(raw) > AUTH_MAX_BYTES or raw[-1:] != b"\n":
            return False
        line = bytes(raw).strip()
        supplied = line[5:] if line.startswith(b"AUTH ") else b""
        expected = (app.identity or {}).get("stream_token", "")
        if not expected or not _secret_equal(supplied, expected):
            app.stats["tcp_auth_failures"] = app.stats.get("tcp_auth_failures", 0) + 1
            writer.write(b"ERR AUTH\r\n")
            await asyncio.wait_for(writer.drain(),
                                   _auth_budget(started, AUTH_DEADLINE_SEC))
            return False
        writer.write(b"OK AUTH\r\n")
        await asyncio.wait_for(writer.drain(), _auth_budget(started, AUTH_DEADLINE_SEC))
        return True
    except Exception:
        return False


def _sentences(data):
    """Number of NMEA sentences in a payload; a fragment counts as one."""
    return data.count(b"\n") or 1


class _ClientSession:
    """Bounded output queue and writer task of one TCP client.

    The reader task (the connection handler, which only watches for the
    connection end) and the writer task always end together: cancelling one
    waiter makes MicroPython's IO poller drop the whole socket, so a leftover
    partner task would wait forever.
    """

    def __init__(self, writer, reader_task, client_id):
        self.writer = writer
        self.reader_task = reader_task
        self.writer_task = None
        self.items = []
        self.queued_bytes = 0
        # Payload taken by the writer but not yet confirmed by drain().
        self.inflight = None
        self.ready = asyncio.Event()
        self.stats = {"id": client_id, "drops": 0, "queue_max": 0}
        # Set by remove_client: the writer ends even if its cancel was lost.
        self.closed = False

    def offer(self, data):
        """Queue a payload for this client; False means it was dropped."""
        if (len(self.items) >= CLIENT_QUEUE_ITEMS
                or self.queued_bytes + len(data) > CLIENT_QUEUE_BYTES):
            self.count_drops(_sentences(data))
            app.stats["tcp_queue_overflows"] = app.stats.get("tcp_queue_overflows", 0) + 1
            return False
        self.items.append(data)
        self.queued_bytes += len(data)
        depth = len(self.items)
        self.stats["queue_max"] = max(self.stats["queue_max"], depth)
        app.stats["tcp_client_queue_max"] = max(
            app.stats.get("tcp_client_queue_max", 0), depth)
        self.ready.set()
        return True

    def take(self):
        """Next payload in order (kept as in-flight), or None when empty."""
        if not self.items:
            return None
        # pop(0) is cheap enough here: the queue never exceeds CLIENT_QUEUE_ITEMS.
        data = self.items.pop(0)
        self.queued_bytes -= len(data)
        self.inflight = data
        return data

    def count_drops(self, sentences):
        self.stats["drops"] += sentences
        app.stats["nmea_tcp_drops"] += sentences

    def discard(self):
        """Drop the in-flight and queued payloads, counting their sentences."""
        lost = 0
        if self.inflight is not None:
            lost += _sentences(self.inflight)
        for data in self.items:
            lost += _sentences(data)
        self.inflight = None
        self.items = []
        self.queued_bytes = 0
        if lost:
            self.count_drops(lost)
        return lost

    def start(self):
        self.writer_task = asyncio.create_task(_client_writer(self))

    def cancel_tasks(self):
        """Cancel reader and writer task, except the task calling this."""
        tasks = [task for task in (self.reader_task, self.writer_task)
                 if task is not None]
        if not tasks:
            return
        try:
            current = asyncio.current_task()
        except RuntimeError:
            # Called outside a task (MicroPython raises here, CPython without a loop).
            current = None
        for task in tasks:
            if task is not current:
                task.cancel()


def add_client(writer, reader_task=None):
    """Create a session when there is still space; its writer is not started yet."""
    global _next_client_id
    if len(clients) >= _max_clients():
        return None
    _next_client_id += 1
    session = _ClientSession(writer, reader_task, _next_client_id)
    clients.append(writer)
    _sessions[id(writer)] = session
    app.stats.setdefault("nmea_tcp_client_stats", []).append(session.stats)
    return session


def remove_client(writer):
    """Remove a client and end its session; double removal is harmless.

    Returns True when this call ended a session. Undelivered payloads count
    as drops; reader and writer task are cancelled together (see
    _ClientSession).
    """
    try:
        clients.remove(writer)
    except ValueError:
        pass
    session = _sessions.pop(id(writer), None)
    if session is None:
        return False
    session.closed = True
    # Wakes a writer in ready.wait() so it sees the flag even without a cancel.
    session.ready.set()
    session.discard()
    try:
        app.stats["nmea_tcp_client_stats"].remove(session.stats)
    except (KeyError, ValueError):
        pass
    session.cancel_tasks()
    return True


async def _close_writer(writer):
    try:
        writer.close()
        # MicroPython Stream.close() is a no-op; wait_closed closes the socket.
        await asyncio.wait_for(writer.wait_closed(), CLIENT_WRITE_TIMEOUT_SEC)
    except Exception:
        pass


async def disconnect_all_clients():
    """End every TCP session and close its socket (stream-token rotation).

    Returns the number of disconnected clients. Handshakes still in progress
    are not admitted afterwards (see _rotation_epoch).
    """
    global _rotation_epoch
    _rotation_epoch += 1
    # Copy: remove_client changes clients in place, which is never rebound.
    writers = list(clients)
    for writer in writers:
        remove_client(writer)
    for writer in writers:
        await _close_writer(writer)
    return len(writers)


_SEND_FAILURE_LOG = LogThrottle(HOT_PATH_LOG_INTERVAL_MS)


def _log_send_failure(error_name):
    # Throttled: dead clients can fail every sentence; counts are in /status (tcp_write_errors).
    n = _SEND_FAILURE_LOG.hit(error_name)
    if n:
        log("WARN", "GNSS", "NMEA-TCP send failed (%s); closing client.%s"
            % (error_name, throttle_note(n)))


async def broadcast(data):
    """Queue a payload for every TCP client without waiting for any socket.

    All clients share the same bytes object; each client's writer task sends
    it, so a stalled client only loses its own copy.
    """
    for writer in clients:
        session = _sessions.get(id(writer))
        if session is not None:
            session.offer(data)


async def _deliver(session, data):
    """Write one payload to the client; on failure the client is removed and closed."""
    writer = session.writer
    started = time.ticks_ms()
    try:
        writer.write(data)
        await asyncio.wait_for(writer.drain(), CLIENT_WRITE_TIMEOUT_SEC)
        session.inflight = None
        app.stats["tcp_tx_bytes"] = app.stats.get("tcp_tx_bytes", 0) + len(data)
        return True
    except Exception as error:
        # Counts the in-flight payload and the queued ones as drops.
        remove_client(writer)
        timeout = isinstance(error, asyncio.TimeoutError)
        key = "tcp_write_timeouts" if timeout else "tcp_write_errors"
        app.stats[key] = app.stats.get(key, 0) + 1
        app.stats["tcp_last_write_error"] = type(error).__name__
        _log_send_failure(type(error).__name__)
        await _close_writer(writer)
        return False
    finally:
        app.stats["tcp_write_max_ms"] = max(app.stats.get("tcp_write_max_ms", 0),
            max(0, time.ticks_diff(time.ticks_ms(), started)))


async def _client_writer(session):
    """Send one client's queued payloads in order until it fails or is removed.

    The closed flag ends the loop on the next wake even when remove_client's
    cancel was swallowed (uasyncio's wait_for can lose a cancellation that
    arrives as its inner task completes).
    """
    try:
        while not shutdown_event.is_set() and not session.closed:
            data = session.take()
            if data is None:
                session.ready.clear()
                await session.ready.wait()
                continue
            if not await _deliver(session, data):
                break
    except Exception as error:
        # MicroPython would only print an unhandled task error; end the session instead.
        _log_send_failure(type(error).__name__)
    finally:
        if remove_client(session.writer):
            await _close_writer(session.writer)


async def _handle_client(reader, writer):
    """Optionally authenticates a TCP client before sending the NMEA stream.

    The socket is closed on every path, cancellation included.
    """
    global pending_auth
    started = time.ticks_ms()
    epoch = _rotation_epoch
    try:
        peer = writer.get_extra_info("peername")
    except Exception:
        peer = None

    # Open handshakes and established clients share one limit. Reject without
    # any bytes or log line, so a connection flood cannot fill the log.
    if pending_auth + len(clients) >= _max_clients():
        app.stats["nmea_tcp_rejected_full"] = (
            app.stats.get("nmea_tcp_rejected_full", 0) + 1)
        await _close_writer(writer)
        return

    admitted = False
    try:
        if CONFIG.get("nmea_tcp_auth_required", True):
            pending_auth += 1
            try:
                authenticated = await _authenticate_client(reader, writer, started)
            finally:
                pending_auth -= 1
        else:
            authenticated = True

        if not authenticated:
            app.stats["nmea_tcp_auth_failures"] = (
                app.stats.get("nmea_tcp_auth_failures", 0) + 1)
            log("WARN", "GNSS", "NMEA-TCP: Authentication rejected (%s)." % (peer,))
            return
        if epoch != _rotation_epoch:
            log("INFO", "GNSS",
                "NMEA-TCP: stream token rotated during handshake; closing %s." % (peer,))
            return

        # No await between pending_auth -= 1 and add_client: the sum stays in the limit.
        session = add_client(writer, asyncio.current_task())
        if session is None:
            log("WARN", "GNSS", "NMEA-TCP: too many clients; rejecting %s" % (peer,))
            return
        admitted = True
        session.start()

        log("INFO", "GNSS", "NMEA-TCP: Client %s connected as #%d (%d open)."
            % (peer, session.stats["id"], len(clients)))
        try:
            # Keep open until the opposite side leaves. Read only to notice the
            # connection end.
            while not shutdown_event.is_set():
                data = await reader.read(64)
                if not data:
                    break
        except Exception:
            pass
    finally:
        removed = remove_client(writer) if admitted else False
        if admitted:
            log("INFO", "GNSS", "NMEA-TCP: Client %s disconnected (%d open)."
                % (peer, len(clients)))
        # Whoever ended the session (writer failure, rotation) closes the socket.
        if not admitted or removed:
            await _close_writer(writer)


def nmea_tcp_ports():
    """Deliver canonical and optional legacy ports without duplicates."""
    ports = []
    canonical = int(CONFIG.get("nmea_tcp_port", 0))
    if canonical:
        ports.append(canonical)
    if CONFIG.get("nmea_tcp_legacy_enabled", True):
        legacy = int(CONFIG.get("nmea_tcp_legacy_port", 0))
        if legacy and legacy not in ports:
            ports.append(legacy)
    return ports


async def nmea_tcp_task():
    """Emit NMEA electricity on standard and legacy ports."""
    ports = nmea_tcp_ports()
    if not ports:
        log("INFO", "GNSS", "NMEA-TCP disabled (nmea_tcp_port=0).")
        return

    servers = []
    # No except: a server failure must reach supervise(), which logs, counts and
    # restarts; a normal return would leave the service down.
    try:
        for port in ports:
            try:
                srv = await asyncio.start_server(_handle_client, "0.0.0.0", port)
                servers.append((port, srv))
            except Exception as e:
                if port == int(CONFIG.get("nmea_tcp_port", 0)):
                    raise
                log("WARN", "GNSS", "Legacy-NMEA-Port %d unavailable: %s"
                    % (port, e))
        aktive_ports = [port for port, _srv in servers]
        log("INFO", "GNSS", "NMEA-TCP listening on port(s) %s."
            % ", ".join(str(p) for p in aktive_ports))
        while not shutdown_event.is_set():
            await asyncio.sleep(1)
    finally:
        for _port, srv in servers:
            try:
                srv.close()
                await srv.wait_closed()
            except Exception:
                pass
    log("INFO", "GNSS", "NMEA-TCP task stopped.")


class NmeaSenderTask:
    """Route input into independent bounded BLE and TCP output queues.

    A slow radio or socket must not hold up GNSS UART consumption or the other
    transport. Each output records its own drops when its queue is full.
    """

    def __init__(self, ble_mgr, nmea_queue):
        self.ble = ble_mgr
        self.queue = nmea_queue
        self.ble_queue = SimpleQueue(maxsize=100)
        self.tcp_queue = SimpleQueue(maxsize=100)

    def route(self, data):
        if not self.ble_queue.put_nowait(data):
            app.stats["ble_drops"] += 1
            app.stats["ble_queue_overflows"] = app.stats.get("ble_queue_overflows", 0) + 1
        if not self.tcp_queue.put_nowait(data):
            app.stats["nmea_tcp_drops"] += 1
            app.stats["tcp_queue_overflows"] = app.stats.get("tcp_queue_overflows", 0) + 1
        for name, queue in (("ble", self.ble_queue), ("tcp", self.tcp_queue)):
            key = name + "_queue_max"
            app.stats[key] = max(app.stats.get(key, 0), queue.qsize())

    async def run(self):
        log("INFO", "GNSS", "NMEA fan-out router started.")
        app.beat("ble")
        while not shutdown_event.is_set():
            data = await self.queue.get()
            app.beat("ble")

            if data is None:
                continue
            self.route(data)
            self.queue.task_done()
            for _ in range(NMEA_WORK_BATCH_SENTENCES - 1):
                if not self.queue.qsize():
                    break
                data = await self.queue.get()
                if data is None:
                    break
                self.route(data)
                self.queue.task_done()
            await asyncio.sleep_ms(0)
        log("INFO", "GNSS", "NMEA fan-out router stopped.")

    async def run_ble(self):
        log("INFO", "BLE", "BLE output worker started.")
        while not shutdown_event.is_set():
            data = await self.ble_queue.get()
            if data is not None:
                await self.ble.send(data)
                self.ble_queue.task_done()
                for _ in range(NMEA_WORK_BATCH_SENTENCES - 1):
                    if not self.ble_queue.qsize():
                        break
                    data = await self.ble_queue.get()
                    if data is None:
                        break
                    # BLE backpressure still yields/retries the same chunk.
                    await self.ble.send(data)
                    self.ble_queue.task_done()
                # Also yield for sentences with no subscribed BLE clients.
                await asyncio.sleep_ms(0)
        log("INFO", "BLE", "BLE output worker stopped.")

    async def run_tcp(self):
        log("INFO", "GNSS", "TCP output worker started.")
        pending = None
        while not shutdown_event.is_set():
            data = pending if pending is not None else await self.tcp_queue.get()
            pending = None
            if data is not None:
                batch, size = [data], len(data)
                # Drain an existing burst with one bounded write, preserving
                # every complete sentence and limiting cooperative-task work.
                while self.tcp_queue.qsize() and len(batch) < TCP_BATCH_SENTENCES:
                    item = await self.tcp_queue.get()
                    if item is None:
                        break
                    if size + len(item) > TCP_BATCH_BYTES:
                        pending = item
                        break
                    batch.append(item); size += len(item)
                await broadcast(b"".join(batch))
                for _ in batch:
                    self.tcp_queue.task_done()
                await asyncio.sleep_ms(0)
        log("INFO", "GNSS", "TCP output worker stopped.")
