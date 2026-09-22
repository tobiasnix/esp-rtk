# SPDX-License-Identifier: AGPL-3.0-only
"""Encrypted and bonded Nordic UART Service peripheral."""
import bluetooth
import os
import time
import ubinascii
import uasyncio as asyncio
import ujson

from cfg import CONFIG, log
from state import app, shutdown_event


_UART_UUID = bluetooth.UUID("6E400001-B5A3-F393-E0A9-E50E24DCCA9E")
_FLAG_READ_ENCRYPTED = 0x0200
_FLAG_WRITE_ENCRYPTED = 0x1000
_WRITE_NO_RESPONSE = getattr(bluetooth, "FLAG_WRITE_NO_RESPONSE", 0)
_UART_TX = (bluetooth.UUID("6E400003-B5A3-F393-E0A9-E50E24DCCA9E"),
            bluetooth.FLAG_NOTIFY | _FLAG_READ_ENCRYPTED)
_UART_RX = (bluetooth.UUID("6E400002-B5A3-F393-E0A9-E50E24DCCA9E"),
            bluetooth.FLAG_WRITE | _WRITE_NO_RESPONSE | _FLAG_WRITE_ENCRYPTED)
_UART_SERVICE = (_UART_UUID, (_UART_TX, _UART_RX))

_IRQ_CENTRAL_CONNECT = 1
_IRQ_CENTRAL_DISCONNECT = 2
_IRQ_GATTS_WRITE = 3
_IRQ_MTU_EXCHANGED = 21
_IRQ_ENCRYPTION_UPDATE = 28
_IRQ_GET_SECRET = 29
_IRQ_SET_SECRET = 30
_IRQ_PASSKEY_ACTION = 31
_PASSKEY_ACTION_DISPLAY = 3
_IO_CAPABILITY_DISPLAY_ONLY = 0
_BOND_FILE = "ble-bonds.json"
_BOND_TEMP_FILE = "ble-bonds.tmp"
_BOND_PREVIOUS_FILE = "ble-bonds.previous"
_NOTIFY_TIMEOUT_MS = 500
_NOTIFY_RETRY_MS = 10


