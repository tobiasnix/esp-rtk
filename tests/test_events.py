# SPDX-License-Identifier: AGPL-3.0-only
"""Event stream building blocks (#27): frame encoding, bounded drain and flush, frame budget,
the event hub (reservations, cadence, conflated slots) and the stream loop.

BufferedWriter is the shared test writer of the event stream tests: like the real
StreamWriter it only counts bytes as transferred when drain() delivers them, and
every drain can be delayed, blocked or made to fail.
"""
import asyncio
import inspect
import json
import os
import sys
import time
import unittest
from unittest import mock

from support import cfg, main, state, web
import ujson

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import events
import push
from test_live_snapshot import WORST_ASCII_DEFAULTS, contract_env, normalised


class BufferedWriter:
    """A writer that transfers bytes only on drain().

    block: an asyncio.Event; while it is set every drain waits.
    raise_on_drain: an exception raised by every drain (None = no failure).
    drain_script: {n: seconds | "block" | exception} for the n-th drain (1-based);
        a number waits that long and then delivers, "block" never delivers (waits
        until the test cancels it), an exception instance is raised.
    fail_write_at: the n-th write() call raises MemoryError.
    """

    def __init__(self):
        self.pending = bytearray()
        self.sent = bytearray()
        self.block = asyncio.Event()
        self.raise_on_drain = None
        self.drain_script = {}
        self.fail_write_at = None
        self.write_calls = 0
        self.drain_calls = 0
        self.drain_started = []
        self.closed = False
        self.pending_at_close = None

    def write(self, data):
        self.write_calls += 1
        if self.fail_write_at is not None and self.write_calls == self.fail_write_at:
            raise MemoryError
        self.pending += data

    async def drain(self):
        self.drain_calls += 1
        self.drain_started.append(time.monotonic())
        step = self.drain_script.get(self.drain_calls)
        if isinstance(step, BaseException):
            raise step
        if step == "block":
            await asyncio.Event().wait()
        elif step is not None:
            await asyncio.sleep(step)
        else:
            if self.raise_on_drain is not None:
                raise self.raise_on_drain
            while self.block.is_set():
                await asyncio.sleep(0.005)
        self.sent += self.pending
        self.pending = bytearray()

    def close(self):
        self.closed = True
        self.pending_at_close = bytes(self.pending)

    async def wait_closed(self):
        pass

    def get_extra_info(self, key):
        return ("192.168.4.2", 5000) if key == "peername" else None


def _data_of(frame_bytes):
    """The text between 'data: ' and the closing blank line of one frame."""
    text = frame_bytes.decode()
    return text.split("data: ", 1)[1][:-2]


class FrameEncodingTests(unittest.TestCase):
    def test_header_and_preamble_bytes(self):
        self.assertEqual(events.HEADER,
                         b"HTTP/1.0 200 OK\r\nContent-Type: text/event-stream; "
                         b"charset=utf-8\r\nCache-Control: no-store\r\n"
                         b"Connection: close\r\nX-Content-Type-Options: nosniff\r\n\r\n")
        self.assertEqual(events.PREAMBLE, b"retry: 2000\n:ok\n\n")

    def test_frame_bytes(self):
        self.assertEqual(events.frame("live", '{"a": 1}'),
                         b'event: live\ndata: {"a": 1}\n\n')
        self.assertEqual(events.bye_frame("session"),
                         b'event: bye\ndata: {"reason": "session"}\n\n')
        self.assertEqual(events.heartbeat_frame(42),
                         b'event: heartbeat\ndata: {"t": 42}\n\n')
        for value in (events.frame("live", "{}"), events.bye_frame("evicted"),
                      events.heartbeat_frame(0)):
            self.assertIs(type(value), bytes)

    def test_limits_match_the_contract(self):
        self.assertEqual(events.MAX_STREAMS, 3)
        self.assertEqual(events.MAX_OPEN_STREAMS, 4)
        self.assertEqual(events.WRITE_TIMEOUT_SEC, 5)
        self.assertEqual(events.HEARTBEAT_SEC, 10)
        self.assertEqual(events.TICK_MS, 200)
        self.assertEqual(events.LIVE_INTERVAL_MS, 1000)
        self.assertEqual(events.LIVE_MIN_INTERVAL_MS, 400)
        self.assertEqual(events.SESSION_CHECK_MS, 1000)

    def test_frame_rejects_line_breaks(self):
        for name, payload in (("live", "a\nb"), ("live", "a\rb"), ("li\nve", "{}"),
                              ("li\rve", "{}")):
            with self.subTest(name=name, payload=payload):
                with self.assertRaises(ValueError):
                    events.frame(name, payload)

    def test_live_frame_is_the_ui_live_json(self):
        with contract_env() as now:
            snapshot = web.ui_live_snapshot(now)
            body = events.frame("live", ujson.dumps(snapshot))
            self.assertTrue(body.startswith(b"event: live\ndata: "))
            self.assertTrue(body.endswith(b"\n\n"))
            self.assertEqual(json.loads(_data_of(body)),
                             normalised(web.ui_live_snapshot(now)))
            # A line break in a device-controlled text never splits the data line.
            state.app.identity = dict(state.app.identity, display_name="Rover\n1\r2")
            body = events.frame("live", ujson.dumps(web.ui_live_snapshot(now)))
            lines = body.decode().split("\n")
            self.assertEqual([line for line in lines if line.startswith("data:")],
                             [lines[1]])
            self.assertEqual(json.loads(_data_of(body))["system"]["display_name"],
                             "Rover\n1\r2")

    def test_live_frame_fits_its_budget(self):
        with contract_env() as now, mock.patch.dict(web.CONFIG, WORST_ASCII_DEFAULTS):
            body = events.frame("live", ujson.dumps(web.ui_live_snapshot(now)))
        self.assertLessEqual(len(body), events.LIVE_FRAME_BUDGET_BYTES)
        self.assertEqual(events.LIVE_FRAME_BUDGET_BYTES, 4608)

    def test_events_is_a_board_file(self):
        self.assertIn("events.py", push.BOARD_FILES)
        self.assertLess(push.BOARD_FILES.index("events.py"),
                        push.BOARD_FILES.index("web.py"))


