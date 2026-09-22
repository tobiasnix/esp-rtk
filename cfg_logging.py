# SPDX-License-Identifier: AGPL-3.0-only
"""RTC flight recorder and wear-limited persistent error log."""
import os
import sys
import time


class Logger:
    def __init__(self, config, severities, error_file="errors.log",
                 error_max=8192, rtc_max=1900):
        self.config, self.severities = config, severities
        self.error_file, self.error_max, self.rtc_max = error_file, error_max, rtc_max
        self.rtc = None
        self.lines, self.start, self.previous = [], 0, ""
        self.saved, self.last, self.repeated = False, None, 0
        self.entry_count = None
        self.generation = 0

    def reset(self):
        self.lines, self.previous, self.saved = [], "", False
        try:
            import machine
            self.rtc = machine.RTC(); self.rtc.memory()
        except Exception: self.rtc = None
        self.adopt()

    def adopt(self):
        self.saved = False
        if self.rtc is None:
            self.lines, self.start, self.previous = [], 0, ""; return
        try: self.previous = self.rtc.memory().decode("utf-8", "ignore")
        except Exception: self.previous = ""
        self.lines = self.previous.split("\n") + ["----- Restart -----"] if self.previous else []
        self.start = len(self.lines)

    def append(self, line):
        if self.rtc is None: return
        self.lines.append(line); data = "\n".join(self.lines)
        while len(data) > self.rtc_max and len(self.lines) > 1:
            self.lines.pop(0); self.start = max(0, self.start - 1); data = "\n".join(self.lines)
        try: self.rtc.memory(data.encode())
        except Exception: pass

    def append_file(self, line):
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
        except Exception: pass

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

    def flush_repeated(self):
        if self.repeated:
            # Kept for compatibility with existing field logs and parsers.
            self.append_file("[%9.1f] ----- previous message %dx wiederholt" % (time.ticks_ms()/1000, self.repeated))
            self.repeated = 0

    def error(self, line, key=None, always=False):
        if always or key is None:
            self.flush_repeated(); self.last = None; self.append_file(line); return
        if key == self.last: self.repeated += 1; return
        self.flush_repeated(); self.last = key; self.append_file(line)

    def log(self, sev, tag, message, print_traceback=False, exception=None):
        rank = self.severities.get(sev, 1)
        if rank < self.severities.get(self.config.get("log_level", "INFO"), 1): return
        muted = tuple(x.strip().upper() for x in self.config.get("log_mute", "").split(",") if x.strip())
        if tag in muted: return
        line = "[%9.1f] %-5s %-5s %s" % (time.ticks_ms()/1000, sev, tag, message)
        print(line); self.append(line)
        if print_traceback and exception: sys.print_exception(exception)
        if rank >= self.severities["WARN"]: self.error(line, "%s|%s|%s" % (sev, tag, message))

    def log_boot(self, text):
        line = "[%9.1f] ----- %s" % (time.ticks_ms()/1000, text)
        self.error(line, always=True); self.append(line)

    def read_error(self):
        self.flush_repeated(); parts = []
        for name in (self.error_file + ".1", self.error_file):
            try:
                with open(name) as fh: parts.append(fh.read())
            except OSError: pass
        return "".join(parts)

    def read_entries(self, cursor=None, limit=200, level=None):
        """Return a bounded incremental view of the persistent log."""
        self.flush_repeated()
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
                parts = rest.strip().split(None, 2)
                if len(parts) >= 2 and parts[0] in self.severities:
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

    def save_previous(self):
        if self.saved or not self.previous: return
        self.saved = True; self.append_file("--- History before the last restart ---")
        for line in self.previous.split("\n"):
            if line: self.append_file(line)
        self.append_file("--- End of history ---")

    def start_new(self): self.lines, self.start = [], 0
    def reset_error_state(self):
        self.last, self.repeated, self.entry_count = None, 0, None
    def clear_rtc(self):
        if self.rtc:
            try: self.rtc.memory(b"")
            except Exception: pass
