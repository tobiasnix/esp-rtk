# SPDX-License-Identifier: AGPL-3.0-only
"""Measurement session: sample loop, progress contract, cancel and freshness."""
import asyncio
import contextlib
import copy
import time
import unittest
from unittest import mock

from support import cfg, exact_fix, state
import measurement
import tracking
from measurement import MeasurementSession, sample_marker


# Fixed here on purpose: the browser reads exactly these keys, so the test does
# not derive them from measurement.PROGRESS_KEYS.
EXPECTED_PROGRESS_KEYS = ["active", "collected", "total", "mean_h_sigma_m", "mean_v_sigma_m",
                          "mean_alt_m", "cancelled", "waiting_reasons", "rejected_samples",
                          "mode", "phase", "elapsed_sec"]

GGA = exact_fix({"lat": 50.0, "lon": 8.0, "alt": 100.0, "qual": 4, "sats": 20, "hdop": 0.6,
                 "correction_age_sec": 1.0, "fix_status_text": "RTK_FIXED"})
GST = {"horizontal_sigma_m": .01, "altitude_sigma_m": .02}

_FIX_AND_GATE_FIELDS = ("_last_fix", "_fix_ticks_ms", "_fix_seq", "_fix_rev", "_fix_expired",
                        "_gate_quality", "_gate_accepted", "_gate_changed_ms",
                        "_gate_hold_since_ms", "_gate_receiver_accuracy")


def _save_fix_and_gate():
    return {name: getattr(state.app, name) for name in _FIX_AND_GATE_FIELDS}


def _restore_fix_and_gate(saved):
    for name, value in saved.items():
        setattr(state.app, name, value)


def held_app():
    """An app whose latest fix 120001.00 is published bare and held for its GST."""
    app = state.AppState()
    app.stats["gst_output_state"] = "receiving"
    with mock.patch("tracking.tracker") as tracker:
        tracker.enqueue_fix.return_value = False
        app.update_fix(dict(GGA, utc="120000.00"))
        app.attach_gst(dict(GST, utc="120000.00"))
        app._last_update_ms -= 1000           # the next epoch passes the throttle
        app.update_fix(dict(GGA, utc="120001.00"))
    if not app.gate_state()["pending_gst"]:
        raise AssertionError("the held app does not hold its latest fix for GST")
    return app


def attach_gst(app):
    with mock.patch("tracking.tracker") as tracker:
        tracker.enqueue_fix.return_value = False
        if not app.attach_gst(dict(GST, utc="120001.00")):
            raise AssertionError("the GST of 120001.00 was not attached")


async def collected(session, n):
    """Wait until the running measurement has accepted n samples.

    The measurement polls the latest fix every 250 ms, so a test that swaps the
    fix after a fixed sleep races that poll. Swapping only once the progress
    counter shows the previous sample makes the order deterministic. The first
    pass sleeps before it looks, which lets a measurement task created just
    before reset the counter.
    """
    for _round in range(200):
        await asyncio.sleep(0.02)
        if session.progress["collected"] >= n:
            return
    # What TestCase.fail raises; a module helper has no test case to call it on.
    raise AssertionError("the measurement collected %r of %d samples within 4 s"
                         % (session.progress["collected"], n))


class FakeReceiver:
    """The receiver facade a MeasurementSession reads, without NMEA or a gate."""

    def __init__(self, last_fix=None, stale=False, pending_gst=False):
        self.last_fix = last_fix
        self.stale = stale
        self.pending_gst = pending_gst

    def fix_is_stale(self, now_ms=None):
        return self.stale

    def fix_age_ms(self, now_ms=None):
        return 0

    def gate_state(self, now_ms=None):
        return {"pending_gst": self.pending_gst}


@contextlib.contextmanager
def fake_clock(start_ms, on_sleep=None):
    """Drive time.ticks_ms and measurement.asyncio.sleep_ms from one counter.

    Every sleep_ms(ms) advances the clock by ms, counts the call, calls
    on_sleep(clock) and then yields once to the real event loop.
    """
    clock = {"now": start_ms, "sleeps": 0}
    real_sleep = asyncio.sleep

    async def sleep_ms(ms):
        clock["now"] += ms
        clock["sleeps"] += 1
        if on_sleep is not None:
            on_sleep(clock)
        await real_sleep(0)

    with mock.patch.object(time, "ticks_ms", side_effect=lambda: clock["now"]), \
            mock.patch.object(measurement.asyncio, "sleep_ms", sleep_ms):
        yield clock


