# SPDX-License-Identifier: AGPL-3.0-only
"""HTTP status matrix: every route answers a refusal with the status of its cause.

401 means "no valid session" (sign in again), 403 means "session valid, but the
CSRF token or the Origin was rejected" (the session lives on).
"""
import ast
import asyncio
import copy
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

from support import access, cfg, state, web
import tracking
import web_routing
from source_checks import source_tree
from test_access_v11 import FakeReader, http_request
from test_tracking import FIX

HOST = "rtk-d4e5f6.local"
ORIGIN = "http://" + HOST
FOREIGN = "http://evil.example"
SIGN_IN = {"error": "admin_session_required", "message": "Administrator sign-in required."}
SAVE_TEXT = b"Configuration access expired or invalid. Open /setup and sign in again."

PROBES = {"/generate_204", "/gen_204", "/hotspot-detect.html", "/library/test/success.html",
          "/ncsi.txt", "/connecttest.txt", "/redirect"}
PUBLIC = {"/ui", "/api/access", "/api/captive", "/qr.js", "/i18n.js", "/base.css",
          "/pages.css", "/pages.js", "/app.css", "/app.js"} | PROBES
LOGIN_PAGES = {"/", "/ui-v2", "/setup", "/label", "/diagnostics"}   # 401 + HTML sign-in page
TEXT_401 = {"/rtcm", "/errors"}                                    # 401 text/plain
SESSION_ROUTE, SAVE_ROUTE = "/api/session", "/save"
JSON_ROUTES = set(web_routing.ROUTES) - PUBLIC - LOGIN_PAGES - TEXT_401 - {SESSION_ROUTE, SAVE_ROUTE}
JSON_EXPECTED = {  # explicit, so a new route must be classified here (#20a, #24, WP2, #27, WP4)
    "/api/label", "/api/ui/live", "/api/events", "/api/diagnostics", "/api/field-diagnostics",
    "/api/field-diagnostics/export", "/api/wifi-scan", "/api/config-result",
    "/api/config/backup", "/status", "/health", "/api/log", "/api/parallel-benchmark",
    "/api/parallel-benchmark/export", "/api/config/restore", "/api/config/apply",
    "/api/ble-maintenance", "/api/stream-token/rotate", "/api/reboot", "/api/config",
    "/api/measurement-gate", "/api/field-setup", "/api/tcp-auth", "/gnss",
    "/api/gnss-command", "/api/tracking", "/api/tracking/import", "/api/tracking/export",
    "/api/tracking/line", "/api/tracking/map", "/api/stakeout", "/api/stakeout/import",
    "/api/stakeout/settings", "/api/stakeout/live", "/api/stakeout/progress",
    "/api/stakeout/export"}


def request(method, path, cookie=None, csrf=None, origin=ORIGIN, body=b"{}", length=None):
    """(status, head, body) of one request through the dispatcher.

    length overrides the declared Content-Length (for the body limit).
    """
    lines = ["%s %s HTTP/1.0" % (method, path), "Host: " + HOST,
             "Content-Type: application/json"]
    if origin:
        lines.append("Origin: " + origin)
    if cookie:
        lines.append("Cookie: " + cookie)
    if csrf:
        lines.append("X-CSRF-Token: " + csrf)
    if method in ("GET", "HEAD"):
        body = b""
    else:
        lines.append("Content-Length: %d" % (len(body) if length is None else length))
    return answer(("\r\n".join(lines) + "\r\n\r\n").encode() + body)


def answer(raw):
    """(status, head, body) of the dispatcher's answer to the raw request bytes."""
    head, _sep, payload = http_request(raw).partition(b"\r\n\r\n")
    return int(head.split(b" ", 2)[1]), head, payload


def header_value(head, name):
    """Value of header *name* in the answer head, or None."""
    prefix = name.lower().encode() + b":"
    for line in head.split(b"\r\n")[1:]:
        if line.lower().startswith(prefix):
            return line.split(b":", 1)[1].strip().decode()
    return None


def writes():
    """Every (path, method) pair that changes state, except the sign-in itself."""
    return sorted((path, method) for path, policy in web_routing.ROUTES.items()
                  for method in policy[0] if method in ("POST", "PUT", "DELETE")
                  and (path, method) != (SESSION_ROUTE, "POST"))


def literal_owners(tree, text):
    """Names of the innermost functions that contain the string literal *text*."""
    owners = set()

    def visit(node, owner):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            owner = node.name
        elif isinstance(node, ast.Constant) and node.value == text:
            owners.add(owner)
        for child in ast.iter_child_nodes(node):
            visit(child, owner)

    visit(tree, None)
    return owners


