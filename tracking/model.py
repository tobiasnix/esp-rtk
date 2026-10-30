# SPDX-License-Identifier: AGPL-3.0-only
"""The event model: one handler per journal event kind, project activity and live map deltas."""
import geo
from tracking.common import MAX_LINE_GAP_M, MAX_VERTICAL_STEP_M, _profile_id_valid
from tracking.frames import (_assignable, _count_frame, _effective_frame, _frame_assignment,
                             _reassign_frames, _uncount_frame)
from tracking.fixes import _distance_m


# One handler per journal event kind (EVENT_HANDLERS). Tracker._apply runs the
# handler, then the part every kind shares: the project's activity time and,
# outside a replay, the live map change (MAP_CHANGE_BUILDERS) and the status
# cache. A handler returns False when the event changed nothing else; the
# shared part is then skipped.


def _on_project_created(tracker, event):
    created = event["project"]
    if not tracker._project(created["id"]):
        tracker.projects.append(created)
        if (tracker._replaying or created.get("lines") or created.get("points") or
                created.get("frame_assignment")):
            tracker._project_frames.pop(created["id"], None)
        else:
            # An empty new project: its tally is kept from the start.
            tracker._project_frames[created["id"]] = {}
    tracker.active_project_id = created["id"]


def _on_project_selected(tracker, event):
    tracker.active_project_id = event.get("project_id")
    tracker.active_line_id = None


def _on_line_started(tracker, event):
    tracker._pack_line(event["line"])
    project = tracker._project(event.get("project_id"))
    if project and not tracker._line(project, event["line"]["id"]):
        project["lines"].append(event["line"])
    tracker.active_project_id = event.get("project_id")
    tracker.active_line_id = event["line"]["id"]
    tracker.auto_distance_m = event.get("auto_distance_m", 1.0)
    tracker._line_lengths[event["line"]["id"]] = 0.0


def _on_profile_registered(tracker, event):
    profile = event.get("profile") or {}
    if profile.get("id") and isinstance(profile.get("limits"), dict):
        if not _profile_id_valid(profile["id"], profile["limits"]):
            raise ValueError("Invalid measurement profile ID.")
        tracker.profiles[profile["id"]] = profile


def _on_vertex_added(tracker, event):
    line = tracker._line(tracker._project(event.get("project_id")),
                         event.get("line_id"))
    if not line:
        return None
    if tracker._replaying:
        _replay_vertex(tracker, event, line)
        return False
    return _append_vertex(tracker, event, line)


def _replay_vertex(tracker, event, line):
    event["measurement"]["record_order"] = event.get("order", 0)
    tracker._register_profile(event["measurement"])
    if tracker.point_store.append(event.get("order", 0), line["id"],
                                  event["coordinate"], event["measurement"]):
        line.pop("_summary", None)
        # The load builds the tally after replaying the journal.
        tracker._project_frames.pop(event.get("project_id"), None)
        tracker._project_activity[event.get("project_id")] = event["measurement"].get("recorded_at")


def _append_vertex(tracker, event, line):
    """The live part of vertex_added; False when the store did not take the vertex."""
    previous = line["coordinates"][-1] if line["coordinates"] else None
    current_length = tracker._line_lengths.get(line["id"])
    if current_length is None:
        current_length = tracker._line_length(line)
    event["measurement"]["record_order"] = event.get("order", 0)
    tracker._register_profile(event["measurement"])
    phase = tracker._ticks_us()
    appended = tracker.point_store.append(event.get("order", 0), line["id"],
                                          event["coordinate"], event["measurement"])
    tracker._profile_add("pointstore_write", phase)
    if not appended:
        return False
    gap = _distance_m(previous, event["coordinate"]) if previous else 0.0
    vertical = (abs(event["coordinate"][2] - previous[2]) / geo.ALT_SCALE
                if previous else 0.0)
    frame = event["measurement"].get("reference_frame")
    frames = tracker._project_frames.get(event.get("project_id"))
    if frames is not None:
        _count_frame(frames, frame)
    line["_summary"] = _vertex_summary(tracker, line, event["measurement"], previous,
                                       current_length, gap, vertical, frame)
    if previous:
        tracker._line_lengths[line["id"]] = (current_length +
                                             _distance_m(previous, event["coordinate"]))
    else:
        tracker._line_lengths[line["id"]] = 0.0
    return None


