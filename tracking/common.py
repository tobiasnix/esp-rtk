# SPDX-License-Identifier: AGPL-3.0-only
"""Survey limits, file names, schema versions and the time and profile helpers the tracking modules share."""
import time
import ujson
from pointstore import _expand


TRACK_FILE = "tracking"
TRACK_INDEX_FILE = "tracking.index.json"
TRACK_CHECKPOINT_FILE = "tracking.checkpoint.json"
CHECKPOINT_BATCH_EVENTS = 256
# Accepting a socket, reading its request and draining its response require
# several scheduler turns. A one-millisecond yield after a multi-second flash
# commit can expire before that chain finishes, putting the next flash commit
# ahead of the response. Keep a service window between the two long commits.
CHECKPOINT_IO_SERVICE_MS = 100
# A restore pauses this long after each long staging write and after its
# publication, a JSON restore also around upgrade and validation, so HTTP
# gets service windows (_SERVICE_PAUSE).
RESTORE_SERVICE_MS = 10
FLASH_RECHECK_WRITES = 128
# Flash an archive needs per line vertex, before the 25 % LittleFS margin.
# ujson writes ", " and ": " separators: an averaged vertex row takes about
# 520 bytes with typical frame and mount texts and 710 bytes with the longest
# configurable ones (80 characters each for frame, epoch and mount).
ARCHIVE_VERTEX_BYTES = 768
SCHEMA_VERSION = 5
LEGACY_SCHEMA_VERSIONS = (2, 3, 4)
SEGMENT_MAX_BYTES = 131072
SEGMENT_HARD_LIMIT_BYTES = 524288
# Space that may still be consumed between reserve checks: one new journal and
# point-store segment plus both bounded persistent error-log generations.
OPERATIONAL_FLASH_HEADROOM_BYTES = (2 * SEGMENT_MAX_BYTES) + (2 * 8192)
MAX_PROJECTS = 20
MAX_FEATURES = 5000
_ENOENT = 2  # errno of a missing file, on MicroPython and POSIX hosts
TARGET_MAX_FEATURES = 10000
MIN_FLASH_RESERVE_PERCENT = 20
MAX_BACKUP_BYTES = 32 * 1024 * 1024
MAX_LINES_PER_PROJECT = 500
MAX_PROPERTIES = 32
MAX_LINE_GAP_M = 2.0
MAX_VERTICAL_STEP_M = 0.50
# "stakeout" points come only from a stakeout confirmation (add_asset's stakeout argument).
OBJECT_TYPES = ("tap", "valve", "hydrant", "branch", "transition", "control", "tree",
                "repair", "line_start", "line_end", "other", "stakeout")
_STAKEOUT_ONLY = "Stakeout points come only from a stakeout confirmation."
MEASUREMENT_GATE_KEYS = (
    "measurement_required_fix", "measurement_min_satellites",
    "measurement_max_hdop", "measurement_max_correction_age_sec",
    "measurement_max_horizontal_spread_m",
    "measurement_max_gst_horizontal_sigma_m",
    "measurement_max_gst_vertical_sigma_m",
    "measurement_quality_window_samples")


# Stored measurement fields the map payload (full and incremental) carries per
# point; _map_measurement adds the derived DERIVED_TIME_KEYS to them.
MAP_MEASUREMENT_KEYS = ("fix_quality", "fix_status", "satellites", "hdop",
                        "correction_age_sec", "station_id", "accuracy_observation",
                        "receiver_accuracy", "quality", "precision", "profile_id",
                        "geoid_sep_mm", "alt_ellipsoid_mm", "antenna_height_mm")

# Time base: every stored timestamp (recorded_at, created_at, finished_at, the
# events' "at", device_time) counts seconds since 2000-01-01T00:00:00Z, the
# MicroPython epoch, on every platform. CPython's epoch is 1970, so the host
# subtracts the difference; it is detected once here.
_EPOCH_OFFSET = 946684800 if time.gmtime(0)[0] == 1970 else 0
# Fields derived from recorded_at on output; never stored, stripped on restore.
DERIVED_TIME_KEYS = ("recorded_at_utc", "clock_unsynced")


def _now_seconds():
    """The current time in seconds since 2000-01-01T00:00:00Z."""
    return int(time.time()) - _EPOCH_OFFSET


def _utc_tuple(seconds):
    """UTC calendar fields of a 2000-based timestamp (index only: MicroPython returns a tuple)."""
    return time.gmtime(seconds + _EPOCH_OFFSET)


