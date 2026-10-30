# SPDX-License-Identifier: AGPL-3.0-only
"""Wi-Fi (STA + AP) and NTRIP client. The NTRIP client writes the correction data to the same UART object from which the GNSS handler reads - it is submitted to it by main.py.
"""
import network
import socket
import time
import uasyncio as asyncio
import ubinascii

from cfg import (CONFIG, backoff_delay, config_file_protected, generate_ap_password,
                 commit_pending_config, retry_rollback, rollback_open,
                 is_derived_password, is_factory_password, log, configured_networks,
                 pending_config_active, rollback_pending_config, save_config)
from cfg_logging import HOT_PATH_LOG_INTERVAL_MS, LogThrottle, throttle_note
from field_diagnostics import recorder as field_recorder
from identity import load_identity
from state import app, clock_is_valid as _clock_valid, shutdown_event


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


# A sync that was postponed because a recording was running (see _zeit_holen).
_ntp_deferred = False


def _drop_ntp_deferral():
    """Forget a postponed NTP sync (it is no longer wanted)."""
    global _ntp_deferred
    _ntp_deferred = False


def _zeit_holen():
    """Without this time.time() counts from 1.1.2000 and log lines can not be combined with other recordings. A failure is without consequences - all time measurements in the code are relative (ticks_diff). Never during a recording: ntptime.settime() blocks the loop; deferred and caught up on a connected station once the recording ends (NetworkManager._ntp_catch_up).
    """
    global _ntp_deferred
    if not CONFIG.get("ntp_sync", True):
        _ntp_deferred = False
        return
    if app.recording_active():
        if not _ntp_deferred:
            _ntp_deferred = True
            field_recorder.event("ntp", {"result": "deferred"})
            log("INFO", "WIFI", "NTP deferred until the recording ends.")
        return
    _ntp_deferred = False
    try:
        import ntptime
        was_valid = _clock_valid()
        before, started = time.time(), time.ticks_ms()
        ntptime.settime()
        field_recorder.event("ntp", {"result": "synchronized"})
        # The request itself takes time on the wall clock; only the rest is a correction.
        delta = time.time() - before - time.ticks_diff(time.ticks_ms(), started) // 1000
        if was_valid and abs(delta) > 1:
            # Same bookkeeping and 1 s threshold as an RMC step (a smaller
            # difference is the RTC's whole-second phase): epoch-age caches drop.
            app.clock_steps += 1
            app.stats["clock_resyncs"] = app.stats.get("clock_resyncs", 0) + 1
            app.stats["clock_last_delta_s"] = delta
            field_recorder.event("clock_resync", {"reason": "ntp", "delta_s": delta})
        log("INFO", "WIFI", "Clock synchronized via NTP.")
    except Exception as e:
        field_recorder.event("ntp", {"result": "failed"})
        log("WARN", "WIFI", "NTP failed: %s" % e)


