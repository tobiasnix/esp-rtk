# SPDX-License-Identifier: AGPL-3.0-only
"""The package tracking/ behind its facade (#26c): layout, composition, names and imports.

tracking.py became the package tracking/ without a change of behaviour. The modules
hold the code in layers, class Tracker is composed of the tracking mixins, and the
facade tracking/__init__.py re-exports exactly the names that code outside the
package reads, plus the event model interface, and creates the tracker singleton.
"""
import ast
import builtins
import importlib
import os
import re
import symtable
import tempfile
import types
import unittest
from unittest import mock

import support
from support import REPO
import cfg_factory
import identity
import tracking
import push
from source_checks import production_packages
from test_tracking import FIX


# Board order: the layers from the bottom up, the facade last.
PACKAGE_MODULES = ("common", "guards", "frames", "validation", "fixes", "model", "journal",
                   "archive", "restore", "summary", "exports", "recording", "core", "__init__")
LAYERS = {"common": 0, "guards": 0, "frames": 1, "validation": 1, "fixes": 2, "model": 3,
          "journal": 4, "archive": 4, "restore": 4, "summary": 4, "exports": 4, "recording": 4,
          "core": 5, "__init__": 6}
MRO = ("Tracker", "RecordingMixin", "JournalMixin", "FrameAssignmentMixin", "ArchiveMixin",
       "RestoreMixin", "SummaryMixin", "ExportMixin", "object")
HOT_PATH = ("enqueue_fix", "run_worker", "add_vertex", "add_asset", "_measurement", "_apply",
            "_append", "_write_journal_event", "_record_map_change")
# The event model interface of #26b, re-exported whether or not a consumer reads it.
INTERFACE = ("EVENT_HANDLERS", "ACTIVITY_EVENTS", "_event_activity", "MAP_CHANGE_BUILDERS")
# Names only the facade itself binds.
OWNED = ("tracker",)

# Dunder entries a plain class body gets from CPython; a mixin has no others.
CLASS_DUNDERS = frozenset({"__module__", "__qualname__", "__dict__", "__weakref__", "__doc__",
                           "__firstlineno__", "__static_attributes__"})
# Modules no tracking module may import at module level (lazy imports stay in functions).
LAZY_ONLY = frozenset({"state", "web", "main", "stakeout", "stakeout_progress",
                       "stakeout_export", "stakeout_session", "measurement", "gnss", "net",
                       "uasyncio"})
# A dotted name after "tracking." in a string: device program texts and mock.patch targets.
DOTTED = re.compile(r"\btracking\.([A-Za-z_]\w*)")


def source_path(module):
    return os.path.join(REPO, "tracking", module + ".py")


def parse_module(module):
    with open(source_path(module), encoding="utf-8") as handle:
        return ast.parse(handle.read(), "tracking/%s.py" % module)


def submodules():
    """{name: module} of the 13 modules below the facade."""
    return {name: importlib.import_module("tracking." + name)
            for name in PACKAGE_MODULES if name != "__init__"}


def loaded_package_modules():
    """The facade and its 13 modules."""
    return [tracking] + list(submodules().values())


def repo_sources(skip=()):
    """Every *.py of the repository (no hidden or cache folders), without the top folders
    `skip`."""
    for root, dirs, files in os.walk(REPO):
        top = os.path.relpath(root, REPO) == "."
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d != "__pycache__"
                         and not (top and d in skip))
        for name in sorted(files):
            if name.endswith(".py"):
                yield os.path.join(root, name)


def parse_file(path):
    with open(path, encoding="utf-8") as handle:
        return ast.parse(handle.read(), path)


def module_level_imports(tree):
    """Import statements of the module level, those in a module-level try included."""
    found = []
    for node in tree.body:
        if isinstance(node, ast.Try):
            for part in node.body + [s for h in node.handlers for s in h.body] + node.orelse:
                if isinstance(part, (ast.Import, ast.ImportFrom)):
                    found.append(part)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            found.append(node)
    return found


def bound_by_import(node):
    return [alias.asname or alias.name.split(".")[0] for alias in node.names]


