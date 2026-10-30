# SPDX-License-Identifier: AGPL-3.0-only
"""Stakeout progress: stakeout points, the progress log and target status.

A confirmation commits as one survey point of object_type "stakeout"
(Tracker.add_asset) that names its target (stakeout_tid), the target's
generation (stakeout_gen) and the request's command (stakeout_cmd).
valid_point_fields is the one check of those fields: add_asset and the
backup validator refuse a point that fails it.

What no point says lives in "<store root>/<project_id>/progress.jsonl": a
header line (project ID and fingerprint), then one event per line (skip,
reopen, active, order, set_removed, restored). Events carry absolute
generations, so a line written twice folds the same. A Fold is the folded
log of one project; the status of a target is derived from the fold and
the project's stakeout points (derive). ProgressLog loads folds under the
store's change lock, caches the fold of the active project and appends
events; an append that raises once it opened the file sets the store's
uncertain latch (stakeout_write_uncertain until restart). A load compacts a
grown log into a verified snapshot of its fold (ProgressLog._compact).
"""
import os
import time
import ujson
import stakeout
from cfg import log

# Generations are 0 <= gen < GEN_LIMIT; a larger one is never written.
GEN_LIMIT = 10 ** 12
SOURCES = ("stakeout_verified", "stakeout_manual")
PROGRESS_FILE = "progress.jsonl"
COMPACT_BYTES = 32768
# A longer line is damaged; the read piece is as long.
LINE_MAX = 4096
_SEQ_MASK = (1 << stakeout.SEQ_BITS) - 1


def valid_command(value):
    """True for exactly 16 lowercase hex digits (a request's command)."""
    return isinstance(value, str) and len(value) == 16 and stakeout._lower_hex(value)


def _number(value, minimum=None):
    # type() excludes bool; value - value is nan for nan and both infinities.
    return ((type(value) is int or type(value) is float) and value - value == 0
            and (minimum is None or value >= minimum))


def valid_point_fields(point):
    """True when point is a complete stakeout point (object type, target, generation, command, offsets)."""
    gen = point.get("stakeout_gen")
    tolerance = point.get("stakeout_tolerance_m")
    measurement = point.get("measurement")
    return (point.get("object_type") == "stakeout"
            and stakeout.parse_tid(point.get("stakeout_tid")) is not None
            and type(gen) is int and 0 <= gen < GEN_LIMIT
            and valid_command(point.get("stakeout_cmd"))
            and isinstance(measurement, dict) and measurement.get("source") in SOURCES
            and (tolerance is None or _number(tolerance, 0))
            and _number(point.get("stakeout_dn_m")) and _number(point.get("stakeout_de_m"))
            and _number(point.get("stakeout_dist_m"), 0)
            and point.get("line_id") is None)


def _gen(value):
    """True for a generation: an int (no bool) with 0 <= value < GEN_LIMIT."""
    return type(value) is int and 0 <= value < GEN_LIMIT


def derive(value, floor, point):
    """(status, mode, gen, point) of a target: value is its fold entry
    (gen << 1 | skipped, 0 when untouched), floor the effective floor of its
    set, point its index point or None. The generation is the highest of
    the three; the point confirms only at that generation."""
    gen = max(value >> 1, floor)
    if point is not None and point["stakeout_gen"] >= gen:
        return "confirmed", point["measurement"]["source"][9:], point["stakeout_gen"], point
    if value & 1 and value >> 1 == gen:
        return "skipped", None, gen, None
    return "open", None, gen, None


def _put(index, point):
    """Index point under its set and target key; the highest generation
    wins, a tie keeps the point indexed first."""
    set_id, part, seq = stakeout.parse_tid(point["stakeout_tid"])
    targets = index.setdefault(set_id, {})
    key = stakeout.target_key(part, seq)
    old = targets.get(key)
    if old is None or point["stakeout_gen"] > old["stakeout_gen"]:
        targets[key] = point


