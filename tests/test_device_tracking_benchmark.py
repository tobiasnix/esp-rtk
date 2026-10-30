# SPDX-License-Identifier: AGPL-3.0-only
import asyncio
import builtins
import json
import re
import sys
import types
import unittest

import device_tracking_benchmark as benchmark

LIMITS = ("MAX_FEATURES", "SEGMENT_MAX_BYTES")
BOUND = ("tracking", "tracking.common", "tracking.journal", "tracking.recording")


def real_bindings():
    """Snapshot of the limits the real tracking modules bind right now."""
    return {(k, n): vars(m).get(n) for k, m in sys.modules.items()
            if k == "tracking" or k.startswith("tracking.") for n in LIMITS}


def run_isolated(text, modules):
    """Run a device program with exec against a fake module table.

    The program sees only `modules` through import (and a stub `sys`); the
    real sys.modules is neither read nor changed. Its print output lands in
    ns["_printed"].
    """
    printed = []
    scope = dict(vars(builtins))

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "sys":
            return types.SimpleNamespace(modules=modules)
        if name not in modules:
            raise ImportError("no module named " + name)
        return modules[name]

    scope["__import__"] = fake_import
    scope["print"] = lambda *args, **kw: printed.append(" ".join(str(a) for a in args))
    ns = {"__builtins__": scope, "_printed": printed}
    exec(compile(text, "<device-program>", "exec"), ns)
    return ns


def fake_device_modules(bound=BOUND, unbound=("tracking.frames",), stray=("trackingx",)):
    """Fake MicroPython modules for the benchmark programs.

    Every module of `bound` and `stray` binds the limits at the values the
    package starts with, `unbound` binds none. The fake Tracker records the
    limits of every tracking module in tracking.seen: ("new", snapshot) at each
    construction, ("add", snapshot) at the first add_vertex of an instance.
    """
    clock = [0]
    mods = {}

    def module(name, **attrs):
        m = types.ModuleType(name)
        vars(m).update(attrs)
        mods[name] = m
        return m

    def snapshot():
        return {k: (vars(m).get("MAX_FEATURES"), vars(m).get("SEGMENT_MAX_BYTES"))
                for k, m in mods.items() if k == "tracking" or k.startswith("tracking")}

    def ticks_ms():
        clock[0] += 1
        return clock[0]

    async def sleep_ms(ms):
        clock[0] += ms
        await asyncio.sleep(0)

    class PointStore:
        def close(self):
            pass

    class Tracker:
        def __init__(self, path):
            self.path = path
            self.projects = []
            self.count = mods["tracking"].start_features
            self.max_append_ms = 0
            self.point_store = PointStore()
            self.added = False
            mods["tracking"].seen.append(("new", snapshot()))

        def create_project(self, name):
            self.projects.append(name)

        def start_line(self, name, kind):
            pass

        def feature_count(self):
            return self.count

        def add_vertex(self, fix, source=None):
            if not self.added:
                self.added = True
                mods["tracking"].seen.append(("add", snapshot()))
            self.count += 1

        def compact(self):
            pass

        async def compact_async(self):
            pass

        def iter_backup_ndjson(self):
            yield "{}\n"

        def append_profile(self):
            return {"journal_us": 1}

    module("gc", collect=lambda: None, mem_free=lambda: 100000)
    module("os", listdir=lambda *a: [], remove=lambda n: None,
           statvfs=lambda p: (4096, 0, 0, 10))
    module("time", ticks_ms=ticks_ms, ticks_diff=lambda a, b: a - b,
           ticks_add=lambda a, b: a + b)
    module("ujson", dumps=json.dumps)
    module("machine", WDT=lambda *a: types.SimpleNamespace(feed=lambda: None))
    module("pointstore", POINT_SEGMENT_BYTES=131072)
    module("uasyncio", run=asyncio.run, create_task=asyncio.create_task, sleep_ms=sleep_ms)
    module("tracking", TARGET_MAX_FEATURES=10000, Tracker=Tracker, seen=[], start_features=0)
    for name in tuple(bound) + tuple(stray):
        vars(mods.setdefault(name, types.ModuleType(name))).update(
            MAX_FEATURES=5000, SEGMENT_MAX_BYTES=131072)
    for name in unbound:
        mods.setdefault(name, types.ModuleType(name))
    return mods


class TestDeviceTrackingBenchmark(unittest.TestCase):
    def test_generated_program_compiles_and_formats_metrics(self):
        source=benchmark.program(5000,True)
        compile(source,"<device-benchmark>","exec")
        self.assertNotIn("%%d",source)
        self.assertIn("'points':done",source)
        self.assertIn("'append_profile_us':t.append_profile()", source)
        self.assertIn("POINT_SEGMENT_BYTES=128*1024", source)
        self.assertIn("scale-benchmark-v15",source)

    def test_program_sets_tracking_limits_in_every_module(self):
        before = real_bindings()
        source = benchmark.program(2, False, segment_kib=64)
        self.assertIsNone(re.search(r"tracking\.[A-Z_]+\s*=", source))
        mods = fake_device_modules()
        ns = run_isolated(source, mods)
        seen = mods["tracking"].seen
        self.assertEqual(seen[0][0], "new")
        self.assertEqual(seen[1][0], "add")
        for name in BOUND:
            self.assertEqual(seen[0][1][name], (5000, 64 * 1024), name)
            self.assertEqual(seen[1][1][name], (max(10000, 2), 64 * 1024), name)
        self.assertEqual(seen[1][1]["tracking.frames"], (None, None))
        self.assertEqual(seen[1][1]["trackingx"], (5000, 131072))
        self.assertEqual(mods["trackingx"].MAX_FEATURES, 5000)
        self.assertFalse(hasattr(mods["tracking.frames"], "MAX_FEATURES"))
        self.assertEqual(mods["pointstore"].POINT_SEGMENT_BYTES, 64 * 1024)
        self.assertEqual(json.loads(ns["_printed"][-1])["points"], 2)
        self.assertEqual(real_bindings(), before)

    def test_experimental_target_raises_only_benchmark_limit(self):
        before = real_bindings()
        mods = fake_device_modules()
        mods["tracking"].start_features = 19999
        ns = run_isolated(benchmark.program(20000, False), mods)
        add = mods["tracking"].seen[1]
        self.assertEqual(add[0], "add")
        for name in BOUND:
            self.assertEqual(add[1][name][0], 20000, name)
        self.assertEqual(json.loads(ns["_printed"][-1])["points"], 20000)
        self.assertEqual(real_bindings(), before)

    def test_tset_sets_each_bound_module(self):
        before = real_bindings()
        a, b, c, d = (types.ModuleType(n) for n in ("a", "b", "c", "d"))
        for m in (a, b, d):
            m.MAX_FEATURES = 1
        ns = run_isolated(benchmark.TRACKING_SET,
                          {"tracking": a, "tracking.x": b, "tracking.y": c, "trackingx": d})
        ns["_tset"]("MAX_FEATURES", 9)
        self.assertEqual((a.MAX_FEATURES, b.MAX_FEATURES, d.MAX_FEATURES), (9, 9, 1))
        self.assertFalse(hasattr(c, "MAX_FEATURES"))
        self.assertEqual(real_bindings(), before)


if __name__ == "__main__":
    unittest.main()
