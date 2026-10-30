# SPDX-License-Identifier: AGPL-3.0-only
"""Stakeout WP4 export (GET|HEAD /api/stakeout/export): GeoJSON and CSV of the
targets of the visible sets with their status and observations, streamed as a
snapshot of its start (R9) that releases its hold on every exit (R10).

The fixture tests/fixtures/stakeout_export_sample.geojson is the export of the
WP1 sample set in a fixed environment (sample_export); write it again with
`python3 tests/test_stakeout_export.py --write-fixture`.
"""
import ast
import asyncio
import csv
import decimal
import importlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock
from urllib.parse import urlencode

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import support  # noqa: F401  (installs the MicroPython stubs)
import geo
import push
import stakeout
import stakeout_progress
import tracking
import web
import web_routing
from test_access_v11 import FakeHttpWriter, FakeReader
from test_http import _real_open
from test_stakeout_confirm import CMD, NDJSON, PROGRESS, ReopenFixture
from test_stakeout_session_http import SET_ID, TID, TRACKING
from test_stakeout_store import compact, make_package, sample_package

EXPORT = "/api/stakeout/export"
STAKEOUT = "/api/stakeout"
FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures",
                       "stakeout_export_sample.geojson")
CSV_HEADER = ["tid", "set_id", "part_id", "seq", "gen", "name", "target_longitude",
              "target_latitude", "target_altitude_m", "status", "mode", "source",
              "confirmed_at", "confirmed_at_utc", "clock_unsynced", "actual_longitude",
              "actual_latitude", "actual_altitude_msl_m", "actual_quality", "dn_m", "de_m",
              "dist_m", "tolerance_m", "point_id", "fix_quality", "samples",
              "gst_horizontal_sigma_max_m", "horizontal_spread_max_m", "correction_age_sec",
              "note"]
OBSERVATION_KEYS = {"point_id", "recorded_at", "lat_e8", "lon_e8", "alt_mm", "dn_m", "de_m",
                    "dist_m", "tolerance_m", "accepted", "fix_quality", "samples",
                    "gst_h_sigma_max_m", "spread_max_m", "correction_age_sec", "note"}
PROPERTY_KEYS = {"tid", "set_id", "part_id", "seq", "name", "props", "alt_import", "status",
                 "mode", "gen", "source", "observation"}
FEATURE = b'"type": "Feature"'
_PARTS_HEAD = b'"parts": [\n'
_TARGETS_HEAD = b'\n],\n"targets": [\n'


def export_module():
    return importlib.import_module("stakeout_export")


def decimals(payload):
    return json.loads(payload, parse_float=decimal.Decimal)


def e8(value):
    """The int e8 of a Decimal coordinate text; it must be exact."""
    scaled = value * 10 ** 8
    assert scaled == int(scaled), value
    return int(scaled)


def file_doc(path):
    """The set file (layout 2) as one JSON document (body and trailer)."""
    with _real_open(path, "rb") as source:
        return json.loads(source.read())


def file_part_lines(path):
    """The part lines of a set file, each parsed."""
    with _real_open(path, "rb") as source:
        data = source.read()
    begin = data.index(_PARTS_HEAD) + len(_PARTS_HEAD)
    return [json.loads(line) for line in data[begin:data.index(_TARGETS_HEAD)].split(b",\n")]


def stakeout_point(point_id, tid, gen=0, mode="manual", accepted=False):
    """A valid stakeout point as the survey keeps it (no tracker needed)."""
    return {"id": point_id, "object_type": "stakeout", "name": "Target", "note": "found",
            "line_id": None, "chainage_m": None, "lateral_offset_m": None, "side": None,
            "projected_coordinate": None, "coordinate": [1000000123, 5100000456, 412345],
            "stakeout_tid": tid, "stakeout_gen": gen, "stakeout_cmd": CMD,
            "stakeout_tolerance_m": None, "stakeout_dn_m": -0.012, "stakeout_de_m": 0.034,
            "stakeout_dist_m": 0.036,
            "measurement": {"source": "stakeout_" + mode, "recorded_at": 815000000,
                            "fix_quality": 4, "correction_age_sec": 1.0,
                            "quality": {"accepted": accepted, "reasons": []},
                            "accuracy_observation": {"samples": 5,
                                                     "horizontal_max_deviation_m": 0.008},
                            "receiver_accuracy": {"horizontal_sigma_max_m": 0.011}}}


FIXED_NOW = 815000123
FIXED_PROJECT = "project-00000001-0001"
FIXED_FINGERPRINT = 815000000


def sample_export():
    """The GeoJSON export of the WP1 sample set in a fixed environment: the
    clock stands, target 2 of part 3 is skipped and target 1 of part 2 has a
    manual stakeout point."""
    module = export_module()
    with tempfile.TemporaryDirectory(prefix="stakeout-export-") as root, \
            mock.patch.object(stakeout, "now_seconds", return_value=FIXED_NOW), \
            mock.patch.object(stakeout, "log"):
        store = stakeout.TargetStore(os.path.join(root, "stakeout"))
        answer = asyncio.run(store.import_package(
            compact(sample_package(FIXED_PROJECT)), lambda project_id: FIXED_FINGERPRINT,
            lambda size: True))
        set_id = answer["set_id"]
        fold = stakeout_progress.Fold(FIXED_PROJECT, FIXED_FINGERPRINT)
        fold.apply({"ev": "skip", "tid": set_id + "/3/2", "gen": 0})
        points = [stakeout_point("point-0000000a-0001", set_id + "/2/1")]
        sets = asyncio.run(module.readable(store, FIXED_PROJECT, FIXED_FINGERPRINT))
        snapshot = module.Snapshot(fold, points)
        return "".join(module.iter_geojson(store, snapshot,
                                           {"id": FIXED_PROJECT, "name": "Sample"}, sets))


