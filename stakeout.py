# SPDX-License-Identifier: AGPL-3.0-only
"""Stakeout targets: import package validation, set files and IDs.

The browser parses GeoJSON and sends a normalized import package whose
coordinates are integers in 1e-8 degrees (e8) and whose altitudes are integer
millimetres; the device never parses raw GeoJSON, so no coordinate passes
through a Float32. validate_import checks the package completely again and
builds the stored set; a stored set is never parsed again on the device (its
trailer summary is checked instead). Errors name the offending field with a path such as
``targets[12].lat_e8``, ``parts[0].coords_e8[3][1]`` or
``source.skipped[0].reason``; the empty path means the payload itself.

This module knows neither tracking nor web: project checks and the flash
reserve are passed in by the caller.

TargetStore keeps each set in its own file and lists, reads, adds and
deletes sets per project.
"""
import gc
import os
import time
import ujson
import uasyncio as asyncio
try:
    import hashlib
except ImportError:  # MicroPython
    import uhashlib as hashlib
import geo
from cfg import log

SCHEMA = 1
# The folder at the flash root that holds the sets. Its name must never be a
# module name: MicroPython imports a directory "<name>" (even without
# __init__.py, as an empty namespace package) before "<name>.py", so a folder
# "stakeout" would replace this module after the next restart. A dot cannot
# occur in a module name (like "tracking.archives"); main.py renames a folder
# left by an older firmware before anything imports stakeout.
DEFAULT_ROOT = "stakeout.sets"
MAX_SETS = 4
MAX_TARGETS_PER_SET = 500
MAX_PARTS_PER_SET = 500
MAX_COORDS_PER_SET = 2000
MAX_COORDS_TOTAL = 1000000
# A target's seq is coord_index + 1 on a line of up to coords_total vertices
# (thinned lines keep only MAX_COORDS_PER_SET of them), so it reaches
# MAX_COORDS_TOTAL; target_key packs (part, seq) below 2**30 (a small int).
MAX_TARGET_SEQ = MAX_COORDS_TOTAL
SEQ_BITS = 20
MAX_PROPS = 8
MAX_PROP_KEY = 40
MAX_PROP_VALUE = 80
MAX_NAME = 80
MAX_FILENAME = 120
MAX_TYPE = 40
MAX_SKIPPED = 50
MAX_FEATURES = 5000
MAX_SOURCE_BYTES = 4194304
MAX_PROJECT_ID = 64
MAX_SET_FILE_BYTES = 393216
# Set file layout 2 (the API SCHEMA stays 1): the body (the head, one line per
# part and per target) and a trailer of exactly TRAILER_BYTES that carries the
# summary a listing reads (_store_set_file, _read_trailer).
FILE_LAYOUT = 2
TRAILER_BYTES = 4096
# Every target line is shorter (in both encodings, pinned by a test), so one
# target can be found by scanning lines from targets_offset in such pieces.
TARGET_LINE_MAX = 16384
# find_target reads a set file in blocks of this many bytes, a pause after each.
SCAN_PIECE = 2048
# The largest import package body, the same bound the HTTP route enforces
# (web_routing.STAKEOUT_IMPORT_LIMIT): the parse alone of a 256 KiB package
# exceeds the device's block budget, so a larger one is refused unparsed.
MAX_IMPORT_BYTES = 131072
PART_KINDS = ("point", "multipoint", "line")
SKIP_REASONS = ("unsupported_geometry", "null_geometry", "empty_coordinates")
# Long local work yields once it has run SLICE_MS since the operation's last
# yield (_Pacer), looked at for every target, part head and skip entry and
# every COORD_STEP vertices; these counts (part heads and skip entries count
# as targets) are the backstop for a clock that does not advance (a coarse or
# stopped tick counter) and for work that is slower to check than to do.
SLICE_MS = 50
YIELD_TARGETS = 32
YIELD_COORDS = 128
# Vertices are counted this many at a time before the pacer looks at the clock.
COORD_STEP = 16
# A service pause sleeps this long, so other tasks really run (the actual
# suspension is measured, not assumed).
SERVICE_PAUSE_MS = 1
# The commit writes and reads back the set file in pieces of this many bytes
# with a yield after each (F1a/F1b).
WRITE_PIECE = 4096
# The import answer's timing_ms: constant-size, whole milliseconds, no
# per-record logs.
#   parse, validate, reserve, write, verify, rename: the wall time of the
#     phase (yields included); size is always 0 (the file is serialized
#     once, while it is written, and no longer measured first).
#   longest_block: the longest stretch without a yield, from the parse to the
#     commit; the other tasks' time is excluded, all contiguous local work
#     (statvfs, gc, serializing, writing) is in.
#   gc: the timed gc.collect() before the parse, a block of its own (0 while
#     none is made).
#   validate/write/verify _work and _wait: that phase's wall time split into
#     local work and time suspended in service pauses.
#   pauses, max_pause: how many service pauses the import made and the
#     longest actual suspension of one.
#   max_serialize, max_read, max_write, max_flush, max_close, max_dir,
#     max_capacity: the slowest single serialization step (one part, target
#     or the trailer), file read (a trailer or a read-back block), file
#     write, flush, close, directory operation (listing, folder, rename) and
#     flash-capacity probe.
#   capacity_refresh: the charge of the committed file against the flash
#     sample (charge, no statvfs), right after the commit.
TIMING_KEYS = ("parse", "validate", "reserve", "size", "write", "verify",
               "rename", "longest_block", "capacity_refresh", "gc",
               "validate_work", "validate_wait", "write_work", "write_wait",
               "verify_work", "verify_wait", "pauses", "max_pause",
               "max_serialize", "max_read", "max_write", "max_flush",
               "max_close", "max_dir", "max_capacity")
_LARGEST = ("serialize", "read", "write", "flush", "close", "dir", "capacity")
_BACKSTOP = {"targets": YIELD_TARGETS, "coords": YIELD_COORDS}

_EPOCH_OFFSET = 946684800 if time.gmtime(0)[0] == 1970 else 0
_INVALID = "invalid_stakeout_import"
_OUT_OF_RANGE = "coordinate_out_of_range"
_TOO_MANY = "too_many_targets"
_LAT_LIMIT = 90 * geo.COORD_SCALE
_LON_LIMIT = 180 * geo.COORD_SCALE
_ALT_LOW = -20000 * geo.ALT_SCALE
_ALT_HIGH = 100000 * geo.ALT_SCALE
_ID_PUNCTUATION = "-_"
_HEX = "0123456789abcdef"
_UINT32 = 4294967295
_TRAILER_HEAD = b',\n"summary": '
_TRAILER_END = b"}\n"
# The integer members of a summary and of its source, with their ranges.
# coords counts stored vertices, 0 for a set of points and multipoints.
_SUMMARY_INTS = (("layout", FILE_LAYOUT, FILE_LAYOUT), ("imported_at", 0, _UINT32),
                 ("parts", 1, MAX_PARTS_PER_SET), ("targets", 1, MAX_TARGETS_PER_SET),
                 ("coords", 0, MAX_COORDS_PER_SET),
                 ("body_bytes", 1, MAX_SET_FILE_BYTES - TRAILER_BYTES))
_SOURCE_INTS = (("bytes", 0, MAX_SOURCE_BYTES), ("features", 0, MAX_FEATURES),
                ("skipped_total", 0, MAX_FEATURES))
_ENOENT = 2  # errno of a missing file, on MicroPython and POSIX hosts
_EEXIST = 17  # errno of an existing directory, likewise
_DIRECTORY = 0x4000  # the entry type os.ilistdir gives a directory
_BAD_REQUEST = "400 Bad Request"
_CONFLICT = "409 Conflict"
_NOT_FOUND = "404 Not Found"
_UNAVAILABLE = "503 Service Unavailable"
_NO_STORAGE = "507 Insufficient Storage"
# Distinguishes a missing key from an explicit null.
_MISSING = object()


