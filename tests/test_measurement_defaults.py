# SPDX-License-Identifier: AGPL-3.0-only
"""Device-wide measurement defaults: the method beside the gate (#24b)."""
import asyncio
import copy
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from support import cfg, state, web
import tracking
import web_routing
from test_gate_consistency import GGA, GST
from test_access_v11 import FakeHttpWriter
from test_live_snapshot import FakeWriter


def call_gate(method, payload=None, dump=True):
    """(status, body) of /api/measurement-gate with config writes mocked."""
    body = b"" if payload is None else json.dumps(payload).encode()
    writer = FakeWriter()
    with mock.patch.object(web, "_admin_allowed", return_value=True), \
            mock.patch.object(cfg, "_atomic_dump", return_value=dump):
        asyncio.run(web._handle_measurement_gate_request(writer, method, body, False, {}))
    head, _sep, answer = bytes(writer.buf).partition(b"\r\n\r\n")
    return int(head.split(b" ", 2)[1]), json.loads(answer)


class TestMeasurementMethodEndpoint(unittest.TestCase):
    def setUp(self):
        self.saved = dict(cfg.CONFIG)
        self.saved_warnings = list(cfg.CONFIG_WARNINGS)
        for key in tracking.MEASUREMENT_GATE_KEYS + ("measurement_samples",):
            cfg.CONFIG[key] = cfg.DEFAULT_CONFIG[key]
        self.assertEqual(cfg.CONFIG["measurement_min_satellites"], 10)

    def tearDown(self):
        cfg.CONFIG.clear()
        cfg.CONFIG.update(self.saved)
        cfg.CONFIG_WARNINGS[:] = self.saved_warnings
        cfg._CONFIG_FILE_PROTECTED = False

    def test_get_reports_the_method_beside_the_unchanged_gate(self):
        status, body = call_gate("GET")
        self.assertEqual(status, 200)
        self.assertEqual((body["samples"], body["samples_default"]), (5, 5))
        self.assertEqual(set(body["values"]), set(tracking.measurement_gate()))
        self.assertNotIn("samples", body["values"])
        self.assertNotIn("samples", body["defaults"])

    def test_method_only_put_keeps_gate_profile_and_verdict(self):
        gate, deviations = tracking.measurement_gate(), tracking.measurement_gate_deviations()
        with mock.patch.object(web.app, "reevaluate_gate") as reevaluate:
            status, body = call_gate("PUT", {"samples": "quality"})
        self.assertEqual((status, body["samples"]), (200, "quality"))
        self.assertEqual(cfg.CONFIG["measurement_samples"], "quality")
        self.assertEqual((tracking.measurement_gate(), body["deviations"]), (gate, deviations))
        reevaluate.assert_not_called()

    def test_old_full_gate_put_keeps_the_method(self):
        cfg.CONFIG["measurement_samples"] = 10
        with mock.patch.object(web.app, "reevaluate_gate") as reevaluate:
            status, body = call_gate("PUT", dict(tracking.measurement_gate(), min_satellites=12))
        self.assertEqual((status, body["samples"], body["values"]["min_satellites"]), (200, 10, 12))
        reevaluate.assert_called_once()

    def test_full_gate_with_method_saves_both_in_one_write(self):
        self.assertNotEqual(cfg.CONFIG["measurement_min_satellites"], 12)
        with mock.patch.object(cfg, "save_config", wraps=cfg.save_config) as save, \
                mock.patch.object(web.app, "reevaluate_gate") as reevaluate:
            status, body = call_gate("PUT", dict(tracking.measurement_gate(), min_satellites=12,
                                                 samples=1))
        self.assertEqual((status, body["samples"], body["values"]["min_satellites"]), (200, 1, 12))
        self.assertEqual(save.call_count, 1)
        changes = save.call_args[0][0]
        self.assertEqual((changes["measurement_min_satellites"], changes["measurement_samples"]),
                         (12, 1))
        self.assertEqual((cfg.CONFIG["measurement_min_satellites"], cfg.CONFIG["measurement_samples"]),
                         (12, 1))
        reevaluate.assert_called_once()

    def test_failed_combined_write_changes_neither_rule_nor_method(self):
        before = (cfg.CONFIG["measurement_min_satellites"], cfg.CONFIG["measurement_samples"])
        with mock.patch.object(web.app, "reevaluate_gate") as reevaluate:
            status, body = call_gate("PUT", dict(tracking.measurement_gate(), min_satellites=12,
                                                 samples=1), dump=False)
        self.assertEqual((status, body["error"]), (500, "save_failed"))
        self.assertEqual((cfg.CONFIG["measurement_min_satellites"], cfg.CONFIG["measurement_samples"]),
                         before)
        reevaluate.assert_not_called()

    def test_invalid_method_or_partial_gate_is_400_and_changes_nothing(self):
        before = dict(cfg.CONFIG)
        for payload in ({"samples": 2}, {"samples": True}, {"samples": "Quality"}, {},
                        {"min_satellites": 6, "samples": 5}, {"samples": 5, "extra": 1},
                        dict(tracking.measurement_gate(), samples=7), [5], "x"):
            status, body = call_gate("PUT", payload)
            with self.subTest(payload=payload):
                self.assertEqual((status, body["error"]), (400, "invalid_measurement_gate"))
        self.assertEqual(cfg.CONFIG, before)

    def test_delete_resets_only_the_rules(self):
        cfg.CONFIG.update(measurement_samples="quality", measurement_min_satellites=20)
        with mock.patch.object(web.app, "reevaluate_gate"):
            status, body = call_gate("DELETE")
        self.assertEqual((status, body["reset"], body["samples"]), (200, True, "quality"))
        self.assertEqual(body["values"], tracking.measurement_gate(True))

    def test_failed_write_restores_the_method(self):
        status, body = call_gate("PUT", {"samples": 10}, dump=False)
        self.assertEqual((status, body["error"]), (500, "save_failed"))
        self.assertEqual(cfg.CONFIG["measurement_samples"], 5)

    def test_method_put_is_refused_while_config_json_is_protected(self):
        cfg._CONFIG_FILE_PROTECTED = True
        status, body = call_gate("PUT", {"samples": 10})
        self.assertEqual((status, body["error"]), (503, "config_file_unreadable"))
        self.assertEqual(cfg.CONFIG["measurement_samples"], 5)


