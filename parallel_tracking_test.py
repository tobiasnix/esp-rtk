#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Arm and load the in-firmware parallel tracking benchmark safely."""
import argparse
import asyncio
import http.client
import json
import os
import socket
import time
import getpass
import hashlib
import math
import http.cookiejar
import urllib.error
import urllib.request

PREFIX = "parallel-benchmark-v15"
CONFIG_PATH = "parallel-benchmark.json"
ALLOWED_TARGETS = (5000, 8000, 10000, 15000, 20000)
PROFILES = ("local", "ble", "tcp", "parallel")
BLE_TX_UUID = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"
RUNTIME_COUNTERS = {
    "http_errors": "http_errors", "task_restarts": "task_restarts",
    "tracking_queue_overflows": "tracking_queue_overflows",
    "nmea_queue_overflows": "queue_overflows", "ble_drops": "ble_drops",
    "tcp_drops": "nmea_tcp_drops", "tracking_write_errors": "tracking_write_errors",
    "uart_buffer_overflows": "uart_buffer_overflows", "gga_parse_errors": "gga_parse_errors",
    "nmea_crc_errors": "crc_errors",
}


def _usb(port, code, name="parallel benchmark control"):
    from rawrepl import with_retry
    return with_retry(lambda repl: repl.ex(code, 120), port=port, versuche=3,
                      name=name).decode().strip()


class WifiControl:
    """Authenticated control without serial dependencies or stored credentials."""
    def __init__(self, host, port=80, code=None):
        self.base = "http://%s:%d" % (host, port)
        self.code = code or os.environ.get("ESP_RTK_DEVICE_CODE") or getpass.getpass("Device code: ")
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.csrf = None

    def login(self):
        request = urllib.request.Request(self.base + "/api/session",
            json.dumps({"device_code": self.code}).encode(), {"Content-Type": "application/json"})
        with self.opener.open(request, timeout=10) as response:
            self.csrf = json.load(response)["csrf_token"]

    def request(self, path, method="GET", value=None, timeout=10):
        if self.csrf is None:
            self.login()
        for attempt in range(2):
            headers = {"Content-Type": "application/json", "X-CSRF-Token": self.csrf}
            request = urllib.request.Request(self.base + path,
                None if value is None else json.dumps(value).encode(), headers, method=method)
            try:
                with self.opener.open(request, timeout=timeout) as response:
                    return response.status, response.read()
            except urllib.error.HTTPError as error:
                if error.code in (401, 403) and attempt == 0:
                    self.login()  # A planned reboot invalidates the old session.
                    continue
                if error.code == 404 and path == "/api/parallel-benchmark":
                    raise RuntimeError("Installed firmware has no WLAN benchmark API; no test was started.") from None
                if error.code == 409:
                    detail = json.load(error).get("message", "Benchmark refused.")
                    raise RuntimeError(detail) from None
                raise

    def json(self, path, method="GET", value=None):
        return json.loads(self.request(path, method, value)[1])

    def action(self, action, target=None, confirm=None, profile=None):
        values = {"action": action, "target_points": target, "confirm": confirm}
        if profile is not None:
            values["profile"] = profile
        return self.json("/api/parallel-benchmark", "DELETE" if action == "cleanup" else "POST",
                         values)

    def export(self, filename, target):
        # A large verified store may need more time before the first export byte.
        # Keep ordinary status/load requests on their existing ten-second limit.
        raw = self.request("/api/parallel-benchmark/export", timeout=120)[1]
        result = verify_export(raw, target)
        with open(filename + ".tmp", "wb") as output:
            output.write(raw)
        os.replace(filename + ".tmp", filename)
        return result


def _unique_json_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate export object key.")
        value[key] = item
    return value


def _reject_json_constant(_value):
    raise ValueError("Non-finite export value.")


