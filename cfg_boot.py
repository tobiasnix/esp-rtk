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
    try:
        os.remove(path)
    except OSError:
        pass


def bump_boot_count(cause, bootcount_path, planned_path, crash_causes):
    if _consume_planned_reset(planned_path):
        reset_boot_count(bootcount_path)
        return 0
    if cause is not None and cause not in crash_causes:
        reset_boot_count(bootcount_path)
        return 0
    try:
        with open(bootcount_path, "r") as fh:
            count = int(ujson.load(fh).get("n", 0))
    except (OSError, ValueError, TypeError, AttributeError):
        count = 0
    count += 1
    try:
        with open(bootcount_path, "w") as fh:
            ujson.dump({"n": count}, fh)
    except OSError:
        pass
    return count


def safe_mode_active(boot_count, threshold):
    return boot_count >= threshold
