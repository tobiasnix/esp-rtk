# SPDX-License-Identifier: AGPL-3.0-only
"""#24-UI: one route model for Measure, Projects and Device; old links redirect.

The route helpers run under Node straight from the single-line declarations of app.js.
"""
import json
import os
import re
import subprocess
import unittest

from support import ui
import test_ui

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODE = test_ui.NODE
APP_JS = test_ui.APP_JS
APP_LINES = APP_JS.split("\n")
# Shared Node prelude: loads the dictionaries and offers eq() to collect failed expectations.
NODE_PRELUDE = r"""
"use strict";
globalThis.RtkI18n = require(process.cwd() + "/i18n.js");
const dict = RtkI18n.dictionaries;
const failures = [];
const eq = (name, actual, expected) => {
  if (JSON.stringify(actual) !== JSON.stringify(expected))
    failures.push(name + ": got " + JSON.stringify(actual) + " expected " + JSON.stringify(expected));
};
"""
ROUTE_PREFIXES = ("const MEASURE_ROUTE=", "const ROUTES=", "const LEGACY_ROUTES=", "function resolveRoute(",
                  "function routeHash(")
AREA_PREFIXES = ("const tr=",) + ROUTE_PREFIXES + ("const AREAS=", "const ROUTE_META=", "function contextLinks(")
# Only the alias table itself may name an alias (Task 4: the transitional legacyDeviceMount is gone).
ALIAS_EXEMPT_PREFIXES = ("const LEGACY_ROUTES=",)


def app_declarations(prefixes):
    """One single-line app.js declaration per prefix; a missing one is reported by name."""
    found = []
    for prefix in prefixes:
        lines = [line for line in APP_LINES if line.startswith(prefix)]
        if not lines:
            raise AssertionError("declaration missing: " + prefix)
        if len(lines) != 1:
            raise AssertionError("declaration not unique: " + prefix)
        found.append(lines[0])
    return "\n".join(found)


def run_app_node(prefixes, body, stubs="", asynchronous=False):
    """Run a Node body after the named declarations; stubs come before them, the body uses eq() and fills `out`.

    With asynchronous=True the body runs inside an async function and may use await.
    """
    if NODE is None:
        raise unittest.SkipTest("node is unavailable")
    report = "out.failures = failures;\nconsole.log(JSON.stringify(out));\n"
    if asynchronous:
        body = "(async () => {\n" + body + "\n})().then(() => {\n" + report + \
            "}, error => { console.error(error && error.stack || String(error)); process.exit(1); });\n"
        report = ""
    script = NODE_PRELUDE + stubs + "\n" + app_declarations(prefixes) + "\nconst out = {};\n" + body + \
        "\n" + report
    run = subprocess.run([NODE, "-e", script], cwd=ROOT, capture_output=True, text=True, timeout=20)
    if run.returncode != 0:
        raise AssertionError(run.stderr)
    return json.loads(run.stdout)


def source(name):
    with open(os.path.join(ROOT, "src", name), encoding="utf-8") as handle:
        return handle.read()


class TestRouteModel(unittest.TestCase):

    def test_routes_resolve_canonical_paths(self):
        out = run_app_node(ROUTE_PREFIXES, r"""
const cases = [
  ["", "measure", "measure", {}, "measure"],
  ["#measure", "measure", "measure", {}, "measure"],
  ["#measure/setup", "field-setup", "measure/setup", {}, "measure"],
  ["#measure/setup/frame", "field-setup", "measure/setup/frame", {focus: "frame"}, "measure"],
  ["#measure/setup/method", "field-setup", "measure/setup/method", {focus: "method"}, "measure"],
  ["#measure/setup/bogus", "field-setup", "measure/setup", {}, "measure"],
  ["#measure/x", "measure", "measure", {}, "measure"],
  ["#archive", "archive", "archive", {}, "projects"],
  ["#projects", "projects", "projects", {}, "projects"],
  ["#projects/project-0000abcd-0001", "project", "projects/project-0000abcd-0001", {id: "project-0000abcd-0001"}, "projects"],
  ["#projects/a%2Fb", "project", "projects/a%2Fb", {id: "a/b"}, "projects"],
  ["#projects/", "projects", "projects", {}, "projects"],
  ["#projects/p1/stakeout", "project-stakeout", "projects/p1/stakeout", {id: "p1", rest: []}, "projects"],
  ["#projects/p1/stakeout/target/t%201", "project-stakeout", "projects/p1/stakeout/target/t%201",
   {id: "p1", rest: ["target", "t 1"]}, "projects"],
  ["#/device/connections/access/", "connections", "device/connections/access", {focus: "access"}, "device"],
  ["#device/connections/internet", "connections", "device/connections/internet", {focus: "internet"}, "device"],
  ["#device/connections/bogus", "connections", "device/connections", {}, "device"],
  ["#device", "device", "device", {}, "device"],
  ["#device/display", "display", "device/display", {}, "device"],
  ["#device/diagnostics", "diagnostics", "device/diagnostics", {}, "device"],
  ["#device/diagnostics/rtcm", "rtcm", "device/diagnostics/rtcm", {}, "device"],
  ["#device/diagnostics/log", "log", "device/diagnostics/log", {}, "device"],
  ["#device/diagnostics/console", "console", "device/diagnostics/console", {}, "device"],
  ["#device/maintenance", "maintenance", "device/maintenance", {}, "device"],
  ["#device/nope", "device", "device", {}, "device"],
  ["#measure?x=1", "measure", "measure", {}, "measure"],
  ["#Status", "measure", "measure", {}, "measure"],
  ["#nonsense", "measure", "measure", {}, "measure"],
  ["#archive/x", "archive", "archive", {}, "projects"],
  // A broken escape stays raw (no throw); its canonical form escapes the percent signs.
  ["#projects/%E0%A4%A", "project", "projects/%25E0%25A4%25A", {id: "%E0%A4%A"}, "projects"]];
for (const [hash, view, path, params, root] of cases) {
  let route;
  try { route = resolveRoute(hash); } catch (error) { failures.push(hash + ": threw " + error.message); continue; }
  eq(hash, [route.view, route.path, route.params, route.root], [view, path, params, root]);
  // The canonical path resolves to itself: a replaced hash never loops.
  eq(hash + " is stable", resolveRoute("#" + route.path).path, route.path);
}
""")
        self.assertEqual(out["failures"], [])

    def test_legacy_routes_land_on_their_new_place(self):
        out = run_app_node(ROUTE_PREFIXES, r"""
const expected = {"status": "measure", "device/settings": "device/display",
  "device/configuration": "device/connections/internet", "device/access": "device/connections/access",
  "device/rtcm": "device/diagnostics/rtcm", "device/log": "device/diagnostics/log",
  "device/module": "device/diagnostics/console", "device/label": "device/connections/access"};
eq("aliases", Object.keys(LEGACY_ROUTES).sort(), Object.keys(expected).sort());
for (const [alias, target] of Object.entries(expected)) {
  eq("#" + alias, resolveRoute("#" + alias).path, target);
  eq("#" + alias + " target is not an alias", Object.prototype.hasOwnProperty.call(LEGACY_ROUTES, target), false);
}
eq("diagnostics stays", resolveRoute("#device/diagnostics").path, "device/diagnostics");
eq("label is the access group", [resolveRoute("#device/label").view, resolveRoute("#device/label").params],
  ["connections", {focus: "access"}]);
eq("no label view", ROUTES.some(route => route.view === "label"), false);
const paths = ROUTES.map(route => route.path);
eq("no alias is a route", Object.keys(LEGACY_ROUTES).filter(alias => paths.includes(alias)), []);
""")
        self.assertEqual(out["failures"], [])

    def test_route_hash_round_trips(self):
        out = run_app_node(ROUTE_PREFIXES, r"""
let checked = 0;
for (const route of ROUTES) {
  const base = route.path.includes(":id") ? {id: "a/b c"} : {};
  const variants = [base];
  for (const focus of route.focus || []) variants.push(Object.assign({}, base, {focus}));
  if (route.rest) { variants.length = 0; variants.push(Object.assign({}, base, {rest: []}), Object.assign({}, base, {rest: ["target", "t 1"]})); }
  for (const params of variants) {
    const hash = routeHash(route.view, params), back = resolveRoute("#" + hash);
    eq(route.view + " " + JSON.stringify(params), {view: back.view, params: back.params}, {view: route.view, params});
    eq(route.view + " has no hash sign", hash.startsWith("#"), false);
    checked += 1;
  }
}
eq("project id is one escaped segment", routeHash("project", {id: "a/b c"}), "projects/a%2Fb%20c");
eq("every view checked", checked >= 19, true);
""")
        self.assertEqual(out["failures"], [])

    def test_views_without_a_mount_fall_back(self):
        out = run_app_node(ROUTE_PREFIXES + ("function viewFallback(",), r"""
eq("stakeout", viewFallback(resolveRoute("#projects/p1/stakeout/import")), "projects/p1");
eq("stakeout escaped id", viewFallback(resolveRoute("#projects/a%2Fb/stakeout")), "projects/a%2Fb");
eq("measure", viewFallback(resolveRoute("#measure")), null);
eq("project", viewFallback(resolveRoute("#projects/p1")), null);
""", stubs="const VIEW_MOUNTS = {measure: () => {}, projects: () => {}, project: () => {}};")
        self.assertEqual(out["failures"], [])

    def test_context_links_follow_the_areas(self):
        out = run_app_node(AREA_PREFIXES, r"""
const de = dict.de;
eq("device", contextLinks("device").map(link => link.href),
  ["/#device", "/#device/connections", "/#device/display", "/#device/diagnostics", "/#device/maintenance"]);
eq("measure", contextLinks("measure").map(link => link.href), ["/#measure", "/#measure/setup"]);
eq("projects", contextLinks("projects").map(link => link.href), ["/#projects", "/#archive"]);
eq("views", contextLinks("device").map(link => link.view), ["device", "connections", "display", "diagnostics", "maintenance"]);
RtkI18n.setLanguage("de");
eq("device de", contextLinks("device").map(link => link.label),
  [de["ui.route_device_overview"], de["ui.route_connections"], de["ui.route_display"], de["ui.route_diagnostics"],
   de["ui.route_maintenance"]]);
eq("measure de", contextLinks("measure").map(link => link.label), [de["app.measure"], de["ui.route_field_setup"]]);
eq("projects de", contextLinks("projects").map(link => link.label), [de["app.projects"], de["ui.route_archive"]]);
eq("german differs", de["ui.route_connections"] !== dict.en["ui.route_connections"], true);
RtkI18n.setLanguage("en");
eq("device en", (contextLinks("device")[2] || {}).label, "Operation & display");
eq("unknown area", contextLinks("nowhere"), []);
""")
        self.assertEqual(out["failures"], [])

    def test_every_view_has_a_title_and_kicker(self):
        out = run_app_node(AREA_PREFIXES, r"""
const de = dict.de;
for (const route of ROUTES) {
  const meta = ROUTE_META[route.view];
  if (route.view === "project" || route.view === "project-stakeout") { eq(route.view + " uses the project name", meta, undefined); continue; }
  const texts = meta ? meta() : [];
  eq(route.view + " title and kicker", texts.length === 2 && texts.every(text => typeof text === "string" && text.length > 0), true);
}
RtkI18n.setLanguage("de");
eq("connections de", ROUTE_META.connections(), [de["ui.route_connections"], de["ui.route_connections_kicker"]]);
eq("field setup de", ROUTE_META["field-setup"](), [de["ui.route_field_setup"], de["ui.route_field_setup_kicker"]]);
eq("console de", ROUTE_META.console(), [de["ui.route_console"], de["ui.route_diagnostics"]]);
eq("area labels de", Object.keys(AREAS).map(area => AREAS[area].navLabel()),
  [de["ui.context_nav_measure"], de["ui.context_nav_projects"], de["ui.context_nav_device"]]);
""", stubs="const localText = value => value;")
        self.assertEqual(out["failures"], [])

    def test_sign_in_hash_reads_the_return_parameter(self):
        out = run_app_node(("const signInTarget=", "function signInHash("), r"""
const cases = [
  [["", "?return=%2Fui-v2%23device%2Fconfiguration"], "#device/configuration"],
  [["#projects/p1", ""], "#projects/p1"],
  [["", ""], "#measure"],
  [["", undefined], "#measure"],
  [["", "?return=https%3A%2F%2Fevil%23x"], "#measure"],
  [["", "?return=%2Fui-v2%23bad%20hash"], "#measure"],
  [["", "?return=%2F%23archive"], "#archive"],
  [["#code=ABC", ""], "#measure"],
  [["#status", ""], "#status"],
  [["#projects/p1", "?return=%2F%23archive"], "#projects/p1"],
  [["", "?signed_in=1"], "#measure"]];
for (const [args, expected] of cases) eq(JSON.stringify(args), signInHash(...args), expected);
eq("fallback", signInTarget("#Device"), "#measure");
""")
        self.assertEqual(out["failures"], [])


