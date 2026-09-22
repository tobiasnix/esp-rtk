# SPDX-License-Identifier: AGPL-3.0-only
"""HTTP layer: URL decoding, POST parsing, headers, and setup page."""
import asyncio
import inspect
import json
import os
import tempfile
import unittest
from unittest import mock

from support import cfg, state, web
import assets
import web_routing
import tracking


class FakeWriter:
    """Collects data written by _http_send."""

    def __init__(self):
        self.buf = bytearray()

    def write(self, data):
        self.buf.extend(data)

    def get_extra_info(self, key):
        return ("192.168.4.2", 12345)

    async def drain(self):
        pass


class TestStaticAssets(unittest.TestCase):
    def test_streams_get_with_mime_cache_and_csp(self):
        with tempfile.NamedTemporaryFile(delete=False) as asset:
            asset.write(b"a" * 2500)
            filename = asset.name
        try:
            writer = FakeWriter()
            asyncio.run(web._http_send_file(writer, filename,
                                             "text/css; charset=utf-8", "GET"))
            header, body = bytes(writer.buf).split(b"\r\n\r\n", 1)
            self.assertIn(b"Content-Type: text/css; charset=utf-8", header)
            self.assertIn(b"Content-Length: 2500", header)
            self.assertIn(b"max-age=31536000, immutable", header)
            self.assertIn(b"Content-Security-Policy:", header)
            self.assertNotIn(b"unsafe-inline", header)
            self.assertNotIn(b"unsafe-eval", header)
            self.assertEqual(body, b"a" * 2500)
        finally:
            os.unlink(filename)

    def test_head_has_get_length_but_no_body(self):
        with tempfile.NamedTemporaryFile(delete=False) as asset:
            asset.write(b"dashboard")
            filename = asset.name
        try:
            writer = FakeWriter()
            asyncio.run(web._http_send_file(writer, filename,
                                             "application/javascript", "HEAD"))
            header, body = bytes(writer.buf).split(b"\r\n\r\n", 1)
            self.assertIn(b"Content-Length: 9", header)
            self.assertEqual(body, b"")
        finally:
            os.unlink(filename)

    def test_missing_asset_is_no_store_404(self):
        writer = FakeWriter()
        asyncio.run(web._http_send_file(writer, "/missing/asset.js",
                                         "application/javascript"))
        response = bytes(writer.buf)
        self.assertIn(b"404 Not Found", response)
        self.assertIn(b"Cache-Control: no-store", response)


class TestWifiScanContract(unittest.TestCase):
    def _request(self, active_line=None, measurement=False):
        manager = mock.Mock()
        manager.request_scan.return_value = ("ready", [{"ssid": "Field", "rssi": -50}], None)
        writer = FakeWriter()
        progress = dict(web.MEASUREMENT_PROGRESS)
        web.MEASUREMENT_PROGRESS["active"] = measurement
        try:
            with mock.patch.object(web, "_admin_allowed", return_value=True), \
                    mock.patch.object(web.tracker, "status", return_value={"active_line": active_line}), \
                    mock.patch.dict(web._instances, {"network": manager}, clear=False):
                asyncio.run(web._handle_diagnostics_request(
                    writer, "/api/wifi-scan", "", False, {}))
        finally:
            web.MEASUREMENT_PROGRESS.clear()
            web.MEASUREMENT_PROGRESS.update(progress)
        return bytes(writer.buf), manager

    def test_scan_is_available_while_corrections_are_streaming(self):
        old = state.app.stats.get("ntrip_state")
        state.app.stats["ntrip_state"] = "streaming"
        try:
            response, manager = self._request()
        finally:
            state.app.stats["ntrip_state"] = old
        self.assertIn(b"200 OK", response)
        self.assertIn(b'"ssid": "Field"', response)
        manager.request_scan.assert_called_once_with()

    def test_scan_is_blocked_only_during_an_active_measurement(self):
        for active_line, measurement in (({"state": "active"}, False),
                                         ({"state": "paused"}, True)):
            response, manager = self._request(active_line, measurement)
            self.assertIn(b"409 Conflict", response)
            self.assertIn(b"wifi_scan_busy", response)
            manager.request_scan.assert_not_called()

    def test_manifest_has_all_offline_assets(self):
        self.assertEqual(set(web.STATIC_ASSETS), {
            "/base.css", "/pages.css",
            "/pages.js", "/i18n.js", "/qr.js", "/app.css", "/app.js"})

    def test_shared_page_behaviour_uses_no_native_blocking_dialogs(self):
        with open("pages.js", encoding="utf-8") as source:
            javascript = source.read()
        for obsolete in ("prompt(", "confirm(", "alert("):
            self.assertNotIn(obsolete, javascript)


