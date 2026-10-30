#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Host side of the NMEA hot-path benchmark (#28c, #36).

Executes the very program that device_nmea_benchmark.py sends to the device
and adds what only the host can measure without writing flash: how often one
epoch calls quality_check on the survey and on the route path, and the
tracemalloc peak per component as a proxy for the device's gc.mem_alloc()
counts. Host times are CPython times; the Raw REPL run on the device is the
reference for them.

    python3 tests/benchmark_nmea_hotpath.py [--out FILE] [--baseline FILE]
"""
import argparse
import contextlib
import io
import json
import os
import tempfile
import time
import tracemalloc
from unittest import mock

import support  # noqa: F401 - installs the MicroPython compatibility modules
import device_nmea_benchmark
import geo
import gnss
import state
import tracking

# metric key per component for compare(); alloc keys follow the same pattern
COMPONENTS = {
    "parse_gga": ("us_per_call", "alloc_bytes_per_call"),
    "coord_e8": ("us_per_call", "alloc_bytes_per_call"),
    "decimal_mm": ("us_per_call", "alloc_bytes_per_call"),
    "quality_check": ("us_per_call", "alloc_bytes_per_call"),
    "checksum": ("us_per_call", "alloc_bytes_per_call"),
    "scanner": ("us_per_sentence", "alloc_bytes_per_sentence"),
    "rtcm_feed": ("us_per_kb", "alloc_bytes_per_kb"),
    "observe_rtcm": ("us_per_kb", "alloc_bytes_per_kb"),
    "uart_tx_write_rtcm": ("us_per_kb", "alloc_bytes_per_kb"),
}

GST = {"source": "NMEA_GST", "utc": "123519.00", "horizontal_sigma_m": 0.012,
       "semi_major_sigma_m": 0.012, "altitude_sigma_m": 0.025}
# 0.5 m of latitude per epoch in e8 degrees (1e-8 degree is about 1.11 mm)
STEP_E8 = 449


def _gate_epochs(route, epochs):
    """Count quality_check calls per epoch on the real AppState/Tracker path.

    Per epoch: update_fix(GGA), attach_gst(GST), then the worker's write the way
    Tracker.on_fix does it. The clocks are fixed so that runs are comparable.
    """
    base = gnss.GNSSHandler.parse_gga(None, "$%s*00" % device_nmea_benchmark.GGA_BODY)
    now = [100000]
    listener_calls = []
    with tempfile.TemporaryDirectory(prefix="esp-rtk-nmea-bench-") as directory, \
            mock.patch.object(time, "ticks_ms", side_effect=lambda: now[0]), \
            mock.patch.object(tracking.time, "time", return_value=1700000000), \
            support.patch_module_global("tracking", "_now_seconds", return_value=1700000000):
        tracker = tracking.Tracker(os.path.join(directory, "bench.jsonl"))
        app = state.AppState()
        app.stats["gst_output_state"] = "receiving"
        app.set_fix_listener(lambda *args: listener_calls.append(args))
        tracker.create_project("NMEA hot-path benchmark")
        tracker.start_line("Benchmark", 0.2, recording_mode="route" if route else "survey")
        real = tracking.quality_check
        try:
            with mock.patch.object(tracking, "tracker", tracker), \
                    support.patch_module_global("tracking", "quality_check", wraps=real) as counted:
                before = app.fix_state()
                for epoch in range(epochs):
                    now[0] += 1000
                    utc = "%06d.00" % (120000 + epoch)
                    fix = dict(base, utc=utc, lat_e8=base["lat_e8"] + STEP_E8 * epoch)
                    if route:
                        fix["route_ready"] = True
                    app.update_fix(fix)
                    app.attach_gst(dict(GST, utc=utc))
                    while tracker._pending_fixes:
                        tracker.add_vertex(tracker._pending_fixes.pop(0), source="automatic")
                after = app.fix_state()
                checks = counted.call_count
            stored = tracker.feature_count()
        finally:
            tracker.point_store.close()
    return {"epochs": epochs, "quality_checks": checks,
            "per_epoch": checks / epochs if epochs else 0,
            "fix_versions": (after["fix_seq"] - before["fix_seq"]
                             + after["fix_rev"] - before["fix_rev"]),
            "listener_calls": len(listener_calls), "points_stored": stored}


def _peak(function):
    """Largest traced allocation of one call, after a warm-up call."""
    function()
    tracemalloc.start()
    try:
        base = tracemalloc.get_traced_memory()[0]
        tracemalloc.reset_peak()
        function()
        return tracemalloc.get_traced_memory()[1] - base
    finally:
        tracemalloc.stop()


def _host_peaks(namespace):
    line = "$%s*%s" % (device_nmea_benchmark.GGA_BODY,
                       namespace["checksum_text"](device_nmea_benchmark.GGA_BODY))
    fix = gnss.GNSSHandler.parse_gga(None, line)
    fix["receiver_accuracy"] = dict(GST)
    chunk = bytes(1024)
    counter = support.net.RtcmCounter()
    epoch = namespace["EPOCH"] * 10
    return {
        "parse_gga": _peak(lambda: gnss.GNSSHandler.parse_gga(None, line)),
        "coord_e8": _peak(lambda: geo.parse_nmea_coord_e8("4807.038123", "N")),
        "decimal_mm": _peak(lambda: geo.parse_decimal_fixed("545.412", 3)),
        "quality_check": _peak(lambda: tracking.quality_check(fix)),
        "checksum": _peak(lambda: gnss.GNSSHandler.check_nmea_checksum(line)),
        "scanner": _peak(lambda: namespace["scan"](epoch, 512, False)),
        "rtcm_feed": _peak(lambda: counter.feed(chunk)),
    }


def run(sentences=10000, repeat=500, epochs=200):
    """Run the device program on the host and add the host-only scenarios."""
    namespace = {"__name__": "nmea_hotpath"}
    source = device_nmea_benchmark.program(sentences, repeat)
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        exec(compile(source, "<device-nmea-benchmark>", "exec"), namespace)
    result = json.loads(output.getvalue().strip().splitlines()[-1])
    with contextlib.redirect_stdout(io.StringIO()):
        result["gate_survey"] = _gate_epochs(False, epochs)
        result["gate_route"] = _gate_epochs(True, epochs)
        result["host_peak_bytes"] = _host_peaks(namespace)
    parse_us = result["parse_gga"]["us_per_call"]
    check_us = result["quality_check"]["us_per_call"]
    survey, route = result["gate_survey"]["per_epoch"], result["gate_route"]["per_epoch"]
    result["epoch_cost_us"] = {
        "K_survey": round(parse_us + survey * check_us),
        "K_route": round(parse_us + route * check_us),
        "K_n4": round(parse_us + 4 * check_us),
        "per_second_5hz_survey": round(5 * parse_us + survey * check_us),
        "per_second_5hz_route": round(5 * parse_us + route * check_us),
    }
    return result


def compare(old, new):
    """Factors old/new per component (above 1 = faster or leaner now).

    A component that is missing or {"unavailable": ...} on either side gives
    None; allocation factors appear as "<component>_alloc" when both runs have
    the device counter.
    """
    factors = {}
    for name, (time_key, alloc_key) in COMPONENTS.items():
        factors[name] = _factor(old.get(name), new.get(name), time_key)
        alloc = _factor(old.get(name), new.get(name), alloc_key)
        if alloc is not None:
            factors[name + "_alloc"] = alloc
    return factors


def _factor(old, new, key):
    try:
        before, after = old[key], new[key]
    except (KeyError, TypeError):
        return None
    if not before or not after:
        return None
    return round(before / after, 2)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", help="write the result JSON to this file")
    parser.add_argument("--baseline", help="earlier result JSON to compare against")
    parser.add_argument("--sentences", type=int, default=10000)
    parser.add_argument("--repeat", type=int, default=500)
    parser.add_argument("--epochs", type=int, default=200)
    args = parser.parse_args(argv)
    result = run(args.sentences, args.repeat, args.epochs)
    text = json.dumps(result, sort_keys=True, indent=1)
    if args.out:
        with open(args.out, "w") as handle:
            handle.write(text + "\n")
    print(text)
    if args.baseline:
        with open(args.baseline) as handle:
            print(json.dumps(compare(json.load(handle), result), sort_keys=True, indent=1))


if __name__ == "__main__":
    main()
