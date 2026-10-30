# SPDX-License-Identifier: AGPL-3.0-only
"""Stakeout progress (WP4): the field check of a stakeout point, the progress
log, the fold with target generations, the point index and the decision rule."""
import asyncio
import copy
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

import support  # noqa: F401 - installs the MicroPython compatibility modules
import stakeout
import stakeout_progress
from test_stakeout_session import device_bytes


def stakeout_point(mode="verified", **fields):
    """A valid stakeout point as add_asset stores it; fields override top-level keys."""
    point = {"id": "point-00000001-0001", "object_type": "stakeout", "name": "Line A / P001",
             "note": "", "line_id": None, "chainage_m": None, "lateral_offset_m": None,
             "side": None, "projected_coordinate": None,
             "coordinate": [800000000, 5000000000, 123400],
             "measurement": {"recorded_at": 900000000, "fix_status": "RTK_FIXED",
                             "satellites": 24, "hdop": 0.6,
                             "quality": {"accepted": True, "reasons": []},
                             "precision": "e8", "source": "stakeout_" + mode},
             "stakeout_tid": "so-3a4739b9/1/1", "stakeout_gen": 2,
             "stakeout_cmd": "0123456789abcdef", "stakeout_tolerance_m": 0.1,
             "stakeout_dn_m": 0.012, "stakeout_de_m": -0.004, "stakeout_dist_m": 0.013}
    point.update(fields)
    return point


MISSING = object()