def _vertex_summary(tracker, line, measurement, previous, current_length, gap,
                    vertical, frame):
    """The line's cached summary after one appended vertex."""
    summary = line.get("_summary")
    if summary is not None and ("legacy_vertex_count" not in summary or
                                "frames" not in summary):
        # A summary from before the precision count or the frame
        # tally is stale; rebuilding it never counts a vertex twice.
        summary = None
        line.pop("_summary", None)
    rebuilt = False
    stale = (not summary or
             summary.get("vertex_count") != len(line["coordinates"]) - 1)
    if stale and previous:
        # Missing, or not matching the stored vertices (restores
        # carry the backup's cached summary into line_started):
        # rebuild from the store, which now includes this vertex.
        line.pop("_summary", None)
        tracker._line_summary(line)
        summary = line.get("_summary")
        rebuilt = True
    elif not previous:
        # The first vertex: nothing before it to count, whatever a
        # restored line brought along as its cached summary.
        summary = {"vertex_count": len(line["coordinates"]) - 1,
                   "length_m": current_length,
                   "quality_accepted_count": 0,
                   "large_gap_count": 0, "max_gap_m": 0.0,
                   "large_vertical_step_count": 0,
                   "max_vertical_step_m": 0.0,
                   "overlapping": False, "legacy_vertex_count": 0,
                   "frames": {}}
    if not rebuilt:
        summary["vertex_count"] = len(line["coordinates"])
        summary["legacy_vertex_count"] += int(
            measurement.get("precision") == "legacy_float32")
        _count_frame(summary["frames"], frame)
        summary["length_m"] = current_length + gap
        summary["quality_accepted_count"] += int(bool(
            (measurement.get("quality") or {}).get("accepted")))
        summary["large_gap_count"] += int(gap > MAX_LINE_GAP_M)
        summary["max_gap_m"] = max(summary["max_gap_m"], gap)
        summary["large_vertical_step_count"] += int(
            vertical > MAX_VERTICAL_STEP_M)
        summary["max_vertical_step_m"] = max(
            summary["max_vertical_step_m"], vertical)
        summary["overlapping"] = summary["overlapping"] or bool(
            previous and gap < 0.05)
    return summary


def _on_asset_added(tracker, event):
    project = tracker._project(event.get("project_id"))
    if project:
        event["point"]["measurement"]["record_order"] = event.get("order", 0)
        tracker._register_profile(event["point"]["measurement"])
        project["points"].append(event["point"])
        frames = tracker._project_frames.get(project["id"])
        if frames is not None and not tracker._replaying:
            _count_frame(frames, event["point"]["measurement"].get("reference_frame"))
        else:
            tracker._project_frames.pop(project["id"], None)


def _event_line(tracker, event):
    """The line an event names by project_id and line_id, or None."""
    return tracker._line(tracker._project(event.get("project_id")), event.get("line_id"))


def _on_line_paused(tracker, event):
    line = _event_line(tracker, event)
    if line:
        line["state"] = "paused"
        line["pause_reason"] = event.get("reason")


def _on_line_resumed(tracker, event):
    line = _event_line(tracker, event)
    if line:
        line["state"] = "recording"
        line["pause_reason"] = None
        tracker.active_line_id = line["id"]


def _on_line_selected(tracker, event):
    project = tracker._project(event.get("project_id"))
    line = tracker._line(project, event.get("line_id"))
    if line and line.get("state") != "finished":
        tracker.active_project_id = project["id"]
        tracker.active_line_id = line["id"]


def _on_line_finished(tracker, event):
    line = _event_line(tracker, event)
    if line:
        line["state"] = "finished"
        line["finished_at"] = event.get("at")
        line["finish_diagnostics"] = event.get("diagnostics")
    tracker.active_line_id = None


