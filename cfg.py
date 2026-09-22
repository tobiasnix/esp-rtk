# SPDX-License-Identifier: AGPL-3.0-only
"""Configuration, logging, and input validation.

This lowest application layer has no dependency on other application modules.
Splitting the firmware into modules originally reduced MicroPython compilation
memory from 172 KB to 70 KB on hardware without PSRAM. The modular structure is
still retained for maintainability and compatibility with constrained builds.
"""
import ujson  # compatibility: host tests and support tools patch cfg.ujson
import cfg_validation as _validation
import cfg_boot as _boot
import cfg_factory as _factory
import cfg_store as _store
import cfg_logging as _logging

validate_wifi_networks = _validation.validate_wifi_networks


# ---------------------------------------------------------------------------
# CONFIGURATION & CONSTANTS
# ---------------------------------------------------------------------------

CONFIG = {
    "version": "ESP-RTK V15.8.0",
    "config_schema_version": 3,

    "config_file": "config.json",

    # WLAN & HW
    # Access data is intentionally not in the code, but in /config.json on the board
    # (set via the config page or mpexec.py) - otherwise they end up in the repository.
    "wifi_ssid": "", "wifi_pass": "",
    # An RTK rover moves between home, auto hotspot and field; without the list, any
    # change of location would mean: connect to the configuration AP and reconfigure.
    "wifi_networks": [],
    "uart_id": 1, "uart_baud": 115200, "tx_pin": 18, "rx_pin": 17, "http_port": 80,
    "uart_rxbuf_bytes": 16384,
    "nmea_queue_size": 512,
    "status_led_pin": 2,
    # ble_mtu_size is only the starting value until negotiation; ble_mtu_max is offered
    # to the client.
    "ble_mtu_size": 20,
    "ble_mtu_max": 247,

    # 10110 is the registered NMEA-0183 port. 2947 remains as a compatibility listener
    # because previous installations and diagnostic instructions use it.
    "nmea_tcp_port": 10110,
    "nmea_tcp_legacy_port": 2947,
    "nmea_tcp_legacy_enabled": True,
    "nmea_tcp_max_clients": 4,
    # Require the stream token before the first NMEA byte. This is intentionally on
    # by default; disabling it is only for trusted isolated networks and legacy clients.
    "nmea_tcp_auth_required": True,

    # AP configuration ATTENTION: The STA network must NOT be in the same /24 as ap_ip,
    # otherwise the AP recognition over the client subnetwork will not work.
    "ap_ssid": "ESP-RTK-Setup",
    # Blank = create a random one on first boot and write it in config.json. A fixed
    # password would be the same on any device; if you are in radio range, you could
    # reconfigure the device. The generated password is on the serial console when
    # booted.
    "ap_pass": "",
    "ap_ip": "192.168.4.1",
    # How often the AP may be disabled for an STA attempt. This avoids the single-radio
    # channel lock, but temporarily interrupts access to the configuration page.
    #
    # 0 means: the AP has priority. It is then FIRST started - is therefore after a good
    # second instead of ten to fifteen - and never switched off. Price: if the AP is on
    # a different channel than the router, the station may never find it, and without
    # STA there is no correction data. For a device that should be accessible above all,
    # this is the right exchange; for one that RTK should deliver, not.
    "ap_channel_switch_interval_sec": 60,

    # NTRIP
    "ntrip_enabled": True,
    "ntrip_host": "", "ntrip_port": 2101,
    "ntrip_mount": "", "ntrip_user": "", "ntrip_pass": "",
    # Optional independent fallback caster. Empty host/mount keeps it disabled.
    "ntrip_fallback_host": "", "ntrip_fallback_port": 2101,
    "ntrip_fallback_mount": "", "ntrip_fallback_user": "",
    "ntrip_fallback_pass": "",
    "ntrip_read_timeout_sec": 5, "ntrip_idle_timeout_sec": 15,
    "ntrip_connect_timeout_sec": 15, "ntrip_header_timeout_sec": 10,
    # Delay between attempts doubles up to ntrip_retry_max_sec.
    "ntrip_retry_delay_sec": 5, "ntrip_retry_max_sec": 300,
    "ntrip_max_retries": 0,
    # Physical reference stations may not need this; VRS and near-mount points
    # commonly require periodic rover positions.
    "ntrip_gga_interval_sec": 10,

    # Field measurement acceptance gate. These defaults preserve the established
    # RTK-only behaviour and may be changed from the authenticated V2 settings page.
    "measurement_required_fix": "RTK_FIXED",
    "measurement_min_satellites": 10,
    "measurement_max_hdop": 1.5,
    "measurement_max_correction_age_sec": 5.0,
    "measurement_max_horizontal_spread_m": 0.05,
    "measurement_max_gst_horizontal_sigma_m": 0.05,
    "measurement_max_gst_vertical_sigma_m": 0.10,
    # Rolling sample window used by the open-ended "quality OK" measurement.
    "measurement_quality_window_samples": 5,
    # 2 sets the header "Ntrip version: Ntrip/2.0" - some casters demand it, and the
    # search for an MSM4/MSM7 caster leads to external networks.
    "ntrip_version": 1,

    # System
    # wdt_enabled=False for development on the REPL: The ESP32-WDT can no longer be
    # disabled after activation and resets the board about 15 seconds after Ctrl-C.
    "wdt_enabled": True,
    "wdt_timeout": 15000, "gc_interval_ms": 10000, "fix_update_interval_ms": 950,
    # 0 = only monitor the event loop (behavior up to V9.3). Otherwise, each long-running
    # long-running task must report a heartbeat within this period.
    "heartbeat_timeout_sec": 60,
    # log_level filtert nach Schwere (DEBUG/INFO/WARN/ERROR), log_mute nach
    # subsystem. GC is muted by default because its ten-second message is not useful in
    # normal operation.
    "log_level": "INFO",
    "log_mute": "GC",
    # Network name of the device: reachable as <hostname>.local, then you do not have to
    # search for the IP.
    "hostname": "",
    # Obsolete fields are only read/emptied, but never used for authorization, so an old
    # config.json remains bootable.
    "status_token": "",
    "config_token": "",
    # Without them, time.time() counts from the year 2000 and log lines can not be
    # classified.
    "ntp_sync": True,
    "gsv_limit_per_sec": 8,
    # Versioned device telemetry in the normal NMEA fan-out. 0 = off.
    "telemetry_interval_sec": 1,
    # BLE-NUS is a read-only transport for Terranaut. Commands to the GNSS module are
    # enabled only through the authenticated HTTP interface, never permanently by radio.
    "ble_commands_enabled": False,
}

