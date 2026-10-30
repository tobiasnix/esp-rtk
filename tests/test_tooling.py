# SPDX-License-Identifier: AGPL-3.0-only
"""Requires no board: push.py can be imported since the __main__ guard without opening a serial port.
"""
import ast
import base64
import contextlib
import gzip
import io
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import push
from source_checks import production_packages


def loaded_repository_files(module):
    """Repository files an import of `module` loads, outermost first.

    `a.b` loads `a/__init__.py` (or `a.py`) and then `a/b/__init__.py` (or
    `a/b.py`); a package directory wins over a module file, as on the board.
    Names that resolve to no repository file (standard or board library) give
    nothing.
    """
    parts = module.split(".")
    files = []
    for depth in range(1, len(parts) + 1):
        stem = "/".join(parts[:depth])
        for candidate in (stem + "/__init__.py", stem + ".py"):
            if os.path.isfile(os.path.join(REPO, candidate)):
                files.append(candidate)
                break
    return files


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

    def test_an_external_build_project_uses_the_allowlist(self):
        for rel in ("src/10-a.js", "assets.py", "push.py", "app.js", "web.py", "README.md",
                    "tests/test_x.py"):
            self.write_file(rel)
        board = [dp for _lp, dp in push.targets(self.tmp)]
        self.assertEqual(board, ["/" + n for n in push.BOARD_FILES
                                 if n in ("assets.py", "app.js", "web.py")])

    def test_a_backup_with_a_stray_src_folder_is_pushed_as_a_tree(self):
        # A board backup never contains push.py (not in BOARD_FILES).
        for rel in ("src/10-a.js", "assets.py", "app.js", "config.json"):
            self.write_file(rel)
        self.assertFalse(push.is_build_project(self.tmp))
        self.assertEqual([dp for _lp, dp in push.targets(self.tmp)],
                         ["/app.js", "/assets.py", "/config.json", "/src/10-a.js"])

    def test_a_backup_without_src_is_pushed_as_a_tree(self):
        for rel in ("assets.py", "app.js", "config.json", "lib/helper.py"):
            self.write_file(rel)
        self.assertEqual([dp for _lp, dp in push.targets(self.tmp)],
                         ["/app.js", "/assets.py", "/config.json", "/lib/helper.py"])

    def test_repository_manifest_contains_runtime_files_only(self):
        board = [dp for _lp, dp in push.targets(REPO)]
        self.assertEqual(board, ["/" + name for name in push.BOARD_FILES])
        self.assertNotIn("/push.py", board)
        self.assertNotIn("/README.md", board)
        self.assertIn("/qr.js", board)
        self.assertIn("/i18n.js.gz", board)
        for runtime_file in ("cfg_validation.py", "web_routing.py",
                             "base.css", "app.css", "app.js",
                             "pages.css", "pages.js"):
            self.assertIn("/" + runtime_file, board)

    def test_board_files_import_only_modules_that_are_on_the_board(self):
        # A module in the repository root imports fine on the host but is
        # missing on the board when BOARD_FILES leaves it out. That is how
        # geo.py, imported by gnss.py and tracking.py, was left out.
        board = set(push.BOARD_FILES)
        missing = set()
        for name in push.BOARD_FILES:
            if not name.endswith(".py"):
                continue
            with open(os.path.join(REPO, name), encoding="utf-8") as source:
                tree = ast.parse(source.read(), name)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and not node.level:
                    imported = [node.module]
                else:
                    continue
                for module in imported:
                    for path in loaded_repository_files(module):
                        if path not in board:
                            missing.add("%s imports %s" % (name, path))
        self.assertEqual(sorted(missing), [])

    def test_a_single_package_file_keeps_its_folder_on_the_board(self):
        link = os.path.join(REPO, "state", "link.py")
        self.assertEqual(push.targets(link), [(link, "/state/link.py")])
        web = os.path.join(REPO, "web.py")
        self.assertEqual(push.targets(web), [(web, "/web.py")])
        outside = self.write_file("state/link.py")
        self.assertEqual(push.targets(outside), [(outside, "/link.py")])

    def test_package_init_is_written_last_of_its_package(self):
        packages = production_packages(REPO)
        self.assertEqual(list(push.PACKAGES), packages)
        for package in packages:
            with self.subTest(package=package):
                entries = [name for name in push.BOARD_FILES if name.startswith(package + "/")]
                self.assertEqual(entries[-1], package + "/__init__.py")
        for _retired, successor in push.RETIRED_BOARD_FILES:
            self.assertIn(successor.lstrip("/"), push.BOARD_FILES)

    def test_every_retired_module_is_replaced_by_its_package(self):
        packages = production_packages(REPO)
        for retired, successor in push.RETIRED_BOARD_FILES:
            with self.subTest(retired=retired):
                package = retired[1:-len(".py")]
                self.assertEqual(retired, "/%s.py" % package)
                self.assertEqual(successor, "/%s/__init__.py" % package)
                self.assertIn(package, packages)
                self.assertIn(successor[1:], push.BOARD_FILES)
                self.assertNotIn(retired[1:], push.BOARD_FILES)


class TestPrecompressedAssets(unittest.TestCase):
    def test_build_precompressed_assets_writes_i18n_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            plain = b"window.I18N={};" * 200
            with open(os.path.join(tmp, "i18n.js"), "wb") as fh:
                fh.write(plain)
            gz = os.path.join(tmp, "i18n.js.gz")
            push.build_precompressed_assets(tmp)
            with open(gz, "rb") as fh:
                self.assertEqual(fh.read(), gzip.compress(plain, compresslevel=9, mtime=0))
            os.utime(gz, (1000000, 1000000))
            push.build_precompressed_assets(tmp)
            self.assertEqual(os.stat(gz).st_mtime, 1000000)


class TestChunkSize(unittest.TestCase):
    def test_chunk_is_big_enough(self):
        """256 bytes meant ~190 Raw-REPL conversions for main.py."""
        self.assertGreaterEqual(push.CHUNK, 1024)


