# SPDX-License-Identifier: AGPL-3.0-only
"""RTC flight recorder and wear-limited persistent error log."""
import os
import sys
import time

# Token bucket per "<SEV> <TAG>" for persistent appends: a burst of six lines,
# then one more line every ten seconds.
LOG_RATE_BURST = 6
LOG_RATE_REFILL_MS = 10000
_SUMMARY_TAIL = ["lines", "suppressed", "(rate", "limit)"]

# Hot-path log sites (send failures, rejected requests, ...) report a condition
# once, then at most once per interval with a count of what was held back.
HOT_PATH_LOG_INTERVAL_MS = 60000


def throttle_note(n):
    """Suffix for a throttled report; n is the value LogThrottle.hit returned."""
    return "" if n <= 1 else " [%d more since the last report]" % (n - 1)


class LogThrottle:
    """Report each key once, then once per interval, counting what was held back.

    Entries are [last_report_ms, pending]. At most max_keys keys are tracked;
    further keys share one overflow entry. An entry with pending events is
    never dropped, so no count is lost.
    """

    def __init__(self, interval_ms, max_keys=8, clock=None):
        self.interval_ms, self.max_keys = interval_ms, max_keys
        self.clock = clock or time.ticks_ms
        self.entries, self.overflow = {}, None

    def _due(self, entry, now):
        # A negative difference (ticks_ms wrapped) counts as long ago.
        diff = time.ticks_diff(now, entry[0])
        return diff < 0 or diff >= self.interval_ms

    def _count(self, entry, now):
        if self._due(entry, now):
            n = entry[1] + 1; entry[0], entry[1] = now, 0
            return n
        entry[1] += 1
        return 0

    def hit(self, key=None):
        """0 = stay silent, n >= 1 = report; n counts events since the last report."""
        now = self.clock(); entry = self.entries.get(key)
        if entry is None:
            if len(self.entries) >= self.max_keys:
                for old in [k for k, e in self.entries.items()
                            if e[1] == 0 and self._due(e, now)]:
                    del self.entries[old]
            if len(self.entries) < self.max_keys:
                self.entries[key] = [now, 0]
                return 1
            if self.overflow is None:
                self.overflow = [now, 0]
                return 1
            entry = self.overflow
        return self._count(entry, now)

    def pending_total(self):
        total = sum(e[1] for e in self.entries.values())
        return total + (self.overflow[1] if self.overflow else 0)

    def reset(self):
        self.entries, self.overflow = {}, None


