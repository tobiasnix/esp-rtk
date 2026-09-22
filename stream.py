#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Authenticated TCP access to the NMEA/$PESPS stream."""
import getpass, os, socket, sys

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    host = argv[0] if argv else os.getenv("ESP_RTK_HOST")
    if not host:
        raise SystemExit("Host is required (argument or ESP_RTK_HOST).")
    port = int(argv[1]) if len(argv) > 1 else 10110
    token = os.getenv("ESP_RTK_STREAM_TOKEN") or getpass.getpass("Stream token: ")
    with socket.create_connection((host, port), timeout=10) as connection:
        if b"AUTH REQUIRED" not in connection.recv(128): raise SystemExit("Unerwartete Serverantwort")
        connection.sendall(("AUTH %s\r\n" % token).encode("ascii"))
        if b"OK AUTH" not in connection.recv(128): raise SystemExit("Authentication rejected")
        while True:
            data = connection.recv(4096)
            if not data: break
            sys.stdout.write(data.decode("ascii", "replace")); sys.stdout.flush()

if __name__ == "__main__": main()