class TestMeasurementProgress(unittest.TestCase):
    def test_progress_contract_exposes_measurement_mode(self):
        self.assertIn(web.MEASUREMENT_PROGRESS["mode"], ("fixed", "quality"))
        self.assertIn("phase", web.MEASUREMENT_PROGRESS)
        self.assertIn("elapsed_sec", web.MEASUREMENT_PROGRESS)

    def test_parallel_measurement_is_rejected_explicitly(self):
        previous_fix = state.app._last_fix
        async def scenario():
            state.app._last_fix = {"utc": "1", "lat": 50.0, "lon": 8.0}
            first = asyncio.create_task(web.averaged_current_fix(10))
            await asyncio.sleep(.05)
            with self.assertRaisesRegex(ValueError, "already running"):
                await web.averaged_current_fix(5)
            web.cancel_measurement()
            with self.assertRaisesRegex(ValueError, "cancelled"):
                await first
        try:
            asyncio.run(scenario())
        finally:
            state.app._last_fix = previous_fix

    def test_running_measurement_can_be_cancelled(self):
        previous_fix = state.app._last_fix
        fix = {"utc": "1", "lat": 50.0, "lon": 8.0, "alt": 100.0}

        async def scenario():
            state.app._last_fix = fix
            task = asyncio.create_task(web.averaged_current_fix(10))
            await asyncio.sleep(.05)
            self.assertTrue(web.MEASUREMENT_PROGRESS["active"])
            self.assertEqual(web.cancel_measurement(), {"cancelled": True})
            with self.assertRaisesRegex(ValueError, "cancelled"):
                await task

        try:
            asyncio.run(scenario())
        finally:
            state.app._last_fix = previous_fix
        self.assertFalse(web.MEASUREMENT_PROGRESS["active"])
        self.assertTrue(web.MEASUREMENT_PROGRESS["cancelled"])

    def test_counts_distinct_gnss_samples(self):
        previous_fix = state.app._last_fix
        fix = {"utc": "120000.00", "lat": 50.0, "lon": 8.0, "alt": 100.0,
               "qual": 4, "sats": 20, "hdop": 0.6, "correction_age_sec": 1.0,
               "fix_status_text": "RTK_FIXED",
               "receiver_accuracy": {"horizontal_sigma_m": .01,
                                       "altitude_sigma_m": .02}}

        async def scenario():
            state.app._last_fix = dict(fix)
            task = asyncio.create_task(web.averaged_current_fix(3))
            await asyncio.sleep(0.05)
            self.assertTrue(web.MEASUREMENT_PROGRESS["active"])
            self.assertEqual(web.MEASUREMENT_PROGRESS["collected"], 1)
            for index in (1, 2):
                state.app._last_fix = dict(fix, utc="12000%d.00" % index,
                                           lat=50.0 + index * 0.000001)
                await asyncio.sleep(0.3)
            result = await task
            self.assertEqual(result["accuracy_observation"]["samples"], 3)

        try:
            asyncio.run(scenario())
        finally:
            state.app._last_fix = previous_fix
        self.assertFalse(web.MEASUREMENT_PROGRESS["active"])
        self.assertEqual(web.MEASUREMENT_PROGRESS["collected"], 3)
        self.assertEqual(web.MEASUREMENT_PROGRESS["total"], 3)

    def test_waits_for_same_epoch_gst_before_counting_sample(self):
        previous_fix = state.app._last_fix
        base = {"utc": "1", "lat": 50.0, "lon": 8.0, "alt": 100.0,
                "qual": 4, "sats": 20, "hdop": .6,
                "correction_age_sec": 1.0, "fix_status_text": "RTK_FIXED"}
        accuracy = {"horizontal_sigma_m": .01, "altitude_sigma_m": .02}

        async def scenario():
            state.app._last_fix = dict(base)
            task = asyncio.create_task(web.averaged_current_fix(2))
            await asyncio.sleep(.05)
            self.assertEqual(web.MEASUREMENT_PROGRESS["collected"], 0)
            self.assertIn("gst_required", web.MEASUREMENT_PROGRESS["waiting_reasons"])
            state.app._last_fix = dict(base, receiver_accuracy=accuracy)
            await asyncio.sleep(.3)
            self.assertEqual(web.MEASUREMENT_PROGRESS["collected"], 1)
            state.app._last_fix = dict(base, utc="2", lat=50.0000001,
                                       receiver_accuracy=accuracy)
            await asyncio.sleep(.3)
            result = await task
            self.assertEqual(result["receiver_accuracy"]["samples"], 2)

        try:
            asyncio.run(scenario())
        finally:
            state.app._last_fix = previous_fix

    def test_reports_running_sigma_and_altitude_means(self):
        previous_fix = state.app._last_fix
        base = {"utc": "1", "lat": 50.0, "lon": 8.0, "alt": 100.0,
                "qual": 4, "sats": 20, "hdop": .6,
                "receiver_accuracy": {"horizontal_sigma_m": .01,
                                        "altitude_sigma_m": .02}}
        async def scenario():
            state.app._last_fix = dict(base)
            task = asyncio.create_task(web.averaged_current_fix(2))
            await asyncio.sleep(.05)
            state.app._last_fix = dict(base, utc="2", alt=102.0,
                receiver_accuracy={"horizontal_sigma_m": .03,
                                   "altitude_sigma_m": .04})
            await asyncio.sleep(.3)
            await task
        try:
            asyncio.run(scenario())
        finally:
            state.app._last_fix = previous_fix
        self.assertEqual(web.MEASUREMENT_PROGRESS["mean_h_sigma_m"], .02)
        self.assertEqual(web.MEASUREMENT_PROGRESS["mean_v_sigma_m"], .03)
        self.assertEqual(web.MEASUREMENT_PROGRESS["mean_alt_m"], 101.0)

    def test_quality_mode_uses_a_rolling_window_until_spread_is_accepted(self):
        previous_fix = state.app._last_fix
        base = {"utc": "0", "lat": 50.001, "lon": 8.001, "alt": 100.0,
                "qual": 4, "sats": 20, "hdop": .6,
                "correction_age_sec": 1.0, "fix_status_text": "RTK_FIXED",
                "receiver_accuracy": {"horizontal_sigma_m": .01,
                                        "altitude_sigma_m": .02}}

        async def scenario():
            state.app._last_fix = dict(base)
            task = asyncio.create_task(web.averaged_current_fix("quality"))
            await asyncio.sleep(.05)
            for index in range(1, 6):
                state.app._last_fix = dict(base, utc=str(index),
                    lat=50.0 + index * .00000001,
                    lon=8.0 + index * .00000001)
                await asyncio.sleep(.3)
            result = await task
            self.assertEqual(result["accuracy_observation"]["samples"], 5)
            self.assertLessEqual(
                result["accuracy_observation"]["horizontal_max_deviation_m"], .03)

        try:
            asyncio.run(scenario())
        finally:
            state.app._last_fix = previous_fix
        self.assertFalse(web.MEASUREMENT_PROGRESS["active"])
        self.assertEqual(web.MEASUREMENT_PROGRESS["mode"], "quality")
        self.assertIsNone(web.MEASUREMENT_PROGRESS["total"])
        self.assertEqual(web.MEASUREMENT_PROGRESS["collected"], 6)

    def test_quality_mode_uses_configured_window(self):
        previous_fix = state.app._last_fix
        previous_window = cfg.CONFIG.get("measurement_quality_window_samples")
        base = {"utc": "0", "lat": 50.001, "lon": 8.001, "alt": 100.0,
                "qual": 4, "sats": 20, "hdop": .6,
                "correction_age_sec": 1.0, "fix_status_text": "RTK_FIXED",
                "receiver_accuracy": {"horizontal_sigma_m": .01,
                                        "altitude_sigma_m": .02}}
        async def scenario():
            cfg.CONFIG["measurement_quality_window_samples"] = 3
            state.app._last_fix = dict(base)
            task = asyncio.create_task(web.averaged_current_fix("quality"))
            await asyncio.sleep(.05)
            for index in range(1, 3):
                state.app._last_fix = dict(base, utc=str(index))
                await asyncio.sleep(.3)
            return await task
        try:
            result = asyncio.run(scenario())
            self.assertEqual(result["accuracy_observation"]["samples"], 3)
        finally:
            state.app._last_fix = previous_fix
            cfg.CONFIG["measurement_quality_window_samples"] = previous_window

    def test_single_sample_resets_live_means(self):
        previous_fix = state.app._last_fix
        state.app._last_fix = {"lat": 1, "lon": 2, "alt": 3}
        web.MEASUREMENT_PROGRESS["mean_alt_m"] = 999
        try:
            asyncio.run(web.averaged_current_fix(1))
        finally:
            state.app._last_fix = previous_fix
        self.assertFalse(web.MEASUREMENT_PROGRESS["active"])
        self.assertEqual(web.MEASUREMENT_PROGRESS["total"], 1)
        self.assertIsNone(web.MEASUREMENT_PROGRESS["mean_alt_m"])


