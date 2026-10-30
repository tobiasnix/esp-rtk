# SPDX-License-Identifier: AGPL-3.0-only
"""Independent reference implementations used as test oracles; not on the device.

geojson() builds the export as one dict, the oracle for the streamed
Tracker.iter_geojson(); parse_backup_ndjson() reads a whole NDJSON backup at
once, the oracle for the streamed restore. Not a test module itself (no test_
prefix); test modules import it with `import tracking_reference`.
"""
import support  # noqa: F401; MicroPython modules on the host.
import tracking


def coordinate_degrees(coordinate):
    """The float [lon, lat, alt] of a stored triple, for the dict form of the export."""
    return [tracking.geo.e8_to_deg(coordinate[0]), tracking.geo.e8_to_deg(coordinate[1]),
            coordinate[2] / tracking.geo.ALT_SCALE]


def frame_fields(measurement, in_scope, assignment):
    """(frame, epoch, source) of a stored measurement by the #39 rule.

    Written out independently of tracking._effective_frame: a stored frame is
    "recorded"; an RTK measurement (fix quality 4/5, else the fix status) in
    the assignment's scope without one is "assigned"; anything else is unknown.
    """
    if measurement.get("reference_frame"):
        return (measurement["reference_frame"], measurement.get("reference_frame_epoch") or None,
                "recorded")
    quality = measurement.get("fix_quality")
    rtk = (quality in (4, 5) if quality is not None else
           measurement.get("fix_status") in ("RTK_FIXED", "RTK_FLOAT"))
    if assignment is not None and in_scope and rtk:
        return assignment["reference_frame"], assignment["reference_frame_epoch"], "assigned"
    return "unknown", measurement.get("reference_frame_epoch") or None, None


def exported_measurement(tracker, measurement, coordinate, in_scope, assignment):
    """The expanded export of a measurement with its frame fields set by frame_fields."""
    exported = tracker._export_measurement(measurement, coordinate)
    frame, epoch, source = frame_fields(measurement, in_scope, assignment)
    exported["reference_frame"], exported["reference_frame_source"] = frame, source
    if source == "assigned" or "reference_frame_epoch" in exported:
        exported["reference_frame_epoch"] = epoch
    return exported