class DrainAndFlushTests(unittest.TestCase):
    def test_drain_raises_timeout_and_oserror(self):
        async def blocked():
            writer = BufferedWriter()
            writer.block.set()
            writer.pending += b"x"
            started = time.monotonic()
            with self.assertRaises(asyncio.TimeoutError):
                await events.drain(writer, 0.05)
            self.assertLess(time.monotonic() - started, 0.2)
            self.assertEqual(writer.sent, b"")

        async def reset():
            writer = BufferedWriter()
            writer.raise_on_drain = OSError(104)
            with self.assertRaises(OSError) as caught:
                await events.drain(writer, 1)
            # Under CPython TimeoutError is an OSError: the reverse must not hold.
            self.assertIs(type(caught.exception), OSError)

        async def expired():
            for budget in (0, -1):
                writer = BufferedWriter()
                with self.assertRaises(asyncio.TimeoutError):
                    await events.drain(writer, budget)
                self.assertEqual(writer.drain_calls, 0)

        async def delivered():
            writer = BufferedWriter()
            writer.write(b"abc")
            self.assertIsNone(await events.drain(writer, 1))
            self.assertEqual(writer.sent, b"abc")
            self.assertEqual(writer.pending, b"")

        for scenario in (blocked, reset, expired, delivered):
            with self.subTest(scenario=scenario.__name__):
                asyncio.run(scenario())

    def test_flush_moves_bytes_and_is_bounded(self):
        async def run():
            writer = BufferedWriter()
            writer.write(b"abc")
            self.assertIs(await events.flush(writer), True)
            self.assertEqual(writer.sent, b"abc")

            stuck = BufferedWriter()
            stuck.block.set()
            stuck.write(b"abc")
            started = time.monotonic()
            with mock.patch.object(events, "WRITE_TIMEOUT_SEC", 0.05):
                self.assertIs(await events.flush(stuck), False)
            self.assertLess(time.monotonic() - started, 0.2)
            self.assertEqual(stuck.sent, b"")

            gone = BufferedWriter()
            gone.raise_on_drain = OSError(104)
            self.assertIs(await events.flush(gone), False)

            broken = BufferedWriter()
            broken.raise_on_drain = MemoryError()
            with self.assertRaises(MemoryError):
                await events.flush(broken)
        asyncio.run(run())


class BufferedWriterTests(unittest.TestCase):
    def test_buffered_writer_follows_its_drain_script(self):
        async def run():
            writer = BufferedWriter()
            writer.drain_script = {1: 0.03, 2: OSError(104)}
            writer.write(b"a")
            started = time.monotonic()
            await writer.drain()
            self.assertGreaterEqual(time.monotonic() - started, 0.03)
            self.assertEqual(writer.sent, b"a")
            writer.write(b"b")
            with self.assertRaises(OSError):
                await writer.drain()
            self.assertEqual(writer.sent, b"a")
            await writer.drain()
            self.assertEqual(writer.sent, b"ab")
            self.assertEqual(len(writer.drain_started), 3)
            self.assertEqual(writer.drain_calls, 3)
        asyncio.run(run())

    def test_buffered_writer_blocks_and_fails_writes_on_demand(self):
        async def run():
            writer = BufferedWriter()
            writer.drain_script = {1: "block"}
            writer.write(b"a")
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(writer.drain(), 0.03)
            self.assertEqual(writer.sent, b"")

            failing = BufferedWriter()
            failing.fail_write_at = 2
            failing.write(b"a")
            with self.assertRaises(MemoryError):
                failing.write(b"b")
            failing.close()
            self.assertTrue(failing.closed)
            self.assertEqual(failing.pending_at_close, b"a")
            self.assertEqual(failing.get_extra_info("peername"), ("192.168.4.2", 5000))
        asyncio.run(run())