class TestTrackingViews(unittest.TestCase):
    def test_tcp_auth_endpoint_is_boolean_persistent_and_defaults_on(self):
        async def call(method, payload=b""):
            writer = FakeWriter()
            with mock.patch.object(web, "_admin_allowed", return_value=True), \
                    mock.patch.object(web, "save_config", side_effect=lambda clean: (cfg.CONFIG.update(clean) or True)):
                await web._handle_tcp_auth_request(writer, method, payload, False, {})
            return bytes(writer.buf).split(b"\r\n\r\n", 1)[1]
        saved = dict(cfg.CONFIG)
        try:
            self.assertIn(b'"required": true', asyncio.run(call("GET")))
            self.assertIn(b'"required": false', asyncio.run(call(
                "PUT", b'{"required":false}')))
            self.assertFalse(cfg.CONFIG["nmea_tcp_auth_required"])
            invalid = asyncio.run(call("PUT", b'{"required":"no"}'))
            self.assertIn(b'invalid_tcp_auth_setting', invalid)
        finally:
            cfg.CONFIG.clear()
            cfg.CONFIG.update(saved)

    def test_measurement_gate_endpoint_reads_updates_and_resets(self):
        values = tracking.measurement_gate()
        values["max_hdop"] = 2.0
        async def call(method, payload=b""):
            writer = FakeWriter()
            with mock.patch.object(web, "_admin_allowed", return_value=True), \
                    mock.patch.object(web, "save_config", side_effect=lambda clean: (cfg.CONFIG.update(clean) or True)):
                await web._handle_measurement_gate_request(
                    writer, method, payload, False, {})
            return bytes(writer.buf).split(b"\r\n\r\n", 1)[1]
        saved = dict(cfg.CONFIG)
        try:
            body = asyncio.run(call("GET"))
            self.assertIn(b'"defaults"', body)
            body = asyncio.run(call("PUT", json.dumps(values).encode()))
            self.assertEqual(cfg.CONFIG["measurement_max_hdop"], 2.0)
            self.assertIn(b'"values"', body)
            asyncio.run(call("DELETE"))
            self.assertEqual(cfg.CONFIG["measurement_max_hdop"], 1.5)
        finally:
            cfg.CONFIG.clear()
            cfg.CONFIG.update(saved)

    def test_ui_live_view_combines_only_field_loop_state(self):
        writer = FakeWriter()
        compact = {"revision": 7, "device_time": 100,
                   "active_project": None, "active_line": None}
        with mock.patch.object(web, "_read_allowed", return_value=True), \
                mock.patch.object(web.tracker, "live_status", return_value=compact), \
                mock.patch.object(web, "board_metrics", return_value={"ram_free_bytes": 1}):
            asyncio.run(web._handle_ui_live_request(writer, "", False, {}))
        _header, body = bytes(writer.buf).split(b"\r\n\r\n", 1)
        self.assertIn(b'"schema_version": 1', body)
        self.assertIn(b'"revision": 7', body)
        self.assertIn(b'"measurement_progress"', body)
        self.assertNotIn(b'"projects"', body)

    def test_incremental_log_view_is_bounded_and_authenticated(self):
        writer = FakeWriter()
        payload = {"generation": 2, "cursor": "2:9", "entries": [],
                   "has_more": False}
        with mock.patch.object(web, "_admin_allowed", return_value=True), \
                mock.patch.object(web, "incremental_error_log",
                                  return_value=payload) as read:
            asyncio.run(web._handle_status_request(
                writer, "/api/log", "cursor=2%3A4&limit=999&level=WARN",
                False, {}))
        read.assert_called_once_with("2:4", 200, "WARN")
        self.assertIn(b'"cursor": "2:9"', bytes(writer.buf))

    def test_name_check_view_returns_compact_contract(self):
        writer = FakeWriter()
        with mock.patch.object(web, "_admin_allowed", return_value=True), \
                mock.patch.object(web.tracker, "_loaded", True), \
                mock.patch.object(web.tracker, "name_available",
                                  return_value={"available": False,
                                                "existing": "V-01"}) as check:
            asyncio.run(web._handle_tracking_request(
                writer, "/api/tracking", "GET",
                "view=name_check&kind=point&name=V-01", b"", False, {}))
        check.assert_called_once_with("V-01", "point")
        _header, body = bytes(writer.buf).split(b"\r\n\r\n", 1)
        self.assertIn(b'"available": false', body)
        self.assertIn(b'"existing": "V-01"', body)


