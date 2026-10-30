# SPDX-License-Identifier: AGPL-3.0-only
"""The NMEA number parsers of geo.py against the frozen 08c5928 reference (#36).

parse_nmea_coord_e8 and parse_decimal_fixed were rewritten to build fewer
temporary strings. Their results are stored for good (e8 degrees, millimetres),
so every input must give exactly what 08c5928 gave, None and the exception
type included.
"""
import random
import tracemalloc
import unittest

from support import cfg  # noqa: F401  (installs the MicroPython stubs)
import geo
import geo_reference

HEMIS = ("N", "S", "E", "W", "", "n", None)
DECIMAL_DIGITS = (0, 1, 2, 3, 8)
FREE_ALPHABET = "0123456789.+- e_\u0661\u00b2"
DECIMAL_EXTRAS = ("12.", ".5", ".", "-", "+", "-.5", "-0.0004", "1e2", " 12.3",
                  "1_000", "0005.4995", "545.4125", "545.41249")
NON_TEXT = (None, 5, 4.5, b"4807.0", ["4807.0"])


def _outcome(function, args):
    """Result of one call, or the exception type it raised."""
    try:
        result = function(*args)
    except Exception as exc:  # the comparison covers raising too
        return ("raises", type(exc))
    return (type(result), result)


def _number(rng, length, alphabet="0123456789"):
    return "".join(rng.choice(alphabet) for _ in range(length))


def _coordinate_cases(rng):
    cases = []
    # Structured: degree width 0..4, whole minutes "00".."61", no point or a
    # point with 0..9 minute decimals, every hemisphere.
    for width in range(5):
        for whole in range(62):
            for decimals in range(-1, 10):
                head = _number(rng, width) + "%02d" % whole
                if decimals < 0:
                    text = head
                else:
                    # Edge digits (0, 4, 5, 9) half of the time.
                    alphabet = "0459" if rng.random() < 0.5 else "0123456789"
                    text = head + "." + _number(rng, decimals, alphabet)
                for hemi in HEMIS:
                    cases.append((text, hemi))
    # Degree limits with and without minutes and decimals, also zero padded.
    for degrees in ("89", "90", "91", "179", "180", "181",
                    "089", "090", "091", "0179", "0180", "0181"):
        for minutes in ("", "00", "01", "59", "60"):
            for fraction in ("", ".", ".0", ".5", ".0000000", ".0000001",
                             ".00000000", ".00000001", ".9999999", ".99999999"):
                for hemi in HEMIS:
                    cases.append((degrees + minutes + fraction, hemi))
    # 32 characters are accepted, 33 are not: with the point at every
    # position and without one.
    for length in (31, 32, 33):
        for point in range(-1, length):
            digits = _number(rng, length if point < 0 else length - 1)
            if point >= 0:
                digits = digits[:point] + "." + digits[point:]
            for hemi in ("N", "E", "W"):
                cases.append((digits, hemi))
        cases.append(("4807." + "0" * (length - 5), "N"))
        cases.append(("01131." + "9" * (length - 6), "E"))
    for value in NON_TEXT:
        for hemi in HEMIS:
            cases.append((value, hemi))
    return cases


def _decimal_cases(rng):
    cases = []
    # Structured: sign, 0..6 whole digits, no point or a point with 0..9
    # fraction digits, every digit count.
    for sign in ("", "+", "-"):
        for whole in range(7):
            for decimals in range(-1, 10):
                for _ in range(4):
                    alphabet = "0459" if rng.random() < 0.5 else "0123456789"
                    text = sign + _number(rng, whole, alphabet)
                    if decimals >= 0:
                        text += "." + _number(rng, decimals, alphabet)
                    for digits in DECIMAL_DIGITS:
                        cases.append((text, digits))
    for text in DECIMAL_EXTRAS:
        for digits in DECIMAL_DIGITS:
            cases.append((text, digits))
    for length in (31, 32, 33):
        for text in ("1" * length, "-" + "2" * (length - 1), "0." + "5" * (length - 2)):
            for digits in DECIMAL_DIGITS:
                cases.append((text, digits))
    for value in NON_TEXT:
        for digits in DECIMAL_DIGITS:
            cases.append((value, digits))
    return cases


def _free_cases(rng):
    """30 000 random texts of length 0..34, each for both parsers."""
    coordinates, decimals = [], []
    for _ in range(30000):
        text = "".join(rng.choice(FREE_ALPHABET) for _ in range(rng.randrange(35)))
        coordinates.append((text, rng.choice(HEMIS)))
        decimals.append((text, rng.choice(DECIMAL_DIGITS)))
    return coordinates, decimals


def _mismatches(new, reference, cases):
    found = []
    for args in cases:
        expected = _outcome(reference, args)
        actual = _outcome(new, args)
        if actual != expected:
            found.append((args, actual, expected))
    return found


def _peak(function, cases, calls=1000):
    """tracemalloc peak above the current level while calling function."""
    for args in cases:
        function(*args)  # warm-up outside the measurement
    base = tracemalloc.get_traced_memory()[0]
    tracemalloc.reset_peak()
    for _ in range(calls):
        for args in cases:
            function(*args)
    return tracemalloc.get_traced_memory()[1] - base


class TestGeoParserEquivalence(unittest.TestCase):
    def test_new_parsers_match_the_reference_on_the_corpus(self):
        rng = random.Random(36)
        coordinates = _coordinate_cases(rng)
        decimals = _decimal_cases(rng)
        free_coordinates, free_decimals = _free_cases(rng)
        coordinates += free_coordinates
        decimals += free_decimals
        self.assertGreaterEqual(len(coordinates) + len(decimals), 60000)
        for name, cases in (("parse_nmea_coord_e8", coordinates),
                            ("parse_decimal_fixed", decimals)):
            found = _mismatches(getattr(geo, name), getattr(geo_reference, name), cases)
            self.assertEqual(found[:10], [], "%s: %d of %d cases differ"
                             % (name, len(found), len(cases)))
        # The corpus reaches the accepting paths, not only the rejections.
        accepted = sum(1 for args in coordinates
                       if geo_reference.parse_nmea_coord_e8(*args) is not None)
        self.assertGreater(accepted, 5000)
        accepted = sum(1 for args in decimals
                       if geo_reference.parse_decimal_fixed(*args) is not None)
        self.assertGreater(accepted, 5000)

    def test_parsers_hold_less_memory_than_the_reference(self):
        # Host proxy for the device's gc.mem_alloc() count (E2): the largest
        # amount of memory held at once while parsing.
        measured = (
            ("parse_nmea_coord_e8", (("4807.038123", "N"), ("01131.000456", "E"))),
            ("parse_decimal_fixed", (("545.412", 3), ("46.903", 3), ("-12.5", 3))),
        )
        tracemalloc.start()
        try:
            for name, cases in measured:
                reference = _peak(getattr(geo_reference, name), cases)
                new = _peak(getattr(geo, name), cases)
                self.assertLess(new, reference, "%s: peak %d bytes, reference %d bytes"
                                % (name, new, reference))
        finally:
            tracemalloc.stop()


if __name__ == "__main__":
    unittest.main()