GGA = {"utc": "120000.00", "lat": 50.0, "lon": 8.0, "alt": 100.0, "qual": 4, "sats": 20,
       "hdop": 0.6, "fix_status_text": "RTK_FIXED", "lat_e8": 5000000000, "lon_e8": 800000000,
       "alt_mm": 100000}


class Sources:
    """Counting stubs for the hub's live and version sources."""

    def __init__(self):
        self.live_calls = 0
        self.version_calls = 0
        self.version = (0, 0)
        self.error = None

    def live(self, now_ms):
        self.live_calls += 1
        if self.error is not None:
            raise self.error
        return {"n": self.live_calls, "now_ms": now_ms}

    def fix_version(self):
        self.version_calls += 1
        return self.version


class TestEventHub(unittest.TestCase):
    def setUp(self):
        stats = mock.patch.dict(state.app.stats)
        stats.start()
        self.addCleanup(stats.stop)
        self.hub = events.EventHub()
        self.src = Sources()

    def tick(self, now_ms):
        self.hub.tick(now_ms, self.src.live, self.src.fix_version)

    def reserve(self):
        return self.hub.reserve(lambda: True)

    def frames_at(self, sub, times):
        """The tick times at which sub received a live frame."""
        sent = []
        for now in times:
            self.tick(now)
            if sub.live is not None:
                sent.append(now)
                sub.live = None
        return sent

    def test_no_subscriber_builds_nothing(self):
        self.tick(0)
        self.assertEqual(self.src.live_calls, 0)
        self.assertEqual(self.src.version_calls, 0)

    def test_one_serialisation_shared_by_all(self):
        subs = [self.reserve() for _ in range(3)]
        self.tick(0)
        self.assertEqual(self.src.live_calls, 1)
        self.assertIs(type(subs[0].live), bytes)
        self.assertIs(subs[0].live, subs[1].live)
        self.assertIs(subs[1].live, subs[2].live)
        self.assertTrue(all(sub.event.is_set() for sub in subs))
        self.assertEqual(json.loads(_data_of(subs[0].live)), {"n": 1, "now_ms": 0})
        self.assertEqual(state.app.stats["sse_frame_bytes_max"], len(subs[0].live))

    def test_live_cadence(self):
        sub = self.reserve()
        self.assertEqual(self.frames_at(sub, range(0, 1001, 200)), [0, 1000])

        self.hub, self.src = events.EventHub(), Sources()
        sub = self.reserve()
        self.assertEqual(self.frames_at(sub, [0]), [0])
        self.src.version = (1, 0)
        self.assertEqual(self.frames_at(sub, [200]), [])     # changed, but < 400 ms
        self.assertEqual(self.frames_at(sub, [400]), [400])
        self.assertEqual(self.frames_at(sub, [600]), [])     # unchanged since 400
        self.src.version = (1, 1)
        self.assertEqual(self.frames_at(sub, [800]), [800])

    def test_cadence_survives_tick_wrap(self):
        wrap = lambda a, b: ((a - b + 2 ** 29) % 2 ** 30) - 2 ** 29
        sub = self.reserve()
        with mock.patch.object(events.time, "ticks_diff", wrap):
            sent = self.frames_at(sub, [2 ** 30 - 400, 2 ** 30 - 200, 0, 200, 600,
                                        800, 1000])
        self.assertEqual(sent, [2 ** 30 - 400, 600])

    def test_new_subscriber_forces_a_frame(self):
        first = self.reserve()
        self.tick(0)
        first.live = None
        second = self.reserve()
        self.tick(100)
        self.assertIsNotNone(first.live)
        self.assertIsNotNone(second.live)
        self.assertIs(first.live, second.live)
        self.assertEqual(self.src.live_calls, 2)

    def test_rejoin_after_a_long_pause(self):
        sub = self.reserve()
        self.tick(0)
        self.hub.release(sub)
        for now in range(200, 600_001, 200):
            self.tick(now)
        self.assertEqual(self.src.live_calls, 1)
        sub = self.reserve()
        times = range(600_200, 601_401, 200)
        self.assertEqual(self.frames_at(sub, times), [600_200, 601_200])

    def test_slots_conflate(self):
        sub = self.reserve()
        self.tick(0)
        first = sub.live
        self.tick(1000)
        self.assertIsNot(sub.live, first)
        self.assertEqual(json.loads(_data_of(sub.live)), {"n": 2, "now_ms": 1000})
        self.assertFalse(hasattr(sub, "progress"))

    def test_fourth_stream_evicts_the_oldest(self):
        subs = [self.reserve() for _ in range(4)]
        self.assertEqual(self.hub.subs, subs[1:])
        self.assertEqual(subs[0].bye, "evicted")
        self.assertTrue(subs[0].event.is_set())
        self.assertTrue(all(sub.bye is None for sub in subs[1:]))
        stats = state.app.stats
        self.assertEqual(stats["sse_evictions"], 1)
        self.assertEqual(stats["sse_streams"], 3)
        self.assertEqual(self.hub.open, 4)
        self.assertEqual(stats["sse_open"], 4)

    def test_reserve_counts_evicted_streams_until_released(self):
        subs = [self.reserve() for _ in range(4)]
        stats = state.app.stats
        self.assertIsNone(self.reserve())
        self.assertEqual(stats["sse_refusals"], 1)
        self.assertEqual(self.hub.open, 4)
        self.hub.release(subs[0])
        self.assertEqual((self.hub.open, stats["sse_open"]), (3, 3))
        sixth = self.reserve()
        self.assertIsNotNone(sixth)
        self.assertEqual(self.hub.open, 4)
        self.hub.release(subs[0])
        self.assertEqual(self.hub.open, 4)
        self.hub.release(sixth)
        self.hub.release(sixth)
        self.assertEqual((self.hub.open, stats["sse_open"]), (3, 3))
        self.assertNotIn(sixth, self.hub.subs)
        self.assertEqual(stats["sse_streams"], 2)

    def test_kill_switch_disables_running_streams(self):
        subs = [self.reserve() for _ in range(3)]
        with mock.patch.object(events, "MAX_STREAMS", 0):
            self.tick(0)
            self.assertEqual([sub.bye for sub in subs], ["disabled"] * 3)
            self.assertTrue(all(sub.event.is_set() for sub in subs))
            self.assertEqual(self.hub.subs, [])
            self.assertEqual(state.app.stats["sse_streams"], 0)
            self.assertEqual(self.src.live_calls, 0)
            self.assertIsNone(self.reserve())
        self.assertEqual(self.hub.open, 3)

    def test_snapshot_error_is_counted_and_the_hub_survives(self):
        sub = self.reserve()
        self.src.error = MemoryError()
        with mock.patch.object(events, "log") as log, \
                mock.patch.object(events.gc, "collect") as collect:
            self.tick(0)
            self.assertIsNone(sub.live)
            self.assertFalse(sub.event.is_set())
            self.assertEqual(state.app.stats["sse_snapshot_errors"], 1)
            self.tick(200)
            self.assertEqual(state.app.stats["sse_snapshot_errors"], 2)
            errors = [c for c in log.call_args_list if c.args[0] == "ERROR"]
            self.assertEqual(len(errors), 1)
            self.assertEqual(collect.call_count, 2)
            self.src.error = None
            self.tick(400)
        self.assertIsNotNone(sub.live)
        self.assertEqual(state.app.stats["sse_snapshot_errors"], 2)

    def test_snapshot_error_reporting_never_stops_the_hub(self):
        # Under memory pressure the throttle and the logger may fail too; the
        # hub must contain that and recover on the next good tick.
        sub = self.reserve()
        self.src.error = MemoryError()
        with mock.patch.object(events, "log", side_effect=MemoryError()), \
                mock.patch.object(self.hub._snapshot_log, "hit", side_effect=MemoryError()):
            self.tick(0)
            self.assertEqual(state.app.stats["sse_snapshot_errors"], 1)
        with mock.patch.object(events, "log", side_effect=MemoryError()):
            self.tick(200)
            self.assertEqual(state.app.stats["sse_snapshot_errors"], 2)
        self.src.error = None
        self.tick(400)
        self.assertIsNotNone(sub.live)

    def test_tick_time_is_recorded(self):
        self.reserve()
        with mock.patch.object(events.time, "ticks_ms", lambda: 1007):
            self.tick(1000)
        self.assertEqual(state.app.stats["sse_tick_max_ms"], 7)
        with mock.patch.object(events.time, "ticks_ms", lambda: 1203):
            self.tick(1200)                                    # nothing due: no work
        self.assertEqual(state.app.stats["sse_tick_max_ms"], 7)

    def test_run_beats_every_tick_and_stops_on_shutdown(self):
        sleeps = []

        async def sleep_ms(ms):
            sleeps.append(ms)
            if len(sleeps) == 3:
                state.shutdown_event.set()

        self.addCleanup(state.shutdown_event.clear)
        self.reserve()
        with mock.patch.object(events.asyncio, "sleep_ms", sleep_ms), \
                mock.patch.dict(state.app.heartbeat, clear=True):
            asyncio.run(self.hub.run(self.src.live, self.src.fix_version))
            self.assertIn("sse", state.app.heartbeat)
        self.assertEqual(sleeps, [events.TICK_MS] * 3)
        self.assertEqual(self.src.version_calls, 3)

    def test_fix_version_tracks_seq_and_rev(self):
        app = state.AppState()
        now = [10_000]
        with mock.patch.object(time, "ticks_ms", lambda: now[0]), \
                mock.patch("tracking.tracker") as tracker:
            tracker.enqueue_fix.return_value = False
            self.assertEqual(app.fix_version(), (0, 0))
            now[0] += 1000
            app.update_fix(dict(GGA))
            seq, rev = app.fix_version()
            self.assertEqual(seq, 1)
            now[0] += 400
            self.assertTrue(app.attach_gst({"utc": "120000.00", "horizontal_sigma_m": 0.01}))
            self.assertEqual(app.fix_version(), (seq, rev + 1))
            self.assertIs(type(app.fix_version()), tuple)

    def test_hub_never_touches_a_writer(self):
        source = inspect.getsource(events.EventHub.tick)
        self.assertNotIn("write(", source)
        self.assertNotIn("drain(", source)

    def test_events_has_no_progress_channel(self):
        self.assertEqual(list(inspect.signature(events.EventHub.tick).parameters),
                         ["self", "now_ms", "live_source", "version_source"])
        self.assertEqual(list(inspect.signature(events.EventHub.run).parameters),
                         ["self", "live_source", "version_source"])
        self.assertNotIn('frame("progress"', inspect.getsource(events))
        self.assertIsInstance(events.hub, events.EventHub)