class TestCanonicalLinks(unittest.TestCase):

    def aliases(self):
        out = run_app_node(("const LEGACY_ROUTES=",), "out.aliases = Object.keys(LEGACY_ROUTES);")
        self.assertTrue(out["aliases"], "LEGACY_ROUTES is empty")
        return out["aliases"]

    def test_app_links_use_canonical_routes(self):
        aliases = self.aliases()
        # 1. Generated links: no alias anywhere except in the alias table itself.
        generated = "\n".join(line for line in APP_LINES if not line.startswith("const LEGACY_ROUTES="))
        for alias in aliases:
            for form in ('"/#' + alias + '"', 'hash="' + alias + '"'):
                self.assertNotIn(form, generated)
        self.assertNotIn('livePollIntervalMs("status")', APP_JS)
        self.assertNotIn('location.hash="status"', APP_JS)
        self.assertIn('href="/#measure"', ui.APP_V2)
        self.assertNotIn("/#status", ui.APP_V2)
        # The device overview menu builds its hrefs as "/#device/"+item[0] from its links table.
        device = next(line for line in APP_LINES if line.startswith("async function mountDevice("))
        self.assertIn('h("a",{href:"/#device/"+item[0]}', device)
        table = re.search(r"const links=\[(.*?)\],menu=", device)
        self.assertIsNotNone(table)
        menu = ["device/" + target for target in re.findall(r"\['([^']+)',", table.group(1))]
        self.assertEqual(menu, ["device/connections", "device/display", "device/diagnostics", "device/maintenance"])
        # 2. Publicly resolved routes are canonical and never an alias.
        out = run_app_node(AREA_PREFIXES + ("function navigation(", "const HELP_ROUTES=", "const CONFIG_ROUTE="), r"""
const canonical = (name, value) => {
  eq(name + " canonical", resolveRoute("#" + value).path, value);
  eq(name + " no alias", Object.prototype.hasOwnProperty.call(LEGACY_ROUTES, value), false);
};
canonical("CONFIG_ROUTE", CONFIG_ROUTE);
canonical("FIELD_SETUP_ROUTE", FIELD_SETUP_ROUTE);
eq("routes", [CONFIG_ROUTE, FIELD_SETUP_ROUTE], ["device/connections/internet", "measure/setup"]);
eq("help routes", HELP_ROUTES, {corrections: "device/connections/internet", wifi: "device/connections/internet",
  diagnostics: "device/diagnostics"});
for (const [key, value] of Object.entries(HELP_ROUTES)) canonical("HELP_ROUTES." + key, value);
for (const route of ROUTES) if (!route.path.includes(":id")) canonical("ROUTES " + route.view, route.path);
for (const area of Object.keys(AREAS))
  for (const link of contextLinks(area)) canonical("context " + link.href, link.href.replace(/^\/#/, ""));
const tabs = navigation();
eq("three tabs", tabs.map(tab => tab.attrs.href), ["/#measure", "/#projects", "/#device"]);
eq("tab routes", tabs.map(tab => tab.attrs["data-route"]), ["measure", "projects", "device"]);
for (const tab of tabs) canonical("tab " + tab.attrs.href, tab.attrs.href.replace(/^\/#/, ""));
for (const value of """ + json.dumps(menu) + r""") canonical("device menu " + value, value);
""", stubs="const h = (tag, attrs, ...children) => ({tag, attrs: attrs || {}, children});")
        self.assertEqual(out["failures"], [])
        # 3. No internal comparison with an alias outside the exempt lines.
        for line in APP_LINES:
            if line.startswith(ALIAS_EXEMPT_PREFIXES):
                continue
            for alias in list(aliases) + ["status"]:
                self.assertNotIn('path==="' + alias + '"', line, line[:120])

    def test_boot_and_render_canonicalize_first(self):
        self.assertIn("canonicalizeRoute();resumeRestartWatch();renderRoute();", source("99-boot.js"))
        render = next(line for line in APP_LINES if line.startswith("async function renderRoute("))
        hold = render.index("restartHoldActive()")
        canonical = render.index("canonicalizeRoute()")
        self.assertLess(hold, canonical)
        self.assertLess(canonical, render.index("startPolling()"))
        self.assertIn("VIEW_MOUNTS[", render)
        for gone in ("mountDeviceChild(", "refineSettings(", 'route.path==="device"'):
            self.assertNotIn(gone, render)


# Field setup (Task 2): the measurement method is a device value, read from the live snapshot.
METHOD_PREFIXES = ("const MEASUREMENT_METHODS=", "function measurementSamples(", "function liveMethod(",
                   "function adoptMeasurementSamples(", "let methodWrite=", "async function saveMeasurementSamples(",
                   "function invalidateLive(")
# A store that counts set() calls and notifies its listeners like the real one.
STORE_STUB = r"""
let requestSerial = 0;
// No live stream here: invalidateLive() (#27 rule R3) only raises the serial.
const liveSource = {renew() {}};
const store = {state: {live: null}, sets: 0, listeners: new Set(),
  set(patch) { this.sets += 1; Object.assign(this.state, patch); this.listeners.forEach(fn => fn(this.state)); },
  subscribe(fn) { this.listeners.add(fn); return () => this.listeners.delete(fn); }};
"""
# Minimal h(): plain objects, so cards can be inspected without a DOM.
TREE_STUB = r"""
const h = (tag, attrs, ...children) => ({tag, attrs: attrs || {}, children: children.flat(Infinity), setAttribute(name, value) { this.attrs[name] = value; }});
const walk = (node, found = []) => { if (node && typeof node === "object") { found.push(node); (node.children || []).forEach(child => walk(child, found)); } return found; };
const textOf = node => walk(node).map(item => [item.attrs.text, ...(item.children || []).filter(child => typeof child === "string")].filter(Boolean).join(" ")).join(" ");
const iconButton = (label, handler, kind) => h("button", {text: label, kind, handler});
"""
GATE_VALUES = r"""{required_fix: "RTK_FIXED", quality_window_samples: 5, min_satellites: 10, max_hdop: 1.5,
  max_correction_age_sec: 5, max_gst_horizontal_sigma_m: 0.05, max_gst_vertical_sigma_m: 0.1, max_horizontal_spread_m: 0.05}"""


def multi_line_function(prefix):
    """A function whose declaration spans lines (restoreSurvey): from its first line to the closing brace at column 0."""
    start = APP_JS.index("\n" + prefix) + 1
    return APP_JS[start:APP_JS.index("\n}\n", start) + 2]


def app_line(prefix):
    lines = [line for line in APP_LINES if line.startswith(prefix)]
    if len(lines) != 1:
        raise AssertionError("declaration missing or not unique: " + prefix)
    return lines[0]


class TestFieldSetup(unittest.TestCase):

    def test_measurement_samples_come_from_the_device(self):
        out = run_app_node(("const MEASUREMENT_METHODS=", "function measurementSamples("), r"""
const cases = [[{measurement_defaults: {samples: 10}}, 10], [{measurement_defaults: {samples: "quality"}}, "quality"],
  [{measurement_defaults: {samples: 1}}, 1], [{measurement_defaults: {samples: "10"}}, 5],
  [{measurement_defaults: {samples: 7}}, 5], [{}, 5], [null, 5], [{measurement_defaults: null}, 5]];
for (const [live, expected] of cases) eq(JSON.stringify(live), measurementSamples(live), expected);
eq("methods", MEASUREMENT_METHODS, [1, 5, 10, "quality"]);
""")
        self.assertEqual(out["failures"], [])

    def test_adopt_measurement_samples_publishes_once(self):
        out = run_app_node(METHOD_PREFIXES, r"""
requestSerial = 3;
const seen = [];
store.subscribe(state => seen.push(JSON.parse(JSON.stringify(state.live.measurement_defaults))));
const old = {current_fix: {qual: 4}, measurement_defaults: {samples: 5, reference_frame: ""}};
store.state.live = old;
adoptMeasurementSamples(10);
eq("one set", store.sets, 1);
eq("listener sees the new method and the same frame", seen, [{samples: 10, reference_frame: ""}]);
eq("old live object unchanged", old.measurement_defaults, {samples: 5, reference_frame: ""});
eq("other live fields kept", store.state.live.current_fix, {qual: 4});
eq("serial", requestSerial, 4);
store.state.live = {current_fix: {}};
adoptMeasurementSamples(10);
eq("no defaults: no set", store.sets, 1);
eq("no defaults: serial still counts", requestSerial, 5);
store.state.live = null;
let threw = null;
try { adoptMeasurementSamples(10); } catch (error) { threw = error.message; }
eq("no live: no throw, no set", [threw, store.sets], [null, 1]);
""", stubs=STORE_STUB + "const main = {querySelectorAll: () => []};")
        self.assertEqual(out["failures"], [])

    def test_optional_json_keeps_aborts(self):
        out = run_app_node(("async function optionalJson(",), r"""
const outcome = promise => promise.then(value => ["ok", value], error => ["rejected", error.name || error.message]);
answer = () => Promise.resolve({values: {antenna_height_m: 2}});
const live = new AbortController();
eq("data", await outcome(optionalJson("/api/field-setup", live.signal)), ["ok", {values: {antenna_height_m: 2}}]);
eq("signal passed on", calls[0], ["/api/field-setup", true]);
answer = () => Promise.reject(Object.assign(new Error("Internal error"), {status: 500}));
eq("server error", await outcome(optionalJson("/api/field-setup", live.signal)), ["ok", null]);
answer = () => Promise.reject(Object.assign(new Error("Request cancelled"), {name: "AbortError"}));
eq("abort error", await outcome(optionalJson("/api/field-setup", live.signal)), ["rejected", "AbortError"]);
const gone = new AbortController();
gone.abort();
answer = () => Promise.reject(new Error("boom"));
eq("aborted signal", await outcome(optionalJson("/api/field-setup", gone.signal)), ["rejected", "Error"]);
""", stubs=r"""
let answer = null;
const calls = [];
const api = {json: (path, options) => { calls.push([path, Boolean(options && options.signal)]); return answer(); }};
""", asynchronous=True)
        self.assertEqual(out["failures"], [])

    def test_section_refresh_ends_after_navigation(self):
        out = run_app_node(("function sectionTarget(", "async function refreshSection("), r"""
eq("no match", sectionTarget('[data-card="gate"]'), null);
eq("asked for", main.asked, '[data-card="gate"]');
const section = {isConnected: true};
main.found = section;
const then = activeController, target = sectionTarget('[data-card="gate"]');
eq("target", target && [target.section === section, target.signal === then.signal], [true, true]);
const fills = [];
const fill = (node, signal) => { fills.push([node === section, signal === activeController.signal]); };
// Navigation: a new controller, the old one aborted.
activeController = new AbortController();
then.abort();
await refreshSection(target, fill);
eq("aborted view", fills.length, 0);
const fresh = sectionTarget('[data-card="gate"]');
section.isConnected = false;
await refreshSection(fresh, fill);
eq("detached section", fills.length, 0);
await refreshSection(null, fill);
eq("no target", fills.length, 0);
section.isConnected = true;
await refreshSection(fresh, fill);
eq("valid target", fills, [[true, true]]);
activeController = null;
eq("no controller", sectionTarget('[data-card="gate"]'), null);
eq("never renderRoute", renders, 0);
""", stubs=r"""
let activeController = new AbortController(), renders = 0;
const main = {found: null, asked: null, querySelector(selector) { this.asked = selector; return this.found; }};
function renderRoute() { renders += 1; }
""", asynchronous=True)
        self.assertEqual(out["failures"], [])

    def test_method_save_publishes_after_unlocking(self):
        out = run_app_node(METHOD_PREFIXES, r"""
store.state.live = {measurement_defaults: {samples: 5, reference_frame: ""}};
const log = [];
store.subscribe(state => log.push({write: methodWrite, disabled: buttons.map(button => button.disabled), value: liveMethod(state.live)}));
let release = null;
hold = new Promise(resolve => { release = resolve; });
const first = saveMeasurementSamples(10);
eq("buttons locked during the PUT", buttons.map(button => button.disabled), [true, true, true, true]);
const second = saveMeasurementSamples(10);
eq("one PUT", puts, [["/api/measurement-gate", {samples: 10}]]);
eq("no early notification", log.length, 0);
release({samples: 10});
const [a, b] = await Promise.all([first, second]);
eq("both calls end with the saved answer", [a, b === a], [{samples: 10}, true]);
eq("one notification after unlocking", log, [{write: null, disabled: [false, false, false, false], value: 10}]);
eq("serial", requestSerial, 1);
eq("success", successes, ["Measurement method saved for new measurements"]);
eq("pressed state is left to the subscription", buttons.map(button => button.pressed), [undefined, undefined, undefined, undefined]);
hold = null;
fail = true;
const failed = await saveMeasurementSamples(1);
eq("failure", [failed, log.length, buttons.map(button => button.disabled), errors, requestSerial],
  [null, 1, [false, false, false, false], ["refused"], 1]);
eq("still one PUT per save", puts.length, 2);
eq("live unchanged", liveMethod(store.state.live), 10);
""", stubs=STORE_STUB + r"""
const tr = (key, fallback) => fallback;
const buttons = [{}, {}, {}, {}].map(button => Object.assign(button, {disabled: false}));
const main = {querySelectorAll: selector => selector === ".measurement-choice button" ? buttons : []};
const puts = [], successes = [], errors = [];
let hold = null, fail = false;
const api = {put: (path, payload) => { puts.push([path, payload]); if (fail) return Promise.reject(new Error("refused"));
  return hold || Promise.resolve({samples: payload.samples}); }};
const showSuccess = message => successes.push(message), showError = error => errors.push(error.message);
""", asynchronous=True)
        self.assertEqual(out["failures"], [])

    def test_gate_read_does_not_touch_the_method(self):
        out = run_app_node(METHOD_PREFIXES + ("function gateCard(", "function methodQualityHint(",
                                              "async function fillGateCard("), r"""
store.state.live = {measurement_defaults: {samples: 10}};
const section = {isConnected: true, replaced: null, replaceWith(node) { this.replaced = node; }};
await fillGateCard(section, new AbortController().signal);
eq("gate GET", gets, [["/api/measurement-gate", true]]);
eq("card replaced", section.replaced && section.replaced.attrs["data-card"], "gate");
// The method hint takes the refreshed sample window in place; nothing else on the page is queried.
eq("hint follows the window", /last 12 accepted samples/.test(hint.textContent), true);
eq("only the hint is looked up", queried, ['[data-card="method"] [data-hint="method-quality"]']);
hint.textContent = "untouched";
eq("no store write", store.sets, 0);
eq("serial unchanged", requestSerial, 0);
eq("method stays", liveMethod(store.state.live), 10);
const late = {isConnected: true, replaced: null, replaceWith(node) { this.replaced = node; }}, controller = new AbortController();
const pending = fillGateCard(late, controller.signal);
controller.abort();
await pending;
eq("aborted: no replace", late.replaced, null);
eq("aborted: hint untouched", hint.textContent, "untouched");
const detached = {isConnected: true, replaced: null, replaceWith(node) { this.replaced = node; }};
const pending2 = fillGateCard(detached, new AbortController().signal);
detached.isConnected = false;
await pending2;
eq("detached: no replace", detached.replaced, null);
eq("detached: hint untouched", hint.textContent, "untouched");
""", stubs=STORE_STUB + TREE_STUB + r"""
const tr = (key, fallback, values) => fallback.replace(/\{(\w+)\}/g, (all, name) => values && name in values ? String(values[name]) : all);
const localText = value => value;
const hint = {textContent: "mount"}, queried = [];
const main = {querySelectorAll: () => [], querySelector: selector => { queried.push(selector); return hint; }};
const gets = [];
const api = {json: async (path, options) => { gets.push([path, Boolean(options && options.signal)]);
  return {samples: 5, values: Object.assign(""" + GATE_VALUES + r""", {quality_window_samples: 12}), deviations: []}; }};
const dataRow = (label, value) => h("div", {class: "data-row"}, h("span", {text: label}), h("b", {textRaw: value}));
const frameMixHint = () => "";
const gateBadge = () => h("div", {class: "gate-softened"}), renderGateBadge = (node, items) => { node.items = items; };
let settingsGateDeviations = [];
const editMeasurementGate = () => {}, resetMeasurementGate = () => {};
""", asynchronous=True)
        self.assertEqual(out["failures"], [])
        self.assertNotIn("gate.samples", APP_JS)
        for prefix in ("async function fillGateCard(", "async function mountFieldSetup("):
            self.assertNotIn("adoptMeasurementSamples", app_line(prefix), prefix)
        self.assertEqual(APP_JS.count("adoptMeasurementSamples("), 2)
        self.assertIn("adoptMeasurementSamples(", app_line("async function saveMeasurementSamples("))

    def test_no_browser_local_measurement_method(self):
        self.assertNotIn("sampleCount", APP_JS)
        self.assertEqual(APP_JS.count("rtk-samples"), 1)
        self.assertIn('localStorage.removeItem("rtk-samples")', APP_JS)
        # measuredAction spans several lines of src/40-tracking-actions.js: from its declaration to the closing brace.
        lines = source("40-tracking-actions.js").split("\n")
        start = next(index for index, line in enumerate(lines) if line.startswith("async function measuredAction("))
        end = next(index for index in range(start, len(lines)) if lines[index] == "}")
        action = "\n".join(lines[start:end + 1])
        self.assertEqual(action.count("measurementSamples(store.state.live)"), 1)
        self.assertRegex(action, r"\{samples(?::samples)?\}")
        self.assertNotIn("sampleCount", action)
        for prefix in ("async function editMeasurementGate(", "async function resetMeasurementGate("):
            line = app_line(prefix)
            self.assertNotIn("renderRoute(", line, prefix)
            self.assertTrue(line.startswith(prefix + "){const target=sectionTarget('[data-card=\"gate\"]');"), prefix)
            self.assertIn("refreshSection(target,fillGateCard)", line, prefix)
        self.assertNotIn("renderRoute(", app_line("async function refreshSection("))
        self.assertNotIn("methodWrite", app_line("async function mountFieldSetup("))

    def test_field_setup_page_marks_new_measurements(self):
        out = run_app_node(("const tr=", "const V2_DE=", "function localText(", "const sampleLabel=", "const CONFIG_ROUTE=")
                           + METHOD_PREFIXES + ("function methodCard(", "function methodQualityHint(", "function gateCard(",
                                                "function fieldSetupCard("), r"""
const pressed = card => walk(card).filter(node => node.tag === "button" && node.attrs["aria-pressed"] !== undefined)
  .map(node => [node.attrs.text, String(node.attrs["aria-pressed"])]);
const badge = card => walk(card).filter(node => /\bscope-badge\b/.test(node.attrs.class || "")).map(node => node.attrs.text);
const hint = (card, name) => walk(card).filter(node => node.attrs["data-hint"] === name);
for (const lang of ["en", "de"]) {
  RtkI18n.setLanguage(lang);
  const words = dict[lang], card = methodCard("quality", 7), quality = sampleLabel("quality");
  eq(lang + " method card", card.attrs["data-card"], "method");
  eq(lang + " four choices", pressed(card).map(item => item[0]), [1, 5, 10, "quality"].map(sampleLabel));
  eq(lang + " only quality pressed", pressed(card).filter(item => item[1] === "true").map(item => item[0]), [quality]);
  eq(lang + " badge", badge(card), [words["ui.new_measurements_only"]]);
  const help = textOf(card);
  eq(lang + " help text exists", typeof words["ui.method_quality_help"], "string");
  eq(lang + " window count", help.includes(String(words["ui.method_quality_help"]).replace("{count}", "7")), true);
  eq(lang + " no consecutive", /consecutive|aufeinanderfolgend/i.test(help), false);
  eq(lang + " nothing pressed without a device value", pressed(methodCard(null, 5)).filter(item => item[1] === "true"), []);
  const dgps = gateCard({values: Object.assign({}, """ + GATE_VALUES + r""", {required_fix: "DGPS"}),
    deviations: [{key: "required_fix", value: "DGPS", default: "RTK_FIXED"}]});
  eq(lang + " gate card", dgps.attrs["data-card"], "gate");
  eq(lang + " frame mix hint", hint(dgps, "frame-mix").length, 1);
  eq(lang + " reset text", walk(dgps).some(node => node.tag === "button" && node.attrs.text === words["ui.rules_reset"]), true);
  eq(lang + " deviations shown", walk(dgps).filter(node => node.items).map(node => node.items.length), [1]);
  eq(lang + " deviations kept", settingsGateDeviations.length, 1);
  eq(lang + " no frame mix for RTK FIXED", hint(gateCard({values: """ + GATE_VALUES + r""", deviations: []}), "frame-mix").length, 0);
  const setup = fieldSetupCard({});
  eq(lang + " frame epoch help", hint(setup, "frame-epoch").map(node => node.attrs.text), [words["ui.frame_epoch_help"]]);
  eq(lang + " setup badge", badge(setup), [words["ui.new_measurements_only"]]);
  eq(lang + " points keep", textOf(setup).includes(words["ui.field_setup_points_keep"]), true);
}
RtkI18n.setLanguage("en");
""", stubs=STORE_STUB + TREE_STUB + r"""
const main = {querySelectorAll: () => []};
const dataRow = (label, value) => h("div", {class: "data-row"}, h("span", {text: label}), h("b", {textRaw: value}));
const frameMixHint = fix => fix === "DGPS" ? "mix" : "";
const gateBadge = () => h("div", {class: "gate-softened"}), renderGateBadge = (node, items) => { node.items = items; };
let settingsGateDeviations = [];
const editMeasurementGate = () => {}, resetMeasurementGate = () => {}, saveFieldSetup = () => {};
""")
        self.assertEqual(out["failures"], [])


