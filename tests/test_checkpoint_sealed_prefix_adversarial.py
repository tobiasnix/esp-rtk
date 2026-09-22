# SPDX-License-Identifier: AGPL-3.0-only
"""Independent adversarial cases for verified sealed-prefix reuse.

These use real temporary segment files. No cached offset or digest is trusted
as an expected result: complete coordinates are compared with a cold replay.
"""
import asyncio
import builtins
import copy
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from support import state  # MicroPython compatibility only
import pointstore
import tracking
from test_tracking import FIX


class TestSealedPrefixAdversarial(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="rtk-prefix-adversarial-")
        self.root = Path(self.temporary.name)
        self.segment_patch = mock.patch.object(pointstore, "POINT_SEGMENT_BYTES", 700)
        self.segment_patch.start()
        self.store = pointstore.FlashPointStore(str(self.root / "points"))
        self.probes = []
        self.coordinates = []
        for order in range(1, 25):
            self.append(order)
        self.store.sync()
        self.assertGreater(len(self.store.segments), 2)

    def tearDown(self):
        for store in self.probes + [self.store]:
            store.close()
        self.segment_patch.stop()
        self.temporary.cleanup()

    def append(self, order, station="STATION-A"):
        coordinate = [8.0 + order / 10000, 50.0, 100.0]
        self.store.append(order, "line-a", coordinate, {
            "record_order": order, "source": "automatic", "station_id": station,
            "profile_id": "PROFILE-A", "hdop": 0.6, "satellites": 24})
        self.coordinates.append(coordinate)

    def files(self):
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in self.root.rglob("*") if path.is_file()}

    async def probe(self, accept=False):
        probe = pointstore.FlashPointStore(self.store.path, autoload=False)
        self.probes.append(probe)
        await probe.load_async(verified_store=self.store)
        if accept:
            self.store.accept_verification(probe)
        return probe

    def assert_replay(self, probe, expected):
        self.assertEqual(list(probe.sequences("line-a")[0]), expected)
        cold = pointstore.FlashPointStore(self.store.path)
        self.probes.append(cold)
        self.assertEqual(list(cold.sequences("line-a")[0]), expected)
        self.assertEqual(list(probe.offsets["line-a"]), list(cold.offsets["line-a"]))
        self.assertEqual(probe.max_order, cold.max_order)
        self.assertEqual(probe.valid_bytes, cold.valid_bytes)

    async def test_tail_removes_cross_sealed_boundary_without_mutating_or_double_applying_prefix(self):
        await self.probe(accept=True)
        tail = self.store.segments[-1]
        tail_vertices = sum((locator >> pointstore._OFFSET_BITS) == tail
                            for locator in self.store.offsets["line-a"])
        removed = tail_vertices + 2
        for order in range(25, 25 + removed):
            self.store.remove_last(order, "line-a")
        self.store.sync()
        expected = self.coordinates[:-removed]
        for _ in range(3):
            previous = copy.deepcopy(self.store._verified_prefix)
            probe = await self.probe()
            self.assertEqual(self.store._verified_prefix, previous,
                             "Applying tail removes must not mutate the accepted sealed index")
            self.assert_replay(probe, expected)
            self.store.accept_verification(probe)

    async def test_rotation_and_appended_string_table_rebuild_match_cold_replay(self):
        await self.probe(accept=True)
        original_segments = list(self.store.segments)
        for order in range(25, 46):
            self.append(order, station="NEW-STATION" if order >= 31 else "STATION-A")
        self.store.sync()
        self.assertGreater(len(self.store.segments), len(original_segments))
        probe = await self.probe(accept=True)
        self.assert_replay(probe, self.coordinates)
        self.assertEqual(probe.sequences("line-a")[1][-1]["station_id"], "NEW-STATION")

    async def test_legitimate_generation_change_cannot_reuse_previous_generation_index(self):
        await self.probe(accept=True)
        self.store.close()
        old_generation = self.store.generation
        generation = old_generation + 1
        for number in self.store.segments:
            shutil.copyfile(self.store._segment_path(number), self.store._segment_path(number, generation))
        self.store._activate_manifest(generation, list(self.store.segments))
        self.assertIsNone(self.store._verified_prefix)
        self.store._scan_generation()
        probe = await self.probe(accept=True)
        self.assertEqual(probe.generation, generation)
        self.assert_replay(probe, self.coordinates)

    async def test_valid_same_size_json_tampering_in_sealed_segment_is_rejected_without_repairs(self):
        await self.probe(accept=True)
        path = Path(self.store._segment_path(self.store.segments[0]))
        original = path.read_bytes()
        changed = original.replace(b"50.0", b"51.0", 1)
        self.assertNotEqual(changed, original)
        self.assertEqual(len(changed), len(original))
        for line in changed.splitlines():
            json.loads(line)
        path.write_bytes(changed)
        before = self.files()
        previous = copy.deepcopy(self.store._verified_prefix)
        with self.assertRaises((ValueError, OSError)):
            await self.probe()
        self.assertEqual(self.files(), before)
        self.assertEqual(self.store._verified_prefix, previous)

    async def test_external_string_table_semantics_change_is_rejected(self):
        await self.probe(accept=True)
        path = Path(self.store.manifest_path)
        manifest = json.loads(path.read_text())
        self.assertTrue(manifest["strings"])
        manifest["strings"][0] = "REPLACED-PROFILE"
        path.write_text(json.dumps(manifest))
        before = self.files()
        with self.assertRaises((ValueError, OSError)):
            await self.probe()
        self.assertEqual(self.files(), before)

    async def test_same_size_invalid_string_reference_in_mutable_tail_is_fully_validated(self):
        await self.probe(accept=True)
        path = Path(self.store._segment_path(self.store.segments[-1]))
        original = path.read_bytes()
        self.assertLess(len(self.store.strings), 9)
        changed = original.replace(b'[-1, "@", 0]', b'[-1, "@", 9]', 1)
        self.assertNotEqual(changed, original)
        self.assertEqual(len(changed), len(original))
        for line in changed.splitlines():
            json.loads(line)
        path.write_bytes(changed)
        before = self.files()
        with self.assertRaises((ValueError, OSError)):
            await self.probe()
        self.assertEqual(self.files(), before)

    async def test_checkpoint_probe_does_not_promote_a_fallback_point_manifest(self):
        await self.probe(accept=True)
        os.rename(self.store.manifest_path, self.store.manifest_path + ".previous")
        before = self.files()
        with self.assertRaises((ValueError, OSError)):
            await self.probe()
        self.assertEqual(self.files(), before)

    async def test_torn_tail_fails_strict_verification_without_attempting_a_write(self):
        await self.probe(accept=True)
        self.store.close()
        path = Path(self.store._segment_path(self.store.segments[-1]))
        with path.open("ab") as stream:
            stream.write(b'[2,25,"line-a",')
        before, attempted = self.files(), []

        def readonly(filename, mode="r", *args, **kwargs):
            if any(marker in mode for marker in "wa+"):
                attempted.append((str(filename), mode))
                raise AssertionError("Verification attempted disk repair")
            return builtins.open(filename, mode, *args, **kwargs)

        with mock.patch.object(pointstore, "open", readonly, create=True):
            with self.assertRaises((ValueError, OSError)):
                await self.probe()
        self.assertEqual(attempted, [])
        self.assertEqual(self.files(), before)

    async def test_live_rotation_during_yield_rejects_snapshot_and_does_not_publish_cache(self):
        await self.probe(accept=True)
        previous = copy.deepcopy(self.store._verified_prefix)
        changed = [False]

        async def mutate_live_store(_delay):
            if not changed[0]:
                changed[0] = True
                for order in range(25, 34):
                    self.append(order, station="ROTATION-STATION")
                self.store.sync()
            await asyncio.sleep(0)

        with mock.patch.object(state.asyncio, "sleep_ms", mutate_live_store):
            with self.assertRaises((ValueError, OSError)):
                await self.probe()
        self.assertTrue(changed[0])
        self.assertEqual(self.store._verified_prefix, previous)