def module_definitions(tree):
    """Names that def, class or an assignment binds at the module level."""
    names = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                names += [n.id for n in ast.walk(target) if isinstance(n, ast.Name)]
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            names += [n.id for n in ast.walk(node.target) if isinstance(n, ast.Name)]
    return names


def rebound_globals():
    """Names a `global` statement in a tracking module rebinds."""
    names = set()
    for module in PACKAGE_MODULES:
        for node in ast.walk(parse_module(module)):
            if isinstance(node, ast.Global):
                names.update(node.names)
    return names


def consumed_names():
    """Names of tracking that code outside the package reads (E6).

    Scans every *.py of the repository outside tracking/ and docs/: `from tracking
    import X`, the attribute `tracking.X` and "tracking.X" in a string constant
    (device program texts, mock.patch targets). The hits are cut down to the
    module-level names of the tracking modules, which drops file names such as
    "tracking.index.json" and Tracker methods; module names and rebound globals do
    not count. A support.patch_module_global target is a plain name and no hit.
    """
    found = set()
    for path in repo_sources(skip=("tracking", "docs")):
        for node in ast.walk(parse_file(path)):
            if isinstance(node, ast.ImportFrom) and node.module == "tracking" and not node.level:
                found.update(alias.name for alias in node.names)
            elif (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                  and node.value.id == "tracking"):
                found.add(node.attr)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                found.update(DOTTED.findall(node.value))
    names = set()
    for module in submodules().values():
        names.update(n for n in vars(module) if not n.startswith("__"))
    return (found & names) - set(PACKAGE_MODULES) - rebound_globals()


def facade_names():
    """Names in the facade that are neither dunders nor its own modules."""
    return {name for name, value in vars(tracking).items() if not name.startswith("__")
            and not (isinstance(value, types.ModuleType)
                     and value.__name__ == "tracking." + name)}


def tracker_classes():
    return [cls for cls in tracking.Tracker.__mro__ if cls is not object]


def method_entries(cls):
    return {name for name in vars(cls) if name not in CLASS_DUNDERS}


