# SPDX-License-Identifier: AGPL-3.0-only
"""Factory reset via the BOOT button (GPIO0)."""
import json
import os
import tempfile
import unittest

from support import cfg


class TestFactoryDefaults(unittest.TestCase):
    def test_are_established(self):
        self.assertEqual(cfg.FACTORY_AP_SSID, "ESP-RTK-Setup")
        self.assertEqual(cfg.FACTORY_AP_PASS, "12345678")

    def test_are_no_longer_settable_operating_values(self):
        values, error = cfg.validate_settings(
            {"ap_ssid": cfg.FACTORY_AP_SSID, "ap_pass": cfg.FACTORY_AP_PASS})
        self.assertIsNone(error)
        self.assertEqual(values, {})

    def test_the_factory_password_is_recognizable(self):
        self.assertTrue(cfg.is_factory_password(cfg.FACTORY_AP_PASS))
        self.assertFalse(cfg.is_factory_password("gnss-0123456789ab"))
        self.assertFalse(cfg.is_factory_password(""))


class TestFactoryReset(unittest.TestCase):
    def setUp(self):
        self.saved = dict(cfg.CONFIG)
        self.tmp = tempfile.mkdtemp(prefix="esp-rtk-werk-")
        self.cwd = os.getcwd()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self.cwd)
        cfg.CONFIG.clear()
        cfg.CONFIG.update(self.saved)

    def test_delete_the_access_data(self):
        cfg.CONFIG["wifi_ssid"] = "Home"
        cfg.CONFIG["wifi_pass"] = "secret"
        cfg.CONFIG["ntrip_user"] = "nutzer"
        cfg.CONFIG["ntrip_pass"] = "also-secret"
        cfg.CONFIG["wifi_networks"] = [["Auto", "x"]]
        cfg.factory_reset()
        self.assertEqual(cfg.CONFIG["wifi_ssid"], "")
        self.assertEqual(cfg.CONFIG["wifi_pass"], "")
        self.assertEqual(cfg.CONFIG["ntrip_user"], "")
        self.assertEqual(cfg.CONFIG["ntrip_pass"], "")
        self.assertEqual(cfg.CONFIG["wifi_networks"], [])

    def test_retains_the_physical_identity(self):
        cfg.CONFIG["ap_ssid"] = "Irgendwas"
        cfg.CONFIG["ap_pass"] = "gnss-abcdef123456"
        cfg.factory_reset()
        self.assertEqual(cfg.CONFIG["ap_ssid"], "Irgendwas")
        self.assertEqual(cfg.CONFIG["ap_pass"], "gnss-abcdef123456")

    def test_delete_the_tokens(self):
        # Otherwise, you would not come to the surface after setting back.
        cfg.CONFIG["config_token"] = "old"
        cfg.CONFIG["status_token"] = "old"
        cfg.factory_reset()
        self.assertEqual(cfg.CONFIG["config_token"], "")
        self.assertEqual(cfg.CONFIG["status_token"], "")

    def test_writes_the_file(self):
        self.assertTrue(cfg.factory_reset())
        with open("config.json") as fh:
            saved = json.load(fh)
        self.assertEqual(saved["ap_pass"], cfg.CONFIG["ap_pass"])
        self.assertEqual(saved["wifi_ssid"], "")

    def test_memory_error_breaks_down_before_destructive_steps(self):
        original_save = cfg.save_config
        cfg.CONFIG["wifi_ssid"] = "bestehend"
        with open("tracking.jsonl", "w") as saved:
            saved.write("{}\n")
        try:
            cfg.save_config = lambda new_data=None: False
            self.assertFalse(cfg.factory_reset())
        finally:
            cfg.save_config = original_save
        self.assertEqual(cfg.CONFIG["wifi_ssid"], "bestehend")
        self.assertTrue(os.path.exists("tracking.jsonl"))

    def test_keeps_hardware_values_untouched(self):
        # Pins and baudrate have nothing to do with access data.
        cfg.CONFIG["tx_pin"] = 18
        cfg.factory_reset()
        self.assertEqual(cfg.CONFIG["tx_pin"], 18)

    def test_delete_ble_bonds(self):
        with open("ble-bonds.json", "w") as saved:
            saved.write("[]")
        cfg.factory_reset()
        self.assertFalse(os.path.exists("ble-bonds.json"))

    def test_deletes_survey_data(self):
        with open("tracking.jsonl", "w") as saved:
            saved.write("{}\n")
        cfg.factory_reset()
        self.assertFalse(os.path.exists("tracking.jsonl"))

    def test_deletes_all_runtime_sidecars_and_exports(self):
        names = ("tracking.index.json", "tracking.segment-0001.jsonl",
                 "tracking.segment-0002.corrupt", "survey-export-test.ndjson",
                 "config.pending.json", "config.last_good.json")
        for name in names:
            with open(name, "w") as saved:
                saved.write("runtime")
        cfg.factory_reset()
        self.assertFalse(any(os.path.exists(name) for name in names))


class TestButtonDetection(unittest.TestCase):
    """The decision is separate from the hardware so that it remains testable: expressed = 0, released = 1."""

    def test_continuous_press_triggers_reset(self):
        self.assertTrue(cfg.button_held([0] * 30, required=15))

    def test_short_pressed_does_not_trigger(self):
        self.assertFalse(cfg.button_held([1]*5 + [0]*10 + [1]*15, required=15))

    def test_not_pressed(self):
        self.assertFalse(cfg.button_held([1] * 30, required=15))

    def test_pressed_later_also_counts(self):
        self.assertTrue(cfg.button_held([1]*10 + [0]*20, required=15))

    def test_button_bounce_interrupts_hold(self):
        # A brief letting go should reset the series.
        self.assertFalse(cfg.button_held([0]*10 + [1] + [0]*10, required=15))

    def test_empty_series_of_measurements(self):
        self.assertFalse(cfg.button_held([], required=15))


if __name__ == "__main__":
    unittest.main()
