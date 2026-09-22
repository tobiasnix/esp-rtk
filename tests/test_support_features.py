# SPDX-License-Identifier: AGPL-3.0-only
"""WiFi selection, backup contract and diagnostic package."""
import os
import tempfile
import unittest

from support import cfg, state, web


class TestWifiScan(unittest.TestCase):
    def test_deduplicated_and_sorted_without_bssid(self):
        class Wlan:
            def active(self, value=None): return True
            def scan(self):
                return [(b"field-test-network", b"mac1", 1, -70, 3, False),
                        (b"field-test-network", b"mac2", 6, -40, 3, False),
                        (b"offen", b"mac3", 1, -50, 0, False)]
        original = web.network.WLAN
        web.network.WLAN = lambda _iface: Wlan()
        try: result, error = web.wifi_scan_results()
        finally: web.network.WLAN = original
        self.assertIsNone(error)
        self.assertEqual([item["ssid"] for item in result], ["field-test-network", "offen"])
        self.assertNotIn("bssid", result[0])
        self.assertTrue(result[0]["secured"])

    def test_masked_network_edits_preserve_existing_passwords(self):
        current = [["Field", "fallback12"], ["Open", ""]]
        self.assertEqual(web.merge_masked_wifi_networks([
            {"ssid": "Field", "password_set": True},
            {"ssid": "Open", "password_set": False},
            {"ssid": "New", "password": "newpass12"}], current),
            [["Field", "fallback12"], ["Open", ""], ["New", "newpass12"]])


class TestBackup(unittest.TestCase):
    def setUp(self):
        self.saved = dict(cfg.CONFIG)

    def tearDown(self):
        cfg.CONFIG.clear(); cfg.CONFIG.update(self.saved)

    def test_backup_contains_trade_secrets_but_no_identity(self):
        cfg.CONFIG.update({"wifi_ssid": "field-test-network", "wifi_pass": "password12",
                           "ntrip_pass": "castersecret"})
        result = web.config_backup()
        self.assertEqual(result["kind"], "esp-rtk-config-backup")
        self.assertEqual(result["config"]["wifi_pass"], "password12")
        self.assertNotIn("device_code", result["config"])
        self.assertNotIn("stream_token", result["config"])

    def test_restore_validated_schema_and_wlan_passwords(self):
        values, error = web.validate_backup({"schema_version": 1,
            "kind": "esp-rtk-config-backup", "config": {
                "wifi_ssid": "field-test-network", "wifi_pass": "password12",
                "wifi_networks": [["Field", "fallback12"]]}})
        self.assertIsNone(error)
        self.assertEqual(values["wifi_networks"], [["Field", "fallback12"]])
        _values, error = web.validate_backup({"schema_version": 9})
        self.assertIn("schema", error)


class TestDiagnostics(unittest.TestCase):
    def test_snapshot_does_contain_support_data_but_no_secrets(self):
        saved_config, saved_identity = dict(cfg.CONFIG), state.app.identity
        cfg.CONFIG.update({"wifi_ssid": "field-test-network", "wifi_pass": "password12",
                           "ntrip_pass": "castersecret"})
        state.app.identity = {"device_id": "ID", "device_code": "DEVICESECRET",
                              "stream_token": "STREAMSECRET"}
        try:
            snapshot = web.diagnostics_snapshot()
            rendered = web.ujson.dumps(snapshot)
        finally:
            cfg.CONFIG.clear(); cfg.CONFIG.update(saved_config)
            state.app.identity = saved_identity
        self.assertIn("field-test-network", rendered)
        for secret in ("password12", "castersecret", "DEVICESECRET", "STREAMSECRET"):
            self.assertNotIn(secret, rendered)

    def test_config_result_survives_reloading(self):
        cwd = os.getcwd(); tmp = tempfile.mkdtemp(prefix="esp-result-"); os.chdir(tmp)
        try:
            self.assertTrue(cfg.config_result_write("failed", "Incorrect password"))
            self.assertEqual(cfg.config_result_read()["status"], "failed")
        finally:
            os.chdir(cwd)


if __name__ == "__main__": unittest.main()
