# SPDX-License-Identifier: AGPL-3.0-only
"""#23c Task 1: boot identity in the live snapshot and an apply id per request.

cfg.BOOT_ID is random per boot (RAM only); the configuration result carries the
boot that wrote it and the apply id of the request it belongs to. A final record
inherits the apply id only from the `pending` record it replaces.
"""
import asyncio
import errno
import inspect
import json
import os
import re
import tempfile
import unittest
from unittest import mock

from support import cfg, state, web
import cfg_factory
import cfg_store
from test_access_v11 import FakeHttpWriter


APPLY_ID = "0123456789abcdef"
# Measured at the #23c base (Task 0); the dispatcher stays as it is.
DISPATCHER_LINES = 277


def eio_for(real, path):
    """A stand-in for os.remove/os.rename that fails with EIO for one source path."""
    def call(source, *args):
        if source == path:
            raise OSError(errno.EIO, "EIO")
        return real(source, *args)
    return call


class _IdentityCase(unittest.TestCase):
    """A device with Wi-Fi "Old" in a temporary directory (as TestTransactionalConfiguration)."""

    def setUp(self):
        self.cwd = os.getcwd()
        self.tmp = tempfile.mkdtemp(prefix="esp-rtk-23c-")
        os.chdir(self.tmp)
        self.saved = dict(cfg.CONFIG)
        self.saved_access_state = state.app.stats.get("access_state")
        state.app.stats["access_state"] = "ONLINE"
        cfg._ROLLBACK_OPEN, cfg._ROLLBACK_CODE, cfg._STAGING_UNRECOVERED = False, None, False
        cfg._STAGING_LOG.reset()
        cfg._CONFIG_FILE_PROTECTED = False
        cfg.discard_staged_config()
        cfg.CONFIG["wifi_ssid"] = "Old"
        cfg.CONFIG["wifi_pass"] = "legacy-password"
        cfg.save_config()
        self.boot = None

    def tearDown(self):
        cfg.CONFIG.clear()
        cfg.CONFIG.update(self.saved)
        cfg._ROLLBACK_OPEN, cfg._ROLLBACK_CODE, cfg._STAGING_UNRECOVERED = False, None, False
        cfg.set_task_reloader(None)
        cfg.discard_staged_config()
        state.app.stats["access_state"] = self.saved_access_state
        cfg._CONFIG_FILE_PROTECTED = False
        del cfg.CONFIG_WARNINGS[:]
        os.chdir(self.cwd)

    def write_json(self, path, data):
        with open(path, "w") as fh:
            json.dump(data, fh)

    def result_file(self):
        with open(cfg.CONFIG_RESULT_FILE) as fh:
            return json.load(fh)

    def restart(self, boot_id):
        """Simulate a new boot with boot_id: a patched BOOT_ID, then load_config()."""
        if self.boot is None:
            self.addCleanup(lambda: self.boot.stop())
        else:
            self.boot.stop()
        self.boot = mock.patch.object(cfg, "BOOT_ID", boot_id)
        self.boot.start()
        cfg.load_config()

    def armed(self):
        """Staged wifi_ssid "New" applied with APPLY_ID, then a restart into boot feedbeef."""
        cfg.stage_config({"wifi_ssid": "New"})
        self.assertTrue(cfg.apply_staged_config(apply_id=APPLY_ID))
        self.restart("feedbeef")

    def call(self, handler, path, method="POST", query="", body=b""):
        """(status, body) of one request; the restart is mocked and never scheduled."""
        writer = FakeHttpWriter()
        with mock.patch.object(web, "_admin_allowed", return_value=True), \
                mock.patch.object(web, "do_soft_reboot_deferred", mock.Mock(return_value="reboot")), \
                mock.patch.object(web.asyncio, "create_task") as create_task, \
                mock.patch.object(web.app, "reevaluate_gate"):
            asyncio.run(handler(writer, path, method, query, body, False, {}))
        self.create_task = create_task
        head, _sep, answer = bytes(writer.data).partition(b"\r\n\r\n")
        return int(head.split(b" ", 2)[1]), json.loads(answer)


