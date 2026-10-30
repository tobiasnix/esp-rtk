# SPDX-License-Identifier: AGPL-3.0-only
"""Static checks of the tracking module's structure (event model, map delta writers)."""
import ast
import os
import shutil
import tempfile
import unittest

from support import REPO
from source_checks import called_names, production_packages
import tracking

# #26c: the modules of tracking/
TRACKING_SOURCES = tuple(sorted("tracking/" + name
                                for name in os.listdir(os.path.join(REPO, "tracking"))
                                if name.endswith(".py")))

JOURNAL_KINDS = {"project_created", "project_selected", "line_started", "profile_registered",
                 "vertex_added", "asset_added", "line_paused", "line_resumed", "line_selected",
                 "line_finished", "feature_removed", "snapshot", "project_renamed",
                 "project_deleted", "project_frame_assigned"}

MAP_DELTA_WRITERS = {"__init__", "_init_runtime_state", "_record_map_change",
                     "_reset_map_changes"}

# Sync variant -> the production path it wraps.
SYNC_WRAPS = {"on_fix": "enqueue_fix", "compact": "compact_async",
              "restore": "_restore_payload_steps", "restore_ndjson": "_restore_ndjson_steps",
              "archive_project": "_archive_project_steps",
              "verify_archive": "_verify_archive_steps",
              "reactivate_archive": "_reactivate_archive_steps",
              "verify_archive_path": "_verify_archive_path_steps",
              "map_data": "iter_map_data", "line_detail": "iter_line_detail"}
# Async variant -> the steps it shares with its sync variant.
ASYNC_SHARES = {"verify_archive_async": "_verify_archive_steps",
                "reactivate_archive_async": "_reactivate_archive_steps"}
# (sync variant, sync variant it may call): the only nesting, archiving compacts.
SYNC_IN_SYNC = {("archive_project", "compact")}
# Names only tests use, with the reason they stay in the device module.
TEST_SEAMS = {
    "TARGET_MAX_FEATURES": "read by the tool program texts",
    "HEIGHT_KEYS": "export contract, pinned by tests",
    "_commit_staged_restore": "power cut seam of the restore commit (#26a E7)",
    "on_fix": "sync wrapper", "restore_ndjson": "sync wrapper",
    "verify_archive_path": "sync wrapper", "map_data": "sync wrapper",
    "line_detail": "sync wrapper"}
# Names that production code spells for an object outside tracking; the
# name-based reference scan cannot tell the owners apart.
FOREIGN_HOMONYMS = {
    "on_fix": "stakeout_session.StakeoutSession.on_fix, the fix listener main.py registers"}


def parse(source):
    with open(os.path.join(REPO, source), encoding="utf-8") as handle:
        return ast.parse(handle.read(), source)


def functions(source):
    """Every function node of a source file by name, nested functions included."""
    found = {}
    for node in ast.walk(parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found.setdefault(node.name, []).append(node)
    return found


def _map_delta_writes(node, function=None):
    """(function, line) of every write to a tracker's map deltas below node."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        function = node.name
    targets = []
    if isinstance(node, ast.Assign):
        for target in node.targets:
            targets.extend(ast.walk(target))
    elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
        targets.extend(ast.walk(node.target))
    writes = [(function, node.lineno) for target in targets
              if isinstance(target, ast.Attribute)
              and target.attr in ("_map_changes", "_map_change_floor")]
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("append", "pop", "clear", "extend")
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "_map_changes"):
        writes.append((function, node.lineno))
    for child in ast.iter_child_nodes(node):
        writes.extend(_map_delta_writes(child, function))
    return writes


def tracker_methods():
    """Methods of class Tracker and its mixins by name, from every tracking source."""
    found = {}
    for source in TRACKING_SOURCES:
        for node in parse(source).body:
            if isinstance(node, ast.ClassDef) and (node.name == "Tracker"
                                                   or node.name.endswith("Mixin")):
                found.update((item.name, item) for item in node.body
                             if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)))
    return found


def _method_calls(node, function=None):
    """(innermost function, called attribute, line) of every `<x>.<name>(...)` below node."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        function = node.name
    calls = []
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        calls.append((function, node.func.attr, node.lineno))
    for child in ast.iter_child_nodes(node):
        calls.extend(_method_calls(child, function))
    return calls


def production_sources(repo=REPO):
    """Every Python module in the repository root, in its production packages and in
    tools/ (private only: the public copy has no tools/ folder)."""
    names = [name for name in os.listdir(repo) if name.endswith(".py")]
    for package in production_packages(repo):
        names += [package + "/" + name for name in os.listdir(os.path.join(repo, package))
                  if name.endswith(".py")]
    tools = os.path.join(repo, "tools")
    if os.path.isdir(tools):
        names += ["tools/" + name for name in os.listdir(tools) if name.endswith(".py")]
    return sorted(names)


def defined_names():
    """Module functions, classes, module assignments and non-dunder methods of
    every tracking source, by name."""
    found = {}
    for source in TRACKING_SOURCES:
        for node in parse(source).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                found[node.name] = node
            if isinstance(node, ast.ClassDef):
                found.update((item.name, item) for item in node.body
                             if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                             and not item.name.startswith("__"))
            elif isinstance(node, ast.Assign):
                found.update((target.id, node) for target in node.targets
                             if isinstance(target, ast.Name))
    return found


def referenced_names(sources):
    """Every name the sources load, access as an attribute, import, or spell
    as an identifier string."""
    names = set()
    for source in sources:
        # an import inside the package is no use; the facade re-exports
        in_package = source.startswith("tracking/")
        for node in ast.walk(parse(source)):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.alias):
                if not in_package:
                    names.add(node.name)
            elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
                  and node.value.isidentifier()):
                names.add(node.value)
    return names


