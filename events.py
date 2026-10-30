# SPDX-License-Identifier: AGPL-3.0-only
"""Server-sent events for the live state: frame encoding and bounded writes (#27).

Wire contract of GET /api/events. The answer is HTTP/1.0 with Connection: close
(HEADER), followed by PREAMBLE (the browser reconnect delay and a comment line).
Every frame is "event: <name>\\ndata: <json>\\n\\n". The names are live (exactly the
/api/ui/live snapshot), heartbeat ({"t": uptime_sec}, only after HEARTBEAT_SEC
without another write) and bye ({"reason": "session"|"evicted"|"disabled"}).

Nothing here may wait for a client without a deadline: StreamWriter.write() only
buffers and drain() sends, and wait_closed() drops unsent bytes. drain() therefore
takes a timeout and passes every failure on to the caller, which counts timeouts
and treats a vanished client (OSError) as the normal end of a stream. flush() is the
bounded drain for answers that are not streams (401, 503, HEAD).

EventHub runs one tick every TICK_MS. When a live frame is due it builds the snapshot
once, serialises it into one bytes object and puts that same object into the
one-frame slot of every subscriber (a newer frame replaces an unwritten one). The hub
never writes to a socket: each stream is written by its own connection coroutine.
"""
import gc
import time

import uasyncio as asyncio
import ujson

from cfg_logging import HOT_PATH_LOG_INTERVAL_MS, LogThrottle
from cfg import log
from state import app, shutdown_event

MAX_STREAMS = 3            # active subscribers; the next one evicts the oldest
MAX_OPEN_STREAMS = 4       # open streams including evicted ones that are still closing
WRITE_TIMEOUT_SEC = 5      # one write budget per session check interval
HEARTBEAT_SEC = 10         # silence on a stream before a heartbeat
TICK_MS = 200              # hub cadence
LIVE_INTERVAL_MS = 1000    # a live frame at least this often
LIVE_MIN_INTERVAL_MS = 400  # a changed fix does not send faster than this
SESSION_CHECK_MS = 1000    # the portal session is rechecked at least this often
# The largest live frame at the worst case of the snapshot contract (measured 3748 B
# plus about 150 B of planned additions), including the frame lines.
LIVE_FRAME_BUDGET_BYTES = 4608

HEADER = (b"HTTP/1.0 200 OK\r\nContent-Type: text/event-stream; charset=utf-8\r\n"
          b"Cache-Control: no-store\r\nConnection: close\r\n"
          b"X-Content-Type-Options: nosniff\r\n\r\n")
PREAMBLE = b"retry: 2000\n:ok\n\n"


def frame(name, payload):
    """One event as bytes. A line break in name or payload would split the event."""
    if "\n" in name or "\r" in name or "\n" in payload or "\r" in payload:
        raise ValueError("line break in event")
    return ("event: " + name + "\ndata: " + payload + "\n\n").encode()


def bye_frame(reason):
    return frame("bye", '{"reason": "' + reason + '"}')


def heartbeat_frame(uptime_sec):
    return frame("heartbeat", '{"t": ' + str(int(uptime_sec)) + "}")


async def drain(writer, timeout_sec):
    """Drain within timeout_sec; raises asyncio.TimeoutError, OSError and everything else (#27 E8)."""
    if timeout_sec <= 0:
        raise asyncio.TimeoutError
    await asyncio.wait_for(writer.drain(), timeout_sec)


async def flush(writer):
    """Drain a non-stream answer within WRITE_TIMEOUT_SEC; False on timeout or a vanished client."""
    try:
        await drain(writer, WRITE_TIMEOUT_SEC)
        return True
    except (asyncio.TimeoutError, OSError):
        return False


_STREAM_FAILURE_LOG = LogThrottle(HOT_PATH_LOG_INTERVAL_MS)


def note_failure(error):
    """Count and log one event stream failure; never raises (#27 E9)."""
    try:
        if isinstance(error, asyncio.TimeoutError):
            app.stats["sse_write_timeouts"] += 1
            log("DEBUG", "SSE", "Event stream write timed out.")
        elif isinstance(error, OSError):
            return  # the client went away
        else:
            app.stats["sse_stream_errors"] += 1
            if isinstance(error, MemoryError):
                gc.collect()
            if _STREAM_FAILURE_LOG.hit():
                log("ERROR", "SSE", "Event stream failed.")
    except Exception:
        pass


def _budget(started):
    # The rest of the write budget opened by the session check at started.
    return WRITE_TIMEOUT_SEC - time.ticks_diff(time.ticks_ms(), started) / 1000


class Subscriber:
    """One admitted stream: a one-frame live slot, a pending bye and its wake-up event."""

    def __init__(self, session_ok):
        self.live = None          # the newest unwritten live frame (bytes), shared
        self.bye = None           # "session" | "evicted" | "disabled": end with this frame
        self.event = asyncio.Event()
        self.session_ok = session_ok
        self.released = False


