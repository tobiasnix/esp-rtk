# SPDX-License-Identifier: AGPL-3.0-only
"""Configuration: Type-casting, loading, saving."""
import json
import os
import tempfile
import unittest
from unittest import mock

from support import cfg
import cfg_store
from source_checks import called_names, source_tree


class _RecordedFile:
    """Proxy around a real file that records write, flush and close."""

    def __init__(self, fh, path, events, fail_flush):
        self._fh, self._path, self._events = fh, path, events
        self._fail_flush = fail_flush

    def write(self, data):
        self._events.append(("write", self._path))
        return self._fh.write(data)

    def flush(self):
        self._events.append(("flush", self._path))
        self._fh.flush()
        if self._fail_flush is not None:
            raise self._fail_flush

    def read(self, *args):
        return self._fh.read(*args)

    def close(self):
        self._events.append(("close", self._path))
        self._fh.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def record_file_events(test_case, fail_flush=None):
    """Record cfg_store's file writes, flushes, closes and renames in call order.

    Patches cfg_store.open and cfg_store.os.rename for the rest of the test.
    With fail_flush, the proxy's flush() raises that exception after the data
    has been handed to the real file."""
    events = []
    real_open = open
    real_rename = cfg_store.os.rename

    def recording_open(path, *args, **kwargs):
        return _RecordedFile(real_open(path, *args, **kwargs), path, events, fail_flush)

    def recording_rename(source, target):
        events.append(("rename", source, target))
        return real_rename(source, target)

    for patcher in (mock.patch.object(cfg_store, "open", recording_open, create=True),
                    mock.patch.object(cfg_store.os, "rename", recording_rename)):
        patcher.start()
        test_case.addCleanup(patcher.stop)
    return events


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
        cfg._CONFIG_FILE_PROTECTED = False

    def test_store_and_load(self):
        cfg.CONFIG["wifi_ssid"] = "Rundgang"
        cfg.CONFIG["ntrip_port"] = 2102
        self.assertTrue(cfg.save_config())

        cfg.CONFIG["wifi_ssid"] = "vergessen"
        cfg.CONFIG["ntrip_port"] = 1
        cfg.load_config()
        self.assertEqual(cfg.CONFIG["wifi_ssid"], "Rundgang")
        self.assertEqual(cfg.CONFIG["ntrip_port"], 2102)

    def test_arm_writes_the_transaction_but_no_result(self):
        defaults = {"config_file": "config.json", "wifi_ssid": "", "ntrip_host": ""}
        config = dict(defaults, wifi_ssid="Old", ntrip_host="caster")
        dump = lambda target, data: cfg_store.atomic_dump(target, data, mock.Mock())
        self.assertTrue(cfg_store.arm(config, defaults, ("config_file",), {"wifi_ssid": "New"},
                                      "last_good.json", "pending.json", dump, mock.Mock()))
        for path, ssid in (("last_good.json", "Old"), ("pending.json", "New"), ("config.json", "New")):
            with open(path) as fh:
                self.assertEqual(json.load(fh), {"wifi_ssid": ssid, "ntrip_host": "caster"}, path)
        self.assertEqual(config["wifi_ssid"], "New")
        self.assertEqual(sorted(os.listdir(".")), ["config.json", "last_good.json", "pending.json"])
        self.assertFalse(hasattr(cfg_store, "stage"))

    def test_failed_arm_removes_what_it_wrote(self):
        defaults = {"config_file": "config.json", "wifi_ssid": ""}
        for failing in ("last_good.json", "pending.json", "config.json"):
            for name in os.listdir("."):
                os.remove(name)
            config = dict(defaults, wifi_ssid="Old")
            dump = lambda target, data: target != failing and cfg_store.atomic_dump(target, data, mock.Mock())
            self.assertFalse(cfg_store.arm(config, defaults, ("config_file",), {"wifi_ssid": "New"},
                                           "last_good.json", "pending.json", dump, mock.Mock()))
            self.assertEqual(os.listdir("."), [], failing)
            self.assertEqual(config["wifi_ssid"], "Old", failing)

    def _load_text(self, text):
        with open("config.json", "w") as fh:
            fh.write(text)
        cfg.load_config()

    def test_measurement_samples_quality_survives_save_and_reload(self):
        self.assertEqual(cfg.DEFAULT_CONFIG["measurement_samples"], 5)
        for value in ("quality", 1, 10):
            self.assertTrue(cfg.save_config({"measurement_samples": value}))
            cfg.CONFIG["measurement_samples"] = 5
            cfg.load_config()
            self.assertEqual(cfg.CONFIG["measurement_samples"], value)
            self.assertEqual(cfg.CONFIG_WARNINGS, [])

    def test_invalid_measurement_samples_in_file_is_dropped_with_a_warning(self):
        for bad in (2, "Quality", True, 5.5, None):
            with self.subTest(bad=bad):
                cfg.CONFIG["measurement_samples"] = 10              # stale RAM value
                self._load_text(json.dumps({"measurement_samples": bad, "wifi_ssid": "Kept"}))
                self.assertEqual(cfg.CONFIG["measurement_samples"], 5)
                self.assertEqual(cfg.CONFIG["wifi_ssid"], "Kept")
                self.assertEqual([w["key"] for w in cfg.CONFIG_WARNINGS], ["measurement_samples"])

    def test_invalid_port_in_file_falls_back_to_2101_with_a_warning(self):
        for raw in ('70000', '0', '-1', '"abc"', 'null', 'true', '65535.9', '1e999',
                    '-1e999', 'NaN', '1000000000000000000000000000000'):
            with self.subTest(raw=raw):
                self._load_text('{"ntrip_port": %s, "ntrip_fallback_port": %s, "wifi_ssid": "Kept", '
                                '"ntrip_mount": "MOUNT", "gc_interval_ms": 12000, '
                                '"config_schema_version": 3}' % (raw, raw))
                self.assertEqual(cfg.CONFIG["ntrip_port"], 2101)
                self.assertEqual(cfg.CONFIG["ntrip_fallback_port"], 2101)
                self.assertEqual((cfg.CONFIG["wifi_ssid"], cfg.CONFIG["ntrip_mount"],
                                  cfg.CONFIG["gc_interval_ms"]), ("Kept", "MOUNT", 12000))
                self.assertEqual(sorted(w["key"] for w in cfg.CONFIG_WARNINGS),
                                 ["ntrip_fallback_port", "ntrip_port"])
        self._load_text('{"ntrip_port": 70000, "config_schema_version": 3}')
        self.assertIn("between 1 and 65535", cfg.CONFIG_WARNINGS[0]["message"])

    def test_whole_float_port_in_file_loads_as_int(self):
        self._load_text('{"ntrip_port": 2102.0, "config_schema_version": 3}')
        self.assertEqual(cfg.CONFIG["ntrip_port"], 2102)
        self.assertIsInstance(cfg.CONFIG["ntrip_port"], int)
        self.assertEqual(cfg.CONFIG_WARNINGS, [])

    def test_out_of_range_watchdog_timeout_falls_back_with_a_warning(self):
        for raw in ('100', '9999', '"abc"', 'true', 'null', '15000.5', '1e999',
                    '600001', '2147483648'):
            with self.subTest(raw=raw):
                self._load_text('{"wdt_timeout": %s, "wifi_ssid": "Kept", '
                                '"config_schema_version": 3}' % raw)
                self.assertEqual(cfg.CONFIG["wdt_timeout"], 15000)
                self.assertEqual(cfg.CONFIG["wifi_ssid"], "Kept")
                self.assertEqual([w["key"] for w in cfg.CONFIG_WARNINGS], ["wdt_timeout"])
                self.assertIn("10000", cfg.CONFIG_WARNINGS[0]["message"])
                self.assertIn("600000", cfg.CONFIG_WARNINGS[0]["message"])
        self._load_text('{"wdt_timeout": 600000, "config_schema_version": 3}')
        self.assertEqual(cfg.CONFIG["wdt_timeout"], 600000)
        self.assertEqual(cfg.CONFIG_WARNINGS, [])

    def test_non_finite_value_for_an_unvalidated_int_key_falls_back_to_the_default(self):
        # coerce() path (no validator): int(inf) raised OverflowError and aborted the whole load
        self._load_text('{"gc_interval_ms": 1e999, "wifi_ssid": "Kept", "config_schema_version": 3}')
        self.assertEqual(cfg.CONFIG["gc_interval_ms"], cfg.DEFAULT_CONFIG["gc_interval_ms"])
        self.assertEqual(cfg.CONFIG["wifi_ssid"], "Kept")

    def test_non_finite_schema_version_does_not_abort_the_load(self):
        self._load_text('{"config_schema_version": 1e999, "wifi_ssid": "Kept"}')
        self.assertEqual(cfg.CONFIG["wifi_ssid"], "Kept")

    def test_valid_watchdog_timeout_and_ports_load_without_warning(self):
        self._load_text('{"wdt_timeout": 10000, "ntrip_port": 2102, "ntrip_fallback_port": 65535, '
                        '"config_schema_version": 3}')
        self.assertEqual((cfg.CONFIG["wdt_timeout"], cfg.CONFIG["ntrip_port"],
                          cfg.CONFIG["ntrip_fallback_port"]), (10000, 2102, 65535))
        self.assertEqual(cfg.CONFIG_WARNINGS, [])

    def test_field_setup_survives_save_and_reload(self):
        setup = {"antenna_height_m": 1.8, "reference_frame": "ETRS89 (synthetic)",
                 "reference_frame_epoch": "2010.0"}
        self.assertTrue(cfg.save_config(setup))
        cfg.CONFIG.update(antenna_height_m=0.0, reference_frame="", reference_frame_epoch="")
        cfg.load_config()
        for key, value in setup.items():
            self.assertEqual(cfg.CONFIG[key], value, key)

    def test_invalid_field_setup_in_file_is_dropped_with_a_warning(self):
        # The same validator as /api/field-setup: no silent 0.0, no unchecked value.
        for bad in ("abc", -1, 1000, True, None, 3.005):
            with self.subTest(bad=bad):
                cfg.CONFIG["antenna_height_m"] = 1.2        # stale RAM value
                with open("config.json", "w") as fh:
                    json.dump({"antenna_height_m": bad, "wifi_ssid": "Kept"}, fh)
                cfg.load_config()
                self.assertEqual(cfg.CONFIG["antenna_height_m"],
                                 cfg.DEFAULT_CONFIG["antenna_height_m"])
                self.assertEqual(cfg.CONFIG["wifi_ssid"], "Kept")
                self.assertEqual([w["key"] for w in cfg.CONFIG_WARNINGS],
                                 ["antenna_height_m"])
                self.assertIn("antenna_height_m", cfg.CONFIG_WARNINGS[0]["message"])

    def test_invalid_reference_frame_in_file_is_dropped_with_a_warning(self):
        with open("config.json", "w") as fh:
            json.dump({"reference_frame": "x" * 41, "reference_frame_epoch": 2010.0,
                       "antenna_height_m": "1.805"}, fh)
        cfg.load_config()
        self.assertEqual(cfg.CONFIG["reference_frame"], "")
        self.assertEqual(cfg.CONFIG["reference_frame_epoch"], "")
        self.assertEqual(cfg.CONFIG["antenna_height_m"], 1.81)   # same quantising as the API
        self.assertEqual(sorted(w["key"] for w in cfg.CONFIG_WARNINGS),
                         ["reference_frame", "reference_frame_epoch"])
        with open("config.json", "w") as fh:                      # warnings do not pile up
            json.dump({"reference_frame": "x" * 41}, fh)
        cfg.load_config()
        self.assertEqual(len(cfg.CONFIG_WARNINGS), 1)

    def test_rejected_height_stamps_no_antenna_height_until_resaved(self):
        # A default 0.0 would look like a configured zero (ground height = MSL).
        import tracking
        for bad in ("abc", -1, 1000):
            with self.subTest(bad=bad):
                with open("config.json", "w") as fh:
                    json.dump({"antenna_height_m": bad, "config_schema_version": 3}, fh)
                cfg.load_config()
                self.assertIsNone(tracking._setup_stamps()["antenna_height_mm"])
        cfg.CONFIG_WARNINGS[:] = [w for w in cfg.CONFIG_WARNINGS if w["key"] != "antenna_height_m"]
        self.assertEqual(tracking._setup_stamps()["antenna_height_mm"], 0)

    def test_invalid_value_in_pre_v3_file_warns_and_is_overwritten_by_the_migration(self):
        with open("config.json", "w") as fh:
            json.dump({"antenna_height_m": "abc"}, fh)       # no schema version: migrates
        cfg.load_config()
        self.assertEqual([w["key"] for w in cfg.CONFIG_WARNINGS], ["antenna_height_m"])
        with open("config.json") as fh:
            self.assertEqual(json.load(fh)["antenna_height_m"], 0.0)

    def test_stakeout_settings_survive_save_and_reload(self):
        self.assertEqual((cfg.DEFAULT_CONFIG["stakeout_tolerance_m"],
                          cfg.DEFAULT_CONFIG["stakeout_fine_radius_m"]), (0.1, 5.0))
        self.assertTrue(cfg.save_config({"stakeout_tolerance_m": 0.03,
                                         "stakeout_fine_radius_m": 8.0}))
        cfg.CONFIG.update(stakeout_tolerance_m=0.1, stakeout_fine_radius_m=5.0)
        cfg.load_config()
        self.assertEqual((cfg.CONFIG["stakeout_tolerance_m"],
                          cfg.CONFIG["stakeout_fine_radius_m"]), (0.03, 8.0))
        self.assertEqual(cfg.CONFIG_WARNINGS, [])

    def test_invalid_stakeout_setting_in_file_is_dropped_with_a_warning(self):
        self._load_text('{"stakeout_tolerance_m": 9, "wifi_ssid": "Kept", "config_schema_version": 3}')
        self.assertEqual(cfg.CONFIG["stakeout_tolerance_m"], 0.1)
        self.assertEqual(cfg.CONFIG["wifi_ssid"], "Kept")
        self.assertEqual([w["key"] for w in cfg.CONFIG_WARNINGS], ["stakeout_tolerance_m"])

    def test_invalid_stakeout_pair_in_file_falls_back_to_both_defaults(self):
        import cfg_validation
        text = json.dumps({"config_schema_version": 3, "stakeout_tolerance_m": 2.0,
                           "stakeout_fine_radius_m": 1.0, "wifi_ssid": "Kept",
                           "measurement_max_hdop": 1.5})
        with open("config.json", "w") as fh:
            fh.write(text)
        with open("config.json", "rb") as fh:
            before = fh.read()
        with mock.patch.object(cfg, "save_config", wraps=cfg.save_config) as save:
            cfg.load_config()
        self.assertEqual((cfg.CONFIG["stakeout_tolerance_m"],
                          cfg.CONFIG["stakeout_fine_radius_m"]), (0.1, 5.0))
        self.assertEqual((cfg.CONFIG["wifi_ssid"], cfg.CONFIG["measurement_max_hdop"]),
                         ("Kept", 1.5))
        self.assertEqual({w["key"] for w in cfg.CONFIG_WARNINGS},
                         {"stakeout_tolerance_m", "stakeout_fine_radius_m"})
        self.assertEqual(len(cfg.CONFIG_WARNINGS), 2)
        self.assertTrue(all("smaller than" in w["message"] for w in cfg.CONFIG_WARNINGS))
        self.assertEqual(save.call_count, 0)
        with open("config.json", "rb") as fh:
            self.assertEqual(fh.read(), before)
        self.assertIn(cfg_validation.stakeout_pair_error(2.0, 1.0),
                      [w["message"] for w in cfg.CONFIG_WARNINGS])

    def test_migration_saves_a_valid_stakeout_pair(self):
        with open("config.json", "w") as fh:        # no schema version: migrates to 3
            json.dump({"stakeout_tolerance_m": 2.0, "stakeout_fine_radius_m": 1.0,
                       "wifi_ssid": "Kept"}, fh)
        with mock.patch.object(cfg, "save_config", wraps=cfg.save_config) as save:
            cfg.load_config()
            self.assertEqual(save.call_count, 1)
            with open("config.json") as fh:
                stored = json.load(fh)
            self.assertEqual((stored["config_schema_version"], stored["stakeout_tolerance_m"],
                              stored["stakeout_fine_radius_m"], stored["wifi_ssid"]),
                             (3, 0.1, 5.0, "Kept"))
            cfg.load_config()
            self.assertEqual(cfg.CONFIG_WARNINGS, [])
            self.assertEqual(save.call_count, 1)
        with open("config.json", "w") as fh:        # a valid pair survives the migration
            json.dump({"stakeout_tolerance_m": 0.03, "stakeout_fine_radius_m": 8.0}, fh)
        cfg.load_config()
        with open("config.json") as fh:
            stored = json.load(fh)
        self.assertEqual((stored["stakeout_tolerance_m"], stored["stakeout_fine_radius_m"]),
                         (0.03, 8.0))
        self.assertEqual(cfg.CONFIG_WARNINGS, [])

    def test_pair_check_runs_before_the_migration_save(self):
        calls, held, logs = [], [], []
        config = dict(cfg.DEFAULT_CONFIG, config_file="config.json")

        def pair_check(cfg_, warnings):
            calls.append("pair")
            cfg_.update(stakeout_tolerance_m=0.1, stakeout_fine_radius_m=5.0)

        def save():
            calls.append("save")
            held.append((config["stakeout_tolerance_m"], config["stakeout_fine_radius_m"]))

        def run(text, check):
            with open("config.json", "w") as fh:
                fh.write(text)
            return cfg_store.load(config, cfg.DEFAULT_CONFIG, cfg.PERSIST_EXCLUDE, save,
                                  lambda *a: logs.append(a), pair_check=check)

        invalid = '{"stakeout_tolerance_m": 2.0, "stakeout_fine_radius_m": 1.0%s}'
        self.assertTrue(run(invalid % "", pair_check))
        self.assertEqual((calls, held), (["pair", "save"], [(0.1, 5.0)]))
        del calls[:]
        self.assertTrue(run(invalid % ', "config_schema_version": 3', pair_check))
        self.assertEqual(calls, ["pair"])

        def failing(cfg_, warnings):
            raise RuntimeError("boom")
        del logs[:]
        config.update(stakeout_tolerance_m=0.1, stakeout_fine_radius_m=5.0)
        self.assertTrue(run('{"stakeout_tolerance_m": 0.5, "config_schema_version": 3}', failing))
        self.assertEqual(config["stakeout_tolerance_m"], 0.5)
        self.assertEqual([entry[0] for entry in logs if entry[0] == "ERROR"], ["ERROR"])
        self.assertTrue(run('{"stakeout_tolerance_m": 0.7, "config_schema_version": 3}', None))
        self.assertEqual(config["stakeout_tolerance_m"], 0.7)

    def test_stakeout_settings_are_not_settable_through_the_config_endpoint(self):
        self.assertNotIn("stakeout_tolerance_m", cfg.SETTABLE_KEYS)
        self.assertNotIn("stakeout_fine_radius_m", cfg.SETTABLE_KEYS)

    def test_valid_file_has_no_warnings(self):
        with open("config.json", "w") as fh:
            json.dump({"antenna_height_m": 1.8, "reference_frame": "ETRS89"}, fh)
        cfg.load_config()
        self.assertEqual(cfg.CONFIG_WARNINGS, [])

    def test_obsolete_status_token_is_ignored_and_dropped(self):
        with open("config.json", "w") as fh:
            json.dump({"status_token": "old", "wifi_ssid": "New"}, fh)
        cfg.load_config()
        self.assertNotIn("status_token", cfg.CONFIG)
        self.assertNotIn("status_token", cfg.DEFAULT_CONFIG)
        self.assertEqual(cfg.CONFIG["wifi_ssid"], "New")
        self.assertTrue(cfg.save_config())
        with open("config.json") as fh:
            self.assertNotIn("status_token", json.load(fh))

    def test_failed_save_keeps_the_previous_config_values(self):
        cfg.CONFIG["nmea_tcp_auth_required"] = True
        with mock.patch.object(cfg, "_atomic_dump", return_value=False):
            self.assertFalse(cfg.save_config({"nmea_tcp_auth_required": False,
                                              "not_a_default_key": 1}))
        self.assertTrue(cfg.CONFIG["nmea_tcp_auth_required"])
        self.assertNotIn("not_a_default_key", cfg.CONFIG)

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

    def _error_lines(self, log):
        return [c.args[2] for c in log.call_args_list if c.args[0] == "ERROR"]

    def test_valid_json_that_is_no_object_is_treated_as_unreadable(self):
        for raw in ("[]", "5", "null", '"x"', '"wifi_ssid"'):
            with self.subTest(raw=raw):
                with open("config.json", "w") as fh:
                    fh.write(raw)
                cfg.CONFIG["wifi_ssid"] = "unchanged"
                with mock.patch.object(cfg, "log") as log:
                    cfg.load_config()          # not to throw
                self.assertEqual(cfg.CONFIG["wifi_ssid"], "unchanged")
                self.assertTrue(self._error_lines(log), raw)
                with open("config.json") as fh:
                    self.assertEqual(fh.read(), raw)    # never overwritten

    def test_broken_file_is_logged_as_an_error(self):
        with open("config.json", "w") as fh:
            fh.write("{ this is not json")
        with mock.patch.object(cfg, "log") as log:
            cfg.load_config()
        self.assertTrue(self._error_lines(log))

    def test_missing_file_is_logged_as_info_only(self):
        with mock.patch.object(cfg, "log") as log:
            cfg.load_config()
        self.assertEqual(self._error_lines(log), [])

    def test_recovery_eio_loads_what_is_readable(self):
        with open("config.json", "w") as fh:
            fh.write("{ torn")
        with open("config.json.tmp", "w") as fh:
            json.dump({"wifi_ssid": "FromTmp", "config_schema_version": 3}, fh)
        cfg.CONFIG["wifi_ssid"] = "unchanged"
        with mock.patch.object(cfg_store.os, "rename", side_effect=OSError(5, "EIO")), \
                mock.patch.object(cfg, "log") as log:
            cfg.load_config()          # not to throw
        self.assertEqual(cfg.CONFIG["wifi_ssid"], "FromTmp")
        self.assertTrue(any("EIO" in line or "config.json" in line
                            for line in self._error_lines(log)))
        self.assertTrue(os.path.exists("config.json.tmp"))      # left for the next boot

    def test_a_candidate_read_in_place_is_protected_from_automatic_saves(self):
        # Codex P1: only config.json.previous survives and its recovery rename
        # fails; an automatic save (migration, AP password) must not replace it.
        for name in ("config.json", "config.json.tmp"):
            if os.path.exists(name):
                os.remove(name)
        with open("config.json.previous", "w") as fh:
            json.dump({"wifi_ssid": "Prev", "config_schema_version": 2}, fh)   # would migrate
        with mock.patch.object(cfg_store, "recover_atomic", side_effect=OSError(5, "EIO")), \
                mock.patch.object(cfg, "log"):
            cfg.load_config()
        self.assertEqual(cfg.CONFIG["wifi_ssid"], "Prev")
        self.assertTrue(cfg.config_file_protected())
        self.assertFalse(cfg.save_config())                       # automatic writers refused
        with open("config.json.previous") as fh:
            self.assertEqual(json.load(fh)["wifi_ssid"], "Prev")  # the only copy is intact
        self.assertIsNotNone(cfg.stage_config({"ntrip_host": "h.example"}))   # deliberate write
        self.assertFalse(cfg.config_file_protected())
        with open("config.json") as fh:
            saved = json.load(fh)
        self.assertEqual((saved["wifi_ssid"], saved["ntrip_host"]), ("Prev", "h.example"))

    def test_recovery_eio_with_nothing_readable_keeps_the_defaults(self):
        with open("config.json", "w") as fh:
            fh.write("{ torn")
        with open("config.json.previous", "w") as fh:
            fh.write("[]")
        cfg.CONFIG["wifi_ssid"] = "unchanged"
        with mock.patch.object(cfg_store, "recover_atomic", side_effect=OSError(5, "EIO")), \
                mock.patch.object(cfg, "log") as log:
            cfg.load_config()          # not to throw
        self.assertEqual(cfg.CONFIG["wifi_ssid"], "unchanged")
        self.assertGreaterEqual(len(self._error_lines(log)), 2)   # recovery and the unreadable file
        with open("config.json") as fh:
            self.assertEqual(fh.read(), "{ torn")

    def _boot_with_unreadable_file(self, raw, eio=False):
        import net
        with open("config.json", "wb") as fh:
            fh.write(raw)
        cfg.CONFIG["ap_pass"] = ""
        if eio:
            with mock.patch.object(cfg_store, "recover_atomic", side_effect=OSError(5, "EIO")):
                cfg.load_config()
        else:
            cfg.load_config()
        manager = net.NetworkManager.__new__(net.NetworkManager)
        manager.wlan_ap = mock.Mock()
        manager.wlan_ap.config.return_value = b"\x00" * 6
        with mock.patch.object(net, "generate_ap_password", return_value="gnss-new"), \
                mock.patch.object(net, "log") as log:
            manager._ensure_ap_password()                 # the boot path that saved before
        return log

    def test_unreadable_file_is_never_overwritten_by_the_boot(self):            # ruling
        for raw, eio in ((b"{ torn", False), (b"[]", False), (b"null", False),
                         (b"{ torn", True)):
            with self.subTest(raw=raw, eio=eio):
                log = self._boot_with_unreadable_file(raw, eio)
                with open("config.json", "rb") as fh:
                    self.assertEqual(fh.read(), raw)          # byte-identical
                self.assertEqual(cfg.CONFIG["ap_pass"], "gnss-new")   # RAM only, this boot
                self.assertEqual([c.args[0] for c in log.call_args_list], ["WARN"])
                self.assertTrue(cfg.config_file_protected())
                # No other automatic writer (rollback restore, save) gets through either.
                self.assertFalse(cfg.save_config())
                self.assertFalse(cfg._atomic_dump("config.json", {"wifi_ssid": ""}))
                with open("config.json", "rb") as fh:
                    self.assertEqual(fh.read(), raw)

    def test_a_deliberate_configuration_write_releases_the_unreadable_file(self):
        self._boot_with_unreadable_file(b"{ torn")
        self.assertFalse(cfg.apply_live({"antenna_height_m": 1.5}))     # a live PUT: refused
        self.assertTrue(cfg.config_file_protected())
        self.assertIsNotNone(cfg.stage_config({"ntrip_host": "new.example"}))   # PUT /api/config
        self.assertFalse(cfg.config_file_protected())
        with open("config.json") as fh:
            self.assertEqual(json.load(fh)["ntrip_host"], "new.example")

    def test_saving_the_unchanged_fallback_repairs_the_unreadable_file(self):
        self._boot_with_unreadable_file(b"{ torn")
        host = cfg.CONFIG["ntrip_host"]                    # the fallback the page shows
        result = cfg.stage_config({"ntrip_host": host, "antenna_height_m": cfg.CONFIG["antenna_height_m"]})
        self.assertEqual(result["applied"], ["antenna_height_m", "ntrip_host"])
        self.assertFalse(cfg.config_file_protected())
        with open("config.json") as fh:
            self.assertEqual(json.load(fh)["ntrip_host"], host)
        self.assertTrue(cfg.apply_live({"antenna_height_m": 1.5}))   # live PUTs work again

    def test_a_readable_or_missing_file_is_not_protected(self):
        cfg.load_config()                                  # missing
        self.assertFalse(cfg.config_file_protected())
        with open("config.json", "w") as fh:
            json.dump({"wifi_ssid": "Field", "config_schema_version": 3}, fh)
        cfg.load_config()
        self.assertFalse(cfg.config_file_protected())
        self.assertTrue(cfg.save_config())

    def test_atomic_dump_keeps_an_invalid_target_as_unreadable(self):          # I-A
        log = mock.Mock()
        with open("config.json.unreadable", "w") as fh:
            fh.write("older copy")
        for raw in (b"{ torn", b"[]"):
            with self.subTest(raw=raw):
                with open("config.json", "wb") as fh:
                    fh.write(raw)
                self.assertTrue(cfg_store.atomic_dump("config.json", {"wifi_ssid": "A"}, log))
                with open("config.json.unreadable", "rb") as fh:
                    self.assertEqual(fh.read(), raw)          # the original, for a manual rescue
                with open("config.json") as fh:
                    self.assertEqual(json.load(fh), {"wifi_ssid": "A"})

    def test_a_failed_write_puts_the_unreadable_target_back(self):              # M-1
        with open("config.json", "wb") as fh:
            fh.write(b"{ torn")
        real = cfg_store.os.rename

        def failing(src, dst):
            if src.endswith(".tmp"):
                raise OSError(5, "EIO")
            return real(src, dst)
        with mock.patch.object(cfg_store.os, "rename", side_effect=failing):
            self.assertFalse(cfg_store.atomic_dump("config.json", {"wifi_ssid": "A"}, mock.Mock()))
        with open("config.json", "rb") as fh:
            self.assertEqual(fh.read(), b"{ torn")
        self.assertFalse(os.path.exists("config.json.unreadable"))

    def test_a_failed_put_back_never_keeps_the_failed_write(self):
        for original in (b"{ torn", b'{"wifi_ssid": "Old"}'):
            with self.subTest(original=original):
                for name in ("config.json.tmp", "config.json.previous", "config.json.unreadable"):
                    if os.path.exists(name):
                        os.remove(name)
                with open("config.json", "wb") as fh:
                    fh.write(original)

                def failing(src, dst):
                    # The final rename and the put-back both fail: no target remains.
                    if dst == "config.json":
                        raise OSError(5, "EIO")
                    return os.replace(src, dst)
                with mock.patch.object(cfg_store.os, "rename", side_effect=failing):
                    self.assertFalse(cfg_store.atomic_dump("config.json", {"wifi_ssid": "A"},
                                                           mock.Mock()))
                self.assertFalse(os.path.exists("config.json"))
                self.assertFalse(os.path.exists("config.json.tmp"))   # the failed write never survives
                if original.startswith(b"{ torn"):
                    with open("config.json.unreadable", "rb") as fh:  # the original, for a rescue
                        self.assertEqual(fh.read(), original)
                else:
                    with open("config.json.previous") as fh:          # the old file wins
                        self.assertEqual(json.load(fh), {"wifi_ssid": "Old"})
                    cfg_store.recover_atomic("config.json")
                    with open("config.json") as fh:
                        self.assertEqual(json.load(fh), {"wifi_ssid": "Old"})

    def test_a_failed_new_file_leaves_no_tmp(self):
        # A file that did not exist before (e.g. config.pending.json at arm) must
        # not leave a recoverable .tmp behind, or the next recovery would promote
        # a change the caller reported as failed.
        for name in ("config.pending.json", "config.pending.json.tmp", "config.pending.json.previous"):
            if os.path.exists(name):
                os.remove(name)

        def failing(src, dst):
            if dst == "config.pending.json":
                raise OSError(5, "EIO")
            return os.replace(src, dst)
        with mock.patch.object(cfg_store.os, "rename", side_effect=failing):
            self.assertFalse(cfg_store.atomic_dump("config.pending.json", {"wifi_ssid": "A"},
                                                   mock.Mock()))
        self.assertFalse(os.path.exists("config.pending.json.tmp"))
        self.assertFalse(os.path.exists("config.pending.json"))

    def test_atomic_dump_without_an_invalid_target_makes_no_unreadable_copy(self):
        log = mock.Mock()
        self.assertTrue(cfg_store.atomic_dump("config.json", {"wifi_ssid": "A"}, log))  # missing
        self.assertTrue(cfg_store.atomic_dump("config.json", {"wifi_ssid": "B"}, log))  # valid
        self.assertFalse(os.path.exists("config.json.unreadable"))

    def test_a_deliberate_write_over_an_unreadable_file_keeps_the_original(self):
        raw = b"{ torn"
        self._boot_with_unreadable_file(raw)
        self.assertIsNotNone(cfg.stage_config({"ntrip_host": "new.example"}))
        with open("config.json.unreadable", "rb") as fh:
            self.assertEqual(fh.read(), raw)

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


