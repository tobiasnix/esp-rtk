# SPDX-License-Identifier: AGPL-3.0-only
"""Wi-Fi (STA + AP) and NTRIP client. The NTRIP client writes the correction data to the same UART object from which the GNSS handler reads - it is submitted to it by main.py.
"""
import network
import time
import uasyncio as asyncio
import ubinascii

from cfg import (CONFIG, backoff_delay, generate_ap_password,
                 commit_pending_config,
                 is_derived_password, is_factory_password, log, configured_networks,
                 pending_config_active, rollback_pending_config, save_config)
from field_diagnostics import recorder as field_recorder
from identity import load_identity
from state import app, shutdown_event


# ---------------------------------------------------------------------------
# NETWORK MANAGER (Dual-Mode STA+AP)
# ---------------------------------------------------------------------------

def order_networks(configured, scan):
    """The order remains the one configured: the main network first, then the diversion networks. The scan is only used to jump over unreachable networks - every futile attempt takes seconds. Earlier, it was sorted by field strength. This sounded reasonable, but made the choice unpredictable: two reachable networks landed the device here, sometimes there - and thus under changing addresses that you had to search again. The form calls the "diversion networks" list; the word promises a ranking, and that's true now. Without scan (or if none of the configured networks appears) everyone is tried - the scan can be wrong, for example, a hidden network.
    """
    if not scan:
        return list(configured)

    visible_names = set()
    for entry in scan:
        try:
            visible_names.add(entry[0].decode("utf-8", "ignore"))
        except Exception:
            continue

    sichtbar = [(s, p) for s, p in configured if s in visible_names]
    return sichtbar or list(configured)


def _zeit_holen():
    """Without this time.time() counts from 1.1.2000 and log lines can not be combined with other recordings. A failure is without consequences - all time measurements in the code are relative (ticks_diff).
    """
    if not CONFIG.get("ntp_sync", True):
        return
    try:
        import ntptime
        ntptime.settime()
        field_recorder.event("ntp", {"result": "synchronized"})
        log("INFO", "WIFI", "Clock synchronized via NTP.")
    except Exception as e:
        field_recorder.event("ntp", {"result": "failed"})
        log("WARN", "WIFI", "NTP failed: %s" % e)