def geojson(tracker, project_id=None):
    project = tracker._project(project_id)
    if not project:
        raise ValueError("Project not found.")
    assignment = tracking._frame_assignment(project)
    scopes = assignment["lines"] if assignment else {}
    features = []
    for line in project["lines"]:
        stored_coordinates, stored_measurements = line["coordinates"], line["measurements"]
        coordinates = list(stored_coordinates)
        length_m = sum(tracking._distance_m(coordinates[index - 1], coordinates[index])
                       for index in range(1, len(coordinates)))
        coordinates = [coordinate_degrees(value) for value in coordinates]
        properties = tracker._geojson_line_properties(line, length_m)
        coordinate_count, measurement_count = len(stored_coordinates), len(stored_measurements)
        count = min(coordinate_count, measurement_count)
        in_scope, assigned = scopes.get(line["id"], [0, 0])
        tally = tracker._export_line_frames(line, coordinate_count, measurement_count)
        if tally is not None and assigned:
            # The summary counts stored frames; the assigned ones leave "unknown".
            tally["unknown"] -= assigned
            if not tally["unknown"]:
                del tally["unknown"]
            tally[assignment["reference_frame"]] = (
                tally.get(assignment["reference_frame"], 0) + assigned)
        if "measurements" not in properties:
            properties["measurements"] = [
                exported_measurement(tracker, stored_measurements[index],
                                     stored_coordinates[index], index < in_scope, assignment)
                for index in range(count)]
            if tally is None:
                tally = {}
                for exported in properties["measurements"]:
                    tracking._count_frame(tally, exported["reference_frame"])
        elif tally is None:
            tally = tracker._count_line_frames(
                stored_measurements, count,
                (in_scope, assigned, assignment, project) if assignment else None)
        for key, value in (("reference_frame", tracking._line_frame(tally)),
                           ("reference_frames", tally)):
            properties.setdefault(key, value)
        features.append({"type": "Feature", "id": line["id"],
            "geometry": ({"type": "LineString", "coordinates": coordinates}
                         if len(coordinates) >= 2 else
                         ({"type": "Point", "coordinates": coordinates[0]}
                          if coordinates else None)),
            "properties": properties})
    points_in_scope = assignment["points"][0] if assignment else 0
    for index, point in enumerate(project["points"]):
        measurement = point.get("measurement") or {}
        properties = dict((key, exported_measurement(tracker, value, None,
                                                     index < points_in_scope, assignment)
                           if key == "measurement" else value)
                          for key, value in point.items() if key != "coordinate")
        profile_id = measurement.get("profile_id")
        properties["gate_profile"] = profile_id
        properties["gate_limits"] = tracker._map_profiles((profile_id,)).get(profile_id)
        properties.update(tracking._height_fields(measurement, point["coordinate"][2]))
        frame, epoch, source = frame_fields(measurement, index < points_in_scope, assignment)
        properties.update({"reference_frame": frame, "reference_frame_epoch": epoch,
                           "reference_frame_source": source,
                           "ntrip_mount": measurement.get("ntrip_mount"),
                           "station_id": measurement.get("station_id")})
        projected = point.get("projected_coordinate")
        properties["projected_coordinate"] = (
            None if projected is None else coordinate_degrees(projected))
        features.append({"type": "Feature", "id": point["id"],
            "geometry": {"type": "Point",
                         "coordinates": coordinate_degrees(point["coordinate"])},
            "properties": properties})
    collection = {"type": "FeatureCollection", "name": project["name"],
                  "schema_version": tracking.SCHEMA_VERSION, "crs_note": tracking.CRS_NOTE,
                  "features": features}
    if assignment:
        collection["frame_assignment"] = {
            "reference_frame": assignment["reference_frame"],
            "reference_frame_epoch": assignment["reference_frame_epoch"],
            "assigned_at_utc": tracking._utc_fields(assignment["assigned_at"])[0],
            "count": sum(value[1] for value in assignment["lines"].values()) +
            assignment["points"][1]}
    return collection


def parse_backup_ndjson(lines):
    """Parse an NDJSON backup (schema 2-5) into a validated schema-5 payload."""
    payload = {"kind": "survey_backup", "schema_version": tracking.SCHEMA_VERSION,
               "profiles": [], "projects": [], "active_project_id": None,
               "active_line_id": None, "auto_distance_m": 1.0}
    projects, line_lookup, records = {}, {}, 0
    digest, integrity, header_schema, footer_seen = tracking.hashlib.sha256(), None, None, False
    for raw in lines:
        if not raw or not raw.strip():
            continue
        records += 1
        if records > (tracking.MAX_FEATURES + tracking.MAX_LINES_PER_PROJECT * tracking.MAX_PROJECTS
                       + tracking.MAX_PROJECTS + 3):
            raise ValueError("Survey backup contains too many records.")
        value = tracking._expanded_archive_record(tracking.ujson.loads(raw))
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
                    value.get("schema_version") not in tracking.LEGACY_SCHEMA_VERSIONS + (
                        tracking.SCHEMA_VERSION,)):
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
    # Schema 4 introduced the integrity record; older backups never had one.
    # One that is present is always checked, so a header relabelled to an
    # older schema cannot skip it.
    if integrity is not None or (header_schema is not None and header_schema >= 4):
        checksum = tracking.Tracker._hex_digest(digest)
        if (not integrity or integrity.get("algorithm") != "sha256" or
                integrity.get("content_sha256") != checksum or
                integrity.get("records") != records - 1):
            raise ValueError("Survey backup integrity check failed.")
    return tracking.validate_backup(tracking.upgrade_restore_backup(payload))
