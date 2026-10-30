# SPDX-License-Identifier: AGPL-3.0-only
"""#23c Tasks 3 and 4: pure logic of the guided apply (src/78-guided-apply.js).

The pure functions run under Node straight from the source fragment; the policy
mirror, the error field map, the failure causes and the result records are bound
to the firmware (cfg, net, web).
"""
import json
import os
import re
import subprocess
import unittest

from support import cfg, state, web
import test_ui
from test_config_identity import APPLY_ID, _IdentityCase

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODE = test_ui.NODE
APP_JS = test_ui.APP_JS
EXPORTS = ("CONFIG_FORM_POLICY, CONFIG_SECRET_FIELDS, CONFIG_PORT_FIELDS, CONFIG_PRIMARY_CASTER_FIELDS, "
           "RESTART_MARKER_FIELDS, CONFIG_FIELD_LABELS, parseNetworkLines, maskedNetworkText, configChanges, "
           "savedBaseline, configEffects, configRequired, configErrorField, apiFailure")
RESTART_EXPORTS = EXPORTS + (
    ", CONFIG_ROUTE, FIELD_SETUP_ROUTE, RESTART_MARKER_KEY, RESTART_TIMING, newApplyId, restartAnswerAccepted, "
    "restoreScope, CONFIG_FAILURE_CAUSES, failureCause, restartStepLabel, ntripStep, restartSteps, stepsSettled, "
    "bootChanged, judgeConfigResult, restoredNote, reportedSettingsNote, configFileNote, wifiConnectionNote, "
    "stepNotes, restartWarningText, restartOutcome, restartOutcomeView, readRestartMarker, writeRestartMarker, "
    "clearRestartMarker")

STAGING = {"revision": "a1b2c3d4-0", "keys": [], "restart_required": False}
FACTORY = {"wifi_ssid": "", "wifi_pass_set": False, "wifi_networks": [], "ntrip_enabled": True,
           "ntrip_host": "", "ntrip_port": 2101, "ntrip_mount": "", "ntrip_user": "", "ntrip_pass_set": False,
           "ntrip_fallback_host": "", "ntrip_fallback_port": 2101, "ntrip_fallback_mount": "",
           "ntrip_fallback_user": "", "ntrip_fallback_pass_set": False, "staging": STAGING}
FIELD = dict(FACTORY, wifi_ssid="Field", wifi_pass_set=True,
             wifi_networks=[{"ssid": "Fallback", "password_set": True}],
             ntrip_host="caster", ntrip_mount="MOUNT", ntrip_user="user", ntrip_pass_set=True)

PRELUDE = r"""
const fs = require("fs");
const tr = (k, f, v) => "<" + k + ">" + Object.entries(v || {}).map(([a, b]) => "|" + a + "=" + b).join("");
const localText = x => x;
const capacityText = (m, v) => m.replace(/\{(\w+)\}/g, (_, k) => v[k] ?? "");
const g = new Function("tr", "localText", "capacityText",
  fs.readFileSync("src/78-guided-apply.js", "utf8") + "\nreturn {%(exports)s};")(tr, localText, capacityText);
const FACTORY = %(factory)s, FIELD = %(field)s;
// The form as the page reads it: texts as strings, secrets empty, the textarea as masked lines.
const formOf = (base, over = {}) => Object.assign({
  wifi_ssid: base.wifi_ssid, wifi_pass: "", wifi_networks: g.maskedNetworkText(base.wifi_networks),
  ntrip_enabled: base.ntrip_enabled, ntrip_host: base.ntrip_host, ntrip_port: base.ntrip_port,
  ntrip_mount: base.ntrip_mount, ntrip_user: base.ntrip_user, ntrip_pass: "",
  ntrip_fallback_host: base.ntrip_fallback_host, ntrip_fallback_port: base.ntrip_fallback_port,
  ntrip_fallback_mount: base.ntrip_fallback_mount, ntrip_fallback_user: base.ntrip_fallback_user,
  ntrip_fallback_pass: ""}, over);
"""


# The real translation path: i18n.js in the chosen language, tr/localText from src/10-core.js and
# capacityText from src/45-capacity.js, evaluated in front of the fragment.
REAL_PRELUDE = r"""
const fs = require("fs");
globalThis.RtkI18n = require("./i18n.js");
RtkI18n.setLanguage(%(language)s);
const helpers = fs.readFileSync("src/10-core.js", "utf8").split("\n")
  .filter(line => /^(const tr=|const V2_DE=|function localText\()/.test(line))
  .concat(fs.readFileSync("src/45-capacity.js", "utf8").split("\n").filter(line => line.startsWith("function capacityText(")));
const g = new Function(helpers.join("\n") + "\n" + fs.readFileSync("src/78-guided-apply.js", "utf8") +
  "\nreturn {%(exports)s};")();
"""


def run_guided(script, exports=EXPORTS, language=None, data=None):
    """Run `script` with the fragment's functions in `g`; it assigns its result to `out`.

    With `language` the fragment uses the real i18n.js instead of the key stub;
    `data` is available to the script as DATA.
    """
    if NODE is None:
        raise unittest.SkipTest("node is unavailable")
    if language is None:
        prelude = PRELUDE % {"exports": exports, "factory": json.dumps(FACTORY), "field": json.dumps(FIELD)}
    else:
        prelude = REAL_PRELUDE % {"exports": exports, "language": json.dumps(language)}
    prelude += "const DATA = %s;\n" % json.dumps(data)
    run = subprocess.run([NODE, "-e", prelude + "let out;\n" + script + "\nconsole.log(JSON.stringify(out));"],
                         cwd=ROOT, capture_output=True, text=True, timeout=20)
    if run.returncode != 0:
        raise AssertionError(run.stderr)
    return json.loads(run.stdout)


