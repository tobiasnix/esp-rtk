# SPDX-License-Identifier: AGPL-3.0-only
"""Stakeout WP3: browser geometry, GeoJSON import, routes, live poll and guidance.

The browser functions run under Node straight from the single-line declarations of app.js
(run_app_node from tests/test_navigation.py); the device side (geo, stakeout) is the
reference they are checked against.
"""
import decimal
import json
import math
import os
import random
import re
import unittest

import support  # noqa: F401  (MicroPython shims for the device modules)
import geo
from test_navigation import run_app_node
from test_stakeout_store import FP, PID, _StoreCase, run

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "tests", "fixtures")

GEOMETRY_PREFIXES = ("const tr=", "const fmt=", "function stakeoutOffsets(", "function stakeoutDistance(",
                     "function stakeoutBearing(", "function stakeoutCardinal(", "function stakeoutDirectionText(",
                     "const STAKEOUT_FINE_ENTER_M=", "function stakeoutViewMode(", "function dialRingRadius(",
                     "function dialRoverPosition(")


DECLARATION = re.compile(r"^(async function [A-Za-z0-9_$]+\(|function [A-Za-z0-9_$]+\(|const [A-Za-z0-9_$]+=|let [A-Za-z0-9_$]+=)")


def declared_prefixes(*names):
    """The declaration prefix of every top-level line of the named src files (one declaration per line)."""
    prefixes = []
    for name in names:
        with open(os.path.join(ROOT, "src", name), encoding="utf-8") as handle:
            for line in handle:
                match = DECLARATION.match(line)
                if match:
                    prefixes.append(match.group(1))
    return tuple(prefixes)


def import_prefixes():
    return ("const tr=", "const fmt=") + declared_prefixes("93-stakeout-guide.js", "94-stakeout-import.js")


def fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as handle:
        return json.load(handle)


class TestStakeoutGeometry(unittest.TestCase):

    def tolerance_m(self, distance_m):
        # The tolerances of tests/test_geo.py TestLocalGeometry.
        return 0.001 if distance_m <= 1000 else 0.002 if distance_m <= 10000 else 0.2

    def node_offsets(self, pairs):
        out = run_app_node(GEOMETRY_PREFIXES, """
const pairs = %s;
out.rows = pairs.map(p => {
  const o = stakeoutOffsets(p[0], p[1], p[2], p[3]);
  return [o[0], o[1], stakeoutDistance(p[0], p[1], p[2], p[3]), stakeoutBearing(o[0], o[1])];
});
""" % json.dumps(pairs))
        self.assertEqual(out["failures"], [])
        return out["rows"]

    def test_offsets_match_the_vincenty_vectors(self):
        vectors = fixture("stakeout_geometry_vectors.json")["vectors"]
        rows = self.node_offsets([v["ref"] + v["target"] for v in vectors])
        for vector, (_north, _east, distance, bearing) in zip(vectors, rows):
            self.assertAlmostEqual(distance, vector["distance_m"], delta=self.tolerance_m(vector["distance_m"]),
                                   msg=vector["name"])
            if vector["distance_m"] <= 1000 and vector.get("bearing_deg") is not None:
                diff = (bearing - vector["bearing_deg"] + 180) % 360 - 180
                self.assertLessEqual(abs(diff), 0.01, vector["name"])

    def test_offsets_equal_the_device_formula(self):
        vectors = fixture("stakeout_geometry_vectors.json")["vectors"]
        pairs = [v["ref"] + v["target"] for v in vectors]
        rng = random.Random(20261029)
        centres = [(5100000000, 1000000000), (-3300000000, 15100000000), (5000000000, -50000000),
                   (5000000000, 17999990000), (5000000000, -17999990000)]
        for index in range(200):
            lat, lon = centres[index % len(centres)]
            # |delta| up to about 10 km in both axes.
            dlat = rng.randint(-9000000, 9000000)
            dlon = rng.randint(-14000000, 14000000)
            target_lon = lon + dlon
            if target_lon > 18000000000:
                target_lon -= 36000000000
            elif target_lon < -18000000000:
                target_lon += 36000000000
            pairs.append([lat, lon, lat + dlat, target_lon])
        rows = self.node_offsets(pairs)
        for pair, (north, east, _distance, _bearing) in zip(pairs, rows):
            ref_north, ref_east = geo.offsets_e8_m(*pair)
            self.assertLessEqual(abs(north - ref_north), 1e-6, pair)
            self.assertLessEqual(abs(east - ref_east), 1e-6, pair)

    def test_point_two_vector(self):
        for language in ("en", "de"):
            with self.subTest(language=language):
                out = run_app_node(GEOMETRY_PREFIXES, """
RtkI18n.setLanguage(%s);
const o = stakeoutOffsets(5100000000, 1000000000, 5100000027, 999999943);
out.north = o[0]; out.east = o[1];
out.distance = stakeoutDistance(5100000000, 1000000000, 5100000027, 999999943);
out.bearing = stakeoutBearing(o[0], o[1]);
out.cardinal = stakeoutCardinal(out.bearing);
out.text = stakeoutDirectionText(0.03, -0.04);
out.ring = dialRingRadius(0.1, 0.05);
out.rover = dialRoverPosition(0.03, -0.04, 0.5);
out.expected = dict[%s]["stakeout.go_north"].replace("{value}", "0%s03") + " / " +
  dict[%s]["stakeout.go_west"].replace("{value}", "0%s04");
""" % (json.dumps(language), json.dumps(language), "," if language == "de" else ".",
                    json.dumps(language), "," if language == "de" else "."))
                self.assertEqual(out["failures"], [])
                self.assertAlmostEqual(out["north"], 0.030, delta=0.0005)
                self.assertAlmostEqual(out["east"], -0.040, delta=0.0005)
                self.assertAlmostEqual(out["distance"], 0.050, delta=0.0005)
                self.assertAlmostEqual(out["bearing"], 306.87, delta=0.05)
                self.assertEqual(out["cardinal"], "NW")
                self.assertEqual(out["text"], out["expected"])
                if language == "en":
                    self.assertEqual(out["text"], "0.03 m north / 0.04 m west")
                self.assertEqual(out["ring"], 0.5)
                self.assertAlmostEqual(out["rover"]["x"], 7.2, delta=1e-9)
                self.assertAlmostEqual(out["rover"]["y"], 5.4, delta=1e-9)
                self.assertIs(out["rover"]["outside"], False)

    def test_direction_text_edges(self):
        out = run_app_node(GEOMETRY_PREFIXES, """
out.on = stakeoutDirectionText(0.004, -0.004);
out.north_only = stakeoutDirectionText(0.5, 0.001);
out.south_east = stakeoutDirectionText(-1.25, 2);
out.none = stakeoutDirectionText(null, 0.2);
out.on_text = dict.en["stakeout.on_target"];
""")
        self.assertEqual(out["on"], out["on_text"])
        self.assertEqual(out["north_only"], "0.50 m north")
        self.assertEqual(out["south_east"], "1.25 m south / 2.00 m east")
        self.assertEqual(out["none"], "—")

    def test_ring_radius_steps(self):
        out = run_app_node(GEOMETRY_PREFIXES, """
out.values = [[0.1,0],[0.02,0.01],[0.1,0.2],[0.1,0.4],[0.1,1],[0.1,3],[0.1,40]].map(p => dialRingRadius(p[0], p[1]));
""")
        self.assertEqual(out["values"], [0.5, 0.1, 0.5, 1, 2, 5, 5])

    def test_dial_clamps_outside_the_ring(self):
        out = run_app_node(GEOMETRY_PREFIXES, "out.rover = dialRoverPosition(3, 0, 0.5);")
        self.assertAlmostEqual(out["rover"]["y"], 95, delta=1e-9)
        self.assertAlmostEqual(out["rover"]["x"], 0, delta=1e-9)
        self.assertIs(out["rover"]["outside"], True)

    def test_view_mode_hysteresis(self):
        out = run_app_node(GEOMETRY_PREFIXES, """
const walk = manual => { let mode = "search"; return [10, 4, 2.9, 4, 5, 5.01, null].map(d => (mode = stakeoutViewMode(mode, d, manual))); };
out.auto = walk(null); out.fine = walk("fine"); out.search = walk("search");
""")
        self.assertEqual(out["auto"], ["search", "search", "fine", "fine", "fine", "search", "search"])
        self.assertEqual(out["fine"], ["fine"] * 7)
        self.assertEqual(out["search"], ["search"] * 7)

    def test_bearing_and_cardinal_edges(self):
        out = run_app_node(GEOMETRY_PREFIXES, """
out.b = [stakeoutBearing(0, 0), stakeoutBearing(1, 0), stakeoutBearing(0, -1)];
out.c = [stakeoutCardinal(22.4), stakeoutCardinal(22.5), stakeoutCardinal(337.5), stakeoutCardinal(null),
         stakeoutCardinal(90), stakeoutCardinal(180), stakeoutCardinal(225), stakeoutCardinal(359.9)];
""")
        self.assertEqual(out["b"], [None, 0, 270])
        self.assertEqual(out["c"], ["N", "NE", "N", None, "E", "S", "SW", "N"])



def validate(package):
    """The device validator on the host: the set document, or the StakeoutError."""
    import asyncio
    import stakeout
    return asyncio.run(stakeout.validate_import(package))


def run_import(body, language="en"):
    out = run_app_node(import_prefixes(), "const fs = require('fs');\nRtkI18n.setLanguage(%s);\n" % json.dumps(language)
                       + body)
    if out["failures"]:
        raise AssertionError(out["failures"])
    return out


SAMPLE_BODY = """
const text = fs.readFileSync('tests/fixtures/stakeout_sample.geojson', 'utf8');
const file = {name: 'stakeout_sample.geojson', size: fs.statSync('tests/fixtures/stakeout_sample.geojson').size};
const preview = normalizeGeoJson(text, file);
"""

MIXED_BODY = """
const text = fs.readFileSync('tests/fixtures/stakeout_mixed.geojson', 'utf8');
const file = {name: 'stakeout_mixed.geojson', size: fs.statSync('tests/fixtures/stakeout_mixed.geojson').size};
const preview = normalizeGeoJson(text, file);
"""


def feature_collection(*features):
    return json.dumps({"type": "FeatureCollection", "features": list(features)})


def point_feature(coordinates, properties=None):
    return {"type": "Feature", "properties": properties or {}, "geometry": {"type": "Point", "coordinates": coordinates}}


