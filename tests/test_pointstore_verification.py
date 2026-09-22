# SPDX-License-Identifier: AGPL-3.0-only
"""Measured work boundaries and complete byte coverage of checkpoint probes."""
import asyncio
import builtins
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from support import state
import pointstore
import tracking
from test_tracking import FIX


class TestPointstoreVerification(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="rtk-point-verification-")
        self.path = str(Path(self.temporary.name) / "points")
        self.store = pointstore.FlashPointStore(self.path)
        self.probes = []
        self.segment_patch = mock.patch.object(pointstore, "POINT_SEGMENT_BYTES", 480)
        self.segment_patch.start()

    def tearDown(self):
        for store in self.probes + [self.store]:
            store.close()
        self.segment_patch.stop()
        self.temporary.cleanup()

    def populate(self):
        for order in range(1, 25):
            self.store.append(order, "line-a", [8.0, 50.0, order],
                {"record_order": order, "profile_id": "profile-a"})
        self.store.sync()
        self.assertGreater(len(self.store.segments), 1)

    async def probe(self, accept=False):
        probe = pointstore.FlashPointStore(self.path, autoload=False)
        self.probes.append(probe)
        await probe.load_async(verified_store=self.store)
        if accept:
            self.store.accept_verification(probe)
        return probe

    async def test_warm_probe_hashes_every_sealed_byte_and_decodes_entire_tail(self):
        self.populate()
        cold = await self.probe()
        self.assertIsNone(self.store._verified_prefix)
        self.assertEqual(cold.scan_profile["scan_decoded_records"], 24)
        self.store.accept_verification(cold)
        prefix = copy.deepcopy(self.store._verified_prefix)
        sizes = []
        real_open = builtins.open

        class HashedFile:
            def __init__(self, source): self.source = source
            def __enter__(self): return self
            def __exit__(self, *args): self.source.close()
            def read(self, amount):
                sizes.append(amount)
                return self.source.read(amount)

        def observed_open(path, mode="r", *args, **kwargs):
            source = real_open(path, mode, *args, **kwargs)
            return HashedFile(source) if mode == "rb" else source

        with mock.patch.object(pointstore, "open", observed_open, create=True):
            warm = await self.probe()
        tail = Path(self.store._segment_path(self.store.segments[-1]))
        self.assertEqual(warm.scan_profile["scan_decoded_records"], len(tail.read_bytes().splitlines()))
        self.assertEqual(warm.scan_profile["scan_hash_bytes"], sum(item[1] for item in prefix["digests"]))
        self.assertEqual(warm.scan_profile["scan_verified_sealed_segments"], len(prefix["segments"]))
        self.assertTrue(sizes)
        self.assertEqual(set(sizes), {pointstore.SCAN_HASH_BYTES})
        self.assertEqual(list(warm.offsets["line-a"]), list(self.store.offsets["line-a"]))
        self.assertEqual(self.store._verified_prefix, prefix)

    async def test_failed_or_unstarted_probe_cannot_publish_evidence(self):
        self.populate()
        await self.probe(accept=True)
        prefix = copy.deepcopy(self.store._verified_prefix)
        probe = pointstore.FlashPointStore(self.path, autoload=False)
        self.probes.append(probe)
        with self.assertRaises(ValueError):
            self.store.accept_verification(probe)
        self.store.close()
        tail = Path(self.store._segment_path(self.store.segments[-1]))
        with tail.open("ab") as target:
            target.write(b'[2,25,"line-a",')
        with self.assertRaises(ValueError):
            await probe.load_async(verified_store=self.store)
        with self.assertRaises(ValueError):
            self.store.accept_verification(probe)
        self.assertEqual(self.store._verified_prefix, prefix)

    async def test_strict_probe_rejects_out_of_order_tail_without_truncation(self):
        self.populate()
        await self.probe(accept=True)
        self.store.close()
        tail = Path(self.store._segment_path(self.store.segments[-1]))
        records = [json.loads(line) for line in tail.read_text().splitlines()]
        records[-1][1] = records[0][1]
        tail.write_text("".join(json.dumps(value) + "\n" for value in records))
        before = tail.read_bytes()
        with self.assertRaises(ValueError):
            await self.probe()
        self.assertEqual(tail.read_bytes(), before)

    async def test_empty_store_verifies_without_creating_files(self):
        probe = await self.probe(accept=True)
        self.assertEqual(probe.max_order, 0)
        self.assertEqual(list(Path(self.temporary.name).iterdir()), [])

    async def test_unmanifested_legacy_bytes_are_not_ignored_or_migrated(self):
        Path(self.path).write_text('unverified')
        with self.assertRaises(ValueError):
            await self.probe()
        self.assertEqual(Path(self.path).read_text(), 'unverified')

    async def test_live_store_cannot_be_its_own_destructive_probe(self):
        self.populate()
        before = list(self.store.offsets["line-a"])
        with self.assertRaises(ValueError):
            await self.store.load_async(verified_store=self.store)
        self.assertEqual(list(self.store.offsets["line-a"]), before)

    async def test_cold_rewrite_preserves_tombstone_only_segment_after_rotation(self):
        self.populate()
        # Force the next remove into its own segment, with no appended vertex.
        with mock.patch.object(pointstore, "POINT_SEGMENT_BYTES", self.store._writer_bytes):
            self.store.remove_last(25, "line-a")
        self.store.close()
        removed_segment = self.store.segments[-1]
        cold = pointstore.FlashPointStore(self.path)
        self.probes.append(cold)
        self.assertEqual(len(cold.offsets["line-a"]), 23)
        self.assertIn("line-a", cold.segment_lines[removed_segment])
        cold.rewrite(["line-a"])
        restored = pointstore.FlashPointStore(self.path)
        self.probes.append(restored)
        self.assertEqual(len(restored.offsets["line-a"]), 23)

    async def test_historical_counts_explain_later_adds_and_multiple_undos(self):
        self.populate()
        boundary = self.store.max_order
        self.store.append(25, "line-a", [8.1, 50.0, 101], {"record_order": 25})
        for order in (26, 27, 28):
            self.store.remove_last(order, "line-a")
        self.store.sync()
        self.assertEqual(len(self.store.offsets["line-a"]), 22)
        self.assertTrue(self.store.checkpoint_counts_match({"line-a": 24}, boundary))
        # A fabricated older count cannot be excused by the same real undos.
        self.assertFalse(self.store.checkpoint_counts_match({"line-a": 25}, boundary))

    async def test_regular_checkpoint_does_not_rescan_historical_counts(self):
        with tempfile.TemporaryDirectory(prefix="rtk-no-undo-checkpoint-") as temporary:
            tracker = tracking.Tracker(str(Path(temporary) / "tracking"))
            try:
                tracker.create_project("No undo")
                tracker.start_line("Line", 0, fix=FIX)
                with mock.patch.object(pointstore.FlashPointStore, "checkpoint_counts_match",
                        side_effect=AssertionError("Unexpected historical replay")):
                    await tracker.compact_async()
            finally:
                tracker._sync_journal(close=True)
                tracker.point_store.close()

    async def test_scan_profile_separates_next_work_postlude_and_scheduler_wait(self):
        self.populate()
        probe = pointstore.FlashPointStore(self.path, autoload=False)
        self.probes.append(probe)
        clock = [0]
        original_manifest = probe._load_manifest

        def manifest(*args, **kwargs):
            clock[0] += 3
            return original_manifest(*args, **kwargs)

        def work(*args, **kwargs):
            for delay in (7, 11, 13):
                clock[0] += delay
                yield None
            clock[0] += 5

        async def wait(_delay):
            clock[0] += 101
            await asyncio.sleep(0)

        with mock.patch.object(pointstore, "_ticks_us", side_effect=lambda: clock[0]), \
                mock.patch.object(probe, "_load_manifest", side_effect=manifest), \
                mock.patch.object(probe, "_scan_generation_steps", side_effect=work), \
                mock.patch.object(state.asyncio, "sleep_ms", wait):
            await probe.load_async()
        profile = probe.scan_profile
        self.assertEqual(profile["scan_manifest_us"], 3)
        self.assertEqual(profile["scan_work_us_sum"], 36)
        self.assertEqual(profile["scan_batch_max_us"], 13)
        self.assertEqual(profile["scan_batch_last_us"], 5)
        self.assertEqual(profile["scan_postlude_us"], 5)
        self.assertEqual(profile["scan_batches"], 3)
        self.assertEqual(profile["scan_wait_us_sum"], 303)
        self.assertEqual(profile["scan_wait_last_us"], 101)
        self.assertEqual(profile["scan_wait_max_us"], 101)


class TestCheckpointEarlyMutation(unittest.IsolatedAsyncioTestCase):
    async def test_mutation_at_first_yield_is_not_hidden_by_later_snapshot(self):
        with tempfile.TemporaryDirectory(prefix="rtk-early-checkpoint-mutation-") as temporary:
            tracker = tracking.Tracker(str(Path(temporary) / "tracking"))
            try:
                tracker.create_project("Original")
                tracker.start_line("Line", 0, fix=FIX)
                tracker._sync_journal()
                journal = Path(tracker._segment_path(tracker._segment_number))
                before = journal.read_bytes()
                changed = [False]

                async def mutate(_delay):
                    if not changed[0]:
                        tracker.rename_project(tracker.active_project_id, "Changed")
                        changed[0] = True
                    await asyncio.sleep(0)

                with mock.patch.object(state.asyncio, "sleep_ms", mutate):
                    with self.assertRaises(ValueError):
                        await tracker.compact_async()
                self.assertEqual(journal.read_bytes(), before)
                self.assertIn("scan_work_us_sum", tracker.append_profile()["checkpoint_scan"])
            finally:
                tracker._sync_journal(close=True)
                tracker.point_store.close()