class NetworkManager:
    def __init__(self):
        self.wlan_sta = network.WLAN(network.STA_IF)
        self.wlan_ap = network.WLAN(network.AP_IF)
        identity = load_identity(self.wlan_sta.config("mac"),
                                 CONFIG.get("ap_pass"))
        app.identity = identity
        CONFIG["ap_ssid"] = identity["ap_ssid"]
        CONFIG["hostname"] = identity["hostname"]
        self._ensure_ap_password()
        self.ap_active = False
        self._sta_versuche = 0
        self._last_channel_switch = time.ticks_ms()
        self.scan_requested = False
        self.scan_results = None
        self.scan_error = None
        self.scan_completed_ms = None
        self._last_sta_ssid = ""
        self._last_sta_rssi = None
        self._last_sta_channel = None
        # MicroPython initializes DHCP hostname and native mDNS upon
        # activation/connection, so an interface still active from the previous run
        # would not securely adopt the new stable name.
        self._hostname_setzen()
        try:
            if self.wlan_sta.active():
                self.wlan_sta.active(False)
        except OSError:
            pass

    def _ensure_ap_password(self):
        """Ensure that the AP has a unique, unpredictable password.

        Missing, factory-default, and deprecated MAC-derived passwords are
        replaced with a random value, persisted, and printed during boot.
        """
        try:
            mac = self.wlan_ap.config("mac")
        except Exception:
            mac = b"\x00" * 6

        existing = CONFIG.get("ap_pass")
        if (existing and existing != (app.identity or {}).get("device_code")
                and not is_derived_password(existing, mac)):
            return

        reason = ("was derived from the MAC" if existing
                 else "was not configured")
        CONFIG["ap_pass"] = generate_ap_password()
        save_config()
        log("INFO", "WIFI", "AP password %s - replaced: %s"
            % (reason, CONFIG["ap_pass"]))

    def request_scan(self):
        now = time.ticks_ms()
        if (self.scan_completed_ms is not None and
                time.ticks_diff(now, self.scan_completed_ms) < 30000):
            return "ready", self.scan_results, self.scan_error
        self.scan_requested = True
        return "pending", None, None

    async def _service_requested_scan(self):
        if not self.scan_requested:
            return
        self.scan_requested = False
        raw = await self._scan()
        if raw is None:
            self.scan_results, self.scan_error = None, "scan_failed"
        else:
            result = {}
            for entry in raw:
                try:
                    ssid = entry[0].decode("utf-8", "replace")
                    rssi, authmode = int(entry[3]), int(entry[4])
                except (IndexError, TypeError, ValueError):
                    continue
                if ssid and (ssid not in result or rssi > result[ssid]["rssi"]):
                    result[ssid] = {"ssid": ssid, "rssi": rssi,
                                    "secured": authmode != 0}
            self.scan_results = sorted(result.values(),
                                       key=lambda item: item["rssi"], reverse=True)
            self.scan_error = None
        self.scan_completed_ms = time.ticks_ms()

    def _start_ap(self):
        """Activates AP mode for configuration/monitoring."""
        try:
            if not self.wlan_ap.active():
                self.wlan_ap.active(True)

            self._ensure_ap_password()

            # Switching off modem powersave on the STA interface - stabilizes
            # simultaneous AP operation. (0xa11140 from V9.0 was the CYW43/Pico-W
            # constant and ineffective on the ESP32.)
            pm_none = getattr(network.WLAN, "PM_NONE", None)
            if pm_none is not None:
                try:
                    self.wlan_sta.config(pm=pm_none)
                except (ValueError, OSError):
                    pass

            # authmode is mandatory on the ESP32 - only password set lets the AP OPEN.
            # No channel parameter: with STA+AP, the ESP32 enforces the STA channel
            # anyway.
            auth = getattr(network, "AUTH_WPA_WPA2_PSK", None)
            kwargs = {"password": CONFIG["ap_pass"]}
            if auth is not None:
                kwargs["authmode"] = auth
            try:
                self.wlan_ap.config(ssid=CONFIG["ap_ssid"], **kwargs)
            except (ValueError, TypeError):
                # Fallback for older firmware that only knows 'essid'
                self.wlan_ap.config(essid=CONFIG["ap_ssid"], **kwargs)

            # Custom-Builds can RFC-8910/DHCP Option 114 direkt setzen.
            # Standard MicroPython rejects the key; captive DNS and the sample endpoints
            # then continue to work.
            try:
                self.wlan_ap.config(
                    captive_portal="http://%s/api/captive" % CONFIG["ap_ip"])
            except (ValueError, TypeError, OSError):
                pass

            self.wlan_ap.ifconfig((CONFIG["ap_ip"], "255.255.255.0",
                                   CONFIG["ap_ip"], CONFIG["ap_ip"]))

            ip_ap = self.wlan_ap.ifconfig()[0]
            app.stats["ap_ip"] = ip_ap
            self.ap_active = True
            log("INFO", "WIFI", "Configuration AP started on %s" % ip_ap)

            # As long as the factory password is active, everyone knows it in radio
            # range - it's in the public repository.
            if is_factory_password(CONFIG.get("ap_pass")):
                log("WARN", "WIFI", "AP uses the publicly known factory "
                                    "password. Change it on the configuration page.")

        except Exception as e:
            log("ERROR", "WIFI", "AP startup failed: %s" % e)
            app.log_error("WIFI", "AP startup failed")

    def _stop_ap(self):
        """Setup-Funkteil im Normalbetrieb freigeben."""
        try:
            if self.wlan_ap.active():
                self.wlan_ap.active(False)
            self.ap_active = False
            app.stats["ap_ip"] = "N/A"
        except Exception as e:
            log("WARN", "WIFI", "Could not stop AP: %s" % e)

    async def _connect_sta(self):
        """The ESP32 has a radio part for both interfaces: if the AP starts, STA and AP are necessarily on the same channel. If the AP starts on channel 1 due to lack of STA connection, the station can no longer find a router on another channel - a single failed first attempt locked the device so permanently in AP mode. Therefore, it turns off the AP for the attempt, then it takes over the channel of the station. From the second failed attempt, the AP remains on: the maintenance loop calls this method repeatedly, and a configuration hotspot disappearing every second is gone in the field exactly when it is needed.
        """
        disable_ap = self.wlan_ap.active() and self._may_disable_ap()
        if disable_ap:
            # Visible: During an attempt, the configuration AP is gone, with several
            # networks up to 15 seconds. Without this line, you only see "AP started" in
            # the log and hold the hole for a failure.
            log("INFO", "WIFI", "AP stopped for the connection attempt.")
            self.wlan_ap.active(False)
            await asyncio.sleep_ms(200)
        try:
            return await self._connect_sta_versuch()
        finally:
            if disable_ap:
                self._start_ap()

    def _may_disable_ap(self):
        """On the first attempt, always, then in departures. Two targets stand in the way. If the AP stays, the station can not find a router on another channel because of the common radio channel (trap 5) - the connection setup then fails permanently. If you turn it off for each attempt, the configuration hotspot disappears every second, i.e. exactly when you need it in the field. The compromise: during the fast first attempts, the AP stops, then the channel change is again risked. The interval is deliberately short - one minute waiting for the connection is acceptable, five were not.
        """
        # A form that loses its connection in the middle of sending out is more annoying
        # than a Wi-Fi connection that comes later.
        try:
            if self.wlan_ap.status("stations"):
                return False
        except Exception:
            pass                       # Firmware without station list

        interval = int(CONFIG.get("ap_channel_switch_interval_sec", 60))
        if interval <= 0:
            return False               # AP hat Vorrang, er geht nie weg

        if self._sta_versuche == 0:
            return True
        now = time.ticks_ms()
        interval = interval * 1000
        if time.ticks_diff(now, self._last_channel_switch) > interval:
            self._last_channel_switch = now
            return True
        return False

    def _hostname_setzen(self):
        """Has to happen VOR active(True), otherwise it only applies when the next connection is set up."""
        name = CONFIG.get("hostname", "")
        if not name:
            return
        try:
            network.hostname(name)
        except (AttributeError, OSError, ValueError):
            pass                      # Older firmware does not know this

    def _remember_sta_link(self, ssid=None):
        """Keep the last useful radio values; most ports discard them on loss."""
        if ssid:
            self._last_sta_ssid = ssid
        try:
            self._last_sta_rssi = self.wlan_sta.status("rssi")
        except (AttributeError, OSError, ValueError):
            pass
        try:
            self._last_sta_channel = self.wlan_sta.config("channel")
        except (AttributeError, OSError, ValueError):
            pass

    def _sta_diagnostics(self):
        try:
            status = self.wlan_sta.status()
        except (AttributeError, OSError, ValueError):
            status = "unknown"
        return "status %s, last RSSI %s dBm, channel %s" % (
            status,
            self._last_sta_rssi if self._last_sta_rssi is not None else "unknown",
            self._last_sta_channel if self._last_sta_channel is not None else "unknown")

    def _adopt_restored_connection(self):
        """Reports a self-reestablished STA connection. The ESP32's Wi-Fi driver reconnects automatically after a demolition. Then isconnected() is back to True without this code being involved - and without wifi_connected_event, the NTRIP task always stops even though the network is longest. Observed in the field: after a demolition at 707 seconds, the device continued for three hours without logging a single connection attempt and without correction data - the maintenance loop kept everything in order.
        """
        if app.wifi_connected_event.is_set():
            return False
        try:
            ip = self.wlan_sta.ifconfig()[0]
        except Exception:
            return False
        if not ip or ip == "0.0.0.0":
            return False               # associated, but not yet an address
        app.stats["sta_ip"] = ip
        app.stats["net_mode"] = "STA+AP"
        app.wifi_connected_event.set()
        field_recorder.event("wifi_restored", {"profile": app.stats.get("wifi_profile")})
        log("INFO", "WIFI", "STA connection automatically restored: %s" % ip)
        _zeit_holen()
        return True

    async def _scan(self):
        """Wi-Fi scan, or none if it doesn't go. Takes about three seconds, measured by the device. It's only worth it from two configured networks -- a single one would pay you three seconds to find out what you're trying anyway.
        """
        try:
            networks = self.wlan_sta.scan()
            app.beat("network")
            log("DEBUG", "WIFI", "Scan: %d networks visible" % len(networks))
            return networks
        except Exception as e:
            log("DEBUG", "WIFI", "Scan unavailable: %s" % e)
            return None

    async def _single_attempt(self, ssid, password, seconds):
        """Try a net. True if successful."""
        profile = next(("network-%d" % (index + 1)
                        for index, item in enumerate(configured_networks())
                        if item[0] == ssid), "unconfigured")
        app.stats["wifi_profile"] = profile
        field_recorder.event("wifi_attempt", {"profile": profile, "timeout_sec": seconds})
        try:
            try:
                self.wlan_sta.disconnect()
            except OSError:
                pass

            self._hostname_setzen()

            log("INFO", "WIFI", "Connecting to configured STA network...")
            self.wlan_sta.connect(ssid, password)
        except Exception as e:
            # Typisch: "Wifi Internal Error" nach fehlgeschlagenem Versuch.
            # Re-initialize interface hard and report failure.
            log("ERROR", "WIFI", "Wi-Fi connect() failed: %s" % e)
            app.log_error("WIFI", "connect(): %s" % e)
            field_recorder.event("wifi_result", {"profile": profile, "result": "driver_error"})
            try:
                self.wlan_sta.active(False)
                await asyncio.sleep_ms(500)
                self.wlan_sta.active(True)
            except OSError:
                pass
            return False

        for _ in range(seconds * 5):
            if shutdown_event.is_set():
                return False
            if self.wlan_sta.isconnected():
                ip_sta = self.wlan_sta.ifconfig()[0]
                # Network names are credentials/context and must not enter the
                # persistent or serial logs.
                log("INFO", "WIFI", "STA connected: %s" % ip_sta)
                app.stats["sta_ip"] = ip_sta
                app.stats["wifi_ssid_aktiv"] = ssid
                app.stats["last_wifi_error"] = None
                app.stats["wifi_status_code"] = None
                self._remember_sta_link(ssid)
                field_recorder.event("wifi_result", {"profile": profile, "result": "connected",
                                     "rssi": self._last_sta_rssi, "channel": self._last_sta_channel})
                app.wifi_connected_event.set()
                _zeit_holen()
                return True
            # Signs of life also during long series of experiments - otherwise the
            # watchdog holds the task incorrectly for hanging.
            app.beat("network")
            await asyncio.sleep_ms(200)

        try:
            status_code = self.wlan_sta.status()
        except (AttributeError, OSError, ValueError):
            status_code = None
        error_code = ({201: "access_point_not_found",
                       202: "authentication_failed",
                       203: "association_failed"}.get(status_code,
                                                      "connection_timeout"))
        app.stats["last_wifi_error"] = error_code
        app.stats["wifi_status_code"] = status_code
        field_recorder.event("wifi_result", {"profile": profile, "result": error_code,
                             "status_code": status_code})
        # Keep the persistent message stable so repeated field retries collapse
        # instead of rotating away unrelated tracking failures.
        log("WARN", "WIFI", "STA connection timed out (status %s, %s)." %
            (status_code if status_code is not None else "unknown", error_code))
        return False

    async def _connect_sta_versuch(self):
        """Try visible networks in configured priority order, never by RSSI.

        A connected fallback stays connected until it is lost. For multiple
        candidates each attempt is bounded so a round takes at most one minute.
        """
        networks = configured_networks()
        if not networks:
            return False
        self._sta_versuche += 1

        try:
            if not self.wlan_sta.active():
                self._hostname_setzen()
                self.wlan_sta.active(True)
                await asyncio.sleep_ms(100)

            if self.wlan_sta.isconnected():
                app.stats["sta_ip"] = self.wlan_sta.ifconfig()[0]
                app.wifi_connected_event.set()
                return True
        except OSError as e:
            log("ERROR", "WIFI", "Wi-Fi interface unavailable: %s" % e)
            app.log_error("WIFI", "active(): %s" % e)
            app.stats["sta_ip"] = "Failed"
            app.wifi_connected_event.clear()
            return False

        if len(networks) > 1:
            networks = order_networks(networks, await self._scan())

        # At most four nets per round, otherwise it will take too long.
        networks = networks[:4]
        # Smartphone hotspots may be visible immediately but need several seconds
        # for WPA and DHCP after waking. Six seconds caused avoidable failovers.
        seconds = 15 if len(networks) == 1 else 12

        for ssid, password in networks:
            if shutdown_event.is_set():
                return False
            if await self._single_attempt(ssid, password, seconds):
                return True

        app.stats["sta_ip"] = "Failed"
        app.stats["wifi_ssid_aktiv"] = ""
        app.wifi_connected_event.clear()

        # Putting the driver to rest. wlan.connect() switches on the ESP32 the automatic
        # reconnection: after a failed attempt, the driver in the background tries on
        # endlessly and jumps over the canals. STA, AP and BLE share a radio part - this
        # makes the configuration AP unusable and makes BLE starve to death.
        try:
            self.wlan_sta.disconnect()
        except OSError:
            pass
        return False

    async def run(self):
        log("INFO", "WIFI", "Network manager started.")

        configured = bool(configured_networks())
        force_setup = bool(app.stats.get("force_setup"))
        if not configured or force_setup:
            self._start_ap()
            app.set_access_state("SETUP")
            app.stats["net_mode"] = "AP_SETUP"
            sta_success = False
        else:
            app.set_access_state("CONNECTING")
            sta_success = await self._connect_sta()
            if pending_config_active():
                if sta_success:
                    # A short link flap must not confirm incorrect access data as
                    # functional.
                    for _ in range(10):
                        await asyncio.sleep(1)
                        app.beat("network")
                        if not self.wlan_sta.isconnected():
                            sta_success = False
                            break
                if sta_success:
                    commit_pending_config(app.stats.get("wifi_ssid_aktiv"))
                elif rollback_pending_config():
                    log("WARN", "WIFI", "Trying last known working Wi-Fi network.")
                    sta_success = await self._connect_sta()
            if sta_success:
                self._stop_ap()
                app.set_access_state("ONLINE")
                app.stats["net_mode"] = "STA"
            else:
                self._start_ap()
                app.set_access_state("RECOVERY")
                app.stats["net_mode"] = "AP_RECOVERY"
                log("INFO", "WIFI", "No STA connection; recovery AP active.")
                app.log_error("WIFI", "No STA connection available.")

        # 3. MAINTENANCE LOOP
        fehlversuche = 0 if sta_success else 1
        naechster_versuch = 0
        while not shutdown_event.is_set():
            await asyncio.sleep(5)
            app.beat("network")
            await self._service_requested_scan()
            if shutdown_event.is_set():
                break

            if not configured_networks():
                continue

            if self.wlan_sta.isconnected():
                # Not only accept "all good": the driver can have reconnected itself,
                # and then the NTRIP task is still waiting for the event.
                self._adopt_restored_connection()
                self._remember_sta_link(app.stats.get("wifi_ssid_aktiv"))
                fehlversuche = 0
                naechster_versuch = 0
                if app.stats.get("access_state") != "SETUP":
                    self._stop_ap()
                    app.set_access_state("ONLINE")
                    app.stats["net_mode"] = "STA"
                continue

            if app.wifi_connected_event.is_set():
                log("WARN", "WIFI", "STA connection lost (%s)." %
                    self._sta_diagnostics())
                field_recorder.event("wifi_lost", {"profile": app.stats.get("wifi_profile"),
                                     "rssi": self._last_sta_rssi, "channel": self._last_sta_channel})
                app.wifi_connected_event.clear()
                app.stats["sta_ip"] = "N/A"
                app.stats["last_wifi_error"] = "connection_lost"
                fehlversuche = 0
                # If it fails, it starts within the 30-s window of the recovery AP; an
                # additional waiting time would add both times unnecessarily.
                naechster_versuch = 0
                app.set_access_state("CONNECTING")

            # Backoff statt starrem 5-s-Takt. wifi_reconnects counts bis
            # V9.3 the loop passes, not the attempts.
            if naechster_versuch > 0:
                naechster_versuch -= 5
                continue

            app.stats["wifi_reconnects"] += 1
            sta_success = await self._connect_sta()
            if sta_success:
                fehlversuche = 0
                naechster_versuch = 0
                self._stop_ap()
                app.set_access_state("ONLINE")
                app.stats["net_mode"] = "STA"
            else:
                fehlversuche += 1
                if not self.wlan_ap.active():
                    self._start_ap()
                app.set_access_state("RECOVERY")
                app.stats["net_mode"] = "AP_RECOVERY"
                naechster_versuch = backoff_delay(fehlversuche, 60, 300)
                field_recorder.event("wifi_backoff", {"delay_sec": naechster_versuch})

        log("INFO", "WIFI", "Network manager task stopped.")