class Logger:
    def __init__(self, config, severities, error_file="errors.log",
                 error_max=8192, rtc_max=1900, rate_burst=LOG_RATE_BURST,
                 rate_refill_ms=LOG_RATE_REFILL_MS, clock=None):
        self.config, self.severities = config, severities
        self.error_file, self.error_max, self.rtc_max = error_file, error_max, rtc_max
        self.rtc = None
        self.lines, self.start, self.previous = [], 0, ""
        # Mirror of "\n".join(self.lines).encode(), kept across appends.
        self._rtc = bytearray()
        self.saved, self.last, self.repeated = False, None, 0
        self.entry_count = None
        self.generation = 0
        self.rate_burst, self.rate_refill_ms = rate_burst, rate_refill_ms
        self.clock = clock or time.ticks_ms
        # rate_key -> [tokens, stamp_ms, suppressed]. One bucket per severity
        # and tag, so a flood from one source cannot silence another; tags and
        # severities are code literals, so the set of keys stays small. A
        # summary names only severity, tag and count, never the text of a
        # suppressed line (#3: messages may carry secrets). The limit lowers
        # the number of flash appends; it does not shorten a single write.
        self.buckets = {}
        self.last_rate, self.last_kept, self.tail_key = None, True, None

    def reset(self):
        self.lines, self.previous, self.saved = [], "", False
        self._rtc = bytearray()
        try:
            import machine
            self.rtc = machine.RTC(); self.rtc.memory()
        except Exception: self.rtc = None
        self.adopt()

    def adopt(self):
        self.saved = False
        if self.rtc is None:
            self.lines, self.start, self.previous = [], 0, ""
            self._rtc = bytearray(); return
        try: self.previous = self.rtc.memory().decode("utf-8", "ignore")
        except Exception: self.previous = ""
        self.lines = self.previous.split("\n") + ["----- Restart -----"] if self.previous else []
        self.start = len(self.lines)
        self._rtc = bytearray("\n".join(self.lines).encode())

    def append(self, line):
        if self.rtc is None: return
        # RTC.memory() has no offset, so the copy into RTC memory stays; what
        # goes away is the join/encode of up to 1.9 KB per line. Trimming down
        # to three quarters amortises the slice copy over several lines.
        if self.lines: self._rtc.extend(b"\n")
        self._rtc.extend(line.encode()); self.lines.append(line)
        if len(self._rtc) > self.rtc_max:
            target, cut, drop = self.rtc_max * 3 // 4, 0, 0
            while len(self._rtc) - cut > target and len(self.lines) - drop > 1:
                cut += len(self.lines[drop].encode()) + 1; drop += 1
            if drop:
                del self.lines[:drop]; self.start = max(0, self.start - drop)
                self._rtc = self._rtc[cut:]
        try: self.rtc.memory(self._rtc)
        except Exception: pass

    def append_file(self, line):
        self.tail_key = None
        try:
            rotated = False
            try:
                if os.stat(self.error_file)[6] > self.error_max:
                    try: os.remove(self.error_file + ".1")
                    except OSError: pass
                    os.rename(self.error_file, self.error_file + ".1")
                    rotated = True
            except OSError: pass
            with open(self.error_file, "a") as fh: fh.write(line + "\n")
            if rotated:
                self.entry_count = None
                self.generation += 1
            elif self.entry_count is not None:
                self.entry_count += 1
            return True
        except Exception: return False

    def count_entries(self):
        """Count persistent records once without loading either log into RAM."""
        if self.entry_count is not None:
            return self.entry_count
        count = 0
        for name in (self.error_file + ".1", self.error_file):
            try:
                with open(name, "rb") as source:
                    last = b""
                    while True:
                        chunk = source.read(256)
                        if not chunk:
                            break
                        count += chunk.count(b"\n")
                        last = chunk[-1:]
                    if last and last != b"\n":
                        count += 1
            except OSError:
                pass
        self.entry_count = count
        return count

    def _bucket(self, rate_key):
        now = self.clock(); bucket = self.buckets.get(rate_key)
        if bucket is None:
            bucket = self.buckets[rate_key] = [self.rate_burst, now, 0]
            return bucket
        elapsed = time.ticks_diff(now, bucket[1])
        if elapsed < 0:
            # Tick wrap after a long quiet phase counts as "long ago".
            bucket[0], bucket[1] = self.rate_burst, now
        elif elapsed >= self.rate_refill_ms:
            bucket[0] = min(self.rate_burst, bucket[0] + elapsed // self.rate_refill_ms)
            bucket[1] = now
        return bucket

    def _take(self, rate_key):
        bucket = self._bucket(rate_key)
        if bucket[0] > 0:
            bucket[0] -= 1; return True
        return False

    def _write_summary(self, rate_key, bucket):
        if bucket[2] and self.append_file(
                "[%9.1f] ----- %d more %s lines suppressed (rate limit)"
                % (time.ticks_ms() / 1000, bucket[2], rate_key)) is True:
            bucket[2] = 0

    def flush_suppressed(self):
        for rate_key, bucket in self.buckets.items():
            if bucket[2] and self._take(rate_key):
                self._write_summary(rate_key, bucket)

    def flush_repeated(self):
        if not self.repeated: return
        count, self.repeated = self.repeated, 0
        # The repeat line refers to the line right above it, so it is only
        # written while the repeated message is still the last line in the file.
        if self.last_kept and self.tail_key == self.last and self._take(self.last_rate):
            # Kept for compatibility with existing field logs and parsers.
            if self.append_file("[%9.1f] ----- previous message %dx wiederholt" % (time.ticks_ms()/1000, count)) is True:
                self.tail_key = self.last; return
        self._bucket(self.last_rate)[2] += count

    def error(self, line, key=None, always=False, rate_key=None):
        if always or key is None:
            self.flush_repeated(); self.last = None; self.append_file(line); return
        # Only a line that reached the file anchors repeats. A repeat of a
        # refused or failed line asks for admission again, so it is written
        # as soon as the bucket has a token instead of staying a bare count.
        if key == self.last and self.last_kept: self.repeated += 1; return
        self.flush_repeated()
        if rate_key is None: rate_key = key
        self.last, self.last_rate = key, rate_key
        if self._take(rate_key):
            bucket = self.buckets[rate_key]
            self._write_summary(rate_key, bucket)
            if self.append_file(line) is True:
                self.tail_key, self.last_kept = key, True; return
        else:
            bucket = self.buckets[rate_key]
        bucket[2] += 1; self.last_kept = False

    def log(self, sev, tag, message, print_traceback=False, exception=None):
        rank = self.severities.get(sev, 1)
        if rank < self.severities.get(self.config.get("log_level", "INFO"), 1): return
        muted = tuple(x.strip().upper() for x in self.config.get("log_mute", "").split(",") if x.strip())
        if tag in muted: return
        line = "[%9.1f] %-5s %-5s %s" % (time.ticks_ms()/1000, sev, tag, message)
        print(line); self.append(line)
        if print_traceback and exception: sys.print_exception(exception)
        if rank >= self.severities["WARN"]:
            self.error(line, "%s|%s|%s" % (sev, tag, message), rate_key=sev + " " + tag)

    def log_boot(self, text):
        line = "[%9.1f] ----- %s" % (time.ticks_ms()/1000, text)
        self.error(line, always=True); self.append(line)

    def read_error(self):
        self.flush_repeated(); self.flush_suppressed(); parts = []
        for name in (self.error_file + ".1", self.error_file):
            try:
                with open(name) as fh: parts.append(fh.read())
            except OSError: pass
        return "".join(parts)

    def read_entries(self, cursor=None, limit=200, level=None):
        """Return a bounded incremental view of the persistent log."""
        lines = self.read_error().splitlines()
        count = len(lines)
        start = max(0, count - limit)
        if cursor:
            try:
                generation, offset = [int(value) for value in cursor.split(":", 1)]
                if generation == self.generation and 0 <= offset <= count:
                    start = offset
            except (ValueError, AttributeError):
                pass
        selected = []
        for index, line in enumerate(lines[start:start + limit], start):
            elapsed, severity, source, message = None, "", "", line
            if line.startswith("[") and "]" in line:
                stamp, rest = line.split("]", 1)
                try: elapsed = float(stamp[1:].strip())
                except ValueError: pass
                rest = rest.strip(); parts = rest.split(None, 2)
                summary = self._summary_fields(rest)
                if summary:
                    severity, source, message = summary
                elif len(parts) >= 2 and parts[0] in self.severities:
                    severity, source = parts[0], parts[1]
                    message = parts[2] if len(parts) > 2 else ""
            if level and severity != level:
                continue
            selected.append({"seq": index, "elapsed_sec": elapsed,
                             "level": severity, "source": source,
                             "message": message})
        next_offset = min(count, start + limit)
        return {"generation": self.generation,
                "cursor": "%d:%d" % (self.generation, next_offset),
                "entries": selected, "has_more": next_offset < count}

    def _summary_fields(self, rest):
        """(SEV, TAG, text) for a rate-limit summary, else None."""
        if not rest.startswith("----- "): return None
        text = rest[6:]; words = text.split(" ")
        if (len(words) == 8 and words[0].isdigit() and words[1] == "more"
                and words[2] in self.severities and words[4:] == _SUMMARY_TAIL):
            return words[2], words[3], text
        return None

    def save_previous(self):
        if self.saved or not self.previous: return
        self.saved = True; self.append_file("--- History before the last restart ---")
        for line in self.previous.split("\n"):
            if line: self.append_file(line)
        self.append_file("--- End of history ---")

    def start_new(self): self.lines, self.start, self._rtc = [], 0, bytearray()
    def reset_error_state(self):
        self.last, self.repeated, self.entry_count = None, 0, None
        self.buckets, self.last_rate, self.last_kept, self.tail_key = {}, None, True, None
    def clear_rtc(self):
        self.lines, self.start, self._rtc = [], 0, bytearray()
        if self.rtc:
            try: self.rtc.memory(b"")
            except Exception: pass