def verify_export(raw, target):
    """Validate a complete export independently of the firmware's production cap.

    canonical_sha256 covers every data record, including metadata and the footer;
    points_sha256 remains available for comparisons with older diagnostic reports.
    """
    if type(target) is not int or target < 0:
        raise ValueError("Invalid export target.")
    digest, points_digest, canonical_digest = (hashlib.sha256() for _ in range(3))
    records = points = 0
    integrity = footer = False
    ids, projects, lines, profiles, line_counts, summary_counts = set(), set(), {}, set(), {}, {}

    def register_id(value):
        if not isinstance(value, str) or not value or len(value) > 80 or value in ids:
            raise ValueError("Invalid or duplicate export ID.")
        ids.add(value)
        return value

    def coordinate(value):
        if (not isinstance(value, list) or len(value) != 3 or
                any(type(item) not in (int, float) or not math.isfinite(item) for item in value) or
                not -180 <= value[0] <= 180 or not -90 <= value[1] <= 90 or
                not -20000 <= value[2] <= 100000):
            raise ValueError("Invalid exported coordinate.")

    def measurement(value):
        if (not isinstance(value, dict) or
                any(value.get(key) is None for key in ("recorded_at", "fix_status", "satellites", "hdop", "quality"))):
            raise ValueError("Missing exported measurement.")
        for key in ("recorded_at", "satellites", "hdop", "correction_age_sec"):
            item = value.get(key)
            if item is not None and (type(item) not in (int, float) or not math.isfinite(item)):
                raise ValueError("Invalid exported measurement number.")
        if (not isinstance(value["fix_status"], str) or
                not isinstance(value["quality"], dict) or
                type(value["quality"].get("accepted")) is not bool):
            raise ValueError("Invalid exported measurement quality.")
        if value.get("profile_id") is not None and value["profile_id"] not in profiles:
            raise ValueError("Unknown exported measurement profile.")

    for line in raw.splitlines(keepends=True):
        value = json.loads(line, object_pairs_hook=_unique_json_object,
                           parse_constant=_reject_json_constant)
        if not isinstance(value, dict):
            raise ValueError("Invalid export record.")
        kind = value.get("record")
        if integrity:
            raise ValueError("Data after export integrity record.")
        if kind == "integrity":
            if (not footer or value.get("algorithm") != "sha256" or
                    type(value.get("records")) is not int or value["records"] != records or
                    value.get("content_sha256") != digest.hexdigest()):
                raise ValueError("Export integrity mismatch.")
            integrity = True
            continue
        if records == 0 and kind != "header":
            raise ValueError("Export header missing.")
        if footer:
            raise ValueError("Data after export footer.")
        if kind == "header":
            if (records or value.get("kind") != "survey_backup" or
                    type(value.get("schema_version")) is not int or
                    value["schema_version"] not in (2, 3, 4) or
                    not isinstance(value.get("profiles", []), list)):
                raise ValueError("Invalid export header.")
            for profile in value.get("profiles", []):
                if (not isinstance(profile, dict) or not isinstance(profile.get("id"), str) or
                        not profile["id"] or profile["id"] in profiles or
                        not isinstance(profile.get("limits"), dict)):
                    raise ValueError("Invalid or duplicate exported profile.")
                profiles.add(profile["id"])
        elif kind == "project":
            project = value.get("project")
            if not isinstance(project, dict):
                raise ValueError("Invalid exported project.")
            projects.add(register_id(project.get("id")))
        elif kind == "line":
            metadata = value.get("line")
            if (value.get("project_id") not in projects or not isinstance(metadata, dict) or
                    metadata.get("state") not in ("recording", "paused", "finished") or
                    metadata.get("recording_mode", "survey") not in ("survey", "route")):
                raise ValueError("Invalid exported line or project reference.")
            line_id = register_id(metadata.get("id"))
            lines[line_id] = value["project_id"]
            line_counts[line_id] = 0
            summary = metadata.get("_summary")
            if summary is not None:
                if (not isinstance(summary, dict) or
                        type(summary.get("vertex_count")) is not int or summary["vertex_count"] < 0):
                    raise ValueError("Invalid exported line count.")
                summary_counts[line_id] = summary["vertex_count"]
        elif kind == "vertex":
            if (value.get("line_id") not in lines or
                    lines[value["line_id"]] != value.get("project_id")):
                raise ValueError("Invalid exported vertex references.")
            coordinate(value.get("coordinate"))
            measurement(value.get("measurement"))
            line_counts[value["line_id"]] += 1
        elif kind == "asset":
            point = value.get("point")
            if value.get("project_id") not in projects or not isinstance(point, dict):
                raise ValueError("Invalid exported asset or project reference.")
            register_id(point.get("id"))
            if point.get("line_id") is not None and lines.get(point["line_id"]) != value["project_id"]:
                raise ValueError("Invalid exported asset line reference.")
            coordinate(point.get("coordinate"))
            if point.get("projected_coordinate") is not None:
                coordinate(point["projected_coordinate"])
            measurement(point.get("measurement"))
        elif kind == "footer":
            active_project, active_line = value.get("active_project_id"), value.get("active_line_id")
            if (active_project is not None and active_project not in projects or
                    active_line is not None and
                    (active_line not in lines or lines[active_line] != active_project) or
                    type(value.get("auto_distance_m")) not in (int, float) or
                    not 0 <= value["auto_distance_m"] <= 100):
                raise ValueError("Invalid active export references.")
        else:
            raise ValueError("Unknown export record.")
        footer = kind == "footer"
        records += 1
        digest.update(line)
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode() + b"\n"
        canonical_digest.update(canonical)
        if kind in ("vertex", "asset"):
            points += 1
            points_digest.update(canonical)
            if points > target:
                raise ValueError("Export point count exceeds target.")
    if (not integrity or points != target or
            any(line_counts[key] != count for key, count in summary_counts.items())):
        raise ValueError("Export incomplete or point count mismatch.")
    return {"points": points, "content_sha256": digest.hexdigest(),
            "canonical_sha256": canonical_digest.hexdigest(), "records": records,
            "points_sha256": points_digest.hexdigest()}


