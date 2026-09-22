# SPDX-License-Identifier: AGPL-3.0-only
"""Host qualification must fail closed beyond the point-generation phase."""
import copy
import hashlib
import json
import os
import tempfile
import unittest
from unittest import mock

import parallel_tracking_test as tool


def records(count=1):
    measurement = {"recorded_at": 1, "fix_status": "RTK_FIXED", "satellites": 24,
                   "hdop": 0.6, "quality": {"accepted": True}, "profile_id": "profile-1"}
    return [
        {"record": "header", "kind": "survey_backup", "schema_version": 4,
         "profiles": [{"id": "profile-1", "limits": {"max_hdop": 1.5}}]},
        {"record": "project", "project": {"id": "project-1", "name": "Test"}},
        {"record": "line", "project_id": "project-1", "line": {
            "id": "line-1", "name": "Test line", "state": "recording",
            "_summary": {"vertex_count": count}, "properties": {}}},
    ] + [{"record": "vertex", "project_id": "project-1", "line_id": "line-1",
          "coordinate": [10.0, 40.0, 100.0],
          "measurement": dict(measurement, recorded_at=index + 1)}
         for index in range(count)] + [
        {"record": "footer", "active_project_id": "project-1",
         "active_line_id": "line-1", "auto_distance_m": 1.0}]


def encoded(rows, **kwargs):
    raw = b"".join((json.dumps(row, **kwargs) + "\n").encode() for row in rows)
    return raw + (json.dumps({"record": "integrity", "algorithm": "sha256",
        "records": len(rows), "content_sha256": hashlib.sha256(raw).hexdigest()}) + "\n").encode()


class FakeCompletedRun:
    def __init__(self):
        self.clock, self.boot_time, self.phase = 1000, 100, 0
        self.status_changes = {}
        self.after_records = records()
        self.result_changes = {}
        self.restarted = False
        self.perform_restart = True
        self.export = mock.Mock(side_effect=self._export)
        self.action = mock.Mock(side_effect=self._action)

    def json(self, path):
        if path == "/api/parallel-benchmark":
            result = {"run_id": "run-1", "state": "complete", "profile": "local",
                      "target_points": 1, "confirmed_points": 1, "qualification_valid": True,
                      "initial_points_this_boot": 0, "services_continuous": True,
                      "error_deltas": dict.fromkeys(tool.RUNTIME_COUNTERS, 0)}
            result.update(self.result_changes)
            return {"state": "idle" if self.restarted else "complete", "result": result}
        if path != "/status":
            raise AssertionError("Unexpected path")
        status = {"system": {"device_id": "device-1", "uptime_sec": self.clock - self.boot_time,
                    "boot_count": 0, "safe_mode": False},
                  "stats": dict(dict.fromkeys(tool.RUNTIME_COUNTERS.values(), 0),
                      access_state="ONLINE", ntrip_state="streaming", gnss_msgs=10, ntrip_bytes=100),
                  "secret": "must-not-be-saved"}
        change = self.status_changes.get(self.phase)
        if change:
            change(status)
        self.phase += 1
        return status

    def _export(self, filename, target):
        self.clock += 20
        raw = encoded(self.after_records if self.restarted else records())
        with open(filename, "wb") as output:
            output.write(raw)
        return tool.verify_export(raw, target)

    def _action(self, action):
        if action != "stop":
            raise AssertionError("Unexpected action")
        if self.perform_restart:
            self.boot_time = self.clock + 5
        self.clock += 10
        self.restarted = True


