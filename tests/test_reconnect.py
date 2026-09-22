# SPDX-License-Identifier: AGPL-3.0-only
"""Automatic Wi-Fi driver reconnection. Observed in the field: after a demolition, the ESP32 reconnected on its own, but NTRIP remained silent for three hours. The maintenance loop saw isconnected()==True and kept everything in order - without setting wifi_connected_event for the NTRIP task.
"""
import unittest

from support import cfg, net, state


class FakeWLAN:
    def __init__(self, connected=True, ip="203.0.113.195"):
        self._verbunden = connected
        self._ip = ip

    def isconnected(self):
        return self._verbunden

    def ifconfig(self, cfg=None):
        return (self._ip, "255.255.255.0", "203.0.113.225", "203.0.113.225")

    def active(self, v=None):
        return True

    def config(self, *a, **kw):
        return b"\xa1\xb2\xc3\xd4\xe5\xf6"


class TestAdoptConnection(unittest.TestCase):
    def setUp(self):
        self.nm = net.NetworkManager.__new__(net.NetworkManager)
        self.nm.wlan_sta = FakeWLAN()
        state.app.wifi_connected_event.clear()
        state.app.stats["sta_ip"] = "Failed"
        self.saved = dict(cfg.CONFIG)
        cfg.CONFIG["ntp_sync"] = False        # No network access in the test

    def tearDown(self):
        state.app.wifi_connected_event.clear()
        cfg.CONFIG.clear()
        cfg.CONFIG.update(self.saved)

    def test_set_the_event(self):
        """Without that, the NTRIP task is always waiting for you."""
        self.assertTrue(self.nm._adopt_restored_connection())
        self.assertTrue(state.app.wifi_connected_event.is_set())

    def test_takes_over_the_address(self):
        self.nm._adopt_restored_connection()
        self.assertEqual(state.app.stats["sta_ip"], "203.0.113.195")

    def test_reports_the_network_mode(self):
        self.nm._adopt_restored_connection()
        self.assertEqual(state.app.stats["net_mode"], "STA+AP")

    def test_it_works_only_once(self):
        self.assertTrue(self.nm._adopt_restored_connection())
        self.assertFalse(self.nm._adopt_restored_connection())

    def test_when_the_event_is_already_set_nothing_happens(self):
        state.app.wifi_connected_event.set()
        state.app.stats["sta_ip"] = "unchanged"
        self.assertFalse(self.nm._adopt_restored_connection())
        self.assertEqual(state.app.stats["sta_ip"], "unchanged")

    def test_do_not_take_over_without_ip(self):
        # ifconfig can still deliver 0.0.0.0 immediately after the association.
        self.nm.wlan_sta = FakeWLAN(ip="0.0.0.0")
        self.assertFalse(self.nm._adopt_restored_connection())
        self.assertFalse(state.app.wifi_connected_event.is_set())


class TestWifiDiagnostics(unittest.TestCase):
    def test_last_link_values_survive_a_disconnect(self):
        nm = net.NetworkManager.__new__(net.NetworkManager)
        nm.wlan_sta = FakeWLAN()
        nm._last_sta_ssid = ""
        nm._last_sta_rssi = None
        nm._last_sta_channel = None
        nm._remember_sta_link("Field hotspot")
        text = nm._sta_diagnostics()
        self.assertIn("status unknown", text)
        self.assertNotIn("Field hotspot", text)

    def test_smartphone_hotspots_receive_a_longer_connection_window(self):
        with open("net.py") as source:
            text = source.read()
        self.assertIn("seconds = 15 if len(networks) == 1 else 12", text)
        self.assertIn('"access_point_not_found"', text)
        self.assertIn("STA connection timed out (status %s, %s)", text)
        self.assertNotIn("STA connection to %s timed out", text)


class TestLoopRuftEsAuf(unittest.TestCase):
    def test_maintenance_loop_takes_over_the_connection(self):
        with open("net.py") as fh:
            source = fh.read()
        location = source[source.find("# 3. MAINTENANCE LOOP"):]
        location = location[:location.find("Network Manager task stopped")]
        i = location.find("if self.wlan_sta.isconnected():")
        self.assertGreater(i, 0)
        # Window until the next continue - the comment before it is long.
        block = location[i:]
        block = block[:block.find("continue") + 8]
        self.assertIn("_adopt_restored_connection", block)


