# SPDX-License-Identifier: AGPL-3.0-only
"""Makes main.py importable under CPython. main.py runs on MicroPython and imports modules that do not exist under CPython (machine, network, bluetooth, ubinascii, ujson, uasyncio). This file stores in sys.modules for stubs and imports main.py. The stubs are deliberately thin: the pure functions are tested (NMEA parsing, HTTP parsing, configuration casting, RTCM framing). Hardware-related classes are only replicated so that the import goes through. No pytest, no external packets - the sandbox has no pip, and the repo should remain dependency-free. Run with: python3 -m unittest discover -s tests
"""
import os
import sys
import types
import tempfile
import time as _real_time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --- MicroPython-extensions am echten time-Modul --------------------------
# main.py uses time.ticks_ms()/ticks_diff(). This is not available under CPython; to get
# it here is cheaper than a complete time-stub module, which the tester himself would
# use.
if not hasattr(_real_time, "ticks_ms"):
    _real_time.ticks_ms = lambda: int(_real_time.monotonic() * 1000)
    _real_time.ticks_diff = lambda a, b: a - b
    _real_time.sleep_ms = lambda ms: _real_time.sleep(ms / 1000)


# --- MicroPython-extensions am echten gc-Modul ---------------------------
import gc as _real_gc

if not hasattr(_real_gc, "mem_free"):
    _real_gc.mem_free = lambda: 8_300_000
    _real_gc.mem_alloc = lambda: 40_000
    _real_gc.threshold = lambda *a: None


def _module(name, **attrs):
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod
    return mod


# --- ubinascii / ujson ------------------------------------------------------
import binascii as _binascii
import json as _json

_module("ubinascii", b2a_base64=_binascii.b2a_base64,
        a2b_base64=_binascii.a2b_base64, hexlify=_binascii.hexlify,
        unhexlify=_binascii.unhexlify)
_module("ujson", dumps=_json.dumps, loads=_json.loads,
        dump=_json.dump, load=_json.load)


# --- machine ----------------------------------------------------------------
class FakeUART:
    def __init__(self, *a, **kw):
        self.written = bytearray()
        self.rx = bytearray()

    def any(self):
        return len(self.rx)

    def read(self, n=None):
        data = bytes(self.rx[:n]) if n else bytes(self.rx)
        del self.rx[:len(data)]
        return data or None

    def write(self, data):
        self.written.extend(data)
        return len(data)

    def deinit(self):
        pass


class FakePin:
    OUT = 1
    IN = 0

    def __init__(self, *a, **kw):
        self._v = 0

    def value(self, v=None):
        if v is None:
            return self._v
        self._v = int(bool(v))

    def on(self):
        self._v = 1

    def off(self):
        self._v = 0


class FakeRTC:
    """RTC memory: survives any reset except power failure on the device."""

    _memory = b""

    def memory(self, data=None):
        if data is None:
            return FakeRTC._memory
        if len(data) > 2048:
            raise ValueError("buffer too long")
        FakeRTC._memory = bytes(data)


class FakeWDT:
    def __init__(self, *a, **kw):
        self.feeds = 0

    def feed(self):
        self.feeds += 1


_module("machine", UART=FakeUART, Pin=FakePin, WDT=FakeWDT, RTC=FakeRTC,
        reset=lambda: None, reset_cause=lambda: 1, freq=lambda: 160000000,
        PWRON_RESET=1, HARD_RESET=2, WDT_RESET=3, DEEPSLEEP_RESET=4,
        SOFT_RESET=5)


# --- network ----------------------------------------------------------------
class FakeWLAN:
    def __init__(self, iface=0):
        self.iface = iface
        self._active = False
        self._connected = False
        self.cfg = {}

    def active(self, value=None):
        if value is None:
            return self._active
        self._active = bool(value)
        return self._active

    def isconnected(self):
        return self._connected

    def connect(self, *a):
        pass

    def disconnect(self):
        pass

    def config(self, *a, **kw):
        if a and a[0] == "mac":
            return b"\xa1\xb2\xc3\xd4\xe5\xf6"
        self.cfg.update(kw)

    def ifconfig(self, cfg=None):
        if cfg is None:
            return ("192.168.4.1", "255.255.255.0", "192.168.4.1", "192.168.4.1")
        return cfg


_module("network", WLAN=FakeWLAN, STA_IF=0, AP_IF=1,
        AUTH_WPA_WPA2_PSK=3, hostname=lambda *a: "esp-rtk")


# --- bluetooth --------------------------------------------------------------
class FakeUUID:
    def __init__(self, value):
        self.value = value

    def __bytes__(self):
        return b"\x00" * 16