class TestStakeoutImport(unittest.TestCase):

    def test_sample_package_is_the_golden_fixture(self):
        golden = fixture("stakeout_sample_package.json")
        out = run_import(SAMPLE_BODY + """
out.package = buildImportPackage(preview, stakeoutDefaultSelection(preview), 'PROJECT_ID', 2000);
""")
        self.assertEqual(out["package"], golden)
        golden_set = validate(golden)
        de = run_import(SAMPLE_BODY + """
out.package = buildImportPackage(preview, stakeoutDefaultSelection(preview), 'PROJECT_ID', 2000);
out.en = dict.en['stakeout.default_point']; out.de = dict.de['stakeout.default_point'];
""", "de")
        # E4: the names made from the default name are localized, everything else is the golden fixture.
        expected = json.loads(json.dumps(golden))
        english, german = de["en"].replace("{n}", "2"), de["de"].replace("{n}", "2")
        self.assertNotEqual(english, german)
        for item in expected["parts"] + expected["targets"]:
            if item["name"] == english:
                item["name"] = german
        self.assertEqual(de["package"], expected)
        self.assertEqual(validate(de["package"])["set_id"], golden_set["set_id"])
        self.assertEqual(golden_set["set_id"], "so-3a4739b9")

    def test_browser_packages_pass_the_device_validator(self):
        for body in (SAMPLE_BODY, MIXED_BODY):
            out = run_import(body + """
const every = new Set();
preview.parts.forEach((part, p) => stakeoutEverySelection(part.positions.length, 7, 0, part.positions.length - 1)
  .forEach(c => every.add(p + ':' + c)));
out.packages = [buildImportPackage(preview, stakeoutDefaultSelection(preview), 'PROJECT_ID', 2000),
                buildImportPackage(preview, every, 'PROJECT_ID', 2000),
                buildImportPackage(preview, stakeoutDefaultSelection(preview), 'PROJECT_ID', 250)];
""")
            for package in out["packages"]:
                validate(package)
        self.assertEqual(validate(out["packages"][0])["targets"][0]["name"], "Marker")

    def test_mixed_file_lists_every_skip_and_hint(self):
        out = run_import(MIXED_BODY + """
out.preview = preview;
out.hints = stakeoutImportHints(preview, stakeoutDefaultSelection(preview), null);
""")
        preview = out["preview"]
        self.assertIsNone(preview["blocked"])
        self.assertEqual(preview["source"]["features"], 18)
        self.assertEqual(preview["skipped"], [
            {"feature_index": 7, "type": None, "reason": "null_geometry"},
            {"feature_index": 8, "type": "GeometryCollection", "reason": "unsupported_geometry"},
            {"feature_index": 9, "type": "MultiPoint", "reason": "empty_coordinates"},
            {"feature_index": 10, "type": "LineString", "reason": "empty_coordinates"}])
        names = [part["name"] for part in preview["parts"]]
        self.assertEqual(names, ["Marker", "Marker", "Point 3", "Overlap A", "Overlap B", "Closed loop", "Zero segment",
                                 "Four values", "Props test", "Tab name", "Survey marker µ \U0001F4CD", "Long track",
                                 "N" * 76 + " (1)", "N" * 76 + " (2)", "P-17"])
        hints = [{k: v for k, v in hint.items() if k in ("code", "part", "feature_index", "key")}
                 for hint in preview["hints"]]
        self.assertEqual(hints, [
            {"code": "closed_line", "part": 5}, {"code": "zero_segments", "part": 6},
            {"code": "extra_position_elements", "feature_index": 11},
            {"code": "props_trimmed", "feature_index": 12, "key": "nested"},
            {"code": "props_trimmed", "feature_index": 12, "key": "long"},
            {"code": "long_line", "part": 11},
            {"code": "props_trimmed", "feature_index": 17, "key": " "},
            {"code": "props_trimmed", "feature_index": 17, "key": "a"},
            {"code": "props_trimmed", "feature_index": 17, "key": "k" * 45}])
        props = preview["parts"][8]["props"]
        self.assertEqual(props, {"long": "x" * 80, "note": "padded"})
        self.assertEqual(preview["parts"][7]["positions"], [[1000700000, 5100100000, 100500]])
        overlaps = [hint for hint in out["hints"] if hint["code"] == "overlap"]
        self.assertEqual(len(overlaps), 1)
        self.assertEqual({overlaps[0]["a"]["name"], overlaps[0]["b"]["name"]}, {"Overlap A", "Overlap B"})
        self.assertAlmostEqual(overlaps[0]["distance_m"], 0.01, delta=0.001)

    def test_text_limits_match_the_device(self):
        out = run_import(MIXED_BODY + """
out.joined = stakeoutJoinName('A'.repeat(80), ' / P001');
out.props = preview.parts[14].props;
out.surrogate = stakeoutText('\\ud800x', 80);
out.padded = stakeoutText('  padded  ', 80);
out.long = stakeoutText('B'.repeat(81), 80);
out.emoji = stakeoutText('\\u{1F4CD}'.repeat(81), 80);
out.package = buildImportPackage(preview, stakeoutDefaultSelection(preview), 'PROJECT_ID', 2000);
""")
        self.assertEqual(len(out["joined"]), 80)
        self.assertTrue(out["joined"].endswith(" / P001"))
        self.assertEqual(out["props"], {"a": "1", "k" * 40: "y"})
        self.assertEqual(out["surrogate"], "�x")
        self.assertEqual(out["padded"], "padded")
        self.assertEqual(out["long"], "B" * 80)
        self.assertEqual(out["emoji"], "\U0001F4CD" * 80)
        result = validate(out["package"])
        names = [target["name"] for target in result["targets"]]
        self.assertTrue(all(len(name) <= 80 for name in names))
        self.assertIn("N" * 73 + " / P001", names)
        self.assertIn("Tab name", names)

    def test_altitude_range_blocks_with_a_path(self):
        cases = {"high": feature_collection(point_feature([10.0, 51.0, 150000])),
                 "low": feature_collection(point_feature([10.0, 51.0, -20000])),
                 "top": feature_collection(point_feature([10.0, 51.0, 100000]))}
        out = run_import("""
const cases = %s; out.r = {};
for (const [k, text] of Object.entries(cases)) {
  const preview = normalizeGeoJson(text, {name: k + '.geojson', size: text.length});
  out.r[k] = {blocked: preview.blocked, package: preview.blocked ? null :
    buildImportPackage(preview, stakeoutDefaultSelection(preview), 'PROJECT_ID', 2000)};
}
""" % json.dumps(cases))
        self.assertEqual(out["r"]["high"]["blocked"],
                         {"code": "coordinate_out_of_range", "path": "features[0].geometry.coordinates[2]"})
        for key, alt_mm in (("low", -20000000), ("top", 100000000)):
            self.assertIsNone(out["r"][key]["blocked"])
            self.assertEqual(validate(out["r"][key]["package"])["targets"][0]["alt_import"], alt_mm)

    def test_hostile_inputs_block_or_skip(self):
        cases = {
            "broken": ('{"type":"FeatureCollection","features":[', None),
            "array": ("[]", None),
            "text_numbers": (feature_collection(point_feature(["10", "51"])), None),
            "overflow": ('{"type":"FeatureCollection","features":[{"type":"Feature","properties":{},'
                         '"geometry":{"type":"Point","coordinates":[1e400,51]}}]}', None),
            "projected": (feature_collection(point_feature([500000, 5600000])), None),
            "epsg3857": (json.dumps({"type": "FeatureCollection", "crs": {"type": "name", "properties": {
                "name": "urn:ogc:def:crs:EPSG::3857"}}, "features": [point_feature([10, 51])]}), None),
            "crs84": (json.dumps({"type": "FeatureCollection", "crs": {"type": "name", "properties": {
                "name": "urn:ogc:def:crs:OGC:1.3:CRS84"}}, "features": [point_feature([10, 51])]}), None),
            "huge": ("not even json", 4194305),
            "bom": ("﻿" + feature_collection(point_feature([10, 51])), None),
            "geometry": (json.dumps({"type": "LineString", "coordinates": [[10, 51], [10.001, 51]]}), None),
            "feature": (json.dumps(point_feature([10, 51])), None),
            "short_position": (feature_collection(point_feature([10])), None),
            "deep": ('{"type":"Point","coordinates":' + "[" * 5000 + "]" * 5000 + "}", None),
        }
        out = run_import("""
const cases = %s; out.r = {};
for (const [k, [text, size]] of Object.entries(cases)) {
  try { const p = normalizeGeoJson(text, {name: k, size: size == null ? text.length : size});
        out.r[k] = {blocked: p.blocked, parts: p.parts.length}; }
  catch (error) { out.r[k] = {thrown: String(error)}; }
}
const many = JSON.stringify({type: 'FeatureCollection', features: Array.from({length: 5001}, () =>
  ({type: 'Feature', properties: {}, geometry: {type: 'Point', coordinates: [10, 51]}}))});
out.r.many = {blocked: normalizeGeoJson(many, {name: 'many', size: many.length}).blocked};
""" % json.dumps(cases))
        r = out["r"]
        self.assertEqual(r["broken"]["blocked"]["code"], "invalid_json")
        self.assertEqual(r["array"]["blocked"]["code"], "not_geojson")
        self.assertEqual(r["text_numbers"]["blocked"], {"code": "invalid_position",
                                                        "path": "features[0].geometry.coordinates[0]"})
        self.assertEqual(r["overflow"]["blocked"], {"code": "invalid_position",
                                                    "path": "features[0].geometry.coordinates[0]"})
        self.assertEqual(r["projected"]["blocked"], {"code": "coordinate_out_of_range",
                                                     "path": "features[0].geometry.coordinates[0]"})
        self.assertEqual(r["epsg3857"]["blocked"]["code"], "crs_not_wgs84")
        self.assertIsNone(r["crs84"]["blocked"])
        self.assertEqual(r["huge"]["blocked"]["code"], "file_too_large")
        self.assertEqual(r["many"]["blocked"]["code"], "too_many_features")
        self.assertEqual((r["bom"]["blocked"], r["bom"]["parts"]), (None, 1))
        self.assertEqual((r["geometry"]["blocked"], r["geometry"]["parts"]), (None, 1))
        self.assertEqual((r["feature"]["blocked"], r["feature"]["parts"]), (None, 1))
        self.assertEqual(r["short_position"]["blocked"]["code"], "invalid_position")
        self.assertNotIn("thrown", r["deep"])
        self.assertIsNotNone(r["deep"]["blocked"])
        for key, value in r.items():
            self.assertNotIn("thrown", value, key)

    def test_preselection_and_limits(self):
        out = run_import("""
const points = n => Array.from({length: n}, (_, i) => ({type: 'Feature', properties: {},
  geometry: {type: 'Point', coordinates: [10 + i * 1e-5, 51]}}));
const line = n => ({type: 'Feature', properties: {}, geometry: {type: 'LineString',
  coordinates: Array.from({length: n}, (_, i) => [10.1 + i * 1e-5, 51])}});
const sizes = [[400, 50], [450, 100]];
out.counts = sizes.map(([p, l]) => { const text = JSON.stringify({type: 'FeatureCollection', features: [...points(p), line(l)]});
  const preview = normalizeGeoJson(text, {name: 'x', size: text.length});
  const selection = stakeoutDefaultSelection(preview);
  return [selection.size, [...selection].filter(key => key.startsWith(p + ':')).length]; });
out.every = stakeoutEverySelection(120, 7, 0, 119);
out.range = stakeoutEverySelection(120, 10, 5, 31);
""")
        self.assertEqual(out["counts"], [[450, 50], [450, 0]])
        self.assertEqual(out["every"], list(range(0, 113, 7)) + [119])
        self.assertEqual(out["range"], [5, 15, 25, 31])

    def test_thinning_stays_within_the_budget(self):
        out = run_import("""
out.a = thinBudget(Array(99).fill(2).concat([3000]), 2000);
out.b = thinBudget([3000], 2000);
out.c = thinBudget([10, 10], 100);
out.d = thinBudget(Array(300).fill(2).concat([500]), 250);
out.e = thinBudget([5, 7, 9, 11], 20);
out.idx = thinCoordIndices(3000, 2000);
out.small = thinCoordIndices(10, 4);
const features = Array.from({length: 99}, (_, i) => ({type: 'Feature', properties: {}, geometry: {type: 'LineString',
  coordinates: [[10 + i * 1e-4, 51], [10 + i * 1e-4, 51.00001]]}}));
features.push({type: 'Feature', properties: {name: 'Long'}, geometry: {type: 'LineString',
  coordinates: Array.from({length: 3000}, (_, i) => [10.2 + i * 1e-6, 51 + i * 1e-7])}});
const text = JSON.stringify({type: 'FeatureCollection', features});
const preview = normalizeGeoJson(text, {name: 'many-lines.geojson', size: text.length});
const selection = new Set(preview.parts.map((part, p) => p + ':0'));
out.package = buildImportPackage(preview, selection, 'PROJECT_ID', 2000);
out.last = preview.parts[99].positions[2999];
""")
        self.assertLessEqual(sum(out["a"]), 2000)
        self.assertEqual(out["a"][:99], [2] * 99)
        self.assertEqual(out["a"][99], 1802)
        self.assertEqual(out["b"], [2000])
        self.assertEqual(out["c"], [10, 10])
        self.assertEqual(sum(out["d"]), 602)
        self.assertEqual(set(out["d"]), {2})
        self.assertLessEqual(sum(out["e"]), 20)
        self.assertEqual(len(out["idx"]), 2000)
        self.assertEqual((out["idx"][0], out["idx"][-1]), (0, 2999))
        self.assertEqual(out["idx"], sorted(set(out["idx"])))
        self.assertEqual(out["small"], [0, 3, 6, 9])
        import stakeout
        result = validate(out["package"])
        self.assertLessEqual(sum(len(part["coords_e8"]) for part in result["parts"]), stakeout.MAX_COORDS_PER_SET)
        long_part = out["package"]["parts"][99]
        self.assertIs(long_part["coords_thinned"], True)
        self.assertEqual(long_part["coords_e8"][-1], out["last"])
        self.assertEqual(long_part["coords_e8"][0][:2], [1020000000, 5100000000])

    def test_size_fit(self):
        out = run_import("""
const text = JSON.stringify({type: 'Feature', properties: {name: 'Track'}, geometry: {type: 'LineString',
  coordinates: Array.from({length: 3000}, (_, i) => [10.2 + i * 1e-6, 51 + i * 1e-7, 100 + i / 1000])}});
const preview = normalizeGeoJson(text, {name: 'track.geojson', size: text.length});
const selection = new Set(['0:0', '0:2999']);
const full = new TextEncoder().encode(JSON.stringify(buildImportPackage(preview, selection, 'PROJECT_ID', 2000))).length;
out.fit = fitImportPackage(preview, selection, 'PROJECT_ID', full - 1);
out.fit = {budget: out.fit.budget, bytes: out.fit.bytes, ok: !!out.fit.package};
out.whole = fitImportPackage(preview, selection, 'PROJECT_ID', full).budget;
out.none = fitImportPackage(preview, selection, 'PROJECT_ID', 1000);
out.full = full;
""")
        self.assertEqual(out["whole"], 2000)
        self.assertEqual(out["fit"]["budget"], 1000)
        self.assertLess(out["fit"]["bytes"], out["full"])
        self.assertEqual(out["none"]["error"], "import_too_large")
        self.assertGreater(out["none"]["bytes"], 1000)

    def test_axis_swap_and_range_hints(self):
        out = run_import("""
const make = coords => { const text = JSON.stringify({type: 'FeatureCollection', features: coords.map(c =>
  ({type: 'Feature', properties: {}, geometry: {type: 'Point', coordinates: c}}))});
  const preview = normalizeGeoJson(text, {name: 'x', size: text.length});
  return stakeoutImportHints(preview, stakeoutDefaultSelection(preview), [5100000000, 1000000000]); };
out.swapped = make([[51.0001, 10.0001], [51.0002, 10.0002]]);
out.normal = make([[10.0, 51.0009], [10.0, 51.009]]);
""")
        codes = [hint["code"] for hint in out["swapped"]]
        self.assertIn("axis_swap", codes)
        self.assertNotIn("axis_swap", [hint["code"] for hint in out["normal"]])
        rover = next(hint for hint in out["normal"] if hint["code"] == "rover_range")
        self.assertAlmostEqual(rover["nearest_m"], geo.distance_e8_m(5100000000, 1000000000, 5100090000, 1000000000),
                               delta=0.01)
        self.assertAlmostEqual(rover["farthest_m"], geo.distance_e8_m(5100000000, 1000000000, 5100900000, 1000000000),
                               delta=0.01)
        extent = next(hint for hint in out["normal"] if hint["code"] == "extent")
        self.assertAlmostEqual(extent["north_m"], geo.distance_e8_m(5100090000, 1000000000, 5100900000, 1000000000),
                               delta=0.01)
        self.assertAlmostEqual(extent["east_m"], 0, delta=0.001)


class TestStakeoutImportEndToEnd(_StoreCase):

    def test_thinned_line_endpoint_is_selectable(self):
        out = run_import("""
const text = JSON.stringify({type: 'Feature', properties: {name: 'Recorded'}, geometry: {type: 'LineString',
  coordinates: Array.from({length: 3000}, (_, i) => [10.3 + i * 1e-6, 51 + i * 2e-7])}});
const preview = normalizeGeoJson(text, {name: 'recorded.geojson', size: text.length});
out.package = buildImportPackage(preview, new Set(['0:0', '0:2999']), %s, 2000);
out.last = preview.parts[0].positions[2999];
""" % json.dumps(PID))
        package = out["package"]
        self.assertIs(package["parts"][0]["coords_thinned"], True)
        self.assertEqual(len(package["parts"][0]["coords_e8"]), 2000)
        answer = self.add(package)
        set_id = answer["set_id"] if isinstance(answer, dict) and "set_id" in answer else validate(package)["set_id"]
        found = run(self.store.find_target(PID, FP, set_id + "/1/3000", None))
        self.assertEqual((found["lon_e8"], found["lat_e8"]), tuple(out["last"][:2]))
        route = run_core("out.kind = stakeoutSubview(['target', %s]).kind;" % json.dumps(set_id + "/1/3000"))
        self.assertEqual(route["kind"], "target")



def core_prefixes(*extra):
    return ("const tr=", "const fmt=", "const MEASURE_ROUTE=", "const ROUTES=", "function routeHash(",
            "function resolveRoute(") + extra + declared_prefixes("93-stakeout-guide.js", "95-stakeout.js",
                                                                  "97-stakeout-actions.js")


def run_core(body, stubs="", extra=(), asynchronous=False, language="en"):
    out = run_app_node(core_prefixes(*extra), "RtkI18n.setLanguage(%s);\n" % json.dumps(language) + body,
                       stubs=stubs, asynchronous=asynchronous)
    if out["failures"]:
        raise AssertionError(out["failures"])
    return out


def source_text(name):
    with open(os.path.join(ROOT, "src", name), encoding="utf-8") as handle:
        return handle.read()


def sample_set_doc():
    """The set document of the golden sample package (what a download parses into, without the trailer)."""
    result = validate(fixture("stakeout_sample_package.json"))
    return {"set_id": result["set_id"], "project_id": result["project_id"], "source": result["source"],
            "parts": result["parts"], "targets": result["targets"]}


LIVE_PACKET = fixture("stakeout_live_packet.json")

def source_block(name, prefix):
    """A top-level declaration of src/<name> that may span lines: up to the next top-level line."""
    lines = source_text(name).split("\n")
    start = next(i for i, line in enumerate(lines) if line.startswith(prefix))
    end = start + 1
    while end < len(lines) and (lines[end].startswith((" ", "\t")) or lines[end] == "}"):
        end += 1
        if lines[end - 1] == "}":
            break
    return "\n".join(lines[start:end])


# The shared live state of the header (src/25, src/30) with a stub DOM for the pill.
HEADER_PREFIXES = ("let csrfToken=", "let activeController=", "const LINK_SYMBOL=", "function livePollIntervalMs(",
                   "function pillLink(", "function setConnection(", "function pillGate(",
                   "let staleSinceSeq=", "function fixIsUsable(",
                   "function fixIsStale(", "function ingestFreshness(", "const LIVE_DELAYED_MS=",
                   "function noteLiveAnswer(", "function liveAgeMs(", "function liveLinkState(",
                   "function liveView(", "function displayAccuracy(", "const store=")
HEADER_STUBS = r"""
const __nodes = {};
const $ = id => __nodes[id] || (__nodes[id] = {id, className: "", dataset: {}, title: "", __parts: {},
  setAttribute() {}, querySelector(selector) { return this.__parts[selector] || (this.__parts[selector] = {textContent: "", dataset: {}}); }});
const capacityEstimate = {reset() {}};
const correctionsCause = () => null, fixLabel = fix => String(fix && fix.fix_status_text), gateRows = () => [];
const routeStartupLabel = () => "", localText = text => text, updateCapacityPanel = () => {}, formatClock = s => String(s);
const currentRoute = () => ({view: "project-stakeout", path: "projects/p1/stakeout"}), RTCM_EPOCH_WARN_SEC = 5;
const observeCapacity = () => {}, invalidateLive = () => { ++requestSerial; };
let holdActive = false; const restartHoldActive = () => holdActive;
"""


def run_header(body, asynchronous=False):
    return run_core("const packet = %s;\n" % json.dumps(LIVE_PACKET) + body,
                    stubs=HEADER_STUBS + "\n" + source_block("30-connection.js", "function updateGlobalStatus("),
                    extra=HEADER_PREFIXES, asynchronous=asynchronous)


