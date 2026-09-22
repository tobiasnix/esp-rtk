# SPDX-License-Identifier: AGPL-3.0-only
"""Requires no board: push.py can be imported since the __main__ guard without opening a serial port.
"""
import os
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import push


class TestDirsOf(unittest.TestCase):
    def test_file_in_the_root_directory(self):
        self.assertEqual(push.dirs_of("/main.py"), [])

    def test_one_level(self):
        self.assertEqual(push.dirs_of("/lib/foo.py"), ["/lib"])

    def test_several_levels_from_outside_to_inside(self):
        # The order counts: mkdir cannot create an intermediate directory.
        self.assertEqual(push.dirs_of("/a/b/c/d.py"), ["/a", "/a/b", "/a/b/c"])


class TestTargets(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="esp-rtk-push-")

    def write_file(self, rel, content=b"x"):
        path = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(content)
        return path

    def test_a_single_file_lands_in_the_root_directory(self):
        path = self.write_file("main.py")
        self.assertEqual(push.targets(path), [(path, "/main.py")])

    def test_ordner_flach(self):
        self.write_file("a.py")
        self.write_file("b.py")
        board = [dp for _lp, dp in push.targets(self.tmp)]
        self.assertEqual(board, ["/a.py", "/b.py"])

    def test_folder_with_subfolder(self):
        self.write_file("main.py")
        self.write_file("lib/helper.py")
        board = [dp for _lp, dp in push.targets(self.tmp)]
        self.assertEqual(board, ["/lib/helper.py", "/main.py"])

    def test_order_is_stable(self):
        for name in ("z.py", "a.py", "m.py"):
            self.write_file(name)
        self.assertEqual(push.targets(self.tmp), sorted(push.targets(self.tmp)))

    def test_empty_folder(self):
        self.assertEqual(push.targets(self.tmp), [])

    def test_host_metadata_and_python_cache_are_ignored(self):
        self.write_file("main.py")
        self.write_file(".git/objects/ab/cd")
        self.write_file("__pycache__/main.cpython-313.pyc")
        self.write_file(".env")
        board = [dp for _lp, dp in push.targets(self.tmp)]
        self.assertEqual(board, ["/main.py"])

    def test_repository_manifest_contains_runtime_files_only(self):
        board = [dp for _lp, dp in push.targets(REPO)]
        self.assertEqual(board, ["/" + name for name in push.BOARD_FILES])
        self.assertNotIn("/push.py", board)
        self.assertNotIn("/README.md", board)
        self.assertIn("/qr.js", board)
        for runtime_file in ("cfg_validation.py", "web_routing.py",
                             "base.css", "app.css", "app.js",
                             "pages.css", "pages.js"):
            self.assertIn("/" + runtime_file, board)


class TestChunkSize(unittest.TestCase):
    def test_chunk_is_big_enough(self):
        """256 bytes meant ~190 Raw-REPL conversions for main.py."""
        self.assertGreaterEqual(push.CHUNK, 1024)


class TestPushCli(unittest.TestCase):
    def test_help_does_not_open_a_port(self):
        result = subprocess.run([sys.executable, os.path.join(REPO, "push.py"), "--help"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("Use:", result.stdout)
        self.assertNotIn("Port:", result.stdout)

    def test_missing_source_aborts(self):
        result = subprocess.run([sys.executable, os.path.join(REPO, "push.py")],
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Source is missing", result.stderr)


if __name__ == "__main__":
    unittest.main()
