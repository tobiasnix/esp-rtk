# SPDX-License-Identifier: AGPL-3.0-only
"""Stakeout export: the targets of the visible sets as GeoJSON or CSV (E12).

Imported by the export route only (web._stakeout_export), so it costs RAM
only while exports run. A Snapshot copies the status of every target (fold
entries, floors, project floor and point index) in the synchronous block
before the first byte, so the export shows the state at its start whatever
changes meanwhile (R9). The generators read each set file in SCAN_PIECE
blocks while a SHA-256 runs over its body: GeoJSON first copies the part
lines byte for byte (from byte 0 up to targets_offset), then makes one Point
feature per target line; CSV makes one row per target line. A digest that
differs from the summary's damages the set and breaks the stream off before
its end (no closing "]}").
"""
import ujson
import geo
import stakeout
import stakeout_progress
from tracking import _sample_counts, _utc_fields

# What closes the parts and opens the targets of a set file (targets_offset follows).
_TARGETS_HEAD = b'\n],\n"targets": [\n'
CSV_HEADER = ("tid,set_id,part_id,seq,gen,name,target_longitude,target_latitude,"
              "target_altitude_m,status,mode,source,confirmed_at,confirmed_at_utc,"
              "clock_unsynced,actual_longitude,actual_latitude,actual_altitude_msl_m,"
              "actual_quality,dn_m,de_m,dist_m,tolerance_m,point_id,fix_quality,samples,"
              "gst_horizontal_sigma_max_m,horizontal_spread_max_m,correction_age_sec,note")
# Per CSV column: n for a number (kept as it is), t for text (formula guard).
_CSV_KINDS = "tttnntnnntttnttnnntnnnntnnnnnt"
_TYPES = {"geojson": "application/geo+json", "csv": "text/csv; charset=utf-8"}


def kind(form, project_id, set_id=None):
    """(generator function, content type, file name) of the export format
    form ("geojson" or "csv"; 400 invalid_request for another)."""
    if form not in _TYPES:
        raise stakeout.StakeoutError("invalid_request", "format must be geojson or csv.")
    return ((iter_csv if form == "csv" else iter_geojson), _TYPES[form],
            "stakeout-%s%s.%s" % (project_id, "-" + set_id if set_id else "", form))


async def readable(store, project_id, fingerprint, set_id=None):
    """[(entry, path)] of the sets visible under fingerprint, in overview
    order; with set_id only that set (400 invalid_request for an invalid ID,
    404 set_not_found when it is not visible)."""
    if set_id is not None:
        return [await store._find(project_id, fingerprint, set_id)]
    return [(entry, store.set_path(project_id, entry["summary"]["set_id"]))
            for entry in await store._entries(project_id)
            if stakeout._visible(entry, fingerprint)]


class Snapshot:
    """The status of every target of a project at one moment: copies of the
    fold's entries, floors and project floor and of its point index (the
    points themselves by reference; a stored point never changes)."""

    def __init__(self, fold, points):
        index = fold._index_of(points)
        self.index = dict((set_id, dict(targets)) for set_id, targets in index.items())
        self.entries = dict((set_id, dict(values)) for set_id, values in fold.entries.items())
        self.floors = dict(fold.floors)
        self.base = fold.base
        self.exported_at = stakeout.now_seconds()

    def state(self, tid):
        """(status, mode, gen, point) of the target tid (stakeout_progress.derive)."""
        set_id, part, seq = stakeout.parse_tid(tid)
        key = stakeout.target_key(part, seq)
        return stakeout_progress.derive(self.entries.get(set_id, {}).get(key, 0),
                                        max(self.floors.get(set_id, 0), self.base),
                                        self.index.get(set_id, {}).get(key))


