# SPDX-License-Identifier: AGPL-3.0-only
"""Runtime state: queue, metrics, task oversight. Hold the objects that all tasks share - app (state and number), shutdown_event (cancellation signal) and _instances (for the Ctrl-C cleanup).
"""
import time
import uasyncio as asyncio

from cfg import CONFIG, WATCHED_TASKS, log


# ---------------------------------------------------------------------------
# GRACEFUL SHUTDOWN HANDLER
# ---------------------------------------------------------------------------

shutdown_event = asyncio.Event()

def stale_heartbeat(timeout_sec):
    """Name of the task whose signs of life are the longest - otherwise none. The watchdog alone only notices whether the event loop is still running. A hanging NTRIP task or a dataless GNSS UART remain undetected.
    """
    jetzt = time.ticks_ms()
    threshold = timeout_sec * 1000
    worst, oldest = None, threshold
    for name in WATCHED_TASKS:
        stamp = app.heartbeat.get(name)
        if stamp is None:
            continue                    # Task does not (yet) run
        age = time.ticks_diff(jetzt, stamp)
        if age > oldest:
            worst, oldest = name, age
    return worst

async def supervise(name, factory, restart_delay=2):
    """Until V9.3 each task ran naked in asyncio.gather(): a single exception in watchdog_task, gc_task, StatusLED, BleSenderTask or NetworkManager finished the entire program, and after that only the WDT ran until the reset. factory must generate the coroutine NEW - an already consumed one cannot wait a second time.
    """
    while not shutdown_event.is_set():
        try:
            await factory()
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            app.stats["task_restarts"] += 1
            log("ERROR", "SYS", "Task %s crashed: %s" % (name, e),
                print_traceback=True, exception=e)
            app.log_error(name, str(e))
            if shutdown_event.is_set():
                return
            await asyncio.sleep(restart_delay)

# ---------------------------------------------------------------------------
# ROBUSTE ASYNCHRONE QUEUE
# ---------------------------------------------------------------------------

class SimpleQueue:
    def __init__(self, maxsize=0):
        self._maxsize = maxsize
        self._queue = []
        self._waiting_getters = []
        self._waiting_putters = []

    def qsize(self):
        return len(self._queue)

    def full(self):
        return self._maxsize > 0 and len(self._queue) >= self._maxsize

    async def put(self, item):
        while self.full() and not shutdown_event.is_set():
            event = asyncio.Event()
            self._waiting_putters.append(event)
            try:
                await event.wait()
            finally:
                if event in self._waiting_putters:
                    self._waiting_putters.remove(event)

        if shutdown_event.is_set():
            return

        self._queue.append(item)

        if self._waiting_getters:
            event = self._waiting_getters.pop(0)
            event.set()

    def put_nowait(self, item):
        """Put down an element without waiting; false means full queue."""
        if self.full() or shutdown_event.is_set():
            return False
        self._queue.append(item)
        if self._waiting_getters:
            self._waiting_getters.pop(0).set()
        return True

    async def get(self):
        while not self._queue and not shutdown_event.is_set():
            event = asyncio.Event()
            self._waiting_getters.append(event)
            try:
                await event.wait()
            finally:
                if event in self._waiting_getters:
                    self._waiting_getters.remove(event)

        if shutdown_event.is_set():
            return None

        item = self._queue.pop(0)

        if self._waiting_putters:
            event = self._waiting_putters.pop(0)
            event.set()

        return item

    def task_done(self):
        pass

Queue = SimpleQueue

# ---------------------------------------------------------------------------
# APP STATE
# ---------------------------------------------------------------------------

