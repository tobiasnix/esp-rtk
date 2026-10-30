# SPDX-License-Identifier: AGPL-3.0-only
# Frozen reference of 08c5928 for #28c, never edited.
"""Verbatim copy of GNSSHandler.check_nmea_checksum of gnss.py at 08c5928.

tests/test_gnss_line_scanner.py compares the current checksum check and the
line scanner with this function, so that the scanner of #28c accepts and
rejects exactly the sentences 08c5928 accepted and rejected.
"""


def check_nmea_checksum(line):
    try:
        line = line.strip()
        if not line.startswith("$") or "*" not in line:
            return False

        data, hex_sum = line[1:].split("*")
        calculated = 0
        for char in data:
            calculated ^= ord(char)

        return calculated == int(hex_sum, 16)
    except Exception:
        return False