# How long a SETUP entry (BOOT 3 s, or no network configured) keeps the access
# point without station attempts: 10 min. Saving restarts the device.
SETUP_HOLD_MS = 600_000
# While a configuration rollback is open, it is retried at most once a minute
# (flash writes on a failing file system); no station attempt until it completes.
ROLLBACK_RETRY_SEC = 60


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
        self._setup_since_ms = None
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
        password = generate_ap_password()
        if config_file_protected():
            # config.json was unreadable at start-up and holds the real settings;
            # saving now would replace them with defaults. RAM only, as below.
            CONFIG["ap_pass"] = password
            log("WARN", "WIFI", "AP password %s - config.json unreadable, not saved; valid "
                "until the next boot: %s" % (reason, password))
            return
        if not save_config({"ap_pass": password}):
            # Deliberate exception to the rollback of save_config: the old value is
            # empty or predictable, so a random password for this boot only is still
            # the safer AP. The next boot generates another one.
            CONFIG["ap_pass"] = password
            log("ERROR", "WIFI", "AP password %s - could not be saved, valid until the next "
                "boot: %s" % (reason, password))
            return
        log("INFO", "WIFI", "AP password %s - replaced: %s" % (reason, password))

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
        if app.recording_active():
            log("DEBUG", "WIFI", "Requested scan dropped: a recording is active.")
            return
        if not await self._ensure_sta_for_scan():
            self.scan_results, self.scan_error = None, "scan_failed"
            self.scan_completed_ms = time.ticks_ms()
            return
        # The wait inside the activation can overlap the start of a recording.
        if app.recording_active():
            log("DEBUG", "WIFI", "Requested scan dropped: a recording is active.")
            return
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

    async def _ensure_sta_for_scan(self):
        """Activates the station interface for a scan only; True if it is active.

        MicroPython raises "STA must be active" for scan() on an inactive
        interface. No connect() here, so nothing associates, the driver's
        automatic reconnection stays off and the AP is left alone. The station
        stays active afterwards; _connect_sta_versuch finds it active.
        """
        try:
            if not self.wlan_sta.active():
                self._hostname_setzen()
                self.wlan_sta.active(True)
                await asyncio.sleep_ms(100)
            return True
        except OSError as e:
            log("WARN", "WIFI", "Station interface unavailable for the scan: %s" % e)
            return False

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

    def _ntp_catch_up(self):
        """Run an NTP sync that was postponed during a recording. True exactly when an attempt ran."""
        if not _ntp_deferred:
            return False
        if not CONFIG.get("ntp_sync", True):
            _drop_ntp_deferral()
            return False
        if app.recording_active() or not app.wifi_connected_event.is_set():
            return False
        try:
            if not self.wlan_sta.isconnected():
                return False
        except OSError:
            return False
        _zeit_holen()
        return True

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
        """Wi-Fi scan, or none if it doesn't go. Takes about three seconds, measured by the device. It's only worth it from two configured networks -- a single one would pay you three seconds to find out what you're trying anyway. Never call this while recording (check app.recording_active() first).
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
                app.stats["wifi_ssid_active"] = ssid
                app.stats["wifi_ssid_aktiv"] = ssid  # alias of wifi_ssid_active until the alias removal after the state package (#20)
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

        # A scan blocks the loop for about three seconds; while recording all
        # configured networks are tried in their configured order instead.
        if len(networks) > 1 and not app.recording_active():
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
        app.stats["wifi_ssid_active"] = ""
        app.stats["wifi_ssid_aktiv"] = ""  # alias of wifi_ssid_active until the alias removal after the state package (#20)
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

    def _setup_hold_active(self):
        """True while the SETUP window entered in run() is still open (ticks).

        On expiry the window is closed for good: force_setup is cleared, so a
        restart of the network task afterwards connects normally.
        """
        since = self._setup_since_ms
        if since is None:
            return False
        if 0 <= time.ticks_diff(time.ticks_ms(), since) < SETUP_HOLD_MS:
            return True
        if getattr(self, "_rollback_blocked", False):
            log("INFO", "WIFI", "Setup window over; station attempts stay blocked until the "
                                "configuration rollback completes.")
        else:
            log("INFO", "WIFI", "Setup window over; resuming Wi-Fi connection attempts.")
        self._setup_since_ms = None
        app.stats["force_setup"] = False
        return False

    def _enter_rollback_guard(self):
        """Rescue AP while a configuration rollback is open (invariant I1).

        The station link is dropped and the AP switched on explicitly; the loop
        makes no station attempt until _retry_rollback() completes the rollback.
        """
        self._rollback_blocked = True
        self._rollback_wait = ROLLBACK_RETRY_SEC
        # A failed start below is retried in the next pass, then once a minute.
        self._ap_retry_wait = 0
        try:
            self.wlan_sta.disconnect()
        except OSError:
            pass
        app.wifi_connected_event.clear()
        app.stats["sta_ip"] = "N/A"
        self._start_ap()
        if self._setup_since_ms is not None:
            app.set_access_state("SETUP")
            app.stats["net_mode"] = "AP_SETUP"
        else:
            app.set_access_state("RECOVERY")
            app.stats["net_mode"] = "AP_RECOVERY"
        log("WARN", "WIFI", "A configuration rollback is open; access point on, "
                            "no station attempt until it completes.")

    def _retry_failed_ap(self):
        """One maintenance pass for a loop branch that relies on the AP alone.

        A failed AP start (logged by _start_ap) is retried, as in RECOVERY: in the
        next pass, then at most once a minute (a failing driver must not fill the
        log every 5 s). Used by the rollback guard and both SETUP holds.
        """
        if self.wlan_ap.active():
            self._ap_retry_wait = 0
        else:
            self._ap_retry_wait -= 5
            if self._ap_retry_wait <= 0:
                self._ap_retry_wait = ROLLBACK_RETRY_SEC
                self._start_ap()

    def _retry_rollback(self):
        """One maintenance pass of the guard: True once the rollback is complete.

        Only a successful retry_rollback() releases the guard (invariant I2).
        """
        self._retry_failed_ap()
        # A SETUP window that runs out while blocked ends for good (no restart of
        # the window by the supervisor); the state becomes RECOVERY.
        if self._setup_since_ms is not None and not self._setup_hold_active():
            app.set_access_state("RECOVERY")
            app.stats["net_mode"] = "AP_RECOVERY"
        self._rollback_wait -= 5
        if self._rollback_wait > 0:
            return False
        if retry_rollback():
            self._rollback_blocked = False
            log("INFO", "WIFI", "Configuration rollback completed; connecting with the "
                                "confirmed configuration.")
            return True
        log("WARN", "WIFI", "Configuration rollback still open; retrying in %d s." % ROLLBACK_RETRY_SEC)
        self._rollback_wait = ROLLBACK_RETRY_SEC
        return False

    async def run(self):
        log("INFO", "WIFI", "Network manager started.")

        self._rollback_blocked = False
        # A failed AP start at entry is retried in the next pass, then once a minute.
        self._ap_retry_wait = 0
        configured = bool(configured_networks())
        force_setup = bool(app.stats.get("force_setup"))
        if not configured or force_setup:
            # The setup window skips the verdict; an unverified candidate must not
            # be joined after it.
            if pending_config_active():
                rollback_pending_config("setup_mode")
            self._start_ap()
            app.set_access_state("SETUP")
            app.stats["net_mode"] = "AP_SETUP"
            self._setup_since_ms = time.ticks_ms()
            sta_success = False
        elif rollback_open():
            # A recorded rollback is still incomplete: never join the candidate.
            self._setup_since_ms = None
            self._enter_rollback_guard()
            sta_success = False
        else:
            self._setup_since_ms = None
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

        # Covers the commit path, a failed verdict rollback and the SETUP entry.
        if not self._rollback_blocked and rollback_open():
            self._enter_rollback_guard()

        # 3. MAINTENANCE LOOP
        fehlversuche = 0 if sta_success else 1
        naechster_versuch = 0
        while not shutdown_event.is_set():
            await asyncio.sleep(5)
            app.beat("network")
            await self._service_requested_scan()
            if shutdown_event.is_set():
                break
            # An open rollback keeps the AP and makes no station attempt.
            if self._rollback_blocked and not self._retry_rollback():
                continue
            # BOOT/no-network setup keeps the AP; requested scans are still served.
            if self._setup_hold_active():
                self._retry_failed_ap()
                continue

            if not configured_networks():
                self._retry_failed_ap()
                continue

            if self.wlan_sta.isconnected():
                # Not only accept "all good": the driver can have reconnected itself,
                # and then the NTRIP task is still waiting for the event.
                self._adopt_restored_connection()
                # The driver reports the link; a deferred NTP sync runs only on a
                # connected station.
                self._ntp_catch_up()
                self._remember_sta_link(app.stats.get("wifi_ssid_aktiv"))
                fehlversuche = 0
                naechster_versuch = 0
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


WEEK_MS = 604_800_000
DAY_MS = 86_400_000
# A caster stream that still delivers data this long after its first block counts as a success.
NTRIP_STABLE_STREAM_MS = 60000
# Index (number - 1070) // 10 of an MSM message number -> constellation.
_MSM_SOURCES = ("GPS", "GLO", "GAL", "SBAS", "QZSS", "BDS", "IRNSS")
# Service CRS (1300) and RTCM CRS (1302): the only types whose CRC is checked and
# whose payload is captured, at most CRS_CAPTURE_BYTES of it (N = 31 needs 36 B).
CRS_MESSAGES = (1300, 1302)
CRS_CAPTURE_BYTES = 40
# Coordinate epoch DF564 in 0.01 a since CRS_EPOCH_BASE_YEAR; plausible 1988.00..2100.00.
CRS_EPOCH_BASE_YEAR = 1900
CRS_EPOCH_MIN_CE = 8800
CRS_EPOCH_MAX_CE = 20000
# Mirror of tracking.RESERVED_FRAME_NAMES (#39), compared case-insensitively.
_RESERVED_FRAME_NAMES = ("unknown", "mixed")
# CRC24Q (polynomial 0x1864CFB), one entry per 4-bit step like CorrectionProbe.table.
_CRC24Q_NIBBLES = (0x000000, 0x864CFB, 0x8AD50D, 0x0C99F6, 0x93E6E1, 0x15AA1A,
                   0x1933EC, 0x9F7F17, 0xA18139, 0x27CDC2, 0x2B5434, 0xAD18CF,
                   0x3267D8, 0xB42B23, 0xB8B2D5, 0x3EFE2E)


def _crc24q(crc, b):
    """crc extended by one byte b."""
    crc ^= b << 16
    crc = ((crc << 4) & 0xFFFFFF) ^ _CRC24Q_NIBBLES[crc >> 20]
    return ((crc << 4) & 0xFFFFFF) ^ _CRC24Q_NIBBLES[crc >> 20]


def _crs_bits(buf, start, width):
    """Unsigned big-endian bit field of buf; no slice."""
    value = 0
    for index in range(start, start + width):
        value = (value << 1) | ((buf[index >> 3] >> (7 - (index & 7))) & 1)
    return value


def _crs_epoch_text(ce):
    """Coordinate epoch DF564 as text ('2010.0', '2010.5', '2010.25') or None when it
    is 0 (not given) or outside CRS_EPOCH_MIN_CE..CRS_EPOCH_MAX_CE."""
    if not CRS_EPOCH_MIN_CE <= ce <= CRS_EPOCH_MAX_CE:
        return None
    text = "%d.%02d" % (CRS_EPOCH_BASE_YEAR + ce // 100, ce % 100)
    return text[:-1] if text[-1] == "0" else text


def _crs_name(buf, start_bit, count):
    """Name of `count` characters from bit start_bit of buf, stripped; None unless
    printable ASCII, non-empty and not a reserved frame name."""
    chars = []
    for index in range(count):
        code = _crs_bits(buf, start_bit + 8 * index, 8)
        if not 0x20 <= code <= 0x7E:
            return None
        chars.append(chr(code))
    name = "".join(chars).strip()
    if not name or name.lower() in _RESERVED_FRAME_NAMES:
        return None
    return name


def _utc_sow_ms(tm=None):
    """UTC milliseconds since Sunday 00:00 (resolution 1 s). MicroPython and
    CPython count Monday as 0, so Sunday (6) maps to 0."""
    if tm is None:
        tm = time.gmtime()
    return ((tm[6] + 1) % 7) * DAY_MS + (tm[3] * 3600 + tm[4] * 60 + tm[5]) * 1000


class RtcmCounter:
    """Counts RTCM3 message types in the continuous stream and decodes the GNSS epoch of MSM messages to report the real age of the corrections. Works as a state automaton with a fixed 7-byte header buffer, allocated once: the correction data continues unchanged and without delay to the module, here only the frame format is read. Frame format: D3 | 6 bits reserved + 10 bits length | payload | 3 bytes CRC24Q. The message number is the first 12 bits of the payload; an MSM header continues with a 12-bit station id and a 30-bit epoch (payload bytes 3..6). The CRC is deliberately NOT checked for counting (the diagnostics probe does): frame synchronisation over the length field is enough for a statistic, and epochs failing a plausibility check are ignored. Only 1300 (service CRS) and 1302 (RTCM CRS) are CRC-checked, because their frame name may be adopted into the field setup (#38b); a CRC-valid announcement is decoded when its payload changed and belongs to the current stream (drop_crs). Once in the clock, the automaton stays in the clock; a 0xD3 in the payload can start a false frame, which washes out after a few frames. Everything is O(1) per byte and per summary().

    Epoch age: the device clock (UTC, 1 s resolution, valid only 2024..2099) is converted to the epoch's time scale (GPS/Galileo/QZSS/SBAS/IRNSS time of week = UTC + leap seconds, BeiDou = GPS - 14 s, GLONASS = Moscow day of week + ms of day), the age is the difference modulo a week (a day when GLONASS reports day 7), future epochs clamp to 0. The age is stored with the tick of its decode and grows with ticks_ms() afterwards. A clock step (NTP, or the RMC re-sync in gnss.py) between two valid times drops the cached age at once and the next MSM frame restores it, i.e. within about a second. The epoch cache holds the most recent MSM of any constellation.
    """

    SEARCH, LENGTH, NUMBER, REST, EPOCH, CRS = 0, 1, 2, 3, 4, 5

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
        # Header bytes of the current frame: two length bytes, then the payload up to
        # the end of an MSM epoch field; _head_len of them are valid.
        self._buf = bytearray(7)
        self._head_len = 0
        self._payload_len = 0
        self._rest = 0
        # First length byte (6 reserved bits included) for the CRC of a CRS frame.
        self._lead = 0
        # 1300/1302 (#38b): payload capture, running CRC and the received CRC bytes;
        # the last accepted capture per type with its length (0 = none) so that an
        # unchanged announcement is not decoded again.
        self._crs_buf = bytearray(CRS_CAPTURE_BYTES)
        self._crc = 0
        self._crc_got = 0
        self._crs_last_1300 = bytearray(CRS_CAPTURE_BYTES)
        self._crs_last_1302 = bytearray(CRS_CAPTURE_BYTES)
        self._crs_last_len_1300 = 0
        self._crs_last_len_1302 = 0
        self.service_crs = None
        self.rtcm_crs = None
        self._service_crs_uptime_s = None
        self._service_crs_uptime_ms = 0
        self._rtcm_crs_uptime_s = None
        self._rtcm_crs_uptime_ms = 0
        self.crs_crc_failures = 0
        self.crs_rejected = 0

    def reset_stream(self):
        """Call on a new connection: the parser restarts at a frame boundary and the
        epoch cache and the CRS announcements are dropped. Counts, bytes_total,
        last_msg_ms, the message uptime and the CRS frame counters are kept so that
        $PESPS and the message statistics keep their meaning."""
        self._state = self.SEARCH
        self._head_len = 0
        self._payload_len = 0
        self._rest = 0
        self.last_epoch_age_ms = None
        self.last_epoch_ticks_ms = None
        self.last_epoch_source = None
        self.drop_crs()

    def drop_crs(self):
        """Forget both CRS announcements: they belong to the stream that sent them."""
        self.service_crs = None
        self.rtcm_crs = None
        self._service_crs_uptime_s = None
        self._service_crs_uptime_ms = 0
        self._rtcm_crs_uptime_s = None
        self._rtcm_crs_uptime_ms = 0
        self._crs_last_len_1300 = 0
        self._crs_last_len_1302 = 0

    def feed(self, data):
        """Parse one chunk. One loop with the automaton in local variables; header
        bytes go into the fixed _buf, the rest of a payload and the CRC are skipped
        by index arithmetic. A 1300/1302 frame instead runs byte by byte through the
        CRC (state CRS, the same byte count as REST), its payload start goes into
        the fixed _crs_buf. No buffer is allocated per frame."""
        n = len(data)
        self.bytes_total += n
        SEARCH, LENGTH, NUMBER, REST = self.SEARCH, self.LENGTH, self.NUMBER, self.REST
        CRS = self.CRS
        state = self._state
        buf = self._buf
        head = self._head_len
        payload_len = self._payload_len
        rest = self._rest
        lead = self._lead
        i = 0
        while i < n:
            if state == REST:
                # Skip the remaining payload plus the three CRC bytes in one step.
                step = n - i
                if rest < step:
                    step = rest
                i += step
                rest -= step
                if rest <= 0:
                    state = SEARCH
                continue
            b = data[i]
            i += 1
            if state == SEARCH:
                if b == 0xD3:
                    head = 0
                    state = LENGTH
            elif state == LENGTH:
                buf[head] = b
                head += 1
                if head == 2:
                    lead = buf[0]
                    payload_len = ((buf[0] & 0x03) << 8) | buf[1]
                    head = 0
                    # Too short for a message number - discard.
                    state = SEARCH if payload_len < 2 else NUMBER
            elif state == NUMBER:
                buf[head] = b
                head += 1
                if head == 2:
                    number = (buf[0] << 4) | (buf[1] >> 4)
                    self.counts[number] = self.counts.get(number, 0) + 1
                    now = time.ticks_ms()
                    self.last_msg_ms = now
                    # Read, never advance, app's uptime accumulator: small-int
                    # arithmetic only, the bigint uptime is formed when a report asks.
                    self._msg_uptime_s = app._uptime_s
                    self._msg_uptime_ms = (app._uptime_rem_ms
                                           + max(0, time.ticks_diff(now, app._uptime_at)))
                    if (1071 <= number <= 1137 and 1 <= number % 10 <= 7
                            and payload_len >= 7):
                        # MSM header: keep collecting up to the end of the epoch field.
                        state = self.EPOCH
                    else:
                        rest = payload_len - 2 + 3
                        head = 0
                        state = REST
                        if number in CRS_MESSAGES:
                            crs = self._crs_buf
                            crs[0] = buf[0]
                            crs[1] = buf[1]
                            crc = _crc24q(_crc24q(0, 0xD3), lead)
                            crc = _crc24q(_crc24q(crc, payload_len & 0xFF), buf[0])
                            self._crc = _crc24q(crc, buf[1])
                            self._crc_got = 0
                            state = CRS
            elif state == CRS:
                # Payload into the capture (first CRS_CAPTURE_BYTES), all of it
                # through the CRC, then the three received CRC bytes.
                if rest > 3:
                    position = payload_len + 3 - rest
                    if position < CRS_CAPTURE_BYTES:
                        self._crs_buf[position] = b
                    self._crc = _crc24q(self._crc, b)
                else:
                    self._crc_got = (self._crc_got << 8) | b
                rest -= 1
                if rest == 0:
                    self._crs_frame(payload_len)
                    state = SEARCH
            else:
                # EPOCH
                buf[head] = b
                head += 1
                if head == 7:
                    self._decode_epoch((buf[0] << 4) | (buf[1] >> 4), buf)
                    rest = payload_len - 7 + 3
                    head = 0
                    state = REST
        self._state = state
        self._head_len = head
        self._payload_len = payload_len
        self._rest = rest
        self._lead = lead

    def _crs_frame(self, payload_len):
        """End of a 1300/1302 frame (E2, E6, E7a): a CRC failure keeps the previous
        announcement; an unchanged capture only renews its time; anything else is
        decoded and replaces it, an unusable frame withdraws it."""
        if self._crc != self._crc_got:
            self.crs_crc_failures += 1
            return
        crs = self._crs_buf
        service = ((crs[0] << 4) | (crs[1] >> 4)) == 1300
        size = payload_len if payload_len < CRS_CAPTURE_BYTES else CRS_CAPTURE_BYTES
        last = self._crs_last_1300 if service else self._crs_last_1302
        if service and payload_len > CRS_CAPTURE_BYTES:
            value = None    # E6: a 1300 never exceeds the capture
        else:
            same = size == (self._crs_last_len_1300 if service
                            else self._crs_last_len_1302)
            index = 0
            while same and index < size:
                same = last[index] == crs[index]
                index += 1
            if same:
                self._stamp_crs(service)
                return
            value = self._decode_crs(service, payload_len)
        if value is None:
            self.crs_rejected += 1
            size = 0
        else:
            for index in range(size):
                last[index] = crs[index]
        if service:
            self.service_crs = value
            self._crs_last_len_1300 = size
        else:
            self.rtcm_crs = value
            self._crs_last_len_1302 = size
        if value is None:
            if service:
                self._service_crs_uptime_s = None
            else:
                self._rtcm_crs_uptime_s = None
        else:
            self._stamp_crs(service)

    def _stamp_crs(self, service):
        """Uptime of the announcement as two small ints, like _msg_uptime_s/_ms."""
        now = time.ticks_ms()
        stamp_ms = app._uptime_rem_ms + max(0, time.ticks_diff(now, app._uptime_at))
        if service:
            self._service_crs_uptime_s = app._uptime_s
            self._service_crs_uptime_ms = stamp_ms
        else:
            self._rtcm_crs_uptime_s = app._uptime_s
            self._rtcm_crs_uptime_ms = stamp_ms

    def _decode_crs(self, service, payload_len):
        """New service_crs / rtcm_crs object from _crs_buf, or None (E5, E6)."""
        crs = self._crs_buf
        bits = payload_len * 8
        if bits < 17:
            return None
        count = _crs_bits(crs, 12, 5)
        if bits < (33 if service else 26) + 8 * count:
            return None
        name = _crs_name(crs, 17, count)
        if name is None:
            return None
        start = 17 + 8 * count
        if service:
            ce = _crs_bits(crs, start, 16)
            return {"name": name, "epoch": _crs_epoch_text(ce), "epoch_raw": ce}
        return {"name": name,
                "anchor": "plate_fixed" if _crs_bits(crs, start, 1) else "global",
                "plate": _crs_bits(crs, start + 1, 5),
                "database_links": _crs_bits(crs, start + 6, 3)}

    def _crs_age_sec(self, stamp_s, stamp_ms):
        return round(max(0, app.uptime_ms() - (stamp_s * 1000 + stamp_ms)) / 1000, 1)

    def crs_summary(self):
        """CRS announcements for /status and /rtcm: copies with age_sec, plus the
        frame counters."""
        service = rtcm = None
        if self.service_crs is not None:
            service = dict(self.service_crs)
            service["age_sec"] = self._crs_age_sec(self._service_crs_uptime_s,
                                                   self._service_crs_uptime_ms)
        if self.rtcm_crs is not None:
            rtcm = dict(self.rtcm_crs)
            rtcm["age_sec"] = self._crs_age_sec(self._rtcm_crs_uptime_s,
                                                self._rtcm_crs_uptime_ms)
        return {"service_crs": service, "rtcm_crs": rtcm,
                "crs_frames": {"crc_failures": self.crs_crc_failures,
                               "rejected": self.crs_rejected}}

    def frame_suggestion(self, setup):
        """The 1300 announcement rated against the field setup (E4, E8), or None.

        state: "new" without a reference frame in setup; "matches" when the frame is
        exactly equal (no case folding) and the announced epoch is empty or equal;
        "differs" otherwise, also when only the epoch differs. anchor comes from a
        1302 of the same name only."""
        service = self.service_crs
        if service is None:
            return None
        name, epoch = service["name"], service["epoch"] or ""
        frame = (setup.get("reference_frame") or "").strip()
        if not frame:
            state = "new"
        elif frame == name and (not epoch or epoch ==
                                (setup.get("reference_frame_epoch") or "").strip()):
            state = "matches"
        else:
            state = "differs"
        rtcm = self.rtcm_crs
        anchor = rtcm["anchor"] if rtcm is not None and rtcm["name"] == name else None
        return {"reference_frame": name, "reference_frame_epoch": epoch,
                "anchor": anchor, "state": state}

    def _decode_epoch(self, number, buf):
        """Store the age of the MSM epoch in buf[3:7]; ignore implausible values."""
        epoch = (buf[3] << 22) | (buf[4] << 14) | (buf[5] << 6) | (buf[6] >> 2)
        tm = time.gmtime()
        if not _clock_valid(tm):
            return
        source = _MSM_SOURCES[(number - 1070) // 10]
        sow = _utc_sow_ms(tm)
        if source == "GLO":
            day = epoch >> 27
            tod = epoch & 0x7FFFFFF
            if day > 7 or tod >= DAY_MS:
                return
            moscow = sow + 3 * 3600 * 1000
            if day == 7:
                modulus, device, value = DAY_MS, moscow % DAY_MS, tod
            else:
                modulus, device, value = WEEK_MS, moscow % WEEK_MS, day * DAY_MS + tod
        else:
            if epoch >= WEEK_MS:
                return
            offset = CONFIG["gps_utc_offset_sec"]
            if source == "BDS":
                offset -= 14
            modulus, device, value = WEEK_MS, (sow + offset * 1000) % WEEK_MS, epoch
        age = (device - value) % modulus
        if age > modulus // 2:
            age = 0     # epoch ahead of the 1 s clock: treat as fresh
        self.last_epoch_age_ms = age
        self.last_epoch_ticks_ms = time.ticks_ms()
        self.last_epoch_source = source
        self._epoch_clock_steps = app.clock_steps

    def epoch_age_ms(self, now_ms=None):
        """Age of the newest MSM epoch in ms, or None before the first plausible
        epoch or while the device clock is invalid."""
        if self.last_epoch_age_ms is None or not _clock_valid():
            return None
        if self._epoch_clock_steps != app.clock_steps:
            # The RTC was stepped after this age was derived from it: the next MSM
            # frame (about a second) restores a correct value.
            return None
        if now_ms is None:
            now_ms = time.ticks_ms()
        elapsed = time.ticks_diff(now_ms, self.last_epoch_ticks_ms)
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
        return max(0, app.uptime_ms() - stamp)

    @staticmethod
    def age_is_stale(age_ms):
        """The one stale rule: an epoch older than ntrip_max_epoch_age_sec; no age is not stale."""
        return age_ms is not None and age_ms > CONFIG["ntrip_max_epoch_age_sec"] * 1000

    def epoch_stale(self, now_ms=None):
        return self.age_is_stale(self.epoch_age_ms(now_ms))

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


def _resolve_address(host, port):
    """Numeric address of host, or None when the sockaddr format is not understood."""
    info = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    if not info:
        raise OSError("no address")
    addr = info[0][-1]
    if type(addr) is tuple and addr and type(addr[0]) is str:
        return addr[0]
    return None


_UNEXPECTED_ERROR_LOG = LogThrottle(HOT_PATH_LOG_INTERVAL_MS)


def _log_unexpected_error(error, error_msg):
    # Throttled per error type and errno: an unreachable caster repeats the same failure; counts are /status stats.ntrip_retries.
    errno = error.args[0] if error.args and type(error.args[0]) is int else ""
    n = _UNEXPECTED_ERROR_LOG.hit("%s:%s" % (type(error).__name__, errno))
    if n:
        log("ERROR", "NTRIP", "Unexpected NTRIP error: %s%s" % (error_msg, throttle_note(n)),
            print_traceback=True, exception=error)


class NtripMachine:
    """The connection rules of the NTRIP client, without a socket.

    Endpoints and ntrip_config from one CONFIG reading (#20a E4), the endpoint choice
    with controlled failback, counting, backoff and parking (#16 E3-E7), the stale
    substate and the stale-backlog reconnect (M1 #6), and the reload request of a saved
    caster change (#23 part b). NtripClient.run() keeps the sockets and the recorder
    events that carry the attempt times, and asks this class what an attempt means.
    """

    def __init__(self, rtcm):
        # The client's RtcmCounter (same object): the stale rule reads its epoch age.
        self.rtcm = rtcm
        self.endpoints = []
        self.index = 0
        self.retry_count = 0
        # State the last settled attempt left behind; a parked client keeps it,
        # so a 401/404 stays visible as auth_error/mount_error.
        self.settled_state = "backoff"
        # Whether the current attempt's stream was stable (data for NTRIP_STABLE_STREAM_MS).
        self.stable = False
        self.first_data_ms = None
        self._stale_since_ms = None
        # A plain attribute, not a property: run() checks it after every read.
        self.reload_requested = False

    @staticmethod
    def configured_endpoints():
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
    def config_state():
        """Effective NTRIP configuration of the running client (see #30).

        "switched_off" (ntrip_enabled false), "incomplete" (enabled, but no endpoint
        with host and mountpoint) or "ready"; same rule as configured_endpoints().
        """
        if not CONFIG.get("ntrip_enabled", True):
            return "switched_off"
        for prefix in ("ntrip_", "ntrip_fallback_"):
            if CONFIG.get(prefix + "host") and CONFIG.get(prefix + "mount"):
                return "ready"
        return "incomplete"

    @staticmethod
    def keeps(endpoint):
        """Whether the endpoint (host, port, mount, credentials) is still configured."""
        return endpoint in NtripMachine.configured_endpoints()

    def request_reload(self):
        self.reload_requested = True

    def load(self, current=None):
        """Take the endpoints and publish ntrip_config from the same CONFIG reading, and
        consume a pending reload request.

        True: current (the endpoint in use) is unchanged in the new list; its index and
        the count stay, so the running attempt goes on. False: start at the primary with
        a fresh round, no backoff pending; the running attempt (if any) ends.
        """
        self.reload_requested = False
        self.endpoints = self.configured_endpoints()
        app.stats["ntrip_config"] = self.config_state()
        if current is not None and current in self.endpoints:
            self.index = self.endpoints.index(current)
            return True
        self.index = 0
        self.retry_count = 0
        self.settled_state = "backoff"
        app.stats["last_ntrip_error_code"] = None
        app.stats["ntrip_backoff_sec"] = 0
        if not self.endpoints:
            app.stats["ntrip_endpoint"] = None
        return False

    def endpoint(self):
        return self.endpoints[self.index]

    def begin_attempt(self):
        self.stable = False
        self.first_data_ms = None

    def ok_data(self, now_ms):
        """A data block arrived; the stream is stable once it has delivered for
        NTRIP_STABLE_STREAM_MS since its first block."""
        if self.first_data_ms is None:
            self.first_data_ms = now_ms
        elif time.ticks_diff(now_ms, self.first_data_ms) >= NTRIP_STABLE_STREAM_MS:
            self.stable = True

    def retry_limit_reached(self):
        # ntrip_max_retries counts per endpoint; 0 means no limit.
        retry_limit = int(CONFIG["ntrip_max_retries"]) * len(self.endpoints)
        return retry_limit > 0 and self.retry_count >= retry_limit

    def park(self):
        # Parked: the state of the last attempt (backoff, or auth_error/mount_error
        # after a permanent error), no backoff pending, the reason in the code.
        app.stats["last_ntrip_error_code"] = "retries_exhausted"
        app.stats["ntrip_backoff_sec"] = 0
        app.set_ntrip_state(self.settled_state)

    def release(self):
        # Released (by a Wi-Fi loss): a fresh count, and the parked reason goes with it.
        self.retry_count = 0
        app.stats["last_ntrip_error_code"] = None

    def settle(self, outcome, permanent):
        """Settle one finished attempt; returns the seconds to wait before the next.

        One verdict counts the attempt and picks the next endpoint. A failure moves on
        to the next configured caster; controlled failback only after a stable stream
        or a stale-backlog reconnect; a 200 alone is not success.
        """
        self.retry_count, succeeded = self.settle_attempt(self.retry_count, outcome, self.stable)
        self.index = 0 if succeeded else (self.index + 1) % len(self.endpoints)
        completed_round = self.index == 0
        settled_state = app.stats.get("ntrip_state")
        if settled_state not in ("auth_error", "mount_error"):
            settled_state = "backoff"
            app.set_ntrip_state("backoff")
        self.settled_state = settled_state
        # 401/404 does not heal with a new attempt: the slowest beat, once the
        # round is over (until V9.3 the client ran in a 5-s beat against it).
        wartezeit = self.retry_wait(self.retry_count, completed_round, permanent)
        app.stats["ntrip_backoff_sec"] = wartezeit
        return wartezeit

    def clear_stale(self):
        """Forget the stale flag silently (new connection or connection end); the
        recorder already marks both with ntrip_end / the stream boundary."""
        app.stats["ntrip_substate"] = None
        self._stale_since_ms = None

    def update_stale(self, now_ms):
        """Sets app.stats["ntrip_substate"] to "stale" while the newest MSM epoch is
        older than ntrip_max_epoch_age_sec, else None, and records every transition.
        The stale timer runs on ticks, never on the wall clock."""
        age = self.rtcm.epoch_age_ms(now_ms)
        state = "stale" if self.rtcm.age_is_stale(age) else None
        previous = app.stats.get("ntrip_substate")
        if state == previous:
            return
        app.stats["ntrip_substate"] = state
        self._stale_since_ms = now_ms if state else None
        field_recorder.event("ntrip_substate", {
            "previous": previous, "state": state,
            "epoch_age_sec": None if age is None else round(age / 1000, 1)})

    def check_stale(self, now_ms):
        """Read-loop hook (after data and on read timeouts): updates the substate and,
        once the stream has been stale for longer than ntrip_reconnect_on_stale_sec,
        returns the ntrip_end payload (after logging and counting why) that ends the
        connection, else None; a fresh connection drops the caster's backlog. The
        caller adds the attempt times."""
        self.update_stale(now_ms)
        limit = CONFIG.get("ntrip_reconnect_on_stale_sec", 0)
        if limit <= 0 or self._stale_since_ms is None:
            return None
        if time.ticks_diff(now_ms, self._stale_since_ms) <= limit * 1000:
            return None
        age = self.rtcm.epoch_age_ms(now_ms)
        log("INFO", "NTRIP", "Correction epochs stale for %d s; reconnecting to drop the backlog."
            % (time.ticks_diff(now_ms, self._stale_since_ms) // 1000))
        app.stats["last_ntrip_error_code"] = "stale_backlog"
        app.stats["ntrip_retries"] += 1
        return {"reason": "stale_backlog",
                "epoch_age_sec": None if age is None else round(age / 1000, 1)}

    @staticmethod
    def error_code(error):
        """Map an exception from a connection attempt to a last_ntrip_error_code."""
        # TimeoutError first: on CPython it is a subclass of OSError.
        if isinstance(error, asyncio.TimeoutError):
            return "timeout"
        if isinstance(error, OSError):
            return "network_error"
        return "unexpected_error"

    @staticmethod
    def retry_wait(retry_count, completed_round, permanent_error):
        """Seconds to wait before the next attempt; retry_count already includes the failed one."""
        max_sec = CONFIG.get("ntrip_retry_max_sec", 300)
        if permanent_error and completed_round:
            return max_sec
        return backoff_delay(retry_count, CONFIG["ntrip_retry_delay_sec"], max_sec)

    @staticmethod
    def settle_attempt(retry_count, outcome, stable):
        """Account for one finished attempt: returns (retry_count, succeeded).

        One rule for counting, reset, error code and endpoint choice; see #16 E4/E5.
        outcome is always a code here: only a shutdown or a cancellation (a BaseException
        the except clause in run() does not catch) leaves it empty, and neither is settled.
        """
        app.stats["last_ntrip_error_code"] = outcome
        if stable or outcome == "stale_backlog":
            return 0, True
        app.stats["ntrip_retries"] += 1
        return retry_count + 1, False


class NtripClient:
    def __init__(self, tx):
        # The one receiver UART writer (gnss.UartTx); it owns the uart_* flow counters.
        self.tx = tx
        # Survives reconnects so that the statistics do not start from the beginning
        # with every connection disconnection.
        self.rtcm = RtcmCounter()
        self.machine = NtripMachine(self.rtcm)
        # Fixed-size diagnostics use the device clock, independently of when a
        # delayed HTTP response reaches the observer. No payload or credentials.
        self._flow = {
            "schema": 1, "connections": 0, "phase": "idle",
            "phase_since_ms": time.ticks_ms(), "read_attempts": 0,
            "read_timeouts": 0, "read_wait_ms": 0, "read_wait_max_ms": 0,
            "rx_chunks": 0, "rx_bytes": 0, "last_rx_ticks_ms": None,
            "rx_gap_ms": 0, "rx_gap_max_ms": 0,
            "gga_writes": 0, "gga_failures": 0,
            "last_gga_ticks_ms": None, "gga_gap_max_ms": 0,
            "gga_write_ms": 0, "gga_write_max_ms": 0,
            "dns_ms": 0, "dns_max_ms": 0, "dns_unseparated": 0,
            "connect_ms": 0, "connect_max_ms": 0,
        }
        # Timing of the latest connection attempt, attached to the events that end
        # or confirm it (no extra event per attempt: the recorder queue is small).
        self._attempt_seq = 0
        self._attempt_started_ms = None
        self._attempt_dns_ms = None
        self._attempt_connect_ms = None
        # The open connection of the running attempt and its endpoint, for reload()
        # only: the writer belongs to run(), which also closes it in its finally.
        self._attempt_writer = None
        self._attempt_endpoint = None
        # Set once a reload closed the attempt's connection (see reload()).
        self._reload_closed = False

    def reload(self):
        """Take over the saved caster settings without a restart; always True.

        Synchronous, called from the HTTP handler after the configuration was saved.
        Takes effect at the next return of a pending I/O or wait slice: run() then
        reads the configuration again and ends the attempt unless its endpoint is
        unchanged. An open connection whose endpoint is no longer configured is closed
        at once, so that a pending read, readline or drain returns now. The service CRS
        announcement is dropped at once unless the running endpoint is kept, with or
        without an open connection (#38b E7).
        """
        self.machine.request_reload()
        if not NtripMachine.keeps(self._attempt_endpoint):
            self.rtcm.drop_crs()
        if (self._attempt_writer is not None and not self._reload_closed
                and not NtripMachine.keeps(self._attempt_endpoint)):
            self._abort_io()
        return True

    def _abort_io(self):
        """Close the attempt's connection under a pending I/O; run() still owns it."""
        self._reload_closed = True
        writer = self._attempt_writer
        try:
            writer.close()
        except Exception:
            pass
        # MicroPython 1.28: Stream.close() is a no-op and the socket only closes in
        # wait_closed(). Closing it here wakes the task waiting in the poller, whose
        # read/readline/drain then raises OSError.
        sock = getattr(writer, "s", None)
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass

    def _reload_ends_attempt(self, endpoint):
        """Consume a pending reload after an I/O return; True ends the running attempt.

        Shutdown wins: while it is set, nothing is consumed. A connection the reload
        already closed always ends with a fresh round, even when its endpoint was
        configured again before the I/O returned.
        """
        if shutdown_event.is_set():
            return False
        kept = self.machine.load(current=None if self._reload_closed else endpoint)
        self._log_reload(kept)
        return not kept

    def _log_reload(self, kept):
        if kept:
            message = "Configuration changed; the active caster is unchanged."
        elif self.machine.endpoints:
            message = "Configuration changed; reconnecting with the saved caster settings."
        else:
            message = "Configuration changed; NTRIP client idle (%s)." % app.stats["ntrip_config"]
        log("INFO", "NTRIP", message)

    def _flow_phase(self, phase):
        self._flow["phase"] = phase
        self._flow["phase_since_ms"] = time.ticks_ms()

    def _flow_duration(self, name, started):
        elapsed = max(0, time.ticks_diff(time.ticks_ms(), started))
        self._flow[name + "_ms"] = elapsed
        key = name + "_max_ms"
        self._flow[key] = max(self._flow[key], elapsed)

    async def _open_timed(self, endpoint):
        """Resolve and connect, timing both parts of one connection attempt.

        Name resolution is synchronous (it blocks the event loop) and, as before,
        counts against the same connect deadline as the TCP connect. open_connection()
        with an IP address resolves only a numeric address. An unknown address format
        falls back to the hostname: the measurement is then unseparated (dns_ms None,
        connect_ms is the whole attempt)."""
        started = self._attempt_started_ms = time.ticks_ms()
        try:
            address = _resolve_address(endpoint["host"], endpoint["port"])
        except Exception:
            self._flow_duration("dns", started)
            self._attempt_dns_ms = self._flow["dns_ms"]
            raise
        if address is None:
            self._flow["dns_ms"] = None
            self._flow["dns_unseparated"] += 1
            target = endpoint["host"]
            connect_started = started
        else:
            self._flow_duration("dns", started)
            self._attempt_dns_ms = self._flow["dns_ms"]
            target = address
            connect_started = time.ticks_ms()
        try:
            return await asyncio.open_connection(target, endpoint["port"])
        finally:
            self._flow_duration("connect", connect_started)
            self._attempt_connect_ms = self._flow["connect_ms"]

    def _attempt_timing(self):
        """Payload keys that place the latest attempt in time: its start is the
        event's up_ms minus attempt_age_ms, the resolution ends dns_ms later."""
        started = self._attempt_started_ms
        return {"attempt": self._attempt_seq, "dns_ms": self._attempt_dns_ms,
                "connect_ms": self._attempt_connect_ms,
                "attempt_age_ms": None if started is None
                else max(0, time.ticks_diff(time.ticks_ms(), started))}

    def flow_summary(self):
        """Receive gaps include scheduling delay, not just wire arrival time."""
        now = time.ticks_ms()
        report = dict(self._flow)
        report.update(self.tx.counters)
        report["observed_ticks_ms"] = now
        report["phase_age_ms"] = time.ticks_diff(now, report["phase_since_ms"])
        last = report["last_rx_ticks_ms"]
        report["rx_age_ms"] = None if last is None else time.ticks_diff(now, last)
        return report

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
        """Send the GGA sentence of the published fix to the caster, only while it is fresh.

        Fresh means younger than fix_stale_after_ms and carrying a fix. Otherwise nothing is
        sent and the caster keeps its last position: a position without fix (or even 0/0)
        would create the wrong virtual station at a VRS mountpoint.
        """
        if not CONFIG.get("ntrip_gga_interval_sec"):
            return False
        sentence = app.gga_for_caster()
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
            if self._reload_closed:
                # The reload closed the connection: no GGA failure.
                return False
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
        # Ticks, not the wall clock: an RTC step must not stall or burst the GGA.
        now = time.ticks_ms()
        if self._last_gga is not None and time.ticks_diff(now, self._last_gga) < intervall * 1000:
            return False
        self._last_gga = now
        return await self._sende_gga(writer)

    async def run(self):
        log("INFO", "NTRIP", "Client started.")
        self._last_gga = None
        machine = self.machine
        # Endpoints and ntrip_config; a saved change arrives as a reload request.
        machine.load()

        while not shutdown_event.is_set():
            if machine.reload_requested:
                # Between attempts: nothing runs that could keep its endpoint.
                machine.load()
                self._log_reload(False)
            if not machine.endpoints:
                # Switched off or incomplete: idle until shutdown or a saved change.
                app.set_ntrip_state("disabled")
                self._flow_phase("idle")
                while not shutdown_event.is_set() and not machine.reload_requested:
                    app.beat("ntrip")
                    await asyncio.sleep(5)
                continue
            endpoint = machine.endpoint()

            # Waiting without blocking: without Wi-Fi, this task waits for any length of
            # time, and without signs of life, the watchdog would mistakenly consider it
            # to be haunting.
            app.set_ntrip_state("waiting_wifi")
            self._flow_phase("waiting_wifi")
            while not app.wifi_connected_event.is_set():
                if shutdown_event.is_set() or machine.reload_requested:
                    break
                app.beat("ntrip")
                try:
                    await asyncio.wait_for(app.wifi_connected_event.wait(), 5)
                except asyncio.TimeoutError:
                    pass
            if shutdown_event.is_set():
                break
            if machine.reload_requested:
                continue

            if machine.retry_limit_reached():
                # Heartbeats keep the supervised task alive; no further attempt is made.
                # A Wi-Fi loss releases the client with a fresh count, a saved caster
                # change with a fresh round (the reload at the start of the next round).
                # A dropout shorter than one 5-s slice goes unnoticed.
                machine.park()
                log("ERROR", "NTRIP",
                    "NTRIP maximum retries reached; client parked until restart or Wi-Fi loss.")
                while (not shutdown_event.is_set() and app.wifi_connected_event.is_set()
                       and not machine.reload_requested):
                    app.beat("ntrip")
                    await asyncio.sleep(5)
                if shutdown_event.is_set():
                    break
                if not machine.reload_requested:
                    machine.release()
                continue

            reader = None
            writer = None
            status_ok = False
            # Access data or mountpoint are not right: this does not fix itself, so the
            # slowest beat once the attempt closes the round (see NtripMachine.retry_wait).
            permanent_error = False
            # How the attempt ended (a last_ntrip_error_code); with the machine's
            # stability verdict it settles the attempt below.
            outcome = None
            # A saved caster change ended the attempt: neither counted nor backed off.
            # After every return of a suspending I/O, shutdown is checked first, then a
            # pending reload, and only then the result of the I/O.
            reloaded = False
            self._reload_closed = False
            machine.begin_attempt()
            app.beat("ntrip")

            try:
                # A new attempt starts here: an error before _open_timed() must not
                # carry the previous attempt's times.
                self._attempt_seq += 1
                self._attempt_started_ms = None
                self._attempt_dns_ms = self._attempt_connect_ms = None
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
                    self._open_timed(endpoint),
                    CONFIG.get("ntrip_connect_timeout_sec", 15))
                self._attempt_writer, self._attempt_endpoint = writer, endpoint
                if machine.reload_requested and self._reload_ends_attempt(endpoint):
                    reloaded = True
                    continue

                writer.write(NtripClient._build_request(endpoint))
                await writer.drain()
                if machine.reload_requested and self._reload_ends_attempt(endpoint):
                    reloaded = True
                    continue

                self._flow_phase("headers")
                first_line = await asyncio.wait_for(
                    reader.readline(), CONFIG.get("ntrip_header_timeout_sec", 10))
                if machine.reload_requested and self._reload_ends_attempt(endpoint):
                    reloaded = True
                    continue
                status_ok, status_error, status_code = self._parse_status_line(first_line)
                if not status_ok:
                    outcome = status_error
                    permanent_error = status_error in ("auth_error", "mount_error")
                    if permanent_error:
                        app.set_ntrip_state(status_error)
                else:
                    log("INFO", "NTRIP", "Stream connected (status 200).")

                while status_ok:
                    line = await asyncio.wait_for(
                        reader.readline(),
                        CONFIG.get("ntrip_header_timeout_sec", 10))
                    if machine.reload_requested and self._reload_ends_attempt(endpoint):
                        reloaded = True
                        break
                    if not line or line == b"\r\n":
                        break
                if reloaded:
                    continue

                if not status_ok:
                    error_msg = "HTTP Caster Error (not 200 OK)"
                    app.stats["last_ntrip_error"] = error_msg
                    raise Exception(error_msg)

                app.set_ntrip_state("streaming")
                self._flow["connections"] += 1
                field_recorder.rtcm_boundary()
                self.rtcm.reset_stream()
                machine.clear_stale()
                diagnostic_data_seen = False
                # Do not mix intentional reconnect downtime into receive gaps.
                # Connection/retry counters separately expose every reconnect.
                self._flow["last_rx_ticks_ms"] = None
                self._flow["last_gga_ticks_ms"] = None
                last_data_ticks_ms = time.ticks_ms()
                # Many casters expect the approximate position immediately after the
                # request - VRS and Nearest Mountpoints deliver without them nothing at
                # all.
                self._last_gga = None
                if await self._sende_gga(writer):
                    self._last_gga = time.ticks_ms()
                if machine.reload_requested and self._reload_ends_attempt(endpoint):
                    reloaded = True
                    continue

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
                        if machine.reload_requested and self._reload_ends_attempt(endpoint):
                            reloaded = True
                            break
                        # A stream that stops delivering bytes keeps ageing.
                        end = machine.check_stale(time.ticks_ms())
                        if end:
                            end.update(self._attempt_timing())
                            field_recorder.event("ntrip_end", end)
                            outcome = "stale_backlog"
                            break
                        idle_ms = time.ticks_diff(time.ticks_ms(), last_data_ticks_ms)
                        if idle_ms >= CONFIG["ntrip_idle_timeout_sec"] * 1000:
                            log("INFO", "NTRIP", "Idle timeout; reconnecting.")
                            app.log_error("NTRIP", "Idle Timeout.")
                            payload = {"reason": "idle_timeout", "idle_ms": idle_ms,
                                       "timeout_sec": CONFIG["ntrip_idle_timeout_sec"]}
                            payload.update(self._attempt_timing())
                            field_recorder.event("ntrip_end", payload)
                            outcome = "idle_timeout"
                            break
                        # A VRS caster may wait for the next position before
                        # resuming corrections. Keep GGA alive during silence.
                        await self._gga_due(writer)
                        if machine.reload_requested and self._reload_ends_attempt(endpoint):
                            reloaded = True
                            break
                        continue

                    self._flow_duration("read_wait", started)
                    if machine.reload_requested and self._reload_ends_attempt(endpoint):
                        reloaded = True
                        break
                    if not data:
                        payload = {"reason": "eof"}
                        payload.update(self._attempt_timing())
                        field_recorder.event("ntrip_end", payload)
                        log("INFO", "NTRIP", "Stream ended (EOF).")
                        outcome = "eof"
                        break

                    app.stats["last_ntrip_error_code"] = None
                    last_data_ticks_ms = time.ticks_ms()
                    machine.ok_data(last_data_ticks_ms)
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
                    # detour to the module. Corrections go through the one UART writer;
                    # it completes partial writes and keeps receiver commands out of an
                    # open frame. After waiting for it, shutdown and reload come first.
                    self._flow_phase("uart")
                    if not self.tx.write_rtcm(data):
                        await self.tx.drain_rtcm()
                        if shutdown_event.is_set():
                            break
                        if machine.reload_requested and self._reload_ends_attempt(endpoint):
                            reloaded = True
                            break
                    self.rtcm.feed(data)
                    field_recorder.observe_rtcm(data)
                    if not diagnostic_data_seen:
                        diagnostic_data_seen = True
                        payload = {"endpoint": endpoint["label"]}
                        payload.update(self._attempt_timing())
                        field_recorder.event("ntrip_first_data", payload)
                    end = machine.check_stale(time.ticks_ms())
                    if end:
                        end.update(self._attempt_timing())
                        field_recorder.event("ntrip_end", end)
                        outcome = "stale_backlog"
                        break

                    await self._gga_due(writer)
                    if machine.reload_requested and self._reload_ends_attempt(endpoint):
                        reloaded = True
                        break

            except Exception as e:
                # Three separate cases: shutdown wins (the error is recorded as on every
                # shutdown, nothing is counted), then a reload that ends the attempt (no
                # error at all), then the error accounting.
                if shutdown_event.is_set():
                    pass
                elif machine.reload_requested and self._reload_ends_attempt(endpoint):
                    reloaded = True
                elif outcome is None:
                    outcome = machine.error_code(e)
                if not reloaded:
                    payload = {"endpoint": endpoint["label"], "error_type": type(e).__name__,
                               "errno": e.args[0] if e.args and type(e.args[0]) is int else None}
                    payload.update(self._attempt_timing())
                    field_recorder.event("ntrip_error", payload)
                    error_msg = str(e)
                    app.stats["last_ntrip_error"] = error_msg
                    _log_unexpected_error(e, error_msg)
                    app.log_error("NTRIP", error_msg)
            finally:
                machine.clear_stale()
                self._flow_phase("closing")
                # Every stream end is a frame boundary: waiting commands may go out.
                self.tx.end_stream()
                self._attempt_writer = self._attempt_endpoint = None
                # The service CRS belongs to this stream only (#38b E7).
                self.rtcm.drop_crs()
                if writer:
                    try:
                        writer.close()
                        await writer.wait_closed()
                    except Exception:
                        pass

                # No outcome: the attempt was shut down or cancelled (a BaseException);
                # neither counts nor backs off. A reload ends it without either, and the
                # next attempt starts at once with the new endpoints.
                if reloaded and not shutdown_event.is_set():
                    payload = {"reason": "reload"}
                    payload.update(self._attempt_timing())
                    field_recorder.event("ntrip_end", payload)
                elif outcome is not None and not shutdown_event.is_set():
                    wartezeit = machine.settle(outcome, permanent_error)
                    field_recorder.event("ntrip_backoff", {"delay_sec": wartezeit})
                    self._flow_phase("backoff")
                    log("INFO", "NTRIP", "Retry in %ss (attempt %s)..."
                        % (wartezeit, machine.retry_count))
                    # Sleep in slices so that the watchdog gets further signs of life
                    # during long breaks.
                    geschlafen = 0
                    while (geschlafen < wartezeit and not shutdown_event.is_set()
                           and not machine.reload_requested):
                        await asyncio.sleep(min(5, wartezeit - geschlafen))
                        geschlafen += 5
                        app.beat("ntrip")
        self._flow_phase("stopped")
        log("INFO", "NTRIP", "NTRIP client task stopped.")