class TestMeasurementDefaultsInTheSnapshot(unittest.TestCase):
    def setUp(self):
        self.saved = dict(cfg.CONFIG)
        self.saved_warnings = list(cfg.CONFIG_WARNINGS)
        for key in tracking.MEASUREMENT_GATE_KEYS + ("measurement_samples",):
            cfg.CONFIG[key] = cfg.DEFAULT_CONFIG[key]

    def tearDown(self):
        cfg.CONFIG.clear()
        cfg.CONFIG.update(self.saved)
        cfg.CONFIG_WARNINGS[:] = self.saved_warnings
        cfg._CONFIG_FILE_PROTECTED = False

    def test_snapshot_reports_the_current_device_defaults(self):
        cfg.CONFIG.update(measurement_samples=10, antenna_height_m=1.8,
                          reference_frame="ETRS89", reference_frame_epoch="2016.0")
        first, second = web.ui_live_snapshot(), web.ui_live_snapshot()   # two browsers
        expected = {"samples": 10, "antenna_height_m": 1.8,
                    "reference_frame": "ETRS89", "reference_frame_epoch": "2016.0"}
        self.assertEqual(first["measurement_defaults"], expected)
        self.assertEqual(second["measurement_defaults"], expected)
        self.assertNotIn("samples", first["current_quality"]["limits"] or {})

    def test_snapshot_follows_a_method_change_at_once(self):
        call_gate("PUT", {"samples": "quality"})
        self.assertEqual(web.ui_live_snapshot()["measurement_defaults"]["samples"], "quality")

    def test_snapshot_defaults_are_detached_copies(self):
        defaults = web.ui_live_snapshot()["measurement_defaults"]
        defaults["samples"] = 1
        self.assertEqual(cfg.CONFIG["measurement_samples"], 5)



