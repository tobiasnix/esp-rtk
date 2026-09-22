#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Display the GNSS module's NMEA stream directly in the terminal.

The tool pauses the application through Raw REPL and reads UART1. Prefer
``stream.py`` during normal operation; this direct path is intended for cases
where networking or the application itself is unavailable.
"""
import sys
import time

from rawrepl import RawRepl

UART_ID, TX, RX, BAUD = 1, 18, 17, 115200

MITLESER = (
    "from machine import UART\n"
    "u = UART(%d, %d, tx=%d, rx=%d, timeout=100)\n"
    "buf = b''\n"
    "while True:\n"
    "    if u.any():\n"
    "        buf += u.read()\n"
    "        while b'\\n' in buf:\n"
    "            line, buf = buf.split(b'\\n', 1)\n"
    "            print(line.decode('ascii', 'ignore').strip())\n"
)


def parse_args(argv):
    raw = "--raw" in argv
    filt = None
    rest = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--filter" and i + 1 < len(argv):
            # The value belongs to the option, not to the position arguments.
            filt = [x.strip().upper() for x in argv[i + 1].split(",")]
            i += 2
            continue
        if not a.startswith("--"):
            rest.append(a)
        i += 1
    return raw, filt, (rest[0] if rest else None)


def main():
    raw, filt, port = parse_args(sys.argv[1:])

    repl = RawRepl(port).connect_raw()

    print("--- main.py paused, reading UART%d (TX %d / RX %d, %d Baud) ---"
          % (UART_ID, TX, RX, BAUD), file=sys.stderr)
    print("--- Ctrl-C stops and restarts the application ---", file=sys.stderr)

    repl.send_nowait(MITLESER % (UART_ID, BAUD, TX, RX))
    if repl.ser.read(2) != b"OK":
        sys.exit("Raw REPL rejected the code.")

    buf = b""
    try:
        while True:
            data = repl.ser.read(512)
            if not data:
                continue
            if raw:
                sys.stdout.write(data.decode("utf-8", "replace"))
                sys.stdout.flush()
                continue
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                text = line.decode("utf-8", "replace").strip()
                if not text or text.startswith("\x04"):
                    continue
                if filt and not any(text[3:6] == f or text[1:6].endswith(f)
                                    for f in filt):
                    continue
                print(text, flush=True)
    except KeyboardInterrupt:
        print("\n--- stopping and resetting the board ---", file=sys.stderr)
        try:
            repl.ser.write(b"\x03\x03")      # laufende Schleife abbrechen
            time.sleep(0.3)
            repl.drain()
            repl.reset_board()
        except Exception:
            pass
    finally:
        repl.close()


if __name__ == "__main__":
    main()
