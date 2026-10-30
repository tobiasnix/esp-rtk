# SPDX-License-Identifier: AGPL-3.0-only
"""System part of the runtime state: SystemState owns uptime, clock steps, loop lag, heartbeats, the error list and the device identity.
"""
import time


def clock_is_valid(tm=None):
    """True when the RTC holds a plausible UTC date (2024..2099). The one definition used by gnss and net."""
    if tm is None:
        tm = time.gmtime()
    return 2024 <= tm[0] <= 2099


class SystemState:
    def _init_system(self):
        # Uptime accumulator as whole seconds plus a millisecond remainder: both stay
        # small ints (no bigint past 2**30 ms), so RtcmCounter can stamp each message
        # from them without allocating (#20a).
        self._uptime_at = time.ticks_ms()
        self._uptime_s = 0
        self._uptime_rem_ms = 0
        # Counts RTC steps (RMC re-sync); caches derived from the wall clock compare it.
        self.clock_steps = 0
        # Largest event-loop lag since the field recorder last took it; None if no tick ran.
        self._loop_lag_window_max_ms = None
        self.errors = []
        # Signs of life of the long-running tasks; the watchdog feeds only as long as
        # everyone is fresh.
        self.heartbeat = {}
        self.identity = None

    def _advance_uptime(self):
        """Accumulate monotonic uptime across RTC corrections and tick wrap."""
        now = time.ticks_ms()
        rem = self._uptime_rem_ms + max(0, time.ticks_diff(now, self._uptime_at))
        self._uptime_at = now
        if rem >= 1000:
            self._uptime_s += rem // 1000
            rem %= 1000
        self._uptime_rem_ms = rem

    def uptime_ms(self):
        """Monotonic uptime in ms. A bigint after 12.4 days: for reports, not per message."""
        self._advance_uptime()
        return self._uptime_s * 1000 + self._uptime_rem_ms

    def uptime_seconds(self):
        """Monotonic uptime in whole seconds, always uptime_ms() // 1000."""
        self._advance_uptime()
        return self._uptime_s

    def beat(self, name):
        """Lebenszeichen eines Tasks."""
        self.heartbeat[name] = time.ticks_ms()

    def note_loop_lag(self, lag_ms):
        """Record one ticker measurement (ms beyond the tick, clamped to >= 0).

        Sets the current value and the boot maximum in stats and the maximum of the
        window the field recorder drains. Integer arithmetic only: it runs every 100 ms.
        """
        if lag_ms < 0:
            lag_ms = 0
        stats = self.stats
        stats["loop_lag_ms"] = lag_ms
        if lag_ms > stats["loop_lag_max_ms"]:
            stats["loop_lag_max_ms"] = lag_ms
        window = self._loop_lag_window_max_ms
        if window is None or lag_ms > window:
            self._loop_lag_window_max_ms = lag_ms

    def take_loop_lag_window_max(self):
        """Return the largest lag since the previous call and reset the window.

        None means no ticker measurement arrived in that time (a dead ticker shows
        as None, not as 0). A stall is attributed to the window in which the ticker
        first runs after it.
        """
        window = self._loop_lag_window_max_ms
        self._loop_lag_window_max_ms = None
        return window

    def log_error(self, source, error_msg):
        """Structured error logging for HTTP export (ring buffer)."""
        error_entry = {
            "time": self.uptime_seconds(),
            "source": source,
            "error": error_msg
        }
        self.errors.append(error_entry)
        if len(self.errors) > 10:
            self.errors.pop(0)


# Modular global references for the Ctrl-C-Cleanup: In V9.0, "if 'gnss' in locals()" ran
# into the void in the except block because gnss lived locally in main() - the cleanup
# was never executed.
_instances = {}