class Fold:
    """The folded progress log of one project.

    entries {set_id: {key: gen << 1 | skipped}} holds touched targets only,
    floors {set_id: floor} the floors of removed sets, base the project
    floor of the last restore, removed {set_id: imported_at | None} the
    committed set removals, _orders {set_id: set of reversed parts}. size is
    the file length, base_size the length of its leading lines without a
    time (header and snapshot, E2/E7), torn tells that it ends without a
    line break. The
    point index ({set_id: {key: point}}, valid stakeout points only) is
    rebuilt when the points list or its length changed; generation is the
    ProgressLog generation the fold was loaded in (ProgressLog.valid).
    """

    def __init__(self, project_id, fingerprint, generation=0):
        self.project_id = project_id
        self.fingerprint = fingerprint
        self.generation = generation
        self.entries = {}
        self.floors = {}
        self.base = 0
        self.removed = {}
        self.active_tid = None
        self._orders = {}
        self.size = 0
        self.base_size = 0
        self.torn = False
        self.damaged_lines = 0
        self.ignored_points = 0
        self._points = None
        self._count = 0
        self._index = {}

    def floor_of(self, set_id):
        """The effective floor of set_id: its own or the project floor."""
        return max(self.floors.get(set_id, 0), self.base)

    def apply(self, event):
        """Fold one event (a dict); False when it is not a valid event."""
        ev = event.get("ev")
        if ev == "restored":
            floor = event.get("floor")
            if not _gen(floor):
                return False
            self.base = max(self.base, floor)
            return True
        if ev == "active":
            tid = event.get("tid")
            if tid is not None and stakeout.parse_tid(tid) is None:
                return False
            self.active_tid = tid
            return True
        if ev == "order":
            part_id = event.get("part_id")
            flag = event.get("reversed")
            parsed = isinstance(part_id, str) and stakeout.parse_tid(part_id + "/1")
            if not parsed or type(flag) is not bool:
                return False
            parts = self._orders.setdefault(parsed[0], set())
            if flag:
                parts.add(parsed[1])
            else:
                parts.discard(parsed[1])
                if not parts:
                    del self._orders[parsed[0]]
            return True
        if ev == "set_removed":
            set_id = event.get("set_id")
            imported_at = event.get("imported_at")
            floor = event.get("floor")
            if not (stakeout.valid_set_id(set_id) and _gen(floor)
                    and (imported_at is None or type(imported_at) is int)):
                return False
            self.entries.pop(set_id, None)
            self._orders.pop(set_id, None)
            if self.active_tid and self.active_tid.startswith(set_id + "/"):
                self.active_tid = None
            self.floors[set_id] = max(self.floors.get(set_id, 0), floor)
            self.removed[set_id] = imported_at
            return True
        if ev != "skip" and ev != "reopen":
            return False
        parsed = stakeout.parse_tid(event.get("tid"))
        gen = event.get("gen")
        if parsed is None or not _gen(gen):
            return False
        set_id, part, seq = parsed
        entries = self.entries.setdefault(set_id, {})
        key = stakeout.target_key(part, seq)
        least = max(entries.get(key, 0) >> 1, self.floor_of(set_id))
        if ev == "skip":
            if gen >= least:
                entries[key] = gen << 1 | 1
        elif gen > least:
            entries[key] = gen << 1
        return True

    # --- point index --------------------------------------------------------

    def _index_of(self, points):
        if self._points is not points or self._count != len(points):
            self._build(points)
        return self._index

    def _build(self, points):
        index = {}
        ignored = 0
        for point in points:
            if point.get("object_type") == "stakeout":
                if valid_point_fields(point):
                    _put(index, point)
                else:
                    ignored += 1
        self._index = index
        self._points = points
        self._count = len(points)
        self.ignored_points = ignored
        if ignored:
            log("WARN", "STAKE", "%d invalid stakeout points of %s ignored."
                % (ignored, self.project_id))

    def note_point(self, point, points):
        """point (valid) was just appended to points: index it without a rebuild."""
        if self._points is points and self._count + 1 == len(points):
            _put(self._index, point)
            self._count += 1

    # --- status -------------------------------------------------------------

    def _derive(self, set_id, key, index):
        return derive(self.entries.get(set_id, {}).get(key, 0), self.floor_of(set_id),
                      index.get(set_id, {}).get(key))

    def _keys(self, set_id, index):
        keys = set(self.entries.get(set_id, ()))
        keys.update(index.get(set_id, ()))
        return sorted(keys)

    def state(self, tid, points):
        """(status, mode, gen, point) of the target tid (a valid ID); points
        is the points list of the project."""
        set_id, part, seq = stakeout.parse_tid(tid)
        return self._derive(set_id, stakeout.target_key(part, seq), self._index_of(points))

    def counts(self, set_id, points):
        counts = {"confirmed": 0, "verified": 0, "manual": 0, "skipped": 0}
        index = self._index_of(points)
        for key in self._keys(set_id, index):
            status, mode = self._derive(set_id, key, index)[:2]
            if status != "open":
                counts[mode or status] += 1
        counts["confirmed"] = counts["verified"] + counts["manual"]
        return counts

    def rows(self, set_ids, points):
        """(tid, status, gen, mode, point_id) of every target of set_ids that
        is not open at its set's effective floor, by set and target key."""
        index = self._index_of(points)
        for set_id in set_ids:
            floor = self.floor_of(set_id)
            for key in self._keys(set_id, index):
                status, mode, gen, point = self._derive(set_id, key, index)
                if status != "open" or gen != floor:
                    yield (_tid(set_id, key), status, gen, mode, point and point["id"])

    def floor_for(self, set_id, points):
        """The floor a removal of set_id writes: 1 + the highest generation
        of its targets (entries, points, at least the effective floor)."""
        top = self.floor_of(set_id)
        for value in self.entries.get(set_id, {}).values():
            top = max(top, value >> 1)
        for point in self._index_of(points).get(set_id, {}).values():
            top = max(top, point["stakeout_gen"])
        return top + 1

    def next_floor(self, points):
        """The floor a survey restore writes (E26): 1 + the highest generation
        the project knows (project floor, set floors, entries, valid points)."""
        sets = set(self.floors)
        sets.update(self.entries)
        sets.update(self._index_of(points))
        return max([self.base + 1] + [self.floor_for(set_id, points) for set_id in sets])

    def stub(self):
        """A fold with only what an append needs (project, fingerprint, size, torn)."""
        stub = Fold(self.project_id, self.fingerprint)
        stub.size, stub.torn = self.size, self.torn
        return stub

    def orders(self):
        """{set_id: [reversed part, ...]} for the session (set_order)."""
        return {set_id: sorted(parts) for set_id, parts in self._orders.items()}


