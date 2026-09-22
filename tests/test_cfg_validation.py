# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for the hardware-independent configuration boundary."""
import unittest

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
