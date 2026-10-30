# SPDX-License-Identifier: AGPL-3.0-only
"""Validation of coordinates, measurements and survey backups, and the upgrade of legacy backups."""
import ujson
import geo
from cfg import log
from tracking.common import (LEGACY_SCHEMA_VERSIONS, MAX_BACKUP_BYTES, MAX_FEATURES,
                             MAX_LINES_PER_PROJECT, MAX_PROJECTS, MAX_PROPERTIES, OBJECT_TYPES,
                             SCHEMA_VERSION, _profile_id, _valid_timestamp)


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


# Valid range and unit of each component of a stored coordinate
# [lon_e8, lat_e8, alt_mm]: +-180 and +-90 degrees in 1e-8 degrees, and -20000
# to 100000 metres in millimetres.
_COORDINATE_BOUNDS = (
    (-180 * geo.COORD_SCALE, 180 * geo.COORD_SCALE, "1e-8 degrees"),
    (-90 * geo.COORD_SCALE, 90 * geo.COORD_SCALE, "1e-8 degrees"),
    (-20000 * geo.ALT_SCALE, 100000 * geo.ALT_SCALE, "millimetres"))


def _exact_component_ok(value, index):
    """True when value is a storable component of the stored triple at index.

    Mirrors the per-component rule of _validate_coordinate (a real int, a bool
    is not one here, inside the storage bounds) without raising, so the
    acceptance gate can decide per fix. The code is not shared; the property
    test in test_gate_exact_fields pins that gate and storage agree.
    """
    if type(value) is not int:
        return False
    low, high, _ = _COORDINATE_BOUNDS[index]
    return low <= value <= high


def _validate_coordinate(value, field="coordinate"):
    """Check a stored (schema 5) coordinate: the integer triple [lon_e8, lat_e8, alt_mm].

    Only real ints pass. A float or a bool is rejected, because the exact
    integers exist to avoid the rounding a float would bring back. The error
    names the field and the failing component, for example "coordinate[1]".
    """
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError("%s must contain longitude, latitude and altitude." % field)
    for index in range(3):
        item = value[index]
        low, high, unit = _COORDINATE_BOUNDS[index]
        if type(item) is not int:
            raise ValueError("%s[%d] must be an integer in %s." % (field, index, unit))
        if item < low or item > high:
            raise ValueError("%s[%d] must be between %d and %d (%s)." % (
                field, index, low, high, unit))


def _validate_legacy_coordinate(value, field="coordinate"):
    """Check a coordinate as schema 4 and older stored it: float degrees and metres.

    For the input paths that still read older data; a value that passes is
    converted to the integer triple before it is stored.
    """
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError("%s must contain longitude, latitude and altitude." % field)
    if not _finite_number(value[0], -180, 180) or not _finite_number(value[1], -90, 90):
        raise ValueError("%s contains an invalid longitude or latitude." % field)
    if not _finite_number(value[2], -20000, 100000):
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
    for key in ("satellites", "hdop", "correction_age_sec"):
        item = value.get(key)
        if item is not None and not _finite_number(item):
            raise ValueError("Measurement field %s is invalid." % key)
    if not _valid_timestamp(value["recorded_at"]):
        raise ValueError("Measurement field recorded_at must be an integer "
                         "0 <= recorded_at < 2**40.")
    for key in ("fix_status", "station_id", "source", "reference_frame",
                "reference_frame_epoch", "ntrip_mount"):
        item = value.get(key)
        if item is not None and (not isinstance(item, str) or len(item) > 80):
            raise ValueError("Measurement field %s is invalid." % key)
    if "precision" in value and value["precision"] not in ("e8", "legacy_float32"):
        raise ValueError("Measurement field precision is invalid.")
    for key in ("geoid_sep_mm", "alt_ellipsoid_mm", "antenna_height_mm"):
        item = value.get(key)
        # type() is int excludes the bool and float values an int check would let by.
        if item is not None and type(item) is not int:
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


def _require_precision(measurement):
    """A schema-5 measurement must say whether it is exact or converted legacy data."""
    if "precision" not in measurement:
        raise ValueError("Measurement field precision is required.")


def _validate_backup_profiles(profiles):
    if not isinstance(profiles, list):
        raise ValueError("Survey backup profiles are invalid.")
    profile_ids = set()
    for profile in profiles:
        if (not isinstance(profile, dict) or not isinstance(profile.get("id"), str)
                or profile["id"] in profile_ids or not isinstance(profile.get("limits"), dict)):
            raise ValueError("A measurement profile is invalid or duplicated.")
        profile_ids.add(profile["id"])
    return profile_ids