class _ConfigCase(unittest.TestCase):
    """A device with Wi-Fi "Old" in a temporary directory (as TestTransactionalConfiguration)."""

    def setUp(self):
        self.cwd = os.getcwd()
        self.tmp = tempfile.mkdtemp(prefix="esp-rtk-24b-")
        os.chdir(self.tmp)
        self.saved = dict(cfg.CONFIG)
        self.saved_access_state = state.app.stats.get("access_state")
        state.app.stats["access_state"] = "ONLINE"
        cfg._ROLLBACK_OPEN, cfg._ROLLBACK_CODE, cfg._STAGING_UNRECOVERED = False, None, False
        cfg._STAGING_LOG.reset()
        cfg._CONFIG_FILE_PROTECTED = False
        cfg.discard_staged_config()
        cfg.CONFIG["wifi_ssid"] = "Old"
        cfg.CONFIG["wifi_pass"] = "legacy-password"
        cfg.save_config()

    def tearDown(self):
        cfg.CONFIG.clear()
        cfg.CONFIG.update(self.saved)
        cfg._ROLLBACK_OPEN, cfg._ROLLBACK_CODE, cfg._STAGING_UNRECOVERED = False, None, False
        cfg.discard_staged_config()
        state.app.stats["access_state"] = self.saved_access_state
        cfg._CONFIG_FILE_PROTECTED = False
        del cfg.CONFIG_WARNINGS[:]
        os.chdir(self.cwd)

    def call(self, path, method, payload):
        """(status, body, reboots) of one /api/config* request with the reboot mocked."""
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        writer, reboots = FakeHttpWriter(), []

        async def fake_reboot():
            reboots.append(path)
        with mock.patch.object(web, "_admin_allowed", return_value=True), \
                mock.patch.object(web, "do_soft_reboot_deferred", fake_reboot):
            asyncio.run(web._handle_config_request(writer, path, method, "", body, False, {}))
        head, _sep, answer = bytes(writer.data).partition(b"\r\n\r\n")
        return int(head.split(b" ", 2)[1]), json.loads(answer), reboots

    def call_restore(self, payload):
        return self.call("/api/config/restore", "POST", payload)

    def call_config_put(self, values):
        return self.call("/api/config", "PUT", values)

    def own_backup(self, **config):
        """The backup this device writes with `config` set."""
        cfg.CONFIG.update(config)
        return json.loads(json.dumps(web.config_backup()))

    def target(self, **config):
        """Turn the device into the restore target: Wi-Fi "Old" plus `config`."""
        cfg.CONFIG["wifi_ssid"] = "Old"
        cfg.CONFIG["wifi_pass"] = "legacy-password"
        cfg.CONFIG.update(config)
        cfg.save_config()

    def boot(self, works=("Old",)):
        # As TestTransactionalConfiguration.boot: RAM gone, load, resolve, then the
        # verdict; only SSIDs in `works` with "legacy-password" connect.
        cfg.discard_staged_config()
        cfg._ROLLBACK_OPEN, cfg._ROLLBACK_CODE, cfg._STAGING_UNRECOVERED = False, None, False
        cfg.load_config()
        cfg.resolve_interrupted_apply()
        if cfg.rollback_incomplete():
            return
        if cfg.pending_config_active():
            if cfg.CONFIG["wifi_ssid"] in works and cfg.CONFIG["wifi_pass"] == "legacy-password":
                cfg.commit_pending_config(cfg.CONFIG["wifi_ssid"])
            else:
                cfg.rollback_pending_config("authentication_failed")


CASTER = {"ntrip_enabled": True, "ntrip_host": "caster.example", "ntrip_mount": "MOUNT",
          "ntrip_user": "user", "ntrip_pass": "secret-1"}


