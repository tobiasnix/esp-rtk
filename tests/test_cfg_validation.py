# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for the hardware-independent configuration boundary."""
import unittest

from support import cfg
import cfg_validation


class TestPureConfigurationValidation(unittest.TestCase):
    def test_validation_has_no_cfg_dependency_and_casts_port(self):
        values, error = cfg_validation.validate_settings(
            {"wifi_ssid": "Field", "ntrip_port": "2101", "ignored": "x"},
            ("wifi_ssid", "ntrip_port"), ())
        self.assertIsNone(error)
        self.assertEqual(values, {"wifi_ssid": "Field", "ntrip_port": 2101})

    def test_network_list_keeps_primary_first_and_deduplicates(self):
        result = cfg_validation.configured_networks({
            "wifi_ssid": "Primary", "wifi_pass": "secret123",
            "wifi_networks": [["Primary", "old"], ["Fallback", "pass1234"]],
        })
        self.assertEqual(result, [("Primary", "secret123"),
                                  ("Fallback", "pass1234")])

    def test_subnet_helpers_reject_invalid_values(self):
        self.assertTrue(cfg_validation.same_subnet("192.168.4.1", "192.168.4.9"))
        self.assertFalse(cfg_validation.same_subnet("", ""))


class TestLoadValidators(unittest.TestCase):
    def test_whole_number_checks_type_and_integrality_before_converting(self):
        w = cfg_validation.whole_number
        for value, expected in ((2101, 2101), ("2101", 2101), (" 2101 ", 2101), (2101.0, 2101)):
            self.assertEqual(w(value), expected, value)
        for bad in (True, False, None, 65535.9, float("inf"), float("-inf"), float("nan"),
                    "abc", "1e999", "2101.5", "", [2101], {"port": 2101}):
            self.assertIsNone(w(bad), bad)

    def test_port_validation_rejects_bool_fraction_non_finite_and_overflow(self):
        for key in ("ntrip_port", "ntrip_fallback_port"):
            for bad in (True, 65535.9, float("inf"), float("nan"), 10 ** 30, "1e999"):
                values, error = cfg_validation.validate_settings({key: bad}, cfg.SETTABLE_KEYS, ())
                self.assertIsNone(values, (key, bad))
                self.assertIn(key, error)
            self.assertEqual(cfg_validation.validate_ports({key: 65535.0}), ({key: 65535}, None))

    def test_load_validators_cover_field_setup_ports_and_watchdog(self):
        self.assertEqual(set(cfg_validation.LOAD_VALIDATORS), {
            "antenna_height_m", "reference_frame", "reference_frame_epoch",
            "ntrip_port", "ntrip_fallback_port", "wdt_timeout", "measurement_samples",
            "stakeout_tolerance_m", "stakeout_fine_radius_m"})

    def test_every_load_validator_accepts_its_default(self):
        for key, validator in cfg_validation.LOAD_VALIDATORS.items():
            clean, error = validator({key: cfg.DEFAULT_CONFIG[key]})
            self.assertIsNone(error, key)
            self.assertEqual(clean[key], cfg.DEFAULT_CONFIG[key], key)

    def test_stakeout_settings_accept_their_ranges_in_millimetres(self):
        v = cfg_validation.validate_stakeout_settings
        tolerance, radius = "stakeout_tolerance_m", "stakeout_fine_radius_m"
        for key, cases in ((tolerance, ((0.02, 0.02), (5, 5.0), ("0.1", 0.1),
                                        (0.0255, 0.026), (5.0, 5.0))),
                           (radius, ((1, 1.0), (50, 50.0), ("12.5", 12.5)))):
            for value, expected in cases:
                clean, error = v({key: value})
                self.assertEqual((clean, error), ({key: expected}, None), (key, value))
                self.assertIsInstance(clean[key], float)
        for key, bads in ((tolerance, (0.019, 5.001, 0, -1, True, None, "abc",
                                       float("nan"), float("inf"), [1])),
                          (radius, (0.999, 50.001, 0, True, None, "abc",
                                    float("nan"), float("inf"), [1]))):
            for bad in bads:
                clean, error = v({key: bad})
                self.assertIsNone(clean, (key, bad))
                self.assertIn(key, error)
        self.assertEqual(cfg_validation.STAKEOUT_SETTINGS_KEYS, (tolerance, radius))
        self.assertEqual(cfg_validation.STAKEOUT_TOLERANCE_MM, (20, 5000))
        self.assertEqual(cfg_validation.STAKEOUT_FINE_RADIUS_MM, (1000, 50000))

    def test_stakeout_tolerance_must_stay_below_the_fine_radius(self):
        v = cfg_validation.validate_stakeout_settings
        message = "stakeout_tolerance_m must be smaller than stakeout_fine_radius_m."
        self.assertEqual(v({"stakeout_tolerance_m": 2.0}, {"stakeout_fine_radius_m": 2.0}),
                         (None, message))
        self.assertEqual(v({"stakeout_fine_radius_m": 1.0}, {"stakeout_tolerance_m": 1.5}),
                         (None, message))
        self.assertEqual(v({"stakeout_tolerance_m": 0.5, "stakeout_fine_radius_m": 1.0}),
                         ({"stakeout_tolerance_m": 0.5, "stakeout_fine_radius_m": 1.0}, None))
        self.assertEqual(v({"stakeout_tolerance_m": 2.0, "stakeout_fine_radius_m": 2.0}),
                         (None, message))
        # No cross check without current, and with only one value known.
        self.assertEqual(v({"stakeout_tolerance_m": 4.0}), ({"stakeout_tolerance_m": 4.0}, None))
        self.assertEqual(v({"stakeout_fine_radius_m": 1.0}), ({"stakeout_fine_radius_m": 1.0}, None))
        self.assertEqual(cfg_validation.stakeout_pair_error(2.0, 1.0), message)
        self.assertIsNone(cfg_validation.stakeout_pair_error(0.1, 5.0))

    def test_stakeout_settings_reject_unknown_keys_and_non_objects(self):
        v = cfg_validation.validate_stakeout_settings
        self.assertEqual(v({"tolerance_m": 0.1}), (None, "Unknown stakeout setting: tolerance_m"))
        self.assertEqual(v([]), (None, "Stakeout settings must be an object."))

    def test_measurement_samples_accepts_exactly_the_four_methods(self):
        v = cfg_validation.validate_measurement_samples
        for value, expected in ((1, 1), (5, 5), (10, 10), (5.0, 5), ("10", 10),
                                ("quality", "quality")):
            self.assertEqual(v({"measurement_samples": value}),
                             ({"measurement_samples": expected}, None), value)
        for bad in (0, 2, 3, 30, -5, True, False, None, 5.5, float("nan"), float("inf"),
                    "Quality", "", "5 samples", [5], {"n": 5}):
            clean, error = v({"measurement_samples": bad})
            self.assertIsNone(clean, bad)
            self.assertIn("measurement_samples", error)
        self.assertEqual(cfg_validation.MEASUREMENT_SAMPLES_CHOICES, (1, 5, 10, "quality"))

    def test_watchdog_timeout_bounds(self):
        v = cfg_validation.validate_wdt_timeout
        self.assertEqual(v({"wdt_timeout": 10000}), ({"wdt_timeout": 10000}, None))
        self.assertEqual(v({"wdt_timeout": "15000"}), ({"wdt_timeout": 15000}, None))
        self.assertEqual(v({"wdt_timeout": 600000}), ({"wdt_timeout": 600000}, None))
        for bad in (9999, 100, 0, -1, True, None, "abc", 15000.5, float("inf"),
                    600001, 2 ** 31, 10 ** 30):                           # #18 M-5
            clean, error = v({"wdt_timeout": bad})
            self.assertIsNone(clean, bad)
            self.assertIn("wdt_timeout", error)


