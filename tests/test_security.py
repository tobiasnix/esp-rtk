# SPDX-License-Identifier: AGPL-3.0-only
"""Securing the configuration interface. The configuration AP is the only way to set credentials -- and at the same time, the only place where a stranger could reconfigure the device.
"""
import json
import os
import tempfile
import unittest

from support import cfg, web


class TestSettableKeys(unittest.TestCase):
    def test_only_form_fields_are_settable(self):
        allowed = set(cfg.SETTABLE_KEYS)
        self.assertEqual(
            allowed,
            {"wifi_ssid", "wifi_pass", "ntrip_enabled", "ntrip_host",
             "ntrip_port", "ntrip_mount", "ntrip_user", "ntrip_pass",
             "ntrip_fallback_host", "ntrip_fallback_port",
             "ntrip_fallback_mount", "ntrip_fallback_user",
             "ntrip_fallback_pass"})

    def test_hardware_parameters_cannot_be_set(self):
        # To make this adjustable via /save, the board configures unbootable - recovery
        # only via the REPL.
        for key in ("tx_pin", "rx_pin", "uart_id", "uart_baud", "http_port",
                    "ap_ip", "status_led_pin", "wdt_enabled", "wdt_timeout",
                    "config_file", "version"):
            self.assertNotIn(key, cfg.SETTABLE_KEYS, key)

    def test_all_settable_keys_exist(self):
        for key in cfg.SETTABLE_KEYS:
            self.assertIn(key, cfg.DEFAULT_CONFIG, key)

    def test_watchdog_is_not_persistent(self):
        # A false config.json should not be able to switch off the watchdog permanently.
        self.assertIn("wdt_enabled", cfg.PERSIST_EXCLUDE)


class TestValidateSettings(unittest.TestCase):
    def accepted(self, **kw):
        values, error = cfg.validate_settings(kw)
        self.assertIsNone(error, "unexpectedly rejected: %s" % error)
        return values

    def rejected(self, **kw):
        values, error = cfg.validate_settings(kw)
        self.assertIsNone(values)
        self.assertTrue(error)
        return error

    def test_valid_form(self):
        values = self.accepted(wifi_ssid="Home", wifi_pass="secret12",
                         ntrip_host="caster.example.invalid", ntrip_port="2101",
                         ntrip_mount="SYNTH", ntrip_user="nutzer",
                         ntrip_pass="password")
        self.assertEqual(values["ntrip_port"], 2101)
        self.assertIsInstance(values["ntrip_port"], int)
        self.assertEqual(values["wifi_ssid"], "Home")

    def test_ntrip_can_be_disabled_and_fallback_is_validated(self):
        values = self.accepted(ntrip_enabled="0",
            ntrip_fallback_host="backup.example.invalid",
            ntrip_fallback_port="2201", ntrip_fallback_mount="SYNTH2",
            ntrip_fallback_user="reserve", ntrip_fallback_pass="secret")
        self.assertIs(values["ntrip_enabled"], False)
        self.assertEqual(values["ntrip_fallback_port"], 2201)

    def test_fallback_header_injection_is_rejected(self):
        self.rejected(ntrip_fallback_host="safe.invalid\r\nX-Bad: 1")

    def test_unknown_fields_are_discarded_instead_of_rejected(self):
        # A browser likes to send additional fields; this is not a mistake.
        values = self.accepted(wifi_ssid="Home", submit="Save")
        self.assertNotIn("submit", values)

    def test_hardware_field_is_discarded(self):
        values = self.accepted(wifi_ssid="Home", tx_pin="99")
        self.assertNotIn("tx_pin", values)

    def test_empty_password_means_unchanged(self):
        values = self.accepted(wifi_ssid="Home", wifi_pass="", ntrip_pass="",
                         ap_pass="")
        self.assertNotIn("wifi_pass", values)
        self.assertNotIn("ntrip_pass", values)
        self.assertNotIn("ap_pass", values)

    # --- Port ---
    def test_port_zero_rejected(self):
        self.rejected(ntrip_port="0")

    def test_port_too_denied(self):
        self.rejected(ntrip_port="65536")

    def test_port_no_number_rejected(self):
        self.rejected(ntrip_port="zweitausend")

    def test_port_grenzwerte_erlaubt(self):
        self.assertEqual(self.accepted(ntrip_port="1")["ntrip_port"], 1)
        self.assertEqual(self.accepted(ntrip_port="65535")["ntrip_port"], 65535)

    # --- SSID ---
    def test_rejected_empty_ssid(self):
        self.rejected(wifi_ssid="")

    def test_ssid_has_been_rejected_for_too_long(self):
        self.rejected(wifi_ssid="x" * 33)

    def test_ssid_allowed_with_32_characters(self):
        self.accepted(wifi_ssid="x" * 32)

    # --- Wi-Fi password ---
    def test_too_short_a_wlan_password_rejected(self):
        # WPA2 requires at least 8 characters; kuerzer does not accept the ESP32.
        self.rejected(wifi_pass="short")

    def test_rejected_too_long_a_wi_fi_password(self):
        self.rejected(wifi_pass="x" * 64)

    # --- AP password ---
    def test_ap_password_is_not_an_operational_configuration(self):
        self.assertNotIn("ap_pass", self.accepted(ap_pass="12345678"))

    # --- Mountpoint / Host: Header-Injection ---
    def test_rejected_mountpoint_with_line_change(self):
        # The mountpoint lands unchecked in the GET line of the NTRIP request - CRLF in
        # it would be header injection.
        self.rejected(ntrip_mount="SYNTH\r\nX-Bad: 1")

    def test_mountpoint_with_slash_rejected(self):
        self.rejected(ntrip_mount="a/b")

    def test_empty_mountpoint_rejected(self):
        self.rejected(ntrip_mount="")

    def test_rejected_host_with_line_upset(self):
        self.rejected(ntrip_host="1.2.3.4\r\nX: 1")

    def test_empty_host_rejected(self):
        self.rejected(ntrip_host="")

    def test_rejected_username_with_line_change(self):
        # Lands base64-encoded in the Authorization header, but the value itself should
        # still be clean.
        self.rejected(ntrip_user="nutzer\nX: 1")


