# SPDX-License-Identifier: AGPL-3.0-only
"""Gate profile IDs are the same on every platform and survive a restore."""
import copy
import hashlib
import json
import os
import tempfile
import unittest
from unittest import mock

import support  # noqa: F401  (stubs the MicroPython modules)
import tracking

FIX = {"lat": 50.0, "lon": 8.0, "alt": 123.4, "qual": 4,
       "lat_e8": 5000000000, "lon_e8": 800000000, "alt_mm": 123400,
       "geoid_sep_mm": 45600,
       "fix_status_text": "RTK_FIXED", "sats": 24, "hdop": 0.6,
       "correction_age_sec": 0.5, "station_id": "0054",
       "receiver_accuracy": {"source": "NMEA_GST", "semi_major_sigma_m": 0.02,
                             "altitude_sigma_m": 0.04}}

GATE = {"required_fix": "RTK_FIXED", "min_satellites": 6, "max_hdop": 2.0,
        "max_correction_age_sec": 5.0, "max_horizontal_spread_m": 0.05,
        "max_gst_horizontal_sigma_m": 0.05, "max_gst_vertical_sigma_m": 0.1,
        "quality_window_samples": 5}
# What V15.9.0 derived on a host (sorted keys); these IDs are stored in the wild
# and must not change.
GATE_ID = "profile-10b6e4a6"
SMALL = {"max_hdop": 2.0, "min_satellites": 6}
SMALL_ID = "profile-59c22622"
DEVICE_ID = "profile-27b2c12d"  # the same limits, hashed in a device's key order


def _hash_ordered(value):
    """Rebuild a dict with its keys in a scrambled order (as a hash table would)."""
    if isinstance(value, dict):
        keys = sorted(value, key=lambda key: hashlib.md5(key.encode()).hexdigest())
        return {key: _hash_ordered(value[key]) for key in keys}
    return value


class MicroPythonUjson:
    """ujson as MicroPython has it: no sort_keys, keys in a table's own order."""

    @staticmethod
    def dumps(value, **options):
        if options:
            raise TypeError("unexpected keyword argument")
        return json.dumps(_hash_ordered(value))

    loads = staticmethod(json.loads)


class TestProfileIdIsCanonical(unittest.TestCase):
    def test_insertion_order_does_not_change_the_id(self):
        shuffled = dict(reversed(list(GATE.items())))
        self.assertEqual(tracking._profile_id(GATE), GATE_ID)
        self.assertEqual(tracking._profile_id(shuffled), GATE_ID)
        self.assertEqual(tracking._profile_id(SMALL), SMALL_ID)
        self.assertEqual(tracking._profile_id(dict(reversed(list(SMALL.items())))), SMALL_ID)

    def test_a_ujson_without_sort_keys_and_in_hash_order_gives_the_same_id(self):
        with support.patch_module_global("tracking", "ujson", MicroPythonUjson):
            # The stub really scrambles: a plain dumps of the gate is not sorted.
            self.assertNotEqual(MicroPythonUjson.dumps(GATE),
                                json.dumps(GATE, sort_keys=True))
            self.assertEqual(tracking._profile_id(GATE), GATE_ID)
            self.assertEqual(tracking._profile_id(dict(reversed(list(GATE.items())))),
                             GATE_ID)
            self.assertEqual(tracking._profile_id(SMALL), SMALL_ID)

    def test_nested_objects_are_ordered_too(self):
        with support.patch_module_global("tracking", "ujson", MicroPythonUjson):
            first = tracking._profile_id({"a": {"y": 1, "x": 2}, "b": [1, 2]})
            second = tracking._profile_id({"b": [1, 2], "a": {"x": 2, "y": 1}})
        self.assertEqual(first, second)

    def test_validity_accepts_canonical_and_legacy_shaped_ids_only(self):
        self.assertTrue(tracking._profile_id_valid(SMALL_ID, SMALL))
        self.assertTrue(tracking._profile_id_valid(DEVICE_ID, SMALL))
        for bad in ("profile-foreign", "profile-1", "profile-27B2C12D", "profile-27b2c12",
                    "other-27b2c12d", None, 5):
            self.assertFalse(tracking._profile_id_valid(bad, SMALL), bad)


