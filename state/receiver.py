# SPDX-License-Identifier: AGPL-3.0-only
"""Receiver part of the runtime state: ReceiverState owns the published fix, its freshness, the cached quality gate and the fix-quality timing.
"""
import time

from cfg import CONFIG, log
from cfg_logging import HOT_PATH_LOG_INTERVAL_MS, LogThrottle
from state.system import _instances


# The receiver sends GGA before the same epoch's GST: a GGA whose only shortcoming is
# the missing GST keeps the previous gate verdict this long while GST is arriving.
GST_GRACE_MS = 2000


class ReceiverState:
    def _init_receiver(self):
        self._last_fix = None
        self._last_update_ms = 0
        self._last_fix_status = None
        self._fix_qual = None
        self._fix_qual_seit = None
        self._fix_qual_changed_ms = None
        self._last_rtk_fixed_ms = None
        # One cached gate verdict: _gate_quality is the last applied quality_check
        # result (None before the first fix); _gate_hold_since_ms marks a GGA held
        # for its GST (None when no hold is active); _gate_receiver_accuracy is the
        # GST estimate of the fix the verdict was computed from (None if it had none).
        self._gate_accepted = None
        self._gate_changed_ms = None
        self._gate_quality = None
        self._gate_receiver_accuracy = None
        self._gate_hold_since_ms = None
        self._fix_ticks_ms = None
        # Sticky: set once the age was seen at or past the limit, cleared only by a
        # newly published fix. A full tick wrap (2**30 ms) would otherwise alias an old
        # stamp back to a small age.
        self._fix_expired = False
        self._fix_seq = 0
        self._fix_rev = 0
        # Called with every published fix and every GST enrichment of it (E3).
        self._fix_listener = None
        self._fix_listener_errors = LogThrottle(HOT_PATH_LOG_INTERVAL_MS)

    def set_fix_listener(self, listener):
        """Register listener(fix, quality, pending, published_ms, seq, rev); None removes it.

        It runs for every published fix and for every GST enrichment of it, with
        the gate verdict of that fix, whether the gate waits for its GST, the tick
        time of the publication (an enrichment keeps it) and fix_seq/fix_rev as
        fix_state() reports them afterwards."""
        self._fix_listener = listener

    def _notify_fix(self, fix, quality, published_ms):
        """Call the fix listener; nothing it raises reaches the GNSS path."""
        listener = self._fix_listener
        if listener is None:
            return
        try:
            listener(fix, quality, self._awaits_gst(fix, quality), published_ms,
                     self._fix_seq, self._fix_rev)
        except Exception as error:
            # The throttle allocates and log can raise: keep both inside as well.
            try:
                n = self._fix_listener_errors.hit()
                if n:
                    log("ERROR", "STAKE", "Stakeout fix listener failed (%d): %s" % (n, error))
            except Exception:
                pass

    @property
    def last_fix(self):
        return self._last_fix

    def update_fix(self, fix_data):
        current_ms = time.ticks_ms()
        self._settle_gate_hold(current_ms)
        recorder = _instances.get("field_diagnostics")
        if recorder is not None and (self._last_fix or {}).get("qual") != fix_data.get("qual"):
            recorder.event("fix_change", {"previous": (self._last_fix or {}).get("qual"),
                           "qual": fix_data.get("qual"), "gga_utc": fix_data.get("utc")})
        was_stale = self.fix_is_stale(current_ms)
        if was_stale:
            # The silent seconds belong to no quality: restart the dwell and the streak.
            self._fix_qual = None
        self.track_fix_quality(fix_data.get("qual", 0), current_ms)
        if time.ticks_diff(current_ms, self._last_update_ms) > CONFIG["fix_update_interval_ms"]:
            if was_stale:
                self._gate_accepted = None
                self._gate_quality = None
                self._gate_receiver_accuracy = None
                self._gate_hold_since_ms = None
            quality = self._evaluate_gate(fix_data, current_ms)
            self._last_fix = fix_data
            self._fix_ticks_ms = current_ms
            self._fix_expired = False
            self._fix_seq += 1
            self._last_update_ms = current_ms
            self._notify_fix(fix_data, quality, current_ms)
            try:
                from tracking import tracker
                queued = tracker.enqueue_fix(fix_data, quality, self._fix_seq)
                if queued is None:
                    self.stats["tracking_queue_overflows"] += 1
            except Exception as error:
                log("ERROR", "TRACK", "Automatic tracking point failed: %s" % error)
                self.log_error("TRACK", str(error))

            current_status = fix_data["fix_status_text"]

            if current_status != self._last_fix_status:
                hdop = fix_data["hdop"]
                log("INFO", "GNSS", "Fix: %s | Sats: %s | HDOP: %s"
                    % (current_status, fix_data["sats"],
                       "n/a" if hdop is None else "%.1f" % hdop))
                self._last_fix_status = current_status

    def fix_age_ms(self, now_ms=None):
        """Ticks since the last published fix; None without a stamp."""
        if self._fix_ticks_ms is None:
            return None
        if now_ms is None:
            now_ms = time.ticks_ms()
        diff = time.ticks_diff(now_ms, self._fix_ticks_ms)
        age = 2 ** 29 if diff < 0 else diff
        if age >= CONFIG["fix_stale_after_ms"]:
            self._fix_expired = True
        elif self._fix_expired:
            # Expiry was seen before: a small age can only be an aliased full tick wrap.
            age = 2 ** 29
        return age

    def fix_is_stale(self, now_ms=None):
        age = self.fix_age_ms(now_ms)
        return (self._last_fix is not None and age is not None
                and (self._fix_expired or age >= CONFIG["fix_stale_after_ms"]))

    def fix_state(self, now_ms=None):
        """Freshness snapshot; age and stale derive from one clock reading."""
        if now_ms is None:
            now_ms = time.ticks_ms()
        return {"fix_age_ms": self.fix_age_ms(now_ms), "fix_seq": self._fix_seq,
                "fix_rev": self._fix_rev, "stale": self.fix_is_stale(now_ms)}

    def fix_version(self):
        """(fix_seq, fix_rev): changes with every published fix and GST enrichment (#27)."""
        return (self._fix_seq, self._fix_rev)

    def gga_for_caster(self, now_ms=None):
        """GGA sentence (no CRLF) of the published fix for the NTRIP caster, or None.

        The sentence and its freshness stamp come from the same published fix.
        None when no GGA was received yet or the latest received GGA carries no
        fix (_fix_qual is set before the update throttle, so a fix loss between
        two publishes withholds at once), when the fix is stale, or when the
        published fix has no quality or no raw sentence.
        """
        fix = self._last_fix
        if fix is None or not self._fix_qual or self.fix_is_stale(now_ms):
            return None
        if (fix.get("qual") or 0) <= 0:
            return None
        return fix.get("raw")

    def attach_gst(self, gst):
        """Attach a same-epoch GST estimate when GST arrives after GGA."""
        now_ms = time.ticks_ms()
        # A GST past the grace first settles the expired hold, so its own
        # evaluation below restarts the streak instead of continuing it.
        self._settle_gate_hold(now_ms)
        if self.fix_is_stale(now_ms):
            return False
        if not self._last_fix or self._last_fix.get("utc") != gst.get("utc"):
            return False
        enriched = dict(self._last_fix)
        enriched["receiver_accuracy"] = dict(gst)
        self._last_fix = enriched
        self._fix_rev += 1
        quality = self._evaluate_gate(enriched, now_ms)
        # The publication time of the enriched GGA, never the GST's arrival.
        self._notify_fix(enriched, quality, self._fix_ticks_ms)
        try:
            from tracking import tracker
            queued = tracker.enqueue_fix(enriched, quality, self._fix_seq)
            if queued is None:
                self.stats["tracking_queue_overflows"] += 1
        except Exception as error:
            log("ERROR", "TRACK", "Automatic tracking point failed: %s" % error)
            self.log_error("TRACK", str(error))
        return True

    def _apply_gate(self, quality, fix, changed_ms):
        self._gate_hold_since_ms = None
        self._gate_quality = quality
        accuracy = (fix or {}).get("receiver_accuracy")
        self._gate_receiver_accuracy = dict(accuracy) if accuracy else None
        accepted = bool(quality["accepted"])
        if accepted != self._gate_accepted:
            self._gate_accepted = accepted
            self._gate_changed_ms = changed_ms

    def _awaits_gst(self, fix, quality):
        """True when only the GST of this fix is missing and one is expected."""
        return (not quality["accepted"] and quality["reasons"] == ["gst_required"]
                and not fix.get("receiver_accuracy")
                and self.stats.get("gst_output_state") == "receiving")

    def _evaluate_gate(self, fix, now_ms):
        """Judge a published fix; hold the verdict while only its GST is missing.

        Returns the quality_check verdict of this fix, also when it is held."""
        # Imported here to keep state.py independent during module startup.
        from tracking import quality_check
        quality = quality_check(fix)
        # A verdict that already lacks GST (a dropout past the grace) is not held
        # again: there is nothing better to show while waiting.
        if (self._gate_quality is not None
                and "gst_required" not in self._gate_quality["reasons"]
                and self._awaits_gst(fix, quality)):
            if self._gate_hold_since_ms is None:
                self._gate_hold_since_ms = now_ms
            return quality
        self._apply_gate(quality, fix, now_ms)
        return quality

    def _settle_gate_hold(self, now_ms):
        """Apply the held fix for real once its GST is GST_GRACE_MS overdue."""
        since = self._gate_hold_since_ms
        if since is None or time.ticks_diff(now_ms, since) < GST_GRACE_MS:
            return
        from tracking import quality_check
        try:
            deadline = time.ticks_add(since, GST_GRACE_MS)
        except AttributeError:
            deadline = since + GST_GRACE_MS
        # The verdict changed at the deadline, not whenever someone looked.
        self._apply_gate(quality_check(self._last_fix), self._last_fix, deadline)

    def reevaluate_gate(self):
        """Re-judge the current fix now, e.g. after the gate limits changed.

        A bare fix still waiting for its GST keeps the hold: the GST or the grace
        deadline judges it under the new limits, so the verdict does not flash to
        gst_required. Any other failure under the new limits applies at once.
        """
        if self._last_fix is None:
            return
        now_ms = time.ticks_ms()
        self._settle_gate_hold(now_ms)
        from tracking import quality_check
        quality = quality_check(self._last_fix)
        if (self._gate_hold_since_ms is not None
                and self._awaits_gst(self._last_fix, quality)):
            return
        self._apply_gate(quality, self._last_fix, now_ms)

    def gate_state(self, now_ms=None):
        """The cached gate verdict that quality_streaks() also reports.

        From the first published fix on, accepted equals
        quality_streaks()["gate_accepted"]. Before it, accepted is None and quality
        is quality_check(None). A stale fix is judged like quality_streaks() does:
        not accepted, quality_check(None), no pending GST.
        """
        if now_ms is None:
            now_ms = time.ticks_ms()
        self._settle_gate_hold(now_ms)
        from tracking import quality_check
        if self.fix_is_stale(now_ms):
            return {"accepted": False, "quality": quality_check(None),
                    "pending_gst": False, "held_receiver_accuracy": None}
        cached = self._gate_quality
        if cached is None:
            quality = quality_check(None)
        else:
            # Copies, so that endpoints cannot mutate the cache.
            quality = dict(cached, reasons=list(cached["reasons"]),
                           limits=dict(cached["limits"]))
        pending = self._gate_hold_since_ms is not None
        held = self._gate_receiver_accuracy
        return {"accepted": self._gate_accepted, "quality": quality,
                "pending_gst": pending,
                # The accuracy the held verdict was judged on, for a steady display.
                "held_receiver_accuracy": dict(held) if pending and held else None}

    def track_fix_quality(self, qual, jetzt_ms=None):
        """Perpetuates the dwell time in the previous fixed qualifier. Called at each GGA. The current qualifier appears immediately with 0 seconds, so that the display does not show something only at the second sentence.
        """
        if jetzt_ms is None:
            jetzt_ms = time.ticks_ms()
        counters = self.stats["fix_quality_seconds"]

        elapsed = 0
        if self._fix_qual is not None:
            elapsed = time.ticks_diff(jetzt_ms, self._fix_qual_seit)
            if elapsed > 0:
                key = str(self._fix_qual)
                counters[key] = counters.get(key, 0) + elapsed // 1000

        if qual != self._fix_qual:
            self._fix_qual = qual
            self._fix_qual_seit = jetzt_ms
            self._fix_qual_changed_ms = jetzt_ms
            counters.setdefault(str(qual), 0)
        elif elapsed > 0:
            # Only go on when time was actually recorded - otherwise fractions under a
            # second were lost.
            self._fix_qual_seit = jetzt_ms
        if qual == 4:
            self._last_rtk_fixed_ms = jetzt_ms

    def quality_streaks(self, jetzt_ms=None):
        """Return compact continuous-quality timing for the field UI."""
        if jetzt_ms is None:
            jetzt_ms = time.ticks_ms()
        def seconds_since(stamp):
            if stamp is None:
                return None
            return max(0, time.ticks_diff(jetzt_ms, stamp) // 1000)
        self._settle_gate_hold(jetzt_ms)
        gate_accepted = self._gate_accepted
        gate_streak = seconds_since(self._gate_changed_ms)
        if self.fix_is_stale(jetzt_ms):
            gate_accepted = False
            gate_streak = None
        return {
            "fix_quality": self._fix_qual,
            "streak_sec": seconds_since(self._fix_qual_changed_ms),
            "gate_accepted": gate_accepted,
            "gate_streak_sec": gate_streak,
            "since_rtk_fixed_sec": seconds_since(self._last_rtk_fixed_ms),
        }

    def note_tracking_queue_overflow(self):
        """Count one fix the full tracking queue refused; the one writer of this key (#20)."""
        self.stats["tracking_queue_overflows"] += 1
