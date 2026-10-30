# SPDX-License-Identifier: AGPL-3.0-only
"""Boot health and planned-reset bookkeeping (MicroPython compatible)."""
import os
import ujson


def backoff_delay(attempt, base, cap):
    shift = attempt - 1
    if shift < 1:
        return base
    if shift > 20:
        return cap
    delay = base * (1 << shift)
    return cap if delay > cap else delay


def reset_cause_name(code, causes):
    return causes.get(code, "UNKNOWN(%s)" % code)


def mark_planned_reset(path):
    try:
        with open(path, "w") as fh:
            fh.write("1")
    except OSError:
        pass


def _consume_planned_reset(path):
    try:
        os.stat(path)
    except OSError:
        return False
    try:
        os.remove(path)
    except OSError:
        pass
    return True


def reset_boot_count(path):
    """Remove the counter together with its .tmp and .previous copies.

    A power cut during a write leaves those copies behind; the next boot is a
    PWRON, which resets the counter. If the copies stayed, the next crash boot
    would recover a stale count from them and could enter safe mode wrongly.
    """
    for stale in (path, path + ".tmp", path + ".previous"):
        try:
            os.remove(stale)
        except OSError:
            pass


def _read_count(path):
    """Return the stored count, or None unless the file is a JSON object whose
    "n" is an int (not a bool) >= 0. Selection, recovery and reading share
    this rule."""
    try:
        with open(path, "r") as fh:
            value = ujson.load(fh)
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(value, dict):
        return None
    count = value.get("n")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return None
    return count


def _recover_count(path):
    """Restore the counter after a power cut during its atomic replacement.

    Returns (count, recovered); count is None when no valid copy exists.
    Own recovery instead of cfg_store.recover_atomic: that one keeps any
    valid JSON object, so a target {} would win over a valid .previous.
    recovered is False only when renaming a valid copy into place failed;
    then every file stays as it is and the caller must not write, because
    the write would delete the .previous that still holds the count.
    """
    temporary, previous = path + ".tmp", path + ".previous"
    count = _read_count(path)
    if count is not None:
        for stale in (temporary, previous):
            try:
                os.remove(stale)
            except OSError:
                pass
        return count, True
    for candidate, other in ((temporary, previous), (previous, temporary)):
        count = _read_count(candidate)
        if count is None:
            continue
        try:
            os.remove(path)
        except OSError:
            pass
        try:
            os.rename(candidate, path)
        except OSError:
            return count, False
        try:
            os.remove(other)
        except OSError:
            pass
        return count, True
    return None, True


def bump_boot_count(cause, bootcount_path, planned_path, crash_causes, dump):
    """Count a crash boot and return the new count (0 after a harmless cause).

    dump(path, data) -> bool writes atomically; it is injected so that this
    module stays free of cfg_store. The returned count is valid even when
    saving fails (dump logs the failure); after a failed recovery nothing is
    written, so the next boot counts on from the surviving copy.
    """
    if _consume_planned_reset(planned_path):
        reset_boot_count(bootcount_path)
        return 0
    if cause is not None and cause not in crash_causes:
        reset_boot_count(bootcount_path)
        return 0
    count, recovered = _recover_count(bootcount_path)
    count = (count or 0) + 1
    if recovered:
        dump(bootcount_path, {"n": count})
    return count


def safe_mode_active(boot_count, threshold):
    return boot_count >= threshold
