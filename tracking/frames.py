# SPDX-License-Identifier: AGPL-3.0-only
"""Reference frames and heights of stored measurements, and the project frame assignment."""
import ujson
try:
    import hashlib
except ImportError:  # pragma: no cover
    import uhashlib as hashlib
import cfg_validation as _validation
from tracking.common import MAX_LINES_PER_PROJECT, _now_seconds, _utc_fields, _valid_timestamp


# What the exported altitude_msl_m and the GeoJSON coordinate's third value refer to.
HEIGHT_REFERENCE = "antenna_phase_centre_msl"
CRS_NOTE = ("Coordinates are not transformed. Horizontal positions are in the frame named by reference_frame on each "
            "feature: RTK solutions (fix_quality 4 or 5) in the correction service's frame, other solutions in "
            "WGS84/ITRF at observation time, unknown where no frame was configured. The third coordinate is the MSL "
            "height of the antenna phase centre, not the ellipsoidal height RFC 7946 expects; see altitude_ellipsoid_m. "
            'reference_frame_source says how the frame is known: "recorded" with the measurement, "assigned" '
            "afterwards by the user for RTK points of the project that had none (a statement, not a measurement), "
            "null where it is unknown.")
HEIGHT_KEYS = ("altitude_msl_m", "altitude_ellipsoid_m", "geoid_separation_m",
               "antenna_height_m", "ground_height_msl_m", "height_reference")


def _metres(millimetres):
    """Integer millimetres as float metres for output; None stays None."""
    return None if millimetres is None else millimetres / 1000


def _count_frame(tally, frame):
    """Add one measurement to a frame tally; a missing frame counts as "unknown"."""
    frame = frame or "unknown"
    tally[frame] = tally.get(frame, 0) + 1


def _uncount_frame(tally, frame):
    """Remove one measurement from a frame tally; a key that reaches zero is dropped."""
    frame = frame or "unknown"
    left = tally.get(frame, 0) - 1
    if left > 0:
        tally[frame] = left
    else:
        tally.pop(frame, None)


def _frame_fields(frames):
    """Project overview fields of a frame tally (the tally itself is copied)."""
    return {"frames": dict(frames), "mixed_frames": len(frames) > 1,
            "unknown_frames": len(frames) == 1 and frames.get("unknown", 0) > 0}


def _line_frame(tally):
    """The single frame of a tally, "mixed" for several, "unknown" for none."""
    if len(tally) == 1:
        return next(iter(tally))
    return "mixed" if tally else "unknown"


def _frame_properties(measurement, assignment=None):
    """Top-level frame, epoch, frame source, mountpoint and station of a stored measurement.

    assignment is the project's frame assignment for a measurement in its
    scope (else None); _effective_frame applies it.
    """
    frame, epoch, source = _effective_frame(measurement, assignment)
    return {"reference_frame": frame, "reference_frame_epoch": epoch,
            "reference_frame_source": source,
            "ntrip_mount": measurement.get("ntrip_mount"),
            "station_id": measurement.get("station_id")}


# A frame assigned to a project afterwards (#39). project["frame_assignment"]
# names the RTK measurements without a stored frame that were in the project
# when the user assigned it: {"reference_frame", "reference_frame_epoch",
# "assigned_at", "lines": {line_id: [n, c]}, "points": [k, c]}, with n vertices
# (k object points) in scope and c of them assignable. Stored measurements are
# never rewritten; the readers apply the assignment (_effective_frame). Every
# change replaces the whole dict, so its identity tells whether it changed.
# The frame scan reads at most FRAME_SCAN_BATCH vertices or opens at most
# FRAME_SCAN_WINDOWS line windows per scheduler step.
FRAME_SCAN_BATCH = 64
FRAME_SCAN_WINDOWS = 8
# A stale line summary (after an undo, a restore or an outdated checkpoint) is
# rebuilt before status() or an append in steps of SUMMARY_SCAN_BATCH vertex
# reads per scheduler turn, in at most SUMMARY_REFRESH_PASSES passes over the
# lines an event kept from caching (Tracker.refresh_summaries_async).
SUMMARY_SCAN_BATCH = 64
SUMMARY_REFRESH_PASSES = 2
FRAME_PREVIEW_LINES = 5
RESERVED_FRAME_NAMES = ("unknown", "mixed")
FRAME_ASSIGNMENT_KEYS = ("reference_frame", "reference_frame_epoch", "assigned_at",
                         "lines", "points")