class AppState:
    def __init__(self):
        self._last_fix = None
        self._last_update_ms = 0
        self._uptime_at = time.ticks_ms()
        self._uptime_ms = 0
        self._last_fix_status = None
        self._last_fix_time = None
        self.stats = {
            "wifi_reconnects": 0, "ntrip_bytes": 0, "gnss_msgs": 0, "crc_errors": 0,
            "ntrip_retries": 0, "last_ntrip_error": "N/A", "http_errors": 0,
            "queue_overflows": 0, "ble_drops": 0, "uptime_sec": 0,
            "task_restarts": 0, "gga_parse_errors": 0, "ntrip_backoff_sec": 0,
            "ntrip_gga_sent": 0, "nmea_tcp_drops": 0, "wifi_ssid_aktiv": "",
            "ap_ip": CONFIG["ap_ip"], "sta_ip": "N/A", "net_mode": "INIT",
            "access_state": "CONNECTING", "ntrip_state": "disabled",
            "ntrip_endpoint": None,
            "tcp_auth_failures": 0, "ble_auth_failures": 0,
            "telemetry_drops": 0,
            "uart_buffer_overflows": 0,
            "uart_last_buffer_overflow": None,
            "uart_rx_reads": 0, "uart_rx_bytes": 0,
            "uart_rx_available_max": 0, "uart_rx_chunk_max": 0,
            "uart_last_read_bytes": 0, "uart_last_read_ticks_ms": None,
            "uart_last_read_duration_ms": 0,
            "uart_read_gap_ms": 0, "uart_read_gap_max_ms": 0,
            "uart_rx_rate_bytes_sec": 0, "uart_rx_rate_max_bytes_sec": 0,
            "uart_rx_rate_window_ms": 0, "nmea_last_crc_error": None,
            "tracking_queue_overflows": 0, "tracking_queue_depth": 0,
            "tracking_write_errors": 0,
            "tracking_max_append_ms": 0, "tracking_appends_over_1000ms": 0,
            "http_requests": 0,
            "last_wifi_error": None, "wifi_status_code": None,
            "route_rejected_points": 0, "route_invalid_fixes": 0,
            "boot_count": 0, "reset_cause": "?", "safe_mode": False,
            # How long the device was in which fixed quality (seconds per GGA field 6).
            # Answered "have I ever been to 4?" without continuous observation - the log
            # only reports change.
            "fix_quality_seconds": {}
        }
        self._fix_qual = None
        self._fix_qual_seit = None
        self._fix_qual_changed_ms = None
        self._last_rtk_fixed_ms = None
        self._gate_accepted = None
        self._gate_changed_ms = None
        self.wifi_connected_event = asyncio.Event()
        self.errors = []
        # Signs of life of the long-running tasks; the watchdog feeds only as long as
        # everyone is fresh.
        self.heartbeat = {}
        # Explicit flash maintenance can temporarily stall UART servicing.
        # Normal heartbeat enforcement resumes as soon as the operation ends.
        self.maintenance_reason = None
        # The unchanged last GGA set with fix - the NTRIP client sends it back to the
        # caster.
        self.last_gga_raw = None
        self.identity = None

    def uptime_seconds(self):
        """Accumulate monotonic uptime across RTC corrections and tick wrap."""
        now = time.ticks_ms()
        self._uptime_ms += max(0, time.ticks_diff(now, self._uptime_at))
        self._uptime_at = now
        return self._uptime_ms // 1000

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
        allowed = ("disabled", "waiting_wifi", "connecting", "streaming",
                   "backoff", "auth_error", "mount_error")
        if state not in allowed:
            raise ValueError("Unbekannter NTRIP-State: %s" % state)
        previous = self.stats.get("ntrip_state")
        self.stats["ntrip_state"] = state
        recorder = _instances.get("field_diagnostics")
        if recorder is not None and previous != state:
            recorder.event("ntrip_state", {"previous": previous, "state": state,
                           "profile": self.stats.get("wifi_profile"),
                           "endpoint": self.stats.get("ntrip_endpoint")})

    @property
    def last_fix(self):
        return self._last_fix

    @property
    def last_fix_time(self):
        return self._last_fix_time

    def update_fix(self, fix_data):
        current_ms = time.ticks_ms()
        recorder = _instances.get("field_diagnostics")
        if recorder is not None and (self._last_fix or {}).get("qual") != fix_data.get("qual"):
            recorder.event("fix_change", {"previous": (self._last_fix or {}).get("qual"),
                           "qual": fix_data.get("qual"), "gga_utc": fix_data.get("utc")})
        self.track_fix_quality(fix_data.get("qual", 0), current_ms)
        if time.ticks_diff(current_ms, self._last_update_ms) > CONFIG["fix_update_interval_ms"]:
            # Imported here to keep state.py independent during module startup.
            from tracking import quality_check
            accepted = bool(quality_check(fix_data)["accepted"])
            if accepted != self._gate_accepted:
                self._gate_accepted = accepted
                self._gate_changed_ms = current_ms
            self._last_fix = fix_data
            self._last_update_ms = current_ms
            self._last_fix_time = time.time()
            try:
                from tracking import tracker
                queued = tracker.enqueue_fix(fix_data)
                if queued is None:
                    self.stats["tracking_queue_overflows"] += 1
            except Exception as error:
                log("ERROR", "TRACK", "Automatic tracking point failed: %s" % error)
                self.log_error("TRACK", str(error))

            current_status = fix_data["fix_status_text"]

            if current_status != self._last_fix_status:
                log("INFO", "GNSS", "Fix: %s | Sats: %s | HDOP: %.1f"
                    % (current_status, fix_data["sats"], fix_data["hdop"]))
                self._last_fix_status = current_status

    def attach_gst(self, gst):
        """Attach a same-epoch GST estimate when GST arrives after GGA."""
        if not self._last_fix or self._last_fix.get("utc") != gst.get("utc"):
            return False
        enriched = dict(self._last_fix)
        enriched["receiver_accuracy"] = dict(gst)
        self._last_fix = enriched
        try:
            from tracking import tracker
            queued = tracker.enqueue_fix(enriched)
            if queued is None:
                self.stats["tracking_queue_overflows"] += 1
        except Exception as error:
            log("ERROR", "TRACK", "Automatic tracking point failed: %s" % error)
            self.log_error("TRACK", str(error))
        return True

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
        return {
            "fix_quality": self._fix_qual,
            "streak_sec": seconds_since(self._fix_qual_changed_ms),
            "gate_accepted": self._gate_accepted,
            "gate_streak_sec": seconds_since(self._gate_changed_ms),
            "since_rtk_fixed_sec": seconds_since(self._last_rtk_fixed_ms),
        }

    def beat(self, name):
        """Lebenszeichen eines Tasks."""
        self.heartbeat[name] = time.ticks_ms()

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

app = AppState()

# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

# Modular global references for the Ctrl-C-Cleanup: In V9.0, "if 'gnss' in locals()" ran
# into the void in the except block because gnss lived locally in main() - the cleanup
# was never executed.
_instances = {}
