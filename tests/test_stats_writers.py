# SPDX-License-Identifier: AGPL-3.0-only
"""One owner module per stats key (A-13).

A static scan over every Python board file finds who writes which key of the
shared stats dict. Each key may be written from one module only; the few keys
with two writers are listed in EXCEPTIONS with their reason, and writes with a
computed key only at the documented DYNAMIC_SITES.
"""
import ast
import os
import sys
import textwrap
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import push


# Keys written from more than one module, each with its reason (until the alias removal after the state package, #20).
EXCEPTIONS = {
    "ble_drops": (frozenset({"ble.py", "fanout.py"}),
                  "documented sum of every lost BLE output: fan-out queue overflow "
                  "plus abandoned notify (ble_queue_overflows + ble_notify_drops)"),
    "clock_resyncs": (frozenset({"gnss.py", "net.py"}),
                      "one clock, two sources (GNSS RMC and NTP)"),
    "clock_last_delta_s": (frozenset({"gnss.py", "net.py"}),
                           "one clock, two sources (GNSS RMC and NTP)"),
    "force_setup": (frozenset({"main.py", "net.py"}),
                    "flag: set at boot, cleared when the setup window ends"),
    "tracking_startup": (frozenset({"main.py", "tracking/restore.py"}),
                         "state record of the tracking start-up"),
}

# (file, innermost function) of the writes whose key is computed at run time.
DYNAMIC_SITES = frozenset({
    ("ble.py", "_notify_chunk"),
    ("fanout.py", "_deliver"),
    ("fanout.py", "route"),
})

# Classes whose `self.stats` is the shared stats dict: AppState and its part classes (#20).
STATE_CLASSES = frozenset({"AppState", "ReceiverState", "LinkState", "SystemState", "StorageState"})

_DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
_ROOT_METHODS = ("update", "setdefault", "pop")


def _split_scope(node):
    """Nodes that belong to this scope, and the definitions nested directly in it."""
    own, nested = [], []
    stack = list(ast.iter_child_nodes(node))
    while stack:
        child = stack.pop()
        if isinstance(child, _DEFINITIONS):
            nested.append(child)
        else:
            own.append(child)
            stack.extend(ast.iter_child_nodes(child))
    return own, nested


def _scopes(node, cls=None, func="<module>"):
    """Yield (enclosing class, innermost function, nodes) for every scope."""
    own, nested = _split_scope(node)
    yield cls, func, own
    for child in nested:
        if isinstance(child, ast.ClassDef):
            yield from _scopes(child, child.name, func)
        elif isinstance(child, ast.Lambda):
            yield from _scopes(child, cls, func)
        else:
            yield from _scopes(child, cls, child.name)


def _constant_key(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _assign_targets(node):
    if isinstance(node, ast.Assign):
        targets = list(node.targets)
    elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
        targets = [node.target]
    elif isinstance(node, ast.Delete):
        targets = list(node.targets)
    else:
        return []
    flat = []
    while targets:
        target = targets.pop()
        if isinstance(target, (ast.Tuple, ast.List)):
            targets.extend(target.elts)
        elif isinstance(target, ast.Starred):
            targets.append(target.value)
        else:
            flat.append(target)
    return flat


def stats_writers(sources):
    """Scan {file name: source} for writes to the shared stats dict.

    Roots are `app.stats`, `self.stats` inside a class of STATE_CLASSES, and local
    names bound to one of them in the same function. A write is an
    assignment, augmented assignment or `del` on `root[key]` (also nested,
    `root[key][...]`, or via a local `c = root[key]`), and
    `root.update({...})`, `root.setdefault(key, ...)`, `root.pop(key, ...)`.
    Returns ({key: {file, ...}}, {(file, innermost function)}) where the
    second set holds the writes whose key is not a string constant.
    """
    writers = {}
    dynamic = set()
    for module, source in sources.items():
        tree = ast.parse(source)
        for cls, func, nodes in _scopes(tree):
            def is_root(expr, roots):
                if (isinstance(expr, ast.Attribute) and expr.attr == "stats"
                        and isinstance(expr.value, ast.Name)):
                    return expr.value.id == "app" or (expr.value.id == "self"
                                                      and cls in STATE_CLASSES)
                return isinstance(expr, ast.Name) and expr.id in roots

            def write(key):
                if key is None:
                    dynamic.add((module, func))
                else:
                    writers.setdefault(key, set()).add(module)

            single = [(node.targets[0].id, node.value) for node in nodes
                      if isinstance(node, ast.Assign) and len(node.targets) == 1
                      and isinstance(node.targets[0], ast.Name)]
            roots = {name for name, value in single if is_root(value, ())}
            # Locals bound to one entry of the stats dict: `c = root["key"]`.
            entries = {name: _constant_key(value.slice) for name, value in single
                       if isinstance(value, ast.Subscript) and is_root(value.value, roots)}

            for node in nodes:
                for target in _assign_targets(node):
                    if not isinstance(target, ast.Subscript):
                        continue
                    base = target
                    while isinstance(base.value, ast.Subscript):
                        base = base.value
                    if is_root(base.value, roots):
                        write(_constant_key(base.slice))
                    elif isinstance(base.value, ast.Name) and base.value.id in entries:
                        write(entries[base.value.id])
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr in _ROOT_METHODS
                        and is_root(node.func.value, roots)):
                    continue
                if node.func.attr != "update":
                    write(_constant_key(node.args[0]) if node.args else None)
                    continue
                for arg in node.args:
                    if isinstance(arg, ast.Dict):
                        for key in arg.keys:
                            write(_constant_key(key) if key is not None else None)
                    else:
                        write(None)
                for keyword in node.keywords:
                    write(keyword.arg)
    return writers, dynamic