class TestPushCli(unittest.TestCase):
    def test_help_does_not_open_a_port(self):
        # The unused port argument is a safety net: should --help ever reach the
        # board, it fails on this path instead of a real device.
        command = [sys.executable, os.path.join(REPO, "push.py"), REPO,
                   os.path.join(REPO, "no-such-port"), "--help"]
        self.assertFalse(os.path.exists(command[3]))
        result = subprocess.run(command, capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0)
        self.assertIn("Use:", result.stdout)
        self.assertNotIn("Port:", result.stdout)

    def test_missing_source_aborts(self):
        code, calls, board = self.run_main()
        self.assertIn("Source is missing", str(code))
        self.assertEqual(calls, [])
        board.assert_not_called()

    def run_main(self, *argv, build_effect=None, check_result=None):
        calls = []

        def build(root):
            calls.append(("build", root))

        def check(root):
            calls.append(("check", root))
            return check_result or []

        def board(*_args, **_kwargs):
            calls.append(("board",))
            return True

        with mock.patch.object(sys, "argv", ["push.py", *argv]), \
                mock.patch.object(push, "build_assets", side_effect=build_effect or build), \
                mock.patch.object(push, "check_build", side_effect=check), \
                mock.patch.object(push, "with_retry", side_effect=board) as board_mock, \
                mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            try:
                push.main()
                code = 0
            except SystemExit as exc:
                code = exc.code
        return code, calls, board_mock

    def test_unknown_option_is_rejected_before_any_board_access(self):
        for argv in ((".", "--build_only"), (".", "--buildonly"),
                     (".", "--skip-unchanged=1"), (".", "--check")):
            with self.subTest(argv=argv):
                code, calls, board = self.run_main(*argv)
                self.assertEqual((code, calls), (2, []))
                board.assert_not_called()

    def test_build_only_never_opens_the_board(self):
        code, calls, board = self.run_main(REPO, "--build-only")
        self.assertEqual((code, calls), (0, [("build", REPO)]))
        board.assert_not_called()

    def test_build_only_refuses_a_folder_that_is_no_build_project(self):
        with tempfile.TemporaryDirectory() as backup:
            os.mkdir(os.path.join(backup, "src"))
            files = {"src/10-a.js": push.SPDX_LINE + b"const a=1;\n",
                     "assets.py": b"ASSET_VERSION = '1'\n",
                     "app.js": b"backup app\n", "i18n.js": b"backup i18n\n"}
            for rel, data in files.items():
                with open(os.path.join(backup, rel), "wb") as fh:
                    fh.write(data)
            before = sorted(os.listdir(backup))
            stderr = io.StringIO()
            with mock.patch.object(sys, "argv", ["push.py", backup, "--build-only"]), \
                    mock.patch.object(push, "with_retry") as board, \
                    mock.patch("sys.stdout"), mock.patch("sys.stderr", stderr):
                with self.assertRaises(SystemExit) as caught:
                    push.main()
            self.assertEqual(caught.exception.code, 2)
            self.assertIn("Not a build project", stderr.getvalue())
            board.assert_not_called()
            self.assertEqual(sorted(os.listdir(backup)), before)
            with open(os.path.join(backup, "app.js"), "rb") as fh:
                self.assertEqual(fh.read(), b"backup app\n")

    def test_build_only_defaults_to_the_repository(self):
        _code, calls, board = self.run_main("--build-only")
        self.assertEqual(calls, [("build", os.path.dirname(os.path.abspath(push.__file__)))])
        board.assert_not_called()

    def test_a_build_error_in_build_only_is_reported(self):
        def fail(_root):
            raise push.BuildError("boom")
        code, _calls, board = self.run_main(".", "--build-only", build_effect=fail)
        self.assertIn("Build failed: boom", str(code))
        board.assert_not_called()

    def test_build_only_needs_an_existing_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            for argv in ((os.path.join(tmp, "missing"), "--build-only"),
                         (os.path.join(tmp, "missing"), "--build-only", "--check"),
                         (os.path.join(REPO, "web.py"), "--build-only", "--check")):
                with self.subTest(argv=argv):
                    code, calls, board = self.run_main(*argv)
                    self.assertEqual((code, calls), (2, []))
                    board.assert_not_called()

    def test_a_missing_source_is_rejected_before_any_board_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, calls, board = self.run_main(os.path.join(tmp, "missing"),
                                               os.path.join(tmp, "no-such-port"))
        self.assertEqual((code, calls), (2, []))
        board.assert_not_called()

    def test_check_builds_nothing_and_reports(self):
        code, calls, board = self.run_main(".", "--build-only", "--check")
        self.assertEqual((code, calls), (0, [("check", ".")]))
        board.assert_not_called()
        code, _calls, board = self.run_main(".", "--build-only", "--check",
                                            check_result=["app.js.gz: stale"])
        self.assertEqual(code, 1)
        board.assert_not_called()

    def test_push_builds_before_the_board(self):
        code, calls, _board = self.run_main(REPO)
        self.assertEqual((code, calls), (0, [("build", REPO), ("board",)]))

    def test_a_backup_is_pushed_byte_for_byte_and_never_written(self):
        # Backups have a SHA-256 list next to them; a restore transfers exactly those bytes.
        with tempfile.TemporaryDirectory() as backup:
            files = {"app.js": b"const backup=1;\n" * 50,
                     "app.js.gz": gzip.compress(b"old\n", mtime=0)}
            for rel, data in files.items():
                path = os.path.join(backup, rel)
                with open(path, "wb") as fh:
                    fh.write(data)
                os.utime(path, (1000000, 1000000))
            written = {}

            class FakeRepl:
                current = None

                def ex(self, command):
                    if command == push.BOARD_HELFER:
                        return b""
                    call = ast.parse(command, mode="eval").body
                    name, arg = call.func.id, (call.args[0].value if call.args else None)
                    if name == "_ow":
                        self.current = arg
                        written[arg] = b""
                    elif name == "_wr":
                        written[self.current] += base64.b64decode(arg)
                    elif name == "_sha":
                        return push.hashlib.sha256(written.get(arg, b"")).hexdigest().encode()
                    return b""

            def board(operation, **_kwargs):
                return operation(FakeRepl())

            with mock.patch.object(sys, "argv", ["push.py", backup,
                                                 os.path.join(backup, "no-such-port")]), \
                    mock.patch.object(push, "with_retry", side_effect=board), \
                    mock.patch("sys.stdout"):
                with self.assertRaises(SystemExit) as caught:
                    push.main()
            self.assertEqual(caught.exception.code, 0)
            self.assertEqual(written, {"/" + rel: data for rel, data in files.items()})
            for rel, data in files.items():
                path = os.path.join(backup, rel)
                with open(path, "rb") as fh:
                    self.assertEqual(fh.read(), data)
                self.assertEqual(os.stat(path).st_mtime, 1000000)
            self.assertEqual(sorted(os.listdir(backup)), ["app.js", "app.js.gz"])

    def test_a_backup_with_a_stray_src_folder_is_not_built(self):
        with tempfile.TemporaryDirectory() as backup:
            os.mkdir(os.path.join(backup, "src"))
            files = {"src/10-a.js": push.SPDX_LINE + b"const a=1;\n",
                     "assets.py": b"ASSET_VERSION = '1'\n",
                     "app.js": b"backup app\n", "config.json": b"{}\n"}
            for rel, data in files.items():
                with open(os.path.join(backup, rel), "wb") as fh:
                    fh.write(data)
            pushed = []

            def board(operation, **_kwargs):
                pushed.extend(dp for _lp, dp in push.targets(backup))
                return True

            with mock.patch.object(sys, "argv", ["push.py", backup,
                                                 os.path.join(backup, "no-such-port")]), \
                    mock.patch.object(push, "with_retry", side_effect=board), \
                    mock.patch.object(push, "build_app_js", wraps=push.build_app_js) as build, \
                    mock.patch("sys.stdout"):
                with self.assertRaises(SystemExit) as caught:
                    push.main()
            self.assertEqual(caught.exception.code, 0)
            build.assert_not_called()
            with open(os.path.join(backup, "app.js"), "rb") as fh:
                self.assertEqual(fh.read(), b"backup app\n")
            self.assertIn("/config.json", pushed)
            self.assertIn("/src/10-a.js", pushed)

    def test_the_board_call_keeps_port_name_and_restart(self):
        # rpush (device protocol H11) replaces push.with_retry and relies on these keywords.
        with tempfile.TemporaryDirectory() as tmp:
            port = os.path.join(tmp, "no-such-port")
            _code, _calls, board = self.run_main(".", port, "--keep", "--skip-unchanged")
        self.assertEqual(board.call_args.kwargs,
                         {"port": port, "name": "push", "restart": False})

    def test_the_board_operation_builds_nothing(self):
        class FakeRepl:
            def __init__(self):
                self.commands = []

            def ex(self, command):
                self.commands.append(command)
                if command.startswith("_sha("):
                    # empty board until the file is closed, then the expected hash
                    written = "_cl()" in self.commands
                    return ((expected if written else "") + "\n").encode()
                return b""

        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "main.py"), "wb") as fh:
                fh.write(b"print(1)\n")
            expected = push.hashlib.sha256(b"print(1)\n").hexdigest()
            repl = FakeRepl()
            with mock.patch.object(push, "build_assets") as build_assets, \
                    mock.patch.object(push, "build_app_js") as build_app_js, \
                    mock.patch.object(push, "build_precompressed_assets") as build_gz, \
                    mock.patch("sys.stdout"):
                self.assertTrue(push.push(repl, tmp, False))
        build_assets.assert_not_called()
        build_app_js.assert_not_called()
        build_gz.assert_not_called()
        self.assertIn("_ow('/main.py')", repl.commands)

    def test_a_single_file_push_builds_its_directory(self):
        _code, calls, _board = self.run_main(os.path.join(REPO, "web.py"))
        self.assertEqual(calls[0], ("build", REPO))

    def test_a_build_error_stops_before_the_board(self):
        def fail(_root):
            raise push.BuildError("boom")
        code, _calls, board = self.run_main(REPO, build_effect=fail)
        self.assertIn("boom", str(code))
        board.assert_not_called()

    def test_a_build_error_in_the_check_is_reported(self):
        with mock.patch.object(push, "check_build", side_effect=push.BuildError("bad src")), \
                mock.patch.object(sys, "argv", ["push.py", ".", "--build-only", "--check"]), \
                mock.patch.object(push, "with_retry") as board:
            with self.assertRaises(SystemExit) as caught:
                push.main()
        self.assertIn("bad src", str(caught.exception.code))
        board.assert_not_called()

    def test_build_only_and_check_end_to_end(self):
        with tempfile.TemporaryDirectory() as root:
            os.mkdir(os.path.join(root, "src"))
            with open(os.path.join(root, "src", "10-a.js"), "wb") as fh:
                fh.write(push.SPDX_LINE + b"const a=1;\n")
            # A build project needs src/, assets.py and push.py.
            for marker in ("assets.py", "push.py"):
                with open(os.path.join(root, marker), "wb") as fh:
                    fh.write(b"")
            # The unused port argument is a safety net: should --build-only ever
            # reach the board again, it fails on this path instead of a real device.
            command = [sys.executable, os.path.join(REPO, "push.py"), root,
                       os.path.join(root, "no-such-port"), "--build-only"]
            built = subprocess.run(command, capture_output=True, text=True, timeout=60)
            self.assertEqual(built.returncode, 0, built.stderr)
            expected = push.APP_JS_HEADER + b"const a=1;\n" + push.APP_JS_FOOTER
            with open(os.path.join(root, "app.js"), "rb") as fh:
                self.assertEqual(fh.read(), expected)
            packed = os.path.join(root, "app.js.gz")
            with open(packed, "rb") as fh:
                self.assertEqual(gzip.decompress(fh.read()), expected)

            stale = gzip.compress(b"old\n")
            with open(packed, "wb") as fh:
                fh.write(stale)
            os.utime(packed, (1000000, 1000000))
            checked = subprocess.run(command + ["--check"], capture_output=True, text=True,
                                     timeout=60)
            self.assertEqual(checked.returncode, 1)
            self.assertIn("app.js.gz", checked.stdout + checked.stderr)
            with open(packed, "rb") as fh:
                self.assertEqual(fh.read(), stale)
            self.assertEqual(os.stat(packed).st_mtime, 1000000)


