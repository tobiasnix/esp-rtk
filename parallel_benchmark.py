# SPDX-License-Identifier: AGPL-3.0-only
"""Isolated tracking load controlled over authenticated HTTP or legacy USB."""
import gc
import os
import time
import ujson
import uasyncio as asyncio

from state import app, shutdown_event, _instances
from tracking import Tracker

CONFIG_PATH = "parallel-benchmark.json"
PREFIX = "parallel-benchmark-v15"
RESULT_PATH = PREFIX + ".result.json"
ALLOWED_TARGETS = (5000, 8000, 10000, 15000, 20000)
PROFILES = {
    "local": ("wifi", "gnss", "ntrip", "http"),
    "ble": ("wifi", "gnss", "ntrip", "http", "ble"),
    "tcp": ("wifi", "gnss", "ntrip", "http", "tcp"),
    "parallel": ("wifi", "gnss", "ntrip", "http", "ble", "tcp"),
}
SERVICE_GRACE_MS = 10000
SERVICE_STARTUP_TIMEOUT_MS = 600000
ERROR_COUNTERS = ("http_errors", "task_restarts", "tracking_queue_overflows",
                  "nmea_queue_overflows", "ble_drops", "tcp_drops",
                  "tracking_write_errors", "uart_buffer_overflows",
                  "gga_parse_errors", "nmea_crc_errors")
UART_DIAGNOSTIC_COUNTERS = (
    "uart_rx_reads", "uart_rx_bytes", "uart_rx_available_max", "uart_rx_chunk_max",
    "uart_last_read_bytes", "uart_last_read_ticks_ms", "uart_last_read_duration_ms",
    "uart_read_gap_ms", "uart_read_gap_max_ms", "uart_rx_rate_bytes_sec",
    "uart_rx_rate_max_bytes_sec", "uart_rx_rate_window_ms", "uart_buffer_peak_bytes",
    "uart_last_buffer_overflow")


def control_status():
    result = None
    try:
        with open(RESULT_PATH) as source:
            result = ujson.loads(source.read())
    except (OSError, ValueError):
        pass
    tracker = _instances.get("parallel_tracker")
    return {"state": app.stats.get("parallel_benchmark_state", "idle"),
            "active": tracker is not None,
            "points": app.stats.get("parallel_benchmark_points", 0),
            "target": app.stats.get("parallel_benchmark_target"),
            "run_id": app.stats.get("parallel_benchmark_run_id"),
            "profile": app.stats.get("parallel_benchmark_profile"),
            "required_services": (list(tracker._parallel_required_services) if tracker else []),
            "services_seen": app.stats.get("parallel_benchmark_services_seen", 0),
            "live": ({"append_max_ms": tracker.max_append_ms,
                      "append_profile_us": tracker.append_profile(),
                      "services": _service_snapshot(),
                      "error_deltas": _error_deltas(tracker)} if tracker else None),
            "result": result}


def _save_control(value):
    temporary = CONFIG_PATH + ".tmp"
    with open(temporary, "w") as target:
        target.write(ujson.dumps(value))
    with open(temporary) as source:
        if ujson.loads(source.read()) != value:
            raise OSError("Benchmark configuration verification failed.")
    # A previous configuration remains recoverable until the replacement exists.
    previous = CONFIG_PATH + ".previous"
    try: os.remove(previous)
    except OSError: pass
    try: os.rename(CONFIG_PATH, previous)
    except OSError: pass
    os.rename(temporary, CONFIG_PATH)
    try: os.remove(previous)
    except OSError: pass