class TestHtmlEscape(unittest.TestCase):
    """Values from the configuration end up unchecked in the setup page."""

    def test_spitze_klammern(self):
        self.assertEqual(cfg.html_escape("<script>"), "&lt;script&gt;")

    def test_quotation_marks(self):
        # Otherwise, break from value=..
        self.assertEqual(cfg.html_escape('a"b'), "a&quot;b")

    def test_ampersand_first(self):
        self.assertEqual(cfg.html_escape("&lt;"), "&amp;lt;")

    def test_innocuous_text_remains(self):
        self.assertEqual(cfg.html_escape("SYNTH"), "SYNTH")

    def test_nichtstring(self):
        self.assertEqual(cfg.html_escape(2101), "2101")


class TestSetupPageEscaping(unittest.TestCase):
    def setUp(self):
        self.saved = dict(cfg.CONFIG)

    def tearDown(self):
        cfg.CONFIG.clear()
        cfg.CONFIG.update(self.saved)

    def test_ssid_with_quotation_marks_does_not_break_the_form(self):
        cfg.CONFIG["wifi_ssid"] = '"><script>alert(1)</script>'
        html = web.render_setup()
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)


class TestApPassword(unittest.TestCase):
    def setUp(self):
        self.saved = dict(cfg.CONFIG)

    def tearDown(self):
        cfg.CONFIG.clear()
        cfg.CONFIG.update(self.saved)

    def test_no_default_in_the_code(self):
        # "12345678" was permanently installed until V9.3 - every device the same.
        self.assertEqual(cfg.DEFAULT_CONFIG["ap_pass"], "")

    def test_is_derived_from_the_mac(self):
        pw = cfg.derive_ap_password(b"\xa1\xb2\xc3\xd4\xe5\xf6")
        self.assertGreaterEqual(len(pw), 8)
        self.assertIn("e5f6", pw.lower())

    def test_is_different_per_device(self):
        a = cfg.derive_ap_password(b"\xa1\xb2\xc3\xd4\xe5\xf6")
        b = cfg.derive_ap_password(b"\xa1\xb2\xc3\xd4\xe5\xf8")
        self.assertNotEqual(a, b)

    def test_cannot_be_set_via_operational_configuration(self):
        pw = cfg.derive_ap_password(b"\xa1\xb2\xc3\xd4\xe5\xf6")
        values, error = cfg.validate_settings({"ap_pass": pw})
        self.assertIsNone(error)
        self.assertNotIn("ap_pass", values)


