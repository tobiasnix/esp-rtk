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

from access import authenticate, login, logout, session_active
from assets import version_html
from cfg import (CONFIG, BOOT_ID, CONFIG_WARNINGS, SETTABLE_KEYS, _same_subnet, _subnet_prefix,
                 DEFAULT_CONFIG, read_error_log,
                 error_log_entries,
                 incremental_error_log,
                 config_result_read,
                 read_flight_recorder, previous_flight_recorder, html_escape, log,
                 mark_planned_reset, parse_configured_networks, configured_networks_text,
                 prepare_config_change, prepare_restore_change, apply_live, stage_config,
                 apply_config_change, apply_staged_config, config_restart_required,
                 pending_config_active, rollback_open, staged_config, config_file_protected,
                 staging_revision)
from cfg_logging import HOT_PATH_LOG_INTERVAL_MS, LogThrottle, throttle_note
from cfg_validation import (FIELD_SETUP_KEYS, STAKEOUT_SETTINGS_KEYS,
                            validate_measurement_samples, validate_stakeout_settings)
from field_diagnostics import recorder as field_recorder
from fanout import clients as fanout_clients, disconnect_all_clients, nmea_tcp_ports
from identity import rotate_stream_token
from pointstore import _elapsed as _elapsed_us, _ticks_us
from ui import APP_V2, APP_V2_LOGIN
from gnss import COMMAND_BUSY, COMMAND_QUEUE_FULL, replies as gnss_replies
from measurement import measurement_session
import events
import geo
import stakeout
import stakeout_session
import stakeout_progress
from stakeout import StakeoutError
from state import _instances, app, corrections_health, shutdown_event
from tracking import (average_fixes, field_setup, FrameAssignmentError, measurement_gate,
                      measurement_gate_deviations, MEASUREMENT_GATE_KEYS, SCHEMA_VERSION,
                      MeasurementRejected, StorageBusy, StorageReserveError, tracker,
                      UNDO_UNCHECKED, UndoCandidateChanged, validate_field_setup,
                      validate_measurement_gate)
from web_routing import (CAPTIVE_PROBES, ROUTES as ROUTE_POLICIES,
                         body_limit as _route_body_limit,
                         method_error as _policy_method_error)


HTTP_MAX_CONNECTIONS = 6
# Flash commits can delay a ready request beyond five seconds. Keep one
# bounded header deadline within the same ten-second service budget as bodies.
HTTP_HEADER_DEADLINE_SEC = 10
HTTP_BODY_DEADLINE_SEC = 10
# After this much work the GeoJSON export yields at the next chunk boundary.
EXPORT_PACE_MS = 20
EXPORT_HEAP_SAMPLE_MS = 1000
# Bounds one blocked drain to a vanished client; no total deadline.
EXPORT_DRAIN_TIMEOUT_SEC = 120
_http_connections = 0


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


def current_fix_view(now_ms=None):
    """One fix snapshot for every endpoint: fix fields plus tick-based freshness.

    The quality is the cached gate verdict (app.gate_state), so it never contradicts
    the gate streak. A stale fix keeps its position fields but is judged by
    quality_check(None).
    """
    if now_ms is None:
        now_ms = time.ticks_ms()
    freshness = app.fix_state(now_ms)
    gate = app.gate_state(now_ms)
    current = dict(app.last_fix or {"fix_status_text": "NO_FIX", "qual": 0,
                                    "sats": 0})
    current.update(freshness)
    current["pending_gst"] = gate["pending_gst"]
    if gate["pending_gst"]:
        # The accuracy the held verdict was judged on, so that the browser keeps
        # showing it while this epoch's GST is still on its way.
        current["held_receiver_accuracy"] = gate["held_receiver_accuracy"]
    return current, gate["quality"]


def _short_text(value, limit=80):
    """value as printable ASCII without '"' and '\\' (others become '?'), cut to limit.

    None stays None. Keeps a free error text at limit + 2 serialised bytes under json
    and ujson alike.
    """
    if value is None:
        return None
    # Each character maps to one, so cutting first gives the same text with less work.
    return "".join(c if " " <= c <= "~" and c not in "\"\\" else "?"
                   for c in str(value)[:limit])


def frame_suggestion_view():
    """The service frame of the open correction stream, rated against the field
    setup (#38b), or None without a 1300 announcement or without an NTRIP client."""
    ntrip = _instances.get("ntrip")
    return ntrip.rtcm.frame_suggestion(field_setup()) if ntrip else None


def network_view():
    """The network block of the live state: codes and numbers only (about 250 bytes).

    One source for /api/ui/live and for GET /api/tracking?view=progress, which carries
    it so the NTRIP cause stays current while a measurement keeps the live poll quiet.
    """
    return {"access_state": app.stats.get("access_state"),
            "ntrip_state": app.stats.get("ntrip_state"),
            "ntrip_substate": app.stats.get("ntrip_substate"),
            "ntrip_config": app.stats.get("ntrip_config"),
            "last_ntrip_error_code": app.stats.get("last_ntrip_error_code"),
            "net_mode": app.stats.get("net_mode"),
            "last_wifi_error": app.stats.get("last_wifi_error"),
            "wifi_status_code": app.stats.get("wifi_status_code"),
            "reconnects": app.stats.get("wifi_reconnects", 0)}


def ui_live_snapshot(now_ms=None):
    """The compact, allocation-conscious live state: the one source of /api/ui/live.

    Pure: no writer, no log line, no stats write. Serialise the result at once and
    do not hold it across an await; nested dicts are shallow copies of live state.
    Also the frame body of the planned live event stream (#27).
    """
    if now_ms is None:
        now_ms = time.ticks_ms()
    ntrip = _instances.get("ntrip")
    rtcm = dict(ntrip.rtcm.summary() if ntrip else {})
    rtcm["mount"] = CONFIG.get("ntrip_mount")
    live = tracker.live_status()
    current_fix, current_quality = current_fix_view(now_ms)
    live.update({
        "schema_version": 1,
        "tracking_startup": dict(app.stats.get("tracking_startup") or
                                 {"state": "ready" if tracker.is_loaded() else "loading"}),
        "current_fix": current_fix,
        "current_quality": current_quality,
        "field_diagnostics": field_recorder.status(),
        "quality_streak": app.quality_streaks(now_ms),
        "gate_deviations": measurement_gate_deviations(),
        "measurement_defaults": dict(field_setup(),
                                     samples=CONFIG.get("measurement_samples", 5)),
        "frame_suggestion": frame_suggestion_view(),
        "config_warnings": list(CONFIG_WARNINGS),
        "route_startup": app.stats.get("route_startup"),
        "gnss_accuracy": {"state": app.stats.get("gst_output_state", "waiting"),
                          "attempts": app.stats.get("gst_enable_attempts", 0),
                          "error": _short_text(app.stats.get("gst_output_error"))},
        "measurement_progress": measurement_session.snapshot(),
        "rtcm": rtcm,
        "link": link_health(now_ms, rtcm),
        "network": network_view(),
        "board": board_metrics(False),
        "system": {"version": CONFIG["version"],
                   "uptime_sec": app.stats["uptime_sec"],
                   "boot_count": app.stats["boot_count"],
                   "display_name": (app.identity or {}).get("display_name"),
                   "boot_id": BOOT_ID},
    })
    return live


def link_health(now_ms, rtcm_report):
    """Receiver and correction link health (#20a): one clock reading for the receiver.

    rtcm_report is the rtcm dict of the same snapshot, so corrections and rtcm.stale
    never disagree; None or {} counts as not stale. rtcm_silence_ms counts from the
    last recognised RTCM message number of the running NTRIP client.
    """
    ntrip = _instances.get("ntrip")
    return {"receiver": app.receiver_link(now_ms),
            "uart_silence_ms": app.uart_silence_ms(now_ms),
            "corrections": corrections_health(app.stats.get("ntrip_state"),
                                              bool((rtcm_report or {}).get("stale"))),
            "rtcm_silence_ms": ntrip.rtcm.silence_ms() if ntrip else None}


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
<p class="notice fallback-notice"><span data-i18n="ui.legacy_setup_notice">This is the basic fallback page. The field app offers the full setup under Device &gt; Configuration.</span> <a href="{field_app_setup}" data-i18n="ui.open_field_app_setup">Open the field app setup</a></p>
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
        <p class="network-help" data-i18n="ui.network_format_help">Format: Name:Password. If only the name is provided, the saved password is retained. The device tries the networks in the order entered: the Wi-Fi network above first, then these lines from top to bottom. A scan only skips networks that are not visible; if it sees none of them, the device tries all of them. Enter the Wi-Fi network normally used above as SSID and password; this field is only for additional alternatives.</p>
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
    html = html.replace("{field_app_setup}", html_escape(CAPTIVE_PORTAL_PATH))
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
    """Read the header up to the disconnector; TCP may fragment it at will.

    On an error the first element holds the bytes received so far (b"" when
    none came), so the dispatcher can still tell a HEAD request apart. A
    header whose request line is not METHOD /target HTTP/1.x is rejected as
    request_line_invalid.
    """
    data = b""
    started = time.ticks_ms()
    while b"\r\n\r\n" not in data:
        if len(data) >= MAX_HEADER_BYTES:
            return data, None, "header_too_large"
        remaining = HTTP_HEADER_DEADLINE_SEC - (
            time.ticks_diff(time.ticks_ms(), started) / 1000)
        if remaining <= 0:
            return data, None, "header_timeout"
        try:
            chunk = await asyncio.wait_for(
                reader.read(min(1024, MAX_HEADER_BYTES - len(data))),
                remaining)
        except asyncio.TimeoutError:
            return data, None, "header_timeout"
        if not chunk:
            return data, None, "header_incomplete"
        data += chunk
    header, body = data.split(b"\r\n\r\n", 1)
    if not _request_line_valid(header.split(b"\r\n", 1)[0]):
        return header, None, "request_line_invalid"
    return header, body, None


def _request_line_valid(line):
    """Exactly three fields: upper-case letters, an origin-form target, HTTP/1.0 or 1.1."""
    try:
        text = line.decode("ascii")
    except UnicodeError:
        return False
    if len(text) != len(line):
        return False                   # MicroPython decodes UTF-8 for every codec name
    parts = text.split(" ")
    return (len(parts) == 3 and parts[0].isalpha() and parts[0].isupper() and
            parts[1].startswith("/") and parts[2] in ("HTTP/1.0", "HTTP/1.1"))


_REQUEST_REJECT_LOG = LogThrottle(HOT_PATH_LOG_INTERVAL_MS)


def _log_request_rejected(reason):
    # Throttled per reason: a scan can reject many requests; the total is /status stats.http_rejected_requests.
    app.stats["http_rejected_requests"] = app.stats.get("http_rejected_requests", 0) + 1
    n = _REQUEST_REJECT_LOG.hit(reason)
    if n:
        log("WARN", "HTTP", "Request rejected: %s.%s" % (reason, throttle_note(n)))

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
    header_sent = False
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
        header_sent = True             # before the write: a failed write may be partial
        writer.write(header.encode("ascii"))
        if method != "HEAD":
            while True:
                chunk = asset_file.read(1024)
                if not chunk:
                    break
                writer.write(chunk)
                await writer.drain()
    except OSError:
        if header_sent:
            raise                      # the answer began; the dispatcher only closes
        _http_send(writer, "404 Not Found", "text/plain", "Local asset missing",
                   {"Cache-Control": "no-store"})
    finally:
        if asset_file is not None:
            asset_file.close()