class TestBootAndApplyIdentity(_IdentityCase):
    def test_boot_id_is_random_hex_per_import(self):
        self.assertIsNotNone(re.fullmatch("[0-9a-f]{8}", cfg.BOOT_ID))
        with mock.patch.object(cfg_factory.os, "urandom", return_value=b"\x00\x01\xab\xff"):
            self.assertEqual(cfg_factory.new_boot_id(), "0001abff")

    def test_live_snapshot_carries_the_boot_id(self):
        self.assertEqual(web.ui_live_snapshot()["system"]["boot_id"], cfg.BOOT_ID)
        with mock.patch.object(web, "BOOT_ID", "feedbeef"):
            self.assertEqual(web.ui_live_snapshot()["system"]["boot_id"], "feedbeef")

    def test_pending_carries_the_apply_id_and_the_final_result_inherits_it(self):
        cfg.stage_config({"wifi_ssid": "New"})
        self.assertTrue(cfg.apply_staged_config(apply_id=APPLY_ID))
        result = cfg.config_result_read()
        self.assertEqual((result["status"], result["apply_id"], result["boot_id"]),
                         ("pending", APPLY_ID, cfg.BOOT_ID))
        self.restart("feedbeef")
        self.assertTrue(cfg.commit_pending_config("New"))
        result = cfg.config_result_read()
        self.assertEqual((result["status"], result["apply_id"], result["boot_id"]),
                         ("connected", APPLY_ID, "feedbeef"))
        self.assertEqual(result["details"], {"ssid": "New"})

    def test_rollback_and_interruption_inherit_the_apply_id(self):
        cfg.stage_config({"wifi_ssid": "New"})
        self.assertTrue(cfg.apply_staged_config(apply_id=APPLY_ID))
        self.assertTrue(cfg.rollback_pending_config("access_point_not_found"))
        result = cfg.config_result_read()
        self.assertEqual((result["status"], result["details"]["code"], result["apply_id"]),
                         ("failed", "access_point_not_found", APPLY_ID))
        self.assertFalse(cfg.pending_config_active())
        # A restart interrupted an apply before its pending file was written.
        self.write_json(cfg.CONFIG_RESULT_FILE, {"schema_version": 2, "status": "pending",
                                                 "details": {}, "uptime_sec": 5,
                                                 "apply_id": "aabbccdd"})
        self.assertTrue(cfg.resolve_interrupted_apply())
        result = cfg.config_result_read()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["details"], {"code": "apply_interrupted", "restored": []})
        self.assertEqual((result["apply_id"], result["boot_id"]), ("aabbccdd", cfg.BOOT_ID))

    def test_results_without_a_pending_record_carry_no_apply_id(self):
        old = cfg._persisted_config()
        new = dict(old, wifi_ssid="New")
        for path, data in (("config.json", new), (cfg.PENDING_CONFIG_FILE, new),
                           (cfg.LAST_GOOD_CONFIG_FILE, old),
                           (cfg.CONFIG_RESULT_FILE, {"schema_version": 2, "status": "connected",
                                                     "details": {"ssid": "Old"}, "uptime_sec": 9,
                                                     "apply_id": "11111111"})):
            self.write_json(path, data)
        cfg.load_config()
        self.assertTrue(cfg.commit_pending_config("New"))
        result = cfg.config_result_read()
        self.assertEqual((result["status"], result["apply_id"]), ("connected", None))
        # A failed arm after "none" writes back a complete `none` without an apply id.
        os.remove(cfg.CONFIG_RESULT_FILE)
        cfg.stage_config({"wifi_ssid": "Other"})
        with mock.patch.object(cfg._store, "arm", return_value=False):
            self.assertFalse(cfg.apply_staged_config(apply_id=APPLY_ID))
        result = self.result_file()
        self.assertEqual((result["status"], result["apply_id"]), ("none", None))
        # An old client (/save, restore without a query) arms without an apply id.
        self.assertTrue(cfg.apply_config_change({"wifi_ssid": "X"}))
        result = cfg.config_result_read()
        self.assertEqual((result["status"], result["apply_id"]), ("pending", None))

    def test_v15_10_records_without_ids_stay_readable(self):
        old = cfg._persisted_config()
        new = dict(old, wifi_ssid="New", ntrip_host="new.example")
        for path, data in (("config.json", new), (cfg.PENDING_CONFIG_FILE, new),
                           (cfg.LAST_GOOD_CONFIG_FILE, old),
                           (cfg.CONFIG_RESULT_FILE, {"schema_version": 2, "status": "pending",
                                                     "details": {}, "uptime_sec": 1800000000})):
            self.write_json(path, data)
        cfg.load_config()
        before = web.config_result_for_clients()
        self.assertEqual((before["status"], before["apply_id"], before["boot_id"]),
                         ("pending", None, None))
        self.assertTrue(cfg.commit_pending_config("New"))
        result = web.config_result_for_clients()
        self.assertEqual((result["status"], result["details"]), ("connected", {"ssid": "New"}))
        self.assertIsNone(result["apply_id"])            # the old pending had none
        self.assertEqual(result["boot_id"], cfg.BOOT_ID)

    def test_apply_validates_and_echoes_the_apply_id(self):
        handler = web._handle_config_request
        cfg.stage_config({"wifi_ssid": "New"})
        for body in (b'{"apply_id": "XYZ12345"}', b'{"apply_id": 5}', b'{"apply_id": "abc"}',
                     b'{"apply_id": "' + b"a" * 33 + b'"}'):
            with self.subTest(body=body):
                status, answer = self.call(handler, "/api/config/apply", body=body)
                self.assertEqual((status, answer["error"]), (400, "invalid_request"))
                self.assertEqual(answer["message"],
                                 "apply_id must be 8 to 32 lowercase hex characters.")
                self.assertFalse(cfg.pending_config_active())
                self.assertEqual(cfg.staged_config(), {"wifi_ssid": "New"})
                self.create_task.assert_not_called()
        for body in (b"{", b"[]", b'"x"'):
            with self.subTest(body=body):
                status, answer = self.call(handler, "/api/config/apply", body=body)
                self.assertEqual((status, answer["error"]), (400, "invalid_json"))
                self.assertFalse(cfg.pending_config_active())
        status, answer = self.call(handler, "/api/config/apply",
                                   body=b'{"apply_id": "0123456789abcdef"}')
        self.assertEqual((status, answer), (202, {"applying": True, "apply_id": APPLY_ID}))
        self.create_task.assert_called_once_with("reboot")
        result = cfg.config_result_read()
        self.assertEqual((result["status"], result["apply_id"]), ("pending", APPLY_ID))
        for index, body in enumerate((b"", b"{}")):
            with self.subTest(body=body):
                self.assertTrue(cfg.commit_pending_config("New"))      # the earlier change ends
                cfg.stage_config({"wifi_ssid": "Next%d" % index})
                status, answer = self.call(handler, "/api/config/apply", body=body)
                self.assertEqual((status, answer), (202, {"applying": True, "apply_id": None}))
                self.assertIsNone(cfg.config_result_read()["apply_id"])

    def test_restore_takes_the_apply_id_from_the_query(self):
        handler = web._handle_config_request
        target = dict(cfg.CONFIG)
        cfg.CONFIG.update(wifi_ssid="Restored", antenna_height_m=1.25)
        backup = json.dumps(web.config_backup()).encode()
        cfg.CONFIG.clear()
        cfg.CONFIG.update(target)
        before = (cfg.CONFIG["wifi_ssid"], cfg.CONFIG["antenna_height_m"])
        self.assertNotEqual(before, ("Restored", 1.25))
        for query in ("apply_id=ZZ", "apply_id=" + "a" * 33):
            with self.subTest(query=query):
                status, answer = self.call(handler, "/api/config/restore", query=query,
                                           body=backup)
                self.assertEqual((status, answer["error"]), (400, "invalid_request"))
                self.assertEqual((cfg.CONFIG["wifi_ssid"], cfg.CONFIG["antenna_height_m"]),
                                 before)
                self.assertFalse(cfg.pending_config_active())
                self.create_task.assert_not_called()
        status, answer = self.call(handler, "/api/config/restore", query="apply_id=00112233",
                                   body=backup)
        self.assertEqual(status, 202)
        self.assertEqual(answer["apply_id"], "00112233")
        self.assertEqual((answer["restored"], answer["kept"]), (True, []))
        self.assertIn("field_setup", answer["sections"])
        self.assertEqual(cfg.CONFIG["antenna_height_m"], 1.25)
        result = cfg.config_result_read()
        self.assertEqual((result["status"], result["apply_id"]), ("pending", "00112233"))
        self.assertTrue(cfg.commit_pending_config("Restored"))
        status, answer = self.call(handler, "/api/config/restore", body=backup)
        self.assertEqual((status, answer["restored"], answer["apply_id"]), (202, True, None))
        self.assertIsNone(cfg.config_result_read()["apply_id"])

    def test_reboot_echoes_the_apply_id(self):
        handler = web._handle_maintenance_request
        status, answer = self.call(handler, "/api/reboot", body=b'{"apply_id": "aabbccdd"}')
        self.assertEqual((status, answer), (202, {"restarting": True, "apply_id": "aabbccdd"}))
        self.create_task.assert_called_once_with("reboot")
        for body, code in ((b'{"apply_id": "AABBCCDD"}', "invalid_request"),
                           (b'{"apply_id": null, "x": 1}', None),
                           (b"{", "invalid_json")):
            with self.subTest(body=body):
                status, answer = self.call(handler, "/api/reboot", body=body)
                if code is None:
                    self.assertEqual((status, answer), (202, {"restarting": True,
                                                              "apply_id": None}))
                    continue
                self.assertEqual((status, answer["error"]), (400, code))
                self.create_task.assert_not_called()
        for body in (b"", b"{}"):
            with self.subTest(body=body):
                status, answer = self.call(handler, "/api/reboot", body=body)
                self.assertEqual((status, answer), (202, {"restarting": True, "apply_id": None}))
                self.create_task.assert_called_once_with("reboot")

    def test_request_apply_id_accepts_only_lowercase_hex(self):
        self.assertEqual(web._request_apply_id(None), (None, True))
        for value in ("a" * 8, "0123456789abcdef", "f" * 32):
            self.assertEqual(web._request_apply_id(value), (value, True))
        for value in ("a" * 7, "a" * 33, "ABCDEF01", "0123456g", "", 12345678, ["aabbccdd"]):
            with self.subTest(value=value):
                self.assertEqual(web._request_apply_id(value), (None, False))

    def test_dispatcher_length_is_unchanged(self):
        lines = len(inspect.getsource(web.handle_http_client).splitlines())
        self.assertEqual(lines, DISPATCHER_LINES)
        self.assertLess(lines, 300)


