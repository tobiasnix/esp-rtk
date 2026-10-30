# SPDX-License-Identifier: AGPL-3.0-only
"""Averaged GNSS measurement: sample loop, live progress and cancel."""
import time
import uasyncio as asyncio

from cfg import CONFIG
from state import app, GST_GRACE_MS
from tracking import MeasurementRejected, average_fixes, quality_check, receiver_sigma_stats


# The progress the browser polls; exactly these keys, in this order.
PROGRESS_KEYS = ("active", "collected", "total", "mean_h_sigma_m", "mean_v_sigma_m",
                 "mean_alt_m", "cancelled", "waiting_reasons", "rejected_samples",
                 "mode", "phase", "elapsed_sec")


def idle_progress():
    """The progress of a session that has not measured yet, in PROGRESS_KEYS order."""
    return dict(zip(PROGRESS_KEYS, (False, 0, 0, None, None, None, False, [], 0,
                                    "fixed", "idle", 0)))


def sample_marker(fix):
    """What tells two samples apart: UTC plus the exact position.

    The exact integers tell samples apart that one 32-bit float cannot; the
    gate only lets fixes with integers through, so there is no float fallback.
    """
    current = fix or {}
    return (current.get("utc"), current.get("lat_e8"), current.get("lon_e8"))


class MeasurementSession:
    """One measurement at a time over a receiver facade.

    The receiver provides last_fix, fix_is_stale(), fix_age_ms() and
    gate_state(); without one the session reads the module global app at call
    time.
    """

    def __init__(self, receiver=None):
        self._receiver = receiver
        self._generation = 0
        self.progress = idle_progress()

    def _app(self):
        return self._receiver if self._receiver is not None else app

    def active(self):
        return bool(self.progress.get("active"))

    def snapshot(self):
        snapshot = dict(self.progress)
        # The only mutable value: a reader must not change the running progress.
        snapshot["waiting_reasons"] = list(snapshot.get("waiting_reasons") or [])
        return snapshot

    def cancel(self):
        """Invalidate the running averaging loop without cancelling its HTTP task.

        The cancelled state clears after 5 s unless a measurement started since.
        """
        self._generation += 1
        progress = self.progress
        progress["active"] = False
        progress["cancelled"] = True
        progress["phase"] = "cancelled"
        generation = self._generation

        async def clear_cancelled():
            await asyncio.sleep(5)
            if generation == self._generation:
                self.progress["cancelled"] = False
                self.progress["phase"] = "idle"
        try:
            asyncio.create_task(clear_cancelled())
        except Exception:
            pass
        return {"cancelled": True}

    def _stale_fix_error(self, receiver):
        return ValueError("GNSS position is stale (no GGA for %d s)." %
                          ((receiver.fix_age_ms() or 0) // 1000))

    def _check_generation(self, generation):
        """Stop a superseded loop; it must not write the progress of its successor."""
        if generation != self._generation:
            raise ValueError("Measurement cancelled.")

    def generation(self):
        """The current measurement generation; read it right after collect() returns."""
        return self._generation

    def check_generation(self, generation):
        """ValueError("Measurement cancelled.") when a measurement was cancelled or
        started since generation was read: the collected fix must not be stored."""
        self._check_generation(generation)

    def _finish(self, generation):
        if generation != self._generation:
            return
        progress = self.progress
        progress["active"] = False
        if progress.get("phase") != "complete":
            progress["phase"] = ("cancelled" if progress.get("cancelled") else "failed")

    async def collect(self, samples):
        """Collect distinct current fixes without blocking other device tasks."""
        if self.progress.get("active"):
            raise ValueError("A measurement is already running.")
        quality_mode = samples == "quality"
        sample_count = (int(CONFIG.get("measurement_quality_window_samples", 5))
                        if quality_mode else max(1, min(int(samples or 1), 30)))
        # Each start and each cancel opens a new generation; a loop that sees
        # another one has been superseded and writes nothing more.
        self._generation += 1
        generation = self._generation
        self.progress.update(active=False, collected=0,
                             total=None if quality_mode else sample_count,
                             mean_h_sigma_m=None, mean_v_sigma_m=None,
                             mean_alt_m=None, cancelled=False,
                             waiting_reasons=[], rejected_samples=0,
                             mode="quality" if quality_mode else "fixed",
                             phase="waiting_fix", elapsed_sec=0)
        receiver = self._app()
        if sample_count == 1 and not quality_mode:
            return await self._collect_single(receiver, generation)
        return await self._collect_samples(receiver, sample_count, quality_mode, generation)

    async def _collect_single(self, receiver, generation):
        progress = self.progress
        if receiver.fix_is_stale():
            progress["active"] = False
            progress["phase"] = "failed"
            raise self._stale_fix_error(receiver)
        # The receiver sends GGA before GST: give the held epoch its GST first.
        # The wait is a running measurement: it is exclusive and can be cancelled.
        progress["active"] = True
        try:
            waited = time.ticks_ms()
            while (receiver.gate_state()["pending_gst"]
                   and not (receiver.last_fix or {}).get("receiver_accuracy")
                   and time.ticks_diff(time.ticks_ms(), waited) < GST_GRACE_MS):
                await asyncio.sleep_ms(50)
                self._check_generation(generation)
            # A stalled event loop can resume after the fix went stale.
            if receiver.fix_is_stale(time.ticks_ms()):
                raise self._stale_fix_error(receiver)
            self._check_generation(generation)
            progress["phase"] = "complete"
            return receiver.last_fix
        finally:
            self._finish(generation)

    def _record_progress(self, samples, accepted_total):
        """Count an accepted sample and refresh the live means."""
        progress = self.progress
        progress["collected"] = accepted_total
        progress["waiting_reasons"] = []
        altitudes = [float(item["alt"]) for item in samples
                     if item.get("alt") is not None]
        sigma = receiver_sigma_stats(samples)
        progress["mean_h_sigma_m"] = sigma["horizontal_sigma_mean_m"]
        progress["mean_v_sigma_m"] = sigma["vertical_sigma_mean_m"]
        progress["mean_alt_m"] = (
            round(sum(altitudes) / len(altitudes), 3) if altitudes else None)

    async def _collect_samples(self, receiver, sample_count, quality_mode, generation):
        progress = self.progress
        progress["active"] = True
        samples, previous, rejected_marker, accepted_total = [], None, None, 0
        started = time.ticks_ms()
        if not quality_mode:
            try:
                deadline = time.ticks_add(time.ticks_ms(), max(15000, sample_count * 4000))
            except AttributeError:
                deadline = time.ticks_ms() + max(15000, sample_count * 4000)
        try:
            while (quality_mode or len(samples) < sample_count) and (
                    quality_mode or time.ticks_diff(deadline, time.ticks_ms()) > 0):
                self._check_generation(generation)
                progress["elapsed_sec"] = max(0, round(
                    time.ticks_diff(time.ticks_ms(), started) / 1000, 1))
                if receiver.fix_is_stale():
                    raise self._stale_fix_error(receiver)
                fix = receiver.last_fix
                marker = sample_marker(fix)
                # Judged once per new fix; a poll of the same fix judges nothing.
                sample_quality = (quality_check(fix) if fix and marker != previous
                                  else None)
                if sample_quality is not None and sample_quality["accepted"]:
                    progress["phase"] = "collecting"
                    samples.append(dict(fix))
                    accepted_total += 1
                    if quality_mode and len(samples) > sample_count:
                        samples.pop(0)
                    previous = marker
                    rejected_marker = None
                    self._record_progress(samples, accepted_total)
                    if quality_mode and len(samples) == sample_count:
                        progress["phase"] = "checking_spread"
                        averaged = average_fixes(samples)
                        averaged_quality = quality_check(averaged)
                        if averaged_quality["accepted"]:
                            progress["phase"] = "complete"
                            averaged["accuracy_observation"]["duration_sec"] = round(
                                time.ticks_diff(time.ticks_ms(), started) / 1000, 2)
                            return averaged
                        progress["waiting_reasons"] = list(averaged_quality["reasons"])
                elif sample_quality is not None:
                    progress["phase"] = "waiting_fix"
                    progress["waiting_reasons"] = list(sample_quality["reasons"])
                    if marker != rejected_marker:
                        progress["rejected_samples"] += 1
                        rejected_marker = marker
                if quality_mode or len(samples) < sample_count:
                    await asyncio.sleep_ms(250)
                    self._check_generation(generation)
            # Cancel and restart can coincide with the deadline.
            self._check_generation(generation)
            if not quality_mode and len(samples) < sample_count:
                reasons = progress.get("waiting_reasons") or []
                detail = (": " + ", ".join(reasons)) if reasons else ""
                raise MeasurementRejected(
                    "Not enough valid GNSS samples were received%s." % detail, reasons)
            averaged = average_fixes(samples)
            averaged["accuracy_observation"]["duration_sec"] = round(
                time.ticks_diff(time.ticks_ms(), started) / 1000, 2)
            progress["phase"] = "complete"
            return averaged
        finally:
            self._finish(generation)


measurement_session = MeasurementSession()
