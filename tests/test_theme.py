# SPDX-License-Identifier: AGPL-3.0-only
"""Theme tokens of app.css (M2 #29c): contrast per theme, dark/auto parity, no literal colors."""
import json
import os
import re
import subprocess
import unittest

import ui

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
THEMES = ("light", "dark", "high-contrast", "auto-dark")
HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")
VAR = re.compile(r"var\((--[a-z0-9-]+)\)")


def read_css() -> str:
    with open(os.path.join(ROOT, "app.css"), encoding="utf-8") as handle:
        return re.sub(r"/\*.*?\*/", "", handle.read(), flags=re.S)


def parse_rules(css: str, media: str = "") -> list:
    """Flat list of (media prelude, selector, body); nested at-rules keep their prelude."""
    rules, index = [], 0
    while index < len(css):
        brace = css.find("{", index)
        if brace < 0:
            break
        prelude = css[index:brace].strip()
        depth, end = 1, brace + 1
        while depth:
            depth += {"{": 1, "}": -1}.get(css[end], 0)
            end += 1
        body = css[brace + 1:end - 1]
        if prelude.startswith("@media") or prelude.startswith("@supports"):
            rules.extend(parse_rules(body, prelude))
        elif prelude.startswith("@"):
            pass  # @keyframes and the like carry no theme colors
        else:
            rules.append((media, prelude, body))
        index = end
    return rules


def declarations(body: str) -> dict:
    result = {}
    for item in re.split(r";(?![^(]*\))", body):
        if ":" in item:
            name, value = item.split(":", 1)
            result[name.strip()] = value.strip()
    return result


def normalize(value: str) -> str:
    value = value.strip().lower()
    short = re.fullmatch(r"#([0-9a-f])([0-9a-f])([0-9a-f])", value)
    if short:
        return "#" + "".join(ch * 2 for ch in short.groups())
    return value


def theme_tokens(css: str) -> dict:
    """Resolved declarations per theme: light, dark, high-contrast and auto-dark."""
    base, dark, contrast_theme, auto_dark = {}, {}, {}, {}
    for media, selector, body in parse_rules(css):
        if media == "" and selector == ":root":
            base.update(declarations(body))
        elif media == "" and selector == ':root[data-theme="dark"]':
            dark.update(declarations(body))
        elif media == "" and selector == ':root[data-theme="high-contrast"]':
            contrast_theme.update(declarations(body))
        elif media.replace(" ", "") == "@media(prefers-color-scheme:dark)" and selector == ":root:not([data-theme])":
            auto_dark.update(declarations(body))
    result = {}
    for name, layer in (("light", {}), ("dark", dark), ("high-contrast", contrast_theme), ("auto-dark", auto_dark)):
        merged = dict(base)
        merged.update(layer)
        for key, value in merged.items():
            for _ in range(5):
                resolved = VAR.sub(lambda m: merged.get(m.group(1), m.group(0)), value)
                if resolved == value:
                    break
                value = resolved
            merged[key] = normalize(value)
        result[name] = merged
    return result


