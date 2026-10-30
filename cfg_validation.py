# SPDX-License-Identifier: AGPL-3.0-only
"""Pure validation and presentation helpers for device configuration."""
import geo


def html_escape(value):
    text = str(value).replace("&", "&amp;")
    text = text.replace("<", "&lt;").replace(">", "&gt;")
    return text.replace('"', "&quot;").replace("'", "&#39;")


def has_control_characters(text):
    return any(ord(char) < 0x20 or ord(char) == 0x7F for char in text)


PORT_KEYS = ("ntrip_port", "ntrip_fallback_port")


def whole_number(value):
    """Return value as an int when it is a whole number, else None; never raises.

    Type and integrality are checked before converting: bool, None, fractions,
    NaN/infinity and non-numeric types are rejected instead of being cast
    (True would become 1, 65535.9 would become 65535, inf would raise).
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
        return int(value) if int(value) == value else None
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def validate_settings(raw, settable_keys, secret_keys):
    values = {}
    for key, value in raw.items():
        if key not in settable_keys:
            continue
        # A blank or null secret keeps the stored value.
        if key in secret_keys and (value is None or value == ""):
            continue
        if key == "ntrip_enabled":
            values[key] = (value is True or
                           str(value).strip().lower() in ("1", "true", "on", "yes"))
            continue
        if key in PORT_KEYS:
            port = whole_number(value)
            if port is None:
                return None, "%s must be a whole number." % key
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


def prepare_config_change(raw, settable_keys, secret_keys, validators=None):
    """Validate one whole configuration request; returns (values, code, message).

    Every input path (setup form, JSON PUT, restore) calls this, so they refuse
    the same values: settings, then the per-key validators the loader also
    applies, then the fallback networks (list form [[ssid, password], ...]).
    code is "invalid_configuration" or "invalid_wifi_networks"; values is None
    on error. Unsettable keys and blank secrets are dropped.
    """
    if not isinstance(raw, dict):
        return None, "invalid_configuration", "The configuration must be a JSON object."
    supplied = dict(raw)
    has_networks = "wifi_networks" in supplied
    networks = supplied.pop("wifi_networks", None)
    values, error = validate_settings(supplied, settable_keys, secret_keys)
    if error:
        return None, "invalid_configuration", error
    for key in list(values):
        validator = (validators or {}).get(key)
        if validator is None:
            continue
        clean, error = validator({key: values[key]})
        if error:
            return None, "invalid_configuration", error
        values[key] = clean[key]
    if has_networks:
        clean, error = validate_wifi_networks(networks)
        if error:
            return None, "invalid_wifi_networks", error
        values["wifi_networks"] = clean
    return values, None, None


# Keys a backup may hold as "" although the forms require them: NTRIP off or no
# caster yet, no primary SSID (the fallback networks carry the station).
RESTORE_EMPTY_KEYS = ("ntrip_host", "ntrip_mount", "wifi_ssid")
RESTORE_WIFI_KEYS = ("wifi_ssid", "wifi_pass", "wifi_networks")


def prepare_restore_change(raw, settable_keys, secret_keys, validators=None):
    """Validate a backup's connection settings; returns (values, kept, code, message).

    A device must be able to restore its own backup, which exports every
    settable key as stored, including the empty states the forms refuse:
    "" for ntrip_host, ntrip_mount and wifi_ssid is taken over as "", and a
    "" secret means empty (the backup carries secrets in clear text), while
    None still keeps the stored one. Everything else is checked exactly as by
    prepare_config_change, which the forms keep using. A backup without any
    Wi-Fi network (factory state, hotspot only) leaves the target's Wi-Fi
    keys untouched: a candidate without a network would be rolled back at the
    verification restart. kept is then ("wifi",), else ().
    """
    if not isinstance(raw, dict):
        values, code, message = prepare_config_change(raw, settable_keys, secret_keys, validators)
        return values, (), code, message
    supplied, empty = dict(raw), []
    for key in RESTORE_EMPTY_KEYS + tuple(secret_keys):
        if key in settable_keys and supplied.get(key) == "":
            del supplied[key]
            empty.append(key)
    values, code, message = prepare_config_change(supplied, settable_keys, secret_keys, validators)
    if values is None:
        return None, (), code, message
    for key in empty:
        values[key] = ""
    if values.get("wifi_ssid") == "" and not values.get("wifi_networks"):
        for key in RESTORE_WIFI_KEYS:
            values.pop(key, None)
        return values, ("wifi",), None, None
    return values, (), None, None


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


_FIELD_SETUP_TEXT_LIMITS = {"reference_frame": 40, "reference_frame_epoch": 16}
MAX_ANTENNA_HEIGHT_CM = 300
FIELD_SETUP_KEYS = ("antenna_height_m", "reference_frame", "reference_frame_epoch")


def validate_field_setup(values):
    """Validate any subset of FIELD_SETUP_KEYS; returns (clean, error)."""
    if not isinstance(values, dict):
        return None, "Field setup must be an object."
    clean = {}
    for key, value in values.items():
        if key == "antenna_height_m":
            if isinstance(value, bool) or not isinstance(value, (int, float, str)):
                return None, "antenna_height_m must be a number."
            if isinstance(value, float) and (value != value or value in (
                    float("inf"), float("-inf"))):
                return None, "antenna_height_m must be a finite number."
            # Decimal half-up on the text: binary round() is not portable.
            centimetres = geo.parse_decimal_fixed(str(value).strip(), 2)
            if centimetres is None:
                return None, "antenna_height_m must be a number."
            if not 0 <= centimetres <= MAX_ANTENNA_HEIGHT_CM:
                return None, "antenna_height_m must be between 0 and 3 m."
            clean[key] = centimetres / 100
        elif key in _FIELD_SETUP_TEXT_LIMITS:
            maximum = _FIELD_SETUP_TEXT_LIMITS[key]
            if not isinstance(value, str):
                return None, "%s must be text." % key
            if len(value) > maximum:
                return None, "%s must be at most %d characters." % (key, maximum)
            if has_control_characters(value):
                return None, "%s contains control characters." % key
            # Stripped, not case-folded: ETRS89 and etrs89 stay the user's text.
            clean[key] = value.strip()
        else:
            return None, "Unknown field setup key: %s" % key
    return clean, None


STAKEOUT_SETTINGS_KEYS = ("stakeout_tolerance_m", "stakeout_fine_radius_m")
STAKEOUT_TOLERANCE_MM = (20, 5000)
STAKEOUT_FINE_RADIUS_MM = (1000, 50000)


def stakeout_pair_error(tolerance_m, fine_radius_m):
    """The one cross rule of the stakeout settings: None when the pair is valid."""
    if tolerance_m < fine_radius_m:
        return None
    return "stakeout_tolerance_m must be smaller than stakeout_fine_radius_m."


def validate_stakeout_settings(values, current=None):
    """Validate any subset of STAKEOUT_SETTINGS_KEYS (metres); returns (clean, error).

    current holds the values the subset is checked against for the cross rule;
    without both values known there is no cross check."""
    if not isinstance(values, dict):
        return None, "Stakeout settings must be an object."
    clean = {}
    for key, value in values.items():
        if key not in STAKEOUT_SETTINGS_KEYS:
            return None, "Unknown stakeout setting: %s" % key
        # Decimal half-up on the text: binary round() is not portable. The text of
        # nan and infinity does not parse, so only finite numbers get through.
        millimetres = None if isinstance(value, bool) or not isinstance(
            value, (int, float, str)) else geo.parse_decimal_fixed(str(value).strip(), 3)
        low, high = (STAKEOUT_TOLERANCE_MM, STAKEOUT_FINE_RADIUS_MM)[STAKEOUT_SETTINGS_KEYS.index(key)]
        if millimetres is None or not low <= millimetres <= high:
            return None, "%s must be a number from %s to %s m." % (key, low / 1000, high / 1000)
        clean[key] = millimetres / 1000
    known = dict(current or {})
    known.update(clean)
    if "stakeout_tolerance_m" in known and "stakeout_fine_radius_m" in known:
        error = stakeout_pair_error(known["stakeout_tolerance_m"], known["stakeout_fine_radius_m"])
        if error:
            return None, error
    return clean, None


# The feeder runs every 2 s and the longest known synchronous pause was 8.8 s; a
# shorter timeout would reset the device in a loop even in safe mode.
WDT_TIMEOUT_MIN_MS = 10000
# Ten minutes is far beyond any pause; from 2**31 ms on, machine.WDT() raises at boot.
WDT_TIMEOUT_MAX_MS = 600000


def validate_ports(values):
    """Validate any subset of PORT_KEYS; returns (clean, error)."""
    return validate_settings(values, PORT_KEYS, ())


def validate_wdt_timeout(values):
    """Validate {"wdt_timeout": ms}; returns (clean, error)."""
    n = whole_number(values.get("wdt_timeout"))
    if n is None or not WDT_TIMEOUT_MIN_MS <= n <= WDT_TIMEOUT_MAX_MS:
        return None, ("wdt_timeout must be a whole number from %d to %d ms."
                      % (WDT_TIMEOUT_MIN_MS, WDT_TIMEOUT_MAX_MS))
    return {"wdt_timeout": n}, None


MEASUREMENT_SAMPLES_CHOICES = (1, 5, 10, "quality")


def validate_measurement_samples(values):
    """Validate the device-wide measurement method (1, 5, 10 or "quality").

    Returns (clean, error). "quality" must match exactly; numbers go through
    whole_number, which rejects bool (True would otherwise equal 1).
    """
    value = values["measurement_samples"]
    if isinstance(value, str) and value == "quality":
        return {"measurement_samples": value}, None
    number = whole_number(value)
    if number is not None and number in (1, 5, 10):
        return {"measurement_samples": number}, None
    return None, "measurement_samples must be 1, 5, 10 or quality."


# Loader table: key -> validator(values) -> (clean, error). #23a feeds it into
# prepare_config_change.
LOAD_VALIDATORS = dict(
    [(key, validate_field_setup) for key in FIELD_SETUP_KEYS] +
    [(key, validate_ports) for key in PORT_KEYS] +
    [(key, validate_stakeout_settings) for key in STAKEOUT_SETTINGS_KEYS] +
    [("wdt_timeout", validate_wdt_timeout),
     ("measurement_samples", validate_measurement_samples)])