class PackageLayoutTests(unittest.TestCase):
    def test_tracking_is_a_package_of_fixed_modules(self):
        self.assertTrue(hasattr(tracking, "__path__"))
        self.assertIn("tracking", production_packages(REPO))
        folder = os.path.join(REPO, "tracking")
        self.assertEqual(sorted(n[:-3] for n in os.listdir(folder) if n.endswith(".py")),
                         sorted(PACKAGE_MODULES))
        self.assertFalse(os.path.exists(os.path.join(REPO, "tracking.py")))
        entries = ["tracking/%s.py" % name for name in PACKAGE_MODULES]
        self.assertEqual([n for n in push.BOARD_FILES if n.startswith("tracking/")], entries)
        start = push.BOARD_FILES.index("pointstore.py") + 1
        self.assertEqual(list(push.BOARD_FILES[start:start + len(entries)]), entries)

    def test_tracker_is_composed_of_the_mixins(self):
        self.assertEqual([cls.__name__ for cls in tracking.Tracker.__mro__], list(MRO))
        for cls in tracker_classes()[1:]:
            with self.subTest(mixin=cls.__name__):
                for name, value in vars(cls).items():
                    if name.startswith("__") and name.endswith("__"):
                        self.assertIn(name, CLASS_DUNDERS)
                    else:
                        # property: Tracker.event_sequence moved with the journal
                        self.assertIsInstance(value, (types.FunctionType, staticmethod,
                                                      property), name)
                self.assertIsNone(cls.__doc__)
        for module in PACKAGE_MODULES:
            for node in parse_module(module).body:
                if isinstance(node, ast.ClassDef) and (node.name == "Tracker"
                                                       or node.name.endswith("Mixin")):
                    with self.subTest(cls=node.name):
                        self.assertNotIn("super", {n.id for n in ast.walk(node)
                                                   if isinstance(n, ast.Name)})

    def test_each_name_is_defined_once(self):
        owners = {}
        for module in PACKAGE_MODULES:
            if module == "__init__":
                continue
            for name in module_definitions(parse_module(module)):
                owners.setdefault(name, []).append(module)
        self.assertEqual({n: m for n, m in owners.items() if len(m) > 1}, {})
        self.assertEqual(module_definitions(parse_module("__init__")), list(OWNED))
        methods = {}
        for cls in tracker_classes():
            for name in method_entries(cls):
                methods.setdefault(name, []).append(cls.__name__)
        self.assertEqual({n: c for n, c in methods.items() if len(c) > 1}, {})

    def test_docs_name_the_tracking_modules(self):
        # docs/technical.md lists every package file; no user document still points
        # at the retired single file tracking.py.
        with open(os.path.join(REPO, "docs", "technical.md"), encoding="utf-8") as handle:
            technical = handle.read()
        for name in PACKAGE_MODULES:
            with self.subTest(module=name):
                self.assertIn("`tracking/%s.py`" % name, technical)
        documents = [os.path.join(REPO, "docs", n) for n in os.listdir(os.path.join(REPO, "docs"))
                     if n.endswith(".md")]
        documents += [os.path.join(REPO, n) for n in os.listdir(REPO)
                      if n.startswith("README") and n.endswith(".md")]
        self.assertTrue(documents)
        for path in documents:
            with self.subTest(document=os.path.relpath(path, REPO)):
                with open(path, encoding="utf-8") as handle:
                    self.assertNotIn("`tracking.py`", handle.read())

    def test_package_folder_survives_store_maintenance(self):
        # On the board the package folder /tracking sits next to the survey files
        # tracking.*: no clean-up of the store or the factory reset may touch it.
        tmp = tempfile.TemporaryDirectory(prefix="esp-rtk-26c-folder-")
        self.addCleanup(tmp.cleanup)
        cwd = os.getcwd()
        os.chdir(tmp.name)
        self.addCleanup(os.chdir, cwd)
        package = {os.path.join("tracking", "__init__.py"): b"x\n",
                   os.path.join("tracking", "core.py"): b"x\n"}
        os.mkdir("tracking")
        for path, data in package.items():
            with open(path, "wb") as handle:
                handle.write(data)
        with mock.patch.object(tracking.Tracker, "storage_capacity", return_value={
                "free_flash_bytes": 8_000_000, "required_flash_reserve_bytes": 3_000_000,
                "estimated_temporary_bytes": 16_384, "writable": True}):
            store = tracking.Tracker()
            self.assertEqual(store.path, "tracking")
            store.create_project("Folder")
            store.start_line("Line", 0, fix=FIX)
            for index in range(1, 6):
                store.add_vertex(dict(FIX, lon=8.0 + index / 10000,
                                      lon_e8=800000000 + index * 10000))
            store.compact()
            store.restore(store.backup())
            reloaded = tracking.Tracker()
            self.assertEqual(reloaded.feature_count(), store.feature_count())
            reloaded._discard_restore_staging()
        with mock.patch.object(identity, "rotate_stream_token"):
            self.assertTrue(cfg_factory.factory_reset({}, {}, (), lambda: True, mock.Mock()))
        self.assertTrue(os.path.isdir("tracking"))
        for path, data in package.items():
            with open(path, "rb") as handle:
                self.assertEqual(handle.read(), data, path)