def wait_for_stop(client, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status = client.json("/api/parallel-benchmark")
            if not status.get("active") and status.get("state") == "idle":
                return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(2)
    raise RuntimeError("Device did not return to idle after the planned reboot.")


def read_runtime_snapshot(client):
    """Read only qualification evidence; never retain status secrets or assume missing zeros."""
    started = time.monotonic()
    status = client.json("/status")
    finished = time.monotonic()
    system, stats = status.get("system") or {}, status.get("stats") or {}
    counters = {}
    for name, key in RUNTIME_COUNTERS.items():
        value = stats.get(key)
        if type(value) is not int or value < 0:
            raise ValueError("Missing or invalid runtime counter: %s." % name)
        counters[name] = value
    uptime, boots = system.get("uptime_sec"), system.get("boot_count")
    if (type(uptime) not in (int, float) or not math.isfinite(uptime) or uptime < 0 or
            type(boots) is not int or boots < 0 or type(system.get("safe_mode")) is not bool or
            not isinstance(system.get("device_id"), str) or not system["device_id"]):
        raise ValueError("Missing or invalid runtime boot evidence.")
    for key in ("gnss_msgs", "ntrip_bytes"):
        if type(stats.get(key)) is not int or stats[key] < 0:
            raise ValueError("Missing or invalid runtime progress counter.")
    return {"captured_unix": time.time(), "device_id": system["device_id"],
            "uptime_sec": uptime, "boot_count": boots, "safe_mode": system["safe_mode"],
            "errors": counters,
            "services": {"access_state": stats.get("access_state"),
                         "ntrip_state": stats.get("ntrip_state"),
                         "gnss_messages": stats["gnss_msgs"], "ntrip_bytes": stats["ntrip_bytes"]},
            # Uptime is rounded down and updated by the watchdog every two
            # seconds. Include that sampling age as well as HTTP request time.
            "boot_time_window": [started - uptime - 3, finished - uptime + 1],
            "request_started_monotonic": started}


def _runtime_snapshot_problems(snapshot, phase):
    problems = ["%s:%s=%d" % (phase, key, value)
                for key, value in snapshot["errors"].items() if value]
    if snapshot["safe_mode"] or snapshot["boot_count"]:
        problems.append(phase + ":unsafe_boot")
    return problems


def wait_for_runtime_ready(client, timeout=120, on_snapshot=None):
    """Wait only for service startup, never for an observed error to disappear.

    Planned resets clear boot_count immediately in cfg_boot.bump_boot_count.
    Waiting 300 seconds for a nonzero count to clear would hide a crash.
    """
    deadline = time.monotonic() + timeout
    while True:
        snapshot = read_runtime_snapshot(client)
        if on_snapshot:
            on_snapshot(snapshot)
        services = snapshot["services"]
        if (_runtime_snapshot_problems(snapshot, "startup") or
                (services["access_state"] == "ONLINE" and services["ntrip_state"] == "streaming" and
                 services["gnss_messages"] > 0 and services["ntrip_bytes"] > 0)):
            return snapshot
        if time.monotonic() >= deadline:
            raise TimeoutError("Runtime services did not become ready after restart.")
        time.sleep(2)


def _runtime_transition_problems(before, after, phase, restarted=False):
    problems = []
    if before["device_id"] != after["device_id"]:
        problems.append(phase + ":device_changed")
    old_start, old_end = before["boot_time_window"]
    new_start, new_end = after["boot_time_window"]
    if restarted:
        if new_start <= old_end or new_start < before["request_started_monotonic"] - 4:
            problems.append(phase + ":planned_restart_not_proven")
    elif new_start > old_end or old_start > new_end:
        problems.append(phase + ":unexpected_restart")
    return problems


def _verify_completed_run(client, target, run_id, profile):
    control = client.json("/api/parallel-benchmark")
    result = control.get("result") or {}
    if (control.get("state") not in ("complete", "idle") or not run_id or
            result.get("run_id") != run_id or result.get("state") != "complete" or
            result.get("profile", "parallel") != profile or
            result.get("target_points") != target or result.get("confirmed_points") != target or
            result.get("qualification_valid") is not True or
            result.get("initial_points_this_boot") != 0 or
            result.get("services_continuous") is not True):
        raise ValueError("Completed benchmark identity or qualification mismatch.")
    errors = result.get("error_deltas") or {}
    if any(type(errors.get(key)) is not int or errors[key] != 0 for key in RUNTIME_COUNTERS):
        raise ValueError("Completed benchmark has missing or nonzero error counters.")


def verify_restart_exports(client, target, report_base, run_id, profile="parallel",
                           restart_timeout=120, restart=None):
    """Recheck a completed retained run, without starting or regenerating any points.

    This deliberately returns exports_qualified, not an overall capacity release:
    the caller must also supply the separately proven complete load qualification.
    Stop requests a normal reboot and preserves the synthetic store, including
    when this helper is invoked again after the CLI's original automatic restart.
    """
    result = {"exports_qualified": False, "run_id": run_id, "target_points": target,
              "profile": profile, "runtime_snapshots": {}, "verification_errors": [],
              "restart_attempted": False}
    snapshots, problems = result["runtime_snapshots"], result["verification_errors"]

    def save():
        with open(report_base + ".exports.json.tmp", "w", encoding="utf-8") as output:
            json.dump(result, output, sort_keys=True)
            output.write("\n")
        os.replace(report_base + ".exports.json.tmp", report_base + ".exports.json")

    def snapshot(phase, wait_ready=False):
        def observed(value):
            snapshots[phase] = value
            save()
        snapshots[phase] = (wait_for_runtime_ready(client, restart_timeout, observed)
                            if wait_ready else read_runtime_snapshot(client))
        problems.extend(_runtime_snapshot_problems(snapshots[phase], phase))
        save()

    phase = "verify_completed_run"
    try:
        _verify_completed_run(client, target, run_id, profile)
    except Exception as error:
        problems.append(phase + ":" + type(error).__name__)
        save()
        return result  # A different or still-running test must not be stopped.
    try:
        phase = "before_export"
        snapshot(phase)
        phase = "export_before_restart"
        result[phase] = client.export(report_base + ".before.ndjson", target)
        phase = "after_export"
        snapshot(phase)
        problems.extend(_runtime_transition_problems(snapshots["before_export"],
                                                     snapshots[phase], phase))
    except Exception as error:
        problems.append(phase + ":" + type(error).__name__)
    finally:
        # Also disarm on an export failure. Never silently retry a reboot request
        # whose response failed: the first request may already have taken effect.
        result["restart_attempted"] = True
        try:
            (restart or (lambda: client.action("stop")))()
        except Exception as error:
            problems.append("request_restart:" + type(error).__name__)
        save()
    if "after_export" not in snapshots or any(item.startswith("request_restart:") for item in problems):
        return result
    phase = "wait_for_restart"
    try:
        wait_for_stop(client, restart_timeout)
        phase = "verify_restarted_run"
        _verify_completed_run(client, target, run_id, profile)
        phase = "after_restart"
        snapshot(phase, wait_ready=True)
        problems.extend(_runtime_transition_problems(snapshots["after_export"],
            snapshots[phase], phase, restarted=True))
        phase = "export_after_restart"
        result[phase] = client.export(report_base + ".after.ndjson", target)
        phase = "after_restart_export"
        snapshot(phase)
        problems.extend(_runtime_transition_problems(snapshots["after_restart"],
                                                     snapshots[phase], phase))
        before, after = result["export_before_restart"], result["export_after_restart"]
        if (not before.get("canonical_sha256") or
                before["canonical_sha256"] != after.get("canonical_sha256") or
                before.get("points") != target or after.get("points") != target or
                before.get("records") != after.get("records")):
            problems.append("exports:different_content")
        result["exports_qualified"] = not problems
    except Exception as error:
        problems.append(phase + ":" + type(error).__name__)
    save()
    return result


def arm(port, target):
    payload = json.dumps({"kind": "parallel_tracking_benchmark", "schema": 1,
                          "enabled": True, "target_points": target,
                          "interval_ms": 1000}, separators=(",", ":"))
    code = ("import os,ujson\n"
            "p=%r;t=p+'.tmp';b=p+'.previous';v=%r\n"
            "f=open(t,'w');f.write(v);f.close()\n"
            "f=open(t);c=ujson.loads(f.read());f.close()\n"
            "assert c.get('kind')=='parallel_tracking_benchmark'\n"
            "try:os.remove(b)\nexcept OSError:pass\n"
            "try:os.rename(p,b)\nexcept OSError:pass\n"
            "try:os.rename(t,p)\n"
            "except Exception:\n"
            " try:os.rename(b,p)\n except OSError:pass\n raise\n"
            "try:os.remove(b)\nexcept OSError:pass\n"
            "print('armed')" % (CONFIG_PATH, payload))
    if _usb(port, code, "parallel benchmark arm").splitlines()[-1:] != ["armed"]:
        raise RuntimeError("Device did not confirm benchmark activation.")


def status(port):
    code = ("import os,ujson\n"
            "try:\n f=open(%r);v=ujson.loads(f.read());f.close()\n"
            " print(ujson.dumps(v))\n"
            "except OSError:print(ujson.dumps({'state':'not_available'}))" %
            (PREFIX + ".result.json"))
    value = json.loads(_usb(port, code, "parallel benchmark status").splitlines()[-1])
    print(json.dumps(value, sort_keys=True))


def cleanup(port, confirm):
    if confirm != PREFIX:
        raise ValueError("--confirm must exactly match %s" % PREFIX)
    code = ("import os\np=%r\n"
            "for n in list(os.listdir()):\n"
            " if n==%r or n.startswith(p) or n.startswith(%r):\n"
            "  try:os.remove(n)\n  except OSError:pass\n"
            "print('clean')" % (PREFIX, CONFIG_PATH, CONFIG_PATH))
    if _usb(port, code, "parallel benchmark cleanup").splitlines()[-1:] != ["clean"]:
        raise RuntimeError("Device did not confirm benchmark cleanup.")


def _http_once(host, port, cookie, path):
    connection = http.client.HTTPConnection(host, port, timeout=5)
    headers = {"Connection": "close"}
    if cookie: headers["Cookie"] = cookie
    try:
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        body = response.read()
        return response.status, body
    finally:
        connection.close()


class HttpLoadFailure(RuntimeError):
    """An already recorded HTTP load failure makes this run unqualifiable."""


def _record_http_failure(shared, path, status=None, error=None):
    shared["http_errors"] += 1
    failures = shared.setdefault("http_failures", [])
    if len(failures) >= 20:
        failures.pop(0)
    # URLError wraps connect/send failures. Preserve only the exception class
    # and numeric errno; its reason text may contain URLs or credentials.
    reason = getattr(error, "reason", None)
    cause = reason if isinstance(reason, BaseException) else error
    error_number = getattr(cause, "errno", None)
    failures.append({"path": path, "status": status or getattr(error, "code", None),
        "error_type": type(error).__name__ if error else None,
        "reason_type": type(reason).__name__ if isinstance(reason, BaseException) else None,
        "errno": error_number if type(error_number) is int else None,
        "points": shared.get("confirmed_points"), "at_unix": time.time()})
    if shared.get("fail_fast_http"):
        raise HttpLoadFailure("HTTP load failed; inspect the saved run report.")


async def _http_load(args, shared):
    last_report = 0
    while time.monotonic() < shared["deadline"] and not shared["complete"]:
        started = time.monotonic()
        current_path = "/status"
        try:
            client = shared.get("wifi_client")
            if client:
                status_code, body = await asyncio.to_thread(client.request, "/status")
            else:
                status_code, body = await asyncio.to_thread(
                    _http_once, args.host, args.http_port, shared["cookie"], "/status")
            shared["http_requests"] += 1
            if status_code == 200:
                value = json.loads(body)
                stats = value.get("stats") or {}
                state = stats.get("parallel_benchmark_state")
                shared["benchmark_state"] = state
                shared["confirmed_points"] = stats.get("parallel_benchmark_points", 0)
                shared["service_status"] = {
                    "access_state": stats.get("access_state"),
                    "ntrip_state": stats.get("ntrip_state"),
                    "task_restarts": stats.get("task_restarts"),
                    "tracking_queue_overflows": stats.get("tracking_queue_overflows"),
                    "queue_overflows": stats.get("queue_overflows"),
                    "ble_drops": stats.get("ble_drops"),
                    "tcp_drops": stats.get("nmea_tcp_drops"),
                }
                shared["complete"] = state in ("complete", "capacity_limit",
                    "storage_reserve", "services_incomplete", "runtime_errors")
                if state == "running":
                    shared["running_observed"] = True
                if client:
                    current_path = "/api/parallel-benchmark"
                    control = await asyncio.to_thread(client.json, "/api/parallel-benchmark")
                    shared["device_live"] = control.get("live")
                    shared["device_result"] = control.get("result")
                    if control.get("state", "").startswith(("refused_", "invalid_")):
                        shared["benchmark_state"] = control["state"]
                        shared["complete"] = True
                    # Only results from the requested run can qualify.
                    result = shared["device_result"] or {}
                    shared["qualified"] = (state == "complete" and
                        result.get("qualification_valid") is True and
                        result.get("profile", "parallel") == shared.get("profile", "parallel") and
                        (shared.get("run_id") is None or result.get("run_id") == shared["run_id"]))
            else:
                _record_http_failure(shared, current_path, status=status_code)
            # Exercise the main portal and bounded map without retaining bodies.
            for path in ("/", "/api/tracking/map?limit=500"):
                current_path = path
                if client:
                    status_code, _ = await asyncio.to_thread(client.request, path)
                else:
                    status_code, _ = await asyncio.to_thread(_http_once, args.host,
                        args.http_port, shared["cookie"], path)
                if status_code != 200:
                    _record_http_failure(shared, current_path, status=status_code)
        except HttpLoadFailure:
            # The failure has already been counted; do not count it twice or
            # issue another load request before returning to launcher cleanup.
            raise
        except Exception as error:
            if shared.get("running_observed") or not shared.get("planned_reboot"):
                _record_http_failure(shared, current_path, error=error)
        finally:
            shared["http_max_ms"] = max(shared["http_max_ms"],
                                       int((time.monotonic() - started) * 1000))
        if time.monotonic() - last_report >= 30:
            _save_report(args, shared)
            print("Benchmark: %s, points: %s, TCP bytes: %s, BLE bytes: %s" %
                  (shared["benchmark_state"], shared["confirmed_points"],
                   shared["tcp_bytes"], shared["ble_bytes"]), flush=True)
            last_report = time.monotonic()
        await asyncio.sleep(shared.get("http_interval_sec", 1))


async def _tcp_load(args, shared):
    token = shared["stream_token"]
    while time.monotonic() < shared["deadline"] and not shared["complete"]:
        writer = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(args.host, args.tcp_port), 10)
            greeting = await asyncio.wait_for(reader.readline(), 5)
            if greeting.startswith(b"AUTH"):
                if not token: raise RuntimeError("TCP stream token is required.")
                writer.write(b"AUTH " + token.encode() + b"\n")
                await writer.drain()
                reply = await asyncio.wait_for(reader.readline(), 5)
                if not reply.startswith(b"OK"): raise RuntimeError("TCP authentication failed.")
            while time.monotonic() < shared["deadline"] and not shared["complete"]:
                data = await asyncio.wait_for(reader.read(1024), 10)
                if not data: break
                shared["tcp_bytes"] += len(data)
        except Exception:
            shared["tcp_reconnects"] += 1
            await asyncio.sleep(2)
        finally:
            if writer:
                writer.close()
                try: await writer.wait_closed()
                except Exception: pass


