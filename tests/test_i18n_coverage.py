# SPDX-License-Identifier: AGPL-3.0-only
"""Regression coverage for every UI term reported during the V11 audit."""
import json
import os
import re
import subprocess
import unittest


ROOT = os.path.dirname(os.path.dirname(__file__))


REGEX_PREFIX = set("(,=:[!&|?{};+-*%<>~^")
JS_KEYWORDS = {"return", "typeof", "case", "in", "of", "else", "do", "void", "delete"}
# Mixed-case product and reference-frame names that must stay untranslated.
# Acronym-only tokens (RTK FIXED, GPS FIX, H/V values, unit lines) carry no prose and are
# skipped by the candidate rule itself.
ALLOWED_UNTRANSLATED = {
    "Quectel LC29H",     # receiver module name
    "ETRS89 (ReNEP)",    # reference frame example in a placeholder
    "Bluetooth LE",      # protocol name on the access card
}


# Units and abbreviations that read the same in every language.
UNITS = {"MHz", "MiB", "dBm", "min", "sat"}


def js_literals(source):
    """Return the text of every string and template literal; ${...} becomes {x}.

    Template expressions are scanned recursively, so literals nested in them count too.
    """
    found = []
    size = len(source)

    def read_string(index, quote):
        end = index + 1
        while end < size and source[end] != quote:
            end += 2 if source[end] == "\\" else 1
        return source[index + 1:end], end + 1

    def read_template(index):
        end, parts = index + 1, []
        while end < size and source[end] != "`":
            if source[end] == "\\":
                parts.append(source[end:end + 2])
                end += 2
            elif source.startswith("${", end):
                depth, cursor = 1, end + 2
                while cursor < size and depth:
                    char = source[cursor]
                    if char in "\"'":
                        cursor = read_string(cursor, char)[1]
                        continue
                    if char == "`":
                        cursor = read_template(cursor)[1]
                        continue
                    depth += (char == "{") - (char == "}")
                    cursor += 1
                found.extend(js_literals(source[end + 2:cursor - 1]))
                parts.append("{x}")
                end = cursor
            else:
                parts.append(source[end])
                end += 1
        return "".join(parts), end + 1

    index, last = 0, ""
    while index < size:
        char = source[index]
        if source.startswith("//", index):
            newline = source.find("\n", index)
            index = size if newline < 0 else newline
        elif source.startswith("/*", index):
            index = source.find("*/", index) + 2
        elif char in "\"'":
            value, index = read_string(index, char)
            found.append(value)
            last = "s"
        elif char == "`":
            value, index = read_template(index)
            found.append(value)
            last = "s"
        elif char == "/" and (last in REGEX_PREFIX or last in ("", "kw")):
            end, in_class = index + 1, False
            while end < size and (in_class or source[end] != "/"):
                if source[end] == "\\":
                    end += 1
                elif source[end] == "[":
                    in_class = True
                elif source[end] == "]":
                    in_class = False
                end += 1
            index = end + 1
            while index < size and source[index].isalpha():
                index += 1
            last = "r"
        elif char.isalnum() or char in "_$":
            end = index
            while end < size and (source[end].isalnum() or source[end] in "_$"):
                end += 1
            last = "kw" if source[index:end] in JS_KEYWORDS else "w"
            index = end
        else:
            if not char.isspace():
                last = char
            index += 1
    return found


def is_ui_prose(value):
    """A literal a person reads: a phrase with at least one lowercase word."""
    text = re.sub(r"\{\w*\}", "", value)
    if " " not in value or not re.search("[A-Za-z]", text):
        return False
    # A template may open with a value, e.g. "{x} task restarts"; plain text opens with a capital or digit.
    if not re.match(r"(?:\{x\}\s*)+\S|[A-Z0-9]", value):
        return False
    if re.match(r"^[.#\[]", value) or re.search("[=<>]", value) or re.match(r"^\w+/\w+$", value):
        return False
    words = re.findall("[A-Za-z]{3,}", text)
    return any(re.search("[a-z]", word) and word not in UNITS for word in words)


