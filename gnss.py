# SPDX-License-Identifier: AGPL-3.0-only
"""GNSS handler and status LED. Reads NMEA from UART1, checks the price sum, puts the strings in the queue for BLE and draws the fix for status and LED from GGA.
"""
import math
import time
import ubinascii
import uasyncio as asyncio
from machine import Pin, UART, RTC

from cfg import CONFIG, FIX_STATUS, NMEA_WHITELIST, log
from field_diagnostics import recorder as field_recorder
from state import app, shutdown_event


# ---------------------------------------------------------------------------
# COMMUNICATION CANNEL FOR THE RECOMMENDANT
# ---------------------------------------------------------------------------

# Just proprietary configuration sets. Everything that goes through here goes directly
# into the UART of the receiver - so the list remains short. $PQTM...Quectel (LC29H),
# $PAIR...Airoha/MediaTek subset
COMMAND_PREFIXES = ("$PQTM", "$PAIR")
COMMAND_MAX_LEN = 120
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


def send_command(uart, sentence):
    """Write a verified command in the UART to the Recipient."""
    checked, error = check_command(sentence)
    if error:
        return False, error
    try:
        payload = (checked + "\r\n").encode()
        if uart.write(payload) != len(payload):
            return False, "UART write incomplete."
    except Exception as e:
        return False, "UART error: %s" % e
    log("INFO", "GNSS", "Command sent to module: %s" % checked)
    return True, None


class StatusLED:
    def __init__(self, pin_num=CONFIG["status_led_pin"]):
        self.led = Pin(pin_num, Pin.OUT)

    async def run(self):
        log("INFO", "GNSS", "Status LED task started (Pin %s)." % CONFIG["status_led_pin"])
        while not shutdown_event.is_set():
            fix = app.last_fix
            fix_qual = fix["qual"] if fix else 0

            if fix_qual == 0:
                interval = 0.2
            elif fix_qual == 4:
                self.led.on()
                await asyncio.sleep(1)
                continue
            else:
                interval = 1.0

            self.led.value(not self.led.value())
            await asyncio.sleep(interval)
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


