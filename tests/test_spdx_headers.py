# SPDX-License-Identifier: AGPL-3.0-only
"""Every project source file declares the AGPL SPDX identifier."""
import os
import shutil
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _is_git_checkout(repo=REPO):
    """True if `repo` has a .git directory, i.e. `git ls-files` will work.

    A release tarball (the export checkout after its files are extracted
    elsewhere, or any exported copy without its .git) has no such directory,
    so tests that shell out to git must not crash there.
    """
    return os.path.isdir(os.path.join(repo, ".git"))


class TestGitCheckoutDetection(unittest.TestCase):
    def test_a_plain_directory_is_not_a_git_checkout(self):
        repo = tempfile.mkdtemp(prefix="esp-rtk-nogit-")
        self.addCleanup(shutil.rmtree, repo, True)
        self.assertFalse(_is_git_checkout(repo))

    def test_a_directory_with_dot_git_is_a_git_checkout(self):
        repo = tempfile.mkdtemp(prefix="esp-rtk-git-")
        self.addCleanup(shutil.rmtree, repo, True)
        os.makedirs(os.path.join(repo, ".git"))
        self.assertTrue(_is_git_checkout(repo))


class TestSpdxHeaders(unittest.TestCase):
    @unittest.skipUnless(_is_git_checkout(), "needs a git checkout")
    def test_active_project_sources_declare_agpl_spdx(self):
        root = REPO
        names = subprocess.check_output(
            ["git", "ls-files", "-z", "*.py", "*.js", "*.css"],
            cwd=root).decode().split("\0")
        excluded = {"qr.js"}
        missing = []
        for name in filter(None, names):
            if name in excluded or name.startswith("backup/"):
                continue
            with open(os.path.join(root, name), encoding="utf-8") as fh:
                header = "".join(fh.readline() for _ in range(2))
            if "SPDX-License-Identifier: AGPL-3.0-only" not in header:
                missing.append(name)
        self.assertEqual(missing, [], "missing AGPL SPDX header")


if __name__ == "__main__":
    unittest.main()