class TestPrepareConfigChange(unittest.TestCase):
    def prep(self, raw, validators=None):
        return cfg_validation.prepare_config_change(raw, cfg.SETTABLE_KEYS, cfg.SECRET_KEYS, validators)

    def test_nine_networks_are_refused(self):
        nets = [["Net%d" % i, "password%d" % i] for i in range(9)]
        values, code, message = self.prep({"wifi_ssid": "A", "wifi_networks": nets})
        self.assertIsNone(values)
        self.assertEqual(code, "invalid_wifi_networks")
        self.assertIn("at most 8", message)

    def test_short_fallback_password_is_refused(self):
        self.assertEqual(self.prep({"wifi_networks": [["Field", "short"]]})[1], "invalid_wifi_networks")

    def test_settings_error_and_non_object(self):
        self.assertEqual(self.prep({"ntrip_port": 70000})[1:],
                         ("invalid_configuration", "ntrip_port must be between 1 and 65535."))
        self.assertEqual(self.prep({"ntrip_port": True})[1], "invalid_configuration")
        for raw in (None, [], "x", 3):
            self.assertEqual(self.prep(raw)[1], "invalid_configuration")

    def test_unsettable_keys_and_blank_secrets_are_dropped(self):
        values, code, _ = self.prep({"uart_id": 3, "wifi_ssid": "A", "wifi_pass": "",
                                     "wifi_networks": [["F", ""]]})
        self.assertIsNone(code)
        self.assertEqual(values, {"wifi_ssid": "A", "wifi_networks": [["F", ""]]})

    def test_supplied_validators_run_per_key(self):
        refuse = {"ntrip_mount": lambda v: (None, "mount refused")}
        self.assertEqual(self.prep({"ntrip_mount": "M"}, refuse)[1:], ("invalid_configuration", "mount refused"))
        upper = {"ntrip_mount": lambda v: ({"ntrip_mount": v["ntrip_mount"].upper()}, None)}
        self.assertEqual(self.prep({"ntrip_mount": "m"}, upper)[0], {"ntrip_mount": "M"})

    def test_input_refuses_what_the_loader_refuses(self):
        for key in set(cfg_validation.LOAD_VALIDATORS) & set(cfg.SETTABLE_KEYS):
            for bad in (True, 0, 70000, 2101.5, "abc", None):
                loaded, _error = cfg_validation.LOAD_VALIDATORS[key]({key: bad})
                self.assertIsNone(loaded, (key, bad))
                self.assertIsNone(cfg.prepare_config_change({key: bad})[0], (key, bad))

    def test_never_raises_for_json_values(self):
        for key in tuple(cfg.SETTABLE_KEYS) + ("wifi_networks",):
            for value in (None, True, 1, 1.5, "", "x" * 300, [], {}, [["a"]], [[1, 2]]):
                result = cfg.prepare_config_change({key: value})
                self.assertEqual(len(result), 3, (key, value))