class BLEManager:
    def __init__(self, name=None):
        if name is None:
            name = ((app.identity or {}).get("ble_name") or "RTK-GNSS")
        self.ble = bluetooth.BLE()
        self.connections = set()
        self.encrypted_connections = set()
        self.events = []
        self.secrets = self._load_secrets()
        self.secrets_dirty = False
        self.base_name = name
        self.name = name
        self.command_sink = None
        self.payload_sizes = {}
        self.passkey = int((app.identity or {}).get("ble_pin") or "000000")

        self.ble.irq(self._irq)
        self.ble.config(bond=True, mitm=True, le_secure=True,
                        io=_IO_CAPABILITY_DISPLAY_ONLY)
        self.ble.active(True)
        try:
            self.ble.config(gap_name=name)
        except (ValueError, OSError):
            pass
        try:
            self.ble.config(mtu=CONFIG["ble_mtu_max"])
        except (ValueError, OSError):
            log("INFO", "BLE", "[BLE] MTU configuration unsupported.")
        ((self.tx, self.rx),) = self.ble.gatts_register_services((_UART_SERVICE,))
        try:
            self.ble.gatts_set_buffer(self.rx, 128, True)
        except (AttributeError, ValueError, OSError):
            log("INFO", "BLE", "128-byte append RX buffer unsupported.")
        self._adv_payload, self._scan_response = self._payload()
        self._advertise()

    def _load_secrets(self):
        for path in (_BOND_FILE, _BOND_TEMP_FILE, _BOND_PREVIOUS_FILE):
            secrets = {}
            try:
                with open(path, "r") as source:
                    entries = ujson.load(source)
                for sec_type, key, value in entries:
                    secrets[(int(sec_type), ubinascii.unhexlify(key))] = (
                        ubinascii.unhexlify(value))
                if path != _BOND_FILE:
                    try:
                        os.rename(path, _BOND_FILE)
                    except OSError:
                        pass
                return secrets
            except (OSError, ValueError, TypeError):
                continue
        return {}

    @property
    def payload_size(self):
        """Largest negotiated payload, retained for status API compatibility."""
        largest = CONFIG["ble_mtu_size"]
        for size in self.payload_sizes.values():
            largest = max(largest, size)
        return largest

    def _save_secrets(self):
        if not self.secrets_dirty:
            return
        entries = [[sec_type, ubinascii.hexlify(key).decode(),
                    ubinascii.hexlify(value).decode()]
                   for (sec_type, key), value in self.secrets.items()]
        try:
            with open(_BOND_TEMP_FILE, "w") as target:
                ujson.dump(entries, target)
            try:
                os.remove(_BOND_PREVIOUS_FILE)
            except OSError:
                pass
            try:
                os.rename(_BOND_FILE, _BOND_PREVIOUS_FILE)
            except OSError:
                pass
            os.rename(_BOND_TEMP_FILE, _BOND_FILE)
            try:
                os.remove(_BOND_PREVIOUS_FILE)
            except OSError:
                pass
            self.secrets_dirty = False
        except OSError as error:
            log("ERROR", "BLE", "Could not persist BLE bonds: %s" % error)

    def _payload(self):
        payload = bytearray()

        def append(adt, value):
            payload.append(len(value) + 1)
            payload.append(adt)
            payload.extend(value)

        append(0x01, b"\x06")
        uuid_bytes = bytes(_UART_UUID)
        append(0x03 if len(uuid_bytes) == 2 else 0x07, uuid_bytes)
        response = bytearray()
        encoded_name = self.name.encode()
        response.append(len(encoded_name) + 1)
        response.append(0x09)
        response.extend(encoded_name)
        return payload, response

    def _network_name(self):
        """Return a scan-visible name containing the currently usable web IP."""
        mode = str(app.stats.get("net_mode") or "").upper()
        address = None
        if "STA" in mode:
            candidate = app.stats.get("sta_ip")
            if candidate not in (None, "", "N/A", "Failed"):
                address = candidate
        elif "AP" in mode or app.stats.get("access_state") in (
                "SETUP", "RECOVERY", "APPLYING"):
            candidate = app.stats.get("ap_ip")
            if candidate not in (None, "", "N/A", "Failed"):
                address = candidate
        return self.base_name + (" " + str(address) if address else "")

    def _refresh_network_name(self):
        name = self._network_name()
        if name == self.name:
            return False
        # Legacy scan-response data is limited to 31 bytes. The AD header uses
        # two bytes, leaving 29 ASCII bytes for the complete local name.
        if len(name.encode("ascii")) > 29:
            return False
        self.name = name
        try:
            self.ble.config(gap_name=name)
        except (ValueError, OSError):
            pass
        self._adv_payload, self._scan_response = self._payload()
        self._advertise()
        log("INFO", "BLE", "Advertising web address as %s." % name)
        return True

    def _advertise(self):
        self.ble.gap_advertise(100000, adv_data=self._adv_payload,
                               resp_data=self._scan_response)

    def _irq(self, event, data):
        """Handle secret-store callbacks immediately and defer regular work."""
        try:
            if event == _IRQ_GET_SECRET:
                sec_type, index, key = data
                if key is not None:
                    return self.secrets.get((sec_type, bytes(key)))
                current = 0
                for (stored_type, _key), value in self.secrets.items():
                    if stored_type == sec_type:
                        if current == index:
                            return value
                        current += 1
                return None
            if event == _IRQ_SET_SECRET:
                sec_type, key, value = data
                secret_key = (sec_type, bytes(key))
                if value is None:
                    if secret_key not in self.secrets:
                        return False
                    del self.secrets[secret_key]
                else:
                    self.secrets[secret_key] = bytes(value)
                self.secrets_dirty = True
                return True
            if event == _IRQ_PASSKEY_ACTION:
                connection, action, _passkey = data
                if action == _PASSKEY_ACTION_DISPLAY:
                    self.ble.gap_passkey(connection, action, self.passkey)
                return
            if event == _IRQ_CENTRAL_CONNECT:
                self.connections.add(data[0])
                self.payload_sizes[data[0]] = CONFIG["ble_mtu_size"]
                self.events.append(("connect", data[0]))
            elif event == _IRQ_CENTRAL_DISCONNECT:
                self.connections.discard(data[0])
                self.encrypted_connections.discard(data[0])
                self.payload_sizes.pop(data[0], None)
                self.events.append(("disconnect", data[0]))
                self._advertise()
            elif event == _IRQ_GATTS_WRITE:
                # Copy while still in the IRQ callback. A following write may
                # otherwise replace the characteristic value before the task
                # gets CPU time.
                raw = bytes(self.ble.gatts_read(data[1]))
                self.events.append(("write", (data[0], data[1], raw)))
            elif event == _IRQ_MTU_EXCHANGED:
                connection, mtu = data
                size = max(mtu - 3, CONFIG["ble_mtu_size"])
                self.payload_sizes[connection] = size
                self.events.append(("mtu", (connection, size)))
            elif event == _IRQ_ENCRYPTION_UPDATE:
                connection, encrypted, authenticated, bonded, key_size = data
                if encrypted and authenticated:
                    self.encrypted_connections.add(connection)
                else:
                    self.encrypted_connections.discard(connection)
                self.events.append(("security", (connection, bool(encrypted),
                                                   bool(authenticated), bool(bonded),
                                                   key_size)))
            if len(self.events) > 16:
                del self.events[0]
        except Exception:
            pass

    def drain_events(self):
        events, self.events = self.events, []
        return events

    def handle_write(self, handle, conn=None, raw=None):
        """Forward RX commands only on an encrypted maintenance connection."""
        if handle != self.rx or conn not in self.encrypted_connections:
            return
        if raw is None:
            try:
                raw = bytes(self.ble.gatts_read(self.rx))
            except Exception as error:
                log("WARN", "BLE", "Could not read RX: %s" % error)
                return
        if not raw:
            return
        maintenance = app.stats.get("ble_commands_until", 0) > time.time()
        if (self.command_sink is None or not
                (CONFIG.get("ble_commands_enabled", False) or maintenance)):
            return
        ok, error = self.command_sink(raw)
        if not ok:
            log("WARN", "BLE", "Command rejected: %s" % error)

    def enable_command_maintenance(self, seconds=600):
        seconds = max(1, min(int(seconds), 600))
        app.stats["ble_commands_until"] = time.time() + seconds
        return seconds

    async def _notify_chunk(self, conn, chunk):
        started = time.ticks_ms()
        while conn in self.encrypted_connections:
            try:
                self.ble.gatts_notify(conn, self.tx, chunk)
                app.stats["ble_tx_bytes"] = app.stats.get("ble_tx_bytes", 0) + len(chunk)
                return True
            except OSError as error:
                # NimBLE maps transient errors to positive errno values;
                # negative values are unmapped controller/host errors.
                errno = error.args[0] if error.args and isinstance(error.args[0], int) else None
                app.stats["ble_last_notify_errno"] = errno
                transient = errno in (11, 12, 16)  # EAGAIN, ENOMEM, EBUSY
                if not transient or time.ticks_diff(time.ticks_ms(), started) >= _NOTIFY_TIMEOUT_MS:
                    key = "ble_notify_timeouts" if transient else "ble_notify_errors"
                    app.stats[key] = app.stats.get(key, 0) + 1
                    break
                # Keep this chunk intact until the controller has room again.
                app.stats["ble_notify_retries"] = app.stats.get("ble_notify_retries", 0) + 1
                await asyncio.sleep_ms(_NOTIFY_RETRY_MS)
            except Exception:
                app.stats["ble_notify_errors"] = app.stats.get("ble_notify_errors", 0) + 1
                break
        app.stats["ble_drops"] += 1
        return False

    async def send(self, data):
        """Send NMEA only after authenticated link encryption is active."""
        for conn in list(self.encrypted_connections):
            size = self.payload_sizes.get(conn, CONFIG["ble_mtu_size"])
            for pos in range(0, len(data), size):
                if not await self._notify_chunk(conn, data[pos:pos + size]):
                    break
            # The output worker yields after each sentence. Yielding after
            # every 20-byte chunk multiplies scheduler delays under HTTP/flash
            # load and can overflow the BLE queue despite a healthy radio.

    def _handle_events(self):
        for kind, value in self.drain_events():
            if kind == "connect":
                log("INFO", "BLE", "Client connected (%s); requesting secure pairing." % value)
                try:
                    self.ble.gap_pair(value)
                except Exception as error:
                    log("WARN", "BLE", "Could not start pairing (%s): %s" %
                        (value, error))
            elif kind == "disconnect":
                log("INFO", "BLE", "Client disconnected (%s)." % value)
            elif kind == "mtu":
                log("INFO", "BLE", "MTU negotiated for %s; payload %d Byte."
                    % value)
            elif kind == "write":
                self.handle_write(value[1], value[0], value[2])
            elif kind == "security":
                connection, encrypted, authenticated, bonded, key_size = value
                log("INFO" if encrypted and authenticated else "WARN", "BLE",
                    "Security update (%s): encrypted=%s authenticated=%s bonded=%s key=%s."
                    % (connection, encrypted, authenticated, bonded, key_size))
        self._save_secrets()

    async def event_task(self):
        while not shutdown_event.is_set():
            self._handle_events()
            self._refresh_network_name()
            await asyncio.sleep_ms(50)