# Measure summary (#24-UI Task 3) and the #38a reminder: pure helpers under Node, sessionStorage stubbed.
REMINDER_PREFIXES = ("const tr=", "let staleSinceSeq=", "function fixIsUsable(") + ROUTE_PREFIXES + (
    "let frameReminderHidden=", "function frameReminderDismissed(", "function dismissFrameReminder(",
    "function frameReminderInfo(")
SUMMARY_PREFIXES = REMINDER_PREFIXES + ("const V2_DE=", "function localText(", "const fmt=", "const sampleLabel=",
                                        "function capacityText(", "const MEASUREMENT_METHODS=",
                                        "function measurementSamples(", "function fieldSummaryRows(")
SESSION_STUB = r"""
const sessionData = new Map();
let sessionThrows = false;
globalThis.sessionStorage = {
  getItem(key) { if (sessionThrows) throw new Error("blocked"); return sessionData.has(key) ? sessionData.get(key) : null; },
  setItem(key, value) { if (sessionThrows) throw new Error("blocked"); sessionData.set(key, String(value)); }};
"""


class TestMeasureSummary(unittest.TestCase):

    def test_frame_reminder_follows_fix_and_frame(self):
        out = run_app_node(REMINDER_PREFIXES, r"""
const base = () => ({measurement_defaults: {reference_frame: "", samples: 5, antenna_height_m: 0},
  current_fix: {qual: 4, fix_seq: 9, stale: false}});
const variant = patch => { const live = base(); patch(live); return live; };
RtkI18n.setLanguage("en");
const info = frameReminderInfo(base(), false);
eq("rtk fixed: shown", info !== null, true);
eq("route", info && info.route, "measure/setup/frame");
eq("en text", info && info.text, dict.en["ui.frame_reminder"]);
eq("en text exists", typeof dict.en["ui.frame_reminder"], "string");
eq("rtk float", frameReminderInfo(variant(live => { live.current_fix.qual = 5; }), false) !== null, true);
for (const qual of [2, 1, 0]) eq("qual " + qual, frameReminderInfo(variant(live => { live.current_fix.qual = qual; }), false), null);
eq("stale fix", frameReminderInfo(variant(live => { live.current_fix.stale = true; }), false), null);
staleSinceSeq = 9;
eq("fix older than the stale mark", frameReminderInfo(base(), false), null);
staleSinceSeq = null;
eq("no fix", frameReminderInfo(variant(live => { delete live.current_fix; }), false), null);
eq("blank frame", frameReminderInfo(variant(live => { live.measurement_defaults.reference_frame = "  "; }), false) !== null, true);
eq("frame set", frameReminderInfo(variant(live => { live.measurement_defaults.reference_frame = "ETRS89"; }), false), null);
eq("frame null", frameReminderInfo(variant(live => { live.measurement_defaults.reference_frame = null; }), false) !== null, true);
eq("key missing", frameReminderInfo(variant(live => { delete live.measurement_defaults.reference_frame; }), false), null);
eq("defaults missing", frameReminderInfo(variant(live => { delete live.measurement_defaults; }), false), null);
eq("defaults null", frameReminderInfo(variant(live => { live.measurement_defaults = null; }), false), null);
eq("no live", frameReminderInfo(null, false), null);
eq("dismissed", frameReminderInfo(base(), true), null);
RtkI18n.setLanguage("de");
eq("de text", (frameReminderInfo(base(), false) || {}).text, dict.de["ui.frame_reminder"]);
RtkI18n.setLanguage("en");
// Dismissal: per tab session, with a module fallback when storage is unavailable.
eq("not dismissed at start", frameReminderDismissed(), false);
dismissFrameReminder();
eq("dismissed after the call", frameReminderDismissed(), true);
eq("stored for the session", sessionData.get("rtk-frame-reminder"), "dismissed");
frameReminderHidden = false;
eq("a reload of the tab keeps it hidden", frameReminderDismissed(), true);
sessionData.clear();
eq("a new session shows it", frameReminderDismissed(), false);
sessionThrows = true;
let threw = null;
try { eq("blocked storage reads as shown", frameReminderDismissed(), false); dismissFrameReminder();
  eq("blocked storage: hidden until reload", frameReminderDismissed(), true); } catch (error) { threw = error.message; }
eq("storage errors never throw", threw, null);
""", stubs=SESSION_STUB)
        self.assertEqual(out["failures"], [])

    def test_field_summary_rows(self):
        out = run_app_node(SUMMARY_PREFIXES, r"""
const fixed = {qual: 4, fix_seq: 9, stale: false};
const live = (defaults, fix = fixed) => ({measurement_defaults: defaults, current_fix: fix});
const full = {samples: "quality", antenna_height_m: 1.85, reference_frame: "ETRS89 <x>", reference_frame_epoch: "2010.0"};
for (const lang of ["en", "de"]) {
  RtkI18n.setLanguage(lang);
  const rows = fieldSummaryRows(live(full));
  eq(lang + " method", rows.method, localText("Until quality is OK"));
  eq(lang + " antenna", rows.antenna, lang === "de" ? "1,85 m" : "1.85 m");
  eq(lang + " frame and epoch", rows.frame, lang === "de" ? "ETRS89 <x>, Epoche 2010.0" : "ETRS89 <x>, epoch 2010.0");
  eq(lang + " frame without epoch", fieldSummaryRows(live(Object.assign({}, full, {reference_frame_epoch: ""}))).frame, "ETRS89 <x>");
  eq(lang + " empty frame", fieldSummaryRows(live(Object.assign({}, full, {reference_frame: " "}))).frame, dict[lang]["ui.frame_not_set"]);
  eq(lang + " frame null", fieldSummaryRows(live(Object.assign({}, full, {reference_frame: null}))).frame, dict[lang]["ui.frame_not_set"]);
  eq(lang + " ten samples", fieldSummaryRows(live(Object.assign({}, full, {samples: 10}))).method, sampleLabel(10));
  const unknown = fieldSummaryRows({current_fix: fixed});
  eq(lang + " without defaults", [unknown.method, unknown.antenna, unknown.frame], ["—", "—", "—"]);
}
eq("en not set text", dict.en["ui.frame_not_set"], "not set");
RtkI18n.setLanguage("en");
const signature = defaults => fieldSummaryRows(live(defaults)).signature;
const first = signature(full);
eq("signature is stable", signature(Object.assign({}, full)), first);
for (const [name, patch] of [["method", {samples: 5}], ["antenna", {antenna_height_m: 2}], ["frame", {reference_frame: "ITRF2014"}],
  ["epoch", {reference_frame_epoch: "2020.0"}]]) eq("signature changes with " + name, signature(Object.assign({}, full, patch)) !== first, true);
const empty = Object.assign({}, full, {reference_frame: ""});
const shown = signature(empty);
dismissFrameReminder();
eq("signature changes when the reminder is hidden", signature(empty) !== shown, true);
""", stubs=SESSION_STUB)
        self.assertEqual(out["failures"], [])

    def test_measure_screen_order(self):
        mount = app_line("function mountStatus(){")
        primary = re.search(r'h\("div",\{class:"status-primary"\},([\w,]+)\)', mount)
        self.assertIsNotNone(primary, "status-primary column")
        classes = []
        for name in primary.group(1).split(","):
            found = re.search(r"\b" + name + r'=h\("(?:section|div)",\{class:"([^"]*)"', mount)
            self.assertIsNotNone(found, name)
            classes.append(next(item for item in found.group(1).split()
                                if item in ("status-project", "readiness-hero", "status-setup", "status-last",
                                            "field-actions")))
        self.assertEqual(classes, ["status-project", "readiness-hero", "status-setup", "status-last", "field-actions"])
        self.assertNotIn("contextRow('Measurement')", mount)
        self.assertIn('class:"tool-card status-last"', mount)
        # The summary renders as the last step of the single updateStatus (#29b E7), exactly once.
        self.assertEqual(APP_JS.count("renderFieldSummary(live);"), 1)
        self.assertTrue(app_line("function updateStatus(").endswith("renderFieldSummary(live);}"))
        self.assertIsNone(re.search(r"^updateStatus=", APP_JS, re.M), "no updateStatus layer")


