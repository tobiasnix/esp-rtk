# SPDX-License-Identifier: AGPL-3.0-only
"""V11 access: identity, sessions, discovery, and transport contracts."""
import asyncio
import json
import os
import tempfile
import unittest
from unittest import mock

from support import (access, ble, cfg, discovery, fanout, identity, state,
                     web, FakeNVS)
import assets
import cfg_store

EPOCH_2000 = 946_684_800
JUMP_26Y_S = 820_540_800  # 9497 days


class TestIdentity(unittest.TestCase):
    def setUp(self):
        self.cwd = os.getcwd()
        self.tmp = tempfile.mkdtemp(prefix="esp-rtk-identity-")
        os.chdir(self.tmp)
        FakeNVS.namespaces.clear()
        identity.reset_cache()

    def tearDown(self):
        identity.reset_cache()
        os.chdir(self.cwd)

    def test_id_and_names_are_deterministic(self):
        result = identity.load_identity(mac=b"\xa1\xb2\xc3\xd4\xe5\xf6")
        self.assertEqual(result["device_id"], "A1B2C3D4E5F6")
        self.assertEqual(result["display_name"], "RTK-D4E5F6")
        self.assertEqual(result["ap_ssid"], "RTK-D4E5F6-SETUP")
        self.assertEqual(result["hostname"], "rtk-d4e5f6")

    def test_code_remains_stable_over_reload(self):
        first = identity.load_identity(mac=b"\x01\x02\x03\x04\x05\x06")
        identity.reset_cache()
        second = identity.load_identity(mac=b"\x01\x02\x03\x04\x05\x06")
        self.assertEqual(first["device_code"], second["device_code"])
        self.assertEqual(len(first["device_code"]), 12)
        self.assertEqual(first["stream_token"], second["stream_token"])
        self.assertEqual(len(first["stream_token"]), 32)
        self.assertNotEqual(first["device_code"], first["stream_token"])
        self.assertEqual(first["ble_pin"], second["ble_pin"])
        self.assertEqual(len(first["ble_pin"]), 6)
        self.assertTrue(first["ble_pin"].isdigit())

    def test_stream_token_can_be_rotated_independently(self):
        first = identity.load_identity(mac=b"\x01\x02\x03\x04\x05\x06")
        replacement = identity.rotate_stream_token()
        current = identity.load_identity()
        self.assertNotEqual(replacement, first["stream_token"])
        self.assertEqual(current["stream_token"], replacement)
        self.assertEqual(current["device_code"], first["device_code"])

    def test_individual_legacy_password_is_migrated(self):
        result = identity.load_identity(mac=b"\x01\x02\x03\x04\x05\x06",
                                        legacy_ap_password="mein-age-code")
        self.assertEqual(result["device_code"], "mein-age-code")

    def test_public_factory_password_is_not_migrated(self):
        result = identity.load_identity(mac=b"\x01\x02\x03\x04\x05\x06",
                                        legacy_ap_password="12345678")
        self.assertNotEqual(result["device_code"], "12345678")


class TestSessions(unittest.TestCase):
    def setUp(self):
        access.reset()

    def tearDown(self):
        access.reset()

    def test_login_cookie_and_csrf(self):
        session, error = access.login("ABCDEF123456", "ABCDEF123456",
                                      "192.0.2.4", now=100)
        self.assertIsNone(error)
        cookie = "x=1; rtk_session=%s" % session["token"]
        self.assertIsNotNone(access.authenticate(cookie, now=101))
        self.assertIsNone(access.authenticate(cookie, "wrong", True, now=101))
        self.assertIsNotNone(access.authenticate(cookie, session["csrf"], True,
                                                  now=101))

    def test_session_expires_upon_inactivity(self):
        session, _ = access.login("code", "code", now=100)
        cookie = "rtk_session=" + session["token"]
        self.assertIsNone(access.authenticate(
            cookie, now=100 + access.SESSION_IDLE_SEC + 1))

    def test_session_active_does_not_extend_the_idle_window(self):
        session, _ = access.login("code", "code", now=100)
        cookie = "rtk_session=" + session["token"]
        self.assertTrue(access.session_active(cookie, now=100 + access.SESSION_IDLE_SEC - 1))
        self.assertTrue(access.session_active(cookie, now=100 + access.SESSION_IDLE_SEC))
        self.assertFalse(access.session_active(cookie, now=100 + access.SESSION_IDLE_SEC + 1))
        self.assertFalse(access.session_active("", now=101))
        self.assertFalse(access.session_active("rtk_session=unknown", now=101))

    def test_duration_is_independent_of_ntp_calendar_time(self):
        original_ticks, original_time = access.time.ticks_ms, access.time.time
        try:
            access.time.ticks_ms = lambda: 100_000
            access.time.time = lambda: 1
            session, _ = access.login("code", "code")
            access.time.time = lambda: 2_000_000_000
            access.time.ticks_ms = lambda: 101_000
            self.assertIsNotNone(access.authenticate(
                "rtk_session=" + session["token"]))
        finally:
            access.time.ticks_ms, access.time.time = original_ticks, original_time

    def test_five_errors_throttle(self):
        for stamp in range(5):
            session, error = access.login("wrong", "correct", "peer", now=stamp)
            self.assertIsNone(session)
            self.assertEqual(error, "invalid")
        session, error = access.login("correct", "correct", "peer", now=5)
        self.assertIsNone(session)
        self.assertEqual(error, "blocked")


class TestTransportContracts(unittest.TestCase):
    def setUp(self):
        self.saved_config = dict(cfg.CONFIG)
        self.saved_identity = state.app.identity
        state.app.identity = {
            "device_id": "A1B2C3D4E5F6", "display_name": "RTK-D4E5F6",
            "ble_name": "RTK-D4E5F6", "hostname": "rtk-d4e5f6",
            "device_code": "ABCDEF123456", "stream_token": "STREAM123",
            "ble_pin": "123456",
        }

    def tearDown(self):
        cfg.CONFIG.clear()
        cfg.CONFIG.update(self.saved_config)
        state.app.identity = self.saved_identity

    def test_access_api_is_stable_and_exposes_no_secret(self):
        result = web.access_info()
        self.assertEqual(result["schema_version"], 2)
        self.assertEqual(result["device_id"], "A1B2C3D4E5F6")
        self.assertEqual(result["nmea_tcp"]["canonical_port"], 10110)
        self.assertEqual(result["nmea_tcp"]["legacy_ports"], [2947])
        self.assertTrue(result["nmea_tcp"]["authentication_required"])
        self.assertTrue(result["ble_nus"]["pairing_required"])
        self.assertTrue(result["ble_nus"]["encrypted"])
        self.assertNotIn("device_code", result)
        self.assertNotIn("stream_token", result)

    def test_access_api_reports_optional_tcp_authentication(self):
        cfg.CONFIG["nmea_tcp_auth_required"] = False
        result = web.access_info()["nmea_tcp"]
        self.assertFalse(result["authentication_required"])
        self.assertIsNone(result["authentication"])

    def test_wifi_qr_payload_escaped_sonderzeichen(self):
        payload = web.wifi_qr_payload({"ap_ssid": "RTK;SETUP",
                                       "ap_pass": "ABC:12345678",
                                       "device_code": "LABEL-CODE"})
        self.assertEqual(payload, "WIFI:T:WPA;S:RTK\\;SETUP;P:ABC\\:12345678;;")

    def test_portal_qr_keeps_code_in_the_fragment(self):
        payload = web.portal_qr_payload({"hostname": "rtk-d4e5f6",
                                         "device_code": "gnss-a+b c"})
        self.assertEqual(
            payload,
            "http://rtk-d4e5f6.local/#code=gnss-a%2Bb%20c")
        self.assertNotIn("?code=", payload)

    def test_label_contains_both_offline_qr_codes(self):
        old = state.app.identity
        state.app.identity = {
            "display_name": "RTK-D4E5F6", "hostname": "rtk-d4e5f6",
            "ap_ssid": "RTK-D4E5F6-SETUP", "ap_pass": "APSECRET123",
            "device_code": "CODE123",
            "ble_pin": "123456",
        }
        try:
            html = web.render_label()
        finally:
            state.app.identity = old
        self.assertIn('id="wifi-qr"', html)
        self.assertIn('id="portal-qr"', html)
        self.assertIn('<script defer src="/qr.js?v=%s"></script>' % assets.ASSET_VERSION, html)
        self.assertIn("WIFI:T:WPA;S:RTK-D4E5F6-SETUP;P:APSECRET123;;", html)
        self.assertIn("/#code=CODE123", html)
        self.assertIn("123456", html)

    def test_tcp_standard_and_legacy(self):
        cfg.CONFIG["nmea_tcp_port"] = 10110
        cfg.CONFIG["nmea_tcp_legacy_port"] = 2947
        cfg.CONFIG["nmea_tcp_legacy_enabled"] = True
        self.assertEqual(fanout.nmea_tcp_ports(), [10110, 2947])
        cfg.CONFIG["nmea_tcp_legacy_enabled"] = False
        self.assertEqual(fanout.nmea_tcp_ports(), [10110])

    def test_ble_name_uuid_and_scan_response(self):
        manager = ble.BLEManager("RTK-D4E5F6")
        self.assertEqual(manager.ble.configured["gap_name"], "RTK-D4E5F6")
        self.assertTrue(manager.ble.configured["bond"])
        self.assertTrue(manager.ble.configured["mitm"])
        self.assertTrue(manager.ble.configured["le_secure"])
        self.assertEqual(manager.ble.configured["io"], 0)
        _args, kwargs = manager.ble.advertisements[-1]
        self.assertIn(b"RTK-D4E5F6", bytes(kwargs["resp_data"]))
        self.assertNotIn(b"RTK-D4E5F6", bytes(kwargs["adv_data"]))
        self.assertIn(b"\x02\x01\x06", bytes(kwargs["adv_data"]))

    def test_ble_scan_name_contains_current_sta_ip_and_fits(self):
        old_mode, old_ip = state.app.stats["net_mode"], state.app.stats["sta_ip"]
        try:
            state.app.stats["net_mode"] = "STA"
            state.app.stats["sta_ip"] = "198.51.100.195"
            manager = ble.BLEManager("RTK-D4E5F6")
            self.assertTrue(manager._refresh_network_name())
            self.assertEqual(manager.name, "RTK-D4E5F6 198.51.100.195")
            _args, kwargs = manager.ble.advertisements[-1]
            response = bytes(kwargs["resp_data"])
            self.assertLessEqual(len(response), 31)
            self.assertIn(b"RTK-D4E5F6 198.51.100.195", response)
            self.assertEqual(manager.ble.configured["gap_name"], manager.name)
            self.assertFalse(manager._refresh_network_name())
        finally:
            state.app.stats["net_mode"], state.app.stats["sta_ip"] = old_mode, old_ip

    def test_ble_scan_name_uses_setup_ip_only_while_ap_is_active(self):
        old = (state.app.stats["net_mode"], state.app.stats["access_state"],
               state.app.stats["ap_ip"])
        try:
            manager = ble.BLEManager("RTK-D4E5F6")
            state.app.stats.update(net_mode="INIT", access_state="CONNECTING",
                                   ap_ip="192.168.4.1")
            self.assertEqual(manager._network_name(), "RTK-D4E5F6")
            state.app.stats.update(net_mode="AP_RECOVERY", access_state="RECOVERY")
            self.assertEqual(manager._network_name(), "RTK-D4E5F6 192.168.4.1")
        finally:
            (state.app.stats["net_mode"], state.app.stats["access_state"],
             state.app.stats["ap_ip"]) = old

    def test_ble_commands_are_standard(self):
        manager = ble.BLEManager("RTK-D4E5F6")
        conn = 7
        manager.connections.add(conn)
        calls = []
        manager.command_sink = lambda value: calls.append(value) or (True, None)
        manager.ble.values[manager.rx] = b"$PQTMVERNO*58"
        cfg.CONFIG["ble_commands_enabled"] = False
        manager.handle_write(manager.rx, conn)
        self.assertEqual(calls, [])
        manager._irq(28, (conn, 1, 1, 1, 16))
        self.assertIn(conn, manager.encrypted_connections)
        manager.ble.values[manager.rx] = b"$PQTMVERNO*58"
        cfg.CONFIG["ble_commands_enabled"] = True
        manager.handle_write(manager.rx, conn)
        self.assertEqual(calls, [b"$PQTMVERNO*58"])

    def test_ble_maintenance_window_survives_an_rtc_jump(self):
        manager = ble.BLEManager("RTK-D4E5F6"); conn = 7
        manager.connections.add(conn); manager.encrypted_connections.add(conn)
        calls = []
        manager.command_sink = lambda value: calls.append(value) or (True, None)
        previous = cfg.CONFIG.get("ble_commands_enabled")
        cfg.CONFIG["ble_commands_enabled"] = False
        self.addCleanup(cfg.CONFIG.__setitem__, "ble_commands_enabled", previous)
        uptime = [100]
        with mock.patch.object(ble.app, "uptime_seconds", side_effect=lambda: uptime[0]), \
                mock.patch.object(ble.time, "time", return_value=EPOCH_2000):
            self.assertEqual(manager.enable_command_maintenance(600), 600)
        with mock.patch.object(ble.app, "uptime_seconds", side_effect=lambda: uptime[0]), \
                mock.patch.object(ble.time, "time", return_value=EPOCH_2000 + JUMP_26Y_S):
            manager.handle_write(manager.rx, conn, b"$PQTMVERNO*58")
            uptime[0] = 699
            manager.handle_write(manager.rx, conn, b"$PQTMVERNO*58")
            uptime[0] = 700
            manager.handle_write(manager.rx, conn, b"$PQTMVERNO*58")
        self.assertEqual(calls, [b"$PQTMVERNO*58"] * 2)
        self.assertNotIn("ble_commands_until", state.app.stats)

    def test_ble_only_sends_nmea_to_encrypted_clients(self):
        manager = ble.BLEManager("RTK-D4E5F6")
        manager.connections.update((7, 8))
        manager.encrypted_connections.add(8)
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(manager.send(b"$GNGGA,1\r\n"))
        finally:
            loop.close()
        self.assertEqual(manager.ble.notified, [(8, b"$GNGGA,1\r\n")])

    def test_ble_starts_pairing_and_uses_devicepin(self):
        manager = ble.BLEManager("RTK-D4E5F6")
        manager._irq(1, (7, 0, b""))
        manager._handle_events()
        self.assertIn(7, manager.ble.pairings)
        manager._irq(31, (7, 3, 0))
        self.assertEqual(manager.ble.passkeys, [(7, 3, 123456)])

    def test_failed_ble_security_counts_an_auth_failure(self):
        manager = ble.BLEManager("RTK-D4E5F6")
        with mock.patch.dict(state.app.stats, {"ble_auth_failures": 0}), \
                mock.patch.object(ble, "log"):
            manager.events.extend([("security", (1, False, False, False, 0)),
                                   ("security", (2, True, False, False, 16))])
            manager._handle_events()
            self.assertEqual(state.app.stats["ble_auth_failures"], 2)
            manager.events.append(("security", (3, True, True, True, 16)))
            manager._handle_events()
            self.assertEqual(state.app.stats["ble_auth_failures"], 2)

    def test_ble_bond_secrets_are_persisted(self):
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory(prefix="esp-rtk-bonds-") as tmp:
            os.chdir(tmp)
            try:
                manager = ble.BLEManager("RTK-D4E5F6")
                self.assertTrue(manager._irq(30, (2, b"peer", b"secret")))
                manager._handle_events()
                restored = ble.BLEManager("RTK-D4E5F6")
                self.assertEqual(restored._irq(29, (2, 0, b"peer")), b"secret")
            finally:
                os.chdir(cwd)


