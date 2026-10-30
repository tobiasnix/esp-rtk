# SPDX-License-Identifier: AGPL-3.0-only
"""Router cache, bindings and loading status (M2 #29b)."""
import os
import re
import subprocess
import unittest

from test_navigation import ROUTE_PREFIXES, run_app_node
from test_ui import GATE_DECLARATION_PREFIXES

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(ROOT, "app.js"), encoding="utf-8") as source:
    APP_JS = source.read()
with open(os.path.join(ROOT, "app.css"), encoding="utf-8") as source:
    APP_CSS = source.read()
# The merge commit of Task 0: src/40-tracking-actions.js must stay byte-identical to it (E9), apart from the one call
# #27 part B adds (rule R3: a confirmed tracking mutation renews the live stream before it is adopted).
TASK0_SHA = "e53563ff8bac4f72a0126928eccbe85fd1630a76"


def block_of(prefix):
    """Source of the top-level declaration that starts a line with prefix, up to its closing line."""
    start = APP_JS.index("\n" + prefix) + 1
    one_line = APP_JS.index("\n", start)
    if APP_JS[one_line - 1] == "{" or not APP_JS[start:one_line].rstrip().endswith("}"):
        return APP_JS[start:APP_JS.index("\n}", start) + 2]
    return APP_JS[start:one_line]


class DeadLayersTest(unittest.TestCase):
    def test_no_dead_override_layers(self):
        # E12: the active renderers carry the plain names; no V-suffixed twin, reassignment or dead rule is left.
        self.assertEqual(len(re.findall(r"^async function startLine\(", APP_JS, re.M)), 1)
        self.assertEqual(len(re.findall(r"^async function mountConfiguration\(", APP_JS, re.M)), 1)
        self.assertNotIn("startLineV157", APP_JS)
        self.assertNotIn("mountConfigurationV1561", APP_JS)
        self.assertIsNone(re.search(r"^startLine=", APP_JS, re.M))
        self.assertIsNone(re.search(r"^mountConfiguration=", APP_JS, re.M))
        self.assertNotIn("function parseLog(", APP_JS)
        self.assertNotIn("waitForConfigResult", APP_JS)
        self.assertIsNone(re.search(r"\.toast\b", APP_CSS))
        self.assertIn("commitSavedForm(", block_of("async function mountConfiguration("))
        start = block_of("async function startLine(")
        self.assertIn("recording_mode", start)
        self.assertIn("captureAction(", start)


# E7: the Measure screen renders through one updateStatus made of named steps in the former layer order.
UPDATE_STATUS_STEPS = ("renderStatusCore(live)", "renderStartupBlock(live)", "renderStatusExtras(live)",
                       "applyLiveFreshness(live)", "placeFeedbackSlots(live)", "renderFieldSummary(live)")


class UpdateStatusStepsTest(unittest.TestCase):
    def test_update_status_is_one_function(self):
        self.assertIsNone(re.search(r"^updateStatus=function", APP_JS, re.M))
        for layer in ("const updateStatusBase=", "const updateStatusGate=", "const updateStatusFresh="):
            self.assertNotIn(layer, APP_JS)
        lines = [line for line in APP_JS.split("\n") if line.startswith("function updateStatus(")]
        self.assertEqual(len(lines), 1)
        positions = [lines[0].find(step) for step in UPDATE_STATUS_STEPS]
        self.assertNotIn(-1, positions, "every step is called")
        self.assertEqual(positions, sorted(positions), "steps run in the former layer order")

    def test_update_status_runs_every_step_in_order(self):
        stubs = r"""
let statusRefs = {};
const calls = [];
function renderStatusCore(live) { calls.push("core"); }
function renderStartupBlock(live) { calls.push("startup"); return live.startup; }
function renderStatusExtras(live) { calls.push("extras"); }
function applyLiveFreshness(live) { calls.push("freshness"); }
function placeFeedbackSlots(live) { calls.push("feedback"); }
function renderFieldSummary(live) { calls.push("summary"); }
"""
        body = r"""
const run = live => { calls.length = 0; updateStatus(live); return calls.slice(); };
eq("storage ready", run({startup: false}), ["core", "startup", "extras", "freshness", "feedback", "summary"]);
eq("storage starting", run({startup: true}), ["core", "startup", "freshness", "feedback", "summary"]);
statusRefs = null;
eq("no measure screen", run({startup: false}), []);
"""
        try:
            out = run_app_node(("function updateStatus(",), body, stubs)
        except AssertionError as error:
            lines = str(error).strip().splitlines()
            self.fail("expected order core, startup, extras, freshness, feedback, summary; got: %s"
                      % next((line for line in lines if "Error" in line), lines[-1] if lines else "no output"))
        self.assertEqual(out["failures"], [])

    def test_startup_block_only_for_unready_storage(self):
        stubs = r"""
let statusRefs = null;
const cleared = [];
function clearActionBar(bar) { cleared.push(bar.name); bar.children.length = 0; }
function h(tag, attrs) { return {tag, attrs}; }
function localText(text) { return text; }
const freshRefs = () => ({hero: {className: "readiness-hero ready"}, title: {textContent: "T"},
  subtitle: {textContent: "S"}, actions: {name: "actions", children: ["button"], append(node) { this.children.push(node); }}});
"""
        body = r"""
const hint = "Measurement actions unlock when survey storage is ready.";
for (const [name, live] of [["ready", {tracking_startup: {state: "ready"}}], ["absent", {}]]) {
  statusRefs = freshRefs();
  eq(name + " result", renderStartupBlock(live), false);
  eq(name + " hero", statusRefs.hero.className, "readiness-hero ready");
  eq(name + " title", statusRefs.title.textContent, "T");
  eq(name + " actions", statusRefs.actions.children, ["button"]);
}
eq("nothing cleared", cleared, []);
statusRefs = freshRefs();
eq("loading result", renderStartupBlock({tracking_startup: {state: "loading"}}), true);
eq("loading hero", statusRefs.hero.className, "readiness-hero blocked");
eq("loading title", statusRefs.title.textContent, "Loading survey storage");
eq("loading subtitle", statusRefs.subtitle.textContent, hint);
eq("loading cleared", cleared, ["actions"]);
eq("loading hint", statusRefs.actions.children.map(node => node.tag ? [node.tag, node.attrs.text] : node), [["p", hint]]);
statusRefs = freshRefs();
eq("error result", renderStartupBlock({tracking_startup: {state: "error"}}), true);
eq("error title", statusRefs.title.textContent, "Survey storage unavailable");
"""
        out = run_app_node(("function renderStartupBlock(",), body, stubs)
        self.assertEqual(out["failures"], [])


def app_line(prefix):
    lines = [line for line in APP_JS.split("\n") if line.startswith(prefix)]
    if len(lines) != 1:
        raise AssertionError("expected one line starting with %s, found %d" % (prefix, len(lines)))
    return lines[0]