class TestPointFields(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(stakeout_progress.GEN_LIMIT, 10 ** 12)
        self.assertEqual(stakeout_progress.SOURCES, ("stakeout_verified", "stakeout_manual"))

    def test_command_rule(self):
        valid = stakeout_progress.valid_command
        self.assertTrue(valid("0123456789abcdef"))
        for value in ("0123456789abcde", "0123456789abcdef0", "0123456789ABCDEF",
                      "0123456789abcdeg", "", None, 123, b"0123456789abcdef"):
            with self.subTest(value=value):
                self.assertFalse(valid(value))

    def test_point_field_rules(self):
        check = stakeout_progress.valid_point_fields
        self.assertTrue(check(stakeout_point("verified")))
        self.assertTrue(check(stakeout_point("manual")))
        self.assertTrue(check(stakeout_point(stakeout_tolerance_m=None)))
        self.assertTrue(check(stakeout_point(stakeout_tid="so-3a4739b9/500/1000000")))
        self.assertTrue(check(stakeout_point(stakeout_gen=0)))
        self.assertTrue(check(stakeout_point(stakeout_gen=stakeout_progress.GEN_LIMIT - 1)))
        cases = [("stakeout_tid", MISSING), ("stakeout_tid", "x"),
                 ("stakeout_tid", "so-3a4739b9/0/1"), ("stakeout_tid", "so-3a4739b9/1/01"),
                 ("stakeout_tid", 7),
                 ("stakeout_gen", MISSING), ("stakeout_gen", "1"), ("stakeout_gen", True),
                 ("stakeout_gen", -1), ("stakeout_gen", 1.0),
                 ("stakeout_gen", stakeout_progress.GEN_LIMIT),
                 ("stakeout_cmd", MISSING), ("stakeout_cmd", "0123456789abcde"),
                 ("stakeout_cmd", "0123456789ABCDEF"),
                 ("source", MISSING), ("source", "manual"), ("source", "stakeout_x"),
                 ("stakeout_tolerance_m", -0.1), ("stakeout_tolerance_m", "0.1"),
                 ("stakeout_tolerance_m", True),
                 ("stakeout_dist_m", -1), ("stakeout_dist_m", None),
                 ("stakeout_dist_m", float("nan")), ("stakeout_dn_m", "0"),
                 ("stakeout_de_m", float("inf")), ("stakeout_de_m", False),
                 ("line_id", "line-1"), ("object_type", "valve")]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                point = copy.deepcopy(stakeout_point())
                target = point["measurement"] if key == "source" else point
                if value is MISSING:
                    del target[key]
                else:
                    target[key] = value
                self.assertFalse(check(point))
        point = stakeout_point()
        point["measurement"] = None
        self.assertFalse(check(point))


# --- progress log, fold and decisions (A3) -------------------------------

PID = "project-a"
FP = 1760000000
SET = "so-3a4739b9"
SET2 = "so-0badf00d"
HEADER = {"kind": "stakeout_progress", "schema": 1, "project_id": PID,
          "project_created_at": FP}
_counter = [0]


def tid(part, seq, set_id=SET):
    return "%s/%d/%d" % (set_id, part, seq)


def spoint(target, gen, mode="verified"):
    """A valid stakeout point of target at gen, with a fresh point ID."""
    _counter[0] += 1
    return stakeout_point(mode, id="point-%08d-0001" % _counter[0], stakeout_tid=target,
                          stakeout_gen=gen)


def other_point(number=0):
    return {"id": "point-9%07d-0001" % number, "object_type": "valve", "line_id": None}


def skip(target, gen, cmd="0123456789abcdef"):
    return {"t": 1, "ev": "skip", "tid": target, "gen": gen, "cmd": cmd}


def reopen(target, gen, **fields):
    event = {"t": 1, "ev": "reopen", "tid": target, "gen": gen}
    event.update(fields)
    return event


def active(target):
    return {"t": 1, "ev": "active", "tid": target}


def order(part_id, flag):
    return {"t": 1, "ev": "order", "part_id": part_id, "reversed": flag}


def set_removed(set_id, imported_at, floor):
    return {"t": 1, "ev": "set_removed", "set_id": set_id, "imported_at": imported_at,
            "floor": floor}


def restored(floor):
    return {"t": 1, "ev": "restored", "floor": floor}


def lines(*events):
    return "".join(json.dumps(event) + "\n" for event in events).encode()


class _ProgressCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="stakeout-progress-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "stakeout")
        os.makedirs(os.path.join(self.root, PID))
        os.makedirs(os.path.join(self.root, "project-b"))
        self.store = stakeout.TargetStore(self.root)
        self.progress = stakeout_progress.ProgressLog(self.store)
        for module in (stakeout, stakeout_progress):
            patcher = mock.patch.object(module, "log")
            setattr(self, "log" if module is stakeout_progress else "store_log",
                    patcher.start())
            self.addCleanup(patcher.stop)

    def path(self, project_id=PID, suffix=""):
        return os.path.join(self.root, project_id, "progress.jsonl" + suffix)

    def write_file(self, data, project_id=PID, header=None):
        head = (json.dumps(header or dict(HEADER, project_id=project_id)) + "\n").encode()
        with open(self.path(project_id), "wb") as handle:
            handle.write(head + data)

    def read_file(self, project_id=PID, suffix=""):
        with open(self.path(project_id, suffix), "rb") as handle:
            return handle.read()

    def restart(self):
        self.store = stakeout.TargetStore(self.root)
        self.progress = stakeout_progress.ProgressLog(self.store)

    def load(self, project_id=PID, fingerprint=FP, active=True, write=True, progress=None,
             capacity=None):
        progress = progress or self.progress

        async def run():
            async with progress.store.mutation(write):
                return await progress.load(project_id, fingerprint, active, capacity)
        return asyncio.run(run())

    def read(self, project_id=PID, fingerprint=FP, active=True, capacity=None):
        return asyncio.run(self.progress.read(project_id, fingerprint, active, capacity))

    def folded(self, *events):
        """A fresh load of a file holding events."""
        self.write_file(lines(*events))
        self.restart()
        return self.load()

    def warnings(self, level="WARN"):
        return [call for call in self.log.call_args_list if call.args[0] == level]

    def refused(self, code, call, *args, **kwargs):
        with self.assertRaises(stakeout.StakeoutError) as caught:
            call(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, caught.exception.message)
        return caught.exception


class TestFold(_ProgressCase):
    EVENTS = (skip(tid(1, 2), 0), reopen(tid(1, 2), 1), skip(tid(1, 3), 0),
              active(tid(2, 1)), order(SET + "/2", True),
              set_removed(SET2, 1700, 3))

    def check(self, fold):
        self.assertEqual(fold.state(tid(1, 2), []), ("open", None, 1, None))
        self.assertEqual(fold.state(tid(1, 3), []), ("skipped", None, 0, None))
        self.assertEqual(fold.state(tid(1, 4), []), ("open", None, 0, None))
        self.assertEqual(fold.active_tid, tid(2, 1))
        self.assertEqual(fold.orders(), {SET: [2]})
        self.assertEqual(fold.floors, {SET2: 3})
        self.assertEqual(fold.removed, {SET2: 1700})
        self.assertEqual(fold.floor_of(SET2), 3)
        self.assertEqual(fold.floor_of(SET), 0)
        self.assertEqual(fold.damaged_lines, 0)

    def test_events_fold_as_written(self):
        fold = self.load()
        for event in self.EVENTS:
            self.progress.append(fold, event)
        self.check(fold)
        self.restart()
        self.check(self.load())
        self.write_file(lines(*(self.EVENTS + self.EVENTS)))
        self.restart()
        self.check(self.load())

    def test_restored_floor_lifts_the_project(self):
        fold = self.folded(*(self.EVENTS + (restored(4),)))
        for target in (tid(1, 2), tid(1, 3), tid(1, 4), tid(3, 9, SET2)):
            self.assertEqual(fold.state(target, []), ("open", None, 4, None))
        self.assertEqual((fold.base, fold.floor_of(SET), fold.floor_of(SET2)), (4, 4, 4))
        self.progress.append(fold, skip(tid(1, 2), 4))
        self.progress.append(fold, skip(tid(1, 3), 3))
        self.progress.append(fold, restored(4))
        self.assertEqual(fold.state(tid(1, 2), []), ("skipped", None, 4, None))
        self.assertEqual(fold.state(tid(1, 3), []), ("open", None, 4, None))
        self.restart()
        fold = self.load()
        self.assertEqual(fold.base, 4)
        self.assertEqual(fold.state(tid(1, 2), []), ("skipped", None, 4, None))
        self.assertEqual(fold.state(tid(1, 3), []), ("open", None, 4, None))

    def test_apply_rules(self):
        fold = stakeout_progress.Fold(PID, FP)
        self.assertTrue(fold.apply(skip(tid(1, 1), 2)))
        self.assertFalse(fold.apply({"ev": "skip", "tid": tid(1, 1)}))
        self.assertFalse(fold.apply(order(SET, True)))
        self.assertFalse(fold.apply(order(SET + "/2", 1)))
        self.assertFalse(fold.apply(set_removed("so-x", None, 1)))
        self.assertFalse(fold.apply(set_removed(SET, "1", 1)))
        self.assertFalse(fold.apply(active("x")))
        self.assertTrue(fold.apply(active(None)))
        self.assertTrue(fold.apply(order(SET + "/2", True)))
        self.assertTrue(fold.apply(order(SET + "/2", False)))
        self.assertEqual(fold.orders(), {})
        self.assertTrue(fold.apply(active(tid(1, 1))))
        self.assertTrue(fold.apply(set_removed(SET, None, 1)))
        self.assertEqual((fold.active_tid, fold.entries.get(SET), fold.removed),
                         (None, None, {SET: None}))
        # A lower set_removed floor never lowers the set's floor.
        self.assertTrue(fold.apply(set_removed(SET, 5, 0)))
        self.assertEqual(fold.floors, {SET: 1})


class TestDerivedStatus(_ProgressCase):
    def test_point_at_the_current_generation_confirms(self):
        fold = self.load()
        point = spoint(tid(1, 1), 0)
        self.assertEqual(fold.state(tid(1, 1), [point]), ("confirmed", "verified", 0, point))
        manual = spoint(tid(1, 2), 0, "manual")
        self.assertEqual(fold.state(tid(1, 2), [point, manual]),
                         ("confirmed", "manual", 0, manual))
        derive = stakeout_progress.derive
        self.assertEqual(derive(0, 0, None), ("open", None, 0, None))
        self.assertEqual(derive(3, 0, None), ("skipped", None, 1, None))
        self.assertEqual(derive(3, 2, None), ("open", None, 2, None))
        self.assertEqual(derive(4, 0, point), ("open", None, 2, None))
        self.assertEqual(derive(0, 0, point), ("confirmed", "verified", 0, point))

    def test_reopen_moves_the_generation_on(self):
        fold = self.load()
        points = [spoint(tid(1, 1), 0)]
        self.progress.append(fold, reopen(tid(1, 1), 1))
        self.assertEqual(fold.state(tid(1, 1), points), ("open", None, 1, None))
        points.append(spoint(tid(1, 1), 1))
        self.assertEqual(fold.state(tid(1, 1), points), ("confirmed", "verified", 1, points[1]))

    def test_removing_the_point_reopens_the_target(self):
        fold = self.load()
        point = spoint(tid(1, 1), 0)
        project = {"points": [other_point(1), point]}
        self.assertEqual(fold.state(tid(1, 1), project["points"])[0], "confirmed")
        project["points"] = [other_point(1)]
        self.assertEqual(fold.state(tid(1, 1), project["points"]), ("open", None, 0, None))
        project["points"] = [other_point(1), point]
        self.assertEqual(fold.state(tid(1, 1), project["points"])[0], "confirmed")
        project["points"] = [other_point(1), other_point(2)]
        self.assertEqual(fold.state(tid(1, 1), project["points"]), ("open", None, 0, None))

    def test_lost_reopen_line_still_shows_the_point(self):
        fold = self.load()
        point = spoint(tid(1, 1), 1)
        self.assertEqual(fold.state(tid(1, 1), [point]), ("confirmed", "verified", 1, point))

    def test_floor_hides_old_points(self):
        fold = self.folded(set_removed(SET, 1700, 2))
        self.assertEqual(fold.state(tid(1, 1), [spoint(tid(1, 1), 1)]), ("open", None, 2, None))
        point = spoint(tid(1, 1), 2)
        self.assertEqual(fold.state(tid(1, 1), [point])[0], "confirmed")

    def test_highest_generation_wins_and_ties_keep_the_first(self):
        fold = self.load()
        first, second, newer = spoint(tid(1, 1), 1), spoint(tid(1, 1), 1), spoint(tid(1, 1), 2)
        self.assertIs(fold.state(tid(1, 1), [first, second])[3], first)
        self.assertIs(fold.state(tid(1, 1), [newer, first])[3], newer)

    def test_counts_and_rows(self):
        points = [spoint(tid(1, 1), 0), spoint(tid(1, 2), 0, "manual"), spoint(tid(1, 5), 0),
                  spoint(tid(2, 1, SET2), 0, "manual"), other_point()]
        fold = self.folded(skip(tid(1, 3), 0), reopen(tid(1, 4), 1), reopen(tid(1, 5), 1),
                           skip(tid(1, 6), 0), reopen(tid(1, 6), 1))
        self.assertEqual(fold.counts(SET, points),
                         {"confirmed": 2, "verified": 1, "manual": 1, "skipped": 1})
        self.assertEqual(fold.counts(SET2, points),
                         {"confirmed": 1, "verified": 0, "manual": 1, "skipped": 0})
        self.assertEqual(fold.counts("so-00000000", points),
                         {"confirmed": 0, "verified": 0, "manual": 0, "skipped": 0})
        self.assertEqual(list(fold.rows([SET2, SET], points)), [
            (tid(2, 1, SET2), "confirmed", 0, "manual", points[3]["id"]),
            (tid(1, 1), "confirmed", 0, "verified", points[0]["id"]),
            (tid(1, 2), "confirmed", 0, "manual", points[1]["id"]),
            (tid(1, 3), "skipped", 0, None, None),
            (tid(1, 4), "open", 1, None, None),
            (tid(1, 5), "open", 1, None, None),
            (tid(1, 6), "open", 1, None, None)])
        self.progress.append(fold, restored(1))
        self.assertEqual(list(fold.rows([SET], points)), [])
        self.assertEqual(fold.counts(SET, points)["confirmed"], 0)
        self.assertEqual(fold.floor_for(SET, points), 2)

    def test_floor_for_exceeds_the_set(self):
        fold = self.load()
        self.assertEqual(fold.floor_for(SET, []), 1)
        self.progress.append(fold, reopen(tid(1, 1), 3))
        points = [spoint(tid(1, 2), 5), spoint(tid(1, 1, SET2), 9)]
        self.assertEqual(fold.floor_for(SET, points), 6)
        self.progress.append(fold, restored(7))
        self.assertEqual(fold.floor_for(SET, points), 8)

    def test_index_rebuild_is_bounded(self):
        sets = ["so-%08x" % number for number in range(4)]
        points = [other_point(number) for number in range(3000)]
        points += [spoint(tid(1, seq + 1, set_id), 0) for set_id in sets for seq in range(500)]
        self.assertEqual(len(points), 5000)
        fold = self.load()
        started = time.perf_counter()
        status = fold.state(tid(1, 1, sets[0]), points)
        spent = (time.perf_counter() - started) * 1000
        self.assertEqual(status[0], "confirmed")
        self.assertLess(spent, 250)
        point = spoint(tid(1, 77), 0, "manual")
        points.append(point)
        with mock.patch.object(stakeout_progress.Fold, "_build",
                               side_effect=AssertionError("rebuilt")):
            fold.note_point(point, points)
            self.assertEqual(fold.state(tid(1, 77), points), ("confirmed", "manual", 0, point))
            self.assertEqual(fold.state(tid(1, 1, sets[0]), points)[0], "confirmed")

    def test_invalid_points_are_ignored_and_counted(self):
        target = tid(1, 1)
        bad = [spoint(target, 1), spoint(target, 1), spoint(target, 1), spoint(target, 1)]
        bad[0]["stakeout_gen"] = "1"
        bad[1]["stakeout_gen"] = True
        del bad[2]["stakeout_tid"]
        bad[3]["measurement"]["source"] = "survey"
        good = spoint(tid(1, 2), 0)
        points = bad + [good, other_point()]
        fold = self.load()
        self.assertEqual(fold.state(target, points), ("open", None, 0, None))
        self.assertEqual(fold.state(tid(1, 2), points)[0], "confirmed")
        self.assertEqual(fold.counts(SET, points)["confirmed"], 1)
        self.assertEqual(list(fold.rows([SET], points)),
                         [(tid(1, 2), "confirmed", 0, "verified", good["id"])])
        self.assertEqual(fold.ignored_points, 4)
        self.assertEqual(len(self.warnings()), 1)
        fold.state(target, list(points))
        self.assertEqual(len(self.warnings()), 2)


class TestDecide(_ProgressCase):
    def scenario(self, base):
        """Targets at generation g = base: open, confirmed (verified), skipped,
        reopened (open at base + 1) and confirmed manual."""
        events = [skip(tid(1, 3), base), reopen(tid(1, 4), base + 1)]
        if base:
            events.insert(0, restored(base))
        fold = self.folded(*events)
        points = [spoint(tid(1, 2), base), spoint(tid(1, 5), base, "manual")]
        return fold, points

    def outcome(self, fold, points, action, target, expect, mode=None):
        try:
            return stakeout_progress.decide(fold, action, target, expect, mode, points)
        except stakeout.StakeoutError as error:
            self.assertEqual(error.status, "409 Conflict")
            return error.code, error.payload()

    def test_decision_matrix(self):
        for base in (0, 2):
            with self.subTest(base=base):
                fold, points = self.scenario(base)
                g = base
                T_OPEN, T_CONF, T_SKIP, T_REOPENED, T_MANUAL = (tid(1, n) for n in range(1, 6))
                decide = lambda *args: self.outcome(fold, points, *args)
                changed = decide("confirm", T_OPEN, g + 1, "verified")
                self.assertEqual(changed[0], "target_changed")
                self.assertEqual({k: changed[1][k] for k in ("status", "mode", "gen")},
                                 {"status": "open", "mode": None, "gen": g})
                self.assertEqual(decide("confirm", T_CONF, g, "verified"),
                                 ("confirmed", "verified", g, points[0], True))
                refused = decide("confirm", T_CONF, g, "manual")
                self.assertEqual(refused[0], "target_not_open")
                self.assertEqual((refused[1]["status"], refused[1]["mode"], refused[1]["gen"]),
                                 ("confirmed", "verified", g))
                self.assertEqual(decide("confirm", T_MANUAL, g, "verified")[0],
                                 "target_not_open")
                self.assertEqual(decide("confirm", T_SKIP, g, "verified")[0], "target_not_open")
                self.assertEqual(decide("confirm", T_OPEN, g, "manual"),
                                 ("confirmed", "manual", g, None, False))
                self.assertEqual(decide("confirm", T_REOPENED, g + 1, "verified"),
                                 ("confirmed", "verified", g + 1, None, False))
                # skip
                self.assertEqual(decide("skip", T_OPEN, g + 1)[0], "target_changed")
                self.assertEqual(decide("skip", T_SKIP, g), ("skipped", None, g, None, True))
                self.assertEqual(decide("skip", T_CONF, g)[0], "target_not_open")
                self.assertEqual(decide("skip", T_OPEN, g), ("skipped", None, g, None, False))
                # reopen
                self.assertEqual(decide("reopen", T_REOPENED, g),
                                 ("open", None, g + 1, None, True))
                self.assertEqual(decide("reopen", T_REOPENED, g + 2)[0], "target_changed")
                fold.apply(skip(tid(1, 4), g + 1))
                self.assertEqual(decide("reopen", T_REOPENED, g)[0], "target_changed")
                self.assertEqual(decide("reopen", T_OPEN, g)[0], "target_open")
                self.assertEqual(decide("reopen", T_CONF, g), ("open", None, g + 1, None, False))
                self.assertEqual(decide("reopen", T_SKIP, g), ("open", None, g + 1, None, False))
                self.assertEqual(decide("reopen", T_SKIP, g - 1 if g else g + 1)[0],
                                 "target_changed")

    def test_answer_shape(self):
        point = spoint(tid(1, 1), 3)
        self.assertEqual(stakeout_progress.answer(tid(1, 1), "confirmed", "verified", 3, point,
                                                  True),
                         {"persisted": True, "repeated": True, "tid": tid(1, 1), "gen": 3,
                          "status": "confirmed", "mode": "verified", "point_id": point["id"],
                          "dn_m": 0.012, "de_m": -0.004, "dist_m": 0.013, "tolerance_m": 0.1})
        self.assertEqual(stakeout_progress.answer(tid(1, 1), "open", None, 4, None, False),
                         {"persisted": True, "repeated": False, "tid": tid(1, 1), "gen": 4,
                          "status": "open", "mode": None, "point_id": None, "dn_m": None,
                          "de_m": None, "dist_m": None, "tolerance_m": None})

    def test_event_builders(self):
        with mock.patch.object(stakeout, "now_seconds", return_value=812345678):
            P = stakeout_progress
            self.assertEqual(P.skip_event(tid(1, 1), 2, "0123456789abcdef"),
                             {"t": 812345678, "ev": "skip", "tid": tid(1, 1), "gen": 2,
                              "cmd": "0123456789abcdef"})
            self.assertEqual(P.reopen_event(tid(1, 1), 3),
                             {"t": 812345678, "ev": "reopen", "tid": tid(1, 1), "gen": 3})
            self.assertEqual(P.reopen_event(tid(1, 1), 3, by="undo"),
                             {"t": 812345678, "ev": "reopen", "tid": tid(1, 1), "gen": 3,
                              "by": "undo"})
            self.assertEqual(P.reopen_event(tid(1, 1), 3, "0123456789abcdef")["cmd"],
                             "0123456789abcdef")
            self.assertEqual(P.active_event(None), {"t": 812345678, "ev": "active", "tid": None})
            self.assertEqual(P.order_event(SET + "/2", True),
                             {"t": 812345678, "ev": "order", "part_id": SET + "/2",
                              "reversed": True})
            self.assertEqual(P.set_removed_event(SET, None, 4),
                             {"t": 812345678, "ev": "set_removed", "set_id": SET,
                              "imported_at": None, "floor": 4})
            self.assertEqual(P.restored_event(5), {"t": 812345678, "ev": "restored", "floor": 5})
            fold = P.Fold(PID, FP)
            for event in (P.skip_event(tid(1, 1), 2, "0123456789abcdef"), P.reopen_event(tid(1, 1), 3),
                          P.active_event(tid(1, 1)), P.order_event(SET + "/2", True),
                          P.set_removed_event(SET, None, 4), P.restored_event(5)):
                self.assertTrue(fold.apply(event), event)


class _Handle:
    """A file handle whose method named fail raises OSError; counts writes."""

    def __init__(self, real, fail=None):
        self.real = real
        self.fail = fail
        self.writes = []

    def _check(self, name):
        if self.fail == name:
            raise OSError(5, "injected " + name)

    def write(self, data):
        self.writes.append(bytes(data))
        self._check("write")
        return self.real.write(data)

    def flush(self):
        self._check("flush")
        return self.real.flush()

    def close(self):
        self.real.close()
        self._check("close")

    def read(self, *args):
        return self.real.read(*args)


class _Opener:
    def __init__(self, fail=None):
        self.fail = fail
        self.handles = []

    def __call__(self, path, mode="r"):
        if self.fail == "open" and "a" in mode:
            raise OSError(28, "injected open")
        handle = _Handle(open(path, mode), self.fail if "a" in mode else None)
        self.handles.append(handle)
        return handle


class TestHeader(_ProgressCase):
    def test_first_append_writes_header_and_event_in_one_write(self):
        fold = self.load()
        opener = _Opener()
        with mock.patch("stakeout_progress.open", opener, create=True):
            self.progress.append(fold, skip(tid(1, 1), 0))
        writes = [data for handle in opener.handles for data in handle.writes]
        self.assertEqual(len(writes), 1)
        data = self.read_file()
        self.assertEqual(writes[0], data)
        first, second, rest = data.split(b"\n")
        self.assertEqual(json.loads(first), HEADER)
        self.assertEqual(json.loads(second), skip(tid(1, 1), 0))
        self.assertEqual(rest, b"")
        self.assertEqual(fold.size, len(data))
        self.progress.append(fold, skip(tid(1, 2), 0))
        self.assertEqual(self.read_file().count(b"stakeout_progress"), 1)

    def test_foreign_header_is_set_aside(self):
        cases = (("project", lines(dict(HEADER, project_id="project-b"), skip(tid(1, 1), 0))),
                 ("fingerprint", lines(dict(HEADER, project_created_at=FP + 1),
                                       skip(tid(1, 1), 0))),
                 ("kind", lines(dict(HEADER, kind="other"), skip(tid(1, 1), 0))),
                 ("schema", lines(dict(HEADER, schema=2), skip(tid(1, 1), 0))),
                 ("garbage", b"\x00garbage\n" + lines(skip(tid(1, 1), 0))),
                 ("torn header", b'{"kind": "stakeout_pro'))
        for label, data in cases:
            with self.subTest(label):
                self.log.reset_mock()
                with open(self.path(), "wb") as handle:
                    handle.write(data)
                self.restart()
                fold = self.load()
                self.assertEqual(self.read_file(suffix=".foreign"), data)
                self.assertFalse(os.path.exists(self.path()))
                self.assertEqual((fold.entries, fold.size, fold.damaged_lines, fold.torn),
                                 ({}, 0, 0, False))
                self.assertEqual(fold.state(tid(1, 1), [])[0], "open")
                self.assertEqual(len(self.warnings()), 1)
                self.progress.append(fold, skip(tid(1, 2), 0))
                self.assertEqual(self.read_file(), lines(HEADER, skip(tid(1, 2), 0)))
                self.restart()
                self.assertEqual(self.load().state(tid(1, 2), [])[0], "skipped")
                os.remove(self.path())

    def test_load_removes_a_leftover_tmp(self):
        self.write_file(lines(skip(tid(1, 1), 0)))
        with open(self.path(suffix=".tmp"), "wb") as handle:
            handle.write(b"partial")
        fold = self.load()
        self.assertFalse(os.path.exists(self.path(suffix=".tmp")))
        self.assertEqual(fold.state(tid(1, 1), [])[0], "skipped")


class TestTornLines(_ProgressCase):
    def test_damaged_lines_are_skipped_and_counted(self):
        limit = stakeout_progress.GEN_LIMIT
        data = (lines(skip(tid(1, 1), 0)) + b"not json at all\n"
                + lines({"ev": "skip", "tid": tid(1, 9), "gen": 0, "pad": "x" * 5000},
                        {"t": 1, "ev": "confirm", "tid": tid(1, 2), "gen": 0},
                        skip("so-3a4739b9/0/1", 0), skip(tid(1, 3), -1),
                        skip(tid(1, 4), limit), skip(tid(1, 5), "1"),
                        reopen(tid(1, 6), 1), reopen(tid(1, 7), 1, by="undo"),
                        skip(tid(1, 8), limit - 1)))
        self.write_file(data)
        fold = self.load()
        self.assertEqual(fold.damaged_lines, 7)
        self.assertEqual(fold.state(tid(1, 1), [])[0], "skipped")
        for target in (tid(1, 9), tid(1, 3), tid(1, 4), tid(1, 5)):
            self.assertEqual(fold.state(target, []), ("open", None, 0, None))
        self.assertEqual(fold.state(tid(1, 6), []), ("open", None, 1, None))
        self.assertEqual(fold.state(tid(1, 7), []), ("open", None, 1, None))
        self.assertEqual(fold.state(tid(1, 8), []), ("skipped", None, limit - 1, None))
        self.assertFalse(fold.torn)
        self.assertEqual(len(self.warnings()), 1)

    def test_a_long_line_across_blocks_is_one_damaged_line(self):
        data = (lines(skip(tid(1, 1), 0)) + b"x" * 9000 + b"\n" + lines(skip(tid(1, 2), 0))
                + b"y" * (stakeout_progress.LINE_MAX + 1) + b"\n" + lines(skip(tid(1, 3), 0)))
        self.write_file(data)
        fold = self.load()
        self.assertEqual(fold.damaged_lines, 2)
        for seq in (1, 2, 3):
            self.assertEqual(fold.state(tid(1, seq), [])[0], "skipped")
        self.assertEqual(fold.size, os.path.getsize(self.path()))

    def test_torn_tail_gets_a_line_break_first(self):
        self.write_file(lines(skip(tid(1, 1), 0)) + b'{"t": 1, "ev": "skip", "tid": "so-3a')
        fold = self.load()
        self.assertTrue(fold.torn)
        self.assertEqual(fold.state(tid(1, 1), [])[0], "skipped")
        before = self.read_file()
        self.progress.append(fold, skip(tid(1, 2), 0))
        self.assertEqual(self.read_file(), before + b"\n" + lines(skip(tid(1, 2), 0)))
        self.assertFalse(fold.torn)
        self.restart()
        fold = self.load()
        self.assertEqual(fold.state(tid(1, 1), [])[0], "skipped")
        self.assertEqual(fold.state(tid(1, 2), [])[0], "skipped")
        self.assertEqual((fold.torn, fold.damaged_lines), (False, 1))

    def test_a_complete_last_line_without_a_break_counts(self):
        self.write_file(lines(skip(tid(1, 1), 0)) + json.dumps(skip(tid(1, 2), 0)).encode())
        fold = self.load()
        self.assertTrue(fold.torn)
        self.assertEqual(fold.state(tid(1, 2), [])[0], "skipped")
        self.progress.append(fold, skip(tid(1, 3), 0))
        self.restart()
        fold = self.load()
        self.assertEqual([fold.state(tid(1, n), [])[0] for n in (1, 2, 3)], ["skipped"] * 3)
        self.assertEqual(fold.damaged_lines, 0)


class TestAppend(_ProgressCase):
    def test_append_checks_the_reserve_and_charges(self):
        fold = self.load()
        self.progress.append(fold, skip(tid(1, 1), 0))
        before = self.read_file()
        asked, charged = [], []
        refuse = lambda size: asked.append(size) or False
        error = self.refused("storage_reserve", self.progress.append, fold, skip(tid(1, 2), 0),
                             refuse, charged.append)
        self.assertEqual(error.status, "507 Insufficient Storage")
        self.assertEqual(self.read_file(), before)
        self.assertEqual(charged, [])
        self.assertIsNone(self.store.uncertain)
        line = lines(skip(tid(1, 2), 0))
        self.assertEqual(asked, [len(line) + 4096])
        self.assertEqual(fold.state(tid(1, 2), [])[0], "open")
        self.progress.append(fold, skip(tid(1, 2), 0), lambda size: True, charged.append)
        self.assertEqual(self.read_file(), before + line)
        self.assertEqual(charged, [len(line)])
        self.assertEqual(fold.state(tid(1, 2), [])[0], "skipped")

    def test_set_removed_ignores_the_reserve_but_charges(self):
        fold = self.load()
        charged = []
        self.progress.append(fold, set_removed(SET, 1700, 1), lambda size: False,
                             charged.append, reserve=False)
        self.assertEqual(charged, [len(self.read_file())])
        self.assertEqual(fold.removed, {SET: 1700})

    def test_unclear_write_sets_the_latch(self):
        for stage in ("open", "write", "flush", "close"):
            with self.subTest(stage):
                self.restart()
                self.log.reset_mock()
                fold = self.load()
                self.progress.append(fold, skip(tid(1, 1), 0))
                state, size = fold.state(tid(1, 2), []), fold.size
                with mock.patch("stakeout_progress.open", _Opener(stage), create=True), \
                        mock.patch.object(self.progress, "load",
                                          side_effect=AssertionError("reloaded")):
                    error = self.refused("stakeout_write_uncertain", self.progress.append,
                                         fold, skip(tid(1, 2), 0))
                self.assertEqual(error.status, "503 Service Unavailable")
                self.assertIn(PID, self.store.uncertain)
                self.assertIn("skip", self.store.uncertain)
                errors = self.warnings("ERROR")
                self.assertEqual(len(errors), 1)
                self.assertIn("write uncertain", errors[0].args[2])
                self.assertIn("locked until restart", errors[0].args[2])
                self.assertEqual((fold.state(tid(1, 2), []), fold.size), (state, size))
                self.assertIs(self.progress.peek(PID, FP), fold)

                async def lock():
                    async with self.store.mutation():
                        pass
                with self.assertRaises(stakeout.StakeoutError) as caught:
                    asyncio.run(lock())
                self.assertEqual(caught.exception.code, "stakeout_write_uncertain")
                os.remove(self.path())

    def test_a_power_cut_is_not_caught(self):
        class PowerCut(BaseException):
            pass

        fold = self.load()

        def cut(*args):
            raise PowerCut()
        with mock.patch("stakeout_progress.open", cut, create=True):
            with self.assertRaises(PowerCut):
                self.progress.append(fold, skip(tid(1, 1), 0))
        self.assertIsNone(self.store.uncertain)

    def test_events_are_short_ascii(self):
        limit = stakeout_progress.GEN_LIMIT
        longest = "so-ffffffff/500/1000000"
        with mock.patch.object(stakeout, "now_seconds", return_value=4294967295):
            events = (stakeout_progress.set_removed_event("so-ffffffff", 4294967295, limit - 1),
                      stakeout_progress.reopen_event(longest, limit - 1, "0123456789abcdef",
                                                     "undo"),
                      stakeout_progress.skip_event(longest, limit - 1, "0123456789abcdef"),
                      stakeout_progress.order_event("so-ffffffff/500", False),
                      stakeout_progress.active_event(longest),
                      stakeout_progress.restored_event(limit - 1))
        for event in events:
            line = json.dumps(event)
            self.assertLess(device_bytes(event), stakeout_progress.LINE_MAX)
            self.assertTrue(line.isascii())
            self.assertTrue(stakeout_progress.Fold(PID, FP).apply(event))


class TestLockedLoading(_ProgressCase):
    def test_load_needs_the_lock(self):
        with self.assertRaises(RuntimeError):
            asyncio.run(self.progress.load(PID, FP, True))

    def test_cold_reader_is_busy_while_locked_never_open(self):
        self.write_file(lines(skip(tid(1, 1), 0)))
        self.store.busy = True
        error = self.refused("stakeout_busy", self.read)
        self.assertEqual(error.status, "503 Service Unavailable")
        self.assertIs(self.store.busy, True)
        self.assertIsNone(self.progress.peek(PID, FP))

    def test_cold_reader_loads_under_the_latch(self):
        self.write_file(lines(skip(tid(1, 1), 0)))
        self.store.uncertain = "test"
        fold = self.read()
        self.assertEqual(fold.state(tid(1, 1), [])[0], "skipped")
        self.assertIs(self.progress.peek(PID, FP), fold)
        self.assertIs(self.store.busy, False)

    def test_warm_reader_reads_during_a_mutation(self):
        fold = self.load()
        self.store.busy = True
        self.assertIs(self.read(), fold)
        self.assertIs(self.read(active=False), fold)

    def test_other_projects_load_transiently(self):
        fold = self.load()
        other = self.load("project-b", FP + 5, active=False)
        self.assertIsNot(other, fold)
        self.assertIs(self.progress.peek(PID, FP), fold)
        self.assertIsNone(self.progress.peek("project-b", FP + 5))
        self.assertIsNone(self.progress.peek(PID, FP + 1))
        self.assertTrue(self.progress.valid(other))
        self.progress.append(other, skip(tid(1, 1), 0))
        first = self.read_file("project-b").split(b"\n")[0]
        self.assertEqual(json.loads(first)["project_id"], "project-b")
        self.assertEqual(json.loads(first)["project_created_at"], FP + 5)

    def test_forget_all_invalidates_every_fold(self):
        fold = self.load()
        other = self.load("project-b", FP, active=False)
        self.store.uncertain = "test"
        self.progress.forget_all()
        self.assertFalse(self.progress.valid(fold))
        self.assertFalse(self.progress.valid(other))
        self.assertIsNone(self.progress.peek(PID, FP))
        self.assertEqual(self.store.uncertain, "test")
        fresh = self.load(write=False)
        self.assertIsNot(fresh, fold)
        self.assertTrue(self.progress.valid(fresh))

    def test_forget_drops_its_project_and_every_transient_fold(self):
        fold = self.load()
        other = self.load("project-b", FP, active=False)
        self.progress.forget("project-b")
        self.assertIs(self.progress.peek(PID, FP), fold)
        self.assertTrue(self.progress.valid(fold))
        self.assertFalse(self.progress.valid(other))
        self.progress.forget(PID)
        self.assertIsNone(self.progress.peek(PID, FP))
        self.assertFalse(self.progress.valid(fold))

    def test_a_load_that_raced_a_forget_is_not_published(self):
        self.write_file(lines(skip(tid(1, 1), 0)))
        real = stakeout._pause

        async def pause(meter):
            self.progress.forget_all()
            await real(meter)
        with mock.patch.object(stakeout, "_pause", pause):
            fold = self.load()
        self.assertFalse(self.progress.valid(fold))
        self.assertIsNone(self.progress.peek(PID, FP))

    def test_load_notes_the_removed_sets_in_the_store(self):
        self.write_file(lines(set_removed(SET2, 1700, 3), set_removed(SET, None, 1)))
        self.load()
        self.assertEqual(self.store.removed[PID], {SET2: 1700, SET: None})


class TestPacing(_ProgressCase):
    def test_load_yields_in_short_blocks(self):
        events = []
        for number in range(3000):
            target = tid(1 + number % 20, 1 + number % 7)
            gen = number // 140
            events.append(skip(target, gen) if number % 2 else reopen(target, gen + 1))
        self.write_file(lines(*events))
        size = os.path.getsize(self.path())
        meters = []

        class Meter(stakeout._BlockMeter):
            def __init__(self):
                super().__init__()
                meters.append(self)
        with mock.patch.object(stakeout, "_BlockMeter", Meter):
            fold = self.load()
        self.assertEqual(fold.size, size)
        self.assertEqual(fold.damaged_lines, 0)
        self.assertEqual(len(meters), 1)
        meter = meters[0]
        meter.lap()
        self.assertGreaterEqual(meter.pauses, size // 4096)
        self.assertLess(meter.longest, 250)


# --- snapshot compaction (A4) ---------------------------------------------

SET3 = "so-12345678"
SETS = (SET, SET2, SET3)


def yes(size):
    return True


def churn():
    """300 skip/reopen alternations on 20 targets plus two set removals, a
    restored floor, an active target and two reversed parts: > 32 KiB."""
    events = [skip(tid(1, 1, SET3), 0), set_removed(SET3, None, 7),
              set_removed(SET2, 1700, 3), skip(tid(1, 1, SET2), 3), restored(2),
              order(SET2 + "/1", True)]
    for number in range(300):
        target = tid(1 + number % 20, 1)
        gen = 2 * (number // 20) + 2
        events.append(skip(target, gen))
        events.append(reopen(target, gen + 1))
    events += [skip(tid(4, 1), 31), skip(tid(5, 1), 31), active(tid(3, 1)),
               order(SET + "/2", True), order(SET + "/6", True), order(SET + "/6", False)]
    return events


def many(targets=300):
    """Two skip/reopen rounds on targets; they end skipped at generation 2."""
    events = []
    for number in range(targets):
        target = tid(1 + number // 100, 1 + number % 100)
        events += [skip(target, 0), reopen(target, 1), skip(target, 1), reopen(target, 2),
                   skip(target, 2)]
    return events


def churn_points():
    return [other_point(1), spoint(tid(1, 1), 31), spoint(tid(2, 1), 5, "manual"),
            spoint(tid(1, 1, SET2), 3, "manual")]


def reference(events):
    fold = stakeout_progress.Fold(PID, FP)
    for event in events:
        fold.apply(event)
    return fold


def summary(fold, points=()):
    points = list(points)
    return (list(fold.rows(SETS, points)), dict(fold.floors), fold.base, dict(fold.removed),
            fold.active_tid, fold.orders(), [fold.counts(set_id, points) for set_id in SETS])


class _CompactionCase(_ProgressCase):
    def prepared(self, events):
        self.write_file(lines(*events))
        self.restart()
        return self.read_file()

    def compactions(self):
        return [call for call in self.log.call_args_list if call.args[0] == "INFO"]


class TestCompaction(_CompactionCase):
    def test_compaction_keeps_the_fold(self):
        events = churn()
        points = churn_points()
        before = self.prepared(events)
        self.assertGreater(len(before), 32768)
        expected = summary(reference(events), points)
        self.assertIn(("skipped", 31, None, None),
                      [row[1:] for row in expected[0]])
        fold = self.load(capacity=yes)
        after = self.read_file()
        self.assertLess(len(after), 4096)
        self.assertEqual(summary(fold, points), expected)
        self.assertEqual((fold.size, fold.base_size, fold.torn, fold.damaged_lines),
                         (len(after), len(after), False, 0))
        self.assertIs(self.progress.peek(PID, FP), fold)
        record = self.progress.last_compaction
        self.assertEqual((record["before"], record["after"]), (len(before), len(after)))
        self.assertGreaterEqual(record["ms"], 0)
        self.assertGreaterEqual(record["longest_block_ms"], 0)
        self.assertFalse(os.path.exists(self.path(suffix=".tmp")))
        self.assertEqual(len(self.compactions()), 1)
        self.assertEqual(self.warnings(), [])
        # Compacted lines carry no time, command or reason.
        for line in after.split(b"\n")[1:-1]:
            event = json.loads(line)
            self.assertFalse({"t", "cmd", "by"} & set(event), event)
        self.restart()
        again = self.load(capacity=yes)
        self.assertEqual(summary(again, points), expected)
        self.assertEqual(self.read_file(), after)
        self.assertIsNone(self.progress.last_compaction)

    def test_threshold_doubles_with_the_kept_size(self):
        fold = self.folded(skip(tid(1, 1), 0))
        fold.base_size = 40960
        number = 0
        while fold.size < 61440:
            number += 1
            self.progress.append(fold, reopen(tid(2, 1), number))
        self.assertLess(fold.size, 81920)
        data = self.read_file()
        self.assertIs(self.load(capacity=yes), fold)
        self.assertEqual(self.read_file(), data)
        self.assertIsNone(self.progress.last_compaction)
        while fold.size < 81920:
            number += 1
            self.progress.append(fold, reopen(tid(2, 1), number))
        fresh = self.load(capacity=yes)
        self.assertIsNot(fresh, fold)
        self.assertEqual(self.progress.last_compaction["before"], fold.size)
        self.assertLess(fresh.size, 4096)
        self.assertEqual(fresh.base_size, fresh.size)
        self.assertEqual(fresh.state(tid(2, 1), []), ("open", None, number, None))
        self.assertEqual(fresh.state(tid(1, 1), []), ("skipped", None, 0, None))

    def test_no_compaction_without_reserve(self):
        events = churn()
        before = self.prepared(events)
        expected = summary(reference(events))
        asked = []
        for label, answer in (("False", False), ("None", None)):
            with self.subTest(label):
                self.log.reset_mock()
                self.restart()

                def capacity(size):
                    asked.append(size)
                    return answer
                fold = self.load(capacity=capacity)
                self.assertEqual(self.read_file(), before)
                self.assertEqual(summary(fold), expected)
                self.assertEqual(len(self.warnings()), 1)
                self.assertEqual(asked[-1], len(before) + 32768)
                self.assertIsNone(self.progress.last_compaction)
                self.assertFalse(os.path.exists(self.path(suffix=".tmp")))
                # The next load of the same fold does not warn again.
                self.assertIs(self.load(capacity=capacity), fold)
                self.assertEqual(len(self.warnings()), 1)

    def test_no_compaction_under_the_latch(self):
        before = self.prepared(churn())
        self.store.uncertain = "test"
        fold = self.load(write=False, capacity=yes)
        self.assertEqual(self.read_file(), before)
        self.assertIsNone(self.progress.last_compaction)
        self.assertEqual(summary(fold), summary(reference(churn())))
        self.assertEqual(self.store.uncertain, "test")

    def test_grown_fold_compacts_on_the_next_load(self):
        fold = self.folded(skip(tid(1, 1), 0))
        self.assertIs(self.load(capacity=yes), fold)
        self.assertIsNone(self.progress.last_compaction)
        events = [skip(tid(1, 1), 0)]
        for event in churn():
            self.progress.append(fold, event)
            events.append(event)
        self.assertGreater(fold.size, 32768)
        fresh = self.load(capacity=yes)
        self.assertIsNotNone(self.progress.last_compaction)
        self.assertIs(self.progress.peek(PID, FP), fresh)
        self.assertLess(fresh.size, 4096)
        self.assertEqual(summary(fresh), summary(reference(events)))

    def test_no_reader_compacts_during_a_mutation(self):
        before = self.prepared(churn())
        self.store.busy = True
        self.refused("stakeout_busy", self.read, capacity=yes)
        self.assertEqual(self.read_file(), before)
        self.assertIsNone(self.progress.last_compaction)

    def big_snapshot(self):
        """A log whose snapshot itself exceeds 32 KiB: 900 skipped targets."""
        events = [skip(tid(1 + number // 100, 1 + number % 100), 0) for number in range(900)]
        self.prepared(events)
        return events

    def test_kept_size_survives_a_restart(self):
        events = self.big_snapshot()
        fold = self.load(capacity=yes)
        kept = self.read_file()
        self.assertEqual(len(self.compactions()), 1)
        self.assertGreater(len(kept), 32768)
        self.assertEqual(fold.base_size, len(kept))
        for label, reset in (("restart", self.restart), ("forget_all", None)):
            with self.subTest(label):
                if reset:
                    reset()
                else:
                    self.progress.forget_all()
                self.progress.last_compaction = None
                again = self.load(capacity=yes)
                self.assertIsNot(again, fold)
                self.assertEqual(again.base_size, len(kept))
                self.assertEqual(self.read_file(), kept)
                self.assertIsNone(self.progress.last_compaction)
                self.assertEqual(summary(again), summary(reference(events)))
        self.assertEqual(len(self.compactions()), 1)
        # Appended events carry a time and do not count to the kept size;
        # neither does a damaged or torn line or anything after one.
        self.progress.append(again, reopen(tid(1, 1), 1))
        with open(self.path(), "ab") as handle:
            handle.write(b"garbage\n" + json.dumps({"ev": "skip", "tid": tid(1, 1),
                                                    "gen": 1}).encode() + b"\n{\"ev\"")
        self.restart()
        grown = self.load(capacity=yes)
        self.assertEqual(grown.base_size, len(kept))
        self.assertEqual((grown.damaged_lines, grown.torn), (2, True))
        self.assertEqual(len(self.compactions()), 1)

    def test_transient_loads_compact_at_most_once(self):
        events = self.big_snapshot()
        for number in range(3):
            fold = self.load(active=False, capacity=yes)
            self.assertIsNone(self.progress.peek(PID, FP))
            self.assertEqual(summary(fold), summary(reference(events)))
        self.assertEqual(len(self.compactions()), 1)
        self.assertEqual(self.warnings(), [])

    def test_no_reserve_warns_once_across_transient_loads(self):
        before = self.prepared(churn())
        for number in range(3):
            self.load(active=False, capacity=lambda size: False)
        self.assertEqual(self.read_file(), before)
        self.assertEqual(len(self.warnings()), 1)
        # Twice the declined size retries.
        fold = self.load(capacity=lambda size: False)
        number = 0
        while fold.size < 2 * len(before):
            number += 1
            self.progress.append(fold, reopen(tid(2, 1), 100 + number))
        self.load(active=False, capacity=yes)
        self.assertEqual(len(self.compactions()), 1)


class PowerCut(BaseException):
    """The power fails: nothing after it runs."""


class _FaultyTmp:
    """Opens files like open; on the .tmp, the fault (step, n-th call)
    raises OSError, or a PowerCut after copying the folder (cut), and
    "digest" flips a byte of the n-th read-back block."""

    def __init__(self, case, step, nth=1, cut=False):
        self.case = case
        self.step = step
        self.nth = nth
        self.cut = cut
        self.calls = {}
        self.real_rename = os.rename

    def hit(self, step):
        self.calls[step] = self.calls.get(step, 0) + 1
        if step == self.step and self.calls[step] == self.nth:
            if self.cut:
                shutil.copytree(os.path.join(self.case.root, PID), self.case.crash)
                raise PowerCut()
            raise OSError(5, "injected " + step)

    def __call__(self, path, mode="r"):
        if not path.endswith(".tmp"):
            return open(path, mode)
        if "w" in mode:
            self.hit("open")
        return _FaultyHandle(self, open(path, mode), "w" in mode)

    def rename(self, source, target):
        if source.endswith(".tmp"):
            self.hit("rename")
        self.real_rename(source, target)
        if source.endswith(".tmp"):
            self.hit("renamed")


class _FaultyHandle:
    def __init__(self, faults, real, writing):
        self.faults = faults
        self.real = real
        self.writing = writing

    def write(self, data):
        self.faults.hit("write")
        return self.real.write(data)

    def flush(self):
        self.faults.hit("flush")
        return self.real.flush()

    def close(self):
        self.real.close()
        if self.writing:
            self.faults.hit("close")

    def read(self, *args):
        self.faults.hit("read")
        data = self.real.read(*args)
        if data and self.faults.step == "digest" and self.faults.calls["read"] == self.faults.nth:
            data = bytes([data[0] ^ 1]) + data[1:]
        return data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class TestCompactionCrash(_CompactionCase):
    STEPS = (("open", 1), ("write", 1), ("write", 3), ("flush", 1), ("close", 1),
             ("read", 1), ("read", 3), ("digest", 2), ("rename", 1))

    def test_every_crash_point_leaves_old_or_new(self):
        events = many()
        old = self.prepared(events)
        self.load(capacity=yes)
        new = self.read_file()
        self.assertGreater(len(new), 3 * 4096)
        expected = summary(reference(events))
        self.crash = os.path.join(self.tmp, "crash")
        for step, nth in self.STEPS + (("renamed", 1),):
            for cut in (False, True):
                # A flipped byte is no crash; a crash after the rename is.
                if (step == "renamed") != cut and step in ("renamed", "digest"):
                    continue
                with self.subTest(step=step, nth=nth, cut=cut):
                    self.log.reset_mock()
                    self.prepared(events)
                    faults = _FaultyTmp(self, step, nth, cut)
                    with mock.patch("stakeout_progress.open", faults, create=True), \
                            mock.patch.object(os, "rename", faults.rename):
                        if cut:
                            with self.assertRaises(PowerCut):
                                self.load(capacity=yes)
                        else:
                            fold = self.load(capacity=yes)
                    self.assertGreaterEqual(
                        faults.calls.get("read" if step == "digest" else step, 0), nth)
                    if not cut:
                        # The process lives on: the old file and fold stay.
                        self.assertEqual(summary(fold), expected)
                        self.assertEqual(self.read_file(), old)
                        self.assertFalse(os.path.exists(self.path(suffix=".tmp")))
                        self.assertEqual(len(self.warnings()), 1)
                        self.assertIsNone(self.progress.last_compaction)
                        self.assertIsNone(self.store.uncertain)
                    else:
                        # Restart on the folder as the power cut left it.
                        shutil.rmtree(os.path.join(self.root, PID))
                        shutil.move(self.crash, os.path.join(self.root, PID))
                    self.restart()
                    fold = self.load()
                    self.assertFalse(os.path.exists(self.path(suffix=".tmp")))
                    self.assertIn(self.read_file(), (old, new))
                    if step == "renamed":
                        self.assertEqual(self.read_file(), new)
                    self.assertEqual(summary(fold), expected)
                    self.assertIsNone(self.store.uncertain)

    def test_compaction_paces_its_io(self):
        events = many()
        self.prepared(events)
        sequence = []
        sizes = []
        meters = []
        real_pause = stakeout._pause
        real_rename = os.rename

        async def pause(meter):
            sequence.append("pause")
            await real_pause(meter)

        class Meter(stakeout._BlockMeter):
            def __init__(self):
                super().__init__()
                meters.append(self)

        class Handle:
            def __init__(self, real, writing):
                self.real = real
                self.writing = writing

            def write(self, data):
                sequence.append("write")
                sizes.append(len(data))
                return self.real.write(data)

            def flush(self):
                sequence.append("flush")
                return self.real.flush()

            def close(self):
                if self.writing:
                    sequence.append("close")
                return self.real.close()

            def read(self, *args):
                data = self.real.read(*args)
                if data:
                    sequence.append("read")
                return data

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.close()

        def opener(path, mode="r"):
            if not path.endswith(".tmp"):
                return open(path, mode)
            if "w" in mode:
                sequence.append("open")
            return Handle(open(path, mode), "w" in mode)

        def rename(source, target):
            sequence.append("rename")
            real_rename(source, target)
        with mock.patch("stakeout_progress.open", opener, create=True), \
                mock.patch.object(os, "rename", rename), \
                mock.patch.object(stakeout, "_pause", pause), \
                mock.patch.object(stakeout, "_BlockMeter", Meter):
            fold = self.load(capacity=yes)
        after = os.path.getsize(self.path())
        self.assertEqual(fold.size, after)
        self.assertEqual(sum(sizes), after)
        self.assertLessEqual(max(sizes), 4096)
        self.assertGreaterEqual(sequence.count("write"), after // 4096 + 1)
        self.assertGreaterEqual(sequence.count("read"), after // 4096 + 1)
        # Every flash step and every read-back block ends its block.
        steps = sequence
        for index, item in enumerate(steps):
            if item != "pause":
                self.assertEqual(steps[index + 1], "pause", (index, steps[index - 3:index + 3]))
        rename_at = steps.index("rename")
        self.assertEqual(steps[rename_at - 1], "pause")
        for meter in meters:
            self.assertLess(meter.lap(), 250)
        self.assertLess(self.progress.last_compaction["longest_block_ms"], 250)


if __name__ == "__main__":
    unittest.main()
