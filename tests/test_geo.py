# SPDX-License-Identifier: AGPL-3.0-only
"""Integer (e8) coordinate parsing and ellipsoidal local geometry."""
import ast
import inspect
import json
import math
import os
import random
import struct
import textwrap
import unittest
from unittest import mock

from support import cfg  # noqa: F401  (installs the MicroPython stubs)
import geo


def float_arithmetic(function):
    """Names the true divisions and float literals in the source of function."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Div):
            found.append("true division")
        elif isinstance(node, ast.Constant) and isinstance(node.value, float):
            found.append("float literal %r" % node.value)
    return found


class TestDigits(unittest.TestCase):
    """_digits has a C-speed fast path (str.isdigit) that must stay ASCII-only."""

    def reference(self, text):
        # The original character loop: the behaviour the fast path must keep.
        return bool(text) and all("0" <= char <= "9" for char in text)

    def test_ascii_digits_pass(self):
        for text in ("0", "9", "0123456789", "007", "4807038000"):
            self.assertTrue(geo._digits(text), text)

    def test_empty_and_non_digits_fail(self):
        for text in ("", " ", "1 2", "12 ", " 12", "-1", "+1", "1.5", "1_0", "0x1",
                     "1e3", "a", "\n", "\t1"):
            self.assertFalse(geo._digits(text), repr(text))

    def test_unicode_digits_fail(self):
        # CPython's str.isdigit accepts these (and int() converts them); the
        # device's isdigit does not, so the host must agree with the device.
        for text in ("\u0661\u0662", "\u00b2", "\u2460", "\uff11\uff12", "\u0967",
                     "12\u0663", "\u0663" "12", "1\u00b3"):
            self.assertFalse(geo._digits(text), ascii(text))

    def test_unicode_digits_are_rejected_by_the_parsers(self):
        self.assertIsNone(geo.parse_nmea_coord_e8("4807.\u0660\u0663", "N"))
        self.assertIsNone(geo.parse_nmea_coord_e8("\uff14807.038", "N"))
        self.assertIsNone(geo.parse_decimal_fixed("12\u0663.5", 3))
        self.assertIsNone(geo.parse_decimal_fixed("1.\u00b2", 3))

    def test_agrees_with_the_character_loop(self):
        rng = random.Random(36)
        alphabet = "0123456789" * 3 + "+-. _eE\u0661\u00b2\uff11x"
        for _ in range(3000):
            text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 9)))
            self.assertEqual(geo._digits(text), self.reference(text), ascii(text))


class TestParseNmeaCoordE8(unittest.TestCase):
    def test_spec_example_is_exact(self):
        self.assertEqual(geo.parse_nmea_coord_e8("4807.407360", "N"), 4812345600)

    def test_short_minute_fraction_is_zero_padded(self):
        self.assertEqual(geo.parse_nmea_coord_e8("4807.038", "N"), 4811730000)

    def test_three_digit_longitude_degrees(self):
        self.assertEqual(geo.parse_nmea_coord_e8("12345.678901", "E"), 12376131502)

    def test_south_and_west_are_negative(self):
        self.assertEqual(geo.parse_nmea_coord_e8("4807.407360", "S"), -4812345600)
        self.assertEqual(geo.parse_nmea_coord_e8("00039.259260", "W"), -65432100)

    def test_rounding_of_the_minute_conversion(self):
        # 0.000001' = 1.666…e-8 degrees -> rounds to 2 e8.
        self.assertEqual(geo.parse_nmea_coord_e8("0000.000001", "N"), 2)
        # 0.000002' = 3.333…e-8 degrees -> rounds down to 3 e8.
        self.assertEqual(geo.parse_nmea_coord_e8("0000.000002", "N"), 3)

    def test_seven_minute_decimals_round_half_up(self):
        self.assertEqual(geo.parse_nmea_coord_e8("0000.0000005", "N"), 1)

    def test_exact_tie_rounds_up_and_just_below_it_rounds_down(self):
        # 0.0000003' is exactly 0.5 e8. 0.00000029' is below the tie although it
        # would round to 0.0000003' at seven minute decimals: no double rounding.
        self.assertEqual(geo.parse_nmea_coord_e8("0000.0000003", "N"), 1)
        self.assertEqual(geo.parse_nmea_coord_e8("0000.00000029", "N"), 0)

    def test_garbage_returns_none_without_raising(self):
        for value in ("", None, "abc", "4860.000000", "9100.000000", "48.5", "4807.40.73",
                      "+4807.407360", "-4807.407360", " 4807.407360", "48_07.407360",
                      "4807.4073 ", "4807.40736٣"):
            self.assertIsNone(geo.parse_nmea_coord_e8(value, "N"), value)
        self.assertIsNone(geo.parse_nmea_coord_e8("4807.407360", "X"))
        self.assertIsNone(geo.parse_nmea_coord_e8("4807.407360", ""))

    def test_boundary_degrees(self):
        self.assertEqual(geo.parse_nmea_coord_e8("9000.000000", "N"), 9000000000)
        self.assertEqual(geo.parse_nmea_coord_e8("18000.000000", "W"), -18000000000)
        self.assertIsNone(geo.parse_nmea_coord_e8("9000.000001", "N"))
        self.assertIsNone(geo.parse_nmea_coord_e8("18000.000001", "E"))
        # 91 degrees is out of range for a latitude but a valid longitude.
        self.assertEqual(geo.parse_nmea_coord_e8("9100.000000", "E"), 9100000000)

    def test_small_int_path_equals_the_documented_formula(self):
        # The e8 scale with up to seven minute decimals converts the minutes in
        # small ints; longer fractions (8 and 9 digits here) use the generic
        # formula. Both must give deg * scale + (2 * minutes_num * scale +
        # 60 * minutes_scale) // (120 * minutes_scale) for every input.
        rng = random.Random(20261020)
        scale = geo.COORD_SCALE
        for _ in range(3000):
            hemi = rng.choice("NSEW")
            limit = 90 if hemi in "NS" else 180
            deg = rng.randrange(limit + 1)
            digits = rng.randrange(10)
            if deg == limit:
                # The limit itself is valid, but only with zero minutes.
                whole, fraction = 0, "0" * digits
            else:
                whole = rng.randrange(60)
                fraction = "".join(rng.choice("0123456789") for _ in range(digits))
            width = 2 if hemi in "NS" else 3
            text = "%0*d%02d.%s" % (width, deg, whole, fraction)
            minutes_num = int("%02d%s" % (whole, fraction))
            minutes_scale = 10 ** len(fraction)
            expected = deg * scale + (
                (2 * minutes_num * scale + 60 * minutes_scale) // (120 * minutes_scale))
            if hemi in "SW":
                expected = -expected
            self.assertEqual(geo.parse_nmea_coord_e8(text, hemi), expected, (text, hemi))

    def test_small_int_path_covers_the_largest_minute_values(self):
        # 59.9999999 minutes rounds up to the next whole degree, the limit itself.
        self.assertEqual(geo.parse_nmea_coord_e8("0000.0000000", "N"), 0)
        self.assertEqual(geo.parse_nmea_coord_e8("8959.9999999", "N"), 9000000000)
        self.assertEqual(geo.parse_nmea_coord_e8("17959.9999999", "W"), -18000000000)
        # x = minutes_num * 10 ** (7 - fraction digits) stays below 2 ** 30, the
        # largest MicroPython small int, for every fraction length of the path.
        for digits in range(8):
            largest = int("59" + "9" * digits) * geo._POW10[7 - digits]
            self.assertLessEqual(largest, 599999999, digits)
            self.assertLess(largest, 2 ** 30, digits)

    def test_e7_fallback_follows_coord_scale(self):
        with mock.patch.object(geo, "COORD_SCALE", 10 ** 7):
            self.assertEqual(geo.parse_nmea_coord_e8("4807.407360", "N"), 481234560)

    def test_parser_never_calls_float(self):
        with mock.patch.object(geo, "float", side_effect=AssertionError("float used"),
                               create=True):
            self.assertEqual(geo.parse_nmea_coord_e8("4807.407360", "N"), 4812345600)
            self.assertEqual(geo.parse_decimal_fixed("123.456", 3), 123456)
        self.assertNotIn("float(", inspect.getsource(geo.parse_nmea_coord_e8))
        self.assertNotIn("float(", inspect.getsource(geo.parse_decimal_fixed))

    def test_parsers_use_no_division_or_float_literals(self):
        # The checks above see float() calls; the syntax tree also rules out "/"
        # and literals such as 0.5. The geometry functions may use floats, these may not.
        for function in (geo._digits, geo.parse_decimal_fixed, geo.parse_nmea_coord_e8):
            self.assertEqual(float_arithmetic(function), [], function.__name__)

    def test_float_detector_sees_division_and_float_literals(self):
        def sample(value):
            half = value / 2
            return half + 0.5

        self.assertEqual(float_arithmetic(sample), ["true division", "float literal 0.5"])

    def test_overlong_text_is_rejected_before_int(self):
        # 32 characters still parse; int() raises ValueError on thousands of digits.
        self.assertEqual(geo.parse_nmea_coord_e8("4800." + "0" * 27, "N"), 4800000000)
        self.assertIsNone(geo.parse_nmea_coord_e8("4800." + "0" * 28, "N"))
        self.assertIsNone(geo.parse_nmea_coord_e8("4800." + "0" * 5000, "N"))


class TestParseDecimalFixed(unittest.TestCase):
    def test_millimetres_from_metres(self):
        self.assertEqual(geo.parse_decimal_fixed("123.456", 3), 123456)
        self.assertEqual(geo.parse_decimal_fixed("-0.5", 3), -500)
        self.assertEqual(geo.parse_decimal_fixed("12", 3), 12000)
        self.assertEqual(geo.parse_decimal_fixed("12.", 3), 12000)
        self.assertEqual(geo.parse_decimal_fixed("+7.1", 3), 7100)

    def test_extra_digits_round_half_up(self):
        self.assertEqual(geo.parse_decimal_fixed("1.2345", 3), 1235)
        self.assertEqual(geo.parse_decimal_fixed("1.2344", 3), 1234)
        self.assertEqual(geo.parse_decimal_fixed("-1.2345", 3), -1235)
        # No double rounding: 1.23449 is below the tie although it rounds to 1.2345.
        self.assertEqual(geo.parse_decimal_fixed("1.23451", 3), 1235)
        self.assertEqual(geo.parse_decimal_fixed("1.23449", 3), 1234)

    def test_overlong_text_is_rejected_before_int(self):
        # 32 characters still parse; int() raises ValueError on thousands of digits.
        self.assertEqual(geo.parse_decimal_fixed("1" * 32, 3), int("1" * 32 + "000"))
        self.assertIsNone(geo.parse_decimal_fixed("1" * 33, 3))
        self.assertIsNone(geo.parse_decimal_fixed("1" * 5000, 3))

    def test_eight_digit_degrees(self):
        self.assertEqual(geo.parse_decimal_fixed("51.12345678", 8), 5112345678)

    def test_invalid_returns_none(self):
        for value in ("", None, "abc", "1.2.3", "--1", "1e3", " 1", "1_0", ".", "+", "1٣"):
            self.assertIsNone(geo.parse_decimal_fixed(value, 3), value)


class TestFloat32Evidence(unittest.TestCase):
    """Document why the integer path exists: the old chain loses ~0.4 m."""

    @staticmethod
    def f32(value):
        return struct.unpack("f", struct.pack("f", value))[0]

    def test_legacy_float_chain_loses_the_latitude(self):
        text = "5107.407360"
        value = self.f32(float(text))
        deg = int(value // 100)
        legacy = self.f32(deg + self.f32(self.f32(value - deg * 100) / 60.0))
        exact = geo.parse_nmea_coord_e8(text, "N") / geo.COORD_SCALE
        error_m = abs(legacy - exact) * 111132.954
        self.assertGreater(error_m, 0.05)
        self.assertLess(error_m, 0.5)


class TestLocalGeometry(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = os.path.join(os.path.dirname(__file__), "fixtures",
                            "stakeout_geometry_vectors.json")
        with open(path, encoding="utf-8") as handle:
            cls.fixture = json.load(handle)

    def tolerance_m(self, distance_m):
        return 0.001 if distance_m <= 1000 else 0.002 if distance_m <= 10000 else 0.2

    def test_offsets_are_consistent_with_the_vincenty_distance(self):
        for vector in self.fixture["vectors"]:
            north, east = geo.offsets_e8_m(*vector["ref"], *vector["target"])
            tolerance = self.tolerance_m(vector["distance_m"])
            self.assertAlmostEqual(math.hypot(north, east), vector["distance_m"],
                                   delta=tolerance, msg=vector["name"])
            if vector["name"].startswith("north"):
                self.assertAlmostEqual(north, vector["distance_m"], delta=tolerance)
                self.assertEqual(east, 0.0)
            if vector["name"].startswith("east"):
                self.assertAlmostEqual(east, vector["distance_m"], delta=tolerance)
                self.assertEqual(north, 0.0)
            if vector["name"] == "sw_1km":
                self.assertLess(north, -899.0)
                self.assertLess(east, -399.0)

    def test_distance_matches_vincenty(self):
        for vector in self.fixture["vectors"]:
            distance = geo.distance_e8_m(*vector["ref"], *vector["target"])
            self.assertAlmostEqual(distance, vector["distance_m"],
                                   delta=self.tolerance_m(vector["distance_m"]), msg=vector["name"])

    def test_degree_path_agrees_with_integer_path(self):
        vector = next(v for v in self.fixture["vectors"] if v["name"] == "sw_1km")
        as_deg = [value / geo.COORD_SCALE for value in vector["ref"] + vector["target"]]
        self.assertAlmostEqual(geo.distance_deg_m(*as_deg),
                               geo.distance_e8_m(*vector["ref"], *vector["target"]), delta=1e-6)
        # Component by component, so the return order (north, east) and the sign
        # (target minus reference) of the degree path are pinned as well. The
        # vector lies to the south-west, so both components are negative.
        north_deg, east_deg = geo.offsets_deg_m(*as_deg)
        north_e8, east_e8 = geo.offsets_e8_m(*vector["ref"], *vector["target"])
        self.assertAlmostEqual(north_deg, north_e8, delta=1e-6)
        self.assertAlmostEqual(east_deg, east_e8, delta=1e-6)

    def test_identical_points_have_zero_distance(self):
        self.assertEqual(geo.distance_e8_m(5100000000, 1000000000, 5100000000, 1000000000), 0.0)

    def test_one_e8_step_is_about_a_millimetre(self):
        self.assertAlmostEqual(geo.distance_e8_m(5100000000, 1000000000, 5100000001, 1000000000),
                               0.00111, delta=0.00002)

    def test_distance_across_the_antimeridian_is_the_short_way(self):
        # Two e8 units apart on opposite sides of 180 degrees, not 360 degrees apart.
        self.assertAlmostEqual(
            geo.distance_e8_m(5000000000, 17999999999, 5000000000, -17999999999),
            0.00143, delta=0.0001)

    def test_e8_offsets_across_the_antimeridian_carry_the_sign_of_the_short_way(self):
        north, east = geo.offsets_e8_m(5000000000, 17999999999, 5000000000, -17999999999)
        self.assertEqual(north, 0.0)
        self.assertAlmostEqual(east, 0.00143, delta=0.0001)
        _north, east = geo.offsets_e8_m(5000000000, -17999999999, 5000000000, 17999999999)
        self.assertAlmostEqual(east, -0.00143, delta=0.0001)

    def test_degree_offsets_across_the_antimeridian_carry_the_sign_of_the_short_way(self):
        north, east = geo.offsets_deg_m(50.0, 179.99999999, 50.0, -179.99999999)
        self.assertEqual(north, 0.0)
        self.assertAlmostEqual(east, 0.00143, delta=0.0001)
        _north, east = geo.offsets_deg_m(50.0, -179.99999999, 50.0, 179.99999999)
        self.assertAlmostEqual(east, -0.00143, delta=0.0001)

    def test_local_scale_at_the_equator_and_51_degrees(self):
        m, p = geo.local_scale(0.0)
        self.assertAlmostEqual(m, 110574.3, delta=0.5)
        self.assertAlmostEqual(p, 111319.5, delta=0.5)
        m, p = geo.local_scale(51.0)
        self.assertAlmostEqual(m, 111248.3, delta=1.0)
        self.assertAlmostEqual(p, 70197.7, delta=1.0)


class TestBearing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = os.path.join(os.path.dirname(__file__), "fixtures",
                            "stakeout_geometry_vectors.json")
        with open(path, encoding="utf-8") as handle:
            cls.vectors = json.load(handle)["vectors"]

    def test_bearing_matches_the_vincenty_vectors(self):
        checked = 0
        for vector in self.vectors:
            if vector["distance_m"] < 1:
                continue
            bearing = geo.bearing_deg(*geo.offsets_e8_m(*vector["ref"], *vector["target"]))
            difference = abs(bearing - vector["bearing_deg"]) % 360
            self.assertLess(min(difference, 360 - difference), 0.05, vector["name"])
            checked += 1
        self.assertGreaterEqual(checked, 7)
        by_name = {vector["name"]: vector for vector in self.vectors}
        for name, expected in (("north_0_05m", 0.0), ("east_0_05m", 90.0)):
            vector = by_name[name]
            self.assertEqual(
                geo.bearing_deg(*geo.offsets_e8_m(*vector["ref"], *vector["target"])), expected)

    def test_bearing_quadrants_and_origin(self):
        for north, east, expected in ((1, 0, 0.0), (0, 1, 90.0), (-1, 0, 180.0),
                                      (0, -1, 270.0), (1, -1, 315.0)):
            self.assertAlmostEqual(geo.bearing_deg(north, east), expected, places=9)
        self.assertIsNone(geo.bearing_deg(0, 0))
        self.assertIsNone(geo.bearing_deg(0.0, -0.0))
        for east in (-1e-12, -1e-300):
            bearing = geo.bearing_deg(1, east)
            self.assertGreaterEqual(bearing, 0.0)
            self.assertLess(bearing, 360.0)


class TestIntegerHelpers(unittest.TestCase):
    def test_mean_rounds_half_up(self):
        self.assertEqual(geo.mean_e8([1, 2]), 2)
        self.assertEqual(geo.mean_e8([-3, -4]), -3)
        self.assertEqual(geo.mean_e8([5100000000, 5100000001, 5100000001]), 5100000001)
        with self.assertRaises(ValueError):
            geo.mean_e8([])

    def test_longitude_mean_unwraps_around_the_antimeridian(self):
        # The plain mean of 179.99999999 and -179.99999999 degrees would be 0.
        self.assertEqual(geo.mean_lon_e8([17999999999, -17999999999]), 18000000000)
        self.assertEqual(geo.mean_lon_e8([-17999999999, 17999999999]), 18000000000)

    def test_longitude_mean_is_normalised_into_the_half_open_interval(self):
        # The mean may land on either side of the antimeridian; -180 becomes +180.
        self.assertEqual(geo.mean_lon_e8([17999999999, -17999999997]), -17999999999)
        self.assertEqual(geo.mean_lon_e8([-18000000000]), 18000000000)
        self.assertEqual(geo.mean_lon_e8([18000000000]), 18000000000)

    def test_longitude_mean_without_a_crossing_is_the_plain_mean(self):
        self.assertEqual(geo.mean_lon_e8([800000000, 800000002]), 800000001)
        self.assertEqual(geo.mean_lon_e8(iter([-3, -4])), geo.mean_e8([-3, -4]))

    def test_longitude_mean_of_an_empty_sequence_raises(self):
        with self.assertRaises(ValueError):
            geo.mean_lon_e8([])

    def test_degree_conversions(self):
        self.assertEqual(geo.deg_to_e8(51.12345678), 5112345678)
        self.assertEqual(geo.deg_to_e8(-0.65432100), -65432100)
        # 10.0000001 * 1e8 is 1000000009.9999999: int() alone would truncate it.
        self.assertEqual(geo.deg_to_e8(10.0000001), 1000000010)
        self.assertEqual(geo.e8_to_deg(5112345678), 51.12345678)


class TestFormatting(unittest.TestCase):
    def test_format_fixed_is_sign_aware(self):
        self.assertEqual(geo.format_e8(5112345678), "51.12345678")
        self.assertEqual(geo.format_e8(-65432100), "-0.65432100")
        self.assertEqual(geo.format_e8(0), "0.00000000")
        self.assertEqual(geo.format_e8(-5), "-0.00000005")
        self.assertEqual(geo.format_mm(123456), "123.456")
        self.assertEqual(geo.format_mm(-2500), "-2.500")
        self.assertEqual(geo.format_fixed(5, 3), "0.005")

    def test_format_round_trips_through_the_parser(self):
        for value in (5112345678, -65432100, 0, 18000000000, -9000000000):
            self.assertEqual(geo.parse_decimal_fixed(geo.format_e8(value), 8), value)

    def test_values_around_one_unit_keep_their_padding_and_sign(self):
        self.assertEqual(geo.format_e8(99999999), "0.99999999")
        self.assertEqual(geo.format_e8(100000000), "1.00000000")
        self.assertEqual(geo.format_e8(100000001), "1.00000001")
        self.assertEqual(geo.format_e8(-99999999), "-0.99999999")
        self.assertEqual(geo.format_e8(-100000000), "-1.00000000")
        self.assertEqual(geo.format_mm(0), "0.000")
        self.assertEqual(geo.format_mm(-1), "-0.001")
        self.assertEqual(geo.format_fixed(10, 1), "1.0")
        self.assertEqual(geo.format_fixed(-10, 1), "-1.0")

    def test_zero_digits_have_no_decimal_point(self):
        self.assertEqual(geo.format_fixed(42, 0), "42")
        self.assertEqual(geo.format_fixed(-42, 0), "-42")
        self.assertEqual(geo.format_fixed(0, 0), "0")

    def test_large_values_are_exact(self):
        # The full coordinate and height ranges, then integers far beyond any float.
        self.assertEqual(geo.format_e8(18000000000), "180.00000000")
        self.assertEqual(geo.format_e8(-18000000000), "-180.00000000")
        self.assertEqual(geo.format_mm(100000000), "100000.000")
        self.assertEqual(geo.format_mm(-20000000), "-20000.000")
        self.assertEqual(geo.format_fixed(123456789012345678901234567890, 3),
                         "123456789012345678901234567.890")
        self.assertEqual(geo.format_fixed(-123456789012345678901234567890, 3),
                         "-123456789012345678901234567.890")

    def test_round_trip_at_every_digit_count(self):
        for digits in range(10):
            unit = 10 ** digits
            for magnitude in (0, 1, unit - 1, unit, unit + 1, 123 * unit + 45, 18000000000):
                for value in (magnitude, -magnitude):
                    text = geo.format_fixed(value, digits)
                    self.assertEqual(geo.parse_decimal_fixed(text, digits), value,
                                     (value, digits, text))
                    # Exactly ``digits`` fraction digits, whatever the value.
                    self.assertEqual(len(text.partition(".")[2]), digits, text)

    def test_digits_must_be_a_non_negative_int(self):
        for bad in (-1, True, False, 3.0, "3", None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                geo.format_fixed(5, bad)

    def test_scale_links_are_pinned(self):
        # format_e8 reads COORD_DIGITS when called, so a different scale changes
        # the text with it; format_mm and ALT_SCALE share one digit count.
        with mock.patch.object(geo, "COORD_DIGITS", 7):
            self.assertEqual(geo.format_e8(51123456), "5.1123456")
        self.assertEqual(geo.format_e8(51123456), "0.51123456")
        self.assertEqual(10 ** geo.ALT_DIGITS, geo.ALT_SCALE)
        self.assertEqual(10 ** 3, geo.ALT_SCALE)
        self.assertEqual(geo.format_mm(1500), geo.format_fixed(1500, geo.ALT_DIGITS))

    def test_only_a_real_int_is_formatted(self):
        for bad in (True, False, 5.0, 5112345678.0, "5", None, [5], (5,)):
            for function in (lambda value: geo.format_fixed(value, 3), geo.format_e8,
                             geo.format_mm):
                with self.assertRaises(TypeError, msg=repr(bad)):
                    function(bad)

    def test_formatting_never_uses_floats(self):
        with mock.patch.object(geo, "float", side_effect=AssertionError("float used"),
                               create=True):
            self.assertEqual(geo.format_e8(5112345678), "51.12345678")
            self.assertEqual(geo.format_mm(-2500), "-2.500")
            self.assertEqual(geo.format_fixed(-42, 0), "-42")
        self.assertNotIn("float(", inspect.getsource(geo.format_fixed))
        self.assertEqual(float_arithmetic(geo.format_fixed), [])


class TestLegacyConversion(unittest.TestCase):
    def test_legacy_coordinate_converts_to_integers(self):
        self.assertEqual(geo.legacy_coordinate_to_e8([8.0, 50.0, 123.4]),
                         [800000000, 5000000000, 123400])
        self.assertEqual(geo.legacy_coordinate_to_e8([8, 50, 100]),
                         [800000000, 5000000000, 100000])
        self.assertEqual(geo.legacy_coordinate_to_e8([-0.654321, -33.5, -2.5]),
                         [-65432100, -3350000000, -2500])
        for bad in ([8.0, 50.0], [181.0, 50.0, 0.0], [8.0, 91.0, 0.0], [8.0, 50.0, 200000.0],
                    [True, 50.0, 0.0], [float("nan"), 50.0, 0.0], "8,50,0", None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                geo.legacy_coordinate_to_e8(bad)

    def test_result_is_always_a_list_of_three_ints(self):
        for coordinate in ([8.0, 50.0, 123.4], (8.0, 50.0, 123.4), [8, 50, 100], (8, 50, 100),
                           [-0.0, 0.0, -0.0]):
            result = geo.legacy_coordinate_to_e8(coordinate)
            self.assertIs(type(result), list, coordinate)
            self.assertEqual([type(item) for item in result], [int, int, int], coordinate)
        self.assertEqual(geo.legacy_coordinate_to_e8((8.0, 50.0, 123.4)),
                         [800000000, 5000000000, 123400])
        self.assertEqual(geo.legacy_coordinate_to_e8([-0.0, 0.0, -0.0]), [0, 0, 0])

    def test_degrees_are_rounded_through_deg_to_e8(self):
        # 10.0000001 * 1e8 is 1000000009.9999999: int() alone would truncate it.
        self.assertEqual(geo.legacy_coordinate_to_e8([10.0000001, 10.0000001, 0]),
                         [1000000010, 1000000010, 0])
        with mock.patch.object(geo, "COORD_SCALE", 10 ** 7):
            self.assertEqual(geo.legacy_coordinate_to_e8([8.0, 50.0, 123.4]),
                             [80000000, 500000000, 123400])

    def test_altitude_is_rounded_to_the_nearest_millimetre(self):
        for metres, millimetres in ((1.2344, 1234), (1.2346, 1235), (-1.2344, -1234),
                                    (-1.2346, -1235), (0.0004, 0), (-0.0004, 0),
                                    (0.0006, 1), (-0.0006, -1)):
            self.assertEqual(geo.legacy_coordinate_to_e8([0, 0, metres])[2], millimetres,
                             metres)

    def test_limits_are_inclusive(self):
        self.assertEqual(geo.legacy_coordinate_to_e8([180, 90, 100000]),
                         [18000000000, 9000000000, 100000000])
        self.assertEqual(geo.legacy_coordinate_to_e8([-180, -90, -20000]),
                         [-18000000000, -9000000000, -20000000])
        for bad in ([180.0000001, 0, 0], [-180.0000001, 0, 0], [0, 90.0000001, 0],
                    [0, -90.0000001, 0], [0, 0, 100000.001], [0, 0, -20000.001],
                    [181, 0, 0], [0, -91, 0], [0, 0, 100001], [0, 0, -20001]):
            with self.assertRaises(ValueError, msg=repr(bad)):
                geo.legacy_coordinate_to_e8(bad)

    def test_non_finite_numbers_are_rejected_in_every_position(self):
        for special in (float("nan"), float("inf"), float("-inf")):
            for position in range(3):
                bad = [0.0, 0.0, 0.0]
                bad[position] = special
                with self.assertRaises(ValueError, msg=repr(bad)):
                    geo.legacy_coordinate_to_e8(bad)

    def test_bools_and_non_numbers_are_rejected_in_every_position(self):
        for wrong in (True, False, "8", None, [8.0], b"8"):
            for position in range(3):
                bad = [0.0, 0.0, 0.0]
                bad[position] = wrong
                with self.assertRaises(ValueError, msg=repr(bad)):
                    geo.legacy_coordinate_to_e8(bad)

    def test_huge_integers_are_out_of_range_not_a_crash(self):
        for bad in ([10 ** 400, 0, 0], [0, -10 ** 400, 0], [0, 0, 10 ** 400]):
            with self.assertRaises(ValueError, msg=repr(bad)[:40]):
                geo.legacy_coordinate_to_e8(bad)

    def test_anything_but_a_three_item_list_or_tuple_is_rejected(self):
        for bad in ([], [8.0], [8.0, 50.0], [8.0, 50.0, 0.0, 0.0], (8.0, 50.0), "abc",
                    b"abc", {"lon": 8.0, "lat": 50.0, "alt": 0.0}, {8.0, 50.0, 0.0},
                    iter([8.0, 50.0, 0.0]), 8.0, 5, True, None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                geo.legacy_coordinate_to_e8(bad)


class F32(float):
    """A float that rounds every arithmetic result to IEEE single precision.

    Stands in for MicroPython's 32-bit float: the value and every
    intermediate product or difference is passed through struct 'f'.
    int(), round() and comparisons behave like the device's.
    """

    @staticmethod
    def wrap(value):
        return F32(struct.unpack("f", struct.pack("f", float(value)))[0])

    def __new__(cls, value):
        return float.__new__(cls, struct.unpack("f", struct.pack("f", float(value)))[0])

    def __add__(self, other):
        return F32.wrap(float(self) + float(F32.wrap(other)))

    __radd__ = __add__

    def __sub__(self, other):
        return F32.wrap(float(self) - float(F32.wrap(other)))

    def __rsub__(self, other):
        return F32.wrap(float(F32.wrap(other)) - float(self))

    def __mul__(self, other):
        return F32.wrap(float(self) * float(F32.wrap(other)))

    __rmul__ = __mul__

    def __truediv__(self, other):
        return F32.wrap(float(self) / float(F32.wrap(other)))

    def __neg__(self):
        return F32.wrap(-float(self))

    def __abs__(self):
        return F32.wrap(abs(float(self)))


class TestLegacyConversionOnFloat32(unittest.TestCase):
    """The device multiplies in float32: the e8 result must not leave the float's grid."""

    SAMPLES = ("52.5164321", "13.3777041", "85.0511287", "179.9999999", "50.0000071",
               "50.00015", "-33.5", "-179.123456", "0.0000123", "-0.654321", "89.9999",
               "1.0", "-1.5", "180", "-90", "45.123456", "21.987654", "-85.9")

    def assert_device_matches_host(self, text):
        device_value = F32(float(text))
        host = geo.deg_to_e8(float(device_value))
        device = geo.deg_to_e8(device_value)
        self.assertLessEqual(abs(device - host), 4, (text, device, host))

    def test_named_latitudes_and_longitudes(self):
        for text in self.SAMPLES:
            self.assert_device_matches_host(text)

    def test_sweep_over_the_whole_degree_range(self):
        generator = random.Random(5)
        for _ in range(4000):
            value = generator.uniform(-180.0, 180.0)
            self.assert_device_matches_host(repr(value))
        for whole in range(-180, 181):
            for fraction in ("0", "5", "123456", "9999999"):
                self.assert_device_matches_host("%d.%s" % (whole, fraction))

    def test_emulation_catches_a_single_large_product(self):
        # Guard against a vacuous emulation: the old one-product form leaves
        # the float32 grid by hundreds of e8 units at a 50 degree latitude.
        value = F32(50.0000071)
        single = int(round(value * geo.COORD_SCALE))
        self.assertGreater(abs(single - geo.deg_to_e8(float(value))), 100)

    def test_legacy_triple_on_float32(self):
        triple = [F32(13.3777041), F32(52.5164321), F32(34.567)]
        host = geo.legacy_coordinate_to_e8([float(value) for value in triple])
        device = geo.legacy_coordinate_to_e8(triple)
        for got, want in zip(device, host):
            self.assertLessEqual(abs(got - want), 4, (device, host))
        self.assertEqual(geo.legacy_coordinate_to_e8([F32(-0.654321), F32(-33.5), F32(-2.5)]),
                         [-65432100, -3350000000, -2500])
