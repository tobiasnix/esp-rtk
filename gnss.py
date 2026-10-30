# SPDX-License-Identifier: AGPL-3.0-only
"""GNSS handler and status LED. Reads NMEA from UART1, checks the price sum, puts the strings in the queue for BLE and draws the fix for status and LED from GGA.
"""
import math
import time
import ubinascii
import uasyncio as asyncio
from machine import Pin, UART, RTC

import geo
from cfg import CONFIG, FIX_STATUS, NMEA_WHITELIST, log
from cfg_logging import HOT_PATH_LOG_INTERVAL_MS, LogThrottle, throttle_note
from field_diagnostics import recorder as field_recorder
from state import app, clock_is_valid, shutdown_event


def _ticks_us():
    """Microsecond tick counter. CPython has no time.ticks_us, so use the wall clock there."""
    try:
        return time.ticks_us()
    except AttributeError:
        return int(time.time() * 1000000)


def _elapsed_us(started):
    """Microseconds since started (a _ticks_us value), never negative."""
    try:
        return max(0, time.ticks_diff(_ticks_us(), started))
    except (AttributeError, TypeError):
        return max(0, _ticks_us() - started)


# One GGA parse should stay well below this; stats["gga_parse_over_budget"]
# counts the parses that did not.
_GGA_PARSE_BUDGET_US = 2000


# ---------------------------------------------------------------------------
# COMMUNICATION CANNEL FOR THE RECOMMENDANT
# ---------------------------------------------------------------------------

# Just proprietary configuration sets. Everything that goes through here goes directly
# into the UART of the receiver - so the list remains short. $PQTM...Quectel (LC29H),
# $PAIR...Airoha/MediaTek subset
COMMAND_PREFIXES = ("$PQTM", "$PAIR")
COMMAND_MAX_LEN = 120

# UartTx (one writer for the receiver UART). UartTx reads these at run time.
# Every write in progress (an RTCM chunk from write_rtcm, a command from its
# first uart.write) must take a byte within UART_STALL_MS and finish within
# UART_REST_MAX_MS; both are checked before each further attempt. A command
# that has not begun after COMMAND_MAX_WAIT_MS is dropped as busy.
UART_RETRY_MS = 10
UART_STALL_MS = 1000
UART_REST_MAX_MS = 2000
COMMAND_MAX_WAIT_MS = 5000
COMMAND_POLL_MS = 20
COMMAND_QUEUE_MAX = 4
COMMAND_BUSY = "Receiver input busy with a correction frame; try again."
COMMAND_QUEUE_FULL = "Command queue full; try again."
COMMAND_INCOMPLETE = "UART write incomplete."

MAX_NMEA_BUFFER_BYTES = 32768
# Catch up after flash work while keeping each parser turn finite. The router
# and output workers must sustain the same sentence batch (see fanout.py).
NMEA_YIELD_INTERVAL = 16
CRC_RAW_CAPTURE_BYTES = 256

# They run through the same UART stream as NMEA but are not on the whitelist and would
# otherwise be discarded.
REPLY_MAX = 10
replies = []


def check_command(sentence):
    """Check a command for the recipient: (adjusted_set, none) or (none, message).
    """
    if not sentence:
        return None, "Empty command."
    if not isinstance(sentence, str):
        sentence = sentence.decode("ascii", "ignore")
    sentence = sentence.strip()
    if not sentence:
        return None, "Empty command."
    if len(sentence) > COMMAND_MAX_LEN:
        return None, "Command is too long (maximum %d characters)." % COMMAND_MAX_LEN

    # No second sentence in the same appeal - otherwise, behind a harmless order, an
    # arbitrary second one could be pushed.
    for character in sentence:
        if ord(character) < 0x20 or ord(character) > 0x7E:
            return None, "Command contains prohibited characters."

    if not any(sentence.startswith(p) for p in COMMAND_PREFIXES):
        return None, ("Only %s commands are allowed." % " and ".join(COMMAND_PREFIXES))

    if not GNSSHandler.check_nmea_checksum(sentence):
        return None, "Checksum mismatch."

    return sentence, None


def remember_reply(sentence):
    """Places a module response in the ring buffer."""
    replies.append(sentence)
    while len(replies) > REPLY_MAX:
        replies.pop(0)


class UartStalled(Exception):
    """An RTCM write took no byte for UART_STALL_MS or did not end within UART_REST_MAX_MS."""