class TestConfigFormLogic(unittest.TestCase):
    def test_form_policy_mirrors_the_firmware_policy(self):
        out = run_guided("out = {policy: g.CONFIG_FORM_POLICY, secret: g.CONFIG_SECRET_FIELDS,"
                         " marker: g.RESTART_MARKER_FIELDS, ports: g.CONFIG_PORT_FIELDS,"
                         " primary: g.CONFIG_PRIMARY_CASTER_FIELDS};")
        policy = out["policy"]
        self.assertEqual(policy, {key: cfg.APPLY_POLICY[key] for key in policy})
        with open(os.path.join(ROOT, "src", "80-configuration.js"), encoding="utf-8") as source:
            text = source.read()
        begin = text.index("async function mountConfiguration(signal){")
        form = text[begin:text.index("\n}\n", begin) + 3]
        names = set(re.findall(r'\bfield\("(\w+)"', form))
        names |= set(re.findall(r'(?:textarea|select)",\{name:"(\w+)"', form))
        self.assertEqual(set(policy), names)
        self.assertTrue(set(out["secret"]) <= set(cfg.SECRET_KEYS))
        self.assertTrue(set(out["ports"]) <= set(policy))
        self.assertTrue(set(out["primary"]) <= set(policy))
        for key in out["marker"]:
            self.assertNotIn(key, cfg.SECRET_KEYS)
            self.assertNotIn(key, ("ntrip_user", "ntrip_fallback_user"))
        self.assertEqual(sorted(out["marker"]),
                         ["ntrip_fallback_host", "ntrip_fallback_mount", "ntrip_host", "ntrip_mount", "wifi_ssid"])

    def test_field_labels_cover_the_form(self):
        out = run_guided("out = {labels: g.CONFIG_FIELD_LABELS, keys: Object.keys(g.CONFIG_FORM_POLICY)};")
        self.assertEqual(sorted(out["labels"]), sorted(out["keys"]))
        self.assertEqual(out["labels"]["wifi_ssid"], "Wi-Fi name (SSID)")
        self.assertEqual(out["labels"]["ntrip_fallback_pass"], "Fallback caster password")
        german = json.loads(subprocess.run(
            [NODE, "-e", "console.log(JSON.stringify(require('./i18n.js').v2de))"],
            cwd=ROOT, capture_output=True, text=True, check=True).stdout)
        for label in out["labels"].values():
            self.assertIn(label, german, label)

    def test_changes_in_factory_state_send_only_wifi(self):
        out = run_guided("""out = g.configChanges(formOf(FACTORY, {wifi_ssid: "Barn", wifi_pass: "secret123",
          wifi_networks: []}), FACTORY);""")
        self.assertEqual(out["keys"], ["wifi_pass", "wifi_ssid"])
        self.assertEqual(out["changes"], {"wifi_pass": "secret123", "wifi_ssid": "Barn"})
        self.assertNotIn("staging", out["changes"])

    def test_changes_ignore_unchanged_and_empty_secrets(self):
        out = run_guided("""out = {
          same: g.configChanges(formOf(FIELD), FIELD).keys,
          text_port: g.configChanges(formOf(FIELD, {ntrip_port: "2101", ntrip_fallback_port: "2101"}), FIELD).keys,
          back: g.configChanges(formOf(FIELD, {ntrip_mount: "MOUNT"}), FIELD).keys,
          other: g.configChanges(formOf(FIELD, {ntrip_mount: "OTHER"}), FIELD).keys,
          port: g.configChanges(formOf(FIELD, {ntrip_port: "2102"}), FIELD).changes};""")
        self.assertEqual(out["same"], [])
        self.assertEqual(out["text_port"], [])
        self.assertEqual(out["back"], [])
        self.assertEqual(out["other"], ["ntrip_mount"])
        self.assertEqual(out["port"], {"ntrip_port": 2102})

    def test_changes_keep_masked_fallback_passwords(self):
        out = run_guided("""const c = text => g.configChanges(formOf(FIELD, {wifi_networks: text}), FIELD);
          out = {masked: c("Fallback"), added: c("Fallback\\nSecond:pass12345"), changed: c("Fallback:newpass1"),
                 swapped: g.configChanges(formOf(FIELD, {wifi_networks: "B\\nA"}),
                   Object.assign({}, FIELD, {wifi_networks: [{ssid: "A", password_set: true}, {ssid: "B", password_set: false}]})),
                 removed: c(""), crlf: c("Fallback\\r\\n\\r\\n")};""")
        self.assertEqual(out["masked"]["keys"], [])
        self.assertEqual(out["added"]["keys"], ["wifi_networks"])
        self.assertEqual(out["added"]["changes"]["wifi_networks"],
                         [{"ssid": "Fallback", "password_set": True}, {"ssid": "Second", "password": "pass12345"}])
        self.assertEqual(out["changed"]["changes"]["wifi_networks"], [{"ssid": "Fallback", "password": "newpass1"}])
        self.assertEqual(out["swapped"]["keys"], ["wifi_networks"])
        self.assertEqual(out["removed"]["changes"]["wifi_networks"], [])
        self.assertEqual(out["crlf"]["keys"], [])

    def test_changes_switching_ntrip_off_is_one_key(self):
        out = run_guided("out = g.configChanges(formOf(FIELD, {ntrip_enabled: false}), FIELD);")
        self.assertEqual(out["keys"], ["ntrip_enabled"])
        self.assertIs(out["changes"]["ntrip_enabled"], False)

    def test_changes_read_the_form_select_value(self):
        # The page hands over the select as "1"/"0" before it converts it; both forms must compare.
        out = run_guided("""out = [g.configChanges(formOf(FIELD, {ntrip_enabled: "1"}), FIELD).keys,
                                   g.configChanges(formOf(FIELD, {ntrip_enabled: "0"}), FIELD).keys];""")
        self.assertEqual(out, [[], ["ntrip_enabled"]])

    def test_saved_baseline_closes_the_diff(self):
        out = run_guided("""const sent = g.configChanges(formOf(FIELD, {ntrip_pass: "newsecret", ntrip_mount: "OTHER"}), FIELD);
          const base = g.savedBaseline(FIELD, sent.changes);
          out = {base, cleared: g.configChanges(formOf(base, {ntrip_mount: "OTHER"}), base).keys,
                 stale: g.configChanges(formOf(base, {ntrip_mount: "OTHER", ntrip_pass: "newsecret"}), base).keys,
                 original: FIELD.ntrip_mount};""")
        self.assertIs(out["base"]["ntrip_pass_set"], True)
        self.assertEqual(out["base"]["ntrip_mount"], "OTHER")
        self.assertNotIn("ntrip_pass", out["base"])
        self.assertEqual(out["base"]["staging"], STAGING)
        self.assertEqual(out["cleared"], [])
        self.assertEqual(out["stale"], ["ntrip_pass"])
        self.assertEqual(out["original"], "MOUNT")

    def test_saved_baseline_masks_fallback_passwords(self):
        out = run_guided("""const text = "Fallback\\nSecond:pass12345";
          const sent = g.configChanges(formOf(FIELD, {wifi_networks: text}), FIELD);
          const base = g.savedBaseline(FIELD, sent.changes), masked = g.maskedNetworkText(base.wifi_networks);
          const again = g.configChanges(formOf(base, {wifi_networks: "Fallback:newpass1"}), base);
          const base2 = g.savedBaseline(base, again.changes);
          out = {sent: sent.changes.wifi_networks, base: base.wifi_networks, masked,
                 closed: g.configChanges(formOf(base, {wifi_networks: masked}), base).keys,
                 stale: g.configChanges(formOf(base, {wifi_networks: text}), base).keys,
                 masked2: g.maskedNetworkText(base2.wifi_networks),
                 closed2: g.configChanges(formOf(base2, {wifi_networks: g.maskedNetworkText(base2.wifi_networks)}), base2).keys,
                 secrets: JSON.stringify(base) + JSON.stringify(base2)};""")
        self.assertEqual(out["sent"], [{"ssid": "Fallback", "password_set": True}, {"ssid": "Second", "password": "pass12345"}])
        self.assertEqual(out["base"], [{"ssid": "Fallback", "password_set": True}, {"ssid": "Second", "password_set": True}])
        self.assertEqual(out["masked"], "Fallback\nSecond")
        self.assertEqual(out["closed"], [])
        self.assertEqual(out["stale"], ["wifi_networks"])
        self.assertEqual(out["masked2"], "Fallback")
        self.assertEqual(out["closed2"], [])
        self.assertNotIn("pass12345", out["secrets"])
        self.assertNotIn("newpass1", out["secrets"])

    def test_required_fields_follow_the_change(self):
        out = run_guided("""const r = (base, over) => g.configRequired(formOf(base, over), base);
          out = {factory: r(FACTORY, {}), ssid: r(FACTORY, {wifi_ssid: "Barn"}), pass: r(FACTORY, {wifi_pass: "secret123"}),
                 host: r(FACTORY, {ntrip_host: "x"}), off: r(FACTORY, {ntrip_enabled: false, ntrip_host: "x"}),
                 on: r(Object.assign({}, FACTORY, {ntrip_enabled: false}), {ntrip_enabled: true}),
                 field: r(FIELD, {}), user: r(FACTORY, {ntrip_user: "u"})};""")
        self.assertEqual(out["factory"], ["ntrip_fallback_port", "ntrip_port"])
        self.assertEqual(out["ssid"], ["ntrip_fallback_port", "ntrip_port", "wifi_ssid"])
        self.assertEqual(out["pass"], ["ntrip_fallback_port", "ntrip_port", "wifi_ssid"])
        self.assertEqual(out["host"], ["ntrip_fallback_port", "ntrip_host", "ntrip_mount", "ntrip_port"])
        self.assertEqual(out["off"], ["ntrip_fallback_port", "ntrip_port"])
        self.assertEqual(out["on"], ["ntrip_fallback_port", "ntrip_host", "ntrip_mount", "ntrip_port"])
        self.assertEqual(out["field"], ["ntrip_fallback_port", "ntrip_host", "ntrip_mount", "ntrip_port", "wifi_ssid"])
        self.assertEqual(out["user"], ["ntrip_fallback_port", "ntrip_host", "ntrip_mount", "ntrip_port"])

    @staticmethod
    def device_message(raw):
        """The text the device answers for a rejected PUT, built from the real validator."""
        values, code, message = cfg.prepare_config_change(raw)
        assert values is None, raw
        return code, message if code == "invalid_wifi_networks" else "The configuration is invalid: %s" % message

    def test_error_field_matches_the_device_messages(self):
        with open(os.path.join(ROOT, "web.py"), encoding="utf-8") as source:
            self.assertIn('"The configuration is invalid: %s"', source.read())
        cases = [({"wifi_ssid": ""}, "wifi_ssid"), ({"wifi_ssid": "x" * 33}, "wifi_ssid"),
                 ({"wifi_pass": "short"}, "wifi_pass"), ({"ntrip_host": ""}, "ntrip_host"),
                 ({"ntrip_mount": "a/b"}, "ntrip_mount"), ({"ntrip_port": 0}, "ntrip_port"),
                 ({"ntrip_port": "x"}, "ntrip_port"), ({"ntrip_user": "u" * 65}, "ntrip_user"),
                 ({"ntrip_fallback_mount": "a/b"}, "ntrip_fallback_mount"),
                 ({"ntrip_fallback_port": 70000}, "ntrip_fallback_port"), ({"ntrip_host": "a\x01"}, "ntrip_host"),
                 ({"wifi_ssid": "A", "wifi_networks": [["n%d" % i, ""] for i in range(9)]}, "wifi_networks")]
        answers = [self.device_message(raw) for raw, _field in cases]
        answers += [("stage_failed", "Could not stage the configuration."),
                    ("staging_changed", "The changes waiting in the device memory changed. Check them again.")]
        out = run_guided("out = %s.map(([code, message]) => g.configErrorField(code, message));" % json.dumps(answers))
        self.assertEqual(out, [field for _raw, field in cases] + [None, None])

    def test_error_field_ignores_unknown_identifiers(self):
        out = run_guided("""out = [g.configErrorField("invalid_configuration", "The configuration is invalid: uart_id must be 1"),
          g.configErrorField("invalid_configuration", "Something else"), g.configErrorField("invalid_configuration", ""),
          g.configErrorField(undefined, undefined), g.configErrorField("invalid_wifi_networks", "")];""")
        self.assertEqual(out, [None, None, None, None, "wifi_networks"])

    def test_effects_follow_the_policy_and_the_staging(self):
        out = run_guided("""const none = {keys: [], restart_required: false};
          out = [g.configEffects(["ntrip_mount"], none),
                 g.configEffects(["ntrip_mount"], {keys: ["wifi_ssid"], restart_required: true}),
                 g.configEffects(["wifi_ssid"], {keys: ["wifi_pass", "wifi_ssid"], restart_required: true}),
                 g.configEffects([], {keys: [], restart_required: true}),
                 g.configEffects(["uart_id"], none), g.configEffects(["ntrip_mount"], undefined),
                 g.configEffects(["wifi_ssid", "ntrip_host"], none)];""")
        self.assertEqual(out[0], {"restart": False, "reload": True, "wifi": [], "caster": ["ntrip_mount"], "earlier": []})
        self.assertTrue(out[1]["restart"])
        self.assertEqual(out[1]["earlier"], ["wifi_ssid"])
        self.assertEqual(out[2]["earlier"], ["wifi_pass"])
        self.assertTrue(out[3]["restart"])
        self.assertTrue(out[4]["restart"])
        self.assertEqual(out[5], out[0])
        self.assertEqual((out[6]["wifi"], out[6]["caster"], out[6]["restart"]), (["wifi_ssid"], ["ntrip_host"], True))

    def test_api_failure_classifies_refusals_and_lost_answers(self):
        out = run_guided("""const err = (props) => Object.assign(new Error("x"), props);
          out = [err({status: 400, code: "invalid_configuration"}), err({status: 409, reasons: ["a", "b"]}),
                 err({status: 500}), err({name: "SessionLostError", status: 401}), new TypeError("Failed to fetch"),
                 err({name: "SyntaxError"}), err({name: "TimeoutError"}), err({name: "AbortError"}), null,
                 err({status: "400"}), err({status: 200})].map(g.apiFailure);""")
        refused = {"definitive": True, "uncertain": False}
        lost = {"status": 0, "code": None, "reasons": None, "definitive": False, "uncertain": True}
        self.assertEqual(out[0], dict(refused, status=400, code="invalid_configuration", reasons=None))
        self.assertEqual(out[1], dict(refused, status=409, code=None, reasons=["a", "b"]))
        self.assertEqual(out[2], dict(refused, status=500, code=None, reasons=None))
        self.assertEqual(out[3], dict(refused, status=401, code=None, reasons=None))
        self.assertEqual(out[4:9], [lost] * 5)
        self.assertEqual(out[9], lost)
        self.assertEqual(out[10], dict(lost, status=200))

    def test_api_request_error_contract_includes_forbidden_status(self):
        if NODE is None:
            self.skipTest("node is unavailable")
        source = APP_JS[APP_JS.index("let csrfToken="):APP_JS.index("const apiJsonBase=")]
        script = r"""
const answers = [];
const location = {hash: "#device/configuration", replace: () => {}};
const tr = (key, fallback) => fallback;
const fetch = async () => {
  const [status, payload] = answers.shift(), body = JSON.stringify(payload);
  return {status, ok: status < 400, body: null, json: async () => payload,
          text: async () => body, blob: async () => null, arrayBuffer: async () => null};
};
const probe = new Function("fetch", "location", "tr", process.argv[1] + "\nreturn {api};")(fetch, location, tr);
const shape = error => ({name: error.name, status: error.status, code: error.code === undefined ? "<undefined>" : error.code});
(async () => {
  const out = [];
  for (const answer of [[403, {error: "csrf_rejected", message: "x"}], [400, {error: "invalid_configuration", message: "y"}]]) {
    answers.push(answer);
    out.push(await probe.api.request("/api/config", {method: "PUT"}).then(() => null, shape));
  }
  console.log(JSON.stringify(out));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        run = subprocess.run([NODE, "-e", script, source], capture_output=True, text=True, timeout=10)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(json.loads(run.stdout), [
            {"name": "ForbiddenError", "status": 403, "code": "<undefined>"},
            {"name": "Error", "status": 400, "code": "invalid_configuration"}])
        self.assertIn("status is part of the error contract shared with #21c", APP_JS)


def node_json(expression):
    """JSON of a Node expression evaluated with i18n.js loaded as `i`."""
    run = subprocess.run([NODE, "-e", "const i=require('./i18n.js');console.log(JSON.stringify(%s))" % expression],
                         cwd=ROOT, capture_output=True, text=True, check=True)
    return json.loads(run.stdout)


def read_source(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as source:
        return source.read()


MARKER = r"""
const MARK = (over = {}) => Object.assign({v: 1, kind: "apply", route: "device/configuration", started_ms: 1000000,
  phase: "restarting", apply_id: "0123456789abcdef", boot_id: "aaaa0001", host: "rtk-test.local",
  target_ssid: "New", attempt: {keys: ["wifi_ssid"], wifi_ssid: "New"}}, over);
