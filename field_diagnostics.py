# SPDX-License-Identifier: AGPL-3.0-only
"""Optional bounded field recorder. Producers never touch flash.

NDJSON records have explicit UTC validity, boot/session IDs and monotonic uptime.
MSM masks describe received signals, not successful use by the receiver.
"""
import os
import time
import ubinascii
import ujson as json
import uasyncio as asyncio

MAX_SEGMENTS = 32
SEGMENT_BYTES = 131072
MAX_EVENTS = 32
MAX_NMEA = 48
MAX_RTCM_QUEUE = 8192
SAMPLE_INTERVAL_MS = 5000
FLUSH_INTERVAL_MS = 30000
MAX_SIGNAL_OBSERVATIONS = 128


def new_id():
    return ubinascii.hexlify(os.urandom(8)).decode()


def bits(data, start, width):
    value = 0
    for index in range(start, start + width):
        value = (value << 1) | ((data[index // 8] >> (7 - index % 8)) & 1)
    return value


class CorrectionProbe:
    """CRC24Q framing and MSM header masks; at most one 1029-byte frame.

    Header offsets follow RTCM MSM / RTKLIB decode_msm_head. No observation,
    frequency-name or ambiguity interpretation is performed on the device.
    """
    def __init__(self):
        self.buffer = bytearray()
        self.table = []
        for value in range(16):
            crc = value << 20
            for _ in range(4):
                crc = (crc << 1) ^ (0x1864cfb if crc & 0x800000 else 0)
            self.table.append(crc & 0xffffff)
        self.counts = {}
        self.masks = {}
        self.bad_crc = 0
        self.truncated_frames = 0
        self.mask_drops = 0
        self.last_ms = None

    def reset_stream(self):
        self.truncated_frames += bool(self.buffer)
        self.buffer = bytearray()

    def feed(self, data):
        for byte in data:
            buf = self.buffer
            if not buf and byte != 0xd3:
                continue
            buf.append(byte)
            if len(buf) == 2 and buf[1] & 0xfc:
                self.buffer = bytearray(b"\xd3" if byte == 0xd3 else b"")
                continue
            if len(buf) < 3:
                continue
            size = ((buf[1] & 3) << 8) + buf[2] + 6
            if len(buf) < size:
                continue
            crc = 0
            for value in buf[:-3]:
                crc ^= value << 16
                crc = ((crc << 4) & 0xffffff) ^ self.table[crc >> 20]
                crc = ((crc << 4) & 0xffffff) ^ self.table[crc >> 20]
            if crc == bits(buf, (size - 3) * 8, 24):
                self._frame(buf[3:-3])
                self.last_ms = time.ticks_ms()
            else:
                self.bad_crc += 1
            self.buffer = bytearray()

    def _frame(self, payload):
        if len(payload) < 2:
            return
        number = bits(payload, 0, 12)
        key = str(number)
        if key in self.counts or len(self.counts) < 64:
            self.counts[key] = self.counts.get(key, 0) + 1
        if not (1071 <= number <= 1127 and 1 <= number % 10 <= 7 and len(payload) >= 22):
            return
        satellites = [i + 1 for i in range(64) if bits(payload, 73 + i, 1)]
        signals = [i + 1 for i in range(32) if bits(payload, 137 + i, 1)]
        cell_count = len(satellites) * len(signals)
        if 169 + cell_count > len(payload) * 8:
            return
        station = bits(payload, 12, 12)
        # Different masks in multiple messages of the same type must survive.
        key = (number, station, tuple(satellites), tuple(signals))
        if key not in self.masks and len(self.masks) >= 32:
            self.mask_drops += 1
            return
        self.masks[key] = {"type": number, "station": station,
                           "satellite_ids": satellites, "signal_ids": signals,
                           "epoch_raw": bits(payload, 24, 30),
                           "multiple_message": bits(payload, 54, 1),
                           "active_cells": sum(bits(payload, 169 + i, 1)
                                               for i in range(cell_count))}

    def snapshot(self):
        result = {"types": self.counts, "msm": list(self.masks.values()),
                  "crc_failures": self.bad_crc, "truncated_frames": self.truncated_frames,
                  "mask_drops": self.mask_drops,
                  "valid_frame_age_ms": None if self.last_ms is None else
                  max(0, time.ticks_diff(time.ticks_ms(), self.last_ms))}
        self.counts = {}
        self.masks = {}
        return result


class FieldRecorder:
    def __init__(self, prefix="field-diagnostics", segment_bytes=SEGMENT_BYTES,
                 max_segments=MAX_SEGMENTS):
        self.prefix = prefix
        self.segment_bytes = segment_bytes
        self.max_segments = max_segments
        self.enabled = False
        self.initialized = False
        self.boot = new_id()
        self.session = None
        self.files = []
        self.current = None
        self.sequence = 0
        self.events = []
        self.nmea = []
        self.signals = {}
        self.used_satellites = {}
        self.rtcm_queue = []
        self.rtcm_bytes = 0
        self.probe = None
        self.error = None
        self.export_readers = 0
        self.dropped_events = 0
        self.dropped_nmea = 0
        self.dropped_rtcm_bytes = 0
        self.samples = 0
        self.rotations = 0
        self.last_write_ms = 0
        self.max_write_ms = 0
        self.flush_count = 0
        self.last_sample = None
        self._last_ticks = time.ticks_ms()
        self._uptime_ms = 0

    def stamp(self, kind, data):
        now = time.ticks_ms()
        self._uptime_ms += max(0, time.ticks_diff(now, self._last_ticks))
        self._last_ticks = now
        clock = time.gmtime()
        valid = clock[0] >= 2024
        utc = ("%04d-%02d-%02dT%02d:%02d:%02dZ" % clock[:6]) if valid else None
        return {"kind": kind, "boot": self.boot, "session": self.session,
                "utc": utc, "clock_valid": valid, "up_ms": self._uptime_ms,
                "data": data}

    def initialize(self):
        if self.initialized:
            return
        from state import app
        self._uptime_ms = app.uptime_seconds() * 1000
        self._last_ticks = time.ticks_ms()
        folder, base = self.prefix.rsplit("/", 1) if "/" in self.prefix else (".", self.prefix)
        for name in os.listdir(folder):
            stem = name[len(base) + 1:-6]
            if name.startswith(base + "-") and name.endswith(".jsonl") and len(stem) == 8 and stem.isdigit():
                path = folder + "/" + name
                self.files.append([int(stem), path, os.stat(path)[6]])
        self.files.sort()
        self.sequence = self.files[-1][0] if self.files else 0
        try:
            with open(self.prefix + ".state") as handle:
                saved = json.load(handle)
            self.session = saved.get("session")
            self.enabled = saved.get("enabled") is True and isinstance(self.session, str)
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        self.initialized = True
        if self.enabled:
            self.probe = CorrectionProbe()
            self.event("boot", {"reset_cause": app.stats.get("reset_cause"), "resumed": True})
            # Persist the boot before potentially lengthy survey-store recovery.
            self.flush()

    def _persist(self, enabled, session):
        temporary = self.prefix + ".state.tmp"
        with open(temporary, "w") as handle:
            json.dump({"enabled": enabled, "session": session}, handle)
        os.rename(temporary, self.prefix + ".state")

    def start(self):
        self.initialize()
        if self.export_readers:
            raise ValueError("export_in_progress")
        if self.enabled:
            return self.status()
        session = new_id()
        self._persist(True, session)
        self.session = session
        self.enabled = True
        self.error = None
        self.probe = CorrectionProbe()
        self.current = None
        self.last_sample = None
        self.nmea = []
        self.signals = {}
        self.used_satellites = {}
        self.rtcm_queue = []
        self.rtcm_bytes = 0
        self.event("start", {})
        self.flush()
        return self.status()

    def stop(self):
        self.initialize()
        # Disable the resume marker before flushing, including on write failure.
        self._persist(False, self.session)
        self.event("stop", {})
        self.flush()
        self.enabled = False
        self.probe = None
        self.nmea = []
        self.signals = {}
        self.used_satellites = {}
        self.rtcm_queue = []
        self.rtcm_bytes = 0
        self.current = None
        return self.status()

    def event(self, kind, data=None):
        if not self.enabled:
            return
        if len(self.events) >= MAX_EVENTS:
            self.dropped_events += 1
            return
        self.events.append(self.stamp(kind, data or {}))

    def observe_nmea(self, line):
        if not self.enabled:
            return
        if len(self.nmea) >= MAX_NMEA or len(line) > 160:
            self.dropped_nmea += 1
        else:
            self.nmea.append(line)

    def _consume_nmea(self, limit=4):
        """Compact queued GSV/GSA outside the GNSS receive path."""
        for _ in range(min(limit, len(self.nmea))):
            line = self.nmea.pop(0)
            try:
                fields = line.split("*", 1)[0].split(",")
                sentence_type = fields[0][3:6]
                if sentence_type == "GSV":
                    values = fields[4:]
                    signal = values[-1] if len(values) % 4 == 1 else ""
                    if signal:
                        values = values[:-1]
                    talker = fields[0][1:3]
                    for index in range(0, len(values) - 3, 4):
                        satellite = values[index]
                        if not satellite:
                            continue
                        key = (talker, signal, satellite)
                        if key not in self.signals and len(self.signals) >= MAX_SIGNAL_OBSERVATIONS:
                            self.dropped_nmea += 1
                            continue
                        self.signals[key] = [talker, signal, int(satellite),
                                             int(values[index + 1]) if values[index + 1] else None,
                                             int(values[index + 2]) if values[index + 2] else None,
                                             int(values[index + 3]) if values[index + 3] else None]
                elif sentence_type == "GSA":
                    system = fields[-1] if len(fields) > 18 else ""
                    self.used_satellites[system] = [int(value) for value in fields[3:15]
                                                    if value]
            except (ValueError, IndexError, TypeError):
                self.dropped_nmea += 1

    def _signal_snapshot(self):
        result = {"format": "talker,signal,satellite,elevation_deg,azimuth_deg,cn0_dbhz",
                  "observations": list(self.signals.values()),
                  "used_satellites": self.used_satellites}
        self.signals = {}
        self.used_satellites = {}
        return result

    def rtcm_boundary(self):
        if not self.enabled:
            return
        if len(self.rtcm_queue) >= MAX_EVENTS:
            self.dropped_rtcm_bytes += self.rtcm_bytes
            self.rtcm_queue = []
            self.rtcm_bytes = 0
        # Preserve order: consume old queued frames before resetting framing.
        self.rtcm_queue.append(None)

    def observe_rtcm(self, data):
        if not self.enabled:
            return
        if self.rtcm_bytes + len(data) > MAX_RTCM_QUEUE:
            self.dropped_rtcm_bytes += len(data)
            # A missing chunk invalidates the pending partial frame.
            self.dropped_rtcm_bytes += self.rtcm_bytes
            self.rtcm_queue = []
            self.rtcm_bytes = 0
            self.probe.reset_stream()
        else:
            self.rtcm_queue.append(bytes(data))
            self.rtcm_bytes += len(data)

    def _remove_oldest(self):
        old = self.files[0]
        if self.current is old:
            return False
        os.remove(old[1])
        self.files.pop(0)
        self.rotations += 1
        return True

    def _space(self, required):
        folder = self.prefix.rsplit("/", 1)[0] if "/" in self.prefix else "."
        while True:
            stat = os.statvfs(folder)
            if stat[0] * stat[3] - required >= stat[0] * stat[2] // 5:
                return
            if not self.files or not self._remove_oldest():
                raise OSError("storage_reserve")

    def _write(self, data):
        rotate = self.current is None or self.current[2] + len(data) > self.segment_bytes
        if rotate:
            self.current = None
            while len(self.files) >= self.max_segments:
                self._remove_oldest()
        # One reserve check per batch, including the possible segment header.
        self._space(len(data) + 8192)
        if rotate:
            self.sequence += 1
            path = "%s-%08d.jsonl" % (self.prefix, self.sequence)
            from cfg import CONFIG
            header = self.stamp("segment", {"schema": 2, "firmware": CONFIG.get("version"),
                                "sequence": self.sequence,
                                "sample_interval_ms": SAMPLE_INTERVAL_MS,
                                "flush_interval_ms": FLUSH_INTERVAL_MS,
                                "gsv_format": "compact latest checksum-valid GSV/GSA observations",
                                "rtcm_format": "CRC24Q-valid frame counts and MSM signal masks",
                                "power_loss": "pending records may be lost; ignore incomplete final lines"})
            with open(path, "wb") as handle:
                handle.write((json.dumps(header) + "\n").encode())
            self.current = [self.sequence, path, os.stat(path)[6]]
            self.files.append(self.current)
        with open(self.current[1], "ab") as handle:
            if handle.write(data) != len(data):
                raise OSError("short_write")
        self.current[2] += len(data)

    def flush(self):
        started = time.ticks_ms()
        had_records = bool(self.events)
        try:
            while self.events:
                # statvfs and close/commit are expensive on LittleFS. A burst of
                # connection events must share one reserve check and append.
                encoded = []
                size = 0
                limit = min(32768, self.segment_bytes // 2)
                for record in self.events:
                    line = (json.dumps(record) + "\n").encode()
                    if len(line) > limit:
                        raise ValueError("record_too_large")
                    if size + len(line) > limit:
                        break
                    encoded.append(line)
                    size += len(line)
                self._write(b"".join(encoded))
                del self.events[:len(encoded)]
            self.error = None
        except (OSError, ValueError, MemoryError) as error:
            self.error = "storage_reserve" if str(error) == "storage_reserve" else "write_failed"
            # Do not append valid JSON to a possibly torn final record.
            self.current = None
        self.last_write_ms = max(0, time.ticks_diff(time.ticks_ms(), started))
        self.max_write_ms = max(self.max_write_ms, self.last_write_ms)
        if had_records:
            self.flush_count += 1

    def sample(self):
        if not self.enabled:
            return
        from state import app, _instances
        fix = app.last_fix or {}
        network = _instances.get("network")
        ntrip = _instances.get("ntrip")
        gnss = _instances.get("gnss")
        radio = {"rssi": None, "channel": None}
        link_connected = None
        if network is not None:
            try:
                link_connected = network.wlan_sta.isconnected()
                if link_connected:
                    radio = {"rssi": network.wlan_sta.status("rssi"),
                             "channel": network.wlan_sta.config("channel")}
            except (OSError, ValueError, AttributeError):
                pass
        selected = {key: fix.get(key) for key in
                    ("utc", "qual", "sats", "hdop", "lat", "lon", "alt",
                     "correction_age_sec", "station_id", "receiver_accuracy")}
        gst = selected.get("receiver_accuracy")
        if gst:
            selected["receiver_accuracy"] = {key: gst.get(key) for key in
                ("utc", "horizontal_sigma_m", "altitude_sigma_m")}
        now = time.ticks_ms()
        flow = ntrip.flow_summary() if ntrip else {}
        data = {"fix": selected,
                "fix_age_ms": None if app.last_fix_time is None else
                max(0, time.ticks_diff(now, app._last_update_ms)),
                "sample_gap_ms": None if self.last_sample is None else time.ticks_diff(now, self.last_sample),
                "route_startup": app.stats.get("route_startup"),
                "wifi": {"state": app.stats.get("access_state"), "profile": app.stats.get("wifi_profile"),
                         "link_connected": link_connected,
                         "ready_event": app.wifi_connected_event.is_set(), "radio": radio},
                "ntrip": {"state": app.stats.get("ntrip_state"), "endpoint": app.stats.get("ntrip_endpoint"),
                          "bytes": app.stats.get("ntrip_bytes"), "gga_sent": app.stats.get("ntrip_gga_sent"),
                          "uart_short_writes": flow.get("uart_short_writes"),
                          "flow": {key: flow.get(key) for key in
                                   ("connections", "read_timeouts", "rx_age_ms", "rx_chunks",
                                    "rx_gap_max_ms", "uart_write_max_ms", "gga_failures",
                                    "gga_gap_max_ms")}},
                "storage": {"flush_count": self.flush_count,
                            "last_write_ms": self.last_write_ms,
                            "max_write_ms": self.max_write_ms},
                "rtcm": self.probe.snapshot(), "signal_summary": self._signal_snapshot(),
                "latest_gst": dict(gnss.last_gst) if gnss and gnss.last_gst else None,
                "gst_age_ms": (max(0, time.ticks_diff(now, gnss.last_gst_time))
                               if gnss and gnss.last_gst_time is not None else None),
                "drops": {"events": self.dropped_events, "nmea": self.dropped_nmea,
                          "rtcm_bytes": self.dropped_rtcm_bytes},
                "runtime": {key: app.stats.get(key) for key in
                            ("crc_errors", "gga_parse_errors", "queue_overflows", "task_restarts")}}
        self.last_sample = now
        self.samples += 1
        self.event("sample", data)

    def status(self):
        return {"enabled": self.enabled, "session": self.session, "boot": self.boot,
                "error": self.error, "samples_this_boot": self.samples,
                "stored_bytes": sum(item[2] for item in self.files),
                "limit_bytes": self.max_segments * self.segment_bytes,
                "segments": len(self.files), "rotations_this_boot": self.rotations,
                "dropped_events": self.dropped_events, "dropped_nmea": self.dropped_nmea,
                "dropped_rtcm_bytes": self.dropped_rtcm_bytes,
                "max_write_ms": self.max_write_ms,
                "sample_interval_ms": SAMPLE_INTERVAL_MS,
                "flush_interval_ms": FLUSH_INTERVAL_MS,
                "pending_records": len(self.events),
                "pending_nmea": len(self.nmea)}

    async def run(self):
        from state import app, shutdown_event
        self.initialize()
        due = time.ticks_ms()
        flush_due = due
        while not shutdown_event.is_set():
            app.beat("field_diagnostics")
            if self.enabled:
                # Bounded extra work, after corrections have already reached UART.
                if self.rtcm_queue:
                    chunk = self.rtcm_queue.pop(0)
                    if chunk is None:
                        self.probe.reset_stream()
                    else:
                        self.rtcm_bytes -= len(chunk)
                        self.probe.feed(chunk)
                    await asyncio.sleep_ms(0)
                self._consume_nmea(4)
                now = time.ticks_ms()
                if time.ticks_diff(now, due) >= SAMPLE_INTERVAL_MS:
                    due = now
                    self.sample()
                if time.ticks_diff(now, flush_due) >= FLUSH_INTERVAL_MS:
                    flush_due = now
                    self.flush()
            await asyncio.sleep_ms(50)
        self.flush()


recorder = FieldRecorder()