class StakeoutError(ValueError):
    """A refused stakeout request: API error code, message and HTTP status.

    path names the offending package field (see the module docstring);
    set_id names the set a conflict refers to. extra (a dict) adds members
    such as status, gen or reasons; it never replaces one named above.
    """

    def __init__(self, code, message, status="400 Bad Request", path=None,
                 set_id=None, extra=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.path = path
        self.set_id = set_id
        self.extra = extra

    def payload(self):
        body = {"error": self.code, "message": self.message}
        if self.path is not None:
            body["path"] = self.path
        if self.set_id is not None:
            body["set_id"] = self.set_id
        for key, value in (self.extra or {}).items():
            body.setdefault(key, value)
        return body


def write_uncertain_error():
    """503 stakeout_write_uncertain: a stakeout write may or may not have
    reached the flash (TargetStore.uncertain)."""
    return StakeoutError("stakeout_write_uncertain", "A stakeout change may not be "
                         "stored; restart the device.", "503 Service Unavailable")


def now_seconds():
    """The current time in seconds since 2000-01-01T00:00:00Z."""
    return int(time.time()) - _EPOCH_OFFSET


def valid_project_id(value):
    """True for 1..64 characters from A-Z, a-z, 0-9, "-" and "_"."""
    if not isinstance(value, str) or not 0 < len(value) <= MAX_PROJECT_ID:
        return False
    for char in value:
        if not ("a" <= char <= "z" or "A" <= char <= "Z" or "0" <= char <= "9"
                or char in _ID_PUNCTUATION):
            return False
    return True


def _lower_hex(text):
    """True when every character of text is a lowercase hex digit."""
    for char in text:
        if char not in _HEX:
            return False
    return True


def _hex(data):
    """data (bytes) as lowercase hex digits."""
    return "".join("%02x" % byte for byte in data)


def valid_set_id(value):
    """True for "so-" followed by exactly 8 lowercase hex digits."""
    return (isinstance(value, str) and len(value) == 11 and value[:3] == "so-"
            and _lower_hex(value[3:]))


def parse_tid(tid):
    """(set_id, part, seq) of a target ID "so-xxxxxxxx/<part>/<seq>", else None.

    part is 1..MAX_PARTS_PER_SET and seq 1..MAX_TARGET_SEQ, written as the
    set file writes them (no sign, no leading zero). seq is coord_index + 1 of
    the target on its line, and coord_index stays below the part's
    coords_total <= MAX_COORDS_TOTAL, so a target on a thinned line can lie
    far beyond the MAX_COORDS_PER_SET vertices a set stores.
    """
    try:
        set_id, part, seq = tid.split("/")
        part, seq = int(part), int(seq)
    except (AttributeError, TypeError, ValueError):
        return None
    if (valid_set_id(set_id) and 0 < part <= MAX_PARTS_PER_SET
            and 0 < seq <= MAX_TARGET_SEQ and "%s/%d/%d" % (set_id, part, seq) == tid):
        return set_id, part, seq
    return None


def target_key(part, seq):
    """An int key of a target within its set: (part, seq) packed into SEQ_BITS."""
    return part << SEQ_BITS | seq


def set_id_for(rows):
    """The content-derived set ID of (part_index, coord_index, lat_e8, lon_e8) rows.

    "so-" plus the first 8 hex digits of the SHA-256 over the UTF-8 lines
    "%d,%d,%d,%d\\n", sorted by (part_index, coord_index). Names, properties
    and the source never enter it, so the same selection gets the same ID.
    """
    digest = hashlib.sha256()
    for row in sorted(rows):
        digest.update(("%d,%d,%d,%d\n" % row).encode("utf-8"))
    return "so-" + _hex(digest.digest()[:4])


class _BlockMeter:
    """What one operation spends locally and suspended, from its creation on.

    One meter per operation, passed to every helper that yields; operations
    that overlap in time never share one. Constant size: a few counters.
    """

    def __init__(self):
        self.longest = 0
        self.since = time.ticks_ms()
        self.pauses = 0
        self.max_pause = 0
        self.suspended = 0
        self.largest = {kind: 0 for kind in _LARGEST}

    def lap(self):
        """End the running stretch now; returns the longest one so far."""
        self.longest = max(self.longest, time.ticks_diff(time.ticks_ms(), self.since))
        return self.longest

    def suspend(self):
        """End the running stretch before a yield; returns the tick it ended."""
        now = time.ticks_ms()
        self.longest = max(self.longest, time.ticks_diff(now, self.since))
        return now

    def resume(self, suspended_at):
        """Start a stretch after a yield that began at suspended_at."""
        now = time.ticks_ms()
        waited = time.ticks_diff(now, suspended_at)
        self.pauses += 1
        self.suspended += waited
        self.max_pause = max(self.max_pause, waited)
        self.since = now

    def note(self, kind, started):
        """Record an operation of kind that began at started (_LARGEST)."""
        self.record(kind, time.ticks_diff(time.ticks_ms(), started))

    def record(self, kind, spent):
        """Record an operation of kind that took spent ms (_LARGEST)."""
        if spent > self.largest[kind]:
            self.largest[kind] = spent

    def publish(self, timing):
        """Write the totals into timing (the key set is part of TIMING_KEYS)."""
        timing["longest_block"] = self.lap()
        timing["pauses"] = self.pauses
        timing["max_pause"] = self.max_pause
        for kind in _LARGEST:
            timing["max_" + kind] = self.largest[kind]


async def _pause(meter):
    """Yield to the other tasks; meter (the caller's own) sees it end a stretch."""
    suspended_at = meter.suspend()
    await asyncio.sleep_ms(SERVICE_PAUSE_MS)
    meter.resume(suspended_at)


class _Pacer:
    """Tells a long loop when to yield: SLICE_MS of local work, or the backstop.

    The work done is the time since the meter's last resume. due() counts
    units of a kind (targets or coords) and is True once the count reaches
    the kind's backstop or the slice is used up; pause() then yields and
    starts the counts over. A caller that does not pace item by item passes
    the units since its last call and calls every that many items.
    """

    def __init__(self, meter):
        self.meter = meter
        self.counts = {"targets": 0, "coords": 0}

    def due(self, kind, units=1):
        count = self.counts[kind] + units
        self.counts[kind] = count
        if count >= _BACKSTOP[kind]:
            return True
        return time.ticks_diff(time.ticks_ms(), self.meter.since) >= SLICE_MS

    async def pause(self):
        await _pause(self.meter)
        self.counts["targets"] = 0
        self.counts["coords"] = 0


def _timed_collect(timing):
    """gc.collect(), its duration in timing["gc"]; it is local work like any other."""
    started = time.ticks_ms()
    gc.collect()
    timing["gc"] = time.ticks_diff(time.ticks_ms(), started)


# --- Field checkers (validate_import) --------------------------------------

def _invalid(path, message):
    return StakeoutError(_INVALID, message, path=path)


def _int(value, low, high, path, code=_INVALID):
    """value as an int in [low, high]; a bool or float is a type error."""
    if type(value) is not int:
        raise _invalid(path, "%s must be an integer." % path)
    if value < low or value > high:
        raise StakeoutError(code, "%s is out of range." % path, path=path)
    return value


def _controlled(text):
    """True when text holds a control character (below 0x20, or DEL).

    The same answer as cfg_validation.has_control_characters, with one ord()
    per character and no generator: a 500-target import checks up to 8500
    texts.
    """
    for char in text:
        code = ord(char)
        if code < 0x20 or code == 0x7F:
            return True
    return False


def _text(value, minimum, maximum, path):
    """value stripped, without control characters, minimum..maximum characters."""
    if not isinstance(value, str):
        raise _invalid(path, "%s must be text." % path)
    value = value.strip()
    if _controlled(value) or not minimum <= len(value) <= maximum:
        raise _invalid(path, "%s must be %d to %d characters without control "
                             "characters." % (path, minimum, maximum))
    return value


def _object(value, path):
    if not isinstance(value, dict):
        raise _invalid(path, "%s must be an object." % path)
    return value


def _list(value, maximum, path, minimum=1):
    """value as a list of minimum..maximum items; more is too_many_targets."""
    if not isinstance(value, list):
        raise _invalid(path, "%s must be a list." % path)
    if len(value) > maximum:
        raise StakeoutError(_TOO_MANY, "%s has more than %d entries." % (path, maximum),
                            path=path)
    if len(value) < minimum:
        raise _invalid(path, "%s must not be empty." % path)
    return value


def _altitude(value, path):
    if value is None:
        return None
    return _int(value, _ALT_LOW, _ALT_HIGH, path, _OUT_OF_RANGE)


def _coord(item, path):
    """One checked vertex [lon_e8, lat_e8, alt_mm | null] as a new list (the
    package is not kept)."""
    if not isinstance(item, list) or len(item) != 3:
        raise _invalid(path, "%s must be [lon_e8, lat_e8, alt_mm or null]." % path)
    _int(item[0], -_LON_LIMIT, _LON_LIMIT, path + "[0]", _OUT_OF_RANGE)
    _int(item[1], -_LAT_LIMIT, _LAT_LIMIT, path + "[1]", _OUT_OF_RANGE)
    _altitude(item[2], path + "[2]")
    return [item[0], item[1], item[2]]


def _props(value, path):
    """Up to 8 text properties; values are text or null, never numbers."""
    if not isinstance(value, dict) or len(value) > MAX_PROPS:
        raise _invalid(path, "%s must be an object with at most %d entries."
                       % (path, MAX_PROPS))
    props = {}
    for key, item in value.items():
        name = _text(key, 1, MAX_PROP_KEY, path)
        if name in props:
            raise _invalid(path, "%s repeats the key %s." % (path, name))
        props[name] = None if item is None else _text(
            item, 0, MAX_PROP_VALUE, path + "." + name)
    return props


def _source_head(value):
    """The checked source summary without its skipped entries, and the raw list."""
    source = _object(value, "source")
    filename = _text(source.get("filename", _MISSING), 0, MAX_FILENAME, "source.filename")
    size = _int(source.get("bytes", _MISSING), 0, MAX_SOURCE_BYTES, "source.bytes")
    features = _int(source.get("features", _MISSING), 0, MAX_FEATURES, "source.features")
    raw = source.get("skipped", _MISSING)
    if not isinstance(raw, list) or len(raw) > MAX_SKIPPED:
        raise _invalid("source.skipped", "source.skipped must be a list of at most %d "
                                         "entries." % MAX_SKIPPED)
    return {"filename": filename, "bytes": size, "features": features,
            "skipped": []}, raw


def _skipped(entry, index):
    """One checked skip entry."""
    path = "source.skipped[%d]" % index
    entry = _object(entry, path)
    kind = entry.get("type", _MISSING)
    reason = entry.get("reason", _MISSING)
    return {"feature_index": _int(entry.get("feature_index", _MISSING), 0,
                                  MAX_FEATURES - 1, path + ".feature_index"),
            "type": None if kind is None else _text(kind, 1, MAX_TYPE, path + ".type"),
            "reason": _choice(reason, SKIP_REASONS, path + ".reason")}


def _source_total(source, value):
    """Complete source (head plus its checked skipped entries); skipped is
    capped, skipped_total counts all."""
    source["skipped_total"] = _int(value.get("skipped_total", _MISSING),
                                   len(source["skipped"]), source["features"],
                                   "source.skipped_total")
    return source


def _choice(value, choices, path):
    if not isinstance(value, str) or value not in choices:
        raise _invalid(path, "%s must be one of %s." % (path, ", ".join(choices)))
    return value


def _part_head(value, index):
    """A part without its vertices, and the raw vertex list (still unchecked).

    Points and multipoints store no vertices; a line stores 2..coords_total
    of them, thinned exactly when fewer than coords_total.
    """
    path = "parts[%d]" % index
    part = _object(value, path)
    kind = _choice(part.get("kind", _MISSING), PART_KINDS, path + ".kind")
    name = _text(part.get("name", _MISSING), 1, MAX_NAME, path + ".name")
    feature_index = _int(part.get("feature_index", _MISSING), 0, MAX_FEATURES - 1,
                         path + ".feature_index")
    geometry_index = _int(part.get("geometry_index", _MISSING), 0, MAX_FEATURES - 1,
                          path + ".geometry_index")
    low, high = {"point": (1, 1), "multipoint": (1, MAX_COORDS_TOTAL),
                 "line": (2, MAX_COORDS_TOTAL)}[kind]
    total = _int(part.get("coords_total", _MISSING), low, high, path + ".coords_total")
    raw = part.get("coords_e8", _MISSING)
    thinned = part.get("coords_thinned", _MISSING)
    if kind == "line":
        if not isinstance(raw, list) or not 2 <= len(raw) <= total:
            raise _invalid(path + ".coords_e8", "%s.coords_e8 must hold 2 to %d "
                                                "vertices." % (path, total))
        expected = len(raw) < total
    else:
        if raw != []:
            raise _invalid(path + ".coords_e8", "%s.coords_e8 must be empty for a %s."
                           % (path, kind))
        expected = False
    if type(thinned) is not bool or thinned is not expected:
        raise _invalid(path + ".coords_thinned", "%s.coords_thinned must be %s."
                       % (path, "true" if expected else "false"))
    return {"kind": kind, "name": name, "feature_index": feature_index,
            "geometry_index": geometry_index, "coords_e8": None,
            "coords_total": total, "coords_thinned": thinned}, raw


def _count_coords(used, part):
    used += len(part)
    if used > MAX_COORDS_PER_SET:
        raise StakeoutError(_TOO_MANY, "A set stores at most %d vertices."
                            % MAX_COORDS_PER_SET, path="parts")
    return used


def _target(target, index, part_index, parts, previous):
    """The checked fields of target index on parts[part_index].

    previous is the (part_index, coord_index) of the target before it; the
    pairs must rise strictly. On an unthinned line the target must be the
    stored vertex at its coord_index.
    """
    path = "targets[%d]" % index
    part = parts[part_index]
    coord_index = _int(target.get("coord_index", _MISSING), 0, part["coords_total"] - 1,
                       path + ".coord_index")
    if previous is not None and (part_index, coord_index) <= previous:
        raise _invalid(path, "%s must follow the target before it in part and "
                             "vertex order." % path)
    name = _text(target.get("name", _MISSING), 1, MAX_NAME, path + ".name")
    lat_e8 = _int(target.get("lat_e8", _MISSING), -_LAT_LIMIT, _LAT_LIMIT,
                  path + ".lat_e8", _OUT_OF_RANGE)
    lon_e8 = _int(target.get("lon_e8", _MISSING), -_LON_LIMIT, _LON_LIMIT,
                  path + ".lon_e8", _OUT_OF_RANGE)
    alt = _altitude(target.get("alt_import", _MISSING), path + ".alt_import")
    props = _props(target.get("props", _MISSING), path + ".props")
    if part["kind"] == "line" and not part["coords_thinned"]:
        vertex = part["coords_e8"][coord_index]
        for field, have, want in (("lat_e8", lat_e8, vertex[1]),
                                  ("lon_e8", lon_e8, vertex[0]),
                                  ("alt_import", alt, vertex[2])):
            if have != want:
                raise _invalid(path + "." + field, "%s.%s differs from vertex %d of "
                               "its line." % (path, field, coord_index))
    return {"name": name, "coord_index": coord_index, "lat_e8": lat_e8,
            "lon_e8": lon_e8, "alt_import": alt, "props": props}


def _check_every_part_used(used):
    for part_index, count in enumerate(used):
        if not count:
            raise _invalid("parts[%d]" % part_index, "parts[%d] has no target."
                           % part_index)


def _finish(set_id, parts, targets, rows):
    """Assign part_id, seq and tid; rows hold each target's part_index."""
    for part_index, part in enumerate(parts):
        part["part_id"] = "%s/%d" % (set_id, part_index + 1)
    for target, row in zip(targets, rows):
        part_id = parts[row[0]]["part_id"]
        seq = target["coord_index"] + 1
        target["tid"] = "%s/%d" % (part_id, seq)
        target["part_id"] = part_id
        target["seq"] = seq


async def validate_import(payload, meter=None):
    """Check an import package and build the set it describes (the plan).

    Returns {"project_id", "set_id", "source", "parts", "targets"} with the
    parts and targets in set-file form (part_id, tid, seq). Unknown keys are
    dropped. Raises StakeoutError naming the first offending field; checks
    run in package order. Yields to the event loop through a _Pacer on
    meter (the import's; a private one when None): after SLICE_MS of local
    work, looked at for every skip entry, part head and target and every
    COORD_STEP vertices, and at the latest after YIELD_TARGETS of those items
    or YIELD_COORDS vertices.
    """
    pacer = _Pacer(meter if meter is not None else _BlockMeter())
    package = _object(payload, "")
    _int(package.get("schema", _MISSING), SCHEMA, SCHEMA, "schema")
    project_id = package.get("project_id", _MISSING)
    if not valid_project_id(project_id):
        raise _invalid("project_id", "project_id must be 1 to %d characters from "
                                     "A-Z, a-z, 0-9, - and _." % MAX_PROJECT_ID)
    raw_source = package.get("source", _MISSING)
    source, raw = _source_head(raw_source)
    for index, entry in enumerate(raw):
        source["skipped"].append(_skipped(entry, index))
        if pacer.due("targets"):
            await pacer.pause()
    _source_total(source, raw_source)
    raw_parts = _list(package.get("parts", _MISSING), MAX_PARTS_PER_SET, "parts")
    parts = []
    coords_used = 0
    for part_index, value in enumerate(raw_parts):
        part, raw = _part_head(value, part_index)
        if pacer.due("targets"):
            await pacer.pause()
        checked = coords_used
        coords_used = _count_coords(coords_used, raw)
        coords = []
        for coord_index, item in enumerate(raw):
            coords.append(_coord(item, "parts[%d].coords_e8[%d]" % (part_index, coord_index)))
            checked += 1
            if checked % COORD_STEP == 0 and pacer.due("coords", COORD_STEP):
                await pacer.pause()
        part["coords_e8"] = coords
        parts.append(part)
    raw_targets = _list(package.get("targets", _MISSING), MAX_TARGETS_PER_SET, "targets")
    targets = []
    rows = []
    used = [0] * len(parts)
    previous = None
    for index, value in enumerate(raw_targets):
        path = "targets[%d]" % index
        value = _object(value, path)
        part_index = _int(value.get("part_index", _MISSING), 0, len(parts) - 1,
                          path + ".part_index")
        target = _target(value, index, part_index, parts, previous)
        previous = (part_index, target["coord_index"])
        used[part_index] += 1
        targets.append(target)
        rows.append((part_index, target["coord_index"], target["lat_e8"],
                     target["lon_e8"]))
        if pacer.due("targets"):
            await pacer.pause()
    _check_every_part_used(used)
    set_id = set_id_for(rows)
    _finish(set_id, parts, targets, rows)
    return {"project_id": project_id, "set_id": set_id, "source": source,
            "parts": parts, "targets": targets}


# --- Set files ------------------------------------------------------------

def _exists(path):
    try:
        os.stat(path)
        return True
    except OSError:
        return False


def _present(path):
    """Whether path exists; an error other than ENOENT propagates."""
    try:
        os.stat(path)
        return True
    except OSError as error:
        if _errno(error) != _ENOENT:
            raise
        return False


def _move(source, target):
    """Rename source to target: True when it moved (also when the rename
    raised after moving), False when it did not, None when that cannot be
    told."""
    try:
        os.rename(source, target)
        return True
    except OSError:
        pass
    try:
        if _present(source):
            return False
        return True if _present(target) else None
    except OSError:
        return None


def _discard(path):
    """Remove path if present; a file that stays is logged, not raised."""
    try:
        os.remove(path)
    except OSError as error:
        if not (error.args and error.args[0] == _ENOENT):
            log("WARN", "STAKE", "Could not remove %s (%s)." % (path, error))


def _lap(timing, key, started, meter=None, waited=0):
    """Record the milliseconds since started under key; return the new start.

    With meter, the phase's time is also split: key_wait is what the meter
    spent suspended since waited (its suspended total at the phase's start)
    and key_work the rest of the wall time.
    """
    now = time.ticks_ms()
    if timing is not None:
        wall = time.ticks_diff(now, started)
        timing[key] = wall
        if meter is not None:
            wait = meter.suspended - waited
            timing[key + "_wait"] = wait
            timing[key + "_work"] = wall - wait
    return now


def _write_failed(message):
    return StakeoutError("stakeout_write_failed", message, _NO_STORAGE)


def _already(set_id):
    return StakeoutError("already_imported", "This target set is already imported.",
                         _CONFLICT, set_id=set_id)


def _next_data(chunks, meter):
    """The next piece of _body_chunks (UTF-8 bytes), None at the end.

    meter sees the duration of the step. Not a generator, so no frame keeps
    the previous piece alive while the caller works on it.
    """
    started = time.ticks_ms()
    data = next(chunks, None)
    if data is not None:
        meter.note("serialize", started)
    return data


async def _checked_pause(recheck, meter):
    """Yield, then run recheck (if any), which raises when the import must stop."""
    await _pause(meter)
    if recheck is not None:
        recheck()


async def _write_pieces(target, chunks, digest, recheck, meter, limit):
    """Write the chunks in WRITE_PIECE pieces with a yield after each.

    Returns the bytes written; digest receives each piece. recheck runs
    after every yield, so a stop costs at most the piece already written.
    More than limit bytes is 400 too_many_targets before the chunk that
    passes it is written.
    """
    written = 0
    pending = b""
    while True:
        data = _next_data(chunks, meter)
        if data is None:
            break
        written += len(data)
        if written > limit:
            raise StakeoutError(_TOO_MANY, "The target set would need more than %d "
                                "bytes." % MAX_SET_FILE_BYTES, path="")
        start = 0
        if pending:
            start = min(len(data), WRITE_PIECE - len(pending))
            pending += data[:start]
            if len(pending) < WRITE_PIECE:
                continue
            started = time.ticks_ms()
            target.write(pending)
            meter.note("write", started)
            digest.update(pending)
            pending = b""
            await _checked_pause(recheck, meter)
        while len(data) - start >= WRITE_PIECE:
            piece = data[start:start + WRITE_PIECE]
            started = time.ticks_ms()
            target.write(piece)
            meter.note("write", started)
            digest.update(piece)
            start += WRITE_PIECE
            piece = None
            await _checked_pause(recheck, meter)
        pending = data[start:] if start else data
        data = None
    if pending:
        started = time.ticks_ms()
        target.write(pending)
        meter.note("write", started)
        digest.update(pending)
    return written


def _commit(tmp, path, timing, meter):
    """Rename tmp to path, the commit; a rename that raises counts when path
    exists and tmp is gone (it moved), otherwise tmp is removed: 507."""
    started = time.ticks_ms()
    try:
        os.rename(tmp, path)
    except OSError as error:
        if not _exists(path) or _exists(tmp):
            _discard(tmp)
            raise _write_failed("The target set could not be committed (%s)." % error)
    meter.note("dir", started)
    _lap(timing, "rename", started)


# --- Set file layout 2 ------------------------------------------------------

def _counted(mark, text):
    """text as UTF-8 bytes, their count added to mark[0]."""
    data = text.encode("utf-8")
    mark[0] += len(data)
    return data


def _body_chunks(doc, mark):
    """The body of the layout-2 set file of doc as UTF-8 pieces.

    The head (doc without parts and targets, its closing brace cut off),
    then one ujson.dumps line per part and per target, ",\n" between the
    items; the body ends with the "]" that closes targets. Line breaks are
    JSON whitespace, so body and trailer are one JSON object. mark[0]
    counts the bytes made, mark[1] receives the offset of the first target
    line (targets_offset).
    """
    head = {}
    for key, value in doc.items():
        if key != "parts" and key != "targets":
            head[key] = value
    opening = ujson.dumps(head)[:-1] + ',\n"parts": [\n'
    head = None
    for key in ("parts", "targets"):
        yield _counted(mark, opening)
        if key == "targets":
            mark[1] = mark[0]
        for index, item in enumerate(doc[key]):
            if index:
                yield _counted(mark, ",\n")
            yield _counted(mark, ujson.dumps(item))
        opening = '\n],\n"targets": [\n'
    yield _counted(mark, "\n]")


def _trailer(summary):
    """The trailer that carries summary: TRAILER_BYTES long when it fits."""
    data = _TRAILER_HEAD + ujson.dumps(summary).encode("utf-8")
    return data + b" " * (TRAILER_BYTES - len(data) - len(_TRAILER_END)) + _TRAILER_END


async def _read_back(path, body_bytes, trailer, recheck, meter):
    """(SHA-256 of the body, whether the rest is exactly trailer) of path.

    The body is its first body_bytes bytes. Read in WRITE_PIECE blocks with
    a yield after each; recheck runs after every yield.
    """
    digest = hashlib.sha256()
    size = 0
    same = True
    with open(path, "rb") as source:
        while True:
            started = time.ticks_ms()
            block = source.read(WRITE_PIECE)
            meter.note("read", started)
            if not block:
                break
            count = len(block)
            cut = min(max(body_bytes - size, 0), count)
            if cut:
                digest.update(block if cut == count else block[:cut])
            if cut < count:
                at = size + cut - body_bytes
                same = same and block[cut:] == trailer[at:at + count - cut]
            size += count
            block = None
            await _checked_pause(recheck, meter)
    return digest.digest(), same and size == body_bytes + TRAILER_BYTES


async def _store_set_file(path, doc, timing=None, recheck=None, meter=None):
    """Write doc to path as a layout-2 set file; returns its summary.

    The summary (the trailer's, see _read_trailer for its members) is made
    from doc (counts, source, fingerprint, imported_at) and the bytes
    written (body_bytes, body_sha256, targets_offset). Cooperative, through
    path + ".tmp": the body is serialized once and written in WRITE_PIECE
    pieces with a yield after each while a SHA-256 runs over it; a body
    that would take the file over MAX_SET_FILE_BYTES stops the write with
    400 too_many_targets. The summary must pass the reader's checks
    (_summary_ok) and its trailer be exactly TRAILER_BYTES long (507
    otherwise, before the trailer is written); after a yield the trailer
    follows. The .tmp is flushed, closed and read back in WRITE_PIECE blocks
    with a yield after each; body digest, trailer and length must match.
    recheck() runs after every yield, the last one before the rename
    included, and may raise. Right after it, with no yield, a set file that
    exists at path is 409 already_imported (a rename would replace it);
    then the rename commits (_commit). Any failure before the commit, a
    cancellation included, removes the .tmp: a write or read-back failure
    raises 507 stakeout_write_failed, a MemoryError while verifying 503
    stakeout_busy.
    timing receives "write", "verify" and "rename" in milliseconds (wall
    time, yields included; the first two also as _work and _wait). meter
    (the caller's; a private one when None) sees every yield and the
    slowest serialization step, write, read, flush, close and rename.
    """
    if meter is None:
        meter = _BlockMeter()
    tmp = path + ".tmp"
    summary = _summary(doc)
    mark = [0, 0]
    try:
        started = time.ticks_ms()
        waited = meter.suspended
        digest = hashlib.sha256()
        try:
            target = open(tmp, "wb")
            try:
                body = await _write_pieces(target, _body_chunks(doc, mark), digest,
                                           recheck, meter,
                                           MAX_SET_FILE_BYTES - TRAILER_BYTES)
                expected = digest.digest()
                digest = None
                summary["body_bytes"] = body
                summary["body_sha256"] = _hex(expected)
                summary["targets_offset"] = mark[1]
                writing = time.ticks_ms()
                trailer = _trailer(summary)
                meter.note("serialize", writing)
                # The writer's own summary passes the reader's checks, or
                # nothing is committed (never a silently damaged set).
                try:
                    fits = (len(trailer) == TRAILER_BYTES and _summary_ok(
                        summary, doc["project_id"], doc["set_id"], body + TRAILER_BYTES))
                except (ValueError, TypeError, KeyError):
                    fits = False
                if not fits:
                    raise _write_failed("The target set summary does not fit its trailer.")
                await _checked_pause(recheck, meter)
                writing = time.ticks_ms()
                target.write(trailer)
                meter.note("write", writing)
                flushing = time.ticks_ms()
                target.flush()
                meter.note("flush", flushing)
            finally:
                closing = time.ticks_ms()
                target.close()
                meter.note("close", closing)
        except MemoryError:
            gc.collect()
            raise _write_failed("Not enough memory to write the target set.")
        except OSError as error:
            raise _write_failed("The target set could not be written (%s)." % error)
        started = _lap(timing, "write", started, meter, waited)
        waited = meter.suspended
        await _checked_pause(recheck, meter)
        try:
            stored, intact = await _read_back(tmp, body, trailer, recheck, meter)
            intact = intact and stored == expected
        except MemoryError:
            gc.collect()
            raise _short_of_memory("verify the target set")
        except OSError:
            intact = False
        if not intact:
            raise _write_failed("The written target set did not read back intact.")
        _lap(timing, "verify", started, meter, waited)
        await _checked_pause(recheck, meter)
        # Sets are immutable, and a rename replaces an existing target; a
        # check that cannot tell is no permission.
        try:
            present = _present(path)
        except OSError as error:
            raise _write_failed("The target set could not be checked (%s)." % error)
        if present:
            raise _already(doc["set_id"])
    except BaseException:
        _discard(tmp)
        raise
    _commit(tmp, path, timing, meter)
    return summary


def _summary_ok(summary, project_id, set_id, size):
    """True when summary fits the set file of set_id in project_id of size bytes.

    Exactly the members _store_set_file writes, each within its range;
    raises ValueError, TypeError or KeyError for some malformed summaries.
    """
    source = summary["source"]
    for values, fields in ((summary, _SUMMARY_INTS), (source, _SOURCE_INTS)):
        for key, low, high in fields:
            _int(values[key], low, high, key)
    created = summary["project_created_at"]
    if created is not None:
        _int(created, 0, _UINT32, "project_created_at")
    _text(source["filename"], 0, MAX_FILENAME, "filename")
    body = summary["body_bytes"]
    offset = summary["targets_offset"]
    digest = summary["body_sha256"]
    return (len(summary) == 12 and len(source) == 4
            and valid_set_id(set_id) and summary["set_id"] == set_id
            and valid_project_id(project_id) and summary["project_id"] == project_id
            and type(offset) is int and 0 < offset < body
            and isinstance(digest, str) and len(digest) == 64 and _lower_hex(digest)
            and body + TRAILER_BYTES == size)


def _read_trailer(path, project_id, set_id):
    """The summary of the set file path, or None when the file is damaged.

    Reads only the last TRAILER_BYTES, never the body: the fixed frame
    around the summary, the summary checked by _summary_ok against the IDs
    the file name and directory give and the file size from os.stat. A
    MemoryError and any OSError propagate (ENOENT included: a file gone
    meanwhile is no damage), since neither says anything about the file.
    """
    size = os.stat(path)[6]
    if size <= TRAILER_BYTES:
        return None
    with open(path, "rb") as source:
        source.seek(size - TRAILER_BYTES)
        data = source.read(TRAILER_BYTES)
    if (len(data) != TRAILER_BYTES or not data.startswith(_TRAILER_HEAD)
            or not data.endswith(_TRAILER_END)):
        return None
    try:
        summary = ujson.loads(data[len(_TRAILER_HEAD):-len(_TRAILER_END)])
        data = None
        if _summary_ok(summary, project_id, set_id, size):
            return summary
    except (ValueError, TypeError, KeyError, AttributeError):
        pass
    return None


def _target_lines(rest, data, needle, tid):
    """Split rest + data at "\\n" into target lines; returns (rest, hit).

    rest is the unfinished last line so far, needle the bytes
    '"tid": "<tid>"' that a line of tid contains (a property may too, so
    ujson.loads confirms it). hit is the target of tid when one of the
    complete lines is it, else None. Raises ValueError for a line or rest
    longer than TARGET_LINE_MAX and ValueError, KeyError or TypeError for a
    line of tid that is no valid target line (malformed JSON, a wrong type,
    a coordinate out of range). The "]" that closes the targets is the
    body's last byte, so it stays the rest: the scan ends at body_bytes.
    """
    lines = (rest + data).split(b"\n")
    for line in lines:
        if len(line) > TARGET_LINE_MAX:
            raise ValueError
    rest = lines.pop()
    hit = None
    for line in lines:
        if hit is None and needle in line:
            target = ujson.loads(line.rstrip(b","))
            if target["tid"] == tid:
                lat_e8, lon_e8, alt = target["lat_e8"], target["lon_e8"], target["alt_import"]
                if not (type(lat_e8) is int and abs(lat_e8) <= _LAT_LIMIT
                        and type(lon_e8) is int and abs(lon_e8) <= _LON_LIMIT
                        and isinstance(target["name"], str)
                        and (alt is None or type(alt) is int)):
                    raise ValueError
                hit = target
    return rest, hit


# --- Target store ---------------------------------------------------------

def _marker(set_id, state):
    """A cache entry for a set whose content is unknown (damaged, incomplete)."""
    return {"summary": {"set_id": set_id, "state": state}, "fingerprint": "?"}


def _summary(doc):
    """The trailer summary of a set document, without the members that come
    from the written bytes (body_bytes, body_sha256, targets_offset)."""
    source = doc["source"]
    coords = 0
    for part in doc["parts"]:
        coords += len(part["coords_e8"])
    return {"layout": FILE_LAYOUT, "set_id": doc["set_id"], "project_id": doc["project_id"],
            "project_created_at": doc["project_created_at"],
            "imported_at": doc["imported_at"],
            "source": {"filename": source["filename"], "bytes": source["bytes"],
                       "features": source["features"],
                       "skipped_total": source["skipped_total"]},
            "parts": len(doc["parts"]), "targets": len(doc["targets"]),
            "coords": coords}


def _entry(summary):
    """The cache entry of an intact set file from its trailer summary: the
    overview summary, the fingerprint, what a download checks and where the
    target lines start (find_target)."""
    return {"summary": {"set_id": summary["set_id"], "state": "ok",
                        "imported_at": summary["imported_at"], "source": summary["source"],
                        "parts": summary["parts"], "targets": summary["targets"],
                        "coords": summary["coords"]},
            "fingerprint": summary["project_created_at"],
            "body": summary["body_bytes"], "digest": summary["body_sha256"],
            "offset": summary["targets_offset"]}


def _order(entry):
    summary = entry["summary"]
    if summary["state"] == "ok":
        return (0, summary["imported_at"], summary["set_id"])
    return (1, 0, summary["set_id"])


def _visible(entry, fingerprint):
    return entry["fingerprint"] == "?" or entry["fingerprint"] == fingerprint


def _errno(error):
    return error.args[0] if error.args else None


def _invalid_request(message):
    return StakeoutError("invalid_request", message, _BAD_REQUEST)


def _busy(message="Another target set change is running."):
    return StakeoutError("storage_busy", message, _CONFLICT)


def _not_found():
    return StakeoutError("set_not_found", "No such target set.", _NOT_FOUND)


def _short_of_memory(task):
    """503 stakeout_busy for a MemoryError while doing task (transient)."""
    return StakeoutError("stakeout_busy", "Not enough memory to %s; try again." % task,
                         _UNAVAILABLE)


def _changed_meanwhile():
    return StakeoutError("stakeout_busy", "The target sets changed meanwhile; try again.",
                         _UNAVAILABLE)


def _too_large(message="The import package is too large for the free memory."):
    return StakeoutError("import_too_large", message, "413 Payload Too Large")


def _cleanup_pending():
    return StakeoutError("stakeout_cleanup_pending", "The target sets of an earlier "
                         "deletion of this project are not removed yet; restart the "
                         "device.", _CONFLICT)


def _check_project_id(project_id):
    if not valid_project_id(project_id):
        raise _invalid_request("project_id must be 1 to %d characters from A-Z, a-z, "
                               "0-9, - and _." % MAX_PROJECT_ID)


def _same_project(check_project, project_id, fingerprint):
    """Run check_project (if any) again; a changed fingerprint is 404."""
    if check_project is not None and check_project(project_id) != fingerprint:
        raise StakeoutError("project_not_found", "The project changed meanwhile.",
                            _NOT_FOUND)


class _Mutation:
    """The change lock of a TargetStore (TargetStore.mutation): entering
    never waits and never yields."""

    def __init__(self, store, write):
        self.store = store
        self.write = write

    async def __aenter__(self):
        store = self.store
        if self.write and store.uncertain is not None:
            raise write_uncertain_error()
        if store.busy:
            raise _busy()
        store.busy = True

    async def __aexit__(self, *exc):
        self.store.busy = False
        return False


class TargetStore:
    """The stakeout target sets of all projects, one file per set.

    A set lives in "<root>/<project_id>/<set_id>.json" (layout 2, see
    _store_set_file) and is immutable: an import writes a new file through a
    checked .tmp beside it and a rename, a delete removes it, nothing
    rewrites one. The root and the project directory are made by the first
    import; other names in a project directory are ignored.

    A listing reads only the trailer of each set file (_read_trailer) and
    never renames or removes anything, so it is the same during a recording
    or a maintenance. It classifies: a set file with an intact trailer is
    "ok", any other set file "damaged" (a .tmp beside it is kept); a lone
    .tmp is "incomplete" (an import of the same set replaces it), except the
    .tmp of the running import (_writing), which is not listed at all.
    damaged and incomplete sets are listed and can be deleted. A MemoryError
    while listing is transient (503 stakeout_busy) and an I/O error 507
    stakeout_read_failed; neither leaves a cache entry.

    Each set stores project_created_at, the project fingerprint. A set
    whose fingerprint differs from the current project of the same ID is
    orphaned: hidden, unreadable, not deletable, counted by orphaned().
    This misses an ID reused with the same created_at (same second).

    The cache holds per project a list of entries (_entry, _marker);
    fingerprint "?" marks a set of unknown content, visible under any
    fingerprint. Each project has a generation, raised by every change of
    its sets or of a cached state (_changed); a listing publishes its result
    only when the generation stayed the same meanwhile, lists once more
    otherwise, and answers 503 stakeout_busy when it changed again. A set
    whose download found a digest mismatch stays damaged (_damaged) until
    its file is gone (deleted, or found missing by a listing), a new import
    of it is committed or the device restarts. Open downloads count as
    readers of their project; while there is one, deleting a set of the
    project is 409 storage_busy.

    A project deletion (the caller journals it) first sets the project
    directory aside as "<project_id>.deleting" (begin_project_delete), then
    removes it (finish_project_delete) or puts it back
    (undo_project_delete); a restart in between is settled before the next
    start completes (reconcile_deletions). A live project whose directory
    could not be put back is blocked until then (refuse_blocked).

    One change lock (mutation) covers every change of a project's stakeout
    data: imports, set deletions and what the caller runs under it
    (progress, stakeout points). It never waits: a second change is 409
    storage_busy. After a stakeout write whose outcome cannot be told,
    uncertain (RAM only, a restart clears it) refuses every write lock
    with 503 stakeout_write_uncertain. A set whose removal is committed
    (removed, a marker per set: its imported_at, or None for a set that
    was not "ok") is hidden from listings; its files left behind are
    remnants until clear_removed removes them.
    """

    def __init__(self, root=DEFAULT_ROOT):
        self.root = root
        self.busy = False
        # Why a stakeout write's outcome is unknown (refuses every write lock).
        self.uncertain = None
        # {project_id: {set_id: imported_at | None}}: committed removals.
        self.removed = {}
        # {project_id: set of paths}: files of removed sets still on the flash.
        self._remnants = {}
        # The .tmp the running import writes; listings never list or read it.
        self._writing = None
        self._cache = {}
        self._generation = {}
        self._readers = {}
        self._damaged = set()
        # Live projects whose folder stays set aside (refuse_blocked).
        self._blocked = set()
        # True while the start-up reconciliation has not looked at every
        # set-aside folder: then any project may be blocked.
        self._unresolved = False
        self.last_load_ms = 0
        self.last_load_block_ms = 0
        self.last_scan_ms = 0
        self.last_scan_block_ms = 0

    def project_path(self, project_id):
        """The directory of project_id's sets."""
        _check_project_id(project_id)
        return "%s/%s" % (self.root, project_id)

    def set_path(self, project_id, set_id):
        directory = self.project_path(project_id)
        if not valid_set_id(set_id):
            raise _invalid_request("set_id must be so- followed by 8 lowercase hex "
                                   "digits.")
        return "%s/%s.json" % (directory, set_id)

    def generation(self, project_id):
        return self._generation.get(project_id, 0)

    def readers(self, project_id):
        """How many downloads, lookups and other holds of project_id are open."""
        return self._readers.get(project_id, 0)

    def hold(self, project_id):
        """Count a reader of project_id until release(project_id): deleting
        one of its sets or the project is 409 storage_busy meanwhile."""
        self._readers[project_id] = self.readers(project_id) + 1

    def release(self, project_id):
        self._readers[project_id] -= 1

    def mutation(self, write=True):
        """The change lock, as an async context manager (see the class).
        Entering refuses at once: 503 stakeout_write_uncertain when write and
        uncertain is set, 409 storage_busy while the lock is taken. Only a
        reader that loads progress takes it with write=False."""
        return _Mutation(self, write)

    def _refuse_readers(self, project_id):
        """409 storage_busy while a hold of project_id is open (for callers
        that hold the change lock)."""
        if self.readers(project_id):
            raise _busy("A download of target sets of this project is open; try again.")

    def _refuse_busy(self, project_id):
        """409 storage_busy while the change lock is taken or a hold of project_id is open."""
        if self.busy:
            raise _busy()
        self._refuse_readers(project_id)

    def _refuse_pending(self, project_id):
        """409 stakeout_cleanup_pending while a deletion's folder of project_id
        is set aside: its sets must not change until the next start settles it,
        so the two folders never coexist. Call with no yield before the change."""
        if _present(self.project_path(project_id) + ".deleting"):
            raise _cleanup_pending()

    def _changed(self, project_id):
        """A set of project_id or its cached state changed: raise the generation."""
        self._generation[project_id] = self.generation(project_id) + 1

    def invalidate(self, project_id):
        """Forget the cached listing of project_id; the next access lists again."""
        self._cache.pop(project_id, None)
        self._changed(project_id)

    # --- listing ----------------------------------------------------------

    async def _entries(self, project_id, meter=None):
        """The cached entries of project_id, listed if cold (see the class).

        Yields while listing; meter (the caller's operation's) sees every
        yield and the slowest directory operation and trailer read.
        """
        _check_project_id(project_id)
        entries = self._cache.get(project_id)
        if entries is not None:
            return entries
        if meter is None:
            meter = _BlockMeter()
        for _ in range(2):
            generation = self.generation(project_id)
            try:
                entries = await self._load(project_id, meter)
            except MemoryError:
                gc.collect()
                raise _short_of_memory("list the target sets")
            except OSError as error:
                raise StakeoutError("stakeout_read_failed", "The target sets could not be "
                                    "read (%s)." % error, _NO_STORAGE)
            if self.generation(project_id) == generation:
                self._cache[project_id] = entries
                return entries
        raise _changed_meanwhile()

    async def _load(self, project_id, meter):
        """List and classify the sets of project_id; the caller caches the result.

        Walks the project directory with a yield after every SLICE_MS (at
        the latest every YIELD_TARGETS names) and reads the trailer of each
        set file with a yield after each. A missing directory has no sets.
        A set file gone meanwhile is a change of the project (the listing
        starts over), not damage.
        """
        directory = self.project_path(project_id)
        pacer = _Pacer(meter)
        candidates = {}
        started = time.ticks_ms()
        try:
            names = os.ilistdir(directory)
        except OSError as error:
            if _errno(error) == _ENOENT:
                return []
            raise
        meter.note("dir", started)
        for item in names:
            name = item[0]
            slot = 0 if name.endswith(".json") else 1 if name.endswith(".json.tmp") else None
            if slot is not None and item[1] != _DIRECTORY:
                set_id = name[:-5 - 4 * slot]
                if valid_set_id(set_id):
                    candidates.setdefault(set_id, [False, False])[slot] = True
            if pacer.due("targets"):
                await pacer.pause()
        # A damage mark goes with its file.
        prefix = directory + "/"
        for path in [path for path in self._damaged if path.startswith(prefix)]:
            if not candidates.get(path[len(prefix):-5], (False,))[0]:
                self._damaged.discard(path)
        entries = []
        marks = self.removed.get(project_id) or {}
        remnants = set()
        for set_id, (has_file, has_tmp) in candidates.items():
            path = "%s/%s.json" % (directory, set_id)
            mark = marks.get(set_id, _MISSING)
            if not has_file:
                if path + ".tmp" == self._writing:
                    continue
                entry = _marker(set_id, "incomplete")
            elif path in self._damaged:
                entry = _marker(set_id, "damaged")
            else:
                started = time.ticks_ms()
                try:
                    summary = _read_trailer(path, project_id, set_id)
                except OSError as error:
                    if _errno(error) != _ENOENT:
                        raise
                    self._changed(project_id)
                    continue
                meter.note("read", started)
                entry = _entry(summary) if summary else _marker(set_id, "damaged")
                summary = None
                await _pause(meter)
            # A file of a removed set is a remnant: any lone .tmp, a damaged
            # file under the marker None, an intact one with its imported_at.
            if mark is not _MISSING and (not has_file or
                                         entry["summary"].get("imported_at") == mark):
                if has_file:
                    remnants.add(path)
                if has_tmp:
                    remnants.add(path + ".tmp")
            else:
                entries.append(entry)
        self._remnants[project_id] = remnants
        entries.sort(key=_order)
        return entries

    async def _find(self, project_id, fingerprint, set_id, meter=None):
        """(entry, path) of a visible set; 404 set_not_found otherwise."""
        path = self.set_path(project_id, set_id)
        for entry in await self._entries(project_id, meter):
            if entry["summary"]["set_id"] == set_id and _visible(entry, fingerprint):
                return entry, path
        raise _not_found()

    async def sets(self, project_id, fingerprint):
        """The summaries of the sets visible under fingerprint, in overview order.

        Each summary is a shallow copy of the cached one. Afterwards
        last_load_ms is the wall time of this call's listing (yields
        included) and last_load_block_ms its longest stretch without a
        yield; both are 0 on a cache hit.
        """
        _check_project_id(project_id)
        hit = project_id in self._cache
        meter = _BlockMeter()
        started = time.ticks_ms()
        entries = await self._entries(project_id, meter)
        self.last_load_ms = 0 if hit else time.ticks_diff(time.ticks_ms(), started)
        self.last_load_block_ms = 0 if hit else meter.lap()
        return [dict(entry["summary"]) for entry in entries
                if _visible(entry, fingerprint)]

    def orphaned(self, project_id, fingerprint):
        """How many intact sets of project_id belong to another fingerprint
        (from the cached listing: call right after sets())."""
        _check_project_id(project_id)
        count = 0
        for entry in self._cache.get(project_id, ()):
            if not _visible(entry, fingerprint):
                count += 1
        return count

    def _remember(self, project_id, entry):
        """Put a newly written set into the cached listing of project_id, if any;
        its file and a .tmp beside it are no remnants any more."""
        set_id = entry["summary"]["set_id"]
        path = self.set_path(project_id, set_id)
        self._remnants.get(project_id, set()).difference_update((path, path + ".tmp"))
        entries = self._cache.get(project_id)
        if entries is not None:
            entries = [item for item in entries if item["summary"]["set_id"] != set_id]
            entries.append(entry)
            entries.sort(key=_order)
            self._cache[project_id] = entries

    # --- download -------------------------------------------------------

    async def send_set(self, writer, project_id, fingerprint, set_id, head,
                       check_project=None):
        """Send the stored file of a visible, intact set to writer, byte for byte.

        Before anything is written: the entry is "ok" (409 set_damaged
        otherwise) and the file has the size of its summary. writer gets
        head(size) (the status line and headers) and then the file in 1 KiB
        reads, drained with a service pause after each, while a SHA-256
        runs over the body. The trailer is sent only when the digest equals
        the summary's body_sha256; otherwise the set is damaged from now on
        (ERROR log) and the StakeoutError raised after the head tells the
        caller to close the connection. check_project, if given, runs again
        after the (possibly cold) lookup. From there on, without a yield,
        the download counts as a reader of the project and opens the file;
        the count is released however it ends. A piece the client does not
        take within 10 s ends the download (the TimeoutError propagates).
        """
        entry, path = await self._find(project_id, fingerprint, set_id)
        if entry["summary"]["state"] != "ok":
            raise StakeoutError("set_damaged", "The target set is damaged; delete it and "
                                "import it again.", _CONFLICT, set_id=set_id)
        _same_project(check_project, project_id, fingerprint)
        meter = _BlockMeter()
        body = entry["body"]
        self.hold(project_id)
        try:
            try:
                size = os.stat(path)[6]
                source = open(path, "rb")
            except OSError as error:
                self.invalidate(project_id)
                if _errno(error) == _ENOENT:
                    raise _not_found()
                raise StakeoutError("stakeout_read_failed", "The target set could not be "
                                    "read (%s)." % error, _NO_STORAGE)
            try:
                if size != body + TRAILER_BYTES:
                    raise self._damage(project_id, path, set_id)
                writer.write(head(size))
                digest = hashlib.sha256()
                sent = 0
                while sent < size:
                    if sent == body and _hex(digest.digest()) != entry["digest"]:
                        break
                    want = min(1024, (body if sent < body else size) - sent)
                    try:
                        block = source.read(want)
                    except OSError:
                        # Says nothing about the file, but the cached state
                        # can no longer be trusted.
                        self.invalidate(project_id)
                        raise
                    if not block:
                        break
                    if sent < body:
                        digest.update(block)
                    writer.write(block)
                    sent += len(block)
                    block = None
                    # A vanished client must not hold the reader for the
                    # lifetime of its TCP connection.
                    await asyncio.wait_for(writer.drain(), 10)
                    await _pause(meter)
                if sent < size:
                    raise self._damage(project_id, path, set_id)
            finally:
                source.close()
        finally:
            self.release(project_id)

    def _damage(self, project_id, path, set_id):
        """Mark a set whose file differs from its summary damaged; returns the error."""
        log("ERROR", "STAKE", "The target set %s does not match its summary; it is "
            "damaged." % path)
        self._damaged.add(path)
        self.invalidate(project_id)
        return StakeoutError("set_damaged", "The target set is damaged; delete it and "
                             "import it again.", _CONFLICT, set_id=set_id)

    def is_damaged(self, project_id, set_id):
        """True when the set is known damaged from RAM alone: a damage mark
        (_damaged) or a cached "damaged" entry of project_id (a listing that
        found a bad trailer). No file access and no listing; a cold cache
        knows only the damage marks. Both IDs must be valid (set_path)."""
        return (self.set_path(project_id, set_id) in self._damaged
                or _marker(set_id, "damaged") in self._cache.get(project_id, ()))

    # --- target lookup (stakeout WP2) -----------------------------------

    async def find_target(self, project_id, fingerprint, tid, check_project=None):
        """The target tid of a visible, intact set, read from its set file.

        tid is "so-xxxxxxxx/<part>/<seq>" (parse_tid; 400 invalid_request
        otherwise). Before the file is opened, through the (possibly cold)
        listing: an unknown or invisible set is 404 set_not_found, a set not
        "ok" 409 set_damaged and a part beyond the summary's count 404
        target_not_found; then check_project, if given, runs again. From
        there on, without a yield, the lookup counts as a reader of the
        project (deleting one of its sets is 409 storage_busy) until it
        ends, however it ends.
        The first lookup in a set after its listing was cached is the
        checking pass: it reads the body from byte 0 to its end and checks
        length and SHA-256 against the summary, as a download does, and the
        entry remembers a match ("verified"). Later lookups read from
        targets_offset and stop at the target. Both passes read SCAN_PIECE
        blocks with a pause after each, never a trailer byte, and split the
        target lines at "\\n" (_target_lines); the unfinished line carried
        from block to block is at most TARGET_LINE_MAX bytes, so never more
        than SCAN_PIECE + TARGET_LINE_MAX bytes of the file are buffered
        (split into lines once per block). A digest or length mismatch, a
        line longer than TARGET_LINE_MAX or a line of tid that is no valid
        target damages the set (409 set_damaged). A target the set does not
        have is 404 target_not_found; a file gone is 404 set_not_found, any
        other I/O error 507 stakeout_read_failed (both invalidate the cache)
        and a MemoryError 503 stakeout_busy.
        Returns {"tid", "set_id", "part_id", "seq", "name", "lat_e8",
        "lon_e8", "alt_import"}. Afterwards last_scan_ms is the wall time of
        the call (yields included) and last_scan_block_ms its longest
        stretch without a yield.
        """
        parsed = parse_tid(tid)
        if parsed is None:
            raise _invalid_request("tid must be so-xxxxxxxx/<part>/<seq>.")
        set_id, part, seq = parsed
        meter = _BlockMeter()
        started = time.ticks_ms()
        hit = None
        try:
            entry, path = await self._find(project_id, fingerprint, set_id, meter)
            if entry["summary"]["state"] != "ok":
                raise StakeoutError("set_damaged", "The target set is damaged; delete it and "
                                    "import it again.", _CONFLICT, set_id=set_id)
            if part <= entry["summary"]["parts"]:
                _same_project(check_project, project_id, fingerprint)
                self.hold(project_id)
                try:
                    hit = await self._scan(entry, path, project_id, set_id, tid, meter)
                finally:
                    self.release(project_id)
        finally:
            self.last_scan_ms = time.ticks_diff(time.ticks_ms(), started)
            self.last_scan_block_ms = meter.lap()
        if hit is None:
            raise StakeoutError("target_not_found", "No such target.", _NOT_FOUND)
        return {"tid": tid, "set_id": set_id, "part_id": "%s/%d" % (set_id, part),
                "seq": seq, "name": hit["name"], "lat_e8": hit["lat_e8"],
                "lon_e8": hit["lon_e8"], "alt_import": hit["alt_import"]}

    async def _scan(self, entry, path, project_id, set_id, tid, meter):
        """The target of tid in the set file path, or None (see find_target)."""
        body = entry["body"]
        offset = entry["offset"]
        needle = ('"tid": "%s"' % tid).encode()
        verified = entry.get("verified")
        at = offset if verified else 0
        rest = b""
        hit = None
        try:
            digest = hashlib.sha256()
            with open(path, "rb") as source:
                source.seek(at)
                while at < body:
                    started = time.ticks_ms()
                    block = source.read(min(SCAN_PIECE, body - at))
                    meter.note("read", started)
                    if not block:
                        break
                    if not verified:
                        digest.update(block)
                    if hit is None and at + len(block) > offset:
                        try:
                            rest, hit = _target_lines(rest, block[max(offset - at, 0):],
                                                      needle, tid)
                        except (ValueError, KeyError, TypeError):
                            raise self._damage(project_id, path, set_id)
                    at += len(block)
                    block = None
                    if hit is not None and verified:
                        return hit
                    await _pause(meter)
        except OSError as error:
            self.invalidate(project_id)
            if _errno(error) == _ENOENT:
                raise _not_found()
            raise StakeoutError("stakeout_read_failed", "The target set could not be "
                                "read (%s)." % error, _NO_STORAGE)
        except MemoryError:
            gc.collect()
            raise _short_of_memory("read the target set")
        if at < body or not verified and _hex(digest.digest()) != entry["digest"]:
            raise self._damage(project_id, path, set_id)
        entry["verified"] = True
        return hit

    # --- import ---------------------------------------------------------

    async def add_set(self, plan, imported_at, fingerprint, capacity, timing, meter=None,
                      check_project=None, charge=None, remeasure=None):
        """Store a validated plan as a new set and return the import answer.

        Cooperative (F1a): it yields after the set checks, after making the
        directories, after the flash reserve, and while writing and
        verifying (_store_set_file). check_project(project_id), if given,
        runs again before the folders are made, before the write, after
        every yield of the write and right before the rename; it raises
        when a recording, a measurement, a maintenance or a parallel
        benchmark began or the project is gone, and a changed fingerprint
        (the project was deleted and created again) is 404
        project_not_found. A change of the project's sets meanwhile is 503
        stakeout_busy. Refuses, before anything is written, a fingerprint
        the trailer cannot hold (not None and no int in 0..2**32-1: 409
        unsupported_project_fingerprint), a set that exists (409
        already_imported, checked again right before the rename), is
        damaged (409 set_damaged) or is orphaned (409
        orphaned_set_conflict), a fifth set (409 too_many_sets), a file over
        MAX_SET_FILE_BYTES (400 too_many_targets) and a missing flash
        reserve (507 storage_reserve); an incomplete leftover of the same
        set is replaced. While a deletion's folder of the project is set
        aside, the import is 409 stakeout_cleanup_pending, checked right
        before the folders are made. The store root and the project
        directory are made before the reserve check,
        capacity(2 * MAX_SET_FILE_BYTES + 32768). Right after the rename (the commit) charge(file size) counts the
        file against the flash sample (timing capacity_refresh); any
        failure after it is 500 import_committed_response_failed with the
        set_id. When a failed write leaves its .tmp behind, remeasure()
        makes the next flash check measure. meter (import_package's; a
        private one when None) sees every yield and publishes its totals
        into timing after the commit (TIMING_KEYS).
        """
        if fingerprint is not None and (type(fingerprint) is not int
                                        or not 0 <= fingerprint <= _UINT32):
            raise StakeoutError("unsupported_project_fingerprint", "The project's created_at "
                                "is no whole number of seconds from 0 to %d, so it cannot "
                                "hold target sets." % _UINT32, _CONFLICT)
        if meter is None:
            meter = _BlockMeter()
        project_id = plan["project_id"]
        set_id = plan["set_id"]
        path = self.set_path(project_id, set_id)
        tmp = path + ".tmp"
        entries = await self._entries(project_id, meter)
        generation = self.generation(project_id)

        def recheck():
            _same_project(check_project, project_id, fingerprint)
            if self.generation(project_id) != generation:
                raise _changed_meanwhile()

        count = 0
        for entry in entries:
            summary = entry["summary"]
            visible = _visible(entry, fingerprint)
            if summary["set_id"] == set_id:
                if not visible:
                    raise StakeoutError("orphaned_set_conflict", "A target set of an "
                                        "earlier project with this ID is in the way.",
                                        _CONFLICT, set_id=set_id)
                if summary["state"] == "damaged":
                    raise StakeoutError("set_damaged", "A damaged target set with this ID "
                                        "is in the way; delete the damaged set first.",
                                        _CONFLICT, set_id=set_id)
                if summary["state"] != "incomplete":
                    raise _already(set_id)
            if visible and summary["state"] != "incomplete":
                count += 1
        entries = None
        if count >= MAX_SETS:
            raise StakeoutError("too_many_sets", "A project holds at most %d target sets."
                                % MAX_SETS, _CONFLICT)
        await _pause(meter)
        recheck()
        self._refuse_pending(project_id)
        doc = {"kind": "stakeout_set", "schema": SCHEMA, "project_id": project_id,
               "project_created_at": fingerprint, "set_id": set_id,
               "imported_at": imported_at, "source": plan["source"],
               "parts": plan["parts"], "targets": plan["targets"]}
        started = time.ticks_ms()
        for directory in (self.root, self.project_path(project_id)):
            try:
                os.mkdir(directory)
            except OSError as error:
                if _errno(error) != _EEXIST:
                    raise _write_failed("The target set folder could not be made (%s)."
                                        % error)
        meter.note("dir", started)
        await _pause(meter)
        started = time.ticks_ms()
        reserved = capacity(2 * MAX_SET_FILE_BYTES + 32768)
        meter.note("capacity", started)
        if not reserved:
            raise StakeoutError("storage_reserve", "Not enough free flash for the "
                                "target set.", _NO_STORAGE)
        _lap(timing, "reserve", started)
        await _pause(meter)
        recheck()
        self._writing = tmp
        try:
            summary = await _store_set_file(path, doc, timing, recheck, meter)
        except BaseException:
            self.invalidate(project_id)
            if remeasure is not None and _exists(tmp):
                remeasure()
            raise
        finally:
            self._writing = None
        doc = None
        self._damaged.discard(path)
        self._changed(project_id)
        try:
            size = summary["body_bytes"] + TRAILER_BYTES
            started = time.ticks_ms()
            if charge is not None:
                charge(size)
            _lap(timing, "capacity_refresh", started)
            self._remember(project_id, _entry(summary))
            source = plan["source"]
            answer = {"set_id": set_id, "project_id": project_id,
                      "parts": summary["parts"], "targets": summary["targets"],
                      "coords": summary["coords"], "skipped": source["skipped"],
                      "skipped_total": source["skipped_total"], "bytes": size,
                      "timing_ms": timing}
            meter.publish(timing)
        except Exception:
            self.invalidate(project_id)
            raise StakeoutError("import_committed_response_failed", "The target set was "
                                "stored, but the answer could not be completed.",
                                "500 Internal Server Error", set_id=set_id)
        return answer

    async def import_package(self, body, check_project, capacity, charge=None,
                             remeasure=None, prepare=None):
        """Parse, validate and store an import package (the request body).

        check_project(project_id) returns the project fingerprint or raises
        StakeoutError; it runs before and again after the validation, and
        add_set runs it again after every yield of the write and right
        before the rename (the commit). prepare(project_id), if given, is
        awaited right after the first check_project, before the listing
        (the caller loads progress and clears remnants there).
        capacity(temporary_bytes) says whether the flash reserve allows the
        write; charge and remeasure go to add_set. The import holds the
        change lock (mutation) from the start to the end, every yield
        included. A set with a removal marker gets an imported_at past the
        marker's, so it is no remnant. It yields before and after a timed
        gc.collect, after the parse, while listing the project (cold) and
        after it, during the validation (_Pacer) and throughout add_set. A
        body over MAX_IMPORT_BYTES is 413 import_too_large before anything
        is parsed (the route refuses it earlier still). The answer's
        timing_ms is described at TIMING_KEYS.
        """
        async with self.mutation():
            if not body:
                raise StakeoutError("empty_import", "The import package is empty.")
            if len(body) > MAX_IMPORT_BYTES:
                raise _too_large("The import package is larger than %d bytes."
                                 % MAX_IMPORT_BYTES)
            timing = {key: 0 for key in TIMING_KEYS}
            # The operation's own meter (longest_block and the other diagnostics
            # of TIMING_KEYS), passed to everything that yields.
            meter = _BlockMeter()
            # The collection is a block of its own, between two yields: it
            # keeps an automatic one out of the parse and is timed.
            await _pause(meter)
            _timed_collect(timing)
            await _pause(meter)
            started = time.ticks_ms()
            try:
                payload = ujson.loads(body)
            except MemoryError:
                gc.collect()
                raise _too_large()
            except ValueError:
                raise _invalid("", "The import package is not valid JSON.")
            _lap(timing, "parse", started)
            if not isinstance(payload, dict):
                raise _invalid("", "The import package must be a JSON object.")
            if not valid_project_id(payload.get("project_id")):
                raise _invalid("project_id", "project_id must be 1 to %d characters "
                               "from A-Z, a-z, 0-9, - and _." % MAX_PROJECT_ID)
            project_id = payload["project_id"]
            await _pause(meter)
            check_project(project_id)
            if prepare is not None:
                await prepare(project_id)
            # A cold listing yields on its own, so the validation and
            # add_set start with a warm cache.
            await self._entries(project_id, meter)
            await _pause(meter)
            started = time.ticks_ms()
            waited = meter.suspended
            try:
                plan = await validate_import(payload, meter)
            except MemoryError:
                payload = None
                gc.collect()
                raise _too_large()
            payload = None
            _lap(timing, "validate", started, meter, waited)
            fingerprint = check_project(plan["project_id"])
            imported_at = now_seconds()
            mark = self.removed.get(project_id, {}).get(plan["set_id"])
            if mark is not None:
                imported_at = max(imported_at, mark + 1)
            return await self.add_set(plan, imported_at, fingerprint, capacity, timing,
                                      meter, check_project, charge, remeasure)

    # --- deleting -------------------------------------------------------

    def confirmed_count(self, project_id, set_id):
        """How many targets of the set are confirmed without progress (count)."""
        return 0

    async def delete_set(self, project_id, fingerprint, set_id, expected_confirmed,
                         check_project=None, prepare=None, count=None, on_remove=None):
        """Delete a visible set (also damaged or incomplete); "done" or "pending".

        Holds the change lock (mutation) throughout. prepare(project_id), if
        given, is awaited first (the caller loads progress there).
        expected_confirmed must equal count(project_id, set_id) (default
        confirmed_count), so a delete never drops progress the caller has
        not seen. After the (possibly cold) lookup, with no yield up to the
        commit, check_project (if given) runs again, an open hold of the
        project is 409 storage_busy and a set-aside folder of the project (a
        deletion waiting for the next start) is 409 stakeout_cleanup_pending.

        With on_remove, on_remove(project_id, set_id, imported_at) is the
        commit (the caller journals the removal; imported_at is None for a
        set that is not "ok"): when it raises, nothing changed and its error
        reaches the caller. Then the set is hidden at once (mark_removed)
        and its file and a .tmp beside it are removed after a pause each
        (clear_removed): "pending" when one stays (WARN; a remnant until
        clear_removed runs again), "done" otherwise.

        Without on_remove the removal is the commit: the file first, then a
        .tmp, so a set never comes back as "ok" once its removal began (a
        .tmp left behind is "incomplete"). A file that is gone (also after a
        removal that raised, when a stat then finds ENOENT) is no error; any
        other I/O error is 507 stakeout_delete_failed, with the cache
        unchanged when the file itself is known to have stayed (invalidated
        otherwise).
        """
        async with self.mutation():
            if not valid_set_id(set_id) or type(expected_confirmed) is not int:
                raise _invalid_request("delete_set needs set_id and expected_confirmed "
                                       "(an integer).")
            if prepare is not None:
                await prepare(project_id)
            entry, path = await self._find(project_id, fingerprint, set_id)
            if expected_confirmed != (count or self.confirmed_count)(project_id, set_id):
                raise StakeoutError("confirmed_changed", "The confirmed targets changed; "
                                    "reload the overview.", _CONFLICT, set_id=set_id)
            _same_project(check_project, project_id, fingerprint)
            self._refuse_readers(project_id)
            self._refuse_pending(project_id)
            if on_remove is not None:
                imported_at = entry["summary"].get("imported_at")
                on_remove(project_id, set_id, imported_at)
                self.mark_removed(project_id, set_id, imported_at)
                await asyncio.sleep_ms(SERVICE_PAUSE_MS)
                return "done" if await self.clear_removed(project_id) else "pending"
            for name in (path, path + ".tmp"):
                try:
                    os.remove(name)
                except OSError as error:
                    try:
                        stayed = _errno(error) != _ENOENT and _present(name)
                    except OSError:
                        # Whether the file is gone cannot be told.
                        stayed = None
                    if stayed is not False:
                        if name != path or stayed is None:
                            self.invalidate(project_id)
                        raise StakeoutError("stakeout_delete_failed", "The target set could "
                                            "not be deleted (%s)." % error, _NO_STORAGE,
                                            set_id=set_id)
                # The file is gone: a damage mark of it is no longer true.
                self._damaged.discard(path)
            self._forget_set(project_id, set_id)
            return "done"

    # --- removal markers and remnants (stakeout WP4) --------------------

    def note_removed(self, project_id, markers):
        """Replace the removal markers of project_id ({set_id: imported_at |
        None}, from its progress); a change forgets the cached listing."""
        if self.removed.get(project_id, {}) != markers:
            self.removed[project_id] = dict(markers)
            self.invalidate(project_id)

    def mark_removed(self, project_id, set_id, imported_at):
        """A committed removal: hide the set from now on; its file and a .tmp
        beside it count as remnants until clear_removed removes them."""
        self.removed.setdefault(project_id, {})[set_id] = imported_at
        path = self.set_path(project_id, set_id)
        self._remnants.setdefault(project_id, set()).update((path, path + ".tmp"))
        self._forget_set(project_id, set_id)

    def _forget_set(self, project_id, set_id):
        """Drop set_id from the cached listing of project_id; a change."""
        entries = self._cache.get(project_id)
        if entries is not None:
            self._cache[project_id] = [entry for entry in entries
                                       if entry["summary"]["set_id"] != set_id]
        self._changed(project_id)

    def remnants(self, project_id):
        """True when the last listing or removal of project_id left remnants."""
        return bool(self._remnants.get(project_id))

    async def clear_removed(self, project_id):
        """Remove the remnants of project_id (call under the change lock):
        lists the project when cold, then one file per block with a service
        pause after each attempt. True when none is left; any error is a
        WARN and leaves the rest for a later call."""
        try:
            await self._entries(project_id)
            left = self._remnants.get(project_id) or set()
            for path in sorted(left):
                try:
                    os.remove(path)
                except OSError as error:
                    if _errno(error) != _ENOENT:
                        raise
                finally:
                    await asyncio.sleep_ms(SERVICE_PAUSE_MS)
                left.discard(path)
            return True
        except Exception as error:
            log("WARN", "STAKE", "Remnants of %s stay (%s)." % (project_id, error))
            return False

    def has_data(self, project_id):
        """True when the folder of project_id holds a set file or progress.jsonl
        (one os.listdir); False for a missing folder or an ID that cannot have one."""
        if not valid_project_id(project_id):
            return False
        try:
            names = os.listdir(self.project_path(project_id))
        except OSError as error:
            if _errno(error) == _ENOENT:
                return False
            raise
        for name in names:
            if name == "progress.jsonl" or name.endswith(".json"):
                return True
        return False

    # --- project deletion (C6) ------------------------------------------

    def refuse_blocked(self, project_id=_MISSING, status=_CONFLICT):
        """Raise stakeout_recovery_pending (status) while project_id, or any
        project when it is omitted, is blocked: a live project whose folder
        stays set aside as "<project_id>.deleting" until the next start.
        Every project is blocked when the start-up reconciliation could not
        tell which folders are set aside (reconcile_deletions)."""
        if self._unresolved or (project_id in self._blocked if project_id is not _MISSING
                                else self._blocked):
            raise StakeoutError("stakeout_recovery_pending", "Target sets of a project "
                                "deletion wait for the next start; restart the device.",
                                status)

    def _block(self, level, project_id, message):
        log(level, "STAKE", message + "; the project is blocked until the next start.")
        self._blocked.add(project_id)
        self.invalidate(project_id)

    def begin_project_delete(self, project_id):
        """Set the sets of project_id aside before its deletion is journaled.

        Renames "<root>/<project_id>" to "<project_id>.deleting" beside it,
        the deletion intent, and returns whether there was a folder. It does
        not yield, so the caller journals right after it. Refused before
        that: a blocked project (409 stakeout_recovery_pending), an import
        or an open download of the project (409 storage_busy) and a folder
        an earlier deletion set aside (409 stakeout_cleanup_pending, settled
        by the next start). A failed rename is 507 stakeout_delete_failed
        with nothing changed; one whose outcome cannot be told also blocks
        the project. A project ID that is no valid stakeout ID (restored
        surveys allow others) has no folder: False, nothing touched.
        """
        if not valid_project_id(project_id):
            # Restored surveys allow other IDs; such a project has no sets.
            return False
        source = self.project_path(project_id)
        target = source + ".deleting"
        self.refuse_blocked(project_id)
        self._refuse_busy(project_id)
        try:
            if _present(target):
                raise _cleanup_pending()
            if not _present(source):
                return False
        except OSError:
            moved = False
        else:
            moved = _move(source, target)
        self.invalidate(project_id)
        if moved:
            return True
        if moved is None:
            self._block("ERROR", project_id, "%s may be set aside" % source)
        raise StakeoutError("stakeout_delete_failed", "The target sets of the project "
                            "could not be set aside.", _NO_STORAGE)

    def undo_project_delete(self, project_id):
        """Put the folder of begin_project_delete back: the deletion was not
        journaled. A failure is an ERROR and blocks the project."""
        source = self.project_path(project_id)
        if not _move(source + ".deleting", source):
            self._block("ERROR", project_id, "%s.deleting could not be put back" % source)
        self.invalidate(project_id)

    async def finish_project_delete(self, project_id):
        """After the deletion is journaled: forget the sets of project_id and
        remove "<project_id>.deleting" (_clear). Returns "done", or
        "pending" (WARN) when something is left for the next start."""
        if not valid_project_id(project_id):
            return "done"
        directory = self.project_path(project_id)
        self.invalidate(project_id)
        self._damaged = {path for path in self._damaged
                         if not path.startswith(directory + "/")}
        return "done" if await self._clear(directory + ".deleting") else "pending"

    async def _clear(self, directory, beat=None):
        """Remove directory and the files in it, one at a time with a service
        pause (and beat(), if given) after each attempt, also a failed one
        and the folder's own removal; True once it is gone. Any error is a
        WARN and leaves the rest for the next start."""
        try:
            try:
                names = os.listdir(directory)
            except OSError as error:
                if _errno(error) == _ENOENT:
                    return True
                raise
            # None stands for the folder itself, removed last.
            for name in names + [None]:
                try:
                    if name is None:
                        os.rmdir(directory)
                    else:
                        os.remove(directory + "/" + name)
                except OSError as error:
                    if name is None or _errno(error) != _ENOENT:
                        raise
                finally:
                    if beat is not None:
                        beat()
                    await asyncio.sleep_ms(SERVICE_PAUSE_MS)
            return True
        except Exception as error:
            log("WARN", "STAKE", "%s is not removed completely (%s); the next start "
                "tries again." % (directory, error))
            return False

    async def reconcile_deletions(self, tracker):
        """Settle the project deletions a restart interrupted (start-up hook).

        Tracker.initialize_async awaits it before the survey counts as
        loaded, so no request meets an unsettled folder. For every
        "<project_id>.deleting" in the root, by the loaded survey: a project
        that is gone was deleted, its folder is removed (_clear); a live
        project's deletion was not journaled, its folder is renamed back.
        When "<project_id>" exists as well or the rename fails, both stay
        (WARN) and the project is blocked (refuse_blocked). Nothing else is
        touched: an entry that is not a directory is left alone. Yields
        after every file and folder and keeps the tracking heartbeat; errors
        are WARNs and never raised. Until it has looked at every entry of
        the root (a missing root has none), every project counts as blocked:
        a root that cannot be listed leaves it so until the next start,
        without keeping the survey from loading.
        """
        from state import app

        def beat():
            app.beat("tracking")

        self._unresolved = True
        try:
            names = os.listdir(self.root)
        except Exception as error:
            if not (isinstance(error, OSError) and _errno(error) == _ENOENT):
                log("WARN", "STAKE", "Project deletions could not be checked (%s); every "
                    "project is blocked until the next start." % error)
                return
            names = ()
        live = [project["id"] for project in tracker.projects]
        for name in names:
            project_id = name[:-9]
            if not name.endswith(".deleting") or not valid_project_id(project_id):
                continue
            path = self.root + "/" + name
            source = self.project_path(project_id)
            try:
                if not os.stat(path)[0] & _DIRECTORY:
                    continue
                if project_id not in live:
                    await self._clear(path, beat)
                elif _present(source):
                    self._block("WARN", project_id, "%s stays beside %s" % (path, source))
                elif _move(path, source):
                    log("INFO", "STAKE", "The target sets of %s are back after an "
                        "interrupted deletion." % project_id)
                else:
                    self._block("WARN", project_id, "%s could not be renamed back" % path)
            except Exception as error:
                self._block("WARN", project_id, "%s stays (%s)" % (path, error))
            beat()
            await asyncio.sleep_ms(SERVICE_PAUSE_MS)
        self._unresolved = False

store = TargetStore()