class EventHub:
    """Admission of event streams and the live frame cadence (#27 E2, E5, E6).

    subs holds the active subscribers, oldest first. open counts every reservation
    until its stream is released, including evicted streams that are still closing.
    """

    def __init__(self):
        self.subs = []
        self.open = 0
        self._force = False       # a new subscriber waits for a frame
        self._live_at = None      # ticks of the last live frame; None = never
        self._live_version = None  # fix version of the last live frame
        self._snapshot_log = LogThrottle(HOT_PATH_LOG_INTERVAL_MS)

    def _count(self):
        app.stats["sse_streams"] = len(self.subs)
        app.stats["sse_open"] = self.open

    def reserve(self, session_ok):
        """A new subscriber, or None when streams are disabled or too many are open."""
        stats = app.stats
        if MAX_STREAMS <= 0 or self.open >= MAX_OPEN_STREAMS:
            stats["sse_refusals"] += 1
            return None
        while len(self.subs) >= MAX_STREAMS:
            self._end(self.subs[0], "evicted")
        sub = Subscriber(session_ok)
        self.subs.append(sub)
        self.open += 1
        self._force = True
        self._count()
        return sub

    def release(self, sub):
        """Give a reservation back; idempotent and never raises."""
        if sub.released:
            return
        sub.released = True
        if sub in self.subs:
            self.subs.remove(sub)
        self.open -= 1
        self._count()

    def _end(self, sub, reason):
        # The stream stays open until its serve() releases it.
        self.subs.remove(sub)
        sub.bye = reason
        sub.event.set()
        if reason == "evicted":
            app.stats["sse_evictions"] += 1

    def tick(self, now_ms, live_source, version_source):
        """One hub step: trim the subscribers, then share one live frame when due."""
        subs = self.subs
        if len(subs) > MAX_STREAMS:
            reason = "disabled" if MAX_STREAMS <= 0 else "evicted"
            while subs and len(subs) > MAX_STREAMS:
                self._end(subs[0], reason)
            self._count()
        if not subs:
            return
        version = version_source()
        last = self._live_at
        if not self._force and last is not None:
            since = time.ticks_diff(now_ms, last)
            if since < LIVE_INTERVAL_MS and (version == self._live_version
                                             or since < LIVE_MIN_INTERVAL_MS):
                return
        stats = app.stats
        try:
            body = frame("live", ujson.dumps(live_source(now_ms)))
        except Exception as error:
            stats["sse_snapshot_errors"] += 1
            body = None
            try:
                # Reporting may fail under the same memory pressure; it must
                # never stop the hub for every subscriber.
                if isinstance(error, MemoryError):
                    gc.collect()
                if self._snapshot_log.hit():
                    log("ERROR", "SSE", "Live snapshot failed: " + type(error).__name__)
            except Exception:
                pass
        if body is not None:
            self._force = False
            self._live_at = now_ms
            self._live_version = version
            for sub in subs:
                sub.live = body
                sub.event.set()
            if len(body) > stats["sse_frame_bytes_max"]:
                stats["sse_frame_bytes_max"] = len(body)
        took = time.ticks_diff(time.ticks_ms(), now_ms)
        if took > stats["sse_tick_max_ms"]:
            stats["sse_tick_max_ms"] = took

    async def serve(self, writer, sub):
        """Write one admitted stream until it ends; never raises (#27 E7-E9).

        The session is checked before every write once SESSION_CHECK_MS has passed,
        with or without frames. A passed check opens a write budget of
        WRITE_TIMEOUT_SEC that every write until the next passed check shares,
        the terminal bye included. At most one write per wake-up.
        """
        try:
            started = last_check = last_write = time.ticks_ms()   # admission was the first check
            writer.write(HEADER)
            writer.write(PREAMBLE)
            await drain(writer, _budget(started))
            while not shutdown_event.is_set():
                wait_ms = SESSION_CHECK_MS - time.ticks_diff(time.ticks_ms(), last_check)
                if wait_ms > 0 and sub.bye is None and sub.live is None:
                    try:
                        await asyncio.wait_for(sub.event.wait(), wait_ms / 1000)   # seconds
                    except asyncio.TimeoutError:
                        pass  # the session timer, not a write timeout
                sub.event.clear()
                now = time.ticks_ms()
                if time.ticks_diff(now, last_check) >= SESSION_CHECK_MS:   # due check before every write
                    last_check = now
                    if sub.session_ok():
                        started = now   # a passed check opens the next budget
                    elif sub.bye is None:
                        sub.bye = "session"
                if sub.bye is not None:
                    writer.write(bye_frame(sub.bye))
                    await drain(writer, _budget(started))   # the rest of the same budget
                    return
                data, sub.live = sub.live, None
                if data is None and time.ticks_diff(now, last_write) >= HEARTBEAT_SEC * 1000:
                    data = heartbeat_frame(app.uptime_seconds())
                if data is not None:
                    writer.write(data)
                    await drain(writer, _budget(started))
                    app.stats["sse_frames"] += 1
                    last_write = time.ticks_ms()
        except Exception as error:
            note_failure(error)
        finally:
            self.release(sub)

    async def run(self, live_source, version_source):
        """The hub task: one tick every TICK_MS until shutdown."""
        while not shutdown_event.is_set():
            self.tick(time.ticks_ms(), live_source, version_source)
            app.beat("sse")
            await asyncio.sleep_ms(TICK_MS)


hub = EventHub()
