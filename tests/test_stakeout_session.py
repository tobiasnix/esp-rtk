# SPDX-License-Identifier: AGPL-3.0-only
"""Stakeout session: target selection, guidance states, motion."""
import builtins
import inspect
import json
import os
import re
import unittest
from unittest import mock

import support  # noqa: F401  (installs the MicroPython stubs)
import cfg_logging
import geo
import state
import stakeout_session
import tracking
from cfg import CONFIG, FIX_STATUS
from wrap_clock import WrapClock

PROJECT = "p-0001"
FINGERPRINT = 1760000000
P2 = {"tid": "so-3a4739b9/2/1", "set_id": "so-3a4739b9", "part_id": "so-3a4739b9/2", "seq": 1,
      "name": "Point 2", "lat_e8": 5100000027, "lon_e8": 999999943, "alt_import": 412300}
P1 = dict(P2, tid="so-3a4739b9/1/1", part_id="so-3a4739b9/1", name="Point 1")
# Metres per degree at 51 degrees (geo.local_scale), for building rover offsets.
M_PER_DEG_LAT, M_PER_DEG_LON = geo.local_scale(51.0)


def north_e8(metres):
    return int(round(metres / M_PER_DEG_LAT * geo.COORD_SCALE))


def east_e8(metres):
    return int(round(metres / M_PER_DEG_LON * geo.COORD_SCALE))


def fix_at(lat_e8, lon_e8, utc="120000.00", qual=4, gst=True):
    fix = {"utc": utc, "lat_e8": lat_e8, "lon_e8": lon_e8, "alt_mm": 412300, "alt": 412.3,
           "qual": qual, "fix_status_text": FIX_STATUS.get(qual, "UNKNOWN"), "sats": 20,
           "hdop": 0.6, "correction_age_sec": 1.0}
    if isinstance(lat_e8, int) and isinstance(lon_e8, int):
        fix["lat"] = lat_e8 / geo.COORD_SCALE
        fix["lon"] = lon_e8 / geo.COORD_SCALE
    if gst:
        fix["receiver_accuracy"] = {"horizontal_sigma_m": 0.01, "altitude_sigma_m": 0.02}
    return fix


def judged(fix):
    return fix, tracking.quality_check(fix), False


class SessionCase(unittest.TestCase):
    """A fresh session, a WrapClock, saved CONFIG values and the default
    status hook (every target open; web replaces it) for every test."""

    def setUp(self):
        self.clock = WrapClock()
        for patcher in (mock.patch.object(stakeout_session, "time", self.clock),
                        mock.patch.object(stakeout_session, "status_lookup",
                                          stakeout_session.open_status)):
            patcher.start()
            self.addCleanup(patcher.stop)
        config = mock.patch.dict(CONFIG, {"stakeout_tolerance_m": 0.1,
                                          "stakeout_fine_radius_m": 5.0,
                                          "measurement_quality_window_samples": 5})
        config.start()
        self.addCleanup(config.stop)
        self.session = stakeout_session.StakeoutSession()
        self.seq = 0
        self.rev = 0
        self.published_ms = None

    def push(self, session, fix, now_ms, pending=False, enrich=False, published_ms=None):
        """Call on_fix the way AppState does: a new publication or its enrichment."""
        self.clock.advance(self.clock.ticks_diff(now_ms, self.clock.ticks_ms()))
        if enrich:
            self.rev += 1
            if published_ms is None:
                published_ms = self.published_ms
        else:
            self.seq += 1
            published_ms = now_ms
            self.published_ms = now_ms
        session.on_fix(fix, tracking.quality_check(fix), pending, published_ms,
                       self.seq, self.rev)

    def view(self, session, now_ms, fresh=True):
        return session.guidance(now_ms, fresh, self.seq, self.rev)

    def select(self, target=P2):
        return self.session.select(PROJECT, FINGERPRINT, dict(target))