# Device overview and connections (#24-UI Task 4): four areas, one connections page, secrets without the QR module.
DEVICE_STATUS = r"""{system: {display_name: "RTK-TEST", version: "VTEST", uptime_sec: 120, reset_cause: "HARD", ip: "192.0.2.1"},
  board: {fs_total_bytes: 1000000, fs_free_bytes: 100000, wifi_rssi: -55},
  stats: {wifi_ssid_aktiv: "Field <net>", ntrip_state: "streaming", ble_auth_failures: 1, nmea_tcp_auth_failures: 2},
  fix: {qual: 4, fix_status_text: "RTK_FIXED", sats: 17},
  rtcm: {msm: ["MSM5"], mount: "MOUNT", epoch_age_sec: 2, stale: false},
  ble: {clients: 1}, nmea_tcp: {clients: 2}, log_entries: 4}"""
# Every text of a node tree, translated (text) and raw (textRaw) alike.
TEXT_STUB = r"""
const textAll = node => walk(node).map(item => [(item.attrs || {}).text, (item.attrs || {}).textRaw].filter(value => value != null).join(" ")).join(" ");
const notices = node => walk(node).filter(item => (item.attrs || {})["data-notice"]).map(item => item.attrs["data-notice"]);
const replace = (target, ...children) => { target.children = children.flat(Infinity); };
"""


class TestDeviceAreas(unittest.TestCase):

    def test_device_overview_has_four_areas(self):
        out = run_app_node(AREA_PREFIXES + ("const V2_DE=", "function localText(", "function authFailureTotal(",
                                            "function capacityText(", "function themeLabel(", "async function mountDevice("), r"""
const status = """ + DEVICE_STATUS + r""";
const run = async (lang, aborted) => {
  RtkI18n.setLanguage(lang);
  main.children = []; store.state = {}; api.calls = [];
  const controller = new AbortController();
  api.answer = () => { if (aborted) controller.abort(); return status; };
  await mountDevice(controller.signal);
  return main;
};
await run("en", false);
eq("one status request", api.calls, ["/status"]);
const nav = walk(main).filter(node => node.tag === "nav" && node.attrs.class === "device-links");
eq("one menu", nav.length, 1);
const links = walk(nav[0]).filter(node => node.tag === "a");
eq("four areas", links.map(link => link.attrs.href),
  ["/#device/connections", "/#device/display", "/#device/diagnostics", "/#device/maintenance"]);
const linkText = () => walk(main).filter(node => node.tag === "a").map(link => {
  const parts = walk(link);
  return [parts.find(node => node.tag === "b"), parts.find(node => node.tag === "small")].map(node => node && (node.attrs.text || node.attrs.textRaw));
});
eq("titles and short lines", linkText(), [["Connections", "Field <net> · streaming · 1 BLE · 2 TCP"],
  ["Operation & display", "English · Automatic"], ["Diagnostics", "MSM5 · 4 entries"],
  ["Maintenance", "Configuration backup · restart"]]);
const metrics = walk(main).filter(node => node.tag === "article" && /device-metric/.test(node.attrs.class || ""))
  .map(card => card.children.map(child => child && child.attrs && (child.attrs.text || child.attrs.textRaw)));
eq("metrics", metrics, [["Network", "Field <net>", "-55 dBm · 192.0.2.1"], ["GNSS", "RTK_FIXED", "17 satellites"],
  ["Corrections", "MSM5", "MOUNT · 2.0 s"], ["Storage", "0.9 / 1.0 MB", "Storage is running low"]]);
eq("no restart button", walk(main).filter(node => node.tag === "button" && /Restart/.test(node.attrs.text || "")).length, 0);
eq("identity", textAll(main).includes("RTK-TEST"), true);
eq("status kept", store.state.status === status, true);
await run("de", false);
const de = dict.de;
eq("german titles", linkText().map(item => item[0]),
  [de["ui.route_connections"], de["ui.route_display"], de["ui.route_diagnostics"], de["ui.route_maintenance"]]);
eq("german short lines", linkText().slice(1).map(item => item[1]),
  ["Deutsch · " + de["ui.theme_auto"],
   "MSM5 · 4 " + RtkI18n.v2de["entries"], de["ui.device_link_maintenance"]]);
await run("en", true);
eq("aborted: skeleton only", main.children.map(node => node.tag), ["skeleton"]);
eq("aborted: no status kept", store.state.status, undefined);
RtkI18n.setLanguage("en");
""", stubs=TREE_STUB + TEXT_STUB + r"""
const main = {children: []};
const store = {state: {}, set(patch) { Object.assign(this.state, patch); }};
const api = {calls: [], answer: null, json: async (path, options) => { api.calls.push(path); if (!options || !options.signal) throw new Error("no signal"); return api.answer(); }};
const skeleton = () => h("skeleton", {});
const fmt = (value, digits = 2) => Number(value).toFixed(digits);
const quantity = (count, one, many) => count + " " + (count === 1 ? one : many);
const duration = seconds => seconds + " s";
const setConnection = () => {};
const $ = () => ({textContent: ""});
const STORAGE_RESERVE_RATIO = .2;
function rebootDevice() {}
""", asynchronous=True)
        self.assertEqual(out["failures"], [])
        for gone in ("refineDeviceSummary", "legacyDeviceMount", "mountDeviceChild"):
            self.assertNotIn(gone, APP_JS)

    def test_connections_load_access_once(self):
        areas, device = source("77-device-areas.js"), source("75-device.js")
        # One fetch of /api/access per mount; the technical details still list the endpoint as a link.
        self.assertEqual(areas.count('api.json("/api/access"'), 1)
        self.assertNotIn('"/api/access"', device)
        self.assertEqual(areas.count("mountConfiguration(signal)"), 1)
        self.assertIn('"data-group":"internet"', areas)
        self.assertIn('"data-group":"access"', areas)
        # No second form variant: the configuration form comes from mountConfiguration only.
        self.assertNotIn("configuration-layout", areas)
        for prefix in ("async function setTcpAuthentication(", "async function rotateStreamKey("):
            line = app_line(prefix)
            self.assertNotIn("renderRoute(", line, prefix)
            self.assertTrue(line[line.index("{") + 1:].startswith("const target=sectionTarget('[data-group=\"access\"]');"),
                            prefix)
        self.assertEqual(APP_JS.count('"/api/label"'), 1)
        self.assertIn('"/api/label"', app_line("async function fillLabelBlocks("))
        self.assertNotIn("ensureQr(", app_line("async function fillLabelBlocks("))
        connections = app_line("async function mountConnections(")
        self.assertLess(connections.index("await mountConfiguration(signal);if(signal.aborted)return;"),
                        connections.index("fillAccessGroup(access,signal)"))
        self.assertNotIn("Promise.all", connections)

    def test_access_secrets_do_not_need_the_qr_module(self):
        out = run_app_node(("const tr=", "const V2_DE=", "function localText(", "const dataRow=", "function secretPanel(",
                            "function blockNotice(", "async function fillLabelBlocks(", "async function fillQrCodes("), r"""
const LABEL = {device_code: "CODE-1234", ble_pin: "654321", stream_token: "tok-abc", wifi_qr: "WIFI:T:WPA;S:RTK;P:x;;"};
const slot = name => ({tag: "div", attrs: {class: name}, children: [], isConnected: true});
const retry = node => walk(node).find(item => item.tag === "button") || {attrs: {handler: () => failures.push("no retry button")}};
// 1. The secrets stand before the QR module is asked; a QR failure leaves them and retries only the graphic.
let secret = slot("secret-slot"), qr = slot("qr-slot");
currentSecret = secret;
await fillLabelBlocks(secret, qr, new AbortController().signal);
currentSecret = null;
eq("label fetched once", labelCalls, ["/api/label"]);
eq("secret panel", secret.children.map(node => node.attrs.class), ["tool-card secret-panel"]);
eq("device code and pin", [textAll(secret).includes("CODE-1234"), textAll(secret).includes("654321")], [true, true]);
eq("stream key masked", textAll(secret).includes("tok-abc"), false);
eq("secrets before the QR module", secretsAtQr, [true]);
eq("qr notice", notices(qr), ["qr-unavailable"]);
eq("qr notice text", textAll(qr).includes(dict.en["ui.qr_unavailable"]), true);
qrFails = false;
await retry(qr).attrs.handler();
eq("qr retried", qrCalls, 2);
eq("label still once", labelCalls.length, 1);
eq("qr codes drawn with the label", [qr.children.map(node => node.tag), qrLabels], [["qr-codes"], [LABEL]]);
eq("secrets kept", textAll(secret).includes("CODE-1234"), true);
// 2. No label: a local notice with a retry, no QR attempt, the QR slot stays empty.
labelCalls = []; qrCalls = 0; qrFails = true; labelAnswer = null;
secret = slot("secret-slot"); qr = slot("qr-slot");
await fillLabelBlocks(secret, qr, new AbortController().signal);
eq("label unavailable", notices(secret), ["label-unavailable"]);
eq("label unavailable text", textAll(secret).includes(dict.en["ui.label_unavailable"]), true);
eq("no secret panel", walk(secret).some(node => /secret-panel/.test(node.attrs.class || "")), false);
eq("no qr attempt", qrCalls, 0);
eq("qr slot empty", qr.children, []);
labelAnswer = LABEL;
await retry(secret).attrs.handler();
eq("retry loads the label", labelCalls, ["/api/label", "/api/label"]);
eq("retry shows the secrets", textAll(secret).includes("CODE-1234"), true);
eq("then the QR module", qrCalls, 1);
// 3. Navigation after the label answer: both slots stay as they were.
labelCalls = []; qrCalls = 0;
secret = slot("secret-slot"); qr = slot("qr-slot");
const view = new AbortController();
abortOnLabel = view;
await fillLabelBlocks(secret, qr, view.signal);
abortOnLabel = null;
eq("aborted: slots unchanged", [secret.children, qr.children], [[], []]);
eq("aborted: no qr attempt", qrCalls, 0);
// 4. A detached slot (page rebuilt) is not filled.
secret = slot("secret-slot"); qr = slot("qr-slot"); secret.isConnected = false;
await fillLabelBlocks(secret, qr, new AbortController().signal);
eq("detached: slots unchanged", [secret.children, qr.children], [[], []]);
// 5. Navigation while the QR module loads: no notice, no graphic.
secret = slot("secret-slot"); qr = slot("qr-slot");
const late = new AbortController();
abortOnQr = late;
await fillLabelBlocks(secret, qr, late.signal);
eq("aborted during qr: qr slot unchanged", qr.children, []);
""", stubs=TREE_STUB + TEXT_STUB + r"""
let labelCalls = [], labelAnswer = null, abortOnLabel = null, abortOnQr = null, qrCalls = 0, qrFails = true, qrLabels = [], secretsAtQr = [];
let currentSecret = null;
async function optionalJson(path, signal) { labelCalls.push(path); if (!signal) throw new Error("no signal"); if (abortOnLabel) abortOnLabel.abort(); return labelAnswer; }
async function ensureQr() { qrCalls += 1; if (currentSecret) secretsAtQr.push(textAll(currentSecret).includes("CODE-1234")); if (abortOnQr) { abortOnQr.abort(); abortOnQr = null; throw new Error("QR code module could not be loaded."); } if (qrFails) throw new Error("QR code module could not be loaded."); }
function qrCodes(label) { qrLabels.push(label); return h("qr-codes", {}); }
function rotateStreamKey() {}
labelAnswer = {device_code: "CODE-1234", ble_pin: "654321", stream_token: "tok-abc", wifi_qr: "WIFI:T:WPA;S:RTK;P:x;;"};
""", asynchronous=True)
        self.assertEqual(out["failures"], [])

    def test_qr_loader_can_retry(self):
        out = run_app_node(("let qrPromise=null;",), r"""
const first = await ensureQr().then(() => "resolved", error => "rejected: " + error.message);
eq("first load fails", first, "rejected: QR code module could not be loaded.");
eq("failed script removed", scripts[0].removed, true);
const second = await ensureQr().then(() => "resolved", () => "rejected");
eq("retry loads again", [second, scripts.length], ["resolved", 2]);
eq("versioned source", scripts[1] && scripts[1].attrs.src, "/qr.js?v=v1");
eq("loaded module is reused", [ensureQr() === ensureQr(), scripts.length], [true, 2]);
""", stubs=r"""
const scripts = [], outcomes = ["error", "load"];
const h = (tag, attrs) => ({tag, attrs, listeners: {}, removed: false,
  addEventListener(type, fn) { this.listeners[type] = fn; }, remove() { this.removed = true; }});
globalThis.document = {body: {dataset: {assetVersion: "v1"}},
  head: {append(node) { scripts.push(node); const outcome = outcomes.shift(); setTimeout(() => node.listeners[outcome](), 0); }}};
""", asynchronous=True)
        self.assertEqual(out["failures"], [])


