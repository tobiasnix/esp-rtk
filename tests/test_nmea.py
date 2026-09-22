# SPDX-License-Identifier: AGPL-3.0-only
"""NMEA processing: checksum, coordinates, and GGA parsing."""
import unittest

from support import cfg, gnss, sentence

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


class TestKoordinaten(unittest.TestCase):
    def deg(self, coord, hemi):
        return gnss.GNSSHandler.nmea_to_deg(coord, hemi)

    def test_nord(self):
        # 4807.038 = 48 Grad 07,038 Minuten = 48,1173 Grad
        self.assertAlmostEqual(self.deg("4807.038", "N"), 48.1173, places=4)

    def test_south_is_negative(self):
        self.assertAlmostEqual(self.deg("4807.038", "S"), -48.1173, places=4)

    def test_ost(self):
        self.assertAlmostEqual(self.deg("01131.000", "E"), 11.51667, places=4)

    def test_west_is_negative(self):
        self.assertAlmostEqual(self.deg("00039.259260", "W"), -0.65432, places=4)

    def test_synthetic_position(self):
        self.assertAlmostEqual(self.deg("0007.407360", "N"), 0.12346, places=5)
        self.assertAlmostEqual(self.deg("00039.259260", "W"), -0.65432, places=5)

    def test_waste_does_not_become_an_exception(self):
        self.assertIsNone(self.deg("abc", "N"))


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
    """Without a fix, no position must be indicated. nmea_to_deg delivered 0.0 for an empty coordinate field - i.e. exactly in the case of 'no fix'. status.py checks on 'is not None', which was always true, and showed 0.0000000/0.0000000 as a real position.
    """

    def test_empty_field_is_not_a_coordinate(self):
        self.assertIsNone(gnss.GNSSHandler.nmea_to_deg("", "N"))

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

    def test_garbage_is_left_without_position(self):
        self.assertIsNone(gnss.GNSSHandler.nmea_to_deg("abc", "N"))


class TestRawSentenceForGga(unittest.TestCase):
    """The NTRIP client must be able to pass on the unmodified GGA set - VRS mountpoints demand it."""

    def test_parse_gga_holds_the_raw_set(self):
        fix = gnss.GNSSHandler.parse_gga(_Dummy(), GGA_RTK)
        self.assertEqual(fix["raw"], GGA_RTK)
