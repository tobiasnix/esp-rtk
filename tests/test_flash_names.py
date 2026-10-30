# SPDX-License-Identifier: AGPL-3.0-only
"""Flash names the firmware writes never shadow a firmware module.

MicroPython looks for a directory <name> before <name>.py when it imports <name>,
and a directory without __init__.py is an empty namespace package. A data
folder at the flash root named like a board module therefore replaces that
module after the next restart (a stakeout/ folder next to stakeout.py stopped
the start). CPython prefers the .py file, so only these checks see it.
"""
import ast
import os
import re
import shutil
import tempfile
import unittest
from unittest import mock

from support import REPO, cfg
import ble
import field_diagnostics
import identity
import parallel_benchmark
import push
import stakeout
import tracking

# The board's importable names: every .py of BOARD_FILES without its extension,
# a package by its folder.
MODULES = frozenset(name.split("/")[0] if "/" in name else name[:-3]
                    for name in push.BOARD_FILES if name.endswith(".py"))
# A string that starts like "<identifier>/...": a path below a root folder.
FOLDER_PATH = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)/")
OS_PATH_CALLS = frozenset({"mkdir", "rename", "remove", "rmdir", "stat", "listdir", "ilistdir"})


def conflict_folder():
    """The name the migration gives an old stakeout/ folder when stakeout.sets/ exists."""
    folder = tempfile.mkdtemp(prefix="esp-rtk-conflict-")
    try:
        os.mkdir(os.path.join(folder, "stakeout"))
        os.mkdir(os.path.join(folder, stakeout.DEFAULT_ROOT))
        with mock.patch.object(cfg, "log"):
            load_migration()(os.path.join(folder, "stakeout"),
                             os.path.join(folder, stakeout.DEFAULT_ROOT))
        names = sorted(set(os.listdir(folder)) - {stakeout.DEFAULT_ROOT})
    finally:
        shutil.rmtree(folder, True)
    assert len(names) == 1 and names[0] != "stakeout", names
    return names[0]


def data_paths():
    """Every flash path (or path prefix) at the root the firmware writes, from its constants."""
    tracker = tracking.tracker
    recorder = field_diagnostics.recorder
    return {
        "stakeout root": stakeout.store.root,
        "stakeout default root": stakeout.DEFAULT_ROOT,
        "stakeout conflict folders": conflict_folder(),
        "tracking index": tracker.index_path,
        "tracking checkpoint": tracker.checkpoint_path,
        "tracking segments": tracker.segment_prefix,
        "tracking points": tracker.point_path,
        "tracking restore commit": tracker.restore_commit_path,
        "tracking restore staging": tracker.path + ".restore",
        "tracking archives": tracker.archive_dir,
        "tracking archive index": tracker.archive_index_path,
        "field diagnostics state": recorder.prefix + ".state",
        "field diagnostics segments": recorder.prefix + "-",
        "config": cfg.DEFAULT_CONFIG["config_file"],
        "config pending": cfg.PENDING_CONFIG_FILE,
        "config last good": cfg.LAST_GOOD_CONFIG_FILE,
        "config result": cfg.CONFIG_RESULT_FILE,
        "boot count": cfg.BOOTCOUNT_FILE,
        "planned reset": cfg.PLANNED_RESET_FILE,
        "error log": cfg.ERRORLOG_FILE,
        "identity": identity.IDENTITY_FILE,
        "bonds": ble._BOND_FILE,
        "bonds tmp": ble._BOND_TEMP_FILE,
        "bonds previous": ble._BOND_PREVIOUS_FILE,
        "parallel benchmark": parallel_benchmark.PREFIX,
        "parallel benchmark archives": parallel_benchmark.PREFIX + ".archives",
    }


def board_sources():
    for name in push.BOARD_FILES:
        if name.endswith(".py"):
            with open(os.path.join(REPO, name), encoding="utf-8") as handle:
                yield name, ast.parse(handle.read(), name)


def own_package(top):
    """True for a package's own folder: listed in PACKAGES and a real package on disk."""
    return top in push.PACKAGES and os.path.isfile(os.path.join(REPO, top, "__init__.py"))