async def _ble_load(shared):
    address = shared["ble_address"]
    if not address:return
    try:
        from bleak import BleakClient
    except ImportError:
        shared["ble_error"] = "bleak_not_installed"
        return
    while time.monotonic() < shared["deadline"] and not shared["complete"]:
        try:
            async with BleakClient(address) as client:
                def received(_sender, data): shared["ble_bytes"] += len(data)
                await client.start_notify(BLE_TX_UUID, received)
                shared["ble_error"] = None
                while (time.monotonic() < shared["deadline"] and not shared["complete"]
                       and client.is_connected):
                    await asyncio.sleep(1)
        except Exception:
            shared["ble_error"] = "connection_failed"
        await asyncio.sleep(2)


def _save_report(args, shared):
    report = {key: value for key, value in shared.items()
              if key not in ("cookie", "stream_token", "ble_address", "deadline", "wifi_client")}
    report["load_qualified"] = (report["qualified"] and report["http_errors"] == 0
                                and (shared.get("profile", "parallel") not in ("tcp", "parallel")
                                     or report["tcp_bytes"] > 0) and
                                (not shared.get("ble_address") or
                                 (report["ble_bytes"] > 0 and report["ble_error"] is None)))
    report["qualified"] = False  # Set only after export and restart verification.
    if args.report:
        with open(args.report + ".tmp", "w", encoding="utf-8") as target:
            target.write(json.dumps(report, sort_keys=True) + "\n")
        os.replace(args.report + ".tmp", args.report)
    return report


