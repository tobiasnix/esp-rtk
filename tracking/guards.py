# SPDX-License-Identifier: AGPL-3.0-only
"""Storage guards and the errors of the survey store."""
import os


_STORAGE_BUSY_MESSAGES = {
    "restore_install_pending": "A survey restore is published but not installed.",
    "survey_loading": "Survey storage is loading or unavailable.",
    "storage_busy": "Storage maintenance in progress. Please wait.",
    "recording_active": "Finish the active recording before storage maintenance.",
}


class StorageBusy(ValueError):
    """A guard refused storage access; code names the protected state."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class StorageReserveError(ValueError):
    """A write would let free flash fall below the protected reserve."""


# undo_last() without an expectation: the pre-#21c client path.
UNDO_UNCHECKED = object()
REQUEST_TAG_LIMIT = 8


class MeasurementRejected(ValueError):
    """A measurement failed its quality check; reasons holds the quality_check codes."""

    def __init__(self, message, reasons):
        super().__init__(message)
        self.reasons = tuple(reasons)


class UndoCandidateChanged(ValueError):
    """An undo expected another candidate than the current one; nothing was removed."""

    def __init__(self):
        super().__init__("The last recorded point changed. Nothing was removed.")


# RAM only: tells one survey inventory from the next (boot, restore); never stored (#21c).
def _new_inventory_epoch():
    return "".join("%02x" % byte for byte in os.urandom(4))


def _storage_busy(code):
    return StorageBusy(code, _STORAGE_BUSY_MESSAGES[code])


class _ReaderGuard:
    """Counts one storage reader; refused while maintenance or compaction runs."""

    def __init__(self, tracker):
        self._tracker = tracker

    async def __aenter__(self):
        if self._tracker.maintenance_active():
            raise _storage_busy("storage_busy")
        self._tracker.storage_readers += 1
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self._tracker.storage_readers -= 1
        return False


class _MaintenanceGuard:
    """Exclusive storage maintenance with its own maintenance-registry token.

    Nesting is allowed only below a parent guard of the same tracker that has
    fully entered (readers drained) and not yet left. Only the background
    checkpoint passes allow_recording=True.
    """

    def __init__(self, tracker, reason, progress, parent, allow_recording):
        self._tracker = tracker
        self._reason = reason
        self._progress = progress
        self._parent = parent
        self._allow_recording = allow_recording
        self._app = None
        self._token = None
        self._depth_taken = False
        self.entered = False

    async def __aenter__(self):
        import uasyncio as asyncio
        tracker, parent = self._tracker, self._parent
        # Every refusal happens before any state changes.
        if parent is not None and (not isinstance(parent, _MaintenanceGuard) or
                                   parent._tracker is not tracker or
                                   not parent.entered):
            raise _storage_busy("storage_busy")
        if tracker._install_pending:
            raise _storage_busy("restore_install_pending")
        if not tracker._loaded:
            raise _storage_busy("survey_loading")
        if parent is None and tracker.maintenance_active():
            raise _storage_busy("storage_busy")
        if not self._allow_recording and tracker.has_open_recording():
            raise _storage_busy("recording_active")
        from state import app
        self._app = app
        self._token = app.begin_maintenance(self._reason)
        try:
            tracker._maintenance_depth += 1
            self._depth_taken = True
            tracker.storage_busy = True
            if self._progress is not None:
                tracker.operation_progress.update(self._progress)
            if parent is None:
                while tracker.storage_readers:
                    await asyncio.sleep_ms(20)
        except BaseException:
            # Cancellation while waiting for readers or a failing progress
            # update: undo exactly what was taken, then re-raise.
            self._release()
            raise
        self.entered = True
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self._release()
        return False

    def update(self, **fields):
        self._tracker.operation_progress.update(fields)

    def _release(self):
        self.entered = False
        tracker = self._tracker
        if self._depth_taken:
            self._depth_taken = False
            tracker._maintenance_depth -= 1
        tracker.storage_busy = tracker._maintenance_depth > 0
        if self._app is not None:
            self._app.end_maintenance(self._token)
