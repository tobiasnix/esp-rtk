# SPDX-License-Identifier: AGPL-3.0-only
"""The acceptance gate judges the exact lat_e8/lon_e8/alt_mm fields (#40)."""
import itertools
import time
import unittest
from unittest import mock

from support import exact_fix, gnss, sentence, state
import tracking
from tracking import quality_check

ACCEPTED = {"utc": "120000.00", "lat": 50.0, "lon": 8.0, "alt": 123.4,
            "lat_e8": 5000000000, "lon_e8": 800000000, "alt_mm": 123400,
            "qual": 4, "fix_status_text": "RTK_FIXED", "sats": 24, "hdop": 0.6,
            "correction_age_sec": 0.5, "station_id": "0054",
            "receiver_accuracy": {"source": "NMEA_GST", "horizontal_sigma_m": 0.01,
                                  "altitude_sigma_m": 0.02}}


class _Dummy:
    """parse_gga is an instance method but does not use self."""


class TestExactFixHelper(unittest.TestCase):
    def test_exact_fix_mirrors_the_float_fields(self):
        fix = {"lat": 50.0, "lon": 8.0, "alt": 100.0, "utc": "1"}
        self.assertEqual(exact_fix(fix), {
            "lat": 50.0, "lon": 8.0, "alt": 100.0, "utc": "1",
            "lat_e8": 5000000000, "lon_e8": 800000000, "alt_mm": 100000})
        self.assertEqual(fix, {"lat": 50.0, "lon": 8.0, "alt": 100.0, "utc": "1"})
        stale = {"lat": 50.000001, "lon": 8.0, "alt": 100.0, "lat_e8": 1}
        self.assertEqual(exact_fix(stale)["lat_e8"], 5000000100)
        bare = exact_fix({"lat": None, "lon": 8.0})
        self.assertIsNone(bare["lat_e8"])
        self.assertIsNone(bare["alt_mm"])


class TestGateRequiresExactFields(unittest.TestCase):
    def setUp(self):
        self.assertTrue(quality_check(ACCEPTED)["accepted"])

    def reasons(self, **changes):
        return quality_check(dict(ACCEPTED, **changes))["reasons"]

    def test_float_position_without_integers_is_no_position(self):
        for key in ("lat_e8", "lon_e8"):
            with self.subTest(key=key):
                verdict = quality_check(dict(ACCEPTED, **{key: None}))
                self.assertFalse(verdict["accepted"])
                self.assertEqual(verdict["reasons"], ["no_position"])

    def test_float_height_without_millimetres_is_altitude_required(self):
        self.assertEqual(self.reasons(alt_mm=None), ["altitude_required"])

    def test_floats_and_bools_are_not_exact(self):
        self.assertEqual(self.reasons(lat_e8=50.0), ["no_position"])
        self.assertEqual(self.reasons(lon_e8=True), ["no_position"])
        self.assertEqual(self.reasons(alt_mm=123400.0), ["altitude_required"])
        self.assertEqual(self.reasons(alt_mm=False), ["altitude_required"])

    def test_values_outside_the_storage_bounds_are_refused(self):
        self.assertEqual(self.reasons(lat_e8=9000000001), ["no_position"])
        self.assertEqual(self.reasons(lon_e8=-18000000001), ["no_position"])
        self.assertEqual(self.reasons(alt_mm=100000001), ["altitude_required"])
        self.assertEqual(self.reasons(alt_mm=-20000001), ["altitude_required"])
        for limit in ({"lat_e8": -9000000000}, {"lon_e8": 18000000000},
                      {"alt_mm": 100000000}, {"alt_mm": -20000000}):
            with self.subTest(limit=limit):
                self.assertTrue(quality_check(dict(ACCEPTED, **limit))["accepted"])

    def test_float_fields_do_not_decide(self):
        self.assertTrue(quality_check(dict(ACCEPTED, lat=None, lon=None, alt=None))["accepted"])

    def test_no_fix_names_both_reasons(self):
        reasons = quality_check(None)["reasons"]
        self.assertIn("no_position", reasons)
        self.assertIn("altitude_required", reasons)


