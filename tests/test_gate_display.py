# SPDX-License-Identifier: AGPL-3.0-only
"""Gate display in the field app: the header pill names a blocked measurement gate, the checklist
shows an unknown correction age as unknown, and each hero tile shows only the state of its own check.
Display only; the device gate is unchanged."""
import re
import unittest

import test_ui

APP_JS = test_ui.APP_JS
APP_CSS = test_ui.APP_CSS
NODE = test_ui.NODE
run_gate_node = test_ui.run_gate_node


def section(start, end):
    return APP_JS[APP_JS.index(start):APP_JS.index(end)]


class TestPillGate(unittest.TestCase):
    PREFIXES = ("function pillGate(",)
    EXPORTS = ("pillGate",)

    def test_pill_gate_state_under_node(self):
        if NODE is None:
            self.skipTest("node is unavailable")
        out = run_gate_node(r"""
const dgps = Object.assign({}, fix, {qual: 2, fix_status_text: "DGPS", receiver_accuracy: {horizontal_sigma_m: 5.7, altitude_sigma_m: 9}});
const rtk = Object.assign({}, fix, {qual: 4, fix_status_text: "RTK_FIXED", sats: 20, hdop: 0.6, correction_age_sec: 1.2});
const state = (l, link, stale) => g.pillGate(l, link || "live", Boolean(stale));
eq("blocked", state(reasonLive(["rtk_fixed_required"], {current_fix: dgps})), "blocked");
eq("blocked while delayed", state(reasonLive(["rtk_fixed_required"], {current_fix: dgps}), "delayed"), "blocked");
eq("ready", state(reasonLive([], {current_fix: rtk})), "ready");
eq("rejected without reason", state(withVerdict({accepted: false, reasons: [], limits}, {current_fix: rtk})), "blocked");
eq("accepted with a reason follows the rows", state(withVerdict({accepted: true, reasons: ["hdop_too_high"], limits}, {current_fix: rtk})), "blocked");
eq("accepted as a string", state(withVerdict({accepted: "yes", reasons: [], limits}, {current_fix: rtk})), "blocked");
for (const verdict of [null, undefined, "x", 7]) eq("no verdict " + verdict, state(withVerdict(verdict, {current_fix: rtk})), "unknown");
eq("no live", [null, undefined].map(item => state(item)), ["unknown", "unknown"]);
eq("stale fix", state(reasonLive([], {current_fix: rtk}), "live", true), "unknown");
eq("lost link", state(reasonLive(["rtk_fixed_required"], {current_fix: dgps}), "lost"), "unknown");
eq("connecting", state(reasonLive([], {current_fix: rtk}), "connecting"), "unknown");
""", extra_prefixes=self.PREFIXES, extra_exports=self.EXPORTS)
        self.assertEqual(out["failures"], [])

    def test_pill_label_carries_the_gate_and_the_dot_keeps_the_link(self):
        body = section("function updateGlobalStatus(", "function setHeader(")
        # The rule of #21b stays: class, symbol and data-link never come from the gate.
        self.assertIn("never from the measurement gate", body)
        apply_link = re.search(r"const applyLink=pill=>\{([^}]*)\}", body).group(1)
        self.assertNotIn("gate", apply_link.lower())
        self.assertEqual(body.count("label.dataset.gate="), 3, "startup, connection error and the normal path")
        self.assertIn("pillGate(live,link,stale)", body)
        for key in ("ui.pill_gate_blocked", "ui.pill_measurement_blocked"):
            self.assertIn('tr("%s"' % key, body)
        dictionaries = test_ui.I18N_JS
        for key in ("ui.pill_gate_blocked", "ui.pill_measurement_blocked"):
            self.assertEqual(dictionaries.count('"%s"' % key), 2, key + " in the en and de dictionaries")

    def test_pill_label_colours_come_from_tokens(self):
        self.assertIn('.live-state b[data-gate="blocked"]{color:var(--critical)}', APP_CSS)
        self.assertIn('.live-state b[data-gate="unknown"]{color:var(--muted)}', APP_CSS)
        self.assertNotIn('.live-state b[data-gate="ready"]', APP_CSS)