def _utc_fields(value):
    """(ISO 8601 UTC text or None, clock_unsynced) of a stored recorded_at.

    A timestamp is valid when it is an int (not bool) in 0 <= value < 2**40;
    the clock counts as synchronised only for years 2024-2099, the window
    gnss.clock_is_valid accepts.
    """
    if not _valid_timestamp(value):
        return None, True
    try:
        fields = _utc_tuple(value)
        text = "%04d-%02d-%02dT%02d:%02d:%02dZ" % tuple(fields[:6])
    except (OverflowError, OSError, ValueError):
        return None, True
    return text, not 2024 <= fields[0] <= 2099


def _valid_timestamp(value):
    """True for a stored timestamp: an int (not bool) in 0 <= value < 2**40."""
    return type(value) is int and 0 <= value < 2 ** 40


def _add_time_fields(measurement):
    """Add recorded_at_utc and clock_unsynced to measurement in place; return it.

    Callers pass a fresh copy (an expanded measurement), never stored data.
    """
    measurement["recorded_at_utc"], measurement["clock_unsynced"] = _utc_fields(
        measurement.get("recorded_at"))
    return measurement


def _sample_counts(measurement):
    """(samples, rtk_fixed_samples, gst_samples) of a stored measurement.

    An averaged point carries its counts. A point that was not averaged is one
    sample: RTK fixed only with fix quality 4, a GST sample only when it holds
    a GST sigma. The CSV reads its count columns here instead of defaulting
    to 1; GeoJSON carries the stored objects unchanged and adds no counts.
    """
    observed = measurement.get("accuracy_observation") or {}
    receiver = measurement.get("receiver_accuracy") or {}
    if "samples" in observed:
        samples, fixed = observed["samples"], observed.get("rtk_fixed_samples")
    else:
        quality = measurement.get("fix_quality")
        samples = 1
        fixed = 1 if (quality == 4 if quality is not None else
                      measurement.get("fix_status") == "RTK_FIXED") else 0
    if "samples" in receiver:
        gst = receiver["samples"]
    else:
        gst = 1 if any(receiver.get(key) is not None for key in (
            "horizontal_sigma_m", "semi_major_sigma_m", "altitude_sigma_m")) else 0
    return samples, fixed, gst


def _age_sec(device_time, recorded_at):
    """Seconds since recorded_at, or None when it is not an integer timestamp."""
    return max(0, device_time - recorded_at) if type(recorded_at) is int else None


def _strip_derived_fields(measurement):
    """Remove exactly the derived time fields in place; other keys stay."""
    if isinstance(measurement, dict):
        for key in DERIVED_TIME_KEYS:
            measurement.pop(key, None)
    return measurement


def _canonical_json(value):
    """JSON text with sorted object keys, the same on CPython and MicroPython.

    MicroPython's ujson has no sort_keys and writes keys in the dict's hash
    order, so only scalars and lists go through it; objects are assembled here.
    The separators match CPython's json.dumps(sort_keys=True), which keeps every
    ID a host ever derived unchanged.
    """
    if isinstance(value, dict):
        return "{" + ", ".join(
            ujson.dumps(str(key)) + ": " + _canonical_json(value[key])
            for key in sorted(value)) + "}"
    return ujson.dumps(value)


def _profile_id(limits):
    """Stable ID without depending on hashlib availability on the MCU."""
    value = 2166136261
    for char in _canonical_json(limits):
        value = ((value ^ ord(char)) * 16777619) & 0xffffffff
    return "profile-%08x" % value


def _profile_id_valid(profile_id, limits):
    """Whether a stored profile ID may be trusted for these limits.

    A profile ID derived by this code matches _profile_id(limits). Up to V15.9.0
    a MicroPython device hashed the limits in the dict's hash order, so the same
    limits carry other IDs in existing journals, checkpoints and backups. That
    order cannot be rebuilt on another platform, so a legacy ID passes on its
    shape alone and is never rewritten. The price: a doctored ID of that shape is
    no longer detected; the stored limits are the authoritative data.
    """
    if profile_id == _profile_id(limits):
        return True
    return (isinstance(profile_id, str) and len(profile_id) == 16 and
            profile_id.startswith("profile-") and
            all(char in "0123456789abcdef" for char in profile_id[8:]))


def _expanded_archive_record(value):
    if isinstance(value,list) and len(value)==5 and value[0]=="v":
        return {"record":"vertex","project_id":value[1],"line_id":value[2],
                "coordinate":value[3],"measurement":_expand(value[4])}
    if isinstance(value,list) and len(value)==3 and value[0]=="a":
        point=dict(value[2]);point["measurement"]=_expand(point.get("measurement"))
        return {"record":"asset","project_id":value[1],"point":point}
    return value