class Session:
    """A counting session_ok stub.

    valid(elapsed) decides each answer from the seconds since the stub was made;
    every call is recorded as (elapsed, answer, writes so far on writer).
    """

    def __init__(self, valid=None, writer=None):
        self.valid = valid or (lambda elapsed: True)
        self.writer = writer
        self.start = time.monotonic()
        self.calls = []

    def __call__(self):
        elapsed = time.monotonic() - self.start
        answer = self.valid(elapsed)
        writes = self.writer.write_calls if self.writer is not None else None
        self.calls.append((elapsed, answer, writes))
        return answer


class TestEventStream(unittest.TestCase):
    """EventHub.serve on a real event loop and the real clock, scaled down like the device
    (write budget much longer than the session check interval)."""

    def setUp(self):
        for name, value in (("WRITE_TIMEOUT_SEC", 0.25), ("HEARTBEAT_SEC", 0.1),
                            ("SESSION_CHECK_MS", 50)):
            patcher = mock.patch.object(events, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        stats = mock.patch.dict(state.app.stats)
        stats.start()
        self.addCleanup(stats.stop)
        logger = mock.patch.object(events, "log")
        self.log = logger.start()
        self.addCleanup(logger.stop)
        self.hub = events.EventHub()
        self.src = Sources()

    def stat(self, name):
        return state.app.stats[name]

    def error_lines(self):
        return [c for c in self.log.call_args_list if c.args[0] == "ERROR"]

    def tick(self, offset_ms=0):
        self.hub.tick(time.ticks_ms() + offset_ms, self.src.live, self.src.fix_version)

    def serve(self, writer, sub):
        return asyncio.ensure_future(self.hub.serve(writer, sub))

    async def finish(self, task, within):
        """Wait for serve to end by itself; returns the monotonic end time."""
        await asyncio.wait_for(asyncio.shield(task), within)
        self.assertIsNone(task.result())
        return time.monotonic()

    async def stop(self, task):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    def test_stream_starts_with_header_and_preamble(self):
        async def run():
            writer = BufferedWriter()
            task = self.serve(writer, self.hub.reserve(Session()))
            await asyncio.sleep(0.02)
            self.assertTrue(bytes(writer.sent).startswith(events.HEADER + events.PREAMBLE))
            await self.stop(task)
        asyncio.run(run())

    def test_stream_writes_only_the_latest_live_frame(self):
        async def run():
            writer = BufferedWriter()
            task = self.serve(writer, self.hub.reserve(Session()))
            await asyncio.sleep(0.02)
            self.tick()
            self.tick(1000)
            await asyncio.sleep(0.02)
            await self.stop(task)
            text = bytes(writer.sent).decode()
            self.assertEqual(text.count("event: live\n"), 1)
            body = text.split("event: live\n", 1)[1].split("\n\n", 1)[0]
            self.assertEqual(json.loads(body[len("data: "):])["n"], 2)
            self.assertEqual(self.stat("sse_frames"), 1)
            self.assertNotIn("event: progress", text)
        asyncio.run(run())

    def test_session_timer_waits_in_seconds(self):
        async def run():
            writer = BufferedWriter()
            session = Session()
            with mock.patch.object(events.asyncio, "wait_for",
                                   wraps=events.asyncio.wait_for) as wait_for:
                task = self.serve(writer, self.hub.reserve(session))
                await asyncio.sleep(0.3)
                await self.stop(task)
            limit = max(events.SESSION_CHECK_MS / 1000, events.WRITE_TIMEOUT_SEC)
            timeouts = [c.args[1] for c in wait_for.call_args_list]
            self.assertTrue(timeouts)
            self.assertTrue(all(value <= limit for value in timeouts), timeouts)
            # Milliseconds passed as seconds would leave no check after the start.
            self.assertGreaterEqual(len(session.calls), 0.3 / 0.05 - 2)
        asyncio.run(run())

    def test_heartbeat_after_ten_quiet_seconds(self):
        async def run():
            writer = BufferedWriter()
            task = self.serve(writer, self.hub.reserve(Session()))
            await asyncio.sleep(0.04)
            self.assertEqual(bytes(writer.sent), events.HEADER + events.PREAMBLE)
            await asyncio.sleep(0.16)
            await self.stop(task)
            text = bytes(writer.sent).decode()
            self.assertIn("event: heartbeat\n", text)
            self.assertNotIn("event: live", text)
        asyncio.run(run())

    def test_session_loss_without_frames_says_bye_within_the_interval(self):
        async def run():
            writer = BufferedWriter()
            session = Session(lambda elapsed: elapsed < 0.02)
            task = self.serve(writer, self.hub.reserve(session))
            ended = await self.finish(task, 0.5)
            self.assertLessEqual(ended - (session.start + 0.02),
                                 2 * events.SESSION_CHECK_MS / 1000)
            self.assertTrue(bytes(writer.sent).endswith(events.bye_frame("session")))
            self.assertEqual(bytes(writer.pending), b"")
            self.assertEqual(self.hub.open, 0)
        asyncio.run(run())

    def test_session_is_checked_at_most_once_per_interval(self):
        async def run():
            writer = BufferedWriter()
            session = Session()
            sub = self.hub.reserve(session)
            task = self.serve(writer, sub)
            started = time.monotonic()
            while time.monotonic() - started < 0.3:
                self.tick()
                sub.event.set()
                await asyncio.sleep(0.01)
            await self.stop(task)
            self.assertLessEqual(len(session.calls), 0.3 / 0.05 + 2)
            self.assertGreaterEqual(len(session.calls), 0.3 / 0.05 - 2)
        asyncio.run(run())

    def test_session_check_survives_tick_wrap(self):
        period = 2 ** 30
        origin = time.monotonic()
        clock = lambda: (period - 20 + int((time.monotonic() - origin) * 1000)) % period
        wrap = lambda a, b: ((a - b + period // 2) % period) - period // 2

        async def run():
            writer = BufferedWriter()
            ticks_at_check = []
            session = Session(lambda elapsed: ticks_at_check.append(clock()) or True)
            task = self.serve(writer, self.hub.reserve(session))
            await asyncio.sleep(0.3)
            await self.stop(task)
            return ticks_at_check

        with mock.patch.object(events.time, "ticks_ms", clock), \
                mock.patch.object(events.time, "ticks_diff", wrap):
            checks = asyncio.run(run())
        self.assertGreaterEqual(len(checks), 0.3 / 0.05 - 2)
        self.assertLessEqual(len(checks), 0.3 / 0.05 + 2)
        self.assertTrue(all(value < period // 2 for value in checks), checks)

    def test_evicted_stream_says_bye(self):
        async def run():
            with mock.patch.object(events, "MAX_STREAMS", 1):
                writer = BufferedWriter()
                task = self.serve(writer, self.hub.reserve(Session()))
                await asyncio.sleep(0.02)
                self.assertIsNotNone(self.hub.reserve(Session()))
                await self.finish(task, 0.1)
            self.assertTrue(bytes(writer.sent).endswith(events.bye_frame("evicted")))
            self.assertEqual(bytes(writer.pending), b"")
            self.assertEqual(self.hub.open, 1)
        asyncio.run(run())

    def test_terminal_bye_is_flushed(self):
        async def run():
            writer = BufferedWriter()
            task = self.serve(writer, self.hub.reserve(Session()))
            await asyncio.sleep(0.02)
            with mock.patch.object(events, "MAX_STREAMS", 0):
                self.tick()
            self.assertFalse(task.done())
            await self.finish(task, 0.1)
            self.assertTrue(bytes(writer.sent).endswith(events.bye_frame("disabled")))
            self.assertEqual(self.hub.open, 0)
        asyncio.run(run())

    def test_hanging_drain_ends_only_that_stream(self):
        async def run():
            stuck, reader = BufferedWriter(), BufferedWriter()
            stuck_task = self.serve(stuck, self.hub.reserve(Session()))
            reader_task = self.serve(reader, self.hub.reserve(Session()))
            await asyncio.sleep(0.02)
            stuck.block.set()
            self.tick()
            ended = await self.finish(stuck_task, 0.5)
            self.assertLessEqual(ended - stuck.drain_started[-1],
                                 events.WRITE_TIMEOUT_SEC + 0.05)
            self.assertEqual(self.stat("sse_write_timeouts"), 1)
            self.tick(1000)
            await asyncio.sleep(0.02)
            self.assertEqual(bytes(reader.sent).count(b"event: live\n"), 2)
            self.assertEqual(len(self.hub.subs), 1)
            self.assertEqual(self.hub.open, 1)
            await self.stop(reader_task)
        asyncio.run(run())

    def test_client_gone_ends_quietly(self):
        async def run():
            writer = BufferedWriter()
            writer.raise_on_drain = OSError(104)
            await self.finish(self.serve(writer, self.hub.reserve(Session())), 0.1)
            self.assertEqual(self.stat("sse_write_timeouts"), 0)
            self.assertEqual(self.stat("sse_stream_errors"), 0)
            self.assertEqual(self.hub.open, 0)
            self.assertEqual(self.error_lines(), [])
        asyncio.run(run())

    def test_initial_flush_timeout_is_counted(self):
        async def run():
            writer = BufferedWriter()
            writer.drain_script = {1: "block"}
            started = time.monotonic()
            ended = await self.finish(self.serve(writer, self.hub.reserve(Session())), 0.5)
            self.assertLessEqual(ended - started, events.WRITE_TIMEOUT_SEC + 0.05)
            self.assertEqual(self.stat("sse_write_timeouts"), 1)
            self.assertEqual(self.stat("sse_stream_errors"), 0)
            self.assertEqual(writer.drain_calls, 1)
            self.assertEqual(self.hub.open, 0)
        asyncio.run(run())

    def test_initial_flush_client_gone_is_quiet(self):
        async def run():
            writer = BufferedWriter()
            writer.drain_script = {1: OSError(104)}
            await self.finish(self.serve(writer, self.hub.reserve(Session())), 0.1)
            self.assertEqual(self.stat("sse_write_timeouts"), 0)
            self.assertEqual(self.stat("sse_stream_errors"), 0)
            self.assertEqual(self.error_lines(), [])
            self.assertEqual(writer.drain_calls, 1)
            self.assertEqual(self.hub.open, 0)
        asyncio.run(run())

    def evict_during_terminal_flush(self, step):
        """Evict a stream after its header flush; its bye drain follows step."""
        async def run():
            with mock.patch.object(events, "MAX_STREAMS", 1), \
                    mock.patch.object(events, "HEARTBEAT_SEC", 10):
                writer = BufferedWriter()
                task = self.serve(writer, self.hub.reserve(Session()))
                await asyncio.sleep(0.02)
                self.assertEqual(writer.drain_calls, 1)
                writer.drain_script = {2: step}
                evicted = time.monotonic()
                self.assertIsNotNone(self.hub.reserve(Session()))
                ended = await self.finish(task, 0.5)
            return writer, ended - evicted
        return asyncio.run(run())

    def test_terminal_flush_timeout_is_counted(self):
        writer, took = self.evict_during_terminal_flush("block")
        self.assertLessEqual(took, events.WRITE_TIMEOUT_SEC + 0.05)
        self.assertEqual(self.stat("sse_write_timeouts"), 1)
        self.assertEqual(self.stat("sse_stream_errors"), 0)
        self.assertIn(events.bye_frame("evicted"), bytes(writer.pending))

    def test_terminal_flush_client_gone_is_quiet(self):
        writer, _took = self.evict_during_terminal_flush(OSError(104))
        self.assertEqual(self.stat("sse_write_timeouts"), 0)
        self.assertEqual(self.stat("sse_stream_errors"), 0)
        self.assertEqual(self.error_lines(), [])
        self.assertEqual(self.hub.open, 1)

    def test_one_budget_spans_consecutive_drains(self):
        budget = events.WRITE_TIMEOUT_SEC

        async def run():
            with mock.patch.object(events, "HEARTBEAT_SEC", 10):
                writer = BufferedWriter()
                writer.drain_script = {2: budget - 0.03, 3: "block"}
                session = Session(writer=writer)
                sub = self.hub.reserve(session)
                task = self.serve(writer, sub)
                await asyncio.sleep(0.01)
                self.assertEqual(writer.drain_calls, 1)
                sub.live = events.frame("live", "{}")
                sub.event.set()
                await asyncio.sleep(0.01)
                self.assertEqual(writer.drain_calls, 2)
                logout = writer.drain_started[1] + 0.01
                session.valid = lambda elapsed: time.monotonic() < logout
                ended = await self.finish(task, 1)
            self.assertLessEqual(ended, writer.drain_started[1] + budget + 0.05)
            self.assertEqual(self.stat("sse_write_timeouts"), 1)
            # The failed check ran after header, preamble and live frame, before the bye.
            self.assertEqual(session.calls[-1][1:], (False, 3))
            self.assertEqual(writer.write_calls, 4)
            self.assertIn(b'event: live\ndata: {}\n\n', bytes(writer.sent))
            self.assertEqual(bytes(writer.pending), events.bye_frame("session"))
            self.assertNotIn(b"event: bye", bytes(writer.sent))
            self.assertEqual(self.hub.open, 0)
        asyncio.run(run())

    def test_logout_during_the_initial_header_flush_ends_within_the_budget(self):
        budget = events.WRITE_TIMEOUT_SEC

        async def run():
            writer = BufferedWriter()
            writer.drain_script = {1: budget - 0.03, 2: "block"}
            sub = self.hub.reserve(Session(lambda elapsed: elapsed < 0.01))
            started = time.monotonic()
            task = self.serve(writer, sub)
            await asyncio.sleep(0.02)
            sub.live = events.frame("live", "{}")       # never written: the bye goes first
            sub.event.set()
            ended = await self.finish(task, 1)
            self.assertLessEqual(ended - started, budget + 0.05)
            self.assertTrue(bytes(writer.sent).startswith(events.HEADER + events.PREAMBLE))
            self.assertNotIn(b"event: live", bytes(writer.sent))
            self.assertNotIn(b"event: live", bytes(writer.pending))
            self.assertEqual(self.stat("sse_write_timeouts"), 1)
            self.assertEqual(self.hub.open, 0)
        asyncio.run(run())

    def test_passed_check_opens_a_new_budget(self):
        budget = events.WRITE_TIMEOUT_SEC

        async def run():
            writer = BufferedWriter()
            writer.drain_script = {2: budget - 0.03, 3: budget - 0.03}
            sub = self.hub.reserve(Session())
            task = self.serve(writer, sub)
            await asyncio.sleep(0.01)
            sub.live = events.frame("live", '{"n": 1}')
            sub.event.set()
            await asyncio.sleep(0.02)
            self.assertEqual(writer.drain_calls, 2)
            sub.live = events.frame("live", '{"n": 2}')   # set during the first drain
            sub.event.set()
            await asyncio.sleep(2 * budget)
            self.assertFalse(task.done())
            await self.stop(task)
            sent = bytes(writer.sent)
            self.assertIn(b'event: live\ndata: {"n": 1}\n\n', sent)
            self.assertIn(b'event: live\ndata: {"n": 2}\n\n', sent)
            self.assertEqual(self.stat("sse_write_timeouts"), 0)
            self.assertEqual(self.stat("sse_frames"), 2)
        asyncio.run(run())

    def test_note_failure_never_raises(self):
        with mock.patch.object(events, "log", side_effect=RuntimeError):
            for error in (MemoryError(), ValueError(), asyncio.TimeoutError()):
                self.assertIsNone(events.note_failure(error))
        state.app.stats["sse_write_timeouts"] = state.app.stats["sse_stream_errors"] = 0
        with mock.patch.object(events.gc, "collect") as collect:
            events.note_failure(asyncio.TimeoutError())
            self.assertEqual(self.stat("sse_write_timeouts"), 1)
            events.note_failure(OSError(104))
            self.assertEqual((self.stat("sse_write_timeouts"),
                              self.stat("sse_stream_errors")), (1, 0))
            collect.assert_not_called()
            events.note_failure(MemoryError())
            self.assertEqual(self.stat("sse_stream_errors"), 1)
            collect.assert_called_once_with()

    def test_unexpected_write_error_closes_without_a_drain(self):
        async def run():
            writer = BufferedWriter()
            writer.fail_write_at = 2
            writer.block.set()
            started = time.monotonic()
            # gc.collect() on the host heap of the full suite can take longer than 20 ms.
            with mock.patch.object(events.gc, "collect") as collect:
                ended = await self.finish(self.serve(writer, self.hub.reserve(Session())),
                                          0.1)
            self.assertLess(ended - started, 0.02)
            collect.assert_called_once_with()
            self.assertEqual(writer.drain_calls, 0)
            self.assertEqual(bytes(writer.pending), events.HEADER)
            self.assertEqual(self.stat("sse_stream_errors"), 1)
            self.assertEqual(self.hub.open, 0)
        asyncio.run(run())

    def test_shutdown_ends_the_stream(self):
        self.addCleanup(state.shutdown_event.clear)

        async def run():
            writer = BufferedWriter()
            sub = self.hub.reserve(Session())
            task = self.serve(writer, sub)
            await asyncio.sleep(0.02)
            state.shutdown_event.set()
            sub.event.set()
            await self.finish(task, 0.1)
            self.assertEqual(self.hub.open, 0)
        asyncio.run(run())

class TestEventTask(unittest.TestCase):
    """The hub runs as task sse in both modes without being a watched task (#27 A5)."""

    def test_sse_task_runs_in_both_modes(self):
        self.assertEqual(inspect.getsource(main.main).count('supervise("sse"'), 2)
        self.assertNotIn("sse", cfg.WATCHED_TASKS)

    def test_hanging_stream_keeps_the_hub_beating(self):
        stats = mock.patch.dict(state.app.stats)
        stats.start()
        self.addCleanup(stats.stop)
        for name, value in (("TICK_MS", 20), ("WRITE_TIMEOUT_SEC", 0.1)):
            patcher = mock.patch.object(events, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(state.shutdown_event.clear)
        hub = events.EventHub()
        src = Sources()

        async def run():
            writer = BufferedWriter()
            writer.block.set()   # the reader never takes a byte
            sub = hub.reserve(lambda: True)
            hub_task = asyncio.ensure_future(hub.run(src.live, src.fix_version))
            stream = asyncio.ensure_future(hub.serve(writer, sub))
            started = time.monotonic()
            await asyncio.wait_for(asyncio.shield(stream), 0.2)
            self.assertLess(time.monotonic() - started, 0.2)
            self.assertEqual(hub.open, 0)
            await asyncio.sleep(max(0, 0.5 - (time.monotonic() - started)))
            age = time.ticks_diff(time.ticks_ms(), state.app.heartbeat["sse"])
            self.assertLessEqual(age, 2 * events.TICK_MS)
            self.assertIsNone(state.stale_heartbeat(0.1))
            state.shutdown_event.set()
            await asyncio.wait_for(hub_task, 0.2)

        with mock.patch.dict(state.app.heartbeat, clear=True):
            asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