class TestRestoreOfEmptyStates(_ConfigCase):
    """Every backup the device writes itself can be restored (#24b, E10)."""

    def test_backup_with_ntrip_switched_off_restores(self):
        backup = self.own_backup(wifi_ssid="SiteNet", ntrip_enabled=False, ntrip_host="",
                                 ntrip_mount="", ntrip_user="", ntrip_pass="")
        self.target(**CASTER)
        status, body, reboots = self.call_restore(backup)
        self.assertEqual((status, body["kept"], reboots), (202, [], ["/api/config/restore"]))
        self.assertEqual((cfg.CONFIG["ntrip_enabled"], cfg.CONFIG["ntrip_host"],
                          cfg.CONFIG["ntrip_mount"], cfg.CONFIG["ntrip_pass"]), (False, "", "", ""))
        with open(cfg.PENDING_CONFIG_FILE) as fh:
            self.assertEqual(json.load(fh)["wifi_ssid"], "SiteNet")

    def test_backup_without_a_caster_restores(self):
        backup = self.own_backup(wifi_ssid="SiteNet", ntrip_enabled=True, ntrip_host="",
                                 ntrip_mount="")
        self.target(**CASTER)
        status, body, _reboots = self.call_restore(backup)
        self.assertEqual(status, 202, body)
        self.assertEqual((cfg.CONFIG["ntrip_enabled"], cfg.CONFIG["ntrip_host"]), (True, ""))

    def test_backup_without_primary_ssid_restores_over_the_fallbacks(self):
        backup = self.own_backup(wifi_ssid="", wifi_pass="",
                                 wifi_networks=[["Field", "fallback12"]])
        status, body, _reboots = self.call_restore(backup)
        self.assertEqual((status, body["kept"]), (202, []))
        with open(cfg.PENDING_CONFIG_FILE) as fh:
            pending = json.load(fh)
        self.assertEqual((pending["wifi_ssid"], pending["wifi_networks"]),
                         ("", [["Field", "fallback12"]]))

    def test_factory_state_backup_restores_and_keeps_the_target_wifi(self):
        for key in cfg.SETTABLE_KEYS + ("wifi_networks",):
            cfg.CONFIG[key] = copy.deepcopy(cfg.DEFAULT_CONFIG[key])
        backup = json.loads(json.dumps(web.config_backup()))
        self.target(wifi_ssid="Old", wifi_pass="legacy-password",
                    wifi_networks=[["Spare", "spare-pass"]], **CASTER)
        status, body, reboots = self.call_restore(backup)
        self.assertEqual((status, body["kept"], reboots), (202, ["wifi"], ["/api/config/restore"]))
        self.assertEqual((cfg.CONFIG["wifi_ssid"], cfg.CONFIG["wifi_pass"],
                          cfg.CONFIG["wifi_networks"]),
                         ("Old", "legacy-password", [["Spare", "spare-pass"]]))
        self.assertEqual((cfg.CONFIG["ntrip_host"], cfg.CONFIG["ntrip_mount"]), ("", ""))
        self.boot(works=("Old",))
        self.assertEqual(cfg.config_result_read()["status"], "connected")

    def test_config_form_still_refuses_an_empty_caster(self):
        status, payload, reboots = self.call_config_put({"ntrip_host": "", "ntrip_mount": ""})
        self.assertEqual((status, payload["error"], reboots), (400, "invalid_configuration", []))


FIELD = {"antenna_height_m": 1.85, "reference_frame": "ETRS89 (synthetic)",
         "reference_frame_epoch": "2016.0"}
GATE = dict(tracking.measurement_gate(True), min_satellites=12, max_hdop=1.2)
STRICT = dict(GATE, min_satellites=30)


class _BackupCase(_ConfigCase):
    """Device A writes a schema-2 backup; the target holds the factory defaults."""

    def device_a(self, gate=GATE):
        cfg.CONFIG.update(dict(("measurement_" + k, v) for k, v in gate.items()))
        cfg.CONFIG.update(FIELD, measurement_samples="quality", wifi_ssid="SiteNet")
        return json.loads(json.dumps(web.config_backup()))

    def factory_measurement(self):
        cfg.CONFIG.update(dict(("measurement_" + k, v)
                               for k, v in tracking.measurement_gate(True).items()))
        cfg.CONFIG.update(measurement_samples=5, antenna_height_m=0.0, reference_frame="",
                          reference_frame_epoch="", wifi_ssid="Old")
        cfg.save_config()

    def as_schema_2(self, backup):
        """The same backup as a schema-2 document (without the stakeout section)."""
        backup.pop("stakeout")
        backup["schema_version"] = 2
        return backup


