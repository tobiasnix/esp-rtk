# SPDX-License-Identifier: AGPL-3.0-only
"""Maintenance must yield, preserve old data on failure, and exclude writers."""
import asyncio
import json
import os
import tempfile
import unittest
from unittest import mock

from support import web
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
            self.tracker.add_vertex(dict(FIX, lon=8 + index * .00002))
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
                await self.tracker.reactivate_archive_async(archive['id'])
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
        self.assertFalse(self.tracker._compacting)
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
        self.tracker.storage_readers = 1
        writer = FakeWriter()
        with mock.patch.object(web, 'tracker', self.tracker), \
                mock.patch.object(web, '_admin_allowed', return_value=True):
            action = asyncio.create_task(web._handle_tracking_request(
                writer, '/api/tracking', 'POST', '', b'{"action":"compact"}', False, {}))
            await asyncio.sleep(.005)
            self.assertTrue(self.tracker.storage_busy)
            conflict = FakeWriter()
            await web._handle_tracking_request(conflict, '/api/tracking', 'POST', '',
                b'{"action":"delete_project"}', False, {})
            self.assertIn(b'409 Conflict', conflict.buf)
            self.tracker.storage_readers = 0
            await action
        self.assertIn(b'200 OK', writer.buf)
        self.assertFalse(self.tracker.storage_busy)

    async def test_import_receives_more_than_old_body_limit_without_whole_body_buffer(self):
        self.project('Restore', 96)
        rows = list(self.tracker.iter_compact_archive(self.tracker.active_project_id))
        # Legal leading whitespace remains part of the source integrity digest;
        # increase via actual vertices instead of modifying the protected bytes.
        for index in range(96, 600):
            line = self.tracker.projects[0]['lines'][0]
            self.tracker.active_line_id = line['id']; line['state'] = 'recording'
            self.tracker.add_vertex(dict(FIX, lon=8 + index * .00002))
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
        self.assertEqual(self.tracker.feature_count(), 600)
        self.assertFalse(os.path.exists(self.tracker.path + '.upload.ndjson'))
        self.assertFalse(self.tracker.storage_busy)

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
        self.assertFalse(self.tracker.storage_busy)
        self.assertFalse(os.path.exists(self.tracker.path + '.upload.ndjson'))

    async def test_unauthenticated_upload_is_not_read(self):
        class Reader:
            async def read(self, amount): raise AssertionError('Read before authorization')
        writer = FakeWriter()
        with mock.patch.object(web, '_admin_allowed', return_value=False):
            await web._handle_tracking_import(Reader(), writer, b'', 100, False, {})
        self.assertIn(b'403 Forbidden', writer.buf)

    async def test_oversized_record_rejected_with_bounded_reader(self):
        path = os.path.join(self.folder.name, 'oversized.ndjson')
        with open(path, 'wb') as target: target.write(b'x' * 40000)
        with self.assertRaisesRegex(ValueError, 'too large'):
            list(web._bounded_backup_lines(path))

    async def test_restore_activation_failure_after_segment_moves_preserves_original(self):
        self.project('Original', 64)
        rows = list(self.tracker.iter_backup_ndjson())
        before = self.tracker.backup()
        before_files = sorted(self.tracker.point_store.sidecar_paths())
        def refuse(*args, **kwargs):
            # The active generation remains usable even though the staged
            # segments have already moved into the future generation.
            self.assertEqual(self.tracker.backup(), before)
            self.assertIsNotNone(kwargs.get('prepared_segments'))
            raise ValueError('activation failure')
        with mock.patch.object(self.tracker, '_activate_staged_restore_steps', side_effect=refuse):
            with self.assertRaisesRegex(ValueError, 'activation failure'):
                await self.tracker.restore_ndjson_async(iter(rows))
        self.assertEqual(self.tracker.backup(), before)
        self.assertEqual(sorted(self.tracker.point_store.sidecar_paths()), before_files)

    async def test_cancel_between_metadata_switches_rolls_back_before_publication(self):
        self.project('Original', 16)
        self.tracker._write_checkpoint(); self.tracker._write_index()
        before = self.tracker.backup()
        payload = json.loads(json.dumps(before))
        payload['projects'][0]['name'] = 'Replacement'
        staging = self.tracker._fresh_restore_store()
        staging._batch_restore = True
        for event in staging._events_for_backup(payload): staging._append(event, defer_index=True)
        staging._write_checkpoint(); staging._write_index()
        steps = self.tracker._activate_staged_restore_steps(staging, payload)
        paused = False
        try:
            for _ in steps:
                if not os.path.exists(staging.checkpoint_path):
                    # Both new metadata files have moved, but the original point
                    # manifest is still active. Closing simulates cancellation.
                    paused = True
                    break
        finally: steps.close()
        self.assertTrue(paused)
        self.assertEqual(self.tracker.backup(), before)
        reopened = Tracker(self.tracker.path)
        try: self.assertEqual(reopened.backup(), before)
        finally: reopened.point_store.close(); reopened._sync_journal(True)
        self.tracker._remove_storage(staging)
