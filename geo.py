# SPDX-License-Identifier: AGPL-3.0-only
"""Exact integer positions: NMEA text to e8 degrees and millimetres, and local geometry.

Unit convention: latitude and longitude are integers in 1e-8 degrees ("e8",
COORD_SCALE units per degree, about 1.1 mm) and altitudes are integers in
millimetres (ALT_SCALE units per metre). MicroPython's float has a 24-bit
mantissa, which resolves a latitude only to about 0.4 m, so the receiver's
decimal text is converted with integer arithmetic and never becomes a float.

The e8 offsets and distances use the exact integer differences of two
positions; floats appear only in the scale factors and in the resulting metres.

The integers become exact decimal text again through format_fixed, format_e8
and format_mm (for exports), and legacy_coordinate_to_e8 converts the float
[lon, lat, alt] triples of older firmware at the storage boundary.
"""
import math

COORD_DIGITS = 8
# COORD_SCALE is the unit of every ``*_e8`` field (fixes, measurement means,
# API responses). Changing it, for example to 10 ** 7 as the e7 fallback, is a
# wire-format change that renames those fields, not a runtime switch.
COORD_SCALE = 10 ** COORD_DIGITS
ALT_DIGITS = 3
ALT_SCALE = 10 ** ALT_DIGITS
# NMEA fields are at most a dozen characters. Longer text is rejected before
# int() sees it, because CPython's int() raises ValueError above 4300 digits.
_MAX_TEXT = 32
# Powers of ten for the minute conversion in small ints (see parse_nmea_coord_e8).
_POW10 = (1, 10, 100, 1000, 10000, 100000, 1000000, 10000000)
# legacy_coordinate_to_e8 compares values against it to reject infinities.
_INF = float("inf")


# str.isdigit runs in C, where the former character loop cost over a millisecond
# per GGA on the ESP32. MicroPython's isdigit is ASCII-only, so it is the whole
# check there. CPython's accepts more (Unicode digits such as "\u0661" or "\u00b2",
# which its int() also converts), so there ASCII is checked in addition.
if hasattr("", "isascii"):
    def _digits(text):
        """True when text is non-empty and every character is in "0".."9"."""
        return text.isdigit() and text.isascii()
else:
    def _digits(text):
        """True when text is non-empty and every character is in "0".."9"."""
        return text.isdigit()


def parse_decimal_fixed(text, digits):
    """Parse a decimal string into an integer scaled by 10 ** digits.

    Accepts an optional leading sign, ASCII digits and at most one decimal
    point ("12." and ".5" are valid). Fraction digits beyond ``digits`` are
    rounded half up on the magnitude. Returns None for malformed input,
    including text longer than 32 characters.

    Allocations per call (#36): one replace (none without a point) and one
    slice only for a signed number; the scaling and the rounding are integer
    arithmetic on the single int() of the digits.
    """
    if not isinstance(text, str) or len(text) > _MAX_TEXT:
        return None
    negative = text.startswith("-")
    if negative or text.startswith("+"):
        text = text[1:]
    point = text.find(".")
    digits_text = text.replace(".", "") if point >= 0 else text
    # At least one digit, and nothing but digits around the single point.
    if len(text) - len(digits_text) > 1 or not _digits(digits_text):
        return None
    value = int(digits_text)
    fraction = len(text) - point - 1 if point >= 0 else 0
    if fraction == digits:
        # The common case (545.412 at 3 digits): the digits are the result.
        scaled = value
    elif fraction < digits:
        scaled = value * 10 ** (digits - fraction)
    else:
        # Dropping the extra fraction digits: the remainder is at least half
        # of the divisor exactly when the first dropped digit is 5 or more.
        divisor = 10 ** (fraction - digits)
        scaled = value // divisor
        if (value - scaled * divisor) * 2 >= divisor:
            scaled += 1
    return -scaled if negative else scaled