# ---------------------------------------------------------------------------
# NTRIP CLIENT
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# RTCM3-STATISTIK
# ---------------------------------------------------------------------------

# MSM families according to RTCM3: one block of 10 numbers per constellation, the offset
# in it determines the family (MSM1..MSM7). 1071..1077 GPS, 1081..1087 GLONASS,
# 1091..1097 Galileo, 1101..1107 SBAS, 1111..1117 QZSS, 1121..1127 BeiDou
def msm_familie(number):
    """'MSM4', 'MSM5', 'MSM7' ... or None, if it is not an MSM message. The reason why this function exists is that Quectel documents for the LC29H MSM4 and MSM7 as input, but the ReNEP network sends MSM5.
    """
    if not 1071 <= number <= 1127:
        return None
    offset = number % 10
    if not 1 <= offset <= 7:
        return None
    return "MSM%d" % offset


class RtcmCounter:
    """If RTCM3 message types are counted in the continuous stream. Works as a state automaton and buffers at most four bytes: the correction data should continue unchanged and without delay to the module, here only the frame format is counted. Frame format: D3 | 6 bits reserved + 10 bits long | payload | 3 bytes CRC24Q The message number is the first 12 bits of the payload. The CRC is deliberately NOT checked. For a statistic, the frame synchronization is sufficient over the length specification; once in the clock, the automaton remains in the clock. A 0xD3 in the useful data can generate a false start, which washes out after a few frames.
    """

    SEARCH, LENGTH, NUMBER, REST = 0, 1, 2, 3

    def __init__(self):
        self.counts = {}
        self.bytes_total = 0
        self.last_msg_ms = None
        self._state = self.SEARCH
        self._buf = bytearray()
        self._payload_len = 0
        self._rest = 0

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
                self.last_msg_ms = time.ticks_ms()
                # Skip the remaining payload plus the three CRC bytes.
                self._rest = self._payload_len - 2 + 3
                self._buf = bytearray()
                self._state = self.REST if self._rest else self.SEARCH
            return

        # REST
        self._rest -= 1
        if self._rest <= 0:
            self._state = self.SEARCH

    def summary(self):
        """Report for /status and /rtcm."""
        familien = []
        for number in self.counts:
            fam = msm_familie(number)
            if fam and fam not in familien:
                familien.append(fam)
        age = None
        if self.last_msg_ms is not None:
            age = round(time.ticks_diff(time.ticks_ms(), self.last_msg_ms) / 1000, 1)
        return {
            "messages": dict((str(k), v) for k, v in self.counts.items()),
            "msm": sorted(familien),
            "bytes_total": self.bytes_total,
            "last_msg_age_sec": age,
        }


