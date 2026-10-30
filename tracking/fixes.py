# SPDX-License-Identifier: AGPL-3.0-only
"""The measurement gate, fix quality and averaging, setup stamps and line projection."""
import math
import geo
from cfg import CONFIG, CONFIG_WARNINGS, DEFAULT_CONFIG
import cfg_validation as _validation
from tracking.common import MEASUREMENT_GATE_KEYS
from tracking.validation import _exact_component_ok


# (config key, short key) per gate limit, in MEASUREMENT_GATE_KEYS order.
_GATE_FIELDS = tuple((key, key.replace("measurement_", ""))
                     for key in MEASUREMENT_GATE_KEYS)
# [generation, values, limits, result_limits] of the current CONFIG gate limits;
# values holds the CONFIG objects the dicts were built from.
_GATE = [0, None, None, None]


def _gate_current():
    """The shared gate limits of CONFIG, rebuilt when a limit changed.

    Returns [generation, values, limits, result_limits]: limits maps the short
    keys (with required_fix), result_limits is the limits part of a
    quality_check result (required_fix moved to fix_quality, last). Shared:
    callers never change them. Each call compares the eight CONFIG values by
    identity, so every way of writing CONFIG (PUT, restore, mock.patch.dict, a
    hand edit) invalidates without a hook, and an int replacing an equal float
    still shows up in the stored limits. The valid case allocates nothing; a
    rebuild bumps the generation, which voids every verdict judged before it.
    """
    gate = _GATE
    values = gate[1]
    if values is not None:
        index = 0
        for full_key, _short in _GATE_FIELDS:
            if CONFIG[full_key] is not values[index]:
                break
            index += 1
        else:
            return gate
    values = [CONFIG[full_key] for full_key, _short in _GATE_FIELDS]
    limits = {}
    index = 0
    for _full_key, short in _GATE_FIELDS:
        limits[short] = values[index]
        index += 1
    # The order quality_check results always had: required_fix leaves, its
    # value comes last as fix_quality.
    result_limits = dict(limits)
    result_limits["fix_quality"] = result_limits.pop("required_fix")
    gate[0] += 1
    gate[1], gate[2], gate[3] = values, limits, result_limits
    return gate


def measurement_gate(defaults=False):
    if defaults:
        return {key.replace("measurement_", ""): DEFAULT_CONFIG[key]
                for key in MEASUREMENT_GATE_KEYS}
    return dict(_gate_current()[2])


FIELD_SETUP_KEYS = _validation.FIELD_SETUP_KEYS


def field_setup(defaults=False):
    source = DEFAULT_CONFIG if defaults else CONFIG
    return {key: source[key] for key in FIELD_SETUP_KEYS}


# The validator lives in cfg_validation so the config loader can use the very same
# rules as /api/field-setup.
validate_field_setup = _validation.validate_field_setup


_FIX_ORDER = ("RTK_FIXED", "RTK_FLOAT", "DGPS")


def measurement_gate_deviations():
    """Gate limits that are looser than the defaults, in MEASUREMENT_GATE_KEYS order."""
    result = []
    for full_key in MEASUREMENT_GATE_KEYS:
        value, default = CONFIG[full_key], DEFAULT_CONFIG[full_key]
        if full_key == "measurement_required_fix":
            try:
                looser = _FIX_ORDER.index(value) > _FIX_ORDER.index(default)
            except ValueError:
                looser = False
        elif full_key in ("measurement_min_satellites",
                          "measurement_quality_window_samples"):
            looser = value < default
        else:
            looser = value > default
        if looser:
            result.append({"key": full_key.replace("measurement_", ""),
                           "value": value, "default": default})
    return result