# Snapshot of the defaults (reference for persistence and type casting)
DEFAULT_CONFIG = CONFIG.copy()

# These keys are never written/loaded in config.json (otherwise an old config.json pins
# the old version string after a firmware update). wdt_enabled is included so that a
# malformed config.json cannot permanently turn off the watchdog; development mode is
# deliberately set only in the code.
PERSIST_EXCLUDE = ("version", "config_file", "wdt_enabled",
                   "ble_commands_enabled")

PENDING_CONFIG_FILE = "config.pending.json"
LAST_GOOD_CONFIG_FILE = "config.last_good.json"
CONFIG_RESULT_FILE = "config.apply.result.json"
LANGUAGE_MIGRATION_MARKER = ".v11-english-history-migrated"

# Until V9.3, the entire DEFAULT_CONFIG applied: a self-made POST could thus adjust
# tx_pin, uart_id or http_port and configure the board unbootable - recovery only via
# the REPL.
SETTABLE_KEYS = ("wifi_ssid", "wifi_pass", "ntrip_enabled",
                 "ntrip_host", "ntrip_port", "ntrip_mount", "ntrip_user",
                 "ntrip_pass", "ntrip_fallback_host", "ntrip_fallback_port",
                 "ntrip_fallback_mount", "ntrip_fallback_user",
                 "ntrip_fallback_pass")

# Password fields: blank means "leave unchanged" because the config page does not return
# them.
SECRET_KEYS = ("wifi_pass", "ntrip_pass", "ntrip_fallback_pass")

# Boot accounting: Counts consecutive starts. If the SAFE_MODE_BOOTS device does not go
# beyond SAFE_MODE_UPTIME_SEC in succession, it starts with a slimmed down - AP and HTTP
# remain accessible so that you can always get to the configuration again.
BOOTCOUNT_FILE = "bootcount.json"

SAFE_MODE_BOOTS = 5

SAFE_MODE_UPTIME_SEC = 300

RESET_CAUSES = {1: "PWRON", 2: "HARD", 3: "WDT", 4: "DEEPSLEEP", 5: "SOFT"}

