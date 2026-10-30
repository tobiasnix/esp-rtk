# SPDX-License-Identifier: AGPL-3.0-only
"""Survey status, capacity and the cached line summaries."""
import os
import ujson
import geo
from pointstore import FlashSequence
from tracking.common import (MAX_LINE_GAP_M, MAX_VERTICAL_STEP_M, SCHEMA_VERSION, _age_sec,
                             _now_seconds, _utc_fields)
from tracking.guards import _MaintenanceGuard, _storage_busy
from tracking.frames import (SUMMARY_REFRESH_PASSES, SUMMARY_SCAN_BATCH, _assigned_tally,
                             _control_vertical_delta_mm, _count_frame, _effective_frame,
                             _frame_assignment, _frame_fields)
from tracking.validation import _clean_text
from tracking.fixes import _distance_m


class SummaryMixin:
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
        limit = self.feature_limit()
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
        result = _frame_fields(self._project_frame_tally(project))
        result.update({"id": project["id"], "name": project["name"],
                "description": project["description"],
                "created_at": project.get("created_at"),
                "updated_at": self._project_activity.get(
                    project["id"], project.get("created_at")),
                "line_count": len(project["lines"]),
                "point_count": len(project["points"]),
                "feature_count": len(project["points"]) + sum(
                    len(line["coordinates"]) for line in project["lines"]),
                "last_quality_accepted": accepted,
                "progress": ("recording" if "recording" in states else
                             "paused" if "paused" in states else
                             "complete" if states else "empty")})
        assignment = self._assignment_summary(project)
        if assignment is not None:
            result["frame_assignment"] = assignment
        return result

    def _startup_view(self):
        from state import app
        return {"revision": 0, "device_time": _now_seconds(),
                "tracking_startup": dict(app.stats.get("tracking_startup") or {"state": "loading"}),
                "projects": [], "archives": [], "active_project": None,
                "active_project_id": None, "active_project_lines": [],
                "active_line_id": None, "active_line": None, "undo_candidate": None,
                "capacity": {"level": "loading", "used": 0,
                             "limit": self.feature_limit(), "recording_allowed": False,
                             "writable": False}}

    def status(self):
        if not self._loaded:
            return self._startup_view()
        device_time = _now_seconds()
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
                        last["age_sec"] = _age_sec(device_time, recorded_at)
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
        controls = self._control_checks(project)
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
                          "age_sec": _age_sec(device_time, recorded_at),
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

    def _control_checks(self, project):
        """Compare the first and the latest measurement of each named control point."""
        controls = []
        if project:
            grouped = {}
            for point in project.get("points", []):
                if point.get("object_type") == "control" and point.get("name"):
                    grouped.setdefault(point["name"], []).append(point)
            for name, points in grouped.items():
                first, latest = points[0], points[-1]
                vertical_mm, reference = _control_vertical_delta_mm(first, latest)
                controls.append({
                    "name": name, "measurements": len(points),
                    "horizontal_delta_m": round(_distance_m(
                        first["coordinate"], latest["coordinate"]), 4),
                    "vertical_delta_m": round(vertical_mm / geo.ALT_SCALE, 4),
                    "vertical_reference": reference,
                    "first_recorded_at": first["measurement"].get("recorded_at"),
                    "latest_recorded_at": latest["measurement"].get("recorded_at"),
                })
        return controls

    def live_status(self):
        """Return the field-loop state without directory scans or full summaries."""
        if not self._loaded:
            return self._startup_view()
        device_time = _now_seconds()
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
                          "age_sec": _age_sec(device_time, recorded_at),
                          "h_sigma_m": sigma}
        active_project = None
        if project:
            active_project = _frame_fields(self._project_frame_tally(project))
            active_project.update({"id": project["id"], "name": project["name"],
                                   "line_count": len(project["lines"]),
                                   "point_count": len(project["points"]),
                                   # Always present: a browser merging it into its
                                   # project must see a revocation as None.
                                   "frame_assignment": self._assignment_summary(project)})
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

    @staticmethod
    def _summary_frames(line, count):
        """The line's frame tally when its cached summary covers ``count`` vertices, else None.

        The summary is the source of truth for a line's frames; reading it costs
        no flash reads. The caller copies the tally before handing it out. A line
        without vertices has an empty tally, whatever a restored summary claims.
        """
        if not count:
            return {}
        summary = line.get("_summary")
        if (summary and summary.get("vertex_count") == count and
                "legacy_vertex_count" in summary):
            return summary.get("frames")
        return None

    def _export_line_frames(self, line, coordinate_count, measurement_count):
        """A copy of a current summary's tally for an export, or None to count instead.

        An export takes the line's tally from its summary, read before the first
        yield so appends during streaming cannot change it. A stale summary is not
        rebuilt (that would scan the line once more); the export counts instead.
        """
        frames = (self._summary_frames(line, coordinate_count)
                  if coordinate_count == measurement_count else None)
        return None if frames is None else dict(frames)

    @staticmethod
    def _count_line_frames(measurements, count, scope=None):
        """Count the frames of the first ``count`` stored measurements (one read each).

        Without a scope these are the stored frames; with an export scope
        (n, c, assignment, project) the first n count with the assignment.
        """
        tally = {}
        in_scope = scope[0] if scope else 0
        for index in range(count):
            SummaryMixin._count_row_frame(tally, measurements[index],
                                     scope if index < in_scope else None)
        return tally

    @staticmethod
    def _count_row_frame(tally, measurement, scope=None):
        """Count the frame a reader shows for one stored measurement into tally.

        scope is the export scope (n, c, assignment, project) when the
        measurement is among its first n, else None (its stored frame).
        """
        _count_frame(tally, _effective_frame(measurement, scope[2] if scope else None)[0])

    def _line_frames(self, line):
        """The line's frame tally, rebuilding a stale summary (a scan) first."""
        count = len(line["coordinates"])
        frames = self._summary_frames(line, count)
        if frames is None:
            self._line_summary(line)
            frames = self._summary_frames(line, count)
        if frames is None:
            # Only when the summary could not be cached (an inconsistent line).
            frames = self._count_line_frames(line["measurements"],
                                             min(count, len(line["measurements"])))
        return frames

    def _project_frame_tally(self, project):
        """The kept frame tally of a project, built from line summaries when missing.

        Cleared by load, restore and snapshot and rebuilt from the current line
        summaries on first read, then kept by _apply, so status() and
        live_status() read it without scanning any line. It counts the frames
        readers show: a frame assignment moves its measurements from "unknown"
        to its frame (_assigned_tally, _effective_frame).
        """
        frames = self._project_frames.get(project["id"])
        if frames is None:
            frames = {}
            assignment = _frame_assignment(project)
            scopes = assignment["lines"] if assignment else {}
            for line in project["lines"]:
                tally = self._line_frames(line)
                scope = scopes.get(line["id"])
                if scope:
                    tally = _assigned_tally(tally, assignment["reference_frame"], scope[1])
                for frame, count in tally.items():
                    frames[frame] = frames.get(frame, 0) + count
            in_scope = assignment["points"][0] if assignment else 0
            for index, point in enumerate(project["points"]):
                _count_frame(frames, _effective_frame(point.get("measurement") or {},
                    assignment if index < in_scope else None)[0])
            self._project_frames[project["id"]] = frames
        return frames

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
        cached = dict(line["_summary"]) if self._summary_current(line) else None
        return self._line_summary_steps(line, result, cached, count, measurement_count, revision)

    @staticmethod
    def _summary_current(line):
        """True when the line's cached summary covers its vertices (no scan needed).

        A summary without the legacy count or the frame tally predates them and
        is rebuilt.
        """
        cached = line.get("_summary")
        return bool(cached and cached.get("vertex_count") == len(line["coordinates"]) and
                    "legacy_vertex_count" in cached and "frames" in cached)

    def _summary_targets(self, lines=None):
        """The given lines as a list, or every line of every project."""
        if lines is not None:
            return list(lines)
        return [line for project in self.projects for line in project["lines"]]

    def summaries_current(self, lines=None):
        """True when no line of lines (default: all lines) needs a summary scan."""
        for line in self._summary_targets(lines):
            if not self._summary_current(line):
                return False
        return True

    async def refresh_summaries_async(self, lines=None, owner=None):
        """Rebuild stale line summaries cooperatively; True when all are current.

        Without owner the scan holds a reader for its whole duration (StorageBusy
        when maintenance runs); owner is the entered maintenance guard of this
        tracker the caller works in, and the scan takes no reader then. After
        every SUMMARY_SCAN_BATCH vertex reads it yields once and gives up with
        False when maintenance (or, with owner, the end of owner) wants the
        storage. An event during a pass keeps that pass from caching
        (_line_summary_steps); after SUMMARY_REFRESH_PASSES passes the result is
        whatever is current. There is no synchronous fallback. A line without
        vertices or measurements needs no read and is summarized right away.
        """
        if not self._loaded:
            return True
        if owner is not None and not (isinstance(owner, _MaintenanceGuard) and
                                      owner._tracker is self and owner.entered):
            raise _storage_busy("storage_busy")
        targets = self._summary_targets(lines)
        for line in targets:
            if (not line["coordinates"] and not line.get("measurements") and
                    not self._summary_current(line)):
                for _ in self._iter_line_summary(line):
                    pass
        if self.summaries_current(targets):
            return True
        if owner is not None:
            return await self._refresh_summary_passes(targets, owner)
        async with self.reader():
            return await self._refresh_summary_passes(targets, None)

    async def _refresh_summary_passes(self, targets, owner):
        """The scan of refresh_summaries_async; the caller holds the reader or owner."""
        import uasyncio as asyncio
        reads = 0
        for _pass in range(SUMMARY_REFRESH_PASSES):
            for line in targets:
                if self._summary_current(line):
                    continue
                steps = self._iter_line_summary(line)
                try:
                    for step in steps:
                        if step is not None:
                            continue
                        reads += 1
                        if reads < SUMMARY_SCAN_BATCH:
                            continue
                        reads = 0
                        await asyncio.sleep_ms(0)
                        if ((not owner.entered) if owner is not None else
                                (self.maintenance_active() or self._install_pending)):
                            return False
                finally:
                    steps.close()
            if self.summaries_current(targets):
                return True
        return self.summaries_current(targets)

    async def prepare_status_async(self, owner=None):
        """Make status() scan-free: rebuild stale summaries first (StorageBusy if not possible).

        status() reads the summary of every line it may report or count frames
        of (_project_summary, _project_frame_tally), so every line is a target.
        No await separates the return from the caller's status().
        """
        if (not self._loaded or self._status_cache is not None or
                self.summaries_current()):
            return
        if not await self.refresh_summaries_async(None, owner):
            raise _storage_busy("storage_busy")

    async def prepare_append_async(self):
        """Make add_vertex() scan-free on the active line (StorageBusy if not possible)."""
        line = self._line(self._project())
        if not line or not line["coordinates"] or self._summary_current(line):
            return
        if not await self.refresh_summaries_async((line,)):
            raise _storage_busy("storage_busy")

    def _line_summary_steps(self, line, result, cached, count, measurement_count, revision):
        """Scan the captured range of a line unless cached; yield None per vertex, then result."""
        coordinates, measurements = line["coordinates"], line.get("measurements", [])
        summary = cached
        if summary is None:
            summary = {"vertex_count": count, "length_m": 0.0,
                "quality_accepted_count": 0, "large_gap_count": 0,
                "max_gap_m": 0.0, "large_vertical_step_count": 0,
                "max_vertical_step_m": 0.0, "overlapping": False,
                "legacy_vertex_count": 0, "frames": {}}
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
                            vertical = abs(coordinate[2] - previous[2]) / geo.ALT_SCALE
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
                        measured = row["measurement"] if row is not None else measurements[index]
                        summary["quality_accepted_count"] += int(bool(
                            (measured.get("quality") or {}).get("accepted")))
                        summary["legacy_vertex_count"] += int(
                            measured.get("precision") == "legacy_float32")
                        _count_frame(summary["frames"], measured.get("reference_frame"))
                    yield None
            finally:
                if rows is not None:
                    rows.close()
            if (self._event_sequence == revision and len(coordinates) == count and
                    len(measurements) == measurement_count):
                line["_summary"] = dict(summary)
                self._line_lengths[line["id"]] = summary["length_m"]
        self._finish_line_summary(result, summary, count)
        yield result

    @staticmethod
    def _finish_line_summary(result, summary, count):
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
            "max_vertical_step_m": round(summary.get("max_vertical_step_m", 0.0), 3),
            "legacy_vertex_count": summary.get("legacy_vertex_count", 0)})

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
                utc_text, unsynced = _utc_fields(measurement.get("recorded_at"))
                yield ("," if index != offset else "") + ujson.dumps({
                    "number": index + 1, "coordinate": coordinate,
                    "precision": measurement.get("precision"),
                    "segment_length_m": (round(_distance_m(previous, coordinate), 3)
                                         if previous else None),
                    "vertical_step_m": (round((coordinate[2] - previous[2]) /
                                              geo.ALT_SCALE, 3)
                                        if previous else None),
                    "source": measurement.get("source"),
                    "fix_status": measurement.get("fix_status"),
                    "satellites": measurement.get("satellites"),
                    "hdop": measurement.get("hdop"),
                    "correction_age_sec": measurement.get("correction_age_sec"),
                    "receiver_accuracy": measurement.get("receiver_accuracy"),
                    "accuracy_observation": measurement.get("accuracy_observation"),
                    "quality": self._expand_measurement(measurement).get("quality"),
                    "recorded_at_utc": utc_text, "clock_unsynced": unsynced,
                })
                previous = coordinate
        finally:
            if rows is not None:
                rows.close()
        yield "]}"
