# SPDX-License-Identifier: AGPL-3.0-only
"""Stakeout session: the active target and the guidance state per published fix.

AppState calls StakeoutSession.on_fix for every published GGA fix and for
every GST enrichment of it (seq counts publications, rev enrichments). The
session keeps the active target of the active project and derives from each
fix the guidance state:

- ``no_target``: no target is selected (nothing is kept).
- ``no_position``: no e8 position, ``qual == 0``, or a stale fix when read.
- ``far``: farther than FAR_M; distances are not reported.
- ``coarse``: farther than the fine radius.
- ``fine``: within the fine radius, but outside the tolerance or the gate
  rejected the fix.
- ``settling``/``ready``: within the tolerance with an accepted fix (or one
  whose GST is pending); ``ready`` once the quality window holds
  ``measurement_quality_window_samples`` accepted epochs whose mean lies
  within the tolerance and whose spread is within
  ``measurement_max_horizontal_spread_m``.

The window keeps the fix dicts themselves, so WP4 confirms with exactly the
fixes the guidance judged. One publication counts once: its GST enrichment
(same ``seq``) or a repeat of its ``utc`` within EPOCH_GAP_MS replaces the
newest element and never moves its epoch time; a gap beyond EPOCH_GAP_MS, a
GST that never came, the gate, the tolerance, the target or a changed
configuration reset the window. A result counts only for the ``(seq, rev)``
whose call ran through; a read with a changed configuration is never
``ready``.

Signs: ``dn_m``/``de_m`` are the target minus the rover, the bearing points
from the rover to the target against geographic north. Rounding: metres to
three decimals up to FINE_DETAIL_M, beyond that whole metres and a whole-degree
bearing. ``motion`` comes from the last two epochs of RTK fixes.

live_packet builds the body of GET /api/stakeout/live from the readings the
caller passes in (fix view, NTRIP summary, link health, stats).

The module is pure: no file I/O, no network, no logging per fix. It knows
neither web nor tracking; the caller checks project and storage.
"""
import math
import time

import geo
from cfg import CONFIG

FAR_M = 50000
FINE_DETAIL_M = 10000
EPOCH_GAP_MS = 2500
MOTION_MIN_MPS = 0.5

# Fix qualities that count for motion: RTK fixed and RTK float.
_RTK_QUALITIES = (4, 5)
_NO_REASONS = ()
_AWAITING_FIX = ("awaiting_fix",)
_OUTSIDE_TOLERANCE = ("outside_tolerance",)
_GATE_REJECTED = ("gate_rejected",)
_GST_PENDING = ("gst_pending",)
_CONFIG_CHANGED = ("config_changed",)
_SESSION_ERROR = ("session_error",)
_WINDOW_STATES = ("settling", "ready")
# Sets with part orders (stakeout.MAX_SETS) and the bytes of one order
# bitfield (stakeout.MAX_PARTS_PER_SET parts; this module does not import stakeout).
_ORDER_SETS = 4
_ORDER_BYTES = 63

# The gate limits of tracking.MEASUREMENT_GATE_KEYS (compared in the tests;
# this module does not import tracking). Together with the two stakeout keys
# they form the configuration signature of a window.
_GATE_KEYS = (
    "measurement_required_fix", "measurement_min_satellites",
    "measurement_max_hdop", "measurement_max_correction_age_sec",
    "measurement_max_horizontal_spread_m",
    "measurement_max_gst_horizontal_sigma_m",
    "measurement_max_gst_vertical_sigma_m",
    "measurement_quality_window_samples")
_SIGNATURE_KEYS = _GATE_KEYS + ("stakeout_tolerance_m", "stakeout_fine_radius_m")


def _ticks_us():
    """Microsecond tick counter. CPython has no time.ticks_us, so use the wall clock there."""
    try:
        return time.ticks_us()
    except AttributeError:
        return int(time.time() * 1000000)