class TestUnknownCorrectionAge(unittest.TestCase):
    WHY_PREFIXES = test_ui.TestPage.WHY_PREFIXES
    WHY_EXPORTS = test_ui.TestPage.WHY_EXPORTS

    def test_corrections_row_is_unknown_without_an_age_under_node(self):
        if NODE is None:
            self.skipTest("node is unavailable")
        out = run_gate_node(r"""
const live = (age, reasons, extra) => reasonLive(reasons, Object.assign({current_fix: Object.assign({}, fix, {correction_age_sec: age})}, extra || {}));
const row = (age, reasons, extra) => g.gateRows(live(age, reasons, extra)).find(item => item.id === "corrections");
const noAge = Object.assign({}, fix); delete noAge.correction_age_sec;
eq("null age", row(null, []).state, "unknown");
eq("absent age", g.gateRows(reasonLive([], {current_fix: noAge})).find(item => item.id === "corrections").state, "unknown");
eq("known ages", [1.2, 5, 60].map(age => row(age, []).state), ["ok", "ok", "ok"]);
eq("device reason wins", [row(null, ["corrections_too_old"]).state, row(12, ["corrections_too_old"]).state], ["fail", "fail"]);
eq("detail unchanged", row(null, []).detail, "corrections age unknown (max. 5.0 s)");
eq("no reason texts", row(null, []).reasonTexts, []);
eq("other rows stay ok", g.gateRows(live(null, [])).filter(item => item.id !== "corrections").map(item => item.state), ["ok", "ok", "ok", "ok", "ok"]);
eq("no headline", g.gateHeadline(live(null, [])), null);
eq("stale fails every row", g.gateRows(live(null, [], {current_fix: Object.assign({}, fix, {correction_age_sec: null, stale: true})})).map(item => item.state),
  ["fail", "fail", "fail", "fail", "fail", "fail"]);
""")
        self.assertEqual(out["failures"], [])

    def test_why_corrections_step_is_unknown_without_an_age_under_node(self):
        if NODE is None:
            self.skipTest("node is unavailable")
        out = run_gate_node(r"""
const view = {age: 1000, link: "live"};
const base = (age, extra) => Object.assign({
  link: {receiver: "ok", uart_silence_ms: 40, corrections: "ok", rtcm_silence_ms: 90},
  network: {ntrip_state: "streaming", ntrip_config: "ready", access_state: "ONLINE"},
  gnss_accuracy: {state: "receiving"},
  rtcm: {mount: "MOUNT1", epoch_age_sec: 1, arrival_age_sec: 0.2, stale: false},
  current_fix: Object.assign({}, fix, {correction_age_sec: age})}, extra || {});
const steps = (age, reasons, extra) => g.whyNotSteps(reasonLive(reasons, base(age, extra)), view);
const unknown = steps(null, []);
eq("unknown age step", [unknown[4].state, unknown[4].text], ["unknown", dict.en["ui.why_corrections_age_unknown"]]);
eq("unknown age is no cause", g.whyNotCause(unknown), -1);
eq("other steps ok", unknown.filter(step => step.id !== "corrections").map(step => step.state), ["ok", "ok", "ok", "ok", "ok"]);
eq("known age ok", steps(1.2, [])[4].state, "ok");
eq("device reason fails", steps(null, ["corrections_too_old"])[4].state, "fail");
eq("late corrections still warn", steps(null, [], {link: {receiver: "ok", corrections: "stale"}})[4].state, "warn");
""", extra_prefixes=self.WHY_PREFIXES, extra_exports=self.WHY_EXPORTS)
        self.assertEqual(out["failures"], [])

    def test_unknown_rows_never_count_as_failing(self):
        core = section("function renderStatusCore(", "function measureActionModel(")
        model = APP_JS[APP_JS.index("function measureActionModel("):]
        model = model[:model.index("\n")]
        # Only a failing row closes the gate: an unknown row informs, it never blocks the hero or the actions.
        self.assertIn('rows=gateRows(live),accepted=!rows.some(row=>row.state==="fail")', core)
        self.assertIn('accepted=!gateRows(l).some(row=>row.state==="fail")', model)
        self.assertNotIn('every(row=>row.state==="ok")', APP_JS)

    def test_unknown_row_renders_neutral(self):
        node = APP_JS[APP_JS.index("function gateRowNode("):]
        node = node[:node.index("\n")]
        self.assertIn('unknown:"unknown"', node.replace(" ", ""))
        self.assertIn(".condition.warn,.condition.unknown{color:var(--warn)}", APP_CSS)