def luminance(color: str) -> float:
    channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(fg: str, bg: str) -> float:
    high, low = sorted((luminance(fg), luminance(bg)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def build_pairs() -> list:
    pairs = [(fg, bg) for fg in ("--ink", "--muted", "--action", "--ok", "--warn", "--critical")
             for bg in ("--bg", "--surface", "--surface-2")]
    pairs += [("--ink", "--warn-soft"), ("--muted", "--warn-soft"), ("--warn", "--warn-soft"),
              ("--surface", "--ink"), ("--action-ink", "--action"), ("--critical-ink", "--critical"),
              ("--on-hero", "--hero"), ("--hero-muted", "--hero"), ("--hero-ok", "--hero"),
              ("--hero-warn", "--hero")]
    pairs += [(fg, "--console-bg") for fg in ("--console-ink", "--console-strong", "--console-muted",
                                              "--console-debug", "--console-error", "--console-warn")]
    return pairs


# Literal colors that stay in rules (E4): QR scanability, backdrop and one shadow.
ALLOWED_LITERALS = {
    (".qr-bg", "fill:#fff"),
    (".qr-modules", "fill:#000"),
    (".action-sheet::backdrop", "background:#111820b8"),
    (".mobile-nav", "box-shadow:0 -8px 24px #11182012"),
}


def rule_block(css: str, selector: str) -> str:
    return "\n".join(body for _, sel, body in parse_rules(css) if sel == selector)


class ThemeTokenTest(unittest.TestCase):
    def test_theme_tokens_meet_contrast_in_every_theme(self):
        tokens = theme_tokens(read_css())
        failures = []
        for theme in THEMES:
            for fg, bg in build_pairs():
                if fg not in tokens[theme] or bg not in tokens[theme]:
                    failures.append((theme, fg, bg, "token missing"))
                    continue
                ratio = contrast(tokens[theme][fg], tokens[theme][bg])
                if ratio < 4.5:
                    failures.append((theme, tokens[theme][fg], tokens[theme][bg], round(ratio, 2)))
        self.assertEqual(failures, [])

    def test_dark_theme_defines_every_color_token(self):
        css = read_css()
        rules = parse_rules(css)
        base, dark = {}, {}
        for media, selector, body in rules:
            if media == "" and selector == ":root":
                base.update(declarations(body))
            elif media == "" and selector == ':root[data-theme="dark"]':
                dark.update(declarations(body))
        missing = [key for key, value in base.items()
                   if key.startswith("--") and HEX.search(value) and key not in dark]
        self.assertEqual(missing, [])
        self.assertEqual(dark.get("color-scheme"), "dark")
        self.assertEqual(base.get("color-scheme"), "light")

    def test_auto_dark_matches_the_dark_theme(self):
        rules = parse_rules(read_css())
        dark, auto = {}, {}
        for media, selector, body in rules:
            if media == "" and selector == ':root[data-theme="dark"]':
                dark.update(declarations(body))
            elif media.replace(" ", "") == "@media(prefers-color-scheme:dark)" and selector == ":root:not([data-theme])":
                auto.update(declarations(body))
        self.assertTrue(dark, "dark theme block exists")
        self.assertEqual(auto, dark)

    def test_colors_come_from_tokens(self):
        found = set()
        for _, selector, body in parse_rules(read_css()):
            props = declarations(body)
            if selector.startswith(":root") and all(k.startswith("--") or k == "color-scheme" for k in props):
                continue
            for name, value in props.items():
                if HEX.search(value):
                    found.add((selector, "%s:%s" % (name, value)))
        self.assertEqual(sorted(found - ALLOWED_LITERALS), [])

    def test_consoles_follow_the_theme(self):
        css = read_css()
        for selector in (".log-console", ".module-console"):
            block = rule_block(css, selector)
            self.assertIn("background:var(--console-bg)", block.replace(" ", ""), selector)
            self.assertIn("color:var(--console-ink)", block.replace(" ", ""), selector)
        self.assertIn("color:var(--console-error)", rule_block(css, ".log-entry.error"))


NODE = next((name for name in ("node", "nodejs")
             if subprocess.run(["sh", "-c", "command -v %s" % name], capture_output=True).returncode == 0), None)

THEME_NODE_PRELUDE = r"""
"use strict";
const mode = process.argv[1], stored = JSON.parse(process.argv[2]);
const writes = [], events = [], listeners = {};
const failures = [];
const eq = (name, actual, expected) => {
  if (JSON.stringify(actual) !== JSON.stringify(expected))
    failures.push(name + ": got " + JSON.stringify(actual) + " expected " + JSON.stringify(expected));
};
const store = new Map(stored === null ? [] : [["rtk-theme", stored]]);
globalThis.localStorage = {
  getItem(key) { if (mode === "locked") throw new Error("locked"); return store.has(key) ? store.get(key) : null; },
  setItem(key, value) {
    writes.push([key, value]);
    if (mode === "locked" || mode === "write-fails") throw new Error("write blocked");
    store.set(key, value);
  },
};
globalThis.CustomEvent = class { constructor(type, init) { this.type = type; this.detail = init && init.detail; } };
globalThis.addEventListener = (type, handler) => { listeners[type] = handler; };
const attrs = new Map();
const element = initial => {
  const own = new Map(Object.entries(initial || {}));
  return {setAttribute: (k, v) => own.set(k, String(v)), getAttribute: k => own.has(k) ? own.get(k) : null,
          removeAttribute: k => own.delete(k), addEventListener: () => {}};
};
const metas = [
  element({name: "theme-color", media: "(prefers-color-scheme: light)", "data-theme-surface": "surface", content: "#111820"}),
  element({name: "theme-color", media: "(prefers-color-scheme: dark)", "data-theme-surface": "surface", content: "#111820"}),
  element({name: "theme-color", media: "(prefers-color-scheme: light)", "data-theme-surface": "hero", content: "#111820"}),
  element({name: "theme-color", media: "(prefers-color-scheme: dark)", "data-theme-surface": "hero", content: "#111820"}),
];
const toggles = [element({"data-theme-toggle": ""})];
globalThis.document = {
  readyState: "complete",
  documentElement: {lang: "en", setAttribute: (k, v) => attrs.set(k, String(v)), removeAttribute: k => attrs.delete(k),
                    getAttribute: k => attrs.has(k) ? attrs.get(k) : null},
  body: {dataset: {page: "app-v2"}},
  querySelector: () => null,
  querySelectorAll: sel => sel === 'meta[name="theme-color"]' ? metas : sel === "[data-theme-toggle]" ? toggles : [],
  dispatchEvent: event => { events.push([event.type, event.detail]); return true; },
  addEventListener: () => {},
};
const I = require(process.cwd() + "/i18n.js");
const themeEvents = () => events.filter(item => item[0] === "rtk-theme-change");
const attr = () => attrs.has("data-theme") ? attrs.get("data-theme") : null;
const colors = () => metas.map(meta => meta.getAttribute("content"));
const clearLogs = () => { writes.length = 0; events.length = 0; };
const out = {};
"""


def run_theme_node(stored, body, storage="ok"):
    """Evaluate i18n.js in Node with stubbed storage and document; the body fills `out` and calls eq()."""
    script = THEME_NODE_PRELUDE + body + "\nout.failures = failures; out.writes = writes; out.events = events;\n" \
        "console.log(JSON.stringify(out));\n"
    run = subprocess.run([NODE, "-e", script, storage, json.dumps(stored)], cwd=ROOT,
                         capture_output=True, text=True, timeout=20)
    if run.returncode != 0:
        raise AssertionError(run.stderr)
    return json.loads(run.stdout)


@unittest.skipUnless(NODE, "node is not installed")
class ThemeStateTest(unittest.TestCase):
    def test_stored_theme_values_are_normalised(self):
        cases = ((None, "ok", "auto"), ("default", "ok", "auto"), ("neon", "ok", "auto"),
                 (None, "locked", "auto"), ("light", "ok", "light"), ("dark", "ok", "dark"),
                 ("high-contrast", "ok", "high-contrast"))
        for stored, storage, expected in cases:
            with self.subTest(stored=stored, storage=storage):
                result = run_theme_node(stored, "out.theme = I.getTheme(); out.attr = attr();", storage)
                self.assertEqual(result["failures"], [])
                self.assertEqual(result["theme"], expected)
                self.assertEqual(result["attr"], None if expected == "auto" else expected)
                self.assertEqual(result["writes"], [], "loading never writes")

    def test_set_theme_validates_persists_and_announces(self):
        result = run_theme_node(None, """
eq("themes", I.themes, ["auto", "light", "dark", "high-contrast"]);
out.frozen = Object.isFrozen(I.themes);
eq("dark returns", I.setTheme("dark"), true);
eq("theme", I.getTheme(), "dark");
eq("stored", store.get("rtk-theme"), "dark");
eq("attribute", attr(), "dark");
eq("events", themeEvents(), [["rtk-theme-change", {theme: "dark"}]]);
clearLogs();
eq("unknown returns", I.setTheme("neon"), false);
eq("unknown keeps the theme", I.getTheme(), "dark");
eq("unknown is silent", [writes.length, events.length], [0, 0]);
eq("auto returns", I.setTheme("auto"), true);
eq("auto attribute", attr(), null);
eq("auto stored", store.get("rtk-theme"), "auto");
""")
        self.assertEqual(result["failures"], [])
        self.assertTrue(result["frozen"])

    def test_choice_survives_a_failing_write(self):
        result = run_theme_node("light", """
eq("start", [I.getTheme(), attr()], ["light", "light"]);
eq("returns", I.setTheme("dark"), true);
eq("theme", I.getTheme(), "dark");
eq("attribute", attr(), "dark");
eq("meta colors", colors(), ["#161e24", "#161e24", "#24313a", "#24313a"]);
eq("one event", themeEvents(), [["rtk-theme-change", {theme: "dark"}]]);
eq("write attempted", writes, [["rtk-theme", "dark"]]);
""", storage="write-fails")
        self.assertEqual(result["failures"], [])

    def test_reselecting_the_theme_is_silent(self):
        result = run_theme_node(None, """
I.setTheme("dark");
clearLogs();
eq("same value", I.setTheme("dark"), true);
eq("no event, no write", [events.length, writes.length], [0, 0]);
""")
        self.assertEqual(result["failures"], [])
        result = run_theme_node("default", """
eq("normalised auto", I.setTheme("auto"), true);
eq("no event, no write", [events.length, writes.length], [0, 0]);
""")
        self.assertEqual(result["failures"], [])

    def test_toggle_theme_keeps_legacy_pages_working(self):
        result = run_theme_node(None, """
const pressed = () => toggles[0].getAttribute("aria-pressed");
I.toggleTheme();
eq("auto to high contrast", [I.getTheme(), attr(), pressed()], ["high-contrast", "high-contrast", "true"]);
I.toggleTheme();
eq("back to auto", [I.getTheme(), attr(), pressed()], ["auto", null, "false"]);
""")
        self.assertEqual(result["failures"], [])
        result = run_theme_node("dark", """
I.toggleTheme();
eq("dark to high contrast", [I.getTheme(), attr(), toggles[0].getAttribute("aria-pressed")], ["high-contrast", "high-contrast", "true"]);
""")
        self.assertEqual(result["failures"], [])

    def test_theme_color_follows_the_theme(self):
        tokens = theme_tokens(read_css())
        for name in ("auto", "light", "dark", "high-contrast"):
            light_side = "light" if name == "auto" else name
            dark_side = "dark" if name == "auto" else name
            expected = [tokens[light_side]["--surface"], tokens[dark_side]["--surface"],
                        tokens[light_side]["--hero"], tokens[dark_side]["--hero"]]
            with self.subTest(theme=name):
                result = run_theme_node(None, 'I.setTheme("%s"); out.colors = colors();' % name)
                self.assertEqual([normalize(value) for value in result["colors"]], expected)

    def test_other_tab_change_is_applied(self):
        result = run_theme_node(None, """
const fire = (key, value) => listeners.storage({key: key, newValue: value});
fire("rtk-theme", "dark");
eq("applied", [I.getTheme(), attr()], ["dark", "dark"]);
eq("one event", themeEvents(), [["rtk-theme-change", {theme: "dark"}]]);
clearLogs();
fire("rtk-theme", "dark");
eq("same value is silent", events.length, 0);
fire("rtk-language", "de");
eq("other key is ignored", [I.getTheme(), events.length], ["dark", 0]);
fire("rtk-theme", "neon");
eq("unknown becomes auto", [I.getTheme(), attr(), themeEvents().length], ["auto", null, 1]);
I.setTheme("dark");
clearLogs();
fire("rtk-theme", null);
eq("deleted key", [I.getTheme(), attr(), themeEvents().length], ["auto", null, 1]);
eq("deleted key does not write", writes, []);
I.setTheme("dark");
clearLogs();
fire(null, null);
eq("storage.clear()", [I.getTheme(), attr(), themeEvents().length], ["auto", null, 1]);
eq("no write back", writes, []);
""")
        self.assertEqual(result["failures"], [])
        self.assertEqual(result["writes"], [], "no storage event writes back")


class ThemeShellTest(unittest.TestCase):
    def test_shell_theme_color_matches_the_tokens(self):
        tokens = theme_tokens(read_css())
        for page, html, key in (("app", ui.APP_V2, "--surface"), ("login", ui.APP_V2_LOGIN, "--hero")):
            metas = re.findall(r'<meta name="theme-color"[^>]*>', html)
            with self.subTest(page=page):
                self.assertEqual(len(metas), 2)
                found = {}
                for meta in metas:
                    media = re.search(r'media="\(prefers-color-scheme: (light|dark)\)"', meta)
                    self.assertTrue(media, meta)
                    content = re.search(r'content="([^"]+)"', meta).group(1)
                    found[media.group(1)] = normalize(content)
                    self.assertIn('data-theme-surface="%s"' % ("surface" if page == "app" else "hero"), meta)
                self.assertEqual(found, {"light": tokens["light"][key], "dark": tokens["dark"][key]})
            self.assertIn('<script defer src="/i18n.js', html)
        self.assertNotIn('content="#111820"', ui.APP_V2)


if __name__ == "__main__":
    unittest.main()
