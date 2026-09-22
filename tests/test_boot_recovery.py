# SPDX-License-Identifier: AGPL-3.0-only
"""Cooperative boot recovery preserves data and rejects partial-state writes."""
import asyncio
import copy
import os
import tempfile
import unittest
from unittest import mock

from support import state, web
import tracking
from pointstore import FlashPointStore
from test_tracking import FIX
from test_http import FakeWriter


class BootRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_buffered_selection_handles_long_utf8_rows_and_final_tail(self):
        with tempfile.TemporaryDirectory() as folder:
            store = FlashPointStore(os.path.join(folder, 'points'))
            for order in range(1, 81):
                note = ('\u00e4\u20ac' * 3000) if order == 31 else 'sample-' + str(order)
                store.append(order, 'a', [8., 50., order],
                             {'record_order': order, 'note': note})
            store.close()
            expected = [store.read(locator) for locator in store.offsets['a']]
            path = store._segment_path(store.segments[-1])
            with open(path, 'rb') as source:
                original = source.read()
            with open(path, 'wb') as target:
                target.write(original.rstrip(b'\n'))
            with mock.patch.object(store, 'read', side_effect=AssertionError('random read')):
                self.assertEqual(list(store.iter_line('a')), expected)
                self.assertEqual(list(store.iter_line('a', 3, 76, 9)), expected[3:76:9])
                self.assertEqual(list(store.iter_line('a', 79)), expected[79:])
                with mock.patch('pointstore._expand', side_effect=AssertionError('unused measurement')):
                    projected = list(store.iter_line('a', include_measurement=False))
                self.assertEqual([r['coordinate'] for r in projected],
                                 [r['coordinate'] for r in expected])
                self.assertTrue(all(r['measurement'] is None for r in projected))
            store.close()

    async def test_stale_checkpoint_and_undo_recover_without_random_point_reads(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(
                tracking.Tracker, 'storage_capacity', return_value={'writable': True}):
            path = os.path.join(folder, 'survey')
            first = tracking.Tracker(path)
            first.create_project('Boot')
            first.start_line('Route', 1, recording_mode='route')
            for index in range(8):
                first.add_vertex(dict(FIX, lon=8 + index * .00002), 'automatic')
            first.compact()
            for index in range(8, 82):
                first.add_vertex(dict(FIX, lon=8 + index * .00002), 'automatic')
            first.undo_last()
            first.set_line_state('finish')
            first._line_summary(first.projects[0]['lines'][0])
            expected = copy.deepcopy(first.backup())
            first._sync_journal(close=True); first.point_store.close()
            with mock.patch.object(FlashPointStore, 'load', side_effect=AssertionError('early scan')):
                recovered = tracking.Tracker(path, autoload=False, point_store_autoload=False)
            self.assertFalse(recovered.enqueue_fix(FIX))
            beats = []
            async def observer():
                while not recovered._loaded:
                    beats.append(recovered.live_status())
                    await asyncio.sleep(0)
            observer_task = asyncio.create_task(observer())
            with mock.patch.object(recovered.point_store, 'read', side_effect=AssertionError('random scan')), \
                    mock.patch.object(recovered, '_line_length', side_effect=AssertionError('duplicate length scan')):
                await recovered.initialize_async()
            await observer_task
            self.assertGreater(len(beats), 5)
            self.assertTrue(all(b['active_project'] is None for b in beats))
            self.assertTrue(all(not b['capacity']['recording_allowed'] for b in beats))
            self.assertEqual(recovered.backup(), expected)
            self.assertEqual(recovered.feature_count(), 81)
            self.assertEqual(recovered._map_changes, [])
            recovered._sync_journal(close=True); recovered.point_store.close()

    async def test_partial_recovery_is_not_exposed_or_mutated_through_http(self):
        with tempfile.TemporaryDirectory() as folder:
            tr = tracking.Tracker(os.path.join(folder, 'survey'), autoload=False,
                                  point_store_autoload=False)
            tr.projects = [{'partially_recovered': True}]
            self.assertEqual(tr.status()['projects'], [])
            for path, method in [('/api/tracking', 'POST'), ('/api/tracking/map', 'GET'),
                                 ('/api/tracking/backup', 'GET')]:
                writer = FakeWriter()
                with mock.patch.object(web, 'tracker', tr), \
                        mock.patch.object(web, '_admin_allowed', return_value=True):
                    await web._handle_tracking_request(writer, path, method, '',
                        b'{"action":"project_create","name":"unsafe"}', False, {})
                self.assertIn(b'503 Service Unavailable', writer.buf)
                self.assertEqual(tr.projects, [{'partially_recovered': True}])

    async def test_sequential_reader_respects_tombstones_interleaving_and_rotation(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch('pointstore.POINT_SEGMENT_BYTES', 350):
            store = FlashPointStore(os.path.join(folder, 'points'))
            for order in range(1, 21):
                store.append(order, 'a' if order % 2 else 'b', [8., 50., order],
                             {'record_order': order, 'source': 'automatic'})
            store.remove_last(21, 'a')
            store.remove_last(22, 'a')
            store.append(23, 'a', [8., 50., 23], {'record_order': 23})
            store.close()
            reopened = FlashPointStore(store.path)
            expected = [reopened.read(locator) for locator in reopened.offsets['a']]
            with mock.patch.object(reopened, 'read', side_effect=AssertionError('random read')):
                self.assertEqual(list(reopened.iter_line('a')), expected)
                self.assertEqual(list(reopened.iter_line('a', 1, 8, 2)), expected[1:8:2])
                projected = list(reopened.iter_line('a', 1, 8, 2, include_measurement=False))
                self.assertEqual([r['coordinate'] for r in projected],
                                 [r['coordinate'] for r in expected[1:8:2]])
                self.assertEqual(list(reopened.iter_line('a', 99)), [])
                snapshot = reopened.iter_line('a', 1)
                reopened.remove_last(24, 'a')
                self.assertEqual(list(snapshot), expected[1:])
            self.assertEqual([r['order'] for r in expected], list(range(1, 17, 2)) + [23])
            reopened.close()

    async def test_coordinate_projection_keeps_legacy_and_record_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            store = FlashPointStore(os.path.join(folder, 'points'))
            legacy = {'schema': 1, 'record': 'vertex', 'order': 1, 'line_id': 'a',
                      'coordinate': [8., 50., 1], 'measurement': {'source': 'manual'}}
            self.assertEqual(store._decode(legacy, False)['coordinate'], legacy['coordinate'])
            store.append(1, 'a', [8., 50., 1], {'source': 'manual'})
            store.close()
            for value in ([9, 1, 'a'], [2, 1, 'a'],
                          [2, 1, 'other', [8., 50., 1], [-1]]):
                with mock.patch('pointstore.ujson.loads', return_value=value):
                    with self.assertRaises(ValueError):
                        list(store.iter_line('a', include_measurement=False))