def validate_measurement_gate(values):
    if not isinstance(values, dict):
        return None, "Measurement gate must be an object."
    allowed = set(key.replace("measurement_", "") for key in MEASUREMENT_GATE_KEYS)
    if set(values) != allowed:
        return None, "All measurement gate fields are required."
    required = values.get("required_fix")
    if required not in ("RTK_FIXED", "RTK_FLOAT", "DGPS"):
        return None, "required_fix must be RTK_FIXED, RTK_FLOAT or DGPS."
    ranges = {"min_satellites": (4, 60), "max_hdop": (0.1, 20.0),
              "max_correction_age_sec": (0.1, 120.0),
              "max_horizontal_spread_m": (0.001, 10.0),
              "max_gst_horizontal_sigma_m": (0.001, 10.0),
              "max_gst_vertical_sigma_m": (0.001, 20.0),
              "quality_window_samples": (3, 30)}
    clean = {"measurement_required_fix": required}
    for key, bounds in ranges.items():
        value = values.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None, "%s must be a number." % key
        value = float(value)
        if value != value or not bounds[0] <= value <= bounds[1]:
            return None, "%s must be between %s and %s." % (
                key, bounds[0], bounds[1])
        clean["measurement_" + key] = int(value) if key in (
            "min_satellites", "quality_window_samples") else value
    return clean, None


def _coordinate_text(coordinate):
    """A stored [lon_e8, lat_e8, alt_mm] triple as a GeoJSON array of exact decimal text."""
    return ("[" + geo.format_e8(coordinate[0]) + "," + geo.format_e8(coordinate[1]) +
            "," + geo.format_mm(coordinate[2]) + "]")


def _distance_m(a, b):
    """Horizontal distance in metres between two [lon_e8, lat_e8, alt_mm] triples."""
    if not a or not b:
        return 0.0
    return geo.distance_e8_m(a[1], a[0], b[1], b[0])


def _all_ints(fixes, *keys):
    """True when every fix holds an exact int under every key.

    None, floats and bools do not count: only a real int is an exact value.
    """
    return all(type(fix.get(key)) is int for fix in fixes for key in keys)


def receiver_sigma_stats(fixes):
    """Mean and maximum of the receiver's own GST sigmas over the given fixes.

    Only fixes with a receiver_accuracy count. The horizontal sigma is
    horizontal_sigma_m when that key is present, else semi_major_sigma_m;
    None and 0 are no estimate.
    A mean or maximum without any estimate is None.
    """
    estimates = [fix.get("receiver_accuracy") for fix in fixes
                 if fix.get("receiver_accuracy")]
    horizontal = [item.get("horizontal_sigma_m", item.get("semi_major_sigma_m"))
                  for item in estimates
                  if item.get("horizontal_sigma_m", item.get("semi_major_sigma_m"))
                  not in (None, 0)]
    vertical = [item.get("altitude_sigma_m") for item in estimates
                if item.get("altitude_sigma_m") not in (None, 0)]
    return {
        "samples": len(estimates),
        "horizontal_sigma_mean_m": (round(sum(horizontal) / len(horizontal), 4)
                                    if horizontal else None),
        "horizontal_sigma_max_m": round(max(horizontal), 4) if horizontal else None,
        "vertical_sigma_mean_m": (round(sum(vertical) / len(vertical), 4)
                                  if vertical else None),
        "vertical_sigma_max_m": round(max(vertical), 4) if vertical else None,
    }