class TestGeometry(SessionCase):
    def test_without_target_nothing_is_kept(self):
        self.push(self.session, fix_at(5100000000, 1000000000), 1000)
        view = self.view(self.session, 1000)
        self.assertEqual(view["state"], "no_target")
        self.assertEqual(view["window"]["have"], 0)
        self.assertIsNone(view["dist_m"])
        self.assertFalse(self.session.motion()["valid"])
        self.assertEqual(self.session._window, [])
        self.assertIsNone(self.session.target)

    def test_spec_vector_p2_signs_and_bearing(self):
        self.select()
        self.push(self.session, fix_at(5100000000, 1000000000), 1000)
        view = self.view(self.session, 1000)
        self.assertEqual(view["dn_m"], 0.030)
        self.assertEqual(view["de_m"], -0.040)
        self.assertEqual(view["dist_m"], 0.050)
        self.assertLess(abs(view["bearing_deg"] - 306.87), 0.1)

    def rover_view(self, north_m, east_m, now_ms):
        self.push(self.session, fix_at(P2["lat_e8"] - north_e8(north_m),
                                       P2["lon_e8"] - east_e8(east_m)), now_ms)
        return self.view(self.session, now_ms)

    def test_state_by_distance(self):
        self.select()
        view = self.rover_view(0.05, 0.0, 1000)
        self.assertNotIn(view["state"], ("coarse", "fine"))
        self.assertEqual(view["state"], "settling")
        view = self.rover_view(1.0, 0.0, 2000)
        self.assertEqual((view["state"], view["reasons"]), ("fine", ["outside_tolerance"]))
        self.assertEqual(view["window"]["have"], 0)
        view = self.rover_view(6.0, 0.0, 3000)
        self.assertEqual(view["state"], "coarse")
        self.assertEqual(view["dist_m"], 6.0)
        view = self.rover_view(60000.0, 0.0, 4000)
        self.assertEqual(view["state"], "far")
        for key in ("dn_m", "de_m", "dist_m", "bearing_deg"):
            self.assertIsNone(view[key], key)
        view = self.rover_view(16000.0, 12000.0, 5000)
        self.assertEqual(view["state"], "coarse")
        for key in ("dn_m", "de_m", "dist_m", "bearing_deg"):
            self.assertIsInstance(view[key], int, key)
        rover = (P2["lat_e8"] - north_e8(16000.0), P2["lon_e8"] - east_e8(12000.0))
        self.assertEqual(view["dist_m"], round(geo.distance_e8_m(*rover, P2["lat_e8"],
                                                                 P2["lon_e8"])))
        self.assertLess(abs(view["dist_m"] - 20000), 20)
        self.assertLess(abs(view["bearing_deg"] - 37), 2)
        view = self.rover_view(9000.0, 0.0, 6000)
        self.assertEqual(view["state"], "coarse")
        rover = (P2["lat_e8"] - north_e8(9000.0), P2["lon_e8"])
        self.assertEqual(view["dist_m"], round(geo.distance_e8_m(*rover, P2["lat_e8"],
                                                                 P2["lon_e8"]), 3))
        self.assertIsInstance(view["dist_m"], float)
        self.assertNotEqual(view["dist_m"], round(view["dist_m"]))

    def test_gate_rejection_is_fine_with_reason(self):
        self.select()
        self.push(self.session, fix_at(5100000000, 1000000000, qual=5), 1000)
        view = self.view(self.session, 1000)
        self.assertEqual((view["state"], view["reasons"]), ("fine", ["gate_rejected"]))
        self.assertEqual(view["dist_m"], 0.050)
        self.assertEqual(view["window"]["have"], 0)

    def test_no_position_cases(self):
        self.select()
        cases = (fix_at(5100000000, 1000000000, qual=0),
                 {key: value for key, value in fix_at(5100000000, 1000000000).items()
                  if key != "lat_e8"},
                 fix_at(51.0, 1000000000),
                 fix_at(True, 1000000000))
        for now_ms, fix in enumerate(cases, start=1):
            self.push(self.session, fix, now_ms * 1000)
            view = self.view(self.session, now_ms * 1000)
            self.assertEqual(view["state"], "no_position", fix)
            for key in ("dn_m", "de_m", "dist_m", "bearing_deg"):
                self.assertIsNone(view[key], key)
            self.assertEqual(view["window"]["have"], 0)
        self.push(self.session, fix_at(5100000000, 1000000000), 9000)
        self.assertEqual(self.view(self.session, 9000)["state"], "settling")
        stale = self.view(self.session, 12500, fresh=False)
        self.assertEqual(stale["state"], "no_position")
        for key in ("dn_m", "de_m", "dist_m", "bearing_deg"):
            self.assertIsNone(stale[key], key)
        self.assertEqual(stale["window"]["have"], 0)

    def test_awaiting_fix_after_select(self):
        self.select()
        view = self.view(self.session, 1000)
        self.assertEqual((view["state"], view["reasons"]), ("no_position", ["awaiting_fix"]))
        self.assertIsNone(view["dist_m"])
        self.assertEqual(view["window"], {"have": 0, "need": 5, "spread_m": None})

    def test_reselecting_the_same_target_keeps_state(self):
        self.assertTrue(self.select())
        self.push(self.session, fix_at(5100000000, 1000000000), 1000)
        self.assertFalse(self.select())
        self.assertEqual(self.view(self.session, 1000)["window"]["have"], 1)
        self.assertTrue(self.select(P1))
        self.assertEqual(self.session.target["tid"], P1["tid"])
        self.assertEqual(self.view(self.session, 1000)["reasons"], ["awaiting_fix"])
        self.session.clear()
        self.assertEqual(self.view(self.session, 1000)["state"], "no_target")
        self.assertIsNone(self.session.target)
        self.select()
        self.session.forget_set(PROJECT, "so-00000000")
        self.assertEqual(self.session.target["tid"], P2["tid"])
        self.session.forget_set("p-other", "so-3a4739b9")
        self.assertEqual(self.session.target["tid"], P2["tid"])
        self.session.forget_set(PROJECT, "so-3a4739b9")
        self.assertIsNone(self.session.target)
        self.assertEqual(self.view(self.session, 1000)["state"], "no_target")

    def test_select_none_drops_the_target_without_raising_the_epoch(self):
        self.assertFalse(self.session.select(PROJECT, FINGERPRINT, None))
        self.select()
        epoch = self.session.epoch
        self.assertTrue(self.session.select(PROJECT, FINGERPRINT, None))
        self.assertIsNone(self.session.target)
        self.assertEqual(self.session.epoch, epoch)
        self.assertEqual(self.view(self.session, 1000)["state"], "no_target")
        self.assertFalse(self.session.select(PROJECT, FINGERPRINT, None))

    def test_bearing_is_rounded_within_0_to_360(self):
        self.assertEqual(stakeout_session._degrees(359.97, 1), 0.0)
        self.assertEqual(stakeout_session._degrees(359.6, None), 0)
        self.assertIsInstance(stakeout_session._degrees(359.6, None), int)
        self.assertEqual(stakeout_session._degrees(306.8699, 1), 306.9)

    def test_select_keeps_project_and_fingerprint(self):
        self.select()
        self.assertEqual((self.session.project_id, self.session.fingerprint),
                         (PROJECT, FINGERPRINT))
        self.assertEqual(self.session.target, P2)
        self.assertTrue(self.session.select("p-0002", FINGERPRINT, dict(P2)))
        self.assertEqual(self.session.project_id, "p-0002")

    def east_walk(self, steps_m, gaps_ms, quals=None):
        now_ms = 1000
        lon_e8 = 1000000000
        quals = quals or [4] * (len(steps_m) + 1)
        self.push(self.session, fix_at(5100000000, lon_e8, utc="120000.00", qual=quals[0]),
                  now_ms)
        for index, (step_m, gap_ms) in enumerate(zip(steps_m, gaps_ms), start=1):
            now_ms += gap_ms
            lon_e8 += east_e8(step_m)
            self.push(self.session, fix_at(5100000000, lon_e8, utc="1200%02d.00" % index,
                                           qual=quals[index]), now_ms)
        return self.session.motion()

    def test_motion_needs_two_close_rtk_fixes_at_walking_speed(self):
        self.select()
        motion = self.east_walk([1.0], [1000])
        self.assertEqual(motion["speed_mps"], 1.0)
        self.assertLess(abs(motion["course_deg"] - 90), 0.1)
        self.assertIs(motion["valid"], True)
        self.select(P1)
        motion = self.east_walk([0.4], [1000])
        self.assertEqual(motion["speed_mps"], 0.4)
        self.assertIsNone(motion["course_deg"])
        self.assertIs(motion["valid"], False)
        self.select()
        motion = self.east_walk([1.0], [2600])
        self.assertIs(motion["valid"], False)
        self.assertIsNone(motion["course_deg"])
        self.select(P1)
        self.assertIs(self.east_walk([1.0], [1000], quals=[1, 4])["valid"], False)
        self.select()
        self.assertIs(self.east_walk([1.0], [1000], quals=[4, 1])["valid"], False)

    def test_motion_is_reset_by_target_change_and_lost_position(self):
        self.select()
        self.assertTrue(self.east_walk([1.0], [1000])["valid"])
        self.select(P1)
        self.assertEqual(self.session.motion(),
                         {"speed_mps": None, "course_deg": None, "valid": False})
        self.assertTrue(self.east_walk([1.0], [1000])["valid"])
        self.push(self.session, fix_at(5100000000, 1000000000, utc="120005.00", qual=0), 9000)
        self.assertFalse(self.session.motion()["valid"])

    def test_motion_is_stepped_by_epochs_not_by_gst(self):
        self.select()
        lon_e8 = 1000000000
        for epoch in range(5):
            now_ms = 1000 + 1000 * epoch
            utc = "1200%02d.00" % epoch
            self.push(self.session, fix_at(5100000000, lon_e8, utc=utc, gst=False), now_ms,
                      pending=True)
            self.push(self.session, fix_at(5100000000, lon_e8, utc=utc), now_ms + 400,
                      enrich=True)
            motion = self.session.motion()
            if epoch:
                self.assertIs(motion["valid"], True, epoch)
                self.assertEqual(motion["speed_mps"], 1.0, epoch)
                self.assertLess(abs(motion["course_deg"] - 90), 0.1)
            else:
                self.assertIs(motion["valid"], False)
            lon_e8 += east_e8(1.0)

    def test_repeated_publication_of_the_same_utc_is_no_motion_step(self):
        self.select()
        self.assertTrue(self.east_walk([1.0], [1000])["valid"])
        repeat = fix_at(5100000000, 1000000000 + east_e8(1.0), utc="120001.00")
        self.push(self.session, repeat, 2500)
        self.assertEqual(self.session.motion()["speed_mps"], 1.0)
        self.push(self.session, fix_at(5100000000, 1000000000 + east_e8(2.0), utc="120002.00"),
                  3000)
        self.assertEqual(self.session.motion()["speed_mps"], 1.0)

    def test_repeated_publication_that_loses_rtk_invalidates_motion(self):
        self.select()
        self.assertTrue(self.east_walk([1.0], [1000])["valid"])
        repeat = fix_at(5100000000, 1000000000 + east_e8(1.0), utc="120001.00", qual=2)
        self.push(self.session, repeat, 2500)
        self.assertEqual(self.session.motion(),
                         {"speed_mps": None, "course_deg": None, "valid": False})

    def test_clear_and_forget_raise_the_epoch(self):
        epoch = self.session.epoch
        self.select()
        self.select(P1)
        self.assertEqual(self.session.epoch, epoch)
        self.session.clear()
        self.assertEqual(self.session.epoch, epoch + 1)
        self.select()
        self.session.forget_set(PROJECT, "so-00000000")
        self.assertEqual(self.session.epoch, epoch + 1)
        self.session.forget_set(PROJECT, "so-3a4739b9")
        self.assertEqual(self.session.epoch, epoch + 2)
        self.session.forget_set(PROJECT, "so-3a4739b9")
        self.assertEqual(self.session.epoch, epoch + 2)

    def test_tolerance_and_radius_follow_config(self):
        self.select()
        view = self.view(self.session, 1000)
        self.assertEqual((view["tolerance_m"], view["fine_radius_m"]), (0.1, 5.0))
        CONFIG["stakeout_tolerance_m"] = 0.02
        CONFIG["stakeout_fine_radius_m"] = 2.5
        view = self.view(self.session, 1000)
        self.assertEqual((view["tolerance_m"], view["fine_radius_m"]), (0.02, 2.5))
        self.push(self.session, fix_at(5100000000, 1000000000), 1000)
        view = self.view(self.session, 1000)
        self.assertEqual((view["state"], view["reasons"]), ("fine", ["outside_tolerance"]))

    def test_guidance_keys_and_diagnostics(self):
        self.select()
        self.push(self.session, fix_at(5100000000, 1000000000), 1000)
        view = self.view(self.session, 1000)
        self.assertEqual(set(view), {"state", "dn_m", "de_m", "dist_m", "bearing_deg",
                                     "tolerance_m", "fine_radius_m", "window", "reasons"})
        self.assertEqual(set(view["window"]), {"have", "need", "spread_m"})
        self.assertEqual(self.session.fixes_seen, 1)
        self.assertIsInstance(self.session.on_fix_us_max, int)
        self.assertGreaterEqual(self.session.on_fix_us_max, 0)
        view["reasons"].append("mutated")
        self.assertEqual(self.view(self.session, 1000)["reasons"], [])

    def test_on_fix_duration_is_measured_with_wrapping_ticks(self):
        clock = WrapClock(ms=2 ** 30 - 1, us=2 ** 30 - 100)
        ticks = iter((2 ** 30 - 100, 250))
        clock.ticks_us = lambda: next(ticks)
        with mock.patch.object(stakeout_session, "time", clock):
            self.session.on_fix(fix_at(5100000000, 1000000000),
                                *judged(fix_at(5100000000, 1000000000))[1:], 1000, 1, 0)
        self.assertEqual(self.session.on_fix_us_max, 350)

    def test_module_constants_and_singleton(self):
        self.assertEqual((stakeout_session.FAR_M, stakeout_session.FINE_DETAIL_M,
                          stakeout_session.EPOCH_GAP_MS, stakeout_session.MOTION_MIN_MPS),
                         (50000, 10000, 2500, 0.5))
        self.assertIsInstance(stakeout_session.session, stakeout_session.StakeoutSession)