class NtripClient:
    def __init__(self, uart_obj):
        self.uart = uart_obj
        # Survives reconnects so that the statistics do not start from the beginning
        # with every connection disconnection.
        self.rtcm = RtcmCounter()
        # Fixed-size diagnostics use the device clock, independently of when a
        # delayed HTTP response reaches the observer. No payload or credentials.
        self._flow = {
            "schema": 1, "connections": 0, "phase": "idle",
            "phase_since_ms": time.ticks_ms(), "read_attempts": 0,
            "read_timeouts": 0, "read_wait_ms": 0, "read_wait_max_ms": 0,
            "rx_chunks": 0, "rx_bytes": 0, "last_rx_ticks_ms": None,
            "rx_gap_ms": 0, "rx_gap_max_ms": 0,
            "uart_writes": 0, "uart_bytes": 0, "uart_short_writes": 0,
            "uart_write_ms": 0, "uart_write_max_ms": 0,
            "gga_writes": 0, "gga_failures": 0,
            "last_gga_ticks_ms": None, "gga_gap_max_ms": 0,
            "gga_write_ms": 0, "gga_write_max_ms": 0,
        }

    def _flow_phase(self, phase):
        self._flow["phase"] = phase
        self._flow["phase_since_ms"] = time.ticks_ms()

    def _flow_duration(self, name, started):
        elapsed = max(0, time.ticks_diff(time.ticks_ms(), started))
        self._flow[name + "_ms"] = elapsed
        key = name + "_max_ms"
        self._flow[key] = max(self._flow[key], elapsed)

    def flow_summary(self):
        """Receive gaps include scheduling delay, not just wire arrival time."""
        now = time.ticks_ms()
        report = dict(self._flow)
        report["observed_ticks_ms"] = now
        report["phase_age_ms"] = time.ticks_diff(now, report["phase_since_ms"])
        last = report["last_rx_ticks_ms"]
        report["rx_age_ms"] = None if last is None else time.ticks_diff(now, last)
        return report

    @staticmethod
    def _configured_endpoints():
        """Return enabled caster configs without exposing them through status/logs."""
        if not CONFIG.get("ntrip_enabled", True):
            return []
        endpoints = []
        for prefix, label in (("ntrip_", "primary"),
                              ("ntrip_fallback_", "fallback")):
            host, mount = CONFIG.get(prefix + "host"), CONFIG.get(prefix + "mount")
            if host and mount:
                endpoints.append({"label": label, "host": host,
                    "port": int(CONFIG.get(prefix + "port", 2101)),
                    "mount": mount, "user": CONFIG.get(prefix + "user", ""),
                    "pass": CONFIG.get(prefix + "pass", "")})
        return endpoints

    @staticmethod
    def _build_request(endpoint=None):
        endpoint = endpoint or {"mount": CONFIG["ntrip_mount"],
            "user": CONFIG["ntrip_user"], "pass": CONFIG["ntrip_pass"]}
        auth = ubinascii.b2a_base64(
            ("%s:%s" % (endpoint["user"], endpoint["pass"])).encode()
        ).strip().decode()
        version = ""
        if int(CONFIG.get("ntrip_version", 1)) >= 2:
            version = "Ntrip-Version: Ntrip/2.0\r\n"
        return (
            "GET /%s HTTP/1.0\r\n"
            "User-Agent: NTRIP ESP32-S3/1.0\r\n"
            "%s"
            "Accept: */*\r\n"
            "Connection: close\r\n"
            "Authorization: Basic %s\r\n"
            "\r\n" % (endpoint["mount"], version, auth)
        ).encode()

    @staticmethod
    def _parse_status_line(line):
        """Return (accepted, error_state, status_code) for the first caster line."""
        text = line.decode("ascii", "ignore").strip() if isinstance(line, bytes) else str(line).strip()
        tokens = text.split()
        if not tokens:
            return False, "protocol_error", None
        if tokens[0].upper() == "SOURCETABLE":
            return False, "mount_error", 200 if len(tokens) > 1 and tokens[1] == "200" else None
        if tokens[0].upper() == "ICY" or tokens[0].upper().startswith("HTTP/"):
            try:
                code = int(tokens[1])
            except (ValueError, IndexError):
                return False, "protocol_error", None
            if code == 200:
                return True, None, code
            if code == 401:
                return False, "auth_error", code
            if code == 404:
                return False, "mount_error", code
            return False, "caster_error", code
        return False, "protocol_error", None

    async def _sende_gga(self, writer):
        """Send the last gullible GGA set to the caster, with fix only: a position without fix (or even 0/0) would mislead the caster and create the wrong virtual station at a VRS mountpoint.
        """
        if not CONFIG.get("ntrip_gga_interval_sec"):
            return False
        sentence = app.last_gga_raw
        if not sentence:
            return False
        self._flow_phase("gga")
        started = time.ticks_ms()
        try:
            writer.write((sentence + "\r\n").encode())
            await writer.drain()
            app.stats["ntrip_gga_sent"] += 1
            now = time.ticks_ms()
            last = self._flow["last_gga_ticks_ms"]
            if last is not None:
                self._flow["gga_gap_max_ms"] = max(
                    self._flow["gga_gap_max_ms"], time.ticks_diff(now, last))
            self._flow["last_gga_ticks_ms"] = now
            self._flow["gga_writes"] += 1
            return True
        except Exception as e:
            self._flow["gga_failures"] += 1
            log("WARN", "NTRIP", "GGA upload failed: %s" % e)
            return False
        finally:
            self._flow_duration("gga_write", started)
            self._flow_phase("streaming")

    async def _gga_due(self, writer):
        """Sends GGA when the interval has expired."""
        intervall = CONFIG.get("ntrip_gga_interval_sec", 0)
        if not intervall:
            return False
        if time.time() - self._last_gga < intervall:
            return False
        self._last_gga = time.time()
        return await self._sende_gga(writer)

    async def run(self):
        log("INFO", "NTRIP", "Client started.")
        self._last_gga = 0
        retry_count = 0
        endpoint_index = 0

        endpoints = self._configured_endpoints()
        if not endpoints:
            app.set_ntrip_state("disabled")
            while not shutdown_event.is_set():
                app.beat("ntrip")
                await asyncio.sleep(5)
            return

        while not shutdown_event.is_set():
            # Configuration changes become effective after the supervised task restart.
            endpoint = endpoints[endpoint_index]

            # Waiting without blocking: without Wi-Fi, this task waits for any length of
            # time, and without signs of life, the watchdog would mistakenly consider it
            # to be haunting.
            app.set_ntrip_state("waiting_wifi")
            self._flow_phase("waiting_wifi")
            while not app.wifi_connected_event.is_set():
                if shutdown_event.is_set():
                    break
                app.beat("ntrip")
                try:
                    await asyncio.wait_for(app.wifi_connected_event.wait(), 5)
                except asyncio.TimeoutError:
                    pass
            if shutdown_event.is_set():
                break

            retry_limit = int(CONFIG["ntrip_max_retries"]) * len(endpoints)
            if retry_limit > 0 and retry_count >= retry_limit:
                app.set_ntrip_state("exhausted")
                app.stats["last_ntrip_error_code"] = "retries_exhausted"
                log("ERROR", "NTRIP", "NTRIP maximum retries reached; client parked.")
                while (not shutdown_event.is_set() and
                       retry_count >= retry_limit):
                    app.beat("ntrip")
                    await asyncio.sleep(5)
                continue

            reader = None
            writer = None
            status_ok = False
            # Access data or Mountpoint are not right: this does not fix itself, so
            # immediately on the slowest beat.
            permanent_error = False
            app.beat("ntrip")

            try:
                app.stats["ntrip_endpoint"] = endpoint["label"]
                app.set_ntrip_state("connecting")
                self._flow_phase("connecting")
                log("INFO", "NTRIP", "Connecting to configured %s caster..." %
                    endpoint["label"])

                if shutdown_event.is_set():
                    break

                # Timeout around connection setup: without it, the task hangs when the
                # caster accepts the TCP and then is silent. The WDT does not cover
                # this, because it is fed by its own task.
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(
                        endpoint["host"], endpoint["port"]),
                    CONFIG.get("ntrip_connect_timeout_sec", 15))

                writer.write(NtripClient._build_request(endpoint))
                await writer.drain()

                self._flow_phase("headers")
                first_line = await asyncio.wait_for(
                    reader.readline(), CONFIG.get("ntrip_header_timeout_sec", 10))
                status_ok, status_error, status_code = self._parse_status_line(first_line)
                if not status_ok:
                    app.set_ntrip_state(status_error)
                    app.stats["last_ntrip_error_code"] = status_error
                    permanent_error = status_error in ("auth_error", "mount_error")
                else:
                    log("INFO", "NTRIP", "Stream connected (status 200).")

                while status_ok:
                    line = await asyncio.wait_for(
                        reader.readline(),
                        CONFIG.get("ntrip_header_timeout_sec", 10))
                    if not line or line == b"\r\n":
                        break

                if not status_ok:
                    app.stats["ntrip_retries"] += 1
                    retry_count += 1
                    error_msg = "HTTP Caster Error (not 200 OK)"
                    app.stats["last_ntrip_error"] = error_msg
                    raise Exception(error_msg)

                app.set_ntrip_state("streaming")
                self._flow["connections"] += 1
                field_recorder.rtcm_boundary()
                diagnostic_data_seen = False
                # Do not mix intentional reconnect downtime into receive gaps.
                # Connection/retry counters separately expose every reconnect.
                self._flow["last_rx_ticks_ms"] = None
                self._flow["last_gga_ticks_ms"] = None
                last_data_ticks_ms = time.ticks_ms()
                # Many casters expect the approximate position immediately after the
                # request - VRS and Nearest Mountpoints deliver without them nothing at
                # all.
                self._last_gga = 0
                if await self._sende_gga(writer):
                    self._last_gga = time.time()

                while not shutdown_event.is_set():
                    self._flow_phase("read")
                    started = time.ticks_ms()
                    self._flow["read_attempts"] += 1
                    try:
                        data = await asyncio.wait_for(
                            reader.read(512),
                            CONFIG["ntrip_read_timeout_sec"]
                        )
                    except asyncio.TimeoutError:
                        self._flow_duration("read_wait", started)
                        self._flow["read_timeouts"] += 1
                        app.beat("ntrip")      # Waiting is not a stalled task
                        idle_ms = time.ticks_diff(time.ticks_ms(), last_data_ticks_ms)
                        if idle_ms >= CONFIG["ntrip_idle_timeout_sec"] * 1000:
                            log("INFO", "NTRIP", "Idle timeout; reconnecting.")
                            app.log_error("NTRIP", "Idle Timeout.")
                            field_recorder.event("ntrip_end", {"reason": "idle_timeout",
                                                 "idle_ms": idle_ms,
                                                 "timeout_sec": CONFIG["ntrip_idle_timeout_sec"]})
                            break
                        # A VRS caster may wait for the next position before
                        # resuming corrections. Keep GGA alive during silence.
                        await self._gga_due(writer)
                        continue

                    self._flow_duration("read_wait", started)
                    if not data:
                        field_recorder.event("ntrip_end", {"reason": "eof"})
                        log("INFO", "NTRIP", "Stream ended (EOF).")
                        break

                    retry_count = 0
                    app.stats["last_ntrip_error_code"] = None
                    last_data_ticks_ms = time.ticks_ms()
                    app.beat("ntrip")
                    app.stats["ntrip_bytes"] += len(data)
                    now = time.ticks_ms()
                    previous_rx = self._flow["last_rx_ticks_ms"]
                    if previous_rx is not None:
                        gap = time.ticks_diff(now, previous_rx)
                        self._flow["rx_gap_ms"] = gap
                        self._flow["rx_gap_max_ms"] = max(
                            self._flow["rx_gap_max_ms"], gap)
                    self._flow["last_rx_ticks_ms"] = now
                    self._flow["rx_chunks"] += 1
                    self._flow["rx_bytes"] += len(data)
                    # First pass on, then count: the correction data should be without
                    # detour to the module.
                    self._flow_phase("uart")
                    started = time.ticks_ms()
                    try:
                        written = self.uart.write(data)
                    finally:
                        self._flow_duration("uart_write", started)
                    self._flow["uart_writes"] += 1
                    self._flow["uart_bytes"] += written if type(written) is int else 0
                    if written != len(data):
                        self._flow["uart_short_writes"] += 1
                    self.rtcm.feed(data)
                    field_recorder.observe_rtcm(data)
                    if not diagnostic_data_seen:
                        diagnostic_data_seen = True
                        field_recorder.event("ntrip_first_data", {"endpoint": endpoint["label"]})

                    await self._gga_due(writer)

            except Exception as e:
                if status_ok:
                    app.stats["ntrip_retries"] += 1
                    retry_count += 1

                field_recorder.event("ntrip_error", {"endpoint": endpoint["label"],
                                     "error_type": type(e).__name__,
                                     "errno": e.args[0] if e.args and type(e.args[0]) is int else None})
                error_msg = str(e)
                app.stats["last_ntrip_error"] = error_msg
                log("ERROR", "NTRIP", "Unexpected NTRIP error: %s" % error_msg,
                    print_traceback=True, exception=e)
                app.log_error("NTRIP", error_msg)
            finally:
                self._flow_phase("closing")
                if writer:
                    try:
                        writer.close()
                        await writer.wait_closed()
                    except Exception:
                        pass

                if not shutdown_event.is_set():
                    # Try each configured caster once before applying the normal
                    # exponential backoff. A healthy stream resets retries and the
                    # next disconnect probes the primary again (controlled failback).
                    if status_ok:
                        endpoint_index = 0
                    elif len(endpoints) > 1:
                        endpoint_index = (endpoint_index + 1) % len(endpoints)
                    completed_round = endpoint_index == 0
                    if app.stats.get("ntrip_state") not in ("auth_error", "mount_error"):
                        app.set_ntrip_state("backoff")
                    if len(endpoints) > 1 and not completed_round:
                        wartezeit = CONFIG.get("ntrip_retry_delay_sec", 5)
                    elif permanent_error:
                        # 401/404 does not heal a new attempt. Until V9.3 the client
                        # then ran forever in 5-s bar against the caster.
                        wartezeit = CONFIG.get("ntrip_retry_max_sec", 300)
                    else:
                        wartezeit = backoff_delay(
                            retry_count, CONFIG["ntrip_retry_delay_sec"],
                            CONFIG.get("ntrip_retry_max_sec", 300))
                    app.stats["ntrip_backoff_sec"] = wartezeit
                    field_recorder.event("ntrip_backoff", {"delay_sec": wartezeit})
                    self._flow_phase("backoff")
                    log("INFO", "NTRIP", "Retry in %ss (attempt %s)..."
                        % (wartezeit, retry_count))
                    # Sleep in slices so that the watchdog gets further signs of life
                    # during long breaks.
                    geschlafen = 0
                    while geschlafen < wartezeit and not shutdown_event.is_set():
                        await asyncio.sleep(min(5, wartezeit - geschlafen))
                        geschlafen += 5
                        app.beat("ntrip")
        self._flow_phase("stopped")
        log("INFO", "NTRIP", "NTRIP client task stopped.")