class LiveArrivalTest(unittest.TestCase):
    """E9: every live snapshot arrives through acceptLive; views bind with the route signal."""

    def test_live_arrives_at_one_place(self):
        stubs = r"""
const log = [];
const store = {state: {}, set(patch) { Object.assign(this.state, patch); log.push("set"); }};
function updateGlobalStatus(live) { log.push(store.state.live === live ? "global" : "global before set"); }
let connectionState = {label: "Device unreachable", state: "error"};
"""
        body = r"""
const c1 = new AbortController(), c2 = new AbortController(), calls = [];
bindLive(c1.signal, live => { log.push("first"); calls.push(["first", live]); });
bindLive(c2.signal, live => { log.push("second"); calls.push(["second", live]); });
const L = {revision: 7};
acceptLive(L);
eq("each binding once with the snapshot", calls.map(([name, live]) => [name, live === L]), [["first", true], ["second", true]]);
eq("the store holds the snapshot", store.state.live === L, true);
eq("the connection is ok", connectionState.state, "ok");
eq("store, global status, then the bindings", log, ["set", "global", "first", "second"]);
calls.length = 0;
c1.abort();
acceptLive({revision: 8});
eq("an aborted binding is gone", calls.map(([name]) => name), ["second"]);
const gone = new AbortController();
gone.abort();
let late = 0;
bindLive(gone.signal, () => { late += 1; });
acceptLive({revision: 9});
eq("a binding of an aborted signal never runs", late, 0);
"""
        out = run_app_node(("const liveBindings=", "function bindLive(", "function renderLiveBindings(",
                            "function acceptLive("), body, stubs)
        self.assertEqual(out["failures"], [])

    def test_poll_and_map_use_accept_live(self):
        poll = app_line("async function pollLive(")
        self.assertIn("acceptLive(live)", poll)
        self.assertNotIn("updateStatus(", poll)
        self.assertNotIn("store.set({live})", poll)
        with open(os.path.join(ROOT, "src", "70-project-detail.js"), encoding="utf-8") as source:
            detail = source.read()
        self.assertIn("acceptLive(next)", detail)
        self.assertNotIn("store.set({live:next})", detail)
        self.assertIn("bindLive(signal,measureLiveBinding)", app_line("async function mountMeasure("))
        known = subprocess.run(["git", "cat-file", "-e", TASK0_SHA + "^{commit}"], cwd=ROOT, capture_output=True)
        if known.returncode != 0:
            self.skipTest("the Task 0 commit is not in this checkout")
        before = subprocess.run(["git", "show", TASK0_SHA + ":src/40-tracking-actions.js"], cwd=ROOT,
                                capture_output=True, check=True).stdout
        with open(os.path.join(ROOT, "src", "40-tracking-actions.js"), "rb") as source:
            now = source.read()
        renew = b"if(!delta)return result;invalidateLive();"
        self.assertEqual(now.count(renew), 1)
        self.assertEqual(now.replace(renew, b"if(!delta)return result;"), before,
                         "src/40-tracking-actions.js stays unchanged (E9) apart from the #27 renewal")


# Measure action model (E8): the gate helpers of app.js plus the model's own checks.
MODEL_PREFIXES = GATE_DECLARATION_PREFIXES + (
    "const V2_DE=", "function localText(", "function measurementApproved(", "function storageBlocked(",
    "function measurementPhaseText(", "function measurementTimeText(", "const formatClock=",
    "function measureActionModel(")
MODEL_FIXTURE = r"""
const en = dict.en;
const H30 = "The measurement is still waiting for stable values.";
const H60 = "The measurement has taken more than one minute. Check the antenna view and correction stream.";
const limits = {fix_quality: "RTK_FIXED", min_satellites: 10, max_hdop: 1.5, max_correction_age_sec: 5,
  max_gst_horizontal_sigma_m: 0.05, max_gst_vertical_sigma_m: 0.1, max_horizontal_spread_m: 0.03};
const okFix = {qual: 4, fix_status_text: "RTK_FIXED", sats: 14, hdop: 0.7, lat: 50, lon: 8, alt: 100,
  correction_age_sec: 1.2, fix_seq: 3, stale: false, receiver_accuracy: {horizontal_sigma_m: 0.011, altitude_sigma_m: 0.02}};
const fixture = extra => Object.assign({current_fix: okFix, current_quality: {accepted: true, reasons: [], limits},
  gnss_accuracy: {state: "receiving", attempts: 0}, rtcm: {epoch_age_sec: 1, arrival_age_sec: 0.2, stale: false},
  network: {ntrip_state: "streaming"}, capacity: {level: "ok", writable: true},
  board: {fs_total_bytes: 1000000, fs_free_bytes: 600000}, quality_streak: {gate_streak_sec: 20},
  measurement_progress: {active: false, collected: 0, total: 5},
  active_project: {id: "p1", name: "Project", line_count: 1, point_count: 2}, active_line: null}, extra || {});
const recordingLine = {id: "l1", name: "Line", state: "recording", recording_mode: "survey", vertex_count: 2, length_m: 1.5};
const ids = model => model.buttons.map(button => button.id);
const button = (model, id) => model.buttons.find(item => item.id === id) || {};
"""