class TestIdentityFailurePaths(_IdentityCase):
    def test_cleanup_failure_after_connected_never_inherits_from_a_final_record(self):
        self.armed()
        remove = eio_for(cfg_store.os.remove, cfg.PENDING_CONFIG_FILE)
        with mock.patch.object(cfg_store.os, "remove", side_effect=remove):
            self.assertIs(cfg.commit_pending_config("New"), False)
            result = cfg.config_result_read()
            self.assertEqual((result["status"], result["apply_id"], result["boot_id"],
                              result["details"]),
                             ("connected", APPLY_ID, "feedbeef", {"ssid": "New"}))
            self.assertTrue(os.path.exists(cfg.PENDING_CONFIG_FILE))
            # Reconnected in the same boot, the cleanup still fails.
            self.assertIs(cfg.commit_pending_config("New"), False)
            result = cfg.config_result_read()
        self.assertEqual((result["status"], result["apply_id"], result["boot_id"]),
                         ("connected", None, "feedbeef"))

    def test_restart_between_result_and_cleanup_carries_no_identity(self):
        self.armed()
        remove = eio_for(cfg_store.os.remove, cfg.PENDING_CONFIG_FILE)
        with mock.patch.object(cfg_store.os, "remove", side_effect=remove):
            self.assertIs(cfg.commit_pending_config("New"), False)
        self.restart("0badf00d")
        self.assertIs(cfg.resolve_interrupted_apply(), False)
        result = cfg.config_result_read()
        self.assertEqual((result["status"], result["apply_id"], result["boot_id"]),
                         ("connected", APPLY_ID, "feedbeef"))
        self.assertIs(cfg.commit_pending_config("New"), True)
        result = cfg.config_result_read()
        self.assertEqual((result["status"], result["apply_id"], result["boot_id"]),
                         ("connected", None, "0badf00d"))
        self.assertFalse(os.path.exists(cfg.PENDING_CONFIG_FILE))
        self.assertFalse(os.path.exists(cfg.LAST_GOOD_CONFIG_FILE))

    def test_result_recovered_from_tmp_keeps_its_ids(self):
        self.armed()
        record = {"schema_version": 2, "status": "connected", "details": {"ssid": "New"},
                  "uptime_sec": 12, "boot_id": "feedbeef", "apply_id": APPLY_ID}
        os.remove(cfg.CONFIG_RESULT_FILE)
        self.write_json(cfg.CONFIG_RESULT_FILE + ".tmp", record)
        self.assertEqual(cfg.config_result_read(), record)
        self.assertEqual(web.config_result_for_clients(), record)
        self.assertFalse(os.path.exists(cfg.CONFIG_RESULT_FILE + ".tmp"))
        self.assertEqual(self.result_file(), record)

    def test_pending_recovered_from_previous_is_inherited(self):
        self.armed()
        previous = {"schema_version": 2, "status": "pending", "details": {}, "uptime_sec": 3,
                    "boot_id": "c0ffee00", "apply_id": APPLY_ID}
        with open(cfg.CONFIG_RESULT_FILE, "w") as fh:
            fh.write("{ torn")
        with open(cfg.CONFIG_RESULT_FILE + ".tmp", "w") as fh:
            fh.write("{ torn")
        self.write_json(cfg.CONFIG_RESULT_FILE + ".previous", previous)
        self.assertTrue(cfg.pending_config_active())
        self.assertIs(cfg.commit_pending_config("New"), True)
        result = cfg.config_result_read()
        self.assertEqual((result["status"], result["apply_id"], result["boot_id"]),
                         ("connected", APPLY_ID, "feedbeef"))

    def test_result_read_in_place_after_eio_on_rename(self):
        self.armed()
        record = {"schema_version": 2, "status": "connected", "details": {"ssid": "New"},
                  "uptime_sec": 12, "boot_id": "feedbeef", "apply_id": APPLY_ID}
        os.remove(cfg.CONFIG_RESULT_FILE)
        self.write_json(cfg.CONFIG_RESULT_FILE + ".tmp", record)
        rename = eio_for(cfg_store.os.rename, cfg.CONFIG_RESULT_FILE + ".tmp")
        with mock.patch.object(cfg_store.os, "rename", side_effect=rename):
            self.assertEqual(cfg.config_result_read(), record)
            self.assertEqual(web.config_result_for_clients(), record)
        self.assertFalse(os.path.exists(cfg.CONFIG_RESULT_FILE))

    def test_unreadable_result_carries_no_identity(self):
        self.armed()
        for path in (cfg.CONFIG_RESULT_FILE, cfg.CONFIG_RESULT_FILE + ".tmp"):
            with open(path, "wb") as fh:
                fh.write(b"{")
        self.assertEqual(web.config_result_for_clients(),
                         {"schema_version": 2, "status": "pending",
                          "details": {"code": "result_unreadable"},
                          "boot_id": None, "apply_id": None})
        self.assertIs(cfg.commit_pending_config("New"), False)
        with open(cfg.CONFIG_RESULT_FILE, "rb") as fh:
            self.assertEqual(fh.read(), b"{")            # nothing new was written
        self.assertTrue(cfg.pending_config_active())

    def test_failed_record_keeps_its_apply_id_while_the_rollback_is_retried(self):
        self.armed()
        with mock.patch.object(cfg._store, "complete_rollback", return_value=False):
            self.assertIs(cfg.rollback_pending_config("authentication_failed"), False)
        result = cfg.config_result_read()
        self.assertEqual((result["status"], result["apply_id"], result["boot_id"]),
                         ("failed", APPLY_ID, "feedbeef"))
        self.assertEqual(result["details"], {"code": "authentication_failed",
                                             "restored": ["wifi_ssid"]})
        self.assertEqual(cfg.CONFIG["wifi_ssid"], "New")     # marked to be set back, not yet
        self.restart("0badf00d")
        self.assertIs(cfg.resolve_interrupted_apply(), True)
        self.assertFalse(cfg.pending_config_active())
        self.assertEqual(cfg.CONFIG["wifi_ssid"], "Old")
        result = cfg.config_result_read()
        self.assertEqual((result["status"], result["apply_id"], result["boot_id"]),
                         ("failed", APPLY_ID, "feedbeef"))


