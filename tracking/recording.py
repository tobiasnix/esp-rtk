# SPDX-License-Identifier: AGPL-3.0-only
"""Live recording: lines, vertices, asset points, undo and the fix worker."""
import time
from tracking.common import (CHECKPOINT_BATCH_EVENTS, MAX_LINES_PER_PROJECT, OBJECT_TYPES,
                             _STAKEOUT_ONLY, _now_seconds)
from tracking.guards import (REQUEST_TAG_LIMIT, StorageBusy, UNDO_UNCHECKED, UndoCandidateChanged,
                             _new_inventory_epoch)
from tracking.frames import _assignable, _frame_assignment
from tracking.validation import _clean_text, _validate_properties
from tracking.fixes import (_distance_m, _gate_current, _setup_stamps, project_to_line,
                            quality_check)


class RecordingMixin:
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
            # An unusable position must fail before the line exists.
            self._coordinate(fix)
        line = {"id": self._id("line"), "name": _clean_text(name),
                "state": "recording", "created_at": _now_seconds(),
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
        if self.feature_count() >= self.feature_limit():
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

    def add_asset(self, object_type, name, note, fix, link_to_active_line=True, stakeout=None):
        """Append an object point; stakeout (a confirmation's tid, gen, cmd, mode,
        tolerance_m, dn_m, de_m, dist_m) makes it the point of a stakeout target."""
        project = self._project()
        if not project:
            raise ValueError("Select a project first.")
        if self.feature_count() >= self.feature_limit():
            raise ValueError("Tracking point limit reached.")
        if (stakeout is not None or object_type == "stakeout") and (
                object_type != "stakeout" or not isinstance(stakeout, dict) or
                stakeout.get("mode") not in ("verified", "manual")):
            raise ValueError(_STAKEOUT_ONLY)
        self._require_write_capacity(False)
        if object_type not in OBJECT_TYPES:
            raise ValueError("Unknown object type.")
        # "Found on site" keeps a rejected fix; its verdict stays in measurement.quality.
        measurement = self._measurement(fix, stakeout is not None and stakeout["mode"] == "manual")
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
        if stakeout is not None:
            from stakeout_progress import valid_point_fields
            measurement["source"] = "stakeout_" + stakeout["mode"]
            for key in ("tid", "gen", "cmd", "tolerance_m", "dn_m", "de_m", "dist_m"):
                point["stakeout_" + key] = stakeout.get(key)
            if not valid_point_fields(point):
                raise ValueError(_STAKEOUT_ONLY)
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
                          "line_id": line["id"], "at": _now_seconds(),
                          "reason": reason})

    def set_line_state(self, action, diagnostics=None):
        project, line = self._project(), self._line(self._project())
        if not line:
            raise ValueError("No active line exists.")
        mapping = {"pause": "line_paused", "resume": "line_resumed",
                   "finish": "line_finished"}
        if action not in mapping:
            raise ValueError("Unknown line action.")
        if action == "resume" and self.feature_count() >= self.feature_limit():
            raise ValueError("Archive or delete survey data before resuming the line.")
        if action == "resume":
            self._require_write_capacity(False)
        event = {"event": mapping[action], "project_id": project["id"],
                 "line_id": line["id"], "at": _now_seconds()}
        if action == "finish" and diagnostics:
            event["diagnostics"] = diagnostics
        self._append(event)

    def undo_candidate(self):
        """The undo candidate of the active project (see _undo_candidate), None without a project."""
        project = self._project()
        return self._undo_candidate(project) if project else None

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
        record_order, kind, feature_id, line_id = sorted(candidates)[-1]
        index = None
        if kind == "asset":
            item = next((value for value in project["points"]
                         if value["id"] == feature_id), None)
            label = (item or {}).get("name") or "Object point"
        else:
            line = self._line(project, line_id)
            index = len((line or {}).get("measurements", []))
            label = "%s · V-%d" % ((line or {}).get("name", "Line"), index)
        key = (kind, feature_id, index, record_order)
        request_id = None
        for tagged, tag in self._request_tags:
            if tagged == key:
                request_id = tag
        return {"kind": kind, "feature_id": feature_id, "line_id": line_id,
                "label": label, "index": index, "record_order": record_order,
                "request_id": request_id, "inventory_epoch": self.inventory_epoch}

    def undo_last(self, expect=UNDO_UNCHECKED):
        """Remove the undo candidate; with expect only if it is still that candidate (#21c E7).

        expect None means the client saw no candidate; a dict names the
        inventory_epoch, kind, feature_id, index and record_order it saw. Check
        and append run without a suspension point in between."""
        project = self._project()
        if not project:
            raise ValueError("Select a project first.")
        candidate = self._undo_candidate(project)
        if expect is None:
            raise UndoCandidateChanged()
        if expect is not UNDO_UNCHECKED and not isinstance(expect, dict):
            raise ValueError("The undo expectation must be an object.")
        if not candidate:
            raise ValueError("No recorded point can be undone.")
        if expect is not UNDO_UNCHECKED and any(
                candidate[key] != expect.get(key) for key in
                ("inventory_epoch", "kind", "feature_id", "index", "record_order")):
            raise UndoCandidateChanged()
        event = {"event": "feature_removed", "project_id": project["id"],
                 "feature_kind": candidate["kind"], "feature_id": candidate["feature_id"],
                 "line_id": candidate["line_id"], "at": _now_seconds()}
        assignment = _frame_assignment(project)
        entry = (assignment["lines"].get(candidate["line_id"])
                 if assignment and candidate["kind"] == "vertex" else None)
        if entry:
            measurements = self._line(project, candidate["line_id"])["measurements"]
            if len(measurements) <= entry[0]:
                # A vertex in the frame assignment's scope: the replay cannot
                # read it (A7), so the event says whether it was assignable.
                event["frame_scope"] = 1 if _assignable(measurements[-1]) else 0
        self._append(event)

    def tag_request(self, request_id, kind, feature_id=None):
        """Mark the undo candidate as created by request_id, in RAM only (#21c E19).

        Only when the candidate is the point just recorded: kind and feature_id
        (a vertex without one: the active line). True when marked."""
        project = self._project()
        if not project:
            return False
        if feature_id is None and kind == "vertex":
            line = self._line(project)
            if not line:
                return False
            feature_id = line["id"]
        candidate = self._undo_candidate(project)
        if not candidate or candidate["kind"] != kind or candidate["feature_id"] != feature_id:
            return False
        self._request_tags.append(((kind, feature_id, candidate["index"],
                                    candidate["record_order"]), request_id))
        while len(self._request_tags) > REQUEST_TAG_LIMIT:
            self._request_tags.pop(0)
        return True

    def _renew_inventory(self):
        """A new inventory epoch and no request tags: the survey was exchanged (#21c E21)."""
        epoch = _new_inventory_epoch()
        while epoch == self.inventory_epoch:
            epoch = _new_inventory_epoch()
        self.inventory_epoch = epoch
        self._request_tags = []

    def on_fix(self, fix):
        """enqueue_fix() plus the worker's write, for tools and tests."""
        queued = self.enqueue_fix(fix)
        while queued and self._pending_fixes:
            self.add_vertex(self._pending_fixes.pop(0), source="automatic")
        return bool(queued)

    def enqueue_fix(self, fix, quality=None, origin=None):
        """Validate and queue an automatic point without touching flash.

        quality is the gate verdict AppState already judged this fix version
        with, origin its _fix_seq (the published GGA; a GST enrichment keeps
        it). With quality no second quality_check runs here, and the queued copy
        carries "_verdict" (generation, quality, origin) for the write. A GST
        that enriches a queued point adopts its verdict only for the same
        origin; another GGA of the same UTC voids it, the write judges again.
        """
        if not self._loaded:
            return False
        line = self._line(self._project())
        if not line or line["state"] != "recording" or not self.auto_distance_m:
            return False
        route = line.get("recording_mode") == "route"
        if route and fix.get("route_ready") is False:
            return False
        if not route and not (quality if quality is not None
                              else quality_check(fix))["accepted"]:
            return False
        epoch = fix.get("utc")
        try:
            coordinate = self._coordinate(fix)
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
                        verdict = queued.get("_verdict")
                        if (quality is not None and verdict is not None and
                                origin is not None and verdict[2] == origin):
                            queued["_verdict"] = (_gate_current()[0], quality, origin)
                        else:
                            queued.pop("_verdict", None)
                        break
            return False
        previous = (self._last_queued_coordinate or
                    (line["coordinates"][-1] if line["coordinates"] else None))
        if previous and _distance_m(previous, coordinate) < self.auto_distance_m:
            return False
        if len(self._pending_fixes) >= 64:
            return None
        queued = dict(fix)
        queued["_stamps"] = _setup_stamps()
        if quality is not None:
            queued["_verdict"] = (_gate_current()[0], quality, origin)
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
            if self.storage_busy or self._install_pending:
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
                if (self._loaded and
                        self._checkpoint_dirty_events >= CHECKPOINT_BATCH_EVENTS):
                    started = time.ticks_ms()
                    try:
                        async with self.maintenance("tracking_checkpoint",
                                                    allow_recording=True):
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
            line = self._line(self._project())
            if line and line["coordinates"] and not self._summary_current(line):
                # An undo dropped the summary: rebuild it cooperatively instead
                # of the synchronous scan add_vertex would run, then try again.
                try:
                    ready = await self.refresh_summaries_async((line,))
                except StorageBusy:
                    ready = False
                if not ready:
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