# Only these causes count on the safe mode. PWRON means "someone plugged in", SOFT means
# "soft reboot in the REPL" - both are intentional and must not drive the device into
# the safe mode if it happens several times in a row.
#
# ATTENTION: machine.reset() logs on the ESP32-S3 as HARD (2), so just like a panic
# reset. The cause alone is therefore not enough to distinguish intention from crash -
# callers that restart intentionally first leave a mark with mark_planned_reset().
CRASH_RESET_CAUSES = (2, 3)

PLANNED_RESET_FILE = "planned.reset"

# Factory reset via the BOOT button (GPIO0).
#
# Consciously have a fixed, well-known password: if you've been physically on the
# device, you're supposed to come back without a computer. This is a balance, not a
# negligence: the same password is in the public repository and is therefore known to
# everyone in the radio range once someone has pressed the button, so the device warns
# whenever it starts, as long as it's running.
#
# The button has to be pressed AFTER it is switched on: if you hold it when the voltage
# is applied, the chip starts in the ROM bootloader and MicroPython does not even run.
FACTORY_AP_SSID = "ESP-RTK-Setup"
FACTORY_AP_PASS = "12345678"
FACTORY_RESET_PIN = 0
SETUP_MODE_HOLD_MS = 3000
FACTORY_RESET_HOLD_MS = 10000
FACTORY_RESET_WINDOW_MS = 12000

# Hardware values such as tx_pin remain - they have nothing to do with access data and a
# wrong pin makes the device unusable.
FACTORY_CLEAR_KEYS = ("wifi_ssid", "wifi_pass", "wifi_networks",
                      "ntrip_user", "ntrip_pass", "ntrip_fallback_user",
                      "ntrip_fallback_pass",
                      "config_token", "status_token")

# Only these tasks are allowed to be monitored by the watchdog, they have a loop with
# limited waiting time and therefore reliably give a sign of life, whereas the BLE
# transmitter is hanging on a queue that is legitimately allowed to remain empty (not
# connected to a GNSS module), and otherwise would trigger a false reset, its signs of
# life are still in /status.
WATCHED_TASKS = ("gnss", "ntrip", "network")

FIX_STATUS = {
    0: "NO_FIX", 1: "GPS_FIX_2D/3D", 2: "DGPS/SBAS", 4: "RTK_FIXED",
    5: "RTK_FLOAT", 6: "ESTIMATED", 7: "MANUAL", 8: "SIMULATION"
}

# LC29H-DA firmware NR11A04S provides GST when explicitly enabled. Keep the
# sentence in the transport whitelist so BLE and TCP clients receive the
# receiver's error estimates; gnss.py enables GST and tracking evaluates it.
NMEA_WHITELIST = ("GGA", "RMC", "VTG", "GSA", "GSV", "GST")

# Severity and subsystem are separate. Until V9.3, both were in ONE table: "GC" was on
# the same rank as INFO, so the 10-second spam could only be switched off by losing all
# INFO messages.
SEVERITIES = {"DEBUG": 0, "INFO": 1, "WARN": 2, "ERROR": 3}

TAGS = ("SYS", "WIFI", "NTRIP", "GNSS", "RTCM", "BLE", "HTTP", "GC")

# Persistent error log: only WARN and ERROR, large-scale. The RAM ring buffer in
# AppState does not survive a reset - and exactly after a reset, you want to know what
# was going on before.
ERRORLOG_FILE = "errors.log"
ERRORLOG_MAX_BYTES = 8192

# Flight recorder in RTC memory, which survives on the ESP32 every reset except power
# failure - measured on the device against a hard reset - and costs no flash wear, so
# that's where the full history including INFO ends up, while only starters and errors
# go into the flash. After a watchdog reset, you can see what the device did immediately
# before, instead of just the last error message.
RTC_LOG_MAX_BYTES = 1900    # remain among the 2048 of the RTC memory

# Separator between two sessions in the RTC buffer.
RTC_TRENNER = "----- Restart -----"

_logger = _logging.Logger(CONFIG, SEVERITIES, ERRORLOG_FILE,
                          ERRORLOG_MAX_BYTES, RTC_LOG_MAX_BYTES)

# ---------------------------------------------------------------------------
# ZENTRALE LOGGING FUNKTION
# ---------------------------------------------------------------------------

def flight_recorder_reset():
    """Reconnect the RTC recorder and adopt the previous session."""
    _logger.reset()


def flight_recorder_adopt():
    """Adopt the RTC contents and begin a new logical session."""
    _logger.adopt()