def control(action, production_tracker, target=None, confirm=None, profile="parallel"):
    """Stage bounded actions; the HTTP handler schedules a normal restart."""
    active = _instances.get("parallel_tracker") is not None
    if action == "start":
        if active or app.stats.get("parallel_benchmark_state") in ("armed", "stopping"):
            raise ValueError("Stop the existing benchmark before starting another run.")
        if type(target) is not int or target not in ALLOWED_TARGETS:
            raise ValueError("Unsupported benchmark target.")
        if not isinstance(profile, str) or profile not in PROFILES:
            raise ValueError("Unsupported benchmark profile.")
        if production_tracker.active_line_id or production_tracker._pending_fixes:
            raise ValueError("Finish the active production line before benchmarking.")
        if not production_tracker.storage_capacity(16384, force=True)["writable"]:
            raise ValueError("Insufficient flash reserve for the benchmark.")
        # A new WLAN run must cover the entire requested population under load.
        if any(name.startswith(PREFIX + ".") for name in os.listdir()):
            raise ValueError("Export and clean up the previous synthetic run first.")
        import ubinascii
        run_id = ubinascii.hexlify(os.urandom(8)).decode()
        _save_control({"kind": "parallel_tracking_benchmark", "schema": 1,
                       "enabled": True, "target_points": target, "interval_ms": 1000,
                       "control": "wifi", "run_id": run_id, "profile": profile})
        app.stats["parallel_benchmark_state"] = "armed"
        return {"state": "armed", "run_id": run_id, "profile": profile,
                "reboot_required": True}
    if action == "stop":
        _save_control({"kind": "parallel_tracking_benchmark", "schema": 1,
                       "enabled": False})
        app.stats["parallel_benchmark_state"] = "stopping"
        return {"state": "stopping", "reboot_required": True}
    if action == "cleanup":
        if active or app.stats.get("parallel_benchmark_state") in ("armed", "stopping"):
            raise ValueError("Stop the benchmark and wait for restart before cleanup.")
        if confirm != PREFIX:
            raise ValueError("Exact synthetic benchmark confirmation is required.")
        for name in os.listdir():
            if (name.startswith(PREFIX + ".") or name in
                    (CONFIG_PATH, CONFIG_PATH + ".tmp", CONFIG_PATH + ".previous")):
                os.remove(name)
        app.stats["parallel_benchmark_state"] = "idle"
        return {"state": "idle", "reboot_required": False}
    raise ValueError("Unknown benchmark action.")


def load_config():
    found = False
    for candidate in (CONFIG_PATH, CONFIG_PATH + ".tmp", CONFIG_PATH + ".previous"):
        try:
            with open(candidate, "r") as source:
                value = ujson.loads(source.read())
            found = True
        except (OSError, ValueError, TypeError):
            continue
        if (isinstance(value, dict) and value.get("kind") == "parallel_tracking_benchmark"
                and value.get("schema") == 1 and value.get("enabled") is False):
            return None
        if (not isinstance(value, dict) or
                value.get("kind") != "parallel_tracking_benchmark" or
                value.get("schema") != 1 or value.get("enabled") is not True or
                value.get("target_points") not in ALLOWED_TARGETS or
                value.get("interval_ms") != 1000):
            continue
        profile = value.get("profile", "parallel")
        if not isinstance(profile, str) or profile not in PROFILES:
            continue
        if candidate != CONFIG_PATH:
            try: os.remove(CONFIG_PATH)
            except OSError: pass
            os.rename(candidate, CONFIG_PATH)
        return {"target_points": int(value["target_points"]), "interval_ms": 1000,
                "control": value.get("control", "usb"), "run_id": value.get("run_id"),
                "profile": profile}
    if found:
        app.stats["parallel_benchmark_state"] = "invalid_config"
    return None


