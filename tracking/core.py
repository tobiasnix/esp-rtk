# SPDX-License-Identifier: AGPL-3.0-only
"""The Tracker: construction, start-up, projects and storage capacity, composed of the tracking mixins."""
import os
import time
from pointstore import FlashPointStore, FlashSequence
from cfg import log
from tracking.common import (FLASH_RECHECK_WRITES, MAX_FEATURES, MAX_PROJECTS,
                             MIN_FLASH_RESERVE_PERCENT, OPERATIONAL_FLASH_HEADROOM_BYTES,
                             RESTORE_SERVICE_MS, TRACK_CHECKPOINT_FILE, TRACK_FILE,
                             TRACK_INDEX_FILE, _add_time_fields, _now_seconds, _profile_id)
from tracking.guards import (MeasurementRejected, StorageReserveError, _MaintenanceGuard,
                             _ReaderGuard, _new_inventory_epoch)
from tracking.frames import FrameAssignmentMixin, _effective_frame, _height_fields
from tracking.validation import _SERVICE_PAUSE, _clean_text, _validate_coordinate
from tracking.fixes import (_configured_antenna_height_mm, _gate_current, _ntrip_mount,
                            _reference_frame, _reference_frame_epoch, quality_check)
from tracking.model import ACTIVITY_EVENTS, EVENT_HANDLERS, MAP_CHANGE_BUILDERS, _event_activity
from tracking.journal import JournalMixin
from tracking.archive import ArchiveMixin
from tracking.restore import RestoreMixin
from tracking.summary import SummaryMixin
from tracking.exports import ExportMixin
from tracking.recording import RecordingMixin


