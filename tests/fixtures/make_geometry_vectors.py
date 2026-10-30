# SPDX-License-Identifier: AGPL-3.0-only
"""Independent Vincenty reference for tests/test_geo.py; run from the repository root.

    python3 tests/fixtures/make_geometry_vectors.py

Writes stakeout_geometry_vectors.json next to this script. Each vector is a
pair of integer e8 positions (1e-8 degree units, latitude first) with the
Vincenty inverse distance and initial azimuth on WGS84. The integer targets
are picked once by inverting the local scale series of geo.py (copied below,
so this script never imports the module under test); the reference values are
then computed from those integers alone. All positions are synthetic.
"""
import json
import math
import os

A = 6378137.0              # WGS84 semi-major axis in metres
F = 1 / 298.257223563      # WGS84 flattening
E8 = 10 ** 8               # e8 units per degree
ALT_MM = 100000            # constant altitude of the line vertices (100 m)
OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "stakeout_geometry_vectors.json")

REF_51N_10E = [5100000000, 1000000000]
REF_33_5S_151E = [-3350000000, 15100000000]
REF_50N_0_5W = [5000000000, -50000000]

# name, reference [lat_e8, lon_e8], intended offset (north, east) in metres.
VECTORS = (
    ("north_0_05m", REF_51N_10E, 0.05, 0.0),
    ("east_0_05m", REF_51N_10E, 0.0, 0.05),
    ("diag_1m", REF_51N_10E, 0.5 ** 0.5, 0.5 ** 0.5),
    ("north_100m", REF_51N_10E, 100.0, 0.0),
    ("east_100m", REF_51N_10E, 0.0, 100.0),
    ("sw_1km", REF_51N_10E, -900.0, -400.0),
    # The local model is 0.6 mm off Vincenty here. Its series error grows with
    # the distance and is largest towards the east (2.3 mm at 8.7 km, 51 N), so
    # keep this heading in step with the 2 mm tolerance in tests/test_geo.py.
    ("ne_8_7km", REF_51N_10E, 6000.0, 6300.0),
    ("south_hemisphere_1km", REF_33_5S_151E, -700.0, 700.0),
    ("west_of_greenwich_100m", REF_50N_0_5W, 60.0, -80.0),
)

# Ten legs of about 100 m, (north, east) in metres, heading roughly north-east.
LINE_LEGS = ((60.0, 80.0), (80.0, 60.0), (70.0, 70.0), (60.0, 80.0), (80.0, 60.0),
             (70.0, 70.0), (60.0, 80.0), (80.0, 60.0), (70.0, 70.0), (60.0, 80.0))


def vincenty_inverse(lat1_deg, lon1_deg, lat2_deg, lon2_deg):
    """Return (distance in metres, initial azimuth in degrees, 0 <= azimuth < 360) on WGS84.

    Vincenty's inverse formulae (Survey Review 23, 1975), iterated until the
    longitude on the auxiliary sphere changes by less than 1e-12 rad. One more
    pass then evaluates every term at the converged longitude, so a distance
    of a few metres does not keep the residual of the last iteration.
    """
    b = (1.0 - F) * A
    u1 = math.atan((1.0 - F) * math.tan(math.radians(lat1_deg)))
    u2 = math.atan((1.0 - F) * math.tan(math.radians(lat2_deg)))
    big_l = math.radians(lon2_deg - lon1_deg)
    sin_u1, cos_u1 = math.sin(u1), math.cos(u1)
    sin_u2, cos_u2 = math.sin(u2), math.cos(u2)
    lam = big_l
    converged = False
    for _ in range(200):
        sin_lam, cos_lam = math.sin(lam), math.cos(lam)
        y = cos_u2 * sin_lam
        x = cos_u1 * sin_u2 - sin_u1 * cos_u2 * cos_lam
        sin_sigma = math.sqrt(y * y + x * x)
        if sin_sigma == 0.0:
            return 0.0, 0.0  # coincident points
        cos_sigma = sin_u1 * sin_u2 + cos_u1 * cos_u2 * cos_lam
        sigma = math.atan2(sin_sigma, cos_sigma)
        sin_alpha = cos_u1 * cos_u2 * sin_lam / sin_sigma
        cos_sq_alpha = 1.0 - sin_alpha * sin_alpha
        # cos(2 * sigma_m); on the equatorial line cos_sq_alpha is 0 and the term is 0.
        cos_2sm = cos_sigma - 2.0 * sin_u1 * sin_u2 / cos_sq_alpha if cos_sq_alpha else 0.0
        if converged:
            break
        c = F / 16.0 * cos_sq_alpha * (4.0 + F * (4.0 - 3.0 * cos_sq_alpha))
        previous = lam
        lam = big_l + (1.0 - c) * F * sin_alpha * (
            sigma + c * sin_sigma * (cos_2sm + c * cos_sigma * (-1.0 + 2.0 * cos_2sm * cos_2sm)))
        converged = abs(lam - previous) < 1e-12
    else:
        raise ValueError("Vincenty inverse did not converge (nearly antipodal points)")
    u_sq = cos_sq_alpha * (A * A - b * b) / (b * b)
    big_a = 1.0 + u_sq / 16384.0 * (4096.0 + u_sq * (-768.0 + u_sq * (320.0 - 175.0 * u_sq)))
    big_b = u_sq / 1024.0 * (256.0 + u_sq * (-128.0 + u_sq * (74.0 - 47.0 * u_sq)))
    delta_sigma = big_b * sin_sigma * (cos_2sm + big_b / 4.0 * (
        cos_sigma * (-1.0 + 2.0 * cos_2sm * cos_2sm)
        - big_b / 6.0 * cos_2sm * (-3.0 + 4.0 * sin_sigma * sin_sigma)
        * (-3.0 + 4.0 * cos_2sm * cos_2sm)))
    distance = b * big_a * (sigma - delta_sigma)
    # For a vector shorter than about 2 m, x above is the difference of two
    # nearly equal products, so the azimuth is sensitive to the last libm ulp
    # of sin, cos, tan and atan (about 1e-7 degrees). The bearings stored in
    # the fixture for such vectors may therefore differ in the last digit when
    # this script is run on another platform; that is not a defect.
    return distance, math.degrees(math.atan2(y, x)) % 360.0