def clock_is_valid():
    return 2024 <= time.gmtime()[0] <= 2099


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
            coordinate = (float(fix["lon"]), float(fix["lat"]), float(fix["alt"]))
            valid = (all(math.isfinite(value) for value in coordinate) and
                     abs(coordinate[0]) <= 180 and abs(coordinate[1]) <= 90 and
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


class GNSSHandler:
    def __init__(self, nmea_queue):
        log("INFO", "GNSS", "Initializing GNSS UART...")
        self.uart = UART(CONFIG["uart_id"], baudrate=CONFIG["uart_baud"],
                         tx=CONFIG["tx_pin"], rx=CONFIG["rx_pin"],
                         rxbuf=CONFIG.get("uart_rxbuf_bytes", 16384),
                         txbuf=2048, timeout=0, timeout_char=0)
        self.queue = nmea_queue
        self.gsv_count = 0
        self.gsv_reset_time = time.ticks_ms()
        self.last_gst = None
        self.last_gst_time = None
        self._uart_read_at = None
        self._uart_rate_started = time.ticks_ms()
        self._uart_rate_bytes = 0
        self._gst_attempts = 0
        self._gst_attempt_at = None
        self.route_startup = RouteStartupGuard()
        app.stats["route_startup"] = self.route_startup.status()

    def _ensure_gst_output(self, now):
        """Reapply the volatile GST setting without blocking UART processing."""
        if self.last_gst_time is not None and time.ticks_diff(now, self.last_gst_time) < 5000:
            app.stats["gst_output_state"] = "receiving"
            return
        delay = GST_RETRY_MS[min(max(0, self._gst_attempts - 1), len(GST_RETRY_MS) - 1)]
        if self._gst_attempt_at is not None and time.ticks_diff(now, self._gst_attempt_at) < delay:
            return
        self._gst_attempt_at = now
        self._gst_attempts += 1
        app.stats["gst_enable_attempts"] = app.stats.get("gst_enable_attempts", 0) + 1
        success, error = send_command(self.uart, GST_ENABLE_COMMAND)
        app.stats["gst_output_state"] = "requested" if success else "uart_error"
        app.stats["gst_output_error"] = error

    def _sync_rmc_clock(self, line):
        if clock_is_valid():
            return
        stamp = rmc_datetime(line)
        if stamp is None:
            return
        try:
            RTC().datetime(stamp)
            app.stats["clock_source"] = "GNSS_RMC"
            log("INFO", "GNSS", "Clock synchronized from GNSS UTC.")
        except Exception as error:
            app.stats["gnss_clock_error"] = str(error)

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
        self._uart_rate_bytes += size
        elapsed = max(0, time.ticks_diff(completed_ms, self._uart_rate_started))
        if elapsed >= 1000:
            # A stalled reader can make this window longer than one second.
            # Report its actual duration instead of calling a burst a wire rate.
            rate = (self._uart_rate_bytes * 1000) // elapsed
            stats["uart_rx_rate_bytes_sec"] = rate
            stats["uart_rx_rate_max_bytes_sec"] = max(
                stats.get("uart_rx_rate_max_bytes_sec", 0), rate)
            stats["uart_rx_rate_window_ms"] = elapsed
            self._uart_rate_started, self._uart_rate_bytes = completed_ms, 0

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
        """Bound complete-line backlog as well as unterminated garbage."""
        total = len(pending) + len(chunk)
        app.stats["uart_buffer_peak_bytes"] = max(
            app.stats.get("uart_buffer_peak_bytes", 0), total)
        if total <= MAX_NMEA_BUFFER_BYTES:
            return pending + chunk
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
            if not line.startswith("$") or "*" not in line:
                return False

            data, hex_sum = line[1:].split("*")
            calculated = 0
            for char in data:
                calculated ^= ord(char)

            return calculated == int(hex_sum, 16)
        except Exception:
            return False

    @staticmethod
    def nmea_to_deg(coord_str, hemi):
        """NMEA coordinate (ddmm.mmmm) in decimal degrees - or none. Earlier, when the field was empty, 0.0 came out, so exactly in the case of "no fix". The display then showed 0.0000000/0.0000000 as a real position (Zero Island in the Gulf of Guinea).
        """
        if not coord_str:
            return None
        try:
            v = float(coord_str)
            deg = int(v // 100)
            mins = v - deg * 100
            val = deg + (mins / 60.0)
            return -val if hemi in ("S", "W") else val
        except ValueError:
            return None

    def parse_gga(self, line):
        try:
            # The checksum does not belong to the reference station ID.
            payload = line.split("*", 1)[0]
            parts = payload.split(",")
            if len(parts) < 15:
                return None

            fix_quality = int(parts[6] or 0)

            # Without a fix no position - even if the recipient still writes the last
            # known in the fields.
            if fix_quality == 0:
                lat = lon = None
            else:
                lat = GNSSHandler.nmea_to_deg(parts[2], parts[3])
                lon = GNSSHandler.nmea_to_deg(parts[4], parts[5])

            return {
                "type": "GGA", "utc": parts[1],
                # The unchanged set - the NTRIP client sends it back to the caster, VRS
                # mountpoints demand it.
                "raw": line,
                "lat": lat,
                "lon": lon,
                "qual": fix_quality,
                "fix_status_text": FIX_STATUS.get(fix_quality, "UNKNOWN"),
                "sats": int(parts[7] or 0),
                "hdop": float(parts[8]) if parts[8] else None,
                # GGA field 9 is orthometric height above mean sea level.
                "alt": float(parts[9]) if parts[9] else None,
                "altitude_msl_m": float(parts[9]) if parts[9] else None,
                "geoid_sep_m": float(parts[11]) if parts[11] else None,
                "altitude_ellipsoid_m": (
                    float(parts[9]) + float(parts[11])
                    if parts[9] and parts[11] else None),
                "correction_age_sec": (float(parts[13]) if parts[13] else None),
                # Keep as a string because station IDs can have leading zeros.
                "station_id": parts[14] or None,
            }
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
        last_read_time = time.ticks_ms()
        app.beat("gnss")

        while not shutdown_event.is_set():
            current_time = time.ticks_ms()
            app.beat("gnss")
            chunk = None

            try:
                self._ensure_gst_output(current_time)
                chunk = self._read_uart()
                if chunk:
                    last_read_time = time.ticks_ms()

                if chunk:
                    buf = self._append_uart_chunk(buf, chunk)
                    processed = 0
                    while b"\n" in buf:
                        raw_line, buf = buf.split(b"\n", 1)
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
                                buf = self._append_uart_chunk(buf, more)
                                last_read_time = time.ticks_ms()
                            current_time = time.ticks_ms()
                            app.beat("gnss")

                        try:
                            # Keep legacy filtering for unknown/proprietary lines,
                            # but never accept a whitelisted sentence repaired by
                            # silently dropping a corrupted non-ASCII byte.
                            ascii_error = any(byte > 127 for byte in raw_line)
                            line_str = raw_line.decode("ascii", "ignore").strip()
                        except Exception:
                            continue

                        if not line_str or not line_str.startswith("$"):
                            continue

                        nmea_type = line_str[3:6]
                        if ascii_error and nmea_type not in NMEA_WHITELIST:
                            # Replacing a talker byte can shift the decoded type;
                            # retain a recognizable type in the original framing.
                            raw_type = raw_line.strip()[3:6].decode("ascii", "ignore")
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
                            self._record_crc_error(raw_line, nmea_type, ascii_error, len(buf))
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

                        if not self.queue.put_nowait((line_str + "\r\n").encode()):
                            app.stats["queue_overflows"] += 1
                            if app.stats["queue_overflows"] % 100 == 0:
                                log("ERROR", "GNSS", "NMEA queue full; %d overflows so far."
                                    % app.stats["queue_overflows"])
                                app.log_error("GNSS", "NMEA Queue Overflow.")

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
                                app.stats["gnss_msgs"] += 1
                                # Only with fix: the NTRIP client sends this set back to
                                # the caster.
                                if fix["qual"] > 0:
                                    app.last_gga_raw = fix["raw"]
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

                if chunk:
                    await asyncio.sleep_ms(1)
                elif time.ticks_diff(current_time, last_read_time) > 1000:
                    await asyncio.sleep_ms(100)
                else:
                    await asyncio.sleep_ms(5)

            except Exception as e:
                error_msg = str(e)
                log("ERROR", "GNSS", "GNSS handler error.", print_traceback=True, exception=e)
                app.log_error("GNSS", error_msg)
                await asyncio.sleep(1)
        log("INFO", "GNSS", "GNSS handler task stopped.")