class TestStakeoutCore(unittest.TestCase):

    def test_live_packet_fixture_has_the_device_shape(self):
        import test_stakeout_session as session_tests
        session_tests.assert_packet_keys(self, LIVE_PACKET, True, True)
        self.assertLessEqual(session_tests.device_bytes(LIVE_PACKET), 900)
        out = run_core("out.problem = stakeoutPacketProblem(%s);" % json.dumps(LIVE_PACKET))
        self.assertIsNone(out["problem"])

    def test_subview_parsing(self):
        cases = [[[], "overview", True], [["import"], "import", True], [["targets"], "targets", True],
                 [["target", "so-3a4739b9/1/3"], "target", True], [["target", "so-3a4739b9/1/3000"], "target", True],
                 [["target", "so-3a4739b9/500/1000000"], "target", True]]
        bad = [["target", "so-3a4739b9/1/1000001"], ["target", "so-3a4739b9/501/1"], ["target", "so-3a4739b9/0/1"],
               ["target", "so-3a4739b9/1/03"], ["target", "x"], ["nope"], ["target"], ["import", "x"]]
        out = run_core("""
out.views = %s.map(rest => stakeoutSubview(rest));
out.set = [stakeoutSetIdOf("so-3a4739b9/500/1000000"), stakeoutSetIdOf("so-3a4739b9/1/1000001"), stakeoutSetIdOf(null)];
out.paths = [stakeoutPath("a/b", "overview"), stakeoutPath("p1", "import"), stakeoutPath("p1", "targets"),
             stakeoutPath("p1", "target", "so-3a4739b9/1/3")];
""" % json.dumps([case[0] for case in cases] + bad))
        for (rest, kind, canonical), view in zip(cases, out["views"]):
            self.assertEqual((view["kind"], view["canonical"]), (kind, canonical), rest)
        self.assertEqual(out["views"][3]["tid"], "so-3a4739b9/1/3")
        for view in out["views"][len(cases):]:
            self.assertEqual((view["kind"], view["canonical"], view["tid"]), ("overview", False, None))
        self.assertEqual(out["set"], ["so-3a4739b9", None, None])
        self.assertEqual(out["paths"], ["projects/a%2Fb/stakeout", "projects/p1/stakeout/import",
                                        "projects/p1/stakeout/targets",
                                        "projects/p1/stakeout/target/so-3a4739b9%2F1%2F3"])

    def test_sequence_follows_orders(self):
        doc = sample_set_doc()
        out = run_core("""
const doc = %s;
out.plain = stakeoutSequence([doc], {}).map(t => t.tid);
out.ordered = stakeoutSequence([doc], {"so-3a4739b9": [1, 5]}).map(t => t.tid);
out.first = stakeoutSequence([doc], {})[4];
""" % json.dumps(doc))
        self.assertEqual(out["plain"], [target["tid"] for target in doc["targets"]])
        self.assertEqual(len(out["plain"]), 12)
        line_a = ["so-3a4739b9/1/%d" % n for n in (4, 3, 2, 1)]
        route_b2 = ["so-3a4739b9/5/%d" % n for n in (3, 2, 1)]
        self.assertEqual(out["ordered"], line_a + out["plain"][4:9] + route_b2)
        self.assertEqual(out["first"], {"tid": "so-3a4739b9/2/1", "set_id": "so-3a4739b9", "part_id": "so-3a4739b9/2",
                                        "name": "Point 2", "lat_e8": 5100000027, "lon_e8": 999999943})

    def test_next_target_skips_done_unknown_and_wraps(self):
        out = run_core("""
const doc = %s, seq = stakeoutSequence([doc], {});
const status = {"so-3a4739b9/1/2": "confirmed", "so-3a4739b9/1/3": "skipped", "so-3a4739b9/1/4": "unknown"};
const statusOf = tid => status[tid] || "open";
out.after_first = stakeoutNextTarget(seq, "so-3a4739b9/1/1", statusOf);
out.after_last = stakeoutNextTarget(seq, "so-3a4739b9/5/3", tid => tid === "so-3a4739b9/5/3" || tid === "so-3a4739b9/1/1" ? "confirmed" : statusOf(tid));
out.none = stakeoutNextTarget(seq, "so-3a4739b9/1/1", tid => tid.endsWith("/1") ? "unknown" : "confirmed");
out.start = stakeoutNextTarget(seq, null, statusOf);
""" % json.dumps(sample_set_doc()))
        self.assertEqual(out["after_first"], "so-3a4739b9/2/1")
        self.assertEqual(out["after_last"], "so-3a4739b9/2/1")
        self.assertIsNone(out["none"])
        self.assertEqual(out["start"], "so-3a4739b9/1/1")

    def test_progress_from_overview_without_and_with_wp4(self):
        out = run_core("""
const set = {set_id: "so-3a4739b9", state: "ok", targets: 12};
const plain = {sets: [set]}, wp4 = {progress: {active_tid: "so-3a4739b9/2/1", damaged_lines: 0, write_uncertain: false},
  sets: [{...set, progress: {confirmed: 2, verified: 1, manual: 1, skipped: 1}}]};
const read = o => { const p = stakeoutProgressFromOverview(o), q = stakeoutProgress(o);
  return {status: p.status("so-3a4739b9/1/2"), mode: p.mode("so-3a4739b9/1/2"), gen: p.gen("so-3a4739b9/1/2"),
          counts: p.counts("so-3a4739b9"), confirmed: stakeoutConfirmedCount(p, "so-3a4739b9"), active: stakeoutActiveTid(o),
          same: JSON.stringify([q.status("so-3a4739b9/1/2"), q.mode("so-3a4739b9/1/2"), q.gen("so-3a4739b9/1/2"), q.counts("so-3a4739b9")]) ===
                JSON.stringify([p.status("so-3a4739b9/1/2"), p.mode("so-3a4739b9/1/2"), p.gen("so-3a4739b9/1/2"), p.counts("so-3a4739b9")])}; };
out.plain = read(plain); out.wp4 = read(wp4);
out.missing = read({progress: {active_tid: null}, sets: [set]});
out.text = read({progress: {active_tid: null}, sets: [{...set, progress: {confirmed: "2", verified: 1, manual: 1, skipped: 1}}]});
out.list = read({...wp4, stakeout_progress: {active_tid: "so-3a4739b9/1/1", states: []}});
out.bad = read({...wp4, stakeout_progress: {active_tid: "x"}});
out.session = stakeoutActiveTid({sets: [set], session: {target: {tid: "so-3a4739b9/1/4"}}});
const progress = stakeoutProgressFromOverview(plain);
out.of = [stakeoutStatusOf(progress, {target: {tid: "so-3a4739b9/1/2", status: "skipped"}}, "so-3a4739b9/1/2"),
          stakeoutStatusOf(progress, {target: {tid: "so-3a4739b9/1/2", status: "unknown"}}, "so-3a4739b9/1/2"),
          stakeoutStatusOf(progress, {target: {tid: "so-3a4739b9/1/2", status: "weird"}}, "so-3a4739b9/1/2"),
          stakeoutStatusOf(progress, {target: {tid: "so-3a4739b9/1/3", status: "skipped"}}, "so-3a4739b9/1/2"),
          stakeoutStatusOf(progress, null, "so-3a4739b9/1/2")];
""")
        zero = {"confirmed": 0, "skipped": 0, "verified": 0, "manual": 0}
        self.assertEqual(out["plain"], {"status": "open", "mode": None, "gen": 0, "counts": zero, "confirmed": 0,
                                        "active": None, "same": True})
        self.assertEqual(out["wp4"], {"status": "unknown", "mode": None, "gen": None,
                                      "counts": {"confirmed": 2, "skipped": 1, "verified": 1, "manual": 1},
                                      "confirmed": 2, "active": "so-3a4739b9/2/1", "same": True})
        for key in ("missing", "text"):
            self.assertEqual((out[key]["status"], out[key]["counts"], out[key]["confirmed"]), ("unknown", None, None))
        self.assertEqual((out["list"]["status"], out["list"]["active"]), ("unknown", "so-3a4739b9/1/1"))
        self.assertEqual(out["bad"]["active"], "so-3a4739b9/2/1")
        self.assertEqual(out["session"], "so-3a4739b9/1/4")
        self.assertEqual(out["of"], ["skipped", "unknown", "unknown", "open", "open"])

    def test_frame_latch_and_restart(self):
        out = run_core("""
const make = (seq, stale) => ({...%s, fix_seq: seq, stale});
let frame = null; out.steps = [];
const step = (seq, stale, at) => { frame = stakeoutFrame(frame, make(seq, stale), at);
  out.steps.push([frame.usable, frame.staleSinceSeq, frame.restarted]); };
step(10, false, 0); step(11, true, 1000); step(11, false, 2000); step(12, false, 3000);
step(12, false, 7001); step(13, false, 8000); step(3, false, 9000);
""" % json.dumps(LIVE_PACKET))
        self.assertEqual(out["steps"], [[True, None, False], [False, 11, False], [False, 11, False], [True, 11, False],
                                        [False, 12, False], [True, 12, False], [True, None, True]])

    def test_frame_handles_partial_packets(self):
        cases = {"null": None, "empty": {}, "schema": dict(LIVE_PACKET, schema=2),
                 "seq_text": dict(LIVE_PACKET, fix_seq="12"), "seq_negative": dict(LIVE_PACKET, fix_seq=-1),
                 "seq_fraction": dict(LIVE_PACKET, fix_seq=1.5),
                 "no_gate": {k: v for k, v in LIVE_PACKET.items() if k != "gate"},
                 "no_guidance": {k: v for k, v in LIVE_PACKET.items() if k != "guidance"},
                 "position": dict(LIVE_PACKET, position={"lat_e8": "x", "lon_e8": 1}),
                 "array": [], "stale": dict(LIVE_PACKET, stale="no"), "target": dict(LIVE_PACKET, target={"tid": 5})}
        valid = {"no_position": dict(LIVE_PACKET, fix_seq=12, position=None, guidance=dict(
            LIVE_PACKET["guidance"], state="no_position", dn_m=None, de_m=None, dist_m=None, bearing_deg=None)),
            "far": dict(LIVE_PACKET, fix_seq=12, guidance=dict(LIVE_PACKET["guidance"], state="far", dn_m=None,
                                                                de_m=None, dist_m=None, bearing_deg=None))}
        out = run_header("""
const cases = %s, valid = %s; out.r = {};
for (const [k, p] of Object.entries(cases)) {
  const ready = ingestStakeoutPacket(null, packet, 500);
  try { const f = ingestStakeoutPacket(ready, p, 900); out.r[k] = {usable: f.usable, rejected: f.rejected || null, receivedAt: f.receivedAt, packetSeq: f.packet && f.packet.fix_seq}; }
  catch (error) { out.r[k] = {thrown: String(error)}; }
}
for (const [k, p] of Object.entries(valid)) {
  const ready = ingestStakeoutPacket(null, packet, 500), f = ingestStakeoutPacket(ready, p, 900);
  out.r[k] = {usable: f.usable, rejected: f.rejected || null, receivedAt: f.receivedAt};
}
""" % (json.dumps(cases), json.dumps(valid)))
        codes = {"null": "not_object", "empty": "schema", "schema": "schema", "seq_text": "sequence",
                 "seq_negative": "sequence", "seq_fraction": "sequence", "no_gate": "gate", "no_guidance": "guidance",
                 "position": "position", "array": "not_object", "stale": "stale", "target": "target"}
        for key, code in codes.items():
            self.assertEqual(out["r"][key], {"usable": False, "rejected": code, "receivedAt": 500, "packetSeq": 10}, key)
        self.assertEqual(out["r"]["no_position"], {"usable": False, "rejected": None, "receivedAt": 900})
        self.assertEqual(out["r"]["far"], {"usable": True, "rejected": None, "receivedAt": 900})

    def test_packet_problems_never_reach_the_shared_state(self):
        out = run_header("""
store.state.live = {system: {uptime_sec: 50}};
const ready = ingestStakeoutPacket(null, packet, performance.now());
const live = store.state.live, serial = requestSerial, age = liveAgeMs(performance.now());
const until = Date.now() + 30; while (Date.now() < until) {}
const frame = ingestStakeoutPacket(ready, {...packet, schema: 2, fix_seq: 11}, performance.now());
out.same = store.state.live === live; out.serial = requestSerial - serial;
out.grew = liveAgeMs(performance.now()) > age + 20; out.usable = frame.usable; out.rejected = frame.rejected;
out.count = frame.rejectedCount;
""")
        self.assertEqual((out["same"], out["serial"], out["grew"], out["usable"], out["rejected"], out["count"]),
                         (True, 0, True, False, "schema", 1))

    def test_accept_restores_the_connection_state(self):
        out = run_header("""
setConnection("Device unreachable", "error");
out.before = connectionState.state;
const t0 = performance.now();
acceptStakeoutPacket({...packet, fix_seq: 20}, t0, false);
acceptStakeoutPacket({...packet, fix_seq: 21}, t0 + 1000, false);
out.after = connectionState.state; out.link = $("live-state").dataset.link;
""")
        self.assertEqual((out["before"], out["after"], out["link"]), ("error", "ok", "live"))

    def test_accept_resets_the_latch_after_a_device_restart(self):
        out = run_header("""
store.state.live = {system: {uptime_sec: 4000, boot_id: "b1"}};
let frame = ingestStakeoutPacket(null, {...packet, fix_seq: 199}, 0);
frame = ingestStakeoutPacket(frame, {...packet, fix_seq: 200, stale: true}, 1000);
out.stale_usable = fixIsUsable(store.state.live.current_fix); out.latch = staleSinceSeq;
frame = ingestStakeoutPacket(frame, {...packet, fix_seq: 3}, 2000);
out.usable = frame.usable; out.restarted = frame.restarted; out.shared = fixIsUsable(store.state.live.current_fix);
out.uptime = "uptime_sec" in store.state.live.system; out.boot = store.state.live.system.boot_id;
""")
        self.assertEqual((out["stale_usable"], out["latch"]), (False, 200))
        self.assertEqual((out["usable"], out["restarted"], out["shared"], out["uptime"], out["boot"]),
                         (True, True, True, False, "b1"))

    def test_live_delta_feeds_the_header(self):
        out = run_header("""
store.state.live = {current_quality: {accepted: false, reasons: [], limits: {fix_quality: "RTK_FIXED"}}, rtcm: {mount: "M"}};
const serial = requestSerial, t0 = performance.now();
acceptStakeoutPacket({...packet, fix_seq: 30, gate: {accepted: false, reasons: ["hdop_too_high", "corrections_missing", "fix_stale"]}}, t0, false);
acceptStakeoutPacket({...packet, fix_seq: 31}, t0 + 1000, false);
const live = store.state.live;
out.link = liveView(2000).link; out.sigma = live.current_fix.receiver_accuracy.horizontal_sigma_m;
out.limits = live.current_quality.limits; out.serial = requestSerial - serial; out.mount = live.rtcm.mount;
out.age = live.rtcm.epoch_age_sec; out.ntrip = live.network.ntrip_state; out.fix = live.current_fix;
const first = stakeoutLiveDelta({current_quality: {limits: {a: 1}}}, {...packet, gate: {accepted: false, reasons: ["hdop_too_high", "corrections_missing", "fix_stale"]}}, false);
out.reasons = first.current_quality.reasons;
out.nofix = stakeoutLiveDelta(null, {...packet, position: null}, false).current_fix;
""")
        self.assertEqual(out["link"], "live")
        self.assertEqual(out["sigma"], 0.011)
        self.assertEqual(out["limits"], {"fix_quality": "RTK_FIXED"})
        self.assertEqual(out["serial"], 2)
        self.assertEqual((out["mount"], out["age"], out["ntrip"]), ("M", 0.8, "streaming"))
        self.assertEqual(out["reasons"], ["hdop_too_high"])
        fix = out["fix"]
        self.assertEqual((fix["lat_e8"], fix["lon_e8"], fix["qual"], fix["fix_status_text"], fix["fix_seq"], fix["stale"]),
                         (5100000000, 1000000000, 4, "RTK_FIXED", 31, False))
        self.assertEqual(out["nofix"], {"qual": 0, "fix_status_text": "NO_FIX", "fix_seq": 10, "fix_rev": 0,
                                        "fix_age_ms": 320, "stale": False})

    def test_calm_answer_restores_the_connection(self):
        out = run_header("""
ingestStakeoutPacket(null, packet, performance.now());
const live = store.state.live, serial = requestSerial, age = liveAgeMs(performance.now());
setConnection("Device unreachable", "error");
const until = Date.now() + 30; while (Date.now() < until) {}
out.code = acceptStakeoutCalm("stakeout_busy");
out.state = connectionState.state; out.same = store.state.live === live; out.serial = requestSerial - serial;
out.grew = liveAgeMs(performance.now()) > age + 20; out.link = $("live-state").dataset.link;
out.codes = [{status: 503, code: "stakeout_busy"}, {status: 503, code: "stakeout_write_uncertain"}, null, {status: 503},
             {status: 500, code: "stakeout_busy"}, {status: 409, code: "stakeout_busy"}].map(stakeoutCalmCode);
RtkI18n.setLanguage("de"); out.de = stakeoutErrorText({code: "stakeout_write_uncertain", message: "x"});
RtkI18n.setLanguage("en"); out.en = stakeoutErrorText({code: "stakeout_write_uncertain", message: "x"});
out.dict = [dict.de["stakeout.error.stakeout_write_uncertain"], dict.en["stakeout.error.stakeout_write_uncertain"]];
out.packet_calm = [stakeoutPacketCalm({...packet, guidance: {...packet.guidance, reasons: ["write_uncertain"]}}), stakeoutPacketCalm(packet)];
""")
        self.assertEqual((out["code"], out["state"], out["same"], out["serial"], out["grew"]),
                         ("stakeout_busy", "ok", True, 0, True))
        self.assertNotEqual(out["link"], "error")
        self.assertEqual(out["codes"], ["stakeout_busy", "stakeout_write_uncertain", None, None, None, None])
        self.assertEqual([out["de"], out["en"]], out["dict"])
        self.assertNotEqual(out["de"], out["en"])
        self.assertEqual(out["packet_calm"], ["stakeout_write_uncertain", None])

    def test_error_texts_cover_the_device_codes(self):
        codes = ["project_not_active", "recording_active", "project_changed", "target_not_found", "set_not_found",
                 "set_damaged", "storage_busy", "stakeout_busy", "stakeout_write_uncertain", "survey_loading",
                 "stakeout_recovery_pending", "confirmed_changed", "too_many_sets", "import_too_large",
                 "orphaned_set_conflict", "import_committed_response_failed", "stakeout_write_failed",
                 "invalid_stakeout_settings"]
        out = run_core("""
const codes = %s; out.missing = [];
for (const lang of ["en", "de"]) for (const code of codes) if (!dict[lang]["stakeout.error." + code]) out.missing.push(lang + ":" + code);
out.texts = codes.map(code => stakeoutErrorText({code, message: "raw"}));
out.importText = stakeoutErrorText({code: "coordinate_out_of_range", message: "raw", path: "targets[3].lat_e8"});
out.importKey = dict.en["import.coordinate_out_of_range"];
out.fallback = stakeoutErrorText({code: "something_new", message: "Raw device text"});
out.noPath = stakeoutErrorText({code: "storage_busy", message: "raw", path: 7});
""" % json.dumps(codes))
        self.assertEqual(out["missing"], [])
        self.assertNotIn("raw", out["texts"])
        self.assertEqual(out["importText"], out["importKey"] + " (targets[3].lat_e8)")
        self.assertEqual(out["fallback"], "Raw device text")
        self.assertNotIn("(", out["noPath"])

    def test_static_interface_for_wp4(self):
        sources = {name: source_text(name) for name in ("93-stakeout-guide.js", "94-stakeout-import.js",
                                                        "95-stakeout.js")}
        names = set()
        for text in sources.values():
            for line in text.split("\n"):
                match = re.match(r"^(?:async function |function |const |let )([A-Za-z0-9_$]+)", line)
                if match:
                    names.add(match.group(1))
        reserved = {"createFixSource", "loadStakeoutProgress", "stakeoutProgressView", "stakeoutActionButtons",
                    "stakeoutCommandId", "stakeoutCommand", "stakeoutExportEntry", "stakeoutArchiveMark",
                    "stakeoutActionBar", "STAKEOUT_COMMAND_ATTEMPTS", "STAKEOUT_RETRY_PAUSE_MS"}
        self.assertEqual(names & reserved, set())
        core = sources["95-stakeout.js"].split("\n")
        # WP4 part B replaced the two rump bodies (plan change B-2); signatures and callers stay.
        for rump in ("function stakeoutProgress(overview){return stakeoutProgressView(overview);}",
                     "function stakeoutConfirmActions(view){return stakeoutActionButtons(view);}"):
            self.assertEqual(core.count(rump), 1, rump)
        poll = next(line for line in core if line.startswith("function createStakeoutPoll("))
        self.assertNotIn("acceptStakeoutPacket", poll)
        assignments = []
        for text in sources.values():
            code = "\n".join(line for line in text.split("\n") if not line.startswith("//"))
            assignments += re.findall(r"stakeout_progress\s*=[^=]", code)
        self.assertEqual(len(assignments), 1, assignments)
        self.assertEqual(sources["95-stakeout.js"].count(
            "overview.stakeout_progress=await loadStakeoutProgress(projectId,signal);return overview;}"), 1)
        # WP4 part B lives in slot 97 (ruling 30.10., plan change B-1); 96 stays free.
        self.assertEqual([name for name in os.listdir(os.path.join(ROOT, "src")) if name.startswith(("96-", "97-"))],
                         ["97-stakeout-actions.js"])