class _DeviceCase(unittest.TestCase):
    """A signed-out device with a known identity, reached on its STA address."""

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

    def login(self):
        session, error = access.login("ABCDEF123456", "ABCDEF123456", "203.0.113.9")
        self.assertIsNone(error)
        return "rtk_session=" + session["token"], session["csrf"], session["token"]


class TestAuthStatusMatrix(_DeviceCase):
    def side_effects_blocked(self):
        """Patches the reboot hook and config writes; a gate regression stays harmless."""
        reboot = mock.patch.object(web, "do_soft_reboot_deferred")
        dump = mock.patch.object(cfg, "_atomic_dump", return_value=False)
        self.addCleanup(reboot.stop)
        self.addCleanup(dump.stop)
        return reboot.start(), dump.start()

    def assert_no_side_effects(self, guards):
        for guard in guards:
            guard.assert_not_called()

    def assert_refusal(self, reply, status, code, path):
        self.assertEqual(reply[0], status, (path, reply[0], reply[2][:200]))
        if path == SAVE_ROUTE:
            self.assertIn(b"Content-Type: text/plain", reply[1], path)
        else:
            self.assertEqual(json.loads(reply[2])["error"], code, path)

    def test_every_route_is_classified(self):
        self.assertEqual(JSON_ROUTES, JSON_EXPECTED)

    def test_reads_without_session_are_401(self):
        for path, policy in sorted(web_routing.ROUTES.items()):
            if "GET" not in policy[0]:
                continue
            with self.subTest(path=path):
                status, head, body = request("GET", path)
                if path in PUBLIC:
                    self.assertNotIn(status, (401, 403))
                    continue
                self.assertEqual(status, 401, body[:200])
                if path in LOGIN_PAGES:
                    self.assertIn(b"Content-Type: text/html", head)
                elif path in TEXT_401:
                    self.assertIn(b"Content-Type: text/plain", head)
                elif path == SESSION_ROUTE:
                    self.assertEqual(json.loads(body), {"authenticated": False})
                else:
                    self.assertEqual(json.loads(body), SIGN_IN)

    def test_writes_without_session_are_401(self):
        guards = self.side_effects_blocked()
        for path, method in writes():
            with self.subTest(path=path, method=method):
                status, head, body = request(method, path)
                self.assertEqual(status, 401, body[:200])
                if path == SAVE_ROUTE:
                    self.assertIn(b"Content-Type: text/plain", head)
                    self.assertEqual(body, SAVE_TEXT)
                else:
                    self.assertEqual(json.loads(body), SIGN_IN)
        self.assert_no_side_effects(guards)

    def test_writes_with_a_session_but_no_or_wrong_csrf_are_403_csrf_rejected(self):
        guards = self.side_effects_blocked()
        cookie, _csrf, _token = self.login()
        for path, method in writes():
            for csrf in (None, "0" * 32):
                with self.subTest(path=path, method=method, csrf=csrf):
                    reply = request(method, path, cookie=cookie, csrf=csrf)
                    self.assert_refusal(reply, 403, "csrf_rejected", path)
        self.assert_no_side_effects(guards)

    def test_writes_from_a_foreign_origin_are_403_origin_rejected(self):
        guards = self.side_effects_blocked()
        cookie, csrf, _token = self.login()
        for path, method in writes():
            with self.subTest(path=path, method=method):
                reply = request(method, path, cookie=cookie, csrf=csrf, origin=FOREIGN)
                self.assert_refusal(reply, 403, "origin_rejected", path)
        self.assert_no_side_effects(guards)

    def test_expired_session_on_every_write_route_is_401(self):
        guards = self.side_effects_blocked()
        cookie, csrf, _token = self.login()
        access.reset()
        for path, method in writes():
            with self.subTest(path=path, method=method):
                reply = request(method, path, cookie=cookie, csrf=csrf)
                self.assert_refusal(reply, 401, "admin_session_required", path)
        self.assert_no_side_effects(guards)

    def test_replaced_session_token_is_403_csrf_rejected(self):
        _cookie, first_csrf, _token = self.login()
        second_cookie, _csrf, _token = self.login()
        with mock.patch.object(web, "do_soft_reboot_deferred") as reboot:
            reply = request("POST", "/api/reboot", cookie=second_cookie, csrf=first_csrf)
        self.assert_refusal(reply, 403, "csrf_rejected", "/api/reboot")
        reboot.assert_not_called()

    def test_refused_logout_keeps_the_session_window(self):
        with mock.patch.object(access.time, "ticks_ms", return_value=200_000):
            cookie, csrf, token = self.login()
        with mock.patch.object(access.time, "ticks_ms", return_value=300_000):
            reply = request("DELETE", SESSION_ROUTE, cookie=cookie, csrf=csrf,
                             origin=FOREIGN)
        self.assert_refusal(reply, 403, "origin_rejected", SESSION_ROUTE)
        self.assertIn(token, access._sessions)
        self.assertEqual(access._sessions[token]["last"], 200_000)

    def test_auth_refusals_have_one_source(self):
        tree = source_tree(web)
        self.assertEqual(literal_owners(tree, "admin_session_required"), {"_auth_refusal"})
        self.assertEqual(literal_owners(tree, "403 Forbidden"),
                         {"_auth_refusal", "_handle_session_request"})