def _validate_backup_project(project, ids, project_ids, line_ids, profile_ids):
    """Check one backup project, its lines before its assets; return its feature count."""
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
    project_line_ids, features = set(), 0
    for line in lines:
        features += _validate_backup_line(line, ids, line_ids, project_line_ids, profile_ids)
    for point in points:
        _validate_backup_point(point, ids, project_line_ids, profile_ids)
        features += 1
    return features


def _validate_backup_line(line, ids, line_ids, project_line_ids, profile_ids):
    """Check one backup line; return its vertex count."""
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
    for coordinate in coordinates: _validate_coordinate(coordinate)
    for measurement in measurements:
        _validate_measurement(measurement)
        _require_precision(measurement)
    for measurement in measurements:
        if measurement.get("profile_id") and measurement["profile_id"] not in profile_ids:
            raise ValueError("A measurement references an unknown profile.")
    _validate_properties(line.get("properties", {}))
    return len(coordinates)


def _validate_backup_point(point, ids, project_line_ids, profile_ids):
    if not isinstance(point, dict):
        raise ValueError("A survey asset is invalid.")
    point_id = point.get("id")
    if not isinstance(point_id, str) or not point_id or len(point_id) > 80 or point_id in ids:
        raise ValueError("An asset ID is invalid or duplicated.")
    ids.add(point_id)
    if point.get("object_type") not in OBJECT_TYPES:
        raise ValueError("An asset type is invalid.")
    _clean_text(point.get("name", "")); _clean_text(point.get("note", ""), 500)
    _validate_coordinate(point.get("coordinate"))
    projected = point.get("projected_coordinate")
    if projected is not None: _validate_coordinate(projected, "projected_coordinate")
    if point.get("line_id") is not None and point.get("line_id") not in project_line_ids:
        raise ValueError("An asset references an unknown line.")
    _validate_measurement(point.get("measurement"))
    _require_precision(point["measurement"])
    if (point["measurement"].get("profile_id") and
            point["measurement"]["profile_id"] not in profile_ids):
        raise ValueError("A measurement references an unknown profile.")
    if point["object_type"] == "stakeout":
        from stakeout_progress import valid_point_fields
        if not valid_point_fields(point):
            raise ValueError("A stakeout point is invalid.")


def _validate_backup_selection(payload, projects, project_ids, line_ids):
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
    profile_ids = _validate_backup_profiles(payload.get("profiles", []))
    if len(projects) > MAX_PROJECTS:
        raise ValueError("Survey backup contains too many projects.")
    ids, project_ids, line_ids, features = set(), set(), set(), 0
    for project in projects:
        features += _validate_backup_project(project, ids, project_ids, line_ids, profile_ids)
    if features > MAX_FEATURES:
        raise ValueError("Survey backup contains too many features.")
    _validate_backup_selection(payload, projects, project_ids, line_ids)
    distance = payload.get("auto_distance_m", 1.0)
    if not _finite_number(distance, 0, 100) or (distance and distance < 0.2):
        raise ValueError("The automatic point distance is invalid.")
    return ujson.loads(encoded) if copy_result else payload


def upgrade_restore_backup(payload, copy_result=True):
    """Convert an explicitly selected schema 1-4 backup to the current schema.

    Schema 1 stored projected asset coordinates as longitude/latitude pairs;
    the measured MSL altitude is the only lossless altitude for them. Schemas
    1-4 stored float degrees and metres: the coordinates become the integer
    triple and the measurements are tagged "legacy_float32", value by value
    (see _upgrade_projects). A schema-5 payload is returned unchanged.
    """
    if not isinstance(payload, dict) or payload.get("schema_version") not in (
            (1,) + LEGACY_SCHEMA_VERSIONS):
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
    supplied = upgraded.get("profiles")
    if supplied is None:
        supplied = []
    if not isinstance(supplied, list):
        raise ValueError("Survey backup profiles are invalid.")
    # Start from the profile table the backup carries (compact archives and
    # V15.8.1 backups reference it by profile_id); inline limits add to it.
    # validate_backup checks every reference afterwards.
    profiles = {}
    for profile in supplied:
        if not isinstance(profile, dict) or not isinstance(profile.get("id"), str):
            raise ValueError("A measurement profile is invalid or duplicated.")
        profiles[profile["id"]] = profile
    features_before = _feature_total(upgraded["projects"])
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
        _upgrade_projects([project])
        measurements = [m for line in project.get("lines", [])
                        for m in line.get("measurements", [])]
        measurements += [p.get("measurement") for p in project.get("points", [])]
        for measurement in measurements:
            if not isinstance(measurement, dict):
                continue
            quality = measurement.get("quality") or {}
            limits = quality.get("limits")
            if limits:
                # A reference that already resolves to these limits stays: it
                # is the ID the backup was written with (device and host derive
                # the same limits' ID differently up to V15.9.0).
                profile_id = measurement.get("profile_id")
                known = profiles.get(profile_id)
                if not (known and known.get("limits") == limits):
                    profile_id = _profile_id(limits)
                measurement["profile_id"] = profile_id
                profiles[profile_id] = {"id": profile_id, "version": 1,
                                        "limits": limits}
    upgraded["profiles"] = list(profiles.values())
    if features_before and not _feature_total(upgraded["projects"]):
        raise ValueError(_NOTHING_CONVERTED)
    return upgraded