def dns_query(name, qtype=1, ident=b"\x12\x34"):
    return (ident + b"\x01\0\0\x01\0\0\0\0\0\0" +
            discovery._encode_name(name) + discovery._u16(qtype) + b"\0\x01")


class TestDiscovery(unittest.TestCase):
    def setUp(self):
        self.ident = {"hostname": "rtk-d4e5f6", "display_name": "RTK-D4E5F6",
                      "device_id": "A1B2C3D4E5F6"}

    def test_captive_dns_shows_every_name_to_the_portal(self):
        response = discovery.captive_dns_response(
            dns_query("connectivitycheck.gstatic.com"), "192.168.4.1")
        self.assertIsNotNone(response)
        self.assertEqual(response[:2], b"\x12\x34")
        self.assertIn(bytes((192, 168, 4, 1)), response)

    def test_mdns_hostname(self):
        response = discovery.mdns_response(
            dns_query("rtk-d4e5f6.local", ident=b"\0\0"), self.ident,
            "203.0.113.20")
        self.assertIsNotNone(response)
        self.assertIn(bytes((203, 0, 113, 20)), response)

    def test_mdns_nmea_service(self):
        response = discovery.mdns_response(
            dns_query("_nmea-0183._tcp.local", 12, b"\0\0"), self.ident,
            "203.0.113.20")
        self.assertIn(b"RTK-D4E5F6", response)

    def test_mdns_processes_aaaa_and_a_in_one_package(self):
        first = discovery._encode_name("rtk-d4e5f6.local") + b"\0\x1c\0\x01"
        second = discovery._encode_name("rtk-d4e5f6.local") + b"\0\x01\0\x01"
        query = b"\0\0\0\0\0\x02\0\0\0\0\0\0" + first + second
        response = discovery.mdns_response(query, self.ident, "192.0.2.203")
        self.assertIsNotNone(response)
        self.assertEqual(response[4:6], b"\0\x02")
        self.assertIn(bytes((192, 0, 2, 203)), response)

    def test_mdns_interface_follows_sta_and_recovery_ap(self):
        self.assertEqual(discovery.active_mdns_ip(
            {"sta_ip": "192.0.2.203"}, False, "192.168.4.1"),
            "192.0.2.203")
        self.assertEqual(discovery.active_mdns_ip(
            {"sta_ip": "N/A"}, True, "192.168.4.1"), "192.168.4.1")
        self.assertIsNone(discovery.active_mdns_ip(
            {"sta_ip": "N/A"}, False, "192.168.4.1"))


class PowerCut(BaseException):
    # BaseException on purpose: atomic_dump's `except Exception` cleanup must not run,
    # exactly as after a real power cut.
    pass


