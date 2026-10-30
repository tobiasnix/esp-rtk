# SPDX-License-Identifier: AGPL-3.0-only
"""NMEA processing: checksum, coordinates, and GGA parsing."""
import struct
import unittest
from unittest import mock

from support import cfg, gnss, sentence
import geo

# The classic from the NMEA documentation - checksum occupies externally.
GGA_WIKI = "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47"

# Synthetic RTK_FLOAT fixture; it does not represent a surveyed location.
GGA_RTK = ("$GNGGA,174608.000,0007.407360,N,00039.259260,W,5,31,0.57,"
           "123.456,M,12.3,M,2.0,0054*43")

# The same set structure without a fix - all position fields empty.
GGA_NOFIX = "$GNGGA,174608.000,,,,,0,00,99.99,,,,,,*44"


class _Dummy:
    """parse_gga is an instance method but does not use self."""


class TestChecksum(unittest.TestCase):
    def check(self, line):
        return gnss.GNSSHandler.check_nmea_checksum(line)

    def test_accepts_valid_sentence(self):
        self.assertTrue(self.check(GGA_WIKI))

    def test_accepts_rtk_sentence_from_the_readme(self):
        self.assertTrue(self.check(GGA_RTK))

    def test_accepts_sentence_without_fix(self):
        self.assertTrue(self.check(GGA_NOFIX))

    def test_detects_corrupted_payload(self):
        # A digit changed, checksum unchanged -> must stand out.
        self.assertFalse(self.check(GGA_WIKI.replace("4807.038", "4807.039")))

    def test_detects_false_checksum(self):
        self.assertFalse(self.check(GGA_WIKI[:-2] + "00"))

    def test_without_a_star(self):
        self.assertFalse(self.check("$GPGGA,123519,4807.038"))

    def test_without_dollars(self):
        self.assertFalse(self.check(GGA_WIKI[1:]))

    def test_empty_sentence(self):
        self.assertFalse(self.check(""))

    def test_non_hexadecimal_checksum(self):
        self.assertFalse(self.check(GGA_WIKI[:-2] + "ZZ"))

    def test_mehrfacher_stern(self):
        # split("*") delivers three parts -> ValueError, must be caught.
        self.assertFalse(self.check("$GPGGA,1*2*47"))

    def test_surrounding_spaces_do_not_interfere(self):
        self.assertTrue(self.check("  %s \r\n" % GGA_WIKI))

    def test_proprietary_quilt_phrase(self):
        # From the README chapter to the LC29H.
        self.assertEqual(sentence("PQTMVERNO"), "$PQTMVERNO*58")
        self.assertTrue(self.check("$PQTMVERNO*58"))