class FakeRepl:
    """A board without a serial port: files maps board paths to bytes.

    dirs holds the folders _md created. fail_on(command, repl) is asked before
    every command; True raises OSError before the command has any effect, like
    a pulled cable.
    """

    def __init__(self, files=None, corrupt=None, unreadable=(), dirs=(), rm_error=None,
                 fail_on=None):
        self.files = dict(files or {})
        self.corrupt = corrupt
        # Paths whose content the board cannot read (until written again).
        self.unreadable = set(unreadable)
        self.dirs = set(dirs)
        self.rm_error = rm_error
        self.fail_on = fail_on
        self.writes = []
        self.commands = []
        self.current = None
        self.buffer = b""

    def ex(self, command):
        if self.fail_on is not None and self.fail_on(command, self):
            raise OSError(5, "EIO")
        self.commands.append(command)
        if command == push.BOARD_HELFER:
            return b""
        call = ast.parse(command, mode="eval").body
        name = call.func.id
        args = [ast.literal_eval(arg) for arg in call.args]
        if name == "_sha":
            if args[0] in self.unreadable:
                return b"unreadable\n"
            data = self.files.get(args[0])
            digest = "" if data is None else push.hashlib.sha256(data).hexdigest()
            return (digest + "\n").encode()
        if name == "_cat":
            # hexlified like _sha, so no board byte can break the raw REPL framing
            return base64.b16encode(self.files.get(args[0], b"")).lower() + b"\n"
        if name == "_md":
            self.dirs.add(args[0])
            return b""
        if name == "_rm":
            if self.rm_error is not None:
                return ("error %s\n" % self.rm_error).encode()
            self.files.pop(args[0], None)
            return b"removed\n"
        if name == "_ow":
            # open(p, "wb") on the board truncates the file at once
            self.current, self.buffer = args[0], b""
            self.files[args[0]] = b""
        elif name == "_wr":
            self.buffer += base64.b64decode(args[0])
        elif name == "_cl":
            data = self.buffer + (b"x" if self.current == self.corrupt else b"")
            self.files[self.current] = data
            self.unreadable.discard(self.current)
            self.writes.append(self.current)
        else:
            raise AssertionError("unexpected board command: %s" % command)
        return b""


