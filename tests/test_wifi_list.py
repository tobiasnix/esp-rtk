# SPDX-License-Identifier: AGPL-3.0-only
"""Multiple Wi-Fi networks with fallbacks. An RTK rover moves between home, car hotspot and field. Previously, "different Wi-Fi" was always called: connect to the configuration AP and reconfigure.
"""
import unittest

from support import cfg, net


class TestConfiguredNetworks(unittest.TestCase):
    def setUp(self):
        self.saved = dict(cfg.CONFIG)

    def tearDown(self):
        cfg.CONFIG.clear()
        cfg.CONFIG.update(self.saved)

    def test_primary_network_is_in_front(self):
        cfg.CONFIG["wifi_ssid"] = "Home"
        cfg.CONFIG["wifi_pass"] = "hidden-value"
        cfg.CONFIG["wifi_networks"] = []
        self.assertEqual(cfg.configured_networks(), [("Home", "hidden-value")])

    def test_other_networks_follow(self):
        cfg.CONFIG["wifi_ssid"] = "Home"
        cfg.CONFIG["wifi_pass"] = "a"
        cfg.CONFIG["wifi_networks"] = [["Auto", "b"], ["Field", "c"]]
        self.assertEqual(cfg.configured_networks(),
                         [("Home", "a"), ("Auto", "b"), ("Field", "c")])

    def test_without_a_primary_network(self):
        cfg.CONFIG["wifi_ssid"] = ""
        cfg.CONFIG["wifi_networks"] = [["Auto", "b"]]
        self.assertEqual(cfg.configured_networks(), [("Auto", "b")])

    def test_double_ssid_only_once(self):
        cfg.CONFIG["wifi_ssid"] = "Home"
        cfg.CONFIG["wifi_pass"] = "a"
        cfg.CONFIG["wifi_networks"] = [["Home", "obsolete"], ["Auto", "b"]]
        self.assertEqual(cfg.configured_networks(), [("Home", "a"), ("Auto", "b")])

    def test_broken_entries_are_skipped(self):
        cfg.CONFIG["wifi_ssid"] = ""
        cfg.CONFIG["wifi_networks"] = [["Gut", "x"], ["OhnePass"], [], "quatsch", None]
        self.assertEqual(cfg.configured_networks(), [("Gut", "x")])


class TestOrderBySignalStrength(unittest.TestCase):
    """The scan tells you which networks are there - then you try them in the CONFIGURE order, not by field strength. The form calls the list "diversion networks"; the word promises a ranking. Sorting by field strength made the choice unpredictable and thus also the IP address under which the device can be found.
    """

    SCAN = [
        (b"Field", b"\x01", 11, -80, 3, False),
        (b"Home", b"\x02", 10, -31, 3, False),
        (b"Fremd", b"\x03", 6, -40, 3, False),
    ]

    def test_only_visible_networks(self):
        configured = [("Home", "a"), ("Auto", "b"), ("Field", "c")]
        self.assertEqual(net.order_networks(configured, self.SCAN),
                         [("Home", "a"), ("Field", "c")])

    def test_configured_order_wins(self):
        # Field is clearly weaker with -80 dBm than Home with -31, but is in front - so
        # it is tried first.
        configured = [("Field", "c"), ("Home", "a")]
        self.assertEqual([s for s, _p in net.order_networks(configured, self.SCAN)],
                         ["Field", "Home"])

    def test_the_main_network_remains_the_main_network(self):
        configured = [("Home", "a"), ("Field", "c")]
        self.assertEqual([s for s, _p in net.order_networks(configured, self.SCAN)],
                         ["Home", "Field"])

    def test_without_scan_the_configured_order_remains(self):
        configured = [("Auto", "b"), ("Home", "a")]
        self.assertEqual(net.order_networks(configured, []), configured)
        self.assertEqual(net.order_networks(configured, None), configured)

    def test_no_configured_network_is_visible(self):
        # Then try everyone anyway - the scan may be wrong.
        configured = [("Auto", "b")]
        self.assertEqual(net.order_networks(configured, self.SCAN), configured)

    def test_unicode_characters_in_name(self):
        scan = [("Café".encode(), b"\x01", 1, -50, 3, False)]
        self.assertEqual(net.order_networks([("Café", "x")], scan), [("Café", "x")])


class TestFormField(unittest.TestCase):
    """The list is edited as a text field: one line per network."""

    def test_output_does_not_show_any_passwords(self):
        text = cfg.configured_networks_text([["Auto", "hidden-value"], ["Field", "another-value"]])
        self.assertNotIn("hidden-value", text)
        self.assertIn("Auto", text)
        self.assertIn("Field", text)

    def test_read_with_password(self):
        self.assertEqual(cfg.parse_configured_networks("Auto:hidden-value", []),
                         [["Auto", "hidden-value"]])

    def test_name_alone_retains_the_stored_password(self):
        existing = [["Auto", "old"]]
        self.assertEqual(cfg.parse_configured_networks("Auto", existing),
                         [["Auto", "old"]])

    def test_name_alone_without_stored_password_falls_away(self):
        self.assertEqual(cfg.parse_configured_networks("New", []), [])

    def test_multiple_lines(self):
        existing = [["Auto", "old"]]
        self.assertEqual(
            cfg.parse_configured_networks("Auto\nField:new\n\n  \n", existing),
            [["Auto", "old"], ["Field", "new"]])

    def test_colon_in_the_password(self):
        self.assertEqual(cfg.parse_configured_networks("Auto:a:b:c", []),
                         [["Auto", "a:b:c"]])

    def test_empty_field_deletes_the_list(self):
        self.assertEqual(cfg.parse_configured_networks("", [["Auto", "old"]]), [])

    def test_circulating_text_is_trimmed(self):
        self.assertEqual(cfg.parse_configured_networks("  Auto : x  ", []),
                         [["Auto", "x"]])


if __name__ == "__main__":
    unittest.main()


class TestForm(unittest.TestCase):
    def setUp(self):
        self.saved = dict(cfg.CONFIG)

    def tearDown(self):
        cfg.CONFIG.clear()
        cfg.CONFIG.update(self.saved)

    def test_field_is_in_the_form(self):
        from support import web
        self.assertIn('name="wifi_networks"', web.render_setup())

    def test_shows_the_names_without_passwords(self):
        from support import web
        cfg.CONFIG["wifi_networks"] = [["Auto", "hidden-value"], ["Field", "another-value"]]
        html = web.render_setup()
        self.assertIn("Auto", html)
        self.assertIn("Field", html)
        self.assertNotIn("hidden-value", html)

    def test_no_placeholder_stands_still(self):
        from support import web
        self.assertNotIn("{wifi_networks}", web.render_setup())

    def test_names_are_masked(self):
        from support import web
        cfg.CONFIG["wifi_networks"] = [['<script>', "x"]]
        html = web.render_setup()
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn('<textarea id="wifi-networks" name="wifi_networks" '
                         'placeholder="Car-Hotspot:Password&#10;Field-Wi-Fi"><script>', html)