class MeasureActionModelTest(unittest.TestCase):
    def test_measure_action_model(self):
        body = r"""
let m = measureActionModel(fixture({active_project: null}));
eq("1 no project: buttons", ids(m), ["choose-project"]);
eq("1 no project: reason", m.reason, en["ui.no_project_hint"]);
eq("1 no project: no resume", m.resume, null);
m = measureActionModel(fixture());
eq("2 ready: buttons", ids(m), ["start-line", "object-point", "undo-last"]);
eq("2 ready: all free", m.buttons.map(item => item.disabled), [false, false, false]);
eq("2 ready: no reason", m.reason, null);
eq("2 ready: kinds", m.buttons.map(item => item.kind), ["primary", "secondary", "danger ghost"]);
m = measureActionModel(fixture({current_quality: {accepted: false, reasons: ["rtk_fixed_required"], limits}}));
eq("3 gate closed: start line stays free", button(m, "start-line").disabled, false);
eq("3 gate closed: object point locked", button(m, "object-point").disabled, true);
eq("3 gate closed: route hint", m.reason, en["ui.start_line_route_hint"]);
m = measureActionModel(fixture({capacity: {level: "ok", writable: false}}));
eq("4 storage blocked: start line locked", button(m, "start-line").disabled, true);
eq("4 storage blocked: object point locked", button(m, "object-point").disabled, true);
eq("4 storage blocked: reason", m.reason, "Export or optimize survey data before recording more points.");
m = measureActionModel(fixture({active_line: recordingLine}));
eq("5 recording: buttons", ids(m), ["add-vertex", "object-point", "line-state", "finish-line", "undo-last"]);
eq("5 recording: all free", m.buttons.map(item => item.disabled), [false, false, false, false, false]);
eq("5 recording: pause offered", button(m, "line-state").label, "Pause");
eq("5 recording: no reason", m.reason, null);
m = measureActionModel(fixture({active_line: Object.assign({}, recordingLine, {state: "paused"})}));
eq("6 paused: add line point locked", button(m, "add-vertex").disabled, true);
eq("6 paused: resume offered", button(m, "line-state").label, "Resume");
eq("6 paused: reason", m.reason, "Resume the line before recording another point.");
for (const [elapsed, hint] of [[3, null], [31, H30], [61, H60]]) {
  m = measureActionModel(fixture({active_line: recordingLine,
    measurement_progress: {active: true, phase: "collecting", collected: 2, total: 5, elapsed_sec: elapsed, rejected_samples: 1}}));
  eq("7 running " + elapsed + " s: hint", m.resume && m.resume.hint, hint);
  eq("7 running " + elapsed + " s: summary", typeof (m.resume && m.resume.summary), "string");
  eq("7 running " + elapsed + " s: only danger stays free",
     m.buttons.map(item => [item.id, item.disabled]),
     [["add-vertex", true], ["object-point", true], ["line-state", true], ["finish-line", false], ["undo-last", false]]);
}
const moving = fixture({active_line: recordingLine});
const changed = fixture({active_line: recordingLine, quality_streak: {gate_streak_sec: 47},
  current_fix: Object.assign({}, okFix, {sats: 11, correction_age_sec: 3.4,
    receiver_accuracy: {horizontal_sigma_m: 0.019, altitude_sigma_m: 0.02}})});
eq("8 sats, sigma, correction age and streak keep the key", measureActionModel(changed).key, measureActionModel(moving).key);
const running = fixture({active_line: recordingLine, measurement_progress: {active: true, collected: 1, total: 5, elapsed_sec: 3}});
eq("8 a running measurement keeps the key", measureActionModel(running).key, measureActionModel(moving).key);
const other = fixture({active_line: Object.assign({}, recordingLine, {id: "l2"})});
eq("8 another line changes the key", measureActionModel(other).key === measureActionModel(moving).key, false);
"""
        out = run_app_node(MODEL_PREFIXES, MODEL_FIXTURE + body)
        self.assertEqual(out["failures"], [])

    def test_measurement_resume_follows_the_model(self):
        stubs = r"""
const camel = name => name.replace(/-([a-z])/g, (_, c) => c.toUpperCase());
const document = {activeElement: null};
let root = null;
class El {
  constructor(tag) { Object.assign(this, {tagName: tag.toUpperCase(), children: [], parentNode: null, dataset: {}, attrs: {},
    className: "", disabled: false, hidden: false, own: "", listeners: {}}); }
  get classList() { const el = this; return {contains: name => el.className.split(/\s+/).includes(name)}; }
  get textContent() { return this.own + this.children.map(node => node.textContent).join(""); }
  set textContent(value) { this.children.forEach(node => { node.parentNode = null; }); this.children = []; this.own = String(value); }
  append(...nodes) { for (const node of nodes.flat()) { if (node.parentNode) node.remove(); node.parentNode = this; this.children.push(node); } }
  prepend(...nodes) { for (const node of nodes.flat().reverse()) { if (node.parentNode) node.remove(); node.parentNode = this; this.children.unshift(node); } }
  before(...nodes) { const parent = this.parentNode; for (const node of nodes.flat()) { if (node.parentNode) node.remove(); node.parentNode = parent; parent.children.splice(parent.children.indexOf(this), 0, node); } }
  remove() { if (this.parentNode) { const list = this.parentNode.children; list.splice(list.indexOf(this), 1); this.parentNode = null; } }
  setAttribute(key, value) { if (key.startsWith("data-")) this.dataset[camel(key.slice(5))] = String(value); else this.attrs[key] = String(value); }
  getAttribute(key) { return key.startsWith("data-") ? this.dataset[camel(key.slice(5))] ?? null : this.attrs[key] ?? null; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  contains(node) { for (let item = node; item; item = item.parentNode) if (item === this) return true; return false; }
  get isConnected() { let node = this; while (node.parentNode) node = node.parentNode; return node === root; }
  focus() { document.activeElement = this; }
  matches(selector) {
    const parts = selector.match(/^([a-z]*)((?:\.[\w-]+|\[[\w-]+(?:="[^"]*")?\]|:not\(\[[\w-]+\]\))*)$/);
    if (!parts) throw new Error("selector " + selector);
    if (parts[1] && this.tagName !== parts[1].toUpperCase()) return false;
    for (const token of parts[2].match(/\.[\w-]+|\[[\w-]+(?:="[^"]*")?\]|:not\(\[[\w-]+\]\)/g) || []) {
      if (token[0] === ".") { if (!this.classList.contains(token.slice(1))) return false; continue; }
      const negated = token.startsWith(":not("), [, name, value] = token.match(/\[([\w-]+)(?:="([^"]*)")?\]/);
      const actual = name === "disabled" ? (this.disabled ? "" : null) : this.getAttribute(name);
      const hit = actual !== null && (value === undefined || actual === value);
      if (hit === negated) return false;
    }
    return true;
  }
  querySelectorAll(selector) {
    const direct = selector.startsWith(":scope > "), simple = direct ? selector.slice(9) : selector, found = [];
    const walk = node => { for (const child of node.children) { if (child.matches(simple)) found.push(child); if (!direct) walk(child); } };
    walk(this);
    return found;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}
function h(tag, attrs, ...children) {
  const node = new El(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (key === "class") node.className = value; else if (key === "text" || key === "textRaw") node.textContent = value;
    else if (key === "hidden") node.hidden = value; else node.setAttribute(key, value);
  }
  node.append(...children.flat().filter(child => child instanceof El));
  return node;
}
function iconButton(label, handler, kind) { const node = h("button", {class: "btn " + (kind || "secondary"), text: label}); node.addEventListener("click", handler); return node; }
const MEASURE_ACTIONS = {};
let cancelAnswer = null;
const shownErrors = [];
function trackingAction(action) { return cancelAnswer.promise; }
function showError(error) { shownErrors.push(error.message); }
"""
        body = r"""
root = new El("body");
const bar = h("div", {class: "field-actions"}), strip = h("div", {class: "capture-strip"});
root.append(bar);
bar.append(strip);
const kinds = {"add-vertex": "primary", "object-point": "secondary", "line-state": "secondary", "finish-line": "danger", "undo-last": "danger ghost"};
const model = (running, reason) => ({key: "line-l1", reason: reason || null,
  buttons: Object.entries(kinds).map(([id, kind]) => ({id, label: id, kind, disabled: running && !kind.includes("danger")}))});
const actions = () => bar.querySelectorAll(":scope > button");
const H30 = "half a minute", H60 = "a minute";
renderActionBar(bar, model(false));
renderMeasurementResume(bar, null);
const marked = actions();
marked.forEach((node, index) => { node.__probe = index; });
eq("five action buttons", marked.map(node => node.dataset.action), Object.keys(kinds));
eq("the strip stays last", bar.children[bar.children.length - 1] === strip, true);
const steps = [{summary: "s1", hint: null}, {summary: "s2", hint: H30}, {summary: "s3", hint: H60}];
let cancel = null;
steps.forEach((resume, index) => {
  const name = "step " + (index + 2);
  renderActionBar(bar, model(true));
  renderMeasurementResume(bar, resume);
  const blocks = bar.querySelectorAll(":scope > .measurement-resume");
  eq(name + ": one resume block", blocks.length, 1);
  eq(name + ": resume block first", bar.children[0] === blocks[0], true);
  const block = blocks[0] || h("div", {});
  const button = block.querySelector("button");
  if (index === 0) cancel = button;
  eq(name + ": same cancel button", button !== null && button === cancel, true);
  eq(name + ": cancel button tagged", button && button.getAttribute("data-measurement-cancel"), "");
  const summary = block.querySelector(".resume-summary"), hint = block.querySelector(".resume-hint");
  eq(name + ": summary", summary && summary.textContent, resume.summary);
  eq(name + ": hint", hint && (hint.hidden ? "hidden" : hint.textContent), resume.hint === null ? "hidden" : resume.hint);
  eq(name + ": action buttons kept", actions().length === marked.length && marked.every((node, i) => actions()[i] === node), true);
  eq(name + ": non-danger buttons locked", actions().map(node => node.disabled), [true, true, true, false, false]);
  // Review #29b (2): the cancel button follows the state on every pass: locked while its request is pending, free again
  // after a lost connection had locked it.
  if (index === 0) eq("step 2: cancel free while no request is pending", cancel && cancel.disabled, false);
  if (index === 0) measurementCancelPending = true;
  if (index === 1) { eq("step 3: cancel locked while its request is pending", cancel && cancel.disabled, true); measurementCancelPending = false; if (cancel) cancel.disabled = true; }
  if (index === 2) eq("step 4: cancel free again after a lock from outside", cancel && cancel.disabled, false);
});
if (cancel) { cancel.disabled = false; cancel.focus(); }
renderActionBar(bar, model(false));
renderMeasurementResume(bar, null);
eq("step 5: resume block removed", bar.querySelectorAll(":scope > .measurement-resume").length, 0);
eq("step 5: action buttons kept", actions().length === marked.length && marked.every((node, i) => actions()[i] === node), true);
eq("step 5: action buttons free", actions().map(node => node.disabled), [false, false, false, false, false]);
eq("step 5: focus on the first free action button", document.activeElement === marked[0], true);
renderActionBar(bar, model(false, "Resume the line"));
eq("reason added in place", (bar.querySelector(":scope > .action-reason") || {}).textContent, "Resume the line");
eq("reason before the strip", bar.children.indexOf(bar.querySelector(":scope > .action-reason")) < bar.children.indexOf(strip), true);
renderActionBar(bar, model(false));
eq("reason removed in place", bar.querySelector(":scope > .action-reason"), null);
eq("buttons kept with reason changes", marked.every((node, i) => actions()[i] === node), true);
renderActionBar(bar, Object.assign(model(false), {key: "other"}));
eq("a new key rebuilds the buttons", actions().some(node => marked.includes(node)), false);
eq("a rebuild keeps the strip", bar.children[bar.children.length - 1] === strip, true);
clearActionBar(bar);
eq("clearing forgets the key", bar.dataset.actionKey, undefined);
"""
        out = run_app_node(("let measurementCancelPending=", "function renderMeasurementResume(", "function renderActionBar(",
                            "function clearActionBar("), body, stubs)
        self.assertEqual(out["failures"], [])
        # A click marks the request pending until it ends; a failure still shows its error.
        click = r"""
root = new El("body");
const bar = h("div", {class: "field-actions"});
root.append(bar);
renderMeasurementResume(bar, {summary: "s", hint: null});
const cancel = bar.querySelector("[data-measurement-cancel]");
let settle;
cancelAnswer = {promise: new Promise((resolve, reject) => { settle = reject; })};
const run = cancel.listeners.click[0]({currentTarget: cancel});
eq("the click locks the button", cancel.disabled, true);
eq("the request is pending", measurementCancelPending, true);
renderMeasurementResume(bar, {summary: "s2", hint: null});
eq("a pass during the request keeps it locked", cancel.disabled, true);
settle(new Error("Device busy"));
await run;
eq("a failed cancel shows its error", shownErrors, ["Device busy"]);
eq("the request is no longer pending", measurementCancelPending, false);
renderMeasurementResume(bar, {summary: "s3", hint: null});
eq("the next pass frees the same button", [cancel.disabled, bar.querySelector("[data-measurement-cancel]") === cancel], [false, true]);
cancelAnswer = {promise: Promise.resolve({})};
await cancel.listeners.click[0]({currentTarget: cancel});
eq("a finished cancel is no longer pending", measurementCancelPending, false);
"""
        out = run_app_node(("let measurementCancelPending=", "function renderMeasurementResume("), click, stubs,
                           asynchronous=True)
        self.assertEqual(out["failures"], [])