_FRAME_ASSIGNMENT_MESSAGES = {
    "frame_scope_changed": "The points of this project changed during the check; "
                           "review the assignment again.",
    "frame_assignment_changed": "The frame assignment of this project changed meanwhile; "
                                "check it again.",
    "nothing_to_assign": "This project has no RTK points without a reference frame.",
    "no_frame_assignment": "This project has no frame assignment.",
    "recording_active": "Pause the line of this project before assigning a reference frame.",
    "frame_export_changed": "The frame assignment of this project changed during the export; "
                            "export again.",
}


class FrameAssignmentError(ValueError):
    """A frame assignment was refused because of the survey's state; code names it."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _frame_assignment_error(code):
    return FrameAssignmentError(code, _FRAME_ASSIGNMENT_MESSAGES[code])


def _rtk_measurement(measurement):
    """True for an RTK solution: fix quality 4 or 5, else (older data) the fix status."""
    quality = measurement.get("fix_quality")
    if quality is not None:
        return quality in (4, 5)
    return measurement.get("fix_status") in ("RTK_FIXED", "RTK_FLOAT")


def _assignable(measurement):
    """True for an RTK measurement stored without a frame, which an assignment may name."""
    return not measurement.get("reference_frame") and _rtk_measurement(measurement)


def _clean_assigned_frame(frame, epoch):
    """(frame, epoch or None) by the field setup's text rules; ValueError otherwise."""
    clean, error = _validation.validate_field_setup(
        {"reference_frame": frame, "reference_frame_epoch": epoch})
    if error:
        raise ValueError(error)
    if not clean["reference_frame"]:
        raise ValueError("A reference frame is required.")
    if clean["reference_frame"].lower() in RESERVED_FRAME_NAMES:
        raise ValueError('The frame names "unknown" and "mixed" are reserved.')
    return clean["reference_frame"], clean["reference_frame_epoch"] or None


def _scope_entry(value, empty_allowed):
    """True for an [n, c] scope entry: two ints with 0 <= c <= n (0 < c for a line)."""
    return (isinstance(value, list) and len(value) == 2 and
            type(value[0]) is int and type(value[1]) is int and
            (0 if empty_allowed else 1) <= value[1] <= value[0])


def _frame_assignment(project):
    """The project's frame assignment when it is structurally valid, else None.

    A damaged value (a hand-edited checkpoint) reads as no assignment, so it
    never stops a boot; a restore checks the counts exactly.
    """
    assignment = project.get("frame_assignment") if project else None
    if not isinstance(assignment, dict) or len(assignment) != 5:
        return None
    frame, epoch = assignment.get("reference_frame"), assignment.get("reference_frame_epoch")
    lines = assignment.get("lines")
    if (not isinstance(frame, str) or not frame or
            not (epoch is None or isinstance(epoch, str) and epoch) or
            not _valid_timestamp(assignment.get("assigned_at")) or
            not isinstance(lines, dict) or len(lines) > MAX_LINES_PER_PROJECT or
            not _scope_entry(assignment.get("points"), True)):
        return None
    for line_id, value in lines.items():
        if not isinstance(line_id, str) or not _scope_entry(value, False):
            return None
    return assignment


def _assignment_count(assignment):
    """How many measurements an assignment names."""
    return sum(value[1] for value in assignment["lines"].values()) + assignment["points"][1]


