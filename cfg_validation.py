# SPDX-License-Identifier: AGPL-3.0-only
"""Pure validation and presentation helpers for device configuration."""


def html_escape(value):
    text = str(value).replace("&", "&amp;")
    text = text.replace("<", "&lt;").replace(">", "&gt;")
    return text.replace('"', "&quot;").replace("'", "&#39;")


def has_control_characters(text):
    return any(ord(char) < 0x20 or ord(char) == 0x7F for char in text)


def validate_settings(raw, settable_keys, secret_keys):
    values = {}
    for key, value in raw.items():
        if key not in settable_keys:
            continue
        if key in secret_keys and value == "":
            continue
        if key == "ntrip_enabled":
            values[key] = (value is True or
                           str(value).strip().lower() in ("1", "true", "on", "yes"))
            continue
        if key in ("ntrip_port", "ntrip_fallback_port"):
            try:
                port = int(value)
            except (ValueError, TypeError):
                return None, "%s is not a number." % key
            if not 1 <= port <= 65535:
                return None, "%s must be between 1 and 65535." % key
            values[key] = port
            continue
        if not isinstance(value, str):
            value = str(value)
        if key == "wifi_ssid" and not 1 <= len(value) <= 32:
            return None, "wifi_ssid must contain 1 to 32 characters."
        if key == "wifi_pass" and not 8 <= len(value) <= 63:
            return None, "wifi_pass must contain 8 to 63 characters (WPA2)."
        if key == "ap_ssid" and not 1 <= len(value) <= 32:
            return None, "ap_ssid must contain 1 to 32 characters."
        if key == "ap_pass" and not 8 <= len(value) <= 63:
            return None, "ap_pass must contain 8 to 63 characters (WPA2)."
        if key in ("ntrip_host", "ntrip_fallback_host"):
            minimum = 0 if key.startswith("ntrip_fallback") else 1
            if not minimum <= len(value) <= 64:
                return None, "%s must contain %d to 64 characters." % (key, minimum)
        if key in ("ntrip_mount", "ntrip_fallback_mount"):
            if key.startswith("ntrip_fallback") and value == "":
                values[key] = value
                continue
            if not 1 <= len(value) <= 64:
                return None, "%s must contain 1 to 64 characters." % key
            if "/" in value:
                return None, "%s must not contain a slash." % key
        if key in ("ntrip_user", "ntrip_fallback_user") and len(value) > 64:
            return None, "%s is too long (maximum 64 characters)." % key
        if has_control_characters(value):
            return None, "%s contains control characters." % key
        values[key] = value
    return values, None


def validate_wifi_networks(networks, maximum=8):
    """Validate the shared fallback-network representation used by every input path."""
    if not isinstance(networks, list) or len(networks) > maximum:
        return None, "wifi_networks must be a list containing at most %d networks" % maximum
    clean, seen = [], set()
    for entry in networks:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            return None, "Invalid wifi_networks entry"
        ssid, password = entry
        if not isinstance(ssid, str) or not isinstance(password, str):
            return None, "SSID and Wi-Fi password must be strings"
        if (not 1 <= len(ssid) <= 32 or
                (password and not 8 <= len(password) <= 63)):
            return None, "Invalid SSID or password length in wifi_networks"
        if has_control_characters(ssid) or has_control_characters(password):
            return None, "wifi_networks contains control characters"
        if ssid in seen:
            return None, "wifi_networks contains duplicate SSIDs"
        seen.add(ssid)
        clean.append([ssid, password])
    return clean, None


def configured_networks(config):
    networks, seen = [], set()
    primary = config.get("wifi_ssid", "")
    if primary:
        networks.append((primary, config.get("wifi_pass", "")))
        seen.add(primary)
    for entry in config.get("wifi_networks", []) or []:
        if not isinstance(entry, (list, tuple)) or len(entry) < 2:
            continue
        ssid, password = entry[0], entry[1]
        if not ssid or ssid in seen:
            continue
        networks.append((ssid, password)); seen.add(ssid)
    return networks


def configured_networks_text(networks):
    return "\n".join(str(entry[0]) for entry in (networks or [])
                     if isinstance(entry, (list, tuple)) and entry and entry[0])


def parse_configured_networks(text, existing):
    old = {}
    for entry in existing or []:
        if isinstance(entry, (list, tuple)) and len(entry) >= 2:
            old[entry[0]] = entry[1]
    new = []
    for line in (text or "").split("\n"):
        line = line.strip()
        if not line:
            continue
        if ":" in line:
            ssid, password = line.split(":", 1)
            ssid, password = ssid.strip(), password.strip()
        else:
            ssid, password = line, old.get(line)
            if password is None:
                continue
        if ssid:
            new.append([ssid, password])
    return new


def subnet_prefix(ip):
    parts = (ip or "").split(".")
    return ".".join(parts[:3]) + "." if len(parts) == 4 else ""


def same_subnet(a, b):
    first, second = subnet_prefix(a), subnet_prefix(b)
    return bool(first) and first == second