class Tracker(RecordingMixin, JournalMixin, FrameAssignmentMixin, ArchiveMixin, RestoreMixin,
              SummaryMixin, ExportMixin):
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
        # A restore stages a complete store under path + ".restore" and is
        # published by renaming this record into place (_write_restore_commit).
        self.restore_commit_path = path + ".restore.commit.json"
        # What the last boot repaired; main.py publishes it in /status stats.
        self.storage_recovery = {"restore": None, "unreferenced_point_files": 0,
                                 "unreferenced_point_bytes": 0}
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
        self._init_runtime_state()
        self.inventory_epoch = _new_inventory_epoch()
        # RAM only: which request created a recent point (#21c); empty after a restart or an inventory exchange.
        self._request_tags = []
        # A function returning the steps a survey restore runs before it publishes (stakeout WP4).
        self.before_restore_publish = None
        self._loaded = False
        if autoload:
            self.initialize()

    def _init_runtime_state(self):
        """The state a new tracker starts with: counters, caches, profiling, guards, queues."""
        self._sequence = 0
        self._event_sequence = 0
        self._checkpoint_order = 0
        self._checkpoint_segment = 0
        self._checkpoint_offset = 0
        self._index_dirty_events = 0
        self._checkpoint_dirty_events = 0
        # Set when a load converted schema <= 4 data (checkpoint or journal);
        # the load then schedules one checkpoint so the next boot reads schema 5.
        self._legacy_loaded = False
        self._legacy_copy_source = None
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
        # Frame tally per project id: {frame_text: count} over its line vertices
        # and object points. An entry that exists is exact; a missing one is
        # rebuilt from the current line summaries on first read (_project_frame_tally);
        # restore and snapshot clear it.
        self._project_frames = {}
        # Fingerprint of each project's frame assignment (_assignment_token):
        # {project_id: [assignment, token]}, valid while the project holds that
        # very assignment object (assignments are replaced, never changed).
        # Deleting or archiving a project drops its entry; load, restore and
        # snapshot empty it.
        self._frame_tokens = {}
        self._pending_fixes = []
        self.storage_busy = False
        # Entered maintenance guards (nested ones included); storage_busy follows it.
        self._maintenance_depth = 0
        # Set when a published restore could not be installed: the survey in
        # memory is the old one, and only the next boot may write the store.
        self._install_pending = False
        # Why the last start-up recovery failed, for /status tracking_startup.
        self.startup_error = None
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

    def __del__(self):
        try: self._sync_journal(close=True)
        except Exception: pass
        try: self.point_store.close()
        except Exception: pass

    def reader(self):
        """async with tracker.reader(): read the survey; refused during maintenance."""
        return _ReaderGuard(self)

    def maintenance(self, reason, progress=None, parent=None, allow_recording=False):
        """async with tracker.maintenance(reason) as job: exclusive storage work."""
        return _MaintenanceGuard(self, reason, progress, parent, allow_recording)

    def is_loaded(self):
        return self._loaded

    def is_install_pending(self):
        return self._install_pending

    def maintenance_active(self):
        return bool(self.storage_busy or self._compacting)

    def pending_count(self):
        return len(self._pending_fixes)

    def has_open_recording(self):
        """An open line (paused included) or queued fixes not yet written."""
        return bool(self.active_line_id or self._pending_fixes)

    def active_line(self):
        return self._line(self._project())

    def operation_snapshot(self):
        return dict(self.operation_progress)

    async def load_without_recovery_async(self):
        """Read point store, checkpoint and journal as stored: no restore
        recovery, no archives, and the tracker stays not loaded."""
        if not self._point_store_loaded:
            await self.point_store.load_async()
            self._point_store_loaded = True
        self._load()

    def _register_profile(self, measurement):
        phase = self._ticks_us()
        quality = measurement.get("quality") or {}
        limits = quality.pop("limits", None)
        profile_id = measurement.get("profile_id")
        if limits:
            profile_id = self._profile_for_limits(limits, profile_id)
            self.profiles[profile_id] = {"id": profile_id, "version": 1,
                                         "limits": limits}
        if profile_id:
            measurement["profile_id"] = profile_id
        self._profile_add("profile_calculation", phase)
        return measurement

    def _reset_profile_cache(self):
        # Whenever the profile table is replaced: the cached ID may name a profile
        # the new table holds under another (legacy) ID or not at all.
        self._last_profile_limits = None
        self._last_profile_id = None

    def _profile_for_limits(self, limits, preferred=None):
        """The profile ID for limits; an existing profile with these limits wins.

        A measurement that already names a profile with exactly these limits
        (a restored backup) keeps that ID. Otherwise the canonical ID is used
        unless the table holds the limits under another ID (a legacy hash-order
        ID), which is reused so one set of limits stays one profile. The table
        holds a handful of entries and is scanned only when the limits change.
        """
        known = self.profiles.get(preferred) if preferred else None
        if known and known.get("limits") == limits:
            return preferred
        if limits == self._last_profile_limits and self._last_profile_id:
            return self._last_profile_id
        profile_id = _profile_id(limits)
        if profile_id not in self.profiles:
            for other_id, profile in self.profiles.items():
                if profile.get("limits") == limits:
                    profile_id = other_id
                    break
        self._last_profile_limits = dict(limits)
        self._last_profile_id = profile_id
        return profile_id

    def _map_profiles(self, profile_ids):
        """The stored limits of each referenced profile, keyed by profile id."""
        table = {}
        for profile_id in profile_ids:
            profile = self.profiles.get(profile_id)
            if profile and isinstance(profile.get("limits"), dict):
                table[profile_id] = dict(profile["limits"])
        return table

    def _expand_measurement(self, measurement):
        result = dict(measurement)
        quality = dict(result.get("quality") or {})
        profile = self.profiles.get(result.get("profile_id"))
        if profile:
            quality["limits"] = dict(profile["limits"])
        result["quality"] = quality
        return result

    def _export_measurement(self, measurement, coordinate=None, assignment=None):
        """The expanded measurement for an export: an unknown frame reads "unknown".

        The derived time fields (recorded_at_utc, clock_unsynced) are added.

        Storage keeps None for an unknown reference frame (the NDJSON backup
        emits that stored value); GeoJSON and CSV print text instead. With the
        stored ``coordinate`` the derived height fields are added too; its third
        value is the MSL height of the antenna phase centre. ``assignment`` is
        the project's frame assignment for a measurement in its scope; the
        frame, epoch and reference_frame_source follow _effective_frame.
        """
        result = self._expand_measurement(measurement)
        # Falsy, not just None: an empty string from a hand-edited backup reads
        # the same as a missing frame in every export (and in the tally).
        frame, epoch, source = _effective_frame(measurement, assignment)
        result["reference_frame"], result["reference_frame_source"] = frame, source
        if source == "assigned" or "reference_frame_epoch" in result:
            # An assigned frame always comes with its epoch, even an empty one.
            result["reference_frame_epoch"] = epoch
        if coordinate is not None:
            result.update(_height_fields(result, coordinate[2]))
        return _add_time_fields(result)

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
            outcome = self._finish_steps(self._recover_restore_steps())
            self._reopen_point_store_after(outcome)
            if not self._point_store_loaded:
                self.point_store.load()
                self._point_store_loaded = True
            self._load()
            self._remove_unreferenced_points(outcome)
            self._load_archives()
            self._loaded = True
        return self

    def _reopen_point_store_after(self, outcome):
        # A store autoloaded in __init__ predates a restore completed since.
        if outcome == "completed" and self._point_store_loaded:
            self.point_store.close()
            self.point_store = FlashPointStore(self.point_path, autoload=False)
            self.point_store.reserve_check = self._pointstore_rotation_allowed
            self._point_store_loaded = False

    def _remove_unreferenced_points(self, outcome):
        for _ in self._remove_unreferenced_points_steps(outcome): pass

    def _remove_unreferenced_points_steps(self, outcome):
        """Release segment files no manifest names (boot only, see remove_unreferenced)."""
        removed = [0, 0]
        for _ in self.point_store.remove_unreferenced_steps(removed):
            yield None
        files, size = removed
        quarantine = self.point_store.quarantine()
        self.storage_recovery = {"restore": outcome, "unreferenced_point_files": files,
                                 "unreferenced_point_bytes": size,
                                 "quarantined_point_files": quarantine and quarantine["files"],
                                 "quarantined_point_bytes": quarantine and quarantine["bytes"]}
        if files:
            log("INFO", "TRACK", "Removed %d unreferenced point-store files (%d bytes)." %
                (files, size))
        if quarantine:
            log("WARN", "TRACK", "Point store was recovered from a fallback manifest; "
                "%d unnamed point-store files (%d bytes) are kept until %s is removed." %
                (quarantine["files"], quarantine["bytes"], self.point_store.quarantine_path))

    async def initialize_async(self, reconcile=None):
        """Recover before accepting mutations, while radio and HTTP keep running.

        reconcile, if given, is an async callable awaited with the tracker
        after the recovery and the archives and before the survey counts as
        loaded (main: the stakeout store's reconcile_deletions); its errors
        are logged and never keep the survey from loading. It does not run
        when the recovery fails."""
        import uasyncio as asyncio
        from state import app
        if self._loaded:
            return self
        outcome = None
        recovery = self._recover_restore_steps()
        try:
            # Each step is one rename or remove; service the loop in between.
            for item in recovery:
                if item is not None: outcome = item
                app.beat("tracking")
                await asyncio.sleep_ms(1)
        finally: recovery.close()
        self._reopen_point_store_after(outcome)
        if not self._point_store_loaded:
            await self.point_store.load_async()
            self._point_store_loaded = True
        self._replaying = True
        try:
            steps = self._load_steps()
            try:
                for index, _ in enumerate(steps):
                    if index % 16 == 0:
                        app.beat("tracking")
                        await asyncio.sleep_ms(1)
            finally: steps.close()
            # One remove per step: a sweep after an aborted rewrite can face
            # dozens of segment files.
            for _ in self._remove_unreferenced_points_steps(outcome):
                app.beat("tracking")
                await asyncio.sleep_ms(1)
            self._load_archives()
        finally:
            self._replaying = False
        if reconcile is not None:
            try:
                await reconcile(self)
            except Exception as error:
                log("WARN", "TRACK", "Start-up reconciliation failed: %s" % error)
        self._loaded = True
        return self

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
        """Apply one journal event: live, on replay and in a restore's staging store."""
        kind = event.get("event")
        # Only a text kind indexes the tables; any other value is an unknown kind.
        known = kind if isinstance(kind, str) else None
        handler = EVENT_HANDLERS.get(known)
        if handler is not None and handler(self, event) is False:
            return
        project_id, activity = _event_activity(kind, event)
        if project_id and activity is not None and kind in ACTIVITY_EVENTS:
            self._project_activity[project_id] = int(activity)
        if self._replaying:
            return
        builder = MAP_CHANGE_BUILDERS.get(known)
        change = builder(self, event) if builder is not None else None
        if change is not None:
            self._record_map_change(change, event)
        self._status_cache = None

    def _record_map_change(self, change, event):
        """Publish one live map change; past 128 the oldest goes and raises the floor."""
        change["revision"] = int(event.get("order", self._event_sequence))
        self._map_changes.append(change)
        if len(self._map_changes) > 128:
            removed = self._map_changes.pop(0)
            self._map_change_floor = removed["revision"]

    def _reset_map_changes(self, floor):
        """Drop the live map changes; a client behind floor reloads the full map."""
        self._map_changes = []
        self._map_change_floor = floor

    def _id(self, prefix):
        self._sequence = (self._sequence + 1) & 0xffff
        return "%s-%08x-%04x" % (prefix, int(time.time()) & 0xffffffff,
                                  self._sequence)

    @staticmethod
    def _reusable_verdict(fix):
        """A copy of the verdict enqueue_fix attached to fix, None when void.

        The verdict holds while the gate limits it was judged under are still
        the current ones (same _gate_current() generation). A copy, because
        _register_profile pops the limits of the stored measurement and the
        verdict is shared with the gate cache and the fix listener."""
        verdict = fix.get("_verdict")
        if not verdict or verdict[0] != _gate_current()[0]:
            return None
        quality = verdict[1]
        return dict(quality, reasons=list(quality["reasons"]),
                    limits=dict(quality["limits"]))

    def _measurement(self, fix, allow_rejected=False):
        phase = self._ticks_us()
        quality = self._reusable_verdict(fix)
        if quality is None:
            quality = quality_check(fix)
        self._profile_add("quality_check", phase)
        if not quality["accepted"] and not allow_rejected:
            raise MeasurementRejected("Measurement quality rejected: %s." %
                                      ", ".join(quality["reasons"]), quality["reasons"])
        geoid_sep_mm, alt_mm = fix.get("geoid_sep_mm"), fix.get("alt_mm")
        if type(geoid_sep_mm) is not int:
            geoid_sep_mm = None
        # A queued fix carries the stamps of its queue time (private key, never stored).
        stamps = fix.get("_stamps")
        return {
            "recorded_at": _now_seconds(), "fix_quality": int(fix.get("qual", 0)),
            "fix_status": fix.get("fix_status_text", "UNKNOWN"),
            "satellites": int(fix.get("sats", 0)), "hdop": fix.get("hdop"),
            "correction_age_sec": fix.get("correction_age_sec"),
            "station_id": fix.get("station_id"),
            "accuracy_observation": fix.get("accuracy_observation"),
            "receiver_accuracy": fix.get("receiver_accuracy"),
            "quality": quality,
            "precision": "e8",
            "geoid_sep_mm": geoid_sep_mm,
            "alt_ellipsoid_mm": (alt_mm + geoid_sep_mm if geoid_sep_mm is not None
                                 and type(alt_mm) is int else None),
            "reference_frame": _reference_frame(fix),
            "reference_frame_epoch": _reference_frame_epoch(fix),
            "ntrip_mount": stamps["ntrip_mount"] if stamps else _ntrip_mount(),
            "antenna_height_mm": (stamps["antenna_height_mm"] if stamps
                                  else _configured_antenna_height_mm()),
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
                total = free = block = 0
            self._profile_add("statvfs", phase)
            self._storage_sample = (total, free, block)
            self._storage_budget_used = 0
        else:
            total, free, _ = self._storage_sample
        free = max(0, free - self._storage_budget_used)
        reserve = (total * MIN_FLASH_RESERVE_PERCENT + 99) // 100 if total else 0
        temporary = (max(0, int(temporary_bytes or 0)) +
                     OPERATIONAL_FLASH_HEADROOM_BYTES)
        return {"free_flash_bytes": free,
                "required_flash_reserve_bytes": reserve,
                "estimated_temporary_bytes": temporary,
                "writable": not total or free - temporary >= reserve}

    def charge_storage(self, written):
        """Count a file of written bytes against the cached flash sample.

        No statvfs: whole blocks of the sample's block size plus one for the
        metadata, added to the budget until the next forced sample resets it.
        Without a sample the next check measures anyway.
        """
        sample = self._storage_sample
        if sample and sample[2]:
            block = sample[2]
            self._storage_budget_used += (-(-written // block) + 1) * block

    def invalidate_storage_sample(self):
        """Make the next storage_capacity() measure the flash again."""
        self._storage_sample = None

    def _require_write_capacity(self, automatic=False, force=False):
        force = force or self._writes_until_flash_check <= 0
        storage = self.storage_capacity(force=force)
        self._writes_until_flash_check = (FLASH_RECHECK_WRITES if force else
                                          self._writes_until_flash_check - 1)
        if not storage["writable"]:
            if automatic:
                self._pause_for_capacity("storage_reserve")
            raise StorageReserveError("Flash reserve would fall below 20 percent.")

    def _coordinate(self, fix):
        # The stored coordinate is built from the fix's exact fields only, never
        # from the float lat/lon/alt, which are display values (32-bit on the
        # device). Never manufacture an altitude for a route sample either: a
        # zero inserted for a missing GGA field would look like a valid
        # measurement in exports and would violate the lossless evidence model.
        # Validating here makes add_vertex, add_asset, on_fix and enqueue_fix
        # reject floats, bools and out-of-range values alike.
        if (not isinstance(fix, dict) or fix.get("lon_e8") is None or
                fix.get("lat_e8") is None or fix.get("alt_mm") is None):
            raise ValueError("An exact position (lat_e8, lon_e8, alt_mm) is required.")
        coordinate = [fix["lon_e8"], fix["lat_e8"], fix["alt_mm"]]
        _validate_coordinate(coordinate)
        return coordinate

    def feature_limit(self):
        return MAX_FEATURES if self.max_features is None else self.max_features

    def create_project(self, name, description=""):
        if len(self.projects) >= MAX_PROJECTS:
            raise ValueError("Project limit reached.")
        project = {"id": self._id("project"), "name": _clean_text(name),
                   "description": _clean_text(description, 240),
                   "created_at": _now_seconds(), "lines": [], "points": []}
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
                      "name": name, "at": _now_seconds()})

    def check_deletable(self, project_id):
        """Raise ValueError unless delete_project(project_id) may run."""
        if self.active_project_id == project_id and self.active_line_id:
            raise ValueError("Finish the active line before deleting its project.")
        if not self._project(project_id):
            raise ValueError("Project not found.")

    def delete_project(self, project_id):
        self.check_deletable(project_id)
        self._append({"event": "project_deleted", "project_id": project_id})

    @staticmethod
    def _finish_steps(steps):
        result = None
        try:
            for item in steps:
                if item is not None and item is not _SERVICE_PAUSE: result = item
        finally: steps.close()
        return result

    @staticmethod
    async def _finish_steps_async(steps):
        import uasyncio as asyncio
        result = None
        try:
            for item in steps:
                if item is _SERVICE_PAUSE:
                    await asyncio.sleep_ms(RESTORE_SERVICE_MS); continue
                if item is not None: result = item
                await asyncio.sleep_ms(1)
        finally: steps.close()
        return result