def previous_flight_recorder():
    return _logger.previous


def read_flight_recorder():
    return "\n".join(_logger.lines[_logger.start:])


def save_flight_recorder():
    _logger.save_previous()


def flight_recorder_start_new():
    """Discard the recorder prefix (test and migration helper)."""
    _logger.start_new()


def errorlog_reset_state():
    """Reset persistent-log repetition throttling (test helper)."""
    _logger.reset_error_state()


def log(sev, tag, message, print_traceback=False, exception=None):
    return _logger.log(sev, tag, message, print_traceback, exception)


def log_boot(text):
    return _logger.log_boot(text)


def read_error_log():
    return _logger.read_error()

def error_log_entries():
    return _logger.count_entries()


def incremental_error_log(cursor=None, limit=200, level=None):
    return _logger.read_entries(cursor, limit, level)
def _coerce(key, value):
    """Cast a value (possibly present as a string) to the type of default value. Required, because HTTP-POST delivers everything as a string and can contain older config.json files (V9.0) string ports."""
    return _store.coerce(DEFAULT_CONFIG, key, value)

def load_config():
    """Loads the configuration from config.json and merges it into CONFIG."""
    _store.load(CONFIG, DEFAULT_CONFIG, PERSIST_EXCLUDE, save_config, log)

def _persisted_config(overrides=None):
    return _store.persisted(CONFIG, DEFAULT_CONFIG, PERSIST_EXCLUDE, overrides)


def _atomic_dump(target, data):
    """Write JSON atomically without destroying an existing file."""
    return _store.atomic_dump(target, data, log)


def save_config(new_data=None):
    """Saves the current CONFIG (without PERSIST_EXCLUDE) in config.json."""
    if new_data:
        CONFIG.update(new_data)

    data_to_save = _persisted_config()

    # Atomar write: first completely into a side file, then renam. open(..., "w") on the
    # original truncated immediately - a brownout in the middle of the dump() left a
    # half file, load_config() fell silently back to the defaults and the device was
    # without Wi-Fi and without NTRIP, without any of it anywhere.
    target = CONFIG["config_file"]
    if _atomic_dump(target, data_to_save):
        log("INFO", "SYS", "Configuration saved to %s." % target)
        return True
    return False


def pending_config_active():
    return _store.pending_active(PENDING_CONFIG_FILE)


def stage_config(new_data):
    """Secure candidates; NetworkManager confirms or rolls back."""
    return _store.stage(CONFIG, DEFAULT_CONFIG, PERSIST_EXCLUDE, new_data,
                        LAST_GOOD_CONFIG_FILE, PENDING_CONFIG_FILE,
                        _atomic_dump, config_result_write)


def config_result_write(status, message=None, details=None):
    """Persist the machine-readable outcome of the latest config attempt.

    ``message`` is accepted temporarily at call sites but deliberately not
    stored: presentation belongs to the browser translation dictionary.
    """
    return _store.result_write(CONFIG_RESULT_FILE, _atomic_dump, status, details)


def config_result_read():
    return _store.result_read(CONFIG_RESULT_FILE)


def commit_pending_config(active_ssid=None):
    """A demonstrably achievable candidate configuration."""
    return _store.commit(CONFIG, PENDING_CONFIG_FILE, LAST_GOOD_CONFIG_FILE,
                         log, config_result_write, active_ssid)


def rollback_pending_config():
    """Restore the last working configuration."""
    return _store.rollback(CONFIG, DEFAULT_CONFIG, PERSIST_EXCLUDE,
                           LAST_GOOD_CONFIG_FILE, PENDING_CONFIG_FILE,
                           _atomic_dump, commit_pending_config, log,
                           config_result_write)

# ---------------------------------------------------------------------------
# REGISTRATION FOR THE CONFIGSIDE
# ---------------------------------------------------------------------------

def html_escape(value):
    """Escape a value for safe use in HTML attributes.

    Without escaping, a quote in an SSID, host, or mountpoint could terminate
    the attribute and inject markup into the setup page.
    """
    return _validation.html_escape(value)

def _has_control_characters(text):
    return _validation.has_control_characters(text)

def validate_settings(raw):
    """Validate and normalize values accepted by the setup endpoint."""
    return _validation.validate_settings(raw, SETTABLE_KEYS, SECRET_KEYS)

def configured_networks():
    """All configured WLAN networks as [(ssid, password), ...). The prime network (wifi_ssid) is in front and wins with double SSID - it is what the config page edits.
    """
    return _validation.configured_networks(CONFIG)


