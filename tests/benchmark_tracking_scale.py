#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Synthetic flash-backed scaling benchmark; never reads device or backup data."""
import copy
import json
import os
import tempfile
import time
import tracemalloc

import support  # noqa: F401 - installs the MicroPython compatibility modules
import tracking


MEASUREMENT = {
    "recorded_at": 1, "fix_quality": 4, "fix_status": "RTK_FIXED",
    "satellites": 24, "hdop": 0.6, "correction_age_sec": 0.5,
    "station_id": "SYNTHETIC", "source": "automatic",
    "accuracy_observation": {"samples": 5, "rtk_fixed_samples": 5,
        "horizontal_mean_deviation_m": 0.002, "horizontal_max_deviation_m": 0.004,
        "vertical_mean_deviation_m": 0.003, "vertical_max_deviation_m": 0.006},
    "receiver_accuracy": {"source": "NMEA_GST", "samples": 5,
        "expected_samples": 5, "horizontal_sigma_mean_m": 0.01,
        "horizontal_sigma_max_m": 0.02, "vertical_sigma_mean_m": 0.02,
        "vertical_sigma_max_m": 0.03},
    "quality": {"accepted": True, "reasons": [], "limits": {
        "fix_quality": "RTK_FIXED", "min_satellites": 10, "max_hdop": 1.5,
        "max_correction_age_sec": 5.0, "max_horizontal_spread_m": 0.05,
        "max_gst_horizontal_sigma_m": 0.05, "max_gst_vertical_sigma_m": 0.1,
        "quality_window_samples": 5}}}


def payload(count):
    measurements, coordinates = [], []
    for index in range(count):
        item = copy.deepcopy(MEASUREMENT); item["recorded_at"] = index + 1
        measurements.append(item)
        coordinates.append([10.0 + index * 0.000001, 40.0, 100.0 + index % 7 / 100])
    return {"kind": "survey_backup", "schema_version": 2,
        "active_project_id": "synthetic-project", "active_line_id": None,
        "auto_distance_m": 1.0, "projects": [{"id": "synthetic-project",
        "name": "Synthetic scale data", "description": "", "created_at": 1,
        "points": [], "lines": [{"id": "synthetic-line", "name": "Synthetic line",
        "state": "finished", "created_at": 1, "finished_at": count,
        "coordinates": coordinates, "measurements": measurements, "properties": {}}]}]}


def run(count):
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "tracking")
        source = payload(count)
        tracemalloc.start(); before, started = tracemalloc.get_traced_memory()[0], time.perf_counter()
        tracker = tracking.Tracker(path)
        tracker.restore(source)
        restore_sec = time.perf_counter() - started
        started = time.perf_counter(); tracker.compact()
        checkpoint_sec = time.perf_counter() - started
        current, peak = tracemalloc.get_traced_memory(); tracemalloc.stop()
        started = time.perf_counter()
        exported = sum(len(row) for row in tracker.iter_backup_ndjson())
        export_sec = time.perf_counter() - started
        started = time.perf_counter(); reloaded = tracking.Tracker(path)
        start_sec = time.perf_counter() - started
        started = time.perf_counter(); reloaded.status()
        api_status_sec = time.perf_counter() - started
        flash = sum(os.path.getsize(name) for _, name in reloaded._segment_files())
        flash += os.path.getsize(reloaded.checkpoint_path)
        flash += reloaded.point_store.storage_bytes()
        return {"active_points": count, "ram_before_bytes": before,
            "ram_after_bytes": current, "ram_peak_bytes": peak,
            "start_seconds": round(start_sec, 3), "restore_seconds": round(restore_sec, 3),
            "checkpoint_seconds": round(checkpoint_sec, 3),
            "export_seconds": round(export_sec, 3), "export_bytes": exported,
            "status_seconds": round(api_status_sec, 4),
            "max_append_ms": tracker.max_append_ms,
            "flash_bytes": flash, "point_cache_limit": reloaded.point_store.cache_size,
            "profiles": len(reloaded.profiles)}


def archived_run():
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "tracking")
        tracker = tracking.Tracker(path)
        first = payload(10000)
        tracker.restore(first)
        archive_a = tracker.archive_project("synthetic-project")
        second = payload(10000)
        second["active_project_id"] = "synthetic-project-2"
        second["projects"][0]["id"] = "synthetic-project-2"
        second["projects"][0]["lines"][0]["id"] = "synthetic-line-2"
        tracker.restore(second)
        archive_b = tracker.archive_project("synthetic-project-2")
        started = time.perf_counter(); reloaded = tracking.Tracker(path)
        start_sec = time.perf_counter() - started
        return {"archived_points": 20000, "ram_loaded_points": 0,
                "archive_files_bytes": archive_a["size_bytes"] + archive_b["size_bytes"],
                "archive_count": len(reloaded.archives),
                "active_points": reloaded.feature_count(),
                "start_seconds": round(start_sec, 3)}


if __name__ == "__main__":
    tracking.MAX_FEATURES = tracking.TARGET_MAX_FEATURES
    results = [run(count) for count in (5000, 8000, 10000)]
    results.append(archived_run())
    print(json.dumps({"synthetic": True, "schema_version": tracking.SCHEMA_VERSION,
                      "results": results}, indent=2))
