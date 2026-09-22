# SPDX-License-Identifier: AGPL-3.0-only
"""Configuration: Type-casting, loading, saving."""
import json
import os
import tempfile
import unittest
from unittest import mock

from support import cfg
import cfg_store


class TestCoerce(unittest.TestCase):
    """_coerce casts to the default value type. Required, because HTTP-POST delivers everything as a string and can contain older config.json files (V9.0) string ports.
    """

    def test_int_from_string(self):
        # ntrip_port has an int-default.
        self.assertEqual(cfg._coerce("ntrip_port", "2101"), 2101)
        self.assertIsInstance(cfg._coerce("ntrip_port", "2101"), int)

    def test_int_remains_int(self):
        self.assertEqual(cfg._coerce("ntrip_port", 2101), 2101)

    def test_int_falls_to_default_for_garbage(self):
        self.assertEqual(cfg._coerce("ntrip_port", "abc"),
                         cfg.DEFAULT_CONFIG["ntrip_port"])

    def test_int_falls_on_default_for_none(self):
        self.assertEqual(cfg._coerce("ntrip_port", None),
                         cfg.DEFAULT_CONFIG["ntrip_port"])

    def test_bool_from_truth_word(self):
        for wort in ("1", "true", "True", "on", "yes", "YES"):
            self.assertIs(cfg._coerce("wdt_enabled", wort), True, wort)

    def test_bool_from_wrong_word(self):
        for wort in ("0", "false", "off", "no", ""):
            self.assertIs(cfg._coerce("wdt_enabled", wort), False, wort)

    def test_string_remains_string(self):
        self.assertEqual(cfg._coerce("wifi_ssid", "Home"), "Home")

    def test_unknown_key_is_passed_through(self):
        self.assertEqual(cfg._coerce("nonexistent", "value"), "value")


