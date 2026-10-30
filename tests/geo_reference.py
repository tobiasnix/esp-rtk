# SPDX-License-Identifier: AGPL-3.0-only
# Frozen reference of 08c5928 for #36, never edited.
"""Verbatim copy of the NMEA number parsers of geo.py at 08c5928.

tests/test_geo_parser_equivalence.py compares the current geo parsers with
these functions on a large corpus, so that the faster parsers of #36 return
exactly the same value (None included) for every input. The constants are
copied too, so the reference does not follow later changes of geo.py.
"""

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
    """
    if not isinstance(text, str) or len(text) > _MAX_TEXT:
        return None
    sign = text[:1]
    if sign == "+" or sign == "-":
        text = text[1:]
    whole, _, fraction = text.partition(".")
    # At least one digit, and nothing but digits around the single point.
    if not _digits(whole + fraction):
        return None
    padded = fraction[:digits] + "0" * (digits - len(fraction))
    # The leading "0" keeps int() valid for ".5" with digits == 0.
    scaled = int("0" + whole + padded)
    if len(fraction) > digits and fraction[digits] >= "5":
        scaled += 1
    return -scaled if sign == "-" else scaled


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
    """
    if not isinstance(coord_str, str) or len(coord_str) > _MAX_TEXT:
        return None
    if hemi in ("N", "S"):
        limit = 90
    elif hemi in ("E", "W"):
        limit = 180
    else:
        return None
    head, _, fraction = coord_str.partition(".")
    degrees, minutes = head[:-2], head[-2:]
    # 1 to 3 degree digits, two minute digits, then the fraction: digits only.
    if not (1 <= len(degrees) <= 3) or not _digits(head + fraction):
        return None
    deg = int(degrees)
    minutes_num = int(minutes + fraction)
    # Two ASCII digits compare like the number, so no int() for the whole minutes.
    # The limit itself is valid, but only with zero minutes.
    if minutes >= "60" or deg > limit or (deg == limit and minutes_num):
        return None
    scale = COORD_SCALE
    if scale == 100000000 and len(fraction) <= 7:
        # The e8 scale is 10 ** 8. With k minute decimals the minutes are
        # minutes_num * 10 ** (8 - k) / 60 in e8 degrees, which is x / 6 for
        # x = minutes_num * 10 ** (7 - k). That x is at most 599999999, below
        # 2 ** 30 and so a MicroPython small int, and floor(x / 6 + 1/2) is
        # (x + 3) // 6: half up, in one division.
        minutes_e8 = (minutes_num * _POW10[7 - len(fraction)] + 3) // 6
    else:
        # The minutes in units of 1 / scale degrees are x = minutes_num * scale /
        # (60 * minutes_scale). Rounding half up once is floor(x + 1/2), which
        # over the common denominator 120 * minutes_scale needs integers only.
        minutes_scale = 10 ** len(fraction)
        minutes_e8 = ((2 * minutes_num * scale + 60 * minutes_scale)
                      // (120 * minutes_scale))
    value = deg * scale + minutes_e8
    return -value if hemi in ("S", "W") else value
