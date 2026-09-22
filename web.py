# SPDX-License-Identifier: AGPL-3.0-only
"""Local HTTP server for setup, status, diagnostics, and administration."""
import gc
import machine
import network
import os
import time
import uasyncio as asyncio
import ubinascii
import ujson

from access import authenticate, login, logout
from assets import version_html
from cfg import (CONFIG, SETTABLE_KEYS, _same_subnet, _subnet_prefix, read_error_log,
                 error_log_entries,
                 incremental_error_log,
                 config_result_read,
                 read_flight_recorder, previous_flight_recorder, html_escape, log,
                 mark_planned_reset, parse_configured_networks, configured_networks_text,
                 save_config, stage_config, validate_settings, validate_wifi_networks)
from field_diagnostics import recorder as field_recorder
from fanout import clients as fanout_clients, nmea_tcp_ports
from identity import rotate_stream_token
from ui import APP_V2, APP_V2_LOGIN
from gnss import replies as gnss_replies, send_command
from state import _instances, app, shutdown_event
from tracking import (average_fixes, measurement_gate, MEASUREMENT_GATE_KEYS,
                      quality_check, tracker, validate_measurement_gate)
from web_routing import (ROUTES as ROUTE_POLICIES,
                         body_limit as _route_body_limit,
                         method_error as _policy_method_error)


MEASUREMENT_PROGRESS = {"active": False, "collected": 0, "total": 0,
                        "mean_h_sigma_m": None, "mean_v_sigma_m": None,
                        "mean_alt_m": None, "cancelled": False,
                        "waiting_reasons": [], "rejected_samples": 0,
                        "mode": "fixed", "phase": "idle", "elapsed_sec": 0}
_measurement_generation = 0
HTTP_MAX_CONNECTIONS = 6
# Flash commits can delay a ready request beyond five seconds. Keep one
# bounded header deadline within the same ten-second service budget as bodies.
HTTP_HEADER_DEADLINE_SEC = 10
HTTP_BODY_DEADLINE_SEC = 10
_http_connections = 0


def cancel_measurement():
    """Invalidate the running averaging loop without cancelling its HTTP task."""
    global _measurement_generation
    _measurement_generation += 1
    MEASUREMENT_PROGRESS["active"] = False
    MEASUREMENT_PROGRESS["cancelled"] = True
    MEASUREMENT_PROGRESS["phase"] = "cancelled"
    generation = _measurement_generation
    async def clear_cancelled():
        await asyncio.sleep(5)
        if generation == _measurement_generation and not MEASUREMENT_PROGRESS["active"]:
            MEASUREMENT_PROGRESS["cancelled"] = False
            MEASUREMENT_PROGRESS["phase"] = "idle"
    try:
        asyncio.create_task(clear_cancelled())
    except Exception:
        pass
    return {"cancelled": True}

