# SPDX-License-Identifier: AGPL-3.0-only
"""Factory reset and setup credential helpers."""
import os
import ubinascii


def button_held(samples, required):
    consecutive = 0
    for value in samples:
        consecutive = consecutive + 1 if value == 0 else 0
        if consecutive >= required:
            return True
    return False


def factory_reset(config, defaults, clear_keys, save, log):
    previous = dict((key, config.get(key)) for key in clear_keys)
    for key in clear_keys:
        default = defaults.get(key, "")
        config[key] = [] if isinstance(default, list) else default
    if not save():
        config.update(previous)
        log("ERROR", "SYS", "Factory reset aborted: configuration could not be saved.")
        return False
    try:
        from identity import rotate_stream_token
        rotate_stream_token()
    except Exception as exc:
        log("ERROR", "SYS", "Could not rotate stream token: %s" % exc)
    paths = ["ble-bonds.json", "ble-bonds.tmp", "tracking.jsonl",
             "tracking.jsonl.tmp", "tracking.jsonl.previous",
             "tracking.index.json", "tracking.index.json.tmp",
             "tracking.index.json.previous", "tracking.checkpoint.json",
             "tracking.checkpoint.json.tmp", "tracking.checkpoint.json.previous",
             "tracking.jsonl.points-v1.jsonl",
             "tracking.jsonl.points-v1.jsonl.tmp",
             "tracking.jsonl.points-v1.jsonl.previous",
             "tracking.jsonl.points-v1.jsonl.segments.json",
             "tracking.jsonl.points-v1.jsonl.segments.json.tmp",
             "tracking.jsonl.points-v1.jsonl.segments.json.previous",
             "ble-bonds.previous", "config.pending.json",
             "config.pending.json.tmp", "config.pending.json.previous",
             "config.last_good.json", "config.last_good.json.tmp",
             "config.last_good.json.previous"]
    try:
        paths.extend(name for name in os.listdir()
                     if name.startswith(("tracking.segment-", "survey-export-"))
                     or (name.startswith("tracking.jsonl.points-v1.jsonl.g") and
                         name.endswith(".pjs"))
                     or name.endswith((".ndjson.tmp", ".tracking.tmp")))
    except OSError:
        pass
    for path in paths:
        try:
            os.remove(path)
        except OSError:
            pass
    log("WARN", "SYS", "Factory reset completed; device identity retained.")
    return True


def generate_ap_password():
    return "gnss-" + ubinascii.hexlify(os.urandom(6)).decode()


def derive_ap_password(mac):
    return "gnss-" + ubinascii.hexlify(mac).decode()[-6:]


def is_derived(password, mac):
    return bool(password) and password == derive_ap_password(mac)
