#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Synthetic host benchmark: GeoJSON against backup export through the real senders.

Never reads device or backup data. Each format runs twice on a cold point
cache: once timed with read counters and a competing service task, once under
tracemalloc for the heap peak (tracemalloc would distort the timing).
"""
import argparse
import asyncio
import json
import os
import subprocess
import tempfile
import time
import tracemalloc
from unittest import mock

import support  # noqa: F401 - installs the MicroPython compatibility modules
import export_case
import tracking
import web
from pointstore import FlashPointStore


class _DiscardingWriter:
    """Counts the body bytes after the HTTP header and keeps nothing."""

    def __init__(self):
        self.header = b""
        self.body_bytes = 0

    def write(self, data):
        data = bytes(data)
        if self.body_bytes == 0 and b"\r\n\r\n" not in self.header:
            self.header += data
            head, separator, rest = self.header.partition(b"\r\n\r\n")
            if separator:
                self.header, self.body_bytes = head + separator, len(rest)
            return
        self.body_bytes += len(data)

    async def drain(self):
        pass


async def _send(tracker, project_id, fmt, writer):
    if fmt == "geojson":
        # The export path of the dispatcher; measure() silences its log line.
        chunks = tracker.iter_geojson(project_id)
        await web._send_geojson_export(writer, chunks, "GET", next(chunks),
                                       tracker.point_store)
    elif fmt == "backup":
        await web._send_tracking_backup(writer, "GET", project_id, source_tracker=tracker)
    else:
        raise ValueError("format must be geojson or backup")


async def _timed(tracker, project_id, fmt):
    writer, turns, decodes = _DiscardingWriter(), [], [0]
    decode = FlashPointStore._decode

    def counted_decode(store, value, include_measurement=True):
        decodes[0] += 1
        return decode(store, value, include_measurement)

    async def another_service():
        last = 0
        while True:
            turns.append(decodes[0] - last)
            last = decodes[0]
            await asyncio.sleep(0)

    task = asyncio.create_task(another_service())
    await asyncio.sleep(0)
    try:
        with mock.patch.object(FlashPointStore, "_decode", autospec=True,
                               side_effect=counted_decode), \
                mock.patch("pointstore.open", wraps=open, create=True) as opened:
            started = time.perf_counter()
            await _send(tracker, project_id, fmt, writer)
            seconds = time.perf_counter() - started
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    turns.append(decodes[0] - sum(turns))
    return {"format": fmt, "bytes": writer.body_bytes, "seconds": round(seconds, 3),
            "opens": opened.call_count, "decodes": decodes[0], "loop_turns": len(turns),
            "max_decodes_per_turn": max(turns)}


async def _unbounded(awaitable, _timeout):
    return await awaitable


async def _heap_peak(tracker, project_id, fmt):
    # CPython's wait_for leaves every cancelled drain deadline in the loop's
    # timer heap until the next loop turn (about 5 KB in the large case); that
    # is host bookkeeping, not the export's. The device heap run is E19.
    tracemalloc.start()
    try:
        with mock.patch.object(web.asyncio, "wait_for", _unbounded):
            tracemalloc.reset_peak()
            before = tracemalloc.get_traced_memory()[0]
            await _send(tracker, project_id, fmt, _DiscardingWriter())
            return tracemalloc.get_traced_memory()[1] - before
    finally:
        tracemalloc.stop()


def measure(tracker, project_id, fmt):
    """One export of project_id as fmt ("geojson" or "backup") on a cold cache."""
    # The real clock: a sender that yields by elapsed time needs ticks_ms.
    with mock.patch.object(web, "log"):
        tracker.point_store.close_cache()
        result = asyncio.run(_timed(tracker, project_id, fmt))
        tracker.point_store.close_cache()
        result["tracemalloc_peak_bytes"] = asyncio.run(_heap_peak(tracker, project_id, fmt))
    return result


def _base():
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True,
                              text=True, check=True,
                              cwd=os.path.dirname(os.path.abspath(__file__))).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def run(case):
    counts = {"large": export_case.LARGE, "long": export_case.LONG}[case]
    with tempfile.TemporaryDirectory(prefix="esp-rtk-export-benchmark-") as directory:
        tracker = export_case.build(directory, export_case.large_export_payload(**counts))
        try:
            results = [measure(tracker, export_case.PROJECT_ID, fmt)
                       for fmt in ("geojson", "backup")]
        finally:
            export_case.close(tracker)
    return {"benchmark": "export", "synthetic": True, "base": _base(),
            "schema_version": tracking.SCHEMA_VERSION, "case": dict(counts, name=case),
            "results": results,
            "ratio_seconds": round(results[0]["seconds"] / results[1]["seconds"], 3)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--case", choices=("large", "long"), default="large")
    print(json.dumps(run(parser.parse_args().case), indent=2))
