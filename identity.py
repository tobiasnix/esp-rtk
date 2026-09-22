# SPDX-License-Identifier: AGPL-3.0-only
"""Stable device identity and access code. The visible identity comes from the factory MAC and therefore does not change during a factory reset or when updating the file system. The secret device code is preferably in the NVS. The JSON fallback also holds the logic on firmware without ``esp32.NVS`` and in the CPython tests usable.
"""
import os
import ubinascii
import ujson


IDENTITY_SCHEMA_VERSION = 3
IDENTITY_FILE = "identity.json"
_NVS_NAMESPACE = "rtk_identity"
_NVS_DEVICE_KEY = "setup_code"
_NVS_STREAM_KEY = "stream_token"
_NVS_BLE_PIN_KEY = "ble_pin"
_CODE_BYTES = 6
_STREAM_BYTES = 16
_cached = None


def _hex(data):
    return ubinascii.hexlify(data).decode().upper()


def device_id_from_mac(mac):
    """Factory-MAC als stabile, transportfreundliche 12-Hex-ID."""
    if not isinstance(mac, (bytes, bytearray)) or len(mac) != 6:
        raise ValueError("Factory MAC must contain exactly 6 bytes.")
    return _hex(bytes(mac))


def names_for(device_id):
    """Derive all visible names from the same short ID."""
    short = device_id[-6:].upper()
    return {
        "display_name": "RTK-" + short,
        "ap_ssid": "RTK-" + short + "-SETUP",
        "ble_name": "RTK-" + short,
        "hostname": "rtk-" + short.lower(),
    }


def generate_device_code():
    """Twelve gut ablesbare Hexzeichen (48 Bit Hardware-Zufall)."""
    return _hex(os.urandom(_CODE_BYTES))


def generate_stream_token():
    """Separate 128-bit secret for authenticated TCP position data."""
    return _hex(os.urandom(_STREAM_BYTES))


def generate_ble_pin():
    """Generate the six-digit passkey required by standard BLE pairing."""
    raw = os.urandom(4)
    number = ((raw[0] << 24) | (raw[1] << 16) | (raw[2] << 8) | raw[3])
    return "%06d" % (number % 1000000)


def _read_nvs(key, minimum=8, maximum=63):
    try:
        import esp32
        nvs = esp32.NVS(_NVS_NAMESPACE)
        buf = bytearray(32)
        length = nvs.get_blob(key, buf)
        value = bytes(buf[:length]).decode("ascii")
        return value if minimum <= len(value) <= maximum else None
    except Exception:
        return None


def _write_nvs(key, value):
    try:
        import esp32
        nvs = esp32.NVS(_NVS_NAMESPACE)
        nvs.set_blob(key, value.encode("ascii"))
        nvs.commit()
        return True
    except Exception:
        return False


def _read_file():
    try:
        with open(IDENTITY_FILE, "r") as fh:
            data = ujson.load(fh)
        result = {}
        for key in ("device_code", "stream_token"):
            value = data.get(key)
            if isinstance(value, str) and 8 <= len(value) <= 63:
                result[key] = value
        pin = data.get("ble_pin")
        if isinstance(pin, str) and len(pin) == 6 and pin.isdigit():
            result["ble_pin"] = pin
        return result
    except Exception:
        return {}


def _write_file(device_code, stream_token, ble_pin):
    tmp = IDENTITY_FILE + ".tmp"
    try:
        with open(tmp, "w") as fh:
            ujson.dump({"schema_version": IDENTITY_SCHEMA_VERSION,
                        "device_code": device_code,
                        "stream_token": stream_token,
                        "ble_pin": ble_pin}, fh)
        try:
            os.rename(tmp, IDENTITY_FILE)
        except OSError:
            try:
                os.remove(IDENTITY_FILE)
            except OSError:
                pass
            os.rename(tmp, IDENTITY_FILE)
        return True
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def _mac_from_network():
    import network
    return network.WLAN(network.STA_IF).config("mac")


def load_identity(mac=None, legacy_ap_password=None, force_reload=False):
    """Load identity and generate it once if necessary. An existing individual AP password is transferred as a device code. A firmware update does not lock out an already used device. The public factory password is never transferred.
    """
    global _cached
    if _cached is not None and not force_reload:
        return dict(_cached)

    if mac is None:
        mac = _mac_from_network()
    device_id = device_id_from_mac(mac)
    file_values = _read_file()
    nvs_code = _read_nvs(_NVS_DEVICE_KEY)
    code = nvs_code or file_values.get("device_code")
    if not code and legacy_ap_password and legacy_ap_password != "12345678":
        code = legacy_ap_password
    if not code:
        code = generate_device_code()
    stream_token = (_read_nvs(_NVS_STREAM_KEY) or
                    file_values.get("stream_token") or
                    generate_stream_token())
    nvs_ble_pin = _read_nvs(_NVS_BLE_PIN_KEY, 6, 6)
    if nvs_ble_pin is not None and not nvs_ble_pin.isdigit():
        nvs_ble_pin = None
    ble_pin = nvs_ble_pin or file_values.get("ble_pin") or generate_ble_pin()
    device_saved = nvs_code is not None or _write_nvs(_NVS_DEVICE_KEY, code)
    stream_saved = (_read_nvs(_NVS_STREAM_KEY) is not None or
                    _write_nvs(_NVS_STREAM_KEY, stream_token))
    pin_saved = (nvs_ble_pin is not None or
                 _write_nvs(_NVS_BLE_PIN_KEY, ble_pin))
    if not (device_saved and stream_saved and pin_saved):
        _write_file(code, stream_token, ble_pin)

    result = {"schema_version": IDENTITY_SCHEMA_VERSION,
              "device_id": device_id, "device_code": code,
              "stream_token": stream_token, "ble_pin": ble_pin}
    result.update(names_for(device_id))
    _cached = result
    return dict(result)


def reset_cache():
    """Only for tests and a controlled firmware restart."""
    global _cached
    _cached = None


def rotate_stream_token():
    """Replaces only the transport locket, never device code or name."""
    global _cached
    current = load_identity()
    token = generate_stream_token()
    if not _write_nvs(_NVS_STREAM_KEY, token):
        if not _write_file(current["device_code"], token, current["ble_pin"]):
            return None
    current["stream_token"] = token
    current["schema_version"] = IDENTITY_SCHEMA_VERSION
    _cached = current
    return token