class TestParseGga(unittest.TestCase):
    def parse(self, line):
        return gnss.GNSSHandler.parse_gga(_Dummy(), line)

    def test_full_sentence(self):
        fix = self.parse(GGA_RTK)
        self.assertEqual(fix["qual"], 5)
        self.assertEqual(fix["fix_status_text"], "RTK_FLOAT")
        self.assertEqual(fix["sats"], 31)
        self.assertAlmostEqual(fix["hdop"], 0.57)
        self.assertAlmostEqual(fix["alt"], 123.456)
        self.assertAlmostEqual(fix["altitude_msl_m"], 123.456)
        self.assertAlmostEqual(fix["geoid_sep_m"], 12.3)
        self.assertAlmostEqual(fix["altitude_ellipsoid_m"], 135.756)
        self.assertAlmostEqual(fix["lat"], 0.12346, places=5)
        self.assertAlmostEqual(fix["lon"], -0.65432, places=5)
        self.assertEqual(fix["correction_age_sec"], 2.0)
        self.assertEqual(fix["station_id"], "0054")

    def test_empty_correction_fields_are_unknown(self):
        fix = self.parse(GGA_WIKI)
        self.assertIsNone(fix["correction_age_sec"])
        self.assertIsNone(fix["station_id"])

    def test_missing_hdop_and_altitude_are_unknown(self):
        fix = self.parse("$GNGGA,123519,4807.038,N,01131.000,E,4,08,,,M,,M,,*00")
        self.assertIsNone(fix["hdop"])
        self.assertIsNone(fix["alt"])

    def test_rtk_fixed_is_detected(self):
        fix = self.parse(GGA_RTK.replace(",W,5,31,", ",W,4,31,"))
        self.assertEqual(fix["qual"], 4)
        self.assertEqual(fix["fix_status_text"], "RTK_FIXED")

    def test_too_few_fields(self):
        self.assertIsNone(self.parse("$GNGGA,1,2,3"))

    def test_unknown_fixed_quality(self):
        fix = self.parse(GGA_RTK.replace(",W,5,31,", ",W,9,31,"))
        self.assertEqual(fix["fix_status_text"], "UNKNOWN")

    def test_all_fixed_levels_have_a_name(self):
        for qual, name in cfg.FIX_STATUS.items():
            self.assertTrue(name)
            self.assertIsInstance(qual, int)

    def test_exact_integer_fields(self):
        fix = self.parse(GGA_RTK)
        self.assertEqual(fix["lat_e8"], 12345600)
        self.assertEqual(fix["lon_e8"], -65432100)
        self.assertEqual(fix["alt_mm"], 123456)
        self.assertEqual(fix["geoid_sep_mm"], 12300)
        for key in ("lat_e8", "lon_e8", "alt_mm", "geoid_sep_mm"):
            self.assertIs(type(fix[key]), int, key)
        # The float fields stay as they were.
        self.assertAlmostEqual(fix["lat"], 0.12346, places=5)
        self.assertAlmostEqual(fix["alt"], 123.456)

    def test_integer_fields_are_none_without_fix(self):
        fix = self.parse(GGA_NOFIX)
        self.assertIsNone(fix["lat_e8"])
        self.assertIsNone(fix["lon_e8"])
        self.assertIsNone(fix["alt_mm"])
        self.assertIsNone(fix["geoid_sep_mm"])

    def test_missing_altitude_fields_stay_none(self):
        line = sentence("GNGGA,174608.000,0007.407360,N,00039.259260,W,4,31,0.57,,M,,M,2.0,0054")
        fix = self.parse(line)
        self.assertEqual(fix["lat_e8"], 12345600)
        self.assertIsNone(fix["alt_mm"])
        self.assertIsNone(fix["geoid_sep_mm"])
        self.assertIsNone(fix["altitude_ellipsoid_m"])

    def test_heights_are_parsed_once_and_the_floats_follow_the_millimetres(self):
        floats = []

        def spy_float(value):
            floats.append(value)
            return float(value)

        with mock.patch.object(geo, "parse_decimal_fixed",
                               wraps=geo.parse_decimal_fixed) as spy, \
                mock.patch.object(gnss, "float", spy_float, create=True):
            fix = self.parse(GGA_RTK)
        # One exact parse each for field 9 and field 11, and neither height goes
        # through float() again: the float fields come from the millimetres.
        self.assertEqual([call.args[0] for call in spy.call_args_list], ["123.456", "12.3"])
        self.assertNotIn("123.456", floats)
        self.assertNotIn("12.3", floats)
        self.assertEqual(fix["alt"], fix["alt_mm"] / 1000)
        self.assertEqual(fix["altitude_msl_m"], fix["alt"])
        self.assertEqual(fix["geoid_sep_m"], fix["geoid_sep_mm"] / 1000)
        self.assertEqual(fix["altitude_ellipsoid_m"],
                         (fix["alt_mm"] + fix["geoid_sep_mm"]) / 1000)

    def test_coordinates_never_go_through_float(self):
        floats = []

        def spy_float(value):
            floats.append(value)
            return float(value)

        with mock.patch.object(gnss, "float", spy_float, create=True):
            fix = self.parse(GGA_RTK)
        self.assertNotIn("0007.407360", floats)
        self.assertNotIn("00039.259260", floats)
        self.assertEqual(fix["lat"], geo.e8_to_deg(fix["lat_e8"]))

    def test_height_with_extra_decimals_is_rounded_to_the_millimetre(self):
        line = sentence("GNGGA,174608.000,0007.407360,N,00039.259260,W,4,31,0.57,"
                        "123.4567,M,-12.3004,M,2.0,0054")
        fix = self.parse(line)
        self.assertEqual(fix["alt_mm"], 123457)
        self.assertEqual(fix["geoid_sep_mm"], -12300)
        self.assertAlmostEqual(fix["alt"], 123.457)
        self.assertAlmostEqual(fix["geoid_sep_m"], -12.3)
        self.assertAlmostEqual(fix["altitude_ellipsoid_m"], 111.157)

    def gga(self, lat=("0007.407360", "N"), lon=("00039.259260", "W"),
            alt="123.456", geoid="12.3"):
        return sentence("GNGGA,174608.000,%s,%s,%s,%s,4,31,0.57,%s,M,%s,M,2.0,0054" % (
            lat[0], lat[1], lon[0], lon[1], alt, geoid))

    def assertFloatsFollowIntegers(self, fix, context=None):
        """No float without its integer, and every float derived from it exactly."""
        for float_key, exact_key in (("lat", "lat_e8"), ("lon", "lon_e8"), ("alt", "alt_mm"),
                                     ("altitude_msl_m", "alt_mm"),
                                     ("geoid_sep_m", "geoid_sep_mm")):
            self.assertEqual(fix[float_key] is None, fix[exact_key] is None,
                             (float_key, context))
        for key, exact in (("lat", "lat_e8"), ("lon", "lon_e8")):
            if fix[exact] is not None:
                self.assertEqual(fix[key], geo.e8_to_deg(fix[exact]), (key, context))
        for key, exact in (("alt", "alt_mm"), ("altitude_msl_m", "alt_mm"),
                           ("geoid_sep_m", "geoid_sep_mm")):
            if fix[exact] is not None:
                self.assertEqual(fix[key], fix[exact] / 1000, (key, context))
        if fix["alt_mm"] is None or fix["geoid_sep_mm"] is None:
            self.assertIsNone(fix["altitude_ellipsoid_m"], context)
        else:
            self.assertEqual(fix["altitude_ellipsoid_m"],
                             (fix["alt_mm"] + fix["geoid_sep_mm"]) / 1000, context)

    def test_height_the_exact_parser_refuses_has_no_float(self):
        # float() would read "1e2"; the exact parser does not, so the height is
        # missing (both forms) and the position and the geoid stay.
        for alt in ("1e2", " 12.3"):
            with self.subTest(alt=alt):
                fix = self.parse(self.gga(lat=("0007.407360", "N"), alt=alt))
                self.assertIsNotNone(fix)
                for key in ("alt", "altitude_msl_m", "alt_mm", "altitude_ellipsoid_m"):
                    self.assertIsNone(fix[key], key)
                self.assertEqual(fix["geoid_sep_mm"], 12300)
                self.assertEqual(fix["lat_e8"], 12345600)

    def test_geoid_the_exact_parser_refuses_has_no_float(self):
        for geoid in ("1e2", " 12.3"):
            with self.subTest(geoid=geoid):
                fix = self.parse(self.gga(geoid=geoid))
                self.assertIsNotNone(fix)
                self.assertEqual(fix["alt_mm"], 123456)
                self.assertEqual(fix["alt"], 123.456)
                for key in ("geoid_sep_mm", "geoid_sep_m", "altitude_ellipsoid_m"):
                    self.assertIsNone(fix[key], key)

    def test_garbage_height_leaves_the_position_without_height(self):
        from state import app
        errors = app.stats["gga_parse_errors"]
        fix = self.parse(self.gga(alt="abc", geoid="12.3"))
        self.assertIsNotNone(fix)
        self.assertEqual(fix["lat_e8"], 12345600)
        self.assertIsNone(fix["alt"])
        self.assertIsNone(fix["alt_mm"])
        fix = self.parse(self.gga(alt="123.4", geoid="x"))
        self.assertIsNotNone(fix)
        self.assertEqual(fix["alt_mm"], 123400)
        for key in ("geoid_sep_m", "geoid_sep_mm", "altitude_ellipsoid_m"):
            self.assertIsNone(fix[key], key)
        self.assertEqual(app.stats["gga_parse_errors"], errors)

    def test_coordinates_the_exact_parser_refuses_have_no_float(self):
        for lat in (("4012.3456", ""), ("4072.0", "N"), ("-4012.3456", "N"),
                    ("4012.3456 ", "N"), ("1e3", "N")):
            with self.subTest(lat=lat):
                fix = self.parse(self.gga(lat=lat))
                self.assertIsNotNone(fix)
                self.assertIsNone(fix["lat"])
                self.assertIsNone(fix["lat_e8"])
                self.assertEqual(fix["lon_e8"], -65432100)
        for lon in (("01000.0000", ""), ("00072.0", "E"), ("1e3", "E")):
            with self.subTest(lon=lon):
                fix = self.parse(self.gga(lon=lon))
                self.assertIsNotNone(fix)
                self.assertIsNone(fix["lon"])
                self.assertIsNone(fix["lon_e8"])
                self.assertEqual(fix["lat_e8"], 12345600)

    def test_every_float_follows_its_integer(self):
        lines = [GGA_RTK, GGA_NOFIX, GGA_WIKI,
                 self.gga(alt="1e2"), self.gga(alt=" 12.3"), self.gga(geoid="1e2"),
                 self.gga(geoid=" 12.3"), self.gga(alt="abc"), self.gga(geoid="x"),
                 self.gga(alt="", geoid=""), self.gga(alt="-20000.001", geoid="12.34567")]
        lines += [self.gga(lat=lat) for lat in (("4012.3456", ""), ("4072.0", "N"),
                                                ("-4012.3456", "N"), ("4012.3456 ", "N"),
                                                ("1e3", "N"))]
        lines += [self.gga(lon=lon) for lon in (("01000.0000", ""), ("00072.0", "E"),
                                                ("1e3", "E"))]
        for line in lines:
            with self.subTest(line=line):
                fix = self.parse(line)
                self.assertIsNotNone(fix)
                self.assertFloatsFollowIntegers(fix, line)

    def test_unicode_digits_in_a_coordinate_give_no_position(self):
        line = sentence("GNGGA,174608.000,\u0660\u0660\u0660\u0667.407360,N,"
                        "00039.259260,W,4,31,0.57,123.456,M,12.3,M,2.0,0054")
        fix = self.parse(line)
        self.assertIsNone(fix["lat_e8"])
        self.assertEqual(fix["lon_e8"], -65432100)

    def test_integer_fields_never_go_through_float(self):
        with mock.patch.object(geo, "float", side_effect=AssertionError("float used"),
                               create=True):
            fix = self.parse(GGA_RTK)
        self.assertEqual(fix["lat_e8"], 12345600)

    def test_exact_fields_survive_a_float32_parse_chain(self):
        # On the device every float() in gnss.py is 32 bits wide. Emulate that
        # here, and keep geo.py off float() altogether: an e8 value derived from
        # the float latitude would be off by more than a hundred units (over
        # 10 cm) instead of the exact integer.
        float_original = float

        def float32(text):
            return struct.unpack("f", struct.pack("f", float_original(text)))[0]

        line = sentence("GNGGA,174608.000,5107.407360,N,01000.000000,E,4,31,0.57,"
                        "123.456,M,12.3,M,2.0,0054")
        with mock.patch.object(geo, "float", side_effect=AssertionError("float used"),
                               create=True), \
                mock.patch.object(gnss, "float", new=float32, create=True):
            fix = self.parse(line)
        # The premise, independent of the fix: a 32-bit float has lost about a
        # tenth of a metre (more than 5e-7 degrees) of this latitude.
        self.assertGreater(abs(float32("51.123456") - 51.123456), 5e-7)
        self.assertEqual(fix["lat_e8"], 5112345600)
        self.assertEqual(fix["lon_e8"], 1000000000)
        self.assertEqual(fix["lat"], geo.e8_to_deg(fix["lat_e8"]))

    def test_parse_cost_is_published_in_stats(self):
        self.parse(GGA_RTK)
        from state import app
        self.assertIsInstance(app.stats["gga_parse_us"], int)
        self.assertGreaterEqual(app.stats["gga_parse_us_max"], app.stats["gga_parse_us"])

    def test_parse_cost_tracks_last_and_maximum(self):
        from state import app
        with mock.patch.dict(app.stats, {"gga_parse_us": 0, "gga_parse_us_max": 0}), \
                mock.patch.object(gnss, "_ticks_us", side_effect=[1000, 1042, 2000, 2010]):
            self.parse(GGA_RTK)
            self.assertEqual((app.stats["gga_parse_us"], app.stats["gga_parse_us_max"]), (42, 42))
            self.parse(GGA_RTK)
            self.assertEqual((app.stats["gga_parse_us"], app.stats["gga_parse_us_max"]), (10, 42))

    def test_parse_cost_statistics_start_at_zero(self):
        from state import AppState
        stats = AppState().stats
        for key in ("gga_parse_us", "gga_parse_us_max", "gga_parse_us_avg",
                    "gga_parse_over_budget"):
            self.assertEqual(stats[key], 0, key)

    def test_parse_cost_average_and_budget_counter(self):
        from state import app
        fresh = {"gga_parse_us": 0, "gga_parse_us_max": 0, "gga_parse_us_avg": 0,
                 "gga_parse_over_budget": 0}
        # Every parse reads the clock twice: elapsed 42, 10, 3000, 2000 and 2001 us.
        ticks = [1000, 1042, 2000, 2010, 3000, 6000, 7000, 9000, 10000, 12001]
        expected = [(42, 0), (40, 0), (225, 1),
                    # 2000 us is within the budget, 2001 us is the first one over it.
                    (335, 1), (439, 2)]
        with mock.patch.dict(app.stats, fresh), \
                mock.patch.object(gnss, "_ticks_us", side_effect=ticks):
            for average, over_budget in expected:
                self.parse(GGA_RTK)
                self.assertEqual(
                    (app.stats["gga_parse_us_avg"], app.stats["gga_parse_over_budget"]),
                    (average, over_budget))
            self.assertEqual(app.stats["gga_parse_us_max"], 3000)
            self.assertEqual(app.stats["gga_parse_us"], 2001)


