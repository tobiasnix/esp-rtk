# SPDX-License-Identifier: AGPL-3.0-only
"""Generation-based segmented flash store with bounded RAM expansion."""
import os
import time
import ujson
try:
    import hashlib
except ImportError:  # pragma: no cover
    import uhashlib as hashlib
try:
    from array import array
except ImportError:  # pragma: no cover
    array = None

POINT_STORE_SCHEMA = 3
POINT_CACHE_SIZE = 32
POINT_WRITE_BATCH = 16
POINT_SEGMENT_BYTES = 131072
# Isolated hardware: eight records take 21.5 ms median, while other ready
# services add further time between GNSS turns. Keep each decode turn smaller.
SCAN_BATCH_RECORDS = 4
SCAN_HASH_BYTES = 4096
_OFFSET_BITS = 20
_OFFSET_MASK = (1 << _OFFSET_BITS) - 1
_KEYS = ("recorded_at","fix_quality","fix_status","satellites","hdop",
    "correction_age_sec","station_id","source","accuracy_observation",
    "receiver_accuracy","quality","profile_id","record_order","samples",
    "rtk_fixed_samples","horizontal_mean_deviation_m","horizontal_max_deviation_m",
    "vertical_mean_deviation_m","vertical_max_deviation_m","expected_samples",
    "horizontal_sigma_mean_m","horizontal_sigma_max_m","vertical_sigma_mean_m",
    "vertical_sigma_max_m","horizontal_sigma_m","vertical_sigma_m",
    "semi_major_sigma_m","semi_minor_sigma_m","altitude_sigma_m","accepted",
    "reasons","duration_sec","source_samples","gst_complete",
    "accepted_fix_samples")