class UartTx:
    """One writer for the receiver UART.

    Commands go out only at an RTCM frame boundary of the bytes actually
    written, or while no frame is open. A partial write is completed before
    anything else. An RTCM write that stalls ends the stream: its untaken bytes
    are dropped and write_rtcm/drain_rtcm raise UartStalled. pump() is the only
    caller of uart.write and never raises; an RTCM error belongs to the caller
    that wrote the chunk and is raised there exactly once. The frame cursor
    follows the length field only, without CRC: a corrupted length can misplace
    the boundary by at most one frame (1029 bytes).
    """

    def __init__(self, uart):
        self.uart = uart
        self.busy = False      # a command waits or is in progress, or a chunk is open
        self.counters = {
            "uart_writes": 0, "uart_bytes": 0, "uart_short_writes": 0,
            "uart_write_ms": 0, "uart_write_max_ms": 0, "uart_dropped_bytes": 0,
            "uart_commands_sent": 0, "uart_commands_deferred": 0,
            "uart_commands_dropped": 0, "uart_command_wait_max_ms": 0}
        # Frame cursor: _hdr 0 with _left 0 = search (a boundary), 1 = sync
        # byte seen, 2 = length high bits seen; _left = frame bytes still due.
        self._hdr = 0
        self._len_hi = 0
        self._left = 0
        # Waiting commands, oldest first: [payload, done, submitted_ms, text].
        self._queue = []
        # The command in progress (begun, not yet completely taken).
        self._cmd = None
        self._cmd_pos = 0
        self._cmd_start = 0
        self._cmd_progress = 0
        # The open RTCM chunk: _buf[_pos:_end] is not yet taken; the last write
        # offered _buf[..:_target] and _short says it took less (a rest).
        self._buf = None
        self._view = None
        self._pos = 0
        self._end = 0
        self._target = 0
        self._short = False
        self._start = 0
        self._progress = 0
        self._rtcm_error = None
        self._log = LogThrottle(HOT_PATH_LOG_INTERVAL_MS)

    def at_boundary(self):
        return self._hdr == 0 and self._left == 0

    def submit(self, sentence, done=None, now_ms=None):
        """Queue a checked command: (True, None), or (False, error) without done.

        done(ok, error, now_ms) runs exactly once per accepted command, possibly
        before submit returns. Without done the result is logged here.
        """
        checked, error = check_command(sentence)
        if error:
            return False, error
        if len(self._queue) >= COMMAND_QUEUE_MAX:
            return False, COMMAND_QUEUE_FULL
        now = time.ticks_ms() if now_ms is None else now_ms
        entry = [(checked + "\r\n").encode(), done, now, checked]
        self._queue.append(entry)
        self.pump(now)
        if self._queue and self._queue[-1] is entry:
            self.counters["uart_commands_deferred"] += 1
        return True, None

    async def command(self, sentence):
        """Submit and wait for the write result; a refusal returns at once."""
        result = []

        def done(ok, error, now_ms):
            result.append((ok, error))
        accepted, error = self.submit(sentence, done)
        if not accepted:
            return False, error
        # Ends through the deadlines: at most COMMAND_MAX_WAIT_MS +
        # UART_REST_MAX_MS + COMMAND_POLL_MS, unless one uart.write blocks.
        while not result:
            await asyncio.sleep_ms(COMMAND_POLL_MS)
            self.pump()
        return result[0]

    def write_rtcm(self, data):
        """Write one correction chunk; False = still open, await drain_rtcm() next.

        Synchronous on purpose: a fully taken chunk costs no coroutine object
        and uart.write gets the chunk itself.
        """
        if self._buf is not None:
            raise RuntimeError("RTCM chunk still open")
        self.counters["uart_writes"] += 1
        now = time.ticks_ms()
        self._buf = data
        self._pos = 0
        self._end = len(data)
        self._short = False
        self._start = self._progress = now
        self.pump(now)
        if self._rtcm_error is not None:
            self._raise_rtcm_error()
        return self._buf is None

    async def drain_rtcm(self):
        """Wait until the open chunk is taken or given up; raise its error once."""
        while self._buf is not None:
            await asyncio.sleep_ms(UART_RETRY_MS)
            self.pump()
        if self._rtcm_error is not None:
            self._raise_rtcm_error()

    def end_stream(self):
        """The correction stream ended: drop an open chunk, reset the cursor.

        A command in progress is completed; waiting commands may go out now.
        """
        if self._buf is not None:
            self.counters["uart_dropped_bytes"] += self._end - self._pos
            self._close_chunk()
        self._rtcm_error = None
        self._hdr = 0
        self._left = 0
        self.pump()

    def pump(self, now_ms=None):
        """Write what is due; True = nothing open. Never raises."""
        now = time.ticks_ms() if now_ms is None else now_ms
        if self._queue:
            self._drop_expired(now)
        if self._cmd is not None:
            self._write_command(now)
        while True:
            while (self._cmd is None and self._queue and not self._short
                   and self._hdr == 0 and self._left == 0):
                self._begin_command(now)
            if self._buf is None:
                break
            # Deadlines before every further attempt, never after it.
            if (time.ticks_diff(now, self._start) >= UART_REST_MAX_MS
                    or time.ticks_diff(now, self._progress) >= UART_STALL_MS):
                self._abort_chunk()
                break
            if self._cmd is not None or not self._write_chunk(now):
                break
        self.busy = not (self._buf is None and self._cmd is None and not self._queue)
        return not self.busy

    def _raise_rtcm_error(self):
        error = self._rtcm_error
        self._rtcm_error = None
        raise error

    def _write(self, part):
        """One uart.write: the number of bytes taken (None counts as 0)."""
        counters = self.counters
        started = time.ticks_ms()
        try:
            n = self.uart.write(part)
        finally:
            elapsed = max(0, time.ticks_diff(time.ticks_ms(), started))
            counters["uart_write_ms"] = elapsed
            if elapsed > counters["uart_write_max_ms"]:
                counters["uart_write_max_ms"] = elapsed
        size = len(part)
        if type(n) is not int or n < 0:
            n = 0
        elif n > size:
            n = size
        if n < size:
            counters["uart_short_writes"] += 1
        return n

    def _write_chunk(self, now):
        """Offer the next part of the open chunk; False = the UART took less."""
        buf, pos, end = self._buf, self._pos, self._end
        if pos >= end:
            self._close_chunk()
            return True
        if not self._short:
            # Waiting commands: stop at the next frame end so they can follow.
            self._target = self._frame_end(buf, pos, end) if self._queue else end
        target = self._target
        if pos == 0 and target == end:
            part = buf
        else:
            if self._view is None:
                self._view = memoryview(buf)
            part = self._view[pos:target]
        try:
            n = self._write(part)
        except Exception as e:
            self._rtcm_error = e
            self._close_chunk()
            return True
        if n:
            self._advance(buf, pos, pos + n)
            self._pos = pos = pos + n
            self._progress = now
            self.counters["uart_bytes"] += n
        if pos >= end:
            self._close_chunk()
            return True
        self._short = pos < target
        return not self._short

    def _close_chunk(self):
        self._buf = None
        self._view = None
        self._short = False

    def _abort_chunk(self):
        # The cursor stays where the taken bytes left it: only a real boundary
        # or end_stream() releases waiting commands.
        dropped = self._end - self._pos
        self.counters["uart_dropped_bytes"] += dropped
        self._close_chunk()
        message = "UART stalled; %d correction bytes dropped." % dropped
        self._rtcm_error = UartStalled(message)
        n = self._log.hit("stall")
        if n:
            log("WARN", "GNSS", message + throttle_note(n))

    def _advance(self, buf, pos, end):
        """Move the frame cursor over buf[pos:end], the bytes the UART took."""
        hdr, left, i = self._hdr, self._left, pos
        while i < end:
            if left:
                take = min(left, end - i)
                left -= take
                i += take
            elif hdr == 0:
                j = buf.find(b"\xd3", i, end)
                if j < 0:
                    i = end
                else:
                    i = j + 1
                    hdr = 1
            elif hdr == 1:
                b = buf[i]
                if b & 0xFC:
                    hdr = 0            # false sync: look at this byte again
                else:
                    self._len_hi = b & 3
                    i += 1
                    hdr = 2
            else:
                left = ((self._len_hi << 8) | buf[i]) + 3
                i += 1
                hdr = 0
        self._hdr, self._left = hdr, left

    def _frame_end(self, buf, pos, end):
        """First i > pos with the cursor at a boundary after buf[pos:i], else end.

        Same transitions as _advance, without changing the cursor.
        """
        hdr, len_hi, left = self._hdr, self._len_hi, self._left
        i = pos
        while i < end:
            if left:
                take = min(left, end - i)
                left -= take
                i += take
                if not left:
                    return i
            elif hdr == 0:
                if buf[i] != 0xD3:
                    return i + 1
                hdr = 1
                i += 1
            elif hdr == 1:
                b = buf[i]
                if b & 0xFC:
                    if b != 0xD3:
                        return i + 1   # false sync, this byte is outside a frame
                else:
                    len_hi = b & 3
                    hdr = 2
                i += 1
            else:
                left = ((len_hi << 8) | buf[i]) + 3
                hdr = 0
                i += 1
        return end

    def _drop_expired(self, now):
        # A command that has not begun in time is dropped, never written into a frame.
        queue, i = self._queue, 0
        while i < len(queue):
            entry = queue[i]
            if time.ticks_diff(now, entry[2]) >= COMMAND_MAX_WAIT_MS:
                queue.pop(i)
                self.counters["uart_commands_dropped"] += 1
                self._report(entry, False, COMMAND_BUSY, now)
            else:
                i += 1

    def _begin_command(self, now):
        entry = self._queue.pop(0)
        wait = max(0, time.ticks_diff(now, entry[2]))
        if wait > self.counters["uart_command_wait_max_ms"]:
            self.counters["uart_command_wait_max_ms"] = wait
        self._cmd = entry
        self._cmd_pos = 0
        self._cmd_start = self._cmd_progress = now
        self._write_command(now)

    def _write_command(self, now):
        if (time.ticks_diff(now, self._cmd_start) >= UART_REST_MAX_MS
                or time.ticks_diff(now, self._cmd_progress) >= UART_STALL_MS):
            self._finish_command(False, COMMAND_INCOMPLETE, now)
            return
        payload, pos = self._cmd[0], self._cmd_pos
        try:
            n = self._write(payload[pos:] if pos else payload)
        except Exception as e:
            self._finish_command(False, "UART error: %s" % e, now)
            return
        if n:
            self._cmd_pos = pos = pos + n
            self._cmd_progress = now
        if pos >= len(payload):
            self.counters["uart_commands_sent"] += 1
            self._finish_command(True, None, now)

    def _finish_command(self, ok, error, now):
        entry = self._cmd
        self._cmd = None
        self._report(entry, ok, error, now)

    def _report(self, entry, ok, error, now):
        done = entry[1]
        if done is not None:
            done(ok, error, now)
        elif ok:
            log("INFO", "GNSS", "Command sent to module: %s" % entry[3])
        else:
            n = self._log.hit("command")
            if n:
                log("WARN", "GNSS", "Command not sent: %s%s" % (error, throttle_note(n)))