async def _send_tracking_json_stream(writer, chunks, method="GET",
                                      content_type="application/json", filename=None,
                                      first=None, pace_ms=None, report=None,
                                      drain_timeout_sec=None):
    """Stream chunks as one 200 answer.

    pace_ms None yields every fourth chunk (map, line detail); otherwise the
    stream yields at the next chunk boundary once more than pace_ms passed
    since its last own yield (a drain is no yield). drain_timeout_sec bounds
    each drain. report, if given, is filled in place, also on an abort (#42 E5).
    """
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
    # Body bytes, own yields, largest slice in ms; microseconds in next(), drain(), yields.
    tally, spent = [0, 0, 0], [0, 0, 0]
    last = sampled = time.ticks_ms()
    ram_free = gc.mem_free() if report is not None else None

    async def send(data):
        writer.write(data)
        tally[0] += len(data)
        started = _ticks_us()
        if drain_timeout_sec is None:
            await writer.drain()
        else:
            await asyncio.wait_for(writer.drain(), drain_timeout_sec)
        spent[1] += _elapsed_us(started)

    try:
        if method == "HEAD":
            return
        await send(first.encode("utf-8"))
        pending = bytearray()
        index = 0
        while True:
            started = _ticks_us()
            try:
                chunk = next(chunks)
            except StopIteration:
                break
            spent[0] += _elapsed_us(started)
            encoded = chunk.encode("utf-8")
            if len(pending) + len(encoded) > 1024 and pending:
                await send(pending)
                pending = bytearray()
            if len(encoded) >= 1024:
                await send(encoded)
            else:
                pending.extend(encoded)
            now = time.ticks_ms()
            if (index % 4 == 3 if pace_ms is None
                    else time.ticks_diff(now, last) > pace_ms):
                tally[2] = max(tally[2], time.ticks_diff(now, last))
                if report is not None and time.ticks_diff(now, sampled) >= EXPORT_HEAP_SAMPLE_MS:
                    ram_free, sampled = min(ram_free, gc.mem_free()), now
                started = _ticks_us()
                await asyncio.sleep_ms(1 if pace_ms is None else 0)
                spent[2] += _elapsed_us(started)
                tally[1] += 1
                last = time.ticks_ms()
            index += 1
        if pending:
            await send(pending)
    finally:
        close = getattr(chunks, "close", None)
        if close:
            close()
        if report is not None:
            report.update(bytes=tally[0], yields=tally[1], ram_free_min=ram_free,
                          slice_max_ms=max(tally[2], time.ticks_diff(time.ticks_ms(), last)),
                          produce_ms=spent[0] // 1000, drain_ms=spent[1] // 1000,
                          yield_ms=spent[2] // 1000)


async def _send_geojson_export(writer, chunks, method, first, store=None, label="geojson",
                               content_type="application/geo+json",
                               filename="tracking.geojson"):
    """An export stream: paced by time, each drain bounded, one log line (#42 E4, E5, E18).

    store (the tracker's point store or None) gives read_ms/decode_ms as the
    change of its profile over the export; parallel readers count as well.
    label opens the log line; the defaults are the survey's GeoJSON export,
    the stakeout export (stakeout WP4) passes its own.
    """
    profile = store.profile_us if store is not None else {}
    read_us, decode_us = profile.get("read", 0), profile.get("decode", 0)
    report, outcome, started = {}, "complete", time.ticks_ms()
    try:
        await _send_tracking_json_stream(
            writer, chunks, method, content_type, filename, first,
            pace_ms=EXPORT_PACE_MS, report=report, drain_timeout_sec=EXPORT_DRAIN_TIMEOUT_SEC)
    except BaseException as error:     # cancellation and the drain deadline included
        outcome = "interrupted " + type(error).__name__
        raise
    finally:
        if method != "HEAD":
            log("INFO", "EXPORT", "%s %s bytes=%d ms=%d yields=%d slice_max_ms=%d "
                "produce_ms=%d read_ms=%d decode_ms=%d drain_ms=%d yield_ms=%d "
                "ram_free_min=%s" % (
                    label, outcome, report.get("bytes", 0),
                    time.ticks_diff(time.ticks_ms(), started),
                    report.get("yields", 0), report.get("slice_max_ms", 0),
                    report.get("produce_ms", 0), (profile.get("read", 0) - read_us) // 1000,
                    (profile.get("decode", 0) - decode_us) // 1000, report.get("drain_ms", 0),
                    report.get("yield_ms", 0), report.get("ram_free_min")))


async def _send_tracking_backup(writer, method="GET", project_id=None, source_tracker=None):
    header = (
        "HTTP/1.0 200 OK\r\nContent-Type: application/x-ndjson\r\n"
        "Connection: close\r\nX-Content-Type-Options: nosniff\r\n"
        "Cache-Control: no-store\r\nContent-Disposition: attachment; "
        "filename=%s\r\n\r\n" % ("project-archive-schema5.ndjson"
            if project_id else "tracking-schema5.ndjson"))
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
    # The file is sent byte for byte, so its name carries the schema it was
    # archived with (older firmware wrote schema 4); only an int is trusted.
    schema = metadata.get("schema_version")
    if type(schema) is not int:
        schema = SCHEMA_VERSION
    source = None
    try:
        source = open(path, "rb")
        header = ("HTTP/1.0 200 OK\r\nContent-Type: application/x-ndjson\r\n"
                  "Connection: close\r\nX-Content-Type-Options: nosniff\r\n"
                  "Cache-Control: no-store\r\nContent-Disposition: attachment; "
                  "filename=survey-archive-schema%d.ndjson\r\n\r\n" % schema)
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


def _send_api_error(writer, status, code, message, extra=None):
    """Send the stable machine-readable API error envelope; extra adds keys to it."""
    if extra:
        payload = {"error": code, "message": message}
        payload.update(extra)
        _send_json(writer, payload, status)
    else:
        _send_json(writer, api_error(code, message), status)


class _AnswerCollector:
    """A writer that keeps what is written, to build an answer ahead of time."""

    def __init__(self):
        self.parts = []

    def write(self, data):
        self.parts.append(bytes(data))


def _prebuilt_api_error(status, code, message):
    collector = _AnswerCollector()
    _send_api_error(collector, status, code, message)
    return b"".join(collector.parts)


# Built at import: sending it under memory pressure builds nothing (#19a E14).
_DEVICE_BUSY_ANSWER = _prebuilt_api_error("503 Service Unavailable", "device_busy",
                                          "Not enough memory on the device; try again.")


def _answer_started(writer):
    """True once a dispatcher writer has begun the answer; a plain writer never says so."""
    return getattr(writer, "started", False)


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


def _auth_refusal(is_ap_request, headers, method="GET"):
    """(status_line, code, message) for a request the admin gate refused.

    The cause decides, not the method (#19a E1): 401 without a valid session
    (always for GET/HEAD), so the client signs in again; 403 only while the
    session lives, for a rejected Origin or a missing or wrong CSRF token.
    The session check does not extend the session's idle window.
    """
    if method in ("GET", "HEAD") or not session_active(headers.get("cookie", "")):
        return ("401 Unauthorized", "admin_session_required",
                "Administrator sign-in required.")
    if not _origin_allowed(headers, is_ap_request):
        return ("403 Forbidden", "origin_rejected",
                "The request origin was rejected. Reload the page.")
    return ("403 Forbidden", "csrf_rejected",
            "The security token was rejected. Reload the page.")


def _send_auth_refusal(writer, is_ap_request, headers, method="GET"):
    """Answer a refused request with the status and code of its cause."""
    status, code, message = _auth_refusal(is_ap_request, headers, method)
    _send_api_error(writer, status, code, message)


CAPTIVE_PORTAL_PATH = "/#device/configuration"


def captive_portal_url():
    """Canonical captive-portal target: the field app on the AP address."""
    return "http://%s%s" % (CONFIG["ap_ip"], CAPTIVE_PORTAL_PATH)


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
    """Field app sign-in; the code stays in the fragment, so it never reaches
    the HTTP request, logs or the Referer header."""
    ident = identity_data or app.identity or {}
    hostname = ident.get("hostname") or "rtk"
    return "http://%s.local/#code=%s" % (
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
            # Only text can name a saved network; anything else fails validation.
            if password is None and entry.get("password_set") and isinstance(ssid, str):
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
    rtcm_report = ntrip.rtcm.summary() if ntrip else None
    now_ms = time.ticks_ms()
    fix, quality = current_fix_view(now_ms)
    snapshot = {
        "schema_version": 1,
        "identity": dict((key, (app.identity or {}).get(key)) for key in
                         ("device_id", "display_name", "hostname", "ble_name")),
        "firmware": {"esp": CONFIG.get("version"),
                     "gnss_responses": list(gnss_replies[-20:])},
        "network": {"state": app.stats.get("access_state"),
                    "mode": app.stats.get("net_mode"),
                    "ssid": app.stats.get("wifi_ssid_active"),
                    "sta_ip": app.stats.get("sta_ip"),
                    "ap_ip": app.stats.get("ap_ip"),
                    "configured": {"primary_ssid": CONFIG.get("wifi_ssid"),
                                   "fallbacks": masked_wifi_networks()}},
        "field_diagnostics": field_recorder.status(),
        "config_apply": config_result_for_clients(),
        "fix": fix,
        "current_quality": quality,
        "link": link_health(now_ms, rtcm_report),
        "rtcm": rtcm_report,
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


CONFIG_BACKUP_SCHEMA = 3
# Sections a backup of each schema restores; schema 1 holds only the connections,
# and every later schema adds sections to the one before.
_BACKUP_SECTIONS = ("connections", "measurement", "field_setup", "stakeout")
CONFIG_BACKUP_SECTIONS = {1: _BACKUP_SECTIONS[:1], 2: _BACKUP_SECTIONS[:3], 3: _BACKUP_SECTIONS}
_BACKUP_SCHEMA_ERROR = "backup_schema_version must be 1, 2 or 3"


def config_backup():
    """Connections plus the device defaults for new measurements.

    measurement mirrors /api/measurement-gate (gate values and samples),
    field_setup mirrors /api/field-setup, stakeout the values of
    /api/stakeout/settings. Identity and uptime values remain outside; survey
    data is backed up by /api/tracking/export?format=backup.
    """
    keys = tuple(SETTABLE_KEYS) + ("wifi_networks",)
    return {"schema_version": CONFIG_BACKUP_SCHEMA, "kind": "esp-rtk-config-backup",
            "config": dict((key, CONFIG.get(key)) for key in keys),
            "credentials": {"ap_pass": CONFIG.get("ap_pass"),
                            "device_code": (app.identity or {}).get("device_code")},
            "measurement": {"gate": measurement_gate(),
                            "samples": CONFIG.get("measurement_samples", 5)},
            "field_setup": field_setup(), "stakeout": _stakeout_settings_values()}


def config_result_for_clients():
    """The stored config result in the statuses clients know.

    An unreadable result file reads as "unknown". The browser shows every final
    status other than "connected" as a failed change whose previous settings
    were restored, which an unreadable file does not say; clients therefore see
    it as still pending, with details.code "result_unreadable".
    Every answer carries boot_id and apply_id (None for a record written
    before #23c and for an unreadable one).
    """
    result = dict(config_result_read())
    if result.get("status") == "unknown":
        result["status"], result["details"] = "pending", {"code": "result_unreadable"}
        result["boot_id"] = result["apply_id"] = None
    result.setdefault("boot_id", None)
    result.setdefault("apply_id", None)
    return result


def _backup_measurement_values(payload):
    """The measurement and field_setup sections of a schema-2 backup: (values, error).

    Both sections must be complete; values are CONFIG keys, checked by the
    same validators as /api/measurement-gate and /api/field-setup.
    """
    measurement = payload.get("measurement")
    if not isinstance(measurement, dict):
        return None, "measurement must be an object."
    if set(measurement) != set(("gate", "samples")):
        return None, "measurement must contain gate and samples."
    values, error = validate_measurement_gate(measurement["gate"])
    if error:
        return None, "measurement: " + error
    samples, error = validate_measurement_samples(
        {"measurement_samples": measurement["samples"]})
    if error:
        return None, "measurement: " + error
    values.update(samples)
    setup = payload.get("field_setup")
    if not isinstance(setup, dict):
        return None, "field_setup must be an object."
    if set(setup) != set(FIELD_SETUP_KEYS):
        return None, ("field_setup must contain antenna_height_m, reference_frame "
                      "and reference_frame_epoch.")
    clean, error = validate_field_setup(setup)
    if error:
        return None, "field_setup: " + error
    values.update(clean)
    return values, None


def _backup_stakeout_values(payload):
    """The stakeout section of a schema-3 backup: (values, error).

    The section must be complete; values are CONFIG keys, checked by the same
    validator and pair rule as PUT /api/stakeout/settings.
    """
    section = payload.get("stakeout")
    # Two keys the validator knows are exactly these two, so the pair rule applies.
    if not isinstance(section, dict) or len(section) != 2:
        return None, "stakeout: must contain tolerance_m and fine_radius_m."
    values, error = validate_stakeout_settings(
        dict(("stakeout_" + key, value) for key, value in section.items()))
    return values, error and "stakeout: " + error


def backup_restore_plan(payload):
    """Check a whole backup before anything is written: (plan, error).

    plan = {"values", "schema_version", "sections", "kept"}; values go to
    apply_config_change, kept names what the target keeps (e.g. "wifi" for a
    backup without any Wi-Fi network). Schema 1 restores only the connections
    and ignores any other section; schema 2 ignores a stakeout section.
    """
    if not isinstance(payload, dict):
        return None, _BACKUP_SCHEMA_ERROR
    version = payload.get("schema_version")
    # type() first: a list would not hash, and True or 2.0 would compare equal.
    if type(version) is not int or version not in CONFIG_BACKUP_SECTIONS:
        return None, _BACKUP_SCHEMA_ERROR
    if payload.get("kind") != "esp-rtk-config-backup" or not isinstance(payload.get("config"), dict):
        return None, "Not a valid ESP-RTK configuration backup"
    supplied = dict(payload["config"])
    # A restore replaces the fallback-network list completely.
    supplied.setdefault("wifi_networks", [])
    values, kept, _code, error = prepare_restore_change(supplied)
    if error:
        return None, error
    # Schema 2 adds the measurement and field_setup check, schema 3 the stakeout check.
    for check in (_backup_measurement_values, _backup_stakeout_values)[:version - 1]:
        section, error = check(payload)
        if error:
            return None, error
        values.update(section)
    return {"values": values, "schema_version": version,
            "sections": list(CONFIG_BACKUP_SECTIONS[version]), "kept": list(kept)}, None


def validate_backup(payload):
    plan, error = backup_restore_plan(payload)
    return (None, error) if error else (plan["values"], None)

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
                    # Schema 4 introduced the integrity record; 5 is the current schema.
                    if (not isinstance(header, dict) or header.get("record") != "header" or
                            header.get("kind") != "survey_backup" or
                            header.get("schema_version") not in (4, 5)):
                        raise ValueError("Use an integrity-protected NDJSON survey backup.")
                    first = False
                yield raw.decode("utf-8")
            if len(pending) > 32768:
                raise ValueError("A backup record is too large.")
            if not block: break


async def _handle_tracking_import(reader, writer, initial, length, is_ap_request, headers):
    """Receive large backups on flash; publish only after complete verification."""
    if not _admin_allowed(is_ap_request, headers, "POST"):
        _send_auth_refusal(writer, is_ap_request, headers, "POST")
        return
    if headers.get("content-type", "").split(";", 1)[0] != "application/x-ndjson":
        _send_api_error(writer, "415 Unsupported Media Type", "ndjson_required",
                        "Use an NDJSON survey backup.")
        return
    if tracker.is_install_pending():
        # Only a start-up installs a published restore; nothing else clears it.
        _send_api_error(writer, "409 Conflict", "restore_install_pending",
                        "A survey restore is published but not installed. "
                        "Restart the device to complete it.")
        return
    startup = app.stats.get("tracking_startup") or {}
    if not tracker.is_loaded() and startup.get("state") == "error":
        _send_api_error(writer, "503 Service Unavailable", "survey_unavailable",
                        "Survey storage is unavailable (%s); see /status "
                        "tracking_startup." % startup.get("code", "safe mode or start-up error"))
        return
    if (not tracker.is_loaded() or tracker.maintenance_active() or stakeout.store.busy or
            tracker.has_open_recording() or measurement_session.active() or
            _instances.get("parallel_tracker") or
            app.stats.get("parallel_benchmark_state") in ("armed", "stopping")):
        _send_api_error(writer, "409 Conflict", "storage_busy",
                        "Finish the active operation before restoring survey data.")
        return
    try:
        # A restore could drop a project whose target sets wait for the next start.
        stakeout.store.refuse_blocked()
    except StakeoutError as error:
        _send_stakeout_error(writer, error)
        return
    if length <= 0:
        _send_api_error(writer, "400 Bad Request", "empty_backup", "The backup is empty.")
        return
    if not tracker.storage_capacity(length * 2 + 32768, force=True)["writable"]:
        _send_api_error(writer, "507 Insufficient Storage", "storage_reserve",
                        "Not enough space to restore while preserving existing data.")
        return
    path = tracker.path + ".upload.ndjson"
    # The checks above leave nothing for the guard to refuse; no await in between.
    async with tracker.maintenance("tracking_restore", {
            "operation": "restore", "state": "receiving", "processed": 0,
            "total": length, "error": None}) as job:
        try:
            started, received = time.ticks_ms(), 0
            with open(path, "wb") as target:
                block = initial[:length]
                while received < length:
                    if block:
                        target.write(block); received += len(block)
                        job.update(processed=received)
                        await asyncio.sleep_ms(1)
                    if received == length: break
                    if time.ticks_diff(time.ticks_ms(), started) > 180000:
                        raise ValueError("Backup upload timed out.")
                    block = await asyncio.wait_for(reader.read(min(4096, length - received)), 10)
                    if not block: raise ValueError("Backup upload is incomplete.")
            job.update(state="validating")
            result = await _stakeout_restore(
                lambda: tracker.restore_ndjson_async(_bounded_backup_lines(path)))
            job.update(state="complete", processed=result["features"],
                       total=result["features"])
            _send_json(writer, dict(result, tracking_live=tracker.live_status()))
        except StakeoutError as error:
            if _answer_started(writer): raise
            job.update(state="failed", error=error.message)
            _send_stakeout_error(writer, error)
        except (ValueError, TypeError, UnicodeError, asyncio.TimeoutError) as error:
            if _answer_started(writer): raise
            job.update(state="failed", error=str(error))
            _send_api_error(writer, "400 Bad Request", "invalid_backup", str(error))
        except (OSError, MemoryError) as error:
            if _answer_started(writer): raise
            job.update(state="failed", error=str(error))
            _send_api_error(writer, "507 Insufficient Storage", "restore_failed",
                            "Could not restore the backup.")
        finally:
            try: os.remove(path)
            except OSError: pass


async def _handle_tracking_request(writer, path, method, query, body,
                                   is_ap_request, headers):
    if not _admin_allowed(is_ap_request, headers, method, query, body):
        _send_auth_refusal(writer, is_ap_request, headers, method)
        return
    changing = method == "POST" and path == "/api/tracking"
    # Every method on these paths reads the survey, POST export included.
    reading = path in ("/api/tracking/map", "/api/tracking/line", "/api/tracking/export")
    if (changing or reading) and tracker.maintenance_active():
        _send_api_error(writer, "409 Conflict" if changing else "503 Service Unavailable",
                        "storage_busy", "Storage maintenance in progress. Please wait.")
        return
    maintenance = False
    if changing:
        try: action = ujson.loads(body.decode("utf-8")).get("action")
        except (ValueError, AttributeError): action = None
        maintenance = action in ("archive_project", "reactivate_archive", "verify_archive",
                                 "compact", "restore")
        if maintenance and stakeout.store.busy:
            # A stakeout change (an undo among them) excludes maintenance (E26).
            _send_api_error(writer, "409 Conflict", "storage_busy",
                            "A stakeout change is running. Please wait.")
            return
        if maintenance and (tracker.has_open_recording() or measurement_session.active()):
            _send_api_error(writer, "409 Conflict", "recording_active",
                            "Finish the active measurement before storage maintenance.")
            return
    # The checks above leave nothing for the guards to refuse; no await in between.
    if maintenance and tracker.is_loaded():
        async with tracker.maintenance("tracking_storage", {
                "operation": action, "state": "running", "error": None}) as job:
            await _dispatch_tracking_request(writer, path, method, query, body,
                                             is_ap_request, headers, maintenance=job)
    elif reading:
        async with tracker.reader():
            await _dispatch_tracking_request(writer, path, method, query, body,
                                             is_ap_request, headers)
    else:
        # Includes maintenance while the survey is not loaded: the dispatch
        # answers benchmark_active or survey_loading before any action runs.
        await _dispatch_tracking_request(writer, path, method, query, body,
                                         is_ap_request, headers)


# Tracking actions that change the active project or replace the survey: each
# clears the stakeout session and progress before and after it (E15).
_STAKEOUT_RESET_ACTIONS = ("create_project", "select_project", "delete_project",
                           "archive_project", "reactivate_archive", "restore")


# The frame assignment actions of POST /api/tracking (#39 E13).
FRAME_ASSIGNMENT_ACTIONS = ("frame_assignment_preview", "assign_reference_frame",
                            "revoke_reference_frame")


async def _frame_assignment_action(action, supplied):
    """Run one frame assignment action; the scans of preview and assignment hold a reader.

    A cancelled request stops before the journal append and writes nothing;
    once appended, the assignment stays even if the answer is lost (#39 E13).
    """
    project_id = supplied.get("project_id")
    if action == "revoke_reference_frame":
        return tracker.revoke_frame_assignment(project_id, supplied.get("assignment_token"))
    async with tracker.reader():
        if action == "frame_assignment_preview":
            return await tracker.frame_assignment_preview_async(project_id)
        return await tracker.assign_frame_async(
            project_id, supplied.get("reference_frame"),
            supplied.get("reference_frame_epoch", ""), supplied.get("expected_count"),
            supplied.get("scope_token"))


def _request_id(supplied):
    """The client's request_id of one tracking POST (#21c E19), checked before any measurement.

    None when absent; otherwise 1 to 32 characters of A-Z, a-z, 0-9, '_' and '-'."""
    value = supplied.get("request_id")
    if value is None:
        return None
    if (not isinstance(value, str) or not 1 <= len(value) <= 32 or
            any(not ("a" <= c <= "z" or "A" <= c <= "Z" or "0" <= c <= "9" or c in "_-")
                for c in value)):
        raise ValueError("The request id is invalid.")
    return value


async def _dispatch_tracking_request(writer, path, method, query, body,
                                   is_ap_request, headers, maintenance=None):
    """Handle the tracking API as one domain boundary.

    maintenance is the entered guard of the caller's storage maintenance, if
    any; a restore nests below it."""
    if not _admin_allowed(is_ap_request, headers, method, query, body):
        _send_auth_refusal(writer, is_ap_request, headers, method)
        return
    params = parse_query(query)
    if (path == "/api/tracking" and method == "POST" and
            (_instances.get("parallel_tracker") or
             app.stats.get("parallel_benchmark_state") in ("armed", "stopping"))):
        _send_api_error(writer, "409 Conflict", "benchmark_active",
                        "Stop the parallel benchmark before changing production projects.")
        return
    if path == "/api/tracking" and method == "POST" and tracker.is_install_pending():
        # Like the import: only a restart installs the published restore (#19a E8).
        _send_api_error(writer, "409 Conflict", "restore_install_pending",
                        "A survey restore is published but not installed. "
                        "Restart the device to complete it.")
        return
    if not tracker.is_loaded():
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
            await _send_geojson_export(writer, chunks, method, first, tracker.point_store)
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
            if _answer_started(writer): raise
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
            now_ms = time.ticks_ms()
            current_fix, current_quality = current_fix_view(now_ms)
            _send_json(writer, {"measurement_progress": measurement_session.snapshot(),
                                "storage_operation": tracker.operation_snapshot(),
                                "current_fix": current_fix,
                                "current_quality": current_quality,
                                "quality_streak": app.quality_streaks(now_ms),
                                "network": network_view()})
            return
        try:
            await tracker.prepare_status_async()
        except StorageBusy as error:
            _send_api_error(writer, "503 Service Unavailable", error.code, str(error))
            return
        result = tracker.status()
        now_ms = time.ticks_ms()
        result["current_fix"], result["current_quality"] = current_fix_view(now_ms)
        result["quality_streak"] = app.quality_streaks(now_ms)
        result["gate_deviations"] = measurement_gate_deviations()
        result["config_warnings"] = list(CONFIG_WARNINGS)
        result["measurement_progress"] = measurement_session.snapshot()
        result["storage_operation"] = tracker.operation_snapshot()
        _send_json(writer, result)
        return
    refresh = False
    reset = False
    try:
        action_started = time.ticks_ms()
        supplied = ujson.loads(body.decode("utf-8"))
        if not isinstance(supplied, dict):
            raise ValueError("The request body must be an object.")
        action = supplied.get("action")
        request_id = _request_id(supplied)
        reset = isinstance(action, str) and action in _STAKEOUT_RESET_ACTIONS
        if reset:
            _stakeout_reset()
        if action == "cancel_measurement":
            result = measurement_session.cancel()
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
                   await measurement_session.collect(supplied.get("samples", 1)))
            line = tracker.start_line(supplied.get("name"),
                supplied.get("auto_distance_m", 1), supplied.get("properties"), fix,
                recording_mode)
            if request_id: tracker.tag_request(request_id, "vertex", line["id"])
            # start_line() returns the canonical in-memory object. During the
            # durable append its point arrays are replaced with FlashSequence
            # views, which must never be handed to ujson. Return a detached,
            # deliberately small transport representation instead.
            result = {"id": line["id"], "name": line["name"],
                      "state": line["state"],
                      "recording_mode": line.get("recording_mode", "survey")}
        elif action == "add_vertex":
            fix = await measurement_session.collect(supplied.get("samples", 1))
            # The generation of exactly this collect (no await in between): a
            # cancel or a new measurement during the summary rebuild voids the fix.
            generation = measurement_session.generation()
            await tracker.prepare_append_async()
            measurement_session.check_generation(generation)
            result = tracker.add_vertex(fix)
            if request_id: tracker.tag_request(request_id, "vertex")
        elif action == "add_asset":
            result = tracker.add_asset(supplied.get("object_type", "other"),
                supplied.get("name", ""), supplied.get("note", ""),
                await measurement_session.collect(supplied.get("samples", 1)),
                supplied.get("link_to_active_line", True))
            if request_id: tracker.tag_request(request_id, "asset", result["id"])
        elif action == "undo":
            # Only a missing expect is the pre-#21c client; an explicit null is checked.
            await _stakeout_undo(supplied.get("expect", UNDO_UNCHECKED))
            result = {}
        elif action == "rename_project":
            tracker.rename_project(supplied.get("project_id"), supplied.get("name"))
            result = {}
        elif action == "delete_project":
            result, refresh = await _delete_project(supplied.get("project_id"))
            if refresh is None:
                _send_json(writer, result, "507 Insufficient Storage")
                return
        elif action == "archive_project":
            # Archiving a blocked project would let the next start delete its sets.
            project_id = supplied.get("project_id")
            stakeout.store.refuse_blocked(project_id)
            try:
                # The archive notes whether the project's stakeout folder held data (E23).
                extra = {"stakeout_data": stakeout.store.has_data(project_id)}
            except OSError as error:
                log("WARN", "STAKE", "Stakeout data of %s unknown (%s)." % (project_id, error))
                extra = None
            result = await tracker.archive_project_async(project_id, extra)
        elif action == "verify_archive":
            result = await tracker.verify_archive_async(supplied.get("archive_id"))
        elif action == "reactivate_archive":
            stakeout.store.refuse_blocked()
            result = await tracker.reactivate_archive_async(supplied.get("archive_id"))
        elif action == "compact":
            await tracker.compact_async()
            await tracker.prepare_status_async(owner=maintenance)
            result = tracker.status()
        elif action == "restore":
            stakeout.store.refuse_blocked()
            log("INFO", "TRACK", "Survey restore started.")
            async with tracker.maintenance("tracking_restore", parent=maintenance):
                restored = await _stakeout_restore(
                    lambda: tracker.restore_async(supplied.get("backup")))
            log("INFO", "TRACK", "Survey restore completed (%s features)." %
                tracker.feature_count())
            await tracker.prepare_status_async(owner=maintenance)
            result = dict(tracker.status(),
                          legacy_values_skipped=restored["legacy_values_skipped"])
        elif action in FRAME_ASSIGNMENT_ACTIONS:
            result = await _frame_assignment_action(action, supplied)
        elif action in ("pause", "resume", "finish"):
            diagnostics = None
            if action == "finish":
                diagnostics = dict((key, app.stats.get(key)) for key in (
                    "queue_overflows", "ble_drops", "nmea_tcp_drops", "crc_errors",
                    "gga_parse_errors", "task_restarts", "ntrip_retries", "ntrip_bytes"))
            tracker.set_line_state(action, diagnostics); result = {}
        else:
            raise ValueError("Unknown tracking action.")
    except StakeoutError as error:
        if _answer_started(writer): raise
        _send_stakeout_error(writer, error)
        return
    except FrameAssignmentError as error:
        # Refused by the survey's state (archive exports included), not a bad request.
        if _answer_started(writer): raise
        _send_api_error(writer, "409 Conflict", error.code, str(error))
        return
    except (ValueError, TypeError) as error:
        if _answer_started(writer): raise
        if 'action' in locals() and action == "restore":
            log("ERROR", "TRACK", "Survey restore rejected: %s" % error)
        elif ('action' in locals() and action in ("start_line", "add_vertex", "add_asset")
              and str(error) != "Measurement cancelled."):
            log("WARN", "TRACK", "Measurement rejected (%s): %s" % (action, error))
        if isinstance(error, StorageReserveError):
            _send_api_error(writer, "507 Insufficient Storage", "storage_reserve", str(error))
        elif isinstance(error, UndoCandidateChanged):
            _send_api_error(writer, "409 Conflict", "undo_candidate_changed", str(error))
        elif isinstance(error, MeasurementRejected):
            _send_api_error(writer, "400 Bad Request", "invalid_tracking_request", str(error),
                            {"reasons": list(error.reasons)})
        else:
            _send_api_error(writer, "400 Bad Request", "invalid_tracking_request", str(error))
        return
    except OSError as error:
        if _answer_started(writer): raise
        log("ERROR", "TRACK", "Could not persist tracking data: %s" % error)
        _send_api_error(writer, "507 Insufficient Storage", "tracking_storage_failed",
                        "Could not store tracking data.")
        return
    finally:
        if reset:
            _stakeout_reset()
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
    if request_id is not None: result["request_id"] = request_id
    result["tracking_live"] = tracker.live_status()
    result["tracking_performance"] = {
        "action": action, "action_ms": action_ms,
        "append_ms": getattr(tracker, "last_append_ms", 0)}
    # Keep transport failures separate from journal failures. A browser closing
    # the response after a successful mutation must not be reported as lost data.
    try:
        _send_json(writer, result)
    finally:
        if refresh:
            await _refresh_flash_sample()


def _project_live(project_id):
    for project in tracker.projects:
        if project["id"] == project_id:
            return True
    return False


async def _delete_project(project_id):
    """delete_project with the project's stakeout sets (C6).

    Returns (answer, refresh): refresh is True when set files were removed
    (the flash is measured again after the answer) and None when the
    outcome is not known; the answer is then the 507 error envelope. The
    sets' folder is set aside as "<project_id>.deleting" (the store's
    begin_project_delete) and the deletion journaled with no yield in
    between. An append that raises may still have been applied, so the
    outcome is read from the survey in RAM: a live project gets its folder
    back and the error as before; a gone one keeps the folder for the next
    start (507 tracking_storage_failed, "stakeout_cleanup": "pending").
    Once journaled, the deletion is never reported as failed; the project's
    progress is forgotten, the folder (progress.jsonl with it) is removed
    file by file and "stakeout_cleanup" says "done" or "pending".
    """
    tracker.check_deletable(project_id)
    store = stakeout.store
    moved = store.begin_project_delete(project_id)
    try:
        tracker.delete_project(project_id)
    except Exception as error:
        if _project_live(project_id):
            if moved:
                store.undo_project_delete(project_id)
            raise
        if not moved:
            raise
        log("ERROR", "TRACK", "Project deletion may be stored (%s); its target sets "
            "are kept for the next start." % error)
        return ujson.dumps({"error": "tracking_storage_failed",
                            "message": "Could not store tracking data.",
                            "stakeout_cleanup": "pending"}), None
    stakeout_progress.progress.forget(project_id)
    return {"stakeout_cleanup": await store.finish_project_delete(project_id)}, moved


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
            _send_auth_refusal(writer, is_ap_request, headers, method)
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
        _send_auth_refusal(writer, is_ap_request, headers, method)
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


def recording_active():
    """A recording line (not paused) or a running measurement; blocking radio work waits."""
    line = tracker.active_line() or {}
    return line.get("state") == "recording" or measurement_session.active()


async def _handle_diagnostics_request(writer, path, query, is_ap_request, headers):
    """Handle diagnostics, Wi-Fi scan, config result, and backup reads."""
    if not _admin_allowed(is_ap_request, headers, "GET", query):
        if path == "/diagnostics":
            _http_send(writer, "401 Unauthorized", "text/html; charset=utf-8",
                       render_login(path), {"Cache-Control": "no-store"})
        else:
            _send_auth_refusal(writer, is_ap_request, headers)
        return
    if path == "/diagnostics":
        _http_send(writer, "200 OK", "text/html; charset=utf-8", DIAGNOSTICS_HTML,
                   {"Cache-Control": "no-store"})
    elif path == "/api/wifi-scan":
        manager = _instances.get("network")
        if recording_active():
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
        _send_json(writer, config_result_for_clients())
    elif path == "/api/config/backup":
        _send_json(writer, config_backup(), extra_headers={
            "Content-Disposition": "attachment; filename=esp-rtk-config-backup.json"})
    else:
        response_headers = {}
        if parse_query(query).get("download") == "1":
            response_headers["Content-Disposition"] = "attachment; filename=esp-rtk-diagnostics.json"
        try:
            await tracker.prepare_status_async()
        except StorageBusy as error:
            _send_api_error(writer, "503 Service Unavailable", error.code, str(error))
            return
        _send_json(writer, diagnostics_snapshot(), extra_headers=response_headers)


def _config_verification_running():
    """The network manager is connecting right now."""
    # While NetworkManager is connecting, its verdict (commit or rollback) would
    # act on a change armed now.
    return app.stats.get("access_state") == "CONNECTING"


def _open_change_pending():
    """A change is armed, being verified, or its rollback is not complete."""
    # pending_config_active() runs the file recovery once; without a pending
    # file only the RAM view of rollback_open() is left.
    return pending_config_active() or rollback_open(files=False)


def _config_refusal():
    """(code, message) why no change may be armed now, or None."""
    if _config_verification_running():
        return ("config_verification_running",
                "The device is connecting to Wi-Fi. Try again in a minute.")
    if _open_change_pending():
        return ("open_change_pending",
                "A configuration change is still being verified or rolled back. "
                "Try again after the restart.")
    return None


_APPLY_ID_CHARS = "0123456789abcdef"


def _request_apply_id(value):
    """(apply_id, ok) of a client's request identity; None (an older client) is accepted.

    Valid are 8 to 32 lowercase hex characters, checked without re.
    """
    if value is None:
        return None, True
    if not isinstance(value, str) or not 8 <= len(value) <= 32:
        return None, False
    for char in value:
        if char not in _APPLY_ID_CHARS:
            return None, False
    return value, True


def _checked_apply_id(writer, value):
    """_request_apply_id(value); an invalid value is answered with 400 invalid_request."""
    apply_id, ok = _request_apply_id(value)
    if not ok:
        _send_api_error(writer, "400 Bad Request", "invalid_request",
                        "apply_id must be 8 to 32 lowercase hex characters.")
    return apply_id, ok


def _request_object(writer, body):
    """The JSON object of an optional request body ({} when empty); None after a 400."""
    if not body:
        return {}
    try:
        supplied = ujson.loads(body.decode("utf-8"))
    except (ValueError, TypeError):
        supplied = None
    if not isinstance(supplied, dict):
        _send_api_error(writer, "400 Bad Request", "invalid_json",
                        "The request body is not valid JSON.")
        return None
    return supplied


def _staging_current(writer, fields):
    """False after a 409 when `fields` names a staging revision other than the current one.

    A client that sends the revision it saw never applies a staging it has not
    seen; a missing field is an older client and is not checked.
    """
    if "staging_revision" in fields and fields["staging_revision"] != staging_revision():
        _send_api_error(writer, "409 Conflict", "staging_changed",
                        "The changes waiting in the device memory changed. Check them again.")
        return False
    return True


def _handle_config_apply(writer, body):
    refusal = _config_refusal()
    if refusal:
        _send_api_error(writer, "409 Conflict", refusal[0], refusal[1])
        return
    supplied = _request_object(writer, body)
    if supplied is None:
        return
    apply_id, ok = _checked_apply_id(writer, supplied.get("apply_id"))
    if not ok or not _staging_current(writer, supplied):
        return
    if not config_restart_required():
        _send_api_error(writer, "409 Conflict", "nothing_to_apply",
                        "There is no staged configuration change to apply.")
    elif not apply_staged_config(apply_id):
        _send_api_error(writer, "500 Internal Server Error", "stage_failed",
                        "Could not stage the configuration.")
    else:
        _send_json(writer, {"applying": True, "apply_id": apply_id}, "202 Accepted")
        app.set_access_state("APPLYING")
        asyncio.create_task(do_soft_reboot_deferred())


def _handle_config_restore(writer, body, query):
    refusal = _config_refusal()
    if refusal:
        # Before the validation: a refused restore touches nothing.
        _send_api_error(writer, "409 Conflict", refusal[0], refusal[1])
        return
    apply_id, ok = _checked_apply_id(writer, parse_query(query).get("apply_id"))
    if not ok:
        return
    try:
        payload = ujson.loads(body.decode("utf-8"))
    except (ValueError, TypeError):
        payload = None
    plan, error = backup_restore_plan(payload)
    if error:
        _send_api_error(writer, "400 Bad Request", "invalid_backup",
                        "The backup is invalid: %s" % error)
        return
    applied = apply_config_change(plan["values"], apply_id)
    if "measurement_required_fix" in plan["values"]:
        # The live part is saved before arming, even when arming fails: judge the
        # current fix by the restored limits now, not after the restart.
        app.reevaluate_gate()
    if not applied:
        _send_api_error(writer, "500 Internal Server Error", "stage_failed",
                        "Could not stage the configuration.")
    else:
        _send_json(writer, {"restored": True, "schema_version": plan["schema_version"],
                            "sections": plan["sections"], "kept": plan["kept"],
                            "apply_id": apply_id},
                   "202 Accepted")
        asyncio.create_task(do_soft_reboot_deferred())


def _handle_config_form(writer, body):
    refusal = _config_refusal()
    if refusal:
        _http_send(writer, "409 Conflict", "text/html; charset=utf-8",
                   '<html lang="en"><body><h2>Busy</h2><p>%s</p>'
                   '<p><a href="/">Back</a></p></body></html>' % html_escape(refusal[1]))
        return
    fields = parse_post_data(body)
    networks_text = fields.pop("wifi_networks", None)
    if networks_text is not None:
        fields["wifi_networks"] = parse_configured_networks(
            networks_text, CONFIG.get("wifi_networks"))
    values, _code, error = prepare_config_change(fields)
    if error:
        log("ERROR", "HTTP", "Configuration rejected: %s" % error)
        _http_send(writer, "400 Bad Request", "text/html; charset=utf-8",
                   '<html lang="en"><body><h2>Rejected</h2><p>%s</p>'
                   '<p><a href="/">Back</a></p></body></html>' % html_escape(error))
        return
    if apply_config_change(values):
        _http_send(writer, "200 OK", "text/html; charset=utf-8",
                   '<html lang="en"><body><h2>Saved!</h2>'
                   '<p>Restarting in 5 seconds...</p></body></html>')
        asyncio.create_task(do_soft_reboot_deferred())
    else:
        _http_send(writer, "500 Internal Server Error", "text/html; charset=utf-8",
                   '<html lang="en"><body><h2>Save failed!</h2></body></html>')


def _handle_config_put(writer, body, query):
    try:
        supplied = ujson.loads(body.decode("utf-8"))
    except (ValueError, TypeError):
        supplied = None
    if not isinstance(supplied, dict):
        _send_api_error(writer, "400 Bad Request", "invalid_json",
                        "The request body is not valid JSON.")
        return
    if not _staging_current(writer, parse_query(query)):
        return
    if supplied.get("wifi_networks") is None:
        supplied.pop("wifi_networks", None)
    else:
        # Hidden passwords resolve against the staged list, if one is staged.
        current = staged_config().get("wifi_networks", CONFIG.get("wifi_networks"))
        supplied["wifi_networks"] = merge_masked_wifi_networks(
            supplied["wifi_networks"], current)
    values, code, error = prepare_config_change(supplied)
    if error:
        if code == "invalid_wifi_networks":
            _send_api_error(writer, "400 Bad Request", code, error)
        else:
            _send_api_error(writer, "400 Bad Request", code,
                            "The configuration is invalid: %s" % error)
        return
    summary = stage_config(values)
    if summary is None:
        _send_api_error(writer, "500 Internal Server Error", "stage_failed",
                        "Could not stage the configuration.")
    else:
        _send_json(writer, summary,
                   "202 Accepted" if summary["restart_required"] else "200 OK")


async def _handle_config_request(writer, path, method, query, body,
                                 is_ap_request, headers):
    """Handle JSON config read/stage/restore/apply and the legacy save form."""
    if not _admin_allowed(is_ap_request, headers, method, query, body):
        if path == "/save":
            status = _auth_refusal(is_ap_request, headers, method)[0]
            _http_send(writer, status, "text/plain",
                       "Configuration access expired or invalid. Open /setup and sign in again.")
        else:
            _send_auth_refusal(writer, is_ap_request, headers, method)
        return
    if path == "/api/config/apply":
        _handle_config_apply(writer, body)
    elif path == "/api/config/restore":
        _handle_config_restore(writer, body, query)
    elif path == "/save":
        _handle_config_form(writer, body)
    elif method == "GET":
        result = dict((key, CONFIG.get(key)) for key in
                      ("wifi_ssid", "ntrip_enabled", "ntrip_host", "ntrip_port",
                       "ntrip_mount", "ntrip_user", "ntrip_fallback_host",
                       "ntrip_fallback_port", "ntrip_fallback_mount",
                       "ntrip_fallback_user"))
        result["wifi_networks"] = masked_wifi_networks()
        result["wifi_pass_set"] = bool(CONFIG.get("wifi_pass"))
        result["ntrip_pass_set"] = bool(CONFIG.get("ntrip_pass"))
        result["ntrip_fallback_pass_set"] = bool(CONFIG.get("ntrip_fallback_pass"))
        result["staging"] = {"revision": staging_revision(), "keys": sorted(staged_config()),
                             "restart_required": config_restart_required()}
        _send_json(writer, result)
    else:
        _handle_config_put(writer, body, query)


async def _handle_parallel_benchmark_export_request(writer, method, body,
                                                    is_ap_request, headers):
    if not _admin_allowed(is_ap_request, headers, method, "", body):
        _send_auth_refusal(writer, is_ap_request, headers, method)
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
            await source.load_without_recovery_async()
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
        _send_auth_refusal(writer, is_ap_request, headers, method)
        return
    from parallel_benchmark import control, control_status
    if method == "GET":
        _send_json(writer, control_status())
        return
    if not tracker.is_loaded():
        _send_api_error(writer, "503 Service Unavailable", "survey_loading",
                        "Wait for survey storage before starting a benchmark.")
        return
    try:
        values = ujson.loads(body.decode("utf-8"))
        if not isinstance(values, dict):
            raise ValueError("A JSON object is required.")
        if measurement_session.active():
            raise ValueError("Wait for the current measurement to finish.")
        action = "cleanup" if method == "DELETE" else values.get("action")
        result = control(action, tracker, values.get("target_points"), values.get("confirm"),
                         profile=values.get("profile", "parallel"))
    except (ValueError, TypeError) as error:
        if isinstance(error, StorageReserveError):
            _send_api_error(writer, "507 Insufficient Storage", "storage_reserve", str(error))
        else:
            _send_api_error(writer, "409 Conflict", "benchmark_refused", str(error))
        return
    except OSError:
        _send_api_error(writer, "500 Internal Server Error", "benchmark_storage_error",
                        "Benchmark control could not be saved.")
        return
    _send_json(writer, result)
    if result.get("reboot_required"):
        asyncio.create_task(do_soft_reboot_deferred())


_NO_SAMPLES = object()


def _measurement_gate_change(supplied):
    """Split a measurement-gate PUT body into (clean, error).

    The eight gate fields go to validate_measurement_gate unchanged (all or
    none); the optional "samples" is the device-wide measurement method.
    clean holds CONFIG keys of either or both parts.
    """
    if not isinstance(supplied, dict):
        return validate_measurement_gate(supplied)
    supplied = dict(supplied)
    samples = supplied.pop("samples", _NO_SAMPLES)
    clean = {}
    if supplied or samples is _NO_SAMPLES:
        gate, error = validate_measurement_gate(supplied)
        if error:
            return None, error
        clean.update(gate)
    if samples is not _NO_SAMPLES:
        method, error = validate_measurement_samples({"measurement_samples": samples})
        if error:
            return None, error
        clean.update(method)
    return clean, None


def _measurement_gate_body(**extra):
    """The /api/measurement-gate answer: gate, its defaults and deviations, method."""
    body = {"values": measurement_gate(),
            "defaults": measurement_gate(True),
            "deviations": measurement_gate_deviations(),
            "samples": CONFIG["measurement_samples"],
            "samples_default": DEFAULT_CONFIG["measurement_samples"]}
    body.update(extra)
    return body


async def _handle_measurement_gate_request(writer, method, body,
                                           is_ap_request, headers):
    """Read, update, or reset the field acceptance limits and measurement method.

    DELETE resets only the limits; the method keeps its value.
    """
    if not _admin_allowed(is_ap_request, headers, method, "", body):
        _send_auth_refusal(writer, is_ap_request, headers, method)
        return
    if method == "GET":
        _send_json(writer, _measurement_gate_body())
        return
    if method == "DELETE":
        defaults = measurement_gate(True)
        clean, _error = validate_measurement_gate(defaults)
    else:
        try:
            supplied = ujson.loads(body.decode("utf-8"))
        except (ValueError, TypeError):
            supplied = None
        clean, error = _measurement_gate_change(supplied)
        if error:
            _send_api_error(writer, "400 Bad Request", "invalid_measurement_gate",
                            error)
            return
    if _refuse_unreadable_config(writer):
        return
    if not apply_live(clean):
        _send_api_error(writer, "500 Internal Server Error", "save_failed",
                        "Measurement settings could not be saved.")
        return
    # The cached verdict and its streak follow new limits at once; a method
    # change alone leaves both untouched.
    if any(key in clean for key in MEASUREMENT_GATE_KEYS):
        app.reevaluate_gate()
    _send_json(writer, _measurement_gate_body(reset=method == "DELETE"))


def _refuse_unreadable_config(writer):
    """Answer 503 for a live PUT while an unreadable config.json is protected.

    Saving one key would replace the whole file (Wi-Fi and caster access
    included) with the defaults in RAM; PUT /api/config, /save or a restore
    write the whole configuration deliberately.
    """
    if not config_file_protected():
        return False
    _send_api_error(writer, "503 Service Unavailable", "config_file_unreadable",
                    "config.json could not be read at start-up; save the whole "
                    "configuration first.")
    return True


async def _handle_field_setup_request(writer, method, body,
                                      is_ap_request, headers):
    """Read or update antenna height and reference-frame labels."""
    if not _admin_allowed(is_ap_request, headers, method, "", body):
        _send_auth_refusal(writer, is_ap_request, headers, method)
        return
    if method == "PUT":
        try:
            supplied = ujson.loads(body.decode("utf-8"))
        except (ValueError, TypeError):
            supplied = None
        clean, error = validate_field_setup(supplied)
        if error:
            _send_api_error(writer, "400 Bad Request", "invalid_field_setup",
                            error)
            return
        if _refuse_unreadable_config(writer):
            return
        if not apply_live(clean):
            _send_api_error(writer, "500 Internal Server Error", "save_failed",
                            "Field setup could not be saved.")
            return
    _send_json(writer, {"values": field_setup(), "defaults": field_setup(True),
                        "suggestion": frame_suggestion_view()})


async def _handle_tcp_auth_request(writer, method, body,
                                   is_ap_request, headers):
    """Read or change whether TCP clients must authenticate before NMEA."""
    if not _admin_allowed(is_ap_request, headers, method, "", body):
        _send_auth_refusal(writer, is_ap_request, headers, method)
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
    if _refuse_unreadable_config(writer):
        return
    if not apply_live({"nmea_tcp_auth_required": supplied["required"]}):
        _send_api_error(writer, "500 Internal Server Error", "save_failed",
                        "TCP authentication setting could not be saved.")
        return
    _send_json(writer, {"required": bool(CONFIG["nmea_tcp_auth_required"]),
                        "default": True})


async def _handle_maintenance_request(writer, path, method, query, body,
                                      is_ap_request, headers):
    """Handle BLE maintenance mode and NMEA stream-token rotation."""
    if not _admin_allowed(is_ap_request, headers, method, query, body):
        _send_auth_refusal(writer, is_ap_request, headers, method)
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
        supplied = _request_object(writer, body)
        if supplied is None:
            return
        apply_id, ok = _checked_apply_id(writer, supplied.get("apply_id"))
        if ok:
            _send_json(writer, {"restarting": True, "apply_id": apply_id}, "202 Accepted")
            asyncio.create_task(do_soft_reboot_deferred())
        return
    token = rotate_stream_token()
    if token is None:
        _send_api_error(writer, "500 Internal Server Error", "rotation_failed",
                        "Could not rotate the stream token.")
        return
    app.identity["stream_token"] = token
    await disconnect_all_clients()
    _send_json(writer, {"stream_token": token})


async def _handle_ui_live_request(writer, query, is_ap_request, headers):
    """Serve the live snapshot used by the V2 field loop."""
    if not _read_allowed(is_ap_request, headers, query):
        _send_auth_refusal(writer, is_ap_request, headers)
        return
    _send_json(writer, ui_live_snapshot())


async def _serve_event_stream(writer, method, headers, is_ap_request):
    """Answer GET/HEAD /api/events; never raises (#27 E6, E9).

    True when an admitted stream left the request slots: the dispatcher
    must not count this connection down again. A reservation is returned
    in every path (release() is idempotent; serve() releases as well).
    """
    global _http_connections
    sub = None
    left = False
    try:
        if not _read_allowed(is_ap_request, headers):
            _send_auth_refusal(writer, is_ap_request, headers, method)
        elif events.MAX_STREAMS <= 0:
            _send_api_error(writer, "503 Service Unavailable", "events_disabled",
                            "The event stream is switched off.")
        elif method == "HEAD":
            writer.write(events.HEADER)
        else:
            cookie = headers.get("cookie", "")
            sub = events.hub.reserve(lambda: authenticate(cookie) is not None)
            if sub is None:
                _send_api_error(writer, "503 Service Unavailable", "events_busy",
                                "Too many event streams are open; try again shortly.")
        if sub is None:
            await events.flush(writer)
        else:
            _http_connections -= 1
            left = True
            await events.hub.serve(writer, sub)
    except Exception as error:
        events.note_failure(error)
    finally:
        if sub is not None:
            events.hub.release(sub)
    return left


async def _handle_status_request(writer, path, query, is_ap_request, headers):
    """Handle live status, RTCM summaries, and persistent error reports."""
    if path == "/api/log":
        if not _admin_allowed(is_ap_request, headers, "GET", query):
            _send_auth_refusal(writer, is_ap_request, headers)
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
                       "Administrator sign-in required.")
        else:
            _send_auth_refusal(writer, is_ap_request, headers)
        return
    ntrip = _instances.get("ntrip")
    # Raw CRS announcements (#38b): top level in /rtcm, under "rtcm" in /status.
    crs = (ntrip.rtcm.crs_summary() if ntrip else
           {"service_crs": None, "rtcm_crs": None,
            "crs_frames": {"crc_failures": 0, "rejected": 0}})
    if path == "/rtcm":
        report = ntrip.rtcm.summary() if ntrip else {}
        report["mount"] = CONFIG["ntrip_mount"]
        report["host"] = CONFIG["ntrip_host"]
        report.update(crs)
        _send_json(writer, report)
        return
    wlan_if = network.WLAN(network.AP_IF if is_ap_request else network.STA_IF)
    ip = (wlan_if.ifconfig()[0] if
          (wlan_if.active() if is_ap_request else wlan_if.isconnected()) else "N/A")
    ble = _instances.get("ble")
    rtcm_report = ntrip.rtcm.summary() if ntrip else {}
    rtcm_report = dict(rtcm_report or {})
    rtcm_report["mount"] = CONFIG.get("ntrip_mount")
    rtcm_report.update(crs)
    now_ms = time.ticks_ms()
    fix, _ = current_fix_view(now_ms)
    fix_age = (None if fix["fix_age_ms"] is None
               else round(fix["fix_age_ms"] / 1000, 1))
    _send_json(writer, {
        "fix": fix,
        "fix_age_sec": fix_age, "stats": app.stats, "recent_errors": app.errors,
        "field_diagnostics": field_recorder.status(),
        "log_entries": error_log_entries(),
        "quality_streak": app.quality_streaks(now_ms),
        "gate_deviations": measurement_gate_deviations(),
        "config_warnings": list(CONFIG_WARNINGS),
        "ble": {"clients": len(ble.connections) if ble else 0,
                "encrypted_clients": len(ble.encrypted_connections) if ble else 0,
                "payload_bytes": ble.payload_size if ble else None,
                "drops": app.stats["ble_drops"]},
        "nmea_tcp": {"clients": len(fanout_clients),
                     "port": CONFIG.get("nmea_tcp_port", 0),
                     "ports": nmea_tcp_ports(), "drops": app.stats["nmea_tcp_drops"]},
        "rtcm": rtcm_report,
        "link": link_health(now_ms, rtcm_report),
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
        _send_auth_refusal(writer, is_ap_request, headers, method)
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
    # Waits for an RTCM frame boundary; a busy receiver input answers 503.
    ok, error = await gnss_obj.tx.command(sentence)
    if ok:
        _http_send(writer, "200 OK", "text/plain",
                   "sent - retrieve the response with GET /gnss")
    elif error in (COMMAND_BUSY, COMMAND_QUEUE_FULL):
        _http_send(writer, "503 Service Unavailable", "text/plain", error)
    else:
        _http_send(writer, "400 Bad Request", "text/plain", error)


# --- stakeout (WP1: target sets and import) ---

def _stakeout_project_check(project_id, reading=False):
    """The fingerprint of project_id for a stakeout request.

    Raises StakeoutError: 503 survey_loading; 503 stakeout_recovery_pending
    for a blocked project (stakeout.TargetStore.refuse_blocked); for a
    change (reading False) 409 benchmark_active while a parallel benchmark
    runs (the condition of the tracking change and restore guards), 409
    storage_busy during tracker maintenance and 409 recording_active while
    a line records or a measurement runs; 404 project_not_found. Reading is allowed in those
    states (listings never rename or remove). The fingerprint is the
    project's created_at; projects from before created_at give None, so
    their sets are shared by every such project with the same ID.
    """
    if not tracker.is_loaded():
        raise StakeoutError("survey_loading", "The survey is still loading; try again.",
                            "503 Service Unavailable")
    stakeout.store.refuse_blocked(project_id, "503 Service Unavailable")
    if not reading and (_instances.get("parallel_tracker") or
                        app.stats.get("parallel_benchmark_state") in ("armed", "stopping")):
        raise StakeoutError("benchmark_active", "Stop the parallel benchmark before "
                            "changing target sets.", "409 Conflict")
    if not reading:
        _stakeout_refuse_maintenance()
    if not reading and recording_active():
        raise StakeoutError("recording_active", "Pause the line or finish the "
                            "measurement before changing target sets.", "409 Conflict")
    for project in tracker.projects:
        if project["id"] == project_id:
            return project.get("created_at")
    raise StakeoutError("project_not_found", "Project not found.", "404 Not Found")


def _stakeout_refuse_maintenance():
    """409 storage_busy during tracker maintenance."""
    if tracker.maintenance_active():
        raise StakeoutError("storage_busy", "Storage maintenance in progress. Please "
                            "wait.", "409 Conflict")


def _send_stakeout_error(writer, error):
    _send_json(writer, error.payload(), error.status)


class _StakeoutAnswer:
    """The request writer, noting whether the answer has begun.

    started is True from the first write attempt on: after it, a failure
    must not append a second status line; the dispatcher closes instead.
    """

    def __init__(self, writer):
        self.writer = writer
        self.started = False

    def write(self, data):
        self.started = True
        self.writer.write(data)

    async def drain(self):
        await self.writer.drain()


async def _handle_stakeout_request(writer, path, method, query, body, is_ap_request,
                                   headers):
    """The stakeout API: target set overview, set file, import, deletion,
    target selection, confirm/skip/reopen, settings, the live packet, the
    progress list and the export.

    No stakeout exception reaches the dispatcher: before the answer has
    begun, StakeoutError gets its envelope, a MemoryError 503 stakeout_busy
    and an OSError (a store I/O error) 507 stakeout_read_failed. Once the
    answer has begun, a failure (the connection, as a rule) is only logged
    and the dispatcher closes the connection. Only a failure to send the
    error answer itself propagates.
    """
    # Every write below goes through answer, which notes that it began.
    writer = answer = _StakeoutAnswer(writer)
    try:
        # A POST checks CSRF from the header only, so the body (up to 128 KiB)
        # never goes through the form parser.
        if not _admin_allowed(is_ap_request, headers, method):
            _send_auth_refusal(writer, is_ap_request, headers, method)
        elif path == "/api/stakeout/import":
            await _stakeout_import(writer, body, headers)
        elif path == "/api/stakeout/settings":
            await _stakeout_settings(writer, method, body)
        elif path == "/api/stakeout/live":
            _send_json(writer, stakeout_live_snapshot())
        elif path == "/api/stakeout/progress":
            await _stakeout_progress_list(writer, method, query)
        elif path == "/api/stakeout/export":
            await _stakeout_export(writer, query, method)
        elif path == "/api/stakeout" and method == "GET":
            await _stakeout_overview_or_set(writer, query)
        elif path == "/api/stakeout":
            await _stakeout_action(writer, body)
        else:
            _send_api_error(writer, "404 Not Found", "not_found", "No such stakeout "
                            "endpoint.")
    except (StakeoutError, MemoryError, OSError, asyncio.TimeoutError) as error:
        # A TimeoutError (a download whose client stopped taking data) comes
        # only after the head, so it is never answered.
        if isinstance(error, MemoryError):
            gc.collect()
        if answer.started:
            log("DEBUG", "HTTP", "Stakeout answer broken off: %s" % error)
            return
        if isinstance(error, StakeoutError):
            _send_stakeout_error(writer, error)
        elif isinstance(error, MemoryError):
            _send_api_error(writer, "503 Service Unavailable", "stakeout_busy",
                            "Not enough memory for the target sets; try again.")
        else:
            _send_api_error(writer, "507 Insufficient Storage", "stakeout_read_failed",
                            "The target sets could not be read (%s)." % error)
    await writer.drain()


async def _refresh_flash_sample():
    """Measure the free flash again after a set file was removed.

    The tracker caches its flash sample (resuming a line checks the cached
    one); without this a deleted set would not count as free space. Yields
    first, so the statvfs (0.72 to 0.95 s measured on the device) is a block
    of its own.
    """
    await asyncio.sleep_ms(0)
    tracker.storage_capacity(force=True)


async def _stakeout_import(writer, body, headers):
    """Import the request body; writer is the handler's _StakeoutAnswer.

    The reserve check is the import's one forced flash sample; the
    committed file is charged against it (no second statvfs).
    """
    if headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
        raise StakeoutError("json_required", "Send the import package as "
                            "application/json.", "415 Unsupported Media Type")
    async def prepare(project_id):
        # The progress first: its removal markers hide remnants, which go now.
        await _stakeout_load(project_id, _stakeout_project_check(project_id))
        await stakeout.store.clear_removed(project_id)

    answer = await stakeout.store.import_package(
        body, _stakeout_project_check,
        lambda size: tracker.storage_capacity(size, force=True)["writable"],
        tracker.charge_storage, tracker.invalidate_storage_sample, prepare)
    project_id, set_id = answer["project_id"], answer["set_id"]
    # The set is committed: from here on a failure is never a refusal.
    try:
        _send_json(writer, answer)
    except (MemoryError, OSError):
        gc.collect()
        stakeout.store.invalidate(project_id)
        if writer.started:
            raise
        raise StakeoutError("import_committed_response_failed", "The target set was "
                            "stored, but the answer could not be completed.",
                            "500 Internal Server Error", set_id=set_id)


def _stakeout_set_head(size):
    """Status line and headers of a set file of size bytes."""
    return ("HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n"
            "Connection: close\r\nX-Content-Type-Options: nosniff\r\n"
            "Referrer-Policy: no-referrer\r\nX-Frame-Options: DENY\r\n"
            "Content-Security-Policy: %s\r\nCache-Control: no-store\r\n"
            "Content-Length: %d\r\n\r\n"
            % (CONTENT_SECURITY_POLICY, size)).encode("ascii")


def _stakeout_reading_check(project_id):
    return _stakeout_project_check(project_id, reading=True)


def _line_recording():
    """True while a line records (not paused); a measurement does not count."""
    return (tracker.active_line() or {}).get("state") == "recording"


def _stakeout_session_check():
    """Clear the stakeout session unless it belongs to the active project with
    its current fingerprint and the set of its target is not known damaged.
    RAM only: no file access, no listing."""
    project_id = tracker.active_project_id
    try:
        fingerprint = _stakeout_reading_check(project_id)
    except StakeoutError:
        project_id = fingerprint = None
    stakeout_session.session.keep(project_id, fingerprint, stakeout.store.is_damaged)


def _stakeout_commit_check(project_id, fingerprint, epoch, refuse_recording, set_id=None):
    """Check a session change; returns the fingerprint of project_id.

    Raises StakeoutError: those of _stakeout_reading_check, 409
    project_not_active for a project that is not the active one and 409
    recording_active while a line records if refuse_recording. With epoch
    (after the last await, with none up to the session change): 404
    project_not_found for another fingerprint, 409 project_changed when the
    session epoch moved (a project switch and back, a restore) and 409
    set_damaged when set_id is known damaged.
    """
    current = _stakeout_reading_check(project_id)
    changed = "The project changed meanwhile; select the target again."
    if epoch is not None and current != fingerprint:
        raise StakeoutError("project_not_found", changed, "404 Not Found")
    if project_id != tracker.active_project_id:
        raise StakeoutError("project_not_active", "Select the project first.",
                            "409 Conflict")
    if refuse_recording and _line_recording():
        raise StakeoutError("recording_active", "Pause the line first.", "409 Conflict")
    if epoch is not None and stakeout_session.session.epoch != epoch:
        raise StakeoutError("project_changed", changed, "409 Conflict")
    if set_id and stakeout.store.is_damaged(project_id, set_id):
        raise StakeoutError("set_damaged", "The target set is damaged.", "409 Conflict",
                            set_id=set_id)
    return current


def _stakeout_storage_level():
    """The survey storage level from RAM (E16), None before the survey is loaded
    or without a flash sample: capacity() would then run statvfs (0.72 to 0.95 s
    on the device), which the live path must never trigger."""
    if tracker.is_loaded() and getattr(tracker, "_storage_sample", None) is not None:
        return tracker.capacity()["level"]


def stakeout_live_snapshot(now_ms=None):
    """The one-second stakeout packet (E6, E7), from one tick reading: fix,
    freshness and gate from current_fix_view, corrections from link_health,
    as /api/ui/live has them. RAM only; serialise the result at once."""
    if now_ms is None:
        now_ms = time.ticks_ms()
    _stakeout_session_check()
    ntrip = _instances.get("ntrip")
    rtcm = ntrip.rtcm.summary() if ntrip else {}
    packet = stakeout_session.session.live_packet(
        now_ms, current_fix_view(now_ms), rtcm, link_health(now_ms, rtcm), app.stats,
        _stakeout_storage_level(), _line_recording(), stakeout.now_seconds())
    if stakeout.store.uncertain is not None:
        packet["guidance"]["reasons"].append("write_uncertain")
    return packet


def _stakeout_confirm_window(tid, now_ms=None):
    """The fixes that confirm target tid now (a list), or None: a list exactly
    when the live packet of the same reading reports tid as ready.
    Synchronous, no file access. Call after the last await of a
    confirmation; nothing may await between this call and add_asset."""
    return stakeout_session.session.packet_window(tid, stakeout_live_snapshot(now_ms))


# --- stakeout progress (WP4) ---

def _stakeout_reset():
    """A change of the active project or of the survey (E15): the session and
    every fold go (the unowned folder count with them); the store's removal
    markers and its latch stay."""
    stakeout_session.session.clear()
    stakeout_progress.progress.forget_all()


def _stakeout_points(project_id):
    """The points list of project_id ([] for none); RAM only."""
    for project in tracker.projects:
        if project["id"] == project_id:
            return project["points"]
    return []


def _stakeout_status_of(project_id, tid):
    """The live status of tid (E16): from the cached fold of the session's
    project, "unknown" without one. RAM only."""
    fold = stakeout_progress.progress.peek(project_id, stakeout_session.session.fingerprint)
    return fold.state(tid, _stakeout_points(project_id))[0] if fold else "unknown"


stakeout_session.status_lookup = _stakeout_status_of


def _undo_differs(candidate, expect):
    """True when candidate is None or differs from expect in a field undo_last checks."""
    return not candidate or any(candidate[key] != expect.get(key) for key in (
        "inventory_epoch", "kind", "feature_id", "index", "record_order"))


async def _stakeout_undo(expect):
    """POST /api/tracking undo (E26). Unless the candidate is a stakeout
    point, undo_last(expect) as before. Otherwise, under the change lock:
    the expectation is the candidate (without expect) or must match it;
    after the progress load the candidate is checked again; when the point
    is the one that confirms its target, reopen {tid, gen + 1, by: "undo"}
    is appended first (no reserve check), then, after a pause,
    undo_last(expect) writes. Tracker maintenance is 409 storage_busy for
    either side meanwhile. A failing undo leaves the target open."""
    candidate = tracker.undo_candidate()
    point = None
    if candidate and candidate["kind"] == "asset":
        for item in _stakeout_points(tracker.active_project_id):
            # Only a valid stakeout point can confirm its target.
            if item["id"] == candidate["feature_id"] and stakeout_progress.valid_point_fields(
                    item):
                point = item
    if point is not None and expect is UNDO_UNCHECKED:
        expect = candidate
    if point is None or not isinstance(expect, dict) or _undo_differs(candidate, expect):
        # Not a stakeout point, or an expectation undo_last refuses as it stands.
        tracker.undo_last(expect)
        return
    async with stakeout.store.mutation():
        project_id = tracker.active_project_id
        fold = await _stakeout_load(project_id, _stakeout_project_check(project_id, True))
        # Maintenance stays out for the whole undo: it is refused while the
        # lock is held, and one that began before is checked after each yield.
        _stakeout_refuse_maintenance()
        if _undo_differs(tracker.undo_candidate(), expect):
            raise UndoCandidateChanged()
        tid = point["stakeout_tid"]
        gen, counted = fold.state(tid, _stakeout_points(project_id))[2:]
        # By ID: the survey may hold the same point in a new object by now.
        if counted and counted["id"] == point["id"]:
            stakeout_progress.progress.append(
                fold, stakeout_progress.reopen_event(tid, gen + 1, by="undo"),
                reserve=False, charge=tracker.charge_storage)
            await asyncio.sleep_ms(1)
            _stakeout_refuse_maintenance()
        tracker.undo_last(expect)


async def _stakeout_restore(run):
    """The survey restore await run() under the change lock (E26): the
    session and every fold go before and, whatever the outcome, after it
    (E15); before publishing, the tracker writes restored {floor} to each
    project that was live and has stakeout data (before_restore_publish).
    A failing floor append leaves the restore unpublished (503)."""
    progress = stakeout_progress.progress
    async with stakeout.store.mutation():
        try:
            _stakeout_reset()
            floors = await progress.restore_floors(
                [(project["id"], project.get("created_at"), project["points"])
                 for project in tracker.projects])
            tracker.before_restore_publish = lambda: progress.floor_steps(
                floors, tracker.charge_storage)
            return await run()
        finally:
            tracker.before_restore_publish = None
            _stakeout_reset()


def _stakeout_capacity(size):
    """Whether the flash allows size more bytes, from the cached flash sample."""
    return tracker.storage_capacity(size)["writable"]


async def _stakeout_reserve(size):
    """507 storage_reserve unless the flash allows size more bytes; a failed
    check of the cached sample measures the flash once more first."""
    if not _stakeout_capacity(size):
        await _refresh_flash_sample()
        if not _stakeout_capacity(size):
            raise stakeout_progress.reserve_error()


def _stakeout_restore_orders(fold):
    """Hand the remembered part orders of the active project to a session
    that belongs to no project (E8); no target is selected."""
    session = stakeout_session.session
    if session.project_id is None and fold.project_id == tracker.active_project_id:
        for set_id, parts in fold.orders().items():
            for part in parts:
                session.set_order(fold.project_id, set_id, part, True, fold.fingerprint)


async def _stakeout_fold(project_id, fingerprint, read=True):
    """The fold of project_id, with the remembered orders handed to the
    session: a reader's (E21: the cached one, else loaded while no change
    holds the lock, 503 stakeout_busy otherwise), or with read False the
    load of a change that holds the lock."""
    progress = stakeout_progress.progress
    fold = await (progress.read if read else progress.load)(
        project_id, fingerprint, project_id == tracker.active_project_id, _stakeout_capacity)
    _stakeout_restore_orders(fold)
    return fold


async def _stakeout_load(project_id, fingerprint):
    """The fold of project_id for a change (the caller holds the change lock)."""
    return await _stakeout_fold(project_id, fingerprint, False)


def _stakeout_remember(project_id, fingerprint, event):
    """Append active or order to the cached fold of project_id (E8): only
    while the change lock is free, no latch is set and the reserve allows
    it. True when stored; otherwise a WARN (an append that raised once it
    opened the file has set the latch)."""
    store = stakeout.store
    fold = stakeout_progress.progress.peek(project_id, fingerprint)
    why = store.uncertain or "busy"
    if fold and not store.busy and store.uncertain is None:
        try:
            stakeout_progress.progress.append(fold, event, _stakeout_capacity,
                                              tracker.charge_storage)
            return True
        except StakeoutError as error:
            why = error.code
    log("WARN", "STAKE", "%s of %s not remembered (%s)." % (event["ev"], project_id, why))
    return False


async def _stakeout_progress_list(writer, method, query):
    """GET /api/stakeout/progress (E18), streamed: every target of the visible
    sets that is not open at its set's floor, as [tid, status, gen, mode,
    point_id], with the floors, counts and orders of those sets."""
    project_id = parse_query(query).get("project_id") or tracker.active_project_id
    fingerprint = _stakeout_reading_check(project_id)
    fold = await _stakeout_fold(project_id, fingerprint)
    set_ids = [summary["set_id"]
               for summary in await stakeout.store.sets(project_id, fingerprint)]
    points = _stakeout_points(project_id)
    head = ujson.dumps({
        "schema": 1, "project_id": project_id, "active_tid": fold.active_tid,
        "write_uncertain": stakeout.store.uncertain is not None, "orders": fold.orders(),
        "floors": dict((set_id, fold.floor_of(set_id)) for set_id in set_ids),
        "counts": dict((set_id, fold.counts(set_id, points)) for set_id in set_ids)})
    await _send_tracking_json_stream(writer, _stakeout_rows(head, fold.rows(set_ids, points)),
                                     method)


async def _stakeout_export(writer, query, method):
    """GET|HEAD /api/stakeout/export (E12): the visible sets (or set_id) as
    GeoJSON or CSV (stakeout_export, imported here only), showing the state
    at its start (R9). The export holds the project's sets (no set or project
    deletion meanwhile) and releases them however it ends (R10)."""
    import stakeout_export
    params = parse_query(query)
    project_id = params.get("project_id") or tracker.active_project_id
    form, set_id = params.get("format", "geojson"), params.get("set_id")
    make, content_type, filename = stakeout_export.kind(form, project_id, set_id)
    fingerprint = _stakeout_reading_check(project_id)
    project = [item for item in tracker.projects if item["id"] == project_id][0]
    store = stakeout.store
    store.hold(project_id)
    chunks = None
    try:
        # The progress first: its removal markers hide removed sets (E21).
        await _stakeout_fold(project_id, fingerprint)
        sets = await stakeout_export.readable(store, project_id, fingerprint, set_id)
        fold = await _stakeout_fold(project_id, fingerprint)
        # No await between this fold and the snapshot: the export shows this moment (R9).
        snapshot = stakeout_export.Snapshot(fold, _stakeout_points(project_id))
        chunks = make(store, snapshot, project, sets)
        await _send_geojson_export(writer, chunks, method, None, None, "stakeout " + form,
                                   content_type, filename)
    finally:
        try:
            if chunks is not None:
                chunks.close()
        finally:
            store.release(project_id)


def _stakeout_rows(head, rows):
    """The chunks of the progress list: head (a JSON object) with the rows as "states"."""
    yield head[:-1] + ', "states": ['
    comma = ""
    for row in rows:
        yield comma + ujson.dumps(list(row))
        comma = ","
    yield "]}"


async def _stakeout_overview_or_set(writer, query):
    _stakeout_session_check()
    params = parse_query(query)
    project_id = params.get("project_id") or tracker.active_project_id
    fingerprint = _stakeout_reading_check(project_id)
    store = stakeout.store
    # The progress first: its removal markers hide removed sets (E21).
    fold = await _stakeout_fold(project_id, fingerprint)
    if "set_id" in params:
        # Streamed from the file in 1 KiB pieces (stakeout.TargetStore.send_set).
        await store.send_set(writer, project_id, fingerprint, params["set_id"],
                             _stakeout_set_head, _stakeout_reading_check)
        return
    sets = await store.sets(project_id, fingerprint)
    # A cold listing may have found the target's set damaged.
    _stakeout_session_check()
    load_ms, load_block_ms = store.last_load_ms, store.last_load_block_ms
    points = _stakeout_points(project_id)
    targets = 0
    for summary in sets:
        targets += summary.get("targets", 0)
        summary["progress"] = fold.counts(summary["set_id"], points)
    _send_json(writer, {
        "schema": stakeout.SCHEMA, "project_id": project_id, "sets": sets,
        "totals": {"sets": len(sets), "targets": targets,
                   "orphaned": store.orphaned(project_id, fingerprint)},
        "limits": {"sets": stakeout.MAX_SETS,
                   "targets_per_set": stakeout.MAX_TARGETS_PER_SET,
                   "coords_per_set": stakeout.MAX_COORDS_PER_SET,
                   "import_bytes": _route_body_limit("/api/stakeout/import"),
                   "set_file_bytes": stakeout.MAX_SET_FILE_BYTES},
        "load_ms": load_ms, "load_block_ms": load_block_ms,
        "settings": _stakeout_settings_values(),
        "session": stakeout_session.session.overview(project_id),
        "progress": {"active_tid": fold.active_tid, "damaged_lines": fold.damaged_lines,
                     "write_uncertain": store.uncertain is not None},
        "unowned_folders": stakeout_progress.unowned_folders(store, tracker)})
    # After the answer: the files of removed sets go while nothing holds the lock.
    if store.remnants(project_id) and not store.busy and store.uncertain is None:
        async with store.mutation():
            await store.clear_removed(project_id)


async def _stakeout_action(writer, body):
    try:
        request = ujson.loads(body)
    except ValueError:
        request = None
    if not isinstance(request, dict):
        raise StakeoutError("invalid_request", "Send a JSON object.")
    action = request.get("action")
    if action not in ("delete_set", "select_target", "set_order", "confirm", "skip",
                      "reopen"):
        raise StakeoutError("unknown_action", "Unknown stakeout action.")
    project_id = request.get("project_id", tracker.active_project_id)
    if "project_id" in request and not isinstance(project_id, str):
        raise StakeoutError("invalid_request", "project_id must be a string.")
    if action in ("confirm", "skip", "reopen"):
        await _stakeout_progress_action(writer, request, project_id)
        return
    if action == "select_target":
        await _stakeout_select(writer, request, project_id)
        return
    if action == "set_order":
        await _stakeout_set_order(writer, request, project_id)
        return
    fingerprint = _stakeout_project_check(project_id)
    set_id = request.get("set_id")
    # The removal commits as set_removed in the progress (E14), with the
    # floor of the set's targets at that moment.
    folds = []
    progress = stakeout_progress.progress

    async def prepare(project_id):
        folds.append(await _stakeout_load(project_id, fingerprint))

    def on_remove(project_id, set_id, imported_at):
        fold = folds[0]
        progress.append(fold, stakeout_progress.set_removed_event(
            set_id, imported_at, fold.floor_for(set_id, _stakeout_points(project_id))),
            charge=tracker.charge_storage, reserve=False)

    # The selection goes with a set that is or may be gone (E9); a refusal
    # before the removal keeps it.
    forget = True
    try:
        cleanup = await stakeout.store.delete_set(
            project_id, fingerprint, set_id, request.get("expected_confirmed"),
            _stakeout_project_check, prepare,
            lambda project_id, set_id: folds[0].counts(
                set_id, _stakeout_points(project_id))["confirmed"], on_remove)
    except StakeoutError as error:
        forget = error.code in ("stakeout_delete_failed", "set_not_found")
        raise
    finally:
        if forget:
            stakeout_session.session.forget_set(project_id, set_id)
    try:
        _send_json(writer, {"deleted": set_id, "cleanup": cleanup})
    finally:
        await _refresh_flash_sample()


async def _stakeout_select(writer, request, project_id):
    """select_target (E10): {"tid": <target ID> | null}; the same target again keeps
    the window. Answers {"selected": <target> | null, "changed": bool,
    "remembered": bool}."""
    _stakeout_session_check()
    fingerprint = _stakeout_commit_check(project_id, None, None, True)
    session = stakeout_session.session
    # A missing tid is no deselection: 0 fails the tid check.
    tid = request.get("tid", 0)
    epoch = session.epoch
    fold = await _stakeout_fold(project_id, fingerprint)
    target = None
    if tid is not None:
        target = await stakeout.store.find_target(project_id, fingerprint, tid,
                                                  _stakeout_reading_check)
    _stakeout_commit_check(project_id, fingerprint, epoch, True, target and target["set_id"])
    changed = session.select(project_id, fingerprint, target)
    tid = target and target["tid"]
    # The selected target is remembered (E8), unless it is already.
    _send_json(writer, {"selected": target, "changed": changed,
                        "remembered": fold.active_tid == tid or _stakeout_remember(
                            project_id, fingerprint, stakeout_progress.active_event(tid))})


async def _stakeout_set_order(writer, request, project_id):
    """set_order (E11): {"part_id": "so-xxxxxxxx/<n>", "reversed": bool}, checked
    against the overview of the set (no set file read)."""
    _stakeout_session_check()
    fingerprint = _stakeout_commit_check(project_id, None, None, False)
    session = stakeout_session.session
    part_id, reverse = request.get("part_id"), request.get("reversed")
    parsed = isinstance(part_id, str) and stakeout.parse_tid(part_id + "/1")
    if not parsed or type(reverse) is not bool:
        raise StakeoutError("invalid_request", "Send part_id and reversed (a boolean).")
    set_id, part, _ = parsed
    epoch = session.epoch
    await _stakeout_fold(project_id, fingerprint)
    sets = await stakeout.store.sets(project_id, fingerprint)
    _stakeout_commit_check(project_id, fingerprint, epoch, False)
    # A cold listing may have found the target's set damaged.
    _stakeout_session_check()
    for summary in sets:
        if summary["set_id"] == set_id:
            if summary["state"] != "ok":
                raise StakeoutError("set_damaged", "The target set is damaged.",
                                    "409 Conflict", set_id=set_id)
            if part <= summary["parts"]:
                session.set_order(project_id, set_id, part, reverse, fingerprint)
                _send_json(writer, {"part_id": part_id, "reversed": reverse,
                                    "remembered": _stakeout_remember(
                                        project_id, fingerprint,
                                        stakeout_progress.order_event(part_id, reverse))})
                return
    raise StakeoutError("part_not_found", "No such part.", "404 Not Found")


async def _stakeout_progress_action(writer, request, project_id):
    """confirm (mode verified or manual), skip and reopen (E11): {"tid", "cmd"
    (16 lowercase hex digits, for the log only), "expect_gen", "mode"?,
    "note"?}. Under the change lock the progress loads, the target is read
    (a verified confirmation takes the session's) and the reserve of a skip
    or reopen that writes is checked; then the commit core decides and
    writes once."""
    action, tid, gen = request["action"], request.get("tid"), request.get("expect_gen")
    mode, note = request.get("mode", "verified"), request.get("note", "")
    if (stakeout.parse_tid(tid) is None
            or not stakeout_progress.valid_command(request.get("cmd"))
            or type(gen) is not int or gen < 0 or not isinstance(note, str) or len(note) > 500
            or action == "confirm" and mode not in ("verified", "manual")):
        raise StakeoutError("invalid_request", "Invalid tid, cmd, expect_gen, mode or note.")
    store = stakeout.store
    async with store.mutation():
        fingerprint = _stakeout_commit_check(project_id, None, None, True)
        epoch = stakeout_session.session.epoch
        fold = await _stakeout_load(project_id, fingerprint)
        target = None
        if action != "confirm" or mode == "manual":
            target = await store.find_target(project_id, fingerprint, tid,
                                             _stakeout_reading_check)
        if action != "confirm":
            try:
                repeated = stakeout_progress.decide(fold, action, tid, gen, None,
                                                    _stakeout_points(project_id))[4]
            except StakeoutError:
                # The commit core refuses it.
                repeated = False
            # A repeat writes nothing and needs no reserve.
            if not repeated:
                await _stakeout_reserve(2 * stakeout_progress.LINE_MAX)
        answer = _stakeout_commit(request, project_id, fingerprint, epoch, fold, target)
    _send_json(writer, answer)


def _stakeout_commit(request, project_id, fingerprint, epoch, fold, target, now_ms=None):
    """The commit core of confirm, skip and reopen (E11, E20); synchronous, so
    the checks, the confirmation window and the one flash write see one
    state. target is the read target (None: the session's). An add_asset
    that raised once it began to write sets the latch (503
    stakeout_write_uncertain). Returns the answer."""
    if measurement_session.active():
        raise StakeoutError("measurement_active", "Finish the measurement first.",
                            "409 Conflict")
    _stakeout_project_check(project_id)
    action, tid, cmd = request["action"], request["tid"], request["cmd"]
    _stakeout_commit_check(project_id, fingerprint, epoch, True, tid.split("/")[0])
    progress = stakeout_progress.progress
    if not progress.valid(fold):
        raise StakeoutError("project_changed", "The project changed meanwhile; select the "
                            "target again.", "409 Conflict")
    points = _stakeout_points(project_id)
    status, mode, gen, point, repeated = stakeout_progress.decide(
        fold, action, tid, request["expect_gen"],
        request.get("mode", "verified") if action == "confirm" else None, points)
    if repeated:
        pass
    elif action != "confirm":
        progress.append(fold, (stakeout_progress.skip_event if action == "skip" else
                               stakeout_progress.reopen_event)(tid, gen, cmd),
                        _stakeout_capacity, tracker.charge_storage)
    else:
        session = stakeout_session.session
        fix = tolerance = None
        if mode == "manual":
            # "Found on site": the current position, of any quality (E10).
            fix = current_fix_view(now_ms)[0]
            # All three exact fields are int (one chained comparison).
            if fix["stale"] or not (type(fix.get("lat_e8")) is type(fix.get("lon_e8"))
                                    is type(fix.get("alt_mm")) is int):
                raise StakeoutError("no_position", "No current position.", "409 Conflict")
        else:
            # The window and the packet of one reading (WP2 point 7).
            packet = stakeout_live_snapshot(now_ms)
            fixes = session.packet_window(tid, packet)
            target = session.target
            if fixes is None:
                guidance = packet["guidance"]
                raise StakeoutError("not_ready", "The target is not ready.", "409 Conflict",
                                    extra={"state": guidance["state"], "reasons":
                                           guidance["reasons"] if target and target["tid"] == tid
                                           else ["target_not_selected"]})
            tolerance = CONFIG["stakeout_tolerance_m"]
        if tracker.feature_count() >= tracker.feature_limit():
            raise StakeoutError("point_limit_reached", "Tracking point limit reached.",
                                "409 Conflict")
        # The counter grows before add_asset writes: it tells "nothing
        # written" from "maybe written" whatever the exception (E11, E25).
        before = tracker.event_sequence
        try:
            fix = fix or average_fixes(fixes)
            north, east = geo.offsets_e8_m(fix["lat_e8"], fix["lon_e8"], target["lat_e8"],
                                           target["lon_e8"])
            point = tracker.add_asset("stakeout", target["name"], request.get("note"), fix,
                                      False, {"tid": tid, "gen": gen, "cmd": cmd, "mode": mode,
                                              "tolerance_m": tolerance,
                                              "dn_m": round(north, 3), "de_m": round(east, 3),
                                              "dist_m": round((north * north +
                                                               east * east) ** 0.5, 3)})
        except Exception as error:
            if tracker.event_sequence != before:
                raise stakeout_progress.latch(stakeout.store, "point %s gen %d" % (tid, gen))
            if isinstance(error, MeasurementRejected):
                raise StakeoutError("quality_rejected", str(error), "409 Conflict",
                                    extra={"reasons": list(error.reasons)})
            if isinstance(error, StorageReserveError):
                raise StakeoutError("storage_reserve", str(error), "507 Insufficient Storage")
            if isinstance(error, ValueError):
                raise StakeoutError("invalid_request", str(error))
            raise StakeoutError("tracking_storage_failed", "Point not stored: %s" % error,
                                "507 Insufficient Storage")
        fold.note_point(point, points)
    log("INFO", "STAKE", "%s %s gen %d cmd %s -> %s" % (action, tid, gen, cmd, status))
    return stakeout_progress.answer(tid, status, mode, gen, point, repeated)


def _stakeout_settings_values(defaults=False):
    """The stakeout settings in metres by their API names (the factory values if defaults)."""
    source = DEFAULT_CONFIG if defaults else CONFIG
    return dict((key[len("stakeout_"):], source[key]) for key in STAKEOUT_SETTINGS_KEYS)


async def _stakeout_settings(writer, method, body):
    """GET the stakeout tolerance and fine radius; PUT any subset of them."""
    if method == "PUT":
        try:
            supplied = ujson.loads(body)
        except ValueError:
            supplied = None
        clean = error = None
        if not isinstance(supplied, dict) or not supplied:
            error = "Send a JSON object with tolerance_m and/or fine_radius_m."
        else:
            # The API names are the config keys without their "stakeout_" prefix.
            long = dict(("stakeout_" + key, value) for key, value in supplied.items())
            current = dict((key, CONFIG[key]) for key in STAKEOUT_SETTINGS_KEYS)
            clean, error = validate_stakeout_settings(long, current)
        if error:
            raise StakeoutError("invalid_stakeout_settings", error)
        if _refuse_unreadable_config(writer):
            return
        if not apply_live(clean):
            raise StakeoutError("save_failed", "Stakeout settings could not be saved.",
                                "500 Internal Server Error")
    _send_json(writer, {"values": _stakeout_settings_values(),
                        "defaults": _stakeout_settings_values(True)})


async def http_server_task():
    log("INFO", "HTTP", "HTTP server started on port %s." % CONFIG["http_port"])
    srv = None
    # No except: a server failure must reach supervise(), which logs, counts and
    # restarts; a normal return would leave the service down.
    try:
        srv = await asyncio.start_server(handle_http_client, "0.0.0.0", CONFIG["http_port"])
        log("INFO", "HTTP", "HTTP server running (listeners active on AP and STA).")

        while not shutdown_event.is_set():
            await asyncio.sleep(1)

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
        "/api/field-setup": _handle_field_setup_request,
        "/api/tcp-auth": _handle_tcp_auth_request,
    }.get(path)


def _send_failure(writer, error):
    """Step 1 of _answer_failure: the answer that names the failure (#19a E5, E6)."""
    if isinstance(error, MemoryError):
        gc.collect()
        writer.write(_DEVICE_BUSY_ANSWER)
    elif isinstance(error, StakeoutError):
        _send_stakeout_error(writer, error)
    elif isinstance(error, StorageReserveError):
        _send_api_error(writer, "507 Insufficient Storage", "storage_reserve", str(error))
    elif isinstance(error, StorageBusy):
        _send_api_error(writer, "503 Service Unavailable" if error.code == "survey_loading"
                        else "409 Conflict", error.code, str(error))
    else:
        _send_api_error(writer, "500 Internal Server Error", "internal_error",
                        "The device encountered an internal error.")


def _connection_ended(error):
    """A timeout or a client that went away: no answer, not a server error."""
    return (isinstance(error, asyncio.TimeoutError) or
            (isinstance(error, OSError) and _client_disconnected(error)))


def _diagnose_failure(error):
    """Step 3 of _answer_failure: count and log what the failure says about the server."""
    if isinstance(error, asyncio.TimeoutError):
        # Browsers may open speculative connections without sending requests;
        # a timeout is therefore not an application error.
        log("DEBUG", "HTTP", "Connection closed without a request (timeout).")
    elif _connection_ended(error):
        log("DEBUG", "HTTP", "Client closed connection: %s" % error)
    elif isinstance(error, (StakeoutError, StorageBusy, StorageReserveError)):
        log("DEBUG", "HTTP", "Request refused: %s" % getattr(error, "code", "storage_reserve"))
    elif isinstance(error, MemoryError):
        app.stats["http_errors"] += 1
        log("ERROR", "HTTP", "HTTP handler out of memory.")   # a traceback allocates
        app.log_error("HTTP_Handler", "MemoryError")
    else:
        app.stats["http_errors"] += 1
        log("ERROR", "HTTP", "HTTP handler error.", print_traceback=True, exception=error)
        app.log_error("HTTP_Handler", str(error))


async def _answer_failure(writer, error):
    """Answer a failure that left a handler, then record it; never raises (#19a E5, E14).

    The answer goes out only while nothing was sent: after the header a
    second status line would corrupt the body, so the connection just closes.
    Answer, drain and diagnosis are guarded one by one, answer first: the
    client waits, and the prebuilt busy answer needs no memory.
    """
    try:
        if not _connection_ended(error) and not _answer_started(writer):
            _send_failure(writer, error)
    except Exception:
        try:
            # A MemoryError already tried the busy answer in step 1; never send it twice.
            if not isinstance(error, MemoryError) and not _answer_started(writer):
                writer.write(_DEVICE_BUSY_ANSWER)
        except Exception:
            pass
    try:
        await writer.drain()
    except Exception:
        pass
    try:
        _diagnose_failure(error)
    except Exception:
        pass


class _HttpAnswer:
    """The request writer of the dispatcher: notes the answer start, drops HEAD bodies.

    started is True from the first write attempt on. With head set, bytes up
    to and including the first blank line pass, everything after is dropped;
    rule: every answer's header ends inside one write() call (_http_send does).
    """

    def __init__(self, writer):
        self.writer = writer
        self.started = False
        self.head = False
        self.header_passed = False

    def write(self, data):
        self.started = True
        if self.head:
            if self.header_passed:
                return
            end = bytes(data).find(b"\r\n\r\n")
            if end >= 0:
                self.header_passed = True
                data = data[:end + 4]
        self.writer.write(data)

    async def drain(self):
        await self.writer.drain()

    def close(self):
        self.writer.close()

    async def wait_closed(self):
        await self.writer.wait_closed()

    def get_extra_info(self, key):
        return self.writer.get_extra_info(key)


async def handle_http_client(reader, writer):
    """Parse one request, establish its interface context and dispatch it."""
    global _http_connections
    admitted = False
    try:
        writer = _HttpAnswer(writer)
        app.stats["http_connections_total"] += 1
        if _http_connections >= HTTP_MAX_CONNECTIONS:
            _http_send(writer, "503 Service Unavailable", "text/plain", "",
                       {"Retry-After": "2"})
            await writer.drain()
            return
        _http_connections += 1
        admitted = True
        header, body, request_error = await _read_request(reader)
        writer.head = (header or b"").startswith(b"HEAD ")
        if request_error:
            _log_request_rejected(request_error)
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

        # Probes go to the field app on the canonical AP address (the origin
        # rules accept only that host), not to a claim of Internet access.
        if path in CAPTIVE_PROBES:
            _http_send(writer, "302 Found", "text/plain", "Setup",
                       {"Location": captive_portal_url(), "Cache-Control": "no-store"})
            await writer.drain()
            return

        if path == "/api/captive":
            portal_url = captive_portal_url()
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

        if path == "/api/events":
            if await _serve_event_stream(writer, method, headers, is_ap_request):
                admitted = False  # the stream left the request slots itself (#27)
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
        if path.startswith("/api/stakeout"):
            return await _handle_stakeout_request(writer, path, method, query, body, is_ap_request, headers)
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
                _send_auth_refusal(writer, is_ap_request, headers)
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

    except Exception as e:
        # Answers only before the answer began; see _answer_failure.
        await _answer_failure(writer, e)
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
