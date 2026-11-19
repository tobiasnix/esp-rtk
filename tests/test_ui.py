# SPDX-License-Identifier: AGPL-3.0-only
"""Web interface: delivery and access protection."""
import gzip
import inspect
import json
import os
import re
import subprocess
import tempfile
import unittest
from unittest import mock
import web_routing

from support import cfg, ui, web
import assets
import tracking

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(ROOT, "base.css"), encoding="utf-8") as source:
    BASE_CSS = source.read()
with open(os.path.join(ROOT, "pages.js"), encoding="utf-8") as source:
    PAGES_JS = source.read()
with open(os.path.join(ROOT, "pages.css"), encoding="utf-8") as source:
    PAGES_CSS = source.read()
with open(os.path.join(ROOT, "app.js"), encoding="utf-8") as source:
    APP_JS = source.read()
with open(os.path.join(ROOT, "app.css"), encoding="utf-8") as source:
    APP_CSS = source.read()
with open(os.path.join(ROOT, "i18n.js"), encoding="utf-8") as source:
    I18N_JS = source.read()
SETUP_SOURCE = web.SETUP_HTML + PAGES_JS + BASE_CSS + PAGES_CSS
NODE = next((name for name in ("node", "nodejs")
             if subprocess.run(["sh", "-c", "command -v %s" % name],
                               capture_output=True).returncode == 0), None)


GATE_DECLARATION_PREFIXES = (
    "const tr=", "const fmt=", "const fixLabel=", "const quantity=", "let staleSinceSeq=",
    "const RTCM_EPOCH_WARN_SEC=", "const GATE_ROW_IDS=", "const GATE_REASON_ROW=",
    "function displayAccuracy(", "function fixIsUsable(", "function fixIsStale(",
    "function staleText(", "function requiredFixName(", "function correctionLinkState(",
    "function gstStateText(", "function gateReasonText(", "function gateRows(",
    "function gateHeadline(",
    # gateRows reads the corrections cause (#30), so its helpers are part of every gate run.
    "const HELP_ROUTES=", "function wifiErrorText(", "function ntripErrorText(", "function correctionsCause(")
# The 13 reasons of tracking.quality_check and the checklist row each one belongs to.
GATE_CODE_ROWS = {
    "rtk_fixed_required": "fix", "required_fix_not_met": "fix", "not_all_samples_rtk_fixed": "fix",
    "corrections_too_old": "corrections",
    "gst_horizontal_error_too_high": "accuracy", "gst_vertical_error_too_high": "accuracy",
    "horizontal_spread_too_high": "accuracy",
    "too_few_satellites": "satellites", "hdop_too_high": "satellites",
    "gst_required": "gst", "not_all_samples_have_gst": "gst",
    "no_position": "position", "altitude_required": "position"}
# Shared Node prelude: loads the dictionaries, builds the gate helpers from app.js and offers eq() to
# collect failed expectations, so one run reports every mismatch.
GATE_NODE_PRELUDE = r"""
globalThis.RtkI18n = require(process.cwd() + "/i18n.js");
const g = new Function('"use strict";const localText=value=>value;' + process.argv[1] +
  "\nreturn {gateRows, gateHeadline, gateReasonText, gstStateText, correctionLinkState, displayAccuracy, GATE_ROW_IDS, GATE_REASON_ROW" +
  (process.argv[2] ? ", " + process.argv[2] : "") + "};")();
const dict = RtkI18n.dictionaries;
const failures = [];
const eq = (name, actual, expected) => {
  if (JSON.stringify(actual) !== JSON.stringify(expected))
    failures.push(name + ": got " + JSON.stringify(actual) + " expected " + JSON.stringify(expected));
};
const fill = (template, params) => Object.keys(params || {}).reduce(
  (text, key) => text.split("{" + key + "}").join(params[key]), template);
const limits = {fix_quality: "RTK_FIXED", min_satellites: 10, max_hdop: 1.5, max_correction_age_sec: 5,
  max_gst_horizontal_sigma_m: 0.035, max_gst_vertical_sigma_m: 0.04, max_horizontal_spread_m: 0.03};
const fix = {qual: 1, sats: 7, hdop: 2.25, lat: 50, lon: 8, alt: 100, correction_age_sec: 12, fix_seq: 3,
  stale: false, receiver_accuracy: {horizontal_sigma_m: 0.0512, altitude_sigma_m: 0.0834}};
const baseLive = {current_fix: fix, gnss_accuracy: {state: "requested", attempts: 3},
  rtcm: {epoch_age_sec: 1, arrival_age_sec: 0.2, stale: false}, network: {ntrip_state: "streaming"}};
const withVerdict = (verdict, extra) => Object.assign({}, baseLive, {current_quality: verdict}, extra || {});
const reasonLive = (reasons, extra) => withVerdict({accepted: reasons.length === 0, reasons, limits}, extra);
"""


def gate_declarations(extra_prefixes=()):
    """The single-line gate helpers of app.js, joined for a Node run (each prefix must be unique)."""
    found = []
    for prefix in GATE_DECLARATION_PREFIXES + tuple(p for p in extra_prefixes if p not in GATE_DECLARATION_PREFIXES):
        lines = [line for line in APP_JS.split("\n") if line.startswith(prefix)]
        if len(lines) != 1:
            raise AssertionError("%s must be one single-line declaration" % prefix)
        found.append(lines[0])
    return "\n".join(found)


def run_gate_node(body, extra_prefixes=(), extra_exports=()):
    """Run a Node body after the gate prelude; the body assigns to `out` and uses eq() for expectations.

    extra_prefixes add single-line app.js declarations, extra_exports add their names to the object `g`."""
    script = GATE_NODE_PRELUDE + "const out = {};\n" + body + \
        "\nout.failures = failures;\nconsole.log(JSON.stringify(out));\n"
    run = subprocess.run([NODE, "-e", script, gate_declarations(extra_prefixes), ", ".join(extra_exports)], cwd=ROOT,
                         capture_output=True, text=True, timeout=20)
    if run.returncode != 0:
        raise AssertionError(run.stderr)
    return json.loads(run.stdout)


FRESHNESS_DECLARATION_PREFIXES = (
    "const tr=", "const fmt=", "let staleSinceSeq=", "const LIVE_DELAYED_MS=", "function noteLiveAnswer(",
    "function noteLiveAttempt(", "function liveAgeMs(", "function liveLinkState(", "function liveView(",
    "const LINK_SYMBOL=", "function livePollIntervalMs(", "function pillLink(", "function freshnessText(",
    "function liveLostText(", "function heroAnnouncementKey(", "function announcementDecision(",
    "function announcementQuiet(", "let announcedKey=", "function flushAnnouncement(",
    "function quietAnnouncements(", "const MEASURE_ROUTE=")
FRESHNESS_NODE_PRELUDE = r"""
"use strict";
globalThis.RtkI18n = require(process.cwd() + "/i18n.js");
const localText = value => value;
const dict = RtkI18n.dictionaries;
const failures = [];
const eq = (name, actual, expected) => {
  if (JSON.stringify(actual) !== JSON.stringify(expected))
    failures.push(name + ": got " + JSON.stringify(actual) + " expected " + JSON.stringify(expected));
};
let route = "measure";
const currentRoute = () => ({path: route});
const announcer = {textContent: ""};
const $ = id => id === "rtk-announcer" ? announcer : null;
"""


def freshness_declarations(extra_prefixes=()):
    """The single-line freshness helpers of app.js, joined for a Node run (each prefix must be unique)."""
    found = []
    for prefix in FRESHNESS_DECLARATION_PREFIXES + tuple(extra_prefixes):
        lines = [line for line in APP_JS.split("\n") if line.startswith(prefix)]
        if len(lines) != 1:
            raise AssertionError("%s must be one single-line declaration" % prefix)
        found.append(lines[0])
    return "\n".join(found)


def run_freshness_node(body, extra_prefixes=(), stubs=""):
    """Run a Node body after the freshness helpers; stubs come before the declarations, the body uses eq()."""
    script = FRESHNESS_NODE_PRELUDE + stubs + "\n" + freshness_declarations(extra_prefixes) + \
        "\nconst out = {};\n" + body + "\nout.failures = failures;\nconsole.log(JSON.stringify(out));\n"
    run = subprocess.run([NODE, "-e", script], cwd=ROOT, capture_output=True, text=True, timeout=20)
    if run.returncode != 0:
        raise AssertionError(run.stderr)
    return json.loads(run.stdout)


FEEDBACK_DECLARATION_PREFIXES = (
    "const tr=", "const V2_DE=", "function localText(", "const FEEDBACK_RANK=", "function feedbackUpsert(",
    "function feedbackVisible(", "function feedbackNextExpiry(")
FEEDBACK_NODE_PRELUDE = r"""
"use strict";
globalThis.RtkI18n = require(process.cwd() + "/i18n.js");
const dict = RtkI18n.dictionaries;
const failures = [];
const eq = (name, actual, expected) => {
  if (JSON.stringify(actual) !== JSON.stringify(expected))
    failures.push(name + ": got " + JSON.stringify(actual) + " expected " + JSON.stringify(expected));
};
"""


def feedback_declarations(extra_prefixes=()):
    """The single-line feedback helpers of app.js, joined for a Node run (each prefix must be unique)."""
    found = []
    for prefix in FEEDBACK_DECLARATION_PREFIXES + tuple(extra_prefixes):
        lines = [line for line in APP_JS.split("\n") if line.startswith(prefix)]
        if len(lines) != 1:
            raise AssertionError("%s must be one single-line declaration" % prefix)
        found.append(lines[0])
    return "\n".join(found)


def run_feedback_node(body, extra_prefixes=(), stubs=""):
    """Run an (async) Node body after the feedback helpers; stubs come before the declarations, the body uses eq()."""
    script = FEEDBACK_NODE_PRELUDE + stubs + "\n" + feedback_declarations(extra_prefixes) + \
        "\nconst out = {};\n(async () => {\n" + body + \
        "\n})().then(() => { out.failures = failures; console.log(JSON.stringify(out)); }," \
        " error => { console.error(error); process.exitCode = 1; });\n"
    run = subprocess.run([NODE, "-e", script], cwd=ROOT, capture_output=True, text=True, timeout=20)
    if run.returncode != 0:
        raise AssertionError(run.stderr)
    return json.loads(run.stdout)


class TestPage(unittest.TestCase):

    def test_archive_cards_stack_info_above_actions(self):
        line = next(l for l in APP_JS.split("\n") if l.startswith("function archiveLibrary("))
        self.assertIn('h("article",{class:"archive-card","aria-label":item.name}', line)
        self.assertIn('class:"archive-info"', line)
        self.assertIn('class:"inline-actions archive-actions"', line)
        self.assertNotIn("project-card", line)
        self.assertIn("textRaw:item.name", line)
        block = APP_CSS[APP_CSS.index("/* M2 #43/#44"):]
        self.assertIn(".archive-actions .btn{flex:1 1 auto;min-width:var(--tap)}", block)
        self.assertIn(':root[data-theme="high-contrast"] .archive-card{border-width:2px}', block)

    def test_empty_project_card_keeps_its_width(self):
        block = APP_CSS[APP_CSS.index("/* M2 #43/#44"):]
        self.assertIn(".project-card-wrap{grid-template-columns:minmax(0,1fr) fit-content(40%)}", block)

    def test_project_views_wrap_actions_and_long_names(self):
        block = APP_CSS[APP_CSS.index("/* M2 #43/#44"):]
        self.assertIn(".text-actions{flex-wrap:wrap}", block)
        self.assertIn(".project-card>span:first-child{min-width:0}", block)
        wrapped = set()
        for selectors in re.findall(r"([^{}]+)\{overflow-wrap:anywhere\}", block):
            wrapped.update(part.strip() for part in selectors.split(","))
        self.assertEqual(wrapped, {
            ".project-title b", ".project-card small", ".archive-info b", ".export-card h2", ".map-panel h2",
            ".line-row b", ".point-row b", ".line-quality h3", ".point-detail-title h3"})

    def test_asset_version_follows_the_firmware_version(self):
        # A release bump must move the cache-buster too, or browsers keep stale assets.
        self.assertEqual(cfg.CONFIG["version"].rsplit(" V", 1)[1], assets.ASSET_VERSION)

    def test_field_diagnostics_uses_the_shared_notices(self):
        section = APP_JS[APP_JS.index("async function mountFieldDiagnostics("):APP_JS.index("async function mountDiagnostics(")]
        self.assertEqual(APP_JS.count("toast("), 0)
        self.assertIn('showWarning("Stop field diagnostics before downloading.")', section)
        self.assertIn('showSuccess("Field marker saved")', section)
        self.assertIn("showError(error)", section)
        # handlers must not hand a promise to iconButton (it would re-enable the button)
        self.assertNotIn("()=>control(", section)

    def test_shell_has_a_sign_out_button(self):
        # #24-UI: sign-out is a header button (E10), after the live pill, inside the app header.
        header = re.search(r'<header class="app-header">(.*?)</header>', ui.APP_V2, re.S).group(1)
        self.assertIn('id="sign-out"', header)
        button = re.search(r'<button id="sign-out"[^>]*>.*?</button>', header, re.S).group(0)
        self.assertIn('class="header-action"', button)
        self.assertIn('type="button"', button)
        self.assertIn('class="header-action-label">Sign out</span>', button)
        self.assertLess(header.index('id="live-state"'), header.index('id="sign-out"'))
        self.assertNotIn('id="sign-out"', ui.APP_V2_LOGIN)

    def test_current_shell_is_external_and_responsive(self):
        self.assertIn('id="app-main"', ui.APP_V2)
        self.assertNotIn('id="view-back"', ui.APP_V2)
        self.assertIn('id="live-state" class="live-state" type="button"', ui.APP_V2)
        # The announcer is the only live region for state changes; the pill is not one (#21b).
        self.assertIn('id="rtk-announcer" class="visually-hidden" role="status" aria-live="polite" aria-atomic="true"', ui.APP_V2)
        self.assertLess(ui.APP_V2.index('id="rtk-announcer"'), ui.APP_V2.index("<main"))
        pill_tag = re.search(r'<button id="live-state"[^>]*>', ui.APP_V2).group(0)
        self.assertNotIn("aria-live", pill_tag)
        self.assertIn('<link rel="icon" href="data:image/svg+xml,', ui.APP_V2)
        self.assertIn('<link rel="icon" href="data:image/svg+xml,', ui.APP_V2_LOGIN)
        self.assertIn('/app.css?v=%s' % assets.ASSET_VERSION, ui.APP_V2)
        self.assertIn('/app.js?v=%s' % assets.ASSET_VERSION, ui.APP_V2)
        self.assertIn('.view-heading{display:flex;width:0;min-width:0;flex:1;', APP_CSS)
        self.assertIn('.module-console{overflow:auto;width:100%;min-width:0;max-width:100%', APP_CSS)
        self.assertIn('class:"device-metrics diagnostics-summary"', APP_JS)
        self.assertNotIn('JSON.stringify(data,null,2)', APP_JS)
        self.assertIn('@media(min-width:900px)', APP_CSS)
        for contract in ('const store=', 'const api=', 'async function renderRoute(retry=0)',
                         'function mountStatus()', 'async function mountProjects',
                         'async function mountDevice'):
            self.assertIn(contract, APP_JS)
        self.assertNotIn('main.innerHTML', APP_JS)
        # #24-UI: access and label are building blocks of the connections page; every view has one mount.
        for screen in ('mountRtcm', 'mountConnections', 'mountLog', 'mountModule',
                       'mountConfiguration', 'mountDiagnostics', 'mountDisplay'):
            self.assertIn('function %s' % screen, APP_JS)
        for gone in ('function mountAccess', 'function mountLabel', 'const screens={"device/rtcm"'):
            self.assertNotIn(gone, APP_JS)
        self.assertIn('const VIEW_MOUNTS={', APP_JS)
        self.assertIn('/api/log?limit=200&cursor=', APP_JS)
        self.assertIn('/ui-v2', web_routing.ROUTES)

    def test_v2_login_uses_the_same_design_and_session_contract(self):
        self.assertIn('data-page="app-v2-login"', ui.APP_V2_LOGIN)
        self.assertIn('id="v2-login-form"', ui.APP_V2_LOGIN)
        self.assertIn('device_code:code,next:"/"', APP_JS)
        self.assertIn('.v2-login{', APP_CSS)
        for page in (ui.APP_V2, ui.APP_V2_LOGIN):
            self.assertNotIn('<style', page)
            self.assertNotRegex(page, r'<script(?![^>]+\bsrc=)')

    def test_v2_configuration_exposes_ntrip_switch_and_fallback(self):
        for field in ("ntrip_enabled", "ntrip_fallback_host",
                      "ntrip_fallback_port", "ntrip_fallback_mount",
                      "ntrip_fallback_user", "ntrip_fallback_pass"):
            self.assertIn(field, APP_JS)
        # The form converts through configChanges (select value "1"/"0", ports as numbers).
        self.assertIn('configChanges(configFormValues(form),form.configBaseline)', APP_JS)
        self.assertIn('field("ntrip_fallback_port","Port","number")', APP_JS)
        self.assertIn("Fallback caster (optional)", APP_JS)
        self.assertIn("No password is saved.", APP_JS)

    def test_v2_exposes_route_recording_and_wifi_errors(self):
        for value in ('recording_mode', 'Route — preserve every valid position',
                      'values.recording_mode==="route"', 'live.network',
                      'last_wifi_error', 'Wi-Fi error:'):
            self.assertIn(value, APP_JS)

    def test_start_line_response_does_not_serialize_flash_sequences(self):
        with open(os.path.join(ROOT, "web.py"), encoding="utf-8") as source:
            web_source = source.read()
        self.assertIn('line = tracker.start_line(', web_source)
        self.assertIn('result = {"id": line["id"], "name": line["name"]',
                      web_source)
        self.assertNotIn('result = tracker.start_line(', web_source)

    def test_api_request_separates_sign_in_from_refusal(self):
        # 401: the session is gone, sign in again. 403: the session lives, the
        # CSRF token or origin was refused; drop the token, report, no redirect.
        if NODE is None:
            self.skipTest("node is unavailable")
        source = APP_JS[APP_JS.index("let csrfToken="):APP_JS.index("const apiJsonBase=")]
        script = r"""
const replaced = [], answers = [];
const location = {hash: "#projects", replace: url => replaced.push(url)};
const tr = (key, fallback) => "<" + key + ">";
const fetch = async () => {
  const [status, payload] = answers.shift();
  return {status, ok: status < 400, body: null, json: async () => payload,
          text: async () => "", blob: async () => null, arrayBuffer: async () => null};
};
// No guided restart runs (#23c): a 401 leads to the sign-in page. No unsaved configuration form asks first (#24).
const restartHoldActive = () => false;
const main = {querySelector: () => null};
const probe = new Function("fetch", "location", "tr", "restartHoldActive", "main", process.argv[1] +
  "\nreturn {api, token: () => csrfToken, setToken: v => { csrfToken = v; }};")(
  fetch, location, tr, restartHoldActive, main);
const refusal = request => request.then(() => null, error => ({name: error.name,
                                                               message: error.message}));
(async () => {
  probe.setToken("old");
  answers.push([403, {error: "csrf_rejected", message: "x"}]);
  const forbidden = await refusal(probe.api.request("/api/reboot", {method: "POST"}));
  const afterForbidden = {token: probe.token(), replaced: replaced.slice()};
  answers.push([200, {csrf_token: "fresh"}]);
  const fresh = await probe.api.csrf();
  answers.push([401, {error: "admin_session_required", message: "x"}]);
  const signIn = await refusal(probe.api.request("/api/reboot", {method: "POST"}));
  console.log(JSON.stringify({forbidden, afterForbidden, fresh, signIn, replaced}));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        run = subprocess.run([NODE, "-e", script, source],
                             capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        result = json.loads(run.stdout)
        self.assertEqual(result["forbidden"], {"name": "ForbiddenError",
                                               "message": "<ui.action_refused_reload>"})
        self.assertEqual(result["afterForbidden"], {"token": None, "replaced": []})
        self.assertEqual(result["fresh"], "fresh")
        self.assertEqual(result["signIn"]["name"], "AbortError")
        self.assertEqual(result["replaced"], ["/?return=%2Fui-v2%23projects"])

    def test_api_errors_carry_status_code_and_reasons(self):
        # E18 (shared with #23c): a refused request names its status, error code and reason codes.
        if NODE is None:
            self.skipTest("node is unavailable")
        source = APP_JS[APP_JS.index("let csrfToken="):APP_JS.index("const apiJsonBase=")]
        script = r"""
const answers = [];
const location = {hash: "#status", replace: () => {}};
const tr = (key, fallback) => fallback;
const fetch = async () => {
  const [status, payload] = answers.shift(), body = typeof payload === "string" ? payload : JSON.stringify(payload);
  return {status, ok: status < 400, body: null, json: async () => JSON.parse(body),
          text: async () => body, blob: async () => null, arrayBuffer: async () => null};
};
const probe = new Function("fetch", "location", "tr", process.argv[1] + "\nreturn {api};")(fetch, location, tr);
const shape = error => ({status: error.status, message: error.message,
  code: error.code === undefined ? "<undefined>" : error.code,
  reasons: error.reasons === undefined ? "<undefined>" : error.reasons});
(async () => {
  const out = [];
  for (const answer of [
      [400, {error: "invalid_tracking_request", message: "x", reasons: ["no_position", 3]}],
      [409, {error: "undo_candidate_changed", message: "y"}],
      [500, "boom"]]) {
    answers.push(answer);
    out.push(await probe.api.request("/api/tracking", {method: "POST"}).then(() => null, shape));
  }
  console.log(JSON.stringify(out));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        run = subprocess.run([NODE, "-e", script, source], capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout), [
            {"status": 400, "message": "x", "code": "invalid_tracking_request", "reasons": ["no_position"]},
            {"status": 409, "message": "y", "code": "undo_candidate_changed", "reasons": "<undefined>"},
            {"status": 500, "message": "boom", "code": "<undefined>", "reasons": "<undefined>"}])
        self.assertIn("status/code/reasons let callers tell a refusal from a lost connection (#23c, #21c)", APP_JS)

    def rejection_bodies(self):
        """Real 400 answers of both rejection sites: Tracker._measurement and MeasurementSession._collect_samples."""
        import test_http
        import test_measurement
        from test_tracking import FIX
        helper = test_http.TestMeasurementEndpoints("test_post_real_answers_a_real_action")
        self.addCleanup(helper.doCleanups)
        tracker = helper.real_tracker()
        tracker.start_line("L1", 0, None, dict(FIX), "survey")
        bodies = {}
        with mock.patch.object(web.measurement_session, "collect",
                               mock.AsyncMock(return_value=dict(FIX, lat_e8=None, lon_e8=None))):
            status, bodies["tracker"] = helper.post_real(tracker, {"action": "add_vertex"})
        self.assertIn(b"400 Bad Request", status)
        session = test_measurement.MeasurementSession(test_measurement.FakeReceiver(last_fix=dict(
            test_measurement.GGA, utc="1", lat_e8=None, lon_e8=None,
            receiver_accuracy=dict(test_measurement.GST))))
        with mock.patch.object(web.measurement_session, "collect", session.collect), \
                test_measurement.fake_clock(100_000):
            status, bodies["session"] = helper.post_real(tracker, {"action": "add_vertex", "samples": 2})
        self.assertIn(b"400 Bad Request", status)
        return bodies

    def test_rejection_reasons_travel_from_http_to_the_strip_under_node(self):
        # E6 contract: the reason codes of a real HTTP answer reach the message, whatever its text says.
        if NODE is None:
            self.skipTest("node is unavailable")
        bodies = self.rejection_bodies()
        for name, body in bodies.items():
            self.assertEqual(body["error"], "invalid_tracking_request", name)
            self.assertIn("no_position", body["reasons"], name)
        self.assertTrue(bodies["tracker"]["message"].startswith("Measurement quality rejected: "))
        self.assertTrue(bodies["session"]["message"].startswith("Not enough valid GNSS samples were received: "))
        api_source = APP_JS[APP_JS.index("let csrfToken="):APP_JS.index("const apiJsonBase=")]
        stubs = r"""
const answers = [];
const location = {hash: "#status", replace: () => {}};
const fetch = async () => {
  const [status, payload] = answers.shift(), body = JSON.stringify(payload);
  return {status, ok: status < 400, body: null, json: async () => JSON.parse(body),
          text: async () => body, blob: async () => null, arrayBuffer: async () => null};
};
""" + api_source
        out = run_feedback_node(r"""
const bodies = %s;
const refused = async payload => { answers.push([400, payload]);
  return api.request("/api/tracking", {method: "POST"}).then(() => null, error => error); };
for (const [name, body] of Object.entries(bodies)) {
  const error = await refused(body), info = rejectionInfo(error);
  out[name] = {codes: info.codes, text: info.text, field: fieldError(error),
    changed: rejectionInfo(await refused(Object.assign({}, body, {message: "Changed text."}))).codes};
  info.codes.forEach((code, index) => eq(name + " reason " + code, info.reasons[index], dict.en["quality." + code]));
  eq(name + " reason count", info.reasons.length, info.codes.length);
}
RtkI18n.setLanguage("de");
out.de = rejectionInfo(await refused(bodies.tracker)).text;
RtkI18n.setLanguage("en");
const plain = message => rejectionInfo(new Error(message));
out.fallback = plain("Measurement quality rejected: rtk_fixed_required, too_few_satellites.").codes;
out.stale = plain("GNSS position is stale (no GGA for 7 s).");
out.other = plain("Something else.");
out.expected = {de: RtkI18n.v2de["Measurement could not be completed."], stale: dict.en["ui.measurement_stopped_stale"]};
""" % json.dumps(bodies), extra_prefixes=("function rejectionInfo(", "function fieldError("), stubs=stubs)
        self.assertEqual(out["failures"], [])
        english, german = "Measurement could not be completed.", out["expected"]["de"]
        for name, body in bodies.items():
            self.assertEqual(out[name]["codes"], body["reasons"], name)
            self.assertTrue(out[name]["codes"], name)
            for code in out[name]["codes"]:
                self.assertIn(code, GATE_CODE_ROWS, name)
            self.assertEqual(out[name]["text"], english, name)
            self.assertEqual(out[name]["changed"], body["reasons"], name)
            self.assertTrue(out[name]["field"].startswith(english + " "), name)
        self.assertEqual(out["de"], german)
        self.assertEqual(out["fallback"], ["rtk_fixed_required", "too_few_satellites"])
        self.assertEqual(out["stale"]["codes"], [])
        self.assertEqual(out["stale"]["reasons"], [])
        self.assertEqual(out["stale"]["text"], out["expected"]["stale"].replace("{seconds}", "7"))
        self.assertEqual(out["other"], {"text": "Something else.", "codes": [], "reasons": []})

    def test_feedback_model_under_node(self):
        if NODE is None:
            self.skipTest("node is unavailable")
        out = run_feedback_node(r"""
const keys = (items, slot, now) => feedbackVisible(items, slot, now).map(item => item.key);
const a = feedbackUpsert([], {key: "action", kind: "ok", text: "Saved", ttl: 4000}, 1000);
eq("first entry", a.map(item => [item.key, item.kind, item.text, item.at, item.expires]), [["action", "ok", "Saved", 1000, 5000]]);
eq("defaults", [a[0].reasons, a[0].actions, a[0].quietOnMeasure], [[], [], false]);
const b = feedbackUpsert(a, {key: "action", kind: "critical", text: "Failed"}, 2000);
eq("same key replaces", b.map(item => [item.key, item.kind, item.expires]), [["action", "critical", null]]);
eq("pure", a.length, 1);
const low = feedbackUpsert(feedbackUpsert([], {kind: "warn", text: "Low"}, 1), {kind: "warn", text: "Low"}, 2);
eq("text is the default key", low.map(item => [item.key, item.at]), [["Low", 2]]);
eq("default kind", feedbackUpsert([], {text: "Plain"}, 1)[0].kind, "info");
let mix = [];
for (const [spec, at] of [[{key: "a", kind: "ok", text: "A"}, 1], [{key: "capacity", kind: "warn", text: "W", quietOnMeasure: true}, 2],
    [{key: "action", kind: "critical", text: "C"}, 3], [{key: "b", kind: "running", text: "R"}, 4], [{key: "c", kind: "info", text: "I"}, 5]])
  mix = feedbackUpsert(mix, spec, at);
eq("dock by rank", keys(mix, "dock", 10), ["action", "capacity", "b"]);
eq("strip by rank, quiet left out", keys(mix, "capture", 10), ["action", "b"]);
// Guaranteed place (r3-3): the capture entry comes first in both views, the rest follow by rank.
let base = feedbackUpsert([], {key: "capacity", kind: "critical", text: "Cap"}, 1);
base = feedbackUpsert(base, {key: "action", kind: "critical", text: "Err"}, 2);
const running = feedbackUpsert(base, {key: "capture", kind: "running", text: "Run"}, 3);
eq("running strip", keys(running, "capture", 10), ["capture", "action"]);
eq("running dock", keys(running, "dock", 10), ["capture", "action", "capacity"]);
const saved = feedbackUpsert(base, {key: "capture", kind: "ok", text: "Saved", ttl: 6000}, 3);
eq("capture expiry", saved.find(item => item.key === "capture").expires, 6003);
eq("saved strip", keys(saved, "capture", 10), ["capture", "action"]);
eq("saved strip expired", keys(saved, "capture", 6003), ["action", "capacity"]);
const info = feedbackUpsert(base, {key: "capture", kind: "info", text: "I", quietOnMeasure: false}, 3);
eq("capture first despite its rank", keys(info, "capture", 10), ["capture", "action"]);
const quiet = feedbackUpsert(base, {key: "capture", kind: "ok", text: "Q", quietOnMeasure: true}, 3);
eq("quiet capture not in the strip", keys(quiet, "capture", 10), ["action", "capacity"]);
eq("quiet capture in the dock", keys(quiet, "dock", 10), ["capture", "action", "capacity"]);
const expiring = feedbackUpsert([], {key: "x", kind: "ok", text: "X", ttl: 8}, 1);
eq("expired at its time", keys(expiring, "dock", 9), []);
eq("visible before", keys(expiring, "dock", 8), ["x"]);
const tie = feedbackUpsert(feedbackUpsert([], {key: "p", kind: "warn", text: "P"}, 5), {key: "q", kind: "warn", text: "Q"}, 5);
eq("tie: later position first", keys(tie, "dock", 6), ["q", "p"]);
const timed = feedbackUpsert(feedbackUpsert(feedbackUpsert([], {key: "l", kind: "ok", text: "L", ttl: 4000}, 1000),
  {key: "s", kind: "ok", text: "S", ttl: 100}, 1000), {key: "n", kind: "warn", text: "N"}, 1000);
eq("next expiry", [feedbackNextExpiry(timed, 1000), feedbackNextExpiry(timed, 1100), feedbackNextExpiry(timed, 5000),
  feedbackNextExpiry([], 1)], [1100, 5000, null, null]);
""")
        self.assertEqual(out["failures"], [])

    def test_feedback_hook_lives_only_in_the_harness(self):
        # r3-5: the browser test access exists only in the copy of app.js the --feedback-only fixture serves.
        source_dir = os.path.join(ROOT, "src")
        for name in sorted(os.listdir(source_dir)):
            if name.endswith(".js"):
                with open(os.path.join(source_dir, name), encoding="utf-8") as source:
                    self.assertNotIn("__rtkFeedbackTest", source.read(), name)
        self.assertNotIn("__rtkFeedbackTest", APP_JS)
        with gzip.open(os.path.join(ROOT, "app.js.gz"), "rt", encoding="utf-8") as packed:
            self.assertNotIn("__rtkFeedbackTest", packed.read())
        with open(os.path.join(ROOT, "tests", "app_v2_acceptance.js"), encoding="utf-8") as source:
            harness = source.read()
        hooks = [line for line in harness.split("\n") if line.startswith("const FEEDBACK_HOOK=")]
        self.assertEqual(len(hooks), 1)
        self.assertTrue(hooks[0].startswith("const FEEDBACK_HOOK='window.__rtkFeedbackTest={"))
        marker = ".replace(/\\}\\)\\(\\);\\n$/, FEEDBACK_HOOK"
        self.assertEqual(harness.count(marker), 1)
        start = harness.index("async function fixture(")
        end = harness.index("\nasync function ", start + 1)
        self.assertLess(start, harness.index(marker))
        self.assertLess(harness.index(marker), end)
        line = next(line for line in harness.split("\n") if marker in line)
        self.assertTrue(line.strip().startswith(
            'if (path === "/app.js" && process.argv.includes("--feedback-only")) {'), line)
        self.assertTrue(APP_JS.endswith("})();\n"))

    def test_feedback_never_drives_the_pill(self):
        # E4: the pill names the data connection; action results go to the message model only.
        for prefix in ("function showError(", "function showSuccess(", "function showWarning("):
            lines = [line for line in APP_JS.split("\n") if line.startswith(prefix)]
            self.assertEqual(len(lines), 1, prefix)
            self.assertIn("showFeedback(", lines[0], prefix)
            self.assertNotIn("setConnection(", lines[0], prefix)
            self.assertNotIn("overlay.append", lines[0], prefix)
        page = ui.APP_V2
        self.assertIn('id="rtk-feedback-live" class="visually-hidden" aria-live="polite" aria-atomic="true"', page)
        self.assertRegex(page, r'id="rtk-announcer"[^>]*></p>\s*<p id="rtk-feedback-live"')
        self.assertIn('id="feedback-dock" class="feedback-dock"', page)
        self.assertRegex(page, r'</main>\s*<section id="feedback-dock" class="feedback-dock" aria-label="Messages" hidden></section>')
        self.assertIn("/* M2 #21c/#30/#45:", APP_CSS)
        block = APP_CSS[APP_CSS.index("/* M2 #21c/#30/#45:"):]
        self.assertIn(".feedback-dock{display:grid", block)
        self.assertNotIn("--feedback-dock-h", block)
        self.assertNotIn("ResizeObserver", block)
        for selector, body in re.findall(r"([^{}]*\.feedback-dock[^{}]*)\{([^{}]*)\}", block):
            self.assertNotIn("position:", body, selector)
            self.assertNotIn("z-index:", body, selector)
        self.assertNotIn("syncDockSpace", APP_JS)
        self.assertNotIn("ResizeObserver", APP_JS)

    def test_capacity_compact_under_node(self):
        # #45: the panel shrinks to title, count and meter only while nothing about capacity needs a sentence.
        if NODE is None:
            self.skipTest("node is unavailable")
        lines = APP_JS.split("\n")
        start = lines.index(next(line for line in lines if line.startswith("function capacityLevel(")))
        level = "\n".join(lines[start:lines.index("}", start) + 1])
        declarations = [level]
        for prefix in ("function storageBlocked(", "function capacityCompact("):
            found = [line for line in lines if line.startswith(prefix)]
            self.assertEqual(len(found), 1, "%s must be one single-line declaration" % prefix)
            declarations.append(found[0])
        script = r"""
const f = new Function('"use strict";const STORAGE_RESERVE_RATIO=0.2;' + process.argv[1] +
  "\nreturn {capacityCompact};")();
const capacity = (used, extra) => Object.assign({used, limit: 5000, writable: true}, extra);
const paused = {state: "paused", pause_reason: "capacity_limit"};
console.log(JSON.stringify({
  idle: f.capacityCompact({capacity: capacity(31)}, false),
  notice: f.capacityCompact({capacity: capacity(4000)}, false),
  lastBelowNotice: f.capacityCompact({capacity: capacity(3999)}, false),
  readOnly: f.capacityCompact({capacity: capacity(31, {writable: false})}, false),
  lowStorage: f.capacityCompact({capacity: capacity(31), board: {fs_total_bytes: 100, fs_free_bytes: 10}}, false),
  capacityPause: f.capacityCompact({capacity: capacity(31), active_line: paused}, false),
  otherPause: f.capacityCompact({capacity: capacity(31), active_line: {state: "paused", pause_reason: "user"}}, false),
  recording: f.capacityCompact({capacity: capacity(31), active_line: {state: "recording"}}, false),
  unavailable: f.capacityCompact({capacity: capacity(31)}, true),
  noCapacity: f.capacityCompact({}, false)}));
"""
        run = subprocess.run([NODE, "-e", script, "\n".join(declarations)], cwd=ROOT,
                             capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout), {
            "idle": True, "notice": False, "lastBelowNotice": True, "readOnly": False, "lowStorage": False,
            "capacityPause": False, "otherPause": True, "recording": True, "unavailable": False, "noCapacity": False})

    def test_capacity_warning_uses_the_feedback_model(self):
        # #45: one message mechanism; the warning is a feedback entry that stays quiet on the measuring screen.
        line = next(l for l in APP_JS.split("\n") if l.startswith("function observeCapacity("))
        body = APP_JS[APP_JS.index(line):APP_JS.index("\nfunction capacityPanel(")]
        self.assertIn("showFeedback(", body)
        self.assertIn('key:"capacity"', body)
        self.assertIn("quietOnMeasure", body)
        self.assertIn('dismissFeedback("capacity")', body)
        self.assertNotIn("overlay", body)
        self.assertNotIn("capacity-toast", body)
        self.assertNotIn("capacity-toast", APP_JS)
        update = next(l for l in APP_JS.split("\n") if l.startswith("function updateCapacityPanel("))
        body = APP_JS[APP_JS.index(update):APP_JS.index("\nasync function chooseCapacityProject(")]
        self.assertIn('panel.classList.toggle("compact",capacityCompact(live,unavailable))', body)
        block = APP_CSS[APP_CSS.index("/* M2 #21c/#30/#45:"):]
        self.assertIn(".recording-capacity.compact>:not(h2):not(.capacity-count):not(.capacity-meter){display:none}", block)
        self.assertIn(".recording-capacity.compact{gap:4px}", block)

    def test_capture_undo_target_under_node(self):
        # E7/E21: a quick undo needs the very candidate the browser still shows, inventory epoch included.
        if NODE is None:
            self.skipTest("node is unavailable")
        out = run_feedback_node(r"""
const candidate = {kind: "vertex", feature_id: "l1", line_id: "l1", label: "Water · V-3", index: 3, record_order: 41,
  request_id: "r1", inventory_epoch: "a1b2c3d4"};
const live = patch => ({undo_candidate: Object.assign({}, candidate, patch), measurement_progress: {active: false}});
eq("same candidate", captureUndoTarget(candidate, live({})), true);
eq("same candidate without a request tag", captureUndoTarget(candidate, live({request_id: null})), true);
eq("other record_order", captureUndoTarget(candidate, live({record_order: 42})), false);
eq("other index", captureUndoTarget(candidate, live({index: 4})), false);
eq("other inventory epoch only", captureUndoTarget(candidate, live({inventory_epoch: "0badf00d"})), false);
eq("other kind", captureUndoTarget(candidate, live({kind: "asset"})), false);
eq("other feature", captureUndoTarget(candidate, live({feature_id: "l2", line_id: "l2"})), false);
eq("no live candidate", captureUndoTarget(candidate, {undo_candidate: null}), false);
eq("empty live", captureUndoTarget(candidate, {}), false);
eq("measurement running", captureUndoTarget(candidate, Object.assign(live({}), {measurement_progress: {active: true}})), false);
eq("no candidate", captureUndoTarget(null, live({})), false);
eq("identity", undoIdentity(candidate), {inventory_epoch: "a1b2c3d4", kind: "vertex", feature_id: "l1", index: 3, record_order: 41});
out.keys = Object.keys(undoIdentity(candidate));
eq("identity of null", undoIdentity(null), null);
out.nullIsNull = undoIdentity(null) === null;
""", extra_prefixes=("function undoIdentity(", "function captureUndoTarget("))
        self.assertEqual(out["failures"], [])
        self.assertEqual(out["keys"], ["inventory_epoch", "kind", "feature_id", "index", "record_order"])
        self.assertTrue(out["nullIsNull"])

    def test_adopt_checked_live_under_node(self):
        # E19 (Codex branch review P2): a confirmed capture hands its checked snapshot to the store, so the Undo of
        # point B never meets the older cached point A; a store at a later revision is newer and stays.
        if NODE is None:
            self.skipTest("node is unavailable")
        stubs = r"""
let applied = [];
const store = {state: {live: null}};
const applyTrackingMutation = result => { applied.push(result.tracking_live); return result; };
"""
        out = run_feedback_node(r"""
const A = {kind: "asset", feature_id: "a41", label: "A1", record_order: 41, request_id: null, inventory_epoch: "a1b2c3d4"};
const B = {kind: "asset", feature_id: "a42", label: "Valve 21c", record_order: 42, request_id: "r1", inventory_epoch: "a1b2c3d4"};
const checked = {revision: 42, device_time: 900, capacity: {points: 32}, persistence: {last_append_ms: 3},
  active_project: {id: "p1", point_count: 32}, active_line: null, undo_candidate: B,
  current_fix: {fix_seq: 7}, measurement_progress: {active: false}, network: {ntrip_state: "streaming"}};
const run = (name, live, snapshot) => { applied = []; store.state.live = live; adoptCheckedLive(snapshot); out[name] = applied; };
run("older", {revision: 41, undo_candidate: A, current_fix: {fix_seq: 9}}, checked);
run("same", {revision: 42, undo_candidate: B}, checked);
run("newer", {revision: 43, undo_candidate: Object.assign({}, B, {feature_id: "a43", label: "C1"})}, checked);
run("empty store", null, checked);
run("no snapshot", {revision: 41, undo_candidate: A}, null);
""", extra_prefixes=("const TRACKING_LIVE_KEYS=", "function adoptCheckedLive("), stubs=stubs)
        self.assertEqual(out["failures"], [])
        picked = {"revision": 42, "device_time": 900, "capacity": {"points": 32}, "persistence": {"last_append_ms": 3},
                  "active_project": {"id": "p1", "point_count": 32}, "active_line": None,
                  "undo_candidate": {"kind": "asset", "feature_id": "a42", "label": "Valve 21c", "record_order": 42,
                                     "request_id": "r1", "inventory_epoch": "a1b2c3d4"}}
        self.assertEqual(out["older"], [picked])
        self.assertEqual(list(out["older"][0]), ["revision", "device_time", "capacity", "persistence", "active_project",
                                                 "active_line", "undo_candidate"])
        self.assertEqual(out["same"], [picked])
        self.assertEqual(out["newer"], [])
        self.assertEqual(out["empty store"], [picked])
        self.assertEqual(out["no snapshot"], [])
        with open(os.path.join(ROOT, "tracking", "summary.py"), encoding="utf-8") as source:
            tracking_source = source.read()
        live_status = tracking_source[tracking_source.index("    def live_status(self):"):]
        live_status = live_status[:live_status.index("\n    def ", 10)]
        for key in picked:
            self.assertIn('"%s":' % key, live_status, key)

    def test_restore_summary_text_under_node(self):
        # #21c: the restore result names projects and points, from either server answer shape.
        if NODE is None:
            self.skipTest("node is unavailable")
        out = run_feedback_node(r"""
RtkI18n.setLanguage("en");
eq("ndjson", restoreSummaryText({projects: 3, features: 1245, legacy_values_skipped: 0}),
   "Survey backup restored: 3 projects, 1245 points");
eq("json", restoreSummaryText({projects: [{feature_count: 10}, {feature_count: 5}]}), "Survey backup restored: 2 projects, 15 points");
eq("empty", restoreSummaryText({}), "Survey backup restored");
eq("null", restoreSummaryText(null), "Survey backup restored");
eq("features only", restoreSummaryText({features: 4}), "Survey backup restored");
eq("zero features", restoreSummaryText({projects: 1, features: 0}), "Survey backup restored: 1 projects, 0 points");
eq("json, a project without a count", restoreSummaryText({projects: [{feature_count: 10}, {}]}), "Survey backup restored: 2 projects, 10 points");
RtkI18n.setLanguage("de");
eq("de ndjson", restoreSummaryText({projects: 3, features: 1245}), dict.de["ui.restore_done"].replace("{projects}", 3).replace("{points}", 1245));
eq("de plain", restoreSummaryText({}), dict.de["ui.restore_done_plain"]);
const all = [{projects: 3, features: 1245}, {projects: [{feature_count: 10}], legacy_values_skipped: 1}, {}, null, {projects: "x", features: "y"}];
for (const language of ["en", "de"]) {
  RtkI18n.setLanguage(language);
  all.forEach((result, index) => {
    const text = restoreSummaryText(result);
    if (/undefined|NaN|[{}]/.test(text)) failures.push(language + " " + index + ": " + text);
  });
}
out.de = [dict.de["ui.restore_done"], dict.de["ui.restore_done_plain"]];
""", extra_prefixes=("function restoreSummaryText(",))
        self.assertEqual(out["failures"], [])
        self.assertNotEqual(out["de"][0], "Survey backup restored: {projects} projects, {points} points")
        self.assertNotEqual(out["de"][1], "Survey backup restored")
        for key, text in (("ui.restoring_survey", "Restoring survey data \u2026"),
                          ("ui.optimizing_storage", "Optimizing recording storage \u2026"),
                          ("ui.optimize_done", "Recording storage optimized")):
            values = re.findall(r'"%s": "([^"]+)"' % re.escape(key), I18N_JS)
            self.assertEqual(len(values), 3, key)
            self.assertEqual(values[0], text)
            self.assertNotEqual(values[0], values[1], key)
            self.assertNotEqual(values[0], values[2], key)

    def test_restore_and_optimize_report_through_the_maintenance_wrapper(self):
        # #21c: both actions run under maintenanceAction (own key, no capture lock) and no longer write to the pill.
        restore = APP_JS[APP_JS.index("async function restoreSurvey("):APP_JS.index("\nasync function compactSurvey(")]
        compact = next(l for l in APP_JS.split("\n") if l.startswith("async function compactSurvey("))
        for name, body in (("restoreSurvey", restore), ("compactSurvey", compact)):
            self.assertIn("maintenanceAction(", body, name)
            self.assertNotIn("setConnection(", body, name)
            self.assertNotIn("captureAction(", body, name)
        wrapper = next(l for l in APP_JS.split("\n") if l.startswith("async function maintenanceAction("))
        self.assertIn('key:"maintenance"', wrapper)
        self.assertNotIn("captureRun", wrapper)

    def test_capture_helpers_under_node(self):
        if NODE is None:
            self.skipTest("node is unavailable")
        with open(os.path.join(ROOT, "measurement.py"), encoding="utf-8") as source:
            self.assertIn('raise ValueError("Measurement cancelled.")', source.read())
        out = run_feedback_node(r"""
const error = (fields, message) => Object.assign(new Error(message || "x"), fields);
const unknown = [new TypeError("Failed to fetch"), error({name: "TimeoutError"}), error({name: "AbortError"}),
  error({status: 500, code: "internal_error"}), error({status: 500}), error({status: 503, code: "device_busy"}),
  error({status: 503}), error({status: 502}), error({status: 507})];
const unclear = [error({status: 507, code: "tracking_storage_failed"})];
const refused = [error({status: 507, code: "storage_reserve"}), error({status: 503, code: "survey_loading"}),
  error({status: 400, code: "invalid_tracking_request"}), error({status: 409, code: "undo_candidate_changed"}),
  error({status: 409, code: "storage_busy"}), error({status: 409, code: "recording_active"}),
  error({name: "ForbiddenError"}), new Error("Session expired")];
unknown.forEach((value, index) => eq("unknown " + index, captureFailure(value), "unknown"));
unclear.forEach((value, index) => eq("unclear " + index, captureFailure(value), "unclear"));
refused.forEach((value, index) => eq("refused " + index, captureFailure(value), "refused"));
eq("cancelled", measurementCancelled(error({status: 400, code: "invalid_tracking_request"}, "Measurement cancelled.")), true);
eq("cancelled, older firmware", measurementCancelled(new Error("Measurement cancelled.")), true);
eq("without the full stop", measurementCancelled(error({status: 400, code: "invalid_tracking_request"}, "Measurement cancelled")), false);
eq("other refusal", measurementCancelled(error({status: 400, code: "invalid_tracking_request"}, "Select a project first.")), false);
eq("transport", measurementCancelled(new TypeError("Failed to fetch")), false);
const tagged = {undo_candidate: {kind: "asset", feature_id: "a1", label: "Valve", request_id: "r1"}};
eq("own candidate", ownCandidate(tagged, "r1"), tagged.undo_candidate);
eq("other request", ownCandidate(tagged, "r2"), null);
eq("untagged candidate, no request", ownCandidate({undo_candidate: {kind: "asset", request_id: null}}, null), null);
eq("untagged candidate, empty request", ownCandidate({undo_candidate: {kind: "asset"}}, ""), null);
eq("no candidate", ownCandidate({}, "r1"), null);
const ids = Array.from({length: 100}, () => newRequestId());
out.badIds = ids.filter(id => !/^r[a-z0-9]{6,20}$/.test(id) || id.length > 32);
out.distinct = new Set(ids).size;
""", extra_prefixes=("function captureFailure(", "function measurementCancelled(", "function ownCandidate(",
                     "function newRequestId("))
        self.assertEqual(out["failures"], [])
        self.assertEqual(out["badIds"], [])
        self.assertEqual(out["distinct"], 100)

    def test_verify_capture_under_node(self):
        # E19/r3-6: own answers requested after the loss decide; every fetch and pause ends by the 45 s deadline.
        if NODE is None:
            self.skipTest("node is unavailable")
        stubs = r"""
let clock = 0, answers = [], pauseClock = [], calls = [], timeouts = [], pauses = [], checks = [];
const performance = {now: () => clock};
const setTimeout = (fn, ms) => {
  checks.push(["pause", ms, 45000 - clock]); pauses.push(ms);
  const set = pauseClock.shift();
  clock = set === undefined ? clock + Math.trunc(ms) : set;
  fn();
};
const apiJsonBase = async (path, options) => {
  if (path !== "/api/ui/live") throw new Error("unexpected path " + path);
  calls.push(clock); timeouts.push(options.timeout); checks.push(["fetch", options.timeout, 45000 - clock]);
  if (!answers.length) throw new Error("answer sequence exhausted");
  const next = answers.shift();
  if (next === "TypeError") throw new TypeError("Failed to fetch");
  // A hanging fetch is aborted by a setTimeout timer; the browser truncates timer delays to whole milliseconds.
  if (next && next.hang) { clock += Math.trunc(options.timeout); throw Object.assign(new Error("Request timed out."), {name: "TimeoutError"}); }
  return JSON.parse(JSON.stringify(next));
};
const api = {json: async () => { throw new Error("shared live request"); }};
const hitCandidate = {kind: "asset", feature_id: "a9", line_id: null, label: "Valve 21c", index: null, record_order: 50,
  request_id: "r1", inventory_epoch: "a1b2c3d4"};
const hit = {undo_candidate: hitCandidate, measurement_progress: {active: false}};
const store = {state: {live: hit}};
"""
        out = run_feedback_node(r"""
const run = async (name, sequence, pause) => {
  clock = 0; answers = sequence.slice(); pauseClock = (pause || []).slice(); calls = []; timeouts = []; pauses = []; checks = [];
  const result = await verifyCapture("r1");
  for (const [kind, value, remaining] of checks) {
    if (kind === "pause" && !(value <= 2000 && value <= remaining)) failures.push(name + ": pause " + value + " > " + remaining);
    if (kind === "fetch" && !(value <= 5000 && value <= remaining)) failures.push(name + ": timeout " + value + " > " + remaining);
  }
  if (clock > 45000) failures.push(name + ": clock " + clock);
  return {result, calls: calls.slice(), timeouts: timeouts.slice(), pauses: pauses.slice(), clock, left: answers.length};
};
const foreign = Object.assign({}, hitCandidate, {feature_id: "a10", label: "B1", record_order: 51, request_id: "other"});
let r = await run("a", [hit]);
eq("a", [r.result.state, r.result.candidate && r.result.candidate.label, r.calls.length], ["saved", "Valve 21c", 1]);
eq("a snapshot", r.result.live, hit);
r = await run("b", [{measurement_progress: {active: true}}, hit]);
eq("b", [r.result.state, r.calls.length], ["saved", 2]);
r = await run("c", ["TypeError", "TypeError", hit]);
eq("c", [r.result.state, r.calls.length], ["saved", 3]);
r = await run("d", [{active_project: {point_count: 32}, undo_candidate: foreign, measurement_progress: {active: false}}]);
eq("d foreign +1", [r.result, r.calls.length], [{state: "unclear"}, 1]);
r = await run("e", [{undo_candidate: Object.assign({}, foreign, {request_id: null})}]);
eq("e own plus foreign", [r.result, r.calls.length], [{state: "unclear"}, 1]);
r = await run("f", [{tracking_startup: {state: "loading"}, undo_candidate: null}]);
eq("f restart", [r.result, r.calls.length], [{state: "unclear"}, 1]);
r = await run("g", [{active_project: {id: "p9"}, undo_candidate: Object.assign({}, hitCandidate, {request_id: null})}]);
eq("g project change", [r.result, r.calls.length], [{state: "unclear"}, 1]);
r = await run("h", Array.from({length: 9}, () => ({hang: true})));
eq("h deadline", r, {result: {state: "unclear"}, calls: [0, 7000, 14000, 21000, 28000, 35000, 42000],
  timeouts: [5000, 5000, 5000, 5000, 5000, 5000, 3000], pauses: [2000, 2000, 2000, 2000, 2000, 2000], clock: 45000, left: 2});
r = await run("i", [{undo_candidate: null}]);
eq("i old snapshot", [r.result, r.calls.length], [{state: "unclear"}, 1]);
r = await run("j", ["TypeError", {hang: true}, hit], [44500]);
eq("j hang before the deadline", r, {result: {state: "unclear"}, calls: [0, 44500], timeouts: [5000, 500], pauses: [2000],
  clock: 45000, left: 1});
r = await run("j2", ["TypeError", {hang: true}, hit], [44599.3]);
eq("j2 truncated abort leaves a sub-millisecond rest", r, {result: {state: "unclear"}, calls: [0, 44599.3],
  timeouts: [5000, 45000 - 44599.3], pauses: [2000], clock: 44599.3 + 400, left: 1});
r = await run("k", [{undo_candidate: Object.assign({}, hitCandidate, {inventory_epoch: "0badf00d", request_id: null})}]);
eq("k old tag after restore", [r.result, r.calls.length], [{state: "unclear"}, 1]);
""", extra_prefixes=("const CAPTURE_TAGGED=", "function ownCandidate(", "async function fetchLiveOnce(",
                     "async function verifyCapture("), stubs=stubs)
        self.assertEqual(out["failures"], [])
        verify = self.line_of("async function verifyCapture(")
        self.assertNotIn("store.", verify)
        self.assertNotIn("api.json", verify)
        self.assertIn("fetchLiveOnce(", verify)
        self.assertIn("apiJsonBase(", self.line_of("async function fetchLiveOnce("))
        self.assertIn("const CAPTURE_TAGGED=[\"add_vertex\",\"add_asset\",\"start_line\"],CAPTURE_VERIFY_MS=45000,"
                      "CAPTURE_VERIFY_STEP_MS=2000;", APP_JS)

    def block_of(self, prefix):
        """A top-level declaration up to the next top-level line (multi-line functions end with their `}` line)."""
        lines = APP_JS.split("\n")
        starts = [index for index, line in enumerate(lines) if line.startswith(prefix)]
        self.assertEqual(len(starts), 1, prefix)
        block = [lines[starts[0]]]
        for line in lines[starts[0] + 1:]:
            if not line.startswith((" ", "}")):
                break
            block.append(line)
            if line.startswith("}"):
                break
        return "\n".join(block)

    def test_capture_actions_use_one_path(self):
        # E8/E19/E20: every capture runs through captureAction; undo only with a concrete candidate.
        for prefix in ("async function addVertex(", "async function addAsset(", "async function lineState(",
                       "async function finishLine(", "async function undoLast(", "async function startLine("):
            block = self.block_of(prefix)
            self.assertIn("captureAction(", block, prefix)
            self.assertNotIn("showSuccess(", block, prefix)
            if prefix != "async function undoLast(":
                self.assertNotIn("showError(", block, prefix)
        for prefix in ("async function addVertex(", "async function addAsset(", "async function startLine("):
            self.assertIn("request_id:requestId", self.block_of(prefix), prefix)
        undo = self.block_of("async function undoLast(")
        for token in ("fetchLiveOnce(", "ui.undo_nothing", "expect:undoIdentity(candidate)"):
            self.assertIn(token, undo)
        self.assertNotIn("Remove the most recently recorded", undo)
        self.assertNotIn("Remove the most recently recorded", APP_JS)
        positions = [match.start() for match in re.finditer(re.escape('trackingAction("undo"'), APP_JS)]
        self.assertGreaterEqual(len(positions), 2)
        for position in positions:
            self.assertTrue(APP_JS.startswith('trackingAction("undo",{expect:undoIdentity(', position),
                            APP_JS[position:position + 60])
        for name in ("captureEvidence", "captureOutcome", "captureUnconfirmed", "resolveUnconfirmed"):
            self.assertNotIn(name, APP_JS)
        locked = self.line_of("function captureLocked(")
        self.assertIn("transportLost(", locked)
        self.assertNotIn("statusRefs.lost", locked)
        measured = self.block_of("async function measuredAction(")
        self.assertIn("measurementCancelled(error)", measured)
        self.assertNotIn("return cancelled?null:result", measured)
        self.assertNotIn("if(cancelled)return null", measured)
        with open(os.path.join(ROOT, "src", "50-status.js"), encoding="utf-8") as source:
            status = source.read()
        self.assertNotIn("replace(statusRefs.actions", status)
        # #29b E8: the start line rule and the lock while a measurement runs live once, in the action model.
        model = self.line_of("function measureActionModel(")
        self.assertIn('button("start-line","Start line","primary",!fixIsUsable(l.current_fix)||capacityFull||storageLow)', model)
        self.assertIn('if(!item.kind.split(" ").includes("danger"))item.disabled=true;', model)
        self.assertNotIn('querySelector(":scope > button")', status)
        self.assertNotIn('querySelectorAll("button:not(.danger)', status)
        # #24-UI E24 and #29b E7: the field setup summary is the last step of updateStatus, after the feedback slots,
        # which follow the freshness step.
        steps = self.line_of("function updateStatus(")
        self.assertTrue(steps.endswith("applyLiveFreshness(live);placeFeedbackSlots(live);renderFieldSummary(live);}"))
        self.assertNotIn("updateStatus=function", APP_JS)
        core = self.block_of("function renderStatusCore(")
        self.assertIn("renderActionBar(statusRefs.actions,model);renderMeasurementResume(statusRefs.actions,model.resume);", core)
        self.assertNotIn("clearActionBar(statusRefs.actions)", core)
        self.assertIn('querySelectorAll("button:not([data-feedback-dismiss])")', self.line_of("function applyLiveFreshness("))
        self.assertIn("applyCaptureLock();", self.line_of("function renderFeedback("))
        # The bar's Cancel measurement stays usable under the own run's lock, but not when the data is lost.
        self.assertIn('transportLost()?"button:not([data-feedback-dismiss])":"button:not([data-feedback-dismiss])'
                      ':not([data-measurement-cancel])"', self.line_of("function applyCaptureLock("))
        self.assertEqual(status.count('cancel.setAttribute("data-measurement-cancel","");'), 1)
        self.assertIn('cancel.setAttribute("data-measurement-cancel","");', self.line_of("function renderMeasurementResume("))
        # A confirmed capture hands its checked snapshot to the store before its Undo is offered (E19).
        self.assertIn('const check=await verifyCapture(requestId);if(check.state==="saved")adoptCheckedLive(check.live);'
                      'showFeedback(', self.line_of("async function captureAction("))
        self.assertIn("const captureLockTick=liveDialogChecks.add(applyCaptureLock);", APP_JS)
        block = APP_CSS[APP_CSS.index("/* M2 #21c/#30/#45:"):]
        self.assertIn(".capture-strip{grid-column:1/-1;display:grid;gap:var(--s2);max-height:min(30dvh,14rem);"
                      "overflow-y:auto}", block)
        self.assertIn(".capture-strip .feedback-item{box-shadow:none}", block)

    def test_capture_texts_exist_in_both_languages(self):
        en = {"ui.capture_starting_line": "Starting line …", "ui.capture_saving_vertex": "Saving line point …",
              "ui.capture_saving_asset": "Saving object point …", "ui.capture_pausing": "Pausing line …",
              "ui.capture_resuming": "Resuming line …", "ui.capture_finishing": "Finishing line …",
              "ui.capture_undoing": "Removing last point …", "ui.line_paused": "Line paused",
              "ui.line_resumed": "Line resumed", "ui.undo_point": "Undo",
              "ui.undo_changed": "The last point changed – nothing was removed.",
              "ui.undo_nothing": "Nothing to undo – no recorded point found",
              "ui.capture_checking": "Result unknown – checking the device …",
              "ui.capture_confirmed": "Saved – confirmed by the device",
              "ui.capture_unclear": "Result unclear – check the project", "ui.capture_open_project": "Open project",
              "ui.capture_cancelled": "Measurement cancelled"}
        for key, english in en.items():
            values = re.findall(r'"%s": "([^"]+)"' % re.escape(key), I18N_JS)
            self.assertEqual(len(values), 3, key)
            self.assertEqual(values[0], english, key)
            self.assertNotEqual(values[1], english, key)
            self.assertNotEqual(values[2], english, key)
            self.assertIn('tr("%s","%s"' % (key, english), APP_JS)

    def test_v2_restart_button_uses_explicit_translation_key(self):
        self.assertIn('label==="Restart device"', APP_JS)
        self.assertIn('tr("ui.device_restart","Restart device")', APP_JS)
        self.assertIn('tr("ui.device_restart_confirm"', APP_JS)

    def test_v2_keeps_complete_navigation_and_field_contracts(self):
        for contract in ('project=route.view==="project"||route.view==="project-stakeout"',
                         'function updateGlobalStatus(live)',
                         '[hidden]{display:none!important}', 'line.last_point',
                         'data-context="storage"', 'diameter_mm', 'burial_depth_m',
                         'Load more line points', 'refreshMap=async',
                         'navigator.clipboard&&navigator.clipboard.writeText',
                         'password_set:Boolean',
                         'keepAwake=localStorage'):
            self.assertIn(contract, APP_JS + APP_CSS)
        self.assertNotIn('back.href=projectChild', APP_JS)
        self.assertIn('const types=["tap","valve","hydrant","branch","transition","control","tree","repair","line_start","line_end","other"]', APP_JS)
        self.assertNotIn('"meter","junction","bend","reducer"', APP_JS)
        # #24-UI Task 8, #46: the export flow builds the one file for share and download from the checked chunks.
        self.assertIn('file=new File([new Blob(chunks,{type:"application/geo+json"})],`${project.name}.geojson`,{type:"application/geo+json"})', APP_JS)

    def test_configuration_skips_the_restart_wait_when_nothing_restarts(self):
        english = "Configuration saved and in effect; no restart needed."
        self.assertIn("summary.restart_required===false", APP_JS)
        self.assertIn(english, APP_JS)
        # The translated texts live only in i18n.js; tests read them from there.
        values = re.findall(r'"%s": "([^"]+)"' % re.escape(english), I18N_JS)
        self.assertEqual(len(values), 2)
        for translated in values:
            self.assertNotEqual(translated, english)

    def test_configuration_form_states_its_constraints(self):
        # Source pins; the browser cases A1-A13 (tests/guided_apply_acceptance.js) prove the behaviour.
        for contract in ('function configField(config,name,label,type="text",help="",attrs={})',
                         'tr("ui.guided_check_changes"', 'tr("ui.guided_unsaved"',
                         'class:"config-review"', 'class:"config-fields"', 'class:"config-staged"',
                         'class:"unsaved-marker"', "staging_revision=", 'tr("ui.network_format_help"'):
            self.assertIn(contract, APP_JS)
        for key, english in (("ui.guided_check_changes", "Check changes"), ("ui.guided_unsaved", "Unsaved changes")):
            self.assertEqual(len(re.findall(r'"%s": "' % key, I18N_JS)), 3, key)
            self.assertIn('"%s": "%s"' % (key, english), I18N_JS)
        # The blanket input listener of src/99-boot.js is gone; the form computes its own dirty state.
        self.assertNotIn('if(form)form.dataset.dirty="true";', APP_JS)
        with open(os.path.join(ROOT, "src", "99-boot.js"), encoding="utf-8") as source:
            boot = source.read()
        self.assertNotIn('addEventListener("input"', boot)
        # The link guard and beforeunload keep reading data-dirty.
        self.assertIn('.configuration-layout[data-dirty="true"]', boot)
        self.assertIn('addEventListener("beforeunload"', boot)
        # The static constraints come from cfg_validation.validate_settings.
        for attribute in ('maxLength:32', 'minLength:8', 'inputMode:"numeric"', 'pattern:"[^\\\\/]*"', 'autocapitalize:"off"'):
            self.assertIn(attribute, APP_JS)

    def test_v2_restores_field_and_diagnostic_parity(self):
        for contract in ('async function undoLast()', 'trackingAction("undo",{expect:undoIdentity(',
                         'min:"0",max:"100",step:"0.01"', 'value:"tap"',
                         'vertical_delta_m', 'vertex.vertical_step_m', 'vertex.source',
                         'Reference station', 'Last NTRIP error', 'fix_quality_seconds',
                         'Recording journal', 'Caster host', 'BLE service UUID',
                         'CPU clock', 'IDF heap free', 'Wi-Fi transmit power',
                         'Automatic on', 'Clock time', 'Share filtered'):
            self.assertIn(contract, APP_JS)
        self.assertIn('measurementStarted=false', APP_JS)
        self.assertIn('if(progress.active)measurementStarted=true', APP_JS)
        self.assertIn('if(measurementStarted){', APP_JS)
        self.assertIn('details.open&&Date.now()-statusDetailsUpdatedAt>=30000', APP_JS)
        self.assertIn('Math.max(.5,Math.min(4', APP_JS)
        self.assertIn('minus.disabled=zoom<=.5', APP_JS)
        self.assertIn('class","map-world"', APP_JS)
        self.assertIn('scale(${1/zoom})', APP_JS)
        self.assertIn('mapGraphic.scaleMetres/zoom', APP_JS)
        self.assertIn('class:"map-layers"', APP_JS)
        self.assertIn('requestFullscreen', APP_JS)
        self.assertIn('[1,5,10,"quality"]', APP_JS)
        self.assertIn('progress.mode==="quality"', APP_JS)
        self.assertIn('qualityMode?null:undefined', APP_JS)
        self.assertIn('.readiness-hero .metric-grid{', APP_CSS)
        self.assertIn('.map-world{', APP_CSS)
        self.assertIn('.quality-history-row', APP_CSS)
        self.assertIn('progress::-webkit-progress-value', APP_CSS)

    def test_every_restart_uses_the_shared_watch(self):
        # Pins complement the browser cases C1-C11 of tests/guided_apply_acceptance.js; they replace none of them.
        for needle in ('requestRestart("reboot"', 'requestRestart("restore"', 'requestRestart("apply"',
                       'api.post("/api/config/restore?apply_id="', 'startRestartWatch(', 'confirmRestart({',
                       'localText(description)'):
            self.assertIn(needle, APP_JS, needle)
        for needle in ('/status?_reboot=', 'setTimeout(resolve,8000)', 'api.write("/api/config/restore'):
            self.assertNotIn(needle, APP_JS, needle)

    def test_field_actions_use_the_compact_performance_path(self):
        self.assertIn('/api/tracking?view=progress', APP_JS)
        self.assertIn('setTimeout(update,650)', APP_JS)
        self.assertIn('measurementRequestActive&&!force', APP_JS)
        self.assertIn('if(measurementRequestActive)return', APP_JS)
        self.assertIn('function applyTrackingMutation(result)', APP_JS)
        self.assertIn('result.tracking_live', APP_JS)
        self.assertIn('button.classList.add("busy")', APP_JS)
        self.assertIn('.btn.busy::after', APP_CSS)
        self.assertNotIn('setTimeout(update,350)', APP_JS)
        self.assertNotIn('if(result)showSuccess("Line point saved");await pollLive(true)', APP_JS)
        self.assertIn('function mergeMapDelta(map,delta)', APP_JS)
        self.assertIn('since_revision=${Number(map.revision)||0}', APP_JS)

    def test_field_app_renders_exact_e8_coordinates(self):
        self.assertIn("function e8Text(", APP_JS)
        self.assertIn("function coordText(", APP_JS)
        self.assertIn("function fixLonLat(", APP_JS)
        self.assertIn("lat_e8", APP_JS)
        self.assertIn("lon_e8", APP_JS)
        self.assertNotIn("Number(fix.lat).toFixed(7)", APP_JS)

    def test_e8_helpers_evaluate_under_node_and_the_call_sites_use_them(self):
        # The presence test above also passes with the e8 path switched off, so
        # pin the call sites (whitespace removed, a reformat must not matter)
        # and evaluate the helpers themselves.
        compact = "".join(APP_JS.split())
        # "fixLonLat(fix)" alone would match the declaration, so the centring
        # call carries its context.
        for call in ("coordText(fix.lat_e8,fix.lat)", "coordText(fix.lon_e8,fix.lon)",
                     "centerPosition?fixLonLat(fix):null", "fixLonLat(current)"):
            self.assertTrue(call in compact, "app.js lost the call site " + call)
        if NODE is None:
            self.skipTest("node is unavailable")
        declarations = []
        for name in ("e8Text", "coordText", "fixLonLat"):
            lines = [line for line in APP_JS.split("\n")
                     if line.startswith("function %s(" % name)]
            self.assertEqual(len(lines), 1, "%s must be one single-line declaration" % name)
            declarations.append(lines[0])
        script = r"""
const {e8Text, coordText, fixLonLat} = new Function('"use strict";' + process.argv[1] +
  "\nreturn {e8Text, coordText, fixLonLat};")();
console.log(JSON.stringify({
  e8: [5112345678, -65432100, 2, 0, -18000000000].map(e8Text),
  text: [coordText(5112345678, 51.1), coordText(null, 51.1234567),
         coordText(undefined, 8), coordText(5112345678.5, 51.1), coordText(2 ** 53, 51.1)],
  lonLat: [fixLonLat({lat_e8: 5112345678, lon_e8: 1000000000, lat: 1, lon: 2}),
           fixLonLat({lat: 50, lon: 8}), fixLonLat({}), fixLonLat(null),
           fixLonLat({lat_e8: 5112345678.5, lon_e8: 1000000000, lat: 50, lon: 8}),
           fixLonLat({lat_e8: 5112345678, lon_e8: null, lat: 50, lon: 8})],
}));
"""
        run = subprocess.run([NODE, "-e", script, "\n".join(declarations)],
                             capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        result = json.loads(run.stdout)
        self.assertEqual(result["e8"], ["51.12345678", "-0.65432100", "0.00000002",
                                        "0.00000000", "-180.00000000"])
        # A fractional, unsafe or missing e8 is no exact position: the floats are used.
        self.assertEqual(result["text"], ["51.12345678", "51.1234567", "8.0000000",
                                          "51.1000000", "51.1000000"])
        self.assertEqual(result["lonLat"], [[10, 51.12345678], [8, 50], None, None,
                                            [8, 50], [8, 50]])

    def test_auth_failure_total_under_node(self):
        # Mandatory under node (no skip): the sum is the one number the device
        # page and the access page show, so it is evaluated, not pattern-matched.
        self.assertIsNotNone(NODE, "node is required for this test; CI installs it")
        for source in ("src/75-device.js", "src/77-device-areas.js"):
            with open(os.path.join(ROOT, source), encoding="utf-8") as handle:
                self.assertFalse("stats.tcp_auth_failures" in handle.read(),
                                 source + " still reads stats.tcp_auth_failures")
        # Each mount function is one line of app.js; whitespace is removed so a
        # reformat does not matter.
        # #24-UI: the overview links the areas; the connections page (access group) shows the sum.
        for scope in ("function accessGroup(",):
            found = [line for line in APP_JS.split("\n") if line.startswith(scope)]
            self.assertEqual(len(found), 1, scope)
            self.assertIn("authFailureTotal(stats)", "".join(found[0].split()), scope)
        lines = [line for line in APP_JS.split("\n")
                 if line.startswith("function authFailureTotal(")]
        self.assertEqual(len(lines), 1, "authFailureTotal must be one single-line declaration")
        script = r"""
const {authFailureTotal} = new Function('"use strict";' + process.argv[1] +
  "\nreturn {authFailureTotal};")();
console.log(JSON.stringify([
  {ble_auth_failures: 1, tcp_auth_failures: 1, nmea_tcp_auth_failures: 2},
  {nmea_tcp_auth_failures: 2}, {ble_auth_failures: 1}, {tcp_auth_failures: 5},
  {}, null, undefined,
  {ble_auth_failures: "2", nmea_tcp_auth_failures: "3"},
  {ble_auth_failures: NaN, nmea_tcp_auth_failures: null},
].map(authFailureTotal)));
"""
        run = subprocess.run([NODE, "-e", script, lines[0]],
                             capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout), [3, 2, 1, 0, 0, 0, 0, 5, 0])

    def test_fix_lon_lat_needs_both_float_axes_under_node(self):
        # A fix with one unreadable axis (null) must not be drawn or centred at
        # 0 on that axis: Number(null) is 0, so the float fallback needs both.
        if NODE is None:
            self.skipTest("node is unavailable")
        lines = [line for line in APP_JS.split("\n") if line.startswith("function fixLonLat(")]
        self.assertEqual(len(lines), 1, "fixLonLat must be one single-line declaration")
        script = r"""
const {fixLonLat} = new Function('"use strict";' + process.argv[1] +
  "\nreturn {fixLonLat};")();
console.log(JSON.stringify([
  fixLonLat({lat_e8: null, lon_e8: 1000000000, lat: null, lon: 10}),
  fixLonLat({lat_e8: 5000000000, lon_e8: null, lat: 50, lon: null}),
  fixLonLat({lat: null, lon: 10}), fixLonLat({lat: "50", lon: "8"}),
  fixLonLat({lat_e8: 0, lon_e8: 0, lat: 0, lon: 0}), fixLonLat({lat: 0, lon: 0}),
  fixLonLat({lat: 50, lon: 8}),
  fixLonLat({lat_e8: 5000000000.5, lon_e8: 800000000, lat: 50, lon: 8}),
  fixLonLat({}), fixLonLat(null)]));
"""
        run = subprocess.run([NODE, "-e", script, lines[0]],
                             capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout), [None, None, None, None, [0, 0], [0, 0],
                                                  [8, 50], [8, 50], None, None])

    def test_numbers_follow_the_language_under_node(self):
        # Measurement numbers use the language's decimal mark; the exact coordinate
        # path and the status position line stay locale independent.
        self.assertIn("fixedText(fix.alt,3)", "".join(APP_JS.split()))
        if NODE is None:
            self.skipTest("node is unavailable")
        declarations = [line for line in APP_JS.split("\n")
                        if line.startswith(("const fmt=", "const fixedText="))]
        self.assertEqual(len(declarations), 2, "fmt and fixedText must be single-line declarations")
        for name in ("e8Text", "mmText"):
            lines = [line for line in APP_JS.split("\n") if line.startswith("function %s(" % name)]
            self.assertEqual(len(lines), 1, "%s must be one single-line declaration" % name)
            declarations.append(lines[0])
        script = r"""
globalThis.RtkI18n = require(process.cwd() + "/i18n.js");
const f = new Function('"use strict";' + process.argv[1] + "\nreturn {fmt, fixedText, e8Text, mmText};")();
const out = {};
const sample = () => [f.fmt(0.082, 3), f.fmt(1.25, 1), f.fmt(1234.5, 1), f.fmt(null), f.fmt(undefined),
  f.fmt(7), f.fmt("2.5", 1), f.fixedText(100, 3), f.fixedText(null), f.e8Text(5000000000), f.mmText(147100)];
out.en = sample();
globalThis.RtkI18n.setLanguage("de");
out.de = sample();
globalThis.RtkI18n.setLanguage("en");
out.enAgain = sample();
console.log(JSON.stringify(out));
"""
        run = subprocess.run([NODE, "-e", script, "\n".join(declarations)], cwd=ROOT,
                             capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        result = json.loads(run.stdout)
        self.assertEqual(result["en"], ["0.082", "1.3", "1234.5", "\u2014", "\u2014", "7.00", "2.5",
                                        "100.000", "\u2014", "50.00000000", "147.100 m"])
        self.assertEqual(result["de"], ["0,082", "1,3", "1234,5", "\u2014", "\u2014", "7,00", "2,5",
                                        "100.000", "\u2014", "50.00000000", "147.100 m"])
        self.assertEqual(result["enAgain"], result["en"])

    def test_field_app_handles_exact_stored_coordinates(self):
        with open(os.path.join(ROOT, "app.js"), encoding="utf-8") as source:
            javascript = source.read()
        self.assertIn("function coordToDeg(", javascript)
        self.assertIn('"legacy_float32"', javascript)
        self.assertIn("ui.precision_legacy", javascript)
        self.assertIn("ui.legacy_vertices", javascript)
        self.assertIn("[2,3,4,5].includes(value.schema_version)", javascript)
        self.assertNotIn("first.schema_version===4;", javascript)

    def test_coord_to_deg_converts_stored_integers_under_node(self):
        compact = "".join(APP_JS.split())
        # The map must draw degrees, never raw integers: the overlap key and the
        # projection both go through the helper.
        for call in ("raw.push(coordToDeg(c))", "raw.push(coordToDeg(point.coordinate))",
                     "(line.coordinates||[]).map(c=>xy(coordToDeg(c))", "xy(degrees)", "degrees=coordToDeg(point.coordinate)"):
            self.assertTrue(call in compact, "app.js lost the call site " + call)
        self.assertNotIn("point.coordinate.slice(0,2)", compact)
        if NODE is None:
            self.skipTest("node is unavailable")
        lines = [line for line in APP_JS.split("\n") if line.startswith("function coordToDeg(")]
        self.assertEqual(len(lines), 1, "coordToDeg must be one single-line declaration")
        script = r"""
const {coordToDeg} = new Function('"use strict";' + process.argv[1] +
  "\nreturn {coordToDeg};")();
console.log(JSON.stringify([coordToDeg([800000000, 5000000000, 123400]),
  coordToDeg([-65432100, -5000000000, -2500]), coordToDeg([0, 0, 0])]));
"""
        run = subprocess.run([NODE, "-e", script, lines[0]],
                             capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout),
                         [[8, 50, 123.4], [-0.654321, -50, -2.5], [0, 0, 0]])

    def test_legacy_restore_and_detail_helpers_under_node(self):
        compact = "".join(APP_JS.split())
        # Restores route through the predicate and report skipped legacy values;
        # an archive reactivation reports them as well.
        self.assertIn("ndjson=isNdjsonBackup(source)", compact)
        self.assertEqual(compact.count("reportLegacySkipped(result)"), 2)
        self.assertIn("reportLegacySkipped(awaittrackingAction(\"reactivate_archive\"", compact)
        # Rows carry the short label, the line notice the full text; the point
        # detail renders its position through positionText.
        self.assertIn('tr("ui.precision_legacy_short"', compact)
        self.assertIn("legacyVerticesText(lineResult.legacy_vertex_count)", compact)
        self.assertIn("positionText(point.coordinate,legacy)", compact)
        if NODE is None:
            self.skipTest("node is unavailable")
        declarations = [line for line in APP_JS.split("\n") if line.startswith("const tr=")]
        for name in ("e8Text", "coordToDeg", "legacySkippedText", "isNdjsonBackup",
                     "legacyVerticesText", "positionText", "mergeMapDelta"):
            lines = [line for line in APP_JS.split("\n")
                     if line.startswith("function %s(" % name)]
            self.assertEqual(len(lines), 1, "%s must be one single-line declaration" % name)
            declarations.append(lines[0])
        script = r"""
globalThis.RtkI18n = require(process.argv[2]);
const f = new Function('"use strict";' + process.argv[1] +
  "\nreturn {legacySkippedText, isNdjsonBackup, legacyVerticesText, positionText, mergeMapDelta};")();
const header = schema => JSON.stringify({record: "header", kind: "survey_backup", schema_version: schema});
const map = {revision: 1, lines: [{id: "l", coordinates: [[800000000, 5000000000, 1000]], vertex_count: 1}], points: []};
const legacyPoint = {id: "a", coordinate: [800050000, 5000050000, 100500],
                     measurement: {precision: "legacy_float32"}};
f.mergeMapDelta(map, {delta: true, revision: 3, changes: [
  {op: "asset", point: legacyPoint},
  {op: "vertex", line_id: "l", coordinate: [800001000, 5000000000, 1000], precision: "legacy_float32"}]});
console.log(JSON.stringify({
  skipped: [f.legacySkippedText({legacy_values_skipped: 2}), f.legacySkippedText({legacy_values_skipped: 1}),
            f.legacySkippedText({legacy_values_skipped: 0}), f.legacySkippedText({}),
            f.legacySkippedText(null)],
  route: [f.isNdjsonBackup(header(5) + "\n{}"), f.isNdjsonBackup(header(4)),
          f.isNdjsonBackup(header(3)), f.isNdjsonBackup('{"kind":"survey_backup","projects":[]}'),
          f.isNdjsonBackup("null"), f.isNdjsonBackup("not json"), f.isNdjsonBackup("")],
  vertices: [f.legacyVerticesText(1), f.legacyVerticesText(3), f.legacyVerticesText(0)],
  position: [f.positionText([800000000, 5000000000, 100000], false),
             f.positionText([800050000, 5000050000, 100500], true),
             f.positionText([800000000, 5000000000, null], false),
             f.positionText([800000000, 5000000000], true), f.positionText(null, false)],
  map: [map.revision, map.points, map.lines[0].coordinates.length, map.lines[0].vertex_count],
}));
"""
        run = subprocess.run([NODE, "-e", script, "\n".join(declarations),
                              os.path.join(ROOT, "i18n.js")],
                             capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        result = json.loads(run.stdout)
        self.assertEqual(result["skipped"], [
            "2 legacy values could not be converted and were left out; see the device log",
            "One legacy value could not be converted and was left out; see the device log",
            None, None, None])
        self.assertEqual(result["route"], [True, True, False, False, False, False, False])
        self.assertEqual(result["vertices"], [
            "One line point recorded before V15.9 is quantised to about 0.4 m",
            "3 line points recorded before V15.9 are quantised to about 0.4 m", None])
        self.assertEqual(result["position"], [
            "50.00000000 / 8.00000000 · 100.000 m", "50.0005000 / 8.0005000 · 100.500 m",
            "50.00000000 / 8.00000000", "50.0000000 / 8.0000000", None])
        self.assertEqual(result["map"], [3, [{"id": "a", "coordinate": [800050000, 5000050000, 100500],
                                              "measurement": {"precision": "legacy_float32"}}], 2, 2])

    def test_restore_dialog_names_the_whole_backup_scope(self):
        if NODE is None:
            self.skipTest("node is unavailable")
        spa_restore = ("This replaces Wi-Fi and NTRIP settings, the measurement rules and method, "
                       "and the field setup (antenna height, reference frame, epoch), then restarts "
                       "the receiver. Backups from older firmware contain only Wi-Fi and NTRIP settings.")
        detail_en = ("This replaces Wi-Fi and NTRIP settings, the measurement rules and method, and "
                     "the field setup, then restarts the device. Backups from older firmware contain "
                     "only Wi-Fi and NTRIP settings. Continue?")
        old_spa = "This replaces Wi-Fi and NTRIP settings and restarts the receiver."
        with open(os.path.join(ROOT, "src", "80-configuration.js"), encoding="utf-8") as source:
            configuration = source.read()
        self.assertIn(json.dumps(spa_restore), configuration)
        self.assertNotIn(json.dumps(old_spa), configuration)
        # app.js is generated from src/; the shipped bundle must carry the same text.
        self.assertIn(json.dumps(spa_restore), APP_JS)
        self.assertNotIn(json.dumps(old_spa), APP_JS)
        # English value in the dictionary plus the reverse-map keys for the SPA (DE and PL).
        self.assertEqual(I18N_JS.count(
            "Backups from older firmware contain only Wi-Fi and NTRIP settings"), 3)
        script = ("const i=require('./i18n.js');i.setLanguage('en');const en=i.t('ui.confirm_restore_detail');"
                  "i.setLanguage('de');const de=i.t('ui.confirm_restore_detail');"
                  "console.log(JSON.stringify({en,de,spa:i.v2de[%s],old:i.v2de[%s]||null}));"
                  % (json.dumps(spa_restore), json.dumps(old_spa)))
        run = subprocess.run([NODE, "-e", script], cwd=ROOT, capture_output=True, text=True,
                             timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        shown = json.loads(run.stdout)
        self.assertEqual(shown["en"], detail_en)
        self.assertIsNone(shown["old"])
        for german in (shown["de"], shown["spa"]):
            self.assertTrue(german, shown)
            self.assertNotIn(german, (detail_en, spa_restore))
            self.assertIn("NTRIP", german)
            self.assertEqual(german.count("."), 2, german)  # scope sentence + older backups

    def test_legacy_texts_exist_in_both_languages(self):
        if NODE is None:
            self.skipTest("node is unavailable")
        run = subprocess.run([NODE, "-e", "console.log(JSON.stringify(require(process.argv[1])"
                              ".dictionaries))", os.path.join(ROOT, "i18n.js")],
                             capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        dictionaries = json.loads(run.stdout)
        english = {
            "ui.legacy_values_skipped": "{count} legacy values could not be converted and were left out; see the device log",
            "ui.legacy_value_skipped": "One legacy value could not be converted and was left out; see the device log",
            "ui.legacy_vertex": "One line point recorded before V15.9 is quantised to about 0.4 m",
            "ui.precision_legacy_short": "Legacy"}
        for key, text in english.items():
            self.assertEqual(dictionaries["en"][key], text)
            translated = dictionaries["de"][key]
            self.assertTrue(translated and translated != text, key)
            self.assertEqual("{count}" in translated, "{count}" in text, key)

    def test_field_app_handles_a_stale_receiver_position(self):
        compact = "".join(APP_JS.split())
        self.assertIn("function fixIsUsable(", APP_JS)
        self.assertIn("function ingestFreshness(", APP_JS)
        self.assertIn("staleSinceSeq", APP_JS)
        self.assertNotIn("fix.fix_seq<staleSinceSeq", compact)
        self.assertIn("fix.fix_seq>staleSinceSeq", compact)
        self.assertIn("uptime_sec", compact)
        self.assertNotIn("boot_count", compact[compact.index("functioningestFreshness("):
                                               compact.index("letactiveController")])
        self.assertIn("ui.fix_stale", APP_JS)
        self.assertIn("ui.no_current_position", APP_JS)
        self.assertIn('"stale"', APP_JS)
        self.assertIn(".readiness-hero.stale", APP_CSS)
        self.assertIn(".map-position.stale", APP_CSS)
        self.assertIn(".map-position.hidden", APP_CSS)
        # Measurement dialogs lock their submit on a stale position and re-check before submitting.
        self.assertIn("functionguardStaleSubmit(dialog,form,save,extra)", compact)
        self.assertEqual(compact.count("requiresFix:true"), 1)
        self.assertIn("submitGuard:(live,form)=>startLineVerdict(live,form.elements.recording_mode.value)",
                      compact)
        self.assertIn("if(fixGuard&&!fixGuard())return;", compact)
        self.assertIn("if(!fixGuard())return;resolve(", compact)
        # The map chip says RTK FIXED only while the freshness latch is open.
        self.assertIn("&&fixIsUsable(next.current_fix);chip.className", compact)
        # Every live-store replacement reaches updateGlobalStatus, which ingests first.
        self.assertIn("functionupdateGlobalStatus(live){ingestFreshness(live);", compact)

    def line_of(self, prefix):
        lines = [line for line in APP_JS.split("\n") if line.startswith(prefix)]
        self.assertEqual(len(lines), 1, "%s must be one single-line declaration" % prefix)
        return lines[0]

    def test_live_age_follows_new_fix_data_under_node(self):
        if NODE is None:
            self.skipTest("node is unavailable")
        for prefix in ("function noteLiveAnswer(", "function noteLiveAttempt(", "function liveAgeMs("):
            self.assertIn("performance.now()", self.line_of(prefix))
            self.assertNotIn("Date.now", self.line_of(prefix))
        self.assertIn("current_fix", self.line_of("function noteLiveAnswer("))
        out = run_freshness_node(r"""
eq("no stamp", liveAgeMs(1000), null);
const a = {current_fix: {}};
eq("first stamp", noteLiveAnswer(a, 1000), true);
eq("same answer again", noteLiveAnswer(a, 5000), false);
eq("merged copy keeps the fix reference", noteLiveAnswer(Object.assign({}, a, {revision: 9}), 5000), false);
eq("age", liveAgeMs(5000), 4000);
eq("empty object", noteLiveAnswer({}, 6000), false);
eq("null fix", noteLiveAnswer({current_fix: null}, 6000), false);
eq("null live", noteLiveAnswer(null, 6000), false);
eq("undefined live", noteLiveAnswer(undefined, 6000), false);
eq("new fix object", noteLiveAnswer({current_fix: {}}, 7000), true);
eq("never negative", liveAgeMs(6500), 0);
const ages = [null, 0, 2999, 3000, 9999, 10000, 60000];
eq("states 2 s", ages.map(age => liveLinkState(age, 2000)),
  ["connecting", "live", "live", "delayed", "delayed", "lost", "lost"]);
eq("states 5 s", [5999, 6000, 12999, 13000].map(age => liveLinkState(age, 5000)),
  ["live", "delayed", "delayed", "lost"]);
eq("states default", ages.map(age => liveLinkState(age, undefined)), ages.map(age => liveLinkState(age, 2000)));
""")
        self.assertEqual(out["failures"], [])

    def test_live_view_without_any_answer_under_node(self):
        if NODE is None:
            self.skipTest("node is unavailable")
        out = run_freshness_node(r"""
eq("before any attempt", liveView(2000, 500), {age: null, link: "connecting", never: true});
noteLiveAttempt(1000);
eq("waiting", liveView(2000, 10999).link, "connecting");
eq("lost without data", liveView(2000, 11000), {age: null, link: "lost", never: true});
eq("5 s poll waiting", liveView(5000, 13999).link, "connecting");
eq("5 s poll lost", liveView(5000, 14000).link, "lost");
noteLiveAttempt(50000);
eq("attempt does not restart", liveView(2000, 50500).link, "lost");
noteLiveAttempt(50000, true);
eq("restart", liveView(2000, 55000).link, "connecting");
noteLiveAnswer({current_fix: {}}, 56000);
eq("after data", liveView(2000, 57000), {age: 1000, link: "live", never: false});
noteLiveAttempt(60000, true);
eq("restart ignored once data came", liveView(2000, 67000).link, "lost");
eq("never false", liveView(2000, 67000).never, false);
""")
        self.assertEqual(out["failures"], [])

    def test_tracking_mutation_keeps_the_data_age_under_node(self):
        if NODE is None:
            self.skipTest("node is unavailable")
        with mock.patch.object(tracking.Tracker, "storage_capacity", return_value={
                "free_flash_bytes": 8_000_000, "required_flash_reserve_bytes": 3_000_000,
                "estimated_temporary_bytes": 16_384, "writable": True}), \
                tempfile.TemporaryDirectory(prefix="esp-rtk-fresh-") as tmp:
            tracker = tracking.Tracker(os.path.join(tmp, "tracking.jsonl"))
            tracker.create_project("Mutation 21b")
            partial = tracker.live_status()
        self.assertTrue(partial["active_project"])
        self.assertNotIn("current_fix", partial)
        self.assertNotIn("current_quality", partial)
        stubs = ("let clock=0;const store={state:{live:null},set(patch){Object.assign(this.state,patch);}};"
                 "const statusRefs=null;const updateGlobalStatus=live=>{noteLiveAnswer(live,clock);};"
                 "const invalidateLive=()=>{};")
        out = run_freshness_node(
            "const PARTIAL = " + json.dumps(partial) + r""";
clock = 0;
const cached = {current_fix: {fix_seq: 5}, current_quality: {accepted: true}, revision: 1};
store.state.live = cached;
updateGlobalStatus(cached);
clock = 11000;
const result = applyTrackingMutation({ok: true, tracking_live: PARTIAL});
eq("metadata renewed", store.state.live.revision, PARTIAL.revision);
eq("fix reference kept", store.state.live.current_fix === cached.current_fix, true);
eq("age keeps counting", liveAgeMs(11000), 11000);
eq("lock stays", liveView(2000, 11000).link, "lost");
eq("delta consumed", result.tracking_live, undefined);
clock = 12000;
updateGlobalStatus({current_fix: {fix_seq: 6}, current_quality: {accepted: true}});
eq("full answer unlocks", liveView(2000, 12000).link, "live");
""", extra_prefixes=("let refreshFrameNotice=", "function applyTrackingMutation("), stubs=stubs)
        self.assertEqual(out["failures"], [])

    def test_dialog_guard_and_progress_poll_follow_the_data_age(self):
        guard = self.line_of("function guardStaleSubmit(")
        for token in ('liveView(livePollIntervalMs(MEASURE_ROUTE))', "liveDialogChecks.add(check)",
                      "liveDialogChecks.delete(check)", "liveLostText(null).action"):
            self.assertIn(token, guard)
        # The transport check runs before the position freshness.
        self.assertLess(guard.index("liveView("), guard.index("fixIsUsable("))
        with open(os.path.join(ROOT, "src", "40-tracking-actions.js"), encoding="utf-8") as source:
            actions = source.read().split("\n")
        self.assertIn('updateGlobalStatus(live);if(statusRefs&&statusRefs.lost&&currentRoute().path===MEASURE_ROUTE)updateStatus(live);',
                      actions[10])
        # A mutation only merges metadata and must never stamp the data age.
        mutation = self.line_of("function applyTrackingMutation(")
        self.assertIn("Object.assign({},store.state.live||{},delta)", mutation)
        self.assertNotIn("noteLiveAnswer", mutation)

    def test_pill_shows_fix_and_link_without_the_gate(self):
        section = APP_JS[APP_JS.index("function updateGlobalStatus("):APP_JS.index("function setHeader(")]
        self.assertTrue(section.startswith("function updateGlobalStatus(live){ingestFreshness(live);noteLiveAnswer(live);"))
        for token in ("pillLink(", "liveView(", "livePollIntervalMs(", "ui.link_latency", "dataset.link"):
            self.assertIn(token, section)
        self.assertNotIn("quality.accepted", section)
        self.assertNotIn("qual>0", section)
        self.assertNotIn("`RTCM ${", section)
        # The invisible "Updated" label no longer couples the connection state to the gate.
        self.assertIsNone(re.search(r"setConnection\(`\$\{localText\(recording", APP_JS))
        self.assertIn("/* M2 #21b:", APP_CSS)
        self.assertIn(".live-state[data-link]>span:first-child", APP_CSS)

    def test_pill_link_never_reads_the_gate_under_node(self):
        if NODE is None:
            self.skipTest("node is unavailable")
        self.assertNotIn("quality", self.line_of("function pillLink("))
        self.assertNotIn("qual", self.line_of("function pillLink("))
        out = run_freshness_node(r"""
eq("live", pillLink("live", false, false), {state: "live", cls: "ok", symbol: "●", word: "Live"});
eq("delayed", pillLink("delayed", false, false), {state: "delayed", cls: "warn", symbol: "◐", word: "Delayed"});
eq("connecting", pillLink("connecting", false, false),
  {state: "connecting", cls: "warn", symbol: "◐", word: "Connecting"});
eq("lost", pillLink("lost", false, false), {state: "lost", cls: "error", symbol: "○", word: "No data"});
eq("error", pillLink("live", true, false), {state: "error", cls: "error", symbol: "○", word: ""});
const stale = pillLink("live", false, true);
eq("stale position", [stale.cls, stale.symbol, stale.word], ["error", "●", "Live"]);
RtkI18n.setLanguage("de");
for (const link of ["live", "delayed", "lost", "connecting"])
  eq("de " + link, pillLink(link, false, false).word, dict.de["ui.link_" + link]);
eq("intervals", ["measure", "projects/p1", "projects", "device", "device/log"].map(livePollIntervalMs),
  [2000, 2000, 5000, 5000, 5000]);
""")
        self.assertEqual(out["failures"], [])

    def test_live_poll_interval_follows_the_new_routes(self):
        if NODE is None:
            self.skipTest("node is unavailable")
        out = run_freshness_node(r"""
eq("measure", livePollIntervalMs("measure"), 2000);
eq("project", livePollIntervalMs("projects/p1"), 2000);
eq("stakeout", livePollIntervalMs("projects/p1/stakeout"), 2000);
eq("field setup", livePollIntervalMs("measure/setup"), 5000);
eq("connections", livePollIntervalMs("device/connections"), 5000);
eq("library", livePollIntervalMs("projects"), 5000);
""")
        self.assertEqual(out["failures"], [])

    def test_freshness_text_and_lost_texts_under_node(self):
        if NODE is None:
            self.skipTest("node is unavailable")
        out = run_freshness_node(r"""
const en = dict.en, de = dict.de;
eq("no age", freshnessText(null, 0), "");
eq("age only", freshnessText(0, null), "Updated 0 s ago");
eq("floor", freshnessText(4999, 500), "Updated 4 s ago");
eq("fix age below the bound", freshnessText(1200, 2999), "Updated 1 s ago");
eq("fix age shown", freshnessText(1200, 3000), "Updated 1 s ago · position 3.0 s old");
const texts = [];
texts.push(liveLostText(12345), liveLostText(null));
eq("lost", liveLostText(12345), {title: "No status data from the device for 12 s",
  hint: en["ui.live_lost_hint"], action: en["ui.live_lost_action"]});
eq("never", liveLostText(null), {title: en["ui.live_never_title"], hint: en["ui.live_never_hint"],
  action: en["ui.live_lost_action"]});
RtkI18n.setLanguage("de");
eq("de age", freshnessText(4200, 3500), "Stand: vor 4 s · Position 3,5 s alt");
eq("de lost", liveLostText(12345), {title: de["ui.live_lost_title"].replace("{seconds}", "12"),
  hint: de["ui.live_lost_hint"], action: de["ui.live_lost_action"]});
eq("de never", liveLostText(null).title, de["ui.live_never_title"]);
texts.push(liveLostText(12345), liveLostText(null));
for (const text of texts.flatMap(item => [item.title, item.hint, item.action]))
  eq("clean " + text, /[{]|undefined|NaN/.test(text), false);
""")
        self.assertEqual(out["failures"], [])

    def test_announcements_speak_once_per_change_under_node(self):
        if NODE is None:
            self.skipTest("node is unavailable")
        out = run_freshness_node(r"""
eq("keys", ["readiness-hero ready", "readiness-hero blocked recording", "readiness-hero blocked stale",
  "readiness-hero stale-critical recording", "readiness-hero"].map(heroAnnouncementKey),
  ["ready", "blocked", "stale", "lost", null]);
const table = [[null, "ready", false], [null, "blocked", false], [null, "lost", true], ["ready", "ready", false],
  ["ready", "blocked", true], ["blocked", "lost", true], ["lost", "ready", true], ["lost", "connected", true],
  ["ready", "connected", false], ["connected", "ready", false], ["connected", "lost", true],
  ["stale", "ready", true], ["ready", null, false]];
for (const [last, key, speak] of table)
  eq("decision " + last + ">" + key, announcementDecision(last, key).speak, speak);
eq("null key keeps the last", announcementDecision("ready", null), {speak: false, key: "ready"});
eq("key returned", announcementDecision("ready", "blocked").key, "blocked");
const quiet = [[1000, null, null, false], [1000, 0, null, true], [3500, 0, 500, false], [2999, 0, 500, true],
  [9999, 0, null, true], [10000, 0, null, false], [5000, 0, -1, true]];
for (const [now, from, answer, expected] of quiet)
  eq("quiet " + [now, from, answer], announcementQuiet(now, from, answer), expected);
""")
        self.assertEqual(out["failures"], [])

    def test_announcer_sequences_under_node(self):
        if NODE is None:
            self.skipTest("node is unavailable")
        out = run_freshness_node(r"""
let count = 0;
const flush = (key, text, now) => { const spoke = flushAnnouncement(key, text, now); if (spoke) count++; return spoke; };
const reset = () => { announcedKey = null; announcedRoute = null; announceQuietFrom = null; count = 0; announcer.textContent = ""; route = "measure"; };
// 1. Start: route sync, first key silent, then one announcement per change.
reset();
eq("1 route sync", flush(null, "x", 500), false);
eq("1 first key", flush("ready", "Ready...", 1000), false);
["ready", "lost", "lost", "lost", "ready"].forEach((key, index) =>
  flush(key, key === "lost" ? "Lost..." : "Ready...", 2000 + index * 1000));
eq("1 count", count, 2);
eq("1 text", announcer.textContent, "Ready...");
// 2. Mute device from the start.
reset();
flush(null, "x", 1000); flush(null, "x", 2000);
eq("2 silent while loading", count, 0);
eq("2 lost", flush("lost", "No status data from the device yet. ...", 11000), true);
eq("2 text", announcer.textContent, "No status data from the device yet. ...");
eq("2 lost again", flush("lost", "again", 12000), false);
// 3. Tab return after an announced loss, device answers.
reset();
flush(null, "x", 1); flush("ready", "r", 2); flush("lost", "l", 3);
count = 0;
quietAnnouncements(20000);
eq("3 quiet lost", flush("lost", "l", 20500), false);
noteLiveAnswer({current_fix: {}}, 21000);
eq("3 quiet ready", flush("ready", "r", 21200), false);
eq("3 after 3 s", flush("ready", "r", 23500), false);
eq("3 later", flush("ready", "r", 30000), false);
eq("3 count", count, 0);
// 4. Tab return, device stays mute.
reset();
flush(null, "x", 1); flush("ready", "r", 2);
quietAnnouncements(40000);
for (let t = 40500; t < 50000; t += 500) flush("lost", "l", t);
eq("4 quiet until 10 s", count, 0);
eq("4 lost", flush("lost", "l", 50000), true);
eq("4 count", count, 1);
// 5. Navigation without a new answer.
reset();
flush(null, "x", 1); flush("ready", "r", 2); flush("lost", "l", 3);
count = 0;
route = "device";
eq("5 sync", flush("connected", "c", 11000), false);
eq("5 next tick", flush("connected", "c", 12000), false);
route = "measure";
eq("5 back sync", flush("lost", "l", 13000), false);
eq("5 recovery", flush("ready", "r", 14000), true);
eq("5 count", count, 1);
""")
        self.assertEqual(out["failures"], [])

    def test_live_loss_tops_the_hero_and_the_ticker_runs(self):
        # The freshness step runs after the core and the storage start or extras step (#29b E7).
        steps = self.line_of("function updateStatus(")
        self.assertGreater(steps.index("applyLiveFreshness(live);"), steps.index("renderStatusExtras(live);"))
        with open(os.path.join(ROOT, "src", "99-boot.js"), encoding="utf-8") as source:
            boot = source.read()
        for token in ("setInterval(()=>tickFreshness(),1000)", "noteLiveAttempt()", "quietAnnouncements()"):
            self.assertIn(token, boot)
        apply_source = self.line_of("function applyLiveFreshness(")
        for token in ("stale-critical", "liveLostText(", 'closest(".conditions")',
                      "statusRefs.actions.querySelectorAll(\"button"):
            self.assertIn(token, apply_source)
        tick_source = self.line_of("function tickFreshness(")
        for token in ("flushAnnouncement(", "liveDialogChecks"):
            self.assertIn(token, tick_source)
        self.assertNotIn("if(!store.state.live)return", tick_source)
        self.assertIn("const liveDialogChecks=new Set();", APP_JS)
        # Only the announcer is a live region for state changes: the resume notice has no status role and the
        # Wi-Fi error hint is a plain node.
        section = APP_JS[APP_JS.index("function renderStatusExtras("):APP_JS.index("async function startLine(")]
        self.assertIsNone(re.search(r'measurement-resume",role:"status"', section))
        self.assertNotIn("notice(localText(\"Wi-Fi error: \")", section)
        for token in (".readiness-hero.stale-critical", ".conditions.stale-critical", ".hero-freshness.aging"):
            self.assertIn(token, APP_CSS)

    def test_stale_latch_unlocks_only_on_a_larger_sequence(self):
        if NODE is None:
            self.skipTest("node is unavailable")
        declarations = []
        for prefix in ("let staleSinceSeq", "function fixIsUsable(", "function ingestFreshness("):
            lines = [line for line in APP_JS.split("\n") if line.startswith(prefix)]
            self.assertEqual(len(lines), 1, "%s must be one single-line declaration" % prefix)
            declarations.append(lines[0])
        script = r"""
const {ingestFreshness, fixIsUsable} = new Function('"use strict";' + process.argv[1] +
  "\nreturn {ingestFreshness, fixIsUsable};")();
const poll = (seq, stale, uptime) => {
  const live = {current_fix: {fix_seq: seq, stale, fix_age_ms: stale ? 4200 : 120},
                system: uptime === undefined ? undefined : {uptime_sec: uptime, boot_count: 0}};
  ingestFreshness(live);
  return live.fix_usable === fixIsUsable(live.current_fix) ? live.fix_usable : "inconsistent";
};
const out = {};
out.fresh = poll(5, false, 1000);
out.stale = poll(9, true, 1001);
out.sameSeqNotStale = poll(9, false, 1002);
out.sameSeqAgain = poll(9, false, 1003);
out.larger = poll(10, false, 1004);
out.staysUsable = poll(11, false, 1005);
// Delayed or reordered responses never clear or lower the latch.
poll(20, true, 1010);
out.olderFreshResponse = poll(15, false, 1008);
out.olderStaleResponse = poll(18, true, 1007);
out.stillLockedAtLatch = poll(20, false, 1011);
out.unlockedByLarger = poll(21, false, 1012);
// Reordering also lowers the uptime by a second or two: not a reboot.
poll(40, true, 1020);
out.slightlyOlderUptime = poll(5, false, 1018);
out.stillLockedAfterReorder = poll(40, false, 1021);
// A reboot is the only reset for a lower fix_seq: the live uptime drops.
poll(10000, true, 1030);
out.rebootStaleStillLocked = poll(10000, false, 1031);
out.counterRestartedWithoutReboot = poll(3, false, 1032);
out.counterRestartedAfterReboot = poll(3, false, 12);
out.afterRebootKeepsWorking = poll(4, false, 13);
// boot_count is zeroed 300 s after boot and must not be used as reboot signal.
poll(50, true, 14);
out.lockedBeforeBoot = poll(50, false, 15);
out.bootCountIgnored = (() => { const live = {current_fix: {fix_seq: 50, stale: false},
  system: {uptime_sec: 16, boot_count: 9}}; ingestFreshness(live); return live.fix_usable; })();
out.noUptimeNoReset = poll(3, false);
out.noFix = (() => { const live = {current_fix: null}; ingestFreshness(live); return live.fix_usable; })();
out.noLive = (() => { try { ingestFreshness(null); return true; } catch (e) { return false; } })();
out.noSequence = (() => { poll(1, true, 17); const live = {current_fix: {stale: false}};
  ingestFreshness(live); return live.fix_usable; })();
console.log(JSON.stringify(out));
"""
        run = subprocess.run([NODE, "-e", script, "\n".join(declarations)],
                             capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout), {
            "fresh": True, "stale": False,
            "sameSeqNotStale": False, "sameSeqAgain": False,
            "larger": True, "staysUsable": True,
            "olderFreshResponse": False, "olderStaleResponse": False,
            "stillLockedAtLatch": False, "unlockedByLarger": True,
            "slightlyOlderUptime": False, "stillLockedAfterReorder": False,
            "rebootStaleStillLocked": False, "counterRestartedWithoutReboot": False,
            "counterRestartedAfterReboot": True, "afterRebootKeepsWorking": True,
            "lockedBeforeBoot": False, "bootCountIgnored": False,
            "noUptimeNoReset": False,
            "noFix": False, "noLive": True, "noSequence": True})

    def test_expired_sequence_stays_stale_after_a_newer_fix_arrived(self):
        if NODE is None:
            self.skipTest("node is unavailable")
        declarations = []
        for prefix in ("let staleSinceSeq", "function fixIsUsable(", "function ingestFreshness("):
            declarations.append([line for line in APP_JS.split("\n")
                                 if line.startswith(prefix)][0])
        script = r"""
const {ingestFreshness} = new Function('"use strict";' + process.argv[1] +
  "\nreturn {ingestFreshness};")();
const poll = (seq, stale, uptime) => { const live = {
  current_fix: {fix_seq: seq, stale, fix_age_ms: stale ? 4200 : 120},
  system: {uptime_sec: uptime}}; ingestFreshness(live); return live.fix_usable; };
const out = {};
poll(99, false, 1000);
out.stale100 = poll(100, true, 1001);
out.fresh101 = poll(101, false, 1002);
out.delayedFresh100 = poll(100, false, 1001);
out.delayedFresh99 = poll(99, false, 1001);
out.fresh102 = poll(102, false, 1003);
out.delayedFresh101 = poll(101, false, 1002);
out.rebootResets = poll(3, false, 5);
console.log(JSON.stringify(out));
"""
        run = subprocess.run([NODE, "-e", script, "\n".join(declarations)],
                             capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout), {
            "stale100": False, "fresh101": True, "delayedFresh100": False,
            "delayedFresh99": False, "fresh102": True,
            "delayedFresh101": True, "rebootResets": True})

    def test_field_app_shows_gate_deviations_and_profiles(self):
        with open(os.path.join(ROOT, "app.js"), encoding="utf-8") as source:
            javascript = source.read()
        self.assertIn("function gateDeviationText(", javascript)
        self.assertIn("function gateLimitsLine(", javascript)
        self.assertIn("gate_deviations", javascript)
        self.assertIn("ui.gate_softened", javascript)
        self.assertIn("ui.gate_profile", javascript)
        self.assertIn("profiles[", javascript)

    def test_gate_helpers_and_profile_merge_under_node(self):
        if NODE is None:
            self.skipTest("node is unavailable")
        declarations = [line for line in APP_JS.split("\n") if line.startswith("const tr=")]
        for prefix in ("function gateDeviationText(", "function gateLimitFormat(",
                       "function gateLimitsLine(",
                       "function mergeMapDelta("):
            lines = [line for line in APP_JS.split("\n") if line.startswith(prefix)]
            self.assertEqual(len(lines), 1, "%s must be one single-line declaration" % prefix)
            declarations.append(lines[0])
        script = r"""
globalThis.RtkI18n = require(process.argv[2]);
const f = new Function('"use strict";' + process.argv[1] +
  "\nreturn {gateDeviationText, gateLimitsLine, mergeMapDelta};")();
const limits = {fix_quality: "RTK_FIXED", min_satellites: 10, max_hdop: 1.5,
  max_correction_age_sec: 5, max_gst_horizontal_sigma_m: 0.05, max_gst_vertical_sigma_m: 0.1,
  max_horizontal_spread_m: 0.05, quality_window_samples: 5};
const out = {};
out.deviations = f.gateDeviationText([
  {key: "required_fix", value: "DGPS", default: "RTK_FIXED"},
  {key: "max_hdop", value: 2, default: 1.5},
  {key: "max_gst_horizontal_sigma_m", value: 2, default: 0.05},
  {key: "quality_window_samples", value: 3, default: 5}]);
out.oneDeviation = f.gateDeviationText([{key: "max_hdop", value: 2, default: 1.5}]);
out.minSatellites = f.gateDeviationText([{key: "min_satellites", value: 6, default: 10}]);
out.noDeviations = [f.gateDeviationText([]), f.gateDeviationText(null), f.gateDeviationText(undefined)];
out.line = f.gateLimitsLine(limits);
out.lineRequiredFix = f.gateLimitsLine(Object.assign({}, limits, {fix_quality: undefined, required_fix: "DGPS"}));
out.lineFixQualityWins = f.gateLimitsLine(Object.assign({}, limits, {fix_quality: "RTK_FLOAT", required_fix: "DGPS"}));
out.lineFineLimit = f.gateLimitsLine(Object.assign({}, limits, {max_horizontal_spread_m: 0.003}));
out.lineEmpty = [f.gateLimitsLine(null), f.gateLimitsLine(undefined), f.gateLimitsLine({})];
const map = {revision: 1, lines: [], points: [], profiles: {a: {fix_quality: "RTK_FIXED"}}};
f.mergeMapDelta(map, {delta: true, revision: 2, profiles: {b: {fix_quality: "DGPS"}}, changes: []});
out.merged = [map.revision, Object.keys(map.profiles).sort(), map.profiles.a.fix_quality, map.profiles.b.fix_quality];
const bare = {revision: 1, lines: [], points: []};
f.mergeMapDelta(bare, {delta: true, revision: 2, profiles: {c: {fix_quality: "RTK_FLOAT"}}, changes: []});
out.createdProfiles = Object.keys(bare.profiles);
const noProfiles = {revision: 1, lines: [], points: [], profiles: {a: {fix_quality: "RTK_FIXED"}}};
f.mergeMapDelta(noProfiles, {delta: true, revision: 2, changes: []});
out.tolerated = [noProfiles.revision, Object.keys(noProfiles.profiles)];
const noKey = {revision: 1, lines: [], points: []};
f.mergeMapDelta(noKey, {delta: true, revision: 2, changes: []});
out.noKey = [noKey.revision, noKey.profiles];
console.log(JSON.stringify(out));
"""
        run = subprocess.run([NODE, "-e", script, "\n".join(declarations),
                              os.path.join(ROOT, "i18n.js")],
                             capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        result = json.loads(run.stdout)
        self.assertEqual(result["deviations"],
                         "required_fix DGPS > RTK_FIXED \u00b7 max_hdop 2 > 1.5 \u00b7 "
                         "max_gst_horizontal_sigma_m 2 > 0.05 \u00b7 quality_window_samples 3 < 5")
        self.assertEqual(result["oneDeviation"], "max_hdop 2 > 1.5")
        self.assertEqual(result["minSatellites"], "min_satellites 6 < 10")
        self.assertEqual(result["noDeviations"], ["", "", ""])
        line = ("RTK FIXED \u00b7 \u226510 sat \u00b7 HDOP \u22641.5 \u00b7 age \u22645 s \u00b7 "
                "H \u22640.05 m \u00b7 V \u22640.10 m \u00b7 spread \u22640.05 m \u00b7 window 5")
        self.assertEqual(result["line"], line)
        self.assertEqual(result["lineRequiredFix"], line.replace("RTK FIXED", "DGPS"))
        self.assertEqual(result["lineFixQualityWins"], line.replace("RTK FIXED", "RTK FLOAT"))
        self.assertEqual(result["lineFineLimit"], line.replace("spread \u22640.05", "spread \u22640.003"))
        self.assertEqual(result["lineEmpty"], ["", "", ""])
        self.assertEqual(result["merged"], [2, ["a", "b"], "RTK_FIXED", "DGPS"])
        self.assertEqual(result["createdProfiles"], ["c"])
        self.assertEqual(result["tolerated"], [2, ["a"]])
        self.assertEqual(result["noKey"], [2, None])

    def test_displayed_accuracy_uses_the_held_value_under_node(self):
        if NODE is None:
            self.skipTest("node is unavailable")
        for removed in ("heldAccuracyRows", "holdPendingAccuracy"):
            self.assertFalse(removed in APP_JS, "%s must be gone" % removed)
        result = run_gate_node(r"""
const own = {horizontal_sigma_m: 0.012, altitude_sigma_m: 0.034};
const held = {horizontal_sigma_m: 0.031, altitude_sigma_m: 0.047};
const base = {qual: 4, sats: 20, hdop: 0.6, fix_seq: 3, stale: false};
out.own = g.displayAccuracy(Object.assign({}, base, {receiver_accuracy: own}));
out.ownWinsWhilePending = g.displayAccuracy(Object.assign({}, base,
  {receiver_accuracy: own, pending_gst: true, held_receiver_accuracy: held}));
out.held = g.displayAccuracy(Object.assign({}, base, {pending_gst: true, held_receiver_accuracy: held}));
out.heldIgnoredWhenNotPending = g.displayAccuracy(Object.assign({}, base, {held_receiver_accuracy: held}));
out.pendingWithoutHeld = g.displayAccuracy(Object.assign({}, base, {pending_gst: true, held_receiver_accuracy: null}));
out.none = [g.displayAccuracy(null), g.displayAccuracy(undefined), g.displayAccuracy({})];
const row = fix => g.gateRows({current_fix: fix, current_quality: {accepted: true, reasons: [], limits},
  rtcm: {epoch_age_sec: 1}}).find(item => item.id === "accuracy");
out.rowHeld = row(Object.assign({}, base, {pending_gst: true, held_receiver_accuracy: held}));
out.rowOwn = row(Object.assign({}, base, {receiver_accuracy: own}));
out.rowBare = row(Object.assign({}, base, {held_receiver_accuracy: held}));
out.rowStale = row(Object.assign({}, base, {stale: true, pending_gst: true, held_receiver_accuracy: held}));
""")
        self.assertEqual(result["failures"], [])
        own = {"horizontal_sigma_m": 0.012, "altitude_sigma_m": 0.034}
        held = {"horizontal_sigma_m": 0.031, "altitude_sigma_m": 0.047}
        self.assertEqual(result["own"], own)
        self.assertEqual(result["ownWinsWhilePending"], own)
        self.assertEqual(result["held"], held)
        self.assertEqual(result["heldIgnoredWhenNotPending"], {})
        self.assertEqual(result["pendingWithoutHeld"], {})
        self.assertEqual(result["none"], [{}, {}, {}])
        self.assertTrue(result["rowHeld"]["detail"].startswith("H 0.031 m"))
        self.assertIn("V 0.047 m", result["rowHeld"]["detail"])
        self.assertTrue(result["rowOwn"]["detail"].startswith("H 0.012 m"))
        self.assertIn("V 0.034 m", result["rowOwn"]["detail"])
        self.assertTrue(result["rowBare"]["detail"].startswith("H \u2014 m"))
        # Stale rendering still wins: the held values stay visible but nothing passes.
        self.assertTrue(result["rowStale"]["detail"].startswith("H 0.031 m"))
        self.assertEqual(result["rowStale"]["state"], "fail")

    def test_field_app_edits_the_reference_frame(self):
        with open(os.path.join(ROOT, "app.js"), encoding="utf-8") as source:
            javascript = source.read()
        for needle in ("ui.reference_frame", "ui.reference_frame_help", "ui.reference_frame_epoch",
                       "reference_frame_epoch"):
            self.assertIn(needle, javascript)

    def test_field_app_shows_the_height_chain(self):
        for needle in ("/api/field-setup", "ui.antenna_height", "ui.antenna_height_help", "ui.height_ellipsoid",
                       "ui.geoid_separation", "ui.ground_height", "ui.antenna_height_zero",
                       "ui.antenna_height_missing", "antenna_height_mm", 'data-card="field-setup"'):
            self.assertIn(needle, APP_JS)

    def test_settings_cards_are_addressed_by_stable_selectors(self):
        # #24-UI: the method, rules and field setup cards live on the field setup page; no post-patch layer.
        self.assertNotIn("function refineSettings(", APP_JS)
        line = lambda prefix: next(item for item in APP_JS.split("\n") if item.startswith(prefix))
        mount = line("async function mountDisplay(")
        for card in ("language", "display", "measure-link"):
            self.assertIn('"data-card":"%s"' % card, mount)
        for card in ("measurement", "method", "gate", "field-setup", "session"):
            self.assertNotIn('"data-card":"%s"' % card, mount)
        self.assertNotIn("fieldSetupCard(", mount)
        self.assertIn('"data-card":"method"', line("function methodCard("))
        self.assertIn('"data-card":"gate"', line("function gateCard("))
        setup = line("async function mountFieldSetup(")
        self.assertIn('"field-setup-layout"', setup)
        for call in ("methodCard(", "gateCard(gate)", "fieldSetupCard(setup)"):
            self.assertIn(call, setup)

    def test_point_detail_height_rows_under_node(self):
        if NODE is None:
            self.skipTest("node is unavailable")
        declarations = [line for line in APP_JS.split("\n") if line.startswith("const tr=")]
        for prefix in ("function mmText(", "function heightRows("):
            lines = [line for line in APP_JS.split("\n") if line.startswith(prefix)]
            self.assertEqual(len(lines), 1, "%s must be one single-line declaration" % prefix)
            declarations.append(lines[0])
        script = r"""
const f = new Function('"use strict";' + process.argv[1] + "\nreturn {heightRows, mmText};")();
const point = (alt, measurement) => ({coordinate: [1, 2, alt], measurement});
const out = {};
const compact = result => ({rows: result.rows.map(row => row.value), hint: result.hint});
out.normal = compact(f.heightRows(point(512345, {geoid_sep_mm: 47100, alt_ellipsoid_mm: 559445, antenna_height_mm: 1800})));
out.zero = compact(f.heightRows(point(512345, {geoid_sep_mm: 47100, alt_ellipsoid_mm: 559445, antenna_height_mm: 0})));
out.missing = compact(f.heightRows(point(512345, {geoid_sep_mm: 47100, alt_ellipsoid_mm: 559445})));
out.nullAntenna = compact(f.heightRows(point(512345, {antenna_height_mm: null})));
out.geoidNull = compact(f.heightRows(point(512345, {geoid_sep_mm: null, alt_ellipsoid_mm: null, antenna_height_mm: 1800})));
out.negative = compact(f.heightRows(point(-1500, {geoid_sep_mm: -2500, alt_ellipsoid_mm: -4000, antenna_height_mm: 1800})));
out.belowOne = compact(f.heightRows(point(-5, {geoid_sep_mm: -7, alt_ellipsoid_mm: 5, antenna_height_mm: 0})));
out.noMeasurement = compact(f.heightRows({coordinate: [1, 2]}));
out.labels = f.heightRows(point(0, {})).rows.map(row => row.label);
console.log(JSON.stringify(out));
"""
        run = subprocess.run([NODE, "-e", script, "\n".join(declarations)],
                             capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        result = json.loads(run.stdout)
        self.assertEqual(result["labels"], [
            "Height (MSL, phase centre)", "Ellipsoid height", "Geoid separation",
            "Antenna height above the ground point", "Ground height (MSL)"])
        zero_hint = "Antenna height configured as 0; the ground height equals the phase-centre height."
        missing_hint = "No antenna height was recorded with this point (older firmware); no ground height."
        # Ground height is subtracted in integer millimetres, then formatted.
        self.assertEqual(result["normal"], {
            "rows": ["512.345 m", "559.445 m", "47.100 m", "1.800 m", "510.545 m"], "hint": None})
        self.assertEqual(result["zero"], {
            "rows": ["512.345 m", "559.445 m", "47.100 m", "0.000 m", "512.345 m"], "hint": zero_hint})
        self.assertEqual(result["missing"], {
            "rows": ["512.345 m", "559.445 m", "47.100 m", None, None], "hint": missing_hint})
        self.assertEqual(result["nullAntenna"], {
            "rows": ["512.345 m", None, None, None, None], "hint": missing_hint})
        self.assertEqual(result["geoidNull"], {
            "rows": ["512.345 m", None, None, "1.800 m", "510.545 m"], "hint": None})
        self.assertEqual(result["negative"], {
            "rows": ["-1.500 m", "-4.000 m", "-2.500 m", "1.800 m", "-3.300 m"], "hint": None})
        self.assertEqual(result["belowOne"], {
            "rows": ["-0.005 m", "0.005 m", "-0.007 m", "0.000 m", "-0.005 m"], "hint": zero_hint})
        self.assertEqual(result["noMeasurement"], {"rows": [None] * 5, "hint": missing_hint})

    def test_field_app_shows_utc_timestamps(self):
        for needle in ("recorded_at_utc", "clock_unsynced", "ui.recorded_utc", "ui.clock_unsynced"):
            self.assertIn(needle, APP_JS)
        # The raw seconds since 2000 are never read by the field app.
        self.assertIsNone(re.search(r"recorded_at(?!_utc)", APP_JS))

    def test_recorded_utc_helper_under_node(self):
        if NODE is None:
            self.skipTest("node is unavailable")
        declarations = [line for line in APP_JS.split("\n") if line.startswith("const tr=")]
        lines = [line for line in APP_JS.split("\n") if line.startswith("function recordedUtc(")]
        self.assertEqual(len(lines), 1, "recordedUtc must be one single-line declaration")
        declarations.append(lines[0])
        script = r"""
const f = new Function('"use strict";' + process.argv[1] + "\nreturn {recordedUtc};")();
const out = {};
out.synced = f.recordedUtc({recorded_at: 841057764, recorded_at_utc: "2026-08-26T11:09:24Z", clock_unsynced: false});
out.unsynced = f.recordedUtc({recorded_at: 12, recorded_at_utc: "2000-01-01T00:00:12Z", clock_unsynced: true});
out.unsyncedInvalid = f.recordedUtc({recorded_at: -1, recorded_at_utc: null, clock_unsynced: true});
out.nullFields = f.recordedUtc({recorded_at_utc: null, clock_unsynced: false});
out.legacy = f.recordedUtc({precision: "e8"});
out.empty = f.recordedUtc({});
out.none = f.recordedUtc(null);
out.undef = f.recordedUtc(undefined);
out.truthyNotTrue = f.recordedUtc({recorded_at_utc: "2026-08-26T11:09:24Z", clock_unsynced: "yes"});
console.log(JSON.stringify(out));
"""
        run = subprocess.run([NODE, "-e", script, "\n".join(declarations)],
                             capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        result = json.loads(run.stdout)
        warning = "Clock was not synchronised when this point was recorded"
        self.assertEqual(result["synced"], {
            "label": "Recorded (UTC)", "text": "2026-08-26T11:09:24Z", "unsynced": False, "warning": None})
        # An unsynchronised point shows its year-2000 text and the warning.
        self.assertEqual(result["unsynced"], {
            "label": "Recorded (UTC)", "text": "2000-01-01T00:00:12Z", "unsynced": True, "warning": warning})
        # An invalid timestamp has no text but is still flagged.
        self.assertEqual(result["unsyncedInvalid"], {
            "label": "Recorded (UTC)", "text": None, "unsynced": True, "warning": warning})
        for key in ("nullFields", "legacy", "empty", "none", "undef"):
            self.assertIsNone(result[key], key)
        self.assertFalse(result["truthyNotTrue"]["unsynced"])
        # The raw seconds never reach the rendered values.
        for key in ("synced", "unsynced", "unsyncedInvalid"):
            self.assertNotIn("841057764", json.dumps(result[key]))

    def test_field_app_warns_about_mixed_frames(self):
        for needle in ("mixed_frames", "unknown_frames", "ui.frame_mix_warning", "ui.frame_unknown_warning",
                       "ui.frame_mix_hint", '"data-notice","frames"'):
            self.assertIn(needle, APP_JS)
        self.assertIn('==="DGPS"', APP_JS.replace(" ", ""))
        # Both views read the project objects of /api/tracking, which carry the frame fields.
        self.assertIn("tracking.projects.flatMap(project=>[projectCard(project,tracking),frameNotice(project)])", APP_JS)
        self.assertIn("frameSlot,\n    projectIsActive", APP_JS)

    def test_frame_warnings_and_dgps_hint_under_node(self):
        if NODE is None:
            self.skipTest("node is unavailable")
        declarations = [line for line in APP_JS.split("\n") if line.startswith("const tr=")]
        for prefix in ("function frameTallyText(", "function frameNoticeInfo(", "function frameMixHint(",
                       "function fieldHelp("):
            lines = [line for line in APP_JS.split("\n") if line.startswith(prefix)]
            self.assertEqual(len(lines), 1, "%s must be one single-line declaration" % prefix)
            declarations.append(lines[0])
        script = r"""
globalThis.RtkI18n = require(process.cwd() + "/i18n.js");
const h = (tag, attrs) => { const node = {tag, attrs: attrs || {}, listeners: {}, hidden: false, textContent: "",
  addEventListener(name, fn) { (this.listeners[name] = this.listeners[name] || []).push(fn); }}; return node; };
const f = new Function("h", "localText", '"use strict";' + process.argv[1] +
  "\nreturn {frameTallyText, frameNoticeInfo, frameMixHint, fieldHelp};");
// A localText that marks its input, so a text translated twice shows up.
const api = f(h, value => "[L]" + value);
const out = {};
out.tally = api.frameTallyText({"WGS84/ITRF": 1, "ETRS89 (synthetic)": 2});
out.tieBreak = api.frameTallyText({B: 2, A: 2, unknown: 5});
out.empty = [api.frameTallyText({}), api.frameTallyText(undefined)];
out.escape = api.frameTallyText({"A&B <x>": 1, C: 1});
const mixed = {mixed_frames: true, unknown_frames: false, frames: {"WGS84/ITRF": 1, "ETRS89 (synthetic)": 2}};
out.mixed = api.frameNoticeInfo(mixed);
out.mixedWithUnknown = api.frameNoticeInfo({mixed_frames: true, unknown_frames: false, frames: {unknown: 3, "WGS84/ITRF": 1}});
out.unknown = api.frameNoticeInfo({mixed_frames: false, unknown_frames: true, frames: {unknown: 4}});
out.single = api.frameNoticeInfo({mixed_frames: false, unknown_frames: false, frames: {"ETRS89 (synthetic)": 3}});
out.noFields = [api.frameNoticeInfo({}), api.frameNoticeInfo(null), api.frameNoticeInfo(undefined)];
out.escapeNotice = api.frameNoticeInfo({mixed_frames: true, frames: {"A&B": 1, C: 1}}).text;
out.hint = ["RTK_FIXED", "RTK_FLOAT", "DGPS", undefined, "dgps"].map(value => api.frameMixHint(value));
// The dialog help follows the select value.
// The fake select records the listeners fieldHelp registers on it.
const input = h("select"); input.value = "RTK_FIXED";
const note2 = api.fieldHelp({name: "required_fix", help: api.frameMixHint, helpKey: "frame-mix"}, input);
const dispatch = (name, value) => { input.value = value; (input.listeners[name] || []).forEach(fn => fn()); return [note2.hidden, note2.textContent]; };
out.dialog = {key: note2.attrs["data-hint"], initial: [note2.hidden, note2.textContent]};
out.dialog.toDgps = dispatch("change", "DGPS");
out.dialog.back = dispatch("change", "RTK_FLOAT");
out.dialog.inputEvent = dispatch("input", "DGPS");
out.dialog.registered = Object.keys(input.listeners).sort();
const plain = api.fieldHelp({name: "x", help: "static"}, input);
out.plain = [plain.tag, plain.attrs.text];
out.none = api.fieldHelp({name: "x"}, input);
globalThis.RtkI18n.setLanguage("de");
out.deMixed = api.frameNoticeInfo(mixed).text;
out.deUnknown = api.frameNoticeInfo({unknown_frames: true}).text;
out.deHint = api.frameMixHint("DGPS");
out.deTally = api.frameTallyText({unknown: 5, "ETRS89 (synthetic)": 2});
out.deMixedUnknown = api.frameNoticeInfo({mixed_frames: true, frames: {unknown: 3, "WGS84/ITRF": 1}}).text;
const deInput = h("select"); deInput.value = "RTK_FIXED";
const deNote = api.fieldHelp({name: "required_fix", help: api.frameMixHint, helpKey: "frame-mix"}, deInput);
deInput.value = "DGPS"; (deInput.listeners.change || []).forEach(fn => fn());
out.deDialog = [deNote.hidden, deNote.textContent];
out.de = globalThis.RtkI18n.dictionaries.de;
console.log(JSON.stringify(out));
"""
        run = subprocess.run([NODE, "-e", script, "\n".join(declarations)],
                             capture_output=True, text=True, timeout=10, cwd=ROOT)
        self.assertEqual(run.returncode, 0, run.stderr)
        result = json.loads(run.stdout)
        mix_text = "This project contains points in more than one reference frame: %s"
        hint = ("DGPS fixes are in WGS84/ITRF, RTK fixes in the correction service's frame "
                "(about 0.7 to 0.9 m apart in Europe). Accepting DGPS mixes frames within a project.")
        # Order: descending count, then name; entries are "frame count" joined by " · ".
        self.assertEqual(result["tally"], "ETRS89 (synthetic) 2 \u00b7 WGS84/ITRF 1")
        self.assertEqual(result["tieBreak"], "unknown 5 \u00b7 A 2 \u00b7 B 2")
        self.assertEqual(result["empty"], ["", ""])
        self.assertEqual(result["escape"], "A&B <x> 1 \u00b7 C 1")
        self.assertEqual(result["mixed"], {"kind": "mix", "text": mix_text % "ETRS89 (synthetic) 2 \u00b7 WGS84/ITRF 1"})
        # Mixed wins over unknown, even when "unknown" is one of the keys; never both warnings.
        self.assertEqual(result["mixedWithUnknown"],
                         {"kind": "mix", "text": mix_text % "unknown 3 \u00b7 WGS84/ITRF 1"})
        self.assertEqual(result["unknown"], {
            "kind": "unknown",
            "text": "The reference frame of this project's RTK points is unknown. The field setup applies to "
                    "new measurements only; assign a frame to the existing points in the project."})
        self.assertIsNone(result["single"])
        self.assertEqual(result["noFields"], [None, None, None])
        self.assertEqual(result["escapeNotice"], mix_text % "A&B 1 \u00b7 C 1")
        self.assertEqual(result["hint"], ["", "", hint, "", ""])
        dialog = result["dialog"]
        self.assertEqual(dialog["key"], "frame-mix")
        self.assertEqual(dialog["initial"], [True, ""])
        self.assertEqual(dialog["toDgps"], [False, hint])
        self.assertEqual(dialog["back"], [True, ""])
        self.assertEqual(dialog["inputEvent"], [False, hint])
        self.assertEqual(dialog["registered"], ["change", "input"])
        self.assertEqual(result["plain"], ["small", "static"])
        self.assertEqual(result["none"], [])
        german = result["de"]
        self.assertEqual(result["deMixed"], german["ui.frame_mix_warning"].replace(
            "{frames}", "ETRS89 (synthetic) 2 \u00b7 WGS84/ITRF 1"))
        self.assertEqual(result["deUnknown"], german["ui.frame_unknown_warning"])
        self.assertEqual(result["deHint"], german["ui.frame_mix_hint"])
        self.assertNotEqual(german["ui.frame_mix_hint"], hint)
        # The tally key "unknown" is translated for display only; exports keep "unknown".
        self.assertEqual(german["ui.frame_unknown_key"], "unbekannt")
        self.assertEqual(result["deTally"], "unbekannt 5 \u00b7 ETRS89 (synthetic) 2")
        self.assertEqual(result["deMixedUnknown"], german["ui.frame_mix_warning"].replace(
            "{frames}", "unbekannt 3 \u00b7 WGS84/ITRF 1"))
        # The modal help text is translated exactly once (by tr): no localText over it.
        self.assertEqual(result["deDialog"], [False, german["ui.frame_mix_hint"]])
        self.assertNotIn("[L]", json.dumps(result["dialog"]))

    def test_tracking_mutation_refreshes_the_open_project_frame_notice_under_node(self):
        if NODE is None:
            self.skipTest("node is unavailable")
        lines = [line for line in APP_JS.split("\n")
                 if line.startswith(("const MEASURE_ROUTE=", "let refreshFrameNotice=", "function applyTrackingMutation("))]
        self.assertEqual(len(lines), 3)
        script = r"""
const seen = [], store = {state: {live: {a: 1}}, set(patch) { Object.assign(this.state, patch); }};
const f = new Function("store", "updateGlobalStatus", "currentRoute", "statusRefs", "updateStatus", "invalidateLive",
  '"use strict";' + process.argv[1] +
  "\nreturn {apply: applyTrackingMutation, set(fn) { refreshFrameNotice = fn; }};");
const api = f(store, () => {}, () => ({path: "projects/p1"}), null, () => {}, () => {});
const out = {};
out.noPage = api.apply({tracking_live: {active_project: {id: "p1", mixed_frames: true}}}) && "ok";
api.set(live => seen.push(live.active_project));
const result = api.apply({tracking_live: {active_project: {id: "p1", mixed_frames: true, frames: {A: 1, B: 1}}}, other: 1});
out.result = result;
out.seen = seen;
api.apply({other: 2});
out.afterPlain = seen.length;
console.log(JSON.stringify(out));
"""
        run = subprocess.run([NODE, "-e", script, "\n".join(lines)], capture_output=True, text=True, timeout=10, cwd=ROOT)
        self.assertEqual(run.returncode, 0, run.stderr)
        result = json.loads(run.stdout)
        self.assertEqual(result["seen"], [{"id": "p1", "mixed_frames": True, "frames": {"A": 1, "B": 1}}])
        self.assertEqual(result["result"], {"other": 1})
        self.assertEqual(result["afterPlain"], 1)

    FRAME_UI_PREFIXES = (
        "const tr=", "const V2_DE=", "function localText(", "function capacityText(", "function frameTallyText(",
        "function frameNoticeInfo(", "function frameNotice(", "const FRAME_RELOAD_MS=",
        "function frameAssignmentInfo(", "function frameAssignmentLabel(",
        "function frameAssignmentPreviewText(", "function frameAssignmentControls(",
        "function frameNoticeSync(")
    # A fake DOM for the frame assignment node tests: nodes are plain {tag, attrs, children} objects.
    FRAME_UI_PRELUDE = r"""
globalThis.RtkI18n = require(process.cwd() + "/i18n.js");
const h = (tag, attrs, ...children) => ({tag, attrs: attrs || {}, children: children.flat(Infinity)});
const iconButton = (label, handler, kind) => ({tag: "button", attrs: {}, label, kind, handler,
  setAttribute(key, value) { this.attrs[key] = value; }});
const notice = (message, kind) => ({tag: "notice", attrs: {}, message, kind,
  setAttribute(key, value) { this.attrs[key] = value; }});
const replace = (target, ...children) => { target.children = children.flat(Infinity); };
// One string per control: a notice's kind, a button's action or label, the assignment row's text and button.
const summary = parts => [parts].flat(Infinity).map(node => {
  if (node.tag === "notice") return "notice:" + node.attrs["data-kind"];
  if (node.tag === "button") return "button:" + (node.attrs["data-action"] || node.label);
  return "row:" + node.children[0].attrs.textRaw + "|" + node.children[1].label;
});
const NAMES = ["h", "iconButton", "notice", "replace", "assignReferenceFrame", "revokeReferenceFrame", "api",
  "PROJECT_READ_TIMEOUT", "Date", "setTimeout", "clearTimeout"];
const make = (...stubs) => new Function(...NAMES, '"use strict";' + process.argv[1] +
  "\nreturn {frameAssignmentInfo, frameAssignmentPreviewText, frameAssignmentControls, frameNoticeSync};")(
  h, iconButton, notice, replace, () => {}, () => {}, ...stubs);
"""

    def run_frame_ui_node(self, body):
        declarations = "\n".join(self.line_of(prefix) for prefix in self.FRAME_UI_PREFIXES)
        script = self.FRAME_UI_PRELUDE + "(async () => {\n" + body + "\n})().catch(error => { console.error(error); process.exit(1); });"
        run = subprocess.run([NODE, "-e", script, declarations], capture_output=True, text=True, timeout=20, cwd=ROOT)
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout)

    def i18n_json(self, expression):
        script = "console.log(JSON.stringify(require('./i18n.js').%s));" % expression
        return json.loads(subprocess.run([NODE, "-e", script], capture_output=True, text=True,
                                         check=True, cwd=ROOT).stdout)

    def test_frame_assignment_is_wired_into_the_project_view(self):
        with open(os.path.join(ROOT, "src", "72-frame-assignment.js"), encoding="utf-8") as source:
            component = source.read()
        with open(os.path.join(ROOT, "src", "70-project-detail.js"), encoding="utf-8") as source:
            detail = source.read()
        self.assertTrue(component.startswith("// SPDX-License-Identifier: AGPL-3.0-only\n"))
        self.assertTrue(component.endswith("\n"))
        # The project view carries the controls and follows live and reloaded state; the library keeps the notice.
        self.assertIn('h("div",{class:"frame-notice-slot"},frameAssignmentControls(project))', detail)
        self.assertIn("syncFrameNotice=frameNoticeSync(project,frameSlot,signal)", detail)
        self.assertNotIn("frameNotice(active)", detail)
        self.assertIn("frameSlot,\n    projectIsActive", APP_JS)
        self.assertIn("tracking.projects.flatMap(project=>[projectCard(project,tracking),frameNotice(project)])", APP_JS)
        self.assertIn("replace(slot,frameAssignmentControls(Object.assign({},project,active)))", component)
        for needle in ('trackingAction("frame_assignment_preview"', 'trackingAction("assign_reference_frame"',
                       'trackingAction("revoke_reference_frame"', "expected_count:preview.total",
                       "scope_token:preview.scope_token", "assignment_token:project.frame_assignment.token",
                       '"data-action","assign-frame"'):
            self.assertIn(needle, component, needle)
        # The revoke body names the shown assignment by its fingerprint only (E6).
        self.assertNotIn("assigned_at", component.replace("assigned_at_utc", ""))
        # Every function of the component is one line, as the node extraction needs.
        names = re.findall(r"^(?:async )?function (\w+)\(", component, re.M)
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(len(names), len(re.findall(r"function \w+\(", component)))
        dictionaries = self.i18n_json("dictionaries")
        keys = ("ui.frame_unknown_warning", "ui.frame_assign_action", "ui.frame_assign_title",
                "ui.frame_assign_submit", "ui.frame_revoke_action", "ui.frame_revoke_title")
        for key in keys:
            self.assertIn(key, dictionaries["en"], key)
            self.assertIn(key, dictionaries["de"], key)
        for key in keys[1:]:
            self.assertIn('"%s"' % key, component, key)
        self.assertEqual(
            dictionaries["en"]["ui.frame_unknown_warning"],
            "The reference frame of this project's RTK points is unknown. The field setup applies to new "
            "measurements only; assign a frame to the existing points in the project.")
        # The German wording lives only in i18n.js: every text is translated and no two coincide.
        german = [dictionaries["de"][key] for key in keys]
        for key, text in zip(keys, german):
            self.assertTrue(text.strip(), key)
            self.assertNotEqual(text, dictionaries["en"][key], key)
        self.assertEqual(len(set(german)), len(keys), german)
        v2de = self.i18n_json("v2de")
        for message in (
                "The points of this project changed during the check; review the assignment again.",
                "The frame assignment of this project changed meanwhile; check it again.",
                "This project has no frame assignment.",
                "Pause the line of this project before assigning a reference frame.",
                "A reference frame is required.",
                'The frame names "unknown" and "mixed" are reserved.',
                "The frame assignment of this project changed during the export; export again.",
                "This project has no RTK points without a reference frame."):
            self.assertIn(message, v2de, message)

    def test_frame_assignment_texts_under_node(self):
        if NODE is None:
            self.skipTest("node is unavailable")
        result = self.run_frame_ui_node(r"""
const api = make();
const out = {};
const assignment = {reference_frame: "ETRS89 <b>", reference_frame_epoch: "2010.0",
  assigned_at_utc: "2026-10-27T10:00:00Z", count: 6, token: "0123456789abcdef"};
const emptied = {reference_frame: "X", reference_frame_epoch: null, assigned_at_utc: "2026-10-27T10:00:00Z",
  count: 0, token: "0123456789abcdef"};
const projects = {
  unknown: {frames: {unknown: 7}, unknown_frames: true},
  assigned: {frames: {"ETRS89 <b>": 6, unknown: 1}, unknown_frames: true, frame_assignment: assignment},
  known: {frames: {A: 3}},
  nulled: {frames: {A: 3}, frame_assignment: null},
  emptied: {frames: {}, frame_assignment: emptied}};
out.canAssign = {}; out.row = {}; out.controls = {};
for (const [name, project] of Object.entries(projects)) {
  const info = api.frameAssignmentInfo(project);
  out.canAssign[name] = info.canAssign;
  out.row[name] = info.rowText || null;
  out.controls[name] = summary(api.frameAssignmentControls(project));
}
const preview = {total: 12, new_unknown: 12, points: 1, other_unknown: 134, more_lines: 2, assignment: null,
  lines: [1, 2, 3, 4, 5].map(i => ({id: "l" + i, name: i === 3 ? null : "Line " + i, count: i}))};
const quiet = {total: 3, new_unknown: 3, points: 0, other_unknown: 0, more_lines: 0, assignment: null,
  lines: [{id: "l1", name: "Only", count: 3}]};
const replacing = Object.assign({}, quiet, {total: 8, new_unknown: 2, assignment: assignment,
  lines: [{id: "l1", name: "Only", count: 8}]});
out.preview = api.frameAssignmentPreviewText(preview);
out.quiet = api.frameAssignmentPreviewText(quiet);
out.replacing = api.frameAssignmentPreviewText(replacing);
globalThis.RtkI18n.setLanguage("de");
out.deRow = api.frameAssignmentInfo(projects.assigned).rowText;
out.deNoEpoch = api.frameAssignmentInfo(projects.emptied).rowText;
out.dePreview = api.frameAssignmentPreviewText(preview);
out.deQuiet = api.frameAssignmentPreviewText(quiet);
out.deReplacing = api.frameAssignmentPreviewText(replacing);
out.deControls = summary(api.frameAssignmentControls(projects.assigned));
out.revokeLabel = globalThis.RtkI18n.dictionaries.de["ui.frame_revoke_action"];
console.log(JSON.stringify(out));
""")
        self.assertEqual(result["canAssign"], {"unknown": True, "assigned": True, "known": False,
                                               "nulled": False, "emptied": True})
        for name in ("unknown", "known", "nulled"):
            self.assertIsNone(result["row"][name], name)
        # Frame names are user text: shown as typed, never escaped or reworded.
        row = "Assigned afterwards: ETRS89 <b>, epoch 2010.0 for 6 points (2026-10-27)"
        empty_row = "Assigned afterwards: X for 0 points (2026-10-27)"
        self.assertEqual(result["row"]["assigned"], row)
        self.assertEqual(result["row"]["emptied"], empty_row)
        self.assertEqual(result["controls"]["unknown"], ["notice:unknown", "button:assign-frame"])
        self.assertEqual(result["controls"]["assigned"],
                         ["notice:unknown", "button:assign-frame", "row:%s|Undo assignment" % row])
        # An assignment emptied by undo stays visible and revocable (ruling).
        self.assertEqual(result["controls"]["emptied"],
                         ["button:assign-frame", "row:%s|Undo assignment" % empty_row])
        self.assertEqual(result["controls"]["known"], [])
        self.assertEqual(result["controls"]["nulled"], [])
        keep = ("Points with a recorded frame keep it. Coordinates are not transformed; the frame you enter is "
                "your statement about the correction service used then.")
        first = "%d RTK points of this project have no recorded reference frame (%s)."
        self.assertEqual(
            result["preview"],
            first % (12, "Line 1: 1, Line 2: 2, l3: 3, Line 4: 4, Line 5: 5, 2 more lines, object points: 1")
            + " 134 points without an RTK solution keep the frame “unknown”. " + keep)
        self.assertEqual(result["quiet"], first % (3, "Only: 3") + " " + keep)
        self.assertEqual(
            result["replacing"],
            first % (8, "Only: 8") + " Replaces the current assignment ETRS89 <b>, epoch 2010.0 (6 points); "
            "2 of the points are not assigned yet. " + keep)
        v2de = self.i18n_json("v2de")

        def german(message, **values):
            text = v2de[message]
            for key, value in values.items():
                text = text.replace("{%s}" % key, str(value))
            return text

        label = german("{frame}, epoch {epoch}", frame="ETRS89 <b>", epoch="2010.0")
        date_row = "Assigned afterwards: {frame} for {count} points ({date})"
        self.assertEqual(result["deRow"], german(date_row, frame=label, count=6, date="2026-10-27"))
        self.assertEqual(result["deNoEpoch"], german(date_row, frame="X", count=0, date="2026-10-27"))
        self.assertNotEqual(result["deRow"], row)
        english_first = "{count} RTK points of this project have no recorded reference frame ({parts})."
        parts = ", ".join(["Line 1: 1", "Line 2: 2", "l3: 3", "Line 4: 4", "Line 5: 5",
                           german("{count} more lines", count=2), german("object points: {count}", count=1)])
        self.assertEqual(
            result["dePreview"],
            german(english_first, count=12, parts=parts) + " "
            + german("{count} points without an RTK solution keep the frame “unknown”.", count=134)
            + " " + v2de[keep])
        self.assertEqual(result["deQuiet"], german(english_first, count=3, parts="Only: 3") + " " + v2de[keep])
        self.assertEqual(
            result["deReplacing"],
            german(english_first, count=8, parts="Only: 8") + " "
            + german("Replaces the current assignment {frame} ({count} points); {new} of the points are not "
                     "assigned yet.", frame=label, count=6, new=2) + " " + v2de[keep])
        self.assertEqual(result["deControls"], ["notice:unknown", "button:assign-frame",
                                                "row:%s|%s" % (result["deRow"], result["revokeLabel"])])

    def test_frame_assignment_follows_live_updates_under_node(self):
        if NODE is None:
            self.skipTest("node is unavailable")
        result = self.run_frame_ui_node(r"""
const api = make();
const controls = project => summary(api.frameAssignmentControls(project));
const assigned = (count, token) => ({reference_frame: "F", reference_frame_epoch: null,
  assigned_at_utc: "2026-10-27T10:00:00Z", count, token});
const library = {id: "p", frames: {unknown: 7}, unknown_frames: true, mixed_frames: false};
const out = {};
// (a) another client assigned a frame: the library object has none, the live object has one.
out.a = controls(Object.assign({}, library, {frames: {F: 7}, unknown_frames: false, frame_assignment: assigned(7, "a")}));
// (b) another client revoked it: the live key null overwrites the assignment the object had.
const had = Object.assign({}, library, {frames: {F: 7}, unknown_frames: false, frame_assignment: assigned(7, "a")});
out.bBefore = controls(had);
out.b = controls(Object.assign({}, had, {frames: {unknown: 7}, unknown_frames: true, frame_assignment: null}));
// (c) an undo shrank the scope: the row follows the live count.
out.c = controls(Object.assign({}, had, {frame_assignment: assigned(5, "b")}));
console.log(JSON.stringify(out));
""")
        row = "row:Assigned afterwards: F for %d points (2026-10-27)|Undo assignment"
        self.assertEqual(result["a"], ["button:assign-frame", row % 7])
        self.assertEqual(result["bBefore"], result["a"])
        self.assertEqual(result["b"], ["notice:unknown", "button:assign-frame"])
        self.assertEqual(result["c"], ["button:assign-frame", row % 5])

    def test_a_shown_inactive_project_reloads_its_assignment_under_node(self):
        if NODE is None:
            self.skipTest("node is unavailable")
        result = self.run_frame_ui_node(r"""
let clock = 0, release = null;
const timers = [], calls = [], responses = [];
const apiStub = {json: url => { calls.push(url); return responses.shift()(); }};
const respond = projects => () => Promise.resolve({projects});
const hang = () => new Promise(resolve => { release = () => resolve({projects: [{id: "A", frames: {unknown: 7}, unknown_frames: true}]}); });
const pending = () => timers.filter(timer => !timer.cancelled);
const fire = () => { for (const timer of timers.splice(0)) { if (timer.cancelled) continue; if (timer.at <= clock) timer.fn(); else timers.push(timer); } };
const tick = () => new Promise(resolve => setImmediate(resolve));
const api = make(apiStub, 20000, {now: () => clock},
  (fn, ms) => { const timer = {fn, at: clock + ms, cancelled: false}; timers.push(timer); return timer; },
  timer => { if (timer) timer.cancelled = true; });
const assigned = count => ({reference_frame: "F", reference_frame_epoch: null, assigned_at_utc: "2026-10-27T10:00:00Z",
  count, token: "aaaaaaaaaaaaaaaa"});
const project = {id: "A", frames: {unknown: 7}, unknown_frames: true, mixed_frames: false};
const slot = {children: []};
const controller = new AbortController();
const sync = api.frameNoticeSync(project, slot, controller.signal);
const live = (revision, id, extra) => ({revision, active_project: Object.assign(
  {id, frames: {F: 5}, unknown_frames: false, mixed_frames: false, frame_assignment: null}, extra || {})});
const out = {};
// (a) the first live state only records its revision.
sync(live(10, "B")); await tick();
out.a = [calls.length, slot.children.length];
// (b) a higher revision while another project is active reloads the shown project's summary.
clock = 11000;
responses.push(respond([{id: "B"}, {id: "A", frames: {unknown: 1, F: 6}, unknown_frames: true, mixed_frames: true,
  frame_assignment: assigned(6)}]));
sync(live(11, "B")); await tick();
out.b = [calls.length, calls[0], summary(slot.children), project.frame_assignment.count];
// (c) the same revision again changes nothing.
sync(live(11, "B")); await tick();
out.c = calls.length;
// (d) a change inside the 10 s lock waits for one timer; the reload then drops the assignment.
clock = 13000;
responses.push(respond([{id: "A", frames: {unknown: 7}, unknown_frames: true, mixed_frames: false}]));
sync(live(12, "B")); await tick();
out.d1 = [calls.length, pending().length];
clock = 21000; fire(); await tick();
out.d2 = [calls.length, summary(slot.children), project.frame_assignment];
// (e) revisions during a running fetch cause exactly one more fetch, after the lock.
clock = 40000;
responses.push(hang);
sync(live(13, "B")); await tick();
sync(live(14, "B")); sync(live(15, "B")); await tick();
out.e1 = [calls.length, pending().length];
responses.push(respond([{id: "A", frames: {unknown: 7}, unknown_frames: true, mixed_frames: false}]));
release(); await tick();
out.e2 = [calls.length, pending().length];
clock = 50000; fire(); await tick();
out.e3 = [calls.length, pending().length];
// (f) the shown project becomes the active one: its live state carries the assignment, no fetch.
sync(live(16, "A", {frames: {F: 5}, frame_assignment: assigned(5)})); await tick();
out.f = [calls.length, summary(slot.children)];
// (g) after the abort no fetch starts and no timer is left.
sync(live(17, "B")); await tick();
out.g1 = [calls.length, pending().length];
controller.abort();
out.g2 = pending().length;
clock = 90000; fire(); sync(live(18, "B")); await tick();
out.g3 = [calls.length, pending().length];
console.log(JSON.stringify(out));
""")
        row = "row:Assigned afterwards: F for %d points (2026-10-27)|Undo assignment"
        self.assertEqual(result["a"], [0, 0])
        self.assertEqual(result["b"], [1, "/api/tracking", ["notice:mix", "button:assign-frame", row % 6], 6])
        self.assertEqual(result["c"], 1)
        self.assertEqual(result["d1"], [1, 1])
        self.assertEqual(result["d2"], [2, ["notice:unknown", "button:assign-frame"], None])
        self.assertEqual(result["e1"], [3, 0])
        self.assertEqual(result["e2"], [3, 1])
        self.assertEqual(result["e3"], [4, 0])
        self.assertEqual(result["f"], [4, ["button:assign-frame", row % 5]])
        self.assertEqual(result["g1"], [4, 1])
        self.assertEqual(result["g2"], 0)
        self.assertEqual(result["g3"], [4, 0])

    def test_control_check_text_and_unavailable_field_setup_under_node(self):
        if NODE is None:
            self.skipTest("node is unavailable")
        declarations = [line for line in APP_JS.split("\n") if line.startswith(("const tr=", "const fmt=", "const CONFIG_ROUTE="))]
        for prefix in ("function controlCheckText(", "function fieldSetupCard("):
            lines = [line for line in APP_JS.split("\n") if line.startswith(prefix)]
            self.assertEqual(len(lines), 1, "%s must be one single-line declaration" % prefix)
            declarations.append(lines[0])
        script = r"""
const h = (tag, attrs, ...children) => ({tag, attrs: attrs || {}, children: children.flat()});
const iconButton = () => ({tag: "button", attrs: {}, children: [], setAttribute(name, value) { this.attrs[name] = value; }});
const f = new Function("h", "iconButton", "saveFieldSetup", '"use strict";' + process.argv[1] +
    "\nreturn {controlCheckText, fieldSetupCard};");
const api = f(h, iconButton, () => {});
const out = {};
const base = {measurements: 2, horizontal_delta_m: 0.01};
out.ground = api.controlCheckText(Object.assign({vertical_delta_m: 0.05, vertical_reference: "ground"}, base));
out.phase = api.controlCheckText(Object.assign({vertical_delta_m: -0.2, vertical_reference: "phase_centre"}, base));
out.bare = api.controlCheckText(Object.assign({vertical_delta_m: 0}, base));
globalThis.RtkI18n = {getLanguage: () => "de", t: key => key};
out.german = api.controlCheckText(Object.assign({vertical_delta_m: -0.2, vertical_reference: "ground"}, base));
delete globalThis.RtkI18n;
const card = api.fieldSetupCard(null);
out.card = {tag: card.tag, dataCard: card.attrs["data-card"], texts: card.children.map(c => c.attrs.text)};
out.full = api.fieldSetupCard({values: {antenna_height_m: 1.8}}).attrs["data-card"];
console.log(JSON.stringify(out));
"""
        run = subprocess.run([NODE, "-e", script, "\n".join(declarations)],
                             capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        result = json.loads(run.stdout)
        self.assertEqual(result["ground"], "2\u00d7 \u00b7 \u0394H 0.010 m \u00b7 \u0394V +0.050 m (ground)")
        self.assertEqual(result["phase"], "2\u00d7 \u00b7 \u0394H 0.010 m \u00b7 \u0394V -0.200 m (phase centre)")
        self.assertEqual(result["bare"], "2\u00d7 \u00b7 \u0394H 0.010 m \u00b7 \u0394V +0.000 m")
        self.assertEqual(result["german"], "2\u00d7 \u00b7 \u0394H 0,010 m \u00b7 \u0394V -0,200 m (ground)")
        self.assertEqual(result["card"], {"tag": "section", "dataCard": "field-setup",
                                          "texts": ["Field setup", "Field setup could not be loaded."]})
        self.assertEqual(result["full"], "field-setup")
        # Both dictionaries (EN, DE) carry each new key with a different text.
        for key, english in (("ui.field_setup_unavailable", "Field setup could not be loaded."),
                             ("ui.vertical_ground", "ground"), ("ui.vertical_phase_centre", "phase centre")):
            values = re.findall(r'"%s": "([^"]+)"' % re.escape(key), I18N_JS)
            self.assertEqual(len(values), 3, key)
            self.assertEqual(values[0], english)
            self.assertNotEqual(values[0], values[1], key)
            self.assertNotEqual(values[0], values[2], key)

    def test_lazy_qr_module_uses_the_asset_version(self):
        self.assertIn('data-asset-version="%s"' % assets.ASSET_VERSION, ui.APP_V2)
        self.assertNotIn("v=15.1.0", APP_JS)
        self.assertIn('"/qr.js?v="+encodeURIComponent(document.body.dataset.assetVersion', APP_JS)
        self.assertNotIn("label.ap_ssid", APP_JS)
        self.assertNotIn("ap_password", APP_JS)
        self.assertIn("label.wifi_qr", APP_JS)

    def test_start_line_verdict_follows_gate_and_mode_under_node(self):
        if NODE is None:
            self.skipTest("node is unavailable")
        declarations = [line for line in APP_JS.split("\n")
                        if line.startswith(("const tr=", "let staleSinceSeq="))]
        for prefix in ("function fixIsUsable(", "function measurementApproved(",
                       "function startLineVerdict("):
            lines = [line for line in APP_JS.split("\n") if line.startswith(prefix)]
            self.assertEqual(len(lines), 1, "%s must be one single-line declaration" % prefix)
            declarations.append(lines[0])
        script = r"""
const i18n = require(process.cwd() + "/i18n.js");
globalThis.RtkI18n = i18n;
const f = new Function('"use strict";' + process.argv[1] + "\nreturn {startLineVerdict};")();
const fix = {fix_seq: 3, stale: false}, stale = {fix_seq: 3, stale: true};
const live = (accepted, current_fix) => ({current_quality: {accepted}, current_fix});
const out = {};
out.surveyOk = f.startLineVerdict(live(true, fix), "survey");
out.routeOk = f.startLineVerdict(live(true, fix), "route");
out.surveyBlocked = f.startLineVerdict(live(false, fix), "survey");
out.routeFree = f.startLineVerdict(live(false, fix), "route");
out.staleSurvey = f.startLineVerdict(live(true, stale), "survey");
out.noQuality = f.startLineVerdict({current_fix: fix}, "survey");
out.empty = f.startLineVerdict(undefined, "survey");
i18n.setLanguage("de");
out.routeDe = f.startLineVerdict(live(false, fix), "route");
console.log(JSON.stringify(out));
"""
        run = subprocess.run([NODE, "-e", script, "\n".join(declarations)], cwd=ROOT,
                             capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        out = json.loads(run.stdout)
        dictionaries = json.loads(subprocess.run(
            [NODE, "-e", "console.log(JSON.stringify(require('./i18n.js').dictionaries))"],
            cwd=ROOT, capture_output=True, text=True, timeout=10, check=True).stdout)
        en, de = dictionaries["en"], dictionaries["de"]
        self.assertIsNone(out["surveyOk"])
        self.assertIsNone(out["routeOk"])
        self.assertEqual(out["surveyBlocked"],
                         {"blocked": True, "text": en["ui.start_line_survey_blocked"]})
        self.assertEqual(out["routeFree"],
                         {"blocked": False, "text": en["ui.route_without_approval"]})
        self.assertTrue(out["staleSurvey"]["blocked"], "a stale position is never approved")
        self.assertTrue(out["noQuality"]["blocked"])
        self.assertTrue(out["empty"]["blocked"])
        self.assertEqual(out["routeDe"]["text"], de["ui.route_without_approval"])
        for key in ("ui.route_without_approval", "ui.start_line_survey_blocked",
                    "ui.start_line_route_hint", "ui.no_project_hint"):
            self.assertIn(key, en)
            self.assertIn(key, de)

    def test_user_data_is_never_translated_under_node(self):
        if NODE is None:
            self.skipTest("node is unavailable")
        self.assertNotIn("reverseI18n", APP_JS)
        declarations = []
        for prefix in ("const tr=", "const V2_DE=", "function localText(", "const text=",
                       "const raw=", "function h(", "function capacityText("):
            lines = [line for line in APP_JS.split("\n") if line.startswith(prefix)]
            self.assertEqual(len(lines), 1, "%s must be one single-line declaration" % prefix)
            declarations.append(lines[0])
        script = r"""
const i18n = require(process.cwd() + "/i18n.js");
globalThis.RtkI18n = i18n;
globalThis.Node = class Node {};
class Fake extends Node {
  constructor(tag) { super(); this.tag = tag; this.children = []; this.textContent = ""; this.attrs = {}; }
  append(...items) { this.children.push(...items); }
  setAttribute(key, value) { this.attrs[key] = value; }
  addEventListener() {}
}
class FakeText extends Node { constructor(text) { super(); this.text = text; } }
i18n.setLanguage("de");
globalThis.document = { createElement: tag => new Fake(tag), createTextNode: value => new FakeText(value) };
const f = new Function('"use strict";' + process.argv[1] + "\nreturn {localText, text, raw, h, capacityText};")();
const de = i18n.dictionaries.de, en = i18n.dictionaries.en;
const untranslated = Object.keys(en).map(key => en[key]).find(value => !(value in i18n.v2de) && de[Object.keys(en).find(key => en[key] === value)] !== value);
const out = {};
out.rawText = f.h("b", {textRaw: "Log"}).textContent;
out.plainText = f.h("b", {text: "Log"}).textContent;
out.rawChild = f.h("b", {}, f.raw("Log")).children[0].text;
out.stringChild = f.h("b", {}, "Log").children[0].text;
out.rawEscape = f.h("b", {textRaw: "A&B <x>"}).textContent;
out.rawNull = f.h("b", {textRaw: null}).textContent;
out.rawNumber = f.h("b", {textRaw: 0}).textContent;
out.rawKeepsGerman = f.h("b", {textRaw: "Cancel"}).textContent;
out.capacity = f.capacityText("Project \u201c{name}\u201d archived", {name: "A&B <x>"});
out.untranslated = untranslated;
out.localUntranslated = f.localText(untranslated);
out.v2de = i18n.v2de;
console.log(JSON.stringify(out));
"""
        run = subprocess.run([NODE, "-e", script, "\n".join(declarations)], cwd=ROOT,
                             capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        out = json.loads(run.stdout)
        v2de = out["v2de"]
        self.assertEqual(out["rawText"], "Log")
        self.assertEqual(out["plainText"], v2de["Log"])
        self.assertEqual(out["rawChild"], "Log")
        self.assertEqual(out["stringChild"], v2de["Log"])
        self.assertEqual(out["rawEscape"], "A&B <x>")
        self.assertEqual(out["rawNull"], "")
        self.assertEqual(out["rawNumber"], "0")
        self.assertEqual(out["rawKeepsGerman"], "Cancel")
        self.assertEqual(out["capacity"], v2de["Project \u201c{name}\u201d archived"].replace("{name}", "A&B <x>"))
        self.assertIsNotNone(out["untranslated"])
        self.assertEqual(out["localUntranslated"], out["untranslated"],
                         "localText must not fall back to the reverse dictionary lookup")

    def test_log_ring_buffer_under_node(self):
        if NODE is None:
            self.skipTest("node is unavailable")
        lines = [line for line in APP_JS.split("\n")
                 if line.startswith(("const LOG_RING_LIMIT=", "function appendLogEntries("))]
        self.assertEqual(len(lines), 2, "ring limit and append helper must be single-line declarations")
        script = r"""
const api = new Function('"use strict";' + process.argv[1] + "\nreturn {LOG_RING_LIMIT, appendLogEntries};")();
const range = (from, to) => Array.from({length: to - from}, (_, i) => from + i);
const out = {limit: api.LOG_RING_LIMIT};
const ring = range(0, 999);
out.dropped = api.appendLogEntries(ring, [999, 1000, 1001], 1000);
out.length = ring.length; out.first = ring[0]; out.last = ring[ring.length - 1];
out.empty = api.appendLogEntries(ring, [], 1000);
out.afterEmpty = [ring.length, ring[0]];
const fresh = [];
out.big = api.appendLogEntries(fresh, range(0, 1500), 1000);
out.bigState = [fresh.length, fresh[0], fresh[fresh.length - 1]];
const small = [1, 2];
out.fits = api.appendLogEntries(small, [3], 1000);
out.fitsState = small;
console.log(JSON.stringify(out));
"""
        run = subprocess.run([NODE, "-e", script, "\n".join(lines)], cwd=ROOT,
                             capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        result = json.loads(run.stdout)
        self.assertEqual(result["limit"], 1000)
        self.assertEqual((result["dropped"], result["length"], result["first"], result["last"]), (2, 1000, 2, 1001))
        self.assertEqual((result["empty"], result["afterEmpty"]), (0, [1000, 2]))
        self.assertEqual((result["big"], result["bigState"]), (500, [1000, 500, 1499]))
        self.assertEqual((result["fits"], result["fitsState"]), (0, [1, 2, 3]))
        # The log view keeps the whole ring in the DOM and fetches one batch at a time.
        self.assertNotIn("slice(-300)", APP_JS)
        self.assertIn("/api/log?limit=200&cursor=", APP_JS)
        self.assertRegex(APP_CSS, r"\.log-console\{overflow-anchor:none\}")

    def test_wifi_qr_ssid_under_node(self):
        if NODE is None:
            self.skipTest("node is unavailable")
        lines = [line for line in APP_JS.split("\n") if line.startswith("function wifiQrSsid(")]
        self.assertEqual(len(lines), 1, "wifiQrSsid must be one single-line declaration")
        script = r"""
const f = new Function('"use strict";' + process.argv[1] + "\nreturn wifiQrSsid;")();
console.log(JSON.stringify([
  f("WIFI:T:WPA;S:RTK-1A2B-SETUP;P:secret;;"),
  f('WIFI:T:WPA;S:a\\;b\\:c\\,d\\\\e\\"f;P:x;;'),
  f("WIFI:S:first;T:WPA;P:p;;"),
  f("WIFI:T:WPA;S:;P:;;"),
  f(undefined),
  f("http://x")]));
"""
        run = subprocess.run([NODE, "-e", script, lines[0]], cwd=ROOT,
                             capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout),
                         ["RTK-1A2B-SETUP", 'a;b:c,d\\e"f', "first", "", "", ""])

    def test_point_shapes_under_node(self):
        if NODE is None:
            self.skipTest("node is unavailable")
        declarations = []
        for prefix in ("const POINT_SHAPES=", "function pointShapePath("):
            lines = [line for line in APP_JS.split("\n") if line.startswith(prefix)]
            self.assertEqual(len(lines), 1, "%s must be one single-line declaration" % prefix)
            declarations.append(lines[0])
        script = r"""
const f = new Function('"use strict";' + process.argv[1] + "\nreturn {POINT_SHAPES, pointShapePath};")();
const shapes = [...new Set(Object.values(f.POINT_SHAPES)), "square"];
const out = {types: f.POINT_SHAPES, paths: {}};
for (const shape of shapes) out.paths[shape] = f.pointShapePath(shape, 100.37, 50.12);
console.log(JSON.stringify(out));
"""
        run = subprocess.run([NODE, "-e", script, "\n".join(declarations)], cwd=ROOT,
                             capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        out = json.loads(run.stdout)
        x, y = 100.37, 50.12
        for shape, path in out["paths"].items():
            self.assertTrue(path.startswith("M"), shape)
            numbers = [float(value) for value in re.findall(r"-?\d+(?:\.\d+)?", path)]
            self.assertTrue(numbers and len(numbers) % 2 == 0, shape)
            for index, value in enumerate(numbers):
                centre = x if index % 2 == 0 else y
                self.assertLessEqual(abs(value - centre), 8.0 + 0.006, "%s: %s" % (shape, path))
        # The five colour groups of the point types stay distinguishable without colour.
        groups = ("valve", "control", "hydrant", "tree", "line_start")
        forms = [out["types"][name] for name in groups]
        self.assertEqual(len(set(forms)), len(groups), forms)
        self.assertEqual(out["types"]["line_end"], out["types"]["line_start"])
        self.assertEqual(len({out["paths"][form] for form in forms}), len(groups))
        # Map points and lines are button groups with an unscaled 48 px hit path.
        self.assertNotIn('rect.setAttribute("class","map-point', APP_JS)
        self.assertIn('toggleAttribute("hidden"', APP_JS)
        self.assertNotIn(".hidden=!layers", APP_JS)
        self.assertIn(".map-hit{", APP_CSS)
        self.assertIn("vector-effect:non-scaling-stroke", APP_CSS)
        self.assertIn(".map-position{pointer-events:none}", APP_CSS)
        for shape in set(out["types"].values()):
            self.assertIn(".point-symbol.shape-%s{" % shape, APP_CSS)

    def test_thresholds_and_profile_texts_under_node(self):
        if NODE is None:
            self.skipTest("node is unavailable")
        declarations = [line for line in APP_JS.split("\n")
                        if line.startswith(("const tr=", "const RTCM_EPOCH_WARN_SEC="))]
        self.assertEqual(len(declarations), 2)
        for prefix in ("function storageBlocked(", "function requiredFixName(",
                       "function measurementPhaseText("):
            lines = [line for line in APP_JS.split("\n") if line.startswith(prefix)]
            self.assertEqual(len(lines), 1, "%s must be one single-line declaration" % prefix)
            declarations.append(lines[0])
        script = r"""
const i18n = require(process.cwd() + "/i18n.js");
globalThis.RtkI18n = i18n;
const f = new Function('"use strict";const localText=value=>value;' + process.argv[1] +
  "\nreturn {storageBlocked, requiredFixName, measurementPhaseText};")();
const out = {};
out.storage = [
  f.storageBlocked({capacity: {writable: false}}),
  f.storageBlocked({capacity: {writable: true}, board: {fs_total_bytes: 100, fs_free_bytes: 19}}),
  f.storageBlocked({capacity: {writable: true}, board: {fs_total_bytes: 100, fs_free_bytes: 20}}),
  f.storageBlocked({board: {fs_total_bytes: 0, fs_free_bytes: 0}}),
  f.storageBlocked({})];
out.names = [f.requiredFixName({fix_quality: "DGPS"}), f.requiredFixName({required_fix: "RTK_FLOAT"}),
  f.requiredFixName(undefined), f.requiredFixName({})];
out.phases = [f.measurementPhaseText("waiting_fix", {fix_quality: "DGPS"}),
  f.measurementPhaseText("collecting", {}), f.measurementPhaseText("checking_spread", {}),
  f.measurementPhaseText("bogus", {}), f.measurementPhaseText(undefined, {})];
i18n.setLanguage("de");
out.de = f.measurementPhaseText("waiting_fix", {fix_quality: "RTK_FLOAT"});
out.deTemplate = i18n.dictionaries.de["ui.phase_waiting_fix"];
console.log(JSON.stringify(out));
"""
        run = subprocess.run([NODE, "-e", script, "\n".join(declarations)], cwd=ROOT,
                             capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        out = json.loads(run.stdout)
        self.assertEqual(out["storage"], [True, True, False, False, False])
        self.assertEqual(out["names"], ["DGPS", "RTK FLOAT", "RTK FIXED", "RTK FIXED"])
        self.assertEqual(out["phases"], ["Waiting for DGPS and valid GST", "Collecting valid samples",
                                         "Checking measurement spread", None, None])
        self.assertEqual(out["de"], out["deTemplate"].replace("{fix}", "RTK FLOAT"))
        self.assertNotIn("{", out["de"])

    def test_measurement_time_text_under_node(self):
        if NODE is None:
            self.skipTest("node is unavailable")
        declarations = [line for line in APP_JS.split("\n") if line.startswith("const tr=")]
        self.assertEqual(len(declarations), 1)
        for prefix in ("function measurementTimeText(", "function rejectionInfo(", "function fieldError("):
            lines = [line for line in APP_JS.split("\n") if line.startswith(prefix)]
            self.assertEqual(len(lines), 1, "%s must be one single-line declaration" % prefix)
            declarations.append(lines[0])
        script = r"""
const i18n = require(process.cwd() + "/i18n.js");
globalThis.RtkI18n = i18n;
const f = new Function('"use strict";const localText=value=>value;' + process.argv[1] +
  "\nreturn {measurementTimeText, fieldError};")();
const out = {};
out.left = f.measurementTimeText({mode: "fixed", total: 5, elapsed_sec: 8});
out.zero = f.measurementTimeText({mode: "fixed", total: 10, elapsed_sec: 50});
out.single = f.measurementTimeText({mode: "fixed", total: 1});
out.quality = f.measurementTimeText({mode: "quality", elapsed_sec: 90});
out.stale = f.fieldError(new Error("GNSS position is stale (no GGA for 7 s)."));
out.other = f.fieldError(new Error("Something else."));
out.expect = [i18n.dictionaries.en["ui.measurement_time_left"],
  i18n.dictionaries.en["ui.measurement_no_time_limit"],
  i18n.dictionaries.en["ui.measurement_stopped_stale"]];
console.log(JSON.stringify(out));
"""
        run = subprocess.run([NODE, "-e", script, "\n".join(declarations)], cwd=ROOT,
                             capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        out = json.loads(run.stdout)
        self.assertEqual(out["left"], out["expect"][0].replace("{seconds}", "12"))
        self.assertEqual(out["zero"], out["expect"][0].replace("{seconds}", "0"))
        self.assertEqual(out["single"], "")
        self.assertEqual(out["quality"], out["expect"][1])
        self.assertEqual(out["stale"], out["expect"][2].replace("{seconds}", "7"))
        self.assertEqual(out["other"], "Something else.")

    def test_measurement_deadline_matches_the_device(self):
        sources = []
        for name in ("web.py", "measurement.py"):
            if os.path.exists(os.path.join(ROOT, name)):
                with open(os.path.join(ROOT, name), encoding="utf-8") as handle:
                    sources.append(handle.read())
        self.assertTrue(any("max(15000, sample_count * 4000)" in source for source in sources))
        self.assertIn("Math.max(15,total*4)", APP_JS)
        self.assertNotIn("time_limit_sec", APP_JS)

    def test_storage_reserve_matches_the_device(self):
        match = re.search(r"STORAGE_RESERVE_RATIO=([\d.]+)", APP_JS)
        self.assertIsNotNone(match)
        self.assertEqual(float(match.group(1)), tracking.MIN_FLASH_RESERVE_PERCENT / 100)
        self.assertIn("< .2 ?", PAGES_JS)
        self.assertNotIn("< .1 ?", PAGES_JS)
        self.assertNotIn('.1?localText("Storage is running low")', APP_JS)

    def test_start_line_guard_writes_the_status_text_only_on_change_under_node(self):
        if NODE is None:
            self.skipTest("node is unavailable")
        declarations = [line for line in APP_JS.split("\n")
                        if line.startswith(("const tr=", "let staleSinceSeq=", "const MEASURE_ROUTE="))]
        for prefix in ("function fixIsUsable(", "function ingestFreshness(", "function staleText(",
                       "const LIVE_DELAYED_MS=", "function noteLiveAnswer(", "function liveAgeMs(",
                       "function liveLinkState(", "function liveView(", "function livePollIntervalMs(",
                       "function liveLostText(", "const liveDialogChecks=", "function guardStaleSubmit("):
            lines = [line for line in APP_JS.split("\n") if line.startswith(prefix)]
            self.assertEqual(len(lines), 1, "%s must be one single-line declaration" % prefix)
            declarations.append(lines[0])
        script = r"""
const writes = [];
const h = () => { let text = ""; return {hidden: true, before() {}, get textContent() { return text; },
  set textContent(value) { writes.push(value); text = value; }}; };
const store = {state: {live: {current_fix: {fix_seq: 1, stale: false}}}, subscribe: () => () => {}};
const form = {querySelector: () => null, append() {}, addEventListener() {}};
const closers = [];
const dialog = {addEventListener(type, fn) { if (type === "close") closers.push(fn); }};
const save = {disabled: false};
let message = "Blocked";
const f = new Function("h", "store", '"use strict";' + process.argv[1] + "\nreturn [guardStaleSubmit, liveLostText, liveDialogChecks, noteLiveAnswer];")(h, store);
const [guard, lostText, checks, stamp] = f;
const check = guard(dialog, form, save, () => ({blocked: true, text: message}));
const registered = checks.has(check);
check(); check(); check();
const afterSame = writes.length;
message = "Other"; check(); check();
store.state.live = {current_fix: {fix_seq: 2, stale: true, fix_age_ms: 4200}};
check(); check();
// Lost device data (no fresh answer for 10 s) locks first, whatever the last position says, and writes its text once.
const beforeLoss = writes.length;
store.state.live = {current_fix: {fix_seq: 3, stale: false}};
stamp(store.state.live, performance.now() - 11000);
check(); check();
const lost = {disabled: save.disabled, writes: writes.slice(beforeLoss), text: lostText(null).action};
// A fresh answer releases the dialog again.
store.state.live = {current_fix: {fix_seq: 4, stale: false}};
stamp(store.state.live);
check();
const released = {lostWrites: writes.length - beforeLoss, last: writes[writes.length - 1]};
closers.forEach(fn => fn());
console.log(JSON.stringify({afterSame, writes: writes.slice(0, beforeLoss), registered, lost, released,
  unregistered: !checks.has(check)}));
"""
        run = subprocess.run([NODE, "-e", script, "\n".join(declarations)], cwd=ROOT,
                             capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        out = json.loads(run.stdout)
        # Repeated polls with an unchanged text must not rewrite the live region.
        self.assertEqual(out["writes"], ["Blocked", "Other", "Position outdated for 4 s"])
        self.assertEqual(out["afterSame"], 1)
        self.assertTrue(out["registered"] and out["unregistered"])
        self.assertIn("connection", out["lost"]["text"])
        self.assertEqual(out["lost"], {"disabled": True, "writes": [out["lost"]["text"]], "text": out["lost"]["text"]})
        self.assertEqual(out["released"], {"lostWrites": 2, "last": "Other"})

    def test_checklist_judges_corrections_by_gga_field_13(self):
        javascript = APP_JS
        rows = next(line for line in javascript.split("\n") if line.startswith("function gateRows("))
        link = next(line for line in javascript.split("\n") if line.startswith("function correctionLinkState("))
        self.assertIn("fix.correction_age_sec", rows)
        self.assertNotIn("rtcm.last_msg_age_sec", rows)
        self.assertIn("rtcm.epoch_age_sec", link)
        self.assertIn("ui.correction_link", rows)
        self.assertIn("ui.corrections_stale", javascript)
        # A warning link diagnostic gets its own mark in the row renderer.
        self.assertIn('warn:"!"', javascript.replace(" ", ""))
        self.assertIn("epoch<RTCM_EPOCH_WARN_SEC", link.replace(" ", ""))
        self.assertIn("epoch<RTCM_EPOCH_STALE_FALLBACK_SEC", link.replace(" ", ""))
        self.assertNotIn("Waiting for RTK FIXED and valid GST", javascript)
        self.assertNotIn('localText(ok?"RTK FIXED"', javascript)
        self.assertIn("age>RTCM_EPOCH_WARN_SEC", javascript)
        self.assertEqual(javascript.count("age<=RTCM_ARRIVAL_LIVE_SEC"), 2)
        for start, end in (("async function mountRtcm(", "// Like setTcpAuthentication"),
                           ("function updateGlobalStatus(", "function setHeader(")):
            self.assertNotIn("max_correction_age_sec", javascript[javascript.index(start):javascript.index(end)])
        with open(os.path.join(ROOT, "app.css"), encoding="utf-8") as source:
            self.assertIn(".condition.warn", source.read())

    def test_corrections_row_follows_the_verdict_and_the_link_diagnostic_under_node(self):
        if NODE is None:
            self.skipTest("node is unavailable")
        out = run_gate_node(r"""
const row = (age, reasons, extra, lim) => g.gateRows(Object.assign({}, baseLive, {
  current_fix: Object.assign({}, fix, {correction_age_sec: age}),
  current_quality: {accepted: reasons.length === 0, reasons, limits: lim || limits}}, extra)).find(item => item.id === "corrections");
// The row judges only the server reasons: the age alone never turns it red; a missing age is unknown, not passed.
eq("ages ok", [null, 1.25, 5, 60].map(age => row(age, []).state), ["unknown", "ok", "ok", "ok"]);
eq("age red with reason", row(60, ["corrections_too_old"]).state, "fail");
eq("age detail", row(1.25, []).detail, "corrections 1.3 s old (max. 5.0 s)");
eq("age unknown detail", row(null, []).detail, "corrections age unknown (max. 5.0 s)");
eq("age absent detail", row(undefined, []).detail, "corrections age unknown (max. 5.0 s)");
eq("label", row(1, []).label, "Corrections");
// The link diagnostic: warn from 5 s inclusive, fail from 15 s inclusive, missing epoch warns; row stays ok.
const link = (epoch, rtcm) => row(1, [], {rtcm: Object.assign({epoch_age_sec: epoch, arrival_age_sec: 0.2}, rtcm)});
const linked = [null, 4.9, 5, 14.9, 15, 60].map(epoch => link(epoch, {stale: epoch !== null && epoch >= 15}));
eq("link states", linked.map(item => item.diagnostic.state), ["warn", "ok", "warn", "warn", "fail", "fail"]);
eq("link rows ok", linked.map(item => item.state), linked.map(() => "ok"));
eq("link stale false", link(20, {stale: false}).diagnostic.state, "warn");
eq("link fallback", [14.9, 15].map(epoch => link(epoch, {stale: undefined}).diagnostic.state), ["warn", "fail"]);
eq("link missing key", row(1, [], {rtcm: {}}).diagnostic.state, "warn");
eq("link text", [linked[0], linked[1], linked[4]].map(item => item.diagnostic.text), [
  "Correction link: No epoch decoded yet", "Correction link: Link latency 4.9 s · arrival 0.2 s",
  "Correction link: Link latency 15.0 s · arrival 0.2 s"]);
// A relaxed age limit moves only the detail bound; the epoch thresholds stay put.
const relaxed = Object.assign({}, limits, {max_correction_age_sec: 10});
eq("relaxed detail", row(7, [], {}, relaxed).detail, "corrections 7.0 s old (max. 10.0 s)");
eq("relaxed link", link(7, {stale: false}).diagnostic.state, "warn");
// German text substitutes the numbers too (templates come from the German dictionary).
RtkI18n.setLanguage("de");
const de = dict.de, german = link(7, {arrival_age_sec: 0.4});
eq("de detail", row(2.5, []).detail, de["ui.corrections_age"].replace("{seconds}", "2,5") + " (" + de["ui.gate_max"].replace("{value}", "5,0 s") + ")");
eq("de unknown", row(null, []).detail, de["ui.corrections_age_unknown"] + " (" + de["ui.gate_max"].replace("{value}", "5,0 s") + ")");
eq("de link", german.diagnostic.text, de["ui.correction_link"] + ": " + de["ui.link_latency"].replace("{seconds}", "7,0")
  + " · " + de["ui.arrival_age_short"].replace("{seconds}", "0,4"));
eq("de no epoch", link(null).diagnostic.text, de["ui.correction_link"] + ": " + de["ui.no_epoch_yet"]);
eq("de label", german.label, de["ui.gate_row_corrections"]);
out.deText = [row(2.5, []).detail, german.diagnostic.text].join("");
""")
        self.assertEqual(out["failures"], [])
        self.assertNotIn("{", out["deText"])

    def test_rtcm_hero_is_stale_only_while_the_device_substate_is_stale_under_node(self):
        if NODE is None:
            self.skipTest("node is unavailable")
        lines = [line for line in APP_JS.split("\n") if line.startswith("function rtcmHeroStale(")]
        self.assertEqual(len(lines), 1)
        script = r"""
const f = new Function(process.argv[1] + "\nreturn rtcmHeroStale;")();
console.log(JSON.stringify([
  f({stale: true}, {stats: {ntrip_substate: "stale"}}),
  f({stale: true}, {stats: {ntrip_substate: null}}),
  f({stale: true}, {stats: {}}), f({stale: true}, {}),
  f({stale: false}, {stats: {ntrip_substate: "stale"}})]));
"""
        run = subprocess.run([NODE, "-e", script, lines[0]], cwd=ROOT,
                             capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout), [True, False, False, False, False])

    def test_main_app_assets_have_precompressed_variants(self):
        for name in ("app.js", "app.css", "i18n.js"):
            with open(os.path.join(ROOT, name), "rb") as source:
                plain = source.read()
            with open(os.path.join(ROOT, name + ".gz"), "rb") as source:
                compressed = source.read()
            # A stale copy still compresses well, so compare the content ...
            self.assertTrue(gzip.decompress(compressed) == plain,
                            "%s.gz is stale: run python3 -c \"import push; "
                            "push.build_precompressed_assets('.')\"" % name)
            # ... and still require that the copy saves at least half of the bytes.
            self.assertLess(len(compressed), len(plain) // 2)
        with open(os.path.join(ROOT, "web.py"), encoding="utf-8") as source:
            web_source = source.read()
        with open(os.path.join(ROOT, "push.py"), encoding="utf-8") as source:
            push_source = source.read()
        self.assertIn('Content-Encoding: gzip', web_source)
        self.assertIn('"app.css.gz"', push_source)
        self.assertIn('"i18n.js.gz"', push_source)
        self.assertIn('gzip.compress(handle.read(), compresslevel=9, mtime=0)', push_source)

    def test_assets_are_external_and_csp_compatible(self):
        pages = (ui.APP_V2, web.SETUP_HTML, web.DIAGNOSTICS_HTML,
                 web.LOGIN_HTML, web.LABEL_HTML)
        for page in pages:
            self.assertNotIn("<style", page)
            self.assertNotRegex(page, r"<script(?![^>]+\bsrc=)")
            self.assertNotRegex(page, r"\sstyle=")
            self.assertNotRegex(page, r"\sonclick=")


    def test_high_contrast_theme_is_explicit_persistent_and_accessible(self):
        with open(os.path.join(ROOT, "i18n.js"), encoding="utf-8") as source:
            i18n_source = source.read()
        for value in ('const THEME_KEY = "rtk-theme"',
                      'data-theme-toggle', 'aria-pressed="false"',
                      'setAttribute("data-theme", theme)',
                      'localStorage.setItem(THEME_KEY, theme)',
                      '"ui.high_contrast": "High contrast"',
                      '"ui.high_contrast": "Hoher Kontrast"'):
            self.assertIn(value, i18n_source)
        self.assertIn(':root[data-theme="high-contrast"]', BASE_CSS)

    def test_every_page_loads_the_shared_style_layer_first(self):
        # Two sides that separately define their colors and mass run apart - that's
        # exactly what happened. base.css is the only source, and each side loads them
        # in front of its own sheet.
        for page in (web.SETUP_HTML, web.DIAGNOSTICS_HTML,
                     web.LOGIN_HTML, web.LABEL_HTML):
            self.assertIn('href="/base.css', page)
            own = "/pages.css"
            self.assertLess(page.index("/base.css"), page.index(own), own)
        self.assertIn(("/base.css", ("base.css", "text/css; charset=utf-8")),
                      web.STATIC_ASSETS.items())

    def test_field_app_css_has_tap_focus_and_overlap_rules(self):
        self.assertIn("--warn-soft:", APP_CSS)
        self.assertIn(':root[data-theme="high-contrast"]{--warn-soft:', APP_CSS)
        block = APP_CSS[APP_CSS.index("/* M2 #17"):]
        # Disabled controls differ by pattern and border, not only by colour.
        self.assertIn("repeating-linear-gradient", block)
        self.assertIn("border-style:dashed", block)
        self.assertIn("scroll-padding-bottom", APP_CSS)
        self.assertIn("scroll-padding-top", APP_CSS)
        self.assertIn("html:has(.field-actions){scroll-padding-bottom", block)
        self.assertIn(".status-side{display:contents}", block)
        # Toasts were replaced by the in-flow message dock (#45): nothing floats above the content.
        self.assertIn(".feedback-dock{display:grid", block)
        self.assertIsNone(re.search(r"\.feedback-dock[^{}]*\{[^{}]*position:", block))
        self.assertIn("summary:focus-visible", block)
        # The layer switches are part of the tool row (inside its menu), not a second overlay on the map.
        self.assertIn('h("details",{class:"map-menu"}', APP_JS)
        self.assertIn("replace(canvas,mapGraphic,chip,tools,legend)", APP_JS)
        self.assertNotIn("layerTools", APP_JS)

    def test_map_menu_holds_layers_and_full_screen(self):
        draw = next(line for line in APP_JS.splitlines() if line.lstrip().startswith("const drawMap="))
        self.assertIn('class:"map-menu"', draw)
        menu_start = draw.index('class:"map-menu"')
        tools_start = draw.index('class:"map-tools"')
        # The menu is built first and handed to the tool row as its last child.
        menu = draw[menu_start:tools_start]
        direct = draw[tools_start:draw.index("replace(canvas,mapGraphic,chip,tools,legend)")]
        # Layers and full screen sit inside the menu; the tool row itself keeps zoom, fit and centre.
        self.assertIn('class:"map-layers"', menu)
        self.assertIn('"Full screen"', menu)
        self.assertIn("map-menu-state", draw)
        self.assertNotIn('"Full screen"', direct)
        self.assertNotIn('class:"map-layers"', direct)
        self.assertTrue(direct.rstrip().endswith(",menu);"))
        for label in ('"Fit project"', '"Center position"'):
            self.assertIn(label, direct)
        self.assertIn('tr("ui.map_menu","Layers & full screen")', menu)

    def test_design_tokens_exist_only_in_the_shared_layer(self):
        for token in ("--radius:10px", "--space-4:16px", "--tap:48px",
                      "--shadow:0 1px 3px", "--surface:#fff", "--accent:#075fb8"):
            self.assertIn(token, BASE_CSS, token)
        # Neither side is allowed to bring its own color; otherwise it falls out of the
        # row in the external contrast theme.
        for name, sheet in (("pages.css", PAGES_CSS),):
            self.assertNotRegex(sheet, r"#[0-9a-fA-F]{3,8}", name)
            self.assertNotIn(":root{", sheet, name)
        for sheet in (BASE_CSS, PAGES_CSS):
            self.assertNotIn("100% - 2*", sheet)

    def test_pages_share_one_visual_component_language(self):
        for value in ("button:hover:not(:disabled)", "border-radius:var(--radius)",
                      "background:var(--surface)", "@media(max-width:600px)"):
            self.assertIn(value, BASE_CSS, value)
        for page in (web.SETUP_HTML, web.DIAGNOSTICS_HTML,
                     web.LOGIN_HTML, web.LABEL_HTML):
            self.assertIn('class="product-header', page)
            self.assertIn('class="product-name"', page)
        for page in (web.SETUP_HTML, web.DIAGNOSTICS_HTML, web.LABEL_HTML):
            self.assertIn('class="product-nav app-navigation"', page)
            self.assertIn('class="page-content', page)
        self.assertNotIn('class="product-nav"', web.LOGIN_HTML)

    def test_navigation_and_surfaces_use_one_idiom(self):
        # Tabs in the dashboard and side links looked like two products.
        self.assertIn('.product-nav a,.product-nav button', BASE_CSS)
        self.assertIn('[aria-selected="true"]', BASE_CSS)
        self.assertIn('[aria-current="page"]', BASE_CSS)
        # Each page builds on the same two surfaces: panel carries page content, card
        # the tiles in it.
        for page in (web.SETUP_HTML, web.DIAGNOSTICS_HTML,
                     web.LOGIN_HTML, web.LABEL_HTML):
            self.assertRegex(page, r'class="[^"]*\b(panel|card)\b', page[:80])
        self.assertIn(".panel{", BASE_CSS)
        self.assertIn(".card{", BASE_CSS)

    def test_mobile_navigation_keeps_every_destination_visible(self):
        self.assertIn('grid-template-columns:repeat(3,minmax(0,1fr))', BASE_CSS)
        self.assertIn('@media(max-width:380px)', BASE_CSS)
        self.assertIn('grid-template-columns:repeat(2,minmax(0,1fr))', BASE_CSS)
        mobile = BASE_CSS[BASE_CSS.index('@media(max-width:600px)'):]
        self.assertNotIn('overflow-x:auto', mobile)
        self.assertIn('overflow-wrap:anywhere', mobile)


    def test_product_is_named_esp_rtk_everywhere(self):
        self.assertTrue(cfg.CONFIG["version"].startswith("ESP-RTK V"))
        self.assertEqual(cfg.FACTORY_AP_SSID, "ESP-RTK-Setup")
        self.assertEqual(cfg.CONFIG["ap_ssid"], "ESP-RTK-Setup")
        # Built by concatenation so the old product name is not a literal
        # substring anywhere in the exported public tree.
        old_suffix_name = "GNSS-" + "PRO"
        old_prefix_name = "ESP32-" + "GNSS"
        for page in (web.SETUP_HTML, web.render_setup(csrf="CSRF"), ui.APP_V2, ui.APP_V2_LOGIN):
            self.assertNotIn(old_suffix_name, page)
            self.assertNotIn(old_prefix_name, page)

    def test_authenticated_shell_order_and_logout_are_identical(self):
        pages = (web.SETUP_HTML, web.DIAGNOSTICS_HTML, web.LABEL_HTML)
        for page in pages:
            header = page.index('class="product-header"')
            navigation = page.index('class="product-nav app-navigation')
            status = page.index('class="device-status-bar"')
            content = page.index('class="page-content')
            self.assertLess(header, navigation)
            self.assertLess(navigation, status)
            self.assertLess(status, content)
            self.assertIn("logout", page)
        self.assertNotIn("app-navigation", web.LOGIN_HTML)
        self.assertNotIn("page-logout", web.LOGIN_HTML)
        for value in ('querySelectorAll(".page-logout")', 'method:"DELETE"',
                      '"X-CSRF-Token":csrf', 'location.replace("/")'):
            self.assertIn(value, PAGES_JS)


    def test_status_renders_gate_rows_only(self):
        base = self.block_of("function renderStatusCore(")
        self.assertIn("gateRows(live)", base)
        self.assertIn("gateHeadline(live)", base)
        for removed in ("qualityRows", "passed-conditions", '"GST accuracy age"'):
            self.assertNotIn(removed, APP_JS)
        for needle in ("data-gate", "condition-diagnostic", "ui.gst_output", "hero-activity"):
            self.assertIn(needle, APP_JS)
        # The control hint is decided once, by the core step; the extras step only toggles its class.
        self.assertEqual(APP_JS.count("statusRefs.control.hidden="), 1)
        self.assertIn("statusRefs.control.hidden=", base)
        self.assertIn("/* M2 #21a: gate rows from server reasons", APP_CSS)
        self.assertIn(".diagnostic-mark", APP_CSS)

    def test_measure_screen_follows_the_target_order(self):
        start = APP_JS.index("function mountStatus(){")
        mount = APP_JS[start:APP_JS.index("\n", start)]
        for needle in ('h("div",{class:"status-primary"},project,hero,setup,last,actions)',
                       'h("div",{class:"status-side"},checks,context,capacity,diagnostics)',
                       'class:"tool-card status-project"', 'class:"status-project-text"',
                       'class:"tool-card status-setup"', "ui.switch_project"):
            self.assertIn(needle, mount)
        self.assertIn("switchProject", APP_JS)
        block = APP_CSS[APP_CSS.index("/* M2 #21a: gate rows from server reasons"):]
        self.assertIn("overflow-wrap:anywhere", block)
        self.assertIn(".status-primary{display:contents}", block)
        self.assertIn(".status-primary>.field-actions{order:1}", block)
        self.assertIn('data-context="storage"] span', APP_JS)

    def test_gate_reason_codes_match_the_server(self):
        if NODE is None:
            self.skipTest("node is unavailable")
        source = inspect.getsource(tracking.quality_check)
        self.assertEqual(len(GATE_CODE_ROWS), 13)
        for code in GATE_CODE_ROWS:
            self.assertIn('"%s"' % code, source, code)
        self.assertEqual(source.count("reasons.append("), 13)
        out = run_gate_node(r"""
out.rowTable = g.GATE_REASON_ROW;
out.rowIds = g.GATE_ROW_IDS;
out.keys = {en: Object.keys(dict.en), de: Object.keys(dict.de)};
""")
        self.assertEqual(out["rowTable"], GATE_CODE_ROWS)
        self.assertEqual(out["rowIds"], ["fix", "corrections", "accuracy", "satellites", "gst", "position"])
        wanted = ["gate.gst_required_waiting", "gate.gst_required_uart_error", "gate.gst_required_receiving"]
        for code in GATE_CODE_ROWS:
            wanted += ["gate." + code, "quality." + code]
        for language in ("en", "de"):
            for key in wanted:
                self.assertIn(key, out["keys"][language], "%s missing in %s" % (key, language))

    def test_gate_rows_follow_server_reasons_under_node(self):
        if NODE is None:
            self.skipTest("node is unavailable")
        out = run_gate_node(r"""
const E2 = g.GATE_REASON_ROW;
const params = {
  en: {rtk_fixed_required: {fix: "GPS FIX"}, required_fix_not_met: {fix: "GPS FIX", required: "RTK FIXED"},
       too_few_satellites: {count: "7", limit: "10"}, hdop_too_high: {value: "2.25", limit: "1.50"},
       corrections_too_old: {age: "12.0", limit: "5.0"}, gst_required: {attempt: "3"},
       gst_horizontal_error_too_high: {value: "0.051", limit: "0.035"},
       gst_vertical_error_too_high: {value: "0.083", limit: "0.040"}},
  de: {rtk_fixed_required: {fix: "GPS FIX"}, required_fix_not_met: {fix: "GPS FIX", required: "RTK FIXED"},
       too_few_satellites: {count: "7", limit: "10"}, hdop_too_high: {value: "2,25", limit: "1,50"},
       corrections_too_old: {age: "12,0", limit: "5,0"}, gst_required: {attempt: "3"},
       gst_horizontal_error_too_high: {value: "0,051", limit: "0,035"},
       gst_vertical_error_too_high: {value: "0,083", limit: "0,040"}}};
const ids = rows => rows.map(row => row.id);
const failing = rows => rows.filter(row => row.state === "fail");
for (const language of ["en", "de"]) {
  RtkI18n.setLanguage(language);
  for (const code of Object.keys(E2)) {
    const live = reasonLive([code]), rows = g.gateRows(live), name = language + ":" + code;
    const expected = fill(dict[language]["gate." + code], params[language][code]);
    eq(name + " ids", ids(rows), g.GATE_ROW_IDS);
    eq(name + " failing", ids(failing(rows)), [E2[code]]);
    eq(name + " texts", failing(rows)[0].reasonTexts, [expected]);
    eq(name + " reasons", failing(rows)[0].reasons, [code]);
    eq(name + " headline", g.gateHeadline(live), expected);
    eq(name + " reasonText", g.gateReasonText(code, live), expected);
  }
}
RtkI18n.setLanguage("en");
const headline = (code, extra) => g.gateHeadline(reasonLive([code], extra));
eq("literal corrections_too_old", headline("corrections_too_old"),
  "Corrections 12.0 s old (max. 5.0 s) – check caster/internet");
eq("literal gst_required", headline("gst_required"),
  "Receiver sends no accuracy estimate (GST). Device is requesting it (attempt 3).");
eq("literal no_position", headline("no_position"), "No position – wait for a clear view of the sky");
// gst_required follows the device state; the GST row then carries no diagnostic (the reason names the state).
for (const [state, key] of [["uart_error", "gate.gst_required_uart_error"], ["waiting", "gate.gst_required_waiting"],
                            ["receiving", "gate.gst_required_receiving"], [null, "gate.gst_required_receiving"]]) {
  const live = reasonLive(["gst_required"], {gnss_accuracy: state ? {state, attempts: 2} : undefined});
  const row = g.gateRows(live).find(item => item.id === "gst");
  eq("gst variant " + state, row.reasonTexts, [dict.en[key]]);
  eq("gst variant diagnostic " + state, row.diagnostic, null);
}
// Without a reason every row is ok and nothing is left to say.
const accepted = g.gateRows(withVerdict({accepted: true, reasons: [], limits}));
eq("accepted states", accepted.map(row => row.state), ["ok", "ok", "ok", "ok", "ok", "ok"]);
eq("accepted ids", ids(accepted), g.GATE_ROW_IDS);
eq("accepted headline", g.gateHeadline(withVerdict({accepted: true, reasons: [], limits})), null);
eq("accepted details", accepted.map(row => row.detail), ["GPS FIX · required: RTK FIXED",
  "corrections 12.0 s old (max. 5.0 s)", "H 0.051 m (max. 0.035) · V 0.083 m (max. 0.040)",
  "7 satellites (min. 10) · HDOP 2.25 (max. 1.50)", "Accuracy estimate present", "Position and height present"]);
// The German wording lives only in i18n.js: build the expectation from the de dictionary, pin the
// German number format and prove that every detail text really is translated.
const de = dict.de, deMax = value => " (" + fill(de["ui.gate_max"], {value}) + ")";
eq("accepted detail keys translated", ["ui.gate_fix_required", "ui.corrections_age", "ui.gate_min",
  "ui.gate_gst_present", "ui.gate_position_ok"].filter(key => !de[key] || de[key] === dict.en[key]), []);
RtkI18n.setLanguage("de");
eq("accepted details de", g.gateRows(withVerdict({accepted: true, reasons: [], limits})).map(row => row.detail),
  ["GPS FIX · " + fill(de["ui.gate_fix_required"], {fix: "RTK FIXED"}),
   fill(de["ui.corrections_age"], {seconds: "12,0"}) + deMax("5,0 s"),
   "H 0,051 m" + deMax("0,035") + " · V 0,083 m" + deMax("0,040"),
   "7 satellites (" + fill(de["ui.gate_min"], {value: "10"}) + ") · HDOP 2,25" + deMax("1,50"),
   de["ui.gate_gst_present"], de["ui.gate_position_ok"]]);
RtkI18n.setLanguage("en");
eq("labels", accepted.map(row => row.label), ["Fix type", "Corrections", "Accuracy H/V",
  "Satellites and geometry", "GST output", "Position and height"]);
// Several reasons: one red row per affected row, the first server reason heads the hero, order is the server's.
const two = reasonLive(["hdop_too_high", "gst_required"]);
eq("two failing", ids(failing(g.gateRows(two))), ["satellites", "gst"]);
eq("two headline", g.gateHeadline(two), "HDOP 2.25 (max. 1.50) – wait for a better satellite geometry");
const same = g.gateRows(reasonLive(["too_few_satellites", "hdop_too_high"]));
eq("same row", ids(failing(same)), ["satellites"]);
eq("same row texts", failing(same)[0].reasonTexts, [
  "7 satellites (min. 10) – move to a clearer view of the sky",
  "HDOP 2.25 (max. 1.50) – wait for a better satellite geometry"]);
""")
        self.assertEqual(out["failures"], [])

    def test_gate_rows_handle_missing_and_unknown_verdicts_under_node(self):
        if NODE is None:
            self.skipTest("node is unavailable")
        out = run_gate_node(r"""
const ids = rows => rows.map(row => row.id);
const failing = rows => rows.filter(row => row.state === "fail");
const sixPlus = ["fix", "corrections", "accuracy", "satellites", "gst", "position", "other"];
const checkOther = (name, live, texts) => {
  const rows = g.gateRows(live), last = rows[rows.length - 1];
  eq(name + " ids", ids(rows), sixPlus);
  eq(name + " failing", ids(failing(rows)), ["other"]);
  eq(name + " texts", last.reasonTexts, texts);
  eq(name + " headline", g.gateHeadline(live), texts[0]);
  eq(name + " label", last.label, "Further condition");
};
checkOther("future", reasonLive(["future_code"]), [fill(dict.en["gate.unknown"], {code: "future_code"})]);
const futureMixed = g.gateRows(reasonLive(["hdop_too_high", "future_code"]));
eq("future with known failing", ids(failing(futureMixed)), ["satellites", "other"]);
eq("future with known texts", futureMixed[6].reasonTexts, [fill(dict.en["gate.unknown"], {code: "future_code"})]);
checkOther("no reason", withVerdict({accepted: false, reasons: [], limits}), [dict.en["gate.no_reason"]]);
checkOther("null verdict", withVerdict(null), [dict.en["gate.no_verdict"]]);
checkOther("missing verdict", Object.assign({}, baseLive), [dict.en["gate.no_verdict"]]);
checkOther("reasons not an array", withVerdict({accepted: false, reasons: "x", limits}), [dict.en["gate.no_reason"]]);
checkOther("accepted missing", withVerdict({reasons: [], limits}), [dict.en["gate.no_reason"]]);
// The hero headline of a mixed verdict names the first server reason, known or not.
eq("unknown first", g.gateHeadline(reasonLive(["future_code", "hdop_too_high"])),
  fill(dict.en["gate.unknown"], {code: "future_code"}));
// Inherited object keys are not reason codes.
checkOther("proto key", reasonLive(["constructor"]), [fill(dict.en["gate.unknown"], {code: "constructor"})]);
// Reasons win over the accepted flag: accepted:true with a reason is still red on that row.
const mixed = g.gateRows(withVerdict({accepted: true, reasons: ["hdop_too_high"], limits}));
eq("accepted with reason", ids(failing(mixed)), ["satellites"]);
eq("accepted with reason has no other", ids(mixed), g.GATE_ROW_IDS);
// Duplicates (the server can name required_fix_not_met twice) and non-strings do not repeat or break rows.
const dup = g.gateRows(withVerdict({accepted: false, reasons: ["required_fix_not_met", "required_fix_not_met", 7, null], limits}));
eq("duplicate texts", dup[0].reasonTexts.length, 1);
eq("non-string ignored", ids(dup), g.GATE_ROW_IDS);
// Stale position: every row is red, only the position row talks, headline is left to the stale hero.
const staleReasons = ["no_position", "rtk_fixed_required", "too_few_satellites", "hdop_too_high", "altitude_required", "gst_required"];
const staleFix = Object.assign({}, fix, {stale: true, fix_age_ms: 4200});
const staleLive = reasonLive(staleReasons, {current_fix: staleFix});
const staleRows = g.gateRows(staleLive);
eq("stale ids", ids(staleRows), g.GATE_ROW_IDS);
eq("stale states", staleRows.map(row => row.state), ["fail", "fail", "fail", "fail", "fail", "fail"]);
eq("stale position text", staleRows[5].reasonTexts, ["Position outdated for 4 s"]);
eq("stale other texts", staleRows.slice(0, 5).map(row => row.reasonTexts), [[], [], [], [], []]);
eq("stale headline", g.gateHeadline(staleLive), null);
eq("stale detail kept", staleRows[3].detail, "7 satellites (min. 10) · HDOP 2.25 (max. 1.50)");
eq("stale without verdict", ids(g.gateRows(withVerdict(null, {current_fix: staleFix}))), g.GATE_ROW_IDS);
// Nothing may leak placeholders, undefined or NaN, whatever the snapshot lacks, in both languages.
const cases = [{}, undefined, null, {current_quality: {}}, {current_quality: {accepted: false, reasons: ["hdop_too_high", "gst_required", "corrections_too_old", "too_few_satellites", "gst_horizontal_error_too_high", "gst_vertical_error_too_high", "required_fix_not_met"]}},
  {current_fix: {}, current_quality: {accepted: false, reasons: staleReasons}}, reasonLive(staleReasons), reasonLive(["future_code"]),
  reasonLive(["gst_required"], {gnss_accuracy: {state: "requested"}}), withVerdict({accepted: true, reasons: []}, {rtcm: {}, network: {}})];
const seen = JSON.stringify(["en", "de"].map(language => { RtkI18n.setLanguage(language);
  return cases.map(live => [g.gateRows(live), g.gateHeadline(live)]); }),
  (key, value) => value === undefined ? "undefined" : typeof value === "number" && Number.isNaN(value) ? "NaN" : value);
out.leaks = [/undefined/.test(seen), /NaN/.test(seen), /\{\w+\}/.test(seen)];
out.sample = seen.length;
""")
        self.assertEqual(out["failures"], [])
        self.assertEqual(out["leaks"], [False, False, False])
        self.assertGreater(out["sample"], 1000)

    def test_gate_gst_diagnostic_follows_the_reason_not_the_stale_fix_under_node(self):
        if NODE is None:
            self.skipTest("node is unavailable")
        out = run_gate_node(r"""
const gstRow = live => g.gateRows(live).find(item => item.id === "gst");
const staleFix = Object.assign({}, fix, {stale: true, fix_age_ms: 4200});
const requested = {gnss_accuracy: {state: "requested", attempts: 3}};
// gst_required names the output state in its own text, so a fresh fix shows no second line ...
eq("fresh gst_required", gstRow(reasonLive(["gst_required"], requested)).diagnostic, null);
// ... while a stale fix replaces the reason texts, so the diagnostic is the only place left for the state.
const stale = gstRow(reasonLive(["gst_required"], Object.assign({current_fix: staleFix}, requested)));
eq("stale gst_required diagnostic", stale.diagnostic, {kind: "gst", state: "warn", text: "GST output: requested (attempt 3)"});
eq("stale gst row", [stale.state, stale.reasonTexts], ["fail", []]);
eq("stale uart error", gstRow(reasonLive(["gst_required"], {current_fix: staleFix, gnss_accuracy: {state: "uart_error"}})).diagnostic.state, "fail");
eq("stale without gnss_accuracy", gstRow(reasonLive(["gst_required"], {current_fix: staleFix, gnss_accuracy: undefined})).diagnostic, null);
""")
        self.assertEqual(out["failures"], [])

    def test_gate_diagnostics_never_change_the_verdict_under_node(self):
        if NODE is None:
            self.skipTest("node is unavailable")
        out = run_gate_node(r"""
const open = extra => g.gateRows(withVerdict({accepted: true, reasons: [], limits}, extra));
const byId = (rows, id) => rows.find(row => row.id === id);
const epochs = [null, 4.9, 5, 14.9, 15, 60];
const linkRows = epochs.map(epoch => open({rtcm: {epoch_age_sec: epoch, arrival_age_sec: 0.2, stale: epoch !== null && epoch >= 15}}));
eq("link row stays ok", linkRows.map(rows => byId(rows, "corrections").state), epochs.map(() => "ok"));
eq("link kind", linkRows.map(rows => byId(rows, "corrections").diagnostic.kind), epochs.map(() => "link"));
eq("link state", linkRows.map(rows => byId(rows, "corrections").diagnostic.state), ["warn", "ok", "warn", "warn", "fail", "fail"]);
eq("link all rows ok", linkRows.map(rows => rows.every(row => row.state === "ok")), epochs.map(() => true));
eq("link text", byId(linkRows[1], "corrections").diagnostic.text, "Correction link: Link latency 4.9 s · arrival 0.2 s");
eq("link text no epoch", byId(linkRows[0], "corrections").diagnostic.text, "Correction link: No epoch decoded yet");
// Without the device flag the 15 s fallback decides.
eq("link fallback", [14.9, 15].map(epoch => open({rtcm: {epoch_age_sec: epoch}})).map(rows => byId(rows, "corrections").diagnostic.state), ["warn", "fail"]);
eq("link helper", [[undefined], [{}], [{epoch_age_sec: 1}], [{epoch_age_sec: 20, stale: false}]].map(args => g.correctionLinkState(...args)), ["warn", "warn", "ok", "warn"]);
// ntrip_state "disabled" means no endpoint (switched off or incomplete): say only that nothing arrives.
const disabled = open({network: {ntrip_state: "disabled"}});
eq("disabled rows", disabled.map(row => row.id), g.GATE_ROW_IDS);
eq("disabled diagnostic", byId(disabled, "corrections").diagnostic, {kind: "link", state: "warn", text: dict.en["ui.corrections_unavailable"]});
out.unavailable = dict.en["ui.corrections_unavailable"];
eq("disabled keeps rows ok", disabled.every(row => row.state === "ok"), true);
// GST output state is a diagnostic of the GST row only.
const states = ["receiving", "requested", "waiting", "uart_error"];
const gstRows = states.map(state => open({gnss_accuracy: {state, attempts: 1}}));
eq("gst row ok", gstRows.map(rows => byId(rows, "gst").state), states.map(() => "ok"));
eq("gst diagnostic state", gstRows.map(rows => byId(rows, "gst").diagnostic.state), ["ok", "warn", "warn", "fail"]);
eq("gst diagnostic kind", gstRows.map(rows => byId(rows, "gst").diagnostic.kind), states.map(() => "gst"));
eq("gst diagnostic text", gstRows.map(rows => byId(rows, "gst").diagnostic.text), [
  "GST output: receiving", "GST output: requested (attempt 1)", "GST output: waiting for the receiver",
  "GST output: request failed (UART)"]);
eq("gst all rows ok", gstRows.map(rows => rows.every(row => row.state === "ok")), states.map(() => true));
eq("gst absent", byId(open({gnss_accuracy: undefined}), "gst").diagnostic, null);
eq("gst unknown state", byId(open({gnss_accuracy: {state: "bogus"}}), "gst").diagnostic.text, "GST output: —");
eq("gst state text", [{state: "waiting"}, {}, undefined].map(value => g.gstStateText(value)), ["waiting for the receiver", "—", "—"]);
// A GST reason other than gst_required keeps the diagnostic; gst_required drops it.
eq("gst kept for other reason", byId(g.gateRows(reasonLive(["not_all_samples_have_gst"])), "gst").diagnostic.kind, "gst");
// Only the server reasons judge the row: an old correction age alone does not turn it red.
const oldAge = open({current_fix: Object.assign({}, fix, {correction_age_sec: 60})});
eq("old age without reason", byId(oldAge, "corrections").state, "ok");
// A pending GST shows the held accuracy and changes no state.
const held = open({current_fix: Object.assign({}, fix, {receiver_accuracy: undefined, pending_gst: true,
  held_receiver_accuracy: {horizontal_sigma_m: 0.031, altitude_sigma_m: 0.047}})});
eq("pending gst detail", byId(held, "accuracy").detail, "H 0.031 m (max. 0.035) · V 0.047 m (max. 0.040)");
eq("pending gst states", held.every(row => row.state === "ok"), true);
// Without limits the details drop the bounds instead of printing placeholders.
const bare = g.gateRows({current_fix: fix, current_quality: {accepted: true, reasons: []}});
eq("bare details", bare.slice(0, 4).map(row => row.detail), ["GPS FIX · required: RTK FIXED", "corrections 12.0 s old",
  "H 0.051 m · V 0.083 m", "7 satellites · HDOP 2.25"]);
""")
        self.assertEqual(out["failures"], [])
        for word in ("switched off", "disabled"):
            self.assertNotIn(word, out["unavailable"])


    CAUSE_PREFIXES = ("const HELP_ROUTES=", "function wifiErrorText(", "function ntripErrorText(", "function correctionsCause(")
    CAUSE_EXPORTS = ("HELP_ROUTES", "wifiErrorText", "ntripErrorText", "correctionsCause")

    def test_corrections_cause_tells_the_states_apart_under_node(self):
        if NODE is None:
            self.skipTest("node is unavailable")
        out = run_gate_node(r"""
const en = dict.en;
const live = (state, config, code, extra) => Object.assign({}, baseLive, {network: Object.assign(
  {ntrip_state: state, ntrip_config: config, last_ntrip_error_code: code}, extra || {})});
const T = {
  parked: "NTRIP stopped after repeated failures – it starts again when the caster settings change, the device restarts or Wi-Fi reconnects",
  off: "NTRIP switched off – no correction data via NTRIP",
  incomplete: "NTRIP is on, but no caster with host and mountpoint is configured",
  unavailable: en["ui.corrections_unavailable"],
  wifi: "Waiting for Wi-Fi before connecting to the caster",
  auth: "The caster refuses the NTRIP user or password",
  mount: "The caster does not offer this mountpoint",
  failed: "Caster connection failed (" + en["ntrip_error.timeout"] + ") – the device retries automatically",
  again: "Connecting to the caster again after: " + en["ntrip_error.timeout"]};
// [ntrip_state, ntrip_config, last_ntrip_error_code, id, state, short token, text, action label, routeKey]
const table = [
  ["disabled", "switched_off", null, "switched_off", "off", "NTRIP off", T.off, "Open NTRIP settings", "corrections"],
  ["disabled", "incomplete", null, "incomplete", "warn", "NTRIP not set up", T.incomplete, "Set up NTRIP", "corrections"],
  ["disabled", null, null, "unavailable", "warn", "NTRIP inactive", T.unavailable, "Open diagnostics", "diagnostics"],
  ["waiting_wifi", "ready", null, "waiting_wifi", "warn", "NTRIP waits for Wi-Fi", T.wifi, "Check Wi-Fi", "wifi"],
  ["connecting", "ready", "timeout", "connection_error", "warn", "NTRIP reconnecting", T.again, "Check NTRIP settings", "corrections"],
  ["auth_error", "ready", "auth_error", "auth_error", "fail", "NTRIP login refused", T.auth, "Check NTRIP settings", "corrections"],
  ["mount_error", "ready", "mount_error", "mount_error", "fail", "NTRIP mountpoint missing", T.mount, "Check NTRIP settings", "corrections"],
  ["backoff", "ready", "timeout", "connection_error", "fail", "NTRIP error", T.failed, "Check NTRIP settings", "corrections"],
  ["backoff", "ready", "retries_exhausted", "parked", "fail", "NTRIP stopped", T.parked, "Check NTRIP settings", "corrections"],
  ["auth_error", "ready", "retries_exhausted", "parked", "fail", "NTRIP stopped", T.parked, "Check NTRIP settings", "corrections"],
  ["mount_error", "ready", "retries_exhausted", "parked", "fail", "NTRIP stopped", T.parked, "Check NTRIP settings", "corrections"]];
for (const [state, config, code, id, level, short, text, label, routeKey] of table) {
  const name = [state, config, code].join("/");
  const cause = g.correctionsCause(live(state, config, code));
  eq(name + " cause", cause && [cause.id, cause.state, cause.short, cause.text, cause.action], [id, level, short, text, {label, routeKey}]);
  eq(name + " route", Object.keys(g.HELP_ROUTES).includes(routeKey), true);
  eq(name + " no placeholder", /[{}]|undefined|null/.test(cause.short + cause.text + cause.action.label), false);
  // The gate never changes: all rows stay ok, only the corrections diagnostic carries the cause.
  const rows = g.gateRows(reasonLive([], live(state, config, code)));
  eq(name + " rows ok", rows.every(row => row.state === "ok"), true);
  eq(name + " diagnostic", rows.find(row => row.id === "corrections").diagnostic,
    {kind: "link", state: level === "off" ? "warn" : level, text});
}
// No cause: connecting without a code, streaming (also with an old code), no network block, unknown states.
eq("connecting without code", g.correctionsCause(live("connecting", "ready", null)), null);
eq("streaming", g.correctionsCause(live("streaming", "ready", null)), null);
eq("streaming with an old code", g.correctionsCause(live("streaming", "ready", "timeout")), null);
eq("no network", g.correctionsCause(Object.assign({}, baseLive, {network: undefined})), null);
eq("no live", [g.correctionsCause(null), g.correctionsCause(undefined), g.correctionsCause({})], [null, null, null]);
eq("unknown state", g.correctionsCause(live("idle", "ready", null)), null);
// "switched off" needs the explicit device word; a missing or unknown config is only "inactive".
eq("config values", ["switched_off", "incomplete", null, undefined, "ready", "bogus"].map(
  config => g.correctionsCause(live("disabled", config, null)).id), ["switched_off", "incomplete", "unavailable", "unavailable", "unavailable", "unavailable"]);
// A parked client is not "retrying automatically"; a plain failure is.
const parked = g.correctionsCause(live("backoff", "ready", "retries_exhausted")).text;
eq("parked wording", [parked.includes("retries automatically"), parked.includes("caster settings change")], [false, true]);
eq("failure wording", g.correctionsCause(live("backoff", "ready", "timeout")).text.includes("retries automatically"), true);
// Unknown error codes are shown as they are, a missing code as "unknown error".
eq("unknown code", g.correctionsCause(live("backoff", "ready", "weird_code")).text.includes("(weird_code)"), true);
eq("missing code", g.correctionsCause(live("backoff", "ready", null)).text.includes("(unknown error)"), true);
// The Wi-Fi error is its own text part; unknown codes pass through.
eq("wifi error", g.correctionsCause(live("waiting_wifi", "ready", null, {last_wifi_error: "access_point_not_found"})).text, T.wifi + " · Hotspot not found");
eq("wifi unknown", g.correctionsCause(live("waiting_wifi", "ready", null, {last_wifi_error: "odd"})).text, T.wifi + " · odd");
eq("wifi helper", [g.wifiErrorText(null), g.wifiErrorText(""), g.wifiErrorText(undefined), g.wifiErrorText("connection_lost"),
  g.wifiErrorText("authentication_failed"), g.wifiErrorText("association_failed"), g.wifiErrorText("connection_timeout")],
  ["", "", "", "Wi-Fi connection lost", "Wi-Fi authentication failed", "Wi-Fi association failed", "Wi-Fi connection timed out"]);
const codes = ["timeout", "network_error", "unexpected_error", "stale_backlog", "idle_timeout", "eof", "retries_exhausted",
  "protocol_error", "caster_error", "auth_error", "mount_error"];
eq("ntrip error helper", [g.ntripErrorText(null), g.ntripErrorText(undefined), g.ntripErrorText("other")], ["", "", "other"]);
eq("ntrip error texts", codes.map(code => g.ntripErrorText(code)), codes.map(code => en["ntrip_error." + code]));
eq("ntrip error texts differ", new Set(codes.map(code => en["ntrip_error." + code])).size, codes.length);
eq("help routes", g.HELP_ROUTES, {corrections: "device/connections/internet", wifi: "device/connections/internet", diagnostics: "device/diagnostics"});
// German: every text comes from the German dictionary and differs from the English one.
RtkI18n.setLanguage("de");
const texts = table.map(row => {
  const cause = g.correctionsCause(live(row[0], row[1], row[2]));
  return [cause.short, cause.text, cause.action.label];
});
eq("de differs", texts.map((parts, index) => parts.map((part, at) => part !== [table[index][5], table[index][6], table[index][7]][at])),
  texts.map(() => [true, true, true]));
eq("de no placeholder", texts.some(parts => parts.some(part => /[{}]|undefined|null/.test(part))), false);
eq("de error code", g.correctionsCause(live("backoff", "ready", "timeout")).text.includes(dict.de["ntrip_error.timeout"]), true);
eq("de keys", Object.keys(dict.en).filter(key => /^(ui\.cause_|ntrip_error\.)/.test(key)).filter(key => !(key in dict.de)), []);
out.count = table.length;
""", extra_prefixes=self.CAUSE_PREFIXES, extra_exports=self.CAUSE_EXPORTS)
        self.assertEqual(out["failures"], [])
        self.assertEqual(out["count"], 11)

    WHY_PREFIXES = CAUSE_PREFIXES + (
        "const WHY_STEPS=", "const WHY_ROW_STEP=", "function whyNotSteps(", "function whyNotCause(",
        "function freshnessText(", "function liveLostText(", "const LIVE_DELAYED_MS=")
    WHY_EXPORTS = ("WHY_STEPS", "WHY_ROW_STEP", "GATE_ROW_IDS", "gateRows", "whyNotSteps", "whyNotCause",
                   "correctionsCause", "wifiErrorText", "HELP_ROUTES")

    def test_why_row_steps_cover_every_gate_row(self):
        if NODE is None:
            self.skipTest("node is unavailable")
        out = run_gate_node(r"""
eq("row keys", Object.keys(g.WHY_ROW_STEP).sort(), [...g.GATE_ROW_IDS, "other"].sort());
eq("row key count", Object.keys(g.WHY_ROW_STEP).length, 7);
eq("steps", g.WHY_STEPS, ["app", "receiver", "wifi", "caster", "corrections", "accuracy"]);
eq("every row reaches a step", Object.values(g.WHY_ROW_STEP).every(step => g.WHY_STEPS.includes(step)), true);
const codes = Object.keys(g.GATE_REASON_ROW);
eq("13 codes", codes.length, 13);
eq("every code reaches a step", codes.every(code => g.WHY_STEPS.includes(g.WHY_ROW_STEP[g.GATE_REASON_ROW[code]])), true);
""", extra_prefixes=self.WHY_PREFIXES, extra_exports=self.WHY_EXPORTS)
        self.assertEqual(out["failures"], [])
        self.assertNotIn("WHY_REASON_STEP", APP_JS)

    def test_why_not_steps_under_node(self):
        if NODE is None:
            self.skipTest("node is unavailable")
        out = run_gate_node(r"""
const en = dict.en;
const view = {age: 1000, link: "live"};
const base = extra => Object.assign({
  link: {receiver: "ok", uart_silence_ms: 40, corrections: "ok", rtcm_silence_ms: 90},
  network: {ntrip_state: "streaming", ntrip_config: "ready", access_state: "ONLINE"},
  gnss_accuracy: {state: "receiving"},
  rtcm: {mount: "MOUNT1", epoch_age_sec: 1, arrival_age_sec: 0.2, stale: false}}, extra || {});
const live = (reasons, extra) => reasonLive(reasons, base(extra));
const steps = (l, v) => g.whyNotSteps(l, v || view);
const IDS = ["app", "receiver", "wifi", "caster", "corrections", "accuracy"];
const staleFix = Object.assign({}, fix, {stale: true, fix_age_ms: 9000});
const bad = list => list.some(step => /[{}]|undefined|NaN|null/.test(step.text + (step.detail || "") + ((step.action || {}).label || "")));

// (a) everything good
const good = steps(live([]));
eq("good ids", good.map(step => step.id), IDS);
eq("good states", good.map(step => step.state), IDS.map(() => "ok"));
eq("good cause", g.whyNotCause(good), -1);
eq("good no placeholder", bad(good), false);
eq("good app text", good[0].text, "Updated 1 s ago");
eq("good receiver text", good[1].text, "GPS FIX · 7 satellites");
eq("good mount is its own field", [good[3].text, good[3].detail], ["Caster connected", "MOUNT1"]);
eq("good actions", good.map(step => step.action), IDS.map(() => null));

// (b) each gate reason alone
const codes = Object.keys(g.GATE_REASON_ROW);
for (const code of codes) {
  const l = live([code]), list = steps(l), want = IDS.indexOf(g.WHY_ROW_STEP[g.GATE_REASON_ROW[code]]);
  eq(code + " state", list[want].state, "fail");
  eq(code + " text", list[want].text.includes(g.gateReasonText(code, l)), true);
  eq(code + " cause", g.whyNotCause(list), want);
  eq(code + " others ok", list.filter((_, index) => index !== want).every(step => step.state === "ok"), true);
}

// (c) invariant matrix: a failing server row always yields a non-ok step and a cause
const cases = codes.map(code => [code, live([code])]);
for (let i = 0; i < codes.length; i++) for (let j = i + 1; j < codes.length; j++)
  if (g.GATE_REASON_ROW[codes[i]] !== g.GATE_REASON_ROW[codes[j]]) cases.push([codes[i] + "+" + codes[j], live([codes[i], codes[j]])]);
cases.push(["future_reason", live(["future_reason"])]);
for (const [name, verdict] of [["null", null], ["undefined", undefined], ["string", "x"], ["number", 7],
    ["rejected without reason", {accepted: false, reasons: []}], ["accepted as a string", {accepted: "yes", reasons: []}],
    ["accepted with an unknown reason", {accepted: true, reasons: ["future_reason"]}]])
  cases.push([name, withVerdict(verdict, base())]);
cases.push(["stale fix", live([], {current_fix: staleFix})]);
let failing = 0;
for (const [name, l] of cases) {
  const rows = g.gateRows(l), failRows = rows.filter(row => row.state === "fail"), list = steps(l), cause = g.whyNotCause(list);
  eq(name + " no placeholder", bad(list), false);
  if (!failRows.length) { eq(name + " no cause without a failing row", cause, -1); continue; }
  failing++;
  eq(name + " cause found", cause >= 0, true);
  if (name === "stale fix") continue;
  eq(name + " every failing row has a non-ok step", failRows.every(row => list[IDS.indexOf(g.WHY_ROW_STEP[row.id])].state !== "ok"), true);
  eq(name + " cause row", failRows.some(row => g.WHY_ROW_STEP[row.id] === list[cause].id && list[cause].text.includes(row.reasonTexts[0])), true);
}
out.failing = failing;
out.cases = cases.length;

// (d) unknown code, missing and contentless verdict land in the accuracy step
let l = live(["future_reason"]), list = steps(l);
eq("unknown code", [list[5].state, list[5].text.includes(g.gateReasonText("future_reason", l)), g.whyNotCause(list)], ["fail", true, 5]);
for (const verdict of [null, "x"]) {
  list = steps(withVerdict(verdict, base()));
  eq("verdict " + verdict, [list[5].state, list[5].text, g.whyNotCause(list)], ["warn", en["gate.no_verdict"], 5]);
}
list = steps(withVerdict({accepted: false, reasons: []}, base()));
eq("no reason", [list[5].state, list[5].text, g.whyNotCause(list)], ["fail", en["gate.no_reason"], 5]);

// (e) the data link to the device is lost, delayed or not yet established
list = steps(live([]), {age: 12000, link: "lost"});
eq("lost", list.map(step => step.state), ["fail", "unknown", "unknown", "unknown", "unknown", "unknown"]);
eq("lost texts", [list[0].text, list[1].text, list[5].text], ["No status data from the device for 12 s", en["ui.why_no_data"], en["ui.why_no_data"]]);
eq("lost cause", g.whyNotCause(list), 0);
list = steps(live([]), {age: null, link: "lost", never: true});
eq("never answered", [list[0].state, list[0].text], ["fail", "No status data from the device yet"]);
list = steps(live([]), {age: 5000, link: "delayed"});
eq("delayed", [list[0].state, list[0].text, g.whyNotCause(list)], ["warn", "Updated 5 s ago", 0]);
list = steps(undefined, {age: null, link: "connecting", never: true});
eq("connecting", list.map(step => step.state), ["warn", "unknown", "unknown", "unknown", "unknown", "unknown"]);
eq("connecting text", list[0].text, en["ui.why_connecting"]);

// (f) silent receiver (cable) and (g) receiver alive but no positions
list = steps(live([], {link: {receiver: "silent", uart_silence_ms: 12000, corrections: "ok"}}));
eq("silent", [list[1].state, list[1].text.includes("12 s"), list[1].text.includes("cable"), g.whyNotCause(list)], ["fail", true, true, 1]);
list = steps(live([], {current_fix: staleFix}));
eq("no gga", [list[1].state, list[1].text.includes("no positions"), list[1].text.includes("9 s"), list[1].text.includes("cable")], ["fail", true, true, false]);
eq("stale hides the rest", [list[4].state, list[4].text, list[5].state, list[5].text, g.whyNotCause(list)], ["unknown", en["ui.why_no_data"], "unknown", en["ui.why_no_data"], 1]);
eq("stale keeps wifi and caster", [list[2].state, list[3].state], ["ok", "ok"]);
list = steps(live([], {link: {receiver: "unknown"}}));
eq("receiver unknown", [list[1].state, list[1].text], ["warn", en["ui.why_receiver_unknown"]]);
list = steps(live([], {link: undefined}));
eq("link missing is judged by the fix", list[1].state, "ok");
list = steps(live([], {link: undefined, current_fix: staleFix}));
eq("link missing and stale", [list[1].state, list[1].text.includes("no positions")], ["fail", true]);

// (h) Wi-Fi
const wifi = (access, error, extra) => steps(live([], {network: Object.assign({ntrip_state: "waiting_wifi", ntrip_config: "ready", access_state: access, last_wifi_error: error}, extra || {})}));
list = wifi("RECOVERY", "authentication_failed");
eq("wifi recovery", [list[2].state, list[2].text, list[2].action.routeKey, list[2].action.label, g.whyNotCause(list)],
  ["warn", g.wifiErrorText("authentication_failed"), "wifi", en["ui.cause_action_wifi"], 2]);
list = wifi("CONNECTING", null);
eq("wifi connecting", [list[2].state, list[2].text, list[2].action.routeKey], ["warn", en["ui.why_wifi_connecting"], "wifi"]);
list = wifi("SETUP", null);
eq("wifi setup", [list[2].state, list[2].text, list[2].action.routeKey], ["warn", en["ui.why_wifi_setup"], "wifi"]);
list = wifi("ONLINE", null, {ntrip_state: "streaming"});
eq("wifi online", [list[2].state, list[2].text, list[2].action], ["ok", en["ui.why_wifi_online"], null]);

// (i) switched off, (j) login refused and parked
const caster = (state, config, code) => steps(live([], {network: {ntrip_state: state, ntrip_config: config, last_ntrip_error_code: code, access_state: "ONLINE"}}));
list = caster("disabled", "switched_off", null);
eq("off", [list[3].state, list[4].state, list[4].text, g.whyNotCause(list), list[3].action.routeKey], ["off", "off", en["ui.why_corrections_none"], 3, "corrections"]);
list = caster("auth_error", "ready", "auth_error");
eq("login refused", [list[3].state, list[4].state, g.whyNotCause(list), list[3].text], ["fail", "unknown", 3, g.correctionsCause(live([], {network: {ntrip_state: "auth_error", ntrip_config: "ready", last_ntrip_error_code: "auth_error"}})).text]);
list = caster("backoff", "ready", "retries_exhausted");
eq("parked", [list[3].state, list[3].text.includes("starts again when the caster settings change")], ["fail", true]);
list = caster("connecting", "ready", null);
eq("connecting without a cause", [list[3].state, list[3].text], ["unknown", en["ui.why_caster_connecting"]]);

// (k) pending GST and (l) GST output states
list = steps(live([], {current_fix: Object.assign({}, fix, {pending_gst: true, receiver_accuracy: null, held_receiver_accuracy: {horizontal_sigma_m: 0.021, altitude_sigma_m: 0.03}})}));
eq("pending gst", [list[5].state, list[5].text.includes(en["ui.gst_pending"]), list[5].text.includes("0.021"), g.whyNotCause(list)], ["warn", true, true, 5]);
for (const [state, word] of [["requested", "requested"], ["waiting", "waiting for the receiver"], ["uart_error", "request failed"]]) {
  list = steps(live([], {gnss_accuracy: {state, attempts: 2}}));
  eq("gst " + state, [list[5].state, list[5].text.startsWith("GST output: "), list[5].text.includes(word)], ["warn", true, true]);
}
list = steps(live([]));
eq("accuracy text", list[5].text, "H 0.051 m · V 0.083 m");
eq("late corrections", steps(live([], {link: {receiver: "ok", corrections: "stale"}}))[4].state, "warn");

// (m) missing blocks never throw
list = steps(live([], {network: undefined}));
eq("no network", [list[2].state, list[3].state, list[4].state], ["unknown", "unknown", "unknown"]);
for (const empty of [null, undefined, {}]) eq("empty live", steps(empty).length, 6);
eq("empty view", g.whyNotSteps(live([]), undefined).length, 6);

// every action is a known help route
const everything = cases.map(row => row[1]).concat([live([], {network: {ntrip_state: "disabled", ntrip_config: "incomplete"}}),
  live([], {network: {ntrip_state: "disabled", ntrip_config: null}}), live([], {network: {ntrip_state: "mount_error", last_ntrip_error_code: "mount_error"}}),
  live([], {network: {ntrip_state: "waiting_wifi", ntrip_config: "ready", access_state: "RECOVERY"}})]);
let actions = 0;
for (const item of everything) for (const step of steps(item)) if (step.action) {
  actions++;
  eq("route " + step.action.routeKey, Object.keys(g.HELP_ROUTES).includes(step.action.routeKey), true);
  eq("label " + step.id, typeof step.action.label === "string" && step.action.label.length > 0, true);
}
out.actions = actions;

// German: every text comes from the German dictionary
const rich = [live([]), live([], {link: {receiver: "silent", uart_silence_ms: 12000}}), live([], {current_fix: staleFix}),
  live([], {network: {ntrip_state: "disabled", ntrip_config: "switched_off", access_state: "SETUP"}}),
  live([], {gnss_accuracy: {state: "requested", attempts: 2}}), live([], {link: {receiver: "unknown", corrections: "stale"}}),
  withVerdict(null, base())];
const english = rich.map(item => steps(item).map(step => step.text));
const englishLost = steps(live([]), {age: 12000, link: "lost"}).map(step => step.text);
RtkI18n.setLanguage("de");
const german = rich.map(item => steps(item).map(step => step.text));
const germanLost = steps(live([]), {age: 12000, link: "lost"}).map(step => step.text);
// The satellite count goes through localText (v2de), which this harness stubs; the browser run checks that one.
eq("de differs", german.flat().filter((text, index) => text === english.flat()[index] && text !== "GPS FIX · 7 satellites"), []);
eq("de lost differs", germanLost.map((text, index) => text !== englishLost[index]), [true, true, true, true, true, true]);
eq("de no placeholder", german.concat([germanLost]).flat().some(text => /[{}]|undefined|NaN/.test(text)), false);
eq("de keys", Object.keys(dict.en).filter(key => /^ui\.why_/.test(key)).filter(key => !(key in dict.de)), []);
eq("en keys", Object.keys(dict.de).filter(key => /^ui\.why_/.test(key)).filter(key => !(key in dict.en)), []);
out.whyKeys = Object.keys(dict.en).filter(key => /^ui\.why_/.test(key)).length;
""", extra_prefixes=self.WHY_PREFIXES, extra_exports=self.WHY_EXPORTS)
        self.assertEqual(out["failures"], [])
        self.assertEqual(out["cases"], 13 + 69 + 1 + 7 + 1)
        self.assertGreater(out["failing"], 80)
        self.assertEqual(out["actions"], 5)
        self.assertGreater(out["whyKeys"], 15)

    def test_why_sheet_is_wired_to_the_measuring_help(self):
        sheet = APP_JS[APP_JS.index("function openWhyNot("):]
        sheet = sheet[:sheet.index("\n")]
        node = APP_JS[APP_JS.index("function whyStepNode("):]
        node = node[:node.index("\n")]
        for part in ("action-sheet why-sheet", "aria-labelledby", "why-steps", "aria-current", "liveDialogChecks.add(",
                     "liveDialogChecks.delete(", "store.subscribe(", "whyNotCause("):
            self.assertIn(part, sheet)
        for part in ("why-step", "data-step", "data-state", "why-mark", "HELP_ROUTES[", "dialog.close()"):
            self.assertIn(part, node)
        self.assertIn('iconButton(tr("ui.why_open"', APP_JS)
        self.assertIn(".why-steps{", APP_CSS)
        self.assertIn(".why-step.cause{border-width:3px}", APP_CSS)
        for state, color in (("fail", "critical"), ("warn", "warn"), ("ok", "ok")):
            self.assertIn('.why-step[data-state="%s"] .why-mark{color:var(--%s)}' % (state, color), APP_CSS)

    def test_pill_and_sheet_name_the_corrections_cause(self):
        section = APP_JS[APP_JS.index("function updateGlobalStatus("):APP_JS.index("function setHeader(")]
        self.assertTrue(section.startswith("function updateGlobalStatus(live){ingestFreshness(live);noteLiveAnswer(live);"))
        self.assertIn("correctionsCause(", section)
        self.assertIn("cause.short", section)
        self.assertIn("cause.text", section)
        measured = APP_JS[APP_JS.index("async function measuredAction("):]
        measured = measured[:measured.index("\n}\n") + 3]
        self.assertIn("measurement-cause", measured)
        self.assertIn("correctionsCause(live)", measured)
        # The cause line follows the progress answer, which carries the network block.
        self.assertLess(measured.index("store.set({live})"), measured.index("correctionsCause(live)"))
        self.assertIn(".measurement-cause{", APP_CSS)
        self.assertIn('.measure-help[data-cause="switched_off"]', APP_CSS)
        self.assertIn("function measureHelpCard(", APP_JS)
        self.assertIn("function renderMeasureHelp(", APP_JS)
        self.assertIn("statusRefs.hero.after(statusRefs.help)", APP_JS)
        # One mapping of the Wi-Fi codes, shared by the details row and the cause.
        self.assertEqual(APP_JS.count("Hotspot not found"), 1)


class TestZugriff(unittest.TestCase):
    def setUp(self):
        self.saved = dict(cfg.CONFIG)

    def tearDown(self):
        cfg.CONFIG.clear()
        cfg.CONFIG.update(self.saved)

    def test_without_token_not_from_the_sta_network(self):
        cfg.CONFIG["config_token"] = ""
        self.assertFalse(web._config_allowed(False, "", b""))

    def test_with_tokens_from_the_sta_network(self):
        cfg.CONFIG["config_token"] = "secret"
        self.assertTrue(web._config_allowed(False, "token=secret", b""))

    def test_always_through_the_ap(self):
        cfg.CONFIG["config_token"] = "secret"
        self.assertTrue(web._config_allowed(True, "", b""))


if __name__ == "__main__":
    unittest.main()


class TestNavigation(unittest.TestCase):

    def test_side_pages_link_all_portal_areas(self):
        for page in (web.SETUP_HTML, web.DIAGNOSTICS_HTML, web.LABEL_HTML):
            for route in ("#status", "#projects", "#device"):
                self.assertIn(route, page)


    def test_protected_side_pages_show_the_status_line(self):
        for page in (web.SETUP_HTML, web.DIAGNOSTICS_HTML, web.LABEL_HTML):
            self.assertIn('class="device-status-bar"', page)
        self.assertNotIn('class="device-status-bar"', web.LOGIN_HTML)
        for value in ('fetch("/status")', 'setInterval(() =>', 'board.cpu_hz',
                      'board.ram_free_bytes', 'board.fs_free_bytes',
                      'stats.wifi_ssid_aktiv', 'stats.task_restarts'):
            self.assertIn(value, PAGES_JS)

    def test_config_page_links_to_overview(self):
        # Check on the rendered HTML, not on the template - there is still the
        # placeholder for the token.
        self.assertIn('href="/', web.render_setup())


class TestTokenInLink(unittest.TestCase):
    def setUp(self):
        self.saved = dict(cfg.CONFIG)

    def tearDown(self):
        cfg.CONFIG.clear()
        cfg.CONFIG.update(self.saved)

    def test_without_tokens_the_link_remains_simple(self):
        html = web.render_setup()
        self.assertIn('href="/#status"', html)
        self.assertNotIn("{token_query}", html)


    def test_old_token_will_not_be_attached(self):
        html = web.render_setup(token="s3cret")
        self.assertNotIn("token=s3cret", html)

    def test_tokens_in_the_link_are_masked(self):
        html = web.render_setup(token='a"b')
        self.assertNotIn('href="/?token=a"b"', html)

    def test_no_placeholders_remain_standing(self):
        for html in (web.render_setup(), web.render_setup(token="x")):
            self.assertNotIn("{token_query}", html)
            self.assertNotIn("{config_token_field}", html)


class TestTokenEncoding(unittest.TestCase):
    """html_escape is for HTML attributes, not for URLs. a token with '&' or '#' was cut off in the link, one decoded with '+' as space, one with '%' made the URL unruly.
    """

    def test_kodiert_trennzeichen(self):
        self.assertEqual(web.url_encode("a&b"), "a%26b")
        self.assertEqual(web.url_encode("a#b"), "a%23b")
        self.assertEqual(web.url_encode("a b"), "a%20b")
        self.assertEqual(web.url_encode("a+b"), "a%2Bb")
        self.assertEqual(web.url_encode("100%"), "100%25")

    def test_leaves_unobjectionable(self):
        self.assertEqual(web.url_encode("ee34faa987c0fd3c"), "ee34faa987c0fd3c")
        self.assertEqual(web.url_encode("a-b_c.d~e"), "a-b_c.d~e")

    def test_utf8(self):
        self.assertEqual(web.url_encode("café"), "caf%C3%A9")

    def test_umkehrbar(self):
        for value in ("a&b", "a#b", "a b", "a+b", "100%", "café", "naïve"):
            self.assertEqual(web.url_decode(web.url_encode(value)), value)

    def test_token_is_not_taken_over_in_link(self):
        for token in ("a&b", "a#b", "a b", "a+b", "100%"):
            html = web.render_setup(token=token)
            self.assertNotIn("token=", html)

    def test_token_does_not_arrive_at_the_server(self):
        for token in ("a&b", "a#b", "a b", "100%"):
            html = web.render_setup(token=token)
            self.assertNotIn(web.url_encode(token), html)


# #38b: the frame the correction service reports, offered in Measure -> Field setup. The helpers of
# test_navigation (it imports this module, hence the late import) run the single-line declarations of
# app.js under Node against a small element model with the real h(), iconButton() semantics reduced
# to what the card needs.
SUGGESTION_PREFIXES = (
    "const tr=", "const V2_DE=", "function localText(", "function capacityText(", "const replace=",
    "let frameSuggestionHidden=", "function frameSuggestionSignature(", "function frameSuggestionDismissed(",
    "function dismissFrameSuggestion(", "function frameSuggestionCurrent(", "function frameSuggestionInfo(",
    "function frameSuggestionFor(", "function frameSuggestionKey(", "let frameAdoptPending=",
    "let fieldSetupSaving=", "function fieldSetupWriting(", "function syncSaveLock(", "function syncFieldSetupLock(",
    "async function saveFieldSetup(",
    "function frameEpochText(", "function frameServiceName(", "function frameDiffersText(",
    "function frameSuggestionBox(", "function syncFrameSlot(",
    "function adoptFrameValues(", "function invalidateLive(", "async function adoptFrameSuggestion(", "function sectionTarget(",
    "function fieldSetupCard(", "const CONFIG_ROUTE=")
SUGGESTION_STUBS = r"""
const matchesSelector = (node, selector) => {
  const parts = /^(\w+)?(?:\[([\w-]+)(?:="([^"]*)")?\])?$/.exec(selector);
  if (selector.startsWith("#")) return node.attrs.id === selector.slice(1);
  if (selector.startsWith(".")) return String(node.attrs.class || "").split(" ").includes(selector.slice(1));
  if (!parts) throw new Error("selector not supported: " + selector);
  return (!parts[1] || node.tag === parts[1]) && (!parts[2] || (parts[3] === undefined ? parts[2] in node.attrs : node.attrs[parts[2]] === parts[3]));
};
class El {
  constructor(tag, attrs, children) { this.tag = tag; this.attrs = attrs; this.children = children; this.dataset = {}; this.isConnected = true; this.disabled = false; this.value = attrs.value; }
  replaceChildren(...items) { this.children = items.flat(Infinity); }
  setAttribute(name, value) { this.attrs[name] = value; }
  removeAttribute(name) { delete this.attrs[name]; }
  get name() { return this.attrs.name; }
  focus() { this.focused = true; }
  all(found = []) { found.push(this); this.children.forEach(child => child instanceof El && child.all(found)); return found; }
  querySelector(selector) { return this.all().slice(1).find(node => matchesSelector(node, selector)) || null; }
  querySelectorAll(selector) { return this.all().slice(1).filter(node => matchesSelector(node, selector)); }
  text() { return [this.attrs.textRaw != null ? String(this.attrs.textRaw) : this.attrs.text != null ? localText(this.attrs.text) : "",
    ...this.children.map(child => child instanceof El ? child.text() : String(child))].filter(Boolean).join(" "); }
}
const h = (tag, attrs, ...children) => new El(tag, Object.assign({}, attrs), children.flat(Infinity));
const iconButton = (label, handler, kind) => { const button = h("button", {text: label, kind});
  button.click = () => button.disabled ? undefined : handler(); return button; };
let requestSerial = 0, activeController = new AbortController();
// No live stream here: invalidateLive() (#27 rule R3) only raises the serial.
const liveSource = {renew() {}};
const store = {state: {live: null}, sets: 0, listeners: new Set(),
  set(patch) { this.sets += 1; Object.assign(this.state, patch); this.listeners.forEach(fn => fn(this.state)); },
  subscribe(fn) { this.listeners.add(fn); return () => this.listeners.delete(fn); }};
const main = new El("main", {}, []);
const puts = [], successes = [], errors = [];
let hold = null, answer = null;
const api = {put: (path, body) => { puts.push([path, body]); return hold || Promise.resolve(answer(body)); }};
const showSuccess = message => successes.push(message), showError = error => errors.push(error.message);
// What mountFieldSetup reads besides the field setup card: the other cards are plain sections.
let loadedSetup = null;
const methodCard = () => h("section", {"data-card": "method"}), gateCard = () => h("section", {"data-card": "gate"});
const skeleton = () => h("div", {}), setConnection = () => {}, optionalJson = async () => loadedSetup;
api.json = async () => ({values: {quality_window_samples: 5}});
const shown = (card = main) => card.querySelector('[data-slot="frame-suggestion"]');
const buttons = card => shown(card).querySelectorAll("button");
const suggestion = (state, frame, epoch, anchor) => ({reference_frame: frame, reference_frame_epoch: epoch, anchor: anchor || null, state});
const setup = (values, extra) => Object.assign({values: Object.assign({antenna_height_m: 1.8, reference_frame: "", reference_frame_epoch: ""}, values), defaults: {}}, extra);
const freshCard = card => { main.replaceChildren(card); activeController = new AbortController(); return card; };
"""


def navigation():
    import test_navigation
    return test_navigation


class TestFrameSuggestion(unittest.TestCase):

    def run_node(self, body, asynchronous=False, prefixes=SUGGESTION_PREFIXES):
        module = navigation()
        return module.run_app_node(prefixes, body, stubs=SUGGESTION_STUBS, asynchronous=asynchronous)

    def test_frame_suggestion_info_follows_state_and_dismissal(self):
        out = self.run_node(r"""
const sessionData = new Map();
globalThis.sessionStorage = {getItem: key => sessionData.has(key) ? sessionData.get(key) : null,
  setItem: (key, value) => sessionData.set(key, String(value))};
const current = {frame: "", epoch: ""};
const fresh = suggestion("new", "ETRS89", "2010.0", "plate_fixed");
const info = frameSuggestionInfo(fresh, current, false);
eq("new", info && [info.kind, info.frame, info.epoch, info.anchor, info.current, info.currentEpoch],
  ["new", "ETRS89", "2010.0", "plate_fixed", "", ""]);
eq("signature", frameSuggestionSignature(fresh), "ETRS89|2010.0");
eq("signature without epoch", frameSuggestionSignature(suggestion("new", "ETRS89", "")), "ETRS89|");
eq("not dismissed at the start", frameSuggestionDismissed(fresh), false);
dismissFrameSuggestion(fresh);
eq("dismissed by signature", frameSuggestionDismissed(fresh), true);
eq("stored per tab session", sessionData.get("rtk-frame-suggestion"), "ETRS89|2010.0");
eq("dismissed new is hidden", frameSuggestionInfo(fresh, current, frameSuggestionDismissed(fresh)), null);
const other = suggestion("new", "ETRS89", "2020.0");
eq("another signature shows again", frameSuggestionInfo(other, current, frameSuggestionDismissed(other)) !== null, true);
const differs = frameSuggestionInfo(suggestion("differs", "ETRS89", "2010.0"), {frame: "ITRF2014", epoch: ""}, false);
eq("differs", differs && [differs.kind, differs.current, differs.currentEpoch, differs.anchor], ["differs", "ITRF2014", "", null]);
eq("dismissed differs is hidden", frameSuggestionInfo(suggestion("differs", "ETRS89", "2010.0"), current, true), null);
const matches = frameSuggestionInfo(suggestion("matches", "ETRS89", "2010.0"), {frame: "ETRS89", epoch: "2010.0"}, true);
eq("matches is never dismissed", matches && matches.kind, "matches");
for (const bad of [null, undefined, {}, "x", [], suggestion("bogus", "ETRS89", ""), suggestion("new", "", ""),
    suggestion("new", "  ", ""), {state: "new"}, {state: "new", reference_frame: 5}])
  eq("invalid " + JSON.stringify(bad), frameSuggestionInfo(bad, current, false), null);
eq("odd epoch and anchor are dropped", (({epoch, anchor}) => [epoch, anchor])(
  frameSuggestionInfo({state: "new", reference_frame: "ETRS89", reference_frame_epoch: 5, anchor: "sideways"}, current, false)), ["", null]);
// The current values: the confirmed live defaults first, the loaded setup only before the first live snapshot.
const loaded = setup({reference_frame: "ITRF2014", reference_frame_epoch: "2005.0"});
eq("setup before live", frameSuggestionCurrent(null, loaded), {frame: "ITRF2014", epoch: "2005.0"});
eq("live before setup", frameSuggestionCurrent({measurement_defaults: {reference_frame: " ETRS89 ", reference_frame_epoch: "2010.0"}}, loaded),
  {frame: "ETRS89", epoch: "2010.0"});
eq("live without a frame key", frameSuggestionCurrent({measurement_defaults: {samples: 5}}, loaded), {frame: "ITRF2014", epoch: "2005.0"});
eq("live frame cleared", frameSuggestionCurrent({measurement_defaults: {reference_frame: "", reference_frame_epoch: null}}, loaded), {frame: "", epoch: ""});
eq("nothing known", frameSuggestionCurrent(null, null), {frame: "", epoch: ""});
// The shown suggestion: the live snapshot decides once it carries the key (null = none), else the loaded answer.
const both = setup({}, {suggestion: suggestion("new", "ETRS89", "2030.0")});
eq("loaded answer", (frameSuggestionFor(null, both) || {}).frame, "ETRS89");
eq("live null wins", frameSuggestionFor({frame_suggestion: null}, both), null);
eq("live object wins", (frameSuggestionFor({frame_suggestion: suggestion("new", "ITRF2020", "")}, both) || {}).frame, "ITRF2020");
eq("live without the key", (frameSuggestionFor({}, both) || {}).frame, "ETRS89");
eq("key none", frameSuggestionKey(null, true), "none");
const keys = new Set([frameSuggestionKey(info, false), frameSuggestionKey(info, true), frameSuggestionKey(differs, false),
  frameSuggestionKey(Object.assign({}, info, {anchor: null}), false), frameSuggestionKey(Object.assign({}, info, {current: "x"}), false),
  frameSuggestionKey(Object.assign({}, info, {currentEpoch: "x"}), false), frameSuggestionKey(Object.assign({}, info, {epoch: "x"}), false)]);
eq("every input changes the key", keys.size, 7);
eq("equal inputs, equal key", frameSuggestionKey(Object.assign({}, info), false), frameSuggestionKey(info, false));
""")
        self.assertEqual(out["failures"], [])

    def test_frame_suggestion_box_renders_text_only(self):
        out = self.run_node(r"""
const kinds = (state, frame, epoch, current, currentEpoch, anchor) => frameSuggestionInfo(suggestion(state, frame, epoch, anchor),
  {frame: current, epoch: currentEpoch}, false);
const texts = box => box.text();
for (const lang of ["en", "de"]) {
  RtkI18n.setLanguage(lang);
  const words = dict[lang], v2 = RtkI18n.v2de, fill = (template, values) => (lang === "de" ? v2[template] : template)
    .replace(/\{(\w+)\}/g, (all, key) => values[key]);
  const epochOf = value => fill("epoch {epoch}", {epoch: value});
  const fresh = frameSuggestionBox(kinds("new", "<b>x</b>", "2010.0", "", ""), false);
  eq(lang + " new class", [fresh.attrs.class, fresh.attrs["data-notice"]], ["notice", "frame-suggestion"]);
  eq(lang + " name is a text, not markup", fresh.all().some(node => node.tag === "b"), false);
  eq(lang + " name shown", texts(fresh).includes("<b>x</b>"), true);
  eq(lang + " new sentence", texts(fresh).includes(fill("{frame}, epoch {epoch} – use it for new measurements?", {frame: "<b>x</b>", epoch: "2010.0"})), true);
  eq(lang + " title", texts(fresh).includes(words["ui.frame_suggestion_title"]), true);
  eq(lang + " two buttons", fresh.querySelectorAll("button").map(node => [node.attrs["data-action"], node.attrs.text, node.disabled]),
    [["adopt-frame", words["ui.frame_suggestion_adopt"], false], ["dismiss-frame", words["ui.frame_suggestion_dismiss"], false]]);
  const noEpoch = frameSuggestionBox(kinds("new", "ETRS89", "", "", ""), false);
  eq(lang + " new without epoch", texts(noEpoch).includes(fill("{frame} – use it for new measurements?", {frame: "ETRS89"})), true);
  const anchored = frameSuggestionBox(kinds("new", "ETRS89", "2010.0", "", "", "plate_fixed"), false);
  eq(lang + " anchor addendum", texts(anchored).includes("ETRS89 (" + words["ui.frame_anchor_plate_fixed"] + ")"), true);
  eq(lang + " global anchor", texts(frameSuggestionBox(kinds("new", "ETRS89", "", "", "", "global"), false)).includes("(" + words["ui.frame_anchor_global"] + ")"), true);
  const onlyEpoch = frameSuggestionBox(kinds("differs", "ETRS89", "2020.0", "ETRS89", "2010.0"), false);
  eq(lang + " differs is a warning", [onlyEpoch.attrs.class, onlyEpoch.attrs["data-notice"]], ["notice warn", "frame-suggestion"]);
  eq(lang + " both epochs", texts(onlyEpoch).includes(fill("The correction service reports {frame} ({epoch}), the field setup says {current} ({current_epoch}). New RTK points are saved with the field setup value.",
    {frame: "ETRS89", epoch: epochOf("2020.0"), current: "ETRS89", current_epoch: epochOf("2010.0")})), true);
  const noCurrentEpoch = frameSuggestionBox(kinds("differs", "ETRS89", "2010.0", "ITRF2014", ""), false);
  eq(lang + " missing epoch is said", texts(noCurrentEpoch).includes("(" + words["ui.frame_epoch_none"] + ")"), true);
  const noServiceEpoch = frameSuggestionBox(kinds("differs", "ETRS89", "", "ETRS89", "2010.0"), false);
  eq(lang + " missing service epoch is said", texts(noServiceEpoch).includes("(" + words["ui.frame_epoch_none"] + ")"), true);
  const same = frameSuggestionBox(kinds("matches", "ETRS89", "2010.0", "ETRS89", "2010.0"), false);
  eq(lang + " matches is a quiet hint", [same.tag, same.attrs.class, same.attrs["data-hint"], same.text(), same.querySelectorAll("button").length],
    ["p", "muted", "frame-confirmed", words["ui.frame_suggestion_matches"], 0]);
  const pending = frameSuggestionBox(kinds("differs", "ETRS89", "2020.0", "ETRS89", "2010.0"), true);
  eq(lang + " pending locks both", pending.querySelectorAll("button").map(node => node.disabled), [true, true]);
  eq(lang + " no box without a suggestion", frameSuggestionBox(null, false), []);
}
RtkI18n.setLanguage("en");
for (const key of ["ui.frame_suggestion_title", "ui.frame_suggestion_adopt", "ui.frame_suggestion_dismiss",
    "ui.frame_suggestion_matches", "ui.frame_suggestion_saved", "ui.frame_anchor_plate_fixed", "ui.frame_anchor_global", "ui.frame_epoch_none"])
  eq(key + " in both dictionaries", [typeof dict.en[key], typeof dict.de[key]], ["string", "string"]);
eq("dismiss text", [dict.en["ui.frame_suggestion_dismiss"], dict.de["ui.frame_suggestion_dismiss"]], ["Dismiss", "Verwerfen"]);
""")
        self.assertEqual(out["failures"], [])

    def test_adopt_sends_exactly_the_shown_values(self):
        out = self.run_node(r"""
answer = body => ({values: Object.assign({antenna_height_m: 1.8}, body), suggestion: suggestion("matches", body.reference_frame, body.reference_frame_epoch)});
const card = freshCard(fieldSetupCard(setup({}, {suggestion: suggestion("new", "ETRS89", "2010.0")})));
eq("box rendered by the card", buttons(card).length, 2);
const antenna = card.querySelector('input[name="antenna_height_m"]'), frame = card.querySelector('input[name="reference_frame"]'),
  epoch = card.querySelector('input[name="reference_frame_epoch"]');
antenna.value = "1.23";
await buttons(card)[0].click();
eq("one PUT with the shown values", puts, [["/api/field-setup", {reference_frame: "ETRS89", reference_frame_epoch: "2010.0"}]]);
eq("antenna input untouched", antenna.value, "1.23");
eq("frame fields are the saved values", [frame.value, epoch.value], ["ETRS89", "2010.0"]);
eq("success once", successes, [dict.en["ui.frame_suggestion_saved"]]);
eq("box shows the confirmation", shown(card).children.map(node => node.attrs["data-hint"]), ["frame-confirmed"]);
eq("lock released", frameAdoptPending, null);
// Only the epoch differs: the body carries exactly the reported epoch.
puts.length = 0;
const second = freshCard(fieldSetupCard(setup({reference_frame: "ETRS89", reference_frame_epoch: "2010.0"}, {suggestion: suggestion("differs", "ETRS89", "2020.0")})));
second.querySelector('input[name="antenna_height_m"]').value = "0.5";
await buttons(second)[0].click();
eq("epoch-only body", puts, [["/api/field-setup", {reference_frame: "ETRS89", reference_frame_epoch: "2020.0"}]]);
eq("antenna still untouched", second.querySelector('input[name="antenna_height_m"]').value, "0.5");
// A refused write leaves the fields alone, says so once and frees the buttons.
puts.length = 0; successes.length = 0;
answer = () => { throw new Error("refused"); };
const third = freshCard(fieldSetupCard(setup({reference_frame: "ITRF2014"}, {suggestion: suggestion("differs", "ETRS89", "2010.0")})));
const thirdFrame = third.querySelector('input[name="reference_frame"]');
thirdFrame.value = "typed";
await buttons(third)[0].click();
eq("refused: one PUT", puts.length, 1);
eq("refused: fields unchanged", thirdFrame.value, "typed");
eq("refused: one error, no success", [errors, successes], [["refused"], []]);
eq("refused: buttons free again", buttons(third).map(node => node.disabled), [false, false]);
eq("refused: lock released", frameAdoptPending, null);
""", asynchronous=True)
        self.assertEqual(out["failures"], [])

    def test_adopt_stays_pending_across_rerender_and_navigation(self):
        out = self.run_node(r"""
const loaded = setup({}, {suggestion: suggestion("new", "ETRS89", "2010.0")});
const card = freshCard(fieldSetupCard(loaded));
const first = buttons(card)[0];
let release = null;
hold = new Promise(resolve => { release = resolve; });
const running = first.click();
eq("buttons locked at once", buttons(card).map(node => node.disabled), [true, true]);
eq("the card is the pending one", frameAdoptPending === card, true);
// A new suggestion arrives while the PUT runs: the slot renders again, still locked, and a click sends nothing.
const next = suggestion("differs", "ETRS89", "2020.0");
syncFrameSlot(shown(card), frameSuggestionFor({frame_suggestion: next, measurement_defaults: {reference_frame: "ITRF2014", reference_frame_epoch: ""}}, loaded), frameAdoptPending === card);
const newest = buttons(card);
eq("new box, still locked", [shown(card).children[0].attrs.class, newest.map(node => node.disabled), newest[0] === first], ["notice warn", [true, true], false]);
await newest[0].click();
await adoptFrameSuggestion(frameSuggestionInfo(next, {frame: "ITRF2014", epoch: ""}, false));
eq("no second PUT", puts.length, 1);
// Navigation: the view is aborted and the card removed, a new card is mounted with unsaved text.
activeController.abort();
card.isConnected = false;
const later = freshCard(fieldSetupCard(setup({}, {})));
later.querySelector('input[name="reference_frame"]').value = "typed";
release({values: {reference_frame: "ETRS89", reference_frame_epoch: "2010.0"}, suggestion: suggestion("matches", "ETRS89", "2010.0")});
await running;
eq("late answer changes nothing", later.querySelector('input[name="reference_frame"]').value, "typed");
eq("no success after leaving", successes, []);
eq("no error after leaving", errors, []);
eq("lock cleared", frameAdoptPending, null);
eq("late answer did not touch the store", store.sets, 0);
""", asynchronous=True)
        self.assertEqual(out["failures"], [])

    def test_adopting_and_saving_never_overlap(self):
        out = self.run_node(r"""
const saveOf = card => card.querySelector('[data-action="save-field-setup"]');
const shownInfo = card => frameSuggestionInfo(suggestion("new", "ETRS89", "2010.0"), {frame: "", epoch: ""}, false);
answer = body => ({values: Object.assign({antenna_height_m: 1.8}, body), suggestion: suggestion("matches", body.reference_frame, body.reference_frame_epoch)});
// 1. A pending adoption: Save is off, a click and a direct call send nothing, the frame fields still hold the old values.
{
  const card = freshCard(fieldSetupCard(setup({reference_frame: "ITRF2014", reference_frame_epoch: ""}, {suggestion: suggestion("new", "ETRS89", "2010.0")})));
  const save = saveOf(card), frame = card.querySelector('input[name="reference_frame"]');
  eq("save is enabled at rest", [save.disabled, save.attrs["aria-busy"]], [false, undefined]);
  let release = null;
  hold = new Promise(resolve => { release = resolve; });
  const running = buttons(card)[0].click();
  eq("adoption: save disabled and busy", [save.disabled, save.attrs["aria-busy"]], [true, "true"]);
  eq("adoption: the frame input is still the old value", frame.value, "ITRF2014");
  await save.click();
  await saveFieldSetup();
  eq("adoption: a Save sends no PUT", puts.length, 1);
  eq("adoption: no message from the refused Save", [successes, errors], [[], []]);
  eq("adoption: the lock is the adoption", [frameAdoptPending === card, fieldSetupSaving], [true, null]);
  release({values: {reference_frame: "ETRS89", reference_frame_epoch: "2010.0"}, suggestion: suggestion("matches", "ETRS89", "2010.0")});
  await running;
  hold = null;
  eq("adopted: save enabled again", [save.disabled, save.attrs["aria-busy"]], [false, undefined]);
  eq("adopted: the frame inputs show the saved values", [frame.value, card.querySelector('input[name="reference_frame_epoch"]').value], ["ETRS89", "2010.0"]);
  successes.length = 0;
  await save.click();
  eq("a following Save sends the adopted frame", puts[1], ["/api/field-setup", {antenna_height_m: "1.8", reference_frame: "ETRS89", reference_frame_epoch: "2010.0"}]);
  eq("the Save is reported once, nothing is left locked", [successes, errors, frameAdoptPending, fieldSetupSaving, save.disabled], [[dict.en["ui.field_setup_saved"]], [], null, null, false]);
}
// 2. A pending save: Adopt and Dismiss are off, a click sends nothing, a second Save sends nothing.
{
  puts.length = 0; successes.length = 0;
  const card = freshCard(fieldSetupCard(setup({}, {suggestion: suggestion("new", "ETRS89", "2010.0")})));
  const save = saveOf(card);
  let release = null;
  hold = new Promise(resolve => { release = resolve; });
  const running = save.click();
  eq("save: the lock is the save", [fieldSetupSaving === card, frameAdoptPending], [true, null]);
  eq("save: Adopt and Dismiss disabled", buttons(card).map(node => node.disabled), [true, true]);
  await buttons(card)[0].click();
  await adoptFrameSuggestion(shownInfo(card));
  await save.click();
  await saveFieldSetup();
  eq("save: nothing else is sent", puts.length, 1);
  // A foreign suggestion arriving meanwhile renders locked too.
  syncFrameSlot(shown(card), frameSuggestionFor(null, setup({}, {suggestion: suggestion("differs", "ETRS89", "2020.0")})), fieldSetupWriting(card));
  eq("save: a new box is locked", buttons(card).map(node => node.disabled), [true, true]);
  release({values: {antenna_height_m: 1.8}});
  await running;
  hold = null;
  eq("saved: buttons free again", buttons(card).map(node => node.disabled), [false, false]);
  eq("saved: one success, lock released", [successes, errors, fieldSetupSaving], [[dict.en["ui.field_setup_saved"]], [], null]);
  await buttons(card)[0].click();
  eq("saved: Adopt works again", puts.length, 2);
  eq("saved: the adoption body is the box shown now", puts[1], ["/api/field-setup", {reference_frame: "ETRS89", reference_frame_epoch: "2020.0"}]);
}
// 3. A refused save frees the adoption again.
{
  puts.length = 0; successes.length = 0;
  const card = freshCard(fieldSetupCard(setup({}, {suggestion: suggestion("new", "ETRS89", "2010.0")})));
  answer = () => { throw new Error("refused"); };
  await saveOf(card).click();
  eq("refused save: one error", [errors, successes], [["refused"], []]);
  eq("refused save: buttons and lock free", [buttons(card).map(node => node.disabled), fieldSetupSaving, saveOf(card).disabled], [[false, false], null, false]);
  errors.length = 0;
}
// 4. The card is gone while the save runs: the result is still reported once, nothing is rendered into the old card,
// and only its own lock is released.
{
  puts.length = 0; successes.length = 0; errors.length = 0;
  const gonePair = () => {
    const card = freshCard(fieldSetupCard(setup({}, {suggestion: suggestion("new", "ETRS89", "2010.0")})));
    let release = null, fail = null;
    hold = new Promise((resolve, reject) => { release = resolve; fail = reject; });
    const running = saveOf(card).click();
    activeController.abort();
    card.isConnected = false;
    const later = freshCard(fieldSetupCard(setup({}, {suggestion: suggestion("new", "ETRS89", "2010.0")})));
    fieldSetupSaving = later;
    return {card, later, release, fail, running};
  };
  const done = pair => { hold = null; eq("gone: only the new card's lock stays", fieldSetupSaving === pair.later, true); fieldSetupSaving = null; };
  answer = body => ({values: body});
  const ok = gonePair();
  ok.release({values: {antenna_height_m: 2.5, reference_frame: "ITRF2020", reference_frame_epoch: "2020.0"}});
  await ok.running;
  eq("gone, saved: reported once", [successes, errors], [[dict.en["ui.field_setup_saved"]], []]);
  eq("gone, saved: the old inputs are untouched", ["antenna_height_m", "reference_frame", "reference_frame_epoch"].map(name => ok.card.querySelector('input[name="' + name + '"]').value), ["1.8", "", ""]);
  eq("gone, saved: the new card is untouched", ["antenna_height_m", "reference_frame", "reference_frame_epoch"].map(name => ok.later.querySelector('input[name="' + name + '"]').value), ["1.8", "", ""]);
  done(ok);
  successes.length = 0;
  const refused = gonePair();
  refused.fail(new Error("refused"));
  await refused.running;
  eq("gone, refused: reported once", [successes, errors], [[], ["refused"]]);
  done(refused);
}
""", asynchronous=True)
        self.assertEqual(out["failures"], [])

    def test_suggestion_box_follows_foreign_changes_and_a_late_anchor(self):
        out = self.run_node(r"""
const loaded = setup({reference_frame: "ITRF2014", reference_frame_epoch: ""}, {suggestion: suggestion("differs", "ETRS89", "2010.0")});
loadedSetup = loaded;
store.state.live = null;
const controller = new AbortController();
activeController = controller;
await mountFieldSetup({params: {}}, controller.signal);
const card = main.querySelector('[data-card="field-setup"]'), slot = shown(card);
const frame = card.querySelector('input[name="reference_frame"]');
frame.value = "typed by hand";
const rendered = () => slot.children[0].text();
eq("initial box names the loaded frame", rendered().includes("ITRF2014"), true);
let renders = 0;
const original = slot.replaceChildren.bind(slot);
slot.replaceChildren = (...items) => { renders += 1; original(...items); };
const live = (current, suggested) => ({measurement_defaults: {samples: 5, reference_frame: current, reference_frame_epoch: ""}, frame_suggestion: suggested});
store.set({live: live("ITRF2020", suggestion("differs", "ETRS89", "2010.0"))});
eq("a foreign change shows its frame", [rendered().includes("ITRF2020"), rendered().includes("ITRF2014")], [true, false]);
eq("one render", renders, 1);
store.set({live: live("ITRF2020", suggestion("differs", "ETRS89", "2010.0"))});
eq("identical data does not render again", renders, 1);
store.set({live: live("ITRF2020", suggestion("differs", "ETRS89", "2010.0", "plate_fixed"))});
eq("the late anchor appears", [rendered().includes("ETRS89 (plate-fixed)"), renders], [true, 2]);
store.set({live: live("ETRS89", suggestion("matches", "ETRS89", "2010.0", "plate_fixed"))});
eq("matches turns the box quiet", [slot.children.length, slot.children[0].attrs["data-hint"]], [1, "frame-confirmed"]);
store.set({live: live("ETRS89", null)});
eq("no suggestion empties the slot", slot.children, []);
eq("typed text survives every render", frame.value, "typed by hand");
eq("only the slot was rendered", main.querySelector('[data-card="field-setup"]') === card, true);
controller.abort();
const before = renders;
store.set({live: live("ETRS89", suggestion("new", "ETRS89", "2010.0"))});
eq("a left page is not rendered any more", renders, before);
""", asynchronous=True, prefixes=SUGGESTION_PREFIXES + (
            "const MEASUREMENT_METHODS=", "function measurementSamples(", "function liveMethod(", "function syncMethodChoice(",
            "async function mountFieldSetup("))
        self.assertEqual(out["failures"], [])

    def test_field_setup_card_has_the_suggestion_slot(self):
        out = self.run_node(r"""
const card = fieldSetupCard(setup({}, {suggestion: suggestion("new", "ETRS89", "2010.0")}));
const nodes = card.all();
const slots = nodes.filter(node => node.attrs["data-slot"] === "frame-suggestion");
eq("one slot", slots.length, 1);
eq("the box is inside", [slots[0].querySelectorAll('[data-notice="frame-suggestion"]').length], [1]);
eq("the slot comes before the frame input", nodes.indexOf(slots[0]) < nodes.findIndex(node => node.attrs.id === "field-setup-reference-frame"), true);
const empty = fieldSetupCard(setup({}, {suggestion: null}));
const slot = empty.all().find(node => node.attrs["data-slot"] === "frame-suggestion");
eq("an empty slot without a suggestion", slot && slot.children, []);
eq("no suggestion key at all", fieldSetupCard(setup({})).all().filter(node => node.attrs["data-slot"] === "frame-suggestion").length, 1);
""")
        self.assertEqual(out["failures"], [])


# #38b: Measure shows the reported frame in the reminder and warns when it differs from the field setup.
# The summary card is drawn by the real renderFieldSummary() into the element model of the tests above.
SUMMARY_WARNING_PREFIXES = tuple(dict.fromkeys(SUGGESTION_PREFIXES + (
    "let staleSinceSeq=", "function fixIsUsable(", "const MEASURE_ROUTE=", "const ROUTES=", "const LEGACY_ROUTES=",
    "function resolveRoute(", "function routeHash(", "let frameReminderHidden=", "function frameReminderDismissed(",
    "function dismissFrameReminder(", "function frameReminderInfo(", "const fmt=", "const sampleLabel=",
    "const MEASUREMENT_METHODS=", "function measurementSamples(", "function fieldSummaryRows(",
    "function frameDiffersNotice(", "function renderFieldSummary(")))
SUMMARY_WARNING_STUBS = SUGGESTION_STUBS + r"""
const sessionData = new Map();
globalThis.sessionStorage = {getItem: key => sessionData.has(key) ? sessionData.get(key) : null,
  setItem: (key, value) => sessionData.set(key, String(value))};
let statusRefs = null;
"""


class TestMeasureFrameWarning(unittest.TestCase):

    def run_node(self, body, prefixes=SUMMARY_WARNING_PREFIXES, stubs=SUMMARY_WARNING_STUBS):
        return navigation().run_app_node(prefixes, body, stubs=stubs)

    def run_reminder(self, body):
        # The reminder helper alone, with the same declarations as the #24-UI test of its conditions.
        module = navigation()
        return module.run_app_node(module.REMINDER_PREFIXES, body, stubs=module.SESSION_STUB)

    def test_reminder_without_suggestion_is_unchanged(self):
        out = self.run_reminder(r"""
const base = () => ({measurement_defaults: {reference_frame: "", samples: 5, antenna_height_m: 0},
  current_fix: {qual: 4, fix_seq: 9, stale: false}});
const variant = patch => { const live = base(); patch(live); return live; };
const cases = [
  ["rtk fixed", base(), true], ["rtk float", variant(live => { live.current_fix.qual = 5; }), true],
  ["dgps", variant(live => { live.current_fix.qual = 2; }), false],
  ["stale fix", variant(live => { live.current_fix.stale = true; }), false],
  ["no fix", variant(live => { delete live.current_fix; }), false],
  ["blank frame", variant(live => { live.measurement_defaults.reference_frame = "  "; }), true],
  ["frame set", variant(live => { live.measurement_defaults.reference_frame = "ETRS89"; }), false],
  ["frame null", variant(live => { live.measurement_defaults.reference_frame = null; }), true],
  ["key missing", variant(live => { delete live.measurement_defaults.reference_frame; }), false],
  ["defaults null", variant(live => { live.measurement_defaults = null; }), false]];
for (const [name, live, shown] of cases) {
  const without = frameReminderInfo(live, false);
  eq(name + ": shown as before", without !== null, shown);
  const none = frameReminderInfo(Object.assign({}, live, {frame_suggestion: null}), false);
  eq(name + ": a null suggestion changes nothing", none !== null, shown);
  if (shown) eq(name + ": nothing suggested", [without.suggested === null, none.suggested === null, none.route, none.text], [true, true, "measure/setup/frame", without.text]);
}
eq("dismissed", frameReminderInfo(Object.assign(base(), {frame_suggestion: null}), true), null);
eq("no live", frameReminderInfo(null, false), null);
""")
        self.assertEqual(out["failures"], [])

    def test_reminder_names_the_reported_frame(self):
        out = self.run_reminder(r"""
const suggestion = (state, frame, epoch) => ({reference_frame: frame, reference_frame_epoch: epoch, anchor: null, state});
const live = reported => ({measurement_defaults: {reference_frame: "", samples: 5}, current_fix: {qual: 4, fix_seq: 9, stale: false},
  frame_suggestion: reported});
eq("new names the frame", frameReminderInfo(live(suggestion("new", "ETRS89", "2010.0")), false).suggested, "ETRS89");
eq("differs names nothing", frameReminderInfo(live(suggestion("differs", "ETRS89", "2010.0")), false).suggested, null);
eq("matches names nothing", frameReminderInfo(live(suggestion("matches", "ETRS89", "2010.0")), false).suggested, null);
for (const bad of [{state: "new"}, suggestion("new", "  ", ""), {state: "new", reference_frame: 5}, "new", []])
  eq("invalid " + JSON.stringify(bad), frameReminderInfo(live(bad), false).suggested, null);
eq("the condition is unchanged: frame set", frameReminderInfo(Object.assign(live(suggestion("new", "ETRS89", "")),
  {measurement_defaults: {reference_frame: "ITRF2014"}}), false), null);
eq("the condition is unchanged: no RTK fix", frameReminderInfo(Object.assign(live(suggestion("new", "ETRS89", "")),
  {current_fix: {qual: 1, fix_seq: 9, stale: false}}), false), null);
eq("dismissed reminder stays hidden", frameReminderInfo(live(suggestion("new", "ETRS89", "")), true), null);
""")
        self.assertEqual(out["failures"], [])

    def test_measure_summary_warns_about_a_differing_frame(self):
        out = self.run_node(r"""
const fixed = {qual: 4, fix_seq: 9, stale: false};
const live = (frame, epoch, reported) => ({current_fix: fixed, frame_suggestion: reported,
  measurement_defaults: {samples: 5, antenna_height_m: 1.8, reference_frame: frame, reference_frame_epoch: epoch}});
let renders = 0;
const card = new El("div", {}, []);
const original = card.replaceChildren.bind(card);
card.replaceChildren = (...items) => { renders += 1; original(...items); };
statusRefs = {setup: card};
const draw = value => { store.state.live = value; renderFieldSummary(value); };
const warnings = () => card.querySelectorAll('[data-notice="frame-differs"]');
const reminders = () => card.querySelectorAll('[data-notice="frame-reminder"]');
const reset = () => { frameSuggestionHidden = null; sessionData.clear(); };
for (const lang of ["en", "de"]) {
  RtkI18n.setLanguage(lang);
  reset();
  const words = dict[lang], v2 = RtkI18n.v2de, fill = (template, values) => (lang === "de" ? v2[template] : template)
    .replace(/\{(\w+)\}/g, (all, key) => values[key]);
  const epochOf = value => fill("epoch {epoch}", {epoch: value});
  const template = "The correction service reports {frame} ({epoch}), the field setup says {current} ({current_epoch}). New RTK points are saved with the field setup value.";
  // Another frame: one warning with both names and both epoch statements.
  draw(live("ITRF2014", "", suggestion("differs", "ETRS89", "2010.0")));
  eq(lang + " one warning", warnings().length, 1);
  const warning = warnings()[0];
  eq(lang + " warning class", warning.attrs.class, "notice warn");
  eq(lang + " both frames, one missing epoch said", warning.text().includes(fill(template,
    {frame: "ETRS89", epoch: epochOf("2010.0"), current: "ITRF2014", current_epoch: words["ui.frame_epoch_none"]})), true);
  eq(lang + " link to the field setup", warning.querySelectorAll("a").map(node => [node.attrs.href, node.attrs.text]),
    [["/#measure/setup/frame", words["ui.frame_reminder_open"]]]);
  eq(lang + " dismiss button", warning.querySelectorAll("button").map(node => node.attrs.text), [words["ui.frame_suggestion_dismiss"]]);
  eq(lang + " no reminder next to it", reminders().length, 0);
  // Only the epoch differs: both epochs are in the text.
  draw(live("ETRS89", "2010.0", suggestion("differs", "ETRS89", "2020.0")));
  eq(lang + " epoch-only: one warning", warnings().length, 1);
  eq(lang + " epoch-only: both epochs", warnings()[0].text().includes(fill(template,
    {frame: "ETRS89", epoch: epochOf("2020.0"), current: "ETRS89", current_epoch: epochOf("2010.0")})), true);
  // A changed epoch of the field setup alone renders again; the same data twice does not.
  const before = renders;
  draw(live("ETRS89", "2015.0", suggestion("differs", "ETRS89", "2020.0")));
  eq(lang + " a foreign epoch change renders", [renders, warnings()[0].text().includes(epochOf("2015.0"))], [before + 1, true]);
  draw(live("ETRS89", "2015.0", suggestion("differs", "ETRS89", "2020.0")));
  eq(lang + " identical live data does not render again", renders, before + 1);
  draw(live("ETRS89", "2015.0", suggestion("differs", "ETRS89", "2020.0", "plate_fixed")));
  eq(lang + " a late anchor renders", [renders, warnings()[0].text().includes("ETRS89 (" + words["ui.frame_anchor_plate_fixed"] + ")")], [before + 2, true]);
  // Dismissing hides it for this suggestion, shows it again for another one.
  warnings()[0].querySelectorAll("button")[0].click();
  eq(lang + " dismissed: no warning", warnings().length, 0);
  eq(lang + " focus returns to the edit link", card.querySelector(".setup-edit").focused, true);
  eq(lang + " dismissed per signature", sessionData.get("rtk-frame-suggestion"), "ETRS89|2020.0");
  const afterDismiss = renders;
  draw(live("ETRS89", "2015.0", suggestion("differs", "ETRS89", "2020.0")));
  eq(lang + " still none after a render", [warnings().length, renders], [0, afterDismiss]);
  draw(live("ETRS89", "2015.0", suggestion("differs", "ETRS89", "2030.0")));
  eq(lang + " another suggestion shows again", warnings().length, 1);
  // Matching and a missing suggestion warn about nothing.
  reset();
  draw(live("ETRS89", "2010.0", suggestion("matches", "ETRS89", "2010.0")));
  eq(lang + " matches: none", warnings().length, 0);
  draw(live("ETRS89", "2010.0", null));
  eq(lang + " null: none", warnings().length, 0);
  draw(live("ETRS89", "2010.0", undefined));
  eq(lang + " no key: none", warnings().length, 0);
  // Service data stays text.
  draw(live("ITRF2014", "", suggestion("differs", "<b>x</b>", "2010.0")));
  eq(lang + " names are text", [warnings()[0].all().some(node => node.tag === "b"), warnings()[0].text().includes("<b>x</b>")], [false, true]);
  // The reminder names the reported frame; without a report it reads as before.
  reset();
  draw(live("", "", suggestion("new", "ETRS89", "2010.0")));
  eq(lang + " reminder shown, no warning", [reminders().length, warnings().length], [1, 0]);
  eq(lang + " reminder text kept", reminders()[0].text().includes(words["ui.frame_reminder"]), true);
  eq(lang + " reminder names the frame", reminders()[0].text().includes(fill("The correction service reports {frame}.", {frame: "ETRS89"})), true);
  draw(live("", "", suggestion("new", "ITRF2020", "")));
  eq(lang + " a new reported frame renders again", reminders()[0].text().includes("ITRF2020"), true);
  draw(live("", "", null));
  eq(lang + " no report: the plain reminder", [reminders().length, reminders()[0].text().includes("ITRF2020"),
    reminders()[0].text().includes(fill("The correction service reports {frame}.", {frame: ""}))], [1, false, false]);
}
const german = RtkI18n.v2de["The correction service reports {frame}."];
eq("German sentence is translated and keeps the placeholder", [typeof german, german !== "The correction service reports {frame}.", String(german).includes("{frame}")], ["string", true, true]);
eq("dismiss is translated", dict.de["ui.frame_suggestion_dismiss"] !== dict.en["ui.frame_suggestion_dismiss"], true);
RtkI18n.setLanguage("en");
""")
        self.assertEqual(out["failures"], [])


# M2 #46: the pure helpers of the visible GeoJSON export flow (src/68-export-flow.js). German wording is
# pinned through the dictionary; words the language policy flags are assembled from two parts.
EXPORT_FLOW_PREFIXES = (
    "const EXPORT_STALL_MS=", "function exportElapsedText(", "function exportSizeText(", "function jsonEnvelopeStart(",
    "function jsonEnvelopeScan(", "function jsonEnvelopeComplete(", "function exportFailure(",
    "function exportShareOutcome(", "function exportPhaseText(", "function exportMetaText(", "function exportBusy(")


class ExportFlowHelperTests(unittest.TestCase):
    def setUp(self):
        if NODE is None:
            self.skipTest("node is unavailable")

    def test_elapsed_and_size_texts(self):
        out = run_freshness_node(r"""
eq("elapsed", [0, 999, 61000, 3599000, 3600000, 3723000, -5, undefined, NaN].map(exportElapsedText),
  ["0:00", "0:00", "1:01", "59:59", "1:00:00", "1:02:03", "0:00", "0:00", "0:00"]);
eq("stall and tick constants", [EXPORT_STALL_MS, EXPORT_TICK_MS], [30000, 1000]);
RtkI18n.setLanguage("en");
eq("en sizes", [0, 99999, 100000, 4786856].map(exportSizeText), ["0 kB", "100 kB", "0.1 MB", "4.8 MB"]);
RtkI18n.setLanguage("de");
eq("de sizes", [0, 4786856].map(exportSizeText), ["0 kB", "4,8 MB"]);
RtkI18n.setLanguage("en");
for (const text of [0, 61000, 3723000].map(exportElapsedText).concat([0, 99999, 4786856].map(exportSizeText)))
  eq("no percent sign in " + text, text.includes("%"), false);
""", extra_prefixes=EXPORT_FLOW_PREFIXES)
        self.assertEqual(out["failures"], [])

    def test_envelope_is_complete_only_for_the_whole_document(self):
        out = run_freshness_node(r"""
const encode = text => new TextEncoder().encode(text);
const sample = JSON.stringify({type: "FeatureCollection", name: 'A "}] \\ \u00dc', features: [{geometry: {type: "LineString",
  coordinates: [[8.1, 50.2, 1.5], [8.2, 50.3, 1.6]]}, properties: {m: [{x: "]}"}]}}]});
const bytes = encode(sample);
const scan = (data, size) => { const state = jsonEnvelopeStart();
  for (let at = 0; at < data.length; at += size) jsonEnvelopeScan(state, data.subarray(at, at + size)); return state; };
eq("start state", jsonEnvelopeStart(), {depth: 0, inString: false, escape: false, opened: false, closed: false, invalid: false});
const prefixes = [];
for (let k = 0; k <= bytes.length; k += 1) { const state = scan(bytes.subarray(0, k), bytes.length || 1);
  if (jsonEnvelopeComplete(state) !== (k === bytes.length)) prefixes.push(k); }
eq("complete exactly for the whole document", prefixes, []);
for (const size of [1, 7, 4096]) { const state = scan(bytes, size);
  eq("chunks of " + size, [jsonEnvelopeComplete(state), state.invalid, state.depth], [true, false, 0]); }
eq("scan returns its state", (() => { const state = jsonEnvelopeStart(); return jsonEnvelopeScan(state, bytes) === state; })(), true);
eq("trailing white space", jsonEnvelopeComplete(scan(encode(sample + "\n  \t\r"), 5)), true);
const junk = scan(encode(sample + "x"), 5);
eq("trailing byte", [jsonEnvelopeComplete(junk), junk.invalid], [false, true]);
const html = scan(encode("<html>"), 3);
eq("html", [jsonEnvelopeComplete(html), html.invalid], [false, true]);
const empty = scan(encode(""), 1);
eq("empty", [jsonEnvelopeComplete(empty), empty.invalid], [false, false]);
eq("empty array", jsonEnvelopeComplete(scan(encode("[]"), 1)), true);
eq("white space before the document", jsonEnvelopeComplete(scan(encode(" \n{}"), 2)), true);
eq("unbalanced close", scan(encode("{}}"), 3).invalid, true);
eq("close before open", scan(encode("]"), 1).invalid, true);
eq("open string is incomplete", jsonEnvelopeComplete(scan(encode('{"a":"b'), 2)), false);
eq("escaped quote does not end the string", jsonEnvelopeComplete(scan(encode('{"a":"\\"}'), 1)), false);
""", extra_prefixes=EXPORT_FLOW_PREFIXES)
        self.assertEqual(out["failures"], [])

    def test_failure_and_share_outcomes(self):
        out = run_freshness_node(r"""
const kind = (error, reason) => exportFailure(error, reason).kind;
eq("user", kind(null, "user"), "cancelled");
eq("route", kind(null, "route"), "route");
eq("stall", kind(null, "stall"), "stalled");
eq("reason wins over the error", kind({status: 503, message: "x"}, "user"), "cancelled");
eq("session", kind(new Error("Session expired")), "session");
eq("type error", kind({name: "TypeError"}), "lost");
eq("no error", kind(undefined), "lost");
eq("abort without reason", kind({name: "AbortError"}), "lost");
eq("float status", kind({status: 1.5, message: "x"}), "lost");
eq("string status", kind({status: "503", message: "x"}), "lost");
const refused = exportFailure({status: 503, message: "Storage maintenance in progress. Please wait."});
eq("refused", [refused.kind, refused.text], ["refused", "Storage maintenance in progress. Please wait."]);
eq("outcomes", [exportShareOutcome(undefined), exportShareOutcome(null), exportShareOutcome({name: "AbortError"}),
  exportShareOutcome({name: "NotAllowedError"}), exportShareOutcome(new Error("x"))],
  ["shared", "shared", "share_cancelled", "share_unavailable", "share_unavailable"]);
""", extra_prefixes=EXPORT_FLOW_PREFIXES)
        self.assertEqual(out["failures"], [])

    def test_export_texts_exist_in_both_languages(self):
        out = run_freshness_node(r"""
const flow = ["region", "start", "retry", "cancel", "discard", "preparing", "transferring", "meta", "meta_done", "ready",
  "shared", "share_cancelled", "share_unavailable", "downloaded", "cancelled", "stalled", "lost", "refused", "incomplete",
  "close_title", "close_text", "close_stay", "close_confirm"].map(name => "ui.export_flow_" + name);
for (const key of flow) {
  eq(key + " en", typeof dict.en[key] === "string" && dict.en[key].length > 0, true);
  eq(key + " de", typeof dict.de[key] === "string" && dict.de[key].length > 0 && dict.de[key] !== dict.en[key], true);
}
eq("flow key count", Object.keys(dict.en).filter(key => key.startsWith("ui.export_flow_")).length, flow.length);
const other = language => Object.keys(dict[language]).filter(key => key.startsWith("ui.export_") && !key.startsWith("ui.export_flow_")).sort();
// ui.export_project is an unused leftover of the removed library export card; #46 adds no key beside the flow keys.
const expected = ["ui.export_title", "ui.export_share_hint", "ui.export_geojson", "ui.export_csv", "ui.export_archive",
  "ui.export_share", "ui.export_download", "ui.export_project"].sort();
eq("no further non-flow export keys (en)", other("en"), expected);
eq("no further non-flow export keys (de)", other("de"), expected);
eq("share wording", [dict.en["ui.export_share"], dict.de["ui.export_share"]], ["Share file", "Dat" + "ei teilen"]);
eq("download wording", [dict.en["ui.export_download"], dict.de["ui.export_download"]], ["Download file", "Dat" + "ei herunterladen"]);
eq("close dialog wording de", [dict.de["ui.export_flow_close_title"], dict.de["ui.export_flow_close_stay"], dict.de["ui.export_flow_close_confirm"]],
  ["Export abbrechen?", "Bleiben", "Abbrechen"]);
eq("close dialog wording en", [dict.en["ui.export_flow_close_title"], dict.en["ui.export_flow_close_stay"], dict.en["ui.export_flow_close_confirm"]],
  ["Cancel export?", "Keep exporting", "Cancel export"]);
for (const language of ["en", "de"]) {
  RtkI18n.setLanguage(language);
  eq(language + " stalled names the seconds", exportPhaseText("stalled", {seconds: 30}).includes("30"), true);
  eq(language + " refused ends with the message", exportPhaseText("refused", {message: "x"}).endsWith("x"), true);
  eq(language + " ready names the size", exportPhaseText("ready", {size: "4.8 MB"}).includes("4.8 MB"), true);
  eq(language + " a device message stays verbatim", exportPhaseText("refused", {message: "a<b & \"c\" $& $1"}).endsWith('a<b & "c" $& $1'), true);
  for (const outcome of ["preparing", "transferring", "ready", "shared", "share_cancelled", "share_unavailable", "downloaded",
      "cancelled", "stalled", "lost", "refused", "incomplete"])
    eq(language + " text for " + outcome, exportPhaseText(outcome, {size: "1 kB", message: "m", seconds: 30}), dict[language]["ui.export_flow_" + outcome]
      .replace("{size}", "1 kB").replace("{message}", "m").replace("{seconds}", "30"));
  eq(language + " route and session are silent", [exportPhaseText("route", {}), exportPhaseText("session", {})], ["", ""]);
  eq(language + " meta", exportMetaText(61000, 4786856, false), dict[language]["ui.export_flow_meta"]
    .replace("{elapsed}", "1:01").replace("{size}", exportSizeText(4786856)));
  eq(language + " meta done", exportMetaText(61000, 4786856, true), dict[language]["ui.export_flow_meta_done"]
    .replace("{elapsed}", "1:01").replace("{size}", exportSizeText(4786856)));
}
RtkI18n.setLanguage("en");
""", extra_prefixes=EXPORT_FLOW_PREFIXES)
        self.assertEqual(out["failures"], [])

    def test_export_busy_phases(self):
        out = run_freshness_node(r"""
eq("busy", ["preparing", "transferring"].map(exportBusy), [true, true]);
eq("not busy", ["idle", "ready", "cancelled", "failed", "", undefined, null].map(exportBusy), [false, false, false, false, false, false, false]);
""", extra_prefixes=EXPORT_FLOW_PREFIXES)
        self.assertEqual(out["failures"], [])


def app_function_source(prefix):
    """One app.js function: its single line, or up to the closing brace at column 0 when its first line ends with '{'."""
    lines = [line for line in APP_JS.split("\n") if line.startswith(prefix)]
    if len(lines) != 1:
        raise AssertionError("%s must be declared exactly once" % prefix)
    if not lines[0].endswith("{"):
        return lines[0]
    start = APP_JS.index("\n" + prefix) + 1
    return APP_JS[start:APP_JS.index("\n}\n", start) + 2]


# M2 #46 Task 7: a small fake DOM for the export component and its close question. Elements keep data-* in dataset,
# clicks run their listeners, dialogs open with showModal() and run their close listeners once per close; a disabled element takes no focus.
EXPORT_DOM_STUBS = r"""
let focused = null;
const downloads = [];
const camel = name => name.replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
class El {
  constructor(tag) { this.tag = tag; this.attrs = {}; this.dataset = {}; this.children = []; this.listeners = {}; this.hidden = false;
    this.disabled = false; this.own = ""; this.writes = 0; this.className = ""; this.isConnected = true; this.open = false; this.returnValue = "";
    this.classList = {add() {}, remove() {}}; }
  get textContent() { return this.own + this.children.map(child => child.textContent).join(""); }
  set textContent(value) { this.own = String(value); this.children = []; this.writes += 1; }
  setAttribute(name, value) { if (name.startsWith("data-")) this.dataset[camel(name.slice(5))] = String(value); else this.attrs[name] = String(value); }
  getAttribute(name) { return name.startsWith("data-") ? this.dataset[camel(name.slice(5))] ?? null : this.attrs[name] ?? null; }
  removeAttribute(name) { delete this.attrs[name]; }
  addEventListener(name, fn) { (this.listeners[name] = this.listeners[name] || []).push(fn); }
  append(...nodes) { nodes.forEach(node => { node.isConnected = true; }); this.children.push(...nodes); }
  replaceChildren(...nodes) { this.own = ""; this.children = nodes; }
  remove() { this.isConnected = false; }
  focus() { if (!this.disabled) focused = this; }
  click() { if (this.tag === "a") downloads.push([this.href, this.download, this.isConnected]); (this.listeners.click || []).forEach(fn => fn({})); }
  showModal() { this.open = true; }
  close(value) { if (!this.open) return; this.open = false; if (value !== undefined) this.returnValue = value;
    const handlers = this.listeners.close || []; this.listeners.close = []; handlers.forEach(fn => fn()); }
}
const h = (tag, attrs, ...children) => { const node = new El(tag);
  for (const [key, value] of Object.entries(attrs || {})) { if (value == null || value === false && !key.startsWith("aria-")) continue;
    if (key === "class") node.className = value; else if (key === "text" || key === "textRaw") node.own = String(value);
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (["hidden", "disabled", "type", "href", "download", "id"].includes(key)) node[key] = value; else node.setAttribute(key, value === true ? "" : value); }
  children.flat(Infinity).forEach(child => node.children.push(child instanceof El ? child : Object.assign(new El("#text"), {own: String(child)})));
  return node; };
const replace = (target, ...children) => target.replaceChildren(...children.flat(Infinity));
const all = (node, found = []) => { found.push(node); node.children.forEach(child => all(child, found)); return found; };
const find = (node, test) => all(node).filter(test);
const settle = () => new Promise(resolve => setTimeout(resolve, 5));
const fill = (template, values) => Object.keys(values).reduce((text, key) => text.split("{" + key + "}").join(values[key]), template);
const errors = [];
const showError = error => errors.push(String(error && error.message || error));
"""
# The environment of exportGeojsonControl: a controllable clock, interval and visibility, the API (headers, chunks, end,
# network error, refusal), object URLs, the close hook registry, the close question and the dock.
EXPORT_COMPONENT_STUBS = EXPORT_DOM_STUBS + r"""
let clock = 1000, navigator = {};
const intervals = [], warnings = [], guardCalls = [], guards = new Map(), questions = [], revoked = [], requests = [], docListeners = {};
let created = 0;
const performance = {now: () => clock};
const setInterval = (fn, ms) => { intervals.push({fn, ms, live: true}); return intervals.length; };
const clearInterval = id => { if (intervals[id - 1]) intervals[id - 1].live = false; };
const tick = () => intervals.filter(item => item.live).forEach(item => item.fn());
const liveIntervals = () => intervals.filter(item => item.live).length;
const document = {visibilityState: "visible", get activeElement() { return focused; },
  addEventListener(name, fn) { (docListeners[name] = docListeners[name] || []).push(fn); },
  removeEventListener(name, fn) { docListeners[name] = (docListeners[name] || []).filter(item => item !== fn); }};
const visibility = state => { document.visibilityState = state; (docListeners.visibilitychange || []).slice().forEach(fn => fn()); };
const URL = {createObjectURL: () => { created += 1; return "blob:" + created; }, revokeObjectURL: url => revoked.push(url)};
const showWarning = (text, key) => warnings.push([key, text]);
const showFeedback = spec => warnings.push([spec.key, spec.text]);
const guardExportSheetClose = (signal, guard) => { guardCalls.push(guard === null ? null : typeof guard); if (guard === null) guards.delete(signal); else guards.set(signal, guard); };
const exportCloseQuestion = signal => { let answer; const question = {signal, dialog: {closed: null, close(value) { if (this.closed !== null) return; this.closed = value; answer(value === "confirm"); }}};
  question.answer = new Promise(resolve => { answer = resolve; }); questions.push(question); return question; };
const api = {request(path, options) {
  const entry = {path, options, queue: [], waiting: null, released: 0, cancelled: 0, aborted: false};
  const deliver = item => { if (entry.waiting) { const waiting = entry.waiting; entry.waiting = null; if (item instanceof Error) waiting.reject(item); else waiting.resolve(item); } else entry.queue.push(item); };
  entry.reader = {read: () => entry.queue.length ? (item => item instanceof Error ? Promise.reject(item) : Promise.resolve(item))(entry.queue.shift())
      : new Promise((resolve, reject) => { entry.waiting = {resolve, reject}; }),
    releaseLock() { entry.released += 1; }, async cancel() { entry.cancelled += 1; }};
  entry.feed = text => deliver({done: false, value: new TextEncoder().encode(text)});
  entry.end = () => deliver({done: true});
  entry.fail = () => deliver(new TypeError("Failed to fetch"));
  requests.push(entry);
  return new Promise((resolve, reject) => {
    entry.respond = () => resolve({body: {getReader: () => entry.reader}});
    entry.refuse = error => reject(error);
    options.signal.addEventListener("abort", () => { entry.aborted = true; const error = Object.assign(new Error("Request cancelled"), {name: "AbortError"}); reject(error); deliver(error); });
  });
}};
// The sheet signal counts the abort listeners registered on it (added minus removed).
const sheet = () => { const controller = new AbortController(), signal = controller.signal, add = signal.addEventListener.bind(signal), remove = signal.removeEventListener.bind(signal);
  controller.listeners = 0;
  signal.addEventListener = (type, fn, options) => { if (type === "abort") controller.listeners += 1; add(type, fn, options); };
  signal.removeEventListener = (type, fn, options) => { if (type === "abort") controller.listeners -= 1; remove(type, fn, options); };
  return controller; };
const project = {id: "a/b", name: "Field"};
const DOC = '{"type":"FeatureCollection","name":"A \\"}]","features":[{"geometry":{"type":"Point","coordinates":[8.1,50.2]}}]}';
const part = (node, name) => find(node, item => item.className === name)[0];
const action = (node, name) => find(node, item => item.dataset.exportAction === name)[0] || null;
const actionNames = node => find(part(node, "export-flow-actions"), item => item.tag === "button").map(item => item.dataset.exportAction);
const view = node => ({phase: node.dataset.phase, outcome: node.dataset.outcome, text: part(node, "export-flow-phase").textContent,
  hidden: part(node, "export-flow-status").hidden, kind: part(node, "export-flow-status").dataset.kind});
async function runToReady(node, request) { request.respond(); await settle(); request.feed(DOC.slice(0, 20)); await settle(); request.feed(DOC.slice(20)); request.end(); await settle(); }
"""
EXPORT_COMPONENT_PREFIXES = EXPORT_FLOW_PREFIXES + ("function runButtonAction(", "function iconButton(")


def run_export_node(body, stubs, prefixes, functions=()):
    """Run an async Node body after the fake DOM stubs, single-line app.js declarations and whole functions."""
    script = FRESHNESS_NODE_PRELUDE + stubs + "\n" + freshness_declarations(prefixes) + "\n" + \
        "\n".join(app_function_source(prefix) for prefix in functions) + "\nconst out = {};\n(async () => {\n" + body + \
        "\n})().then(() => { out.failures = failures; console.log(JSON.stringify(out)); }," \
        " error => { console.error(error && error.stack || String(error)); process.exitCode = 1; });\n"
    run = subprocess.run([NODE, "-e", script], cwd=ROOT, capture_output=True, text=True, timeout=20)
    if run.returncode != 0:
        raise AssertionError(run.stderr)
    return json.loads(run.stdout)


class ExportFlowWiringTests(unittest.TestCase):
    """M2 #46 Task 7: exportGeojsonControl is the body of geojsonExportActions, with the close hook of the export sheet."""

    def setUp(self):
        if NODE is None:
            self.skipTest("node is unavailable")

    def component(self):
        return app_function_source("function exportGeojsonControl(")

    def test_geojson_export_goes_through_the_flow(self):
        self.assertNotIn("function shareProjectGeoJson(", APP_JS)
        self.assertNotIn("function fileShareSupported(", APP_JS)
        # The log page keeps its own text share; neither the project view nor the export flow has a share closure.
        for name in ("70-project-detail.js", "68-export-flow.js"):
            with open(os.path.join(ROOT, "src", name), encoding="utf-8") as handle:
                self.assertNotIn("const share=async", handle.read(), name)
        actions = app_function_source("function geojsonExportActions(")
        self.assertIn("exportGeojsonControl(", actions)
        self.assertIn("sheetSignal", actions)
        for absent in ("format=geojson", "addEventListener(", "showError("):
            self.assertNotIn(absent, actions)
        self.assertEqual(APP_JS.count("format=geojson"), 1)
        component = self.component()
        self.assertIn("format=geojson", component)
        sheet = app_function_source("function openProjectExport(")
        self.assertIn("geojsonExportActions(", sheet)
        self.assertNotIn("exportGeojsonControl(", sheet)
        for present in ("timeout:null", "getReader()", "releaseLock()", "revokeObjectURL", 'removeEventListener("abort"',
                        "jsonEnvelopeScan(", "navigator.canShare", "visibilitychange", 'tr("ui.export_share"',
                        'tr("ui.export_download"', 'data-export":"geojson-share', 'data-export":"geojson-download'):
            self.assertIn(present, component)
        for absent in ("response.blob()", "showError(", 'addEventListener("close"'):
            self.assertNotIn(absent, component)

    def test_geojson_actions_delegate_to_the_flow(self):
        out = run_freshness_node(r"""
const sentinel = {sentinel: true}, calls = [];
const exportGeojsonControl = (...args) => { calls.push(args); return sentinel; };
const signal = {aborted: false}, project = {id: "a/b", name: "Field"};
const actions = geojsonExportActions(project, signal);
eq("one element", Array.isArray(actions) ? actions.length : -1, 1);
eq("the flow", Array.isArray(actions) && actions[0] === sentinel, true);
eq("called once", calls.length, 1);
eq("with the project and the sheet signal", calls.length === 1 && calls[0][0] === project && calls[0][1] && calls[0][1].signal === signal
  && Object.keys(calls[0][1]).length === 1, true);
""", extra_prefixes=("function geojsonExportActions(",), stubs=r"""
const h = (tag, attrs) => ({tag, attrs: attrs || {}, dataset: {}});
const iconButton = (label, handler, kind) => ({tag: "button", attrs: {text: label}, dataset: {}});
const fileShareSupported = () => true;
const shareProjectGeoJson = async () => {};
""")
        self.assertEqual(out["failures"], [])

    def test_export_close_guard_is_registered(self):
        component = self.component()
        for present in ("guardExportSheetClose(", "exportBusy(", "exportCloseQuestion("):
            self.assertIn(present, component)
        question = app_function_source("function exportCloseQuestion(")
        for present in ("showModal()", "ui.export_flow_close_title", "ui.export_flow_close_stay", "ui.export_flow_close_confirm"):
            self.assertIn(present, question)
        for absent in ("modal(", "confirm("):
            self.assertNotIn(absent, question)
        rest = APP_JS.replace(app_function_source("function guardExportSheetClose("), "").replace(component, "")
        self.assertNotIn("guardExportSheetClose(", rest)
        self.assertNotIn("guardExportSheetClose(", app_function_source("function openProjectExport("))

    def test_export_meta_is_not_a_live_region(self):
        component = self.component()
        self.assertEqual(component.count("aria-live"), 1)
        self.assertIn('class:"export-flow-phase",role:"status","aria-live":"polite"', component)
        meta = re.search(r'h\("p",\{class:"export-flow-meta"[^}]*\}', component)
        self.assertIsNotNone(meta)
        self.assertNotIn("role", meta.group(0))
        self.assertNotIn("aria-live", meta.group(0))

    def test_export_flow_css_block(self):
        self.assertIn("/* M2 #46:", APP_CSS)
        start = APP_CSS.index("/* M2 #46:")
        following = APP_CSS.find("/* M2 #", start + 1)
        block = APP_CSS[start:following if following != -1 else len(APP_CSS)]
        self.assertIn(".export-flow{", block)
        self.assertIn("button-spin", block)
        self.assertNotIn(".export-links", block)
        # The rules before the block are the ones #24-UI left (append only).
        self.assertIn(".export-links.stacked{display:grid}", APP_CSS[:start])

    def test_flow_runs_to_a_checked_file_and_releases_the_transfer(self):
        out = run_export_node(r"""
RtkI18n.setLanguage("en");
const controller = sheet(), signal = controller.signal, node = exportGeojsonControl(project, {signal});
const start = action(node, "start");
eq("root", node && [node.tag, node.className, node.dataset.export, node.attrs["aria-label"], node.dataset.phase],
  ["div", "export-flow", "geojson", dict.en["ui.export_flow_region"], "idle"]);
eq("start", [start && start.className, start && start.textContent], ["btn secondary", dict.en["ui.export_flow_start"]]);
eq("idle", view(node), {phase: "idle", outcome: "", text: "", hidden: true, kind: undefined});
eq("one hook", guardCalls, ["function"]);
eq("nothing on the sheet signal before a start", controller.listeners, 0);
start.click(); await settle();
const request = requests[0];
eq("one request", [requests.length, request.path, request.options.timeout, request.options.signal !== signal],
  [1, "/api/tracking/export?project_id=a%2Fb&format=geojson", null, true]);
eq("preparing", view(node), {phase: "preparing", outcome: "preparing", text: dict.en["ui.export_flow_preparing"], hidden: false, kind: "running"});
eq("start is busy", [start.disabled, start.attrs["aria-busy"]], [true, "true"]);
eq("meta from the start", part(node, "export-flow-meta").textContent, fill(dict.en["ui.export_flow_meta"], {elapsed: "0:00", size: "0 kB"}));
eq("cancel while running", actionNames(node), ["cancel"]);
start.disabled = false; start.click(); await settle();
eq("a second start during the run joins it", [requests.length, start.disabled], [1, true]);
request.respond(); await settle();
eq("transferring", [view(node).phase, view(node).text], ["transferring", dict.en["ui.export_flow_transferring"]]);
const cancelButton = action(node, "cancel");
const phase = part(node, "export-flow-phase"), writes = phase.writes;
request.feed(DOC.slice(0, 20)); await settle(); clock += 29000; tick();
request.feed(DOC.slice(20, 40)); await settle(); clock += 29000; tick();
request.feed(DOC.slice(40, 60)); await settle(); clock += 3000; tick();
eq("the tick moves the meta line only", [part(node, "export-flow-meta").textContent, phase.writes, view(node).phase],
  [fill(dict.en["ui.export_flow_meta"], {elapsed: "1:01", size: exportSizeText(60)}), writes, "transferring"]);
eq("the start stays busy", [start.disabled, start.attrs["aria-busy"]], [true, "true"]);
eq("the cancel button stays across the phases", action(node, "cancel") === cancelButton, true);
request.feed(DOC.slice(60)); request.end(); await settle();
eq("ready", view(node), {phase: "ready", outcome: "ready", text: fill(dict.en["ui.export_flow_ready"], {size: exportSizeText(DOC.length)}), hidden: false, kind: "ok"});
eq("meta done", part(node, "export-flow-meta").textContent, fill(dict.en["ui.export_flow_meta_done"], {elapsed: "1:01", size: exportSizeText(DOC.length)}));
eq("transfer end; a finished transfer is not aborted", [request.released, request.cancelled, request.aborted, liveIntervals(),
  (docListeners.visibilitychange || []).length], [1, 0, false, 0, 0]);
eq("the disposer stays on the sheet signal", controller.listeners, 1);
eq("no share without navigator.share", actionNames(node), ["download", "discard"]);
const download = action(node, "download");
eq("download button", [download.dataset.export, download.textContent, focused === download], ["geojson-download", dict.en["ui.export_download"], true]);
await settle();
eq("start offers a retry", [start.disabled, start.textContent], [false, dict.en["ui.export_flow_retry"]]);
eq("ready closes without asking", [await guards.get(signal)(), questions.length], [true, 0]);
download.click(); await settle(); download.click(); await settle();
eq("one object URL for two downloads", [created, downloads], [1, [["blob:1", "Field.geojson", true], ["blob:1", "Field.geojson", true]]]);
eq("downloaded keeps the file", [view(node).phase, view(node).outcome, view(node).text, actionNames(node)],
  ["ready", "downloaded", dict.en["ui.export_flow_downloaded"], ["download", "discard"]]);
action(node, "discard").click(); await settle();
eq("discard", [view(node), revoked, start.textContent, actionNames(node)],
  [{phase: "idle", outcome: "", text: "", hidden: true, kind: undefined}, ["blob:1"], dict.en["ui.export_flow_start"], []]);
// Sharing: the real file decides; outcomes keep the file ready.
const shares = [];
let shareError = null;
navigator = {canShare: data => data.files.length === 1 && data.files[0].name === "Field.geojson" && data.files[0].type === "application/geo+json",
  share: async data => { shares.push(data); if (shareError) throw shareError; }};
start.click(); await settle();
await runToReady(node, requests[1]);
eq("share first", actionNames(node), ["share", "download", "discard"]);
const share = action(node, "share");
eq("share button", [share.dataset.export, share.textContent, share.className, focused === share], ["geojson-share", dict.en["ui.export_share"], "btn primary", true]);
share.click(); await settle();
eq("shared", [view(node).outcome, view(node).text, shares.length, shares[0].title, await shares[0].files[0].text()],
  ["shared", dict.en["ui.export_flow_shared"], 1, "Field", DOC]);
shareError = Object.assign(new Error("closed"), {name: "AbortError"}); share.click(); await settle();
eq("share cancelled", [view(node).phase, view(node).outcome, view(node).kind], ["ready", "share_cancelled", "warn"]);
shareError = Object.assign(new Error("gesture"), {name: "NotAllowedError"}); share.click(); await settle();
eq("share unavailable", [view(node).outcome, view(node).text, actionNames(node)], ["share_unavailable", dict.en["ui.export_flow_share_unavailable"], ["share", "download", "discard"]]);
download.click(); await settle();
action(node, "download").click(); await settle();
eq("a new start frees the previous file once", [created, revoked], [2, ["blob:1"]]);
start.click(); await settle();
eq("freed at the new start", revoked, ["blob:1", "blob:2"]);
navigator = {canShare: () => false, share: async () => {}};
await runToReady(node, requests[2]);
eq("canShare refuses the file", actionNames(node), ["download", "discard"]);
eq("no error went to showError", errors, []);
eq("the hook stays registered", [guardCalls, guards.has(signal)], [["function"], true]);
""", EXPORT_COMPONENT_STUBS, EXPORT_COMPONENT_PREFIXES, ("function exportGeojsonControl(",))
        self.assertEqual(out["failures"], [])

    def test_flow_cancel_stall_and_failures(self):
        out = run_export_node(r"""
RtkI18n.setLanguage("de");
const controller = sheet(), signal = controller.signal, node = exportGeojsonControl(project, {signal}), start = action(node, "start");
const begin = async () => { start.click(); await settle(); return requests[requests.length - 1]; };
let request = await begin();
request.respond(); await settle(); request.feed(DOC.slice(0, 20)); await settle();
action(node, "cancel").click(); await settle();
eq("cancelled", view(node), {phase: "cancelled", outcome: "cancelled", text: dict.de["ui.export_flow_cancelled"], hidden: false, kind: "warn"});
eq("cancel ends the transfer", [request.aborted, request.cancelled, actionNames(node), liveIntervals(), controller.listeners], [true, 1, [], 0, 1]);
eq("focus on the free start", [focused === start, start.disabled, start.textContent], [true, false, dict.de["ui.export_flow_retry"]]);
// No data for EXPORT_STALL_MS while visible.
request = await begin(); request.respond(); await settle(); request.feed(DOC.slice(0, 20)); await settle();
clock += 29999; tick();
eq("not yet stalled", view(node).phase, "transferring");
clock += 1; tick(); await settle();
eq("stalled", [view(node).phase, view(node).outcome, view(node).text, view(node).kind, request.aborted],
  ["failed", "stalled", fill(dict.de["ui.export_flow_stalled"], {seconds: "30"}), "critical", true]);
eq("focus on start after a failure", focused === start, true);
// Before the headers, too.
request = await begin(); clock += 30000; tick(); await settle();
eq("stalled before the headers", [view(node).outcome, request.aborted], ["stalled", true]);
// A hidden page never stalls; coming back counts from now.
request = await begin(); request.respond(); await settle();
visibility("hidden"); clock += 40000; tick();
eq("hidden", view(node).phase, "transferring");
visibility("visible"); tick(); clock += 29999; tick();
eq("back in the foreground", [view(node).phase, request.aborted], ["transferring", false]);
request.feed(DOC.slice(0, 20)); request.end(); await settle();
eq("incomplete", [view(node).phase, view(node).outcome, view(node).text, actionNames(node), created],
  ["failed", "incomplete", dict.de["ui.export_flow_incomplete"], [], 0]);
eq("incomplete ends with releaseLock", [request.released, request.cancelled], [1, 0]);
request = await begin(); request.respond(); await settle(); request.feed("{"); await settle(); request.fail(); await settle();
eq("lost", [view(node).outcome, view(node).text, request.cancelled], ["lost", dict.de["ui.export_flow_lost"], 1]);
request = await begin(); request.refuse(Object.assign(new Error("Storage maintenance in progress. Please wait."), {status: 503})); await settle();
eq("refused", [view(node).phase, view(node).outcome, view(node).text], ["failed", "refused", fill(dict.de["ui.export_flow_refused"], {message: "Storage maintenance in progress. Please wait."})]);
request = await begin(); request.refuse(new Error("Session expired")); await settle();
eq("session ends silently", [view(node).phase, view(node).outcome, view(node).text], ["failed", "session", ""]);
eq("listeners after every end", [liveIntervals(), (docListeners.visibilitychange || []).length, controller.listeners], [0, 0, 1]);
eq("no dock entry and no showError while the sheet is open", [warnings, errors], [[], []]);
RtkI18n.setLanguage("en");
""", EXPORT_COMPONENT_STUBS, EXPORT_COMPONENT_PREFIXES, ("function exportGeojsonControl(",))
        self.assertEqual(out["failures"], [])

    def test_close_hook_asks_only_while_running(self):
        out = run_export_node(r"""
RtkI18n.setLanguage("en");
const controller = sheet(), signal = controller.signal, node = exportGeojsonControl(project, {signal}), start = action(node, "start");
const ask = () => guards.get(signal)();
start.click(); await settle();
let request = requests[0];
const first = ask(), second = ask();
eq("one question while preparing", [questions.length, first === second, questions[0].signal === signal], [1, true, true]);
questions[0].dialog.close("stay");
eq("stay keeps the run", [await first, view(node).phase, request.aborted], [false, "preparing", false]);
request.respond(); await settle();
const third = ask();
eq("a new question after the answer", questions.length, 2);
await runToReady(node, request);
eq("the end of the run closes the question as stay", [questions[1].dialog.closed, await third, view(node).phase, request.aborted], ["ended", false, "ready", false]);
eq("no question when ready", [await ask(), questions.length], [true, 2]);
start.click(); await settle();
request = requests[1];
const confirmed = ask();
questions[2].dialog.close("confirm");
eq("cancel export allows the close", await confirmed, true);
const shown = view(node);
controller.abort(); await settle();
eq("the close ends the run quietly", [request.aborted, warnings, view(node)], [true, [], shown]);
eq("the disposer removes the hook", [guardCalls, guards.has(signal)], [["function", null], false]);
""", EXPORT_COMPONENT_STUBS, EXPORT_COMPONENT_PREFIXES, ("function exportGeojsonControl(",))
        self.assertEqual(out["failures"], [])

    def test_leaving_during_a_run_reports_in_the_dock(self):
        out = run_export_node(r"""
RtkI18n.setLanguage("de");
// Leaving the view while the device still sends: the run ends, nothing is rendered, the dock says so once.
let controller = sheet(), node = exportGeojsonControl(project, {signal: controller.signal});
action(node, "start").click(); await settle();
let request = requests[0];
request.respond(); await settle(); request.feed(DOC.slice(0, 20)); await settle();
const ask = guards.get(controller.signal)();
const text = part(node, "export-flow-phase").textContent;
controller.abort(); await settle();
eq("aborted with the view", [request.aborted, request.cancelled, questions[0].dialog.closed, await ask], [true, 1, "ended", false]);
eq("one dock entry", warnings, [["export", dict.de["ui.export_flow_cancelled"]]]);
eq("nothing new in the old view", part(node, "export-flow-phase").textContent, text);
eq("released", [liveIntervals(), (docListeners.visibilitychange || []).length, guards.has(controller.signal)], [0, 0, false]);
// Leaving with a ready file or before any start: nothing to report, the file is freed once.
warnings.length = 0;
controller = sheet(); node = exportGeojsonControl(project, {signal: controller.signal});
action(node, "start").click(); await settle();
await runToReady(node, requests[1]);
action(node, "download").click(); await settle();
controller.abort(); await settle();
eq("ready: no dock entry, the URL is freed", [warnings, revoked], [[], ["blob:1"]]);
controller = sheet(); node = exportGeojsonControl(project, {signal: controller.signal});
controller.abort(); await settle();
eq("idle: no dock entry", warnings, []);
action(node, "start").click(); await settle();
eq("no start after the sheet is gone", requests.length, 2);
RtkI18n.setLanguage("en");
""", EXPORT_COMPONENT_STUBS, EXPORT_COMPONENT_PREFIXES, ("function exportGeojsonControl(",))
        self.assertEqual(out["failures"], [])

    def test_close_question_answers(self):
        out = run_export_node(r"""
RtkI18n.setLanguage("de");
// busy: the opener is the sheet's Close button, which runButtonAction keeps disabled until the close guard has settled.
const ask = async (close, busy = false) => {
  const controller = sheet(), opener = new El("button");
  focused = opener;
  const question = exportCloseQuestion(controller.signal), dialog = question.dialog;
  if (busy) { opener.disabled = true; Promise.resolve(question.answer).then(answer => answer).finally(() => { opener.disabled = false; }); }
  const heading = find(dialog, item => item.tag === "h2")[0], buttons = find(dialog, item => item.tag === "button");
  const shape = {open: dialog.open, inOverlay: overlay.children.includes(dialog), className: dialog.className,
    labelled: dialog.attrs["aria-labelledby"] === (heading && heading.id) && Boolean(heading.id), title: heading && heading.textContent,
    text: (find(dialog, item => item.tag === "p")[0] || {}).textContent,
    row: (find(dialog, item => item.className === "sheet-actions")[0] || {children: []}).children.map(item => [item.className, item.textContent]),
    focusOnStay: focused === buttons[0], listeners: controller.listeners};
  close(dialog, buttons, controller);
  const answer = await question.answer;
  await settle();
  return {shape, answer, removed: !dialog.isConnected, focusBack: focused === opener, listeners: controller.listeners};
};
const stay = await ask((dialog, buttons) => buttons[0].click());
eq("shape", stay.shape, {open: true, inOverlay: true, className: "action-sheet export-close-question", labelled: true,
  title: dict.de["ui.export_flow_close_title"], text: dict.de["ui.export_flow_close_text"],
  row: [["btn secondary", dict.de["ui.export_flow_close_stay"]], ["btn danger", dict.de["ui.export_flow_close_confirm"]]], focusOnStay: true, listeners: 1});
eq("stay", [stay.answer, stay.removed, stay.focusBack, stay.listeners], [false, true, true, 0]);
const confirmed = await ask((dialog, buttons) => buttons[1].click());
eq("cancel export", [confirmed.answer, confirmed.removed, confirmed.focusBack, confirmed.listeners], [true, true, true, 0]);
const escape = await ask(dialog => dialog.close());
eq("Escape or Back", [escape.answer, escape.removed, escape.listeners], [false, true, 0]);
const leave = await ask((dialog, buttons, controller) => controller.abort());
eq("the sheet signal closes it", [leave.answer, leave.removed, leave.focusBack], [false, true, false]);
const busyStay = await ask((dialog, buttons) => buttons[0].click(), true);
eq("stay from the busy Close button", [busyStay.answer, busyStay.removed, busyStay.focusBack], [false, true, true]);
const busyBack = await ask(dialog => dialog.close("cancel"), true);
eq("Back from the busy Close button", [busyBack.answer, busyBack.focusBack], [false, true]);
const ended = await ask(dialog => dialog.close("ended"));
eq("the component's own close keeps focus", [ended.answer, ended.removed, ended.focusBack, ended.listeners], [false, true, false, 0]);
RtkI18n.setLanguage("en");
""", EXPORT_DOM_STUBS + "\nconst overlay = new El(\"div\");\nconst document = {get activeElement() { return focused; }};\n" + r"""
const sheet = () => { const controller = new AbortController(), signal = controller.signal, add = signal.addEventListener.bind(signal), remove = signal.removeEventListener.bind(signal);
  controller.listeners = 0;
  signal.addEventListener = (type, fn, options) => { if (type === "abort") controller.listeners += 1; add(type, fn, options); };
  signal.removeEventListener = (type, fn, options) => { if (type === "abort") controller.listeners -= 1; remove(type, fn, options); };
  return controller; };
""", EXPORT_COMPONENT_PREFIXES, ("function exportCloseQuestion(",))
        self.assertEqual(out["failures"], [])