def on_p2(north_m=0.0, east_m=0.0, **kwargs):
    """A rover fix ``north_m``/``east_m`` from P2 (positive = north/east of it)."""
    return fix_at(P2["lat_e8"] + north_e8(north_m), P2["lon_e8"] + east_e8(east_m), **kwargs)


class WindowCase(SessionCase):
    """Feeds epochs with rising utc; every fed fix is a fresh dict."""

    def setUp(self):
        super().setUp()
        self.epoch_index = 0

    def feed(self, session, fixes, start_ms=1000, step_ms=1000):
        """Publish ``fixes`` one epoch each; returns the time for the next epoch."""
        now_ms = start_ms
        for fix in fixes:
            self.epoch_index += 1
            self.push(session, dict(fix, utc="1200%02d.00" % self.epoch_index), now_ms)
            now_ms += step_ms
        return now_ms

    def have(self, now_ms):
        return self.view(self.session, now_ms)["window"]["have"]

    def reach_ready(self, north_m=0.08, start_ms=1000):
        """Five epochs ``north_m`` from P2; returns the time of the last one."""
        self.select()
        next_ms = self.feed(self.session, [on_p2(north_m)] * 5, start_ms)
        last_ms = next_ms - 1000
        self.assertEqual(self.view(self.session, last_ms)["state"], "ready")
        return last_ms


class TestWindow(WindowCase):
    def test_five_good_epochs_reach_ready(self):
        self.select()
        offsets = (0.0, 0.004, -0.003, 0.002, 0.011)
        now_ms = 1000
        for count, north_m in enumerate(offsets[:4], start=1):
            now_ms = self.feed(self.session, [on_p2(north_m)], now_ms)
            view = self.view(self.session, now_ms - 1000)
            self.assertEqual(view["state"], "settling")
            self.assertEqual(view["window"], {"have": count, "need": 5, "spread_m": None})
            self.assertEqual(view["reasons"], [])
        self.feed(self.session, [on_p2(offsets[4])], now_ms)
        view = self.view(self.session, now_ms)
        self.assertEqual(view["state"], "ready")
        self.assertEqual(view["reasons"], [])
        self.assertEqual(view["window"]["have"], 5)
        observed = tracking.average_fixes(self.session.window_fixes())["accuracy_observation"]
        self.assertGreater(observed["horizontal_max_deviation_m"], 0.005)
        self.assertEqual(view["window"]["spread_m"],
                         round(observed["horizontal_max_deviation_m"], 3))
        mean = tracking.average_fixes(self.session.window_fixes())
        self.assertEqual(self.session.window_mean()[:2], (mean["lat_e8"], mean["lon_e8"]))
        self.assertEqual(round(self.session.window_mean()[2], 4),
                         observed["horizontal_max_deviation_m"])

    def test_repeated_utc_counts_once(self):
        self.select()
        for now_ms in (1000, 2000, 3000, 4000, 5000):
            self.push(self.session, on_p2(utc="120001.00"), now_ms)
            self.assertEqual(self.have(now_ms), 1)
        self.assertEqual(len(self.session.window_fixes()), 1)

    def test_outlier_resets(self):
        self.select()
        now_ms = self.feed(self.session, [on_p2()] * 4)
        self.assertEqual(self.have(now_ms - 1000), 4)
        now_ms = self.feed(self.session, [on_p2(0.5)], now_ms)
        view = self.view(self.session, now_ms - 1000)
        self.assertEqual((view["state"], view["window"]["have"]), ("fine", 0))
        self.assertEqual(self.session.window_fixes(), [])
        now_ms = self.feed(self.session, [on_p2()], now_ms)
        self.assertEqual(self.have(now_ms - 1000), 1)

    def test_gst_after_gga_enriches_the_same_element(self):
        self.select()
        now_ms = self.feed(self.session, [on_p2()] * 2)
        self.push(self.session, on_p2(utc="120003.00", gst=False), now_ms, pending=True)
        view = self.view(self.session, now_ms)
        self.assertEqual((view["state"], view["reasons"]), ("settling", ["gst_pending"]))
        self.assertEqual(view["window"]["have"], 2)
        self.push(self.session, on_p2(utc="120003.00"), now_ms + 400, enrich=True)
        view = self.view(self.session, now_ms + 400)
        self.assertEqual((view["state"], view["reasons"]), ("settling", []))
        self.assertEqual(view["window"]["have"], 3)
        self.assertEqual(len(self.session.window_fixes()), 3)
        self.assertIn("receiver_accuracy", self.session.window_fixes()[-1])

    def test_missing_gst_resets_at_the_next_epoch(self):
        self.select()
        now_ms = self.feed(self.session, [on_p2()] * 2)
        self.push(self.session, on_p2(utc="120003.00", gst=False), now_ms, pending=True)
        self.push(self.session, on_p2(utc="120004.00"), now_ms + 1000)
        self.assertEqual(self.have(now_ms + 1000), 1)
        self.assertEqual(self.session.window_fixes()[0]["utc"], "120004.00")

    def test_gst_before_gga_counts_directly(self):
        self.select()
        now_ms = self.feed(self.session, [on_p2()] * 2)
        self.push(self.session, on_p2(utc="120003.00"), now_ms)
        view = self.view(self.session, now_ms)
        self.assertEqual((view["window"]["have"], view["reasons"]), (3, []))

    def test_epoch_gap_boundary(self):
        self.select()
        self.push(self.session, on_p2(utc="120001.00"), 1000)
        self.push(self.session, on_p2(utc="120002.00"), 3500)
        self.assertEqual(self.have(3500), 2)
        self.push(self.session, on_p2(utc="120003.00"), 6001)
        self.assertEqual(self.have(6001), 1)
        self.assertEqual(self.session._window[-1][1], 6001)

    def gga_gst_gga(self, gst_ms, next_ms, gst_published_ms=None):
        self.select()
        self.push(self.session, on_p2(utc="120006.00", gst=False), 6000, pending=True)
        self.assertEqual(self.have(6000), 0)
        self.push(self.session, on_p2(utc="120006.00"), gst_ms, enrich=True,
                  published_ms=gst_published_ms)
        self.assertEqual(self.have(gst_ms), 1)
        self.assertEqual(self.session._window[-1][1], 6000)
        self.push(self.session, on_p2(utc="120009.00"), next_ms)
        return self.have(next_ms)

    def test_gst_keeps_the_epoch_time(self):
        self.assertEqual(self.gga_gst_gga(7900, 9000), 1)
        self.assertEqual(self.session._window[-1][1], 9000)
        self.session.clear()
        self.assertEqual(self.gga_gst_gga(7900, 8400), 2)

    def test_late_gst_of_the_same_publication_never_moves_the_epoch(self):
        self.assertEqual(self.gga_gst_gga(8700, 9000), 1)
        self.assertEqual((self.seq, self.rev), (2, 1))
        self.assertEqual(len(self.session.window_fixes()), 1)
        self.assertIs(self.session.motion()["valid"], False)
        self.session.clear()
        self.assertEqual(self.gga_gst_gga(8700, 9000, gst_published_ms=8700), 1)
        self.assertEqual(len(self.session.window_fixes()), 1)
        self.session.clear()
        self.assertEqual(self.gga_gst_gga(8000, 8400), 2)

    def test_target_selected_between_gga_and_gst(self):
        for next_ms, expected in ((9000, 1), (8400, 2)):
            self.session = stakeout_session.StakeoutSession()
            self.seq = self.rev = 0
            self.push(self.session, on_p2(utc="120006.00", gst=False), 6000, pending=True)
            self.assertEqual(self.view(self.session, 6000)["state"], "no_target")
            self.assertEqual(self.session._window, [])
            self.clock.advance(1000)
            self.select()
            self.push(self.session, on_p2(utc="120006.00"), 8700, enrich=True)
            self.assertEqual(self.seq, 1)
            self.assertEqual(self.have(8700), 1)
            self.assertEqual(self.session._window[-1][1], 6000)
            self.push(self.session, on_p2(utc="120009.00"), next_ms)
            self.assertEqual(self.have(next_ms), expected, next_ms)

    def test_same_utc_after_a_long_break_is_a_new_epoch(self):
        self.select()
        self.push(self.session, on_p2(utc="120001.00"), 1000)
        self.push(self.session, on_p2(utc="120001.00"), 3500)
        self.assertEqual(self.have(3500), 1)
        self.assertEqual(self.session._window[-1][1], 1000)
        self.push(self.session, on_p2(utc="120001.00"), 5000)
        self.assertEqual(self.have(5000), 1)
        self.assertEqual(self.session._window[-1][1], 5000)
        self.assertIs(self.session.motion()["valid"], False)

    def test_tick_wrap_is_not_a_gap(self):
        self.select()
        self.clock.ms = 2 ** 30 - 400
        self.push(self.session, on_p2(utc="120001.00"), 2 ** 30 - 400)
        self.push(self.session, on_p2(utc="120002.00"), self.clock.ticks_add(2 ** 30 - 400, 1000))
        self.assertEqual(self.clock.ticks_ms(), 600)
        self.assertEqual(self.have(600), 2)
        # A real gap across the wrap still resets (a plain subtraction would not).
        self.select(P1)
        self.clock.ms = 2 ** 30 - 400
        self.push(self.session, on_p2(utc="120003.00"), 2 ** 30 - 400)
        self.push(self.session, on_p2(utc="120004.00"), self.clock.ticks_add(2 ** 30 - 400, 2501))
        self.assertEqual(self.have(2101), 1)
        self.assertEqual(self.session._window[-1][1], 2101)

    def test_spread_keeps_rolling_without_reset(self):
        CONFIG["measurement_max_horizontal_spread_m"] = 0.01
        self.select()
        now_ms = self.feed(self.session, [on_p2(0.015), on_p2(-0.015), on_p2(0.015),
                                          on_p2(), on_p2()])
        view = self.view(self.session, now_ms - 1000)
        self.assertEqual((view["state"], view["reasons"]),
                         ("settling", ["horizontal_spread_too_high"]))
        self.assertEqual(view["window"]["have"], 5)
        self.assertGreater(view["window"]["spread_m"], 0.01)
        self.assertEqual(len(self.session.window_fixes()), 5)
        for count in range(3):
            now_ms = self.feed(self.session, [on_p2()], now_ms)
            view = self.view(self.session, now_ms - 1000)
            self.assertEqual(view["window"]["have"], 5)
            self.assertEqual(view["state"], "ready" if count == 2 else "settling", count)
        self.assertEqual(view["window"]["spread_m"], 0.0)

    def test_window_state_rules(self):
        rules = stakeout_session._window_state
        self.assertEqual(rules(0.1000001, 0.0, 0.1, 0.05), ("settling", ["mean_outside_tolerance"]))
        self.assertEqual(rules(0.05, 0.06, 0.1, 0.05), ("settling", ["horizontal_spread_too_high"]))
        self.assertEqual(rules(0.05, 0.05, 0.1, 0.05), ("ready", []))
        self.assertEqual(rules(0.1, 0.0, 0.1, 0.05), ("ready", []))

    def test_config_change_resets_the_window(self):
        for key, value, need in (("measurement_max_hdop", 1.0, 5),
                                 ("stakeout_tolerance_m", 0.2, 5),
                                 ("measurement_quality_window_samples", 7, 7)):
            self.select(P1)
            self.select()
            now_ms = self.feed(self.session, [on_p2()] * 3)
            self.assertEqual(self.have(now_ms - 1000), 3)
            saved = CONFIG[key]
            CONFIG[key] = value
            now_ms = self.feed(self.session, [on_p2()], now_ms)
            view = self.view(self.session, now_ms - 1000)
            self.assertEqual((view["state"], view["reasons"]), ("settling", []), key)
            self.assertEqual(view["window"]["have"], 1, key)
            self.assertEqual(view["window"]["need"], need, key)
            CONFIG[key] = saved

    def test_config_change_is_seen_before_the_next_fix(self):
        now_ms = self.reach_ready(0.08)
        cases = (("stakeout_tolerance_m", 0.02, "fine", ["outside_tolerance"], 5),
                 ("stakeout_tolerance_m", 0.09, "settling", ["config_changed"], 5),
                 ("measurement_quality_window_samples", 10, "settling", ["config_changed"], 10),
                 ("measurement_max_hdop", 1.0, "settling", ["config_changed"], 5))
        for key, value, state, reasons, need in cases:
            saved = CONFIG[key]
            CONFIG[key] = value
            view = self.view(self.session, now_ms)
            self.assertEqual((view["state"], view["reasons"]), (state, reasons), key)
            self.assertEqual(view["window"], {"have": 0, "need": need, "spread_m": None}, key)
            self.assertEqual(view["dist_m"], 0.08, key)
            self.assertEqual(self.session.window_fixes(), [], key)
            self.assertIsNone(self.session.window_mean(), key)
            CONFIG[key] = saved
            view = self.view(self.session, now_ms)
            self.assertEqual(view["state"], "ready", key)
            self.assertEqual(view["window"]["have"], 5, key)
            self.assertEqual(len(self.session.window_fixes()), 5, key)
            self.assertIsNotNone(self.session.window_mean(), key)

    def test_target_change_resets_and_same_target_keeps(self):
        self.select()
        now_ms = self.feed(self.session, [on_p2()] * 3)
        self.assertFalse(self.select())
        self.assertEqual(self.have(now_ms - 1000), 3)
        self.assertTrue(self.select(P1))
        self.assertEqual(self.have(now_ms - 1000), 0)
        self.assertEqual(self.session.window_fixes(), [])

    def test_ready_needs_fresh_fix(self):
        now_ms = self.reach_ready(0.0)
        view = self.view(self.session, now_ms + 3500, fresh=False)
        self.assertEqual(view["state"], "no_position")
        self.assertIsNone(view["dist_m"])
        self.assertEqual(view["window"]["have"], 0)

    def test_gate_keys_match_tracking(self):
        self.assertEqual(stakeout_session._GATE_KEYS, tracking.MEASUREMENT_GATE_KEYS)
        CONFIG["stakeout_fine_radius_m"] = 7.5
        signature = stakeout_session._signature()
        self.assertEqual(len(signature), 10)
        self.assertEqual(signature, tuple(CONFIG[key] for key in tracking.MEASUREMENT_GATE_KEYS)
                         + (CONFIG["stakeout_tolerance_m"], 7.5))

    def test_work_per_fix_is_bounded(self):
        CONFIG["measurement_quality_window_samples"] = 30
        self.select()
        forbidden = [mock.patch.object(builtins, "open", side_effect=AssertionError("open")),
                     mock.patch.object(os, "stat", side_effect=AssertionError("stat")),
                     mock.patch.object(os, "remove", side_effect=AssertionError("remove")),
                     mock.patch.object(os, "rename", side_effect=AssertionError("rename"))]
        if hasattr(os, "ilistdir"):
            forbidden.append(mock.patch.object(os, "ilistdir",
                                               side_effect=AssertionError("ilistdir")))
        for patcher in forbidden:
            patcher.start()
        try:
            now_ms = 1000
            for _ in range(1000):
                now_ms = self.feed(self.session, [on_p2()], now_ms)
                self.assertLessEqual(len(self.session.window_fixes()), 30)
                self.assertLessEqual(len(self.session._window), 30)
        finally:
            for patcher in forbidden:
                patcher.stop()
        self.assertEqual(self.view(self.session, now_ms - 1000)["state"], "ready")
        self.assertFalse(hasattr(stakeout_session, "log"))
        with open(stakeout_session.__file__) as handle:
            source = handle.read()
        self.assertIsNone(re.search(r"^\s*(import|from)\s+u?os\b", source, re.M))


