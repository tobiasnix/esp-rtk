# SPDX-License-Identifier: AGPL-3.0-only
"""Runtime state: queue, metrics, task oversight. Hold the objects that all tasks share - app (state and number), shutdown_event (cancellation signal) and _instances (for the Ctrl-C cleanup).
"""
import time
import uasyncio as asyncio
from collections import deque

from cfg import CONFIG, WATCHED_TASKS, backoff_delay, log
from cfg_logging import HOT_PATH_LOG_INTERVAL_MS, LogThrottle
from state.link import NTRIP_STATES, LinkState, corrections_health
from state.receiver import GST_GRACE_MS, ReceiverState
from state.storage import StorageState
from state.system import SystemState, _instances, clock_is_valid


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

# A permanently crashing task (e.g. EADDRINUSE in start_server) must not cost log lines,
# a traceback and heap every 2 s: the wait between restarts doubles up to this cap.
SUPERVISE_MAX_DELAY_SEC = 60
# A run of at least this long counts as recovered and resets the backoff. It is measured
# with app.uptime_seconds(): ticks_diff turns negative after half a tick period.
SUPERVISE_STABLE_SEC = 60

def _restart_wait(name, crashes, restart_delay):
    """Seconds to wait before restart number `crashes` (1 = first consecutive crash)."""
    if restart_delay <= 0:
        return 0
    cap = SUPERVISE_MAX_DELAY_SEC
    timeout = CONFIG.get("heartbeat_timeout_sec", 60)
    if name in WATCHED_TASKS and timeout > 0:
        # The supervisor never beats for a task; waiting beyond the heartbeat deadline
        # would only produce a false watchdog report (see docs/technical.md).
        cap = min(cap, max(1, timeout // 2))
    return min(backoff_delay(crashes, restart_delay, cap), cap)

async def supervise(name, factory, restart_delay=2):
    """Until V9.3 each task ran naked in asyncio.gather(): a single exception in watchdog_task, gc_task, StatusLED, BleSenderTask or NetworkManager finished the entire program, and after that only the WDT ran until the reset. factory must generate the coroutine NEW - an already consumed one cannot wait a second time.

    A crashed task is restarted after restart_delay seconds, doubling per consecutive crash up to SUPERVISE_MAX_DELAY_SEC; a run of at least SUPERVISE_STABLE_SEC resets the backoff. For WATCHED_TASKS the wait is capped at half the heartbeat timeout, and the supervisor never beats on behalf of the task. restart_delay=0 never waits. A task that returns normally is not restarted.
    """
    crashes = 0
    while not shutdown_event.is_set():
        started = app.uptime_seconds()
        try:
            await factory()
            return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if app.uptime_seconds() - started >= SUPERVISE_STABLE_SEC:
                crashes = 0
            crashes += 1
            delay = _restart_wait(name, crashes, restart_delay)
            app.stats["task_restarts"] += 1
            log("ERROR", "SYS", "Task %s crashed: %s; restart in %d s" % (name, e, delay),
                print_traceback=True, exception=e)
            app.log_error(name, str(e))
            if shutdown_event.is_set():
                return
            await asyncio.sleep(delay)

# ---------------------------------------------------------------------------
# ROBUSTE ASYNCHRONE QUEUE
# ---------------------------------------------------------------------------

class SimpleQueue:
    """FIFO queue for one event loop; maxsize <= 0 is unbounded.

    A bounded queue sits on a deque (popleft without the list memmove), an
    unbounded one stays a list: MicroPython has no deque without maxlen, and an
    artificial bound would change the contract. full() is checked before every
    append, so the deque never drops an item silently. Waiting reuses two events
    made once here instead of one per waiting call: every waiter wakes on set()
    and checks its condition again, the items keep their FIFO order.
    """
    def __init__(self, maxsize=0):
        self._maxsize = maxsize
        self._bounded = maxsize > 0
        self._queue = deque((), maxsize) if self._bounded else []
        self._readable = asyncio.Event()
        self._writable = asyncio.Event()

    def qsize(self):
        return len(self._queue)

    def full(self):
        return self._bounded and len(self._queue) >= self._maxsize

    async def put(self, item):
        while self.full() and not shutdown_event.is_set():
            self._writable.clear()
            await self._writable.wait()

        if shutdown_event.is_set():
            return

        self._queue.append(item)
        self._readable.set()

    def put_nowait(self, item):
        """Put down an element without waiting; false means full queue."""
        if self.full() or shutdown_event.is_set():
            return False
        self._queue.append(item)
        self._readable.set()
        return True

    async def get(self):
        while not len(self._queue) and not shutdown_event.is_set():
            self._readable.clear()
            await self._readable.wait()

        if shutdown_event.is_set():
            return None

        item = self._queue.popleft() if self._bounded else self._queue.pop(0)
        self._writable.set()
        return item

    def task_done(self):
        pass

Queue = SimpleQueue

# ---------------------------------------------------------------------------
# APP STATE
# ---------------------------------------------------------------------------

class AppState(ReceiverState, LinkState, SystemState, StorageState):
    def __init__(self):
        self.stats = {
            "wifi_reconnects": 0, "ntrip_bytes": 0, "gnss_msgs": 0, "crc_errors": 0,
            "ntrip_retries": 0, "last_ntrip_error": "N/A", "http_errors": 0,
            "queue_overflows": 0, "uptime_sec": 0,
            # ble_drops is the sum of every lost BLE output: fan-out queue overflow
            # plus abandoned notify.
            "ble_drops": 0, "ble_queue_overflows": 0, "ble_notify_drops": 0,
            "task_restarts": 0, "gga_parse_errors": 0, "ntrip_backoff_sec": 0,
            "gga_parse_us": 0, "gga_parse_us_max": 0,
            "gga_parse_us_avg": 0, "gga_parse_over_budget": 0,
            # Parsed GGA sentences; gnss_msgs above is its alias until the alias removal after the state package (#20).
            "gga_parsed": 0,
            "ntrip_gga_sent": 0, "nmea_tcp_drops": 0,
            # wifi_ssid_aktiv: alias of wifi_ssid_active until the alias removal after the state package (#20).
            "wifi_ssid_active": "", "wifi_ssid_aktiv": "",
            "ap_ip": CONFIG["ap_ip"], "sta_ip": "N/A", "net_mode": "INIT",
            "access_state": "CONNECTING", "ntrip_state": "disabled",
            "ntrip_substate": None, "ntrip_endpoint": None,
            # Effective NTRIP configuration the running client read; None until it started.
            "ntrip_config": None,
            "gst_output_state": "waiting", "gst_output_error": None,
            "gst_enable_attempts": 0,
            # tcp_auth_failures: wrong or missing key only, a subset of every rejected
            # TCP login (nmea_tcp_auth_failures, written by fanout.py).
            "tcp_auth_failures": 0, "ble_auth_failures": 0,
            "nmea_tcp_client_stats": [], "tcp_client_queue_max": 0, "nmea_tcp_rejected_full": 0,
            "telemetry_drops": 0,
            "uart_buffer_overflows": 0,
            "uart_last_buffer_overflow": None,
            "uart_rx_reads": 0, "uart_rx_bytes": 0,
            "uart_rx_available_max": 0, "uart_rx_chunk_max": 0,
            "uart_last_read_bytes": 0, "uart_last_read_ticks_ms": None,
            "uart_last_read_duration_ms": 0,
            "uart_read_gap_ms": 0, "uart_read_gap_max_ms": 0,
            "loop_lag_ms": 0, "loop_lag_max_ms": 0,
            "uart_rx_rate_bytes_sec": 0, "uart_rx_rate_max_bytes_sec": 0,
            "uart_rx_rate_window_ms": 0, "nmea_last_crc_error": None,
            "tracking_queue_overflows": 0, "tracking_queue_depth": 0,
            "tracking_write_errors": 0, "legacy_conversion_errors": 0,
            "tracking_max_append_ms": 0, "tracking_appends_over_1000ms": 0,
            "http_requests": 0,
            "http_rejected_requests": 0,
            "last_wifi_error": None, "wifi_status_code": None,
            "route_rejected_points": 0, "route_invalid_fixes": 0,
            "boot_count": 0, "reset_cause": "?", "safe_mode": False,
            # Event stream (#27): accepted HTTP connections, active and open streams,
            # refusals and failures, written frames, the largest frame and hub tick.
            "http_connections_total": 0, "sse_streams": 0, "sse_open": 0,
            "sse_evictions": 0, "sse_refusals": 0, "sse_write_timeouts": 0,
            "sse_stream_errors": 0, "sse_frames": 0, "sse_frame_bytes_max": 0,
            "sse_tick_max_ms": 0, "sse_snapshot_errors": 0,
            # How long the device was in which fixed quality (seconds per GGA field 6).
            # Answered "have I ever been to 4?" without continuous observation - the log
            # only reports change.
            "fix_quality_seconds": {}
        }
        self._init_receiver()
        self._init_link()
        self._init_system()
        self._init_storage()

app = AppState()