def parse_nmea_coord_e8(coord_str, hemi):
    """Convert an NMEA ``(d)ddmm.mmmm`` coordinate to integer e8 degrees.

    The two characters before the decimal point are the whole minutes and
    everything in front of them is the degrees (1 to 3 digits), so the split
    does not depend on the number of minute decimals. The minute string is
    kept as one integer and divided once, rounding half up; at the e8 scale
    with up to seven minute decimals that division stays in small ints, which
    spares the ESP32 most of its bigint arithmetic. NMEA carries no sign:
    south and west are negative through the hemisphere. Returns None
    for malformed input (any sign or blank included, or more than 32
    characters), minutes of 60 or more, or a position beyond 90 degrees (N/S)
    or 180 degrees (E/W).

    Allocations per call (#36): one replace (none without a point) and two
    slices, the degrees and the minutes of the text without its point; the
    whole minutes are checked on the minute integer, not on a third slice.
    """
    if not isinstance(coord_str, str) or len(coord_str) > _MAX_TEXT:
        return None
    if hemi in ("N", "S"):
        limit = 90
    elif hemi in ("E", "W"):
        limit = 180
    else:
        return None
    point = coord_str.find(".")
    digits_text = coord_str.replace(".", "") if point >= 0 else coord_str
    # At most one point, and digits only around it.
    if len(coord_str) - len(digits_text) > 1 or not _digits(digits_text):
        return None
    # 1 to 3 degree digits and two minute digits in front of the point.
    head = point if point >= 0 else len(coord_str)
    if not 3 <= head <= 5:
        return None
    fraction = len(coord_str) - head - 1 if point >= 0 else 0
    deg = int(digits_text[:head - 2])
    minutes_num = int(digits_text[head - 2:])
    # The whole minutes are minutes_num without its fraction digits. The limit
    # itself is valid, but only with zero minutes.
    if (minutes_num // 10 ** fraction >= 60 or deg > limit
            or (deg == limit and minutes_num)):
        return None
    scale = COORD_SCALE
    if scale == 100000000 and fraction <= 7:
        # The e8 scale is 10 ** 8. With k minute decimals the minutes are
        # minutes_num * 10 ** (8 - k) / 60 in e8 degrees, which is x / 6 for
        # x = minutes_num * 10 ** (7 - k). That x is at most 599999999, below
        # 2 ** 30 and so a MicroPython small int, and floor(x / 6 + 1/2) is
        # (x + 3) // 6: half up, in one division.
        minutes_e8 = (minutes_num * _POW10[7 - fraction] + 3) // 6
    else:
        # The minutes in units of 1 / scale degrees are x = minutes_num * scale /
        # (60 * minutes_scale). Rounding half up once is floor(x + 1/2), which
        # over the common denominator 120 * minutes_scale needs integers only.
        minutes_scale = 10 ** fraction
        minutes_e8 = ((2 * minutes_num * scale + 60 * minutes_scale)
                      // (120 * minutes_scale))
    value = deg * scale + minutes_e8
    return -value if hemi in ("S", "W") else value


def format_fixed(value, digits):
    """Format an integer scaled by 10 ** digits as decimal text.

    The inverse of parse_decimal_fixed: exactly ``digits`` fraction digits and
    no decimal point when ``digits`` is 0 (5112345678 at 8 digits is
    "51.12345678", 5 at 3 digits is "0.005"). The sign is read before the
    value is split, so a negative value below one unit keeps its minus
    ("-0.00000005") and zero never gets one. Integers only, so the text is
    exact for any realistic size (CPython refuses int to text conversions
    beyond 4300 digits). Raises TypeError for anything but an int, a bool
    included: a non-integer here is a unit mix-up at the call site.
    ``digits`` must be a non-negative int (a bool is refused); ValueError
    otherwise.
    """
    if type(value) is bool or not isinstance(value, int):
        raise TypeError("format_fixed needs an int")
    if type(digits) is not int or digits < 0:
        raise ValueError("digits must be a non-negative int")
    negative = value < 0
    magnitude = -value if negative else value
    whole, fraction = divmod(magnitude, 10 ** digits)
    text = "%d" % whole
    if digits:
        # The device's str has no zfill or rjust: pad by concatenation.
        fraction_text = "%d" % fraction
        text += "." + "0" * (digits - len(fraction_text)) + fraction_text
    return "-" + text if negative else text


def format_e8(value):
    """Integer e8 degrees to decimal degrees with COORD_DIGITS (8) fraction digits."""
    return format_fixed(value, COORD_DIGITS)


def format_mm(value):
    """Integer millimetres to decimal metres with three fraction digits.

    The digit count is ALT_DIGITS (3); ALT_SCALE is 10 ** ALT_DIGITS.
    """
    return format_fixed(value, ALT_DIGITS)


def local_scale(lat_deg):
    """Return (metres per degree of latitude, metres per degree of longitude).

    The WGS84 series for the length of one degree at latitude ``lat_deg``
    (in degrees): M = 111132.954 - 559.822 cos 2phi + 1.175 cos 4phi and
    P = 111412.84 cos phi - 93.5 cos 3phi + 0.118 cos 5phi.
    """
    phi = math.radians(lat_deg)
    m = 111132.954 - 559.822 * math.cos(2 * phi) + 1.175 * math.cos(4 * phi)
    p = 111412.84 * math.cos(phi) - 93.5 * math.cos(3 * phi) + 0.118 * math.cos(5 * phi)
    return m, p


def offsets_e8_m(lat_ref_e8, lon_ref_e8, lat_e8, lon_e8):
    """Return (north_m, east_m): the target minus the reference, in metres.

    Both positions are integers in e8 degrees. The differences are exact
    integers; the scale is taken at the mean latitude of the two positions.
    Meant for local distances: within a few millimetres of Vincenty's inverse
    formula up to 10 km. The longitude difference is wrapped to the shortest
    signed interval, so two positions on opposite sides of the antimeridian
    are close together, not 360 degrees apart.
    """
    m, p = local_scale((lat_ref_e8 + lat_e8) / (2 * COORD_SCALE))
    dlon = lon_e8 - lon_ref_e8
    half = 180 * COORD_SCALE
    if dlon > half:
        dlon -= 2 * half
    elif dlon < -half:
        dlon += 2 * half
    return (lat_e8 - lat_ref_e8) * m / COORD_SCALE, dlon * p / COORD_SCALE


def offsets_deg_m(lat_ref, lon_ref, lat, lon):
    """Return (north_m, east_m) for float degrees, for legacy data.

    The same model as offsets_e8_m, including the wrap of the longitude
    difference at the antimeridian. On the device the inputs are 32-bit
    floats, which resolve a latitude only to about 0.4 m.
    """
    m, p = local_scale((lat_ref + lat) / 2)
    dlon = lon - lon_ref
    if dlon > 180.0:
        dlon -= 360.0
    elif dlon < -180.0:
        dlon += 360.0
    return (lat - lat_ref) * m, dlon * p


def distance_e8_m(lat1_e8, lon1_e8, lat2_e8, lon2_e8):
    """Distance in metres between two e8 positions, as a float."""
    north, east = offsets_e8_m(lat1_e8, lon1_e8, lat2_e8, lon2_e8)
    return math.sqrt(north * north + east * east)


def distance_deg_m(lat1, lon1, lat2, lon2):
    """Distance in metres between two float-degree positions, for legacy data."""
    north, east = offsets_deg_m(lat1, lon1, lat2, lon2)
    return math.sqrt(north * north + east * east)


def bearing_deg(north_m, east_m):
    """Bearing of the offset (north_m, east_m) against geographic north, in [0, 360).

    atan2(east, north) in degrees, clockwise from north; None when both
    components are zero (no direction).
    """
    if north_m == 0 and east_m == 0:
        return None
    bearing = math.degrees(math.atan2(east_m, north_m))
    if bearing < 0:
        bearing += 360.0
    # A tiny negative angle rounds up to exactly 360.0 when 360 is added.
    return 0.0 if bearing >= 360.0 else bearing


def mean_e8(values):
    """Integer mean of integers, rounded half up towards +infinity.

    For e8 coordinates and millimetre altitudes alike. Raises ValueError for
    an empty sequence.
    """
    total = 0
    count = 0
    for value in values:
        total += value
        count += 1
    if not count:
        raise ValueError("mean of an empty sequence")
    # divmod floors, so 0 <= remainder < count even for a negative total; a
    # remainder of half the count or more rounds up (a tie goes to +infinity).
    quotient, remainder = divmod(total, count)
    return quotient + 1 if 2 * remainder >= count else quotient


def mean_lon_e8(values):
    """Integer mean of longitudes in e8 degrees, correct across the antimeridian.

    Every value is first moved by a whole turn to within 180 degrees of the
    first value, so samples on both sides of the antimeridian average on the
    short arc; mean_e8 then rounds as usual, and the result is brought back
    into the half-open interval (-180, 180] degrees. Raises ValueError for an
    empty sequence.
    """
    values = list(values)
    if not values:
        raise ValueError("mean of an empty sequence")
    half = 180 * COORD_SCALE
    first = values[0]
    unwrapped = []
    for value in values:
        if value - first > half:
            value -= 2 * half
        elif value - first < -half:
            value += 2 * half
        unwrapped.append(value)
    mean = mean_e8(unwrapped)
    if mean > half:
        mean -= 2 * half
    elif mean <= -half:
        mean += 2 * half
    return mean


def _scale_float(value, scale):
    """round(value * scale) without a float product beyond 2**24 * 8.

    On the device a float is 32 bits: value * 1e8 for a latitude near 50
    degrees is about 5e9, where float32 values lie 512 apart, so the single
    product would move the result off the float's own grid by up to 256
    units. The whole part is scaled as an int (exact); value - whole is
    exact in any binary float (Sterbenz) and the remaining product stays
    below scale, whose float32 spacing is 8 for scale 1e8, so the device
    result is within 4 units of the exact value of its float input.
    """
    whole = int(value)
    return whole * scale + int(round((value - whole) * scale))


def deg_to_e8(value):
    """Float degrees to an integer in e8 degrees, for legacy data only.

    Exact on the host. On the device the argument is already a 32-bit float
    (a latitude resolved to about 0.4 m); the conversion adds at most 4 e8
    units (4 mm) to that, see _scale_float. New positions come from
    parse_nmea_coord_e8 instead.
    """
    return _scale_float(value, COORD_SCALE)


def e8_to_deg(value):
    """Integer e8 degrees to float degrees, for display and legacy consumers.

    The result on the device is a 32-bit float; do arithmetic on the integers.
    """
    return value / COORD_SCALE


def legacy_coordinate_to_e8(coordinate):
    """Convert a legacy [lon, lat, alt] triple to [lon_e8, lat_e8, alt_mm].

    ``coordinate`` is a list or tuple of three ints or floats, degrees,
    degrees and metres, as older firmware stored them. The result is a new
    list of three ints: the degrees go through deg_to_e8, the altitude is
    rounded to the nearest millimetre (ties follow Python's round()). Raises
    ValueError for anything else: another type or length, a bool, a value
    that is not finite, or a value beyond +-180 degrees longitude, +-90
    degrees latitude or -20000 to 100000 metres altitude (the limits
    themselves are valid). Like deg_to_e8 this is for legacy data only; on
    the device the floats were 32-bit already and each converted value is
    within 4 units of the exact value of that float.
    """
    if not isinstance(coordinate, (list, tuple)) or len(coordinate) != 3:
        raise ValueError("coordinate must be a list of three numbers")
    for value in coordinate:
        # bool is a subclass of int and would pass as 0 or 1.
        if type(value) is bool or not isinstance(value, (int, float)):
            raise ValueError("coordinate values must be numbers")
        # NaN is the only value that differs from itself. Kept explicit even
        # though the range test below also fails for NaN and the infinities.
        if value != value or abs(value) == _INF:
            raise ValueError("coordinate values must be finite")
    lon, lat, alt = coordinate
    if not (-180 <= lon <= 180 and -90 <= lat <= 90
            and -20000 <= alt <= 100000):
        raise ValueError("coordinate out of range")
    return [deg_to_e8(lon), deg_to_e8(lat), _scale_float(alt, ALT_SCALE)]
