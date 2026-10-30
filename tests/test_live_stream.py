# SPDX-License-Identifier: AGPL-3.0-only
"""#27 part B: one fix source streams live frames from /api/events or leaves the caller polling.

The source, the arrival (acceptLive), pollLive and startPolling run under Node straight from the single-line
declarations of app.js, with a test clock, test timers and a test EventSource.
"""
import json
import os
import unittest

from test_navigation import multi_line_function, run_app_node

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(ROOT, "app.js"), encoding="utf-8") as handle:
    APP_JS = handle.read()
APP_LINES = APP_JS.split("\n")

# The freshness helpers of src/25-fix-gate.js and the fix source of src/91-live-stream.js.
BASE_PREFIXES = ("let staleSinceSeq=", "function fixIsUsable(", "function ingestFreshness(", "const LIVE_DELAYED_MS=",
                 "function noteLiveAnswer(", "function noteLiveAttempt(", "function liveAgeMs(", "function liveLinkState(",
                 "function liveView(", "const LIVE_STREAM_URL=", "function createFixSource(")
ROUTER_PREFIXES = ("const liveBindings=", "function renderLiveBindings(", "function acceptLive(", "async function pollLive(")
POLLING_PREFIXES = ("const MEASURE_ROUTE=", "function livePollIntervalMs(", "function startPolling(", "const liveSource=")

# Test clock and timers, a test EventSource and the app globals the declarations above read.
STUBS = r"""
const clock = {now: 0, seq: 0, timers: new Map(), after: null};
const performance = {now: () => clock.now};
const addTimer = every => (fn, ms = 0) => {
  const id = ++clock.seq, wait = Math.max(0, Number(ms) || 0);
  clock.timers.set(id, {at: clock.now + wait, fn, ms: wait, every: every ? Math.max(1, wait) : null});
  return id;
};
const setTimeout = addTimer(false), setInterval = addTimer(true);
const clearTimeout = id => { clock.timers.delete(id); }, clearInterval = clearTimeout;
const settle = async () => { for (let i = 0; i < 6; i++) await new Promise(resolve => setImmediate(resolve)); };
// Runs every timer due within ms in time order; clock.after (if set) runs after each one.
async function advance(ms) {
  const end = clock.now + ms;
  for (;;) {
    await settle();
    let next = null;
    for (const [id, item] of clock.timers) if (item.at <= end && (!next || item.at < next[1].at)) next = [id, item];
    if (!next) break;
    const [id, item] = next;
    clock.now = item.at;
    if (item.every) item.at += item.every; else clock.timers.delete(id);
    item.fn();
    if (clock.after) clock.after();
  }
  clock.now = end;
  await settle();
}
const intervals = () => [...clock.timers.values()].filter(item => item.every).length;
const timeouts = ms => [...clock.timers.values()].filter(item => !item.every && item.ms === ms).length;
class FakeEventSource {
  constructor(url) { Object.assign(this, {url, listeners: {}, readyState: 0, closed: false}); FakeEventSource.instances.push(this); }
  addEventListener(name, fn) { (this.listeners[name] = this.listeners[name] || []).push(fn); }
  emit(name, data) {
    this.readyState = 1;
    for (const fn of this.listeners[name] || []) fn({type: name, data: typeof data === "string" ? data : JSON.stringify(data)});
  }
  fail(readyState) { this.readyState = readyState; for (const fn of this.listeners.error || []) fn({type: "error"}); }
  close() { this.closed = true; this.readyState = 2; }
}
FakeEventSource.instances = [];
const openStreams = () => FakeEventSource.instances.filter(item => !item.closed);
let requestSerial = 0, pollBusy = false, measurementRequestActive = false, activeController = null, pollTimer = 0;
let connectionState = {label: "Connecting", state: "warn"}, hold = false;
const restartHoldActive = () => hold;
const document = {hidden: false};
const navigator = {onLine: true};
const calls = {restart: 0, accept: [], poll: [], connection: [], stamped: []};
const store = {state: {live: null}, set(patch) { Object.assign(this.state, patch); }};
let route = {path: "measure", view: "measure"};
const currentRoute = () => route;
const setConnection = (label, state) => { calls.connection.push([label, state]); connectionState = {label, state}; };
// The first statement of the real updateGlobalStatus (pinned in test_both_paths_end_in_the_same_status_update).
function updateGlobalStatus(live) { ingestFreshness(live); calls.stamped.push(noteLiveAnswer(live)); }
const api = {pending: [], json(path, options) { return new Promise((resolve, reject) => api.pending.push({path, options, resolve, reject})); }};
// A source on the test EventSource; restart and accept only count.
const testSource = (extra = {}) => createFixSource(Object.assign({url: "/api/events", EventSource: FakeEventSource,
  restart: () => { calls.restart += 1; }, accept: live => { calls.accept.push(live); }, wanted: () => true}, extra));
"""
POLL_STUB = "\nconst pollLive = async force => { calls.poll.push(force); };\n"
HOLD_STUB = "const restartHoldActive = () => hold;\n"
API_STUB = ("const api = {pending: [], json(path, options) { return new Promise((resolve, reject) => "
            "api.pending.push({path, options, resolve, reject})); }};\n")
# The sign-in of src/15-store-api.js only counts; the real api.request (api_block) calls it on a 401.
SIGN_IN_STUBS = "\nconst signInRedirect = userAction => { calls.signIn.push(userAction); };\n"


def api_block():
    """The multi-line api object of src/15-store-api.js (request with its timeout and 401 branch, json, ...)."""
    start = APP_JS.index("\nconst api={") + 1
    return APP_JS[start:APP_JS.index("\n};\n", start) + 3]


def run_live(body, prefixes=(), real_poll=False, stubs="", real_hold=False, real_api=False):
    """Run an async Node body with the fix source; with real_poll the router's acceptLive and pollLive are real.

    With real_hold the restart hold is the caller's (the real restartHoldActive among the prefixes), not the test flag.
    With real_api the caller brings the api object (api_block in stubs) instead of the test api.
    """
    extra = (ROUTER_PREFIXES if real_poll else ()) + tuple(prefixes)
    base = STUBS.replace(HOLD_STUB, "") if real_hold else STUBS
    if real_api:
        if API_STUB not in base:
            raise AssertionError("the test api stub changed")
        base = base.replace(API_STUB, "")
    return run_app_node(BASE_PREFIXES + extra, body, base + ("" if real_poll else POLL_STUB) + stubs, asynchronous=True)