const T0 = 1000000;
"""
CASTER_PART = "caster caster:2101/MOUNT"
REPORTED = "<ui.guided_reported_settings> Wi-Fi ‘Field’ · %s · <ui.guided_ntrip_on>" % CASTER_PART


class TestRestartLogic(unittest.TestCase):
    """#23c Task 4: restart and result judgement by server identity, texts and the marker."""

    @classmethod
    def setUpClass(cls):
        if NODE is None:
            raise unittest.SkipTest("node is unavailable")
        cls.dictionaries = node_json("i.dictionaries")
        cls.v2de = node_json("i.v2de")

    def run_restart(self, script, **kwargs):
        return run_guided(MARKER + script, RESTART_EXPORTS, **kwargs)

    def english(self, stub):
        """The English text behind a stubbed tr() call ("<key>")."""
        match = re.fullmatch(r"<([\w.]+)>", stub)
        self.assertIsNotNone(match, stub)
        self.assertIn(match.group(1), self.dictionaries["en"])
        return self.dictionaries["en"][match.group(1)]

    def test_constants_of_the_restart_watch(self):
        out = self.run_restart("out = {route: g.CONFIG_ROUTE, field: g.FIELD_SETUP_ROUTE, key: g.RESTART_MARKER_KEY,"
                               " timing: g.RESTART_TIMING};")
        self.assertEqual((out["route"], out["field"], out["key"]),
                         ("device/connections/internet", "measure/setup", "rtk-restart-watch"))
        self.assertEqual(out["timing"], {
            "probeMs": 2000, "probeTimeoutMs": 3000, "requestTimeoutMs": 3000, "backNoticeMs": 1500,
            "backDeadlineMs": 90000, "notRestartedMs": 20000, "resultDeadlineMs": 90000,
            "settleDeadlineMs": 60000, "ntripObserveMs": 30000, "ntripSettleMs": 2000, "markerMaxAgeMs": 600000})

    def test_cause_table_covers_every_device_code(self):
        net_source, cfg_source = read_source("net.py"), read_source("cfg.py")
        codes = set(re.findall(r'"(access_point_not_found|authentication_failed|association_failed)"', net_source))
        self.assertEqual(len(codes), 3)
        for code in ("connection_timeout", "connection_lost"):
            self.assertIn('"%s"' % code, net_source)
            codes.add(code)
        for code in ("apply_interrupted", "storage_error"):
            self.assertIn('"%s"' % code, cfg_source)
            codes.add(code)
        # The network manager records "setup_mode" through cfg.rollback_pending_config.
        self.assertIn('rollback_pending_config("setup_mode")', net_source)
        codes.add("setup_mode")
        out = self.run_restart("out = {causes: g.CONFIG_FAILURE_CAUSES, unknown: g.failureCause('x_y'),"
                               " known: g.failureCause('setup_mode'), none: g.failureCause(null)};")
        self.assertEqual(set(out["causes"]), codes)
        for code, (key, english) in out["causes"].items():
            self.assertTrue(key.startswith("ui.guided_cause_"), key)
            self.assertEqual(self.dictionaries["en"][key], english, key)
            self.assertIn(key, self.dictionaries["de"])
            self.assertNotEqual(self.dictionaries["de"][key], english, key)
        self.assertEqual(out["unknown"], "<ui.guided_cause_unknown>|code=x_y")
        self.assertEqual(out["known"], "<%s>" % out["causes"]["setup_mode"][0])
        self.assertEqual(self.dictionaries["en"]["ui.guided_cause_unknown"], "unknown cause ({code})")
        self.assertTrue(out["none"].startswith("<ui.guided_cause_unknown>|code="))

    def test_unknown_cause_interpolates_with_the_real_translation(self):
        script = "out = {unknown: g.failureCause('x_y'), known: g.failureCause('authentication_failed')};"
        german, english = (run_guided(script, RESTART_EXPORTS, language=language) for language in ("de", "en"))
        self.assertIn("x_y", german["unknown"])
        self.assertNotEqual(german["unknown"], "unknown cause (x_y)")
        self.assertEqual(german["unknown"], self.dictionaries["de"]["ui.guided_cause_unknown"].replace("{code}", "x_y"))
        self.assertEqual(english["unknown"], "unknown cause (x_y)")
        self.assertEqual(german["known"], self.dictionaries["de"]["ui.guided_cause_authentication_failed"])
        self.assertEqual(english["known"], self.dictionaries["en"]["ui.guided_cause_authentication_failed"])

    def test_steps_map_live_states(self):
        out = self.run_restart(r"""
          const states = (live, back = true) => Object.fromEntries(g.restartSteps(live, back).map(x => [x.step, x.state]));
          const step = (live, name) => g.restartSteps(live, true).find(x => x.step === name);
          const full = {network: {access_state: "ONLINE", ntrip_state: "streaming"}, tracking_startup: {state: "ready"}};
          out = {order: g.restartSteps({}, true).map(x => x.step), none: states(null, false), empty: states({}, true),
                 back: states(full, true), notBack: states(full, false),
                 wifi: ["ONLINE", "CONNECTING", "APPLYING", undefined, "SETUP", "RECOVERY", "OTHER"]
                   .map(a => step({network: {access_state: a}}, "wifi")),
                 ntrip: ["streaming", "waiting_wifi", "connecting", "backoff", undefined, "auth_error", "mount_error"]
                   .map(n => step({network: {ntrip_state: n}}, "ntrip").state),
                 storage: ["ready", "loading", undefined, "error"].map(t => step({tracking_startup: {state: t}}, "storage").state),
                 nostartup: step({}, "storage").state, http: [step({}, "http"), g.restartSteps({}, false)[0]]};""")
        self.assertEqual(out["order"], ["http", "wifi", "ntrip", "storage"])
        wait = {"wifi": "wait", "ntrip": "wait", "storage": "wait"}
        self.assertEqual(out["none"], dict(wait, http="wait"))
        self.assertEqual(out["empty"], dict(wait, http="ok"))
        self.assertEqual(out["back"], {"http": "ok", "wifi": "ok", "ntrip": "ok", "storage": "ok"})
        self.assertEqual(out["notBack"], dict(wait, http="wait"))
        self.assertEqual([item["state"] for item in out["wifi"]], ["ok", "wait", "wait", "wait", "off", "fail", "wait"])
        self.assertEqual(self.english(out["wifi"][4]["detail"]), "Hotspot only")
        self.assertEqual(self.english(out["wifi"][5]["detail"]), "No Wi-Fi; hotspot on")
        self.assertEqual(out["wifi"][5]["value"], "RECOVERY")
        self.assertEqual(out["ntrip"], ["ok", "wait", "wait", "wait", "wait", "fail", "fail"])
        self.assertEqual(out["storage"], ["ok", "wait", "wait", "fail"])
        self.assertEqual(out["nostartup"], "wait")
        self.assertEqual([item["state"] for item in out["http"]], ["ok", "wait"])

    def test_ntrip_step_follows_the_cause_contract(self):
        out = self.run_restart(r"""
          const rows = [{ntrip_state: "disabled", ntrip_config: "switched_off"}, {ntrip_state: "disabled", ntrip_config: "incomplete"},
            {ntrip_state: "disabled", ntrip_config: null}, {ntrip_state: "disabled"}, {ntrip_state: "disabled", ntrip_config: "ready"},
            {ntrip_state: "backoff", last_ntrip_error_code: null}, {ntrip_state: "backoff", last_ntrip_error_code: "network_error"},
            {ntrip_state: "backoff", last_ntrip_error_code: "retries_exhausted"},
            {ntrip_state: "mount_error", last_ntrip_error_code: "retries_exhausted"},
            {ntrip_state: "auth_error", last_ntrip_error_code: "retries_exhausted"},
            {ntrip_state: "streaming", last_ntrip_error_code: "retries_exhausted"}];
          const steps = network => g.restartSteps({network: Object.assign({access_state: "ONLINE"}, network),
                                                   tracking_startup: {state: "ready"}}, true);
          out = {rows: rows.map(network => g.ntripStep({network})),
                 parkedBackoff: g.stepNotes(steps({ntrip_state: "backoff", last_ntrip_error_code: "retries_exhausted"}), false),
                 parkedMount: g.stepNotes(steps({ntrip_state: "mount_error", last_ntrip_error_code: "retries_exhausted"}), false),
                 auth: g.stepNotes(steps({ntrip_state: "auth_error"}), false),
                 idle: g.stepNotes(steps({ntrip_state: "disabled", ntrip_config: null}), true),
                 idleEarly: g.stepNotes(steps({ntrip_state: "disabled", ntrip_config: null}), false)};""")
        rows = [(row["state"], self.english(row["detail"]), row["parked"]) for row in out["rows"]]
        started = ("wait", "Not started yet", False)
        self.assertEqual(rows[0], ("off", "Switched off", False))
        self.assertEqual(rows[1], ("off", "Not set up", False))
        self.assertEqual(rows[2:5], [started] * 3)
        self.assertEqual([(state, parked) for state, _detail, parked in rows[5:7]], [("wait", False)] * 2)
        self.assertEqual(rows[7], ("fail", "Stopped after the retry limit", True))
        self.assertEqual(rows[8], ("fail", "Mountpoint not found", True))
        self.assertEqual(rows[9], ("fail", "Credentials rejected", True))
        self.assertEqual((rows[10][0], rows[10][2]), ("ok", False))
        park = ("NTRIP: the client stopped after its retry limit – check the caster settings and save them; "
                "saving, a Wi-Fi loss or a restart starts a new round.")
        mount = "NTRIP: mountpoint not found – check the caster settings."
        self.assertEqual([self.english(note) for note in out["parkedBackoff"]], [park])
        self.assertEqual([self.english(note) for note in out["parkedMount"]], [mount, park])
        self.assertEqual([self.english(note) for note in out["auth"]],
                         ["NTRIP: credentials rejected – check the caster settings."])
        self.assertEqual(out["idle"], ["NTRIP still waiting after 60 s – state: disabled."])
        self.assertEqual(out["idleEarly"], [])
        net_source = read_source("net.py")
        self.assertIn('"retries_exhausted"', net_source)
        config_state = net_source[net_source.index("def config_state"):]
        config_state = config_state[:config_state.index("\n    def ", 1)]
        self.assertIn('"switched_off"', config_state)
        self.assertIn('"incomplete"', config_state)
        self.assertIn('"ntrip_config": None', read_source("state/__init__.py"))

    def test_outcome_deadlines_come_first(self):
        out = self.run_restart(r"""
          const same = {system: {boot_id: "aaaa0001"}}, other = {system: {boot_id: "bbbb0002"}};
          const R = (marker, obs, dt) => g.restartOutcome(marker, obs, T0 + dt);
          const own = {status: "connected", apply_id: "0123456789abcdef", details: {ssid: "New"}};
          const verifying = MARK({phase: "verifying"});
          out = {r1: R(MARK(), {probe: "unreachable"}, 89999), r2: R(MARK(), {probe: "unreachable"}, 90000),
                 first: R(MARK(), {probe: "authenticated", live: other}, 90000),
                 n1: R(MARK(), {probe: "authenticated", live: same, sameBootSinceMs: T0}, 19999),
                 n2: R(MARK(), {probe: "authenticated", live: same, sameBootSinceMs: T0}, 20000),
                 unreachable: [20000, 60000, 89999].map(dt => R(MARK(), {probe: "unreachable", live: same, sameBootSinceMs: T0}, dt)),
                 signedOut: R(MARK(), {probe: "signed_out", live: same, sameBootSinceMs: T0}, 30000),
                 v0: R(verifying, {live: null, resumedMs: T0}, 89999), v1: R(verifying, {live: null, resumedMs: T0}, 90000),
                 v2: R(verifying, {live: {}, resumedMs: T0}, 89999), v3: R(verifying, {live: {}, resumedMs: T0}, 90000),
                 vLate: R(verifying, {live: other, result: own, resumedMs: T0}, 90000),
                 s0: R(MARK({phase: "settling", result: {state: "connected", ssid: "New"}}),
                       {settledFromMs: T0, steps: [{step: "storage", state: "wait"}]}, 59999),
                 s1: R(MARK({phase: "settling", result: {state: "connected", ssid: "New"}}),
                       {settledFromMs: T0, steps: [{step: "storage", state: "wait"}]}, 60000)};""")
        restarting = {"phase": "restarting", "outcome": None}
        self.assertEqual(out["r1"], restarting)
        self.assertEqual(out["r2"], {"phase": "final", "outcome": {"state": "timeout"}})
        self.assertEqual(out["first"], out["r2"])
        self.assertEqual(out["n1"], restarting)
        self.assertEqual(out["n2"], {"phase": "final", "outcome": {"state": "not_restarted", "uncertain": False}})
        self.assertEqual(out["unreachable"], [restarting] * 3)
        self.assertEqual(out["signedOut"], restarting)
        self.assertEqual(out["v0"], {"phase": "verifying", "outcome": None})
        self.assertEqual(out["v1"], {"phase": "final", "outcome": {"state": "open"}})
        self.assertEqual(out["v2"], {"phase": "verifying", "outcome": None})
        self.assertEqual(out["v3"], out["v1"])
        self.assertEqual(out["vLate"], out["v1"])
        self.assertEqual(out["s0"], {"phase": "settling", "outcome": {"state": "connected", "ssid": "New"}})
        self.assertEqual(out["s1"], {"phase": "final", "outcome": {"state": "connected", "ssid": "New"}})

    def test_restart_warning_names_measurement_and_line(self):
        out = self.run_restart(r"""out = [{measurement_progress: {active: true}}, {active_line: {state: "recording", name: "L <7>"}},
          {active_line: {state: "paused", name: "L <7>"}}, {measurement_progress: {active: true},
          active_line: {state: "recording", name: "L <7>"}}, null, {measurement_progress: {active: false}}].map(g.restartWarningText);""")
        line = ("Line ‘L <7>’ is recording. The restart interrupts the recording; "
                "points already saved are kept.")
        self.assertEqual(out[0], "<ui.guided_warn_measurement>")
        self.assertEqual(self.english(out[0]),
                         "A measurement is running. The restart cancels it; points already saved are kept.")
        self.assertEqual(out[1], line)
        self.assertEqual(out[2], "")
        self.assertEqual(out[3], "<ui.guided_warn_measurement> " + line)
        self.assertEqual(out[4:], ["", ""])

    def test_apply_id_is_sixteen_hex_characters(self):
        out = self.run_restart("out = [g.newApplyId(n => Uint8Array.from([0, 1, 2, 3, 250, 251, 252, 255]).slice(0, n)),"
                               " g.newApplyId(), g.newApplyId()];")
        self.assertEqual(out[0], "00010203fafbfcff")
        for value in out[1:]:
            self.assertIsNotNone(re.fullmatch("[0-9a-f]{16}", value), value)
        self.assertNotEqual(out[1], out[2])

    def test_boot_change_needs_a_boot_id(self):
        out = self.run_restart(r"""const m = MARK();
          out = [g.bootChanged(m, {system: {boot_id: "bbbb0002"}}), g.bootChanged(m, {system: {boot_id: "aaaa0001"}}),
                 g.bootChanged(m, {system: {}}), g.bootChanged(m, {system: {boot_id: null}}), g.bootChanged(m, {system: {boot_id: 7}}),
                 g.bootChanged(m, null), g.bootChanged(m, {}),
                 g.bootChanged(m, {system: {boot_id: "aaaa0001", uptime_sec: 1}})];""")
        self.assertEqual(out, [True, False, None, None, None, None, None, False])

    def test_result_needs_the_apply_id(self):
        out = self.run_restart(r"""const m = MARK();
          out = [g.judgeConfigResult(m, {status: "connected", apply_id: "0123456789abcdef", details: {ssid: "New"}}),
                 g.judgeConfigResult(m, {status: "failed", apply_id: "0123456789abcdef",
                                         details: {code: "access_point_not_found", restored: ["wifi_ssid"]}}),
                 g.judgeConfigResult(m, {status: "pending", apply_id: "0123456789abcdef", details: {}}),
                 g.judgeConfigResult(m, {status: "pending", details: {code: "result_unreadable"}}),
                 g.judgeConfigResult(m, {status: "none", details: {}, apply_id: null}), g.judgeConfigResult(m, null)];""")
        self.assertEqual(out[0], {"state": "connected", "ssid": "New"})
        self.assertEqual(out[1], {"state": "failed", "code": "access_point_not_found", "restored": ["wifi_ssid"]})
        self.assertEqual(out[2:], [{"state": "none"}] * 4)

    def test_result_from_an_intermediate_boot_is_only_observed(self):
        out = self.run_restart(r"""const m = MARK({apply_id: "aaaaaaaaaaaaaaaa", boot_id: "0000000a"});
          const result = {status: "connected", apply_id: "bbbbbbbbbbbbbbbb", boot_id: "0000000b", details: {ssid: "Other"}};
          const judged = g.judgeConfigResult(m, result);
          out = {judged, phase: g.restartOutcome(Object.assign({}, m, {phase: "verifying"}),
                   {live: {system: {boot_id: "0000000c"}}, result, resumedMs: T0}, T0 + 1000),
                 view: g.restartOutcomeView(judged, m, {})};""")
        self.assertEqual(out["judged"], {"state": "observed", "reported": "connected", "ssid": "Other"})
        self.assertEqual(out["phase"], {"phase": "settling", "outcome": out["judged"]})
        self.assertIn("does not belong to this request", out["view"]["text"])
        self.assertIn("connected to ‘Other’", out["view"]["text"])
        self.assertNotIn("Applied", out["view"]["text"])

    def test_foreign_result_after_a_lost_request_is_only_observed(self):
        out = self.run_restart(r"""const m = MARK({uncertain: true, apply_id: "aaaaaaaaaaaaaaaa"});
          out = [g.judgeConfigResult(m, {status: "failed", apply_id: null, details: {code: "authentication_failed", restored: []}}),
                 g.judgeConfigResult(m, {status: "failed", details: {code: "authentication_failed", restored: []}}),
                 g.judgeConfigResult(m, {status: "connected", apply_id: null, boot_id: "0000000d", details: {ssid: "New"}}),
                 g.judgeConfigResult(Object.assign({}, m, {apply_id: undefined}), {status: "connected", apply_id: null, details: {}})];""")
        self.assertEqual([item["state"] for item in out], ["observed"] * 4)
        self.assertEqual([item["reported"] for item in out], ["failed", "failed", "connected", "connected"])

    def test_restart_answers_must_be_decoded_json(self):
        out = self.run_restart(r"""const a = g.restartAnswerAccepted;
          out = {yes: [a("apply", {applying: true, apply_id: "0123456789abcdef"}),
                       a("restore", {restored: true, schema_version: 2, sections: [], kept: []}),
                       a("reboot", {restarting: true, apply_id: null})],
                 no: [a("apply", {applying: "true"}), a("restore", {restored: false}), a("reboot", {}), a("apply", null),
                      a("apply", []), a("restore", new Response('{"restored": true}', {status: 202})),
                      a("other", {applying: true}), a("constructor", {constructor: true}), a("apply", undefined)],
                 scope: g.restoreScope({sections: ["connections", 5, "measurement"], kept: "wifi"}),
                 scopeNone: [g.restoreScope(null), g.restoreScope([]), g.restoreScope({kept: ["wifi", null]})]};""")
        self.assertEqual(out["yes"], [True] * 3)
        self.assertEqual(out["no"], [False] * 9)
        self.assertEqual(out["scope"], {"sections": ["connections", "measurement"], "kept": []})
        self.assertEqual(out["scopeNone"], [{"sections": [], "kept": []}, {"sections": [], "kept": []},
                                            {"sections": [], "kept": ["wifi"]}])

    def test_failed_view_separates_decision_storage_and_connection(self):
        out = self.run_restart(r"""
          const failed = {state: "failed", code: "access_point_not_found", restored: ["wifi_ssid", "wifi_pass"]};
          const config = {wifi_ssid: "Field", ntrip_host: "caster", ntrip_port: 2101, ntrip_mount: "MOUNT", ntrip_enabled: true};
          const m = MARK({attempt: {keys: ["wifi_ssid"], wifi_ssid: "New"}});
          const view = (outcome, marker, context) => g.restartOutcomeView(outcome, marker, context);
          out = {bare: view(failed, m, {}),
                 states: ["SETUP", "CONNECTING", "APPLYING", "ONLINE", "RECOVERY", undefined]
                   .map(a => view(failed, m, {config, live: {network: {access_state: a}}}).notes),
                 unset: view(failed, m, {config: Object.assign({}, config, {wifi_ssid: ""}), live: {}}).notes,
                 empty: view(Object.assign({}, failed, {restored: []}), m, {config}).notes,
                 caster: view(failed, MARK({attempt: {keys: ["ntrip_mount", "wifi_ssid"]}}), {config, live: {}}).notes,
                 off: view(failed, m, {config: Object.assign({}, config, {ntrip_host: "", ntrip_enabled: false})}).notes};""")
        marked = "<ui.guided_marked_set_back> Wi-Fi name (SSID), Wi-Fi password"
        self.assertEqual(out["bare"]["notes"], [marked, "<ui.guided_wifi_now_unknown>"])
        self.assertEqual(out["bare"]["text"],
                         "Not applied: <ui.guided_cause_access_point_not_found>. The device returns to the previous Wi-Fi settings.")
        self.assertEqual(out["bare"]["actions"], ["edit", "close"])
        connection = {
            "SETUP": "Wi-Fi now: no connection – only the setup hotspot is on.",
            "CONNECTING": "Wi-Fi now: connecting …", "APPLYING": "Wi-Fi now: connecting …",
            "ONLINE": "Wi-Fi now: connected.",
            "RECOVERY": ("Wi-Fi now: no connection to a Wi-Fi network – the rescue hotspot is on "
                         "and the device keeps trying."),
            None: "Wi-Fi now: unknown."}
        for notes, access in zip(out["states"], ["SETUP", "CONNECTING", "APPLYING", "ONLINE", "RECOVERY", None]):
            self.assertEqual(notes[:2], [marked, REPORTED])
            self.assertEqual(len(notes), 3)
            self.assertEqual(self.english(notes[2]), connection[access])
        self.assertIn("rescue hotspot", self.english(out["states"][4][2]))
        self.assertIn("no connection to a Wi-Fi network", self.english(out["states"][4][2]))
        self.assertEqual(out["unset"][1], "<ui.guided_reported_settings> <ui.guided_wifi_not_set> · %s · "
                                          "<ui.guided_ntrip_on>" % CASTER_PART)
        self.assertEqual(out["empty"], [REPORTED, "<ui.guided_wifi_now_unknown>"])
        self.assertEqual(out["caster"][-1], "<ui.guided_caster_kept>")
        self.assertEqual(self.english(out["caster"][-1]),
                         "The caster changes from this save already took effect and stay in effect.")
        self.assertEqual(out["off"][1], "<ui.guided_reported_settings> Wi-Fi ‘Field’ · "
                                        "<ui.guided_no_caster> · <ui.guided_ntrip_off>")
        texts = [out["bare"]["text"]] + [note for notes in out["states"] for note in notes]
        english = [self.english(text) if re.fullmatch(r"<[\w.]+>", text) else text for text in texts]
        english += [self.english(note) for note in (out["bare"]["notes"] + out["caster"]) if re.fullmatch(r"<[\w.]+>", note)]
        for text in english:
            for word in ("active", "complete", "finished"):
                self.assertNotIn(word, text.lower(), text)
        source = read_source("src/78-guided-apply.js")
        for stale in ("Previous Wi-Fi settings are active", "not complete yet", "Set back:", "Saved on the device now"):
            self.assertNotIn(stale, source)

    def judge_records(self, data, language=None):
        """Judge and view device records in Node: DATA = [{marker, result, config, live}]."""
        return run_guided(MARKER + r"""out = DATA.map(item => {
            const outcome = g.judgeConfigResult(item.marker, item.result);
            return {outcome, view: g.restartOutcomeView(outcome, item.marker,
                    {config: item.config, live: item.live, steps: g.restartSteps(item.live, true), phase: "final"})};
          });""", RESTART_EXPORTS, language=language, data=data)

    def device(self):
        """A device record fixture (test_config_identity), torn down with this test."""
        fixture = _IdentityCase()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.addCleanup(fixture.tearDown)
        return fixture

    def test_view_texts_follow_real_backend_records(self):
        from unittest import mock
        fixture = self.device()
        before = cfg.BOOT_ID
        fixture.armed()
        with mock.patch.object(cfg._store, "complete_rollback", return_value=False):
            self.assertIs(cfg.rollback_pending_config("authentication_failed"), False)
        state.app.stats["access_state"] = "RECOVERY"
        status, config = fixture.call(web._handle_config_request, "/api/config", "GET")
        self.assertEqual(status, 200)
        marker = {"v": 1, "kind": "apply", "phase": "verifying", "started_ms": 0, "apply_id": APPLY_ID,
                  "boot_id": before, "host": "rtk.local", "attempt": {"keys": ["wifi_ssid"], "wifi_ssid": "New"}}
        rollback = {"marker": marker, "result": web.config_result_for_clients(), "config": config,
                    "live": json.loads(json.dumps(web.ui_live_snapshot()))}
        self.assertEqual(rollback["result"]["details"]["restored"], ["wifi_ssid"])
        self.assertEqual(config["wifi_ssid"], "New")

        # A boot with a protected, unreadable config.json: the device reports default values and warns.
        fixture = self.device()
        for suffix in (".tmp", ".previous"):
            if os.path.exists(cfg.CONFIG["config_file"] + suffix):
                os.remove(cfg.CONFIG["config_file"] + suffix)
        with open(cfg.CONFIG["config_file"], "wb") as broken:
            broken.write(b"{")
        cfg.CONFIG.clear()
        cfg.CONFIG.update(cfg.DEFAULT_CONFIG)
        cfg.load_config()
        self.assertIs(cfg.config_file_protected(), True)
        _status, protected_config = fixture.call(web._handle_config_request, "/api/config", "GET")
        self.assertEqual(protected_config["wifi_ssid"], "")
        protected_live = json.loads(json.dumps(web.ui_live_snapshot()))
        self.assertTrue(any(item.get("key") == "config_file" for item in protected_live["config_warnings"]))
        failed = {"schema_version": 2, "status": "failed", "uptime_sec": 3, "boot_id": cfg.BOOT_ID, "apply_id": APPLY_ID,
                  "details": {"code": "storage_error", "restored": []}}
        unwarned = dict(protected_live, config_warnings=[])
        protected = [{"marker": marker, "result": failed, "config": protected_config, "live": protected_live},
                     {"marker": marker, "result": dict(failed, apply_id=None), "config": protected_config,
                      "live": protected_live},
                     {"marker": marker, "result": failed, "config": protected_config, "live": unwarned}]
        data = [rollback] + protected

        out = self.judge_records(data)
        caster = ("caster %s:%s/%s" % (config["ntrip_host"], config["ntrip_port"], config["ntrip_mount"])
                  if config["ntrip_host"] else "<ui.guided_no_caster>")
        ntrip = "<ui.guided_ntrip_%s>" % ("on" if config["ntrip_enabled"] else "off")
        self.assertEqual(out[0]["outcome"]["state"], "failed")
        self.assertEqual(out[0]["view"]["notes"], [
            "<ui.guided_marked_set_back> Wi-Fi name (SSID)",
            "<ui.guided_reported_settings> Wi-Fi ‘New’ · %s · %s" % (caster, ntrip),
            "<ui.guided_wifi_now_recovery>"])
        file_note = "<ui.guided_config_file_unread>"
        self.assertEqual(self.english(file_note), "The device could not read its saved configuration file at "
                                                  "start-up; the settings shown may be default values.")
        self.assertEqual([item["outcome"]["state"] for item in out[1:]], ["failed", "observed", "failed"])
        for item in out[1:3]:
            self.assertTrue(item["view"]["notes"][0].startswith(
                "<ui.guided_reported_settings> <ui.guided_wifi_not_set> · "), item["view"]["notes"])
            self.assertEqual(item["view"]["notes"][1], file_note)
        self.assertNotIn(file_note, out[3]["view"]["notes"])
        self.assertTrue(out[3]["view"]["notes"][0].startswith("<ui.guided_reported_settings> "))

        english = self.judge_records(data, language="en")
        for item in english:
            for text in [item["view"]["text"]] + item["view"]["notes"]:
                for word in ("restored", "active", "complete", "saved on"):
                    self.assertNotIn(word, text.lower(), text)
        german = self.judge_records(data, language="de")
        self.assertTrue(german[0]["view"]["notes"][0].startswith(self.dictionaries["de"]["ui.guided_marked_set_back"] + " "))
        self.assertTrue(german[0]["view"]["notes"][1].startswith(self.dictionaries["de"]["ui.guided_reported_settings"] + " "))
        for item in german[1:3]:
            self.assertTrue(item["view"]["notes"][0].startswith(self.dictionaries["de"]["ui.guided_reported_settings"] + " "))
            self.assertEqual(item["view"]["notes"][1], self.dictionaries["de"]["ui.guided_config_file_unread"])

    def test_reboot_is_done_only_after_settling(self):
        out = self.run_restart(r"""
          const steps = storage => [{step: "http", state: "ok"}, {step: "wifi", state: "ok"}, {step: "ntrip", state: "off"},
                                    {step: "storage", state: storage}];
          const reboot = over => MARK(Object.assign({kind: "reboot", attempt: null, target_ssid: null}, over));
          const other = {system: {boot_id: "bbbb0002"}};
          out = {verifying: g.restartOutcome(reboot({phase: "verifying"}), {live: other, resumedMs: T0}, T0 + 1000),
                 unknownBoot: g.restartOutcome(reboot({phase: "verifying"}), {live: {}, resumedMs: T0}, T0 + 1000),
                 fromRestarting: g.restartOutcome(reboot(), {probe: "authenticated", live: other}, T0 + 1000),
                 view: g.restartOutcomeView(null, reboot({phase: "settling"}), {phase: "settling"}),
                 waiting: g.restartOutcome(reboot({phase: "settling"}), {settledFromMs: T0, steps: steps("wait")}, T0 + 1000),
                 done: g.restartOutcome(reboot({phase: "settling"}), {settledFromMs: T0, steps: steps("ok")}, T0 + 1000),
                 observed: g.restartOutcome(reboot({phase: "settling", uncertain: true}),
                                            {settledFromMs: T0, steps: steps("fail")}, T0 + 1000)};""")
        self.assertEqual(out["verifying"], {"phase": "settling", "outcome": None})
        self.assertEqual(out["unknownBoot"], {"phase": "verifying", "outcome": None})
        self.assertEqual(out["fromRestarting"], {"phase": "settling", "outcome": None})
        self.assertEqual(self.english(out["view"]["text"]), "Restart detected – checking whether the device is ready …")
        self.assertNotIn("<ui.guided_outcome_done>", out["view"]["text"])
        self.assertEqual(out["view"]["actions"], [])
        self.assertEqual(out["waiting"], {"phase": "settling", "outcome": None})
        self.assertEqual(out["done"], {"phase": "final", "outcome": {"state": "done"}})
        self.assertEqual(out["observed"], {"phase": "final", "outcome": {"state": "restart_observed"}})

    def test_result_and_readiness_are_separate(self):
        out = self.run_restart(r"""
          const own = {status: "connected", apply_id: "0123456789abcdef", details: {ssid: "New"}};
          const live = (storage, ntrip) => ({system: {boot_id: "bbbb0002"}, tracking_startup: {state: storage},
                                             network: {access_state: "ONLINE", ntrip_state: ntrip || "streaming"}});
          const early = g.restartOutcome(MARK({phase: "verifying"}), {live: live("loading"), result: own, resumedMs: T0}, T0 + 1000);
          const settling = MARK({phase: "settling", result: early.outcome});
          const settle = l => g.restartOutcome(settling, {settledFromMs: T0, steps: g.restartSteps(l, true)}, T0 + 2000);
          out = {early, loading: settle(live("loading")), ready: settle(live("ready")), error: settle(live("error")),
                 errorNotes: g.stepNotes(g.restartSteps(live("error"), true), false),
                 auth: settle(live("ready", "auth_error")), authNotes: g.stepNotes(g.restartSteps(live("ready", "auth_error"), true), false)};""")
        connected = {"state": "connected", "ssid": "New"}
        self.assertEqual(out["early"], {"phase": "settling", "outcome": connected})
        self.assertEqual(out["loading"], {"phase": "settling", "outcome": connected})
        for name in ("ready", "error", "auth"):
            self.assertEqual(out[name], {"phase": "final", "outcome": connected})
        self.assertEqual([self.english(note) for note in out["errorNotes"]],
                         ["Survey storage reports an error – open Status for details."])
        self.assertEqual([self.english(note) for note in out["authNotes"]],
                         ["NTRIP: credentials rejected – check the caster settings."])

    def test_outcome_view_texts_and_actions(self):
        out = self.run_restart(r"""
          const v = (outcome, over, context) => g.restartOutcomeView(outcome, MARK(over), context || {});
          const done = [{step: "storage", state: "fail", value: "error"}];
          out = {
            connected: v({state: "connected", ssid: "O'Brien & <Co>"}, {}),
            connectedNoHost: v({state: "connected", ssid: "Barn"}, {host: ""}),
            restore: v({state: "connected", ssid: "Barn"}, {kind: "restore", sections: ["connections"], kept: ["wifi"]}),
            restoreFull: v({state: "connected", ssid: "Barn"}, {kind: "restore", sections: ["connections", "measurement"], kept: []}),
            failedRestore: v({state: "failed", code: "authentication_failed", restored: ["wifi_ssid"]},
                             {kind: "restore", attempt: {keys: ["ntrip_host"]}}),
            observed: v({state: "observed", reported: "failed", code: "connection_lost"}, {}),
            timeout: v({state: "timeout"}, {}), timeoutNoSsid: v({state: "timeout"}, {target_ssid: null}),
            timeoutNoHost: v({state: "timeout"}, {host: null}),
            notRestarted: v({state: "not_restarted", uncertain: false}, {}),
            notRestartedUncertain: v({state: "not_restarted", uncertain: true}, {uncertain: true}),
            notRestartedReboot: v({state: "not_restarted"}, {kind: "reboot"}),
            done: v({state: "done"}, {kind: "reboot"}, {steps: done}),
            restartObserved: v({state: "restart_observed"}, {kind: "reboot", uncertain: true}, {steps: done}),
            open: v({state: "open"}, {}),
            historicBoot: g.restartOutcomeView({state: "historic_boot", reported: "connected", ssid: "Field"}, null, {}),
            historicUnknown: g.restartOutcomeView({state: "historic_unknown", reported: "failed", code: "setup_mode"}, null, {}),
            phases: ["restarting", "resuming", "back", "verifying", "settling"].map(phase => v(null, {phase}, {phase})),
            rebootVerifying: v(null, {kind: "reboot", phase: "verifying"}, {phase: "verifying"})};""")
        actions = lambda name: out[name]["actions"]
        self.assertEqual(out["connected"]["text"],
                         "Applied: connected to ‘O'Brien & <Co>’. Portal: http://rtk-test.local/")
        self.assertEqual(out["connected"]["kind"], "connected")
        self.assertEqual(out["connectedNoHost"]["text"], "Applied: connected to ‘Barn’.")
        for name in ("connected", "connectedNoHost", "restore", "restoreFull"):
            self.assertEqual(actions(name), ["field_setup", "close"], name)
        self.assertEqual(out["restore"]["text"], "Backup restored: connected to ‘Barn’.")
        self.assertEqual([self.english(note) for note in out["restore"]["notes"]], [
            "This backup comes from older firmware and contained only the Wi-Fi and NTRIP settings.",
            "The backup contains no Wi-Fi network; the device kept its Wi-Fi access."])
        self.assertEqual(out["restoreFull"]["notes"], [])
        self.assertEqual(out["failedRestore"]["text"],
                         "Backup restored without its Wi-Fi settings: <ui.guided_cause_authentication_failed>. The device "
                         "returns to the previous Wi-Fi settings; the other settings from the backup apply.")
        self.assertNotIn("<ui.guided_caster_kept>", out["failedRestore"]["notes"])
        self.assertEqual(actions("failedRestore"), ["edit", "close"])
        self.assertEqual(out["observed"]["text"], "The device reports: not applied: <ui.guided_cause_connection_lost>. "
                                                  "This result does not belong to this request for certain.")
        self.assertEqual(out["observed"]["notes"], ["<ui.guided_wifi_now_unknown>"])
        self.assertEqual(actions("observed"), ["edit", "close"])
        self.assertEqual(out["timeout"]["text"],
                         "No answer after 90 s – connect the phone to ‘New’ and reload the page.")
        self.assertEqual(out["timeout"]["notes"], ["Portal: http://rtk-test.local/"])
        self.assertEqual(actions("timeout"), ["open_portal", "close"])
        self.assertEqual(self.english(out["timeoutNoSsid"]["text"]),
                         "No answer after 90 s – connect the phone to the device's network and reload the page.")
        self.assertEqual((out["timeoutNoHost"]["notes"], actions("timeoutNoHost")), ([], ["close"]))
        self.assertEqual(self.english(out["notRestarted"]["text"]),
                         "The device did not restart; the change is not applied yet.")
        self.assertEqual(self.english(out["notRestartedUncertain"]["text"]),
                         "It is not clear whether the device received the request; it did not restart.")
        self.assertEqual(actions("notRestarted"), ["retry_apply", "close"])
        self.assertEqual(actions("notRestartedUncertain"), ["retry_apply", "close"])
        self.assertEqual(actions("notRestartedReboot"), ["close"])
        self.assertEqual(self.english(out["done"]["text"]), "Restart complete.")
        self.assertEqual([self.english(note) for note in out["done"]["notes"]],
                         ["Survey storage reports an error – open Status for details."])
        self.assertEqual(self.english(out["restartObserved"]["text"]),
                         "A restart was observed. The answer to the request was lost, so it is not certain that "
                         "this request caused it.")
        self.assertEqual(out["restartObserved"]["notes"], out["done"]["notes"])
        self.assertEqual(self.english(out["open"]["text"]),
                         "No final result yet. The steps above show the current state; reload this page later.")
        for name in ("done", "restartObserved", "open"):
            self.assertEqual(actions(name), ["close"], name)
        self.assertEqual(out["historicBoot"]["text"],
                         "Configuration result since the last device start: connected to ‘Field’.")
        self.assertEqual(out["historicUnknown"]["text"],
                         "Last recorded configuration result (time unknown): not applied: <ui.guided_cause_setup_mode>.")
        self.assertEqual((actions("historicBoot"), actions("historicUnknown")), (["edit", "close"], ["edit", "close"]))
        self.assertEqual([self.english(view["text"]) for view in out["phases"]], [
            "The device is restarting …", "Checking whether the device restarted …",
            "The device is reachable again – sign in to see the result.",
            "Checking the new Wi-Fi connection (the device must hold it for 10 s) …",
            "Restart detected – checking whether the device is ready …"])
        self.assertEqual([view["kind"] for view in out["phases"]],
                         ["restarting", "resuming", "back", "verifying", "settling"])
        self.assertEqual([(view["notes"], view["actions"]) for view in out["phases"]], [([], [])] * 5)
        self.assertEqual(self.english(out["rebootVerifying"]["text"]), "Waiting for the device to start …")

    def test_guided_texts_have_german_translations(self):
        source = read_source("src/78-guided-apply.js")
        templates = re.findall(r'capacityText\("((?:[^"\\]|\\.)*)"', source)
        self.assertGreaterEqual(len(templates), 15)
        for template in templates:
            self.assertIn(template, self.v2de, template)
            german = self.v2de[template]
            self.assertNotEqual(german, template, template)
            self.assertEqual(sorted(re.findall(r"\{\w+\}", german)), sorted(re.findall(r"\{\w+\}", template)), template)
        pairs = re.findall(r'\btr\("(ui\.guided_\w+)","((?:[^"\\]|\\.)*)"', source)
        causes = self.run_restart("out = g.CONFIG_FAILURE_CAUSES;")
        pairs += [tuple(entry) for entry in causes.values()]
        self.assertGreaterEqual(len(pairs), 50)
        keys = [key for key in self.dictionaries["en"] if key.startswith("ui.guided_")]
        for key, english in pairs:
            self.assertEqual(self.dictionaries["en"].get(key), english.replace('\\"', '"'), key)
        for key in keys:
            self.assertIn(key, self.dictionaries["de"], key)
            self.assertNotEqual(self.dictionaries["de"][key], self.dictionaries["en"][key], key)
        out = self.run_restart('out = ["http", "wifi", "ntrip", "storage"].map(g.restartStepLabel);')
        self.assertEqual(out[0], "HTTP")
        self.assertEqual(out[2], "NTRIP")
        self.assertEqual((self.english(out[1]), self.english(out[3])), ("Wi-Fi", "Survey storage"))

    def test_marker_round_trip_keeps_only_allowed_fields(self):
        out = self.run_restart(r"""
          const memory = () => { const items = {}; return {items, getItem: k => k in items ? items[k] : null,
            setItem: (k, v) => { items[k] = String(v); }, removeItem: k => { delete items[k]; }}; };
          const broken = {getItem() { throw new Error("denied"); }, setItem() { throw new Error("denied"); },
                          removeItem() { throw new Error("denied"); }};
          const store = memory();
          const written = g.writeRestartMarker(store, MARK({before: {status: "connected"}, before_ok: true, extra: "x",
            attempt: {keys: ["ntrip_host", "ntrip_mount", "wifi_pass", "wifi_ssid"], wifi_ssid: "N", wifi_pass: "secret123",
                      ntrip_pass: "p", ntrip_user: "alice", ntrip_host: "caster.example", ntrip_mount: "M",
                      wifi_networks: [{ssid: "A", password: "x"}]}}));
          const text = store.items["rtk-restart-watch"];
          const atLimit = g.readRestartMarker(store, T0 + 600000), present = "rtk-restart-watch" in store.items;
          const tooOld = g.readRestartMarker(store, T0 + 600001), removed = !("rtk-restart-watch" in store.items);
          const v2 = memory(); v2.items["rtk-restart-watch"] = JSON.stringify({v: 2, started_ms: T0});
          const junk = memory(); junk.items["rtk-restart-watch"] = "{";
          const empty = memory();
          g.clearRestartMarker(broken);
          out = {written, text, atLimit, present, tooOld, removed, v2: g.readRestartMarker(v2, T0), v2gone: Object.keys(v2.items),
                 junk: g.readRestartMarker(junk, T0), junkGone: Object.keys(junk.items), empty: g.readRestartMarker(empty, T0),
                 brokenRead: g.readRestartMarker(broken, T0), brokenWrite: g.writeRestartMarker(broken, MARK()),
                 nullRead: g.readRestartMarker(null, T0), nullWrite: g.writeRestartMarker(undefined, MARK())};""")
        self.assertIs(out["written"], True)
        for secret in ("secret123", '"password"', "alice", "wifi_networks", "before", "extra", "ntrip_pass", '"p"'):
            self.assertNotIn(secret, out["text"])
        for kept in ('"N"', "caster.example", '"M"', "0123456789abcdef", "aaaa0001"):
            self.assertIn(kept, out["text"])
        stored = json.loads(out["text"])
        self.assertEqual(stored["attempt"], {"keys": ["ntrip_host", "ntrip_mount", "wifi_pass", "wifi_ssid"],
                                             "wifi_ssid": "N", "ntrip_host": "caster.example", "ntrip_mount": "M"})
        self.assertEqual(stored["v"], 1)
        self.assertTrue(set(stored) <= {"v", "kind", "route", "started_ms", "phase", "apply_id", "boot_id", "uncertain",
                                        "target_ssid", "host", "attempt", "sections", "kept", "result", "outcome"})
        self.assertEqual(out["atLimit"], stored)
        self.assertIs(out["present"], True)
        self.assertIsNone(out["tooOld"])
        self.assertIs(out["removed"], True)
        self.assertEqual((out["v2"], out["v2gone"], out["junk"], out["junkGone"], out["empty"]), (None, [], None, [], None))
        self.assertEqual((out["brokenRead"], out["brokenWrite"], out["nullRead"], out["nullWrite"]), (None, False, None, False))



