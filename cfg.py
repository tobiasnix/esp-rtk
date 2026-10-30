# SPDX-License-Identifier: AGPL-3.0-only
"""Configuration, logging, and input validation.

This lowest application layer has no dependency on other application modules.
Splitting the firmware into modules originally reduced MicroPython compilation
memory from 172 KB to 70 KB on hardware without PSRAM. The modular structure is
still retained for maintainability and compatibility with constrained builds.
The survey store is a package `tracking/` for the same reason.
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
    "version": "ESP-RTK V16.0.0",
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
    # Correction age from the GNSS epochs inside the RTCM stream (not settable from
    # the UI). Older than ntrip_max_epoch_age_sec counts as stale; a positive
    # ntrip_reconnect_on_stale_sec reconnects after that long without fresh epochs
    # (0 = never). gps_utc_offset_sec is the GPS-UTC leap second count.
    "ntrip_max_epoch_age_sec": 15,
    "ntrip_reconnect_on_stale_sec": 0,
    "gps_utc_offset_sec": 18,

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
    # Device-wide measurement method for new measurements: 1, 5 or 10 averaged
    # samples, or "quality" (open-ended until the gate holds for
    # measurement_quality_window_samples). Not a gate limit: never part of a profile ID.
    "measurement_samples": 5,
    # Survey metadata stored with every measurement. Empty frame text means "not
    # configured" and is stored as null. antenna_height_m is the distance from the
    # ground point up to the antenna phase centre (pole length plus the antenna's
    # ARP-to-phase-centre offset); the Field setup card in the settings sets it.
    # The reference_frame keys are not exposed in the UI until issue #8.
    "reference_frame": "",
    "reference_frame_epoch": "",
    "antenna_height_m": 0.0,
    # Stakeout: the horizontal tolerance of a confirmation, and the distance from which
    # the fine view and the quality window apply (tolerance < fine radius). The window
    # size is measurement_quality_window_samples.
    "stakeout_tolerance_m": 0.1,
    "stakeout_fine_radius_m": 5.0,
    # 2 sets the header "Ntrip version: Ntrip/2.0" - some casters demand it, and the
    # search for an MSM4/MSM7 caster leads to external networks.
    "ntrip_version": 1,

    # System
    # wdt_enabled=False for development on the REPL: The ESP32-WDT can no longer be
    # disabled after activation and resets the board about 15 seconds after Ctrl-C.
    "wdt_enabled": True,
    "wdt_timeout": 15000, "gc_interval_ms": 10000, "fix_update_interval_ms": 950, "fix_stale_after_ms": 3000,
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
    # Obsolete field, only emptied, never used for authorization, so an old config.json
    # remains bootable. (status_token is gone: an old key is ignored on load and
    # dropped by the next save.)
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

# Stored values load_config rejected (key, message); reported by /status.
CONFIG_WARNINGS = []

PENDING_CONFIG_FILE = "config.pending.json"
LAST_GOOD_CONFIG_FILE = "config.last_good.json"
CONFIG_RESULT_FILE = "config.apply.result.json"
# Random per boot, RAM only: tells clients whether the device restarted and
# which boot wrote a configuration result (boot_count is no identity, #19b).
BOOT_ID = _factory.new_boot_id()
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

# Apply policy: when does a changed value take effect?
#   live            - read at runtime; takes effect immediately, no restart.
#   task_restart    - takes effect when the owning task reloads (NTRIP client), otherwise
#                     after the next restart.
#   verified_reboot - changes an access path or a boot-time resource; held back until the
#                     user applies it, then confirmed by a reboot and rolled back if the
#                     device does not come back. Only these keys are ever rolled back.
APPLY_LIVE = "live"
APPLY_TASK_RESTART = "task_restart"
APPLY_VERIFIED_REBOOT = "verified_reboot"

_LIVE_KEYS = (
    "ble_mtu_size", "nmea_tcp_max_clients", "nmea_tcp_auth_required",
    "ap_channel_switch_interval_sec", "ntrip_read_timeout_sec",
    "ntrip_idle_timeout_sec", "ntrip_connect_timeout_sec",
    "ntrip_header_timeout_sec", "ntrip_retry_delay_sec", "ntrip_retry_max_sec",
    "ntrip_max_retries", "ntrip_gga_interval_sec", "ntrip_max_epoch_age_sec",
    "ntrip_reconnect_on_stale_sec", "gps_utc_offset_sec",
    "measurement_required_fix", "measurement_min_satellites",
    "measurement_max_hdop", "measurement_max_correction_age_sec",
    "measurement_max_horizontal_spread_m", "measurement_max_gst_horizontal_sigma_m",
    "measurement_max_gst_vertical_sigma_m", "measurement_quality_window_samples",
    "measurement_samples", "reference_frame", "reference_frame_epoch", "antenna_height_m",
    "stakeout_tolerance_m", "stakeout_fine_radius_m", "ntrip_version", "gc_interval_ms",
    "fix_update_interval_ms", "fix_stale_after_ms",
    "log_level", "log_mute", "config_token", "ntp_sync", "gsv_limit_per_sec",
    "telemetry_interval_sec", "ble_commands_enabled")

_TASK_RESTART_KEYS = (
    "ntrip_enabled", "ntrip_host", "ntrip_port", "ntrip_mount", "ntrip_user",
    "ntrip_pass", "ntrip_fallback_host", "ntrip_fallback_port",
    "ntrip_fallback_mount", "ntrip_fallback_user", "ntrip_fallback_pass")

_VERIFIED_REBOOT_KEYS = (
    "config_schema_version", "wifi_ssid", "wifi_pass", "wifi_networks", "uart_id",
    "uart_baud", "tx_pin", "rx_pin", "http_port", "uart_rxbuf_bytes",
    "nmea_queue_size", "status_led_pin", "ble_mtu_max", "nmea_tcp_port",
    "nmea_tcp_legacy_port", "nmea_tcp_legacy_enabled", "ap_ssid", "ap_pass", "ap_ip",
    "wdt_enabled", "wdt_timeout", "heartbeat_timeout_sec", "hostname")

APPLY_POLICY = {}
for _keys, _policy in ((_LIVE_KEYS, APPLY_LIVE),
                       (_TASK_RESTART_KEYS, APPLY_TASK_RESTART),
                       (_VERIFIED_REBOOT_KEYS, APPLY_VERIFIED_REBOOT)):
    for _key in _keys:
        APPLY_POLICY[_key] = _policy
del _keys, _policy, _key
VERIFIED_REBOOT_KEYS = _VERIFIED_REBOOT_KEYS

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
                      "config_token")

# Only these tasks are allowed to be monitored by the watchdog, they have a loop with
# limited waiting time and therefore reliably give a sign of life, whereas the BLE
# transmitter is hanging on a queue that is legitimately allowed to remain empty (not
# connected to a GNSS module), and otherwise would trigger a false reset, its signs of
# life are still in /status. `http` and `nmea_tcp` are deliberately not watched either:
# their loop only sleeps; a server failure raises and supervise() restarts it (counted in
# task_restarts).
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

# config.json existed at load but could not be read: CONFIG holds defaults, so
# no automatic write (generated AP password, rollback) may replace the file in
# this boot - a transient read error would otherwise wipe the configuration,
# Wi-Fi access included. Only a whole, deliberate configuration write releases
# it (PUT /api/config, /save, restore, apply, factory reset); the live PUTs
# are refused meanwhile.
_CONFIG_FILE_PROTECTED = False
# True when the protection holds only defaults in RAM (nothing of config.json
# was loaded); False when a candidate was read in place after a failed recovery.
_CONFIG_DEFAULTS_ONLY = False


def _check_stakeout_pair(config, warnings):
    """Loader hook: a tolerance not below the fine radius resets both stakeout keys."""
    error = _validation.stakeout_pair_error(config["stakeout_tolerance_m"],
                                            config["stakeout_fine_radius_m"])
    if not error:
        return
    for key in _validation.STAKEOUT_SETTINGS_KEYS:
        config[key] = DEFAULT_CONFIG[key]
        warnings.append({"key": key, "message": error})


def load_config():
    """Loads the configuration from config.json and merges it into CONFIG."""
    global _CONFIG_FILE_PROTECTED, _CONFIG_DEFAULTS_ONLY
    del CONFIG_WARNINGS[:]
    _CONFIG_FILE_PROTECTED = False         # a migration save inside the load may write
    loaded = _store.load(CONFIG, DEFAULT_CONFIG, PERSIST_EXCLUDE, save_config, log,
                         _validation.LOAD_VALIDATORS, CONFIG_WARNINGS, _check_stakeout_pair)
    _CONFIG_DEFAULTS_ONLY = loaded is False
    if not loaded:
        _protect_config_file()


def config_file_protected():
    """True while an unreadable config.json must not be overwritten (this boot)."""
    return _CONFIG_FILE_PROTECTED


def _protect_config_file():
    """Protect config.json; listed in config_warnings (/status, /api/ui/live)."""
    global _CONFIG_FILE_PROTECTED
    _CONFIG_FILE_PROTECTED = True
    if not any(w.get("key") == "config_file" for w in CONFIG_WARNINGS):
        CONFIG_WARNINGS.append({"key": "config_file", "message": (
            "%s could not be read or recovered at start-up; it is kept and not overwritten until "
            "the whole configuration is saved." % CONFIG["config_file"])})


def _release_config_file(source="A user change"):
    """A deliberate whole configuration write may replace config.json; True if it was protected."""
    global _CONFIG_FILE_PROTECTED
    if not _CONFIG_FILE_PROTECTED:
        return False
    _CONFIG_FILE_PROTECTED = False
    CONFIG_WARNINGS[:] = [w for w in CONFIG_WARNINGS if w.get("key") != "config_file"]
    log("WARN", "SYS", "%s replaces the unreadable %s." % (source, CONFIG["config_file"]))
    return True

def _persisted_config(overrides=None):
    return _store.persisted(CONFIG, DEFAULT_CONFIG, PERSIST_EXCLUDE, overrides)


def _atomic_dump(target, data):
    """Write JSON atomically without destroying an existing file."""
    if _CONFIG_FILE_PROTECTED and target == CONFIG["config_file"]:
        log("WARN", "SYS", "%s was unreadable at start-up; it is not overwritten before "
                           "a user change." % target)
        return False
    return _store.atomic_dump(target, data, log)


def save_config(new_data=None):
    """Saves the current CONFIG (without PERSIST_EXCLUDE) in config.json."""
    # CONFIG only keeps new_data if the write succeeds: a device must not run on
    # a value that a reboot would lose (security settings such as
    # nmea_tcp_auth_required included).
    unset = object()
    previous = dict((key, CONFIG.get(key, unset)) for key in (new_data or ()))
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
    for key, value in previous.items():
        if value is unset: CONFIG.pop(key, None)
        else: CONFIG[key] = value
    return False


# A rollback that was started but not completed stays open in RAM (with its
# first cause), whether or not `failed` could be persisted (invariant I2).
_ROLLBACK_OPEN = False
_ROLLBACK_CODE = None
# The last recovery of the transaction files failed: the change counts as open
# and blocked until a later recovery succeeds (invariant I3).
_STAGING_UNRECOVERED = False
# pending_config_active() runs on every request and network pass; a lasting
# recovery error is logged once, then at most once a minute with a count.
_STAGING_LOG = _logging.LogThrottle(_logging.HOT_PATH_LOG_INTERVAL_MS)


def pending_config_active():
    """True while a configuration change is open; recovers the files first.

    A failed recovery never raises and counts as open, so every change stays
    refused until the files are back.
    """
    global _STAGING_UNRECOVERED
    if not _store.recover_staging(PENDING_CONFIG_FILE, LAST_GOOD_CONFIG_FILE):
        _STAGING_UNRECOVERED = True
        n = _STAGING_LOG.hit()
        if n:
            log("ERROR", "SYS", "Configuration staging files could not be recovered; changes "
                                "stay blocked.%s" % _logging.throttle_note(n))
        return True
    _STAGING_UNRECOVERED = False
    return _store.pending_active(PENDING_CONFIG_FILE)


def rollback_incomplete():
    """A rollback decision is stored (`failed`) but its pending file still exists.

    An unreadable result (`unknown`) may hide that decision and counts as open,
    as in commit_pending_config().
    """
    try:
        return bool(pending_config_active()
                    and config_result_read().get("status") in ("failed", "unknown"))
    except Exception as exc:
        log("ERROR", "SYS", "Rollback state unreadable; treated as open: %s" % exc)
        return True


def rollback_open(files=True):
    """True while a rollback is open in RAM, on disk, or the files are unrecovered.

    files=False skips the file view, for a caller that has just found no
    pending file with pending_config_active() (one recovery per request).
    """
    try:
        # The file view first: it runs the recovery that refreshes _STAGING_UNRECOVERED.
        incomplete = rollback_incomplete() if files else False
        return bool(incomplete or _ROLLBACK_OPEN or _STAGING_UNRECOVERED)
    except Exception:
        return True


# Changed verified_reboot keys wait here, in RAM only, until they are applied;
# a reboot before the apply drops the staged change.
_STAGED = {}
# Counts the content changes of _STAGED (RAM only); staging_revision() names them.
_STAGING_SERIAL = 0
# Saved task_restart values not yet handed to a running task.
_RESTART_REQUIRED = False
# Takes saved task_restart values into the running task (main.py: NtripClient.reload);
# None in safe mode and in tests.
_TASK_RELOADER = None


def set_task_reloader(fn):
    """Register the reload of the running task (no arguments, truthy once taken over).

    None removes it.
    """
    global _TASK_RELOADER
    _TASK_RELOADER = fn


def _request_task_reload():
    """Hand all saved task_restart values to the running task; True on success.

    A success clears the restart flag, also of an earlier failed handover.
    Without a reloader, or when it fails, the flag stays.
    """
    global _RESTART_REQUIRED
    if _TASK_RELOADER is None:
        return False
    try:
        if not _TASK_RELOADER():
            return False
    except Exception as exc:
        log("ERROR", "SYS", "NTRIP reload failed: %s" % exc)
        return False
    _RESTART_REQUIRED = False
    return True


def apply_live(changes, deliberate=False):
    """Save live and task_restart keys now; True once they are in config.json.

    A key with any other policy (or none) is a programming error and raises
    ValueError before anything is written. A changed task_restart key is
    handed to the registered task reloader; without one (safe mode) or when
    the handover fails it takes effect after the next restart and
    config_restart_required() reports that until a later handover succeeds.
    While an unreadable config.json is protected, only a deliberate whole
    configuration write (stage_config) saves and releases it; any other call
    returns False without touching CONFIG or the file.
    """
    global _RESTART_REQUIRED
    for key in changes:
        if APPLY_POLICY.get(key) not in (APPLY_LIVE, APPLY_TASK_RESTART):
            raise ValueError("%s cannot be applied live" % key)
    if not changes:
        return True
    released = False
    if _CONFIG_FILE_PROTECTED:
        if not deliberate:
            return False
        released = _release_config_file()
    restart = any(APPLY_POLICY[key] == APPLY_TASK_RESTART and CONFIG.get(key) != value
                  for key, value in changes.items())
    if not save_config(changes):
        if released:
            _protect_config_file()              # the file is still the unreadable one
        return False
    if restart:
        _RESTART_REQUIRED = True
        _request_task_reload()
    CONFIG_WARNINGS[:] = [w for w in CONFIG_WARNINGS if w.get("key") not in changes]
    return True


def stage_config(changes):
    """Apply a change by policy: live and task_restart keys now, the rest staged.

    Changed verified_reboot keys are held in RAM until apply_staged_config();
    CONFIG, config.json and the transaction files do not see them before, and
    a reboot before the apply drops them. A key set back to its current value
    leaves the staging; a live or task_restart key with a load warning is saved
    even then, and so is every sent one while config.json is protected. Returns None when the live part cannot be saved (the
    staging is then unchanged), otherwise the key names that were applied and
    that are staged.
    """
    for key in changes:
        if key not in APPLY_POLICY:
            raise ValueError("Unknown configuration key: %s" % key)
    # A key with a load warning is saved even when the value equals its fallback
    # in RAM: config.json still holds the rejected value, and only a save
    # repairs the file and clears the warning.
    # While an unreadable config.json is protected, every sent live or
    # task_restart key is saved: this deliberate write is what repairs the file
    # and releases the protection, even when all values equal the fallback.
    warned = set(w.get("key") for w in CONFIG_WARNINGS)
    protected = _CONFIG_FILE_PROTECTED
    live = dict((key, value) for key, value in changes.items()
                if APPLY_POLICY[key] != APPLY_VERIFIED_REBOOT
                and (protected or CONFIG.get(key) != value or key in warned))
    if not apply_live(live, deliberate=True):
        return None
    before = dict(_STAGED)
    for key, value in changes.items():
        if APPLY_POLICY[key] != APPLY_VERIFIED_REBOOT:
            continue
        if CONFIG.get(key) == value:
            _STAGED.pop(key, None)
        else:
            _STAGED[key] = value
    _note_staging(before)
    return {"applied": sorted(live), "staged": sorted(_STAGED),
            "pending": bool(_STAGED), "restart_required": config_restart_required(),
            "staging_revision": staging_revision()}


def _note_staging(before):
    """Count a content change of the staging (a new, changed or removed key)."""
    global _STAGING_SERIAL
    if _STAGED != before:
        _STAGING_SERIAL += 1


def staging_revision():
    """Names the current staging content; a client that sends the one it saw
    never applies a staging it has not seen. The boot id keeps it unique across
    restarts."""
    return "%s-%d" % (BOOT_ID, _STAGING_SERIAL)


def staged_config():
    """A copy of the staged verified_reboot keys."""
    return dict(_STAGED)


def discard_staged_config():
    """Drop the staged keys and the restart flag."""
    global _RESTART_REQUIRED
    before = dict(_STAGED)
    _STAGED.clear()
    _note_staging(before)
    _RESTART_REQUIRED = False


def config_restart_required():
    """True while staged keys or saved task_restart keys wait for a restart."""
    return bool(_STAGED) or _RESTART_REQUIRED


def apply_staged_config(apply_id=None):
    """Arm the staged keys as the candidate of a verified reboot.

    `pending` is written first, with the client's apply_id: if that fails
    nothing is armed. If arming fails, the previous result is written back and
    the staging is kept for a retry; the import-time resolution handles a
    `pending` that stays behind. An empty staging arms the current
    configuration (task_restart changes are then confirmed by the same
    verified reboot).
    """
    global _RESTART_REQUIRED
    # At most one transaction: web.py answers 409 open_change_pending before this.
    if pending_config_active():
        return False
    released = _release_config_file()
    previous = config_result_read()
    if not config_result_write("pending", apply_id=apply_id):
        if released:
            _protect_config_file()
        return False
    if not _store.arm(CONFIG, DEFAULT_CONFIG, PERSIST_EXCLUDE, dict(_STAGED),
                      LAST_GOOD_CONFIG_FILE, PENDING_CONFIG_FILE, _atomic_dump, log):
        if released:
            _protect_config_file()
        status = previous.get("status")
        if status == "none":
            # No result before (or an unreadable one): a complete `none` record,
            # never the synthetic read without uptime_sec.
            config_result_write("none")
        elif status != "unknown":                 # never persist the synthetic status
            _atomic_dump(CONFIG_RESULT_FILE, previous)
        return False
    before = dict(_STAGED)
    _STAGED.clear()
    _note_staging(before)
    _RESTART_REQUIRED = False
    return True


def apply_config_change(changes, apply_id=None):
    """Stage and apply one whole change (/save, restore) as its own candidate.

    An earlier PUT staging is dropped first, so only this request and the
    active configuration form the candidate. A restart requirement of saved
    task_restart keys stays until a candidate is armed.
    """
    before = dict(_STAGED)
    _STAGED.clear()
    _note_staging(before)
    if stage_config(changes) is None:
        return False
    if apply_staged_config(apply_id):
        return True
    # The user sees the failure, so nothing of this change stays staged; saved
    # task_restart keys keep their restart flag.
    before = dict(_STAGED)
    _STAGED.clear()
    _note_staging(before)
    return False


def config_result_write(status, message=None, details=None, apply_id=None):
    """Persist the machine-readable outcome of the latest config attempt.

    ``message`` is accepted temporarily at call sites but deliberately not
    stored: presentation belongs to the browser translation dictionary. The
    record names this boot; `pending` carries apply_id, a final record
    (`connected`, `failed`) the one of the `pending` record it replaces.
    """
    from state import app  # lazy: state imports cfg
    if status in ("connected", "failed"):
        apply_id = _inherited_apply_id(config_result_read())
    elif status != "pending":
        apply_id = None
    return _store.result_write(CONFIG_RESULT_FILE, _atomic_dump, status, details,
                               app.uptime_seconds(), BOOT_ID, apply_id)


def _inherited_apply_id(previous):
    """The apply_id of the pending record this final record replaces; None otherwise.

    Only a read `pending` passes its identity on (also one recovered from a
    leftover file); a final, unreadable or missing predecessor never does.
    """
    apply_id = previous.get("apply_id") if previous.get("status") == "pending" else None
    return apply_id if isinstance(apply_id, str) else None


def config_result_read():
    return _store.result_read(CONFIG_RESULT_FILE, log)


def complete_rollback():
    """Finish an open rollback (idempotent); True once nothing is left to restore."""
    global _ROLLBACK_OPEN, _ROLLBACK_CODE
    pending_config_active()
    if _STAGING_UNRECOVERED:
        return False
    # An unreadable config.json left only defaults in RAM: the whole confirmed
    # last-good configuration replaces it (bypassing the write protection), and
    # the protection ends once that was written and read back. A configuration
    # read in place after a failed recovery holds real settings: only the
    # changed keys go back, but the write also passes the protection.
    full = _CONFIG_FILE_PROTECTED and _CONFIG_DEFAULTS_ONLY
    written = []

    def dump(target, data):
        ok = _store.atomic_dump(target, data, log)
        if ok and target == CONFIG["config_file"]:
            written.append(target)
        return ok
    before = dict((key, CONFIG.get(key)) for key in _TASK_RESTART_KEYS)
    try:
        ok = _store.complete_rollback(CONFIG, DEFAULT_CONFIG, PERSIST_EXCLUDE,
                                      VERIFIED_REBOOT_KEYS, LAST_GOOD_CONFIG_FILE,
                                      PENDING_CONFIG_FILE,
                                      dump if _CONFIG_FILE_PROTECTED else _atomic_dump,
                                      log, full=full)
    except Exception as exc:
        log("ERROR", "SYS", "Completing the configuration rollback failed: %s" % exc)
        _hand_over_rollback(before)
        return False
    _hand_over_rollback(before)
    if ok and written:
        _release_config_file("The confirmed last-good configuration")
    if ok:
        _ROLLBACK_OPEN, _ROLLBACK_CODE = False, None
    return ok


def _hand_over_rollback(before):
    """Hand caster values that a rollback changed in CONFIG to the running task.

    Coupled to CONFIG, not to the file cleanup: the store updates CONFIG before
    it removes `pending`. Without a reloader (start-up path, safe mode) the
    task reads the configuration when it starts. A failed handover stays as
    the restart flag and is retried by the next complete_rollback().
    """
    global _RESTART_REQUIRED
    if _TASK_RELOADER is None:
        return
    if any(CONFIG.get(key) != value for key, value in before.items()):
        _RESTART_REQUIRED = True
    if _RESTART_REQUIRED:
        _request_task_reload()


def retry_rollback():
    """Retry entry of the network manager while a rollback is open."""
    return rollback_pending_config(_ROLLBACK_CODE or "storage_error")


def commit_pending_config(active_ssid=None):
    """Confirm a candidate that connected - only if it landed and no rollback was decided.

    True means confirmed (or nothing open). A recorded `failed` or an unlanded
    change completes the rollback instead and returns False even when that
    succeeds.
    """
    global _ROLLBACK_OPEN, _ROLLBACK_CODE
    if not pending_config_active():
        return True
    if _STAGING_UNRECOVERED:
        return False
    status = config_result_read().get("status")
    if status == "unknown":
        # An unreadable result may hide a rollback decision: never confirm.
        _ROLLBACK_OPEN = True
        log("ERROR", "SYS", "Configuration result unreadable; nothing is confirmed.")
        return False
    if status == "failed":
        _ROLLBACK_OPEN = True
        log("WARN", "SYS", "A recorded rollback decision is completed; nothing is confirmed.")
        # False either way: a completed rollback is no confirmation.
        complete_rollback()
        return False
    if not _store.change_landed(CONFIG, VERIFIED_REBOOT_KEYS, PERSIST_EXCLUDE,
                                PENDING_CONFIG_FILE, LAST_GOOD_CONFIG_FILE):
        _ROLLBACK_OPEN, _ROLLBACK_CODE = True, "apply_interrupted"
        if not config_result_write("failed", details={"code": "apply_interrupted", "restored": []}):
            log("ERROR", "SYS", "Could not record the interrupted configuration apply.")
        log("WARN", "SYS", "The applied configuration never reached config.json; nothing is confirmed.")
        complete_rollback()
        return False
    # `connected` first: if it cannot be written, the files stay and the next
    # boot verifies again.
    if not config_result_write("connected",
                               details={"ssid": active_ssid or CONFIG.get("wifi_ssid", "")}):
        return False
    log("INFO", "SYS", "New network configuration confirmed operational.")
    return _store.finish(PENDING_CONFIG_FILE, LAST_GOOD_CONFIG_FILE, log)


def rollback_pending_config(code=None):
    """Decide the rollback (`failed` with its first cause), then complete it.

    Without a code the last Wi-Fi error is the cause. A result that cannot be
    written does not stop the restore: reachability first, and the next import
    resolves the stale `pending`.
    """
    global _ROLLBACK_OPEN, _ROLLBACK_CODE
    _ROLLBACK_OPEN = True
    try:
        if code is None:
            from state import app  # lazy: state imports cfg
            code = app.stats.get("last_wifi_error") or "connection_lost"
        _ROLLBACK_CODE = _ROLLBACK_CODE or code
        _record_rollback_decision()
    except Exception as exc:
        # The decision record must never stop the restore (and never the caller).
        log("ERROR", "SYS", "Recording the configuration rollback failed: %s" % exc)
    return complete_rollback()


def _record_rollback_decision():
    # pending_config_active() recovers the files the change set is read from.
    # Without any pending file there is no transaction, and an earlier
    # `connected` must never be overwritten.
    if not pending_config_active() or not _store.staging_trace(PENDING_CONFIG_FILE):
        return
    if config_result_read().get("status") == "failed":
        return
    previous = _store._read_dict(LAST_GOOD_CONFIG_FILE)
    restored = [] if previous is None else sorted(_store.changed_keys(
        VERIFIED_REBOOT_KEYS, PERSIST_EXCLUDE,
        _store._read_dict(PENDING_CONFIG_FILE), previous))
    if not config_result_write("failed", details={"code": _ROLLBACK_CODE,
                                                  "restored": restored}):
        log("ERROR", "SYS", "Could not record the configuration rollback; restoring anyway.")


def resolve_interrupted_apply():
    """Resolve what a restart interrupted, before the network manager starts.

    A stale `pending` without a pending file becomes `failed`/apply_interrupted;
    a recorded `failed` with a pending file is completed. Runs at import time
    and never raises (a failure leaves the change open and blocked).
    """
    global _ROLLBACK_OPEN
    try:
        previous = config_result_read()
        status = previous.get("status")
        pending = pending_config_active()
        if status == "pending" and not pending:
            # Import time: state does not exist yet, hence no uptime.
            _store.result_write(CONFIG_RESULT_FILE, _atomic_dump, "failed",
                                {"code": "apply_interrupted", "restored": []}, 0,
                                BOOT_ID, _inherited_apply_id(previous))
            log("WARN", "SYS", "An interrupted configuration apply was recorded as failed.")
            return True
        if status == "failed" and pending:
            _ROLLBACK_OPEN = True
            complete_rollback()
            return True
        return False
    except Exception as exc:
        # Only an unrecoverable staging file blocks (pending_config_active()
        # records that itself); an error elsewhere must not.
        log("ERROR", "SYS", "Resolving the interrupted configuration apply failed: %s" % exc)
        return False

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


def prepare_config_change(raw):
    """Validate a whole configuration request: (values, code, message)."""
    return _validation.prepare_config_change(
        raw, SETTABLE_KEYS, SECRET_KEYS, _validation.LOAD_VALIDATORS)

def prepare_restore_change(raw):
    """Validate a backup's connection settings: (values, kept, code, message)."""
    return _validation.prepare_restore_change(
        raw, SETTABLE_KEYS, SECRET_KEYS, _validation.LOAD_VALIDATORS)

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
    released = _release_config_file()
    if _factory.factory_reset(CONFIG, DEFAULT_CONFIG, FACTORY_CLEAR_KEYS, save_config, log):
        return True
    if released:
        _protect_config_file()
    return False


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
                                 PLANNED_RESET_FILE, CRASH_RESET_CAUSES,
                                 _atomic_dump)

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
# Completes a recorded rollback before the network manager starts.
resolve_interrupted_apply()
