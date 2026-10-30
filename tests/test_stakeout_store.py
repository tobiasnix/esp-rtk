# SPDX-License-Identifier: AGPL-3.0-only
"""Stakeout target store: import package validation, stored-set check, IDs,
set files with checked recovery and the project fingerprint."""
import ast
import asyncio
import builtins
import contextlib
import copy
import hashlib
import inspect
import json
import os
import shutil
import tempfile
import types
import unittest
from unittest import mock

import support  # noqa: F401  (installs the MicroPython stubs)
import geo
import stakeout
from cfg_validation import has_control_characters

TESTS = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(TESTS, "fixtures")
REPO = os.path.dirname(TESTS)
SAMPLE_GEOJSON = os.path.join(FIXTURES, "stakeout_sample.geojson")
SAMPLE_PACKAGE = os.path.join(FIXTURES, "stakeout_sample_package.json")
SAMPLE_SET_ID = "so-3a4739b9"
TARGET_KEYS = {"tid", "name", "part_id", "seq", "coord_index", "lat_e8",
               "lon_e8", "alt_import", "props"}
PART_KEYS = {"part_id", "kind", "name", "feature_index", "geometry_index",
             "coords_e8", "coords_total", "coords_thinned"}
SOURCE_KEYS = {"filename", "bytes", "features", "skipped", "skipped_total"}


def run(coro):
    return asyncio.run(coro)


def compact(pkg):
    """The package bytes as the browser sends them (JSON.stringify)."""
    return json.dumps(pkg, separators=(",", ":")).encode()


def sample_package(project_id="project-00000001-0001"):
    with open(SAMPLE_PACKAGE, encoding="utf-8") as fh:
        pkg = json.load(fh)
    pkg["project_id"] = project_id
    return pkg


def make_package(targets=1, lines=0, coords_per_line=0, offset=0, props=0,
                 prop_value="", project_id="project-00000001-0001"):
    """A synthetic import package of a given shape.

    One multipoint part carries ``targets`` targets, then ``lines`` unthinned
    line parts with ``coords_per_line`` vertices each carry one target on
    their first vertex. Different ``offset`` values give different set IDs.
    """
    parts = []
    rows = []
    names = iter(range(1, targets + lines + 1))

    def target(part_index, coord_index, lat_e8, lon_e8):
        rows.append({"part_index": part_index, "coord_index": coord_index,
                     "name": "Target %03d" % next(names),
                     "lat_e8": lat_e8, "lon_e8": lon_e8, "alt_import": None,
                     "props": {"k%d" % j: prop_value for j in range(props)}})

    if targets:
        parts.append({"kind": "multipoint", "name": "Group", "feature_index": 0,
                      "geometry_index": 0, "coords_total": targets,
                      "coords_thinned": False, "coords_e8": []})
        for i in range(targets):
            target(0, i, 5100000000 + offset + 1000 * i, 1000000000)
    for line in range(lines):
        lat_e8 = 5101000000 + offset + 100000 * line
        coords = [[1000000000 + 1000 * j, lat_e8, None]
                  for j in range(coords_per_line)]
        parts.append({"kind": "line", "name": "Line %d" % (line + 1),
                      "feature_index": len(parts), "geometry_index": 0,
                      "coords_total": coords_per_line, "coords_thinned": False,
                      "coords_e8": coords})
        if coords:
            target(len(parts) - 1, 0, coords[0][1], coords[0][0])
    return {"schema": 1, "project_id": project_id,
            "source": {"filename": "generated.geojson", "bytes": 0,
                       "features": len(parts), "skipped": [], "skipped_total": 0},
            "parts": parts, "targets": rows}


# Device packages for the import profile (G4): P128 is 120 680 bytes compact
# (at most 128 KiB, the import limit), P256 256 180 bytes (at most 256 KiB;
# the import refuses it, only validate_import takes it directly).
P256_PROP_VALUE_LENGTH = 26
P128 = make_package(targets=496, lines=4, coords_per_line=500)
P256 = make_package(targets=496, lines=4, coords_per_line=500, props=8,
                    prop_value="v" * P256_PROP_VALUE_LENGTH)


def set_doc(plan, project_created_at=0, imported_at=0):
    """The stored set document built from a validation plan."""
    doc = {"kind": "stakeout_set", "schema": stakeout.SCHEMA,
           "project_id": plan["project_id"],
           "project_created_at": project_created_at,
           "set_id": plan["set_id"], "imported_at": imported_at,
           "source": plan["source"], "parts": plan["parts"],
           "targets": plan["targets"]}
    return json.loads(json.dumps(doc))


def canonical_set_id(pkg):
    rows = sorted((t["part_index"], t["coord_index"], t["lat_e8"], t["lon_e8"])
                  for t in pkg["targets"])
    text = "".join("%d,%d,%d,%d\n" % row for row in rows)
    return "so-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def geojson_positions(geometry, geometry_index):
    """Positions of one geometry part (MultiLineString: one of its lines)."""
    kind, coordinates = geometry["type"], geometry["coordinates"]
    if kind == "Point":
        return [coordinates]
    if kind == "MultiLineString":
        return coordinates[geometry_index]
    return coordinates


def position_e8(position):
    alt = geo.parse_decimal_fixed(position[2], 3) if len(position) > 2 else None
    return [geo.parse_decimal_fixed(position[0], 8),
            geo.parse_decimal_fixed(position[1], 8), alt]


def _set(path, value):
    """A mutation that assigns value at a key path of the package."""
    def mutate(pkg):
        node = pkg
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value
    return mutate


def _delete(path):
    def mutate(pkg):
        node = pkg
        for key in path[:-1]:
            node = node[key]
        del node[path[-1]]
    return mutate


def _many_props(pkg):
    pkg["targets"][0]["props"] = {"k%d" % j: "v" for j in range(9)}


def _copy_first_target(pkg):
    pkg["targets"][1] = copy.deepcopy(pkg["targets"][0])


def _many_skipped(pkg):
    entry = pkg["source"]["skipped"][0]
    pkg["source"]["skipped"] = [dict(entry, feature_index=i) for i in range(51)]
    pkg["source"]["features"] = 60
    pkg["source"]["skipped_total"] = 51


REJECTIONS = [
    ("payload is a list", lambda pkg: [pkg], "invalid_stakeout_import", ""),
    ("schema 2", _set(["schema"], 2), "invalid_stakeout_import", "schema"),
    ("schema True", _set(["schema"], True), "invalid_stakeout_import", "schema"),
    ("project_id ../x", _set(["project_id"], "../x"), "invalid_stakeout_import", "project_id"),
    ("project_id empty", _set(["project_id"], ""), "invalid_stakeout_import", "project_id"),
    ("project_id too long", _set(["project_id"], "a" * 65), "invalid_stakeout_import", "project_id"),
    ("project_id dot", _set(["project_id"], "p.1"), "invalid_stakeout_import", "project_id"),
    ("project_id null", _set(["project_id"], None), "invalid_stakeout_import", "project_id"),
    ("lat float", _set(["targets", 0, "lat_e8"], 5100000000.0),
     "invalid_stakeout_import", "targets[0].lat_e8"),
    ("lat bool", _set(["targets", 0, "lat_e8"], True),
     "invalid_stakeout_import", "targets[0].lat_e8"),
    ("lat out of range", _set(["targets", 4, "lat_e8"], 9000000001),
     "coordinate_out_of_range", "targets[4].lat_e8"),
    ("lon out of range", _set(["targets", 4, "lon_e8"], -18000000001),
     "coordinate_out_of_range", "targets[4].lon_e8"),
    ("alt float", _set(["targets", 4, "alt_import"], 412.3),
     "invalid_stakeout_import", "targets[4].alt_import"),
    ("alt bool", _set(["targets", 4, "alt_import"], True),
     "invalid_stakeout_import", "targets[4].alt_import"),
    ("alt out of range", _set(["targets", 4, "alt_import"], 100000001),
     "coordinate_out_of_range", "targets[4].alt_import"),
    ("lat missing", _delete(["targets", 0, "lat_e8"]),
     "invalid_stakeout_import", "targets[0].lat_e8"),
    ("alt missing", _delete(["targets", 4, "alt_import"]),
     "invalid_stakeout_import", "targets[4].alt_import"),
    ("part_index beyond parts", _set(["targets", 0, "part_index"], 5),
     "invalid_stakeout_import", "targets[0].part_index"),
    ("coord_index beyond coords_total", _set(["targets", 3, "coord_index"], 4),
     "invalid_stakeout_import", "targets[3].coord_index"),
    ("multipoint coord_index beyond coords_total", _set(["targets", 6, "coord_index"], 2),
     "invalid_stakeout_import", "targets[6].coord_index"),
    ("duplicate target", _copy_first_target, "invalid_stakeout_import", "targets[1]"),
    ("target off its unthinned line", _set(["targets", 1, "lat_e8"], 5100008990),
     "invalid_stakeout_import", "targets[1].lat_e8"),
    ("target longitude off its unthinned line", _set(["targets", 1, "lon_e8"], 1000000001),
     "invalid_stakeout_import", "targets[1].lon_e8"),
    ("target altitude off its unthinned line", _set(["targets", 1, "alt_import"], 0),
     "invalid_stakeout_import", "targets[1].alt_import"),
    ("name too long", _set(["targets", 0, "name"], "x" * 81),
     "invalid_stakeout_import", "targets[0].name"),
    ("name with newline", _set(["targets", 0, "name"], "a\nb"),
     "invalid_stakeout_import", "targets[0].name"),
    ("name blank", _set(["targets", 0, "name"], "  "),
     "invalid_stakeout_import", "targets[0].name"),
    ("name null", _set(["targets", 0, "name"], None),
     "invalid_stakeout_import", "targets[0].name"),
    ("nine props", _many_props, "invalid_stakeout_import", "targets[0].props"),
    ("prop key too long", _set(["targets", 0, "props"], {"k" * 41: "v"}),
     "invalid_stakeout_import", "targets[0].props"),
    ("props not an object", _set(["targets", 0, "props"], []),
     "invalid_stakeout_import", "targets[0].props"),
    ("prop value a number", _set(["targets", 0, "props"], {"material": 5}),
     "invalid_stakeout_import", "targets[0].props.material"),
    ("prop value too long", _set(["targets", 0, "props"], {"material": "x" * 81}),
     "invalid_stakeout_import", "targets[0].props.material"),
    ("part kind polygon", _set(["parts", 0, "kind"], "polygon"),
     "invalid_stakeout_import", "parts[0].kind"),
    ("part name empty", _set(["parts", 0, "name"], ""),
     "invalid_stakeout_import", "parts[0].name"),
    ("point with two coordinates", _set(["parts", 1, "coords_total"], 2),
     "invalid_stakeout_import", "parts[1].coords_total"),
    ("point with stored coordinates", _set(["parts", 1, "coords_e8"], [[999999943, 5100000027, 412300]]),
     "invalid_stakeout_import", "parts[1].coords_e8"),
    ("line thinned flag wrong", _set(["parts", 0, "coords_thinned"], True),
     "invalid_stakeout_import", "parts[0].coords_thinned"),
    ("line coordinate without altitude", _set(["parts", 0, "coords_e8", 1], [1000000000, 5100008989]),
     "invalid_stakeout_import", "parts[0].coords_e8[1]"),
    ("line coordinate out of range", _set(["parts", 0, "coords_e8", 0, 1], 9000000001),
     "coordinate_out_of_range", "parts[0].coords_e8[0][1]"),
    ("line coordinate float", _set(["parts", 0, "coords_e8", 0, 0], 1000000000.0),
     "invalid_stakeout_import", "parts[0].coords_e8[0][0]"),
    ("line with one stored coordinate", _set(["parts", 3, "coords_e8"], [[1000100000, 5100100000, None]]),
     "invalid_stakeout_import", "parts[3].coords_e8"),
    ("part without targets", _delete(["targets", 4]), "invalid_stakeout_import", "parts[1]"),
    ("no parts", _set(["parts"], []), "invalid_stakeout_import", "parts"),
    ("skip reason unknown", _set(["source", "skipped", 0, "reason"], "other"),
     "invalid_stakeout_import", "source.skipped[0].reason"),
    ("skip type empty", _set(["source", "skipped", 0, "type"], ""),
     "invalid_stakeout_import", "source.skipped[0].type"),
    ("51 skipped features", _many_skipped, "invalid_stakeout_import", "source.skipped"),
    ("skipped_total below the list", _set(["source", "skipped_total"], 0),
     "invalid_stakeout_import", "source.skipped_total"),
    ("source too large", _set(["source", "bytes"], 4194305),
     "invalid_stakeout_import", "source.bytes"),
    ("source missing", _delete(["source"]), "invalid_stakeout_import", "source"),
    ("no targets", _set(["targets"], []), "invalid_stakeout_import", "targets"),
]