class TestUrlDecode(unittest.TestCase):
    def test_plus_becomes_space(self):
        self.assertEqual(web.url_decode("a+b"), "a b")

    def test_prozent_hex(self):
        self.assertEqual(web.url_decode("a%40b"), "a@b")

    def test_multiple_sequences(self):
        self.assertEqual(web.url_decode("%2Fmnt%2Fpnt"), "/mnt/pnt")

    def test_utf8_umlaut(self):
        # 'U' = C3 BC
        self.assertEqual(web.url_decode("Caf%C3%A9"), "Café")

    def test_cut_off_sequence_remains_literal(self):
        # No crash if % is not followed by enough signs.
        self.assertEqual(web.url_decode("abc%4"), "abc%4")

    def test_invalid_hex_remains_literal(self):
        self.assertEqual(web.url_decode("a%ZZb"), "a%ZZb")

    def test_empty_string(self):
        self.assertEqual(web.url_decode(""), "")


class TestParsePostData(unittest.TestCase):
    def test_einfaches_formular(self):
        got = web.parse_post_data(b"wifi_ssid=Home&ntrip_port=2101")
        self.assertEqual(got, {"wifi_ssid": "Home", "ntrip_port": "2101"})

    def test_field_without_equality_sign_is_skipped(self):
        # V9.0 crashed here.
        got = web.parse_post_data(b"a=1&kaputt&b=2")
        self.assertEqual(got, {"a": "1", "b": "2"})

    def test_empty_value(self):
        # An empty password field means leave it unchanged.
        self.assertEqual(web.parse_post_data(b"wifi_pass="), {"wifi_pass": ""})

    def test_the_value_contains_the_same_sign(self):
        self.assertEqual(web.parse_post_data(b"p=a=b"), {"p": "a=b"})

    def test_urlkodierte_werte(self):
        got = web.parse_post_data(b"ntrip_user=a%40b.de&wifi_ssid=my+network")
        self.assertEqual(got["ntrip_user"], "a@b.de")
        self.assertEqual(got["wifi_ssid"], "my network")

    def test_empty_body(self):
        self.assertEqual(web.parse_post_data(b""), {})
        self.assertEqual(web.parse_post_data(None), {})


