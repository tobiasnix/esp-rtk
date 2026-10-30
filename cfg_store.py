# SPDX-License-Identifier: AGPL-3.0-only
"""Transactional configuration persistence and schema migration."""
import os
import ujson


def coerce(defaults, key, value):
    default = defaults.get(key)
    if isinstance(default, bool):
        return value.lower() in ("1", "true", "on", "yes") if isinstance(value, str) else bool(value)
    if isinstance(default, int):
        try: return int(value)
        except (ValueError, TypeError, OverflowError): return default
    if isinstance(default, float):
        try: return float(value)
        except (ValueError, TypeError, OverflowError): return default
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


def _exists(path):
    try: os.stat(path); return True
    except OSError: return False


def atomic_dump(target, data, log):
    tmp = target + ".tmp"
    previous = target + ".previous"
    unreadable = target + ".unreadable"
    moved_current = moved_unreadable = False
    try:
        with open(tmp, "w") as fh:
            ujson.dump(data, fh)
            # flush is what made a write survive a power cut on this port (device validation V15.10.0, 4.2); os.sync() added nothing measurable.
            fh.flush()
        if not _valid_json(tmp):
            raise ValueError("temporary JSON verification failed")
        try: os.remove(previous)
        except OSError: pass
        if _valid_json(target):
            os.rename(target, previous)
            moved_current = True
        elif _exists(target):
            # Never delete what could not be read: <target>.unreadable keeps the
            # original for a manual rescue (an older copy is replaced). If that
            # rename fails, the write fails and the file stays.
            try: os.remove(unreadable)
            except OSError: pass
            os.rename(target, unreadable)
            moved_unreadable = True
        try:
            os.rename(tmp, target)
        except Exception:
            if moved_current:
                try: os.rename(previous, target)
                except OSError: pass
            elif moved_unreadable:
                try: os.rename(unreadable, target)
                except OSError: pass
            raise
        try: os.remove(previous)
        except OSError: pass
        return True
    except Exception as exc:
        log("ERROR", "SYS", "Saving %s failed: %s" % (target, exc))
        # A failed write never leaves a recoverable candidate: recovery would
        # otherwise activate a change the caller reported as failed. If an
        # unreadable target could not be put back, its original stays in
        # <target>.unreadable and the next boot runs on the defaults, as it
        # already did with the unreadable file.
        try: os.remove(tmp)
        except OSError: pass
        return False


def load(config, defaults, excluded, save, log, validated=None, warnings=None, pair_check=None):
    """validated maps a key to a validator(values) -> (clean, error) that replaces
    coerce: a stored value it rejects is dropped, never turned into a silent
    substitute. The reason goes to the log and into the warnings list.
    pair_check(config, warnings) checks rules across keys after the migration and
    before any save, so a migration save never writes an invalid combination.

    Never raises: the cfg import must not fail on config.json. A failed recovery
    (for example EIO on a rename) reads a readable candidate in place; a file
    that exists but is unreadable or holds no JSON object leaves config as it is
    (the defaults at import), logs an ERROR and is not overwritten here.

    Returns False when config.json exists but nothing of it was loaded (or the
    merge failed), and None when a candidate was read in place after a failed
    recovery: in both cases the caller protects the file for this boot, so no
    automatic writer replaces the only surviving copy; with None the settings
    in config are the recovered ones, not defaults."""
    path = config["config_file"]
    candidates = (path,)
    recovery_failed = False
    try:
        recover_atomic(path)
    except Exception as exc:
        log("ERROR", "SYS", "Recovering %s failed: %s" % (path, exc))
        candidates = (path, path + ".tmp", path + ".previous")
        recovery_failed = True
    loaded = None
    for candidate in candidates:
        loaded = _read_dict(candidate)
        if loaded is not None:
            break
    if loaded is None:
        if staging_trace(path):
            log("ERROR", "SYS", "%s cannot be read or holds no JSON object; using defaults, "
                                "the file is kept." % path)
            return False
        log("INFO", "SYS", "No saved configuration (%s) found; using defaults." % path)
        return True
    try:
        for key in defaults:
            if key in excluded or key not in loaded:
                continue
            if validated and key in validated:
                clean, error = validated[key]({key: loaded[key]})
                if error:
                    config[key] = defaults[key]
                    text = "config.json: %s; using the default." % error
                    log("WARN", "SYS", text)
                    if warnings is not None: warnings.append({"key": key, "message": error})
                else:
                    config[key] = clean[key]
                continue
            config[key] = coerce(defaults, key, loaded[key])
        try: schema = int(loaded.get("config_schema_version", 1) or 1)
        except (ValueError, TypeError, OverflowError): schema = 1
        migrated = False
        if schema < 2 and int(config.get("nmea_tcp_port", 0)) == 2947:
            config.update(nmea_tcp_port=10110, nmea_tcp_legacy_port=2947,
                          nmea_tcp_legacy_enabled=True); migrated = True
        if schema < 3:
            legacy = "ap_kanalwechsel_intervall_sec"
            if legacy in loaded:
                config["ap_channel_switch_interval_sec"] = coerce(defaults, "ap_channel_switch_interval_sec", loaded[legacy])
            config["config_schema_version"] = 3; migrated = True
        if pair_check:
            try: pair_check(config, warnings)
            except Exception as exc:
                log("ERROR", "SYS", "Checking the loaded configuration failed: %s" % exc)
        if recovery_failed:
            log("ERROR", "SYS", "Configuration read from %s in place; automatic saves wait "
                                "for a deliberate configuration write." % candidate)
            return None
        if migrated: save()
        log("INFO", "SYS", "Configuration loaded from %s." % path)
        return True
    except Exception as exc:
        log("ERROR", "SYS", "Loading %s failed: %s" % (path, exc))
        return False


