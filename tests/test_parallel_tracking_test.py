# SPDX-License-Identifier: AGPL-3.0-only
import json
import os
import tempfile
import unittest
import io
import urllib.error
import asyncio
import time
from types import SimpleNamespace
from unittest import mock

import parallel_tracking_test as tool


class TestParallelTrackingTool(unittest.TestCase):
    def test_local_load_report_does_not_require_tcp_but_parallel_still_does(self):
        shared = {'qualified': True, 'http_errors': 0, 'tcp_bytes': 0,
                  'ble_bytes': 0, 'ble_error': None, 'profile': 'local'}
        args = SimpleNamespace(report=None)
        self.assertTrue(tool._save_report(args, shared)['load_qualified'])
        shared['profile'] = 'parallel'
        self.assertFalse(tool._save_report(args, shared)['load_qualified'])
        shared['tcp_bytes'] = 42
        self.assertTrue(tool._save_report(args, shared)['load_qualified'])

    def test_local_monitor_never_connects_external_transports(self):
        args = SimpleNamespace(profile='local', duration=1, cookie_env=None,
            stream_token_env=None, ble_address_env=None, report=None)

        async def http_load(_args, shared):
            shared['complete'] = True
            shared['qualified'] = True

        with mock.patch.object(tool, '_http_load', side_effect=http_load), \
                mock.patch.object(tool, '_tcp_load', new_callable=mock.AsyncMock) as tcp, \
                mock.patch.object(tool, '_ble_load', new_callable=mock.AsyncMock) as ble:
            report = asyncio.run(tool.monitor(args))
        tcp.assert_not_called()
        ble.assert_not_called()
        self.assertTrue(report['load_qualified'])
        self.assertEqual(report['profile'], 'local')
        self.assertEqual(report['http_interval_sec'], 5)

    def test_export_uses_separate_timeout_and_still_validates_all_points(self):
        client = tool.WifiControl('device.invalid', code='synthetic-code')
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(client, 'request', return_value=(200, self.export_fixture())) as request:
            verified = client.export(os.path.join(tmp, 'points.ndjson'), 1)
        self.assertEqual(verified['points'], 1)
        request.assert_called_once_with('/api/parallel-benchmark/export', timeout=120)

    def test_http_failure_diagnostics_are_bounded_and_exclude_exception_secrets(self):
        shared = {"http_errors": 0, "confirmed_points": 256}
        error = urllib.error.HTTPError("secret-url", 400, "secret-body", {}, None)
        for _ in range(25):
            tool._record_http_failure(shared, "/status", error=error)
        self.assertEqual(shared["http_errors"], 25)
        self.assertEqual(len(shared["http_failures"]), 20)
        self.assertEqual(shared["http_failures"][-1]["status"], 400)
        self.assertEqual(shared["http_failures"][-1]["points"], 256)
        self.assertNotIn("secret", json.dumps(shared))

    def export_fixture(self):
        rows = [{"record": "header", "kind": "survey_backup", "schema_version": 4},
                {"record": "project", "project": {"id": "project-1", "name": "Test"}},
                {"record": "line", "project_id": "project-1",
                 "line": {"id": "line-1", "state": "recording", "name": "Test line"}},
                {"record": "vertex", "project_id": "project-1", "line_id": "line-1",
                 "coordinate": [10.0, 40.0, 100.0], "measurement": {"recorded_at": 1,
                    "fix_status": "RTK_FIXED", "satellites": 24, "hdop": 0.6,
                    "quality": {"accepted": True}}},
                {"record": "footer", "active_project_id": "project-1",
                 "active_line_id": "line-1", "auto_distance_m": 1.0}]
        raw = b"".join((json.dumps(row) + "\n").encode() for row in rows)
        return raw + (json.dumps({"record": "integrity", "records": len(rows),
            "algorithm": "sha256", "content_sha256": tool.hashlib.sha256(raw).hexdigest()}) + "\n").encode()

    def test_export_requires_matching_integrity_and_point_count(self):
        raw = self.export_fixture()
        self.assertEqual(tool.verify_export(raw, 1)["points"], 1)
        for broken, target in [(raw, 2), (raw.replace(b"10.0", b"11.0"), 1),
                               (raw.splitlines(keepends=True)[0], 1),
                               (raw + b'{}\n', 1)]:
            with self.assertRaises(ValueError):
                tool.verify_export(broken, target)

    def test_wifi_controls_use_json_csrf_and_no_usb(self):
        client = tool.WifiControl("device.invalid", code="synthetic-code")
        login_reply = mock.MagicMock()
        login_reply.__enter__.return_value = io.BytesIO(b'{"csrf_token":"synthetic-csrf"}')
        action_reply = mock.MagicMock()
        action_reply.__enter__.return_value.status = 200
        action_reply.__enter__.return_value.read.return_value = b'{"state":"armed"}'
        client.opener = mock.Mock()
        client.opener.open.side_effect = [login_reply, action_reply]
        with mock.patch.object(tool, "_usb", side_effect=AssertionError("USB called")):
            self.assertEqual(client.action("start", 10000)["state"], "armed")
        request = client.opener.open.call_args.args[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.get_header("X-csrf-token"), "synthetic-csrf")
        self.assertEqual(json.loads(request.data)["target_points"], 10000)

    def test_missing_wifi_api_is_reported_without_fallback_mutation(self):
        client = tool.WifiControl("device.invalid", code="synthetic-code")
        client.csrf = "synthetic-csrf"
        client.opener = mock.Mock()
        client.opener.open.side_effect = urllib.error.HTTPError(
            client.base, 404, "Not Found", {}, None)
        with self.assertRaisesRegex(RuntimeError, "no WLAN benchmark API"):
            client.json("/api/parallel-benchmark")
        self.assertEqual(client.opener.open.call_count, 1)

    def test_wifi_session_is_renewed_after_reboot(self):
        client = tool.WifiControl("device.invalid", code="synthetic-code")
        client.csrf = "old-csrf"
        denied = urllib.error.HTTPError(client.base, 403, "Forbidden", {}, None)
        reply = mock.MagicMock()
        reply.__enter__.return_value.status = 200
        reply.__enter__.return_value.read.return_value = b'{"state":"running"}'
        client.opener = mock.Mock()
        client.opener.open.side_effect = [denied, reply]
        with mock.patch.object(client, "login") as login:
            self.assertEqual(client.json("/api/parallel-benchmark")["state"], "running")
        login.assert_called_once()

    def test_arm_writes_only_validated_synthetic_configuration(self):
        with mock.patch.object(tool, "_usb", return_value="armed") as usb:
            tool.arm("synthetic-port", 20000)
        code = usb.call_args.args[1]
        self.assertIn("parallel_tracking_benchmark", code)
        self.assertIn("20000", code)
        self.assertNotIn("password", code.lower())

    def test_cleanup_requires_exact_confirmation_and_prefix(self):
        with self.assertRaises(ValueError):
            tool.cleanup(None, "wrong")
        with mock.patch.object(tool, "_usb", return_value="clean") as usb:
            tool.cleanup(None, tool.PREFIX)
        code = usb.call_args.args[1]
        self.assertIn(tool.PREFIX, code)
        self.assertIn("startswith(p)", code)

    def test_monitor_report_excludes_all_connection_secrets(self):
        async def fake_http(_args, shared):
            shared["complete"] = True
        async def fake_transport(_args, _shared): pass
        async def fake_ble(_shared): pass
        with tempfile.TemporaryDirectory(prefix="esp-rtk-host-report-") as tmp:
            report = os.path.join(tmp, "report.json")
            args = type("Args", (), {"duration": 1, "cookie_env": "COOKIE",
                "stream_token_env": "TOKEN", "ble_address_env": "BLE",
                "report": report})()
            with mock.patch.dict(os.environ, {"COOKIE": "secret-cookie",
                    "TOKEN": "secret-token", "BLE": "secret-address"}), \
                    mock.patch.object(tool, "_http_load", fake_http), \
                    mock.patch.object(tool, "_tcp_load", fake_transport), \
                    mock.patch.object(tool, "_ble_load", fake_ble):
                tool.asyncio.run(tool.monitor(args))
            with open(report, encoding="utf-8") as source:
                encoded = source.read()
            self.assertNotIn("secret", encoded)
            self.assertTrue(json.loads(encoded)["complete"])