class TestContentLength(unittest.TestCase):
    def parse(self, *lines):
        return web._parse_content_length([b"POST /save HTTP/1.1"] + list(lines))

    def test_is_found(self):
        self.assertEqual(self.parse(b"Content-Length: 42"), 42)

    def test_gross_klein_egal(self):
        self.assertEqual(self.parse(b"content-length: 7"), 7)

    def test_fehlt(self):
        self.assertEqual(self.parse(b"Host: 192.168.4.1"), 0)

    def test_no_numerical_value(self):
        self.assertEqual(self.parse(b"Content-Length: abc"), 0)

    def test_recall_line_is_not_read(self):
        # The slice [1:] should skip the request line.
        self.assertEqual(web._parse_content_length([b"Content-Length: 99"]), 0)


class TestHttpSend(unittest.TestCase):
    def test_header_and_body(self):
        w = FakeWriter()
        web._http_send(w, "200 OK", "text/plain", "hallo")
        out = bytes(w.buf)
        self.assertTrue(out.startswith(b"HTTP/1.0 200 OK\r\n"))
        self.assertIn(b"Content-Length: 5\r\n", out)
        self.assertTrue(out.endswith(b"\r\n\r\nhallo"))

    def test_content_length_does_not_count_bytes_of_characters(self):
        # The bug from V9.0: len(str) instead of len(bytes) for umlauten.
        w = FakeWriter()
        web._http_send(w, "200 OK", "text/html; charset=utf-8", "café")
        out = bytes(w.buf)
        self.assertIn(b"Content-Length: 5\r\n", out)   # c a f é(2)
        header, body = out.split(b"\r\n\r\n", 1)
        self.assertEqual(len(body), 5)

    def test_bytes_payload(self):
        w = FakeWriter()
        web._http_send(w, "404 Not Found", "text/plain", b"weg")
        self.assertIn(b"Content-Length: 3\r\n", bytes(w.buf))

    def test_json_helpers_enforce_no_store_and_stable_error_envelope(self):
        writer = FakeWriter()
        web._send_api_error(writer, "400 Bad Request", "invalid", "Invalid input.")
        header, body = bytes(writer.buf).split(b"\r\n\r\n", 1)
        self.assertIn(b"Content-Type: application/json", header)
        self.assertIn(b"Cache-Control: no-store", header)
        self.assertEqual(body, b'{"error": "invalid", "message": "Invalid input."}')