class TestFlashNames(unittest.TestCase):
    def test_board_modules_are_known(self):
        for name in ("main", "web", "stakeout", "stakeout_progress", "state", "tracking"):
            self.assertIn(name, MODULES)

    def test_stakeout_root_can_never_be_a_module_name(self):
        self.assertEqual(stakeout.DEFAULT_ROOT, "stakeout.sets")
        self.assertEqual(stakeout.store.root, stakeout.DEFAULT_ROOT)
        self.assertFalse(stakeout.DEFAULT_ROOT.isidentifier())
        self.assertTrue(any(not (char.isalnum() or char == "_")
                            for char in stakeout.DEFAULT_ROOT))

    def test_no_data_root_shadows_a_board_module(self):
        for label, path in data_paths().items():
            top = path.split("/")[0]
            with self.subTest(label, path=path):
                self.assertTrue(top)
                self.assertNotIn(top, MODULES)
                # Not even a future module: a root name is no identifier.
                self.assertFalse(top.isidentifier(), top)

    def test_no_literal_flash_path_starts_in_a_module_folder(self):
        hits = []
        for name, tree in board_sources():
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    match = FOLDER_PATH.match(node.value)
                    if match and match.group(1) in MODULES and not own_package(match.group(1)):
                        hits.append("%s:%d %r" % (name, node.lineno, node.value))
        self.assertEqual(hits, [])

    def test_no_literal_flash_call_names_a_module(self):
        hits = []
        for name, tree in board_sources():
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                func = node.func
                called = (func.attr if isinstance(func, ast.Attribute)
                          and isinstance(func.value, ast.Name) and func.value.id == "os"
                          else func.id if isinstance(func, ast.Name) else None)
                if called not in OS_PATH_CALLS | {"open", "TargetStore", "Tracker", "FieldRecorder"}:
                    continue
                for arg in node.args[:2]:
                    if (isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                            and arg.value.split("/")[0] in MODULES
                            and not own_package(arg.value.split("/")[0])):
                        hits.append("%s:%d %s(%r)" % (name, node.lineno, called, arg.value))
        self.assertEqual(hits, [])


def main_source():
    with open(os.path.join(REPO, "main.py"), encoding="utf-8") as handle:
        return handle.read()


def load_migration():
    """The migration function from main.py's source; main.py deletes it after its call."""
    tree = ast.parse(main_source(), "main.py")
    node = next(item for item in tree.body
                if isinstance(item, ast.FunctionDef) and item.name == "_move_stakeout_root")
    namespace = {}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "main.py", "exec"), namespace)
    return namespace["_move_stakeout_root"]


