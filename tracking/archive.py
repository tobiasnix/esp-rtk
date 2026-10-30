# SPDX-License-Identifier: AGPL-3.0-only
"""Project archives: write, verify, reactivate and stream archived projects."""
import os
import ujson
from pointstore import _compact
try:
    import hashlib
except ImportError:  # pragma: no cover
    import uhashlib as hashlib
from tracking.common import (ARCHIVE_VERTEX_BYTES, LEGACY_SCHEMA_VERSIONS, SCHEMA_VERSION,
                             _expanded_archive_record, _now_seconds, _strip_derived_fields)
from tracking.guards import StorageReserveError
from tracking.validation import (_SERVICE_PAUSE, _legacy_conversion_error, _legacy_coordinate,
                                 _tag_legacy, _upgrade_legacy_line, _upgrade_legacy_point,
                                 _validate_coordinate, _validate_legacy_coordinate)


class ArchiveMixin:
    def _load_archives(self):
        try:
            with open(self.archive_index_path, "r") as source:
                value = ujson.loads(source.read())
            if value.get("kind") == "survey_archive_index":
                self.archives = value.get("archives") or []
        except (OSError, ValueError, TypeError, AttributeError):
            self.archives = []

    def _write_archive_index(self):
        try: os.mkdir(self.archive_dir)
        except OSError: pass
        temporary = self.archive_index_path + ".tmp"
        payload = {"kind": "survey_archive_index", "schema_version": SCHEMA_VERSION,
                   "archives": self.archives}
        with open(temporary, "w") as target: target.write(ujson.dumps(payload))
        with open(temporary, "r") as source:
            if ujson.loads(source.read()).get("kind") != "survey_archive_index":
                raise ValueError("Archive index verification failed.")
        try: os.remove(self.archive_index_path)
        except OSError: pass
        os.rename(temporary, self.archive_index_path)

    def archive_project(self, project_id):
        """Archive, then compact(); for tools and tests, never from a running task."""
        result = self._finish_steps(self._archive_project_steps(project_id))
        self.compact()
        return result

    async def archive_project_async(self, project_id, extra=None):
        """extra: {"stakeout_data": bool} for the archive metadata; other keys are ignored."""
        result = await self._finish_steps_async(self._archive_project_steps(project_id, extra))
        await self.compact_async()
        return result

    def _archive_project_steps(self, project_id, extra=None):
        """Write, parse and hash one restorable project before removing it."""
        project = self._project(project_id)
        if not project:
            raise ValueError("Project not found.")
        if self.active_project_id == project_id and self.active_line_id:
            raise ValueError("Finish the active line before archiving its project.")
        archive_id = self._id("archive")
        try: os.mkdir(self.archive_dir)
        except OSError: pass
        final_path = self.archive_dir + "/" + archive_id + ".ndjson"
        temporary = final_path + ".tmp"
        estimated = sum(ARCHIVE_VERTEX_BYTES * len(line["measurements"])
                        for line in project["lines"]) + 32768
        storage = self.storage_capacity((estimated * 5) // 4, force=True)
        if not storage["writable"]:
            raise StorageReserveError("Not enough flash to archive while preserving the reserve.")
        try:
            with open(temporary, "w") as target:
                chunks = self.iter_compact_archive(project_id)
                try:
                    for index, raw in enumerate(chunks):
                        target.write(raw)
                        if index % 4 == 3: yield None
                finally: chunks.close()
            verification = self._verify_archive_path_steps(temporary)
            try:
                for verified in verification:
                    if verified is None: yield None
            finally: verification.close()
            if not verified["valid"]:
                raise ValueError("The completed archive could not be verified.")
            os.rename(temporary, final_path)
            yield None
            metadata = {"id": archive_id, "project_id": project["id"],
                        "name": project["name"], "created_at": project.get("created_at"),
                        "archived_at": _now_seconds(),
                        "point_count": len(project["points"]) + sum(
                            len(line["coordinates"]) for line in project["lines"]),
                        "line_count": len(project["lines"]),
                        "size_bytes": os.stat(final_path)[6], "schema_version": SCHEMA_VERSION,
                        "sha256": verified["sha256"]}
            flag = (extra or {}).get("stakeout_data")
            if type(flag) is bool: metadata["stakeout_data"] = flag
            self.archives.append(metadata)
            self._write_archive_index()
            yield None
            self._append({"event": "project_deleted", "project_id": project_id,
                          "at": _now_seconds(), "archived_as": archive_id})
            yield dict(metadata)
        finally:
            try: os.remove(temporary)
            except OSError: pass

    def verify_archive_path(self, path):
        return self._finish_steps(self._verify_archive_path_steps(path))

    def _verify_archive_path_steps(self, path):
        """Check an archive's structure and digest; schema 2-5 archives are accepted.

        Coordinates are checked by the rule of the archive's own schema. For a
        legacy archive that is the rule older firmware applied when it wrote
        the archive (vertex and asset coordinate); the derived
        projected_coordinate was never checked then and is converted value by
        value on reactivation instead.
        """
        digest = hashlib.sha256(); records = points = 0
        projects, lines, header, footer = set(), set(), False, False
        check = _validate_coordinate
        try:
            with open(path, "r") as source:
                for index, raw in enumerate(source):
                    original = ujson.loads(raw)
                    value = _expanded_archive_record(original)
                    if isinstance(value,dict) and value.get("record") == "integrity":
                        expected = value
                        break
                    digest.update(raw.encode("utf-8")); records += 1
                    kind=value.get("record")
                    if kind=="header":
                        schema=value.get("schema_version")
                        header=(index==0 and value.get("kind")=="survey_backup" and
                                schema in LEGACY_SCHEMA_VERSIONS+(SCHEMA_VERSION,))
                        if header and schema in LEGACY_SCHEMA_VERSIONS:
                            check=_validate_legacy_coordinate
                    elif kind=="project": projects.add((value.get("project")or{}).get("id"))
                    elif kind=="line":
                        if value.get("project_id") not in projects: raise ValueError("line order")
                        lines.add((value.get("line")or{}).get("id"))
                    elif kind=="vertex":
                        if value.get("line_id") not in lines: raise ValueError("vertex order")
                        check(value.get("coordinate"))
                        if not isinstance(value.get("measurement"),dict):raise ValueError("measurement")
                        points+=1
                    elif kind=="asset":
                        if value.get("project_id") not in projects:raise ValueError("asset order")
                        point=value.get("point")or{};check(point.get("coordinate"));points+=1
                    elif kind=="footer":footer=True
                    else:raise ValueError("record")
                    if index % 4 == 3: yield None
                else:
                    yield {"valid": False, "error": "integrity_record_missing"}; return
                if source.read().strip():
                    yield {"valid": False, "error": "trailing_records"}; return
            checksum = self._hex_digest(digest)
            if (not header or not footer or expected.get("algorithm") != "sha256" or
                    expected.get("content_sha256") != checksum or
                    expected.get("records") != records):
                yield {"valid": False, "error": "checksum_mismatch"}; return
            yield {"valid": True, "sha256": checksum,"point_count":points}
        except (OSError, ValueError, TypeError, AttributeError):
            yield {"valid": False, "error": "archive_invalid"}

    def verify_archive(self, archive_id):
        return self._finish_steps(self._verify_archive_steps(archive_id))

    def reactivate_archive(self, archive_id):
        result = self._finish_steps(self._reactivate_archive_steps(archive_id))
        return self._project(result["id"])

    async def verify_archive_async(self, archive_id):
        return await self._finish_steps_async(self._verify_archive_steps(archive_id))

    async def reactivate_archive_async(self, archive_id):
        """Returns {"id": <project id>, "legacy_values_skipped": <count>}."""
        return await self._finish_steps_async(self._reactivate_archive_steps(archive_id))

    def _archive_metadata(self, archive_id):
        metadata = next((item for item in self.archives if item["id"] == archive_id), None)
        if not metadata: raise ValueError("Archive not found.")
        return metadata

    def _verify_archive_steps(self, archive_id):
        """Check an indexed archive's file and index digest; the last step is the result."""
        metadata = self._archive_metadata(archive_id)
        verification = self._verify_archive_path_steps(
            self.archive_dir + "/" + archive_id + ".ndjson")
        result = None
        try:
            for item in verification:
                if item is None: yield None
                else: result = item
        finally: verification.close()
        if result.get("sha256") != metadata.get("sha256"):
            result = {"valid": False, "error": "index_checksum_mismatch"}
        yield result

    def _reactivate_archive_steps(self, archive_id):
        """Verify an archive, then restore it beside the survey; the last step is the summary."""
        metadata = self._archive_metadata(archive_id)
        if self._project(metadata.get("project_id")):
            raise ValueError("The archived project is already active.")
        if self.feature_count() + int(metadata.get("point_count", 0)) > self.feature_limit():
            raise ValueError("Reactivation would exceed the active point limit.")
        verification, verified = self._verify_archive_steps(archive_id), None
        try:
            for item in verification:
                if item is None: yield None
                else: verified = item
        finally: verification.close()
        if not verified.get("valid"):
            raise ValueError("Archive verification failed.")
        path = self.archive_dir + "/" + archive_id + ".ndjson"
        restore = self._restore_ndjson_steps(
            self._merged_archive_stream(path, metadata["project_id"]))
        restored = None
        try:
            for item in restore:
                if item is None or item is _SERVICE_PAUSE: yield item
                else: restored = item
        finally: restore.close()
        yield {"id": metadata["project_id"],
               "legacy_values_skipped": restored["legacy_values_skipped"]}

    def iter_compact_archive(self, project_id):
        """Internal text archive with compact measurements and full integrity."""
        digest, records = hashlib.sha256(), 0
        chunks = self.iter_backup_ndjson(project_id)
        try:
            for raw in chunks:
                value = ujson.loads(raw)
                if isinstance(value,dict) and value.get("record") == "integrity": continue
                if isinstance(value,dict) and value.get("record") == "header":
                    value["encoding"] = "compact-measurements-v1"
                elif isinstance(value,dict) and value.get("record") == "vertex":
                    measurement=_strip_derived_fields(dict(value["measurement"]))
                    quality=dict(measurement.get("quality") or {});quality.pop("limits",None)
                    measurement["quality"]=quality
                    value = ["v",value.get("project_id"),value["line_id"],
                             value["coordinate"],_compact(measurement)]
                elif isinstance(value,dict) and value.get("record") == "asset":
                    point=dict(value["point"]);measurement=_strip_derived_fields(dict(point.get("measurement")or{}))
                    quality=dict(measurement.get("quality")or{});quality.pop("limits",None)
                    measurement["quality"]=quality;point["measurement"]=_compact(measurement)
                    value=["a",value.get("project_id"),point]
                encoded=ujson.dumps(value)+"\n";digest.update(encoded.encode("utf-8"));records+=1
                yield encoded
        finally:
            chunks.close()
        yield ujson.dumps({"record":"integrity","algorithm":"sha256",
            "content_sha256":self._hex_digest(digest),"records":records})+"\n"

    def _merged_archive_stream(self,path,active_project_id):
        """Current store plus one archived project as a single schema-5 NDJSON backup.

        Rows of a schema <= 4 archive are converted value by value (as on load)
        and emitted as expanded vertex and asset records, so the schema-5
        header never labels float coordinates.
        """
        with open(path,"r") as source: archive_header=ujson.loads(source.readline())
        legacy=archive_header.get("schema_version") in LEGACY_SCHEMA_VERSIONS
        profiles=dict((p["id"],p) for p in self.profiles.values())
        profiles.update((p["id"],p) for p in archive_header.get("profiles",[]))
        digest,records=hashlib.sha256(),0
        def emit(value):
            nonlocal records
            raw=ujson.dumps(value)+"\n";digest.update(raw.encode("utf-8"));records+=1;return raw
        yield emit({"kind":"survey_backup","schema_version":SCHEMA_VERSION,
                    "record":"header","profiles":list(profiles.values())})
        chunks = self.iter_backup_ndjson()
        try:
            for raw in chunks:
                value=ujson.loads(raw)
                if value.get("record") not in ("header","footer","integrity"): yield emit(value)
        finally:
            chunks.close()
        with open(path,"r") as source:
            source.readline()
            for raw in source:
                value=ujson.loads(raw)
                if isinstance(value,dict) and value.get("record") in ("footer","integrity"):continue
                if legacy:
                    value=self._upgrade_archive_record(value)
                    if value is None: continue
                yield emit(value)
        yield emit({"record":"footer","active_project_id":active_project_id,
                    "active_line_id":None,"auto_distance_m":self.auto_distance_m})
        yield ujson.dumps({"record":"integrity","algorithm":"sha256",
            "content_sha256":self._hex_digest(digest),"records":records})+"\n"

    @staticmethod
    def _upgrade_archive_record(value):
        """Convert one row of a schema <= 4 archive; None when the row is left out."""
        value=_expanded_archive_record(value)
        if not isinstance(value,dict): return value
        kind=value.get("record")
        if kind=="vertex":
            measurement=value.get("measurement")
            coordinate=_legacy_coordinate(value.get("coordinate"),"coordinate",measurement)
            if coordinate is None:
                _legacy_conversion_error("line %s vertex %s"%(value.get("line_id"),
                    measurement.get("record_order") if isinstance(measurement,dict) else None),
                    "coordinate")
                return None
            value["coordinate"]=coordinate; _tag_legacy(measurement)
        elif kind=="asset":
            if not _upgrade_legacy_point(value.get("point")): return None
        elif kind=="line":
            _upgrade_legacy_line(value.get("line"))
        return value