# A deterministic clock and timer queue for the poll and the ticker; fetch is a steerable promise.
POLL_PRELUDE = r"""
globalThis.document = {hidden: false};
let clock = 0, nextId = 1; const timers = [];
const setTimer = (fn, ms) => { const t = {id: nextId++, fn, at: clock + ms}; timers.push(t); return t.id; };
const clearTimer = id => { const i = timers.findIndex(t => t.id === id); if (i >= 0) timers.splice(i, 1); };
const flush = async () => { for (let i = 0; i < 5; i++) await new Promise(r => setImmediate(r)); };
const advance = async ms => { const end = clock + ms; for (;;) { timers.sort((a, b) => a.at - b.at || a.id - b.id);
  const t = timers[0]; if (!t || t.at > end) break; timers.shift(); clock = t.at; t.fn(); await flush(); } clock = end; await flush(); };
let open = 0, maxOpen = 0; const calls = [], accepted = [], failed = [], calmed = [];
const respond = (delay, value, error) => signal => { calls.push(clock); open++; maxOpen = Math.max(maxOpen, open);
  return new Promise((resolve, reject) => setTimer(() => { open--; if (error) reject(error); else resolve(value); }, delay)); };
const controller = new AbortController();
const makePoll = fetch => createStakeoutPoll({signal: controller.signal, fetch, now: () => clock, setTimer, clearTimer,
  accept: (p, at) => accepted.push(at), calm: (code, at) => calmed.push([code, at]), fail: error => failed.push(error.name || error.code)});
const error = (name, status, code) => Object.assign(new Error(name), {name, status, code});
"""


def run_poll(body):
    return run_header(POLL_PRELUDE + body, asynchronous=True)


class TestStakeoutPoll(unittest.TestCase):

    def test_poll_waits_for_completion(self):
        out = run_poll("""
const poll = makePoll(respond(2500, packet)); poll.start(); await advance(8000);
out.calls = calls; out.maxOpen = maxOpen; out.accepted = accepted; out.running = poll.running();
""")
        self.assertEqual(out["calls"], [0, 3500, 7000])
        self.assertEqual(out["maxOpen"], 1)
        self.assertEqual(out["accepted"], [2500, 6000])
        self.assertIs(out["running"], True)

    def test_poll_timeout_continues(self):
        out = run_poll("""
let first = true;
const poll = makePoll(signal => { if (first) { first = false; return respond(3000, null, error("TimeoutError"))(signal); } return respond(10, packet)(signal); });
poll.start(); await advance(4500);
out.calls = calls; out.failed = failed; out.accepted = accepted;
""")
        self.assertEqual(out["calls"], [0, 4000])
        self.assertEqual(out["failed"], ["TimeoutError"])
        self.assertEqual(out["accepted"], [4010])

    def test_poll_rests_when_hidden_or_held(self):
        out = run_poll("""
document.hidden = true;
const poll = makePoll(respond(10, packet)); poll.start(); await advance(3000);
out.hidden = calls.length; document.hidden = false; await advance(1000); out.visible = calls.length;
holdActive = true; await advance(3500); out.held = calls.length; holdActive = false; await advance(1200); out.after = calls.length;
""")
        self.assertEqual((out["hidden"], out["visible"], out["held"]), (0, 1, 1))
        self.assertGreater(out["after"], out["held"])

    def test_poll_stops_on_abort(self):
        out = run_poll("""
const poll = makePoll(respond(2000, packet)); poll.start(); await advance(500);
controller.abort(); await advance(5000);
out.calls = calls.length; out.accepted = accepted.length; out.failed = failed.length; out.timers = timers.length;
out.running = poll.running();
""")
        self.assertEqual((out["calls"], out["accepted"], out["failed"], out["timers"], out["running"]), (1, 0, 0, 0, False))

    def test_poll_stop_drops_the_open_answer(self):
        out = run_poll("""
const poll = makePoll(respond(2000, packet)); poll.start(); await advance(500);
poll.stop(); await advance(5000); out.after_stop = [calls.length, accepted.length, timers.length];
poll.start(); await advance(100); out.restart = calls.length;
""")
        self.assertEqual(out["after_stop"], [1, 0, 0])
        self.assertEqual(out["restart"], 2)

    def test_poll_reports_receive_time(self):
        out = run_poll("""
const poll = makePoll(respond(730, packet)); poll.start(); await advance(2500); out.accepted = accepted;
""")
        self.assertEqual(out["accepted"], [730, 2460])

    def test_poll_answers_busy_calmly(self):
        out = run_poll("""
let n = 0;
const poll = makePoll(signal => { n++; if (n <= 3) return respond(0, null, error("Error", 503, "stakeout_write_uncertain"))(signal);
  if (n === 4) return respond(0, packet)(signal); if (n === 5) return respond(0, null, error("Error", 503, "stakeout_busy"))(signal);
  if (n === 6) return respond(0, null, error("Error", 503, "survey_loading"))(signal);
  if (n === 7) return respond(0, null, error("Error", 409, "stakeout_busy"))(signal); return respond(500, null, error("Error", 503, "stakeout_busy"))(signal); });
poll.start(); await advance(6200);
out.calls = calls; out.calmed = [...calmed]; out.failed = failed.length; out.accepted = accepted; out.maxOpen = maxOpen;
await advance(1000); out.open = open; controller.abort(); await advance(2000); out.after_abort = [calmed.length, failed.length];
""")
        self.assertEqual(out["calls"][:7], [0, 1000, 2000, 3000, 4000, 5000, 6000])
        self.assertEqual(out["calmed"][:4], [["stakeout_write_uncertain", 0], ["stakeout_write_uncertain", 1000],
                                             ["stakeout_write_uncertain", 2000], ["stakeout_busy", 4000]])
        self.assertEqual(out["accepted"], [3000])
        self.assertEqual(out["failed"], 2)
        self.assertEqual(out["maxOpen"], 1)
        self.assertEqual(out["open"], 1)
        self.assertEqual(out["after_abort"], [len(out["calmed"]), 2])

    def test_ticker_marks_the_frame_stale_without_packets(self):
        out = run_poll("""
const frame = stakeoutFrame(null, packet, 0); const ticks = []; let fetched = 0;
const ticker = createStakeoutTicker(controller.signal, now => ticks.push([now, stakeoutFrameStale(frame, now)]),
  {setInterval: (fn, ms) => { const id = nextId++; const again = () => { const t = {id, fn: () => { fn(); again(); }, at: clock + ms}; timers.push(t); }; again(); return id; },
   clearInterval: id => { for (let i = timers.length - 1; i >= 0; i--) if (timers[i].id === id) timers.splice(i, 1); }, now: () => clock});
await advance(4250);
out.at4000 = ticks.find(t => t[0] === 4000); out.at4250 = ticks.find(t => t[0] === 4250); out.count = ticks.length;
document.hidden = true; await advance(1000); out.hiddenCount = ticks.length; document.hidden = false;
controller.abort(); await advance(1000); out.abortCount = ticks.length; out.timers = timers.length; out.calls = calls.length;
""")
        self.assertEqual(out["at4000"], [4000, False])
        self.assertEqual(out["at4250"], [4250, True])
        self.assertEqual(out["count"], 17)
        self.assertEqual(out["hiddenCount"], 17)
        self.assertEqual(out["abortCount"], 17)
        self.assertEqual(out["timers"], 0)
        self.assertEqual(out["calls"], 0)


def api_block():
    """The real request wrapper of src/15-store-api.js: requestAbortError and the multi-line const api={...};."""
    lines = source_text("15-store-api.js").split("\n")
    start = next(i for i, line in enumerate(lines) if line.startswith("const api={"))
    end = next(i for i in range(start, len(lines)) if lines[i] == "};")
    abort = next(line for line in lines if line.startswith("function requestAbortError("))
    return abort + "\n" + "\n".join(lines[start:end + 1])


class TestStakeoutApiError(unittest.TestCase):

    def test_api_error_keeps_the_device_path(self):
        out = run_core("""
let answer = null; globalThis.fetch = async () => answer;
const respond = body => { answer = new Response(JSON.stringify(body), {status: 400, headers: {"content-type": "application/json"}}); };
respond({error: "coordinate_out_of_range", message: "lat out of range", path: "targets[3].lat_e8"});
try { await api.json("/api/stakeout/import"); } catch (error) { out.first = {path: error.path, code: error.code, status: error.status, text: stakeoutErrorText(error)}; }
respond({error: "invalid_stakeout_import", message: "bad", path: 7});
try { await api.json("/api/stakeout/import"); } catch (error) { out.second = {path: error.path === undefined ? "undefined" : error.path}; }
out.key = dict.en["import.coordinate_out_of_range"];
""", stubs="const restartHoldActive = () => false, signInRedirect = () => {}; let csrfToken = null;\n" + api_block(),
                       asynchronous=True)
        self.assertEqual(out["first"]["path"], "targets[3].lat_e8")
        self.assertEqual((out["first"]["code"], out["first"]["status"]), ("coordinate_out_of_range", 400))
        self.assertIn(out["key"], out["first"]["text"])
        self.assertIn("targets[3].lat_e8", out["first"]["text"])
        self.assertEqual(out["second"]["path"], "undefined")



# A small DOM for the view builders under Node (h(), replace() and the SVG helpers need only this much).
MINI_DOM = r"""
class Node {}
class Text extends Node { constructor(value) { super(); this.data = String(value); this.parentNode = null; } get textContent() { return this.data; } }
const camel = name => name.replace(/-([a-z])/g, (_, c) => c.toUpperCase());
class El extends Node {
  constructor(tag, ns) { super(); Object.assign(this, {tagName: tag.toUpperCase(), localName: tag, namespaceURI: ns || null, children: [], attrs: {},
    dataset: {}, listeners: {}, parentNode: null, style: {}, hidden: false, disabled: false, className: "", id: ""});
    this.classList = {add: (...n) => { const set = new Set(this.className.split(/\s+/).filter(Boolean)); n.forEach(x => set.add(x)); this.className = [...set].join(" "); },
      remove: (...n) => { this.className = this.className.split(/\s+/).filter(x => x && !n.includes(x)).join(" "); },
      toggle: (x, on) => { (on ?? !this.classList.contains(x)) ? this.classList.add(x) : this.classList.remove(x); },
      contains: x => this.className.split(/\s+/).includes(x)}; }
  append(...items) { for (const item of items.flat()) { const node = item instanceof Node ? item : new Text(item); if (node.parentNode) node.remove(); node.parentNode = this; this.children.push(node); } }
  prepend(...items) { const rest = this.children.splice(0); this.append(...items); for (const node of rest) this.children.push(node); }
  remove() { if (this.parentNode) { const list = this.parentNode.children; list.splice(list.indexOf(this), 1); this.parentNode = null; } }
  replaceChildren(...items) { for (const node of this.children) node.parentNode = null; this.children = []; this.append(...items); }
  replaceWith(node) { const list = this.parentNode.children, at = list.indexOf(this); list[at] = node; node.parentNode = this.parentNode; this.parentNode = null; }
  get textContent() { return this.children.map(node => node.textContent).join(""); }
  set textContent(value) { this.replaceChildren(new Text(value == null ? "" : value)); }
  setAttribute(key, value) { if (key.startsWith("data-")) this.dataset[camel(key.slice(5))] = String(value); else if (key === "class") this.className = String(value); else if (key === "id") this.id = String(value); else this.attrs[key] = String(value); }
  getAttribute(key) { return key.startsWith("data-") ? (this.dataset[camel(key.slice(5))] ?? null) : key === "class" ? this.className : (this.attrs[key] ?? null); }
  removeAttribute(key) { if (key.startsWith("data-")) delete this.dataset[camel(key.slice(5))]; else delete this.attrs[key]; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  removeEventListener() {}
  all() { const out = []; const walk = node => { for (const child of node.children) if (child instanceof El) { out.push(child); walk(child); } }; walk(this); return out; }
  find(test) { return this.all().filter(test); }
  matches(selector) { return selector.split(",").some(part => matchSelector(this, part.trim())); }
  querySelectorAll(selector) { return this.all().filter(el => el.matches(selector)); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  closest(selector) { for (let node = this; node instanceof El; node = node.parentNode) if (node.matches(selector)) return node; return null; }
  get isConnected() { return true; }
}
// Compound selectors (tag, .class, [attr], [attr="value"]) joined by descendant spaces.
const matchCompound = (el, compound) => { const parts = compound.match(/^([a-z0-9-]*)((?:[.][\w-]+|\[[^\]]+\])*)$/i); if (!parts) throw new Error("selector " + compound);
  if (parts[1] && el.localName !== parts[1]) return false;
  for (const token of parts[2].match(/[.][\w-]+|\[[^\]]+\]/g) || []) { if (token[0] === ".") { if (!el.classList.contains(token.slice(1))) return false; continue; }
    const [, name, value] = token.match(/^\[([\w-]+)(?:="([^"]*)")?\]$/), actual = el.getAttribute(name); if (actual === null || actual === undefined || (value !== undefined && String(actual) !== value)) return false; }
  return true; };
const matchSelector = (el, selector) => { const steps = selector.split(/\s+/); if (!matchCompound(el, steps.pop())) return false;
  let node = el.parentNode; while (steps.length && node instanceof El) { if (matchCompound(node, steps[steps.length - 1])) steps.pop(); node = node.parentNode; } return !steps.length; };
globalThis.document = {hidden: false, documentElement: {}, querySelectorAll: () => [], dispatchEvent() {},
  createElement: tag => new El(tag), createElementNS: (ns, tag) => new El(tag, ns), createTextNode: value => new Text(value)};
"""
DOM_PREFIXES = ("const V2_DE=", "function localText(", "const text=", "const raw=", "function h(", "const replace=")