class TestConfirmWindow(WindowCase):
    def confirm(self, tid, now_ms, fresh=True, seq=None, rev=None):
        return self.session.confirm_window(tid, now_ms, fresh,
                                           self.seq if seq is None else seq,
                                           self.rev if rev is None else rev)

    def test_confirm_window_is_retaken_after_a_config_change(self):
        now_ms = self.reach_ready(0.08)
        epoch = self.session.epoch
        before = self.confirm(P2["tid"], now_ms)
        self.assertEqual(len(before), 5)
        CONFIG["stakeout_tolerance_m"] = 0.02
        self.assertIsNone(self.confirm(P2["tid"], now_ms))
        self.assertEqual(self.view(self.session, now_ms)["state"], "fine")
        next_ms = self.feed(self.session, [on_p2()] * 5, now_ms + 1000)
        self.assertEqual(self.view(self.session, next_ms - 1000)["state"], "ready")
        after = self.confirm(P2["tid"], next_ms - 1000)
        self.assertEqual(len(after), 5)
        self.assertTrue({id(fix) for fix in after}.isdisjoint({id(fix) for fix in before}))
        target = (P2["lat_e8"], P2["lon_e8"])
        mean_after = tracking.average_fixes(after)
        mean_before = tracking.average_fixes(before)
        self.assertLessEqual(geo.distance_e8_m(mean_after["lat_e8"], mean_after["lon_e8"],
                                               *target), 0.02)
        self.assertGreater(geo.distance_e8_m(mean_before["lat_e8"], mean_before["lon_e8"],
                                             *target), 0.02)
        self.assertLess(abs(geo.distance_e8_m(mean_before["lat_e8"], mean_before["lon_e8"],
                                              *target) - 0.08), 0.001)
        self.assertEqual(self.session.epoch, epoch)

    def test_confirm_window_refuses_a_target_change_during_the_wait(self):
        now_ms = self.reach_ready(0.0)
        self.assertEqual(len(self.confirm(P2["tid"], now_ms)), 5)
        self.assertTrue(self.select(P1))
        self.assertEqual(P1["tid"], "so-3a4739b9/1/1")
        self.assertIsNone(self.confirm(P2["tid"], now_ms))
        self.assertIsNone(self.confirm(P1["tid"], now_ms))
        self.assertTrue(self.select())
        self.assertIsNone(self.confirm(P2["tid"], now_ms))
        self.assertEqual(self.have(now_ms), 0)
        fresh = [on_p2() for _ in range(5)]
        next_ms = now_ms + 1000
        for count, fix in enumerate(fresh, start=1):
            if count < 5:
                self.assertIsNone(self.confirm(P2["tid"], next_ms - 1000))
            next_ms = self.feed(self.session, [fix], next_ms)
        window = self.confirm(P2["tid"], next_ms - 1000)
        self.assertEqual(len(window), 5)
        self.assertEqual([fix["utc"] for fix in window],
                         ["1200%02d.00" % index for index in range(6, 11)])
        self.assertEqual([id(fix) for fix in window],
                         [id(item[0]) for item in self.session._window])

    def test_confirm_window_checks_release_freshness_and_signature(self):
        now_ms = self.reach_ready(0.0)
        first = self.confirm(P2["tid"], now_ms)
        second = self.confirm(P2["tid"], now_ms)
        self.assertEqual(len(first), 5)
        self.assertIsNot(first, second)
        self.assertIsNot(first, self.session.window_fixes())
        for left, right in zip(first, self.session.window_fixes()):
            self.assertIs(left, right)
        self.assertIsNone(self.confirm(P2["tid"], now_ms, fresh=False))
        self.assertIsNone(self.confirm(P2["tid"], now_ms, rev=self.rev + 1))
        self.assertIsNone(self.confirm(P2["tid"], now_ms, seq=self.seq + 1))
        saved = CONFIG["measurement_max_hdop"]
        CONFIG["measurement_max_hdop"] = 1.0
        self.assertIsNone(self.confirm(P2["tid"], now_ms))
        CONFIG["measurement_max_hdop"] = saved
        self.assertEqual(len(self.confirm(P2["tid"], now_ms)), 5)
        self.assertIsNone(self.confirm(None, now_ms))
        self.assertIsNone(self.confirm(5, now_ms))
        self.session.select(PROJECT, FINGERPRINT, None)
        self.assertIsNone(self.confirm(P2["tid"], now_ms))
        self.assertIsNone(self.confirm(None, now_ms))

    def test_confirm_window_is_synchronous(self):
        self.assertIs(inspect.iscoroutinefunction(stakeout_session.StakeoutSession.confirm_window),
                      False)
        now_ms = self.reach_ready(0.0)
        for result in (self.confirm(P2["tid"], now_ms), self.confirm(P1["tid"], now_ms)):
            self.assertFalse(inspect.isawaitable(result))
            self.assertFalse(inspect.isgenerator(result))


