# SPDX-License-Identifier: AGPL-3.0-only
import json
import asyncio
import os
import tempfile
import unittest
from unittest import mock

from support import state  # noqa: F401
import parallel_benchmark as benchmark
import tracking
from tracking import Tracker


class TestParallelBenchmark(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="esp-rtk-parallel-")
        self.config = os.path.join(self.tmp.name, "parallel.json")
        self.prefix = os.path.join(self.tmp.name, "parallel-data")
        self.result = self.prefix + ".result.json"
        self.patchers = [
            mock.patch.object(benchmark, "CONFIG_PATH", self.config),
            mock.patch.object(benchmark, "PREFIX", self.prefix),
            mock.patch.object(benchmark, "RESULT_PATH", self.result),
            # Unit tests must not depend on the runner's current disk usage.
            # The production reserve calculation itself remains exercised with
            # a deterministic 4 KiB-block volume well above the 20 % reserve.
            mock.patch.object(tracking.os, "statvfs", return_value=(
                4096, 4096, 1000000, 900000, 900000, 0, 0, 0, 0, 255)),
        ]
        for patcher in self.patchers: patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patchers): patcher.stop()
        self.tmp.cleanup()

    def write_config(self, **changes):
        value = {"kind": "parallel_tracking_benchmark", "schema": 1,
                 "enabled": True, "target_points": 10000,
                 "interval_ms": 1000}
        value.update(changes)
        with open(self.config, "w", encoding="utf-8") as target:
            json.dump(value, target)

    def test_config_requires_fixed_one_hertz_and_allowed_target(self):
        self.write_config(target_points=20000)
        self.assertEqual(benchmark.load_config()["target_points"], 20000)
        self.write_config(interval_ms=500)
        self.assertIsNone(benchmark.load_config())
        self.write_config(target_points=35000)
        self.assertIsNone(benchmark.load_config())

    def test_profiles_are_explicit_and_invalid_profiles_are_rejected(self):
        self.write_config()
        self.assertEqual(benchmark.load_config()['profile'], 'parallel')
        for profile in benchmark.PROFILES:
            self.write_config(profile=profile)
            self.assertEqual(benchmark.load_config()['profile'], profile)
        for profile in ('unknown', [], None):
            self.write_config(profile=profile)
            self.assertIsNone(benchmark.load_config())

    def test_local_profile_requires_live_core_services_without_external_receivers(self):
        self.write_config(profile='local')
        production = Tracker(os.path.join(self.tmp.name, 'production'))
        tracker, _ = benchmark.prepare(production)
        now = benchmark.time.ticks_ms()
        for key in ('wifi', 'gnss', 'ntrip', 'http'):
            tracker._parallel_evidence[key] = True
            tracker._parallel_last_progress[key] = now
        current = dict(tracker._parallel_baseline)
        with mock.patch.object(benchmark, '_update_service_evidence', return_value=current):
            self.assertEqual(benchmark._gate_state(tracker), 'complete')
            self.assertFalse(tracker._parallel_evidence['ble'])
            self.assertFalse(tracker._parallel_evidence['tcp'])
            tracker._parallel_last_progress['ntrip'] = now - benchmark.SERVICE_GRACE_MS - 1
            self.assertEqual(benchmark._gate_state(tracker), 'services_incomplete')
            tracker._parallel_last_progress['ntrip'] = now
            current['nmea_queue_overflows'] += 1
            self.assertEqual(benchmark._gate_state(tracker), 'runtime_errors')
        benchmark._write_result(tracker, 'runtime_errors', now)
        with open(self.result) as source:
            result = json.load(source)
        self.assertEqual(result['profile'], 'local')
        self.assertEqual(set(result['required_services']), {'wifi', 'gnss', 'ntrip', 'http'})
        self.assertFalse(result['qualification_valid'])

    def test_valid_previous_config_is_recovered(self):
        self.write_config(target_points=15000)
        os.rename(self.config, self.config + ".previous")
        self.assertEqual(benchmark.load_config()["target_points"], 15000)
        self.assertTrue(os.path.exists(self.config))

    def test_prepare_refuses_any_real_inventory(self):
        self.write_config()
        production = Tracker(os.path.join(self.tmp.name, "production"))
        production.create_project("Existing project")
        tracker, config = benchmark.prepare(production)
        self.assertIsNone(tracker)
        self.assertIsNone(config)
        self.assertEqual(benchmark.app.stats["parallel_benchmark_state"],
                         "refused_real_inventory")

    def test_prepare_creates_isolated_resumable_tracker(self):
        self.write_config(target_points=20000)
        production = Tracker(os.path.join(self.tmp.name, "production"))
        tracker, config = benchmark.prepare(production)
        self.assertEqual(config["target_points"], 20000)
        self.assertEqual(tracker._feature_limit(), 20000)
        self.assertEqual(tracker.feature_count(), 0)
        self.assertTrue(tracker.active_line_id)
        tracker.add_vertex(benchmark._synthetic_fix(1), source="automatic")
        tracker.point_store.close()
        resumed, _ = benchmark.prepare(production)
        self.assertEqual(resumed.feature_count(), 1)

    def test_result_contains_only_metrics_and_is_verified(self):
        self.write_config(target_points=5000)
        production = Tracker(os.path.join(self.tmp.name, "production"))
        tracker, _ = benchmark.prepare(production)
        benchmark._write_result(tracker, "complete", benchmark.time.ticks_ms())
        with open(self.result, encoding="utf-8") as source:
            result = json.load(source)
        self.assertEqual(result["target_points"], 5000)
        self.assertNotIn("coordinate", result)
        self.assertFalse(os.path.exists(self.result + ".tmp"))

    def test_crc_diagnostics_and_uart_metrics_survive_benchmark_result_reload(self):
        from support import gnss
        self.write_config(target_points=10000, profile="local")
        with mock.patch.dict(benchmark.app.stats, {"crc_errors": 0,
                "nmea_last_crc_error": None, "uart_rx_bytes": 0}, clear=True):
            production = Tracker(os.path.join(self.tmp.name, "production"))
            tracker, _ = benchmark.prepare(production)
            handler = gnss.GNSSHandler(object())
            handler._record_uart_read(16000, b"x" * 16000, 100, 105)
            handler._record_crc_error(b"$GNRMC,damaged*ZZ\r", "RMC", False, 20)
            handler._append_uart_chunk(b"", b"$" + b"x" * gnss.MAX_NMEA_BUFFER_BYTES)
            expected = dict(benchmark.app.stats["nmea_last_crc_error"])
            self.assertEqual(benchmark._error_deltas(tracker)["nmea_crc_errors"], 1)
            benchmark._write_result(tracker, "runtime_errors", benchmark.time.ticks_ms())
            # Simulate the volatile counters being cleared by the planned reboot.
            benchmark.app.stats.clear()
            restored = benchmark.control_status()["result"]
            self.assertEqual(restored["nmea_last_crc_error"], expected)
            self.assertEqual(restored["uart_diagnostics"]["uart_rx_bytes"], 16000)
            self.assertEqual(restored["uart_diagnostics"]["uart_rx_available_max"], 16000)
            self.assertEqual(restored["uart_diagnostics"]["uart_last_buffer_overflow"]
                             ["discarded_bytes"], gnss.MAX_NMEA_BUFFER_BYTES + 1)
            self.assertEqual(restored["error_deltas"]["nmea_crc_errors"], 1)
            self.assertEqual(restored["error_deltas"]["uart_buffer_overflows"], 1)
            self.assertFalse(restored["qualification_valid"])
            tracker.point_store.close()
            production.point_store.close()

    def test_gate_requires_every_service_and_no_new_errors(self):
        self.write_config(target_points=5000)
        production = Tracker(os.path.join(self.tmp.name, "production"))
        tracker, _ = benchmark.prepare(production)
        tracker._parallel_evidence = dict((key, True) for key in
            ("wifi", "gnss", "ntrip", "ble", "tcp", "http"))
        with mock.patch.object(benchmark, "_update_service_evidence",
                               return_value=dict(tracker._parallel_baseline)), \
                mock.patch.object(benchmark, "_services_live", return_value=True):
            self.assertEqual(benchmark._gate_state(tracker), "complete")
        current = dict(tracker._parallel_baseline)
        current["task_restarts"] += 1
        with mock.patch.object(benchmark, "_update_service_evidence",
                               return_value=current), \
                mock.patch.object(benchmark, "_services_live", return_value=True):
            self.assertEqual(benchmark._gate_state(tracker), "runtime_errors")
        tracker._parallel_evidence["ble"] = False
        with mock.patch.object(benchmark, "_update_service_evidence",
                               return_value=dict(tracker._parallel_baseline)):
            self.assertEqual(benchmark._gate_state(tracker), "services_incomplete")

    def test_connections_without_bytes_do_not_prove_transport_load(self):
        self.write_config()
        tracker, _ = benchmark.prepare(Tracker(os.path.join(self.tmp.name, "production")))
        current = dict(tracker._parallel_baseline)
        current.update(access_state="ONLINE", ntrip_state="streaming", ble_clients=1, tcp_clients=1)
        for key in ("ntrip_bytes", "gnss_messages", "http_requests"):
            current[key] += 100
        with mock.patch.object(benchmark, "_service_snapshot", return_value=current):
            benchmark._update_service_evidence(tracker)
            self.assertFalse(benchmark._services_live(tracker))
            self.assertFalse(tracker._parallel_evidence["ble"])
            self.assertFalse(tracker._parallel_evidence["tcp"])
            current["ble_bytes"] += 100
            current["tcp_bytes"] += 100
            benchmark._update_service_evidence(tracker)
            self.assertTrue(benchmark._services_live(tracker))
        later = benchmark.time.ticks_ms() + benchmark.SERVICE_GRACE_MS + 1
        with mock.patch.object(benchmark.time, "ticks_ms", return_value=later):
            self.assertFalse(benchmark._services_live(tracker))

    def test_resumed_run_cannot_qualify_an_entire_capacity(self):
        self.write_config()
        tracker, _ = benchmark.prepare(Tracker(os.path.join(self.tmp.name, "production")))
        tracker._parallel_initial_points = 10
        with mock.patch.object(tracker, "feature_count", return_value=10000):
            benchmark._write_result(tracker, "complete", benchmark.time.ticks_ms())
        with open(self.result) as source:
            self.assertFalse(json.load(source)["qualification_valid"])

    def test_live_status_exposes_write_phases_and_separate_drop_causes(self):
        self.write_config()
        tracker, _ = benchmark.prepare(Tracker(os.path.join(self.tmp.name, "production")))
        with mock.patch.dict(benchmark._instances, {"parallel_tracker": tracker}), \
                mock.patch.dict(benchmark.app.stats, {"tcp_queue_overflows": 3,
                    "tcp_write_timeouts": 1, "ble_notify_retries": 8}):
            live = benchmark.control_status()["live"]
        self.assertIn("phase_max_us", live["append_profile_us"])
        self.assertEqual(live["services"]["tcp_queue_overflows"], 3)
        self.assertEqual(live["services"]["tcp_write_timeouts"], 1)
        self.assertEqual(live["services"]["ble_notify_retries"], 8)
        self.assertEqual(live["error_deltas"]["tcp_drops"], 0)

    def test_runtime_error_stops_early_and_saves_unqualified_result(self):
        self.write_config()
        tracker, config = benchmark.prepare(Tracker(os.path.join(self.tmp.name, "production")))
        stop = asyncio.Event()
        original_sleep = asyncio.sleep

        async def sleep(delay):
            if delay == 2:  # Terminal heartbeat loop stays alive until shutdown.
                stop.set()
            await original_sleep(0)

        with mock.patch.object(benchmark, "shutdown_event", stop), \
                mock.patch.object(benchmark, "_services_live", return_value=True), \
                mock.patch.object(benchmark, "_gate_state", return_value="runtime_errors"), \
                mock.patch.object(benchmark.asyncio, "sleep", sleep), \
                mock.patch.object(tracker, "enqueue_fix") as enqueue:
            asyncio.run(benchmark.producer(tracker, config))
        enqueue.assert_not_called()
        with open(self.result) as source:
            result = json.load(source)
        self.assertEqual(result["state"], "runtime_errors")
        self.assertEqual(result["confirmed_points"], 0)
        self.assertFalse(result["qualification_valid"])
        self.assertEqual(benchmark.app.stats["parallel_benchmark_state"], "runtime_errors")

    def test_manual_ble_reconnect_has_time_without_starting_partial_load(self):
        self.write_config()
        tracker, config = benchmark.prepare(Tracker(os.path.join(self.tmp.name, "production")))
        stop = asyncio.Event()
        now = [0]
        waits = []

        async def wait_for_phone(delay):
            waits.append(now[0])
            self.assertEqual(benchmark.app.stats["parallel_benchmark_state"], "waiting_services")
            self.assertEqual(tracker.feature_count(), 0)
            now[0] += 120000

        async def finish(delay):
            stop.set()

        with mock.patch.object(benchmark, "shutdown_event", stop), \
                mock.patch.object(benchmark.time, "ticks_ms", lambda: now[0]), \
                mock.patch.object(benchmark, "_services_live", return_value=False), \
                mock.patch.object(benchmark.asyncio, "sleep_ms", wait_for_phone), \
                mock.patch.object(benchmark.asyncio, "sleep", finish), \
                mock.patch.object(benchmark, "_write_result") as result, \
                mock.patch.object(tracker, "enqueue_fix") as enqueue:
            asyncio.run(benchmark.producer(tracker, config))
        self.assertIn(240000, waits)  # Still waiting after the previous deadline.
        self.assertEqual(now[0], 600000)
        enqueue.assert_not_called()
        self.assertEqual(result.call_args.args[1], "services_incomplete")