def app_line(prefix):
    lines = [line for line in APP_LINES if line.startswith(prefix)]
    if len(lines) != 1:
        raise AssertionError("expected one line starting with %s, found %d" % (prefix, len(lines)))
    return lines[0]


FRESHNESS_BODY = r"""
const STALE = JSON.stringify({current_fix: {fix_seq: 7, stale: true, quality: "RTK_FIXED"}, system: {uptime_sec: 100}});
const FRESH = JSON.stringify({current_fix: {fix_seq: 8, stale: false, quality: "RTK_FIXED"}, system: {uptime_sec: 101}});
const source = createFixSource({url: "/api/events", EventSource: FakeEventSource, restart() {}, accept: live => acceptLive(live),
  wanted: () => true});
if (CHANNEL === "stream") source.start();
const deliver = async text => {
  if (CHANNEL === "poll") { const poll = pollLive(); api.pending.shift().resolve(JSON.parse(text)); await poll; }
  else FakeEventSource.instances[0].emit("live", text);
};
const snap = () => ({usable: fixIsUsable(store.state.live.current_fix), link: liveView(2000).link, age: liveAgeMs(),
  staleSinceSeq, connection: connectionState.state});
clock.now = 1000;
await deliver(STALE);
out.stale = snap();
await advance(1500);
await deliver(FRESH);
out.fresh = snap();
await advance(4000);
out.later = snap();
await deliver(FRESH);
out.same = snap();
out.stamped = calls.stamped;
"""


class FixSourceArrivalTest(unittest.TestCase):
    """E12/E15: stream frames and polls end in the same acceptLive under one ordering rule."""

    def test_stream_and_poll_feed_the_same_freshness_under_node(self):
        results = {}
        for channel in ("poll", "stream"):
            out = run_live('const CHANNEL = "%s";\n' % channel + FRESHNESS_BODY, real_poll=True)
            self.assertEqual(out.pop("failures"), [])
            results[channel] = out
        self.assertEqual(results["poll"], results["stream"])
        stream = results["stream"]
        self.assertEqual(stream["stale"], {"usable": False, "link": "live", "age": 0, "staleSinceSeq": 7, "connection": "ok"})
        self.assertEqual(stream["fresh"], {"usable": True, "link": "live", "age": 0, "staleSinceSeq": 7, "connection": "ok"})
        self.assertEqual(stream["later"]["link"], "delayed")
        # A new current_fix object stamps the data age, also when its content repeats (each arrival parses anew).
        self.assertEqual(stream["stamped"], [True, True, True])
        self.assertEqual(stream["same"]["age"], 0)

    def test_both_paths_end_in_the_same_status_update(self):
        self.assertTrue(app_line("function updateGlobalStatus(").startswith(
            "function updateGlobalStatus(live){ingestFreshness(live);noteLiveAnswer(live);"))
        self.assertIn("updateGlobalStatus(live)", app_line("function acceptLive("))
        self.assertIn("renderLiveBindings(live)", app_line("function acceptLive("))

    def test_production_source_uses_the_global_event_source(self):
        line = app_line("const liveSource=")
        self.assertNotIn("EventSource", line)
        out = run_live(r"""
globalThis.EventSource = FakeEventSource;
let thrown = null;
try {
  out.started = liveSource.start();
  out.urls = FakeEventSource.instances.map(item => item.url);
  out.mode = liveSource.mode();
  liveSource.stop();
  delete globalThis.EventSource;
  out.without = liveSource.start();
  out.count = FakeEventSource.instances.length;
} catch (error) { thrown = String(error); }
out.thrown = thrown;
""", POLLING_PREFIXES, real_poll=True)
        self.assertEqual(out["failures"], [])
        self.assertIsNone(out["thrown"])
        self.assertEqual((out["started"], out["urls"], out["mode"]), (True, ["/api/events"], "stream"))
        self.assertEqual((out["without"], out["count"]), (False, 1))

    def test_frame_invalidates_an_older_poll(self):
        out = run_live(r"""
activeController = new AbortController();
const accepted = [];
liveBindings.add(live => accepted.push(live.name));
const source = createFixSource({url: "/api/events", EventSource: FakeEventSource, restart() {}, accept: live => acceptLive(live),
  wanted: () => true});
source.start();
const poll = pollLive();
const serial = requestSerial;
FakeEventSource.instances[0].emit("live", {name: "A"});
out.serialRaised = requestSerial === serial + 1;
api.pending[0].resolve({name: "B"});
await poll;
out.live = store.state.live.name;
out.accepted = accepted;
out.connection = calls.connection;
""", real_poll=True)
        self.assertEqual(out["failures"], [])
        self.assertTrue(out["serialRaised"])
        self.assertEqual((out["live"], out["accepted"], out["connection"]), ("A", ["A"], []))

    def test_poll_after_frame_is_accepted(self):
        out = run_live(r"""
const accepted = [];
liveBindings.add(live => accepted.push(live.name));
const source = createFixSource({url: "/api/events", EventSource: FakeEventSource, restart() {}, accept: live => acceptLive(live),
  wanted: () => true});
source.start();
FakeEventSource.instances[0].emit("live", {name: "A"});
const poll = pollLive();
api.pending[0].resolve({name: "B"});
await poll;
out.live = store.state.live.name;
out.accepted = accepted;
""", real_poll=True)
        self.assertEqual(out["failures"], [])
        self.assertEqual((out["live"], out["accepted"]), ("B", ["A", "B"]))

    def test_frame_during_a_forced_poll_reaches_the_caller(self):
        # R-B0-3: a forced poll that checks a result is never lost silently; a newer frame (by rule R) is what it gets.
        out = run_live(r"""
activeController = new AbortController();
const accepted = [];
liveBindings.add(live => accepted.push(live.name));
const source = createFixSource({url: "/api/events", EventSource: FakeEventSource, restart() {}, accept: live => acceptLive(live),
  wanted: () => true});
source.start();
const stream = FakeEventSource.instances[0];
const check = async settleAnswer => {
  let done = false;
  connectionState = {label: "Connecting", state: "warn"};
  const poll = pollLive(true).then(() => { done = true; });
  stream.emit("live", {name: "frame"});
  settleAnswer(api.pending.shift());
  await settle();
  await poll;
  return {done, live: store.state.live.name, connection: connectionState.state, busy: pollBusy};
};
out.answered = await check(request => request.resolve({name: "poll"}));
out.failed = await check(request => request.reject(new TypeError("Failed to fetch")));
out.accepted = [...accepted];
out.errors = calls.connection;
// The next forced poll after the frames is taken as it is.
const next = pollLive(true);
api.pending.shift().resolve({name: "next"});
await next;
out.next = store.state.live.name;
""", real_poll=True)
        self.assertEqual(out["failures"], [])
        expected = {"done": True, "live": "frame", "connection": "ok", "busy": False}
        self.assertEqual(out["answered"], expected)
        self.assertEqual(out["failed"], expected)
        self.assertEqual(out["accepted"], ["frame", "frame"])
        self.assertEqual(out["errors"], [])
        self.assertEqual(out["next"], "next")