class LoadingStatusTest(unittest.TestCase):
    """E10: long loads show what is loading and the elapsed seconds; only whole pages offer to cancel."""

    def test_loading_elapsed_seconds(self):
        body = r"""
for (const [start, now, expected] of [[0, 0, null], [0, 999, null], [0, 1000, 1], [0, 3999, 3], [0, 8500, 8], [0, 12400, 12],
                                      [500, 1499, null], [500, 1500, 1]])
  eq("elapsed " + start + " -> " + now, loadingElapsedSeconds(start, now), expected);
"""
        out = run_app_node(("const LOADING_TEXT_DELAY_MS=", "function loadingElapsedSeconds("), body)
        self.assertEqual(out["failures"], [])

    def test_skeleton_offers_cancel_only_for_pages(self):
        skeleton = block_of("function skeleton(")
        for needle in ("data-loading-cancel", "LOADING_TEXT_DELAY_MS", "cancelLoading(", "parentNode===main"):
            self.assertIn(needle, skeleton)
        cancel = block_of("function cancelLoading(")
        self.assertIn("controller===activeController", cancel)
        self.assertIn("load-cancelled", cancel)
        self.assertNotIn("setConnection(", cancel)

    def test_cancel_loading_only_for_the_current_route(self):
        stubs = r"""
let activeController = null, renders = 0;
const attrs = {}, shown = [];
const main = {setAttribute(key, value) { attrs[key] = value; }};
function replace(target, ...nodes) { shown.push(nodes); }
function notice(text, kind) { return {notice: text, kind, attrs: {}, setAttribute(key, value) { this.attrs[key] = value; }}; }
function iconButton(label, handler, kind) { return {button: label, handler, kind}; }
function tr(key, fallback) { return fallback; }
function renderRoute() { renders += 1; }
"""
        body = r"""
const stale = new AbortController(), current = new AbortController();
activeController = current;
cancelLoading(stale);
eq("a stale controller changes nothing", [stale.signal.aborted, current.signal.aborted, shown.length], [false, false, 0]);
cancelLoading(current);
eq("the current controller is aborted", current.signal.aborted, true);
eq("one notice with a retry", shown.length, 1);
const [message, retry] = shown[0] || [{attrs: {}}, {handler() {}}];
eq("warning notice", [message.kind, message.attrs["data-notice"]], ["warn", "load-cancelled"]);
eq("nothing was changed", message.notice, "Loading cancelled. Nothing was changed.");
eq("retry button", [retry.button, retry.kind], ["Try again", "primary"]);
retry.handler();
eq("retry renders the route", renders, 1);
eq("the page is no longer busy", attrs["aria-busy"], "false");
cancelLoading(current);
eq("a second cancel does nothing", shown.length, 1);
"""
        out = run_app_node(("function cancelLoading(",), body, stubs)
        self.assertEqual(out["failures"], [])


# E1: the cache declarations of src/17-route-cache.js and the route helpers they key with.
CACHE_PREFIXES = ROUTE_PREFIXES + ("const ROUTE_CACHE_LIMIT=", "const CACHED_VIEWS=", "const routeCache=",
                                   "function cacheGet(", "function cachePut(", "function cacheForget(",
                                   "function forgetTrackingCache(")