def write_fixture(path=FIXTURE):
    with open(path, "w", encoding="utf-8", newline="") as target:
        target.write(sample_export())


def normalized_package(doc, project_id):
    """The import package (WP1 contract) of the Point features of an export:
    one point part and one target per feature, e8 and mm from the coordinate
    text by integer arithmetic."""
    parts, targets = [], []
    for index, feature in enumerate(doc["features"]):
        coordinates = feature["geometry"]["coordinates"]
        properties = feature["properties"]
        alt = coordinates[2] if len(coordinates) == 3 else None
        parts.append({"kind": "point", "name": properties["name"], "feature_index": index,
                      "geometry_index": 0, "coords_total": 1, "coords_thinned": False,
                      "coords_e8": []})
        targets.append({"part_index": index, "coord_index": 0, "name": properties["name"],
                        "lat_e8": e8(coordinates[1]), "lon_e8": e8(coordinates[0]),
                        "alt_import": None if alt is None else int(alt * 1000),
                        "props": properties["props"]})
    return {"schema": 1, "project_id": project_id,
            "source": {"filename": "export.geojson", "bytes": 0, "features": len(parts),
                       "skipped": [], "skipped_total": 0},
            "parts": parts, "targets": targets}


class GatedWriter(FakeHttpWriter):
    """A client whose drain stops, once marker was written, until gate is set."""

    def __init__(self, marker):
        super().__init__()
        self.marker = marker
        self.stopped = asyncio.Event()
        self.gate = asyncio.Event()
        self.at_stop = b""

    async def drain(self):
        if not self.gate.is_set() and self.marker in self.data:
            self.at_stop = bytes(self.data)
            self.stopped.set()
            await self.gate.wait()


class FailingWriter(FakeHttpWriter):
    """A client whose write number fail_at raises error."""

    def __init__(self, fail_at, error):
        super().__init__()
        self.fail_at = fail_at
        self.error = error
        self.attempts = 0

    def write(self, data):
        self.attempts += 1
        if self.attempts >= self.fail_at:
            raise self.error
        super().write(data)


class ExportFixture(ReopenFixture):
    """The reopen fixture (sample set in the active project, confirmations,
    undo and restore) with the export route."""

    def setUp(self):
        super().setUp()
        self.module = export_module()

    def export(self, method="GET", **params):
        """(status, head, body bytes) of one export request."""
        return self.call(method, EXPORT + ("?" + urlencode(params) if params else ""))

    def geojson(self, **params):
        status, head, payload = self.export(**params)
        self.assertEqual(status, 200, payload)
        self.assertIn(b"Content-Type: application/geo+json", head)
        return decimals(payload)

    def table(self, **params):
        params["format"] = "csv"
        status, head, payload = self.export(**params)
        self.assertEqual(status, 200, payload)
        self.assertIn(b"Content-Type: text/csv; charset=utf-8", head)
        return list(csv.reader(io.StringIO(payload.decode("utf-8"), newline="")))

    @staticmethod
    def by_tid(doc):
        return dict((feature["id"], feature) for feature in doc["features"])

    @staticmethod
    def rows(table):
        return dict((row[0], dict(zip(CSV_HEADER, row))) for row in table[1:])

    def readers(self):
        return self.store.readers(self.pid)

    async def fetch(self, method, path, writer=None):
        """(status, body bytes) of one request through the dispatcher."""
        writer = writer or FakeHttpWriter()
        await web.handle_http_client(FakeReader(self.raw_request(method, path)), writer)
        head, _, payload = bytes(writer.data).partition(b"\r\n\r\n")
        return int(head.split(b" ", 2)[1]), payload

    def gated(self, path, during, marker=FEATURE):
        """(answer bytes, bytes at the stop, during's result) of GET path whose
        client stops taking data once marker was written, while during() (a
        coroutine function) runs."""
        async def scenario():
            writer = GatedWriter(marker)
            task = asyncio.ensure_future(self.fetch("GET", path, writer))
            await asyncio.wait_for(writer.stopped.wait(), 5)
            try:
                result = await during()
            finally:
                writer.gate.set()
            await asyncio.wait_for(task, 10)
            return bytes(writer.data), writer.at_stop, result

        return asyncio.run(scenario())

    @staticmethod
    def body(data):
        return data.partition(b"\r\n\r\n")[2]

    def export_logs(self):
        return [call.args[2] for call in web.log.call_args_list
                if call.args[:2] == ("INFO", "EXPORT")]