def metres_per_degree(lat_deg):
    """(metres per degree of latitude, metres per degree of longitude) at lat_deg.

    The same series as geo.local_scale, only used to choose integer targets.
    """
    phi = math.radians(lat_deg)
    m = 111132.954 - 559.822 * math.cos(2 * phi) + 1.175 * math.cos(4 * phi)
    p = 111412.84 * math.cos(phi) - 93.5 * math.cos(3 * phi) + 0.118 * math.cos(5 * phi)
    return m, p


def target_for(ref, north_m, east_m):
    """Integer [lat_e8, lon_e8] about (north_m, east_m) away from ref.

    The local scale is inverted once: latitude with M at the reference, then
    longitude with P at the mean latitude of the two points.
    """
    lat_ref = ref[0] / E8
    m, _ = metres_per_degree(lat_ref)
    dlat = round(north_m / m * E8)
    _, p = metres_per_degree(lat_ref + dlat / E8 / 2)
    dlon = round(east_m / p * E8)
    return [ref[0] + dlat, ref[1] + dlon]


def reference_values(ref, target):
    """Vincenty distance and azimuth for two e8 positions, rounded to 6 decimals."""
    distance, azimuth = vincenty_inverse(ref[0] / E8, ref[1] / E8, target[0] / E8, target[1] / E8)
    return round(distance, 6), round(azimuth, 6) % 360.0


def build():
    vectors = []
    for name, ref, north_m, east_m in VECTORS:
        target = target_for(ref, north_m, east_m)
        distance, azimuth = reference_values(ref, target)
        vectors.append({"name": name, "ref": ref, "target": target,
                        "distance_m": distance, "bearing_deg": azimuth})
    points = [REF_51N_10E]
    for north_m, east_m in LINE_LEGS:
        points.append(target_for(points[-1], north_m, east_m))
    length = 0.0
    for first, second in zip(points, points[1:]):
        length += vincenty_inverse(first[0] / E8, first[1] / E8,
                                   second[0] / E8, second[1] / E8)[0]
    return {
        "datum": "WGS84",
        "reference": "Vincenty inverse, tests/fixtures/make_geometry_vectors.py",
        "vectors": vectors,
        # Tracking coordinate order: [lon_e8, lat_e8, alt_mm].
        "line_1km": {"vertices": [[lon, lat, ALT_MM] for lat, lon in points],
                     "length_m": round(length, 6)},
    }


def render(fixture):
    """JSON text with one line per vector and per vertex, so a diff stays readable."""
    vectors = ",\n".join("    " + json.dumps(vector) for vector in fixture["vectors"])
    vertices = ",\n".join("      " + json.dumps(vertex)
                          for vertex in fixture["line_1km"]["vertices"])
    lines = ["{",
             '  "datum": %s,' % json.dumps(fixture["datum"]),
             '  "reference": %s,' % json.dumps(fixture["reference"]),
             '  "vectors": [',
             vectors,
             "  ],",
             '  "line_1km": {',
             '    "vertices": [',
             vertices,
             "    ],",
             '    "length_m": %s' % json.dumps(fixture["line_1km"]["length_m"]),
             "  }",
             "}"]
    return "\n".join(lines) + "\n"


def main():
    with open(OUTPUT, "w", encoding="utf-8") as handle:
        handle.write(render(build()))
    print("wrote %s" % os.path.relpath(OUTPUT))


if __name__ == "__main__":
    main()
