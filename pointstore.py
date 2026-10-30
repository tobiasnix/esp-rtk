# SPDX-License-Identifier: AGPL-3.0-only
"""Generation-based segmented flash store with bounded RAM expansion."""
import os
import time
import ujson
import geo
try:
    import hashlib
except ImportError:  # pragma: no cover
    import uhashlib as hashlib
try:
    from array import array
except ImportError:  # pragma: no cover
    array = None

# Manifest schema 4 introduced record marker 3 (exact [lon_e8, lat_e8, alt_mm]
# integers). Marker 2 records hold float degrees/metres written by schema <= 3
# firmware; they stay readable and decode tagged as "legacy_float32".
POINT_STORE_SCHEMA = 4
_LEGACY_MARKER, _EXACT_MARKER = 2, 3
_INTERNED_KEYS = ("profile_id", "station_id", "reference_frame",
    "reference_frame_epoch", "ntrip_mount")
POINT_CACHE_SIZE = 32
POINT_WRITE_BATCH = 16
POINT_SEGMENT_BYTES = 131072
# Isolated hardware: eight records take 21.5 ms median, while other ready
# services add further time between GNSS turns. Keep each decode turn smaller.
SCAN_BATCH_RECORDS = 4
SCAN_HASH_BYTES = 4096
_OFFSET_BITS = 20
_OFFSET_MASK = (1 << _OFFSET_BITS) - 1
_ENOENT = 2  # errno of a missing file, on MicroPython and POSIX hosts
_KEYS = ("recorded_at","fix_quality","fix_status","satellites","hdop",
    "correction_age_sec","station_id","source","accuracy_observation",
    "receiver_accuracy","quality","profile_id","record_order","samples",
    "rtk_fixed_samples","horizontal_mean_deviation_m","horizontal_max_deviation_m",
    "vertical_mean_deviation_m","vertical_max_deviation_m","expected_samples",
    "horizontal_sigma_mean_m","horizontal_sigma_max_m","vertical_sigma_mean_m",
    "vertical_sigma_max_m","horizontal_sigma_m","vertical_sigma_m",
    "semi_major_sigma_m","semi_minor_sigma_m","altitude_sigma_m","accepted",
    "reasons","duration_sec","source_samples","gst_complete",
    "accepted_fix_samples","precision","geoid_sep_mm","alt_ellipsoid_mm",
    "reference_frame","reference_frame_epoch","ntrip_mount","antenna_height_mm")
_KEY_TO_CODE = dict((key,index) for index,key in enumerate(_KEYS))
_STRINGS = ("automatic", "manual", "start", "RTK_FIXED", "RTK_FLOAT",
    "GPS", "DGPS", "NMEA_GST", "accepted", "rejected", "e8", "legacy_float32",
    "WGS84/ITRF")
_STRING_TO_CODE = dict((value,index) for index,value in enumerate(_STRINGS))

def _compact(value):
    if isinstance(value,dict):
        out=[-1]
        for key,item in value.items():
            out.extend((_KEY_TO_CODE.get(key,key),_compact(item)))
        return out
    if isinstance(value,list):return [_compact(item) for item in value]
    if isinstance(value,str) and value in _STRING_TO_CODE:
        # A dict marker cannot collide with an application dict: normal dicts
        # have already been converted to the tagged list representation above.
        return {"$":_STRING_TO_CODE[value]}
    return value

def _expand(value):
    if isinstance(value,dict) and len(value)==1 and "$" in value:
        code=value["$"]
        if isinstance(code,int) and 0<=code<len(_STRINGS):return _STRINGS[code]
    if isinstance(value,list) and value and value[0]==-1:
        out={}
        for index in range(1,len(value),2):
            key=value[index]
            if isinstance(key,int) and 0<=key<len(_KEYS):key=_KEYS[key]
            out[key]=_expand(value[index+1])
        return out
    if isinstance(value,list):return [_expand(item) for item in value]
    return value

class LegacyConversionError(ValueError):
    """A structurally valid legacy vertex whose coordinate cannot be converted.

    Scans skip only that vertex (see _scan_file_steps); any other ValueError
    from _decode means damaged bytes. order and line_id name the record.
    """
    def __init__(self, order, line_id):
        super().__init__("Legacy point-store coordinate is invalid.")
        self.order, self.line_id = order, line_id

def _legacy_vertex(order, line_id, coordinate, measurement):
    """Decode a float-degree vertex of schema <= 3 into exact integers.

    ``measurement`` is a fresh dict (or None when not requested); it gains the
    "legacy_float32" tag unless it carries a precision already. Raises
    LegacyConversionError when only the coordinate is invalid.
    """
    order = int(order)
    if not isinstance(line_id, str) or not line_id:
        raise ValueError("Invalid point-store line.")
    try:
        coordinate = geo.legacy_coordinate_to_e8(coordinate)
    except ValueError:
        raise LegacyConversionError(order, line_id)
    if measurement is not None and "precision" not in measurement:
        measurement["precision"] = "legacy_float32"
    return {"record":"vertex","order":order,"line_id":line_id,
            "coordinate":coordinate,"measurement":measurement}

def _exact(coordinate):
    """Return an exact [lon_e8, lat_e8, alt_mm] triple or raise ValueError."""
    if (not isinstance(coordinate,(list,tuple)) or len(coordinate)!=3 or
            type(coordinate[0]) is not int or type(coordinate[1]) is not int or
            type(coordinate[2]) is not int):
        raise ValueError("Point-store coordinates must be exact integers.")
    return coordinate

def _offsets(): return array("I") if array else []
def _copy_offsets(values): return array("I", values) if array else list(values)

def _scan_profile():
    return {key: 0 for key in ("scan_batches", "scan_work_us_sum",
        "scan_batch_last_us", "scan_batch_max_us", "scan_postlude_us",
        "scan_wait_us_sum", "scan_wait_last_us", "scan_wait_max_us",
        "scan_manifest_us", "scan_decoded_records", "scan_hash_bytes",
        "scan_verified_sealed_segments")}