_KEY_TO_CODE = dict((key,index) for index,key in enumerate(_KEYS))
_STRINGS = ("automatic", "manual", "start", "RTK_FIXED", "RTK_FLOAT",
    "GPS", "DGPS", "NMEA_GST", "accepted", "rejected")
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
        self.generation, self.segments = 0, []
        self.strings, self._string_codes, self._strings_dirty = [], {}, False
        self._cache, self._cache_order = {}, []
        self._writer, self._pending_writes = None, 0
        self._writer_segment, self._writer_bytes = 0, 0
        self.profile_us={"encode":0,"ensure_writer":0,"write":0}
        # RAM-only evidence from fully decoded flash, never from pending writes.
        # Its index is the state BEFORE mutable-tail remove events are applied.
        self._verified_prefix = None
        self._verification_expected = None
        self._verification_complete = False
        self.scan_profile = _scan_profile()
        if autoload:
            self.load()

    def load(self):
        if self._load_manifest(): self._scan_generation()
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
                    value = self._decode(ujson.loads(raw))
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
                counts[key] == len(self.offsets.get(key, ()))
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
            "segment_lines": {key: set(values) for key, values in self.segment_lines.items()}}

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
        candidates = ((self.manifest_path, self.manifest_path + ".tmp",
                       self.manifest_path + ".previous") if recover else (self.manifest_path,))
        for candidate in candidates:
            try:
                with open(candidate, "r") as source: value = ujson.loads(source.read())
                if (value.get("kind") != "point_store" or
                        value.get("schema") not in (2, POINT_STORE_SCHEMA)): continue
                generation = int(value["generation"])
                segments = [int(item) for item in value["segments"]]
                if not generation or not segments: continue
                for number in segments: os.stat(self._segment_path(number, generation))
                strings = value.get("strings") or []
                if not isinstance(strings, list): continue
                self.generation, self.segments = generation, segments
                self.strings = list(strings)
                self._string_codes = dict((item, index)
                                          for index, item in enumerate(self.strings))
                if candidate != self.manifest_path:
                    try: os.remove(self.manifest_path)
                    except OSError: pass
                    os.rename(candidate, self.manifest_path)
                return True
            except (OSError, ValueError, TypeError, KeyError, AttributeError): pass
        return False

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
        self._strings_dirty = False
        if not self._prefix_matches(self._verified_prefix):
            self._verified_prefix = None

    def _decode(self, value, include_measurement=True):
        if isinstance(value, list) and value and value[0] == 2:
            if len(value) == 5:
                return {"record":"vertex","order":int(value[1]),"line_id":value[2],
                        "coordinate":value[3],
                        "measurement":(self._expand_dynamic(_expand(value[4]))
                                       if include_measurement else None)}
            if len(value) == 3:
                return {"record":"remove","order":int(value[1]),"line_id":value[2]}
        if isinstance(value, dict) and value.get("schema") == 1: return value
        raise ValueError("Unknown point-store record.")

    def _intern(self, value):
        if value is None:return value
        code=self._string_codes.get(value)
        if code is None:
            code=len(self.strings);self.strings.append(value)
            self._string_codes[value]=code;self._strings_dirty=True
        return {"@":code}

    def _expand_dynamic(self, measurement):
        for key in ("profile_id","station_id"):
            marker=measurement.get(key)
            if isinstance(marker,dict) and len(marker)==1 and "@" in marker:
                code=marker["@"]
                if not isinstance(code,int) or not 0<=code<len(self.strings):
                    raise ValueError("Invalid point-store string reference.")
                measurement[key]=self.strings[code]
        return measurement

    def _encode(self, value):
        if value["record"] == "vertex":
            measurement=dict(value["measurement"])
            for key in ("profile_id","station_id"):
                if isinstance(measurement.get(key),str):
                    measurement[key]=self._intern(measurement[key])
            return [2,value["order"],value["line_id"],value["coordinate"],
                    _compact(measurement)]
        return [2,value["order"],value["line_id"]]

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
                    value = self._decode(ujson.loads(raw)); order = int(value["order"])
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
        self._verified_prefix = None
        start, digests, clean = 0, [], True
        if self._prefix_matches(prefix):
            for _ in self._verify_prefix_steps(prefix):
                yield None
            self.offsets = {key: _copy_offsets(values) for key, values in prefix["offsets"].items()}
            self.segment_lines = {key: set(values) for key, values in prefix["segment_lines"].items()}
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
        self._writer_segment = self.segments[-1]
        try: self._writer_bytes = os.stat(self._segment_path(self._writer_segment))[6]
        except OSError: self._writer_bytes = 0

    def _migrate_legacy(self):
        try: os.stat(self.path)
        except OSError: return
        generation = max(1, int(time.time()) & 0xffffffff); target = self._segment_path(1,generation)
        count = 0
        with open(self.path,"r") as source, open(target,"w") as output:
            for raw in source:
                try:
                    output.write(ujson.dumps(self._encode(self._decode(ujson.loads(raw))))+"\n")
                    count += 1
                except (ValueError,TypeError,AttributeError): break
        if count:
            self._activate_manifest(generation,[1]); self._scan_generation()
            try: os.remove(self.path)
            except OSError: pass
        else:
            try: os.remove(target)
            except OSError: pass

    def sequences(self,line_id):
        self.offsets.setdefault(line_id,_offsets())
        return FlashSequence(self,line_id,"coordinate"),FlashSequence(self,line_id,"measurement")

    def _ensure_writer(self,size,line_id):
        if not self.generation:
            self._activate_manifest(max(1,int(time.time())&0xffffffff),[1])
            self._writer_segment,self._writer_bytes=1,0
        current_lines=self.segment_lines.get(self._writer_segment,set())
        if self._writer_bytes and (self._writer_bytes+size>POINT_SEGMENT_BYTES or
                (current_lines and line_id not in current_lines)):
            if self.reserve_check is not None and not self.reserve_check():
                raise OSError("Point-store rotation would violate the flash reserve.")
            self.close(); self._writer_segment+=1; self._writer_bytes=0
            self._activate_manifest(self.generation,self.segments+[self._writer_segment])
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

    def _iter_line_locators(self, line_id, locators, include_measurement):
        self.sync()
        source, current_number, position = None, None, 0
        buffer, buffer_at = b"", 0
        try:
            for locator in locators:
                number, offset = locator >> _OFFSET_BITS, locator & _OFFSET_MASK
                if number != current_number:
                    if source: source.close()
                    source = open(self._segment_path(number), "rb")
                    current_number, position = number, 0
                    buffer = b""
                if offset < position:
                    raise ValueError("Unordered point locator.")
                if not buffer or offset >= buffer_at + len(buffer):
                    source.seek(offset)
                    buffer, buffer_at = b"", offset
                start = offset - buffer_at
                while True:
                    end = buffer.find(b"\n", start)
                    if end >= 0:
                        raw = buffer[start:end + 1]
                        break
                    if start:
                        buffer, buffer_at, start = buffer[start:], offset, 0
                    block = source.read(4096)
                    if not block:
                        raw = buffer[start:]
                        if not raw:
                            raise ValueError("Point locator beyond segment end.")
                        break
                    buffer += block
                position = offset + len(raw)
                value = self._decode(ujson.loads(raw), include_measurement)
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
        generation=((old_generation+1)&0xffffffff)or 1;number,size,target=1,0,None;new=[]
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
        new_generation = old_generation if kept else ((old_generation + 1) & 0xffffffff) or 1
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
            self._activate_manifest(probe.generation, probe.segments)
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