# A legacy restore that keeps no feature at all is refused: it would replace
# the store with an empty survey. The usual cause is current data under a
# legacy label (for example an edited header).
_NOTHING_CONVERTED = ("No coordinate of the legacy backup could be converted; "
                      "the survey was left unchanged.")


def _feature_total(projects):
    """Vertices plus assets of a backup's projects list, ignoring malformed entries."""
    total = 0
    for project in projects or ():
        if not isinstance(project, dict):
            continue
        for line in project.get("lines") or ():
            if isinstance(line, dict) and isinstance(line.get("coordinates"), list):
                total += len(line["coordinates"])
        if isinstance(project.get("points"), list):
            total += len(project["points"])
    return total


# A step that asks _finish_steps_async for a RESTORE_SERVICE_MS pause.
_SERVICE_PAUSE = object()


def _payload_records(payload):
    """A validated schema-5 JSON payload as the records of an NDJSON backup."""
    yield {"record": "header", "kind": "survey_backup", "schema_version": SCHEMA_VERSION,
           "profiles": payload.get("profiles") or []}
    for project in payload["projects"]:
        metadata = dict(project); metadata.pop("lines"); metadata.pop("points")
        yield {"record": "project", "project": metadata}
        for line in project["lines"]:
            metadata = dict(line)
            coordinates, measurements = metadata.pop("coordinates"), metadata.pop("measurements")
            yield {"record": "line", "project_id": project["id"], "line": metadata}
            for coordinate, measurement in zip(coordinates, measurements):
                yield {"record": "vertex", "project_id": project["id"], "line_id": line["id"],
                       "coordinate": coordinate, "measurement": measurement}
        for point in project["points"]:
            yield {"record": "asset", "project_id": project["id"], "point": point}
    yield {"record": "footer", "active_project_id": payload.get("active_project_id"),
           "active_line_id": payload.get("active_line_id"),
           "auto_distance_m": payload.get("auto_distance_m", 1.0)}


# Loading schema <= 4 data. Those schemas stored coordinates as float degrees
# and metres; on load they become the exact integer triple and every measurement
# that belongs to them is tagged "legacy_float32", because the integers are no
# better than the 32-bit floats they came from. Only data labelled with a
# legacy schema is converted, so stored integers are never converted twice.
#
# Older firmware did not validate everything it stored (V15.8.1 add_asset kept
# any projected_coordinate, and project_to_line extrapolated altitude without a
# limit). One such value must never cost a whole checkpoint or event, so each
# value is handled on its own: an invalid projected_coordinate becomes None, an
# asset or a vertex with an invalid coordinate is left out. Each case is
# logged (record and field, not the value) and counted.

def _legacy_conversion_error(record, field):
    log("ERROR", "TRACK", "Legacy %s of %s is invalid; not converted." % (field, record))
    from state import app
    app.stats["legacy_conversion_errors"] = app.stats.get("legacy_conversion_errors", 0) + 1


def _legacy_error_count():
    """Values counted by _legacy_conversion_error so far; restores report the difference."""
    from state import app
    return app.stats.get("legacy_conversion_errors", 0)


_relabel_warned = False


def _relabelled_exact(value, measurement):
    """True for a stored integer triple that only carries a legacy label.

    Older firmware (V15.8.1) loads a schema-5 stream checkpoint and writes it
    back as schema 4, keeping every value and the measurement's precision
    tag. No schema <= 4 firmware wrote that tag, so a tagged measurement with
    an integer triple that is valid as [lon_e8, lat_e8, alt_mm] is current
    data and must not go through the degree rule (which would drop it).
    """
    if (not isinstance(measurement, dict) or
            measurement.get("precision") not in ("e8", "legacy_float32") or
            not isinstance(value, (list, tuple)) or len(value) != 3 or
            type(value[0]) is not int or type(value[1]) is not int or
            type(value[2]) is not int):
        return False
    try:
        _validate_coordinate(value)
    except ValueError:
        return False
    global _relabel_warned
    if not _relabel_warned:
        _relabel_warned = True
        log("WARN", "TRACK", "Schema-4 label on schema-5 data; older firmware ran "
            "in between. The stored integers are kept.")
    return True