def latch(store, why):
    """A stakeout write's outcome is unknown: set store.uncertain, log it
    and return the 503 stakeout_write_uncertain to raise."""
    store.uncertain = why
    log("ERROR", "STAKE", "write uncertain (%s); stakeout changes are locked until "
        "restart" % why)
    return stakeout.write_uncertain_error()


class ProgressLog:
    """The progress logs of all projects in the folders of store.

    load and append run under the store's change lock; read is a reader's
    load (E21). Only the fold of the active project is cached; forget and
    forget_all raise generation, which invalidates every fold loaded before.
    """

    def __init__(self, store):
        self.store = store
        self.generation = 0
        self._fold = None
        # {project_id: log size} of compactions that failed since the start.
        self._declined = {}
        # {"before", "after", "ms", "longest_block_ms"} of the last compaction.
        self.last_compaction = None
        # The count of unowned_folders; None until counted (again after forget_all).
        self.unowned = None

    def path(self, project_id):
        return "%s/%s" % (self.store.project_path(project_id), PROGRESS_FILE)

    def valid(self, fold):
        return fold.generation == self.generation

    def peek(self, project_id, fingerprint):
        """The cached, valid fold of project_id with fingerprint, else None."""
        fold = self._fold
        if (fold is not None and fold.project_id == project_id
                and fold.fingerprint == fingerprint and self.valid(fold)):
            return fold
        return None

    async def load(self, project_id, fingerprint, active, capacity=None):
        """The fold of project_id (call under the change lock): cached, else
        read; an active project's fold is cached when no forget happened
        meanwhile, any other is transient. A grown log is compacted first
        (_compact; capacity as there), then read afresh. Notes the removed
        sets in the store."""
        if not self.store.busy:
            raise RuntimeError("no change lock")
        fold = self.peek(project_id, fingerprint)
        if fold is None:
            fold = await self._read(project_id, fingerprint)
            if active and self.valid(fold):
                self._fold = fold
        if await self._compact(fold, capacity):
            fresh = await self._read(project_id, fingerprint)
            fresh.generation = fold.generation
            if self._fold is fold:
                self._fold = fresh
            fold = fresh
        self.store.note_removed(project_id, fold.removed)
        return fold

    async def read(self, project_id, fingerprint, active, capacity=None):
        """A reader's fold: the cached one, else a load under the lock taken
        for reading; 503 stakeout_busy while a change holds the lock."""
        fold = self.peek(project_id, fingerprint)
        if fold is not None:
            return fold
        if self.store.busy:
            raise stakeout.StakeoutError("stakeout_busy", "Stakeout data is changing; try "
                                         "again.", "503 Service Unavailable")
        async with self.store.mutation(False):
            return await self.load(project_id, fingerprint, active, capacity)

    async def _read(self, project_id, fingerprint):
        fold = Fold(project_id, fingerprint, self.generation)
        path = self.path(project_id)
        meter = stakeout._BlockMeter()
        stakeout._discard(path + ".tmp")
        await stakeout._pause(meter)
        try:
            handle = open(path, "rb")
        except OSError as error:
            if stakeout._errno(error) == stakeout._ENOENT:
                return fold
            raise
        pacer = stakeout._Pacer(meter)
        # rest: the line read so far, None while dropping a line beyond LINE_MAX.
        rest = b""
        headed = foreign = False
        try:
            while not foreign:
                chunk = handle.read(LINE_MAX)
                if not chunk:
                    break
                end = fold.size
                fold.size += len(chunk)
                pieces = chunk.split(b"\n")
                for piece in pieces[:-1]:
                    end += len(piece) + 1
                    foreign = not _line(fold, None if rest is None else rest + piece, headed,
                                        end)
                    if foreign:
                        break
                    headed = True
                    rest = b""
                    if pacer.due("targets"):
                        await pacer.pause()
                if rest is not None:
                    rest += pieces[-1]
                    if len(rest) > LINE_MAX:
                        rest = None
                await stakeout._pause(meter)
            if rest != b"" and not foreign:
                fold.torn = True
                foreign = not _line(fold, rest, headed)
        finally:
            handle.close()
        if foreign:
            # rename replaces an older .foreign (LittleFS and POSIX).
            os.rename(path, path + ".foreign")
            await stakeout._pause(meter)
            log("WARN", "STAKE", "Foreign progress of %s set aside." % project_id)
            return Fold(project_id, fingerprint, fold.generation)
        if fold.damaged_lines:
            log("WARN", "STAKE", "%d damaged progress lines of %s skipped."
                % (fold.damaged_lines, project_id))
        return fold

    async def _compact(self, fold, capacity):
        """Replace the log of fold by a snapshot of the fold (E7); True when
        it did. Only from COMPACT_BYTES and twice the size kept by the last
        compaction on, never under the latch, and only when capacity(size +
        COMPACT_BYTES) is true. The snapshot goes to progress.jsonl.tmp in
        pieces of at most LINE_MAX bytes under a SHA-256, is flushed, closed
        and read back against the digest, then renamed over the log; one
        flash step or read piece per block. Old and new log fold the same,
        so a failure only removes the .tmp, warns and keeps the old log;
        until the log doubles, it is not tried again before a restart. The
        kept size is read from the log (Fold.base_size), so neither a
        restart nor a transient fold compacts a snapshot again."""
        before = fold.size
        kept = max(fold.base_size, self._declined.get(fold.project_id, 0))
        if self.store.uncertain is not None or before < max(COMPACT_BYTES, 2 * kept):
            return False
        self._declined[fold.project_id] = before
        why = "no reserve"
        if capacity is not None and capacity(before + COMPACT_BYTES):
            path = self.path(fold.project_id)
            tmp = path + ".tmp"
            meter = stakeout._BlockMeter()
            started = time.ticks_ms()
            try:
                after = await _write_snapshot(fold, tmp, meter)
                os.rename(tmp, path)
                await stakeout._pause(meter)
                self.last_compaction = {"before": before, "after": after,
                                        "ms": time.ticks_diff(time.ticks_ms(), started),
                                        "longest_block_ms": meter.lap()}
                log("INFO", "STAKE", "Progress of %s compacted: %d -> %d B."
                    % (fold.project_id, before, after))
                del self._declined[fold.project_id]
                return True
            except Exception as error:
                why = error
            await stakeout._pause(meter)
            stakeout._discard(tmp)
            await stakeout._pause(meter)
        log("WARN", "STAKE", "Progress of %s not compacted (%s)." % (fold.project_id, why))
        return False

    def append(self, fold, event, capacity=None, charge=None, reserve=True):
        """Append event to the log of fold in one open/write/flush/close (the
        header with it for a new file), then fold it. With reserve, capacity
        (if given) must allow the bytes plus 4096 (507 storage_reserve);
        charge (if given) gets the bytes written. An exception once the file
        is opened sets the latch: 503 stakeout_write_uncertain."""
        data = _dump(event)
        if fold.torn:
            data = "\n" + data
        if not fold.size:
            data = _header(fold) + data
        data = data.encode()
        if reserve and capacity is not None and not capacity(len(data) + LINE_MAX):
            raise reserve_error()
        try:
            handle = open(self.path(fold.project_id), "ab")
            try:
                handle.write(data)
                handle.flush()
            finally:
                handle.close()
        except Exception as error:
            raise latch(self.store, "progress %s %s: %s" % (fold.project_id, event.get("ev"),
                                                            error))
        fold.size += len(data)
        fold.torn = False
        fold.apply(event)
        if charge is not None:
            charge(len(data))

    async def restore_floors(self, projects):
        """[(stub, floor)] for each of projects [(project_id, fingerprint,
        points)] whose folder holds stakeout data (store.has_data): the stub
        and next_floor of its fold, read transiently; a pause per project.
        Call under the change lock, before a survey restore (E26)."""
        floors = []
        meter = stakeout._BlockMeter()
        for project_id, fingerprint, points in projects:
            if self.store.has_data(project_id):
                fold = await self._read(project_id, fingerprint)
                floors.append((fold.stub(), fold.next_floor(points)))
            await stakeout._pause(meter)
        return floors

    def floor_steps(self, floors, charge=None):
        """Append restored {floor} to each (stub, floor) of restore_floors, one
        per step, without a reserve check (a restore must work on a full
        flash); charge as in append."""
        for stub, floor in floors:
            self.append(stub, restored_event(floor), reserve=False, charge=charge)
            yield None

    def forget(self, project_id):
        """Invalidate every transient fold and the cached one of project_id."""
        fold = self._fold
        self.forget_all()
        if fold is not None and fold.project_id != project_id:
            fold.generation = self.generation
            self._fold = fold

    def forget_all(self):
        self._fold = None
        self.unowned = None
        self.generation += 1