def average_fixes(fixes):
    """Return a mean fix plus observed repeatability statistics.

    Every sample must carry the exact integers the storage keeps (lon_e8,
    lat_e8 and alt_mm, each a real int inside the storage bounds); one sample
    without them refuses the average. The mean, the spread and the float
    fields lat, lon, alt and altitude_msl_m all come from the integers, so
    identical samples have a spread of exactly zero.
    """
    usable = [fix for fix in fixes if fix]
    if not usable:
        raise ValueError("No GNSS position is available.")
    for fix in usable:
        for index, key in enumerate(("lon_e8", "lat_e8", "alt_mm")):
            if not _exact_component_ok(fix.get(key), index):
                raise ValueError("A GNSS sample has no exact position.")
    count = len(usable)
    mean = dict(usable[-1])
    mean["lat_e8"] = geo.mean_e8([fix["lat_e8"] for fix in usable])
    mean["lon_e8"] = geo.mean_lon_e8([fix["lon_e8"] for fix in usable])
    mean["lat"] = geo.e8_to_deg(mean["lat_e8"])
    mean["lon"] = geo.e8_to_deg(mean["lon_e8"])
    horizontal = [geo.distance_e8_m(mean["lat_e8"], mean["lon_e8"],
                                    fix["lat_e8"], fix["lon_e8"]) for fix in usable]
    mean["alt_mm"] = geo.mean_e8([fix["alt_mm"] for fix in usable])
    mean["alt"] = mean["alt_mm"] / geo.ALT_SCALE
    vertical = [abs(fix["alt_mm"] - mean["alt_mm"]) / geo.ALT_SCALE
                for fix in usable]
    mean["altitude_msl_m"] = mean["alt"]
    # The geoid separation is averaged like the height, and the legacy float
    # fields are derived from the integer means, so they never describe only
    # the last sample. One sample without GGA field 11 leaves the mean without
    # a separation and without the two floats.
    if _all_ints(usable, "geoid_sep_mm"):
        mean["geoid_sep_mm"] = geo.mean_e8([fix["geoid_sep_mm"] for fix in usable])
        mean["geoid_sep_m"] = mean["geoid_sep_mm"] / geo.ALT_SCALE
        mean["altitude_ellipsoid_m"] = round(mean["alt"] + mean["geoid_sep_m"], 3)
    else:
        for key in ("geoid_sep_mm", "geoid_sep_m", "altitude_ellipsoid_m"):
            mean.pop(key, None)
    required_fix = _gate_current()[2]["required_fix"]
    accepted_qualities = {"RTK_FIXED": (4,), "RTK_FLOAT": (4, 5),
                          "DGPS": (2, 4, 5)}[required_fix]
    mean["accuracy_observation"] = {
        "samples": count,
        "rtk_fixed_samples": sum(1 for fix in usable if fix.get("qual") == 4),
        "accepted_fix_samples": sum(1 for fix in usable
                                    if fix.get("qual") in accepted_qualities),
        "horizontal_mean_deviation_m": round(sum(horizontal) / count, 4),
        "horizontal_max_deviation_m": round(max(horizontal), 4),
        "vertical_mean_deviation_m": round(sum(vertical) / count, 4),
        "vertical_max_deviation_m": round(max(vertical), 4),
    }
    sigma = receiver_sigma_stats(usable)
    mean["receiver_accuracy"] = {
        "source": "NMEA_GST", "samples": sigma["samples"],
        "expected_samples": count,
        "horizontal_sigma_mean_m": sigma["horizontal_sigma_mean_m"],
        "horizontal_sigma_max_m": sigma["horizontal_sigma_max_m"],
        "vertical_sigma_mean_m": sigma["vertical_sigma_mean_m"],
        "vertical_sigma_max_m": sigma["vertical_sigma_max_m"],
    }
    return mean


