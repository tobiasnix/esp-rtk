# SPDX-License-Identifier: AGPL-3.0-only
"""A test clock with MicroPython tick semantics (period 2**30).

tests/support.py gives the real time module a ticks_diff that subtracts, for
the whole suite. Tests that need a tick counter that overflows patch a module's
``time`` attribute with a WrapClock instead:
``mock.patch.object(stakeout_session, "time", WrapClock(ms=2**30 - 400))``.
"""

TICKS_PERIOD = 2 ** 30
TICKS_HALF = 2 ** 29


class WrapClock:
    def __init__(self, ms=0, us=0):
        self.ms = ms % TICKS_PERIOD
        self.us = us % TICKS_PERIOD
        self.elapsed_ms = 0

    def ticks_ms(self):
        return self.ms

    def ticks_us(self):
        return self.us

    def ticks_add(self, ticks, delta):
        return (ticks + delta) % TICKS_PERIOD

    def ticks_diff(self, end, start):
        return ((end - start + TICKS_HALF) % TICKS_PERIOD) - TICKS_HALF

    def advance(self, ms):
        self.ms = (self.ms + ms) % TICKS_PERIOD
        self.us = (self.us + ms * 1000) % TICKS_PERIOD
        self.elapsed_ms += ms

    def time(self):
        """Seconds since the clock was made, as a float like time.time()."""
        return self.elapsed_ms / 1000.0