# A fake clock for watchRestart: sleep and answer latency advance it; a source is a list (its last item repeats)
# or a function of the elapsed time; an Error item is thrown. The guard ends a watch that would never stop.
WATCH = r"""
const LIVE = (o = {}) => ({system: o.boot === null ? {} : Object.assign({boot_id: o.boot || "aaaa0001"}, o.uptime === undefined ? {} : {uptime_sec: o.uptime}),
  network: {access_state: o.access || "ONLINE", ntrip_state: o.ntrip || "streaming", ntrip_config: "ready"},
  tracking_startup: {state: o.storage || "ready"}});
const OWN = (status, details, id) => ({status, details: details || {}, apply_id: id === undefined ? "0123456789abcdef" : id, boot_id: "bbbb0002"});
function runWatch(marker, sources, options = {}) {
  let clock = T0, alive = true;
  const calls = [], renders = [], saves = [], latency = options.latency || {};
  const take = (name, source) => async () => {
    calls.push(name);
    if (calls.length > 20000) throw new Error("the watch does not end");
    if (options.onCall) options.onCall(name, ctl);
    const entry = typeof source === "function" ? source(clock - T0) : source.length > 1 ? source.shift() : source[0];
    clock += latency[name] || 0;
    if (entry instanceof Error) throw entry;
    return entry;
  };
  const ctl = {stop() { alive = false; }};
  const deps = {now: () => clock, sleep: async ms => { calls.push("sleep"); clock += ms; },
    probe: take("probe", sources.probe || ["unreachable"]), live: take("live", sources.live || [null]),
    result: take("result", sources.result || [{status: "pending"}]),
    save: m => { calls.push("save"); saves.push(JSON.parse(JSON.stringify(m))); },
    reload: () => { calls.push("reload"); },
    render: s => { calls.push("render"); renders.push(JSON.parse(JSON.stringify(s))); },
    alive: () => alive};
  return g.watchRestart(marker, deps).then(outcome => ({outcome, calls, renders, saves, elapsed: clock - T0, marker}));
}
const stepState = (render, step) => (render.steps || []).find(item => item.step === step).state;
"""
WATCH_EXPORTS = RESTART_EXPORTS + ", watchRestart"


