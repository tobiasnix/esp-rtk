# SPDX-License-Identifier: AGPL-3.0-only
"""Short-lived admin sessions for the local web interface."""
import os
import time
import ubinascii


SESSION_IDLE_SEC = 30 * 60
SESSION_MAX_SEC = 8 * 60 * 60
FAIL_WINDOW_SEC = 10 * 60
FAIL_LIMIT = 5
BLOCK_SEC = 60

_sessions = {}
_failures = {}


def constant_time_equal(a, b):
    a = str(a or "").encode()
    b = str(b or "").encode()
    different = len(a) ^ len(b)
    for i in range(max(len(a), len(b))):
        left = a[i] if i < len(a) else 0
        right = b[i] if i < len(b) else 0
        different |= left ^ right
    return different == 0


def _token(bytes_count=16):
    return ubinascii.hexlify(os.urandom(bytes_count)).decode()


def parse_cookie(header_value):
    result = {}
    for part in (header_value or "").split(";"):
        if "=" in part:
            key, value = part.strip().split("=", 1)
            result[key] = value
    return result


def _now(now=None):
    if now is not None:
        return int(now * 1000)
    # Session age is a duration, not a calendar time. NTP can move time.time() by years
    # after login and immediately discard the session.
    try:
        return time.ticks_ms()
    except AttributeError:
        return int(time.monotonic() * 1000)


def _diff(newer, older):
    try:
        return time.ticks_diff(newer, older)
    except AttributeError:
        return newer - older


def _prune(now):
    for token, session in list(_sessions.items()):
        if (_diff(now, session["last"]) > SESSION_IDLE_SEC * 1000 or
                _diff(now, session["created"]) > SESSION_MAX_SEC * 1000):
            del _sessions[token]


def login(device_code, expected_code, peer="", now=None):
    """(session, error) - error is ``blocked`` or ``invalid``."""
    now = _now(now)
    history = [stamp for stamp in _failures.get(peer, [])
               if _diff(now, stamp) < FAIL_WINDOW_SEC * 1000]
    if len(history) >= FAIL_LIMIT and _diff(now, history[-1]) < BLOCK_SEC * 1000:
        _failures[peer] = history
        return None, "blocked"
    if not constant_time_equal(device_code, expected_code):
        history.append(now)
        _failures[peer] = history
        return None, "invalid"
    _failures.pop(peer, None)
    token = _token()
    session = {"token": token, "csrf": _token(), "peer": peer,
               "created": now, "last": now}
    _sessions[token] = session
    return dict(session), None


def authenticate(cookie_header, csrf=None, require_csrf=False, now=None):
    now = _now(now)
    _prune(now)
    token = parse_cookie(cookie_header).get("rtk_session")
    session = _sessions.get(token)
    if session is None:
        return None
    if require_csrf and not constant_time_equal(csrf, session["csrf"]):
        return None
    session["last"] = now
    return dict(session)


def logout(cookie_header):
    token = parse_cookie(cookie_header).get("rtk_session")
    if token:
        _sessions.pop(token, None)


def reset():
    """Test Seam; sessions are on the board only in RAM anyway."""
    _sessions.clear()
    _failures.clear()
