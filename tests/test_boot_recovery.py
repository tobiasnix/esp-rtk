# SPDX-License-Identifier: AGPL-3.0-only
"""Cooperative boot recovery preserves data and rejects partial-state writes."""
import asyncio
import json
import copy
import os
import shutil
import tempfile
import unittest
from unittest import mock

import support
from support import state, web
import tracking
import pointstore
from pointstore import FlashPointStore
from test_tracking import FIX
from test_http import FakeWriter


class BootRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_buffered_selection_handles_long_utf8_rows_and_final_tail(self):
        with tempfile.TemporaryDirectory() as folder:
            store = FlashPointStore(os.path.join(folder, 'points'))
            for order in range(1, 81):
                note = ('\u00e4\u20ac' * 3000) if order == 31 else 'sample-' + str(order)
                store.append(order, 'a', [800000000, 5000000000, order * 1000],
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
                first.add_vertex(dict(FIX, lon=8 + index * .00002, lon_e8=800000000 + index * 2000),
                                 'automatic')
            await first.compact_async()
            for index in range(8, 82):
                first.add_vertex(dict(FIX, lon=8 + index * .00002, lon_e8=800000000 + index * 2000),
                                 'automatic')
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
            # The project tally is built from the summaries, without flash reads.
            self.assertEqual(recovered._project_frames,
                             {recovered.projects[0]['id']: {'unknown': 81}})
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
                store.append(order, 'a' if order % 2 else 'b', [800000000, 5000000000, order * 1000],
                             {'record_order': order, 'source': 'automatic'})
            store.remove_last(21, 'a')
            store.remove_last(22, 'a')
            store.append(23, 'a', [800000000, 5000000000, 23000], {'record_order': 23})
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
            self.assertEqual(store._decode(legacy, False)['coordinate'], [800000000, 5000000000, 1000])
            store.append(1, 'a', [800000000, 5000000000, 1000], {'source': 'manual'})
            store.close()
            for value in ([9, 1, 'a'], [3, 1, 'a'], [2, 1, 'a'],
                          [3, 1, 'other', [800000000, 5000000000, 1000], [-1]],
                          [2, 1, 'other', [8., 50., 1], [-1]]):
                with mock.patch('pointstore.ujson.loads', return_value=value):
                    with self.assertRaises(ValueError):
                        list(store.iter_line('a', include_measurement=False))


def _legacy_measurement(order, recorded_at=None, source="automatic"):
    """A measurement dict as schema 4 and older firmware stored it (no precision)."""
    return {"recorded_at": order if recorded_at is None else recorded_at,
            "fix_quality": 4, "fix_status": "RTK_FIXED", "satellites": 24,
            "hdop": 0.6, "correction_age_sec": 0.5, "station_id": "0054",
            "source": source, "quality": {"accepted": True, "reasons": []},
            "record_order": order}


def _legacy_line(line_id="line-a", state="recording", **extra):
    line = {"id": line_id, "name": "Legacy line", "state": state, "created_at": 1,
            "finished_at": None, "properties": {}, "recording_mode": "survey"}
    line.update(extra)
    return line


def _write_lines(path, records):
    with open(path, "w", encoding="utf-8") as target:
        for record in records:
            target.write(json.dumps(record) + "\n")


def _write_marker_two_store(tracker_path, records, generation=7, schema=3):
    """Write a point store as schema-3 firmware left it: marker-2 float records."""
    point_path = tracker_path + ".points-v1.jsonl"
    segment = "%s.g%08x-%04d.pjs" % (point_path, generation, 1)
    with open(segment, "w", encoding="utf-8") as target:
        for order, line_id, coordinate, measurement in records:
            target.write(json.dumps([2, order, line_id, coordinate,
                                     pointstore._compact(measurement)]) + "\n")
    with open(point_path + ".segments.json", "w", encoding="utf-8") as target:
        json.dump({"kind": "point_store", "schema": schema, "generation": generation,
                   "segments": [1], "strings": []}, target)


def _close(tracker):
    tracker._sync_journal(close=True)
    tracker.point_store.close()


class _LegacyStoreCase(unittest.TestCase):
    """Fixtures for stores written by schema <= 4 firmware."""

    def setUp(self):
        self.capacity = mock.patch.object(tracking.Tracker, "storage_capacity",
                                          return_value={"writable": True})
        self.capacity.start()
        self.tmp = tempfile.TemporaryDirectory(prefix="esp-rtk-upgrade-")
        self.path = os.path.join(self.tmp.name, "survey")

    def tearDown(self):
        self.tmp.cleanup()
        self.capacity.stop()

    def segment(self, number):
        return "%s.segment-%06d.jsonl" % (self.path, number)

    def write_schema4_store(self, summary=None, point_count=1, store_records=None):
        """Schema-4 checkpoint stream + marker-2 point store + schema-4 journal tail."""
        line_meta = _legacy_line(point_count=point_count)
        if summary is not None:
            line_meta["_summary"] = summary
        _write_lines(self.path + ".checkpoint.json", [
            {"kind": "survey_checkpoint_stream", "schema_version": 4, "record": "header",
             "order": 3, "segment": 2, "offset": 0, "profiles": []},
            {"record": "project", "project": {"id": "p", "name": "Legacy",
                                              "description": "", "created_at": 1}},
            {"record": "line", "project_id": "p", "line": line_meta},
            {"record": "footer", "active_project_id": "p", "active_line_id": "line-a",
             "auto_distance_m": 1.0}])
        _write_marker_two_store(self.path, store_records or [
            (3, "line-a", [8.0, 50.0, 123.4], _legacy_measurement(3, source="start"))])

    def write_schema4_journal_tail(self):
        _write_lines(self.segment(2), [
            {"event": "vertex_added", "project_id": "p", "line_id": "line-a",
             "coordinate": [8.00001, 50.0, 123.4], "measurement": _legacy_measurement(4),
             "order": 4, "schema_version": 4},
            {"event": "asset_added", "project_id": "p", "point": {
                "id": "point-1", "object_type": "valve", "name": "V-1", "note": "",
                "line_id": "line-a", "chainage_m": 0.0, "lateral_offset_m": 0.0,
                "side": "on_line", "coordinate": [8.0, 50.0, 100.0],
                "projected_coordinate": [8.0, 50.0, 100.0],
                "measurement": _legacy_measurement(5, source="asset")},
             "order": 5, "schema_version": 4}])



class LegacyUpgradeTests(_LegacyStoreCase):
    """An in-place upgrade loads schema <= 4 journals and checkpoints as exact integers."""

    def test_schema4_journal_and_checkpoint_load_as_tagged_integers(self):
        self.write_schema4_store()
        self.write_schema4_journal_tail()
        recovered = tracking.Tracker(self.path)
        line = recovered.projects[0]["lines"][0]
        self.assertEqual(list(line["coordinates"]), [[800000000, 5000000000, 123400], [800001000, 5000000000, 123400]])
        self.assertEqual([m["precision"] for m in line["measurements"]], ["legacy_float32"] * 2)
        self.assertTrue(all(type(v) is int for c in line["coordinates"] for v in c))
        point = recovered.projects[0]["points"][0]
        self.assertEqual(point["coordinate"], [800000000, 5000000000, 100000])
        self.assertEqual(point["projected_coordinate"], [800000000, 5000000000, 100000])
        self.assertEqual(point["measurement"]["precision"], "legacy_float32")
        # Schema-4 points never get a frame invented: all of them count as unknown.
        project = recovered.status()["projects"][0]
        self.assertEqual((project["frames"], project["mixed_frames"], project["unknown_frames"]),
                         ({"unknown": 3}, False, True))
        backup = recovered.backup()
        self.assertEqual(backup["schema_version"], 5)
        # The converted state is a valid schema-5 backup.
        tracking.validate_backup(copy.deepcopy(backup))
        # The replayed vertex went into the point store as an exact marker-3 record.
        with open(recovered.point_store._segment_path(recovered.point_store.segments[-1]),
                  encoding="utf-8") as source:
            rows = [json.loads(raw) for raw in source if raw.strip()]
        self.assertEqual([row[0] for row in rows], [2, 3])
        self.assertEqual(rows[1][3], [800001000, 5000000000, 123400])
        # A second and a third cold reload see the same data: nothing converts twice.
        _close(recovered)
        for _ in range(2):
            again = tracking.Tracker(self.path)
            self.assertEqual(again.backup(), backup)
            _close(again)

    def test_new_events_after_an_upgrade_mix_with_legacy_vertices(self):
        self.write_schema4_store()
        self.write_schema4_journal_tail()
        tracker = tracking.Tracker(self.path)
        tracker.add_vertex(dict(FIX, lon_e8=800002000))
        _close(tracker)
        reloaded = tracking.Tracker(self.path)
        line = reloaded.projects[0]["lines"][0]
        self.assertEqual(list(line["coordinates"]), [
            [800000000, 5000000000, 123400], [800001000, 5000000000, 123400],
            [800002000, 5000000000, 123400]])
        self.assertEqual([m["precision"] for m in line["measurements"]],
                         ["legacy_float32", "legacy_float32", "e8"])
        summary = reloaded._line_summary(line)
        self.assertGreater(summary["length_m"], 0)
        self.assertAlmostEqual(summary["length_m"], reloaded._line_length(line), places=3)
        self.assertGreater(reloaded._line_lengths["line-a"], 0)
        _close(reloaded)

    def test_compaction_keeps_legacy_records_readable(self):
        self.write_schema4_store()
        self.write_schema4_journal_tail()
        tracker = tracking.Tracker(self.path)
        tracker.add_vertex(dict(FIX, lon_e8=800002000))
        before = copy.deepcopy(tracker.backup())
        tracker.compact()
        with open(tracker.checkpoint_path, encoding="utf-8") as source:
            self.assertEqual(json.loads(source.readline())["schema_version"], 5)
        _close(tracker)
        reloaded = tracking.Tracker(self.path)
        line = reloaded.projects[0]["lines"][0]
        self.assertEqual(list(line["coordinates"]),
                         before["projects"][0]["lines"][0]["coordinates"])
        self.assertEqual([m["precision"] for m in line["measurements"]],
                         ["legacy_float32", "legacy_float32", "e8"])
        self.assertEqual(reloaded.projects[0]["points"][0]["measurement"]["precision"],
                         "legacy_float32")
        self.assertEqual(reloaded.backup()["projects"][0]["points"],
                         before["projects"][0]["points"])
        _close(reloaded)

    def test_monolithic_schema3_checkpoint_loads_converted(self):
        project = {"id": "p", "name": "Legacy", "description": "", "created_at": 1,
                   "lines": [_legacy_line(
                       state="finished", finished_at=9,
                       coordinates=[[8.0, 50.0, 100.0], [8.00002, 50.0, 100.25]],
                       measurements=[_legacy_measurement(2), _legacy_measurement(3)],
                       _summary={"vertex_count": 2, "length_m": 999.0,
                                 "quality_accepted_count": 2, "large_gap_count": 0,
                                 "max_gap_m": 0.0, "large_vertical_step_count": 0,
                                 "max_vertical_step_m": 0.0, "overlapping": False})],
                   "points": [{"id": "point-1", "object_type": "valve", "name": "V-1",
                               "note": "", "line_id": None, "chainage_m": None,
                               "lateral_offset_m": None, "side": None,
                               "projected_coordinate": None,
                               "coordinate": [-0.654321, 40.0, -2.5],
                               "measurement": _legacy_measurement(4, source="asset")}]}
        with open(self.path + ".checkpoint.json", "w", encoding="utf-8") as target:
            json.dump({"kind": "survey_checkpoint", "schema_version": 3, "order": 4,
                       "segment": 0, "offset": 0, "profiles": [], "projects": [project],
                       "active_project_id": "p", "active_line_id": None,
                       "auto_distance_m": 1.0}, target)
        recovered = tracking.Tracker(self.path)
        line = recovered.projects[0]["lines"][0]
        self.assertEqual(list(line["coordinates"]), [[800000000, 5000000000, 100000],
                                                     [800002000, 5000000000, 100250]])
        self.assertEqual([m["precision"] for m in line["measurements"]], ["legacy_float32"] * 2)
        point = recovered.projects[0]["points"][0]
        self.assertEqual(point["coordinate"], [-65432100, 4000000000, -2500])
        self.assertIsNone(point["projected_coordinate"])
        self.assertEqual(point["measurement"]["precision"], "legacy_float32")
        # The stale cached length of the former distance model is rebuilt.
        self.assertNotEqual(line["_summary"]["length_m"], 999.0)
        self.assertAlmostEqual(line["_summary"]["length_m"], recovered._line_length(line), places=6)
        backup = recovered.backup()
        self.assertEqual(backup["schema_version"], 5)
        tracking.validate_backup(copy.deepcopy(backup))
        _close(recovered)
        again = tracking.Tracker(self.path)
        self.assertEqual(again.backup(), backup)
        _close(again)

    def test_schema4_snapshot_event_is_converted(self):
        project = {"id": "p", "name": "Snapshot", "description": "", "created_at": 1,
                   "lines": [_legacy_line(
                       coordinates=[[10.0, 51.0, 200.0], [10.00001, 51.0, 200.5]],
                       measurements=[_legacy_measurement(1), _legacy_measurement(2)],
                       _summary={"vertex_count": 2, "length_m": 999.0})],
                   "points": [{"id": "point-1", "object_type": "valve", "name": "V-1",
                               "note": "", "line_id": "line-a", "chainage_m": 0.0,
                               "lateral_offset_m": 0.0, "side": "on_line",
                               "projected_coordinate": [10.0, 51.0, 200.0],
                               "coordinate": [10.0, 51.0, 200.0],
                               "measurement": _legacy_measurement(3, source="asset")}]}
        _write_lines(self.segment(1), [
            {"event": "snapshot", "order": 3, "schema_version": 4, "projects": [project],
             "active_project_id": "p", "active_line_id": "line-a", "auto_distance_m": 1.0}])
        recovered = tracking.Tracker(self.path)
        line = recovered.projects[0]["lines"][0]
        self.assertEqual(list(line["coordinates"]), [[1000000000, 5100000000, 200000],
                                                     [1000001000, 5100000000, 200500]])
        self.assertEqual([m["precision"] for m in line["measurements"]], ["legacy_float32"] * 2)
        point = recovered.projects[0]["points"][0]
        self.assertEqual(point["coordinate"], [1000000000, 5100000000, 200000])
        self.assertEqual(point["projected_coordinate"], [1000000000, 5100000000, 200000])
        self.assertEqual(point["measurement"]["precision"], "legacy_float32")
        self.assertAlmostEqual(line["_summary"]["length_m"], recovered._line_length(line), places=6)
        backup = recovered.backup()
        _close(recovered)
        again = tracking.Tracker(self.path)
        self.assertEqual(again.backup(), backup)
        _close(again)

    def test_schema4_checkpoint_summary_length_is_recomputed_on_first_load(self):
        stale = {"vertex_count": 2, "length_m": 999.0, "quality_accepted_count": 2,
                 "large_gap_count": 1, "max_gap_m": 999.0, "large_vertical_step_count": 0,
                 "max_vertical_step_m": 0.0, "overlapping": False}
        self.write_schema4_store(summary=stale, point_count=2, store_records=[
            (2, "line-a", [8.0, 50.0, 123.4], _legacy_measurement(2, source="start")),
            (3, "line-a", [8.00001, 50.0, 123.4], _legacy_measurement(3))])
        recovered = tracking.Tracker(self.path)
        line = recovered.projects[0]["lines"][0]
        self.assertEqual(len(line["coordinates"]), 2)
        expected = tracking._distance_m(line["coordinates"][0], line["coordinates"][1])
        self.assertGreater(expected, 0.5)
        self.assertLess(expected, 1.0)
        self.assertAlmostEqual(line["_summary"]["length_m"], expected, places=6)
        self.assertEqual(line["_summary"]["large_gap_count"], 0)
        self.assertAlmostEqual(recovered._line_lengths["line-a"], expected, places=6)
        _close(recovered)

    def test_schema5_checkpoint_summary_is_kept(self):
        # Control: a current checkpoint is never converted and its cache stays.
        tracker = tracking.Tracker(self.path)
        tracker.create_project("Current")
        tracker.start_line("Route", 1, fix=FIX)
        tracker.add_vertex(dict(FIX, lon_e8=800002000))
        tracker._line_summary(tracker.projects[0]["lines"][0])
        tracker.compact()
        _close(tracker)
        with mock.patch.object(tracking.geo, "legacy_coordinate_to_e8",
                               side_effect=AssertionError("converted twice")), \
                mock.patch.object(tracking.Tracker, "_iter_line_summary",
                                  side_effect=AssertionError("summary rebuilt")):
            reloaded = tracking.Tracker(self.path)
        self.assertEqual(reloaded.projects[0]["lines"][0]["_summary"]["vertex_count"], 2)
        # A normal boot does not ask for a new checkpoint.
        self.assertLess(reloaded._checkpoint_dirty_events, tracking.CHECKPOINT_BATCH_EVENTS)
        _close(reloaded)

    def test_schema5_journal_vertices_after_the_checkpoint_do_not_mark_it_outdated(self):
        tracker = tracking.Tracker(self.path)
        tracker.create_project("Journal")
        tracker.start_line("Route", 1, fix=FIX)
        tracker.add_vertex(dict(FIX, lon_e8=800002000))
        tracker._line_summary(tracker.projects[0]["lines"][0])
        tracker.compact()
        tracker.add_vertex(dict(FIX, lon_e8=800004000))          # only in the journal
        _close(tracker)
        reloaded = tracking.Tracker(self.path)
        self.assertEqual(len(reloaded.projects[0]["lines"][0]["coordinates"]), 3)
        self.assertLess(reloaded._checkpoint_dirty_events, tracking.CHECKPOINT_BATCH_EVENTS)
        _close(reloaded)

    def test_schema5_checkpoint_summary_without_frames_is_rebuilt_on_load(self):
        tracker = tracking.Tracker(self.path)
        tracker.create_project("Before frames")
        with mock.patch.dict(tracking.CONFIG, {"reference_frame": "ETRS89 (synthetic)"}):
            tracker.start_line("Route", 1, fix=FIX)
            tracker.add_vertex(dict(FIX, lon_e8=800002000))
        with mock.patch.dict(tracking.CONFIG, {"measurement_required_fix": "DGPS"}):
            tracker.add_asset("tap", "T", "", dict(FIX, qual=2, fix_status_text="DGPS"),
                              link_to_active_line=False)
        tracker.compact()
        _close(tracker)
        with open(self.path + ".checkpoint.json", encoding="utf-8") as source:
            records = [json.loads(raw) for raw in source if raw.strip()]
        lines = [record for record in records if record.get("record") == "line"]
        self.assertEqual(lines[0]["line"]["_summary"]["frames"], {"ETRS89 (synthetic)": 2})
        for record in lines:
            record["line"]["_summary"].pop("frames")
        _write_lines(self.path + ".checkpoint.json", records)
        reloaded = tracking.Tracker(self.path)
        # The outdated checkpoint is rewritten on the first idle turn, not rebuilt every boot.
        self.assertGreaterEqual(reloaded._checkpoint_dirty_events, tracking.CHECKPOINT_BATCH_EVENTS)
        line = reloaded.projects[0]["lines"][0]
        self.assertEqual(line["_summary"]["frames"], {"ETRS89 (synthetic)": 2})
        project_id = reloaded.projects[0]["id"]
        self.assertEqual(reloaded._project_frames,
                         {project_id: {"ETRS89 (synthetic)": 2, "WGS84/ITRF": 1}})
        self.assertTrue(reloaded.status()["projects"][0]["mixed_frames"])
        _close(reloaded)

    def test_schema4_stream_checkpoint_runs_the_point_store_deficit_check(self):
        # An undo after the checkpoint explains the deficit through a legacy tombstone.
        self.write_schema4_store(point_count=2, store_records=[
            (2, "line-a", [8.0, 50.0, 123.4], _legacy_measurement(2, source="start")),
            (3, "line-a", [8.00001, 50.0, 123.4], _legacy_measurement(3))])
        segment = "%s.points-v1.jsonl.g%08x-%04d.pjs" % (self.path, 7, 1)
        with open(segment, "a", encoding="utf-8") as target:
            target.write('[2,4,"line-a"]\n')
        recovered = tracking.Tracker(self.path)
        self.assertEqual(list(recovered.projects[0]["lines"][0]["coordinates"]),
                         [[800000000, 5000000000, 123400]])
        _close(recovered)
        # A point store that is behind a schema-4 checkpoint is checked like a
        # schema-5 one: the deficit goes to the point-store history check.
        shutil.rmtree(self.tmp.name); os.makedirs(self.tmp.name)
        self.write_schema4_store(point_count=2)
        with mock.patch.object(pointstore.FlashPointStore, "checkpoint_counts_match",
                               return_value=False) as check:
            behind = tracking.Tracker(self.path)
        check.assert_called_once_with({"line-a": 2}, 3)
        self.assertEqual(behind._checkpoint_order, 0)
        _close(behind)

    def test_schema3_stream_checkpoint_vertex_records_load_converted(self):
        measurement = _legacy_measurement(2, source="start")
        _write_lines(self.path + ".checkpoint.json", [
            {"kind": "survey_checkpoint_stream", "schema_version": 3, "record": "header",
             "order": 4, "segment": 0, "offset": 0, "profiles": []},
            {"record": "project", "project": {"id": "p", "name": "Legacy",
                                              "description": "", "created_at": 1}},
            {"record": "line", "project_id": "p", "line": _legacy_line(
                state="finished", _summary={"vertex_count": 2, "length_m": 999.0})},
            {"record": "vertex", "line_id": "line-a", "coordinate": [8.0, 50.0, 123.4],
             "measurement": measurement},
            {"record": "vertex", "line_id": "line-a", "coordinate": [8.00001, 50.0, 123.4],
             "measurement": _legacy_measurement(3)},
            {"record": "asset", "project_id": "p", "point": {
                "id": "point-1", "object_type": "valve", "name": "V-1", "note": "",
                "line_id": "line-a", "chainage_m": 0.0, "lateral_offset_m": 0.0,
                "side": "on_line", "coordinate": [8.0, 50.0, 100.0],
                "projected_coordinate": [8.0, 50.0, 123.4],
                "measurement": _legacy_measurement(4, source="asset")}},
            {"record": "footer", "active_project_id": "p", "active_line_id": None,
             "auto_distance_m": 1.0}])
        recovered = tracking.Tracker(self.path)
        line = recovered.projects[0]["lines"][0]
        self.assertEqual(list(line["coordinates"]), [[800000000, 5000000000, 123400],
                                                     [800001000, 5000000000, 123400]])
        self.assertEqual([m["precision"] for m in line["measurements"]], ["legacy_float32"] * 2)
        point = recovered.projects[0]["points"][0]
        self.assertEqual(point["coordinate"], [800000000, 5000000000, 100000])
        self.assertEqual(point["projected_coordinate"], [800000000, 5000000000, 123400])
        self.assertEqual(point["measurement"]["precision"], "legacy_float32")
        self.assertLess(line["_summary"]["length_m"], 1.0)
        backup = recovered.backup()
        tracking.validate_backup(copy.deepcopy(backup))
        _close(recovered)
        again = tracking.Tracker(self.path)
        self.assertEqual(again.backup(), backup)
        _close(again)

    def test_cooperative_boot_converts_the_same_way(self):
        self.write_schema4_store()
        self.write_schema4_journal_tail()
        expected = tracking.Tracker(self.path)
        backup = expected.backup()
        _close(expected)
        recovered = tracking.Tracker(self.path, autoload=False, point_store_autoload=False)
        asyncio.run(recovered.initialize_async())
        self.assertEqual(recovered.backup(), backup)
        self.assertEqual(backup["projects"][0]["points"][0]["coordinate"],
                         [800000000, 5000000000, 100000])
        _close(recovered)

    def test_stream_checkpoint_of_an_unknown_schema_is_not_loaded(self):
        self.write_schema4_store()
        with open(self.path + ".checkpoint.json", encoding="utf-8") as source:
            rows = [json.loads(raw) for raw in source]
        rows[0]["schema_version"] = 6
        _write_lines(self.path + ".checkpoint.json", rows)
        recovered = tracking.Tracker(self.path)
        self.assertEqual(recovered.projects, [])
        self.assertEqual(recovered._checkpoint_order, 0)
        _close(recovered)


class LegacyValuePolicyTests(_LegacyStoreCase):
    """One invalid legacy value costs that value, never the checkpoint or the event."""

    def setUp(self):
        super().setUp()
        state.app.stats["legacy_conversion_errors"] = 0
        self.log = support.patch_module_global("tracking", "log").start()
        self.addCleanup(mock.patch.stopall)

    def errors(self):
        return [c.args for c in self.log.call_args_list if c.args[0] == "ERROR"]

    @staticmethod
    def asset(point_id, line_id, coordinate, projected, order, chainage=0.0):
        return {"id": point_id, "object_type": "valve", "name": point_id, "note": "",
                "line_id": line_id, "chainage_m": chainage,
                "lateral_offset_m": 0.5 if line_id else None,
                "side": "left" if line_id else None, "coordinate": coordinate,
                "projected_coordinate": projected,
                "measurement": _legacy_measurement(order, source="asset")}

    def write_reviewer_store(self, far_coordinate=(8.0, 50.012, 100.0)):
        """Two projects as V15.8.1 compacted them, one asset extrapolated to 120100 m."""
        rows = [{"kind": "survey_checkpoint_stream", "schema_version": 4, "record": "header",
                 "order": 12, "segment": 2, "offset": 0, "profiles": []}]
        rows.append({"record": "project", "project": {"id": "p1", "name": "Bad",
                                                      "description": "", "created_at": 1}})
        rows.append({"record": "line", "project_id": "p1", "line": _legacy_line(
            "l1", state="finished", finished_at=6, point_count=2,
            _summary={"vertex_count": 2, "length_m": 0.445})})
        rows.append({"record": "asset", "project_id": "p1", "point": self.asset(
            "V-far", "l1", list(far_coordinate), [8.0, 50.000004, 120100.0], 5, 1334.4)})
        rows.append({"record": "asset", "project_id": "p1", "point": self.asset(
            "V-near", "l1", [8.0, 50.000002, 120.0], [8.0, 50.000002, 120.0], 6, 0.2)})
        rows.append({"record": "project", "project": {"id": "p2", "name": "Second",
                                                      "description": "", "created_at": 7}})
        rows.append({"record": "line", "project_id": "p2", "line": _legacy_line(
            "l2", state="finished", finished_at=11, point_count=2)})
        rows.append({"record": "asset", "project_id": "p2", "point": self.asset(
            "V-free", None, [10.0, 51.0, 5.0], None, 12)})
        rows.append({"record": "footer", "active_project_id": "p2", "active_line_id": None,
                     "auto_distance_m": 1.0})
        _write_lines(self.path + ".checkpoint.json", rows)
        _write_marker_two_store(self.path, [
            (3, "l1", [8.0, 50.0, 100.0], _legacy_measurement(3, source="start")),
            (4, "l1", [8.0, 50.000004, 140.0], _legacy_measurement(4)),
            (9, "l2", [10.0, 51.0, 5.0], _legacy_measurement(9, source="start")),
            (10, "l2", [10.00002, 51.0, 5.1], _legacy_measurement(10))])

    def summary(self, tracker):
        return [(project["name"], [(line["id"], len(line["coordinates"]))
                                   for line in project["lines"]],
                 [point["id"] for point in project["points"]])
                for project in tracker.projects]

    def test_extrapolated_projection_keeps_every_record(self):
        self.write_reviewer_store()
        recovered = tracking.Tracker(self.path)
        self.assertEqual(self.summary(recovered), [
            ("Bad", [("l1", 2)], ["V-far", "V-near"]), ("Second", [("l2", 2)], ["V-free"])])
        far = recovered.projects[0]["points"][0]
        self.assertIsNone(far["projected_coordinate"])
        self.assertEqual(far["coordinate"], [800000000, 5001200000, 100000])
        self.assertEqual((far["chainage_m"], far["lateral_offset_m"], far["side"]),
                         (1334.4, 0.5, "left"))
        self.assertEqual(recovered.projects[0]["points"][1]["projected_coordinate"],
                         [800000000, 5000000200, 120000])
        self.assertEqual(state.app.stats["legacy_conversion_errors"], 1)
        errors = self.errors()
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0][1], "TRACK")
        self.assertIn("point V-far", errors[0][2])
        self.assertIn("projected_coordinate", errors[0][2])
        self.assertNotIn("120100", errors[0][2])
        backup = recovered.backup()
        tracking.validate_backup(copy.deepcopy(backup))
        recovered.compact()
        _close(recovered)
        again = tracking.Tracker(self.path)
        self.assertEqual(again.backup(), backup)
        _close(again)
        # The schema-5 checkpoint carries None; nothing is reported again.
        self.assertEqual(state.app.stats["legacy_conversion_errors"], 1)

    def test_asset_with_an_invalid_coordinate_is_the_only_record_left_out(self):
        self.write_reviewer_store(far_coordinate=(8.0, 95.0, 100.0))
        recovered = tracking.Tracker(self.path)
        self.assertEqual(self.summary(recovered), [
            ("Bad", [("l1", 2)], ["V-near"]), ("Second", [("l2", 2)], ["V-free"])])
        self.assertEqual(state.app.stats["legacy_conversion_errors"], 1)
        errors = self.errors()
        self.assertEqual(len(errors), 1)
        self.assertIn("point V-far", errors[0][2])
        self.assertIn("coordinate", errors[0][2])
        self.assertNotIn("95", errors[0][2])
        _close(recovered)

    def test_invalid_embedded_vertex_is_skipped_with_its_measurement(self):
        project = {"id": "p", "name": "Legacy", "description": "", "created_at": 1,
                   "lines": [_legacy_line(
                       state="finished", finished_at=9,
                       coordinates=[[8.0, 50.0, 100.0], [8.00001, 50.0, 100000.5],
                                    [8.00002, 50.0, 100.25]],
                       measurements=[_legacy_measurement(2), _legacy_measurement(3),
                                     _legacy_measurement(4)])],
                   "points": [self.asset("V-1", None, [8.0, 50.0, 100.0], None, 5)]}
        with open(self.path + ".checkpoint.json", "w", encoding="utf-8") as target:
            json.dump({"kind": "survey_checkpoint", "schema_version": 3, "order": 5,
                       "segment": 0, "offset": 0, "profiles": [], "projects": [project],
                       "active_project_id": "p", "active_line_id": None,
                       "auto_distance_m": 1.0}, target)
        recovered = tracking.Tracker(self.path)
        line = recovered.projects[0]["lines"][0]
        self.assertEqual(list(line["coordinates"]), [[800000000, 5000000000, 100000],
                                                     [800002000, 5000000000, 100250]])
        self.assertEqual([m["record_order"] for m in line["measurements"]], [2, 4])
        self.assertEqual([m["recorded_at"] for m in line["measurements"]], [2, 4])
        self.assertEqual([p["id"] for p in recovered.projects[0]["points"]], ["V-1"])
        self.assertEqual(state.app.stats["legacy_conversion_errors"], 1)
        errors = self.errors()
        self.assertEqual(len(errors), 1)
        self.assertIn("line line-a vertex 3", errors[0][2])
        _close(recovered)

    def test_invalid_stream_vertex_record_is_skipped(self):
        _write_lines(self.path + ".checkpoint.json", [
            {"kind": "survey_checkpoint_stream", "schema_version": 3, "record": "header",
             "order": 4, "segment": 0, "offset": 0, "profiles": []},
            {"record": "project", "project": {"id": "p", "name": "Legacy",
                                              "description": "", "created_at": 1}},
            {"record": "line", "project_id": "p", "line": _legacy_line(state="finished")},
            {"record": "vertex", "line_id": "line-a", "coordinate": [8.0, 50.0, 123.4],
             "measurement": _legacy_measurement(2, source="start")},
            {"record": "vertex", "line_id": "line-a", "coordinate": [181.0, 50.0, 123.4],
             "measurement": _legacy_measurement(3)},
            {"record": "vertex", "line_id": "line-a", "coordinate": [8.00002, 50.0, 123.4],
             "measurement": _legacy_measurement(4)},
            {"record": "footer", "active_project_id": "p", "active_line_id": None,
             "auto_distance_m": 1.0}])
        recovered = tracking.Tracker(self.path)
        line = recovered.projects[0]["lines"][0]
        self.assertEqual(list(line["coordinates"]), [[800000000, 5000000000, 123400],
                                                     [800002000, 5000000000, 123400]])
        self.assertEqual([m["record_order"] for m in line["measurements"]], [2, 4])
        self.assertEqual(state.app.stats["legacy_conversion_errors"], 1)
        self.assertIn("line line-a vertex 3", self.errors()[0][2])
        _close(recovered)

    def test_unconvertible_point_store_vertex_costs_only_that_vertex(self):
        # Codex #1: one bad marker-2 vertex must not truncate the segment and
        # take the valid vertices after it.
        records = [(3, "line-a", [8.0, 50.0, 123.4], _legacy_measurement(3, source="start")),
                   (4, "line-a", [8.00001, 50.0, 120100.0], _legacy_measurement(4)),
                   (5, "line-a", [8.00002, 50.0, 123.4], _legacy_measurement(5))]
        self.write_schema4_store(point_count=3, store_records=records)
        rows = []
        with open(self.path + ".checkpoint.json", encoding="utf-8") as source:
            rows = [json.loads(raw) for raw in source]
        rows[0]["order"] = 5
        _write_lines(self.path + ".checkpoint.json", rows)
        segment = "%s.points-v1.jsonl.g%08x-%04d.pjs" % (self.path, 7, 1)
        size = os.path.getsize(segment)
        recovered = tracking.Tracker(self.path)
        self.assertEqual(os.path.getsize(segment), size)
        self.assertEqual(recovered._checkpoint_order, 5)
        self.assertEqual(list(recovered.projects[0]["lines"][0]["coordinates"]),
                         [[800000000, 5000000000, 123400], [800002000, 5000000000, 123400]])
        self.assertEqual(state.app.stats["legacy_conversion_errors"], 1)
        errors = self.errors()
        self.assertEqual(len(errors), 1)
        self.assertIn("line line-a vertex 4", errors[0][2])
        self.assertNotIn("120100", errors[0][2])
        # A new vertex follows the skipped order, and compaction verifies.
        recovered.add_vertex(dict(FIX, lon_e8=800003000))
        asyncio.run(recovered.compact_async())
        with open(recovered.checkpoint_path, encoding="utf-8") as source:
            self.assertEqual(json.loads(source.readline())["schema_version"], 5)
        before = recovered.backup()
        _close(recovered)
        again = tracking.Tracker(self.path)
        self.assertEqual(again.backup(), before)
        self.assertEqual(len(again.projects[0]["lines"][0]["coordinates"]), 3)
        _close(again)

    def test_schema5_checkpoint_relabelled_by_older_firmware_keeps_everything(self):
        # W5: V15.8.1 accepts a schema-5 stream checkpoint and re-stamps it as
        # schema 4. The measurements keep their precision tag, so the next
        # upgrade must recognise the integers instead of dropping every asset.
        relabel = support.patch_module_global("tracking", "_relabel_warned", False)
        relabel.start()
        self.addCleanup(relabel.stop)
        tracker = tracking.Tracker(self.path)
        tracker.create_project("Exact")
        tracker.start_line("L", 1, fix=FIX)
        tracker.add_vertex(dict(FIX, lon_e8=800002000))
        tracker.add_asset("valve", "V-mid", "", dict(FIX, lon_e8=800001000, lat_e8=5000000300))
        tracker.add_asset("tap", "Free", "", dict(FIX, lat_e8=5000100000),
                          link_to_active_line=False)
        tracker.compact()
        before = tracker.backup()
        _close(tracker)
        checkpoint = self.path + ".checkpoint.json"
        with open(checkpoint, encoding="utf-8") as source:
            rows = [json.loads(raw) for raw in source]
        self.assertEqual(rows[0]["schema_version"], 5)
        rows[0]["schema_version"] = 4
        _write_lines(checkpoint, rows)
        recovered = tracking.Tracker(self.path)
        self.assertEqual(state.app.stats["legacy_conversion_errors"], 0)
        self.assertEqual(self.errors(), [])
        self.assertEqual(recovered.backup()["projects"], before["projects"])
        warnings = [c.args[2] for c in self.log.call_args_list if c.args[0] == "WARN"]
        self.assertTrue(any("older firmware" in text for text in warnings), warnings)
        _close(recovered)

    def test_relabelled_integers_in_backups_and_journals_are_kept(self):
        tracker = tracking.Tracker(self.path)
        tracker.create_project("Exact")
        tracker.start_line("L", 1, fix=FIX)
        tracker.add_asset("valve", "V-mid", "", dict(FIX, lon_e8=800001000))
        point = copy.deepcopy(tracker.projects[0]["points"][0])
        vertex = list(tracker.projects[0]["lines"][0]["coordinates"][0])
        measurement = dict(tracker.projects[0]["lines"][0]["measurements"][0])
        _close(tracker)
        upgraded = tracking._upgrade_event({"event": "asset_added", "schema_version": 4,
                                            "project_id": "p", "point": copy.deepcopy(point)})
        self.assertEqual(upgraded["point"]["coordinate"], point["coordinate"])
        self.assertEqual(upgraded["point"]["projected_coordinate"],
                         point["projected_coordinate"])
        upgraded = tracking._upgrade_event({"event": "vertex_added", "schema_version": 4,
                                            "line_id": "l", "order": 9,
                                            "coordinate": list(vertex),
                                            "measurement": dict(measurement)})
        self.assertEqual(upgraded["coordinate"], vertex)
        self.assertEqual(upgraded["measurement"]["precision"], "e8")
        # Floats under a precision tag still go through the degree rule, and
        # untagged integers are legacy degrees as before.
        self.assertEqual(tracking._legacy_coordinate([8.0, 50.0, 1.0], measurement=measurement),
                         [800000000, 5000000000, 1000])
        self.assertIsNone(tracking._legacy_coordinate(vertex, measurement={}))
        self.assertEqual(state.app.stats["legacy_conversion_errors"], 0)

    def test_upgrade_is_persisted_once_by_the_first_idle_checkpoint(self):
        # W14/W7: without a schema-5 checkpoint every boot re-converted the
        # legacy data, rebuilt every line summary and re-logged its errors.
        self.write_reviewer_store()
        checkpoint = self.path + ".checkpoint.json"
        first = tracking.Tracker(self.path)
        self.assertEqual(state.app.stats["legacy_conversion_errors"], 1)
        self.assertGreaterEqual(first._checkpoint_dirty_events,
                                tracking.CHECKPOINT_BATCH_EVENTS)
        stop = asyncio.Event()
        real_compact = first.compact_async

        async def compact_then_stop():
            await real_compact()
            stop.set()

        async def drive():
            with mock.patch.object(state, "shutdown_event", stop), \
                    mock.patch.object(first, "compact_async", side_effect=compact_then_stop):
                await asyncio.wait_for(first.run_worker(), 5)

        asyncio.run(drive())
        self.assertEqual(first._checkpoint_dirty_events, 0)
        with open(checkpoint, encoding="utf-8") as source:
            self.assertEqual(json.loads(source.readline())["schema_version"], 5)
        before = first.backup()
        _close(first)
        os.remove(checkpoint + ".v4")
        with open(checkpoint + ".v4.tmp", "wb") as target:
            target.write(b"stale partial copy")
        state.app.stats["legacy_conversion_errors"] = 0
        self.log.reset_mock()
        decoded = []
        real_decode = pointstore.FlashPointStore._decode

        def counting_decode(store, value, include_measurement=True):
            decoded.append(include_measurement)
            return real_decode(store, value, include_measurement)

        with mock.patch.object(pointstore.FlashPointStore, "_decode", counting_decode):
            second = tracking.Tracker(self.path)
        # Four point-store records: the scan decodes each once, no summary rebuild.
        self.assertEqual(len(decoded), 4)
        self.assertEqual(second.backup(), before)
        self.assertEqual(state.app.stats["legacy_conversion_errors"], 0)
        self.assertEqual(self.errors(), [])
        self.assertLess(second._checkpoint_dirty_events, tracking.CHECKPOINT_BATCH_EVENTS)
        # The deleted copy is not recreated and the stale partial copy is gone.
        self.assertFalse(os.path.exists(checkpoint + ".v4"))
        self.assertFalse(os.path.exists(checkpoint + ".v4.tmp"))
        _close(second)

    def write_large_schema4_store(self, assets=40):
        self.write_schema4_store()
        checkpoint = self.path + ".checkpoint.json"
        with open(checkpoint, encoding="utf-8") as source:
            rows = [json.loads(raw) for raw in source]
        extra = [{"record": "asset", "project_id": "p", "point": self.asset(
            "V-%d" % index, None, [8.0, 50.0 + index / 10000.0, 100.0], None, 10 + index)}
            for index in range(assets)]
        _write_lines(checkpoint, rows[:-1] + extra + rows[-1:])
        return checkpoint

    def test_legacy_checkpoint_copy_yields_between_chunks(self):
        # W15: the copy ran inside the first synchronous load step, while the
        # watchdog waits for the scheduler. It now yields after every chunk.
        checkpoint = self.write_large_schema4_store()
        size = os.path.getsize(checkpoint)
        self.assertGreater(size, 3 * 4096)
        tracker = tracking.Tracker(self.path, autoload=False)
        tracker.point_store.load()
        mid_copy = 0
        for _ in tracker._load_steps():
            if os.path.exists(checkpoint + ".v4.tmp"):
                mid_copy += 1
        self.assertGreaterEqual(mid_copy, size // 4096)
        with open(checkpoint, "rb") as source, open(checkpoint + ".v4", "rb") as copied:
            self.assertEqual(copied.read(), source.read())
        self.assertEqual(len(tracker.projects[0]["points"]), 40)
        # The reserve check sees fresh flash figures and the copy is budgeted.
        _close(tracker)

    def test_interrupted_legacy_checkpoint_copy_is_not_retried(self):
        checkpoint = self.write_large_schema4_store()
        with open(checkpoint + ".v4.tmp", "wb") as target:
            target.write(b"partial")
        recovered = tracking.Tracker(self.path)
        self.assertEqual(len(recovered.projects[0]["points"]), 40)
        self.assertFalse(os.path.exists(checkpoint + ".v4"))
        self.assertFalse(os.path.exists(checkpoint + ".v4.tmp"))
        errors = self.errors()
        self.assertEqual(len(errors), 1)
        self.assertIn("legacy checkpoint", errors[0][2])
        _close(recovered)

    def test_legacy_checkpoint_copy_uses_a_fresh_reserve_figure(self):
        checkpoint = self.write_large_schema4_store()
        size = os.path.getsize(checkpoint)
        calls = []

        def capacity(temporary_bytes=16384, force=False):
            calls.append((temporary_bytes, force))
            return {"writable": True}

        tracking.Tracker.storage_capacity.side_effect = capacity
        try:
            recovered = tracking.Tracker(self.path)
        finally:
            tracking.Tracker.storage_capacity.side_effect = None
        self.assertIn((size, True), calls)
        self.assertGreaterEqual(recovered._storage_budget_used, size)
        _close(recovered)

    def test_legacy_checkpoint_copy_is_kept_once(self):
        self.write_reviewer_store()
        checkpoint = self.path + ".checkpoint.json"
        with open(checkpoint, "rb") as source:
            original = source.read()
        recovered = tracking.Tracker(self.path)
        with open(checkpoint + ".v4", "rb") as source:
            self.assertEqual(source.read(), original)
        self.assertFalse(os.path.exists(checkpoint + ".v4.tmp"))
        recovered.compact()
        with open(checkpoint, encoding="utf-8") as source:
            self.assertEqual(json.loads(source.readline())["schema_version"], 5)
        _close(recovered)
        # A schema-5 checkpoint makes no copy, and an existing copy is never overwritten.
        with open(checkpoint + ".v4", "ab") as target:
            target.write(b"marker")
        again = tracking.Tracker(self.path)
        with open(checkpoint + ".v4", "rb") as source:
            self.assertEqual(source.read(), original + b"marker")
        _close(again)
        # A legacy checkpoint loaded twice is copied once.
        shutil.rmtree(self.tmp.name); os.makedirs(self.tmp.name)
        self.write_reviewer_store()
        _close(tracking.Tracker(self.path))
        with open(checkpoint + ".v4", "ab") as target:
            target.write(b"marker")
        _close(tracking.Tracker(self.path))
        with open(checkpoint + ".v4", "rb") as source:
            self.assertEqual(source.read(), original + b"marker")

    def test_legacy_checkpoint_copy_respects_the_flash_reserve(self):
        self.write_reviewer_store()
        checkpoint = self.path + ".checkpoint.json"
        size = os.stat(checkpoint).st_size
        requests = []

        def capacity(temporary_bytes=16384, force=False):
            requests.append(temporary_bytes)
            return {"writable": temporary_bytes != size}

        # setUp replaced storage_capacity with a mock that always has room.
        tracking.Tracker.storage_capacity.side_effect = capacity
        recovered = tracking.Tracker(self.path)
        tracking.Tracker.storage_capacity.side_effect = None
        self.assertIn(size, requests)
        self.assertEqual(self.summary(recovered), [
            ("Bad", [("l1", 2)], ["V-far", "V-near"]), ("Second", [("l2", 2)], ["V-free"])])
        self.assertFalse(os.path.exists(checkpoint + ".v4"))
        self.assertFalse(os.path.exists(checkpoint + ".v4.tmp"))
        warnings = [c.args for c in self.log.call_args_list if c.args[0] == "WARN"]
        self.assertEqual(len(warnings), 1)
        self.assertIn("legacy checkpoint", warnings[0][2])
        self.assertIn("reserve", warnings[0][2])
        _close(recovered)

    def test_upgrade_goes_on_when_the_copy_cannot_be_written(self):
        self.write_reviewer_store()
        copy_path = self.path + ".checkpoint.json.v4"
        real_open = open

        def failing_open(name, *args, **kwargs):
            if str(name).startswith(copy_path):
                raise OSError(28, "No space left on device")
            return real_open(name, *args, **kwargs)

        with mock.patch("builtins.open", side_effect=failing_open):
            recovered = tracking.Tracker(self.path)
        self.assertEqual(self.summary(recovered), [
            ("Bad", [("l1", 2)], ["V-far", "V-near"]), ("Second", [("l2", 2)], ["V-free"])])
        self.assertFalse(os.path.exists(copy_path))
        self.assertFalse(os.path.exists(copy_path + ".tmp"))
        warnings = [c.args for c in self.log.call_args_list if c.args[0] == "WARN"]
        self.assertEqual(len(warnings), 1)
        self.assertIn("legacy checkpoint", warnings[0][2])
        recovered.compact()
        _close(recovered)
        again = tracking.Tracker(self.path)
        self.assertEqual(self.summary(again), self.summary(recovered))
        _close(again)