class RouteCacheTest(unittest.TestCase):
    """E1-E4: an in-memory cache of the project pages, stale-while-revalidate with a stamp, forgotten on own mutations."""

    def test_route_cache_keys_and_limits(self):
        body = r"""
const route = id => ({view: "project", path: "projects/" + id});
const stored = cachePut(route("p1"), 1);
eq("cachePut returns the entry", Boolean(stored) && stored.data, 1);
const hit = cacheGet(route("p1"));
eq("a stored entry comes back", hit && hit.data, 1);
eq("with the time it was saved", typeof (hit && hit.savedAt), "number");
eq("the limit", ROUTE_CACHE_LIMIT, 6);
for (let n = 2; n <= 7; n += 1) cachePut(route("p" + n), n);
eq("the seventh entry drops the oldest", cacheGet(route("p1")), null);
eq("six entries are kept", routeCache.size, 6);
cacheGet(route("p2"));
cachePut(route("p8"), 8);
eq("a hit moves to the end, so p3 is the oldest", cacheGet(route("p3")), null);
const kept = cacheGet(route("p2"));
eq("p2 stays", kept && kept.data, 2);
eq("the key is the canonical path", cacheGet(resolveRoute("#projects/p2")) && cacheGet(resolveRoute("#projects/p2")).data, 2);
"""
        out = run_app_node(CACHE_PREFIXES, body)
        self.assertEqual(out["failures"], [])

    def test_route_cache_never_holds_live_views(self):
        body = r"""
for (const view of ["device", "measure", "field-setup", "connections", "display", "diagnostics", "rtcm", "log", "console",
                    "maintenance", "project-stakeout"]) {
  const route = {view, path: routeHash(view, {id: "p1"})};
  eq(view + ": not stored", cachePut(route, {kept: true}), null);
  eq(view + ": nothing comes back", cacheGet(route), null);
}
eq("nothing was stored", routeCache.size, 0);
eq("exactly the project overviews and the project page", [...CACHED_VIEWS].sort(), ["archive", "project", "projects"]);
"""
        out = run_app_node(CACHE_PREFIXES, body)
        self.assertEqual(out["failures"], [])

    def test_forget_tracking_cache(self):
        body = r"""
const fill = () => ["#projects", "#archive", "#projects/a%2Fb", "#projects/p2"].forEach(hash => cachePut(resolveRoute(hash), hash));
fill();
eq("four entries", [...routeCache.keys()], ["projects", "archive", "projects/a%2Fb", "projects/p2"]);
forgetTrackingCache("a/b");
eq("with an id: both overviews and that project", [...routeCache.keys()], ["projects/p2"]);
fill();
forgetTrackingCache();
eq("without an id: every project page too", [...routeCache.keys()], []);
"""
        out = run_app_node(CACHE_PREFIXES, body)
        self.assertEqual(out["failures"], [])

    def test_library_signature_covers_what_is_rendered(self):
        """Review #29b (3): the library and archive signature ignores live telemetry and names every rendered field."""
        body = r"""
const tracking = {device_time: 2000, revision: 7, storage_bytes: 2048, active_project_id: "p1",
  active_line: {id: "l1", name: "Line", state: "recording", recording_sec: 30, vertex_count: 3, length_m: 4.2, last_vertex_at: 1990},
  capacity: {used: 31, limit: 5000, level: "ok"}, current_fix: {qual: 4, sats: 17, fix_age_ms: 200}, current_quality: {accepted: true},
  quality_streak: {gate_streak_sec: 20}, measurement_progress: {active: false, collected: 0, total: 5}, storage_operation: null,
  gate_deviations: [], config_warnings: [],
  projects: [{id: "p1", name: "Project", line_count: 1, point_count: 2, progress: "in progress", last_quality_accepted: true,
      created_at: 900, updated_at: 990, frames: {ETRS89: 40, unknown: 2}, mixed_frames: true, unknown_frames: false},
    {id: "p2", name: "Second", line_count: 0, point_count: 0, created_at: 800, frames: {unknown: 3}, mixed_frames: false, unknown_frames: true}],
  archives: [{id: "arc-1", project_id: "p9", name: "Old", point_count: 4, line_count: 1, schema_version: 5, size_bytes: 900, archived_at: 400}]};
const base = trackingSignature(tracking);
const clone = value => JSON.parse(JSON.stringify(value));
const variant = change => { const t = clone(tracking); change(t, t.projects[0], t.archives[0]); return trackingSignature(t); };
eq("a string", typeof base, "string");
const same = {
  "device time": t => { t.device_time = 9999; },
  "revision": t => { t.revision = 99; },
  "current fix": t => { t.current_fix = {qual: 5, sats: 9, fix_age_ms: 900}; },
  "current quality": t => { t.current_quality = {accepted: false, reasons: ["hdop_too_high"]}; },
  "quality streak": t => { t.quality_streak = {gate_streak_sec: 21}; },
  "measurement progress": t => { t.measurement_progress = {active: true, collected: 3, total: 5, elapsed_sec: 4}; },
  "active line timing": t => { Object.assign(t.active_line, {recording_sec: 31, vertex_count: 4, length_m: 5.1, last_vertex_at: 1999}); },
  "storage operation": t => { t.storage_operation = {kind: "compact", running: true}; },
  "capacity": t => { t.capacity = {used: 32, limit: 5000, level: "ok"}; },
  "gate deviations and warnings": t => { t.gate_deviations = [{key: "max_hdop"}]; t.config_warnings = ["x"]; },
  "a journal size within the shown 0.1 KB": t => { t.storage_bytes = 2050; },
  "more points in the only frame": (t, p1) => { t.projects[1].frames = {unknown: 9}; },
  "archive size and time": (t, p1, a) => { a.size_bytes = 1000; a.archived_at = 500; },
};
for (const [name, change] of Object.entries(same)) eq(name + " keeps the signature", variant(change), base);
const differs = {
  "a rename": (t, p1) => { p1.name = "Renamed"; },
  "a line count": (t, p1) => { p1.line_count = 2; },
  "a point count": (t, p1) => { p1.point_count = 3; },
  "the progress": (t, p1) => { p1.progress = "finished"; },
  "the last quality": (t, p1) => { p1.last_quality_accepted = false; },
  "the update time": (t, p1) => { p1.updated_at = 995; },
  "a new project": t => { t.projects.push({id: "p3", name: "New", line_count: 0, point_count: 0, created_at: 1000}); },
  "another active project": t => { t.active_project_id = "p2"; },
  "the end of the active line": t => { t.active_line = null; },
  "the journal size": t => { t.storage_bytes = 4096; },
  "a mixed tally": (t, p1) => { p1.frames.ETRS89 = 41; },
  "frames no longer unknown": t => { Object.assign(t.projects[1], {frames: {ETRS89: 3}, unknown_frames: false}); },
  "an archive rename": (t, p1, a) => { a.name = "New"; },
  "an archive count": (t, p1, a) => { a.point_count = 5; },
  "an archive schema": (t, p1, a) => { a.schema_version = 4; },
  "another archive": t => { t.archives.push({id: "arc-2", project_id: "p8", name: "Older", point_count: 1, line_count: 0, schema_version: 5}); },
};
for (const [name, change] of Object.entries(differs)) eq(name + " changes the signature", variant(change) !== base, true);
"""
        out = run_app_node(("const fmt=", "function trackingSignature("), body)
        self.assertEqual(out["failures"], [])

    def test_stale_stamp_text(self):
        stubs = r"""
const realI18n = globalThis.RtkI18n;
let language = "en";
globalThis.RtkI18n = {getLanguage: () => language, t(key, values) {
  let text = realI18n.dictionaries[language][key] || key;
  Object.keys(values || {}).forEach(name => { text = text.split("{" + name + "}").join(values[name]); });
  return text;
}};
"""
        body = r"""
const savedAt = Date.UTC(2026, 9, 29, 8, 5, 9);
const clock = locale => new Date(savedAt).toLocaleTimeString(locale, {hour: "2-digit", minute: "2-digit", second: "2-digit"});
for (const key of ["ui.cached_as_of", "ui.cached_refreshing", "ui.cached_failed"])
  eq(key + " in both dictionaries", [typeof dict.en[key], typeof dict.de[key]], ["string", "string"]);
const asOf = lang => String(dict[lang]["ui.cached_as_of"]).split("{time}")[0];
let text = staleStampText(savedAt, "refreshing");
eq("EN starts with the as-of text", text.startsWith(asOf("en")) && asOf("en").length > 0, true);
eq("EN refreshing", text, asOf("en") + clock("en-GB") + " · " + dict.en["ui.cached_refreshing"]);
eq("EN ends with the refreshing text", text.endsWith(dict.en["ui.cached_refreshing"]), true);
eq("EN failed", staleStampText(savedAt, "failed").endsWith(dict.en["ui.cached_failed"]), true);
language = "de";
text = staleStampText(savedAt, "failed");
eq("DE failed", text, asOf("de") + clock("de-DE") + " · " + dict.de["ui.cached_failed"]);
eq("DE refreshing", staleStampText(savedAt, "refreshing").endsWith(dict.de["ui.cached_refreshing"]), true);
"""
        out = run_app_node(("const tr=", "function staleStampText("), body, stubs)
        self.assertEqual(out["failures"], [])

    def test_swr_mount_paths(self):
        stubs = r"""
const log = [];
const main = {main: true};
function replace(target, ...nodes) { log.push(["replace", target === main ? "main" : "other", nodes.length]); }
function skeleton(label) { log.push(["skeleton", label]); return {skeleton: label}; }
let stamps = 0;
function staleStamp(savedAt) { const stamp = {stamp: ++stamps, savedAt}; log.push(["stamp", stamp.stamp]); return stamp; }
function setStampState(stamp, state) { log.push(["state", stamp ? stamp.stamp : null, state]); }
"""
        body = r"""
const route = {view: "projects", path: "projects"};
const kinds = kind => log.filter(entry => entry[0] === kind);
const deferred = () => { const d = {}; d.promise = new Promise((resolve, reject) => { d.resolve = resolve; d.reject = reject; }); return d; };
const spec = load => ({label: "Loading projects", load: signal => { log.push(["load"]); return load(signal); },
  render: (data, stamp) => log.push(["render", data, stamp ? stamp.stamp : null]),
  signature: data => JSON.stringify(data.items), fresh: data => log.push(["fresh", data.items])});
// (1) no entry: the skeleton, then the data without a stamp; the entry is stored.
let result = await swrMount(route, new AbortController().signal, spec(async () => ({items: [1]})));
eq("(1) one skeleton", kinds("skeleton"), [["skeleton", "Loading projects"]]);
eq("(1) the skeleton fills main", kinds("replace"), [["replace", "main", 1]]);
eq("(1) one render without a stamp", kinds("render"), [["render", {items: [1]}, null]]);
eq("(1) no stamp state", kinds("state"), []);
eq("(1) fresh once", kinds("fresh"), [["fresh", [1]]]);
eq("(1) the result", result, {items: [1]});
eq("(1) the entry is stored", cacheGet(route) && cacheGet(route).data, {items: [1]});
// (2a) entry, same signature: the cached render comes before the load answers, then only the stamp turns fresh.
log.length = 0;
let held = deferred();
let pending = swrMount(route, new AbortController().signal, spec(() => held.promise));
eq("(2a) the cached render is synchronous", kinds("render"), [["render", {items: [1]}, 1]]);
eq("(2a) the cached render comes before the load", log.findIndex(entry => entry[0] === "render") < log.findIndex(entry => entry[0] === "load"), true);
eq("(2a) no skeleton", kinds("skeleton"), []);
held.resolve({items: [1], device_time: 5});
result = await pending;
eq("(2a) no second render", kinds("render").length, 1);
eq("(2a) the stamp turns fresh", kinds("state"), [["state", 1, "fresh"]]);
eq("(2a) fresh once", kinds("fresh"), [["fresh", [1]]]);
eq("(2a) the result", result, {items: [1], device_time: 5});
// (2b) entry, another signature: render again into the same stamp, then fresh.
log.length = 0;
held = deferred();
pending = swrMount(route, new AbortController().signal, spec(() => held.promise));
held.resolve({items: [2]});
await pending;
eq("(2b) cached, then new data, both with the same stamp", kinds("render"), [["render", {items: [1], device_time: 5}, 2], ["render", {items: [2]}, 2]]);
eq("(2b) one stamp", kinds("stamp"), [["stamp", 2]]);
eq("(2b) fresh after the second render", log.map(entry => entry[0]).filter(kind => kind === "render" || kind === "state"), ["render", "render", "state"]);
eq("(2b) the stamp turns fresh", kinds("state"), [["state", 2, "fresh"]]);
// (3) entry, the load fails: the content stays, the stamp says failed, no throw.
log.length = 0;
const unavailable = Object.assign(new Error("Temporarily unavailable"), {status: 503});
result = await swrMount(route, new AbortController().signal, spec(async () => { throw unavailable; }));
eq("(3) only the cached render", kinds("render"), [["render", {items: [2]}, 3]]);
eq("(3) the stamp says failed", kinds("state"), [["state", 3, "failed"]]);
eq("(3) fresh never", kinds("fresh"), []);
eq("(3) the result", result, null);
// (4) no entry, the load fails: the error goes to the router as before.
cacheForget("projects");
log.length = 0;
let thrown = null;
try { await swrMount(route, new AbortController().signal, spec(async () => { throw unavailable; })); } catch (error) { thrown = error; }
eq("(4) the same error", thrown === unavailable, true);
eq("(4) no stamp state", kinds("state"), []);
// (5) entry, the signal aborts during the load: nothing after the abort.
cachePut(route, {items: [5]});
for (const ending of ["answer", "abort error"]) {
  log.length = 0;
  const controller = new AbortController();
  held = deferred();
  pending = swrMount(route, controller.signal, spec(() => held.promise));
  controller.abort();
  if (ending === "answer") held.resolve({items: [6]});
  else held.reject(Object.assign(new Error("Request cancelled"), {name: "AbortError"}));
  result = await pending;
  eq("(5) " + ending + ": only the cached render", kinds("render"), [["render", {items: [5]}, stamps]]);
  eq("(5) " + ending + ": no stamp state", kinds("state"), []);
  eq("(5) " + ending + ": fresh never", kinds("fresh"), []);
  eq("(5) " + ending + ": the result", result, null);
}
// (5b) no entry, aborted during the load: the skeleton stays, no render.
cacheForget("projects");
log.length = 0;
const controller = new AbortController();
held = deferred();
pending = swrMount(route, controller.signal, spec(() => held.promise));
controller.abort();
held.resolve({items: [7]});
result = await pending;
eq("(5b) no render after the abort", kinds("render"), []);
eq("(5b) nothing stored", cacheGet(route), null);
eq("(5b) the result", result, null);
"""
        out = run_app_node(CACHE_PREFIXES + ("async function swrMount(",), body, stubs, asynchronous=True)
        self.assertEqual(out["failures"], [])

    def test_mutations_forget_the_cache(self):
        # E4: each own mutation forgets after its successful write and before it navigates or renders the route again.
        cases = (
            ("async function renameProject(", "forgetTrackingCache(project.id)", "await trackingAction(", "location.hash="),
            ("async function deleteProject(", "forgetTrackingCache(project.id)", "await trackingAction(", "location.hash="),
            ("async function archiveProject(", "forgetTrackingCache(project.id)", "await trackingAction(", "location.hash="),
            ("async function reactivateArchive(", "forgetTrackingCache(item.project_id)", "await trackingAction(", "renderRoute()"),
            ("async function assignReferenceFrame(", "forgetTrackingCache(project.id)",
             'await trackingAction("assign_reference_frame"', "renderRoute()"),
            ("async function revokeReferenceFrame(", "forgetTrackingCache(project.id)",
             'await trackingAction("revoke_reference_frame"', "renderRoute()"),
            ("async function createProject(", "forgetTrackingCache()", "await trackingAction(", "location.hash="),
            ("async function restoreSurvey(", "forgetTrackingCache()", "await maintenanceAction(", "renderRoute()"),
            ("async function compactSurvey(", "forgetTrackingCache()", "await maintenanceAction(", "renderRoute()"),
            # The project view's activate closure delegates to activateProject (#24-UI), which writes and renders again.
            ("async function activateProject(", "forgetTrackingCache()", "await trackingAction(", "renderRoute()"),
        )
        for prefix, call, after, before in cases:
            with self.subTest(prefix):
                block = block_of(prefix)
                start = block.index(after)
                self.assertIn(call, block[start:], "forgets after the write")
                self.assertLess(block.index(call, start), block.index(before, start), "forgets before it navigates")
        self.assertIn("const activate=()=>activateProject(project,signal)", block_of("async function mountProjectDetail("))
        choose = block_of("async function chooseCapacityProject(")
        self.assertIn("forgetTrackingCache()", choose[choose.index("await archiveProject("):])
        with open(os.path.join(ROOT, "src", "40-tracking-actions.js"), encoding="utf-8") as handle:
            self.assertNotIn("forgetTrackingCache", handle.read())