def _ticks_us():
    try:return time.ticks_us()
    except AttributeError:return int(time.time()*1000000)
def _elapsed(started):
    try:return max(0,time.ticks_diff(_ticks_us(),started))
    except (AttributeError,TypeError):return max(0,_ticks_us()-started)

class FlashSequence:
    def __init__(self, store, line_id, field):
        self.store, self.line_id, self.field = store, line_id, field
    def __len__(self): return len(self.store.offsets.get(self.line_id, ()))
    def __bool__(self): return bool(len(self))
    def __iter__(self):
        for index in range(len(self)): yield self[index]
    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[item] for item in range(*index.indices(len(self)))]
        if index < 0: index += len(self)
        values = self.store.offsets.get(self.line_id, ())
        if index < 0 or index >= len(values): raise IndexError(index)
        return self.store.read(values[index])[self.field]

class FlashPointStore:
    """Compact JSON-array segments selected by one atomic manifest."""
    def __init__(self, path, cache_size=POINT_CACHE_SIZE, reserve_check=None, autoload=True):
        self.path, self.manifest_path = path, path + ".segments.json"
        self.cache_size = max(1, int(cache_size))
        self.reserve_check = reserve_check
        self.offsets, self.max_order, self.valid_bytes = {}, 0, 0
        self.segment_lines = {}
        # Orders of legacy vertices per line that a scan skipped because their
        # coordinate cannot be converted (LegacyConversionError). The bytes
        # stay on flash; the tracker logs and counts them.
        self.legacy_skipped = {}
        self.generation, self.segments = 0, []
        # Schema of the manifest on flash; a lower one is relabelled before
        # the first marker-3 write so older firmware never scans those bytes.
        self._manifest_schema = POINT_STORE_SCHEMA
        self.strings, self._string_codes, self._strings_dirty = [], {}, False
        self._cache, self._cache_order = {}, []
        self._writer, self._pending_writes = None, 0
        self._writer_segment, self._writer_bytes = 0, 0
        # Sequential readers add read (open/seek/read of blocks) and decode (loads + _decode).
        self.profile_us={"encode":0,"ensure_writer":0,"write":0,"read":0,"decode":0}
        # RAM-only evidence from fully decoded flash, never from pending writes.
        # Its index is the state BEFORE mutable-tail remove events are applied.
        self._verified_prefix = None
        self._verification_expected = None
        self._verification_complete = False
        self.scan_profile = _scan_profile()
        # True only after a recovering load read the main manifest itself and
        # scanned every segment it names; remove_unreferenced requires it.
        self._loaded_from_manifest = self._main_manifest_loaded = False
        if autoload:
            self.load()

    def load(self):
        self._loaded_from_manifest = False
        if self._load_manifest():
            self._scan_generation()
            self._loaded_from_manifest = self._main_manifest_loaded
        else: self._migrate_legacy()

    async def load_async(self, verified_store=None):
        """Read all bytes; reuse decoded sealed-prefix evidence only if unchanged.

        A checkpoint probe is strict and read-only. Ordinary boot/export readers
        retain their existing torn-tail recovery behavior and start with no cache.
        """
        import uasyncio as asyncio
        if verified_store is self:
            raise ValueError("Verification requires an independent point store.")
        self.scan_profile = profile = _scan_profile()
        expected = (verified_store._verification_state()
                    if verified_store is not None else None)
        self._verification_expected = None
        self._verification_complete = False
        self._loaded_from_manifest = False
        started = _ticks_us()
        loaded = self._load_manifest(recover=expected is None)
        profile["scan_manifest_us"] = _elapsed(started)
        if expected is not None:
            if (self._verification_state()[:4] != expected[:4] or
                    (not loaded and (expected[1] or self._manifest_exists()))):
                raise ValueError("Point-store manifest changed during verification.")
            if not loaded:
                try: legacy_bytes = os.stat(self.path)[6]
                except OSError: legacy_bytes = 0
                if legacy_bytes:
                    raise ValueError("Unexpected unverified legacy point data.")
            self._verification_expected = expected
        if loaded:
            prefix = (verified_store._verified_prefix if expected is not None else None)
            steps = self._scan_generation_steps(prefix, strict=expected is not None)
            while True:
                started = _ticks_us()
                done = False
                try:
                    next(steps)
                except StopIteration:
                    done = True
                finally:
                    elapsed = _elapsed(started)
                    profile["scan_work_us_sum"] += elapsed
                    profile["scan_batch_last_us"] = elapsed
                    profile["scan_batch_max_us"] = max(profile["scan_batch_max_us"], elapsed)
                if done:
                    profile["scan_postlude_us"] = elapsed
                    break
                profile["scan_batches"] += 1
                started = _ticks_us()
                await asyncio.sleep_ms(1)
                elapsed = _elapsed(started)
                profile["scan_wait_us_sum"] += elapsed
                profile["scan_wait_last_us"] = elapsed
                profile["scan_wait_max_us"] = max(profile["scan_wait_max_us"], elapsed)
            if expected is not None:
                # Producers may enqueue during the scan; changing stored data
                # or its manifest requires a fresh probe before retiring journals.
                if (verified_store._verification_state() != expected or
                        self.max_order != expected[4] or self.valid_bytes != expected[5]):
                    raise ValueError("Point store changed during verification.")
            else:
                self._loaded_from_manifest = self._main_manifest_loaded
        else:
            # Active trackers have already migrated any legacy store at boot.
            if expected is None:
                self._migrate_legacy()
        self._verification_complete = expected is not None

    def _manifest_exists(self):
        try: os.stat(self.manifest_path); return True
        except OSError: return False

    def _verification_state(self):
        return (self.path, self.generation, tuple(self.segments), tuple(self.strings),
                self.max_order, self.valid_bytes)

    def accept_verification(self, probe):
        """Publish independent prefix evidence only after the checkpoint passes."""
        expected = probe._verification_expected
        if (expected is None or not probe._verification_complete or
                self._verification_state() != expected or
                probe._verification_state() != expected):
            raise ValueError("Point store changed before verification acceptance.")
        self._verified_prefix = probe._verified_prefix

    def checkpoint_counts_match(self, expected_counts, checkpoint_order):
        """Explain a checkpoint deficit only through durable later removes.

        This read-only historical replay is needed only on the uncommon boot
        path after undo. A later append/order alone cannot excuse missing old
        points, and the ordinary checkpoint verification path pays no scan cost.
        """
        counts = {key: 0 for key in expected_counts}
        historical, removed_after = None, set()
        last_order, valid_bytes = 0, 0
        for number in self.segments:
            with open(self._segment_path(number), "r") as source:
                for raw in source:
                    try:
                        value = self._decode(ujson.loads(raw))
                    except LegacyConversionError as error:
                        # Counted like a stored vertex: the checkpoint saw it.
                        value = {"record": "vertex", "order": error.order,
                                 "line_id": error.line_id}
                    order, line_id = int(value["order"]), value["line_id"]
                    if (order <= last_order or not line_id or
                            value["record"] not in ("vertex", "remove")):
                        raise ValueError("Invalid point-store history.")
                    if historical is None and order > checkpoint_order:
                        historical = dict(counts)
                    if line_id in counts:
                        if value["record"] == "vertex":
                            counts[line_id] += 1
                        elif counts[line_id]:
                            counts[line_id] -= 1
                            if order > checkpoint_order:
                                removed_after.add(line_id)
                    last_order = order
                    valid_bytes += len(raw.encode("utf-8"))
        if historical is None:
            historical = counts
        return (last_order == self.max_order and valid_bytes == self.valid_bytes and
            all(historical[key] == expected and key in removed_after and
                counts[key] == len(self.offsets.get(key, ())) +
                len(self.legacy_skipped.get(key, ()))
                for key, expected in expected_counts.items()))

    def _prefix_matches(self, prefix):
        return bool(prefix and prefix["path"] == self.path and
            prefix["generation"] == self.generation and
            len(prefix["segments"]) < len(self.segments) and
            tuple(self.segments[:len(prefix["segments"])]) == prefix["segments"] and
            tuple(self.strings[:len(prefix["strings"])]) == prefix["strings"])

    def _prefix_snapshot(self, digests):
        return {"path": self.path, "generation": self.generation,
            "segments": tuple(self.segments[:-1]), "strings": tuple(self.strings),
            "digests": tuple(digests), "max_order": self.max_order,
            "valid_bytes": self.valid_bytes,
            "offsets": {key: _copy_offsets(values) for key, values in self.offsets.items()},
            "segment_lines": {key: set(values) for key, values in self.segment_lines.items()},
            "legacy_skipped": {key: list(values) for key, values in self.legacy_skipped.items()}}

    def _verify_prefix_steps(self, prefix):
        for number, expected_bytes, expected_digest in prefix["digests"]:
            digest, size = hashlib.sha256(), 0
            with open(self._segment_path(number), "rb") as source:
                while True:
                    raw = source.read(SCAN_HASH_BYTES)
                    if not raw: break
                    digest.update(raw); size += len(raw)
                    self.scan_profile["scan_hash_bytes"] += len(raw)
                    yield None
            if size != expected_bytes or digest.digest() != expected_digest:
                raise ValueError("Verified sealed point segment changed.")
            self.scan_profile["scan_verified_sealed_segments"] += 1

    def _segment_path(self, number, generation=None):
        generation = self.generation if generation is None else generation
        return "%s.g%08x-%04d.pjs" % (self.path, generation, number)

    def _load_manifest(self, recover=True):
        self._main_manifest_loaded = False
        candidates = ((self.manifest_path, self.manifest_path + ".tmp",
                       self.manifest_path + ".previous") if recover else (self.manifest_path,))
        for candidate in candidates:
            try:
                with open(candidate, "r") as source: value = ujson.loads(source.read())
                if (value.get("kind") != "point_store" or
                        value.get("schema") not in (2, 3, POINT_STORE_SCHEMA)): continue
                generation = int(value["generation"])
                segments = [int(item) for item in value["segments"]]
                # An empty segment list is valid: an asset-only restore
                # activates a generation before its first line vertex.
                if not generation: continue
                for number in segments: os.stat(self._segment_path(number, generation))
                strings = value.get("strings") or []
                if not isinstance(strings, list): continue
                self.generation, self.segments = generation, segments
                self._manifest_schema = value.get("schema")
                self.strings = list(strings)
                self._string_codes = dict((item, index)
                                          for index, item in enumerate(self.strings))
                self._main_manifest_loaded = candidate == self.manifest_path
            except (OSError, ValueError, TypeError, KeyError, AttributeError): continue
            if candidate != self.manifest_path:
                # Outside the candidate loop: a marker that cannot be written
                # must stop the load, not fall through to an older fallback.
                self._quarantine_unnamed(candidate)
                try: os.remove(self.manifest_path)
                except OSError: pass
                try: os.rename(candidate, self.manifest_path)
                except OSError: pass  # loaded; the next boot recovers it again
            return True
        return False

    @property
    def quarantine_path(self):
        return self.path + ".recovered.json"

    def _own_segment_files(self):
        """Yield (name, path) of every segment file of this store's prefix."""
        directory, prefix = ".", self.path + ".g"
        if "/" in prefix:
            directory, prefix = prefix.rsplit("/", 1)
        for name in os.listdir(directory or "/"):
            if not (name.startswith(prefix) and name.endswith(".pjs")): continue
            parts = name[len(prefix):-4].split("-")
            if len(parts) != 2 or len(parts[0]) != 8 or not parts[1].isdigit(): continue
            try: int(parts[0], 16)
            except ValueError: continue
            yield name, (name if directory == "." else directory + "/" + name)

    def _names(self, generation, segments):
        # Compared by file name: "./x" and "x" name the same file.
        return set(self._segment_path(int(number), generation).rsplit("/", 1)[-1]
                   for number in segments)

    def _read_quarantine(self):
        """Return (present, names): names is None for an unreadable marker."""
        try:
            with open(self.quarantine_path, "r") as source: raw = source.read()
        except OSError as error:
            # Only "not found" counts as absent; any other read error
            # protects like an unreadable marker.
            return not (error.args and error.args[0] == _ENOENT), None
        try:
            value = ujson.loads(raw)
            if value.get("kind") != "point_store_quarantine": return True, None
            names = set(value["files"])
            if any(type(name) is not str for name in names): return True, None
            return True, names
        except (ValueError, TypeError, KeyError, AttributeError):
            return True, None

    def _protected_names(self):
        """Names a fallback recovery protects, or None without a quarantine.

        An unreadable marker protects every file the active manifest does
        not name.
        """
        present, names = self._read_quarantine()
        if not present: return None
        if names is None:
            live = self._names(self.generation, self.segments)
            names = set(name for name, _path in self._own_segment_files() if name not in live)
        return names

    def _quarantine_unnamed(self, candidate):
        """Persist which files a fallback manifest leaves unnamed, before it is promoted.

        The fallback is older than the damaged main manifest, or a .tmp of a
        switch that may never have been meant to happen. The files it does
        not name can then be the only copy of newer data; once the fallback
        is the main manifest, nothing else would tell a later boot so. The
        marker is written before the rename and an error propagates, so no
        boot can sweep them without it.
        """
        live = self._names(self.generation, self.segments)
        present, names = self._read_quarantine()
        protected = set(names or ())
        protected.update(name for name, _path in self._own_segment_files() if name not in live)
        if not protected and not present: return
        with open(self.quarantine_path, "w") as target:
            target.write(ujson.dumps({"kind": "point_store_quarantine", "schema": 1,
                "source": candidate[len(self.manifest_path):],
                "files": sorted(protected)}))

    def quarantine(self):
        """Return {"files", "bytes"} still protected by a recovery, else None."""
        names = self._protected_names()
        if names is None: return None
        files = size = 0
        for name, path in self._own_segment_files():
            if name not in names: continue
            try: size += os.stat(path)[6]
            except OSError: continue
            files += 1
        return {"files": files, "bytes": size}

    def release_quarantine(self):
        """Explicit decision: the next boot sweep may delete the protected files."""
        try: os.remove(self.quarantine_path)
        except OSError: pass

    def _free_segment_number(self, generation, number):
        """Return number, or the next one whose file no recovery protects.

        _start_segment removes a file of the same name first; a protected
        one (a segment the damaged manifest had already added) must stay.
        """
        names = self._protected_names()
        while names and self._segment_path(number, generation).rsplit("/", 1)[-1] in names:
            number += 1
        return number

    def _next_generation(self, generation):
        """Return generation + 1, skipping a generation whose files are protected.

        Writing a protected generation would truncate the very files a
        recovery kept ("w" and _install_file replace them).
        """
        names = self._protected_names() or ()
        taken = set(name[len(self.path.rsplit("/", 1)[-1]) + 2:][:8] for name in names)
        for _ in range(len(taken) + 1):
            generation = ((generation + 1) & 0xffffffff) or 1
            if "%08x" % generation not in taken: break
        return generation

    def _activate_manifest(self, generation, segments):
        temporary, previous = self.manifest_path + ".tmp", self.manifest_path + ".previous"
        with open(temporary, "w") as target:
            target.write(ujson.dumps({"kind":"point_store","schema":POINT_STORE_SCHEMA,
                "generation":generation,"segments":segments,
                "strings":self.strings}))
        with open(temporary, "r") as source: check = ujson.loads(source.read())
        if check.get("generation") != generation: raise ValueError("Point manifest invalid.")
        try: os.remove(previous)
        except OSError: pass
        try: os.rename(self.manifest_path, previous)
        except OSError: pass
        try: os.rename(temporary, self.manifest_path)
        except Exception:
            try: os.rename(previous, self.manifest_path)
            except OSError: pass
            raise
        try: os.remove(previous)
        except OSError: pass
        self.generation, self.segments = generation, list(segments)
        self._manifest_schema = POINT_STORE_SCHEMA
        self._strings_dirty = False
        if not self._prefix_matches(self._verified_prefix):
            self._verified_prefix = None

    def _decode(self, value, include_measurement=True):
        if isinstance(value, list) and value and value[0] in (_LEGACY_MARKER, _EXACT_MARKER):
            if len(value) == 5:
                measurement = (self._expand_dynamic(_expand(value[4]))
                               if include_measurement else None)
                if value[0] == _EXACT_MARKER:
                    return {"record":"vertex","order":int(value[1]),"line_id":value[2],
                            "coordinate":_exact(value[3]),"measurement":measurement}
                return _legacy_vertex(value[1], value[2], value[3], measurement)
            if len(value) == 3:
                return {"record":"remove","order":int(value[1]),"line_id":value[2]}
        if isinstance(value, dict) and value.get("schema") == 1:
            # Pre-manifest monolith records; _migrate_legacy re-encodes them.
            record = value.get("record")
            if record == "vertex":
                return _legacy_vertex(value.get("order"), value.get("line_id"),
                    value.get("coordinate"),
                    dict(value.get("measurement") or {}) if include_measurement else None)
            if record == "remove":
                return {"record":"remove","order":int(value.get("order")),
                        "line_id":value.get("line_id")}
        raise ValueError("Unknown point-store record.")

    def _intern(self, value):
        if value is None:return value
        code=self._string_codes.get(value)
        if code is None:
            code=len(self.strings);self.strings.append(value)
            self._string_codes[value]=code;self._strings_dirty=True
        return {"@":code}

    def _expand_dynamic(self, measurement):
        for key in _INTERNED_KEYS:
            marker=measurement.get(key)
            if isinstance(marker,dict) and len(marker)==1 and "@" in marker:
                code=marker["@"]
                if not isinstance(code,int) or not 0<=code<len(self.strings):
                    raise ValueError("Invalid point-store string reference.")
                measurement[key]=self.strings[code]
        return measurement

    def _encode(self, value):
        if value["record"] == "vertex":
            # Marker 3 always means e8/mm integers. An unconverted legacy
            # float must fail here instead of being stored as exact.
            coordinate=_exact(value["coordinate"])
            measurement=dict(value["measurement"])
            for key in _INTERNED_KEYS:
                if isinstance(measurement.get(key),str):
                    measurement[key]=self._intern(measurement[key])
            return [_EXACT_MARKER,value["order"],value["line_id"],coordinate,
                    _compact(measurement)]
        return [_EXACT_MARKER,value["order"],value["line_id"]]

    def _scan_file(self, path, number):
        offset = 0
        for offset in self._scan_file_steps(path, number):
            pass
        return offset

    def _scan_file_steps(self, path, number, strict=False):
        offset = 0
        records = 0
        digest = hashlib.sha256()
        clean = True
        with open(path, "r") as source:
            while True:
                raw = source.readline()
                if not raw: break
                encoded = raw.encode("utf-8")
                following = offset + len(encoded)
                try:
                    try:
                        value = self._decode(ujson.loads(raw))
                    except LegacyConversionError as error:
                        # Only this vertex is unusable: keep its bytes, skip it
                        # from the index and go on. The same rule applies in a
                        # strict scan, because the store it verifies skipped it
                        # too. A later tombstone of the line pops the last kept
                        # vertex, which may not be the one the undo meant.
                        value = {"record": "skipped", "order": error.order,
                                 "line_id": error.line_id}
                    order = int(value["order"])
                    line_id = value["line_id"]
                    if order <= self.max_order or not line_id:
                        raise ValueError("Invalid point-store record order or line.")
                    # Tombstone-only segments still belong to their line. A
                    # later rewrite must not drop its removes and revive points.
                    lines = self.segment_lines.get(number)
                    if lines is None:
                        lines = set(); self.segment_lines[number] = lines
                    lines.add(line_id)
                    if value["record"] == "vertex":
                        points = self.offsets.get(line_id)
                        if points is None:
                            points = _offsets(); self.offsets[line_id] = points
                        points.append((number << _OFFSET_BITS) | offset)
                    elif value["record"] == "skipped":
                        self.legacy_skipped.setdefault(line_id, []).append(order)
                    else:
                        points = self.offsets.get(line_id)
                        if points: points.pop()
                    self.max_order, offset = order, following
                    records += 1
                    self.scan_profile["scan_decoded_records"] += 1
                    digest.update(encoded)
                except (ValueError, TypeError, KeyError, AttributeError, IndexError):
                    if strict:
                        raise ValueError("Point-store validation failed; data unchanged.")
                    clean = False
                    break
                if records % SCAN_BATCH_RECORDS == 0:
                    yield offset
        try:
            if os.stat(path)[6] != offset:
                if strict:
                    raise ValueError("Point-store length changed during verification.")
                clean = False
                with open(path, "r+b") as target: target.truncate(offset)
        except (OSError, AttributeError):
            if strict:
                raise ValueError("Point-store file unavailable during verification.")
        self._last_scan_digest, self._last_scan_clean = digest.digest(), clean
        yield offset

    def _scan_generation(self):
        for _ in self._scan_generation_steps():
            pass

    def _scan_generation_steps(self, prefix=None, strict=False):
        self.offsets, self.max_order, self.valid_bytes, self.segment_lines = {}, 0, 0, {}
        self.legacy_skipped = {}
        self._verified_prefix = None
        start, digests, clean = 0, [], True
        if self._prefix_matches(prefix):
            for _ in self._verify_prefix_steps(prefix):
                yield None
            self.offsets = {key: _copy_offsets(values) for key, values in prefix["offsets"].items()}
            self.segment_lines = {key: set(values) for key, values in prefix["segment_lines"].items()}
            self.legacy_skipped = {key: list(values) for key, values in prefix["legacy_skipped"].items()}
            self.max_order, self.valid_bytes = prefix["max_order"], prefix["valid_bytes"]
            start, digests = len(prefix["segments"]), list(prefix["digests"])
            self._verified_prefix = prefix
            yield None
        for index in range(start, len(self.segments)):
            number = self.segments[index]
            offset = 0
            for offset in self._scan_file_steps(self._segment_path(number), number, strict):
                yield None
            self.valid_bytes += offset
            clean = clean and self._last_scan_clean
            if index < len(self.segments) - 1:
                digests.append((number, offset, self._last_scan_digest))
                if clean and index == len(self.segments) - 2:
                    self._verified_prefix = self._prefix_snapshot(digests)
                    yield None
        if not self.segments:
            # _ensure_writer registers segment 1 before the first record.
            self._writer_segment, self._writer_bytes = 0, 0
            return
        self._writer_segment = self.segments[-1]
        try: self._writer_bytes = os.stat(self._segment_path(self._writer_segment))[6]
        except OSError: self._writer_bytes = 0

    def _migrate_legacy(self):
        try: os.stat(self.path)
        except OSError: return
        generation = max(1, int(time.time()) & 0xffffffff); target = self._segment_path(1,generation)
        count, skipped, last = 0, {}, 0
        with open(self.path,"r") as source, open(target,"w") as output:
            for raw in source:
                try:
                    value = self._decode(ujson.loads(raw))
                    if int(value["order"]) <= last:
                        raise ValueError("Invalid point-store record order.")
                    output.write(ujson.dumps(self._encode(value))+"\n")
                    count += 1; last = int(value["order"])
                except LegacyConversionError as error:
                    # Left out like in a segment scan; the order stays used.
                    if error.order <= last: break
                    skipped.setdefault(error.line_id, []).append(error.order)
                    last = error.order
                except (ValueError,TypeError,AttributeError): break
        if count:
            self._activate_manifest(generation,[1]); self._scan_generation()
            self.legacy_skipped = skipped
            self.max_order = max(self.max_order, last)
            try: os.remove(self.path)
            except OSError: pass
        else:
            try: os.remove(target)
            except OSError: pass

    def sequences(self,line_id):
        self.offsets.setdefault(line_id,_offsets())
        return FlashSequence(self,line_id,"coordinate"),FlashSequence(self,line_id,"measurement")

    def _start_segment(self,generation,number):
        """Create segment ``number`` empty on flash, then name it in the manifest.

        A file of the same name can survive on flash when power fails before
        cleanup (restored generations restart at old + 1, a clock-derived
        generation repeats after an unsynced boot). Appending behind its
        records would hide the new vertices or revive the old ones on the next
        scan. LittleFS commits an "w" truncation of an open file only at its
        sync or close, so the old file is removed and the new one created and
        flushed before the manifest switch; a power cut can then never leave
        the manifest naming the stale bytes. If the switch fails, the empty
        file stays unnamed and the writer state is unchanged, so the next
        append retries the same step.
        """
        path=self._segment_path(number,generation)
        try:os.remove(path)
        except OSError:pass
        writer=open(path,"w")
        try:
            writer.flush()
            self._activate_manifest(generation,self.segments+[number])
        except BaseException:
            writer.close();raise
        self._writer,self._writer_segment,self._writer_bytes=writer,number,0

    def _ensure_writer(self,size,line_id):
        if not self.segments:
            # A fresh store (generation 0) or an asset-only restore, whose
            # manifest names a generation but no segment: register segment 1
            # before the first record, never append to an unnamed file.
            self.close()
            generation=self.generation or max(1,int(time.time())&0xffffffff)
            self._start_segment(generation,self._free_segment_number(generation,1))
        current_lines=self.segment_lines.get(self._writer_segment,set())
        if self._writer_bytes and (self._writer_bytes+size>POINT_SEGMENT_BYTES or
                (current_lines and line_id not in current_lines)):
            if self.reserve_check is not None and not self.reserve_check():
                raise OSError("Point-store rotation would violate the flash reserve.")
            self.close()
            self._start_segment(self.generation,
                                self._free_segment_number(self.generation,max(self.segments)+1))
        if self._manifest_schema < POINT_STORE_SCHEMA:
            # Schema <= 3 firmware truncates marker-3 records it cannot decode,
            # and vertices are not journaled. Relabel before the first write.
            self._activate_manifest(self.generation,self.segments)
        if self._writer is None: self._writer=open(self._segment_path(self._writer_segment),"a")

    def append(self,order,line_id,coordinate,measurement):
        order=int(order)
        if order<=self.max_order:return False
        value={"record":"vertex","order":order,"line_id":line_id,
               "coordinate":coordinate,"measurement":measurement}
        phase=_ticks_us();raw=ujson.dumps(self._encode(value))+"\n";size=len(raw.encode("utf-8"))
        self.profile_us["encode"]+=_elapsed(phase)
        if self._strings_dirty and self.generation:
            self._activate_manifest(self.generation,self.segments)
        phase=_ticks_us();self._ensure_writer(size,line_id);self.profile_us["ensure_writer"]+=_elapsed(phase)
        locator=(self._writer_segment<<_OFFSET_BITS)|self._writer_bytes
        phase=_ticks_us();self._writer.write(raw);self.profile_us["write"]+=_elapsed(phase)
        self._writer_bytes+=size;self.valid_bytes+=size
        self.offsets.setdefault(line_id,_offsets()).append(locator);self.max_order=order
        self.segment_lines.setdefault(self._writer_segment,set()).add(line_id)
        self._pending_writes+=1;self._remember(locator,value)
        if self._pending_writes>=POINT_WRITE_BATCH:self.sync()
        return True

    def remove_last(self,order,line_id):
        order=int(order)
        if order<=self.max_order:return False
        value={"record":"remove","order":order,"line_id":line_id}
        raw=ujson.dumps(self._encode(value))+"\n";size=len(raw.encode("utf-8"));self._ensure_writer(size,line_id)
        self._writer.write(raw);self._writer_bytes+=size;self.valid_bytes+=size;self._pending_writes+=1
        self.segment_lines.setdefault(self._writer_segment,set()).add(line_id)
        points=self.offsets.get(line_id)
        if points:
            removed=points.pop();self._cache.pop(removed,None)
            try:self._cache_order.remove(removed)
            except ValueError:pass
        self.max_order=order;self.sync();return True

    def sync(self):
        if self._writer is not None and self._pending_writes:
            self._writer.flush();self._pending_writes=0
    def close(self):
        if self._writer is not None:
            self.sync();self._writer.close();self._writer=None
    def __del__(self):
        try:self.close()
        except Exception:pass

    def iter_line(self, line_id, start=0, stop=None, step=1, include_measurement=True):
        """Read surviving indexed vertices sequentially, including after undo.

        The offset index, not physical record order, selects live points. Skip
        interleaved lines and tombstoned vertices without reopening each row.
        Maps can skip measurement expansion; boot and detail readers retain it.
        """
        values = self.offsets.get(line_id, ())
        start = max(0, int(start))
        stop = len(values) if stop is None else min(len(values), max(0, int(stop)))
        step = int(step)
        if step < 1:
            raise ValueError("Point stride must be positive.")
        # Capture only the requested locators; later appends/undo must not move
        # the population under an HTTP response that has already started.
        locators = _copy_offsets(values[index] for index in range(start, stop, step))
        return self._iter_line_locators(line_id, locators, include_measurement)

    def line_locators(self, line_id, stop):
        """A copy of the line's first stop locators: a snapshot for later passes.

        The bytes they name stay valid: segments are append-only, rotation only
        adds segments and a rewrite waits for readers.
        """
        values = self.offsets.get(line_id, ())
        return _copy_offsets(values[index] for index in range(min(int(stop), len(values))))

    def iter_locators(self, line_id, locators, include_measurement=True):
        """Read the vertices of a locator snapshot sequentially, in its order."""
        rows = self._iter_line_locators(line_id, locators, include_measurement)
        try:
            for value in rows:
                yield value
        finally:
            rows.close()

    def _iter_line_locators(self, line_id, locators, include_measurement):
        self.sync()
        profile = self.profile_us
        source, current_number, position = None, None, 0
        buffer, buffer_at = b"", 0
        try:
            for locator in locators:
                number, offset = locator >> _OFFSET_BITS, locator & _OFFSET_MASK
                if number != current_number:
                    if source: source.close()
                    phase = _ticks_us()
                    source = open(self._segment_path(number), "rb")
                    profile["read"] += _elapsed(phase)
                    current_number, position = number, 0
                    buffer = b""
                if offset < position:
                    raise ValueError("Unordered point locator.")
                if not buffer or offset >= buffer_at + len(buffer):
                    phase = _ticks_us()
                    source.seek(offset)
                    profile["read"] += _elapsed(phase)
                    buffer, buffer_at = b"", offset
                start = offset - buffer_at
                while True:
                    end = buffer.find(b"\n", start)
                    if end >= 0:
                        raw = buffer[start:end + 1]
                        break
                    if start:
                        buffer, buffer_at, start = buffer[start:], offset, 0
                    phase = _ticks_us()
                    block = source.read(4096)
                    profile["read"] += _elapsed(phase)
                    if not block:
                        raw = buffer[start:]
                        if not raw:
                            raise ValueError("Point locator beyond segment end.")
                        break
                    buffer += block
                position = offset + len(raw)
                phase = _ticks_us()
                value = self._decode(ujson.loads(raw), include_measurement)
                profile["decode"] += _elapsed(phase)
                if value["record"] != "vertex" or (line_id is not None and value["line_id"] != line_id):
                    raise ValueError("Point locator does not match line.")
                yield value
        finally:
            if source: source.close()

    def read(self,locator):
        if locator in self._cache:
            value=self._cache[locator]
            try:self._cache_order.remove(locator)
            except ValueError:pass
            self._cache_order.append(locator);return value
        self.sync();number,offset=locator>>_OFFSET_BITS,locator&_OFFSET_MASK
        with open(self._segment_path(number),"r") as source:
            source.seek(offset);value=self._decode(ujson.loads(source.readline()))
        if value["record"]!="vertex":raise ValueError("Point locator is not a vertex.")
        self._remember(locator,value);return value

    def _remember(self,locator,value):
        self._cache[locator]=value
        try:self._cache_order.remove(locator)
        except ValueError:pass
        self._cache_order.append(locator)
        while len(self._cache_order)>self.cache_size:self._cache.pop(self._cache_order.pop(0),None)

    def storage_bytes(self):
        total=0
        for number in self.segments:
            try:total+=os.stat(self._segment_path(number))[6]
            except OSError:pass
        try:total+=os.stat(self.manifest_path)[6]
        except OSError:pass
        return total
    def sidecar_paths(self):return [self._segment_path(n) for n in self.segments]

    def _manifest_segment_names(self, path):
        """Return the segment file names a readable manifest names, else None."""
        try:
            with open(path, "r") as source: value = ujson.loads(source.read())
            if value.get("kind") != "point_store": return None
            generation = int(value["generation"])
            return set(self._segment_path(int(number), generation)
                       for number in value["segments"])
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return None

    def remove_unreferenced(self):
        """Delete this store's segment files that no manifest names.

        A power cut, an aborted rewrite or an interrupted cleanup can leave
        whole segment files behind that nothing reads any more; they only
        cost flash. Callable only after a boot load read the main manifest
        itself and scanned every segment it names: a missing or invalid
        manifest, or one recovered from .tmp or .previous, deletes nothing,
        because the files may then be the only copy of the newer data. That
        recovery records the files it left unnamed in quarantine_path, and
        every later sweep keeps them until release_quarantine() (or until
        they are gone); an unreadable marker keeps everything. Files named
        by .previous are kept. Never call it while another instance writes
        the same prefix: a new segment exists before its manifest names it.
        Returns (files, bytes) removed.
        """
        result = [0, 0]
        for _ in self.remove_unreferenced_steps(result): pass
        return result[0], result[1]

    def remove_unreferenced_steps(self, result):
        """remove_unreferenced, yielding after each file; adds to result [files, bytes]."""
        if not self._loaded_from_manifest:
            return
        present, protected = self._read_quarantine()
        if present and protected is None:
            return
        keep = self._names(self.generation, self.segments)
        previous = self._manifest_segment_names(self.manifest_path + ".previous")
        if previous: keep |= set(path.rsplit("/", 1)[-1] for path in previous)
        if protected: keep |= protected
        try:
            names = list(self._own_segment_files())
        except OSError:
            return
        remaining = 0
        for name, path in names:
            if name in keep:
                remaining += bool(protected and name in protected)
                continue
            try:
                length = os.stat(path)[6]
                os.remove(path)
            except OSError:
                continue
            result[0] += 1; result[1] += length
            yield None
        if present and not remaining:
            # Every protected file was removed by hand: nothing left to decide.
            self.release_quarantine()
        elif not present:
            # A reset between writing .tmp and the switch leaves a .tmp of a
            # state that never became active; as a later fallback it would
            # revive it. The main manifest rules, so it goes, unless a
            # quarantine keeps it as evidence for a manual recovery.
            temporary = self.manifest_path + ".tmp"
            try:
                length = os.stat(temporary)[6]
                os.remove(temporary)
            except OSError:
                return
            result[0] += 1; result[1] += length
            yield None
    def rewrite_temporary_bytes(self,active_line_ids):
        wanted=set(active_line_ids)
        if self.segments and all(len(self.segment_lines.get(n,set()))<=1 for n in self.segments):
            return 4096
        return (self.storage_bytes()*5)//4+4096

    def rewrite(self,active_line_ids):
        self.close();wanted=set(active_line_ids)
        # Normal V15 segments never mix lines. Archiving can therefore switch
        # the manifest first and release complete inactive segments without a
        # second full-store generation.
        if self.segments and all(len(self.segment_lines.get(n,set())) <= 1 for n in self.segments):
            kept=[n for n in self.segments if self.segment_lines.get(n,set()) <= wanted and
                  self.segment_lines.get(n,set())]
            dropped=[n for n in self.segments if n not in kept]
            # A normal checkpoint does not change the active line set. Avoid
            # rewriting the manifest and rescanning every point in that common case.
            if not dropped and kept == self.segments:
                return
            if kept:
                self._activate_manifest(self.generation,kept);self._scan_generation();self.close_cache()
                for number in dropped:
                    try:os.remove(self._segment_path(number))
                    except OSError:pass
                return
        retained=[]
        for line_id,values in self.offsets.items():
            if line_id in wanted:retained.extend(values)
        retained.sort();old_generation,old_segments=self.generation,list(self.segments)
        generation=self._next_generation(old_generation);number,size,target=1,0,None;new=[]
        try:
            for locator in retained:
                raw=ujson.dumps(self._encode(self.read(locator)))+"\n";encoded=len(raw.encode("utf-8"))
                if target is None or size+encoded>POINT_SEGMENT_BYTES:
                    if target:target.close()
                    new.append(number);size=0;target=open(self._segment_path(number,generation),"w");number+=1
                target.write(raw);size+=encoded
            if target:target.close();target=None
            if not new:
                new=[1]
                with open(self._segment_path(1,generation),"w"):pass
            previous=self.generation,self.segments;self.generation,self.segments=generation,new
            self._scan_generation()
            if sum(len(v)for v in self.offsets.values())!=len(retained):
                raise ValueError("Rewritten point generation failed validation.")
            self._activate_manifest(generation,new);self.close_cache()
            for old in old_segments:
                try:os.remove(self._segment_path(old,old_generation))
                except OSError:pass
        except Exception:
            if target:target.close()
            self.generation,self.segments=old_generation,old_segments;self._scan_generation()
            for item in new:
                try:os.remove(self._segment_path(item,generation))
                except OSError:pass
            raise

    def _adopt_index(self, probe):
        self.offsets, self.max_order = probe.offsets, probe.max_order
        self.valid_bytes, self.segment_lines = probe.valid_bytes, probe.segment_lines
        self._writer_segment, self._writer_bytes = probe._writer_segment, probe._writer_bytes
        self.close_cache()
        self._verified_prefix = None

    async def rewrite_async(self, active_line_ids, unchanged=None):
        """Verify the replacement cooperatively before publishing its manifest."""
        import uasyncio as asyncio
        self.close()
        wanted = set(active_line_ids)
        old_generation, old_segments = self.generation, list(self.segments)
        separate = self.segments and all(len(self.segment_lines.get(n, ())) <= 1
                                         for n in self.segments)
        kept = ([n for n in self.segments if self.segment_lines.get(n, set()) and
                 self.segment_lines[n] <= wanted] if separate else [])
        if separate and kept == self.segments:
            return
        probe = FlashPointStore(self.path, autoload=False)
        probe.strings = list(self.strings)
        probe._string_codes = dict(self._string_codes)
        new_generation = old_generation if kept else self._next_generation(old_generation)
        probe.generation = new_generation
        created, target, rows, scan = [], None, None, None
        committed = False
        async def service():
            await asyncio.sleep_ms(1)
            if unchanged: unchanged()
        try:
            if kept:
                probe.segments = kept
            else:
                retained = []
                for line_id, values in self.offsets.items():
                    if line_id in wanted: retained.extend(values)
                retained.sort()
                rows = self._iter_line_locators(None, retained, True)
                number, size = 0, 0
                for index, value in enumerate(rows):
                    raw = ujson.dumps(probe._encode(value)) + "\n"
                    encoded = len(raw.encode("utf-8"))
                    if target is None or size + encoded > POINT_SEGMENT_BYTES:
                        if target: target.close()
                        number += 1; size = 0; created.append(number)
                        target = open(probe._segment_path(number), "w")
                    target.write(raw); size += encoded
                    if index % SCAN_BATCH_RECORDS == SCAN_BATCH_RECORDS - 1:
                        await service()
                if target: target.close(); target = None
                if not created:
                    created.append(1)
                    with open(probe._segment_path(1), "w"): pass
                probe.segments = created
            await service()
            scan = probe._scan_generation_steps(strict=True)
            for _ in scan:
                await service()
            expected = sum(len(v) for key, v in self.offsets.items() if key in wanted)
            if sum(len(v) for v in probe.offsets.values()) != expected:
                raise ValueError("Rewritten point generation failed validation.")
            if unchanged: unchanged()
            # Re-encoding may intern strings (a literal legacy station_id, a
            # frame or mount). They must be committed with the new generation.
            old_strings, old_codes = self.strings, self._string_codes
            if probe.strings[:len(old_strings)] != old_strings:
                raise ValueError("Point-store strings changed during rewrite.")
            self.strings, self._string_codes = probe.strings, probe._string_codes
            try:
                self._activate_manifest(probe.generation, probe.segments)
            except Exception:
                self.strings, self._string_codes = old_strings, old_codes
                raise
            self._adopt_index(probe)
            committed = True
            for number in old_segments:
                if old_generation == new_generation and number in kept: continue
                try: os.remove(self._segment_path(number, old_generation))
                except OSError: pass
                await asyncio.sleep_ms(1)
        finally:
            if rows is not None: rows.close()
            if scan is not None: scan.close()
            if target: target.close()
            if not committed:
                for number in created:
                    try: os.remove(probe._segment_path(number))
                    except OSError: pass

    def remove_all(self):
        self.close()
        self._verified_prefix = None
        for path in self.sidecar_paths()+[self.manifest_path,self.manifest_path+".tmp",
                self.manifest_path+".previous",self.path]:
            try:os.remove(path)
            except OSError:pass
    def close_cache(self):self._cache.clear();self._cache_order[:]=[]