class TestTransactionalConfiguration(unittest.TestCase):
    def setUp(self):
        self.cwd = os.getcwd()
        self.tmp = tempfile.mkdtemp(prefix="esp-rtk-pending-")
        os.chdir(self.tmp)
        self.saved = dict(cfg.CONFIG)
        self.saved_access_state = state.app.stats.get("access_state")
        state.app.stats["access_state"] = "ONLINE"
        cfg._ROLLBACK_OPEN, cfg._ROLLBACK_CODE, cfg._STAGING_UNRECOVERED = False, None, False
        cfg._STAGING_LOG.reset()       # each test sees the first recovery error
        cfg._CONFIG_FILE_PROTECTED = False
        cfg.discard_staged_config()
        cfg.CONFIG["wifi_ssid"] = "Old"
        cfg.CONFIG["wifi_pass"] = "legacy-password"
        cfg.save_config()

    def tearDown(self):
        cfg.CONFIG.clear()
        cfg.CONFIG.update(self.saved)
        cfg._ROLLBACK_OPEN, cfg._ROLLBACK_CODE, cfg._STAGING_UNRECOVERED = False, None, False
        cfg.set_task_reloader(None)
        cfg.discard_staged_config()
        state.app.stats["access_state"] = self.saved_access_state
        cfg._CONFIG_FILE_PROTECTED = False
        del cfg.CONFIG_WARNINGS[:]
        os.chdir(self.cwd)

    def test_stage_without_apply_changes_nothing_on_disk_or_in_config(self):
        summary = cfg.stage_config({"wifi_ssid": "New"})
        self.assertEqual(summary, {"applied": [], "staged": ["wifi_ssid"], "pending": True,
                                   "restart_required": True,
                                   "staging_revision": cfg.staging_revision()})
        self.assertEqual(cfg.configured_networks(), [("Old", "legacy-password")])
        with open("config.json") as fh:
            self.assertEqual(json.load(fh)["wifi_ssid"], "Old")
        self.assertFalse(cfg.pending_config_active())
        self.assertEqual(cfg.config_result_read()["status"], "none")
        self.assertEqual(cfg.stage_config({"wifi_ssid": "Old"})["staged"], [])   # reverted
        self.assertFalse(cfg.config_restart_required())

    def test_stage_commit(self):
        cfg.stage_config({"wifi_ssid": "New"})
        self.assertTrue(cfg.apply_staged_config())
        self.assertTrue(cfg.pending_config_active())
        self.assertEqual(cfg.config_result_read()["status"], "pending")
        self.assertEqual(cfg.staged_config(), {})
        cfg.commit_pending_config("FieldNetwork")
        self.assertFalse(cfg.pending_config_active())
        self.assertEqual(cfg.config_result_read()["details"]["ssid"], "FieldNetwork")
        with open("config.json") as fh:
            self.assertEqual(json.load(fh)["wifi_ssid"], "New")

    def test_stage_rollback(self):
        cfg.stage_config({"wifi_ssid": "Invalid"})
        self.assertTrue(cfg.apply_staged_config())
        self.assertTrue(cfg.rollback_pending_config("authentication_failed"))
        self.assertEqual(cfg.CONFIG["wifi_ssid"], "Old")
        with open("config.json") as fh:
            self.assertEqual(json.load(fh)["wifi_ssid"], "Old")

    def test_apply_live_refuses_verified_and_unknown_keys_and_keeps_config_on_write_errors(self):
        for bad in ({"wifi_ssid": "X"}, {"no_such_key": 1}):
            with self.assertRaises(ValueError):
                cfg.apply_live(bad)
        before = cfg.CONFIG["ntrip_host"]
        with mock.patch.object(cfg, "_atomic_dump", return_value=False):
            self.assertFalse(cfg.apply_live({"ntrip_host": "new.example"}))
            self.assertIsNone(cfg.stage_config({"ntrip_host": "new.example", "wifi_ssid": "New"}))
        self.assertEqual(cfg.CONFIG["ntrip_host"], before)
        self.assertEqual(cfg.staged_config(), {})
        self.assertFalse(cfg.config_restart_required())

    def _load_with_warning(self, key, raw):
        with open("config.json") as fh:
            data = json.load(fh)
        data[key] = raw
        with open("config.json", "w") as fh:
            json.dump(data, fh)
        cfg.load_config()
        self.assertEqual([w["key"] for w in cfg.CONFIG_WARNINGS], [key])

    def _assert_repaired(self, key, value):
        with open("config.json") as fh:
            self.assertEqual(json.load(fh)[key], value)
        self.assertEqual(cfg.CONFIG_WARNINGS, [])
        self.assertEqual(cfg.CONFIG[key], value)

    def test_sent_value_equal_to_the_fallback_repairs_a_warned_key(self):       # #18 x #23a
        # task_restart (ntrip_port: 70000 -> RAM 2101) and live (antenna_height_m)
        for key, raw, value in (("ntrip_port", 70000, 2101),
                                ("antenna_height_m", "abc", cfg.DEFAULT_CONFIG["antenna_height_m"])):
            with self.subTest(key=key):
                self._load_with_warning(key, raw)
                self.assertEqual(cfg.CONFIG[key], value)
                summary = cfg.stage_config({key: value})
                self.assertEqual(summary["applied"], [key])
                self.assertFalse(summary["restart_required"])     # the task runs on the fallback
                self._assert_repaired(key, value)

    def test_apply_live_with_the_fallback_value_repairs_a_warned_key(self):     # #18 x #23a
        for key, raw, value in (("ntrip_port", 70000, 2101),
                                ("antenna_height_m", "abc", cfg.DEFAULT_CONFIG["antenna_height_m"])):
            with self.subTest(key=key):
                self._load_with_warning(key, raw)
                self.assertTrue(cfg.apply_live({key: value}))
                self.assertFalse(cfg.config_restart_required())
                self._assert_repaired(key, value)

    def test_unchanged_key_without_a_warning_is_not_saved(self):
        with mock.patch.object(cfg, "save_config") as save:
            summary = cfg.stage_config({"ntrip_port": cfg.CONFIG["ntrip_port"]})
        save.assert_not_called()
        self.assertEqual(summary["applied"], [])

    def test_recovery_error_of_the_staging_files_is_logged_at_most_once_a_minute(self):  # #23a M-6
        import cfg_logging
        clock = [0]
        throttle = cfg_logging.LogThrottle(cfg_logging.HOT_PATH_LOG_INTERVAL_MS,
                                           clock=lambda: clock[0])
        with mock.patch.object(cfg, "_STAGING_LOG", throttle), \
                mock.patch.object(cfg._store, "recover_staging", return_value=False), \
                mock.patch.object(cfg, "log") as log:
            for _ in range(5):
                self.assertTrue(cfg.pending_config_active())          # still open and blocked
                clock[0] += 5000
            errors = [c.args[2] for c in log.call_args_list if c.args[0] == "ERROR"]
            self.assertEqual(len(errors), 1)
            clock[0] = 60000
            self.assertTrue(cfg.pending_config_active())
            errors = [c.args[2] for c in log.call_args_list if c.args[0] == "ERROR"]
        self.assertEqual(len(errors), 2)
        self.assertIn("[4 more since the last report]", errors[1])
        self.assertTrue(cfg._STAGING_UNRECOVERED)

    def arm(self, changes):
        cfg.stage_config(changes)
        self.assertTrue(cfg.apply_staged_config())

    def fresh(self):
        # New empty working directory with config.json = "Old" (same state as setUp).
        os.chdir(tempfile.mkdtemp(prefix="esp-rtk-pending-"))
        cfg.CONFIG.clear(); cfg.CONFIG.update(self.saved)
        cfg._ROLLBACK_OPEN, cfg._ROLLBACK_CODE, cfg._STAGING_UNRECOVERED = False, None, False
        cfg.discard_staged_config()
        cfg.CONFIG["wifi_ssid"], cfg.CONFIG["wifi_pass"] = "Old", "legacy-password"
        cfg.save_config()

    def boot(self, works=("Old",)):
        # A restart (assumption A5): RAM gone, load, the import-time resolution (R2a),
        # then NetworkManager.run: no station attempt while a recorded rollback is
        # incomplete (R3), otherwise the verdict. Only SSIDs in `works` connect, and
        # only with the confirmed password "legacy-password".
        cfg.discard_staged_config()                                                             # so is the staging
        cfg._ROLLBACK_OPEN, cfg._ROLLBACK_CODE, cfg._STAGING_UNRECOVERED = False, None, False   # RAM is gone
        cfg.load_config()
        cfg.resolve_interrupted_apply()
        if cfg.rollback_incomplete():
            return
        if cfg.pending_config_active():
            if cfg.CONFIG["wifi_ssid"] in works and cfg.CONFIG["wifi_pass"] == "legacy-password":
                cfg.commit_pending_config(cfg.CONFIG["wifi_ssid"])
            else:
                cfg.rollback_pending_config("authentication_failed")

    def cut_at(self, step):
        # File operation number `step` (os.rename / os.remove inside cfg_store) never happens.
        calls, real_rename, real_remove = [0], cfg_store.os.rename, cfg_store.os.remove
        def guarded(real):
            def operation(*args):
                calls[0] += 1
                if calls[0] == step:
                    raise PowerCut()
                return real(*args)
            return operation
        return mock.patch.multiple(cfg_store.os, rename=guarded(real_rename), remove=guarded(real_remove))

    def fail_writes_to(self, *targets):
        real = cfg._atomic_dump
        return mock.patch.object(cfg, "_atomic_dump", side_effect=lambda target, data:
                                 False if target in targets else real(target, data))

    def test_rollback_writes_failed_with_a_code_and_never_connected(self):
        self.arm({"wifi_ssid": "Invalid"})
        statuses = []
        real = cfg.config_result_write
        def record(status, message=None, details=None):
            statuses.append(status)
            return real(status, message, details)
        with mock.patch.object(cfg, "config_result_write", side_effect=record):
            self.assertTrue(cfg.rollback_pending_config("authentication_failed"))
        self.assertEqual(statuses, ["failed"])
        result = cfg.config_result_read()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["details"], {"code": "authentication_failed", "restored": ["wifi_ssid"]})
        self.assertFalse(cfg.pending_config_active())
        self.assertFalse(os.path.exists(cfg.LAST_GOOD_CONFIG_FILE))

    def test_rollback_code_comes_from_the_last_wifi_error(self):
        for stats_value, expected in (("access_point_not_found", "access_point_not_found"),
                                      (None, "connection_lost")):
            self.arm({"wifi_ssid": "Invalid"})
            with mock.patch.dict(state.app.stats, {"last_wifi_error": stats_value}):
                self.assertTrue(cfg.rollback_pending_config())
            self.assertEqual(cfg.config_result_read()["details"]["code"], expected)

    def test_rollback_restores_only_changed_verified_keys(self):
        self.arm({"wifi_ssid": "Invalid", "ntrip_host": "new.example"})
        cfg.save_config({"measurement_min_satellites": 30, "ap_pass": "rotated-at-boot-1"})
        self.assertTrue(cfg.rollback_pending_config("connection_timeout"))
        with open("config.json") as fh:
            stored = json.load(fh)
        for source in (cfg.CONFIG, stored):
            self.assertEqual(source["wifi_ssid"], "Old")
            self.assertEqual(source["ntrip_host"], "new.example")       # task_restart stays
            self.assertEqual(source["measurement_min_satellites"], 30)  # live stays
            self.assertEqual(source["ap_pass"], "rotated-at-boot-1")    # not part of the change
        self.assertEqual(cfg.config_result_read()["details"]["restored"], ["wifi_ssid"])

    def test_power_cut_at_every_rollback_step_never_keeps_the_rejected_candidate(self):
        for works in (("Old",), ("Old", "Invalid")):   # candidate unreachable / reachable on the next boot
            step = 1
            while True:
                self.fresh()
                self.arm({"wifi_ssid": "Invalid"})
                cut = False
                with self.cut_at(step):
                    try:
                        cfg.rollback_pending_config("authentication_failed")
                    except PowerCut:
                        cut = True
                decided = cfg.config_result_read()["status"] == "failed"
                self.boot(works)
                with self.subTest(works=works, step=step):
                    self.assertFalse(cfg.pending_config_active())
                    if decided or "Invalid" not in works:
                        self.assertEqual(cfg.CONFIG["wifi_ssid"], "Old")
                        self.assertEqual(cfg.config_result_read()["details"]["code"], "authentication_failed")
                if not cut:
                    break
                step += 1
        self.assertGreater(step, 3)                     # the cut really walked through the steps

    def test_recorded_decision_is_completed_before_any_connect_even_if_the_candidate_is_reachable(self):
        self.arm({"wifi_ssid": "Invalid"})
        # power cut after `failed` was written, before config.json was restored
        self.assertTrue(cfg.config_result_write("failed", details={"code": "authentication_failed",
                                                                   "restored": ["wifi_ssid"]}))
        with mock.patch.object(cfg, "commit_pending_config", wraps=cfg.commit_pending_config) as commit:
            self.boot(works=("Old", "Invalid"))
        commit.assert_not_called()
        self.assertEqual((cfg.CONFIG["wifi_ssid"], cfg.CONFIG["wifi_pass"]), ("Old", "legacy-password"))
        with open("config.json") as fh:
            self.assertEqual(json.load(fh)["wifi_ssid"], "Old")
        self.assertFalse(cfg.pending_config_active())
        self.assertEqual(cfg.config_result_read()["details"]["code"], "authentication_failed")

    def test_power_cut_after_a_failed_verification_of_an_empty_change_never_confirms(self):
        for change in ({"ntrip_host": "new.example"}, {}):   # caster only; unchanged /save
            step = 1
            while True:
                self.fresh()
                self.arm(change)
                cut = False
                with self.cut_at(step):
                    try:
                        cfg.rollback_pending_config("connection_timeout")   # the router was down
                    except PowerCut:
                        cut = True
                decided = cfg.config_result_read()["status"] == "failed"
                self.boot()                                   # Old connects again
                with self.subTest(change=change, step=step):
                    self.assertFalse(cfg.pending_config_active())
                    if decided:                               # a recorded decision is never confirmed
                        self.assertEqual(cfg.config_result_read()["status"], "failed")
                        self.assertEqual(cfg.config_result_read()["details"]["code"], "connection_timeout")
                if not cut:
                    break
                step += 1

    def test_rollback_keeps_an_existing_failed_result(self):
        self.arm({"wifi_ssid": "Invalid"})
        self.assertTrue(cfg.config_result_write("failed", details={"code": "authentication_failed",
                                                                   "restored": ["wifi_ssid"]}))
        self.assertTrue(cfg.rollback_pending_config("connection_timeout"))
        self.assertEqual(cfg.config_result_read()["details"]["code"], "authentication_failed")

    def test_rollback_with_an_unwritable_result_still_restores(self):
        self.arm({"wifi_ssid": "Invalid"})
        with self.fail_writes_to(cfg.CONFIG_RESULT_FILE):
            self.assertTrue(cfg.rollback_pending_config("authentication_failed"))
        self.assertEqual(cfg.CONFIG["wifi_ssid"], "Old")
        self.assertFalse(cfg.pending_config_active())
        self.boot()                                       # stale `pending` without pending file
        self.assertEqual(cfg.config_result_read()["details"],
                         {"code": "apply_interrupted", "restored": []})

    def test_unwritable_config_leaves_an_incomplete_rollback_that_the_next_boot_completes(self):
        self.arm({"wifi_ssid": "Invalid"})
        with self.fail_writes_to("config.json"):
            self.assertFalse(cfg.rollback_pending_config("authentication_failed"))
        self.assertEqual(cfg.CONFIG["wifi_ssid"], "Invalid")
        self.assertTrue(cfg.rollback_incomplete())
        self.boot(works=("Old", "Invalid"))
        self.assertEqual(cfg.CONFIG["wifi_ssid"], "Old")
        self.assertFalse(cfg.rollback_incomplete())
        self.assertEqual(cfg.config_result_read()["details"]["code"], "authentication_failed")

    def test_unremovable_pending_keeps_the_rollback_incomplete(self):
        self.arm({"wifi_ssid": "Invalid"})
        real_remove = cfg_store.os.remove
        def refuse_pending(target):
            if target == cfg.PENDING_CONFIG_FILE:
                raise OSError(5, "EIO")
            return real_remove(target)
        with mock.patch.object(cfg_store.os, "remove", side_effect=refuse_pending):
            self.assertFalse(cfg.rollback_pending_config("authentication_failed"))
        self.assertTrue(os.path.exists(cfg.LAST_GOOD_CONFIG_FILE))   # kept while pending exists
        self.assertTrue(cfg.rollback_incomplete())
        self.assertTrue(cfg.complete_rollback())                     # removal works again
        self.assertFalse(cfg.rollback_incomplete())
        self.assertEqual(cfg.CONFIG["wifi_ssid"], "Old")

    def test_complete_rollback_verifies_the_written_config_and_is_idempotent(self):
        self.arm({"wifi_ssid": "Invalid"})
        self.assertTrue(cfg.config_result_write("failed", details={"code": "authentication_failed",
                                                                   "restored": ["wifi_ssid"]}))
        real = cfg._atomic_dump
        with mock.patch.object(cfg, "_atomic_dump", side_effect=lambda target, data:
                               True if target == "config.json" else real(target, data)):
            self.assertFalse(cfg.complete_rollback())                # claimed write, nothing on disk
        self.assertTrue(cfg.rollback_incomplete())
        self.assertTrue(cfg.complete_rollback())
        self.assertTrue(cfg.complete_rollback())                     # nothing left: still True
        self.assertEqual(cfg.CONFIG["wifi_ssid"], "Old")

    def test_rollback_stays_open_in_ram_when_failed_cannot_be_written(self):     # I2
        self.arm({"wifi_ssid": "Invalid"})
        with self.fail_writes_to(cfg.CONFIG_RESULT_FILE, "config.json"):
            self.assertFalse(cfg.rollback_pending_config("authentication_failed"))
            self.assertEqual(cfg.config_result_read()["status"], "pending")   # nothing persisted
            self.assertFalse(cfg.rollback_incomplete())                        # the file view misses it
            self.assertTrue(cfg.rollback_open())                               # the RAM view does not
            self.assertFalse(cfg.retry_rollback())
            self.assertTrue(cfg.rollback_open())
        self.assertTrue(cfg.retry_rollback())                                  # writes work again
        self.assertFalse(cfg.rollback_open())
        self.assertEqual(cfg.CONFIG["wifi_ssid"], "Old")
        self.assertEqual(cfg.config_result_read()["details"]["code"], "authentication_failed")

    def test_recovery_error_never_raises_and_counts_as_an_open_change(self):    # I3, cfg side
        self.arm({"wifi_ssid": "Invalid"})
        os.rename(cfg.PENDING_CONFIG_FILE, cfg.PENDING_CONFIG_FILE + ".tmp")   # target lost, valid .tmp
        real_rename = cfg_store.os.rename
        def eio(src, dst):
            if src.endswith(".tmp"):
                raise OSError(5, "EIO")
            return real_rename(src, dst)
        with mock.patch.object(cfg_store.os, "rename", side_effect=eio):
            self.assertIsInstance(cfg.resolve_interrupted_apply(), bool)        # import-time call: no raise
            self.assertTrue(cfg.pending_config_active())
            self.assertTrue(cfg.rollback_open())
            self.assertFalse(cfg.retry_rollback())
            self.assertTrue(os.path.exists(cfg.PENDING_CONFIG_FILE + ".tmp"))
            self.assertTrue(os.path.exists(cfg.LAST_GOOD_CONFIG_FILE))
        self.assertTrue(cfg.retry_rollback())                                  # recovery works again
        self.assertFalse(cfg.rollback_open())
        self.assertEqual(cfg.CONFIG["wifi_ssid"], "Old")
        self.assertEqual(cfg.config_result_read()["details"]["code"], "storage_error")

    def test_commit_result_write_failure_keeps_the_files_for_the_next_boot(self):
        self.arm({"wifi_ssid": "New"})
        with self.fail_writes_to(cfg.CONFIG_RESULT_FILE):
            self.assertFalse(cfg.commit_pending_config("New"))
        self.assertTrue(cfg.pending_config_active())
        self.boot(works=("New",))
        self.assertEqual(cfg.config_result_read()["details"], {"ssid": "New"})
        self.assertFalse(cfg.pending_config_active())

    def test_commit_of_an_unlanded_change_records_apply_interrupted(self):
        self.arm({"wifi_ssid": "New"})
        with open("config.json", "w") as fh:                  # the candidate never reached config.json
            json.dump(dict(cfg._persisted_config(), wifi_ssid="Old"), fh)
        self.boot()
        self.assertEqual(cfg.CONFIG["wifi_ssid"], "Old")
        self.assertFalse(cfg.pending_config_active())
        self.assertEqual(cfg.config_result_read()["details"],
                         {"code": "apply_interrupted", "restored": []})

    def test_a_stale_pending_result_without_pending_file_resolves_to_failed(self):
        self.assertTrue(cfg.config_result_write("pending"))
        self.assertTrue(cfg.resolve_interrupted_apply())
        result = cfg.config_result_read()
        self.assertEqual((result["status"], result["details"]),
                         ("failed", {"code": "apply_interrupted", "restored": []}))
        self.assertFalse(cfg.resolve_interrupted_apply())      # nothing left to resolve

    def test_setup_mode_rollback_restores_the_confirmed_network(self):
        self.arm({"wifi_ssid": "Invalid"})
        self.assertTrue(cfg.rollback_pending_config("setup_mode"))
        self.assertEqual(cfg.CONFIG["wifi_ssid"], "Old")
        self.assertEqual(cfg.config_result_read()["details"], {"code": "setup_mode", "restored": ["wifi_ssid"]})

    def test_lost_pending_target_is_recovered_before_the_check(self):
        self.arm({"wifi_ssid": "Invalid"})
        os.rename(cfg.PENDING_CONFIG_FILE, cfg.PENDING_CONFIG_FILE + ".previous")
        self.assertTrue(cfg.pending_config_active())
        self.assertTrue(os.path.exists(cfg.PENDING_CONFIG_FILE))

    def test_unreadable_last_good_ends_the_transaction_without_a_restore(self):
        self.arm({"wifi_ssid": "Invalid"})
        with open(cfg.LAST_GOOD_CONFIG_FILE, "w") as fh: fh.write("{broken")
        self.assertTrue(cfg.rollback_pending_config("connection_timeout"))
        self.assertEqual(cfg.CONFIG["wifi_ssid"], "Invalid")       # nothing to restore from (E4)
        self.assertFalse(cfg.pending_config_active())
        self.assertEqual(cfg.config_result_read()["details"], {"code": "connection_timeout", "restored": []})

    def test_unreadable_pending_restores_every_verified_key_from_last_good(self):
        self.arm({"wifi_ssid": "Invalid"})
        with open(cfg.PENDING_CONFIG_FILE, "w") as fh: fh.write("{broken")
        self.assertTrue(cfg.rollback_pending_config("connection_timeout"))
        self.assertEqual(cfg.CONFIG["wifi_ssid"], "Old")
        self.assertIn("wifi_ssid", cfg.config_result_read()["details"]["restored"])

    def test_v15_10_2_pending_files_still_commit_and_roll_back(self):
        old = cfg._persisted_config()
        new = dict(old, wifi_ssid="New", ntrip_host="new.example")
        for path, data in (("config.json", new), (cfg.PENDING_CONFIG_FILE, new),
                           (cfg.LAST_GOOD_CONFIG_FILE, old),
                           (cfg.CONFIG_RESULT_FILE, {"schema_version": 2, "status": "pending",
                                                     "details": {}, "uptime_sec": 1800000000})):
            with open(path, "w") as fh: json.dump(data, fh)
        cfg.load_config()
        self.assertTrue(cfg.pending_config_active())
        self.assertEqual(cfg.config_result_read()["status"], "pending")
        self.assertTrue(cfg.rollback_pending_config("access_point_not_found"))
        self.assertEqual((cfg.CONFIG["wifi_ssid"], cfg.CONFIG["ntrip_host"]), ("Old", "new.example"))
        for path, data in (("config.json", new), (cfg.PENDING_CONFIG_FILE, new),
                           (cfg.LAST_GOOD_CONFIG_FILE, old),
                           (cfg.CONFIG_RESULT_FILE, {"schema_version": 2, "status": "pending",
                                                     "details": {}, "uptime_sec": 1800000000})):
            with open(path, "w") as fh: json.dump(data, fh)        # a second, separate V15.10.2 transaction
        cfg.load_config()
        self.assertTrue(cfg.commit_pending_config("New"))
        self.assertEqual(cfg.config_result_read()["details"], {"ssid": "New"})
        self.assertFalse(cfg.pending_config_active())

    def result_tmp_fault(self):
        # The result file lost its target mid-rename; renaming its valid .tmp back fails with EIO.
        os.rename(cfg.CONFIG_RESULT_FILE, cfg.CONFIG_RESULT_FILE + ".tmp")
        real = cfg_store.os.rename

        def eio(src, dst):
            if src == cfg.CONFIG_RESULT_FILE + ".tmp":
                raise OSError(5, "EIO")
            return real(src, dst)
        return mock.patch.object(cfg_store.os, "rename", side_effect=eio)

    def test_unrecoverable_result_file_is_read_from_its_candidate_and_never_raises(self):
        self.arm({"wifi_ssid": "Invalid"})
        with self.result_tmp_fault():
            self.assertEqual(cfg.config_result_read()["status"], "pending")
            self.assertFalse(cfg.resolve_interrupted_apply())
            self.assertFalse(cfg._STAGING_UNRECOVERED)              # the result file alone never blocks
            self.assertTrue(cfg.rollback_pending_config("authentication_failed"))
        self.assertEqual(cfg.CONFIG["wifi_ssid"], "Old")
        self.assertFalse(cfg.rollback_open())

    def test_commit_with_a_stored_failed_completes_the_rollback_but_never_confirms(self):  # M-7
        self.arm({"wifi_ssid": "New"})
        self.assertTrue(cfg.config_result_write("failed", details={"code": "authentication_failed",
                                                                   "restored": ["wifi_ssid"]}))
        self.assertFalse(cfg.commit_pending_config("New"))
        self.assertFalse(cfg.pending_config_active())                 # the rollback completed
        self.assertFalse(cfg.rollback_open())
        self.assertEqual(cfg.CONFIG["wifi_ssid"], "Old")
        self.assertEqual(cfg.config_result_read()["status"], "failed")

    def test_commit_of_an_unlanded_change_never_confirms(self):                     # M-7
        self.arm({"wifi_ssid": "New"})
        with open("config.json", "w") as fh:
            json.dump(dict(cfg._persisted_config(), wifi_ssid="Old"), fh)
        cfg.load_config()                                     # as on the verification boot
        self.assertFalse(cfg.commit_pending_config("New"))
        self.assertFalse(cfg.rollback_open())
        self.assertEqual(cfg.config_result_read()["details"]["code"], "apply_interrupted")

    def test_commit_never_confirms_with_an_unreadable_result(self):
        self.arm({"wifi_ssid": "New"})
        os.remove(cfg.CONFIG_RESULT_FILE)
        real = cfg_store.recover_atomic

        def broken(path):
            if path == cfg.CONFIG_RESULT_FILE:
                raise OSError(5, "EIO")
            return real(path)
        with mock.patch.object(cfg_store, "recover_atomic", side_effect=broken):
            self.assertEqual(cfg.config_result_read()["status"], "unknown")
            self.assertFalse(cfg.commit_pending_config("New"))
        self.assertTrue(cfg.pending_config_active())
        self.assertTrue(cfg.rollback_open())
        self.assertEqual(cfg.config_result_read()["status"], "none")   # nothing was confirmed

    def test_unreadable_result_with_a_pending_file_counts_as_an_open_rollback(self):
        self.arm({"wifi_ssid": "New"})
        os.remove(cfg.CONFIG_RESULT_FILE)
        real = cfg_store.recover_atomic

        def broken(path):
            if path == cfg.CONFIG_RESULT_FILE:
                raise OSError(5, "EIO")
            return real(path)
        with mock.patch.object(cfg_store, "recover_atomic", side_effect=broken):
            self.assertEqual(cfg.config_result_read()["status"], "unknown")
            self.assertTrue(cfg.rollback_incomplete())          # as commit: never treated as settled
        self.assertFalse(cfg.rollback_incomplete())             # readable again: just pending
        cfg.rollback_pending_config("authentication_failed")
        with mock.patch.object(cfg_store, "recover_atomic", side_effect=broken):
            self.assertFalse(cfg.rollback_incomplete())         # no pending file: nothing open

    def test_an_existing_but_unreadable_result_is_unknown_never_none(self):
        self.assertEqual(cfg.config_result_read()["status"], "none")   # no result file at all
        for damage in ("invalid_json", "eio"):
            with self.subTest(damage=damage):
                self.fresh()
                self.arm({"wifi_ssid": "New"})
                self.assertTrue(cfg.config_result_write("failed", details={"code": "authentication_failed",
                                                                           "restored": ["wifi_ssid"]}))
                patches = []
                if damage == "invalid_json":
                    with open(cfg.CONFIG_RESULT_FILE, "w") as fh: fh.write("{broken")
                else:
                    real_open = open

                    def eio_open(path, *args, **kwargs):
                        if path == cfg.CONFIG_RESULT_FILE:
                            raise OSError(5, "EIO")
                        return real_open(path, *args, **kwargs)
                    patches.append(mock.patch("builtins.open", side_effect=eio_open))
                for patcher in patches: patcher.start()
                try:
                    self.assertEqual(cfg.config_result_read()["status"], "unknown")
                    self.assertTrue(cfg.rollback_incomplete())
                    self.assertFalse(cfg.commit_pending_config("New"))  # never confirms the candidate
                finally:
                    for patcher in patches: patcher.stop()
                self.assertTrue(cfg.pending_config_active())
                self.assertTrue(cfg.rollback_open())
                self.assertNotEqual(cfg.config_result_read()["status"], "connected")

    def test_unreadable_result_is_served_as_pending_never_as_a_failure(self):
        # The current UI reports every final status except "connected" as
        # "Configuration failed; the previous working settings were restored."
        self.arm({"wifi_ssid": "New"})
        os.remove(cfg.CONFIG_RESULT_FILE)
        real = cfg_store.recover_atomic

        def broken(path):
            if path == cfg.CONFIG_RESULT_FILE:
                raise OSError(5, "EIO")
            return real(path)
        with mock.patch.object(cfg_store, "recover_atomic", side_effect=broken), \
                mock.patch.object(web, "_admin_allowed", return_value=True):
            self.assertEqual(cfg.config_result_read()["status"], "unknown")
            writer = FakeHttpWriter()
            asyncio.run(web._handle_diagnostics_request(writer, "/api/config-result", "", False, {}))
            served = json.loads(bytes(writer.data).partition(b"\r\n\r\n")[2])
        self.assertEqual((served["status"], served["details"]), ("pending", {"code": "result_unreadable"}))
        self.assertEqual(cfg.config_result_read()["status"], "none")   # serving it wrote nothing

    def test_a_failing_decision_record_never_stops_the_restore(self):
        self.arm({"wifi_ssid": "Invalid"})
        with mock.patch.object(cfg, "config_result_write", side_effect=RuntimeError("boom")):
            self.assertTrue(cfg.rollback_pending_config("authentication_failed"))
        self.assertEqual(cfg.CONFIG["wifi_ssid"], "Old")
        self.assertFalse(cfg.pending_config_active())
        self.assertFalse(cfg.rollback_open())

    def test_unrecoverable_last_good_without_a_pending_file_never_overwrites_connected(self):
        self.assertTrue(cfg.config_result_write("connected", details={"ssid": "Old"}))
        with open(cfg.LAST_GOOD_CONFIG_FILE + ".tmp", "w") as fh:
            json.dump(cfg._persisted_config(), fh)
        real = cfg_store.os.rename

        def eio(src, dst):
            if src == cfg.LAST_GOOD_CONFIG_FILE + ".tmp":
                raise OSError(5, "EIO")
            return real(src, dst)
        with mock.patch.object(cfg_store.os, "rename", side_effect=eio):
            cfg.resolve_interrupted_apply()
            self.assertTrue(cfg.rollback_open())                    # a staging file is unrecovered
            self.assertFalse(cfg.retry_rollback())
        self.assertEqual(cfg.config_result_read()["details"], {"ssid": "Old"})
        self.assertTrue(cfg.retry_rollback())                       # recovered, nothing to roll back
        self.assertFalse(cfg.rollback_open())
        self.assertEqual(cfg.config_result_read()["details"], {"ssid": "Old"})

    def test_rollback_open_checks_the_files_before_the_flags(self):
        self.arm({"wifi_ssid": "Invalid"})
        os.rename(cfg.PENDING_CONFIG_FILE, cfg.PENDING_CONFIG_FILE + ".tmp")
        real = cfg_store.os.rename

        def eio(src, dst):
            if src == cfg.PENDING_CONFIG_FILE + ".tmp":
                raise OSError(5, "EIO")
            return real(src, dst)
        with mock.patch.object(cfg_store.os, "rename", side_effect=eio):
            self.assertTrue(cfg.rollback_open())        # the first look already sees the failure
        self.assertFalse(cfg.rollback_open())           # recovered, nothing decided: no stale block
        self.assertTrue(cfg.pending_config_active())

    def call_config(self, path, method="POST", body=b""):
        writer, reboots = FakeHttpWriter(), []

        async def fake_reboot():
            reboots.append(path)
        with mock.patch.object(web, "_admin_allowed", return_value=True), \
                mock.patch.object(web, "do_soft_reboot_deferred", fake_reboot):
            asyncio.run(web._handle_config_request(writer, path, method, "", body, False, {}))
        raw = bytes(writer.data)
        head, _, payload = raw.partition(b"\r\n\r\n")
        return head.split(b"\r\n", 1)[0], payload, reboots

    def call_settings(self, handler, body):
        writer = FakeHttpWriter()
        with mock.patch.object(web, "_admin_allowed", return_value=True), \
                mock.patch.object(web.app, "reevaluate_gate"):
            asyncio.run(handler(writer, "PUT", body, False, {}))
        return bytes(writer.data).split(b"\r\n", 1)[0]

    def unreadable(self, raw=b"{ torn"):
        with open("config.json", "wb") as fh:
            fh.write(raw)
        cfg.load_config()
        self.assertTrue(cfg.config_file_protected())
        return raw

    def file_bytes(self):
        with open("config.json", "rb") as fh:
            return fh.read()

    def call_live(self, handler, method, body):
        writer = FakeHttpWriter()
        with mock.patch.object(web, "_admin_allowed", return_value=True), \
                mock.patch.object(web.app, "reevaluate_gate"):
            asyncio.run(handler(writer, method, body, False, {}))
        head, _, payload = bytes(writer.data).partition(b"\r\n\r\n")
        return head.split(b"\r\n", 1)[0], payload

    def test_live_puts_are_refused_while_config_json_is_unreadable(self):        # I-A
        raw = self.unreadable()
        before = dict(cfg.CONFIG)
        gate = json.dumps(dict(web.measurement_gate(), min_satellites=30)).encode()
        for handler, method, body in (
                (web._handle_measurement_gate_request, "PUT", gate),
                (web._handle_measurement_gate_request, "DELETE", b""),
                (web._handle_field_setup_request, "PUT", b'{"antenna_height_m": 1.8}'),
                (web._handle_tcp_auth_request, "PUT", b'{"required": false}')):
            with self.subTest(handler=handler.__name__, method=method):
                status, payload = self.call_live(handler, method, body)
                self.assertIn(b"503", status)
                self.assertEqual(json.loads(payload)["error"], "config_file_unreadable")
                self.assertEqual(cfg.CONFIG, before)
                self.assertEqual(self.file_bytes(), raw)
                self.assertTrue(cfg.config_file_protected())

    def test_a_non_deliberate_live_save_never_releases_the_protection(self):     # I-A
        raw = self.unreadable()
        before = dict(cfg.CONFIG)
        self.assertFalse(cfg.apply_live({"measurement_min_satellites": 9}))
        self.assertEqual(cfg.CONFIG, before)
        self.assertEqual(self.file_bytes(), raw)
        self.assertTrue(cfg.config_file_protected())

    def test_a_wifi_only_put_keeps_the_protection(self):                         # I-A
        raw = self.unreadable()
        status, _payload, _ = self.call_config("/api/config", "PUT",
                                               json.dumps({"wifi_ssid": "New"}).encode())
        self.assertIn(b"202", status)
        self.assertEqual(self.file_bytes(), raw)
        self.assertTrue(cfg.config_file_protected())

    def test_a_config_put_with_caster_keys_releases_the_protection(self):        # I-A
        self.unreadable()
        status, _payload, _ = self.call_config("/api/config", "PUT",
                                               json.dumps({"ntrip_host": "new.example"}).encode())
        self.assertIn(b"202", status)
        self.assertFalse(cfg.config_file_protected())
        with open("config.json") as fh:
            self.assertEqual(json.load(fh)["ntrip_host"], "new.example")

    def test_the_protection_is_listed_in_config_warnings_until_released(self):    # M-2
        self.unreadable()
        entries = [w for w in cfg.CONFIG_WARNINGS if w["key"] == "config_file"]
        self.assertEqual(len(entries), 1)
        self.assertIn("config.json", entries[0]["message"])
        self.assertIs(web.CONFIG_WARNINGS, cfg.CONFIG_WARNINGS)    # /status, /api/ui/live
        self.assertIsNotNone(cfg.stage_config({"ntrip_host": "new.example"}))
        self.assertEqual([w for w in cfg.CONFIG_WARNINGS if w["key"] == "config_file"], [])

    def test_a_failed_deliberate_write_protects_the_file_again(self):           # M-1
        raw = self.unreadable()
        with mock.patch.object(cfg_store, "atomic_dump", return_value=False):
            self.assertIsNone(cfg.stage_config({"ntrip_host": "new.example"}))
        self.assertTrue(cfg.config_file_protected())
        self.assertEqual([w["key"] for w in cfg.CONFIG_WARNINGS], ["config_file"])
        self.assertEqual(self.file_bytes(), raw)
        cfg.stage_config({"wifi_ssid": "New"})
        real = cfg_store.atomic_dump
        with mock.patch.object(cfg_store, "atomic_dump",
                               side_effect=lambda t, d, l: t != "config.json" and real(t, d, l)):
            self.assertFalse(cfg.apply_staged_config())         # arm fails at config.json
        self.assertTrue(cfg.config_file_protected())
        self.assertEqual(self.file_bytes(), raw)
        with mock.patch.object(cfg_store, "atomic_dump", return_value=False):
            self.assertFalse(cfg.factory_reset())
        self.assertTrue(cfg.config_file_protected())
        self.assertEqual(self.file_bytes(), raw)

    def test_a_rollback_over_an_unreadable_file_writes_the_whole_last_good(self):  # M-3
        cfg.CONFIG["ntrip_host"] = "caster.example"
        cfg.CONFIG["measurement_min_satellites"] = 12
        self.assertTrue(cfg.save_config())
        self.arm({"wifi_ssid": "New"})
        with open(cfg.LAST_GOOD_CONFIG_FILE) as fh:
            last_good = json.load(fh)
        # Verification boot: config.json became unreadable, RAM holds the defaults.
        cfg.CONFIG.clear(); cfg.CONFIG.update(cfg.DEFAULT_CONFIG)
        raw = self.unreadable()
        self.assertTrue(cfg.rollback_pending_config("authentication_failed"))
        with open("config.json") as fh:
            stored = json.load(fh)
        self.assertEqual(stored, last_good)
        for key in ("wifi_ssid", "wifi_pass", "ntrip_host", "measurement_min_satellites"):
            self.assertEqual(cfg.CONFIG[key], last_good[key], key)
        self.assertEqual((cfg.CONFIG["wifi_ssid"], cfg.CONFIG["ntrip_host"]), ("Old", "caster.example"))
        self.assertFalse(cfg.config_file_protected())
        self.assertEqual([w for w in cfg.CONFIG_WARNINGS if w["key"] == "config_file"], [])
        self.assertFalse(cfg.pending_config_active())
        self.assertFalse(cfg.rollback_open())
        with open("config.json.unreadable", "rb") as fh:
            self.assertEqual(fh.read(), raw)

    def test_a_rollback_over_a_configuration_read_in_place_keeps_live_changes(self):
        # Codex r5: recovery failed but .previous was read in place - RAM holds
        # real settings, so only the changed Wi-Fi keys go back.
        self.arm({"wifi_ssid": "New"})
        cfg.CONFIG["measurement_min_satellites"] = 13               # a live change after arming
        with open("config.json") as fh:
            current = json.load(fh)
        current["measurement_min_satellites"] = 13
        os.remove("config.json")
        with open("config.json.previous", "w") as fh:
            json.dump(current, fh)
        with mock.patch.object(cfg_store, "recover_atomic", side_effect=OSError(5, "EIO")):
            cfg.load_config()
        self.assertTrue(cfg.config_file_protected())
        self.assertTrue(cfg.rollback_pending_config("authentication_failed"))
        with open("config.json") as fh:
            stored = json.load(fh)
        self.assertEqual(stored["wifi_ssid"], "Old")                  # rolled back
        self.assertEqual(stored["measurement_min_satellites"], 13)    # live change kept
        self.assertEqual(cfg.CONFIG["measurement_min_satellites"], 13)
        self.assertFalse(cfg.config_file_protected())
        self.assertFalse(cfg.rollback_open())

    def test_a_failed_rollback_write_keeps_the_protection(self):                   # M-3
        self.arm({"wifi_ssid": "New"})
        raw = self.unreadable()
        real = cfg_store.atomic_dump
        with mock.patch.object(cfg_store, "atomic_dump",
                               side_effect=lambda t, d, l: t != "config.json" and real(t, d, l)):
            self.assertFalse(cfg.rollback_pending_config("authentication_failed"))
        self.assertTrue(cfg.config_file_protected())
        self.assertTrue(cfg.rollback_open())
        self.assertEqual(self.file_bytes(), raw)

    def test_no_open_rollback_never_releases_the_protection(self):                 # M-3
        self.unreadable()
        self.assertTrue(cfg.complete_rollback())                 # nothing to restore
        self.assertTrue(cfg.config_file_protected())

    def test_rollback_keeps_live_changes_made_before_and_after_apply(self):
        self.call_config("/api/config", "PUT", json.dumps({"wifi_ssid": "Invalid"}).encode())
        gate = dict(web.measurement_gate(), min_satellites=30)
        self.assertIn(b"200", self.call_settings(web._handle_measurement_gate_request,
                                                 json.dumps(gate).encode()))
        self.assertIn(b"202", self.call_config("/api/config/apply", body=b"{}")[0])
        cfg.load_config()                                    # the apply restart
        self.assertIn(b"200", self.call_settings(web._handle_field_setup_request,
                                                 b'{"antenna_height_m": 1.8}'))
        self.assertIn(b"200", self.call_settings(web._handle_tcp_auth_request, b'{"required": false}'))
        self.assertTrue(cfg.rollback_pending_config("authentication_failed"))
        with open("config.json") as fh:
            stored = json.load(fh)
        for source in (cfg.CONFIG, stored):
            self.assertEqual(source["wifi_ssid"], "Old")
            self.assertEqual(source["measurement_min_satellites"], 30)
            self.assertEqual(source["antenna_height_m"], 1.8)
            self.assertFalse(source["nmea_tcp_auth_required"])
        self.assertEqual(cfg.config_result_read()["details"],
                         {"code": "authentication_failed", "restored": ["wifi_ssid"]})

    def test_ntrip_only_change_is_saved_now_and_applied_by_a_verified_restart(self):
        status, payload, reboots = self.call_config(
            "/api/config", "PUT", json.dumps({"wifi_ssid": "Old", "ntrip_host": "new.example"}).encode())
        self.assertIn(b"202", status)
        self.assertEqual(json.loads(payload), {"pending": False, "restart_required": True,
                                               "applied": ["ntrip_host"], "staged": [],
                                               "staging_revision": cfg.staging_revision()})
        with open("config.json") as fh:
            self.assertEqual(json.load(fh)["ntrip_host"], "new.example")
        self.assertFalse(cfg.pending_config_active())
        status, payload, reboots = self.call_config("/api/config/apply", body=b"{}")
        self.assertIn(b"202", status)
        self.assertEqual(reboots, ["/api/config/apply"])
        self.assertTrue(cfg.pending_config_active())
        self.assertEqual(cfg.config_result_read()["status"], "pending")
        cfg.commit_pending_config("Old")
        self.assertEqual(cfg.config_result_read()["status"], "connected")

    # --- #23 part b: saved caster settings handed to the running NTRIP client ---

    def reloader(self, *results):
        """Register a task reloader answering with results in turn (the last one repeats).

        An exception instance is raised instead of returned; self.reloads counts calls.
        """
        self.reloads = []

        def reload():
            result = results[min(len(self.reloads), len(results) - 1)]
            self.reloads.append(result)
            if isinstance(result, BaseException):
                raise result
            return result
        cfg.set_task_reloader(reload)

    def full_rollback_setup(self):
        # The situation of test_a_rollback_over_an_unreadable_file_writes_the_whole_last_good.
        cfg.CONFIG["ntrip_host"] = "caster.example"
        self.assertTrue(cfg.save_config())
        self.arm({"wifi_ssid": "New"})
        cfg.CONFIG.clear(); cfg.CONFIG.update(cfg.DEFAULT_CONFIG)
        self.unreadable()

    def test_caster_change_reloads_the_running_client_without_a_restart(self):
        seen = []

        def reload():
            with open("config.json") as fh:
                seen.append(json.load(fh)["ntrip_host"])
            return True
        cfg.set_task_reloader(reload)
        summary = cfg.stage_config({"ntrip_host": "new.example"})
        self.assertEqual(seen, ["new.example"])                    # after the durable save
        self.assertEqual((summary["applied"], summary["restart_required"]), (["ntrip_host"], False))
        self.assertFalse(cfg.config_restart_required())
        cfg.stage_config({"ntrip_host": "new.example"})            # unchanged: no second reload
        cfg.apply_live({"antenna_height_m": 1.5})                  # live key: no reload
        self.assertEqual(len(seen), 1)

    def test_failed_save_never_reloads(self):
        self.reloader(True)
        before = cfg.CONFIG["ntrip_host"]
        with mock.patch.object(cfg, "_atomic_dump", return_value=False):
            self.assertFalse(cfg.apply_live({"ntrip_host": "new.example"}))
        self.assertEqual(self.reloads, [])
        self.assertEqual(cfg.CONFIG["ntrip_host"], before)
        self.assertFalse(cfg.config_restart_required())

    def test_reloader_failure_falls_back_to_a_restart(self):
        for result in (False, RuntimeError("synthetic reload failure")):
            with self.subTest(result=result):
                self.fresh()
                self.reloader(result)
                with mock.patch.object(cfg, "log") as log:
                    status, payload, _ = self.call_config(
                        "/api/config", "PUT", json.dumps({"ntrip_host": "new.example"}).encode())
                errors = [c.args for c in log.call_args_list if c.args[0] == "ERROR"]
                self.assertEqual(len(self.reloads), 1)
                self.assertIn(b"202", status)
                self.assertTrue(json.loads(payload)["restart_required"])
                self.assertTrue(cfg.config_restart_required())
                if isinstance(result, BaseException):
                    self.assertEqual(errors, [("ERROR", "SYS",
                                               "NTRIP reload failed: synthetic reload failure")])
                else:
                    self.assertEqual(errors, [])
                status, _, reboots = self.call_config("/api/config/apply", body=b"{}")
                self.assertIn(b"202", status)                  # the verified restart of #23a
                self.assertEqual(reboots, ["/api/config/apply"])
                self.assertTrue(cfg.pending_config_active())

    def test_a_successful_reload_clears_an_earlier_failed_handover(self):  # Codex 5
        self.reloader(False, True)
        cfg.stage_config({"ntrip_host": "a.example"})
        self.assertTrue(cfg.config_restart_required())
        summary = cfg.stage_config({"ntrip_mount": "B"})
        self.assertFalse(summary["restart_required"])
        self.assertFalse(cfg.config_restart_required())
        self.assertEqual(self.reloads, [False, True])          # exactly one successful handover

    def test_a_successful_reload_clears_an_earlier_failed_handover_over_http(self):  # Codex 5
        self.reloader(False, True)
        status, _, _ = self.call_config("/api/config", "PUT",
                                        json.dumps({"ntrip_host": "a.example"}).encode())
        self.assertIn(b"202", status)
        status, payload, _ = self.call_config("/api/config", "PUT",
                                              json.dumps({"ntrip_mount": "B"}).encode())
        self.assertIn(b"200", status)
        self.assertFalse(json.loads(payload)["restart_required"])
        status, payload, reboots = self.call_config("/api/config/apply", body=b"{}")
        self.assertIn(b"409", status)
        self.assertIn(b"nothing_to_apply", payload)
        self.assertEqual(reboots, [])

    def test_a_successful_reload_keeps_the_restart_of_a_staged_wifi(self):  # Codex 5
        status, _, _ = self.call_config("/api/config", "PUT",
                                        json.dumps({"wifi_ssid": "New"}).encode())
        self.assertIn(b"202", status)
        self.reloader(False, True)
        cfg.stage_config({"ntrip_host": "a.example"})
        summary = cfg.stage_config({"ntrip_mount": "B"})
        self.assertTrue(summary["restart_required"])           # the staged Wi-Fi
        self.assertFalse(cfg._RESTART_REQUIRED)
        self.assertEqual(self.reloads, [False, True])
        cfg.discard_staged_config()
        self.assertFalse(cfg.config_restart_required())

    def test_put_of_a_caster_change_answers_200_without_restart(self):
        self.reloader(True)
        status, payload, reboots = self.call_config(
            "/api/config", "PUT", json.dumps({"wifi_ssid": "Old", "ntrip_host": "new.example"}).encode())
        self.assertIn(b"200", status)
        self.assertEqual(json.loads(payload), {"pending": False, "restart_required": False,
                                               "applied": ["ntrip_host"], "staged": [],
                                               "staging_revision": cfg.staging_revision()})
        self.assertEqual(self.reloads, [True])
        status, payload, apply_reboots = self.call_config("/api/config/apply", body=b"{}")
        self.assertIn(b"409", status)
        self.assertIn(b"nothing_to_apply", payload)
        self.assertEqual(reboots + apply_reboots, [])
        self.assertFalse(cfg.pending_config_active())

    def test_wifi_and_caster_in_one_put_reload_the_caster_and_stage_the_wifi(self):
        self.reloader(True)
        summary = cfg.stage_config({"wifi_ssid": "New", "ntrip_host": "new.example"})
        self.assertEqual(self.reloads, [True])
        self.assertEqual(summary["staged"], ["wifi_ssid"])
        self.assertTrue(summary["restart_required"])           # the staging
        self.assertTrue(cfg.apply_staged_config())
        self.assertTrue(cfg.rollback_pending_config("authentication_failed"))
        self.assertEqual((cfg.CONFIG["wifi_ssid"], cfg.CONFIG["ntrip_host"]), ("Old", "new.example"))
        self.assertEqual(self.reloads, [True])                 # the rollback changed no caster
        self.assertFalse(cfg.config_restart_required())

    def test_full_rollback_reloads_changed_caster_values(self):
        self.full_rollback_setup()
        self.reloader(True)
        self.assertTrue(cfg.rollback_pending_config("authentication_failed"))
        self.assertEqual(self.reloads, [True])
        self.assertEqual(cfg.CONFIG["ntrip_host"], "caster.example")
        self.assertFalse(cfg.config_restart_required())

    def test_full_rollback_hands_over_even_when_the_cleanup_fails(self):  # Codex 4
        self.full_rollback_setup()
        self.reloader(True)
        with mock.patch.object(cfg_store, "finish", side_effect=[False, True]):
            self.assertFalse(cfg.rollback_pending_config("authentication_failed"))
            self.assertEqual(cfg.CONFIG["ntrip_host"], "caster.example")    # RAM changed
            self.assertEqual(self.reloads, [True])
            self.assertTrue(cfg.retry_rollback())
        self.assertEqual(self.reloads, [True])                 # already handed over
        self.assertFalse(cfg.config_restart_required())

    def test_a_failed_rollback_handover_is_retried(self):                 # Codex 4
        self.full_rollback_setup()
        self.reloader(False, True)
        with mock.patch.object(cfg_store, "finish", side_effect=[False, True]):
            self.assertFalse(cfg.rollback_pending_config("authentication_failed"))
            self.assertTrue(cfg.config_restart_required())
            self.assertTrue(cfg.retry_rollback())
        self.assertEqual(self.reloads, [False, True])
        self.assertFalse(cfg.config_restart_required())

    def test_rollback_without_a_reloader_changes_no_restart_flag(self):
        self.full_rollback_setup()
        self.assertTrue(cfg.rollback_pending_config("authentication_failed"))
        self.assertEqual(cfg.CONFIG["ntrip_host"], "caster.example")
        self.assertFalse(cfg.config_restart_required())

    def test_put_without_changes_needs_no_restart_and_apply_refuses(self):
        status, payload, _ = self.call_config(
            "/api/config", "PUT", json.dumps({"wifi_ssid": "Old"}).encode())
        self.assertIn(b"200", status)
        self.assertFalse(json.loads(payload)["restart_required"])
        status, payload, reboots = self.call_config("/api/config/apply", body=b"{}")
        self.assertIn(b"409", status)
        self.assertIn(b"nothing_to_apply", payload)
        self.assertEqual(reboots, [])
        self.assertFalse(cfg.pending_config_active())

    def test_masked_networks_resolve_against_the_staged_list(self):
        self.call_config("/api/config", "PUT", json.dumps(
            {"wifi_networks": [{"ssid": "Field", "password": "fieldpass1"}]}).encode())
        self.call_config("/api/config", "PUT", json.dumps(
            {"wifi_networks": [{"ssid": "Field", "password_set": True}]}).encode())
        self.assertEqual(cfg.staged_config()["wifi_networks"], [["Field", "fieldpass1"]])

    def test_apply_save_and_restore_are_refused_while_connecting(self):
        cfg.stage_config({"wifi_ssid": "Second"})
        self.assertFalse(cfg.pending_config_active())           # no pending at all (ruling)
        state.app.stats["access_state"] = "CONNECTING"
        restore = json.dumps({"schema_version": 1, "kind": "esp-rtk-config-backup",
                              "config": {"wifi_ssid": "Third"}}).encode()
        for path, body in (("/api/config/apply", b"{}"), ("/api/config/restore", restore),
                           ("/save", b"wifi_ssid=Third")):
            status, payload, reboots = self.call_config(path, body=body)
            self.assertIn(b"409", status, path)
            self.assertEqual(reboots, [], path)
            self.assertFalse(cfg.pending_config_active(), path)
            if path != "/save":
                self.assertEqual(json.loads(payload)["error"], "config_verification_running", path)
        with open("config.json") as fh:
            self.assertEqual(json.load(fh)["wifi_ssid"], "Old")
        self.assertEqual(cfg.config_result_read()["status"], "none")

    def test_apply_save_and_restore_are_refused_while_a_change_is_open(self):
        restore = json.dumps({"schema_version": 1, "kind": "esp-rtk-config-backup",
                              "config": {"wifi_ssid": "Third"}}).encode()
        for prepare in ("armed", "failed"):                     # verification pending / recorded decision
            self.fresh()
            cfg.stage_config({"wifi_ssid": "First"})
            self.assertTrue(cfg.apply_staged_config())
            if prepare == "failed":
                self.assertTrue(cfg.config_result_write("failed", details={"code": "authentication_failed",
                                                                           "restored": ["wifi_ssid"]}))
            with open(cfg.PENDING_CONFIG_FILE) as fh: pending_before = json.load(fh)
            self.call_config("/api/config", "PUT", json.dumps({"wifi_ssid": "Second"}).encode())
            for path, body in (("/api/config/apply", b"{}"), ("/api/config/restore", restore),
                               ("/save", b"wifi_ssid=Third")):
                status, payload, reboots = self.call_config(path, body=body)
                with self.subTest(prepare=prepare, path=path):
                    self.assertIn(b"409", status)
                    self.assertEqual(reboots, [])
                    if path != "/save":
                        self.assertEqual(json.loads(payload)["error"], "open_change_pending")
                    with open(cfg.PENDING_CONFIG_FILE) as fh:
                        self.assertEqual(json.load(fh), pending_before)
            self.assertFalse(cfg.apply_staged_config())          # defensive refusal in cfg as well

    def test_power_cut_at_every_apply_step_never_confirms_an_unlanded_change(self):
        for works in (("Old",), ("Old", "New")):
            step = 1
            while True:
                self.fresh()
                cfg.stage_config({"wifi_ssid": "New"})
                with self.cut_at(step):
                    try:
                        done = cfg.apply_staged_config()
                    except PowerCut:
                        done = None
                self.boot(works)
                with self.subTest(works=works, step=step):
                    self.assertFalse(cfg.pending_config_active())
                    status = cfg.config_result_read()["status"]
                    self.assertIn(status, ("none", "connected", "failed"))
                    self.assertEqual(status == "connected", cfg.CONFIG["wifi_ssid"] == "New")
                    if "New" not in works:
                        self.assertEqual(cfg.CONFIG["wifi_ssid"], "Old")
                if done is not None:
                    break
                step += 1
            self.assertGreater(step, 3)                         # the cut really walked through the steps

    def test_apply_with_an_unwritable_result_arms_nothing(self):
        cfg.stage_config({"wifi_ssid": "New"})
        with self.fail_writes_to(cfg.CONFIG_RESULT_FILE):
            self.assertFalse(cfg.apply_staged_config())
        self.assertFalse(cfg.pending_config_active())
        self.assertEqual(cfg.CONFIG["wifi_ssid"], "Old")
        self.assertEqual(cfg.staged_config(), {"wifi_ssid": "New"})

    def test_failed_arm_restores_the_previous_result(self):
        self.assertTrue(cfg.config_result_write("connected", details={"ssid": "Old"}))
        cfg.stage_config({"wifi_ssid": "New"})
        with self.fail_writes_to("config.json"):
            self.assertFalse(cfg.apply_staged_config())
        self.assertEqual(cfg.config_result_read()["details"], {"ssid": "Old"})

    def test_failed_arm_without_an_earlier_result_writes_a_complete_none(self):
        self.assertFalse(os.path.exists(cfg.CONFIG_RESULT_FILE))
        cfg.stage_config({"wifi_ssid": "New"})
        with self.fail_writes_to("config.json"):
            self.assertFalse(cfg.apply_staged_config())
        self.assertEqual(cfg.config_result_read()["status"], "none")
        if os.path.exists(cfg.CONFIG_RESULT_FILE):
            with open(cfg.CONFIG_RESULT_FILE) as fh:
                stored = json.load(fh)
            self.assertEqual(set(stored), {"schema_version", "status", "details", "uptime_sec",
                                           "boot_id", "apply_id"})

    def test_failed_save_and_restore_leave_nothing_staged(self):
        restore = json.dumps({"schema_version": 1, "kind": "esp-rtk-config-backup",
                              "config": {"wifi_ssid": "Third"}}).encode()
        for path, body in (("/save", b"wifi_ssid=Third"), ("/api/config/restore", restore)):
            self.fresh()
            with self.fail_writes_to("config.json"):
                status, _, reboots = self.call_config(path, body=body)
            with self.subTest(path=path):
                self.assertIn(b"500", status)
                self.assertEqual(reboots, [])
                self.assertEqual(cfg.staged_config(), {})        # the user saw the failure
                self.assertFalse(cfg.pending_config_active())
                self.assertEqual(cfg.CONFIG["wifi_ssid"], "Old")
                status, payload, reboots = self.call_config("/api/config/apply", body=b"{}")
                self.assertEqual(json.loads(payload)["error"], "nothing_to_apply")
                self.assertEqual(reboots, [])

    def test_the_change_check_recovers_the_files_once_per_request(self):
        real, calls, errors = cfg_store.recover_staging, [], []

        def counting(*args):
            calls.append(args)
            return real(*args)

        def failing(*args):
            calls.append(args)
            return False
        for flag in (False, True):                            # no change / a rollback open in RAM
            del calls[:]
            cfg._ROLLBACK_OPEN = flag
            with mock.patch.object(cfg_store, "recover_staging", side_effect=counting):
                self.assertEqual(web._open_change_pending(), flag)
            self.assertEqual(len(calls), 1, flag)
        cfg._ROLLBACK_OPEN = False
        del calls[:]
        with mock.patch.object(cfg_store, "recover_staging", side_effect=failing), \
                mock.patch.object(cfg, "log", side_effect=lambda sev, *a, **k: errors.append(sev)):
            self.assertEqual(web._config_refusal()[0], "open_change_pending")
        self.assertEqual(len(calls), 1)
        self.assertEqual(errors.count("ERROR"), 1)

    def test_failed_save_and_restore_keep_an_earlier_restart_requirement(self):
        restore = json.dumps({"schema_version": 1, "kind": "esp-rtk-config-backup",
                              "config": {"ntrip_host": "new.example"}}).encode()
        for path, body in (("/save", b"wifi_ssid=Old&ntrip_host=new.example"),
                           ("/api/config/restore", restore)):
            self.fresh()
            cfg.discard_staged_config()
            with self.subTest(path=path):
                status, payload, _ = self.call_config(
                    "/api/config", "PUT", json.dumps({"ntrip_host": "new.example"}).encode())
                self.assertTrue(json.loads(payload)["restart_required"])   # caster saved, task still old
                with self.fail_writes_to("config.json"):                   # the same value, arm fails
                    status, _, reboots = self.call_config(path, body=body)
                self.assertIn(b"500", status)
                self.assertEqual(reboots, [])
                self.assertTrue(cfg.config_restart_required())
                status, payload, reboots = self.call_config("/api/config/apply", body=b"{}")
                self.assertIn(b"202", status)
                self.assertEqual(reboots, ["/api/config/apply"])
            cfg.discard_staged_config()

    def test_save_and_restore_ignore_and_drop_an_earlier_staging(self):
        restore = json.dumps({"schema_version": 1, "kind": "esp-rtk-config-backup",
                              "config": {"wifi_ssid": "Old"}}).encode()
        for path, body in (("/save", b"wifi_ssid=Old&wifi_pass="), ("/api/config/restore", restore)):
            self.fresh()
            self.call_config("/api/config", "PUT", json.dumps({"wifi_pass": "staged-password-1"}).encode())
            status, _, reboots = self.call_config(path, body=body)
            self.assertIn(b"20", status, path)
            self.assertEqual(reboots, [path])
            with open(cfg.PENDING_CONFIG_FILE) as fh:
                self.assertEqual(json.load(fh)["wifi_pass"], "legacy-password", path)
            self.assertEqual(cfg.staged_config(), {}, path)

    def test_failed_arm_leaves_no_half_applied_change(self):
        real = cfg._atomic_dump
        def fail_config_json(target, data):
            return False if target == "config.json" else real(target, data)
        cfg.stage_config({"wifi_ssid": "New"})
        with mock.patch.object(cfg, "_atomic_dump", side_effect=fail_config_json):
            self.assertFalse(cfg.apply_staged_config())
        self.assertFalse(cfg.pending_config_active())
        self.assertFalse(os.path.exists(cfg.LAST_GOOD_CONFIG_FILE))
        self.assertEqual(cfg.CONFIG["wifi_ssid"], "Old")
        self.assertEqual(cfg.staged_config(), {"wifi_ssid": "New"})   # retry possible
        self.assertTrue(cfg.apply_staged_config())

    def test_restore_and_save_stage_apply_and_restart(self):
        backup = json.dumps({"schema_version": 1, "kind": "esp-rtk-config-backup",
                             "config": {"wifi_ssid": "Restored", "ntrip_host": "r.example"}}).encode()
        status, payload, reboots = self.call_config("/api/config/restore", body=backup)
        self.assertIn(b"202", status)
        self.assertEqual(json.loads(payload), {"restored": True, "schema_version": 1,
                                               "sections": ["connections"], "kept": [],
                                               "apply_id": None})
        self.assertEqual(reboots, ["/api/config/restore"])
        with open(cfg.PENDING_CONFIG_FILE) as fh:
            self.assertEqual(json.load(fh)["wifi_ssid"], "Restored")
        self.assertEqual(cfg.CONFIG["ntrip_host"], "r.example")

    def test_save_form_refuses_nine_networks_and_short_passwords(self):
        nine = "%0A".join("Net%d%%3Apassword%d" % (i, i) for i in range(9))
        for networks in (nine, "Field%3Ashort"):
            status, payload, reboots = self.call_config(
                "/save", body=("wifi_ssid=Old&wifi_networks=" + networks).encode())
            self.assertIn(b"400", status, networks)
            self.assertIn(b"Rejected", payload)
            self.assertEqual(reboots, [])
            self.assertFalse(cfg.pending_config_active())

    def test_json_put_keeps_the_network_error_code(self):
        body = json.dumps({"wifi_networks": [{"ssid": "Net%d" % i, "password": "password%d" % i}
                                             for i in range(9)]}).encode()
        status, payload, _ = self.call_config("/api/config", "PUT", body)
        self.assertIn(b"400", status)
        self.assertEqual(json.loads(payload)["error"], "invalid_wifi_networks")

    def test_masked_row_with_a_non_text_ssid_is_rejected(self):
        for ssid in ([], {}, 7, None):
            body = json.dumps({"wifi_networks": [{"ssid": ssid, "password_set": True}]}).encode()
            status, payload, _ = self.call_config("/api/config", "PUT", body)
            with self.subTest(ssid=ssid):
                self.assertIn(b"400", status)
                self.assertEqual(json.loads(payload)["error"], "invalid_wifi_networks")
        self.assertEqual(cfg.staged_config(), {})

    def test_null_secret_keeps_the_stored_value_on_put_and_restore(self):
        cfg.CONFIG["ntrip_pass"] = "caster-secret"
        self.assertTrue(cfg.save_config())
        status, payload, _ = self.call_config("/api/config", "PUT", json.dumps(
            {"wifi_pass": None, "ntrip_pass": None, "ntrip_fallback_pass": None}).encode())
        self.assertIn(b"200", status)
        self.assertEqual(json.loads(payload)["applied"], [])
        self.assertEqual(cfg.CONFIG["ntrip_pass"], "caster-secret")
        self.assertEqual(cfg.staged_config(), {})
        backup = json.dumps({"schema_version": 1, "kind": "esp-rtk-config-backup",
                             "config": {"wifi_ssid": "Old", "wifi_pass": None,
                                        "ntrip_pass": None}}).encode()
        status, _, reboots = self.call_config("/api/config/restore", body=backup)
        self.assertIn(b"202", status)
        self.assertEqual(reboots, ["/api/config/restore"])
        with open(cfg.PENDING_CONFIG_FILE) as fh:
            candidate = json.load(fh)
        self.assertEqual(candidate["wifi_pass"], "legacy-password")
        self.assertEqual(candidate["ntrip_pass"], "caster-secret")

    def test_restore_refuses_nine_networks_and_short_passwords(self):
        nine = [["Net%d" % i, "password%d" % i] for i in range(9)]
        for networks in (nine, [["Field", "short"]]):
            backup = {"schema_version": 1, "kind": "esp-rtk-config-backup",
                      "config": {"wifi_ssid": "Old", "wifi_networks": networks}}
            values, error = web.validate_backup(backup)
            self.assertIsNone(values)
            self.assertTrue(error)
            status, payload, reboots = self.call_config("/api/config/restore",
                                                        body=json.dumps(backup).encode())
            with self.subTest(networks=len(networks)):
                self.assertIn(b"400", status)
                self.assertEqual(json.loads(payload)["error"], "invalid_backup")
                self.assertEqual(reboots, [])
                self.assertFalse(cfg.pending_config_active())

    def test_save_form_refuses_a_fractional_port(self):
        status, payload, reboots = self.call_config(
            "/save", body=b"wifi_ssid=Old&ntrip_host=c.example&ntrip_mount=M&ntrip_port=2101.5")
        self.assertIn(b"400", status)
        self.assertIn(b"ntrip_port", payload)
        self.assertEqual(reboots, [])
        self.assertFalse(cfg.pending_config_active())

    def test_v9_port_is_migrated_to_dual_port(self):
        with open("config.json", "w") as fh:
            json.dump({"nmea_tcp_port": 2947, "config_schema_version": 1}, fh)
        cfg.CONFIG.clear()
        cfg.CONFIG.update(cfg.DEFAULT_CONFIG)
        cfg.load_config()
        self.assertEqual(cfg.CONFIG["nmea_tcp_port"], 10110)
        self.assertEqual(cfg.CONFIG["nmea_tcp_legacy_port"], 2947)