class TestConfigBackupSchema2(_BackupCase):
    """The backup carries the measurement rules, the method and the field setup (#24b)."""

    def test_backup_carries_rules_method_and_field_setup(self):
        backup = self.device_a()
        self.assertEqual((backup["schema_version"], backup["kind"]), (3, "esp-rtk-config-backup"))
        self.assertEqual(backup["measurement"], {"gate": GATE, "samples": "quality"})
        self.assertEqual(backup["field_setup"], FIELD)
        self.assertEqual(backup["config"]["wifi_ssid"], "SiteNet")
        self.assertIn("credentials", backup)

    def test_replacement_device_receives_the_same_defaults(self):
        backup = self.as_schema_2(self.device_a())
        profile = tracking._profile_id(tracking.measurement_gate())
        self.factory_measurement()
        with mock.patch.object(web.app, "reevaluate_gate") as reevaluate:
            status, body, reboots = self.call_restore(backup)
        self.assertEqual((status, body), (202, {"restored": True, "schema_version": 2,
            "sections": ["connections", "measurement", "field_setup"], "kept": [],
            "apply_id": None}))
        self.assertEqual(reboots, ["/api/config/restore"])
        self.assertEqual(tracking.measurement_gate(), GATE)
        self.assertEqual(tracking._profile_id(tracking.measurement_gate()), profile)
        self.assertEqual((tracking.field_setup(), cfg.CONFIG["measurement_samples"]),
                         (FIELD, "quality"))
        with open("config.json") as fh:
            saved = json.load(fh)
        self.assertEqual((saved["measurement_samples"], saved["antenna_height_m"]),
                         ("quality", 1.85))
        with open(cfg.PENDING_CONFIG_FILE) as fh:
            self.assertEqual(json.load(fh)["wifi_ssid"], "SiteNet")
        reevaluate.assert_called_once()

    def test_schema_1_backup_restores_and_leaves_the_field_defaults(self):
        cfg.CONFIG.update(FIELD, measurement_samples=10, measurement_min_satellites=14)
        cfg.save_config()
        old = {"schema_version": 1, "kind": "esp-rtk-config-backup",
               "config": {"wifi_ssid": "Restored", "ntrip_host": "r.example"},
               "measurement": {"gate": {}, "samples": 2}}              # ignored in schema 1
        with mock.patch.object(web.app, "reevaluate_gate") as reevaluate:
            status, body, _ = self.call_restore(old)
        self.assertEqual((status, body["schema_version"], body["sections"]),
                         (202, 1, ["connections"]))
        self.assertEqual((tracking.field_setup(), cfg.CONFIG["measurement_samples"],
                          cfg.CONFIG["measurement_min_satellites"]), (FIELD, 10, 14))
        reevaluate.assert_not_called()

    def test_incomplete_or_invalid_schema_2_is_refused_without_any_write(self):
        good = self.as_schema_2(self.device_a())
        self.factory_measurement()
        before = dict(cfg.CONFIG)
        with open("config.json") as fh:
            file_before = fh.read()

        def variant(change):
            backup = copy.deepcopy(good)
            change(backup)
            return backup
        cases = (
            lambda b: b.pop("measurement"), lambda b: b.pop("field_setup"),
            lambda b: b.update(measurement=[1]), lambda b: b["measurement"].update(extra=1),
            lambda b: b["measurement"]["gate"].pop("max_hdop"),
            lambda b: b["measurement"]["gate"].update(min_satellites=2),
            lambda b: b["measurement"].update(samples=2),
            lambda b: b["measurement"].update(samples=True),
            lambda b: b["field_setup"].update(antenna_height_m=5),
            lambda b: b["field_setup"].pop("reference_frame_epoch"),
            lambda b: b["field_setup"].update(reference_frame="x" * 41))
        cases += tuple((lambda version: lambda b: b.update(schema_version=version))(v)
                       for v in ([], {}, True, False, 1.0, 2.0, "2", None, 3, 4, 0))
        cases += (lambda b: b.pop("schema_version"),)
        for index, change in enumerate(cases):
            status, body, reboots = self.call_restore(variant(change))
            with self.subTest(case=index):
                self.assertEqual((status, body["error"], reboots), (400, "invalid_backup", []))
                self.assertEqual(cfg.CONFIG, before)
                self.assertFalse(cfg.pending_config_active())
                with open("config.json") as fh:
                    self.assertEqual(fh.read(), file_before)

    def test_refused_restore_touches_no_field_default(self):
        backup = self.device_a()
        self.factory_measurement()
        before = dict(cfg.CONFIG)
        state.app.stats["access_state"] = "CONNECTING"
        status, body, reboots = self.call_restore(backup)
        self.assertEqual((status, body["error"], reboots), (409, "config_verification_running", []))
        state.app.stats["access_state"] = "ONLINE"
        cfg.stage_config({"wifi_ssid": "First"})
        self.assertTrue(cfg.apply_staged_config())
        status, body, reboots = self.call_restore(backup)
        self.assertEqual((status, body["error"], reboots), (409, "open_change_pending", []))
        for key in ("measurement_samples", "measurement_min_satellites", "antenna_height_m"):
            self.assertEqual(cfg.CONFIG[key], before[key], key)

    def test_validate_backup_names_the_section_and_checks_the_schema_type(self):
        backup = self.device_a()
        backup["field_setup"]["antenna_height_m"] = 5
        values, error = web.validate_backup(backup)
        self.assertIsNone(values)
        self.assertTrue(error.startswith("field_setup:"), error)
        refusal = (None, "backup_schema_version must be 1, 2 or 3")
        for version in ([], {}, True, False, 1.0, 2.0, 3.0, "3", None, 4, 9):
            with self.subTest(version=version):
                self.assertEqual(web.validate_backup({"schema_version": version}), refusal)
        self.assertEqual(web.validate_backup({}), refusal)
        self.assertEqual(web.validate_backup([2]), refusal)