def configured_networks_text(networks):
    """The config page basically does not give passwords back; whoever wants to change one writes 'ssid:password', who only leaves the name, keeps the stored one.
    """
    return _validation.configured_networks_text(networks)


def parse_configured_networks(text, bestehend):
    """Text field -> List. One line per network, 'ssid' or 'ssid:password'."""
    return _validation.parse_configured_networks(text, bestehend)


def is_factory_password(password):
    """True, if the AP password is the publicly known factory password."""
    return bool(password) and password == FACTORY_AP_PASS


def button_held(samples, required):
    """True, if the required measurements key was pressed on the piece, separate from the hardware to make the decision testable. Rugged = 0 (the key pulls against ground), released = 1.
    """
    return _factory.button_held(samples, required)


def factory_reset():
    """Deletes operational data, but retains identity and code, so that the physical QR label remains valid even after a factory reset. The runtime then sets AP name and password again from the unchangeable identity.
    """
    return _factory.factory_reset(CONFIG, DEFAULT_CONFIG, FACTORY_CLEAR_KEYS,
                                  save_config, log)


def generate_ap_password():
    """Generates an AP password. 12 hex characters from os.urandom, 48 bits. The preefix makes it recognizable in config.json and on the console. Why not derive it from the MAC (see derive_ap_password): the AP-MAC is the BSSID and is in every Wi-Fi beacon. Since the procedure is described in the public repository, anyone could calculate the password in radio range. A random value costs nothing in retrieval - it is on the serial console at the first start anyway and then in config.json.
    """
    return _factory.generate_ap_password()


def derive_ap_password(mac):
    """The old password derived from the MAC. Only there to recognize and convert existing devices - see is_derived_password() for new password generate_ap_password() use.
    """
    return _factory.derive_ap_password(mac)


def is_derived_password(password, mac):
    """Return whether a password was derived by the deprecated MAC-based scheme.

    Such passwords can be reconstructed from the broadcast BSSID and are
    replaced with a random password on the next boot.
    """
    return _factory.is_derived(password, mac)

# ---------------------------------------------------------------------------
# SELBSTHEILUNG: BACKOFF, BOOT-COUNTER, RESET-URSACHE
# ---------------------------------------------------------------------------

def backoff_delay(attempt, base, cap):
    """The fixed 5-s clock meant that if the password was incorrect, the NTRIP client would reconnect forever every 5 s and risk a lock on the caster.
    """
    return _boot.backoff_delay(attempt, base, cap)

def reset_cause_name(code):
    return _boot.reset_cause_name(code, RESET_CAUSES)

def mark_planned_reset():
    """Marks that the following restart is intention."""
    _boot.mark_planned_reset(PLANNED_RESET_FILE)


def bump_boot_count(ursache=None):
    """In the event of a harmless cause (plugging in, planned reset), the numerator is deleted and 0 is returned - a series of crashes is thus considered interrupted.
    """
    return _boot.bump_boot_count(ursache, BOOTCOUNT_FILE,
                                 PLANNED_RESET_FILE, CRASH_RESET_CAUSES)

def reset_boot_count():
    """Mark the boot as successful after sufficient stable runtime."""
    _boot.reset_boot_count(BOOTCOUNT_FILE)

def safe_mode_active(boot_count):
    return _boot.safe_mode_active(boot_count, SAFE_MODE_BOOTS)

# ---------------------------------------------------------------------------
# HTTP SERVER
# ---------------------------------------------------------------------------

def _subnet_prefix(ip):
    """'192.168.4.1' -> '192.168.4.' ; ''for unusable inputs."""
    return _validation.subnet_prefix(ip)

def _same_subnet(a, b):
    return _validation.same_subnet(a, b)


def migrate_v11_runtime_history():
    """Delete pre-V11 localized history exactly once.

    The marker is created only after the cleanup attempts. A failed marker
    write therefore safely retries on the next boot instead of silently
    preserving mixed-language runtime history.
    """
    return _store.migrate_runtime_history(
        LANGUAGE_MIGRATION_MARKER,
        (ERRORLOG_FILE, ERRORLOG_FILE + ".1", CONFIG_RESULT_FILE), _logger)


# In V9.0, load_config() only ran in the NetworkManager task and all hardware parameters
# from config.json were ignored.
flight_recorder_reset()
migrate_v11_runtime_history()
load_config()