class StatusLED:
    def __init__(self, pin_num=CONFIG["status_led_pin"]):
        self.led = Pin(pin_num, Pin.OUT)

    async def run(self):
        log("INFO", "GNSS", "Status LED task started (Pin %s)." % CONFIG["status_led_pin"])
        next_toggle = time.ticks_ms()
        while not shutdown_event.is_set():
            # Observing the age here latches an expired fix even when no client polls.
            # A silent GNSS UART must show "no fix" like /api/ui/live does, not the
            # colour of the last GGA.
            stale = app.fix_is_stale()
            fix = app.last_fix
            fix_qual = 0 if stale or not fix else fix["qual"]

            if fix_qual == 4:
                self.led.on()
                next_toggle = time.ticks_ms()
            else:
                interval = 200 if fix_qual == 0 else 1000
                now = time.ticks_ms()
                if time.ticks_diff(now, next_toggle) >= 0:
                    self.led.value(not self.led.value())
                    next_toggle = now + interval
            # Short slices: a pattern change is seen within 0.1 s, well inside
            # fix_stale_after_ms.
            await asyncio.sleep(0.1)
        self.led.off()
        log("INFO", "GNSS", "Status LED task stopped.")

# ---------------------------------------------------------------------------
# GNSS HANDLER
# ---------------------------------------------------------------------------

# Startup qualification is independent of the stricter survey quality gate.
# A route can start offline with ordinary GPS, but not with an unset clock or
# the first moving solution of a receiver cold start.
ROUTE_SETTLE_MS = 10000
ROUTE_SAMPLE_GAP_MS = 3000
ROUTE_STARTUP_MAX_SPEED_MPS = 60
GST_ENABLE_COMMAND = "$PQTMCFGMSGRATE,W,GST,1*0B"
GST_RETRY_MS = (2000, 5000, 15000, 60000)
# A GST sentence younger than this means the receiver is still sending them.
GST_FRESH_MS = 5000


# An RTC step is applied above this deviation from RMC, and at least this often.
CLOCK_RESYNC_DEVIATION_S = 1
CLOCK_RESYNC_INTERVAL_MS = 3600000


