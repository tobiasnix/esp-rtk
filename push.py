#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Play a file or folder on the MicroPython board.

Use: python3 push.py <file|folder> [port] [--keep] [--skip-unchanged] [--allow-stale-asset-version]
     python3 push.py [folder] --build-only [--check]

A build project is the repository itself or any checkout with src/, assets.py
and push.py. Before touching the board, push.py builds its app.js from src/
and the .gz copies of the compressed browser assets (for a single file: in
its folder); a build error aborts before any port is opened. A build project
is pushed through the BOARD_FILES allowlist. Other folders (backups, rollback
trees) and single files outside a build project are pushed byte for byte as
they are: push.py writes nothing into them.

--keep board DO NOT restart, but leave it in the REPL. Attention: until the
watchdog closes, the device is not reachable by any means - no Wi-Fi, no BLE,
no AP. Without --keep, the board restarts at the end so that the application
restarts immediately.
--skip-unchanged accepted for compatibility: files whose SHA-256 on the board is
already correct are always skipped.
--allow-stale-asset-version push changed browser assets although ASSET_VERSION is unchanged.
--build-only only build (default folder: the one of push.py), never open the board;
a folder that is no build project is refused with exit code 2 and left untouched.
--check with --build-only: write nothing, list stale build artefacts, exit 1 if any.
Unknown options abort with exit code 2. Each written file is checked by SHA-256.

When a build project is pushed as a folder, changed uncompressed browser assets
(VERSIONED_ASSETS) need a different ASSET_VERSION in assets.py than the board;
otherwise the push aborts before writing anything. /assets.py is always
written last, and only after every earlier file checked out.