class TestWifiBenchmarkControl(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.patches = [mock.patch.dict(benchmark.app.stats, {}, clear=True),
                        mock.patch.dict(benchmark._instances, {}, clear=True),
                        mock.patch.object(tracking.os, "statvfs", return_value=(
                            4096, 4096, 1000000, 900000, 900000, 0, 0, 0, 0, 255))]
        for patch in self.patches: patch.start()
        self.production = Tracker("production")

    def tearDown(self):
        self.production.point_store.close()
        for patch in reversed(self.patches): patch.stop()
        os.chdir(self.cwd)
        self.tmp.cleanup()

    def test_wifi_start_preserves_existing_project(self):
        project = self.production.create_project("Existing field project")
        before = self.production.feature_count()
        result = benchmark.control("start", self.production, 10000)
        self.assertTrue(result["reboot_required"])
        self.assertEqual(len(result["run_id"]), 16)
        tracker, config = benchmark.prepare(self.production)
        self.assertEqual(config["control"], "wifi")
        self.assertEqual(self.production.projects[0]["id"], project["id"])
        self.assertEqual(self.production.feature_count(), before)
        self.assertEqual(tracker.path, benchmark.PREFIX)
        tracker.point_store.close()

    def test_start_rejects_active_line_invalid_target_and_double_start(self):
        for target in (True, 10000.0, "10000", 35000):
            with self.assertRaises(ValueError):
                benchmark.control("start", self.production, target)
        project = self.production.create_project("Field")
        self.production.select_project(project["id"])
        self.production.start_line("Active", 0.2)
        with self.assertRaises(ValueError):
            benchmark.control("start", self.production, 10000)
        self.production.active_line_id = None
        benchmark.control("start", self.production, 10000)
        with self.assertRaises(ValueError):
            benchmark.control("start", self.production, 10000)

    def test_stop_disables_resume_without_deleting_test_data(self):
        benchmark.control("start", self.production, 10000)
        with open(benchmark.PREFIX + ".points", "w") as target: target.write("keep")
        benchmark.control("stop", self.production)
        self.assertIsNone(benchmark.load_config())
        self.assertTrue(os.path.exists(benchmark.PREFIX + ".points"))

    def test_cleanup_requires_stopped_state_and_exact_prefix(self):
        synthetic = benchmark.PREFIX + ".points"
        unrelated = benchmark.PREFIX + "-unrelated"
        for name in (synthetic, unrelated, "production.user-data"):
            with open(name, "w") as target: target.write("keep")
        with self.assertRaises(ValueError):
            benchmark.control("cleanup", self.production, confirm="wrong")
        with mock.patch.dict(benchmark._instances, {"parallel_tracker": object()}):
            with self.assertRaises(ValueError):
                benchmark.control("cleanup", self.production, confirm=benchmark.PREFIX)
        benchmark.control("cleanup", self.production, confirm=benchmark.PREFIX)
        self.assertFalse(os.path.exists(synthetic))
        self.assertTrue(os.path.exists(unrelated))
        self.assertTrue(os.path.exists("production.user-data"))

    def test_old_synthetic_inventory_requires_explicit_cleanup(self):
        with open(benchmark.PREFIX + ".result.json", "w") as target: target.write("{}")
        with self.assertRaises(ValueError):
            benchmark.control("start", self.production, 10000)


if __name__ == "__main__":
    unittest.main()
