# SPDX-License-Identifier: AGPL-3.0-only
"""Access protection for AP password and local-network configuration."""
import unittest

from support import cfg, web
import assets


class TestRandomApPassword(unittest.TestCase):
    """The password derived from the MAC was known in radio range. The AP-MAC is the BSSID and is available in every Wi-Fi beacon; since the derivation method is available in the public repository, anyone could calculate it within range.
    """

    def test_it_s_random(self):
        a = cfg.generate_ap_password()
        b = cfg.generate_ap_password()
        self.assertNotEqual(a, b)

    def test_is_not_changeable_via_the_form(self):
        pw = cfg.generate_ap_password()
        values, error = cfg.validate_settings({"ap_pass": pw})
        self.assertIsNone(error)
        self.assertNotIn("ap_pass", values)

    def test_remains_recognisable(self):
        self.assertTrue(cfg.generate_ap_password().startswith("gnss-"))

    def test_i_have_enough_entropy(self):
        # 12 Hexzeichen = 48 Bit. Shorter would be in Funkreichweite angreifbar.
        self.assertGreaterEqual(len(cfg.generate_ap_password()), 17)

    def test_recognizes_the_old_derived_password(self):
        mac = b"\xa1\xb2\xc3\xd4\xe5\xf7"
        old = cfg.derive_ap_password(mac)
        self.assertTrue(cfg.is_derived_password(old, mac))

    def test_random_password_is_not_considered_derived(self):
        mac = b"\xa1\xb2\xc3\xd4\xe5\xf7"
        self.assertFalse(cfg.is_derived_password(cfg.generate_ap_password(), mac))

    def test_empty_password_is_not_considered_derived(self):
        self.assertFalse(cfg.is_derived_password("", b"\xa1\xb2\xc3\xd4\xe5\xf7"))


class TestConfigToken(unittest.TestCase):
    """If you reach /save, you can change the Wi-Fi and change the device into a foreign network."""

    def setUp(self):
        self.saved = dict(cfg.CONFIG)

    def tearDown(self):
        cfg.CONFIG.clear()
        cfg.CONFIG.update(self.saved)

    def test_without_tokens_only_through_the_ap(self):
        cfg.CONFIG["config_token"] = ""
        self.assertTrue(web._config_allowed(True, "", b""))
        self.assertFalse(web._config_allowed(False, "", b""))
        self.assertFalse(web._config_allowed(False, "token=egal", b""))

    def test_with_tokens_also_from_the_sta_network(self):
        cfg.CONFIG["config_token"] = "s3cret"
        self.assertTrue(web._config_allowed(False, "token=s3cret", b""))
        self.assertFalse(web._config_allowed(False, "token=wrong-token", b""))
        self.assertFalse(web._config_allowed(False, "", b""))

    def test_ap_does_not_need_a_token(self):
        cfg.CONFIG["config_token"] = "s3cret"
        self.assertTrue(web._config_allowed(True, "", b""))

    def test_tokens_are_allowed_in_the_form(self):
        # A POST from the form sends it in the body, not in the URL.
        cfg.CONFIG["config_token"] = "s3cret"
        self.assertTrue(web._config_allowed(False, "", b"config_token=s3cret&wifi_ssid=X"))
        self.assertFalse(web._config_allowed(False, "", b"config_token=wrong-token"))

    def test_token_is_not_a_configuration_field(self):
        # Otherwise, it could be overwritten via /save itself.
        self.assertNotIn("config_token", cfg.SETTABLE_KEYS)
        values, error = cfg.validate_settings({"config_token": "new"})
        self.assertIsNone(error)
        self.assertNotIn("config_token", values)


class TestSetupPageCarriesToken(unittest.TestCase):
    def setUp(self):
        self.saved = dict(cfg.CONFIG)

    def tearDown(self):
        cfg.CONFIG.clear()
        cfg.CONFIG.update(self.saved)

    def test_without_tokens_no_hidden_field(self):
        cfg.CONFIG["config_token"] = ""
        self.assertNotIn('name="config_token"', web.render_setup())

    def test_old_token_will_not_be_passed_on(self):
        cfg.CONFIG["config_token"] = "s3cret"
        html = web.render_setup(token="s3cret")
        self.assertNotIn('name="config_token"', html)
        self.assertNotIn('token=s3cret', html)

    def test_tokens_are_not_issued_at_all(self):
        cfg.CONFIG["config_token"] = 'a"b'
        html = web.render_setup(token='a"b')
        self.assertNotIn('value="a"b"', html)
        self.assertNotIn("a&quot;b", html)

    def test_via_the_ap_without_token_no_field(self):
        cfg.CONFIG["config_token"] = "s3cret"
        self.assertNotIn('name="config_token"', web.render_setup())


class TestSetupSaveUx(unittest.TestCase):
    def test_one_time_fetch_shows_progress_and_errors(self):
        html = web.render_setup(csrf="CSRF")
        self.assertIn('src="/pages.js?v=%s"' % assets.ASSET_VERSION, html)
        with open("pages.js", encoding="utf-8") as source:
            script = source.read()
        self.assertIn("event.preventDefault()", script)
        self.assertIn("button.disabled = true", script)
        self.assertIn("Save failed", script)
        self.assertIn('tr("ui.save_success"', script)

    def test_restore_uses_visible_confirmation_and_single_request(self):
        with open("pages.js", encoding="utf-8") as source:
            script = source.read()
        self.assertIn('confirmAction(tr("ui.confirm_restore_detail"', script)
        self.assertIn('fetch("/api/config/restore"', script)
        self.assertNotIn('button.dataset.confirmed', script)


if __name__ == "__main__":
    unittest.main()


class TestTokenHierarchy(unittest.TestCase):
    """config_token is the higher-valued token: anyone who is allowed to change the configuration is allowed to read the status, otherwise the web interface would have to carry two tokens."""

    def setUp(self):
        self.saved = dict(cfg.CONFIG)

    def tearDown(self):
        cfg.CONFIG.clear()
        cfg.CONFIG.update(self.saved)

    def test_status_token_applies_to_status(self):
        cfg.CONFIG["status_token"] = "read-token"
        cfg.CONFIG["config_token"] = ""
        self.assertTrue(web._token_ok("token=read-token"))
        self.assertFalse(web._token_ok("token=wrong-token"))

    def test_config_token_also_applies_to_status(self):
        cfg.CONFIG["status_token"] = "read-token"
        cfg.CONFIG["config_token"] = "write-token"
        self.assertTrue(web._token_ok("token=write-token"))

    def test_status_token_does_not_apply_to_configuration(self):
        cfg.CONFIG["status_token"] = "read-token"
        cfg.CONFIG["config_token"] = "write-token"
        self.assertFalse(web._config_allowed(False, "token=read-token", b""))
        self.assertTrue(web._config_allowed(False, "token=write-token", b""))

    def test_without_both_status_remains_open(self):
        cfg.CONFIG["status_token"] = ""
        cfg.CONFIG["config_token"] = ""
        self.assertTrue(web._token_ok(""))

    def test_only_config_tokens_set_leaves_status_open(self):
        # status_token empty still means: /status is free.
        cfg.CONFIG["status_token"] = ""
        cfg.CONFIG["config_token"] = "write-token"
        self.assertTrue(web._token_ok(""))