def _utc_seconds(tm):
    """Seconds since 2000-01-01 of (year, month, day, hour, minute, second), independent of the platform epoch/timezone."""
    year, month, day = tm[0], tm[1], tm[2]
    year -= month <= 2
    era_day = (153 * (month + (9 if month <= 2 else -3)) + 2) // 5 + day - 1
    days = 365 * year + year // 4 - year // 100 + year // 400 + era_day
    return (days - 730425) * 86400 + tm[3] * 3600 + tm[4] * 60 + tm[5]


def rmc_datetime(sentence):
    """Return a validated UTC RTC tuple from an active RMC solution."""
    try:
        fields = sentence.split("*", 1)[0].split(",")
        if fields[2] != "A":
            return None
        stamp, date = fields[1], fields[9]
        whole, dot, fraction = stamp.partition(".")
        if (len(whole) != 6 or not whole.isdigit() or len(date) != 6 or
                not date.isdigit() or (dot and not fraction.isdigit())):
            return None
        hour, minute, second = int(whole[:2]), int(whole[2:4]), int(whole[4:6])
        day, month, year = int(date[:2]), int(date[2:4]), 2000 + int(date[4:6])
        if not 2024 <= year <= 2099 or not 1 <= month <= 12:
            return None
        days = (31, 29 if year % 4 == 0 else 28, 31, 30, 31, 30,
                31, 31, 30, 31, 30, 31)
        if not 1 <= day <= days[month - 1] or hour > 23 or minute > 59 or second > 59:
            return None
        return (year, month, day, 0, hour, minute, second, 0)
    except (ValueError, TypeError, IndexError):
        return None


# Coordinate limits of the route guard: e8 degrees and millimetres of height.
# The height limits mirror tracking._COORDINATE_BOUNDS[2] (the storable range;
# a test pins them), so a route never reports ready for a point the store
# would reject. Precomputed: on the device the e8 values are heap bigints
# (small ints end at 2**30), and the guard runs for every GGA while a route
# settles.
_LON_LIMIT_E8 = 180 * geo.COORD_SCALE
_LAT_LIMIT_E8 = 90 * geo.COORD_SCALE
_LON_FLOOR_E8 = -_LON_LIMIT_E8
_LAT_FLOOR_E8 = -_LAT_LIMIT_E8
_ALT_FLOOR_MM = -20000 * geo.ALT_SCALE
_ALT_LIMIT_MM = 100000 * geo.ALT_SCALE


