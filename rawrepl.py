#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Common Raw-REPL layer for the board tools. Up to now, the same code was in the repository four times - in pull.py, push.py, mpexec.py and nmea.py. A correction had to be made four times each time; that's exactly what the commit was to "Secure tools against the watchdog". Two quirks of this environment are built in here: * No tcflush/reset_input_buffer - this throws it on this USB-CDC in the EIO container. The input is instead blanked. * The soft reboot dance when entering the Raw-REPL. It stops the running application and jumps over boot.py/main.py, so that the board remains operable even when it is hanging in a reboot loop.
"""
import os
import time

import serial

from espport import resolve_port


class RawRepl:
    """Connection to the MicroPython board in the Raw-REPL."""

    def __init__(self, port=None, baud=115200, timeout=1):
        self.port = resolve_port(port)
        self.baud = baud
        self.timeout = timeout
        self.ser = None

    # --- Connection ---------------------------------------------------------

    def open(self):
        self.ser = serial.Serial(self.port, self.baud, timeout=self.timeout)
        return self

    def connect_raw(self):
        """Opening, entering Raw-REPL, deactivating watchdog - on a train. If you only call open() sits in the normal REPL: ex() does not get OK there and fails silently if the caller removes the exception.
        """
        self.open()
        self.enter_raw()
        self.defuse_wdt()
        return self

    def close(self):
        try:
            if self.ser:
                self.ser.close()
        except Exception:
            pass
        self.ser = None

    def leave_raw(self):
        """Leave Raw REPL (Ctrl-B), board continues in normal REPL."""
        try:
            self.ser.write(b"\x02")
        except Exception:
            pass

    # --- Grundoperationen ---------------------------------------------------

    def drain(self):
        """Entrance empty WITHOUT tcflush."""
        previous_timeout = self.ser.timeout
        self.ser.timeout = 0.2
        while self.ser.read(256):
            pass
        self.ser.timeout = previous_timeout

    def read_until(self, expected, tot=15):
        buf = b""
        ende = time.time() + tot
        while not buf.endswith(expected):
            c = self.ser.read(1)
            if c:
                buf += c
            elif time.time() > ende:
                raise RuntimeError("Timeout; expected %r, last received %r"
                                   % (expected, buf[-80:]))
        return buf

    def ex(self, cmd, timeout=15):
        """execute code and return the output. timeout applies to the waiting time for the end of the output. The default value is sufficient for file operations; longer running code (series of measurements, recordings) needs more.
        """
        self.ser.write(cmd.encode() + b"\x04")
        ack = self.ser.read(2)
        if ack != b"OK":
            raise RuntimeError("expected OK, received %r" % ack)
        out = self.read_until(b"\x04", timeout)[:-1]
        err = self.read_until(b"\x04", timeout)[:-1]
        self.read_until(b">", timeout)
        if err:
            raise RuntimeError(err.decode("utf-8", "replace"))
        return out

    def send_nowait(self, cmd):
        """Send code without waiting for output. Needy for everything that leaves the board: machine.reset(), machine.bootloader().
        """
        self.ser.write(cmd.encode() + b"\x04")

    # --- Einstieg -----------------------------------------------------------

    def enter_raw(self):
        """The soft reboot stops the application and jumps over main.py.
        """
        self.ser.write(b"\r\x03\x03")
        time.sleep(0.2)
        self.drain()
        self.ser.write(b"\r\x01")
        self.read_until(b"raw REPL; CTRL-B to exit\r\n>")
        try:
            self.ser.write(b"\x04")
            self.read_until(b"soft reboot\r\n", 6)
            self.read_until(b"raw REPL; CTRL-B to exit\r\n>", 6)
        except RuntimeError:
            # If CDC briefly disappears, enter Raw REPL again.
            self.drain()
            self.ser.write(b"\r\x03\x03\r\x01")
            self.read_until(b"raw REPL; CTRL-B to exit\r\n>")

    def defuse_wdt(self, timeout_ms=90000):
        """Boost the watchdog timeout. The application activates a 15-s hardware WDT. The application survives the soft reboot into the Raw-REPL - without this intervention, so the board resets in the middle of longer actions (transmit file, read NMEA). Switching off on the ESP32 is not, only delay. Side effect: no application runs after the set deadline by itself - and thus starts the board again in main.py. Until then, the device is completely dead: no Wi-Fi, no BLE, no AP. Therefore, 90 seconds and no more - the longest measured operation (pull.py) takes 16 seconds. And therefore the tools set the board back on its own at the end, instead of waiting the deadline. Exactly this reset logs on as WDT and so for the application, the next reset is marked as planned. The marking applies exactly once - the subsequent start consumes it.
        """
        try:
            self.ex("from machine import WDT\nWDT(0, %d)\n" % timeout_ms)
        except Exception:
            pass
        self.mark_planned_reset()

    def mark_planned_reset(self):
        """Put Planned.reset on the board."""
        try:
            self.ex("f=open('planned.reset','w')\nf.write('1')\nf.close()")
        except Exception:
            pass

    def reset_board(self):
        """Hard restart so that the application starts again immediately. Without this, the board stops after a transmission in the REPL until the upset watchdog closes - i.e. up to five minutes. Planned.reset() is set beforehand: machine.reset() logs on the ESP32-S3 as HARD and is therefore indistinguishable from a panic reset. Without the marking, the application would count any developer reset as a false start and go into safe mode after five transmissions.
        """
        self.mark_planned_reset()
        try:
            self.send_nowait("import machine\nmachine.reset()")
            time.sleep(0.5)
        except Exception:
            pass


def with_retry(operation, port=None, versuche=5, pause=1.5, name="Aktion",
               restart=True, wdt_timeout_ms=90000):
    """Runs work(repl) and repeats when connection is disconnected. The native USB-CDC disappears briefly when the board is rebooted; without repetition any longer transmission fails. work gets an open RawRepl in the Raw-REPL with deactivated watchdog and returns any value. restart=True restarts the board afterwards. This is more important than it sounds: the Raw-REPL holds the application, and until the deactivated watchdog closes, the device is NOT reachable - no Wi-Fi, no BLE, no configuration AP. If you do not want to do this (for example, to examine the state in the REPL), restart=false and then have to reset itself.
    """
    repl = None
    for versuch in range(1, versuche + 1):
        try:
            repl = RawRepl(port).open()
            repl.enter_raw()
            repl.defuse_wdt(wdt_timeout_ms)
            ergebnis = operation(repl)
            if restart:
                repl.reset_board()
            else:
                repl.leave_raw()
            repl.close()
            return ergebnis
        except (RuntimeError, OSError, serial.SerialException) as e:
            print("%s: attempt %d/%d failed: %s"
                  % (name, versuch, versuche, e))
            if repl:
                repl.close()
            if versuch == versuche:
                raise
            time.sleep(pause)
            # Waiting for the device node to return.
            for _ in range(40):
                if os.path.exists(RawRepl(port).port):
                    break
                time.sleep(0.5)
