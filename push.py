#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Play a file or folder on the MicroPython board. Use: python3 push.py <file|folder> [port] [--keep] [--skip-unchanged] --keep board DO NOT restart, but leave it in the REPL. Attention: until the watchdog closes, the device is not reachable by any means - no Wi-Fi, no BLE, no AP. --skip-unchanged files jump over whose SHA-256 on the board is already correct. Without --keep, the board restarts at the end so that the application restarts immediately. Each written file is checked by SHA-256.
"""
import base64
import gzip
import hashlib
import os
import sys

from rawrepl import with_retry

# 1024 instead of the earlier 256 bytes per round: each round is a complete
# Raw-REPL-circulation. main.py needed about 190 round trips, now about 48.
CHUNK = 1024

# Only these files form the application on the board. Host tools, tests, backups,
# documentation and firmware binaries may not get into the flash when conveniently
# called ``push.py .``.
BOARD_FILES = (
    "boot.py", "access.py", "identity.py", "discovery.py", "cfg_validation.py",
    "cfg_boot.py", "cfg_factory.py", "cfg_store.py", "cfg_logging.py",
    "cfg.py", "state.py", "gnss.py", "net.py", "fanout.py", "esptel.py", "ble.py",
    "qr.js", "i18n.js", "base.css", "pages.css", "pages.js",
    "app.css", "app.css.gz", "app.js", "app.js.gz",
    "assets.py", "ui.py", "web_routing.py", "web.py", "main.py", "pointstore.py",
    "tracking.py", "parallel_benchmark.py", "field_diagnostics.py",
)


def build_precompressed_assets(src):
    """Keep immutable browser assets in sync without requiring a separate build."""
    if not os.path.isdir(src):
        return
    for name in ("app.js", "app.css"):
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

BOARD_HELFER = (
    "import os, ubinascii as _b, hashlib\n_f=None\n"
    "def _md(p):\n try:\n  os.mkdir(p)\n except OSError:\n  pass\n"
    "def _ow(p):\n global _f\n _f=open(p,'wb')\n"
    "def _wr(d):\n _f.write(_b.a2b_base64(d))\n"
    "def _cl():\n _f.close()\n"
    "def _sha(p):\n"
    " try:\n  f=open(p,'rb')\n except OSError:\n  print('')\n  return\n"
    " h=hashlib.sha256()\n"
    " while True:\n  b=f.read(512)\n  if not b:\n   break\n  h.update(b)\n"
    " f.close()\n print(_b.hexlify(h.digest()).decode())\n"
)


def targets(src):
    """(local path, board path) - src may be file or folder."""
    if os.path.isfile(src):
        return [(src, "/" + os.path.basename(src))]
    if os.path.abspath(src) == os.path.dirname(os.path.abspath(__file__)):
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


def push(repl, src, skip_unchanged):
    build_precompressed_assets(src)
    repl.ex(BOARD_HELFER)
    alles_gut = True
    for lp, dp in targets(src):
        blob = open(lp, "rb").read()
        lokal = hashlib.sha256(blob).hexdigest()

        if skip_unchanged:
            if repl.ex("_sha(%r)" % dp).decode().strip() == lokal:
                print("  == %-28s %7d bytes  unchanged" % (dp, len(blob)))
                continue

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
    return alles_gut


def main():
    if any(arg in ("-h", "--help") for arg in sys.argv[1:]):
        print(__doc__.strip())
        return
    opts = [a for a in sys.argv[1:] if a.startswith("--")]
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        raise SystemExit("Source is missing. For the application, run: python3 push.py .")
    src = args[0]
    port = args[1] if len(args) > 1 else None
    restart = "--keep" not in opts
    skip = "--skip-unchanged" in opts

    okay = with_retry(lambda r: push(r, src, skip), port=port, name="push",
                     restart=restart)

    print("Complete: %s -> board%s%s"
          % (src, "" if okay else "  (WITH ERRORS!)",
             "" if restart else "  (application stopped, --keep)"))
    sys.exit(0 if okay else 2)


if __name__ == "__main__":
    main()