class TestStakeoutOverview(unittest.TestCase):

    def test_project_entry_links_to_the_overview(self):
        out = run_core("""
for (const lang of ["en", "de"]) { RtkI18n.setLanguage(lang);
  const entry = stakeoutProjectEntry({id: "a/b", name: "Name"}), link = entry.find(el => el.tagName === "A")[0];
  out[lang] = {cls: entry.className, href: link.getAttribute("href"), link: link.className, label: link.textContent,
    text: entry.find(el => el.className === "muted")[0].textContent, labelKey: dict[lang]["stakeout.entry"], textKey: dict[lang]["stakeout.entry_text"]}; }
""", stubs=MINI_DOM, extra=DOM_PREFIXES)
        for lang in ("en", "de"):
            entry = out[lang]
            self.assertEqual(entry["cls"], "stakeout-entry")
            self.assertEqual(entry["href"], "/#projects/a%2Fb/stakeout")
            self.assertEqual(entry["link"], "btn secondary")
            self.assertEqual((entry["label"], entry["text"]), (entry["labelKey"], entry["textKey"]))
        self.assertNotEqual(out["en"]["text"], out["de"]["text"])

    def test_settings_form_checks_the_pair(self):
        out = run_core("""
out.r = [[0.01, 5], [0.1, 5.5], [5, 5], [0.1, 0.5], [0.1, 5], [0.1, 51], ["x", 5], [0.02, 1], [5, 50]]
  .map(([t, f]) => stakeoutSettingsError({tolerance_m: t, fine_radius_m: f}));
out.keys = ["stakeout.settings_tolerance_range", "stakeout.settings_fine_range", "stakeout.settings_pair"].map(key => dict.en[key]);
""")
        tolerance, fine, pair = out["keys"]
        self.assertEqual(out["r"], [tolerance, None, pair, fine, None, fine, tolerance, None, None])

    def test_set_counts_from_injected_progress(self):
        out = run_core("""
const seq = stakeoutSequence([%s], {}), ids = ["so-3a4739b9"];
const model = (status, counts) => ({status: tid => status[tid] || "open", mode: () => null, gen: () => 0, counts: () => counts});
out.a = stakeoutSetCounts(seq, model({"so-3a4739b9/1/2": "confirmed", "so-3a4739b9/1/3": "confirmed", "so-3a4739b9/2/1": "skipped", "so-3a4739b9/3/1": "unknown"}, {confirmed: 2, skipped: 1, verified: 2, manual: 0}), ids);
const unknown = {status: () => "unknown", mode: () => null, gen: () => null};
out.b = stakeoutSetCounts(seq, {...unknown, counts: () => ({confirmed: 2, skipped: 1, verified: 1, manual: 1})}, ids);
out.c = stakeoutSetCounts(seq, {...unknown, counts: () => null}, ids);
out.d = stakeoutSetCounts(null, {...unknown, counts: () => ({confirmed: 2, skipped: 1, verified: 1, manual: 1})}, ids);
out.e = stakeoutSetCounts(seq, model({"so-3a4739b9/1/2": "confirmed"}, null), ids);
""" % json.dumps(sample_set_doc()))
        self.assertEqual(out["a"], {"targets": 12, "open": 8, "confirmed": 2, "skipped": 1, "unknown": 1})
        self.assertEqual(out["b"], {"targets": 12, "open": 0, "confirmed": 2, "skipped": 1, "unknown": 9})
        self.assertEqual(out["c"], {"targets": 12, "open": 0, "confirmed": None, "skipped": None, "unknown": 12})
        self.assertEqual(out["d"]["targets"], None)
        self.assertEqual(out["e"], {"targets": 12, "open": 11, "confirmed": 1, "skipped": 0, "unknown": 0})

    def test_overview_loader_is_the_wp4_hook(self):
        out = run_core("""
const calls = [], answer = {sets: []};
globalThis.api = {json: (path, options) => { calls.push(path); return new Promise((resolve, reject) => {
  if (options.signal) options.signal.addEventListener("abort", () => reject(Object.assign(new Error("x"), {name: "AbortError"})));
  setTimeout(() => resolve(answer), 20); }); }};
const first = await loadStakeoutOverview("p/1", new AbortController().signal);
out.same = first === answer; out.calls = calls; out.keys = Object.keys(first);
const controller = new AbortController(), pending = loadStakeoutOverview("p/1", controller.signal); controller.abort();
out.aborted = await pending;
""", stubs="const PROJECT_READ_TIMEOUT = 20000;", asynchronous=True)
        # WP4 part B (plan change B-2): the progress list joins the overview right before the return.
        self.assertEqual((out["same"], out["calls"][0], out["keys"], out["aborted"]),
                         (True, "/api/stakeout?project_id=p%2F1", ["sets", "stakeout_progress"], None))
        line = next(line for line in source_text("95-stakeout.js").split("\n")
                    if line.startswith("async function loadStakeoutOverview("))
        self.assertTrue(line.endswith("if(signal&&signal.aborted)return null;"
                                      "overview.stakeout_progress=await loadStakeoutProgress(projectId,signal);"
                                      "return overview;}"), line[-120:])

    def test_static_overview_contract(self):
        core = source_text("95-stakeout.js")
        self.assertEqual(core.count('VIEW_MOUNTS["project-stakeout"]=mountStakeout;'), 1)
        self.assertNotIn("renderRoute(", core)
        overview = next(line for line in core.split("\n") if line.startswith("async function mountStakeoutOverview("))
        self.assertEqual(overview.count('"data-slot":"stakeout-export"'), 1)
        # WP4 B3 fills the slot with the one project-wide entry (plan change B-5); nothing else of the overview exports.
        hook = 'stakeoutExportEntry(ctx.project.id,ctx.signal)'
        self.assertEqual(overview.count(hook), 1)
        self.assertNotIn("export", overview.replace('"data-slot":"stakeout-export"', "").replace(hook, "").lower())
        detail = source_text("70-project-detail.js")
        self.assertIn('h("div",{class:"project-tools","data-slot":"project-tools"},stakeoutProjectEntry(project))', detail)



class TestStakeoutTargets(unittest.TestCase):

    def test_status_marks_are_text_and_symbol(self):
        out = run_core("""
for (const lang of ["en", "de"]) { RtkI18n.setLanguage(lang);
  out[lang] = ["open", "confirmed", "skipped", "unknown", "weird", null].map(stakeoutStatusMark);
  out[lang + "_keys"] = ["open", "confirmed", "skipped", "unknown"].map(s => dict[lang]["stakeout.status." + s]); }
""")
        for lang in ("en", "de"):
            marks = out[lang]
            self.assertEqual([mark["text"] for mark in marks[:4]], out[lang + "_keys"])
            self.assertEqual([mark["symbol"] for mark in marks[:4]], ["○", "●", "◇", "?"])
            self.assertEqual(marks[4], marks[3])
            self.assertEqual(marks[5], marks[3])
        self.assertNotEqual(out["en"][1]["text"], out["de"][1]["text"])

    def test_target_rows_from_injected_progress(self):
        out = run_core("""
const seq = stakeoutSequence([%s], {}), status = {"so-3a4739b9/1/2": "confirmed", "so-3a4739b9/1/3": "skipped", "so-3a4739b9/2/1": "unknown"};
const frame = stakeoutFrame(null, %s, 0);
out.rows = stakeoutTargetRows(seq, tid => status[tid] || "open", frame, 1000);
out.stale = stakeoutTargetRows(seq, tid => "open", {...frame, usable: false}).map(row => row.distance);
out.none = stakeoutTargetRows(seq, tid => "open", null).map(row => row.distance);
""" % (json.dumps(sample_set_doc()), json.dumps(LIVE_PACKET)))
        rows = out["rows"]
        self.assertEqual([row["tid"] for row in rows][:5], ["so-3a4739b9/1/1", "so-3a4739b9/1/2", "so-3a4739b9/1/3",
                                                          "so-3a4739b9/1/4", "so-3a4739b9/2/1"])
        self.assertEqual([row["status"] for row in rows[:5]], ["open", "confirmed", "skipped", "open", "unknown"])
        self.assertEqual(rows[4]["mark"]["symbol"], "?")
        self.assertEqual(rows[4]["name"], "Point 2")
        self.assertAlmostEqual(rows[4]["distance"], 0.05, delta=0.0005)
        self.assertEqual(set(rows[0]), {"tid", "set_id", "part_id", "name", "status", "mark", "distance"})
        self.assertEqual(set(out["stale"]), {None})
        self.assertEqual(set(out["none"]), {None})

    def test_unknown_is_never_open(self):
        out = run_core("""
const doc = %s, seq = stakeoutSequence([doc], {});
const overview = {progress: {active_tid: null, damaged_lines: 0}, sets: [{set_id: "so-3a4739b9", state: "ok", targets: 12}]};
const progress = stakeoutProgressFromOverview(overview), statusOf = tid => stakeoutStatusOf(progress, null, tid);
out.next = stakeoutNextTarget(seq, null, statusOf);
out.open = stakeoutSetCounts(seq, progress, ["so-3a4739b9"]).open;
out.confirmed = stakeoutConfirmedCount(progress, "so-3a4739b9");
out.symbols = [...new Set(stakeoutTargetRows(seq, statusOf, null).map(row => row.mark.symbol))];
const plain = stakeoutProgressFromOverview({sets: overview.sets}), packet = {target: {tid: "so-3a4739b9/2/1", status: "unknown"}};
const packetOf = tid => stakeoutStatusOf(plain, packet, tid);
out.point2 = stakeoutTargetRows(seq, packetOf, null).find(row => row.tid === "so-3a4739b9/2/1").status;
out.skip = stakeoutNextTarget(seq, "so-3a4739b9/1/4", packetOf);
""" % json.dumps(sample_set_doc()))
        self.assertIsNone(out["next"])
        self.assertEqual(out["open"], 0)
        self.assertIsNone(out["confirmed"])
        self.assertEqual(out["symbols"], ["?"])
        self.assertEqual(out["point2"], "unknown")
        self.assertEqual(out["skip"], "so-3a4739b9/3/1")



MAP_PREFIXES = DOM_PREFIXES + ("function coordToDeg(", "function fixLonLat(", "const POINT_SHAPES=", "function pointShapePath(",
                               "function mapSvg(", "const quantity=")
MAP_STUBS = MINI_DOM + "const fixIsStale = () => false;\n"


class TestStakeoutMap(unittest.TestCase):

    def test_world_transform_centres_the_rover(self):
        out = run_core("""
out.r = [];
for (const z of [1, 1.5, 4]) for (const p of [[350, 350], [120, 610], [-200, 900]]) {
  const t = stakeoutWorldTransform({zoom: z, panX: 13, panY: -7}, p); out.r.push([t.x + t.z * p[0], t.y + t.z * p[1], t.z]); }
out.mid = stakeoutWorldTransform({zoom: 1.5, panX: 0, panY: 0}, [350, 350]);
const free = stakeoutWorldTransform({zoom: 2, panX: 0, panY: 0}, null); out.free = [free.x + free.z * 350, free.y + free.z * 350];
const moved = stakeoutWorldTransform({zoom: 1, panX: 30, panY: -20}, null); out.moved = [moved.x, moved.y];
""")
        for x, y, z in out["r"]:
            self.assertAlmostEqual(x, 350, delta=1e-9)
            self.assertAlmostEqual(y, 350, delta=1e-9)
        self.assertEqual([row[2] for row in out["r"]], [1, 1, 1, 1.5, 1.5, 1.5, 4, 4, 4])
        self.assertAlmostEqual(out["mid"]["x"], -175, delta=1e-9)
        self.assertAlmostEqual(out["mid"]["x"] + 1.5 * 350, 350, delta=1e-9)
        self.assertEqual(out["free"], [350, 350])
        self.assertEqual(out["moved"], [30, -20])

    def test_map_marks_from_injected_progress(self):
        out = run_core("""
const doc = %s, status = {"so-3a4739b9/1/2": "confirmed", "so-3a4739b9/1/3": "skipped", "so-3a4739b9/2/1": "unknown"};
out.marks = stakeoutMapMarks(doc, tid => status[tid] || "open", "so-3a4739b9/1/4");
out.extent = stakeoutMapExtent(doc, {tid: "x", lat_e8: 5200000000, lon_e8: 1100000000});
""" % json.dumps(sample_set_doc()))
        marks = out["marks"]
        self.assertEqual(len(marks), 12)
        by = {mark["tid"]: mark for mark in marks}
        self.assertEqual([by["so-3a4739b9/1/1"]["shape"], by["so-3a4739b9/1/2"]["shape"], by["so-3a4739b9/1/3"]["shape"],
                          by["so-3a4739b9/2/1"]["shape"]], ["ring", "disc", "diamond", "dashed-ring"])
        self.assertEqual([mark["tid"] for mark in marks if mark["current"]], ["so-3a4739b9/1/4"])
        self.assertEqual(by["so-3a4739b9/2/1"]["label"], "Point 2: Unknown")
        self.assertIn([1100000000, 5200000000], out["extent"])
        self.assertIn([999999943, 5100000027], out["extent"])

    def test_map_renders_the_set_without_survey_geometry(self):
        out = run_core("""
const doc = %s, statusOf = () => "open";
const svg = mapSvg({lines: [], points: []}, {lat_e8: 5300000000, lon_e8: 1000000000},
  {extent: stakeoutMapExtent(doc, null), layers: stakeoutMapLayers(doc, statusOf, "so-3a4739b9/2/1"), unclipped: true});
out.tag = svg.tagName; out.cls = svg.getAttribute("class");
out.targets = svg.find(el => el.getAttribute("class") && el.getAttribute("class").startsWith("stakeout-target")).length;
out.current = svg.find(el => el.getAttribute("class") === "stakeout-target current").map(el => el.dataset.tid);
out.parts = svg.find(el => el.getAttribute("class") === "stakeout-part").length;
out.lines = ["stakeout-aim", "stakeout-track"].map(name => svg.find(el => el.getAttribute("class") === name).length);
const dot = svg.find(el => (el.getAttribute("class") || "").startsWith("map-position"))[0];
out.dotHidden = dot.classList.contains("hidden");
out.project = typeof svg.project === "function" ? svg.project([999999943, 5100000027]).map(v => Number.isFinite(v)) : null;
out.empty = mapSvg({lines: [], points: []}, null, {}).className;
const plain = mapSvg({lines: [], points: [{id: "a", type: "valve", name: "V", coordinate: [1000000000, 5100000000, 0]}]}, {lat_e8: 5300000000, lon_e8: 1000000000}, {});
out.plainDotHidden = plain.find(el => (el.getAttribute("class") || "").startsWith("map-position"))[0].classList.contains("hidden");
""" % json.dumps(sample_set_doc()), stubs=MAP_STUBS, extra=MAP_PREFIXES)
        self.assertEqual(out["tag"], "SVG")
        self.assertEqual(out["targets"], 12)
        self.assertEqual(out["current"], ["so-3a4739b9/2/1"])
        self.assertEqual(out["parts"], 3)
        self.assertEqual(out["lines"], [1, 1])
        self.assertIs(out["dotHidden"], False)
        self.assertEqual(out["project"], [True, True])
        self.assertEqual(out["empty"], "map-empty")
        self.assertIs(out["plainDotHidden"], True)

    def test_draw_map_uses_the_shared_gestures(self):
        detail = source_text("70-project-detail.js")
        draw = next(line for line in detail.split("\n") if line.strip().startswith("const drawMap="))
        self.assertIn("bindMapGestures(", detail)
        self.assertNotIn("pointerdown", draw)
        self.assertNotIn("pointerdown", detail)



GUIDE_STUBS = MAP_STUBS + r"""
const gateReasonText = code => "gate:" + code;
let routeController = new AbortController();
const PROJECT_READ_TIMEOUT = 20000, main = new El("main"), overlay = new El("div");
let overviewCalls = 0, overviewAnswer = {sets: [{set_id: "so-3a4739b9", state: "ok", targets: 12}], session: {target: null, orders: {}}};
globalThis.api = {json: async path => { if (!String(path).startsWith("/api/stakeout/progress")) overviewCalls++; await new Promise(r => setTimeout(r, 20)); return structuredClone(overviewAnswer); }};
const store = {state: {live: null}, set() {}};
const showError = () => {}, showSuccess = () => {}, iconButton = (label, fn) => { const b = new El("button"); b.textContent = label; return b; };
"""