if __name__ == "__main__":
    unittest.main()


class TestStagingRevision(_IdentityCase):
    """#23c Task 2: the staging revision and the 409 precondition on save and apply."""

    def config_file(self):
        with open(cfg.CONFIG["config_file"]) as fh:
            return fh.read()

    def two_tabs(self):
        """Tab A saw wifi_ssid "Barn" (r1); tab B then staged "Other" (r2)."""
        cfg.stage_config({"wifi_ssid": "Barn"})
        r1 = cfg.staging_revision()
        cfg.stage_config({"wifi_ssid": "Other"})
        return r1, cfg.staging_revision()

    def test_revision_moves_with_every_staging_change(self):
        r0 = cfg.staging_revision()
        self.assertTrue(r0.startswith(cfg.BOOT_ID + "-"))
        cfg.stage_config({"ntrip_mount": "X2"})            # task_restart only: nothing staged
        self.assertEqual(cfg.staging_revision(), r0)
        cfg.stage_config({"wifi_ssid": "Barn"})
        r1 = cfg.staging_revision()
        self.assertNotEqual(r1, r0)
        cfg.stage_config({"wifi_ssid": "Barn"})            # the same again
        self.assertEqual(cfg.staging_revision(), r1)
        cfg.stage_config({"wifi_ssid": "Other"})           # same key, other value
        r2 = cfg.staging_revision()
        self.assertNotEqual(r2, r1)
        cfg.stage_config({"wifi_ssid": cfg.CONFIG["wifi_ssid"]})   # the key leaves the staging
        r3 = cfg.staging_revision()
        self.assertNotEqual(r3, r2)
        cfg.discard_staged_config()                        # empty staging: no change
        self.assertEqual(cfg.staging_revision(), r3)
        cfg.stage_config({"wifi_ssid": "Barn"})
        r4 = cfg.staging_revision()
        self.assertNotEqual(r4, r3)
        cfg.discard_staged_config()
        self.assertNotEqual(cfg.staging_revision(), r4)
        cfg.stage_config({"wifi_ssid": "Barn"})
        before_apply = cfg.staging_revision()
        self.assertTrue(cfg.apply_staged_config())
        self.assertNotEqual(cfg.staging_revision(), before_apply)
        counter = cfg.staging_revision().rsplit("-", 1)[1]
        with mock.patch.object(cfg, "BOOT_ID", "feedbeef"):
            other_boot = cfg.staging_revision()
        self.assertEqual(other_boot, "feedbeef-" + counter)
        self.assertNotEqual(other_boot, cfg.staging_revision())

    def test_stage_config_answers_with_the_revision(self):
        summary = cfg.stage_config({"wifi_ssid": "Barn"})
        self.assertEqual(summary["staging_revision"], cfg.staging_revision())
        again = cfg.stage_config({"wifi_ssid": "Barn"})
        self.assertEqual(again["staging_revision"], summary["staging_revision"])

    def test_put_with_a_stale_revision_changes_nothing(self):
        handler = web._handle_config_request
        r1, r2 = self.two_tabs()
        saved_file = self.config_file()
        mount = cfg.CONFIG["ntrip_mount"]
        status, answer = self.call(handler, "/api/config", "PUT",
                                   "staging_revision=" + r1, b'{"ntrip_mount": "M2"}')
        self.assertEqual((status, answer["error"]), (409, "staging_changed"))
        self.assertEqual(answer["message"],
                         "The changes waiting in the device memory changed. Check them again.")
        self.assertEqual(cfg.CONFIG["ntrip_mount"], mount)
        self.assertEqual(cfg.staged_config(), {"wifi_ssid": "Other"})
        self.assertEqual(self.config_file(), saved_file)
        self.assertEqual(cfg.staging_revision(), r2)
        for query in ("staging_revision=", "staging_revision=null"):     # present but not the current one
            with self.subTest(query=query):
                status, answer = self.call(handler, "/api/config", "PUT", query,
                                           b'{"ntrip_mount": "M2"}')
                self.assertEqual((status, answer["error"]), (409, "staging_changed"))
        self.assertEqual(self.config_file(), saved_file)
        status, answer = self.call(handler, "/api/config", "PUT", "staging_revision=" + r2,
                                   b'{"ntrip_mount": "M2"}')
        self.assertEqual(status, 202)
        self.assertEqual(answer["staging_revision"], r2)
        self.assertEqual(answer["staged"], ["wifi_ssid"])
        self.assertEqual(cfg.CONFIG["ntrip_mount"], "M2")
        # An old client sends no revision and is not checked.
        status, answer = self.call(handler, "/api/config", "PUT", "", b'{"ntrip_mount": "M3"}')
        self.assertEqual(status, 202)
        self.assertEqual(cfg.CONFIG["ntrip_mount"], "M3")

    def test_put_checks_the_revision_after_the_json_and_before_the_validation(self):
        handler = web._handle_config_request
        r1, _r2 = self.two_tabs()
        status, answer = self.call(handler, "/api/config", "PUT", "staging_revision=" + r1, b"{")
        self.assertEqual((status, answer["error"]), (400, "invalid_json"))
        status, answer = self.call(handler, "/api/config", "PUT", "staging_revision=" + r1,
                                   b'{"ntrip_port": 0}')
        self.assertEqual((status, answer["error"]), (409, "staging_changed"))
        status, answer = self.call(handler, "/api/config", "PUT", "staging_revision=" + _r2,
                                   b'{"ntrip_port": 0}')
        self.assertEqual((status, answer["error"]), (400, "invalid_configuration"))

    def test_apply_with_a_stale_revision_is_refused(self):
        handler = web._handle_config_request
        r1, r2 = self.two_tabs()
        for body in (b'{"staging_revision": "' + r1.encode() + b'"}', b'{"staging_revision": null}',
                     b'{"staging_revision": 5}', b'{"staging_revision": ""}'):
            with self.subTest(body=body):
                status, answer = self.call(handler, "/api/config/apply", body=body)
                self.assertEqual((status, answer["error"]), (409, "staging_changed"))
                self.assertFalse(cfg.pending_config_active())
                self.assertEqual(cfg.staged_config(), {"wifi_ssid": "Other"})
                self.assertEqual(cfg.staging_revision(), r2)
                self.create_task.assert_not_called()
        status, answer = self.call(handler, "/api/config/apply",
                                   body=b'{"staging_revision": "' + r2.encode() + b'"}')
        self.assertEqual((status, answer), (202, {"applying": True, "apply_id": None}))
        self.assertTrue(cfg.pending_config_active())
        # An old client sends no revision and is not checked.
        self.assertTrue(cfg.commit_pending_config("Other"))
        cfg.stage_config({"wifi_ssid": "Third"})
        status, _answer = self.call(handler, "/api/config/apply", body=b"{}")
        self.assertEqual(status, 202)

    def test_apply_checks_refusal_before_revision(self):
        r1, _r2 = self.two_tabs()
        state.app.stats["access_state"] = "CONNECTING"
        status, answer = self.call(web._handle_config_request, "/api/config/apply",
                                   body=b'{"staging_revision": "' + r1.encode() + b'"}')
        self.assertEqual((status, answer["error"]), (409, "config_verification_running"))

    def test_apply_checks_body_and_apply_id_before_revision_and_revision_before_nothing(self):
        handler = web._handle_config_request
        # Empty staging: a wrong revision is still reported first (E23 order).
        status, answer = self.call(handler, "/api/config/apply",
                                   body=b'{"staging_revision": "nope"}')
        self.assertEqual((status, answer["error"]), (409, "staging_changed"))
        status, answer = self.call(handler, "/api/config/apply",
                                   body=b'{"staging_revision": "' + cfg.staging_revision().encode() + b'"}')
        self.assertEqual((status, answer["error"]), (409, "nothing_to_apply"))
        status, answer = self.call(handler, "/api/config/apply",
                                   body=b'{"staging_revision": "nope", "apply_id": "XYZ"}')
        self.assertEqual((status, answer["error"]), (400, "invalid_request"))

    def test_get_config_reports_the_staging_without_values(self):
        _r1, r2 = self.two_tabs()
        writer = FakeHttpWriter()
        with mock.patch.object(web, "_admin_allowed", return_value=True):
            asyncio.run(web._handle_config_request(writer, "/api/config", "GET", "", b"",
                                                   False, {}))
        head, _sep, raw = bytes(writer.data).partition(b"\r\n\r\n")
        self.assertEqual(
            json.loads(raw)["staging"],
            {"revision": r2, "keys": ["wifi_ssid"], "restart_required": True})
        self.assertNotIn(b"Other", raw)
        cfg.discard_staged_config()
        writer = FakeHttpWriter()
        with mock.patch.object(web, "_admin_allowed", return_value=True):
            asyncio.run(web._handle_config_request(writer, "/api/config", "GET", "", b"",
                                                   False, {}))
        _head, _sep, raw = bytes(writer.data).partition(b"\r\n\r\n")
        staging = json.loads(raw)["staging"]
        self.assertEqual((staging["keys"], staging["restart_required"]), ([], False))
        self.assertEqual(staging["revision"], cfg.staging_revision())

    def test_restore_and_save_drop_the_staging_and_move_the_revision(self):
        _r1, r2 = self.two_tabs()
        self.assertEqual(cfg.staged_config(), {"wifi_ssid": "Other"})
        self.assertTrue(cfg.apply_config_change({"ntrip_host": "h.example"}, apply_id=None))
        self.assertEqual(cfg.staged_config(), {})
        self.assertNotEqual(cfg.staging_revision(), r2)

    def test_a_failed_change_that_drops_the_staging_moves_the_revision(self):
        _r1, r2 = self.two_tabs()
        with mock.patch.object(cfg, "stage_config", return_value=None):
            self.assertFalse(cfg.apply_config_change({"ntrip_host": "h.example"}))
        self.assertEqual(cfg.staged_config(), {})
        self.assertNotEqual(cfg.staging_revision(), r2)

    def test_an_empty_change_leaves_the_revision(self):
        cfg.stage_config({"wifi_ssid": "Barn"})
        revision = cfg.staging_revision()
        self.assertTrue(cfg.stage_config({"wifi_ssid": "Barn"}))
        self.assertEqual(cfg.staging_revision(), revision)

    def test_dispatcher_length_is_unchanged_by_the_revision(self):
        self.assertEqual(len(inspect.getsource(web.handle_http_client).splitlines()),
                         DISPATCHER_LINES)
