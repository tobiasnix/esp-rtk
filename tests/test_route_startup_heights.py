# SPDX-License-Identifier: AGPL-3.0-only
"""The route guard reports ready only for fixes whose height is storable."""
import random
import unittest

from support import gnss
import tracking
from test_gnss_startup import position

ALT_LOW, ALT_HIGH = tracking._COORDINATE_BOUNDS[2][:2]


def settle(alt_mm, seconds=11):
    """Feed seconds of valid fixes with the given height; return (guard, last result)."""
    guard = gnss.RouteStartupGuard()
    result = None
    for second in range(seconds):
        result = guard.check(position(second, alt_mm=alt_mm), second * 1000, True)
    return guard, result


class TestRouteGuardHeights(unittest.TestCase):
    def test_route_guard_height_bounds_match_storage(self):
        self.assertEqual((gnss._ALT_FLOOR_MM, gnss._ALT_LIMIT_MM), (ALT_LOW, ALT_HIGH))
        for alt_mm in (ALT_LOW, ALT_LOW - 1, ALT_HIGH, ALT_HIGH + 1, 0):
            with self.subTest(alt_mm=alt_mm):
                _guard, result = settle(alt_mm)
                self.assertEqual(result, tracking._exact_component_ok(alt_mm, 2))
        # A ready guard drops back as soon as one fix is not storable.
        guard, result = settle(0)
        self.assertTrue(result)
        self.assertFalse(guard.check(position(11, alt_mm=ALT_HIGH + 1), 11000, True))
        self.assertEqual(guard.status()["reason"], "waiting_position")
        self.assertFalse(guard.ready)

    def test_route_guard_rejects_non_int_heights(self):
        for alt_mm in (545412.0, True, None, "545412", float("nan")):
            with self.subTest(alt_mm=alt_mm):
                guard, result = settle(alt_mm, 15)
                self.assertFalse(result)
                self.assertFalse(guard.ready)

    def test_route_guard_readiness_implies_a_storable_coordinate(self):
        rng = random.Random(5)
        edges = [ALT_LOW, ALT_LOW - 1, ALT_HIGH, ALT_HIGH + 1, 0]
        ready = 0
        for index in range(2000):
            alt_mm = (rng.choice(edges) if index % 10 == 0
                      else rng.randint(ALT_LOW - 1000, ALT_HIGH + 1000))
            fix = position(index % 60, alt_mm=alt_mm)
            guard = gnss.RouteStartupGuard()
            # Make the guard ready at a storable height first, then offer the candidate:
            # readiness is re-checked per fix, so it must follow the candidate's height.
            for second in range(11):
                guard.check(position(second), second * 1000, True)
            self.assertTrue(guard.ready)
            if guard.check(fix, 11000, True):
                ready += 1
                self.assertTrue(all(tracking._exact_component_ok(value, i) for i, value in
                                    enumerate((fix["lon_e8"], fix["lat_e8"], fix["alt_mm"]))))
        self.assertGreater(ready, 1000)


if __name__ == "__main__":
    unittest.main()
