# SPDX-License-Identifier: AGPL-3.0-only
"""V11 access: identity, sessions, discovery, and transport contracts."""
import asyncio
import json
import os
import tempfile
import unittest

from support import (access, ble, cfg, discovery, fanout, identity, state,
                     web, FakeNVS)
import assets


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
            "http://rtk-d4e5f6.local/setup#code=gnss-a%2Bb%20c")
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
        self.assertIn("setup#code=CODE123", html)
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


class TestTransactionalConfiguration(unittest.TestCase):
    def setUp(self):
        self.cwd = os.getcwd()
        self.tmp = tempfile.mkdtemp(prefix="esp-rtk-pending-")
        os.chdir(self.tmp)
        self.saved = dict(cfg.CONFIG)
        cfg.CONFIG["wifi_ssid"] = "Old"
        cfg.CONFIG["wifi_pass"] = "legacy-password"
        cfg.save_config()

    def tearDown(self):
        cfg.CONFIG.clear()
        cfg.CONFIG.update(self.saved)
        os.chdir(self.cwd)

    def test_stage_commit(self):
        self.assertTrue(cfg.stage_config({"wifi_ssid": "New"}))
        self.assertTrue(cfg.pending_config_active())
        cfg.commit_pending_config("FieldNetwork")
        self.assertFalse(cfg.pending_config_active())
        self.assertEqual(cfg.config_result_read()["details"]["ssid"], "FieldNetwork")
        with open("config.json") as fh:
            self.assertEqual(json.load(fh)["wifi_ssid"], "New")

    def test_stage_rollback(self):
        self.assertTrue(cfg.stage_config({"wifi_ssid": "Invalid"}))
        self.assertTrue(cfg.rollback_pending_config())
        self.assertEqual(cfg.CONFIG["wifi_ssid"], "Old")
        with open("config.json") as fh:
            self.assertEqual(json.load(fh)["wifi_ssid"], "Old")

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
        original_stage = web.stage_config
        web.stage_config = lambda _values: False  # do not schedule a test reboot
        try:
            response = http_request(raw)
        finally:
            web.stage_config = original_stage
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

    def test_admin_session_is_allowed_to_read_protected_status(self):
        old = cfg.CONFIG.get("status_token")
        cfg.CONFIG["status_token"] = "legacy-read-token"
        session, error = access.login("ABCDEF123456", "ABCDEF123456",
                                      "203.0.113.9")
        self.assertIsNone(error)
        try:
            response = http_request(
                b"GET /status HTTP/1.0\r\nCookie: rtk_session=" +
                session["token"].encode() + b"\r\n\r\n")
        finally:
            cfg.CONFIG["status_token"] = old
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
        old = cfg.CONFIG.get("status_token")
        cfg.CONFIG["status_token"] = "protected"
        _session, headers = self._session_headers()
        try:
            for path in ("/label", "/api/label", "/api/config", "/status",
                         "/health", "/rtcm", "/errors", "/gnss"):
                response = http_request(
                    ("GET %s HTTP/1.0\r\n" % path).encode() + headers + b"\r\n")
                self.assertIn(b"200 OK", response, path)
        finally:
            cfg.CONFIG["status_token"] = old

    def test_protected_reading_routes_remain_closed_without_access(self):
        old = cfg.CONFIG.get("status_token")
        cfg.CONFIG["status_token"] = "protected"
        try:
            for path in ("/label", "/api/label", "/api/config", "/status",
                         "/health", "/rtcm", "/errors", "/gnss"):
                response = http_request(("GET %s HTTP/1.0\r\n\r\n" % path).encode())
                self.assertNotIn(b"200 OK", response, path)
        finally:
            cfg.CONFIG["status_token"] = old

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
        old_stage = web.stage_config
        old_access = state.app.stats.get("access_state")
        web.stage_config = lambda values: True
        state.app.stats["access_state"] = "SETUP"
        try:
            response = http_request(
                b"POST /save HTTP/1.0\r\nHost: rtk-d4e5f6.local\r\n"
                b"Origin: null\r\n" + headers +
                (b"Content-Length: %d\r\n\r\n" % len(body)) + body,
                local=cfg.CONFIG["ap_ip"])
        finally:
            web.stage_config = old_stage
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

    def test_reboot_is_admin_and_csrf_protected(self):
        denied = http_request(
            b"POST /api/reboot HTTP/1.0\r\nContent-Length: 0\r\n\r\n")
        self.assertIn(b"403 Forbidden", denied)
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