async def monitor(args, wifi_client=None, run_id=None):
    profile = getattr(args, "profile", "parallel")
    shared = {"deadline": time.monotonic() + args.duration,
              "profile": profile, "http_interval_sec": 5 if profile == "local" else 1,
              "cookie": os.environ.get(args.cookie_env, "") if args.cookie_env else "",
              "stream_token": os.environ.get(args.stream_token_env, "")
              if args.stream_token_env else "",
              "ble_address": os.environ.get(args.ble_address_env, "")
              if args.ble_address_env else "",
              "complete": False, "benchmark_state": None, "confirmed_points": 0,
              "http_requests": 0, "http_errors": 0, "http_max_ms": 0,
              "tcp_bytes": 0, "tcp_reconnects": 0, "ble_bytes": 0,
              "ble_error": None, "service_status": {}, "wifi_client": wifi_client,
              "run_id": run_id, "planned_reboot": run_id is not None,
              "fail_fast_http": getattr(args, "fail_fast_http", False) is True,
              "running_observed": False, "qualified": False, "device_result": None}
    if profile not in ("ble", "parallel"):
        shared["ble_address"] = ""
    if wifi_client and profile in ("tcp", "parallel") and not shared["stream_token"]:
        label = await asyncio.to_thread(wifi_client.json, "/api/label")
        shared["stream_token"] = label.get("stream_token", "")
    tasks = [_http_load(args, shared)]
    if profile in ("tcp", "parallel"):
        tasks.append(_tcp_load(args, shared))
    if profile in ("ble", "parallel"):
        tasks.append(_ble_load(shared))
    try:
        await asyncio.gather(*tasks)
    except HttpLoadFailure:
        # Persist the exact first error and current points before an immediate
        # abort, even when the ordinary 30-second report interval has not elapsed.
        _save_report(args, shared)
        raise
    report = _save_report(args, shared)
    encoded = json.dumps(report, sort_keys=True)
    print(encoded)
    return report


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    armed = sub.add_parser("arm")
    armed.add_argument("target", type=int, choices=ALLOWED_TARGETS)
    armed.add_argument("--port")
    armed.add_argument("--host", help="Use authenticated WLAN control instead of USB.")
    armed.add_argument("--profile", choices=PROFILES, default="parallel")
    stat = sub.add_parser("status"); stat.add_argument("--port"); stat.add_argument("--host")
    clean = sub.add_parser("cleanup"); clean.add_argument("--port")
    clean.add_argument("--confirm", required=True)
    clean.add_argument("--host")
    stop = sub.add_parser("stop"); stop.add_argument("--host", required=True)
    load = sub.add_parser("monitor")
    load.add_argument("--host", required=True); load.add_argument("--http-port", type=int, default=80)
    load.add_argument("--tcp-port", type=int, default=10110)
    load.add_argument("--duration", type=int, default=14400)
    load.add_argument("--cookie-env", default="ESP_RTK_SESSION_COOKIE")
    load.add_argument("--stream-token-env", default="ESP_RTK_STREAM_TOKEN")
    load.add_argument("--ble-address-env", default="ESP_RTK_BLE_ADDRESS")
    load.add_argument("--report")
    load.add_argument("--profile", choices=PROFILES, default="parallel")
    load.add_argument("--wifi", action="store_true", help="Sign in with the device code; reconnect after reboot.")
    run = sub.add_parser("run", parents=[], help="Start and monitor a full WLAN-controlled run.")
    run.add_argument("target", type=int, choices=ALLOWED_TARGETS)
    run.add_argument("--host", required=True)
    run.add_argument("--duration", type=int, default=22000)
    run.add_argument("--http-port", type=int, default=80)
    run.add_argument("--tcp-port", type=int, default=10110)
    run.add_argument("--cookie-env", default="ESP_RTK_SESSION_COOKIE")
    run.add_argument("--stream-token-env", default="ESP_RTK_STREAM_TOKEN")
    run.add_argument("--ble-address-env", default="ESP_RTK_BLE_ADDRESS")
    run.add_argument("--report", required=True)
    run.add_argument("--profile", choices=PROFILES, default="parallel")
    args = parser.parse_args()
    if args.command == "run":
        if args.duration <= 0:
            parser.error("--duration must be positive")
        client = WifiControl(args.host, args.http_port)
        client.json("/api/parallel-benchmark")  # Capability check before any mutation.
        # Retrieve the TCP token before the planned reboot.
        if args.profile in ("tcp", "parallel"):
            label = client.json("/api/label")
            if not os.environ.get(args.stream_token_env):
                os.environ[args.stream_token_env] = label["stream_token"]
        result = client.action("start", args.target, profile=args.profile)
        report = {"qualified": False, "load_qualified": False, "run_id": result["run_id"]}
        stop_attempted = False

        def stop_once():
            nonlocal stop_attempted
            if not stop_attempted:
                stop_attempted = True
                client.action("stop")

        try:
            report = asyncio.run(monitor(args, client, result["run_id"]))
            if report["load_qualified"]:
                verification = verify_restart_exports(client, args.target, args.report,
                    result["run_id"], args.profile, restart=stop_once)
                report.update(verification)
                report["qualified"] = verification["exports_qualified"]
        finally:
            # Also disarm on Ctrl-C; keep all synthetic files for inspection.
            stop_once()
        with open(args.report + ".tmp", "w", encoding="utf-8") as output:
            json.dump(report, output, sort_keys=True)
            output.write("\n")
        os.replace(args.report + ".tmp", args.report)
        print(json.dumps(report, sort_keys=True))
        if not report["qualified"]:
            raise SystemExit("Benchmark did not qualify; see the report.")
    elif args.command == "monitor":
        asyncio.run(monitor(args, WifiControl(args.host, args.http_port) if args.wifi else None))
    elif args.host:
        client = WifiControl(args.host)
        result = (client.json("/api/parallel-benchmark") if args.command == "status" else
                  client.action("start" if args.command == "arm" else args.command,
                                getattr(args, "target", None), getattr(args, "confirm", None),
                                profile=getattr(args, "profile", None)))
        print(json.dumps(result, sort_keys=True))
    elif args.command == "arm":
        if args.profile != "parallel":
            parser.error("Additional profiles require --host WLAN control.")
        arm(args.port, args.target)
    elif args.command == "status": status(args.port)
    elif args.command == "cleanup": cleanup(args.port, args.confirm)


if __name__ == "__main__":
    main()