class TestParseGst(unittest.TestCase):
    def test_complete_receiver_error_estimate(self):
        gst = gnss.GNSSHandler.parse_gst(
            "$GNGST,174608.000,0.021,0.018,0.012,34.5,0.011,0.015,0.030*00")
        self.assertEqual(gst["source"], "NMEA_GST")
        self.assertEqual(gst["utc"], "174608.000")
        self.assertAlmostEqual(gst["semi_major_sigma_m"], 0.018)
        self.assertAlmostEqual(gst["horizontal_sigma_m"],
                               (0.011 ** 2 + 0.015 ** 2) ** 0.5)
        self.assertAlmostEqual(gst["altitude_sigma_m"], 0.030)

    def test_blank_optional_values_are_unknown(self):
        gst = gnss.GNSSHandler.parse_gst("$GNGST,174608.000,,,,,,,*00")
        self.assertIsNone(gst["semi_major_sigma_m"])

    def test_zero_sigmas_are_treated_as_missing(self):
        gst = gnss.GNSSHandler.parse_gst(
            "$GNGST,174608.000,0.1,0.1,0.1,0,0,0,0*00")
        self.assertIsNone(gst["horizontal_sigma_m"])
        self.assertIsNone(gst["altitude_sigma_m"])

    def test_malformed_or_negative_estimate_is_rejected(self):
        self.assertIsNone(gnss.GNSSHandler.parse_gst("$GNGST,174608.000,x*00"))
        self.assertIsNone(gnss.GNSSHandler.parse_gst(
            "$GNGST,174608.000,0.1,-0.1,0.1,0,0.1,0.1,0.1*00"))