class TestHeroMetricStates(unittest.TestCase):
    PREFIXES = ("function metricStates(",)
    EXPORTS = ("metricStates", "fixIsStale")

    def test_each_tile_follows_its_own_row_under_node(self):
        if NODE is None:
            self.skipTest("node is unavailable")
        out = run_gate_node(r"""
const ready = Object.assign({}, fix, {qual: 4, sats: 20, hdop: 0.6, correction_age_sec: 1.2});
const tiles = (reasons, patch, extra) => {
  const live = reasonLive(reasons, Object.assign({current_fix: Object.assign({}, ready, patch || {})}, extra || {}));
  return g.metricStates(g.gateRows(live), g.fixIsStale(live.current_fix));
};
// The field report: DGPS, both sigmas too large, 40 satellites and no correction age.
const dgps = {qual: 2, sats: 40, hdop: 0.6, correction_age_sec: null, receiver_accuracy: {horizontal_sigma_m: 5.7, altitude_sigma_m: 9.1}};
eq("field report", tiles(["rtk_fixed_required", "gst_horizontal_error_too_high", "gst_vertical_error_too_high"], dgps),
  {horizontal: "fail", vertical: "fail", satellites: "ok", corrections: "unknown"});
eq("fix type alone fails no tile", tiles(["rtk_fixed_required"], dgps), {horizontal: "ok", vertical: "ok", satellites: "ok", corrections: "unknown"});
eq("ready", tiles([]), {horizontal: "ok", vertical: "ok", satellites: "ok", corrections: "ok"});
eq("horizontal sigma", tiles(["gst_horizontal_error_too_high"]), {horizontal: "fail", vertical: "ok", satellites: "ok", corrections: "ok"});
eq("horizontal spread", tiles(["horizontal_spread_too_high"]), {horizontal: "fail", vertical: "ok", satellites: "ok", corrections: "ok"});
eq("vertical sigma", tiles(["gst_vertical_error_too_high"]), {horizontal: "ok", vertical: "fail", satellites: "ok", corrections: "ok"});
for (const code of ["too_few_satellites", "hdop_too_high"])
  eq(code, tiles([code]), {horizontal: "ok", vertical: "ok", satellites: "fail", corrections: "ok"});
eq("corrections too old", tiles(["corrections_too_old"]), {horizontal: "ok", vertical: "ok", satellites: "ok", corrections: "fail"});
eq("corrections too old without an age", tiles(["corrections_too_old"], {correction_age_sec: null}).corrections, "fail");
eq("unknown age", tiles([], {correction_age_sec: null}).corrections, "unknown");
for (const code of ["gst_required", "no_position", "altitude_required", "future_code"])
  eq(code + " fails no tile", tiles([code]), {horizontal: "ok", vertical: "ok", satellites: "ok", corrections: "ok"});
eq("stale follows the rows", tiles([], {stale: true}), {horizontal: "fail", vertical: "fail", satellites: "fail", corrections: "fail"});
// A stale position keeps the device reasons on the rows: staleness wins before the axis split.
const allFail = {horizontal: "fail", vertical: "fail", satellites: "fail", corrections: "fail"};
eq("stale with a horizontal reason", tiles(["gst_horizontal_error_too_high"], {stale: true}), allFail);
eq("stale with a vertical reason", tiles(["gst_vertical_error_too_high"], {stale: true}), allFail);
eq("stale with a spread reason", tiles(["horizontal_spread_too_high"], {stale: true}), allFail);
eq("stale with both reasons", tiles(["gst_horizontal_error_too_high", "gst_vertical_error_too_high"], {stale: true}), allFail);
eq("stale by age with a horizontal reason", tiles(["gst_horizontal_error_too_high"], {fix_age_ms: 9000, stale: true}), allFail);
const axisRows = [{id: "accuracy", state: "fail", reasons: ["gst_horizontal_error_too_high"]}, {id: "satellites", state: "fail", reasons: []}, {id: "corrections", state: "fail", reasons: []}];
eq("stale flag splits no axis", g.metricStates(axisRows, true), allFail);
eq("fresh rows split the axes", g.metricStates(axisRows, false), {horizontal: "fail", vertical: "ok", satellites: "fail", corrections: "fail"});
eq("no verdict", g.metricStates(g.gateRows(withVerdict(null, {current_fix: ready}))), {horizontal: "ok", vertical: "ok", satellites: "ok", corrections: "ok"});
// An accuracy row that fails without an H- or V-specific reason marks both tiles.
eq("unspecific accuracy failure", g.metricStates([{id: "accuracy", state: "fail", reasons: []}, {id: "satellites", state: "ok", reasons: []}, {id: "corrections", state: "ok", reasons: []}]),
  {horizontal: "fail", vertical: "fail", satellites: "ok", corrections: "ok"});
eq("missing rows are unknown", g.metricStates([]), {horizontal: "unknown", vertical: "unknown", satellites: "unknown", corrections: "unknown"});
""", extra_prefixes=self.PREFIXES, extra_exports=self.EXPORTS)
        self.assertEqual(out["failures"], [])

    def test_tiles_carry_id_and_state_from_the_rows(self):
        mount = APP_JS[APP_JS.index("function mountStatus("):]
        mount = mount[:mount.index("\n")]
        for metric in ("horizontal", "vertical", "satellites", "corrections"):
            self.assertIn('["%s",' % metric, mount)
        self.assertIn('"data-metric":id', mount)
        core = section("function renderStatusCore(", "function measureActionModel(")
        # The tile state comes from the same rows as the checklist: no second gate logic.
        self.assertIn("metricStates(rows,stale)", core)
        self.assertIn("dataset.state=", core)
        states = APP_JS[APP_JS.index("function metricStates("):]
        states = states[:states.index("\n")]
        self.assertNotIn("current_quality", states)
        self.assertNotIn("reasons:", states)

    def test_failed_and_unknown_tiles_are_named_in_words(self):
        core = section("function renderStatusCore(", "function measureActionModel(")
        self.assertIn('tr("ui.diagnostic_fail","Failed")', core)
        self.assertIn('tr("ui.why_state_unknown","Unknown")', core)
        mount = APP_JS[APP_JS.index("function mountStatus("):]
        mount = mount[:mount.index("\n")]
        self.assertIn('h("span",{class:"visually-hidden"})', mount)

    def test_tile_colours_come_from_tokens(self):
        self.assertIn('.readiness-hero .metric[data-state="fail"] b{color:var(--critical)}', APP_CSS)
        self.assertIn('.readiness-hero .metric[data-state="unknown"] b{color:var(--warn)}', APP_CSS)
        # Amber on the hero's tinted tile misses 4.5:1 in the light theme (4.37 on red, 4.50 on green): plain surface.
        self.assertIn('.readiness-hero .metric[data-state="unknown"]{background:var(--surface)}', APP_CSS)
        self.assertIn('.readiness-hero.blocked .metric[data-state="ok"] b{color:var(--ink)}', APP_CSS)
        # A lost link greys the hero as before, whatever the last tile states were.
        self.assertIn('.readiness-hero.stale-critical .metric[data-state] b{color:inherit}', APP_CSS)
        self.assertLess(APP_CSS.index('.readiness-hero .metric[data-state="fail"] b'),
                        APP_CSS.index('.readiness-hero.stale-critical .metric[data-state] b'))
        # The hero itself keeps its colours.
        self.assertIn(".readiness-hero.blocked{color:var(--critical)}", APP_CSS)
        self.assertIn(".readiness-hero.ready{color:var(--ok)}", APP_CSS)


if __name__ == "__main__":
    unittest.main()