class PackageImportTests(unittest.TestCase):
    def test_every_global_name_resolves(self):
        known = set(dir(builtins))
        for name, module in [("__init__", tracking)] + list(submodules().items()):
            with open(source_path(name), encoding="utf-8") as handle:
                table = symtable.symtable(handle.read(), "tracking/%s.py" % name, "exec")
            missing = set()

            def walk(scope, top):
                for symbol in scope.get_symbols():
                    if top:
                        used = symbol.is_referenced()
                    else:
                        used = symbol.is_global() and (symbol.is_referenced()
                                                       or symbol.is_assigned())
                    if used and symbol.get_name() not in vars(module) \
                            and symbol.get_name() not in known:
                        missing.add(symbol.get_name())
                for child in scope.get_children():
                    walk(child, False)
            walk(table, True)
            with self.subTest(module=name):
                self.assertEqual(sorted(missing), [])

    def test_every_import_is_used(self):
        for module in PACKAGE_MODULES:
            if module == "__init__":
                continue
            tree = parse_module(module)
            read = {n.id for n in ast.walk(tree)
                    if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
            imported = {name for node in module_level_imports(tree)
                        for name in bound_by_import(node)}
            with self.subTest(module=module):
                self.assertEqual(sorted(imported - read), [])

    def test_imports_follow_the_layers(self):
        for module in PACKAGE_MODULES:
            tree = parse_module(module)
            with self.subTest(module=module):
                for node in module_level_imports(tree):
                    names = ([node.module] if isinstance(node, ast.ImportFrom)
                             else [alias.name for alias in node.names])
                    for name in names:
                        top = (name or "").split(".")[0]
                        self.assertNotIn(top, LAZY_ONLY)
                        if name and name.startswith("tracking."):
                            self.assertLess(LAYERS[name.split(".")[1]], LAYERS[module], name)
                if module == "__init__":
                    continue
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom):
                        self.assertEqual(node.level, 0, node.lineno)
                        self.assertNotEqual(node.module, "tracking", node.lineno)
                    elif isinstance(node, ast.Import):
                        self.assertNotIn("tracking", [a.name for a in node.names], node.lineno)

    def test_hot_path_methods_resolve_within_three_classes(self):
        near = (tracking.Tracker, submodules()["recording"].RecordingMixin,
                submodules()["journal"].JournalMixin)
        for name in HOT_PATH:
            with self.subTest(method=name):
                self.assertTrue(any(name in vars(cls) for cls in near))


class FacadeTests(unittest.TestCase):
    def test_facade_exports_exactly_the_consumed_names(self):
        consumed = consumed_names()
        self.assertEqual(sorted(facade_names()),
                         sorted(consumed | set(INTERFACE) | set(OWNED)))
        self.assertEqual(consumed & set(OWNED), set())
        for name in sorted(facade_names() - set(OWNED)):
            binders = [m for m in submodules().values() if name in vars(m)]
            with self.subTest(name=name):
                self.assertTrue(binders)
                for module in binders:
                    self.assertIs(vars(module)[name], vars(tracking)[name], module.__name__)

    def test_no_rebound_global_is_exported(self):
        rebound = rebound_globals()
        self.assertTrue(rebound)
        self.assertEqual(sorted(rebound & set(vars(tracking))), [])

    def test_singleton_lives_only_in_the_facade(self):
        self.assertIsInstance(tracking.tracker, tracking.Tracker)
        for name, module in submodules().items():
            with self.subTest(module=name):
                self.assertNotIn("tracker", vars(module))
        self.assertEqual(set(module_definitions(parse_module("__init__"))), set(OWNED))
        self.assertEqual(type(tracking).owned, set(OWNED))

    def test_fan_out_reaches_every_module_that_binds_a_name(self):
        modules = loaded_package_modules()
        for name in ("log", "os", "ujson", "_now_seconds", "MAX_FEATURES"):
            binders = [m for m in modules if name in vars(m)]
            with self.subTest(name=name):
                with support.patch_module_global("tracking", name) as fake:
                    for module in binders:
                        self.assertIs(vars(module)[name], fake, module.__name__)
                for module in binders:
                    self.assertIsNot(vars(module)[name], fake, module.__name__)
                if name in ("log", "os"):
                    self.assertGreaterEqual(len([m for m in binders if m is not tracking]), 2)
        with support.patch_module_global("tracking", "open", create=True) as fake:
            self.assertEqual(len([m for m in modules if vars(m).get("open") is fake]),
                             len(PACKAGE_MODULES))
        self.assertEqual([m.__name__ for m in modules if "open" in vars(m)], [])

    def test_no_class_derives_from_tracker(self):
        derived = []
        for path in repo_sources():
            for node in ast.walk(parse_file(path)):
                if isinstance(node, ast.ClassDef):
                    for base in node.bases:
                        if (isinstance(base, ast.Name) and base.id == "Tracker") or (
                                isinstance(base, ast.Attribute) and base.attr == "Tracker"
                                and isinstance(base.value, ast.Name)
                                and base.value.id == "tracking"):
                            derived.append("%s:%d" % (os.path.relpath(path, REPO), node.lineno))
        self.assertEqual(derived, [])


if __name__ == "__main__":
    unittest.main()
