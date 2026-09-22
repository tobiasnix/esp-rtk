#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Read the serial output of the running ESP with - only reading, with timestamps. Use: python3 monitor.py [port] [--raw] [--no-ts] Takes the re-enumeration of the native USB-CDC: disappears /dev/ttyACM0 when the board reboots, waits for the script and reconnects. It does not write anything to the port and therefore does not stop the running application. Quit Ctrl-C.
"""
import sys, time, os, serial
from espport import resolve_port, find_ports

args = [a for a in sys.argv[1:] if not a.startswith("--")]
opts = [a for a in sys.argv[1:] if a.startswith("--")]
def initial_port():
    """Waiting for the board at the start instead of aborting - after a reset, the node is gone for one or two seconds."""
    if args:
        return args[0]
    shown = False
    while True:
        found = find_ports()
        if found:
            return found[0][1]
        if not shown:
            print("--- waiting for the board ---", flush=True)
            shown = True
        time.sleep(0.3)


PORT = None
TS = True
RAW = False

def stamp():
    t = time.localtime()
    return "%02d:%02d:%02d " % (t[3], t[4], t[5])

def wait_for_port():
    """Resolve port - after a reset, it can reappear on another node (ttyACM0 -> ttyACM1)."""
    global PORT
    shown = False
    while True:
        found = find_ports()
        if found:
            node = found[0][1]
            if node != PORT:
                print("--- port changed: %s -> %s ---" % (PORT, node), flush=True)
                PORT = node
            return
        if not shown:
            print("--- waiting for the board ---", flush=True)
            shown = True
        time.sleep(0.3)

def main():
    global PORT, TS, RAW
    PORT = initial_port()
    TS = "--no-ts" not in opts
    RAW = "--raw" in opts
    print("--- monitoring %s, press Ctrl-C to stop ---" % PORT, flush=True)
    buf = b""
    while True:
        try:
            wait_for_port()
            ser = serial.Serial(PORT, 115200, timeout=0.5)
            print("%s--- connected ---" % (stamp() if TS else ""), flush=True)
            while True:
                data = ser.read(512)
                if not data:
                    continue
                if RAW:
                    sys.stdout.write(data.decode("utf-8", "replace"))
                    sys.stdout.flush()
                    continue
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    text = line.decode("utf-8", "replace").rstrip("\r")
                    print((stamp() if TS else "") + text, flush=True)
        except KeyboardInterrupt:
            print("\n--- stopped ---")
            return
        except (OSError, serial.SerialException) as e:
            print("%s--- connection lost (%s), reconnecting ---"
                  % (stamp() if TS else "", e), flush=True)
            buf = b""
            time.sleep(1)


if __name__ == "__main__":
    main()