STAKEOUT = {"stakeout_tolerance_m": 0.03, "stakeout_fine_radius_m": 8.0}
FACTORY_STAKEOUT = {"stakeout_tolerance_m": 0.1, "stakeout_fine_radius_m": 5.0}
SECTIONS_3 = ["connections", "measurement", "field_setup", "stakeout"]


class TestConfigBackupSchema3(_BackupCase):
    """Schema 3 adds the stakeout tolerance and fine radius (stakeout WP2, E13)."""

    def stakeout_values(self):
        return dict((key, cfg.CONFIG[key]) for key in STAKEOUT)

    def load_with(self, **values):
        """Boot from a current config.json that holds `values`; returns the warnings."""
        with open("config.json") as fh:
            stored = json.load(fh)
        stored.update(values)
        with open("config.json", "w") as fh:
            json.dump(stored, fh)
        cfg.load_config()
        return [warning["key"] for warning in cfg.CONFIG_WARNINGS]

    def test_backup_carries_stakeout_settings(self):
        cfg.CONFIG.update(STAKEOUT)
        backup = self.device_a()
        self.assertEqual(backup["schema_version"], 3)
        self.assertEqual(backup["stakeout"], {"tolerance_m": 0.03, "fine_radius_m": 8.0})
        self.assertEqual((backup["measurement"], backup["field_setup"]),
                         ({"gate": GATE, "samples": "quality"}, FIELD))
        self.assertEqual(backup["config"]["wifi_ssid"], "SiteNet")

    def test_loaded_settings_survive_their_own_backup_and_restore(self):
        # An invalid pair in the file loads as both factory values with a warning;
        # the backup of that state is restorable by the same device.
        warnings = self.load_with(stakeout_tolerance_m=2.0, stakeout_fine_radius_m=1.0)
        self.assertEqual(warnings, ["stakeout_tolerance_m", "stakeout_fine_radius_m"])
        self.assertEqual(self.stakeout_values(), FACTORY_STAKEOUT)
        backup = json.loads(json.dumps(web.config_backup()))
        status, body, reboots = self.call_restore(backup)
        self.assertEqual((status, body["sections"], reboots),
                         (202, SECTIONS_3, ["/api/config/restore"]), body)
        self.assertEqual(self.stakeout_values(), FACTORY_STAKEOUT)
        self.boot(works=("Old",))                      # the restored candidate connects
        self.assertEqual(cfg.config_result_read()["status"], "connected")
        # A valid non-default pair loads unchanged and reaches a factory device.
        self.assertEqual(self.load_with(**STAKEOUT), [])
        self.assertEqual(self.stakeout_values(), STAKEOUT)
        backup = json.loads(json.dumps(web.config_backup()))
        self.target(**FACTORY_STAKEOUT)
        status, body, _reboots = self.call_restore(backup)
        self.assertEqual((status, body["sections"]), (202, SECTIONS_3), body)
        self.assertEqual(self.stakeout_values(), STAKEOUT)

    def test_replacement_device_receives_the_stakeout_settings(self):
        cfg.CONFIG.update(STAKEOUT)
        backup = self.device_a()
        self.factory_measurement()
        self.target(**FACTORY_STAKEOUT)
        status, body, reboots = self.call_restore(backup)
        self.assertEqual((status, body), (202, {"restored": True, "schema_version": 3,
                                                "sections": SECTIONS_3, "kept": [],
                                                "apply_id": None}))
        self.assertEqual(reboots, ["/api/config/restore"])
        self.assertEqual(self.stakeout_values(), STAKEOUT)
        self.assertEqual((tracking.measurement_gate(), tracking.field_setup()), (GATE, FIELD))
        with open("config.json") as fh:
            saved = json.load(fh)
        self.assertEqual(dict((key, saved[key]) for key in STAKEOUT), STAKEOUT)

    def test_schema_2_backup_leaves_stakeout_settings_unchanged(self):
        backup = self.as_schema_2(self.device_a())
        self.factory_measurement()
        self.target(**STAKEOUT)
        status, body, _reboots = self.call_restore(backup)
        self.assertEqual((status, body["schema_version"], body["sections"]),
                         (202, 2, ["connections", "measurement", "field_setup"]), body)
        self.assertEqual(self.stakeout_values(), STAKEOUT)
        self.assertEqual(tracking.measurement_gate(), GATE)

    def test_invalid_stakeout_section_changes_nothing(self):
        good = self.device_a()
        self.factory_measurement()
        self.target(**STAKEOUT)
        before = dict(cfg.CONFIG)
        with open("config.json") as fh:
            file_before = fh.read()

        def variant(change):
            backup = copy.deepcopy(good)
            change(backup)
            return backup
        cases = (
            lambda b: b.pop("stakeout"), lambda b: b.update(stakeout=[0.1, 5.0]),
            lambda b: b.update(stakeout=None), lambda b: b.update(stakeout={}),
            lambda b: b["stakeout"].pop("fine_radius_m"),
            lambda b: b["stakeout"].update(extra=1),
            lambda b: b.update(stakeout={"tolerance_m": 0.1, "radius_m": 5.0}),
            lambda b: b["stakeout"].update(tolerance_m=0.01),
            lambda b: b["stakeout"].update(fine_radius_m=51),
            lambda b: b["stakeout"].update(tolerance_m=True),
            lambda b: b["stakeout"].update(tolerance_m="x"),
            lambda b: b["stakeout"].update(tolerance_m=2.0, fine_radius_m=2.0),
            lambda b: b["stakeout"].update(tolerance_m=3.0, fine_radius_m=2.0))
        for index, change in enumerate(cases):
            backup = variant(change)
            with self.subTest(case=index):
                values, error = web.validate_backup(backup)
                self.assertIsNone(values)
                self.assertTrue(error.startswith("stakeout: "), error)
                status, body, reboots = self.call_restore(backup)
                self.assertEqual((status, body["error"], reboots), (400, "invalid_backup", []))
                self.assertEqual(cfg.CONFIG, before)
                self.assertFalse(cfg.pending_config_active())
                with open("config.json") as fh:
                    self.assertEqual(fh.read(), file_before)
        status, body, reboots = self.call_restore(variant(lambda b: b.update(schema_version=4)))
        self.assertEqual((status, body["error"], reboots), (400, "invalid_backup", []))
        self.assertEqual(cfg.CONFIG, before)