class TestValidateImport(unittest.TestCase):
    def test_package_fixture_matches_the_geojson_fixture(self):
        with open(SAMPLE_GEOJSON, encoding="utf-8") as fh:
            geojson = json.load(fh, parse_float=str, parse_int=str)
        pkg = sample_package()
        features = geojson["features"]
        for part_index, part in enumerate(pkg["parts"]):
            with self.subTest(part=part["name"]):
                geometry = features[part["feature_index"]]["geometry"]
                positions = [position_e8(position) for position in
                             geojson_positions(geometry, part["geometry_index"])]
                self.assertEqual(part["coords_total"], len(positions))
                if part["kind"] == "line":
                    self.assertEqual(part["coords_e8"], positions)
                targets = [t for t in pkg["targets"] if t["part_index"] == part_index]
                self.assertTrue(targets)
                for target in targets:
                    lon_e8, lat_e8, alt = positions[target["coord_index"]]
                    self.assertEqual((target["lat_e8"], target["lon_e8"], target["alt_import"]),
                                     (lat_e8, lon_e8, alt))
        self.assertEqual(features[4]["geometry"]["type"], "Polygon")
        self.assertEqual(pkg["source"]["skipped"][0]["feature_index"], 4)
        self.assertEqual(pkg["source"]["bytes"], os.path.getsize(SAMPLE_GEOJSON))
        self.assertEqual(pkg["source"]["features"], 5)
        self.assertEqual(len(features), 5)

    def test_sample_yields_twelve_targets_in_source_order(self):
        pkg = sample_package()
        plan = run(stakeout.validate_import(pkg))
        self.assertEqual(plan["set_id"], SAMPLE_SET_ID)
        self.assertEqual(plan["project_id"], "project-00000001-0001")
        self.assertEqual([t["tid"] for t in plan["targets"]], [
            "so-3a4739b9/1/1", "so-3a4739b9/1/2", "so-3a4739b9/1/3", "so-3a4739b9/1/4",
            "so-3a4739b9/2/1", "so-3a4739b9/3/1", "so-3a4739b9/3/2", "so-3a4739b9/4/1",
            "so-3a4739b9/4/2", "so-3a4739b9/5/1", "so-3a4739b9/5/2", "so-3a4739b9/5/3"])
        self.assertEqual([t["seq"] for t in plan["targets"][:4]], [1, 2, 3, 4])
        self.assertEqual([p["part_id"] for p in plan["parts"]],
                         ["so-3a4739b9/%d" % n for n in range(1, 6)])
        # A MultiLineString keeps its lines apart: own parts, own vertices.
        self.assertEqual(plan["parts"][3]["coords_e8"], pkg["parts"][3]["coords_e8"])
        self.assertEqual(plan["parts"][4]["coords_e8"], pkg["parts"][4]["coords_e8"])
        self.assertEqual([t["part_id"] for t in plan["targets"][7:]],
                         ["so-3a4739b9/4"] * 2 + ["so-3a4739b9/5"] * 3)
        self.assertEqual([t["seq"] for t in plan["targets"][7:]], [1, 2, 1, 2, 3])
        first, manhole = plan["targets"][0], plan["targets"][5]
        self.assertEqual((first["lat_e8"], first["lon_e8"]),
                         (manhole["lat_e8"], manhole["lon_e8"]))
        self.assertNotEqual(first["tid"], manhole["tid"])
        self.assertEqual(plan["targets"][4]["alt_import"], 412300)
        self.assertIs(type(plan["targets"][4]["alt_import"]), int)
        self.assertEqual(plan["targets"][0]["props"], {"material": "PE"})
        self.assertEqual(plan["source"]["skipped"], pkg["source"]["skipped"])
        self.assertEqual(plan["source"]["skipped_total"], 1)

    def test_set_id_is_the_sha256_of_the_canonical_rows(self):
        pkg = sample_package()
        self.assertEqual(canonical_set_id(pkg), SAMPLE_SET_ID)
        self.assertEqual(run(stakeout.validate_import(pkg))["set_id"], canonical_set_id(pkg))

        renamed = sample_package(project_id="project-00000002-0001")
        for target in renamed["targets"]:
            target["name"] = "Renamed"
            target["props"] = {"note": "changed"}
        for part in renamed["parts"]:
            part["name"] = "Renamed"
        renamed["source"].update(filename="other.geojson", bytes=17, features=9,
                                 skipped_total=4)
        self.assertEqual(run(stakeout.validate_import(renamed))["set_id"], SAMPLE_SET_ID)

        moved = sample_package()
        moved["targets"][0]["lat_e8"] += 1
        moved["parts"][0]["coords_e8"][0][1] += 1
        moved_id = run(stakeout.validate_import(moved))["set_id"]
        self.assertNotEqual(moved_id, SAMPLE_SET_ID)
        self.assertEqual(moved_id, canonical_set_id(moved))

        self.assertEqual(stakeout.set_id_for(
            [(t["part_index"], t["coord_index"], t["lat_e8"], t["lon_e8"])
             for t in reversed(pkg["targets"])]), SAMPLE_SET_ID)
        self.assertNotEqual(run(stakeout.validate_import(make_package(offset=0)))["set_id"],
                            run(stakeout.validate_import(make_package(offset=1)))["set_id"])

    def test_duplicate_names_are_kept(self):
        pkg = sample_package()
        pkg["targets"][5]["name"] = "Line A / P001"
        plan = run(stakeout.validate_import(pkg))
        self.assertEqual(plan["targets"][0]["name"], "Line A / P001")
        self.assertEqual(plan["targets"][5]["name"], "Line A / P001")
        self.assertEqual(len({t["tid"] for t in plan["targets"]}), 12)

    def test_unknown_keys_are_not_stored(self):
        pkg = sample_package()
        pkg["extra"] = 1
        pkg["source"]["extra"] = 1
        pkg["source"]["skipped"][0]["extra"] = 1
        for item in pkg["parts"] + pkg["targets"]:
            item["extra"] = 1
        plan = run(stakeout.validate_import(pkg))
        self.assertEqual(set(plan), {"project_id", "set_id", "source", "parts", "targets"})
        self.assertEqual(set(plan["source"]), SOURCE_KEYS)
        self.assertEqual(set(plan["source"]["skipped"][0]), {"feature_index", "type", "reason"})
        for part in plan["parts"]:
            self.assertEqual(set(part), PART_KEYS)
        for target in plan["targets"]:
            self.assertEqual(set(target), TARGET_KEYS)

    def test_text_is_stored_stripped(self):
        pkg = sample_package()
        pkg["targets"][0]["name"] = "  Line A / P001 "
        pkg["targets"][0]["props"] = {" material ": " PE ", "empty": "", "none": None}
        target = run(stakeout.validate_import(pkg))["targets"][0]
        self.assertEqual(target["name"], "Line A / P001")
        self.assertEqual(target["props"], {"material": "PE", "empty": "", "none": None})

    def test_rejections_name_the_path(self):
        for label, mutate, code, path in REJECTIONS:
            with self.subTest(label):
                pkg = sample_package()
                payload = mutate(pkg)
                if payload is None:
                    payload = pkg
                with self.assertRaises(stakeout.StakeoutError) as caught:
                    run(stakeout.validate_import(payload))
                error = caught.exception
                self.assertEqual((error.code, error.path), (code, path))
                self.assertEqual(error.status, "400 Bad Request")
                self.assertEqual(error.payload()["path"], path)
                self.assertTrue(error.message)

    def test_count_limits_are_too_many_targets(self):
        def rejection(pkg):
            with self.assertRaises(stakeout.StakeoutError) as caught:
                run(stakeout.validate_import(pkg))
            return caught.exception.code, caught.exception.path

        self.assertEqual(rejection(make_package(targets=501)), ("too_many_targets", "targets"))
        points = make_package(targets=0)
        points["parts"] = [{"kind": "point", "name": "Point %d" % i, "feature_index": i,
                            "geometry_index": 0, "coords_total": 1,
                            "coords_thinned": False, "coords_e8": []} for i in range(501)]
        points["targets"] = [{"part_index": i, "coord_index": 0, "name": "Target %03d" % i,
                              "lat_e8": 5100000000 + i, "lon_e8": 1000000000,
                              "alt_import": None, "props": {}} for i in range(501)]
        self.assertEqual(rejection(points), ("too_many_targets", "parts"))
        self.assertEqual(rejection(make_package(targets=1, lines=2, coords_per_line=1001)),
                         ("too_many_targets", "parts"))
        plan = run(stakeout.validate_import(make_package(targets=496, lines=4,
                                                         coords_per_line=500)))
        self.assertEqual(len(plan["targets"]), 500)
        self.assertEqual(sum(len(p["coords_e8"]) for p in plan["parts"]), 2000)

    def test_validation_yields_cooperatively(self):
        # Counts the vertices and targets checked since the last pause; the
        # widest stretch between two pauses (or before the first and after
        # the last) must stay within one yield quantum of each kind.
        pauses = []
        since = {"coords": 0, "targets": 0}
        widest = {"coords": 0, "targets": 0}
        real_coord, real_target = stakeout._coord, stakeout._target

        def counted(kind, real):
            def check(*args):
                since[kind] += 1
                widest[kind] = max(widest[kind], since[kind])
                return real(*args)
            return check

        async def counting_pause(meter):
            pauses.append(dict(since))
            since.update(coords=0, targets=0)

        with mock.patch.object(stakeout, "_pause", counting_pause), \
                mock.patch.object(stakeout, "_coord", counted("coords", real_coord)), \
                mock.patch.object(stakeout, "_target", counted("targets", real_target)):
            run(stakeout.validate_import(P128))
        self.assertLessEqual(widest["coords"], stakeout.YIELD_COORDS)
        self.assertLessEqual(widest["targets"], stakeout.YIELD_TARGETS)
        self.assertGreaterEqual(len(pauses), 500 // 32 + 2000 // 128)
        self.assertEqual(sum(p["coords"] for p in pauses) + since["coords"], 2000)
        self.assertEqual(sum(p["targets"] for p in pauses) + since["targets"], 500)

    def test_no_whole_file_path_remains(self):
        # A stored set is never parsed or read whole on the device: listings
        # read trailers, downloads stream, the writer serializes once.
        for name in ("check_set_doc", "_check_set_doc", "_read_set_doc", "_measure",
                     "_encoded_size", "_set_chunks", "_write_set_file", "_read_digest",
                     "read_set_bytes", "_source", "_check_coord"):
            self.assertFalse(hasattr(stakeout, name), name)
            self.assertFalse(hasattr(stakeout.TargetStore, name), name)
        with open(stakeout.__file__, encoding="utf-8") as fh:
            source = fh.read()
        self.assertNotIn("ujson.load(", source)
        self.assertNotIn(".read()", source)

    def test_device_packages_have_the_planned_sizes(self):
        self.assertLessEqual(110000, len(compact(P128)))
        self.assertLessEqual(len(compact(P128)), 131072)
        self.assertLessEqual(240000, len(compact(P256)))
        self.assertLessEqual(len(compact(P256)), 262144)
        plan = run(stakeout.validate_import(P128))
        self.assertEqual(len(plan["targets"]), 500)


class TestIdsAndErrors(unittest.TestCase):
    def test_project_ids(self):
        for value in ("project-00000001-0001", "A_b-9", "a" * 64):
            self.assertIs(stakeout.valid_project_id(value), True, value)
        for value in ("", "a" * 65, "../x", "p.1", "a/b", "a b", "\u00e4", None, 5, ["a"]):
            self.assertIs(stakeout.valid_project_id(value), False, value)

    def test_set_ids(self):
        for value in ("so-3a4739b9", "so-00000000"):
            self.assertIs(stakeout.valid_set_id(value), True, value)
        for value in ("so-3A4739B9", "so-3a4739b", "so-3a4739b90", "xx-3a4739b9",
                      "so-3a4739bg", "", None, 5):
            self.assertIs(stakeout.valid_set_id(value), False, value)

    def test_error_payload_names_path_and_set_only_when_given(self):
        plain = stakeout.StakeoutError("too_many_sets", "Too many sets.", "409 Conflict")
        self.assertEqual(plain.payload(), {"error": "too_many_sets", "message": "Too many sets."})
        self.assertEqual(plain.status, "409 Conflict")
        self.assertIsInstance(plain, ValueError)
        full = stakeout.StakeoutError("already_imported", "Imported.", "409 Conflict",
                                      path="", set_id="so-3a4739b9")
        self.assertEqual(full.payload(), {"error": "already_imported", "message": "Imported.",
                                          "path": "", "set_id": "so-3a4739b9"})

    def test_now_seconds_counts_from_2000(self):
        with mock.patch.object(stakeout.time, "time", return_value=946684800 + 42.9):
            self.assertEqual(stakeout.now_seconds(), 42)

    def test_module_imports_no_application_layers(self):
        with open(os.path.join(REPO, "stakeout.py"), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module)
        self.assertFalse(imported & {"tracking", "web", "cfg_store"})


PID = "project-00000001-0001"
FP = 700000000
_real_open = builtins.open
_real_rename = os.rename
_real_stat = os.stat
_real_load = json.load  # what the ujson stub calls


def _pad(text, length):
    return text + "x" * (length - len(text)) if length > len(text) else text


def wide_package(name_len, coords):
    """500 line parts with one target each: the worst case for the file size.

    Names are padded with "x" to name_len characters; every target carries
    eight short properties. Key order as in the import contract.
    """
    parts, targets = [], []
    for i in range(500):
        coords_e8 = [[1000000000 + 1000 * c, 5100000000 + 100000 * i, None]
                     for c in range(coords)]
        parts.append({"kind": "line", "name": _pad("Line %03d" % i, name_len),
                      "feature_index": i, "geometry_index": 0, "coords_total": coords,
                      "coords_e8": coords_e8, "coords_thinned": False})
        targets.append({"part_index": i, "coord_index": 0,
                        "name": _pad("Line %03d / P001" % i, name_len),
                        "lat_e8": coords_e8[0][1], "lon_e8": coords_e8[0][0],
                        "alt_import": None,
                        "props": {"p%d" % j: "v%d" % j for j in range(8)}})
    return {"schema": 1, "project_id": PID,
            "source": {"filename": "wide.geojson", "bytes": 1000000, "features": 500,
                       "skipped": [], "skipped_total": 0},
            "parts": parts, "targets": targets}


def _failing_open(predicate, error):
    """builtins.open that raises error for the paths and modes predicate selects."""
    def fake_open(path, mode="r", *args, **kwargs):
        if predicate(str(path), mode):
            raise error
        return _real_open(path, mode, *args, **kwargs)
    return fake_open


class _MemoryLowFile:
    """A file whose second write raises MemoryError."""

    def __init__(self, handle):
        self.handle = handle
        self.writes = 0

    def write(self, data):
        self.writes += 1
        if self.writes > 1:
            raise MemoryError
        return self.handle.write(data)

    def __getattr__(self, name):
        return getattr(self.handle, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.handle.close()


class _HeadWriter:
    """Records what a download writes; the head is kept apart from the file."""

    def __init__(self, fail_at=None, error=None):
        self.head = None
        self.pieces = []
        self.drains = 0
        self.fail_at = fail_at
        self.error = error

    def write(self, data):
        if self.head is None:
            self.head = bytes(data)
            return
        if len(self.pieces) + 1 == self.fail_at:
            raise self.error
        self.pieces.append(bytes(data))

    async def drain(self):
        self.drains += 1

    @property
    def body(self):
        return b"".join(self.pieces)


def _head(size):
    return b"HEAD %d\n" % size


class _StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="stakeout-store-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "stakeout")
        self.store = stakeout.TargetStore(self.root)
        self.check = lambda pid: FP
        self.ok = lambda n: True
        logger = mock.patch.object(stakeout, "log")
        self.log = logger.start()
        self.addCleanup(logger.stop)

    # --- helpers ---------------------------------------------------------

    def add(self, pkg=None, store=None, check=None, capacity=None, body=None, **kwargs):
        if body is None:
            body = compact(pkg or sample_package(PID))
        return run((store or self.store).import_package(
            body, check or self.check, capacity or self.ok, **kwargs))

    def refused(self, code, call, *args, **kwargs):
        with self.assertRaises(stakeout.StakeoutError) as caught:
            result = call(*args, **kwargs)
            if asyncio.iscoroutine(result):
                run(result)
        self.assertEqual(caught.exception.code, code, caught.exception.message)
        return caught.exception

    def refused_import(self, code, **kwargs):
        return self.refused(code, self.add, **kwargs)

    def sets(self, store=None, fingerprint=FP, project_id=PID):
        return run((store or self.store).sets(project_id, fingerprint))

    def delete(self, set_id=SAMPLE_SET_ID, store=None, fingerprint=FP, project_id=PID,
               **kwargs):
        return run((store or self.store).delete_set(project_id, fingerprint, set_id, 0,
                                                    **kwargs))

    def download(self, set_id=SAMPLE_SET_ID, store=None, fingerprint=FP, project_id=PID,
                 writer=None):
        """The bytes a download sends after its head."""
        writer = writer or _HeadWriter()
        run((store or self.store).send_set(writer, project_id, fingerprint, set_id, _head))
        self.assertEqual(writer.head, _head(len(writer.body)))
        return writer.body

    def path(self, set_id=SAMPLE_SET_ID, project_id=PID):
        return os.path.join(self.root, project_id, set_id + ".json")

    def name(self, set_id=SAMPLE_SET_ID, project_id=PID):
        return "%s/%s.json" % (project_id, set_id)

    def files(self):
        """Every file under the store root, as <project_id>/<name>."""
        found = []
        for folder, _dirs, names in os.walk(self.root):
            for name in names:
                found.append(os.path.relpath(os.path.join(folder, name),
                                             self.root).replace(os.sep, "/"))
        return sorted(found)

    def snapshot(self):
        result = {}
        for name in self.files():
            with _real_open(os.path.join(self.root, name), "rb") as fh:
                result[name] = fh.read()
        return result

    def write(self, path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with _real_open(path, "wb") as fh:
            fh.write(data if isinstance(data, bytes) else json.dumps(data).encode())

    def read(self, path):
        with _real_open(path, "rb") as fh:
            return fh.read()

    def fresh(self):
        return stakeout.TargetStore(self.root)

    def stored_sample(self):
        """The bytes of the sample set as the store writes them."""
        self.add()
        data = self.read(self.path())
        os.remove(self.path())
        self.store.invalidate(PID)
        return data

    def assert_no_set(self):
        self.assertEqual(self.files(), [])
        self.assertIs(self.store.busy, False)
        self.assertEqual(self.sets(self.fresh()), [])

    def states(self, store=None):
        return {s["set_id"]: s["state"] for s in self.sets(store or self.fresh())}

    @contextlib.contextmanager
    def immutable_patches(self):
        """Fails the test if anything renames, removes or makes a file or folder."""
        with contextlib.ExitStack() as stack:
            for name in ("rename", "remove", "rmdir", "mkdir"):
                stack.enter_context(mock.patch.object(
                    stakeout.os, name, side_effect=AssertionError("os." + name)))
            yield


class TestTargetStore(_StoreCase):
    # --- writing ---------------------------------------------------------

    def test_a_project_fingerprint_the_trailer_cannot_hold_is_refused_up_front(self):
        # A restored survey may carry any created_at; the trailer holds an
        # unsigned 32-bit integer or null.
        for fingerprint in (1.5, -1, 2 ** 32, "x", True):
            with self.subTest(fingerprint=fingerprint), self.immutable_patches():
                error = self.refused_import("unsupported_project_fingerprint",
                                           check=lambda pid: fingerprint)
                self.assertEqual(error.status, "409 Conflict")
                self.assertFalse(os.path.exists(self.root))
                self.assertIs(self.store.busy, False)
        for fingerprint in (845000000, None, 0, 2 ** 32 - 1):
            with self.subTest(fingerprint=fingerprint):
                store = self.fresh()
                self.add(store=store, check=lambda pid: fingerprint)
                self.assertEqual([s["state"] for s in self.sets(self.fresh(), fingerprint)],
                                 ["ok"])
                shutil.rmtree(self.root)

    def test_import_writes_one_file_and_a_new_store_reads_it(self):
        answer = self.add()
        self.assertEqual(answer["set_id"], SAMPLE_SET_ID)
        self.assertEqual(answer["project_id"], PID)
        self.assertEqual((answer["targets"], answer["parts"], answer["coords"]), (12, 5, 9))
        self.assertEqual(answer["skipped_total"], 1)
        self.assertEqual(answer["skipped"], sample_package()["source"]["skipped"])
        self.assertEqual(self.files(), [self.name()])
        self.assertEqual(answer["bytes"], os.path.getsize(self.path()))
        self.assertEqual(set(answer["timing_ms"]), set(stakeout.TIMING_KEYS))
        for key, value in answer["timing_ms"].items():
            self.assertIs(type(value), int, key)
            self.assertGreaterEqual(value, 0, key)
        self.assertEqual(answer["timing_ms"]["size"], 0)

        store = self.fresh()
        listed = self.sets(store)
        self.assertEqual(listed, self.sets())
        data = self.download(store=store)
        self.assertEqual(data, self.read(self.path()))
        doc = json.loads(data)
        self.assertEqual(listed, [{
            "set_id": SAMPLE_SET_ID, "state": "ok", "imported_at": doc["imported_at"],
            "source": {"filename": "stakeout_sample.geojson", "bytes": 851,
                       "features": 5, "skipped_total": 1},
            "parts": 5, "targets": 12, "coords": 9}])
        self.assertIs(type(doc["imported_at"]), int)
        self.assertEqual(doc["targets"][4]["lat_e8"], 5100000027)
        self.assertIs(type(doc["targets"][4]["lat_e8"]), int)
        self.assertEqual(doc["project_created_at"], FP)
        self.assertEqual(doc["summary"], stakeout._read_trailer(self.path(), PID,
                                                                SAMPLE_SET_ID))
        self.assertEqual(doc["summary"]["body_bytes"] + stakeout.TRAILER_BYTES,
                         answer["bytes"])
        self.assertEqual(store.orphaned(PID, FP), 0)
        self.assertEqual(store.confirmed_count(PID, SAMPLE_SET_ID), 0)

    def test_overview_entries_are_copies(self):
        self.add()
        listed = self.sets()
        listed[0]["state"] = "changed"
        listed[0]["extra"] = 1
        self.assertEqual(self.sets(), self.sets(self.fresh()))

    def test_reimport_is_already_imported_and_leaves_the_file_unchanged(self):
        self.add()
        data, mtime = self.read(self.path()), os.stat(self.path()).st_mtime_ns
        for store in (self.store, self.fresh()):
            error = self.refused_import("already_imported", store=store)
            self.assertEqual(error.status, "409 Conflict")
            self.assertEqual(error.set_id, SAMPLE_SET_ID)
        self.assertEqual(self.read(self.path()), data)
        self.assertEqual(os.stat(self.path()).st_mtime_ns, mtime)
        self.assertEqual(self.files(), [self.name()])

    def test_a_set_file_appearing_before_the_rename_is_never_replaced(self):
        # Sets are immutable and a rename would replace the file: the last
        # check before it (no yield in between) refuses.
        data = self.stored_sample()
        real_rename = stakeout._commit
        written = []

        async def pause(meter):
            if (self.name() + ".tmp" in self.files() and not written
                    and not os.path.exists(self.path())):
                written.append(True)
                self.write(self.path(), b"outside")
            await asyncio.sleep(0)

        with mock.patch.object(stakeout, "_pause", pause), \
                mock.patch.object(stakeout, "_commit", side_effect=real_rename) as commit:
            error = self.refused_import("already_imported")
        self.assertEqual((error.status, error.set_id), ("409 Conflict", SAMPLE_SET_ID))
        self.assertFalse(commit.called)
        self.assertEqual(self.files(), [self.name()])
        self.assertEqual(self.read(self.path()), b"outside")
        self.assertNotEqual(data, b"outside")

    def test_import_over_a_damaged_set_of_the_same_id_is_set_damaged(self):
        self.write(self.path(), {"kind": "wrong"})
        data = self.read(self.path())
        for store in (self.store, self.fresh()):
            error = self.refused_import("set_damaged", store=store)
            self.assertEqual(error.status, "409 Conflict")
            self.assertEqual(error.set_id, SAMPLE_SET_ID)
            self.assertEqual(error.payload()["set_id"], SAMPLE_SET_ID)
            self.assertIn("delete the damaged set first", error.message)
        self.assertEqual(self.read(self.path()), data)
        self.assertEqual(self.files(), [self.name()])
        self.delete()
        self.assertEqual(self.add()["set_id"], SAMPLE_SET_ID)

    def test_fifth_set_is_refused(self):
        imported = []
        for k, at in zip((0, 10, 20, 30), (400, 300, 200, 100)):
            with mock.patch.object(stakeout, "now_seconds", return_value=at):
                imported.append(self.add(make_package(targets=3, offset=k))["set_id"])
        self.assertEqual(len(set(imported)), 4)
        # Overview order: oldest import first.
        self.assertEqual([s["set_id"] for s in self.sets()], list(reversed(imported)))
        self.assertEqual([s["set_id"] for s in self.sets(self.fresh())],
                         list(reversed(imported)))
        error = self.refused_import("too_many_sets", pkg=make_package(targets=3, offset=40))
        self.assertEqual(error.status, "409 Conflict")
        self.assertEqual(len(self.files()), 4)

    def test_reserve_asks_for_twice_the_file_limit_after_making_the_folders(self):
        asked = []

        def capacity(temporary_bytes):
            asked.append((temporary_bytes, os.path.isdir(os.path.join(self.root, PID))))
            return False

        error = self.refused_import("storage_reserve", capacity=capacity)
        self.assertEqual(error.status, "507 Insufficient Storage")
        self.assertEqual(asked, [(2 * 393216 + 32768, True)])
        self.assert_no_set()
        # The folders stay; an empty project folder lists as no sets.
        self.assertTrue(os.path.isdir(os.path.join(self.root, PID)))

    def test_folders_that_cannot_be_made_are_507(self):
        for error in (OSError(28, "no space"), OSError(5, "io")):
            with self.subTest(error=error), \
                    mock.patch.object(stakeout.os, "mkdir", side_effect=error):
                refused = self.refused_import("stakeout_write_failed")
                self.assertEqual(refused.status, "507 Insufficient Storage")
                self.assertFalse(os.path.exists(self.root))
        # An existing folder is no error.
        os.makedirs(os.path.join(self.root, PID))
        self.assertEqual(self.add()["set_id"], SAMPLE_SET_ID)

    def test_the_project_is_checked_again_before_the_folders_are_made(self):
        calls = []
        changed = []

        def check(pid):
            calls.append(pid)
            return FP + 1 if changed else FP

        real_pause = stakeout._pause

        async def pause(meter):
            # The validation's own checks are done after the second call.
            if len(calls) >= 2 and not changed:
                changed.append(True)
            await real_pause(meter)

        with mock.patch.object(stakeout, "_pause", pause), \
                mock.patch.object(stakeout.os, "mkdir", side_effect=AssertionError) as mkdir:
            error = self.refused_import("project_not_found", check=check)
        self.assertEqual(error.status, "404 Not Found")
        self.assertFalse(mkdir.called)
        self.assertFalse(os.path.exists(self.root))

    def test_a_failing_existence_check_before_the_rename_is_507(self):
        final = self.path()
        real_stat = os.stat

        def stat(path, *args, **kwargs):
            if path == final:
                raise OSError(5, "io")
            return real_stat(path, *args, **kwargs)

        with mock.patch.object(stakeout.os, "stat", stat), \
                mock.patch.object(stakeout, "_commit") as commit:
            error = self.refused_import("stakeout_write_failed")
        self.assertEqual(error.status, "507 Insufficient Storage")
        self.assertFalse(commit.called)
        self.assertEqual(self.files(), [])
        self.assertEqual(self.add()["set_id"], SAMPLE_SET_ID)

    def test_answer_bytes_are_the_file_size(self):
        unicode = sample_package("project-unicode")
        for target in unicode["targets"]:
            target["name"] = "Gr\u00f6\u00dfe \u2713 \U0001d11e"
            target["props"] = {"note": "Gr\u00f6\u00dfe \u2713 \U0001d11e"}
        p128 = copy.deepcopy(P128)
        p128["project_id"] = "project-p128"
        for pkg in (sample_package(PID), p128, unicode):
            with self.subTest(pkg["project_id"]):
                answer = self.add(pkg)
                path = self.path(answer["set_id"], pkg["project_id"])
                self.assertEqual(answer["bytes"], os.path.getsize(path))
                summary = stakeout._read_trailer(path, pkg["project_id"], answer["set_id"])
                self.assertEqual(summary["body_bytes"] + stakeout.TRAILER_BYTES,
                                 answer["bytes"])

    def test_wide_packages_have_the_pinned_sizes(self):
        self.assertEqual(len(compact(wide_package(0, 4))), 225450)
        self.assertEqual(len(compact(wide_package(80, 3))), 279450)

    def test_device_package_at_the_import_limit_fits_the_file_limit(self):
        answer = self.add(P128)
        size = os.path.getsize(self.path(answer["set_id"], P128["project_id"]))
        self.assertEqual(answer["bytes"], size)
        self.assertLessEqual(size, stakeout.MAX_SET_FILE_BYTES)

    def _padded(self, pkg, length):
        """compact(pkg) plus JSON whitespace up to exactly length bytes."""
        body = compact(pkg)
        self.assertLessEqual(len(body), length)
        return body + b" " * (length - len(body))

    def test_import_limit_is_131072_bytes(self):
        self.assertEqual(stakeout.MAX_IMPORT_BYTES, 131072)
        self.assertLessEqual(len(compact(P128)), stakeout.MAX_IMPORT_BYTES)
        self.assertGreater(len(compact(P256)), stakeout.MAX_IMPORT_BYTES)

    def test_import_of_exactly_the_limit_is_accepted(self):
        body = self._padded(P128, stakeout.MAX_IMPORT_BYTES)
        self.assertEqual(len(body), 131072)
        answer = self.add(body=body)
        self.assertEqual(answer["targets"], 500)
        self.assertIn(self.name(answer["set_id"], P128["project_id"]), self.files())

    def test_import_over_the_limit_is_refused_without_parsing_or_writing(self):
        too_big = self._padded(P128, stakeout.MAX_IMPORT_BYTES + 1)
        for label, body in (("one byte over", too_big), ("P256", compact(P256)),
                            ("wide", compact(wide_package(0, 4)))):
            with self.subTest(label):
                with mock.patch.object(stakeout.ujson, "loads") as parse, \
                        mock.patch.object(stakeout.gc, "collect") as collect, \
                        mock.patch.object(stakeout, "validate_import") as validate:
                    error = self.refused_import("import_too_large", body=body)
                self.assertEqual(error.status, "413 Payload Too Large")
                self.assertIn(str(stakeout.MAX_IMPORT_BYTES), error.message)
                for untouched in (parse, collect, validate):
                    self.assertFalse(untouched.called)
                self.assert_no_set()

    def test_import_limit_check_comes_after_busy_and_empty(self):
        self.assertEqual(self.refused_import("empty_import", body=b"").status,
                         "400 Bad Request")
        self.store.busy = True
        try:
            self.refused_import("storage_busy", body=compact(P256))
        finally:
            self.store.busy = False

    def test_unicode_heavy_package_never_exceeds_the_file_limit(self):
        def body(targets, name_length, length):
            pkg = make_package(targets=targets, props=8, prop_value="\U0001d11e" * length)
            for target in pkg["targets"]:
                target["name"] = "\u20ac" * name_length
            # The browser sends raw UTF-8 (JSON.stringify).
            return json.dumps(pkg, separators=(",", ":"), ensure_ascii=False).encode()

        # The heaviest properties that still fit the import limit, for many
        # short names and for fewer maximal ones.
        for targets, name_length in ((500, 1), (150, stakeout.MAX_NAME)):
            length = stakeout.MAX_PROP_VALUE
            while length and len(body(targets, name_length, length)) > stakeout.MAX_IMPORT_BYTES:
                length -= 1
            self.assertGreater(length, 0)
            with self.subTest(targets=targets):
                try:
                    answer = self.add(body=body(targets, name_length, length))
                except stakeout.StakeoutError as error:
                    self.assertEqual((error.code, error.path), ("too_many_targets", ""))
                    self.assertEqual(error.status, "400 Bad Request")
                else:
                    size = os.path.getsize(self.path(answer["set_id"]))
                    self.assertLessEqual(size, stakeout.MAX_SET_FILE_BYTES)

        # The limit is checked against the bytes written, exactly at the edge.
        with mock.patch.object(stakeout, "now_seconds", return_value=845000000):
            size = self.add()["bytes"]
            self.delete()
            with mock.patch.object(stakeout, "MAX_SET_FILE_BYTES", size - 1):
                error = self.refused_import("too_many_targets")
            self.assertEqual(error.path, "")
            self.assertNotIn(self.name(), self.files())
            self.assertNotIn(self.name() + ".tmp", self.files())
            with mock.patch.object(stakeout, "MAX_SET_FILE_BYTES", size):
                self.assertEqual(self.add()["bytes"], size)

    def test_write_phase_failures_leave_no_set(self):
        def tmp_write(path, mode):
            return path.endswith(".tmp") and "w" in mode

        def tmp_read(path, mode):
            return path.endswith(".tmp") and "r" in mode

        def wrapped_open(wrapper):
            def fake_open(path, mode="r", *args, **kwargs):
                handle = _real_open(path, mode, *args, **kwargs)
                return wrapper(handle) if tmp_write(str(path), mode) else handle
            return fake_open

        class Corrupting(_MemoryLowFile):
            """Flips one byte of the second piece on its way to the flash."""

            def write(self, data):
                self.writes += 1
                if self.writes == 2:
                    data = bytes([data[0] ^ 1]) + bytes(data[1:])
                return self.handle.write(data)

        class Losing(_MemoryLowFile):
            """Drops the second piece."""

            def write(self, data):
                self.writes += 1
                return len(data) if self.writes == 2 else self.handle.write(data)

        cases = [
            ("open fails", mock.patch("builtins.open",
                                      _failing_open(tmp_write, OSError(28, "no space")))),
            ("write runs out of memory", mock.patch("builtins.open",
                                                    wrapped_open(_MemoryLowFile))),
            ("verification unreadable", mock.patch("builtins.open",
                                                   _failing_open(tmp_read, OSError(5, "io")))),
            ("verification digest differs", mock.patch("builtins.open",
                                                       wrapped_open(Corrupting))),
            ("verification size differs", mock.patch("builtins.open",
                                                     wrapped_open(Losing))),
            ("rename fails", mock.patch.object(stakeout.os, "rename",
                                               side_effect=OSError(5, "io"))),
        ]
        # Three pieces, so a failure can hit the middle of the file.
        pkg = make_package(targets=64)
        for label, patch in cases:
            with self.subTest(label):
                with patch, mock.patch.object(stakeout.gc, "collect") as collect:
                    error = self.refused_import("stakeout_write_failed", pkg=pkg)
                self.assertEqual(error.status, "507 Insufficient Storage")
                if "memory" in label:
                    self.assertTrue(collect.called)
                self.assert_no_set()

    def test_verification_reads_the_file_back_without_parsing_it(self):
        with mock.patch.object(stakeout.ujson, "load", side_effect=AssertionError), \
                mock.patch.object(stakeout, "_read_trailer", side_effect=AssertionError):
            answer = self.add(make_package(targets=64))
        self.assertEqual(self.files(), [self.name(answer["set_id"])])

    def test_memory_error_while_verifying_is_busy_and_leaves_no_set(self):
        with mock.patch.object(stakeout, "_read_back", side_effect=MemoryError):
            error = self.refused_import("stakeout_busy")
        self.assertEqual(error.status, "503 Service Unavailable")
        self.assert_no_set()

    def test_rename_error_after_the_file_moved_counts_as_committed(self):
        def rename_then_fail(source, target):
            _real_rename(source, target)
            raise OSError(5, "io")

        with mock.patch.object(stakeout.os, "rename", rename_then_fail):
            answer = self.add()
        self.assertEqual(answer["set_id"], SAMPLE_SET_ID)
        self.assertEqual(self.files(), [self.name()])
        self.assertEqual(len(self.sets(self.fresh())), 1)

    def test_rename_error_with_the_tmp_still_there_is_not_committed(self):
        def copy_then_fail(source, target):
            self.write(target, self.read(source))
            raise OSError(5, "io")

        with mock.patch.object(stakeout.os, "rename", copy_then_fail):
            error = self.refused_import("stakeout_write_failed")
        self.assertEqual(error.status, "507 Insufficient Storage")
        self.assertNotIn(self.name() + ".tmp", self.files())

    def test_failure_after_commit_is_500_with_set_id(self):
        with mock.patch.object(stakeout.TargetStore, "_remember", side_effect=MemoryError):
            error = self.refused_import("import_committed_response_failed")
        self.assertEqual(error.status, "500 Internal Server Error")
        self.assertEqual(error.set_id, SAMPLE_SET_ID)
        self.assertEqual(error.payload()["set_id"], SAMPLE_SET_ID)
        self.assertEqual(self.files(), [self.name()])
        self.assertIs(self.store.busy, False)
        self.assertEqual([s["set_id"] for s in self.sets()], [SAMPLE_SET_ID])
        self.refused_import("already_imported")

    def test_failed_rename_with_undeletable_tmp_stays_incomplete(self):
        with mock.patch.object(stakeout.os, "rename", side_effect=OSError(5, "io")), \
                mock.patch.object(stakeout.os, "remove", side_effect=OSError(5, "io")):
            self.refused_import("stakeout_write_failed")
        self.assertEqual(self.files(), [self.name() + ".tmp"])
        self.assertTrue(self.log.called)
        staged = self.snapshot()
        # Never promoted: the same store and new ones list it as incomplete.
        for store in (self.store, self.fresh(), self.fresh()):
            self.assertEqual(self.sets(store), [{"set_id": SAMPLE_SET_ID,
                                                 "state": "incomplete"}])
            self.assertEqual(self.snapshot(), staged)
        # An import of the same set replaces it.
        self.assertEqual(self.add()["set_id"], SAMPLE_SET_ID)
        self.assertEqual(self.files(), [self.name()])
        self.assertEqual(self.states(), {SAMPLE_SET_ID: "ok"})

    def test_parse_and_validation_errors(self):
        for body, code, path in ((b"{", "invalid_stakeout_import", ""),
                                 (b"\xff\xfe", "invalid_stakeout_import", ""),
                                 (b"[]", "invalid_stakeout_import", ""),
                                 (b'{"project_id": "../x"}', "invalid_stakeout_import",
                                  "project_id"),
                                 (b"", "empty_import", None)):
            with self.subTest(body):
                error = self.refused_import(code, body=body)
                self.assertEqual(error.status, "400 Bad Request")
                self.assertEqual(error.path, path)
                self.assert_no_set()
        for label, target, name in (("parse", stakeout.ujson, "loads"),
                                    ("validation", stakeout, "validate_import")):
            with self.subTest(label):
                with mock.patch.object(target, name, side_effect=MemoryError), \
                        mock.patch.object(stakeout.gc, "collect") as collect:
                    error = self.refused_import("import_too_large")
                self.assertEqual(error.status, "413 Payload Too Large")
                self.assertTrue(collect.called)
                self.assert_no_set()

    def test_project_check_runs_before_and_after_validation(self):
        calls = []

        def check(project_id):
            calls.append(project_id)
            if len(calls) == 2:
                raise stakeout.StakeoutError("recording_active", "Recording.", "409 Conflict")
            return FP

        self.refused_import("recording_active", pkg=make_package(targets=64), check=check)
        self.assertEqual(calls, [PID, PID])
        self.assert_no_set()

    def test_busy_store_refuses_import_and_delete(self):
        self.add()
        self.store.busy = True
        self.refused_import("storage_busy", pkg=make_package(targets=3))
        error = self.refused("storage_busy", self.store.delete_set, PID, FP, SAMPLE_SET_ID, 0)
        self.assertEqual(error.status, "409 Conflict")
        self.assertEqual(self.files(), [self.name()])

    def test_import_holds_busy_across_validation(self):
        async def scenario():
            gate = asyncio.Event()
            paused = asyncio.Event()

            async def gated_pause(meter):
                paused.set()
                await gate.wait()

            with mock.patch.object(stakeout, "_pause", gated_pause):
                task = asyncio.ensure_future(self.store.import_package(
                    compact(make_package(targets=64)), self.check, self.ok))
                await paused.wait()
                self.assertIs(self.store.busy, True)
                with self.assertRaises(stakeout.StakeoutError) as caught:
                    await self.store.import_package(compact(sample_package(PID)),
                                                    self.check, self.ok)
                self.assertEqual(caught.exception.code, "storage_busy")
                with self.assertRaises(stakeout.StakeoutError) as caught:
                    await self.store.delete_set(PID, FP, SAMPLE_SET_ID, 0)
                self.assertEqual(caught.exception.code, "storage_busy")
                self.assertEqual(self.files(), [])
                gate.set()
                return await task

        answer = run(scenario())
        self.assertIs(self.store.busy, False)
        self.assertEqual(self.files(), [self.name(answer["set_id"])])
        self.assertEqual(self.add()["set_id"], SAMPLE_SET_ID)

    # --- listing errors ----------------------------------------------------

    def _two_sets(self):
        a = self.add()["set_id"]
        b = self.add(make_package(targets=3))["set_id"]
        self.store.invalidate(PID)
        return a, b

    def test_memory_error_while_listing_is_transient(self):
        a, b = self._two_sets()
        before = self.snapshot()
        loads = []
        real_loads = stakeout.ujson.loads

        # The first trailer parse fails, whichever set the listing reads first.
        def loads_once_low(data):
            loads.append(1)
            if len(loads) == 1:
                raise MemoryError
            return real_loads(data)

        with self.immutable_patches(), \
                mock.patch.object(stakeout.ujson, "loads", loads_once_low):
            error = self.refused("stakeout_busy", self.store.sets, PID, FP)
        self.assertEqual(error.status, "503 Service Unavailable")
        self.assertEqual(self.snapshot(), before)
        self.assertNotIn(PID, self.store._cache)
        self.assertEqual(sorted((s["set_id"], s["state"]) for s in self.sets()),
                         sorted([(a, "ok"), (b, "ok")]))

    def test_listing_io_errors_are_507_without_cache(self):
        self._two_sets()
        before = self.snapshot()
        opened = []

        def first_read(path, mode):
            opened.append(path)
            return len(opened) == 1

        stats = []
        real_stat = os.stat

        def first_stat(path, *args, **kwargs):
            if str(path).endswith(".json"):
                stats.append(path)
                if len(stats) == 1:
                    raise OSError(5, "io")
            return real_stat(path, *args, **kwargs)

        cases = [
            ("ilistdir", mock.patch.object(stakeout.os, "ilistdir",
                                           side_effect=OSError(5, "io"))),
            ("open", mock.patch("builtins.open", _failing_open(first_read, OSError(5, "io")))),
            ("stat", mock.patch.object(stakeout.os, "stat", first_stat)),
        ]
        for label, patch in cases:
            with self.subTest(label):
                del opened[:], stats[:]
                with self.immutable_patches(), patch:
                    error = self.refused("stakeout_read_failed", self.store.sets, PID, FP)
                self.assertEqual(error.status, "507 Insufficient Storage")
                self.assertNotIn(PID, self.store._cache)
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(sorted(s["state"] for s in self.sets()), ["ok", "ok"])
                self.store.invalidate(PID)

    # --- project fingerprint -------------------------------------------

    def test_reused_project_id_with_other_created_at_orphans_the_set(self):
        self.add()
        data = self.read(self.path())
        store = self.fresh()
        self.assertEqual(self.sets(store, FP + 1), [])
        self.assertEqual(store.orphaned(PID, FP + 1), 1)
        self.refused("set_not_found", self.download, store=store, fingerprint=FP + 1)
        error = self.refused("set_not_found", store.delete_set, PID, FP + 1, SAMPLE_SET_ID, 0)
        self.assertEqual(error.status, "404 Not Found")
        self.assertEqual(self.read(self.path()), data)
        error = self.refused_import("orphaned_set_conflict", store=store,
                                    check=lambda pid: FP + 1)
        self.assertEqual((error.status, error.set_id), ("409 Conflict", SAMPLE_SET_ID))
        self.assertEqual(self.read(self.path()), data)
        self.assertEqual([s["set_id"] for s in self.sets(store)], [SAMPLE_SET_ID])
        self.assertEqual(store.orphaned(PID, FP), 0)

    def test_same_second_id_reuse_is_not_detected(self):
        # Known limit (D19): a reused project ID with the same created_at
        # sees the old sets.
        self.add()
        self.assertEqual([s["set_id"] for s in self.sets(self.fresh())], [SAMPLE_SET_ID])

    # --- deleting, names -------------------------------------------------

    def test_delete_set_validates_required_fields(self):
        self.add()
        for set_id in (None, 5, "../x"):
            with self.subTest(set_id=set_id):
                error = self.refused("invalid_request", self.store.delete_set, PID, FP,
                                     set_id, 0)
                self.assertEqual(error.status, "400 Bad Request")
        for expected in (None, True, "0", 0.0):
            with self.subTest(expected=expected):
                self.refused("invalid_request", self.store.delete_set, PID, FP,
                             SAMPLE_SET_ID, expected)
        error = self.refused("confirmed_changed", self.store.delete_set, PID, FP,
                             SAMPLE_SET_ID, 1)
        self.assertEqual(error.status, "409 Conflict")
        self.assertEqual(self.files(), [self.name()])
        self.assertEqual(self.delete(), "done")
        self.assertEqual(self.files(), [])
        self.assertEqual(self.sets(), [])
        self.assertEqual(self.sets(self.fresh()), [])
        self.refused("set_not_found", self.store.delete_set, PID, FP, SAMPLE_SET_ID, 0)

    def test_delete_io_error_keeps_the_cache_and_file(self):
        self.add()
        before = self.sets()

        def remove(path):
            if path.endswith(".tmp"):
                raise OSError(2, "missing")
            raise OSError(5, "io")

        with mock.patch.object(stakeout.os, "remove", remove):
            error = self.refused("stakeout_delete_failed", self.store.delete_set, PID, FP,
                                 SAMPLE_SET_ID, 0)
        self.assertEqual(error.status, "507 Insufficient Storage")
        self.assertEqual(error.set_id, SAMPLE_SET_ID)
        self.assertEqual(self.sets(), before)
        self.assertEqual(self.files(), [self.name()])
        self.delete()
        self.assertEqual(self.files(), [])

    def test_a_removal_whose_outcome_cannot_be_told_is_507(self):
        data = self.stored_sample()
        for leftover in ("", ".tmp"):
            with self.subTest(failing=leftover or "file"):
                self.write(self.path(), data)
                self.write(self.path() + ".tmp", data)
                self.store.invalidate(PID)
                before = self.sets()
                failing = self.path() + leftover
                real_remove, real_stat = os.remove, os.stat

                def remove(path):
                    if path == failing:
                        raise OSError(5, "io")
                    real_remove(path)

                def stat(path, *args, **kwargs):
                    if path == failing:
                        raise OSError(5, "io")
                    return real_stat(path, *args, **kwargs)

                with mock.patch.object(stakeout.os, "remove", remove), \
                        mock.patch.object(stakeout.os, "stat", stat):
                    error = self.refused("stakeout_delete_failed", self.store.delete_set,
                                         PID, FP, SAMPLE_SET_ID, 0)
                self.assertEqual(error.status, "507 Insufficient Storage")
                self.assertEqual(error.set_id, SAMPLE_SET_ID)
                self.assertIn(os.path.basename(failing), [os.path.basename(name)
                                                          for name in self.files()])
                # The cache matches the files: listed again, as a new store does.
                self.assertEqual(self.sets(), self.sets(self.fresh()))
                if not leftover:
                    self.assertEqual(self.sets(), before)
                self.delete()
                self.assertEqual(self.files(), [])

    def test_delete_removes_the_file_before_a_tmp_beside_it(self):
        data = self.stored_sample()
        self.write(self.path(), data)
        self.write(self.path() + ".tmp", data[:100])
        removed = []
        real_remove = os.remove

        def remove(path):
            removed.append(os.path.basename(path))
            return real_remove(path)

        with mock.patch.object(stakeout.os, "remove", remove):
            self.delete(store=self.fresh())
        self.assertEqual(removed, [SAMPLE_SET_ID + ".json", SAMPLE_SET_ID + ".json.tmp"])
        self.assertEqual(self.files(), [])

    def test_delete_with_check_project_runs_it_after_the_lookup(self):
        self.add()
        store = self.fresh()
        calls = []

        def check(project_id):
            calls.append(PID in store._cache)
            raise stakeout.StakeoutError("recording_active", "Recording.", "409 Conflict")

        self.refused("recording_active", store.delete_set, PID, FP, SAMPLE_SET_ID, 0,
                     check)
        self.assertEqual(calls, [True])
        self.assertEqual(self.files(), [self.name()])
        self.refused("project_not_found", store.delete_set, PID, FP, SAMPLE_SET_ID, 0,
                     lambda pid: FP + 1)
        self.assertEqual(self.files(), [self.name()])

    def test_invalid_ids_never_reach_the_filesystem(self):
        with mock.patch.object(stakeout.os, "ilistdir", side_effect=AssertionError), \
                mock.patch.object(stakeout.os, "stat", side_effect=AssertionError), \
                mock.patch("builtins.open", side_effect=AssertionError):
            for call, args in ((self.store.set_path, ("../x", SAMPLE_SET_ID)),
                               (self.store.set_path, (PID, "../so")),
                               (self.download, ("so-3A4739B9",)),
                               (self.store.send_set, (None, "../x", FP, SAMPLE_SET_ID,
                                                      _head)),
                               (self.store.sets, ("a/b", FP)),
                               (self.store.orphaned, ("", FP)),
                               (self.store.delete_set, ("a.b", FP, SAMPLE_SET_ID, 0))):
                with self.subTest(args=args):
                    error = self.refused("invalid_request", call, *args)
                    self.assertEqual(error.status, "400 Bad Request")
        self.assertEqual(self.store.set_path(PID, SAMPLE_SET_ID),
                         "%s/%s/%s.json" % (self.root, PID, SAMPLE_SET_ID))

    # --- yields ------------------------------------------------------------

    def _slow_listing_clock(self):
        """Patches a clock that advances 1 ms per reading and 5000 ms per listing.

        Returns the list of listing calls.
        """
        clock = [1000]
        listings = []
        real_load = stakeout.TargetStore._load

        def ticks_ms():
            clock[0] += 1
            return clock[0]

        async def slow_load(store, *args):
            listings.append(args)
            clock[0] += 5000
            return await real_load(store, *args)

        for patch in (mock.patch.object(stakeout.time, "ticks_ms", ticks_ms),
                      mock.patch.object(stakeout.TargetStore, "_load", slow_load)):
            patch.start()
            self.addCleanup(patch.stop)
        return listings

    def test_cold_listing_before_validation_is_a_block_of_its_own(self):
        self.add(make_package(targets=3))
        store = self.fresh()
        listings = self._slow_listing_clock()
        timing = self.add(store=store)["timing_ms"]
        self.assertEqual(len(listings), 1)
        # The parse ends with a yield before the cold listing, and the
        # listing yields before the validation.
        self.assertGreaterEqual(timing["longest_block"], 5000)
        self.assertLess(timing["longest_block"], 5000 + 100)
        self.assertLess(timing["reserve"] + timing["write"] + timing["verify"]
                        + timing["rename"], 5000)
        self.sets(store)
        self.assertEqual((store.last_load_ms, store.last_load_block_ms), (0, 0))

    def test_listing_again_in_add_set_is_a_block_of_its_own(self):
        listings = self._slow_listing_clock()
        real_pause = stakeout._pause
        invalidated = []

        async def invalidating_pause(meter):
            # Once, during the validation: add_set lists again.
            if PID in self.store._cache and not invalidated:
                invalidated.append(True)
                self.store.invalidate(PID)
            await real_pause(meter)

        with mock.patch.object(stakeout, "_pause", invalidating_pause):
            timing = self.add(make_package(targets=64))["timing_ms"]
        self.assertEqual(len(listings), 2)
        written = timing["reserve"] + timing["write"] + timing["verify"] + timing["rename"]
        self.assertLess(written, 5000)
        self.assertGreaterEqual(timing["longest_block"], 5000)
        self.assertLess(timing["longest_block"], 5000 + 100)

    def _stopped_clock(self):
        """Patches a clock that only moves when the test moves it; returns it."""
        clock = [1000]
        patch = mock.patch.object(stakeout.time, "ticks_ms", lambda: clock[0])
        patch.start()
        self.addCleanup(patch.stop)
        return clock

    def test_longest_block_spans_a_validation_without_a_yield(self):
        clock = self._stopped_clock()
        real_validation = stakeout.validate_import

        async def slow_validation(payload, meter):
            clock[0] += 1200
            return await real_validation(payload, meter)

        # The sample package is below both yield thresholds: parse,
        # validation and commit run as one block.
        with mock.patch.object(stakeout, "validate_import", slow_validation):
            timing = self.add()["timing_ms"]
        self.assertEqual(timing["validate"], 1200)
        self.assertGreaterEqual(timing["longest_block"], 1200)

    def test_longest_block_ends_at_each_yield(self):
        clock = self._stopped_clock()
        real_validation = stakeout.validate_import

        async def validation_with_one_yield(payload, meter):
            clock[0] += 700
            await stakeout._pause(meter)
            clock[0] += 700
            return await real_validation(payload, meter)

        with mock.patch.object(stakeout, "validate_import", validation_with_one_yield):
            timing = self.add()["timing_ms"]
        self.assertEqual(timing["validate"], 1400)
        self.assertEqual(timing["longest_block"], 700)

    # --- cooperative commit (F1a/F1b) -----------------------------------

    def _instrumented(self, cost=None):
        """Patches the heavy operations of an import to record themselves.

        Returns (events, capacity): an event is (kind, bytes); kinds are
        parse, listing, serialize, reserve, write, read, rename and pause.
        cost(kind, bytes), if given, runs at each heavy operation (to move
        a patched clock). Pass capacity to the import.
        """
        events = []

        def note(kind, size=0):
            events.append((kind, size))
            if cost is not None:
                cost(kind, size)

        real_loads, real_dumps = stakeout.ujson.loads, stakeout.ujson.dumps
        real_load_listing = stakeout.TargetStore._load
        real_pause = stakeout._pause

        def loads(body):
            note("parse", len(body))
            return real_loads(body)

        def dumps(value):
            text = real_dumps(value)
            note("serialize", len(text.encode("utf-8")))
            return text

        async def listing(store, *args):
            note("listing")
            return await real_load_listing(store, *args)

        def rename(source, target):
            note("rename")
            return _real_rename(source, target)

        async def pause(meter):
            events.append(("pause", 0))
            await real_pause(meter)

        class Recorded:
            def __init__(self, handle):
                self.handle = handle

            def write(self, data):
                note("write", len(data))
                return self.handle.write(data)

            def read(self, *args):
                data = self.handle.read(*args)
                note("read", len(data))
                return data

            def readinto(self, buffer):
                count = self.handle.readinto(buffer)
                note("read", count or 0)
                return count

            def __getattr__(self, name):
                return getattr(self.handle, name)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.handle.close()

        def recorded_open(path, mode="r", *args, **kwargs):
            handle = _real_open(path, mode, *args, **kwargs)
            return Recorded(handle) if str(path).endswith(".tmp") else handle

        def capacity(size):
            note("reserve", size)
            return True

        for patch in (mock.patch.object(stakeout.ujson, "loads", loads),
                      mock.patch.object(stakeout.ujson, "dumps", dumps),
                      mock.patch.object(stakeout.TargetStore, "_load", listing),
                      mock.patch.object(stakeout.os, "rename", rename),
                      mock.patch.object(stakeout, "_pause", pause),
                      mock.patch("builtins.open", recorded_open)):
            patch.start()
            self.addCleanup(patch.stop)
        return events, capacity

    @staticmethod
    def _blocks(events):
        """The heavy operations between two yields, as lists of events."""
        blocks = [[]]
        for event in events:
            if event[0] == "pause":
                blocks.append([])
            else:
                blocks[-1].append(event)
        return [block for block in blocks if block]

    def test_import_yields_between_all_commit_phases(self):
        events, capacity = self._instrumented()
        answer = self.add(P128, capacity=capacity)
        size = answer["bytes"]
        self.assertGreater(size, 16 * 4096)
        kinds = {kind for kind, _ in events}
        self.assertEqual(kinds, {"parse", "listing", "serialize", "reserve", "write",
                                 "read", "rename", "pause"})
        for block in self._blocks(events):
            block_kinds = {kind for kind, _ in block}
            # Serializing feeds the writing; every other phase stands alone.
            if not block_kinds <= {"serialize", "write"}:
                self.assertEqual(len(block_kinds), 1, block_kinds)
            for kind in ("write", "read"):
                self.assertLessEqual(sum(n for k, n in block if k == kind), 4096, kind)
            serialized = [n for k, n in block if k == "serialize"]
            if serialized:
                # At most one piece's worth plus the item that crossed the edge.
                self.assertLess(sum(serialized) - max(serialized), 4096)
        self.assertEqual(sum(n for k, n in events if k == "write"), size)
        self.assertEqual(sum(n for k, n in events if k == "read"), size)
        self.assertGreaterEqual(len([e for e in events if e[0] == "pause"]),
                                2 * (size // 4096))
        self.assertEqual(os.path.getsize(self.path(answer["set_id"])), size)
        self.assertEqual([s["state"] for s in self.sets(self.fresh())], ["ok"])

    def test_longest_block_stays_one_phase_long(self):
        clock = self._stopped_clock()
        fixed = {"parse": 300, "listing": 300, "reserve": 300, "rename": 300}

        def cost(kind, size):
            # Fixed phases cost 300 ms; serializing 1 ms per 256 bytes, writing
            # and reading 1 ms per 32 bytes (a 4 KiB piece is 128 ms).
            clock[0] += fixed.get(kind) or size // (256 if kind == "serialize" else 32)

        _, capacity = self._instrumented(cost)
        timing = self.add(P128, capacity=capacity)["timing_ms"]
        chain = timing["reserve"] + timing["write"] + timing["verify"] + timing["rename"]
        # Without yields between the phases the commit alone is one block.
        self.assertGreater(chain, 5000)
        self.assertLessEqual(timing["longest_block"], 400)
        self.assertGreaterEqual(timing["longest_block"], 300)

    def _recording_after(self, moment):
        """A check_project that answers recording_active once moment() was true
        at a yield; returns (check, calls)."""
        state = {"recording": False}
        calls = []
        real_pause = stakeout._pause

        async def pause(meter):
            if moment():
                state["recording"] = True
            await real_pause(meter)

        patch = mock.patch.object(stakeout, "_pause", pause)
        patch.start()
        self.addCleanup(patch.stop)

        def check(project_id):
            calls.append(state["recording"])
            if state["recording"]:
                raise stakeout.StakeoutError("recording_active", "Recording.",
                                             "409 Conflict")
            return FP
        return check, calls

    def test_recording_started_between_writing_and_rename_leaves_no_set(self):
        tmp = self.name() + ".tmp"
        seen = []
        check, calls = self._recording_after(
            lambda: seen.append(tmp in self.files()) or seen[-1])
        self.refused_import("recording_active", check=check)
        self.assertTrue(any(seen))
        self.assertEqual(calls[-1], True)
        self.assert_no_set()

    def _recording_during(self, phase):
        """Starts a recording at the first yield after a piece of phase (write
        or read) and checks the import stops after at most one more piece."""
        events, capacity = self._instrumented()
        state = {"recording": False, "flagged_at": None}
        real_pause = stakeout._pause  # the instrumented one

        async def pause(meter):
            if not state["recording"] and any(k == phase for k, _ in events):
                state["recording"] = True
                state["flagged_at"] = len(events)
            await real_pause(meter)

        def check(project_id):
            if state["recording"]:
                raise stakeout.StakeoutError("recording_active", "Recording.",
                                             "409 Conflict")
            return FP

        with mock.patch.object(stakeout, "_pause", pause):
            error = self.refused_import("recording_active", pkg=P128, check=check,
                                        capacity=capacity)
        self.assertEqual(error.status, "409 Conflict")
        later = events[state["flagged_at"]:]
        self.assertLessEqual(sum(n for k, n in later if k in ("write", "read")),
                             stakeout.WRITE_PIECE)
        self.assertNotIn("rename", [k for k, _ in later])
        self.assert_no_set()

    def test_recording_started_while_writing_stops_after_one_piece(self):
        self._recording_during("write")

    def test_recording_started_while_reading_back_stops_after_one_piece(self):
        self._recording_during("read")

    def test_listing_during_an_import_never_lists_or_reads_its_tmp(self):
        pkg = make_package(targets=64)
        set_id = canonical_set_id(pkg)
        tmp = self.path(set_id) + ".tmp"
        self.add(make_package(targets=3, offset=5))
        touched = []
        listings = []
        real_pause = stakeout._pause
        real_trailer = stakeout._read_trailer
        real_open = builtins.open
        inside = []

        def trailer(path, *args):
            touched.append(path)
            return real_trailer(path, *args)

        def watched_open(path, mode="r", *args, **kwargs):
            if inside and str(path) == tmp:
                touched.append(path)
            return real_open(path, mode, *args, **kwargs)

        async def pause(meter):
            if os.path.exists(tmp) and not inside:
                inside.append(True)
                try:
                    with self.immutable_patches():
                        listings.append(await self.store._load(PID, stakeout._BlockMeter()))
                finally:
                    inside.pop()
            await real_pause(meter)

        with mock.patch.object(stakeout, "_pause", pause), \
                mock.patch.object(stakeout, "_read_trailer", trailer), \
                mock.patch("builtins.open", watched_open):
            answer = self.add(pkg)
        self.assertGreaterEqual(len(listings), 3)
        for listed in listings:
            self.assertEqual([entry["summary"]["state"] for entry in listed], ["ok"])
            self.assertNotIn(set_id, [entry["summary"]["set_id"] for entry in listed])
        self.assertNotIn(tmp, touched)
        self.assertEqual(sorted(self.files()), sorted([self.name(answer["set_id"]),
                                                       self.name(canonical_set_id(
                                                           make_package(targets=3,
                                                                        offset=5)))]))
        self.assertEqual(sorted(self.states().values()), ["ok", "ok"])

    def test_recording_started_before_writing_writes_nothing(self):
        reserved = []

        def capacity(size):
            reserved.append(size)
            return True

        def tmp_open(path, mode):
            return path.endswith(".tmp")

        check, calls = self._recording_after(lambda: bool(reserved))
        with mock.patch("builtins.open", _failing_open(tmp_open, AssertionError("opened"))):
            self.refused_import("recording_active", check=check, capacity=capacity)
        self.assertEqual(len(reserved), 1)
        self.assert_no_set()

    def test_project_changed_during_the_commit_leaves_no_set(self):
        for label, answers in (("deleted", None), ("recreated", FP + 1)):
            with self.subTest(label):
                calls = []

                def check(project_id):
                    calls.append(project_id)
                    if len(calls) < 4:
                        return FP
                    if answers is None:
                        raise stakeout.StakeoutError("project_not_found",
                                                     "Project not found.", "404 Not Found")
                    return answers

                error = self.refused_import("project_not_found", check=check)
                self.assertEqual(error.status, "404 Not Found")
                self.assertEqual(len(calls), 4)
                self.assert_no_set()

    def test_cancelled_import_removes_its_tmp(self):
        async def scenario():
            gate = asyncio.Event()
            written = asyncio.Event()
            real_pause = stakeout._pause

            async def pause(meter):
                if self.name() + ".tmp" in self.files():
                    written.set()
                    await gate.wait()
                await real_pause(meter)

            with mock.patch.object(stakeout, "_pause", pause):
                task = asyncio.ensure_future(self.store.import_package(
                    compact(sample_package(PID)), self.check, self.ok))
                waiter = asyncio.ensure_future(written.wait())
                await asyncio.wait([task, waiter], return_when=asyncio.FIRST_COMPLETED)
                waiter.cancel()
                self.assertFalse(task.done(), "the import never yielded with its .tmp")
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

        run(scenario())
        self.assert_no_set()

    def test_overview_reports_wall_time_and_longest_block_of_its_listing(self):
        self.add()
        self.add(make_package(targets=3))
        store = self.fresh()
        clock = FakeClock(self, suspension=5)
        real_trailer = stakeout._read_trailer

        def slow_trailer(*args):
            clock.advance(30)
            return real_trailer(*args)

        with mock.patch.object(stakeout, "_read_trailer", slow_trailer):
            self.assertEqual(len(self.sets(store)), 2)
        # Two trailers of 30 ms, each followed by a 5 ms service pause.
        self.assertEqual((store.last_load_ms, store.last_load_block_ms), (70, 30))
        with mock.patch.object(stakeout.os, "ilistdir", side_effect=AssertionError):
            self.assertEqual(len(self.sets(store)), 2)
        self.assertEqual((store.last_load_ms, store.last_load_block_ms), (0, 0))


class TestListing(_StoreCase):
    """Listings classify the files of a project (C2) and never change them."""

    def stage(self, final=None, tmp=None, set_id=SAMPLE_SET_ID):
        for suffix, data in (("", final), (".tmp", tmp)):
            if data is not None:
                self.write(self.path(set_id) + suffix, data)

    def test_classification_matrix(self):
        data = self.stored_sample()
        summary = json.loads(data)["summary"]
        body = data[:-stakeout.TRAILER_BYTES]
        same_length = bytes([data[0] ^ 1]) + data[1:]
        other = dict(summary, set_id="so-00000000")
        cases = [
            ("final, trailer valid", data, None, "ok"),
            ("final with an interior byte changed (found by downloads)", same_length,
             None, "ok"),
            ("final, trailer of another set", body + stakeout._trailer(other), None,
             "damaged"),
            ("final, truncated", data[:-1], None, "damaged"),
            ("final, no trailer", body, None, "damaged"),
            ("final, empty", b"", None, "damaged"),
            ("final, old flat layout", json.dumps(json.loads(data)).encode(), None,
             "damaged"),
            ("lone .tmp, complete", None, data, "incomplete"),
            ("lone .tmp, truncated", None, data[:100], "incomplete"),
            ("lone .tmp, empty", None, b"", "incomplete"),
            ("final and a .tmp", data, data[:100], "ok"),
            ("final and a complete .tmp", data, data, "ok"),
            ("damaged final and a complete .tmp", data[:-1], data, "damaged"),
        ]
        for label, final, tmp, state in cases:
            with self.subTest(label):
                shutil.rmtree(self.root, True)
                self.stage(final, tmp)
                before = self.snapshot()
                with self.immutable_patches():
                    store = self.fresh()
                    listed = self.sets(store)
                    self.assertEqual([(s["set_id"], s["state"]) for s in listed],
                                     [(SAMPLE_SET_ID, state)])
                    if state != "ok":
                        self.assertEqual(listed[0], {"set_id": SAMPLE_SET_ID,
                                                     "state": state})
                        self.refused("set_damaged", self.download, store=store)
                self.assertEqual(self.snapshot(), before)
                # Every state is deletable, with its .tmp.
                self.delete(store=store)
                self.assertEqual(self.files(), [])

    def test_an_incomplete_set_does_not_count_and_an_import_replaces_it(self):
        # One import time for both imports of the sample, so their bytes match
        # even across a second boundary.
        clock = mock.patch.object(stakeout, "now_seconds", return_value=846000000)
        clock.start()
        self.addCleanup(clock.stop)
        data = self.stored_sample()
        for k in range(3):
            self.add(make_package(targets=3, offset=k))
        self.stage(tmp=data[:100])
        self.assertEqual(sorted(self.states().values()), ["incomplete", "ok", "ok", "ok"])
        answer = self.add(store=self.fresh())
        self.assertEqual(answer["set_id"], SAMPLE_SET_ID)
        self.assertEqual(self.read(self.path()), data)
        self.assertNotIn(self.name() + ".tmp", self.files())
        self.assertEqual(sorted(self.states().values()), ["ok"] * 4)

    def test_unknown_names_are_ignored(self):
        data = self.stored_sample()
        for name in ("so-zzzzzzzz.json", "notes.txt", "so-3a4739b9.jsonx",
                     "so-3a4739b9.json.previous", "so-3a4739b9.json.tmp.tmp",
                     "SO-3A4739B9.json", "so-3a4739b9.JSON", ".json", ".json.tmp"):
            self.write(os.path.join(self.root, "project-a", name), data)
        os.makedirs(os.path.join(self.root, "project-a", "so-3a4739b8.json"))
        self.write(os.path.join(self.root, "project-ab", SAMPLE_SET_ID + ".json"), data)
        self.write(os.path.join(self.root, "project-a.deleting", SAMPLE_SET_ID + ".json"),
                   data)
        self.write(os.path.join(self.tmp, "tracking.stakeout-project-a.%s.json"
                                % SAMPLE_SET_ID), data)
        before = self.snapshot()
        with self.immutable_patches():
            self.assertEqual(self.sets(self.fresh(), project_id="project-a"), [])
        self.assertEqual(self.snapshot(), before)

    def test_missing_project_folder_is_an_empty_cacheable_listing(self):
        store = self.fresh()
        with self.immutable_patches():
            self.assertEqual(self.sets(store), [])
            self.assertEqual(store.orphaned(PID, FP), 0)
            self.assertIn(PID, store._cache)
            with mock.patch.object(stakeout.os, "ilistdir", side_effect=AssertionError):
                self.assertEqual(self.sets(store), [])
        self.assertFalse(os.path.exists(self.root))

    def test_errors_at_each_step_leave_no_cache_and_no_change(self):
        self.add()
        self.add(make_package(targets=3))
        before = self.snapshot()
        real_open = builtins.open

        def failing_handle(method, error):
            class Failing:
                def __init__(self, handle):
                    self.handle = handle

                def __getattr__(self, name):
                    if name == method:
                        raise error
                    return getattr(self.handle, name)

                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    self.handle.close()

            def fake_open(path, mode="r", *args, **kwargs):
                return Failing(real_open(path, mode, *args, **kwargs))
            return mock.patch("builtins.open", fake_open)

        def failing_names(error):
            def names(path):
                yield (SAMPLE_SET_ID + ".json", 0x8000, 0)
                raise error
            return mock.patch.object(stakeout.os, "ilistdir", names)

        steps = [("ilistdir", lambda error: mock.patch.object(
                      stakeout.os, "ilistdir", side_effect=error)),
                 ("iteration", failing_names),
                 ("stat", lambda error: mock.patch.object(stakeout.os, "stat",
                                                          side_effect=error)),
                 ("open", lambda error: mock.patch("builtins.open", side_effect=error)),
                 ("seek", lambda error: failing_handle("seek", error)),
                 ("read", lambda error: failing_handle("read", error)),
                 ("parse", lambda error: mock.patch.object(stakeout.ujson, "loads",
                                                           side_effect=error))]
        for step, patch in steps:
            for error, code in ((MemoryError(), "stakeout_busy"),
                                (OSError(5, "io"), "stakeout_read_failed")):
                if step == "parse" and isinstance(error, OSError):
                    continue
                with self.subTest(step=step, error=type(error).__name__):
                    store = self.fresh()
                    with self.immutable_patches(), patch(error):
                        self.refused(code, store.sets, PID, FP)
                    self.assertNotIn(PID, store._cache)
                    self.assertEqual(self.snapshot(), before)
                    self.assertEqual(sorted(self.states(store).values()), ["ok", "ok"])

    def test_a_set_file_gone_meanwhile_is_a_change_not_damage(self):
        a, b = self.add()["set_id"], self.add(make_package(targets=3))["set_id"]
        real_trailer = stakeout._read_trailer
        gone = []

        def vanishing(path, *args):
            if not gone:
                gone.append(path)
                os.remove(path)
            return real_trailer(path, *args)

        store = self.fresh()
        with mock.patch.object(stakeout, "_read_trailer", vanishing):
            listed = self.sets(store)
        # Listed once more without the vanished set: never cached as damaged.
        survivor = b if gone[0] == self.path(a) else a
        self.assertEqual([(s["set_id"], s["state"]) for s in listed], [(survivor, "ok")])
        self.assertEqual(store.generation(PID), 1)

    def test_listing_never_renames_or_removes_during_recording_or_maintenance(self):
        # The store has no reading mode any more: the same code lists always.
        self.assertNotIn("recover", inspect.signature(stakeout.TargetStore.sets).parameters)
        data = self.stored_sample()
        self.add(make_package(targets=3))
        self.stage(tmp=data)
        self.stage(set_id="so-00000001", final=data[:-1], tmp=data)
        before = self.snapshot()
        with self.immutable_patches():
            listed = self.sets(self.fresh())
        self.assertEqual(sorted(s["state"] for s in listed), ["damaged", "incomplete", "ok"])
        self.assertEqual(self.snapshot(), before)

    def test_discovery_yields_while_walking_many_names(self):
        self.add()
        for index in range(200):
            self.write(os.path.join(self.root, PID, "stray-%03d" % index), b"")
        pauses = []
        real_pause = stakeout._pause

        async def pause(meter):
            pauses.append(True)
            await real_pause(meter)

        FakeClock(self)
        with mock.patch.object(stakeout, "_pause", pause):
            self.assertEqual(len(self.sets(self.fresh())), 1)
        # At the latest every YIELD_TARGETS names, and once after the trailer.
        self.assertGreaterEqual(len(pauses), 201 // stakeout.YIELD_TARGETS + 1)

    def test_four_large_sets_list_from_their_trailers(self):
        # Four P256-shaped files (host codec): written directly, larger than an
        # import could make them; the listing reads 4 KiB of each.
        written = {}
        with mock.patch.object(stakeout, "_pause", _no_pause):
            for k in range(4):
                pkg = make_package(targets=496, lines=4, coords_per_line=500, props=8,
                                   prop_value="v" * P256_PROP_VALUE_LENGTH, offset=k)
                plan = run(stakeout.validate_import(pkg))
                doc = set_doc(plan, project_created_at=FP, imported_at=100 + k)
                os.makedirs(os.path.join(self.root, PID), exist_ok=True)
                run(stakeout._store_set_file(self.path(doc["set_id"]), doc))
                written[doc["set_id"]] = os.path.getsize(self.path(doc["set_id"]))
        self.assertGreater(min(written.values()), 256 * 1024)
        reads = []
        parsed = []
        real_open, real_loads = builtins.open, stakeout.ujson.loads

        class Watched:
            def __init__(self, handle):
                self.handle = handle

            def read(self, *args):
                data = self.handle.read(*args)
                reads.append(len(data))
                return data

            def __getattr__(self, name):
                return getattr(self.handle, name)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.handle.close()

        def loads(data):
            parsed.append(len(data))
            return real_loads(data)

        store = self.fresh()
        with mock.patch("builtins.open", lambda *a, **k: Watched(real_open(*a, **k))), \
                mock.patch.object(stakeout.ujson, "loads", loads), \
                mock.patch.object(stakeout.ujson, "load", side_effect=AssertionError), \
                mock.patch.object(stakeout.hashlib, "sha256", side_effect=AssertionError):
            listed = self.sets(store)
        self.assertEqual(sorted(s["set_id"] for s in listed), sorted(written))
        self.assertEqual(reads, [stakeout.TRAILER_BYTES] * 4)
        self.assertTrue(all(size < stakeout.TRAILER_BYTES for size in parsed))
        self.assertGreater(store.last_load_ms, 0)
        self.assertLessEqual(store.last_load_block_ms, store.last_load_ms)
        self.assertIs(type(store.last_load_block_ms), int)
        self.sets(store)
        self.assertEqual((store.last_load_ms, store.last_load_block_ms), (0, 0))

    def test_cold_delete_import_and_download_read_no_set_body(self):
        self.add()
        self.add(make_package(targets=3))
        other = canonical_set_id(make_package(targets=3))
        reads = []
        real_open = builtins.open

        class Watched:
            def __init__(self, path, handle):
                self.path, self.handle = path, handle

            def read(self, *args):
                data = self.handle.read(*args)
                reads.append((self.path, len(data)))
                return data

            def __getattr__(self, name):
                return getattr(self.handle, name)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.handle.close()

        def watched(path, mode="r", *args, **kwargs):
            return Watched(str(path), real_open(path, mode, *args, **kwargs))

        actions = (("delete", lambda store: self.delete(other, store=store)),
                   ("import", lambda store: self.add(make_package(targets=5), store=store)),
                   ("download", lambda store: self.download(store=store)))
        for label, action in actions:
            with self.subTest(label):
                del reads[:]
                with mock.patch("builtins.open", watched), \
                        mock.patch.object(stakeout.ujson, "load", side_effect=AssertionError):
                    action(self.fresh())
                sets = [(path, size) for path, size in reads if path.endswith(".json")]
                self.assertTrue(sets)
                if label == "download":
                    # The trailers of the listing, then the file in 1 KiB reads.
                    listing = [size for _, size in sets[:2]]
                    self.assertEqual(listing, [stakeout.TRAILER_BYTES] * 2)
                    self.assertTrue(all(size <= 1024 for _, size in sets[2:]))
                else:
                    # Only trailers: one read of TRAILER_BYTES per set file.
                    self.assertEqual([size for _, size in sets],
                                     [stakeout.TRAILER_BYTES] * len(sets))


class TestCrashSafety(_StoreCase):
    """Abrupt process loss at every file operation keeps every committed set."""

    class Lost(BaseException):
        """The process stopped: nothing after it reaches the flash."""

    @contextlib.contextmanager
    def power(self, at=None, after=False):
        """Counts the file operations; operation number at loses power before it
        runs (after False) or right after it (after True). Writes reach the
        file at once, nothing reaches it after the loss. Yields the list of
        operations seen."""
        seen = []
        dead = []
        handles = []
        lost = self.Lost

        def step(name, run_it):
            if dead:
                raise lost()
            seen.append(name)
            if len(seen) == at and not after:
                dead.append(True)
                raise lost()
            try:
                result = run_it()
            except Exception:
                if len(seen) == at:  # the operation failed, then the power
                    dead.append(True)
                    raise lost()
                raise
            if len(seen) == at:
                dead.append(True)
                raise lost()
            return result

        real = {name: getattr(os, name) for name in ("mkdir", "rename", "remove", "stat")}

        def wrap(name):
            return lambda *args, **kwargs: step(name, lambda: real[name](*args, **kwargs))

        class Handle:
            def __init__(self, handle):
                self.handle = handle
                handles.append(handle)

            def write(self, data):
                def write_through():
                    count = self.handle.write(data)
                    self.handle.flush()
                    return count
                return step("write", write_through)

            def read(self, *args):
                return step("read", lambda: self.handle.read(*args))

            def flush(self):
                return step("flush", self.handle.flush)

            def close(self):
                return step("close", self.handle.close)

            def __getattr__(self, name):
                return getattr(self.handle, name)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.close()

        def fake_open(path, mode="r", *args, **kwargs):
            return step("open", lambda: Handle(_real_open(path, mode, *args, **kwargs)))

        with contextlib.ExitStack() as stack:
            for name in real:
                stack.enter_context(mock.patch.object(stakeout.os, name, wrap(name)))
            stack.enter_context(mock.patch("builtins.open", fake_open))
            stack.enter_context(mock.patch.object(stakeout, "_pause", _no_pause))
            try:
                yield seen
            finally:
                for handle in handles:
                    handle.close()

    def clear(self, set_id):
        for suffix in ("", ".tmp"):
            if os.path.exists(self.path(set_id) + suffix):
                os.remove(self.path(set_id) + suffix)

    def test_power_loss_at_every_import_step(self):
        # A committed set of the same project that must survive byte for byte.
        kept = self.add(make_package(targets=3))["set_id"]
        kept_bytes = self.read(self.path(kept))
        pkg = make_package(targets=64)
        new = canonical_set_id(pkg)
        with mock.patch.object(stakeout, "now_seconds", return_value=845000000):
            with self.power() as seen:
                self.add(pkg, store=self.fresh())
            operations = list(seen)
            committed = self.read(self.path(new))
            kinds = set(operations)
            self.assertTrue({"mkdir", "open", "write", "flush", "close", "read", "stat",
                             "rename"} <= kinds, kinds)
            self.assertGreater(operations.count("write"), 3)
            outcomes = set()
            for at in range(1, len(operations) + 1):
                for after in (False, True):
                    with self.subTest(at=at, operation=operations[at - 1], after=after):
                        self.clear(new)
                        with self.power(at, after):
                            with self.assertRaises(self.Lost):
                                self.add(pkg, store=self.fresh())
                        # A restart: a new store over what reached the flash.
                        states = self.states()
                        self.assertEqual(states[kept], "ok")
                        self.assertEqual(self.read(self.path(kept)), kept_bytes)
                        state = states.get(new)
                        self.assertIn(state, (None, "incomplete", "ok"))
                        if state == "ok":
                            self.assertEqual(self.read(self.path(new)), committed)
                            self.assertEqual(self.download(new, store=self.fresh()),
                                             committed)
                        elif state == "incomplete":
                            # A re-import replaces the leftover.
                            self.assertEqual(self.add(pkg, store=self.fresh())["bytes"],
                                             len(committed))
                            self.assertEqual(self.read(self.path(new)), committed)
                        outcomes.add(state)
        self.assertEqual(outcomes, {None, "incomplete", "ok"})

    def test_rename_raising_before_or_after_moving(self):
        def moved_then_raise(source, target):
            _real_rename(source, target)
            raise OSError(5, "io")

        for label, effect, state in (("raises before moving", OSError(5, "io"), None),
                                     ("raises after moving", moved_then_raise, "ok")):
            with self.subTest(label):
                shutil.rmtree(self.root, True)
                with mock.patch.object(stakeout.os, "rename", side_effect=effect):
                    try:
                        self.add(store=self.fresh())
                    except stakeout.StakeoutError as error:
                        self.assertEqual(error.code, "stakeout_write_failed")
                self.assertEqual(self.states().get(SAMPLE_SET_ID), state)
                self.assertNotIn(self.name() + ".tmp", self.files())

    def test_power_loss_while_deleting(self):
        data = self.stored_sample()
        for at in (1, 2, 3):
            for after in (False, True):
                with self.subTest(at=at, after=after):
                    shutil.rmtree(self.root, True)
                    self.write(self.path(), data)
                    self.write(self.path() + ".tmp", data)
                    store = self.fresh()
                    self.sets(store)
                    with self.power(at, after):
                        try:
                            self.delete(store=store)
                        except self.Lost:
                            pass
                    state = self.states().get(SAMPLE_SET_ID)
                    # Once the removal began, the set never comes back as ok.
                    if os.path.exists(self.path()):
                        self.assertEqual(state, "ok")
                    elif os.path.exists(self.path() + ".tmp"):
                        self.assertEqual(state, "incomplete")
                    else:
                        self.assertIsNone(state)

    def test_removal_that_raises_after_unlinking_counts(self):
        self.add()
        real_remove = os.remove

        def remove_then_raise(path):
            real_remove(path)
            raise OSError(5, "io")

        with mock.patch.object(stakeout.os, "remove", remove_then_raise):
            self.assertEqual(self.delete(), "done")
        self.assertEqual(self.files(), [])
        self.assertEqual(self.sets(), [])
        self.assertEqual(self.sets(self.fresh()), [])

    def test_tmp_removal_failing_after_the_file_went_is_507_and_incomplete(self):
        data = self.stored_sample()
        self.write(self.path(), data)
        self.write(self.path() + ".tmp", data)
        real_remove = os.remove

        def remove(path):
            if path.endswith(".tmp"):
                raise OSError(5, "io")
            real_remove(path)

        self.sets()
        with mock.patch.object(stakeout.os, "remove", remove):
            error = self.refused("stakeout_delete_failed", self.store.delete_set, PID, FP,
                                 SAMPLE_SET_ID, 0)
        self.assertEqual(error.status, "507 Insufficient Storage")
        self.assertEqual(self.files(), [self.name() + ".tmp"])
        self.assertEqual(self.sets(), [{"set_id": SAMPLE_SET_ID, "state": "incomplete"}])


class _Gate:
    """Holds the first service pause that until(meter) selects until opened."""

    def __init__(self, test, until=None):
        self.reached = asyncio.Event()
        self.opened = asyncio.Event()
        self.until = until or (lambda: True)
        self.used = False
        real_pause = stakeout._pause

        async def pause(meter):
            if not self.used and self.until():
                self.used = True
                self.reached.set()
                await self.opened.wait()
            await real_pause(meter)

        patch = mock.patch.object(stakeout, "_pause", pause)
        patch.start()
        test.addCleanup(patch.stop)


class TestGenerations(_StoreCase):
    """A listing that ran beside a change never publishes a stale result (C2)."""

    def suspended_listing(self, during):
        """A cold listing held at its first yield while during() runs."""
        async def scenario():
            gate = _Gate(self)
            listing = asyncio.ensure_future(self.store.sets(PID, FP))
            await gate.reached.wait()
            await during()
            gate.opened.set()
            return await listing
        return run(scenario())

    def test_a_suspended_listing_cannot_resurrect_a_deleted_set(self):
        a, b = self.add()["set_id"], self.add(make_package(targets=3))["set_id"]
        self.store = self.fresh()

        async def delete():
            await self.store.delete_set(PID, FP, a, 0)

        listed = self.suspended_listing(delete)
        self.assertEqual([s["set_id"] for s in listed], [b])
        self.assertEqual([s["set_id"] for s in self.sets()], [b])

    def test_a_suspended_listing_cannot_hide_a_new_set(self):
        a = self.add()["set_id"]
        self.add(make_package(targets=7))
        self.store = self.fresh()
        added = []

        async def import_one():
            added.append((await self.store.import_package(
                compact(make_package(targets=3)), self.check, self.ok))["set_id"])

        listed = self.suspended_listing(import_one)
        self.assertIn(added[0], [s["set_id"] for s in listed])
        self.assertIn(a, [s["set_id"] for s in listed])
        self.assertEqual(len(self.sets()), 3)

    def test_a_suspended_listing_cannot_turn_a_damaged_set_back_to_ok(self):
        self.add()
        self.add(make_package(targets=3))
        data = bytearray(self.read(self.path()))
        data[10] ^= 1
        self.write(self.path(), bytes(data))
        self.store = self.fresh()

        async def download():
            with self.assertRaises(stakeout.StakeoutError) as caught:
                await self.store.send_set(_HeadWriter(), PID, FP, SAMPLE_SET_ID, _head)
            self.assertEqual(caught.exception.code, "set_damaged")

        listed = self.suspended_listing(download)
        self.assertEqual({s["set_id"]: s["state"] for s in listed}[SAMPLE_SET_ID],
                         "damaged")
        self.assertEqual(self.states(self.store)[SAMPLE_SET_ID], "damaged")

    def test_a_stale_listing_is_repeated_once_then_busy(self):
        self.add()
        real_load = stakeout.TargetStore._load
        for changes, listings in ((1, 2), (2, 2)):
            with self.subTest(changes=changes):
                calls = []

                async def changing(store, project_id, meter):
                    calls.append(1)
                    entries = await real_load(store, project_id, meter)
                    if len(calls) <= changes:
                        store._changed(project_id)
                    return entries

                store = self.fresh()
                with mock.patch.object(stakeout.TargetStore, "_load", changing):
                    if changes == 1:
                        self.assertEqual(len(self.sets(store)), 1)
                        self.assertIn(PID, store._cache)
                    else:
                        error = self.refused("stakeout_busy", store.sets, PID, FP)
                        self.assertEqual(error.status, "503 Service Unavailable")
                        self.assertNotIn(PID, store._cache)
                self.assertEqual(len(calls), listings)

    def test_a_cold_delete_that_suspends_while_a_download_starts_is_busy(self):
        self.add(make_package(targets=64))
        set_id = canonical_set_id(make_package(targets=64))
        self.store = self.fresh()

        async def scenario():
            gate = _Gate(self)
            held = asyncio.Event()
            release = asyncio.Event()

            class Held(_HeadWriter):
                async def drain(self):
                    held.set()
                    await release.wait()

            deleting = asyncio.ensure_future(self.store.delete_set(PID, FP, set_id, 0))
            await gate.reached.wait()
            writer = Held()
            download = asyncio.ensure_future(self.store.send_set(writer, PID, FP, set_id,
                                                                 _head))
            await held.wait()
            self.assertEqual(self.store.readers(PID), 1)
            gate.opened.set()
            with self.assertRaises(stakeout.StakeoutError) as caught:
                await deleting
            release.set()
            await download
            return caught.exception, writer

        error, writer = run(scenario())
        self.assertEqual((error.code, error.status), ("storage_busy", "409 Conflict"))
        self.assertIn("download", error.message)
        self.assertEqual(writer.body, self.read(self.path(set_id)))
        self.assertEqual(self.store.readers(PID), 0)
        self.delete(set_id)
        self.assertEqual(self.files(), [])

    def test_a_stalled_download_ends_at_its_drain_timeout_and_releases_the_reader(self):
        # A vanished client must not keep the reader (and so block deletes of
        # the project) for the lifetime of the TCP connection.
        self.add()
        requested = []
        real_wait_for = asyncio.wait_for

        def wait_for(awaitable, timeout):
            requested.append(timeout)
            return real_wait_for(awaitable, 0.01)

        class Stalled(_HeadWriter):
            async def drain(self):
                if not self.drains:
                    await asyncio.sleep(0.2)
                self.drains += 1

        writer = Stalled()
        with mock.patch.object(stakeout.asyncio, "wait_for", wait_for):
            with self.assertRaises(asyncio.TimeoutError):
                run(self.store.send_set(writer, PID, FP, SAMPLE_SET_ID, _head))
        self.assertEqual(requested, [10])
        self.assertEqual(len(writer.pieces), 1)
        self.assertEqual(self.store.readers(PID), 0)
        self.delete()
        self.assertEqual(self.files(), [])

    def test_a_delete_can_never_land_between_lookup_and_reader_registration(self):
        self.add(make_package(targets=64))
        set_id = canonical_set_id(make_package(targets=64))
        data = self.read(self.path(set_id))
        tried = 0
        while True:
            tried += 1
            with self.subTest(pause=tried):
                if not os.path.exists(self.path(set_id)):
                    self.write(self.path(set_id), data)
                store = self.fresh()
                outcome = {}
                real_pause = stakeout._pause
                count = [0]

                async def pause(meter):
                    count[0] += 1
                    if count[0] == tried:
                        try:
                            await store.delete_set(PID, FP, set_id, 0)
                            outcome["delete"] = "deleted"
                        except stakeout.StakeoutError as error:
                            outcome["delete"] = error.code
                    await real_pause(meter)

                writer = _HeadWriter()
                with mock.patch.object(stakeout, "_pause", pause):
                    try:
                        run(store.send_set(writer, PID, FP, set_id, _head))
                        outcome["download"] = "sent"
                    except stakeout.StakeoutError as error:
                        outcome["download"] = error.code
                if "delete" not in outcome:
                    break
                if outcome["delete"] == "deleted":
                    # Before the registration: nothing was sent.
                    self.assertEqual(outcome["download"], "set_not_found")
                    self.assertIsNone(writer.head)
                else:
                    self.assertEqual(outcome, {"delete": "storage_busy",
                                               "download": "sent"})
                    self.assertEqual(writer.body, data)
                self.assertEqual(store.readers(PID), 0)
        self.assertGreater(tried, 10)


class TestDownload(_StoreCase):
    """GET of a set file: byte for byte, digest-gated trailer, reader count (C5)."""

    def setUp(self):
        super().setUp()
        # A fixed import time, so a re-import writes the same bytes even
        # across a second boundary.
        clock = mock.patch.object(stakeout, "now_seconds", return_value=845000000)
        clock.start()
        self.addCleanup(clock.stop)
        self.set_id = self.add(make_package(targets=64))["set_id"]
        self.data = self.read(self.path(self.set_id))
        self.body_bytes = json.loads(self.data)["summary"]["body_bytes"]

    def corrupt(self, at, data=None):
        changed = bytearray(self.data if data is None else data)
        changed[at] ^= 1
        self.write(self.path(self.set_id), bytes(changed))
        return bytes(changed)

    def test_bytes_and_length_equal_the_file_in_small_drained_pieces(self):
        writer = _HeadWriter()
        self.assertEqual(self.download(self.set_id, store=self.fresh(), writer=writer),
                         self.data)
        self.assertEqual(writer.head, _head(len(self.data)))
        self.assertTrue(all(len(piece) <= 1024 for piece in writer.pieces))
        self.assertEqual(writer.drains, len(writer.pieces))
        self.assertEqual(self.store.readers(PID), 0)

    def test_a_changed_interior_byte_withholds_the_trailer_and_damages_the_set(self):
        for at in (0, self.body_bytes // 2, self.body_bytes - 1):
            with self.subTest(at=at):
                self.corrupt(at)
                store = self.fresh()
                self.assertEqual(self.states(store)[self.set_id], "ok")
                writer = _HeadWriter()
                self.log.reset_mock()
                error = self.refused("set_damaged", store.send_set, writer, PID, FP,
                                     self.set_id, _head)
                self.assertEqual(error.set_id, self.set_id)
                # One head, the body, never the trailer.
                self.assertEqual(writer.head, _head(len(self.data)))
                self.assertEqual(len(writer.body), self.body_bytes)
                self.assertNotIn(b'"summary"', writer.body)
                self.assertEqual(self.log.call_args[0][0], "ERROR")
                self.assertEqual(store.readers(PID), 0)
                generation = store.generation(PID)
                self.assertEqual(self.states(store)[self.set_id], "damaged")
                # Refused before the head from now on, and still deletable.
                writer = _HeadWriter()
                self.refused("set_damaged", store.send_set, writer, PID, FP, self.set_id,
                             _head)
                self.assertIsNone(writer.head)
                self.assertEqual(store.generation(PID), generation)
                self.write(self.path(self.set_id), self.data)

    def test_a_size_that_differs_from_the_summary_is_damage_before_the_head(self):
        store = self.fresh()
        self.sets(store)
        self.write(self.path(self.set_id), self.data + b" ")
        writer = _HeadWriter()
        self.refused("set_damaged", store.send_set, writer, PID, FP, self.set_id, _head)
        self.assertIsNone(writer.head)
        self.assertEqual(self.states(store)[self.set_id], "damaged")
        self.delete(self.set_id, store=store)
        self.assertEqual(self.files(), [])

    def test_a_file_shorter_than_its_size_is_damage_without_the_trailer(self):
        real_open = builtins.open

        class Short:
            def __init__(self, handle):
                self.handle = handle

            def read(self, size=-1):
                return b"" if self.handle.tell() >= 2048 else self.handle.read(size)

            def __getattr__(self, name):
                return getattr(self.handle, name)

        store = self.fresh()
        self.sets(store)
        writer = _HeadWriter()
        with mock.patch("builtins.open", lambda *a, **k: Short(real_open(*a, **k))):
            self.refused("set_damaged", store.send_set, writer, PID, FP, self.set_id, _head)
        self.assertEqual(writer.body, self.data[:2048])
        self.assertEqual(store.readers(PID), 0)

    def test_disconnect_cancellation_and_io_errors_release_the_reader(self):
        real_open = builtins.open

        class Failing:
            def __init__(self, handle):
                self.handle = handle

            def read(self, size=-1):
                if self.handle.tell() >= 3072:
                    raise OSError(5, "io")
                return self.handle.read(size)

            def __getattr__(self, name):
                return getattr(self.handle, name)

        store = self.fresh()
        cases = [("disconnect", OSError, lambda: run(store.send_set(
                      _HeadWriter(3, OSError(104, "reset")), PID, FP, self.set_id, _head))),
                 ("memory", MemoryError, lambda: run(store.send_set(
                      _HeadWriter(2, MemoryError()), PID, FP, self.set_id, _head))),
                 ("read error", OSError, lambda: self._with(
                     mock.patch("builtins.open", lambda *a, **k: Failing(real_open(*a, **k))),
                     lambda: run(store.send_set(_HeadWriter(), PID, FP, self.set_id, _head))))]
        for label, error, call in cases:
            with self.subTest(label):
                with self.assertRaises(error):
                    call()
                self.assertEqual(store.readers(PID), 0)

        async def cancelled():
            held = asyncio.Event()

            class Held(_HeadWriter):
                async def drain(self):
                    held.set()
                    await asyncio.Event().wait()

            task = asyncio.ensure_future(store.send_set(Held(), PID, FP, self.set_id, _head))
            await held.wait()
            self.assertEqual(store.readers(PID), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        run(cancelled())
        self.assertEqual(store.readers(PID), 0)
        self.assertEqual(self.states(store)[self.set_id], "ok")
        self.delete(self.set_id, store=store)

    @staticmethod
    def _with(patch, call):
        with patch:
            return call()

    def test_deletes_are_busy_while_a_download_is_open(self):
        store = self.fresh()
        other = self.add(make_package(targets=3), store=store)["set_id"]

        async def scenario():
            held = asyncio.Event()
            release = asyncio.Event()

            class Held(_HeadWriter):
                async def drain(self):
                    held.set()
                    await release.wait()

            writer = Held()
            task = asyncio.ensure_future(store.send_set(writer, PID, FP, self.set_id, _head))
            await held.wait()
            for set_id in (self.set_id, other):
                with self.assertRaises(stakeout.StakeoutError) as caught:
                    await store.delete_set(PID, FP, set_id, 0)
                self.assertEqual(caught.exception.code, "storage_busy")
            # Imports never touch existing files and go on.
            await store.import_package(compact(make_package(targets=5)), self.check,
                                       self.ok)
            release.set()
            await task
            return writer

        self.assertEqual(run(scenario()).body, self.data)
        self.delete(other, store=store)
        self.delete(self.set_id, store=store)
        self.assertEqual(len(self.files()), 1)

    def test_open_errors_before_the_head(self):
        for label, error, code in (("gone", OSError(2, "gone"), "set_not_found"),
                                   ("io", OSError(5, "io"), "stakeout_read_failed")):
            with self.subTest(label):
                store = self.fresh()
                self.sets(store)
                generation = store.generation(PID)
                writer = _HeadWriter()
                reading = lambda path, mode: path.endswith(".json")  # noqa: E731
                with mock.patch("builtins.open", _failing_open(reading, error)):
                    self.refused(code, store.send_set, writer, PID, FP, self.set_id, _head)
                self.assertIsNone(writer.head)
                self.assertEqual(store.readers(PID), 0)
                self.assertNotIn(PID, store._cache)
                self.assertGreater(store.generation(PID), generation)
        with mock.patch("builtins.open", side_effect=MemoryError):
            with self.assertRaises(MemoryError):
                run(self.store.send_set(_HeadWriter(), PID, FP, self.set_id, _head))
        self.assertEqual(self.store.readers(PID), 0)

    def test_damaged_and_incomplete_sets_are_refused_before_the_head(self):
        self.write(self.path(SAMPLE_SET_ID) + ".tmp", b"x")
        self.write(self.path("so-00000001"), b"x" * 5000)
        store = self.fresh()
        for set_id in (SAMPLE_SET_ID, "so-00000001"):
            writer = _HeadWriter()
            error = self.refused("set_damaged", store.send_set, writer, PID, FP, set_id,
                                 _head)
            self.assertEqual(error.status, "409 Conflict")
            self.assertIsNone(writer.head)
        self.refused("set_not_found", store.send_set, _HeadWriter(), PID, FP,
                     "so-00000002", _head)
        self.assertEqual(store.readers(PID), 0)

    def test_check_project_runs_after_the_lookup(self):
        store = self.fresh()
        self.refused("project_not_found", store.send_set, _HeadWriter(), PID, FP,
                     self.set_id, _head, lambda pid: FP + 1)
        self.assertEqual(store.readers(PID), 0)

    def test_a_read_error_after_the_head_drops_the_cached_listing(self):
        real_open = builtins.open

        class Failing:
            def __init__(self, handle):
                self.handle = handle

            def read(self, size=-1):
                if self.handle.tell() >= 2048:
                    raise OSError(5, "io")
                return self.handle.read(size)

            def __getattr__(self, name):
                return getattr(self.handle, name)

        store = self.fresh()
        self.assertEqual(self.states(store)[self.set_id], "ok")
        generation = store.generation(PID)
        writer = _HeadWriter()
        with mock.patch("builtins.open", lambda *a, **k: Failing(real_open(*a, **k))):
            with self.assertRaises(OSError):
                run(store.send_set(writer, PID, FP, self.set_id, _head))
        self.assertIsNotNone(writer.head)
        self.assertNotIn(PID, store._cache)
        self.assertGreater(store.generation(PID), generation)
        # No damage: the next download reads the file again.
        self.assertEqual(self.states(store)[self.set_id], "ok")
        self.assertEqual(self.download(self.set_id, store=store), self.data)

    def test_a_damage_mark_goes_with_the_file(self):
        # The download marks the set damaged; a delete that removes the file
        # but not a .tmp beside it fails, and an intact import of the same
        # set must then list "ok" (cached and listed again).
        self.corrupt(5)
        store = self.fresh()
        self.refused("set_damaged", store.send_set, _HeadWriter(), PID, FP, self.set_id,
                     _head)
        self.write(self.path(self.set_id) + ".tmp", b"x")
        real_remove = os.remove

        def remove(path):
            if path.endswith(".tmp"):
                raise OSError(5, "io")
            real_remove(path)

        with mock.patch.object(stakeout.os, "remove", remove):
            self.refused("stakeout_delete_failed", store.delete_set, PID, FP, self.set_id, 0)
        self.assertNotIn(self.path(self.set_id), store._damaged)
        self.assertEqual(self.sets(store)[0]["state"], "incomplete")
        self.add(make_package(targets=64), store=store)
        self.assertEqual(self.read(self.path(self.set_id)), self.data)
        self.assertEqual(self.states(store)[self.set_id], "ok")
        self.assertEqual([s["state"] for s in self.sets(store)], ["ok"])
        store.invalidate(PID)
        self.assertEqual([s["state"] for s in self.sets(store)], ["ok"])
        self.assertEqual(self.download(self.set_id, store=store), self.data)

    def test_a_damage_mark_is_dropped_when_a_listing_finds_no_file(self):
        self.corrupt(5)
        store = self.fresh()
        self.refused("set_damaged", store.send_set, _HeadWriter(), PID, FP, self.set_id,
                     _head)
        os.remove(self.path(self.set_id))
        store.invalidate(PID)
        self.assertEqual(self.sets(store), [])
        self.assertEqual(store._damaged, set())

    def test_a_commit_drops_a_damage_mark_of_its_path(self):
        os.remove(self.path(self.set_id))
        store = self.fresh()
        # A stale mark that no listing has seen yet (set before the commit).
        real_store = stakeout._store_set_file

        async def store_set_file(path, *args, **kwargs):
            summary = await real_store(path, *args, **kwargs)
            store._damaged.add(path)
            return summary

        with mock.patch.object(stakeout, "_store_set_file", store_set_file):
            self.add(make_package(targets=64), store=store)
        self.assertEqual(store._damaged, set())
        self.assertEqual(self.states(store)[self.set_id], "ok")


class _Survey:
    """The part of a tracker reconcile_deletions reads: the live projects."""

    def __init__(self, *project_ids):
        self.projects = [{"id": project_id} for project_id in project_ids]


class TestProjectDeletion(_StoreCase):
    """Project deletion through a set-aside folder and the start-up hook (C6)."""

    def setUp(self):
        super().setUp()
        self.add(sample_package("project-a"))
        self.add(sample_package("project-ab"))
        self.write(os.path.join(self.root, "project-a", "notes.txt"), b"{}\n")
        self.write(os.path.join(self.root, "project-a", SAMPLE_SET_ID + ".json.tmp"), b"x")
        self.write(os.path.join(self.root, "readme.txt"), b"keep")
        self.kept = self.outside()
        self.folder = os.path.join(self.root, "project-a")
        self.aside = self.folder + ".deleting"

    def outside(self):
        """The files outside project-a and its set-aside folder, with their bytes."""
        return {name: data for name, data in self.snapshot().items()
                if name.split("/")[0] not in ("project-a", "project-a.deleting")}

    def assert_kept(self):
        """Everything outside project-a is byte for byte as before."""
        self.assertEqual(self.outside(), self.kept)
        self.assertEqual(len(self.kept), 2)

    def delete_project(self, project_id="project-a", store=None):
        store = store or self.store
        moved = store.begin_project_delete(project_id)
        return moved, run(store.finish_project_delete(project_id))

    def reconcile(self, *live, store=None):
        run((store or self.store).reconcile_deletions(_Survey(*live)))

    def test_a_deletion_removes_the_folder_and_nothing_else(self):
        self.sets(project_id="project-a")
        generation = self.store.generation("project-a")
        self.assertEqual(self.delete_project(), (True, "done"))
        self.assertFalse(os.path.exists(self.folder))
        self.assertFalse(os.path.exists(self.aside))
        self.assert_kept()
        self.assertNotIn("project-a", self.store._cache)
        self.assertGreater(self.store.generation("project-a"), generation)
        # The same ID again sees no old set.
        self.assertEqual(self.sets(project_id="project-a"), [])
        self.assertEqual(self.sets(self.fresh(), project_id="project-a"), [])
        self.assertEqual(len(self.sets(project_id="project-ab")), 1)

    def test_a_project_without_sets_changes_nothing(self):
        self.assertEqual(self.delete_project("project-none"), (False, "done"))
        self.assertFalse(os.path.exists(os.path.join(self.root, "project-none.deleting")))
        self.assertEqual(sorted(os.listdir(self.root)),
                         ["project-a", "project-ab", "readme.txt"])

    def test_the_folder_is_set_aside_by_one_rename_without_a_yield(self):
        with mock.patch.object(stakeout.asyncio, "sleep_ms",
                               side_effect=AssertionError("yield")):
            self.assertIs(self.store.begin_project_delete("project-a"), True)
        self.assertFalse(os.path.exists(self.folder))
        self.assertEqual(sorted(os.listdir(self.aside)),
                         sorted(["notes.txt", SAMPLE_SET_ID + ".json",
                                 SAMPLE_SET_ID + ".json.tmp"]))
        self.assert_kept()

    def test_refusals_change_nothing(self):
        before = self.snapshot()
        self.store.busy = True
        error = self.refused("storage_busy", self.store.begin_project_delete, "project-a")
        self.assertIn("change is running", error.message)
        self.store.busy = False
        self.store._readers["project-a"] = 1
        error = self.refused("storage_busy", self.store.begin_project_delete, "project-a")
        self.assertIn("download", error.message)
        self.store._readers["project-a"] = 0
        # An open download of another project does not block.
        self.store._readers["project-ab"] = 1
        os.makedirs(self.aside)
        error = self.refused("stakeout_cleanup_pending", self.store.begin_project_delete,
                             "project-a")
        self.assertEqual(error.status, "409 Conflict")
        os.rmdir(self.aside)
        self.store._blocked.add("project-a")
        error = self.refused("stakeout_recovery_pending", self.store.begin_project_delete,
                             "project-a")
        self.assertEqual(error.status, "409 Conflict")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(sorted(os.listdir(self.root)),
                         ["project-a", "project-ab", "readme.txt"])

    def test_a_failed_rename_is_507_with_nothing_changed(self):
        before = self.snapshot()
        with mock.patch.object(stakeout.os, "rename", side_effect=OSError(5, "io")):
            error = self.refused("stakeout_delete_failed", self.store.begin_project_delete,
                                 "project-a")
        self.assertEqual(error.status, "507 Insufficient Storage")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.store._blocked, set())
        stat = os.stat

        def failing_stat(path, *args, **kwargs):
            if path.endswith(".deleting"):
                raise OSError(5, "io")
            return stat(path, *args, **kwargs)

        with mock.patch.object(stakeout.os, "stat", failing_stat), \
                mock.patch.object(stakeout.os, "rename", side_effect=AssertionError):
            self.refused("stakeout_delete_failed", self.store.begin_project_delete,
                         "project-a")
        self.assertEqual(self.snapshot(), before)

    def test_a_rename_that_moves_and_then_raises_counts(self):
        rename = os.rename

        def moved_then_raised(source, target):
            rename(source, target)
            raise OSError(5, "io")

        with mock.patch.object(stakeout.os, "rename", moved_then_raised):
            self.assertIs(self.store.begin_project_delete("project-a"), True)
        self.assertTrue(os.path.isdir(self.aside))
        self.assertEqual(self.store._blocked, set())

    def test_a_rename_whose_outcome_is_unknown_blocks_the_project(self):
        stat = os.stat
        failing = []

        def failing_stat(path, *args, **kwargs):
            if failing:
                raise OSError(5, "io")
            return stat(path, *args, **kwargs)

        def rename(source, target):
            failing.append(True)
            raise OSError(5, "io")

        with mock.patch.object(stakeout.os, "stat", failing_stat), \
                mock.patch.object(stakeout.os, "rename", rename):
            self.refused("stakeout_delete_failed", self.store.begin_project_delete,
                         "project-a")
        self.assertEqual(self.store._blocked, {"project-a"})
        self.assertEqual(self.log.call_args[0][0], "ERROR")

    def test_undo_puts_the_folder_back(self):
        before = self.snapshot()
        self.store.begin_project_delete("project-a")
        self.store.undo_project_delete("project-a")
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(os.path.exists(self.aside))
        self.assertEqual(self.store._blocked, set())
        self.assertEqual(len(self.sets(project_id="project-a")), 1)

    def test_a_failed_undo_blocks_the_project(self):
        self.store.begin_project_delete("project-a")
        self.log.reset_mock()
        with mock.patch.object(stakeout.os, "rename", side_effect=OSError(5, "io")):
            self.store.undo_project_delete("project-a")
        self.assertTrue(os.path.isdir(self.aside))
        self.assertEqual(self.store._blocked, {"project-a"})
        self.assertEqual(self.log.call_args[0][0], "ERROR")
        for status in ("409 Conflict", "503 Service Unavailable"):
            error = self.refused("stakeout_recovery_pending", self.store.refuse_blocked,
                                 "project-a", status)
            self.assertEqual(error.status, status)
        # Without a project: any blocked project refuses.
        self.refused("stakeout_recovery_pending", self.store.refuse_blocked)
        self.store.refuse_blocked("project-ab")
        self.store.refuse_blocked(None)
        self.fresh().refuse_blocked()

    def test_a_cleanup_failure_at_any_file_is_pending_and_the_next_start_finishes(self):
        names = sorted(os.listdir(self.folder))
        for index in range(len(names) + 1):
            with self.subTest(failing=index):
                if not os.path.exists(self.folder):
                    self.add(sample_package("project-a"))
                    self.write(os.path.join(self.folder, "notes.txt"), b"{}\n")
                    self.write(os.path.join(self.folder, SAMPLE_SET_ID + ".json.tmp"),
                               b"x")
                calls = []
                remove, rmdir = os.remove, os.rmdir

                def failing(real):
                    def call(path):
                        calls.append(path)
                        if len(calls) == index + 1:
                            raise OSError(5, "io")
                        real(path)
                    return call

                self.log.reset_mock()
                with mock.patch.object(stakeout.os, "remove", failing(remove)), \
                        mock.patch.object(stakeout.os, "rmdir", failing(rmdir)):
                    self.assertEqual(self.delete_project(), (True, "pending"))
                self.assertEqual(self.log.call_args[0][0], "WARN")
                self.assertTrue(os.path.isdir(self.aside))
                self.assertEqual(len(os.listdir(self.aside)), max(0, len(names) - index))
                self.assert_kept()
                # The next start removes the rest; the project is gone.
                self.reconcile("project-ab", store=self.fresh())
                self.assertFalse(os.path.exists(self.aside))
                self.assert_kept()

    def test_the_cleanup_yields_after_every_file(self):
        for number in range(6):
            self.write(os.path.join(self.folder, "extra-%d" % number), b"x")
        files = len(os.listdir(self.folder))
        sleeps = []
        real = stakeout.asyncio.sleep_ms

        async def sleep_ms(ms):
            sleeps.append(sorted(os.listdir(self.aside)) if os.path.exists(self.aside)
                          else None)
            await real(ms)

        self.store.begin_project_delete("project-a")
        with mock.patch.object(stakeout.asyncio, "sleep_ms", sleep_ms):
            self.assertEqual(run(self.store.finish_project_delete("project-a")), "done")
        # One pause per file and one after the folder's removal.
        self.assertEqual(len(sleeps), files + 1)
        self.assertEqual([len(names) for names in sleeps[:-1]],
                         list(range(files - 1, -1, -1)))
        self.assertIsNone(sleeps[-1])

    def test_a_deletion_forgets_damage_marks_of_the_project_only(self):
        self.store._damaged.update({self.path(project_id="project-a"),
                                    self.path(project_id="project-ab")})
        self.delete_project()
        self.assertEqual(self.store._damaged, {self.path(project_id="project-ab")})

    # --- start-up reconciliation -------------------------------------------

    def test_a_live_project_gets_its_folder_back(self):
        before = self.snapshot()
        self.store.begin_project_delete("project-a")
        store = self.fresh()
        self.reconcile("project-a", "project-ab", store=store)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(os.path.exists(self.aside))
        self.assertEqual(store._blocked, set())
        self.assertEqual(self.download(store=store, project_id="project-a"),
                         before["project-a/%s.json" % SAMPLE_SET_ID])

    def test_an_absent_project_is_removed(self):
        self.store.begin_project_delete("project-a")
        store = self.fresh()
        self.reconcile("project-ab", store=store)
        self.assertFalse(os.path.exists(self.aside))
        self.assertFalse(os.path.exists(self.folder))
        self.assert_kept()
        self.assertEqual(store._blocked, set())

    def test_both_folders_stay_and_the_project_is_blocked(self):
        self.store.begin_project_delete("project-a")
        # Both folders can only come from outside the API.
        self.write(self.path(project_id="project-a"),
                   self.read(os.path.join(self.aside, SAMPLE_SET_ID + ".json")))
        before = self.snapshot()
        store = self.fresh()
        self.reconcile("project-a", "project-ab", store=store)
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(os.path.isdir(self.aside))
        self.assertEqual(store._blocked, {"project-a"})
        self.assertEqual(self.log.call_args[0][0], "WARN")
        self.refused("stakeout_recovery_pending", store.refuse_blocked, "project-a")

    def test_errors_leave_the_folder(self):
        self.store.begin_project_delete("project-a")
        before = self.snapshot()
        cases = (("rename", "project-a", {"project-a"}),
                 ("stat", "project-a", {"project-a"}),
                 ("remove", None, set()), ("listdir", None, set()))
        for name, live, blocked in cases:
            with self.subTest(name):
                store = self.fresh()
                real = getattr(os, name)

                def failing(path, *args, **kwargs):
                    if ".deleting" in path or (name == "stat" and
                                               path.endswith("project-a")):
                        raise OSError(5, "io")
                    return real(path, *args, **kwargs)

                self.log.reset_mock()
                with mock.patch.object(stakeout.os, name, failing):
                    self.reconcile(*([live] if live else []), store=store)
                self.assertEqual(self.snapshot(), before)
                self.assertTrue(os.path.isdir(self.aside))
                self.assertEqual(store._blocked, blocked)
                self.assertEqual(self.log.call_args[0][0], "WARN")
        with mock.patch.object(stakeout.os, "listdir", side_effect=OSError(5, "io")):
            self.reconcile()
        self.assertEqual(self.snapshot(), before)

    def test_an_unlisted_root_blocks_every_project_until_the_next_start(self):
        self.store.begin_project_delete("project-a")
        before = self.snapshot()
        store = self.fresh()
        real = os.listdir

        def listdir(path):
            if path == self.root:
                raise OSError(5, "io")
            return real(path)

        with mock.patch.object(stakeout.os, "listdir", listdir):
            self.reconcile("project-a", "project-ab", store=store)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.log.call_args[0][0], "WARN")
        # Whether a live project's folder is set aside is unknown: archiving
        # any project could let the next start delete its sets.
        for project_id in ("project-a", "project-ab", "project-x", None):
            with self.subTest(project_id=project_id):
                error = self.refused("stakeout_recovery_pending", store.refuse_blocked,
                                     project_id)
                self.assertEqual(error.status, "409 Conflict")
        self.refused("stakeout_recovery_pending", store.refuse_blocked)
        self.refused("stakeout_recovery_pending", store.begin_project_delete, "project-ab")
        self.assertEqual(self.snapshot(), before)
        # The next start lists the root: the folder is back, its sets intact.
        store = self.fresh()
        self.reconcile("project-a", "project-ab", store=store)
        store.refuse_blocked()
        self.assertFalse(os.path.exists(self.aside))
        self.assertEqual(self.snapshot(), {
            name.replace("project-a.deleting/", "project-a/", 1): data
            for name, data in before.items()})
        self.assertEqual([item["state"] for item in self.sets(store, project_id="project-a")],
                         ["ok"])

    def test_only_set_aside_folders_are_touched(self):
        # Plain folders of projects that are not live (archived, for
        # instance) are never swept; invalid names are ignored.
        os.makedirs(os.path.join(self.root, "bad.id.deleting"))
        os.makedirs(os.path.join(self.root, ".deleting"))
        before = self.snapshot()
        with self.immutable_patches():
            self.reconcile()
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(os.path.isdir(os.path.join(self.root, "bad.id.deleting")))
        shutil.rmtree(self.root)
        store = self.fresh()
        self.reconcile(store=store)
        self.assertFalse(os.path.exists(self.root))
        # A missing root has nothing set aside: no project is blocked.
        store.refuse_blocked()

    def test_a_set_aside_name_that_is_not_a_folder_is_left_alone(self):
        # A file called "<project_id>.deleting" is no set-aside folder: it is
        # neither removed (project gone) nor renamed (project live) nor blocking.
        for name, live in (("project-x.deleting", ()), ("project-a.deleting", ("project-a",))):
            with self.subTest(name=name):
                self.write(os.path.join(self.root, name), b"not a folder")
                before = self.snapshot()
                store = self.fresh()
                with self.immutable_patches():
                    self.reconcile(*live, store=store)
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(store._blocked, set())
                os.remove(os.path.join(self.root, name))

    def test_the_warning_says_which_part_of_the_rename_back_failed(self):
        self.store.begin_project_delete("project-a")
        store = self.fresh()
        with mock.patch.object(stakeout.os, "rename", side_effect=OSError(5, "io")):
            self.reconcile("project-a", store=store)
        self.assertIn("could not be renamed back", self.log.call_args[0][2])
        self.assertNotIn("stays beside", self.log.call_args[0][2])
        self.write(self.path(project_id="project-a"),
                   self.read(os.path.join(self.aside, SAMPLE_SET_ID + ".json")))
        self.reconcile("project-a", store=self.fresh())
        self.assertIn("stays beside", self.log.call_args[0][2])

    def test_a_project_id_that_cannot_have_sets_deletes_without_the_store(self):
        # Restored surveys may carry any project ID; no folder can exist then.
        before = self.snapshot()
        for project_id in ("Projekt 1", "a" * 65, "x.y", ""):
            with self.subTest(project_id=project_id), self.immutable_patches(), \
                    mock.patch.object(stakeout.os, "stat", side_effect=AssertionError), \
                    mock.patch.object(stakeout.os, "listdir", side_effect=AssertionError):
                self.assertEqual(self.delete_project(project_id), (False, "done"))
                self.store.refuse_blocked(project_id)
        self.assertEqual(self.snapshot(), before)
        # The start-up hook and the guards take such live projects in their stride.
        self.store.begin_project_delete("project-a")
        self.reconcile("Projekt 1", "project-ab")
        self.assertFalse(os.path.exists(self.aside))
        self.assert_kept()

    def test_set_changes_wait_while_a_set_aside_folder_exists(self):
        # A deletion's leftover and a new folder of the same project must
        # never coexist: imports and set deletions wait for the next start.
        self.store.begin_project_delete("project-a")
        self.assertTrue(os.path.isdir(self.aside))
        aside = self.snapshot()
        for store in (self.store, self.fresh()):
            error = self.refused("stakeout_cleanup_pending", self.add,
                                 sample_package("project-a"), store=store)
            self.assertEqual(error.status, "409 Conflict")
            self.assertFalse(os.path.exists(self.folder))
            self.assertIs(store.busy, False)
        self.assertEqual(self.snapshot(), aside)
        # Both folders present (made outside the API): a set deletion waits too.
        os.makedirs(self.folder)
        self.write(self.path(project_id="project-a"),
                   self.read(os.path.join(self.aside, SAMPLE_SET_ID + ".json")))
        both = self.snapshot()
        self.store.invalidate("project-a")
        for store in (self.store, self.fresh()):
            error = self.refused("stakeout_cleanup_pending", store.delete_set, "project-a",
                                 FP, SAMPLE_SET_ID, 0)
            self.assertEqual(error.status, "409 Conflict")
            self.refused("stakeout_cleanup_pending", self.add,
                         make_package(targets=3, project_id="project-a"), store=store)
        self.assertEqual(self.snapshot(), both)

    def test_a_folder_set_aside_during_a_change_is_seen_right_before_it(self):
        # The guard runs after the last yield: a .deleting that appears in the
        # pause before the folders are made, or during a cold lookup, refuses.
        real_pause = stakeout._pause

        def planting(until):
            async def pause(meter):
                if until() and not os.path.isdir(self.aside):
                    os.makedirs(self.aside)
                await real_pause(meter)
            return pause

        shutil.rmtree(self.folder)
        store = self.fresh()
        validated = []
        real_validate = stakeout.validate_import

        async def validate(*args):
            plan = await real_validate(*args)
            validated.append(True)
            return plan

        # The first pause after the validation is the one before the folders.
        with mock.patch.object(stakeout, "_pause", planting(lambda: validated)), \
                mock.patch.object(stakeout, "validate_import", validate):
            self.refused("stakeout_cleanup_pending", self.add, sample_package("project-a"),
                         store=store)
        self.assertEqual(validated, [True])
        self.assertFalse(os.path.exists(self.folder))
        os.rmdir(self.aside)
        self.add(sample_package("project-a"), store=store)
        store = self.fresh()
        with mock.patch.object(stakeout, "_pause", planting(lambda: True)):
            self.refused("stakeout_cleanup_pending", store.delete_set, "project-a", FP,
                         SAMPLE_SET_ID, 0)
        self.assertTrue(os.path.exists(self.path(project_id="project-a")))

    def test_the_heartbeat_is_kept_during_a_long_cleanup(self):
        for number in range(40):
            self.write(os.path.join(self.folder, "extra-%02d" % number), b"x")
        files = len(os.listdir(self.folder))
        self.store.begin_project_delete("project-a")
        beats, sleeps = [], []
        real = stakeout.asyncio.sleep_ms
        import state

        async def sleep_ms(ms):
            sleeps.append(len(beats))
            await real(ms)

        with mock.patch.object(state.app, "beat", side_effect=beats.append), \
                mock.patch.object(stakeout.asyncio, "sleep_ms", sleep_ms):
            self.reconcile("project-ab", store=self.fresh())
        self.assertFalse(os.path.exists(self.aside))
        self.assertEqual(set(beats), {"tracking"})
        # A beat before every yield: one per file, one for the folder's
        # removal and one after the folder.
        self.assertEqual(sleeps, list(range(1, files + 3)))


class TestImportCapacity(_StoreCase):
    """The import charges its file and never measures the flash twice (C4)."""

    def test_charge_comes_right_after_the_commit_and_is_timed(self):
        clock = FakeClock(self, suspension=3)
        charged = []

        def charge(size):
            charged.append((size, os.path.exists(self.path())))
            clock.advance(9)

        answer = self.add(charge=charge, remeasure=mock.Mock(side_effect=AssertionError))
        self.assertEqual(charged, [(answer["bytes"], True)])
        self.assertEqual(answer["timing_ms"]["capacity_refresh"], 9)

    def test_the_failure_path_after_the_commit_is_charged_too(self):
        charged = []
        with mock.patch.object(stakeout.TargetStore, "_remember", side_effect=MemoryError):
            self.refused_import("import_committed_response_failed", charge=charged.append)
        self.assertEqual(charged, [os.path.getsize(self.path())])

    def test_failures_before_the_commit_charge_nothing(self):
        charged = []
        remeasured = []
        for label, patch in (("reserve", mock.patch.object(self, "ok", lambda n: False)),
                             ("write", mock.patch.object(stakeout.os, "rename",
                                                         side_effect=OSError(5, "io")))):
            with self.subTest(label), patch:
                with self.assertRaises(stakeout.StakeoutError):
                    self.add(charge=charged.append,
                             remeasure=lambda: remeasured.append(True))
        self.assertEqual((charged, remeasured), ([], []))

    def test_an_unremovable_tmp_makes_the_next_check_measure(self):
        remeasured = []
        with mock.patch.object(stakeout.os, "rename", side_effect=OSError(5, "io")), \
                mock.patch.object(stakeout.os, "remove", side_effect=OSError(5, "io")):
            self.refused_import("stakeout_write_failed",
                                remeasure=lambda: remeasured.append(True))
        self.assertEqual(remeasured, [True])
        self.assertEqual(self.files(), [self.name() + ".tmp"])


class FakeClock:
    """A tick counter that moves only when a test moves it.

    Wraps like MicroPython's ticks_ms (period 2**30) and gives ticks_diff its
    wrap-safe meaning. A service pause (asyncio.sleep_ms) costs `suspension`
    ms of other tasks' time and then really yields to the event loop.
    """
    PERIOD = 1 << 30

    def __init__(self, test, start=1000, suspension=0):
        self.now = start
        self.suspension = suspension
        self.sleeps = 0
        for patch in (mock.patch.object(stakeout.time, "ticks_ms", self.ticks_ms),
                      mock.patch.object(stakeout.time, "ticks_diff", self.ticks_diff),
                      mock.patch.object(stakeout.asyncio, "sleep_ms", self.sleep_ms)):
            patch.start()
            test.addCleanup(patch.stop)

    def ticks_ms(self):
        return self.now % self.PERIOD

    @classmethod
    def ticks_diff(cls, a, b):
        half = cls.PERIOD >> 1
        return ((a - b + half) & (cls.PERIOD - 1)) - half

    def advance(self, ms):
        self.now += ms

    async def sleep_ms(self, ms):
        self.sleeps += 1
        self.now += self.suspension
        await asyncio.sleep(0)


class _CostModel:
    """A deterministic model of what the stakeout store costs on the device.

    One clock for one sequential workload. Every file, directory, hash and
    JSON operation of the store charges a modeled number of milliseconds
    (ASSUMPTIONS, not measurements: the device checks in the validation
    protocol decide). A service pause ends a stretch; the longest stretch
    without a pause is the modeled block. The injected costs of single slow
    calls (the parse of the package, gc.collect, the flash probe and the
    serialization of one huge part) are charged like any other but also
    summed per stretch, so the budget can exclude them (`worst`) while the
    diagnostics must still see them (`raw_worst`).
    """
    ENTRY = 0.25        # ms per directory entry listed
    CALL = {"ilistdir": 20, "listdir": 20, "stat": 3, "mkdir": 40, "rename": 60,
            "remove": 50, "rmdir": 40, "open": 10, "seek": 1, "read": 3,
            "write": 8, "flush": 30, "close": 40}
    HASH_PER_KIB = 0.5
    DUMPS_PER_KIB = 2.0         # JSON text written, 1 ms per 512 bytes
    VALIDATE_PER_UNIT = {"targets": 1.0, "coords": 0.15}  # per target/part head, per vertex
    SUSPENSION = 2              # ms the other tasks keep the loop per pause
    # Injected single costs.
    PARSE_PER_KIB = 7.0         # ujson.loads of the package (about 819 ms for P128)
    GC = 100
    STATVFS = 600
    HUGE_DUMPS = 60000          # a dumps result this long is one huge part

    def __init__(self):
        self.now = 1000.0
        self.since = self.now
        self.injected = 0.0
        self.calls = []         # (kind, ms) of every injected call
        self.sleeps = 0
        self.validating = False
        self.worst = 0
        self.raw_worst = 0

    # --- the clock -------------------------------------------------------

    def ticks_ms(self):
        return int(self.now) % FakeClock.PERIOD

    def charge(self, ms):
        self.now += ms

    def inject(self, kind, ms):
        self.calls.append((kind, ms))
        self.now += ms
        self.injected += ms

    def close(self):
        raw = self.now - self.since
        self.raw_worst = max(self.raw_worst, raw)
        self.worst = max(self.worst, raw - self.injected)

    async def sleep_ms(self, ms):
        self.close()
        self.sleeps += 1
        self.now += self.SUSPENSION
        await asyncio.sleep(0)
        self.since = self.now
        self.injected = 0.0

    def begin(self):
        self.worst = self.raw_worst = 0
        self.since = self.now
        self.injected = 0.0
        del self.calls[:]

    def end(self):
        self.close()
        return int(self.worst), int(self.raw_worst)

    # --- the patched environment -----------------------------------------

    def installed(self):
        model = self
        real_os, real_json, real_hashlib = os, stakeout.ujson, hashlib
        real_validate, real_due = stakeout.validate_import, stakeout._Pacer.due

        class Files:
            def __init__(self, handle):
                self.handle = handle

            def read(self, size=-1):
                model.charge(model.CALL["read"])
                return self.handle.read(size)

            def write(self, data):
                model.charge(model.CALL["write"])
                return self.handle.write(data)

            def seek(self, *args):
                model.charge(model.CALL["seek"])
                return self.handle.seek(*args)

            def flush(self):
                model.charge(model.CALL["flush"])
                self.handle.flush()

            def close(self):
                model.charge(model.CALL["close"])
                self.handle.close()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.close()

        class Os:
            def __getattr__(self, name):
                return getattr(real_os, name)

            def ilistdir(self, path):
                entries = real_os.ilistdir(path)
                model.charge(model.CALL["ilistdir"])
                return self.counted(entries)

            @staticmethod
            def counted(entries):
                for entry in entries:
                    model.charge(model.ENTRY)
                    yield entry

            def listdir(self, path):
                names = real_os.listdir(path)
                model.charge(model.CALL["listdir"] + model.ENTRY * len(names))
                return names

        def operation(name):
            def call(*args, **kwargs):
                result = getattr(real_os, name)(*args, **kwargs)
                model.charge(model.CALL[name])
                return result
            return call

        for name in ("stat", "mkdir", "rename", "remove", "rmdir"):
            setattr(Os, name, staticmethod(operation(name)))

        def open_file(path, mode="r", *args, **kwargs):
            model.charge(model.CALL["open"])
            return Files(_real_open(path, mode, *args, **kwargs))

        def dumps(item):
            text = real_json.dumps(item)
            ms = len(text) / 1024 * model.DUMPS_PER_KIB
            if len(text) > model.HUGE_DUMPS:
                model.inject("dumps", ms)
            else:
                model.charge(ms)
            return text

        def loads(data):
            if len(data) > 16384:
                model.inject("parse", len(data) / 1024 * model.PARSE_PER_KIB)
            else:
                model.charge(len(data) / 1024 * model.DUMPS_PER_KIB)
            return real_json.loads(data)

        class Digest:
            def __init__(self, *args):
                self.real = real_hashlib.sha256(*args)

            def update(self, data):
                model.charge(len(data) / 1024 * model.HASH_PER_KIB)
                self.real.update(data)

            def __getattr__(self, name):
                return getattr(self.real, name)

        async def validate(payload, meter=None):
            model.validating = True
            try:
                return await real_validate(payload, meter)
            finally:
                model.validating = False

        def due(pacer, kind, units=1):
            if model.validating:
                model.charge(model.VALIDATE_PER_UNIT[kind] * units)
            return real_due(pacer, kind, units)

        stack = contextlib.ExitStack()
        for target, name, value, create in (
                (stakeout.time, "ticks_ms", self.ticks_ms, False),
                (stakeout.time, "ticks_diff", FakeClock.ticks_diff, False),
                (stakeout.asyncio, "sleep_ms", self.sleep_ms, False),
                (stakeout, "os", Os(), False),
                (stakeout, "open", open_file, True),
                (stakeout, "ujson", types.SimpleNamespace(dumps=dumps, loads=loads), False),
                (stakeout, "hashlib", types.SimpleNamespace(sha256=Digest), False),
                (stakeout, "gc", types.SimpleNamespace(
                    collect=lambda: model.inject("gc", model.GC)), False),
                (stakeout, "validate_import", validate, False),
                (stakeout._Pacer, "due", due, False)):
            stack.enter_context(mock.patch.object(target, name, value, create=create))
        return stack


class TestWorkloadModel(_StoreCase):
    """G4/G5 on the host: every entry point on a heavy project, on a cost model."""

    BUDGET = 250
    STRAYS = 300

    def package(self, offset, huge=False):
        if huge:
            return make_package(targets=3, lines=1, coords_per_line=2000, offset=offset)
        return make_package(targets=20, lines=2, coords_per_line=200, offset=offset)

    def build_project(self):
        """Four sets (two with a damaged trailer), stale .tmp files, many strays."""
        self.ids = {}
        for label, offset, huge in (("huge", 0, True), ("plain", 1, False),
                                    ("bad_trailer", 2, False), ("truncated", 3, False)):
            self.ids[label] = self.add(self.package(offset, huge))["set_id"]
        path = lambda label: self.path(self.ids[label])
        data = self.read(path("bad_trailer"))
        self.write(path("bad_trailer"), data[:-stakeout.TRAILER_BYTES]
                   + b"x" * stakeout.TRAILER_BYTES)
        self.write(path("truncated"), self.read(path("truncated"))[:1000])
        folder = os.path.join(self.root, PID)
        self.ids["incomplete"] = "so-0000000e"
        self.write(os.path.join(folder, "so-0000000e.json.tmp"), b'{"kind"')
        self.write(path("huge") + ".tmp", self.read(path("huge"))[:5000])
        for i in range(self.STRAYS):
            name = ("stray-%03d.txt", "so-%08x.bak", "SO-%08X.json", "note %03d")[i % 4]
            self.write(os.path.join(folder, name % i), b"x" * (i % 7))
        for i in range(5):
            os.makedirs(os.path.join(folder, "folder-%d" % i))
        os.makedirs(os.path.join(folder, "so-0000000d.json"))

    def measure(self, label, call, *args, refused=None):
        self.model.begin()
        try:
            result = run(call(*args))
        except stakeout.StakeoutError as error:
            self.assertEqual(error.code, refused, label)
            result = None
        else:
            self.assertIsNone(refused, label)
        self.blocks[label] = self.model.end()
        return result

    def test_every_entry_point_stays_within_the_modeled_block_budget(self):
        self.build_project()
        ids = self.ids
        self.blocks = {}
        self.model = model = _CostModel()
        with model.installed():
            # Overview: a cold listing, then a cached one.
            store = self.fresh()
            sets = self.measure("overview, cold", store.sets, PID, FP)
            self.assertEqual({entry["set_id"]: entry["state"] for entry in sets},
                             {ids["huge"]: "ok", ids["plain"]: "ok",
                              ids["bad_trailer"]: "damaged", ids["truncated"]: "damaged",
                              ids["incomplete"]: "incomplete"})
            self.assertEqual(self.blocks["overview, cold"][1], store.last_load_block_ms)
            self.assertGreater(store.last_load_ms, store.last_load_block_ms)
            self.measure("overview, cached", store.sets, PID, FP)
            self.assertEqual((store.last_load_ms, store.last_load_block_ms), (0, 0))
            # Downloads: the 2000-vertex set, a small one, the damaged ones refused.
            for label, refused in (("huge", None), ("plain", None),
                                   ("bad_trailer", "set_damaged"),
                                   ("truncated", "set_damaged"),
                                   ("incomplete", "set_damaged")):
                writer = _HeadWriter()
                self.measure("download " + label, store.send_set, writer, PID, FP,
                             ids[label], _head, refused=refused)
                if refused is None:
                    self.assertEqual(writer.body, self.read(self.path(ids[label])))
            # Deleting the damaged sets from a cold store, then importing a second
            # huge set into the project, again cold, with the slow calls injected.
            for label in ("bad_trailer", "truncated", "incomplete"):
                self.measure("delete " + label, self.fresh().delete_set, PID, FP,
                             ids[label], 0)
            self.assertEqual(sorted(self.states().values()), ["ok", "ok"])
            body = compact(self.package(5, huge=True))
            self.assertLess(len(body), stakeout.MAX_IMPORT_BYTES)

            def probe(needed):
                model.inject("statvfs", model.STATVFS)
                return True

            importer = self.fresh()
            answer = self.measure("import", importer.import_package, body, self.check,
                                  probe, lambda size: None)
            timing = answer["timing_ms"]
            injected = dict(model.calls)
            worst, raw = self.blocks["import"]
            # Nothing is hidden: the diagnostics see every injected call.
            self.assertEqual(timing["gc"], model.GC)
            self.assertGreaterEqual(timing["max_capacity"], model.STATVFS)
            self.assertGreaterEqual(timing["max_serialize"], int(injected["dumps"]))
            self.assertGreaterEqual(timing["longest_block"], int(injected["parse"]))
            self.assertAlmostEqual(timing["longest_block"], raw, delta=2)
            self.assertGreater(raw, 2 * self.BUDGET)
            # Project deletion: the folder is set aside, then removed file by file.
            # (A stray subdirectory would keep it pending, see known-limitations.)
            for name in os.listdir(os.path.join(self.root, PID)):
                if os.path.isdir(os.path.join(self.root, PID, name)):
                    os.rmdir(os.path.join(self.root, PID, name))
            self.measure("delete project", self.delete_whole_project, importer)
            self.assertFalse(os.path.exists(os.path.join(self.root, PID)))
            self.assertFalse(os.path.exists(os.path.join(self.root, PID + ".deleting")))
            # Start-up settling: one folder of a deleted project, one to put back.
            gone = os.path.join(self.root, "project-gone.deleting")
            for i in range(120):
                self.write(os.path.join(gone, "so-%08x.json" % i), b"{}")
            self.write(os.path.join(self.root, "project-live.deleting",
                                    "so-0000000a.json"), b"{}")
            self.measure("reconcile", self.fresh().reconcile_deletions,
                         _Survey("project-live"))
            self.assertFalse(os.path.exists(gone))
            self.assertTrue(os.path.exists(os.path.join(self.root, "project-live",
                                                        "so-0000000a.json")))
        for label, (worst, raw) in sorted(self.blocks.items()):
            with self.subTest(label):
                self.assertLessEqual(worst, self.BUDGET, "%s: %s" % (label, self.blocks))
        for label in ("overview, cold", "download huge", "import", "delete project",
                      "reconcile"):
            self.assertGreater(self.blocks[label][1], 0, label)
        self.assertGreater(model.sleeps, 500)

    async def delete_whole_project(self, store):
        """What the web adapter does around the journal append, without the tracker."""
        self.assertTrue(store.begin_project_delete(PID))
        return await store.finish_project_delete(PID)


class TestMeterAndPacer(unittest.TestCase):
    """The per-operation meter and the pacer, on a clock the tests move."""

    def test_longest_block_counts_contiguous_local_work_not_suspensions(self):
        clock = FakeClock(self, suspension=500)
        timing = {key: 0 for key in stakeout.TIMING_KEYS}

        async def scenario():
            meter = stakeout._BlockMeter()
            clock.advance(30)                       # prefix work
            await stakeout._pause(meter)            # 500 ms in other tasks
            clock.advance(20)
            with mock.patch.object(stakeout.gc, "collect",
                                   lambda: clock.advance(40)):
                stakeout._timed_collect(timing)     # the timed gc.collect
            started = clock.ticks_ms()
            clock.advance(25)                       # statvfs
            meter.note("capacity", started)
            clock.advance(15)                       # suffix work
            meter.publish(timing)
            return meter

        meter = run(scenario())
        self.assertEqual(timing["gc"], 40)
        # 20 + 40 + 25 + 15 in one stretch; the 500 ms and the 30 ms before
        # the pause are not part of it.
        self.assertEqual(timing["longest_block"], 100)
        self.assertEqual(timing["pauses"], 1)
        self.assertEqual(timing["max_pause"], 500)
        self.assertEqual(timing["max_capacity"], 25)
        self.assertEqual(meter.suspended, 500)

    def test_prefix_stretch_is_measured_too(self):
        clock = FakeClock(self, suspension=10)

        async def scenario():
            meter = stakeout._BlockMeter()
            clock.advance(400)
            await stakeout._pause(meter)
            clock.advance(5)
            return meter.lap()

        self.assertEqual(run(scenario()), 400)

    def test_meters_of_overlapping_operations_are_independent(self):
        # While the first operation is suspended, a second one (an overview
        # listing, say) works and pauses on its own meter.
        clock = FakeClock(self, suspension=0)
        meters = {}

        async def other():
            meters["other"] = stakeout._BlockMeter()
            for _ in range(4):
                clock.advance(900)
                await stakeout._pause(meters["other"])

        async def first():
            meters["first"] = stakeout._BlockMeter()
            clock.advance(10)
            await stakeout._pause(meters["first"])
            clock.advance(20)
            await stakeout._pause(meters["first"])
            clock.advance(5)
            meters["first"].lap()

        async def scenario():
            await asyncio.gather(first(), other())

        run(scenario())
        first, other_meter = meters["first"], meters["other"]
        self.assertEqual((first.longest, first.pauses), (20, 2))
        self.assertEqual(first.largest, {kind: 0 for kind in stakeout._LARGEST})
        self.assertEqual((other_meter.longest, other_meter.pauses), (900, 4))
        self.assertGreaterEqual(first.max_pause, 900)

    def test_meter_survives_the_tick_counter_wrapping(self):
        clock = FakeClock(self, start=FakeClock.PERIOD - 60, suspension=30)

        async def scenario():
            meter = stakeout._BlockMeter()
            clock.advance(50)
            await stakeout._pause(meter)     # now past the wrap
            clock.advance(45)
            started = clock.ticks_ms()
            clock.advance(10)
            meter.note("read", started)
            return meter

        meter = run(scenario())
        self.assertLess(clock.ticks_ms(), 1000)
        self.assertEqual(meter.lap(), 55)
        self.assertEqual((meter.max_pause, meter.suspended), (30, 30))
        self.assertEqual(meter.largest["read"], 10)

    def test_pacer_yields_after_the_slice_for_many_tiny_items(self):
        clock = FakeClock(self, suspension=500)
        yields = []

        async def scenario():
            pacer = stakeout._Pacer(stakeout._BlockMeter())
            for item in range(1, 301):
                clock.advance(1)     # each vertex is tiny
                if pacer.due("coords"):
                    yields.append(item)
                    await pacer.pause()

        run(scenario())
        # 50 ms of local work, not the 128-vertex count; the 500 ms spent
        # suspended never counts as work.
        self.assertEqual(stakeout.SLICE_MS, 50)
        self.assertEqual(yields, [50, 100, 150, 200, 250, 300])

    def test_pacer_falls_back_to_the_counts_when_the_clock_stands_still(self):
        FakeClock(self)
        yields = {"targets": [], "coords": []}

        async def scenario():
            pacer = stakeout._Pacer(stakeout._BlockMeter())
            for kind in ("targets", "coords"):
                for item in range(1, 1001):
                    if pacer.due(kind):
                        yields[kind].append(item)
                        await pacer.pause()

        run(scenario())
        self.assertEqual(yields["targets"], list(range(32, 1001, 32)))
        self.assertEqual(yields["coords"], list(range(128, 1001, 128)))

    def test_pacer_counts_units_and_resets_after_a_pause(self):
        FakeClock(self)

        async def scenario():
            pacer = stakeout._Pacer(stakeout._BlockMeter())
            seen = [pacer.due("coords", 16) for _ in range(8)]
            await pacer.pause()
            return seen, pacer.due("coords", 16)

        seen, again = run(scenario())
        self.assertEqual(seen, [False] * 7 + [True])
        self.assertFalse(again)

    def test_pacer_survives_the_tick_counter_wrapping(self):
        clock = FakeClock(self, start=FakeClock.PERIOD - 20)
        pacer = stakeout._Pacer(stakeout._BlockMeter())
        clock.advance(49)
        self.assertFalse(pacer.due("coords"))
        clock.advance(1)
        self.assertTrue(pacer.due("coords"))

    def test_validation_yields_after_the_slice_and_keeps_the_count_backstop(self):
        clock = FakeClock(self)
        real_coord = stakeout._coord

        def slow_coord(*args):
            clock.advance(1)
            return real_coord(*args)

        pauses = []
        real_pause = stakeout._pause

        async def pause(meter):
            pauses.append(clock.now)
            await real_pause(meter)

        package = make_package(targets=1, lines=1, coords_per_line=500)
        with mock.patch.object(stakeout, "_pause", pause), \
                mock.patch.object(stakeout, "_coord", slow_coord):
            run(stakeout.validate_import(package))
        # The clock is looked at every COORD_STEP vertices: 64 ms of work is
        # the first look past the 50 ms slice (the count alone gives 3). The
        # 53 ms left over at the end of the vertices make the first target
        # yield once more.
        self.assertEqual(len(pauses), 501 // 64 + 1)


class TestValidationPacing(unittest.TestCase):
    """validate_import looks at the clock at every kind of item (C3)."""

    SLICE_COST = 2 * stakeout.SLICE_MS      # one such item uses up the slice

    def trace(self, package, **costs):
        """The checked items and the pauses between them, in order.

        costs maps a checker name (_part_head, _skipped, _target, _coord) to
        the ms one call of it takes on the fake clock.
        """
        clock = FakeClock(self)
        log = []
        counts = {}
        real_pause = stakeout._pause

        async def pause(meter):
            log.append(("pause",))
            await real_pause(meter)

        def timed(name):
            real = getattr(stakeout, name)

            def checker(*args):
                counts[name] = counts.get(name, 0) + 1
                log.append((name, counts[name]))
                clock.advance(costs.get(name, 0))
                return real(*args)
            return checker

        patches = [mock.patch.object(stakeout, "_pause", pause)]
        patches += [mock.patch.object(stakeout, name, timed(name))
                    for name in ("_part_head", "_skipped", "_target", "_coord")]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        plan = run(stakeout.validate_import(package))
        return log, plan

    def assert_pause_after_each(self, log, name, expected, every=1):
        items = [index for index, entry in enumerate(log) if entry[0] == name]
        self.assertEqual(len(items), expected)
        for index in items[every - 1::every]:
            self.assertEqual(log[index + 1], ("pause",), (name, log[index]))

    def test_every_part_head_is_a_service_point(self):
        points = make_package(targets=1)
        for part_index in range(1, 500):
            points["parts"].append(dict(points["parts"][0], feature_index=part_index))
            points["targets"].append(dict(points["targets"][0], part_index=part_index,
                                          lat_e8=5100000000 + part_index))
        log, plan = self.trace(points, _part_head=self.SLICE_COST)
        self.assertEqual(len(plan["parts"]), 500)
        self.assert_pause_after_each(log, "_part_head", 500)

    def test_every_skip_entry_is_a_service_point(self):
        package = sample_package()
        entry = package["source"]["skipped"][0]
        package["source"]["skipped"] = [dict(entry, feature_index=i) for i in range(50)]
        package["source"]["skipped_total"] = 50
        package["source"]["features"] = max(package["source"]["features"], 60)
        log, plan = self.trace(package, _skipped=self.SLICE_COST)
        self.assertEqual(len(plan["source"]["skipped"]), 50)
        self.assert_pause_after_each(log, "_skipped", 50)

    def test_every_target_is_a_service_point(self):
        log, plan = self.trace(make_package(targets=500), _target=self.SLICE_COST)
        self.assertEqual(len(plan["targets"]), 500)
        self.assert_pause_after_each(log, "_target", 500)

    def test_vertices_are_looked_at_every_sixteen(self):
        package = make_package(targets=1, lines=1, coords_per_line=2000)
        log, plan = self.trace(package, _coord=stakeout.SLICE_MS // 8)
        coords = sum(len(part["coords_e8"]) for part in plan["parts"])
        self.assertEqual(coords, 2000)
        self.assert_pause_after_each(log, "_coord", 2000, every=stakeout.COORD_STEP)
        # Nothing else yields: 2000 / 16 pauses, none between two looks.
        self.assertEqual(sum(1 for entry in log if entry == ("pause",)), 2000 // 16)

    def test_plan_ids_and_values_do_not_depend_on_the_pacing(self):
        package = sample_package()
        calm = run(stakeout.validate_import(copy.deepcopy(package)))
        slow = self.trace(copy.deepcopy(package), _part_head=self.SLICE_COST,
                          _skipped=self.SLICE_COST, _target=self.SLICE_COST,
                          _coord=self.SLICE_COST)[1]
        self.assertEqual(slow, calm)
        self.assertEqual(slow["set_id"], SAMPLE_SET_ID)
        self.assertEqual(slow["set_id"], canonical_set_id(package))
        for part in slow["parts"]:
            self.assertTrue(part["part_id"].startswith(SAMPLE_SET_ID + "/"))
        for target in slow["targets"]:
            self.assertEqual(target["tid"], "%s/%d" % (target["part_id"], target["seq"]))

    def test_exact_e8_values_survive_the_pacing(self):
        package = make_package(targets=2, lines=1, coords_per_line=40)
        package["parts"][1]["coords_e8"][3] = [-17999999999, 8999999999, -1234]
        package["parts"][1]["coords_e8"][7] = [18000000000, -9000000000, None]
        log, plan = self.trace(package, _coord=stakeout.SLICE_MS)
        self.assertEqual(plan["parts"][1]["coords_e8"], package["parts"][1]["coords_e8"])
        self.assertGreater(sum(1 for entry in log if entry == ("pause",)), 2)
        for sent, kept in zip(package["targets"], plan["targets"]):
            self.assertEqual((sent["lat_e8"], sent["lon_e8"]),
                             (kept["lat_e8"], kept["lon_e8"]))

    def test_skip_entry_errors_keep_their_paths_and_order(self):
        package = sample_package()
        package["source"]["skipped"][0]["reason"] = "nonsense"
        package["source"]["skipped_total"] = 99999
        with self.assertRaises(stakeout.StakeoutError) as caught:
            run(stakeout.validate_import(package))
        self.assertEqual(caught.exception.path, "source.skipped[0].reason")


class TestControlCharacters(unittest.TestCase):
    """stakeout's local text check agrees with cfg_validation's, always."""

    @staticmethod
    def samples():
        yield from (chr(code) for code in range(0x300))
        yield from ("", " ", "abc", "a b", "a\tb", "a\nb", "a\x7fb", "a\x80b",
                    "\u00a0", "\u2028", "\u200b", "Gr\u00f6\u00dfe \u2713", "\U0001d11e", "\U0001f600 x", "x" * 79 + "\x00",
                    "\x1f" + "x" * 79, "x" * 80, "\x00" * 5, "\u00e4" * 40 + "\x7f")

    def test_agrees_with_the_shared_check(self):
        differing = [text for text in self.samples()
                     if stakeout._controlled(text) is not has_control_characters(text)]
        self.assertEqual(differing, [])

    def test_boundaries(self):
        self.assertTrue(stakeout._controlled("\x1f"))
        self.assertFalse(stakeout._controlled(" "))
        self.assertTrue(stakeout._controlled("\x7f"))
        self.assertFalse(stakeout._controlled("\x80"))
        self.assertFalse(stakeout._controlled("\x9f\u00a0"))
        self.assertFalse(stakeout._controlled(""))

    def test_text_checks_the_stripped_value(self):
        self.assertEqual(stakeout._text("\t name \n", 1, 80, "x"), "name")
        for bad in ("a\x00b", "a\nb", "a\x7fb"):
            with self.assertRaises(stakeout.StakeoutError) as caught:
                stakeout._text(bad, 1, 80, "targets[0].name")
            self.assertEqual(caught.exception.path, "targets[0].name")
        for text, minimum, maximum in (("", 1, 5), ("abcdef", 1, 5), ("   ", 1, 5)):
            with self.assertRaises(stakeout.StakeoutError):
                stakeout._text(text, minimum, maximum, "x")
        self.assertEqual(stakeout._text("\U0001f600\U0001d11e", 2, 2, "x"), "\U0001f600\U0001d11e")


class TestImportDiagnostics(unittest.TestCase):
    """The import's timing_ms on a fake clock with injected operation costs."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="stakeout-meter-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = stakeout.TargetStore(os.path.join(self.tmp, "stakeout"))
        logger = mock.patch.object(stakeout, "log")
        logger.start()
        self.addCleanup(logger.stop)

    def add(self, pkg=None, capacity=None, store=None):
        return run((store or self.store).import_package(
            compact(pkg or sample_package(PID)), lambda pid: FP,
            capacity or (lambda n: True)))

    def costs(self, clock, **costs):
        """Patches the heavy operations to cost the given ms on clock."""
        real_load = stakeout.TargetStore._load
        real_remember = stakeout.TargetStore._remember
        real_loads = stakeout.ujson.loads
        seen = {"dumps": 0}

        def loads(body):
            clock.advance(costs.get("parse", 0))
            return real_loads(body)

        async def listing(store, *args):
            clock.advance(costs.get("listing", 0))
            return await real_load(store, *args)

        def remember(store, *args):
            clock.advance(costs.get("remember", 0))
            return real_remember(store, *args)

        def rename(source, target):
            clock.advance(costs.get("rename", 0))
            return _real_rename(source, target)

        real_dumps = stakeout.ujson.dumps

        def dumps(value):
            text = real_dumps(value)
            cost = len(text) // costs.get("dumps_per", 1 << 30)
            seen["dumps"] = max(seen["dumps"], cost)
            clock.advance(cost)
            return text

        for patch in (mock.patch.object(stakeout.ujson, "loads", loads),
                      mock.patch.object(stakeout.ujson, "dumps", dumps),
                      mock.patch.object(stakeout.TargetStore, "_load", listing),
                      mock.patch.object(stakeout.TargetStore, "_remember", remember),
                      mock.patch.object(stakeout.os, "rename", rename)):
            patch.start()
            self.addCleanup(patch.stop)
        return seen

    def test_longest_block_includes_prefix_work(self):
        clock = FakeClock(self, suspension=40)
        self.costs(clock, parse=450, listing=100, rename=60)
        timing = self.add()["timing_ms"]
        self.assertEqual(timing["longest_block"], 450)
        self.assertEqual(timing["parse"], 450)

    def test_gc_before_the_parse_is_timed_at_a_service_point(self):
        clock = FakeClock(self, suspension=40)
        self.costs(clock, parse=120, listing=100, rename=60)
        events = []
        real_pause = stakeout._pause
        real_loads = stakeout.ujson.loads

        async def pause(meter):
            events.append("pause")
            await real_pause(meter)

        def collect():
            events.append("gc")
            clock.advance(400)

        def loads(body):
            events.append("parse")
            return real_loads(body)

        with mock.patch.object(stakeout, "_pause", pause), \
                mock.patch.object(stakeout.ujson, "loads", loads), \
                mock.patch.object(stakeout.gc, "collect", collect):
            timing = self.add()["timing_ms"]
        # A yield on each side: the collection is a block of its own, apart
        # from the parse, and it is the longest one here.
        self.assertEqual(events[:4], ["pause", "gc", "pause", "parse"])
        self.assertEqual(events.count("gc"), 1)
        self.assertEqual(timing["gc"], 400)
        self.assertEqual(timing["longest_block"], 400)
        self.assertEqual(timing["parse"], 120)

    def test_longest_block_includes_suffix_work(self):
        clock = FakeClock(self, suspension=40)
        self.costs(clock, parse=120, rename=200, remember=150)

        def capacity(size):
            clock.advance(300)          # a statvfs
            return True

        timing = self.add(capacity=capacity)["timing_ms"]
        # rename and the cache update follow the last yield: one stretch.
        self.assertEqual(timing["longest_block"], 350)
        self.assertEqual(timing["max_capacity"], 300)
        self.assertEqual(timing["max_dir"], 200)

    def test_longest_block_includes_capacity_and_ignores_other_tasks(self):
        clock = FakeClock(self, suspension=40)
        self.costs(clock, parse=120, listing=100, rename=60)

        def capacity(size):
            clock.advance(300)
            return True

        meters = []

        async def overview():
            # Another operation busy while the import is suspended.
            meter = stakeout._BlockMeter()
            meters.append(meter)
            for _ in range(500):
                clock.advance(1000)
                await stakeout._pause(meter)

        async def scenario():
            task = asyncio.ensure_future(overview())
            answer = await self.store.import_package(
                compact(sample_package(PID)), lambda pid: FP, capacity)
            await task
            return answer

        timing = run(scenario())["timing_ms"]
        self.assertEqual(timing["longest_block"], 300)
        self.assertEqual(timing["max_capacity"], 300)
        self.assertEqual(timing["max_dir"], 60)  # the rename
        self.assertEqual(meters[0].lap(), 1000)
        self.assertGreaterEqual(timing["max_pause"], 1000)

    def test_file_operations_report_their_slowest_call(self):
        clock = FakeClock(self, suspension=7)
        seen = self.costs(clock, dumps_per=40)
        costs = {"write": 13, "read": 21, "flush": 34, "close": 55}

        class Slow:
            def __init__(self, handle):
                self.handle = handle

            def write(self, data):
                clock.advance(costs["write"])
                return self.handle.write(data)

            def read(self, *args):
                clock.advance(costs["read"])
                return self.handle.read(*args)

            def flush(self):
                clock.advance(costs["flush"])
                return self.handle.flush()

            def close(self):
                clock.advance(costs["close"])
                return self.handle.close()

            def __getattr__(self, name):
                return getattr(self.handle, name)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.close()

        def slow_open(path, mode="r", *args, **kwargs):
            handle = _real_open(path, mode, *args, **kwargs)
            return Slow(handle) if str(path).endswith(".tmp") else handle

        with mock.patch("builtins.open", slow_open):
            timing = self.add(make_package(targets=64))["timing_ms"]
        self.assertGreater(seen["dumps"], 0)
        self.assertEqual(timing["max_serialize"], seen["dumps"])
        for kind, cost in costs.items():
            self.assertEqual(timing["max_" + kind], cost, kind)

    def test_phase_wall_time_is_split_into_work_and_suspension(self):
        clock = FakeClock(self, suspension=7)
        self.costs(clock)
        reads = []
        writes = []

        class Slow:
            def __init__(self, handle):
                self.handle = handle

            def write(self, data):
                clock.advance(2)
                writes.append(len(data))
                return self.handle.write(data)

            def read(self, *args):
                clock.advance(3)
                reads.append(1)
                return self.handle.read(*args)

            def __getattr__(self, name):
                return getattr(self.handle, name)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.handle.close()

        def slow_open(path, mode="r", *args, **kwargs):
            handle = _real_open(path, mode, *args, **kwargs)
            return Slow(handle) if str(path).endswith(".tmp") else handle

        with mock.patch("builtins.open", slow_open):
            answer = self.add(make_package(targets=64))
        timing, size = answer["timing_ms"], answer["bytes"]
        pieces = -(-size // stakeout.WRITE_PIECE)
        for phase in ("validate", "write", "verify"):
            self.assertEqual(timing[phase + "_work"] + timing[phase + "_wait"],
                             timing[phase], phase)
        self.assertEqual(timing["write_work"], 2 * len(writes))
        self.assertEqual(timing["write_wait"], 7 * (size // stakeout.WRITE_PIECE))
        self.assertEqual(timing["verify_work"], 3 * len(reads))
        # The yield after the flush belongs to the verification.
        self.assertEqual(timing["verify_wait"], 7 * (pieces + 1))
        self.assertEqual(timing["validate_work"], 0)
        self.assertGreater(timing["validate_wait"], 0)
        self.assertEqual(timing["validate_wait"] % 7, 0)
        self.assertEqual(timing["max_pause"], 7)
        self.assertEqual(clock.sleeps, timing["pauses"])

    def test_diagnostics_are_constant_size_integers(self):
        timing = self.add(make_package(targets=300))["timing_ms"]
        self.assertEqual(set(timing), set(stakeout.TIMING_KEYS))
        for key, value in timing.items():
            self.assertIs(type(value), int, key)
        self.assertGreater(timing["pauses"], 5)

    def test_a_later_import_does_not_inherit_a_failed_ones_meter(self):
        clock = FakeClock(self, suspension=3)
        baseline = self.add(make_package(targets=64, offset=0))["timing_ms"]
        with mock.patch.object(stakeout.os, "rename", side_effect=OSError(5, "io")):
            with self.assertRaises(stakeout.StakeoutError):
                self.add(make_package(targets=64, offset=7))
        self.assertIs(self.store.busy, False)
        again = self.add(make_package(targets=64, offset=0, ),
                         store=stakeout.TargetStore(os.path.join(self.tmp, "other")))
        self.assertEqual(again["timing_ms"]["pauses"], baseline["pauses"])
        self.assertEqual(again["timing_ms"]["max_pause"], 3)

    def test_cancelled_import_leaves_no_meter_behind(self):
        clock = FakeClock(self, suspension=3)

        async def scenario():
            task = asyncio.ensure_future(self.store.import_package(
                compact(make_package(targets=64)), lambda pid: FP, lambda n: True))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertIs(self.store.busy, False)
            return await self.store.import_package(
                compact(make_package(targets=64, offset=9)), lambda pid: FP,
                lambda n: True)

        timing = run(scenario())["timing_ms"]
        clean = self.add(make_package(targets=64, offset=9),
                         store=stakeout.TargetStore(os.path.join(self.tmp, "clean")))
        self.assertEqual(timing["pauses"], clean["timing_ms"]["pauses"])
        self.assertEqual(timing["longest_block"], 0)

    def test_failure_after_the_commit_still_closes_the_meter(self):
        clock = FakeClock(self, suspension=3)
        with mock.patch.object(stakeout.TargetStore, "_remember", side_effect=MemoryError):
            with self.assertRaises(stakeout.StakeoutError) as caught:
                self.add(make_package(targets=64))
        self.assertEqual(caught.exception.code, "import_committed_response_failed")
        self.assertIs(self.store.busy, False)
        timing = self.add(make_package(targets=64, offset=5))["timing_ms"]
        self.assertEqual(timing["max_pause"], 3)
        self.assertEqual(timing["longest_block"], 0)


# --- Set file layout 2: writer and trailer reader -------------------------

UINT32 = 4294967295
TRAILER_HEAD = b',\n"summary": '


async def _no_pause(meter):
    """A service pause that does not sleep (the size tests write many files)."""
    meter.resume(meter.suspend())


def device_dumps(value):
    """ujson.dumps as on the device: raw UTF-8, not \\u escapes."""
    return json.dumps(value, ensure_ascii=False)


def maximal_summary(text_char="\U0001d11e"):
    """The largest summary C1 allows: every text and integer at its maximum."""
    body = stakeout.MAX_SET_FILE_BYTES - stakeout.TRAILER_BYTES
    return {"layout": 2, "set_id": "so-ffffffff", "project_id": "P" * 64,
            "project_created_at": UINT32, "imported_at": UINT32,
            "source": {"filename": text_char * stakeout.MAX_FILENAME,
                       "bytes": stakeout.MAX_SOURCE_BYTES,
                       "features": stakeout.MAX_FEATURES,
                       "skipped_total": stakeout.MAX_FEATURES},
            "parts": 500, "targets": 500, "coords": stakeout.MAX_COORDS_PER_SET,
            "body_bytes": body, "body_sha256": "f" * 64, "targets_offset": body - 1}


def maximal_text_package(char, props, value_length):
    """As many targets as fit 131072 bytes, each with a maximal name and
    props maximal keys with values of value_length, all in char, plus a
    maximal filename and skip list: the text that expands most when stored."""
    def build(count):
        targets = []
        for i in range(count):
            keys = [("%d" % j) + char * (stakeout.MAX_PROP_KEY - 1) for j in range(props)]
            targets.append({"part_index": 0, "coord_index": i,
                            "name": char * stakeout.MAX_NAME, "lat_e8": -9000000000, "lon_e8": -18000000000,
                            "alt_import": -20000000,
                            "props": {key: char * value_length for key in keys}})
        skipped = [{"feature_index": 4999, "type": char * stakeout.MAX_TYPE,
                    "reason": "unsupported_geometry"}] * stakeout.MAX_SKIPPED
        return {"schema": 1, "project_id": "P" * 64,
                "source": {"filename": char * stakeout.MAX_FILENAME, "bytes": 4194304,
                           "features": 5000, "skipped": skipped, "skipped_total": 5000},
                "parts": [{"kind": "multipoint", "name": char * stakeout.MAX_NAME,
                           "feature_index": 4999, "geometry_index": 4999,
                           "coords_total": 1000000, "coords_thinned": False,
                           "coords_e8": []}],
                "targets": targets}

    def size(pkg):
        # The browser sends raw UTF-8 (JSON.stringify).
        return len(json.dumps(pkg, separators=(",", ":"), ensure_ascii=False).encode())

    count = 1
    while count < 500 and size(build(count + 1)) <= stakeout.MAX_IMPORT_BYTES:
        count += 1
    return build(count)


class _SetFileCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="stakeout-setfile-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        logger = mock.patch.object(stakeout, "log")
        self.log = logger.start()
        self.addCleanup(logger.stop)

    def doc(self, pkg=None, created=FP, imported=845000000):
        plan = run(stakeout.validate_import(pkg or sample_package(PID)))
        return set_doc(plan, project_created_at=created, imported_at=imported)

    def path(self, doc):
        return os.path.join(self.tmp, doc["set_id"] + ".json")

    def store(self, doc, path=None, **kwargs):
        """Write doc with the layout-2 writer; returns (summary, file bytes)."""
        path = path or self.path(doc)
        summary = run(stakeout._store_set_file(path, doc, **kwargs))
        with _real_open(path, "rb") as fh:
            return summary, fh.read()

    def files(self):
        return sorted(os.listdir(self.tmp))


class TestSetFileWriter(_SetFileCase):
    def unicode_package(self):
        pkg = sample_package("project-unicode")
        pkg["source"]["filename"] = "Gr\u00f6sse \U0001d11e.geojson"
        for target in pkg["targets"]:
            target["name"] = "Gr\u00f6\u00dfe \u2713 \U0001d11e"
            target["props"] = {"note": "Gr\u00f6\u00dfe \u2713 \U0001d11e", "quote": 'a "b" \\ c'}
        return pkg

    def assert_layout(self, doc, summary, data):
        """The file is doc plus summary in layout 2, recomputed from its bytes."""
        trailer_bytes = stakeout.TRAILER_BYTES
        self.assertEqual(json.loads(data), dict(doc, summary=summary))
        body, trailer = data[:-trailer_bytes], data[-trailer_bytes:]
        self.assertEqual(len(trailer), 4096)
        self.assertTrue(trailer.startswith(TRAILER_HEAD))
        self.assertTrue(trailer.endswith(b"}\n"))
        self.assertEqual(json.loads(trailer[len(TRAILER_HEAD):-2]), summary)
        self.assertTrue(body.endswith(b"\n]"))
        # Body: the head without its closing brace, one line per part and
        # target, ",\n" between the items.
        head = {key: value for key, value in doc.items() if key not in ("parts", "targets")}
        lines = body.split(b"\n")
        parts, targets = len(doc["parts"]), len(doc["targets"])
        self.assertEqual(lines[0], stakeout.ujson.dumps(head)[:-1].encode() + b",")
        self.assertEqual(lines[1], b'"parts": [')
        for index, part in enumerate(doc["parts"]):
            ending = b"," if index < parts - 1 else b""
            self.assertEqual(lines[2 + index], stakeout.ujson.dumps(part).encode() + ending)
        self.assertEqual(lines[2 + parts:4 + parts], [b"],", b'"targets": ['])
        for index, target in enumerate(doc["targets"]):
            ending = b"," if index < targets - 1 else b""
            self.assertEqual(lines[4 + parts + index],
                             stakeout.ujson.dumps(target).encode() + ending)
        self.assertEqual(lines[4 + parts + targets:], [b"]"])
        # The summary, its body fields recomputed independently.
        coords = sum(len(part["coords_e8"]) for part in doc["parts"])
        source = doc["source"]
        marker = b'\n"targets": [\n'
        self.assertEqual(body.count(marker), 1)
        self.assertEqual(summary, {
            "layout": 2, "set_id": doc["set_id"], "project_id": doc["project_id"],
            "project_created_at": doc["project_created_at"],
            "imported_at": doc["imported_at"],
            "source": {key: source[key]
                       for key in ("filename", "bytes", "features", "skipped_total")},
            "parts": parts, "targets": targets, "coords": coords,
            "body_bytes": len(body),
            "body_sha256": hashlib.sha256(body).hexdigest(),
            "targets_offset": body.index(marker) + len(marker)})
        # WP2 finds every target by scanning lines from targets_offset.
        scanned = body[summary["targets_offset"]:].split(b"\n")[:targets]
        self.assertEqual([json.loads(line.rstrip(b",")) for line in scanned], doc["targets"])

    def test_round_trip_of_head_parts_targets_and_trailer(self):
        p128 = copy.deepcopy(P128)
        p128["project_id"] = "project-p128"
        points = make_package(targets=3)
        for label, pkg, created in (("sample", sample_package(PID), FP),
                                    ("p128", p128, None),
                                    ("unicode", self.unicode_package(), 0),
                                    ("points only", points, UINT32)):
            for encoding, dumps in (("host", json.dumps), ("device", device_dumps)):
                with self.subTest(label, encoding=encoding), \
                        mock.patch.object(stakeout.ujson, "dumps", dumps):
                    doc = self.doc(pkg, created=created, imported=UINT32)
                    summary, data = self.store(doc)
                    self.assert_layout(doc, summary, data)
                    self.assertEqual(self.files(), [doc["set_id"] + ".json"])
                    self.assertEqual(stakeout._read_trailer(self.path(doc), doc["project_id"],
                                                            doc["set_id"]), summary)
                    os.remove(self.path(doc))
        # A set of points and multipoints stores no vertices.
        self.assertEqual(self.doc(points)["parts"][0]["coords_e8"], [])

    def test_the_file_is_serialized_once(self):
        doc = self.doc(P128)
        with mock.patch.object(stakeout.ujson, "dumps", wraps=json.dumps) as dumps:
            summary, data = self.store(doc)
        # The head, every part, every target and the summary, once each.
        self.assertEqual(dumps.call_count, 1 + len(doc["parts"]) + len(doc["targets"]) + 1)
        self.assertEqual(len(data), summary["body_bytes"] + stakeout.TRAILER_BYTES)

    def test_writes_and_reads_back_in_pieces_with_a_recheck_after_every_yield(self):
        doc = self.doc(P128)
        writes, reads = [], []

        class Recorded:
            def __init__(self, handle):
                self.handle = handle

            def write(self, data):
                writes.append(len(data))
                return self.handle.write(data)

            def read(self, *args):
                data = self.handle.read(*args)
                reads.append(len(data))
                return data

            def __getattr__(self, name):
                return getattr(self.handle, name)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.handle.close()

        def recorded_open(path, mode="r", *args, **kwargs):
            handle = _real_open(path, mode, *args, **kwargs)
            return Recorded(handle) if str(path).endswith(".tmp") else handle

        recheck = mock.Mock()
        meter = stakeout._BlockMeter()
        timing = {}
        with mock.patch("builtins.open", recorded_open):
            summary, data = self.store(doc, timing=timing, recheck=recheck, meter=meter)
        self.assertEqual(sum(writes), len(data))
        self.assertEqual(sum(reads), len(data))
        self.assertEqual(reads[-1], 0)  # the end of the file
        blocks = reads[:-1]
        self.assertTrue(all(0 < size <= stakeout.WRITE_PIECE for size in writes + blocks))
        self.assertGreater(len(blocks), 16)
        # A yield after every full piece of the body, one before the trailer,
        # one after the write phase, one after every block read back and one
        # before the rename; recheck after each.
        full = summary["body_bytes"] // stakeout.WRITE_PIECE
        self.assertEqual(meter.pauses, full + 1 + 1 + len(blocks) + 1)
        self.assertEqual(recheck.call_count, meter.pauses)
        for key in ("write", "verify", "rename", "write_work", "write_wait",
                    "verify_work", "verify_wait"):
            self.assertIn(key, timing)

    def test_worst_case_summary_fits_the_trailer_in_both_encodings(self):
        for encoding, dumps in (("host", json.dumps), ("device", device_dumps)):
            for char in ("\U0001d11e", "\u00e9", "\x7f"):
                with self.subTest(encoding, char=char), \
                        mock.patch.object(stakeout.ujson, "dumps", dumps):
                    trailer = stakeout._trailer(maximal_summary(char))
                    self.assertEqual(len(trailer), stakeout.TRAILER_BYTES)
                    spare = len(trailer) - 2 - len(trailer[:-2].rstrip(b" "))
                    self.assertGreaterEqual(spare, 64)
        # Under the host shim the worst summary is far the larger one.
        self.assertGreater(len(json.dumps(maximal_summary())), 1500)

    def test_worst_case_target_line_stays_under_the_line_limit(self):
        def worst_doc():
            props = {chr(0x1F600 + j) * stakeout.MAX_PROP_KEY:
                     chr(0x1F680 + j) * stakeout.MAX_PROP_VALUE
                     for j in range(stakeout.MAX_PROPS)}
            part = {"kind": "multipoint", "name": "\U0001d11e" * stakeout.MAX_NAME,
                    "feature_index": 4999, "geometry_index": 4999,
                    "coords_total": 1000000, "coords_e8": [], "coords_thinned": False,
                    "part_id": "so-ffffffff/500"}
            target = {"name": "\U0001d11e" * stakeout.MAX_NAME, "coord_index": 999999,
                      "lat_e8": -9000000000, "lon_e8": -18000000000,
                      "alt_import": -20000000, "props": props,
                      "tid": "so-ffffffff/500/1000000", "part_id": "so-ffffffff/500",
                      "seq": 1000000}
            return {"kind": "stakeout_set", "schema": 1, "project_id": "P" * 64,
                    "project_created_at": UINT32, "set_id": "so-ffffffff",
                    "imported_at": UINT32,
                    "source": {"filename": "x", "bytes": 1, "features": 1, "skipped": [],
                               "skipped_total": 0},
                    "parts": [part], "targets": [target, dict(target, seq=999999)]}

        lengths = {}
        for encoding, dumps in (("host", json.dumps), ("device", device_dumps)):
            with self.subTest(encoding), mock.patch.object(stakeout.ujson, "dumps", dumps):
                doc = worst_doc()
                summary, data = self.store(doc)
                body = data[:summary["body_bytes"]]
                lines = body[summary["targets_offset"]:].split(b"\n")[:2]
                lengths[encoding] = max(len(line) + 1 for line in lines)  # with "\n"
                self.assertLess(lengths[encoding], stakeout.TARGET_LINE_MAX)
                os.remove(self.path(doc))
        self.assertEqual(stakeout.TARGET_LINE_MAX, 16384)
        # The host shim escapes each character to 12 bytes: about 12.7 KB.
        self.assertGreater(lengths["host"], 12000)
        self.assertGreater(lengths["host"], lengths["device"])

    def test_worst_131072_byte_packages_stay_under_the_file_limit(self):
        shapes = [("\U0001d11e", 8, 80), ("\U0001d11e", 8, 40), ("\U0001d11e", 4, 80),
                  ("\u00e9", 8, 80), ("\u00e9", 4, 80), ("\u20ac", 8, 80)]
        largest = 0
        with mock.patch.object(stakeout, "_pause", _no_pause):
            for char, props, length in shapes:
                pkg = maximal_text_package(char, props, length)
                body = json.dumps(pkg, separators=(",", ":"), ensure_ascii=False).encode()
                self.assertLessEqual(len(body), stakeout.MAX_IMPORT_BYTES)
                self.assertGreater(len(body), stakeout.MAX_IMPORT_BYTES - 4096)
                doc = self.doc(pkg, created=UINT32, imported=UINT32)
                for encoding, dumps in (("host", json.dumps), ("device", device_dumps)):
                    with self.subTest(char=char, props=props, length=length,
                                      encoding=encoding), \
                            mock.patch.object(stakeout.ujson, "dumps", dumps):
                        summary, data = self.store(doc)
                        self.assertLessEqual(len(data), stakeout.MAX_SET_FILE_BYTES)
                        largest = max(largest, len(data))
                        os.remove(self.path(doc))
        self.assertEqual(stakeout.MAX_SET_FILE_BYTES, 393216)
        # The worst shape comes close: the limit is not loose by accident.
        self.assertGreater(largest, 360000)

    def test_overflow_while_writing_is_too_many_targets_without_a_tmp(self):
        doc = self.doc(make_package(targets=64))
        summary, data = self.store(doc)
        os.remove(self.path(doc))
        size = len(data)
        self.assertGreater(summary["body_bytes"], 2 * stakeout.WRITE_PIECE)
        for limit in (size - 1, stakeout.TRAILER_BYTES + stakeout.WRITE_PIECE + 1, 0):
            with self.subTest(limit=limit), \
                    mock.patch.object(stakeout, "MAX_SET_FILE_BYTES", limit):
                with self.assertRaises(stakeout.StakeoutError) as caught:
                    self.store(doc)
                error = caught.exception
                self.assertEqual((error.code, error.status, error.path),
                                 ("too_many_targets", "400 Bad Request", ""))
                self.assertEqual(self.files(), [])
        with mock.patch.object(stakeout, "MAX_SET_FILE_BYTES", size):
            self.assertEqual(self.store(doc), (summary, data))

    def test_the_tmp_never_grows_past_the_limit(self):
        doc = self.doc(make_package(targets=64))
        limit = stakeout.TRAILER_BYTES + 2 * stakeout.WRITE_PIECE + 100
        written = []

        class Counting(_MemoryLowFile):
            def write(self, data):
                written.append(len(data))
                return self.handle.write(data)

        def counting_open(path, mode="r", *args, **kwargs):
            handle = _real_open(path, mode, *args, **kwargs)
            return Counting(handle) if "w" in mode else handle

        with mock.patch.object(stakeout, "MAX_SET_FILE_BYTES", limit), \
                mock.patch("builtins.open", counting_open):
            with self.assertRaises(stakeout.StakeoutError):
                self.store(doc)
        self.assertLessEqual(sum(written), limit - stakeout.TRAILER_BYTES)
        self.assertEqual(self.files(), [])

    def test_failures_before_the_commit_leave_no_file(self):
        doc = self.doc(make_package(targets=64))

        def tmp(mode):
            return lambda path, used: path.endswith(".tmp") and mode in used

        def wrapped_open(wrapper):
            def fake_open(path, mode="r", *args, **kwargs):
                handle = _real_open(path, mode, *args, **kwargs)
                return wrapper(handle) if tmp("w")(str(path), mode) else handle
            return fake_open

        def changing(number, change):
            """Changes write number (or the trailer's) on its way to the flash."""
            class Changing(_MemoryLowFile):
                def write(self, data):
                    self.writes += 1
                    trailer = bytes(data).startswith(TRAILER_HEAD)
                    if self.writes == number or (number == "trailer" and trailer):
                        data = change(bytes(data))
                    return self.handle.write(data)
            return Changing

        class FailingFlush(_MemoryLowFile):
            def write(self, data):
                return self.handle.write(data)

            def flush(self):
                raise OSError(5, "io")

        flip = lambda data: bytes([data[0] ^ 1]) + data[1:]  # noqa: E731
        drop = lambda data: b""  # noqa: E731
        extra = lambda data: data + b" "  # noqa: E731
        cases = [
            ("open fails", "stakeout_write_failed",
             mock.patch("builtins.open", _failing_open(tmp("w"), OSError(28, "no space")))),
            ("write runs out of memory", "stakeout_write_failed",
             mock.patch("builtins.open", wrapped_open(_MemoryLowFile))),
            ("flush fails", "stakeout_write_failed",
             mock.patch("builtins.open", wrapped_open(FailingFlush))),
            ("verification unreadable", "stakeout_write_failed",
             mock.patch("builtins.open", _failing_open(tmp("r"), OSError(5, "io")))),
            ("body digest differs", "stakeout_write_failed",
             mock.patch("builtins.open", wrapped_open(changing(2, flip)))),
            ("body shorter", "stakeout_write_failed",
             mock.patch("builtins.open", wrapped_open(changing(2, drop)))),
            ("trailer differs", "stakeout_write_failed",
             mock.patch("builtins.open", wrapped_open(changing("trailer", flip)))),
            ("trailer longer", "stakeout_write_failed",
             mock.patch("builtins.open", wrapped_open(changing("trailer", extra)))),
            ("trailer missing", "stakeout_write_failed",
             mock.patch("builtins.open", wrapped_open(changing("trailer", drop)))),
            ("rename fails", "stakeout_write_failed",
             mock.patch.object(stakeout.os, "rename", side_effect=OSError(5, "io"))),
            ("memory runs out while verifying", "stakeout_busy",
             mock.patch.object(stakeout, "_read_back", side_effect=MemoryError)),
        ]
        statuses = {"stakeout_write_failed": "507 Insufficient Storage",
                    "stakeout_busy": "503 Service Unavailable"}
        for label, code, patch in cases:
            with self.subTest(label):
                with patch, mock.patch.object(stakeout.gc, "collect") as collect:
                    with self.assertRaises(stakeout.StakeoutError) as caught:
                        self.store(doc)
                self.assertEqual((caught.exception.code, caught.exception.status),
                                 (code, statuses[code]))
                if "memory" in label:
                    self.assertTrue(collect.called)
                self.assertEqual(self.files(), [])
        # A summary the reader would refuse is never committed: one that does
        # not fit its trailer, one outside the ranges, one of another length.
        writes = []

        def counting_open(path, mode="r", *args, **kwargs):
            handle = _real_open(path, mode, *args, **kwargs)

            class Counting(_MemoryLowFile):
                def write(self, data):
                    writes.append(bytes(data))
                    return self.handle.write(data)
            return Counting(handle) if "w" in mode else handle

        too_long = copy.deepcopy(doc)
        too_long["source"]["filename"] = "x" * stakeout.TRAILER_BYTES
        out_of_range = dict(copy.deepcopy(doc), imported_at=UINT32 + 1)
        short = mock.patch.object(stakeout, "_trailer", lambda summary: b"}\n")
        for label, changed, patch in (("too long", too_long, None),
                                      ("out of range", out_of_range, None),
                                      ("other length", doc, short)):
            with self.subTest(label), patch or contextlib.nullcontext(), \
                    mock.patch("builtins.open", counting_open):
                del writes[:]
                with self.assertRaises(stakeout.StakeoutError) as caught:
                    self.store(changed)
                self.assertEqual((caught.exception.code, caught.exception.status),
                                 ("stakeout_write_failed", "507 Insufficient Storage"))
                self.assertFalse(any(data.startswith(TRAILER_HEAD) for data in writes))
                self.assertEqual(self.files(), [])

    def test_an_existing_set_file_is_never_replaced(self):
        doc = self.doc()
        with _real_open(self.path(doc), "wb") as fh:
            fh.write(b"kept")
        with self.assertRaises(stakeout.StakeoutError) as caught:
            self.store(doc)
        self.assertEqual((caught.exception.code, caught.exception.set_id),
                         ("already_imported", doc["set_id"]))
        self.assertEqual(self.files(), [doc["set_id"] + ".json"])
        with _real_open(self.path(doc), "rb") as fh:
            self.assertEqual(fh.read(), b"kept")

    def test_recheck_and_cancellation_stop_the_writer_without_a_file(self):
        doc = self.doc(make_package(targets=64))
        for stop in (stakeout.StakeoutError("project_not_found", "gone"),
                     asyncio.CancelledError()):
            for after in (1, 3, 6, 8):
                calls = []

                def recheck():
                    calls.append(1)
                    if len(calls) == after:
                        raise stop

                with self.subTest(stop=type(stop).__name__, after=after):
                    with self.assertRaises(type(stop)):
                        self.store(doc, recheck=recheck)
                    self.assertEqual(self.files(), [])

    def test_rename_that_raises_after_moving_counts_as_committed(self):
        doc = self.doc()

        def rename_then_fail(source, target):
            _real_rename(source, target)
            raise OSError(5, "io")

        def copy_then_fail(source, target):
            with _real_open(source, "rb") as fh, _real_open(target, "wb") as out:
                out.write(fh.read())
            raise OSError(5, "io")

        with mock.patch.object(stakeout.os, "rename", rename_then_fail):
            summary, data = self.store(doc)
        self.assertEqual(self.files(), [doc["set_id"] + ".json"])
        self.assertEqual(summary["body_bytes"] + stakeout.TRAILER_BYTES, len(data))
        os.remove(self.path(doc))
        with mock.patch.object(stakeout.os, "rename", copy_then_fail):
            with self.assertRaises(stakeout.StakeoutError) as caught:
                self.store(doc)
        self.assertEqual(caught.exception.code, "stakeout_write_failed")
        self.assertNotIn(doc["set_id"] + ".json.tmp", self.files())


class TestTrailerReader(_SetFileCase):
    def setUp(self):
        super().setUp()
        self.set_doc = self.doc()
        self.summary, self.data = self.store(self.set_doc)
        self.file = self.path(self.set_doc)

    def read(self, path=None, project_id=PID, set_id=SAMPLE_SET_ID):
        return stakeout._read_trailer(path or self.file, project_id, set_id)

    def rewrite(self, data, path=None):
        with _real_open(path or self.file, "wb") as fh:
            fh.write(data)

    def body(self):
        return self.data[:-stakeout.TRAILER_BYTES]

    def with_summary(self, summary, body=None):
        """The file with summary in a writer-made trailer (body kept unless given)."""
        self.rewrite((self.body() if body is None else body) + stakeout._trailer(summary))

    def assert_damaged(self, label, **read):
        with self.subTest(label):
            self.assertIsNone(self.read(**read))

    def test_intact_file_gives_the_writers_summary(self):
        self.assertEqual(self.read(), self.summary)
        self.assertEqual(self.summary["set_id"], SAMPLE_SET_ID)

    def test_wrong_ids_are_damaged(self):
        self.assert_damaged("other set", set_id="so-00000000")
        self.assert_damaged("other project", project_id="project-other")
        self.assert_damaged("invalid set", set_id="so-3A4739B9")
        self.assert_damaged("invalid project", project_id="project.x")

    def test_frame_damage(self):
        trailer = self.data[-stakeout.TRAILER_BYTES:]
        text = trailer[len(TRAILER_HEAD):-2].rstrip(b" ")
        pad = lambda content: content + b" " * (  # noqa: E731
            stakeout.TRAILER_BYTES - len(TRAILER_HEAD) - 2 - len(content))
        cases = {
            "prefix": b";" + trailer[1:],
            "prefix newline": trailer.replace(b",\n", b", ", 1),
            "suffix brace": trailer[:-2] + b" \n",
            "suffix newline": trailer[:-1] + b" ",
            "malformed": TRAILER_HEAD + pad(text[:-1]) + b"}\n",
            "two objects": TRAILER_HEAD + pad(text + b" {}") + b"}\n",
            "not an object": TRAILER_HEAD + pad(b"[" + text + b"]") + b"}\n",
            "invalid UTF-8": TRAILER_HEAD + pad(text.replace(b'"layout"', b'"\xff\xfeyout"'))
            + b"}\n",
        }
        for label, damaged in cases.items():
            self.assertEqual(len(damaged), stakeout.TRAILER_BYTES, label)
            self.rewrite(self.body() + damaged)
            self.assert_damaged(label)
        # A summary too large for the trailer moves the frame out of place.
        large = dict(self.summary, source=dict(self.summary["source"],
                                               filename="\U0001d11e" * 400))
        self.with_summary(large)
        self.assertGreater(os.path.getsize(self.file),
                           self.summary["body_bytes"] + stakeout.TRAILER_BYTES)
        self.assert_damaged("oversized summary")

    def test_length_must_agree_with_stat(self):
        body = self.body()
        for label, data in (("byte added to the body", body + b" " + self.data[len(body):]),
                            ("byte missing from the body", body[1:] + self.data[len(body):]),
                            ("trailer only", self.data[len(body):]),
                            ("shorter than a trailer", self.data[-100:]),
                            ("empty", b""),
                            ("byte after the trailer", self.data + b" ")):
            self.rewrite(data)
            self.assert_damaged(label)
        # A missing file is no damage: the caller decides (a change meanwhile).
        os.remove(self.file)
        with self.assertRaises(OSError) as caught:
            self.read()
        self.assertEqual(caught.exception.args[0], 2)

    def test_every_field_outside_its_range_is_damaged(self):
        body_max = stakeout.MAX_SET_FILE_BYTES - stakeout.TRAILER_BYTES
        good = self.summary
        bad_values = {
            "layout": [1, 3, True, 2.0, "2", None],
            "set_id": ["so-00000000", "so-3A4739B9", None, 1],
            "project_id": ["project-other", "", None, ["x"]],
            "project_created_at": [-1, UINT32 + 1, 1.5, "1", True],
            "imported_at": [-1, UINT32 + 1, None, 1.5, True],
            "parts": [0, 501, None, "1", True],
            "targets": [0, 501, None, 1.0, True],
            "coords": [-1, stakeout.MAX_COORDS_PER_SET + 1, None, False],
            "body_sha256": ["0" * 63, "0" * 65, "A" * 64, "g" * 64, None, 0],
            "targets_offset": [0, -1, good["body_bytes"], good["body_bytes"] + 1, None,
                               1.0, True],
            "source": [None, [], "x", {}],
        }
        for key, values in bad_values.items():
            for value in values:
                self.with_summary(dict(good, **{key: value}))
                self.assert_damaged("%s=%r" % (key, value))
        source_values = {
            "filename": ["x" * (stakeout.MAX_FILENAME + 1), None, 5, "a\x01b"],
            "bytes": [-1, stakeout.MAX_SOURCE_BYTES + 1, None, 1.0, True],
            "features": [-1, stakeout.MAX_FEATURES + 1, None, True],
            "skipped_total": [-1, stakeout.MAX_FEATURES + 1, None, True],
        }
        for key, values in source_values.items():
            for value in values:
                source = dict(good["source"], **{key: value})
                self.with_summary(dict(good, source=source))
                self.assert_damaged("source.%s=%r" % (key, value))
        for key in list(good) + ["source." + key for key in good["source"]]:
            summary = copy.deepcopy(good)
            if key.startswith("source."):
                del summary["source"][key[7:]]
            else:
                del summary[key]
            self.with_summary(summary)
            self.assert_damaged("missing " + key)
        self.with_summary(dict(good, extra=1))
        self.assert_damaged("extra member")
        self.with_summary(dict(good, source=dict(good["source"], skipped=[])))
        self.assert_damaged("extra source member")
        # body_bytes out of range with a file length that agrees with it.
        for body_bytes in (0, body_max + 1):
            self.with_summary(dict(good, body_bytes=body_bytes, targets_offset=1),
                              body=b" " * body_bytes)
            self.assert_damaged("body_bytes=%d" % body_bytes)

    def test_extremes_inside_the_ranges_are_accepted(self):
        body_max = stakeout.MAX_SET_FILE_BYTES - stakeout.TRAILER_BYTES
        for label, summary in (("maximal", maximal_summary()),
                               ("minimal", dict(maximal_summary(), project_created_at=None,
                                                imported_at=0, parts=1, targets=1,
                                                coords=0, body_bytes=2, targets_offset=1,
                                                source={"filename": "", "bytes": 0,
                                                        "features": 0,
                                                        "skipped_total": 0}))):
            body = b" " * summary["body_bytes"]
            self.with_summary(summary, body=body)
            with self.subTest(label):
                self.assertEqual(self.read(project_id="P" * 64, set_id="so-ffffffff"),
                                 summary)
        self.assertEqual(maximal_summary()["body_bytes"], body_max)

    def test_reads_only_the_trailer(self):
        size = len(self.data)
        events = []

        class Watched:
            def __init__(self, handle):
                self.handle = handle

            def seek(self, *args):
                events.append(("seek",) + args)
                return self.handle.seek(*args)

            def read(self, *args):
                at = self.handle.tell()
                data = self.handle.read(*args)
                events.append(("read", at, len(data)))
                return data

            def readinto(self, *args):
                raise AssertionError("readinto")

            def readline(self, *args):
                raise AssertionError("readline")

            def __iter__(self):
                raise AssertionError("iteration")

            def __getattr__(self, name):
                return getattr(self.handle, name)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.handle.close()

        def watched_open(path, mode="r", *args, **kwargs):
            return Watched(_real_open(path, mode, *args, **kwargs))

        with mock.patch("builtins.open", watched_open), \
                mock.patch.object(stakeout.hashlib, "sha256", side_effect=AssertionError):
            self.assertEqual(self.read(), self.summary)
        self.assertEqual(events, [("seek", size - stakeout.TRAILER_BYTES),
                                  ("read", size - stakeout.TRAILER_BYTES,
                                   stakeout.TRAILER_BYTES)])

    def test_memory_and_io_errors_propagate(self):
        with mock.patch.object(stakeout.ujson, "loads", side_effect=MemoryError):
            with self.assertRaises(MemoryError):
                self.read()
        with mock.patch("builtins.open", _failing_open(lambda path, mode: True,
                                                       MemoryError())):
            with self.assertRaises(MemoryError):
                self.read()
        with mock.patch("builtins.open", _failing_open(lambda path, mode: True,
                                                       OSError(5, "io"))):
            with self.assertRaises(OSError):
                self.read()
        with mock.patch.object(stakeout.os, "stat", side_effect=OSError(5, "io")):
            with self.assertRaises(OSError):
                self.read()
        with mock.patch("builtins.open", _failing_open(lambda path, mode: True,
                                                       OSError(2, "gone"))):
            with self.assertRaises(OSError):
                self.read()



# --- Target lookup (stakeout WP2) ------------------------------------------

class _ReadSpy:
    """A file that records (position, requested, returned) of every read;
    fail["on"] makes the next reads raise OSError(5)."""

    def __init__(self, handle, reads, fail=None):
        self.handle = handle
        self.reads = reads
        self.fail = fail

    def read(self, size=-1):
        if self.fail is not None and self.fail["on"]:
            raise OSError(5, "io")
        at = self.handle.tell()
        data = self.handle.read(size)
        self.reads.append((at, size, len(data)))
        return data

    def __getattr__(self, name):
        return getattr(self.handle, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.handle.close()


class TestFindTarget(_StoreCase):
    """find_target: one target by a line scan from targets_offset, the body
    checked once per set and boot, counted as a reader."""

    OUTPUT_KEYS = ("tid", "part_id", "seq", "name", "lat_e8", "lon_e8", "alt_import")

    def setUp(self):
        super().setUp()
        clock = mock.patch.object(stakeout, "now_seconds", return_value=845000000)
        clock.start()
        self.addCleanup(clock.stop)

    def find(self, tid, store=None, fingerprint=FP, project_id=PID, check=None):
        return run((store or self.store).find_target(project_id, fingerprint, tid, check))

    def stored(self, set_id=SAMPLE_SET_ID):
        """The stored set document, its trailer summary as "summary"."""
        return json.loads(self.read(self.path(set_id)))

    def expected(self, target, set_id=SAMPLE_SET_ID):
        result = {key: target[key] for key in self.OUTPUT_KEYS}
        result["set_id"] = set_id
        return result

    def spied(self, reads, fail=None):
        """builtins.open whose files record their reads (_ReadSpy)."""
        def spy_open(path, mode="r", *args, **kwargs):
            return _ReadSpy(_real_open(path, mode, *args, **kwargs), reads, fail)
        return mock.patch("builtins.open", spy_open)

    @contextlib.contextmanager
    def no_file_access(self):
        with mock.patch("builtins.open", side_effect=AssertionError("open")), \
                mock.patch.object(stakeout.os, "stat", side_effect=AssertionError("stat")), \
                mock.patch.object(stakeout.os, "ilistdir",
                                  side_effect=AssertionError("ilistdir")):
            yield

    def test_parse_tid(self):
        self.assertEqual(stakeout.parse_tid("so-3a4739b9/1/1"), ("so-3a4739b9", 1, 1))
        self.assertEqual(stakeout.parse_tid("so-3a4739b9/500/2000"),
                         ("so-3a4739b9", 500, 2000))
        for tid in ("so-3a4739b9/0/1", "so-3a4739b9/501/1", "so-3a4739b9/1/0",
                    "so-3a4739b9/1/1000001", "so-3A4739b9/1/1", "so-3a4739b9/01/1",
                    "so-3a4739b9/1/01", "so-3a4739b9/+1/1", "so-3a4739b9/1/ 1",
                    "so-3a4739b9/1", "so-3a4739b9/1/1/1", "so-3a4739b9//1", "x", "",
                    5, None, b"so-3a4739b9/1/1", ["so-3a4739b9/1/1"]):
            with self.subTest(tid=tid):
                self.assertIsNone(stakeout.parse_tid(tid))
        self.assertEqual(stakeout.SCAN_PIECE, 2048)

    def test_finds_first_middle_and_last_target(self):
        self.add()
        targets = self.stored()["targets"]
        tids = ("so-3a4739b9/1/1", "so-3a4739b9/2/1", targets[-1]["tid"])
        by_tid = {target["tid"]: target for target in targets}
        self.assertEqual(self.find("so-3a4739b9/2/1", store=self.fresh()), {
            "tid": "so-3a4739b9/2/1", "set_id": SAMPLE_SET_ID,
            "part_id": "so-3a4739b9/2", "seq": 1, "name": "Point 2",
            "lat_e8": 5100000027, "lon_e8": 999999943, "alt_import": 412300})
        # Each one first on a fresh store (the checking pass), then on one
        # store again and again (the scan from targets_offset).
        for tid in tids:
            with self.subTest(tid=tid, first=True):
                store = self.fresh()
                self.assertEqual(self.find(tid, store=store), self.expected(by_tid[tid]))
                self.assertEqual(store.readers(PID), 0)
        for _ in range(2):
            for tid in tids:
                with self.subTest(tid=tid, first=False):
                    self.assertEqual(self.find(tid), self.expected(by_tid[tid]))
        self.assertEqual(self.store.readers(PID), 0)

    def test_unknown_target_part_or_set(self):
        self.add()
        error = self.refused("target_not_found", self.find, "so-3a4739b9/1/9")
        self.assertEqual(error.status, "404 Not Found")
        self.assertEqual(self.store.readers(PID), 0)
        # A part beyond the summary's count is refused before any file is opened.
        self.sets()
        opened = []

        def counting_open(path, mode="r", *args, **kwargs):
            opened.append(path)
            return _real_open(path, mode, *args, **kwargs)

        with mock.patch("builtins.open", counting_open):
            self.refused("target_not_found", self.find, "so-3a4739b9/9/1")
            self.refused("target_not_found", self.find, "so-3a4739b9/6/1")
        self.assertEqual(opened, [])
        error = self.refused("set_not_found", self.find, "so-00000000/1/1")
        self.assertEqual(error.status, "404 Not Found")
        for tid in ("so-3a4739b9/0/1", "so-3a4739b9/1/1000001", "so-3A4739b9/1/1", "x", 5):
            with self.subTest(tid=tid):
                error = self.refused("invalid_request", self.find, tid)
                self.assertEqual(error.status, "400 Bad Request")

    def test_first_access_verifies_the_body_then_scans_from_the_offset(self):
        self.add(make_package(targets=64))
        set_id = self.store.sets(PID, FP)
        set_id = run(set_id)[0]["set_id"]
        doc = self.stored(set_id)
        summary = doc["summary"]
        body, offset = summary["body_bytes"], summary["targets_offset"]
        tid = doc["targets"][3]["tid"]
        store = self.fresh()
        self.sets(store)  # the listing reads the trailer, outside the spy
        reads = []
        for attempt in ("first", "after invalidate"):
            with self.subTest(attempt):
                del reads[:]
                with self.spied(reads):
                    self.assertEqual(self.find(tid, store=store),
                                     self.expected(doc["targets"][3], set_id))
                # From byte 0 to the body's end in a row, never a trailer byte.
                self.assertEqual(reads[0][0], 0)
                self.assertTrue(all(at + count <= body for at, _, count in reads), reads)
                self.assertEqual(sum(count for _, _, count in reads), body)
                self.assertEqual([at for at, _, _ in reads[1:]],
                                 [at + count for at, _, count in reads[:-1]])
                self.assertTrue(all(size <= stakeout.SCAN_PIECE for _, size, _ in reads))
                # Later lookups start at targets_offset.
                for _ in range(2):
                    del reads[:]
                    with self.spied(reads):
                        self.assertEqual(self.find(tid, store=store)["tid"], tid)
                    self.assertEqual(reads[0][0], offset)
                    self.assertTrue(all(at + count <= body for at, _, count in reads))
                    self.assertLess(sum(count for _, _, count in reads), body - offset)
                store.invalidate(PID)
                self.sets(store)
        self.assertEqual(store.readers(PID), 0)

    def test_digest_mismatch_marks_the_set_damaged(self):
        self.add()
        data = self.read(self.path())
        at = data.index(b'"lat_e8": 5100000027') + len(b'"lat_e8": 510000002')
        self.assertEqual(data[at:at + 1], b"7")
        self.write(self.path(), data[:at] + b"8" + data[at + 1:])
        store = self.fresh()
        self.assertEqual(self.states(store)[SAMPLE_SET_ID], "ok")
        self.log.reset_mock()
        error = self.refused("set_damaged", self.find, "so-3a4739b9/2/1", store=store)
        self.assertEqual((error.status, error.set_id), ("409 Conflict", SAMPLE_SET_ID))
        self.assertEqual(self.log.call_args[0][0], "ERROR")
        self.assertEqual(store.readers(PID), 0)
        self.assertEqual(self.states(store)[SAMPLE_SET_ID], "damaged")
        with self.no_file_access():
            self.assertIs(store.is_damaged(PID, SAMPLE_SET_ID), True)
            self.assertIs(store.is_damaged(PID, "so-00000000"), False)
            # Refused from the cached state, before the file.
            self.refused("set_damaged", self.find, "so-3a4739b9/1/1", store=store)

    def test_is_damaged_sees_a_cached_damage_marker(self):
        self.add()
        store = self.fresh()
        self.sets(store)
        with self.no_file_access():
            self.assertIs(store.is_damaged(PID, SAMPLE_SET_ID), False)
        data = self.read(self.path())
        self.write(self.path(), data[:-8] + b"x" * 8)
        self.assertIsNone(stakeout._read_trailer(self.path(), PID, SAMPLE_SET_ID))
        store.invalidate(PID)
        with self.no_file_access():
            # A cold cache knows nothing and does not list.
            self.assertIs(store.is_damaged(PID, SAMPLE_SET_ID), False)
        self.assertEqual(self.states(store), {SAMPLE_SET_ID: "damaged"})
        self.assertNotIn(store.set_path(PID, SAMPLE_SET_ID), store._damaged)
        with self.no_file_access():
            self.assertIs(store.is_damaged(PID, SAMPLE_SET_ID), True)
            self.assertIs(store.is_damaged(PID, "so-00000000"), False)
            self.assertIs(store.is_damaged("project-other", SAMPLE_SET_ID), False)

    def test_target_line_over_the_limit_is_damage(self):
        plan = run(stakeout.validate_import(sample_package(PID)))
        doc = set_doc(plan, project_created_at=FP, imported_at=845000000)
        # Twice the limit: the scan stops long before the line's end.
        doc["targets"][0]["props"] = {"note": "x" * (2 * stakeout.TARGET_LINE_MAX)}
        os.makedirs(os.path.dirname(self.path()))
        summary = run(stakeout._store_set_file(self.path(), doc))
        data = self.read(self.path())
        body = data[:-stakeout.TRAILER_BYTES]
        marker = b'\n"targets": [\n'
        self.assertEqual(summary["targets_offset"], body.index(marker) + len(marker))
        self.assertEqual(summary["body_sha256"], hashlib.sha256(body).hexdigest())
        self.assertEqual(summary["body_bytes"], len(body))
        lines = body[summary["targets_offset"]:].split(b"\n")
        self.assertGreater(len(lines[0]), 2 * stakeout.TARGET_LINE_MAX)
        for tid in ("so-3a4739b9/1/1", "so-3a4739b9/2/1"):
            with self.subTest(tid=tid):
                store = self.fresh()
                self.assertEqual(self.states(store)[SAMPLE_SET_ID], "ok")
                reads = []
                with self.spied(reads):
                    self.refused("set_damaged", self.find, tid, store=store)
                # Never more than a line limit and a piece buffered.
                self.assertLessEqual(max(at + count for at, _, count in reads),
                                     summary["targets_offset"] + stakeout.TARGET_LINE_MAX
                                     + stakeout.SCAN_PIECE)
                self.assertEqual(self.states(store)[SAMPLE_SET_ID], "damaged")
                self.assertEqual(store.readers(PID), 0)

    def test_a_target_line_of_wrong_types_is_damage(self):
        plan = run(stakeout.validate_import(sample_package(PID)))
        os.makedirs(os.path.dirname(self.path()))
        for key, value in (("lat_e8", "5100000027"), ("lat_e8", True),
                           ("lat_e8", 9000000001), ("lon_e8", 999999943.0),
                           ("lon_e8", -18000000001), ("name", 5), ("name", None),
                           ("alt_import", 412.3), ("alt_import", "412300")):
            with self.subTest(key=key, value=value):
                doc = set_doc(plan, project_created_at=FP, imported_at=845000000)
                doc["targets"][4][key] = value
                run(stakeout._store_set_file(self.path(), doc))
                store = self.fresh()
                self.refused("set_damaged", self.find, "so-3a4739b9/2/1", store=store)
                self.assertEqual(self.states(store)[SAMPLE_SET_ID], "damaged")
                os.remove(self.path())
        # The same line intact is found; another target never looks at it.
        doc = set_doc(plan, project_created_at=FP, imported_at=845000000)
        run(stakeout._store_set_file(self.path(), doc))
        self.assertEqual(self.find("so-3a4739b9/2/1", store=self.fresh())["name"], "Point 2")

    def test_prop_named_tid_does_not_fool_the_scan(self):
        pkg = sample_package(PID)
        pkg["targets"][0]["props"] = {"tid": "so-3a4739b9/1/2"}
        self.add(pkg)
        doc = self.stored()
        self.assertEqual(doc["targets"][0]["props"], {"tid": "so-3a4739b9/1/2"})
        store = self.fresh()
        for _ in range(2):  # the checking pass, then the scan
            found = self.find("so-3a4739b9/1/2", store=store)
            self.assertEqual(found, self.expected(doc["targets"][1]))
            self.assertEqual(found["name"], "Line A / P002")

    def test_scan_counts_as_reader(self):
        set_id = self.add(make_package(targets=64))["set_id"]
        tid = self.stored(set_id)["targets"][-1]["tid"]
        real_pause = stakeout._pause

        async def scenario(store, fail):
            entered, release = asyncio.Event(), asyncio.Event()

            async def held(meter):
                entered.set()
                await release.wait()
                await real_pause(meter)

            with mock.patch.object(stakeout, "_pause", held):
                task = asyncio.ensure_future(store.find_target(PID, FP, tid))
                await asyncio.wait_for(entered.wait(), 5)
                self.assertEqual(store.readers(PID), 1)
                with self.assertRaises(stakeout.StakeoutError) as caught:
                    await store.delete_set(PID, FP, set_id, 0)
                self.assertEqual(caught.exception.code, "storage_busy")
                failing["on"] = fail
                release.set()
                try:
                    return await task
                finally:
                    failing["on"] = False

        for fail in (True, False):
            with self.subTest(fail=fail):
                store = self.fresh()
                self.sets(store)
                failing = {"on": False}
                with self.spied([], failing):
                    if fail:
                        error = self.refused("stakeout_read_failed", run,
                                             scenario(store, True))
                        self.assertEqual(error.status, "507 Insufficient Storage")
                    else:
                        self.assertEqual(run(scenario(store, False))["tid"], tid)
                self.assertEqual(store.readers(PID), 0)
        self.delete(set_id, store=store)
        self.assertEqual(self.files(), [])

    def test_io_and_memory_errors(self):
        self.add()
        store = self.fresh()
        self.sets(store)
        with mock.patch("builtins.open", _failing_open(lambda path, mode: True,
                                                       OSError(5, "io"))):
            error = self.refused("stakeout_read_failed", self.find, "so-3a4739b9/1/1",
                                 store=store)
        self.assertEqual(error.status, "507 Insufficient Storage")
        self.assertNotIn(PID, store._cache)
        self.assertEqual(store.readers(PID), 0)
        self.sets(store)
        with mock.patch.object(stakeout.hashlib, "sha256", side_effect=MemoryError):
            error = self.refused("stakeout_busy", self.find, "so-3a4739b9/1/1", store=store)
        self.assertEqual(error.status, "503 Service Unavailable")
        self.assertEqual(store.readers(PID), 0)
        self.assertEqual(self.states(store)[SAMPLE_SET_ID], "ok")
        # A file gone meanwhile is no damage: the cache is listed again.
        os.remove(self.path())
        error = self.refused("set_not_found", self.find, "so-3a4739b9/1/1", store=store)
        self.assertEqual(error.status, "404 Not Found")
        self.assertNotIn(PID, store._cache)
        self.assertEqual(store.readers(PID), 0)

    def test_orphaned_and_damaged_sets_are_refused(self):
        self.add()
        error = self.refused("set_not_found", self.find, "so-3a4739b9/1/1",
                             fingerprint=FP + 1)
        self.assertEqual(error.status, "404 Not Found")
        # check_project runs after the lookup; what it raises propagates.
        opened = []

        def counting_open(path, mode="r", *args, **kwargs):
            opened.append(path)
            return _real_open(path, mode, *args, **kwargs)

        def gone(pid):
            raise RuntimeError("project check")

        self.sets()
        with mock.patch("builtins.open", counting_open):
            with self.assertRaises(RuntimeError):
                self.find("so-3a4739b9/1/1", check=gone)
            self.refused("project_not_found", self.find, "so-3a4739b9/1/1",
                         check=lambda pid: FP + 1)
        self.assertEqual(opened, [])
        self.assertEqual(self.store.readers(PID), 0)
        self.assertEqual(self.find("so-3a4739b9/1/1", check=lambda pid: FP)["tid"],
                         "so-3a4739b9/1/1")
        # A damaged and an incomplete set are refused from their entry.
        data = self.read(self.path())
        self.write(self.path(), data[:-8] + b"x" * 8)
        self.write(self.path("so-0000000e") + ".tmp", b'{"kind"')
        store = self.fresh()
        for set_id in (SAMPLE_SET_ID, "so-0000000e"):
            with self.subTest(set_id=set_id):
                error = self.refused("set_damaged", self.find, set_id + "/1/1",
                                     store=store)
                self.assertEqual((error.status, error.set_id), ("409 Conflict", set_id))

    def test_scan_of_a_large_set_is_paced(self):
        pkg = make_package(targets=496, lines=4, coords_per_line=500, offset=0)
        self.assertLessEqual(len(compact(pkg)), stakeout.MAX_IMPORT_BYTES)
        set_id = self.add(pkg)["set_id"]
        doc = self.stored(set_id)
        body = doc["summary"]["body_bytes"]
        last = doc["targets"][-1]
        real_pause = stakeout._pause
        pauses = [0]

        async def counted(meter):
            pauses[0] += 1
            await real_pause(meter)

        reads = []
        store = self.fresh()
        with self.spied(reads), mock.patch.object(stakeout, "_pause", counted):
            self.assertEqual(self.find(last["tid"], store=store),
                             self.expected(last, set_id))
        self.assertGreaterEqual(pauses[0], body // stakeout.SCAN_PIECE)
        limit = max(stakeout.SCAN_PIECE, stakeout.TARGET_LINE_MAX)
        self.assertTrue(all(size <= limit and 0 < size and count <= limit
                            for _, size, count in reads))
        # On the device cost model, cold (listing and checking pass) and warm.
        model = _CostModel()
        store = self.fresh()
        with model.installed():
            for label in ("cold", "warm"):
                with self.subTest(label):
                    model.begin()
                    self.assertEqual(self.find(last["tid"], store=store)["tid"], last["tid"])
                    worst, _ = model.end()
                    self.assertGreater(store.last_scan_block_ms, 0)
                    self.assertLess(store.last_scan_block_ms, 250)
                    self.assertLess(worst, 250)
                    self.assertGreater(store.last_scan_ms, store.last_scan_block_ms)



class TestTargetIdLimits(unittest.TestCase):
    def test_tid_accepts_every_importable_sequence(self):
        self.assertEqual(stakeout.parse_tid("so-3a4739b9/1/2001"),
                         ("so-3a4739b9", 1, 2001))
        self.assertEqual(stakeout.MAX_TARGET_SEQ, 1000000)
        self.assertEqual(stakeout.parse_tid("so-3a4739b9/1/1000000"),
                         ("so-3a4739b9", 1, 1000000))
        for tid in ("so-3a4739b9/1/1000001", "so-3a4739b9/0/1", "so-3a4739b9/501/1",
                    "so-3a4739b9/1/01", "so-3a4739b9/1/+5", "so-3a4739b9/1/-1",
                    "so-3a4739b9/1/1.0"):
            with self.subTest(tid=tid):
                self.assertIsNone(stakeout.parse_tid(tid))

    def test_target_keys_never_collide(self):
        self.assertEqual(stakeout.SEQ_BITS, 20)
        keys = {}
        for part in (1, 2, 499, 500):
            for seq in (1, 2000, 2001, 2048, 999999, 1000000):
                key = stakeout.target_key(part, seq)
                self.assertNotIn(key, keys, (part, seq, keys.get(key)))
                keys[key] = (part, seq)
        self.assertEqual(stakeout.target_key(500, 1000000), 525288000)
        self.assertLess(stakeout.target_key(500, 1000000), 2 ** 30)



# --- one change lock, uncertain-write latch, removal in steps (WP4 A2) ------

@contextlib.contextmanager
def _writes_and_pauses(fail=None):
    """Records every os.remove, os.rmdir and os.rename of stakeout (by base
    name) and every service pause; fail(kind, name), if given, may return an
    exception to raise instead of the call."""
    events = []
    real = {kind: getattr(os, kind) for kind in ("remove", "rmdir", "rename")}
    real_sleep = stakeout.asyncio.sleep_ms

    def recording(kind):
        def call(path, *args):
            name = os.path.basename(path)
            events.append((kind, name))
            error = fail(kind, name) if fail is not None else None
            if error is not None:
                raise error
            return real[kind](path, *args)
        return call

    async def sleep_ms(ms):
        events.append(("sleep",))
        await real_sleep(ms)

    with contextlib.ExitStack() as stack:
        for kind in real:
            stack.enter_context(mock.patch.object(stakeout.os, kind, recording(kind)))
        stack.enter_context(mock.patch.object(stakeout.asyncio, "sleep_ms", sleep_ms))
        yield events


def _nth(kind, number, error):
    """A fail() for _writes_and_pauses: error at the number-th call of kind."""
    seen = []

    def fail(called, _name):
        if called == kind:
            seen.append(called)
            if len(seen) == number:
                return error
        return None
    return fail


class _LockCase(_StoreCase):
    def assert_paced(self, events):
        """Every write is followed by a pause before the next write or the end."""
        last = None
        for event in events:
            if event[0] == "sleep":
                last = "sleep"
            else:
                self.assertNotEqual(last, "write", events)
                last = "write"
        self.assertEqual(last, "sleep", events)

    def warned(self):
        return [call for call in self.log.call_args_list if call[0][0] == "WARN"]

    def locked_clear(self, store=None, project_id=PID):
        store = store or self.store

        async def scenario():
            async with store.mutation():
                return await store.clear_removed(project_id)
        return run(scenario())

    def imported_at(self, store=None, set_id=SAMPLE_SET_ID):
        for summary in self.sets(store):
            if summary["set_id"] == set_id:
                return summary["imported_at"]
        raise AssertionError("not listed: " + set_id)


class TestErrorExtras(unittest.TestCase):
    def test_extra_fields_join_the_envelope(self):
        error = stakeout.StakeoutError(
            "not_ready", "m", "409 Conflict", path="p", set_id=SAMPLE_SET_ID,
            extra={"state": "fine", "reasons": ["outside_tolerance"], "error": "x",
                   "message": "y", "path": "q", "set_id": "z"})
        self.assertEqual(error.payload(), {
            "error": "not_ready", "message": "m", "path": "p", "set_id": SAMPLE_SET_ID,
            "state": "fine", "reasons": ["outside_tolerance"]})
        self.assertEqual(stakeout.StakeoutError("not_ready", "m", "409 Conflict",
                                                extra={"gen": 0}).payload(),
                         {"error": "not_ready", "message": "m", "gen": 0})
        self.assertEqual(stakeout.StakeoutError("invalid_request", "m").payload(),
                         {"error": "invalid_request", "message": "m"})

    def test_the_uncertain_write_error(self):
        error = stakeout.write_uncertain_error()
        self.assertEqual((error.code, error.status),
                         ("stakeout_write_uncertain", "503 Service Unavailable"))
        self.assertEqual(error.message,
                         "A stakeout change may not be stored; restart the device.")
        self.assertEqual(error.payload(), {"error": "stakeout_write_uncertain",
                                           "message": error.message})


class TestMutationLock(_LockCase):
    def test_one_lock_for_every_set_change(self):
        self.add()
        before = self.snapshot()
        store = self.store

        async def refused(call):
            with self.assertRaises(stakeout.StakeoutError) as caught:
                await call()
            self.assertEqual((caught.exception.code, caught.exception.status),
                             ("storage_busy", "409 Conflict"))

        async def nested():
            async with store.mutation():
                pass

        async def begin():
            store.begin_project_delete(PID)

        async def scenario():
            async with store.mutation():
                self.assertIs(store.busy, True)
                await refused(lambda: store.import_package(
                    compact(make_package(targets=3)), self.check, self.ok))
                await refused(lambda: store.delete_set(PID, FP, SAMPLE_SET_ID, 0,
                                                       on_remove=lambda *args: None))
                await refused(nested)
                await refused(begin)
                self.assertIs(store.busy, True)
            self.assertIs(store.busy, False)
            with self.assertRaises(RuntimeError):
                async with store.mutation():
                    raise RuntimeError("inside")
            self.assertIs(store.busy, False)
            async with store.mutation(write=False):
                self.assertIs(store.busy, True)
            self.assertIs(store.busy, False)

        run(scenario())
        self.assertEqual(self.snapshot(), before)
        self.add(make_package(targets=3))
        self.assertEqual(self.delete(), "done")
        self.assertEqual(len(self.sets()), 1)

    def test_uncertain_refuses_every_write_lock(self):
        self.add()
        before = self.snapshot()
        listed = self.sets()
        store = self.store
        store.uncertain = "test"

        async def enter():
            async with store.mutation():
                raise AssertionError("entered")

        calls = (
            ("mutation", enter),
            ("import", lambda: store.import_package(
                compact(make_package(targets=3)), self.check, self.ok)),
            ("delete", lambda: store.delete_set(PID, FP, SAMPLE_SET_ID, 0,
                                                on_remove=lambda *args: None)),
            ("legacy delete", lambda: store.delete_set(PID, FP, SAMPLE_SET_ID, 0)),
        )
        for busy in (False, True):
            for label, call in calls:
                with self.subTest(label, busy=busy):
                    store.busy = busy
                    error = self.refused("stakeout_write_uncertain", call)
                    self.assertEqual(error.status, "503 Service Unavailable")
                    self.assertIn("restart", error.message)
                    self.assertIs(store.busy, busy)
        store.busy = False
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.sets(), listed)

        async def read():
            async with store.mutation(write=False):
                self.assertIs(store.busy, True)

        run(read())
        self.assertIs(store.busy, False)
        self.assertEqual(store.uncertain, "test")
        self.assertIs(store.begin_project_delete(PID), True)
        self.assertEqual(run(store.finish_project_delete(PID)), "done")
        self.assertIsNone(self.fresh().uncertain)
        self.assertIsNone(stakeout.TargetStore(self.root).uncertain)


class TestDeleteSetCommit(_LockCase):
    def setUp(self):
        super().setUp()
        data = self.stored_sample()
        self.write(self.path(), data)
        self.write(self.path() + ".tmp", data[:100])
        self.store = self.fresh()
        self.stamp = self.imported_at()

    def test_commit_comes_first_and_files_follow_in_steps(self):
        name = SAMPLE_SET_ID + ".json"
        with _writes_and_pauses() as events:
            async def prepare(project_id):
                events.append(("prepare", project_id))

            def count(project_id, set_id):
                events.append(("count", project_id, set_id))
                return 0

            def check(project_id):
                events.append(("check", project_id))
                return FP

            def on_remove(project_id, set_id, imported_at):
                events.append(("on_remove", project_id, set_id, imported_at,
                               os.path.exists(self.path())))

            result = self.delete(check_project=check, prepare=prepare, count=count,
                                 on_remove=on_remove)
        self.assertEqual(result, "done")
        self.assertEqual(events, [
            ("prepare", PID), ("count", PID, SAMPLE_SET_ID), ("check", PID),
            ("on_remove", PID, SAMPLE_SET_ID, self.stamp, True), ("sleep",),
            ("remove", name), ("sleep",), ("remove", name + ".tmp"), ("sleep",)])
        self.assert_paced(events[4:])
        self.assertEqual(self.files(), [])
        self.assertEqual(self.sets(), [])
        self.assertEqual(self.sets(self.fresh()), [])
        self.assertIs(self.store.remnants(PID), False)
        self.assertEqual(self.store.removed, {PID: {SAMPLE_SET_ID: self.stamp}})
        self.assertIs(self.store.busy, False)

    def test_count_replaces_the_confirmed_count(self):
        commits = []

        def on_remove(*args):
            commits.append(args)

        before = self.snapshot()
        error = self.refused("confirmed_changed", self.store.delete_set, PID, FP,
                             SAMPLE_SET_ID, 0, count=lambda pid, set_id: 2,
                             on_remove=on_remove)
        self.assertEqual((error.status, error.set_id), ("409 Conflict", SAMPLE_SET_ID))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(commits, [])
        self.assertIs(self.store.busy, False)
        self.assertEqual(run(self.store.delete_set(
            PID, FP, SAMPLE_SET_ID, 2, count=lambda pid, set_id: 2,
            on_remove=on_remove)), "done")
        self.assertEqual(commits, [(PID, SAMPLE_SET_ID, self.stamp)])
        self.assertEqual(self.files(), [])

    def test_commit_refusal_keeps_everything(self):
        before = self.snapshot()
        listed = self.sets()
        for error in (stakeout.StakeoutError("storage_reserve", "No flash.",
                                             "507 Insufficient Storage"),
                      stakeout.write_uncertain_error()):
            with self.subTest(error.code):
                def on_remove(*args):
                    raise error

                with _writes_and_pauses() as events:
                    with self.assertRaises(stakeout.StakeoutError) as caught:
                        run(self.store.delete_set(PID, FP, SAMPLE_SET_ID, 0,
                                                  on_remove=on_remove))
                self.assertIs(caught.exception, error)
                self.assertEqual([event for event in events if event[0] != "sleep"], [])
                self.assertEqual(self.snapshot(), before)
                self.assertEqual(self.sets(), listed)
                self.assertEqual(self.sets(self.fresh()), listed)
                self.assertEqual(self.store.removed, {})
                self.assertIs(self.store.remnants(PID), False)
                self.assertIs(self.store.busy, False)

    def test_failed_removal_after_the_commit_is_pending(self):
        with mock.patch.object(stakeout.os, "remove", side_effect=OSError(5, "io")):
            result = self.delete(on_remove=lambda *args: None)
        self.assertEqual(result, "pending")
        self.assertTrue(self.warned())
        self.assertEqual(self.sets(), [])
        self.assertEqual(self.files(), [self.name(), self.name() + ".tmp"])
        self.assertIs(self.store.remnants(PID), True)
        self.assertIs(self.store.busy, False)
        self.assertIs(self.locked_clear(), True)
        self.assertEqual(self.files(), [])
        self.assertIs(self.store.remnants(PID), False)
        self.assertEqual(self.sets(), [])

    def test_without_on_remove_the_removal_is_the_commit(self):
        listed = self.sets()
        with mock.patch.object(stakeout.os, "remove", side_effect=OSError(5, "io")):
            error = self.refused("stakeout_delete_failed", self.store.delete_set, PID, FP,
                                 SAMPLE_SET_ID, 0)
        self.assertEqual(error.status, "507 Insufficient Storage")
        self.assertEqual(self.sets(), listed)
        self.assertEqual(self.files(), [self.name(), self.name() + ".tmp"])
        self.assertEqual(self.store.removed, {})
        self.assertIs(self.store.remnants(PID), False)
        self.assertEqual(self.delete(), "done")
        self.assertEqual(self.files(), [])


class TestRemnants(_LockCase):
    def test_remnant_is_hidden_after_a_restart(self):
        self.add()
        other = self.add(make_package(targets=3))["set_id"]
        stamp = self.imported_at()
        store = self.fresh()
        store.note_removed(PID, {SAMPLE_SET_ID: stamp})
        listed = self.sets(store)
        self.assertEqual([summary["set_id"] for summary in listed], [other])
        self.assertEqual(sum(summary["targets"] for summary in listed), 3)
        self.assertIs(store.remnants(PID), True)
        self.refused("set_not_found", store.find_target, PID, FP, SAMPLE_SET_ID + "/1/1")
        self.refused("set_not_found", store.delete_set, PID, FP, SAMPLE_SET_ID, 0)
        self.assertIn(self.name(), self.files())
        # A marker of another import of the same set does not hide it.
        store = self.fresh()
        store.note_removed(PID, {SAMPLE_SET_ID: stamp - 1})
        self.assertEqual(len(self.sets(store)), 2)
        self.assertIs(store.remnants(PID), False)
        # Changed markers invalidate a warm listing; the same ones do not.
        generation = store.generation(PID)
        store.note_removed(PID, {SAMPLE_SET_ID: stamp - 1})
        self.assertEqual(store.generation(PID), generation)
        store.note_removed(PID, {SAMPLE_SET_ID: stamp})
        self.assertGreater(store.generation(PID), generation)
        self.assertEqual(len(self.sets(store)), 1)
        store.note_removed(PID, {})
        self.assertEqual(len(self.sets(store)), 2)

    def test_a_null_marker_hides_only_a_damaged_file_of_its_set(self):
        self.add()
        other = self.add(make_package(targets=3))["set_id"]
        store = self.fresh()
        store.note_removed(PID, {SAMPLE_SET_ID: None})
        self.assertEqual(len(self.sets(store)), 2)
        self.assertIs(store.remnants(PID), False)
        data = bytearray(self.read(self.path()))
        data[-10] ^= 1
        self.write(self.path(), bytes(data))
        store = self.fresh()
        self.assertEqual(self.states(store)[SAMPLE_SET_ID], "damaged")
        store.note_removed(PID, {SAMPLE_SET_ID: None})
        self.assertEqual([summary["set_id"] for summary in self.sets(store)], [other])
        self.assertIs(store.remnants(PID), True)
        # A marker with a time keeps a damaged file visible (it may be a new import).
        store = self.fresh()
        store.note_removed(PID, {SAMPLE_SET_ID: 5})
        self.assertEqual(self.states(store)[SAMPLE_SET_ID], "damaged")
        # A lone .tmp of a removed set is a remnant too.
        os.rename(self.path(), self.path() + ".tmp")
        store = self.fresh()
        self.assertEqual(self.states(store)[SAMPLE_SET_ID], "incomplete")
        store.note_removed(PID, {SAMPLE_SET_ID: 5})
        self.assertEqual([summary["set_id"] for summary in self.sets(store)], [other])
        self.assertIs(self.locked_clear(store), True)
        self.assertEqual(self.files(), [self.name(other)])

    def test_reimport_after_removal_is_fresh(self):
        self.add()
        stamp = self.imported_at()
        store = self.fresh()
        store.note_removed(PID, {SAMPLE_SET_ID: stamp})
        prepared = []

        def prepare(project_id):
            prepared.append(project_id)
            return store.clear_removed(project_id)

        with mock.patch.object(stakeout, "now_seconds", return_value=stamp):
            answer = self.add(store=store, prepare=prepare)
        self.assertEqual(prepared, [PID])
        self.assertEqual(answer["set_id"], SAMPLE_SET_ID)
        self.assertIs(store.remnants(PID), False)
        self.assertEqual(self.imported_at(store), stamp + 1)
        self.assertEqual(self.imported_at(self.fresh()), stamp + 1)
        restarted = self.fresh()
        restarted.note_removed(PID, {SAMPLE_SET_ID: stamp})
        self.assertEqual(self.imported_at(restarted), stamp + 1)

    def test_reimport_of_a_removed_incomplete_set_is_no_remnant(self):
        # A failed cleanup leaves the absent .json of a removed incomplete set
        # among the remnants; a reimport of that set must take it out again.
        self.add()
        other = self.add(make_package())["set_id"]
        self.assertLess(SAMPLE_SET_ID, other)
        os.rename(self.path(other), self.path(other) + ".tmp")
        store = self.fresh()
        self.assertEqual(self.states(store)[other], "incomplete")
        with mock.patch.object(stakeout.os, "remove", side_effect=OSError(5, "io")):
            self.assertEqual(self.delete(store=store, on_remove=lambda *args: None),
                             "pending")
        with _writes_and_pauses(_nth("remove", 1, OSError(5, "io"))):
            self.assertEqual(self.delete(other, store=store,
                                         on_remove=lambda *args: None), "pending")
        self.assertEqual(self.add(make_package(), store=store)["set_id"], other)
        self.assertIs(self.locked_clear(store), True)
        self.assertEqual(self.files(), [self.name(other)])
        self.assertEqual([summary["set_id"] for summary in self.sets(store)], [other])
        self.assertEqual(self.states(), {other: "ok"})

    def test_prepare_runs_after_the_project_check_and_before_the_listing(self):
        calls = []
        store = self.fresh()

        def check(project_id):
            calls.append(("check", PID in store._cache))
            return FP

        async def prepare(project_id):
            calls.append(("prepare", PID in store._cache))

        self.add(store=store, check=check, prepare=prepare)
        self.assertEqual(calls[:2], [("check", False), ("prepare", False)])

    def test_clearing_paces_every_file_and_keeps_failures(self):
        self.add()
        store = self.fresh()
        store.note_removed(PID, {SAMPLE_SET_ID: self.imported_at()})
        self.write(self.path() + ".tmp", b"x")
        self.sets(store)
        for number, expected, left in ((1, ["remove", "sleep"],
                                        [self.name(), self.name() + ".tmp"]),
                                       (2, ["remove", "sleep", "remove", "sleep"],
                                        [self.name() + ".tmp"])):
            with self.subTest(failing=number):
                self.log.reset_mock()
                with _writes_and_pauses(_nth("remove", number,
                                             OSError(5, "io"))) as events:
                    self.assertIs(self.locked_clear(store), False)
                self.assertEqual([event[0] for event in events], expected)
                self.assertEqual(len(self.warned()), 1)
                self.assertEqual(self.files(), left)
                self.assertIs(store.remnants(PID), True)
                self.assertEqual(self.sets(store), [])
        with _writes_and_pauses() as events:
            self.assertIs(self.locked_clear(store), True)
        self.assertEqual(events, [("remove", SAMPLE_SET_ID + ".json.tmp"), ("sleep",)])
        self.assertEqual(self.files(), [])
        self.assertIs(store.remnants(PID), False)
        with _writes_and_pauses() as events:
            self.assertIs(self.locked_clear(store), True)
        self.assertEqual(events, [])
        # A listing error is a WARN and False, never raised.
        store = self.fresh()
        with mock.patch.object(stakeout.os, "ilistdir", side_effect=OSError(5, "io")):
            self.assertIs(self.locked_clear(store), False)
        self.assertIs(store.busy, False)


class TestHolds(_LockCase):
    def test_hold_blocks_deletes(self):
        self.add()
        self.store.hold(PID)
        self.assertEqual(self.store.readers(PID), 1)
        for on_remove in (None, lambda *args: None):
            error = self.refused("storage_busy", self.store.delete_set, PID, FP,
                                 SAMPLE_SET_ID, 0, on_remove=on_remove)
            self.assertIn("download", error.message)
        self.refused("storage_busy", self.store.begin_project_delete, PID)
        self.assertEqual(self.files(), [self.name()])
        self.assertIs(self.store.busy, False)
        # A hold of another project does not block.
        self.store.hold("project-b")
        self.store.release(PID)
        self.assertEqual(self.store.readers(PID), 0)
        self.assertEqual(self.delete(on_remove=lambda *args: None), "done")
        self.assertEqual(self.files(), [])

    def test_has_data(self):
        folder = os.path.join(self.root, PID)
        self.assertIs(self.store.has_data(PID), False)
        self.write(os.path.join(folder, "notes.txt"), b"x")
        self.write(os.path.join(folder, "progress.jsonl.foreign"), b"x")
        self.assertIs(self.store.has_data(PID), False)
        self.write(os.path.join(folder, "progress.jsonl"), b"{}\n")
        self.assertIs(self.store.has_data(PID), True)
        os.remove(os.path.join(folder, "progress.jsonl"))
        self.add()
        self.assertIs(self.store.has_data(PID), True)
        self.assertIs(self.store.has_data("../x"), False)
        self.assertIs(self.store.has_data("project-b"), False)


class TestClearPacing(_LockCase):
    def setUp(self):
        super().setUp()
        self.folder = os.path.join(self.root, "project-a.deleting")
        self.write(os.path.join(self.folder, "a.json"), b"x")
        self.write(os.path.join(self.folder, "progress.jsonl"), b"x")

    def clear(self, folder=None):
        return run(self.store._clear(folder or self.folder))

    def test_every_write_in_clear_is_followed_by_a_pause(self):
        with _writes_and_pauses() as events:
            self.assertIs(self.clear(), True)
        self.assertEqual([event[0] for event in events],
                         ["remove", "sleep", "remove", "sleep", "rmdir", "sleep"])
        self.assertEqual(sorted(event[1] for event in events if event[0] == "remove"),
                         ["a.json", "progress.jsonl"])
        self.assertFalse(os.path.exists(self.folder))
        self.assertEqual(self.warned(), [])

    def test_a_failure_is_followed_by_a_pause_too(self):
        cases = (("remove", 2, ["remove", "sleep", "remove", "sleep"]),
                 ("rmdir", 1, ["remove", "sleep", "remove", "sleep", "rmdir", "sleep"]))
        for kind, number, expected in cases:
            with self.subTest(kind):
                self.setUp()
                self.log.reset_mock()
                with _writes_and_pauses(_nth(kind, number, OSError(5, "io"))) as events:
                    self.assertIs(self.clear(), False)
                self.assertEqual([event[0] for event in events], expected)
                self.assertEqual(len(self.warned()), 1)
                self.assertTrue(os.path.isdir(self.folder))

    def test_a_missing_folder_writes_nothing(self):
        with _writes_and_pauses() as events:
            self.assertIs(self.clear(os.path.join(self.root, "none.deleting")), True)
        self.assertEqual(events, [])

    def test_two_folders_never_write_back_to_back(self):
        second = os.path.join(self.root, "project-b.deleting")
        self.write(os.path.join(second, "b.json"), b"x")
        self.write(os.path.join(second, "progress.jsonl"), b"x")
        with _writes_and_pauses() as events:
            run(self.store.reconcile_deletions(_Survey("project-live")))
        self.assertFalse(os.path.exists(self.folder))
        self.assertFalse(os.path.exists(second))
        self.assertEqual(len([event for event in events if event[0] != "sleep"]), 6)
        self.assert_paced(events)
        self.add(sample_package("project-c"))
        self.assertIs(self.store.begin_project_delete("project-c"), True)
        with _writes_and_pauses() as events:
            self.assertEqual(run(self.store.finish_project_delete("project-c")), "done")
        self.assertEqual([event[0] for event in events if event[0] != "sleep"],
                         ["remove", "rmdir"])
        self.assert_paced(events)


if __name__ == "__main__":
    unittest.main()