def observation(point):
    """The observation of a stakeout point (None for none): its position,
    offsets and measurement quality."""
    if point is None:
        return None
    measurement = point["measurement"]
    coordinate = point["coordinate"]
    receiver = measurement.get("receiver_accuracy") or {}
    return {"point_id": point["id"], "recorded_at": measurement.get("recorded_at"),
            "lat_e8": coordinate[1], "lon_e8": coordinate[0], "alt_mm": coordinate[2],
            "dn_m": point["stakeout_dn_m"], "de_m": point["stakeout_de_m"],
            "dist_m": point["stakeout_dist_m"], "tolerance_m": point["stakeout_tolerance_m"],
            "accepted": (measurement.get("quality") or {}).get("accepted"),
            "fix_quality": measurement.get("fix_quality"),
            "samples": _sample_counts(measurement)[0],
            "gst_h_sigma_max_m": receiver.get("horizontal_sigma_max_m",
                                              receiver.get("semi_major_sigma_m")),
            "spread_max_m": (measurement.get("accuracy_observation") or {}).get(
                "horizontal_max_deviation_m"),
            "correction_age_sec": measurement.get("correction_age_sec"),
            "note": point.get("note")}


def _blocks(path, digest, at, end):
    """The bytes at..end of the file path in SCAN_PIECE blocks, each fed to
    digest; fewer when the file is shorter."""
    with open(path, "rb") as source:
        source.seek(at)
        while at < end:
            block = source.read(min(stakeout.SCAN_PIECE, end - at))
            if not block:
                return
            digest.update(block)
            at += len(block)
            yield block


def _parts(entry, path, digest):
    """The part lines of a set file as text, byte for byte: everything after
    the head's two line breaks up to the targets head; digest gets every
    byte up to targets_offset. One piece per block ("" for none); a piece
    ends before a cut multibyte character, which opens the next one."""
    offset = entry["offset"]
    end = offset - len(_TARGETS_HEAD)
    begin = None
    breaks = at = 0
    rest = b""
    for block in _blocks(path, digest, 0, offset):
        start = 0
        while begin is None and breaks < 2:
            start = block.find(b"\n", start) + 1
            if not start:
                break
            breaks += 1
            if breaks == 2:
                begin = at + start
        if begin is not None:
            rest += block[max(begin - at, 0):max(end - at, 0)]
        at += len(block)
        cut = len(rest)
        # An ASCII byte always ends a character in UTF-8.
        while cut and rest[cut - 1] >= 0x80:
            cut -= 1
        yield rest[:cut].decode()
        rest = rest[cut:]


def _rows(store, project_id, entry, path, digest, make):
    """make(target) for every target line of a set file, then the digest
    check of the body (digest has seen it up to targets_offset). A line that
    is no target, a missing end or another digest damages the set (the
    StakeoutError ends the stream). At least one item per block ("" for a
    block within one long line), so the stream paces per block."""
    rest = b""
    try:
        for block in _blocks(path, digest, entry["offset"], entry["body"]):
            lines = (rest + block).split(b"\n")
            rest = lines.pop()
            if len(rest) > stakeout.TARGET_LINE_MAX:
                raise ValueError
            if not lines:
                yield ""
            for line in lines:
                yield make(ujson.loads(line.rstrip(b",")))
        # The "]" that closes the targets is the body's last byte.
        intact = rest == b"]" and stakeout._hex(digest.digest()) == entry["digest"]
    except (ValueError, KeyError, TypeError):
        intact = False
    if not intact:
        raise store._damage(project_id, path, entry["summary"]["set_id"])


def _coordinates(lon_e8, lat_e8, alt_mm):
    text = geo.format_e8(lon_e8) + "," + geo.format_e8(lat_e8)
    return text if alt_mm is None else text + "," + geo.format_mm(alt_mm)


def _feature(snapshot, target):
    tid = target["tid"]
    status, mode, gen, point = snapshot.state(tid)
    alt = target["alt_import"]
    properties = {"tid": tid, "set_id": tid[:11], "part_id": target["part_id"],
                  "seq": target["seq"], "name": target["name"], "props": target["props"],
                  "alt_import": alt, "status": status, "mode": mode, "gen": gen,
                  "source": mode and "stakeout_" + mode, "observation": observation(point)}
    return ('{"type": "Feature", "id": ' + ujson.dumps(tid) +
            ', "geometry": {"type": "Point", "coordinates": [' +
            _coordinates(target["lon_e8"], target["lat_e8"], alt) +
            ']}, "properties": ' + ujson.dumps(properties) + "}")