class TestDisplayPage(unittest.TestCase):

    def test_display_page_is_browser_only(self):
        out = run_app_node(("const tr=", "const V2_DE=", "function localText(", "const CONFIG_ROUTE=", "function themeLabel(", "function themeControl(", "async function mountDisplay("), r"""
const run = async lang => {
  RtkI18n.setLanguage(lang);
  main.children = []; api.calls = [];
  await mountDisplay({view: "display"}, new AbortController().signal);
};
await run("en");
const layout = main.children[0];
eq("layout", layout.attrs.class, "settings-layout");
const cards = layout.children.filter(node => node.attrs && node.attrs["data-card"]);
eq("cards", cards.map(node => node.attrs["data-card"]), ["language", "display", "measure-link"]);
eq("no device request", api.calls, []);
eq("scope hint", textOf(layout.children[0]), "Settings on this page apply to this browser only.");
const link = walk(cards[2]).find(node => node.tag === "a");
eq("measure link", link && link.attrs.href, "/#measure/setup");
eq("moved text", textOf(cards[2]).includes("Measurement method, measurement rules and field setup are under Measure"), true);
const all = walk(main);
eq("no session card", all.some(node => (node.attrs || {})["data-card"] === "session"), false);
eq("no sign-out button here", all.some(node => node.tag === "button" && /Sign out/.test(node.attrs.text || "")), false);
eq("no method or rules controls", all.some(node => /measurement-choice|field-setup/.test((node.attrs || {}).class || "")), false);
eq("connection state", connection, ["Settings ready", "ok"]);
const group = walk(cards[1]).find(node => (node.attrs || {}).role === "group");
eq("theme group", group && [group.attrs.class, group.attrs["aria-label"]], ["segmented theme-choice", "Appearance"]);
eq("language buttons", walk(cards[0]).filter(node => node.tag === "button").map(node => [node.attrs.text, node.attrs["aria-pressed"]]),
  [["Deutsch", false], ["English", true], ["Polski", false]]);
const choices = walk(group).filter(node => node.tag === "button");
eq("theme buttons", choices.map(node => [node.attrs["data-theme-choice"], node.attrs.text, node.attrs["aria-pressed"]]),
  [["auto", "Automatic", true], ["light", "Light", false], ["dark", "Dark", false], ["high-contrast", "High contrast", false]]);
eq("no contrast switch", walk(cards[1]).some(node => /^High contrast o/.test(node.attrs.text || "")), false);
eq("one listener, tied to the route signal", [listeners.length, listeners[0].type, listeners[0].options.signal instanceof AbortSignal], [1, "rtk-theme-change", true]);
choices[2].attrs.onclick();
eq("a click only sets the theme", [RtkI18n.getTheme(), api.calls, renderCalls], ["dark", [], 0]);
RtkI18n.setTheme("auto");
await run("de");
eq("german theme buttons", walk(main).filter(node => node.tag === "button" && (node.attrs || {})["data-theme-choice"]).map(node => node.attrs.text),
  [dict.de["ui.theme_auto"], dict.de["ui.theme_light"], dict.de["ui.theme_dark"], dict.de["ui.high_contrast"]]);
const de = dict.de;
eq("german hint", textOf(main.children[0].children[0]), de["ui.display_scope"]);
eq("german moved text", textOf(main.children[0].children[3]).includes(de["ui.moved_to_measure"]), true);
RtkI18n.setLanguage("en");
""", stubs=TREE_STUB + TEXT_STUB + r"""
const main = {children: []};
const api = {calls: [], json: async path => { api.calls.push(path); throw new Error("the display page must not fetch"); }};
let connection = null;
const setConnection = (...args) => { connection = args; };
const text = value => String(localText(value));
const dataRow = () => h("div", {});
const skeleton = () => h("skeleton", {});
async function logout() {}
let keepAwake = false;
const syncWakeLock = async () => {};
let renderCalls = 0;
const renderRoute = () => { renderCalls += 1; };
const listeners = [];
// A scope-local document: the global one stays absent for i18n.js.
const document = {addEventListener: (type, handler, options) => listeners.push({type, handler, options})};
Object.defineProperty(globalThis, "navigator", {value: {}, configurable: true});
""", asynchronous=True)
        self.assertEqual(out["failures"], [])
        mount = app_line("async function mountDisplay(")
        for needle in ('"data-card":"language"', '"data-card":"display"', '"data-card":"measure-link"'):
            self.assertIn(needle, mount)
        for gone in ('"/api/tracking"', '"/status"', "measurement-choice", "field-setup"):
            self.assertNotIn(gone, mount)
        self.assertNotIn('"data-card":"session"', APP_JS)
        self.assertNotIn("function mountSettings(", APP_JS)

    def test_header_sign_out_follows_the_language(self):
        out = run_app_node(("const tr=", "const V2_DE=", "function localText(", "function runButtonAction(",
                            "function headerSignOut("), r"""
const read = () => ({text: label.textContent, aria: button.attrs["aria-label"], title: button.title});
RtkI18n.setLanguage("en");
headerSignOut();
eq("english", read(), {text: "Sign out", aria: "Sign out", title: "Sign out"});
await button.onclick({});
await new Promise(resolve => setTimeout(resolve, 0));
eq("click signs out", logouts, 1);
RtkI18n.setLanguage("de");
headerSignOut();
eq("german", read(), {text: dict.de["ui.sign_out"], aria: dict.de["ui.sign_out"], title: dict.de["ui.sign_out"]});
eq("german word", dict.de["ui.sign_out"], "Abmelden");
await button.onclick({});
await new Promise(resolve => setTimeout(resolve, 0));
eq("one handler after two calls", logouts, 2);
// A refused DELETE (network error, timeout, CSRF refusal) goes to showError; the button is free again afterwards.
failure = new Error("refused");
const unhandled = [];
process.on("unhandledRejection", reason => unhandled.push(String(reason && reason.message || reason)));
button.onclick({});
eq("busy while signing out", [button.disabled, button.attrs["aria-busy"]], [true, "true"]);
await new Promise(resolve => setTimeout(resolve, 0));
await new Promise(resolve => setTimeout(resolve, 0));
eq("the rejection is shown", errors, ["refused"]);
eq("no unhandled rejection", unhandled, []);
eq("free again", [button.disabled, button.attrs["aria-busy"]], [false, undefined]);
RtkI18n.setLanguage("en");
""", stubs=r"""
const label = {textContent: "Sign out"};
const button = {attrs: {}, title: "", onclick: null, disabled: false, isConnected: true,
  classList: {add() {}, remove() {}},
  setAttribute(name, value) { this.attrs[name] = value; }, removeAttribute(name) { delete this.attrs[name]; },
  querySelector: selector => selector === ".header-action-label" ? label : null};
const $ = id => id === "sign-out" ? button : null;
let logouts = 0, failure = null;
const errors = [];
const showError = error => errors.push(error.message);
async function logout() { logouts += 1; if (failure) throw failure; }
""", asynchronous=True)
        self.assertEqual(out["failures"], [])

    def test_boot_wires_the_header_button(self):
        boot = source("99-boot.js")
        # Once at start and again after every language change (the label is not part of the re-rendered page).
        self.assertGreaterEqual(boot.count("headerSignOut()"), 2)
        handler = boot[boot.index('"rtk-language-change"'):]
        self.assertIn("headerSignOut()", handler.split("\n")[0])

    def test_logout_asks_before_discarding_a_dirty_form(self):
        out = run_app_node(("const LEAVE_QUESTION_PAUSE_MS=", "let leavingDocument=", "function leaveDocument(", "async function logout("), r"""
const run = async (dirty, accept) => {
  calls.length = 0; dialogs.length = 0; replaced.length = 0; form.dataset.dirty = dirty ? "true" : "false"; form.released = false;
  answer = accept;
  await logout();
  return {calls: calls.slice(), dialogs: dialogs.length, replaced: replaced.slice(), released: form.released, dirty: form.dataset.dirty};
};
eq("clean form signs out", await run(false, true),
  {calls: ["DELETE /api/session csrf"], dialogs: 0, replaced: ["/"], released: false, dirty: "false"});
const cancelled = await run(true, false);
eq("cancel: no DELETE, no navigation", [cancelled.calls, cancelled.replaced, cancelled.released], [[], [], false]);
eq("cancel: the form stays dirty", cancelled.dirty, "true");
eq("one dialog with the link guard texts", dialogs, [{title: "Discard unsaved changes?",
  description: "The Wi-Fi and NTRIP values on this page have not been saved.", submit: "Discard changes", danger: true}]);
const accepted = await run(true, true);
eq("accept: guard released, then DELETE and leave", [accepted.released, accepted.calls, accepted.replaced, accepted.dialogs],
  [true, ["DELETE /api/session csrf"], ["/"], 1]);
""", stubs=r"""
const calls = [], dialogs = [], replaced = [];
let answer = true;
const form = {dataset: {dirty: "false"}, released: false};
const main = {querySelector: selector => selector === '.configuration-layout[data-dirty="true"]' && form.dataset.dirty === "true" ? form : null};
const releaseDirtyGuard = () => { form.released = true; form.dataset.dirty = "false"; };
const modal = async options => { dialogs.push(options); return answer; };
const api = {csrf: async () => "csrf", request: async (path, options) => { calls.push(options.method + " " + path + " " + options.headers["X-CSRF-Token"]); }};
globalThis.location = {replace: url => replaced.push(url)};
""", asynchronous=True)
        self.assertEqual(out["failures"], [])


# Diagnostics with expert pages and the maintenance area (#24-UI Task 6).
class TestDiagnosticsAndMaintenance(unittest.TestCase):

    def test_restart_and_backup_live_under_maintenance(self):
        out = run_app_node(("const tr=", "const V2_DE=", "function localText(", "async function mountMaintenance("), r"""
const run = async lang => {
  RtkI18n.setLanguage(lang);
  main.children = []; api.calls = [];
  const controller = new AbortController();
  await mountMaintenance({view: "maintenance"}, controller.signal);
  return main.children[0];
};
const layout = await run("en");
eq("layout", layout.attrs.class, "maintenance-layout");
const cards = layout.children.filter(node => node.attrs && node.attrs["data-card"]);
eq("cards", cards.map(node => node.attrs["data-card"]), ["config-backup", "restart"]);
if (cards.length !== 2) return;
eq("no request", api.calls, []);
eq("connection state", connection, ["Maintenance ready", "ok"]);
const backup = cards[0];
eq("restore notice slot: the card carries .config-backup", /(^| )config-backup( |$)/.test(backup.attrs.class), true);
eq("title", textOf(walk(backup).find(node => node.tag === "h2")), "Device configuration");
const english = textAll(backup);
eq("scope names the measurement rules", english.includes("measurement rules"), true);
eq("secrets sentence", english.includes("The file contains Wi-Fi and NTRIP passwords in plain text. Keep it like a key."), true);
eq("survey hint", english.includes("Survey data (projects and points) are not part of it; back them up under Projects → Data management."), true);
eq("survey link", walk(backup).filter(node => node.tag === "a").map(node => node.attrs.href), ["/#projects", "/api/config/backup"]);
const download = walk(backup).find(node => node.tag === "a" && node.attrs.href === "/api/config/backup");
eq("download", [download.attrs.class, download.attrs.textRaw], ["btn secondary", "Download device configuration"]);
const buttons = walk(backup).filter(node => node.tag === "button");
eq("restore button", buttons.map(node => [node.attrs.text, node.attrs.handler === restoreConfiguration]), [["Restore device configuration", true]]);
const restart = cards[1];
eq("restart title", textOf(walk(restart).find(node => node.tag === "h2")), "Restart");
eq("restart scope", textAll(restart).includes("The web app and the data streams pause until the device is back. Survey data and configuration are kept."), true);
eq("restart button", walk(restart).filter(node => node.tag === "button").map(node => [node.attrs.text, node.attrs.kind, node.attrs.handler === rebootDevice]),
  [["Restart device", "danger", true]]);
await run("de");
const de = dict.de, german = textAll(main);
for (const key of ["ui.maintenance_backup_title", "ui.maintenance_secrets", "ui.maintenance_survey_hint", "ui.maintenance_restart_title",
  "ui.maintenance_restart_scope", "ui.guided_backup_help", "ui.guided_backup_kicker", "ui.guided_backup_download",
  "ui.guided_backup_restore"])
  eq("german " + key, german.includes(de[key]), true);
eq("german scope names the rules", de["ui.guided_backup_help"].includes("Messregeln"), true);
RtkI18n.setLanguage("en");
""", stubs=TREE_STUB + TEXT_STUB + r"""
const main = {children: []};
const api = {calls: [], json: async path => { api.calls.push(path); throw new Error("the maintenance page must not fetch"); }};
let connection = null;
const setConnection = (...args) => { connection = args; };
function restoreConfiguration() {}
function rebootDevice() {}
async function mountConfiguration() { main.children = [h("form", {class: "configuration-layout"})]; }
""", asynchronous=True)
        self.assertEqual(out["failures"], [])
        # Static: the three actions have exactly one place each, and the form has none of them any more.
        self.assertEqual(APP_JS.count('iconButton("Restart device",rebootDevice'), 1)
        self.assertEqual(APP_JS.count('href:"/api/config/backup"'), 1)
        self.assertEqual(APP_JS.count("restoreConfiguration,"), 1)
        maintenance = app_line("async function mountMaintenance(")
        for needle in ('iconButton("Restart device",rebootDevice', 'href:"/api/config/backup"', "restoreConfiguration,"):
            self.assertIn(needle, maintenance)
        form = source("80-configuration.js")
        form = form[form.index("async function mountConfiguration("):]
        for gone in ('"/api/config/backup"', "restoreConfiguration", "config-backup", "restoreButton"):
            self.assertNotIn(gone, form)
        # The console no longer restarts; the overview never did (E9).
        self.assertNotIn("rebootDevice", app_line("async function mountModule("))

    def test_maintenance_view_mounts_the_maintenance_page(self):
        router = source("90-live-router.js")
        self.assertIn("maintenance:(route,signal)=>mountMaintenance(route,signal)", router)
        self.assertNotIn("maintenance:(route,signal)=>mountConfiguration", router)

    def test_diagnostics_lead_to_expert_pages(self):
        out = run_app_node(AREA_PREFIXES + ("const V2_DE=", "function localText(", "function capacityText(", "const dataRow=",
                                            "const pageIntro=", "async function mountDiagnostics("), r"""
const status = """ + DEVICE_STATUS + r""";
api.answer = () => status;
const run = async lang => {
  RtkI18n.setLanguage(lang);
  main.children = []; api.calls = [];
  await mountDiagnostics(new AbortController().signal);
};
await run("en");
eq("one status request", api.calls, ["/status"]);
const navs = walk(main).filter(node => node.tag === "nav");
eq("one expert menu", navs.map(node => node.attrs.class), ["device-links diagnostic-links"]);
const nav = navs[0];
eq("menu follows the figures row", main.children[main.children.findIndex(node => /diagnostics-summary/.test(node.attrs.class || "")) + 1], nav);
const links = walk(nav).filter(node => node.tag === "a");
eq("three links in order", links.map(link => link.attrs.href),
  ["/#device/diagnostics/rtcm", "/#device/diagnostics/log", "/#device/diagnostics/console"]);
const small = link => textRaw(walk(link).find(node => node.tag === "small"));
const bold = link => textRaw(walk(link).find(node => node.tag === "b"));
eq("titles", links.map(bold), ["RTCM message details", "Log", "Receiver console"]);
eq("short lines", links.map(small), ["MSM5", "4 entries", "For experts: commands to the receiver"]);
const hrefs = walk(main).filter(node => node.tag === "a").map(node => node.attrs.href);
eq("support package stays", hrefs.includes("/api/diagnostics?download=1"), true);
eq("no label or access link", hrefs.filter(href => /device\/label|device\/connections\/access/.test(href)), []);
eq("no label text", textAll(main).includes("label & QR"), false);
eq("field diagnostics still mounts", mounted, 1);
await run("de");
const de = dict.de, germanLinks = walk(main).filter(node => node.tag === "nav").flatMap(node => walk(node).filter(item => item.tag === "a"));
eq("german titles", germanLinks.map(bold), [de["ui.route_rtcm"], de["ui.route_log"], de["ui.route_console"]]);
eq("german expert line", small(germanLinks[2]), de["ui.expert_only"]);
eq("english expert line", dict.en["ui.expert_only"], "For experts: commands to the receiver");
RtkI18n.setLanguage("en");
""", stubs=TREE_STUB + TEXT_STUB + r"""
const main = {children: []};
const textRaw = node => node && (node.attrs.text || node.attrs.textRaw);
const api = {calls: [], answer: null, json: async path => { api.calls.push(path); return api.answer(); }};
const skeleton = () => h("skeleton", {});
const fmt = (value, digits = 2) => Number(value).toFixed(digits);
const quantity = (count, one, many) => count + " " + (count === 1 ? one : many);
const duration = seconds => seconds + " s";
const setConnection = () => {};
const notice = (message, kind) => h("div", {class: "notice " + kind, text: message});
const renderRoute = () => {};
let mounted = 0;
async function mountFieldDiagnostics() { mounted += 1; }
""", asynchronous=True)
        self.assertEqual(out["failures"], [])

    def test_console_points_to_maintenance_for_the_restart(self):
        mount = app_line("async function mountModule(")
        self.assertIn('href:"/#device/maintenance"', mount)
        self.assertIn('tr("ui.console_restart_link","Restart the device: Maintenance")', mount)
        self.assertIn('href:"/#device/diagnostics",text:"Open support package"', mount)
        self.assertNotIn("destructive-zone", mount)


