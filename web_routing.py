# SPDX-License-Identifier: AGPL-3.0-only
"""Declarative transport policy for the local HTTP server.

Entries contain ``(allowed_methods, method_error, body_limit)``.  Authentication
and domain decisions intentionally stay in the endpoint handlers: a route table
must not silently widen the device's trust boundary.
"""

DEFAULT_BODY_LIMIT = 4096
TRACKING_BODY_LIMIT = 262144
TRACKING_IMPORT_LIMIT = 8 * 1024 * 1024

_GET = (("GET", "HEAD"), "GET required", DEFAULT_BODY_LIMIT)
_POST = (("POST",), "POST required", DEFAULT_BODY_LIMIT)

ROUTES = {
    "/": _GET,
    "/ui": _GET,
    "/ui-v2": _GET,
    "/setup": _GET,
    "/label": _GET,
    "/api/label": _GET,
    "/api/access": _GET,
    "/api/ui/live": _GET,
    "/api/captive": _GET,
    "/diagnostics": _GET,
    "/api/diagnostics": _GET,
    "/api/field-diagnostics": (("GET", "POST"), "GET or POST required", DEFAULT_BODY_LIMIT),
    "/api/field-diagnostics/export": _GET,
    "/api/wifi-scan": _GET,
    "/api/config-result": _GET,
    "/api/config/backup": _GET,
    "/qr.js": _GET,
    "/i18n.js": _GET,
    "/base.css": _GET,
    "/pages.css": _GET,
    "/pages.js": _GET,
    "/app.css": _GET,
    "/app.js": _GET,
    "/status": _GET,
    "/health": _GET,
    "/rtcm": _GET,
    "/errors": _GET,
    "/api/log": _GET,
    "/api/parallel-benchmark": (("GET", "POST", "DELETE"),
                                "GET, POST or DELETE required", DEFAULT_BODY_LIMIT),
    "/api/parallel-benchmark/export": _GET,
    "/api/config/restore": _POST,
    "/api/config/apply": _POST,
    "/api/ble-maintenance": _POST,
    "/api/stream-token/rotate": _POST,
    "/api/reboot": _POST,
    "/save": (("POST",), "Only POST is allowed.", DEFAULT_BODY_LIMIT),
    "/api/session": (("GET", "POST", "DELETE"),
                     "GET, POST or DELETE required", DEFAULT_BODY_LIMIT),
    "/api/config": (("GET", "PUT"), "GET or PUT required", DEFAULT_BODY_LIMIT),
    "/api/measurement-gate": (("GET", "PUT", "DELETE"),
                              "GET, PUT or DELETE required", DEFAULT_BODY_LIMIT),
    "/api/tcp-auth": (("GET", "PUT"), "GET or PUT required", DEFAULT_BODY_LIMIT),
    "/gnss": (("GET", "POST"), "GET or POST required", DEFAULT_BODY_LIMIT),
    "/api/gnss-command": (("GET", "POST"), "GET or POST required",
                          DEFAULT_BODY_LIMIT),
    "/api/tracking": (("GET", "POST", "HEAD"), "GET or POST required",
                      TRACKING_BODY_LIMIT),
    "/api/tracking/import": (("POST",), "POST required", TRACKING_IMPORT_LIMIT),
    "/api/tracking/export": (("GET", "POST", "HEAD"),
                             "GET or POST required", DEFAULT_BODY_LIMIT),
    "/api/tracking/line": (("GET", "HEAD"), "GET required",
                           DEFAULT_BODY_LIMIT),
    "/api/tracking/map": (("GET", "HEAD"), "GET required",
                          DEFAULT_BODY_LIMIT),
}

for _probe in ("/generate_204", "/gen_204", "/hotspot-detect.html",
               "/library/test/success.html", "/ncsi.txt", "/connecttest.txt",
               "/redirect"):
    ROUTES[_probe] = _GET


def route_policy(path):
    """Return the immutable policy tuple for *path*, or ``None``."""
    return ROUTES.get(path)


def method_error(path, method):
    policy = route_policy(path)
    if policy is not None and method not in policy[0]:
        return policy[1]
    return None


def body_limit(path):
    policy = route_policy(path)
    return policy[2] if policy is not None else DEFAULT_BODY_LIMIT