class TestBoardHelper(unittest.TestCase):
    """BOARD_HELFER runs on the board; here under CPython with ubinascii = binascii."""

    def helper(self, fail_after=None):
        """The helper's functions; with fail_after, the n-th read of a file raises EIO."""
        import binascii

        class Failing:
            def __init__(self, handle):
                self.handle = handle
                self.reads = 0

            def read(self, *args):
                self.reads += 1
                if self.reads > fail_after:
                    raise OSError(5, "EIO")
                return self.handle.read(*args)

            def close(self):
                self.handle.close()

        def board_open(path, mode="r"):
            handle = open(path, mode)
            return handle if fail_after is None else Failing(handle)

        namespace = {"open": board_open}
        with mock.patch.dict(sys.modules, {"ubinascii": binascii}):
            exec(push.BOARD_HELFER, namespace)
        return namespace

    def output(self, function, path):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            function(path)
        return out.getvalue().strip()

    def test_sha_reports_a_missing_an_intact_and_an_unreadable_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.bin")
            data = os.urandom(1500)
            with open(path, "wb") as fh:
                fh.write(data)
            sha = self.helper()["_sha"]
            self.assertEqual(self.output(sha, os.path.join(tmp, "missing")), "")
            self.assertEqual(self.output(sha, path), push.hashlib.sha256(data).hexdigest())
            for fail_after in (0, 1):
                with self.subTest(fail_after=fail_after):
                    sha = self.helper(fail_after)["_sha"]
                    self.assertEqual(self.output(sha, path), "unreadable")

    def test_cat_reports_an_unreadable_file_as_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "assets.py")
            with open(path, "wb") as fh:
                fh.write(b"ASSET_VERSION = '1'\n")
            self.assertEqual(self.output(self.helper()["_cat"], path),
                             b"ASSET_VERSION = '1'\n".hex())
            self.assertEqual(self.output(self.helper(0)["_cat"], path), "")

    def test_rm_removes_a_file_and_reports_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.py")
            with open(path, "wb") as fh:
                fh.write(b"old\n")
            rm = self.helper()["_rm"]
            self.assertEqual(self.output(rm, path), "removed")
            self.assertFalse(os.path.exists(path))
            self.assertTrue(self.output(rm, path).startswith("error"))


def ver(v, q='"'):
    return ("ASSET_VERSION = %s%s%s\n" % (q, v, q)).encode()


BUILT_APP_JS = push.APP_JS_HEADER + b"a;\n" + push.APP_JS_FOOTER