class TestStakeoutGuide(unittest.TestCase):

    def test_guide_renders_null_numbers_as_dashes(self):
        out = run_core("""
const far = {...%s, position: null, guidance: {state: "far", dn_m: null, de_m: null, dist_m: null, bearing_deg: null, tolerance_m: 0.1, fine_radius_m: 5, window: {have: null, need: null, spread_m: null}, reasons: ["new_reason"]}, gate: {accepted: false, reasons: []}, fix_age_ms: null};
const frame = stakeoutFrame(null, far, 0), bar = stakeoutQualityBar();
for (const lang of ["en", "de"]) { RtkI18n.setLanguage(lang); bar.update(frame, 500, null);
  out[lang] = Object.fromEntries(bar.all().filter(el => el.dataset.field).map(el => [el.dataset.field, el.find(x => "value" in x.dataset)[0].textContent]));
  out[lang + "_state"] = stakeoutStateText({state: "weird"}); out[lang + "_far"] = stakeoutStateText(far.guidance);
  out[lang + "_reasons"] = stakeoutReasonTexts(far.guidance, far.gate, null);
  out[lang + "_keys"] = [dict[lang]["stakeout.state_unknown"], dict[lang]["stakeout.reason_unknown"], dict[lang]["stakeout.state_far"]]; }
const dial = targetDial(); dial.update(far.guidance, far.motion, true, 0.1);
out.marker = dial.find(el => el.getAttribute("class") === "dial-rover")[0].style.display;
""" % json.dumps(LIVE_PACKET), stubs=GUIDE_STUBS, extra=DOM_PREFIXES)
        for lang in ("en", "de"):
            fields = out[lang]
            self.assertEqual((fields["fix"], fields["sigma"], fields["age"]), ("—", "—", "—"), lang)
            self.assertFalse(any("0,00" in value or "0.00" in value for value in fields.values()), fields)
            unknown, reason, far = out[lang + "_keys"]
            self.assertEqual(out[lang + "_state"]["text"], unknown)
            self.assertEqual(out[lang + "_far"]["text"], far)
            self.assertEqual(out[lang + "_reasons"], [reason])
        self.assertEqual(out["marker"], "none")

    def test_reason_texts_cover_the_wp2_codes(self):
        codes = ["awaiting_fix", "gst_pending", "outside_tolerance", "gate_rejected", "mean_outside_tolerance",
                 "horizontal_spread_too_high", "config_changed", "session_error", "recording_active", "fix_stale",
                 "corrections_missing", "write_uncertain"]
        out = run_core("""
out.missing = []; for (const lang of ["en", "de"]) for (const code of %s) if (!dict[lang]["stakeout.reason." + code]) out.missing.push(lang + ":" + code);
out.text = stakeoutReasonText("gst_pending"); out.key = dict.en["stakeout.reason.gst_pending"];
""" % json.dumps(codes))
        self.assertEqual(out["missing"], [])
        self.assertEqual(out["text"], out["key"])

    def test_view_contract_for_wp4(self):
        out = run_core("""
const doc = %s, target = doc.targets.find(t => t.tid === "so-3a4739b9/2/1"), packet = %s;
const tracking = {active_project_id: "p1", projects: [{id: "p1", name: "P"}]};
const ctx = createStakeoutContext({params: {id: "p1"}}, routeController.signal, tracking.projects[0], tracking, {kind: "target", tid: target.tid});
ctx.setOverview(structuredClone(overviewAnswer)); ctx.sets = [doc]; ctx.sequence = stakeoutSequence([doc], {});
let clock = 1000; const view = stakeoutTargetView(ctx, doc, target, {now: () => clock});
out.keys = Object.keys(view).sort(); out.signal = view.signal === ctx.signal && view.signal === routeController.signal;
out.ids = [view.tid, view.projectId];
let notified = 0; view.subscribe(() => notified++);
ctx.accept(packet, 1000); out.latest = view.latest().packet.fix_seq;
out.ready = view.ready();
clock = 5000; out.at5000 = [view.frameStale(), view.ready()];
clock = 5251; out.at5251 = [view.frameStale(), view.ready()];
clock = 2000; ctx.accept({...packet, fix_seq: 11, guidance: {...packet.guidance, state: "settling"}}, 2000); out.settling = view.ready();
ctx.accept({...packet, fix_seq: 12}, 2000); out.again = view.ready();
ctx.calm("stakeout_busy", "live"); out.busy = view.ready();
ctx.calm("stakeout_write_uncertain", "live"); out.uncertain = view.ready(); ctx.calm(null, "live"); out.cleared = view.ready();
ctx.accept({...packet, fix_seq: 13, schema: 2}, 2500); out.rejected = [view.latest().packet.fix_seq, view.latest().usable, view.frameStale()];
ctx.accept({...packet, fix_seq: 14}, 2600);
out.progress = view.progressOf("so-3a4739b9/2/1"); out.other = view.progressOf("so-3a4739b9/1/1");
ctx.accept({...packet, fix_seq: 15, target: {...packet.target, status: "unknown"}}, 2700); out.unknownReady = view.ready();
out.next = view.nextTid();
const before = overviewCalls; await Promise.all([view.reload(), view.reload()]); out.reloads = overviewCalls - before;
out.overviewSame = view.overview === ctx.overview; out.notified = notified > 0;
overviewAnswer = {...overviewAnswer, progress: {active_tid: null, damaged_lines: 0}};
await view.reload(); out.afterWp4 = view.progressOf("so-3a4739b9/1/1");
stakeoutConfirmActions = v => { const b = new El("button"); b.setAttribute("data-wp4", "1"); return [b]; };
const dock = stakeoutActionDock(view), kids = dock.children;
out.dock = [kids[0].dataset.stakeoutActions === "", kids[0].children.length, kids[0].children[0].dataset.wp4, kids[1].dataset.action];
""" % (json.dumps(sample_set_doc()), json.dumps(LIVE_PACKET)), stubs=GUIDE_STUBS.replace("const fixIsStale = () => false;\n", "") + HEADER_STUBS.replace('localText = text => text, ', "") + "\n" + source_block("30-connection.js", "function updateGlobalStatus("),
                       extra=DOM_PREFIXES + tuple(p for p in HEADER_PREFIXES if p != "const store=") +
                       ("function runButtonAction(", "async function refreshSection(", "function sectionTarget("),
                       asynchronous=True)
        self.assertEqual(out["keys"], sorted(["project", "overview", "set", "target", "tid", "signal", "latest", "subscribe",
                                              "ready", "reload", "nextTid", "projectId", "frameStale", "progressOf"]))
        self.assertIs(out["signal"], True)
        self.assertEqual(out["ids"], ["so-3a4739b9/2/1", "p1"])
        self.assertEqual(out["latest"], 10)
        self.assertIs(out["ready"], True)
        self.assertEqual(out["at5000"], [False, True])
        self.assertEqual(out["at5251"], [True, False])
        self.assertIs(out["settling"], False)
        self.assertIs(out["again"], True)
        self.assertIs(out["busy"], True)
        self.assertIs(out["uncertain"], False)
        self.assertIs(out["cleared"], True)
        self.assertEqual(out["rejected"], [12, False, True])
        self.assertEqual(out["progress"], {"status": "open", "mode": None, "gen": 0})
        self.assertEqual(out["other"], {"status": "open", "mode": None, "gen": 0})
        self.assertIs(out["unknownReady"], False)
        self.assertEqual(out["next"], "so-3a4739b9/3/1")
        # WP4 B (Codex re-review): the second reload was asked while the first ran, so it gets its own, later request.
        self.assertEqual(out["reloads"], 2)
        self.assertIs(out["overviewSame"], True)
        self.assertIs(out["notified"], True)
        self.assertEqual(out["afterWp4"], {"status": "unknown", "mode": None, "gen": None})
        self.assertEqual(out["dock"], [True, 1, "1", "next"])



class TestStakeoutReviewFindings(unittest.TestCase):
    """Codex review of WP3 (P2 findings 1, 3, 4 and 5; finding 2 is a browser case of the stakeout suite)."""

    def test_list_distances_turn_stale_with_the_frame(self):
        out = run_core("""
const seq = stakeoutSequence([%s], {}), frame = stakeoutFrame(null, %s, 0);
out.fresh = stakeoutTargetRows(seq, () => "open", frame, 4000).find(row => row.tid === "so-3a4739b9/2/1").distance;
out.old = stakeoutTargetRows(seq, () => "open", frame, 4001).map(row => row.distance);
""" % (json.dumps(sample_set_doc()), json.dumps(LIVE_PACKET)))
        self.assertAlmostEqual(out["fresh"], 0.05, delta=0.0005)
        self.assertEqual(set(out["old"]), {None})

    def test_overlap_hint_is_bounded_on_huge_files(self):
        out = run_import("""
const big = JSON.stringify({type: "FeatureCollection", features: [
  {type: "Feature", properties: {name: "Cloud"}, geometry: {type: "MultiPoint",
    coordinates: Array.from({length: 200000}, (_, i) => [10 + (i % 500) * 1e-5, 51 + Math.floor(i / 500) * 1e-5])}},
  {type: "Feature", properties: {name: "Near"}, geometry: {type: "Point", coordinates: [10, 51.000000001]}}]});
let started = Date.now();
const preview = normalizeGeoJson(big, {name: "big.geojson", size: big.length});
const selection = stakeoutDefaultSelection(preview);
started = Date.now();
out.hints = stakeoutImportHints(preview, selection, [5100000000, 1000000000]).map(hint => hint.code);
out.ms = Date.now() - started; out.selected = selection.size;
// Grid buckets: the same pairs as a full comparison on a small cluster.
const parts = Array.from({length: 6}, (_, p) => ({type: "Feature", properties: {name: "P" + p}, geometry: {type: "MultiPoint",
  coordinates: Array.from({length: 40}, (_, i) => [10 + ((i * 7 + p * 3) % 41) * 1.3e-7, 51 + ((i * 11 + p * 5) % 37) * 1.1e-7])}}));
const small = JSON.stringify({type: "FeatureCollection", features: parts});
const pv = normalizeGeoJson(small, {name: "s", size: small.length}), sel = stakeoutDefaultSelection(pv);
const brute = []; const list = stakeoutSelectedTargets(pv, sel);
for (let i = 0; i < list.length; i++) for (let j = i + 1; j < list.length; j++) if (list[i].part !== list[j].part &&
  stakeoutDistance(list[i].lat_e8, list[i].lon_e8, list[j].lat_e8, list[j].lon_e8) < STAKEOUT_IMPORT.overlapM) brute.push(1);
out.brute = brute.length;
out.grid = stakeoutImportHints(pv, sel, null).filter(hint => hint.code === "overlap").length;
""")
        self.assertEqual(out["selected"], 200001)
        self.assertLess(out["ms"], 3000, out["ms"])
        self.assertIn("extent", out["hints"])
        self.assertGreater(out["brute"], 0)
        self.assertEqual(out["grid"], min(out["brute"], 20))

    def test_overlap_hint_across_the_antimeridian_and_near_a_pole(self):
        out = run_import("""
const pairsOf = coords => { const text = JSON.stringify({type: "FeatureCollection", features: coords.map((c, i) =>
  ({type: "Feature", properties: {name: "T" + i}, geometry: {type: "Point", coordinates: c}}))});
  const preview = normalizeGeoJson(text, {name: "x", size: text.length});
  return stakeoutImportHints(preview, stakeoutDefaultSelection(preview), null).filter(hint => hint.code === "overlap")
    .map(hint => [hint.a.name, hint.b.name, hint.distance_m]); };
out.dateline = pairsOf([[0, 0], [179.99999999, 0], [-179.99999999, 0]]);
out.pole = pairsOf([[0, 0], [0, 89.9999999], [90, 89.9999999]]);
out.southPole = pairsOf([[10, 51], [-170, -89.9999999], [100, -89.9999999]]);
out.devicePole = stakeoutDistance(8999999990, 0, 8999999990, 9000000000);
out.far = pairsOf([[0, 0], [0, 89.9999999], [180, 89.9999999]]);
out.farDevice = stakeoutDistance(8999999990, 0, 8999999990, 18000000000);
""")
        self.assertEqual([pair[:2] for pair in out["dateline"]], [["T1", "T2"]])
        self.assertAlmostEqual(out["dateline"][0][2], 0.0022, delta=0.0002)
        self.assertLess(out["devicePole"], 0.02)
        self.assertEqual([pair[:2] for pair in out["pole"]], [["T1", "T2"]])
        self.assertAlmostEqual(out["pole"][0][2], out["devicePole"], delta=1e-12)
        self.assertEqual([pair[:2] for pair in out["southPole"]], [["T1", "T2"]])
        # Across the pole the device formula gives more than 2 cm: no hint, as the device would judge it.
        self.assertGreater(out["farDevice"], 0.02)
        self.assertEqual(out["far"], [])

    def test_map_marks_follow_a_status_change(self):
        out = run_core("""
const doc = %s; let status = "unknown";
const svg = mapSvg({lines: [], points: []}, null, {extent: stakeoutMapExtent(doc, null), layers: stakeoutMapLayers(doc, () => status, "so-3a4739b9/2/1"), unclipped: true});
const mark = () => svg.querySelector('.stakeout-target[data-tid="so-3a4739b9/2/1"]');
const read = () => ({shape: mark().dataset.shape, status: mark().dataset.status, label: mark().getAttribute("aria-label"), geometry: mark().children[0].localName,
  current: mark().classList.contains("current"), at: mark().dataset.x});
out.before = read();
for (const next of ["skipped", "confirmed", "open"]) { status = next; stakeoutRefreshMarks(svg, doc, () => status, "so-3a4739b9/2/1"); out[next] = read(); }
out.key = dict.en["stakeout.status.skipped"];
""" % json.dumps(sample_set_doc()), stubs=MAP_STUBS, extra=MAP_PREFIXES)
        self.assertEqual((out["before"]["shape"], out["before"]["geometry"]), ("dashed-ring", "circle"))
        self.assertEqual(out["skipped"], {"shape": "diamond", "status": "skipped", "label": "Point 2: " + out["key"],
                                          "geometry": "path", "current": True, "at": out["before"]["at"]})
        self.assertEqual((out["confirmed"]["shape"], out["confirmed"]["geometry"]), ("disc", "circle"))
        self.assertEqual((out["open"]["shape"], out["open"]["status"]), ("ring", "open"))

    def test_scale_label_follows_the_zoom(self):
        out = run_core("out.r = [[1, 1], [1, 4], [5, 1.5], [10, 0.5]].map(([m, z]) => stakeoutScaleText(m, z));")
        self.assertEqual(out["r"], ["1 m", "0.25 m", "3.3 m", "20 m"])


# WP4 part B (src/97-stakeout-actions.js): steerable request answers for stakeoutCommand.
COMMAND_HELPERS = r"""
const fail = (status, code) => Object.assign(new Error(code || "refused"), {status, code});
const timeout = () => Object.assign(new Error("Request timed out."), {name: "TimeoutError"});
const offline = () => new TypeError("Failed to fetch");
const script = answers => { const calls = [];
  return {calls, request: async payload => { calls.push(JSON.stringify(payload)); const next = answers.shift();
    if (typeof next === "function") return next(); if (next instanceof Error) throw next; return next; }}; };
const pauses = []; const sleep = async ms => { pauses.push(ms); };
const payload = {action: "confirm", project_id: "p1", tid: "so-3a4739b9/2/1", cmd: "0123456789abcdef", expect_gen: 0, mode: "verified"};
const brief = result => ({outcome: result.outcome, code: result.error ? result.error.code ?? null : null, left: result.left,
  answer: result.answer || null});
"""


class TestStakeoutCommand(unittest.TestCase):
    """WP4 B1: command ids, repeats with the same body and the reading of the outcome."""

    def run_command(self, body):
        return run_core(COMMAND_HELPERS + body, asynchronous=True)

    def test_command_id_shape_under_node(self):
        out = run_core("""
const ids = Array.from({length: 1000}, () => stakeoutCommandId());
out.bad = ids.filter(id => !/^[0-9a-f]{16}$/.test(id)); out.unique = new Set(ids).size;
""")
        self.assertEqual((out["bad"], out["unique"]), ([], 1000))

    def test_default_request_uses_the_real_api_under_node(self):
        out = run_core("""
const seen = [];
globalThis.fetch = async (path, init) => { seen.push({path, method: (init && init.method) || "GET",
  token: init && init.headers ? init.headers["X-CSRF-Token"] : null, body: init && init.body ? init.body : null});
  const body = path === "/api/session" ? {csrf_token: "tok"} : {persisted: true, repeated: false, tid: "so-3a4739b9/2/1", gen: 0, status: "confirmed"};
  return new Response(JSON.stringify(body), {status: 200, headers: {"content-type": "application/json"}}); };
const payload = {action: "skip", project_id: "p1", tid: "so-3a4739b9/2/1", cmd: "0123456789abcdef", expect_gen: 2};
const result = await stakeoutCommand(payload, {sleep: async () => {}});
out.outcome = result.outcome; out.seen = seen; out.payload = payload;
""", stubs="const restartHoldActive = () => false, signInRedirect = () => {}; let csrfToken = null;\n" + api_block(),
                       asynchronous=True)
        self.assertEqual(out["outcome"], "saved")
        posts = [entry for entry in out["seen"] if entry["path"] == "/api/stakeout"]
        self.assertEqual(len(posts), 1)
        self.assertEqual((posts[0]["method"], posts[0]["token"]), ("POST", "tok"))
        self.assertEqual(json.loads(posts[0]["body"]), out["payload"])

    def test_retry_keeps_the_body_under_node(self):
        out = self.run_command("""
const s = script([timeout(), offline(), {persisted: true, repeated: true, gen: 0}]);
out.r = brief(await stakeoutCommand(payload, {request: s.request, sleep}));
out.calls = s.calls; out.pauses = pauses; out.body = JSON.stringify(payload);
""")
        self.assertEqual(out["r"]["outcome"], "saved")
        self.assertEqual(out["calls"], [out["body"]] * 3)
        self.assertEqual(out["pauses"], [1000, 1000])

    def test_busy_is_retried_under_node(self):
        for status, code in ((409, "storage_busy"), (503, "stakeout_busy")):
            with self.subTest(code=code):
                out = self.run_command("""
const s = script([fail(%d, %s), {persisted: true, repeated: false}]);
out.r = brief(await stakeoutCommand(payload, {request: s.request, sleep})); out.n = s.calls.length;
""" % (status, json.dumps(code)))
                self.assertEqual((out["r"]["outcome"], out["n"]), ("saved", 2))

    def test_write_uncertain_is_never_retried_under_node(self):
        out = self.run_command("""
const s = script([fail(503, "stakeout_write_uncertain"), {persisted: true}]);
out.r = brief(await stakeoutCommand(payload, {request: s.request, sleep})); out.n = s.calls.length;
""")
        self.assertEqual((out["r"]["outcome"], out["r"]["code"], out["n"]), ("refused", "stakeout_write_uncertain", 1))

    def test_three_failures_are_unconfirmed_under_node(self):
        out = self.run_command("""
const s = script([timeout(), timeout(), timeout(), {persisted: true}]);
out.r = brief(await stakeoutCommand(payload, {request: s.request, sleep})); out.n = s.calls.length;
""")
        self.assertEqual((out["r"]["outcome"], out["r"]["left"], out["n"]), ("unconfirmed", False, 3))

    def test_changed_and_refused_are_not_retried_under_node(self):
        out = self.run_command("""
out.rows = [];
for (const error of [fail(409, "target_changed"), fail(409, "not_ready"), fail(409, "target_not_open"),
                     Object.assign(new Error("Action refused."), {name: "ForbiddenError", status: 403}),
                     new Error("Session expired")]) {
  const s = script([error, {persisted: true}]);
  const r = brief(await stakeoutCommand(payload, {request: s.request, sleep}));
  out.rows.push([r.outcome, r.code, s.calls.length]);
}
""")
        self.assertEqual(out["rows"], [["changed", "target_changed", 1], ["refused", "not_ready", 1],
                                       ["refused", "target_not_open", 1], ["refused", None, 1],
                                       ["refused", None, 1]])

    def test_saved_only_after_persisted_under_node(self):
        out = self.run_command("""
out.rows = [];
for (const answer of [{persisted: false}, {}, null, {persisted: "true"}, {persisted: true}]) {
  const s = script([answer]); out.rows.push((await stakeoutCommand(payload, {request: s.request, sleep})).outcome); }
""")
        self.assertEqual(out["rows"], ["refused", "refused", "refused", "refused", "saved"])

    def test_navigation_stops_further_attempts_under_node(self):
        out = self.run_command("""
const route = new AbortController(), s = script([timeout(), {persisted: true}]);
out.first = brief(await stakeoutCommand(payload, {request: s.request, signal: route.signal, sleep: async () => route.abort()}));
out.n = s.calls.length;
const late = new AbortController(), t = script([() => { late.abort(); return {persisted: true, repeated: false}; }]);
out.second = brief(await stakeoutCommand(payload, {request: t.request, signal: late.signal, sleep}));
const gone = new AbortController(), u = script([() => { gone.abort(); throw timeout(); }, {persisted: true}]);
out.third = brief(await stakeoutCommand(payload, {request: u.request, signal: gone.signal, sleep}));
out.m = u.calls.length;
""")
        self.assertEqual((out["first"]["outcome"], out["first"]["left"], out["n"]), ("unconfirmed", True, 1))
        self.assertEqual((out["second"]["outcome"], out["second"]["left"]), ("saved", True))
        self.assertEqual((out["third"]["outcome"], out["third"]["left"], out["m"]), ("unconfirmed", True, 1))

    def test_largest_payload_fits_the_body_limit_under_node(self):
        out = run_core("""
const note = "\\u{1F600}".repeat(500);
const body = {action: "confirm", project_id: "P".repeat(64), tid: "so-ffffffff/500/1000000", cmd: stakeoutCommandId(),
  expect_gen: 999999999999, mode: "manual", note: note};
out.chars = [...note].length; out.bytes = new TextEncoder().encode(JSON.stringify(body)).length;
""")
        self.assertEqual(out["chars"], 500)
        self.assertLess(out["bytes"], 4096)