class TestGateAgreesWithStorage(unittest.TestCase):
    BODY = "GNGGA,120000.00,%s,%s,%s,%s,4,31,0.57,%s,M,%s,M,%s,0054"

    def parse(self, lat, lon, alt, age, geoid="12.3"):
        fix = gnss.GNSSHandler.parse_gga(
            _Dummy(), sentence(self.BODY % (lat[0], lat[1], lon[0], lon[1], alt, geoid, age)))
        if fix is not None:
            fix["receiver_accuracy"] = dict(ACCEPTED["receiver_accuracy"])
        return fix

    def test_gate_accepts_exactly_what_storage_accepts(self):
        lats = (("5107.407360", "N"), ("9000.000000", "N"), ("9000.000001", "N"),
                ("0000.000000", "S"), ("4072.0", "N"), ("-4012.3456", "N"),
                ("4012.3456 ", "N"), ("1e3", "N"), ("4012.3456", ""),
                ("\u0660\u0660\u0660\u0667.407360", "N"), ("", "N"))
        lons = (("01000.000000", "E"), ("18000.000000", "W"), ("18000.000001", "E"),
                ("00039.259260", "W"), ("1e3", "E"))
        alts = ("123.456", "-20000.000", "100000.000", "100000.001", "-20000.001",
                "1e2", " 12.3", "abc", "", "12.34567")
        accepted_count = refused_count = 0
        expected = None
        for lat, lon, alt, age in itertools.product(lats, lons, alts, ("2.0", "")):
            fix = self.parse(lat, lon, alt, age)
            self.assertIsNotNone(fix, (lat, lon, alt, age))
            try:
                tracking._validate_coordinate([fix["lon_e8"], fix["lat_e8"], fix["alt_mm"]])
                storable = True
            except ValueError:
                storable = False
            accepted = quality_check(fix)["accepted"]
            self.assertEqual(accepted, storable, (lat, lon, alt, age))
            accepted_count += accepted
            refused_count += not accepted
            if (lat, lon, alt, age) == (lats[0], lons[0], "123.456", ""):
                expected = accepted
        self.assertGreater(accepted_count, 0)
        self.assertGreater(refused_count, 0)
        self.assertTrue(expected)      # an empty field 13 passes the gate (ruling)

    def test_unreadable_geoid_alone_still_passes_the_gate(self):
        # Only field 9 decides; an unreadable field 11 leaves the geoid empty.
        for geoid in ("1e2", " 12.3"):
            with self.subTest(geoid=geoid):
                fix = self.parse(("5107.407360", "N"), ("01000.000000", "E"), "123.456", "2.0", geoid)
                self.assertIsNone(fix["geoid_sep_mm"])
                self.assertTrue(quality_check(fix)["accepted"])

    def test_the_issue_table_rows_are_refused(self):
        good_lat, good_lon = ("5107.407360", "N"), ("01000.000000", "E")
        rows = (
            ("no_position", ("4012.3456", ""), good_lon, "123.456"),
            ("no_position", ("4072.0", "N"), good_lon, "123.456"),
            ("no_position", ("-4012.3456", "N"), good_lon, "123.456"),
            ("no_position", ("4012.3456 ", "N"), good_lon, "123.456"),
            ("no_position", ("1e3", "N"), good_lon, "123.456"),
            ("altitude_required", good_lat, good_lon, "1e2"),
            ("altitude_required", good_lat, good_lon, " 12.3"),
        )
        for reason, lat, lon, alt in rows:
            with self.subTest(reason=reason, lat=lat, alt=alt):
                fix = self.parse(lat, lon, alt, "2.0")
                self.assertIsNotNone(fix)
                verdict = quality_check(fix)
                self.assertFalse(verdict["accepted"])
                self.assertIn(reason, verdict["reasons"])


class TestLiveVerdict(unittest.TestCase):
    def setUp(self):
        self.app = state.AppState()
        self.app.stats["gst_output_state"] = "receiving"
        self.now = 100_000
        patcher = mock.patch.object(time, "ticks_ms", side_effect=lambda: self.now)
        patcher.start(); self.addCleanup(patcher.stop)
        tracker = mock.patch("tracking.tracker"); self.tracker = tracker.start(); self.addCleanup(tracker.stop)
        self.tracker.enqueue_fix.return_value = False

    def test_live_verdict_names_the_missing_exact_field(self):
        self.app.update_fix(dict(ACCEPTED, lat_e8=None))
        self.assertFalse(self.app.gate_state()["accepted"])
        self.assertEqual(self.app.gate_state()["quality"]["reasons"], ["no_position"])
        self.assertFalse(self.app.quality_streaks()["gate_accepted"])

    def test_an_inexact_bare_gga_is_not_held(self):
        self.app.update_fix(dict(ACCEPTED))
        self.assertTrue(self.app.gate_state()["accepted"])
        self.now += 1000
        bare = {key: value for key, value in ACCEPTED.items() if key != "receiver_accuracy"}
        self.app.update_fix(dict(bare, utc="120001.00", lat_e8=None))
        gate = self.app.gate_state()
        self.assertFalse(gate["accepted"])
        self.assertFalse(gate["pending_gst"])
        self.assertIn("no_position", gate["quality"]["reasons"])


class TestAveragedFallback(unittest.TestCase):
    def test_inexact_samples_never_reach_the_gate_as_a_mean(self):
        for key in ("lat_e8", "alt_mm"):
            with self.subTest(key=key):
                with self.assertRaisesRegex(ValueError, "no exact position"):
                    tracking.average_fixes([dict(ACCEPTED), dict(ACCEPTED, **{key: None})])


if __name__ == "__main__":
    unittest.main()