class PushTreeMixin:
    def tree(self, files, src=True):
        """A temporary source folder; src=True makes it a consistent build project."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        content = {}
        if src:
            content = {"src/10-a.js": push.SPDX_LINE + b"a;\n", "app.js": BUILT_APP_JS,
                       "push.py": b"# checkout marker\n"}
        content.update(files)
        for rel, data in content.items():
            path = os.path.join(tmp.name, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                fh.write(data)
        return tmp.name

    def tracking_tree(self, missing=None):
        """A build project with the complete tracking package of BOARD_FILES, without `missing`."""
        return self.tree({rel: data for rel, data in TRACKING_TREE.items() if rel != missing})

    def run_push(self, repl, src, allow=False):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = push.push(repl, src, False, allow)
        return result, out.getvalue()


class TestAssetVersionGuard(PushTreeMixin, unittest.TestCase):
    def forgotten_bump(self, board_quote='"'):
        tmp = self.tree({"assets.py": ver("1")})
        repl = FakeRepl({"/assets.py": ver("1", board_quote), "/app.js": b"old\n"})
        return tmp, repl

    def test_asset_version_of(self):
        self.assertEqual(push.asset_version_of('ASSET_VERSION = "15.10.2"\n'), "15.10.2")
        self.assertEqual(push.asset_version_of("ASSET_VERSION = '15.10.2'\n"), "15.10.2")
        for broken in ("", "ASSET_VERSION = 15\n", "ASSET_VERSION = ''\n",
                       "ASSET_VERSION = (\n", "VERSION = '1'\n"):
            self.assertIsNone(push.asset_version_of(broken), broken)

    def test_rules(self):
        P = push.asset_version_problem
        self.assertIsNone(P([], "1", "1"))
        self.assertIn("--allow-stale-asset-version", P(["/app.js"], "1", "1"))  # forgotten bump
        self.assertIsNone(P(["/app.js"], "2", "1"))                             # bumped
        self.assertIsNone(P(["/app.js"], "1", None))                            # empty board / old device
        self.assertIn("ASSET_VERSION", P(["/app.js"], None, "1"))                # unreadable local version

    def test_versioned_assets_are_the_uncompressed_served_files(self):
        # Read web.STATIC_ASSETS from the source: importing web needs the board stubs
        # of tests/support.py and logs a missing config.json.
        with open(os.path.join(REPO, "web.py"), encoding="utf-8") as fh:
            module = ast.parse(fh.read())
        static_assets = next(ast.literal_eval(node.value) for node in module.body
                             if isinstance(node, ast.Assign)
                             and getattr(node.targets[0], "id", None) == "STATIC_ASSETS")
        self.assertEqual(set(push.VERSIONED_ASSETS),
                         {name for name, _t in static_assets.values()})

    def test_a_forgotten_bump_aborts_before_writing(self):
        tmp, repl = self.forgotten_bump()
        result, out = self.run_push(repl, tmp)
        self.assertFalse(result)
        self.assertEqual(repl.writes, [])
        self.assertIn("ABORTED", out)
        self.assertIn("ASSET_VERSION", out)
        self.assertIn("/app.js", out)
        self.assertEqual(repl.files["/app.js"], b"old\n")

    def test_single_quotes_on_the_board_are_read(self):
        tmp, repl = self.forgotten_bump(board_quote="'")
        result, out = self.run_push(repl, tmp)
        self.assertFalse(result)
        self.assertEqual(repl.writes, [])
        self.assertIn("ABORTED", out)

    def test_an_unreadable_local_version_aborts(self):
        tmp = self.tree({"assets.py": b"ASSET_VERSION = (\n"})
        repl = FakeRepl({"/assets.py": ver("1"), "/app.js": b"old\n"})
        result, out = self.run_push(repl, tmp)
        self.assertFalse(result)
        self.assertEqual(repl.writes, [])
        self.assertIn("cannot read ASSET_VERSION", out)

    def test_the_board_version_is_read_only_when_a_versioned_asset_changed(self):
        tmp = self.tree({"assets.py": ver("1"), "web.py": b"web = 2\n"})
        repl = FakeRepl({"/assets.py": ver("1"), "/app.js": BUILT_APP_JS, "/web.py": b"web = 1\n"})
        self.assertTrue(self.run_push(repl, tmp)[0])
        self.assertFalse([c for c in repl.commands if c.startswith("_cat(")])

        tmp = self.tree({"assets.py": b"ASSET_VERSION = (\n"})
        repl = FakeRepl({"/assets.py": ver("1"), "/app.js": b"old\n"})
        self.assertFalse(self.run_push(repl, tmp)[0])
        self.assertFalse([c for c in repl.commands if c.startswith("_cat(")])

    def test_a_board_assets_py_that_is_no_text_does_not_block(self):
        for board_assets in (b"\xff\xfe\x04ASSET_VERSION = '1'\n", ver("1") + b"\x04\x80"):
            with self.subTest(board_assets=board_assets):
                tmp = self.tree({"assets.py": ver("1")})
                repl = FakeRepl({"/assets.py": board_assets, "/app.js": b"old\n"})
                result, out = self.run_push(repl, tmp)
                self.assertTrue(result, out)
                self.assertEqual(repl.writes[-1], "/assets.py")
                self.assertNotIn("ABORTED", out)

    def test_a_bump_is_allowed_and_assets_py_goes_last(self):
        tmp = self.tree({"assets.py": ver("2"), "web.py": b"web = 2\n"})
        repl = FakeRepl({"/assets.py": ver("1"), "/app.js": b"old\n"})
        result, out = self.run_push(repl, tmp)
        self.assertTrue(result, out)
        self.assertEqual(sorted(repl.writes), ["/app.js", "/assets.py", "/web.py"])
        self.assertEqual(repl.writes[-1], "/assets.py")
        self.assertEqual(repl.files["/assets.py"], ver("2"))
        self.assertEqual(repl.files["/app.js"], BUILT_APP_JS)
        self.assertNotIn("ABORTED", out)

    def test_the_override_allows_a_same_version_push(self):
        tmp, repl = self.forgotten_bump()
        result, out = self.run_push(repl, tmp, allow=True)
        self.assertTrue(result, out)
        self.assertIn("/app.js", repl.writes)
        self.assertEqual(repl.files["/app.js"], BUILT_APP_JS)
        self.assertIn("WARNING", out)
        self.assertNotIn("ABORTED", out)

    def test_compressed_copies_never_count(self):
        packed = gzip.compress(BUILT_APP_JS, compresslevel=9, mtime=0)
        tmp = self.tree({"assets.py": ver("1"), "app.js.gz": packed, "web.py": b"web = 2\n"})
        repl = FakeRepl({"/assets.py": ver("1"), "/app.js": BUILT_APP_JS,
                         "/app.js.gz": gzip.compress(BUILT_APP_JS, compresslevel=1, mtime=0),
                         "/web.py": b"web = 1\n"})
        result, out = self.run_push(repl, tmp)
        self.assertTrue(result, out)
        self.assertNotIn("ABORTED", out)
        self.assertEqual(sorted(repl.writes), ["/app.js.gz", "/web.py"])

    def test_an_empty_board_is_allowed(self):
        tmp = self.tree({"assets.py": ver("1"), "web.py": b"web = 1\n"})
        repl = FakeRepl({})
        result, out = self.run_push(repl, tmp)
        self.assertTrue(result, out)
        self.assertEqual(repl.writes[-1], "/assets.py")
        self.assertEqual(sorted(repl.writes), ["/app.js", "/assets.py", "/web.py"])

    def test_identical_files_are_skipped_without_the_flag(self):
        tmp = self.tree({"assets.py": ver("1"), "web.py": b"web = 2\n"})
        repl = FakeRepl({"/assets.py": ver("1"), "/app.js": BUILT_APP_JS,
                         "/web.py": b"web = 1\n"})
        result, out = self.run_push(repl, tmp)
        self.assertTrue(result, out)
        self.assertEqual(repl.writes, ["/web.py"])
        self.assertIn("unchanged", out)

    def test_a_failed_hash_check_holds_assets_py_back(self):
        tmp = self.tree({"assets.py": ver("2")})
        repl = FakeRepl({"/assets.py": ver("1"), "/app.js": b"old\n"}, corrupt="/app.js")
        result, out = self.run_push(repl, tmp)
        self.assertFalse(result)
        self.assertEqual(repl.files["/assets.py"], ver("1"))
        self.assertNotIn("/assets.py", repl.writes)
        self.assertIn("held back", out)

    def test_an_unreadable_board_file_is_overwritten(self):
        tmp = self.tree({"assets.py": ver("2")})
        repl = FakeRepl({"/assets.py": ver("1"), "/app.js": b"old\n"},
                        unreadable={"/app.js"})
        result, out = self.run_push(repl, tmp)
        self.assertTrue(result, out)
        self.assertIn("/app.js", repl.writes)
        self.assertEqual(repl.files["/app.js"], BUILT_APP_JS)
        self.assertIn("unreadable", out)

    def test_a_failed_push_resumes_without_the_flag(self):
        tmp = self.tree({"assets.py": ver("2")})
        repl = FakeRepl({"/assets.py": ver("1"), "/app.js": b"old\n"}, corrupt="/app.js")
        self.assertFalse(self.run_push(repl, tmp)[0])
        resumed = FakeRepl(repl.files)
        result, out = self.run_push(resumed, tmp)
        self.assertTrue(result, out)
        self.assertEqual(resumed.writes, ["/app.js", "/assets.py"])
        self.assertEqual(resumed.files["/app.js"], BUILT_APP_JS)
        self.assertEqual(resumed.files["/assets.py"], ver("2"))

    def test_a_garbled_board_assets_py_is_allowed(self):
        for board_assets in (b"\x00\x01garbage", b'ASSET_VERSION = "1', b"ASSET_VERSION = (\n"):
            with self.subTest(board_assets=board_assets):
                tmp = self.tree({"assets.py": ver("1")})
                repl = FakeRepl({"/assets.py": board_assets, "/app.js": b"old\n"})
                result, out = self.run_push(repl, tmp)
                self.assertTrue(result, out)
                self.assertEqual(repl.writes, ["/app.js", "/assets.py"])
                self.assertEqual(repl.files["/assets.py"], ver("1"))
                self.assertNotIn("ABORTED", out)

    def test_an_earlier_write_error_fails_with_an_unchanged_assets_py(self):
        tmp = self.tree({"assets.py": ver("1"), "web.py": b"web = 2\n"})
        repl = FakeRepl({"/assets.py": ver("1"), "/app.js": BUILT_APP_JS, "/web.py": b"web = 1\n"},
                        corrupt="/web.py")
        result, out = self.run_push(repl, tmp)
        self.assertFalse(result)
        self.assertEqual(repl.writes, ["/web.py"])
        self.assertIn("ERROR", out)
        self.assertNotIn("held back", out)

    def test_single_file_and_backup_tree_pushes_are_not_guarded(self):
        tmp, repl = self.forgotten_bump()
        result, out = self.run_push(repl, os.path.join(tmp, "app.js"))
        self.assertTrue(result, out)
        self.assertEqual(repl.writes, ["/app.js"])
        self.assertIn("NOTE", out)
        self.assertNotIn("ABORTED", out)

        backup = self.tree({"assets.py": ver("1"), "app.js": b"backup app\n"}, src=False)
        repl = FakeRepl({"/assets.py": ver("1"), "/app.js": b"old\n"})
        result, out = self.run_push(repl, backup)
        self.assertTrue(result, out)
        self.assertEqual(repl.writes, ["/app.js"])
        self.assertIn("NOTE", out)
        self.assertNotIn("ABORTED", out)

    def test_a_rollback_tree_without_src_goes_through(self):
        old = {"assets.py": ver("15.10.1"), "app.js": b"app 15.10.1\n",
               "app.js.gz": gzip.compress(b"app 15.10.1\n", mtime=0),
               "web.py": b"web = '15.10.1'\n"}
        rollback = self.tree(old, src=False)
        repl = FakeRepl({"/assets.py": ver("15.10.2"), "/app.js": b"app 15.10.2\n",
                         "/app.js.gz": gzip.compress(b"app 15.10.2\n", mtime=0),
                         "/web.py": b"web = '15.10.2'\n"})
        result, out = self.run_push(repl, rollback)
        self.assertTrue(result, out)
        self.assertEqual(repl.files, {"/" + rel: data for rel, data in old.items()})
        self.assertEqual(repl.writes[-1], "/assets.py")
        self.assertNotIn("ABORTED", out)

    def test_the_override_flag_reaches_push(self):
        with mock.patch.object(sys, "argv", ["push.py", ".", "--allow-stale-asset-version"]), \
                mock.patch.object(push, "build_assets"), \
                mock.patch.object(push, "push", return_value=True) as fn, \
                mock.patch.object(push, "with_retry", side_effect=lambda op, **kw: op("REPL")), \
                mock.patch("sys.stdout"):
            with self.assertRaises(SystemExit) as exit_:
                push.main()
        self.assertEqual(exit_.exception.code, 0)
        fn.assert_called_once_with("REPL", ".", False, True)


# A build project with the complete state package; state/link.py needs three
# chunks, so a transfer can break off in the middle of it.
PACKAGE_TREE = {
    "assets.py": ver("1"),
    "state/receiver.py": b"RECEIVER = 1\n",
    "state/link.py": b"LINK = 1\n" * (2 * push.CHUNK // 9 + 10),
    "state/system.py": b"SYSTEM = 1\n",
    "state/storage.py": b"STORAGE = 1\n",
    "state/__init__.py": b"from state.system import _instances\n",
}
PACKAGE_FILES = {"/" + rel: data for rel, data in PACKAGE_TREE.items() if rel.startswith("state/")}
# A board before #20b; app.js equals the build, so the ASSET_VERSION guard stays quiet.
OLD_BOARD = {"/state.py": b"old\n", "/assets.py": ver("1"), "/app.js": BUILT_APP_JS}

# A build project with the 14 files of the tracking package, each with its own
# bytes; tracking/restore.py needs three chunks, so a transfer can break off in
# the middle of it.
TRACKING_RELS = tuple(name for name in push.BOARD_FILES if name.startswith("tracking/"))
TRACKING_TREE = dict([("assets.py", ver("1"))]
                     + [(rel, ("# %s\n" % rel).encode()) for rel in TRACKING_RELS])
TRACKING_TREE["tracking/restore.py"] = b"# tracking/restore.py\n" * (2 * push.CHUNK // 22 + 10)
TRACKING_FILES = {"/" + rel: TRACKING_TREE[rel] for rel in TRACKING_RELS}
# A board before #26c.
TRACKING_BOARD = {"/tracking.py": b"old tracking\n", "/assets.py": ver("1"),
                  "/app.js": BUILT_APP_JS}


def state_import_on_board(repl, expected):
    """What `import state` finds on the board, as MicroPython 1.28 resolves it.

    A folder /state wins over /state.py (also without __init__.py): "package"
    if every file of expected (board path -> bytes) is there with these bytes,
    else "broken"; without the folder "file" for /state.py, else "missing".
    """
    if "/state" in repl.dirs or any(path.startswith("/state/") for path in repl.files):
        intact = all(repl.files.get(path) == data for path, data in expected.items())
        return "package" if intact else "broken"
    return "file" if "/state.py" in repl.files else "missing"


class TestRetiredBoardFiles(PushTreeMixin, unittest.TestCase):
    def package_push(self, tree=PACKAGE_TREE, board=OLD_BOARD, **options):
        src = self.tree(tree)
        repl = FakeRepl(board, **options)
        result, out = self.run_push(repl, src)
        return repl, result, out

    def test_push_removes_the_retired_state_module_after_every_file_checked_out(self):
        repl, result, out = self.package_push()
        self.assertTrue(result, out)
        for path, data in PACKAGE_FILES.items():
            self.assertEqual(repl.files[path], data, path)
        package_writes = [path for path in repl.writes if path.startswith("/state/")]
        self.assertEqual(sorted(package_writes), sorted(PACKAGE_FILES))
        self.assertEqual(package_writes[-1], "/state/__init__.py")
        self.assertNotIn("/state.py", repl.files)
        self.assertIn("  xx /state.py removed (replaced by /state/__init__.py)", out)
        last_close = max(i for i, command in enumerate(repl.commands) if command == "_cl()")
        self.assertGreater(repl.commands.index("_rm('/state.py')"), last_close)

    def test_an_incomplete_package_fails_before_any_board_command(self):
        for rel in (rel for rel in PACKAGE_TREE if rel.startswith("state/")):
            with self.subTest(missing=rel):
                tree = {name: data for name, data in PACKAGE_TREE.items() if name != rel}
                repl, result, out = self.package_push(tree)
                self.assertFalse(result)
                self.assertEqual(repl.commands, [])
                self.assertEqual(repl.files, OLD_BOARD)
                self.assertIn("ABORTED: incomplete package state/ (missing %s); "
                              "nothing was written" % rel, out)

    def test_a_failed_package_file_stops_the_push(self):
        repl, result, out = self.package_push(corrupt="/state/link.py")
        self.assertFalse(result)
        opened = repl.commands.index("_ow('/state/link.py')")
        closed = repl.commands.index("_cl()", opened)
        later = repl.commands[closed + 1:]
        self.assertEqual([command for command in later if command.startswith("_ow(")], [])
        self.assertEqual([command for command in later if command.startswith("_rm(")], [])
        self.assertNotIn("_ow('/state/__init__.py')", repl.commands)
        self.assertIn("/state.py", repl.files)
        self.assertIn("  !! push stopped: /state/link.py failed; later files, "
                      "/state/__init__.py and /assets.py are held back", out)

    def test_retired_module_stays_without_its_replacement(self):
        backup = self.tree({"state.py": b"backup state\n", "assets.py": ver("1"),
                            "app.js": BUILT_APP_JS}, src=False)
        repl = FakeRepl(OLD_BOARD)
        result, out = self.run_push(repl, backup)
        self.assertTrue(result, out)
        self.assertFalse([command for command in repl.commands if command.startswith("_rm(")])
        self.assertEqual(repl.files["/state.py"], b"backup state\n")

    def test_an_absent_retired_module_is_skipped(self):
        board = {path: data for path, data in OLD_BOARD.items() if path != "/state.py"}
        repl, result, out = self.package_push(board=board)
        self.assertTrue(result, out)
        self.assertFalse([command for command in repl.commands if command.startswith("_rm(")])
        self.assertNotIn(" xx ", out)

    def test_a_failing_removal_fails_the_push(self):
        repl, result, out = self.package_push(rm_error="EACCES")
        self.assertFalse(result)
        self.assertIn("  !! /state.py could not be removed: EACCES", out)
        self.assertIn("/state.py", repl.files)

    def test_a_repeated_push_changes_nothing(self):
        first, result, out = self.package_push()
        self.assertTrue(result, out)
        src = self.tree(PACKAGE_TREE)
        repl = FakeRepl(first.files, dirs=first.dirs)
        result, out = self.run_push(repl, src)
        self.assertTrue(result, out)
        file_lines = [line for line in out.splitlines() if line.startswith("  ")]
        self.assertEqual(len(file_lines), len(push.targets(src)))
        for line in file_lines:
            self.assertTrue(line.startswith("  == ") and line.endswith("unchanged"), line)
        self.assertFalse([command for command in repl.commands
                          if command.startswith(("_ow(", "_rm("))])
        self.assertNotIn(" xx ", out)

    def tracking_push(self, missing=None, board=TRACKING_BOARD, **options):
        src = self.tracking_tree(missing)
        repl = FakeRepl(board, **options)
        result, out = self.run_push(repl, src)
        return repl, result, out

    def assert_tracking_checks_out(self, repl, src):
        """Every file of the tracking package is on the board with its local SHA-256."""
        for rel in TRACKING_RELS:
            with open(os.path.join(src, rel), "rb") as handle:
                local = push.hashlib.sha256(handle.read()).hexdigest()
            board = repl.files.get("/" + rel)
            self.assertIsNotNone(board, rel)
            self.assertEqual(push.hashlib.sha256(board).hexdigest(), local, rel)

    def test_push_removes_the_retired_tracking_module_after_every_file_checked_out(self):
        src = self.tracking_tree()
        repl = FakeRepl(TRACKING_BOARD)
        result, out = self.run_push(repl, src)
        self.assertTrue(result, out)
        self.assertEqual(len(TRACKING_RELS), 14)
        self.assert_tracking_checks_out(repl, src)
        self.assertNotIn("/tracking.py", sorted(repl.files))
        self.assertIn("  xx /tracking.py removed (replaced by /tracking/__init__.py)", out)
        last_close = max(i for i, command in enumerate(repl.commands) if command == "_cl()")
        self.assertGreater(repl.commands.index("_rm('/tracking.py')"), last_close)

        repl, result, out = self.tracking_push(corrupt="/tracking/core.py")
        self.assertFalse(result, out)
        self.assertEqual(repl.files["/tracking.py"], TRACKING_BOARD["/tracking.py"])
        self.assertFalse([command for command in repl.commands if command.startswith("_rm(")])

    def test_an_incomplete_tracking_package_writes_nothing(self):
        for rel in TRACKING_RELS:
            with self.subTest(missing=rel):
                repl, result, out = self.tracking_push(missing=rel)
                self.assertFalse(result)
                self.assertIn("ABORTED: incomplete package tracking/ (missing %s); "
                              "nothing was written" % rel, out)
                self.assertFalse([command for command in repl.commands
                                  if command.startswith(("_md(", "_ow(", "_wr(", "_rm("))])
                self.assertEqual(repl.files, TRACKING_BOARD)
                self.assertEqual(repl.dirs, set())

    def test_an_aborted_tracking_push_resumes_with_the_same_push(self):
        restore = "/tracking/restore.py"
        self.assertGreater(len(TRACKING_FILES[restore]), 2 * push.CHUNK)

        def right_after(previous):
            return lambda _command, repl: bool(repl.commands) and repl.commands[-1] == previous

        def second_restore_chunk(command, repl):
            return (command.startswith("_wr(") and repl.current == restore
                    and repl.commands[-1].startswith("_wr("))

        def removed_before_the_answer(command, repl):
            # the board removes the file, the cable breaks before its answer
            if command != "_rm('/tracking.py')":
                return False
            del repl.files["/tracking.py"]
            return True

        points = (
            ("folder created", {"fail_on": right_after("_md('/tracking')")}),
            ("partial transfer", {"fail_on": second_restore_chunk}),
            ("checksum error", {"corrupt": "/tracking/core.py"}),
            ("initializer opened", {"fail_on": right_after("_ow('/tracking/__init__.py')")}),
            ("removal cut before the board", {"fail_on": lambda command, _repl:
                                              command == "_rm('/tracking.py')"}),
            ("removal cut before the answer", {"fail_on": removed_before_the_answer}),
        )
        for point, options in points:
            with self.subTest(point=point):
                src = self.tracking_tree()
                first = FakeRepl(TRACKING_BOARD, **options)
                if "fail_on" in options:
                    with self.assertRaises(OSError):
                        self.run_push(first, src)
                else:
                    result, out = self.run_push(first, src)
                    self.assertFalse(result, out)
                removal = point.startswith("removal")
                self.assertFalse([command for command in first.commands
                                  if command.startswith("_rm(")])
                if not removal:
                    self.assertEqual(first.files["/tracking.py"], TRACKING_BOARD["/tracking.py"])
                if point == "folder created":
                    self.assertIn("/tracking", first.dirs)
                    self.assertFalse([path for path in first.files
                                      if path.startswith("/tracking/")])
                if point == "partial transfer":
                    self.assertLess(len(first.files[restore]), len(TRACKING_FILES[restore]))
                if point == "checksum error":
                    closed = first.commands.index(
                        "_cl()", first.commands.index("_ow('/tracking/core.py')"))
                    self.assertFalse([command for command in first.commands[closed + 1:]
                                      if command.startswith("_ow(")])
                    self.assertNotIn("/tracking/__init__.py", first.files)
                if point == "initializer opened":
                    self.assertEqual(first.files["/tracking/__init__.py"], b"")
                if removal:
                    self.assert_tracking_checks_out(first, src)

                repl = FakeRepl(first.files, dirs=first.dirs)
                result, out = self.run_push(repl, src)
                self.assertTrue(result, out)
                self.assert_tracking_checks_out(repl, src)
                self.assertNotIn("/tracking.py", sorted(repl.files))
                self.assertNotIn("  !! ", out)
                removed = [i for i, command in enumerate(repl.commands)
                           if command == "_rm('/tracking.py')"]
                closes = [i for i, command in enumerate(repl.commands) if command == "_cl()"]
                if removed and closes:
                    self.assertGreater(removed[0], closes[-1])
                if not removal:
                    self.assertTrue(removed)
                    self.assertIn("  xx /tracking.py removed (replaced by /tracking/__init__.py)",
                                  out)
                    continue
                self.assertFalse([command for command in repl.commands
                                  if command.startswith(("_md(", "_ow(", "_wr(", "_cl("))])
                for path in TRACKING_FILES:
                    self.assertRegex(out, r"  == %s +\d+ bytes  unchanged" % path)
                if point == "removal cut before the board":
                    checks = max(i for i, command in enumerate(repl.commands)
                                 if command.startswith("_sha('/tracking/"))
                    self.assertGreater(removed[0], checks)
                    self.assertIn("  xx /tracking.py removed (replaced by /tracking/__init__.py)",
                                  out)
                else:
                    self.assertEqual(removed, [])
                    self.assertNotIn(" xx ", out)


class TestInterruptedPackagePush(PushTreeMixin, unittest.TestCase):
    """An upgrade broken off at any point is repaired by the same push (K1)."""

    def interrupted(self, fail_on):
        src = self.tree(PACKAGE_TREE)
        first = FakeRepl(OLD_BOARD, fail_on=fail_on)
        with self.assertRaises(OSError):
            self.run_push(first, src)
        return src, first

    def resume(self, src, first):
        repl = FakeRepl(first.files, dirs=first.dirs)
        result, out = self.run_push(repl, src)
        self.assertTrue(result, out)
        self.assertEqual(state_import_on_board(repl, PACKAGE_FILES), "package")
        self.assertNotIn("/state.py", repl.files)
        return repl, out

    def test_resume_after_the_folder_was_created(self):
        src, first = self.interrupted(lambda command, _repl: command == "_ow('/state/receiver.py')")
        self.assertIn("_md('/state')", first.commands)
        # K1: the folder shadows /state.py, so the board does not boot until the push is repeated
        self.assertEqual(state_import_on_board(first, PACKAGE_FILES), "broken")
        self.assertIn("/state.py", first.files)
        self.resume(src, first)

    def test_resume_after_a_partial_transfer(self):
        chunks = []

        def second_link_chunk(command, repl):
            if command.startswith("_wr(") and repl.current == "/state/link.py":
                chunks.append(command)
            return len(chunks) == 2

        src, first = self.interrupted(second_link_chunk)
        self.assertEqual(state_import_on_board(first, PACKAGE_FILES), "broken")
        self.assertLess(len(first.files["/state/link.py"]), len(PACKAGE_TREE["state/link.py"]))
        self.resume(src, first)

    def test_resume_after_a_checksum_error(self):
        src = self.tree(PACKAGE_TREE)
        first = FakeRepl(OLD_BOARD, corrupt="/state/link.py")
        result, out = self.run_push(first, src)
        self.assertFalse(result, out)
        self.assertEqual(state_import_on_board(first, PACKAGE_FILES), "broken")
        self.assertNotIn("/state/__init__.py", first.files)
        self.resume(src, first)

    def test_resume_after_the_initializer_was_written(self):
        src, first = self.interrupted(
            lambda command, repl: (command == "_sha('/state/__init__.py')"
                                   and repl.commands[-1] == "_cl()"))
        self.assertEqual(state_import_on_board(first, PACKAGE_FILES), "package")
        # dead flash until the next complete push removes it
        self.assertIn("/state.py", first.files)
        self.resume(src, first)

    def test_resume_after_an_interrupted_removal(self):
        src, first = self.interrupted(lambda command, _repl: command == "_rm('/state.py')")
        self.assertEqual(state_import_on_board(first, PACKAGE_FILES), "package")
        self.assertIn("/state.py", first.files)
        repl, out = self.resume(src, first)
        self.assertFalse([command for command in repl.commands
                          if command.startswith("_ow('/state/")])
        for path in PACKAGE_FILES:
            self.assertRegex(out, r"  == %s +\d+ bytes  unchanged" % path)
        self.assertIn("  xx /state.py removed (replaced by /state/__init__.py)", out)


if __name__ == "__main__":
    unittest.main()