class TestRestoreFailureModes(_BackupCase):
    """Partial outcomes of a schema-2 restore stay honest (#24b, E8)."""

    def setUp(self):
        super().setUp()
        self.gate_app = state.AppState()
        self.gate_app.stats["gst_output_state"] = "receiving"
        self.gate_app.stats["access_state"] = "ONLINE"
        self.now = 100_000
        # As tests/test_gate_consistency.py: a real AppState judges a real fix.
        for patcher in (mock.patch.object(time, "ticks_ms", side_effect=lambda: self.now),
                        mock.patch("tracking.tracker"),
                        mock.patch.object(web, "app", self.gate_app)):
            patcher.start()
            self.addCleanup(patcher.stop)
        tracking.tracker.enqueue_fix.return_value = False

    def fix(self):
        self.gate_app.update_fix(dict(GGA, utc="120000.00"))
        self.gate_app.attach_gst(dict(GST, utc="120000.00"))

    def fail_writes_to(self, *targets):
        real = cfg._atomic_dump
        return mock.patch.object(cfg, "_atomic_dump", side_effect=lambda target, data:
                                 False if target in targets else real(target, data))

    def test_live_write_failure_keeps_every_old_value_and_the_verdict(self):
        backup = self.device_a(gate=STRICT)
        self.factory_measurement()
        self.fix()
        self.assertTrue(self.gate_app.gate_state()["accepted"])
        before = dict(cfg.CONFIG)
        with open("config.json") as fh:
            file_before = fh.read()
        with self.fail_writes_to("config.json"):
            status, body, reboots = self.call_restore(backup)
        self.assertEqual((status, body["error"], reboots), (500, "stage_failed", []))
        self.assertEqual(cfg.CONFIG, before)
        with open("config.json") as fh:
            self.assertEqual(fh.read(), file_before)
        self.assertFalse(cfg.pending_config_active())
        self.assertTrue(self.gate_app.gate_state()["accepted"])

    def test_arm_failure_after_the_live_write_keeps_the_new_defaults_and_judges_by_them(self):
        backup = self.device_a(gate=STRICT)
        self.factory_measurement()
        self.fix()
        self.assertTrue(self.gate_app.gate_state()["accepted"])
        with mock.patch.object(cfg._store, "arm", return_value=False):
            status, body, reboots = self.call_restore(backup)
        self.assertEqual((status, body["error"], reboots), (500, "stage_failed", []))
        self.assertEqual((tracking.measurement_gate(), tracking.field_setup(),
                          cfg.CONFIG["measurement_samples"]), (STRICT, FIELD, "quality"))
        with open("config.json") as fh:
            saved = json.load(fh)
        self.assertEqual((saved["measurement_min_satellites"], saved["measurement_samples"],
                          saved["wifi_ssid"]), (30, "quality", "Old"))
        self.assertEqual((cfg.CONFIG["wifi_ssid"], cfg.staged_config()), ("Old", {}))
        self.assertFalse(cfg.pending_config_active())
        self.assertFalse(self.gate_app.gate_state()["accepted"])
        self.assertIn("too_few_satellites", self.gate_app.gate_state()["quality"]["reasons"])

    def test_verified_restart_after_restore_keeps_everything(self):
        backup = self.device_a()
        self.factory_measurement()
        status, _body, _reboots = self.call_restore(backup)
        self.assertEqual(status, 202)
        self.boot(works=("SiteNet",))
        self.assertEqual(cfg.config_result_read()["status"], "connected")
        self.assertEqual((cfg.CONFIG["wifi_ssid"], tracking.measurement_gate(),
                          tracking.field_setup(), cfg.CONFIG["measurement_samples"]),
                         ("SiteNet", GATE, FIELD, "quality"))
        self.assertFalse(cfg.config_restart_required())

    def test_wifi_rollback_after_restore_keeps_the_restored_defaults(self):
        backup = self.device_a()
        self.factory_measurement()
        status, _body, _reboots = self.call_restore(backup)
        self.assertEqual(status, 202)
        self.boot(works=("Old",))                                 # SiteNet is not reachable
        self.assertEqual(cfg.config_result_read()["status"], "failed")
        self.assertEqual(cfg.CONFIG["wifi_ssid"], "Old")
        self.assertEqual((tracking.measurement_gate(), tracking.field_setup(),
                          cfg.CONFIG["measurement_samples"]), (GATE, FIELD, "quality"))
        cfg.load_config()                                         # and from the file
        self.assertEqual((cfg.CONFIG["measurement_samples"], cfg.CONFIG["antenna_height_m"]),
                         ("quality", 1.85))
        self.assertFalse(cfg.pending_config_active())