def detail_source():
    with open(os.path.join(ROOT, "src", "70-project-detail.js"), encoding="utf-8") as handle:
        return handle.read()


def refresh_map_block():
    """The refreshMap closure of mountProjectDetail, from its declaration to its closing line."""
    detail = detail_source()
    start = detail.index("const refreshMap=")
    return detail[start:detail.index("\n  };", start) + 5]


class ProjectMapCacheTest(unittest.TestCase):
    """E5, E6, E14: the project map reopens from the cache, takes deltas in place and reloads in full when the history breaks."""

    def test_map_gestures_pan_and_zoom(self):
        stubs = r"""
const svgStub = () => { const handlers = {}; return {handlers, captured: [],
  addEventListener(type, fn, options) { (handlers[type] = handlers[type] || []).push({fn, options}); },
  setPointerCapture(id) { this.captured.push(id); }, releasePointerCapture() {},
  fire(type, event) { (handlers[type] || []).forEach(entry => entry.fn(event)); }}; };
const free = {closest: () => null};
const on = className => ({closest: selector => selector.split(",").map(part => part.trim()).includes(className) ? {} : null});
const pointer = (id, x, y, target) => ({pointerId: id, clientX: x, clientY: y, target: target || free});
"""
        body = r"""
// One pointer pans by the distance moved.
let svg = svgStub(), state = {zoom: 1, panX: 5, panY: 7}, changes = [];
bindMapGestures(svg, state, kind => changes.push(kind));
svg.fire("pointerdown", pointer(1, 100, 100));
svg.fire("pointermove", pointer(1, 130, 80));
eq("drag pans by (30, -20)", [state.panX, state.panY], [35, -13]);
eq("drag reports pan", changes.includes("pan"), true);
eq("the pointer is captured", svg.captured, [1]);
svg.fire("pointerup", pointer(1, 130, 80));
svg.fire("pointermove", pointer(1, 200, 200));
eq("after release nothing moves", [state.panX, state.panY], [35, -13]);
// Two pointers moving apart zoom in, never above 4.
svg = svgStub(); state = {zoom: 1, panX: 0, panY: 0}; changes = [];
bindMapGestures(svg, state, kind => changes.push(kind));
svg.fire("pointerdown", pointer(1, 100, 100));
svg.fire("pointerdown", pointer(2, 110, 100));
svg.fire("pointermove", pointer(2, 125, 100));
eq("pinch apart zooms in", state.zoom > 1, true);
eq("pinch reports zoom", changes.includes("zoom"), true);
eq("pinch does not pan", [state.panX, state.panY], [0, 0]);
svg.fire("pointermove", pointer(2, 900, 100));
eq("zoom never above 4", state.zoom, 4);
// The wheel zooms by 0.2 per step, rounded to 0.1, never below 0.5; it is a non-passive listener that prevents scrolling.
svg = svgStub(); state = {zoom: 1, panX: 0, panY: 0}; changes = [];
bindMapGestures(svg, state, kind => changes.push(kind));
let prevented = 0;
const wheel = deltaY => svg.fire("wheel", {deltaY, preventDefault() { prevented += 1; }});
wheel(-10);
eq("wheel up zooms in by 0.2", state.zoom, 1.2);
for (let n = 0; n < 20; n += 1) wheel(10);
eq("20 wheel steps down end at 0.5", state.zoom, 0.5);
eq("each wheel step prevents scrolling", prevented, 21);
eq("the wheel listener is not passive", (svg.handlers.wheel || []).map(entry => entry.options && entry.options.passive), [false]);
eq("wheel reports zoom", changes.includes("zoom"), true);
// A press on a line, a point or a stakeout target is theirs: the map does not move.
for (const className of [".map-point", ".map-line", ".stakeout-target"]) {
  svg = svgStub(); state = {zoom: 1, panX: 0, panY: 0}; changes = [];
  bindMapGestures(svg, state, kind => changes.push(kind));
  svg.fire("pointerdown", pointer(1, 100, 100, on(className)));
  svg.fire("pointermove", pointer(1, 160, 140, on(className)));
  eq(className + ": no pan", [state.panX, state.panY, changes.length, svg.captured.length], [0, 0, 0, 0]);
}
"""
        out = run_app_node(("function bindMapGestures(",), body, stubs)
        self.assertEqual(out["failures"], [])

    def test_map_refresh_kind(self):
        body = r"""
const cases = [
  [{revision: 5}, {revision: 5}, "b1", "none"],
  [{revision: 5}, {revision: 7, system: {boot_id: "b1"}}, "b1", "delta"],
  [{revision: 9}, {revision: 4, system: {boot_id: "b1"}}, "b1", "full"],
  [{revision: 5}, {revision: 5, system: {boot_id: "b2"}}, "b1", "full"],
  [{revision: 5}, {revision: 7, system: {boot_id: "b2"}}, null, "delta"],
  [{}, {revision: 3}, null, "full"],
  [{revision: 5}, {}, "b1", "none"],
];
for (const [map, live, bootId, expected] of cases)
  eq(JSON.stringify([map, live, bootId]), mapRefreshKind(map, live, bootId), expected);
"""
        out = run_app_node(("function mapRefreshKind(",), body)
        self.assertEqual(out["failures"], [])

    def test_map_answers_replace_the_map(self):
        refresh = refresh_map_block()
        self.assertIn("map=mergeMapDelta(map,", refresh)
        self.assertIn("mapRefreshKind(", refresh)
        self.assertIsNone(re.search(r"(?<![=\w])mergeMapDelta\(map,delta\);", refresh), "no merge without assignment")
        self.assertTrue(refresh.split("\n")[1].strip().startswith("if(mapStopped||signal.aborted)return;"),
                        "refreshMap ends at once on a stopped or left view")
        detail = block_of("async function mountProjectDetail(")
        self.assertIn("let map", detail)
        self.assertNotIn("const map=await", detail)
        self.assertTrue(detail.split("\n")[1].strip().startswith("const route=currentRoute();"),
                        "the cache key is taken first")
        body = r"""
const answer = {revision: 9, points: [{id: "b"}]};
const result = mergeMapDelta({revision: 1, points: [{id: "a"}]}, answer);
eq("a full answer is the new map", result === answer, true);
eq("its content", result, {revision: 9, points: [{id: "b"}]});
"""
        out = run_app_node(("function mergeMapDelta(",), body)
        self.assertEqual(out["failures"], [])

    def test_draw_map_uses_the_shared_gestures(self):
        detail = detail_source()
        self.assertIn("bindMapGestures(", detail)
        self.assertNotIn("pointerdown", detail)
        self.assertNotIn('"wheel"', detail)
        refresh = refresh_map_block()
        full_start = refresh.index("if(full){")
        full_end = re.compile(r"}\s*else\{").search(refresh, full_start).start()
        delta = refresh[full_end:]
        self.assertIn("redrawGraphic(", delta, "a delta redraws only the graphic")
        self.assertNotIn("drawMap(", delta)
        self.assertNotIn("drawMap(", refresh[:full_start])
        self.assertIn("drawMap(", refresh[full_start:full_end], "only the full replacement rebuilds the map")
        self.assertIn("forgetTrackingCache()", refresh[full_start:full_end], "a full replacement forgets every project page")
        self.assertIn("cachePut(route,", refresh)
        self.assertIn("function bindMapGestures(", APP_JS)

    def test_a_confirmed_view_renders_once(self):
        """Review #29b (1): a revalidation that found new data renders the confirmed state once, without another read."""
        body = r"""
const route = {view: "project", path: "projects/p1"}, entry = {data: {}}, other = {data: {}};
validatedProjectView = {path: "projects/p1", entry, mapConfirmed: true};
eq("the confirmed entry is taken once", takeValidatedProjectView(route, entry), {path: "projects/p1", entry, mapConfirmed: true});
eq("and only once", takeValidatedProjectView(route, entry), null);
validatedProjectView = {path: "projects/p1", entry, mapConfirmed: false};
eq("a replaced cache entry is not confirmed", takeValidatedProjectView(route, other), null);
eq("a mismatch still uses it up", validatedProjectView, null);
validatedProjectView = {path: "projects/p2", entry, mapConfirmed: true};
eq("another project is not confirmed", takeValidatedProjectView(route, entry), null);
"""
        out = run_app_node(("let validatedProjectView=", "function takeValidatedProjectView("), body)
        self.assertEqual(out["failures"], [])
        mount = block_of("async function mountProjects(")
        self.assertIn("takeValidatedProjectView(route,entry)", mount, "every project mount uses the one-shot up")
        detail = block_of("async function mountProjectDetail(")
        read = detail[detail.index("api.json(\"/api/tracking\""):]
        guard = detail[:detail.index("api.json(\"/api/tracking\"")].rsplit("\n", 1)[-1]
        self.assertIn("!cached.validated", guard, "a confirmed view does not read the tracking state again")
        self.assertNotIn("renderRoute();return;}", read.split("\n")[0],
                         "a changed signature does not re-render at once (a pending map round finishes first)")
        refresh = refresh_map_block()
        self.assertIn("rerender", refresh[refresh.index("finally{"):], "the re-render waits for the map round")

    def test_project_view_signature(self):
        body = r"""
const project = {id: "p1", name: "Project", line_count: 1, point_count: 2, updated_at: 5,
  frames: {unknown: 3}, mixed_frames: false, unknown_frames: true};
const tracking = {device_time: 100, storage_bytes: 2000, active_project_id: "p1", active_line: null,
  projects: [project], control_checks: [{name: "C1", measurements: 2, horizontal_delta_m: 0.01}]};
const base = projectViewSignature(project, tracking);
const clone = value => JSON.parse(JSON.stringify(value));
const variant = change => { const t = clone(tracking), p = t.projects[0]; change(p, t); return projectViewSignature(p, t); };
eq("a string", typeof base, "string");
eq("same for another device time and storage size", variant((p, t) => { t.device_time = 999; t.storage_bytes = 1; }), base);
eq("same for another update time", variant(p => { p.updated_at = 99; }), base);
// Recording adds points to the tally of a single frame: the view shows only that a frame is unknown, not the counts.
eq("same for more points in the only frame", variant(p => { p.frames = {unknown: 9}; }), base);
const mixed = clone(project);
Object.assign(mixed, {frames: {ETRS89: 40, unknown: 2}, mixed_frames: true, unknown_frames: false});
const mixedBase = projectViewSignature(mixed, tracking);
mixed.frames = {ETRS89: 41, unknown: 2};
eq("a mixed tally is shown, so its counts change it", projectViewSignature(mixed, tracking) !== mixedBase, true);
const differs = {
  "a rename": p => { p.name = "Renamed"; },
  "another point count": p => { p.point_count = 3; },
  "another line count": p => { p.line_count = 2; },
  "activation elsewhere": (p, t) => { t.active_project_id = "p2"; },
  "a new active line": (p, t) => { t.active_line = {id: "l9", name: "Line", state: "recording", recording_sec: 3}; },
  "a changed control measurement": (p, t) => { t.control_checks[0].measurements = 3; },
  "a frame assignment": p => { p.frame_assignment = {reference_frame: "ETRS89", count: 3, token: "t"}; p.frames = {ETRS89: 3}; p.unknown_frames = false; },
  "mixed frames": p => { p.mixed_frames = true; },
};
for (const [name, change] of Object.entries(differs)) eq(name + " changes the signature", variant(change) !== base, true);
const assigned = clone(project);
assigned.frame_assignment = {reference_frame: "ETRS89", count: 3, token: "t"};
const withAssignment = projectViewSignature(assigned, tracking);
assigned.frame_assignment = {reference_frame: "ETRS89", count: 3, token: "u"};
eq("an undone and repeated assignment changes it", projectViewSignature(assigned, tracking) !== withAssignment, true);
"""
        out = run_app_node(("function projectViewSignature(",), body)
        self.assertEqual(out["failures"], [])


if __name__ == "__main__":
    unittest.main()
