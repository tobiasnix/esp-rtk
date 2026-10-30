# SPDX-License-Identifier: AGPL-3.0-only
"""Requires no board: the app.js build (src/*.js -> app.js) and its consistency check in push.py."""
import gzip
import os
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import push
from support import web

SPDX = push.SPDX_LINE


class RootCase(unittest.TestCase):
    def make_root(self, files, app_js=None, plain=None):
        """Temp tree with src/<name> (bytes; sub folders are created) and optional app.js / plain files."""
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        if files is not None:
            os.makedirs(os.path.join(root, "src"))
            for name, data in files.items():
                path = os.path.join(root, "src", name)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as handle:
                    handle.write(data)
        if app_js is not None:
            self.write(root, "app.js", app_js)
        for name, data in (plain or {}).items():
            self.write(root, name, data)
        return root

    @staticmethod
    def write(root, name, data):
        with open(os.path.join(root, name), "wb") as handle:
            handle.write(data)

    @staticmethod
    def read(root, name):
        with open(os.path.join(root, name), "rb") as handle:
            return handle.read()

    @staticmethod
    def stat(root, name):
        info = os.stat(os.path.join(root, name))
        return info.st_mtime_ns, info.st_size


class TestRenderAppJs(RootCase):
    def test_concatenates_in_file_name_order_inside_one_iife(self):
        root = self.make_root({"20-b.js": SPDX + b"const b=2;\n", "10-a.js": SPDX + b"const a=1;\n"})
        self.assertEqual(push.APP_JS_HEADER,
                         b'// SPDX-License-Identifier: AGPL-3.0-only\n(()=>{"use strict";\n')
        self.assertEqual(push.APP_JS_FOOTER, b"})();\n")
        self.assertEqual(push.render_app_js(root),
                         push.APP_JS_HEADER + b"const a=1;\nconst b=2;\n" + push.APP_JS_FOOTER)

    def test_strips_exactly_the_first_spdx_line(self):
        root = self.make_root({"10-a.js": SPDX + SPDX + b"x;\n"})
        self.assertEqual(push.render_app_js(root).count(SPDX), 2)  # header + the second one

    def test_a_source_without_spdx_is_rejected(self):
        with self.assertRaisesRegex(push.BuildError, "10-a.js"):
            push.render_app_js(self.make_root({"10-a.js": b"const a=1;\n"}))

    def test_a_missing_src_directory_is_rejected(self):
        with self.assertRaises(push.BuildError):
            push.render_app_js(self.make_root(None))

    def test_unexpected_entries_are_rejected(self):
        for bad in ("a.js", "10_a.js", "10-A.js", "100-a.js", "10-a.mjs", "views/10-a.js"):
            with self.subTest(bad=bad):
                with self.assertRaises(push.BuildError):
                    push.render_app_js(self.make_root({"05-ok.js": SPDX, bad: SPDX}))

    def test_dotfiles_are_ignored(self):
        root = self.make_root({"10-a.js": SPDX + b"a;\n", ".10-a.js.swp": b"junk"})
        self.assertEqual(push.render_app_js(root), push.APP_JS_HEADER + b"a;\n" + push.APP_JS_FOOTER)

    def test_duplicate_slot_numbers_are_rejected(self):
        with self.assertRaisesRegex(push.BuildError, "10"):
            push.render_app_js(self.make_root({"10-a.js": SPDX, "10-b.js": SPDX}))

    def test_an_empty_source_directory_is_rejected(self):
        with self.assertRaises(push.BuildError):
            push.render_app_js(self.make_root({}))

    def test_a_directory_with_only_dotfiles_is_rejected(self):
        with self.assertRaises(push.BuildError):
            push.render_app_js(self.make_root({".keep": b""}))

    def test_a_fragment_must_end_with_a_newline(self):
        with self.assertRaisesRegex(push.BuildError, "10-a.js"):
            push.render_app_js(self.make_root({"10-a.js": SPDX + b"const a=1;"}))

    def test_an_spdx_only_fragment_contributes_nothing(self):
        root = self.make_root({"10-a.js": SPDX + b"a;\n", "95-empty.js": SPDX})
        self.assertEqual(push.render_app_js(root), push.APP_JS_HEADER + b"a;\n" + push.APP_JS_FOOTER)

    def test_duplicate_top_level_declarations_are_rejected(self):
        for first, second in ((b"function foo(){}\n", b"async function foo(){}\n"),
                              (b"const foo=1;\n", b"let foo=2;\n"),
                              (b"class foo {}\n", b"function foo(){}\n")):
            with self.subTest(second=second):
                root = self.make_root({"10-a.js": SPDX + first, "20-b.js": SPDX + second})
                with self.assertRaisesRegex(push.BuildError, "foo"):
                    push.render_app_js(root)

    def test_reassignments_and_nested_declarations_are_not_duplicates(self):
        root = self.make_root({"10-a.js": SPDX + b"function foo(){}\nconst base=foo;\n",
                               "20-b.js": SPDX + b"foo=function(){};\n  function foo(){}\n"
                                                 b"const bar=()=>{function foo(){}};\n"})
        push.render_app_js(root)  # no BuildError

    def test_top_level_names(self):
        self.assertEqual(push.top_level_names(
            b"function a(){}\nasync function b(){}\nclass C {}\nconst d=1,e=2;\nlet f;\n  const g=1;\n"),
            ["a", "b", "C", "d", "f"])

    def test_limited_contract_is_pinned(self):
        # E5: a declaration after another statement on the same line is not seen (app.js: ensureQr).
        self.assertEqual(push.top_level_names(b"let qrPromise=null;function ensureQr(){}\n"), ["qrPromise"])
        # `function` must be followed by whitespace or `*`: an identifier that merely starts with it is no declaration.
        self.assertEqual(push.top_level_names(b"functionality=1;\nfunctions.push(1);\n"), [])
        self.assertEqual(push.top_level_names(b"function*gen(){}\nfunction * spaced(){}\nasync function  b(){}\n"),
                         ["gen", "spaced", "b"])


