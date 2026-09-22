# SPDX-License-Identifier: AGPL-3.0-only
"""Crash-tolerant field mapping with lines and related asset points."""
import math
import os
import time
import ujson
from pointstore import (FlashPointStore, FlashSequence, POINT_WRITE_BATCH,
                        _compact, _expand)
try:
    import hashlib
except ImportError:  # pragma: no cover
    import uhashlib as hashlib
from cfg import CONFIG, DEFAULT_CONFIG


TRACK_FILE = "tracking"
TRACK_INDEX_FILE = "tracking.index.json"
TRACK_CHECKPOINT_FILE = "tracking.checkpoint.json"
INDEX_BATCH_EVENTS = 16
CHECKPOINT_BATCH_EVENTS = 256
# Accepting a socket, reading its request and draining its response require
# several scheduler turns. A one-millisecond yield after a multi-second flash
# commit can expire before that chain finishes, putting the next flash commit
# ahead of the response. Keep a service window between the two long commits.
CHECKPOINT_IO_SERVICE_MS = 100
FLASH_RECHECK_WRITES = 128
SCHEMA_VERSION = 4
LEGACY_SCHEMA_VERSIONS = (2, 3)
SEGMENT_MAX_BYTES = 131072
SEGMENT_HARD_LIMIT_BYTES = 524288
# Space that may still be consumed between reserve checks: one new journal and
# point-store segment plus both bounded persistent error-log generations.
OPERATIONAL_FLASH_HEADROOM_BYTES = (2 * SEGMENT_MAX_BYTES) + (2 * 8192)
MAX_PROJECTS = 20
MAX_FEATURES = 5000
TARGET_MAX_FEATURES = 10000
MIN_FLASH_RESERVE_PERCENT = 20
MAX_BACKUP_BYTES = 32 * 1024 * 1024
MAX_LINES_PER_PROJECT = 500
MAX_PROPERTIES = 32
MIN_SATELLITES = 10
MAX_HDOP = 1.5
MAX_CORRECTION_AGE_SEC = 5.0
MAX_HORIZONTAL_SPREAD_M = 0.05
MAX_GST_HORIZONTAL_SIGMA_M = 0.05
MAX_GST_VERTICAL_SIGMA_M = 0.10
MAX_LINE_GAP_M = 2.0
MAX_VERTICAL_STEP_M = 0.50
OBJECT_TYPES = ("tap", "valve", "hydrant", "branch", "transition", "control", "tree",
                "repair", "line_start", "line_end", "other")
MEASUREMENT_GATE_KEYS = (
    "measurement_required_fix", "measurement_min_satellites",
    "measurement_max_hdop", "measurement_max_correction_age_sec",
    "measurement_max_horizontal_spread_m",
    "measurement_max_gst_horizontal_sigma_m",
    "measurement_max_gst_vertical_sigma_m",
    "measurement_quality_window_samples")


def _profile_id(limits):
    """Stable ID without depending on hashlib availability on the MCU."""
    try:
        encoded = ujson.dumps(limits, sort_keys=True)
    except TypeError:
        encoded = ujson.dumps(dict(sorted(limits.items())))
    value = 2166136261
    for char in encoded:
        value = ((value ^ ord(char)) * 16777619) & 0xffffffff
    return "profile-%08x" % value


def _expanded_archive_record(value):
    if isinstance(value,list) and len(value)==5 and value[0]=="v":
        return {"record":"vertex","project_id":value[1],"line_id":value[2],
                "coordinate":value[3],"measurement":_expand(value[4])}
    if isinstance(value,list) and len(value)==3 and value[0]=="a":
        point=dict(value[2]);point["measurement"]=_expand(point.get("measurement"))
        return {"record":"asset","project_id":value[1],"point":point}
    return value


def measurement_gate(defaults=False):
    source = DEFAULT_CONFIG if defaults else CONFIG
    return {key.replace("measurement_", ""): source[key]
            for key in MEASUREMENT_GATE_KEYS}


def validate_measurement_gate(values):
    if not isinstance(values, dict):
        return None, "Measurement gate must be an object."
    allowed = set(key.replace("measurement_", "") for key in MEASUREMENT_GATE_KEYS)
    if set(values) != allowed:
        return None, "All measurement gate fields are required."
    required = values.get("required_fix")
    if required not in ("RTK_FIXED", "RTK_FLOAT", "DGPS"):
        return None, "required_fix must be RTK_FIXED, RTK_FLOAT or DGPS."
    ranges = {"min_satellites": (4, 60), "max_hdop": (0.1, 20.0),
              "max_correction_age_sec": (0.1, 120.0),
              "max_horizontal_spread_m": (0.001, 10.0),
              "max_gst_horizontal_sigma_m": (0.001, 10.0),
              "max_gst_vertical_sigma_m": (0.001, 20.0),
              "quality_window_samples": (3, 30)}
    clean = {"measurement_required_fix": required}
    for key, bounds in ranges.items():
        value = values.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None, "%s must be a number." % key
        value = float(value)
        if value != value or not bounds[0] <= value <= bounds[1]:
            return None, "%s must be between %s and %s." % (
                key, bounds[0], bounds[1])
        clean["measurement_" + key] = int(value) if key in (
            "min_satellites", "quality_window_samples") else value
    return clean, None


def _clean_text(value, maximum=80):
    value = str(value or "").strip()
    if len(value) > maximum:
        raise ValueError("Text is too long.")
    return value