class TestAtomicSave(unittest.TestCase):
    def setUp(self):
        self.saved = dict(cfg.CONFIG)
        self.tmp = tempfile.mkdtemp(prefix="esp-rtk-atomic-")
        self.cwd = os.getcwd()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self.cwd)
        cfg.CONFIG.clear()
        cfg.CONFIG.update(self.saved)

    def test_no_temporary_file_remains(self):
        cfg.CONFIG["wifi_ssid"] = "Home"
        self.assertTrue(cfg.save_config())
        self.assertEqual(os.listdir("."), ["config.json"])

    def test_old_file_survives_a_failure(self):
        """The core of 'atomic': if the writing fails, the previous configuration is retained instead of remaining as a ruin."""
        cfg.CONFIG["wifi_ssid"] = "Old"
        self.assertTrue(cfg.save_config())

        echtes_dump = cfg.ujson.dump

        def dump_bricht_ab(obj, fh):
            fh.write('{"wifi_ssid": "hal')
            raise OSError(28, "No space left on device")

        cfg.ujson.dump = dump_bricht_ab
        try:
            self.assertFalse(cfg.save_config({"wifi_ssid": "New"}))
        finally:
            cfg.ujson.dump = echtes_dump

        with open("config.json") as fh:
            saved = json.load(fh)          # Must be gullible JSON
        self.assertEqual(saved["wifi_ssid"], "Old")


if __name__ == "__main__":
    unittest.main()


class TestStatusToken(unittest.TestCase):
    def setUp(self):
        self.saved = dict(cfg.CONFIG)

    def tearDown(self):
        cfg.CONFIG.clear()
        cfg.CONFIG.update(self.saved)

    def test_without_tokens_status_is_open(self):
        cfg.CONFIG["status_token"] = ""
        self.assertTrue(web._token_ok(""))
        self.assertTrue(web._token_ok("token=egal"))

    def test_with_tokens_it_is_required(self):
        cfg.CONFIG["status_token"] = "s3cret"
        self.assertFalse(web._token_ok(""))
        self.assertFalse(web._token_ok("token=wrong"))
        self.assertTrue(web._token_ok("token=s3cret"))

    def test_the_token_may_be_url_coded(self):
        cfg.CONFIG["status_token"] = "a b"
        self.assertTrue(web._token_ok("token=a+b"))
        self.assertTrue(web._token_ok("token=a%20b"))

    def test_other_parameters_do_not_interfere(self):
        cfg.CONFIG["status_token"] = "s3cret"
        self.assertTrue(web._token_ok("x=1&token=s3cret&y=2"))


class TestSubnet(unittest.TestCase):
    def test_prefix(self):
        self.assertEqual(cfg._subnet_prefix("192.168.4.1"), "192.168.4.")

    def test_prefix_in_case_of_waste(self):
        self.assertEqual(cfg._subnet_prefix(""), "")
        self.assertEqual(cfg._subnet_prefix("N/A"), "")
        self.assertEqual(cfg._subnet_prefix("Failed"), "")

    def test_collision_is_detected(self):
        self.assertTrue(cfg._same_subnet("192.168.4.1", "192.168.4.55"))

    def test_different_networks(self):
        self.assertFalse(cfg._same_subnet("192.168.4.1", "192.168.5.238"))

    def test_unusable_sta_address_is_not_a_collision(self):
        # sta_ip is "N/A" or "Failed" as long as there is no connection.
        self.assertFalse(cfg._same_subnet("192.168.4.1", "N/A"))
        self.assertFalse(cfg._same_subnet("192.168.4.1", ""))


class TestApSsidIdentity(unittest.TestCase):
    """AP name and code belong to the physical label and are stable."""

    def test_is_not_feasible(self):
        self.assertNotIn("ap_ssid", cfg.SETTABLE_KEYS)

    def test_it_is_denied(self):
        values, error = cfg.validate_settings({"ap_ssid": "Survey-01"})
        self.assertIsNone(error)
        self.assertNotIn("ap_ssid", values)

    def test_empty_name_is_ignored(self):
        values, error = cfg.validate_settings({"ap_ssid": ""})
        self.assertEqual(values, {})
        self.assertIsNone(error)

    def test_too_long_name_is_ignored(self):
        values, error = cfg.validate_settings({"ap_ssid": "x" * 33})
        self.assertEqual(values, {})
        self.assertIsNone(error)

    def test_stall_signs_do_not_reach_identity(self):
        values, error = cfg.validate_settings({"ap_ssid": "a\r\nb"})
        self.assertEqual(values, {})
        self.assertIsNone(error)

    def test_is_not_in_the_form(self):
        self.assertNotIn('name="ap_ssid"', web.render_setup())