class TestRestoreSizeContract(_ConfigCase):
    """Every backup the device writes for a valid configuration fits the restore limit (E11)."""

    def largest_config(self, char, password=1024):
        cfg.CONFIG.update(wifi_ssid=char * 32, wifi_pass=char * 63, ntrip_host=char * 64,
                          ntrip_mount=char * 64, ntrip_user=char * 64, ntrip_pass=char * password,
                          ntrip_fallback_host=char * 64, ntrip_fallback_mount=char * 64,
                          ntrip_fallback_user=char * 64, ntrip_fallback_pass=char * password,
                          ntrip_port=65535, ntrip_fallback_port=65535, ap_pass=char * 63,
                          wifi_networks=[[char * 31 + str(i), char * 63] for i in range(8)],
                          reference_frame=char * 40, reference_frame_epoch=char * 16,
                          antenna_height_m=2.99, measurement_samples="quality",
                          stakeout_tolerance_m=4.999, stakeout_fine_radius_m=49.999)

    def test_largest_valid_backup_fits_the_restore_limit_in_device_bytes(self):
        for char in ('"', "\\", "\u00fc", "\U0001F600"):
            with self.subTest(char=char):
                self.largest_config(char)
                backup = web.config_backup()
                self.assertEqual(backup["stakeout"], {"tolerance_m": 4.999,
                                                      "fine_radius_m": 49.999})
                for indent in (None, 2):
                    size = len(json.dumps(backup, ensure_ascii=False,
                                          indent=indent).encode("utf-8"))
                    self.assertLessEqual(size, web_routing.CONFIG_RESTORE_BODY_LIMIT, indent)
        self.assertEqual(web_routing.CONFIG_RESTORE_BODY_LIMIT, 16384)


if __name__ == "__main__":
    unittest.main()