def _effective_frame(measurement, assignment):
    """(frame, epoch, source) a reader shows for a stored measurement.

    The stored frame wins ("recorded"); otherwise an RTK measurement takes the
    assignment's frame ("assigned"); otherwise it is "unknown" without source.
    The caller passes the assignment only for a measurement in its scope.
    """
    frame = measurement.get("reference_frame")
    if frame:
        return frame, measurement.get("reference_frame_epoch") or None, "recorded"
    if assignment is not None and _rtk_measurement(measurement):
        return assignment["reference_frame"], assignment["reference_frame_epoch"], "assigned"
    return "unknown", measurement.get("reference_frame_epoch") or None, None


def _assigned_tally(tally, frame, count):
    """A copy of a stored-frame tally with count "unknown" measurements moved to frame."""
    result = dict(tally)
    _move_frames(result, "unknown", frame, count)
    return result


def _move_frames(tally, source, target, count):
    """Move count measurements (at most all of source's) from source to target in place."""
    moved = min(count, tally.get(source, 0))
    if moved > 0:
        left = tally[source] - moved
        if left:
            tally[source] = left
        else:
            del tally[source]
        tally[target] = tally.get(target, 0) + moved


def _reassign_frames(tally, previous, assignment):
    """Keep a project's frame tally across an assignment change, without a scan.

    The previous assignment's measurements go back to "unknown", the new
    one's leave it: each assignment's count is exact (E2, E7), and the stored
    frames of its measurements are none.
    """
    if previous is not None:
        _move_frames(tally, previous["reference_frame"], "unknown",
                     _assignment_count(previous))
    if assignment is not None:
        _move_frames(tally, "unknown", assignment["reference_frame"],
                     _assignment_count(assignment))


def _frame_point_scope(points, count, frames):
    """(assignable, other_unknown) among the first count object points (RAM only).

    Their stored frames are counted into the tally frames as well.
    """
    assignable = other = 0
    for index in range(count):
        measurement = points[index].get("measurement") or {}
        _count_frame(frames, measurement.get("reference_frame"))
        if _assignable(measurement):
            assignable += 1
        elif not measurement.get("reference_frame"):
            other += 1
    return assignable, other


def _note_restored_frame_scope(state, project_id, line_id, measurement):
    """Count one restored measurement against its project's frame assignment (E10).

    state["frame_scopes"][project_id] is [assignment, counts] for a project
    whose backup record brought a structurally valid assignment; counts maps
    a line ID (None: the object points) to [measurements so far, assignable
    ones among the first n (k)]. RAM only, O(1) per record.
    """
    scope = state["frame_scopes"].get(project_id)
    if scope is None:
        return
    assignment, counts = scope
    if line_id is None:
        limit = assignment["points"][0]
    else:
        entry = assignment["lines"].get(line_id)
        if entry is None:
            return
        limit = entry[0]
    count = counts.get(line_id)
    if count is None:
        count = counts[line_id] = [0, 0]
    if count[0] < limit and _assignable(measurement):
        count[1] += 1
    count[0] += 1


def _restored_frame_scope_valid(assignment, counts):
    """True when a restored assignment's texts are clean and each c is the counted one."""
    for key in FRAME_ASSIGNMENT_KEYS:
        if key not in assignment:
            return False
    frame, epoch = assignment["reference_frame"], assignment["reference_frame_epoch"]
    try:
        if _clean_assigned_frame(frame, epoch or "") != (frame, epoch):
            return False
    except ValueError:
        return False
    for line_id, value in assignment["lines"].items():
        seen, counted = counts.get(line_id) or (0, 0)
        if value[0] > seen or value[1] != counted:
            return False
    seen, counted = counts.get(None) or (0, 0)
    return assignment["points"][0] <= seen and assignment["points"][1] == counted