class TestBuildAppJs(RootCase):
    FILES = {"10-a.js": SPDX + b"const a=1;\n", "20-b.js": SPDX + b"const b=2;\n"}

    def test_an_equal_app_js_is_left_untouched(self):
        root = self.make_root(self.FILES)
        self.write(root, "app.js", push.render_app_js(root))
        os.utime(os.path.join(root, "app.js"), (1_000_000, 1_000_000))
        self.assertIs(push.build_app_js(root), False)
        self.assertEqual(os.stat(os.path.join(root, "app.js")).st_mtime, 1_000_000)
        self.assertEqual(self.read(root, "app.js"), push.render_app_js(root))

    def test_a_missing_app_js_is_written(self):
        root = self.make_root(self.FILES)
        self.assertIs(push.build_app_js(root), True)
        self.assertEqual(self.read(root, "app.js"), push.render_app_js(root))
        self.assertFalse(os.path.exists(os.path.join(root, "app.js.tmp")))

    def test_a_differing_app_js_is_rebuilt(self):
        root = self.make_root(self.FILES, app_js=b"edited\n")
        self.assertIs(push.build_app_js(root), True)
        self.assertEqual(self.read(root, "app.js"), push.render_app_js(root))
        self.assertFalse(os.path.exists(os.path.join(root, "app.js.tmp")))

    def test_a_failed_replace_removes_the_temporary_file(self):
        root = self.make_root(self.FILES, app_js=b"keep\n")
        with mock.patch.object(push.os, "replace", side_effect=OSError("replace failed")):
            with self.assertRaisesRegex(OSError, "replace failed"):
                push.build_app_js(root)
        self.assertFalse(os.path.exists(os.path.join(root, "app.js.tmp")))
        self.assertEqual(self.read(root, "app.js"), b"keep\n")

    def test_a_failed_write_removes_the_temporary_file(self):
        root = self.make_root(self.FILES, app_js=b"keep\n")
        real_open = open

        def open_with_failing_write(path, mode="r", *args, **kwargs):
            handle = real_open(path, mode, *args, **kwargs)
            if str(path).endswith(".tmp"):
                handle.write(b"partial")
                handle.close()
                raise OSError("disk full")
            return handle
        with mock.patch.object(push, "open", open_with_failing_write, create=True):
            with self.assertRaisesRegex(OSError, "disk full"):
                push.build_app_js(root)
        self.assertFalse(os.path.exists(os.path.join(root, "app.js.tmp")))
        self.assertEqual(self.read(root, "app.js"), b"keep\n")

    def test_without_src_nothing_happens(self):
        root = self.make_root(None, app_js=b"x\n")
        self.assertIs(push.build_app_js(root), False)
        self.assertEqual(self.read(root, "app.js"), b"x\n")

    def test_a_failing_render_leaves_app_js_untouched(self):
        root = self.make_root({"10-a.js": SPDX + b"const foo=1;\n", "20-b.js": SPDX + b"let foo=2;\n"},
                              app_js=b"keep\n")
        with self.assertRaisesRegex(push.BuildError, "foo"):
            push.build_app_js(root)
        self.assertEqual(self.read(root, "app.js"), b"keep\n")
        self.assertFalse(os.path.exists(os.path.join(root, "app.js.tmp")))

    def test_build_assets_writes_the_compressed_copy(self):
        root = self.make_root(self.FILES)
        self.assertIsNone(push.build_assets(root))
        self.assertEqual(gzip.decompress(self.read(root, "app.js.gz")), self.read(root, "app.js"))
        self.assertEqual(self.read(root, "app.js"), push.render_app_js(root))