def _is_e8(value):
    return type(value) is int


def _signature():
    """The configuration values a window depends on, as a tuple."""
    return tuple([CONFIG[key] for key in _SIGNATURE_KEYS])


def _window_state(mean_dist_m, spread_m, tolerance_m, spread_limit_m):
    """State and reasons of a full window of accepted fixes; both limits inclusive.

    At most one reason (the mean first), so a guidance never carries more
    than one state reason; that bounds the size of the live packet.
    """
    if mean_dist_m > tolerance_m:
        return "settling", ["mean_outside_tolerance"]
    if spread_m > spread_limit_m:
        return "settling", ["horizontal_spread_too_high"]
    return "ready", []


def _degrees(value, digits):
    """A bearing rounded to ``digits`` (None for whole degrees), kept in [0, 360)."""
    value = round(value) if digits is None else round(value, digits)
    return value - 360 if value >= 360 else value


def open_status(project_id, tid):
    """The status of every target without stakeout progress: open."""
    return "open"


# The status of a target in the live packet, f(project_id, tid) -> str; web
# sets it to the stakeout progress (WP4 E16).
status_lookup = open_status


class StakeoutSession:
    def __init__(self):
        self.project_id = None
        self.fingerprint = None
        self.target = None
        # set_id -> bytearray bitfield of the parts walked in reverse (bit n-1 for part n).
        self._orders = {}
        # Raised by clear() and by a forget_set that removes the target or an
        # order, so a caller that awaited in between sees that the session changed.
        self.epoch = 0
        self.on_fix_us_max = 0
        self.fixes_seen = 0
        self._busy = False
        self._done_seq = None
        self._done_rev = None
        # Elements [fix, epoch_ms, accepted, pending, seq], oldest first;
        # epoch_ms is the publication time of the epoch's first call.
        self._window = []
        # (lat_e8, lon_e8, spread_m) of a full window of accepted fixes.
        self._mean = None
        self._signature = None
        self._reset_motion()
        self._set_result("no_target", _NO_REASONS, None)

    def _set_result(self, state, reasons, offsets):
        self._state = state
        self._reasons = reasons
        # (north_m, east_m, distance_m) from the rover to the target, or None.
        self._offsets = offsets

    def _reset_window(self):
        self._window = []
        self._mean = None

    def _reset_motion(self):
        # The last epoch as [lat_e8, lon_e8, epoch_ms, utc, seq, rtk]; no fix reference.
        self._last_epoch = None
        self._speed_mps = None
        self._course_deg = None
        self._moving = False

    def _reset(self):
        self._reset_window()
        self._reset_motion()

    def select(self, project_id, fingerprint, target):
        """Make ``target`` the active target; True when it changed.

        The same target of the same project again keeps window and motion (a
        reconnecting browser selects it again); any other target resets both.
        """
        same = (target is not None and self.target is not None and
                self.project_id == project_id and self.target["tid"] == target["tid"])
        if same or (target is None and self.target is None):
            return False
        if project_id != self.project_id or fingerprint != self.fingerprint:
            self._orders = {}
        self.project_id = project_id
        self.fingerprint = fingerprint
        self.target = target
        self._reset()
        if target is None:
            self._set_result("no_target", _NO_REASONS, None)
        else:
            self._set_result("no_position", _AWAITING_FIX, None)
        return True

    def clear(self):
        """Drop target and project, for any change of the active project."""
        self.project_id = None
        self.fingerprint = None
        self.target = None
        self._orders = {}
        self._reset()
        self._set_result("no_target", _NO_REASONS, None)
        self.epoch += 1

    def forget_set(self, project_id, set_id):
        """Drop the target and the order of the set ``set_id`` of ``project_id``."""
        if self.project_id != project_id:
            return
        forgot = self._orders.pop(set_id, None) is not None
        if self.target is not None and self.target["set_id"] == set_id:
            self.target = None
            self._reset()
            self._set_result("no_target", _NO_REASONS, None)
            forgot = True
        if forgot:
            self.epoch += 1

    def keep(self, project_id, fingerprint, damaged):
        """Clear the session unless it belongs to ``project_id`` with
        ``fingerprint`` and ``damaged(project_id, set_id)`` is false for the
        set of its target (the caller passes the active project, or None)."""
        if self.project_id is None:
            return
        target = self.target
        if (self.project_id != project_id or self.fingerprint != fingerprint or
                target is not None and damaged(project_id, target["set_id"])):
            self.clear()

    def overview(self, project_id):
        """The session as the overview of ``project_id`` reports it."""
        mine = self.project_id == project_id
        return {"target": self.target if mine else None,
                "orders": self.orders() if mine else {},
                "on_fix_us_max": self.on_fix_us_max, "fixes_seen": self.fixes_seen}

    def set_order(self, project_id, set_id, part, reversed, fingerprint=None):
        """Note whether part ``part`` (1..500) of set ``set_id`` is walked in reverse.

        An order of another project (or fingerprint) first clears the
        session. At most _ORDER_SETS sets keep an order; a set whose parts
        all run forward again has none.
        """
        if self.project_id is not None and (project_id != self.project_id or
                                            fingerprint != self.fingerprint):
            self.clear()
        self.project_id = project_id
        self.fingerprint = fingerprint
        orders = self._orders
        bits = orders.get(set_id)
        if bits is None:
            if not reversed:
                return
            if len(orders) >= _ORDER_SETS:
                orders.pop(next(iter(orders)))
            bits = orders[set_id] = bytearray(_ORDER_BYTES)
        index = part - 1
        if reversed:
            bits[index >> 3] |= 1 << (index & 7)
        else:
            bits[index >> 3] &= ~(1 << (index & 7)) & 0xFF
            if not any(bits):
                del orders[set_id]

    def orders(self):
        """{set_id: [part numbers walked in reverse, ascending]}."""
        return dict((set_id, [index + 1 for index in range(len(bits) * 8)
                              if bits[index >> 3] & (1 << (index & 7))])
                    for set_id, bits in self._orders.items())

    def on_fix(self, fix, quality, pending, published_ms, seq, rev):
        """Update the guidance from one published fix or its GST enrichment.

        ``quality`` is the gate verdict of this fix, ``pending`` says the gate
        waits for the GST of this fix, ``published_ms`` is the tick time of
        the publication ``seq`` (unchanged by an enrichment, which only raises
        ``rev``). The result counts once the call has run through: a call
        that finds the previous one unfinished first drops window and motion.
        """
        aborted = self._busy
        self._busy = True
        if aborted:
            self._reset()
        started = _ticks_us()
        self.fixes_seen += 1
        if self.target is not None:
            self._judge(fix, quality, pending, published_ms, seq)
        self._done_seq = seq
        self._done_rev = rev
        self._busy = False
        elapsed = time.ticks_diff(_ticks_us(), started)
        if elapsed > self.on_fix_us_max:
            self.on_fix_us_max = elapsed

    def _judge(self, fix, quality, pending, published_ms, seq):
        lat_e8 = fix.get("lat_e8") if fix else None
        lon_e8 = fix.get("lon_e8") if fix else None
        qual = (fix.get("qual") or 0) if fix else 0
        if not _is_e8(lat_e8) or not _is_e8(lon_e8) or qual == 0:
            self._reset()
            self._set_result("no_position", _NO_REASONS, None)
            return
        signature = _signature()
        if signature != self._signature:
            self._reset_window()
            self._signature = signature
        self._step_motion(lat_e8, lon_e8, fix.get("utc"), qual in _RTK_QUALITIES,
                          published_ms, seq)
        target = self.target
        north, east = geo.offsets_e8_m(lat_e8, lon_e8, target["lat_e8"], target["lon_e8"])
        distance = math.sqrt(north * north + east * east)
        offsets = (north, east, distance)
        accepted = bool(quality and quality.get("accepted"))
        if distance > FAR_M:
            self._reset_window()
            self._set_result("far", _NO_REASONS, offsets)
        elif distance > CONFIG["stakeout_fine_radius_m"]:
            self._reset_window()
            self._set_result("coarse", _NO_REASONS, offsets)
        elif distance > CONFIG["stakeout_tolerance_m"]:
            self._reset_window()
            self._set_result("fine", _OUTSIDE_TOLERANCE, offsets)
        elif not accepted and not pending:
            self._reset_window()
            self._set_result("fine", _GATE_REJECTED, offsets)
        else:
            self._judge_window(fix, accepted, pending, published_ms, seq, offsets)

    def _judge_window(self, fix, accepted, pending, published_ms, seq, offsets):
        window = self._window
        need = CONFIG["measurement_quality_window_samples"]
        self._mean = None
        newest = window[-1] if window else None
        if newest is not None and (seq == newest[4] or (
                fix.get("utc") == newest[0].get("utc") and
                time.ticks_diff(published_ms, newest[1]) <= EPOCH_GAP_MS)):
            # The same epoch again: its GST enrichment or a repeated GGA. It
            # replaces the newest element and keeps its epoch time.
            newest[0], newest[2], newest[3], newest[4] = fix, accepted, pending, seq
        else:
            if newest is not None and (
                    newest[3] or time.ticks_diff(published_ms, newest[1]) > EPOCH_GAP_MS):
                # The GST of the newest epoch never came, or epochs are missing.
                self._reset_window()
                window = self._window
            window.append([fix, published_ms, accepted, pending, seq])
            while len(window) > need:
                window.pop(0)
        if pending:
            self._set_result("settling", _GST_PENDING, offsets)
            return
        if len(window) < need:
            self._set_result("settling", _NO_REASONS, offsets)
            return
        lat_e8 = geo.mean_e8([item[0]["lat_e8"] for item in window])
        lon_e8 = geo.mean_lon_e8([item[0]["lon_e8"] for item in window])
        spread = 0.0
        for item in window:
            distance = geo.distance_e8_m(lat_e8, lon_e8, item[0]["lat_e8"], item[0]["lon_e8"])
            if distance > spread:
                spread = distance
        target = self.target
        state, reasons = _window_state(
            geo.distance_e8_m(lat_e8, lon_e8, target["lat_e8"], target["lon_e8"]), spread,
            CONFIG["stakeout_tolerance_m"], CONFIG["measurement_max_horizontal_spread_m"])
        self._mean = (lat_e8, lon_e8, spread)
        self._set_result(state, reasons, offsets)

    def _step_motion(self, lat_e8, lon_e8, utc, rtk, published_ms, seq):
        """One motion step per epoch: a new publication with a new utc.

        An enrichment (same seq) or a repeated publication of the same utc
        within EPOCH_GAP_MS updates at most the RTK flag, never the epoch
        time, so a late GST neither hides a gap nor adds a zero step. An
        epoch that loses RTK drops the motion it carried.
        """
        last = self._last_epoch
        if last is not None and (seq == last[4] or (
                utc == last[3] and time.ticks_diff(published_ms, last[2]) <= EPOCH_GAP_MS)):
            last[5] = rtk
            if not rtk:
                self._speed_mps = None
                self._course_deg = None
                self._moving = False
            return
        speed = None
        course = None
        if last is not None and last[5] and rtk:
            gap_ms = time.ticks_diff(published_ms, last[2])
            if 0 < gap_ms <= EPOCH_GAP_MS:
                north, east = geo.offsets_e8_m(last[0], last[1], lat_e8, lon_e8)
                speed = math.sqrt(north * north + east * east) * 1000 / gap_ms
                if speed >= MOTION_MIN_MPS:
                    course = geo.bearing_deg(north, east)
        self._speed_mps = speed
        self._course_deg = course
        self._moving = course is not None
        if last is None:
            self._last_epoch = [lat_e8, lon_e8, published_ms, utc, seq, rtk]
        else:
            last[0], last[1], last[2], last[3], last[4], last[5] = (
                lat_e8, lon_e8, published_ms, utc, seq, rtk)

    def _window_valid(self):
        """True when the window belongs to a finished run and the current configuration."""
        return not self._busy and self._signature == _signature()

    def guidance(self, now_ms, fresh, seq, rev):
        """The guidance as read now for the current fix ``(seq, rev)``.

        ``fresh`` is the M1 freshness of that fix. A result that was not
        released for ``(seq, rev)`` reads as ``session_error`` (a target
        that saw no fix yet reads as ``awaiting_fix``); with a changed
        configuration the stored geometry is judged against the new limits
        and is never ``ready``. Reading changes nothing.
        """
        state, reasons, offsets = self._state, self._reasons, self._offsets
        need = CONFIG["measurement_quality_window_samples"]
        have = 0
        spread = None
        if self.target is None:
            state, reasons, offsets = "no_target", _NO_REASONS, None
        elif self._busy or (reasons is not _AWAITING_FIX and
                            (self._done_seq != seq or self._done_rev != rev)):
            state, reasons, offsets = "no_position", _SESSION_ERROR, None
        elif not fresh and state != "no_position":
            state, reasons, offsets = "no_position", _NO_REASONS, None
        elif offsets is not None and self._signature != _signature():
            distance = offsets[2]
            if distance > FAR_M:
                state, reasons = "far", _NO_REASONS
            elif distance > CONFIG["stakeout_fine_radius_m"]:
                state, reasons = "coarse", _NO_REASONS
            elif distance > CONFIG["stakeout_tolerance_m"]:
                state, reasons = "fine", _OUTSIDE_TOLERANCE
            else:
                state, reasons = "settling", _CONFIG_CHANGED
        elif state in _WINDOW_STATES:
            have = sum(1 for item in self._window if item[2] and not item[3])
            if self._mean is not None:
                spread = round(self._mean[2], 3)
        dn_m = de_m = dist_m = bearing = None
        if offsets is not None and state != "far":
            north, east, distance = offsets
            bearing = geo.bearing_deg(north, east)
            if distance <= FINE_DETAIL_M:
                dn_m, de_m, dist_m = round(north, 3), round(east, 3), round(distance, 3)
                if bearing is not None:
                    bearing = _degrees(bearing, 1)
            else:
                dn_m, de_m, dist_m = round(north), round(east), round(distance)
                bearing = _degrees(bearing, None)
        return {"state": state, "dn_m": dn_m, "de_m": de_m, "dist_m": dist_m,
                "bearing_deg": bearing,
                "tolerance_m": CONFIG["stakeout_tolerance_m"],
                "fine_radius_m": CONFIG["stakeout_fine_radius_m"],
                "window": {"have": have, "need": need, "spread_m": spread},
                "reasons": list(reasons)}

    def window_fixes(self):
        """A copy of the window's fix dicts, oldest first; [] for an invalid window."""
        if not self._window_valid():
            return []
        return [item[0] for item in self._window]

    def window_mean(self):
        """(lat_e8, lon_e8, spread_m) of a full, valid window, else None."""
        if not self._window_valid():
            return None
        return self._mean

    def confirm_window(self, tid, now_ms, fresh, seq, rev):
        """The fixes that confirm target ``tid`` now, or None when it is not ready.

        Synchronous and taken anew on every call, so a caller binds the
        window after its last await: target identity, the release for
        ``(seq, rev)``, the configuration and the freshness all hold for
        exactly the returned list.
        """
        target = self.target
        if target is None or target["tid"] != tid:
            return None
        if self.guidance(now_ms, fresh, seq, rev)["state"] != "ready":
            return None
        return [item[0] for item in self._window]

    def status_of(self, tid):
        """The status of target ``tid`` in the live packet (status_lookup)."""
        return status_lookup(self.project_id, tid)

    def live_packet(self, now_ms, fix_view, rtcm, link, stats, storage_level, recording,
                    device_time):
        """The body of GET /api/stakeout/live for one reading at ``now_ms``.

        ``fix_view`` is the (fix, quality) pair of web.current_fix_view(now_ms)
        (fix_seq, fix_rev, fix_age_ms, stale, the held accuracy while the GST
        is pending), ``rtcm`` the NTRIP summary and ``link`` the
        web.link_health(now_ms, rtcm) of the same reading, ``stats`` app.stats
        (ntrip_state, access_state). ``fix_stale`` and ``corrections_missing``
        are display reasons of the gate; they never touch the window.
        ``recording`` adds ``recording_active`` after the session reasons.
        Serialise the packet at once: it shares values with the fix view.
        """
        fix, quality = fix_view
        stale = fix["stale"]
        position = None
        lat_e8 = fix.get("lat_e8")
        lon_e8 = fix.get("lon_e8")
        if _is_e8(lat_e8) and _is_e8(lon_e8):
            # The accuracy the live display shows: the fix's own GST, or the
            # held one while this epoch's GST is on its way.
            accuracy = (fix.get("receiver_accuracy") or
                        fix.get("pending_gst") and fix.get("held_receiver_accuracy") or {})
            position = {"lat_e8": lat_e8, "lon_e8": lon_e8, "alt_mm": fix.get("alt_mm"),
                        "qual": fix.get("qual"), "fix_status_text": fix.get("fix_status_text"),
                        "sats": fix.get("sats"), "hdop": fix.get("hdop"),
                        "correction_age_sec": fix.get("correction_age_sec"),
                        "gst_h_sigma_m": accuracy.get("horizontal_sigma_m"),
                        "gst_v_sigma_m": accuracy.get("altitude_sigma_m")}
        corrections = link["corrections"]
        reasons = list(quality["reasons"])
        if stale:
            reasons.append("fix_stale")
        if corrections != "ok":
            reasons.append("corrections_missing")
        seq = fix["fix_seq"]
        rev = fix["fix_rev"]
        guidance = self.guidance(now_ms, not stale, seq, rev)
        if recording:
            guidance["reasons"].append("recording_active")
        target = self.target
        if target is not None:
            target = {"tid": target["tid"], "lat_e8": target["lat_e8"],
                      "lon_e8": target["lon_e8"], "status": self.status_of(target["tid"])}
        return {"schema": 1, "device_time": device_time, "fix_seq": seq, "fix_rev": rev,
                "fix_age_ms": fix["fix_age_ms"], "stale": stale, "position": position,
                "corrections": {"ntrip_state": stats.get("ntrip_state"),
                                "rtcm_age_sec": rtcm.get("epoch_age_sec"),
                                "link": corrections},
                "gate": {"accepted": quality["accepted"], "reasons": reasons},
                "motion": self.motion(),
                "header": {"access_state": stats.get("access_state"),
                           "storage_level": storage_level},
                "target": target, "guidance": guidance}

    def packet_window(self, tid, packet):
        """confirm_window for the reading of ``packet``, a live packet built in
        the same synchronous call: a list exactly when ``packet`` reports
        ``tid`` as ready (guidance reads no clock, so no tick is needed)."""
        return self.confirm_window(tid, None, not packet["stale"], packet["fix_seq"],
                                   packet["fix_rev"])

    def motion(self):
        speed = self._speed_mps
        course = self._course_deg
        return {"speed_mps": None if speed is None else round(speed, 2),
                "course_deg": None if course is None else _degrees(course, 1),
                "valid": self._moving}


session = StakeoutSession()