def _on_feature_removed(tracker, event):
    project = tracker._project(event.get("project_id"))
    if not project:
        return False
    frames = None if tracker._replaying else tracker._project_frames.get(project["id"])
    if frames is None:
        tracker._project_frames.pop(project["id"], None)
    assignment = _frame_assignment(project)
    if event.get("feature_kind") == "asset":
        kept, removed = [], None
        in_scope = assignment["points"][0] if assignment else 0
        for index, point in enumerate(project["points"]):
            if point["id"] != event.get("feature_id"):
                kept.append(point)
                continue
            removed = (index, point.get("measurement") or {})
            if frames is not None:
                _uncount_frame(frames, _effective_frame(
                    removed[1], assignment if index < in_scope else None)[0])
        project["points"] = kept
        if removed is not None:
            _shrink_frame_scope(tracker, project, event, removed)
    elif event.get("feature_kind") == "vertex":
        line = tracker._line(project, event.get("line_id"))
        if line and line["coordinates"]:
            if frames is not None:
                # One read of the vertex being removed; undo is rare. A vertex
                # in the scope carries frame_scope (undo_last).
                _uncount_frame(frames, _effective_frame(
                    line["measurements"][-1],
                    assignment if event.get("frame_scope") is not None else None)[0])
            if len(line["coordinates"]) > 1 and not tracker._replaying:
                current_length = tracker._line_lengths.get(line["id"])
                if current_length is None:
                    current_length = tracker._line_length(line)
                tracker._line_lengths[line["id"]] = max(0.0,
                    current_length -
                    _distance_m(line["coordinates"][-2], line["coordinates"][-1]))
            tracker.point_store.remove_last(event.get("order", 0), line["id"])
            line.pop("_summary", None)
        # Outside the branch above: a replay meets a line that was emptied
        # later as empty (A7), and its scope must shrink all the same.
        if line:
            _shrink_frame_scope(tracker, project, event, None)
    return None


def _shrink_frame_scope(tracker, project, event, removed_point):
    """Shrink the project's frame assignment by one undone measurement in its scope (E7).

    A vertex takes the change from the event's frame_scope (1: it was
    assignable, 0: it was not), which undo_last writes only for a vertex in
    the scope; a replay cannot read the vertex (A7). removed_point is (index,
    measurement) of an undone object point, from RAM. The assignment is
    replaced by a new dict with new lists (E16); an emptied one stays.
    """
    assignment = _frame_assignment(project)
    if assignment is None:
        return
    points, line_id = list(assignment["points"]), None
    if removed_point is None:
        scope, line_id = event.get("frame_scope"), event.get("line_id")
        entry = assignment["lines"].get(line_id)
        if scope not in (0, 1) or entry is None:
            return
    else:
        index, measurement = removed_point
        if index >= points[0]:
            return
        points = [points[0] - 1, points[1] - (1 if _assignable(measurement) else 0)]
    lines = {}
    for key, value in assignment["lines"].items():
        if key != line_id:
            lines[key] = list(value)
        elif entry[1] - scope > 0:
            lines[key] = [entry[0] - 1, entry[1] - scope]
    project["frame_assignment"] = {
        "reference_frame": assignment["reference_frame"],
        "reference_frame_epoch": assignment["reference_frame_epoch"],
        "assigned_at": assignment["assigned_at"], "lines": lines, "points": points}
    tracker._frame_tokens.pop(project["id"], None)


def _on_snapshot(tracker, event):
    tracker.projects = event.get("projects", [])
    tracker._pack_projects()
    tracker._project_frames = {}
    tracker._frame_tokens = {}
    tracker.active_project_id = event.get("active_project_id")
    tracker.active_line_id = event.get("active_line_id")
    tracker.auto_distance_m = event.get("auto_distance_m", 1.0)
    tracker._project_activity = dict((project["id"], project.get("created_at"))
                                     for project in tracker.projects)
    tracker._line_lengths = {} if tracker._replaying else dict(
        (line["id"], tracker._line_length(line))
        for project in tracker.projects for line in project.get("lines", []))


def _on_project_renamed(tracker, event):
    project = tracker._project(event.get("project_id"))
    if project:
        project["name"] = event.get("name", project["name"])


def _on_project_deleted(tracker, event):
    tracker.projects = [project for project in tracker.projects
                        if project["id"] != event.get("project_id")]
    tracker._project_activity.pop(event.get("project_id"), None)
    tracker._project_frames.pop(event.get("project_id"), None)
    # Deleted or archived: the project's assignment token is not needed again.
    tracker._frame_tokens.pop(event.get("project_id"), None)
    if tracker.active_project_id == event.get("project_id"):
        tracker.active_project_id = tracker.active_line_id = None