# A view like stakeoutTargetView's (WP3 E25) with steerable status, readiness and frame; the dock functions are stubs.
ACTION_HELPERS = r"""
const feedback = [], reloads = [], modals = [];
globalThis.showFeedback = spec => { feedback.push([spec.kind, spec.text]); };
globalThis.showSuccess = text => { feedback.push(["ok", text]); };
let modalAnswer = null; globalThis.modal = async spec => { modals.push(spec.fields.map(field => [field.name, field.maxLength])); return modalAnswer; };
const fresh = {packet: {position: {lat_e8: 5100000000, lon_e8: 1000000000}, guidance: {state: "ready", reasons: []}}};
const mkView = o => { const view = {tid: "so-3a4739b9/2/1", projectId: "p1", signal: o.signal || new AbortController().signal,
  overview: o.overview || {progress: {active_tid: null, damaged_lines: 0, write_uncertain: false}},
  latest: () => o.frame === undefined ? fresh : o.frame, frameStale: () => Boolean(o.stale), ready: () => Boolean(o.ready),
  progressOf: () => ({status: o.status, mode: null, gen: o.gen === undefined ? 4 : o.gen}),
  reload: async () => { reloads.push(view.tid); }}; return view; };
const shape = nodes => [].concat(nodes || []).map(node => [node.dataset.action, node.disabled, node.textContent]);
const settle = () => new Promise(resolve => setTimeout(resolve, 0));
"""


def run_actions(body, language="en"):
    return run_core(ACTION_HELPERS + body, stubs=MINI_DOM, extra=DOM_PREFIXES, asynchronous=True, language=language)


SAMPLE_PROGRESS_OVERVIEW = {
    "progress": {"active_tid": "so-3a4739b9/1/2", "damaged_lines": 0, "write_uncertain": False},
    "sets": [{"set_id": "so-3a4739b9", "state": "ok", "progress": {"confirmed": 1, "verified": 1, "manual": 0, "skipped": 1}}],
    "stakeout_progress": {"schema": 1, "project_id": "p1", "active_tid": "so-3a4739b9/1/2", "write_uncertain": False,
                          "orders": {}, "floors": {"so-3a4739b9": 2},
                          "counts": {"so-3a4739b9": {"confirmed": 1, "verified": 1, "manual": 0, "skipped": 1}},
                          "states": [["so-3a4739b9/1/2", "confirmed", 3, "verified", "point-1"],
                                     ["so-3a4739b9/1/3", "skipped", 2, None, None],
                                     ["so-3a4739b9/1/4", "open", 5, None, None],
                                     ["so-3a4739b9/1/5", "bogus", 2, None, None]]}}

# Every code of plan E19 an action may answer with (new and reused ones).
E19_CODES = ["target_changed", "target_not_open", "target_open", "not_ready", "measurement_active", "quality_rejected",
             "point_limit_reached", "no_position", "storage_reserve", "tracking_storage_failed", "stakeout_write_uncertain",
             "invalid_request", "storage_busy", "stakeout_busy", "project_not_active", "project_changed",
             "recording_active", "target_not_found", "set_not_found", "set_damaged", "undo_candidate_changed"]


class TestStakeoutActions(unittest.TestCase):
    """WP4 B2: the progress list from the device (B-3) and the action bar in WP3's dock (B-4)."""

    def test_device_progress_uses_rows_and_floors_under_node(self):
        out = run_core("""
const overview = %s, tids = ["so-3a4739b9/1/2", "so-3a4739b9/1/3", "so-3a4739b9/1/4", "so-3a4739b9/1/5", "so-3a4739b9/2/1", "so-00000001/1/1"];
const read = model => tids.map(tid => [model.status(tid), model.mode(tid), model.gen(tid)]);
out.loaded = read(stakeoutProgress(overview)); out.counts = stakeoutProgress(overview).counts("so-3a4739b9");
out.active = stakeoutActiveTid(overview);
const failed = {...overview, stakeout_progress: null};
out.failed = read(stakeoutProgress(failed)); out.failedCounts = stakeoutProgress(failed).counts("so-3a4739b9");
const plain = {sets: overview.sets.map(set => ({set_id: set.set_id, state: "ok"})), stakeout_progress: null};
out.plain = read(stakeoutProgress(plain));
""" % json.dumps(SAMPLE_PROGRESS_OVERVIEW))
        self.assertEqual(out["loaded"], [["confirmed", "verified", 3], ["skipped", None, 2], ["open", None, 5],
                                         ["unknown", None, None], ["open", None, 2], ["open", None, 0]])
        self.assertEqual(out["counts"], {"confirmed": 1, "skipped": 1, "verified": 1, "manual": 0})
        self.assertEqual(out["active"], "so-3a4739b9/1/2")
        self.assertEqual(out["failed"], [["unknown", None, None]] * 6)
        self.assertEqual(out["failedCounts"], {"confirmed": 1, "skipped": 1, "verified": 1, "manual": 0})
        self.assertEqual(out["plain"], [["open", None, 0]] * 6)

    def test_progress_list_is_loaded_with_the_overview_under_node(self):
        out = run_core("""
const calls = []; let mode = "ok";
globalThis.api = {json: (path, options) => { calls.push(path); return new Promise((resolve, reject) => {
  if (options.signal) options.signal.addEventListener("abort", () => reject(Object.assign(new Error("x"), {name: "AbortError"})));
  if (path.startsWith("/api/stakeout/progress")) {
    if (mode === "busy") return reject(Object.assign(new Error("busy"), {status: 503, code: "stakeout_busy"}));
    if (mode === "hang") return;
    return resolve({schema: 1, project_id: mode === "other" ? "p2" : "p/1", states: [], floors: {}, counts: {}});
  }
  resolve({sets: [], progress: {active_tid: null, damaged_lines: 0, write_uncertain: false}}); }); }};
const first = await loadStakeoutOverview("p/1", new AbortController().signal);
out.calls = calls.slice(); out.keys = Object.keys(first); out.list = first.stakeout_progress && first.stakeout_progress.project_id;
out.rest = [];
for (const m of ["busy", "other"]) { mode = m; out.rest.push((await loadStakeoutProgress("p/1", new AbortController().signal))); }
mode = "hang"; const controller = new AbortController(), pending = loadStakeoutProgress("p/1", controller.signal); controller.abort();
out.rest.push(await pending);
""", stubs="const PROJECT_READ_TIMEOUT = 20000;", asynchronous=True)
        self.assertEqual(out["calls"], ["/api/stakeout?project_id=p%2F1", "/api/stakeout/progress?project_id=p%2F1"])
        self.assertEqual((out["keys"], out["list"]), (["sets", "progress", "stakeout_progress"], "p/1"))
        self.assertEqual(out["rest"], [None, None, None])

    def test_action_buttons_follow_status_and_readiness_under_node(self):
        out = run_actions("""
out.rows = {};
for (const status of ["open", "confirmed", "skipped", "unknown"]) for (const ready of [true, false])
  out.rows[status + "/" + ready] = shape(stakeoutConfirmActions(mkView({status, ready})));
out.stale = shape(stakeoutConfirmActions(mkView({status: "open", ready: false, stale: true})));
out.noFrame = shape(stakeoutConfirmActions(mkView({status: "open", ready: false, frame: null, stale: true})));
out.noGen = shape(stakeoutConfirmActions(mkView({status: "open", ready: true, gen: null})));
const latched = {packet: {position: {lat_e8: 1, lon_e8: 2}, guidance: {state: "ready", reasons: ["write_uncertain"]}}};
out.latch = shape(stakeoutConfirmActions(mkView({status: "open", ready: false, frame: latched})));
out.latchOverview = shape(stakeoutConfirmActions(mkView({status: "skipped", overview: {progress: {write_uncertain: true}}})));
out.labels = ["confirm", "found", "skip", "reopen"].map(key => dict.en["stakeout.action." + key]);
""")
        confirm, found, skip, reopen = out["labels"]
        rows = out["rows"]
        self.assertEqual(rows["open/true"], [["confirm", False, confirm], ["found", False, found], ["skip", False, skip]])
        self.assertEqual(rows["open/false"], [["confirm", True, confirm], ["found", False, found], ["skip", False, skip]])
        for status in ("confirmed", "skipped"):
            for ready in ("true", "false"):
                self.assertEqual(rows[status + "/" + ready], [["reopen", False, reopen]])
        self.assertEqual((rows["unknown/true"], rows["unknown/false"]), ([], []))
        # Codex review P2: no generation (progress list not read) shows the reload notice, never an action.
        self.assertEqual([row[0] for row in out["noGen"]], [None])
        self.assertEqual(out["stale"], [["confirm", True, confirm], ["found", True, found], ["skip", False, skip]])
        self.assertEqual(out["noFrame"], out["stale"])
        self.assertEqual(out["latch"], [["confirm", True, confirm], ["found", True, found], ["skip", True, skip]])
        self.assertEqual(out["latchOverview"], [["reopen", True, reopen]])

    def test_unchanged_refill_keeps_the_nodes_under_node(self):
        out = run_actions("""
const o = {status: "open", ready: true}, view = mkView(o);
view.nextTid = () => null;
const dock = stakeoutActionDock(view), slot = dock.querySelector("[data-stakeout-actions]"), first = [...slot.children];
let replaced = 0; const original = slot.replaceChildren.bind(slot); slot.replaceChildren = (...items) => { replaced++; original(...items); };
dock.refresh(); dock.refresh();
out.same = [...slot.children].every((node, i) => node === first[i]) && slot.children.length === 3; out.quiet = replaced;
o.ready = false; dock.refresh(); out.changed = replaced; out.confirm = slot.children[0].disabled;
""", )
        self.assertEqual((out["same"], out["quiet"], out["changed"], out["confirm"]), (True, 0, 1, True))

    def test_pending_survives_a_refill_under_node(self):
        out = run_actions("""
let release; const posts = [];
stakeoutAction = payload => { posts.push(payload); return new Promise(resolve => { release = resolve; }); };
const view = mkView({status: "open", ready: true});
const skip = stakeoutConfirmActions(view).find(node => node.dataset.action === "skip");
skip.listeners.click[0]();
await settle();
out.during = shape(stakeoutConfirmActions(view)); out.again = shape(stakeoutConfirmActions(view));
stakeoutConfirmActions(view)[0].listeners.click[0](); await settle(); out.posts = posts.length;
release({persisted: true, repeated: false, tid: view.tid, gen: 4, status: "skipped"});
await settle(); await settle();
out.after = shape(stakeoutConfirmActions(view)); out.feedback = feedback; out.reloads = reloads.length;
out.saving = dict.en["stakeout.saving"]; out.saved = dict.en["stakeout.saved"];
""")
        saving = out["saving"]
        self.assertTrue(all(row[1] for row in out["during"]), out["during"])
        self.assertEqual(out["during"][2][2], saving)
        self.assertEqual(out["again"], out["during"])
        self.assertEqual(out["posts"], 1)
        self.assertTrue(all(not row[1] for row in out["after"]), out["after"])
        self.assertEqual((out["feedback"], out["reloads"]), ([["ok", out["saved"]]], 1))

    def test_request_bodies_and_outcomes_under_node(self):
        out = run_actions("""
const posts = []; let answers = [];
stakeoutAction = async payload => { posts.push(payload); const next = answers.shift(); if (next instanceof Error) throw next; return next; };
const fail = (status, code, reasons) => Object.assign(new Error("raw " + code), {status, code, reasons});
const tap = async (view, action) => { const node = stakeoutConfirmActions(view).find(item => item.dataset.action === action);
  node.listeners.click[0](); for (let i = 0; i < 5; i++) await settle(); };
out.rows = [];
const cases = [["confirm", {status: "open", ready: true, gen: 3}, [{persisted: true, gen: 3, status: "confirmed", mode: "verified"}]],
  ["skip", {status: "open", gen: 7}, [fail(409, "target_changed")]],
  ["reopen", {status: "confirmed", gen: 2}, [fail(409, "target_open")]],
  ["confirm", {status: "open", ready: true, gen: 0}, [fail(409, "quality_rejected", ["outside_tolerance"])]],
  ["skip", {status: "open", gen: 1}, [fail(507, "storage_reserve")]],
  ["skip", {status: "open", gen: 1}, [{persisted: false}]]];
for (const [action, o, list] of cases) {
  feedback.length = 0; reloads.length = 0; answers = list.slice(); const from = posts.length;
  await tap(mkView(o), action);
  out.rows.push({body: posts.slice(from), feedback: feedback.slice(), reloads: reloads.length}); }
modalAnswer = {note: "  by the fence  "}; feedback.length = 0; answers = [{persisted: true, gen: 4, status: "confirmed", mode: "manual"}];
await tap(mkView({status: "open", gen: 4}), "found"); out.found = posts[posts.length - 1]; out.modals = modals;
modalAnswer = null; const before = posts.length; await tap(mkView({status: "open", gen: 4}), "found"); out.cancelled = posts.length - before;
modalAnswer = {note: ""}; answers = [{persisted: true}]; await tap(mkView({status: "open", gen: 4}), "found"); out.empty = posts[posts.length - 1];
const route = new AbortController(); feedback.length = 0; reloads.length = 0;
stakeoutAction = async payload => { posts.push(payload); route.abort(); return {persisted: true}; };
await tap(mkView({status: "open", gen: 4, signal: route.signal}), "skip"); out.left = {feedback: feedback.slice(), reloads: reloads.length};
out.texts = ["saved", "changed", "unconfirmed"].map(key => dict.en["stakeout." + key]);
out.errors = ["target_open", "quality_rejected", "storage_reserve"].map(code => dict.en["stakeout.error." + code]);
out.reason = dict.en["stakeout.reason.outside_tolerance"];
""")
        saved, changed, unconfirmed = out["texts"]
        target_open, rejected, reserve = out["errors"]
        rows = out["rows"]
        for row in rows:
            self.assertEqual(len(row["body"]), 1, row)
            self.assertRegex(row["body"][0].pop("cmd"), r"^[0-9a-f]{16}$")
        tid = "so-3a4739b9/2/1"
        self.assertEqual(rows[0]["body"][0], {"action": "confirm", "project_id": "p1", "tid": tid, "expect_gen": 3, "mode": "verified"})
        self.assertEqual((rows[0]["feedback"], rows[0]["reloads"]), ([["ok", saved]], 1))
        self.assertEqual(rows[1]["body"][0], {"action": "skip", "project_id": "p1", "tid": tid, "expect_gen": 7})
        self.assertEqual((rows[1]["feedback"], rows[1]["reloads"]), ([["warn", changed]], 1))
        self.assertEqual(rows[2]["body"][0]["action"], "reopen")
        self.assertEqual((rows[2]["feedback"], rows[2]["reloads"]), ([["critical", target_open]], 0))
        self.assertEqual(rows[3]["feedback"], [["critical", rejected + " · " + out["reason"]]])
        self.assertEqual(rows[4]["feedback"], [["critical", reserve]])
        self.assertEqual((rows[5]["feedback"], rows[5]["reloads"]), ([["warn", unconfirmed]], 1))
        found = out["found"]
        self.assertRegex(found.pop("cmd"), r"^[0-9a-f]{16}$")
        self.assertEqual(found, {"action": "confirm", "project_id": "p1", "tid": tid, "expect_gen": 4, "mode": "manual",
                                 "note": "by the fence"})
        self.assertEqual(out["modals"][0], [["note", 500]])
        self.assertEqual(out["cancelled"], 0)
        self.assertNotIn("note", out["empty"])
        self.assertEqual(out["left"], {"feedback": [["ok", saved]], "reloads": 0})

    def test_every_error_code_has_a_text_under_node(self):
        out = run_core("""
const codes = %s; out.missing = [];
for (const lang of ["en", "de"]) { for (const code of codes) if (!dict[lang]["stakeout.error." + code]) out.missing.push(lang + ":" + code);
  for (const key of ["stakeout.reason.write_uncertain", "stakeout.saving", "stakeout.saved", "stakeout.unconfirmed", "stakeout.changed",
                     "stakeout.note", "stakeout.found_hint", "asset.stakeout"]) if (!dict[lang][key]) out.missing.push(lang + ":" + key); }
out.differ = codes.filter(code => dict.en["stakeout.error." + code] === dict.de["stakeout.error." + code]);
""" % json.dumps(E19_CODES))
        self.assertEqual(out["missing"], [])
        self.assertEqual(out["differ"], [])

    def test_stakeout_is_never_offered_for_manual_points_under_node(self):
        line = next(line for line in source_text("50-status.js").split("\n") if line.strip().startswith("const types=["))
        self.assertNotIn('"stakeout"', line)
        self.assertIn('"tap"', line)

    def test_no_next_button_from_wp4_under_node(self):
        actions = source_text("97-stakeout-actions.js")
        self.assertNotIn('"next"', actions)
        self.assertNotIn("stakeout.action.next", actions)
        out = run_core("out.keys = Object.keys(dict.en).filter(key => key.startsWith('stakeout.action.')).sort();")
        self.assertEqual(out["keys"], ["stakeout.action.confirm", "stakeout.action.found", "stakeout.action.reopen",
                                       "stakeout.action.skip"])


