# SPDX-License-Identifier: AGPL-3.0-only
"""Distribute validated NMEA sentences to independent BLE and TCP queues.

TCP authentication is enabled by default and may be disabled explicitly. A
client must therefore follow the current access contract instead of assuming
that the first bytes are always NMEA or always an AUTH greeting.
"""
import uasyncio as asyncio
import time

from cfg import CONFIG, log
from state import SimpleQueue, app, shutdown_event

# Open TCP clients. One list, no amount: StreamWriters aren't necessarily hashable, and
# more than a handful of clients never exist here.
clients = []
_reader_tasks = {}
pending_auth = 0
AUTH_TIMEOUT_SEC = 5
AUTH_MAX_BYTES = 96
# A single measured flash commit can take about 2.5 s. Keep a finite socket
# deadline that accommodates it; the bounded queue holds about 7 s of NMEA.
CLIENT_WRITE_TIMEOUT_SEC = 5
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


async def _authenticate_client(reader, writer):
    """Expects as the first line ``AUTH <stream_token>``."""
    try:
        writer.write(b"AUTH REQUIRED\r\n")
        await writer.drain()
        raw = bytearray()
        # Do not use readline(): on some MicroPython stream implementations it
        # allocates the complete attacker-controlled line before returning.
        while len(raw) <= AUTH_MAX_BYTES:
            chunk = await asyncio.wait_for(reader.read(1), AUTH_TIMEOUT_SEC)
            if not chunk:
                break
            raw.extend(chunk)
            if chunk == b"\n":
                break
    except Exception:
        return False
    if not raw or len(raw) > AUTH_MAX_BYTES or raw[-1:] != b"\n":
        return False
    line = bytes(raw).strip()
    supplied = line[5:] if line.startswith(b"AUTH ") else b""
    expected = (app.identity or {}).get("stream_token", "")
    if not expected or not _secret_equal(supplied, expected):
        app.stats["tcp_auth_failures"] = app.stats.get("tcp_auth_failures", 0) + 1
        writer.write(b"ERR AUTH\r\n")
        await writer.drain()
        return False
    writer.write(b"OK AUTH\r\n")
    await writer.drain()
    return True


def add_client(writer, reader_task=None):
    """Pick up a client when there is still space."""
    threshold = CONFIG.get("nmea_tcp_max_clients", 4)
    if len(clients) >= threshold:
        return False
    clients.append(writer)
    if reader_task is not None:
        _reader_tasks[id(writer)] = reader_task
    return True


def remove_client(writer):
    """Removes a client; double removal is harmless."""
    try:
        clients.remove(writer)
    except ValueError:
        pass
    task = _reader_tasks.pop(id(writer), None)
    if task is not None and task is not asyncio.current_task():
        # Cancelling a timed-out drain can also unregister the blocked reader
        # from MicroPython's IO poller. Do not leave that reader task orphaned.
        task.cancel()


async def _close_writer(writer):
    try:
        writer.close()
        # MicroPython Stream.close() is a no-op; wait_closed closes the socket.
        await asyncio.wait_for(writer.wait_closed(), CLIENT_WRITE_TIMEOUT_SEC)
    except Exception:
        pass


async def broadcast(data):
    """Send a sentence to all TCP clients. If you don't accept any more, you'll fly out - one dead client won't be allowed to stop the stream for the others. You'll iterate over a copy because it changes the list.
    """
    if not clients:
        return
    for writer in list(clients):
        started = time.ticks_ms()
        try:
            writer.write(data)
            await asyncio.wait_for(writer.drain(), CLIENT_WRITE_TIMEOUT_SEC)
            app.stats["tcp_tx_bytes"] = app.stats.get("tcp_tx_bytes", 0) + len(data)
        except Exception as error:
            remove_client(writer)
            app.stats["nmea_tcp_drops"] += 1
            timeout = isinstance(error, asyncio.TimeoutError)
            key = "tcp_write_timeouts" if timeout else "tcp_write_errors"
            app.stats[key] = app.stats.get(key, 0) + 1
            app.stats["tcp_last_write_error"] = type(error).__name__
            log("WARN", "GNSS", "NMEA-TCP send failed (%s); closing client." % type(error).__name__)
            await _close_writer(writer)
        finally:
            app.stats["tcp_write_max_ms"] = max(app.stats.get("tcp_write_max_ms", 0),
                max(0, time.ticks_diff(time.ticks_ms(), started)))


async def _handle_client(reader, writer):
    """Optionally authenticates a TCP client before sending the NMEA stream."""
    global pending_auth
    try:
        peer = writer.get_extra_info("peername")
    except Exception:
        peer = None

    threshold = int(CONFIG.get("nmea_tcp_max_clients", 4))
    auth_required = bool(CONFIG.get("nmea_tcp_auth_required", True))
    if auth_required and pending_auth >= threshold:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass
        return
    if auth_required:
        pending_auth += 1
        try:
            authenticated = await _authenticate_client(reader, writer)
        finally:
            pending_auth -= 1
    else:
        authenticated = True

    if not authenticated:
        app.stats["nmea_tcp_auth_failures"] = (
            app.stats.get("nmea_tcp_auth_failures", 0) + 1)
        log("WARN", "GNSS", "NMEA-TCP: Authentication rejected (%s)." % (peer,))
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass
        return

    if not add_client(writer, asyncio.current_task()):
        log("WARN", "GNSS", "NMEA-TCP: too many clients; rejecting %s" % (peer,))
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass
        return

    log("INFO", "GNSS", "NMEA-TCP: Client %s connected (%d open)."
        % (peer, len(clients)))
    try:
        # Keep open until the opposite side leaves. Read only to notice the connection
        # end.
        while not shutdown_event.is_set():
            data = await reader.read(64)
            if not data:
                break
    except Exception:
        pass
    finally:
        remove_client(writer)
        log("INFO", "GNSS", "NMEA-TCP: Client %s disconnected (%d open)."
            % (peer, len(clients)))
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


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
    except Exception as e:
        log("ERROR", "GNSS", "NMEA-TCP-server failed.",
            print_traceback=True, exception=e)
        app.log_error("NMEA_TCP", str(e))
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