def prepare(production_tracker):
    config = load_config()
    if config is None:
        return None, None
    # The benchmark is deliberately isolated from normal tracking files, but a
    # real active/archive inventory would make flash results ambiguous and raises
    # the risk of an operator confusing test and field data.
    if ((production_tracker.projects or production_tracker.archives)
            and config.get("control") != "wifi"):
        app.stats["parallel_benchmark_state"] = "refused_real_inventory"
        return None, None
    if production_tracker.active_line_id or production_tracker._pending_fixes:
        app.stats["parallel_benchmark_state"] = "refused_active_recording"
        return None, None
    tracker = Tracker(PREFIX, max_features=config["target_points"])
    if tracker.archives:
        app.stats["parallel_benchmark_state"] = "refused_test_archives"
        tracker.point_store.close()
        return None, None
    if not tracker.projects:
        project = tracker.create_project("Synthetic parallel benchmark")
        tracker.select_project(project["id"])
        tracker.start_line("Synthetic one-hertz line", 0.2)
    if len(tracker.projects) != 1 or not tracker.active_line_id:
        app.stats["parallel_benchmark_state"] = "invalid_test_inventory"
        tracker.point_store.close()
        return None, None
    app.stats.update({
        "parallel_benchmark_state": "resuming" if tracker.feature_count() else "starting",
        "parallel_benchmark_target": config["target_points"],
        "parallel_benchmark_points": tracker.feature_count(),
        "parallel_benchmark_generated": 0,
        "parallel_benchmark_queue_max": len(tracker._pending_fixes),
        "parallel_benchmark_heap_min": gc.mem_free(),
        "parallel_benchmark_run_id": config.get("run_id"),
        "parallel_benchmark_profile": config["profile"],
    })
    tracker._parallel_profile = config["profile"]
    tracker._parallel_required_services = PROFILES[config["profile"]]
    tracker._parallel_initial_points = tracker.feature_count()
    tracker._parallel_production_points = production_tracker.feature_count()
    tracker._parallel_baseline = _service_snapshot()
    tracker._parallel_evidence = dict((key, False) for key in
        ("wifi", "gnss", "ntrip", "ble", "tcp", "http"))
    tracker._parallel_last_progress = {}
    tracker._parallel_previous = dict(tracker._parallel_baseline)
    tracker._parallel_services_continuous = True
    tracker._parallel_run_started = False
    return tracker, config


def _synthetic_fix(index):
    return {"lat": 40.0, "lon": 10.0 + index * 0.00001, "alt": 100.0,
            "utc": index, "qual": 4, "fix_status_text": "RTK_FIXED",
            "sats": 24, "hdop": 0.6, "correction_age_sec": 0.5,
            "station_id": "SYNTHETIC", "receiver_accuracy": {
                "source": "NMEA_GST", "semi_major_sigma_m": 0.02,
                "altitude_sigma_m": 0.04}}


def _service_snapshot():
    ble = _instances.get("ble")
    try:
        from fanout import clients as tcp_clients
        tcp_count = len(tcp_clients)
    except (ImportError, AttributeError):
        tcp_count = 0
    return {
        "access_state": app.stats.get("access_state"),
        "ntrip_state": app.stats.get("ntrip_state"),
        "ntrip_bytes": app.stats.get("ntrip_bytes", 0),
        "gnss_messages": app.stats.get("gnss_msgs", 0),
        "ble_clients": len(ble.connections) if ble else 0,
        "tcp_clients": tcp_count,
        "ble_bytes": app.stats.get("ble_tx_bytes", 0),
        "tcp_bytes": app.stats.get("tcp_tx_bytes", 0),
        "http_errors": app.stats.get("http_errors", 0),
        "http_requests": app.stats.get("http_requests", 0),
        "task_restarts": app.stats.get("task_restarts", 0),
        "tracking_queue_overflows": app.stats.get("tracking_queue_overflows", 0),
        "nmea_queue_overflows": app.stats.get("queue_overflows", 0),
        "ble_drops": app.stats.get("ble_drops", 0),
        "tcp_drops": app.stats.get("nmea_tcp_drops", 0),
        "ble_queue_overflows": app.stats.get("ble_queue_overflows", 0),
        "ble_notify_retries": app.stats.get("ble_notify_retries", 0),
        "ble_notify_timeouts": app.stats.get("ble_notify_timeouts", 0),
        "ble_notify_errors": app.stats.get("ble_notify_errors", 0),
        "ble_last_notify_errno": app.stats.get("ble_last_notify_errno"),
        "ble_queue_max": app.stats.get("ble_queue_max", 0),
        "tcp_queue_overflows": app.stats.get("tcp_queue_overflows", 0),
        "tcp_write_timeouts": app.stats.get("tcp_write_timeouts", 0),
        "tcp_write_errors": app.stats.get("tcp_write_errors", 0),
        "tcp_write_max_ms": app.stats.get("tcp_write_max_ms", 0),
        "tcp_queue_max": app.stats.get("tcp_queue_max", 0),
        "tracking_write_errors": app.stats.get("tracking_write_errors", 0),
        "uart_buffer_overflows": app.stats.get("uart_buffer_overflows", 0),
        "gga_parse_errors": app.stats.get("gga_parse_errors", 0),
        "nmea_crc_errors": app.stats.get("crc_errors", 0),
    }