def quality_check(fix):
    # The shared limits are only read here; the result gets its own copy.
    gate = _gate_current()
    limits = gate[2]
    reasons = []
    # The exact fields decide, not the display floats: the gate must agree
    # with what the storage accepts (_exact_component_ok).
    if (not fix or not _exact_component_ok(fix.get("lon_e8"), 0) or
            not _exact_component_ok(fix.get("lat_e8"), 1)):
        reasons.append("no_position")
    qual = int((fix or {}).get("qual", 0))
    accepted_qualities = {"RTK_FIXED": (4,), "RTK_FLOAT": (4, 5),
                          "DGPS": (2, 4, 5)}[limits["required_fix"]]
    if qual not in accepted_qualities:
        reasons.append("rtk_fixed_required" if limits["required_fix"] == "RTK_FIXED"
                       else "required_fix_not_met")
    if int((fix or {}).get("sats", 0)) < limits["min_satellites"]:
        reasons.append("too_few_satellites")
    hdop = (fix or {}).get("hdop")
    if hdop is None or float(hdop) > limits["max_hdop"]:
        reasons.append("hdop_too_high")
    if not _exact_component_ok((fix or {}).get("alt_mm"), 2):
        reasons.append("altitude_required")
    age = (fix or {}).get("correction_age_sec")
    if age is not None and float(age) > limits["max_correction_age_sec"]:
        reasons.append("corrections_too_old")
    observed = (fix or {}).get("accuracy_observation") or {}
    if observed:
        if (limits["required_fix"] == "RTK_FIXED" and
                observed.get("rtk_fixed_samples", 0) != observed.get("samples", 0)):
            reasons.append("not_all_samples_rtk_fixed")
        if (observed.get("accepted_fix_samples") is not None and
                observed.get("accepted_fix_samples") != observed.get("samples")):
            reasons.append("required_fix_not_met")
        if observed.get("horizontal_max_deviation_m", 999) > limits["max_horizontal_spread_m"]:
            reasons.append("horizontal_spread_too_high")
    receiver = (fix or {}).get("receiver_accuracy") or {}
    horizontal_sigma = receiver.get("horizontal_sigma_max_m",
                                    receiver.get("horizontal_sigma_m",
                                                 receiver.get("semi_major_sigma_m")))
    vertical_sigma = receiver.get("vertical_sigma_max_m",
                                  receiver.get("altitude_sigma_m"))
    if not receiver or horizontal_sigma is None or vertical_sigma is None:
        reasons.append("gst_required")
    else:
        if receiver.get("expected_samples") is not None and receiver.get("samples") != receiver.get("expected_samples"):
            reasons.append("not_all_samples_have_gst")
        if float(horizontal_sigma) > limits["max_gst_horizontal_sigma_m"]:
            reasons.append("gst_horizontal_error_too_high")
        if float(vertical_sigma) > limits["max_gst_vertical_sigma_m"]:
            reasons.append("gst_vertical_error_too_high")
    return {"accepted": not reasons, "reasons": reasons, "limits": dict(gate[3])}


_antenna_warned = [None]


def _configured_antenna_height_mm():
    """The configured antenna height in integer millimetres, None when invalid.

    The value comes from config.json, which can be edited by hand, so it gets
    the same rule as the field-setup endpoint (finite, 0 to 3 m). An invalid
    value stamps None, so the ground height stays empty instead of wrong, and
    is logged once per distinct value. An unset value (None) means 0.
    """
    # The loader replaced a rejected value by the default 0.0, which would read
    # as a configured zero; the load warning keeps the stamp empty until the
    # user saves a valid field setup (the endpoint clears the warning).
    if any(w.get("key") == "antenna_height_m" for w in CONFIG_WARNINGS):
        return None
    value = CONFIG.get("antenna_height_m")
    if value is None:
        return 0
    clean, _error = validate_field_setup({"antenna_height_m": value})
    if clean is not None:
        return int(round(clean["antenna_height_m"] * 1000))
    marker = repr(value)
    if _antenna_warned[0] != marker:
        _antenna_warned[0] = marker
        try:
            from state import app
            app.log_error("TRACK", "Invalid antenna_height_m %s; the height is not stamped." % marker[:40])
        except (ImportError, AttributeError):
            pass
    return None


def _setup_stamps():
    """What a measurement reads from the configuration, taken at one moment.

    A fix waits in the route queue before it is recorded; the stamps are taken
    when it is queued so that a setting changed in between does not apply to it.
    """
    return {"antenna_height_mm": _configured_antenna_height_mm(),
            "reference_frame": CONFIG.get("reference_frame"),
            "reference_frame_epoch": CONFIG.get("reference_frame_epoch"),
            "ntrip_mount": _ntrip_mount()}


def _reference_frame(fix):
    """Reference frame of a position: the configured one for RTK, WGS84/ITRF otherwise.

    Fix qualities 4 and 5 (RTK) refer to the frame of the correction service,
    which only the operator knows, so the configured text is used, or None when
    it is empty. Any other fix is a plain GNSS position in WGS84/ITRF. Quality
    0 is no position, so there is no frame.
    """
    quality = int(fix.get("qual", 0))
    if quality in (4, 5):
        # Clamped to what _validate_measurement accepts, so that a long value from
        # config.json or the REPL cannot make every backup unrestorable.
        stamps = fix.get("_stamps")
        text = stamps["reference_frame"] if stamps else CONFIG.get("reference_frame")
        text = str(text).strip() if text else ""
        return text[:80] or None
    return "WGS84/ITRF" if quality else None