A package of BOARD_FILES (a folder such as state/) goes to the board completely
or not at all: an allowlist source that lacks one of its files aborts before the
first board command. A failed checksum of a package file stops the push at once
and holds back the later files, the package's __init__.py and /assets.py; the
board may then not boot until the same push is repeated, which restores it.
After a complete push of a build project, each retired board file (the module a
package replaced, such as /state.py) whose successor checked out is removed. A
single file whose path relative to push.py is in BOARD_FILES keeps that path on
the board (state/link.py -> /state/link.py).
"""
import ast
import base64
import binascii
import gzip
import hashlib
import os
import re
import sys
import zlib

from rawrepl import with_retry

# 1024 instead of the earlier 256 bytes per round: each round is a complete
# Raw-REPL-circulation. main.py needed about 190 round trips, now about 48.
CHUNK = 1024

OPTIONS = ("--keep", "--skip-unchanged", "--build-only", "--check",
           "--allow-stale-asset-version")

# Only these files form the application on the board. Host tools, tests, backups,
# documentation and firmware binaries may not get into the flash when conveniently
# called ``push.py .``.
BOARD_FILES = (
    "boot.py", "access.py", "identity.py", "discovery.py", "cfg_validation.py",
    "cfg_boot.py", "cfg_factory.py", "cfg_store.py", "cfg_logging.py",
    "cfg.py", "state/receiver.py", "state/link.py", "state/system.py", "state/storage.py",
    "state/__init__.py", "gnss.py", "geo.py", "net.py", "fanout.py", "esptel.py", "ble.py",
    "qr.js", "i18n.js", "i18n.js.gz", "base.css", "pages.css", "pages.js",
    "app.css", "app.css.gz", "app.js", "app.js.gz",
    "assets.py", "ui.py", "web_routing.py", "events.py", "web.py", "main.py",
    "pointstore.py", "tracking/common.py", "tracking/guards.py", "tracking/frames.py",
    "tracking/validation.py", "tracking/fixes.py", "tracking/model.py", "tracking/journal.py",
    "tracking/archive.py", "tracking/restore.py", "tracking/summary.py", "tracking/exports.py",
    "tracking/recording.py", "tracking/core.py", "tracking/__init__.py",
    "measurement.py", "parallel_benchmark.py",
    "field_diagnostics.py", "stakeout.py", "stakeout_session.py", "stakeout_progress.py",
    "stakeout_export.py",
)
# Production packages: the folders of BOARD_FILES, each with its __init__.py
# listed last. A package goes to the board completely or not at all.
PACKAGES = tuple(sorted({name.split("/")[0] for name in BOARD_FILES if "/" in name}))
# (retired board file, its successor): once the successor checked out in a
# complete push of a build project, the retired file is removed from the board.
# MicroPython imports a folder state/ before state.py, so a leftover /state.py
# is dead flash, and a later rollback that writes it would be shadowed.
RETIRED_BOARD_FILES = (("/state.py", "/state/__init__.py"),
                       ("/tracking.py", "/tracking/__init__.py"))


SPDX_LINE = b"// SPDX-License-Identifier: AGPL-3.0-only\n"
APP_JS_HEADER = SPDX_LINE + b'(()=>{"use strict";\n'
APP_JS_FOOTER = b"})();\n"
APP_JS_SOURCE_DIR = "src"
SOURCE_NAME = re.compile(r"^([0-9]{2})-[a-z0-9-]+\.js$")
COMPRESSED_ASSETS = ("app.js", "app.css", "i18n.js")
# The uncompressed browser assets web.STATIC_ASSETS serves; the browser caches
# them under ASSET_VERSION, so a change needs a new version.
VERSIONED_ASSETS = ("base.css", "pages.css", "pages.js", "i18n.js", "app.css", "app.js", "qr.js")
TOP_LEVEL_DECLARATION = re.compile(
    rb"^(?:async[ \t]+)?function(?:[ \t]*\*[ \t]*|[ \t]+)([A-Za-z_$][\w$]*)"
    rb"|^class[ \t]+([A-Za-z_$][\w$]*)"
    rb"|^(?:const|let|var)[ \t]+([A-Za-z_$][\w$]*)", re.MULTILINE)


class BuildError(Exception):
    """src/ cannot be turned into app.js (bad layout, missing header, duplicate name)."""


def app_js_sources(root):
    """Absolute paths of root/src/NN-name.js ordered by file name; None without root/src.

    Anything else in src/ (sub folders, other names, a slot number used twice,
    an empty folder) is a BuildError; dotfiles such as editor swap files are ignored.
    """
    folder = os.path.join(root, APP_JS_SOURCE_DIR)
    if not os.path.isdir(folder):
        return None
    names = sorted(name for name in os.listdir(folder) if not name.startswith("."))
    slots = {}
    for name in names:
        match = SOURCE_NAME.match(name)
        if not match or not os.path.isfile(os.path.join(folder, name)):
            raise BuildError("src/%s: expected a file named NN-name.js (two-digit slot)" % name)
        if match.group(1) in slots:
            raise BuildError("src/: slot %s is used twice (%s, %s)"
                             % (match.group(1), slots[match.group(1)], name))
        slots[match.group(1)] = name
    if not names:
        raise BuildError("src/ contains no NN-name.js file")
    return [os.path.join(folder, name) for name in names]


def top_level_names(body):
    """Names declared at the start of a line (function, async function, class, const, let, var).

    Restricted contract: only the first name of a declaration that begins in
    column 0 is seen. A declaration behind another statement on the same line
    (``let x=null;function f(){}``) and indented or nested declarations are not
    seen; a column-0 line inside a multi-line string or template literal that
    looks like a declaration is a false alarm. There is no scope parser.
    """
    return [next(group for group in match.groups() if group).decode()
            for match in TOP_LEVEL_DECLARATION.finditer(body)]


def render_app_js(root):
    """The app.js that root/src/*.js produces; raises BuildError, writes nothing.

    Every fragment starts with the SPDX line, which is removed; header, fragments
    in file name order and footer form one IIFE. A fragment must end with a newline
    and no top-level name may be declared twice across the fragments.
    """
    sources = app_js_sources(root)
    if sources is None:
        raise BuildError("%s/ does not exist" % APP_JS_SOURCE_DIR)
    parts = []
    for path in sources:
        name = os.path.basename(path)
        with open(path, "rb") as handle:
            data = handle.read()
        if not data.startswith(SPDX_LINE):
            raise BuildError("src/%s: the first line must be the SPDX line" % name)
        rest = data[len(SPDX_LINE):]
        if rest and not rest.endswith(b"\n"):
            raise BuildError("src/%s: must end with a newline" % name)
        parts.append(rest)
    body = b"".join(parts)
    seen = set()
    duplicates = set()
    for name in top_level_names(body):
        (duplicates if name in seen else seen).add(name)
    if duplicates:
        raise BuildError("duplicate top-level name(s): " + ", ".join(sorted(duplicates)))
    return APP_JS_HEADER + body + APP_JS_FOOTER


def build_app_js(root):
    """Write root/app.js from root/src/*.js; True if the file changed.

    The fragments are joined in file name order (two-digit slot), so ``99-boot.js``
    comes last; slot 95 is reserved for the stakeout UI. Without src/ or with an
    equal app.js nothing is touched; otherwise app.js is replaced atomically.
    Never edit app.js directly: the next build overwrites it.
    """
    if not os.path.isdir(os.path.join(root, APP_JS_SOURCE_DIR)):
        return False
    rendered = render_app_js(root)
    target = os.path.join(root, "app.js")
    try:
        with open(target, "rb") as handle:
            if handle.read() == rendered:
                return False
    except OSError:
        pass
    temporary = target + ".tmp"
    try:
        with open(temporary, "wb") as handle:
            handle.write(rendered)
        os.replace(temporary, target)
    except BaseException:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise
    return True


def check_build(root):
    """Problems of the committed build artefacts as ``file: reason``; writes nothing.

    app.js must equal the build of src/ (only checked if src/ exists); each
    compressed copy must be a valid gzip whose content equals the plain file
    (for app.js: the build). Other gzip representations are fine. A BuildError
    from rendering is passed through.
    """
    rendered = render_app_js(root) if os.path.isdir(os.path.join(root, APP_JS_SOURCE_DIR)) else None
    problems = []
    for name in COMPRESSED_ASSETS:
        path = os.path.join(root, name)
        if not os.path.isfile(path):
            if name == "app.js" and rendered is not None:
                problems.append(name + ": missing")
            continue
        with open(path, "rb") as handle:
            expected = handle.read()
        if name == "app.js" and rendered is not None:
            if expected != rendered:
                problems.append(name + ": differs from the build of src/")
            expected = rendered
        try:
            with open(path + ".gz", "rb") as handle:
                packed = handle.read()
        except OSError:
            problems.append(name + ".gz: missing")
            continue
        try:
            if gzip.decompress(packed) != expected:
                problems.append(name + ".gz: content differs from " + name)
        except (OSError, EOFError, zlib.error):
            problems.append(name + ".gz: not a valid gzip file")
    return problems


def build_precompressed_assets(src):
    """Keep immutable browser assets in sync without requiring a separate build."""
    if not os.path.isdir(src):
        return
    for name in COMPRESSED_ASSETS:
        source = os.path.join(src, name)
        target = source + ".gz"
        if not os.path.isfile(source):
            continue
        with open(source, "rb") as handle:
            compressed = gzip.compress(handle.read(), compresslevel=9, mtime=0)
        try:
            with open(target, "rb") as handle:
                if handle.read() == compressed:
                    continue
        except OSError:
            pass
        with open(target, "wb") as handle:
            handle.write(compressed)


def build_assets(root):
    """Build app.js from src/ (if present), then refresh the compressed copies."""
    build_app_js(root)
    build_precompressed_assets(root)


BOARD_HELFER = (
    "import os, ubinascii as _b, hashlib\n_f=None\n"
    "def _md(p):\n try:\n  os.mkdir(p)\n except OSError:\n  pass\n"
    "def _ow(p):\n global _f\n _f=open(p,'wb')\n"
    "def _wr(d):\n _f.write(_b.a2b_base64(d))\n"
    "def _cl():\n _f.close()\n"
    "def _x(f):\n try:\n  f.close()\n except OSError:\n  pass\n"
    "def _sha(p):\n"
    " try:\n  f=open(p,'rb')\n except OSError:\n  print('')\n  return\n"
    " h=hashlib.sha256()\n"
    " try:\n  while True:\n   b=f.read(512)\n   if not b:\n    break\n   h.update(b)\n"
    " except OSError:\n  _x(f)\n  print('unreadable')\n  return\n"
    " _x(f)\n print(_b.hexlify(h.digest()).decode())\n"
    "def _cat(p):\n"
    " try:\n  f=open(p,'rb')\n except OSError:\n  print('')\n  return\n"
    " try:\n  d=f.read()\n except OSError:\n  d=b''\n"
    " _x(f)\n print(_b.hexlify(d).decode())\n"
    "def _rm(p):\n try:\n  os.remove(p)\n  print('removed')\n"
    " except OSError as e:\n  print('error',e)\n"
)
# What _sha prints for a board file that opens but cannot be read: it never
# equals a digest, so the file is overwritten (and checked again) instead of
# breaking off the push.
BOARD_UNREADABLE = "unreadable"


def asset_version_of(text):
    """The first module-level ASSET_VERSION = "<non-empty str>" in text, else None. Never executes it."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "ASSET_VERSION"):
            value = node.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str) and value.value:
                return value.value
            return None
    return None


def board_text(repl, path):
    """A board file as text via _cat (hexlified on the wire); '' if missing or garbled."""
    try:
        data = binascii.unhexlify(repl.ex("_cat(%r)" % path).strip())
    except (binascii.Error, ValueError):
        return ""
    return data.decode("utf-8", "replace")


def asset_version_problem(changed, local_version, board_version):
    """Why changed browser assets may not be pushed under these versions, or None."""
    if not changed:
        return None
    if local_version is None:
        return "cannot read ASSET_VERSION from assets.py"
    if board_version is None:
        return None
    if local_version == board_version:
        return ("browser assets changed (%s) but ASSET_VERSION %s is already on the board; "
                "raise ASSET_VERSION in assets.py, or pass --allow-stale-asset-version"
                % (", ".join(changed), local_version))
    return None


def source_root(src):
    """The folder whose assets are built before pushing src (a folder or a single file)."""
    if os.path.isdir(src):
        return src
    return os.path.dirname(src) or "."


def is_build_project(root):
    """True for a checkout like this repository: root/src is a folder, assets.py and push.py are files.

    push.py is not in BOARD_FILES, so a backup pulled from a board is never a
    build project, even if a stray /src folder landed on that board.
    """
    return (os.path.isdir(os.path.join(root, APP_JS_SOURCE_DIR))
            and os.path.isfile(os.path.join(root, "assets.py"))
            and os.path.isfile(os.path.join(root, "push.py")))


def uses_allowlist(src):
    """True if the folder src goes through BOARD_FILES: the folder of push.py or a build project."""
    return os.path.isdir(src) and (
        os.path.abspath(src) == os.path.dirname(os.path.abspath(__file__))
        or is_build_project(src))


def missing_package_files(src):
    """BOARD_FILES entries missing under src, for each package with at least one file there.

    Empty means every package is complete or absent as a whole (a backup tree
    with state.py instead of state/).
    """
    missing = []
    for package in PACKAGES:
        names = [name for name in BOARD_FILES if name.startswith(package + "/")]
        absent = [name for name in names if not os.path.isfile(os.path.join(src, name))]
        if len(absent) < len(names):
            missing.extend(absent)
    return missing


def package_of(dp):
    """The package of PACKAGES a board path lies in, else None."""
    parts = dp.lstrip("/").split("/")
    return parts[0] if len(parts) > 1 and parts[0] in PACKAGES else None


def targets(src):
    """(local path, board path) - src may be file or folder.

    The folder of push.py and any build project go through the BOARD_FILES
    allowlist; other folders are pushed as a whole tree. A single file keeps
    its path relative to push.py if that path is in BOARD_FILES (state/link.py
    -> /state/link.py), otherwise it lands in the root folder.
    """
    if os.path.isfile(src):
        rel = os.path.relpath(os.path.abspath(src), os.path.dirname(os.path.abspath(__file__)))
        rel = rel.replace(os.sep, "/")
        return [(src, "/" + (rel if rel in BOARD_FILES else os.path.basename(src)))]
    if uses_allowlist(src):
        return [(os.path.join(src, name), "/" + name)
                for name in BOARD_FILES if os.path.isfile(os.path.join(src, name))]
    out = []
    for root, dirs, fnames in os.walk(src):
        # Host metadata and python caches never belong on the board. in particular, the
        # documented call ``push.py .`` may not copy the Git repository into the narrow
        # flash.
        dirs[:] = [name for name in dirs
                   if not name.startswith(".") and name != "__pycache__"]
        rel = os.path.relpath(root, src)
        for name in sorted(fnames):
            if name.startswith(".") or name.endswith((".pyc", ".pyo")):
                continue
            dp = "/" + (name if rel == "."
                        else rel.replace(os.sep, "/") + "/" + name)
            out.append((os.path.join(root, name), dp))
    return sorted(out)


def dirs_of(dp):
    """All parenting directories of a board path, from the outside to the inside."""
    parts = dp.strip("/").split("/")[:-1]
    cur, out = "", []
    for p in parts:
        cur += "/" + p
        out.append(cur)
    return out


def push(repl, src, skip_unchanged, allow_stale_assets=False):
    """Write targets(src) to the board; True if every file checks out. Builds nothing.

    Files already correct on the board are skipped (skip_unchanged is kept for
    callers only). /assets.py goes last and only after every earlier file checked out.
    An allowlist source with an incomplete package aborts before any board
    command; a failed package file stops the push at once. After a complete
    push of a build project, RETIRED_BOARD_FILES whose successor checked out
    are removed.
    """
    if uses_allowlist(src):
        missing = missing_package_files(src)
        for package in PACKAGES:
            absent = [name for name in missing if name.startswith(package + "/")]
            if absent:
                print("ABORTED: incomplete package %s/ (missing %s); nothing was written"
                      % (package, ", ".join(absent)))
        if missing:
            return False
    repl.ex(BOARD_HELFER)
    pairs = targets(src)
    root = source_root(src)
    files = []
    for lp, dp in pairs:
        with open(lp, "rb") as handle:
            blob = handle.read()
        on_board = repl.ex("_sha(%r)" % dp).decode().strip()
        files.append((dp, blob, hashlib.sha256(blob).hexdigest(), on_board))

    if os.path.isdir(src) and is_build_project(root):
        changed = sorted(dp for dp, _blob, lokal, on_board in files
                         if dp.lstrip("/") in VERSIONED_ASSETS and lokal != on_board)
        with open(os.path.join(root, "assets.py"), encoding="utf-8", errors="replace") as handle:
            local_version = asset_version_of(handle.read())
        board_version = None
        if changed and local_version is not None:
            board_version = asset_version_of(board_text(repl, "/assets.py"))
        problem = asset_version_problem(changed, local_version, board_version)
        if problem and not allow_stale_assets:
            print("ABORTED: %s" % problem)
            return False
        if problem:
            print("WARNING: %s" % problem)
    else:
        print("NOTE: ASSET_VERSION guard applies only to pushes of a checkout with src/ and assets.py")

    files.sort(key=lambda entry: entry[0] == "/assets.py")
    alles_gut = True
    checked = set()
    for dp, blob, lokal, on_board in files:
        if on_board == lokal:
            print("  == %-28s %7d bytes  unchanged" % (dp, len(blob)))
            checked.add(dp)
            continue
        if dp == "/assets.py" and not alles_gut:
            print("  !! /assets.py held back: earlier files failed")
            return False
        if on_board == BOARD_UNREADABLE:
            print("  ?? %-28s unreadable on the board, overwriting" % dp)

        for d in dirs_of(dp):
            repl.ex("_md(%r)" % d)
        repl.ex("_ow(%r)" % dp)
        for i in range(0, len(blob), CHUNK):
            repl.ex("_wr(%r)" % base64.b64encode(blob[i:i + CHUNK]).decode())
        repl.ex("_cl()")

        entfernt = repl.ex("_sha(%r)" % dp).decode().strip()
        ok = entfernt == lokal
        alles_gut = alles_gut and ok
        print("  -> %-28s %7d bytes  %s" % (dp, len(blob), "OK " if ok else "ERROR"))
        if ok:
            checked.add(dp)
            continue
        package = package_of(dp)
        if package is not None:
            # A package with a wrong module would look importable: stop before
            # its __init__.py, the mark of a complete package, is written.
            init = "/%s/__init__.py" % package
            held = ("later files and /assets.py" if dp == init
                    else "later files, %s and /assets.py" % init)
            print("  !! push stopped: %s failed; %s are held back" % (dp, held))
            return False

    if alles_gut and os.path.isdir(src) and is_build_project(root):
        for retired, successor in RETIRED_BOARD_FILES:
            # checked: unchanged on the board or written and verified in this push
            if successor not in checked:
                continue
            if not repl.ex("_sha(%r)" % retired).decode().strip():
                continue
            answer = repl.ex("_rm(%r)" % retired).decode().strip()
            if answer == "removed":
                print("  xx %s removed (replaced by %s)" % (retired, successor))
            else:
                alles_gut = False
                text = answer[len("error"):].strip() if answer.startswith("error") else answer
                print("  !! %s could not be removed: %s" % (retired, text))
    return alles_gut


def main():
    if any(arg in ("-h", "--help") for arg in sys.argv[1:]):
        print(__doc__.strip())
        return
    opts = [a for a in sys.argv[1:] if a.startswith("--")]
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    for opt in opts:
        if opt not in OPTIONS:
            print("Unknown option: %s (see python3 push.py --help)" % opt, file=sys.stderr)
            sys.exit(2)
    if "--check" in opts and "--build-only" not in opts:
        print("--check needs --build-only", file=sys.stderr)
        sys.exit(2)
    if "--build-only" in opts:
        root = args[0] if args else os.path.dirname(os.path.abspath(__file__))
        if not os.path.isdir(root):
            print("Not a folder: %s" % root, file=sys.stderr)
            sys.exit(2)
        try:
            if "--check" in opts:
                problems = check_build(root)
                for problem in problems:
                    print(problem)
                if problems:
                    sys.exit(1)
                print("Build check passed: %s" % root)
            elif not is_build_project(root):
                print("Not a build project (needs src/, assets.py and push.py), nothing "
                      "built: %s" % root, file=sys.stderr)
                sys.exit(2)
            else:
                build_assets(root)
                print("Built: %s" % root)
        except BuildError as exc:
            sys.exit("Build failed: %s" % exc)
        return
    if not args:
        raise SystemExit("Source is missing. For the application, run: python3 push.py .")
    src = args[0]
    if not os.path.exists(src):
        print("Source not found: %s" % src, file=sys.stderr)
        sys.exit(2)
    port = args[1] if len(args) > 1 else None
    restart = "--keep" not in opts
    skip = "--skip-unchanged" in opts
    allow = "--allow-stale-asset-version" in opts
    root = source_root(src)
    # Only a build project is built. Backups, rollback trees and single files
    # outside a checkout are pushed exactly as they are: a backup is compared
    # against its SHA-256 list, and a restore must transfer the saved bytes.
    if is_build_project(root):
        try:
            build_assets(root)
        except BuildError as exc:
            sys.exit("Build failed: %s" % exc)

    okay = with_retry(lambda r: push(r, src, skip, allow), port=port, name="push",
                      restart=restart)

    print("Complete: %s -> board%s%s"
          % (src, "" if okay else "  (WITH ERRORS!)",
             "" if restart else "  (application stopped, --keep)"))
    sys.exit(0 if okay else 2)


if __name__ == "__main__":
    main()