def _legacy_coordinate(value, field="coordinate", measurement=None):
    """Convert one legacy triple; None when it is invalid even by the legacy rule.

    ``measurement`` is the record's measurement: a triple that
    _relabelled_exact recognises is returned unchanged (as a list).
    """
    if _relabelled_exact(value, measurement):
        return list(value)
    try:
        _validate_legacy_coordinate(value, field)
        return geo.legacy_coordinate_to_e8(value)
    except (ValueError, TypeError):
        return None


def _tag_legacy(measurement):
    if isinstance(measurement, dict) and "precision" not in measurement:
        measurement["precision"] = "legacy_float32"


def _upgrade_legacy_line(line):
    """Convert a legacy line dict in place: embedded vertices, tags, cached summary.

    A vertex with an invalid coordinate is left out together with its
    measurement, so both lists stay aligned.
    """
    if not isinstance(line, dict):
        return
    coordinates = line.get("coordinates")
    measurements = line.get("measurements")
    if isinstance(coordinates, list):
        kept_coordinates, kept_measurements = [], []
        has_measurements = isinstance(measurements, list)
        for index, item in enumerate(coordinates):
            measurement = (measurements[index] if has_measurements and
                           index < len(measurements) else None)
            converted = _legacy_coordinate(item, "coordinate", measurement)
            if converted is None:
                order = (measurement.get("record_order")
                         if isinstance(measurement, dict) else None)
                _legacy_conversion_error("line %s vertex %s" % (
                    line.get("id"), order if order is not None else index), "coordinate")
                continue
            kept_coordinates.append(converted)
            if has_measurements and index < len(measurements):
                kept_measurements.append(measurement)
        if has_measurements:
            # Measurements beyond the coordinates never had a vertex; keep them as they were.
            kept_measurements.extend(measurements[len(coordinates):])
            line["measurements"] = kept_measurements
        line["coordinates"] = kept_coordinates
    for measurement in line.get("measurements") or ():
        _tag_legacy(measurement)
    # The cached length and gap statistics came from the former distance model
    # and the float values; the load rebuilds them from the integers.
    line.pop("_summary", None)


def _upgrade_legacy_point(point):
    """Convert a legacy asset point in place; False when the asset must be left out."""
    if not isinstance(point, dict):
        return True
    coordinate = _legacy_coordinate(point.get("coordinate"), "coordinate",
                                    point.get("measurement"))
    if coordinate is None:
        _legacy_conversion_error("point %s" % point.get("id"), "coordinate")
        return False
    point["coordinate"] = coordinate
    if point.get("projected_coordinate") is not None:
        # Derived data: without it the asset keeps chainage, offset and side.
        projected = _legacy_coordinate(point["projected_coordinate"],
                                       "projected_coordinate", point.get("measurement"))
        if projected is None:
            _legacy_conversion_error("point %s" % point.get("id"), "projected_coordinate")
        point["projected_coordinate"] = projected
    _tag_legacy(point.get("measurement"))
    return True


def _upgrade_projects(projects):
    """Convert a schema <= 4 projects list in place and return it.

    Used for monolithic legacy checkpoints and snapshot events: embedded line
    coordinates and measurements, point coordinate, projected_coordinate and
    measurement. Invalid values are handled one by one (see above).
    """
    for project in projects or ():
        if not isinstance(project, dict):
            continue
        for line in project.get("lines") or ():
            _upgrade_legacy_line(line)
        points = project.get("points")
        if isinstance(points, list):
            project["points"] = [point for point in points if _upgrade_legacy_point(point)]
    return projects


def _upgrade_event(event):
    """Convert a schema <= 4 journal event in place to schema 5 and return it.

    Events of the current schema are returned unchanged. A converted event
    carries the current schema afterwards, so a second call does nothing.
    Returns None for a vertex_added or asset_added event whose coordinate is
    invalid: that one feature is left out, the rest of the journal replays.
    """
    if not isinstance(event, dict) or event.get("schema_version") not in LEGACY_SCHEMA_VERSIONS:
        return event
    kind = event.get("event")
    if kind == "vertex_added":
        coordinate = _legacy_coordinate(event.get("coordinate"), "coordinate",
                                        event.get("measurement"))
        if coordinate is None:
            _legacy_conversion_error("line %s vertex %s" % (
                event.get("line_id"), event.get("order")), "coordinate")
            return None
        event["coordinate"] = coordinate
        _tag_legacy(event.get("measurement"))
    elif kind == "asset_added":
        if not _upgrade_legacy_point(event.get("point")):
            return None
    elif kind == "line_started":
        _upgrade_legacy_line(event.get("line"))
    elif kind == "snapshot":
        _upgrade_projects(event.get("projects"))
    elif kind == "project_created":
        # Projects are created empty; convert anything embedded all the same.
        _upgrade_projects([event.get("project")])
    event["schema_version"] = SCHEMA_VERSION
    return event