def _reference_frame_epoch(fix):
    """Epoch of the configured reference frame; only RTK positions have one."""
    if int(fix.get("qual", 0)) in (4, 5):
        stamps = fix.get("_stamps")
        text = stamps["reference_frame_epoch"] if stamps else CONFIG.get("reference_frame_epoch")
        text = str(text).strip() if text else ""
        return text[:80] or None
    return None


def _ntrip_mount():
    """Mount point of the caster in use while corrections stream, otherwise None.

    The fallback caster has its own mount, so the endpoint decides which
    configured mount is meant. Independent of the fix quality: a mount is
    recorded whenever the stream is up.
    """
    try:
        from state import app
        if app.stats.get("ntrip_state") != "streaming":
            return None
        fallback = app.stats.get("ntrip_endpoint") == "fallback"
    except (ImportError, AttributeError):
        return None
    return CONFIG.get("ntrip_fallback_mount" if fallback else "ntrip_mount") or None


_HALF_TURN_E8 = 180 * geo.COORD_SCALE
_FULL_TURN_E8 = 2 * _HALF_TURN_E8


def _wrap_lon_e8(value):
    """A longitude (difference) in e8 wrapped into -180..180 degrees, like geo.offsets_e8_m."""
    if value > _HALF_TURN_E8:
        return value - _FULL_TURN_E8
    if value < -_HALF_TURN_E8:
        return value + _FULL_TURN_E8
    return value


def project_to_line(coordinates, point):
    """Project a point onto a polyline and return chainage and signed offset.

    Coordinates and point are [lon_e8, lat_e8, alt_mm] triples. The geometry runs
    in metres from the exact integer differences, with the scale factors of each
    segment taken at its mean latitude (as _distance_m does), so the chainage at
    a vertex equals the sum of the segment distances up to it. The projected
    coordinate is an integer triple again. Longitude differences are wrapped at
    the antimeridian, so a line crossing it is metres long, not 360 degrees.
    """
    if not coordinates:
        return None
    if len(coordinates) == 1:
        return {"chainage_m": round(_distance_m(coordinates[0], point), 3),
                "lateral_offset_m": 0.0, "side": "on_line",
                "projected_coordinate": list(point[:3])}
    cumulative, best = 0.0, None
    for index, (a, b) in enumerate(zip(coordinates, coordinates[1:])):
        scale_y, scale_x = geo.local_scale((a[1] + b[1]) / (2 * geo.COORD_SCALE))
        ax = _wrap_lon_e8(a[0] - point[0]) * scale_x / geo.COORD_SCALE
        ay = (a[1] - point[1]) * scale_y / geo.COORD_SCALE
        dx = _wrap_lon_e8(b[0] - a[0]) * scale_x / geo.COORD_SCALE
        dy = (b[1] - a[1]) * scale_y / geo.COORD_SCALE
        length_sq = dx * dx + dy * dy
        if not length_sq:
            continue
        raw_t = -(ax * dx + ay * dy) / length_sq
        t = max(0.0, raw_t)
        if index < len(coordinates) - 2:
            t = min(1.0, t)
        px, py = ax + t * dx, ay + t * dy
        distance = math.sqrt(px * px + py * py)
        segment_length = math.sqrt(length_sq)
        cross = dx * (-ay) - dy * (-ax)
        candidate = {"chainage_m": cumulative + t * segment_length,
                     "lateral_offset_m": distance,
                     "side": "left" if cross > 0 else "right" if cross < 0 else "on_line",
                     "projected_coordinate": [
                         _wrap_lon_e8(point[0] + int(round(px / scale_x * geo.COORD_SCALE))),
                         point[1] + int(round(py / scale_y * geo.COORD_SCALE)),
                         a[2] + int(round(t * (b[2] - a[2])))]}
        if best is None or distance < best[0]:
            best = (distance, candidate)
        cumulative += segment_length
    if best is None:
        return None
    result = best[1]
    result["chainage_m"] = round(result["chainage_m"], 3)
    result["lateral_offset_m"] = round(result["lateral_offset_m"], 3)
    return result