class TestRestoreChange(unittest.TestCase):
    """The restore accepts every state the device itself can back up (#24b)."""

    def prepare(self, raw):
        return cfg_validation.prepare_restore_change(raw, cfg.SETTABLE_KEYS, cfg.SECRET_KEYS,
                                                     cfg_validation.LOAD_VALIDATORS)

    def test_empty_caster_and_secrets_are_restorable_states(self):
        values, kept, code, message = self.prepare({
            "wifi_ssid": "SiteNet", "wifi_pass": "", "ntrip_enabled": False, "ntrip_host": "",
            "ntrip_mount": "", "ntrip_user": "", "ntrip_pass": "", "ntrip_fallback_pass": "",
            "wifi_networks": []})
        self.assertEqual((kept, code, message), ((), None, None))
        for key in ("wifi_pass", "ntrip_host", "ntrip_mount", "ntrip_pass", "ntrip_fallback_pass"):
            self.assertEqual(values[key], "", key)
        self.assertIs(values["ntrip_enabled"], False)

    def test_missing_primary_ssid_with_fallbacks_is_restored(self):
        values, kept, _code, _message = self.prepare(
            {"wifi_ssid": "", "wifi_pass": "", "wifi_networks": [["Field", "fallback12"]]})
        self.assertEqual((values["wifi_ssid"], values["wifi_networks"], kept),
                         ("", [["Field", "fallback12"]], ()))

    def test_backup_without_any_network_keeps_the_target_wifi(self):
        values, kept, _code, _message = self.prepare(
            {"wifi_ssid": "", "wifi_pass": "", "wifi_networks": [], "ntrip_host": ""})
        self.assertEqual(kept, ("wifi",))
        for key in ("wifi_ssid", "wifi_pass", "wifi_networks"):
            self.assertNotIn(key, values)
        self.assertEqual(values["ntrip_host"], "")

    def test_non_empty_values_keep_the_normal_rules(self):
        for raw in ({"ntrip_mount": "a/b"}, {"ntrip_host": "h" * 65}, {"wifi_ssid": "s" * 33},
                    {"wifi_pass": "short"}, {"ntrip_port": 0}, {"wifi_networks": [["", "x"]]}, []):
            values, kept, code, message = self.prepare(raw)
            with self.subTest(raw=raw):
                self.assertIsNone(values)
                self.assertIn(code, ("invalid_configuration", "invalid_wifi_networks"))
        values, _kept, _code, _message = self.prepare({"ntrip_pass": None})
        self.assertNotIn("ntrip_pass", values)                     # None still keeps

    def test_forms_still_refuse_empty_required_fields(self):
        for raw in ({"ntrip_host": ""}, {"ntrip_mount": ""}, {"wifi_ssid": ""}):
            values, code, _message = cfg.prepare_config_change(raw)
            self.assertEqual((values, code), (None, "invalid_configuration"), raw)
        values, _code, _message = cfg.prepare_config_change({"ntrip_pass": ""})
        self.assertNotIn("ntrip_pass", values)                     # blank keeps in forms