def reserve_error():
    """The 507 storage_reserve of a progress write the flash reserve refuses."""
    return stakeout.StakeoutError("storage_reserve", "Not enough free flash for the stakeout "
                                  "progress.", "507 Insufficient Storage")


def unowned_folders(store, tracker):
    """How many folders in the root of store are named like a project that is
    neither live nor archived (E13): kept and counted, never removed. One
    os.listdir, cached until progress.forget_all()."""
    count = progress.unowned
    if count is None:
        known = set(project["id"] for project in tracker.projects)
        known.update(archive.get("project_id") for archive in tracker.archives)
        try:
            names = os.listdir(store.root)
        except OSError:
            names = ()
        count = 0
        for name in names:
            if stakeout.valid_project_id(name) and name not in known:
                count += 1
        progress.unowned = count
        if count:
            log("INFO", "STAKE", "Stakeout folders without a project: %d." % count)
    return count


def _dump(event):
    return ujson.dumps(event) + "\n"


def _header(fold):
    return _dump({"kind": "stakeout_progress", "schema": 1, "project_id": fold.project_id,
                  "project_created_at": fold.fingerprint})


def _tid(set_id, key):
    return "%s/%d/%d" % (set_id, key >> stakeout.SEQ_BITS, key & _SEQ_MASK)