class FixSourceFallbackTest(unittest.TestCase):
    """E14: heartbeats, errors, silence and bye frames decide between the stream and polling."""

    def test_heartbeat_keeps_the_stream_but_not_the_data_age(self):
        out = run_live(r"""
const source = testSource({accept: live => acceptLive(live)});
source.start();
const stream = FakeEventSource.instances[0];
stream.emit("live", {current_fix: {fix_seq: 1, stale: false}});
await advance(5000);
stream.emit("heartbeat", {t: 5});
await advance(5000);
stream.emit("heartbeat", {t: 10});
await advance(1000);
out.link = liveView(2000, 11000).link;
out.mode = source.mode();
out.restart = calls.restart;
out.closed = stream.closed;
out.retries = timeouts(30000) + timeouts(300000);
out.stamped = calls.stamped;
""", real_poll=True)
        self.assertEqual(out["failures"], [])
        self.assertEqual((out["link"], out["mode"], out["restart"], out["closed"], out["retries"]),
                         ("lost", "stream", 0, False, 0))
        self.assertEqual(out["stamped"], [True])

    def test_closed_error_falls_back_and_retries_after_30s(self):
        out = run_live(r"""
const source = testSource();
out.started = source.start();
FakeEventSource.instances[0].fail(2);
out.afterFail = {restart: calls.restart, closed: FakeEventSource.instances[0].closed, mode: source.mode(),
  start: source.start(), retry: timeouts(30000), count: FakeEventSource.instances.length, poll: [...calls.poll]};
await advance(29999);
out.before = {restart: calls.restart, start: source.start()};
await advance(1);
out.after = {restart: calls.restart, start: source.start(), count: FakeEventSource.instances.length, mode: source.mode()};
""")
        self.assertEqual(out["failures"], [])
        self.assertTrue(out["started"])
        # The fallback polls once at once (not forced), so the poll interval it starts leaves no gap.
        self.assertEqual(out["afterFail"], {"restart": 1, "closed": True, "mode": "poll", "start": False, "retry": 1, "count": 1,
                                            "poll": [None]})
        self.assertEqual(out["before"], {"restart": 1, "start": False})
        self.assertEqual(out["after"], {"restart": 2, "start": True, "count": 2, "mode": "stream"})

    def test_two_errors_in_ten_seconds_fall_back_to_polling(self):
        out = run_live(r"""
const near = testSource();
near.start();
FakeEventSource.instances[0].fail(0);
await advance(9000);
FakeEventSource.instances[0].fail(0);
out.near = {restart: calls.restart, mode: near.mode(), closed: FakeEventSource.instances[0].closed};
calls.restart = 0;
const far = testSource();
far.start();
FakeEventSource.instances[1].fail(0);
await advance(11000);
FakeEventSource.instances[1].fail(0);
out.far = {restart: calls.restart, mode: far.mode(), closed: FakeEventSource.instances[1].closed};
""")
        self.assertEqual(out["failures"], [])
        self.assertEqual(out["near"], {"restart": 1, "mode": "poll", "closed": True})
        self.assertEqual(out["far"], {"restart": 0, "mode": "stream", "closed": False})

    def test_silent_stream_falls_back_after_22s(self):
        out = run_live(r"""
const source = testSource();
source.start();
await advance(21999);
out.quiet = {restart: calls.restart, mode: source.mode()};
await advance(1);
out.silent = {restart: calls.restart, mode: source.mode(), closed: FakeEventSource.instances[0].closed, retry: timeouts(30000)};
await advance(30000);
source.start();
const stream = FakeEventSource.instances[1];
await advance(10000);
stream.emit("heartbeat", {t: 10});
await advance(21999);
out.kept = {restart: calls.restart, mode: source.mode()};
await advance(1);
out.again = {restart: calls.restart, mode: source.mode(), closed: stream.closed};
""")
        self.assertEqual(out["failures"], [])
        self.assertEqual(out["quiet"], {"restart": 0, "mode": "stream"})
        self.assertEqual(out["silent"], {"restart": 1, "mode": "poll", "closed": True, "retry": 1})
        # Every event restarts the watchdog: a heartbeat at 10 s moves the fallback to 32 s.
        self.assertEqual(out["kept"], {"restart": 2, "mode": "stream"})
        self.assertEqual(out["again"], {"restart": 3, "mode": "poll", "closed": True})

    def test_poll_stream_poll_switch_keeps_one_timer(self):
        out = run_live(r"""
globalThis.EventSource = FakeEventSource;
const seen = [];
const look = () => {
  const state = {intervals: intervals(), open: openStreams().length};
  if (state.intervals > 1 || state.open > 1 || (state.intervals && state.open)) seen.push(state);
  return state;
};
clock.after = look;
startPolling();
out.streaming = look();
FakeEventSource.instances[0].fail(2);
out.fellBack = look();
startPolling();
out.pending = Object.assign(look(), {count: FakeEventSource.instances.length, mode: liveSource.mode()});
await advance(30000);
out.retried = Object.assign(look(), {count: FakeEventSource.instances.length, mode: liveSource.mode()});
const polled = calls.poll.length;
FakeEventSource.instances[1].fail(2);
out.again = Object.assign(look(), {closed: FakeEventSource.instances[1].closed, polled: calls.poll.length - polled});
await advance(30000);
route = {path: "projects/p1", view: "project"};
startPolling();
out.project = Object.assign(look(), {mode: liveSource.mode()});
out.seen = seen;
""", ("const liveBindings=", "function renderLiveBindings(", "function acceptLive(") + POLLING_PREFIXES)
        self.assertEqual(out["failures"], [])
        self.assertEqual(out["streaming"], {"intervals": 0, "open": 1})
        self.assertEqual(out["fellBack"], {"intervals": 1, "open": 0})
        self.assertEqual(out["pending"], {"intervals": 1, "open": 0, "count": 1, "mode": "poll"})
        self.assertEqual(out["retried"], {"intervals": 0, "open": 1, "count": 2, "mode": "stream"})
        self.assertEqual(out["again"], {"intervals": 1, "open": 0, "closed": True, "polled": 1})
        self.assertEqual(out["project"], {"intervals": 0, "open": 0, "mode": "off"})
        self.assertEqual(out["seen"], [])

    def test_bye_session_falls_back_to_polling(self):
        # Codex review finding 1 (integrator ruling): bye session takes the fallback of a stream error (poll interval, one
        # poll at once, a new stream attempt after 30 s) instead of closing and forcing a single poll that a poll in flight
        # would swallow; the next poll's 401 then leads to the sign-in.
        out = run_live(r"""
const source = testSource();
source.start();
FakeEventSource.instances[0].emit("bye", {reason: "session"});
out.closed = FakeEventSource.instances[0].closed;
out.poll = calls.poll;
out.restart = calls.restart;
out.retry = timeouts(30000) + timeouts(300000);
out.mode = source.mode();
out.start = source.start();
""")
        self.assertEqual(out["failures"], [])
        self.assertEqual((out["closed"], out["poll"], out["restart"], out["retry"], out["mode"], out["start"]),
                         (True, [None], 1, 1, "poll", False))

    def test_bye_session_during_a_poll_keeps_polling_until_the_sign_in(self):
        # Codex review finding 1: a poll in flight at the bye (here mountMeasure's forced poll) must not leave the app without
        # a poll interval. Whether it times out or brings a 200 from before the expiry, the next poll follows within one
        # interval, its 401 asks for the sign-in, and polling goes on when the sign-in is declined.
        for outcome in ("timeout", "answer"):
            with self.subTest(outcome=outcome):
                out = run_live(r"""
globalThis.EventSource = FakeEventSource;
activeController = new AbortController();
calls.signIn = [];
const fetches = [];
globalThis.fetch = (path, options) => new Promise((resolve, reject) => {
  fetches.push({path, resolve});
  options.signal.addEventListener("abort", () => reject(Object.assign(new Error("aborted"), {name: "AbortError"})), {once: true});
});
const respond = (status, data) => ({status, ok: status < 300, body: status < 300 ? {} : null, json: async () => data,
  text: async () => JSON.stringify(data), blob: async () => null, arrayBuffer: async () => null});
const polls = () => fetches.filter(item => item.path === "/api/ui/live").length;
startPolling();
const mounted = pollLive(true);
FakeEventSource.instances[0].emit("bye", {reason: "session"});
out.atBye = {closed: FakeEventSource.instances[0].closed, mode: liveSource.mode(), polls: polls(), intervals: intervals()};
if (OUTCOME === "timeout") await advance(5000);
else fetches[0].resolve(respond(200, {name: "before expiry"}));
await mounted;
out.settled = {polls: polls(), signIn: calls.signIn.length, busy: pollBusy};
await advance(livePollIntervalMs(route.path));
out.next = {polls: polls()};
fetches[fetches.length - 1].resolve(respond(401, {error: "unauthorized"}));
await settle();
out.signIn = calls.signIn;
await advance(livePollIntervalMs(route.path));
out.declined = {polls: polls() - out.next.polls, intervals: intervals(), streams: FakeEventSource.instances.length};
""".replace("OUTCOME", json.dumps(outcome)), POLLING_PREFIXES + ("function requestAbortError(",), real_poll=True,
                    stubs=SIGN_IN_STUBS + api_block(), real_api=True)
                self.assertEqual(out["failures"], [])
                self.assertEqual(out["atBye"], {"closed": True, "mode": "poll", "polls": 1, "intervals": 1})
                self.assertEqual(out["settled"], {"polls": 1, "signIn": 0, "busy": False})
                self.assertEqual(out["next"], {"polls": 2})
                self.assertEqual(out["signIn"], [False])
                self.assertEqual(out["declined"], {"polls": 1, "intervals": 1, "streams": 1})

    def test_eviction_yields_for_five_minutes(self):
        out = run_live(r"""
for (const reason of ["evicted", "disabled"]) {
  calls.restart = 0;
  const source = testSource();
  source.start();
  const stream = FakeEventSource.instances[FakeEventSource.instances.length - 1];
  stream.emit("bye", {reason});
  const state = {closed: stream.closed, restart: calls.restart, yield: timeouts(300000), retry: timeouts(30000), start: source.start()};
  await advance(299999);
  state.waiting = {start: source.start(), restart: calls.restart};
  await advance(1);
  state.after = {restart: calls.restart, start: source.start()};
  source.stop();
  out[reason] = state;
}
out.poll = calls.poll;
""")
        self.assertEqual(out["failures"], [])
        for reason in ("evicted", "disabled"):
            self.assertEqual(out[reason], {"closed": True, "restart": 1, "yield": 1, "retry": 0, "start": False,
                                           "waiting": {"start": False, "restart": 1}, "after": {"restart": 2, "start": True}})
        self.assertEqual(out["poll"], [None, None])

    def test_renew_drops_the_old_generation(self):
        out = run_live(r"""
const source = testSource();
source.start();
const first = FakeEventSource.instances[0];
first.fail(0);
await advance(1000);
const serial = requestSerial;
source.renew();
const second = FakeEventSource.instances[1];
out.renewed = {firstClosed: first.closed, secondOpen: !second.closed, mode: source.mode(), restart: calls.restart,
  retries: timeouts(30000) + timeouts(300000)};
first.emit("live", {name: "old"});
await advance(5000);
first.emit("live", {name: "old later"});
first.fail(2);
out.old = {accept: calls.accept.map(live => live.name), serial: requestSerial - serial, mode: source.mode(), restart: calls.restart};
second.emit("live", {name: "new"});
second.fail(0);
out.fresh = {accept: calls.accept.map(live => live.name), serial: requestSerial - serial, mode: source.mode(), restart: calls.restart};
// retryAt stays unchanged (never set): after a stop the source opens again at once.
source.stop();
out.reopen = source.start();
FakeEventSource.instances[2].fail(2);
const count = FakeEventSource.instances.length;
source.renew();
out.pollRenew = {count: FakeEventSource.instances.length - count, mode: source.mode()};
""")
        self.assertEqual(out["failures"], [])
        self.assertEqual(out["renewed"], {"firstClosed": True, "secondOpen": True, "mode": "stream", "restart": 0, "retries": 0})
        self.assertEqual(out["old"], {"accept": [], "serial": 0, "mode": "stream", "restart": 0})
        # The error log starts empty with the new instance: one error is no fallback.
        self.assertEqual(out["fresh"], {"accept": ["new"], "serial": 1, "mode": "stream", "restart": 0})
        self.assertTrue(out["reopen"])
        self.assertEqual(out["pollRenew"], {"count": 0, "mode": "poll"})

    def test_poll_only_source_never_opens_a_stream(self):
        out = run_live(r"""
const source = testSource({url: null});
out.start = source.start();
out.mode = source.mode();
source.renew();
out.count = FakeEventSource.instances.length;
source.stop();
out.stopped = source.mode();
out.timers = clock.timers.size;
""")
        self.assertEqual(out["failures"], [])
        self.assertEqual((out["start"], out["mode"], out["count"], out["stopped"], out["timers"]), (False, "poll", 0, "off", 0))

    def test_stream_frame_in_restart_hold_closes(self):
        out = run_live(r"""
const source = testSource();
source.start();
hold = true;
const serial = requestSerial;
FakeEventSource.instances[0].emit("live", {name: "held"});
out.frame = {accept: calls.accept.length, serial: requestSerial - serial, closed: FakeEventSource.instances[0].closed,
  mode: source.mode()};
hold = false;
source.start();
hold = true;
FakeEventSource.instances[1].fail(0);
FakeEventSource.instances[1].fail(2);
out.error = {closed: FakeEventSource.instances[1].closed, restart: calls.restart, timers: clock.timers.size, mode: source.mode()};
""")
        self.assertEqual(out["failures"], [])
        self.assertEqual(out["frame"], {"accept": 0, "serial": 0, "closed": True, "mode": "off"})
        self.assertEqual(out["error"], {"closed": True, "restart": 0, "timers": 0, "mode": "off"})

    def test_source_has_no_progress_channel(self):
        out = run_live(r"""
const source = testSource();
out.keys = Object.keys(source).sort();
source.start();
out.listeners = Object.keys(FakeEventSource.instances[0].listeners).sort();
""")
        self.assertEqual(out["failures"], [])
        self.assertEqual(out["keys"], ["mode", "renew", "start", "stop"])
        self.assertEqual(out["listeners"], ["bye", "error", "heartbeat", "live"])
        self.assertNotIn("PROGRESS_STREAM_FRESH_MS", APP_JS)


