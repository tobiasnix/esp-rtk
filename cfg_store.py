# SPDX-License-Identifier: AGPL-3.0-only
"""Transactional configuration persistence and schema migration."""
import os
import time
import ujson


def coerce(defaults, key, value):
    default = defaults.get(key)
    if isinstance(default, bool):
        return value.lower() in ("1", "true", "on", "yes") if isinstance(value, str) else bool(value)
    if isinstance(default, int):
        try: return int(value)
        except (ValueError, TypeError): return default
    if isinstance(default, float):
        try: return float(value)
        except (ValueError, TypeError): return default
    return value


def persisted(config, defaults, excluded, overrides=None):
    data = {key: config[key] for key in defaults if key not in excluded}
    if overrides:
        data.update(overrides)
    return data


def _valid_json(path):
    try:
        with open(path, "r") as fh:
            value = ujson.load(fh)
        return isinstance(value, dict)
    except (OSError, ValueError, TypeError):
        return False


def recover_atomic(target):
    """Recover a JSON file whose atomic replacement lost power mid-rename."""
    temporary, previous = target + ".tmp", target + ".previous"
    if _valid_json(target):
        for stale in (temporary, previous):
            try: os.remove(stale)
            except OSError: pass
        return True
    for candidate in (temporary, previous):
        if not _valid_json(candidate):
            continue
        try: os.remove(target)
        except OSError: pass
        os.rename(candidate, target)
        for stale in (temporary, previous):
            if stale != candidate:
                try: os.remove(stale)
                except OSError: pass
        return True
    return False


def atomic_dump(target, data, log):
    tmp = target + ".tmp"
    previous = target + ".previous"
    moved_current = False
    try:
        with open(tmp, "w") as fh: ujson.dump(data, fh)
        if not _valid_json(tmp):
            raise ValueError("temporary JSON verification failed")
        try: os.remove(previous)
        except OSError: pass
        if _valid_json(target):
            os.rename(target, previous)
            moved_current = True
        else:
            try: os.remove(target)
            except OSError: pass
        try:
            os.rename(tmp, target)
        except Exception:
            if moved_current:
                try: os.rename(previous, target)
                except OSError: pass
            raise
        try: os.remove(previous)
        except OSError: pass
        return True
    except Exception as exc:
        log("ERROR", "SYS", "Saving %s failed: %s" % (target, exc))
        try: os.remove(tmp)
        except OSError: pass
        return False


def load(config, defaults, excluded, save, log):
    recover_atomic(config["config_file"])
    try:
        with open(config["config_file"], "r") as fh: loaded = ujson.load(fh)
        for key in defaults:
            if key not in excluded and key in loaded:
                config[key] = coerce(defaults, key, loaded[key])
        try: schema = int(loaded.get("config_schema_version", 1) or 1)
        except (ValueError, TypeError): schema = 1
        migrated = False
        if schema < 2 and int(config.get("nmea_tcp_port", 0)) == 2947:
            config.update(nmea_tcp_port=10110, nmea_tcp_legacy_port=2947,
                          nmea_tcp_legacy_enabled=True); migrated = True
        if schema < 3:
            legacy = "ap_kanalwechsel_intervall_sec"
            if legacy in loaded:
                config["ap_channel_switch_interval_sec"] = coerce(defaults, "ap_channel_switch_interval_sec", loaded[legacy])
            config["config_schema_version"] = 3; migrated = True
        if migrated: save()
        log("INFO", "SYS", "Configuration loaded from %s." % config["config_file"])
    except (OSError, ValueError):
        log("INFO", "SYS", "No saved configuration (%s) found; using defaults." % config["config_file"])


def result_read(path):
    recover_atomic(path)
    try:
        with open(path, "r") as fh: return ujson.load(fh)
    except (OSError, ValueError): return {"schema_version": 2, "status": "none", "details": {}}


def result_write(path, atomic, status, details=None):
    payload = {"schema_version": 2, "status": status, "details": details or {},
               "uptime_sec": int(time.time())}
    return atomic(path, payload)


def pending_active(path):
    try: os.stat(path); return True
    except OSError: return False


def stage(config, defaults, excluded, new_data, last_good, pending,
          atomic, result):
    current = persisted(config, defaults, excluded)
    candidate = persisted(config, defaults, excluded, new_data)
    if not atomic(last_good, current): return False
    if not atomic(pending, candidate):
        try: os.remove(last_good)
        except OSError: pass
        return False
    if not atomic(config["config_file"], candidate):
        for path in (pending, last_good):
            try: os.remove(path)
            except OSError: pass
        return False
    config.update(new_data); result("pending"); return True


def commit(config, pending, last_good, log, result, active_ssid=None):
    for path in (pending, last_good):
        try: os.remove(path)
        except OSError: pass
    log("INFO", "SYS", "New network configuration confirmed operational.")
    result("connected", details={"ssid": active_ssid or config.get("wifi_ssid", "")})


def rollback(config, defaults, excluded, last_good, pending, atomic,
             commit_fn, log, result):
    try:
        with open(last_good, "r") as fh: previous = ujson.load(fh)
    except (OSError, ValueError): return False
    if not atomic(config["config_file"], previous): return False
    for key in defaults:
        if key in previous and key not in excluded:
            config[key] = coerce(defaults, key, previous[key])
    commit_fn(); log("WARN", "SYS", "New network configuration rejected; previous configuration restored.")
    result("failed"); return True


def migrate_runtime_history(marker, paths, logger):
    try: os.stat(marker); return False
    except OSError: pass
    for path in paths:
        try: os.remove(path)
        except OSError: pass
    logger.clear_rtc(); logger.start_new()
    try:
        with open(marker, "w") as fh: fh.write("V11\n")
    except OSError: return False
    return True