class TestCheckBuild(RootCase):
    FILES = {"10-a.js": SPDX + b"const a=1;\n", "20-b.js": SPDX + b"const b=2;\n"}
    PLAIN = {"app.css": b"body{margin:0}\n", "i18n.js": b"const T={};\n"}

    def consistent_root(self):
        root = self.make_root(self.FILES, plain=self.PLAIN)
        push.build_assets(root)
        return root

    def test_a_consistent_tree_has_no_problems(self):
        root = self.consistent_root()
        self.assertEqual(push.check_build(root), [])

    def test_a_stale_app_js_is_reported(self):
        root = self.consistent_root()
        self.write(root, "app.js", b"old\n")
        problems = push.check_build(root)
        self.assertEqual(len(problems), 1)
        self.assertTrue(problems[0].startswith("app.js:"), problems)
        self.assertEqual(self.read(root, "app.js"), b"old\n")

    def test_only_a_stale_committed_gzip_is_reported(self):
        root = self.consistent_root()
        self.write(root, "app.js.gz", gzip.compress(b"old\n"))
        os.utime(os.path.join(root, "app.js.gz"), (1_000_000, 1_000_000))
        before = self.read(root, "app.js.gz")
        problems = push.check_build(root)
        self.assertEqual(len(problems), 1)
        self.assertTrue(problems[0].startswith("app.js.gz"), problems)
        self.assertEqual(self.read(root, "app.js.gz"), before)
        self.assertEqual(os.stat(os.path.join(root, "app.js.gz")).st_mtime, 1_000_000)

    def test_a_missing_or_broken_gzip_is_reported(self):
        root = self.consistent_root()
        os.remove(os.path.join(root, "app.css.gz"))
        self.write(root, "i18n.js.gz", b"nogzip")
        problems = push.check_build(root)
        self.assertEqual(len(problems), 2, problems)
        self.assertTrue(any(p.startswith("app.css.gz") for p in problems), problems)
        self.assertTrue(any(p.startswith("i18n.js.gz") for p in problems), problems)
        self.assertFalse(os.path.exists(os.path.join(root, "app.css.gz")))
        self.assertEqual(self.read(root, "i18n.js.gz"), b"nogzip")

    def test_a_missing_app_js_is_reported_when_src_exists(self):
        root = self.consistent_root()
        os.remove(os.path.join(root, "app.js"))
        self.assertEqual(push.check_build(root), ["app.js: missing"])

    def test_a_different_gzip_representation_is_fine(self):
        root = self.consistent_root()
        self.write(root, "app.js.gz",
                   gzip.compress(self.read(root, "app.js"), compresslevel=1, mtime=123))
        self.assertEqual(push.check_build(root), [])

    def test_without_src_app_js_is_checked_against_its_own_gzip(self):
        root = self.make_root(None, app_js=b"x;\n", plain={"app.js.gz": gzip.compress(b"y;\n")})
        problems = push.check_build(root)
        self.assertEqual(len(problems), 1)
        self.assertTrue(problems[0].startswith("app.js.gz"), problems)

    def test_a_render_error_is_passed_through(self):
        root = self.make_root({"10-a.js": b"no spdx\n"}, app_js=b"x\n")
        with self.assertRaises(push.BuildError):
            push.check_build(root)