class TestWhitelist(unittest.TestCase):
    def test_expected_sentence_types(self):
        for typ in ("GGA", "RMC", "VTG", "GSA", "GSV", "GST"):
            self.assertIn(typ, cfg.NMEA_WHITELIST)

    def test_type_is_read_from_position_3_to_6(self):
        # So does GNSSHandler.run(); the Talker prefix is two characters.
        self.assertEqual(GGA_RTK[3:6], "GGA")
        self.assertEqual("$GNRMC,1"[3:6], "RMC")
        self.assertEqual(GGA_WIKI[3:6], "GGA")


if __name__ == "__main__":
    unittest.main()


class TestKeinNullIsland(unittest.TestCase):
    """Without a fix, no position must be indicated. An empty coordinate field once came out as 0.0 - i.e. exactly in the case of 'no fix' - and status.py, which checks on 'is not None', showed 0.0000000/0.0000000 as a real position. An empty or unreadable coordinate field must never appear as 0.0/0.0.
    """

    def test_empty_field_is_not_a_coordinate(self):
        fix = gnss.GNSSHandler.parse_gga(
            _Dummy(), sentence("GNGGA,174608.000,,N,00039.259260,W,4,31,0.57,123.456,M,12.3,M,2.0,0054"))
        self.assertIsNone(fix["lat"])
        self.assertIsNone(fix["lat_e8"])
        self.assertEqual(fix["lon_e8"], -65432100)

    def test_gga_without_a_fix_has_no_position(self):
        fix = gnss.GNSSHandler.parse_gga(_Dummy(), GGA_NOFIX)
        self.assertIsNotNone(fix)
        self.assertEqual(fix["qual"], 0)
        self.assertIsNone(fix["lat"])
        self.assertIsNone(fix["lon"])

    def test_gga_with_fix_continues_to_have_a_position(self):
        fix = gnss.GNSSHandler.parse_gga(_Dummy(), GGA_RTK)
        self.assertIsNotNone(fix["lat"])
        self.assertIsNotNone(fix["lon"])

    def test_quality_zero_also_discards_filled_fields(self):
        # Some recipients forward the last known position, even though the quality is 0,
        # which is not trustworthy.
        sentence = GGA_RTK.replace(",W,5,31,", ",W,0,31,")
        fix = gnss.GNSSHandler.parse_gga(_Dummy(), sentence)
        self.assertIsNone(fix["lat"])
        self.assertIsNone(fix["lon"])
        self.assertIsNone(fix["lat_e8"])
        self.assertIsNone(fix["lon_e8"])
        # The heights are not position data: they stay populated, like the float alt.
        self.assertEqual(fix["alt_mm"], 123456)
        self.assertEqual(fix["geoid_sep_mm"], 12300)

    def test_garbage_is_left_without_position(self):
        fix = gnss.GNSSHandler.parse_gga(
            _Dummy(), sentence("GNGGA,174608.000,abc,N,00039.259260,W,4,31,0.57,123.456,M,12.3,M,2.0,0054"))
        self.assertIsNone(fix["lat"])
        self.assertIsNone(fix["lat_e8"])
        self.assertEqual(fix["lon_e8"], -65432100)


class TestRawSentenceForGga(unittest.TestCase):
    """The NTRIP client must be able to pass on the unmodified GGA set - VRS mountpoints demand it."""

    def test_parse_gga_holds_the_raw_set(self):
        fix = gnss.GNSSHandler.parse_gga(_Dummy(), GGA_RTK)
        self.assertEqual(fix["raw"], GGA_RTK)