class TestHttpFailFast(unittest.TestCase):
    class Client:
        def __init__(self, failure_path='/', failure=None):
            self.failure_path = failure_path
            self.failure = failure or urllib.error.URLError('secret URL and credentials')
            self.calls = []

        def request(self, path):
            self.calls.append(path)
            if path == self.failure_path:
                if isinstance(self.failure, BaseException):
                    raise self.failure
                return self.failure, b'failure'
            if path == '/status':
                return 200, json.dumps({'stats': {'parallel_benchmark_state': 'running',
                    'parallel_benchmark_points': 125}}).encode()
            return 200, b'healthy'

        def json(self, path):
            if path == self.failure_path:
                self.calls.append(path)
                raise self.failure
            self.calls.append(path)
            return {'state': 'running', 'live': {}, 'result': None}

    def args(self, path):
        return SimpleNamespace(profile='local', duration=22000, cookie_env=None,
            stream_token_env=None, ble_address_env=None, report=path,
            fail_fast_http=True)

    def test_first_failed_request_stops_further_load_and_persists_exact_error_once(self):
        scenarios = (
            ('/', urllib.error.URLError('secret URL and credentials'), 125),
            ('/status', 503, 0),
            ('/api/parallel-benchmark', ValueError('secret invalid response'), 125),
            ('/api/tracking/map?limit=500', 400, 125),
        )
        for path, failure, points in scenarios:
            with self.subTest(path=path), tempfile.TemporaryDirectory() as directory:
                report_path = os.path.join(directory, 'report.json')
                client = self.Client(path, failure)
                with mock.patch.object(tool, '_tcp_load', new_callable=mock.AsyncMock) as tcp, \
                        mock.patch.object(tool, '_ble_load', new_callable=mock.AsyncMock) as ble:
                    with self.assertRaises(tool.HttpLoadFailure):
                        asyncio.run(tool.monitor(self.args(report_path), client, 'run-id'))
                with open(report_path) as source:
                    report = json.load(source)
                self.assertEqual(report['http_errors'], 1)
                self.assertEqual(len(report['http_failures']), 1)
                self.assertEqual(report['http_failures'][0]['path'], path)
                self.assertEqual(report['http_failures'][0]['points'], points)
                self.assertEqual(client.calls[-1], path)
                self.assertFalse(report['qualified'])
                self.assertFalse(report['load_qualified'])
                self.assertNotIn('secret', json.dumps(report))
                tcp.assert_not_called()
                ble.assert_not_called()

    def test_planned_reboot_connection_error_before_running_stays_ignored(self):
        class BootClient(self.Client):
            def request(self, path):
                if not self.calls:
                    self.calls.append(path)
                    raise urllib.error.URLError('expected boot restart')
                return super().request(path)

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'report.json')
            client = BootClient()
            with mock.patch.object(tool.asyncio, 'sleep', new_callable=mock.AsyncMock):
                with self.assertRaises(tool.HttpLoadFailure):
                    asyncio.run(tool.monitor(self.args(path), client, 'fresh-run'))
            with open(path) as source:
                report = json.load(source)
        self.assertEqual(report['http_errors'], 1)
        self.assertEqual(report['http_failures'][0]['path'], '/')
        self.assertEqual(client.calls[:2], ['/status', '/status'])

    def test_opt_in_preserves_legacy_error_collection(self):
        shared = {'http_errors': 0, 'confirmed_points': 7}
        tool._record_http_failure(shared, '/', status=503)
        self.assertEqual(shared['http_errors'], 1)
        shared['fail_fast_http'] = True
        with self.assertRaises(tool.HttpLoadFailure):
            tool._record_http_failure(shared, '/', status=503)
        self.assertEqual(shared['http_errors'], 2)

    def test_urlerror_reason_records_only_exception_class_and_numeric_errno(self):
        shared = {'http_errors': 0, 'confirmed_points': 125}
        tool._record_http_failure(shared, '/',
            error=urllib.error.URLError(OSError(110, 'secret URL and credentials')))
        failure = shared['http_failures'][0]
        self.assertEqual(failure['error_type'], 'URLError')
        self.assertEqual(failure['reason_type'], 'TimeoutError')
        self.assertEqual(failure['errno'], 110)
        self.assertNotIn('secret', json.dumps(shared))

    def test_failed_round_duration_is_persisted_before_immediate_abort(self):
        class SlowFailure(self.Client):
            def request(self, path):
                if path == '/':
                    time.sleep(0.02)
                return super().request(path)
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, 'report.json')
            with self.assertRaises(tool.HttpLoadFailure):
                asyncio.run(tool.monitor(self.args(path), SlowFailure(), 'run-id'))
            with open(path) as source:
                report = json.load(source)
        self.assertGreaterEqual(report['http_max_ms'], 20)


if __name__ == "__main__":
    unittest.main()