def _on_project_frame_assigned(tracker, event):
    project = tracker._project(event.get("project_id"))
    if not project:
        return False
    previous, assignment = _frame_assignment(project), event.get("assignment")
    if isinstance(assignment, dict):
        # Always a new dict with new lists (never changed in place): readers
        # and exports detect a change by the object's identity.
        lines = assignment.get("lines")
        if isinstance(lines, dict):
            lines = dict((key, list(value) if isinstance(value, list) else value)
                         for key, value in lines.items())
        points = assignment.get("points")
        project["frame_assignment"] = {
            "reference_frame": assignment.get("reference_frame"),
            "reference_frame_epoch": assignment.get("reference_frame_epoch"),
            "assigned_at": assignment.get("assigned_at"), "lines": lines,
            "points": list(points) if isinstance(points, list) else points}
    else:
        project.pop("frame_assignment", None)
        tracker._frame_tokens.pop(project["id"], None)
    frames = None if tracker._replaying else tracker._project_frames.get(project["id"])
    if frames is None:
        # Built on its next read (the load builds it after replaying the
        # journal; assign_frame_async from its scan).
        tracker._project_frames.pop(project["id"], None)
    else:
        # Kept, not rebuilt: rebuilding would rescan every line whose summary
        # an undo left stale, with no yield (B7).
        _reassign_frames(frames, previous, _frame_assignment(project))
    return None


EVENT_HANDLERS = {
    "project_created": _on_project_created, "project_selected": _on_project_selected,
    "line_started": _on_line_started, "profile_registered": _on_profile_registered,
    "vertex_added": _on_vertex_added, "asset_added": _on_asset_added,
    "line_paused": _on_line_paused, "line_resumed": _on_line_resumed,
    "line_selected": _on_line_selected, "line_finished": _on_line_finished,
    "feature_removed": _on_feature_removed, "snapshot": _on_snapshot,
    "project_renamed": _on_project_renamed, "project_deleted": _on_project_deleted,
    "project_frame_assigned": _on_project_frame_assigned}

# Kinds whose time (_event_activity) becomes the project's latest activity.
ACTIVITY_EVENTS = ("project_created", "line_started", "vertex_added", "asset_added",
                   "line_paused", "line_resumed", "line_finished",
                   "feature_removed", "project_renamed", "project_frame_assigned")


def _event_activity(kind, event):
    """(project_id, time) that an event contributes to its project's activity."""
    if kind == "project_created":
        return event["project"]["id"], event["project"].get("created_at")
    if kind == "line_started":
        return event.get("project_id"), event["line"].get("created_at")
    if kind == "vertex_added":
        return event.get("project_id"), event["measurement"].get("recorded_at")
    if kind == "asset_added":
        return event.get("project_id"), event["point"].get("measurement", {}).get("recorded_at")
    return event.get("project_id"), event.get("at")


def _vertex_change(tracker, event):
    return {"op": "vertex", "project_id": event.get("project_id"),
            "line_id": event.get("line_id"),
            "coordinate": event.get("coordinate"),
            "precision": (event.get("measurement") or {}).get("precision")}


def _asset_change(tracker, event):
    point = event.get("point") or {}
    return {"op": "asset", "project_id": event.get("project_id"),
            "point": {"id": point.get("id"),
                      "type": point.get("object_type"),
                      "name": point.get("name"),
                      "note": point.get("note", ""),
                      "coordinate": point.get("coordinate"),
                      "line_id": point.get("line_id"),
                      "chainage_m": point.get("chainage_m"),
                      "measurement": tracker._map_measurement(point.get("measurement"))}}


def _removal_change(tracker, event):
    return {"op": "remove", "project_id": event.get("project_id"),
            "feature_kind": event.get("feature_kind"),
            "feature_id": event.get("feature_id"),
            "line_id": event.get("line_id")}


def _line_change(tracker, event):
    line = tracker._line(tracker._project(event.get("project_id")),
                         event.get("line_id") or (event.get("line") or {}).get("id"))
    if not line:
        return None
    length = tracker._line_lengths.get(line.get("id"))
    if length is None:
        length = tracker._line_length(line)
    return {"op": "line", "project_id": event.get("project_id"),
            "line": {"id": line.get("id"), "name": line.get("name"),
                     "state": line.get("state"),
                     "vertex_count": len(line.get("coordinates", [])),
                     "length_m": round(length, 3)}}


# Live map delta per kind (Tracker.iter_map_data since_revision).
MAP_CHANGE_BUILDERS = {
    "vertex_added": _vertex_change, "asset_added": _asset_change,
    "feature_removed": _removal_change, "line_started": _line_change,
    "line_paused": _line_change, "line_resumed": _line_change,
    "line_finished": _line_change}