def _snapshot(fold):
    """The lines of a log that folds like fold, header first: the project
    floor, every set removal, then each target that differs from open at
    its set's floor, the active target and the reversed parts (E7)."""
    yield _header(fold)
    if fold.base:
        yield _dump({"ev": "restored", "floor": fold.base})
    for set_id, floor in fold.floors.items():
        yield _dump({"ev": "set_removed", "set_id": set_id,
                     "imported_at": fold.removed.get(set_id), "floor": floor})
    for set_id, entries in fold.entries.items():
        floor = fold.floor_of(set_id)
        for key, value in entries.items():
            gen = value >> 1
            if value & 1 and gen >= floor:
                yield _dump({"ev": "skip", "tid": _tid(set_id, key), "gen": gen})
            elif gen > floor:
                yield _dump({"ev": "reopen", "tid": _tid(set_id, key), "gen": gen})
    if fold.active_tid:
        yield _dump({"ev": "active", "tid": fold.active_tid})
    for set_id, parts in fold._orders.items():
        for part in parts:
            yield _dump({"ev": "order", "part_id": "%s/%d" % (set_id, part), "reversed": True})


async def _write_snapshot(fold, tmp, meter):
    """Write the snapshot of fold to tmp and read it back; returns its size.
    Raises when it does not read back intact."""
    digest = stakeout.hashlib.sha256()
    size = 0
    piece = b""
    handle = open(tmp, "wb")
    try:
        await stakeout._pause(meter)
        for line in _snapshot(fold):
            line = line.encode()
            if len(piece) + len(line) > LINE_MAX:
                handle.write(piece)
                digest.update(piece)
                size += len(piece)
                piece = b""
                await stakeout._pause(meter)
            piece += line
        handle.write(piece)
        digest.update(piece)
        size += len(piece)
        piece = None
        await stakeout._pause(meter)
        handle.flush()
        await stakeout._pause(meter)
    finally:
        handle.close()
    await stakeout._pause(meter)
    expected = digest.digest()
    digest = stakeout.hashlib.sha256()
    count = 0
    with open(tmp, "rb") as handle:
        while True:
            block = handle.read(LINE_MAX)
            if not block:
                break
            digest.update(block)
            count += len(block)
            await stakeout._pause(meter)
    if count != size or digest.digest() != expected:
        raise ValueError("snapshot did not read back intact")
    return size