def _height_fields(measurement, altitude_mm):
    """The derived height chain of one measurement, integer millimetres first.

    ``altitude_mm`` is the MSL height of the antenna phase centre (the stored
    coordinate's third value). Legacy measurements without an antenna height
    keep None: nothing is substituted from the current configuration.
    """
    antenna_mm = measurement.get("antenna_height_mm")
    ground_mm = (altitude_mm - antenna_mm
                 if altitude_mm is not None and antenna_mm is not None else None)
    return {"altitude_msl_m": _metres(altitude_mm),
            "altitude_ellipsoid_m": _metres(measurement.get("alt_ellipsoid_mm")),
            "geoid_separation_m": _metres(measurement.get("geoid_sep_mm")),
            "antenna_height_m": _metres(antenna_mm),
            "ground_height_msl_m": _metres(ground_mm),
            "height_reference": HEIGHT_REFERENCE}


def _control_vertical_delta_mm(first, latest):
    """Height change between two measurements of one control mark, in millimetres.

    With an antenna height on both measurements the ground heights are compared,
    so a pole change between them is not reported as movement of the mark.
    Otherwise (older points) the phase-centre heights are compared.
    Returns (delta_mm, "ground" | "phase_centre").
    """
    first_mm, latest_mm = first["coordinate"][2], latest["coordinate"][2]
    first_ant = first["measurement"].get("antenna_height_mm")
    latest_ant = latest["measurement"].get("antenna_height_mm")
    if type(first_ant) is int and type(latest_ant) is int:
        return (latest_mm - latest_ant) - (first_mm - first_ant), "ground"
    return latest_mm - first_mm, "phase_centre"


