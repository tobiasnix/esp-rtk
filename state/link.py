# SPDX-License-Identifier: AGPL-3.0-only
"""Link part of the runtime state: LinkState owns the access and NTRIP states and the receiver UART link watch.
"""
import time
import uasyncio as asyncio

from cfg import CONFIG, log
from state.system import _instances


# Published NTRIP states: the `$PESPS` contract, /status and the UI rely on
# exactly this set. `stale` is a substate (`ntrip_substate`), never a state.
NTRIP_STATES = ("disabled", "waiting_wifi", "connecting", "streaming",
                "backoff", "auth_error", "mount_error")


def corrections_health(ntrip_state, epoch_stale):
    """Correction link of the live snapshot (#20a): off while NTRIP is disabled, ok or
    stale while streaming (epoch_stale from RtcmCounter.age_is_stale, the one stale
    rule), down in every other state."""
    if ntrip_state == "disabled":
        return "off"
    if ntrip_state == "streaming":
        return "stale" if epoch_stale else "ok"
    return "down"


class LinkState:
    def _init_link(self):
        # Receiver link: _receiver_watch_ms is the tick the GNSS handler first ran
        # (None: not watched, e.g. Safe Mode); _uart_data_ticks_ms stamps the last
        # UART read that delivered bytes; _receiver_link_reported is the state the
        # last transition logged; _receiver_silent_stamp is the reference the silence
        # was measured from when it was reported; _uart_data_since_silent is set by
        # every delivered byte and cleared when silence is reported.
        self._receiver_watch_ms = None
        self._uart_data_ticks_ms = None
        self._receiver_link_reported = None
        self._receiver_silent_stamp = None
        self._uart_data_since_silent = False
        self.wifi_connected_event = asyncio.Event()

    def set_access_state(self, state):
        """Central, WebUI and later telemetry stable state."""
        allowed = ("CONNECTING", "ONLINE", "SETUP", "RECOVERY",
                   "APPLYING", "ERROR")
        if state not in allowed:
            raise ValueError("Unbekannter Access-State: %s" % state)
        previous = self.stats.get("access_state")
        self.stats["access_state"] = state
        recorder = _instances.get("field_diagnostics")
        if recorder is not None and previous != state:
            recorder.event("access_state", {"previous": previous, "state": state,
                           "profile": self.stats.get("wifi_profile"),
                           "endpoint": self.stats.get("ntrip_endpoint")})

    def set_ntrip_state(self, state):
        """Explicit NTRIP status instead of derivation from error messages."""
        if state not in NTRIP_STATES:
            raise ValueError("Unbekannter NTRIP-State: %s" % state)
        previous = self.stats.get("ntrip_state")
        self.stats["ntrip_state"] = state
        recorder = _instances.get("field_diagnostics")
        if recorder is not None and previous != state:
            recorder.event("ntrip_state", {"previous": previous, "state": state,
                           "profile": self.stats.get("wifi_profile"),
                           "endpoint": self.stats.get("ntrip_endpoint")})

    def start_receiver_watch(self, now_ms=None):
        """Start measuring receiver silence; only the first call counts.

        A restarted GNSS task keeps the start, so a receiver that never sent a byte
        stays silent across task restarts."""
        if self._receiver_watch_ms is None:
            self._receiver_watch_ms = time.ticks_ms() if now_ms is None else now_ms

    def note_uart_data(self, ticks_ms):
        """Stamp a UART read that delivered bytes. Runs per read: two stores only."""
        self._uart_data_ticks_ms = ticks_ms
        self._uart_data_since_silent = True

    def uart_silence_ms(self, now_ms=None):
        """Ticks since the last delivered UART byte (or the watch start); None unwatched.

        A negative difference reads as 2**29 like fix_age_ms. Once silence was
        reported, a small age without a byte since can only be an aliased full tick
        wrap (2**30 ms) and also reads as 2**29."""
        if self._receiver_watch_ms is None:
            return None
        if now_ms is None:
            now_ms = time.ticks_ms()
        stamp = self._uart_data_ticks_ms
        if stamp is None:
            stamp = self._receiver_watch_ms
        silence = time.ticks_diff(now_ms, stamp)
        if silence < 0:
            silence = 2 ** 29
        elif (silence < CONFIG["fix_stale_after_ms"] and not self._uart_data_since_silent
                and self._receiver_link_reported == "silent"):
            silence = 2 ** 29
        return silence

    def receiver_link(self, now_ms=None):
        """Link state without side effects: unknown (unwatched), silent or ok.

        Silent from fix_stale_after_ms of silence on: the fix goes stale then too."""
        silence = self.uart_silence_ms(now_ms)
        if silence is None:
            return "unknown"
        return "silent" if silence >= CONFIG["fix_stale_after_ms"] else "ok"

    def check_receiver_link(self, now_ms=None):
        """Report a change of the receiver link once: a log line and a recorder event.

        Called by the GNSS loop after each read, so bytes waiting after a stalled
        turn renew the stamp first. None -> ok (first byte) is not logged; None ->
        silent (no byte since the watch start) warns."""
        if now_ms is None:
            now_ms = time.ticks_ms()
        silence = self.uart_silence_ms(now_ms)
        if silence is None:
            return "unknown"
        link = "silent" if silence >= CONFIG["fix_stale_after_ms"] else "ok"
        previous = self._receiver_link_reported
        if link == previous or (previous is None and self._uart_data_ticks_ms is None
                                and link == "ok"):
            # Before the first byte "ok" is only the grace period, not a reported state.
            return link
        self._receiver_link_reported = link
        if link == "silent":
            stamp = self._uart_data_ticks_ms
            self._receiver_silent_stamp = self._receiver_watch_ms if stamp is None else stamp
            self._uart_data_since_silent = False
            log("WARN", "GNSS", "Receiver UART silent for %d s; check the receiver cable and power."
                % (silence // 1000))
        elif previous is None:
            return link
        else:
            # The silence lasted from the reported reference to the first new byte; a
            # shorter span than the threshold can only be an aliased tick wrap.
            silence = time.ticks_diff(self._uart_data_ticks_ms, self._receiver_silent_stamp)
            if silence < CONFIG["fix_stale_after_ms"]:
                silence = 2 ** 29
            log("INFO", "GNSS", "Receiver UART data resumed after %d s." % (silence // 1000))
        recorder = _instances.get("field_diagnostics")
        if recorder is not None:
            recorder.event("receiver_link", {"previous": previous, "state": link,
                           "silence_ms": silence})
        return link