SOURCES = ["05-login.js", "10-core.js", "15-store-api.js", "17-route-cache.js", "20-navigation.js", "25-fix-gate.js",
           "30-connection.js", "35-components.js", "40-tracking-actions.js", "45-capacity.js",
           "50-status.js", "55-notices.js", "60-projects.js", "65-map.js", "68-export-flow.js", "70-project-detail.js",
           "72-frame-assignment.js",
           "75-device.js", "76-field-setup.js", "77-device-areas.js", "78-guided-apply.js", "80-configuration.js", "82-restart-panel.js",
           "85-log.js",
           "90-live-router.js", "91-live-stream.js", "93-stakeout-guide.js", "94-stakeout-import.js", "95-stakeout.js", "97-stakeout-actions.js",
           "99-boot.js"]


class TestRepositoryBuild(unittest.TestCase):
    def test_committed_app_js_is_the_build_of_src(self):
        with open(os.path.join(REPO, "app.js"), "rb") as fh:
            committed = fh.read()
        self.assertEqual(push.render_app_js(REPO), committed,
                         "app.js differs from src/: never edit app.js directly; edit src/*.js, "
                         "then run python3 push.py . --build-only")

    def test_committed_artifacts_pass_the_build_check(self):
        self.assertEqual(push.check_build(REPO), [])

    def test_source_layout(self):
        names = sorted(os.listdir(os.path.join(REPO, "src")))
        self.assertEqual(names, SOURCES)  # 95 is reserved for 95-stakeout.js (stakeout UI)

    def test_every_source_starts_at_a_top_level_statement(self):
        start = re.compile(rb"^(?:const |let |function |async function |class |// |addEventListener\()")
        for name in SOURCES:
            with open(os.path.join(REPO, "src", name), "rb") as fh:
                data = fh.read()
            self.assertTrue(data.startswith(push.SPDX_LINE), name)
            self.assertRegex(data[len(push.SPDX_LINE):], start, name)

    def test_build_sources_never_reach_the_board(self):
        self.assertEqual([n for n in push.BOARD_FILES if n.startswith("src")], [])
        self.assertEqual([dp for _lp, dp in push.targets(REPO) if dp.startswith("/src")], [])
        self.assertEqual([f for f, _t in web.STATIC_ASSETS.values() if f.startswith("src")], [])


    def test_progress_module_is_a_board_file(self):
        self.assertIn("stakeout_progress.py", push.BOARD_FILES)
        self.assertEqual(push.BOARD_FILES.index("stakeout_progress.py"),
                         push.BOARD_FILES.index("stakeout_session.py") + 1)


class TestCiWorkflow(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(REPO, ".github", "workflows", "ci.yml"), encoding="utf-8") as fh:
            self.text = fh.read()

    def test_ci_checks_the_committed_build_before_the_unit_tests(self):
        self.assertIn("run: python3 push.py . --build-only --check\n", self.text)
        self.assertLess(self.text.index("push.py . --build-only --check"),
                        self.text.index("python3 -m unittest discover -s tests"))

    def test_ci_runs_the_browser_acceptance_with_the_pinned_install(self):
        self.assertIn("run: python3 tests/browser_acceptance.py --install --with-deps"
                      " --shard ${{ matrix.shard }}\n", self.text)
        self.assertLess(self.text.index("name: Check JavaScript syntax"),
                        self.text.index("tests/browser_acceptance.py"))

    def test_ci_runs_every_browser_shard(self):
        self.assertIn('shard: ["1/4", "2/4", "3/4", "4/4"]\n', self.text)
        self.assertIn("fail-fast: false\n", self.text)

    def test_ci_checks_every_src_file_on_its_own(self):
        self.assertIn("for file in *.js src/*.js tests/*.js; do\n", self.text)


if __name__ == "__main__":
    unittest.main()