def suite_sources():
    """Every Python module in tests/."""
    return sorted("tests/" + name for name in os.listdir(os.path.join(REPO, "tests"))
                  if name.endswith(".py"))


class ProductionSourcesTests(unittest.TestCase):
    """The public copy has no tools/; the source list must not need it."""

    def repo_with(self, names):
        repo = tempfile.mkdtemp(prefix="esp-rtk-sources-")
        self.addCleanup(shutil.rmtree, repo, True)
        for name in names:
            path = os.path.join(repo, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "w", encoding="utf-8").close()
        return repo

    def test_a_tree_without_tools_lists_root_and_package_modules(self):
        repo = self.repo_with(["main.py", "pkg/__init__.py", "pkg/part.py", "notes.txt"])
        self.assertEqual(production_sources(repo), ["main.py", "pkg/__init__.py", "pkg/part.py"])

    def test_tools_modules_are_listed_when_the_folder_exists(self):
        repo = self.repo_with(["main.py", "tools/helper.py", "tools/readme.txt"])
        self.assertEqual(production_sources(repo), ["main.py", "tools/helper.py"])


class EventModelStructureTests(unittest.TestCase):
    def test_every_journal_kind_has_one_handler(self):
        self.assertEqual(set(tracking.EVENT_HANDLERS), JOURNAL_KINDS)
        for kind, handler in tracking.EVENT_HANDLERS.items():
            with self.subTest(kind):
                self.assertEqual(handler.__name__, "_on_" + kind)
                self.assertIs(getattr(tracking.model, handler.__name__), handler)
        self.assertLessEqual(set(tracking.MAP_CHANGE_BUILDERS), JOURNAL_KINDS)
        self.assertLessEqual(set(tracking.ACTIVITY_EVENTS), JOURNAL_KINDS)
        # A list or dict kind must not raise in `kind in ACTIVITY_EVENTS`.
        self.assertIs(type(tracking.ACTIVITY_EVENTS), tuple)

    def test_apply_only_dispatches(self):
        [node] = functions("tracking/core.py")["_apply"]  # Tracker._apply, the only one
        self.assertLessEqual(node.end_lineno - node.lineno + 1, 20)
        for compare in ast.walk(node):
            if isinstance(compare, ast.Compare):
                for operand in [compare.left] + compare.comparators:
                    self.assertFalse(isinstance(operand, ast.Constant)
                                     and isinstance(operand.value, str),
                                     "string comparison at line %d" % compare.lineno)
        self.assertLessEqual({"EVENT_HANDLERS.get", "MAP_CHANGE_BUILDERS.get", "isinstance"},
                             called_names(node))

    def test_only_two_methods_write_the_map_deltas(self):
        writes = [(source, function, line) for source in TRACKING_SOURCES
                  for function, line in _map_delta_writes(parse(source))]
        self.assertTrue(writes)
        for source, function, line in writes:
            with self.subTest(source=source, line=line):
                self.assertIn(function, MAP_DELTA_WRITERS)


class NameStructureTests(unittest.TestCase):
    def test_no_name_in_tracking_is_dead(self):
        used = referenced_names(production_sources()) | referenced_names(suite_sources())
        self.assertEqual(sorted(name for name in defined_names() if name not in used), [])

    def test_test_only_names_are_thin_seams(self):
        defined = defined_names()
        production = referenced_names(production_sources()) - set(FOREIGN_HOMONYMS)
        tests = referenced_names(suite_sources())
        test_only = {name for name in defined if name in tests and name not in production}
        for name in sorted(test_only):
            node = defined[name]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                with self.subTest(name):
                    self.assertLessEqual(node.end_lineno - node.lineno + 1, 6)
        self.assertEqual(sorted(test_only), sorted(TEST_SEAMS))


class SyncWrapperStructureTests(unittest.TestCase):
    def test_sync_variants_wrap_their_production_path(self):
        methods = tracker_methods()
        for name, target in list(SYNC_WRAPS.items()) + list(ASYNC_SHARES.items()):
            with self.subTest(name):
                node = methods[name]
                if name in SYNC_WRAPS:
                    self.assertLessEqual(node.end_lineno - node.lineno + 1, 8)
                self.assertIn(target, called_names(node))

    def test_no_production_module_calls_a_sync_variant(self):
        for source in production_sources():
            for function, name, line in _method_calls(parse(source)):
                if name not in SYNC_WRAPS: continue
                with self.subTest(source=source, line=line):
                    self.assertTrue(source in TRACKING_SOURCES
                                    and (function, name) in SYNC_IN_SYNC,
                                    "%s calls %s" % (function, name))


class FunctionLengthStructureTests(unittest.TestCase):
    def test_no_function_exceeds_80_lines(self):
        longer = set()
        for source in TRACKING_SOURCES:
            for name, nodes in functions(source).items():
                if any(node.end_lineno - node.lineno + 1 > 80 for node in nodes):
                    longer.add(name)
        self.assertEqual(sorted(longer), [])


if __name__ == "__main__":
    unittest.main()
