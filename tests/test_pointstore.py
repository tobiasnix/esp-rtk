# SPDX-License-Identifier: AGPL-3.0-only
import os
import json
import tempfile
import unittest
import asyncio
from unittest import mock

from support import state  # noqa: F401 - MicroPython compatibility
import pointstore
from pointstore import FlashPointStore, _compact, _expand

MEASUREMENT = {"record_order": 1, "source": "automatic", "precision": "e8"}


class TestFlashPointStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="esp-rtk-pointstore-")
        self.path = os.path.join(self.tmp.name, "points.jsonl")
        self.store = FlashPointStore(self.path, cache_size=3)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def append(self, order, line="line-a"):
        self.store.append(order, line, [800000000 + order * 100000, 5000000000, 100000],
                          {"record_order": order, "source": "automatic"})

    def raw_store(self, text, schema=3, strings=None, generation=7):
        """Replace the store with segment text written by older firmware."""
        self.store.close(); self.store.remove_all()
        with open(self.store._segment_path(1, generation), "w", encoding="utf-8") as target:
            target.write(text)
        with open(self.store.manifest_path, "w", encoding="utf-8") as target:
            json.dump({"kind": "point_store", "schema": schema, "generation": generation,
                       "segments": [1], "strings": strings or []}, target)
        self.store = FlashPointStore(self.path, cache_size=3)
        return self.store

    def segment_lines(self, store):
        lines = []
        for number in store.segments:
            with open(store._segment_path(number), encoding="utf-8") as source:
                lines.extend(line.replace(" ", "") for line in source)
        return lines

    def manifest(self, store):
        with open(store.manifest_path, encoding="utf-8") as source:
            return json.load(source)

    def test_code_tables_are_append_only(self):
        self.assertEqual(len(pointstore._KEYS), 42)
        self.assertEqual(pointstore._KEYS[34], "accepted_fix_samples")
        self.assertEqual(pointstore._KEYS[35:], ("precision", "geoid_sep_mm",
            "alt_ellipsoid_mm", "reference_frame", "reference_frame_epoch",
            "ntrip_mount", "antenna_height_mm"))
        self.assertEqual(len(pointstore._STRINGS), 13)
        self.assertEqual(pointstore._STRINGS[9], "rejected")
        self.assertEqual(pointstore._STRINGS[10:], ("e8", "legacy_float32", "WGS84/ITRF"))
        self.assertEqual(pointstore.POINT_STORE_SCHEMA, 4)

    def test_new_records_use_marker_three_and_exact_integers(self):
        store = self.store
        store.append(1, "line-a", [800000000, 5000000000, 100000], MEASUREMENT)
        store.remove_last(2, "line-a")
        store.sync()
        raw = self.segment_lines(store)
        self.assertTrue(raw[0].startswith("[3,1,"))
        self.assertIn("[800000000,5000000000,100000]", raw[0])
        # "e8" is a positional string code, not literal text.
        self.assertNotIn('"e8"', raw[0])
        self.assertEqual(raw[1], '[3,2,"line-a"]\n')
        self.assertEqual(self.manifest(store)["schema"], 4)
        store.append(3, "line-a", [-65432100, 5000000000, -2500], MEASUREMENT)
        store.close()
        cold = FlashPointStore(self.path)
        value = cold.read(cold.offsets["line-a"][0])
        self.assertEqual(value["coordinate"], [-65432100, 5000000000, -2500])
        self.assertEqual(value["measurement"], MEASUREMENT)
        self.assertTrue(all(type(item) is int for item in value["coordinate"]))
        cold.close()

    def test_unconverted_coordinates_are_never_written_as_exact(self):
        self.append(1)
        self.store.sync()
        before = self.segment_lines(self.store)
        for coordinate in ([8.0, 50.0, 100.0], [800000000, 5000000000, 100.0],
                           [800000000, 5000000000], [800000000, 5000000000, 1, 2],
                           [True, 5000000000, 100000], [800000000, "5000000000", 100000],
                           None, "800000000,5000000000,100000"):
            with self.assertRaisesRegex(ValueError, "must be exact integers"):
                self.store.append(2, "line-a", coordinate, MEASUREMENT)
        self.store.sync()
        self.assertEqual(self.store.max_order, 1)
        self.assertEqual(len(self.store.offsets["line-a"]), 1)
        self.assertEqual(self.segment_lines(self.store), before)
        self.assertTrue(self.store.append(2, "line-a", (800000000, 5000000000, 100000), MEASUREMENT))

    def test_legacy_marker_two_records_decode_to_tagged_integers(self):
        store = self.raw_store('[2,1,"line-a",[8.0,50.0,123.4],[-1,0,841057764,1,4]]\n')
        value = store.read(store.offsets["line-a"][0])
        self.assertEqual(value["coordinate"], [800000000, 5000000000, 123400])
        self.assertTrue(all(type(item) is int for item in value["coordinate"]))
        self.assertEqual(value["measurement"], {"recorded_at": 841057764, "fix_quality": 4,
                                                "precision": "legacy_float32"})
        projected = list(store.iter_line("line-a", include_measurement=False))
        self.assertEqual(projected[0]["coordinate"], [800000000, 5000000000, 123400])
        self.assertIsNone(projected[0]["measurement"])
        # A legacy tag already present is not overwritten, an e8-sized marker-2
        # coordinate is a legacy record like any other and fails conversion.
        self.assertEqual(store._decode([2, 1, "a", [8.0, 50.0, 1.0], [-1, 35, {"$": 10}]])
                         ["measurement"]["precision"], "e8")
        with self.assertRaises(ValueError):
            store._decode([2, 1, "a", [800000000, 5000000000, 100000], [-1]])

    BAD_MIDDLE = ('[2,1,"line-a",[8.0,50.0,100.0],[-1,12,1]]\n'
                  '[2,2,"line-a",[8.00001,50.0,120100.0],[-1,12,2]]\n'
                  '[2,3,"line-a",[8.00002,50.0,100.0],[-1,12,3]]\n')

    def test_unconvertible_legacy_vertex_is_skipped_without_truncation(self):
        store = self.raw_store(self.BAD_MIDDLE)
        segment = store._segment_path(1)
        self.assertEqual(os.path.getsize(segment), len(self.BAD_MIDDLE.encode()))
        self.assertEqual([item["coordinate"] for item in store.iter_line("line-a")],
                         [[800000000, 5000000000, 100000], [800002000, 5000000000, 100000]])
        self.assertEqual(store.legacy_skipped, {"line-a": [2]})
        self.assertEqual(store.max_order, 3)
        self.assertEqual(store.valid_bytes, len(self.BAD_MIDDLE.encode()))
        self.assertEqual(store.segment_lines, {1: {"line-a"}})
        # The decode error says which record failed; structure errors stay plain.
        with self.assertRaises(pointstore.LegacyConversionError) as caught:
            store._decode(json.loads(self.BAD_MIDDLE.splitlines()[1]))
        self.assertEqual((caught.exception.order, caught.exception.line_id), (2, "line-a"))
        self.assertIsInstance(caught.exception, ValueError)
        # The cooperative boot scan does the same, and a new vertex appends.
        cold = FlashPointStore(self.path, autoload=False)
        asyncio.run(cold.load_async())
        self.assertEqual(len(cold.offsets["line-a"]), 2)
        self.assertEqual(cold.legacy_skipped, {"line-a": [2]})
        self.assertEqual(os.path.getsize(segment), len(self.BAD_MIDDLE.encode()))
        cold.append(4, "line-a", [800003000, 5000000000, 100000], MEASUREMENT)
        cold.close()
        again = FlashPointStore(self.path)
        self.assertEqual(len(again.offsets["line-a"]), 3)
        self.assertEqual(again.legacy_skipped, {"line-a": [2]})
        again.close()

    def test_history_check_counts_a_skipped_vertex_like_a_stored_one(self):
        # Checkpoint at order 3 expects 3 vertices; an undo at order 4 follows.
        store = self.raw_store(self.BAD_MIDDLE + '[2,4,"line-a"]\n')
        self.assertEqual(len(store.offsets["line-a"]), 1)
        self.assertTrue(store.checkpoint_counts_match({"line-a": 3}, 3))
        self.assertFalse(store.checkpoint_counts_match({"line-a": 4}, 3))

    def test_strict_scan_matches_the_boot_scan_on_a_skipped_vertex(self):
        # A verification probe compares against the store, which skipped the
        # same vertex: it must agree instead of failing every compaction.
        store = self.raw_store(self.BAD_MIDDLE)
        probe = FlashPointStore(self.path, autoload=False)
        asyncio.run(probe.load_async(verified_store=store))
        self.assertEqual(probe.legacy_skipped, {"line-a": [2]})
        self.assertEqual((probe.max_order, probe.valid_bytes),
                         (store.max_order, store.valid_bytes))
        store.accept_verification(probe)
        probe.close()

    def test_structural_damage_after_a_skipped_vertex_still_truncates(self):
        store = self.raw_store(self.BAD_MIDDLE + '[2,4,"line-a",')
        self.assertEqual(os.path.getsize(store._segment_path(1)),
                         len(self.BAD_MIDDLE.encode()))
        self.assertEqual(len(store.offsets["line-a"]), 2)

    def test_tombstone_after_a_skipped_vertex_removes_the_last_kept_one(self):
        # Documented imprecision: the undo was meant for the skipped vertex,
        # but only kept vertices are indexed, so the last kept one goes.
        store = self.raw_store(self.BAD_MIDDLE.splitlines(keepends=True)[0] +
                               self.BAD_MIDDLE.splitlines(keepends=True)[1] +
                               '[2,3,"line-a"]\n')
        self.assertEqual(store.offsets["line-a"], pointstore._offsets())
        self.assertEqual(store.legacy_skipped, {"line-a": [2]})

    def test_schema1_migration_skips_only_the_unconvertible_vertex(self):
        self.store.close(); self.store.remove_all()
        with open(self.path, "w", encoding="utf-8") as target:
            for order, altitude in ((1, 100), (2, 120100.0), (3, 101)):
                target.write(json.dumps({"schema": 1, "record": "vertex", "order": order,
                    "line_id": "legacy", "coordinate": [8, 50, altitude],
                    "measurement": {"record_order": order}}) + "\n")
        self.store = migrated = FlashPointStore(self.path)
        self.assertEqual([item["coordinate"][2] for item in migrated.iter_line("legacy")],
                         [100000, 101000])
        self.assertEqual(migrated.legacy_skipped, {"legacy": [2]})
        self.assertEqual(migrated.max_order, 3)

    def test_legacy_tombstones_and_upgraded_tail_read_together(self):
        store = self.raw_store('[2,1,"line-a",[8.0,50.0,100.0],[-1,12,1]]\n'
                               '[2,2,"line-a",[8.1,50.0,100.0],[-1,12,2]]\n'
                               '[2,3,"line-a"]\n')
        self.assertEqual(len(store.offsets["line-a"]), 1)
        self.assertEqual(store.max_order, 3)
        self.assertTrue(store.checkpoint_counts_match({"line-a": 2}, 2))
        store.append(4, "line-a", [810000000, 5000000000, 100000], MEASUREMENT)
        store.close()
        raw = self.segment_lines(store)
        self.assertEqual([line[:2] for line in raw], ["[2", "[2", "[2", "[3"])
        cold = FlashPointStore(self.path)
        values = list(cold.iter_line("line-a"))
        self.assertEqual([item["coordinate"] for item in values],
                         [[800000000, 5000000000, 100000], [810000000, 5000000000, 100000]])
        self.assertEqual([item["measurement"]["precision"] for item in values],
                         ["legacy_float32", "e8"])
        cold.close()

    def test_schema1_dict_records_decode_to_tagged_integers(self):
        self.store.close(); self.store.remove_all()
        with open(self.path, "w", encoding="utf-8") as target:
            for value in ({"schema": 1, "record": "vertex", "order": 1, "line_id": "legacy",
                           "coordinate": [8, 50, 100],
                           "measurement": {"record_order": 1, "station_id": "SYNTH-STATION"}},
                          {"schema": 1, "record": "vertex", "order": 2, "line_id": "legacy",
                           "coordinate": [8.5, 50.25, -2.5], "measurement": {"record_order": 2}},
                          {"schema": 1, "record": "remove", "order": 3, "line_id": "legacy"}):
                target.write(json.dumps(value) + "\n")
        self.store = migrated = FlashPointStore(self.path)
        self.assertFalse(os.path.exists(self.path))
        self.assertEqual(len(migrated.offsets["legacy"]), 1)
        value = migrated.read(migrated.offsets["legacy"][0])
        self.assertEqual(value["coordinate"], [800000000, 5000000000, 100000])
        self.assertEqual(value["measurement"], {"record_order": 1, "station_id": "SYNTH-STATION",
                                                "precision": "legacy_float32"})
        raw = self.segment_lines(migrated)
        self.assertEqual(len(raw), 3)
        self.assertTrue(all(line.startswith("[3,") for line in raw))
        self.assertIn("[850000000,5025000000,-2500]", raw[1])
        self.assertEqual(self.manifest(migrated)["strings"], ["SYNTH-STATION"])
        legacy = {"schema": 1, "record": "vertex", "order": 1, "line_id": "a",
                  "coordinate": [8.0, 50.0, 1], "measurement": {"source": "manual"}}
        decoded = migrated._decode(legacy, False)
        self.assertEqual(decoded["coordinate"], [800000000, 5000000000, 1000])
        self.assertIsNone(decoded["measurement"])
        self.assertEqual(legacy["coordinate"], [8.0, 50.0, 1])
        self.assertEqual(legacy["measurement"], {"source": "manual"})

    def test_full_rewrite_keeps_the_legacy_tag(self):
        store = self.raw_store('[2,1,"line-a",[8.0,50.0,100.0],[-1,12,1]]\n'
                               '[2,2,"line-b",[9.0,50.0,100.0],[-1,12,2]]\n'
                               '[2,3,"line-a",[8.1,50.0,-2.5],[-1,12,3]]\n')
        old_generation = store.generation
        store.rewrite(["line-a"])
        self.assertNotEqual(store.generation, old_generation)
        self.assertEqual(self.manifest(store)["schema"], 4)
        raw = self.segment_lines(store)
        self.assertEqual(len(raw), 2)
        self.assertTrue(all(line.startswith("[3,") for line in raw))
        expected = [([800000000, 5000000000, 100000], 1), ([810000000, 5000000000, -2500], 3)]
        for reader in (store, FlashPointStore(self.path)):
            values = [reader.read(locator) for locator in reader.offsets["line-a"]]
            self.assertEqual([(item["coordinate"], item["measurement"]["record_order"])
                              for item in values], expected)
            self.assertTrue(all(item["measurement"]["precision"] == "legacy_float32"
                                for item in values))
            self.assertNotIn("line-b", reader.offsets)
            if reader is not store: reader.close()

    def test_async_rewrite_commits_strings_interned_during_reencoding(self):
        store = self.raw_store('[2,1,"line-a",[8.0,50.0,100.0],[-1,6,"SYNTH-STATION",12,1]]\n'
                               '[2,2,"line-b",[9.0,50.0,100.0],[-1,12,2]]\n')
        self.assertEqual(store.strings, [])
        asyncio.run(store.rewrite_async(["line-a"]))
        self.assertEqual(self.manifest(store)["strings"], ["SYNTH-STATION"])
        raw = self.segment_lines(store)
        self.assertEqual(len(raw), 1)
        self.assertTrue(raw[0].startswith("[3,1,"))
        self.assertNotIn("SYNTH-STATION", raw[0])
        cold = FlashPointStore(self.path)
        self.assertEqual(cold.strings, ["SYNTH-STATION"])
        value = cold.read(cold.offsets["line-a"][0])
        self.assertEqual(value["measurement"], {"station_id": "SYNTH-STATION", "record_order": 1,
                                                "precision": "legacy_float32"})
        self.assertEqual(value["coordinate"], [800000000, 5000000000, 100000])
        cold.close()

    def test_failed_async_rewrite_keeps_manifest_and_string_table(self):
        store = self.raw_store('[2,1,"line-a",[8.0,50.0,100.0],[-1,6,"SYNTH-STATION",12,1]]\n'
                               '[2,2,"line-b",[9.0,50.0,100.0],[-1,12,2]]\n')
        with open(store.manifest_path, "rb") as source: manifest = source.read()
        files = sorted(os.listdir(self.tmp.name))
        with mock.patch.object(store, "_activate_manifest", side_effect=OSError("flash full")):
            with self.assertRaises(OSError):
                asyncio.run(store.rewrite_async(["line-a"]))
        self.assertEqual(store.strings, [])
        self.assertEqual(store._string_codes, {})
        with open(store.manifest_path, "rb") as source: self.assertEqual(source.read(), manifest)
        self.assertEqual(sorted(os.listdir(self.tmp.name)), files)
        self.assertEqual(len(store.offsets["line-a"]), 1)
        cold = FlashPointStore(self.path)
        self.assertEqual(cold.read(cold.offsets["line-b"][0])["coordinate"],
                         [900000000, 5000000000, 100000])
        cold.close()
        asyncio.run(store.rewrite_async(["line-a"]))
        self.assertEqual(self.manifest(store)["strings"], ["SYNTH-STATION"])

    def test_frame_and_mount_strings_are_interned(self):
        first = dict(MEASUREMENT, reference_frame="ETRS89 (synthetic)",
                     reference_frame_epoch="2024.5", ntrip_mount="MOUNT1",
                     geoid_sep_mm=47000, alt_ellipsoid_mm=147000, antenna_height_mm=1800)
        second = dict(first, record_order=2)
        self.store.append(1, "line-a", [800000000, 5000000000, 100000], first)
        self.store.append(2, "line-a", [800000100, 5000000000, 100000], second)
        self.store.close()
        strings = self.manifest(self.store)["strings"]
        self.assertEqual(sorted(strings), ["2024.5", "ETRS89 (synthetic)", "MOUNT1"])
        raw = "".join(self.segment_lines(self.store))
        for text in ("ETRS89", "MOUNT1", "2024.5", "geoid_sep_mm", "antenna_height_mm"):
            self.assertNotIn(text, raw)
        cold = FlashPointStore(self.path)
        values = [cold.read(locator)["measurement"] for locator in cold.offsets["line-a"]]
        self.assertEqual(values, [first, second])
        cold.close()

    def test_schema_three_and_unknown_manifests(self):
        store = self.raw_store('[3,1,"line-a",[800000000,5000000000,100000],[-1,12,1]]\n', schema=3)
        self.assertEqual(len(store.offsets["line-a"]), 1)
        self.assertNotIn("precision", store.read(store.offsets["line-a"][0])["measurement"])
        store = self.raw_store('[3,1,"line-a",[800000000,5000000000,100000],[-1,12,1]]\n', schema=4)
        self.assertEqual(len(store.offsets["line-a"]), 1)
        store = self.raw_store('[3,1,"line-a",[800000000,5000000000,100000],[-1,12,1]]\n', schema=5)
        self.assertEqual(store.offsets, {})

    def assert_relabelled_before_write(self, write):
        store = self.raw_store('[2,1,"line-a",[8.0,50.0,100.0],[-1,12,1]]\n', schema=3)
        segment = store._segment_path(1)
        size = os.path.getsize(segment)
        seen = []
        original = store._activate_manifest

        def activate(generation, segments):
            seen.append(os.path.getsize(segment))
            return original(generation, segments)

        with mock.patch.object(store, "_activate_manifest", side_effect=activate):
            write(store)
        store.sync()
        self.assertEqual(seen, [size])
        self.assertEqual(self.manifest(store)["schema"], 4)
        raw = self.segment_lines(store)
        self.assertEqual(raw[0][:2], "[2")
        return raw[-1]

    def test_legacy_manifest_is_relabelled_before_a_first_tombstone(self):
        raw = self.assert_relabelled_before_write(lambda store: store.remove_last(2, "line-a"))
        self.assertEqual(raw, '[3,2,"line-a"]\n')

    def test_legacy_manifest_is_relabelled_before_a_first_vertex(self):
        raw = self.assert_relabelled_before_write(lambda store: store.append(
            2, "line-a", [810000000, 5000000000, 100000], {"record_order": 2}))
        self.assertTrue(raw.startswith("[3,2,"))

    def test_legacy_manifest_is_unchanged_without_writes(self):
        store = self.raw_store('[2,1,"line-a",[8.0,50.0,100.0],[-1,12,1]]\n', schema=3)
        with open(store.manifest_path, "rb") as source: before = source.read()
        store.read(store.offsets["line-a"][0]); store.close()
        reader = FlashPointStore(self.path); reader.close()
        with open(store.manifest_path, "rb") as source: self.assertEqual(source.read(), before)

    def test_exact_records_with_float_coordinates_are_corrupt(self):
        store = self.raw_store('[3,1,"a",[800000000,5000000000,100000],[-1]]\n'
                               '[3,2,"a",[8.0,50.0,100.0],[-1]]\n', schema=4)
        with self.assertRaisesRegex(ValueError, "exact integers"):
            store._decode([3, 3, "b", [8.0, 50.0, 100.0], [-1]])
        with self.assertRaisesRegex(ValueError, "exact integers"):
            store._decode([3, 3, "b", [8.0, 50.0, 100.0], [-1]], False)
        # The ordinary corruption policy keeps the valid prefix only.
        self.assertEqual(store.max_order, 1)
        self.assertEqual(len(store.offsets["a"]), 1)
        self.assertEqual(len(self.segment_lines(store)), 1)

    def test_async_rewrite_rejects_a_string_table_changed_meanwhile(self):
        store = self.raw_store('[2,1,"line-a",[8.0,50.0,100.0],[-1,6,"SYNTH-STATION",12,1]]\n'
                               '[2,2,"line-b",[9.0,50.0,100.0],[-1,12,2]]\n', schema=4)
        generation = store.generation
        changed = [False]

        async def mutate(_delay):
            if not changed[0]:
                changed[0] = True
                store.append(3, "line-c", [900000000, 5100000000, 100000],
                             {"record_order": 3, "station_id": "OTHER-STATION"})
                store.close()
            await asyncio.sleep(0)

        with mock.patch.object(state.asyncio, "sleep_ms", mutate):
            with self.assertRaisesRegex(ValueError, "strings changed during rewrite"):
                asyncio.run(store.rewrite_async(["line-a"]))
        self.assertTrue(changed[0])
        self.assertEqual(store.generation, generation)
        self.assertEqual(store.strings, ["OTHER-STATION"])
        self.assertEqual(self.manifest(store)["generation"], generation)
        self.assertEqual(self.manifest(store)["strings"], ["OTHER-STATION"])
        self.assertFalse([name for name in os.listdir(self.tmp.name)
                          if ".g%08x-" % ((generation + 1) & 0xffffffff) in name])
        cold = FlashPointStore(self.path)
        self.assertEqual(cold.read(cold.offsets["line-a"][0])["measurement"]["station_id"],
                         "SYNTH-STATION")
        self.assertEqual(cold.read(cold.offsets["line-c"][0])["measurement"]["station_id"],
                         "OTHER-STATION")
        cold.close()

    def test_keeps_only_offsets_and_a_bounded_expansion_cache(self):
        for order in range(1, 101):
            self.append(order)
        coordinates, measurements = self.store.sequences("line-a")
        self.assertEqual(len(coordinates), 100)
        self.assertEqual(measurements[99]["record_order"], 100)
        for index in range(20):
            self.assertEqual(coordinates[index][1], 5000000000)
        self.assertLessEqual(len(self.store._cache), 3)
        self.assertFalse(any(isinstance(value, dict)
                             for value in self.store.offsets["line-a"]))
        self.assertGreater(self.store.profile_us["encode"], 0)
        self.assertGreaterEqual(self.store.profile_us["write"], 0)

    def test_sequential_reads_accumulate_read_and_decode_time(self):
        for order in range(1, 11):
            self.append(order)
        self.store.sync()
        written = {key: self.store.profile_us[key] for key in ("encode", "ensure_writer", "write")}
        ticks = [0]

        def ticks_us():
            ticks[0] += 10
            return ticks[0]

        with mock.patch.object(pointstore, "_ticks_us", side_effect=ticks_us):
            self.assertEqual(len(list(self.store.iter_line("line-a"))), 10)
        self.assertGreater(self.store.profile_us["read"], 0)
        self.assertGreater(self.store.profile_us["decode"], 0)
        self.assertEqual({key: self.store.profile_us[key] for key in written}, written)

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
        self.assertTrue(recovered.append(2, "line-a", [810000000, 5000000000, 100000],
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
                self.assertTrue(source.readline().startswith("[3,"))

    STALE = json.dumps([3, 1, "line-ghost", [800000000, 5000000000, 1000], [-1]]) + "\n"

    def test_empty_store_with_a_generation_registers_its_first_segment(self):
        # An asset-only restore installs a manifest generation without segments.
        self.store.close(); self.store.remove_all()
        self.store = FlashPointStore(self.path, autoload=False)
        self.store.generation = 5
        self.append(1); self.append(2); self.store.close()
        self.assertEqual(self.manifest(self.store)["segments"], [1])
        self.assertEqual(self.manifest(self.store)["generation"], 5)
        reloaded = FlashPointStore(self.path)
        self.assertEqual(len(reloaded.sequences("line-a")[0]), 2)
        reloaded.close()

    def test_fresh_first_segment_truncates_a_stale_file_of_the_same_name(self):
        self.store.close(); self.store.remove_all()
        self.store = FlashPointStore(self.path)
        with mock.patch.object(pointstore.time, "time", return_value=42):
            with open(self.store._segment_path(1, 42), "w", encoding="utf-8") as target:
                target.write(self.STALE)
            self.append(5); self.append(6)
        self.assertEqual(self.store.generation, 42)
        self.assertEqual([v["order"] for v in self.store.iter_line("line-a")], [5, 6])
        self.store.close()
        reloaded = FlashPointStore(self.path)
        self.assertNotIn("line-ghost", reloaded.offsets)
        self.assertEqual([v["order"] for v in reloaded.iter_line("line-a")], [5, 6])
        reloaded.close()

    def test_new_segment_is_removed_recreated_and_flushed_before_the_manifest_names_it(self):
        # LittleFS commits a "w" truncation of an open file only at sync or
        # close. The stale file must be gone and the new one empty on flash
        # before the manifest switch, for the first segment and on rotation.
        events = []
        real_remove, real_open = os.remove, open

        class Recorder:
            def __init__(self, handle, path): self.handle, self.path = handle, path
            def write(self, data): return self.handle.write(data)
            def flush(self): events.append(("flush", self.path)); self.handle.flush()
            def close(self): self.handle.close()
            def __enter__(self): return self
            def __exit__(self, *args): self.handle.close()
            def __getattr__(self, name): return getattr(self.handle, name)

        def recording_open(path, mode="r", *args, **kwargs):
            handle = real_open(path, mode, *args, **kwargs)
            if path.endswith(".pjs") and "w" in mode:
                events.append(("open_w", path)); return Recorder(handle, path)
            return handle

        def recording_remove(path):
            if path.endswith(".pjs"): events.append(("remove", path))
            return real_remove(path)

        for rotation in (False, True):
            with self.subTest(rotation=rotation):
                events[:] = []
                self.store.close(); self.store.remove_all()
                self.store = FlashPointStore(self.path, autoload=False)
                self.store.generation = 9
                real_activate = self.store._activate_manifest
                with mock.patch("pointstore.POINT_SEGMENT_BYTES", 240):
                    if rotation: self.append(1)
                    target = self.store._segment_path(2 if rotation else 1, 9)
                    with real_open(target, "w", encoding="utf-8") as stale:
                        stale.write(self.STALE)
                    events[:] = []

                    def checked_activate(generation, segments):
                        if target.endswith("-%04d.pjs" % segments[-1]):
                            events.append(("activate", os.path.getsize(target)))
                        return real_activate(generation, segments)

                    with mock.patch.object(self.store, "_activate_manifest", checked_activate), \
                            mock.patch("pointstore.open", recording_open, create=True), \
                            mock.patch.object(pointstore.os, "remove", recording_remove):
                        for order in range(2 if rotation else 1, 7): self.append(order)
                started = [event for event in events if event[0] != "flush" or event[1] == target]
                self.assertEqual(started[:4], [("remove", target), ("open_w", target),
                                               ("flush", target), ("activate", 0)])
                self.store.close()
                reloaded = FlashPointStore(self.path)
                self.assertNotIn("line-ghost", reloaded.offsets)
                reloaded.close()

    def test_rotation_truncates_a_stale_file_of_the_next_segment(self):
        with mock.patch("pointstore.POINT_SEGMENT_BYTES", 240):
            self.append(1)
            with open(self.store._segment_path(2), "w", encoding="utf-8") as target:
                target.write(self.STALE)
            for order in range(2, 6): self.append(order)
            self.assertGreater(len(self.store.segments), 1)
            self.store.close()
            reloaded = FlashPointStore(self.path)
            self.assertNotIn("line-ghost", reloaded.offsets)
            self.assertEqual([v["order"] for v in reloaded.iter_line("line-a")], [1, 2, 3, 4, 5])
            reloaded.close()

    def test_failed_rotation_manifest_retries_without_an_unregistered_segment(self):
        with mock.patch("pointstore.POINT_SEGMENT_BYTES", 240):
            self.append(1)
            with mock.patch.object(self.store, "_activate_manifest",
                                   side_effect=OSError("flash")):
                with self.assertRaises(OSError):
                    for order in range(2, 6): self.append(order)
            for order in range(6, 10): self.append(order)
            kept = [v["order"] for v in self.store.iter_line("line-a")]
            self.store.close()
            reloaded = FlashPointStore(self.path)
            self.assertEqual([v["order"] for v in reloaded.iter_line("line-a")], kept)
            self.assertEqual(kept[-4:], [6, 7, 8, 9])
            reloaded.close()

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