class TestExportShape(ExportFixture, unittest.TestCase):
    def test_geojson_has_one_point_per_target_and_the_source_parts(self):
        doc = self.geojson()
        stored = file_doc(self.path())
        self.assertEqual((doc["type"], doc["kind"], doc["schema"], doc["project_id"]),
                         ("FeatureCollection", "stakeout_export", 1, self.pid))
        self.assertEqual(doc["name"], "Stakeout")
        self.assertIs(type(doc["exported_at"]), int)
        self.assertEqual(len(doc["features"]), 12)
        self.assertEqual([feature["id"] for feature in doc["features"]],
                         [target["tid"] for target in stored["targets"]])
        summary = stored["summary"]
        self.assertEqual(doc["stakeout_sets"], [{
            "set_id": SET_ID, "state": "ok", "imported_at": summary["imported_at"],
            "source": summary["source"], "parts": file_part_lines(self.path())}])
        self.assertEqual(doc["stakeout_sets"][0]["parts"], stored["parts"])
        for feature, target in zip(doc["features"], stored["targets"]):
            with self.subTest(tid=target["tid"]):
                self.assertEqual(feature["type"], "Feature")
                self.assertEqual(feature["geometry"]["type"], "Point")
                coordinates = feature["geometry"]["coordinates"]
                self.assertEqual((e8(coordinates[0]), e8(coordinates[1])),
                                 (target["lon_e8"], target["lat_e8"]))
                if target["alt_import"] is None:
                    self.assertEqual(len(coordinates), 2)
                else:
                    self.assertEqual(coordinates[2] * 1000, target["alt_import"])
                properties = feature["properties"]
                self.assertEqual(set(properties), PROPERTY_KEYS)
                self.assertEqual(
                    [properties[key] for key in ("tid", "set_id", "part_id", "seq", "name",
                                                 "props", "alt_import")],
                    [target["tid"], SET_ID, target["part_id"], target["seq"], target["name"],
                     target["props"], target["alt_import"]])
                self.assertEqual([properties[key] for key in ("status", "mode", "gen",
                                                              "source", "observation")],
                                 ["open", None, 0, None, None])
        # The coordinate text is geo.format_e8 / format_mm exactly.
        payload = self.export()[2]
        target = stored["targets"][4]
        self.assertIn(("[%s,%s,%s]" % (geo.format_e8(target["lon_e8"]),
                                       geo.format_e8(target["lat_e8"]),
                                       geo.format_mm(target["alt_import"]))).encode(), payload)

    def test_part_lines_survive_any_block_cut(self):
        # A device writes names as raw UTF-8; any read block may cut a character.
        package = make_package(targets=0, lines=2, coords_per_line=40, project_id=self.pid,
                               offset=9)
        package["parts"][1]["name"] = "Line \u03b1\u03b2\u03b3 \u00b0" * 5
        package["targets"][1]["name"] = "Target \u03b1"
        with mock.patch.object(stakeout.ujson, "dumps",
                               lambda value: json.dumps(value, ensure_ascii=False)):
            status, answer = self.import_(package)
        self.assertEqual(status, 200, answer)
        set_id = answer["set_id"]
        self.assertIn("\u03b1".encode(), self.file_bytes(self.path(set_id)))
        expected = file_part_lines(self.path(set_id))
        for piece in (1, 2, 3, 7, 2048):
            with self.subTest(piece=piece), mock.patch.object(stakeout, "SCAN_PIECE", piece):
                status, _head, payload = self.export(set_id=set_id)
                self.assertEqual(status, 200)
                doc = json.loads(payload.decode("utf-8"))
                self.assertEqual(doc["stakeout_sets"][0]["parts"], expected)
                self.assertEqual(doc["features"][1]["properties"]["name"], "Target \u03b1")

    def test_observations_come_from_the_points(self):
        tids = [feature["id"] for feature in self.geojson()["features"]]
        manual, skipped, reopened = tids[0], tids[1], tids[2]
        self.ready()
        status, verified = self.confirm_target("verified")
        self.assertEqual(status, 200, verified)
        found = self.found(manual)
        self.assertEqual(self.skip(skipped)[0], 200)
        old = self.found(reopened)
        self.assertEqual(self.reopen(reopened)[0], 200)
        features = self.by_tid(self.geojson())
        points = dict((point["id"], point) for point in self.points())
        cases = ((TID, "confirmed", "verified", 0, verified["point_id"]),
                 (manual, "confirmed", "manual", 0, found["point_id"]),
                 (skipped, "skipped", None, 0, None),
                 (reopened, "open", None, 1, None))
        for tid, status, mode, gen, point_id in cases:
            with self.subTest(tid=tid):
                properties = features[tid]["properties"]
                self.assertEqual([properties[key] for key in ("status", "mode", "gen")],
                                 [status, mode, gen])
                self.assertEqual(properties["source"], mode and "stakeout_" + mode)
                observation = properties["observation"]
                if point_id is None:
                    self.assertIsNone(observation)
                    continue
                point = points[point_id]
                measurement = point["measurement"]
                self.assertEqual(set(observation), OBSERVATION_KEYS)
                self.assertEqual(observation, {
                    "point_id": point_id, "recorded_at": measurement["recorded_at"],
                    "lat_e8": point["coordinate"][1], "lon_e8": point["coordinate"][0],
                    "alt_mm": point["coordinate"][2],
                    "dn_m": decimal.Decimal(repr(point["stakeout_dn_m"])),
                    "de_m": decimal.Decimal(repr(point["stakeout_de_m"])),
                    "dist_m": decimal.Decimal(repr(point["stakeout_dist_m"])),
                    "tolerance_m": (None if point["stakeout_tolerance_m"] is None else
                                    decimal.Decimal(repr(point["stakeout_tolerance_m"]))),
                    "accepted": measurement["quality"]["accepted"],
                    "fix_quality": measurement["fix_quality"],
                    "samples": tracking._sample_counts(measurement)[0],
                    "gst_h_sigma_max_m": observation["gst_h_sigma_max_m"],
                    "spread_max_m": observation["spread_max_m"],
                    "correction_age_sec": observation["correction_age_sec"],
                    "note": point["note"]})
        # The old point of the reopened target is history, not its observation.
        self.assertIn(old["point_id"], points)

    def test_observation_fields_of_a_stored_point(self):
        module = self.module
        point = stakeout_point("point-1", TID)
        snapshot = module.Snapshot(stakeout_progress.Fold(self.pid, self.fp()), [point])
        status, mode, gen, counted = snapshot.state(TID)
        self.assertEqual((status, mode, gen, counted), ("confirmed", "manual", 0, point))
        self.assertEqual(module.observation(point), {
            "point_id": "point-1", "recorded_at": 815000000, "lat_e8": 5100000456,
            "lon_e8": 1000000123, "alt_mm": 412345, "dn_m": -0.012, "de_m": 0.034,
            "dist_m": 0.036, "tolerance_m": None, "accepted": False, "fix_quality": 4,
            "samples": 5, "gst_h_sigma_max_m": 0.011, "spread_max_m": 0.008,
            "correction_age_sec": 1.0, "note": "found"})
        self.assertIsNone(module.observation(None))

    def test_set_filter(self):
        second = self.second_set()
        doc = self.geojson()
        self.assertEqual([item["set_id"] for item in doc["stakeout_sets"]],
                         [item["set_id"] for item in self.overview()[1]["sets"]])
        self.assertEqual(len(doc["features"]), 15)
        status, head, payload = self.export(set_id=second)
        self.assertEqual(status, 200, payload)
        self.assertIn(("Content-Disposition: attachment; filename=stakeout-%s-%s.geojson"
                       % (self.pid, second)).encode(), head)
        doc = decimals(payload)
        self.assertEqual([item["set_id"] for item in doc["stakeout_sets"]], [second])
        self.assertEqual([feature["properties"]["set_id"] for feature in doc["features"]],
                         [second] * 3)
        status, head, payload = self.export(format="csv", set_id=second)
        self.assertEqual(status, 200, payload)
        self.assertIn(("filename=stakeout-%s-%s.csv" % (self.pid, second)).encode(), head)
        self.assertEqual(len(self.table(set_id=second)), 4)
        status, head, payload = self.export()
        self.assertTrue(head.endswith(("filename=stakeout-%s.geojson" % self.pid).encode()),
                        head)
        for form in ("geojson", "csv"):
            with self.subTest(format=form):
                status, _head, payload = self.export(format=form, set_id="so-00000000")
                self.assert_error((status, json.loads(payload)), 404, "set_not_found")
                status, _head, payload = self.export(format=form, set_id="x")
                self.assert_error((status, json.loads(payload)), 400, "invalid_request")
        self.assertEqual(self.readers(), 0)

    def test_reexport_reimports_with_identical_coordinates(self):
        # Test case 5, device side: export, normalize, import: the same targets.
        doc = self.geojson(set_id=SET_ID)
        stored = file_doc(self.path())["targets"]
        plan = asyncio.run(stakeout.validate_import(normalized_package(doc, self.pid)))
        self.assertEqual(len(plan["targets"]), len(stored))
        for again, target in zip(plan["targets"], stored):
            with self.subTest(tid=target["tid"]):
                self.assertEqual((again["lat_e8"], again["lon_e8"], again["alt_import"]),
                                 (target["lat_e8"], target["lon_e8"], target["alt_import"]))
        # The checked-in fixture (B4 normalizes it in the browser) is current.
        with open(FIXTURE, encoding="utf-8", newline="") as source:
            fixture = source.read()
        self.assertEqual(fixture, sample_export(),
                         "write it again: python3 tests/test_stakeout_export.py "
                         "--write-fixture")
        doc = decimals(fixture)
        plan = asyncio.run(stakeout.validate_import(normalized_package(doc, FIXED_PROJECT)))
        self.assertEqual([(target["lat_e8"], target["lon_e8"], target["alt_import"])
                          for target in plan["targets"]],
                         [(target["lat_e8"], target["lon_e8"], target["alt_import"])
                          for target in sample_package()["targets"]])
        statuses = [feature["properties"]["status"] for feature in doc["features"]]
        self.assertEqual((statuses.count("confirmed"), statuses.count("skipped")), (1, 1))

    def test_csv_rows_and_formula_guard(self):
        package = make_package(targets=2, project_id=self.pid, offset=5)
        package["targets"][0].update(name="=cmd", lon_e8=-100000000, alt_import=-1500)
        status, answer = self.import_(package)
        self.assertEqual(status, 200, answer)
        guarded = answer["set_id"]
        table = self.table(set_id=guarded)
        self.assertEqual(table[0], CSV_HEADER)
        self.assertEqual(len(table), 3)
        first = dict(zip(CSV_HEADER, table[1]))
        self.assertEqual(first["name"], "'=cmd")
        self.assertEqual(first["target_longitude"], "-1.00000000")
        self.assertEqual(first["target_latitude"], geo.format_e8(5100000005))
        self.assertEqual(first["target_altitude_m"], "-1.500")
        self.assertEqual((first["status"], first["gen"], first["mode"], first["point_id"]),
                         ("open", "0", "", ""))
        self.assertEqual(dict(zip(CSV_HEADER, table[2]))["target_altitude_m"], "")
        # Every cell is quoted; the header line and each row end with CRLF.
        payload = self.export(format="csv", set_id=guarded)[2].decode()
        self.assertTrue(payload.startswith(",".join(CSV_HEADER) + "\r\n"), payload[:200])
        self.assertIn('"\'=cmd"', payload)
        self.assertIn('"-1.00000000"', payload)
        self.assertTrue(payload.endswith('"\r\n'))
        # Observations: one verified, one manual of each gate verdict.
        tids = [row[0] for row in self.table(set_id=SET_ID)[1:]]
        self.ready()
        status, verified = self.confirm_target("verified")
        self.assertEqual(status, 200, verified)
        good = self.found(tids[0])
        self.fresh_fix(qual=1)
        status, poor = self.confirm_target("manual", tids[1])
        self.assertEqual(status, 200, poor)
        rows = self.rows(self.table())
        points = dict((point["id"], point) for point in self.points())
        for answer, mode in ((verified, "verified"), (good, "manual"), (poor, "manual")):
            with self.subTest(mode=mode, point=answer["point_id"]):
                row = rows[answer["tid"]]
                point = points[answer["point_id"]]
                measurement = point["measurement"]
                accepted = measurement["quality"]["accepted"]
                utc, unsynced = tracking._utc_fields(measurement["recorded_at"])
                self.assertEqual(
                    [row[key] for key in ("status", "mode", "source", "point_id",
                                          "confirmed_at", "confirmed_at_utc",
                                          "clock_unsynced", "actual_quality")],
                    ["confirmed", mode, "stakeout_" + mode, answer["point_id"],
                     str(measurement["recorded_at"]), utc or "",
                     "true" if unsynced else "false",
                     "verified" if mode == "verified" else
                     "gate_accepted" if accepted else "not_verified"])
                self.assertEqual(
                    [row[key] for key in ("actual_longitude", "actual_latitude",
                                          "actual_altitude_msl_m")],
                    [geo.format_e8(point["coordinate"][0]),
                     geo.format_e8(point["coordinate"][1]),
                     geo.format_mm(point["coordinate"][2])])
                self.assertEqual(row["dn_m"], str(point["stakeout_dn_m"]))
                self.assertEqual(row["samples"],
                                 str(tracking._sample_counts(measurement)[0]))
        self.assertEqual(rows[poor["tid"]]["actual_quality"], "not_verified")
        self.assertEqual(rows[tids[2]]["confirmed_at"], "")
        self.assertEqual(rows[tids[2]]["clock_unsynced"], "")

    def test_damaged_set_is_listed_without_targets(self):
        second = self.second_set()
        self.rewrite(lambda data: data[:-10] + b"x" * 10)
        self.cold()
        doc = self.geojson()
        damaged, = [item for item in doc["stakeout_sets"] if item["set_id"] == SET_ID]
        self.assertEqual(damaged, {"set_id": SET_ID, "state": "damaged", "source": None,
                                   "imported_at": None, "parts": []})
        self.assertEqual(set(feature["properties"]["set_id"] for feature in doc["features"]),
                         {second})
        self.assertEqual(len(self.table()), 4)
        self.assertEqual(self.readers(), 0)

    def test_digest_mismatch_breaks_the_stream(self):
        stored = file_doc(self.path())
        name = stored["targets"][3]["name"].encode()
        self.rewrite(lambda data: data.replace(name, name[:-1] + b"9"))
        for form in ("geojson", "csv"):
            with self.subTest(format=form):
                self.cold()
                stakeout.log.reset_mock()
                status, _head, payload = self.export(format=form)
                self.assertEqual(status, 200)
                self.assertFalse(payload.endswith(b"]}"), payload[-40:])
                self.assertTrue(self.store.is_damaged(self.pid, SET_ID))
                self.assertTrue(self.logged(stakeout, "ERROR"))
                self.assertEqual(self.readers(), 0)
        self.assertEqual(self.overview()[1]["sets"][0]["state"], "damaged")

    def test_export_route_policy(self):
        self.assertEqual(web_routing.ROUTES[EXPORT], (("GET", "HEAD"), "GET required", 4096))
        status, _head, payload = self.call("GET", EXPORT, cookie=False)
        self.assertEqual(status, 401, payload)
        status, _head, payload = self.call("POST", EXPORT, b"{}")
        self.assertEqual(status, 405, payload)
        status, _head, payload = self.export(format="xml")
        self.assert_error((status, json.loads(payload)), 400, "invalid_request")
        status, _head, payload = self.export(project_id="p-unknown")
        self.assert_error((status, json.loads(payload)), 404, "project_not_found")
        status, head, payload = self.export("HEAD")
        self.assertEqual((status, payload), (200, b""))
        self.assertIn(b"Content-Type: application/geo+json", head)
        # Another project than the active one, by project_id.
        other = self.other_project()
        self.assertEqual(self.import_other(other)[0], 200)
        doc = self.geojson(project_id=other)
        self.assertEqual((doc["project_id"], len(doc["features"])), (other, 2))
        self.assertIsNone(self.fold(other))

    def test_export_module_is_lazy_and_a_board_file(self):
        self.assertIn("stakeout_export.py", push.BOARD_FILES)
        self.assertEqual(push.BOARD_FILES.index("stakeout_export.py"),
                         push.BOARD_FILES.index("stakeout_progress.py") + 1)
        with open(web.__file__, encoding="utf-8") as source:
            tree = ast.parse(source.read())
        owners = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Import) and any(
                            alias.name == "stakeout_export" for alias in inner.names):
                        owners.append(node.name)
        self.assertEqual(owners, ["_stakeout_export"])
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                self.assertNotIn("stakeout_export",
                                 [alias.name for alias in node.names] +
                                 [getattr(node, "module", None)])


