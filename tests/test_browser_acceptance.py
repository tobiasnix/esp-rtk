# SPDX-License-Identifier: AGPL-3.0-only
"""Requires no browser: the one-command Playwright runner tests/browser_acceptance.py (suite table, env, exit logic)."""
import contextlib
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import browser_acceptance as runner

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODE = shutil.which("node")  # the runner itself starts "node", not "nodejs"


class TestBrowserAcceptanceRunner(unittest.TestCase):
    def test_every_harness_mode_is_a_suite(self):
        with open(os.path.join(REPO, "tests", "app_v2_acceptance.js"), encoding="utf-8") as fh:
            modes = set(re.findall(r'process\.argv\.includes\(\s*"(--[a-z0-9-]+-only)"\s*\)', fh.read()))
        wired = {args[1] for _n, args, _m in runner.SUITES if len(args) > 1}
        self.assertEqual(modes - {"--status-only"}, wired)

    def test_suite_names_are_unique_and_markers_exist(self):
        names = [n for n, _a, _m in runner.SUITES]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(len(names), 26)
        for _n, args, marker in runner.SUITES:
            with open(os.path.join(REPO, args[0]), encoding="utf-8") as fh:
                self.assertIn(marker, fh.read(), args)

    def test_fixtures_refuse_the_live_stream_like_older_firmware(self):
        """B4 (#27): a fixture that counts unmocked API requests, or answers every path, refuses /api/events with 404."""
        for name in ("app_v2_acceptance.js", "captive_acceptance.js", "guided_apply_acceptance.js"):
            with open(os.path.join(REPO, "tests", name), encoding="utf-8") as fh:
                source = fh.read()
            index = source.index('path === "/api/events"')
            self.assertIn("404", source[index:index + 600], name)
            if "unmocked.push" in source:
                self.assertLess(index, source.index("unmocked.push"), name)
        with open(os.path.join(REPO, "tests", "device_v2_acceptance.js"), encoding="utf-8") as fh:
            failed = fh.read().split('on("requestfailed"', 1)[1]
        self.assertIn('"/api/events"', failed.split("failures.push", 1)[0])

    def test_pinned_playwright_version(self):
        self.assertEqual(runner.PLAYWRIGHT_VERSION, "1.62.1")

    def test_install_dir(self):
        self.assertEqual(runner.install_dir({"RTK_PLAYWRIGHT_DIR": "/opt/pw"}), "/opt/pw")
        self.assertTrue(runner.install_dir({}).endswith(os.path.join(".cache", "esp-rtk", "playwright-1.62.1")))

    def test_node_env(self):
        sep, mods = os.pathsep, os.path.join("/opt/p", "node_modules")
        self.assertEqual(runner.node_env({"NODE_PATH": "/opt/a"}, "/opt/p", False)["NODE_PATH"], "/opt/a" + sep + mods)
        self.assertEqual(runner.node_env({"NODE_PATH": "/opt/a"}, "/opt/p", True)["NODE_PATH"], mods + sep + "/opt/a")
        self.assertEqual(runner.node_env({}, "/opt/p", False)["NODE_PATH"], mods)

    @unittest.skipUnless(NODE, "node not installed")
    def test_run_suites_needs_exit_zero_and_the_marker(self):
        suites = [("ok", ("-e", "console.log('x=ok')"), "x=ok"),
                  ("no-marker", ("-e", "console.log('other')"), "x=ok"),
                  ("fails", ("-e", "console.log('x=ok');process.exit(3)"), "x=ok"),
                  ("html", ("-e", "console.log(process.env.RTK_APP_HTML)"), "shell.html")]
        results = runner.run_suites(suites, dict(os.environ), "shell.html", timeout=30)
        self.assertEqual([(name, ok) for name, ok, _s, _out in results],
                         [("ok", True), ("no-marker", False), ("fails", False), ("html", True)])

    def test_list_and_unknown_suite(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(runner.main(["--list"]), 0)
        self.assertIn("field-diagnostics", out.getvalue())
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(runner.main(["--suite", "nope"]), 2)

    def test_shards_cover_every_suite_exactly_once(self):
        for count in (1, 2, 3, 4, 6):
            seen = []
            for index in range(1, count + 1):
                seen += [suite[0] for suite in runner.shard_of(runner.SUITES, index, count)]
            self.assertEqual(sorted(seen), sorted(name for name, _args, _marker in runner.SUITES), count)

    def test_four_shards_stay_balanced(self):
        loads = [sum(runner.SUITE_SECONDS.get(suite[0], runner.SHARD_DEFAULT_SECONDS)
                     for suite in runner.shard_of(runner.SUITES, index, 4)) for index in range(1, 5)]
        self.assertLess(max(loads), 1000, loads)

    def test_every_timed_suite_exists(self):
        names = {name for name, _args, _marker in runner.SUITES}
        self.assertLessEqual(set(runner.SUITE_SECONDS), names)

    def test_parse_shard(self):
        self.assertEqual(runner.parse_shard("2/4"), (2, 4))
        for bad in ("0/4", "5/4", "2", "a/b", "", None, "1/0", "01/4"):
            self.assertIsNone(runner.parse_shard(bad), bad)

    def test_main_runs_only_the_shard(self):
        code, _err, run, _build = self.run_main(["--shard", "1/4"])
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args[0][0], runner.shard_of(runner.SUITES, 1, 4))
        code, err, run, _build = self.run_main(["--shard", "5/4"])
        self.assertEqual(code, 2)
        self.assertIn("--shard", err)
        run.assert_not_called()

    def run_main(self, argv, version="1.62.1", build_error=None, results=()):
        """main(argv) with Playwright, the build and the suites mocked; (exit code, stderr, run_suites mock)."""
        err = io.StringIO()
        build = mock.Mock(side_effect=build_error)
        with mock.patch.object(runner.shutil, "which", return_value="/usr/bin/node"), \
                mock.patch.object(runner, "playwright_version", return_value=version), \
                mock.patch.object(runner.push, "build_assets", build), \
                mock.patch.object(runner, "run_suites", return_value=list(results)) as run, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = runner.main(argv)
        return code, err.getvalue(), run, build

    def test_main_exit_zero_when_every_suite_passes(self):
        code, _err, run, build = self.run_main(["--suite", "app"], results=[("app", True, 0.1, "")])
        self.assertEqual(code, 0)
        build.assert_called_once()
        self.assertEqual([name for name, _a, _m in run.call_args[0][0]], ["app"])

    def test_main_exit_one_when_a_suite_fails(self):
        code, err, _run, _build = self.run_main([], results=[("app", True, 0.1, ""), ("log", False, 0.1, "")])
        self.assertEqual(code, 1)
        self.assertIn("FAILED: log", err)

    def test_main_exit_one_when_the_build_fails(self):
        code, err, run, _build = self.run_main([], build_error=runner.push.BuildError("boom"))
        self.assertEqual(code, 1)
        self.assertIn("Build failed: boom", err)
        run.assert_not_called()

    def test_main_exit_two_without_playwright(self):
        code, err, run, build = self.run_main([], version=None)
        self.assertEqual(code, 2)
        self.assertIn("not found", err)
        build.assert_not_called()
        run.assert_not_called()

    def test_main_warns_about_another_playwright_version_but_goes_on(self):
        code, err, _run, _build = self.run_main(["--suite", "app"], version="1.0.0",
                                                results=[("app", True, 0.1, "")])
        self.assertEqual(code, 0)
        self.assertIn("WARNING: Playwright 1.0.0 found, 1.62.1 expected", err)

    def test_html_into_a_missing_directory_creates_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "a", "b", "shell.html")
            code, _err, run, _build = self.run_main(["--suite", "app", "--html", target],
                                                    results=[("app", True, 0.1, "")])
            self.assertEqual(code, 0)
            self.assertTrue(os.path.getsize(target) > 0)
            self.assertEqual(run.call_args[0][2], target)

    def test_html_to_an_unwritable_path_exits_two(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocker = os.path.join(tmp, "file")
            with open(blocker, "w", encoding="utf-8"):
                pass
            code, err, run, _build = self.run_main(["--html", os.path.join(blocker, "x", "shell.html")])
        self.assertEqual(code, 2)
        self.assertIn("Cannot write", err)
        run.assert_not_called()

    def test_runner_starts_without_pythonpath(self):
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        with tempfile.TemporaryDirectory() as cwd:
            result = subprocess.run([sys.executable, os.path.join(REPO, "tests", "browser_acceptance.py"), "--list"],
                                    cwd=cwd, env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("field-diagnostics", result.stdout)

    def test_runner_has_no_local_paths(self):
        for name in ("browser_acceptance.py", "test_browser_acceptance.py"):
            with open(os.path.join(REPO, "tests", name), encoding="utf-8") as fh:
                self.assertNotRegex(fh.read(), r"(?<![\w.])/(?:wo" r"rk|ho" r"me/[a-z])")


if __name__ == "__main__":
    unittest.main()