class TestLegacyProfileIdsSurvive(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(tracking.Tracker, "storage_capacity", return_value={
            "free_flash_bytes": 8_000_000, "required_flash_reserve_bytes": 3_000_000,
            "estimated_temporary_bytes": 16_384, "writable": True})
        patch.start(); self.addCleanup(patch.stop)
        self.tmp = tempfile.TemporaryDirectory(prefix="esp-rtk-profile-")
        self.addCleanup(self.tmp.cleanup)

    def tracker(self, name):
        return tracking.Tracker(os.path.join(self.tmp.name, name))

    def recorded(self):
        tracker = self.tracker("source.jsonl")
        tracker.create_project("Ids")
        tracker.start_line("L", 1, fix=FIX)
        tracker.add_vertex(FIX)
        tracker.add_asset("tap", "T", "", FIX, link_to_active_line=False)
        return tracker

    @staticmethod
    def relabel(payload, old, new):
        """The backup as another platform wrote it: the same limits under another ID."""
        payload = copy.deepcopy(payload)
        for profile in payload["profiles"]:
            if profile["id"] == old:
                profile["id"] = new
        for project in payload["projects"]:
            measurements = [m for line in project["lines"] for m in line["measurements"]]
            measurements += [p["measurement"] for p in project["points"]]
            for measurement in measurements:
                if measurement.get("profile_id") == old:
                    measurement["profile_id"] = new
        return payload

    @staticmethod
    def profile_ids(payload):
        project = payload["projects"][0]
        measurements = [m for line in project["lines"] for m in line["measurements"]]
        measurements += [p["measurement"] for p in project["points"]]
        return sorted(set(m["profile_id"] for m in measurements))

    def test_json_restore_keeps_the_id_a_backup_was_written_with(self):
        source = self.recorded()
        canonical = self.profile_ids(source.backup())
        self.assertEqual(len(canonical), 1)
        device = self.relabel(source.backup(), canonical[0], DEVICE_ID)
        target = self.tracker("target.jsonl")
        target.restore(device)
        self.assertEqual(sorted(target.profiles), [DEVICE_ID])
        self.assertEqual(self.profile_ids(target.backup()), [DEVICE_ID])

    def test_ndjson_restore_keeps_the_id_and_the_second_backup_matches(self):
        source = self.recorded()
        canonical = self.profile_ids(source.backup())[0]
        rows = [json.loads(line) for line in source.iter_backup_ndjson()]
        rows = [row for row in rows if row["record"] != "integrity"]
        text = json.dumps(rows).replace(canonical, DEVICE_ID)
        lines = [json.dumps(row) + "\n" for row in json.loads(text)]
        digest = hashlib.sha256()
        for line in lines:
            digest.update(line.encode("utf-8"))
        lines.append(json.dumps({"record": "integrity", "algorithm": "sha256",
                                 "content_sha256": digest.hexdigest(),
                                 "records": len(lines)}) + "\n")
        target = self.tracker("target.jsonl")
        target.restore_ndjson(lines)
        self.assertEqual(sorted(target.profiles), [DEVICE_ID])
        again = [json.loads(line) for line in target.iter_backup_ndjson()]
        self.assertEqual(
            sorted(set(row["measurement"]["profile_id"] for row in again
                       if row["record"] == "vertex")), [DEVICE_ID])
        self.assertEqual(again[0]["profiles"][0]["id"], DEVICE_ID)
        self.assertEqual(len(again[0]["profiles"]), 1)

    def test_new_measurements_reuse_the_stored_profile_of_the_same_limits(self):
        source = self.recorded()
        canonical = self.profile_ids(source.backup())[0]
        target = self.tracker("target.jsonl")
        target.restore(self.relabel(source.backup(), canonical, DEVICE_ID))
        target.add_vertex(FIX)
        self.assertEqual(sorted(target.profiles), [DEVICE_ID])
        self.assertEqual(self.profile_ids(target.backup()), [DEVICE_ID])

    def test_a_restore_drops_the_cached_profile_of_the_earlier_measurements(self):
        target = self.recorded()   # its last-profile cache holds the canonical ID
        canonical = self.profile_ids(target.backup())[0]
        target.restore(self.relabel(target.backup(), canonical, DEVICE_ID))
        target.add_vertex(FIX)
        self.assertEqual(sorted(target.profiles), [DEVICE_ID])
        self.assertEqual(self.profile_ids(target.backup()), [DEVICE_ID])

    def test_a_reload_keeps_the_stored_legacy_id(self):
        source = self.recorded()
        canonical = self.profile_ids(source.backup())[0]
        target = self.tracker("target.jsonl")
        target.restore(self.relabel(source.backup(), canonical, DEVICE_ID))
        target.add_vertex(FIX)
        reloaded = self.tracker("target.jsonl")
        self.assertEqual(sorted(reloaded.profiles), [DEVICE_ID])
        self.assertEqual(self.profile_ids(reloaded.backup()), [DEVICE_ID])

    def test_a_journal_with_a_legacy_profile_event_replays(self):
        tracker = self.tracker("journal.jsonl")
        tracker.create_project("J")
        tracker._apply({"event": "profile_registered", "order": 99, "profile": {
            "id": DEVICE_ID, "version": 1, "limits": SMALL}})
        self.assertIn(DEVICE_ID, tracker.profiles)
        with self.assertRaisesRegex(ValueError, "profile ID"):
            tracker._apply({"event": "profile_registered", "order": 100, "profile": {
                "id": "profile-foreign", "version": 1, "limits": SMALL}})

    def test_the_backup_docstring_does_not_promise_byte_identity(self):
        doc = tracking.Tracker.iter_backup_ndjson.__doc__
        self.assertIn("content-identical", doc)
        self.assertNotIn("round trip is byte-identical", doc)


class TestMeasurementSamplesStayOutsideTheGate(unittest.TestCase):
    def test_method_changes_neither_gate_profile_deviations_nor_limits(self):
        saved = dict(tracking.CONFIG)
        self.addCleanup(lambda: (tracking.CONFIG.clear(), tracking.CONFIG.update(saved)))
        self.assertNotIn("measurement_samples", tracking.MEASUREMENT_GATE_KEYS)

        def view():
            gate = tracking.measurement_gate()
            return (gate, tracking._profile_id(gate), tracking.measurement_gate_deviations(),
                    tracking.quality_check(FIX)["limits"])
        before = view()
        for value in (1, 10, "quality"):
            tracking.CONFIG["measurement_samples"] = value
            self.assertEqual(view(), before, value)
        self.assertNotIn("samples", before[0])
        self.assertNotIn("samples", before[3])


if __name__ == "__main__":
    unittest.main()