class TestRestartWatch(unittest.TestCase):
    """#23c Task 6: the restart watch without DOM, on a fake clock."""

    @classmethod
    def setUpClass(cls):
        if NODE is None:
            raise unittest.SkipTest("node is unavailable")
        cls.dictionaries = node_json("i.dictionaries")

    def run_watch(self, script):
        if NODE is None:
            raise unittest.SkipTest("node is unavailable")
        prelude = PRELUDE % {"exports": WATCH_EXPORTS, "factory": json.dumps(FACTORY), "field": json.dumps(FIELD)}
        body = MARKER + WATCH + "let out;\n(async () => {\n" + script + "\n})().then(() => console.log(JSON.stringify(out)), " \
            "error => { console.error(error); process.exit(1); });"
        run = subprocess.run([NODE, "-e", prelude + body], cwd=ROOT, capture_output=True, text=True, timeout=60)
        if run.returncode != 0:
            raise AssertionError(run.stderr)
        return json.loads(run.stdout)

    def test_watch_reloads_once_when_the_session_is_gone(self):
        out = self.run_watch(r"""
          const run = await runWatch(MARK(), {probe: ["authenticated", "authenticated", "unreachable", "unreachable",
                                                      "unreachable", "signed_out"], live: [LIVE()]});
          out = {outcome: run.outcome, reloads: run.calls.filter(c => c === "reload").length, tail: run.calls.slice(-4),
                 last: run.saves[run.saves.length - 1], phases: run.renders.map(r => r.phase),
                 probes: run.calls.filter(c => c === "probe").length};""")
        self.assertEqual(out["outcome"], {"state": "back"})
        self.assertEqual(out["reloads"], 1)
        self.assertEqual(out["tail"], ["save", "render", "sleep", "reload"])
        self.assertEqual(out["last"]["phase"], "back")
        self.assertEqual(out["phases"][0], "restarting")
        self.assertEqual(out["phases"][-1], "back")
        self.assertEqual(set(out["phases"]), {"restarting", "back"})
        self.assertEqual(out["probes"], 6)

    def test_watch_times_out_without_reload(self):
        out = self.run_watch(r"""
          const run = await runWatch(MARK(), {probe: ["unreachable"]});
          out = {outcome: run.outcome, elapsed: run.elapsed, reload: run.calls.includes("reload"),
                 last: run.saves[run.saves.length - 1], render: run.renders[run.renders.length - 1]};""")
        self.assertEqual(out["outcome"], {"state": "timeout"})
        self.assertGreaterEqual(out["elapsed"], 90000)
        self.assertLessEqual(out["elapsed"], 92000)
        self.assertFalse(out["reload"])
        self.assertEqual((out["last"]["phase"], out["last"]["outcome"]), ("final", {"state": "timeout"}))
        self.assertEqual((out["render"]["phase"], out["render"]["outcome"]), ("final", {"state": "timeout"}))

    def test_watch_reports_a_device_that_did_not_restart(self):
        out = self.run_watch(r"""
          const plain = await runWatch(MARK(), {probe: ["authenticated"], live: [LIVE()]});
          const unsure = await runWatch(MARK({uncertain: true}), {probe: ["authenticated"], live: [LIVE()]});
          const blind = await runWatch(MARK(), {probe: ["authenticated"], live: [LIVE({boot: null})]});
          out = {plain: [plain.outcome, plain.elapsed], unsure: unsure.outcome, blind: [blind.outcome, blind.elapsed],
                 reload: [plain, unsure, blind].some(run => run.calls.includes("reload"))};""")
        self.assertEqual(out["plain"][0], {"state": "not_restarted", "uncertain": False})
        self.assertGreaterEqual(out["plain"][1], 20000)
        self.assertLessEqual(out["plain"][1], 22000)
        self.assertEqual(out["unsure"], {"state": "not_restarted", "uncertain": True})
        self.assertEqual(out["blind"][0], {"state": "timeout"})
        self.assertGreaterEqual(out["blind"][1], 90000)
        self.assertFalse(out["reload"])

    def test_same_boot_id_is_no_restart(self):
        out = self.run_watch(r"""
          const run = await runWatch(MARK(), {probe: ["authenticated"], live: [LIVE({uptime: 2})]});
          out = {outcome: run.outcome, elapsed: run.elapsed, phases: [...new Set(run.saves.map(m => m.phase))],
                 result: run.calls.includes("result")};""")
        self.assertEqual(out["outcome"], {"state": "not_restarted", "uncertain": False})
        self.assertGreaterEqual(out["elapsed"], 20000)
        self.assertEqual(out["phases"], ["final"])
        self.assertFalse(out["result"])

    def test_watch_resumes_and_waits_for_the_own_result(self):
        out = self.run_watch(r"""
          const run = await runWatch(MARK({phase: "back"}), {
            live: t => LIVE({boot: "bbbb0002", access: t < 4000 ? "CONNECTING" : "ONLINE"}),
            result: [OWN("pending", {}), OWN("pending", {}), OWN("connected", {ssid: "NewNet"})]});
          const settling = run.renders.find(r => r.phase === "settling");
          out = {outcome: run.outcome, probes: run.calls.filter(c => c === "probe").length, settling,
                 order: run.calls, last: run.saves[run.saves.length - 1]};""")
        self.assertEqual(out["outcome"], {"state": "connected", "ssid": "NewNet"})
        self.assertEqual(out["probes"], 0)
        self.assertEqual(out["settling"]["outcome"], {"state": "connected", "ssid": "NewNet"})
        calls = out["order"]
        third = [i for i, c in enumerate(calls) if c == "result"][2]
        self.assertEqual(calls[third + 1:third + 3], ["save", "render"], "the result is shown at once")
        self.assertEqual((out["last"]["phase"], out["last"]["outcome"]), ("final", {"state": "connected", "ssid": "NewNet"}))

    def test_result_of_another_request_is_observed(self):
        out = self.run_watch(r"""
          const run = await runWatch(MARK({phase: "back"}), {live: [LIVE({boot: "bbbb0002"})],
            result: [OWN("connected", {ssid: "NewNet"}, "fedcba9876543210")]});
          out = {outcome: run.outcome, settling: run.renders.find(r => r.phase === "settling").outcome};""")
        observed = {"state": "observed", "reported": "connected", "ssid": "NewNet"}
        self.assertEqual(out["settling"], observed)
        self.assertEqual(out["outcome"], observed)

    def test_watch_goes_back_to_restarting_when_the_boot_did_not_change(self):
        out = self.run_watch(r"""
          const run = await runWatch(MARK({phase: "back"}), {live: [LIVE()], probe: ["unreachable"]});
          out = {requests: run.calls.filter(c => ["probe", "live", "result"].includes(c)).slice(0, 3),
                 phases: run.saves.map(m => m.phase).slice(0, 1)};""")
        self.assertEqual(out["requests"][:2], ["live", "probe"])
        self.assertEqual(out["phases"], ["restarting"])

    def test_watch_ends_on_time_without_live_data(self):
        out = self.run_watch(r"""
          const throwing = await runWatch(MARK({phase: "back"}), {live: [new Error("offline")], result: [new Error("offline")]});
          const empty = await runWatch(MARK({phase: "back"}), {live: [{}], result: [{}]});
          const slow = await runWatch(MARK({phase: "back"}), {live: [LIVE({boot: "bbbb0002"})], result: [OWN("pending", {})]},
                                      {latency: {live: 2900, result: 2900}});
          out = [throwing, empty, slow].map(run => [run.outcome, run.elapsed]);""")
        for outcome, elapsed in out:
            self.assertEqual(outcome, {"state": "open"})
            self.assertGreaterEqual(elapsed, 90000)
            self.assertLessEqual(elapsed, 90000 + 2000 + 2 * 3000)

    def test_http_step_follows_the_last_request_while_verifying(self):
        out = self.run_watch(r"""
          const failing = await runWatch(MARK({phase: "back"}), {live: [new Error("offline")], result: [new Error("offline")]});
          const lost = await runWatch(MARK({phase: "back"}), {
            live: t => t < 6000 ? LIVE({boot: "bbbb0002", access: "CONNECTING"}) : new Error("offline"),
            result: t => t < 6000 ? OWN("pending", {}) : new Error("offline")});
          const answered = await runWatch(MARK({phase: "back"}), {live: [new Error("offline")], result: [OWN("pending", {})]});
          const http = run => run.renders.filter(r => r.phase === "verifying").map(r => stepState(r, "http"));
          out = {failing: http(failing), lost: http(lost), lostOutcome: lost.outcome,
                 answered: http(answered).slice(1)};""")
        self.assertTrue(out["failing"])
        self.assertEqual(set(out["failing"]), {"wait"}, "no answer at all: HTTP is not reachable")
        self.assertIn("ok", out["lost"], "a live answer: reachable")
        self.assertLess(out["lost"].index("ok"), len(out["lost"]) - 1)
        self.assertEqual(out["lost"][-1], "wait", "the requests fail again: not reachable any more")
        self.assertEqual(set(out["answered"]), {"ok"}, "a result answer alone: reachable")
        self.assertEqual(out["lostOutcome"], {"state": "open"})

    def test_watch_keeps_observing_after_an_early_result(self):
        out = self.run_watch(r"""
          const early = (storage, ntrip) => runWatch(MARK({phase: "back"}), {
            live: t => LIVE({boot: "bbbb0002", storage: t < 6000 ? "loading" : storage, ntrip}),
            result: [OWN("connected", {ssid: "NewNet"})]});
          const ready = await early("ready"), error = await early("error"), slow = await early("ready", "connecting");
          const view = run => g.restartOutcomeView(run.outcome, run.marker, run.renders[run.renders.length - 1]);
          out = {ready: [ready.outcome, ready.elapsed],
                 shownEarly: ready.renders.some(r => r.outcome && r.outcome.state === "connected" && stepState(r, "storage") === "wait"),
                 lastStorage: stepState(ready.renders[ready.renders.length - 1], "storage"),
                 errorNotes: view(error).notes, slow: [slow.outcome, slow.elapsed, slow.renders[slow.renders.length - 1].settleTimedOut],
                 slowNotes: view(slow).notes, readyNotes: view(ready).notes};""")
        self.assertEqual(out["ready"][0], {"state": "connected", "ssid": "NewNet"})
        self.assertGreaterEqual(out["ready"][1], 6000)
        self.assertLess(out["ready"][1], 6000 + 2 * 2000)
        self.assertTrue(out["shownEarly"])
        self.assertEqual(out["lastStorage"], "ok")
        self.assertIn("<ui.guided_note_storage_error>", out["errorNotes"])
        self.assertEqual(out["slow"][0], {"state": "connected", "ssid": "NewNet"})
        self.assertGreaterEqual(out["slow"][1], 60000)
        self.assertLessEqual(out["slow"][1], 60000 + 2000 + 3000)
        self.assertIs(out["slow"][2], True)
        self.assertIn("NTRIP still waiting after 60 s – state: connecting.", out["slowNotes"])
        self.assertFalse(any("still waiting" in note for note in out["readyNotes"]))

    def test_reboot_watch_shows_no_completion_before_readiness(self):
        out = self.run_watch(r"""
          const marker = MARK({kind: "reboot", attempt: null, target_ssid: null});
          const run = await runWatch(marker, {probe: ["authenticated"],
            live: t => LIVE({boot: "bbbb0002", storage: t < 6000 ? "loading" : "ready"})});
          const views = run.renders.map(r => [stepState(r, "storage"), r.outcome, g.restartOutcomeView(r.outcome, run.marker, r).text]);
          out = {outcome: run.outcome, views, phases: [...new Set(run.saves.map(m => m.phase))]};""")
        self.assertEqual(out["outcome"], {"state": "done"})
        self.assertEqual(out["phases"], ["settling", "final"])
        before = [view for view in out["views"] if view[0] != "ok"]
        self.assertTrue(before, "the watch renders while the storage loads")
        for storage, outcome, text in before:
            self.assertNotEqual((outcome or {}).get("state"), "done")
            self.assertNotEqual(text, "<ui.guided_outcome_done>")
        self.assertEqual(out["views"][-1][2], "<ui.guided_outcome_done>")

    def test_watch_stops_when_its_token_is_stale(self):
        out = self.run_watch(r"""
          let release;
          const pending = new Promise(resolve => { release = resolve; });
          const holder = {};
          const running = runWatch(MARK({phase: "back"}), {live: () => pending, result: [OWN("connected", {ssid: "N"})]},
                                   {onCall: (name, ctl) => { holder.ctl = ctl; }});
          await new Promise(resolve => setImmediate(resolve));
          holder.ctl.stop();
          release(LIVE({boot: "bbbb0002"}));
          const run = await running;
          out = {outcome: run.outcome, calls: run.calls};""")
        self.assertIsNone(out["outcome"])
        after = out["calls"][out["calls"].index("live") + 1:]
        for name in ("probe", "live", "result", "save", "render", "reload"):
            self.assertNotIn(name, after)



