# SPDX-License-Identifier: AGPL-3.0-only
# frozen reference of net.RtcmCounter at 08c5928 for #38b, never edited
"""Verbatim copy of net.RtcmCounter (net.py:798-991 at 08c5928). Module globals
(time, app, CONFIG, _clock_valid, _utc_sow_ms, _MSM_SOURCES, WEEK_MS, DAY_MS,
msm_familie) are read through the net module at run time, so a patch on net
reaches both this reference and the production class."""
from support import net


class RtcmCounter:
    """Counts RTCM3 message types in the continuous stream and decodes the GNSS epoch of MSM messages to report the real age of the corrections. Works as a state automaton and buffers at most eight bytes: the correction data continues unchanged and without delay to the module, here only the frame format is read. Frame format: D3 | 6 bits reserved + 10 bits length | payload | 3 bytes CRC24Q. The message number is the first 12 bits of the payload; an MSM header continues with a 12-bit station id and a 30-bit epoch (payload bytes 3..6). The CRC is deliberately NOT checked (the diagnostics probe does): frame synchronisation over the length field is enough for a statistic, and epochs failing a plausibility check are ignored. Once in the clock, the automaton stays in the clock; a 0xD3 in the payload can start a false frame, which washes out after a few frames. Everything is O(1) per byte and per summary().

    Epoch age: the device clock (UTC, 1 s resolution, valid only 2024..2099) is converted to the epoch's time scale (GPS/Galileo/QZSS/SBAS/IRNSS time of week = UTC + leap seconds, BeiDou = GPS - 14 s, GLONASS = Moscow day of week + ms of day), the age is the difference modulo a week (a day when GLONASS reports day 7), future epochs clamp to 0. The age is stored with the tick of its decode and grows with ticks_ms() afterwards. A clock step (NTP, or the RMC re-sync in gnss.py) between two valid times drops the cached age at once and the next MSM frame restores it, i.e. within about a second. The epoch cache holds the most recent MSM of any constellation.
    """

    SEARCH, LENGTH, NUMBER, REST, EPOCH = 0, 1, 2, 3, 4

    def __init__(self):
        self.counts = {}
        self.bytes_total = 0
        self.last_msg_ms = None
        # Uptime of the last recognised message number as two small ints: whole
        # seconds of app's accumulator and the ms since them (see last_msg_uptime_ms).
        self._msg_uptime_s = None
        self._msg_uptime_ms = 0
        self.last_epoch_age_ms = None
        self.last_epoch_ticks_ms = None
        self.last_epoch_source = None
        self._epoch_clock_steps = 0
        self._state = self.SEARCH
        self._buf = bytearray()
        self._payload_len = 0
        self._rest = 0

    def reset_stream(self):
        """Call on a new connection: the parser restarts at a frame boundary and the
        epoch cache is dropped. Counts, bytes_total, last_msg_ms and the message uptime
        are kept so that $PESPS and the message statistics keep their meaning."""
        self._state = self.SEARCH
        self._buf = bytearray()
        self._payload_len = 0
        self._rest = 0
        self.last_epoch_age_ms = None
        self.last_epoch_ticks_ms = None
        self.last_epoch_source = None

    def feed(self, data):
        self.bytes_total += len(data)
        for b in data:
            self._schritt(b)

    def _schritt(self, b):
        if self._state == self.SEARCH:
            if b == 0xD3:
                self._buf = bytearray()
                self._state = self.LENGTH
            return

        if self._state == self.LENGTH:
            self._buf.append(b)
            if len(self._buf) == 2:
                self._payload_len = ((self._buf[0] & 0x03) << 8) | self._buf[1]
                if self._payload_len < 2:
                    # Too short for a message number - discard.
                    self._state = self.SEARCH
                else:
                    self._buf = bytearray()
                    self._state = self.NUMBER
            return

        if self._state == self.NUMBER:
            self._buf.append(b)
            if len(self._buf) == 2:
                number = (self._buf[0] << 4) | (self._buf[1] >> 4)
                self.counts[number] = self.counts.get(number, 0) + 1
                now = net.time.ticks_ms()
                self.last_msg_ms = now
                # Read, never advance, app's uptime accumulator: small-int arithmetic
                # only, the bigint uptime is formed when a report asks for it.
                self._msg_uptime_s = net.app._uptime_s
                self._msg_uptime_ms = (net.app._uptime_rem_ms
                                       + max(0, net.time.ticks_diff(now, net.app._uptime_at)))
                if (1071 <= number <= 1137 and 1 <= number % 10 <= 7
                        and self._payload_len >= 7):
                    # MSM header: keep collecting up to the end of the epoch field.
                    self._state = self.EPOCH
                    return
                # Skip the remaining payload plus the three CRC bytes.
                self._rest = self._payload_len - 2 + 3
                self._buf = bytearray()
                self._state = self.REST if self._rest else self.SEARCH
            return

        if self._state == self.EPOCH:
            self._buf.append(b)
            if len(self._buf) == 7:
                number = (self._buf[0] << 4) | (self._buf[1] >> 4)
                self._decode_epoch(number, self._buf)
                self._rest = self._payload_len - 7 + 3
                self._buf = bytearray()
                self._state = self.REST
            return

        # REST
        self._rest -= 1
        if self._rest <= 0:
            self._state = self.SEARCH

    def _decode_epoch(self, number, buf):
        """Store the age of the MSM epoch in buf[3:7]; ignore implausible values."""
        epoch = (buf[3] << 22) | (buf[4] << 14) | (buf[5] << 6) | (buf[6] >> 2)
        tm = net.time.gmtime()
        if not net._clock_valid(tm):
            return
        source = net._MSM_SOURCES[(number - 1070) // 10]
        sow = net._utc_sow_ms(tm)
        if source == "GLO":
            day = epoch >> 27
            tod = epoch & 0x7FFFFFF
            if day > 7 or tod >= net.DAY_MS:
                return
            moscow = sow + 3 * 3600 * 1000
            if day == 7:
                modulus, device, value = net.DAY_MS, moscow % net.DAY_MS, tod
            else:
                modulus, device, value = net.WEEK_MS, moscow % net.WEEK_MS, day * net.DAY_MS + tod
        else:
            if epoch >= net.WEEK_MS:
                return
            offset = net.CONFIG["gps_utc_offset_sec"]
            if source == "BDS":
                offset -= 14
            modulus, device, value = net.WEEK_MS, (sow + offset * 1000) % net.WEEK_MS, epoch
        age = (device - value) % modulus
        if age > modulus // 2:
            age = 0     # epoch ahead of the 1 s clock: treat as fresh
        self.last_epoch_age_ms = age
        self.last_epoch_ticks_ms = net.time.ticks_ms()
        self.last_epoch_source = source
        self._epoch_clock_steps = net.app.clock_steps

    def epoch_age_ms(self, now_ms=None):
        """Age of the newest MSM epoch in ms, or None before the first plausible
        epoch or while the device clock is invalid."""
        if self.last_epoch_age_ms is None or not net._clock_valid():
            return None
        if self._epoch_clock_steps != net.app.clock_steps:
            # The RTC was stepped after this age was derived from it: the next MSM
            # frame (about a second) restores a correct value.
            return None
        if now_ms is None:
            now_ms = net.time.ticks_ms()
        elapsed = net.time.ticks_diff(now_ms, self.last_epoch_ticks_ms)
        if elapsed < 0:
            return None     # ticks wrapped (~6.2 days): the cached epoch has expired
        return self.last_epoch_age_ms + elapsed

    @property
    def last_msg_uptime_ms(self):
        """app.uptime_ms() when the last message number was recognised, None before."""
        if self._msg_uptime_s is None:
            return None
        return self._msg_uptime_s * 1000 + self._msg_uptime_ms

    def silence_ms(self):
        """ms since the last recognised message number (the moment arrival_age_sec
        counts from), on the monotonic uptime: no alias after a tick wrap. None before
        the first message."""
        stamp = self.last_msg_uptime_ms
        if stamp is None:
            return None
        return max(0, net.app.uptime_ms() - stamp)

    @staticmethod
    def age_is_stale(age_ms):
        """The one stale rule: an epoch older than ntrip_max_epoch_age_sec; no age is not stale."""
        return age_ms is not None and age_ms > net.CONFIG["ntrip_max_epoch_age_sec"] * 1000

    def epoch_stale(self, now_ms=None):
        return self.age_is_stale(self.epoch_age_ms(now_ms))

    def summary(self):
        """Report for /status and /rtcm."""
        familien = []
        for number in self.counts:
            fam = net.msm_familie(number)
            if fam and fam not in familien:
                familien.append(fam)
        age = None
        if self.last_msg_ms is not None:
            age = round(net.time.ticks_diff(net.time.ticks_ms(), self.last_msg_ms) / 1000, 1)
        epoch_age = self.epoch_age_ms()
        return {
            "messages": dict((str(k), v) for k, v in self.counts.items()),
            "msm": sorted(familien),
            "bytes_total": self.bytes_total,
            "last_msg_age_sec": age,
            "arrival_age_sec": age,
            "epoch_age_sec": None if epoch_age is None else round(epoch_age / 1000, 1),
            "epoch_source": None if epoch_age is None else self.last_epoch_source,
            "stale": self.age_is_stale(epoch_age),
        }