class TestStakeoutExportAndArchive(unittest.TestCase):
    """WP4 B3: the project-wide stakeout export (plan change B-5) and the archive mark from the metadata (S8)."""

    def test_export_entry_links_both_formats_for_the_project_under_node(self):
        out = run_core("""
El.prototype.showModal = function () { this.open = true; }; El.prototype.close = function () { this.open = false; };
globalThis.overlay = new El("div"); globalThis.requestSheetClose = dialog => dialog.close();
for (const lang of ["en", "de"]) { RtkI18n.setLanguage(lang);
  const route = new AbortController(), entry = stakeoutExportEntry("a/b&c", route.signal);
  const button = entry.matches("button") ? entry : entry.querySelector("button");
  button.listeners.click[0]();
  const dialog = overlay.querySelector("dialog");
  out[lang] = {action: button.dataset.action, label: button.textContent, cls: button.className,
    links: dialog.querySelectorAll("a").map(a => [a.dataset.export, a.getAttribute("href"), a.getAttribute("download")]),
    text: dialog.textContent, open: dialog.open};
  route.abort(); out[lang].closed = !dialog.open; overlay.replaceChildren(); }
out.keys = ["stakeout.export", "stakeout.export_hint"].map(key => [dict.en[key], dict.de[key]]);
""", stubs=MINI_DOM, extra=DOM_PREFIXES + ("function iconButton(",))
        for lang in ("en", "de"):
            row = out[lang]
            self.assertEqual(row["action"], "stakeout-export")
            self.assertIn("btn", row["cls"])
            self.assertEqual(row["links"], [["geojson", "/api/stakeout/export?project_id=a%2Fb%26c&format=geojson", ""],
                                            ["csv", "/api/stakeout/export?project_id=a%2Fb%26c&format=csv", ""]])
            self.assertTrue(row["open"])
            self.assertTrue(row["closed"])
        label, hint = out["keys"]
        self.assertEqual(out["en"]["label"], label[0])
        self.assertEqual(out["de"]["label"], label[1])
        self.assertIn(hint[0], out["en"]["text"])
        self.assertIn(hint[1], out["de"]["text"])

    def test_archive_mark_only_from_metadata_under_node(self):
        out = run_core("""
let fetches = 0; globalThis.fetch = async () => { fetches++; throw new Error("no fetch"); };
const mark = stakeoutArchiveMark({id: "ar1", stakeout_data: true});
out.mark = [mark.localName, mark.className, mark.textContent];
out.none = [{stakeout_data: false}, {}, {stakeout_data: "true"}, {stakeout_data: 1}].map(item => stakeoutArchiveMark(item));
out.fetches = fetches; out.text = dict.en["archive.stakeout_data"]; out.de = dict.de["archive.stakeout_data"];
""", stubs=MINI_DOM, extra=DOM_PREFIXES)
        self.assertEqual(out["mark"][:2], ["small", "archive-stakeout"])
        self.assertIn(out["text"], out["mark"][2])
        self.assertNotEqual(out["text"], out["de"])
        self.assertEqual(out["none"], [None, None, None, None])
        self.assertEqual(out["fetches"], 0)

    def test_static_hooks_of_the_export_entry_and_the_archive_mark(self):
        core = source_text("95-stakeout.js")
        overview = next(line for line in core.split("\n") if line.startswith("async function mountStakeoutOverview("))
        self.assertEqual(overview.count('h("div",{"data-slot":"stakeout-export"},stakeoutExportEntry(ctx.project.id,ctx.signal))'), 1)
        library = next(line for line in source_text("60-projects.js").split("\n") if line.startswith("function archiveLibrary("))
        self.assertEqual(library.count("stakeoutArchiveMark(item)"), 1)
        self.assertNotIn("set_id", source_text("97-stakeout-actions.js").split("function stakeoutExportEntry(")[1].split("\n")[0])


class TestStakeoutOwnExport(unittest.TestCase):
    """WP4 B4, check case 5: the device's own stakeout export goes through WP3's normalizer without loss."""

    def test_own_export_reimports_exactly_under_node(self):
        path = os.path.join(FIXTURES, "stakeout_export_sample.geojson")
        with open(path, encoding="utf-8") as handle:
            doc = json.loads(handle.read(), parse_float=decimal.Decimal)
        expected = []
        for feature in doc["features"]:
            coords = feature["geometry"]["coordinates"]
            lat, lon = coords[1] * 10 ** 8, coords[0] * 10 ** 8
            alt = coords[2] * 1000 if len(coords) > 2 else None
            for value in (lat, lon) + ((alt,) if alt is not None else ()):
                self.assertEqual(value, value.to_integral_value(), feature["id"])
            expected.append([feature["properties"]["name"], int(lat), int(lon), None if alt is None else int(alt)])
        self.assertTrue(any(row[3] is not None for row in expected), "the fixture carries a height")
        self.assertTrue(doc["stakeout_sets"])
        out = run_import("""
const text = fs.readFileSync('tests/fixtures/stakeout_export_sample.geojson', 'utf8');
const preview = normalizeGeoJson(text, {name: 'stakeout_export_sample.geojson', size: Buffer.byteLength(text)});
const pkg = buildImportPackage(preview, stakeoutDefaultSelection(preview), 'PROJECT_ID', 2000);
out.targets = pkg.targets.map(t => [t.name, t.lat_e8, t.lon_e8, t.alt_import]);
out.kinds = [...new Set(pkg.parts.map(part => part.kind))];
""")
        self.assertEqual(out["targets"], expected)
        self.assertEqual(out["kinds"], ["point"])



class TestStakeoutProgressRecovery(unittest.TestCase):
    """Codex review P2 (WP4 B): a progress list that could not be read stays fail-closed but can be loaded again."""

    def test_failed_list_offers_a_reload_in_the_dock_under_node(self):
        out = run_actions("""
const view = mkView({status: "open", ready: true, gen: null});
const first = stakeoutConfirmActions(view), again = stakeoutConfirmActions(view);
out.same = first.length === 1 && first[0] === again[0];
const notice = first[0];
out.notice = [notice.dataset.notice, notice.textContent.includes(dict.en["stakeout.progress_failed"])];
const button = notice.querySelector("button");
out.buttons = notice.querySelectorAll("button").map(node => [node.dataset.action, node.textContent]);
button.listeners.click[0](); out.disabled = button.disabled; await settle(); await settle();
out.reloads = reloads.length; out.enabled = !button.disabled;
out.label = dict.en["stakeout.retry_overview"];
""")
        self.assertTrue(out["same"])
        self.assertEqual(out["notice"], ["stakeout-progress", True])
        self.assertEqual(out["buttons"], [["reload-progress", out["label"]]])
        self.assertEqual((out["disabled"], out["reloads"], out["enabled"]), (True, 1, True))

    def test_overview_notice_only_while_the_list_failed_under_node(self):
        out = run_actions("""
const calls = []; const ctx = overview => ({overview, refreshOverview: async () => { calls.push(1); return true; }});
const base = {progress: {active_tid: null, damaged_lines: 0, write_uncertain: false}, sets: []};
const failed = stakeoutOverviewProgressNotice(ctx({...base, stakeout_progress: null}));
out.failed = [].concat(failed).map(node => node.dataset.notice);
out.loaded = [].concat(stakeoutOverviewProgressNotice(ctx({...base, stakeout_progress: {states: []}}))).length;
out.plain = [].concat(stakeoutOverviewProgressNotice(ctx({sets: []}))).length;
out.none = [].concat(stakeoutOverviewProgressNotice(ctx(null))).length;
[].concat(failed)[0].querySelector("button").listeners.click[0](); await settle(); out.calls = calls.length;
""")
        self.assertEqual(out["failed"], ["stakeout-progress"])
        self.assertEqual((out["loaded"], out["plain"], out["none"], out["calls"]), (0, 0, 0, 1))

    def test_watch_reloads_on_packets_only_while_failed_under_node(self):
        out = run_actions("""
const fns = []; let hidden = false, pending = null; const calls = [];
const ctx = {signal: new AbortController().signal, overview: {progress: {}, stakeout_progress: {states: []}},
  subscribe: fn => { fns.push(fn); return () => {}; },
  refreshOverview: () => { calls.push(1); return new Promise(resolve => { pending = resolve; }); }};
stakeoutWatchProgress(ctx, {hidden: () => hidden});
const frame = {packet: {}, rejected: null}, emit = (now, reason, f) => fns.forEach(fn => fn(f === undefined ? frame : f, now, reason || "packet"));
emit(1000); out.healthy = calls.length;
ctx.overview = {progress: {}, stakeout_progress: null};
emit(2000, "tick"); emit(2000, "progress"); emit(2000, "packet", {packet: {}, rejected: "schema"}); emit(2000, "packet", null); out.other = calls.length;
hidden = true; emit(2100); out.hidden = calls.length; hidden = false;
emit(2200); out.first = calls.length;
emit(13000); out.busy = calls.length;
pending(true); await settle();
emit(9000 + 2200); out.throttled = calls.length;
emit(12200); out.second = calls.length;
pending(false); await settle(); ctx.overview = {progress: {}, stakeout_progress: {states: []}};
emit(30000); out.recovered = calls.length;
""")
        self.assertEqual((out["healthy"], out["other"], out["hidden"], out["first"]), (0, 0, 0, 1))
        self.assertEqual((out["busy"], out["throttled"], out["second"], out["recovered"]), (1, 1, 2, 2))

    def test_static_hooks_of_the_recovery(self):
        core = source_text("95-stakeout.js").split("\n")
        mount = next(line for line in core if line.startswith("async function mountStakeout("))
        self.assertEqual(mount.count("stakeoutWatchProgress(ctx);"), 1)
        fill = next(line for line in core if line.startswith("function fillStakeoutOverview("))
        self.assertEqual(fill.count("stakeoutOverviewProgressNotice(ctx)"), 1)
        out = run_core("out.keys = [dict.en['stakeout.progress_failed'], dict.de['stakeout.progress_failed']];")
        self.assertTrue(all(out["keys"]))
        self.assertNotEqual(out["keys"][0], out["keys"][1])



def run_context(body):
    """createStakeoutContext with the stubs of the WP3 view contract test and a fully steerable api.json."""
    return run_core("""
const waiting = []; globalThis.api = {json: path => new Promise((resolve, reject) => waiting.push({path, resolve, reject}))};
const until = async n => { for (let i = 0; i < 200 && waiting.length < n; i++) await new Promise(r => setTimeout(r, 0)); return waiting.length; };
let sets = ["so-0000000a", "so-0000000b"];
const body = () => ({sets: sets.map(id => ({set_id: id, state: "ok", targets: 1, progress: {confirmed: 0, verified: 0, manual: 0, skipped: 0}})),
  session: {target: null, orders: {}}, progress: {active_tid: null, damaged_lines: 0, write_uncertain: false}});
const list = {schema: 1, project_id: "p1", active_tid: null, write_uncertain: false, orders: {}, floors: {}, counts: {}, states: []};
const project = {id: "p1", name: "P"}, tracking = {active_project_id: "p1", projects: [project]};
const ctx = createStakeoutContext({params: {id: "p1"}}, routeController.signal, project, tracking, {kind: "overview", tid: null});
""" + body, stubs=GUIDE_STUBS.replace("const fixIsStale = () => false;\n", "") + HEADER_STUBS.replace('localText = text => text, ', "")
                    + "\n" + source_block("30-connection.js", "function updateGlobalStatus("),
                    extra=DOM_PREFIXES + tuple(p for p in HEADER_PREFIXES if p != "const store=") +
                    ("function runButtonAction(", "async function refreshSection(", "function sectionTarget("),
                    asynchronous=True)


class TestStakeoutFreshAfterWrite(unittest.TestCase):
    """Codex re-review P2 (WP4 B): a refresh after a change never takes a state that was requested before the change."""

    def test_delete_during_an_automatic_retry_ends_without_the_set_under_node(self):
        out = run_context("""
ctx.setOverview({...body(), stakeout_progress: null});
const automatic = ctx.refreshOverview();
await until(1); waiting[0].resolve(body());
await until(2); out.second = waiting[1].path;
sets = ["so-0000000b"];
const afterDelete = ctx.refreshOverview();
waiting[1].resolve(list);
await automatic;
await until(3); waiting[2].resolve(body()); await until(4); waiting[3].resolve(list);
out.result = await afterDelete;
out.sets = ctx.overview.sets.map(set => set.set_id); out.paths = waiting.map(item => item.path.split("?")[0]);
out.failed = stakeoutProgressFailed(ctx.overview);
""")
        self.assertEqual(out["second"], "/api/stakeout/progress?project_id=p1")
        self.assertEqual(out["sets"], ["so-0000000b"])
        self.assertEqual(out["paths"], ["/api/stakeout", "/api/stakeout/progress"] * 2)
        self.assertIs(out["result"], True)
        self.assertIs(out["failed"], False)

    def test_a_later_answer_is_never_overwritten_by_an_earlier_one_under_node(self):
        out = run_context("""
const doc = {set_id: "so-0000000a", targets: [{tid: "so-0000000a/1/1", part_id: "so-0000000a/1", name: "T"}], parts: [{part_id: "so-0000000a/1"}]};
ctx.setOverview({...body(), stakeout_progress: list});
const view = stakeoutTargetView(ctx, doc, doc.targets[0]);
const first = ctx.refreshOverview();
await until(1);
sets = ["so-0000000c"];
const second = view.reload(), third = ctx.refreshOverview();
waiting[0].resolve({...body(), sets: [{set_id: "so-old", state: "ok", targets: 1}]}); await until(2); waiting[1].resolve(list);
await first; out.between = ctx.overview.sets.map(set => set.set_id);
await until(3); waiting[2].resolve(body()); await until(4); waiting[3].resolve(list);
await Promise.all([second, third]);
await new Promise(r => setTimeout(r, 10));
out.sets = ctx.overview.sets.map(set => set.set_id); out.view = view.overview === ctx.overview; out.requests = waiting.length;
""")
        self.assertEqual(out["between"], ["so-old"])
        self.assertEqual(out["sets"], ["so-0000000c"])
        self.assertIs(out["view"], True)
        self.assertEqual(out["requests"], 4)



class TestStakeoutMapEntry(unittest.TestCase):
    """User decision 30.10.: a "Stakeout" overlay in the project map besides the entry below it."""

    def test_overlay_links_to_the_overview_under_node(self):
        out = run_core("""
for (const lang of ["en", "de"]) { RtkI18n.setLanguage(lang); const link = stakeoutMapEntry({id: "a/b", name: "P"});
  out[lang] = [link.localName, link.className, link.getAttribute("href"), link.textContent, dict[lang]["stakeout.entry"]]; }
""", stubs=MINI_DOM, extra=DOM_PREFIXES)
        for lang in ("en", "de"):
            tag, cls, href, text, label = out[lang]
            self.assertEqual((tag, href), ("a", "/#projects/a%2Fb/stakeout"))
            self.assertIn("map-stakeout", cls.split())
            self.assertIn("btn", cls.split())
            self.assertTrue(text.endswith(label), text)
        self.assertNotEqual(out["en"][3], out["de"][3])

    def test_one_overlay_node_per_mount_in_every_map_draw(self):
        detail = source_text("70-project-detail.js")
        mount = detail[detail.index("async function mountProjectDetail("):]
        self.assertEqual(mount.count("stakeoutMapEntry(project)"), 2)  # the canvas node (once per mount) and the skeleton
        self.assertIn("mapEntry=stakeoutMapEntry(project)", mount)
        draw = next(line for line in detail.split("\n") if line.strip().startswith("const drawMap="))
        self.assertIn("replace(canvas,mapGraphic,chip,tools,legend);canvas.append(mapEntry);", draw)
        self.assertNotIn("stakeoutMapEntry(", draw)
        redraw = next(line for line in detail.split("\n") if line.strip().startswith("const redrawGraphic="))
        self.assertNotIn("mapEntry", redraw)
        # The page skeleton stays a direct child of main (its cancel button depends on that); the overlay follows it.
        self.assertIn('replace(main,skeleton(tr("ui.loading_project_map","Loading project map (up to 500 points)")),stakeoutMapEntry(project))', mount)


if __name__ == "__main__":
    unittest.main()
