# SPDX-License-Identifier: AGPL-3.0-only
"""Survey backups and the one restore entry with its staged, power-cut safe activation."""
import os
import ujson
from pointstore import FlashPointStore, FlashSequence
try:
    import hashlib
except ImportError:  # pragma: no cover
    import uhashlib as hashlib
from cfg import log
from tracking.common import (LEGACY_SCHEMA_VERSIONS, MAX_LINES_PER_PROJECT, MAX_PROJECTS,
                             SCHEMA_VERSION, _ENOENT, _add_time_fields, _expanded_archive_record,
                             _profile_id_valid, _strip_derived_fields)
from tracking.frames import (_frame_assignment, _note_restored_frame_scope,
                             _restored_frame_scope_valid)
from tracking.validation import (_NOTHING_CONVERTED, _SERVICE_PAUSE, _feature_total,
                                 _legacy_conversion_error, _legacy_coordinate, _legacy_error_count,
                                 _payload_records, _require_precision, _tag_legacy,
                                 _upgrade_legacy_line, _upgrade_legacy_point, _validate_coordinate,
                                 _validate_measurement, upgrade_restore_backup, validate_backup)


class RestoreMixin:
    def backup(self):
        projects = []
        for project in self.projects:
            output = dict(project); output["lines"], output["points"] = [], []
            for line in project["lines"]:
                item = dict(line)
                item["coordinates"] = list(line["coordinates"])
                item["measurements"] = [_add_time_fields(self._expand_measurement(value))
                                        for value in line["measurements"]]
                output["lines"].append(item)
            for point in project["points"]:
                item = dict(point)
                item["measurement"] = _add_time_fields(
                    self._expand_measurement(point["measurement"]))
                output["points"].append(item)
            projects.append(output)
        return {"schema_version": SCHEMA_VERSION, "kind": "survey_backup",
                "profiles": list(self.profiles.values()), "projects": projects,
                "active_project_id": self.active_project_id,
                "active_line_id": self.active_line_id,
                "auto_distance_m": self.auto_distance_m}

    def iter_backup_ndjson(self, project_id=None):
        """Yield an integrity-protected schema-5 backup one record at a time.

        Coordinates are written as the stored integer triple, so a device
        parsing the backup with 32-bit floats still reads them exactly. Each
        measurement carries the derived time fields; restore strips them, so a
        backup -> restore -> backup round trip is content-identical except
        record_order, which restore renumbers in backup order; a second round
        trip is identical. Bytes match only where the JSON writer keeps key
        order, as on a host; ujson writes keys in hash order, so a device's
        second backup differs in key order and content_sha256. Sorting would
        cost CPU and RAM per record for no gain: restore reads records by key.
        """
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
        # The frame assignment each project record carries; a change before the
        # footer stops the backup without its integrity record (E16).
        exported = []
        for project in selected:
            metadata = dict(project); metadata.pop("lines", None); metadata.pop("points", None)
            assignment = project.get("frame_assignment")
            exported.append((project, assignment))
            yield encoded({"record": "project", "project": metadata})
            for line in project["lines"]:
                self._check_export_snapshot(project, assignment, True)
                metadata = dict(line)
                coordinates = metadata.pop("coordinates", [])
                measurements = metadata.pop("measurements", [])
                yield encoded({"record": "line", "project_id": project["id"],
                               "line": metadata})
                rows = (coordinates.store.iter_line(coordinates.line_id)
                        if isinstance(coordinates, FlashSequence) else None)
                try:
                    for index in range(len(coordinates)):
                        self._check_export_snapshot(project, assignment)
                        row = next(rows) if rows is not None else None
                        coordinate = row["coordinate"] if row is not None else coordinates[index]
                        measurement = row["measurement"] if row is not None else measurements[index]
                        yield encoded({"record": "vertex", "project_id": project["id"],
                                       "line_id": line["id"], "coordinate": coordinate,
                                       "measurement": _add_time_fields(
                                           self._expand_measurement(measurement))})
                        self.operation_progress["processed"] += 1
                finally:
                    if rows is not None: rows.close()
            self._check_export_snapshot(project, assignment, True)
            for point in project["points"]:
                self._check_export_snapshot(project, assignment)
                yield encoded({"record": "asset", "project_id": project["id"],
                               "point": dict(point, measurement=_add_time_fields(
                                   self._expand_measurement(point["measurement"])))})
                self.operation_progress["processed"] += 1
        for project, assignment in exported:
            self._check_export_snapshot(project, assignment, True)
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

    def _fresh_restore_store(self):
        if self._restore_published():
            raise ValueError("A published survey restore is not installed yet.")
        self._discard_restore_staging()
        return type(self)(self.path + ".restore")

    def restore_ndjson(self, lines):
        return self._finish_steps(self._restore_ndjson_steps(lines))

    async def restore_ndjson_async(self, lines):
        return await self._finish_steps_async(self._restore_ndjson_steps(lines))

    def _restore_ndjson_steps(self, lines):
        """An NDJSON backup (schemas 2-5) through the common restore entry."""
        check = {}
        return self._restore_steps(self._ndjson_records(lines, check), check)

    def restore(self, payload):
        """Restore a JSON backup (schema 1-5); returns the same summary as restore_ndjson."""
        return self._finish_steps(self._restore_payload_steps(payload, True))

    async def restore_async(self, payload):
        """Restore a JSON backup while yielding between flash steps (WDT)."""
        return await self._finish_steps_async(self._restore_payload_steps(payload, False))

    def _restore_payload_steps(self, payload, copy_result):
        """A JSON backup through the common restore entry, with operation progress."""
        progress = self.operation_progress
        progress.update(operation="restore", state="validating", processed=0, total=0, error=None)
        errors_before = _legacy_error_count()
        try:
            yield _SERVICE_PAUSE
            payload = upgrade_restore_backup(payload, copy_result=copy_result)
            yield _SERVICE_PAUSE
            payload = validate_backup(payload, copy_result=copy_result)
            yield _SERVICE_PAUSE
            progress.update(state="writing", total=_feature_total(payload["projects"]))
            steps, result = self._restore_steps(payload, None, progress, errors_before), None
            try:
                for item in steps:
                    if item is None or item is _SERVICE_PAUSE: yield item
                    else: result = item; yield None
            finally: steps.close()
            yield _SERVICE_PAUSE
            progress.update(state="complete", processed=progress["total"])
            yield result
        except Exception as error:
            progress.update(state="failed", error=str(error))
            raise

    def _restore_steps(self, source, check=None, progress=None, errors_before=None):
        """The one restore entry: stage a backup, verify it, publish it once.

        source is a validated schema-5 JSON payload (_restore_payload_steps)
        or the records of an NDJSON backup (_ndjson_records, which fills check
        for _check_integrity). It is staged in steps (one per event, vertices
        by four; _SERVICE_PAUSE after each long write) and published once by
        _commit_staged_restore_steps; the last step yields the summary.
        """
        if errors_before is None: errors_before = _legacy_error_count()
        staging, summary = None, {}
        try:
            staging = self._fresh_restore_store()
            staging._batch_restore = True
            events = staging._events_for_backup(source, self.feature_limit(), check, summary)
            vertices = 0
            try:
                for event in events:
                    if event is None: yield None; continue
                    staging._append(event, defer_index=True)
                    kind = event["event"]
                    if progress is not None and kind in ("vertex_added", "asset_added"):
                        progress["processed"] += 1
                    vertices += int(kind == "vertex_added")
                    if kind != "vertex_added" or vertices % 4 == 0: yield None
            finally: events.close()
            staging.point_store.sync(); yield _SERVICE_PAUSE
            staging._write_checkpoint(); yield _SERVICE_PAUSE
            staging._write_index(); yield _SERVICE_PAUSE
            commit = self._commit_staged_restore_steps(staging, summary)
            try:
                for _ in commit: yield None
            finally: commit.close()
            yield {"schema_version": SCHEMA_VERSION, "projects": len(summary["projects"]),
                   "features": summary["features"],
                   "legacy_values_skipped": _legacy_error_count() - errors_before}
        finally:
            if staging is not None: self._drop_restore_staging(staging)
            close = getattr(source, "close", None)
            if close: close()

    def _ndjson_records(self, lines, check):
        """Parse NDJSON backup rows; check receives what the integrity record must match."""
        limit = self.feature_limit() + MAX_LINES_PER_PROJECT * MAX_PROJECTS + MAX_PROJECTS + 3
        digest, records, integrity = hashlib.sha256(), 0, None
        try:
            for raw in lines:
                if not raw or not raw.strip(): continue
                records += 1
                if records > limit:
                    raise ValueError("Survey backup contains too many records.")
                value = _expanded_archive_record(ujson.loads(raw))
                if value.get("record") == "integrity":
                    if integrity is not None: raise ValueError("Duplicate integrity record.")
                    integrity = value; continue
                if integrity is not None: raise ValueError("Records follow integrity record.")
                digest.update(raw.encode("utf-8"))
                yield value
            check.update(integrity=integrity, records=records - 1,
                         content_sha256=self._hex_digest(digest))
        finally:
            close = getattr(lines, "close", None)
            if close: close()

    @staticmethod
    def _check_integrity(check, schema):
        # Schema 4 introduced the integrity record; older backups never had one.
        # One that is present is always checked, so a header relabelled to an
        # older schema cannot skip it.
        integrity = check.get("integrity")
        if schema >= 4 or integrity is not None:
            if (not integrity or integrity.get("algorithm") != "sha256" or
                    integrity.get("content_sha256") != check.get("content_sha256") or
                    integrity.get("records") != check.get("records")):
                raise ValueError("Survey backup integrity check failed.")

    def _events_for_backup(self, source, limit=None, check=None, summary=None):
        """Yield the events that rebuild a backup in this (staging) store.

        source is a JSON payload, whose records are closed here, or backup
        records, which the caller closes. The caller appends every
        yielded event before resuming; None marks a record that adds nothing.
        Profiles and the footer's selection are set on this store directly.
        Rows of schema <= 4 are converted value by value as on load: a vertex
        or asset with an invalid coordinate is left out (legacy_values_skipped),
        an invalid projected_coordinate becomes None.
        """
        records = _payload_records(source) if isinstance(source, dict) else source
        limit = self.feature_limit() if limit is None else limit
        state = {"schema": None, "legacy": False, "footer": None, "line": None,
                 "untrusted": (), "projects": [], "ids": set(), "features": 0,
                 "legacy_features": 0, "records": 0, "frame_scopes": {}}
        try:
            for value in records:
                state["records"] += 1
                kind = value.get("record")
                if kind != "header" and state["schema"] is None:
                    raise ValueError("Survey backup header must come first.")
                if state["footer"] is not None:
                    raise ValueError("Records follow the backup footer.")
                if kind in ("project", "line", "asset", "footer"):
                    ended = self._line_end_event(state)
                    if ended: yield ended
                if kind == "header": self._restore_header(value, state)
                elif kind == "project":
                    project = dict(value.get("project") or {})
                    self._restore_id(project.get("id"), state, "project")
                    project["lines"], project["points"] = [], []
                    state["projects"].append(project.get("id"))
                    assignment = _frame_assignment(project)
                    if assignment is not None:
                        state["frame_scopes"][project.get("id")] = [assignment, {}]
                    yield {"event": "project_created", "project": project}
                elif kind == "line": yield self._restore_line_event(value, state)
                elif kind == "vertex": yield self._restore_vertex_event(value, state)
                elif kind == "asset": yield self._restore_asset_event(value, state)
                elif kind == "footer": state["footer"] = value
                else: raise ValueError("Survey backup record is invalid.")
        finally:
            if records is not source: records.close()
        self._finish_backup_events(state, limit, check, summary)

    def _restore_id(self, value, state, label):
        if value in state["ids"]: raise ValueError("Duplicate %s ID." % label)
        state["ids"].add(value)

    def _restore_header(self, value, state):
        schema = value.get("schema_version")
        if state["schema"] is not None: raise ValueError("Duplicate backup header.")
        if (value.get("kind") != "survey_backup" or
                schema not in LEGACY_SCHEMA_VERSIONS + (SCHEMA_VERSION,)):
            raise ValueError("Survey backup header is invalid.")
        state["schema"], state["legacy"] = schema, schema in LEGACY_SCHEMA_VERSIONS
        profiles = value.get("profiles", [])
        validate_backup({"kind": "survey_backup", "schema_version": SCHEMA_VERSION,
                         "projects": [], "profiles": profiles}, copy_result=False)
        # A profile whose ID does not derive from its limits is not registered
        # (replay would reject it); see _restore_measurement for references.
        self.profiles = dict((p["id"], p) for p in profiles
                             if _profile_id_valid(p["id"], p["limits"]))
        state["untrusted"] = set(p["id"] for p in profiles if p["id"] not in self.profiles)

    def _restore_line_event(self, value, state):
        line = dict(value.get("line") or {})
        self._restore_id(line.get("id"), state, "line")
        if value.get("project_id") not in state["projects"]: raise ValueError("Unknown project.")
        if state["legacy"]: _upgrade_legacy_line(line)
        wanted = dict(line, project_id=value.get("project_id"))
        line["coordinates"], line["measurements"] = [], []
        state["line"] = wanted
        return {"event": "line_started", "project_id": value.get("project_id"),
                "line": line, "auto_distance_m": 0}

    @staticmethod
    def _line_end_event(state):
        line, state["line"] = state["line"], None
        if not line or line.get("state") not in ("paused", "finished"): return None
        event = {"event": "line_" + line["state"], "project_id": line["project_id"],
                 "line_id": line["id"]}
        if line["state"] == "paused": event["reason"] = line.get("pause_reason")
        else: event.update(at=line.get("finished_at"), diagnostics=line.get("finish_diagnostics"))
        return event

    def _restore_vertex_event(self, value, state):
        line = state["line"]
        if not line or line["id"] != value.get("line_id"):
            raise ValueError("A vertex precedes its line.")
        if value.get("project_id") != line["project_id"]:
            raise ValueError("Vertex belongs to another project.")
        coordinate, measurement = value.get("coordinate"), value.get("measurement")
        _validate_measurement(measurement)
        _strip_derived_fields(measurement)
        if state["legacy"]:
            state["legacy_features"] += 1
            coordinate = _legacy_coordinate(coordinate, "coordinate", measurement)
            if coordinate is None:
                _legacy_conversion_error("line %s vertex %s" % (line["id"], measurement.get(
                    "record_order", "at record %d" % state["records"])), "coordinate")
                return None
            _tag_legacy(measurement)
        _validate_coordinate(coordinate)
        self._restore_measurement(measurement, state)
        _note_restored_frame_scope(state, line["project_id"], line["id"], measurement)
        return {"event": "vertex_added", "project_id": value.get("project_id"),
                "line_id": value.get("line_id"), "coordinate": coordinate,
                "measurement": measurement}

    def _restore_asset_event(self, value, state):
        point = value.get("point") or {}
        self._restore_id(point.get("id"), state, "asset")
        if value.get("project_id") not in state["projects"]: raise ValueError("Unknown project.")
        if state["legacy"]:
            state["legacy_features"] += 1
            if isinstance(point, dict) and not _upgrade_legacy_point(point): return None
        else: _validate_measurement(point.get("measurement"))
        _strip_derived_fields(point.get("measurement"))
        self._restore_measurement(point["measurement"], state)
        _note_restored_frame_scope(state, value.get("project_id"), None, point["measurement"])
        return {"event": "asset_added", "project_id": value.get("project_id"),
                "point": value.get("point")}

    def _restore_measurement(self, measurement, state):
        _require_precision(measurement)
        # A reference to an untrusted header ID is derived anew from its inline
        # limits on append (_register_profile); any other is unknown.
        profile_id = measurement.get("profile_id")
        if profile_id and profile_id not in self.profiles and (
                profile_id not in state["untrusted"] or
                not (measurement.get("quality") or {}).get("limits")):
            raise ValueError("Unknown measurement profile.")
        state["features"] += 1

    def _finish_backup_events(self, state, limit, check, summary):
        """Check a completely read backup and apply its footer to this store."""
        footer = state["footer"]
        if state["schema"] is None or footer is None:
            raise ValueError("Survey backup is incomplete.")
        if check is not None: self._check_integrity(check, state["schema"])
        if state["legacy_features"] and not state["features"]:
            raise ValueError(_NOTHING_CONVERTED)
        if state["features"] > limit: raise ValueError("Survey backup exceeds active limit.")
        self.active_project_id = footer.get("active_project_id")
        self.active_line_id = footer.get("active_line_id")
        self.auto_distance_m = footer.get("auto_distance_m", 1.0)
        metadata = {"kind": "survey_backup", "schema_version": SCHEMA_VERSION,
                    "projects": [dict(project, lines=[dict(line, coordinates=[], measurements=[])
                                                      for line in project["lines"]])
                                 for project in self.projects],
                    "profiles": list(self.profiles.values()),
                    "active_project_id": self.active_project_id,
                    "active_line_id": self.active_line_id,
                    "auto_distance_m": self.auto_distance_m}
        validate_backup(metadata, copy_result=False)
        self._check_frame_assignments(state)
        if summary is not None:
            summary.update(projects=state["projects"], features=state["features"],
                           active_project_id=self.active_project_id,
                           active_line_id=self.active_line_id)

    def _check_frame_assignments(self, state):
        """Refuse a restored frame assignment unless it names exactly what it counts (E10).

        Beyond the structure (_frame_assignment), the texts must be clean as
        on input, n (k) must not exceed the restored vertices (object points)
        and c must equal the assignable measurements among the first n (k),
        counted while the records were read (_note_restored_frame_scope).
        """
        for project in self.projects:
            if "frame_assignment" not in project:
                continue
            scope = state["frame_scopes"].get(project["id"])
            if scope is None or not _restored_frame_scope_valid(
                    project["frame_assignment"], scope[1]):
                raise ValueError("A frame assignment is invalid.")

    def _commit_staged_restore_steps(self, staging, expected):
        staging._sync_journal(close=True)
        staging.point_store.close()
        probe = FlashPointStore(staging.point_path, autoload=False)
        if not probe._load_manifest(recover=False):
            # The point store holds line vertices only; assets live in the journal,
            # so a backup of object points alone legitimately has no manifest.
            if any(len(line["coordinates"]) for project in staging.projects
                   for line in project["lines"]):
                raise ValueError("Missing staged point manifest.")
        elif probe.segments:
            scan = probe._scan_generation_steps(strict=True)
            try:
                for _ in scan: yield None
            finally: scan.close()
        if (probe.max_order != staging.point_store.max_order or
                {key: list(v) for key, v in probe.offsets.items() if v} !=
                {key: list(v) for key, v in staging.point_store.offsets.items() if v}):
            raise ValueError("Staged point-store verification failed.")
        activation = self._activate_staged_restore_steps(staging, expected,
            copy_validation=False, verified_store=probe)
        try:
            for _ in activation: yield None
        finally: activation.close()

    def _commit_staged_restore(self, staging, payload, copy_validation=True, verified_store=None):
        return self._finish_steps(self._activate_staged_restore_steps(staging, payload,
            copy_validation, verified_store))

    def _activate_staged_restore_steps(self, staging, payload, copy_validation=True, verified_store=None):
        """Verify the staged store, publish it with one rename, then install it.

        Until the commit record is renamed into place nothing of the live
        survey has changed, and boot drops the staging. From that rename on
        boot completes the installation (_restore_roll_forward_steps), so a
        reset at any write leaves either the complete original or the
        complete restore: journal, index, checkpoint and point manifest
        switch together. A cancellation or error after the rename finishes
        the installation at once instead of undoing it.
        """
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
            self._drop_restore_staging(staging)
            raise ValueError("The staged survey restore could not be verified.")
        hook = self.before_restore_publish
        if hook is not None:
            # Its error leaves the restore unpublished; the caller drops the staging.
            steps = hook()
            try:
                for item in steps: yield item
            finally:
                close = getattr(steps, "close", None)
                if close: close()
        record = self._write_restore_commit(staging)
        installation = self._restore_roll_forward_steps(record)
        try:
            yield None
            for _ in installation: yield None
        except GeneratorExit:
            installation.close()
            self._finish_published_restore(record)
            self._adopt_restored_state(staging, record, verified_store)
            raise
        except Exception:
            self._finish_published_restore(record)
        self._adopt_restored_state(staging, record, verified_store)

    @staticmethod
    def _present(path):
        """Whether path exists. Only "not found" means absent: any other error
        is raised, since an unreadable file is not a missing one."""
        try:
            os.stat(path); return True
        except OSError as error:
            if error.args and error.args[0] == _ENOENT: return False
            raise

    def _remove_present(self, path):
        """Remove path; an error is raised unless the file is gone afterwards."""
        try: os.remove(path)
        except OSError:
            if self._present(path): raise

    def _restore_published(self):
        return self._present(self.restore_commit_path)

    def _restore_staging_files(self):
        """Every file under the staging prefix except the commit record."""
        directory, prefix = ".", self.path + ".restore."
        if "/" in prefix:
            directory, prefix = prefix.rsplit("/", 1)
        try: names = os.listdir(directory or "/")
        except OSError: return []
        record = self.restore_commit_path.rsplit("/", 1)[-1]
        return [name if directory == "." else directory + "/" + name
                for name in names if name.startswith(prefix) and name != record]

    def _discard_restore_staging(self):
        removed = [0]
        for _ in self._discard_restore_staging_steps(removed): pass
        return removed[0]

    def _discard_restore_staging_steps(self, removed):
        """Remove the staging files one per step; counts them in removed[0]."""
        for path in self._restore_staging_files():
            try: os.remove(path); removed[0] += 1
            except OSError: pass
            yield None

    def _drop_restore_staging(self, staging):
        """Remove an unpublished staging store; a published one is installed instead."""
        try: staging._sync_journal(close=True)
        except Exception: pass
        staging.point_store.close()
        try: published = self._restore_published()
        except OSError: return  # unknown: keep the staging for the next boot
        if not published:
            self._discard_restore_staging()

    def _write_restore_commit(self, staging):
        """Publish the verified staging: write, verify and rename the commit record."""
        for path in (staging.checkpoint_path, staging.index_path):
            os.stat(path)
        record = {"kind": "survey_restore_commit", "schema_version": 1,
                  "point_generation": self.point_store._next_generation(self.point_store.generation),
                  "point_segments": list(staging.point_store.segments),
                  "point_strings": list(staging.point_store.strings),
                  "staged_point_generation": staging.point_store.generation,
                  "journal_segments": [number for number, _path in staging._segment_files()],
                  "old_point_generation": self.point_store.generation,
                  "old_point_segments": list(self.point_store.segments)}
        temporary = self.restore_commit_path + ".tmp"
        with open(temporary, "w") as target: target.write(ujson.dumps(record))
        if self._read_restore_commit(temporary) != record:
            raise ValueError("Restore commit record verification failed.")
        os.rename(temporary, self.restore_commit_path)
        return record

    def _read_restore_commit(self, path=None):
        """Return the commit record, None without one; raise ValueError if unusable."""
        path = path or self.restore_commit_path
        try:
            if not self._present(path): return None
            with open(path, "r") as source: raw = source.read()
            record = ujson.loads(raw)
            numbers = ("point_segments", "journal_segments", "old_point_segments")
            if (record.get("kind") != "survey_restore_commit" or
                    record.get("schema_version") != 1 or
                    any(type(record[key]) is not int for key in ("point_generation",
                        "staged_point_generation", "old_point_generation")) or
                    any(type(number) is not int for key in numbers for number in record[key]) or
                    any(type(value) is not str for value in record["point_strings"])):
                raise ValueError("Unknown restore commit record.")
            return record
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
            raise ValueError("Restore commit record is unreadable: %s" % error)

    def _install_file(self, source, target):
        if not self._present(source):
            if self._present(target): return  # installed by an earlier attempt
            raise OSError(_ENOENT, "Staged restore file missing: %s" % source)
        self._remove_present(target)
        os.rename(source, target)

    def _restore_roll_forward_steps(self, record):
        """Install a published restore; every step may be repeated after a reset.

        Each staged file is moved over its live name only while it still
        exists, live files the restore does not name are removed, and the
        record goes last, so boot can run this again from the start.
        """
        staging = type(self)(self.path + ".restore", autoload=False, point_store_autoload=False)
        live = FlashPointStore(self.point_path, autoload=False)
        generation = record["point_generation"]
        for number in record["point_segments"]:
            self._install_file(staging.point_store._segment_path(
                number, record["staged_point_generation"]), live._segment_path(number, generation))
            yield None
        live.strings = list(record["point_strings"])
        live._activate_manifest(generation, record["point_segments"])
        yield None
        wanted = record["journal_segments"]
        for number in wanted:
            self._install_file(staging._segment_path(number), self._segment_path(number))
            yield None
        for number, path in self._segment_files():
            if number not in wanted:
                # A kept old segment would replay into the restored survey.
                self._remove_present(path)
                yield None
        for target, source in ((self.index_path, staging.index_path),
                               (self.checkpoint_path, staging.checkpoint_path)):
            # A stale .tmp or .previous would be a fallback into the old survey.
            for stale in (target + ".tmp", target + ".previous"):
                self._remove_present(stale)
            self._install_file(source, target)
            yield None
        old = record["old_point_generation"]
        if old and old != generation:
            for number in record["old_point_segments"]:
                try: os.remove(live._segment_path(number, old))
                except OSError: pass
                yield None
        self._discard_restore_staging()
        # Never report success while the record stays: the next boot would
        # repeat the installation and drop everything recorded since.
        self._remove_present(self.restore_commit_path)
        yield None

    def _recover_restore_steps(self):
        """Boot: complete a published restore or drop an unpublished staging.

        Boot only: a second tracker initialized on a path whose restore is
        still staging would delete that staging.
        """
        self.startup_error = None
        try:
            record = self._read_restore_commit()
        except ValueError as error:
            self.startup_error = {"code": "restore_commit_unreadable", "message": "%s. "
                "The record and the staged files are kept; see docs/known-limitations.md, "
                "A survey restore stops every start-up." % error}
            raise
        if record is None:
            removed = [0]
            for _ in self._discard_restore_staging_steps(removed):
                yield None
            if removed[0]:
                log("WARN", "TRACK", "An unfinished survey restore was discarded; "
                    "the previous survey is kept.")
                yield "discarded"
            return
        try:
            for _ in self._restore_roll_forward_steps(record):
                yield None
        except Exception as error:
            self.startup_error = {"code": "restore_install_failed", "message": "A published "
                "survey restore could not be installed (%s); every start-up retries it. See "
                "docs/known-limitations.md, A survey restore stops every start-up." % error}
            raise
        log("WARN", "TRACK", "An interrupted survey restore was completed.")
        yield "completed"

    def _finish_published_restore(self, record):
        # Published restores are never undone. Finish without yielding; if
        # flash refuses, stop accepting changes so the next boot completes it.
        try:
            self._finish_steps(self._restore_roll_forward_steps(record))
        except Exception as error:
            from state import app
            self._loaded = False
            # The worker would otherwise write the old index, checkpoint and
            # queued points over files the restore already installed.
            self._install_pending = True
            self._pending_fixes = []
            self._index_dirty_events = self._checkpoint_dirty_events = 0
            app.stats["tracking_startup"] = {"state": "error",
                "code": "restore_install_pending",
                "message": "A survey restore is published but not installed (%s). "
                           "Restart the device to complete it." % error}
            log("ERROR", "TRACK", "Survey restore published but not installed (%s); "
                "the next boot completes it." % error)
            raise

    def _adopt_restored_state(self, staging, record, verified_store):
        self.projects = staging.projects
        self.profiles = staging.profiles
        self._reset_profile_cache()
        if verified_store is None:
            self.point_store = FlashPointStore(self.point_path)
        else:
            self.point_store = verified_store
            self.point_store.path = self.point_path
            self.point_store.manifest_path = self.point_path + ".segments.json"
            self.point_store.generation = record["point_generation"]
            self.point_store._verified_prefix = None
        self.point_store.reserve_check = self._pointstore_rotation_allowed
        self._pack_projects()
        self._project_activity = staging._project_activity
        self._line_lengths = staging._line_lengths
        self._project_frames = {}
        self._frame_tokens = {}
        self._status_cache = None
        self._reset_map_changes(staging._event_sequence + 1)
        self.active_project_id = staging.active_project_id
        self.active_line_id = staging.active_line_id
        self.auto_distance_m = staging.auto_distance_m
        self._event_sequence = staging._event_sequence
        self._checkpoint_order = staging._checkpoint_order
        self._segment_number = staging._segment_number
        self._index_dirty_events = 0
        self._renew_inventory()