class TestRouteMethodPolicy(unittest.TestCase):
    def test_complete_route_method_matrix(self):
        usual = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE")
        for path, policy in web_routing.ROUTES.items():
            allowed, expected_error, _body_limit = policy
            for method in usual:
                error = web._route_method_error(path, method)
                if method in allowed:
                    self.assertIsNone(error, (path, method))
                else:
                    self.assertEqual(error, expected_error, (path, method))

    def test_read_only_routes_accept_get_and_head(self):
        for path in web.READ_ONLY_ROUTES:
            self.assertIsNone(web._route_method_error(path, "GET"), path)
            self.assertIsNone(web._route_method_error(path, "HEAD"), path)

    def test_read_only_routes_reject_mutating_methods(self):
        for path in web.READ_ONLY_ROUTES:
            self.assertEqual(web._route_method_error(path, "POST"),
                             "GET required", path)

    def test_declarative_action_route_methods(self):
        self.assertIsNone(web._route_method_error("/api/session", "POST"))
        self.assertIsNone(web._route_method_error("/api/config", "PUT"))
        self.assertIsNone(web._route_method_error("/save", "POST"))


class TestHttpDomainBoundaries(unittest.TestCase):
    def test_all_mutating_domains_have_dedicated_async_handlers(self):
        for name in ("_handle_session_request", "_handle_config_request",
                     "_handle_diagnostics_request", "_handle_status_request",
                     "_handle_ui_live_request",
                     "_handle_measurement_gate_request",
                     "_handle_tcp_auth_request",
                     "_handle_maintenance_request", "_handle_gnss_request",
                     "_handle_tracking_request"):
            self.assertTrue(inspect.iscoroutinefunction(getattr(web, name)), name)

    def test_transport_dispatch_no_longer_contains_domain_implementations(self):
        source = inspect.getsource(web.handle_http_client)
        self.assertLess(len(source.splitlines()), 300)
        self.assertNotIn("validate_settings(", source)
        self.assertNotIn("send_command(", source)
        self.assertNotIn("read_flight_recorder(", source)
        self.assertNotIn("rotate_stream_token(", source)
        self.assertEqual(web._route_method_error("/api/config", "POST"),
                         "GET or PUT required")

    def test_unknown_routes_are_left_to_the_404_handler(self):
        self.assertIsNone(web._route_method_error("/missing", "POST"))

    def test_tracking_route_has_larger_declared_body_limit(self):
        self.assertEqual(web._route_body_limit("/api/tracking"),
                         web.MAX_TRACKING_BODY_BYTES)
        self.assertEqual(web._route_body_limit("/api/config"),
                         web.MAX_BODY_BYTES)


