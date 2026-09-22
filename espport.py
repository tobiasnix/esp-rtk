#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Locate the ESP32-S3 serial port through stable ``/dev/serial/by-id`` links.

The kernel node may change after a reset or ROM-download transition. Native
USB CDC and the external UART bridge are supported; the inactive GNSS-board
CP2102N interface is intentionally ignored. Native USB is preferred.
"""
import os, glob, sys

BY_ID = "/dev/serial/by-id"
# Order is the preferred order, not just a lot.
MARKERS = ("Espressif_Systems_Espressif_Device",
           "Espressif_USB_JTAG_serial_debug_unit",
           "1a86_USB_Single_Serial")


def find_ports():
    """Return all candidates as (by-id path, device node), preferred first."""
    out = []
    for marker in MARKERS:                 # by preference, non-alphabetical
        for link in sorted(glob.glob(os.path.join(BY_ID, "*"))):
            name = os.path.basename(link)
            if marker in name:
                out.append((link, os.path.realpath(link)))
    return out


def port_art(link):
    """Clearly, which way this is."""
    if "JTAG" in link:
        return "Download-Modus"
    if "1a86" in link:
        return "UART bridge (COM)"
    return "MicroPython (native USB)"


def resolve_port(explicit=None, quiet=False):
    """Submit Explicit Port, otherwise automatically search the ESP."""
    if explicit:
        return explicit
    found = find_ports()
    if not found:
        sys.exit("No ESP32-S3 found. Available in %s:\n  %s"
                 % (BY_ID, "\n  ".join(os.listdir(BY_ID)) if os.path.isdir(BY_ID) else "(nichts)"))
    link, node = found[0]
    if len(found) > 1 and not quiet:
        print("Multiple candidates; using %s" % node, file=sys.stderr)
    if not quiet:
        print("Port: %s (%s)" % (node, port_art(link)), file=sys.stderr)
    return node


if __name__ == "__main__":
    # --node: only output the device node for which shell: esptool --port $(python3
    # espport.py --node) ...
    if "--node" in sys.argv:
        print(resolve_port(quiet=True))
    else:
        for link, node in find_ports():
            print("%-12s %s -> %s" % (port_art(link), os.path.basename(link), node))