def _line(fold, line, headed, end=None):
    """Fold one line (None: one beyond LINE_MAX); before the header, check
    it instead. False when the header does not belong to fold. end is the
    file offset after the line's break (None for a torn last line): the
    header and each following valid line without a time extend
    fold.base_size to it."""
    event = None
    if line is not None and len(line) <= LINE_MAX:
        try:
            event = ujson.loads(line)
        except ValueError:
            pass
    if not isinstance(event, dict):
        event = {}
    if not headed:
        if (event.get("kind") != "stakeout_progress" or event.get("schema") != 1
                or event.get("project_id") != fold.project_id
                or event.get("project_created_at") != fold.fingerprint):
            return False
        if end is not None:
            fold.base_size = end
        return True
    if not fold.apply(event):
        fold.damaged_lines += 1
    elif "t" not in event and end is not None and fold.base_size == end - len(line) - 1:
        fold.base_size = end
    return True


# --- decisions and answers (E20, E11) -----------------------------------------

def _refused(code, message, status, mode, gen):
    return stakeout.StakeoutError(code, message, "409 Conflict",
                                  extra={"status": status, "mode": mode, "gen": gen})


def decide(fold, action, tid, expect_gen, mode, points):
    """The outcome (status, mode, gen, point, repeated) of action ("confirm"
    with mode, "skip" or "reopen") on tid expecting generation expect_gen;
    raises 409 target_changed, target_not_open or target_open. repeated
    False means the caller writes the outcome: a point at gen (confirm),
    skip at gen or reopen at gen."""
    status, held, gen, point = fold.state(tid, points)
    if action == "reopen":
        if gen > expect_gen and status == "open":
            return status, None, gen, None, True
        if gen != expect_gen:
            raise _refused("target_changed", "The target changed meanwhile.", status, held, gen)
        if status == "open":
            raise _refused("target_open", "The target is open already.", status, held, gen)
        return "open", None, gen + 1, None, False
    if gen != expect_gen:
        raise _refused("target_changed", "The target changed meanwhile.", status, held, gen)
    if action == "skip":
        if status == "skipped":
            return status, None, gen, None, True
        if status == "open":
            return "skipped", None, gen, None, False
    elif status == "confirmed" and held == mode:
        return status, held, gen, point, True
    elif status == "open":
        return "confirmed", mode, gen, None, False
    raise _refused("target_not_open", "The target is not open.", status, held, gen)


def answer(tid, status, mode, gen, point, repeated):
    """The answer of a stakeout action; the offsets come from point (or null)."""
    point = point or {}
    return {"persisted": True, "repeated": repeated, "tid": tid, "gen": gen,
            "status": status, "mode": mode, "point_id": point.get("id"),
            "dn_m": point.get("stakeout_dn_m"), "de_m": point.get("stakeout_de_m"),
            "dist_m": point.get("stakeout_dist_m"),
            "tolerance_m": point.get("stakeout_tolerance_m")}


def _event(ev, fields):
    fields["t"] = stakeout.now_seconds()
    fields["ev"] = ev
    return fields


def skip_event(tid, gen, cmd):
    return _event("skip", {"tid": tid, "gen": gen, "cmd": cmd})


def reopen_event(tid, gen, cmd=None, by=None):
    event = _event("reopen", {"tid": tid, "gen": gen})
    if cmd is not None:
        event["cmd"] = cmd
    if by is not None:
        event["by"] = by
    return event


def active_event(tid):
    return _event("active", {"tid": tid})


def order_event(part_id, reversed):
    return _event("order", {"part_id": part_id, "reversed": reversed})


def set_removed_event(set_id, imported_at, floor):
    return _event("set_removed", {"set_id": set_id, "imported_at": imported_at,
                                  "floor": floor})


def restored_event(floor):
    return _event("restored", {"floor": floor})


progress = ProgressLog(stakeout.store)
