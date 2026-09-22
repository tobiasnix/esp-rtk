# SPDX-License-Identifier: AGPL-3.0-only
import os
import json
import tempfile
import unittest
import asyncio
from unittest import mock

from support import state  # noqa: F401 - MicroPython compatibility
from pointstore import FlashPointStore, _compact, _expand


class TestFlashPointStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="esp-rtk-pointstore-")
        self.path = os.path.join(self.tmp.name, "points.jsonl")
        self.store = FlashPointStore(self.path, cache_size=3)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def append(self, order, line="line-a"):
        self.store.append(order, line, [8.0 + order / 1000, 50.0, 100.0],
                          {"record_order": order, "source": "automatic"})

    def test_keeps_only_offsets_and_a_bounded_expansion_cache(self):
        for order in range(1, 101):
            self.append(order)
        coordinates, measurements = self.store.sequences("line-a")
        self.assertEqual(len(coordinates), 100)
        self.assertEqual(measurements[99]["record_order"], 100)
        for index in range(20):
            self.assertEqual(coordinates[index][1], 50.0)
        self.assertLessEqual(len(self.store._cache), 3)
        self.assertFalse(any(isinstance(value, dict)
                             for value in self.store.offsets["line-a"]))
        self.assertGreater(self.store.profile_us["encode"], 0)
        self.assertGreaterEqual(self.store.profile_us["write"], 0)

    def test_async_scan_verifies_all_records_in_bounded_batches(self):
        for order in range(1, 130):
            self.append(order)
        self.store.sync()
        restored = FlashPointStore(self.path, autoload=False)
        self.assertEqual(restored.max_order, 0)
        batches = []
        previous = [0]

        async def service_network(delay):
            batches.append(restored.max_order - previous[0])
            previous[0] = restored.max_order
            await asyncio.sleep(0)

        with mock.patch.object(state.asyncio, "sleep_ms", service_network), \
                mock.patch.object(FlashPointStore, "_scan_generation",
                                  side_effect=AssertionError("Synchronous scan")):
            asyncio.run(restored.load_async())
        self.assertEqual(restored.max_order, 129)
        self.assertGreater(len(batches), 1)
        self.assertLessEqual(max(batches), 32)
        self.assertEqual(list(restored.offsets["line-a"]), list(self.store.offsets["line-a"]))
        self.assertEqual(list(restored.sequences("line-a")[0]),
                         list(self.store.sequences("line-a")[0]))
        restored.close()

    def test_async_scan_preserves_valid_prefix_when_tail_is_torn(self):
        self.append(1); self.append(2)
        self.store.remove_last(3, "line-a")
        self.store.close()
        segment = self.store._segment_path(1)
        valid_size = os.path.getsize(segment)
        with open(segment, "ab") as target:
            target.write(b'[2,4,"line-a",')
        restored = FlashPointStore(self.path, autoload=False)
        asyncio.run(restored.load_async())
        self.assertEqual(restored.max_order, 3)
        self.assertEqual(len(restored.sequences("line-a")[0]), 1)
        self.assertEqual(os.path.getsize(segment), valid_size)
        restored.close()

    def test_repeated_measurement_strings_are_interned_losslessly(self):
        value = {"source": "automatic", "fix_status": "RTK_FIXED",
                 "receiver_accuracy": {"source": "NMEA_GST"},
                 "reasons": ["accepted", "unchanged custom text"],
                 "custom": {"$": 0}}
        compact = _compact(value)
        self.assertEqual(_expand(compact), value)
        encoded = json.dumps(compact, separators=(",", ":"))
        self.assertNotIn("automatic", encoded)
        self.assertNotIn("RTK_FIXED", encoded)
        self.assertIn("unchanged custom text", encoded)

    def test_dynamic_station_and_profile_strings_survive_restart(self):
        measurement = {"record_order": 1, "source": "automatic",
                       "station_id": "SYNTH-STATION",
                       "profile_id": "synthetic-profile"}
        self.store.append(1, "line-a", [8, 50, 100], measurement)
        self.store.sync(); self.store.close()
        recovered = FlashPointStore(self.path)
        restored = recovered.sequences("line-a")[1][0]
        self.assertEqual(restored, measurement)
        self.assertEqual(recovered.strings,
                         ["synthetic-profile", "SYNTH-STATION"])

    def test_schema_two_manifest_remains_readable(self):
        self.append(1); self.store.sync(); self.store.close()
        with open(self.store.manifest_path, encoding="utf-8") as source:
            manifest = json.load(source)
        manifest["schema"] = 2
        manifest.pop("strings", None)
        with open(self.store.manifest_path, "w", encoding="utf-8") as target:
            json.dump(manifest, target)
        recovered = FlashPointStore(self.path)
        self.assertEqual(len(recovered.sequences("line-a")[0]), 1)

    def test_torn_tail_is_truncated_and_journal_order_can_be_replayed(self):
        self.append(1)
        self.store.sync()
        segment = self.store._segment_path(1)
        valid_size = os.path.getsize(segment)
        with open(segment, "ab") as target:
            target.write(b'{"schema":1,"record":"vertex"')
        recovered = FlashPointStore(self.path)
        self.assertEqual(os.path.getsize(segment), valid_size)
        self.assertTrue(recovered.append(2, "line-a", [8.1, 50, 100],
                                         {"record_order": 2}))
        recovered.sync()
        self.assertEqual(len(FlashPointStore(self.path).offsets["line-a"]), 2)

    def test_remove_and_verified_rewrite_do_not_resurrect_vertices(self):
        self.append(1)
        self.append(2)
        self.append(3, "line-b")
        self.store.remove_last(4, "line-a")
        self.store.rewrite(["line-a"])
        coordinates, _ = self.store.sequences("line-a")
        self.assertEqual(len(coordinates), 1)
        self.assertNotIn("line-b", self.store.offsets)
        self.assertFalse(os.path.exists(self.store.manifest_path + ".tmp"))
        self.assertFalse(os.path.exists(self.store.manifest_path + ".previous"))

    def test_rewrite_of_unchanged_active_lines_does_not_rescan_store(self):
        self.append(1); self.append(2); self.store.sync()
        with mock.patch.object(self.store, "_scan_generation") as scan:
            self.store.rewrite(["line-a"])
        scan.assert_not_called()
        self.assertEqual(len(self.store.sequences("line-a")[0]), 2)

    def test_rotates_compact_segments_and_switches_generation(self):
        with mock.patch("pointstore.POINT_SEGMENT_BYTES", 240):
            for order in range(1, 20): self.append(order)
            self.store.sync()
            self.assertGreater(len(self.store.segments), 1)
            old_generation = self.store.generation
            self.store.rewrite(["line-a"])
            self.assertEqual(self.store.generation, old_generation)
            self.assertEqual(len(self.store.sequences("line-a")[0]), 19)
            with open(self.store._segment_path(1), encoding="utf-8") as source:
                self.assertTrue(source.readline().startswith("[2,"))

    def test_rotation_is_rejected_before_writing_when_reserve_is_low(self):
        self.store.reserve_check = lambda: False
        self.append(1)
        with mock.patch("pointstore.POINT_SEGMENT_BYTES", self.store._writer_bytes):
            with self.assertRaisesRegex(OSError, "flash reserve"):
                self.append(2)
        self.assertEqual(len(self.store.sequences("line-a")[0]), 1)

    def test_migrates_legacy_monolith_after_verified_segment_scan(self):
        self.store.close(); self.store.remove_all()
        with open(self.path, "w", encoding="utf-8") as target:
            target.write(json.dumps({"schema":1,"record":"vertex","order":1,
                "line_id":"legacy","coordinate":[8,50,100],
                "measurement":{"record_order":1}})+"\n")
        migrated=FlashPointStore(self.path)
        self.assertEqual(len(migrated.sequences("legacy")[0]),1)
        self.assertFalse(os.path.exists(self.path))
        self.assertTrue(os.path.exists(migrated.manifest_path))


if __name__ == "__main__":
    unittest.main()
