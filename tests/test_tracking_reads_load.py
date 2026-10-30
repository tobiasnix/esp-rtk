# SPDX-License-Identifier: AGPL-3.0-only
import asyncio
import contextlib
import json
import os
import tempfile
import unittest
from unittest import mock

from support import web
import parallel_benchmark
from tracking import Tracker
import tracking_reference


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

    @staticmethod
    async def filled_tracker(tmp):
        """129 vertices of one line, compacted and reloaded from flash."""
        prefix = os.path.join(tmp, 'tracking')
        original = Tracker(prefix)
        original.create_project('Filled map')
        line = original.start_line('L-1', 0.2)
        for index in range(1, 130):
            original.add_vertex(parallel_benchmark._synthetic_fix(index), source='automatic')
        await original.compact_async()
        original.point_store.close()
        return Tracker(prefix), line

    async def stream_turns(self, tracker, path, query, clock=None):
        """Body and the decodes per loop turn of one request on a fast socket.

        clock, given the decodes so far, is ticks_ms during the request.
        """
        class Writer:
            def __init__(self):
                self.buf = bytearray()

            def write(self, value):
                self.buf.extend(value)

            async def drain(self):
                pass

        tracker.point_store.close_cache()
        writer, batches, reads, total = Writer(), [], [0], [0]
        decode = tracker.point_store._decode

        def decode_point(value, include_measurement=True):
            reads[0] += 1
            total[0] += 1
            return decode(value, include_measurement)

        async def another_service():
            while True:
                batches.append(reads[0])
                reads[0] = 0
                await asyncio.sleep(0)

        ticks = (mock.patch.object(web.time, 'ticks_ms', new=lambda: clock(total[0]))
                 if clock else contextlib.nullcontext())
        task = asyncio.create_task(another_service())
        try:
            with mock.patch.object(web, 'tracker', tracker), \
                    mock.patch.object(web, '_admin_allowed', return_value=True), \
                    mock.patch.object(tracker.point_store, '_decode', side_effect=decode_point), \
                    mock.patch('pointstore.open', wraps=open, create=True) as opened, \
                    mock.patch.object(web, 'log'), ticks:
                await web._handle_tracking_request(
                    writer, path, 'GET', query, b'', False, {})
                # The export reads each segment once per pass (geometry, measurements).
                passes = 2 if path == '/api/tracking/export' else 1
                self.assertLessEqual(opened.call_count,
                                     passes * len(tracker.point_store.segments))
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        batches.append(reads[0])
        header, raw = bytes(writer.buf).split(b'\r\n\r\n', 1)
        self.assertIn(b'200 OK', header)
        return raw, batches, total[0]

    async def test_populated_map_detail_and_geojson_yield_with_fast_socket(self):
        with tempfile.TemporaryDirectory() as tmp:
            tracker, line = await self.filled_tracker(tmp)
            expected_map = tracker.map_data(50)
            expected_detail = tracker.line_detail(line['id'], 79, 50)
            expected_geojson = tracking_reference.geojson(tracker)

            for path, query, expected, clock in (
                ('/api/tracking/map', 'limit=50', expected_map, None),
                ('/api/tracking/line', 'line_id=%s&offset=79&limit=50' % line['id'],
                 expected_detail, None),
                # The export yields by elapsed time; the fast host needs a clock.
                ('/api/tracking/export', '', expected_geojson, lambda decodes: 5 * decodes),
            ):
                raw, batches, _decodes = await self.stream_turns(tracker, path, query, clock)
                self.assertEqual(json.loads(raw), expected, path)
                if path == '/api/tracking/export':
                    # Coordinates travel as exact decimal text, never as stored integers.
                    self.assertRegex(raw.decode(), r'"coordinates":\[\[-?\d+\.\d{8},-?\d+\.\d{8},-?\d+\.\d{3}\]')
                self.assertGreater(sum(batches), 16, path)
                # Four vertices plus the preceding vertex of a detail page.
                self.assertLessEqual(max(batches), 5, path)
                self.assertGreater(len(batches), 2, path)
            self.assertEqual(expected_map['lines'][0]['vertex_count'], 129)
            self.assertEqual(len(expected_map['lines'][0]['coordinates']), 50)
            self.assertEqual(expected_detail['vertices'][0]['number'], 80)
            self.assertFalse(expected_detail['has_more_vertices'])
            tracker.point_store.close()

    async def test_geojson_export_paces_by_time_not_by_chunks(self):
        with tempfile.TemporaryDirectory() as tmp:
            tracker, _line = await self.filled_tracker(tmp)
            expected = tracking_reference.geojson(tracker)
            # 1 ms per decode: about one yield per 20 decodes (every fourth chunk
            # would give one per two decodes).
            raw, batches, decodes = await self.stream_turns(
                tracker, '/api/tracking/export', '', lambda count: count)
            self.assertEqual(json.loads(raw), expected)
            self.assertLessEqual(decodes / 25, len(batches))
            self.assertLessEqual(len(batches), decodes / 15)
            self.assertLessEqual(max(batches), 21)
            # 30 ms per decode: a yield after every vertex chunk.
            raw, batches, decodes = await self.stream_turns(
                tracker, '/api/tracking/export', '', lambda count: 30 * count)
            self.assertEqual(json.loads(raw), expected)
            self.assertLessEqual(max(batches), 2)
            self.assertGreaterEqual(len(batches), decodes / 2)
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
                    mock.patch.object(tracker.point_store, '_decode', side_effect=ValueError('Invalid record')), \
                    mock.patch.object(web, 'log'):
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