class TestExportSnapshot(ExportFixture, unittest.TestCase):
    def test_export_is_a_snapshot_of_its_start(self):
        raw = self.ndjson()  # an older backup: no stakeout point yet
        tids = [feature["id"] for feature in self.geojson()["features"]]
        five, seven, nine = tids[4], tids[6], tids[8]
        self.assertEqual(five, TID)
        seven_point = self.found(seven)
        five_point = self.found(five)

        async def during():
            answers = {"readers": self.readers()}
            answers["undo"] = await self.dispatch(TRACKING, {"action": "undo"})
            answers["reopen"] = await self.dispatch(STAKEOUT, self.action_body("reopen", seven))
            answers["skip"] = await self.dispatch(STAKEOUT, self.action_body("skip", nine))
            self.progress.forget_all()
            answers["reload"] = await self.fetch("GET", PROGRESS)
            with mock.patch.object(stakeout_progress, "COMPACT_BYTES", 64):
                self.progress.forget_all()
                answers["compacted"] = await self.fetch("GET", PROGRESS)
            answers["compaction"] = self.progress.last_compaction
            answers["restore"] = await self.dispatch("/api/tracking/import", raw, NDJSON)
            answers["after"] = await self.fetch("GET", PROGRESS)
            return answers

        data, at_stop, answers = self.gated(EXPORT, during)
        # The stream stood after its first feature, before target 5.
        self.assertIn(FEATURE, at_stop)
        self.assertNotIn(('"id": "%s"' % five).encode(), at_stop)
        self.assertEqual(answers["readers"], 1)
        for name in ("undo", "reopen", "skip", "restore"):
            self.assertEqual(answers[name][0], 200, (name, answers[name]))
        self.assertIsNotNone(answers["compaction"])
        # The changes took effect.
        status, compacted = answers["compacted"]
        self.assertEqual(status, 200)
        states = dict((row[0], row) for row in json.loads(compacted)["states"])
        self.assertEqual(states[five], [five, "open", 1, None, None])
        self.assertEqual(states[seven], [seven, "open", 1, None, None])
        self.assertEqual(states[nine], [nine, "skipped", 0, None, None])
        status, after = answers["after"]
        self.assertEqual((status, json.loads(after)["floors"], json.loads(after)["states"]),
                         (200, {SET_ID: 2}, []))
        self.assertEqual(self.points(), [])
        # The export shows the moment it began.
        self.assertTrue(data.startswith(b"HTTP/1.0 200 OK"), data[:80])
        features = self.by_tid(decimals(self.body(data)))
        self.assertEqual(len(features), 12)
        properties = features[five]["properties"]
        self.assertEqual([properties[key] for key in ("status", "mode", "gen", "source")],
                         ["confirmed", "manual", 0, "stakeout_manual"])
        self.assertEqual(properties["observation"]["point_id"], five_point["point_id"])
        properties = features[seven]["properties"]
        self.assertEqual([properties[key] for key in ("status", "gen")], ["confirmed", 0])
        self.assertEqual(properties["observation"]["point_id"], seven_point["point_id"])
        properties = features[nine]["properties"]
        self.assertEqual([properties[key] for key in ("status", "gen", "observation")],
                         ["open", 0, None])
        self.assertEqual(self.readers(), 0)

    def test_csv_export_is_a_snapshot_of_its_start(self):
        five_point = self.found()

        async def during():
            return await self.dispatch(TRACKING, {"action": "undo"})

        # The stream stands after the header line, before any row.
        data, at_stop, undone = self.gated(EXPORT + "?format=csv", during, marker=b"\r\n")
        self.assertEqual(undone[0], 200, undone)
        self.assertNotIn(TID.encode(), at_stop)
        self.assertEqual(self.row(), [TID, "open", 1, None, None])
        table = list(csv.reader(io.StringIO(self.body(data).decode(), newline="")))
        row = self.rows(table)[TID]
        self.assertEqual((row["status"], row["mode"], row["gen"], row["point_id"]),
                         ("confirmed", "manual", "0", five_point["point_id"]))
        self.assertEqual(self.readers(), 0)

    def test_confirm_during_export_is_allowed(self):
        async def during():
            self.fresh_fix()
            return await self.dispatch(STAKEOUT, self.action_body("confirm", mode="manual"))

        data, _at_stop, confirmed = self.gated(EXPORT, during)
        self.assertEqual(confirmed[0], 200, confirmed)
        self.assertEqual(self.row(), [TID, "confirmed", 0, "manual",
                                      confirmed[1]["point_id"]])
        properties = self.by_tid(decimals(self.body(data)))[TID]["properties"]
        self.assertEqual((properties["status"], properties["observation"]), ("open", None))

    def test_snapshot_is_one_bounded_block(self):
        module = self.module
        set_id = "so-0000000a"
        fold = stakeout_progress.Fold(self.pid, self.fp())
        tids = ["%s/%d/%d" % (set_id, part, seq) for part in range(1, 5)
                for seq in range(1, 501)]
        for tid in tids:
            fold.apply({"ev": "skip", "tid": tid, "gen": 1})
        points = [stakeout_point("point-%d" % index, tid, 2)
                  for index, tid in enumerate(tids)]
        points += [dict(stakeout_point("plain-%d" % index, tids[0]), object_type="valve")
                   for index in range(3000)]
        self.assertEqual(len(points), 5000)
        started = time.perf_counter()
        snapshot = module.Snapshot(fold, points)
        spent = time.perf_counter() - started
        self.assertLess(spent, 0.25)
        for tid in tids[::97]:
            self.assertEqual(snapshot.state(tid), fold.state(tid, points))
        # Later changes of the fold and of the points do not reach the snapshot.
        before = snapshot.state(tids[5])
        fold.apply({"ev": "reopen", "tid": tids[5], "gen": 7})
        fold.apply({"ev": "restored", "floor": 9})
        points.append(stakeout_point("point-late", tids[5], 9))
        fold.note_point(points[-1], points)
        self.assertEqual(fold.state(tids[5], points)[:3], ("confirmed", "manual", 9))
        self.assertEqual(snapshot.state(tids[5]), before)
        self.assertEqual(before[:3], ("confirmed", "manual", 2))
        self.assertIs(type(snapshot.exported_at), int)