class TestHeadAnswers(_DeviceCase):
    def assert_head_like_get(self, path, cookie=None):
        """HEAD answers GET's status line and Content-Length without a body."""
        _status, get_head, get_body = request("GET", path, cookie=cookie)
        status, head, body = request("HEAD", path, cookie=cookie)
        self.assertEqual(head.split(b"\r\n", 1)[0], get_head.split(b"\r\n", 1)[0], path)
        self.assertEqual(body, b"", path)
        if header_value(get_head, "Content-Length") is not None:
            self.assertEqual(header_value(head, "Content-Length"), str(len(get_body)), path)
        return status, head, get_body

    def test_head_matches_get_without_body_on_every_get_route(self):
        for path, policy in sorted(web_routing.ROUTES.items()):
            if "HEAD" in policy[0]:
                with self.subTest(path=path):
                    self.assert_head_like_get(path)

    def test_head_on_a_signed_in_status_has_no_body(self):
        cookie, _csrf, _token = self.login()
        status, head, body = request("HEAD", "/status", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertGreater(int(header_value(head, "Content-Length")), 0)
        self.assertEqual(body, b"")

    def test_head_on_unknown_and_refused_paths_has_no_body(self):
        for path, expected in (("/nope", 404), ("/api/config", 405)):
            with self.subTest(path=path):
                status, _head, body = request("HEAD", path)
                self.assertEqual(status, expected)
                self.assertEqual(body, b"")

    def test_signed_in_head_on_a_page_and_a_streaming_export(self):
        cookie, _csrf, _token = self.login()
        status, head, get_body = self.assert_head_like_get("/", cookie)
        self.assertEqual(status, 200)
        self.assertIn(b"Content-Type: text/html", head)
        self.assertGreater(len(get_body), 0)

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        tracker = tracking.Tracker(os.path.join(folder.name, "survey"))
        self.addCleanup(tracker._sync_journal, True)
        self.addCleanup(tracker.point_store.close)
        tracker.create_project("Head probe")
        export = "/api/tracking/export?format=backup"
        with mock.patch.object(web, "tracker", tracker):
            status, head, body = request("HEAD", export, cookie=cookie)
            get_status, _get_head, get_body = request("GET", export, cookie=cookie)
        self.assertEqual((status, get_status), (200, 200))
        self.assertIsNotNone(header_value(head, "Content-Disposition"))
        self.assertEqual(body, b"")
        self.assertGreater(len(get_body), 0)

    def test_early_answers_to_head_have_no_body(self):
        with mock.patch.object(web, "_http_connections", web.HTTP_MAX_CONNECTIONS):
            for method in ("HEAD", "GET"):
                with self.subTest(limit=method):
                    status, head, body = request(method, "/status")
                    self.assertEqual(status, 503)
                    self.assertEqual(header_value(head, "Retry-After"), "2")
                    self.assertEqual(header_value(head, "Content-Length"), "0")
                    self.assertEqual(body, b"")
        oversized = " /status HTTP/1.0\r\nX-Pad: " + "a" * 5000 + "\r\n\r\n"
        truncated = " /status HTTP/1.0\r\n"
        for raw, expected in ((oversized, 431), (truncated, 400)):
            for method, text in (("HEAD", b""), ("GET", b"Invalid HTTP request.")):
                with self.subTest(status=expected, method=method):
                    status, _head, body = answer((method + raw).encode())
                    self.assertEqual(status, expected)
                    self.assertEqual(body, text)

    def test_read_request_returns_the_received_bytes_on_error(self):
        self.assertEqual(asyncio.run(web._read_request(FakeReader(b"HEAD /x HTTP/1.0\r\n"))),
                         (b"HEAD /x HTTP/1.0\r\n", None, "header_incomplete"))
        self.assertEqual(asyncio.run(web._read_request(FakeReader(b""))),
                         (b"", None, "header_incomplete"))


class TestDomainStatusMatrix(_DeviceCase):
    """Survey refusals through the dispatcher with a real session and CSRF token."""

    RESERVE = "Flash reserve would fall below 20 percent."
    ARCHIVE_RESERVE = "Not enough flash to archive while preserving the reserve."
    LOW_FLASH = {"free_flash_bytes": 100, "required_flash_reserve_bytes": 100,
                 "estimated_temporary_bytes": 1, "writable": False}

    def setUp(self):
        super().setUp()
        self.cookie, self.csrf, _token = self.login()
        quiet = mock.patch.object(web, "log")
        quiet.start()
        self.addCleanup(quiet.stop)

    def call(self, method, path, payload=None):
        body = b"{}" if payload is None else json.dumps(payload).encode()
        status, _head, answer_body = request(method, path, cookie=self.cookie,
                                             csrf=self.csrf, body=body)
        return status, json.loads(answer_body)

    def test_survey_action_during_a_pending_install_is_409(self):
        with mock.patch.object(web.tracker, "_loaded", False), \
                mock.patch.object(web.tracker, "_install_pending", True):
            status, body = self.call("POST", "/api/tracking", {"action": "compact"})
            self.assertEqual((status, body["error"]), (409, "restore_install_pending"))
            status, body = self.call("GET", "/api/tracking/map")
            self.assertEqual((status, body["error"]), (503, "survey_loading"))

    def test_reserve_refusal_is_507(self):
        with mock.patch.object(web.tracker, "_loaded", True), \
                mock.patch.object(web.tracker, "create_project",
                                  side_effect=tracking.StorageReserveError(self.RESERVE)):
            status, body = self.call("POST", "/api/tracking",
                                     {"action": "create_project", "name": "X"})
        self.assertEqual((status, body), (507, {"error": "storage_reserve",
                                                "message": self.RESERVE}))

    def test_maintenance_refusals_keep_409_and_503(self):
        with mock.patch.object(web.tracker, "_loaded", True), \
                mock.patch.object(web.tracker, "maintenance_active", return_value=True):
            status, body = self.call("POST", "/api/tracking", {"action": "compact"})
            self.assertEqual((status, body["error"]), (409, "storage_busy"))
            status, body = self.call("GET", "/api/tracking/map")
            self.assertEqual((status, body["error"]), (503, "storage_busy"))

    def test_archive_reserve_refusal_is_507_and_keeps_the_project(self):
        folder = tempfile.mkdtemp(prefix="reserve-http-")
        self.addCleanup(shutil.rmtree, folder, True)
        tracker = tracking.Tracker(os.path.join(folder, "survey"))
        self.addCleanup(tracker._sync_journal, True)
        self.addCleanup(tracker.point_store.close)
        pid = tracker.create_project("Reserve")["id"]
        tracker.start_line("L", 1, fix=FIX)
        tracker.add_vertex(dict(FIX, lon_e8=800002000))
        tracker.set_line_state("finish")
        before = ([(p["id"], p["name"]) for p in tracker.projects], tracker.feature_count())
        with mock.patch.object(web, "tracker", tracker), \
                mock.patch.object(tracker, "storage_capacity", return_value=self.LOW_FLASH):
            status, body = self.call("POST", "/api/tracking",
                                     {"action": "archive_project", "project_id": pid})
        self.assertEqual((status, body), (507, {"error": "storage_reserve",
                                                "message": self.ARCHIVE_RESERVE}))
        self.assertEqual(([(p["id"], p["name"]) for p in tracker.projects],
                          tracker.feature_count()), before)
        self.assertEqual(tracker.archives, [])
        left = os.listdir(tracker.archive_dir) if os.path.isdir(tracker.archive_dir) else []
        self.assertEqual([name for name in left if name.endswith((".ndjson", ".tmp"))], [])

    def test_handler_failure_is_500_and_bad_request_line_is_400(self):
        with mock.patch.object(web, "access_info", side_effect=KeyError("hostname")), \
                mock.patch.object(web.app, "log_error"):
            status, head, body = request("GET", "/api/access")
        self.assertEqual((status, json.loads(body)["error"]), (500, "internal_error"))
        self.assertIn(b"Content-Type: application/json", head)
        status, head, body = answer(b"GET status HTTP/1.1\r\n\r\n")
        self.assertEqual((status, body), (400, b"Invalid HTTP request."))


class TestMeasurementDefaultsStatusMatrix(_DeviceCase):
    """Measurement method answers of /api/measurement-gate through the dispatcher (#24b)."""

    def setUp(self):
        super().setUp()
        self.cookie, self.csrf, _token = self.login()
        quiet = mock.patch.object(web, "log")
        quiet.start()
        self.addCleanup(quiet.stop)

        async def no_reboot():
            pass
        reboot = mock.patch.object(web, "do_soft_reboot_deferred", no_reboot)
        reboot.start()
        self.addCleanup(reboot.stop)
        self.cwd = os.getcwd()
        self.tmp = tempfile.mkdtemp(prefix="esp-rtk-24b-matrix-")
        os.chdir(self.tmp)
        self.saved = dict(cfg.CONFIG)
        self.saved_access_state = state.app.stats.get("access_state")
        state.app.stats["access_state"] = "ONLINE"
        cfg._ROLLBACK_OPEN, cfg._ROLLBACK_CODE, cfg._STAGING_UNRECOVERED = False, None, False
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
        shutil.rmtree(self.tmp, ignore_errors=True)
        super().tearDown()

    def call(self, method, path, payload=None):
        body = b"{}" if payload is None else json.dumps(payload).encode()
        return self.call_body(method, path, body)

    def call_raw(self, method, path, body=b"{}", length=None):
        """(status, head, body bytes) of one signed-in request with raw body bytes."""
        return request(method, path, cookie=self.cookie, csrf=self.csrf, body=body,
                       length=length)

    def call_body(self, method, path, body):
        """(status, parsed JSON answer) of one signed-in request with raw body bytes."""
        status, _head, answer_body = self.call_raw(method, path, body)
        return status, json.loads(answer_body)

    def test_measurement_gate_method_statuses(self):
        status, body = self.call("PUT", "/api/measurement-gate", {"samples": "quality"})
        self.assertEqual((status, body["samples"]), (200, "quality"))
        for payload in ({"samples": 2}, {"min_satellites": 6, "samples": 5}):
            status, body = self.call("PUT", "/api/measurement-gate", payload)
            self.assertEqual((status, body["error"]), (400, "invalid_measurement_gate"))
        for method in ("GET", "DELETE"):
            status, body = self.call(method, "/api/measurement-gate")
            self.assertEqual((status, body["samples"]), (200, "quality"), method)

    def test_own_large_backup_round_trips_through_the_dispatcher(self):
        cfg.CONFIG.update(wifi_networks=[['"' * 31 + str(i), '"' * 63] for i in range(8)],
                          ntrip_host='"' * 64, ntrip_mount='"' * 64, ntrip_user='"' * 64,
                          ntrip_pass='"' * 1024, ntrip_fallback_pass='"' * 1024)
        cfg.save_config()
        status, _head, raw = self.call_raw("GET", "/api/config/backup")
        self.assertEqual(status, 200)
        self.assertGreater(len(raw), 4096)                       # over the old limit
        status, body = self.call_body("POST", "/api/config/restore", raw)
        self.assertEqual((status, body["schema_version"]), (202, 3), body)
        self.assertEqual(cfg.CONFIG["ntrip_pass"], '"' * 1024)

    def test_restore_body_limit_is_declared_and_enforced(self):
        status, _head, payload = self.call_raw("POST", "/api/config/restore", b"",
                                               length=web_routing.CONFIG_RESTORE_BODY_LIMIT + 1)
        self.assertEqual((status, payload), (413, b"Request body is too large."))
        self.assertEqual(cfg.CONFIG["wifi_ssid"], "Old")

    def test_config_restore_statuses_by_schema(self):
        backup = json.loads(json.dumps(web.config_backup()))
        bad = copy.deepcopy(backup)
        bad["measurement"]["samples"] = 2
        status, body = self.call("POST", "/api/config/restore", bad)
        self.assertEqual((status, body["error"]), (400, "invalid_backup"))
        broken = copy.deepcopy(backup)
        broken["schema_version"] = []
        status, body = self.call("POST", "/api/config/restore", broken)
        self.assertEqual((status, body["error"]), (400, "invalid_backup"))     # never 500
        state.app.stats["access_state"] = "CONNECTING"
        status, body = self.call("POST", "/api/config/restore", backup)
        self.assertEqual((status, body["error"]), (409, "config_verification_running"))
        state.app.stats["access_state"] = "ONLINE"
        status, body = self.call("POST", "/api/config/restore", backup)
        self.assertEqual((status, body["sections"]),
                         (202, ["connections", "measurement", "field_setup", "stakeout"]))
        old = {"schema_version": 1, "kind": "esp-rtk-config-backup", "config": {"wifi_ssid": "Old"}}
        status, body = self.call("POST", "/api/config/restore", old)
        self.assertEqual((status, body["error"]), (409, "open_change_pending"))


if __name__ == "__main__":
    unittest.main()