class TestRelease(WindowCase):
    def failed_run(self, attribute, error):
        now_ms = self.reach_ready(0.0)
        with mock.patch.object(geo, attribute, side_effect=error("injected")) as patched:
            with self.assertRaises(error):
                self.feed(self.session, [on_p2()], now_ms + 1000)
        self.assertTrue(patched.called)
        view = self.view(self.session, now_ms + 1000)
        self.assertEqual((view["state"], view["reasons"]), ("no_position", ["session_error"]))
        for key in ("dn_m", "de_m", "dist_m", "bearing_deg"):
            self.assertIsNone(view[key], key)
        self.assertEqual(view["window"]["have"], 0)
        self.assertEqual(self.session.window_fixes(), [])
        self.assertIsNone(self.session.window_mean())
        self.assertIsNone(self.session.confirm_window(P2["tid"], now_ms + 1000, True,
                                                      self.seq, self.rev))
        self.feed(self.session, [on_p2()], now_ms + 2000)
        view = self.view(self.session, now_ms + 2000)
        self.assertEqual((view["state"], view["window"]["have"]), ("settling", 1))

    def test_failed_run_never_keeps_ready(self):
        self.failed_run("offsets_e8_m", RuntimeError)

    def test_memory_error_in_the_window_never_keeps_ready(self):
        self.failed_run("mean_e8", MemoryError)

    def test_release_is_bound_to_seq_and_rev(self):
        now_ms = self.reach_ready(0.0)
        view = self.session.guidance(now_ms, True, self.seq, self.rev + 1)
        self.assertEqual((view["state"], view["reasons"]), ("no_position", ["session_error"]))
        self.assertIsNone(view["dist_m"])
        view = self.session.guidance(now_ms, True, self.seq + 1, self.rev)
        self.assertEqual(view["reasons"], ["session_error"])
        self.assertEqual(self.view(self.session, now_ms)["state"], "ready")

    def test_session_error_needs_a_target(self):
        self.feed(self.session, [on_p2()])
        view = self.session.guidance(1000, True, self.seq + 1, self.rev)
        self.assertEqual((view["state"], view["reasons"]), ("no_target", []))


# The keys of the live packet on each level (Task 8).
PACKET_KEYS = {"schema", "device_time", "fix_seq", "fix_rev", "fix_age_ms", "stale",
               "position", "corrections", "gate", "motion", "header", "target", "guidance"}
POSITION_KEYS = {"lat_e8", "lon_e8", "alt_mm", "qual", "fix_status_text", "sats", "hdop",
                 "correction_age_sec", "gst_h_sigma_m", "gst_v_sigma_m"}
CORRECTIONS_KEYS = {"ntrip_state", "rtcm_age_sec", "link"}
GATE_KEYS = {"accepted", "reasons"}
MOTION_KEYS = {"speed_mps", "course_deg", "valid"}
HEADER_KEYS = {"access_state", "storage_level"}
TARGET_KEYS = {"tid", "lat_e8", "lon_e8", "status"}
GUIDANCE_KEYS = {"state", "dn_m", "de_m", "dist_m", "bearing_deg", "tolerance_m",
                 "fine_radius_m", "window", "reasons"}
WINDOW_KEYS = {"have", "need", "spread_m"}
HEADER = {"access_state": "ONLINE", "storage_level": "ok"}
STATS = {"access_state": "ONLINE", "ntrip_state": "streaming", "uptime_sec": 5}
NO_FIX_VIEW = {"fix_status_text": "NO_FIX", "qual": 0, "sats": 0, "fix_age_ms": None,
               "fix_seq": 0, "fix_rev": 0, "stale": False, "pending_gst": False}
# Every reason the session can report as guidance.reasons (one at a time).
SESSION_REASONS = {"awaiting_fix", "outside_tolerance", "gate_rejected", "gst_pending",
                   "config_changed", "session_error", "mean_outside_tolerance",
                   "horizontal_spread_too_high"}
# A float with the longest ujson form the packet can carry.
LONG_FLOAT = -1.234567e-05


def device_bytes(packet):
    """The packet as the device serialises it: ujson separators, UTF-8."""
    return len(json.dumps(packet, ensure_ascii=False).encode())


def assert_packet_keys(case, packet, target, position):
    case.assertEqual(set(packet), PACKET_KEYS)
    case.assertEqual(set(packet["corrections"]), CORRECTIONS_KEYS)
    case.assertEqual(set(packet["gate"]), GATE_KEYS)
    case.assertEqual(set(packet["motion"]), MOTION_KEYS)
    case.assertEqual(set(packet["header"]), HEADER_KEYS)
    case.assertEqual(set(packet["guidance"]), GUIDANCE_KEYS)
    case.assertEqual(set(packet["guidance"]["window"]), WINDOW_KEYS)
    if target:
        case.assertEqual(set(packet["target"]), TARGET_KEYS)
    else:
        case.assertIsNone(packet["target"])
    if position:
        case.assertEqual(set(packet["position"]), POSITION_KEYS)
    else:
        case.assertIsNone(packet["position"])


