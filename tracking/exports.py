# SPDX-License-Identifier: AGPL-3.0-only
"""Map data and the GeoJSON and CSV export streams."""
import ujson
import geo
from pointstore import FlashSequence
from tracking.common import MAP_MEASUREMENT_KEYS, SCHEMA_VERSION, _sample_counts, _utc_fields
from tracking.frames import (CRS_NOTE, HEIGHT_REFERENCE, _assigned_tally, _assignment_count,
                             _count_frame, _effective_frame, _frame_assignment,
                             _frame_assignment_error, _frame_properties, _height_fields,
                             _line_frame)
from tracking.fixes import _coordinate_text, _distance_m


class ExportMixin:
    @staticmethod
    def _map_measurement(measurement):
        measurement = measurement or {}
        result = {key: measurement.get(key) for key in MAP_MEASUREMENT_KEYS}
        result["recorded_at_utc"], result["clock_unsynced"] = _utc_fields(
            measurement.get("recorded_at"))
        return result

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
                changes = [dict(change) for change in self._map_changes
                           if change["revision"] > since_revision and
                           change.get("project_id") == project["id"]]
                yield ujson.dumps({"project_id": project["id"], "revision": self._event_sequence,
                        "changes": changes,
                        "profiles": self._map_profiles(
                            change["point"]["measurement"].get("profile_id")
                            for change in changes if change.get("op") == "asset"),
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
        profile_ids = []
        for index, point in enumerate(points):
            measurement = point.get("measurement", {})
            profile_id = measurement.get("profile_id")
            if profile_id and profile_id not in profile_ids:
                profile_ids.append(profile_id)
            yield ("," if index else "") + ujson.dumps({"id": point["id"], "type": point["object_type"],
             "name": point["name"], "note": point.get("note", ""),
             "coordinate": point["coordinate"], "line_id": point.get("line_id"),
             "chainage_m": point["chainage_m"],
             "recorded_at": measurement.get("recorded_at"),
             "receiver_accuracy": measurement.get("receiver_accuracy"),
             "measurement": self._map_measurement(measurement)})
        yield '],"profiles":%s,"truncated":%s}' % (
            ujson.dumps(self._map_profiles(profile_ids)), "true" if truncated else "false")

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
        an HTTP caller can yield to other services between flash reads. Each
        line's vertices are captured when its output begins and read
        sequentially in two passes (geometry, measurements); a point undone or
        added meanwhile does not change that line in this file, including its
        frame tally.

        The frame assignment is taken in the same step as the head; when it or
        the project is replaced while the export runs, the stream stops with
        frame_export_changed instead of mixing two states (E16).
        """
        project = self._project(project_id)
        if not project:
            raise ValueError("Project not found.")
        snapshot = project.get("frame_assignment")
        assignment = _frame_assignment(project)
        head = ""
        if assignment:
            head = ',"frame_assignment":' + ujson.dumps({
                "reference_frame": assignment["reference_frame"],
                "reference_frame_epoch": assignment["reference_frame_epoch"],
                "assigned_at_utc": _utc_fields(assignment["assigned_at"])[0],
                "count": _assignment_count(assignment)})
        yield ('{"type":"FeatureCollection","name":' +
               ujson.dumps(project["name"]) + ',"schema_version":' +
               str(SCHEMA_VERSION) + ',"crs_note":' + ujson.dumps(CRS_NOTE) + head +
               ',"features":[')
        scopes = assignment["lines"] if assignment else {}
        first_feature = True
        for line in project["lines"]:
            self._check_export_snapshot(project, snapshot, True)
            entry = scopes.get(line["id"]) or (0, 0)
            line_stream = self._iter_geojson_line(
                line, first_feature, (entry[0], entry[1], snapshot, project))
            try:
                for chunk in line_stream:
                    yield chunk
            finally:
                line_stream.close()
            first_feature = False
        self._check_export_snapshot(project, snapshot, True)
        in_scope = assignment["points"][0] if assignment else 0
        for index, point in enumerate(project["points"]):
            self._check_export_snapshot(project, snapshot)
            yield self._geojson_point_text(point, first_feature,
                                           assignment if index < in_scope else None)
            first_feature = False
        self._check_export_snapshot(project, snapshot, True)
        yield ']}'

    def _check_export_snapshot(self, project, assignment, full=False):
        """Stop an export whose project or frame assignment changed since it began.

        An assignment is replaced, never changed in place, so an identity check
        tells whether it still holds; full also checks that the project itself
        was not replaced (restore) or deleted. O(1), O(projects) with full.
        """
        if (project.get("frame_assignment") is not assignment or
                full and self._project(project["id"]) is not project):
            raise _frame_assignment_error("frame_export_changed")

    def _line_locators(self, line, count):
        """A snapshot of the locators of a flash line's first count vertices, else None."""
        coordinates, measurements = line["coordinates"], line["measurements"]
        if (isinstance(coordinates, FlashSequence) and
                isinstance(measurements, FlashSequence) and
                coordinates.store is measurements.store and
                coordinates.line_id == measurements.line_id):
            return coordinates.store.line_locators(coordinates.line_id, count)
        return None

    def _line_rows(self, line, count, locators, include_measurement):
        """(coordinate, measurement or None) of the line's first count vertices, in order.

        A flash line reads its locator snapshot sequentially (one open file per
        segment); a line held in RAM (locators None) is read by index.
        """
        if locators is None:
            coordinates, measurements = line["coordinates"], line["measurements"]
            for index in range(count):
                yield (coordinates[index],
                       measurements[index] if include_measurement and
                       index < len(measurements) else None)
            return
        sequence = line["coordinates"]
        rows = sequence.store.iter_locators(
            sequence.line_id, locators[:count] if count < len(locators) else locators,
            include_measurement)
        try:
            for value in rows:
                yield value["coordinate"], value["measurement"]
        finally:
            rows.close()

    def _iter_geojson_line(self, line, first_feature, scope=None):
        """Yield one line feature in chunks; its vertices are captured on the first step.

        scope is (n, c, assignment, project) from iter_geojson: the first n
        vertices take the assignment, c of them assignable; every vertex first
        checks that the project's assignment is still the one exported. Both
        passes read the same locator snapshot sequentially, so a point undone or
        added meanwhile changes neither the line nor its frame tally (E2).
        """
        coordinate_count, measurement_count = len(line["coordinates"]), len(line["measurements"])
        # Equal lengths are an invariant; min() keeps stream and dict identical if it breaks.
        count = min(coordinate_count, measurement_count)
        tally = self._export_line_frames(line, coordinate_count, measurement_count)
        in_scope, assignment, project = (scope[0], scope[2], scope[3]) if scope else (0, None, None)
        if tally is not None and in_scope:
            tally = _assigned_tally(tally, assignment["reference_frame"], scope[1])
        locators = self._line_locators(line, coordinate_count)
        # Custom scalar properties historically override built-in fields,
        # including "measurements"; preserve that export contract. Without a
        # current summary the frame tally rides along a vertex pass, so it
        # costs no extra reads (the geometry pass when "measurements" is overridden).
        override = "measurements" in line["properties"]
        counted = {} if tally is None else None
        yield (("" if first_feature else ",") + '{"type":"Feature","id":' +
               ujson.dumps(line["id"]) + ',"geometry":')
        length_m, previous = 0.0, None
        rows = self._line_rows(line, coordinate_count, locators,
                               override and counted is not None)
        try:
            if coordinate_count >= 2:
                yield '{"type":"LineString","coordinates":['
            for index in range(coordinate_count):
                if project is not None:
                    self._check_export_snapshot(project, assignment)
                coordinate, measurement = next(rows)
                if measurement is not None and index < count:
                    self._count_row_frame(counted, measurement,
                                          scope if index < in_scope else None)
                if coordinate_count == 1:
                    yield '{"type":"Point","coordinates":' + _coordinate_text(coordinate) + '}'
                    break
                if previous is not None:
                    length_m += _distance_m(previous, coordinate)
                previous = coordinate
                yield ("," if index else "") + _coordinate_text(coordinate)
        finally:
            rows.close()
        if coordinate_count != 1:
            yield ']}' if coordinate_count else 'null'
        yield ',"properties":{'
        properties = self._geojson_line_properties(line, length_m)
        for index, (key, value) in enumerate(properties.items()):
            yield (("," if index else "") + ujson.dumps(key) + ":" +
                   ujson.dumps(value))
        if not override:
            yield ',"measurements":['
            rows = self._line_rows(line, count, locators, True)
            try:
                for index in range(count):
                    if project is not None:
                        self._check_export_snapshot(project, assignment)
                    coordinate, measurement = next(rows)
                    exported = self._export_measurement(measurement, coordinate,
                                                        assignment if index < in_scope else None)
                    if counted is not None:
                        _count_frame(counted, exported["reference_frame"])
                    yield ("," if index else "") + ujson.dumps(exported)
            finally:
                rows.close()
            yield ']'
        if tally is None:
            tally = counted
        for key, value in (("reference_frame", _line_frame(tally)), ("reference_frames", tally)):
            if key not in properties:
                yield "," + ujson.dumps(key) + ":" + ujson.dumps(value)
        yield '}}'

    def _geojson_point_text(self, point, first_feature, assignment=None):
        # The coordinate triples are decimal text, so the feature is written
        # in pieces; the remaining properties go through ujson. assignment is
        # the project's frame assignment when the point is in its scope.
        properties = dict((key, self._export_measurement(value, assignment=assignment)
                           if key == "measurement" else value)
                          for key, value in point.items()
                          if key not in ("coordinate", "projected_coordinate"))
        profile_id = (point.get("measurement") or {}).get("profile_id")
        properties["gate_profile"] = profile_id
        properties["gate_limits"] = self._map_profiles((profile_id,)).get(profile_id)
        properties.update(_height_fields(point.get("measurement") or {}, point["coordinate"][2]))
        properties.update(_frame_properties(point.get("measurement") or {}, assignment))
        projected = point.get("projected_coordinate")
        return (("" if first_feature else ",") + '{"type":"Feature","id":' +
                ujson.dumps(point["id"]) + ',"geometry":{"type":"Point","coordinates":' +
                _coordinate_text(point["coordinate"]) + '},"properties":' +
                ujson.dumps(properties)[:-1] + ',"projected_coordinate":' +
                ("null" if projected is None else _coordinate_text(projected)) + '}}')

    def csv(self, project_id=None):
        # Built in one step without a yield, so the frame assignment read here
        # holds for every row (no snapshot check as in iter_geojson).
        project = self._project(project_id)
        if not project:
            raise ValueError("Project not found.")
        assignment = _frame_assignment(project)
        in_scope = assignment["points"][0] if assignment else 0
        rows = ["id,type,name,longitude,latitude,altitude_msl_m,altitude_ellipsoid_m,geoid_separation_m,antenna_height_m,ground_height_msl_m,height_reference,line_id,chainage_m,lateral_offset_m,side,fix_status,satellites,hdop,correction_age_sec,samples,rtk_fixed_samples,horizontal_max_deviation_m,vertical_max_deviation_m,gst_samples,gst_horizontal_sigma_mean_m,gst_horizontal_sigma_max_m,gst_vertical_sigma_mean_m,gst_vertical_sigma_max_m,station_id,ntrip_mount,reference_frame,reference_frame_epoch,reference_frame_source,gate_profile,precision,recorded_at,recorded_at_utc,clock_unsynced,note"]
        def quote(value, numeric=False):
            text = "" if value is None else str(value)
            # Numbers keep their minus sign; the prefix only guards text cells.
            if not numeric and text.startswith(("=", "+", "-", "@")):
                text = "'" + text
            return '"' + text.replace('"', '""') + '"'
        for index, point in enumerate(project["points"]):
            coordinate, measurement = point["coordinate"], point["measurement"]
            frame_cells = _effective_frame(measurement, assignment if index < in_scope else None)
            observed = measurement.get("accuracy_observation") or {}
            receiver = measurement.get("receiver_accuracy") or {}
            samples, fixed_samples, gst_samples = _sample_counts(measurement)
            recorded_at = measurement.get("recorded_at")
            utc_text, unsynced = _utc_fields(recorded_at)
            text_cells = (point["id"], point["object_type"], point["name"])
            number_cells = (geo.format_e8(coordinate[0]), geo.format_e8(coordinate[1]),
                            geo.format_mm(coordinate[2]))
            # Heights in integer millimetres; None stays an empty cell, never 0.
            antenna_mm = measurement.get("antenna_height_mm")
            height_cells = tuple(
                None if value is None else geo.format_mm(value) for value in (
                    measurement.get("alt_ellipsoid_mm"), measurement.get("geoid_sep_mm"), antenna_mm,
                    None if antenna_mm is None else coordinate[2] - antenna_mm))
            rows.append(",".join([quote(value) for value in text_cells] +
                [quote(value, True) for value in number_cells + height_cells] +
                [quote(HEIGHT_REFERENCE)] +
                [quote(point["line_id"])] +
                [quote(value, True) for value in (
                    point["chainage_m"], point.get("lateral_offset_m"))] +
                [quote(value) for value in (
                    point.get("side"), measurement["fix_status"])] +
                [quote(value, True) for value in (
                    measurement["satellites"], measurement["hdop"],
                    measurement.get("correction_age_sec"),
                    samples, fixed_samples,
                    observed.get("horizontal_max_deviation_m"),
                    observed.get("vertical_max_deviation_m"), gst_samples,
                    receiver.get("horizontal_sigma_mean_m", receiver.get("semi_major_sigma_m")),
                    receiver.get("horizontal_sigma_max_m", receiver.get("semi_major_sigma_m")),
                    receiver.get("vertical_sigma_mean_m", receiver.get("altitude_sigma_m")),
                    receiver.get("vertical_sigma_max_m", receiver.get("altitude_sigma_m")))] +
                [quote(value) for value in (
                    measurement.get("station_id"), measurement.get("ntrip_mount")) + frame_cells] +
                [quote(value) for value in (measurement.get("profile_id"), measurement.get("precision"))] +
                [quote(recorded_at, type(recorded_at) is int), quote(utc_text),
                 quote("true" if unsynced else "false"), quote(point["note"])]))
        return "\r\n".join(rows) + "\r\n"
