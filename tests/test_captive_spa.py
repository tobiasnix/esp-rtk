# SPDX-License-Identifier: AGPL-3.0-only
"""Captive portal entry: probes and /api/captive lead to the field app (#19c)."""
import inspect
import json
import os
import shutil
import subprocess
import unittest

import ui
from support import access, state, web
import web_routing
from test_access_v11 import http_request
from test_http_status_matrix import PROBES

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODE = shutil.which("node")
URL = "http://192.168.4.1/#device/configuration"
AP = {"peer": "192.168.4.2", "local": "192.168.4.1"}
STA = {}


def split(response):
    head, _, body = response.partition(b"\r\n\r\n")
    return head.decode("latin-1"), body


class TestCaptiveEntry(unittest.TestCase):
    def setUp(self):
        access.reset()
        self.old_identity = state.app.identity
        self.old_sta = state.app.stats.get("sta_ip")
        self.old_access_state = state.app.stats.get("access_state")
        state.app.identity = {
            "device_id": "A1B2C3D4E5F6", "display_name": "RTK-D4E5F6",
            "ble_name": "RTK-D4E5F6", "hostname": "rtk-d4e5f6",
            "device_code": "ABCDEF123456",
        }
        state.app.stats["sta_ip"] = "203.0.113.20"

    def tearDown(self):
        access.reset()
        state.app.identity = self.old_identity
        state.app.stats["sta_ip"] = self.old_sta
        state.app.stats["access_state"] = self.old_access_state

    def test_every_probe_redirects_to_the_field_app_configuration(self):
        for path in web_routing.CAPTIVE_PROBES:
            for label, where in (("AP", AP), ("STA", STA)):
                with self.subTest(path=path, side=label):
                    get_head, get_body = split(http_request(
                        ("GET %s HTTP/1.0\r\n\r\n" % path).encode(), **where))
                    self.assertTrue(get_head.startswith("HTTP/1.0 302 Found"), get_head)
                    self.assertIn("Location: " + URL + "\r\n", get_head + "\r\n")
                    self.assertIn("Cache-Control: no-store", get_head)
                    head_head, head_body = split(http_request(
                        ("HEAD %s HTTP/1.0\r\n\r\n" % path).encode(), **where))
                    self.assertEqual(head_head.splitlines()[0], get_head.splitlines()[0])
                    self.assertIn("Location: " + URL, head_head)
                    self.assertEqual(head_body, b"")

    def test_probe_redirect_ignores_the_probe_host(self):
        for path, host in (("/generate_204", "connectivitycheck.gstatic.com"),
                           ("/hotspot-detect.html", "captive.apple.com")):
            with self.subTest(host=host):
                head, _ = split(http_request(
                    ("GET %s HTTP/1.0\r\nHost: %s\r\n\r\n" % (path, host)).encode(), **AP))
                self.assertIn("Location: " + URL + "\r\n", head + "\r\n")
                self.assertNotIn(host, head)

    def test_api_captive_names_the_field_app(self):
        for label, where, captive in (("AP", AP, True), ("STA", STA, False)):
            with self.subTest(side=label):
                head, body = split(http_request(
                    b"GET /api/captive HTTP/1.0\r\n\r\n", **where))
                self.assertIn("200 OK", head)
                self.assertIn("Content-Type: application/captive+json", head)
                self.assertEqual(json.loads(body.decode()),
                                 {"captive": captive, "user-portal-url": URL})
        self.assertEqual(web.captive_portal_url(), URL)

    def test_probe_list_has_one_source(self):
        self.assertEqual(set(web_routing.CAPTIVE_PROBES), PROBES)
        for path in web_routing.CAPTIVE_PROBES:
            self.assertEqual(web_routing.ROUTES[path], web_routing.ROUTES["/api/captive"])
        self.assertEqual(web.CAPTIVE_PORTAL_PATH, "/#device/configuration")
        self.assertNotIn("/generate_204", inspect.getsource(web.handle_http_client))

    def test_unauthenticated_captive_target_serves_the_field_app_sign_in(self):
        head, body = split(http_request(
            b"GET / HTTP/1.0\r\nHost: 192.168.4.1\r\n\r\n", **AP))
        self.assertIn(" 401 ", head.splitlines()[0])
        self.assertIn(b'data-page="app-v2-login"', body)

    def test_origin_null_is_accepted_only_on_the_canonical_ap_host(self):
        # Pins the unchanged origin rules (ruling F3): they are not widened.
        state.app.stats["access_state"] = "SETUP"
        for host in ("192.168.4.1", "rtk-d4e5f6.local", "rtk-d4e5f6"):
            with self.subTest(host=host):
                self.assertTrue(web._origin_allowed({"origin": "null", "host": host}, True))
        for host in ("connectivitycheck.gstatic.com", "captive.apple.com"):
            with self.subTest(host=host):
                self.assertFalse(web._origin_allowed({"origin": "null", "host": host}, True))
        self.assertFalse(web._origin_allowed({"origin": "null", "host": "192.168.4.1"}, False))
        self.assertFalse(web._login_origin_allowed(
            {"origin": "null", "host": "connectivitycheck.gstatic.com"}, True))
        state.app.stats["access_state"] = "ONLINE"
        self.assertFalse(web._origin_allowed({"origin": "null", "host": "192.168.4.1"}, True))

    def test_sign_in_works_on_the_canonical_ap_host(self):
        state.app.stats["access_state"] = "SETUP"
        body = json.dumps({"device_code": "ABCDEF123456", "next": "/"}).encode()

        def sign_in(host):
            raw = (b"POST /api/session HTTP/1.0\r\nHost: " + host.encode() +
                   b"\r\nOrigin: null\r\nContent-Type: application/json\r\n"
                   b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
            return split(http_request(raw, **AP))[0]

        head = sign_in("192.168.4.1")
        self.assertIn(" 200 ", head.splitlines()[0])
        self.assertIn("Set-Cookie: rtk_session=", head)
        head = sign_in("connectivitycheck.gstatic.com")
        self.assertIn(" 403 ", head.splitlines()[0])


LOGIN_HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const input = JSON.parse(fs.readFileSync(0, "utf8"));
const log = [], handlers = {}, stubs = {};
const element = name => ({
  name, textContent: "", className: "", value: "", type: "password", disabled: false,
  addEventListener(kind, fn) { handlers[name + ":" + kind] = fn; },
  setAttribute() {}, focus() { result.focused = name; },
});
const result = {focused: null};
const ids = {}, getById = id => ids[id] || (ids[id] = element(id));
const submit = element("submit");
getById("v2-login-form").querySelector = () => submit;
const document = {
  body: {dataset: {page: "app-v2-login"}}, documentElement: {}, title: "",
  getElementById: getById,
  querySelector: sel => stubs[sel] || (stubs[sel] = element(sel)),
};
const replaced = [], history = [], fetches = [];
const responses = input.responses.slice();
const context = {
  document, console,
  location: {hash: input.hash, pathname: "/", replace: url => { replaced.push(url); log.push("replace"); }},
  history: {replaceState: (...args) => { history.push(args); log.push("history"); }},
  fetch: async (url, options) => {
    log.push("fetch");
    fetches.push({url, method: options.method, body: JSON.parse(options.body)});
    const answer = responses.shift();
    return {ok: answer.status < 400, status: answer.status,
            text: async () => answer.text || "", json: async () => answer.json || {}};
  },
  URLSearchParams,
};
context.globalThis = context;
context.RtkI18n = input.language === "de"
  ? {getLanguage: () => "de", v2de: require("./i18n.js").v2de} : undefined;
const flush = async () => { for (let i = 0; i < 3; i++) await new Promise(r => setImmediate(r)); };
(async () => {
  vm.runInNewContext(fs.readFileSync("app.js", "utf8"), context);
  await flush();
  const out = [];
  for (const action of input.actions) {
    const code = action.split(":")[1];
    getById("v2-device-code").value = code;
    handlers["v2-login-form:submit"]({preventDefault() {}});
    await flush();
    out.push({className: getById("v2-login-state").className,
              textContent: getById("v2-login-state").textContent,
              submitDisabled: submit.disabled, focused: result.focused,
              replaced: replaced.slice()});
  }
  console.log(JSON.stringify({
    fetches, replaced, history, log, steps: out,
    state: {className: getById("v2-login-state").className,
            textContent: getById("v2-login-state").textContent},
    submitDisabled: submit.disabled, focused: result.focused,
    fallbackText: (stubs[".login-fallback a"] || {}).textContent || null,
  }));
})();
"""


def run_login(hash_, responses, language="en", actions=("submit:ABC",)):
    payload = json.dumps({"hash": hash_, "responses": responses,
                          "language": language, "actions": list(actions)})
    done = subprocess.run([NODE, "-e", LOGIN_HARNESS], input=payload, cwd=REPO,
                          capture_output=True, text=True, timeout=60)
    if done.returncode != 0:
        raise AssertionError(done.stderr)
    return json.loads(done.stdout)


def ok_session():
    return {"status": 200, "json": {"csrf_token": "c", "redirect": "/"}}


@unittest.skipUnless(NODE, "node is required")
class TestFieldAppSignIn(unittest.TestCase):
    def sign_in_target(self, value):
        with open(os.path.join(REPO, "app.js"), encoding="utf-8") as handle:
            line = next(l for l in handle if l.startswith("const signInTarget="))
        done = subprocess.run(
            [NODE, "-e", line + "process.stdout.write(signInTarget(%s))" % json.dumps(value)],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def test_sign_in_target_keeps_router_paths_only(self):
        for kept in ("#device/configuration", "#projects/p1", "#status", "#" + "a" * 64):
            with self.subTest(kept=kept):
                self.assertEqual(self.sign_in_target(kept), kept)
        for dropped in ("", "#code=ABC", "#javascript:alert(1)", "#device%2Fconfiguration",
                        "#Device", "#" + "a" * 65):
            with self.subTest(dropped=dropped):
                self.assertEqual(self.sign_in_target(dropped), "#measure")

    def test_form_sign_in_opens_the_requested_route(self):
        result = run_login("#device/configuration", [ok_session()])
        self.assertEqual(result["fetches"], [{"url": "/api/session", "method": "POST",
                                              "body": {"device_code": "ABC", "next": "/"}}])
        self.assertEqual(result["replaced"], ["/?signed_in=1#device/configuration"])
        self.assertEqual(result["history"], [])

    def test_failed_sign_in_can_be_repeated(self):
        result = run_login("#device/configuration",
                           [{"status": 401, "text": "Invalid device code."}, ok_session()],
                           actions=("submit:WRONG", "submit:ABC"))
        first, second = result["steps"]
        self.assertEqual(first["className"], "notice critical")
        self.assertEqual(first["textContent"], "Invalid device code.")
        self.assertIs(first["submitDisabled"], False)
        self.assertEqual(first["focused"], "v2-device-code")
        self.assertEqual(first["replaced"], [])
        self.assertEqual(second["replaced"], ["/?signed_in=1#device/configuration"])
        self.assertEqual(len(result["fetches"]), 2)

    def test_code_fragment_signs_in_and_clears_the_address(self):
        result = run_login("#code=ABC-123", [ok_session()], actions=())
        self.assertEqual(result["history"], [[None, "", "/"]])
        self.assertEqual(result["log"], ["history", "fetch", "replace"])
        self.assertEqual(result["fetches"][0]["body"]["device_code"], "ABC-123")
        self.assertEqual(result["replaced"], ["/?signed_in=1#measure"])
        failed = run_login("#code=ABC-123", [{"status": 401, "text": "Invalid device code."}],
                           actions=())
        self.assertEqual(failed["state"]["className"], "notice critical")
        self.assertEqual(failed["replaced"], [])
        self.assertEqual(failed["history"], [[None, "", "/"]])

    def test_login_page_links_the_fallback_without_script(self):
        html = ui.APP_V2_LOGIN
        link = '<p class="login-fallback"><a href="/setup">Basic setup page (fallback)</a></p>'
        self.assertIn(link, html)
        self.assertLess(html.index('id="v2-login-state"'), html.index(link))
        self.assertLess(html.index(link), html.index("</form>"))
        anchor = html[html.index(link):html.index("</p>", html.index(link))]
        self.assertNotIn("onclick", anchor)
        self.assertNotIn("target", anchor)
        done = subprocess.run(
            [NODE, "-e", 'process.stdout.write(require("./i18n.js").v2de'
             '["Basic setup page (fallback)"] || "")'],
            cwd=REPO, capture_output=True, text=True, timeout=30)
        german = done.stdout
        self.assertTrue(german)
        self.assertNotEqual(german, "Basic setup page (fallback)")
        result = run_login("", [], language="de", actions=())
        self.assertEqual(result["fallbackText"], german)
        with open(os.path.join(REPO, "app.css"), encoding="utf-8") as handle:
            css = handle.read()
        rule = css[css.index(".login-fallback a{"):]
        rule = rule[:rule.index("}")]
        self.assertIn("color:var(--action)", rule)
        self.assertIn("min-height:var(--tap)", rule)


SCAN_HARNESS = r"""
const fs = require("fs"), vm = require("vm");
const input = JSON.parse(fs.readFileSync(0, "utf8"));
const handlers = {}, ids = {};
const element = name => ({
  name, textContent: "", className: "", value: "", disabled: false, hidden: true,
  options: [],
  addEventListener(kind, fn) { handlers[name + ":" + kind] = fn; },
  replaceChildren(...items) { this.options = items; },
  add(item) { this.options.push(item); },
});
const byId = id => ids[id] || (ids[id] = element(id));
const stateAtFetch = [], fetchUrls = [], delays = [], timers = [], cleared = [];
let aborts = 0, nextTimer = 1, script = [];
class Option { constructor(text, value) { this.text = text; this.value = value; } }
class FakeAbortController {
  constructor() { this.signal = {}; }
  abort() { aborts += 1; }
}
const context = {
  document: {
    body: {dataset: {page: "setup", hostname: "rtk"}},
    getElementById: byId, querySelector: () => null, querySelectorAll: () => [],
  },
  Option, console, Promise, JSON, Error, TypeError, SyntaxError,
  fetch: (url) => {
    fetchUrls.push(url);
    stateAtFetch.push(byId("wifi-scan-state").textContent);
    const answer = script.shift();
    if (answer === "hang") return new Promise(() => {});
    if (answer === "neterr") return Promise.reject(new TypeError("Failed to fetch"));
    const body = answer === "hang-body" ? () => new Promise(() => {}) : () => {
      if (answer.html) return Promise.reject(new SyntaxError("Unexpected token <"));
      if (answer.bodyFail) return Promise.reject(new TypeError("body stream broke"));
      return Promise.resolve(answer.json);
    };
    const status = answer === "hang-body" ? 200 : answer.status;
    return Promise.resolve({status, ok: status >= 200 && status < 300, json: body});
  },
  setTimeout: (fn, ms) => {
    delays.push(ms);
    const id = nextTimer++;
    if (ms === 8000) timers.push({id, fn});
    else queueMicrotask(fn);
    return id;
  },
  clearTimeout: id => { cleared.push(id); },
  window: {RtkI18n: {t: key => "<" + key + ">", label: x => x}},
};
if (input.abortController) context.AbortController = FakeAbortController;
context.globalThis = context;
const flush = async () => { for (let i = 0; i < 4; i++) await new Promise(r => setImmediate(r)); };
(async () => {
  vm.runInNewContext(fs.readFileSync("pages.js", "utf8"), context);
  const results = [];
  for (const click of input.clicks) {
    script = click.script.slice();
    stateAtFetch.length = 0; fetchUrls.length = 0; delays.length = 0; timers.length = 0;
    cleared.length = 0; aborts = 0;
    const button = element("button"), state = byId("wifi-scan-state"), select = byId("wifi-results");
    const done = handlers["wifi-scan:click"]({currentTarget: button});
    if (click.fireDeadline) { await flush(); timers.forEach(t => t.fn()); }
    await done;
    results.push({
      text: state.textContent, className: state.className, options: select.options.length,
      hidden: select.hidden, disabled: button.disabled, delays: delays.slice(),
      aborts, fetches: fetchUrls.length, urls: fetchUrls.slice(), stateAtFetch: stateAtFetch.slice(),
      deadlines: timers.map(t => t.id), cleared: cleared.slice(),
    });
  }
  console.log(JSON.stringify(results));
})();
"""

TWO_NETWORKS = {"status": 200, "json": {"status": "ready", "networks": [
    {"ssid": "A", "rssi": -50, "secured": True},
    {"ssid": "B", "rssi": -71, "secured": False}]}}
PENDING = {"status": 202, "json": {"status": "pending"}}
BUSY = {"status": 409, "json": {
    "error": "wifi_scan_busy",
    "message": "Pause the active recording before scanning for Wi-Fi networks."}}
BUSY_TEXT = "Pause the active recording before scanning for Wi-Fi networks."
HTML_401 = {"status": 401, "html": True}
BODY_FAIL = {"status": 200, "bodyFail": True}
HTML_200 = {"status": 200, "html": True}


def run_scan(clicks, abort_controller=True):
    payload = json.dumps({"clicks": clicks, "abortController": abort_controller})
    done = subprocess.run([NODE, "-e", SCAN_HARNESS], input=payload, cwd=REPO,
                          capture_output=True, text=True, timeout=60)
    if done.returncode != 0:
        raise AssertionError(done.stderr)
    return json.loads(done.stdout)


def click(*script, fire=False):
    return {"script": list(script), "fireDeadline": fire}


def dictionary_value(language, key):
    done = subprocess.run(
        [NODE, "-e", 'const d = require("./i18n.js").dictionaries[process.argv[1]];'
         'process.stdout.write(d[process.argv[2]] === undefined ? "" : d[process.argv[2]])',
         language, key],
        cwd=REPO, capture_output=True, text=True, timeout=30)
    if done.returncode != 0:
        raise AssertionError(done.stderr)
    return done.stdout


@unittest.skipUnless(NODE, "node is required")
class TestLegacySetupPage(unittest.TestCase):
    def test_pending_scan_shows_searching_then_networks(self):
        (result,) = run_scan([click(PENDING, PENDING, TWO_NETWORKS)])
        self.assertEqual(result["urls"], ["/api/wifi-scan"] * 3)
        self.assertEqual(result["stateAtFetch"][1:], ["<ui.searching>"] * 2)
        self.assertEqual([ms for ms in result["delays"] if ms == 750], [750, 750])
        self.assertEqual(len(result["deadlines"]), 3)
        for timer in result["deadlines"]:
            self.assertIn(timer, result["cleared"])
        self.assertEqual(result["text"], "2 <ui.networks_found>")
        self.assertEqual(result["className"], "muted")
        self.assertEqual(result["options"], 3)
        self.assertIs(result["hidden"], False)
        self.assertIs(result["disabled"], False)

    def test_legacy_scan_gives_up_after_sixteen_pending_answers(self):
        (result,) = run_scan([click(*([PENDING] * 17))])
        self.assertEqual(result["fetches"], 17)
        self.assertEqual([ms for ms in result["delays"] if ms == 750], [750] * 16)
        self.assertEqual(result["text"], "<ui.scan_still_running>")
        self.assertEqual(result["className"], "muted warn")
        self.assertIs(result["hidden"], True)
        self.assertEqual(result["options"], 0)
        self.assertIs(result["disabled"], False)

    def test_legacy_scan_reports_refusals_readably(self):
        failed = "<ui.scan_failed> "
        cases = (
            ("busy", [BUSY], failed + BUSY_TEXT, "muted critical"),
            ("html", [HTML_401], failed + "HTTP 401", "muted critical"),
            ("no networks key", [{"status": 200, "json": {"status": "ready"}}],
             "0 <ui.networks_found>", "muted"),
            ("pending then busy", [PENDING, BUSY], failed + BUSY_TEXT, "muted critical"),
            ("pending then html", [PENDING, HTML_401], failed + "HTTP 401", "muted critical"),
        )
        for label, script, text, class_name in cases:
            with self.subTest(label):
                (result,) = run_scan([click(*script)])
                self.assertEqual(result["text"], text)
                self.assertEqual(result["className"], class_name)
                self.assertIs(result["disabled"], False)

    def test_legacy_scan_ends_a_hanging_request(self):
        for label, script, abort_controller in (
                ("request", "hang", True), ("body", "hang-body", True),
                ("request without AbortController", "hang", False)):
            with self.subTest(label):
                (result,) = run_scan([click(script, fire=True)],
                                     abort_controller=abort_controller)
                self.assertEqual(result["text"], "<ui.scan_failed> <ui.scan_no_answer>")
                self.assertEqual(result["className"], "muted critical")
                self.assertIs(result["disabled"], False)
                self.assertEqual(result["fetches"], 1)
                self.assertEqual(result["aborts"], 1 if abort_controller else 0)

    def test_legacy_scan_keeps_results_after_a_failure(self):
        variants = (
            ("network error", click("neterr"), "<ui.scan_failed> Failed to fetch"),
            ("hanging request", click("hang", fire=True),
             "<ui.scan_failed> <ui.scan_no_answer>"),
            ("pending then busy", click(PENDING, BUSY), "<ui.scan_failed> " + BUSY_TEXT),
            ("pending then html", click(PENDING, HTML_401), "<ui.scan_failed> HTTP 401"),
            ("body fails mid-read", click(BODY_FAIL),
             "<ui.scan_failed> body stream broke"),
            ("success with invalid JSON", click(HTML_200),
             "<ui.scan_failed> Unexpected token <"),
        )
        for label, second, text in variants:
            with self.subTest(label):
                first_result, second_result = run_scan([click(TWO_NETWORKS), second])
                self.assertEqual(first_result["options"], 3)
                self.assertEqual(second_result["text"], text)
                self.assertEqual(second_result["options"], 3)
                self.assertIs(second_result["hidden"], False)
                self.assertIs(second_result["disabled"], False)

    def test_setup_page_is_marked_as_fallback(self):
        html = web.render_setup(csrf="CSRF")
        self.assertIn('class="notice fallback-notice"', html)
        self.assertIn('data-i18n="ui.legacy_setup_notice"', html)
        self.assertIn('href="%s"' % web.CAPTIVE_PORTAL_PATH, html)
        self.assertIn('data-i18n="ui.open_field_app_setup"', html)
        self.assertNotIn("{field_app_setup}", html)
        self.assertLess(html.index("fallback-notice"), html.index('id="setup-form"'))

    def test_network_help_describes_the_configured_order(self):
        self.assertNotIn("strongest", web.SETUP_HTML)
        english = dictionary_value("en", "ui.network_format_help")
        german = dictionary_value("de", "ui.network_format_help")
        self.assertTrue(english.startswith("Format:"))
        self.assertIn("order", english)
        self.assertIn("tries all of them", english)
        self.assertNotIn("strongest", english)
        self.assertTrue(german)
        self.assertNotIn("st\u00e4rkste", german)
        self.assertRegex(german, r"probiert \w+ Ger\w+t alle")
        self.assertIn('data-i18n="ui.network_format_help">%s</p>' % english, web.SETUP_HTML)
        for key in ("ui.scan_still_running", "ui.scan_no_answer",
                    "ui.legacy_setup_notice", "ui.open_field_app_setup"):
            for language in ("en", "de"):
                with self.subTest(key=key, language=language):
                    value = dictionary_value(language, key)
                    self.assertTrue(value)
                    self.assertNotEqual(value, key)
        self.assertNotEqual(dictionary_value("en", "ui.scan_no_answer"),
                            dictionary_value("de", "ui.scan_no_answer"))


class TestPortalQr(unittest.TestCase):
    def setUp(self):
        access.reset()
        self.old_identity = state.app.identity
        state.app.identity = {
            "device_id": "A1B2C3D4E5F6", "display_name": "RTK-D4E5F6",
            "ble_name": "RTK-D4E5F6", "hostname": "rtk-d4e5f6",
            "device_code": "ABCDEF123456",
        }

    def tearDown(self):
        access.reset()
        state.app.identity = self.old_identity

    def test_portal_qr_targets_the_field_app_sign_in(self):
        payload = web.portal_qr_payload()
        self.assertTrue(payload.startswith("http://rtk-d4e5f6.local/#code="), payload)
        self.assertNotIn("/setup", payload)

    def test_old_setup_qr_keeps_working(self):
        # Labels printed by earlier firmware encode /setup#code=...; the fragment
        # never reaches the server, so this is the legacy sign-in round trip.
        head, body = split(http_request(b"GET /setup HTTP/1.0\r\n\r\n", **AP))
        self.assertIn(" 401 ", head.splitlines()[0])
        page = body.decode()
        self.assertIn('data-page="login"', page)
        self.assertIn('name="next" value="/setup"', page)
        form = b"device_code=ABCDEF123456&next=%2Fsetup"
        head, _ = split(http_request(
            b"POST /api/session HTTP/1.0\r\n"
            b"Content-Type: application/x-www-form-urlencoded\r\n"
            b"Content-Length: " + str(len(form)).encode() + b"\r\n\r\n" + form, **AP))
        self.assertIn("303 See Other", head.splitlines()[0])
        self.assertIn("Location: /setup\r\n", head + "\r\n")
        self.assertIn("Set-Cookie: rtk_session=", head)
        cookie = head.split("Set-Cookie: ", 1)[1].split(";", 1)[0]
        head, body = split(http_request(
            ("GET /setup HTTP/1.0\r\nCookie: %s\r\n\r\n" % cookie).encode(), **AP))
        self.assertIn(" 200 ", head.splitlines()[0])
        self.assertIn('id="setup-form"', body.decode())


if __name__ == "__main__":
    unittest.main()