def _update_service_evidence(tracker):
    current = _service_snapshot(); baseline = tracker._parallel_baseline
    evidence = tracker._parallel_evidence
    evidence["wifi"] |= current["access_state"] == "ONLINE"
    evidence["gnss"] |= current["gnss_messages"] > baseline["gnss_messages"]
    evidence["ntrip"] |= (current["ntrip_state"] == "streaming" and
                          current["ntrip_bytes"] > baseline["ntrip_bytes"])
    evidence["ble"] |= current["ble_clients"] > 0 and current["ble_bytes"] > baseline["ble_bytes"]
    evidence["tcp"] |= current["tcp_clients"] > 0 and current["tcp_bytes"] > baseline["tcp_bytes"]
    evidence["http"] |= current["http_requests"] > baseline["http_requests"]
    app.stats["parallel_benchmark_services_seen"] = sum(
        1 for value in evidence.values() if value)
    previous = tracker._parallel_previous
    now = time.ticks_ms()
    conditions = {
        "wifi": current["access_state"] == "ONLINE",
        "gnss": current["gnss_messages"] > previous["gnss_messages"],
        "ntrip": current["ntrip_state"] == "streaming" and current["ntrip_bytes"] > previous["ntrip_bytes"],
        "ble": current["ble_clients"] > 0 and current["ble_bytes"] > previous["ble_bytes"],
        "tcp": current["tcp_clients"] > 0 and current["tcp_bytes"] > previous["tcp_bytes"],
        "http": current["http_requests"] > previous["http_requests"],
    }
    for key, progressing in conditions.items():
        if progressing:
            tracker._parallel_last_progress[key] = now
    tracker._parallel_previous = dict(current)
    return current


def _services_live(tracker):
    now = time.ticks_ms()
    return all(key in tracker._parallel_last_progress and
               time.ticks_diff(now, tracker._parallel_last_progress[key]) <= SERVICE_GRACE_MS
               for key in tracker._parallel_required_services)


def _error_deltas(tracker, current=None):
    current = current if current is not None else _service_snapshot()
    return dict((key, max(0, current[key] - tracker._parallel_baseline[key]))
                for key in ERROR_COUNTERS)


def _gate_state(tracker):
    current = _update_service_evidence(tracker)
    if not all(tracker._parallel_evidence[key] for key in tracker._parallel_required_services):
        return "services_incomplete"
    if not tracker._parallel_services_continuous or not _services_live(tracker):
        return "services_incomplete"
    if any(_error_deltas(tracker, current).values()):
        return "runtime_errors"
    return "complete"


def _write_result(tracker, state, started_ms):
    if not tracker.storage_capacity(8192, force=True)["writable"]:
        raise OSError("Parallel benchmark result would violate flash reserve.")
    stat = os.statvfs("/")
    payload = {
        "kind": "parallel_tracking_benchmark_result", "schema": 1,
        "state": state, "target_points": tracker._feature_limit(),
        "profile": tracker._parallel_profile,
        "required_services": list(tracker._parallel_required_services),
        "confirmed_points": tracker.feature_count(),
        "elapsed_ms_this_boot": max(0, time.ticks_diff(time.ticks_ms(), started_ms)),
        "append_max_ms": tracker.max_append_ms,
        "append_profile_us": tracker.append_profile(),
        "heap_free_bytes": gc.mem_free(),
        "heap_min_bytes": app.stats.get("parallel_benchmark_heap_min"),
        "flash_free_bytes": stat[0] * stat[3],
        "flash_total_bytes": stat[0] * stat[2],
        "services": _service_snapshot(),
        "error_deltas": _error_deltas(tracker),
        "uart_diagnostics": dict((key, app.stats.get(key)) for key in UART_DIAGNOSTIC_COUNTERS),
        "nmea_last_crc_error": app.stats.get("nmea_last_crc_error"),
        "service_evidence": dict(tracker._parallel_evidence),
        "services_continuous": tracker._parallel_services_continuous,
        "run_id": app.stats.get("parallel_benchmark_run_id"),
        "initial_points_this_boot": tracker._parallel_initial_points,
        "production_points": tracker._parallel_production_points,
        "qualification_valid": (state == "complete" and
                                tracker.feature_count() == tracker._feature_limit() and
                                tracker._parallel_initial_points == 0),
    }
    temporary = RESULT_PATH + ".tmp"
    with open(temporary, "w") as target:
        target.write(ujson.dumps(payload))
    with open(temporary, "r") as source:
        check = ujson.loads(source.read())
    if (check.get("kind") != payload["kind"] or
            check.get("confirmed_points") != payload["confirmed_points"] or
            check.get("uart_diagnostics") != payload["uart_diagnostics"] or
            check.get("nmea_last_crc_error") != payload["nmea_last_crc_error"]):
        try: os.remove(temporary)
        except OSError: pass
        raise ValueError("Parallel benchmark result verification failed.")
    try: os.remove(RESULT_PATH)
    except OSError: pass
    os.rename(temporary, RESULT_PATH)