class TestAtomicDumpDurability(unittest.TestCase):
    """The temporary file is flushed before it is closed and renamed; failures
    while dumping or flushing leave the predecessor untouched."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="esp-rtk-cfg-")
        self.cwd = os.getcwd()
        os.chdir(self.tmp)
        with open("x.json", "w") as fh:
            json.dump({"a": 0}, fh)

    def tearDown(self):
        os.chdir(self.cwd)

    def assert_failed_cleanly(self, result, events, log):
        self.assertFalse(result)
        with open("x.json") as fh:
            self.assertEqual(json.load(fh), {"a": 0})
        self.assertNotIn(("rename", "x.json.tmp", "x.json"), events)
        self.assertFalse(os.path.exists("x.json.tmp"))
        self.assertEqual(log.call_count, 1)
        self.assertEqual(log.call_args[0][0], "ERROR")
        self.assertIn("Saving x.json failed", log.call_args[0][2])

    def test_tmp_is_flushed_before_close_and_rename(self):
        events = record_file_events(self)
        self.assertTrue(cfg_store.atomic_dump("x.json", {"a": 1}, lambda *a: None))
        tmp = "x.json.tmp"
        writes = [i for i, e in enumerate(events) if e == ("write", tmp)]
        self.assertIn(("flush", tmp), events)
        flush = events.index(("flush", tmp))
        close = events.index(("close", tmp))
        rename = events.index(("rename", tmp, "x.json"))
        self.assertTrue(writes)
        self.assertLess(max(writes), flush)
        self.assertLess(flush, close)
        self.assertLess(close, rename)
        with open("x.json") as fh:
            self.assertEqual(json.load(fh), {"a": 1})

    def test_partially_written_dump_fails_cleanly(self):
        events = record_file_events(self)
        log = mock.Mock()

        def write_part_then_fail(data, fh):
            fh.write('{"a": ')
            raise OSError(28, "no space")

        with mock.patch.object(cfg_store.ujson, "dump", side_effect=write_part_then_fail):
            result = cfg_store.atomic_dump("x.json", {"a": 1}, log)
        self.assertIn(("write", "x.json.tmp"), events)
        self.assert_failed_cleanly(result, events, log)

    def test_failing_flush_fails_cleanly(self):
        events = record_file_events(self, fail_flush=OSError(5, "EIO"))
        log = mock.Mock()
        result = cfg_store.atomic_dump("x.json", {"a": 1}, log)
        self.assert_failed_cleanly(result, events, log)
        self.assertIn(("flush", "x.json.tmp"), events)
        self.assertLess(events.index(("flush", "x.json.tmp")),
                        events.index(("close", "x.json.tmp")))

    def test_no_os_sync_call(self):
        names = called_names(source_tree(cfg_store))
        for forbidden in ("os.sync", "sync", "fsync"):
            self.assertNotIn(forbidden, names)


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


class TestApplyPolicy(unittest.TestCase):
    def test_every_setting_has_exactly_one_policy(self):
        self.assertEqual(set(cfg.APPLY_POLICY), set(cfg.DEFAULT_CONFIG) - {"version", "config_file"})
        self.assertEqual(set(cfg.APPLY_POLICY.values()),
                         {"live", "task_restart", "verified_reboot"})
        self.assertEqual(set(cfg.VERIFIED_REBOOT_KEYS),
                         {k for k, p in cfg.APPLY_POLICY.items() if p == "verified_reboot"})

    def test_class_sizes_are_regression_anchors(self):
        sizes = {}
        for policy in cfg.APPLY_POLICY.values():
            sizes[policy] = sizes.get(policy, 0) + 1
        self.assertEqual(sizes, {"live": 40, "task_restart": 11, "verified_reboot": 23})
        self.assertEqual(len(cfg.VERIFIED_REBOOT_KEYS), 23)

    def test_access_paths_are_verified_and_casters_restart_the_task(self):
        for key in ("wifi_ssid", "wifi_pass", "wifi_networks", "ap_ssid", "ap_pass",
                    "ap_ip", "hostname", "http_port", "wdt_timeout", "nmea_tcp_port"):
            self.assertEqual(cfg.APPLY_POLICY[key], "verified_reboot", key)
        for key in cfg.SETTABLE_KEYS:
            if not key.startswith("wifi_"):
                self.assertEqual(cfg.APPLY_POLICY[key], "task_restart", key)

    def test_runtime_read_keys_are_live(self):
        import tracking
        for key in ("ntrip_max_epoch_age_sec", "ntrip_reconnect_on_stale_sec",
                    "gps_utc_offset_sec", "fix_stale_after_ms", "reference_frame",
                    "reference_frame_epoch", "antenna_height_m", "nmea_tcp_auth_required",
                    "ntrip_version", "log_level", "measurement_samples",
                    "stakeout_tolerance_m", "stakeout_fine_radius_m") + tuple(tracking.MEASUREMENT_GATE_KEYS):
            self.assertEqual(cfg.APPLY_POLICY[key], "live", key)


if __name__ == "__main__":
    unittest.main()