if __name__ == "__main__":
    unittest.main()


class TestTreiberZurRuheBringen(unittest.TestCase):
    """wlan.connect() turns on the automatic reconnection. After a failed lap, the driver in the background tries on endlessly and jumps over the canal - STA, AP and BLE share a radio part, so everything becomes unattainable."""

    def test_failed_round_separates_the_station(self):
        with open("net.py") as fh:
            source = fh.read()
        location = source[source.find("app.stats[\"wifi_ssid_aktiv\"] = \"\""):]
        location = location[:location.find("return False")]
        self.assertIn("disconnect()", location)

    def test_switching_off_the_ap_is_logged(self):
        # Otherwise, you only see "AP started" in the log and hold the get_element in between
        # for a failure.
        with open("net.py") as fh:
            source = fh.read()
        i = source.find("disable_ap = self.wlan_ap.active()")
        self.assertIn("AP stopped for the connection attempt",
                      source[i:i + 700])


class FakeAP:
    def __init__(self, stations=0, active=True):
        self._stationen = [("mac",)] * stations
        self._aktiv = active

    def active(self, v=None):
        return self._aktiv

    def status(self, field=None):
        if field == "stations":
            return self._stationen
        raise ValueError(field)


class TestApNichtUnterLaufendemClient(unittest.TestCase):
    """An active configuration client must not lose the AP mid-form."""

    def setUp(self):
        self.nm = net.NetworkManager.__new__(net.NetworkManager)
        self.nm._sta_versuche = 5              # Not the first attempt
        self.nm._last_channel_switch = -10 ** 9   # interval expired long ago
        self.saved = dict(cfg.CONFIG)

    def tearDown(self):
        cfg.CONFIG.clear()
        cfg.CONFIG.update(self.saved)

    def test_without_a_client_the_ap_may_give_way(self):
        self.nm.wlan_ap = FakeAP(stations=0)
        self.assertTrue(self.nm._may_disable_ap())

    def test_with_client_he_remains_standing(self):
        self.nm.wlan_ap = FakeAP(stations=1)
        self.assertFalse(self.nm._may_disable_ap())

    def test_even_on_the_first_attempt(self):
        # Even the first attempt must not throw anyone out.
        self.nm._sta_versuche = 0
        self.nm.wlan_ap = FakeAP(stations=1)
        self.assertFalse(self.nm._may_disable_ap())

    def test_firmware_without_station_list_is_not_blocked(self):
        class Alt(FakeAP):
            def status(self, field=None):
                raise AttributeError("does not implement status()")
        self.nm.wlan_ap = Alt()
        self.assertTrue(self.nm._may_disable_ap())


class TestApHatVorrang(unittest.TestCase):
    """A zero channel-switch interval means the AP is never stopped. Previously, 0 caused the opposite - the interval had expired immediately, the AP was switched off on EVERY attempt.
    """

    def setUp(self):
        self.nm = net.NetworkManager.__new__(net.NetworkManager)
        self.nm.wlan_ap = FakeAP(stations=0)
        self.nm._sta_versuche = 5
        self.nm._last_channel_switch = -10 ** 9
        self.saved = dict(cfg.CONFIG)

    def tearDown(self):
        cfg.CONFIG.clear()
        cfg.CONFIG.update(self.saved)

    def test_zero_means_never(self):
        cfg.CONFIG["ap_channel_switch_interval_sec"] = 0
        self.assertFalse(self.nm._may_disable_ap())

    def test_zero_also_applies_on_the_first_attempt(self):
        cfg.CONFIG["ap_channel_switch_interval_sec"] = 0
        self.nm._sta_versuche = 0
        self.assertFalse(self.nm._may_disable_ap())

    def test_positiver_wert_erlaubt_es_weiterhin(self):
        cfg.CONFIG["ap_channel_switch_interval_sec"] = 60
        self.assertTrue(self.nm._may_disable_ap())

    def test_setup_without_configured_network_takes_precedence(self):
        # V10 decides according to state: without network immediately SETUP. The pure
        # channel change rule is retained for RECOVERY attempts.
        self.assertFalse(cfg.configured_networks())