class TestStakeoutRootMigration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="esp-rtk-root-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.old = os.path.join(self.tmp, "stakeout")
        self.new = os.path.join(self.tmp, "stakeout.sets")
        self.move = load_migration()
        patcher = mock.patch.object(cfg, "log")
        self.log = patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, path, text="x"):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            handle.write(text)

    def listing(self):
        result = []
        for root, _dirs, files in os.walk(self.tmp):
            for name in files:
                result.append(os.path.relpath(os.path.join(root, name), self.tmp))
        return sorted(result)

    def test_defaults_are_the_flash_names(self):
        defaults = self.move.__defaults__
        self.assertEqual(defaults, ("stakeout", stakeout.DEFAULT_ROOT))

    def test_old_folder_is_renamed(self):
        self.write(os.path.join(self.old, "project-a", "set.json"), "set")
        self.write(os.path.join(self.old, "project-a", "progress.jsonl"), "log")
        self.move(self.old, self.new)
        self.assertFalse(os.path.exists(self.old))
        self.assertEqual(self.listing(), ["stakeout.sets/project-a/progress.jsonl",
                                          "stakeout.sets/project-a/set.json"])
        with open(os.path.join(self.new, "project-a", "set.json")) as handle:
            self.assertEqual(handle.read(), "set")
        self.assertEqual(self.log.call_count, 1)
        self.assertEqual(self.log.call_args[0][:2], ("INFO", "SYS"))

    def test_both_present_moves_the_old_folder_aside_with_one_warning(self):
        self.write(os.path.join(self.old, "project-a", "old.json"), "old")
        self.write(os.path.join(self.new, "project-b", "new.json"), "new")
        self.move(self.old, self.new)
        self.assertFalse(os.path.exists(self.old))
        self.assertEqual(self.listing(), ["stakeout.conflict-1/project-a/old.json",
                                          "stakeout.sets/project-b/new.json"])
        with open(os.path.join(self.tmp, "stakeout.conflict-1", "project-a", "old.json")) as handle:
            self.assertEqual(handle.read(), "old")
        self.assertEqual(self.log.call_count, 1)
        self.assertEqual(self.log.call_args[0][:2], ("WARN", "SYS"))
        self.assertIn("stakeout.conflict-1", self.log.call_args[0][2])

    def test_a_taken_conflict_name_is_skipped(self):
        self.write(os.path.join(self.old, "project-a", "old.json"))
        self.write(os.path.join(self.new, "project-b", "new.json"))
        self.write(os.path.join(self.tmp, "stakeout.conflict-1", "earlier.json"))
        self.move(self.old, self.new)
        self.assertEqual(self.listing(), ["stakeout.conflict-1/earlier.json",
                                          "stakeout.conflict-2/project-a/old.json",
                                          "stakeout.sets/project-b/new.json"])
        self.assertEqual(self.log.call_args[0][:2], ("WARN", "SYS"))
        self.assertIn("stakeout.conflict-2", self.log.call_args[0][2])

    def test_all_conflict_names_taken_leaves_everything_with_an_error(self):
        self.write(os.path.join(self.old, "project-a", "old.json"))
        self.write(os.path.join(self.new, "project-b", "new.json"))
        for n in range(1, 10):
            self.write(os.path.join(self.tmp, "stakeout.conflict-%d" % n, "x.json"))
        before = self.listing()
        self.move(self.old, self.new)
        self.assertEqual(self.listing(), before)
        self.assertEqual(self.log.call_count, 1)
        self.assertEqual(self.log.call_args[0][:2], ("ERROR", "SYS"))

    def test_a_file_named_like_the_old_folder_is_left_alone(self):
        self.write(self.old, "not a folder")
        self.move(self.old, self.new)
        self.assertEqual(self.listing(), ["stakeout"])
        self.log.assert_not_called()

    def test_nothing_present_is_a_no_op(self):
        self.move(self.old, self.new)
        self.assertEqual(os.listdir(self.tmp), [])
        self.log.assert_not_called()

    def test_only_the_new_folder_is_a_no_op(self):
        self.write(os.path.join(self.new, "project-b", "new.json"))
        self.move(self.old, self.new)
        self.assertEqual(self.listing(), ["stakeout.sets/project-b/new.json"])
        self.log.assert_not_called()

    def test_a_failing_rename_is_logged_and_does_not_raise(self):
        self.write(os.path.join(self.old, "project-a", "set.json"))
        with mock.patch.object(os, "rename", side_effect=OSError(5, "io")):
            self.move(self.old, self.new)
        self.assertTrue(os.path.isdir(self.old))
        self.assertEqual(self.log.call_args[0][:2], ("ERROR", "SYS"))

    def test_runs_before_any_import_and_is_deleted(self):
        tree = ast.parse(main_source(), "main.py")
        body = tree.body
        call = next(index for index, item in enumerate(body)
                    if isinstance(item, ast.Expr) and isinstance(item.value, ast.Call)
                    and getattr(item.value.func, "id", None) == "_move_stakeout_root")
        first_import = next(index for index, item in enumerate(body)
                            if isinstance(item, (ast.Import, ast.ImportFrom)))
        self.assertLess(call, first_import)
        self.assertTrue(any(isinstance(item, ast.Delete)
                            and any(getattr(t, "id", None) == "_move_stakeout_root"
                                    for t in item.targets)
                            for item in body[call + 1:first_import]))
        # The function imports nothing but os (and cfg for its rare log line).
        function = next(item for item in body if isinstance(item, ast.FunctionDef)
                        and item.name == "_move_stakeout_root")
        imported = set()
        for node in ast.walk(function):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module)
        self.assertEqual(imported, {"os", "cfg"})

    def test_cfg_does_not_load_stakeout(self):
        """The log line imports cfg early; cfg's import chain must not reach stakeout."""
        def module_imports(module):
            path = os.path.join(REPO, module + ".py")
            if not os.path.isfile(path):
                path = os.path.join(REPO, module, "__init__.py")
            if not os.path.isfile(path):
                return set()  # built-in
            with open(path, encoding="utf-8") as handle:
                tree = ast.parse(handle.read(), path)
            names = set()
            nodes = list(tree.body)
            while nodes:
                node = nodes.pop()
                if isinstance(node, ast.Import):
                    names.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                    names.add(node.module.split(".")[0])
                elif isinstance(node, (ast.Try, ast.If)):
                    nodes.extend(node.body + node.orelse + getattr(node, "finalbody", []))
                    for handler in getattr(node, "handlers", []):
                        nodes.extend(handler.body)
            return names
        seen, todo = set(), ["cfg"]
        while todo:
            module = todo.pop()
            if module not in seen:
                seen.add(module)
                todo.extend(module_imports(module))
        self.assertIn("cfg_logging", seen)
        self.assertNotIn("stakeout", seen)


if __name__ == "__main__":
    unittest.main()
