#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Synthetic GeoJSON export cases under a fixed clock; never reads device or backup data.

LARGE matches the device case (3531 features), LONG is one line of 3400
vertices. golden_entries() pins the SHA-256 and length of each export, written
by `python3 tests/export_case.py --write-golden`.
"""
import asyncio
import contextlib
import copy
import hashlib
import json
import os
import sys
import tempfile
from unittest import mock

import support  # noqa: F401 - installs the MicroPython compatibility modules
import tracking
from benchmark_tracking_scale import MEASUREMENT


LARGE = {"lines": 4, "vertices_per_line": 850, "points": 131}
LONG = {"lines": 1, "vertices_per_line": 3400, "points": 0}
EXPORT_CASE_EPOCH = 1_777_000_000   # host clock: 2026-04-24T03:06:40Z
PROJECT_ID = "synthetic-export"
GOLDEN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "fixtures", "geojson_export_golden.json")
# The free flash of tests/test_tracking_geojson_stream.py, so a restore never
# hits the capacity check of the host file system.
# Every object type but "stakeout" (only a stakeout confirmation writes one);
# the golden digests pin this cycle.
POINT_TYPES = tuple(name for name in tracking.OBJECT_TYPES if name != "stakeout")
STORAGE_CAPACITY = {"free_flash_bytes": 8_000_000, "required_flash_reserve_bytes": 3_000_000,
                    "estimated_temporary_bytes": 16_384, "writable": True}


@contextlib.contextmanager
def fixed_clock():
    """Wall clock at EXPORT_CASE_EPOCH and ticks_ms at 0 for the tracker.

    tracking._now_seconds() (the #39 assignment time) reads tracking.time.time.
    """
    # Plain functions, not mocks: ticks_ms is read tens of thousands of times
    # while a large case is built, and mock call bookkeeping would double that.
    with mock.patch("tracking.time.time", new=lambda: EXPORT_CASE_EPOCH), \
            mock.patch.object(tracking.time, "ticks_ms", new=lambda: 0, create=True):
        yield


def _measurement(**fields):
    value = copy.deepcopy(MEASUREMENT)
    value.update(fields)
    return value


def _backup(name, lines, points):
    return {"kind": "survey_backup", "schema_version": tracking.SCHEMA_VERSION,
            "active_project_id": PROJECT_ID, "active_line_id": None,
            "auto_distance_m": 1.0, "projects": [{"id": PROJECT_ID, "name": name,
            "description": "", "created_at": 1, "points": points, "lines": lines}]}


def _line(number, coordinates, measurements, properties=None, **fields):
    line = {"id": "synthetic-line-%d" % number, "name": "Synthetic line %d" % number,
            "state": "finished", "created_at": 1, "finished_at": 2,
            "coordinates": coordinates, "measurements": measurements,
            "properties": properties or {}}
    line.update(fields)
    return line


def _point(k, coordinate, **fields):
    projected = None
    if k % 2:
        projected = [coordinate[0] + 50, coordinate[1] + 50, coordinate[2]]
    return {"id": "synthetic-point-%d" % k,
            "object_type": POINT_TYPES[k % len(POINT_TYPES)],
            "name": "P-%d" % k, "note": "", "line_id": None, "chainage_m": None,
            "lateral_offset_m": None, "side": None, "projected_coordinate": projected,
            "coordinate": coordinate,
            "measurement": _measurement(source="asset", recorded_at=900000000 + k, **fields)}


def large_export_payload(lines=4, vertices_per_line=850, points=131):
    """A schema-5 JSON backup of one project with the given counts."""
    built = []
    for n in range(1, lines + 1):
        coordinates, measurements = [], []
        for i in range(vertices_per_line):
            coordinates.append([1000000000 + i * 100, 4000000000 + n * 1000,
                                100000 + i % 7 * 10])
            item = _measurement(recorded_at=800000000 + n * 10000 + i)
            if i % 10 == 0:
                item["reference_frame"] = "ETRS89 (synthetic)"
            measurements.append(item)
        built.append(_line(n, coordinates, measurements))
    assets = [_point(k, [1000500000 + k * 100, 4000500000, 100000 + k % 5 * 10])
              for k in range(1, points + 1)]
    return _backup("Synthetic export", built, assets)


def mixed_export_payload():
    """A small project covering the export's geometry, property and frame branches."""
    def vertex(i, line, **fields):
        return ([1000000000 + i * 100, 4000000000 + line * 1000, 100000 + i * 10],
                _measurement(recorded_at=800000000 + line * 100 + i, **fields))

    def line(number, vertices, properties=None, **fields):
        return _line(number, [c for c, _ in vertices], [m for _, m in vertices],
                     properties, **fields)

    frames = [{"reference_frame": "ETRS89 (synthetic)", "reference_frame_epoch": "2020.0"},
              {}, {"fix_quality": 5, "fix_status": "RTK_FLOAT"}, {}]
    lines = [
        line(1, []),
        line(2, [vertex(0, 2)]),
        line(3, [vertex(i, 3) for i in range(3)],
             {"material": 'PE "quoted"', "visible": True, "count": 3, "diameter": 1.25,
              "optional": None}, recording_mode="route"),
        line(4, [vertex(i, 4, **fields) for i, fields in enumerate(frames)],
             {"name": "exported name", "measurements": "external"}),
        line(5, [vertex(0, 5, fix_quality=1, fix_status="GPS", correction_age_sec=None,
                        station_id=None)]),
    ]
    points = [_point(1, [1000600000, 4000600000, 100500]),
              _point(2, [1000600100, 4000600000, 100600])]
    return _backup('Water "\u00dc" / network', lines, points)


def build(directory, payload):
    """A tracker reloaded from flash (FlashSequence lines) holding payload."""
    path = os.path.join(directory, "tracking")
    with fixed_clock():
        with mock.patch.object(tracking.Tracker, "storage_capacity",
                               return_value=dict(STORAGE_CAPACITY)):
            tracker = tracking.Tracker(path)
            try:
                tracker.restore(payload)
                tracker.compact()
            finally:
                tracker.point_store.close()
                tracker._sync_journal(True)
        return tracking.Tracker(path)


def close(tracker):
    tracker.point_store.close()
    tracker._sync_journal(True)


def _entry(tracker):
    with fixed_clock():
        raw = "".join(tracker.iter_geojson(PROJECT_ID)).encode("utf-8")
    return {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def _assign(tracker):
    with fixed_clock(), mock.patch.object(tracking.Tracker, "storage_capacity",
                                          return_value=dict(STORAGE_CAPACITY)):
        async def assign():
            preview = await tracker.frame_assignment_preview_async(PROJECT_ID)
            await tracker.assign_frame_async(PROJECT_ID, "ETRS89 (example)", "2010.0",
                                             preview["total"], preview["scope_token"])
        asyncio.run(assign())


def _drop_summaries(tracker):
    """Every line without its summary: the override branch counts the frames (E2)."""
    for line in tracker._project(PROJECT_ID)["lines"]:
        line.pop("_summary")


def golden_entries():
    """{case: {"bytes", "sha256"}} of the GeoJSON export of every case."""
    entries = {}
    with tempfile.TemporaryDirectory(prefix="esp-rtk-export-case-") as root:
        for name, payload, prepare in (
                ("large", large_export_payload(**LARGE), None),
                ("long", large_export_payload(**LONG), None),
                ("mixed", mixed_export_payload(), None),
                ("mixed_stale_summary", mixed_export_payload(), _drop_summaries),
                ("assigned", mixed_export_payload(), _assign)):
            directory = os.path.join(root, name)
            os.mkdir(directory)
            tracker = build(directory, payload)
            try:
                if prepare:
                    prepare(tracker)
                entries[name] = _entry(tracker)
            finally:
                close(tracker)
    return entries


def write_golden(path=GOLDEN_PATH):
    with open(path, "w", encoding="utf-8") as target:
        json.dump(golden_entries(), target, sort_keys=True, indent=2)
        target.write("\n")


if __name__ == "__main__":
    if sys.argv[1:] != ["--write-golden"]:
        sys.exit("usage: python3 tests/export_case.py --write-golden")
    write_golden()