class TestExportRelease(ExportFixture, unittest.TestCase):
    def test_export_holds_and_releases(self):
        async def during():
            readers = self.readers()
            deleted = await self.dispatch(STAKEOUT, {"action": "delete_set", "set_id": SET_ID,
                                                    "expected_confirmed": 0})
            try:
                self.store.begin_project_delete(self.pid)
                code = None
            except stakeout.StakeoutError as error:
                code = error.code
            return readers, deleted, code

        data, _at_stop, (readers, deleted, code) = self.gated(EXPORT, during)
        self.assertEqual(readers, 1)
        self.assert_error(deleted, 409, "storage_busy")
        self.assertEqual(code, "storage_busy")
        self.assertEqual(len(decimals(self.body(data))["features"]), 12)
        self.assertEqual(self.readers(), 0)
        self.assertTrue(self.export_logs()[-1].startswith("stakeout geojson complete "),
                        self.export_logs())
        # Aborts: the client goes away, the drain deadline, HEAD.
        for form in ("geojson", "csv"):
            with self.subTest(abort="client", format=form):
                writer = FailingWriter(3, ConnectionResetError(104, "reset"))
                asyncio.run(self.fetch("GET", EXPORT + "?format=" + form, writer))
                self.assertEqual(self.readers(), 0)
                self.assertIn("stakeout %s interrupted" % form, self.export_logs()[-1])

        class Stalled(FakeHttpWriter):
            async def drain(self):
                if len(self.data) > 2000:
                    await asyncio.sleep(5)

        with self.subTest(abort="deadline"), \
                mock.patch.object(web, "EXPORT_DRAIN_TIMEOUT_SEC", 0.05):
            asyncio.run(self.fetch("GET", EXPORT, Stalled()))
            self.assertEqual(self.readers(), 0)
            self.assertIn("stakeout geojson interrupted TimeoutError", self.export_logs()[-1])
        with self.subTest(abort="head"):
            status, _head, payload = self.export("HEAD", format="csv")
            self.assertEqual((status, payload), (200, b""))
            self.assertEqual(self.readers(), 0)

    def test_release_on_every_failure(self):
        module = self.module
        # (1) The listing fails.
        with mock.patch.object(module, "readable", side_effect=OSError(5, "io")):
            status, _head, payload = self.export()
        self.assert_error((status, json.loads(payload)), 507, "stakeout_read_failed")
        self.assertEqual(self.readers(), 0)
        # (2) Making the generator fails: the route's failure answer.
        with mock.patch.object(module, "iter_geojson", side_effect=RuntimeError("make")):
            status, _head, payload = self.export()
        self.assert_error((status, json.loads(payload)), 500, "internal_error")
        self.assertEqual(self.readers(), 0)
        # (3) The generator's close raises: logged, still released.
        closed = []

        def broken(*args):
            try:
                yield "{"
                yield "}"
            finally:
                closed.append(True)
                raise RuntimeError("close")

        web.log.reset_mock()
        with mock.patch.object(module, "iter_geojson", broken):
            status, _head, payload = self.export("HEAD")
        self.assertEqual(status, 200)
        self.assertEqual(closed, [True])
        self.assertEqual(self.readers(), 0)
        self.assertTrue([call for call in web.log.call_args_list
                         if call.args[:2] == ("ERROR", "HTTP")], web.log.call_args_list)
        # (4) The first header write fails: the generator is closed.
        real = module.iter_geojson
        exits = []

        def observed(*args):
            try:
                yield from real(*args)
            except GeneratorExit:
                exits.append(True)
                raise

        with mock.patch.object(module, "iter_geojson", observed):
            self.handle(FailingWriter(1, OSError(104, "reset")), "GET", EXPORT)
        self.assertEqual(exits, [True])
        self.assertEqual(self.readers(), 0)
        # (5) The fold is cold while a change holds the lock: 503 stakeout_busy.
        self.progress.forget_all()

        async def locked():
            async with self.store.mutation():
                return await self.fetch("GET", EXPORT)

        status, payload = asyncio.run(locked())
        self.assert_error((status, json.loads(payload)), 503, "stakeout_busy")
        self.assertEqual(self.readers(), 0)

    def test_export_under_the_latch(self):
        self.store.uncertain = "test"
        self.assertEqual(len(self.geojson()["features"]), 12)
        self.assertEqual(len(self.table()), 13)
        self.assertEqual(self.readers(), 0)