class FixSourceWiringTest(unittest.TestCase):
    """The router delegates to the source; the constants are the plan's values."""

    def test_poll_live_uses_accept_live(self):
        poll = app_line("async function pollLive(")
        self.assertIn("const serial=++requestSerial", poll)
        self.assertIn("if(serial!==requestSerial", poll)
        self.assertIn("acceptLive(live)", poll)
        self.assertNotIn("store.set({live})", poll)
        self.assertIn("updateGlobalStatus(live)", app_line("function acceptLive("))
        polling = app_line("function startPolling(")
        self.assertTrue(polling.startswith("function startPolling(){if(restartHoldActive())return;clearInterval(pollTimer);"))
        self.assertIn("if(!delay)liveSource.stop();else if(!liveSource.start())pollTimer=setInterval(pollLive,delay);", polling)

    def test_constants_and_production_source(self):
        self.assertEqual(app_line("const LIVE_STREAM_URL="),
                         'const LIVE_STREAM_URL="/api/events",LIVE_STREAM_WATCHDOG_MS=22000,LIVE_STREAM_ERROR_WINDOW_MS=10000,'
                         'LIVE_STREAM_RETRY_MS=30000,LIVE_STREAM_YIELD_MS=300000;')
        self.assertEqual(app_line("const liveSource="),
                         "const liveSource=createFixSource({url:LIVE_STREAM_URL,restart:()=>startPolling(),"
                         "accept:live=>{if(!measurementRequestActive)acceptLive(live);},"
                         "wanted:()=>!document.hidden&&navigator.onLine!==false});")
        self.assertTrue(app_line("function createFixSource(options){"))
        with open(os.path.join(ROOT, "src", "91-live-stream.js"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn("progress", source)


# The boot line of src/99-boot.js with the window and document listeners; a test registry stands in for both.
BOOT_PREFIX = 'addEventListener("hashchange",'
BOOT_STUBS = r"""
const handlers = {};
const listen = (name, fn) => { (handlers[name] = handlers[name] || []).push(fn); };
globalThis.addEventListener = (name, fn) => listen("window:" + name, fn);
document.addEventListener = (name, fn) => listen("document:" + name, fn);
const fire = name => (handlers[name] || []).forEach(fn => fn());
const syncWakeLock = () => {};
// The boot handlers keep a route with an open export sheet alive (#46); no sheet is open here.
const exportSheetOpen = () => false;
// renderRoute starts the polling once more, as the real one does after a visible tab or a restored connection.
const renderRoute = () => { calls.rendered = (calls.rendered || 0) + 1; startPolling(); };
"""
# The restart hold of src/82-restart-panel.js: the single-line state helpers and the two multi-line hold functions.
HOLD_PREFIXES = ("let restartWatchState=", "function newRestartWatchState(", "function restartHoldActive(")
OWN_CHANGE_PREFIXES = POLLING_PREFIXES + ("function invalidateLive(",)


def boot_branch(start, end="}"):
    """The text of src/99-boot.js from start up to the first end after it."""
    with open(os.path.join(ROOT, "src", "99-boot.js"), encoding="utf-8") as handle:
        boot = handle.read()
    begin = boot.index(start) + len(start)
    return boot[begin:boot.index(end, begin)]


class FixSourceLifecycleTest(unittest.TestCase):
    """B2: a hidden tab, offline and the restart hold close the stream; confirmed own changes renew it (rule R3)."""

    def test_hidden_tab_and_offline_close_the_stream(self):
        self.assertIn("liveSource.stop();", boot_branch('addEventListener("offline",()=>{', "});"))
        self.assertIn("liveSource.stop();", boot_branch("if(document.hidden){"))
        self.assertNotIn("liveSource", boot_branch("}else{"))
        out = run_live(r"""
globalThis.EventSource = FakeEventSource;
activeController = new AbortController();
const look = () => ({open: openStreams().length, count: FakeEventSource.instances.length, mode: liveSource.mode(),
  intervals: intervals(), retries: timeouts(30000) + timeouts(300000)});
startPolling();
out.visible = look();
document.hidden = true;
fire("document:visibilitychange");
out.hidden = Object.assign(look(), {closed: FakeEventSource.instances[0].closed});
await advance(60000);
out.hiddenLater = look();
document.hidden = false;
fire("document:visibilitychange");
// Visible again: startPolling runs twice (directly and through renderRoute) and opens exactly one new instance.
out.shown = Object.assign(look(), {rendered: calls.rendered});
const serial = requestSerial;
navigator.onLine = false;
fire("window:offline");
out.offline = Object.assign(look(), {closed: FakeEventSource.instances[1].closed, serial: requestSerial - serial,
  connection: calls.connection.map(item => item[1])});
await advance(60000);
out.offlineLater = look();
""", ("const liveBindings=", "function renderLiveBindings(", "function acceptLive(") + POLLING_PREFIXES + (BOOT_PREFIX,),
            stubs=BOOT_STUBS)
        self.assertEqual(out["failures"], [])
        self.assertEqual(out["visible"], {"open": 1, "count": 1, "mode": "stream", "intervals": 0, "retries": 0})
        self.assertEqual(out["hidden"], {"open": 0, "count": 1, "mode": "off", "intervals": 0, "retries": 0, "closed": True})
        self.assertEqual(out["hiddenLater"], {"open": 0, "count": 1, "mode": "off", "intervals": 0, "retries": 0})
        self.assertEqual(out["shown"], {"open": 1, "count": 2, "mode": "stream", "intervals": 0, "retries": 0, "rendered": 1})
        self.assertEqual(out["offline"], {"open": 0, "count": 2, "mode": "off", "intervals": 0, "retries": 0, "closed": True,
                                          "serial": 1, "connection": ["error"]})
        self.assertEqual(out["offlineLater"], {"open": 0, "count": 2, "mode": "off", "intervals": 0, "retries": 0})

    def test_start_opens_a_stream_only_when_wanted(self):
        # Codex review finding 2 (integrator ruling): start() asks options.wanted() before it opens a stream; unwanted, the
        # caller polls and nothing waits for a retry. Wanted, start() stays idempotent (R-B0-5).
        out = run_live(r"""
let wanted = false;
const source = testSource({wanted: () => wanted});
out.unwanted = {start: source.start(), count: FakeEventSource.instances.length, mode: source.mode(), timers: clock.timers.size};
wanted = true;
out.wanted = {start: source.start(), again: source.start(), count: FakeEventSource.instances.length, mode: source.mode()};
""")
        self.assertEqual(out["failures"], [])
        self.assertEqual(out["unwanted"], {"start": False, "count": 0, "mode": "poll", "timers": 0})
        self.assertEqual(out["wanted"], {"start": True, "again": True, "count": 1, "mode": "stream"})

    def test_hidden_or_offline_start_opens_no_stream(self):
        # Codex review finding 2: an app that loads in a hidden background tab or offline opens no stream (a background tab
        # would hold one of the device's three); the visible or online transition of src/99-boot.js opens exactly one.
        for start in ("hidden", "offline"):
            with self.subTest(start=start):
                out = run_live(r"""
globalThis.EventSource = FakeEventSource;
activeController = new AbortController();
const look = () => ({open: openStreams().length, count: FakeEventSource.instances.length, mode: liveSource.mode(),
  intervals: intervals(), retries: timeouts(30000) + timeouts(300000)});
if (START === "hidden") document.hidden = true; else navigator.onLine = false;
// The boot render: renderRoute starts the polling.
renderRoute();
out.boot = look();
await advance(60000);
out.bootLater = look();
if (START === "hidden") { document.hidden = false; fire("document:visibilitychange"); }
else { navigator.onLine = true; fire("window:online"); }
out.shown = look();
""".replace("START", json.dumps(start)),
                    ("const liveBindings=", "function renderLiveBindings(", "function acceptLive(") + POLLING_PREFIXES +
                    (BOOT_PREFIX,), stubs=BOOT_STUBS)
                self.assertEqual(out["failures"], [])
                # Not wanted: the poll interval runs as before #27 (pollLive itself rests while the tab is hidden).
                self.assertEqual(out["boot"], {"open": 0, "count": 0, "mode": "poll", "intervals": 1, "retries": 0})
                self.assertEqual(out["bootLater"], {"open": 0, "count": 0, "mode": "poll", "intervals": 1, "retries": 0})
                self.assertEqual(out["shown"], {"open": 1, "count": 1, "mode": "stream", "intervals": 0, "retries": 0})

    def test_restart_hold_closes_the_stream(self):
        self.assertIn("liveSource.stop()", multi_line_function("function beginRestartHold("))
        out = run_live(r"""
globalThis.EventSource = FakeEventSource;
activeController = new AbortController();
const look = () => ({open: openStreams().length, count: FakeEventSource.instances.length, mode: liveSource.mode(),
  intervals: intervals(), held: restartHoldActive()});
startPolling();
out.before = look();
// A hold that begins while the stream is open.
let serial = requestSerial;
beginRestartHold();
out.held = Object.assign(look(), {closed: FakeEventSource.instances[0].closed, serial: requestSerial - serial,
  retries: timeouts(30000) + timeouts(300000)});
startPolling();
await advance(40000);
out.heldLater = look();
endRestartHold();
out.ended = look();
// A hold that begins after a fallback: the retry timer fires during the hold, so retryAt lies in the past at the end.
FakeEventSource.instances[1].fail(2);
out.fellBack = look();
beginRestartHold();
out.heldPoll = Object.assign(look(), {retries: timeouts(30000)});
await advance(31000);
out.retryInHold = Object.assign(look(), {retries: timeouts(30000)});
endRestartHold();
out.endedPoll = look();
""", ("const liveBindings=", "function renderLiveBindings(", "function acceptLive(") + POLLING_PREFIXES + HOLD_PREFIXES,
            stubs="\n" + multi_line_function("function beginRestartHold(") + multi_line_function("function endRestartHold("),
            real_hold=True)
        self.assertEqual(out["failures"], [])
        self.assertEqual(out["before"], {"open": 1, "count": 1, "mode": "stream", "intervals": 0, "held": False})
        self.assertEqual(out["held"], {"open": 0, "count": 1, "mode": "off", "intervals": 0, "held": True, "closed": True,
                                       "serial": 1, "retries": 0})
        self.assertEqual(out["heldLater"], {"open": 0, "count": 1, "mode": "off", "intervals": 0, "held": True})
        self.assertEqual(out["ended"], {"open": 1, "count": 2, "mode": "stream", "intervals": 0, "held": False})
        self.assertEqual(out["fellBack"], {"open": 0, "count": 2, "mode": "poll", "intervals": 1, "held": False})
        self.assertEqual(out["heldPoll"], {"open": 0, "count": 2, "mode": "off", "intervals": 0, "held": True, "retries": 1})
        self.assertEqual(out["retryInHold"], {"open": 0, "count": 2, "mode": "off", "intervals": 0, "held": True, "retries": 0})
        self.assertEqual(out["endedPoll"], {"open": 1, "count": 3, "mode": "stream", "intervals": 0, "held": False})

    OWN_CHANGES = (
        ("adoptMeasurementSamples(10)", "function adoptMeasurementSamples(",
         {"samples": 5, "reference_frame": "ETRS89"}, {"samples": 10, "reference_frame": "ETRS89"}),
        ('adoptFrameValues({values: {reference_frame: "ITRF2014", reference_frame_epoch: "2010.0"}, suggestion: null})',
         "function adoptFrameValues(",
         {"samples": 5, "reference_frame": "ETRS89", "reference_frame_epoch": ""},
         {"samples": 5, "reference_frame": "ITRF2014", "reference_frame_epoch": "2010.0"}),
    )

    def test_own_change_drops_the_old_stream_generation(self):
        for call, prefix, old, new in self.OWN_CHANGES:
            with self.subTest(call=call):
                out = run_live(r"""
globalThis.EventSource = FakeEventSource;
activeController = new AbortController();
const OLD = %s, NEW = %s;
const accepted = [];
liveBindings.add(live => accepted.push(live.name));
const defaults = () => store.state.live.measurement_defaults;
store.state.live = {name: "start", measurement_defaults: Object.assign({}, OLD), frame_suggestion: null};
startPolling();
const first = FakeEventSource.instances[0];
// A poll started before the change; it answers with the old values afterwards.
const poll = pollLive();
const serial = requestSerial;
%s;
out.changed = {serial: requestSerial - serial, firstClosed: first.closed, count: FakeEventSource.instances.length,
  open: openStreams().length, mode: liveSource.mode(), defaults: defaults(), intervals: intervals(),
  retries: timeouts(30000) + timeouts(300000)};
await advance(6000);
first.emit("live", {name: "old frame", measurement_defaults: Object.assign({}, OLD)});
api.pending.shift().resolve({name: "old poll", measurement_defaults: Object.assign({}, OLD)});
await poll;
out.old = {defaults: defaults(), name: store.state.live.name, accepted: [...accepted], connection: calls.connection};
FakeEventSource.instances[1].emit("live", {name: "new frame", measurement_defaults: Object.assign({}, NEW)});
out.fresh = {defaults: defaults(), accepted: [...accepted], mode: liveSource.mode()};
""" % (json.dumps(old), json.dumps(new), call), OWN_CHANGE_PREFIXES + (prefix,), real_poll=True)
                self.assertEqual(out["failures"], [])
                self.assertEqual(out["changed"], {"serial": 1, "firstClosed": True, "count": 2, "open": 1, "mode": "stream",
                                                  "defaults": new, "intervals": 0, "retries": 0})
                self.assertEqual(out["old"], {"defaults": new, "name": "start", "accepted": [], "connection": []})
                self.assertEqual(out["fresh"], {"defaults": new, "accepted": ["new frame"], "mode": "stream"})

    def test_field_setup_changes_go_through_invalidate_live(self):
        self.assertEqual(app_line("function invalidateLive("), "function invalidateLive(){++requestSerial;liveSource.renew();}")
        with open(os.path.join(ROOT, "src", "76-field-setup.js"), encoding="utf-8") as handle:
            field_setup = handle.read()
        self.assertNotIn("++requestSerial", field_setup)
        for prefix, start in (("function adoptMeasurementSamples(", "function adoptMeasurementSamples(samples){invalidateLive();"),
                              ("function adoptFrameValues(", "function adoptFrameValues(saved){invalidateLive();")):
            self.assertTrue(app_line(prefix).startswith(start), prefix)

    def test_tracking_mutation_renews_the_stream(self):
        mutation = app_line("function applyTrackingMutation(")
        self.assertEqual(mutation.count("invalidateLive();"), 1)
        self.assertIn("if(!delta)return result;invalidateLive();", mutation)
        self.assertLess(mutation.index("invalidateLive();"), mutation.index("store.set({live})"))
        out = run_live(r"""
globalThis.EventSource = FakeEventSource;
const log = [];
const set = store.set;
store.set = function (patch) {
  log.push(["set", requestSerial, FakeEventSource.instances.length]);
  return set.call(this, patch);
};
const renew = liveSource.renew;
liveSource.renew = () => { log.push(["renew", requestSerial]); renew(); };
store.state.live = {current_fix: {fix_seq: 1}, revision: 1};
startPolling();
const first = FakeEventSource.instances[0];
const serial = requestSerial;
const result = applyTrackingMutation({ok: true, tracking_live: {revision: 2, active_project: "p1"}});
out.log = log.map(([name, at, count]) => [name, at - serial].concat(count === undefined ? [] : [count]));
const live = store.state.live;
out.mutated = {firstClosed: first.closed, open: openStreams().length, mode: liveSource.mode(), revision: live.revision,
  project: live.active_project, fix: live.current_fix.fix_seq, delta: "tracking_live" in result};
first.emit("live", {current_fix: {fix_seq: 1}, revision: 1});
out.oldFrame = store.state.live.revision;
log.length = 0;
const plain = applyTrackingMutation({ok: true});
out.plain = {log: log.length, count: FakeEventSource.instances.length, serial: requestSerial - serial, ok: plain.ok};
""", OWN_CHANGE_PREFIXES + ("let refreshFrameNotice=", "function applyTrackingMutation("), real_poll=True,
            stubs="\nconst statusRefs = null;\n")
        self.assertEqual(out["failures"], [])
        self.assertEqual(out["log"], [["renew", 1], ["set", 1, 2]])
        self.assertEqual(out["mutated"], {"firstClosed": True, "open": 1, "mode": "stream", "revision": 2, "project": "p1",
                                          "fix": 1, "delta": False})
        self.assertEqual(out["oldFrame"], 2)
        self.assertEqual(out["plain"], {"log": 0, "count": 2, "serial": 1, "ok": True})


# The production source with the real arrival, the router and the tracking mutation (B3).
MEASUREMENT_PREFIXES = OWN_CHANGE_PREFIXES + ("let refreshFrameNotice=", "function applyTrackingMutation(")
MEASUREMENT_STUBS = "\nconst statusRefs = null;\n"


class FixSourceMeasurementTest(unittest.TestCase):
    """B3 (E16): the measurement dialog keeps its progress poll; the stream stays open but takes no frame meanwhile."""

    def test_measurement_dialog_is_unchanged(self):
        dialog = multi_line_function("async function measuredAction(")
        self.assertEqual(dialog.count('api.json("/api/tracking?view=progress",{timeout:2500})'), 1)
        self.assertEqual(dialog.count("setTimeout(update,650)"), 1)
        self.assertNotIn("liveSource", dialog)
        self.assertNotIn("requestSerial", dialog)
        self.assertNotIn("invalidateLive", dialog)

    def test_stream_frames_wait_while_a_measurement_runs(self):
        out = run_live(r"""
globalThis.EventSource = FakeEventSource;
activeController = new AbortController();
const accepted = [];
liveBindings.add(live => accepted.push(live.name));
store.state.live = {name: "dialog"};
startPolling();
const instance = FakeEventSource.instances[0];
measurementRequestActive = true;
const serial = requestSerial;
for (let second = 1; second <= 30; second++) {
  await advance(1000);
  instance.emit("live", {name: "frame " + second});
}
out.during = {accepted: [...accepted], live: store.state.live.name, mode: liveSource.mode(), closed: instance.closed,
  count: FakeEventSource.instances.length, intervals: intervals(), retries: timeouts(30000) + timeouts(300000),
  serial: requestSerial - serial};
measurementRequestActive = false;
await advance(1000);
instance.emit("live", {name: "after"});
out.after = {accepted: [...accepted], live: store.state.live.name, mode: liveSource.mode()};
""", MEASUREMENT_PREFIXES, real_poll=True, stubs=MEASUREMENT_STUBS)
        self.assertEqual(out["failures"], [])
        # Rule R2 still counts each frame; pollLive rests during the measurement anyway.
        self.assertEqual(out["during"], {"accepted": [], "live": "dialog", "mode": "stream", "closed": False, "count": 1,
                                         "intervals": 0, "retries": 0, "serial": 30})
        self.assertEqual(out["after"], {"accepted": ["after"], "live": "after", "mode": "stream"})

    def test_measurement_end_renews_the_stream(self):
        out = run_live(r"""
globalThis.EventSource = FakeEventSource;
activeController = new AbortController();
const accepted = [];
liveBindings.add(live => accepted.push(live.revision));
store.state.live = {current_fix: {fix_seq: 1}, revision: 1};
startPolling();
const first = FakeEventSource.instances[0];
measurementRequestActive = true;
first.emit("live", {current_fix: {fix_seq: 1}, revision: 1});
// The confirmed answer of the measurement arrives before the finally of measuredAction clears the flag.
applyTrackingMutation({ok: true, tracking_live: {revision: 2}});
const second = FakeEventSource.instances[1];
out.renewed = {firstClosed: first.closed, count: FakeEventSource.instances.length, open: openStreams().length,
  mode: liveSource.mode(), revision: store.state.live.revision};
second.emit("live", {current_fix: {fix_seq: 2}, revision: 3});
out.beforeFinally = {revision: store.state.live.revision, accepted: [...accepted]};
measurementRequestActive = false;
first.emit("live", {current_fix: {fix_seq: 1}, revision: 1});
out.oldFrame = {revision: store.state.live.revision, accepted: [...accepted]};
second.emit("live", {current_fix: {fix_seq: 3}, revision: 4});
out.newFrame = {revision: store.state.live.revision, accepted: [...accepted], mode: liveSource.mode()};
""", MEASUREMENT_PREFIXES, real_poll=True, stubs=MEASUREMENT_STUBS)
        self.assertEqual(out["failures"], [])
        self.assertEqual(out["renewed"], {"firstClosed": True, "count": 2, "open": 1, "mode": "stream", "revision": 2})
        self.assertEqual(out["beforeFinally"], {"revision": 2, "accepted": []})
        self.assertEqual(out["oldFrame"], {"revision": 2, "accepted": []})
        self.assertEqual(out["newFrame"], {"revision": 4, "accepted": [4], "mode": "stream"})

    def test_forced_poll_during_a_measurement_ends_without_a_hang_or_an_error(self):
        # R-B0-3: a frame raises requestSerial even while it is declined, so a forced poll in flight (mountMeasure forces
        # one during a measurement) resolves without taking its answer; the dialog's poll stays the only source (E16).
        for outcome in ("answers", "fails"):
            with self.subTest(outcome=outcome):
                out = run_live(r"""
globalThis.EventSource = FakeEventSource;
activeController = new AbortController();
store.state.live = {name: "dialog"};
startPolling();
measurementRequestActive = true;
let settled = false;
const poll = pollLive(true).then(() => { settled = true; });
FakeEventSource.instances[0].emit("live", {name: "frame"});
if (OUTCOME === "answers") api.pending[0].resolve({name: "poll"});
else api.pending[0].reject(new Error("timeout"));
await poll;
out.result = {settled, live: store.state.live.name, connection: calls.connection, busy: pollBusy, mode: liveSource.mode()};
""".replace("OUTCOME", json.dumps(outcome)), MEASUREMENT_PREFIXES, real_poll=True, stubs=MEASUREMENT_STUBS)
                self.assertEqual(out["failures"], [])
                self.assertEqual(out["result"], {"settled": True, "live": "dialog", "connection": [], "busy": False,
                                                 "mode": "stream"})


if __name__ == "__main__":
    unittest.main()