class TestCheckpointRetirementAdversarial(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="rtk-checkpoint-adversarial-")
        self.path = str(Path(self.temporary.name) / "tracking")
        self.storage_patch = mock.patch.object(tracking.Tracker, "storage_capacity", return_value={
            "free_flash_bytes": 8_000_000, "required_flash_reserve_bytes": 3_000_000,
            "estimated_temporary_bytes": 16_384, "writable": True})
        self.storage_patch.start()
        self.segment_patch = mock.patch.object(pointstore, "POINT_SEGMENT_BYTES", 1000)
        self.segment_patch.start()
        self.tracker = tracking.Tracker(self.path)
        self.tracker.create_project("Protected journal")
        self.tracker.start_line("Line", 0, fix=FIX)
        for index in range(1, 13):
            self.tracker.add_vertex(dict(FIX, lon=8.0 + index / 10000))
        self.tracker.point_store.sync()
        self.assertGreater(len(self.tracker.point_store.segments), 2)

    def tearDown(self):
        self.tracker._sync_journal(close=True)
        self.tracker.point_store.close()
        self.segment_patch.stop()
        self.storage_patch.stop()
        self.temporary.cleanup()

    def journals(self):
        self.tracker._sync_journal()
        return {path: Path(path).read_bytes() for _, path in self.tracker._segment_files()}

    async def test_checkpoint_rejection_keeps_every_covered_journal_byte_and_cache(self):
        before = self.journals()
        cache = copy.deepcopy(getattr(self.tracker.point_store, "_verified_prefix", None))
        with mock.patch.object(tracking.Tracker, "_load_checkpoint", return_value=0):
            with self.assertRaises(ValueError):
                await self.tracker.compact_async()
        for path, content in before.items():
            self.assertEqual(Path(path).read_bytes(), content)
        self.assertEqual(getattr(self.tracker.point_store, "_verified_prefix", None), cache)

    async def test_event_change_during_checkpoint_probe_prevents_journal_retirement(self):
        before = self.journals()
        original = pointstore.FlashPointStore.load_async
        changed = [False]

        async def load_then_mutate(probe, *args, **kwargs):
            await original(probe, *args, **kwargs)
            if kwargs.get("verified_store") is self.tracker.point_store:
                # A real UI metadata action changes event order without changing
                # the point-store snapshot; the tracker must reject it separately.
                self.tracker.rename_project(self.tracker.active_project_id, "Changed during verification")
                changed[0] = True

        with mock.patch.object(pointstore.FlashPointStore, "load_async", load_then_mutate):
            with self.assertRaises(ValueError):
                await self.tracker.compact_async()
        self.assertTrue(changed[0])
        for path, content in before.items():
            self.assertEqual(Path(path).read_bytes(), content)

    async def mutate_after_verification(self, mutation, after_yields=1):
        """Run a real UI action at a post-verification scheduler boundary."""
        verified, count, changed = [False], [0], [False]
        profile = self.tracker._profile_add

        def mark_profile(name, started):
            profile(name, started)
            if name == "checkpoint_verify":
                verified[0] = True

        async def scheduler(_delay):
            if verified[0]:
                count[0] += 1
                if count[0] == after_yields:
                    changed[0] = True
                    mutation()
            await asyncio.sleep(0)

        with mock.patch.object(self.tracker, "_profile_add", mark_profile), \
                mock.patch.object(state.asyncio, "sleep_ms", scheduler):
            with self.assertRaises(ValueError):
                await self.tracker.compact_async()
        self.assertTrue(changed[0], "The requested scheduler boundary was not exercised")

    def assert_cold_replay_matches(self):
        expected = self.tracker.backup()
        self.tracker._sync_journal(close=True)
        self.tracker.point_store.close()
        cold = tracking.Tracker(self.path)
        try:
            actual = cold.backup()
            # A cold load rebuilds this transient aggregate after Undo. All
            # durable metadata, active references and measurement values must
            # still match, whether the live aggregate has been recomputed yet.
            for payload in (actual, expected):
                for project in payload["projects"]:
                    for line in project["lines"]:
                        line.pop("_summary", None)
            self.assertEqual(actual, expected)
        finally:
            cold._sync_journal(close=True)
            cold.point_store.close()

    async def test_ui_mutation_after_probe_keeps_all_journals_and_unpublished_cache(self):
        before = self.journals()
        cache = copy.deepcopy(self.tracker.point_store._verified_prefix)
        await self.mutate_after_verification(lambda: self.tracker.rename_project(
            self.tracker.active_project_id, "Changed after verification"))
        for path, content in before.items():
            self.assertEqual(Path(path).read_bytes(), content)
        self.assertEqual(self.tracker.point_store._verified_prefix, cache)
        self.assert_cold_replay_matches()

    async def test_mutation_during_retirement_retains_remaining_journal_and_recovers(self):
        self.tracker._sync_journal(close=True)
        self.tracker._segment_number += 1
        self.tracker.rename_project(self.tracker.active_project_id, "Second journal")
        before = self.journals()
        self.assertEqual(len(before), 2)
        cache = copy.deepcopy(self.tracker.point_store._verified_prefix)
        await self.mutate_after_verification(lambda: self.tracker.rename_project(
            self.tracker.active_project_id, "Changed during retirement"), after_yields=2)
        paths = sorted(before)
        self.assertFalse(Path(paths[0]).exists(), "First covered segment was already retired")
        self.assertEqual(Path(paths[1]).read_bytes(), before[paths[1]])
        self.assertEqual(self.tracker.point_store._verified_prefix, cache)
        self.assert_cold_replay_matches()

    async def test_new_line_at_final_yield_cannot_be_dropped_by_stale_rewrite_selection(self):
        selected, changed = [False], [False]
        original = self.tracker.point_store.rewrite_temporary_bytes

        def capture_selection(active_lines):
            selected[0] = True
            return original(active_lines)

        async def scheduler(_delay):
            if selected[0] and not changed[0]:
                changed[0] = True
                self.tracker.set_line_state("finish")
                self.tracker.start_line("New during final yield", 0,
                                        fix=dict(FIX, lon=9.0))
            await asyncio.sleep(0)

        with mock.patch.object(self.tracker.point_store, "rewrite_temporary_bytes", capture_selection), \
                mock.patch.object(self.tracker.point_store, "rewrite", wraps=self.tracker.point_store.rewrite) as rewrite, \
                mock.patch.object(state.asyncio, "sleep_ms", scheduler):
            with self.assertRaises(ValueError):
                await self.tracker.compact_async()
        self.assertTrue(changed[0])
        rewrite.assert_not_called()
        self.assertEqual(self.tracker.feature_count(), 14)
        self.assertEqual(list(self.tracker._line(self.tracker._project())["coordinates"]),
                         [[9.0, 50.0, 123.4]])
        self.assert_cold_replay_matches()

    async def test_second_failed_checkpoint_with_same_event_order_cannot_expand_retirement(self):
        before = self.journals()
        expected_order = self.tracker._event_sequence

        def second_checkpoint():
            with mock.patch.object(tracking.Tracker, "_load_checkpoint", return_value=0):
                with self.assertRaises(ValueError):
                    self.tracker.compact()
            self.assertEqual(self.tracker._event_sequence, expected_order)

        await self.mutate_after_verification(second_checkpoint)
        for path, content in before.items():
            self.assertEqual(Path(path).read_bytes(), content)
        self.assert_cold_replay_matches()

    async def test_undo_after_partial_retirement_keeps_removed_point_removed_on_replay(self):
        self.tracker._sync_journal(close=True)
        self.tracker._segment_number += 1
        self.tracker.rename_project(self.tracker.active_project_id, "Second journal")
        await self.mutate_after_verification(self.tracker.undo_last, after_yields=2)
        self.assertEqual(self.tracker.feature_count(), 12)
        self.assert_cold_replay_matches()

    async def test_undo_after_completed_checkpoint_keeps_active_references_on_replay(self):
        await self.tracker.compact_async()
        self.tracker.undo_last()
        self.assertEqual(self.tracker.feature_count(), 12)
        self.assert_cold_replay_matches()

    async def test_project_deletion_after_partial_retirement_is_replayed(self):
        self.tracker._sync_journal(close=True)
        self.tracker._segment_number += 1
        self.tracker.rename_project(self.tracker.active_project_id, "Second journal")

        def delete():
            project_id = self.tracker.active_project_id
            self.tracker.set_line_state("finish")
            self.tracker.delete_project(project_id)

        await self.mutate_after_verification(delete, after_yields=2)
        self.assertEqual(self.tracker.feature_count(), 0)
        self.assert_cold_replay_matches()

    def assert_missing_old_vertices_reject_checkpoint(self, removed):
        self.tracker._sync_journal(close=True)
        self.tracker.point_store.close()
        remaining = removed
        for number in self.tracker.point_store.segments:
            path = Path(self.tracker.point_store._segment_path(number))
            kept = []
            for raw in path.read_bytes().splitlines(keepends=True):
                record = self.tracker.point_store._decode(json.loads(raw))
                if (remaining and record["record"] == "vertex" and
                        record["order"] <= self.tracker._checkpoint_order):
                    remaining -= 1
                else:
                    kept.append(raw)
            path.write_bytes(b"".join(kept))
        self.assertEqual(remaining, 0)
        probe = tracking.Tracker(self.path, autoload=False)
        root = Path(self.temporary.name)
        before = {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
        try:
            self.assertEqual(probe._load_checkpoint(readonly=True), 0,
                             "Later valid records must not excuse missing historical points")
            self.assertEqual({str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}, before)
        finally:
            probe.point_store.close()

    async def test_missing_old_vertex_is_not_excused_by_later_valid_undo(self):
        await self.tracker.compact_async()
        self.tracker.undo_last()
        self.assert_missing_old_vertices_reject_checkpoint(1)

    async def test_missing_old_vertices_are_not_excused_by_later_valid_append(self):
        await self.tracker.compact_async()
        self.tracker.add_vertex(dict(FIX, lon=9.0))
        self.assert_missing_old_vertices_reject_checkpoint(2)

    async def test_readonly_checkpoint_verification_never_promotes_fallback(self):
        self.tracker._write_checkpoint()
        probe = tracking.Tracker(self.path, autoload=False, point_store_autoload=False)
        try:
            await probe.point_store.load_async(verified_store=self.tracker.point_store)
            os.rename(self.tracker.checkpoint_path, self.tracker.checkpoint_path + ".previous")
            root = Path(self.temporary.name)
            before = {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
            with mock.patch.object(tracking.os, "rename", side_effect=AssertionError("Unexpected promotion")), \
                    mock.patch.object(tracking.os, "remove", side_effect=AssertionError("Unexpected removal")):
                self.assertEqual(probe._load_checkpoint(readonly=True), 0)
            self.assertEqual({str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}, before)
        finally:
            probe.point_store.close()


if __name__ == "__main__":
    unittest.main()
