# SPDX-License-Identifier: AGPL-3.0-only
"""Maintenance must yield, preserve old data on failure, and exclude writers."""
import asyncio
import errno
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import support
from support import web
import tracking
from tracking import Tracker
from pointstore import FlashPointStore
from test_tracking import FIX
from test_http import FakeWriter


class StorageMaintenanceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.tracker = Tracker(os.path.join(self.folder.name, 'survey'))
        self.addCleanup(self.tracker.point_store.close)
        self.addCleanup(self.tracker._sync_journal, True)

    def project(self, name, count=80):
        project = self.tracker.create_project(name)
        self.tracker.start_line('Route', .2)
        for index in range(count):
            self.tracker.add_vertex(dict(FIX, lon=8 + index * .00002,
                                         lon_e8=800000000 + index * 2000))
        self.tracker.set_line_state('finish')
        return project

    async def test_archive_and_reactivate_yield_and_preserve_remaining_project(self):
        first = self.project('Remaining', 96)
        second = self.project('Archive', 65)
        before = self.tracker.backup()['projects']
        turns, reads, batches = [], [0], []
        original = FlashPointStore._decode
        def decode(store, *args):
            reads[0] += 1
            return original(store, *args)
        async def observer():
            while True:
                turns.append(True); batches.append(reads[0]); reads[0] = 0
                await asyncio.sleep(0)
        task = asyncio.create_task(observer())
        try:
            with mock.patch.object(FlashPointStore, '_decode', decode), \
                    mock.patch.object(FlashPointStore, '_scan_generation', side_effect=AssertionError('blocking scan')), \
                    mock.patch.object(self.tracker, 'compact', side_effect=AssertionError('blocking compaction')):
                archive = await self.tracker.archive_project_async(second['id'])
                self.assertGreater(len(turns), 20)
                self.assertEqual(self.tracker.feature_count(), 96)
                self.assertEqual([p['id'] for p in self.tracker.projects], [first['id']])
                self.assertTrue((await self.tracker.verify_archive_async(archive['id']))['valid'])
                reactivated = await self.tracker.reactivate_archive_async(archive['id'])
                self.assertEqual(reactivated, {'id': second['id'], 'legacy_values_skipped': 0})
                self.assertEqual(self.tracker.feature_count(), 161)
        finally:
            task.cancel(); await asyncio.gather(task, return_exceptions=True)
        self.assertLessEqual(max(batches), 8)
        after = self.tracker.backup()['projects']
        for expected, actual in zip(before, after):
            self.assertEqual(expected['id'], actual['id'])
            self.assertEqual(expected['lines'][0]['coordinates'], actual['lines'][0]['coordinates'])
            for old, new in zip(expected['lines'][0]['measurements'], actual['lines'][0]['measurements']):
                old.pop('record_order', None); new.pop('record_order', None)
                self.assertEqual(old, new)
        reopened = Tracker(self.tracker.path)
        self.assertEqual(reopened.feature_count(), 161)
        reopened.point_store.close(); reopened._sync_journal(True)

    async def test_archive_other_project_keeps_full_line_paused(self):
        old = self.project('Previous trip', 2)
        current = self.tracker.create_project('Current trip')
        line = self.tracker.start_line('Keep paused', 1, fix=FIX)
        self.tracker.max_features = 3
        with self.assertRaises(ValueError):
            self.tracker.add_vertex(dict(FIX, lon=8.00004, lon_e8=800004000), source='automatic')
        before = self.tracker.backup()['projects'][1]
        archived = await self.tracker.archive_project_async(old['id'])
        self.assertTrue((await self.tracker.verify_archive_async(archived['id']))['valid'])
        self.assertEqual(self.tracker.capacity()['remaining'], 2)
        self.assertEqual(self.tracker.active_project_id, current['id'])
        self.assertEqual(self.tracker.active_line_id, line['id'])
        self.assertEqual(self.tracker.status()['active_line']['state'], 'paused')
        after = self.tracker.backup()['projects'][0]
        self.assertEqual(before, after)
        self.tracker.set_line_state('resume')
        self.assertTrue(self.tracker.on_fix(dict(FIX, lon=8.00006, lon_e8=800006000)))

    async def test_point_rewrite_mixed_segments_and_following_append(self):
        store = FlashPointStore(os.path.join(self.folder.name, 'mixed'))
        for order in range(1, 65):
            store.append(order, 'a' if order % 2 else 'b', [8, 50, order], {'record_order': order})
        # Exercise the legacy mixed-segment fallback independently of the normal
        # writer's per-line rotation policy.
        with mock.patch.object(store, 'segment_lines', {n: {'a', 'b'} for n in store.segments}):
            await store.rewrite_async(['a'])
        self.assertEqual(len(store.offsets['a']), 32)
        self.assertNotIn('b', store.offsets)
        store.append(100, 'a', [8, 50, 100], {'record_order': 100})
        store.close()
        reopened = FlashPointStore(store.path)
        self.assertEqual(len(reopened.offsets['a']), 33)
        self.assertEqual(reopened.read(reopened.offsets['a'][-1])['coordinate'], [8, 50, 100])
        reopened.close()

    async def test_cancelled_compaction_keeps_manifest_and_unlocks(self):
        first = self.project('Keep')
        second = self.project('Drop')
        self.tracker.delete_project(second['id'])
        self.tracker.point_store.close()
        with open(self.tracker.point_store.manifest_path, 'rb') as source:
            manifest = source.read()
        task = asyncio.create_task(self.tracker.compact_async())
        await asyncio.sleep(.01); task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertFalse(self.tracker.maintenance_active())
        with open(self.tracker.point_store.manifest_path, 'rb') as source:
            self.assertEqual(source.read(), manifest)
        reopened = Tracker(self.tracker.path)
        self.assertEqual([p['id'] for p in reopened.projects], [first['id']])
        self.assertEqual(reopened.feature_count(), 80)
        reopened.point_store.close(); reopened._sync_journal(True)

    async def test_bad_import_keeps_original_and_removes_staging(self):
        self.project('Original', 12)
        before = self.tracker.backup()
        rows = list(self.tracker.iter_compact_archive(self.tracker.active_project_id))
        rows[-1] = rows[-1].replace('sha256', 'invalid')
        with self.assertRaises(ValueError):
            await self.tracker.restore_ndjson_async(iter(rows))
        self.assertEqual(self.tracker.backup(), before)
        self.assertFalse(os.path.exists(self.tracker.path + '.restore.points-v1.jsonl.segments.json'))

    async def test_staged_import_batches_flushes_but_live_points_remain_durable(self):
        self.project('Source', 64)
        rows = list(self.tracker.iter_compact_archive(self.tracker.active_project_id))
        sync = FlashPointStore.sync
        pending, indexes = [], []
        write_index = Tracker._write_index
        def index(tracker):
            if tracker.path.endswith('.restore'): indexes.append(tracker.feature_count())
            return write_index(tracker)
        def observe(store):
            if '.restore.' in store.path: pending.append(store._pending_writes)
            return sync(store)
        with mock.patch.object(FlashPointStore, 'sync', observe), \
                mock.patch.object(Tracker, '_write_index', index):
            await self.tracker.restore_ndjson_async(iter(rows))
        self.assertTrue(any(value >= 16 for value in pending), pending)
        self.assertIn(64, indexes)
        self.assertTrue(all(value in (0, 64) for value in indexes), indexes)
        self.tracker.start_line('Live', .2)
        self.tracker.add_vertex(FIX)
        self.assertEqual(self.tracker.point_store._pending_writes, 0)

    async def test_storage_guard_rejects_writes_and_waits_for_readers(self):
        self.project('Guard', 4)
        release, holding = asyncio.Event(), asyncio.Event()
        async def hold_reader():
            async with self.tracker.reader():
                holding.set()
                await release.wait()
        reader = asyncio.create_task(hold_reader())
        await holding.wait()
        writer = FakeWriter()
        with mock.patch.object(web, 'tracker', self.tracker), \
                mock.patch.object(web, '_admin_allowed', return_value=True):
            action = asyncio.create_task(web._handle_tracking_request(
                writer, '/api/tracking', 'POST', '', b'{"action":"compact"}', False, {}))
            await asyncio.sleep(.005)
            self.assertTrue(self.tracker.maintenance_active())
            self.assertFalse(action.done())
            conflict = FakeWriter()
            await web._handle_tracking_request(conflict, '/api/tracking', 'POST', '',
                b'{"action":"delete_project"}', False, {})
            self.assertIn(b'409 Conflict', conflict.buf)
            release.set()
            await reader
            await action
        self.assertIn(b'200 OK', writer.buf)
        self.assertFalse(self.tracker.maintenance_active())

    def tracking_request(self, path, method, body=b'', query=''):
        writer = FakeWriter()
        return writer, web._handle_tracking_request(writer, path, method, query, body,
                                                    False, {})

    def error_code(self, writer):
        return json.loads(bytes(writer.buf).split(b'\r\n\r\n', 1)[1])['error']

    async def test_post_export_counts_as_reader(self):
        self.project('Export', 2)
        calls, release = [], asyncio.Event()
        async def dispatch(writer, path, method, *rest, **kwargs):
            calls.append(path)
            if path == '/api/tracking/export':
                await release.wait()
        with mock.patch.object(web, 'tracker', self.tracker), \
                mock.patch.object(web, '_admin_allowed', return_value=True), \
                mock.patch.object(web, '_dispatch_tracking_request', dispatch):
            export = asyncio.create_task(
                self.tracking_request('/api/tracking/export', 'POST')[1])
            await asyncio.sleep(.01)
            compact = asyncio.create_task(
                self.tracking_request('/api/tracking', 'POST', b'{"action":"compact"}')[1])
            await asyncio.sleep(.05)
            self.assertEqual(calls, ['/api/tracking/export'])
            self.assertTrue(self.tracker.maintenance_active())
            second, request = self.tracking_request('/api/tracking/export', 'POST')
            await request
            self.assertIn(b'503 Service Unavailable', second.buf)
            self.assertEqual(self.error_code(second), 'storage_busy')
            release.set()
            await asyncio.gather(export, compact)
        self.assertEqual(calls, ['/api/tracking/export', '/api/tracking'])
        self.assertFalse(self.tracker.maintenance_active())
        self.assertIsNone(web.app.maintenance_reason)

    async def test_measurement_blocks_maintenance_but_not_export(self):
        self.project('Measuring', 2)
        class Reader:
            async def read(self, amount): raise AssertionError('Read during a measurement')
        with mock.patch.object(web, 'tracker', self.tracker), \
                mock.patch.object(web, '_admin_allowed', return_value=True), \
                mock.patch.object(web.measurement_session, 'active', return_value=True):
            compact, request = self.tracking_request('/api/tracking', 'POST',
                                                     b'{"action":"compact"}')
            await request
            export, request = self.tracking_request('/api/tracking/export', 'GET',
                                                    query='format=backup')
            await request
            upload = FakeWriter()
            await web._handle_tracking_import(Reader(), upload, b'', 100, False,
                                               {'content-type': 'application/x-ndjson'})
        self.assertIn(b'409 Conflict', compact.buf)
        self.assertEqual(self.error_code(compact), 'recording_active')
        self.assertIn(b'200 OK', export.buf)
        self.assertIn(b'409 Conflict', upload.buf)
        self.assertEqual(self.error_code(upload), 'storage_busy')
        self.assertFalse(self.tracker.maintenance_active())

    async def test_maintenance_action_while_not_loaded_keeps_its_answer(self):
        self.tracker._loaded = False
        cases = (({}, '503 Service Unavailable', 'survey_loading'),
                 ({'install_pending': True}, '409 Conflict', 'restore_install_pending'),
                 ({'install_pending': True, 'benchmark': True}, '409 Conflict',
                  'benchmark_active'))
        for setup, status, code in cases:
            with self.subTest(setup=setup):
                if setup.get('install_pending'):
                    self.tracker._install_pending = True
                instances = {'parallel_tracker': object()} if setup.get('benchmark') else {}
                with mock.patch.object(web, 'tracker', self.tracker), \
                        mock.patch.object(web, '_admin_allowed', return_value=True), \
                        mock.patch.dict(web._instances, instances):
                    writer, request = self.tracking_request('/api/tracking', 'POST',
                                                            b'{"action":"compact"}')
                    await request
                self.assertIn(status.encode(), writer.buf)
                self.assertEqual(self.error_code(writer), code)
                self.assertFalse(self.tracker.maintenance_active())
                self.assertIsNone(web.app.maintenance_reason)

    async def test_nested_restore_returns_to_the_outer_reason(self):
        reasons = []
        async def restore(backup):
            reasons.append(web.app.maintenance_reason)
            return {'legacy_values_skipped': 0}
        with mock.patch.object(web, 'tracker', self.tracker), \
                mock.patch.object(web, '_admin_allowed', return_value=True), \
                mock.patch.object(self.tracker, 'restore_async',
                                  mock.AsyncMock(side_effect=restore)), \
                mock.patch.object(self.tracker, 'status',
                                  lambda: reasons.append(web.app.maintenance_reason) or {}), \
                support.patch_module_global('tracking', 'log'), mock.patch.object(web, 'log'):
            writer, request = self.tracking_request(
                '/api/tracking', 'POST', b'{"action":"restore","backup":{}}')
            await request
        self.assertIn(b'200 OK', writer.buf)
        self.assertEqual(reasons[:2], ['tracking_restore', 'tracking_storage'])
        self.assertIsNone(web.app.maintenance_reason)
        self.assertFalse(self.tracker.maintenance_active())

    async def test_dispatch_receives_the_outer_maintenance_handle(self):
        self.project('Handle', 2)
        seen, original = [], web._dispatch_tracking_request
        async def observe(writer, path, method, *rest, **kwargs):
            handle = kwargs.get('maintenance')
            seen.append((method, path, handle, getattr(handle, 'entered', None)))
            return await original(writer, path, method, *rest, **kwargs)
        with mock.patch.object(web, 'tracker', self.tracker), \
                mock.patch.object(web, '_admin_allowed', return_value=True), \
                mock.patch.object(web, '_dispatch_tracking_request', observe), \
                mock.patch.object(self.tracker, 'restore_async',
                                  mock.AsyncMock(return_value={'legacy_values_skipped': 0})), \
                support.patch_module_global('tracking', 'log'), mock.patch.object(web, 'log'):
            restore, request = self.tracking_request(
                '/api/tracking', 'POST', b'{"action":"restore","backup":{}}')
            await request
            export, request = self.tracking_request('/api/tracking/export', 'GET',
                                                    query='format=backup')
            await request
        self.assertIn(b'200 OK', restore.buf)
        self.assertIn(b'200 OK', export.buf)
        self.assertEqual([entry[:2] for entry in seen],
                         [('POST', '/api/tracking'), ('GET', '/api/tracking/export')])
        self.assertIsNotNone(seen[0][2])
        self.assertIs(seen[0][3], True)
        self.assertIsNone(seen[1][2])

    async def test_import_reason_coexists_with_another_maintenance(self):
        self.project('Coexist', 2)
        noted = []
        class Reader:
            async def read(self, amount):
                noted.append(web.app.maintenance_reason)
                return b''
        writer = FakeWriter()
        token = web.app.begin_maintenance('other')
        try:
            with mock.patch.object(web, 'tracker', self.tracker), \
                    mock.patch.object(web, '_admin_allowed', return_value=True):
                await web._handle_tracking_import(Reader(), writer, b'', 100, False,
                                                   {'content-type': 'application/x-ndjson'})
            self.assertIn(b'400 Bad Request', writer.buf)
            self.assertEqual(noted, ['tracking_restore'])
            self.assertEqual(web.app.maintenance_reason, 'other')
            self.assertFalse(self.tracker.maintenance_active())
        finally:
            web.app.end_maintenance(token)
        self.assertIsNone(web.app.maintenance_reason)

    async def test_import_receives_more_than_old_body_limit_without_whole_body_buffer(self):
        self.project('Restore', 96)
        rows = list(self.tracker.iter_compact_archive(self.tracker.active_project_id))
        # Legal leading whitespace remains part of the source integrity digest;
        # increase via actual vertices instead of modifying the protected bytes.
        for index in range(96, 600):
            line = self.tracker.projects[0]['lines'][0]
            self.tracker.active_line_id = line['id']; line['state'] = 'recording'
            self.tracker.add_vertex(dict(FIX, lon=8 + index * .00002,
                                         lon_e8=800000000 + index * 2000))
        self.tracker.set_line_state('finish')
        raw = ''.join(self.tracker.iter_backup_ndjson()).encode()
        self.assertGreater(len(raw), 262144)
        class Reader:
            at = 0
            async def read(inner, amount):
                self.assertLessEqual(amount, 4096)
                value = raw[inner.at:inner.at + amount]; inner.at += len(value)
                return value
        writer = FakeWriter()
        with mock.patch.object(web, 'tracker', self.tracker), \
                mock.patch.object(web, '_admin_allowed', return_value=True):
            await web._handle_tracking_import(Reader(), writer, b'', len(raw), False,
                                               {'content-type': 'application/x-ndjson'})
        self.assertIn(b'200 OK', writer.buf)
        self.assertIn(b'"schema_version": 5', raw.split(b'\n', 1)[0])
        response = json.loads(bytes(writer.buf).split(b'\r\n\r\n', 1)[1])
        self.assertEqual((response['schema_version'], response['features'],
                          response['legacy_values_skipped']), (5, 600, 0))
        self.assertEqual(self.tracker.feature_count(), 600)
        self.assertFalse(os.path.exists(self.tracker.path + '.upload.ndjson'))
        self.assertFalse(self.tracker.maintenance_active())
        self.assertIsNone(web.app.maintenance_reason)

    async def test_bounded_import_reader_accepts_schema4_and_schema5_headers(self):
        path = os.path.join(self.folder.name, 'header.ndjson')
        for schema, accepted in ((3, False), (4, True), (5, True), (6, False)):
            with open(path, 'w') as target:
                target.write(json.dumps({'kind': 'survey_backup', 'record': 'header',
                                         'schema_version': schema}) + '\n')
            with self.subTest(schema=schema):
                if accepted:
                    self.assertEqual(len(list(web._bounded_backup_lines(path))), 1)
                else:
                    with self.assertRaisesRegex(ValueError, 'integrity-protected'):
                        list(web._bounded_backup_lines(path))

    async def test_download_names_carry_schema5(self):
        project = self.project('Names', 2)
        with mock.patch.object(web, 'tracker', self.tracker):
            names = []
            for project_id in (None, project['id']):
                writer = FakeWriter()
                await web._send_tracking_backup(writer, 'HEAD', project_id)
                names.append(bytes(writer.buf))
            archive = await self.tracker.archive_project_async(project['id'])
            writer = FakeWriter()
            await web._send_tracking_archive(writer, archive['id'], 'HEAD')
            names.append(bytes(writer.buf))
        self.assertIn(b'filename=tracking-schema5.ndjson', names[0])
        self.assertIn(b'filename=project-archive-schema5.ndjson', names[1])
        self.assertIn(b'filename=survey-archive-schema5.ndjson', names[2])
        # W11: an archive written by older firmware keeps its own schema in the name.
        self.tracker.archives[0]['schema_version'] = 4
        writer = FakeWriter()
        with mock.patch.object(web, 'tracker', self.tracker):
            await web._send_tracking_archive(writer, archive['id'], 'HEAD')
        self.assertIn(b'filename=survey-archive-schema4.ndjson', bytes(writer.buf))
        # A malformed index value never reaches the header.
        self.tracker.archives[0]['schema_version'] = '4\r\nX-Injected: 1'
        writer = FakeWriter()
        with mock.patch.object(web, 'tracker', self.tracker):
            await web._send_tracking_archive(writer, archive['id'], 'HEAD')
        self.assertIn(b'filename=survey-archive-schema5.ndjson', bytes(writer.buf))
        self.assertNotIn(b'X-Injected', bytes(writer.buf))

    async def test_json_restore_reports_skipped_legacy_values(self):
        # W8: the browser warns from this field; a schema-4 asset whose stored
        # projection is out of range keeps the asset and nulls the projection.
        measurement = {"recorded_at": 3, "fix_quality": 4, "fix_status": "RTK_FIXED",
                       "satellites": 24, "hdop": 0.6, "quality": {"accepted": True, "reasons": []}}
        payload = {"kind": "survey_backup", "schema_version": 4, "profiles": [],
                   "projects": [{"id": "p1", "name": "Old", "description": "", "created_at": 1,
                                 # One vertex: an asset-only async restore fails on the
                                 # missing staged point manifest (pre-existing, reported).
                                 "lines": [{"id": "l1", "name": "L", "state": "finished",
                                            "created_at": 2, "finished_at": 4, "properties": {},
                                            "coordinates": [[8.0, 50.0, 100.0]],
                                            "measurements": [dict(measurement, recorded_at=2)]}],
                                 "points": [{
                                     "id": "a1", "object_type": "valve", "name": "V", "note": "",
                                     "line_id": None, "chainage_m": None, "lateral_offset_m": None,
                                     "side": None, "coordinate": [8.0, 50.0, 100.0],
                                     "projected_coordinate": [8.0, 50.0, 120100.0],
                                     "measurement": measurement}]}],
                   "active_project_id": "p1", "active_line_id": None, "auto_distance_m": 1.0}
        body = json.dumps({"action": "restore", "backup": payload}).encode()
        writer = FakeWriter()
        with mock.patch.object(web, 'tracker', self.tracker), \
                mock.patch.object(web, '_admin_allowed', return_value=True), \
                support.patch_module_global('tracking', 'log'):
            await web._handle_tracking_request(writer, '/api/tracking', 'POST', '', body,
                                               False, {})
        self.assertIn(b'200 OK', writer.buf)
        response = json.loads(bytes(writer.buf).split(b'\r\n\r\n', 1)[1])
        self.assertEqual(response['legacy_values_skipped'], 1)
        self.assertIsNone(self.tracker.projects[0]['points'][0]['projected_coordinate'])

    async def test_truncated_upload_preserves_data_and_cleans_upload(self):
        self.project('Keep', 4)
        before = self.tracker.backup()
        class Reader:
            async def read(self, amount): return b''
        writer = FakeWriter()
        with mock.patch.object(web, 'tracker', self.tracker), \
                mock.patch.object(web, '_admin_allowed', return_value=True):
            await web._handle_tracking_import(Reader(), writer, b'{', 100, False,
                                               {'content-type': 'application/x-ndjson'})
        self.assertIn(b'400 Bad Request', writer.buf)
        self.assertEqual(self.tracker.backup(), before)
        self.assertFalse(self.tracker.maintenance_active())
        self.assertIsNone(web.app.maintenance_reason)
        self.assertFalse(os.path.exists(self.tracker.path + '.upload.ndjson'))

    async def test_unauthenticated_upload_is_not_read(self):
        class Reader:
            async def read(self, amount): raise AssertionError('Read before authorization')
        writer = FakeWriter()
        with mock.patch.object(web, '_admin_allowed', return_value=False):
            await web._handle_tracking_import(Reader(), writer, b'', 100, False, {})
        self.assertIn(b'401 Unauthorized', writer.buf)

    async def test_oversized_record_rejected_with_bounded_reader(self):
        path = os.path.join(self.folder.name, 'oversized.ndjson')
        with open(path, 'wb') as target: target.write(b'x' * 40000)
        with self.assertRaisesRegex(ValueError, 'too large'):
            list(web._bounded_backup_lines(path))

    async def test_restore_failure_before_publication_preserves_original(self):
        self.project('Original', 64)
        rows = list(self.tracker.iter_backup_ndjson())
        before = self.tracker.backup()
        before_files = sorted(os.listdir(self.folder.name))
        def refuse(staging):
            # Staged and verified, but nothing of the live survey has moved.
            self.assertEqual(self.tracker.backup(), before)
            self.assertTrue(any('.restore.' in name for name in os.listdir(self.folder.name)))
            raise ValueError('publication failure')
        with mock.patch.object(self.tracker, '_write_restore_commit', side_effect=refuse):
            with self.assertRaisesRegex(ValueError, 'publication failure'):
                await self.tracker.restore_ndjson_async(iter(rows))
        self.assertEqual(self.tracker.backup(), before)
        self.assertEqual(sorted(os.listdir(self.folder.name)), before_files)

    def staged(self, name):
        self.project('Original', 16)
        self.tracker._write_checkpoint(); self.tracker._write_index()
        before = self.tracker.backup()
        payload = json.loads(json.dumps(before))
        payload['projects'][0]['name'] = name
        staging = self.tracker._fresh_restore_store()
        staging._batch_restore = True
        for event in staging._events_for_backup(payload): staging._append(event, defer_index=True)
        staging._write_checkpoint(); staging._write_index()
        return before, payload, staging

    def reopened_backup(self):
        reopened = Tracker(self.tracker.path)
        try: return reopened.backup()
        finally: reopened.point_store.close(); reopened._sync_journal(True)

    async def test_cancel_at_any_step_keeps_the_original_or_completes_the_restore(self):
        before, payload, staging = self.staged('Replacement')
        staging._sync_journal(True); staging.point_store.close()
        self.tracker._sync_journal(True); self.tracker.point_store.close()
        outcomes = []
        for stop in range(1, 200):
            folder = tempfile.TemporaryDirectory()
            self.addCleanup(folder.cleanup)
            # Boot drops an unpublished staging, so it arrives after the boot.
            names = os.listdir(self.folder.name)
            for staged in (False, True):
                for name in names:
                    if ('.restore.' in name) == staged:
                        shutil.copy(os.path.join(self.folder.name, name), folder.name)
                if not staged: tracker = Tracker(os.path.join(folder.name, 'survey'))
            copy = Tracker(os.path.join(folder.name, 'survey.restore'))
            steps = tracker._activate_staged_restore_steps(copy, payload)
            finished = True
            try:
                for index, _ in enumerate(steps, 1):
                    if index == stop:
                        finished = False
                        break
            finally: steps.close()
            state = tracker.backup()
            names = [p['name'] for p in state['projects']]
            self.assertIn(names, (['Original'], ['Replacement']), stop)
            reopened = Tracker(tracker.path)
            try: self.assertEqual(reopened.backup(), state, stop)
            finally: reopened.point_store.close(); reopened._sync_journal(True)
            tracker.point_store.close(); tracker._sync_journal(True)
            self.assertFalse(any('.restore.' in name for name in os.listdir(folder.name)), stop)
            outcomes.append(names == ['Replacement'])
            if finished: break
        self.assertFalse(outcomes[0]); self.assertTrue(outcomes[-1])
        self.assertEqual(outcomes, sorted(outcomes))

    async def test_error_after_publication_completes_the_restore(self):
        before, payload, staging = self.staged('Replacement')
        install, failures = Tracker._install_file, []
        def flaky(tracker, source, target):
            if not failures:
                failures.append(target); raise OSError('flash busy')
            return install(tracker, source, target)
        with mock.patch.object(Tracker, '_install_file', flaky):
            self.tracker._commit_staged_restore(staging, payload)
        self.assertTrue(failures)
        self.assertEqual([p['name'] for p in self.tracker.backup()['projects']], ['Replacement'])
        self.assertEqual(self.reopened_backup(), self.tracker.backup())
        self.assertFalse(os.path.exists(self.tracker.restore_commit_path))

    async def test_persistent_error_after_publication_stops_writes_until_boot(self):
        before, payload, staging = self.staged('Replacement')
        with mock.patch.object(Tracker, '_install_file', side_effect=OSError('flash broken')), \
                support.patch_module_global('tracking', 'log') as log:
            with self.assertRaisesRegex(OSError, 'flash broken'):
                self.tracker._commit_staged_restore(staging, payload)
        self.assertFalse(self.tracker.is_loaded())
        self.assertIn('next boot completes it', str(log.call_args_list))
        self.assertTrue(os.path.exists(self.tracker.restore_commit_path))
        with self.assertRaisesRegex(ValueError, 'not installed yet'):
            self.tracker._fresh_restore_store()
        restarted = self.reopened_backup()
        self.assertEqual([p['name'] for p in restarted['projects']], ['Replacement'])
        self.assertFalse(os.path.exists(self.tracker.restore_commit_path))
        self.assertFalse(any('.restore.' in name for name in os.listdir(self.folder.name)))

    def files(self):
        result = {}
        for name in os.listdir(self.folder.name):
            with open(os.path.join(self.folder.name, name), 'rb') as source:
                result[name] = source.read()
        return result

    async def test_the_worker_does_not_write_the_old_survey_over_a_failed_install(self):
        # The index is installed, the checkpoint is refused. The old survey
        # still in memory must not reach flash: a rewritten index or a
        # compaction would mix it into the published restore for good.
        before, payload, staging = self.staged('Replacement')
        install = Tracker._install_file
        def refuse_checkpoint(tracker, source, target):
            if target == self.tracker.checkpoint_path: raise OSError(errno.EIO, 'flash broken')
            return install(tracker, source, target)
        def queue_work():
            self.tracker._index_dirty_events, self.tracker._index_dirty_since = 3, 0
            self.tracker._checkpoint_dirty_events = tracking.CHECKPOINT_BATCH_EVENTS
            self.tracker._pending_fixes.append(dict(FIX))
        queue_work()
        with mock.patch.object(Tracker, '_install_file', refuse_checkpoint), \
                support.patch_module_global('tracking', 'log'):
            with self.assertRaises(OSError):
                self.tracker._commit_staged_restore(staging, payload)
        self.assertEqual(self.tracker.pending_count(), 0)
        self.assertEqual((self.tracker._index_dirty_events,
                          self.tracker._checkpoint_dirty_events), (0, 0))
        # Work that slips in afterwards still waits for the boot.
        queue_work()
        files = self.files()
        stop, app = asyncio.Event(), mock.MagicMock(stats={})
        async def stop_later():
            await asyncio.sleep(.3); stop.set()
        import state
        with mock.patch.object(state, 'shutdown_event', stop), \
                mock.patch.object(state, 'app', app), \
                mock.patch.object(tracking.time, 'ticks_ms', return_value=10 ** 6):
            await asyncio.gather(asyncio.wait_for(self.tracker.run_worker(), 5), stop_later())
        self.assertEqual(self.files(), files)
        self.assertEqual(self.tracker.pending_count(), 1)
        self.assertEqual([p['name'] for p in self.reopened_backup()['projects']], ['Replacement'])

    async def test_a_record_that_cannot_be_removed_is_no_success(self):
        # A record left behind would make the next boot install the restore
        # again and drop everything recorded after it.
        before, payload, staging = self.staged('Replacement')
        real_remove = os.remove
        def remove(path):
            if path == self.tracker.restore_commit_path: raise OSError(errno.EIO, 'busy')
            return real_remove(path)
        with mock.patch.object(tracking.os, 'remove', remove), support.patch_module_global('tracking', 'log'):
            with self.assertRaises(OSError):
                self.tracker._commit_staged_restore(staging, payload)
        self.assertFalse(self.tracker.is_loaded())
        self.assertTrue(os.path.exists(self.tracker.restore_commit_path))
        self.assertEqual([p['name'] for p in self.reopened_backup()['projects']], ['Replacement'])
        self.assertFalse(os.path.exists(self.tracker.restore_commit_path))

    def published(self):
        """A restore whose record is renamed into place, nothing installed yet."""
        before, payload, staging = self.staged('Replacement')
        staging._sync_journal(True); staging.point_store.close()
        self.tracker._sync_journal(True); self.tracker.point_store.close()
        self.tracker._write_restore_commit(staging)
        return sorted(os.listdir(self.folder.name))

    def boot_with_failing_stat(self, failing):
        real_stat = os.stat
        def stat(path, *args):
            if path == failing: raise OSError(errno.EIO, 'read error')
            return real_stat(path, *args)
        tracker = Tracker(self.tracker.path, autoload=False, point_store_autoload=False)
        try:
            with mock.patch.object(tracking.os, 'stat', stat), support.patch_module_global('tracking', 'log'):
                with self.assertRaises((OSError, ValueError)):
                    tracker.initialize()
        finally: tracker.point_store.close(); tracker._sync_journal(True)

    async def test_an_unreadable_record_is_not_taken_for_a_missing_one(self):
        files = self.published()
        self.boot_with_failing_stat(self.tracker.restore_commit_path)
        # Nothing was discarded; a boot that can read the record completes it.
        self.assertEqual(sorted(os.listdir(self.folder.name)), files)
        self.assertEqual([p['name'] for p in self.reopened_backup()['projects']], ['Replacement'])

    async def test_an_unreadable_staged_file_is_not_taken_for_an_installed_one(self):
        self.published()
        staged_checkpoint = self.tracker.path + '.restore.checkpoint.json'
        self.boot_with_failing_stat(staged_checkpoint)
        self.assertTrue(os.path.exists(self.tracker.restore_commit_path))
        self.assertTrue(os.path.exists(staged_checkpoint))
        self.assertEqual([p['name'] for p in self.reopened_backup()['projects']], ['Replacement'])

    async def test_an_old_journal_segment_that_cannot_be_removed_stops_the_install(self):
        self.published()
        old = self.tracker._segment_path(99)
        with open(old, 'w') as target: target.write('{}\n')
        real_remove = os.remove
        def remove(path):
            if path == old: raise OSError(errno.EIO, 'busy')
            return real_remove(path)
        tracker = Tracker(self.tracker.path, autoload=False, point_store_autoload=False)
        try:
            with mock.patch.object(tracking.os, 'remove', remove), support.patch_module_global('tracking', 'log'):
                with self.assertRaises(OSError): tracker.initialize()
        finally: tracker.point_store.close(); tracker._sync_journal(True)
        self.assertTrue(os.path.exists(self.tracker.restore_commit_path))
        self.assertEqual([p['name'] for p in self.reopened_backup()['projects']], ['Replacement'])
        self.assertFalse(os.path.exists(old))

    async def startup_state(self, tracker):
        from support import main, state as app_state
        stop = asyncio.Event(); stop.set()
        with mock.patch.object(main, 'tracker', tracker), \
                mock.patch.object(main, 'shutdown_event', stop), \
                mock.patch.object(main, 'log'), support.patch_module_global('tracking', 'log'):
            await main.tracking_startup_task()
        return dict(app_state.app.stats['tracking_startup'])

    async def test_status_names_a_restore_that_stops_every_start_up(self):
        self.published()
        with open(self.tracker.restore_commit_path, 'w') as target: target.write('{')
        for _boot in range(2):
            tracker = Tracker(self.tracker.path, autoload=False, point_store_autoload=False)
            try: startup = await self.startup_state(tracker)
            finally: tracker.point_store.close(); tracker._sync_journal(True)
            self.assertEqual((startup['state'], startup['code']),
                             ('error', 'restore_commit_unreadable'))
            self.assertIn('known-limitations', startup['message'])
        self.assertTrue(os.path.exists(self.tracker.restore_commit_path))

    async def test_status_names_a_roll_forward_that_keeps_failing(self):
        self.published()
        install = Tracker._install_file
        def refuse(tracker, source, target):
            if target == self.tracker.checkpoint_path: raise OSError(errno.EIO, 'flash broken')
            return install(tracker, source, target)
        tracker = Tracker(self.tracker.path, autoload=False, point_store_autoload=False)
        try:
            with mock.patch.object(Tracker, '_install_file', refuse):
                startup = await self.startup_state(tracker)
        finally: tracker.point_store.close(); tracker._sync_journal(True)
        self.assertEqual((startup['state'], startup['code']), ('error', 'restore_install_failed'))
        self.assertIn('flash broken', startup['message'])

    async def test_other_recovery_failures_carry_a_generic_code(self):
        tracker = Tracker(self.tracker.path, autoload=False, point_store_autoload=False)
        try:
            with mock.patch.object(tracker, '_load_steps', side_effect=ValueError('bad journal')):
                startup = await self.startup_state(tracker)
        finally: tracker.point_store.close(); tracker._sync_journal(True)
        self.assertEqual(startup, {'state': 'error', 'code': 'survey_recovery_failed',
                                   'message': 'bad journal'})

    async def test_restore_upload_during_a_pending_install_asks_for_a_restart(self):
        before, payload, staging = self.staged('Replacement')
        install = Tracker._install_file
        def refuse(tracker, source, target):
            if target == self.tracker.checkpoint_path: raise OSError(errno.EIO, 'flash broken')
            return install(tracker, source, target)
        from support import state as app_state
        with mock.patch.object(Tracker, '_install_file', refuse), support.patch_module_global('tracking', 'log'):
            with self.assertRaises(OSError):
                self.tracker._commit_staged_restore(staging, payload)
        self.assertEqual(app_state.app.stats['tracking_startup']['code'],
                         'restore_install_pending')
        class Reader:
            async def read(self, amount): raise AssertionError('Read during a pending install')
        writer = FakeWriter()
        with mock.patch.object(web, 'tracker', self.tracker), \
                mock.patch.object(web, '_admin_allowed', return_value=True):
            await web._handle_tracking_import(Reader(), writer, b'', 100, False,
                                               {'content-type': 'application/x-ndjson'})
        self.assertIn(b'409 Conflict', writer.buf)
        body = json.loads(bytes(writer.buf).split(b'\r\n\r\n', 1)[1])
        self.assertEqual(body['error'], 'restore_install_pending')
        self.assertIn('Restart the device', body['message'])

    async def test_restore_upload_after_a_failed_start_up_names_the_cause(self):
        from support import state as app_state
        self.tracker._loaded = False
        writer = FakeWriter()
        class Reader:
            async def read(self, amount): raise AssertionError('Read without a survey')
        with mock.patch.dict(app_state.app.stats, {'tracking_startup': {
                    'state': 'error', 'code': 'restore_commit_unreadable', 'message': 'x'}}), \
                mock.patch.object(web, 'tracker', self.tracker), \
                mock.patch.object(web, '_admin_allowed', return_value=True):
            await web._handle_tracking_import(Reader(), writer, b'', 100, False,
                                               {'content-type': 'application/x-ndjson'})
        self.assertIn(b'503 Service Unavailable', writer.buf)
        body = json.loads(bytes(writer.buf).split(b'\r\n\r\n', 1)[1])
        self.assertEqual(body['error'], 'survey_unavailable')
        self.assertIn('restore_commit_unreadable', body['message'])

    def assert_released(self):
        self.assertIsNone(web.app.maintenance_reason)
        self.assertFalse(self.tracker.maintenance_active())
        self.assertEqual(self.tracker.storage_readers, 0)

    async def nested_restore(self, side_effect, cancel):
        self.project('Nested', 2)
        started = asyncio.Event()
        async def restore(backup):
            started.set()
            if side_effect:
                raise side_effect
            await asyncio.sleep(10)
        writer = FakeWriter()
        with mock.patch.object(web, 'tracker', self.tracker), \
                mock.patch.object(web, '_admin_allowed', return_value=True), \
                mock.patch.object(self.tracker, 'restore_async', mock.AsyncMock(side_effect=restore)), \
                support.patch_module_global('tracking', 'log'), mock.patch.object(web, 'log'):
            task = asyncio.create_task(web._handle_tracking_request(
                writer, '/api/tracking', 'POST', '',
                b'{"action":"restore","backup":{}}', False, {}))
            await started.wait()
            await asyncio.sleep(0)
            if cancel:
                self.assertEqual(web.app.maintenance_reason, 'tracking_restore')
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            else:
                await task
        return writer

    async def test_nested_restore_value_error_answers_400_and_releases(self):
        writer = await self.nested_restore(ValueError('bad'), False)
        self.assertIn(b'400 Bad Request', writer.buf)
        self.assert_released()

    async def test_nested_restore_cancelled_releases(self):
        await self.nested_restore(None, True)
        self.assert_released()

    async def test_import_cancelled_while_reading_releases_and_removes_upload(self):
        self.project('Import', 2)
        reading = asyncio.Event()
        class Reader:
            async def read(self, amount):
                reading.set()
                await asyncio.sleep(10)
        with mock.patch.object(web, 'tracker', self.tracker), \
                mock.patch.object(web, '_admin_allowed', return_value=True):
            task = asyncio.create_task(web._handle_tracking_import(
                Reader(), FakeWriter(), b'', 100, False,
                {'content-type': 'application/x-ndjson'}))
            await reading.wait()
            self.assertEqual(web.app.maintenance_reason, 'tracking_restore')
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assert_released()
        self.assertFalse(os.path.exists(self.tracker.path + '.upload.ndjson'))

    async def test_export_cancelled_during_dispatch_releases_reader(self):
        self.project('Export', 2)
        dispatching = asyncio.Event()
        async def dispatch(*args, **kwargs):
            dispatching.set()
            await asyncio.sleep(10)
        with mock.patch.object(web, 'tracker', self.tracker), \
                mock.patch.object(web, '_admin_allowed', return_value=True), \
                mock.patch.object(web, '_dispatch_tracking_request', dispatch):
            task = asyncio.create_task(web._handle_tracking_request(
                FakeWriter(), '/api/tracking/export', 'GET', 'format=backup', b'', False, {}))
            await dispatching.wait()
            self.assertEqual(self.tracker.storage_readers, 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assert_released()