def iter_geojson(store, snapshot, project, sets):
    """The GeoJSON export of sets (readable) as text pieces: a FeatureCollection
    with kind, schema, project_id, exported_at and stakeout_sets (each set's
    set_id, state, source, imported_at and its part lines byte for byte),
    then one Point feature per target of an intact set, in set file order."""
    project_id = project["id"]
    yield ujson.dumps({"type": "FeatureCollection", "name": project.get("name"),
                       "kind": "stakeout_export", "schema": 1, "project_id": project_id,
                       "exported_at": snapshot.exported_at})[:-1] + ', "stakeout_sets": ['
    digests = []
    comma = ""
    for entry, path in sets:
        summary = entry["summary"]
        yield comma + ujson.dumps({"set_id": summary["set_id"], "state": summary["state"],
                                   "source": summary.get("source"),
                                   "imported_at": summary.get("imported_at")})[:-1] + \
            ', "parts": ['
        comma = ", "
        digest = None
        if summary["state"] == "ok":
            digest = stakeout.hashlib.sha256()
            for piece in _parts(entry, path, digest):
                yield piece
        digests.append(digest)
        yield "]}"
    yield '], "features": ['
    comma = ""
    for (entry, path), digest in zip(sets, digests):
        if digest is not None:
            for feature in _rows(store, project_id, entry, path, digest,
                                 lambda target: _feature(snapshot, target)):
                if feature:
                    yield comma + feature
                    comma = ",\n"
                else:
                    yield feature
    yield "]}"


def _cell(value, kind):
    """One quoted CSV cell; a text cell that a spreadsheet would read as a
    formula gets a leading apostrophe (numbers keep their minus)."""
    text = "" if value is None else str(value)
    if kind == "t" and text.startswith(("=", "+", "-", "@")):
        text = "'" + text
    return '"' + text.replace('"', '""') + '"'


def _fixed(value, formatter):
    return None if value is None else formatter(value)


def _row(snapshot, target):
    tid = target["tid"]
    status, mode, gen, point = snapshot.state(tid)
    seen = observation(point) or {}
    utc = unsynced = quality = None
    if seen:
        utc, unsynced = _utc_fields(seen["recorded_at"])
        unsynced = "true" if unsynced else "false"
        quality = (mode if mode != "manual" else
                   "gate_accepted" if seen["accepted"] else "not_verified")
    values = (tid, tid[:11], target["part_id"], target["seq"], gen, target["name"],
              geo.format_e8(target["lon_e8"]), geo.format_e8(target["lat_e8"]),
              _fixed(target["alt_import"], geo.format_mm), status, mode,
              mode and "stakeout_" + mode, seen.get("recorded_at"), utc, unsynced,
              _fixed(seen.get("lon_e8"), geo.format_e8),
              _fixed(seen.get("lat_e8"), geo.format_e8),
              _fixed(seen.get("alt_mm"), geo.format_mm), quality, seen.get("dn_m"),
              seen.get("de_m"), seen.get("dist_m"), seen.get("tolerance_m"),
              seen.get("point_id"), seen.get("fix_quality"), seen.get("samples"),
              seen.get("gst_h_sigma_max_m"), seen.get("spread_max_m"),
              seen.get("correction_age_sec"), seen.get("note"))
    return ",".join([_cell(value, kind) for value, kind in zip(values, _CSV_KINDS)]) + "\r\n"


def iter_csv(store, snapshot, project, sets):
    """The CSV export of sets (readable) as text pieces: CSV_HEADER, then one
    row per target of an intact set, every cell quoted, CRLF line ends."""
    yield CSV_HEADER + "\r\n"
    for entry, path in sets:
        if entry["summary"]["state"] == "ok":
            digest = stakeout.hashlib.sha256()
            for _ in _blocks(path, digest, 0, entry["offset"]):
                yield ""
            for row in _rows(store, project["id"], entry, path, digest,
                             lambda target: _row(snapshot, target)):
                yield row