class RouteStartupGuard:
    def __init__(self):
        self.ready = False
        self.reason = "waiting_clock"
        self.stable_ms = 0
        self._since = None
        self._previous = None
        self._previous_at = None
        self._epoch = None

    def reset(self, reason):
        self.ready, self.reason, self.stable_ms = False, reason, 0
        self._since = self._previous = self._previous_at = self._epoch = None

    def check(self, fix, now, clock_ready):
        if not clock_ready:
            self.reset("waiting_clock")
            return False
        try:
            coordinate = (fix["lon_e8"], fix["lat_e8"], fix["alt_mm"])
            valid = (all(type(value) is int for value in coordinate) and
                     _LON_FLOOR_E8 <= coordinate[0] <= _LON_LIMIT_E8 and
                     _LAT_FLOOR_E8 <= coordinate[1] <= _LAT_LIMIT_E8 and
                     _ALT_FLOOR_MM <= coordinate[2] <= _ALT_LIMIT_MM and
                     fix.get("qual") in (1, 2, 4, 5))
        except (KeyError, ValueError, TypeError):
            valid = False
        if not valid:
            self.reset("waiting_position")
            return False
        gap = (time.ticks_diff(now, self._previous_at)
               if self._previous_at is not None else 0)
        if gap > ROUTE_SAMPLE_GAP_MS:
            self.reset("settling")
        if self.ready:
            self._previous_at = now
            return True
        try:
            usable = (fix.get("sats", 0) >= 6 and
                      0 < float(fix["hdop"]) <= 3)
        except (KeyError, ValueError, TypeError):
            usable = False
        epoch = fix.get("utc")
        if not usable or not epoch:
            self.reset("waiting_position")
            return False
        if epoch == self._epoch:
            return False
        if self._previous is not None:
            from tracking import _distance_m
            if gap <= 0 or _distance_m(self._previous, coordinate) > ROUTE_STARTUP_MAX_SPEED_MPS * gap / 1000:
                self.reset("settling")
        if self._since is None:
            self._since = now
        self._previous, self._previous_at, self._epoch = coordinate, now, epoch
        self.stable_ms = max(0, time.ticks_diff(now, self._since))
        self.ready = self.stable_ms >= ROUTE_SETTLE_MS
        self.reason = "ready" if self.ready else "settling"
        return self.ready

    def status(self):
        return {"ready": self.ready, "reason": self.reason,
                "stable_sec": self.stable_ms // 1000,
                "required_sec": ROUTE_SETTLE_MS // 1000}


_QUEUE_OVERFLOW_LOG = LogThrottle(HOT_PATH_LOG_INTERVAL_MS)


def _note_queue_overflow():
    # Throttled: a stalled consumer overflows on every sentence; the total is /status stats.queue_overflows.
    if _QUEUE_OVERFLOW_LOG.hit():
        log("ERROR", "GNSS", "NMEA queue full; %d overflows so far."
            % app.stats["queue_overflows"])
        app.log_error("GNSS", "NMEA Queue Overflow.")


class GNSSHandler:
    def __init__(self, nmea_queue):
        log("INFO", "GNSS", "Initializing GNSS UART...")
        self.uart = UART(CONFIG["uart_id"], baudrate=CONFIG["uart_baud"],
                         tx=CONFIG["tx_pin"], rx=CONFIG["rx_pin"],
                         rxbuf=CONFIG.get("uart_rxbuf_bytes", 16384),
                         txbuf=2048, timeout=0, timeout_char=0)
        self.tx = UartTx(self.uart)
        self.queue = nmea_queue
        self.gsv_count = 0
        self.gsv_reset_time = time.ticks_ms()
        self.last_gst = None
        self.last_gst_time = None
        self._clock_checked_ms = None
        self._uart_read_at = None
        self._uart_rate_started = time.ticks_ms()
        self._uart_rate_bytes = 0
        self._gst_attempts = 0
        self._gst_attempt_at = None
        self._gst_pending = False      # an enable command is queued or in progress in self.tx
        self.route_startup = RouteStartupGuard()
        app.stats["route_startup"] = self.route_startup.status()

    def _ensure_gst_output(self, now):
        """Reapply the volatile GST setting through self.tx without blocking.

        The command may wait for a frame boundary; _gst_done reports the write.
        """
        if self.last_gst_time is not None and time.ticks_diff(now, self.last_gst_time) < GST_FRESH_MS:
            app.stats["gst_output_state"] = "receiving"
            return
        if app.stats.get("gst_output_state") == "receiving":
            # GST stopped; a request is about to go out (it may have to wait).
            app.stats["gst_output_state"] = "waiting"
        if self._gst_pending:
            return
        delay = GST_RETRY_MS[min(max(0, self._gst_attempts - 1), len(GST_RETRY_MS) - 1)]
        if self._gst_attempt_at is not None and time.ticks_diff(now, self._gst_attempt_at) < delay:
            return
        self._gst_attempt_at = now
        self._gst_attempts += 1
        self._gst_pending = True
        accepted, _ = self.tx.submit(GST_ENABLE_COMMAND, self._gst_done, now)
        if not accepted:
            self._gst_pending = False   # queue full: retried after the next step

    def _gst_done(self, ok, error, now_ms):
        """The write result of a GST enable; an attempt is a write to the UART."""
        self._gst_pending = False
        if not ok and error == COMMAND_BUSY:
            return   # dropped before its first byte: neither an attempt nor an error
        app.stats["gst_enable_attempts"] = app.stats.get("gst_enable_attempts", 0) + 1
        if ok:
            # A GST that arrived while the command waited keeps the gate hold.
            fresh = (self.last_gst_time is not None
                     and time.ticks_diff(now_ms, self.last_gst_time) < GST_FRESH_MS)
            app.stats["gst_output_state"] = "receiving" if fresh else "requested"
            log("INFO", "GNSS", "Command sent to module: %s" % GST_ENABLE_COMMAND)
        else:
            app.stats["gst_output_state"] = "uart_error"
        app.stats["gst_output_error"] = error

    def _sync_rmc_clock(self, line):
        """Set an unset RTC from RMC and keep a set one within CLOCK_RESYNC_DEVIATION_S.

        The ESP32 RTC drifts, and recorded_at plus the RTCM epoch age read it. The
        RMC second is compared with the RTC second; a step is applied when they
        differ by more than one second. A whole-second difference of one is just
        the sub-second phase (the RTC is set to the RMC second when the line
        arrives, so it trails real time by the line latency), at the hourly check
        too: stepping on it would be a spurious step at random. Durations keep using ticks, so a step never changes an interval.
        """
        stamp = rmc_datetime(line)
        if stamp is None:
            return
        now_ms = time.ticks_ms()
        was_valid = clock_is_valid()
        old = time.gmtime()
        # RTC().datetime() orders (y, m, d, weekday, h, m, s, sub); gmtime() has no weekday there.
        new = stamp[:3] + stamp[4:7]
        delta = _utc_seconds(new) - _utc_seconds(old[:6])
        if was_valid:
            due = (self._clock_checked_ms is None or
                   time.ticks_diff(now_ms, self._clock_checked_ms) >= CLOCK_RESYNC_INTERVAL_MS)
            if abs(delta) <= CLOCK_RESYNC_DEVIATION_S:
                if due:
                    self._clock_checked_ms = now_ms
                return
            reason = "drift"
        else:
            reason = "initial"
        try:
            RTC().datetime(stamp)
        except Exception as error:
            app.stats["gnss_clock_error"] = str(error)
            return
        self._clock_checked_ms = now_ms
        app.stats["clock_source"] = "GNSS_RMC"
        if not was_valid:
            log("INFO", "GNSS", "Clock synchronized from GNSS UTC.")
            return
        # Step counter: consumers that cached a clock-derived value (RTCM epoch
        # age) drop it instead of reporting an age from the old clock.
        app.clock_steps += 1
        app.stats["clock_resyncs"] = app.stats.get("clock_resyncs", 0) + 1
        app.stats["clock_last_delta_s"] = delta
        log("WARN", "GNSS", "Clock stepped by %+d s from GNSS UTC (%s)." % (delta, reason))
        field_recorder.event("clock_resync", {
            "reason": reason, "delta_s": delta,
            "old": "%04d-%02d-%02dT%02d:%02d:%02dZ" % old[:6],
            "new": "%04d-%02d-%02dT%02d:%02d:%02dZ" % new})

    def _record_uart_read(self, available, chunk, started_ms, completed_ms):
        """Observe delivered bytes, not unreported FIFO/driver losses."""
        size = len(chunk) if chunk else 0
        gap = (max(0, time.ticks_diff(started_ms, self._uart_read_at))
               if self._uart_read_at is not None else 0)
        self._uart_read_at = started_ms
        stats = app.stats
        stats["uart_rx_reads"] = stats.get("uart_rx_reads", 0) + 1
        stats["uart_rx_bytes"] = stats.get("uart_rx_bytes", 0) + size
        stats["uart_rx_available_max"] = max(stats.get("uart_rx_available_max", 0), available)
        stats["uart_rx_chunk_max"] = max(stats.get("uart_rx_chunk_max", 0), size)
        stats["uart_last_read_bytes"] = size
        stats["uart_last_read_ticks_ms"] = started_ms
        stats["uart_last_read_duration_ms"] = max(0, time.ticks_diff(completed_ms, started_ms))
        stats["uart_read_gap_ms"] = gap
        stats["uart_read_gap_max_ms"] = max(stats.get("uart_read_gap_max_ms", 0), gap)
        if size:
            # Silence counts delivered bytes only; an empty read is not data.
            app.note_uart_data(started_ms)
        self._uart_rate_bytes += size
        self._close_rate_window(completed_ms)

    def _close_rate_window(self, now_ms):
        """Publish the RX rate once the window is a second old, with or without reads.

        Called without a read too, so the rate falls to 0 after one full window
        without bytes instead of freezing at the last value."""
        elapsed = max(0, time.ticks_diff(now_ms, self._uart_rate_started))
        if elapsed >= 1000:
            # A stalled reader can make this window longer than one second.
            # Report its actual duration instead of calling a burst a wire rate.
            stats = app.stats
            rate = (self._uart_rate_bytes * 1000) // elapsed
            stats["uart_rx_rate_bytes_sec"] = rate
            stats["uart_rx_rate_max_bytes_sec"] = max(
                stats.get("uart_rx_rate_max_bytes_sec", 0), rate)
            stats["uart_rx_rate_window_ms"] = elapsed
            self._uart_rate_started, self._uart_rate_bytes = now_ms, 0

    def _read_uart(self):
        available = self.uart.any()
        if not available:
            return None
        started = time.ticks_ms()
        # Nonblocking RX returns the bytes already available. Partial sentences
        # stay in the parser buffer; waiting here slows every four-line refill.
        chunk = self.uart.read()
        self._record_uart_read(available, chunk, started, time.ticks_ms())
        return chunk

    def _append_uart_chunk(self, pending, chunk):
        """Bound complete-line backlog as well as unterminated garbage.

        Without pending bytes the chunk itself is the buffer: no copy of a
        whole UART read."""
        total = len(pending) + len(chunk)
        app.stats["uart_buffer_peak_bytes"] = max(
            app.stats.get("uart_buffer_peak_bytes", 0), total)
        if total <= MAX_NMEA_BUFFER_BYTES:
            return pending + chunk if pending else chunk
        # Build only a bounded suffix, then keep its last possible sentence
        # start. Discarding overload data is explicit and fails the benchmark.
        if len(chunk) >= MAX_NMEA_BUFFER_BYTES:
            suffix = chunk[-MAX_NMEA_BUFFER_BYTES:]
        else:
            suffix = pending[-(MAX_NMEA_BUFFER_BYTES - len(chunk)):] + chunk
        marker = suffix.rfind(b"$")
        retained = suffix[marker:] if marker >= 0 else b""
        app.stats["uart_buffer_overflows"] = app.stats.get("uart_buffer_overflows", 0) + 1
        app.stats["uart_last_buffer_overflow"] = {
            "ticks_ms": time.ticks_ms(), "pending_bytes": len(pending),
            "incoming_bytes": len(chunk), "retained_bytes": len(retained),
            "discarded_bytes": total - len(retained),
        }
        return retained

    def _record_crc_error(self, raw_line, sentence_type, ascii_error, pending_bytes):
        """Retain one bounded RAM record; do not write flash from the RX path."""
        now = time.ticks_ms()
        stripped = raw_line.strip()
        star = stripped.find(b"*")
        provided, calculated = None, None
        if star >= 0:
            provided = stripped[star + 1:star + 9].decode("ascii", "ignore")
            if stripped.startswith(b"$"):
                calculated = 0
                for byte in stripped[1:star]:
                    calculated ^= byte
        try: available = self.uart.any()
        except Exception: available = None
        try: queue_depth = self.queue.qsize()
        except (AttributeError, TypeError): queue_depth = None
        stats = app.stats
        stats["crc_errors"] = stats.get("crc_errors", 0) + 1
        stats["nmea_last_crc_error"] = {
            "ticks_ms": now, "device_time_sec": int(time.time()),
            "crc_error_count": stats["crc_errors"], "sentence_type": sentence_type,
            "raw_length": len(raw_line),
            "raw_hex": ubinascii.hexlify(raw_line[:CRC_RAW_CAPTURE_BYTES]).decode(),
            "raw_truncated": len(raw_line) > CRC_RAW_CAPTURE_BYTES,
            "ascii_error": ascii_error, "dollar_count": raw_line.count(b"$"),
            "star_count": raw_line.count(b"*"),
            "checksum_provided": provided, "checksum_calculated": calculated,
            "pending_python_bytes": pending_bytes, "uart_available_bytes": available,
            "uart_rxbuf_config_bytes": CONFIG.get("uart_rxbuf_bytes", 16384),
            "uart_rx_bytes": stats.get("uart_rx_bytes", 0),
            "uart_rx_available_max": stats.get("uart_rx_available_max", 0),
            "uart_last_read_bytes": stats.get("uart_last_read_bytes", 0),
            "uart_read_gap_ms": stats.get("uart_read_gap_ms", 0),
            "uart_read_gap_max_ms": stats.get("uart_read_gap_max_ms", 0),
            "uart_last_read_ticks_ms": stats.get("uart_last_read_ticks_ms"),
            "uart_rx_rate_bytes_sec": stats.get("uart_rx_rate_bytes_sec", 0),
            "uart_rx_rate_window_ms": stats.get("uart_rx_rate_window_ms", 0),
            "since_last_uart_read_ms": (max(0, time.ticks_diff(now, self._uart_read_at))
                                        if self._uart_read_at is not None else None),
            "tracking_max_append_ms": stats.get("tracking_max_append_ms", 0),
            "nmea_queue_depth": queue_depth,
            "nmea_queue_overflows": stats.get("queue_overflows", 0),
            "tracking_queue_depth": stats.get("tracking_queue_depth", 0),
            "tracking_queue_max": stats.get("parallel_benchmark_queue_max", 0),
            "ble_queue_max": stats.get("ble_queue_max", 0),
            "tcp_queue_max": stats.get("tcp_queue_max", 0),
        }

    @staticmethod
    def check_nmea_checksum(line):
        try:
            line = line.strip()
            if not line.startswith("$"):
                return False
            # Exactly one "*", as the former split("*") unpacking demanded;
            # find avoids its list and data string per sentence.
            star = line.find("*")
            if star < 0 or line.find("*", star + 1) >= 0:
                return False

            calculated = 0
            for char in line[1:star]:
                calculated ^= ord(char)

            return calculated == int(line[star + 1:], 16)
        except Exception:
            return False

    def parse_gga(self, line):
        started = _ticks_us()
        try:
            # The checksum does not belong to the reference station ID.
            star = line.find("*")
            parts = (line[:star] if star >= 0 else line).split(",")
            if len(parts) < 15:
                return None

            fix_quality = int(parts[6] or 0)

            # Without a fix no position - even if the recipient still writes the last
            # known in the fields.
            if fix_quality == 0:
                lat_e8 = lon_e8 = None
            else:
                lat_e8 = geo.parse_nmea_coord_e8(parts[2], parts[3])
                lon_e8 = geo.parse_nmea_coord_e8(parts[4], parts[5])

            # The float fields are display values derived from the exact integers
            # (a 32-bit float resolves a latitude only to about 0.4 m); a text the
            # exact parsers refuse (such as "1e2" or " 12.3") leaves both the
            # integer and its float empty, and the sentence still parses.
            alt_text = parts[9]
            geoid_text = parts[11]
            alt_mm = geo.parse_decimal_fixed(alt_text, 3) if alt_text else None
            geoid_mm = geo.parse_decimal_fixed(geoid_text, 3) if geoid_text else None
            lat = geo.e8_to_deg(lat_e8) if lat_e8 is not None else None
            lon = geo.e8_to_deg(lon_e8) if lon_e8 is not None else None
            alt = alt_mm / geo.ALT_SCALE if alt_mm is not None else None
            geoid = geoid_mm / geo.ALT_SCALE if geoid_mm is not None else None
            if alt_mm is not None and geoid_mm is not None:
                ellipsoid = (alt_mm + geoid_mm) / geo.ALT_SCALE
            else:
                ellipsoid = None

            fix = {
                "type": "GGA", "utc": parts[1],
                # The unchanged set - the NTRIP client sends it back to the caster, VRS
                # mountpoints demand it.
                "raw": line,
                "lat": lat,
                "lon": lon,
                "lat_e8": lat_e8,
                "lon_e8": lon_e8,
                "qual": fix_quality,
                "fix_status_text": FIX_STATUS.get(fix_quality, "UNKNOWN"),
                "sats": int(parts[7] or 0),
                "hdop": float(parts[8]) if parts[8] else None,
                # GGA field 9 is orthometric height above mean sea level.
                "alt": alt,
                "altitude_msl_m": alt,
                "geoid_sep_m": geoid,
                "altitude_ellipsoid_m": ellipsoid,
                # The same heights as exact integer millimetres.
                "alt_mm": alt_mm,
                "geoid_sep_mm": geoid_mm,
                "correction_age_sec": (float(parts[13]) if parts[13] else None),
                # Keep as a string because station IDs can have leading zeros.
                "station_id": parts[14] or None,
            }
            elapsed = _elapsed_us(started)
            stats = app.stats
            stats["gga_parse_us"] = elapsed
            # get(): a bookkeeping miss must never turn a good fix into a parse error.
            if elapsed > stats.get("gga_parse_us_max", 0):
                stats["gga_parse_us_max"] = elapsed
            # The maximum keeps a one-off pause (a collection, a flash write) for the
            # rest of the uptime. The moving average shows the typical cost and the
            # count how often a parse went over the budget at all.
            average = stats.get("gga_parse_us_avg", 0)
            stats["gga_parse_us_avg"] = (
                elapsed if average == 0 else (average * 15 + elapsed) // 16)
            if elapsed > _GGA_PARSE_BUDGET_US:
                stats["gga_parse_over_budget"] = stats.get("gga_parse_over_budget", 0) + 1
            return fix
        except Exception as e:
            # Until V9.3, every parser error disappeared without a trace: no numerator,
            # no log - the fix simply failed.
            app.stats["gga_parse_errors"] += 1
            log("DEBUG", "GNSS", "Could not parse GGA (%s): %s" % (e, line))
            return None

    @staticmethod
    def parse_gst(line):
        """Parse NMEA GST receiver error estimates (one standard deviation)."""
        try:
            parts = line.split("*", 1)[0].split(",")
            if len(parts) < 9 or not parts[1]:
                return None
            values = [float(value) if value else None for value in parts[2:9]]
            for index, value in enumerate(values):
                if value is not None and (not math.isfinite(value) or (index != 3 and value < 0)):
                    return None
            lat_sigma = values[4] if values[4] not in (None, 0) else None
            lon_sigma = values[5] if values[5] not in (None, 0) else None
            altitude_sigma = values[6] if values[6] not in (None, 0) else None
            horizontal_sigma = (math.sqrt(lat_sigma * lat_sigma + lon_sigma * lon_sigma)
                                if lat_sigma is not None and lon_sigma is not None else None)
            return {
                "source": "NMEA_GST", "utc": parts[1],
                "rms_residual_m": values[0],
                "semi_major_sigma_m": values[1],
                "semi_minor_sigma_m": values[2],
                "orientation_deg": values[3],
                "latitude_sigma_m": lat_sigma,
                "longitude_sigma_m": lon_sigma,
                "horizontal_sigma_m": horizontal_sigma,
                "altitude_sigma_m": altitude_sigma,
            }
        except (ValueError, TypeError, IndexError):
            return None

    async def run(self):
        log("INFO", "GNSS", "GNSS handler started.")
        buf = b""
        pos = 0   # start of the unscanned rest of buf
        last_read_time = time.ticks_ms()
        app.beat("gnss")
        app.start_receiver_watch(last_read_time)

        while not shutdown_event.is_set():
            current_time = time.ticks_ms()
            app.beat("gnss")
            chunk = None

            try:
                if self.tx.busy:
                    self.tx.pump(current_time)   # deadlines and rests of the writer
                self._ensure_gst_output(current_time)
                chunk = self._read_uart()
                # Checked after the read with a fresh reading: bytes that waited in
                # the driver during a stalled turn renew the data stamp first.
                read_done = time.ticks_ms()
                app.check_receiver_link(read_done)
                if chunk:
                    last_read_time = read_done
                else:
                    self._close_rate_window(read_done)

                if chunk:
                    # Lines are sliced off at a read position. Splitting off the
                    # rest per line copied the whole backlog for every sentence.
                    buf = self._append_uart_chunk(buf, chunk)
                    pos = 0
                    processed = 0
                    while True:
                        nl = buf.find(b"\n", pos)
                        if nl < 0:
                            break
                        raw = buf[pos:nl + 1]   # one slice per line, "\n" included
                        pos = nl + 1
                        processed += 1
                        # Yield even when this line is later filtered or invalid.
                        # Otherwise a dropped fourth line can skip every yield.
                        if processed % NMEA_YIELD_INTERVAL == 0:
                            await asyncio.sleep_ms(0)
                            # Flash work can occupy every scheduler turn. Drain
                            # RX after each turn, not only after the entire old
                            # chunk, or several individually safe pauses can
                            # exhaust the driver's finite receive buffer.
                            more = self._read_uart()
                            if more:
                                # Same arguments as before: the unscanned rest
                                # and the new bytes, so the overflow and peak
                                # figures stay the same.
                                buf = self._append_uart_chunk(buf[pos:], more)
                                pos = 0
                                last_read_time = time.ticks_ms()
                            current_time = time.ticks_ms()
                            app.beat("gnss")

                        try:
                            # Keep legacy filtering for unknown/proprietary lines,
                            # but never accept a whitelisted sentence repaired by
                            # silently dropping a corrupted non-ASCII byte.
                            # "ignore" drops exactly the bytes above 127.
                            text = raw.decode("ascii", "ignore")
                            ascii_error = len(text) != len(raw)
                            line_str = text.strip()
                        except Exception:
                            continue

                        if not line_str or not line_str.startswith("$"):
                            continue

                        nmea_type = line_str[3:6]
                        if ascii_error and nmea_type not in NMEA_WHITELIST:
                            # Replacing a talker byte can shift the decoded type;
                            # retain a recognizable type in the original framing.
                            raw_type = raw.strip()[3:6].decode("ascii", "ignore")
                            if raw_type in NMEA_WHITELIST:
                                nmea_type = raw_type
                        if nmea_type not in NMEA_WHITELIST:
                            # Answers to $PQTM/$PAIR commands run through the same
                            # stream but are not on the whitelist.
                            if line_str.startswith(COMMAND_PREFIXES):
                                remember_reply(line_str)
                            continue

                        # In V9.0 sentences were queued unchecked to BLE and only GGA
                        # validated.
                        if ascii_error or not GNSSHandler.check_nmea_checksum(line_str):
                            self._record_crc_error(raw[:-1], nmea_type, ascii_error,
                                                   len(buf) - pos)
                            continue

                        if nmea_type in ("GSV", "GSA"):
                            field_recorder.observe_nmea(line_str)

                        if nmea_type == "GSV":
                            if time.ticks_diff(current_time, self.gsv_reset_time) >= 1000:
                                self.gsv_count = 0
                                self.gsv_reset_time = current_time

                            if self.gsv_count >= CONFIG["gsv_limit_per_sec"]:
                                continue

                            self.gsv_count += 1

                        # A line that strip() shortened by exactly its CRLF is the
                        # payload as received; anything else is rebuilt as before.
                        if len(line_str) + 2 == len(raw) and raw.endswith(b"\r\n"):
                            payload = raw
                        else:
                            payload = (line_str + "\r\n").encode()
                        if not self.queue.put_nowait(payload):
                            app.stats["queue_overflows"] += 1
                            _note_queue_overflow()

                        if nmea_type == "GGA":
                            fix = self.parse_gga(line_str)
                            if fix:
                                fix["route_ready"] = self.route_startup.check(
                                    fix, time.ticks_ms(), clock_is_valid())
                                app.stats["route_startup"] = self.route_startup.status()
                                if (self.last_gst and self.last_gst.get("utc") == fix.get("utc")
                                        and time.ticks_diff(current_time, self.last_gst_time) <= 2000):
                                    fix["receiver_accuracy"] = dict(self.last_gst)
                                app.update_fix(fix)
                                app.stats["gga_parsed"] += 1
                                app.stats["gnss_msgs"] += 1  # alias of gga_parsed until the alias removal after the state package (#20)
                        elif nmea_type == "RMC":
                            self._sync_rmc_clock(line_str)
                        elif nmea_type == "GST":
                            gst = self.parse_gst(line_str)
                            if gst:
                                self.last_gst = gst
                                self.last_gst_time = time.ticks_ms()
                                self._gst_attempts = 0
                                app.stats["gst_output_state"] = "receiving"
                                app.stats["gst_output_error"] = None
                                app.attach_gst(gst)

                    # One copy of the unterminated rest per scan, not per line.
                    if pos:
                        buf, pos = buf[pos:], 0

                if chunk:
                    await asyncio.sleep_ms(1)
                elif time.ticks_diff(current_time, last_read_time) > 1000:
                    await asyncio.sleep_ms(100)
                else:
                    await asyncio.sleep_ms(5)

            except Exception as e:
                # Lines before the failing one are done, as with the former
                # split; the rest waits for the next read.
                if pos:
                    buf, pos = buf[pos:], 0
                error_msg = str(e)
                log("ERROR", "GNSS", "GNSS handler error.", print_traceback=True, exception=e)
                app.log_error("GNSS", error_msg)
                try:
                    # A UART that raises on every access is silent as well; the
                    # rate must not freeze at the value of the last good window.
                    failed_at = time.ticks_ms()
                    app.check_receiver_link(failed_at)
                    self._close_rate_window(failed_at)
                except Exception:
                    pass
                await asyncio.sleep(1)
        log("INFO", "GNSS", "GNSS handler task stopped.")
