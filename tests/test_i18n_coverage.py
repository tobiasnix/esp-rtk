# SPDX-License-Identifier: AGPL-3.0-only
"""Regression coverage for every UI term reported during the V11 audit."""
import json
import os
import subprocess
import unittest


ROOT = os.path.dirname(os.path.dirname(__file__))


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
        self.assertEqual(
            set(self.dictionaries["en"]), set(self.dictionaries["de"])
        )

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

    def test_dynamic_whole_paragraphs_are_marked(self):
        with open(os.path.join(ROOT, "web.py"), encoding="utf-8") as source:
            web_source = source.read()
        for key, source in (
            ("ui.network_format_help", web_source),
        ):
            self.assertIn('data-i18n="%s"' % key, source)

    def test_german_time_uses_24_hour_clock(self):
        script = (
            "const i=require('./i18n.js');"
            "i.setLanguage('de');"
            "console.log(i.formatTime(new Date('2026-08-25T17:04:05Z')));"
        )
        result = subprocess.run(
            ["node", "-e", script], cwd=ROOT, capture_output=True,
            text=True, check=True,
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

    def test_status_renderers_translate_labels_explicitly(self):
        for filename in ("pages.js",):
            with open(os.path.join(ROOT, filename), encoding="utf-8") as handle:
                source = handle.read()
            self.assertIn(".label(", source)



if __name__ == "__main__":
    unittest.main()