class TestCompleteExportValidation(unittest.TestCase):
    def test_all_metadata_participates_in_canonical_comparison(self):
        original = tool.verify_export(encoded(records()), 1)
        for alter in (
                lambda rows: rows[0]["profiles"][0]["limits"].update(max_hdop=2),
                lambda rows: rows[1]["project"].update(name="Changed project"),
                lambda rows: rows[2]["line"]["properties"].update(material="PE"),
                lambda rows: rows[-1].update(auto_distance_m=2)):
            rows = records()
            alter(rows)
            changed = tool.verify_export(encoded(rows), 1)
            self.assertEqual(original["points_sha256"], changed["points_sha256"])
            self.assertNotEqual(original["canonical_sha256"], changed["canonical_sha256"])

    def test_object_order_and_whitespace_do_not_change_canonical_content(self):
        first = tool.verify_export(encoded(records()), 1)
        second = tool.verify_export(encoded(records(), sort_keys=True, separators=(",", ":")), 1)
        self.assertEqual(first["canonical_sha256"], second["canonical_sha256"])
        self.assertNotEqual(first["content_sha256"], second["content_sha256"])

    def test_structure_references_counts_and_coordinates_are_checked_after_valid_integrity(self):
        alterations = (
            lambda rows: rows.insert(1, copy.deepcopy(rows[0])),
            lambda rows: rows.insert(2, copy.deepcopy(rows[1])),
            lambda rows: rows[2].update(project_id="missing"),
            lambda rows: rows[3].update(line_id="missing"),
            lambda rows: rows[3].update(project_id="wrong"),
            lambda rows: rows[3].update(coordinate=[181, 40, 100]),
            lambda rows: rows[3]["measurement"].update(profile_id="missing"),
            lambda rows: rows[3]["measurement"].pop("quality"),
            lambda rows: rows[2]["line"]["_summary"].update(vertex_count=2),
            lambda rows: rows[-1].update(active_project_id="missing"),
            lambda rows: rows[-1].update(active_line_id="missing"),
            lambda rows: rows.insert(-1, {"record": "unknown"}),
        )
        for alter in alterations:
            rows = records()
            alter(rows)
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                tool.verify_export(encoded(rows), 1)

    def test_assets_must_reference_their_own_project_line(self):
        rows = records(0)
        rows.insert(-1, {"record": "asset", "project_id": "project-1", "point": {
            "id": "asset-1", "line_id": "line-1", "coordinate": [10, 40, 100],
            "measurement": records()[3]["measurement"]}})
        self.assertEqual(tool.verify_export(encoded(rows), 1)["points"], 1)
        rows[-2]["point"]["line_id"] = "missing"
        with self.assertRaises(ValueError):
            tool.verify_export(encoded(rows), 1)

    def test_unknown_schema_duplicate_json_keys_and_nonfinite_numbers_are_rejected(self):
        rows = records()
        rows[0]["schema_version"] = 99
        with self.assertRaises(ValueError):
            tool.verify_export(encoded(rows), 1)
        rows = records()
        rows[3]["coordinate"][0] = float("nan")
        with self.assertRaises(ValueError):
            tool.verify_export(encoded(rows), 1)
        with self.assertRaises(ValueError):
            tool.verify_export(b'{"record":"header","record":"header"}\n', 0)

    def test_ten_thousand_points_do_not_import_or_inherit_production_five_thousand_cap(self):
        raw = encoded(records(10000))
        original_import = __import__

        def guarded_import(name, *args, **kwargs):
            if name in ("tracking", "pointstore", "cfg"):
                raise AssertionError("Host validation imported firmware")
            return original_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", side_effect=guarded_import):
            self.assertEqual(tool.verify_export(raw, 10000)["points"], 10000)