class TestMeasurementSession(unittest.TestCase):
    def setUp(self):
        self.session = MeasurementSession()

    def test_progress_contract_exposes_measurement_mode(self):
        self.assertIn(self.session.progress["mode"], ("fixed", "quality"))
        self.assertIn("phase", self.session.progress)
        self.assertIn("elapsed_sec", self.session.progress)

    def test_parallel_measurement_is_rejected_explicitly(self):
        previous_fix = state.app._last_fix
        async def scenario():
            state.app._last_fix = {"utc": "1", "lat": 50.0, "lon": 8.0}
            first = asyncio.create_task(self.session.collect(10))
            await asyncio.sleep(.05)
            with self.assertRaisesRegex(ValueError, "already running"):
                await self.session.collect(5)
            self.session.cancel()
            with self.assertRaisesRegex(ValueError, "cancelled"):
                await first
        try:
            asyncio.run(scenario())
        finally:
            state.app._last_fix = previous_fix

    def test_running_measurement_can_be_cancelled(self):
        previous_fix = state.app._last_fix
        fix = {"utc": "1", "lat": 50.0, "lon": 8.0, "alt": 100.0}

        async def scenario():
            state.app._last_fix = fix
            task = asyncio.create_task(self.session.collect(10))
            await asyncio.sleep(.05)
            self.assertTrue(self.session.progress["active"])
            self.assertEqual(self.session.cancel(), {"cancelled": True})
            with self.assertRaisesRegex(ValueError, "cancelled"):
                await task

        try:
            asyncio.run(scenario())
        finally:
            state.app._last_fix = previous_fix
        self.assertFalse(self.session.progress["active"])
        self.assertTrue(self.session.progress["cancelled"])

    def test_single_sample_action_waits_for_the_pending_gst(self):
        app = held_app()
        self.session = MeasurementSession(app)

        async def scenario():
            started = time.ticks_ms()
            task = asyncio.create_task(self.session.collect(1))
            await asyncio.sleep(.2)
            self.assertFalse(task.done())         # still waiting for the epoch's GST
            attach_gst(app)
            result = await task
            self.assertLess(time.ticks_diff(time.ticks_ms(), started), state.GST_GRACE_MS)
            return result

        result = asyncio.run(scenario())
        self.assertEqual(result["utc"], "120001.00")
        self.assertEqual(result["receiver_accuracy"]["horizontal_sigma_m"], .01)
        self.assertEqual(self.session.progress["phase"], "complete")
        self.assertFalse(self.session.progress["active"])

    def test_single_sample_wait_is_exclusive_and_can_be_cancelled(self):
        app = held_app()
        self.session = MeasurementSession(app)

        async def scenario():
            task = asyncio.create_task(self.session.collect(1))
            await asyncio.sleep(.1)
            self.assertTrue(self.session.progress["active"])
            with self.assertRaisesRegex(ValueError, "already running"):
                await self.session.collect(1)
            self.session.cancel()
            with self.assertRaisesRegex(ValueError, "cancelled"):
                await task

        asyncio.run(scenario())
        self.assertFalse(self.session.progress["active"])
        self.assertEqual(self.session.progress["phase"], "cancelled")

    def test_single_sample_wait_ends_after_the_grace_without_gst(self):
        clock = {"now": 200_000, "sleeps": 0}
        real_sleep = asyncio.sleep

        async def fake_sleep_ms(ms):
            clock["now"] += ms
            clock["sleeps"] += 1
            await real_sleep(0)

        with mock.patch.object(time, "ticks_ms", side_effect=lambda: clock["now"]):
            app = held_app()
            started = clock["now"]
            self.session = MeasurementSession(app)
            with mock.patch.object(measurement.asyncio, "sleep_ms", fake_sleep_ms):
                result = asyncio.run(self.session.collect(1))
        self.assertEqual(result["utc"], "120001.00")
        self.assertNotIn("receiver_accuracy", result)
        self.assertEqual(clock["now"] - started, state.GST_GRACE_MS)
        self.assertEqual(clock["sleeps"], state.GST_GRACE_MS // 50)
        self.assertEqual(self.session.progress["phase"], "complete")
        self.assertFalse(self.session.progress["active"])

    def test_single_sample_refuses_a_fix_that_went_stale_while_the_loop_stalled(self):
        clock = {"now": 200_000}
        real_sleep = asyncio.sleep
        app_ref = {}

        async def stalled_sleep_ms(ms):
            clock["now"] += 100
            attach_gst(app_ref["app"])                # GST arrives in time ...
            clock["now"] += 3400                      # ... but the loop resumes late
            await real_sleep(0)

        with mock.patch.object(time, "ticks_ms", side_effect=lambda: clock["now"]):
            app = app_ref["app"] = held_app()
            self.session = MeasurementSession(app)
            with mock.patch.object(measurement.asyncio, "sleep_ms", stalled_sleep_ms):
                with self.assertRaisesRegex(ValueError, "stale"):
                    asyncio.run(self.session.collect(1))
        self.assertEqual(self.session.progress["phase"], "failed")
        self.assertFalse(self.session.progress["active"])

    def test_single_sample_does_not_wait_during_a_gst_dropout(self):
        clock = {"now": 300_000, "sleeps": 0}
        real_sleep = asyncio.sleep

        async def fake_sleep_ms(ms):
            clock["now"] += ms
            clock["sleeps"] += 1
            await real_sleep(0)

        with mock.patch.object(time, "ticks_ms", side_effect=lambda: clock["now"]):
            app = held_app()                          # hold from 120001.00
            with mock.patch("tracking.tracker") as tracker:
                tracker.enqueue_fix.return_value = False
                for utc in ("120002.00", "120003.00"):
                    clock["now"] += 1000
                    app.update_fix(dict(GGA, utc=utc))
            self.assertEqual(app.gate_state()["quality"]["reasons"], ["gst_required"])
            self.session = MeasurementSession(app)
            with mock.patch.object(measurement.asyncio, "sleep_ms", fake_sleep_ms):
                result = asyncio.run(self.session.collect(1))
        self.assertEqual(result["utc"], "120003.00")
        self.assertEqual(clock["sleeps"], 0)

    def test_counts_distinct_gnss_samples(self):
        previous_fix = state.app._last_fix
        fix = exact_fix({"utc": "120000.00", "lat": 50.0, "lon": 8.0, "alt": 100.0,
                         "qual": 4, "sats": 20, "hdop": 0.6, "correction_age_sec": 1.0,
                         "fix_status_text": "RTK_FIXED",
                         "receiver_accuracy": {"horizontal_sigma_m": .01,
                                               "altitude_sigma_m": .02}})

        async def scenario():
            state.app._last_fix = dict(fix)
            task = asyncio.create_task(self.session.collect(3))
            await collected(self.session, 1)
            self.assertTrue(self.session.progress["active"])
            self.assertEqual(self.session.progress["collected"], 1)
            for index in (1, 2):
                await collected(self.session, index)
                state.app._last_fix = exact_fix(dict(fix, utc="12000%d.00" % index,
                                                     lat=50.0 + index * 0.000001))
            await collected(self.session, 3)
            result = await task
            self.assertEqual(result["accuracy_observation"]["samples"], 3)
            self.assertIn("duration_sec", result["accuracy_observation"])

        try:
            asyncio.run(scenario())
        finally:
            state.app._last_fix = previous_fix
        self.assertFalse(self.session.progress["active"])
        self.assertEqual(self.session.progress["collected"], 3)
        self.assertEqual(self.session.progress["total"], 3)

    def test_distinct_samples_are_told_apart_by_e8_fields(self):
        previous_fix = state.app._last_fix
        fix = {"utc": "120000.00", "lat": 50.0, "lon": 8.0, "alt": 100.0,
               "lat_e8": 5000000000, "lon_e8": 800000000, "alt_mm": 100000,
               "qual": 4, "sats": 20, "hdop": 0.6, "correction_age_sec": 1.0,
               "fix_status_text": "RTK_FIXED",
               "receiver_accuracy": {"horizontal_sigma_m": .01, "altitude_sigma_m": .02}}

        async def scenario():
            state.app._last_fix = dict(fix)
            task = asyncio.create_task(self.session.collect(3))
            for index in (1, 2):
                await collected(self.session, index)
                # Same float position, same UTC: only the exact field moves.
                state.app._last_fix = dict(fix, lat_e8=5000000000 + index)
            await collected(self.session, 3)
            result = await task
            self.assertEqual(result["accuracy_observation"]["samples"], 3)
            self.assertEqual(result["lat_e8"], 5000000001)

        try:
            asyncio.run(scenario())
        finally:
            state.app._last_fix = previous_fix

    def test_samples_differing_only_in_the_exact_longitude_are_told_apart(self):
        previous_fix = state.app._last_fix
        fix = {"utc": "120000.00", "lat": 50.0, "lon": 8.0, "alt": 100.0,
               "lat_e8": 5000000000, "lon_e8": 800000000, "alt_mm": 100000,
               "qual": 4, "sats": 20, "hdop": 0.6, "correction_age_sec": 1.0,
               "fix_status_text": "RTK_FIXED",
               "receiver_accuracy": {"horizontal_sigma_m": .01, "altitude_sigma_m": .02}}

        async def scenario():
            state.app._last_fix = dict(fix)
            task = asyncio.create_task(self.session.collect(2))
            await collected(self.session, 1)
            # Same UTC, same latitude, same floats: only the exact longitude moves.
            state.app._last_fix = dict(fix, lon_e8=800000002)
            await collected(self.session, 2)
            result = await task
            self.assertEqual(result["accuracy_observation"]["samples"], 2)
            self.assertEqual(result["lon_e8"], 800000001)

        try:
            asyncio.run(scenario())
        finally:
            state.app._last_fix = previous_fix

    def test_samples_without_exact_fields_are_not_collected(self):
        # Floats alone no longer pass the gate, so nothing is collected and the
        # failure names the gate reason instead of failing later at storage.
        session = MeasurementSession(FakeReceiver(last_fix=dict(
            GGA, utc="1", lat_e8=None, lon_e8=None, receiver_accuracy=dict(GST))))
        with fake_clock(100_000):
            with self.assertRaisesRegex(
                    ValueError, r"^Not enough valid GNSS samples were received: .*no_position"):
                asyncio.run(session.collect(2))
        self.assertEqual(session.progress["collected"], 0)

    def test_not_enough_samples_names_the_reason_codes(self):
        session = MeasurementSession(FakeReceiver(last_fix=dict(
            GGA, utc="1", lat_e8=None, lon_e8=None, receiver_accuracy=dict(GST))))
        with fake_clock(100_000):
            with self.assertRaises(tracking.MeasurementRejected) as caught:
                asyncio.run(session.collect(2))
        self.assertIsInstance(caught.exception, ValueError)
        self.assertTrue(caught.exception.reasons)
        self.assertEqual(caught.exception.reasons, tuple(session.progress["waiting_reasons"]))
        self.assertTrue(str(caught.exception).startswith(
            "Not enough valid GNSS samples were received: "))

    def test_waits_for_same_epoch_gst_before_counting_sample(self):
        previous_fix = state.app._last_fix
        base = exact_fix({"utc": "1", "lat": 50.0, "lon": 8.0, "alt": 100.0,
                          "qual": 4, "sats": 20, "hdop": .6,
                          "correction_age_sec": 1.0, "fix_status_text": "RTK_FIXED"})
        accuracy = {"horizontal_sigma_m": .01, "altitude_sigma_m": .02}

        async def scenario():
            state.app._last_fix = dict(base)
            task = asyncio.create_task(self.session.collect(2))
            await asyncio.sleep(.05)
            self.assertEqual(self.session.progress["collected"], 0)
            self.assertIn("gst_required", self.session.progress["waiting_reasons"])
            state.app._last_fix = dict(base, receiver_accuracy=accuracy)
            await asyncio.sleep(.3)
            self.assertEqual(self.session.progress["collected"], 1)
            state.app._last_fix = exact_fix(dict(base, utc="2", lat=50.0000001,
                                                 receiver_accuracy=accuracy))
            await asyncio.sleep(.3)
            result = await task
            self.assertEqual(result["receiver_accuracy"]["samples"], 2)

        try:
            asyncio.run(scenario())
        finally:
            state.app._last_fix = previous_fix

    def test_reports_running_sigma_and_altitude_means(self):
        previous_fix = state.app._last_fix
        base = exact_fix({"utc": "1", "lat": 50.0, "lon": 8.0, "alt": 100.0,
                          "qual": 4, "sats": 20, "hdop": .6,
                          "receiver_accuracy": {"horizontal_sigma_m": .01,
                                                "altitude_sigma_m": .02}})
        async def scenario():
            state.app._last_fix = dict(base)
            task = asyncio.create_task(self.session.collect(2))
            await asyncio.sleep(.05)
            state.app._last_fix = exact_fix(dict(base, utc="2", alt=102.0,
                receiver_accuracy={"horizontal_sigma_m": .03,
                                   "altitude_sigma_m": .04}))
            await asyncio.sleep(.3)
            return await task
        try:
            result = asyncio.run(scenario())
        finally:
            state.app._last_fix = previous_fix
        self.assertEqual(self.session.progress["mean_h_sigma_m"], .02)
        self.assertEqual(self.session.progress["mean_v_sigma_m"], .03)
        self.assertEqual(self.session.progress["mean_alt_m"], 101.0)
        # Worked by hand: horizontal (.01 + .03) / 2, vertical (.02 + .04) / 2.
        self.assertEqual(result["receiver_accuracy"],
                         {"source": "NMEA_GST", "samples": 2, "expected_samples": 2,
                          "horizontal_sigma_mean_m": .02, "horizontal_sigma_max_m": .03,
                          "vertical_sigma_mean_m": .03, "vertical_sigma_max_m": .04})

    def test_quality_mode_uses_a_rolling_window_until_spread_is_accepted(self):
        previous_fix = state.app._last_fix
        base = exact_fix({"utc": "0", "lat": 50.001, "lon": 8.001, "alt": 100.0,
                          "qual": 4, "sats": 20, "hdop": .6,
                          "correction_age_sec": 1.0, "fix_status_text": "RTK_FIXED",
                          "receiver_accuracy": {"horizontal_sigma_m": .01,
                                                "altitude_sigma_m": .02}})

        async def scenario():
            state.app._last_fix = dict(base)
            task = asyncio.create_task(self.session.collect("quality"))
            await asyncio.sleep(.05)
            for index in range(1, 6):
                state.app._last_fix = exact_fix(dict(base, utc=str(index),
                    lat=50.0 + index * .00000001,
                    lon=8.0 + index * .00000001))
                await asyncio.sleep(.3)
            result = await task
            self.assertEqual(result["accuracy_observation"]["samples"], 5)
            self.assertLessEqual(
                result["accuracy_observation"]["horizontal_max_deviation_m"], .03)

        try:
            asyncio.run(scenario())
        finally:
            state.app._last_fix = previous_fix
        self.assertFalse(self.session.progress["active"])
        self.assertEqual(self.session.progress["mode"], "quality")
        self.assertIsNone(self.session.progress["total"])
        self.assertEqual(self.session.progress["collected"], 6)

    def test_quality_mode_uses_configured_window(self):
        previous_fix = state.app._last_fix
        previous_window = cfg.CONFIG.get("measurement_quality_window_samples")
        base = exact_fix({"utc": "0", "lat": 50.001, "lon": 8.001, "alt": 100.0,
                          "qual": 4, "sats": 20, "hdop": .6,
                          "correction_age_sec": 1.0, "fix_status_text": "RTK_FIXED",
                          "receiver_accuracy": {"horizontal_sigma_m": .01,
                                                "altitude_sigma_m": .02}})
        async def scenario():
            cfg.CONFIG["measurement_quality_window_samples"] = 3
            state.app._last_fix = dict(base)
            task = asyncio.create_task(self.session.collect("quality"))
            await asyncio.sleep(.05)
            for index in range(1, 3):
                state.app._last_fix = dict(base, utc=str(index))
                await asyncio.sleep(.3)
            return await task
        try:
            result = asyncio.run(scenario())
            self.assertEqual(result["accuracy_observation"]["samples"], 3)
        finally:
            state.app._last_fix = previous_fix
            cfg.CONFIG["measurement_quality_window_samples"] = previous_window

    def test_single_sample_resets_live_means(self):
        previous_fix = state.app._last_fix
        state.app._last_fix = {"lat": 1, "lon": 2, "alt": 3}
        self.session.progress["mean_alt_m"] = 999
        try:
            asyncio.run(self.session.collect(1))
        finally:
            state.app._last_fix = previous_fix
        self.assertFalse(self.session.progress["active"])
        self.assertEqual(self.session.progress["total"], 1)
        self.assertIsNone(self.session.progress["mean_alt_m"])

    def assert_contract(self, progress):
        self.assertEqual(sorted(progress), sorted(EXPECTED_PROGRESS_KEYS))
        self.assertEqual(len(progress), 12)

    def test_progress_contract_by_state(self):
        with self.subTest(state="idle"):
            progress = MeasurementSession().progress
            self.assert_contract(progress)
            self.assertEqual(progress, {
                "active": False, "collected": 0, "total": 0, "mean_h_sigma_m": None,
                "mean_v_sigma_m": None, "mean_alt_m": None, "cancelled": False,
                "waiting_reasons": [], "rejected_samples": 0, "mode": "fixed",
                "phase": "idle", "elapsed_sec": 0})

        fix = dict(GGA, utc="1", alt=100.0, receiver_accuracy=dict(GST))

        async def collecting():
            session = MeasurementSession(FakeReceiver(last_fix=dict(fix)))
            task = asyncio.create_task(session.collect(3))
            await collected(session, 1)
            progress = dict(session.progress)
            session.cancel()
            with self.assertRaisesRegex(ValueError, "cancelled"):
                await task
            return progress, dict(session.progress)

        progress, cancelled = asyncio.run(collecting())
        with self.subTest(state="collecting"):
            self.assert_contract(progress)
            self.assertIs(progress["active"], True)
            self.assertEqual(progress["collected"], 1)
            self.assertEqual(progress["total"], 3)
            self.assertEqual(progress["mean_h_sigma_m"], .01)
            self.assertEqual(progress["mean_v_sigma_m"], .02)
            self.assertEqual(progress["mean_alt_m"], 100.0)
            self.assertIs(progress["cancelled"], False)
            self.assertEqual(progress["waiting_reasons"], [])
            self.assertEqual(progress["rejected_samples"], 0)
            self.assertEqual(progress["mode"], "fixed")
            self.assertEqual(progress["phase"], "collecting")
            self.assertIsInstance(progress["elapsed_sec"], (int, float))
        with self.subTest(state="cancelled"):
            self.assert_contract(cancelled)
            self.assertIs(cancelled["active"], False)
            self.assertIs(cancelled["cancelled"], True)
            self.assertEqual(cancelled["phase"], "cancelled")

        async def quality():
            session = MeasurementSession(FakeReceiver(last_fix=dict(fix)))
            task = asyncio.create_task(session.collect("quality"))
            await asyncio.sleep(.05)
            progress = dict(session.progress)
            session.cancel()
            with self.assertRaisesRegex(ValueError, "cancelled"):
                await task
            return progress

        progress = asyncio.run(quality())
        with self.subTest(state="quality"):
            self.assert_contract(progress)
            self.assertIsNone(progress["total"])
            self.assertEqual(progress["mode"], "quality")
            self.assertIn(progress["phase"], ("waiting_fix", "collecting"))

        session = MeasurementSession(FakeReceiver(last_fix=dict(GGA, utc="1")))
        with fake_clock(100_000):
            with self.assertRaisesRegex(ValueError, "Not enough valid GNSS samples"):
                asyncio.run(session.collect(2))
        with self.subTest(state="failed"):
            progress = session.progress
            self.assert_contract(progress)
            self.assertIs(progress["active"], False)
            self.assertEqual(progress["phase"], "failed")
            self.assertEqual(progress["collected"], 0)
            self.assertEqual(progress["total"], 2)
            self.assertEqual(progress["waiting_reasons"], ["gst_required"])
            self.assertEqual(progress["rejected_samples"], 1)
            self.assertIsInstance(progress["waiting_reasons"], list)
            self.assertIsInstance(progress["rejected_samples"], int)

        async def complete():
            receiver = FakeReceiver(last_fix=dict(fix))
            session = MeasurementSession(receiver)
            task = asyncio.create_task(session.collect(2))
            await collected(session, 1)
            receiver.last_fix = exact_fix(dict(fix, utc="2", lat=fix["lat"] + 1e-6))
            await task
            return session.progress

        progress = asyncio.run(complete())
        with self.subTest(state="complete"):
            self.assert_contract(progress)
            self.assertIs(progress["active"], False)
            self.assertEqual(progress["phase"], "complete")
            self.assertEqual(progress["collected"], 2)
            self.assertEqual(progress["total"], 2)

    def test_fixed_mode_success_reports_complete(self):
        fix = dict(GGA, utc="1", receiver_accuracy=dict(GST))
        receiver = FakeReceiver(last_fix=dict(fix))
        session = MeasurementSession(receiver)

        async def scenario():
            task = asyncio.create_task(session.collect(2))
            await collected(session, 1)
            receiver.last_fix = exact_fix(dict(fix, utc="2", lat=fix["lat"] + 1e-6))
            return await task

        result = asyncio.run(scenario())
        self.assertEqual(result["accuracy_observation"]["samples"], 2)
        self.assertIs(session.progress["active"], False)
        self.assertEqual(session.progress["phase"], "complete")
        self.assertEqual(session.progress["collected"], 2)
        self.assertEqual(session.progress["total"], 2)

    def test_sample_marker_uses_the_exact_fields_only(self):
        self.assertEqual(sample_marker({"utc": "1", "lat": 50.0, "lon": 8.0,
                                        "lat_e8": 5000000000, "lon_e8": None}),
                         ("1", 5000000000, None))
        self.assertEqual(sample_marker({"utc": "1", "lat": 50.0, "lon": 8.0}),
                         ("1", None, None))
        self.assertEqual(sample_marker({"utc": "1", "lat": 50.0, "lon": 8.0,
                                        "lat_e8": 0, "lon_e8": 0}), ("1", 0, 0))
        self.assertEqual(sample_marker({}), (None, None, None))

    def test_fixed_mode_fails_after_the_deadline_naming_the_reasons(self):
        session = MeasurementSession(FakeReceiver(last_fix=dict(GGA, utc="1")))
        with fake_clock(100_000) as clock:
            with self.assertRaisesRegex(
                    ValueError, r"^Not enough valid GNSS samples were received: .*gst_required"):
                asyncio.run(session.collect(2))
        self.assertGreaterEqual(clock["now"] - 100_000, 15000)
        self.assertLessEqual(clock["now"] - 100_000, 15250)

    def test_rejected_samples_count_each_distinct_marker_once(self):
        receiver = FakeReceiver(last_fix=dict(GGA, utc="1"))
        session = MeasurementSession(receiver)

        def next_epoch_after_two_polls(clock):
            if clock["sleeps"] == 2:
                receiver.last_fix = dict(GGA, utc="2")

        with fake_clock(100_000, next_epoch_after_two_polls):
            with self.assertRaisesRegex(ValueError, "Not enough valid GNSS samples"):
                asyncio.run(session.collect(2))
        self.assertEqual(session.progress["rejected_samples"], 2)

    def test_sessions_are_independent(self):
        fix = dict(GGA, utc="1", receiver_accuracy=dict(GST))

        async def scenario():
            first = MeasurementSession(FakeReceiver(last_fix=dict(fix)))
            second = MeasurementSession(FakeReceiver(last_fix=dict(fix)))
            running = asyncio.create_task(first.collect(10))
            await asyncio.sleep(.05)
            self.assertTrue(first.active())
            result = await second.collect(1)
            self.assertEqual(result["utc"], "1")
            self.assertEqual(second.cancel(), {"cancelled": True})
            self.assertIs(first.progress["active"], True)
            first.cancel()
            with self.assertRaisesRegex(ValueError, "cancelled"):
                await running

        asyncio.run(scenario())


class TestSessionGeneration(unittest.TestCase):
    """A superseded measurement loop reports "cancelled" and writes nothing."""

    def test_snapshot_is_a_copy_of_the_progress(self):
        session = MeasurementSession()
        snapshot = session.snapshot()
        self.assertEqual(snapshot, session.progress)
        self.assertIsNot(snapshot, session.progress)
        snapshot["phase"] = "tampered"
        snapshot["waiting_reasons"] = ["x"]
        self.assertEqual(session.progress["phase"], "idle")
        self.assertEqual(session.progress["waiting_reasons"], [])

    def test_snapshot_copies_the_waiting_reasons(self):
        session = MeasurementSession()
        session.progress["waiting_reasons"] = ["gst_required"]
        snapshot = session.snapshot()
        snapshot["waiting_reasons"].append("tampered")
        self.assertEqual(session.progress["waiting_reasons"], ["gst_required"])

    def test_progress_keys_are_the_single_source_of_the_shape(self):
        self.assertEqual(list(measurement.PROGRESS_KEYS), EXPECTED_PROGRESS_KEYS)
        self.assertEqual(list(MeasurementSession().progress), EXPECTED_PROGRESS_KEYS)
        # Every session owns its own waiting_reasons list.
        first, second = MeasurementSession(), MeasurementSession()
        self.assertIsNot(first.progress["waiting_reasons"], second.progress["waiting_reasons"])

    def test_the_cleaner_fires_while_a_restarted_measurement_runs(self):
        receiver = FakeReceiver(last_fix=dict(GGA, utc="1"))      # no GST: never accepted
        session = MeasurementSession(receiver)
        real_sleep = asyncio.sleep

        async def quick(seconds):
            # Only the cleaner's 5 s wait is instant; nothing else may be sped up.
            await real_sleep(0 if seconds == 5 else seconds)

        async def scenario():
            first = asyncio.create_task(session.collect(10))
            await real_sleep(.05)
            session.cancel()                  # arms the cleaner (its 5 s wait is instant)
            second = asyncio.create_task(session.collect(10))
            # Let the cleaner and the new measurement run, then look at once.
            await real_sleep(0)
            await real_sleep(0)
            self.assertIs(session.progress["active"], True)
            self.assertEqual(session.progress["phase"], "waiting_fix")
            self.assertIs(session.progress["cancelled"], False)
            session.cancel()
            for task in (first, second):
                with self.assertRaisesRegex(ValueError, "cancelled"):
                    await task

        with mock.patch.object(measurement.asyncio, "sleep", quick):
            asyncio.run(scenario())

    def test_a_cancelled_loop_does_not_overwrite_the_next_measurement(self):
        receiver = FakeReceiver(last_fix=dict(GGA, utc="1"))      # no GST: never accepted
        session = MeasurementSession(receiver)

        async def scenario():
            first = asyncio.create_task(session.collect(10))
            await asyncio.sleep(.05)
            session.cancel()
            second = asyncio.create_task(session.collect(10))
            await asyncio.sleep(0)
            with self.assertRaisesRegex(ValueError, "cancelled"):
                await first
            self.assertTrue(session.progress["active"])
            self.assertFalse(session.progress["cancelled"])
            self.assertEqual(session.progress["phase"], "waiting_fix")
            with self.assertRaisesRegex(ValueError, "already running"):
                await session.collect(1)
            session.cancel()
            with self.assertRaisesRegex(ValueError, "cancelled"):
                await second

        asyncio.run(scenario())

    def test_cancel_and_restart_as_the_deadline_expires(self):
        session = MeasurementSession(FakeReceiver(last_fix=dict(GGA, utc="1")))
        clock = {"now": 100_000}
        runs = {}
        real_sleep = asyncio.sleep

        async def scenario():
            # Events, not the order of the clock, decide who runs when.
            second_rejected, hold_second = asyncio.Event(), asyncio.Event()

            async def sleep_ms(ms):
                task = asyncio.current_task()
                if task is runs.get("second"):      # second run: held after its first rejection
                    second_rejected.set()
                    await hold_second.wait()
                    return
                clock["now"] += ms                  # only the first run advances the clock
                if "second" not in runs and clock["now"] - 100_000 >= 15000:
                    session.cancel()                # cancel + restart exactly at the deadline
                    runs["second"] = asyncio.create_task(session.collect(10))
                    await second_rejected.wait()    # second ran until its first rejection
                    runs["snapshot"] = copy.deepcopy(session.progress)
                await real_sleep(0)                 # wake the old loop

            with mock.patch.object(measurement.asyncio, "sleep_ms", sleep_ms):
                runs["first"] = asyncio.create_task(session.collect(2))
                with self.assertRaisesRegex(ValueError, r"^Measurement cancelled\.$"):
                    await runs["first"]
                self.assertEqual(session.progress, runs["snapshot"])
                self.assertIs(session.progress["active"], True)
                self.assertEqual(session.progress["total"], 10)
                self.assertEqual(session.progress["phase"], "waiting_fix")
                self.assertEqual(session.progress["rejected_samples"], 1)
                self.assertIs(session.progress["cancelled"], False)
                session.cancel()
                hold_second.set()
                with self.assertRaisesRegex(ValueError, "cancelled"):
                    await runs["second"]
                self.assertEqual(session.progress["phase"], "cancelled")

        with mock.patch.object(time, "ticks_ms", side_effect=lambda: clock["now"]):
            asyncio.run(scenario())

    def test_cancel_as_the_gst_grace_expires_does_not_return_the_fix(self):
        session = MeasurementSession(FakeReceiver(pending_gst=True, last_fix=dict(GGA, utc="1")))
        clock = {"now": 100_000}
        real_sleep = asyncio.sleep

        async def sleep_ms(ms):
            clock["now"] += 50
            if clock["now"] - 100_000 == state.GST_GRACE_MS:
                session.cancel()                    # the wait would end with this round
            await real_sleep(0)

        with mock.patch.object(time, "ticks_ms", side_effect=lambda: clock["now"]), \
                mock.patch.object(measurement.asyncio, "sleep_ms", sleep_ms):
            with self.assertRaisesRegex(ValueError, r"^Measurement cancelled\.$"):
                asyncio.run(session.collect(1))
        self.assertEqual(clock["now"] - 100_000, state.GST_GRACE_MS)
        self.assertEqual(session.progress["phase"], "cancelled")

    def test_cancelled_state_clears_unless_a_new_measurement_followed(self):
        session = MeasurementSession(
            FakeReceiver(last_fix=dict(GGA, utc="1", receiver_accuracy=dict(GST))))
        real_sleep = asyncio.sleep

        async def quick(_seconds):
            await real_sleep(0)

        async def scenario():
            session.cancel()
            self.assertEqual(session.progress["phase"], "cancelled")
            await real_sleep(.01)
            self.assertEqual(session.progress["phase"], "idle")
            self.assertIs(session.progress["cancelled"], False)
            session.cancel()
            await session.collect(1)
            await real_sleep(.01)
            self.assertEqual(session.progress["phase"], "complete")

        with mock.patch.object(measurement.asyncio, "sleep", quick):
            asyncio.run(scenario())


class TestStaleFix(unittest.TestCase):
    def setUp(self):
        self.previous = _save_fix_and_gate()
        self.now = 50_000
        patcher = mock.patch.object(time, "ticks_ms", side_effect=lambda: self.now)
        patcher.start(); self.addCleanup(patcher.stop)
        state.app._last_fix = exact_fix({
            "utc": "1", "lat": 50.0, "lon": 8.0, "alt": 100.0, "qual": 4,
            "sats": 20, "hdop": 0.6, "correction_age_sec": 1.0,
            "fix_status_text": "RTK_FIXED",
            "receiver_accuracy": {"horizontal_sigma_m": .01, "altitude_sigma_m": .02}})
        state.app._fix_ticks_ms, state.app._fix_seq, state.app._fix_rev = self.now, 7, 2
        state.app._fix_expired = False
        state.app.reevaluate_gate()
        self.session = MeasurementSession()

    def tearDown(self):
        _restore_fix_and_gate(self.previous)

    def test_single_sample_measurement_refuses_a_stale_fix(self):
        self.now += 3000
        with self.assertRaisesRegex(ValueError, r"stale \(no GGA for 3 s\)"):
            asyncio.run(self.session.collect(1))
        self.assertFalse(self.session.progress["active"])
        self.assertEqual(self.session.progress["phase"], "failed")

    def test_running_measurement_aborts_when_the_fix_goes_stale(self):
        for mode in (3, "quality"):
            async def scenario():
                task = asyncio.create_task(self.session.collect(mode))
                await asyncio.sleep(0.05)
                self.now += 3000
                with self.assertRaisesRegex(ValueError, "stale"):
                    await asyncio.wait_for(task, 5)
            asyncio.run(scenario())
            self.assertFalse(self.session.progress["active"])
            self.assertEqual(self.session.progress["phase"], "failed")
            self.now = 50_000


if __name__ == "__main__":
    unittest.main()