class TestLivePacket(WindowCase):
    """StakeoutSession.live_packet: the body of GET /api/stakeout/live."""

    def fix_view(self, fix, stale=False, age_ms=320, pending=False, held=None):
        """fix as web.current_fix_view reports it for the session's (seq, rev)."""
        view = dict(fix, fix_seq=self.seq, fix_rev=self.rev, fix_age_ms=age_ms,
                    stale=stale, pending_gst=pending)
        if pending:
            view["held_receiver_accuracy"] = held
        return view

    def packet(self, view, now_ms, stale=None, link="ok", recording=False, quality=None,
               session=None):
        if stale is not None:
            view = dict(view, stale=stale)
        if quality is None:
            quality = tracking.quality_check(None if view["stale"] else view)
        return (session or self.session).live_packet(
            now_ms, (view, quality), {"epoch_age_sec": 0.8, "stale": False},
            {"corrections": link, "receiver": "ok"}, dict(STATS), "ok", recording, 1760000123)

    def test_packet_keys_are_exact(self):
        now_ms = self.reach_ready(0.0)
        fix = self.session._window[-1][0]
        packet = self.packet(self.fix_view(fix), now_ms)
        assert_packet_keys(self, packet, True, True)
        self.assertEqual((packet["schema"], packet["device_time"]), (1, 1760000123))
        self.assertEqual((packet["fix_seq"], packet["fix_rev"], packet["fix_age_ms"],
                          packet["stale"]), (self.seq, self.rev, 320, False))
        self.assertEqual(packet["position"], {
            "lat_e8": fix["lat_e8"], "lon_e8": fix["lon_e8"], "alt_mm": 412300, "qual": 4,
            "fix_status_text": "RTK_FIXED", "sats": 20, "hdop": 0.6,
            "correction_age_sec": 1.0, "gst_h_sigma_m": 0.01, "gst_v_sigma_m": 0.02})
        self.assertEqual(packet["corrections"],
                         {"ntrip_state": "streaming", "rtcm_age_sec": 0.8, "link": "ok"})
        self.assertEqual(packet["gate"], {"accepted": True, "reasons": []})
        self.assertEqual(packet["motion"], self.session.motion())
        self.assertEqual(packet["header"], HEADER)
        self.assertEqual(packet["target"], {"tid": P2["tid"], "lat_e8": P2["lat_e8"],
                                            "lon_e8": P2["lon_e8"], "status": "open"})
        self.assertEqual(packet["guidance"], self.view(self.session, now_ms))
        self.assertEqual(packet["guidance"]["state"], "ready")
        # Without a position: the view of a device that never had a fix.
        packet = self.packet(dict(NO_FIX_VIEW, fix_seq=self.seq, fix_rev=self.rev), now_ms)
        assert_packet_keys(self, packet, True, False)
        # Without a target, with and without a position.
        session = stakeout_session.StakeoutSession()
        packet = self.packet(self.fix_view(fix), now_ms, session=session)
        assert_packet_keys(self, packet, False, True)
        self.assertEqual(packet["guidance"]["state"], "no_target")
        packet = self.packet(dict(NO_FIX_VIEW), now_ms, session=session)
        assert_packet_keys(self, packet, False, False)
        self.assertEqual((packet["fix_seq"], packet["fix_age_ms"]), (0, None))

    def test_position_sigmas_follow_the_live_display(self):
        now_ms = self.reach_ready(0.0)
        bare = on_p2(gst=False)
        held = {"horizontal_sigma_m": 0.013, "altitude_sigma_m": 0.021}
        position = self.packet(self.fix_view(bare, pending=True, held=held), now_ms)["position"]
        self.assertEqual((position["gst_h_sigma_m"], position["gst_v_sigma_m"]), (0.013, 0.021))
        position = self.packet(self.fix_view(bare), now_ms)["position"]
        self.assertEqual((position["gst_h_sigma_m"], position["gst_v_sigma_m"]), (None, None))
        # The fix's own GST wins over a held one.
        own = self.packet(self.fix_view(on_p2(), pending=True, held=held), now_ms)["position"]
        self.assertEqual((own["gst_h_sigma_m"], own["gst_v_sigma_m"]), (0.01, 0.02))

    def spec_guidance(self):
        return {"state": "settling", "dn_m": 0.3, "de_m": -0.29, "dist_m": 0.42,
                "bearing_deg": 316.0, "tolerance_m": 0.1, "fine_radius_m": 5.0,
                "window": {"have": 3, "need": 5, "spread_m": 0.012}, "reasons": []}

    def test_size_budget_in_device_bytes(self):
        # The example values of spec section 8 (E6: at most 900 bytes).
        target = dict(P2, tid="so-3f9a1c2b/1/3", lat_e8=5100009261, lon_e8=999999552)
        self.session.select(PROJECT, FINGERPRINT, target)
        fix = dict(fix_at(5100008991, 1000000012), alt_mm=412345, sats=21,
                   receiver_accuracy={"horizontal_sigma_m": 0.011, "altitude_sigma_m": 0.018})
        view = dict(self.fix_view(fix), fix_seq=4711, fix_rev=2)
        with mock.patch.object(self.session, "guidance", return_value=self.spec_guidance()):
            packet = self.packet(view, 1000)
        self.assertEqual(packet["device_time"], 1760000123)
        self.assertEqual(packet["guidance"]["window"]["have"], 3)
        self.assertLessEqual(device_bytes(packet), 900)
        # The worst case (E6: at most 1400 bytes): every gate reason, the
        # longest session reasons, extreme numbers and the longest tid.
        target = dict(P2, tid="so-ffffffff/500/2000", lat_e8=-9000000000,
                      lon_e8=-18000000000)
        self.assertTrue(self.session.select(PROJECT, FINGERPRINT, target))
        self.session.status_of = lambda tid: "confirmed"
        fix = {"lat_e8": -9000000000, "lon_e8": -18000000000, "alt_mm": -2147483648,
               "qual": 2, "fix_status_text": "GPS_FIX_2D/3D", "sats": 99,
               "hdop": LONG_FLOAT, "correction_age_sec": LONG_FLOAT,
               "receiver_accuracy": {"horizontal_sigma_m": LONG_FLOAT,
                                     "altitude_sigma_m": LONG_FLOAT}}
        view = dict(fix, fix_seq=2 ** 30, fix_rev=2 ** 30, fix_age_ms=2 ** 29, stale=True,
                    pending_gst=False)
        gate_reasons = ["no_position", "required_fix_not_met", "too_few_satellites",
                        "hdop_too_high", "altitude_required", "corrections_too_old",
                        "gst_horizontal_error_too_high", "gst_vertical_error_too_high"]
        guidance = {"state": "no_position", "dn_m": LONG_FLOAT, "de_m": LONG_FLOAT,
                    "dist_m": LONG_FLOAT, "bearing_deg": LONG_FLOAT,
                    "tolerance_m": LONG_FLOAT, "fine_radius_m": LONG_FLOAT,
                    "window": {"have": 30, "need": 30, "spread_m": LONG_FLOAT},
                    "reasons": ["horizontal_spread_too_high"]}
        motion = {"speed_mps": LONG_FLOAT, "course_deg": LONG_FLOAT, "valid": False}
        with mock.patch.object(self.session, "guidance", return_value=guidance), \
                mock.patch.object(self.session, "motion", return_value=motion):
            packet = self.session.live_packet(
                1000, (view, {"accepted": False, "reasons": list(gate_reasons)}),
                {"epoch_age_sec": LONG_FLOAT, "stale": True}, {"corrections": "stale"},
                {"access_state": "CONNECTING", "ntrip_state": "waiting_wifi"}, "critical",
                True, 2 ** 31 - 1)
        self.assertEqual(len(packet["gate"]["reasons"]), 10)
        self.assertEqual(packet["guidance"]["reasons"],
                         ["horizontal_spread_too_high", "recording_active"])
        self.assertEqual(packet["target"]["status"], "confirmed")
        self.assertLessEqual(device_bytes(packet), 1400)

    def test_guidance_has_at_most_one_state_reason(self):
        views = [self.view(self.session, 1000)]                         # no_target
        self.select()
        views.append(self.view(self.session, 1000))                     # awaiting_fix
        for north_m in (60000.0, 6.0, 1.0):                             # far, coarse, fine
            self.feed(self.session, [on_p2(north_m)])
            views.append(self.view(self.session, 1000))
        self.feed(self.session, [dict(on_p2(), hdop=9.0)])              # gate_rejected
        views.append(self.view(self.session, 1000))
        self.push(self.session, on_p2(utc="130000.00", gst=False), 2000, pending=True)
        views.append(self.view(self.session, 2000))                     # gst_pending
        self.push(self.session, on_p2(utc="130000.00"), 2100, enrich=True)
        views.append(self.view(self.session, 2100))                     # settling 1/5
        views.append(self.view(self.session, 2100, fresh=False))       # stale
        views.append(self.session.guidance(2100, True, self.seq, self.rev + 1))  # error
        self.select(P1)
        CONFIG["measurement_max_horizontal_spread_m"] = 0.01
        now_ms = self.feed(self.session, [on_p2(0.02), on_p2(-0.02)] * 2 + [on_p2()], 3000)
        views.append(self.view(self.session, now_ms - 1000))           # spread
        now_ms = self.feed(self.session, [on_p2()] * 5, now_ms)
        views.append(self.view(self.session, now_ms - 1000))           # ready
        CONFIG["stakeout_tolerance_m"] = 0.09
        views.append(self.view(self.session, now_ms - 1000))           # config_changed
        states = set()
        reasons = set()
        for view in views:
            self.assertLessEqual(len(view["reasons"]), 1, view)
            states.add(view["state"])
            reasons.update(view["reasons"])
        self.assertEqual(states, {"no_target", "no_position", "far", "coarse", "fine",
                                  "settling", "ready"})
        self.assertEqual(reasons, SESSION_REASONS - {"mean_outside_tolerance"})
        # A full window outside both limits reports one reason as well.
        state, window_reasons = stakeout_session._window_state(0.2, 0.2, 0.1, 0.05)
        self.assertEqual((state, len(window_reasons)), ("settling", 1))
        self.assertEqual(max(len(reason) for reason in SESSION_REASONS),
                         len("horizontal_spread_too_high"))

    def test_stale_fix_reads_as_no_position_without_touching_the_window(self):
        now_ms = self.reach_ready(0.0)
        fix = self.session._window[-1][0]
        window = list(self.session._window)
        packet = self.packet(self.fix_view(fix, stale=True, age_ms=3000), now_ms + 3000)
        guidance = packet["guidance"]
        self.assertEqual(guidance["state"], "no_position")
        for key in ("dn_m", "de_m", "dist_m", "bearing_deg"):
            self.assertIsNone(guidance[key], key)
        self.assertEqual(packet["gate"]["reasons"][-1], "fix_stale")
        self.assertFalse(packet["gate"]["accepted"])
        self.assertTrue(packet["stale"])
        self.assertEqual(self.session._window, window)
        packet = self.packet(self.fix_view(fix), now_ms)
        self.assertEqual(packet["guidance"]["state"], "ready")
        self.assertNotIn("fix_stale", packet["gate"]["reasons"])

    def test_corrections_missing_is_a_display_reason(self):
        now_ms = self.reach_ready(0.0)
        view = self.fix_view(self.session._window[-1][0])
        for link in ("down", "stale", "off"):
            with self.subTest(link=link):
                packet = self.packet(view, now_ms, link=link)
                self.assertEqual(packet["gate"]["reasons"], ["corrections_missing"])
                self.assertTrue(packet["gate"]["accepted"])
                self.assertEqual(packet["corrections"]["link"], link)
                self.assertEqual(packet["guidance"]["state"], "ready")
        packet = self.packet(view, now_ms, stale=True, link="down")
        self.assertEqual(packet["gate"]["reasons"][-2:], ["fix_stale", "corrections_missing"])
        self.assertEqual(self.view(self.session, now_ms)["state"], "ready")

    def test_recording_follows_the_session_reasons(self):
        self.select()
        self.feed(self.session, [on_p2(1.0)])
        view = self.fix_view(on_p2(1.0))
        guidance = self.packet(view, 1000, recording=True)["guidance"]
        self.assertEqual(guidance["reasons"], ["outside_tolerance", "recording_active"])
        self.assertEqual(self.view(self.session, 1000)["reasons"], ["outside_tolerance"])

    def test_packet_window_reads_the_packet(self):
        now_ms = self.reach_ready(0.0)
        view = self.fix_view(self.session._window[-1][0])
        packet = self.packet(view, now_ms)
        window = self.session.packet_window(P2["tid"], packet)
        self.assertEqual(window, self.session.window_fixes())
        self.assertEqual(len(window), 5)
        self.assertIsNone(self.session.packet_window(P1["tid"], packet))
        stale = self.packet(view, now_ms, stale=True)
        self.assertIsNone(self.session.packet_window(P2["tid"], stale))
        self.assertIsNone(self.session.packet_window(P2["tid"],
                                                     dict(packet, fix_rev=self.rev + 1)))

    def test_target_status_comes_from_status_of(self):
        now_ms = self.reach_ready(0.0)
        view = self.fix_view(self.session._window[-1][0])
        self.assertEqual(self.session.status_of(P2["tid"]), "open")
        self.assertEqual(self.packet(view, now_ms)["target"]["status"], "open")
        self.session.status_of = lambda tid: "confirmed"
        self.assertEqual(self.packet(view, now_ms)["target"]["status"], "confirmed")

    def test_status_comes_from_the_module_hook(self):
        # The default (web sets the hook to the stakeout progress when imported).
        self.assertEqual(stakeout_session.open_status(PROJECT, P2["tid"]), "open")
        self.select()
        with mock.patch.object(stakeout_session, "status_lookup",
                               stakeout_session.open_status):
            self.assertEqual(self.session.status_of(P2["tid"]), "open")
        seen = []

        def lookup(project_id, tid):
            seen.append((project_id, tid))
            return "confirmed"

        with mock.patch.object(stakeout_session, "status_lookup", lookup):
            self.assertEqual(self.session.status_of(P2["tid"]), "confirmed")
            self.assertEqual(stakeout_session.StakeoutSession().status_of(P1["tid"]),
                             "confirmed")
        self.assertEqual(seen, [(PROJECT, P2["tid"]), (None, P1["tid"])])