def _finite_number(value, minimum=None, maximum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    number = float(value)
    if number != number or abs(number) == float("inf"):
        return False
    return ((minimum is None or number >= minimum) and
            (maximum is None or number <= maximum))


def _validate_coordinate(value, field="coordinate"):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError("%s must contain longitude, latitude and altitude." % field)
    if not _finite_number(value[0], -180, 180) or not _finite_number(value[1], -90, 90):
        raise ValueError("%s contains an invalid longitude or latitude." % field)
    if len(value) == 3 and not _finite_number(value[2], -20000, 100000):
        raise ValueError("%s contains an invalid altitude." % field)


def _validate_properties(value):
    if not isinstance(value, dict) or len(value) > MAX_PROPERTIES:
        raise ValueError("Line properties are invalid or too large.")
    for key, item in value.items():
        if not isinstance(key, str) or not key or len(key) > 80:
            raise ValueError("A line property name is invalid.")
        if isinstance(item, str) and len(item) > 500:
            raise ValueError("A line property value is too long.")
        if item is not None and not isinstance(item, (str, int, float, bool)):
            raise ValueError("Line properties must contain scalar values.")


def _validate_measurement(value):
    if not isinstance(value, dict):
        raise ValueError("A measurement is invalid.")
    for required in ("recorded_at", "fix_status", "satellites", "hdop", "quality"):
        if value.get(required) is None:
            raise ValueError("Measurement field %s is required." % required)
    for key in ("satellites", "hdop", "correction_age_sec", "recorded_at"):
        item = value.get(key)
        if item is not None and not _finite_number(item):
            raise ValueError("Measurement field %s is invalid." % key)
    for key in ("fix_status", "station_id", "source"):
        item = value.get(key)
        if item is not None and (not isinstance(item, str) or len(item) > 80):
            raise ValueError("Measurement field %s is invalid." % key)
    quality = value.get("quality")
    if quality is not None:
        if not isinstance(quality, dict) or not isinstance(quality.get("accepted"), bool):
            raise ValueError("Measurement quality is invalid.")
        reasons = quality.get("reasons", [])
        if (not isinstance(reasons, list) or len(reasons) > 32 or
                any(not isinstance(reason, str) or len(reason) > 80 for reason in reasons)):
            raise ValueError("Measurement quality reasons are invalid.")
    for key in ("accuracy_observation", "receiver_accuracy"):
        details = value.get(key)
        if details is not None and (not isinstance(details, dict) or len(details) > 32):
            raise ValueError("Measurement accuracy details are invalid.")


def validate_backup(payload, copy_result=True):
    """Validate and deep-copy a complete survey backup."""
    if (not isinstance(payload, dict) or payload.get("kind") != "survey_backup"
            or payload.get("schema_version") != SCHEMA_VERSION
            or not isinstance(payload.get("projects"), list)):
        raise ValueError("Survey backup is invalid.")
    encoded = None
    if copy_result:
        try:
            encoded = ujson.dumps(payload)
        except (ValueError, TypeError):
            raise ValueError("Survey backup is not serializable.")
        if len(encoded.encode("utf-8")) > MAX_BACKUP_BYTES:
            raise ValueError("Survey backup exceeds the storage limit.")
    projects = payload["projects"]
    profiles = payload.get("profiles", [])
    if not isinstance(profiles, list):
        raise ValueError("Survey backup profiles are invalid.")
    profile_ids = set()
    for profile in profiles:
        if (not isinstance(profile, dict) or not isinstance(profile.get("id"), str)
                or profile["id"] in profile_ids or not isinstance(profile.get("limits"), dict)):
            raise ValueError("A measurement profile is invalid or duplicated.")
        profile_ids.add(profile["id"])
    if len(projects) > MAX_PROJECTS:
        raise ValueError("Survey backup contains too many projects.")
    ids, project_ids, line_ids, features = set(), set(), set(), 0
    for project in projects:
        if not isinstance(project, dict):
            raise ValueError("A survey project is invalid.")
        project_id = project.get("id")
        if not isinstance(project_id, str) or not project_id or len(project_id) > 80 or project_id in ids:
            raise ValueError("A project ID is invalid or duplicated.")
        ids.add(project_id); project_ids.add(project_id)
        _clean_text(project.get("name")); _clean_text(project.get("description", ""), 240)
        lines, points = project.get("lines"), project.get("points")
        if not isinstance(lines, list) or not isinstance(points, list) or len(lines) > MAX_LINES_PER_PROJECT:
            raise ValueError("Project lines or points are invalid.")
        project_line_ids = set()
        for line in lines:
            if not isinstance(line, dict):
                raise ValueError("A survey line is invalid.")
            line_id = line.get("id")
            if not isinstance(line_id, str) or not line_id or len(line_id) > 80 or line_id in ids:
                raise ValueError("A line ID is invalid or duplicated.")
            ids.add(line_id); line_ids.add(line_id); project_line_ids.add(line_id)
            _clean_text(line.get("name"))
            if line.get("state") not in ("recording", "paused", "finished"):
                raise ValueError("A survey line state is invalid.")
            if line.get("recording_mode", "survey") not in ("survey", "route"):
                raise ValueError("A survey line recording mode is invalid.")
            coordinates, measurements = line.get("coordinates"), line.get("measurements")
            if not isinstance(coordinates, list) or not isinstance(measurements, list) or len(coordinates) != len(measurements):
                raise ValueError("Line coordinates and measurements do not match.")
            features += len(coordinates)
            for coordinate in coordinates: _validate_coordinate(coordinate)
            for measurement in measurements: _validate_measurement(measurement)
            for measurement in measurements:
                if measurement.get("profile_id") and measurement["profile_id"] not in profile_ids:
                    raise ValueError("A measurement references an unknown profile.")
            _validate_properties(line.get("properties", {}))
        for point in points:
            if not isinstance(point, dict):
                raise ValueError("A survey asset is invalid.")
            point_id = point.get("id")
            if not isinstance(point_id, str) or not point_id or len(point_id) > 80 or point_id in ids:
                raise ValueError("An asset ID is invalid or duplicated.")
            ids.add(point_id); features += 1
            if point.get("object_type") not in OBJECT_TYPES:
                raise ValueError("An asset type is invalid.")
            _clean_text(point.get("name", "")); _clean_text(point.get("note", ""), 500)
            _validate_coordinate(point.get("coordinate"))
            projected = point.get("projected_coordinate")
            if projected is not None: _validate_coordinate(projected, "projected_coordinate")
            if point.get("line_id") is not None and point.get("line_id") not in project_line_ids:
                raise ValueError("An asset references an unknown line.")
            _validate_measurement(point.get("measurement"))
            if (point["measurement"].get("profile_id") and
                    point["measurement"]["profile_id"] not in profile_ids):
                raise ValueError("A measurement references an unknown profile.")
    if features > MAX_FEATURES:
        raise ValueError("Survey backup contains too many features.")
    active_project = payload.get("active_project_id")
    active_line = payload.get("active_line_id")
    if active_project is not None and active_project not in project_ids:
        raise ValueError("The active project does not exist.")
    if active_line is not None and active_line not in line_ids:
        raise ValueError("The active line does not exist.")
    if active_line is not None:
        active_matches = [(project["id"], line.get("state")) for project in projects
                          for line in project["lines"] if line["id"] == active_line]
        owners = [owner for owner, _state in active_matches]
        if owners != [active_project]:
            raise ValueError("The active line does not belong to the active project.")
        if active_matches[0][1] == "finished":
            raise ValueError("A finished line cannot be active.")
    distance = payload.get("auto_distance_m", 1.0)
    if not _finite_number(distance, 0, 100) or (distance and distance < 0.2):
        raise ValueError("The automatic point distance is invalid.")
    return ujson.loads(encoded) if copy_result else payload


def upgrade_restore_backup(payload, copy_result=True):
    """Convert an explicitly selected schema-1 backup to schema 2.

    Schema 1 stored projected asset coordinates as longitude/latitude pairs.
    Schema 2 consistently uses three-dimensional coordinates; the measured MSL
    altitude is the only lossless altitude available for that projection.
    """
    if not isinstance(payload, dict) or payload.get("schema_version") not in (1, 2, 3):
        return payload
    if payload.get("kind") != "survey_backup" or not isinstance(payload.get("projects"), list):
        raise ValueError("Legacy survey backup is invalid.")
    if copy_result:
        try:
            upgraded = ujson.loads(ujson.dumps(payload))
        except (ValueError, TypeError):
            raise ValueError("Legacy survey backup is not serializable.")
    else:
        # HTTP restore payloads already came from ujson.loads and are private to
        # this request. Avoid two full encode/decode copies on the small MCU heap.
        upgraded = payload
    source_schema = upgraded.get("schema_version")
    upgraded["schema_version"] = SCHEMA_VERSION
    upgraded.setdefault("profiles", [])
    profiles = {}
    for project in upgraded["projects"]:
        if not isinstance(project, dict):
            continue
        for point in project.get("points", []):
            if not isinstance(point, dict):
                continue
            projected = point.get("projected_coordinate")
            measured = point.get("coordinate")
            if (source_schema == 1 and isinstance(projected, list) and len(projected) == 2 and
                    isinstance(measured, list) and len(measured) == 3):
                point["projected_coordinate"] = projected + [measured[2]]
        measurements = [m for line in project.get("lines", [])
                        for m in line.get("measurements", [])]
        measurements += [p.get("measurement") for p in project.get("points", [])]
        for measurement in measurements:
            if not isinstance(measurement, dict):
                continue
            quality = measurement.get("quality") or {}
            limits = quality.get("limits")
            if limits:
                profile_id = _profile_id(limits)
                measurement["profile_id"] = profile_id
                profiles[profile_id] = {"id": profile_id, "version": 1,
                                        "limits": limits}
    upgraded["profiles"] = list(profiles.values())
    return upgraded


def _distance_m(a, b):
    if not a or not b:
        return 0.0
    lat1, lat2 = math.radians(a[1]), math.radians(b[1])
    dlat = lat2 - lat1
    dlon = math.radians(b[0] - a[0])
    x = dlon * math.cos((lat1 + lat2) / 2)
    return 6371000.0 * math.sqrt(x * x + dlat * dlat)


def average_fixes(fixes):
    """Return a mean fix plus observed repeatability statistics."""
    usable = [fix for fix in fixes if fix and fix.get("lat") is not None
              and fix.get("lon") is not None and fix.get("alt") is not None]
    if not usable:
        raise ValueError("No GNSS position is available.")
    count = len(usable)
    mean = dict(usable[-1])
    mean["lat"] = sum(float(fix["lat"]) for fix in usable) / count
    mean["lon"] = sum(float(fix["lon"]) for fix in usable) / count
    mean["alt"] = sum(float(fix["alt"]) for fix in usable) / count
    mean["altitude_msl_m"] = mean["alt"]
    centre = [mean["lon"], mean["lat"], mean["alt"]]
    horizontal = [_distance_m(centre, [fix["lon"], fix["lat"],
                                      fix["alt"]]) for fix in usable]
    vertical = [abs(float(fix["alt"]) - mean["alt"]) for fix in usable]
    required_fix = measurement_gate()["required_fix"]
    accepted_qualities = {"RTK_FIXED": (4,), "RTK_FLOAT": (4, 5),
                          "DGPS": (2, 4, 5)}[required_fix]
    mean["accuracy_observation"] = {
        "samples": count,
        "rtk_fixed_samples": sum(1 for fix in usable if fix.get("qual") == 4),
        "accepted_fix_samples": sum(1 for fix in usable
                                    if fix.get("qual") in accepted_qualities),
        "horizontal_mean_deviation_m": round(sum(horizontal) / count, 4),
        "horizontal_max_deviation_m": round(max(horizontal), 4),
        "vertical_mean_deviation_m": round(sum(vertical) / count, 4),
        "vertical_max_deviation_m": round(max(vertical), 4),
    }
    estimates = [fix.get("receiver_accuracy") for fix in usable
                 if fix.get("receiver_accuracy")]
    horizontal = [item.get("horizontal_sigma_m", item.get("semi_major_sigma_m"))
                  for item in estimates
                  if item.get("horizontal_sigma_m", item.get("semi_major_sigma_m"))
                  not in (None, 0)]
    vertical = [item.get("altitude_sigma_m") for item in estimates
                if item.get("altitude_sigma_m") not in (None, 0)]
    mean["receiver_accuracy"] = {
        "source": "NMEA_GST", "samples": len(estimates),
        "expected_samples": count,
        "horizontal_sigma_mean_m": (round(sum(horizontal) / len(horizontal), 4)
                                    if horizontal else None),
        "horizontal_sigma_max_m": round(max(horizontal), 4) if horizontal else None,
        "vertical_sigma_mean_m": (round(sum(vertical) / len(vertical), 4)
                                  if vertical else None),
        "vertical_sigma_max_m": round(max(vertical), 4) if vertical else None,
    }
    return mean


def quality_check(fix):
    limits = measurement_gate()
    reasons = []
    if not fix or fix.get("lat") is None or fix.get("lon") is None:
        reasons.append("no_position")
    qual = int((fix or {}).get("qual", 0))
    accepted_qualities = {"RTK_FIXED": (4,), "RTK_FLOAT": (4, 5),
                          "DGPS": (2, 4, 5)}[limits["required_fix"]]
    if qual not in accepted_qualities:
        reasons.append("rtk_fixed_required" if limits["required_fix"] == "RTK_FIXED"
                       else "required_fix_not_met")
    if int((fix or {}).get("sats", 0)) < limits["min_satellites"]:
        reasons.append("too_few_satellites")
    hdop = (fix or {}).get("hdop")
    if hdop is None or float(hdop) > limits["max_hdop"]:
        reasons.append("hdop_too_high")
    if (fix or {}).get("alt") is None:
        reasons.append("altitude_required")
    age = (fix or {}).get("correction_age_sec")
    if age is not None and float(age) > limits["max_correction_age_sec"]:
        reasons.append("corrections_too_old")
    observed = (fix or {}).get("accuracy_observation") or {}
    if observed:
        if (limits["required_fix"] == "RTK_FIXED" and
                observed.get("rtk_fixed_samples", 0) != observed.get("samples", 0)):
            reasons.append("not_all_samples_rtk_fixed")
        if (observed.get("accepted_fix_samples") is not None and
                observed.get("accepted_fix_samples") != observed.get("samples")):
            reasons.append("required_fix_not_met")
        if observed.get("horizontal_max_deviation_m", 999) > limits["max_horizontal_spread_m"]:
            reasons.append("horizontal_spread_too_high")
    receiver = (fix or {}).get("receiver_accuracy") or {}
    horizontal_sigma = receiver.get("horizontal_sigma_max_m",
                                    receiver.get("horizontal_sigma_m",
                                                 receiver.get("semi_major_sigma_m")))
    vertical_sigma = receiver.get("vertical_sigma_max_m",
                                  receiver.get("altitude_sigma_m"))
    if not receiver or horizontal_sigma is None or vertical_sigma is None:
        reasons.append("gst_required")
    else:
        if receiver.get("expected_samples") is not None and receiver.get("samples") != receiver.get("expected_samples"):
            reasons.append("not_all_samples_have_gst")
        if float(horizontal_sigma) > limits["max_gst_horizontal_sigma_m"]:
            reasons.append("gst_horizontal_error_too_high")
        if float(vertical_sigma) > limits["max_gst_vertical_sigma_m"]:
            reasons.append("gst_vertical_error_too_high")
    limits["fix_quality"] = limits.pop("required_fix")
    return {"accepted": not reasons, "reasons": reasons, "limits": limits}


def project_to_line(coordinates, point):
    """Project a point onto a polyline and return chainage and signed offset."""
    if not coordinates:
        return None
    if len(coordinates) == 1:
        return {"chainage_m": round(_distance_m(coordinates[0], point), 3),
                "lateral_offset_m": 0.0, "side": "on_line",
                "projected_coordinate": list(point[:3])}
    origin_lat = math.radians(point[1])
    scale_x = 6371000.0 * math.cos(origin_lat) * math.pi / 180
    scale_y = 6371000.0 * math.pi / 180
    cumulative, best = 0.0, None
    for index, (a, b) in enumerate(zip(coordinates, coordinates[1:])):
        ax, ay = (a[0] - point[0]) * scale_x, (a[1] - point[1]) * scale_y
        bx, by = (b[0] - point[0]) * scale_x, (b[1] - point[1]) * scale_y
        dx, dy = bx - ax, by - ay
        length_sq = dx * dx + dy * dy
        if not length_sq:
            continue
        raw_t = -(ax * dx + ay * dy) / length_sq
        t = max(0.0, raw_t)
        if index < len(coordinates) - 2:
            t = min(1.0, t)
        px, py = ax + t * dx, ay + t * dy
        distance = math.sqrt(px * px + py * py)
        segment_length = math.sqrt(length_sq)
        cross = dx * (-ay) - dy * (-ax)
        candidate = {"chainage_m": cumulative + t * segment_length,
                     "lateral_offset_m": distance,
                     "side": "left" if cross > 0 else "right" if cross < 0 else "on_line",
                     "projected_coordinate": [point[0] + px / scale_x,
                                              point[1] + py / scale_y,
                                              float(a[2]) + t * (float(b[2]) - float(a[2]))]}
        if best is None or distance < best[0]:
            best = (distance, candidate)
        cumulative += segment_length
    if best is None:
        return None
    result = best[1]
    result["chainage_m"] = round(result["chainage_m"], 3)
    result["lateral_offset_m"] = round(result["lateral_offset_m"], 3)
    return result


class Tracker:
    def __init__(self, path=TRACK_FILE, autoload=True, max_features=None,
                 point_store_autoload=True):
        self.path = path
        # None intentionally follows the module limit at call time; host tests and
        # release gates patch MAX_FEATURES. Only isolated benchmark stores pin an
        # instance-specific limit.
        self.max_features = (None if max_features is None else max(1, int(max_features)))
        self.index_path = (TRACK_INDEX_FILE if path == TRACK_FILE
                           else path + ".index.json")
        self.checkpoint_path = (TRACK_CHECKPOINT_FILE if path == TRACK_FILE
                                else path + ".checkpoint.json")
        self.segment_prefix = path + ".segment-"
        self.point_path = path + ".points-v1.jsonl"
        self.point_store = FlashPointStore(self.point_path, autoload=point_store_autoload)
        self._point_store_loaded = point_store_autoload
        self._replaying = False
        self._segment_number = 1
        self.projects = []
        self.profiles = {}
        self._last_profile_limits = None
        self._last_profile_id = None
        self.archive_dir = path + ".archives"
        self.archive_index_path = self.archive_dir + "/index.json"
        self.archives = []
        self.active_project_id = None
        self.active_line_id = None
        self.auto_distance_m = 1.0
        self._sequence = 0
        self._event_sequence = 0
        self._checkpoint_order = 0
        self._checkpoint_segment = 0
        self._checkpoint_offset = 0
        self._index_dirty_events = 0
        self._checkpoint_dirty_events = 0
        self._index_dirty_since = None
        self.last_append_ms = 0
        self.max_append_ms = 0
        self.append_profile_us = dict((name, 0) for name in
            ("json_encode", "journal_write", "journal_flush",
             "pointstore_write", "pointstore_flush", "statvfs",
             "quality_check", "profile_calculation", "index", "checkpoint"))
        self.append_profile_count = 0
        self.append_profile_max_us = {}
        self.checkpoint_scan_profile = {}
        self._storage_sample = None
        self._storage_budget_used = 0
        self._writes_until_flash_check = 0
        self.point_store.reserve_check = self._pointstore_rotation_allowed
        self._status_cache = None
        self._status_last_recorded_at = None
        self._map_changes = []
        self._map_change_floor = 0
        self._project_activity = {}
        self._line_lengths = {}
        self._pending_fixes = []
        self.storage_busy = False
        self.storage_readers = 0
        self._compacting = False
        self._batch_restore = False
        self._journal_writer = None
        self._journal_writer_path = None
        self._journal_pending = 0
        self._journal_writer_bytes = 0
        self.operation_progress = {"operation": None, "state": "idle",
                                   "processed": 0, "total": 0,
                                   "error": None}
        self._last_queued_epoch = None
        self._last_queued_coordinate = None
        self._loaded = False
        if autoload:
            self.initialize()

    def __del__(self):
        try: self._sync_journal(close=True)
        except Exception: pass
        try: self.point_store.close()
        except Exception: pass

    def _register_profile(self, measurement):
        phase = self._ticks_us()
        quality = measurement.get("quality") or {}
        limits = quality.pop("limits", None)
        profile_id = measurement.get("profile_id")
        if limits:
            profile_id = self._profile_for_limits(limits)
            self.profiles[profile_id] = {"id": profile_id, "version": 1,
                                         "limits": limits}
        if profile_id:
            measurement["profile_id"] = profile_id
        self._profile_add("profile_calculation", phase)
        return measurement

    def _profile_for_limits(self, limits):
        if limits == self._last_profile_limits and self._last_profile_id:
            return self._last_profile_id
        profile_id = _profile_id(limits)
        self._last_profile_limits = dict(limits)
        self._last_profile_id = profile_id
        return profile_id

    def _expand_measurement(self, measurement):
        result = dict(measurement)
        quality = dict(result.get("quality") or {})
        profile = self.profiles.get(result.get("profile_id"))
        if profile:
            quality["limits"] = dict(profile["limits"])
        result["quality"] = quality
        return result

    def _pack_line(self, line):
        coordinates = line.get("coordinates") or ()
        measurements = line.get("measurements") or ()
        if not isinstance(coordinates, FlashSequence):
            for coordinate, measurement in zip(coordinates, measurements):
                measurement = self._register_profile(dict(measurement))
                order = int(measurement.get("record_order", 0))
                if order <= self.point_store.max_order:
                    continue
                self.point_store.append(order, line["id"], coordinate, measurement)
        line["coordinates"], line["measurements"] = self.point_store.sequences(line["id"])
        return line

    def _pack_projects(self):
        for project in self.projects:
            for line in project.get("lines", []):
                self._pack_line(line)
            for point in project.get("points", []):
                if isinstance(point.get("measurement"), dict):
                    point["measurement"] = self._register_profile(point["measurement"])

    def initialize(self):
        if not self._loaded:
            if not self._point_store_loaded:
                self.point_store.load()
                self._point_store_loaded = True
            self._load()
            self._load_archives()
            self._loaded = True
        return self

    async def initialize_async(self):
        """Recover before accepting mutations, while radio and HTTP keep running."""
        import uasyncio as asyncio
        from state import app
        if self._loaded:
            return self
        if not self._point_store_loaded:
            await self.point_store.load_async()
            self._point_store_loaded = True
        self._replaying = True
        try:
            for index, _ in enumerate(self._load_steps()):
                if index % 16 == 0:
                    app.beat("tracking")
                    await asyncio.sleep_ms(1)
            self._load_archives()
            self._loaded = True
        finally:
            self._replaying = False
        return self

    def _load_archives(self):
        try:
            with open(self.archive_index_path, "r") as source:
                value = ujson.loads(source.read())
            if value.get("kind") == "survey_archive_index":
                self.archives = value.get("archives") or []
        except (OSError, ValueError, TypeError, AttributeError):
            self.archives = []

    def _write_archive_index(self):
        try: os.mkdir(self.archive_dir)
        except OSError: pass
        temporary = self.archive_index_path + ".tmp"
        payload = {"kind": "survey_archive_index", "schema_version": SCHEMA_VERSION,
                   "archives": self.archives}
        with open(temporary, "w") as target: target.write(ujson.dumps(payload))
        with open(temporary, "r") as source:
            if ujson.loads(source.read()).get("kind") != "survey_archive_index":
                raise ValueError("Archive index verification failed.")
        try: os.remove(self.archive_index_path)
        except OSError: pass
        os.rename(temporary, self.archive_index_path)

    @staticmethod
    def _hex_digest(digest):
        try: return digest.hexdigest()
        except AttributeError:
            return "".join("%02x" % byte for byte in digest.digest())

    def _segment_path(self, number=None, suffix=""):
        return "%s%06d.jsonl%s" % (self.segment_prefix,
                                    number or self._segment_number, suffix)

    def _segment_files(self):
        directory, prefix = ".", self.segment_prefix
        if "/" in self.segment_prefix:
            directory, prefix = self.segment_prefix.rsplit("/", 1)
        try:
            names = os.listdir(directory)
        except OSError:
            return []
        result = []
        for name in names:
            if name.startswith(prefix) and name.endswith(".jsonl"):
                try:
                    number = int(name[len(prefix):-6])
                except ValueError:
                    continue
                result.append((number, (directory + "/" + name)
                               if directory != "." else name))
        return sorted(result)

    def _write_index(self):
        payload = {"kind": "survey_index", "schema_version": SCHEMA_VERSION,
                   "global_sequence": self._event_sequence,
                   "checkpoint_order": self._checkpoint_order,
                   "active_segment": self._segment_number,
                   "active_project_id": self.active_project_id,
                   "active_line_id": self.active_line_id,
                   "feature_count": self.feature_count(),
                   "point_store_order": self.point_store.max_order,
                   "point_store_bytes": self.point_store.storage_bytes()}
        temporary = self.index_path + ".tmp"
        previous = self.index_path + ".previous"
        with open(temporary, "w") as target:
            target.write(ujson.dumps(payload))
        try:
            with open(temporary, "r") as source:
                check = ujson.loads(source.read())
            if (check.get("schema_version") != SCHEMA_VERSION or
                    check.get("kind") != "survey_index"):
                raise ValueError("Invalid tracking index.")
        except Exception:
            try: os.remove(temporary)
            except OSError: pass
            raise
        try: os.remove(previous)
        except OSError: pass
        try: os.rename(self.index_path, previous)
        except OSError: pass
        try:
            os.rename(temporary, self.index_path)
        except Exception:
            try: os.rename(previous, self.index_path)
            except OSError: pass
            raise
        try: os.remove(previous)
        except OSError: pass
        self._index_dirty_events = 0

    def _write_checkpoint(self):
        checkpoint_phase = self._ticks_us()
        self._sync_journal(close=False)
        self.point_store.sync()
        temporary = self.checkpoint_path + ".tmp"
        previous = self.checkpoint_path + ".previous"
        estimated = 4096 + 512 * sum(len(project.get("lines", [])) +
                                     len(project.get("points", []))
                                     for project in self.projects)
        # The old checkpoint and journal are already included in current free
        # space. Reserve the complete new stream plus 25% for LittleFS COW.
        if not self.storage_capacity((estimated * 5) // 4, force=True)["writable"]:
            raise OSError("Checkpoint would violate the 20 percent flash reserve.")
        segment_number = self._segment_number
        try:
            segment_offset = os.stat(self._segment_path(segment_number))[6]
        except OSError:
            segment_offset = 0
        with open(temporary, "w") as target:
            target.write(ujson.dumps({"kind": "survey_checkpoint_stream",
                "schema_version": SCHEMA_VERSION, "record": "header",
                "order": self._event_sequence, "segment": segment_number,
                "offset": segment_offset,
                "profiles": list(self.profiles.values())}) + "\n")
            for project in self.projects:
                metadata = dict(project)
                metadata.pop("lines", None); metadata.pop("points", None)
                target.write(ujson.dumps({"record": "project",
                    "project": metadata}) + "\n")
                for line in project.get("lines", []):
                    line_metadata = dict(line)
                    line_metadata["point_count"] = len(line["coordinates"])
                    line_metadata.pop("coordinates", None)
                    line_metadata.pop("measurements", None)
                    target.write(ujson.dumps({"record": "line",
                        "project_id": project["id"],
                        "line": line_metadata}) + "\n")
                for point in project.get("points", []):
                    target.write(ujson.dumps({"record": "asset",
                        "project_id": project["id"], "point": point}) + "\n")
            target.write(ujson.dumps({"record": "footer",
                "active_project_id": self.active_project_id,
                "active_line_id": self.active_line_id,
                "auto_distance_m": self.auto_distance_m}) + "\n")
        with open(temporary, "r") as source:
            first = ujson.loads(source.readline())
            last = None
            for raw in source:
                if raw.strip(): last = raw
            last = ujson.loads(last) if last else None
        if (first.get("kind") != "survey_checkpoint_stream" or
                first.get("schema_version") != SCHEMA_VERSION or
                not last or last.get("record") != "footer"):
            try: os.remove(temporary)
            except OSError: pass
            raise ValueError("Invalid tracking checkpoint.")
        try: os.remove(previous)
        except OSError: pass
        try: os.rename(self.checkpoint_path, previous)
        except OSError: pass
        try:
            os.rename(temporary, self.checkpoint_path)
        except Exception:
            try: os.rename(previous, self.checkpoint_path)
            except OSError: pass
            raise
        try: os.remove(previous)
        except OSError: pass
        self._checkpoint_order = self._event_sequence
        self._checkpoint_segment = segment_number
        self._checkpoint_offset = segment_offset
        self._segment_number = segment_number + 1
        self._profile_add("checkpoint", checkpoint_phase)

    @staticmethod
    def _ticks_us():
        try: return time.ticks_us()
        except AttributeError: return int(time.time() * 1000000)

    def _profile_add(self, name, started):
        try: elapsed = max(0, time.ticks_diff(self._ticks_us(), started))
        except (AttributeError, TypeError): elapsed = max(0, self._ticks_us() - started)
        self.append_profile_us[name] = self.append_profile_us.get(name, 0) + elapsed
        self.append_profile_max_us[name] = max(self.append_profile_max_us.get(name, 0), elapsed)

    def append_profile(self):
        result = dict(self.append_profile_us)
        result["append_count"] = self.append_profile_count
        result["phase_max_us"] = dict(self.append_profile_max_us)
        result["pointstore_detail"] = dict(self.point_store.profile_us)
        result["checkpoint_scan"] = dict(self.checkpoint_scan_profile)
        return result

    def _pointstore_rotation_allowed(self):
        return self.storage_capacity(force=True)["writable"]

    def _write_journal_event(self, event):
        phase = self._ticks_us(); encoded = ujson.dumps(event) + "\n"
        self._profile_add("json_encode", phase)
        segment = self._segment_path()
        if self._journal_writer_path == segment:
            size = self._journal_writer_bytes
        else:
            try: size = os.stat(segment)[6]
            except OSError: size = 0
        encoded_size = len(encoded.encode("utf-8"))
        if size and size + encoded_size > SEGMENT_MAX_BYTES:
            if not self.storage_capacity(force=True)["writable"]:
                raise OSError("Journal rotation would violate the flash reserve.")
            self._segment_number += 1; segment = self._segment_path()
        if self._journal_writer_path != segment:
            self._sync_journal(close=True)
            self._journal_writer = open(segment, "a")
            self._journal_writer_path = segment
            try: self._journal_writer_bytes = os.stat(segment)[6]
            except OSError: self._journal_writer_bytes = 0
        phase = self._ticks_us(); self._journal_writer.write(encoded)
        self._profile_add("journal_write", phase)
        self._journal_writer_bytes += encoded_size; self._journal_pending += 1
        return encoded_size

    def _append(self, event, defer_index=False):
        started = time.ticks_ms()
        self._event_sequence += 1
        event["order"] = self._event_sequence
        event["schema_version"] = SCHEMA_VERSION
        limits = ((event.get("measurement") or {}).get("quality") or {}).get("limits")
        profile_id = self._profile_for_limits(limits) if limits else None
        new_profile = bool(profile_id and profile_id not in self.profiles)
        journal_bytes = 0
        if event.get("event") == "vertex_added":
            if new_profile:
                journal_bytes = self._write_journal_event({
                    "event": "profile_registered", "order": event["order"],
                    "schema_version": SCHEMA_VERSION,
                    "profile": {"id": profile_id, "version": 1,
                                "limits": limits}})
                # A point may reference this profile only after its definition is
                # durable. Profile changes are rare, so this does not affect the
                # normal one-store point path.
                self._sync_journal(close=False)
        else:
            journal_bytes = self._write_journal_event(event)
        self._apply(event)
        if event.get("event") == "vertex_added" and not self._batch_restore:
            phase = self._ticks_us(); self.point_store.sync()
            self._profile_add("pointstore_flush", phase)
        if (event.get("event") != "vertex_added" or new_profile or
                self._journal_pending >= POINT_WRITE_BATCH):
            self._sync_journal(close=False)
        self._index_dirty_events += 1
        if event.get("event") in ("vertex_added", "asset_added", "feature_removed"):
            self._checkpoint_dirty_events += 1
        if not defer_index and not self._batch_restore and event.get("event") not in ("vertex_added", "asset_added"):
            phase = self._ticks_us(); self._write_index()
            self._profile_add("index", phase)
            self._index_dirty_since = None
        else:
            # The index is reconstructible from the durable point store and
            # journal. Update it after idle time, not every 15 s while recording.
            self._index_dirty_since = time.ticks_ms()
        elapsed = max(0, time.ticks_diff(time.ticks_ms(), started))
        self.last_append_ms = elapsed
        self.max_append_ms = max(getattr(self, "max_append_ms", 0), elapsed)
        self.append_profile_count += 1
        self._storage_budget_used += max(2048, journal_bytes * 2)

    def _sync_journal(self, close=False):
        if self._journal_writer is not None:
            phase = self._ticks_us(); self._journal_writer.flush()
            self._profile_add("journal_flush", phase)
            self._journal_pending = 0
            if close:
                self._journal_writer.close()
                self._journal_writer = None
                self._journal_writer_path = None
                self._journal_writer_bytes = 0

    def _load(self):
        self._replaying = True
        try:
            for _ in self._load_steps():
                pass
        finally:
            self._replaying = False

    def _load_steps(self):
        self._recover_index()
        checkpoint_order = self._load_checkpoint()
        self._event_sequence = max(self._event_sequence, self.point_store.max_order)
        if self._checkpoint_segment:
            self._segment_number = max(self._segment_number,
                                       self._checkpoint_segment + 1)
        segments = self._segment_files()
        if segments:
            self._segment_number = max(self._segment_number, segments[-1][0])
        for number, path in segments:
            if self._checkpoint_segment and number < self._checkpoint_segment:
                continue
            try:
                if os.stat(path)[6] > SEGMENT_HARD_LIMIT_BYTES:
                    os.rename(path, path + ".oversize")
                    continue
            except OSError:
                continue
            try:
                with open(path, "r") as source:
                    if (number == self._checkpoint_segment and
                            self._checkpoint_offset > 0):
                        source.seek(self._checkpoint_offset)
                    valid_records, malformed = [], False
                    for raw in source:
                        try:
                            event = ujson.loads(raw)
                            if event.get("schema_version") in LEGACY_SCHEMA_VERSIONS + (
                                    SCHEMA_VERSION,):
                                valid_records.append(raw if raw.endswith("\n") else raw + "\n")
                                self._event_sequence = max(self._event_sequence,
                                                           int(event.get("order", 0)))
                                if int(event.get("order", 0)) > checkpoint_order:
                                    self._apply(event)
                                yield None
                        except (ValueError, TypeError, AttributeError):
                            malformed = True
                            continue
                if malformed:
                    quarantine = path + ".corrupt"
                    try: os.remove(quarantine)
                    except OSError: pass
                    os.rename(path, quarantine)
                    if valid_records:
                        with open(path, "w") as recovered:
                            for raw in valid_records:
                                recovered.write(raw)
            except OSError:
                continue
        self._repair_active_references()
        for project in self.projects:
            for line in project.get("lines", []):
                if (line.get("_summary") or {}).get("vertex_count") != len(
                        line["coordinates"]):
                    line.pop("_summary", None)
                    for _ in self._iter_line_summary(line):
                        yield None
                self._line_lengths[line["id"]] = float(
                    (line.get("_summary") or {}).get("length_m", 0.0))
        if segments or self.projects:
            self._write_index()
        # A new boot requires a full map before incremental changes can follow.
        self._map_changes = []
        self._map_change_floor = self._event_sequence

    def _load_checkpoint(self, readonly=False):
        paths = ((self.checkpoint_path,) if readonly else
                 (self.checkpoint_path, self.checkpoint_path + ".tmp",
                  self.checkpoint_path + ".previous"))
        for path in paths:
            try:
                checkpoint_schema, expected_points = None, {}
                with open(path, "r") as source:
                    first_raw = source.readline()
                    value = ujson.loads(first_raw)
                    if readonly and (value.get("kind") != "survey_checkpoint_stream" or
                                     value.get("schema_version") != SCHEMA_VERSION):
                        raise ValueError("Unexpected checkpoint verification format.")
                    if value.get("kind") == "survey_checkpoint_stream":
                        checkpoint_schema = value.get("schema_version")
                        projects, project_lookup, line_lookup = [], {}, {}
                        expected_points = {}
                        self.profiles = dict((profile["id"], profile) for profile in
                                             value.get("profiles", []))
                        footer = None
                        for raw in source:
                            record = ujson.loads(raw)
                            kind = record.get("record")
                            if kind == "project":
                                project = record.get("project") or {}
                                project["lines"], project["points"] = [], []
                                projects.append(project)
                                project_lookup[project.get("id")] = project
                            elif kind == "line":
                                line = record.get("line") or {}
                                expected_points[line.get("id")] = int(
                                    line.pop("point_count", 0))
                                line["coordinates"], line["measurements"] = [], []
                                project_lookup[record.get("project_id")]["lines"].append(line)
                                line_lookup[line.get("id")] = line
                            elif kind == "vertex":
                                if readonly:
                                    raise ValueError("Current checkpoints must reference the point store.")
                                line = line_lookup[record.get("line_id")]
                                line["coordinates"].append(record.get("coordinate"))
                                measurement = self._register_profile(
                                    record.get("measurement") or {})
                                line["measurements"].append(measurement)
                            elif kind == "asset":
                                project_lookup[record.get("project_id")]["points"].append(
                                    record.get("point"))
                            elif kind == "footer":
                                footer = record
                        if not footer:
                            continue
                        value = {"kind": "survey_checkpoint",
                            "schema_version": SCHEMA_VERSION,
                            "profiles": list(self.profiles.values()),
                            "order": value.get("order", 0), "projects": projects,
                            "segment": value.get("segment", 0),
                            "offset": value.get("offset", 0),
                            "active_project_id": footer.get("active_project_id"),
                            "active_line_id": footer.get("active_line_id"),
                            "auto_distance_m": footer.get("auto_distance_m", 1.0)}
                    else:
                        value = ujson.loads(first_raw + source.read())
                if (value.get("kind") != "survey_checkpoint" or
                        value.get("schema_version") not in LEGACY_SCHEMA_VERSIONS + (
                            SCHEMA_VERSION,)):
                    continue
                self.projects = value.get("projects") or []
                self.profiles = dict((profile["id"], profile) for profile in
                                     value.get("profiles", []))
                self._pack_projects()
                if checkpoint_schema == SCHEMA_VERSION:
                    deficits = {}
                    for project in self.projects:
                        for line in project.get("lines", []):
                            if len(line["coordinates"]) < expected_points.get(
                                    line.get("id"), 0):
                                deficits[line["id"]] = expected_points[line["id"]]
                    if deficits and not self.point_store.checkpoint_counts_match(
                            deficits, int(value.get("order", 0))):
                        raise ValueError("Point store is behind checkpoint.")
                self._line_lengths = dict((line["id"], float(
                    (line.get("_summary") or {}).get("length_m", 0.0)))
                    for project in self.projects for line in project.get("lines", [])
                    if (line.get("_summary") or {}).get("vertex_count") ==
                    len(line["coordinates"]))
                self._project_activity = dict((project["id"], project.get("created_at"))
                                              for project in self.projects)
                self.active_project_id = value.get("active_project_id")
                self.active_line_id = value.get("active_line_id")
                self.auto_distance_m = value.get("auto_distance_m", 1.0)
                self._checkpoint_order = max(0, int(value.get("order", 0)))
                self._checkpoint_segment = max(0, int(value.get("segment", 0)))
                self._checkpoint_offset = max(0, int(value.get("offset", 0)))
                self._event_sequence = self._checkpoint_order
                if path != self.checkpoint_path:
                    try: os.rename(path, self.checkpoint_path)
                    except OSError: pass
                return self._checkpoint_order
            except (OSError, ValueError, TypeError, AttributeError):
                continue
        return 0

    def _recover_index(self):
        temporary, previous = self.index_path + ".tmp", self.index_path + ".previous"
        def valid(path):
            try:
                with open(path, "r") as source:
                    value = ujson.loads(source.read())
                return (value.get("kind") == "survey_index" and
                        value.get("schema_version") == SCHEMA_VERSION)
            except Exception:
                return False
        if valid(self.index_path):
            for stale in (temporary, previous):
                try: os.remove(stale)
                except OSError: pass
            return
        for candidate in (temporary, previous):
            if valid(candidate):
                try: os.remove(self.index_path)
                except OSError: pass
                os.rename(candidate, self.index_path)
                return

    def _repair_active_references(self):
        project = self._project(self.active_project_id)
        if project is None:
            self.active_project_id = self.active_line_id = None
            return
        line = self._line(project, self.active_line_id)
        if line is None or line.get("state") == "finished":
            self.active_line_id = None

    def _file_has_valid_event(self, path):
        try:
            with open(path, "r") as source:
                for raw in source:
                    try:
                        event = ujson.loads(raw)
                        if event.get("schema_version") in LEGACY_SCHEMA_VERSIONS + (
                                SCHEMA_VERSION,):
                            self._event_sequence = max(self._event_sequence,
                                                       int(event.get("order", 0)))
                            self._apply(event)
                    except (ValueError, TypeError, AttributeError):
                        continue
        except OSError:
            pass
        return False

    def _project(self, project_id=None):
        wanted = project_id or self.active_project_id
        for project in self.projects:
            if project["id"] == wanted:
                return project
        return None

    def _line(self, project, line_id=None):
        wanted = line_id or self.active_line_id
        if not project:
            return None
        for line in project["lines"]:
            if line["id"] == wanted:
                return line
        return None

    def _apply(self, event):
        kind = event.get("event")
        if kind == "project_created":
            if not self._project(event["project"]["id"]):
                self.projects.append(event["project"])
            self.active_project_id = event["project"]["id"]
        elif kind == "project_selected":
            self.active_project_id = event.get("project_id")
            self.active_line_id = None
        elif kind == "line_started":
            self._pack_line(event["line"])
            project = self._project(event.get("project_id"))
            if project and not self._line(project, event["line"]["id"]):
                project["lines"].append(event["line"])
            self.active_project_id = event.get("project_id")
            self.active_line_id = event["line"]["id"]
            self.auto_distance_m = event.get("auto_distance_m", 1.0)
            self._line_lengths[event["line"]["id"]] = 0.0
        elif kind == "profile_registered":
            profile = event.get("profile") or {}
            if profile.get("id") and isinstance(profile.get("limits"), dict):
                expected = _profile_id(profile["limits"])
                if expected != profile["id"]:
                    raise ValueError("Invalid measurement profile ID.")
                self.profiles[profile["id"]] = profile
        elif kind == "vertex_added":
            line = self._line(self._project(event.get("project_id")),
                              event.get("line_id"))
            if line:
                if self._replaying:
                    event["measurement"]["record_order"] = event.get("order", 0)
                    self._register_profile(event["measurement"])
                    if self.point_store.append(event.get("order", 0), line["id"],
                                               event["coordinate"], event["measurement"]):
                        line.pop("_summary", None)
                        self._project_activity[event.get("project_id")] = event["measurement"].get("recorded_at")
                    return
                previous = line["coordinates"][-1] if line["coordinates"] else None
                current_length = self._line_lengths.get(line["id"])
                if current_length is None:
                    current_length = self._line_length(line)
                event["measurement"]["record_order"] = event.get("order", 0)
                self._register_profile(event["measurement"])
                phase = self._ticks_us()
                appended = self.point_store.append(event.get("order", 0), line["id"],
                                                   event["coordinate"], event["measurement"])
                self._profile_add("pointstore_write", phase)
                if not appended:
                    return
                gap = _distance_m(previous, event["coordinate"]) if previous else 0.0
                vertical = (abs(float(event["coordinate"][2]) - float(previous[2]))
                            if previous else 0.0)
                summary = line.get("_summary")
                rebuilt = False
                if not summary and previous:
                    self._line_summary(line)
                    summary = line.get("_summary")
                    rebuilt = True
                if not rebuilt and (not summary or
                        summary.get("vertex_count") != len(line["coordinates"]) - 1):
                    summary = {"vertex_count": len(line["coordinates"]) - 1,
                               "length_m": current_length,
                               "quality_accepted_count": 0,
                               "large_gap_count": 0, "max_gap_m": 0.0,
                               "large_vertical_step_count": 0,
                               "max_vertical_step_m": 0.0,
                               "overlapping": False}
                if not rebuilt:
                    summary["vertex_count"] = len(line["coordinates"])
                    summary["length_m"] = current_length + gap
                    summary["quality_accepted_count"] += int(bool(
                        (event["measurement"].get("quality") or {}).get("accepted")))
                    summary["large_gap_count"] += int(gap > MAX_LINE_GAP_M)
                    summary["max_gap_m"] = max(summary["max_gap_m"], gap)
                    summary["large_vertical_step_count"] += int(
                        vertical > MAX_VERTICAL_STEP_M)
                    summary["max_vertical_step_m"] = max(
                        summary["max_vertical_step_m"], vertical)
                    summary["overlapping"] = summary["overlapping"] or bool(
                        previous and gap < 0.05)
                line["_summary"] = summary
                if previous:
                    self._line_lengths[line["id"]] = (current_length +
                                                       _distance_m(previous, event["coordinate"]))
                else:
                    self._line_lengths[line["id"]] = 0.0
        elif kind == "asset_added":
            project = self._project(event.get("project_id"))
            if project:
                event["point"]["measurement"]["record_order"] = event.get("order", 0)
                self._register_profile(event["point"]["measurement"])
                project["points"].append(event["point"])
        elif kind == "line_paused":
            line = self._line(self._project(event.get("project_id")),
                              event.get("line_id"))
            if line:
                line["state"] = "paused"
                line["pause_reason"] = event.get("reason")
        elif kind == "line_resumed":
            line = self._line(self._project(event.get("project_id")),
                              event.get("line_id"))
            if line:
                line["state"] = "recording"
                line["pause_reason"] = None
                self.active_line_id = line["id"]
        elif kind == "line_selected":
            project = self._project(event.get("project_id"))
            line = self._line(project, event.get("line_id"))
            if line and line.get("state") != "finished":
                self.active_project_id = project["id"]
                self.active_line_id = line["id"]
        elif kind == "line_finished":
            line = self._line(self._project(event.get("project_id")),
                              event.get("line_id"))
            if line:
                line["state"] = "finished"
                line["finished_at"] = event.get("at")
                line["finish_diagnostics"] = event.get("diagnostics")
            self.active_line_id = None
        elif kind == "feature_removed":
            project = self._project(event.get("project_id"))
            if not project:
                return
            if event.get("feature_kind") == "asset":
                project["points"] = [point for point in project["points"]
                                     if point["id"] != event.get("feature_id")]
            elif event.get("feature_kind") == "vertex":
                line = self._line(project, event.get("line_id"))
                if line and line["coordinates"]:
                    if len(line["coordinates"]) > 1 and not self._replaying:
                        current_length = self._line_lengths.get(line["id"])
                        if current_length is None:
                            current_length = self._line_length(line)
                        self._line_lengths[line["id"]] = max(0.0,
                            current_length -
                            _distance_m(line["coordinates"][-2], line["coordinates"][-1]))
                    self.point_store.remove_last(event.get("order", 0), line["id"])
                    line.pop("_summary", None)
        elif kind == "snapshot":
            self.projects = event.get("projects", [])
            self._pack_projects()
            self.active_project_id = event.get("active_project_id")
            self.active_line_id = event.get("active_line_id")
            self.auto_distance_m = event.get("auto_distance_m", 1.0)
            self._project_activity = dict((project["id"], project.get("created_at"))
                                          for project in self.projects)
            self._line_lengths = {} if self._replaying else dict((line["id"], self._line_length(line))
                                      for project in self.projects
                                      for line in project.get("lines", []))
        elif kind == "project_renamed":
            project = self._project(event.get("project_id"))
            if project:
                project["name"] = event.get("name", project["name"])
        elif kind == "project_deleted":
            self.projects = [project for project in self.projects
                             if project["id"] != event.get("project_id")]
            self._project_activity.pop(event.get("project_id"), None)
            if self.active_project_id == event.get("project_id"):
                self.active_project_id = self.active_line_id = None

        project_id = event.get("project_id")
        activity = event.get("at")
        if kind == "project_created":
            project_id = event["project"]["id"]
            activity = event["project"].get("created_at")
        elif kind == "line_started":
            activity = event["line"].get("created_at")
        elif kind == "vertex_added":
            activity = event["measurement"].get("recorded_at")
        elif kind == "asset_added":
            activity = event["point"].get("measurement", {}).get("recorded_at")
        if (project_id and activity is not None and kind in
                ("project_created", "line_started", "vertex_added", "asset_added",
                 "line_paused", "line_resumed", "line_finished",
                 "feature_removed", "project_renamed")):
            self._project_activity[project_id] = int(activity)
        if self._replaying:
            return
        change = None
        if kind == "vertex_added":
            change = {"op": "vertex", "project_id": event.get("project_id"),
                      "line_id": event.get("line_id"),
                      "coordinate": event.get("coordinate")}
        elif kind == "asset_added":
            point = event.get("point") or {}
            change = {"op": "asset", "project_id": event.get("project_id"),
                      "point": {"id": point.get("id"),
                                "type": point.get("object_type"),
                                "name": point.get("name"),
                                "note": point.get("note", ""),
                                "coordinate": point.get("coordinate"),
                                "line_id": point.get("line_id"),
                                "chainage_m": point.get("chainage_m")}}
        elif kind == "feature_removed":
            change = {"op": "remove", "project_id": event.get("project_id"),
                      "feature_kind": event.get("feature_kind"),
                      "feature_id": event.get("feature_id"),
                      "line_id": event.get("line_id")}
        elif kind in ("line_started", "line_paused", "line_resumed", "line_finished"):
            line = self._line(self._project(event.get("project_id")),
                              event.get("line_id") or (event.get("line") or {}).get("id"))
            if line:
                length = self._line_lengths.get(line.get("id"))
                if length is None:
                    length = self._line_length(line)
                change = {"op": "line", "project_id": event.get("project_id"),
                          "line": {"id": line.get("id"), "name": line.get("name"),
                                   "state": line.get("state"),
                                   "vertex_count": len(line.get("coordinates", [])),
                                   "length_m": round(length, 3)}}
        if change is not None:
            change["revision"] = int(event.get("order", self._event_sequence))
            self._map_changes.append(change)
            if len(self._map_changes) > 128:
                removed = self._map_changes.pop(0)
                self._map_change_floor = removed["revision"]
        self._status_cache = None

    def _id(self, prefix):
        self._sequence = (self._sequence + 1) & 0xffff
        return "%s-%08x-%04x" % (prefix, int(time.time()) & 0xffffffff,
                                  self._sequence)

    def _measurement(self, fix, allow_rejected=False):
        phase = self._ticks_us()
        quality = quality_check(fix)
        self._profile_add("quality_check", phase)
        if not quality["accepted"] and not allow_rejected:
            raise ValueError("Measurement quality rejected: %s." %
                             ", ".join(quality["reasons"]))
        return {
            "recorded_at": int(time.time()), "fix_quality": int(fix.get("qual", 0)),
            "fix_status": fix.get("fix_status_text", "UNKNOWN"),
            "satellites": int(fix.get("sats", 0)), "hdop": fix.get("hdop"),
            "correction_age_sec": fix.get("correction_age_sec"),
            "station_id": fix.get("station_id"),
            "accuracy_observation": fix.get("accuracy_observation"),
            "receiver_accuracy": fix.get("receiver_accuracy"),
            "quality": quality,
        }

    def storage_capacity(self, temporary_bytes=16384, force=False):
        """Conservative reserve check including one copy-on-write flash window."""
        if force or self._storage_sample is None:
            phase = self._ticks_us()
            try:
                volume = self.path.rsplit("/", 1)[0] if "/" in self.path else "."
                values = os.statvfs(volume or ".")
                block = int(values[1] or values[0]); total = block * int(values[2])
                free = block * int(values[4])
            except (OSError, AttributeError, IndexError):
                total = free = 0
            self._profile_add("statvfs", phase)
            self._storage_sample = (total, free)
            self._storage_budget_used = 0
        else:
            total, free = self._storage_sample
        free = max(0, free - self._storage_budget_used)
        reserve = (total * MIN_FLASH_RESERVE_PERCENT + 99) // 100 if total else 0
        temporary = (max(0, int(temporary_bytes or 0)) +
                     OPERATIONAL_FLASH_HEADROOM_BYTES)
        return {"free_flash_bytes": free,
                "required_flash_reserve_bytes": reserve,
                "estimated_temporary_bytes": temporary,
                "writable": not total or free - temporary >= reserve}

    def _require_write_capacity(self, automatic=False, force=False):
        force = force or self._writes_until_flash_check <= 0
        storage = self.storage_capacity(force=force)
        self._writes_until_flash_check = (FLASH_RECHECK_WRITES if force else
                                          self._writes_until_flash_check - 1)
        if not storage["writable"]:
            if automatic:
                self._pause_for_capacity("storage_reserve")
            raise ValueError("Flash reserve would fall below 20 percent.")

    def _coordinate(self, fix):
        # Never manufacture an altitude for a route sample.  A zero inserted for
        # a missing GGA field would look like a valid measurement in exports and
        # would violate the lossless evidence model.
        if (not isinstance(fix, dict) or fix.get("lon") is None or
                fix.get("lat") is None or fix.get("alt") is None):
            raise ValueError("A complete longitude, latitude and altitude is required.")
        return [float(fix["lon"]), float(fix["lat"]), float(fix["alt"])]

    def _feature_limit(self):
        return MAX_FEATURES if self.max_features is None else self.max_features

    def create_project(self, name, description=""):
        if len(self.projects) >= MAX_PROJECTS:
            raise ValueError("Project limit reached.")
        project = {"id": self._id("project"), "name": _clean_text(name),
                   "description": _clean_text(description, 240),
                   "created_at": int(time.time()), "lines": [], "points": []}
        if not project["name"]:
            raise ValueError("Project name is required.")
        self._append({"event": "project_created", "project": project})
        return project

    def select_project(self, project_id):
        if not self._project(project_id):
            raise ValueError("Project not found.")
        if self.active_line_id and project_id != self.active_project_id:
            raise ValueError("Pause or finish the active line before changing project.")
        self._append({"event": "project_selected", "project_id": project_id})

    def rename_project(self, project_id, name):
        if not self._project(project_id):
            raise ValueError("Project not found.")
        name = _clean_text(name)
        if not name:
            raise ValueError("Project name is required.")
        self._append({"event": "project_renamed", "project_id": project_id,
                      "name": name, "at": int(time.time())})

    def delete_project(self, project_id):
        if self.active_project_id == project_id and self.active_line_id:
            raise ValueError("Finish the active line before deleting its project.")
        if not self._project(project_id):
            raise ValueError("Project not found.")
        self._append({"event": "project_deleted", "project_id": project_id})

    @staticmethod
    def _finish_steps(steps):
        result = None
        try:
            for item in steps:
                if item is not None: result = item
        finally: steps.close()
        return result

    @staticmethod
    async def _finish_steps_async(steps):
        import uasyncio as asyncio
        result = None
        try:
            for item in steps:
                if item is not None: result = item
                await asyncio.sleep_ms(1)
        finally: steps.close()
        return result

    def archive_project(self, project_id):
        result = self._finish_steps(self._archive_project_steps(project_id))
        self.compact()
        return result

    async def archive_project_async(self, project_id):
        result = await self._finish_steps_async(self._archive_project_steps(project_id))
        await self.compact_async()
        return result

    def _archive_project_steps(self, project_id):
        """Write, parse and hash one restorable project before removing it."""
        project = self._project(project_id)
        if not project:
            raise ValueError("Project not found.")
        if self.active_project_id == project_id and self.active_line_id:
            raise ValueError("Finish the active line before archiving its project.")
        archive_id = self._id("archive")
        try: os.mkdir(self.archive_dir)
        except OSError: pass
        final_path = self.archive_dir + "/" + archive_id + ".ndjson"
        temporary = final_path + ".tmp"
        estimated = sum(400 * len(line["measurements"])
                        for line in project["lines"]) + 32768
        storage = self.storage_capacity((estimated * 5) // 4, force=True)
        if not storage["writable"]:
            raise ValueError("Not enough flash to archive while preserving the reserve.")
        try:
            with open(temporary, "w") as target:
                chunks = self.iter_compact_archive(project_id)
                try:
                    for index, raw in enumerate(chunks):
                        target.write(raw)
                        if index % 4 == 3: yield None
                finally: chunks.close()
            verification = self._verify_archive_path_steps(temporary)
            try:
                for verified in verification:
                    if verified is None: yield None
            finally: verification.close()
            if not verified["valid"]:
                raise ValueError("The completed archive could not be verified.")
            os.rename(temporary, final_path)
            yield None
            metadata = {"id": archive_id, "project_id": project["id"],
                        "name": project["name"], "created_at": project.get("created_at"),
                        "archived_at": int(time.time()),
                        "point_count": len(project["points"]) + sum(
                            len(line["coordinates"]) for line in project["lines"]),
                        "line_count": len(project["lines"]),
                        "size_bytes": os.stat(final_path)[6], "schema_version": SCHEMA_VERSION,
                        "sha256": verified["sha256"]}
            self.archives.append(metadata)
            self._write_archive_index()
            yield None
            self._append({"event": "project_deleted", "project_id": project_id,
                          "at": int(time.time()), "archived_as": archive_id})
            yield dict(metadata)
        finally:
            try: os.remove(temporary)
            except OSError: pass

    def verify_archive_path(self, path):
        return self._finish_steps(self._verify_archive_path_steps(path))

    def _verify_archive_path_steps(self, path):
        digest = hashlib.sha256(); records = points = 0
        projects, lines, header, footer = set(), set(), False, False
        try:
            with open(path, "r") as source:
                for index, raw in enumerate(source):
                    original = ujson.loads(raw)
                    value = _expanded_archive_record(original)
                    if isinstance(value,dict) and value.get("record") == "integrity":
                        expected = value
                        break
                    digest.update(raw.encode("utf-8")); records += 1
                    kind=value.get("record")
                    if kind=="header":
                        header=(value.get("kind")=="survey_backup" and
                                value.get("schema_version")==SCHEMA_VERSION)
                    elif kind=="project": projects.add((value.get("project")or{}).get("id"))
                    elif kind=="line":
                        if value.get("project_id") not in projects: raise ValueError("line order")
                        lines.add((value.get("line")or{}).get("id"))
                    elif kind=="vertex":
                        if value.get("line_id") not in lines: raise ValueError("vertex order")
                        _validate_coordinate(value.get("coordinate"));
                        if not isinstance(value.get("measurement"),dict):raise ValueError("measurement")
                        points+=1
                    elif kind=="asset":
                        if value.get("project_id") not in projects:raise ValueError("asset order")
                        point=value.get("point")or{};_validate_coordinate(point.get("coordinate"));points+=1
                    elif kind=="footer":footer=True
                    else:raise ValueError("record")
                    if index % 4 == 3: yield None
                else:
                    yield {"valid": False, "error": "integrity_record_missing"}; return
                if source.read().strip():
                    yield {"valid": False, "error": "trailing_records"}; return
            checksum = self._hex_digest(digest)
            if (not header or not footer or expected.get("algorithm") != "sha256" or
                    expected.get("content_sha256") != checksum or
                    expected.get("records") != records):
                yield {"valid": False, "error": "checksum_mismatch"}; return
            yield {"valid": True, "sha256": checksum,"point_count":points}
        except (OSError, ValueError, TypeError, AttributeError):
            yield {"valid": False, "error": "archive_invalid"}

    def verify_archive(self, archive_id):
        metadata = next((item for item in self.archives if item["id"] == archive_id), None)
        if not metadata: raise ValueError("Archive not found.")
        result = self.verify_archive_path(self.archive_dir + "/" + archive_id + ".ndjson")
        if result.get("sha256") != metadata.get("sha256"):
            return {"valid": False, "error": "index_checksum_mismatch"}
        return result

    def reactivate_archive(self, archive_id):
        metadata = next((item for item in self.archives if item["id"] == archive_id), None)
        if not metadata: raise ValueError("Archive not found.")
        if self._project(metadata.get("project_id")):
            raise ValueError("The archived project is already active.")
        if self.feature_count() + int(metadata.get("point_count", 0)) > self._feature_limit():
            raise ValueError("Reactivation would exceed the active point limit.")
        path = self.archive_dir + "/" + archive_id + ".ndjson"
        if not self.verify_archive(archive_id).get("valid"):
            raise ValueError("Archive verification failed.")
        self.restore_ndjson(self._merged_archive_stream(path, metadata["project_id"]))
        return self._project(metadata["project_id"])

    async def verify_archive_async(self, archive_id):
        metadata = next((item for item in self.archives if item["id"] == archive_id), None)
        if not metadata: raise ValueError("Archive not found.")
        result = await self._finish_steps_async(self._verify_archive_path_steps(
            self.archive_dir + "/" + archive_id + ".ndjson"))
        if result.get("sha256") != metadata.get("sha256"):
            return {"valid": False, "error": "index_checksum_mismatch"}
        return result

    async def reactivate_archive_async(self, archive_id):
        metadata = next((item for item in self.archives if item["id"] == archive_id), None)
        if not metadata: raise ValueError("Archive not found.")
        if self._project(metadata.get("project_id")):
            raise ValueError("The archived project is already active.")
        if self.feature_count() + int(metadata.get("point_count", 0)) > self._feature_limit():
            raise ValueError("Reactivation would exceed the active point limit.")
        if not (await self.verify_archive_async(archive_id)).get("valid"):
            raise ValueError("Archive verification failed.")
        path = self.archive_dir + "/" + archive_id + ".ndjson"
        await self.restore_ndjson_async(self._merged_archive_stream(path, metadata["project_id"]))
        return {"id": metadata["project_id"]}

    def iter_compact_archive(self, project_id):
        """Internal text archive with compact measurements and full integrity."""
        digest, records = hashlib.sha256(), 0
        chunks = self.iter_backup_ndjson(project_id)
        try:
            for raw in chunks:
                value = ujson.loads(raw)
                if isinstance(value,dict) and value.get("record") == "integrity": continue
                if isinstance(value,dict) and value.get("record") == "header":
                    value["encoding"] = "compact-measurements-v1"
                elif isinstance(value,dict) and value.get("record") == "vertex":
                    measurement=dict(value["measurement"])
                    quality=dict(measurement.get("quality") or {});quality.pop("limits",None)
                    measurement["quality"]=quality
                    value = ["v",value.get("project_id"),value["line_id"],
                             value["coordinate"],_compact(measurement)]
                elif isinstance(value,dict) and value.get("record") == "asset":
                    point=dict(value["point"]);measurement=dict(point.get("measurement")or{})
                    quality=dict(measurement.get("quality")or{});quality.pop("limits",None)
                    measurement["quality"]=quality;point["measurement"]=_compact(measurement)
                    value=["a",value.get("project_id"),point]
                encoded=ujson.dumps(value)+"\n";digest.update(encoded.encode("utf-8"));records+=1
                yield encoded
        finally:
            chunks.close()
        yield ujson.dumps({"record":"integrity","algorithm":"sha256",
            "content_sha256":self._hex_digest(digest),"records":records})+"\n"

    def _merged_archive_stream(self,path,active_project_id):
        with open(path,"r") as source: archive_header=ujson.loads(source.readline())
        profiles=dict((p["id"],p) for p in self.profiles.values())
        profiles.update((p["id"],p) for p in archive_header.get("profiles",[]))
        digest,records=hashlib.sha256(),0
        def emit(value):
            nonlocal records
            raw=ujson.dumps(value)+"\n";digest.update(raw.encode("utf-8"));records+=1;return raw
        yield emit({"kind":"survey_backup","schema_version":SCHEMA_VERSION,
                    "record":"header","profiles":list(profiles.values())})
        chunks = self.iter_backup_ndjson()
        try:
            for raw in chunks:
                value=ujson.loads(raw)
                if value.get("record") not in ("header","footer","integrity"): yield emit(value)
        finally:
            chunks.close()
        with open(path,"r") as source:
            source.readline()
            for raw in source:
                value=ujson.loads(raw)
                if isinstance(value,dict) and value.get("record") in ("footer","integrity"):continue
                yield emit(value)
        yield emit({"record":"footer","active_project_id":active_project_id,
                    "active_line_id":None,"auto_distance_m":self.auto_distance_m})
        yield ujson.dumps({"record":"integrity","algorithm":"sha256",
            "content_sha256":self._hex_digest(digest),"records":records})+"\n"

    def backup(self):
        projects = []
        for project in self.projects:
            output = dict(project); output["lines"], output["points"] = [], []
            for line in project["lines"]:
                item = dict(line)
                item["coordinates"] = list(line["coordinates"])
                item["measurements"] = [self._expand_measurement(value)
                                        for value in line["measurements"]]
                output["lines"].append(item)
            for point in project["points"]:
                item = dict(point)
                item["measurement"] = self._expand_measurement(point["measurement"])
                output["points"].append(item)
            projects.append(output)
        return {"schema_version": SCHEMA_VERSION, "kind": "survey_backup",
                "profiles": list(self.profiles.values()), "projects": projects,
                "active_project_id": self.active_project_id,
                "active_line_id": self.active_line_id,
                "auto_distance_m": self.auto_distance_m}

    def iter_backup_ndjson(self, project_id=None):
        """Yield an integrity-protected schema-4 backup one record at a time."""
        selected = self.projects
        if project_id is not None:
            project = self._project(project_id)
            if not project:
                raise ValueError("Project not found.")
            selected = [project]
        total = sum(len(project["points"]) + sum(len(line["coordinates"])
                    for line in project["lines"]) for project in selected)
        self.operation_progress.update(operation="backup", state="running",
                                       processed=0, total=total, error=None)
        digest, record_count = hashlib.sha256(), 0
        def encoded(value):
            nonlocal record_count
            raw = ujson.dumps(value) + "\n"
            digest.update(raw.encode("utf-8")); record_count += 1
            return raw
        yield encoded({"kind": "survey_backup", "schema_version": SCHEMA_VERSION,
                       "record": "header", "profiles": list(self.profiles.values())})
        for project in selected:
            metadata = dict(project); metadata.pop("lines", None); metadata.pop("points", None)
            yield encoded({"record": "project", "project": metadata})
            for line in project["lines"]:
                metadata = dict(line)
                coordinates = metadata.pop("coordinates", [])
                measurements = metadata.pop("measurements", [])
                yield encoded({"record": "line", "project_id": project["id"],
                               "line": metadata})
                rows = (coordinates.store.iter_line(coordinates.line_id)
                        if isinstance(coordinates, FlashSequence) else None)
                try:
                    for index in range(len(coordinates)):
                        row = next(rows) if rows is not None else None
                        coordinate = row["coordinate"] if row is not None else coordinates[index]
                        measurement = row["measurement"] if row is not None else measurements[index]
                        yield encoded({"record": "vertex", "project_id": project["id"],
                                       "line_id": line["id"], "coordinate": coordinate,
                                       "measurement": self._expand_measurement(measurement)})
                        self.operation_progress["processed"] += 1
                finally:
                    if rows is not None: rows.close()
            for point in project["points"]:
                yield encoded({"record": "asset", "project_id": project["id"],
                               "point": dict(point, measurement=self._expand_measurement(
                                   point["measurement"]))})
                self.operation_progress["processed"] += 1
        yield encoded({"record": "footer",
                       "active_project_id": (self.active_project_id
                           if project_id is None else selected[0]["id"]),
                       "active_line_id": (self.active_line_id
                           if project_id is None else None),
                       "auto_distance_m": self.auto_distance_m})
        yield ujson.dumps({"record": "integrity", "algorithm": "sha256",
                           "content_sha256": self._hex_digest(digest),
                           "records": record_count}) + "\n"
        self.operation_progress["state"] = "complete"

    @staticmethod
    def parse_backup_ndjson(lines):
        payload = {"kind": "survey_backup", "schema_version": SCHEMA_VERSION,
                   "profiles": [], "projects": [], "active_project_id": None,
                   "active_line_id": None, "auto_distance_m": 1.0}
        projects, line_lookup, records = {}, {}, 0
        digest, integrity, header_schema, footer_seen = hashlib.sha256(), None, None, False
        for raw in lines:
            if not raw or not raw.strip():
                continue
            records += 1
            if records > MAX_FEATURES + MAX_LINES_PER_PROJECT * MAX_PROJECTS + MAX_PROJECTS + 3:
                raise ValueError("Survey backup contains too many records.")
            value = _expanded_archive_record(ujson.loads(raw))
            kind = value.get("record")
            if kind == "integrity":
                if integrity is not None:
                    raise ValueError("Survey backup contains duplicate integrity records.")
                integrity = value
                continue
            if integrity is not None:
                raise ValueError("Survey backup contains records after integrity.")
            digest.update(raw.encode("utf-8"))
            if kind == "header":
                if (value.get("kind") != "survey_backup" or
                        value.get("schema_version") not in LEGACY_SCHEMA_VERSIONS + (
                            SCHEMA_VERSION,)):
                    raise ValueError("Survey backup header is invalid.")
                payload["schema_version"] = value.get("schema_version")
                header_schema = value.get("schema_version")
                payload["profiles"] = value.get("profiles", [])
            elif kind == "project":
                project = dict(value.get("project") or {})
                project["lines"], project["points"] = [], []
                payload["projects"].append(project); projects[project.get("id")] = project
            elif kind == "line":
                project = projects.get(value.get("project_id"))
                if project is None:
                    raise ValueError("A line precedes its project.")
                line = dict(value.get("line") or {})
                line["coordinates"], line["measurements"] = [], []
                project["lines"].append(line); line_lookup[line.get("id")] = line
            elif kind == "vertex":
                line = line_lookup.get(value.get("line_id"))
                if line is None:
                    raise ValueError("A vertex precedes its line.")
                line["coordinates"].append(value.get("coordinate"))
                line["measurements"].append(value.get("measurement"))
            elif kind == "asset":
                project = projects.get(value.get("project_id"))
                if project is None:
                    raise ValueError("An asset precedes its project.")
                project["points"].append(value.get("point"))
            elif kind == "footer":
                footer_seen = True
                payload["active_project_id"] = value.get("active_project_id")
                payload["active_line_id"] = value.get("active_line_id")
                payload["auto_distance_m"] = value.get("auto_distance_m", 1.0)
            else:
                raise ValueError("Survey backup record is invalid.")
        if not footer_seen:
            raise ValueError("Survey backup footer is missing.")
        if header_schema == SCHEMA_VERSION:
            checksum = Tracker._hex_digest(digest)
            if (not integrity or integrity.get("algorithm") != "sha256" or
                    integrity.get("content_sha256") != checksum or
                    integrity.get("records") != records - 1):
                raise ValueError("Survey backup integrity check failed.")
        return validate_backup(upgrade_restore_backup(payload))

    def _fresh_restore_store(self):
        staging = Tracker(self.path + ".restore", autoload=False, point_store_autoload=False)
        staging.point_store._load_manifest(recover=False)
        self._remove_storage(staging)
        return Tracker(self.path + ".restore")

    def restore_ndjson(self, lines):
        return self._finish_steps(self._restore_ndjson_steps(lines))

    async def restore_ndjson_async(self, lines):
        return await self._finish_steps_async(self._restore_ndjson_steps(lines))

    def _restore_ndjson_steps(self, lines):
        """Validate and stage an NDJSON backup without building a full payload."""
        staging = self._fresh_restore_store()
        staging._batch_restore = True
        digest, integrity, records = hashlib.sha256(), None, 0
        schema, footer, current_line = None, None, None
        project_ids, point_count, ids = [], 0, set()

        def finish_line():
            nonlocal current_line
            if not current_line: return
            state = current_line.get("state")
            if state == "paused":
                staging._append({"event":"line_paused",
                    "project_id":current_line["project_id"],
                    "line_id":current_line["id"]})
            elif state == "finished":
                staging._append({"event":"line_finished",
                    "project_id":current_line["project_id"],
                    "line_id":current_line["id"],
                    "at":current_line.get("finished_at"),
                    "diagnostics":current_line.get("finish_diagnostics")})
            current_line = None

        try:
            for raw in lines:
                if not raw or not raw.strip(): continue
                records += 1
                if records > self._feature_limit() + MAX_LINES_PER_PROJECT * MAX_PROJECTS + MAX_PROJECTS + 3:
                    raise ValueError("Survey backup contains too many records.")
                value = _expanded_archive_record(ujson.loads(raw)); kind = value.get("record")
                if kind == "integrity":
                    if integrity is not None: raise ValueError("Duplicate integrity record.")
                    integrity = value; continue
                if integrity is not None: raise ValueError("Records follow integrity record.")
                digest.update(raw.encode("utf-8"))
                if kind != "header" and schema is None:
                    raise ValueError("Survey backup header must come first.")
                if footer is not None:
                    raise ValueError("Records follow the backup footer.")
                if kind == "header":
                    if schema is not None: raise ValueError("Duplicate backup header.")
                    schema = value.get("schema_version")
                    if value.get("kind") != "survey_backup" or schema not in (2,3,4):
                        raise ValueError("Survey backup header is invalid.")
                    profiles = value.get("profiles", [])
                    validate_backup({"kind":"survey_backup", "schema_version":SCHEMA_VERSION,
                                     "projects":[], "profiles":profiles}, copy_result=False)
                    staging.profiles = dict((p["id"],p) for p in profiles)
                elif kind == "project":
                    finish_line(); project = dict(value.get("project") or {})
                    if project.get("id") in ids: raise ValueError("Duplicate project ID.")
                    ids.add(project.get("id"))
                    project["lines"],project["points"]=[],[]
                    staging._append({"event":"project_created","project":project})
                    project_ids.append(project.get("id"))
                elif kind == "line":
                    finish_line(); line = dict(value.get("line") or {})
                    if line.get("id") in ids: raise ValueError("Duplicate line ID.")
                    ids.add(line.get("id"))
                    if value.get("project_id") not in project_ids: raise ValueError("Unknown project.")
                    wanted = dict(line); line["coordinates"],line["measurements"]=[],[]
                    staging._append({"event":"line_started","project_id":value.get("project_id"),
                        "line":line,"auto_distance_m":0})
                    wanted["project_id"] = value.get("project_id"); current_line = wanted
                elif kind == "vertex":
                    if not current_line or current_line["id"] != value.get("line_id"):
                        raise ValueError("A vertex precedes its line.")
                    if value.get("project_id") != current_line["project_id"]:
                        raise ValueError("Vertex belongs to another project.")
                    _validate_coordinate(value.get("coordinate"))
                    _validate_measurement(value.get("measurement"))
                    if value["measurement"].get("profile_id") and value["measurement"]["profile_id"] not in staging.profiles:
                        raise ValueError("Unknown measurement profile.")
                    staging._append({"event":"vertex_added","project_id":value.get("project_id"),
                        "line_id":value.get("line_id"),"coordinate":value.get("coordinate"),
                        "measurement":value.get("measurement") or {}},defer_index=True)
                    point_count += 1
                elif kind == "asset":
                    finish_line()
                    point = value.get("point") or {}
                    if point.get("id") in ids: raise ValueError("Duplicate asset ID.")
                    ids.add(point.get("id"))
                    if value.get("project_id") not in project_ids: raise ValueError("Unknown project.")
                    staging._append({"event":"asset_added",
                        "project_id":value.get("project_id"),"point":value.get("point")},
                        defer_index=True); point_count += 1
                elif kind == "footer":
                    finish_line(); footer = value
                else: raise ValueError("Survey backup record is invalid.")
                if kind != "vertex" or records % 4 == 0: yield None
            finish_line()
            if schema is None or footer is None: raise ValueError("Survey backup is incomplete.")
            if schema == SCHEMA_VERSION:
                if (not integrity or integrity.get("algorithm") != "sha256" or
                        integrity.get("content_sha256") != self._hex_digest(digest) or
                        integrity.get("records") != records - 1):
                    raise ValueError("Survey backup integrity check failed.")
            if point_count > self._feature_limit(): raise ValueError("Survey backup exceeds active limit.")
            staging.active_project_id = footer.get("active_project_id")
            staging.active_line_id = footer.get("active_line_id")
            staging.auto_distance_m = footer.get("auto_distance_m",1.0)
            metadata = {"kind":"survey_backup", "schema_version":SCHEMA_VERSION,
                        "projects":[], "profiles":list(staging.profiles.values()),
                        "active_project_id":staging.active_project_id,
                        "active_line_id":staging.active_line_id,
                        "auto_distance_m":staging.auto_distance_m}
            for project in staging.projects:
                view = dict(project)
                view["lines"] = [dict(line, coordinates=[], measurements=[])
                                 for line in project["lines"]]
                metadata["projects"].append(view)
            validate_backup(metadata, copy_result=False)
            staging.point_store.sync(); yield None
            staging._write_checkpoint(); yield None
            staging._write_index(); yield None
            expected = {"projects":project_ids,"features":point_count,
                        "active_project_id":staging.active_project_id,
                        "active_line_id":staging.active_line_id}
            for _ in self._commit_staged_restore_steps(staging, expected): yield None
            yield {"schema_version":SCHEMA_VERSION,"projects":len(project_ids),
                   "features":point_count}
        finally:
            self._remove_storage(staging)
            close = getattr(lines, "close", None)
            if close: close()

    def restore(self, payload):
        self.operation_progress.update(operation="restore", state="validating",
                                       processed=0, total=0, error=None)
        try:
            payload = validate_backup(upgrade_restore_backup(payload))
        except Exception as error:
            self.operation_progress.update(state="failed", error=str(error))
            raise
        self.operation_progress["total"] = sum(len(project["points"]) + sum(
            len(line["coordinates"]) for line in project["lines"])
            for project in payload["projects"])
        self.operation_progress["state"] = "writing"
        record_order = 0
        ordered = []
        for project in payload["projects"]:
            for line in project["lines"]:
                for measurement in line["measurements"]:
                    ordered.append((measurement.get("recorded_at", 0), measurement))
            for point in project["points"]:
                ordered.append((point["measurement"].get("recorded_at", 0),
                                point["measurement"]))
        for _recorded_at, measurement in sorted(ordered, key=lambda item: item[0]):
            record_order += 1
            measurement["record_order"] = record_order
        self._replace_with_events(payload)
        self.operation_progress.update(state="complete",
                                       processed=self.operation_progress["total"])

    async def restore_async(self, payload, batch_size=4):
        """Restore while yielding between flash batches so the WDT stays serviced."""
        import uasyncio as asyncio
        self.operation_progress.update(operation="restore", state="validating",
                                       processed=0, total=0, error=None)
        staging = None
        try:
            await asyncio.sleep_ms(10)
            payload = upgrade_restore_backup(payload, copy_result=False)
            await asyncio.sleep_ms(10)
            payload = validate_backup(payload, copy_result=False)
            await asyncio.sleep_ms(10)
            self.operation_progress["total"] = sum(len(project["points"]) + sum(
                len(line["coordinates"]) for line in project["lines"])
                for project in payload["projects"])
            record_order, ordered = 0, []
            for project in payload["projects"]:
                for line in project["lines"]:
                    for measurement in line["measurements"]:
                        ordered.append((measurement.get("recorded_at", 0), measurement))
                for point in project["points"]:
                    ordered.append((point["measurement"].get("recorded_at", 0),
                                    point["measurement"]))
            for _recorded_at, measurement in sorted(ordered, key=lambda item: item[0]):
                record_order += 1
                measurement["record_order"] = record_order

            self.operation_progress["state"] = "writing"
            staging = self._fresh_restore_store()
            staging._batch_restore = True
            for index, event in enumerate(self._events_for_backup(payload), 1):
                staging._append(event, defer_index=True)
                if event.get("event") in ("vertex_added", "asset_added"):
                    self.operation_progress["processed"] += 1
                if index % max(1, batch_size) == 0:
                    await asyncio.sleep_ms(0)
            staging._write_index()
            await asyncio.sleep_ms(10)
            staging._write_checkpoint()
            await asyncio.sleep_ms(10)
            staging._write_index()
            await asyncio.sleep_ms(10)
            expected = {"features": self.operation_progress["total"],
                        "projects": [p["id"] for p in payload["projects"]],
                        "active_project_id": staging.active_project_id,
                        "active_line_id": staging.active_line_id}
            await self._finish_steps_async(self._commit_staged_restore_steps(staging, expected))
            await asyncio.sleep_ms(10)
            self.operation_progress.update(state="complete",
                                           processed=self.operation_progress["total"])
        except Exception as error:
            self.operation_progress.update(state="failed", error=str(error))
            raise
        finally:
            if staging is not None: self._remove_storage(staging)

    def _events_for_backup(self, payload):
        events = []
        for project in payload["projects"]:
            base_project = dict(project)
            base_project["lines"], base_project["points"] = [], []
            events.append({"event": "project_created", "project": base_project})
            for line in project["lines"]:
                base_line = dict(line)
                coordinates = base_line.pop("coordinates")
                measurements = base_line.pop("measurements")
                wanted_state = base_line.get("state")
                base_line["state"] = "recording"
                base_line["finished_at"] = None
                base_line["finish_diagnostics"] = None
                base_line["coordinates"], base_line["measurements"] = [], []
                events.append({"event": "line_started", "project_id": project["id"],
                               "line": base_line,
                               "auto_distance_m": payload.get("auto_distance_m", 1.0)})
                for coordinate, measurement in zip(coordinates, measurements):
                    measurement = dict(measurement)
                    measurement.pop("record_order", None)
                    events.append({"event": "vertex_added", "project_id": project["id"],
                                   "line_id": line["id"], "coordinate": coordinate,
                                   "measurement": measurement})
                if wanted_state == "paused":
                    events.append({"event": "line_paused", "project_id": project["id"],
                                   "line_id": line["id"]})
                elif wanted_state == "finished":
                    events.append({"event": "line_finished", "project_id": project["id"],
                                   "line_id": line["id"], "at": line.get("finished_at"),
                                   "diagnostics": line.get("finish_diagnostics")})
            for point in project["points"]:
                point = dict(point)
                point["measurement"] = dict(point["measurement"])
                point["measurement"].pop("record_order", None)
                events.append({"event": "asset_added", "project_id": project["id"],
                               "point": point})
        if payload.get("active_project_id"):
            events.append({"event": "project_selected",
                           "project_id": payload["active_project_id"]})
        active_line = payload.get("active_line_id")
        if active_line:
            events.append({"event": "line_selected",
                           "project_id": payload["active_project_id"],
                           "line_id": active_line})
        return events

    def _remove_storage(self, tracker, suffix=""):
        if not suffix:
            tracker.point_store.remove_all()
        for _number, path in tracker._segment_files():
            try: os.remove(path + suffix)
            except OSError: pass
            if not suffix:
                try: os.remove(path)
                except OSError: pass
        for path in (tracker.index_path, tracker.index_path + ".tmp",
                     tracker.index_path + ".previous", tracker.checkpoint_path,
                     tracker.checkpoint_path + ".tmp",
                     tracker.checkpoint_path + ".previous"):
            try: os.remove(path + suffix)
            except OSError: pass
            if not suffix:
                try: os.remove(path)
                except OSError: pass

    def _replace_with_events(self, payload):
        staging = self._fresh_restore_store()
        for event in self._events_for_backup(payload):
            staging._append(event)
        with open(staging.point_path, "a"):
            pass
        staging._write_checkpoint()
        staging._write_index()
        self._commit_staged_restore(staging, payload)

    def _commit_staged_restore_steps(self, staging, expected):
        staging._sync_journal(close=True)
        staging.point_store.close()
        probe = FlashPointStore(staging.point_path, autoload=False)
        if not probe._load_manifest(recover=False):
            if staging.feature_count(): raise ValueError("Missing staged point manifest.")
        elif probe.segments:
            scan = probe._scan_generation_steps(strict=True)
            try:
                for _ in scan: yield None
            finally: scan.close()
        if (probe.max_order != staging.point_store.max_order or
                {key: list(v) for key, v in probe.offsets.items() if v} !=
                {key: list(v) for key, v in staging.point_store.offsets.items() if v}):
            raise ValueError("Staged point-store verification failed.")
        # Move verified point segments to unused generation names while the
        # current manifest still references the complete original data. Each
        # rename can involve a flash commit, so service the scheduler between
        # files rather than putting the whole generation in the atomic section.
        generation = ((self.point_store.generation + 1) & 0xffffffff) or 1
        moved, cleanup = [], []
        committed = False
        try:
            for number in staging.point_store.segments:
                target = self.point_store._segment_path(number, generation)
                os.rename(staging.point_store._segment_path(number), target)
                moved.append(target)
                yield None
            activation = self._activate_staged_restore_steps(staging, expected,
                copy_validation=False, verified_store=probe,
                prepared_segments=(generation, moved), cleanup_paths=cleanup)
            try:
                for _ in activation: yield None
            finally: activation.close()
            committed = True
            yield None
            while cleanup:
                try: os.remove(cleanup.pop())
                except OSError: pass
                yield None
        finally:
            # Cancellation before publication must leave the original manifest
            # and its files intact; after publication only obsolete files remain.
            for path in (cleanup if committed or self.point_store.generation == generation else moved):
                try: os.remove(path)
                except OSError: pass

    def _commit_staged_restore(self, staging, payload, copy_validation=True, verified_store=None,
                               prepared_segments=None, cleanup_paths=None):
        return self._finish_steps(self._activate_staged_restore_steps(staging, payload,
            copy_validation, verified_store, prepared_segments, cleanup_paths))

    def _activate_staged_restore_steps(self, staging, payload, copy_validation=True, verified_store=None,
                               prepared_segments=None, cleanup_paths=None):
        staging._sync_journal(close=True)
        self._sync_journal(close=True)
        staging.point_store.close()
        self.point_store.close()
        yield None
        streaming_summary = "features" in payload and "projects" in payload and (
            not payload["projects"] or isinstance(payload["projects"][0], str))
        staged_backup = (None if streaming_summary else
            validate_backup(staging.backup(), copy_result=copy_validation))
        expected_features = (sum(len(project["points"]) + sum(
            len(line["coordinates"]) for line in project["lines"])
            for project in payload["projects"]) if not streaming_summary
            else int(payload["features"]))
        if (staging.feature_count() != expected_features or
                [project["id"] for project in (staged_backup["projects"] if staged_backup
                    else staging.projects)] != ([project["id"] for project in payload["projects"]]
                    if not streaming_summary else payload["projects"]) or
                (staged_backup or staging.__dict__).get("active_project_id") != payload.get("active_project_id") or
                (staged_backup or staging.__dict__).get("active_line_id") != payload.get("active_line_id")):
            self._remove_storage(staging)
            raise ValueError("The staged survey restore could not be verified.")

        old_segments = self._segment_files()
        moved_old, moved_new_journals = [], []
        installed_index = installed_checkpoint = False
        try:
            for _number, path in old_segments:
                previous = path + ".previous"
                try: os.remove(previous)
                except OSError: pass
                os.rename(path, previous); moved_old.append((path, previous))
                yield None
            try:
                os.rename(self.index_path, self.index_path + ".previous")
                moved_index = True
            except OSError:
                moved_index = False
            yield None
            try:
                os.rename(self.checkpoint_path, self.checkpoint_path + ".previous")
                moved_checkpoint = True
            except OSError:
                moved_checkpoint = False
            yield None
            old_point_generation = self.point_store.generation
            old_point_segments = list(self.point_store.segments)
            old_point_strings = list(self.point_store.strings)
            new_point_generation = ((old_point_generation + 1) & 0xffffffff) or 1
            moved_point_segments = []
            if prepared_segments is not None:
                if prepared_segments[0] != new_point_generation:
                    raise ValueError("Point-store generation changed during restore.")
                moved_point_segments = prepared_segments[1]
            else:
                for number in staging.point_store.segments:
                    target = self.point_store._segment_path(number, new_point_generation)
                    os.rename(staging.point_store._segment_path(number), target)
                    moved_point_segments.append(target)
                    yield None
            for number, path in staging._segment_files():
                target = self._segment_path(number)
                os.rename(path, target); moved_new_journals.append(target)
                yield None
            os.rename(staging.index_path, self.index_path); installed_index = True
            yield None
            os.rename(staging.checkpoint_path, self.checkpoint_path); installed_checkpoint = True
            yield None
            self.point_store.strings = list(staging.point_store.strings)
            self.point_store._string_codes = dict((item, index) for index, item in
                                                  enumerate(self.point_store.strings))
            self.point_store._activate_manifest(
                new_point_generation, staging.point_store.segments)
        except BaseException:
            for path in moved_new_journals:
                try: os.remove(path)
                except OSError: pass
            if installed_index:
                try: os.remove(self.index_path)
                except OSError: pass
            if installed_checkpoint:
                try: os.remove(self.checkpoint_path)
                except OSError: pass
            for path, previous in moved_old:
                try: os.rename(previous, path)
                except OSError: pass
            if 'moved_index' in locals() and moved_index:
                try: os.rename(self.index_path + ".previous", self.index_path)
                except OSError: pass
            if 'moved_checkpoint' in locals() and moved_checkpoint:
                try: os.rename(self.checkpoint_path + ".previous", self.checkpoint_path)
                except OSError: pass
            for path in locals().get('moved_point_segments', []):
                try: os.remove(path)
                except OSError: pass
            if 'old_point_strings' in locals():
                self.point_store.strings = old_point_strings
                self.point_store._string_codes = dict((item, index) for index, item in
                                                      enumerate(old_point_strings))
            raise
        obsolete = [previous for _path, previous in moved_old]
        obsolete.extend((self.index_path + ".previous", self.checkpoint_path + ".previous"))
        obsolete.extend(self.point_store._segment_path(number, old_point_generation)
                        for number in old_point_segments)
        if cleanup_paths is not None:
            cleanup_paths.extend(obsolete)
        else:
            for path in obsolete:
                try: os.remove(path)
                except OSError: pass
        self.projects = staging.projects
        self.profiles = staging.profiles
        if verified_store is None:
            self.point_store = FlashPointStore(self.point_path)
        else:
            self.point_store = verified_store
            self.point_store.path = self.point_path
            self.point_store.manifest_path = self.point_path + ".segments.json"
            self.point_store.generation = new_point_generation
            self.point_store._verified_prefix = None
        self.point_store.reserve_check = self._pointstore_rotation_allowed
        self._pack_projects()
        self._project_activity = staging._project_activity
        self._line_lengths = staging._line_lengths
        self._status_cache = None
        self._map_changes = []
        self._map_change_floor = staging._event_sequence + 1
        self.active_project_id = staging.active_project_id
        self.active_line_id = staging.active_line_id
        self.auto_distance_m = staging.auto_distance_m
        self._event_sequence = staging._event_sequence
        self._checkpoint_order = staging._checkpoint_order
        self._segment_number = staging._segment_number
        self._index_dirty_events = 0
        if cleanup_paths is None: self._remove_storage(staging)

    def _largest_record_order(self):
        largest = 0
        for project in self.projects:
            for point in project.get("points", []):
                largest = max(largest, int(point.get("measurement", {}).get(
                    "record_order", 0)))
            for line in project.get("lines", []):
                for measurement in line.get("measurements", []):
                    largest = max(largest, int(measurement.get("record_order", 0)))
        return largest

    def compact(self):
        """Activate a verified checkpoint, then retire only covered segments."""
        self._write_checkpoint()
        self._write_index()
        probe = Tracker(self.path, autoload=False)
        if probe._load_checkpoint() != self._checkpoint_order:
            raise ValueError("Checkpoint verification failed; journal retained.")
        # Once the checkpoint and its point-store high-water mark have been
        # read back, covered journal segments are redundant. Releasing them
        # before the optional store rewrite materially lowers temporary flash
        # pressure while leaving the verified checkpoint and old store intact.
        self._sync_journal(close=True)
        for number, path in self._segment_files():
            if number <= self._checkpoint_segment:
                try: os.remove(path)
                except OSError: pass
        active_lines = [line["id"] for project in self.projects
                        for line in project.get("lines", [])]
        temporary = self.point_store.rewrite_temporary_bytes(active_lines)
        if not self.storage_capacity(temporary, force=True)["writable"]:
            raise OSError("Point-store rewrite would violate the flash reserve.")
        self.point_store.rewrite(active_lines)
        self._pack_projects()

    async def compact_async(self):
        """Checkpoint in scheduler-sized phases while preserving sync semantics."""
        import uasyncio as asyncio
        if self._compacting:
            raise ValueError("Storage maintenance already in progress.")
        self._compacting = True
        try:
            while self.storage_readers:
                await asyncio.sleep_ms(20)
            expected_event = self._event_sequence
            expected_points = self.point_store._verification_state()
            self._write_checkpoint()
            expected_order = self._checkpoint_order
            covered_segment = self._checkpoint_segment

            def unchanged():
                if (self._event_sequence != expected_event or
                        self._checkpoint_order != expected_order or
                        self._checkpoint_segment != covered_segment or
                        self.point_store._verification_state() != expected_points):
                    raise ValueError("Checkpoint changed; remaining journal retained.")
            # Let complete socket request/response chains and UART draining run
            # before the next potentially slow flash commit.
            await asyncio.sleep_ms(CHECKPOINT_IO_SERVICE_MS)
            phase = self._ticks_us()
            self._write_index()
            self._profile_add("checkpoint_index", phase)
            await asyncio.sleep_ms(CHECKPOINT_IO_SERVICE_MS)
            phase = self._ticks_us()
            probe = Tracker(self.path, autoload=False, point_store_autoload=False)
            try:
                await probe.point_store.load_async(verified_store=self.point_store)
                checkpoint_started = self._ticks_us()
                verified_order = probe._load_checkpoint(readonly=True)
                probe.point_store.scan_profile["checkpoint_load_us"] = max(
                    0, time.ticks_diff(self._ticks_us(), checkpoint_started))
                if verified_order != expected_order:
                    raise ValueError("Checkpoint verification failed; journal retained.")
                unchanged()
            finally:
                self.checkpoint_scan_profile = dict(probe.point_store.scan_profile)
                probe.point_store.close()
            self._profile_add("checkpoint_verify", phase)
            await asyncio.sleep_ms(1)
            unchanged()
            self._sync_journal(close=True)
            for number, path in self._segment_files():
                unchanged()
                if number <= covered_segment:
                    try: os.remove(path)
                    except OSError: pass
                await asyncio.sleep_ms(1)
            active_lines = [line["id"] for project in self.projects
                            for line in project.get("lines", [])]
            temporary = self.point_store.rewrite_temporary_bytes(active_lines)
            if not self.storage_capacity(temporary, force=True)["writable"]:
                raise OSError("Point-store rewrite would violate the flash reserve.")
            await asyncio.sleep_ms(1)
            unchanged()
            phase = self._ticks_us()
            self.point_store.accept_verification(probe.point_store)
            await self.point_store.rewrite_async(active_lines, unchanged)
            self._pack_projects()
            self._profile_add("checkpoint_rewrite", phase)
        finally:
            self._compacting = False

    def start_line(self, name, auto_distance_m=1.0, properties=None, fix=None,
                   recording_mode="survey"):
        project = self._project()
        if not project:
            raise ValueError("Select a project first.")
        if self.active_line_id:
            raise ValueError("Finish the active line first.")
        self._require_write_capacity(force=True)
        if len(project["lines"]) >= MAX_LINES_PER_PROJECT:
            raise ValueError("Line limit reached.")
        distance = float(auto_distance_m or 0)
        if distance and not 0.2 <= distance <= 100:
            raise ValueError("Automatic point distance must be between 0.2 and 100 metres.")
        properties = properties if properties is not None else {}
        _validate_properties(properties)
        if recording_mode not in ("survey", "route"):
            raise ValueError("Recording mode must be survey or route.")
        if fix:
            self._measurement(fix, allow_rejected=recording_mode == "route")
        line = {"id": self._id("line"), "name": _clean_text(name),
                "state": "recording", "created_at": int(time.time()),
                "finished_at": None, "coordinates": [], "measurements": [],
                "properties": properties, "recording_mode": recording_mode}
        if not line["name"]:
            raise ValueError("Line name is required.")
        self._append({"event": "line_started", "project_id": project["id"],
                      "line": line, "auto_distance_m": distance})
        if fix:
            self.add_vertex(fix, source="start")
        return line

    def add_vertex(self, fix, source="manual"):
        project, line = self._project(), self._line(self._project())
        if not line or line["state"] != "recording":
            raise ValueError("No line is currently recording.")
        if self.feature_count() >= self._feature_limit():
            if source == "automatic":
                self._pause_for_capacity()
            raise ValueError("Tracking point limit reached.")
        self._require_write_capacity(source == "automatic")
        route_automatic = (line.get("recording_mode") == "route" and
                           source in ("automatic", "start"))
        if route_automatic and fix.get("route_ready") is False:
            raise ValueError("Route position is still stabilizing after receiver startup.")
        measurement = self._measurement(fix, allow_rejected=route_automatic)
        coordinate = self._coordinate(fix)
        _validate_coordinate(coordinate)
        measurement["source"] = source
        self._append({"event": "vertex_added", "project_id": project["id"],
                      "line_id": line["id"], "coordinate": coordinate,
                      "measurement": measurement})
        if route_automatic and not measurement["quality"]["accepted"]:
            try:
                from state import app
                app.stats["route_rejected_points"] = (
                    app.stats.get("route_rejected_points", 0) + 1)
            except (ImportError, AttributeError):
                pass
        return measurement

    def add_asset(self, object_type, name, note, fix, link_to_active_line=True):
        project = self._project()
        if not project:
            raise ValueError("Select a project first.")
        if self.feature_count() >= self._feature_limit():
            raise ValueError("Tracking point limit reached.")
        self._require_write_capacity(False)
        if object_type not in OBJECT_TYPES:
            raise ValueError("Unknown object type.")
        measurement = self._measurement(fix)
        coordinate = self._coordinate(fix)
        line = self._line(project) if link_to_active_line else None
        relation = project_to_line(line["coordinates"], coordinate) if line else None
        clean_name = _clean_text(name)
        if not clean_name:
            prefix = "K" if object_type == "control" else "O"
            used = set(item.get("name") for item in project["points"])
            number = 1
            while "%s-%d" % (prefix, number) in used:
                number += 1
            clean_name = "%s-%d" % (prefix, number)
        point = {"id": self._id("point"), "object_type": object_type,
                 "name": clean_name, "note": _clean_text(note, 500),
                 "line_id": line["id"] if line else None,
                 "chainage_m": relation["chainage_m"] if relation else None,
                 "lateral_offset_m": relation["lateral_offset_m"] if relation else None,
                 "side": relation["side"] if relation else None,
                 "projected_coordinate": relation["projected_coordinate"] if relation else None,
                 "coordinate": coordinate, "measurement": measurement}
        self._append({"event": "asset_added", "project_id": project["id"],
                      "point": point})
        return point

    def _pause_for_capacity(self, reason="capacity_limit"):
        line = self._line(self._project())
        if line and line.get("state") == "recording":
            self._pending_fixes = []
            self._last_queued_epoch = None
            self._last_queued_coordinate = None
            self._append({"event": "line_paused", "project_id": self.active_project_id,
                          "line_id": line["id"], "at": int(time.time()),
                          "reason": reason})

    def set_line_state(self, action, diagnostics=None):
        project, line = self._project(), self._line(self._project())
        if not line:
            raise ValueError("No active line exists.")
        mapping = {"pause": "line_paused", "resume": "line_resumed",
                   "finish": "line_finished"}
        if action not in mapping:
            raise ValueError("Unknown line action.")
        if action == "resume" and self.feature_count() >= self._feature_limit():
            raise ValueError("Archive or delete survey data before resuming the line.")
        if action == "resume":
            self._require_write_capacity(False)
        event = {"event": mapping[action], "project_id": project["id"],
                 "line_id": line["id"], "at": int(time.time())}
        if action == "finish" and diagnostics:
            event["diagnostics"] = diagnostics
        self._append(event)

    def _undo_candidate(self, project):
        candidates = []
        for point in project["points"]:
            candidates.append((point["measurement"].get("record_order", 0),
                               "asset", point["id"], None))
        for line in project["lines"]:
            if line.get("state") != "finished" and line["measurements"]:
                measurement = line["measurements"][-1]
                candidates.append((measurement.get("record_order", 0),
                                   "vertex", line["id"], line["id"]))
        if not candidates:
            return None
        _at, kind, feature_id, line_id = sorted(candidates)[-1]
        if kind == "asset":
            item = next((value for value in project["points"]
                         if value["id"] == feature_id), None)
            label = (item or {}).get("name") or "Object point"
        else:
            line = self._line(project, line_id)
            label = "%s · V-%d" % ((line or {}).get("name", "Line"),
                                     len((line or {}).get("measurements", [])))
        return {"kind": kind, "feature_id": feature_id, "line_id": line_id,
                "label": label}

    def undo_last(self):
        project = self._project()
        if not project:
            raise ValueError("Select a project first.")
        candidate = self._undo_candidate(project)
        if not candidate:
            raise ValueError("No recorded point can be undone.")
        self._append({"event": "feature_removed", "project_id": project["id"],
                      "feature_kind": candidate["kind"],
                      "feature_id": candidate["feature_id"],
                      "line_id": candidate["line_id"], "at": int(time.time())})

    def map_data(self, limit=500, project_id=None, since_revision=None):
        return ujson.loads("".join(self.iter_map_data(limit, project_id, since_revision)))

    def iter_map_data(self, limit=500, project_id=None, since_revision=None):
        """Emit a map incrementally so HTTP can yield between flash reads."""
        limit = max(1, min(int(limit), 500))
        project = self._project(project_id)
        if not project:
            yield ujson.dumps({"project_id": project_id, "lines": [], "points": [],
                               "truncated": False, "revision": self._event_sequence})
            return
        if since_revision is not None:
            since_revision = max(0, int(since_revision))
            if since_revision >= self._map_change_floor:
                yield ujson.dumps({"project_id": project["id"], "revision": self._event_sequence,
                        "changes": [dict(change) for change in self._map_changes
                                    if change["revision"] > since_revision and
                                    change.get("project_id") == project["id"]],
                        "delta": True})
                return
        # Capture the population before yielding; points appended while a map
        # is transmitted are subsequently available through its revision delta.
        lines = [(line, len(line["coordinates"]), self._iter_line_summary(line))
                 for line in project["lines"]]
        points = list(project["points"][-limit:])
        point_count = len(project["points"])
        revision = self._event_sequence
        truncated = False
        yield '{"project_id":%s,"revision":%s,"lines":[' % (
            ujson.dumps(project["id"]), revision)
        for line_index, (line, count, summary_steps) in enumerate(lines):
            if line_index:
                yield ","
            try:
                for summary in summary_steps:
                    if summary is None:
                        yield " "
            finally:
                summary_steps.close()
            coordinates = line["coordinates"]
            step = max(1, count // limit)
            selected_count = (count + step - 1) // step
            start = max(0, selected_count - limit) * step
            truncated = truncated or min(selected_count, limit) < count
            metadata = {"id": summary["id"], "name": summary["name"],
                        "state": summary["state"], "vertex_count": count,
                        "length_m": summary["length_m"]}
            yield ujson.dumps(metadata)[:-1] + ',"coordinates":['
            rows = (coordinates.store.iter_line(coordinates.line_id, start, count, step,
                                                include_measurement=False)
                    if isinstance(coordinates, FlashSequence) else None)
            try:
                for index in range(start, count, step):
                    coordinate = next(rows)["coordinate"] if rows is not None else coordinates[index]
                    yield ("," if index != start else "") + ujson.dumps(coordinate)
            finally:
                if rows is not None:
                    rows.close()
            yield "]}"
        yield '],"points":['
        truncated = truncated or len(points) < point_count
        for index, point in enumerate(points):
            yield ("," if index else "") + ujson.dumps({"id": point["id"], "type": point["object_type"],
             "name": point["name"], "note": point.get("note", ""),
             "coordinate": point["coordinate"], "line_id": point.get("line_id"),
             "chainage_m": point["chainage_m"],
             "recorded_at": point.get("measurement", {}).get("recorded_at"),
             "receiver_accuracy": point.get("measurement", {}).get("receiver_accuracy"),
             "measurement": {key: point.get("measurement", {}).get(key)
                             for key in ("fix_quality", "fix_status", "satellites",
                                         "hdop", "correction_age_sec", "station_id",
                                         "accuracy_observation", "receiver_accuracy",
                                         "quality")}})
        yield '],"truncated":%s}' % ("true" if truncated else "false")

    def on_fix(self, fix):
        if not self._loaded:
            return False
        line = self._line(self._project())
        if not line or line["state"] != "recording" or not self.auto_distance_m:
            return False
        route = line.get("recording_mode") == "route"
        if route and fix.get("route_ready") is False:
            return False
        if not route and not quality_check(fix)["accepted"]:
            return False
        try:
            coordinate = self._coordinate(fix)
            _validate_coordinate(coordinate)
        except (ValueError, TypeError, KeyError):
            if route:
                try:
                    from state import app
                    app.stats["route_invalid_fixes"] = (
                        app.stats.get("route_invalid_fixes", 0) + 1)
                except (ImportError, AttributeError):
                    pass
            return False
        if line["coordinates"] and _distance_m(line["coordinates"][-1], coordinate) < self.auto_distance_m:
            return False
        self.add_vertex(fix, source="automatic")
        return True

    def enqueue_fix(self, fix):
        """Validate and queue an automatic point without touching flash."""
        if not self._loaded:
            return False
        line = self._line(self._project())
        if not line or line["state"] != "recording" or not self.auto_distance_m:
            return False
        route = line.get("recording_mode") == "route"
        if route and fix.get("route_ready") is False:
            return False
        if not route and not quality_check(fix)["accepted"]:
            return False
        epoch = fix.get("utc")
        try:
            coordinate = self._coordinate(fix)
            _validate_coordinate(coordinate)
        except (ValueError, TypeError, KeyError):
            if route:
                try:
                    from state import app
                    app.stats["route_invalid_fixes"] = (
                        app.stats.get("route_invalid_fixes", 0) + 1)
                except (ImportError, AttributeError):
                    pass
            return False
        if epoch is not None and epoch == self._last_queued_epoch:
            # GST commonly follows GGA. Enrich the queued point instead of
            # discarding its receiver accuracy as a duplicate position.
            if fix.get("receiver_accuracy"):
                for queued in reversed(self._pending_fixes):
                    if queued.get("utc") == epoch:
                        queued["receiver_accuracy"] = dict(fix["receiver_accuracy"])
                        break
            return False
        previous = (self._last_queued_coordinate or
                    (line["coordinates"][-1] if line["coordinates"] else None))
        if previous and _distance_m(previous, coordinate) < self.auto_distance_m:
            return False
        if len(self._pending_fixes) >= 64:
            return None
        queued = dict(fix)
        if route and fix.get("raw") and not fix.get("receiver_accuracy"):
            queued["_gst_wait_started_ms"] = time.ticks_ms()
        self._pending_fixes.append(queued)
        self._last_queued_epoch = epoch
        self._last_queued_coordinate = coordinate
        return True

    async def run_worker(self):
        import uasyncio as asyncio
        from state import app, shutdown_event
        while not shutdown_event.is_set():
            if self.storage_busy:
                await asyncio.sleep_ms(50)
                continue
            if not self._pending_fixes:
                app.stats["tracking_queue_depth"] = 0
                if (self._index_dirty_events and self._index_dirty_since is not None and
                        time.ticks_diff(time.ticks_ms(), self._index_dirty_since) >= 15000):
                    started = time.ticks_ms()
                    try:
                        self._write_index()
                        self._index_dirty_since = None
                        app.stats["tracking_background_index_ms"] = max(
                            0, time.ticks_diff(time.ticks_ms(), started))
                    except Exception as error:
                        app.stats["tracking_write_errors"] = (
                            app.stats.get("tracking_write_errors", 0) + 1)
                        app.log_error("TRACK_INDEX", str(error))
                        self._index_dirty_since = time.ticks_ms()
                if self._checkpoint_dirty_events >= CHECKPOINT_BATCH_EVENTS:
                    started = time.ticks_ms()
                    try:
                        await self.compact_async()
                        self._checkpoint_dirty_events = 0
                        app.stats["tracking_background_checkpoint_ms"] = max(
                            0, time.ticks_diff(time.ticks_ms(), started))
                    except Exception as error:
                        app.stats["tracking_write_errors"] = (
                            app.stats.get("tracking_write_errors", 0) + 1)
                        app.log_error("TRACK_CHECKPOINT", str(error))
                await asyncio.sleep_ms(50)
                continue
            pending = self._pending_fixes[0]
            waiting_since = pending.get("_gst_wait_started_ms")
            if (waiting_since is not None and not pending.get("receiver_accuracy") and
                    time.ticks_diff(time.ticks_ms(), waiting_since) < 400):
                # Bounded grace for same-epoch GST; missing GST never stops a route.
                await asyncio.sleep_ms(20)
                continue
            fix = self._pending_fixes.pop(0)
            app.stats["tracking_queue_depth"] = len(self._pending_fixes)
            try:
                self.add_vertex(fix, source="automatic")
                app.stats["tracking_max_append_ms"] = max(
                    app.stats.get("tracking_max_append_ms", 0), self.last_append_ms)
                if self.last_append_ms > 1000:
                    app.stats["tracking_appends_over_1000ms"] = (
                        app.stats.get("tracking_appends_over_1000ms", 0) + 1)
            except Exception as error:
                app.stats["tracking_write_errors"] = (
                    app.stats.get("tracking_write_errors", 0) + 1)
                app.log_error("TRACK", str(error))
            await asyncio.sleep_ms(0)

    def _line_length(self, line):
        if not line:
            return 0.0
        length_m, previous = 0.0, None
        for coordinate in line["coordinates"]:
            if previous is not None:
                length_m += _distance_m(previous, coordinate)
            previous = coordinate
        return length_m

    def feature_count(self):
        return sum(len(p["points"]) + sum(len(line["coordinates"])
                   for line in p["lines"]) for p in self.projects)

    def capacity(self):
        used = self.feature_count()
        limit = self._feature_limit()
        percent = int((used * 100) / limit) if limit else 100
        notice, warning, critical = ((8000, 9000, 9800) if limit == 10000 else
            ((limit * 80 + 99) // 100, (limit * 90 + 99) // 100,
             (limit * 98 + 99) // 100))
        level = ("full" if used >= limit else "critical" if used >= critical
                 else "warning" if used >= warning else "notice" if used >= notice
                 else "ok")
        result = {"used": used, "limit": limit,
                "remaining": max(0, limit - used),
                "percent": percent, "level": level}
        result.update(self.storage_capacity())
        result["active_projects"] = len(self.projects)
        result["archived_projects"] = len(getattr(self, "archives", []))
        return result

    def name_available(self, name, kind):
        """Check one normalized point or line name without expanding poll payloads."""
        if kind not in ("point", "line"):
            raise ValueError("Name kind must be point or line.")
        wanted = _clean_text(name)
        if not wanted:
            raise ValueError("Name is required.")
        project = self._project()
        if not project:
            return {"available": True, "existing": None}
        items = project.get("points", []) if kind == "point" else project.get("lines", [])
        for item in items:
            if item.get("name") == wanted:
                return {"available": False, "existing": item.get("name")}
        return {"available": True, "existing": None}

    def _project_summary(self, project):
        latest = None
        measurements = []
        for line in project["lines"]:
            if line.get("measurements"):
                measurements.append(line["measurements"][-1])
        for point in project["points"]:
            if point.get("measurement"):
                measurements.append(point["measurement"])
        if measurements:
            latest = max(measurements,
                         key=lambda item: item.get("record_order", 0))
        accepted = ((latest or {}).get("quality") or {}).get("accepted")
        states = [line.get("state") for line in project["lines"]]
        return {"id": project["id"], "name": project["name"],
                "description": project["description"],
                "created_at": project.get("created_at"),
                "updated_at": self._project_activity.get(
                    project["id"], project.get("created_at")),
                "line_count": len(project["lines"]),
                "point_count": len(project["points"]),
                "last_quality_accepted": accepted,
                "progress": ("recording" if "recording" in states else
                             "paused" if "paused" in states else
                             "complete" if states else "empty")}

    def _startup_view(self):
        from state import app
        return {"revision": 0, "device_time": int(time.time()),
                "tracking_startup": dict(app.stats.get("tracking_startup") or {"state": "loading"}),
                "projects": [], "archives": [], "active_project": None,
                "active_project_id": None, "active_project_lines": [],
                "active_line_id": None, "active_line": None, "undo_candidate": None,
                "capacity": {"level": "loading", "used": 0,
                             "limit": self._feature_limit(), "recording_allowed": False,
                             "writable": False}}

    def status(self):
        if not self._loaded:
            return self._startup_view()
        device_time = int(time.time())
        if self._status_cache is not None:
            result = dict(self._status_cache)
            result["device_time"] = device_time
            if result.get("active_line"):
                active = dict(result["active_line"])
                active["recording_sec"] = max(0, device_time - int(
                    active.get("created_at") or device_time))
                if active.get("last_point"):
                    last = dict(active["last_point"])
                    recorded_at = self._status_last_recorded_at
                    if recorded_at is not None:
                        last["age_sec"] = max(0, device_time - int(recorded_at))
                    active["last_point"] = last
                result["active_line"] = active
            return result
        project = self._project()
        line = self._line(project)
        storage_bytes = 0
        for _number, path in self._segment_files():
            try: storage_bytes += os.stat(path)[6]
            except OSError: pass
        try: storage_bytes += os.stat(self.index_path)[6]
        except OSError: pass
        storage_bytes += self.point_store.storage_bytes()
        line_summaries = []
        if project:
            for recorded_line in project["lines"]:
                line_summaries.append(self._line_summary(recorded_line))
        controls = []
        if project:
            grouped = {}
            for point in project.get("points", []):
                if point.get("object_type") == "control" and point.get("name"):
                    grouped.setdefault(point["name"], []).append(point)
            for name, points in grouped.items():
                first, latest = points[0], points[-1]
                controls.append({
                    "name": name, "measurements": len(points),
                    "horizontal_delta_m": round(_distance_m(
                        first["coordinate"], latest["coordinate"]), 4),
                    "vertical_delta_m": round(float(latest["coordinate"][2]) -
                                              float(first["coordinate"][2]), 4),
                    "first_recorded_at": first["measurement"].get("recorded_at"),
                    "latest_recorded_at": latest["measurement"].get("recorded_at"),
                })
        last_point = None
        self._status_last_recorded_at = None
        if line and line.get("measurements"):
            measurement = line["measurements"][-1]
            receiver = measurement.get("receiver_accuracy") or {}
            sigma = receiver.get("horizontal_sigma_mean_m",
                                 receiver.get("horizontal_sigma_m",
                                              receiver.get("semi_major_sigma_m")))
            recorded_at = measurement.get("recorded_at")
            self._status_last_recorded_at = recorded_at
            last_point = {"label": "V-%d" % len(line["measurements"]),
                          "age_sec": (max(0, device_time - int(recorded_at))
                                      if recorded_at is not None else None),
                          "h_sigma_m": sigma}
        result = {"schema_version": SCHEMA_VERSION, "storage_bytes": storage_bytes,
                "capacity": self.capacity(),
                "device_time": device_time,
                "projects": [self._project_summary(p) for p in self.projects],
                "archives": [dict(item) for item in self.archives],
                "active_project_id": self.active_project_id,
            "active_project_lines": line_summaries,
            "control_checks": controls,
            "active_line": ({"id": line["id"], "name": line["name"],
                             "state": line["state"],
                             "recording_mode": line.get("recording_mode", "survey"),
                             "pause_reason": line.get("pause_reason"),
                             "created_at": line.get("created_at"),
                             "recording_sec": max(0, device_time - int(
                                 line.get("created_at") or device_time)),
                             "last_point": last_point,
                             "vertex_count": len(line["coordinates"]),
                             "length_m": round(self._line_lengths.get(line["id"], 0.0), 2),
                             "auto_distance_m": self.auto_distance_m}
                            if line else None)}
        self._status_cache = result
        return dict(result)

    def live_status(self):
        """Return the field-loop state without directory scans or full summaries."""
        if not self._loaded:
            return self._startup_view()
        device_time = int(time.time())
        project = self._project()
        line = self._line(project)
        last_point = None
        if line and line.get("measurements"):
            measurement = line["measurements"][-1]
            receiver = measurement.get("receiver_accuracy") or {}
            sigma = receiver.get("horizontal_sigma_mean_m",
                                 receiver.get("horizontal_sigma_m",
                                              receiver.get("semi_major_sigma_m")))
            recorded_at = measurement.get("recorded_at")
            last_point = {"label": "V-%d" % len(line["measurements"]),
                          "age_sec": (max(0, device_time - int(recorded_at))
                                      if recorded_at is not None else None),
                          "h_sigma_m": sigma}
        active_project = None
        if project:
            active_project = {"id": project["id"], "name": project["name"],
                              "line_count": len(project["lines"]),
                              "point_count": len(project["points"])}
        active_line = None
        if line:
            length = self._line_lengths.get(line["id"])
            if length is None:
                length = self._line_length(line)
                self._line_lengths[line["id"]] = length
            active_line = {"id": line["id"], "name": line["name"],
                           "state": line["state"],
                           "recording_mode": line.get("recording_mode", "survey"),
                           "pause_reason": line.get("pause_reason"),
                           "recording_sec": max(0, device_time - int(
                               line.get("created_at") or device_time)),
                           "vertex_count": len(line["coordinates"]),
                           "length_m": round(length, 2),
                           "last_point": last_point,
                           "auto_distance_m": self.auto_distance_m}
        return {"revision": self._event_sequence, "device_time": device_time,
                "capacity": self.capacity(),
                "persistence": {"last_append_ms": getattr(self, "last_append_ms", 0),
                                "max_append_ms": getattr(self, "max_append_ms", 0),
                                "pending_index_events": self._index_dirty_events},
                "active_project": active_project, "active_line": active_line,
                "undo_candidate": self._undo_candidate(project) if project else None}

    def _line_summary(self, line):
        for result in self._iter_line_summary(line):
            pass
        return result

    def _iter_line_summary(self, line):
        """Capture metadata now; yield one scan step per vertex, then a summary.

        This outer function performs no flash reads, allowing a streaming caller
        to emit its JSON prefix before consuming a missing summary. A cached
        summary needs no scan. The captured counts keep concurrent appends out
        of this response and prevent a partial scan replacing a newer cache.
        """
        coordinates = line["coordinates"]
        measurements = line.get("measurements", [])
        count, measurement_count = len(coordinates), len(measurements)
        revision = self._event_sequence
        result = {"id": line["id"], "name": line["name"], "state": line["state"],
            "created_at": line.get("created_at"), "finished_at": line.get("finished_at"),
            "recording_mode": line.get("recording_mode", "survey"),
            "vertex_count": count, "quality_total_count": measurement_count,
            "finish_diagnostics": line.get("finish_diagnostics")}
        cached = line.get("_summary")
        cached = dict(cached) if cached and cached.get("vertex_count") == count else None

        def steps():
            summary = cached
            if summary is None:
                summary = {"vertex_count": count, "length_m": 0.0,
                    "quality_accepted_count": 0, "large_gap_count": 0,
                    "max_gap_m": 0.0, "large_vertical_step_count": 0,
                    "max_vertical_step_m": 0.0, "overlapping": False}
                previous = None
                rows = (coordinates.store.iter_line(coordinates.line_id)
                        if isinstance(coordinates, FlashSequence) and
                        isinstance(measurements, FlashSequence) and
                        coordinates.store is measurements.store and
                        coordinates.line_id == measurements.line_id else None)
                try:
                    for index in range(max(count, measurement_count)):
                        row = next(rows) if rows is not None else None
                        if index < count:
                            coordinate = row["coordinate"] if row is not None else coordinates[index]
                            if previous is not None:
                                gap = _distance_m(previous, coordinate)
                                vertical = abs(float(coordinate[2]) - float(previous[2]))
                                summary["length_m"] += gap
                                summary["large_gap_count"] += int(gap > MAX_LINE_GAP_M)
                                summary["max_gap_m"] = max(summary["max_gap_m"], gap)
                                summary["large_vertical_step_count"] += int(
                                    vertical > MAX_VERTICAL_STEP_M)
                                summary["max_vertical_step_m"] = max(
                                    summary["max_vertical_step_m"], vertical)
                                summary["overlapping"] = summary["overlapping"] or gap < 0.05
                            previous = coordinate
                        if index < measurement_count:
                            summary["quality_accepted_count"] += int(bool(
                                ((row["measurement"] if row is not None else measurements[index]).get("quality") or {}).get("accepted")))
                        yield None
                finally:
                    if rows is not None:
                        rows.close()
                if (self._event_sequence == revision and len(coordinates) == count and
                        len(measurements) == measurement_count):
                    line["_summary"] = dict(summary)
                    self._line_lengths[line["id"]] = summary["length_m"]
            length_m = float(summary.get("length_m", 0.0))
            warnings = []
            if count < 2: warnings.append("not_enough_line_points")
            if summary.get("overlapping"): warnings.append("overlapping_line_points")
            if count >= 2 and length_m < 0.05:
                warnings.append("line_has_no_horizontal_length")
            if summary.get("large_gap_count"): warnings.append("large_line_gaps")
            if summary.get("large_vertical_step_count"):
                warnings.append("large_vertical_steps")
            result.update({"length_m": round(length_m, 3), "warnings": warnings,
                "quality_accepted_count": summary.get("quality_accepted_count", 0),
                "large_gap_count": summary.get("large_gap_count", 0),
                "max_gap_m": round(summary.get("max_gap_m", 0.0), 3),
                "large_vertical_step_count": summary.get("large_vertical_step_count", 0),
                "max_vertical_step_m": round(summary.get("max_vertical_step_m", 0.0), 3)})
            yield result

        return steps()

    def line_detail(self, line_id, offset=0, limit=25, project_id=None):
        return ujson.loads("".join(self.iter_line_detail(line_id, offset, limit, project_id)))

    def iter_line_detail(self, line_id, offset=0, limit=25, project_id=None):
        """Emit one detail vertex at a time for cooperative HTTP responses."""
        project = self._project(project_id)
        line = self._line(project, line_id)
        if not line:
            raise ValueError("Line not found.")
        offset = max(0, int(offset or 0))
        limit = max(1, min(int(limit or 25), 50))
        summary_steps = self._iter_line_summary(line)
        coordinates = line["coordinates"]
        measurements = line.get("measurements", [])
        end = min(len(measurements), len(coordinates), offset + limit)
        has_more = end < min(len(measurements), len(coordinates))
        yield "{"
        try:
            for result in summary_steps:
                if result is None:
                    yield " "
        finally:
            summary_steps.close()
        result["vertex_offset"] = offset
        result["vertices_returned"] = max(0, end - offset)
        result["has_more_vertices"] = has_more
        yield ujson.dumps(result)[1:-1] + ',"vertices":['
        rows = (coordinates.store.iter_line(coordinates.line_id, max(0, offset - 1), end)
                if isinstance(coordinates, FlashSequence) and
                isinstance(measurements, FlashSequence) and
                coordinates.store is measurements.store and
                coordinates.line_id == measurements.line_id else None)
        try:
            previous = (next(rows)["coordinate"] if offset and offset < end and rows is not None
                        else coordinates[offset - 1] if offset and offset < end else None)
            for index in range(offset, end):
                row = next(rows) if rows is not None else None
                measurement = row["measurement"] if row is not None else measurements[index]
                coordinate = row["coordinate"] if row is not None else coordinates[index]
                yield ("," if index != offset else "") + ujson.dumps({
                    "number": index + 1, "coordinate": coordinate,
                    "segment_length_m": (round(_distance_m(previous, coordinate), 3)
                                         if previous else None),
                    "vertical_step_m": (round(float(coordinate[2]) -
                                               float(previous[2]), 3)
                                        if previous else None),
                    "source": measurement.get("source"),
                    "fix_status": measurement.get("fix_status"),
                    "satellites": measurement.get("satellites"),
                    "hdop": measurement.get("hdop"),
                    "correction_age_sec": measurement.get("correction_age_sec"),
                    "receiver_accuracy": measurement.get("receiver_accuracy"),
                    "accuracy_observation": measurement.get("accuracy_observation"),
                    "quality": self._expand_measurement(measurement).get("quality"),
                })
                previous = coordinate
        finally:
            if rows is not None:
                rows.close()
        yield "]}"

    def _geojson_line_properties(self, line, length_m):
        properties = {"line_id": line["id"], "name": line["name"],
                      "object_type": "survey_line",
                      "recording_mode": line.get("recording_mode", "survey"),
                      "state": line["state"], "length_m": round(length_m, 3)}
        properties.update(line["properties"])
        return properties

    def iter_geojson(self, project_id=None):
        """Yield GeoJSON text without materializing a line's flash sequences.

        Each coordinate, expanded measurement and asset is a separate chunk so
        an HTTP caller can yield to other services between flash reads. Capture
        each line's counts before emitting it so appends during streaming cannot
        produce different coordinate and measurement ranges for that line.
        """
        project = self._project(project_id)
        if not project:
            raise ValueError("Project not found.")
        yield ('{"type":"FeatureCollection","name":' +
               ujson.dumps(project["name"]) + ',"schema_version":' +
               str(SCHEMA_VERSION) + ',"features":[')
        first_feature = True
        for line in project["lines"]:
            coordinates, measurements = line["coordinates"], line["measurements"]
            coordinate_count, measurement_count = len(coordinates), len(measurements)
            yield (("" if first_feature else ",") + '{"type":"Feature","id":' +
                   ujson.dumps(line["id"]) + ',"geometry":')
            first_feature = False
            length_m, previous = 0.0, None
            if coordinate_count >= 2:
                yield '{"type":"LineString","coordinates":['
                for index in range(coordinate_count):
                    coordinate = coordinates[index]
                    if previous is not None:
                        length_m += _distance_m(previous, coordinate)
                    previous = coordinate
                    yield ("," if index else "") + ujson.dumps(coordinate)
                yield ']}'
            elif coordinate_count:
                yield ('{"type":"Point","coordinates":' +
                       ujson.dumps(coordinates[0]) + '}')
            else:
                yield 'null'
            yield ',"properties":{'
            properties = self._geojson_line_properties(line, length_m)
            for index, (key, value) in enumerate(properties.items()):
                yield (("," if index else "") + ujson.dumps(key) + ":" +
                       ujson.dumps(value))
            # Custom scalar properties historically override built-in fields,
            # including "measurements"; preserve that export contract.
            if "measurements" not in properties:
                yield ',"measurements":['
                for index in range(measurement_count):
                    yield (("," if index else "") + ujson.dumps(
                        self._expand_measurement(measurements[index])))
                yield ']'
            yield '}}'
        for point in project["points"]:
            feature = {"type": "Feature", "id": point["id"],
                "geometry": {"type": "Point", "coordinates": point["coordinate"]},
                "properties": dict((key, self._expand_measurement(value)
                                     if key == "measurement" else value)
                                   for key, value in point.items() if key != "coordinate")}
            yield ("" if first_feature else ",") + ujson.dumps(feature)
            first_feature = False
        yield ']}'

    def geojson(self, project_id=None):
        project = self._project(project_id)
        if not project:
            raise ValueError("Project not found.")
        features = []
        for line in project["lines"]:
            coordinates = list(line["coordinates"])
            length_m = sum(_distance_m(coordinates[index - 1], coordinates[index])
                           for index in range(1, len(coordinates)))
            properties = self._geojson_line_properties(line, length_m)
            if "measurements" not in properties:
                properties["measurements"] = [self._expand_measurement(value)
                                               for value in line["measurements"]]
            features.append({"type": "Feature", "id": line["id"],
                "geometry": ({"type": "LineString", "coordinates": coordinates}
                             if len(coordinates) >= 2 else
                             ({"type": "Point", "coordinates": coordinates[0]}
                              if coordinates else None)),
                "properties": properties})
        for point in project["points"]:
            features.append({"type": "Feature", "id": point["id"],
                "geometry": {"type": "Point", "coordinates": point["coordinate"]},
                "properties": dict((key, self._expand_measurement(value)
                                     if key == "measurement" else value)
                                   for key, value in point.items() if key != "coordinate")})
        return {"type": "FeatureCollection", "name": project["name"],
                "schema_version": SCHEMA_VERSION, "features": features}

    def csv(self, project_id=None):
        project = self._project(project_id)
        if not project:
            raise ValueError("Project not found.")
        rows = ["id,type,name,longitude,latitude,altitude_msl_m,line_id,chainage_m,lateral_offset_m,side,fix_status,satellites,hdop,correction_age_sec,samples,rtk_fixed_samples,horizontal_max_deviation_m,vertical_max_deviation_m,gst_samples,gst_horizontal_sigma_mean_m,gst_horizontal_sigma_max_m,gst_vertical_sigma_mean_m,gst_vertical_sigma_max_m,note"]
        def quote(value):
            text = "" if value is None else str(value)
            if text.startswith(("=", "+", "-", "@")):
                text = "'" + text
            return '"' + text.replace('"', '""') + '"'
        for point in project["points"]:
            coordinate, measurement = point["coordinate"], point["measurement"]
            observed = measurement.get("accuracy_observation") or {}
            receiver = measurement.get("receiver_accuracy") or {}
            rows.append(",".join(quote(value) for value in (
                point["id"], point["object_type"], point["name"], coordinate[0],
                coordinate[1], coordinate[2], point["line_id"], point["chainage_m"],
                point.get("lateral_offset_m"), point.get("side"),
                measurement["fix_status"], measurement["satellites"],
                measurement["hdop"], measurement.get("correction_age_sec"),
                observed.get("samples", 1), observed.get("rtk_fixed_samples", 1),
                observed.get("horizontal_max_deviation_m"),
                observed.get("vertical_max_deviation_m"), receiver.get("samples", 1),
                receiver.get("horizontal_sigma_mean_m", receiver.get("semi_major_sigma_m")),
                receiver.get("horizontal_sigma_max_m", receiver.get("semi_major_sigma_m")),
                receiver.get("vertical_sigma_mean_m", receiver.get("altitude_sigma_m")),
                receiver.get("vertical_sigma_max_m", receiver.get("altitude_sigma_m")),
                point["note"])))
        return "\r\n".join(rows) + "\r\n"


tracker = Tracker(autoload=False, point_store_autoload=False)
