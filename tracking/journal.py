# SPDX-License-Identifier: AGPL-3.0-only
"""The event journal, index and checkpoint: append, load, replay and compaction."""
import os
import time
import ujson
from pointstore import POINT_WRITE_BATCH
from cfg import log
from tracking.common import (CHECKPOINT_BATCH_EVENTS, CHECKPOINT_IO_SERVICE_MS,
                             LEGACY_SCHEMA_VERSIONS, SCHEMA_VERSION, SEGMENT_HARD_LIMIT_BYTES,
                             SEGMENT_MAX_BYTES)
from tracking.validation import (_legacy_conversion_error, _legacy_coordinate, _tag_legacy,
                                 _upgrade_event, _upgrade_legacy_line, _upgrade_legacy_point,
                                 _upgrade_projects)


class JournalMixin:
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

    @property
    def event_sequence(self):
        """The order of the last append begun; it grows before anything is written."""
        return self._event_sequence

    def _append(self, event, defer_index=False):
        started = time.ticks_ms()
        self._event_sequence += 1
        event["order"] = self._event_sequence
        event["schema_version"] = SCHEMA_VERSION
        limits = ((event.get("measurement") or {}).get("quality") or {}).get("limits")
        profile_id = (self._profile_for_limits(
            limits, event["measurement"].get("profile_id")) if limits else None)
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
            if event.get("frame_scope") is not None:
                # The scope must shrink durably before the point store forgets
                # the vertex: _apply's remove_last syncs the point store itself.
                self._sync_journal(close=False)
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
        for line_id, orders in self.point_store.legacy_skipped.items():
            for order in orders:
                _legacy_conversion_error("line %s vertex %s" % (line_id, order),
                                         "coordinate")
        checkpoint_order = self._load_checkpoint()
        if self._legacy_copy_source:
            source, self._legacy_copy_source = self._legacy_copy_source, None
            for _ in self._keep_legacy_checkpoint_steps(source):
                yield None
        self._event_sequence = max(self._event_sequence, self.point_store.max_order)
        segments = self._journal_segments()
        for number, path in segments:
            replay = self._replay_segment_steps(number, path, checkpoint_order)
            try:
                for _ in replay:
                    yield None
            finally: replay.close()
        refresh = self._refresh_summaries_steps()
        try:
            for _ in refresh:
                yield None
        finally: refresh.close()
        self._finish_load(segments)

    def _journal_segments(self):
        """The journal segments by number; numbers continue after the newest one."""
        if self._checkpoint_segment:
            self._segment_number = max(self._segment_number,
                                       self._checkpoint_segment + 1)
        segments = self._segment_files()
        if segments:
            self._segment_number = max(self._segment_number, segments[-1][0])
        return segments

    def _replay_segment_steps(self, number, path, checkpoint_order):
        """Replay one segment after the checkpoint; quarantine it if a record is malformed."""
        if self._checkpoint_segment and number < self._checkpoint_segment:
            return
        try:
            if os.stat(path)[6] > SEGMENT_HARD_LIMIT_BYTES:
                os.rename(path, path + ".oversize")
                return
        except OSError:
            return
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
                            # The journal keeps the original text; a legacy event
                            # whose feature cannot be converted is not applied.
                            if (event.get("schema_version") != SCHEMA_VERSION and
                                    int(event.get("order", 0)) > checkpoint_order):
                                self._legacy_loaded = True
                            upgraded = _upgrade_event(event)
                            valid_records.append(raw if raw.endswith("\n") else raw + "\n")
                            self._event_sequence = max(self._event_sequence,
                                                       int(event.get("order", 0)))
                            if (upgraded is not None and
                                    int(event.get("order", 0)) > checkpoint_order):
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
            return

    def _refresh_summaries_steps(self):
        """Rebuild the line summaries the checkpoint lacks or the journal outdated."""
        self._repair_active_references()
        self._project_frames = {}
        self._frame_tokens = {}
        outdated = False
        for project in self.projects:
            for line in project.get("lines", []):
                cached = line.get("_summary") or {}
                # A summary without the frame tally predates it and is rebuilt.
                missing = "legacy_vertex_count" not in cached or "frames" not in cached
                if cached.get("vertex_count") != len(line["coordinates"]) or missing:
                    # Rebuilt only for a missing field (same vertex count): the
                    # checkpoint is outdated and is rewritten on the first idle turn,
                    # else every boot repeats the scan. Vertices from the journal are
                    # normal and do not count.
                    if missing and cached.get("vertex_count") == len(line["coordinates"]):
                        outdated = True
                    line.pop("_summary", None)
                    summary = self._iter_line_summary(line)
                    try:
                        for _ in summary:
                            yield None
                    finally: summary.close()
                self._line_lengths[line["id"]] = float(
                    (line.get("_summary") or {}).get("length_m", 0.0))
            # Every summary is current now, so this reads no flash.
            self._project_frame_tally(project)
        if outdated:
            self._checkpoint_dirty_events = max(self._checkpoint_dirty_events,
                                                CHECKPOINT_BATCH_EVENTS)

    def _finish_load(self, segments):
        """Write the index, settle a legacy conversion and start the map deltas over."""
        if segments or self.projects:
            self._write_index()
        if self._legacy_loaded:
            # Persist the conversion once: the storage worker compacts on its
            # first idle turn (it waits while storage_busy, and compaction
            # checks the flash reserve), so later boots read a schema-5
            # checkpoint with cached line summaries instead of converting,
            # rebuilding and logging the same legacy values again.
            self._checkpoint_dirty_events = max(self._checkpoint_dirty_events,
                                                CHECKPOINT_BATCH_EVENTS)
            log("INFO", "TRACK", "Legacy survey data converted; a schema-5 "
                "checkpoint follows on the first idle turn.")
        else:
            # A copy interrupted by a reset is never completed later.
            try: os.remove(self.checkpoint_path + ".v4.tmp")
            except OSError: pass
        # A new boot requires a full map before incremental changes can follow.
        self._reset_map_changes(self._event_sequence)

    def _load_checkpoint(self, readonly=False):
        paths = ((self.checkpoint_path,) if readonly else
                 (self.checkpoint_path, self.checkpoint_path + ".tmp",
                  self.checkpoint_path + ".previous"))
        for path in paths:
            try:
                loaded = self._read_checkpoint(path, readonly)
                if loaded is None:
                    continue
                return self._adopt_checkpoint(path, readonly, *loaded)
            except (OSError, ValueError, TypeError, AttributeError):
                continue
        return 0

    def _read_checkpoint(self, path, readonly):
        """(value, checkpoint_schema, expected_points, legacy), or None to try the next file."""
        checkpoint_schema, expected_points, legacy = None, {}, False
        with open(path, "r") as source:
            first_raw = source.readline()
            value = ujson.loads(first_raw)
            if readonly and (value.get("kind") != "survey_checkpoint_stream" or
                             value.get("schema_version") != SCHEMA_VERSION):
                raise ValueError("Unexpected checkpoint verification format.")
            if value.get("kind") == "survey_checkpoint_stream":
                checkpoint_schema = value.get("schema_version")
                if checkpoint_schema not in LEGACY_SCHEMA_VERSIONS + (SCHEMA_VERSION,):
                    # The stream is relabelled below; an unknown schema
                    # must not pass as current data.
                    return None
                legacy = checkpoint_schema in LEGACY_SCHEMA_VERSIONS
                value = self._read_checkpoint_stream(source, value, legacy, readonly,
                                                     expected_points)
                if value is None:
                    return None
            else:
                value = ujson.loads(first_raw + source.read())
                if (value.get("kind") == "survey_checkpoint" and
                        value.get("schema_version") in LEGACY_SCHEMA_VERSIONS):
                    legacy = True
                    _upgrade_projects(value.get("projects"))
        if (value.get("kind") != "survey_checkpoint" or
                value.get("schema_version") not in LEGACY_SCHEMA_VERSIONS + (
                    SCHEMA_VERSION,)):
            return None
        return value, checkpoint_schema, expected_points, legacy

    def _read_checkpoint_stream(self, source, header, legacy, readonly, expected_points):
        """The records after a stream header as a checkpoint value; None without a footer."""
        projects, project_lookup, line_lookup = [], {}, {}
        self.profiles = dict((profile["id"], profile) for profile in
                             header.get("profiles", []))
        self._reset_profile_cache()
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
                if legacy:
                    _upgrade_legacy_line(line)
                line["coordinates"], line["measurements"] = [], []
                project_lookup[record.get("project_id")]["lines"].append(line)
                line_lookup[line.get("id")] = line
            elif kind == "vertex":
                if readonly:
                    raise ValueError("Current checkpoints must reference the point store.")
                line = line_lookup[record.get("line_id")]
                coordinate = record.get("coordinate")
                measurement = record.get("measurement") or {}
                if legacy:
                    coordinate = _legacy_coordinate(
                        coordinate, "coordinate", measurement)
                    if coordinate is None:
                        _legacy_conversion_error("line %s vertex %s" % (
                            record.get("line_id"),
                            measurement.get("record_order")), "coordinate")
                        continue
                    _tag_legacy(measurement)
                line["coordinates"].append(coordinate)
                measurement = self._register_profile(measurement)
                line["measurements"].append(measurement)
            elif kind == "asset":
                point = record.get("point")
                if legacy and not _upgrade_legacy_point(point):
                    continue
                project_lookup[record.get("project_id")]["points"].append(point)
            elif kind == "footer":
                footer = record
        if not footer:
            return None
        return {"kind": "survey_checkpoint",
                "schema_version": SCHEMA_VERSION,
                "profiles": list(self.profiles.values()),
                "order": header.get("order", 0), "projects": projects,
                "segment": header.get("segment", 0),
                "offset": header.get("offset", 0),
                "active_project_id": footer.get("active_project_id"),
                "active_line_id": footer.get("active_line_id"),
                "auto_distance_m": footer.get("auto_distance_m", 1.0)}

    def _adopt_checkpoint(self, path, readonly, value, checkpoint_schema,
                          expected_points, legacy):
        """Take over a checkpoint read from path; returns its order."""
        self.projects = value.get("projects") or []
        self.profiles = dict((profile["id"], profile) for profile in
                             value.get("profiles", []))
        self._reset_profile_cache()
        self._pack_projects()
        # Schema 4 stream checkpoints already referenced the point store;
        # monolithic checkpoints (schema None here) embed their vertices.
        if checkpoint_schema is not None and checkpoint_schema >= 4:
            deficits = {}
            skipped = self.point_store.legacy_skipped
            for project in self.projects:
                for line in project.get("lines", []):
                    # A legacy vertex the point-store scan skipped
                    # (unconvertible coordinate) is still on flash.
                    if len(line["coordinates"]) + len(skipped.get(
                            line.get("id"), ())) < expected_points.get(
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
        copy_source = path
        if path != self.checkpoint_path:
            try:
                os.rename(path, self.checkpoint_path)
                copy_source = self.checkpoint_path
            except OSError: pass
        if legacy and not readonly:
            self._legacy_loaded = True
            # _load_steps copies it in chunks after this step returns.
            self._legacy_copy_source = copy_source
        return self._checkpoint_order

    def _keep_legacy_checkpoint_steps(self, path):
        """Keep a one-time copy of the schema <= 4 checkpoint before schema 5 replaces it.

        The copy is the way back to the original data if a conversion went
        wrong. It is streamed in 4 KB chunks with a yield after each, so the
        boot loop keeps the scheduler and the watchdog running. When it would
        use the flash reserve, or the write fails, the upgrade goes on
        without the copy. A .v4.tmp left by a reset during the copy means an
        attempt did not finish: it is removed and not retried.
        """
        target_path = self.checkpoint_path + ".v4"
        temporary = target_path + ".tmp"
        try:
            os.stat(target_path)
            return
        except OSError:
            pass
        try:
            os.stat(temporary)
            interrupted = True
        except OSError:
            interrupted = False
        if interrupted:
            try: os.remove(temporary)
            except OSError: pass
            log("ERROR", "TRACK", "The copy of the legacy checkpoint did not finish on "
                "an earlier boot; no copy kept.")
            return
        try:
            size = os.stat(path)[6]
        except OSError:
            return
        if not self.storage_capacity(temporary_bytes=size, force=True)["writable"]:
            log("WARN", "TRACK", "No copy of the legacy checkpoint kept: "
                "it would use the flash reserve.")
            return
        try:
            with open(path, "rb") as source, open(temporary, "wb") as target:
                while True:
                    chunk = source.read(4096)
                    if not chunk:
                        break
                    target.write(chunk)
                    yield None
            os.rename(temporary, target_path)
            self._storage_budget_used += size
        except OSError as error:
            try: os.remove(temporary)
            except OSError: pass
            log("WARN", "TRACK", "No copy of the legacy checkpoint kept: %s" % error)

    def _recover_index(self):
        temporary, previous = self.index_path + ".tmp", self.index_path + ".previous"
        def valid(path):
            try:
                with open(path, "r") as source:
                    value = ujson.loads(source.read())
                # An index of an older schema still names valid segments; the
                # load rewrites it with the current schema.
                return (value.get("kind") == "survey_index" and
                        value.get("schema_version") in LEGACY_SCHEMA_VERSIONS + (
                            SCHEMA_VERSION,))
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

    def compact(self):
        """compact_async() run to completion without the service windows.

        For tools and tests outside the event loop; never from a running task."""
        import uasyncio as asyncio
        asyncio.run(self.compact_async(0))

    async def compact_async(self, service_ms=None):
        """Checkpoint in scheduler-sized phases while preserving sync semantics."""
        import uasyncio as asyncio
        if not self._loaded:
            raise ValueError("Survey storage is not loaded; compaction refused.")
        service_ms = CHECKPOINT_IO_SERVICE_MS if service_ms is None else service_ms
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
            await asyncio.sleep_ms(service_ms)
            phase = self._ticks_us()
            self._write_index()
            self._profile_add("checkpoint_index", phase)
            await asyncio.sleep_ms(service_ms)
            phase = self._ticks_us()
            probe = type(self)(self.path, autoload=False, point_store_autoload=False)
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
            # Once the checkpoint and its point-store high-water mark have been
            # read back, covered journal segments are redundant. Releasing them
            # before the optional store rewrite materially lowers temporary flash
            # pressure while leaving the verified checkpoint and old store intact.
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