def board_sources():
    sources = {}
    for name in push.BOARD_FILES:
        if name.endswith(".py"):
            with open(os.path.join(REPO, name), encoding="utf-8") as fh:
                sources[name] = fh.read()
    return sources


class TestScanner(unittest.TestCase):
    def test_scanner_sees_every_write_form(self):
        snippets = {
            "assign.py": 'app.stats["a"] = 1',
            "augmented.py": 'app.stats["a"] += 1',
            "alias.py": '''
                def f():
                    s = app.stats
                    s["b"] += 1
            ''',
            "appstate.py": '''
                class AppState:
                    def __init__(self):
                        self.stats = {"z": 0}

                    def f(self):
                        self.stats["c"] = 1

                    def g(self):
                        st = self.stats
                        st["d"] = 1

                    def h(self, key):
                        c = self.stats["j"]
                        c[key] = 1
            ''',
            "other.py": '''
                class Other:
                    def f(self):
                        self.stats["x"] = 1
            ''',
            "update.py": 'app.stats.update({"e": 1, "f": 2})',
            "setdefault.py": 'app.stats.setdefault("g", []).append(1)',
            "pop.py": 'app.stats.pop("h", None)',
            "nested.py": 'app.stats["i"]["k"] = 1',
            "dynamic.py": '''
                def writer(key):
                    app.stats[key] = 1
            ''',
        }
        writers, dynamic = stats_writers(
            {name: textwrap.dedent(source) for name, source in snippets.items()})
        self.assertEqual(writers, {
            "a": {"assign.py", "augmented.py"}, "b": {"alias.py"},
            "c": {"appstate.py"}, "d": {"appstate.py"}, "j": {"appstate.py"},
            "e": {"update.py"}, "f": {"update.py"}, "g": {"setdefault.py"},
            "h": {"pop.py"}, "i": {"nested.py"}})
        self.assertEqual(dynamic, {("dynamic.py", "writer")})


class TestStatsOwners(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.writers, cls.dynamic = stats_writers(board_sources())

    def test_each_stats_key_has_one_owner_module(self):
        shared = {key: modules for key, modules in self.writers.items() if len(modules) > 1}
        undocumented = {key: sorted(modules) for key, modules in shared.items()
                        if key not in EXCEPTIONS or EXCEPTIONS[key][0] != modules}
        self.assertEqual(undocumented, {},
                         "a stats key needs one owner module or a documented exception")

    def test_dynamic_stats_writes_are_documented(self):
        self.assertEqual(self.dynamic, DYNAMIC_SITES)

    def test_exceptions_are_still_needed(self):
        stale = sorted(key for key in EXCEPTIONS if len(self.writers.get(key, ())) < 2)
        self.assertEqual(stale, [], "exception no longer needed; remove it")

    def test_state_part_writes_are_owned_by_their_part(self):
        self.assertEqual(self.writers.get("fix_quality_seconds"), {"state/receiver.py"})
        self.assertEqual(self.writers.get("access_state"), {"state/link.py"})
        self.assertEqual(self.writers.get("ntrip_state"), {"state/link.py"})
        self.assertEqual(self.writers.get("loop_lag_max_ms"), {"state/system.py"})
        self.assertEqual(self.writers.get("task_restarts"), {"state/__init__.py"})

    def test_tracking_queue_overflows_has_one_owner(self):
        self.assertNotIn("tracking_queue_overflows", EXCEPTIONS)
        self.assertEqual(self.writers["tracking_queue_overflows"], {"state/receiver.py"})

    def test_new_counters_have_one_owner(self):
        self.assertEqual(self.writers.get("ble_notify_drops"), {"ble.py"})
        self.assertEqual(self.writers.get("gga_parsed"), {"gnss.py"})
        self.assertEqual(self.writers.get("wifi_ssid_active"), {"net.py"})
        self.assertEqual(self.writers.get("ble_auth_failures"), {"ble.py"})


if __name__ == "__main__":
    unittest.main()