class TestRenderSetup(unittest.TestCase):
    def setUp(self):
        self.saved = dict(cfg.CONFIG)
        self.saved_identity = web.app.identity

    def tearDown(self):
        cfg.CONFIG.clear()
        cfg.CONFIG.update(self.saved)
        web.app.identity = self.saved_identity

    def test_no_placeholders_remain_standing(self):
        # V9.0 used str.format() and failed on the CSS staples.
        html = web.render_setup()
        for key in ("version", "wifi_ssid", "ntrip_host", "ntrip_port",
                    "ntrip_mount", "ntrip_user", "ap_ssid", "ap_ip"):
            self.assertNotIn("{%s}" % key, html)

    def test_external_stylesheet_is_linked(self):
        html = web.render_setup()
        self.assertIn('href="/pages.css?v=%s"' % assets.ASSET_VERSION, html)
        self.assertNotIn("<style", html)

    def test_values_are_used(self):
        cfg.CONFIG["wifi_ssid"] = "MeinNetz"
        cfg.CONFIG["ntrip_mount"] = "SYNTH"
        html = web.render_setup()
        self.assertIn("MeinNetz", html)
        self.assertIn("SYNTH", html)

    def test_passwords_do_not_appear_in_the_html(self):
        cfg.CONFIG["wifi_pass"] = "streng-secret-wlan"
        cfg.CONFIG["ntrip_pass"] = "streng-secret-ntrip"
        cfg.CONFIG["ap_pass"] = "streng-secret-ap"
        html = web.render_setup()
        self.assertNotIn("streng-secret-wlan", html)
        self.assertNotIn("streng-secret-ntrip", html)
        self.assertNotIn("streng-secret-ap", html)

    def test_device_code_is_masked_by_default_and_switchable(self):
        web.app.identity = dict(web.app.identity or {}, device_code="CODE-1234")
        html = web.render_setup()
        self.assertIn('id="device-code" type="password"', html)
        self.assertIn('value="CODE-1234" readonly', html)
        self.assertIn("data-secret-toggle", html)
        with open("pages.js", encoding="utf-8") as source:
            self.assertIn('input.type = show ? "text" : "password"', source.read())
        self.assertIn('aria-pressed="false"', html)
        self.assertIn(">Show</button>", html)

    def test_form_fields_cover_the_configuration(self):
        html = web.render_setup()
        for field in ("wifi_ssid", "wifi_pass", "ntrip_host", "ntrip_port",
                     "ntrip_mount", "ntrip_user", "ntrip_pass"):
            self.assertIn('name="%s"' % field, html)