class TestStateContracts(unittest.TestCase):
    def test_access_state(self):
        state.app.set_access_state("ONLINE")
        self.assertEqual(state.app.stats["access_state"], "ONLINE")
        with self.assertRaises(ValueError):
            state.app.set_access_state("IRGENDWAS")

    def test_ntrip_state(self):
        state.app.set_ntrip_state("streaming")
        self.assertEqual(state.app.stats["ntrip_state"], "streaming")
        with self.assertRaises(ValueError):
            state.app.set_ntrip_state("stale")
        for rejected in ("exhausted", "caster_error", "protocol_error"):
            with self.assertRaises(ValueError):
                state.app.set_ntrip_state(rejected)


class FakeReader:
    def __init__(self, data):
        self.data = bytearray(data)

    async def read(self, size=-1):
        if not self.data:
            return b""
        if size < 0:
            size = len(self.data)
        result = bytes(self.data[:size])
        del self.data[:size]
        return result


class FakeHttpWriter:
    def __init__(self, peer="203.0.113.10", local="203.0.113.20",
                 sockname_supported=True):
        self.peer = (peer, 43210)
        self.local = (local, 80)
        self.sockname_supported = sockname_supported
        self.data = bytearray()

    def get_extra_info(self, key):
        if key == "peername":
            return self.peer
        if key == "sockname" and self.sockname_supported:
            return self.local
        return None

    def write(self, data):
        self.data.extend(data)

    async def drain(self):
        pass

    def close(self):
        pass

    async def wait_closed(self):
        pass