def result_read(path, log=None):
    """The stored result; never raises.

    If the recovery of the file fails (for example EIO on a rename), a valid
    candidate is read in place. A result file (or a leftover of it) that
    exists but cannot be recovered or read is "unknown": it may hold a
    rollback decision. "none" means that no result file exists.
    """
    try:
        recover_atomic(path)
    except Exception as exc:
        if log: log("ERROR", "SYS", "Recovering %s failed: %s" % (path, exc))
        for candidate in (path + ".tmp", path + ".previous"):
            value = _read_dict(candidate)
            if value is not None:
                return value
        return {"schema_version": 2, "status": "unknown", "details": {}}
    value = _read_dict(path)
    if value is not None and "status" in value:
        return value
    if staging_trace(path):
        if log: log("ERROR", "SYS", "%s exists but cannot be read." % path)
        return {"schema_version": 2, "status": "unknown", "details": {}}
    return {"schema_version": 2, "status": "none", "details": {}}


def result_write(path, atomic, status, details=None, uptime_sec=0, boot_id=None,
                 apply_id=None):
    """Write one result record; boot_id is the boot that writes it, apply_id its request."""
    payload = {"schema_version": 2, "status": status, "details": details or {},
               "uptime_sec": int(uptime_sec), "boot_id": boot_id, "apply_id": apply_id}
    return atomic(path, payload)


def pending_active(path):
    try: os.stat(path); return True
    except OSError: return False


def staging_trace(path):
    """The file, or a leftover of its atomic replacement, exists."""
    return pending_active(path) or pending_active(path + ".tmp") or pending_active(path + ".previous")


def arm(config, defaults, excluded, new_data, last_good, pending, atomic, log):
    """Arm a candidate: last-good, then pending, then config.json, then config.

    On a failed write the files this call wrote are removed again and config
    stays as it was. The result file and an open transaction are the caller's
    business (cfg.apply_staged_config).
    """
    if not recover_staging(pending, last_good):
        log("ERROR", "SYS", "Configuration staging files could not be recovered; nothing is armed.")
        return False
    current = persisted(config, defaults, excluded)
    target = persisted(config, defaults, excluded, new_data)
    if not atomic(last_good, current): return False
    if not atomic(pending, target):
        try: os.remove(last_good)
        except OSError: pass
        return False
    if not atomic(config["config_file"], target):
        for path in (pending, last_good):
            try: os.remove(path)
            except OSError: pass
        return False
    config.update(new_data)
    return True


def _read_dict(path):
    """The JSON object stored at path, or None when it is missing, unreadable or no object."""
    try:
        with open(path, "r") as fh:
            value = ujson.load(fh)
    except (OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def recover_staging(pending, last_good):
    """Recover both transaction files after a power cut mid-rename; never raises.

    False means a recovery step failed (for example EIO on a rename): the caller
    keeps the change open and blocked, the files stay where they are.
    """
    try:
        recover_atomic(pending)
        recover_atomic(last_good)
    except Exception:
        return False
    return True


def changed_keys(keys, excluded, candidate, previous):
    """Keys of the change set: in previous and different in candidate (all of them without one)."""
    return [key for key in keys if key not in excluded and key in previous
            and (candidate is None or candidate.get(key) != previous[key])]


def change_landed(config, keys, excluded, pending, last_good):
    """True when every changed key already has its candidate value in config.

    Without a readable pending or last-good file there is no change set to
    compare against; that counts as landed.
    """
    previous, candidate = _read_dict(last_good), _read_dict(pending)
    if previous is None or candidate is None:
        return True
    for key in changed_keys(keys, excluded, candidate, previous):
        if config.get(key) != candidate.get(key):
            return False
    return True


def finish(pending, last_good, log):
    """End the transaction: pending first, last-good only once pending is gone."""
    try:
        os.remove(pending)
    except OSError as exc:
        if pending_active(pending):
            log("ERROR", "SYS", "Removing %s failed: %s" % (pending, exc))
            return False
    try: os.remove(last_good)
    except OSError: pass
    return True


def complete_rollback(config, defaults, excluded, keys, last_good, pending, atomic, log,
                      full=False):
    """Restore the change set from last-good into config.json; idempotent.

    Without a pending file there is nothing left to do. The protection files are
    removed only after the restored values were written and read back. full
    restores the whole last-good configuration (confirmed and complete), for a
    config.json that could not be read, so RAM holds only defaults.
    """
    if not recover_staging(pending, last_good):
        return False
    if not pending_active(pending):
        return True
    previous = _read_dict(last_good)
    if previous is None:
        log("ERROR", "SYS", "Last-good configuration unreadable; the unconfirmed configuration stays active.")
        return finish(pending, last_good, log)
    if full:
        values = {key: previous[key] for key in defaults
                  if key not in excluded and key in previous}
    else:
        restore = changed_keys(keys, excluded, _read_dict(pending), previous)
        values = {key: previous[key] for key in restore}
    if not atomic(config["config_file"], persisted(config, defaults, excluded, values)):
        return False
    written = _read_dict(config["config_file"])
    if written is None or any(written.get(key) != value for key, value in values.items()):
        log("ERROR", "SYS", "Restored configuration did not read back from %s." % config["config_file"])
        return False
    for key, value in values.items():
        config[key] = coerce(defaults, key, value)
    log("WARN", "SYS", "New network configuration rejected; previous configuration restored.")
    return finish(pending, last_good, log)


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