class FakeBLE:
    def __init__(self):
        self._active = False
        self.notified = []
        self.configured = {}
        self.advertisements = []
        self.disconnections = []
        self.pairings = []
        self.passkeys = []
        self.values = {}

    def active(self, value=None):
        if value is None:
            return self._active
        self._active = bool(value)
        return self._active

    def config(self, **kw):
        self.configured.update(kw)

    def irq(self, handler):
        self.handler = handler

    def gatts_register_services(self, services):
        return ((1, 2),)

    def gatts_notify(self, conn, handle, data):
        self.notified.append((conn, bytes(data)))

    def gap_advertise(self, *a, **kw):
        self.advertisements.append((a, kw))

    def gap_disconnect(self, conn):
        self.disconnections.append(conn)

    def gap_pair(self, conn):
        self.pairings.append(conn)

    def gap_passkey(self, conn, action, passkey):
        self.passkeys.append((conn, action, passkey))

    def gatts_read(self, handle):
        return self.values.get(handle, b"")


_module("bluetooth", BLE=FakeBLE, UUID=FakeUUID,
        FLAG_NOTIFY=0x10, FLAG_WRITE=0x08, FLAG_WRITE_NO_RESPONSE=0x04)


# --- esp / esp32 ------------------------------------------------------------
_module("esp", flash_size=lambda: 16777216)
class FakeNVS:
    namespaces = {}

    def __init__(self, namespace):
        self.values = self.namespaces.setdefault(namespace, {})

    def get_blob(self, key, buffer):
        if key not in self.values:
            raise OSError(2)
        value = self.values[key]
        buffer[:len(value)] = value
        return len(value)

    def set_blob(self, key, value):
        self.values[key] = bytes(value)

    def commit(self):
        pass


_module("esp32", mcu_temperature=lambda: 46, HEAP_DATA=0, HEAP_EXEC=1, NVS=FakeNVS,
        idf_heap_info=lambda kind: [(253368, 168984, 163840, 28),
                                    (8388608, 8320372, 8257536, 8320372)])


# --- micropython ------------------------------------------------------------
_module("micropython", alloc_emergency_exception_buf=lambda n: None,
        const=lambda x: x)


# --- uasyncio ---------------------------------------------------------------
import asyncio as _asyncio

_uasyncio = _module("uasyncio")
for _name in dir(_asyncio):
    if not _name.startswith("_"):
        setattr(_uasyncio, _name, getattr(_asyncio, _name))
_uasyncio.sleep_ms = lambda ms: _asyncio.sleep(ms / 1000)


# --- sys.print_exception ----------------------------------------------------
if not hasattr(sys, "print_exception"):
    import traceback

    sys.print_exception = lambda exc, *a: traceback.print_exception(
        type(exc), exc, exc.__traceback__)


# --- Deterministic volume ---------------------------------------------------
# Unit tests must not depend on the runner's current disk usage: tracking and
# field_diagnostics refuse writes below the 20 % flash reserve, and CI runners
# are often fuller than that. The production reserve calculation stays
# exercised against a 4 KiB-block volume well above the reserve; tests that
# need a full or failing volume patch os.statvfs themselves.
_real_statvfs = os.statvfs


def _roomy_statvfs(path):
    _real_statvfs(path)  # a missing path must still raise OSError
    return os.statvfs_result((4096, 4096, 1000000, 900000, 900000, 0, 0, 0, 0, 255))


os.statvfs = _roomy_statvfs


# --- main.py importieren ----------------------------------------------------
# From an empty directory, so that load_config() does not read a real config.json of the
# developer when imported and rotates the tests.
if REPO not in sys.path:
    sys.path.insert(0, REPO)

# Order no matter - the modules import each other themselves in the correct sequence
# (cfg <- state <- {net,web,ble,gnss} <- main).
_cwd = os.getcwd()
os.chdir(tempfile.mkdtemp(prefix="esp-rtk-test-"))
try:
    import cfg
    import identity
    import access
    import state
    import net
    import web
    import ble
    import gnss
    import fanout
    import ui
    import discovery
    import main
finally:
    os.chdir(_cwd)


def nmea_checksum(body):
    """Independent reference implementation for test fixtures. Deliberately not imported from main.py - otherwise an error in the implementation would be mirrored by the fixtures and never fails.
    """
    value = 0
    for char in body:
        value ^= ord(char)
    return "%02X" % value


def sentence(body):
    return "$%s*%s" % (body, nmea_checksum(body))