class TestPersistence(unittest.TestCase):
    def setUp(self):
        self.saved = dict(cfg.CONFIG)
        self.tmp = tempfile.mkdtemp(prefix="esp-rtk-cfg-")
        self.cwd = os.getcwd()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self.cwd)
        cfg.CONFIG.clear()
        cfg.CONFIG.update(self.saved)

    def test_store_and_load(self):
        cfg.CONFIG["wifi_ssid"] = "Rundgang"
        cfg.CONFIG["ntrip_port"] = 2102
        self.assertTrue(cfg.save_config())

        cfg.CONFIG["wifi_ssid"] = "vergessen"
        cfg.CONFIG["ntrip_port"] = 1
        cfg.load_config()
        self.assertEqual(cfg.CONFIG["wifi_ssid"], "Rundgang")
        self.assertEqual(cfg.CONFIG["ntrip_port"], 2102)

    def test_the_version_does_not_persist(self):
        # Otherwise, an old config.json pins the old version string after an update.
        cfg.save_config()
        with open("config.json") as fh:
            saved = json.load(fh)
        for key in cfg.PERSIST_EXCLUDE:
            self.assertNotIn(key, saved)

    def test_old_file_with_string_port_will_be_corrected(self):
        with open("config.json", "w") as fh:
            json.dump({"ntrip_port": "2101", "wifi_ssid": "Old"}, fh)
        cfg.load_config()
        self.assertEqual(cfg.CONFIG["ntrip_port"], 2101)
        self.assertIsInstance(cfg.CONFIG["ntrip_port"], int)

    def test_unknown_keys_are_ignored(self):
        with open("config.json", "w") as fh:
            json.dump({"wifi_ssid": "New", "voellig_unbekannt": 1}, fh)
        cfg.load_config()
        self.assertEqual(cfg.CONFIG["wifi_ssid"], "New")
        self.assertNotIn("voellig_unbekannt", cfg.CONFIG)

    def test_broken_file_falls_back_to_defaults(self):
        with open("config.json", "w") as fh:
            fh.write("{ this is not json")
        cfg.CONFIG["wifi_ssid"] = "unchanged"
        cfg.load_config()          # not to throw
        self.assertEqual(cfg.CONFIG["wifi_ssid"], "unchanged")

    def test_missing_file_is_not_an_error(self):
        cfg.load_config()          # not to throw

    def test_interrupted_save_is_restored_from_temporary_file(self):
        with open("config.json.tmp", "w") as fh:
            json.dump({"wifi_ssid": "Recovered"}, fh)
        cfg.load_config()
        self.assertEqual(cfg.CONFIG["wifi_ssid"], "Recovered")
        self.assertTrue(os.path.exists("config.json"))

    def test_atomic_dump_restores_predecessors_at_rename_errors(self):
        with open("config.json", "w") as fh:
            json.dump({"wifi_ssid": "Original"}, fh)
        original_rename = cfg_store.os.rename

        def fail_new_file(source, target):
            if source == "config.json.tmp" and target == "config.json":
                raise OSError("simulated power loss")
            return original_rename(source, target)

        with mock.patch.object(cfg_store.os, "rename", side_effect=fail_new_file):
            self.assertFalse(cfg_store.atomic_dump(
                "config.json", {"wifi_ssid": "New"}, lambda *args: None))
        with open("config.json") as fh:
            self.assertEqual(json.load(fh)["wifi_ssid"], "Original")

    def test_schema_two_migrates_key_and_preserves_credentials(self):
        old = {"config_schema_version": 2,
               "ap_kanalwechsel_intervall_sec": 17,
               "wifi_ssid": "Field network", "wifi_pass": "secret123",
               "ntrip_user": "rover", "ntrip_pass": "caster-secret",
               "ntrip_mount": "SYNTH", "gsv_limit_per_sec": 4}
        with open("config.json", "w") as fh:
            json.dump(old, fh)
        cfg.load_config()
        self.assertEqual(cfg.CONFIG["config_schema_version"], 3)
        self.assertEqual(cfg.CONFIG["ap_channel_switch_interval_sec"], 17)
        self.assertEqual(cfg.CONFIG["wifi_pass"], "secret123")
        self.assertEqual(cfg.CONFIG["ntrip_pass"], "caster-secret")
        self.assertEqual(cfg.CONFIG["gsv_limit_per_sec"], 4)
        with open("config.json") as fh:
            migrated = json.load(fh)
        self.assertNotIn("ap_kanalwechsel_intervall_sec", migrated)

    def test_runtime_history_migration_is_one_time(self):
        for path in (cfg.ERRORLOG_FILE, cfg.ERRORLOG_FILE + ".1",
                     cfg.CONFIG_RESULT_FILE):
            with open(path, "w") as fh:
                fh.write("localized history")
        self.assertTrue(cfg.migrate_v11_runtime_history())
        self.assertTrue(os.path.exists(cfg.LANGUAGE_MIGRATION_MARKER))
        for path in (cfg.ERRORLOG_FILE, cfg.ERRORLOG_FILE + ".1",
                     cfg.CONFIG_RESULT_FILE):
            self.assertFalse(os.path.exists(path))
        with open(cfg.ERRORLOG_FILE, "w") as fh:
            fh.write("new English history")
        self.assertFalse(cfg.migrate_v11_runtime_history())
        self.assertTrue(os.path.exists(cfg.ERRORLOG_FILE))


class TestDefaults(unittest.TestCase):
    def test_no_access_data_in_the_code(self):
        """Access data belongs in config.json, not to the repository."""
        for key in ("wifi_ssid", "wifi_pass", "ntrip_user", "ntrip_pass",
                    "ntrip_fallback_host", "ntrip_fallback_mount",
                    "ntrip_fallback_user", "ntrip_fallback_pass"):
            self.assertEqual(cfg.DEFAULT_CONFIG[key], "",
                             "%s has a value in code" % key)

    def test_defaults_are_a_snapshot(self):
        # DEFAULT_CONFIG serves as a type reference; the keys must be congruent with
        # CONFIG.
        self.assertEqual(set(cfg.DEFAULT_CONFIG), set(cfg.CONFIG))


if __name__ == "__main__":
    unittest.main()
