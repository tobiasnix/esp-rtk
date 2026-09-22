#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Drags all files of a MicroPython board over the Raw-REPL. Usage: python3 pull.py [port] [target] [--keep] (Default-target: backup/) Then restarts the board so that the application restarts immediately. --keep leaves it in the REPL - then it is not reachable until the watchdog.
"""
import base64
import os
import sys

from rawrepl import with_retry

BOARD_HELFER = (
    "import os, ubinascii as _b\n_f=None\n"
    "def _op(p):\n global _f\n _f=open(p,'rb')\n"
    "def _rd():\n return _b.b2a_base64(_f.read(16384)).decode()\n"
)

AUFLISTEN = (
    "def _w(p):\n"
    " for e in os.ilistdir(p if p else '/'):\n"
    "  n=e[0]; t=e[1]; f=(p+'/'+n) if p else n\n"
    "  if t & 0x4000:\n   _w(f)\n  else:\n   print(f,os.stat(f)[6],sep='\\t')\n"
    "_w('')\n"
)


def pull(repl, dest):
    repl.ex(BOARD_HELFER)
    listing = repl.ex(AUFLISTEN).decode()
    files = []
    for raw in listing.replace("\r", "").split("\n"):
        if not raw.strip():
            continue
        name, size = raw.rsplit("\t", 1)
        files.append((name, int(size)))
    print("%d files found" % len(files))

    for f, expected_size in files:
        lokal = os.path.join(dest, f)
        if os.path.isfile(lokal) and os.path.getsize(lokal) == expected_size:
            print("  %-30s %d bytes (already complete)" % (f, expected_size))
            continue
        repl.ex("_op(%r)" % f)
        data = b""
        while True:
            b64 = repl.ex("print(_rd(),end='')").decode().strip()
            if not b64:
                break
            data += base64.b64decode(b64)
        repl.ex("_f.close()")
        os.makedirs(os.path.dirname(lokal) or ".", exist_ok=True)
        with open(lokal, "wb") as fh:
            fh.write(data)
        print("  %-30s %d bytes" % (f, len(data)))
    return len(files)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    port = args[0] if args and args[0] else None
    dest = args[1] if len(args) > 1 else "backup"
    restart = "--keep" not in sys.argv
    with_retry(lambda r: pull(r, dest), port=port, versuche=6, name="pull",
               restart=restart, wdt_timeout_ms=600000)
    print("Complete -> %s/" % dest)


if __name__ == "__main__":
    main()