CONTENT_SECURITY_POLICY = ("default-src 'none'; script-src 'self'; style-src 'self'; "
                           "img-src 'self' data:; connect-src 'self'; "
                           "form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
STATIC_ASSETS = {
    "/base.css": ("base.css", "text/css; charset=utf-8"),
    "/pages.css": ("pages.css", "text/css; charset=utf-8"),
    "/pages.js": ("pages.js", "application/javascript; charset=utf-8"),
    "/i18n.js": ("i18n.js", "application/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/qr.js": ("qr.js", "application/javascript; charset=utf-8"),
}


DIAGNOSTICS_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>RTK diagnostics</title>
<link rel="stylesheet" href="/base.css?v=ASSET_VERSION">
<link rel="stylesheet" href="/pages.css?v=ASSET_VERSION"><script defer src="/i18n.js?v=ASSET_VERSION"></script>
<script defer src="/pages.js?v=ASSET_VERSION"></script></head><body class="page-diagnostics" data-page="diagnostics">
<header class="product-header"><div class="product-identity"><b class="product-name">ESP-RTK</b><span data-i18n="ui.diagnostics_title">Device diagnostics</span></div><div class="product-actions"><button class="primary page-logout" type="button" data-i18n="ui.logout">Sign out</button></div></header>
<nav class="product-nav app-navigation"><a data-i18n="app.status" href="/#status">Status</a><a data-i18n="app.projects" href="/#projects">Projects</a><a class="portal-parent" data-i18n="app.device" href="/#device">Device</a><a data-i18n="ui.configuration" href="/setup">Configuration</a><a data-i18n="nav.diagnostics" aria-current="page" href="/diagnostics">Diagnostics</a><a data-i18n="nav.label" href="/label">Label</a></nav>
<div class="device-status-bar" role="status" aria-live="polite" data-i18n="ui.loading_device_status">Loading device status …</div>
<main class="page-content panel"><h1 data-i18n="ui.diagnostics_title">Device diagnostics</h1>
<p data-i18n="ui.diagnostics_warning">Contains no Wi-Fi or NTRIP passwords. Position data and technical identifiers are sensitive; share them only when necessary.</p>
<div class="diagnostics-actions"><button id="refresh" data-i18n="action.refresh">Refresh</button>
<a href="/api/diagnostics?download=1" data-i18n="ui.download_diagnostics">Download diagnostics package</a>
<span class="muted" id="state" role="status" aria-live="polite"></span></div>
<p><a href="/#device/diagnostics" data-i18n="diag.field_link">Open field diagnostics</a></p><pre id="data">loading ...</pre></main></body></html>"""


async def averaged_current_fix(sample_count):
    """Collect distinct current fixes without blocking other device tasks."""
    global _measurement_generation
    if MEASUREMENT_PROGRESS.get("active"):
        raise ValueError("A measurement is already running.")
    quality_mode = sample_count == "quality"
    sample_count = (int(CONFIG.get("measurement_quality_window_samples", 5))
                    if quality_mode else max(1, min(int(sample_count or 1), 30)))
    generation = _measurement_generation
    MEASUREMENT_PROGRESS.update(active=False, collected=0,
                                total=None if quality_mode else sample_count,
                                mean_h_sigma_m=None, mean_v_sigma_m=None,
                                mean_alt_m=None, cancelled=False,
                                waiting_reasons=[], rejected_samples=0,
                                mode="quality" if quality_mode else "fixed",
                                phase="waiting_fix", elapsed_sec=0)
    if sample_count == 1 and not quality_mode:
        MEASUREMENT_PROGRESS["phase"] = "complete"
        return app.last_fix
    MEASUREMENT_PROGRESS["active"] = True
    samples, previous, rejected_marker, accepted_total = [], None, None, 0
    started = time.ticks_ms()
    if not quality_mode:
        try:
            deadline = time.ticks_add(time.ticks_ms(), max(15000, sample_count * 4000))
        except AttributeError:
            deadline = time.ticks_ms() + max(15000, sample_count * 4000)
    try:
        while (quality_mode or len(samples) < sample_count) and (
                quality_mode or time.ticks_diff(deadline, time.ticks_ms()) > 0):
            MEASUREMENT_PROGRESS["elapsed_sec"] = max(0, round(
                time.ticks_diff(time.ticks_ms(), started) / 1000, 1))
            if generation != _measurement_generation:
                MEASUREMENT_PROGRESS["cancelled"] = True
                raise ValueError("Measurement cancelled.")
            fix = app.last_fix
            marker = ((fix or {}).get("utc"), (fix or {}).get("lat"),
                      (fix or {}).get("lon"))
            sample_quality = quality_check(fix)
            if fix and marker != previous and sample_quality["accepted"]:
                MEASUREMENT_PROGRESS["phase"] = "collecting"
                samples.append(dict(fix))
                accepted_total += 1
                if quality_mode and len(samples) > sample_count:
                    samples.pop(0)
                previous = marker
                rejected_marker = None
                MEASUREMENT_PROGRESS["collected"] = accepted_total
                MEASUREMENT_PROGRESS["waiting_reasons"] = []
                altitudes = [float(item["alt"]) for item in samples
                             if item.get("alt") is not None]
                estimates = [(item.get("receiver_accuracy") or {})
                             for item in samples]
                horizontal = [value for value in
                    (item.get("horizontal_sigma_m", item.get("semi_major_sigma_m"))
                     for item in estimates) if value not in (None, 0)]
                vertical = [item.get("altitude_sigma_m") for item in estimates
                            if item.get("altitude_sigma_m") not in (None, 0)]
                MEASUREMENT_PROGRESS["mean_h_sigma_m"] = (
                    round(sum(horizontal) / len(horizontal), 4) if horizontal else None)
                MEASUREMENT_PROGRESS["mean_v_sigma_m"] = (
                    round(sum(vertical) / len(vertical), 4) if vertical else None)
                MEASUREMENT_PROGRESS["mean_alt_m"] = (
                    round(sum(altitudes) / len(altitudes), 3) if altitudes else None)
                if quality_mode and len(samples) == sample_count:
                    MEASUREMENT_PROGRESS["phase"] = "checking_spread"
                    averaged = average_fixes(samples)
                    averaged_quality = quality_check(averaged)
                    if averaged_quality["accepted"]:
                        MEASUREMENT_PROGRESS["phase"] = "complete"
                        averaged["accuracy_observation"]["duration_sec"] = round(
                            time.ticks_diff(time.ticks_ms(), started) / 1000, 2)
                        return averaged
                    MEASUREMENT_PROGRESS["waiting_reasons"] = list(
                        averaged_quality["reasons"])
            elif fix and marker != previous:
                MEASUREMENT_PROGRESS["phase"] = "waiting_fix"
                MEASUREMENT_PROGRESS["waiting_reasons"] = list(sample_quality["reasons"])
                if marker != rejected_marker:
                    MEASUREMENT_PROGRESS["rejected_samples"] += 1
                    rejected_marker = marker
            if quality_mode or len(samples) < sample_count:
                await asyncio.sleep_ms(250)
        if not quality_mode and len(samples) < sample_count:
            reasons = MEASUREMENT_PROGRESS.get("waiting_reasons") or []
            detail = (": " + ", ".join(reasons)) if reasons else ""
            raise ValueError("Not enough valid GNSS samples were received%s." % detail)
        averaged = average_fixes(samples)
        averaged["accuracy_observation"]["duration_sec"] = round(
            time.ticks_diff(time.ticks_ms(), started) / 1000, 2)
        return averaged
    finally:
        MEASUREMENT_PROGRESS["active"] = False
        if MEASUREMENT_PROGRESS.get("phase") != "complete":
            MEASUREMENT_PROGRESS["phase"] = ("cancelled" if
                MEASUREMENT_PROGRESS.get("cancelled") else "failed")


# Setup HTML uses explicit placeholder replacement to keep escaping explicit.
SETUP_HTML = """<!DOCTYPE html>
<html lang="en">
<head><meta name="viewport" content="width=device-width, initial-scale=1">
<meta charset="utf-8">
<title>ESP-RTK Config</title>
<link rel="stylesheet" href="/base.css?v=ASSET_VERSION">
<link rel="stylesheet" href="/pages.css?v=ASSET_VERSION">
<script defer src="/i18n.js?v=ASSET_VERSION"></script><script defer src="/pages.js?v=ASSET_VERSION"></script></head>
<body class="page-setup" data-page="setup" data-hostname="{hostname}">
<header class="product-header"><div class="product-identity"><b class="product-name">{display_name}</b><span>{version}</span></div><div class="product-actions"><button class="primary page-logout" type="button" data-i18n="ui.logout">Sign out</button></div></header>
<nav class="product-nav app-navigation"><a data-i18n="app.status" href="/#status">Status</a><a data-i18n="app.projects" href="/#projects">Projects</a><a class="portal-parent" data-i18n="app.device" href="/#device">Device</a><a data-i18n="ui.configuration" aria-current="page" href="/setup">Configuration</a><a data-i18n="nav.diagnostics" href="/diagnostics">Diagnostics</a><a data-i18n="nav.label" href="/label{token_query}">Label</a></nav>
<div class="device-status-bar" role="status" aria-live="polite" data-i18n="ui.loading_device_status">Loading device status …</div>
<main class="page-content"><h1 data-i18n="ui.configuration">Configuration</h1>
<form id="setup-form" class="setup-form panel" action="/save{token_query}" method="post">
    {config_token_field}
    {csrf_field}

    <div class="group">
        <h3 data-i18n="ui.device_access">Device access</h3>
        <p><b>{display_name}</b> &middot; <code>{hostname}.local</code></p>
        <label for="device-code" data-i18n="ui.device_code">Device code:</label>
        <div class="secret-row"><input id="device-code" type="password"
        value="{device_code}" readonly autocomplete="off">
        <button type="button" data-secret-toggle aria-controls="device-code" aria-pressed="false" data-i18n="ui.show">Show</button></div>
        <p data-i18n="ui.device_code_retained">This code belongs on the physical label and is retained after a factory reset.</p>
        <p><a href="/label{token_query}" data-i18n="ui.label_codes">Show label and direct-access QR codes</a></p>
    </div>

    <div class="group">
        <h3 data-i18n="ui.wifi_connection">Wi-Fi connection (STA)</h3>
        <p data-i18n="ui.wifi_ntrip_description">Wi-Fi network used to obtain NTRIP corrections (must have Internet access).</p>
        <button type="button" id="wifi-scan" data-i18n="ui.scan_wifi">Scan visible Wi-Fi networks</button>
        <select id="wifi-results" hidden><option value="" data-i18n="ui.choose_wifi_dots">Choose Wi-Fi network ...</option></select>
        <span class="muted" id="wifi-scan-state" role="status"></span>
        <label for="ssid">SSID:</label><input type="text" id="ssid" name="wifi_ssid" value="{wifi_ssid}" required>
        <label for="pass" data-i18n="ui.password">Password:</label><input type="password" id="pass" name="wifi_pass" placeholder="empty = unchanged" data-i18n-placeholder="ui.empty_password">
        <label for="wifi-networks" data-i18n="ui.fallbacks">Fallback networks (one network per line):</label>
        <textarea id="wifi-networks" name="wifi_networks" placeholder="Auto-Hotspot:Password&#10;Field-Wi-Fi">{wifi_networks}</textarea>
        <p class="network-help" data-i18n="ui.network_format_help">Format: Name:Password. If only the name is provided, the saved password is retained. When connecting, the device scans and tries the strongest reachable network first. Enter the Wi-Fi network normally used above as SSID and password; this field is only for additional alternatives.</p>
    </div>

    <div class="group">
        <h3 data-i18n="ui.backup_restore">Backup and restore</h3>
        <p data-i18n="ui.backup_secret_warning">The backup contains Wi-Fi and NTRIP passwords in clear text. Store the file securely.</p>
        <p><a href="/api/config/backup" download data-i18n="ui.download_config">Download configuration</a> &middot;
        <a href="/diagnostics" data-i18n="ui.open_diagnostics">Open device diagnostics</a></p>
        <input id="restore-file" type="file" accept="application/json,.json">
        <button type="button" id="restore-submit" data-i18n="ui.restore">Restore backup</button>
        <span class="muted" id="restore-state" role="status"></span>
    </div>

    <div class="group">
        <h3 data-i18n="ui.ntrip_caster">NTRIP Caster</h3>
        <label for="ntrip-enabled">NTRIP:</label><select id="ntrip-enabled" name="ntrip_enabled"><option value="1"{ntrip_enabled_on}>Enabled</option><option value="0"{ntrip_enabled_off}>Disabled</option></select>
        <label for="host">Host:</label><input type="text" id="host" name="ntrip_host" value="{ntrip_host}" required>
        <label for="port">Port:</label><input type="number" id="port" name="ntrip_port" value="{ntrip_port}" required>
        <label for="mount">Mountpoint:</label><input type="text" id="mount" name="ntrip_mount" value="{ntrip_mount}" required>
        <label for="user" data-i18n="ui.username">Username:</label><input type="text" id="user" name="ntrip_user" value="{ntrip_user}" required>
        <label for="ntrip_pass" data-i18n="ui.password">Password:</label><input type="password" id="ntrip_pass" name="ntrip_pass" placeholder="empty = unchanged" data-i18n-placeholder="ui.empty_password">
        <h4>Fallback caster (optional)</h4>
        <label for="fallback-host">Host:</label><input type="text" id="fallback-host" name="ntrip_fallback_host" value="{ntrip_fallback_host}">
        <label for="fallback-port">Port:</label><input type="number" id="fallback-port" name="ntrip_fallback_port" value="{ntrip_fallback_port}">
        <label for="fallback-mount">Mountpoint:</label><input type="text" id="fallback-mount" name="ntrip_fallback_mount" value="{ntrip_fallback_mount}">
        <label for="fallback-user" data-i18n="ui.username">Username:</label><input type="text" id="fallback-user" name="ntrip_fallback_user" value="{ntrip_fallback_user}">
        <label for="fallback-pass" data-i18n="ui.password">Password:</label><input type="password" id="fallback-pass" name="ntrip_fallback_pass" placeholder="empty = unchanged" data-i18n-placeholder="ui.empty_password">
    </div>

    <input id="save-submit" class="primary" type="submit" value="Save and check connection" data-i18n-value="action.save">
    <p class="muted" id="save-status" role="status" aria-live="polite"></p>
</form>
<p class="setup-hotspot"><span data-i18n="ui.current_hotspot">Current hotspot:</span> <b>{ap_ssid}</b> ({ap_ip})</p></main>
</body></html>
"""

def render_setup(token=None, csrf=None):
    """Render setup without disclosing stored passwords."""
    html = SETUP_HTML
    html = html.replace("{config_token_field}", "")
    csrf_field = ('<input type="hidden" name="csrf_token" value="%s">'
                  % html_escape(csrf)) if csrf else ""
    html = html.replace("{csrf_field}", csrf_field)
    html = html.replace("{token_query}", "")
    enabled = bool(CONFIG.get("ntrip_enabled", True))
    html = html.replace("{ntrip_enabled_on}", " selected" if enabled else "")
    html = html.replace("{ntrip_enabled_off}", "" if enabled else " selected")
    # Render fallback networks separately and never return stored passwords.
    html = html.replace("{wifi_networks}",
                        html_escape(configured_networks_text(CONFIG.get("wifi_networks"))))

    ident = app.identity or {}
    for key in ("display_name", "hostname", "device_code"):
        html = html.replace("{%s}" % key, html_escape(ident.get(key, "-")))

    for key in ("version", "wifi_ssid", "ntrip_host", "ntrip_port",
                "ntrip_mount", "ntrip_user", "ntrip_fallback_host",
                "ntrip_fallback_port", "ntrip_fallback_mount",
                "ntrip_fallback_user", "ap_ssid", "ap_ip"):
        # SSIDs and mountpoints enter value attributes and must be escaped.
        html = html.replace("{%s}" % key, html_escape(CONFIG.get(key, "")))
    return version_html(html)

def url_decode(s):
    """Decode application/x-www-form-urlencoded data."""
    s = s.replace("+", " ")
    b = s.encode()
    res = bytearray()
    i = 0
    n = len(b)
    while i < n:
        c = b[i]
        if c == 0x25 and i + 2 < n:  # '%'
            try:
                res.append(int(b[i + 1:i + 3], 16))
                i += 3
                continue
            except ValueError:
                pass
        res.append(c)
        i += 1
    return res.decode("utf-8", "ignore")

# RFC 3986 unreserved characters; unlike form data, queries do not use '+'.
_URL_SICHER = ("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
               "0123456789-_.~")


def url_encode(s):
    """Percent-encode a query value according to RFC 3986."""
    out = []
    for b in s.encode("utf-8"):
        c = chr(b)
        out.append(c if c in _URL_SICHER else "%%%02X" % b)
    return "".join(out)


def parse_post_data(data):
    """Parse URL-encoded form data, including incomplete fields."""
    params = {}
    if not data:
        return params
    try:
        text = data.decode("utf-8", "ignore")
    except Exception:
        return params
    for pair in text.split("&"):
        if "=" not in pair:
            continue
        key, value = pair.split("=", 1)
        params[url_decode(key)] = url_decode(value)
    return params

def parse_query(query):
    """Parse a query string with the same encoding rules as form data."""
    return parse_post_data(query.encode()) if query else {}

def _config_allowed(is_ap_request, query, body):
    """Return whether a request may view or change configuration."""
    if is_ap_request:
        return True
    expected = CONFIG.get("config_token", "")
    if not expected:
        return False
    if parse_query(query).get("token") == expected:
        return True
    return parse_post_data(body).get("config_token") == expected


def _token_ok(query):
    """Validate legacy read-only access from the STA network."""
    expected = CONFIG.get("status_token", "")
    if not expected:
        return True
    provided = parse_query(query).get("token")
    if provided == expected:
        return True
    expected = CONFIG.get("config_token", "")
    return bool(expected) and provided == expected


def _read_allowed(is_ap_request, headers, query=""):
    """Require a device-code session on both AP and STA interfaces."""
    return _admin_session(headers) is not None

def _parse_content_length(header_lines):
    for hl in header_lines[1:]:
        if hl.lower().startswith(b"content-length:"):
            try:
                return int(hl.split(b":", 1)[1].strip().decode())
            except (ValueError, TypeError):
                return 0
    return 0


async def _read_body(reader, body, content_length):
    started = time.ticks_ms()
    while len(body) < content_length:
        remaining = HTTP_BODY_DEADLINE_SEC - (
            time.ticks_diff(time.ticks_ms(), started) / 1000)
        if remaining <= 0:
            break
        try:
            more = await asyncio.wait_for(
                reader.read(content_length - len(body)), remaining)
        except asyncio.TimeoutError:
            break
        if not more:
            break
        body += more
    return body


MAX_HEADER_BYTES = 4096
MAX_BODY_BYTES = 4096
MAX_TRACKING_BODY_BYTES = 262144

# Keep transport policy next to the HTTP parser instead of repeating it in
# every handler. This first routing slice deliberately covers only endpoints
# with the same GET/HEAD contract and the same error response. Authentication
# and endpoint behaviour remain in their existing handlers.
READ_ONLY_ROUTES = frozenset(path for path, policy in ROUTE_POLICIES.items()
                             if policy[0] == ("GET", "HEAD"))


def _route_method_error(path, method):
    """Return the stable 405 message when a known route rejects *method*.

    ``None`` means that this policy slice does not decide the request. This
    lets action routes retain their more specific GET/POST/PUT policies while
    the router is migrated incrementally.
    """
    return _policy_method_error(path, method)


async def _read_request(reader):
    """Read the header up to the disconnector; TCP may fragment it at will."""
    data = b""
    started = time.ticks_ms()
    while b"\r\n\r\n" not in data:
        if len(data) >= MAX_HEADER_BYTES:
            return None, None, "header_too_large"
        remaining = HTTP_HEADER_DEADLINE_SEC - (
            time.ticks_diff(time.ticks_ms(), started) / 1000)
        if remaining <= 0:
            return None, None, "header_timeout"
        try:
            chunk = await asyncio.wait_for(
                reader.read(min(1024, MAX_HEADER_BYTES - len(data))),
                remaining)
        except asyncio.TimeoutError:
            return None, None, "header_timeout"
        if not chunk:
            return None, None, "header_incomplete"
        data += chunk
    header, body = data.split(b"\r\n\r\n", 1)
    return header, body, None

def _http_send(writer, status_line, content_type, payload, extra_headers=None):
    """Writes a full HTTP response. Content length is calculated from the BYTE length (len(str) was incorrect for umlauts)."""
    if isinstance(payload, str) and content_type.startswith("text/html"):
        payload = version_html(payload)
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    headers = {"X-Content-Type-Options": "nosniff",
               "Referrer-Policy": "no-referrer",
               "X-Frame-Options": "DENY",
               "Content-Security-Policy": CONTENT_SECURITY_POLICY}
    headers.update(extra_headers or {})
    extra = ""
    for key, value in headers.items():
        extra += "%s: %s\r\n" % (key, value)
    header = ("HTTP/1.0 %s\r\nContent-Type: %s\r\nConnection: close\r\n"
              "%sContent-Length: %d\r\n\r\n"
              % (status_line, content_type, extra, len(payload)))
    writer.write(header.encode("ascii"))
    writer.write(payload)


async def _http_send_file(writer, filename, content_type, method="GET",
                          accept_gzip=False):
    """Stream one public local asset without allocating it in full."""
    asset_file = None
    try:
        served_name = filename
        encoding = ""
        if accept_gzip:
            try:
                os.stat(filename + ".gz")
                served_name = filename + ".gz"
                encoding = "Content-Encoding: gzip\r\nVary: Accept-Encoding\r\n"
            except OSError:
                pass
        size = os.stat(served_name)[6]
        asset_file = open(served_name, "rb")
        header = (
            "HTTP/1.0 200 OK\r\nContent-Type: %s\r\nConnection: close\r\n"
            "X-Content-Type-Options: nosniff\r\nReferrer-Policy: no-referrer\r\n"
            "X-Frame-Options: DENY\r\nContent-Security-Policy: %s\r\n"
            "Cache-Control: public, max-age=31536000, immutable\r\n%s"
            "Content-Length: %d\r\n\r\n"
            % (content_type, CONTENT_SECURITY_POLICY, encoding, size))
        writer.write(header.encode("ascii"))
        if method != "HEAD":
            while True:
                chunk = asset_file.read(1024)
                if not chunk:
                    break
                writer.write(chunk)
                await writer.drain()
    except OSError:
        _http_send(writer, "404 Not Found", "text/plain", "Local asset missing",
                   {"Cache-Control": "no-store"})
    finally:
        if asset_file is not None:
            asset_file.close()


async def _send_tracking_json_stream(writer, chunks, method="GET",
                                      content_type="application/json", filename=None,
                                      first=None):
    # Validate the project/line before committing a success response. Iterators
    # emit their metadata first, before reading any large coordinate population.
    if first is None:
        first = next(chunks)
    disposition = ("Content-Disposition: attachment; filename=%s\r\n" % filename
                   if filename else "")
    header = ("HTTP/1.0 200 OK\r\nContent-Type: %s\r\nConnection: close\r\n"
              "X-Content-Type-Options: nosniff\r\nReferrer-Policy: no-referrer\r\n"
              "X-Frame-Options: DENY\r\nContent-Security-Policy: %s\r\n"
              "Cache-Control: no-store\r\n%s\r\n" % (
                  content_type, CONTENT_SECURITY_POLICY, disposition))
    writer.write(header.encode("ascii"))
    try:
        if method == "HEAD":
            return
        writer.write(first.encode("utf-8"))
        await writer.drain()
        pending = bytearray()
        for index, chunk in enumerate(chunks):
            encoded = chunk.encode("utf-8")
            if len(pending) + len(encoded) > 1024 and pending:
                writer.write(pending)
                await writer.drain()
                pending = bytearray()
            if len(encoded) >= 1024:
                writer.write(encoded)
                await writer.drain()
            else:
                pending.extend(encoded)
            if index % 4 == 3:
                await asyncio.sleep_ms(1)
        if pending:
            writer.write(pending)
            await writer.drain()
    finally:
        close = getattr(chunks, "close", None)
        if close:
            close()


async def _send_tracking_backup(writer, method="GET", project_id=None, source_tracker=None):
    header = (
        "HTTP/1.0 200 OK\r\nContent-Type: application/x-ndjson\r\n"
        "Connection: close\r\nX-Content-Type-Options: nosniff\r\n"
        "Cache-Control: no-store\r\nContent-Disposition: attachment; "
        "filename=%s\r\n\r\n" % ("project-archive-schema3.ndjson"
            if project_id else "tracking-schema3.ndjson"))
    writer.write(header.encode("ascii"))
    if method != "HEAD":
        chunks = (source_tracker or tracker).iter_backup_ndjson(project_id)
        try:
            for index, record in enumerate(chunks):
                writer.write(record.encode("utf-8"))
                await writer.drain()
                if index % 4 == 3: await asyncio.sleep_ms(1)
        finally: chunks.close()


async def _send_tracking_archive(writer, archive_id, method="GET"):
    metadata = next((item for item in tracker.archives
                     if item.get("id") == archive_id), None)
    if not metadata:
        raise ValueError("Archive not found.")
    path = tracker.archive_dir + "/" + archive_id + ".ndjson"
    source = None
    try:
        source = open(path, "rb")
        header = ("HTTP/1.0 200 OK\r\nContent-Type: application/x-ndjson\r\n"
                  "Connection: close\r\nX-Content-Type-Options: nosniff\r\n"
                  "Cache-Control: no-store\r\nContent-Disposition: attachment; "
                  "filename=survey-archive-schema3.ndjson\r\n\r\n")
        writer.write(header.encode("ascii"))
        if method != "HEAD":
            while True:
                chunk = source.read(1024)
                if not chunk: break
                writer.write(chunk); await writer.drain()
    finally:
        if source is not None: source.close()


def api_error(code, message):
    """Return the stable API error envelope used by every JSON endpoint."""
    return ujson.dumps({"error": code, "message": message})


def _send_json(writer, payload, status="200 OK", extra_headers=None):
    """Send one JSON response with the API's default no-cache policy."""
    headers = {"Cache-Control": "no-store"}
    headers.update(extra_headers or {})
    encoded = payload if isinstance(payload, str) else ujson.dumps(payload)
    _http_send(writer, status, "application/json", encoded, headers)


def _send_api_error(writer, status, code, message):
    """Send the stable machine-readable API error envelope."""
    _send_json(writer, api_error(code, message), status)


def _headers(header_lines):
    result = {}
    for line in header_lines[1:]:
        if b":" not in line:
            continue
        key, value = line.split(b":", 1)
        result[key.decode("ascii", "ignore").lower()] = value.strip().decode(
            "utf-8", "ignore")
    return result


def _admin_session(headers, require_csrf=False, body=b""):
    csrf = headers.get("x-csrf-token")
    if require_csrf and not csrf and body:
        csrf = parse_post_data(body).get("csrf_token")
    return authenticate(headers.get("cookie", ""),
                        csrf, require_csrf)


def _setup_ap_origin_null_allowed(headers, is_ap_request):
    if not is_ap_request or headers.get("origin", "").lower() != "null":
        return False
    if app.stats.get("access_state") not in ("SETUP", "RECOVERY", "APPLYING"):
        return False
    host = headers.get("host", "").lower().split(":", 1)[0]
    identity = app.identity or {}
    allowed = {str(CONFIG.get("ap_ip", "")).lower(),
               str(identity.get("hostname", "")).lower(),
               (str(identity.get("hostname", "")).lower() + ".local")}
    return bool(host) and host in allowed


def _origin_allowed(headers, is_ap_request=False):
    """Browser CSF also block on the implicitly familiar setup AP."""
    origin = headers.get("origin")
    if not origin:
        return True                    # curl, status.py, form without origin
    if origin.lower() == "null":
        return _setup_ap_origin_null_allowed(headers, is_ap_request)
    host = headers.get("host", "").lower()
    if not host:
        return False
    return origin.lower() in ("http://" + host, "https://" + host)


def _login_origin_allowed(headers, is_ap_request=False):
    """Login from normal browsers and local portal webviews. Some browsers send `Origin: null`` for local HTTP/mDNS pages. The login does not yet have a CSRF token; the secret device code and the existing failed attempt choke are the authorization. A specifically foreign browser original remains blocked.
    """
    origin = headers.get("origin")
    if not origin:
        return True
    if _origin_allowed(headers, is_ap_request):
        return True
    return headers.get("sec-fetch-site", "").lower() in ("same-origin", "same-site")


def _admin_allowed(is_ap_request, headers, method="GET", query="", body=b""):
    mutating = method not in ("GET", "HEAD")
    session = _admin_session(headers, mutating, body)
    if mutating and not _origin_allowed(headers, is_ap_request):
        return False
    return session is not None


LOGIN_HTML = """<!DOCTYPE html><html lang="en"><head><meta name="viewport"
content="width=device-width,initial-scale=1"><meta charset="utf-8">
<title>RTK sign-in</title><link rel="stylesheet" href="/base.css?v=ASSET_VERSION">
<link rel="stylesheet" href="/pages.css?v=ASSET_VERSION">
<script defer src="/i18n.js?v=ASSET_VERSION"></script><script defer src="/pages.js?v=ASSET_VERSION"></script></head>
<body class="page-login" data-page="login"><header class="product-header login-brand"><div class="product-identity"><b class="product-name">ESP-RTK</b><span data-i18n="ui.secure_field_receiver">Secure field receiver</span></div></header><main class="page-content login-card panel"><h1 data-i18n="ui.device_access">Device access</h1>
<p data-i18n="ui.enter_code">Enter the device code from the label.</p><form id="login-form" method="post" action="/api/session">
<input type="hidden" id="login-next" name="next" value="{next_path}">
<div class="login-row"><input id="login-device-code" type="password" class="login-code"
name="device_code" required autocomplete="current-password"
><button type="button" data-secret-toggle
aria-controls="login-device-code" aria-pressed="false"
 data-i18n="ui.show">Show</button></div><button class="login-submit primary" type="submit" data-i18n="action.login">Sign in</button></form>
<p class="muted" id="login-status" role="status" aria-live="polite"></p></main>
</body></html>"""


def _safe_return_path(value, fallback="/setup"):
    """Accept only known local UI destinations after administrator login."""
    value = str(value or "")
    if (len(value) > 256 or not value.startswith("/") or value.startswith("//")
            or any(char in value for char in ("\r", "\n", "\\"))):
        return fallback
    path = value.split("?", 1)[0].split("#", 1)[0]
    return value if path in ("/", "/ui", "/ui-v2", "/setup", "/diagnostics", "/label") else fallback


def render_login(next_path="/setup"):
    return version_html(LOGIN_HTML.replace("{next_path}", html_escape(
        _safe_return_path(next_path))))


LABEL_HTML = """<!DOCTYPE html><html lang="en"><head><meta name="viewport"
content="width=device-width,initial-scale=1"><meta charset="utf-8">
<title>{display_name} QR-Codes</title><link rel="stylesheet" href="/base.css?v=ASSET_VERSION">
<link rel="stylesheet" href="/pages.css?v=ASSET_VERSION">
<script defer src="/i18n.js?v=ASSET_VERSION"></script><script defer src="/qr.js?v=ASSET_VERSION"></script>
<script defer src="/pages.js?v=ASSET_VERSION"></script></head><body class="page-label" data-page="label">
<header class="product-header"><div class="product-identity"><b class="product-name">{display_name}</b><span data-i18n="ui.device_label">Device label</span></div><div class="product-actions"><button class="primary page-logout" type="button" data-i18n="ui.logout">Sign out</button></div></header>
<nav class="product-nav app-navigation"><a data-i18n="app.status" href="/#status">Status</a><a data-i18n="app.projects" href="/#projects">Projects</a><a class="portal-parent" data-i18n="app.device" href="/#device">Device</a><a data-i18n="ui.configuration" href="/setup{token_query}">Configuration</a><a data-i18n="nav.diagnostics" href="/diagnostics">Diagnostics</a><a data-i18n="nav.label" aria-current="page" href="/label">Label</a></nav>
<div class="device-status-bar" role="status" aria-live="polite" data-i18n="ui.loading_device_status">Loading device status …</div>
<main class="page-content"><h1 data-i18n="ui.device_label_access">Device label and access</h1>
<p data-i18n="ui.qr_local">The QR codes are generated entirely on this device.</p><div class="codes">
<section class="card"><h2 data-i18n="ui.device_label">Device label</h2><div class="qr" id="wifi-qr"
data-value="{wifi_qr}"></div><p><span data-i18n="ui.setup_network">Connects to</span> <b>{ap_ssid}</b>.
<span data-i18n="ui.setup_after">The captive portal then opens setup.</span>
<span data-i18n="ui.boot_wifi_hint">If the Wi-Fi network is not visible, hold BOOT for three seconds.</span></p><p class="qr-payload">{wifi_qr}</p></section>
<section class="card"><h2 data-i18n="ui.direct_portal">Direct web portal</h2><div class="qr" id="portal-qr"
data-value="{portal_qr}"></div><p data-i18n="ui.direct_portal_detail">For devices already on the same Wi-Fi network. The access code remains in the URL fragment and is then removed.</p>
<p class="qr-payload">{portal_qr}</p></section>
<section class="card"><h2>Bluetooth LE</h2><p data-i18n="ui.bluetooth_pairing_pin">Bluetooth pairing PIN</p>
<p class="pairing-pin">{ble_pin}</p>
<p data-i18n="ui.bluetooth_pairing_detail">Enter this PIN when Android requests Bluetooth pairing.</p></section></div></main>
</body></html>"""


def access_info():
    ident = app.identity or {}
    canonical = int(CONFIG.get("nmea_tcp_port", 0))
    snapshot = {
        "schema_version": 2,
        "device_id": ident.get("device_id"),
        "display_name": ident.get("display_name"),
        "access_state": app.stats.get("access_state"),
        "hostname": ((ident.get("hostname") + ".local")
                     if ident.get("hostname") else None),
        "http_port": int(CONFIG.get("http_port", 80)),
        "nmea_tcp": {
            "canonical_port": canonical,
            "legacy_ports": [p for p in nmea_tcp_ports() if p != canonical],
            "authentication_required": bool(CONFIG.get("nmea_tcp_auth_required", True)),
            "authentication": ("AUTH <stream_token>\\r\\n" if
                               CONFIG.get("nmea_tcp_auth_required", True) else None),
        },
        "ble_nus": {
            "name": ident.get("ble_name"),
            "service_uuid": "6e400001-b5a3-f393-e0a9-e50e24dcca9e",
            "tx_uuid": "6e400003-b5a3-f393-e0a9-e50e24dcca9e",
            "rx_uuid": "6e400002-b5a3-f393-e0a9-e50e24dcca9e",
            "single_client": True,
            "pairing_required": True,
            "bonding": True,
            "encrypted": True,
            "mitm_protection": "passkey",
        },
    }
    return snapshot


def wifi_qr_payload(identity_data=None):
    """Standard WIFI QR content; output only on admin routes."""
    ident = identity_data or app.identity or {}

    def escaped(value):
        text = str(value or "")
        for char in ("\\", ";", ",", ":", '"'):
            text = text.replace(char, "\\" + char)
        return text

    return "WIFI:T:WPA;S:%s;P:%s;;" % (
        escaped(ident.get("ap_ssid") or CONFIG.get("ap_ssid")),
        escaped(ident.get("ap_pass") or CONFIG.get("ap_pass")))


def portal_qr_payload(identity_data=None):
    """Direct login without access code in HTTP request, logs or refererer."""
    ident = identity_data or app.identity or {}
    hostname = ident.get("hostname") or "rtk"
    return "http://%s.local/setup#code=%s" % (
        hostname, url_encode(ident.get("device_code") or ""))


def render_label(token=None):
    ident = app.identity or {}
    values = {
        "display_name": ident.get("display_name", "RTK device"),
        "ap_ssid": CONFIG.get("ap_ssid") or ident.get("ap_ssid", ""),
        "wifi_qr": wifi_qr_payload(ident),
        "portal_qr": portal_qr_payload(ident),
        "ble_pin": ident.get("ble_pin", ""),
        "token_query": "",
    }
    html = LABEL_HTML
    for key, value in values.items():
        html = html.replace("{%s}" % key, html_escape(value))
    return version_html(html)


def masked_wifi_networks(networks=None):
    """Escape networks for APIs without revealing stored passwords."""
    result = []
    for entry in CONFIG.get("wifi_networks", []) if networks is None else networks:
        if isinstance(entry, (list, tuple)) and entry and entry[0]:
            password = entry[1] if len(entry) > 1 else ""
            result.append({"ssid": entry[0], "password_set": bool(password)})
    return result


def merge_masked_wifi_networks(networks, current=None):
    """Normalize editable API rows while retaining passwords hidden from clients."""
    if not isinstance(networks, list):
        return networks
    saved = dict((entry[0], entry[1]) for entry in
                 (CONFIG.get("wifi_networks", []) if current is None else current)
                 if isinstance(entry, (list, tuple)) and len(entry) == 2)
    result = []
    for entry in networks:
        if isinstance(entry, dict):
            ssid = entry.get("ssid")
            password = entry.get("password")
            if password is None and entry.get("password_set"):
                password = saved.get(ssid, "")
            result.append([ssid, password or ""])
        else:
            result.append(entry)
    return result


def wifi_scan_results():
    """Visible Wi-Fi networks without BSSID, deduplicated after SSID."""
    result = {}
    try:
        wlan = network.WLAN(network.STA_IF)
        if not wlan.active():
            wlan.active(True)
        for entry in wlan.scan():
            try:
                ssid = entry[0].decode("utf-8", "replace")
                rssi = int(entry[3])
                authmode = int(entry[4])
            except (IndexError, TypeError, ValueError):
                continue
            if not ssid:
                continue
            previous = result.get(ssid)
            if previous is None or rssi > previous["rssi"]:
                result[ssid] = {"ssid": ssid, "rssi": rssi,
                                "secured": authmode != 0}
    except Exception as error:
        return None, str(error)
    return sorted(result.values(), key=lambda item: item["rssi"], reverse=True), None


def diagnostics_snapshot():
    """Support package without stored secrets."""
    ntrip = _instances.get("ntrip")
    ble = _instances.get("ble")
    rtcm_report = (_instances["ntrip"].rtcm.summary()
                   if "ntrip" in _instances else None)
    if rtcm_report is not None:
        rtcm_report = dict(rtcm_report)
        rtcm_report["mount"] = CONFIG.get("ntrip_mount")
    fix = dict(app.last_fix or {})
    snapshot = {
        "schema_version": 1,
        "identity": dict((key, (app.identity or {}).get(key)) for key in
                         ("device_id", "display_name", "hostname", "ble_name")),
        "firmware": {"esp": CONFIG.get("version"),
                     "gnss_responses": list(gnss_replies[-20:])},
        "network": {"state": app.stats.get("access_state"),
                    "mode": app.stats.get("net_mode"),
                    "ssid": app.stats.get("wifi_ssid_aktiv"),
                    "sta_ip": app.stats.get("sta_ip"),
                    "ap_ip": app.stats.get("ap_ip"),
                    "configured": {"primary_ssid": CONFIG.get("wifi_ssid"),
                                   "fallbacks": masked_wifi_networks()}},
        "field_diagnostics": field_recorder.status(),
        "config_apply": config_result_read(),
        "fix": fix,
        "rtcm": ntrip.rtcm.summary() if ntrip else None,
        "transports": {"ble_clients": len(ble.connections) if ble else 0,
                       "ble_encrypted": len(ble.encrypted_connections) if ble else 0,
                       "tcp_clients": len(fanout_clients),
                       "tcp_ports": nmea_tcp_ports()},
        "tracking": tracker.status(),
        "board": board_metrics(),
        "stats": dict(app.stats),
        "recent_errors": list(app.errors),
        "logs": {"runtime": read_flight_recorder(),
                 "previous": previous_flight_recorder(),
                 "flash": read_error_log()},
    }
    secrets = [CONFIG.get(key) for key in
               ("wifi_pass", "ntrip_pass", "ntrip_user",
                "ntrip_fallback_pass", "ntrip_fallback_user")]
    secrets += [(app.identity or {}).get(key) for key in ("device_code", "stream_token")]
    secrets = [value for value in secrets if isinstance(value, str) and len(value) >= 3]

    def redact(value):
        if isinstance(value, str):
            for secret in secrets:
                value = value.replace(secret, "[REDACTED]")
            return value
        if isinstance(value, dict):
            return dict((key, redact(item)) for key, item in value.items())
        if isinstance(value, list):
            return [redact(item) for item in value]
        return value
    return redact(snapshot)


def config_backup():
    """Full operational data; identity and uptime values remain outside."""
    keys = tuple(SETTABLE_KEYS) + ("wifi_networks",)
    return {"schema_version": 1, "kind": "esp-rtk-config-backup",
            "config": dict((key, CONFIG.get(key)) for key in keys),
            "credentials": {"ap_pass": CONFIG.get("ap_pass"),
                            "device_code": (app.identity or {}).get("device_code")}}


def validate_backup(payload):
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        return None, "backup_schema_version must be 1"
    if payload.get("kind") != "esp-rtk-config-backup" or not isinstance(payload.get("config"), dict):
        return None, "Not a valid ESP-RTK configuration backup"
    supplied = dict(payload["config"])
    networks = supplied.pop("wifi_networks", [])
    values, error = validate_settings(supplied)
    if error:
        return None, error
    clean, error = validate_wifi_networks(networks)
    if error:
        return None, error
    values["wifi_networks"] = clean
    return values, None

def _still(f, *a):
    """Call f(*a) and return None if it doesn't exist here. Not every firmware knows every reading - a missing one must not cause /status to crash.
    """
    try:
        return f(*a)
    except Exception:
        return None


def _client_disconnected(error):
    """Separate normal breaks from server errors by browsers/probes."""
    errno = error.args[0] if getattr(error, "args", None) else None
    return errno in (32, 54, 103, 104, 107, 128)


_BOARD_SLOW_CACHE = {}
_BOARD_RADIO_CACHE = {}
_BOARD_SLOW_AT = None
_BOARD_RADIO_AT = None


def board_metrics(fresh=False):
    """Everything that the device gives about itself. The purpose is diagnostics in the field: a bad radio connection (rssi) or a hot chip (mcu_temp_c) explain fades that you otherwise only see as "does not work".
    """
    global _BOARD_SLOW_CACHE, _BOARD_RADIO_CACHE, _BOARD_SLOW_AT, _BOARD_RADIO_AT
    now = time.ticks_ms()
    values = {
        "cpu_hz": _still(machine.freq),
        "ram_free_bytes": _still(gc.mem_free),
        "ram_alloc_bytes": _still(gc.mem_alloc),
    }
    if (fresh or _BOARD_SLOW_AT is None or
            time.ticks_diff(now, _BOARD_SLOW_AT) >= 60000):
        slow = {"flash_bytes": None, "fs_total_bytes": None,
                "fs_free_bytes": None, "idf_heap_free_bytes": None,
                "idf_heap_largest_block_bytes": None,
                "idf_heap_minimum_free_bytes": None}
        stat = _still(os.statvfs, "/")
        if stat:
            slow["fs_total_bytes"] = stat[0] * stat[2]
            slow["fs_free_bytes"] = stat[0] * stat[3]
        try:
            import esp
            slow["flash_bytes"] = _still(esp.flash_size)
        except Exception:
            pass
        try:
            import esp32
            regions = _still(esp32.idf_heap_info, esp32.HEAP_DATA)
            if regions:
                slow["idf_heap_free_bytes"] = sum(r[0] for r in regions)
                slow["idf_heap_largest_block_bytes"] = max(r[2] for r in regions)
                slow["idf_heap_minimum_free_bytes"] = sum(r[3] for r in regions)
        except Exception:
            pass
        _BOARD_SLOW_CACHE, _BOARD_SLOW_AT = slow, now
    if (fresh or _BOARD_RADIO_AT is None or
            time.ticks_diff(now, _BOARD_RADIO_AT) >= 10000):
        radio = {"mcu_temp_c": None, "wifi_rssi": None,
                 "wifi_channel": None, "wifi_txpower_dbm": None,
                 "ap_clients": None}
        try:
            import esp32
            radio["mcu_temp_c"] = _still(esp32.mcu_temperature)
        except Exception:
            pass
        sta = _still(network.WLAN, network.STA_IF)
        if sta is not None and _still(sta.isconnected):
            radio["wifi_rssi"] = _still(sta.status, "rssi")
            radio["wifi_channel"] = _still(sta.config, "channel")
            radio["wifi_txpower_dbm"] = _still(sta.config, "txpower")
        ap = _still(network.WLAN, network.AP_IF)
        if ap is not None and _still(ap.active):
            stations = _still(ap.status, "stations")
            if stations is not None:
                radio["ap_clients"] = len(stations)
        _BOARD_RADIO_CACHE, _BOARD_RADIO_AT = radio, now
    values.update(_BOARD_SLOW_CACHE)
    values.update(_BOARD_RADIO_CACHE)

    return values


def _bounded_backup_lines(path):
    with open(path, "rb") as source:
        first, pending = True, b""
        while True:
            block = source.read(4096)
            pending += block
            while b"\n" in pending or (not block and pending):
                at = pending.find(b"\n")
                if at < 0: at = len(pending) - 1
                raw, pending = pending[:at + 1], pending[at + 1:]
                if len(raw) > 32768:
                    raise ValueError("A backup record is too large.")
                if first:
                    header = ujson.loads(raw)
                    if (not isinstance(header, dict) or header.get("record") != "header" or
                            header.get("kind") != "survey_backup" or header.get("schema_version") != 4):
                        raise ValueError("Use an integrity-protected NDJSON survey backup.")
                    first = False
                yield raw.decode("utf-8")
            if len(pending) > 32768:
                raise ValueError("A backup record is too large.")
            if not block: break


async def _handle_tracking_import(reader, writer, initial, length, is_ap_request, headers):
    """Receive large backups on flash; publish only after complete verification."""
    if not _admin_allowed(is_ap_request, headers, "POST"):
        _send_api_error(writer, "403 Forbidden", "admin_session_required",
                        "Administrator sign-in required.")
        return
    if headers.get("content-type", "").split(";", 1)[0] != "application/x-ndjson":
        _send_api_error(writer, "415 Unsupported Media Type", "ndjson_required",
                        "Use an NDJSON survey backup.")
        return
    if (not tracker._loaded or tracker.storage_busy or tracker._compacting or
            tracker.active_line_id or tracker._pending_fixes or MEASUREMENT_PROGRESS.get("active") or
            _instances.get("parallel_tracker") or
            app.stats.get("parallel_benchmark_state") in ("armed", "stopping")):
        _send_api_error(writer, "409 Conflict", "storage_busy",
                        "Finish the active operation before restoring survey data.")
        return
    if length <= 0:
        _send_api_error(writer, "400 Bad Request", "empty_backup", "The backup is empty.")
        return
    if not tracker.storage_capacity(length * 2 + 32768, force=True)["writable"]:
        _send_api_error(writer, "507 Insufficient Storage", "storage_reserve",
                        "Not enough space to restore while preserving existing data.")
        return
    path = tracker.path + ".upload.ndjson"
    tracker.storage_busy = True
    previous_reason = app.maintenance_reason
    app.maintenance_reason = "tracking_restore"
    tracker.operation_progress.update(operation="restore", state="receiving", processed=0,
                                      total=length, error=None)
    try:
        while tracker.storage_readers: await asyncio.sleep_ms(20)
        started, received = time.ticks_ms(), 0
        with open(path, "wb") as target:
            block = initial[:length]
            while received < length:
                if block:
                    target.write(block); received += len(block)
                    tracker.operation_progress["processed"] = received
                    await asyncio.sleep_ms(1)
                if received == length: break
                if time.ticks_diff(time.ticks_ms(), started) > 180000:
                    raise ValueError("Backup upload timed out.")
                block = await asyncio.wait_for(reader.read(min(4096, length - received)), 10)
                if not block: raise ValueError("Backup upload is incomplete.")
        tracker.operation_progress["state"] = "validating"
        result = await tracker.restore_ndjson_async(_bounded_backup_lines(path))
        tracker.operation_progress.update(state="complete", processed=result["features"],
                                          total=result["features"])
        _send_json(writer, dict(result, tracking_live=tracker.live_status()))
    except (ValueError, TypeError, UnicodeError, asyncio.TimeoutError) as error:
        tracker.operation_progress.update(state="failed", error=str(error))
        _send_api_error(writer, "400 Bad Request", "invalid_backup", str(error))
    except (OSError, MemoryError) as error:
        tracker.operation_progress.update(state="failed", error=str(error))
        _send_api_error(writer, "507 Insufficient Storage", "restore_failed",
                        "Could not restore the backup.")
    finally:
        try: os.remove(path)
        except OSError: pass
        tracker.storage_busy = False
        app.maintenance_reason = previous_reason


async def _handle_tracking_request(writer, path, method, query, body,
                                   is_ap_request, headers):
    if not _admin_allowed(is_ap_request, headers, method, query, body):
        _send_api_error(writer, "403 Forbidden", "admin_session_required",
                        "Administrator sign-in required.")
        return
    changing = method == "POST" and path == "/api/tracking"
    reading = method in ("GET", "HEAD") and path in (
        "/api/tracking/map", "/api/tracking/line", "/api/tracking/export")
    if (changing or reading) and (tracker.storage_busy or tracker._compacting):
        _send_api_error(writer, "409 Conflict" if changing else "503 Service Unavailable",
                        "storage_busy", "Storage maintenance in progress. Please wait.")
        return
    maintenance = False
    if changing:
        try: action = ujson.loads(body.decode("utf-8")).get("action")
        except (ValueError, AttributeError): action = None
        maintenance = action in ("archive_project", "reactivate_archive", "verify_archive",
                                 "compact", "restore")
        if maintenance and (tracker.active_line_id or tracker._pending_fixes or
                            MEASUREMENT_PROGRESS.get("active")):
            _send_api_error(writer, "409 Conflict", "recording_active",
                            "Finish the active measurement before storage maintenance.")
            return
    if reading: tracker.storage_readers += 1
    previous_reason = app.maintenance_reason
    if maintenance:
        tracker.storage_busy = True
        app.maintenance_reason = "tracking_storage"
        tracker.operation_progress.update(operation=action, state="running", error=None)
    try:
        if maintenance:
            while tracker.storage_readers:
                await asyncio.sleep_ms(20)
        await _dispatch_tracking_request(writer, path, method, query, body,
                                         is_ap_request, headers)
    finally:
        if reading: tracker.storage_readers -= 1
        if maintenance:
            tracker.storage_busy = False
            app.maintenance_reason = previous_reason


async def _dispatch_tracking_request(writer, path, method, query, body,
                                   is_ap_request, headers):
    """Handle the tracking API as one domain boundary."""
    if not _admin_allowed(is_ap_request, headers, method, query, body):
        _send_api_error(writer, "403 Forbidden", "admin_session_required",
                        "Administrator sign-in required.")
        return
    params = parse_query(query)
    if (path == "/api/tracking" and method == "POST" and
            (_instances.get("parallel_tracker") or
             app.stats.get("parallel_benchmark_state") in ("armed", "stopping"))):
        _send_api_error(writer, "409 Conflict", "benchmark_active",
                        "Stop the parallel benchmark before changing production projects.")
        return
    if not tracker._loaded:
        _send_api_error(writer, "503 Service Unavailable", "survey_loading",
                        "Survey storage is loading or unavailable. Please wait.")
        return
    if path == "/api/tracking/map":
        try:
            limit = max(1, min(int(params.get("limit", 500)), 500))
        except (ValueError, TypeError):
            limit = 500
        try:
            since_revision = (int(params["since_revision"])
                              if "since_revision" in params else None)
        except (ValueError, TypeError):
            since_revision = None
        await _send_tracking_json_stream(writer, tracker.iter_map_data(
            limit, params.get("project_id"), since_revision), method)
        return
    if path == "/api/tracking/line":
        chunks = tracker.iter_line_detail(params.get("line_id"), params.get("offset", 0),
                                         params.get("limit", 25), params.get("project_id"))
        try:
            first = next(chunks)
        except ValueError as error:
            _send_api_error(writer, "404 Not Found", "line_not_found", str(error))
            return
        await _send_tracking_json_stream(writer, chunks, method, first=first)
        return
    if path == "/api/tracking/export":
        if params.get("format") not in ("csv", "backup", "archive"):
            chunks = tracker.iter_geojson(params.get("project_id"))
            try:
                first = next(chunks)
            except ValueError as error:
                _send_api_error(writer, "404 Not Found", "project_not_found", str(error))
                return
            await _send_tracking_json_stream(writer, chunks, method,
                "application/geo+json", "tracking.geojson", first)
            return
        try:
            if params.get("format") == "csv":
                payload, content_type, suffix = (tracker.csv(params.get("project_id")),
                                                 "text/csv; charset=utf-8", "csv")
            elif params.get("format") == "backup":
                await _send_tracking_backup(writer, method, params.get("project_id"))
                return
            elif params.get("format") == "archive":
                await _send_tracking_archive(writer, params.get("archive_id"), method)
                return
            _http_send(writer, "200 OK", content_type, payload,
                       {"Cache-Control": "no-store", "Content-Disposition":
                        "attachment; filename=tracking.%s" % suffix})
        except ValueError as error:
            _send_api_error(writer, "404 Not Found", "project_not_found", str(error))
        return
    if method in ("GET", "HEAD"):
        if params.get("view") == "name_check":
            try:
                _send_json(writer, tracker.name_available(params.get("name"),
                                                          params.get("kind")))
            except ValueError as error:
                _send_api_error(writer, "400 Bad Request", "invalid_name_check",
                                str(error))
            return
        if params.get("view") == "progress":
            _send_json(writer, {"measurement_progress": dict(MEASUREMENT_PROGRESS),
                                "storage_operation": dict(tracker.operation_progress),
                                "current_fix": app.last_fix,
                                "current_quality": quality_check(app.last_fix),
                                "quality_streak": app.quality_streaks()})
            return
        result = tracker.status()
        result["current_fix"] = app.last_fix
        result["current_quality"] = quality_check(app.last_fix)
        result["quality_streak"] = app.quality_streaks()
        result["measurement_progress"] = dict(MEASUREMENT_PROGRESS)
        result["storage_operation"] = dict(tracker.operation_progress)
        _send_json(writer, result)
        return
    try:
        action_started = time.ticks_ms()
        supplied = ujson.loads(body.decode("utf-8"))
        if not isinstance(supplied, dict):
            raise ValueError("The request body must be an object.")
        action = supplied.get("action")
        if action == "cancel_measurement":
            result = cancel_measurement()
        elif action == "create_project":
            result = tracker.create_project(supplied.get("name"),
                                            supplied.get("description", ""))
        elif action == "select_project":
            tracker.select_project(supplied.get("project_id")); result = {}
        elif action == "start_line":
            recording_mode = supplied.get("recording_mode", "survey")
            # A route must become durable immediately. Subsequent GNSS epochs
            # carry their complete accepted/rejected quality evidence.
            fix = (None if recording_mode == "route" else
                   await averaged_current_fix(supplied.get("samples", 1)))
            line = tracker.start_line(supplied.get("name"),
                supplied.get("auto_distance_m", 1), supplied.get("properties"), fix,
                recording_mode)
            # start_line() returns the canonical in-memory object. During the
            # durable append its point arrays are replaced with FlashSequence
            # views, which must never be handed to ujson. Return a detached,
            # deliberately small transport representation instead.
            result = {"id": line["id"], "name": line["name"],
                      "state": line["state"],
                      "recording_mode": line.get("recording_mode", "survey")}
        elif action == "add_vertex":
            result = tracker.add_vertex(await averaged_current_fix(
                supplied.get("samples", 1)))
        elif action == "add_asset":
            result = tracker.add_asset(supplied.get("object_type", "other"),
                supplied.get("name", ""), supplied.get("note", ""),
                await averaged_current_fix(supplied.get("samples", 1)),
                supplied.get("link_to_active_line", True))
        elif action == "undo":
            tracker.undo_last(); result = {}
        elif action == "rename_project":
            tracker.rename_project(supplied.get("project_id"), supplied.get("name"))
            result = {}
        elif action == "delete_project":
            tracker.delete_project(supplied.get("project_id")); result = {}
        elif action == "archive_project":
            result = await tracker.archive_project_async(supplied.get("project_id"))
        elif action == "verify_archive":
            result = await tracker.verify_archive_async(supplied.get("archive_id"))
        elif action == "reactivate_archive":
            result = await tracker.reactivate_archive_async(supplied.get("archive_id"))
        elif action == "compact":
            await tracker.compact_async(); result = tracker.status()
        elif action == "restore":
            log("INFO", "TRACK", "Survey restore started.")
            app.maintenance_reason = "tracking_restore"
            try:
                await tracker.restore_async(supplied.get("backup"))
            finally:
                app.maintenance_reason = None
            log("INFO", "TRACK", "Survey restore completed (%s features)." %
                tracker.feature_count())
            result = tracker.status()
        elif action in ("pause", "resume", "finish"):
            diagnostics = None
            if action == "finish":
                diagnostics = dict((key, app.stats.get(key)) for key in (
                    "queue_overflows", "ble_drops", "nmea_tcp_drops", "crc_errors",
                    "gga_parse_errors", "task_restarts", "ntrip_retries", "ntrip_bytes"))
            tracker.set_line_state(action, diagnostics); result = {}
        else:
            raise ValueError("Unknown tracking action.")
    except (ValueError, TypeError) as error:
        if 'action' in locals() and action == "restore":
            log("ERROR", "TRACK", "Survey restore rejected: %s" % error)
        elif ('action' in locals() and action in ("start_line", "add_vertex", "add_asset")
              and str(error) != "Measurement cancelled."):
            log("WARN", "TRACK", "Measurement rejected (%s): %s" % (action, error))
        _send_api_error(writer, "400 Bad Request", "invalid_tracking_request", str(error))
        return
    except OSError as error:
        log("ERROR", "TRACK", "Could not persist tracking data: %s" % error)
        _send_api_error(writer, "507 Insufficient Storage", "tracking_storage_failed",
                        "Could not store tracking data.")
        return
    action_ms = max(0, time.ticks_diff(time.ticks_ms(), action_started))
    app.stats["last_tracking_action_ms"] = action_ms
    app.stats["max_tracking_action_ms"] = max(
        app.stats.get("max_tracking_action_ms", 0), action_ms)
    if isinstance(result, dict):
        # Domain methods often return the same dictionary that is retained in
        # the in-memory survey model. Never attach transport metadata to it.
        result = dict(result)
    else:
        result = {"result": result}
    result["tracking_live"] = tracker.live_status()
    result["tracking_performance"] = {
        "action": action, "action_ms": action_ms,
        "append_ms": getattr(tracker, "last_append_ms", 0)}
    # Keep transport failures separate from journal failures. A browser closing
    # the response after a successful mutation must not be reported as lost data.
    _send_json(writer, result)


async def _handle_session_request(writer, method, headers, body, peer_ip,
                                  is_ap_request=False):
    """Handle administrator session inspection, login, and logout."""
    if method == "GET":
        session = _admin_session(headers)
        if session is None:
            _send_json(writer, {"authenticated": False}, "401 Unauthorized")
        else:
            _send_json(writer, {"authenticated": True,
                                "csrf_token": session["csrf"]})
        return
    if method == "DELETE":
        if (not _origin_allowed(headers, is_ap_request) or
                _admin_session(headers, True, body) is None):
            _http_send(writer, "403 Forbidden", "text/plain",
                       "Administrator session and CSRF token required")
            return
        logout(headers.get("cookie", ""))
        _http_send(writer, "204 No Content", "text/plain", "",
                   {"Set-Cookie": "rtk_session=; Max-Age=0; Path=/; "
                                  "HttpOnly; SameSite=Strict"})
        return
    if not _login_origin_allowed(headers, is_ap_request):
        _http_send(writer, "403 Forbidden", "text/plain", "Origin rejected")
        return
    content_type = headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            values = ujson.loads(body.decode("utf-8"))
        except (ValueError, TypeError):
            values = {}
    else:
        values = parse_post_data(body)
    expected = (app.identity or {}).get("device_code", "")
    session, error = login(values.get("device_code", ""), expected, peer_ip)
    if error:
        status = "429 Too Many Requests" if error == "blocked" else "401 Unauthorized"
        _http_send(writer, status, "text/plain", "Invalid device code.")
        return
    cookie = ("rtk_session=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=%d"
              % (session["token"], 8 * 60 * 60))
    next_path = _safe_return_path(values.get("next"))
    if "application/json" in content_type:
        _send_json(writer, {"csrf_token": session["csrf"], "redirect": next_path},
                   extra_headers={"Set-Cookie": cookie})
    else:
        _http_send(writer, "303 See Other", "text/plain", "Signed in",
                   {"Set-Cookie": cookie, "Location": next_path})


async def _handle_field_control(writer, method, body, is_ap_request, headers):
    await _handle_field_diagnostics(writer, "/api/field-diagnostics", method, body, is_ap_request, headers)


async def _handle_field_export(writer, method, body, is_ap_request, headers):
    await _handle_field_diagnostics(writer, "/api/field-diagnostics/export", method, body, is_ap_request, headers)


async def _handle_field_diagnostics(writer, path, method, body, is_ap_request, headers):
    if not _admin_allowed(is_ap_request, headers, method, "", body):
        _send_api_error(writer, "403 Forbidden", "admin_session_required",
                        "Administrator session and CSRF token required.")
        return
    field_recorder.initialize()
    if path.endswith("/export"):
        if field_recorder.enabled or field_recorder.export_readers:
            _send_api_error(writer, "409 Conflict", "diagnostics_busy",
                            "Stop field diagnostics and wait for any download to finish.")
            return
        field_recorder.export_readers += 1
        try:
            writer.write(("HTTP/1.0 200 OK\r\nContent-Type: application/x-ndjson\r\n"
                          "Connection: close\r\nCache-Control: no-store\r\n"
                          "X-Content-Type-Options: nosniff\r\nContent-Disposition: attachment; "
                          "filename=field-diagnostics.ndjson\r\n\r\n").encode())
            if method != "HEAD":
                for item in field_recorder.files:
                    with open(item[1], "rb") as handle:
                        while True:
                            chunk = handle.read(1024)
                            if not chunk:
                                break
                            writer.write(chunk)
                            await asyncio.wait_for(writer.drain(), 10)
                            await asyncio.sleep_ms(1)
                    # Isolate a partial tail left by an abrupt power interruption.
                    writer.write(b"\n")
        finally:
            field_recorder.export_readers -= 1
        return
    if method == "GET":
        _send_json(writer, field_recorder.status())
        return
    try:
        values = ujson.loads(body.decode("utf-8"))
        if not isinstance(values, dict):
            raise ValueError("A JSON object is required.")
        action = values.get("action")
        if action == "start":
            field_recorder.start()
        elif action == "stop":
            field_recorder.stop()
        elif action == "mark" and field_recorder.enabled:
            label = values.get("label", "marker")
            if label not in ("marker", "stationary", "walking", "hotspot_off", "hotspot_on", "power_off"):
                raise ValueError("Unknown field marker.")
            field_recorder.event("marker", {"label": label})
            field_recorder.flush()
        else:
            raise ValueError("Unknown action or field diagnostics are stopped.")
    except (ValueError, TypeError) as error:
        _send_api_error(writer, "409 Conflict", "diagnostics_refused", str(error))
        return
    except OSError:
        _send_api_error(writer, "503 Service Unavailable", "diagnostics_storage_error",
                        "Field diagnostics settings could not be saved.")
        return
    _send_json(writer, field_recorder.status())


async def _handle_diagnostics_request(writer, path, query, is_ap_request, headers):
    """Handle diagnostics, Wi-Fi scan, config result, and backup reads."""
    if not _admin_allowed(is_ap_request, headers, "GET", query):
        content = "text/html; charset=utf-8" if path == "/diagnostics" else "application/json"
        payload = (render_login(path) if path == "/diagnostics" else
                   api_error("admin_session_required", "Administrator sign-in required."))
        _http_send(writer, "401 Unauthorized", content, payload,
                   {"Cache-Control": "no-store"})
        return
    if path == "/diagnostics":
        _http_send(writer, "200 OK", "text/html; charset=utf-8", DIAGNOSTICS_HTML,
                   {"Cache-Control": "no-store"})
    elif path == "/api/wifi-scan":
        manager = _instances.get("network")
        active_line = tracker.status().get("active_line") or {}
        recording = (active_line.get("state") == "active" or
                     bool(MEASUREMENT_PROGRESS.get("active")))
        if recording:
            _send_api_error(writer, "409 Conflict", "wifi_scan_busy",
                            "Pause the active recording before scanning for Wi-Fi networks.")
        elif manager is None:
            _send_api_error(writer, "503 Service Unavailable", "wifi_scan_unavailable",
                            "Network manager is unavailable.")
        else:
            state, networks, error = manager.request_scan()
            if state == "pending":
                _send_json(writer, {"status": "pending"}, "202 Accepted")
            elif error:
                _send_api_error(writer, "503 Service Unavailable", "wifi_scan_failed",
                                "Wi-Fi scan failed.")
            else:
                _send_json(writer, {"status": "ready", "networks": networks})
    elif path == "/api/config-result":
        _send_json(writer, config_result_read())
    elif path == "/api/config/backup":
        _send_json(writer, config_backup(), extra_headers={
            "Content-Disposition": "attachment; filename=esp-rtk-config-backup.json"})
    else:
        response_headers = {}
        if parse_query(query).get("download") == "1":
            response_headers["Content-Disposition"] = "attachment; filename=esp-rtk-diagnostics.json"
        _send_json(writer, diagnostics_snapshot(), extra_headers=response_headers)


async def _handle_config_request(writer, path, method, query, body,
                                 is_ap_request, headers):
    """Handle JSON config read/stage/restore/apply and the legacy save form."""
    if not _admin_allowed(is_ap_request, headers, method, query, body):
        if path == "/save":
            _http_send(writer, "403 Forbidden", "text/plain",
                       "Configuration access expired or invalid. Open /setup and sign in again.")
        else:
            _send_api_error(writer, "403 Forbidden", "admin_session_required",
                            "Administrator sign-in required.")
        return
    if path == "/api/config/apply":
        _send_json(writer, {"applying": True}, "202 Accepted")
        app.set_access_state("APPLYING")
        asyncio.create_task(do_soft_reboot_deferred())
        return
    if path == "/api/config/restore":
        try:
            payload = ujson.loads(body.decode("utf-8"))
        except (ValueError, TypeError):
            payload = None
        values, error = validate_backup(payload)
        if error:
            _send_api_error(writer, "400 Bad Request", "invalid_backup",
                            "The backup is invalid: %s" % error)
        elif not stage_config(values):
            _send_api_error(writer, "500 Internal Server Error", "stage_failed",
                            "Could not stage the configuration.")
        else:
            _send_json(writer, {"restored": True}, "202 Accepted")
            asyncio.create_task(do_soft_reboot_deferred())
        return
    if path == "/save":
        fields = parse_post_data(body)
        networks_text = fields.pop("wifi_networks", None)
        values, error = validate_settings(fields)
        if error:
            log("ERROR", "HTTP", "Configuration rejected: %s" % error)
            _http_send(writer, "400 Bad Request", "text/html; charset=utf-8",
                       '<html lang="en"><body><h2>Rejected</h2><p>%s</p>'
                       '<p><a href="/">Back</a></p></body></html>' % html_escape(error))
            return
        if networks_text is not None:
            values["wifi_networks"] = parse_configured_networks(
                networks_text, CONFIG.get("wifi_networks"))
        ok = stage_config(values)
        if ok:
            _http_send(writer, "200 OK", "text/html; charset=utf-8",
                       '<html lang="en"><body><h2>Saved!</h2>'
                       '<p>Restarting in 5 seconds...</p></body></html>')
            asyncio.create_task(do_soft_reboot_deferred())
        else:
            _http_send(writer, "500 Internal Server Error", "text/html; charset=utf-8",
                       '<html lang="en"><body><h2>Save failed!</h2></body></html>')
        return
    if method == "GET":
        result = dict((key, CONFIG.get(key)) for key in
                      ("wifi_ssid", "ntrip_enabled", "ntrip_host", "ntrip_port",
                       "ntrip_mount", "ntrip_user", "ntrip_fallback_host",
                       "ntrip_fallback_port", "ntrip_fallback_mount",
                       "ntrip_fallback_user"))
        result["wifi_networks"] = masked_wifi_networks()
        result["wifi_pass_set"] = bool(CONFIG.get("wifi_pass"))
        result["ntrip_pass_set"] = bool(CONFIG.get("ntrip_pass"))
        result["ntrip_fallback_pass_set"] = bool(CONFIG.get("ntrip_fallback_pass"))
        _send_json(writer, result)
        return
    try:
        supplied = ujson.loads(body.decode("utf-8"))
    except (ValueError, TypeError):
        supplied = None
    if not isinstance(supplied, dict):
        _send_api_error(writer, "400 Bad Request", "invalid_json",
                        "The request body is not valid JSON.")
        return
    networks = supplied.pop("wifi_networks", None)
    values, error = validate_settings(supplied)
    if error:
        _send_api_error(writer, "400 Bad Request", "invalid_configuration",
                        "The configuration is invalid: %s" % error)
        return
    if networks is not None:
        networks = merge_masked_wifi_networks(networks)
        networks, network_error = validate_wifi_networks(networks)
        if network_error:
            _send_api_error(writer, "400 Bad Request", "invalid_wifi_networks",
                            network_error)
            return
        values["wifi_networks"] = networks
    if not stage_config(values):
        _send_api_error(writer, "500 Internal Server Error", "stage_failed",
                        "Could not stage the configuration.")
    else:
        _send_json(writer, {"pending": True}, "202 Accepted")


async def _handle_parallel_benchmark_export_request(writer, method, body,
                                                    is_ap_request, headers):
    if not _admin_allowed(is_ap_request, headers, method, "", body):
        _send_api_error(writer, "403 Forbidden", "admin_session_required",
                        "Administrator session required.")
        return
    from parallel_benchmark import control_status, PREFIX
    from tracking import Tracker
    status = control_status()
    if status["state"] in ("armed", "waiting_services", "starting", "resuming", "running", "stopping"):
        _send_api_error(writer, "409 Conflict", "benchmark_active",
                        "Wait for the benchmark to finish before exporting.")
        return
    result = status.get("result")
    if not result:
        _send_api_error(writer, "404 Not Found", "benchmark_result_missing",
                        "No completed benchmark data is available.")
        return
    source = _instances.get("parallel_tracker")
    opened = source is None
    try:
        if opened:
            source = Tracker(PREFIX, max_features=result["target_points"],
                             autoload=False, point_store_autoload=False)
            await source.point_store.load_async()
            source._load()
        if source.feature_count() != result["confirmed_points"]:
            _send_api_error(writer, "409 Conflict", "benchmark_count_mismatch",
                            "Stored benchmark count differs from the result.")
            return
        await _send_tracking_backup(writer, method, source_tracker=source)
    finally:
        if opened and source is not None:
            source.point_store.close()


async def _handle_parallel_benchmark_request(writer, method, body,
                                             is_ap_request, headers):
    if not _admin_allowed(is_ap_request, headers, method, "", body):
        _send_api_error(writer, "403 Forbidden", "admin_session_required",
                        "Administrator session and CSRF token required.")
        return
    from parallel_benchmark import control, control_status
    if method == "GET":
        _send_json(writer, control_status())
        return
    if not tracker._loaded:
        _send_api_error(writer, "503 Service Unavailable", "survey_loading",
                        "Wait for survey storage before starting a benchmark.")
        return
    try:
        values = ujson.loads(body.decode("utf-8"))
        if not isinstance(values, dict):
            raise ValueError("A JSON object is required.")
        if MEASUREMENT_PROGRESS.get("active"):
            raise ValueError("Wait for the current measurement to finish.")
        action = "cleanup" if method == "DELETE" else values.get("action")
        result = control(action, tracker, values.get("target_points"), values.get("confirm"),
                         profile=values.get("profile", "parallel"))
    except (ValueError, TypeError) as error:
        _send_api_error(writer, "409 Conflict", "benchmark_refused", str(error))
        return
    except OSError:
        _send_api_error(writer, "500 Internal Server Error", "benchmark_storage_error",
                        "Benchmark control could not be saved.")
        return
    _send_json(writer, result)
    if result.get("reboot_required"):
        asyncio.create_task(do_soft_reboot_deferred())


async def _handle_measurement_gate_request(writer, method, body,
                                           is_ap_request, headers):
    """Read, update, or reset the persistent field acceptance limits."""
    if not _admin_allowed(is_ap_request, headers, method, "", body):
        _send_api_error(writer, "403 Forbidden", "admin_session_required",
                        "Administrator sign-in required.")
        return
    if method == "GET":
        _send_json(writer, {"values": measurement_gate(),
                            "defaults": measurement_gate(True)})
        return
    if method == "DELETE":
        defaults = measurement_gate(True)
        clean, _error = validate_measurement_gate(defaults)
    else:
        try:
            supplied = ujson.loads(body.decode("utf-8"))
        except (ValueError, TypeError):
            supplied = None
        clean, error = validate_measurement_gate(supplied)
        if error:
            _send_api_error(writer, "400 Bad Request", "invalid_measurement_gate",
                            error)
            return
    if not save_config(clean):
        _send_api_error(writer, "500 Internal Server Error", "save_failed",
                        "Measurement gate could not be saved.")
        return
    _send_json(writer, {"values": measurement_gate(),
                        "defaults": measurement_gate(True),
                        "reset": method == "DELETE"})


async def _handle_tcp_auth_request(writer, method, body,
                                   is_ap_request, headers):
    """Read or change whether TCP clients must authenticate before NMEA."""
    if not _admin_allowed(is_ap_request, headers, method, "", body):
        _send_api_error(writer, "403 Forbidden", "admin_session_required",
                        "Administrator sign-in required.")
        return
    if method == "GET":
        _send_json(writer, {"required": bool(CONFIG.get(
            "nmea_tcp_auth_required", True)), "default": True})
        return
    try:
        supplied = ujson.loads(body.decode("utf-8"))
    except (ValueError, TypeError):
        supplied = None
    if not isinstance(supplied, dict) or not isinstance(supplied.get("required"), bool):
        _send_api_error(writer, "400 Bad Request", "invalid_tcp_auth_setting",
                        "required must be true or false.")
        return
    if not save_config({"nmea_tcp_auth_required": supplied["required"]}):
        _send_api_error(writer, "500 Internal Server Error", "save_failed",
                        "TCP authentication setting could not be saved.")
        return
    _send_json(writer, {"required": bool(CONFIG["nmea_tcp_auth_required"]),
                        "default": True})


async def _handle_maintenance_request(writer, path, method, query, body,
                                      is_ap_request, headers):
    """Handle BLE maintenance mode and NMEA stream-token rotation."""
    if not _admin_allowed(is_ap_request, headers, method, query, body):
        _send_api_error(writer, "403 Forbidden", "admin_session_required",
                        "Administrator sign-in required.")
        return
    if path == "/api/ble-maintenance":
        manager = _instances.get("ble")
        if manager is None:
            _send_api_error(writer, "503 Service Unavailable", "ble_unavailable",
                            "Bluetooth LE is unavailable.")
        else:
            _send_json(writer, {"enabled_for_sec":
                                manager.enable_command_maintenance(600)})
        return
    if path == "/api/reboot":
        _send_json(writer, {"restarting": True}, "202 Accepted")
        asyncio.create_task(do_soft_reboot_deferred())
        return
    token = rotate_stream_token()
    if token is None:
        _send_api_error(writer, "500 Internal Server Error", "rotation_failed",
                        "Could not rotate the stream token.")
        return
    app.identity["stream_token"] = token
    for client in list(fanout_clients):
        try:
            client.close()
        except Exception:
            pass
    fanout_clients[:] = []
    _send_json(writer, {"stream_token": token})


async def _handle_ui_live_request(writer, query, is_ap_request, headers):
    """Serve the compact, allocation-conscious snapshot used by the V2 field loop."""
    if not _read_allowed(is_ap_request, headers, query):
        _send_api_error(writer, "401 Unauthorized", "admin_session_required",
                        "Administrator sign-in required.")
        return
    ntrip = _instances.get("ntrip")
    rtcm = dict(ntrip.rtcm.summary() if ntrip else {})
    rtcm["mount"] = CONFIG.get("ntrip_mount")
    live = tracker.live_status()
    live.update({
        "schema_version": 1,
        "tracking_startup": dict(app.stats.get("tracking_startup") or
                                 {"state": "ready" if tracker._loaded else "loading"}),
        "current_fix": app.last_fix or {"fix_status_text": "NO_FIX", "qual": 0,
                                         "sats": 0},
        "current_quality": quality_check(app.last_fix),
        "field_diagnostics": field_recorder.status(),
        "quality_streak": app.quality_streaks(),
        "route_startup": app.stats.get("route_startup"),
        "gnss_accuracy": {"state": app.stats.get("gst_output_state", "waiting"),
                          "attempts": app.stats.get("gst_enable_attempts", 0)},
        "measurement_progress": dict(MEASUREMENT_PROGRESS),
        "rtcm": rtcm,
        "network": {"access_state": app.stats.get("access_state"),
                    "net_mode": app.stats.get("net_mode"),
                    "last_wifi_error": app.stats.get("last_wifi_error"),
                    "wifi_status_code": app.stats.get("wifi_status_code"),
                    "reconnects": app.stats.get("wifi_reconnects", 0)},
        "board": board_metrics(False),
        "system": {"version": CONFIG["version"],
                   "uptime_sec": app.stats["uptime_sec"],
                   "display_name": (app.identity or {}).get("display_name")},
    })
    _send_json(writer, live)


async def _handle_status_request(writer, path, query, is_ap_request, headers):
    """Handle live status, RTCM summaries, and persistent error reports."""
    if path == "/api/log":
        if not _admin_allowed(is_ap_request, headers, "GET", query):
            _send_api_error(writer, "401 Unauthorized", "admin_session_required",
                            "Administrator sign-in required.")
            return
        params = parse_query(query)
        try: limit = min(200, max(1, int(params.get("limit", 200))))
        except (ValueError, TypeError): limit = 200
        level = params.get("level")
        if level not in (None, "", "ERROR", "WARN", "INFO", "DEBUG"):
            _send_api_error(writer, "400 Bad Request", "invalid_log_level",
                            "Unknown log level.")
            return
        _send_json(writer, incremental_error_log(params.get("cursor"), limit,
                                                  level or None))
        return
    if path == "/errors":
        if not _admin_allowed(is_ap_request, headers, "GET", query):
            _http_send(writer, "401 Unauthorized", "text/plain",
                       "Administrator session required.")
            return
        parts = []
        previous = previous_flight_recorder()
        if previous:
            parts.append("=== History before the last restart (RTC memory) ===\n" + previous)
        current = read_flight_recorder()
        if current:
            parts.append("=== Current session (RTC memory) ===\n" + current)
        flash = read_error_log()
        if flash:
            parts.append("=== Error log on flash ===\n" + flash)
        _http_send(writer, "200 OK", "text/plain; charset=utf-8",
                   "\n\n".join(parts) or "(no entries yet)",
                   {"Cache-Control": "no-store"})
        return
    if not _read_allowed(is_ap_request, headers, query):
        if path == "/rtcm":
            _http_send(writer, "401 Unauthorized", "text/plain",
                       "Token required: /rtcm?token=...")
        else:
            _send_api_error(writer, "401 Unauthorized", "admin_session_required",
                            "Administrator sign-in required.")
        return
    if path == "/rtcm":
        ntrip = _instances.get("ntrip")
        report = ntrip.rtcm.summary() if ntrip else {}
        report["mount"] = CONFIG["ntrip_mount"]
        report["host"] = CONFIG["ntrip_host"]
        _send_json(writer, report)
        return
    wlan_if = network.WLAN(network.AP_IF if is_ap_request else network.STA_IF)
    ip = (wlan_if.ifconfig()[0] if
          (wlan_if.active() if is_ap_request else wlan_if.isconnected()) else "N/A")
    ble = _instances.get("ble")
    ntrip = _instances.get("ntrip")
    rtcm_report = ntrip.rtcm.summary() if ntrip else {}
    rtcm_report = dict(rtcm_report or {})
    rtcm_report["mount"] = CONFIG.get("ntrip_mount")
    fix_age = (round(time.time() - app.last_fix_time, 1)
               if app.last_fix_time is not None else None)
    _send_json(writer, {
        "fix": app.last_fix or {"fix_status_text": "NO_FIX", "qual": 0, "sats": 0},
        "fix_age_sec": fix_age, "stats": app.stats, "recent_errors": app.errors,
        "field_diagnostics": field_recorder.status(),
        "log_entries": error_log_entries(),
        "quality_streak": app.quality_streaks(),
        "ble": {"clients": len(ble.connections) if ble else 0,
                "encrypted_clients": len(ble.encrypted_connections) if ble else 0,
                "payload_bytes": ble.payload_size if ble else None,
                "drops": app.stats["ble_drops"]},
        "nmea_tcp": {"clients": len(fanout_clients),
                     "port": CONFIG.get("nmea_tcp_port", 0),
                     "ports": nmea_tcp_ports(), "drops": app.stats["nmea_tcp_drops"]},
        "rtcm": rtcm_report,
        "ntrip_flow": (ntrip.flow_summary() if ntrip and
                       hasattr(ntrip, "flow_summary") else None),
        "heartbeat_age_sec": dict((name, round(time.ticks_diff(
            time.ticks_ms(), stamp) / 1000, 1)) for name, stamp in app.heartbeat.items()),
        "board": board_metrics(),
        "system": {"version": CONFIG["version"],
                   "reset_cause": app.stats["reset_cause"],
                   "boot_count": app.stats["boot_count"],
                   "safe_mode": app.stats["safe_mode"],
                   "uptime_sec": app.stats["uptime_sec"],
                   "ram_free_bytes": gc.mem_free(), "ip": ip,
                   "mac": ubinascii.hexlify(wlan_if.config("mac"), ":").decode(),
                   "device_id": (app.identity or {}).get("device_id"),
                   "display_name": (app.identity or {}).get("display_name"),
                   "hostname": (app.identity or {}).get("hostname")}})


async def _handle_gnss_request(writer, path, method, query, body,
                               is_ap_request, headers):
    """Handle validated GNSS commands and the command-reply buffer."""
    if not _admin_allowed(is_ap_request, headers, method, query, body):
        _send_api_error(writer, "403 Forbidden", "admin_session_required",
                        "Administrator sign-in required.")
        return
    if method == "GET":
        _http_send(writer, "200 OK", "text/plain; charset=utf-8",
                   "\n".join(gnss_replies) or "(no responses yet)",
                   {"Cache-Control": "no-store"})
        return
    gnss_obj = _instances.get("gnss")
    if gnss_obj is None:
        _http_send(writer, "503 Service Unavailable", "text/plain",
                   "GNSS handler is not running (safe mode?).")
        return
    if path == "/api/gnss-command" and "application/json" in headers.get("content-type", ""):
        try:
            sentence = ujson.loads(body.decode("utf-8")).get("sentence", "")
        except (ValueError, TypeError, AttributeError):
            sentence = ""
    else:
        sentence = body.decode("ascii", "ignore")
    ok, error = send_command(gnss_obj.uart, sentence)
    _http_send(writer, "200 OK" if ok else "400 Bad Request", "text/plain",
               "sent - retrieve the response with GET /gnss" if ok else error)


async def http_server_task():
    log("INFO", "HTTP", "HTTP server started on port %s." % CONFIG["http_port"])
    srv = None
    try:
        srv = await asyncio.start_server(handle_http_client, "0.0.0.0", CONFIG["http_port"])
        log("INFO", "HTTP", "HTTP server running (listeners active on AP and STA).")

        while not shutdown_event.is_set():
            await asyncio.sleep(1)

    except Exception as e:
        log("ERROR", "HTTP", "Critical HTTP server error.", print_traceback=True, exception=e)
        app.log_error("HTTP_Server", str(e))
    finally:
        if srv:
            try:
                srv.close()
                await srv.wait_closed()
            except Exception:
                pass

    log("INFO", "HTTP", "HTTP server task stopped.")

def _settings_handler(path):
    return {
        "/api/parallel-benchmark": _handle_parallel_benchmark_request,
        "/api/parallel-benchmark/export": _handle_parallel_benchmark_export_request,
        "/api/field-diagnostics": _handle_field_control,
        "/api/field-diagnostics/export": _handle_field_export,
        "/api/measurement-gate": _handle_measurement_gate_request,
        "/api/tcp-auth": _handle_tcp_auth_request,
    }.get(path)


async def handle_http_client(reader, writer):
    """Parse one request, establish its interface context and dispatch it."""
    global _http_connections
    admitted = False
    try:
        if _http_connections >= HTTP_MAX_CONNECTIONS:
            _http_send(writer, "503 Service Unavailable", "text/plain",
                       "Too many HTTP connections.", {"Retry-After": "2"})
            await writer.drain()
            return
        _http_connections += 1
        admitted = True
        header, body, request_error = await _read_request(reader)
        if request_error:
            log("WARN", "HTTP", "Request rejected: %s." % request_error)
            status = ("431 Request Header Fields Too Large"
                      if request_error == "header_too_large"
                      else "400 Bad Request")
            _http_send(writer, status, "text/plain", "Invalid HTTP request.")
            await writer.drain()
            return

        header_lines = header.split(b"\r\n")
        headers = _headers(header_lines)
        request_line = header_lines[0].decode("ascii", "ignore")

        parts = request_line.split(" ")
        if len(parts) < 2:
            return

        method = parts[0]
        path, _, query = parts[1].partition("?")
        app.stats["http_requests"] = app.stats.get("http_requests", 0) + 1

        content_length = _parse_content_length(header_lines)
        body_limit = _route_body_limit(path)
        if content_length > body_limit:
            _http_send(writer, "413 Payload Too Large", "text/plain",
                       "Request body is too large.")
            await writer.drain()
            return
        if method in ("POST", "PUT", "PATCH", "DELETE") and path != "/api/tracking/import":
            body = await _read_body(reader, body, content_length)
            if len(body) != content_length:
                _http_send(writer, "400 Bad Request", "text/plain",
                           "Request body is incomplete.")
                await writer.drain()
                return

        # Prefer the local destination address. Firmware without sockname may
        # use the subnet fallback only when AP and STA networks cannot collide.
        try:
            peer = writer.get_extra_info("peername")
        except Exception as e:
            # An unavailable peer address is an explicit request failure.
            log("ERROR", "HTTP", "Could not determine client origin: %s" % e)
            app.log_error("HTTP_Handler", "peername is unavailable")
            _http_send(writer, "503 Service Unavailable", "text/plain",
                       "Unable to determine request origin.")
            await writer.drain()
            return
        peer_ip = peer[0] if peer else ""
        try:
            ap_ip = network.WLAN(network.AP_IF).ifconfig()[0]
        except Exception:
            ap_ip = CONFIG["ap_ip"]
        try:
            local = writer.get_extra_info("sockname")
            local_ip = local[0] if local else ""
        except Exception:
            local_ip = ""
        if local_ip:
            is_ap_request = local_ip == ap_ip
        else:
            sta_ip = app.stats.get("sta_ip", "")
            safe_fallback = not _same_subnet(ap_ip, sta_ip)
            is_ap_request = (safe_fallback and
                             peer_ip.startswith(_subnet_prefix(ap_ip)))

        method_error = _route_method_error(path, method)
        if method_error:
            _http_send(writer, "405 Method Not Allowed", "text/plain",
                       method_error)
            await writer.drain()
            return

        # Redirect captive-portal probes to local setup instead of claiming
        # that Internet access is available.
        if path in ("/generate_204", "/gen_204", "/hotspot-detect.html",
                    "/library/test/success.html", "/ncsi.txt",
                    "/connecttest.txt", "/redirect"):
            _http_send(writer, "302 Found", "text/plain", "Setup",
                       {"Location": "/setup", "Cache-Control": "no-store"})
            await writer.drain()
            return

        if path == "/api/captive":
            portal_url = "http://%s/setup" % CONFIG["ap_ip"]
            _http_send(writer, "200 OK", "application/captive+json",
                       ujson.dumps({"captive": bool(is_ap_request),
                                    "user-portal-url": portal_url}),
                       {"Cache-Control": "no-store"})
            await writer.drain()
            return

        if path == "/api/access":
            _send_json(writer, access_info())
            await writer.drain()
            return

        if path == "/api/ui/live":
            await _handle_ui_live_request(writer, query, is_ap_request, headers)
            await writer.drain()
            return

        if path == "/api/tracking/import":
            await _handle_tracking_import(reader, writer, body, content_length, is_ap_request, headers)
            await writer.drain()
            return

        if path in ("/api/tracking", "/api/tracking/export", "/api/tracking/line",
                    "/api/tracking/map"):
            await _handle_tracking_request(writer, path, method, query, body,
                                           is_ap_request, headers)
            await writer.drain()
            return
        if path in STATIC_ASSETS:
            asset_name, content_type = STATIC_ASSETS[path]
            await _http_send_file(writer, asset_name, content_type, method,
                                  "gzip" in headers.get("accept-encoding", "").lower())
            await writer.drain()
            return

        if path == "/label":
            if not _admin_allowed(is_ap_request, headers, "GET", query):
                _http_send(writer, "401 Unauthorized", "text/html; charset=utf-8",
                           render_login("/label"), {"Cache-Control": "no-store"})
            else:
                _http_send(writer, "200 OK", "text/html; charset=utf-8",
                           render_label(parse_query(query).get("token")),
                           {"Cache-Control": "no-store"})
            await writer.drain()
            return

        if path == "/api/label":
            if not _admin_allowed(is_ap_request, headers, "GET", query):
                _send_api_error(writer, "401 Unauthorized",
                                "admin_session_required",
                                "Administrator sign-in required.")
            else:
                ident = app.identity or {}
                _send_json(writer, {
                    "schema_version": 1,
                    "display_name": ident.get("display_name"),
                    "device_id": ident.get("device_id"),
                    "device_code": ident.get("device_code"),
                    "stream_token": ident.get("stream_token"),
                    "ble_pin": ident.get("ble_pin"),
                    "hostname": ((ident.get("hostname") + ".local")
                                 if ident.get("hostname") else None),
                    "wifi_qr": wifi_qr_payload(ident),
                    "portal_qr": portal_qr_payload(ident),
                })
            await writer.drain()
            return

        if path == "/api/session":
            await _handle_session_request(writer, method, headers, body, peer_ip,
                                          is_ap_request)
            await writer.drain(); return

        if path in ("/diagnostics", "/api/diagnostics", "/api/wifi-scan",
                    "/api/config-result", "/api/config/backup"):
            await _handle_diagnostics_request(writer, path, query,
                                              is_ap_request, headers)
            await writer.drain(); return

        if path == "/api/config/restore":
            await _handle_config_request(writer, path, method, query, body,
                                         is_ap_request, headers)
            await writer.drain(); return

        # The root is the status page; /ui remains a compatible redirect.
        if path == "/ui":
            _http_send(writer, "302 Found", "text/plain", "Moved",
                       {"Location": "/", "Cache-Control": "no-store"})
            await writer.drain()
            return

        if path == "/ui-v2":
            if not _read_allowed(is_ap_request, headers, query):
                _http_send(writer, "401 Unauthorized", "text/html; charset=utf-8",
                           APP_V2_LOGIN, {"Cache-Control": "no-store"})
            else:
                _http_send(writer, "200 OK", "text/html; charset=utf-8", APP_V2,
                           {"Cache-Control": "no-store"})
            await writer.drain()
            return

        if path == "/":
            if not _read_allowed(is_ap_request, headers, query):
                _http_send(writer, "401 Unauthorized", "text/html; charset=utf-8",
                           APP_V2_LOGIN, {"Cache-Control": "no-store"})
            else:
                _http_send(writer, "200 OK", "text/html; charset=utf-8", APP_V2,
                           {"Cache-Control": "no-store"})
            await writer.drain()
            return

        if path == "/setup":
            if not _admin_allowed(is_ap_request, headers, "GET", query):
                _http_send(writer, "401 Unauthorized", "text/html; charset=utf-8",
                           render_login(parse_query(query).get("return") or "/setup"),
                           {"Cache-Control": "no-store"})
                await writer.drain()
                return
            token = None if is_ap_request else parse_query(query).get("token")
            session = _admin_session(headers)
            _http_send(writer, "200 OK", "text/html; charset=utf-8",
                       render_setup(token, session.get("csrf") if session else None),
                       {"Cache-Control": "no-store"})
            await writer.drain()
            return

        if path in ("/api/config", "/api/config/apply"):
            await _handle_config_request(writer, path, method, query, body,
                                         is_ap_request, headers)
            await writer.drain(); return

        settings_handler = _settings_handler(path)
        if settings_handler:
            await settings_handler(writer, method, body, is_ap_request, headers)
            await writer.drain(); return

        if path in ("/api/ble-maintenance", "/api/stream-token/rotate",
                    "/api/reboot"):
            await _handle_maintenance_request(writer, path, method, query, body,
                                              is_ap_request, headers)
            await writer.drain(); return

        # ROUTE 2: save configuration and restart.
        if path == "/save":
            await _handle_config_request(writer, path, method, query, body,
                                         is_ap_request, headers)
            await writer.drain(); return

        if path in ("/status", "/health", "/rtcm", "/errors", "/api/log"):
            await _handle_status_request(writer, path, query,
                                         is_ap_request, headers)
            await writer.drain(); return

        if path in ("/gnss", "/api/gnss-command"):
            await _handle_gnss_request(writer, path, method, query, body,
                                       is_ap_request, headers)
            await writer.drain(); return

        # Default 404
        _http_send(writer, "404 Not Found", "text/plain",
                   "Not found. Configuration: / (AP only), "
                   "UI: /ui, Status: /status, RTCM: /rtcm, "
                   "error log: /errors, Module: /gnss, Configuration: /")
        await writer.drain()

    except asyncio.TimeoutError:
        # Browsers may open speculative connections without sending requests;
        # a timeout is therefore not an application error.
        log("DEBUG", "HTTP", "Connection closed without a request (timeout).")
    except OSError as e:
        if _client_disconnected(e):
            log("DEBUG", "HTTP", "Client closed connection: %s" % e)
        else:
            app.stats["http_errors"] += 1
            log("ERROR", "HTTP", "HTTP handler error.",
                print_traceback=True, exception=e)
            app.log_error("HTTP_Handler", str(e))
    except Exception as e:
        app.stats["http_errors"] += 1
        error_msg = str(e)
        log("ERROR", "HTTP", "HTTP handler error.",
            print_traceback=True, exception=e)
        app.log_error("HTTP_Handler", error_msg)
    finally:
        if admitted:
            _http_connections -= 1
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

async def do_soft_reboot_deferred():
    """Performs a soft reboot with delaying."""
    await asyncio.sleep(4)
    log("INFO", "HTTP", "Performing soft reboot after configuration save.")
    # Mark the planned reset because the S3 reset cause cannot distinguish it
    # from a panic reset.
    mark_planned_reset()
    shutdown_event.set()
    await asyncio.sleep(1)  # tasks give time to quit
    machine.reset()