LIBRARY_STUBS = TREE_STUB + TEXT_STUB + r"""
const main = {children: []};
const skeleton = () => h("skeleton", {});
const quantity = (count, one, many) => count + " " + (count === 1 ? one : many);
const fmt = (value, digits = 2) => Number(value).toFixed(digits);
const dataRow = (label, value) => h("div", {class: "data-row", "data-label": label, text: value});
const setConnection = (...args) => { connection = args; };
let connection = null;
const store = {state: {}, set(patch) { Object.assign(this.state, patch); }};
const PROJECT_READ_TIMEOUT = 20000;
const api = {calls: [], answer: null, json: async (path, options) => { api.calls.push([path, options && options.signal === signalSeen]); return api.answer(); }};
let signalSeen = null;
const projectCard = project => h("button", {class: "project-card", textRaw: project.name});
const frameNotice = () => [];
function createProject() {}
function restoreSurvey() {}
function compactSurvey() {}
function mountProjectDetail() { throw new Error("not a detail route"); }
function archiveLibrary(tracking) { return (tracking.archives || []).length ? h("section", {class: "archive-library"}) : []; }
"""
# The library and the archive mount through the route cache (#29b); these tests start each mount without an entry.
LIBRARY_CACHE_PREFIXES = ("const ROUTE_CACHE_LIMIT=", "const CACHED_VIEWS=", "const routeCache=", "function cacheGet(",
                          "function cachePut(", "async function swrMount(", "function trackingSignature(",
                          "function trackingFresh(")
LIBRARY_TRACKING = r"""{projects: [{id: "p1", name: "One"}, {id: "p2", name: "Two"}], active_project_id: "p1", storage_bytes: 2048,
  archives: [{id: "a1"}, {id: "a2"}]}"""


class TestProjectLibrary(unittest.TestCase):

    def test_library_has_one_export_place(self):
        library = app_line("function renderLibrary(")
        self.assertIn("render:renderLibrary", app_line("async function mountProjects("))
        self.assertNotIn("ACTIVE PROJECT EXPORT", library)
        self.assertNotIn("export-card", library)
        self.assertNotIn("format=geojson", library)
        # The complete survey backup has one place; the project view keeps its own per-project backup link.
        self.assertEqual(APP_JS.count('"/api/tracking/export?format=backup"'), 1)
        self.assertIn('"/api/tracking/export?format=backup"', library)
        self.assertNotIn("archiveLibrary", source("15-store-api.js"))
        self.assertIn("archive:(route,signal)=>mountArchive(route,signal)", source("90-live-router.js"))

    def test_library_links_to_archive_and_labels_the_data_backup(self):
        out = run_app_node(("const tr=", "const V2_DE=", "function localText(", "function capacityText(") +
                           LIBRARY_CACHE_PREFIXES + ("function renderLibrary(", "async function mountProjects("), r"""
const tracking = """ + LIBRARY_TRACKING + r""";
const run = async (lang, withArchives) => {
  RtkI18n.setLanguage(lang);
  main.children = []; api.calls = []; routeCache.clear();
  api.answer = () => Object.assign({}, tracking, {archives: withArchives ? tracking.archives : []});
  const controller = new AbortController();
  signalSeen = controller.signal;
  await mountProjects({view: "projects", path: "projects"}, controller.signal);
};
const cls = node => node.attrs && node.attrs.class;
await run("en", true);
eq("one tracking request with the signal", api.calls, [["/api/tracking", true]]);
eq("children", main.children.map(cls), ["screen-toolbar", "project-list", "archive-entry", "tool-card data-management"]);
const entry = main.children.find(node => cls(node) === "archive-entry");
eq("entry", entry && [entry.tag, entry.attrs.href, entry.attrs.text], ["a", "/#archive", "Archived projects (2)"]);
const details = main.children.find(node => /data-management/.test(cls(node) || "")) || h("missing", {});
eq("details", details.tag, "details");
const summary = walk(details).find(node => node.tag === "summary");
eq("summary", summary && summary.attrs.text, "Survey data backup & data management");
eq("scope sentence", textAll(details).includes(String(dict.en["ui.data_management_scope"])), true);
eq("scope names the device area", String(dict.en["ui.data_management_scope"]).includes("Device → Maintenance"), true);
eq("links", walk(details).filter(node => node.tag === "a").map(node => [node.attrs.href, cls(node)]),
  [["/#device/maintenance", "text-link"], ["/api/tracking/export?format=backup", "btn secondary"]]);
eq("backup download text", (walk(details).find(node => node.tag === "a" && /format=backup/.test(node.attrs.href)) || {attrs: {}}).attrs.text, "Download survey data backup");
eq("journal row", walk(details).filter(node => (node.attrs || {})["data-label"]).map(node => [node.attrs["data-label"], node.attrs.text]), [["Recording journal", "2.0 KB"]]);
eq("buttons", walk(details).filter(node => node.tag === "button").map(node => [node.attrs.text, node.attrs.handler === restoreSurvey, node.attrs.handler === compactSurvey]),
  [["Restore backup", true, false], ["Optimize storage", false, true]]);
eq("no export card", main.children.filter(node => /export/.test(cls(node) || "")).length, 0);
eq("no geojson or csv link", walk(main).filter(node => node.tag === "a" && /geojson|csv/.test(node.attrs.href || "")).length, 0);
await run("de", true);
const german = main.children.find(node => cls(node) === "archive-entry");
eq("german entry", german && german.attrs.text, String(dict.de["ui.archive_entry"]).replace("{count}", "2"));
eq("german summary", textAll(main).includes(dict.de["ui.data_management_title"]), true);
eq("german scope", textAll(main).includes(dict.de["ui.data_management_scope"]), true);
eq("german download", textAll(main).includes(dict.de["ui.survey_backup_download"]), true);
await run("en", false);
eq("no archives: no entry", main.children.map(cls), ["screen-toolbar", "project-list", "tool-card data-management"]);
RtkI18n.setLanguage("en");
""", stubs=LIBRARY_STUBS, asynchronous=True)
        self.assertEqual(out["failures"], [])

    def test_library_texts_are_defined_in_both_languages(self):
        out = run_app_node((), r"""
const expected = {
  "ui.archive_entry": ["Archived projects ({count})"],
  "ui.archive_empty": ["No archived projects."],
  "ui.data_management_title": ["Survey data backup & data management"],
  "ui.data_management_scope": ["Backs up and restores all projects with their points. The device configuration (Wi-Fi, NTRIP, measurement settings) is backed up separately under Device → Maintenance."],
  "ui.survey_backup_download": ["Download survey data backup"]};
for (const [key, [en]] of Object.entries(expected)) {
  eq(key + " en", dict.en[key], en);
  const de = dict.de[key];
  eq(key + " de translated", [typeof de === "string" && de.trim() !== "", de !== en, (de.match(/\{\w+\}/g) || []).sort()], [true, true, (en.match(/\{\w+\}/g) || []).sort()]);
}
""")
        self.assertEqual(out["failures"], [])

    def test_archive_view_lists_archives_with_a_way_back(self):
        out = run_app_node(("const tr=", "const V2_DE=", "function localText(") + LIBRARY_CACHE_PREFIXES +
                           ("function renderArchive(", "async function mountArchive("), r"""
const tracking = """ + LIBRARY_TRACKING + r""";
const run = async (lang, withArchives, abortOnAnswer) => {
  RtkI18n.setLanguage(lang);
  main.children = []; api.calls = []; delete store.state.tracking; routeCache.clear();
  const controller = new AbortController();
  signalSeen = controller.signal;
  api.answer = () => { if (abortOnAnswer) controller.abort(); return Object.assign({}, tracking, {archives: withArchives ? tracking.archives : []}); };
  await mountArchive({view: "archive", path: "archive"}, controller.signal);
};
const cls = node => node.attrs && node.attrs.class;
await run("en", true, false);
eq("one tracking request with the signal", api.calls, [["/api/tracking", true]]);
eq("back link and library", main.children.map(node => cls(node)), ["text-link", "archive-library"]);
if (main.children.length !== 2) return;
eq("back link", [main.children[0].tag, main.children[0].attrs.href, main.children[0].attrs.text], ["a", "/#projects", "Back to projects"]);
eq("tracking kept", store.state.tracking && store.state.tracking.archives.length, 2);
eq("connection", connection, ["Projects updated", "ok"]);
await run("en", false, false);
eq("empty: back link and notice", main.children.map(node => cls(node)), ["text-link", "empty-state"]);
eq("empty text", textAll(main.children[1]).trim(), "No archived projects.");
await run("de", false, false);
eq("german empty text", textAll(main.children[1]).trim(), dict.de["ui.archive_empty"]);
eq("german back link", main.children[0].attrs.text, "Back to projects");
await run("en", true, true);
eq("aborted: skeleton only", main.children.map(node => node.tag), ["skeleton"]);
eq("aborted: tracking not kept", store.state.tracking, undefined);
RtkI18n.setLanguage("en");
""", stubs=LIBRARY_STUBS, asynchronous=True)
        self.assertEqual(out["failures"], [])

    def test_store_no_longer_appends_the_archive_library(self):
        out = run_app_node(("const store=",), r"""
store.set({tracking: {archives: [{id: "a1"}]}});
await new Promise(resolve => setTimeout(resolve, 20));
eq("nothing appended to the library", appended, 0);
eq("nothing asked of the archive card builder", built, 0);
""", stubs=r"""
let appended = 0, built = 0;
const main = {append() { appended += 1; }, querySelector: () => null};
const currentRoute = () => ({path: "projects"});
const archiveLibrary = () => { built += 1; return {}; };
const observeCapacity = () => {};
""", asynchronous=True)
        self.assertEqual(out["failures"], [])

    def test_survey_maintenance_actions_end_quietly_after_navigation(self):
        out = run_app_node(("async function compactSurvey(",), r"""
async function act(run, navigate) {
  renders = 0; activeController = new AbortController();
  const done = run();
  if (navigate) activeController.abort();
  await done;
  return renders;
}
eq("compact stays on its view", await act(() => compactSurvey(), false), 1);
eq("compact after navigation: no render", await act(() => compactSurvey(), true), 0);
eq("compact after navigation: still reported", feedbackKinds.slice(-1), ["ok"]);
async function restore(navigate) {
  renders = 0; activeController = new AbortController();
  restoreSurvey();
  const input = lastInput;
  input.files = [{text: async () => "{}"}];
  const finished = input.listeners.change();
  if (navigate) activeController.abort();
  await finished;
  return renders;
}
eq("restore stays on its view", await restore(false), 1);
eq("restore after navigation: no render", await restore(true), 0);
""", stubs=r"""
let renders = 0, activeController = null, modalAnswer = true, lastInput = null;
const feedbackKinds = [];
const h = (tag, attrs) => { const node = {tag, attrs: attrs || {}, files: [], listeners: {}, addEventListener(name, fn) { this.listeners[name] = fn; }, click() {}}; lastInput = node; return node; };
const tr = (key, english) => english;
const renderRoute = () => { renders += 1; };
const modal = async () => modalAnswer;
const isNdjsonBackup = () => true;
const api = {write: async () => ({json: async () => ({projects: []})})};
const applyTrackingMutation = result => result;
const trackingAction = async () => ({});
const reportLegacySkipped = () => {};
const showError = error => { throw error; };
const rejectionInfo = error => ({text: String(error)});
const restoreSummaryText = () => "restored";
const FEEDBACK_NOTICE_MS = 1, FEEDBACK_SUCCESS_MS = 1;
const showFeedback = entry => { feedbackKinds.push(entry.kind); };
const forgetTrackingCache = () => {};
""" + multi_line_function("async function restoreSurvey(") + r"""
async function maintenanceAction(running, run, success) { const result = await run(); showFeedback(Object.assign({key: "maintenance"}, success(result))); return result; }
""", asynchronous=True)
        self.assertEqual(out["failures"], [])



EXPORT_PREFIXES = ("const tr=", "const V2_DE=", "function localText(", "function projectExportFormats(",
                   "function geojsonExportActions(")
