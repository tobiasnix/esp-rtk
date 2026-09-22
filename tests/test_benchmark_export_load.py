# SPDX-License-Identifier: AGPL-3.0-only
import asyncio
import os
import tempfile
import unittest
from unittest import mock

from support import web
import parallel_benchmark
from parallel_tracking_test import verify_export
from pointstore import FlashPointStore
from tracking import Tracker


class TestBenchmarkExportLoad(unittest.IsolatedAsyncioTestCase):
    async def test_restart_export_loads_cooperatively_and_preserves_integrity(self):
        class Writer:
            def __init__(self):
                self.buf = bytearray()

            def write(self, data):
                self.buf.extend(data)

            async def drain(self):
                pass  # Fast sockets need not yield in MicroPython.

        with tempfile.TemporaryDirectory() as tmp:
            prefix = os.path.join(tmp, 'benchmark')
            tracker = Tracker(prefix, max_features=10000)
            project = tracker.create_project('Synthetic export test')
            tracker.select_project(project['id'])
            tracker.start_line('Synthetic line', 0.2)
            for index in range(1, 66):
                tracker.add_vertex(parallel_benchmark._synthetic_fix(index), source='automatic')
            tracker.compact()
            tracker.point_store.close()
            writer, turns = Writer(), []

            async def another_service():
                while True:
                    turns.append(True)
                    await asyncio.sleep(0)

            task = asyncio.create_task(another_service())
            try:
                with mock.patch.object(web, '_admin_allowed', return_value=True), \
                        mock.patch.dict(web._instances, {}, clear=True), \
                        mock.patch.object(parallel_benchmark, 'PREFIX', prefix), \
                        mock.patch.object(parallel_benchmark, 'control_status', return_value={
                            'state': 'idle', 'result': {'target_points': 10000, 'confirmed_points': 65}}), \
                        mock.patch.object(FlashPointStore, '_scan_generation',
                            side_effect=AssertionError('Synchronous export scan')):
                    await web._handle_parallel_benchmark_export_request(writer, 'GET', b'', False, {})
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            header, raw = bytes(writer.buf).split(b'\r\n\r\n', 1)
            self.assertIn(b'200 OK', header)
            self.assertEqual(verify_export(raw, 65)['points'], 65)
            self.assertGreater(len(turns), 1)
