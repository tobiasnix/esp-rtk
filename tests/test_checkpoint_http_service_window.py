# SPDX-License-Identifier: AGPL-3.0-only
"""HTTP request chains must finish between successive checkpoint flash phases.

This is a scheduler regression, not a model of native flash duration or proof
of hardware qualification. Real board tests retain the ten-second service gate.
"""
import asyncio
import os
import tempfile
import unittest
from unittest import mock

from support import web
import tracking
import pointstore
import parallel_benchmark


class TestCheckpointHttpServiceWindow(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, service_window=None):
        tasks, responses, observed = [], [], []

        class Reader:
            def __init__(self):
                self.parts = [b'GET /sta', b'tus HTTP/1.1\r\n', b'Host: test.invalid\r\n\r\n']
            async def read(self, _amount):
                # A fragmented request needs multiple I/O resumptions through
                # the actual HTTP reader's wait_for tasks.
                await asyncio.sleep(.003)
                return self.parts.pop(0) if self.parts else b''

        class Writer:
            def __init__(self): self.data = bytearray(); self.completed = False
            def get_extra_info(self, key):
                return ('127.0.0.1', 1234) if key == 'peername' else ('127.0.0.1', 80)
            def write(self, raw): self.data.extend(raw)
            async def drain(self):
                await asyncio.sleep(.003)
                self.completed = True
            def close(self): pass
            async def wait_closed(self): await asyncio.sleep(0)

        async def status_response(writer, *_args):
            web._send_json(writer, {'fixture': 'checkpoint service window'})

        def request():
            writer = Writer(); responses.append(writer)
            tasks.append(asyncio.create_task(web.handle_http_client(Reader(), writer)))

        with tempfile.TemporaryDirectory(prefix='checkpoint-http-window-') as temporary:
            tracker = tracking.Tracker(os.path.join(temporary, 'tracking'))
            tracker.create_project('HTTP service window')
            tracker.start_line('Synthetic vertices', .2)
            for index in range(1, 17):
                tracker.add_vertex(parallel_benchmark._synthetic_fix(index), source='automatic')
            before = ''.join(tracker.iter_backup_ndjson())
            write_checkpoint, write_index = tracker._write_checkpoint, tracker._write_index
            load_async = pointstore.FlashPointStore.load_async

            def checkpoint():
                write_checkpoint()
                request()

            def index():
                observed.append(responses[0].completed)
                write_index()
                request()

            async def verify(store, *args, **kwargs):
                observed.append(responses[1].completed)
                return await load_async(store, *args, **kwargs)

            configured = tracking.CHECKPOINT_IO_SERVICE_MS if service_window is None else service_window
            try:
                with mock.patch.object(tracking, 'CHECKPOINT_IO_SERVICE_MS', configured), \
                     mock.patch.object(tracker, '_write_checkpoint', side_effect=checkpoint), \
                     mock.patch.object(tracker, '_write_index', side_effect=index), \
                     mock.patch.object(pointstore.FlashPointStore, 'load_async', verify), \
                     mock.patch.object(web, '_handle_status_request', status_response), \
                     mock.patch.dict(web.app.stats, {'http_errors': 0}), \
                     mock.patch.object(web, '_http_connections', 0):
                    await tracker.compact_async()
                    await asyncio.gather(*tasks)
                    self.assertEqual(web.app.stats['http_errors'], 0)
                self.assertEqual(''.join(tracker.iter_backup_ndjson()), before)
                for response in responses:
                    self.assertTrue(response.data.startswith(b'HTTP/1.0 200 OK'))
                reloaded = tracking.Tracker(tracker.path)
                try:
                    self.assertEqual(''.join(reloaded.iter_backup_ndjson()), before)
                finally:
                    reloaded.point_store.close(); reloaded._sync_journal(close=True)
            finally:
                for task in tasks:
                    if not task.done(): task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                tracker.point_store.close(); tracker._sync_journal(close=True)
        return observed

    async def test_single_millisecond_yield_can_defer_responses_across_flash_phases(self):
        self.assertEqual(await self.exercise(1), [False, False])

    async def test_configured_window_finishes_http_before_next_flash_phase(self):
        self.assertEqual(await self.exercise(), [True, True])

    async def test_append_during_service_window_aborts_pruning_and_survives_reload(self):
        with tempfile.TemporaryDirectory(prefix='checkpoint-window-mutation-') as temporary:
            tracker = tracking.Tracker(os.path.join(temporary, 'tracking'))
            tracker.create_project('Concurrent edit')
            tracker.start_line('Synthetic vertices', .2)
            tracker.add_vertex(parallel_benchmark._synthetic_fix(1), source='automatic')
            previous_journals = [path for _number, path in tracker._segment_files()]

            async def add():
                await asyncio.sleep(.01)
                tracker.add_vertex(parallel_benchmark._synthetic_fix(2), source='automatic')

            adding = asyncio.create_task(add())
            try:
                with self.assertRaisesRegex(ValueError, 'Checkpoint changed'):
                    await tracker.compact_async()
                await adding
                expected = ''.join(tracker.iter_backup_ndjson())
                self.assertTrue(all(os.path.isfile(path) for path in previous_journals))
                tracker.point_store.close(); tracker._sync_journal(close=True)
                loaded = tracking.Tracker(tracker.path)
                try:
                    self.assertEqual(loaded.feature_count(), 2)
                    self.assertEqual(''.join(loaded.iter_backup_ndjson()), expected)
                finally:
                    loaded.point_store.close(); loaded._sync_journal(close=True)
            finally:
                if not adding.done(): adding.cancel()
                await asyncio.gather(adding, return_exceptions=True)
                tracker.point_store.close(); tracker._sync_journal(close=True)