# The DOM side of the restart watch under Node: the real fragments src/78, 15, 90, 91 and 82 (app.js is built from them,
# test_build pins that) run in one vm context with a small fake DOM, fake timers and a scripted fetch.
HOLD = r"""
const vm = require("vm"), fs = require("fs");
const fragment = name => fs.readFileSync("src/" + name, "utf8").replace(/^\/\/ SPDX[^\n]*\n/, "");
const core = fs.readFileSync("src/10-core.js", "utf8").split("\n")
  .filter(line => /^(const tr=|function localText\(|const text=|const raw=|function h\(|const replace=)/.test(line))
  .concat(fs.readFileSync("src/45-capacity.js", "utf8").split("\n").filter(line => line.startsWith("function capacityText(")));
const later = () => new Promise(resolve => setImmediate(resolve));
function makeHold(options = {}) {
  const clock = {now: 1000000, seq: 0, timers: new Map()}, log = {fetch: [], heldAtFetch: [], replace: [], reload: [], modal: [], mounts: 0,
    counts: {startPolling: 0, renderRoute: 0, releaseDirtyGuard: 0, signInRedirect: 0, watchRestart: 0}, order: []};
  class Node {}
  class Text extends Node { constructor(value) { super(); this.data = String(value); this.parentNode = null; } get textContent() { return this.data; } }
  const camel = name => name.replace(/-([a-z])/g, (_, c) => c.toUpperCase());
  const attrOf = (el, name) => name === "class" ? el.className : name === "id" ? el.id || null
    : name.startsWith("data-") ? (el.dataset[camel(name.slice(5))] ?? null) : (el.attrs[name] ?? null);
  const matchOne = (el, selector) => {
    const parts = selector.match(/^([a-z0-9-]*)((?:[.#][\w-]+|\[[^\]]+\])*)$/i);
    if (!parts) throw new Error("selector " + selector);
    if (parts[1] && el.tagName !== parts[1].toUpperCase()) return false;
    for (const token of parts[2].match(/[.#][\w-]+|\[[^\]]+\]/g) || []) {
      if (token[0] === ".") { if (!el.className.split(/\s+/).includes(token.slice(1))) return false; }
      else if (token[0] === "#") { if (el.id !== token.slice(1)) return false; }
      else { const [, name, value] = token.match(/^\[([\w-]+)(?:="([^"]*)")?\]$/);
        const actual = attrOf(el, name); if (actual === null || (value !== undefined && String(actual) !== value)) return false; }
    }
    return true;
  };
  class El extends Node {
    constructor(tag) { super(); Object.assign(this, {tagName: tag.toUpperCase(), children: [], attrs: {}, dataset: {}, listeners: {},
      parentNode: null, style: {}, hidden: false, disabled: false, className: "", id: "", offsetHeight: 0, value: "", type: ""}); }
    append(...items) { for (const item of items.flat()) { const node = item instanceof Node ? item : new Text(item); if (node.parentNode) node.remove(); node.parentNode = this; this.children.push(node); } }
    prepend(...items) { const rest = this.children.splice(0); this.append(...items); for (const node of rest) this.children.push(node); }
    remove() { if (this.parentNode) { const list = this.parentNode.children; list.splice(list.indexOf(this), 1); this.parentNode = null; } }
    replaceChildren(...items) { for (const node of this.children) node.parentNode = null; this.children = []; this.append(...items); }
    get textContent() { return this.children.map(node => node.textContent).join(""); }
    set textContent(value) { this.replaceChildren(new Text(value == null ? "" : value)); }
    setAttribute(key, value) { if (key.startsWith("data-")) this.dataset[camel(key.slice(5))] = String(value); else if (key === "class") this.className = String(value); else if (key === "id") this.id = String(value); else this.attrs[key] = String(value); }
    getAttribute(key) { return attrOf(this, key); }
    removeAttribute(key) { if (key.startsWith("data-")) delete this.dataset[camel(key.slice(5))]; else delete this.attrs[key]; }
    addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
    removeEventListener() {}
    click() { for (const fn of this.listeners.click || []) fn({currentTarget: this, target: this, preventDefault() {}}); }
    focus() {} scrollIntoView() {}
    matches(selector) { return selector.split(",").some(part => matchOne(this, part.trim())); }
    closest(selector) { for (let node = this; node instanceof El; node = node.parentNode) if (node.matches(selector)) return node; return null; }
    querySelectorAll(selector) { const found = []; const walk = node => { for (const child of node.children) if (child instanceof El) { if (child.matches(selector)) found.push(child); walk(child); } }; walk(this); return found; }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    get isConnected() { let node = this; while (node.parentNode) node = node.parentNode; return node === body; }
  }
  const body = new El("body"), main = new El("main"), overlay = new El("div"), elsewhere = new El("div");
  body.append(main, overlay, elsewhere);
  const memory = {}, sessionStorage = {getItem: key => key in memory ? memory[key] : null, setItem: (key, value) => { memory[key] = String(value); }, removeItem: key => { delete memory[key]; }};
  const timer = every => (fn, ms = 0, ...args) => { const id = ++clock.seq; clock.timers.set(id, {at: clock.now + Math.max(0, Number(ms) || 0), fn: () => fn(...args), every: every ? Math.max(1, Number(ms) || 1) : null}); return id; };
  const clear = id => { clock.timers.delete(id); };
  // The scripted device: answer(path, init) returns {status, body} or a promise of it; a thrown error is a transport failure.
  const respond = ({status = 200, body = {}} = {}) => ({status, ok: status >= 200 && status < 300, body: null,
    json: async () => typeof body === "string" ? JSON.parse(body) : body, text: async () => typeof body === "string" ? body : JSON.stringify(body),
    blob: async () => null, arrayBuffer: async () => null});
  const fetch = (path, init = {}) => {
    log.fetch.push(String(path).split("?")[0]);
    log.heldAtFetch.push(typeof context.restartHoldActive === "function" ? context.restartHoldActive() : null);
    return new Promise((resolve, reject) => {
      const signal = init.signal;
      if (signal) { if (signal.aborted) return reject(new DOMException("aborted", "AbortError")); signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), {once: true}); }
      Promise.resolve().then(() => (options.answer || (() => ({})))(String(path), init)).then(answer => resolve(respond(answer)), reject);
    });
  };
  const location = {hash: options.hash || "#device/configuration", replace: url => { log.replace.push(url); log.order.push("replace"); },
    reload: () => { log.reload.push(main.querySelectorAll(".configuration-layout").map(form => form.dataset.dirty)); log.order.push("reload"); }};
  // The router sees the hash as it is; a device path is the configuration view, everything else Measure.
  const route = () => { const path = (location.hash || "#measure").slice(1) || "measure", root = path.split("/")[0];
    return {path, root, view: root === "device" ? "connections" : root === "projects" ? "projects" : "measure", params: {}}; };
  const stubs = {console, AbortController, DOMException, Response, crypto: globalThis.crypto, setImmediate,
    setTimeout: timer(false), setInterval: timer(true), clearTimeout: clear, clearInterval: clear, sessionStorage, location, fetch,
    navigator: {onLine: true}, scrollTo() {}, Node, main, overlay,
    document: {hidden: false, createElement: tag => new El(tag), createTextNode: value => new Text(value), documentElement: {style: {setProperty() {}, removeProperty() {}}}},
    currentRoute: route, canonicalizeRoute: route, viewFallback: () => null,
    setHeader() {}, livePollIntervalMs: () => 2000, updateGlobalStatus() {}, updateStatus() {}, refreshStatusDetails: async () => {},
    mountStatus() {}, mountProjects: async () => { log.mounts += 1; }, mountDevice: async () => { log.mounts += 1; },
    mountConnections: async () => { log.mounts += 1; }, setConnection() {},
    observeCapacity() {}, archiveLibrary() {}, skeleton: () => new El("div"),
    notice: message => { const el = new El("div"); el.className = "notice"; el.textContent = message; return el; },
    iconButton: (label, handler) => { const el = new El("button"); el.className = "btn"; el.textContent = label; el.addEventListener("click", handler); return el; },
    modal: async args => { log.modal.push(args); return options.modalAnswer === undefined ? {} : options.modalAnswer; },
    applyStagedChanges: async () => null};
  const context = vm.createContext(stubs);
  vm.runInContext("Date.now = () => __clock.now;", Object.assign(context, {__clock: clock}));
  vm.runInContext(core.join("\n"), context);
  vm.runInContext("let activeController=null,pollTimer=0,pollBusy=false,connectionState=null,measurementRequestActive=false,statusRefs=null,statusDetailsUpdatedAt=0,pendingConfigEdit=null;", context);
  for (const name of ["78-guided-apply.js", "15-store-api.js", "90-live-router.js", "91-live-stream.js", "82-restart-panel.js"]) vm.runInContext(fragment(name), context, {filename: name});
  // Count calls of the shared functions; the fragments call them through the global object.
  vm.runInContext(`for (const name of ["startPolling", "renderRoute", "releaseDirtyGuard", "signInRedirect", "watchRestart"]) {
    const real = globalThis[name]; globalThis[name] = function (...args) { __log.counts[name] += 1; if (name === "releaseDirtyGuard" || name === "signInRedirect") __log.order.push(name); return real.apply(this, args); }; }`,
    Object.assign(context, {__log: log}));
  const run = code => vm.runInContext(code, context);
  async function advance(ms, step = 250) {
    const end = clock.now + ms;
    for (;;) {
      await later(); await later();
      let next = null;
      for (const [id, item] of clock.timers) if (item.at <= end && (!next || item.at < next[1].at)) next = [id, item];
      if (!next) break;
      const [id, item] = next; clock.now = Math.max(clock.now, item.at);
      if (item.every) item.at += item.every; else clock.timers.delete(id);
      item.fn();
    }
    clock.now = end; await later(); await later();
  }
  return {context, run, log, clock, memory, main, overlay, elsewhere, El, advance, later};
}
const LIVE_A = {system: {boot_id: "aaaa0001"}, network: {access_state: "ONLINE", ntrip_state: "streaming", ntrip_config: "ready"}, tracking_startup: {state: "ready"}};
const LIVE_B = {system: {boot_id: "bbbb0002"}, network: {access_state: "ONLINE", ntrip_state: "streaming", ntrip_config: "ready"}, tracking_startup: {state: "ready"}};
const devicePaths = (marker, extra = {}) => (path, init) => {
  if (extra[path]) return extra[path](init);
  if (path.startsWith("/api/ui/live")) return {body: LIVE_A};
  if (path === "/api/access") return {body: {hostname: "rtk-test.local"}};
  if (path === "/api/session") return {status: 200, body: {authenticated: true, csrf_token: "c"}};
  return {body: {}};
};
"""