class FrameAssignmentMixin:
    def _frame_project(self, project_id):
        """The project a frame action names; ValueError when there is none."""
        project = self._project(project_id) if project_id else None
        if not project:
            raise ValueError("Project not found.")
        return project

    @staticmethod
    def _refuse_recording(project):
        """A frame is assigned only while no line of the project records (O3)."""
        for line in project["lines"]:
            if line.get("state") == "recording":
                raise _frame_assignment_error("recording_active")

    def _frame_scope_token(self, project):
        """Fingerprint of what a frame scan of the project sees now (E6); RAM only.

        It covers the project ID, the point-store generation, each line's
        vertex count and last locator, the object point count and the last
        point's ID, and the assignment (_assignment_token). Points are only
        appended or removed at the end with new locators and IDs, so an undo
        followed by a new point changes it even when the counts are equal.
        """
        store, digest = self.point_store, hashlib.sha256()
        digest.update(("%s\n%d\n" % (project["id"], store.generation)).encode("utf-8"))
        for line in project["lines"]:
            locators = store.offsets.get(line["id"], ())
            digest.update(("%s %d %d\n" % (line["id"], len(locators),
                                           locators[-1] if locators else 0)).encode("utf-8"))
        points = project["points"]
        digest.update(("%d %s\n%s" % (len(points), points[-1]["id"] if points else "",
                                      self._assignment_token(project) or "-")).encode("utf-8"))
        return self._hex_digest(digest)[:16]

    def _frame_scope_steps(self, project):
        """Count the measurements an assignment of the project would name (E4, E12).

        Each line's vertex count is taken before its first read; its vertices
        are read in windows of at most FRAME_SCAN_BATCH locators, each closed
        before the next yield. None follows FRAME_SCAN_BATCH vertex reads,
        FRAME_SCAN_WINDOWS windows and the last read; then the result. Object
        points are in RAM. The result carries the token from before the first
        read; the caller compares it with the token after the scan. Its
        "frames" is the tally of the stored frames of everything scanned, so
        the commit never rebuilds a missing project tally from the flash.
        """
        token = self._frame_scope_token(project)
        store, lines, shown, project_lines = self.point_store, {}, [], project["lines"]
        vertices = other = reads = windows = more = 0
        frames = {}
        for index in range(len(project_lines)):
            line = project_lines[index]
            count, assignable, start = len(store.offsets.get(line["id"], ())), 0, 0
            while start < count:
                stop = min(count, start + FRAME_SCAN_BATCH - reads)
                window = store.iter_line(line["id"], start, stop)
                try:
                    for value in window:
                        measurement = value["measurement"]
                        _count_frame(frames, measurement.get("reference_frame"))
                        if _assignable(measurement):
                            assignable += 1
                        elif not measurement.get("reference_frame"):
                            other += 1
                finally:
                    window.close()
                reads, windows, start = reads + stop - start, windows + 1, stop
                if reads >= FRAME_SCAN_BATCH or windows >= FRAME_SCAN_WINDOWS:
                    reads = windows = 0
                    yield None
            if assignable:
                lines[line["id"]] = [count, assignable]
                vertices += assignable
                if len(shown) < FRAME_PREVIEW_LINES:
                    shown.append({"id": line["id"], "name": line.get("name"),
                                  "count": assignable})
                else:
                    more += 1
        if reads or windows:
            yield None
        points = project["points"]
        assignable, point_other = _frame_point_scope(points, len(points), frames)
        yield {"lines": lines, "points": [len(points), assignable], "vertices": vertices,
               "point_count": assignable, "total": vertices + assignable,
               "other_unknown": other + point_other, "line_counts": shown,
               "more_lines": more, "token": token, "frames": frames}

    async def frame_assignment_preview_async(self, project_id):
        """What assign_frame_async would assign now (E5); reads, never writes."""
        project = self._frame_project(project_id)
        scan = await self._finish_steps_async(self._frame_scope_steps(project))
        return self._frame_preview(project, scan)

    def _frame_preview(self, project, scan):
        """The preview of a finished scan, refused when the points changed during it."""
        if (self._project(project["id"]) is not project or
                self._frame_scope_token(project) != scan["token"]):
            raise _frame_assignment_error("frame_scope_changed")
        summary = self._assignment_summary(project)
        return {"project_id": project["id"], "total": scan["total"],
                "new_unknown": max(0, scan["total"] - (summary["count"] if summary else 0)),
                "vertices": scan["vertices"], "points": scan["point_count"],
                "lines": scan["line_counts"], "more_lines": scan["more_lines"],
                "other_unknown": scan["other_unknown"], "assignment": summary,
                "scope_token": scan["token"]}

    # Assign a frame to the RTK points without one that the preview showed
    # (E2, E6): the request is checked, the project scanned, and the checks
    # after the scan run with no await before the journal append.
    async def assign_frame_async(self, project_id, reference_frame, reference_frame_epoch,
                                 expected_count, scope_token):
        request = self._frame_request(project_id, reference_frame, reference_frame_epoch,
                                      expected_count, scope_token)
        scan = await self._finish_steps_async(self._frame_scope_steps(request[0]))
        return self._commit_frame_assignment(request, scan)

    def _frame_request(self, project_id, frame, epoch, expected_count, scope_token):
        """Check an assignment request before the scan (E6); what the commit needs."""
        project = self._frame_project(project_id)
        frame, epoch = _clean_assigned_frame(frame, epoch)
        self._refuse_recording(project)
        if type(expected_count) is not int:
            raise ValueError("expected_count must be an integer.")
        if not isinstance(scope_token, str):
            raise ValueError("scope_token must be a text.")
        if self._frame_scope_token(project) != scope_token:
            raise _frame_assignment_error("frame_scope_changed")
        return project, frame, epoch, expected_count, scope_token

    def _commit_frame_assignment(self, request, scan):
        """Repeat the checks after the scan and journal the assignment."""
        project, frame, epoch, expected_count, scope_token = request
        if self._project(project["id"]) is not project:
            raise _frame_assignment_error("frame_scope_changed")
        self._refuse_recording(project)
        if self._frame_scope_token(project) != scope_token:
            raise _frame_assignment_error("frame_scope_changed")
        if not scan["total"]:
            raise _frame_assignment_error("nothing_to_assign")
        if scan["total"] != expected_count:
            raise _frame_assignment_error("frame_scope_changed")
        self._require_write_capacity(False)
        at = _now_seconds()
        return self._commit_frame_change(project, {
            "reference_frame": frame, "reference_frame_epoch": epoch, "assigned_at": at,
            "lines": scan["lines"], "points": scan["points"]}, scan["total"], at,
            scan["frames"])

    def revoke_frame_assignment(self, project_id, assignment_token):
        """Remove the frame assignment the caller showed, named by its token (E6)."""
        project = self._frame_project(project_id)
        self._check_revocation(project, assignment_token)
        return self._commit_frame_change(project, None, 0, _now_seconds())

    def _check_revocation(self, project, assignment_token):
        """Refuse a revocation of anything but the assignment the caller showed."""
        if not isinstance(assignment_token, str):
            raise ValueError("assignment_token must be a text.")
        if _frame_assignment(project) is None:
            raise _frame_assignment_error("no_frame_assignment")
        if assignment_token != self._assignment_token(project):
            raise _frame_assignment_error("frame_assignment_changed")

    def _commit_frame_change(self, project, assignment, count, at, scanned_frames=None):
        """Journal project_frame_assigned (assignment None revokes); return the new state.

        The handler keeps the project's frame tally. A missing one is built from
        the scan's stored-frame tally (scanned_frames): the scope token held, so
        it counts exactly the project's measurements now, and the assignment
        moves its count of them from "unknown" to its frame.
        """
        current, previous = _frame_assignment(project), None
        if current is not None:
            previous = {"reference_frame": current["reference_frame"],
                        "reference_frame_epoch": current["reference_frame_epoch"],
                        "assigned_at": current["assigned_at"],
                        "count": _assignment_count(current)}
        self._append({"event": "project_frame_assigned", "project_id": project["id"],
                      "at": at, "assignment": assignment, "previous": previous,
                      "count": count})
        if scanned_frames is not None and self._project_frames.get(project["id"]) is None:
            self._project_frames[project["id"]] = _assigned_tally(
                scanned_frames, assignment["reference_frame"], count)
        result = {"project_id": project["id"],
                  "frame_assignment": self._assignment_summary(project)}
        result.update(_frame_fields(self._project_frame_tally(project)))
        return result

    def _assignment_token(self, project):
        """Fingerprint of the project's whole frame assignment (E6), or None; RAM only.

        Canonical: lists of scalars only and the lines in sorted ID order, so
        the bytes never depend on dict order. Computed once per assignment
        object (_frame_tokens), since an assignment is replaced, never changed.
        """
        assignment = project.get("frame_assignment")
        entry = self._frame_tokens.get(project["id"])
        if entry is not None and entry[0] is assignment:
            return entry[1]
        if _frame_assignment(project) is None:
            return None
        digest, lines = hashlib.sha256(), assignment["lines"]
        digest.update(ujson.dumps([assignment["reference_frame"],
                                   assignment["reference_frame_epoch"],
                                   assignment["assigned_at"],
                                   assignment["points"]]).encode("utf-8"))
        for line_id in sorted(lines):
            digest.update(ujson.dumps([line_id, lines[line_id][0],
                                       lines[line_id][1]]).encode("utf-8"))
        token = self._hex_digest(digest)[:16]
        self._frame_tokens[project["id"]] = [assignment, token]
        return token

    def _assignment_summary(self, project):
        """Status and live view of the project's frame assignment, or None; RAM only."""
        token = self._assignment_token(project)
        if token is None:
            return None
        assignment = project["frame_assignment"]
        return {"reference_frame": assignment["reference_frame"],
                "reference_frame_epoch": assignment["reference_frame_epoch"],
                "assigned_at": assignment["assigned_at"],
                "assigned_at_utc": _utc_fields(assignment["assigned_at"])[0],
                "count": _assignment_count(assignment), "token": token}