EXPORT_STUBS = TREE_STUB
# History stubs for the sheet binding: entries with state and URL; history.back() is queued like in a browser and lands
# later through deliver(target), which fires popstate; a hash assignment is a fragment navigation with a synchronous popstate.
HISTORY_PREFIXES = ("const LEAVE_QUESTION_PAUSE_MS=", "let leavingDocument=", "function leaveDocument(", "let closingSheetFromHistory=", "function pushSheetEntry(",
                    "function bindSheetHistory(", "function onSheetPopstate(", "async function requestSheetClose(")
HISTORY_STUBS = r"""
const base = "http://local.test/";
let entries = [], index = 0, backs = 0, pushes = 0, queued = 0;
const timers = [];
const setTimeout = (fn, ms) => { timers.push([fn, ms]); return timers.length; };
const overlay = {dialogs: [], querySelectorAll() { return this.dialogs.filter(dialog => dialog.open); },
  querySelector() { return this.querySelectorAll()[0] || null; }};
const copy = value => value == null ? null : JSON.parse(JSON.stringify(value));
const history = {
  get state() { return entries[index].state; },
  pushState(state, title, url) { entries.splice(index + 1); entries.push({state: copy(state), url: url || entries[index].url}); index += 1; pushes += 1; },
  replaceState(state, title, url) { entries[index] = {state: copy(state), url: url || entries[index].url}; },
  back() { backs += 1; queued += 1; }};
const location = {
  get href() { return entries[index].url; },
  get hash() { return "#" + entries[index].url.split("#")[1]; },
  set hash(value) { entries.splice(index + 1); entries.push({state: null, url: base + "#" + String(value).replace(/^#/, "")}); index += 1; onSheetPopstate(); }};
const reset = hash => { entries = [{state: null, url: base + "#" + hash}]; index = 0; backs = 0; pushes = 0; queued = 0; overlay.dialogs = []; timers.length = 0;
  ignoredSheetPops = []; sheetNavigation = null; leavingDocument = false; leaveQuestion = null; leavePause = null; pendingSheetSteps.clear();
  dirty = false; released = 0; answerQuestion = null; questions.length = 0; };
// A queued history.back() lands: one entry down from the page's entry, or where the browser put it (target).
const deliver = target => { if (!queued) { failures.push("popstate delivered without a queued history.back()"); return; } queued -= 1; index = target === undefined ? index - 1 : target; onSheetPopstate(); };
// The user presses Back: one entry down, then popstate.
const userBack = () => { index -= 1; onSheetPopstate(); };
const sheet = guard => {
  const dialog = {open: true, closes: 0, returnValue: "", handlers: [],
    addEventListener(name, fn) { if (name === "close") this.handlers.push(fn); },
    close(value) { if (!this.open) return; this.open = false; this.closes += 1; if (value !== undefined) this.returnValue = value;
      const handlers = this.handlers; this.handlers = []; handlers.forEach(fn => fn()); }};
  if (guard) dialog.closeGuard = guard;
  overlay.dialogs.push(dialog); bindSheetHistory(dialog); return dialog;
};
const tick = () => new Promise(resolve => process.nextTick(resolve));
const onSheet = () => Boolean(history.state && history.state.rtkSheet);
// The unsaved configuration form and the app's question (modal) for leaving the document: dirty is false unless a test sets it.
let dirty = false, released = 0, answerQuestion = null;
const questions = [];
const tr = (key, fallback) => fallback;
const main = {querySelector: selector => selector === '.configuration-layout[data-dirty="true"]' && dirty ? {} : null};
const releaseDirtyGuard = () => { dirty = false; released += 1; };
const modal = options => { questions.push(options); return new Promise(resolve => { answerQuestion = resolve; }); };
"""
PROJECT_ACTION_STUBS = r"""
let activeController = null, renders = 0, hashes = [], modalAnswer = {name: "New"}, release = null;
const messages = [];
const MEASURE_ROUTE = "measure";
const location = {set hash(value) { hashes.push(value); }};
const renderRoute = () => { renders += 1; };
const modal = async () => modalAnswer;
const trackingAction = () => new Promise(resolve => { release = resolve; });
const showSuccess = text => messages.push(["ok", text]);
const showError = error => messages.push(["critical", String(error)]);
const setConnection = () => {};
const capacityText = (text, values) => text.replace(/\{(\w+)\}/g, (_, key) => values[key]);
const capacityNumber = value => value;
const localText = value => value;
"""