class TestRestartHold(unittest.TestCase):
    """#23c Task 6: the hold around a restart request, the one watcher and its end, under Node."""

    def run_hold(self, script):
        if NODE is None:
            self.skipTest("node is unavailable")
        body = MARKER + HOLD + "let out;\n(async () => {\n" + script + "\n})().then(() => console.log(JSON.stringify(out)), " \
            "error => { console.error(error); process.exit(1); });"
        run = subprocess.run([NODE, "-e", body], cwd=ROOT, capture_output=True, text=True, timeout=60)
        if run.returncode != 0:
            raise AssertionError(run.stderr)
        return json.loads(run.stdout)

    def test_hold_keeps_the_session_lost_error(self):
        out = self.run_hold(r"""
          const t = makeHold({answer: () => ({status: 401, body: {authenticated: false}})});
          const shape = error => ({name: error.name, status: error.status, message: error.message});
          t.run("beginRestartHold()");
          const plain = await t.run('api.json("/api/config-result")').then(() => null, shape);
          const external = new AbortController();
          const signalled = await t.run('(signal => api.json("/api/config-result", {signal, timeout: 3000}))')(external.signal).then(() => null, shape);
          const held = [t.run("restartHoldActive()"), t.log.replace.length];
          t.run("endRestartHold()");
          const after = await t.run('api.json("/api/config-result")').then(() => null, shape);
          out = {plain, signalled, held, after, replace: t.log.replace, redirects: t.log.counts.signInRedirect};""")
        lost = {"name": "SessionLostError", "status": 401, "message": "Session lost during restart"}
        self.assertEqual(out["plain"], lost)
        self.assertEqual(out["signalled"], lost)
        self.assertEqual(out["held"], [True, 0])
        # Without the hold the branch is unchanged: it aborts before it throws, so the catch reports an AbortError.
        self.assertEqual(out["after"]["name"], "AbortError")
        self.assertEqual(out["redirects"], 1)
        self.assertEqual(out["replace"], ["/?return=" + "%2Fui-v2%23device%2Fconfiguration"])

    def test_hold_blocks_polling_and_route_requests(self):
        out = self.run_hold(r"""
          const t = makeHold({answer: () => ({body: LIVE_A})});
          t.run("beginRestartHold()");
          const timersBefore = t.clock.timers.size;
          await t.run("pollLive()"); t.run("startPolling()"); await t.run("renderRoute()");
          const held = {fetch: t.log.fetch.length, timers: t.clock.timers.size - timersBefore, mounts: t.log.mounts,
                        placeholder: t.main.querySelectorAll(".restart-hold").length, blocked: t.run("restartWatchState.renderBlocked")};
          const free = [];
          for (const phase of [null, "verifying", "settling", "final"]) {
            const u = makeHold({answer: () => ({body: LIVE_A})});
            if (phase) u.run(`restartWatchState = {token: {}, phase: "${phase}", hold: false, renderBlocked: false, controller: new AbortController(), promise: null}`);
            const before = u.clock.timers.size;
            await u.run("pollLive()"); u.run("startPolling()"); await u.run("renderRoute()");
            free.push({fetch: u.log.fetch.length > 0, interval: u.clock.timers.size > before, mounts: u.log.mounts,
                       placeholder: u.main.querySelectorAll(".restart-hold").length});
          }
          out = {held, free};""")
        self.assertEqual(out["held"], {"fetch": 0, "timers": 0, "mounts": 0, "placeholder": 1, "blocked": True})
        for state in out["free"]:
            self.assertEqual(state, {"fetch": True, "interval": True, "mounts": 1, "placeholder": 0})

    def test_begin_hold_aborts_running_requests(self):
        out = self.run_hold(r"""
          const t = makeHold({answer: () => new Promise(() => {})});
          t.run("activeController = new AbortController()");
          const old = t.run("activeController"), serial = t.run("requestSerial");
          const running = t.run('api.json("/api/log", {signal: activeController.signal})').then(() => null, error => error.name);
          await t.later();
          t.run("beginRestartHold()");
          out = {aborted: old.signal.aborted, serial: t.run("requestSerial") - serial, error: await running, held: t.run("restartHoldActive()")};""")
        self.assertEqual(out, {"aborted": True, "serial": 1, "error": "AbortError", "held": True})

    def test_hold_begins_before_the_write(self):
        out = self.run_hold(r"""
          const t = makeHold({answer: devicePaths()});
          const seen = [];
          let refuse;
          const send = id => { seen.push(t.run("restartHoldActive()"), id); return new Promise((_, reject) => { refuse = reject; }); };
          const asked = t.run("requestRestart")("apply", send, {attempt: {keys: ["wifi_ssid"], wifi_ssid: "N"}});
          for (let i = 0; i < 20 && !refuse; i++) await t.later();
          const marker = JSON.parse(t.memory["rtk-restart-watch"]);
          const liveBefore = t.log.fetch.filter(path => path === "/api/ui/live").length;
          await t.run("pollLive()");
          const liveDuring = t.log.fetch.filter(path => path === "/api/ui/live").length - liveBefore;
          const counts = Object.assign({}, t.log.counts);
          refuse(Object.assign(new Error("An earlier change is still open."), {status: 409, code: "open_change_pending"}));
          const answer = await asked;
          out = {seen, marker, liveDuring, state: answer.state, failure: answer.failure && answer.failure.code,
                 held: t.run("restartHoldActive()"), startPolling: t.log.counts.startPolling - counts.startPolling,
                 renderRoute: t.log.counts.renderRoute - counts.renderRoute, stored: t.memory["rtk-restart-watch"] || null,
                 panel: t.overlay.querySelectorAll(".restart-watch").length};""")
        self.assertIs(out["seen"][0], True)
        self.assertRegex(out["seen"][1], r"^[0-9a-f]{16}$")
        self.assertEqual(out["marker"]["phase"], "requested")
        self.assertEqual(out["marker"]["apply_id"], out["seen"][1])
        self.assertEqual(out["marker"]["boot_id"], "aaaa0001")
        self.assertEqual(out["marker"]["host"], "rtk-test.local")
        self.assertEqual(out["marker"]["attempt"], {"keys": ["wifi_ssid"], "wifi_ssid": "N"})
        self.assertEqual(out["liveDuring"], 0)
        self.assertEqual((out["state"], out["failure"]), ("refused", "open_change_pending"))
        self.assertIs(out["held"], False)
        self.assertEqual((out["startPolling"], out["renderRoute"]), (1, 0))
        self.assertIsNone(out["stored"])
        self.assertEqual(out["panel"], 0)

    def test_no_write_without_a_boot_id(self):
        out = self.run_hold(r"""
          const cases = [() => ({body: {system: {}}}), () => { throw new TypeError("Failed to fetch"); }, () => ({status: 500, body: {error: "internal"}})];
          out = [];
          for (const live of cases) {
            const t = makeHold({answer: devicePaths(null, {"/api/ui/live": live})});
            let sent = 0;
            const answer = await t.run("requestRestart")("apply", () => { sent += 1; return {applying: true}; }, {});
            out.push({state: answer.state, sent, stored: t.memory["rtk-restart-watch"] || null, held: t.run("restartHoldActive()"),
                      state2: t.run("restartWatchState")});
          }""")
        for case in out:
            self.assertEqual(case, {"state": "not_sent", "sent": 0, "stored": None, "held": False, "state2": None})

    def test_end_hold_rerenders_only_a_blocked_route(self):
        out = self.run_hold(r"""
          const t = makeHold({answer: () => ({body: LIVE_A})});
          t.run("beginRestartHold()"); await t.run("renderRoute()");
          const c0 = Object.assign({}, t.log.counts);
          t.run("endRestartHold()");
          const blocked = [t.log.counts.startPolling - c0.startPolling, t.log.counts.renderRoute - c0.renderRoute];
          const c1 = Object.assign({}, t.log.counts);
          t.run("endRestartHold()");
          const again = [t.log.counts.startPolling - c1.startPolling, t.log.counts.renderRoute - c1.renderRoute];
          t.run("restartWatchState = null; beginRestartHold()");
          const c2 = Object.assign({}, t.log.counts);
          t.run("endRestartHold()");
          const free = [t.log.counts.startPolling - c2.startPolling, t.log.counts.renderRoute - c2.renderRoute];
          out = {blocked, again, free};""")
        self.assertEqual(out, {"blocked": [1, 1], "again": [0, 0], "free": [1, 0]})

    def test_resume_requested_marker_checks_the_boot_first(self):
        out = self.run_hold(r"""
          out = {};
          for (const kind of ["apply", "restore", "reboot"]) {
            const t = makeHold({answer: (path) => path === "/api/ui/live" ? {body: LIVE_B}
              : path === "/api/config-result" ? {body: {status: "connected", details: {ssid: "NewNet"}, apply_id: "0123456789abcdef", boot_id: "bbbb0002"}}
              : path === "/api/session" ? {status: 200, body: {authenticated: true}} : {body: {}}});
            t.memory["rtk-restart-watch"] = JSON.stringify(MARK({kind, phase: "requested", started_ms: t.clock.now, boot_id: "aaaa0001"}));
            t.run("resumeRestartWatch()");
            const sync = {held: t.run("restartHoldActive()"), heldAtFetch: t.log.heldAtFetch.slice(), first: t.log.fetch.slice(),
                          panel: t.overlay.querySelectorAll(".restart-watch").length};
            let outcome;
            for (let i = 0; i < 400 && outcome === undefined; i++) {
              await t.advance(500);
              const promise = t.run("restartWatchState && restartWatchState.promise");
              if (promise) promise.then(value => { outcome = value; });
            }
            const requests = t.log.fetch.filter(path => path !== "/api/config");
            out[kind] = {sync, outcome, first: requests[0], probes: requests.filter(path => path === "/api/session").length,
                         results: requests.filter(path => path === "/api/config-result").length,
                         stored: JSON.parse(t.memory["rtk-restart-watch"] || "null")};
          }""")
        for kind in ("apply", "restore", "reboot"):
            case = out[kind]
            # The boot check starts synchronously, but only after the hold is in place.
            self.assertEqual(case["sync"], {"held": True, "heldAtFetch": [True], "first": ["/api/ui/live"], "panel": 1}, kind)
            self.assertEqual(case["first"], "/api/ui/live", kind)
            self.assertEqual(case["probes"], 0, kind)
            self.assertEqual(case["stored"]["phase"], "final", kind)
        self.assertEqual(out["apply"]["outcome"], {"state": "connected", "ssid": "NewNet"})
        self.assertEqual(out["restore"]["outcome"], {"state": "connected", "ssid": "NewNet"})
        self.assertGreater(out["apply"]["results"], 0)
        self.assertEqual(out["reboot"]["outcome"], {"state": "restart_observed"})
        self.assertEqual(out["reboot"]["results"], 0)

    def test_resumed_final_outcome_shows_no_step_list(self):
        out = self.run_hold(r"""
          out = {};
          for (const [kind, outcome] of [["apply", {state: "connected", ssid: "NewNet"}], ["reboot", {state: "done"}], ["apply", {state: "timeout"}]]) {
            const t = makeHold({answer: devicePaths()});
            t.memory["rtk-restart-watch"] = JSON.stringify(MARK({kind, phase: "final", outcome, started_ms: t.clock.now}));
            t.run("resumeRestartWatch()");
            await t.advance(5000);
            const node = t.overlay.querySelector("section.restart-watch"), list = node.querySelector(".restart-steps");
            out[outcome.state] = {outcome: node.dataset.outcome, items: list.querySelectorAll("li").length, hidden: list.hidden,
                                  text: node.textContent, result: node.querySelector(".restart-result").textContent};
          }""")
        for state in ("connected", "done", "timeout"):
            case = out[state]
            self.assertEqual(case["outcome"], state)
            self.assertTrue(case["result"], state)
            self.assertEqual((case["items"], case["hidden"]), (0, True), state + ": no step list without step data")
            for stale in ("Not reachable yet", "Waiting for the device"):
                self.assertNotIn(stale, case["text"], state)

    def test_finish_ends_a_settling_watch(self):
        out = self.run_hold(r"""
          let lives = 0;
          const t = makeHold({answer: path => path === "/api/ui/live" ? (lives += 1, new Promise(() => {})) : {body: {}}});
          const marker = MARK({phase: "settling", result: {state: "connected", ssid: "NewNet"}, started_ms: t.clock.now});
          const first = t.run("startRestartWatch")("apply", marker);
          for (let i = 0; i < 20 && lives === 0; i++) await t.advance(500);
          const controller = t.run("restartWatchState.controller");
          t.memory["rtk-restart-watch"] = JSON.stringify(marker);
          const panelBefore = t.overlay.querySelectorAll(".restart-watch").length;
          t.run("finishRestartWatch()");
          const fetches = t.log.fetch.length;
          await t.advance(10000);
          const outcome = await first, extra = t.log.fetch.length - fetches, stored = t.memory["rtk-restart-watch"] || null;
          const second = t.run("startRestartWatch")("apply", MARK({phase: "settling", result: {state: "connected", ssid: "NewNet"}, started_ms: t.clock.now}));
          out = {lives, panelBefore, aborted: controller.signal.aborted, stored, panelAfter: null, outcome, extra, newPromise: second !== first};
          out.panelAfter = t.overlay.querySelectorAll(".restart-watch").length;""")
        self.assertEqual(out["lives"], 1)
        self.assertEqual(out["panelBefore"], 1)
        self.assertIs(out["aborted"], True)
        self.assertIsNone(out["stored"])
        self.assertIsNone(out["outcome"])
        self.assertEqual(out["extra"], 0)
        self.assertIs(out["newPromise"], True)
        self.assertEqual(out["panelAfter"], 1, "the new watch has its own panel")

    def test_finish_leaves_no_panel_and_no_requests(self):
        out = self.run_hold(r"""
          const t = makeHold({answer: path => path === "/api/ui/live" ? new Promise(() => {}) : {body: {}}});
          const marker = MARK({phase: "settling", result: {state: "connected", ssid: "NewNet"}, started_ms: t.clock.now});
          t.run("startRestartWatch")("apply", marker);
          await t.advance(3000);
          t.run("finishRestartWatch()");
          const fetches = t.log.fetch.length;
          await t.advance(15000);
          out = {state: t.run("restartWatchState"), panel: t.overlay.querySelectorAll(".restart-watch").length, extra: t.log.fetch.length - fetches,
                 stored: t.memory["rtk-restart-watch"] || null};""")
        self.assertEqual(out, {"state": None, "panel": 0, "extra": 0, "stored": None})

    def test_only_one_watcher_runs(self):
        out = self.run_hold(r"""
          const t = makeHold({answer: path => path === "/api/session" ? new Promise(() => {}) : {body: {}}});
          const marker = MARK({started_ms: t.clock.now});
          const first = t.run("startRestartWatch")("apply", marker), second = t.run("startRestartWatch")("apply", marker);
          await t.advance(1000);
          const controller = t.run("restartWatchState.controller"), runs = t.log.counts.watchRestart;
          const form = new t.El("form"); form.className = "configuration-layout"; t.main.append(form);
          const retry = t.run("retryApply")(null);
          const ended = {aborted: controller.signal.aborted, state: t.run("restartWatchState")};
          await t.advance(1000); await retry;
          out = {same: first === second, runs, ended, panels: t.overlay.querySelectorAll(".restart-watch").length};""")
        self.assertIs(out["same"], True)
        self.assertEqual(out["runs"], 1)
        self.assertEqual(out["ended"], {"aborted": True, "state": None})
        self.assertEqual(out["panels"], 0)

    def test_send_answers_are_decoded_json(self):
        out = self.run_hold(r"""
          const cases = {
            restore_ok: ["restore", id => ({restored: true, schema_version: 2, sections: ["connections", 5], kept: ["wifi"], apply_id: id})],
            restore_response: ["restore", () => new Response('{"restored": true}', {status: 202})],
            restore_syntax: ["restore", () => Promise.reject(new SyntaxError("Unexpected end of JSON input"))],
            restore_invalid: ["restore", () => Promise.reject(Object.assign(new Error("The backup is invalid: x"), {status: 400, code: "invalid_backup"}))],
            apply_ok: ["apply", id => ({applying: true, apply_id: id})],
            apply_false: ["apply", () => ({applying: false})],
            reboot_ok: ["reboot", id => ({restarting: true, apply_id: id})],
            reboot_abort: ["reboot", () => Promise.reject({name: "AbortError"})]};
          out = {};
          for (const [name, [kind, send]] of Object.entries(cases)) {
            const t = makeHold({answer: devicePaths()});
            const answer = await t.run("requestRestart")(kind, send, {});
            const stored = JSON.parse(t.memory["rtk-restart-watch"] || "null");
            out[name] = {state: answer.state, held: t.run("restartHoldActive()"),
                         marker: answer.marker && {uncertain: Boolean(answer.marker.uncertain), sections: answer.marker.sections ?? null, kept: answer.marker.kept ?? null, phase: answer.marker.phase},
                         stored: stored && {phase: stored.phase, sections: stored.sections ?? null, kept: stored.kept ?? null, uncertain: Boolean(stored.uncertain)}};
          }""")
        accepted = lambda sections=None, kept=None: {"uncertain": False, "sections": sections, "kept": kept, "phase": "restarting"}
        self.assertEqual(out["restore_ok"]["state"], "accepted")
        self.assertEqual(out["restore_ok"]["marker"], accepted(["connections"], ["wifi"]))
        self.assertEqual(out["restore_ok"]["stored"], {"phase": "restarting", "sections": ["connections"], "kept": ["wifi"], "uncertain": False})
        self.assertIs(out["restore_ok"]["held"], True)
        for name in ("restore_response", "restore_syntax", "apply_false", "reboot_abort"):
            self.assertEqual(out[name]["state"], "uncertain", name)
            self.assertEqual(out[name]["marker"], {"uncertain": True, "sections": None, "kept": None, "phase": "restarting"}, name)
            self.assertEqual(out[name]["stored"], {"phase": "restarting", "sections": None, "kept": None, "uncertain": True}, name)
            self.assertIs(out[name]["held"], True, name)
        self.assertEqual(out["restore_invalid"], {"state": "refused", "held": False, "marker": None, "stored": None})
        for name in ("apply_ok", "reboot_ok"):
            self.assertEqual(out[name]["state"], "accepted", name)
            self.assertEqual(out[name]["marker"], accepted(), name)

    def test_watch_reload_releases_the_dirty_guard(self):
        out = self.run_hold(r"""
          const t = makeHold({answer: path => path === "/api/session" ? {status: 401, body: {authenticated: false}} : {body: {}}});
          const dirtyForm = () => { const form = new t.El("form"); form.className = "configuration-layout"; form.dataset.dirty = "true"; return form; };
          t.main.append(dirtyForm(), dirtyForm());
          const outside = dirtyForm(); t.elsewhere.append(outside);
          t.run("startRestartWatch")("apply", MARK({started_ms: t.clock.now}));
          for (let i = 0; i < 40 && !t.log.reload.length; i++) await t.advance(500);
          const reload = {seen: t.log.reload, order: t.log.order.slice(), outside: outside.dataset.dirty};
          const refusals = {};
          for (const [name, live, failure] of [["refused", LIVE_A, {status: 409, code: "open_change_pending"}], ["not_sent", {system: {}}, null],
                                                ["signed_out", LIVE_A, {status: 401, name: "SessionLostError"}]]) {
            const u = makeHold({answer: devicePaths(null, {"/api/ui/live": () => ({body: live})})});
            u.main.append(dirtyForm());
            await u.run("requestRestart")("apply", () => Promise.reject(Object.assign(new Error("x"), failure || {})), {});
            refusals[name] = {release: u.log.counts.releaseDirtyGuard, order: u.log.order.slice(), replace: u.log.replace.length};
          }
          out = {reload, refusals};""")
        self.assertEqual(out["reload"]["seen"], [["false", "false"]])
        self.assertEqual(out["reload"]["order"], ["releaseDirtyGuard", "reload"])
        self.assertEqual(out["reload"]["outside"], "true")
        self.assertEqual(out["refusals"]["refused"], {"release": 0, "order": [], "replace": 0})
        self.assertEqual(out["refusals"]["not_sent"], {"release": 0, "order": [], "replace": 0})
        self.assertEqual(out["refusals"]["signed_out"], {"release": 1, "order": ["releaseDirtyGuard", "signInRedirect", "replace"], "replace": 1})


if __name__ == "__main__":
    unittest.main()