class TestI18nCoverage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        script = (
            "const i=require('./i18n.js');"
            "console.log(JSON.stringify(i.dictionaries));"
        )
        result = subprocess.run(
            ["node", "-e", script], cwd=ROOT, capture_output=True,
            text=True, check=True,
        )
        cls.dictionaries = json.loads(result.stdout)

    def test_languages_have_identical_keys(self):
        english = set(self.dictionaries["en"])
        for language in ("de", "pl"):
            self.assertEqual(english, set(self.dictionaries[language]), language)

    def test_reported_terms_have_english_translations(self):
        expected = {
            "ui.network_format_help": "Format:",
            "ui.ram_free": "Free RAM",
            "ui.file_system": "File system",
            "ui.tx_power": "Transmit power",
            "ui.uptime": "Uptime",
            "ui.restarts": "Restarts",
            "ui.failed_boots": "failed boots",
            "ui.satellites": "Satellites",
            "ui.correction_data": "Correction data",
            "ui.height": "Height",
            "ui.network": "Network",
            "ui.fixed_session_hint": "RTK FIXED",
            "ui.kb_received": "KB received",
            "ui.last_message_ago": "last message",
            "ui.station_coordinates": "Station coordinates",
            "ui.receiver_description": "Receiver description",
            "ui.observations": "observations",
            "ui.nmea_tcp": "NMEA over TCP",
            "ui.ble_usage": "Other RX commands",
            "ui.http_api": "HTTP API",
            "ui.log_source_placeholder": "Source",
            "ui.search_log": "Search log",
            "ui.updated": "updated",
            "ui.measurement_progress": "Measurement {collected} of {total}",
            "ui.measurement_saved": "Point saved:",
            "ui.age": "Age",
            "ui.reference_station": "Reference station",
            "ui.gga_sent": "GGA sent:",
            "ui.auth_errors": "authentication errors",
            "ui.fix_quality_time": "Time at each fix quality",
            "ui.no_rtcm_detail": "No correction data received yet",
            "ui.datasheet_specifies": "data sheet specifies",
            "ui.module_help": "GNSS receiver",
        }
        english = self.dictionaries["en"]
        for key, fragment in expected.items():
            self.assertIn(fragment, english[key], key)

    def test_app_translation_keys_exist_in_both_languages(self):
        with open(os.path.join(ROOT, "app.js"), encoding="utf-8") as source:
            app_source = source.read()
        keys = set(re.findall(r'\btr\(\s*"([^"]+)"', app_source))
        # "quality." and "asset." are prefixes completed at run time.
        keys = {key for key in keys if not key.endswith(".")}
        self.assertGreater(len(keys), 40)
        for language in ("en", "de", "pl"):
            missing = sorted(key for key in keys if key not in self.dictionaries[language])
            self.assertEqual(missing, [], language)

    def test_reverse_lookup_literals_kept_their_translation(self):
        # These literals were translated only by the removed reverse dictionary lookup
        # (English value -> key -> German value; the last key wins on duplicates).
        literals = (
            "unknown", "label", "Vertical accuracy", "Satellites", "Cancel",
            "Optimize recording storage", "Download", "e.g. Field road east",
            "Rename project", "Delete project", "Device functions", "Stream key",
            "Rotate stream key", "Send", "Restart is being prepared \u2026",
            "Device is offline \u2014 waiting for it to return \u2026",
            "Restart successful \u2014 connection restored.",
            "The device is still restarting. Check Wi-Fi and reload when its network returns.",
            "Direct web portal", "ground", "Search log", "GNSS module",
            # Found while checking the app in German: reverse hits beyond the planned 22.
            "Encrypted pairing", "NMEA over TCP", "Web portal",
        )
        self.assertEqual(len(literals), 25)
        script = "const i=require('./i18n.js');console.log(JSON.stringify(i.v2de));"
        v2de = json.loads(subprocess.run(
            ["node", "-e", script], cwd=ROOT, capture_output=True, text=True,
            check=True).stdout)
        english, german = self.dictionaries["en"], self.dictionaries["de"]
        for literal in literals:
            keys = [key for key, value in english.items() if value == literal]
            self.assertTrue(keys, literal)
            self.assertEqual(v2de.get(literal), german[keys[-1]], literal)

    def test_app_ui_literals_have_german_and_polish_text(self):
        with open(os.path.join(ROOT, "app.js"), encoding="utf-8") as source:
            app_source = source.read()
        # tr("key", "fallback") pairs are translated through the dictionaries.
        app_source = re.sub(
            r'\btr\(\s*"(?:[^"\\]|\\.)*"\s*,\s*(?:"(?:[^"\\]|\\.)*"|`[^`]*`)', "tr(", app_source)
        script = ("const i=require('./i18n.js');"
                  "console.log(JSON.stringify({de:i.v2de,pl:i.v2pl}));")
        maps = json.loads(subprocess.run(
            ["node", "-e", script], cwd=ROOT, capture_output=True, text=True,
            check=True).stdout)
        shape = lambda value: re.sub(r"\{\w*\}", "{}", value)
        candidates = {
            literal.replace("\\u2026", "\u2026").replace('\\"', '"')
            for literal in js_literals(app_source)}
        prose = sorted(value for value in candidates
                       if is_ui_prose(value) and value not in ALLOWED_UNTRANSLATED)
        for language, label in (("de", "German"), ("pl", "Polish")):
            known = {shape(key) for key in maps[language]}
            missing = sorted(value for value in prose if shape(value) not in known)
            self.assertEqual(missing, [], "UI literals without a %s text" % label)
        self.assertLessEqual(len(ALLOWED_UNTRANSLATED), 15)

    def test_quantity_forms_have_german_and_polish_text(self):
        # The pattern only sees quantity(...) calls whose forms sit on one source line.
        with open(os.path.join(ROOT, "app.js"), encoding="utf-8") as source:
            app_source = source.read()
        forms = set()
        for one, many in re.findall(r'quantity\([^\n]*?,\s*"([^"]+)"\s*,\s*"([^"]+)"\s*\)', app_source):
            forms.update((one, many))
        script = ("const i=require('./i18n.js');"
                  "console.log(JSON.stringify({de:i.v2de,pl:i.v2pl}));")
        maps = json.loads(subprocess.run(
            ["node", "-e", script], cwd=ROOT, capture_output=True, text=True,
            check=True).stdout)
        self.assertGreater(len(forms), 10)
        for language in ("de", "pl"):
            self.assertEqual(sorted(form for form in forms if form not in maps[language]), [], language)

    def test_dynamic_whole_paragraphs_are_marked(self):
        with open(os.path.join(ROOT, "web.py"), encoding="utf-8") as source:
            web_source = source.read()
        for key, source in (
            ("ui.network_format_help", web_source),
        ):
            self.assertIn('data-i18n="%s"' % key, source)

    def test_german_and_polish_time_use_24_hour_clock(self):
        for language in ("de", "pl"):
            script = (
                "const i=require('./i18n.js');"
                "i.setLanguage(process.argv[1]);"
                "console.log(i.formatTime(new Date('2026-08-25T17:04:05Z')));"
            )
            result = subprocess.run(
                ["node", "-e", script, language], cwd=ROOT, capture_output=True,
                text=True, check=True, env={**os.environ, "TZ": "UTC"},
            )
            rendered = result.stdout.strip()
            self.assertIn("17", rendered)
            self.assertNotRegex(rendered.lower(), r"\b(am|pm)\b")

    def test_explicit_keys_can_switch_languages(self):
        script = r"""
const i=require('./i18n.js');
const keys=['ui.ram_free','ui.file_system','ui.tx_power',
  'ui.uptime','ui.restarts','ui.satellites',
  'ui.correction_data','ui.height'];
i.setLanguage('de');
const german=keys.map(key=>i.t(key));
const labels=['Satellites','Correction data','Height','Network'].map(value=>i.label(value));
i.setLanguage('en');
const english=keys.map(key=>i.t(key));
console.log(JSON.stringify({english,german,labels}));
"""
        result = subprocess.run(
            ["node", "-e", script], cwd=ROOT, capture_output=True,
            text=True, check=True,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(len(set(payload["german"])), len(payload["german"]))
        self.assertNotEqual(payload["german"], payload["english"])
        self.assertEqual(payload["labels"],
                         ["Satelliten", "Korrekturdaten", "H\u00f6he", "Netz"])


    def test_login_page_uses_explicit_translation_keys(self):
        with open(os.path.join(ROOT, "web.py"), encoding="utf-8") as handle:
            source = handle.read()
        for key in ("ui.secure_field_receiver", "ui.device_access",
                    "ui.enter_code", "ui.show", "action.login"):
            self.assertIn('data-i18n="%s"' % key, source)

    def test_all_static_subpages_mark_primary_ui_text(self):
        with open(os.path.join(ROOT, "web.py"), encoding="utf-8") as handle:
            source = handle.read()
        for key in ("ui.diagnostics_title", "ui.download_diagnostics",
                    "ui.configuration", "ui.device_access",
                    "ui.wifi_connection", "ui.backup_restore",
                    "ui.ntrip_caster", "ui.device_label_access",
                    "ui.direct_portal_detail", "ui.bluetooth_pairing_detail"):
            self.assertIn('data-i18n="%s"' % key, source)

    def test_polish_reverse_map_covers_the_german_literals(self):
        script = ("const i=require('./i18n.js');"
                  "console.log(JSON.stringify({de:i.v2de,pl:i.v2pl}));")
        maps = json.loads(subprocess.run(
            ["node", "-e", script], cwd=ROOT, capture_output=True, text=True,
            check=True).stdout)
        self.assertEqual(sorted(maps["de"]), sorted(maps["pl"]))
        shape = lambda value: sorted(re.findall(r"\{\w+\}", value))
        for source, translated in maps["pl"].items():
            self.assertTrue(translated.strip(), source)
            self.assertEqual(shape(source), shape(translated), source)

    def test_polish_uses_three_plural_forms(self):
        script = r"""
const i=require('./i18n.js');
i.setLanguage('pl');
console.log(JSON.stringify([1,2,5,12,22].map(n => i.plural('point','points',n))));
"""
        result = subprocess.run(["node", "-e", script], cwd=ROOT,
                                capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout),
                         ["punkt", "punkty", "punktów", "punktów", "punkty"])

    def test_polish_is_detected_and_offered_in_the_selector(self):
        script = r"""
Object.defineProperty(globalThis, "navigator", {value: {language: "pl-PL"}, configurable: true});
const i=require('./i18n.js');
console.log(JSON.stringify({language: i.getLanguage(), selector: i.selector()}));
"""
        result = subprocess.run(["node", "-e", script], cwd=ROOT,
                                capture_output=True, text=True, check=True)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["language"], "pl")
        self.assertIn('data-language="pl"', payload["selector"])

    def test_status_renderers_translate_labels_explicitly(self):
        for filename in ("pages.js",):
            with open(os.path.join(ROOT, filename), encoding="utf-8") as handle:
                source = handle.read()
            self.assertIn(".label(", source)



if __name__ == "__main__":
    unittest.main()