async def producer(tracker, config):
    target = config["target_points"]
    started = time.ticks_ms()
    app.stats["parallel_benchmark_state"] = "waiting_services"
    while not shutdown_event.is_set():
        app.beat("parallel_benchmark")
        _update_service_evidence(tracker)
        if _services_live(tracker):
            break
        if time.ticks_diff(time.ticks_ms(), started) >= SERVICE_STARTUP_TIMEOUT_MS:
            app.stats["parallel_benchmark_state"] = "services_incomplete"
            _write_result(tracker, "services_incomplete", started)
            # Keep the supervised task alive without writing synthetic points.
            while not shutdown_event.is_set():
                app.beat("parallel_benchmark")
                await asyncio.sleep(2)
            return
        await asyncio.sleep_ms(1000)
    if shutdown_event.is_set():
        return
    app.stats["parallel_benchmark_state"] = "running"
    tracker._parallel_run_started = True
    next_index = tracker.feature_count() + 1
    failure = None
    while not shutdown_event.is_set() and tracker.feature_count() < target:
        app.beat("parallel_benchmark")
        _update_service_evidence(tracker)
        if not _services_live(tracker):
            tracker._parallel_services_continuous = False
        failure = _gate_state(tracker)
        if failure == "complete":
            failure = None
        else:
            # A failed qualification cannot recover by adding more points.
            # Stop generating, persist diagnostics, and keep exports available.
            break
        queued_total = tracker.feature_count() + len(tracker._pending_fixes)
        if queued_total < target:
            queued = tracker.enqueue_fix(_synthetic_fix(next_index))
            app.stats["parallel_benchmark_generated"] += 1
            if queued is True:
                next_index += 1
            elif queued is None:
                app.stats["tracking_queue_overflows"] += 1
        depth = len(tracker._pending_fixes)
        app.stats["parallel_benchmark_queue_max"] = max(
            app.stats.get("parallel_benchmark_queue_max", 0), depth)
        app.stats["parallel_benchmark_points"] = tracker.feature_count()
        app.stats["parallel_benchmark_heap_min"] = min(
            app.stats.get("parallel_benchmark_heap_min", gc.mem_free()), gc.mem_free())
        line = tracker._line(tracker._project())
        if line and line.get("state") == "paused":
            failure = line.get("pause_reason") or "paused"
            break
        await asyncio.sleep_ms(config["interval_ms"])
    if shutdown_event.is_set():
        return
    while tracker._pending_fixes:
        if shutdown_event.is_set():
            return
        app.beat("parallel_benchmark")
        await asyncio.sleep_ms(100)
    await tracker.compact_async()
    app.stats["parallel_benchmark_points"] = tracker.feature_count()
    state = failure or _gate_state(tracker)
    _write_result(tracker, state, started)
    app.stats["parallel_benchmark_state"] = state
    while not shutdown_event.is_set():
        app.beat("parallel_benchmark")
        await asyncio.sleep(2)
