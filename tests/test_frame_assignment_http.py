# SPDX-License-Identifier: AGPL-3.0-only
"""#39: preview, assign and revoke a project's reference frame over POST /api/tracking."""
import asyncio
import json
from unittest import mock

import support
from support import cfg, web
import tracking
from test_access_v11 import FakeHttpWriter, FakeReader
from test_frame_assignment import EXAMPLE, RESERVED, FrameFixture, digests
from test_http_status_matrix import HOST, ORIGIN, _DeviceCase, request
from test_restore_entry import fix


def journal_on_disk(tracker):
    """The journal events as the files hold them now, without flushing first."""
    events = []
    for _number, path in tracker._segment_files():
        with open(path, encoding="utf-8") as source:
            for raw in source.read().splitlines():
                if raw.strip():
                    events.append(json.loads(raw))
    return events


def assigned_events(tracker):
    return [event for event in journal_on_disk(tracker)
            if event.get("event") == "project_frame_assigned"]


class LostAnswerWriter(FakeHttpWriter):
    """A client that went away: drain() fails once the answer began or the client closed."""

    def __init__(self):
        super().__init__()
        self.closed = False

    async def drain(self):
        if self.closed or self.data:
            raise OSError(104)


class FrameAssignmentHttpTests(FrameFixture, _DeviceCase):
    def setUp(self):
        super().setUp()
        self.cookie, self.csrf, _token = self.login()
        for patch in (mock.patch.object(web, "log"),
                      mock.patch.object(web, "tracker", self.t)):
            patch.start(); self.addCleanup(patch.stop)

    def use(self, tracker):
        """Serve tracker from now on (a restarted one)."""
        patch = mock.patch.object(web, "tracker", tracker)
        patch.start(); self.addCleanup(patch.stop)

    def call(self, payload, method="POST", path="/api/tracking"):
        body = b"{}" if payload is None else json.dumps(payload).encode()
        status, _head, answer = request(method, path, cookie=self.cookie, csrf=self.csrf,
                                        body=body)
        return status, json.loads(answer)

    def raw(self, payload):
        """The request bytes of one POST /api/tracking with the session of this test."""
        body = json.dumps(payload).encode()
        lines = ["POST /api/tracking HTTP/1.0", "Host: " + HOST,
                 "Content-Type: application/json", "Origin: " + ORIGIN,
                 "Cookie: " + self.cookie, "X-CSRF-Token: " + self.csrf,
                 "Content-Length: %d" % len(body)]
        return ("\r\n".join(lines) + "\r\n\r\n").encode() + body

    def preview_over_http(self, pid):
        status, body = self.call({"action": "frame_assignment_preview", "project_id": pid})
        self.assertEqual(status, 200, body)
        return body

    def assign_over_http(self, pid, shown, expected_count=None, frame=EXAMPLE):
        return self.call({"action": "assign_reference_frame", "project_id": pid,
                          "reference_frame": frame, "reference_frame_epoch": "",
                          "expected_count": (shown["total"] if expected_count is None
                                             else expected_count),
                          "scope_token": shown["scope_token"]})

    def revoke_over_http(self, pid, token):
        return self.call({"action": "revoke_reference_frame", "project_id": pid,
                          "assignment_token": token})

    def shown_project(self, pid):
        status, body = self.call(None, "GET")
        self.assertEqual(status, 200)
        return [project for project in body["projects"] if project["id"] == pid][0]

    def assert_conflict(self, reply, code):
        status, body = reply
        self.assertEqual((status, body["error"]), (409, code), body)

    def paused_scan(self, gate, paused, generators):
        """Frame scans that stop after their first batch until gate is set.

        One window per batch, so the first batch ends after line l0. Every scan
        generator (the wrapper and the real one) and every window the store
        opens is noted in generators.
        """
        original, store = tracking.Tracker._frame_scope_steps, self.t.point_store
        windows = store.iter_line

        def window(*args, **kwargs):
            opened = windows(*args, **kwargs)
            generators.append(opened)
            return opened

        def steps(tracker, project):
            inner = original(tracker, project)

            def wrapper():
                try:
                    for item in inner:
                        yield item
                        while item is None and not gate.is_set():
                            paused.set()
                            yield None
                finally:
                    inner.close()
            outer = wrapper()
            generators.extend([outer, inner])
            return outer
        return [mock.patch.object(tracking.Tracker, "_frame_scope_steps", steps),
                support.patch_module_global("tracking", "FRAME_SCAN_WINDOWS", 1),
                mock.patch.object(store, "iter_line", side_effect=window)]

    def test_preview_assign_and_revoke_over_http(self):
        pid = self.legacy_project()
        shown = self.preview_over_http(pid)
        self.assertEqual((shown["total"], shown["new_unknown"]), (6, 6))
        self.assertRegex(shown["scope_token"], r"^[0-9a-f]{16}$")
        self.assertIn("tracking_live", shown)
        status, body = self.assign_over_http(pid, shown)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["frame_assignment"]["reference_frame"], EXAMPLE)
        self.assertEqual(body["tracking_live"]["active_project"]["frame_assignment"]["count"], 6)
        self.assertEqual(body["tracking_performance"]["action"], "assign_reference_frame")
        listed = self.shown_project(pid)
        self.assertEqual(listed["frame_assignment"], body["frame_assignment"])
        status, _head, text = request("GET", "/api/tracking/export?format=csv&project_id=" + pid,
                                      cookie=self.cookie)
        self.assertEqual(status, 200)
        self.assertIn(b"assigned", text)
        # The field setup names the frame of new measurements only.
        def saved(changes):
            cfg.CONFIG.update(changes)
            return True
        with mock.patch.object(web, "apply_live", side_effect=saved):
            status, setup = self.call({"reference_frame": "ETRS89"}, "PUT", "/api/field-setup")
        self.assertEqual((status, setup["values"]["reference_frame"]), (200, "ETRS89"))
        after = self.shown_project(pid)
        self.assertEqual((after["frames"], after["frame_assignment"]),
                         (listed["frames"], listed["frame_assignment"]))
        status, body = self.revoke_over_http(pid, listed["frame_assignment"]["token"])
        self.assertEqual(status, 200, body)
        self.assertIsNone(body["frame_assignment"])
        self.assertIsNone(body["tracking_live"]["active_project"]["frame_assignment"])
        self.assertNotIn("frame_assignment", self.shown_project(pid))

    def test_conflicts_answer_409_with_their_code(self):
        pid = self.legacy_project()
        shown = self.preview_over_http(pid)
        self.assert_conflict(self.assign_over_http(pid, shown, expected_count=5),
                             "frame_scope_changed")
        # Undo and a replacement point between preview and assignment: same count.
        self.t.undo_last(); self.add_rtk_vertex(44)
        self.assertEqual(self.preview(pid)["total"], 6)
        self.assert_conflict(self.assign_over_http(pid, shown), "frame_scope_changed")
        self.assertNotIn("frame_assignment", self.t._project(pid))
        # A revocation with the token shown before an undo inside the scope.
        status, body = self.assign_over_http(pid, self.preview_over_http(pid))
        self.assertEqual(status, 200, body)
        token = self.shown_project(pid)["frame_assignment"]["token"]
        self.t.undo_last()
        self.assert_conflict(self.revoke_over_http(pid, token), "frame_assignment_changed")
        self.assertEqual(self.shown_project(pid)["frame_assignment"]["count"], 5)
        # An archive while a second client revokes the assignment stops.
        self.t.set_line_state("finish")
        other = self.t.create_project("Other")["id"]
        token = self.shown_project(pid)["frame_assignment"]["token"]
        archives, original = list(self.t.archives), tracking.Tracker._finish_steps_async

        async def finish(steps):
            def wrapped():
                done = False
                try:
                    for item in steps:
                        yield item
                        if item is None and not done:
                            done = True; self.t.revoke_frame_assignment(pid, token)
                finally:
                    steps.close()
            return await original(wrapped())
        with mock.patch.object(tracking.Tracker, "_finish_steps_async", staticmethod(finish)):
            self.assert_conflict(self.call({"action": "archive_project", "project_id": pid}),
                                 "frame_export_changed")
        self.assertIsNotNone(self.t._project(pid))
        self.assertEqual(self.t.archives, archives)
        self.assert_conflict(self.revoke_over_http(pid, token), "no_frame_assignment")
        # A project without RTK points that lack a frame.
        empty = self.preview_over_http(other)
        self.assertEqual(empty["total"], 0)
        self.assert_conflict(self.assign_over_http(other, empty), "nothing_to_assign")
        # A recording line in the project.
        self.t.start_line("R1"); self.t.add_vertex(fix(70))
        self.assert_conflict(self.assign_over_http(other, empty), "recording_active")

    def test_invalid_requests_answer_400(self):
        pid = self.legacy_project()
        shown = self.preview_over_http(pid)
        self.t._sync_journal(close=False)
        before = digests(self.tmp)
        assign = {"action": "assign_reference_frame", "project_id": pid,
                  "reference_frame": EXAMPLE, "reference_frame_epoch": "",
                  "expected_count": 6, "scope_token": shown["scope_token"]}
        revoke = {"action": "revoke_reference_frame", "project_id": pid}
        cases = [
            (dict(assign, reference_frame="mixed"), RESERVED),
            (dict(assign, expected_count="6"), "expected_count must be an integer."),
            (dict(assign, expected_count=True), "expected_count must be an integer."),
            (dict((key, value) for key, value in assign.items() if key != "scope_token"),
             "scope_token must be a text."),
            (dict(assign, scope_token=7), "scope_token must be a text."),
            (revoke, "assignment_token must be a text."),
            (dict(revoke, assignment_token=7), "assignment_token must be a text."),
            ({"action": "frame_assignment_preview"}, "Project not found."),
            (dict((key, value) for key, value in assign.items() if key != "project_id"),
             "Project not found."),
            ({"action": "revoke_reference_frame", "assignment_token": "x"},
             "Project not found."),
        ]
        for payload, message in cases:
            with self.subTest(payload=payload):
                status, body = self.call(payload)
                self.assertEqual((status, body), (400, {"error": "invalid_tracking_request",
                                                        "message": message}))
        self.assertNotIn("frame_assignment", self.t._project(pid))
        self.assertEqual(digests(self.tmp), before)

    def test_storage_maintenance_refuses_the_actions(self):
        pid = self.legacy_project()
        shown = self.preview(pid)
        with mock.patch.object(self.t, "maintenance_active", return_value=True):
            for payload in ({"action": "frame_assignment_preview", "project_id": pid},
                            {"action": "assign_reference_frame", "project_id": pid,
                             "reference_frame": EXAMPLE, "expected_count": 6,
                             "scope_token": shown["scope_token"]},
                            {"action": "revoke_reference_frame", "project_id": pid,
                             "assignment_token": "x"}):
                with self.subTest(payload["action"]):
                    self.assert_conflict(self.call(payload), "storage_busy")
        # Maintenance waits for a running preview, which holds a reader.
        self.t.set_line_state("finish")

        async def scenario():
            gate, paused, generators, entered = asyncio.Event(), asyncio.Event(), [], []
            writer = FakeHttpWriter()

            async def maintain():
                async with self.t.maintenance("tracking_storage"):
                    entered.append(self.t.storage_readers)
            patches = self.paused_scan(gate, paused, generators)
            for patch in patches:
                patch.start()
            try:
                answer = asyncio.create_task(web.handle_http_client(FakeReader(self.raw(
                    {"action": "frame_assignment_preview", "project_id": pid})), writer))
                await asyncio.wait_for(paused.wait(), 5)
                self.assertEqual(self.t.storage_readers, 1)
                job = asyncio.create_task(maintain())
                for _ in range(20):
                    await asyncio.sleep(0)
                self.assertEqual(entered, [])
                self.assertTrue(self.t.maintenance_active())
                gate.set()
                await asyncio.wait_for(asyncio.gather(answer, job), 5)
            finally:
                for patch in patches:
                    patch.stop()
            return writer, entered
        writer, entered = self.run_async(scenario())
        self.assertEqual(entered, [0])
        self.assertEqual(self.t.storage_readers, 0)
        self.assertFalse(self.t.maintenance_active())
        head, _sep, body = bytes(writer.data).partition(b"\r\n\r\n")
        self.assertIn(b"200 OK", head)
        self.assertEqual(json.loads(body)["total"], 6)

    def test_a_request_cancelled_during_the_check_writes_nothing(self):
        for action in ("frame_assignment_preview", "assign_reference_frame"):
            with self.subTest(action):
                t = self.tracker(action)
                self.use(t)
                self.t = t
                pid = self.legacy_project(t)
                shown = self.preview(pid, t)
                t._sync_journal(close=False)
                before = digests(self.tmp)
                payload = {"action": action, "project_id": pid}
                if action == "assign_reference_frame":
                    payload.update(reference_frame=EXAMPLE, reference_frame_epoch="",
                                   expected_count=6, scope_token=shown["scope_token"])

                async def scenario():
                    gate, paused, generators = asyncio.Event(), asyncio.Event(), []
                    writer = FakeHttpWriter()
                    patches = self.paused_scan(gate, paused, generators)
                    for patch in patches:
                        patch.start()
                    try:
                        task = asyncio.create_task(web.handle_http_client(
                            FakeReader(self.raw(payload)), writer))
                        await asyncio.wait_for(paused.wait(), 5)
                        self.assertEqual(t.storage_readers, 1)
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                    finally:
                        for patch in patches:
                            patch.stop()
                    return writer, generators
                writer, generators = self.run_async(scenario())
                self.assertGreaterEqual(len(generators), 3)
                self.assertTrue(all(generator.gi_frame is None for generator in generators))
                self.assertEqual(t.storage_readers, 0)
                self.assertEqual(bytes(writer.data), b"")
                self.assertNotIn("frame_assignment", t._project(pid))
                self.assertEqual(digests(self.tmp), before)
                restarted = self.reload(t)
                self.assertNotIn("frame_assignment", restarted._project(pid))
                self.assertEqual(assigned_events(restarted), [])
                again = self.preview(pid, restarted)
                self.assertEqual(dict(again, scope_token=None), dict(shown, scope_token=None))

    def test_an_answer_lost_after_the_commit_keeps_the_assignment(self):
        for closed_during_scan in (False, True):
            with self.subTest(closed_during_scan=closed_during_scan):
                t = self.tracker("lost-%s" % closed_during_scan)
                self.use(t)
                self.t = t
                pid = self.legacy_project(t)
                shown = self.preview(pid, t)
                writer = LostAnswerWriter()
                payload = {"action": "assign_reference_frame", "project_id": pid,
                           "reference_frame": EXAMPLE, "reference_frame_epoch": "",
                           "expected_count": 6, "scope_token": shown["scope_token"]}
                patches = (self.scan_with(lambda: setattr(writer, "closed", True))
                           if closed_during_scan else [])
                for patch in patches:
                    patch.start()
                try:
                    self.run_async(web.handle_http_client(FakeReader(self.raw(payload)),
                                                          writer))
                finally:
                    for patch in patches:
                        patch.stop()
                self.assertEqual(writer.closed, closed_during_scan)
                self.assertEqual(t.storage_readers, 0)
                project = t._project(pid)
                summary = t._assignment_summary(project)
                self.assertEqual(summary["count"], 6)
                self.assertEqual(len(assigned_events(t)), 1)
                assignment, frames = project["frame_assignment"], self.frames(t)
                # The scope token covers the assignment: the old one is stale now.
                self.assert_conflict(self.assign_over_http(pid, shown), "frame_scope_changed")
                restarted = self.reload(t)
                self.use(restarted)
                self.assertEqual(restarted._project(pid)["frame_assignment"], assignment)
                self.assertEqual(self.frames(restarted), frames)
                self.assertEqual(restarted._assignment_summary(restarted._project(pid))["token"],
                                 summary["token"])
                self.assert_conflict(self.assign_over_http(pid, shown), "frame_scope_changed")
                again = self.preview_over_http(pid)
                self.assertEqual((again["assignment"]["count"], again["new_unknown"]), (6, 0))
