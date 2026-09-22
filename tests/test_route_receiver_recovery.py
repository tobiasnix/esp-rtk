# SPDX-License-Identifier: AGPL-3.0-only
"""Persist real GGA/GST pairs and reject cold-start fixes across route reloads."""
import asyncio
import os
import tempfile
import unittest
from unittest import mock

from support import gnss, state, sentence
import tracking
from test_gnss_startup import position


GGA = sentence("GNGGA,120010.000,5000.000,N,00800.000,E,4,18,0.7,100.0,M,40.0,M,1.0,0054")
GST = sentence("GNGST,120010.000,0.01,0.02,0.01,0,0.01,0.01,0.02")


class TestRouteRecovery(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = os.path.join(self.directory.name, "route.json")
        self.tracker = tracking.Tracker(self.path)
        self.tracker.create_project("Recovery")
        self.tracker.start_line("Route", 1, recording_mode="route")
        self.app = state.AppState()
        self.handler = gnss.GNSSHandler(object())

    def tearDown(self):
        self.tracker._sync_journal(close=True)

    def test_enrichment_preserves_exactly_one_queued_point(self):
        fix = self.handler.parse_gga(GGA)
        self.assertTrue(self.tracker.enqueue_fix(fix))
        self.assertFalse(self.tracker.enqueue_fix(dict(fix,
            receiver_accuracy=self.handler.parse_gst(GST))))
        self.assertEqual(len(self.tracker._pending_fixes), 1)
        self.tracker.add_vertex(self.tracker._pending_fixes.pop(), source="automatic")
        self.tracker._sync_journal(close=True)
        restored = tracking.Tracker(self.path)
        self.addCleanup(restored._sync_journal, close=True)
        point = restored._line(restored._project())["measurements"][0]
        self.assertTrue(point["quality"]["accepted"])
        self.assertEqual(point["receiver_accuracy"]["source"], "NMEA_GST")
        self.assertAlmostEqual(point["receiver_accuracy"]["horizontal_sigma_m"], .0141421356)

    async def test_worker_waits_for_same_epoch_gst_but_missing_gst_is_bounded(self):
        for attach in (True, False):
            with self.subTest(attach=attach):
                now, stop = [100000], asyncio.Event()
                fix = self.handler.parse_gga(GGA)
                # A second position must clear the configured minimum distance.
                fix.update(utc="with-gst" if attach else "without-gst", lon=8 if attach else 8.001)
                saved, waits = [], []
                original_add = self.tracker.add_vertex

                def save(value, source="manual"):
                    result = original_add(value, source)
                    saved.append((now[0], result))
                    stop.set()
                    return result

                async def sleep(ms):
                    waits.append(ms)
                    now[0] += ms
                    if ms and attach:
                        self.tracker.enqueue_fix(dict(fix,
                            receiver_accuracy=self.handler.parse_gst(GST)))
                    await asyncio.sleep(0)

                with mock.patch.object(tracking.time, "ticks_ms", side_effect=lambda: now[0]), \
                        mock.patch.object(state, "shutdown_event", stop), \
                        mock.patch.object(state, "app", self.app), \
                        mock.patch.object(gnss.asyncio, "sleep_ms", sleep), \
                        mock.patch.object(self.tracker, "add_vertex", side_effect=save):
                    self.assertTrue(self.tracker.enqueue_fix(fix))
                    await asyncio.wait_for(self.tracker.run_worker(), 2)
                self.assertEqual(len(saved), 1)
                self.assertEqual(saved[0][1]["quality"]["accepted"], attach)
                self.assertEqual(saved[0][0] - 100000, 20 if attach else 400)
                self.assertNotIn("_gst_wait_started_ms", saved[0][1])

    async def test_real_parser_accepts_gst_before_and_after_gga(self):
        for before in (True, False):
            with self.subTest(before=before):
                stop = asyncio.Event()
                # Use fresh state and queues for each independent ordering.
                self.app = state.AppState()
                self.tracker._pending_fixes = []
                self.tracker._last_queued_epoch = None
                self.tracker._last_queued_coordinate = None
                handler = gnss.GNSSHandler(state.SimpleQueue(16))
                handler.route_startup = mock.Mock()
                handler.route_startup.check.return_value = True
                handler.route_startup.status.return_value = {"ready": True}
                handler.uart.rx.extend(((GST + "\r\n" + GGA if before else GGA + "\r\n" + GST) + "\r\n").encode())

                async def sleep(ms):
                    if ms:
                        stop.set()
                    await asyncio.sleep(0)

                with mock.patch.object(gnss, "app", self.app), \
                        mock.patch.object(gnss, "shutdown_event", stop), \
                        mock.patch.object(gnss.asyncio, "sleep_ms", sleep), \
                        mock.patch.object(tracking, "tracker", self.tracker), \
                        mock.patch.object(gnss, "log"), \
                        mock.patch.dict(gnss.CONFIG, {"fix_update_interval_ms": 0}):
                    await asyncio.wait_for(handler.run(), 2)
                self.assertEqual(len(self.tracker._pending_fixes), 1)
                queued = self.tracker._pending_fixes[0]
                self.assertTrue(tracking.quality_check(queued)["accepted"])
                self.assertEqual(self.app.stats["gst_output_state"], "receiving")
                self.assertEqual(self.app.stats["crc_errors"], 0)

    def test_two_power_cycles_preserve_route_and_block_cold_start_points(self):
        self.tracker.add_vertex(position(0), source="automatic")
        for cycle in range(2):
            self.tracker._sync_journal(close=True)
            self.tracker = tracking.Tracker(self.path)
            line = self.tracker._line(self.tracker._project())
            before = len(line["coordinates"])
            self.assertEqual(line["state"], "recording")
            guard = gnss.RouteStartupGuard()
            for second in range(5):
                fix = position(second, lat=50.002 if second < 3 else 50.004)
                fix["route_ready"] = guard.check(fix, second * 1000, False)
                self.assertFalse(self.tracker.enqueue_fix(fix))
                self.assertFalse(self.tracker.on_fix(fix))
                with self.assertRaisesRegex(ValueError, "stabilizing"):
                    self.tracker.add_vertex(fix, source="automatic")
            self.assertEqual(len(line["coordinates"]), before)
            for second in range(11):
                fix = position(second, lon=8 + .001 * (cycle + 1))
                fix["route_ready"] = guard.check(fix, 5000 + second * 1000, True)
                self.assertEqual(self.tracker.on_fix(fix), second == 10)
            self.assertEqual(len(line["coordinates"]), before + 1)
            self.assertTrue(all(coordinate[1] == 50 for coordinate in line["coordinates"]))
        self.assertEqual(len(line["coordinates"]), 3)
        orders = [measurement["record_order"] for measurement in line["measurements"]]
        self.assertEqual(orders, sorted(set(orders)))


if __name__ == "__main__":
    unittest.main()