class TestExportRestartQualification(unittest.TestCase):
    def verify(self, client):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(tool.time, "monotonic", side_effect=lambda: client.clock), \
                mock.patch.object(tool, "wait_for_stop"):
            base = os.path.join(directory, "qualification")
            result = tool.verify_restart_exports(client, 1, base, "run-1", "local")
            with open(base + ".exports.json") as source:
                self.assertEqual(json.load(source), result)
            self.assertNotIn("must-not-be-saved", json.dumps(result))
            return result

    def test_clean_pair_requires_four_snapshots_and_preserves_all_content(self):
        client = FakeCompletedRun()
        result = self.verify(client)
        self.assertTrue(result["exports_qualified"])
        self.assertNotIn("qualified", result)
        self.assertEqual(set(result["runtime_snapshots"]), {
            "before_export", "after_export", "after_restart", "after_restart_export"})
        client.action.assert_called_once_with("stop")
        self.assertEqual(client.export.call_count, 2)

    def test_every_counter_at_every_phase_prevents_qualification(self):
        for phase in range(4):
            for counter in tool.RUNTIME_COUNTERS.values():
                client = FakeCompletedRun()
                client.status_changes[phase] = lambda status, key=counter: status["stats"].update({key: 1})
                with self.subTest(phase=phase, counter=counter):
                    result = self.verify(client)
                    self.assertFalse(result["exports_qualified"])
                    self.assertTrue(result["verification_errors"])

    def test_missing_counters_and_boot_evidence_fail_closed(self):
        for change in (lambda status: status["stats"].pop("queue_overflows"),
                       lambda status: status["stats"].update(crc_errors=True),
                       lambda status: status["system"].pop("uptime_sec"),
                       lambda status: status["system"].update(safe_mode=True),
                       lambda status: status["system"].update(boot_count=1)):
            client = FakeCompletedRun()
            client.status_changes[2] = change
            self.assertFalse(self.verify(client)["exports_qualified"])

    def test_startup_wait_accepts_immediately_ready_planned_boot_without_fixed_delay(self):
        client = FakeCompletedRun()
        with mock.patch.object(tool.time, "sleep") as sleep:
            snapshot = tool.wait_for_runtime_ready(client)
        sleep.assert_not_called()
        self.assertEqual(snapshot["boot_count"], 0)

    def test_startup_waits_for_data_and_does_not_wait_away_crash_or_runtime_errors(self):
        client = FakeCompletedRun()
        client.status_changes[0] = lambda status: status["stats"].update(ntrip_bytes=0)
        with mock.patch.object(tool.time, "sleep") as sleep:
            snapshot = tool.wait_for_runtime_ready(client)
        sleep.assert_called_once_with(2)
        self.assertGreater(snapshot["services"]["ntrip_bytes"], 0)
        for change in (lambda status: status["system"].update(boot_count=1),
                       lambda status: status["stats"].update(queue_overflows=1)):
            client = FakeCompletedRun()
            client.status_changes[0] = change
            with mock.patch.object(tool.time, "sleep") as sleep:
                snapshot = tool.wait_for_runtime_ready(client)
            sleep.assert_not_called()
            self.assertTrue(tool._runtime_snapshot_problems(snapshot, "startup"))

    def test_startup_without_required_data_has_a_bounded_wait(self):
        client = FakeCompletedRun()
        client.status_changes[0] = lambda status: status["stats"].update(gnss_msgs=0)
        with self.assertRaises(TimeoutError):
            tool.wait_for_runtime_ready(client, timeout=0)

    def test_uptime_rounding_and_two_second_watchdog_sampling_are_tolerated(self):
        client = FakeCompletedRun()
        client.status_changes[1] = lambda status: status["system"].update(uptime_sec=918)
        self.assertTrue(self.verify(client)["exports_qualified"])

    def test_errors_and_counter_resets_cannot_be_hidden_by_an_unexpected_export_reboot(self):
        for phase in (1, 3):
            client = FakeCompletedRun()
            client.status_changes[phase] = lambda status: status["system"].update(uptime_sec=1)
            result = self.verify(client)
            self.assertFalse(result["exports_qualified"])
            self.assertTrue(any("unexpected_restart" in error for error in result["verification_errors"]))

    def test_idle_response_alone_does_not_prove_the_planned_restart(self):
        client = FakeCompletedRun()
        client.perform_restart = False
        result = self.verify(client)
        self.assertFalse(result["exports_qualified"])
        self.assertIn("after_restart:planned_restart_not_proven", result["verification_errors"])

    def test_changed_device_or_only_changed_metadata_cannot_qualify(self):
        client = FakeCompletedRun()
        client.status_changes[2] = lambda status: status["system"].update(device_id="device-2")
        self.assertFalse(self.verify(client)["exports_qualified"])
        client = FakeCompletedRun()
        client.after_records[-1]["auto_distance_m"] = 2
        result = self.verify(client)
        self.assertFalse(result["exports_qualified"])
        self.assertEqual(result["export_before_restart"]["points_sha256"],
                         result["export_after_restart"]["points_sha256"])
        self.assertIn("exports:different_content", result["verification_errors"])

    def test_export_failure_is_unqualified_disarmed_and_logged_without_exception_secrets(self):
        client = FakeCompletedRun()
        client.export.side_effect = TimeoutError("secret-url-and-password")
        result = self.verify(client)
        self.assertFalse(result["exports_qualified"])
        client.action.assert_called_once_with("stop")
        self.assertIn("export_before_restart:TimeoutError", result["verification_errors"])
        self.assertNotIn("secret-url", json.dumps(result))

    def test_reboot_request_failure_is_recorded_and_never_retried_blindly(self):
        client = FakeCompletedRun()
        client.action.side_effect = TimeoutError("secret")
        result = self.verify(client)
        self.assertFalse(result["exports_qualified"])
        client.action.assert_called_once_with("stop")

    def test_other_unfinished_or_invalid_run_is_never_rebooted(self):
        for changes in ({"run_id": "different"}, {"state": "running"},
                        {"initial_points_this_boot": 5000}, {"error_deltas": {}}):
            client = FakeCompletedRun()
            client.result_changes = changes
            result = self.verify(client)
            self.assertFalse(result["exports_qualified"])
            client.action.assert_not_called()
            client.export.assert_not_called()

    def test_cli_qualification_depends_on_the_complete_export_checks(self):
        for exports_qualified in (False, True):
            with self.subTest(exports_qualified=exports_qualified), tempfile.TemporaryDirectory() as directory:
                base = os.path.join(directory, "cli.json")
                client = mock.Mock()
                client.action.return_value = {"run_id": "run-1"}
                load = {"qualified": False, "load_qualified": True, "run_id": "run-1"}

                def verify(*args, **kwargs):
                    kwargs["restart"]()
                    return {"exports_qualified": exports_qualified}

                with mock.patch.object(tool, "WifiControl", return_value=client), \
                        mock.patch.object(tool, "monitor", new_callable=mock.AsyncMock, return_value=load), \
                        mock.patch.object(tool, "verify_restart_exports", side_effect=verify), \
                        mock.patch("builtins.print"), \
                        mock.patch("sys.argv", ["parallel_tracking_test.py", "run", "10000",
                            "--profile", "local", "--host", "device.invalid", "--report", base]):
                    if exports_qualified:
                        tool.main()
                    else:
                        with self.assertRaises(SystemExit):
                            tool.main()
                with open(base) as source:
                    self.assertIs(json.load(source)["qualified"], exports_qualified)
                self.assertEqual(client.action.call_args_list, [
                    mock.call("start", 10000, profile="local"), mock.call("stop")])

    def test_cli_does_not_repeat_the_restart_if_export_verification_is_interrupted(self):
        with tempfile.TemporaryDirectory() as directory:
            client = mock.Mock()
            client.action.return_value = {"run_id": "run-1"}

            def interrupted(*args, **kwargs):
                kwargs["restart"]()
                raise KeyboardInterrupt()

            with mock.patch.object(tool, "WifiControl", return_value=client), \
                    mock.patch.object(tool, "monitor", new_callable=mock.AsyncMock,
                        return_value={"qualified": False, "load_qualified": True}), \
                    mock.patch.object(tool, "verify_restart_exports", side_effect=interrupted), \
                    mock.patch("sys.argv", ["parallel_tracking_test.py", "run", "10000",
                        "--profile", "local", "--host", "device.invalid",
                        "--report", os.path.join(directory, "cli.json")]), \
                    self.assertRaises(KeyboardInterrupt):
                tool.main()
            self.assertEqual(client.action.call_args_list, [
                mock.call("start", 10000, profile="local"), mock.call("stop")])


if __name__ == "__main__":
    unittest.main()
