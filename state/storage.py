# SPDX-License-Identifier: AGPL-3.0-only
"""Storage part of the runtime state: StorageState owns the flash maintenance registry and the recording probe.
"""


class StorageState:
    def _init_storage(self):
        # Explicit flash maintenance can temporarily stall UART servicing.
        # Normal heartbeat enforcement resumes as soon as the last operation ends:
        # every maintenance owns one token in this ordered list [(token, reason)].
        self._maintenance = []
        self._maintenance_token = 0
        self._recording_probe = None

    def begin_maintenance(self, reason):
        """Register one running maintenance; returns its token for end_maintenance."""
        self._maintenance_token += 1
        token = self._maintenance_token
        self._maintenance.append((token, reason))
        return token

    def end_maintenance(self, token):
        """End exactly this maintenance; unknown or already ended tokens are a no-op."""
        for index, entry in enumerate(self._maintenance):
            if entry[0] == token:
                del self._maintenance[index]
                return

    @property
    def maintenance_reason(self):
        """Reason of the most recently started maintenance still running, or None."""
        return self._maintenance[-1][1] if self._maintenance else None

    def set_recording_probe(self, probe):
        """Register the no-argument callable that tells whether a recording runs."""
        self._recording_probe = probe

    def recording_active(self):
        """True while a recording runs; blocking radio work (Wi-Fi scan, NTP) waits.

        Fail-open as before #18: no probe registered or a probe that raises counts as
        "not recording", so a broken probe never disables the scan or NTP for good."""
        probe = self._recording_probe
        if probe is None:
            return False
        try:
            return bool(probe())
        except Exception:
            return False