def http_request(raw, **writer_args):
    writer = FakeHttpWriter(**writer_args)
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(web.handle_http_client(FakeReader(raw), writer))
    finally:
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending,
                                                   return_exceptions=True))
        loop.close()
    return bytes(writer.data)


class TestHttpAccess(unittest.TestCase):
    def setUp(self):
        access.reset()
        self.old_identity = state.app.identity
        self.old_sta = state.app.stats.get("sta_ip")
        state.app.identity = {
            "device_id": "A1B2C3D4E5F6", "display_name": "RTK-D4E5F6",
            "ble_name": "RTK-D4E5F6", "hostname": "rtk-d4e5f6",
            "device_code": "ABCDEF123456",
        }
        state.app.stats["sta_ip"] = "203.0.113.20"

    def tearDown(self):
        access.reset()
        state.app.identity = self.old_identity
        state.app.stats["sta_ip"] = self.old_sta

    def test_access_contract_is_public(self):
        response = http_request(b"GET /api/access HTTP/1.0\r\n\r\n")
        self.assertIn(b"200 OK", response)
        self.assertIn(b'A1B2C3D4E5F6', response)
        self.assertNotIn(b'ABCDEF123456', response)

    def test_setup_on_lan_requires_login(self):
        response = http_request(b"GET /setup HTTP/1.0\r\n\r\n")
        self.assertIn(b"401 Unauthorized", response)
        self.assertIn(b"device code", response)
        self.assertIn(b'id="login-device-code" type="password"', response)
        self.assertIn(b">Show</button>", response)
        self.assertIn(('src="/pages.js?v=%s"' % assets.ASSET_VERSION).encode(), response)
        with open("pages.js", "rb") as source:
            script = source.read()
        self.assertIn(b"location.hash", script)
        self.assertIn(b"history.replaceState", script)

    def test_locked_pages_remember_safe_login_redirect(self):
        root = http_request(b"GET / HTTP/1.0\r\n\r\n")
        self.assertIn(b"401 Unauthorized", root)
        self.assertIn(b'data-page="app-v2-login"', root)
        for path in ("/setup", "/diagnostics", "/label"):
            response = http_request(("GET %s HTTP/1.0\r\n\r\n" % path).encode())
            self.assertIn(b"401 Unauthorized", response)
            self.assertIn(('name="next" value="%s"' % path).encode(), response)

    def test_removed_interfaces_return_not_found_with_or_without_session(self):
        _session, headers = self._session_headers()
        for path in ("/v0", "/v0-base.css", "/v0-dashboard.css",
                     "/v0-dashboard.js", "/v0-i18n.js",
                     "/dashboard.js", "/dashboard.css", "/v1"):
            for auth in (b"", headers):
                response = http_request(("GET %s HTTP/1.0\r\n" % path).encode()
                                        + auth + b"\r\n")
                self.assertIn(b"404 Not Found", response)

    def test_portal_navigation_is_only_visible_after_login(self):
        locked = http_request(b"GET / HTTP/1.0\r\n\r\n")
        self.assertIn(b"401 Unauthorized", locked)
        self.assertNotIn(b'class="mobile-nav"', locked)
        _session, headers = self._session_headers()
        unlocked = http_request(b"GET / HTTP/1.0\r\n" + headers + b"\r\n")
        self.assertIn(b"200 OK", unlocked)
        self.assertIn(b'class="mobile-nav"', unlocked)

    def test_portal_tab_is_preserved_by_login(self):
        locked = http_request(
            b"GET /?return=%2F%23tracking HTTP/1.0\r\n\r\n")
        self.assertIn(b"401 Unauthorized", locked)
        self.assertIn(b'data-page="app-v2-login"', locked)
        self.assertEqual(web._safe_return_path("/#tracking"), "/#tracking")

    def test_form_login_returns_to_the_previous_page(self):
        body = b"device_code=ABCDEF123456&next=%2Fdiagnostics"
        response = http_request(
            b"POST /api/session HTTP/1.0\r\nContent-Type: application/x-www-form-urlencoded\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
        self.assertIn(b"303 See Other", response)
        self.assertIn(b"Location: /diagnostics", response)

    def test_login_redirect_rejects_external_and_unknown_targets(self):
        for target in ("https://evil.example", "//evil.example", "/api/label"):
            self.assertEqual(web._safe_return_path(target), "/setup")

    def test_login_creates_cookie_and_opens_setup(self):
        body = b'{"device_code":"ABCDEF123456"}'
        raw = (b"POST /api/session HTTP/1.0\r\nContent-Type: application/json\r\n"
               b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
        login_response = http_request(raw)
        self.assertIn(b"200 OK", login_response)
        cookie = login_response.split(b"Set-Cookie: ", 1)[1].split(b";", 1)[0]
        setup = http_request(b"GET /setup HTTP/1.0\r\nCookie: " + cookie +
                             b"\r\n\r\n")
        self.assertIn(b"200 OK", setup)
        self.assertIn(b'name="csrf_token"', setup)

    def test_registered_setup_form_is_allowed_to_save(self):
        session, error = access.login("ABCDEF123456", "ABCDEF123456",
                                      "203.0.113.9")
        self.assertIsNone(error)
        body = ("csrf_token=%s&wifi_ssid=New&wifi_networks=Field%%3APassword"
                % session["csrf"]).encode()
        raw = (b"POST /save HTTP/1.0\r\nHost: rtk-d4e5f6.local\r\n"
               b"Origin: http://rtk-d4e5f6.local\r\nCookie: rtk_session=" +
               session["token"].encode() + b"\r\nContent-Length: " +
               str(len(body)).encode() + b"\r\n\r\n" + body)
        original_apply = web.apply_config_change
        old_access = state.app.stats.get("access_state")
        web.apply_config_change = lambda _values: False  # do not schedule a test reboot
        state.app.stats["access_state"] = "ONLINE"        # not refused as connecting
        try:
            response = http_request(raw)
        finally:
            web.apply_config_change = original_apply
            state.app.stats["access_state"] = old_access
        self.assertNotIn(b"403 Forbidden", response)
        self.assertIn(b"500 Internal Server Error", response)

    def test_local_ap_address_requires_device_code(self):
        response = http_request(b"GET /setup HTTP/1.0\r\n\r\n",
                                peer="192.168.4.2", local="192.168.4.1")
        self.assertIn(b"401 Unauthorized", response)
        self.assertIn(b"device code", response)

    def test_subnet_collision_does_not_grant_admin_rights(self):
        state.app.stats["sta_ip"] = "192.168.4.55"
        response = http_request(b"GET /setup HTTP/1.0\r\n\r\n",
                                peer="192.168.4.2", local="192.168.4.1",
                                sockname_supported=False)
        self.assertIn(b"401 Unauthorized", response)

    def test_foreign_browser_origin_cannot_modify_ap(self):
        self.assertFalse(web._admin_allowed(
            True, {"host": "192.168.4.1", "origin": "https://evil.example"},
            "POST", body=b"wifi_ssid=X"))
        self.assertFalse(web._admin_allowed(
            True, {"host": "192.168.4.1", "origin": "http://192.168.4.1"},
            "POST", body=b"wifi_ssid=X"))

    def test_ap_status_and_secrets_need_session(self):
        for path in ("/status", "/health", "/rtcm", "/errors", "/api/label",
                     "/api/config/backup", "/api/tracking"):
            response = http_request(
                ("GET %s HTTP/1.0\r\n\r\n" % path).encode(),
                peer="192.168.4.2", local="192.168.4.1")
            self.assertNotIn(b"200 OK", response, path)

    def test_ap_session_opens_protected_routes(self):
        _session, headers = self._session_headers()
        response = http_request(b"GET /status HTTP/1.0\r\n" + headers + b"\r\n",
                                peer="192.168.4.2", local="192.168.4.1")
        self.assertIn(b"200 OK", response)

    def test_fragmented_long_header_is_fully_read(self):
        padding = b"x" * 1400
        response = http_request(
            b"GET /api/access HTTP/1.0\r\nX-Padding: " + padding + b"\r\n\r\n")
        self.assertIn(b"200 OK", response)

    def test_too_big_a_header_will_be_rejected(self):
        response = http_request(
            b"GET /api/access HTTP/1.0\r\nX-Padding: " + b"x" * 5000 +
            b"\r\n\r\n")
        self.assertIn(b"431 Request Header Fields Too Large", response)

    def test_too_big_body_is_not_cut_off(self):
        response = http_request(
            b"POST /save HTTP/1.0\r\nContent-Length: 5000\r\n\r\n")
        self.assertIn(b"413 Payload Too Large", response)

    def test_read_route_lehnt_mutierende_methode_ab(self):
        response = http_request(b"POST /status HTTP/1.0\r\nContent-Length: 0\r\n\r\n")
        self.assertIn(b"405 Method Not Allowed", response)

    def test_old_url_token_does_not_grant_access(self):
        old = cfg.CONFIG.get("config_token")
        cfg.CONFIG["config_token"] = "a+b"
        try:
            response = http_request(b"GET /label?token=a%2Bb HTTP/1.0\r\n\r\n")
        finally:
            cfg.CONFIG["config_token"] = old
        self.assertIn(b"401 Unauthorized", response)

    def test_safety_headers_are_on_every_response(self):
        response = http_request(b"GET /api/access HTTP/1.0\r\n\r\n")
        self.assertIn(b"X-Content-Type-Options: nosniff", response)
        self.assertIn(b"Referrer-Policy: no-referrer", response)
        self.assertIn(b"X-Frame-Options: DENY", response)

    def test_status_reports_rejected_config_values(self):
        session, _error = access.login("ABCDEF123456", "ABCDEF123456", "203.0.113.9")
        cfg.CONFIG_WARNINGS[:] = [{"key": "antenna_height_m", "message": "bad"}]
        try:
            response = http_request(
                b"GET /status HTTP/1.0\r\nCookie: rtk_session=" +
                session["token"].encode() + b"\r\n\r\n")
        finally:
            del cfg.CONFIG_WARNINGS[:]
        self.assertIn(b"200 OK", response)
        self.assertIn(b'"config_warnings": [{', response)
        self.assertIn(b"antenna_height_m", response)

    def test_admin_session_is_allowed_to_read_protected_status(self):
        session, error = access.login("ABCDEF123456", "ABCDEF123456",
                                      "203.0.113.9")
        self.assertIsNone(error)
        response = http_request(
            b"GET /status HTTP/1.0\r\nCookie: rtk_session=" +
            session["token"].encode() + b"\r\n\r\n")
        self.assertIn(b"200 OK", response)
        self.assertIn(b'application/json', response)

    def _session_headers(self, csrf=False):
        session, error = access.login("ABCDEF123456", "ABCDEF123456",
                                      "203.0.113.9")
        self.assertIsNone(error)
        headers = b"Cookie: rtk_session=" + session["token"].encode() + b"\r\n"
        if csrf:
            headers += b"X-CSRF-Token: " + session["csrf"].encode() + b"\r\n"
        return session, headers

    def test_all_protected_reading_routes_accept_admin_session(self):
        _session, headers = self._session_headers()
        for path in ("/label", "/api/label", "/api/config", "/status",
                     "/health", "/rtcm", "/errors", "/gnss"):
            response = http_request(
                ("GET %s HTTP/1.0\r\n" % path).encode() + headers + b"\r\n")
            self.assertIn(b"200 OK", response, path)

    def test_protected_reading_routes_remain_closed_without_access(self):
        for path in ("/label", "/api/label", "/api/config", "/status",
                     "/health", "/rtcm", "/errors", "/gnss"):
            response = http_request(("GET %s HTTP/1.0\r\n\r\n" % path).encode())
            self.assertNotIn(b"200 OK", response, path)
        # The /rtcm rejection names the admin session, not a token that nothing accepts.
        rtcm = http_request(b"GET /rtcm?token=anything HTTP/1.0\r\n\r\n")
        self.assertIn(b"401 Unauthorized", rtcm)
        self.assertIn(b"Administrator sign-in required.", rtcm)
        self.assertNotIn(b"oken", rtcm.split(b"\r\n\r\n", 1)[1])

    def test_config_api_masks_all_wlan_passwords(self):
        old = cfg.CONFIG.get("wifi_networks")
        cfg.CONFIG["wifi_networks"] = [["Field", "streng-secret"], ["Offen", ""]]
        _session, headers = self._session_headers()
        try:
            response = http_request(b"GET /api/config HTTP/1.0\r\n" +
                                    headers + b"\r\n")
        finally:
            cfg.CONFIG["wifi_networks"] = old
        self.assertIn(b"200 OK", response)
        self.assertNotIn(b"streng-secret", response)
        self.assertIn(b'"ssid": "Field"', response)
        self.assertIn(b'"password_set": true', response)

    def test_mutant_api_accepts_session_csrf(self):
        _session, headers = self._session_headers(csrf=True)
        body = b"{"
        response = http_request(
            b"PUT /api/config HTTP/1.0\r\nHost: rtk-d4e5f6.local\r\n"
            b"Origin: http://rtk-d4e5f6.local\r\n" + headers +
            b"Content-Length: 1\r\n\r\n" + body)
        self.assertIn(b"400 Bad Request", response)
        self.assertNotIn(b"403 Forbidden", response)

    def test_captive_webview_origin_null_accepted_form_csrf(self):
        session, headers = self._session_headers()
        body = (b"csrf_token=" + session["csrf"].encode() +
                b"&wifi_ssid=field-test-network&wifi_pass=secret12")
        old_apply = web.apply_config_change
        old_access = state.app.stats.get("access_state")
        web.apply_config_change = lambda values: True
        state.app.stats["access_state"] = "SETUP"
        try:
            response = http_request(
                b"POST /save HTTP/1.0\r\nHost: rtk-d4e5f6.local\r\n"
                b"Origin: null\r\n" + headers +
                (b"Content-Length: %d\r\n\r\n" % len(body)) + body,
                local=cfg.CONFIG["ap_ip"])
        finally:
            web.apply_config_change = old_apply
            state.app.stats["access_state"] = old_access
        self.assertIn(b"200 OK", response)
        self.assertNotIn(b"Configuration access expired", response)

    def test_login_accepts_null_origin_but_rejects_foreign_origin(self):
        body = b"device_code=ABCDEF123456"
        basis = (b"POST /api/session HTTP/1.0\r\nHost: rtk-d4e5f6.local\r\n"
                 b"Content-Type: application/x-www-form-urlencoded\r\n" +
                 (b"Content-Length: %d\r\n" % len(body)))
        old_access = state.app.stats.get("access_state")
        state.app.stats["access_state"] = "SETUP"
        accepted = http_request(basis + b"Origin: null\r\n\r\n" + body,
                                local=cfg.CONFIG["ap_ip"])
        state.app.stats["access_state"] = old_access
        rejected = http_request(basis + b"Origin: https://evil.example\r\n\r\n" + body)
        self.assertIn(b"303 See Other", accepted)
        self.assertIn(b"403 Forbidden", rejected)

    def test_null_origin_is_rejected_outside_setup_ap(self):
        body = b"device_code=ABCDEF123456"
        raw = (b"POST /api/session HTTP/1.0\r\nHost: rtk-d4e5f6.local\r\n"
               b"Origin: null\r\n" +
               (b"Content-Length: %d\r\n\r\n" % len(body)) + body)
        self.assertIn(b"403 Forbidden", http_request(raw))

    def test_maintenance_actions_require_csrf_and_accept_it(self):
        _session, headers = self._session_headers(csrf=True)
        basis = (b"Host: rtk-d4e5f6.local\r\n"
                 b"Origin: http://rtk-d4e5f6.local\r\n")
        for path in ("/api/ble-maintenance", "/api/gnss-command"):
            ohne_csrf = http_request(
                ("POST %s HTTP/1.0\r\n" % path).encode() + basis +
                headers.split(b"X-CSRF-Token:", 1)[0] +
                b"Content-Length: 0\r\n\r\n")
            self.assertIn(b"403 Forbidden", ohne_csrf, path)

            mit_csrf = http_request(
                ("POST %s HTTP/1.0\r\n" % path).encode() + basis + headers +
                b"Content-Length: 0\r\n\r\n")
            self.assertIn(b"503 Service Unavailable", mit_csrf, path)

    def test_logout_requires_csrf_and_ends_session(self):
        session, headers = self._session_headers(csrf=True)
        basis = (b"Host: rtk-d4e5f6.local\r\n"
                 b"Origin: http://rtk-d4e5f6.local\r\n")
        ohne_csrf = http_request(
            b"DELETE /api/session HTTP/1.0\r\n" + basis +
            b"Cookie: rtk_session=" + session["token"].encode() + b"\r\n\r\n")
        self.assertIn(b"403 Forbidden", ohne_csrf)

        response = http_request(
            b"DELETE /api/session HTTP/1.0\r\n" + basis + headers + b"\r\n")
        self.assertIn(b"204 No Content", response)
        self.assertIn(b"Max-Age=0", response)
        danach = http_request(
            b"GET /api/session HTTP/1.0\r\nCookie: rtk_session=" +
            session["token"].encode() + b"\r\n\r\n")
        self.assertIn(b"401 Unauthorized", danach)

    def test_stream_token_rotation_is_admin_and_csrf_protected(self):
        _session, headers = self._session_headers(csrf=True)
        old_rotate = web.rotate_stream_token
        web.rotate_stream_token = lambda: "NEWSTREAMTOKEN"
        try:
            response = http_request(
                b"POST /api/stream-token/rotate HTTP/1.0\r\n"
                b"Host: rtk-d4e5f6.local\r\n"
                b"Origin: http://rtk-d4e5f6.local\r\n" + headers + b"\r\n")
        finally:
            web.rotate_stream_token = old_rotate
        self.assertIn(b"200 OK", response)
        self.assertIn(b"NEWSTREAMTOKEN", response)

    def _rotate_with_disconnect_spy(self, new_token):
        """Rotate via HTTP; returns the response and the token seen by each disconnect."""
        _session, headers = self._session_headers(csrf=True)
        seen = []

        async def disconnect():
            seen.append(state.app.identity.get("stream_token"))
            return 2

        with mock.patch.object(web, "rotate_stream_token", lambda: new_token), \
                mock.patch.object(web, "disconnect_all_clients", disconnect):
            response = http_request(
                b"POST /api/stream-token/rotate HTTP/1.0\r\n"
                b"Host: rtk-d4e5f6.local\r\n"
                b"Origin: http://rtk-d4e5f6.local\r\n" + headers + b"\r\n")
        return response, seen

    def test_stream_token_rotation_disconnects_tcp_clients(self):
        response, seen = self._rotate_with_disconnect_spy("NEWSTREAMTOKEN")
        self.assertIn(b"200 OK", response)
        self.assertIn(b"NEWSTREAMTOKEN", response)
        self.assertEqual(seen, ["NEWSTREAMTOKEN"])      # token set before disconnecting

    def test_failed_rotation_keeps_tcp_clients(self):
        response, seen = self._rotate_with_disconnect_spy(None)
        self.assertIn(b"500 Internal Server Error", response)
        self.assertEqual(seen, [])

    def test_reboot_is_admin_and_csrf_protected(self):
        denied = http_request(
            b"POST /api/reboot HTTP/1.0\r\nContent-Length: 0\r\n\r\n")
        self.assertIn(b"401 Unauthorized", denied)
        _session, headers = self._session_headers(csrf=True)
        response = http_request(
            b"POST /api/reboot HTTP/1.0\r\nHost: rtk-d4e5f6.local\r\n"
            b"Origin: http://rtk-d4e5f6.local\r\n" + headers + b"\r\n")
        self.assertIn(b"202 Accepted", response)
        self.assertIn(b'"restarting": true', response)

    def test_known_routes_deliver_405_if_the_method_is_wrong(self):
        for raw in (
                b"POST /api/label HTTP/1.0\r\nContent-Length: 0\r\n\r\n",
                b"POST /api/captive HTTP/1.0\r\nContent-Length: 0\r\n\r\n",
                b"GET /save HTTP/1.0\r\n\r\n"):
            self.assertIn(b"405 Method Not Allowed", http_request(raw))

    def test_main_page_is_not_cached_with_old_javascript(self):
        response = http_request(b"GET / HTTP/1.0\r\n\r\n")
        self.assertIn(b"Cache-Control: no-store", response)


if __name__ == "__main__":
    unittest.main()
