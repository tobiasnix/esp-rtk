# SPDX-License-Identifier: AGPL-3.0-only
import asyncio
import json
import os
import tempfile
import unittest
from unittest import mock

from support import web
import parallel_benchmark
from tracking import Tracker


class TestTrackingReadsLoad(unittest.IsolatedAsyncioTestCase):
    async def test_disconnect_closes_map_and_detail_readers(self):
        class BrokenWriter:
            def write(self, value): pass
            async def drain(self):
                if any(not handle.closed for handle in handles):
                    raise OSError('client disconnected')

        with tempfile.TemporaryDirectory() as tmp:
            tracker = Tracker(os.path.join(tmp, 'tracking'))
            tracker.create_project('Interrupted map')
            line = tracker.start_line('L-1', 0.2)
            for index in range(1, 65):
                tracker.add_vertex(parallel_benchmark._synthetic_fix(index), source='automatic')
            tracker.point_store.close()
            for cold in (False, True):
                for path, query in (
                    ('/api/tracking/map', 'limit=50'),
                    ('/api/tracking/line', 'line_id=' + line['id']),
                ):
                    tracker._line_summary(tracker._line(tracker._project()))
                    if cold:
                        tracker._line(tracker._project()).pop('_summary', None)
                    handles = []
                    def open_point_file(*args, **kwargs):
                        handle = open(*args, **kwargs)
                        handles.append(handle)
                        return handle
                    with mock.patch.object(web, 'tracker', tracker), \
                            mock.patch.object(web, '_admin_allowed', return_value=True), \
                            mock.patch('pointstore.open', side_effect=open_point_file, create=True):
                        with self.assertRaisesRegex(OSError, 'client disconnected'):
                            await web._handle_tracking_request(
                                BrokenWriter(), path, 'GET', query, b'', False, {})
                    self.assertTrue(handles)
                    self.assertTrue(all(handle.closed for handle in handles))
            tracker._sync_journal(close=True)

    async def test_populated_map_detail_and_geojson_yield_with_fast_socket(self):
        class Writer:
            def __init__(self):
                self.buf = bytearray()

            def write(self, value):
                self.buf.extend(value)

            async def drain(self):
                pass

        with tempfile.TemporaryDirectory() as tmp:
            prefix = os.path.join(tmp, 'tracking')
            original = Tracker(prefix)
            project = original.create_project('Filled map')
            line = original.start_line('L-1', 0.2)
            for index in range(1, 130):
                original.add_vertex(parallel_benchmark._synthetic_fix(index), source='automatic')
            original.compact()
            original.point_store.close()
            tracker = Tracker(prefix)
            expected_map = tracker.map_data(50)
            expected_detail = tracker.line_detail(line['id'], 79, 50)
            expected_geojson = tracker.geojson()

            for path, query, expected in (
                ('/api/tracking/map', 'limit=50', expected_map),
                ('/api/tracking/line', 'line_id=%s&offset=79&limit=50' % line['id'],
                 expected_detail),
                ('/api/tracking/export', '', expected_geojson),
            ):
                tracker.point_store.close_cache()
                writer, batches, reads = Writer(), [], [0]
                decode = tracker.point_store._decode

                def decode_point(value, include_measurement=True):
                    reads[0] += 1
                    return decode(value, include_measurement)

                async def another_service():
                    while True:
                        batches.append(reads[0])
                        reads[0] = 0
                        await asyncio.sleep(0)

                task = asyncio.create_task(another_service())
                try:
                    with mock.patch.object(web, 'tracker', tracker), \
                            mock.patch.object(web, '_admin_allowed', return_value=True), \
                            mock.patch.object(tracker.point_store, '_decode', side_effect=decode_point), \
                            mock.patch('pointstore.open', wraps=open, create=True) as opened:
                        await web._handle_tracking_request(
                            writer, path, 'GET', query, b'', False, {})
                        if path != '/api/tracking/export':
                            self.assertLessEqual(opened.call_count, len(tracker.point_store.segments))
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                batches.append(reads[0])
                header, raw = bytes(writer.buf).split(b'\r\n\r\n', 1)
                self.assertIn(b'200 OK', header)
                self.assertEqual(json.loads(raw), expected, path)
                self.assertGreater(sum(batches), 16, path)
                # Four vertices plus the preceding vertex of a detail page.
                self.assertLessEqual(max(batches), 5, path)
                self.assertGreater(len(batches), 2, path)
            self.assertEqual(expected_map['lines'][0]['vertex_count'], 129)
            self.assertEqual(len(expected_map['lines'][0]['coordinates']), 50)
            self.assertEqual(expected_detail['vertices'][0]['number'], 80)
            self.assertFalse(expected_detail['has_more_vertices'])
            tracker.point_store.close()

    async def test_missing_line_and_project_fail_before_success_headers(self):
        class Writer:
            def __init__(self): self.buf = bytearray()
            def write(self, value): self.buf.extend(value)
            async def drain(self): pass

        with tempfile.TemporaryDirectory() as tmp:
            tracker = Tracker(os.path.join(tmp, 'tracking'))
            with mock.patch.object(web, 'tracker', tracker), \
                    mock.patch.object(web, '_admin_allowed', return_value=True):
                for path in ('/api/tracking/line', '/api/tracking/export'):
                    writer = Writer()
                    await web._handle_tracking_request(writer, path, 'GET', '', b'', False, {})
                    self.assertTrue(bytes(writer.buf).startswith(b'HTTP/1.0 404 Not Found'))
                    self.assertNotIn(b'200 OK', writer.buf)
            tracker.point_store.close()

    async def test_map_stream_preserves_sampling_and_revision_delta(self):
        with tempfile.TemporaryDirectory() as tmp:
            tracker = Tracker(os.path.join(tmp, 'tracking'))
            tracker.create_project('Sampled map')
            tracker.start_line('L-1', 0.2)
            for index in range(1, 130):
                tracker.add_vertex(parallel_benchmark._synthetic_fix(index), source='automatic')
            coordinates = list(tracker._line(tracker._project())['coordinates'])
            response = json.loads(''.join(tracker.iter_map_data(50)))
            self.assertEqual(response['lines'][0]['coordinates'], coordinates[::2][-50:])
            self.assertTrue(response['truncated'])
            tracker.add_vertex(parallel_benchmark._synthetic_fix(130), source='automatic')
            delta = json.loads(''.join(tracker.iter_map_data(50, since_revision=response['revision'])))
            self.assertTrue(delta['delta'])
            self.assertEqual([item['op'] for item in delta['changes']], ['vertex'])
            tracker.point_store.close()

    async def test_read_error_after_headers_aborts_without_a_second_response(self):
        class Writer:
            def __init__(self): self.buf = bytearray()
            def write(self, value): self.buf.extend(value)
            async def drain(self): pass

        with tempfile.TemporaryDirectory() as tmp:
            tracker = Tracker(os.path.join(tmp, 'tracking'))
            tracker.create_project('Interrupted read')
            line = tracker.start_line('L-1', 0.2)
            tracker.add_vertex(parallel_benchmark._synthetic_fix(1), source='automatic')
            tracker.add_vertex(parallel_benchmark._synthetic_fix(2), source='automatic')
            with mock.patch.object(web, 'tracker', tracker), \
                    mock.patch.object(web, '_admin_allowed', return_value=True), \
                    mock.patch.object(tracker.point_store, '_decode', side_effect=ValueError('Invalid record')):
                for path, query in (
                    ('/api/tracking/line', 'line_id=' + line['id']),
                    ('/api/tracking/export', ''),
                    ('/api/tracking/map', 'limit=50'),
                ):
                    tracker.point_store.close_cache()
                    writer = Writer()
                    with self.assertRaisesRegex(ValueError, 'Invalid record'):
                        await web._handle_tracking_request(writer, path, 'GET', query, b'', False, {})
                    self.assertEqual(bytes(writer.buf).count(b'HTTP/1.0 '), 1)
                    self.assertNotIn(b'404 Not Found', writer.buf)
            tracker.point_store.close()