class TestExportPacing(ExportFixture, unittest.TestCase):
    def paced(self, form, **params):
        """(answer body, events) of an export whose every set file read takes
        25 ms of the tick clock (a slow flash); events are ("read", time,
        size) and ("sleep", time) in order. Checks the pacing: a pause between
        any two reads, no stretch of 250 ms without one, the log line."""
        events = []
        clock = [time.monotonic() * 1000]
        real_open, sleep_ms = open, web.asyncio.sleep_ms

        class Slow:
            def __init__(self, handle):
                self.handle = handle

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.handle.close()
                return False

            def seek(self, at):
                self.handle.seek(at)

            def read(self, size):
                events.append(("read", time.perf_counter(), size))
                clock[0] += 25
                return self.handle.read(size)

        async def slept(ms):
            events.append(("sleep", time.perf_counter()))
            await sleep_ms(ms)

        with mock.patch("stakeout_export.open", lambda *args: Slow(real_open(*args)),
                        create=True), \
                mock.patch.object(web.time, "ticks_ms", lambda: int(clock[0])), \
                mock.patch.object(web.asyncio, "sleep_ms", slept):
            status, _head, payload = self.export(format=form, **params)
        self.assertEqual(status, 200)
        reads = [index for index, event in enumerate(events) if event[0] == "read"]
        self.assertTrue(all(event[2] <= stakeout.SCAN_PIECE for event in events
                            if event[0] == "read"))
        for first, second in zip(reads, reads[1:]):
            self.assertIn("sleep", [event[0] for event in events[first:second]])
        stamps = [event[1] for event in events[reads[0]:] if event[0] == "sleep"]
        self.assertLess(max(later - earlier for earlier, later in zip(stamps, stamps[1:])),
                        0.25)
        self.assertTrue(self.export_logs()[-1].startswith("stakeout %s complete " % form),
                        self.export_logs())
        return payload, events

    def test_export_paces(self):
        other = self.other_project()
        for number in range(4):
            status, answer = self.import_other(other, targets=500, offset=10 ** 6 * number)
            self.assertEqual(status, 200, answer)
        for form in ("geojson", "csv"):
            with self.subTest(format=form):
                payload, events = self.paced(form, project_id=other)
                self.assertGreater(len([event for event in events if event[0] == "read"]),
                                   100)
                if form == "geojson":
                    self.assertEqual(len(decimals(payload)["features"]), 2000)
                else:
                    self.assertEqual(payload.count(b"\r\n"), 2001)

    def test_long_lines_pace_too(self):
        # A line of 2000 stored vertices: its part line alone spans many blocks.
        lines = self.other_project("Lines")
        status, answer = self.import_other(lines, targets=0, lines=1, coords_per_line=2000)
        self.assertEqual(status, 200, answer)
        for form in ("geojson", "csv"):
            with self.subTest(format=form):
                _payload, events = self.paced(form, project_id=lines)
                self.assertGreater(len([event for event in events if event[0] == "read"]),
                                   20)
        # Target lines longer than a block (here: blocks of 64 bytes).
        with mock.patch.object(stakeout, "SCAN_PIECE", 64):
            for form in ("geojson", "csv"):
                with self.subTest(format=form, piece=64):
                    payload, _events = self.paced(form)
                    self.assertTrue(payload.endswith(b"]}" if form == "geojson" else b'"\r\n'))


if __name__ == "__main__":
    if sys.argv[1:] == ["--write-fixture"]:
        write_fixture()
    else:
        unittest.main()