class TestProjectView(unittest.TestCase):

    def test_project_export_formats(self):
        out = run_app_node(EXPORT_PREFIXES, r"""
RtkI18n.setLanguage("de");
const formats = projectExportFormats({id: "a/b", name: "Field"});
eq("formats", formats.map(item => item.format), ["geojson", "csv", "archive"]);
eq("geojson has no href", "href" in (formats[0] || {href: "missing row"}), false);
eq("csv href", (formats[1] || {}).href, "/api/tracking/export?project_id=a%2Fb&format=csv");
eq("archive href", (formats[2] || {}).href, "/api/tracking/export?project_id=a%2Fb&format=backup");
const parts = key => String(dict.de[key]).split(" – ");
eq("german names and descriptions", formats.map(item => [item.label, item.description]),
  [parts("ui.export_geojson"), parts("ui.export_csv"), parts("ui.export_archive")]);
RtkI18n.setLanguage("en");
eq("english names and descriptions", projectExportFormats({id: "p1"}).map(item => [item.label, item.description]),
  [["GeoJSON", "lines and object points"], ["Point CSV", "object points"], ["Project archive", "can be restored"]]);
""", stubs=EXPORT_STUBS)
        self.assertEqual(out["failures"], [])

    def test_export_action_texts_are_fixed(self):
        out = run_app_node(EXPORT_PREFIXES, r"""
eq("en share", dict.en["ui.export_share"], "Share file");
eq("de share translated", dict.de["ui.export_share"] !== dict.en["ui.export_share"], true);
eq("en download", dict.en["ui.export_download"], "Download file");
eq("de download translated", dict.de["ui.export_download"] !== dict.en["ui.export_download"], true);
""", stubs=EXPORT_STUBS)
        self.assertEqual(out["failures"], [])
        # #46: the ready actions of the export flow carry these texts (the share and download cases are browser cases).
        component = test_ui.app_function_source("function exportGeojsonControl(")
        self.assertIn('tr("ui.export_share"', component)
        self.assertIn('tr("ui.export_download"', component)

    def test_export_has_one_geojson_place(self):
        self.assertEqual(APP_JS.count("format=geojson"), 1)
        self.assertIn("format=geojson", test_ui.app_function_source("function exportGeojsonControl("))
        # The log page keeps its own text share; the project view has no share closure any more.
        self.assertNotIn("const share=async", source("70-project-detail.js"))

    def test_sheet_close_guard(self):
        out = run_app_node(("async function requestSheetClose(", "const EXPORT_SHEET_GUARDS=", "function guardExportSheetClose("), r"""
const stub = guard => { const dialog = {open: true, closes: 0, close() { this.closes += 1; this.open = false; }}; if (guard) dialog.closeGuard = guard; return dialog; };
const run = async guard => { const dialog = stub(guard); const result = await requestSheetClose(dialog); return [result, dialog.open, dialog.closes]; };
eq("no guard", await run(null), [true, false, 1]);
eq("guard false", await run(() => false), [false, true, 0]);
eq("guard Promise false", await run(() => Promise.resolve(false)), [false, true, 0]);
eq("guard yes is not true", await run(() => "yes"), [false, true, 0]);
eq("guard Promise true", await run(() => Promise.resolve(true)), [true, false, 1]);
eq("guard throws", await run(() => { throw new Error("broken"); }), [true, false, 1]);
eq("guard rejects", await run(() => Promise.reject(new Error("broken"))), [true, false, 1]);
const closed = stub(() => true); closed.open = false;
eq("an already closed sheet is not closed again", [await requestSheetClose(closed), closed.closes], [true, 0]);
const s1 = {}, s2 = {}, g1 = () => true, g2 = () => false, g3 = () => true;
guardExportSheetClose(s1, g1); guardExportSheetClose(s2, g2);
eq("registered per signal", [EXPORT_SHEET_GUARDS.get(s1) === g1, EXPORT_SHEET_GUARDS.get(s2) === g2], [true, true]);
guardExportSheetClose(s1, g3);
eq("a second call replaces", [EXPORT_SHEET_GUARDS.get(s1) === g3, EXPORT_SHEET_GUARDS.get(s2) === g2], [true, true]);
guardExportSheetClose(s1, null);
eq("null removes", EXPORT_SHEET_GUARDS.has(s1), false);
""", asynchronous=True)
        self.assertEqual(out["failures"], [])

    def test_sheet_history_ignores_its_own_back(self):
        out = run_app_node(HISTORY_PREFIXES, r"""
// (a) Back closes a sheet without a guard, without a history.back() of its own.
reset("projects/p1");
let a = sheet();
eq("a: sheet entry", [onSheet(), entries.length], [true, 2]);
userBack();
eq("a: closed by Back, no own back", [a.open, backs, onSheet()], [false, 0, false]);
// (b) A dialog over the sheet closes normally: one back, its popstate is ignored, the sheet stays open.
reset("projects/p1");
const b = sheet(), question = sheet();
question.close();
eq("b: one back", backs, 1);
deliver();
eq("b: the sheet stays open on its own entry", [b.open, onSheet(), index], [true, true, 1]);
b.close();
deliver();
eq("b: the sheet's own back lands on the page", [b.open, backs, onSheet(), index], [false, 2, false, 0]);
// (c) Guard says no: the entry is restored, the sheet stays.
reset("projects/p1");
const c = sheet(() => false);
userBack();
eq("c: entry restored", [pushes, onSheet()], [2, true]);
await tick(); await tick();
eq("c: sheet open", [c.open, backs], [true, 0]);
// (d) Guard says yes: restored, closed, then exactly one back removes the restored entry.
reset("projects/p1");
const d = sheet(() => true);
userBack();
await tick(); await tick();
eq("d: closed after the restore", [pushes, d.open, backs], [2, false, 1]);
deliver();
eq("d: back on the page entry, no sheet entry left", [onSheet(), index, entries.length], [false, 0, 2]);
// (e) Two open dialogs: Back closes only the upper one.
reset("projects/p1");
const lower = sheet(), upper = sheet();
userBack();
eq("e: only the upper one closes", [upper.open, lower.open, backs], [false, true, 0]);
// (f) A navigation between the own back and its popstate is not undone (Chromium lands on the page entry).
reset("projects/p1");
const f = sheet();
f.close();
location.hash = "device";
deliver(0);
eq("f: the navigation stays", [location.hash, onSheet()], ["#device", false]);
eq("f: Back from there returns to the project", entries[index - 1] && entries[index - 1].url, base + "#projects/p1");
// (g) A sheet opened before the closed sheet's popstate arrives stays open and keeps an entry (R2, object point -> measure).
for (const target of [0, undefined]) {
  reset("projects/p1");
  const point = sheet();
  point.close();
  const measure = sheet();
  deliver(target);
  eq("g" + target + ": measure sheet open on a sheet entry", [measure.open, onSheet()], [true, true]);
  measure.close();
  deliver();
  eq("g" + target + ": its own back leaves no sheet entry", [measure.open, onSheet()], [false, false]);
}
// (h) The close settles after the own popstate, at once without a back, and at the latest after the wait.
reset("projects/p1");
const settledAfter = async dialog => { let done = false; Promise.resolve(dialog.sheetHistory).then(() => { done = true; }); await tick(); await tick(); return done; };
const h1 = sheet();
h1.close();
eq("h: not settled before the popstate", await settledAfter(h1), false);
deliver();
eq("h: settled after the popstate", await settledAfter(h1), true);
const h2 = sheet();
userBack();
eq("h: closed by Back settles at once", await settledAfter(h2), true);
const h3 = sheet();
h3.close();
const wait = timers[timers.length - 1];
eq("h: the wait is bounded", wait && wait[1], 1000);
if (wait) wait[0]();
eq("h: settled by the wait", await settledAfter(h3), true);
const late = sheet();
deliver(0);
eq("h: a late own popstate still does not close the next sheet", late.open, true);
""", stubs=HISTORY_STUBS, asynchronous=True)
        self.assertEqual(out["failures"], [])

    def test_sheet_closing_runs_through_one_path(self):
        export = app_line("function openProjectExport(")
        for needle in ("closeGuard", "requestSheetClose(dialog)", "preventDefault()",
                       "EXPORT_SHEET_GUARDS.get(sheetController.signal)", "geojsonExportActions(project,sheetController.signal)"):
            self.assertIn(needle, export, needle)
        navigation = source("20-navigation.js")
        self.assertIn('addEventListener("popstate",onSheetPopstate)', navigation)
        self.assertNotIn('overlay.querySelector("dialog[open]")', navigation)
        self.assertIn("bindSheetHistory(node)", navigation)

    def test_document_navigation_is_not_lost_to_a_sheet_history_step(self):
        # A sign-in redirect issued while a closing sheet's own history.back() is in flight was aborted when that
        # traversal landed (feedback step (h)): the document navigation waits for the step, and once it started no
        # sheet takes another one.
        out = run_app_node(HISTORY_PREFIXES + ("function navigateAfterSheet(",), r"""
const gone = [];
const leave = () => leaveDocument(() => gone.push(location.hash));
// (a) No sheet step pending: the document navigation starts at once, in the same task.
reset("measure");
gone.length = 0;
leave();
eq("a: goes at once without a pending step", gone, ["#measure"]);
// (b) A closed sheet's own back is in flight: the navigation waits until it landed, then goes exactly once.
reset("measure");
gone.length = 0;
const b = sheet();
b.close();
const waiting = leave();
await tick(); await tick();
eq("b: waits for the own back", [gone, backs], [[], 1]);
deliver();
await waiting;
eq("b: goes after the own back landed", gone, ["#measure"]);
// (c) Two steps in flight (object point sheet, then the measurement sheet): it waits for both.
reset("measure");
gone.length = 0;
const point = sheet();
point.close();
const measure = sheet();
deliver();
measure.close();
const both = leave();
await tick(); await tick();
eq("c: waits for the second own back", [gone, backs, measure.open], [[], 2, false]);
deliver();
await both;
eq("c: goes after the last own back", gone, ["#measure"]);
// (d) A sheet closing after the navigation started takes no history step and settles at once.
reset("measure");
gone.length = 0;
const d = sheet();
leave();
d.close();
let settled = false;
Promise.resolve(d.sheetHistory).then(() => { settled = true; });
await tick(); await tick();
eq("d: no history step after the start", [backs, queued, settled, gone], [0, 0, true, ["#measure"]]);
// (e) An own back landing while the document leaves restores no sheet entry and replays no fragment navigation.
reset("projects/p1");
gone.length = 0;
const e = sheet();
e.close();
location.hash = "device";
const next = sheet();
const pushesBefore = pushes, entriesBefore = entries.length;
const replay = leave();
deliver(0);
await replay;
eq("e: no entry pushed and no hash set", [pushes - pushesBefore, entries.length, location.hash, next.open],
  [0, entriesBefore, "#projects/p1", true]);
eq("e: the navigation went once", gone.length, 1);
// (f) A bounded wait: a popstate that never comes does not hold the navigation forever.
reset("measure");
gone.length = 0;
const f = sheet();
f.close();
const bounded = leave();
const wait = timers[timers.length - 1];
eq("f: the wait is bounded", wait && wait[1], 1000);
if (wait) wait[0]();
await bounded;
eq("f: goes after the wait", gone, ["#measure"]);
// (g) A sheet action's hash navigation that is still due does not run once the document leaves.
reset("measure");
gone.length = 0;
const g = sheet();
g.close();
const target = navigateAfterSheet(g, "device/connections/internet");
leave();
deliver();
await target;
eq("g: the hash stays", location.hash, "#measure");
""", stubs=HISTORY_STUBS, asynchronous=True)
        self.assertEqual(out["failures"], [])
        self.assertIn("leaveDocument(()=>location.replace(target),userAction)", app_line("function signInRedirect("))
        self.assertIn('leaveDocument(()=>location.replace("/"))', app_line("async function logout("))
        reload = multi_line_function("function reloadForSignIn(")
        self.assertLess(reload.index("leaveDocument(()=>{"), reload.index("history.replaceState("))
        self.assertLess(reload.index("history.replaceState("), reload.index("location.reload();"))

    def test_leaving_with_unsaved_changes_asks_first(self):
        # A 401 redirect over an unsaved configuration form (Codex P2): the browser's leave prompt could cancel it and
        # leave the page marked as leaving. The app asks first; cancel leaves everything as it was.
        out = run_app_node(HISTORY_PREFIXES + ("function navigateAfterSheet(",), r"""
const gone = [];
const leave = () => leaveDocument(() => gone.push(location.hash));
// (a) Cancel: no navigation, not leaving, the form stays dirty, and the page keeps its sheet history.
reset("device/connections/internet");
gone.length = 0;
dirty = true;
const cancelled = leave();
await tick();
eq("a: one question, nothing left yet", [questions.length, gone, leavingDocument], [1, [], false]);
eq("a: the question", questions[0], {title: "Discard unsaved changes?",
  description: "The session has ended. Signing in again discards the Wi-Fi and NTRIP values on this page that have not been saved.",
  submit: "Sign in again", danger: true});
answerQuestion(false);
await cancelled;
eq("a: cancel keeps the page", [gone, leavingDocument, dirty, released], [[], false, true, 0]);
const later = sheet();
later.close();
eq("a: a later sheet close still takes its history step", [backs, queued], [1, 1]);
deliver();
eq("a: and lands on the page entry", [onSheet(), index], [false, 0]);
const action = sheet();
action.close();
const moved = navigateAfterSheet(action, "device/maintenance");
deliver();
await moved;
eq("a: a sheet action still navigates", location.hash, "#device/maintenance");
// (b) The next attempt asks again; confirm releases the guard, then leaves once.
reset("device/connections/internet");
gone.length = 0;
dirty = true;
const confirmed = leave();
await tick();
answerQuestion(true);
await confirmed;
eq("b: confirm leaves once", [questions.length, released, dirty, leavingDocument, gone], [1, 1, false, true, ["#device/connections/internet"]]);
// (c) Further attempts while the question is open join it: one question, one navigation.
reset("device/connections/internet");
gone.length = 0;
dirty = true;
const first = leave(), second = leave();
await tick();
eq("c: one question", questions.length, 1);
answerQuestion(true);
await Promise.all([first, second]);
eq("c: one navigation", gone.length, 1);
// (d) Confirm with a sheet back still in flight: the navigation waits for it, as without unsaved changes.
reset("device/connections/internet");
gone.length = 0;
dirty = true;
const d = sheet();
d.close();
const waiting = leave();
await tick();
answerQuestion(true);
await tick(); await tick();
eq("d: waits for the own back", [gone, leavingDocument], [[], true]);
deliver();
await waiting;
eq("d: then leaves", gone.length, 1);
// (e) Nothing unsaved: no question, as before.
reset("measure");
gone.length = 0;
leave();
eq("e: no question, at once", [questions.length, gone], [0, ["#measure"]]);
""", stubs=HISTORY_STUBS, asynchronous=True)
        self.assertEqual(out["failures"], [])

    def test_cancelled_leaving_question_waits_before_it_asks_again(self):
        # A question on every 401 poll would make the page unusable: after Cancel, polls do not ask for 30 s; a user's
        # own write that gets a 401 asks at once.
        out = run_app_node(HISTORY_PREFIXES, r"""
const gone = [];
const leave = userAction => leaveDocument(() => gone.push(location.hash), userAction);
const pauses = () => timers.filter(timer => timer[1] === 30000);
reset("device/connections/internet");
dirty = true;
const first = leave(false);
await tick();
answerQuestion(false);
await first;
eq("cancel starts one 30 s pause", pauses().length, 1);
leave(false);
leave();
await tick();
eq("polls in the pause neither ask nor leave", [questions.length, gone, leavingDocument, dirty], [1, [], false, true]);
const own = leave(true);
await tick();
eq("a user's write in the pause asks at once", questions.length, 2);
answerQuestion(false);
await own;
eq("its cancel starts a new pause", pauses().length, 2);
pauses()[0][0]();
leave(false);
await tick();
eq("the first pause ending does not end the second", questions.length, 2);
pauses()[1][0]();
const again = leave(false);
await tick();
eq("after the pause the next 401 asks again", questions.length, 3);
answerQuestion(true);
await again;
eq("and confirm leaves", [gone.length, leavingDocument, released], [1, true, 1]);
// Without unsaved changes a 401 in the pause leaves at once.
reset("device/connections/internet");
gone.length = 0;
dirty = true;
const cancelled = leave(false);
await tick();
answerQuestion(false);
await cancelled;
dirty = false;
leave(false);
eq("a clean page leaves in the pause", [questions.length, gone.length], [1, 1]);
""", stubs=HISTORY_STUBS, asynchronous=True)
        self.assertEqual(out["failures"], [])
        self.assertIn("leaveDocument(()=>location.replace(target),userAction)", app_line("function signInRedirect(userAction)"))
        # A write (any method but GET) and the CSRF token fetch made for one count as the user's own action.
        self.assertIn('signInRedirect(Boolean(options.userAction||options.method&&options.method!=="GET"))', APP_JS)
        self.assertIn('this.json("/api/session",{userAction:true})', APP_JS)

    def test_leaving_question_texts_are_defined_in_both_languages(self):
        out = run_app_node((), r"""
const expected = {
  "ui.sign_in_again": ["Sign in again"],
  "ui.sign_in_unsaved": ["The session has ended. Signing in again discards the Wi-Fi and NTRIP values on this page that have not been saved."],
};
for (const [key, [en]] of Object.entries(expected)) {
  eq(key + " en", dict.en[key], en);
  const de = dict.de[key];
  eq(key + " de translated", [typeof de === "string" && de.trim() !== "", de !== en, (de.match(/\{\w+\}/g) || []).sort()], [true, true, (en.match(/\{\w+\}/g) || []).sort()]);
}
const title = RtkI18n.v2de["Discard unsaved changes?"];
eq("title translated", [typeof title === "string" && title.trim() !== "", title !== "Discard unsaved changes?"], [true, true]);
""")
        self.assertEqual(out["failures"], [])
        line = app_line("function leaveDocument(")
        for needle in ('tr("ui.sign_in_unsaved",', 'tr("ui.sign_in_again",', "releaseDirtyGuard()"):
            self.assertIn(needle, line, needle)

    def test_sheet_action_navigates_after_the_sheet_history(self):
        out = run_app_node(("let leavingDocument=", "function navigateAfterSheet("), r"""
let settle;
const dialog = {sheetHistory: new Promise(resolve => { settle = resolve; })};
location.hash = "#measure";
const done = navigateAfterSheet(dialog, "device/connections/internet");
await Promise.resolve();
eq("waits for the sheet history", location.hash, "#measure");
settle();
await done;
eq("then navigates", location.hash, "#device/connections/internet");
// A navigation made while the sheet's own back is due wins.
let settleLate;
const late = {sheetHistory: new Promise(resolve => { settleLate = resolve; })};
location.hash = "#measure";
const lateDone = navigateAfterSheet(late, "device/connections/internet");
location.hash = "#projects";
settleLate();
await lateDone;
eq("the navigation in the window stays", location.hash, "#projects");
// A dialog without history entry navigates at once.
location.hash = "#measure";
await navigateAfterSheet({}, "device/connections/internet");
eq("without sheet history", location.hash, "#device/connections/internet");
""", stubs=r"""
// Like the browser: the hash reads back with its leading "#".
let currentHash = "";
globalThis.location = {get hash() { return currentHash; }, set hash(value) { currentHash = "#" + String(value).replace(/^#/, ""); }};
""", asynchronous=True)
        self.assertEqual(out["failures"], [])
        self.assertIn("{dialog.close();return navigateAfterSheet(dialog,HELP_ROUTES[step.action.routeKey]);}",
                      app_line("function whyStepNode("))

    def test_project_panel_order(self):
        detail = multi_line_function("async function mountProjectDetail(")
        # WP4 (user request 30.10.): the stakeout entry sits right below the map, before the lists: map, project-tools, lists.
        order = ['"data-slot":"project-tools"', '"PROJECT WORKSPACE"', "frameSlot,\n", "project-activation", '"Survey lines"',
                 "project-actions", "project-danger"]
        positions = [detail.find(needle) for needle in order]
        self.assertNotIn(-1, positions, dict(zip(order, positions)))
        self.assertEqual(positions, sorted(positions))
        for gone in ("export-links", "text-actions", "Share GeoJSON"):
            self.assertNotIn(gone, detail)
        # #39 pin: the frame controls stay as they are.
        self.assertIn('h("div",{class:"frame-notice-slot"},frameAssignmentControls(project))', detail)
        for needle in ('href:"/#measure"', '"ui.go_measure"', '"ui.project_open_not_active"', "openProjectExport(project,signal)",
                       '"ui.project_actions"', '"ui.project_export"', '"ui.project_danger_title"', '"ui.project_delete"',
                       "activateProject(project,signal)"):
            self.assertIn(needle, detail, needle)

    def test_project_view_texts_are_defined_in_both_languages(self):
        out = run_app_node((), r"""
const expected = {
  "ui.project_actions": ["Project actions"],
  "ui.project_export": ["Export …"],
  "ui.export_title": ["Export project"],
  "ui.export_share_hint": ["Share hands the file to another app; Download saves it through the browser."],
  "ui.export_geojson": ["GeoJSON – lines and object points"],
  "ui.export_csv": ["Point CSV – object points"],
  "ui.export_archive": ["Project archive – can be restored"],
  "ui.export_share": ["Share file"],
  "ui.export_download": ["Download file"],
  "ui.project_open_not_active": ["Opening a project does not change the project you measure into."],
  "ui.go_measure": ["Go to Measure"],
  "ui.project_danger_title": ["Delete"],
  "ui.project_delete": ["Delete project"]};
for (const [key, [en]] of Object.entries(expected)) {
  eq(key + " en", dict.en[key], en);
  const de = dict.de[key];
  eq(key + " de translated", [typeof de === "string" && de.trim() !== "", de !== en, (de.match(/\{\w+\}/g) || []).sort()], [true, true, (en.match(/\{\w+\}/g) || []).sort()]);
}
""")
        self.assertEqual(out["failures"], [])

    def test_project_actions_end_quietly_after_navigation(self):
        out = run_app_node(("async function activateProject(", "async function renameProject(", "async function deleteProject("), r"""
const project = {id: "p2", name: "Two", feature_count: 3};
async function act(run, navigate) {
  renders = 0; hashes = []; messages.length = 0; release = null; activeController = new AbortController();
  const signal = activeController.signal, done = run(signal);
  for (let i = 0; i < 5 && !release; i += 1) await new Promise(resolve => process.nextTick(resolve));
  if (navigate) activeController.abort();
  if (release) release({});
  await done;
  return [renders, hashes, messages.map(item => item[0])];
}
eq("activate stays and shows the active state", await act(signal => activateProject(project, signal), false), [1, [], ["ok"]]);
eq("activate after navigation: reported only", await act(signal => activateProject(project, signal), true), [0, [], ["ok"]]);
eq("rename returns to the library", await act(() => renameProject(project), false), [0, ["projects"], []]);
eq("rename after navigation: stays where the user went", await act(() => renameProject(project), true), [0, [], []]);
modalAnswer = {confirm: "Two"};
eq("delete returns to the library", await act(() => deleteProject(project), false), [0, ["projects"], ["ok"]]);
eq("delete after navigation: reported only", await act(() => deleteProject(project), true), [0, [], ["ok"]]);
modalAnswer = true;
eq("archive returns to the library", await act(() => archiveProject(project), false), [0, ["projects"], ["ok"]]);
eq("archive after navigation: reported only", await act(() => archiveProject(project), true), [0, [], ["ok"]]);
eq("archive from Measure returns to Measure", await act(() => archiveProject(project, true, true), false), [0, ["measure"], ["ok"]]);
""", stubs=PROJECT_ACTION_STUBS + "\nconst forgetTrackingCache = () => {};\n" + multi_line_function("async function archiveProject("),
                           asynchronous=True)
        self.assertEqual(out["failures"], [])


if __name__ == "__main__":
    unittest.main()