GST_SENTENCE = {"source": "NMEA_GST", "horizontal_sigma_m": 0.01, "altitude_sigma_m": 0.02}


class TestStateHook(unittest.TestCase):
    """AppState calls the fix listener for every published fix and GST enrichment."""

    def setUp(self):
        self.now = 0
        ticks = mock.patch.object(state.time, "ticks_ms", side_effect=lambda: self.now)
        ticks.start()
        self.addCleanup(ticks.stop)
        config = mock.patch.dict(CONFIG, {"stakeout_tolerance_m": 0.1,
                                          "stakeout_fine_radius_m": 5.0,
                                          "measurement_quality_window_samples": 5,
                                          "fix_update_interval_ms": 950,
                                          "fix_stale_after_ms": 3000})
        config.start()
        self.addCleanup(config.stop)
        tracker = mock.patch("tracking.tracker")
        self.tracker = tracker.start()
        self.addCleanup(tracker.stop)
        self.tracker.enqueue_fix.return_value = False
        log = support.patch_module_global("state", "log")
        self.log = log.start()
        self.addCleanup(log.stop)
        self.app = self.new_app()
        self.calls = []

    def new_app(self):
        # Made after the clock patch, so its log throttle reads the test clock.
        app = state.AppState()
        app.stats["gst_output_state"] = "receiving"
        return app

    def listen(self):
        """A real session on P2, registered as the listener of self.app."""
        session = stakeout_session.StakeoutSession()
        session.select(PROJECT, FINGERPRINT, dict(P2))
        self.app.set_fix_listener(session.on_fix)
        return session

    def record(self, fix, quality, pending, published_ms, seq, rev):
        self.calls.append({"fix": fix, "is_last": fix is self.app.last_fix,
                           "quality": quality, "pending": pending,
                           "published_ms": published_ms, "seq": seq, "rev": rev,
                           "fix_state": self.app.fix_state(self.now)})

    @staticmethod
    def utc(epoch_ms):
        return "12%04d.00" % (epoch_ms // 1000)

    def gga(self, at_ms, gst=False, app=None):
        """Publish a GGA on P2 at ``at_ms``; its utc names the second of ``at_ms``."""
        self.now = at_ms
        (app or self.app).update_fix(on_p2(utc=self.utc(at_ms), gst=gst))

    def gst(self, at_ms, epoch_ms, app=None):
        """A GST for the GGA published at ``epoch_ms``, arriving at ``at_ms``."""
        self.now = at_ms
        return (app or self.app).attach_gst(dict(GST_SENTENCE, utc=self.utc(epoch_ms)))

    def pairs(self, start_ms, count, app=None):
        """``count`` GGA+GST pairs, one per second, the GST 100 ms after its GGA."""
        for index in range(count):
            at_ms = start_ms + index * 1000
            self.gga(at_ms, app=app)
            self.assertTrue(self.gst(at_ms + 100, at_ms, app=app))

    def guidance(self, session, now_ms):
        fix_state = self.app.fix_state(now_ms)
        return session.guidance(now_ms, not fix_state["stale"], fix_state["fix_seq"],
                                fix_state["fix_rev"])

    def stake_logs(self):
        return [call for call in self.log.call_args_list
                if call.args[:2] == ("ERROR", "STAKE")]

    def test_listener_sees_each_published_fix_once(self):
        self.app.set_fix_listener(self.record)
        for at_ms in (1000, 2000, 2100, 3000):       # 2100 is throttled
            self.gga(at_ms, gst=True)
        self.assertEqual([call["published_ms"] for call in self.calls], [1000, 2000, 3000])
        for call in self.calls:
            self.assertTrue(call["is_last"])
            self.assertEqual(call["quality"], tracking.quality_check(call["fix"]))
            self.assertTrue(call["quality"]["accepted"])
            self.assertFalse(call["pending"])
            self.assertEqual((call["seq"], call["rev"]),
                             (call["fix_state"]["fix_seq"], call["fix_state"]["fix_rev"]))
        self.assertEqual([call["seq"] for call in self.calls], [1, 2, 3])
        self.assertEqual(self.app.fix_state()["fix_seq"], 3)

    def test_listener_sees_gst_enrichment_with_pending_first(self):
        self.app.set_fix_listener(self.record)
        self.gga(1000)
        bare = self.calls[0]
        self.assertTrue(bare["pending"])
        self.assertFalse(bare["quality"]["accepted"])
        self.assertEqual(bare["quality"]["reasons"], ["gst_required"])
        self.assertTrue(self.gst(1100, 1000))
        enriched = self.calls[1]
        self.assertTrue(enriched["is_last"])
        self.assertIn("receiver_accuracy", enriched["fix"])
        self.assertFalse(enriched["pending"])
        self.assertTrue(enriched["quality"]["accepted"])
        self.assertEqual(enriched["seq"], bare["seq"])
        self.assertEqual(enriched["rev"], bare["rev"] + 1)
        self.assertEqual((enriched["seq"], enriched["rev"]),
                         (enriched["fix_state"]["fix_seq"], enriched["fix_state"]["fix_rev"]))
        self.assertFalse(self.gst(1200, 5000))            # another epoch's utc
        self.assertEqual(len(self.calls), 2)

    def test_gst_carries_the_publication_time(self):
        self.app.set_fix_listener(self.record)
        self.gga(6000)
        self.assertTrue(self.gst(8700, 6000))             # 2700 ms: still fresh
        self.assertEqual([call["published_ms"] for call in self.calls], [6000, 6000])
        self.assertEqual(self.calls[1]["seq"], self.calls[0]["seq"])
        self.assertEqual(self.calls[1]["rev"], self.calls[0]["rev"] + 1)
        self.assertEqual(self.app.fix_state(8700)["fix_age_ms"], 2700)

    def late_gst_run(self, gst_ms, next_ms):
        session = self.listen()
        self.pairs(2000, 4)
        self.assertEqual(self.guidance(session, 5100)["window"]["have"], 4)
        self.gga(6000)
        self.assertEqual(self.guidance(session, 6000)["reasons"], ["gst_pending"])
        self.assertTrue(self.gst(gst_ms, 6000))
        self.gga(next_ms, gst=True)
        return self.guidance(session, next_ms)

    def test_late_gst_does_not_hide_a_gap_end_to_end(self):
        view = self.late_gst_run(8700, 9000)
        self.assertEqual(view["window"]["have"], 1)       # 6000 -> 9000 is a gap: reset
        self.assertNotEqual(view["state"], "ready")
        self.app = self.new_app()
        view = self.late_gst_run(8000, 8400)              # 6000 -> 8400: no gap
        self.assertEqual(view["window"], {"have": 5, "need": 5, "spread_m": 0.0})
        self.assertEqual(view["state"], "ready")

    def test_listener_errors_never_reach_the_gnss_path(self):
        def fail(*args):
            raise RuntimeError("injected")

        reference = self.new_app()
        self.app.set_fix_listener(fail)
        # 100 failing calls (GGA and GST) within one throttle interval.
        for index in range(50):
            at_ms = 1000 + index * 1000
            for app in (self.app, reference):
                self.gga(at_ms, app=app)
            for app in (self.app, reference):
                self.assertTrue(self.gst(at_ms + 100, at_ms, app=app))
            self.assertEqual(self.app.fix_state(self.now), reference.fix_state(self.now))
            self.assertEqual(self.app.gate_state(self.now), reference.gate_state(self.now))
        self.assertEqual(self.app.fix_state()["fix_seq"], 50)
        self.assertEqual(self.tracker.enqueue_fix.call_count, 200)
        self.assertLess(self.now, cfg_logging.HOT_PATH_LOG_INTERVAL_MS)
        self.assertEqual(len(self.stake_logs()), 1)
        self.assertIn("injected", self.stake_logs()[0].args[2])

    def test_a_failing_throttle_or_log_stays_inside(self):
        def fail(*args):
            raise MemoryError("injected")

        self.pairs(1000, 1)                               # the fix status is logged once here
        self.app.set_fix_listener(fail)
        self.tracker.enqueue_fix.reset_mock()
        with mock.patch.object(self.app._fix_listener_errors, "hit",
                               side_effect=MemoryError("throttle")) as hit:
            self.pairs(2000, 1)
        self.assertEqual(hit.call_count, 2)
        self.assertEqual(self.tracker.enqueue_fix.call_count, 2)
        self.log.side_effect = OSError("log failed")
        self.pairs(3000, 1)
        self.assertEqual(self.tracker.enqueue_fix.call_count, 4)
        self.assertEqual(len(self.stake_logs()), 1)        # the one that raised
        self.assertEqual(self.app.fix_state()["fix_seq"], 3)

    def test_listener_failure_after_ready_never_keeps_ready(self):
        for error in (RuntimeError, MemoryError):
            with self.subTest(error=error.__name__):
                self.app = self.new_app()
                session = self.listen()
                self.pairs(1000, 5)
                self.assertEqual(self.guidance(session, 5100)["state"], "ready")
                with mock.patch.object(geo, "offsets_e8_m",
                                       side_effect=error("injected")) as patched:
                    self.gga(6000, gst=True)
                self.assertTrue(patched.called)
                self.assertEqual(self.app.fix_state()["fix_seq"], 6)
                view = self.guidance(session, 6000)
                self.assertEqual((view["state"], view["reasons"]),
                                 ("no_position", ["session_error"]))
                self.assertEqual(session.window_fixes(), [])

    def test_without_listener_nothing_changes(self):
        reference = self.new_app()
        self.app.set_fix_listener(lambda *args: None)
        steps = [("gga", 1000), ("gst", 1100, 1000), ("gga", 2000), ("gga", 2100),
                 ("gga", 3000), ("gst", 5500, 3000), ("gga", 7000), ("gst", 7100, 7000),
                 ("gga", 8000)]
        for step in steps:
            for app in (self.app, reference):
                if step[0] == "gga":
                    self.gga(step[1], app=app)
                else:
                    self.gst(step[1], step[2], app=app)
            self.assertEqual(self.app.quality_streaks(self.now),
                             reference.quality_streaks(self.now))
            self.assertEqual(self.app.gate_state(self.now), reference.gate_state(self.now))
            self.assertEqual(self.app.fix_state(self.now), reference.fix_state(self.now))

    def test_end_to_end_reaches_ready_through_app_state(self):
        session = self.listen()
        self.pairs(1000, 5)
        view = self.guidance(session, 5100)
        self.assertEqual(view["state"], "ready")
        self.assertEqual(view["window"]["have"], 5)


class TestOrders(SessionCase):
    def test_orders_are_bitfields_per_set(self):
        session = self.session
        self.assertEqual(session.orders(), {})
        session.set_order(PROJECT, "so-3a4739b9", 3, True)
        session.set_order(PROJECT, "so-3a4739b9", 500, True)
        session.set_order(PROJECT, "so-3a4739b9", 3, False)
        self.assertEqual(session.orders(), {"so-3a4739b9": [500]})
        self.assertEqual(session.project_id, PROJECT)
        session.set_order(PROJECT, "so-00000001", 1, True)
        session.set_order(PROJECT, "so-00000001", 2, True)
        self.assertEqual(session.orders(), {"so-3a4739b9": [500], "so-00000001": [1, 2]})
        # A set whose parts all run forward again has no entry.
        session.set_order(PROJECT, "so-00000001", 1, False)
        session.set_order(PROJECT, "so-00000001", 2, False)
        self.assertEqual(session.orders(), {"so-3a4739b9": [500]})
        # The bitfield is 63 bytes per set, whatever the number of parts.
        self.assertEqual([len(bits) for bits in session._orders.values()], [63])
        epoch = session.epoch
        session.forget_set(PROJECT, "so-00000002")
        session.forget_set("p-other", "so-3a4739b9")
        self.assertEqual(session.epoch, epoch)
        session.forget_set(PROJECT, "so-3a4739b9")
        self.assertEqual(session.orders(), {})
        self.assertEqual(session.epoch, epoch + 1)
        session.set_order(PROJECT, "so-3a4739b9", 7, True, FINGERPRINT)
        self.select()
        self.assertEqual(session.orders(), {"so-3a4739b9": [7]})
        session.clear()
        self.assertEqual(session.orders(), {})
        self.assertIsNone(session.target)
        # Another project (or the same ID with another fingerprint): the
        # orders and the target of the previous one go first.
        for project_id, fingerprint in (("p-other", FINGERPRINT), (PROJECT, 1770000000)):
            with self.subTest(project_id=project_id, fingerprint=fingerprint):
                session.clear()
                session.set_order(PROJECT, "so-3a4739b9", 7, True, FINGERPRINT)
                self.select()
                epoch = session.epoch
                session.set_order(project_id, "so-00000001", 4, True, fingerprint)
                self.assertEqual(session.orders(), {"so-00000001": [4]})
                self.assertIsNone(session.target)
                self.assertEqual((session.project_id, session.fingerprint),
                                 (project_id, fingerprint))
                self.assertEqual(session.epoch, epoch + 1)
        # Selecting a target in another project drops the orders as well.
        session.select(PROJECT, FINGERPRINT, dict(P2))
        self.assertEqual(session.orders(), {})

    def test_orders_keep_at_most_four_sets(self):
        for index in range(6):
            self.session.set_order(PROJECT, "so-0000000%d" % index, 1, True)
        orders = self.session.orders()
        self.assertEqual(len(orders), 4)
        self.assertIn("so-00000005", orders)


class TestWrapClock(unittest.TestCase):
    def test_wrap_clock_matches_micropython_semantics(self):
        clock = WrapClock()
        self.assertEqual(clock.ticks_add(2 ** 30 - 400, 1000), 600)
        self.assertEqual(clock.ticks_diff(600, 2 ** 30 - 400), 1000)
        self.assertEqual(clock.ticks_diff(2 ** 30 - 400, 600), -1000)
        self.assertEqual(clock.ticks_diff(5, 5), 0)
        clock = WrapClock(ms=123, us=456)
        clock.advance(2 ** 30)
        self.assertEqual(clock.ticks_ms(), 123)
        self.assertEqual(clock.ticks_us(), 456)
        clock = WrapClock(ms=2 ** 30 - 400, us=2 ** 30 - 400000)
        clock.advance(1000)
        self.assertEqual(clock.ticks_ms(), 600)
        self.assertEqual(clock.ticks_us(), 600000)
        self.assertEqual(clock.time(), 1.0)


if __name__ == "__main__":
    unittest.main()