if __name__ == "__main__":
    unittest.main()


class TestNoFalseErrors(unittest.TestCase):
    """Browsers open connections as a precautionary measure over which a request never comes. When ERROR was reported, it landed in the flash and clogged /errors."""

    def test_typical_client_abortions_are_not_server_errors(self):
        for errno in (32, 54, 103, 104, 107, 128):
            self.assertTrue(web._client_disconnected(OSError(errno, "weg")))
        self.assertFalse(web._client_disconnected(OSError(12, "Memory")))

    def test_idle_connection_is_not_an_error(self):
        with open("web.py") as fh:
            source = fh.read()
        # The external handler of handle_http_client - not the inner timeouts when
        # reading the body, they only break off a loop.
        rest = source[source.rfind("except asyncio.TimeoutError:"):]
        # Only until the next except - otherwise you read the trader who reports real
        # errors.
        naechstes_except = rest.find("except OSError")
        location = rest[:naechstes_except]
        self.assertIn('log("DEBUG"', location)
        self.assertNotIn('log("ERROR"', location)
        self.assertNotIn("app.log_error", location)


class TestParallelBenchmarkApi(unittest.TestCase):
    def test_authentication_is_required_for_read_and_mutation(self):
        import parallel_benchmark
        for method in ("GET", "POST", "DELETE"):
            writer = FakeWriter()
            with mock.patch.object(web, "_admin_allowed", return_value=False), \
                    mock.patch.object(parallel_benchmark, "control") as control:
                asyncio.run(web._handle_parallel_benchmark_request(
                    writer, method, b'{}', False, {}))
            self.assertIn(b"403 Forbidden", writer.buf)
            control.assert_not_called()

    def test_start_checks_csrf_and_schedules_restart_after_success(self):
        import parallel_benchmark
        writer = FakeWriter()
        headers = {"cookie": "synthetic", "x-csrf-token": "synthetic"}
        body = b'{"action":"start","target_points":10000}'
        def discard(coro): coro.close()
        with mock.patch.object(web, "_admin_allowed", return_value=True) as allowed, \
                mock.patch.object(web.tracker, "_loaded", True), \
                mock.patch.object(parallel_benchmark, "control", return_value={
                    "state": "armed", "reboot_required": True}) as control, \
                mock.patch.object(web.asyncio, "create_task", side_effect=discard) as task:
            asyncio.run(web._handle_parallel_benchmark_request(writer, "POST", body, False, headers))
        allowed.assert_called_once_with(False, headers, "POST", "", body)
        control.assert_called_once_with("start", web.tracker, 10000, None, profile="parallel")
        task.assert_called_once()
        self.assertIn(b"200 OK", writer.buf)

    def test_refusal_does_not_restart(self):
        import parallel_benchmark
        writer = FakeWriter()
        with mock.patch.object(web, "_admin_allowed", return_value=True), \
                mock.patch.object(web.tracker, "_loaded", True), \
                mock.patch.object(parallel_benchmark, "control", side_effect=ValueError("Active line")), \
                mock.patch.object(web.asyncio, "create_task") as task:
            asyncio.run(web._handle_parallel_benchmark_request(
                writer, "POST", b'{"action":"start","target_points":10000}', False, {}))
        self.assertIn(b"409 Conflict", writer.buf)
        task.assert_not_called()

    def test_production_writes_are_blocked_during_benchmark(self):
        writer = FakeWriter()
        with mock.patch.object(web, "_admin_allowed", return_value=True), \
                mock.patch.dict(web._instances, {"parallel_tracker": object()}):
            asyncio.run(web._handle_tracking_request(
                writer, "/api/tracking", "POST", "", b'{"action":"delete_project"}', False, {}))
        self.assertIn(b"409 Conflict", writer.buf)
        self.assertIn(b"benchmark_active", writer.buf)
