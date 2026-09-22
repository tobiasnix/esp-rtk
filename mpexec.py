#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Lead code on the MicroPython board in the Raw-REPL. Use: python3 mpexec.py "<code>" [port] [--nowait] --nowait to send code without waiting for output. Needy for everything that tears away the board: machine.bootloader(), machine.reset(). --timeout N seconds waiting for output (standard 15). Longer running code: measurement series, recordings. --keep Board then do NOT restart, but leave it in the REPL. Attention: then it is not reachable until the watchdog closes. Without --keep the board restarts after output - the Raw-REPL holds the application, and a stopped device does not have WLAN, BLE or AP.
"""
import sys
import time

from rawrepl import RawRepl, with_retry


def main():
    opts = [a for a in sys.argv[1:] if a.startswith("--")]
    args = []
    skip_next = False
    for i, a in enumerate(sys.argv[1:]):
        if skip_next:
            skip_next = False
            continue
        if a == "--timeout":
            skip_next = True          # the value belongs to the option
            continue
        if not a.startswith("--"):
            args.append(a)
    if not args:
        sys.exit(__doc__)
    code = args[0]
    port = args[1] if len(args) > 1 else None
    nowait = "--nowait" in opts

    if nowait:
        def operation(repl):
            repl.send_nowait(code)
            time.sleep(0.5)
            print("submitted (--nowait): %s" % code.replace("\n", " "))
        # No leave_raw after that - the board is gone anyway.
        repl = None
        try:
            repl = RawRepl(port).connect_raw()
            operation(repl)
        finally:
            if repl:
                repl.close()
        return

    timeout = 15
    for o in opts:
        if o.startswith("--timeout"):
            teile = o.split("=", 1)
            if len(teile) == 2:
                timeout = int(teile[1])
            elif "--timeout" in sys.argv:
                i = sys.argv.index("--timeout")
                if i + 1 < len(sys.argv):
                    timeout = int(sys.argv[i + 1])

    # Several attempts: since the tools restart the board at the end, an immediately
    # following call can hit the boot process, and a Ctrl-C fizzles while running up.
    output = with_retry(lambda r: r.ex(code, timeout), port=port,
                         versuche=3, name="mpexec",
                         restart="--keep" not in opts)
    sys.stdout.write(output.decode("utf-8", "replace"))


if __name__ == "__main__":
    main()
