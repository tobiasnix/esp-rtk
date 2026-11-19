# Technical reference

This document describes what the firmware in this repository actually does:
its modules, its configuration keys, its network and NTRIP behaviour, the
survey data model, the on-flash storage layout, and the local HTTP API.

It is written for developers and integrators. Operating instructions are in
[../README.md](../README.md) and [onboarding.md](onboarding.md); the stable
client-facing contract is in [integration.md](integration.md).

## Overview

ESP-RTK is MicroPython firmware for an ESP32-S3 paired with a Quectel LC29H
GNSS receiver. It joins a Wi-Fi network, pulls RTCM corrections from an NTRIP
caster into the receiver's UART, evaluates the resulting NMEA stream against a
configurable acceptance gate, records projects, lines, and asset points on the
device's own flash, and serves a local web portal. The same NMEA stream is
published over Bluetooth LE and over TCP.

Nothing leaves the device unless the operator exports it. There is no cloud
component and no outbound connection other than the NTRIP caster, NTP, and
mDNS.

### Versions and schemas

| Item | Value | Defined in |
|---|---|---|
| Firmware version string | `ESP-RTK V16.0.0` | `cfg.py` `CONFIG["version"]` |
| Configuration schema | 3 | `cfg.py` `CONFIG["config_schema_version"]` |
| Survey data schema | 5 (schemas 2 to 4 are still readable) | `tracking/common.py` `SCHEMA_VERSION`, `LEGACY_SCHEMA_VERSIONS` |
| Point-store schema | 4 (record markers 2 legacy, 3 exact) | `pointstore.py` `POINT_STORE_SCHEMA` |
| Device identity schema | 3 | `identity.py` `IDENTITY_SCHEMA_VERSION` |
| Browser asset version | `16.0.0` | `assets.py` `ASSET_VERSION` |

The firmware version is deliberately excluded from `config.json`, so an old
configuration file cannot pin an old version string after an update.

### Device identity

Every visible name is derived from the factory Wi-Fi MAC address, so it
survives a factory reset and a filesystem rebuild. For a device whose short ID
is `A1B2C3`:

| Name | Value | Purpose |
|---|---|---|
| `display_name` | `RTK-A1B2C3` | portal heading, mDNS instance name |
| `ap_ssid` | `RTK-A1B2C3-SETUP` | setup access point |
| `ble_name` | `RTK-A1B2C3` | BLE advertising name |
| `hostname` | `rtk-a1b2c3` | DHCP name, `rtk-a1b2c3.local` |

Three secrets belong to the identity and are stored in NVS where available,
with an `identity.json` fallback: the device code used for portal sign-in, the
stream token used for TCP authentication, and the six-digit BLE passkey. A
factory reset keeps the device code and rotates the stream token, so a printed
label stays valid.

## Architecture

### Modules

`BOARD_FILES` in `push.py` is the allowlist of files that form the application
on the board. Everything else in the repository is a host tool, a test, or
documentation.

| File | Responsibility |
|---|---|
| `boot.py` | Unmodified MicroPython boot template; kept under version control so a freshly flashed board matches a grown one. |
| `cfg.py` | `CONFIG` defaults, the apply policy per key, constants, and the public configuration and logging API; the lowest layer, with no dependency on other application modules. |
| `cfg_validation.py` | Pure validation and presentation helpers: settings validation, Wi-Fi network lists, HTML escaping, subnet comparison. |
| `cfg_store.py` | Transactional persistence: atomic JSON writes, crash recovery, schema migration, pending and last-good staging. |
| `cfg_boot.py` | Boot health: exponential backoff, boot counter, planned-reset marker, reset-cause naming. |
| `cfg_factory.py` | Factory reset, BOOT-button hold evaluation, access-point password generation. |
| `cfg_logging.py` | RTC flight recorder and the wear-limited persistent error log. |
| `identity.py` | Stable device identity, device code, stream token, BLE passkey, and the names derived from the factory MAC. |
| `access.py` | Short-lived administrator sessions: login throttling, session and CSRF tokens, cookie parsing. |
| `state/__init__.py` | The `state` interface and runtime core: the `app` object and its statistics literal, the bounded `SimpleQueue` (backed by a `deque`), task heartbeats, the `supervise()` restart wrapper, and `shutdown_event`. Callers import only from `state`, never from its parts. |
| `state/receiver.py` | Fix, freshness and gate state of `app`: `update_fix`, `attach_gst`, fix age, the RTK gate cache and its hold, quality streaks, and `GST_GRACE_MS`. |
| `state/link.py` | Link state of `app`: access and NTRIP state, receiver UART silence and link health, and `corrections_health`. |
| `state/system.py` | System state of `app`: uptime, clock steps, loop lag window, task heartbeat, the error list, device identity, `clock_is_valid`, and the `_instances` registry. |
| `state/storage.py` | Storage state of `app`: the maintenance flag and the recording probe. |
| `net.py` | Wi-Fi station and access point management plus the NTRIP client and the RTCM message counter. |
| `discovery.py` | Captive DNS during setup and a small mDNS responder for the portal and the NMEA service. |
| `gnss.py` | UART reader, NMEA checksum and whitelist filtering, GGA and GST parsing, GST re-enable retries, the route startup guard, and the status LED. |
| `geo.py` | Exact integer positions: float-free NMEA coordinate parsing to 1e-8 degrees and millimetres, WGS84 local scale factors, offsets, distances and integer means. |
| `fanout.py` | Distributes validated NMEA sentences into independent bounded BLE and TCP queues, gives every TCP client its own bounded queue and writer task, and runs the NMEA TCP listeners including the optional `AUTH` handshake. |
| `ble.py` | Encrypted, bonded Nordic UART Service peripheral with passkey pairing and persisted bonds. |
| `esptel.py` | Builds the versioned `$PESPS` device-telemetry sentence and injects it into the NMEA fan-out. |
| `pointstore.py` | Generation-based segmented flash store for measured points, with compact encoding and a bounded expansion cache. |
| `tracking/common.py` | Survey limits, file names, schema versions and the time and profile helpers the tracking modules share. |
| `tracking/guards.py` | Storage guards and the errors of the survey store. |
| `tracking/frames.py` | Reference frames and heights of stored measurements, and the project frame assignment. |
| `tracking/validation.py` | Validation of coordinates, measurements and survey backups, and the upgrade of legacy backups. |
| `tracking/fixes.py` | The measurement gate, fix quality and averaging, setup stamps and line projection. |
| `tracking/model.py` | The event model: one handler per journal event kind, project activity and live map deltas. |
| `tracking/journal.py` | The event journal, index and checkpoint: append, load, replay and compaction. |
| `tracking/archive.py` | Project archives: write, verify, reactivate and stream archived projects. |
| `tracking/restore.py` | Survey backups and the one restore entry with its staged, power-cut safe activation. |
| `tracking/summary.py` | Survey status, capacity and the cached line summaries. |
| `tracking/exports.py` | Map data and the GeoJSON and CSV export streams. |
| `tracking/recording.py` | Live recording: lines, vertices, asset points, undo and the fix worker. |
| `tracking/core.py` | The Tracker: construction, start-up, projects and storage capacity, composed of the tracking mixins. |
| `tracking/__init__.py` | The `tracking` interface: re-exports the names other modules read and creates the one `tracker` instance. Callers import only from `tracking`, never from its parts; `tracking.tracker` is the one `Tracker` instance. |
| `stakeout.py` | Stakeout target sets: complete validation of a browser-normalized import package, content-based set and target IDs, and the per-set flash store (one folder per project) with its own atomic write, read-only trailer listing, streamed download, and timing profile. |
| `stakeout_progress.py` | Stakeout progress and confirmation: the point checker, the per-project `progress.jsonl` log, the fold of its events with the target generations, the decision rule, compaction, the restore floor, and the count of unowned folders. |
| `stakeout_export.py` | Streamed stakeout export (GeoJSON and CSV) as a snapshot of its start; imported only by the export handler. |
| `measurement.py` | Measurement session: progress, cancel, and the sample loop that averages distinct accepted fixes. |
| `parallel_benchmark.py` | Isolated synthetic tracking load used as a release gate; refuses to run while production survey data exists. |
| `field_diagnostics.py` | Optional bounded NDJSON field recorder with explicit UTC validity, boot and session IDs, and RTCM signal observations. |
| `events.py` | The server-sent event stream `GET /api/events`: frame encoding, bounded drain and flush, and the hub that builds one live frame per tick for all subscribers. The sources are injected by `main.py`, so the module does not import `web`. |
| `web_routing.py` | Declarative transport policy per path: allowed methods, method-error text, and request body limit. |
| `web.py` | The HTTP server: request parsing, interface context, authentication, and every endpoint handler. |
| `ui.py` | The dependency-free portal markup, served from the device itself. |
| `assets.py` | The single cache-busting `ASSET_VERSION` substituted into served HTML. |
| `main.py` | Entry point: reset accounting, BOOT-button handling, safe mode, and the asyncio task set. |
| `app.js`, `app.css` | The field application (status, projects, device) served to the browser. `app.js` is built from `src/` – never edit it directly. |
| `src/*.js` | Build sources of `app.js`, concatenated in file-name order by `push.py`; never on the board. |
| `app.js.gz`, `app.css.gz`, `i18n.js.gz` | Pre-compressed copies of the above, rebuilt by `push.py` and served when the client accepts gzip. |
| `pages.js`, `pages.css` | Shared behaviour and styling for the support pages: sign-in, setup, diagnostics, label. |
| `base.css` | Base stylesheet loaded before `pages.css`. |
| `i18n.js` | Complete German, English and Polish dictionaries for the browser; machine-readable results stay English. |
| `qr.js` | Local QR code rendering for the Wi-Fi and portal codes on the label page. |

### Tasks

`main.py` starts one asyncio task per concern. All long-running tasks except
the shutdown handler are wrapped in `state.supervise()`, which logs a crash,
increments `task_restarts`, waits, and starts the coroutine again from its
factory. The wait is 2 s after the first crash and doubles with each further
consecutive crash up to 60 s; a run of 60 s or more (measured on the
accumulated uptime, so it holds across tick wrap-around) starts again at 2 s.
For the watched tasks the wait is capped at half of `heartbeat_timeout_sec`
(30 s by default), and the supervisor never produces a heartbeat for a task.
Before this change, a watched task that crashed right after its first
heartbeat restarted every 2 s and the watchdog never noticed; a wait of 60 s
would only have produced a short heartbeat gap and a false "the watchdog will
reset the device" report without a reset, because a reset needs the gap to
last `wdt_timeout` beyond the deadline. A crash loop therefore shows in
`task_restarts`, not as a watchdog reset; a task that hangs still triggers the
watchdog. The cap applies only to `WATCHED_TASKS`: the `watchdog` task itself
runs under `supervise()` but is not watched, so if it keeps crashing, its wait
reaches 16 s or more from the fourth crash on with the default `restart_delay`,
the hardware watchdog (`wdt_timeout`, 15 s by default) is no longer fed and resets the board, which is
intended escalation.

A task that returns normally is not restarted, so `http` and `nmea_tcp` let a
server failure (for example `EADDRINUSE` or `ENOMEM` from `start_server`)
propagate; `nmea_tcp` returns normally only when no NMEA port is configured,
and a failing legacy port is logged as a warning. The wait is not cut short by
a shutdown; the only shutdown path resets the board one second later.

| Task | Started as | Role |
|---|---|---|
| `field_diagnostics` | `recorder.run` | Periodic sampling and bounded flushing of the field recorder. |
| `watchdog` | `watchdog_task` | Feeds the hardware watchdog only while every watched task is alive. |
| `gc` | `gc_task` | Periodic `gc.collect()` on `gc_interval_ms`. |
| `loop_lag` | `loop_lag_task` | Measures event-loop lag every 100 ms into `stats.loop_lag_ms`/`loop_lag_max_ms` (since boot) and the field recorder's per-sample maximum; not a watched task. |
| `led` | `StatusLED.run` | Status LED: fast blink without a fix, one-second blink with any other fix, steady on at RTK FIXED. |
| `network` | `NetworkManager.run` | Station and access-point lifecycle, reconnects, pending-configuration verdict. |
| `ntrip` | `NtripClient.run` | Caster connection, failover, backoff, RTCM to UART, periodic GGA upload. |
| `http` | `http_server_task` | The local portal and API on `http_port`. |
| `sse` | `EventHub.run` | Event stream hub: every 200 ms it decides whether a live frame is due and hands the one shared frame to the subscribers; runs in normal and safe mode. |
| `discovery` | `discovery_task` | mDNS responder, and captive DNS while the setup portal is active. |
| `gnss` | `GNSSHandler.run` | UART reading, NMEA validation, fix extraction, GST handling. |
| `ble` | `NmeaSenderTask.run` | Routes sentences from the shared input queue into the two output queues. |
| `ble_output` | `NmeaSenderTask.run_ble` | Drains the BLE queue into notifications. |
| `tcp_output` | `NmeaSenderTask.run_tcp` | Drains the TCP queue into batched socket writes. |
| `ble_events` | `BLEManager.event_task` | Processes BLE IRQ events outside the interrupt context. |
| `nmea_tcp` | `nmea_tcp_task` | Listens on the canonical and legacy NMEA ports. |
| `telemetry` | `EspTelemetryTask.run` | Emits `$PESPS` on `telemetry_interval_sec`. |
| `tracking` | `tracking_startup_task` | Recovers survey storage cooperatively, then runs the tracker's append worker. |

Two tasks are not supervised by design: `shutdown_handler` waits once on
`shutdown_event` and then releases BLE and the UART, and the tracker's append
worker is awaited by `tracking_startup_task`.

The watchdog feeds only while all tasks in `WATCHED_TASKS` — `gnss`, `ntrip`,
and `network` — have produced a heartbeat within `heartbeat_timeout_sec`. The
BLE sender is deliberately not watched, because its queue may legitimately stay
empty. `http` and `nmea_tcp` are deliberately not watched either: their loop
only sleeps, and a server failure raises, so `supervise()` restarts the task
(counted in `task_restarts`). If a watched task stalls, the feed stops and the hardware watchdog
resets the board within `wdt_timeout`, with the cause recorded in the
persistent error log. While storage maintenance runs (restore, archiving,
compaction, or the background checkpoint), the heartbeat check is paused and
the watchdog keeps being fed; the check returns as soon as the last running
maintenance ends.

Wi-Fi is created before BLE. Both radios allocate from the same ESP-IDF heap,
and creating BLE first left too little for the Wi-Fi driver.

### Safe mode

`bump_boot_count()` counts consecutive starts whose reset cause looks like a
crash. Only the hard-reset and watchdog causes (2 and 3) count as a crash; any
other cause clears the counter, including a power-on. A power cut therefore
resets the counter instead of adding to it. A caller that restarts on purpose
leaves a marker first, because `machine.reset()` reports as a hard
reset on the ESP32-S3 and is otherwise indistinguishable from a panic. After
`SAFE_MODE_BOOTS` (5) starts that never reached `SAFE_MODE_UPTIME_SEC` (300 s)
of uptime, the device starts in safe mode: only the field recorder, watchdog,
GC, loop-lag, network, HTTP, and discovery tasks run. NTRIP, BLE, GNSS, and survey
storage stay down, so the portal remains reachable for repair. The network
manager is registered as in normal operation, so the Wi-Fi scan in Diagnostics
(`/api/wifi-scan`) works in safe mode.

The counter is stored in `bootcount.json` and written atomically. A power-on,
a planned reset, or any other non-crash cause removes the file together with
its `.tmp` and `.previous` copies; otherwise the recovery after the next crash
could bring back an old count from a leftover copy. One risk remains: if
renaming a valid copy into place fails during recovery, or writing the counter
fails permanently (for example because the flash is full), the counter stops
advancing, and a series of crashes may then not reach safe mode. The failed
write case existed before the atomic write.

### Persistent error log

Warnings and errors go to three places: the console, the RTC flight recorder
(RTC memory, at most 1900 bytes, shown by `/errors`), and the persistent error
log in flash (`errors.log`, rotated to `errors.log.1`; `/api/log` and
`/errors`). Two separate stages keep a fault that repeats from wearing the
flash.

**Event throttling at the source.** Four hot paths report a condition before
it reaches the logger: a failed NMEA-TCP send, an unexpected NTRIP error, a
rejected HTTP request, and an NMEA queue overflow. Each writes one log line the
first time the condition occurs and afterwards at most one line per 60 seconds
and condition; the line ends with `[N more since the last report]` when events
were held back. The held-back events reach neither the console nor the RTC
recorder nor the flash. They are counted in `/status` instead:
`tcp_write_errors` and `tcp_write_timeouts` (NMEA-TCP send failures),
`nmea_tcp_drops` (lost sentences: router queue, client queues, and
sentences still queued when a client is disconnected; write failures appear only
in `tcp_write_errors` and `tcp_write_timeouts`), `queue_overflows` (NMEA
queue), `tcp_queue_overflows` (overflows of the TCP router queue and of the
client queues; `tcp_queue_max` still refers to the router queue only),
`nmea_tcp_client_stats` (per connected client: `id`, `drops`, `queue_max`),
`tcp_client_queue_max`, `nmea_tcp_rejected_full` (connections closed because
`nmea_tcp_max_clients` was reached), and `http_rejected_requests` (rejected
requests, counted from 0 at each start). For
NTRIP, `ntrip_retries` counts every failed attempt regardless of log
throttling, and a stale-backlog reconnect increments it as well; a stream that
was stable before it failed and a shutdown do not count as failed attempts.

**Sink rate limit.** In the logger, for the flash only: every line still goes
to the console and into the RTC flight recorder. WARN and ERROR lines go to
flash as before, including the repeat line for a message that repeats. Per
severity and tag, the first 6 lines pass as a burst; after that one more line
passes every 10 seconds. Lines above the limit are replaced by one summary
line of the form `----- N more WARN HTTP lines suppressed (rate limit)`. It
names the count, severity and tag but never the message text, and `/api/log`
returns it with its level and source. A held-back message that keeps repeating
is written as soon as a line may pass again; the repeat line only ever follows
a message that was actually written to flash.

Two kinds of line are exempt from the sink limit: the boot marks (the `-----`
start lines) and the history of the previous session, which is copied from the
RTC memory to flash after a crash boot.

A suppressed single line can only be read in `/errors` while it is still in
the RTC history. That history keeps between 75 % and 100 % of 1900 bytes, older
lines are pushed out, and a power cut clears it. Beyond that nothing is
promised. The limit lowers the number of flash appends; it does not shorten
the duration of a single write.

**Counter meanings.** Each counter in `/status` `stats` has one owner module;
`tests/test_stats_writers.py` lists the exceptions. `ble_drops` is the sum of all
lost BLE outputs: `ble_queue_overflows` (the fan-out queue was full) plus
`ble_notify_drops` (a notification was given up). `nmea_tcp_auth_failures`
counts every rejected NMEA-TCP sign-in, including timeouts and malformed
requests; `tcp_auth_failures` is the part with a wrong or missing stream key.
`ble_auth_failures` counts BLE security events that did not end encrypted and
authenticated, so a pairing that disconnects without such an event is not
counted. `gga_parsed` counts parsed GGA sentences (its alias `gnss_msgs` has the
same value) and `wifi_ssid_active` is the SSID in use (alias `wifi_ssid_aktiv`);
the aliases stay for one release. `uart_read_gap_ms` is the gap between the last
two UART reads, not the running silence, which is `link.uart_silence_ms`.
`uart_rx_rate_bytes_sec` is 0 as soon as a full one-second window has passed
without bytes, that is at most about 2 s after the last byte.

### Data flow

```text
LC29H --UART1--> GNSSHandler --checksum + whitelist--> nmea_queue (SimpleQueue)
                      |                                      |
                      | parsed GGA/GST                       | NmeaSenderTask.route()
                      v                                      v
                  app.last_fix                        ble_queue      tcp_queue
                      |                                   |              |
        tracker.enqueue_fix() (no flash)                  v              v
                      |                            BLE NUS notify   per-client queue + writer
                      v
            tracker.run_worker() --> journal + point store on flash
                      |
                      v
              HTTP API (/api/tracking, /api/ui/live, exports)
```

Bounded `SimpleQueue` instances use a `deque`; unbounded ones stay list-based.
Small bounded lists such as replies, errors, map changes and pending fixes keep
`pop(0)` by design.

Sentences reach the transports through bounded queues only. The shared input
queue holds `nmea_queue_size` sentences; the BLE and TCP router queues hold 100
each, and every TCP client then has its own queue of at most 100 entries or
8 KB with its own writer task. A slow BLE client or a stalled socket therefore
cannot block UART consumption or the other transport, and a stalled TCP client
loses only its own sentences — it is disconnected when a drain does not finish
within 5 seconds. The affected queue counts its own drops in `app.stats`. `$PESPS` telemetry is
injected into the same input queue, so it follows the identical path.

The recording path is deliberately split. `enqueue_fix()` runs in the GNSS
task, validates the coordinate, applies the automatic-distance rule, and
appends to an in-memory list bounded at 64 entries. Only `run_worker()` touches
flash. A single flash commit can take seconds; keeping it out of the GNSS task
is what allows NMEA output to continue during a write.

## Configuration

### Storage and recovery

The runtime configuration lives in `CONFIG` in `cfg.py`. It is persisted to
`config.json`, minus `PERSIST_EXCLUDE` (`version`, `config_file`,
`wdt_enabled`, `ble_commands_enabled`). Those four are code-only so that a
stale or malformed file cannot pin an old version string or permanently
disable the watchdog.

Every configuration write is atomic: the new content goes to `<file>.tmp`,
is parsed back to verify it, the current file is renamed to `<file>.previous`,
and the temporary file is renamed into place. The temporary file is flushed
before it is closed and renamed; `os.sync()` is not used. A failure at the
last step restores the previous file. On load, `recover_atomic()` repairs a file whose
rename lost power in the middle by promoting whichever of `.tmp` or
`.previous` still parses as a JSON object. Without this, a brownout during a
plain `open(..., "w")` left a truncated file, `load_config()` silently fell
back to the defaults, and the device came up with no Wi-Fi and no NTRIP.
Loading `config.json` never raises, so the `cfg` import always completes: if
the recovery itself fails (for example `EIO` on a rename), an `ERROR` line is
logged and a readable candidate (`config.json`, `.tmp`, `.previous`) is used
in place; the file is then protected like an unreadable one (below), so no
automatic save replaces the only surviving copy. A file that exists but cannot be read, or holds valid JSON that is
not an object (`[]`, `5`, `null`, `"x"`), is not loaded: the defaults stay,
an `ERROR` line is logged, and nothing automatic overwrites the file in this
boot (`cfg.config_file_protected()`): a newly generated AP password is kept in
RAM only (`WARN`). An open rollback is completed with the whole
`config.last_good.json` (confirmed and complete) instead of the defaults plus
the changed keys, which ends the protection; if that write fails, the rollback
and the protection stay. The live PUTs
(`/api/measurement-gate`, `/api/field-setup`, `/api/tcp-auth`) answer `503
config_file_unreadable` without changing anything, because saving one key would
write the defaults over the whole file. Only a deliberate write of the whole
configuration releases the protection: `PUT /api/config` with caster keys,
`/save`, restore, apply after a Wi-Fi `PUT`, or a factory reset (a Wi-Fi-only
`PUT` without apply keeps it). While the protection holds, `config_warnings`
lists an entry with `key` `config_file`. A missing file is only an `INFO` line.
An atomic write never deletes a target that exists but does not parse: it is
renamed to `<file>.unreadable` first (replacing an older copy), so the original
`config.json` stays available for a manual rescue; a factory reset deletes
these copies.

The boot counter (`bootcount.json`) is written with the same sequence
(temporary file, flush, verify, rename). Its recovery after a power cut is
stricter than the one above: only a JSON object whose `n` is a whole number of
at least 0 counts as a valid copy, and a valid copy is preferred over an
invalid target such as `{}`.

Values are coerced to the type of their default, because HTTP form data
arrives as strings and old configuration files stored ports as strings.

Loading also migrates: a file written under schema 1 that still uses port 2947
as the primary NMEA port is moved to 10110 with 2947 kept as the legacy
listener, and a file below schema 3 adopts the renamed access-point channel
key and is rewritten as schema 3.

### Keys

Only these keys can be set over HTTP (`SETTABLE_KEYS`); the Wi-Fi keys wait for
an apply, the caster keys are saved at once (see
[Pending and last-good configuration](#pending-and-last-good-configuration)):

| Key | Default | Validation |
|---|---|---|
| `wifi_ssid` | `""` | 1–32 characters |
| `wifi_pass` | `""` | 8–63 characters; empty means "leave unchanged" |
| `ntrip_enabled` | `true` | boolean |
| `ntrip_host` | `""` | 1–64 characters |
| `ntrip_port` | `2101` | 1–65535 |
| `ntrip_mount` | `""` | 1–64 characters, no slash |
| `ntrip_user` | `""` | at most 64 characters |
| `ntrip_pass` | `""` | empty means "leave unchanged" |
| `ntrip_fallback_host` | `""` | 0–64 characters; empty disables the fallback |
| `ntrip_fallback_port` | `2101` | 1–65535 |
| `ntrip_fallback_mount` | `""` | empty, or 1–64 characters without a slash |
| `ntrip_fallback_user` | `""` | at most 64 characters |
| `ntrip_fallback_pass` | `""` | empty means "leave unchanged" |

Control characters are rejected in every text field. Hardware keys such as
`tx_pin`, `uart_id`, and `http_port` are intentionally not settable: a
self-made POST could otherwise configure the board unbootable, recoverable only
over the REPL.

`wifi_networks` is handled alongside them by `validate_wifi_networks()`: a list
of at most 8 `[ssid, password]` pairs, each SSID 1–32 characters, each
non-empty password 8–63 characters, no duplicates, no control characters. The
setup form never returns stored passwords, so a line containing only an SSID
keeps the stored password and a line of the form `ssid:password` replaces it.

The remaining keys are code defaults, adjustable only over the REPL or a
hand-edited `config.json`:

| Key | Default | Meaning |
|---|---|---|
| `uart_id`, `uart_baud` | `1`, `115200` | Receiver UART |
| `rx_pin`, `tx_pin` | `17`, `18` | UART pins on the ESP32-S3 |
| `uart_rxbuf_bytes` | `16384` | Driver receive buffer |
| `status_led_pin` | `2` | Status LED |
| `http_port` | `80` | Portal port |
| `nmea_queue_size` | `512` | Shared NMEA input queue depth |
| `nmea_tcp_port` | `10110` | Canonical NMEA-0183 TCP port |
| `nmea_tcp_legacy_port` | `2947` | Compatibility listener |
| `nmea_tcp_legacy_enabled` | `true` | Whether the legacy port is opened |
| `nmea_tcp_max_clients` | `4` | Simultaneous NMEA TCP clients |
| `nmea_tcp_auth_required` | `true` | Require `AUTH <stream token>` first |
| `ble_mtu_size`, `ble_mtu_max` | `20`, `247` | Initial and offered BLE MTU |
| `ble_commands_enabled` | `false` | Whether BLE writes reach the receiver |
| `ap_ssid` | `ESP-RTK-Setup` | Overwritten at boot with the per-device SSID |
| `ap_pass` | `""` | Empty generates a random password on first boot |
| `ap_ip` | `192.168.4.1` | Setup access-point address |
| `ap_channel_switch_interval_sec` | `60` | How often the AP may yield the radio to a station attempt; `0` gives the AP absolute priority |
| `ntrip_version` | `1` | `2` adds the `Ntrip-Version: Ntrip/2.0` header |
| `ntrip_connect_timeout_sec` | `15` | Deadline for name resolution plus TCP connect |
| `ntrip_header_timeout_sec` | `10` | Deadline per response header line |
| `ntrip_read_timeout_sec` | `5` | Deadline for one read from the stream |
| `ntrip_idle_timeout_sec` | `15` | Silence after which the stream is reconnected |
| `ntrip_retry_delay_sec` | `5` | Base of the reconnect backoff |
| `ntrip_retry_max_sec` | `300` | Cap of the reconnect backoff |
| `ntrip_max_retries` | `0` | `0` means unlimited |
| `ntrip_gga_interval_sec` | `10` | Rover position upload interval; `0` disables it |
| `ntrip_max_epoch_age_sec` | `15` | MSM epoch age above which the correction stream counts as stale (`rtcm.stale`, `ntrip_substate`); see [Correction ages](#correction-ages) |
| `ntrip_reconnect_on_stale_sec` | `0` | Reconnect once the stream has been stale for longer than this many seconds; `0` never reconnects |
| `gps_utc_offset_sec` | `18` | GPS-UTC leap seconds, used to convert the device clock to GNSS time when MSM epochs are decoded |
| `wdt_enabled`, `wdt_timeout` | `true`, `15000` | Hardware watchdog; `wdt_timeout` 10000–600000 ms, a value outside loads as the default with a `config_warnings` entry |
| `heartbeat_timeout_sec` | `60` | Per-task liveness deadline; `0` watches only the event loop |
| `gc_interval_ms` | `10000` | Garbage-collection interval |
| `fix_update_interval_ms` | `950` | Minimum interval between published fixes |
| `fix_stale_after_ms` | `3000` | Age of the last published fix after which it counts as stale (`stale`, `fix_age_ms`; see [Live fix and freshness fields](#live-fix-and-freshness-fields)) |
| `gsv_limit_per_sec` | `8` | Cap on forwarded GSV sentences |
| `telemetry_interval_sec` | `1` | `$PESPS` interval; `0` disables it |
| `log_level`, `log_mute` | `INFO`, `GC` | Severity filter and muted subsystem |
| `ntp_sync` | `true` | Enables the NTP request each time the Wi-Fi station connects; it is deferred while a line records or a measurement runs. With it off, the receiver's RMC time still sets the clock while it is invalid (`_sync_rmc_clock`); only a device with neither source keeps an unsynchronised clock that counts from 2000-01-01. The 2000-based epoch of `recorded_at` is a time scale and does not depend on this key |
| `hostname` | `""` | Overwritten at boot from the identity |
| `config_token` | `""` | Obsolete; read from an old config and kept until a factory reset, never used for authorization. `status_token` is gone: an old value is ignored on load and dropped by the next save |

The four keys `ntrip_max_epoch_age_sec`, `ntrip_reconnect_on_stale_sec`,
`gps_utc_offset_sec`, and `fix_stale_after_ms` belong to this second table.
They are not in `SETTABLE_KEYS`, so they are not settable through
`PUT /api/config`; they change only by editing `config.json` (which is read
at start, so restart afterwards) or over the REPL.

The measurement acceptance keys are listed in
[Survey data](#survey-data), because they are validated and exposed as one
group. The three field-setup keys `antenna_height_m`, `reference_frame`, and
`reference_frame_epoch` are not in `SETTABLE_KEYS` either; they have their own
endpoint, described in [Field setup](#field-setup).

### Field setup

Three keys describe the measuring set-up and are stamped into every new
measurement. They are validated as a group by `validate_field_setup()` and
served by `GET`/`PUT /api/field-setup`:

| Key | Default | Validation | Meaning |
|---|---|---|---|
| `antenna_height_m` | `0.0` | a number or numeric text from 0 to 3 m, rounded half up to whole centimetres; booleans, `NaN`, and infinities are rejected | Distance from the ground point up to the antenna phase centre: pole length plus the antenna's offset between its reference point and its phase centre |
| `reference_frame` | `""` | text of at most 40 characters, no control characters, whitespace stripped | Frame of the correction service (the RTK base or network); empty means "not configured" |
| `reference_frame_epoch` | `""` | text of at most 16 characters, same rules | Optional epoch of that frame |

`GET /api/field-setup` needs a session and returns
`{"values": {...}, "defaults": {...}, "suggestion": ...}`. `PUT` needs a session
and a CSRF token, takes a JSON object with any subset of the three keys (a key
that is left out keeps its value), and answers with the same shape. An unknown key, a wrong type,
or an out-of-range value is refused with `400` and the code
`invalid_field_setup`; a failed write answers `500` with `save_failed` and
leaves the previous values in force; while an unreadable `config.json` is
protected (see [Storage and recovery](#storage-and-recovery)) the answer is `503
config_file_unreadable`. Unlike `PUT /api/config`, the change is not
staged: it is written to `config.json` at once, needs no restart, and applies to
the next measurement. Points that are already stored are never changed.

`suggestion` is `null` unless the correction service of the open NTRIP stream
announced its coordinate reference system in RTCM 1300 (see [RTCM
statistics](#rtcm-accounting)). Otherwise it is an object, the same one that
`/api/ui/live` carries as `frame_suggestion`:

| Field | Meaning |
|---|---|
| `reference_frame` | the announced frame name: printable ASCII, at most 31 characters, stripped, never `unknown` or `mixed` in any case, so it passes `validate_field_setup` unchanged |
| `reference_frame_epoch` | the announced epoch as text (`"2010.0"`, `"2010.5"`, `"2010.25"`), `""` when the service gave none or a value outside 1988.00 to 2100.00 |
| `anchor` | `"plate_fixed"` or `"global"` from an RTCM 1302 whose name equals `reference_frame`, otherwise `null` |
| `state` | how the suggestion compares with the saved field setup, see below |

`state` is `new` while `reference_frame` in the field setup is empty. It is
`matches` when the saved frame equals the announced name exactly (no case
folding) and the announced epoch is empty or equal to the saved one. In every
other case it is `differs`, also when only the epoch differs and when the saved
setup has no epoch while the service announces one. Only RTCM 1300 produces a
suggestion; a 1302 alone appears in `/status` and `/rtcm` only.

A suggestion is only information. Accepting it is an ordinary `PUT` with
exactly `{"reference_frame", "reference_frame_epoch"}` of the suggestion the
operator saw; there is no endpoint of its own and no server-side token, and the
configuration stays unchanged until that `PUT`. If the stream announces something
else between display and click, only the values that were shown are saved.

The default antenna height is `0.0`, so a device that was never set up cannot
tell "not configured" from a zero-length pole: it stamps 0 mm, and
`ground_height_msl_m` then equals `altitude_msl_m`.

A hand-edited `config.json` is checked at load by the same validator as the
endpoint (`cfg_validation.validate_field_setup`, applied key by key; every other
key still goes through `cfg_store.coerce`). A stored value it rejects - text
that is not a number, `null`, `true`, a height outside 0 to 3 m, an overlong or
control-character frame text - is dropped: the key keeps its default instead of
a silent `0.0`, a `WARN` line is logged, and the rejected key with the reason is
listed in `config_warnings` of `/status` and `/api/ui/live` until the next
restart. Values that pass are quantised exactly like the endpoint does
(`"1.805"` becomes `1.81`). The next configuration save writes the default over
the rejected value; a file from before schema 3 is re-saved right at that load,
so the typo is gone from the file at once and the warning (and the `WARN` log
line) is all that remains; that re-save writes the default over every rejected
key, not only the field setup.
While the warning for `antenna_height_m` is listed, measurements stamp no
antenna height (`null`, no ground height) rather than the default `0.0`, which
would read as a configured zero; a successful `PUT /api/field-setup` for that
key clears the warning. The same load check covers `ntrip_port` and
`ntrip_fallback_port` (a whole number from 1 to 65535; booleans, fractions, and
non-finite values are rejected) and `wdt_timeout` (a whole number from 10000
to 600000 ms; the upper bound keeps `machine.WDT()` from raising at boot,
which it does from 2^31 ms on): a rejected value loads as the default with a `WARN` line and a
`config_warnings` entry. A stored `ntrip_port: 2101.7`, which was silently
truncated to 2101 before, is now rejected the same way. In a file of schema 3
or later, a rejected port or timeout stays in the file until the next
configuration save. A live or caster key sent with `PUT /api/config` (or
`/save`) while its warning is listed is saved even when the value equals the
default already in RAM, which repairs the file and clears the warning. Check
`config_warnings` after editing the file by hand. When
a measurement is stamped, the value in the configuration is validated once more
(`_configured_antenna_height_mm`); a value that is still invalid stamps no
antenna height (`null`) and is logged once per distinct value.

The 40 and 16 character limits are enforced by `validate_field_setup`, for
the endpoint and for the configuration loader alike. As a last guard,
`_reference_frame` and `_reference_frame_epoch` still cut the text at 80
characters, the longest text that `_validate_measurement` accepts for a stored
measurement.

From `schema_version` 2 on, these three keys are the `field_setup` section of
the [configuration backup](#configuration-backup). A factory reset still does
not clear them (`FACTORY_CLEAR_KEYS` names credentials only).

### Pending and last-good configuration

Every configuration key has an apply policy (`APPLY_POLICY` in `cfg.py`; 38
keys are `live`, 11 `task_restart`, 23 `verified_reboot`):

| Policy | Keys | When a change takes effect |
|---|---|---|
| `live` | measurement gate, field setup, TCP authentication and other runtime settings | saved to `config.json` at once and used by the next reader of the value |
| `task_restart` | the eleven `ntrip_*` caster settings | saved to `config.json` at once and handed to the running NTRIP client, which reloads them without a restart (see *Reloading the configuration*); `restart_required` is true only while a handover has not succeeded (no client, as in safe mode, or the reloader failed) |
| `verified_reboot` | `wifi_ssid`, `wifi_pass`, `wifi_networks`, ports, pins, access point and watchdog settings | held in RAM only until `POST /api/config/apply`, then confirmed by a reboot and rolled back if the device does not come back |

`PUT /api/config`, `/save`, and the restore all run the request through
`prepare_config_change()` and then through `stage_config()`. `live` and
`task_restart` keys are saved right away through `apply_live()`; changed
`verified_reboot` keys wait in `_STAGED`. `CONFIG`, `config.json`, and the
transaction files do not see a staged key, and a reboot before the apply
drops it, so no restart can apply an unconfirmed Wi-Fi change. A key set back
to its current value leaves the staging. The response of `PUT /api/config`
carries `pending` (something is staged), `restart_required`, `applied` (the
keys saved now), and `staged`.

`POST /api/config/apply` arms the staged change. It answers `409
nothing_to_apply` when nothing is staged and no `task_restart` value is still
waiting for a handover to the NTRIP client, `409 config_verification_running` while the network manager is
connecting (access state `CONNECTING`: each connection round takes at most
about a minute; a failed verification boot takes up to two rounds plus ten
seconds), and `409 open_change_pending` while a change is still
open, that is, while any pending file exists, including one whose rollback is
not complete. `/save` and the restore refuse in the same way, before they
validate anything. The restore and `/save` build their own candidate from the
request and the active configuration and drop an unapplied `PUT` staging; if
they fail, nothing of their change stays staged.

`apply_staged_config()` arms the change in this order: the result `pending`,
then three atomic writes:

1. the currently persisted configuration to `config.last_good.json`,
2. the candidate to `config.pending.json`,
3. the candidate to `config.json`.

If any step fails, the files written so far are removed, the previous result
is written back, the staging is kept, and the call reports failure, so the
device never boots from a half-applied change. Without staged keys (only a
`task_restart` value whose handover failed waits) the current configuration is armed, so the same
verified reboot confirms it. The web handler then restarts the device.

After the restart, `NetworkManager.run()` decides. If a pending configuration
exists and the station connects, the link must additionally survive ten further
one-second checks - a short association flap must not confirm wrong
credentials. Only then does `commit_pending_config()` record `connected`
together with the SSID that worked and delete both staging files. A commit
confirms only a change that is in `config.json`; if a power cut kept the
candidate from landing, the result is `failed` with `details.code`
`apply_interrupted` and the change is rolled back. A commit never confirms
after a `failed` result has been stored (or when the result file cannot be
read): it completes that rollback instead.

If the connection fails or drops during the ten seconds,
`rollback_pending_config()` first records the decision, then completes it:

- The decision is the result `failed` with `details.code` (the first cause
  wins), `details.restored` (the keys that go back), and never a `connected`
  before it. The code is the Wi-Fi error category (`access_point_not_found`,
  `authentication_failed`, `association_failed`, `connection_timeout`),
  `connection_lost` (the link dropped during the stability check),
  `setup_mode`, `apply_interrupted`, or `storage_error` (the retry finished a
  rollback that was opened without a known cause and no `failed` decision had
  been stored, for example after an unreadable result file).
- `complete_rollback()` writes the `config.last_good.json` values of the
  changed `verified_reboot` keys into `config.json` and `CONFIG`, verifies the
  file, and only then deletes the staging files. It is idempotent: it runs at
  start-up before the network manager, in the verdict, and when setup mode is
  entered, and a power cut at any point is finished by the next run. Only the
  changed `verified_reboot` keys go back; the measurement gate, field setup,
  TCP authentication, and caster settings saved in the meantime stay.
- As long as the rollback is not complete (`rollback_open()`), the network
  manager keeps the access point on (state `SETUP` while the setup window
  runs, otherwise `RECOVERY`), makes no station attempt, and tries to complete
  the rollback once a minute (`ROLLBACK_RETRY_SEC`). Apply, `/save` and restore
  are refused with `409 open_change_pending` meanwhile; `PUT /api/config` and
  the live PUTs still save (Wi-Fi keys are only staged in RAM). After success the manager connects
  with the restored values.

Entering setup mode (BOOT held for 3 s, or no network configured) on a
verification boot skips the verdict, so an armed candidate is rolled back at
once with the code `setup_mode` instead of being joined unconfirmed when the
setup window ends. A `pending` result without a pending file at start (the
power failed while arming) becomes `failed` with `apply_interrupted`. The
transaction files are recovered after a power cut like `config.json`
(temporary and previous copies); a recovery that fails counts as an open
change and blocks every new one until a later recovery succeeds. If `config.last_good.json` is unreadable
after the recovery, the transaction ends without a restore (an `ERROR` log
line) and the unconfirmed candidate stays active. The file formats are
unchanged.

The machine-readable outcome lives in `config.apply.result.json` and is served
by `GET /api/config-result`. It carries `schema_version`, `status`
(`none`, `pending`, `connected`, or `failed`), and `details`. `uptime_sec` is the
device uptime when the result was written. A result file that cannot be read
reads internally as `unknown`; the endpoint shows it to clients as `pending`
with `details.code` `result_unreadable`, never as a final outcome. Human-readable
text is deliberately not stored: presentation belongs to the browser's
translation dictionary. The result also carries `boot_id` (the device start in
which it was written, see *Guided apply in the field app*) and, when the
request that caused it sent one, its `apply_id`.

#### Guided apply in the field app

The configuration form of the field app (Device -> Connections) does not send
a form blindly. It runs
these steps, all on top of the endpoints above:

1. **Check changes.** The page compares the form with the apply policy and
   with the staged Wi-Fi keys the device already holds, and lists what takes
   effect at once, what restarts the device, and what already waits in the
   device memory. The `PUT /api/config` carries only the changed keys, so Wi-Fi can be set up while no caster is
   configured yet, and the query `staging_revision` of the page's last view.
   Whether the device restarts is decided by `restart_required` of the
   answer: a caster change that the running client took over needs no restart.
2. **Staging revision.** The device counts a revision that rises with every
   change of the staged set. `GET /api/config` reports `staging` (`revision`,
   `keys`, `restart_required`, never values). `PUT /api/config` and
   `POST /api/config/apply` answer `409 staging_changed` when the client sent a
   `staging_revision` and the staging changed since; a request without one is
   accepted as before. The page then reads the staging again and asks again.
3. **Request identity.** The browser creates an `apply_id` per request and sends it
   with apply, restore (query) and `POST /api/reboot` (body). The device writes
   it into the `pending` entry, the final result inherits it, and the answer
   echoes it. A request without one stays valid.
4. **Boot identity.** `system.boot_id` in the live snapshot and `boot_id` in
   the result are random per device start and live in RAM only. `boot_count`
   is a crash counter and no start identity. A result of another start than the one
   the page watches is never taken for the outcome of its request.

The progress panel shared by apply, restore and restart shows the steps HTTP,
Wi-Fi, NTRIP and survey storage with the elapsed time. Each step has its
source: HTTP answers from `GET /api/session` or the live snapshot; Wi-Fi from
the live snapshot and the result; NTRIP "Switched off" only when
`ntrip_config` reports `switched_off`, neutral before the client has started,
and an error with the next step when the client is parked after
`retries_exhausted`; survey storage from the storage state. The wait begins before the request is
sent, so a lost answer does not lose the start. The panel does not poll
and does not redirect to the sign-in page while it waits.

Sessions live in RAM, so the browser has to sign in again after every restart.
The panel resumes after that sign-in from `sessionStorage` of the starting tab:
no password, no caster user name, only the SSID, the caster host, the
mountpoint, the apply and boot ids, and at most 10 minutes. The result and the readiness of the device are judged separately; the
readiness is given 60 s. Without a matching answer after 90 s the page says which network to
join and which address to open.

The outcome is judged by identity. A result with the `apply_id` of the request
(and a new `boot_id`) is the result of this request. A lost answer to the
request leaves the outcome uncertain: the page never repeats a request on its own and
reports an observed restart. A result without a matching `apply_id`
is shown as observed and neutral, and a result of the same device start without
marker is only a hint.

After a failed verification the page separates three things: the keys marked
to be set back (`details.restored`, written before the rollback finishes), the
settings the device reports (RAM values, which are the default values while
`config.json` is protected), and the Wi-Fi state. The device does not report
the end of the rollback.

The page warns before apply, restore and restart while a measurement or line
recording runs and asks for "Restart anyway". The device has no restart lock;
the API, `/save` and `/setup` restart without one.

### Configuration backup

`GET /api/config/backup` returns a JSON document of kind
`esp-rtk-config-backup` containing the settable keys, `wifi_networks`, and a
`credentials` block with the access-point password and the device code.
`POST /api/config/restore`, `PUT /api/config`, and `/save` validate through the
same `prepare_config_change()`: the settings, at most 8 fallback networks, the
password lengths, and for keys that have one the same field check the loader
applies. The restore then saves the result as its own candidate (see
[Pending and last-good configuration](#pending-and-last-good-configuration))
and restarts the device.

The backup is `schema_version` 3. Next to the settable keys it carries a
`measurement` section (`gate`, the eight rules, and `samples`), a
`field_setup` section (antenna height, reference frame and epoch) and a
`stakeout` section (`tolerance_m` and `fine_radius_m`, the values of
`GET /api/stakeout/settings`). Survey data is not part of it. `schema_version`
must be the whole number 1, 2 or 3 (not `true`, `3.0` or `"3"`); every section
is validated before anything is written. The `stakeout` section of a schema 3
backup must hold both values; they pass the same validator and the same pair
rule as `PUT /api/stakeout/settings`, so a backup with a tolerance not below the
fine radius is refused whole. A schema 1 backup from an earlier version still
restores and leaves the measurement rules, method and field setup unchanged. A
schema 2 backup restores as before, ignores a `stakeout` section if one is
present and leaves the stakeout settings unchanged. An older firmware refuses a
schema 3 backup (`400 invalid_backup`, or 413 for its smaller body limit) and
changes nothing. The restore answer carries `schema_version`, the restored
`sections` and the `kept` sections.

The restore checks the connection part with `prepare_restore_change`: the
same rules as `prepare_config_change`, plus an empty caster host and mountpoint
(NTRIP switched off or not set up), empty secrets that mean empty rather than
"keep", and an empty primary SSID when fallback networks exist. A backup without
any Wi-Fi network leaves the device's own Wi-Fi access unchanged and reports
`kept: ["wifi"]`. The settings forms, `PUT /api/config` and `/save` stay on the
strict `prepare_config_change`.

The field app names the scope of the backup next to the download: the
device configuration with the measurement defaults and the field setup; survey
projects are exported separately. After a restore it reports a refused, too
large or partly saved backup and, after the restart, whether the backup came
from older firmware or kept the device's Wi-Fi.

The restore route has its own body limit, `CONFIG_RESTORE_BODY_LIMIT`
(16384 bytes in `web_routing.py`); all other configuration routes keep 4096.
Every backup the firmware writes for a valid configuration fits, with all fields
at their validator limits, eight fallback networks and NTRIP passwords of up to
1024 characters. After a Wi-Fi rollback only `verified_reboot` keys are rolled
back: the measurement rules, method and field setup of the backup stay, also
after `500 stage_failed`, once the live part is saved.

**A configuration backup contains Wi-Fi and NTRIP credentials in clear text.**
It does not belong in public storage or in a support ticket.

### Factory reset

Holding the BOOT button for `FACTORY_RESET_HOLD_MS` (10 s) within the
`FACTORY_RESET_WINDOW_MS` (12 s) sampling window triggers a factory reset;
holding it for `SETUP_MODE_HOLD_MS` (3 s) only forces setup mode. The button is
sampled once at start-up and the routine returns immediately if it is not
pressed, so a normal boot costs nothing. The button must be pressed *after*
power is applied — holding it while power arrives starts the ROM bootloader and
the firmware never runs.

A factory reset clears `FACTORY_CLEAR_KEYS` — both Wi-Fi credential fields, the
configured network list, both NTRIP user and password pairs, and the two
obsolete tokens — saves the configuration, rotates the stream token, and
deletes the BLE bonds, the staged configuration files, and the event journal
(`tracking.index.json`, `tracking.checkpoint.json`, and the
`tracking.segment-*.jsonl` segments). **The point store
(`tracking.points-v1.jsonl` and its generation files) and `tracking.archives/`
are not touched by a factory reset: measured points and archived projects
survive it.** Delete projects in the portal before handing a device on (see
[known limitations](known-limitations.md)). The field-diagnostics recordings
(`field-diagnostics-*.jsonl`, `field-diagnostics.state`) are not touched
either, and the portal offers no way to delete them: the field-diagnostics
endpoints only start, stop, mark, or export the recorder. Recordings leave
the device only through the recorder's own oldest-first rotation, once 32
segments of 128 KiB exist or sooner under flash pressure, so existing
recordings can still be present when a device is handed on; erase the
filesystem or reflash it to be sure they are gone. Hardware keys such as
`tx_pin` are kept, since a wrong pin makes the device unusable and has nothing
to do with credentials. The device identity, and therefore the device name,
hostname, and device code, is retained, so the physical label stays valid.

## Networking

### Station and access point

The ESP32-S3 has one radio for both interfaces, so an active access point
forces the station onto the AP's channel. If the AP came up on its own channel
before any station connection, the station could never find a router on a
different channel and the device stayed locked in AP mode.

The compromise is in `_may_disable_ap()`. The AP is switched off for the first
station attempt, and afterwards only once per
`ap_channel_switch_interval_sec`. It is never switched off while a client is
associated with it, because losing the portal mid-form is worse than a late
connection. Setting the interval to `0` gives the AP absolute priority: it
starts first and never goes away, at the price of a station that may never
associate.

`configured_networks()` returns the primary network first, followed by the
entries of `wifi_networks` with duplicate SSIDs removed. With more than one
candidate, a scan is used to *skip* networks that are not currently visible —
each futile attempt costs seconds — but the order stays the configured one, and
if none of the configured networks appears in the scan, all of them are tried
anyway, because a scan can miss a hidden network. At most four are tried per
round, and each attempt is bounded: 15 s for a single configured network, 12 s
each when several are configured, so a round takes at most about a minute. No
scan runs while a line records or a measurement runs; all configured networks
are then tried in order. A scan in AP+STA mode can briefly disturb clients of
the access point.

The order is never signal strength. Sorting by field strength sounded
reasonable but made the choice unpredictable: with two reachable networks the
device landed sometimes on one and sometimes on the other, and therefore under
changing addresses. A connected fallback also stays connected until it is
actually lost.

After a failed round the driver is explicitly disconnected. `wlan.connect()`
enables automatic reconnection on the ESP32, and a driver quietly hopping
channels in the background makes the configuration AP unusable and starves BLE.

The maintenance loop runs every five seconds. It also adopts a connection the
driver re-established on its own: without that, `isconnected()` could be true
again while `wifi_connected_event` stayed clear and the NTRIP task waited
forever. Reconnect attempts after a loss use `backoff_delay(attempt, 60, 300)`.

### Access states

`app.set_access_state()` accepts exactly `CONNECTING`, `ONLINE`, `SETUP`,
`RECOVERY`, `APPLYING`, and `ERROR`. `SETUP` means no network is configured or
the BOOT button requested it. `SETUP` holds for 10 minutes (`SETUP_HOLD_MS`)
with no station attempts while scans keep working; then normal operation resumes
and `force_setup` is cleared. If the setup access point fails to start, it is
retried in the next maintenance pass and then at most once a minute
(`ROLLBACK_RETRY_SEC`), during the window and, with no network configured,
after it, as in the rollback guard. `RECOVERY` means configured networks exist but
none could be joined, and the AP is up so the operator can fix it; `APPLYING` is
the short window between a staged change and the restart.

### Wi-Fi error categories

A failed station attempt is classified from the driver status code and
published in `app.stats["last_wifi_error"]`, next to the raw
`wifi_status_code`. `GET /api/ui/live` exposes both under `network`, and the
portal turns them into one comprehensible warning.

| Category | Cause |
|---|---|
| `access_point_not_found` | driver status 201 |
| `authentication_failed` | driver status 202 |
| `association_failed` | driver status 203 |
| `connection_timeout` | any other status, or none reported |
| `connection_lost` | an established link dropped |

A `connect()` call that raises immediately — typically an internal driver error
after a previous failure — does not produce one of these categories. The
interface is deactivated, paused briefly, and reactivated, and the attempt is
recorded in the field diagnostics recorder as `driver_error`.

The log line for a timed-out attempt is deliberately phrased identically every
time and contains no network name, so repeated field retries collapse in the
persistent log instead of rotating unrelated failures out of it. Network names
are treated as credentials and never enter the persistent or serial logs.

### Discovery

`discovery.py` implements both protocols directly, without external packages.

mDNS answers on `224.0.0.251:5353` for the interface currently in use — the
station address when connected, otherwise the AP address while a setup portal
is active. It serves an `A` record for `<hostname>.local`, `PTR` records for
`_http._tcp.local` and `_nmea-0183._tcp.local` including the
`_services._dns-sd._udp.local` enumeration, and `SRV` plus `TXT` records for
both service instances. The TXT record carries `id=<device_id>`. If the
MicroPython build reserves port 5353 for its own responder, the task detects
this and leaves `.local` resolution to `network.hostname()`.

Captive DNS binds port 53 only while the access state is `SETUP`, `RECOVERY`,
or `APPLYING`, and answers every `A` or `ANY` query with the AP address. The
known captive-portal probe paths (`/generate_204`, `/gen_204`,
`/hotspot-detect.html`, `/library/test/success.html`, `/ncsi.txt`,
`/connecttest.txt`, `/redirect`) are redirected to
`http://<ap_ip>/#device/configuration` rather than claiming that Internet
access exists. The redirect is absolute so that the portal window runs on the
AP address, for which the origin rules of the setup mode apply. It opens the
field app at Device > Connections > Internet & corrections (the old
`#device/configuration` is an alias, see [Field App routes](#field-app-routes));
without a session the sign-in page comes
first, and after signing in the field app opens the requested target.
`/api/captive` names the same URL and answers the RFC 8908 shape. The basic
page `/setup` stays reachable as a marked fallback, linked from the sign-in
page, for web views that cannot run the field app. If the build supports it, the
AP also advertises the portal through
DHCP option 114.

Routers and phone hotspots do not always forward mDNS. When `.local` does not
resolve, use the current DHCP address instead; it is not stable without a
reservation.

## NTRIP

### Endpoints

`NtripMachine.configured_endpoints()` returns an ordered list. It is empty when
`ntrip_enabled` is false, and an endpoint is included only when both its host
and its mountpoint are set. The primary endpoint uses the `ntrip_` keys, the
optional fallback uses the independent `ntrip_fallback_` keys with its own
host, port, mountpoint, user, and password.

The request is a plain HTTP/1.0 `GET /<mountpoint>` with
`User-Agent: NTRIP ESP32-S3/1.0`, `Connection: close`, and HTTP Basic
authorization. `Ntrip-Version: Ntrip/2.0` is added only when `ntrip_version`
is 2 or higher, because some casters require it and others reject it.

The log lines and the connection state that report which endpoint is in use
(`ntrip_endpoint` in the statistics) name only `primary` or `fallback`; the
password is never exposed, and the diagnostics package redacts the user names
and passwords of both endpoints. The configuration is visible to a signed-in reader, however: `GET /rtcm`
returns `host` and `mount`; `rtcm.mount` (the primary mountpoint) appears in
`GET /status`, `GET /api/ui/live`, and `GET /api/diagnostics`; `GET /api/config`
returns the host, port, mountpoint, and user of both endpoints (the passwords
only as `*_pass_set` flags); and the mountpoint in use is stored in every
measurement and printed in every export. The guards differ: `/status`, `/rtcm`,
and `/api/ui/live` check `_read_allowed`, which requires a device-code
(administrator) session on both the AP and the STA interface and ignores any
`token` parameter; `GET /api/config` and `GET /api/log` require an administrator
session (`_admin_allowed`). `GET /api/diagnostics` also needs an administrator
session.

### Reloading the configuration

The connection rules (endpoint list and index, retry counter, backoff, parking,
stale substate) live in `NtripMachine`, which has no socket code;
`NtripClient` owns the sockets and calls `NtripClient.reload()` through the
task reloader that `main.py` registers with `cfg.set_task_reloader()`.

**Triggers.** `apply_live()` calls the reloader after it has saved a changed
`task_restart` value (`PUT /api/config`, `/save`, and the restore take this
path). A full rollback that replaced the caster values (for example after an
unreadable `config.json`) hands them over as well, also when the cleanup of the
staging files failed, and a handover that failed earlier is repeated at the
next rollback retry. A saved value is never handed over before it is stored. A
successful handover clears the restart flag, whatever left it set earlier. Safe
mode creates no client, and a failing reloader leaves the flag set: both
keep answering `restart_required: true`, as before. Staged Wi-Fi keys need the
verified restart independently of this.

**Effect.** `reload()` asks the machine to read the endpoints again
(`NtripMachine.load()`: one reading of `CONFIG` for the endpoint list and
`ntrip_config`). What happens next depends on the situation:

| Situation | Effect |
|---|---|
| Open connection to an endpoint that is no longer configured (any change of host, port, mountpoint, user, password, or switching NTRIP off) | The connection is closed at once, which ends a blocked `readline`, `read`, or `drain` (request and GGA upload) on the spot. |
| Open connection to an endpoint that is unchanged (for example only the fallback changed) | The client stays connected and keeps its counters. It takes the change over at the next I/O return; there is no general time bound for that. The 5 s of `ntrip_read_timeout_sec` applies only at the normal read wait point with the default configuration, and `drain()` has no deadline of its own on MicroPython 1.28. |
| Waiting slice (idle, Wi-Fi, parked, backoff) | At the next slice, at most 5 s. |
| Connection setup in progress | The attempt ends right after the setup returns, without sending a request: at most `ntrip_connect_timeout_sec` (15 s by default). |
| Name resolution | Synchronous, blocks the whole loop, and no deadline covers it; a documented limit, unchanged. |

After a reload that changes the endpoint list, the next attempt starts at once
(no backoff) at the primary caster with a fresh counter. A parked client is
released and a client that was idle because NTRIP was off or incomplete runs
normally (and the reverse); `ntrip_config` and `ntrip_state` follow from the
same reading. The attempt ended by the reload is not counted: no `ntrip_error`,
no `ntrip_backoff`, no `last_ntrip_error_code`, and `ntrip_retries` stays as it
is. The device protocol receives exactly one `ntrip_end` event with
`reason: "reload"` for each attempt a reload ended; merely leaving the idle
state produces none. The log says "Configuration changed; reconnecting with the saved caster settings.", "Configuration changed; NTRIP client idle (<state>).", or, when the active caster is unchanged, "Configuration changed; the active caster is unchanged.".

Shutdown takes precedence over a reload. The stream can end in the middle of an
RTCM frame, exactly as with an EOF. The `live` keys (`ntrip_*stale*`,
`ntrip_max_retries`, the timeouts, the GGA interval, `ntrip_version`) are read
where they are used and trigger no reload.

Taking the configuration over is not a successful caster connection: whether
the new settings work is decided by the caster, and the state shows it
(`connecting`, `streaming`, `auth_error`, ...).

### Status line handling

The first line from the caster is classified once:

| Response | Error code | Treated as |
|---|---|---|
| `ICY 200` or `HTTP/1.x 200` | — | stream accepted |
| `401` | `auth_error` | permanent |
| `404`, or a `SOURCETABLE` reply | `mount_error` | permanent |
| any other numeric status | `caster_error` | transient |
| unparsable first line | `protocol_error` | transient |

A permanent error does not heal by retrying. When the last attempt of a round
over all configured endpoints ends with one, the client goes straight to the
slowest interval (`ntrip_retry_max_sec`) instead of hammering the caster every
five seconds; only that last attempt of the round decides.

The two permanent errors are published twice: as `app.stats["ntrip_state"]`
(`auth_error` or `mount_error`) and as the error code. `caster_error` and
`protocol_error` are transient: they are recorded only as
`app.stats["last_ntrip_error_code"]`, while the state goes from `connecting` to
`backoff`. The state set therefore stays at the seven values `disabled`,
`waiting_wifi`, `connecting`, `streaming`, `backoff`, `auth_error` and
`mount_error`.

Every settled attempt leaves its outcome as a code in `last_ntrip_error_code`,
including a stable stream that ends with `eof` or `idle_timeout`, which does
not count as a failed attempt. The first data of the next connection clears it
again.

| Code | Meaning |
|---|---|
| `auth_error` | status 401 (permanent) |
| `mount_error` | status 404 or a `SOURCETABLE` reply (permanent) |
| `caster_error` | any other numeric status |
| `protocol_error` | the first line could not be parsed |
| `timeout` | connect or header timeout |
| `network_error` | DNS failure, network error, reset, or a failed request write |
| `unexpected_error` | any other exception |
| `idle_timeout` | the stream stopped delivering data (see *Silent-stream detection*) |
| `eof` | the caster closed the stream |
| `stale_backlog` | the client closed the stream on purpose (see *Stale stream and backlog reconnect*) |
| `retries_exhausted` | the retry limit was reached; the client is parked, see *Failover and backoff* |

### Failover and backoff

Every connection attempt ends with an outcome. Only two outcomes count as
success: a stream that was still delivering data at least 60 s after its first
data block, and a stale-backlog reconnect. Status 200 alone is not success. A
header timeout after 200, an idle stream, or a stream that ends within 60 s of
its first data is a failed attempt.

A failed attempt increments the retry counter, moves to the next configured
endpoint and waits `backoff_delay(attempt, ntrip_retry_delay_sec,
ntrip_retry_max_sec)`: 5, 10, 20, 40, 80, 160, then 300 s with the defaults.
With two endpoints the attempts alternate between primary and fallback and the
wait follows the same sequence, so the fallback is tried after the first five
seconds. If the last attempt of a round (the one before the next primary
attempt) ended with a permanent error, the client waits `ntrip_retry_max_sec`
instead. A success, on either endpoint, resets the counter to zero and makes the
primary caster the next one to try, after the base delay
`ntrip_retry_delay_sec`. This is a controlled failback: a working
fallback is never silently made permanent. Every wait is slept in slices of at
most five seconds so the task keeps producing heartbeats. A cancelled task
passes the cancellation on without counting the attempt or waiting.

`ntrip_max_retries` is a per-endpoint cap; the effective limit is that value
times the number of configured endpoints. The default `0` means unlimited. When
a non-zero limit is reached, the client is parked: `ntrip_state` stays
`backoff`, or `auth_error`/`mount_error` when the last attempt ended with that
permanent error, `last_ntrip_error_code` is `retries_exhausted`,
`ntrip_backoff_sec` is 0, and the log says "client parked until restart or Wi-Fi loss". No further
attempts are made until the device restarts, a configuration apply reboots it,
a change of the caster settings is reloaded (see *Reloading the configuration*),
or the Wi-Fi connection is lost; then the counter starts from zero and
`last_ntrip_error_code` is cleared. A Wi-Fi
interruption shorter than about five seconds may go unnoticed (see
[known limitations](known-limitations.md)). `ntrip_retries` counts every failed
attempt and every stale-backlog reconnect.

### Silent-stream detection

A caster that accepts the TCP connection and then says nothing is the failure
mode that a plain socket read cannot detect. Three bounded deadlines cover it:
`ntrip_connect_timeout_sec` around the connection setup (name resolution and
TCP connect),
`ntrip_header_timeout_sec` per header line, and `ntrip_read_timeout_sec` per
read from the stream. A read timeout is not an error by itself — the task
records it, emits a heartbeat, and keeps the GGA upload alive, because a VRS
caster may be waiting for the next rover position. Only when the silence
reaches `ntrip_idle_timeout_sec` is the connection torn down and rebuilt. A
clean EOF reconnects immediately.

Name resolution and TCP connect run together inside that one deadline but are
timed separately (`ntrip_flow.dns_ms` / `dns_max_ms`, `connect_ms` /
`connect_max_ms`); an unrecognized resolver address format makes the measurement
unseparated (`dns_ms` is `null`, `dns_unseparated` counts it, `connect_ms` covers
the whole attempt).

A silent receiver is detected on the other side of the same device. The GNSS
loop stamps every UART read that delivered bytes; reads that return nothing do
not count. When no byte has arrived for `fix_stale_after_ms` (3 s by default),
`link.receiver` becomes `silent` and the loop writes one `WARN` line to the log
(`Receiver UART silent for N s; check the receiver cable and power.`) and a
`receiver_link` event to the field recorder. When data resumes, one `INFO` line
and one event follow. The loop checks after every read and also when reading the
UART raises an error, so a receiver that never sends a byte or a UART that keeps
failing is reported as well. A receiver that is silent from the start warns once
the grace period has passed; the first byte after a normal start is not logged.
The device neither restarts a task nor resets the watchdog because of it, and
the `gnss` heartbeat keeps running. The live snapshot evaluates the state itself
whenever it is read, so an event loop blocked for more than 3 s by flash work can
show `silent` for a moment without a log line; bytes waiting in the UART buffer
renew the stamp before the loop checks.

### Rover position upload

While a stream is running, the GGA sentence of the published fix (the same
epoch as `fix_age_ms`) is written back to the caster every
`ntrip_gga_interval_sec` and once immediately after the request, because VRS
and nearest-mountpoint services deliver nothing without a position. A sentence
is sent only while the fix is fresh (younger than `fix_stale_after_ms`, 3 s) and
carries a fix (quality above 0, and so does the most recently received GGA).
Otherwise nothing is sent and the caster keeps the position it was last given:
a stale position, a position without a fix, or a literal 0/0 would make the
caster build a virtual station in the wrong place.

The upload resumes at the next due check, that is after a completed read or a
read timeout once `ntrip_gga_interval_sec` has passed since the last attempt,
provided a fresh fix is available then; a withheld attempt still counts as an
attempt. Consequently a reconnect without a fresh fix (receiver silent or
without a fix) sends no initial GGA, and a VRS or nearest-mountpoint stream
delivers nothing until a position is available again.

### RTCM accounting

Incoming bytes are written to the receiver's UART first and counted afterwards,
so corrections reach the module without a detour. `RtcmCounter` parses RTCM3
framing and reports message types, totals, the age of the last message, and the
age of the last decoded MSM epoch (see [Correction ages](#correction-ages)).
MSM families are derived from the message number — 1071–1077 GPS, 1081–1087
GLONASS, 1091–1097 Galileo, 1101–1107 SBAS, 1111–1117 QZSS, 1121–1127 BeiDou —
and the offset inside each block of ten gives the family MSM1 to MSM7. The
summary is served by `GET /rtcm`, appears as `rtcm` in `/status` and
`/api/ui/live`, and is summarised in `$PESPS`. It describes what was received,
not what the receiver managed to use. The CRC of a frame is not checked for the
statistic: the parser only follows the length field, which is enough for counts.
The one exception is RTCM 1300 and 1302, described next.

#### Service CRS (RTCM 1300 and 1302)

A correction service may announce the coordinate reference system of its
corrections. `RtcmCounter` decodes two messages, and only after their CRC24Q
(over `0xD3`, both length bytes and the whole payload, compared with the three
trailing bytes) has passed; all other message types stay unchecked and are
counted as before. Bit offsets count from the first payload bit:

| Message | Field | Bits | Meaning |
|---|---|---|---|
| 1300 Service CRS | DF002 | 12 | message number |
| | DF562 | 5 | N, name length |
| | DF563 | 8 x N | name |
| | DF564 | 16 | CE, epoch in 0.01 a |
| 1302 RTCM CRS | DF002, DF565 | 12, 5 | message number, N |
| | DF566 | 8 x N | name |
| | DF567 | 1 | anchor: 0 global, 1 plate fixed |
| | DF149 | 5 | plate number (0 unknown) |
| | DF568 | 3 | number of database links (not read) |

A payload shorter than `33 + 8N` bits (1300) or `26 + 8N` bits (1302), a 1300
longer than 40 bytes, and a name that is not printable ASCII (`0x20` to `0x7E`),
empty after stripping, or `unknown` or `mixed` in any case are rejected. The
epoch is `1900 + CE / 100` with two decimals and a trailing `0` removed
(`CE 11000` is `"2010.0"`, `11050` is `"2010.5"`, `11025` is `"2010.25"`). It is
accepted from 1988.00 to 2100.00 (`CE` 8800 to 20000); `CE 0` and every other
value give `epoch: null`, and the raw value stays in `epoch_raw`. The field
layout follows pyrtcm 1.2.1 (RTCM 10403.3 Amendment 2) and the BKG NTRIP client
BNC, whose epoch basis is used here; the standard text itself was not available.

`GET /status` (under `rtcm`) and `GET /rtcm` (at the top level) report three more
keys; `summary()` and the existing keys are unchanged:

| Key | Content |
|---|---|
| `service_crs` | `{name, epoch, epoch_raw, age_sec}` of the last valid 1300, or `null` |
| `rtcm_crs` | `{name, anchor, plate, database_links, age_sec}` of the last valid 1302, or `null` |
| `crs_frames` | `{crc_failures, rejected}`: 1300/1302 frames with a wrong CRC, and CRC-valid ones that could not be shown. Both survive reconnects, like the message counts |

`age_sec` is the time since the last copy of the announcement arrived; an
identical repeat only refreshes it. Lifetime: the announcement belongs to the
open stream that carried it. It is dropped at every new handshake, at the end of
every connection attempt (end of stream, error, reload, switch-off) and, at once
and before any new stream exists, when the caster settings change the running
endpoint or switch NTRIP off; a reload that keeps the endpoint keeps it. There is
no timeout. A later CRC-valid 1300 (or 1302) that cannot be shown withdraws the
earlier one (`rejected` + 1); a CRC failure leaves the earlier one in place
(`crc_failures` + 1). The field diagnostics sample carries `ntrip.service_crs`,
the stored 1300 object or `null`.

### Correction ages

Three different ages describe the corrections. They answer different questions
and are not interchangeable; this section is the one definition the other
documents refer to.

| Age | Measured by | Published as | Feeds the acceptance gate |
|---|---|---|---|
| **Arrival age**: time since the last RTCM frame of any message type reached the device | the device, on its tick clock (`RtcmCounter.last_msg_ms`) | `rtcm.arrival_age_sec` (also `rtcm.last_msg_age_sec`, the same value), and `rtcm_stream_age_s` in `$PESPS` | no |
| **Epoch age**: time between the GNSS epoch stamped inside the last decoded MSM message and now | the device, from the MSM epoch and the device UTC clock | `rtcm.epoch_age_sec`, `rtcm.epoch_source`, `rtcm.stale`, and `ntrip_substate` | no |
| **Correction age**: the age of the corrections the receiver reports in GGA field 13 | the receiver | `correction_age_sec` in the fix and in every measurement | yes (`corrections_too_old`) |

The **arrival age** proves that the link delivers bytes. It says nothing about
how old the content is: a caster that replays a backlog keeps the arrival age
near zero while every message in it is old.

The **epoch age** looks inside the stream. The epoch field of an MSM message
(message numbers 1071 to 1137, families MSM1 to MSM7) is converted to the
device clock: GPS, Galileo, QZSS, SBAS, and IRNSS count time of week in GPS
time, which is UTC plus `gps_utc_offset_sec`; BeiDou time is GPS time minus
14 s; GLONASS carries a day of the week and the milliseconds of the day in
Moscow time, which has no leap seconds. The age is the difference modulo one
week (one day when GLONASS reports day 7); an epoch that lies ahead of the
device clock counts as age 0. The value is stored with the tick of its decode
and keeps growing between messages. Rules that follow from this:

- `epoch_source` names the constellation of the last MSM message whose epoch
  was decoded and plausible (`GPS`, `GLO`, `GAL`, `SBAS`, `QZSS`, `BDS`, or
  `IRNSS`). "Last" means last in arrival order, not newest by epoch: the cache
  holds one value, follows whichever constellation was decoded last, and does
  not compare epochs. An older epoch that arrives after a newer one therefore
  raises the reported age (a fresh GPS message followed by a 60 s old BeiDou
  message turns an age of 1 s into 60 s), and with several constellations
  interleaved the value can change between them from one message to the next.
  The epoch is read from the message header; the counter does not check the
  CRC.
- `epoch_age_sec` and `epoch_source` are `null` until the first plausible epoch
  of the current connection has been decoded, and while the device clock is
  invalid (outside the years 2024 to 2099). They are also `null` once the
  cached epoch has expired because the tick counter wraps (about 6.2 days
  without a new epoch); the next decoded epoch restores them. The epoch cache
  is dropped when a new connection starts; the counts, the byte total, and the
  arrival age survive reconnects.
- `rtcm.stale` is `true` when the epoch age exceeds `ntrip_max_epoch_age_sec`.
  A missing epoch age is not stale.
- The device clock has a resolution of one second and is the reference. It is
  set by NTP when the Wi-Fi station connects or reconnects (if `ntp_sync` is on;
  while a line records or a measurement runs the sync is deferred and caught up
  after it ends) and by RMC time: while it is invalid and when it deviates from
  RMC by more than 1 s (a one-second difference is sub-second phase and is
  ignored, also at the hourly check). An NTP sync of an already valid clock that
  moves it by more than 1 s counts as a step too. Each step is logged and
  recorded as a `clock_resync` event (reason `drift` or `ntp`); the cached epoch
  age is dropped at the step and the next MSM frame restores it. Intervals (fix
  age, GGA interval, epoch age growth) run on ticks and do not move with the
  step.
- The epoch age does not enter the acceptance gate. It is a diagnostic: it shows
  that the caster delivers old data even though bytes keep arriving.

The **correction age** is the receiver's own statement. The firmware copies
GGA field 13 as a number (`correction_age_sec`; empty field: `null`) and the
gate rule `measurement_max_correction_age_sec` compares it with the limit. A
GGA whose field 13 is empty passes that rule, and for an averaged set the
stored value is the one of the last sample. Treat the receiver's value as a
statement to be checked against the epoch age, not as ground truth.

#### Stale stream and backlog reconnect

While a stream is running, every read loop pass evaluates the epoch age. When
it exceeds `ntrip_max_epoch_age_sec`, `app.stats["ntrip_substate"]` becomes
`"stale"`; when it falls back, or when the connection ends or a new one starts,
it becomes `null` again. `ntrip_state` itself stays `streaming`: a stale stream
is a substate of a working connection, not a connection state. The value is
published as `stats.ntrip_substate` in `/status` and as
`network.ntrip_substate` next to `network.ntrip_state` in `/api/ui/live`.

With `ntrip_reconnect_on_stale_sec` set to a positive value, a stream that has
been stale for longer than that many seconds is closed and rebuilt, because a
fresh connection drops the backlog the caster had queued. The reconnect counts
in `ntrip_retries`, stores the error code `stale_backlog`, and then follows the
normal path: the state goes to `backoff` for the base delay
(`ntrip_retry_delay_sec`, because a stale-backlog reconnect counts as success
and resets the retry counter) and the primary caster is tried first. The
default `0` never reconnects. Without a valid device clock no epoch age exists, so neither the substate nor the reconnect can trigger.

## Survey data

### Model

A project holds lines and asset points. A line holds an ordered list of
coordinates with one measurement record each. An asset point holds a
coordinate, an object type, a name, a note, and — when it is linked to an
active line — its chainage, lateral offset, side, and projected coordinate.

| Limit | Value |
|---|---|
| Projects | 20 |
| Lines per project | 500 |
| Active features (line vertices plus asset points) | 5000 |
| Custom properties per project or line | 32 |
| Object types | `tap`, `valve`, `hydrant`, `branch`, `transition`, `control`, `tree`, `repair`, `line_start`, `line_end`, `other`; `stakeout` only through a stakeout confirmation |

`TARGET_MAX_FEATURES` (10000) is the value the compact schema is designed for;
`MAX_FEATURES` (5000) is what the firmware enforces. See
[performance-and-limits.md](performance-and-limits.md) for the measured
behaviour behind that distinction.

`GET /api/tracking` reports capacity as `used`, `limit`, `remaining`,
`percent`, and a `level` of `ok`, `notice`, `warning`, `critical`, or `full`,
together with the free flash, the required reserve, the estimated temporary
requirement, and the active and archived project counts. The thresholds are 80,
90, and 98 percent of the active limit, except at a limit of exactly 10000,
where the fixed values 8000, 9000, and 9800 are used. The browser shows the
capacity compactly at `ok`, as a message from `notice`, and permanently from
`critical`.

### Coordinates and heights

Under schema 5 a stored coordinate is the integer triple
`[lon_e8, lat_e8, alt_mm]`: longitude and latitude in units of 1e-8 degree
(about 1.1 mm) and the height in integer millimetres. The firmware builds the
triple from the receiver's GGA text with integer arithmetic
(`geo.parse_nmea_coord_e8`, `geo.parse_decimal_fixed`) and never from a float,
because the MicroPython float is 32 bits wide and resolves a latitude only to
about 0.4 m. Averages, spreads, and distances are computed from the integers
(`geo.mean_e8`, `geo.mean_lon_e8`, `geo.distance_e8_m`), so identical samples
have a spread of exactly zero. A sample without them is refused; the gate
admits none. `_validate_coordinate()` accepts only real
integers inside the range of ±180 and ±90 degrees and -20 000 m to 100 000 m;
a float or a boolean is refused. The live fix carries the same exact fields
next to the float display values; see
[Live fix and freshness fields](#live-fix-and-freshness-fields).

The `precision` field of every measurement says how much the coordinate can be
trusted as a number:

| `precision` | Meaning |
|---|---|
| `e8` | Measured by firmware that stores schema 5. The integers come from the receiver's NMEA text (rounded half up once at 1e-8 degree), or are the integer mean of an averaged set. |
| `legacy_float32` | Converted from schema 4 or older. Those versions stored 32-bit floats, so the integers are no better than that grid (about 0.4 m in latitude); the conversion cannot restore what was never stored. |

Exports print the integers as exact decimal text: eight fraction digits for
degrees and three for metres.

The firmware does not transform coordinates. The frame they refer to is
recorded per measurement; see [Reference frames](#reference-frames).

An altitude is never manufactured: a fix without one is rejected rather than
stored with a zero that would look like a measurement in an export.

The height chain of one measurement is:

| Field | Source | Unit |
|---|---|---|
| `altitude_msl_m` | third value of the coordinate (`alt_mm`): GGA field 9, the orthometric height above mean sea level of the **antenna phase centre**, according to the receiver's geoid model | m |
| `geoid_separation_m` | stored `geoid_sep_mm`: GGA field 11 | m |
| `altitude_ellipsoid_m` | stored `alt_ellipsoid_mm`: `alt_mm` plus `geoid_sep_mm`, present only when both exist | m |
| `antenna_height_m` | stored `antenna_height_mm`: the field-setup value at the time of the measurement; see [Field setup](#field-setup) | m |
| `ground_height_msl_m` | `altitude_msl_m` minus `antenna_height_m`; `null` without a stored antenna height | m |
| `height_reference` | the constant `antenna_phase_centre_msl` | text |

The stored fields are integer millimetres (`geoid_sep_mm`, `alt_ellipsoid_mm`,
`antenna_height_mm`); the metre fields are derived on output. An averaged
measurement averages the geoid separation like the height and has none when a
sample lacks it. The raw receiver heights are never altered: the antenna height
is only subtracted for `ground_height_msl_m`.

### Reference frames

The firmware does not know the frame of the correction service unless the
service announces it in RTCM 1300; it is then offered for the open stream (see
[Field setup](#field-setup)), never applied. Otherwise the operator declares it
in the field setup. Each measurement stores `reference_frame` and
`reference_frame_epoch` from `_reference_frame()` and `_reference_frame_epoch()`:

| Fix quality (GGA field 6) | `reference_frame` | `reference_frame_epoch` |
|---|---|---|
| 4 (RTK fixed) or 5 (RTK float) | the configured `reference_frame`, whitespace stripped, `null` when empty | the configured epoch, or `null` |
| any other nonzero quality | `WGS84/ITRF` | `null` |
| 0 (no position) | `null` | `null` |

The field-setup endpoint accepts at most 40 characters for the frame and 16
for the epoch; text that was edited into `config.json` by hand is cut at 80
characters, the limit `_validate_measurement` accepts. Exports print a missing
frame as `unknown`. A frame label is a statement by the
operator, not a verified fact; a wrong or forgotten label stamps every RTK
point wrongly. The label is stamped when the fix is queued, so a change made
while a route fix waits in the queue does not apply to it. A line export
carries `reference_frame` (the single frame of its vertices, `mixed` for
several, `unknown` for none) and the `reference_frames` tally; the project
overview in `GET /api/tracking` carries `frames`, `mixed_frames`, and
`unknown_frames`. The exported `crs_note` states that no coordinate is
transformed.

The frame epoch needs one more word. For a plate-fixed frame such as ETRS89,
enter the epoch that the correction service states for its coordinates. With an
ITRF-based service the coordinates refer to the time of observation, which every
point already stores as `recorded_at_utc`; no epoch is entered then (see
*Reference frame epoch* in [known-limitations.md](known-limitations.md)). The
Field App reminds the operator before recording when an RTK fix meets an empty
frame in the field setup (see [Field App routes](#field-app-routes)).

#### Frame assignment

An RTK measurement (fix quality 4 or 5) stored without a frame, as before
V15.9.0 or with an empty field setup, can be given a frame afterwards for the
whole project. The assignment is a statement by the operator about the
correction service. Nothing is transformed and no stored measurement is
rewritten: the readers apply the assignment (`tracking._effective_frame`). A
stored frame always wins, and a measurement without an RTK solution stays
`unknown`.

The assignment is kept in the project as `frame_assignment`:
`reference_frame`, `reference_frame_epoch` (`null` when empty), `assigned_at`,
`lines` and `points`. The scope is a count, not a list of IDs. `lines` maps a
line ID to `[n, c]` and `points` is `[k, c]`: the first `n` vertices of the
line (the first `k` object points) were in the project when the frame was
assigned, and `c` of them are RTK measurements without a stored frame. Only
those `c` measurements take the frame; points measured later are outside the
scope and stay `unknown` until the next assignment. A new assignment replaces
the old one and is computed over all points again. Its preview counts the points
of the earlier assignment too and reports `new_unknown`, those not yet assigned.

`frame_assignment_preview` scans the project in small steps (at most 64
vertices or 8 line windows per scheduler step, so the device stays responsive)
while holding a reader, and answers `total`, `new_unknown`, `vertices`,
`points`, up to five `lines` as `{id, name, count}` with `more_lines`,
`other_unknown` (unknown points that are left alone), the current `assignment`
and a `scope_token`. The token fingerprints what the scan saw: the project ID,
the point-store generation, each line's vertex count and last locator, the
object point count with the last point's ID, and the current assignment.
`assign_reference_frame` takes that `scope_token` and `expected_count` (the
previewed `total`), scans again and refuses with `frame_scope_changed` when
the token or the count differ, so nothing is assigned when the points changed
between the check and the confirmation. A project with nothing to assign is
refused with `nothing_to_assign`; an assignment with `count 0` can therefore
not be created, but undoing the last assignable point leaves one standing with
`count 0` (`[n-1, 0]` for a point and the line dropped from `lines`).

The frame and epoch follow the field-setup text rules (at most 40 and 16
characters); the frame is required, and `unknown` and `mixed` are reserved.
The assignment is refused while a line of the project is recording
(`recording_active`).

`revoke_reference_frame` takes the `assignment_token` of the assignment the
caller saw: a fingerprint of the frame, epoch, time and the whole scope. After
an undo inside the scope the token changes and a revocation with the old one
answers `frame_assignment_changed`. Revoking gives the earlier state back,
including the byte-identical backup, since the stored measurements were never
changed.

Undo of a vertex inside the scope shrinks the scope in the same step: the
`feature_removed` event carries `frame_scope` (1: the vertex was assignable, 0:
it was not), because a replay cannot read a vertex that the point store has
already dropped. The journal is synchronised before the point store forgets the
vertex. After a power cut the replay therefore never finds an undone vertex
still counted in the scope. If the point store lost the vertex later than the
journal recorded it, the replay removes it again; a replacement point measured
after the undo stays `unknown`.

Every change is the journal event `project_frame_assigned` with the new
assignment (`null` for a revocation), the `previous` frame, epoch, time and
count, and the `count`. The checkpoint holds the assignment, so restart,
compaction, backup, restore and archives keep it. A restore checks each stored
assignment against the restored points: the texts must be clean, `n` and `k`
must not exceed the restored counts and every `c` must equal the exact number
of assignable measurements among the first `n` (`k`); otherwise the backup is
refused with `A frame assignment is invalid.`. A hand-edited checkpoint with a
damaged assignment reads as no assignment and never stops the boot.

The frame source of an exported or listed measurement is
`reference_frame_source`: `recorded` (stored with the measurement), `assigned`
(from the project's assignment) or `null` (unknown). The project overview
counts (`frames`, `mixed_frames`, `unknown_frames`) and the line frames follow
the assignment.

An export reads the assignment once, when it starts, and every chunk checks that
the project still holds that very assignment object (assignments are replaced,
never changed in place). When the assignment changes, an assigned point is
undone or the project is replaced or deleted meanwhile, a GeoJSON or backup
download stops incomplete (no closing records, so the backup has no integrity
footer) and the server logs `frame_export_changed`. The CSV is built in one step
and cannot be split. `archive_project` answers `409 frame_export_changed` in
that case and archives nothing.

### Recording modes

Every line carries a `recording_mode`. A line restored from an older backup
without the field is treated as `survey`. The mode is preserved in the backup,
the status response, the line detail, and the GeoJSON export.

**`survey`** applies the full acceptance gate. A vertex is stored only when its
fix — or the mean of an averaged set — passes every rule.

**`route`** records continuously without prior averaging and persists every
complete automatic fix together with its positive or negative quality verdict,
so a rejected sample keeps its rejection reasons instead of disappearing.
Rejected route points are counted in `route_rejected_points`, and fixes whose
coordinate failed validation in `route_invalid_fixes`.

A route does not start recording the moment the receiver powers up.
`RouteStartupGuard` requires a valid device clock, a fix of quality 1, 2, 4, or
5, at least 6 satellites, an HDOP of at most 3.0, and 10 s of continuous
stability. A gap of more than 3 s between samples, or an implied speed above
60 m/s, resets the guard to `settling`. Until it reports `ready`, automatic
route vertices are refused with a clear reason.

### Acceptance gate

`quality_check()` evaluates one fix against the eight configurable rules. All
eight are exposed as one group by `GET /api/measurement-gate`, replaced by
`PUT`, and reset to the factory values by `DELETE`. A `PUT` that changes the
rules must supply all eight fields.

The gate limits are cached rather than rebuilt per check, and each published
fix version is judged once: a GST that completes the same fix takes over the
verdict, while a different GGA version of the same UTC time is judged again.

**Measurement method.** `measurement_samples` (`1`, `5`, `10` or `quality`;
factory value 5) is the device-wide default for new measurements. It is
not part of a gate profile: it has no profile ID and never appears in
`gate_deviations`. `quality` measures until the gate holds over
`measurement_quality_window_samples` consecutive samples (see
[Measurement modes](#measurement-modes)); the ratio of the two values decides
how long that takes. The endpoint carries it additively: `GET`, `PUT` and
`DELETE /api/measurement-gate` answer with `samples` and `samples_default`
(5) next to `values`, `defaults` and `deviations`. A `PUT` body is valid as
the eight rules alone (`samples` stays), the eight rules plus `samples` (one
save), or `{"samples": ...}` alone (the rules stay); an empty body, a subset of
the rules or an unknown key answers `400 invalid_measurement_gate`. `DELETE`
resets only the measurement rules and keeps `samples`. The recording actions
still take their own `samples` (`1` when missing).

| Rule | Configuration key | Factory value | Accepted range |
|---|---|---|---|
| Required fix type | `measurement_required_fix` | `RTK_FIXED` | `RTK_FIXED`, `RTK_FLOAT`, `DGPS` |
| Satellites | `measurement_min_satellites` | 10 | 4–60 |
| HDOP | `measurement_max_hdop` | 1.5 | 0.1–20.0 |
| Correction age | `measurement_max_correction_age_sec` | 5.0 s | 0.1–120.0 s |
| Horizontal spread of an averaged set | `measurement_max_horizontal_spread_m` | 0.05 m | 0.001–10.0 m |
| GST horizontal 1σ | `measurement_max_gst_horizontal_sigma_m` | 0.05 m | 0.001–10.0 m |
| GST vertical 1σ | `measurement_max_gst_vertical_sigma_m` | 0.10 m | 0.001–20.0 m |
| Rolling window for the open-ended mode | `measurement_quality_window_samples` | 5 | 3–30 |

The required fix type maps to the GGA quality indicators `RTK_FIXED` → 4,
`RTK_FLOAT` → 4 or 5, `DGPS` → 2, 4, or 5.

The check reports `accepted` plus a list of machine-readable reasons, which the
portal renders. The reasons are `no_position`, `rtk_fixed_required` or
`required_fix_not_met`, `too_few_satellites`, `hdop_too_high`,
`altitude_required`, `corrections_too_old`, `not_all_samples_rtk_fixed`,
`horizontal_spread_too_high`, `gst_required`, `not_all_samples_have_gst`,
`gst_horizontal_error_too_high`, and `gst_vertical_error_too_high`.

The gate reads only the exact fields: `no_position` when `lat_e8` or `lon_e8`
is not an integer within ±90 or ±180 degrees, `altitude_required` when
`alt_mm` is not an integer from -20 000 m to 100 000 m. This is the rule
`_validate_coordinate()` applies when a point is stored, so a fix the gate
accepts can always be saved. The float `lat`, `lon` and `alt` never decide.

Two rules apply only to averaged sets. Every sample must carry a GST error
estimate; a set with missing estimates fails with `not_all_samples_have_gst`.
Every sample must meet the required fix type — the stricter
`not_all_samples_rtk_fixed` is raised only when the configured profile actually
demands RTK FIXED, so lowering the profile genuinely lowers the requirement.

The correction-age rule compares GGA field 13 (the third age in
[Correction ages](#correction-ages)) with `measurement_max_correction_age_sec`.
A fix whose field 13 is empty is not rejected by it, and the MSM epoch age is
not part of the gate.

Each measurement stores the verdict (`quality.accepted`, `quality.reasons`) and
a `profile_id`, a stable hash of the limits in force at that moment (FNV-1a over
the limits as JSON with sorted keys, so a host and a device derive the same ID
for the same limits; an ID stored by an earlier version is kept and never
rewritten, and a restore keeps the ID a backup carries when it still resolves to
the same limits). The limits
themselves are kept once per profile in the backup header (`profiles`) and are
expanded again on output: `quality.limits` per measurement, and `gate_profile`
and `gate_limits` on every asset point feature of the GeoJSON export. In the
limits object the required fix type appears as `fix_quality`. A point therefore
keeps the limits it was judged by even after the gate is changed.

`gate_deviations` lists the limits that are **looser** than the factory value,
in the order of the table above, as objects `{"key", "value", "default"}` with
the key without the `measurement_` prefix. A lower fix type
(`RTK_FIXED` → `RTK_FLOAT` → `DGPS`), fewer satellites or a smaller rolling
window, and any larger maximum count as looser; stricter values are not listed.
An empty list means the gate is at or stricter than the factory profile. The
list is served as `gate_deviations` by `/api/ui/live`, `/status`, and
`GET /api/tracking`, and as `deviations` by `GET`, `PUT`, and `DELETE
/api/measurement-gate`.

The verdict is one cached value (`app.gate_state()`), so the endpoints agree
with each other. Two situations shape it:

- A GGA normally arrives before the GST of the same epoch. While GST output is
  running and the only missing item is that GST, the previous verdict is held
  for up to 2 s (`GST_GRACE_MS`) and `pending_gst` is `true`; after the grace
  the epoch is judged without it (`gst_required`).
- A fix older than `fix_stale_after_ms` is judged like no fix at all, and
  measurements refuse it with "GNSS position is stale".

These factory values are starting points. They must be validated against known
control points for the actual antenna and mounting before the results are
trusted.

### Measurement modes

`MeasurementSession.collect(samples)` (`measurement.py`) drives the measurement:

- `samples = 1` returns the current fix directly.
- `samples` between 2 and 30 collects that many *distinct* accepted fixes,
  bounded by a deadline of `max(15 s, samples × 4 s)`, and returns their mean.
  If too few arrive in time, the attempt fails and names the blocking reasons.
- `samples = "quality"` runs open-ended over a rolling window of
  `measurement_quality_window_samples` accepted fixes and returns as soon as the
  mean of that window passes the gate. It has no deadline and is stopped with
  the `cancel_measurement` action.

Progress is published by `GET /api/tracking?view=progress`: phase, collected
and rejected sample counts, elapsed seconds, the mean GST sigmas, the mean
altitude, and the reasons currently blocking acceptance.

An averaged measurement stores, in addition to the mean position: sample count,
duration, the number of RTK FIXED samples, the number of samples meeting the
required fix type, and the mean and maximum horizontal and vertical deviation
from the mean. The GST block records how many samples carried an estimate,
how many were expected, and the mean and maximum horizontal and vertical sigma.

Every stored measurement — averaged or not — carries these fields
(`Tracker._measurement()`; the unit is the stored one):

| Field | Content |
|---|---|
| `recorded_at` | seconds since 2000-01-01T00:00:00Z; see [Timestamps](integration.md#timestamps) |
| `fix_quality`, `fix_status` | GGA field 6 as a number and as text (`RTK_FIXED`, `RTK_FLOAT`, `DGPS/SBAS`, `GPS_FIX_2D/3D`, …) |
| `satellites`, `hdop` | satellites in use and HDOP from the GGA |
| `correction_age_sec` | GGA field 13; `null` when the field is empty (the correction age of [Correction ages](#correction-ages)) |
| `station_id` | GGA field 14 as text, leading zeros kept |
| `accuracy_observation` | repeatability statistics of an averaged set |
| `receiver_accuracy` | the GST error estimate (`source` `NMEA_GST`) |
| `quality` | the gate verdict: `accepted` and `reasons` |
| `profile_id` | the gate profile the verdict was judged by; the limits are expanded on output |
| `precision` | `e8` or `legacy_float32`; see [Coordinates and heights](#coordinates-and-heights) |
| `geoid_sep_mm`, `alt_ellipsoid_mm` | integer millimetres, `null` when GGA field 11 or 9 was missing |
| `antenna_height_mm` | integer millimetres from the field setup; `null` when invalid |
| `reference_frame`, `reference_frame_epoch` | see [Reference frames](#reference-frames) |
| `ntrip_mount` | the mountpoint of the caster in use while the stream was up, otherwise `null` |
| `source` | on line vertices only: `manual`, `automatic`, or `start` |

The mountpoint name is part of every export. It is not a credential, but it
identifies the correction service; remove it before an export is published
if that matters.

### Asset points and control points

An asset point recorded while a line is active is projected onto that line and
stores its chainage, signed lateral offset, side (`left`, `right`, or
`on_line`), and the projected coordinate.

Asset points of type `control` that share a name are grouped into a
`control_checks` entry in the status response: the number of measurements, the
horizontal distance and the signed vertical difference between the first and
the most recent observation, and both timestamps. Recording the same known
point twice therefore yields a before/after figure. That is a repeatability
check against the earlier observation, not a comparison against an absolute
nominal coordinate.

`vertical_reference` says what the vertical difference compares. When both
observations carry a stored antenna height it is `ground`: the ground heights
are compared, so a pole change between the two observations is not reported as
movement of the mark. Otherwise (a point recorded before V15.9.0 has no
antenna height) it is `phase_centre`, and the difference is between the MSL
heights of the antenna phase centre, including any pole change.

`GET /api/tracking?view=name_check&kind=point|line&name=...` reports whether a
name is still free within the active project, so the portal can warn before a
duplicate is recorded.

### Exports

`GET /api/tracking/export` serves four formats. GeoJSON, the backup, and an
archive are streamed, so a large project is never materialised in RAM. The CSV
is the exception: `tracking.csv()` builds every row in a list, joins them into
one string, and `web.py` sends that complete payload, so the heap must hold the
list and the joined text at once. That is a few hundred bytes per asset point,
about twice over, which is small for a typical project but grows with the
number of asset points; for a very large project prefer GeoJSON or the backup.

| `format` | Content type | Content |
|---|---|---|
| *(omitted)* | `application/geo+json` | GeoJSON `FeatureCollection` of lines and asset points, with the recording mode and the full measurement record per feature; see below |
| `csv` | `text/csv` | One row per asset point (line vertices are not in the CSV), 38 columns; see below |
| `backup` | `application/x-ndjson` | Complete NDJSON survey backup with a header record (schema 5, the gate profiles), one record per project, line, vertex, and asset point, a footer, and a SHA-256 integrity record. Coordinates are the stored integer triples; each measurement is fully expanded and carries the derived time fields. Backup, restore, backup is content-identical except `record_order`, which a restore renumbers in backup order (see [Archives and restore](#archives-and-restore)); a further round trip is identical; the bytes, and therefore `content_sha256`, match only where the JSON writer keeps key order, as on a host, because the device writes object keys in hash order |
| `archive` | `application/x-ndjson` | One previously archived project, identified by `archive_id` |

Coordinates in GeoJSON and CSV are exact decimal text built from the stored
integers (`geo.format_e8`, `geo.format_mm`): eight fraction digits for degrees
and three for metres. The GeoJSON `[lon, lat, alt]` keeps the MSL height of the
antenna phase centre as its third value, for compatibility with existing
consumers; RFC 7946 expects an ellipsoidal height there. The ellipsoidal height
is the separate property `altitude_ellipsoid_m`, and `crs_note` repeats this in
the file.

**GeoJSON.** The collection carries `name`, `schema_version` (5), `crs_note`,
and, when the project has a frame assignment, `frame_assignment`
(`reference_frame`, `reference_frame_epoch`, `assigned_at_utc`, `count`). A line feature is a `LineString` (a `Point` for one vertex, `null`
for none) whose properties hold `line_id`, `name`, `object_type`
(`survey_line`), `recording_mode`, `state`, `length_m`, the custom line
properties, `measurements[]` (one expanded measurement per vertex with the
height fields, `recorded_at_utc`, `clock_unsynced`, and the frame fields
`reference_frame`, `reference_frame_epoch` and `reference_frame_source`),
`reference_frame`, and `reference_frames`. An asset point feature is a `Point` whose properties hold
the point's own fields (`id`, `object_type`, `name`, `note`, `line_id`,
`chainage_m`, `lateral_offset_m`, `side`), the expanded `measurement`, the
height fields (`altitude_msl_m`, `altitude_ellipsoid_m`, `geoid_separation_m`,
`antenna_height_m`, `ground_height_msl_m`, `height_reference`), `reference_frame`,
`reference_frame_epoch`, `reference_frame_source`, `ntrip_mount`, `station_id`,
`gate_profile`, `gate_limits`, and `projected_coordinate` (an exact coordinate triple, or
`null`). The measurement inside carries `precision` and `recorded_at_utc`.
Where a legacy point never stored a field, the GeoJSON shows
`altitude_ellipsoid_m`, `geoid_separation_m`, `antenna_height_m`, and
`ground_height_msl_m` as `null` and `reference_frame` as `unknown`; the CSV
leaves those cells empty and prints the frame as `unknown`. `altitude_msl_m`
keeps the stored phase-centre height.

**GeoJSON read and send.** Each line is read in two sequential passes (the
frame count and the output) over the point set captured when the line begins,
so an undo or a new point while the file is being written does not change that
line in this file. The sender hands over to other services at the next chunk
boundary once more than 20 ms of export work have passed; a single flash access
has no upper bound of its own. A drain that does not complete ends the export
after 120 s, so a client that stopped receiving does not hold the storage. Every
export logs one `EXPORT` line with the outcome (`complete` or `interrupted` and
the error), `bytes`, `ms`, `yields`, `slice_max_ms`, `produce_ms`, `read_ms`,
`decode_ms`, `drain_ms`, `yield_ms` and `ram_free_min`. `slice_max_ms` is the
longest stretch between the sender's own yields and includes waiting in drains,
so it is not event-loop lag (use `stats.loop_lag_ms` for that). `read_ms` and
`decode_ms` also count parallel readers of the point store. `ram_free_min` is
a rough probe sampled about once a second, not an exact minimum. The CSV stays
a point export.

**Portal export flow.** The export sheet of a project starts GeoJSON from a
button instead of a link. The portal reads the response body itself: it shows
the phase (the device preparing, the device sending), the elapsed time and the
amount received, and a Cancel button. No percentage is shown because the size
is not known in advance. There is no overall deadline; only 30 s without any
data while the page is visible end the run with a message. The clock of a hidden
page does not count, because browsers pause timers there. The portal scans the
bytes for the closing bracket of the outermost JSON object and offers the file
only when it has closed; an interrupted body says so and offers no file. When
the export is ready, **Share file** (where the browser can share files) and
**Download file** hand the file over with a separate tap. The portal reports
that the file was handed to the share dialog or the browser and does not claim
that it was saved. The file stays in the browser memory until **Discard file**,
closing the sheet or leaving the view. Closing the sheet while a run is active
(close button, Escape or Back) asks first, through the sheet's close hook
`guardExportSheetClose`: **Keep exporting** or **Cancel export**. Closing when
nothing is running, or after the run has ended, does not ask. Leaving the view
ends a run without asking and reports the cancellation in the status dock. CSV
and archive stay direct links and the backup is unchanged; none of them detects
a truncated download (a restore checks a backup).

**CSV.** The 39 columns, in order, are `id`, `type`, `name`, `longitude`,
`latitude`, `altitude_msl_m`, `altitude_ellipsoid_m`, `geoid_separation_m`,
`antenna_height_m`, `ground_height_msl_m`, `height_reference`, `line_id`,
`chainage_m`, `lateral_offset_m`, `side`, `fix_status`, `satellites`, `hdop`,
`correction_age_sec`, `samples`, `rtk_fixed_samples`,
`horizontal_max_deviation_m`, `vertical_max_deviation_m`, `gst_samples`,
`gst_horizontal_sigma_mean_m`, `gst_horizontal_sigma_max_m`,
`gst_vertical_sigma_mean_m`, `gst_vertical_sigma_max_m`, `station_id`,
`ntrip_mount`, `reference_frame`, `reference_frame_epoch`,
`reference_frame_source`, `gate_profile`, `precision`, `recorded_at`,
`recorded_at_utc`, `clock_unsynced`, and `note`.
Heights are written with three decimals and an unknown height is an empty
cell, never `0`. A point that was not averaged is one sample:
`samples` reads `1`, `rtk_fixed_samples` reads `1` only for an RTK fixed
solution (fix quality 4) and `0` otherwise, and `gst_samples` reads `1` only
when the point carries a GST accuracy and `0` without one. An averaged point
reports its stored counts. `recorded_at_utc` and `clock_unsynced` are
explained in the *Timestamps* section of [integration.md](integration.md).

Fields that are internally stored in compact form are fully expanded in every
export. In the CSV, text cells starting with `=`, `+`, `-`, or `@` are
prefixed with an apostrophe so that a spreadsheet does not interpret them as
formulas; number cells keep their sign.

`POST /api/tracking/import` takes an NDJSON survey backup with
`Content-Type: application/x-ndjson`, up to 8 MiB. It is staged and verified
before anything active is touched; see [Storage](#storage).

### Stakeout target sets

A stakeout target set is a list of target points and lines that the browser
prepared from a GeoJSON file and uploaded to one project. The device never
sees the raw GeoJSON. It validates the browser-normalized package completely
again, so a hand-made request is held to the same rules.

**Package.** `POST /api/stakeout/import` takes one JSON object with
`Content-Type: application/json`:

| Key | Content |
|---|---|
| `schema` | `1` |
| `project_id` | The project that receives the set: 1 to 64 characters from `A-Z`, `a-z`, `0-9`, `-`, `_` |
| `source` | `filename` (at most 120 characters), `bytes` (size of the original file), `features`, `skipped` (at most 50 entries with `feature_index`, `type`, `reason`) and `skipped_total` |
| `parts` | The features, each with `kind` (`point`, `multipoint` or `line`), `name`, `feature_index`, `geometry_index`, `coords_total`, `coords_thinned`, and `coords_e8` (vertices of a line; empty for points) |
| `targets` | The selected targets, each with `part_index`, `coord_index`, `name`, `lat_e8`, `lon_e8`, `alt_import`, and `props` |

Coordinates are exact integers in units of 1e-8 degree (`lat_e8`, `lon_e8`)
and the height `alt_import` is an integer in millimetres, or `null`. A line vertex is `[lon_e8, lat_e8, alt_mm or null]`. A
float, a boolean, or a string where an integer belongs is refused; property
values are strings (at most 80 characters) or `null`. Every part has at least
one target, and targets must follow in ascending part and vertex order.

**Limits.**

| Limit | Value |
|---|---|
| Import body | 131 072 bytes (128 KiB) |
| Sets per project | 4 |
| Targets per set | 500 |
| Stored line vertices per set | 2000 |
| Properties per target | 8 |
| Name | 80 characters |
| Set file | 393 216 bytes (384 KiB), body and trailer, checked while writing |

A line with more vertices than fit is thinned in the browser; the device
stores the thinned vertices and keeps the original count in `coords_total`.
The import body limit is 128 KiB. The device parses the body in one piece,
the one block that cannot be cut; a 256 KiB package alone took longer than the
second that the receiver's fix age allows, so a larger body is refused with
`413` before it is parsed. After the parse the package is checked in slices of
about 50 ms with a yield in between. The size of the file is
not known before it is written, so the write reserves twice the set file limit
plus 32 KiB (819 200 bytes, about 800 KiB) of free flash, within the flash
reserve rules of the survey: one flash measurement per import, deliberately
conservative, so a small set can be refused with `507 storage_reserve` when the
flash is nearly full. A set that would grow beyond the limit stops the write
with `400 too_many_targets`.

**Store folder.** The sets live below `stakeout.sets/` at the flash root
(`stakeout.DEFAULT_ROOT`). The name contains a dot on purpose, like
`tracking.archives/`: MicroPython looks for a directory `<name>` before
`<name>.py` when it imports `<name>`, and a directory without `__init__.py`
is imported as an empty namespace package. A folder named `stakeout` next to
`stakeout.py` would therefore replace the module after the next restart, and
the application would not start. A dot cannot occur in a module name, so this
cannot happen to `stakeout.sets/`; a host test checks that no flash name the
firmware writes at the root is a module name. Earlier firmware kept
the sets in `stakeout/`: before anything imports the module `stakeout`,
`main.py` renames that folder to `stakeout.sets/`. If `stakeout.sets/`
already exists, the old folder is renamed to the first free
`stakeout.conflict-<n>/` (n = 1 to 9) instead and a `WARN` names it; nothing
is deleted or merged, and the start proceeds with `stakeout.sets/`. The
conflict folder is not read by the firmware and is kept for inspection by
hand. Only if all nine names are taken does the old folder stay, with an
`ERROR` in the log. A file named `stakeout` is left alone.

**Set file.** Each set is one file `stakeout.sets/<project_id>/<set_id>.json`
(layout 2). It is one JSON object with line breaks as whitespace: the head
(`kind`, `schema`, `project_id`, `project_created_at`, `set_id`,
`imported_at`, `source`), then `"parts": [` with one part per line, then
`"targets": [` with one target per line, and at the end a trailer of exactly
4096 bytes, `,\n"summary": {...}` padded with spaces and closed by `}\n`. The
body is every byte up to and including the `]` that closes `targets`. The
`summary` repeats what the overview shows (`set_id`, `project_id`,
`project_created_at`, `imported_at`, `source` without `skipped`, `parts`,
`targets`, `coords`) plus `layout` (`2`), `body_bytes`, `body_sha256` (the
lowercase hex SHA-256 of the body) and `targets_offset` (the byte offset of the
first target line). Every target line is shorter than 16 384 bytes. A listing
reads only this trailer; a stored set is never parsed on the device.

**IDs.** `set_id` is `so-` followed by the first eight hex digits of a SHA-256
over the selected targets' part index, vertex index, latitude, and longitude.
The same selection therefore always has the same `set_id`, and importing it
again answers `409 already_imported`. A part is `<set_id>/<n>` and a target is
`<set_id>/<n>/<seq>`, where `seq` is the target's vertex index plus one. A
target identifier starts with the `set_id`, which depends on the whole
selection, so another selection gives other target identifiers; only `seq`
within a set is stable.

**Access.** All three routes need a session. The two `POST` routes also need
the CSRF header and an acceptable `Origin`.

* `GET /api/stakeout[?project_id=...]` answers the overview for the active
  project (or the named one): `sets` with `set_id`, `state`, `imported_at`,
  `source`, `parts`, `targets`, and `coords`, `totals` (`sets`, `targets`,
  `orphaned`), `limits`, `load_ms`, and `load_block_ms`.
* `GET /api/stakeout?set_id=<id>[&project_id=...]` answers that set file
  byte for byte, `summary` and line breaks included: the device streams the
  set file from flash in 1 KiB pieces, with `Content-Length` from the file
  size, while a SHA-256 runs over the body. The trailer is sent only when the
  digest equals `body_sha256`; otherwise the connection is closed before it
  (the answer is short, so every client sees it fail), the set is `damaged`
  from then on and an `ERROR` is logged. A set that is not `ok` answers
  `409 set_damaged` before anything is sent. A client that does not take a
  piece within 10 s ends the download: the connection is closed without a
  further answer, and the download no longer counts as open.
* `POST /api/stakeout/import` stores a package and answers `200` with
  `set_id`, `project_id`, `parts`, `targets`, `coords`, `skipped`,
  `skipped_total`, `bytes` (the file size, which is what a download sends),
  and `timing_ms`.
* `POST /api/stakeout` with `{"action": "delete_set", "project_id": ...,
  "set_id": ..., "expected_confirmed": 0}` removes one set and answers
  `{"deleted": "<set_id>"}`. `expected_confirmed` is required and must be an
  integer; it is the number of confirmed targets the caller believes the set
  has, and a different count answers `409 confirmed_changed`. While a download of the same project
  is open, a deletion answers `409 storage_busy`.

**Errors.** Every error is `{"error": code, "message": text}` and an import
validation error adds `path`. `already_imported`, `orphaned_set_conflict`,
`confirmed_changed`, and `set_damaged` add `set_id`;
`too_many_targets` adds `path`.

| Status | Code | Meaning |
|---|---|---|
| 400 | `invalid_stakeout_import` | The package breaks a rule; `path` names the field |
| 400 | `coordinate_out_of_range` | A latitude, longitude, or height lies outside its range |
| 400 | `too_many_targets` | A count or the exact set file size exceeds a limit |
| 400 | `invalid_request` | Malformed request, `project_id` or `set_id` |
| 400 | `empty_import` | Empty body |
| 400 | `unknown_action` | `POST /api/stakeout` with an action other than `delete_set`, `select_target`, `set_order`, `confirm`, `skip` or `reopen` |
| 401 | `admin_session_required` | No valid session |
| 403 | `csrf_rejected`, `origin_rejected` | A valid session, but the CSRF header is missing or wrong, or the `Origin` is refused |
| 404 | `project_not_found`, `set_not_found`, `not_found` | Unknown project, set, or stakeout path |
| 409 | `already_imported` | This `set_id` is stored already |
| 409 | `orphaned_set_conflict` | A set of an earlier project with the same ID occupies the name |
| 409 | `too_many_sets` | The project holds 4 sets |
| 409 | `recording_active` | A line is recording or a measurement runs |
| 409 | `benchmark_active` | A parallel benchmark runs (import and deletion only) |
| 409 | `storage_busy` | Another set change or a storage maintenance runs; a deletion while a download of the project is open |
| 409 | `stakeout_cleanup_pending` | An import or a set deletion while a folder set aside by a deletion of this project ID waits for the next start (see *Project deletion*) |
| 409 | `unsupported_project_fingerprint` | An import into a project whose `created_at` is neither null nor a whole number from 0 to 4 294 967 295 (a restored survey can carry one); a set cannot record it, so nothing is written |
| 409 | `confirmed_changed`, `set_damaged` | Deletion count differs; the set is damaged or incomplete (also for an import whose `set_id` names a damaged set: delete the damaged set first) |
| 413 | `import_too_large` | The body does not fit into memory. A body over 131 072 bytes never gets this far: the dispatcher answers `413` as `text/plain` ("Request body is too large.") without the JSON envelope |
| 415 | `json_required` | Wrong content type |
| 500 | `import_committed_response_failed` | The set was stored but the answer could not be completed; it carries `set_id`, and the same import answers `already_imported` |
| 503 | `survey_loading`, `stakeout_busy` | The survey is not loaded yet; not enough memory, or the sets changed while they were listed or imported (transient) |
| 503 | `stakeout_recovery_pending` | The project's sets are set aside by a project deletion that could not be undone; restart the device (see *Project deletion*) |
| 507 | `storage_reserve`, `stakeout_write_failed`, `stakeout_read_failed`, `stakeout_delete_failed` | Flash reserve, write, read, or delete failed |

An import or a deletion is refused with `recording_active` while a line is
recording or a measurement runs, and the refusal comes before the package is validated field by field.
A paused line does not block. `GET` stays allowed then, and also during a
storage maintenance; a listing never renames or removes anything at any time.

**States.** A listing classifies the files of a project folder and changes
none of them: a set file with an intact trailer (frame, summary members within
their ranges, `set_id` and `project_id` equal to the file and folder names,
`body_bytes` plus 4096 equal to the file size) is `ok`; any other set file is
`damaged`; a `.tmp` without a set file is `incomplete` (an import of the same
set replaces it, and it does not count against the four sets); a `.tmp` beside
a set file is kept and the set has the state of the file. The `.tmp` of the
running import is not listed. Other names in the folder are ignored. `damaged`
and `incomplete` sets are listed with `set_id` and `state` only and can be
deleted; `delete_set` removes the set file first and then a `.tmp` beside it,
so a set never comes back as `ok` once its removal began. A set file whose
body was changed without changing its length is found by the next download
(see above). A power cut during an import leaves either no set, an
`incomplete` one, or the complete `ok` set; the `200` is sent only after the
rename. A leftover `.tmp` is never promoted: a cut in the short window between
the end of the read-back and the rename leaves `incomplete`, and the import is
repeated. (Version 1 of the store promoted such a file and had a listing state
for a pending promotion; both are gone, because a listing changes nothing.)

**Trust model.** `ok` means the writer committed the set (checked in memory,
written once, read back, renamed) and its trailer is intact; a listing does not
hash or parse the body. A change inside the body that keeps the file length is
found only by the next download of that set, whose digest check marks it
`damaged`. That mark is kept in RAM only: after a restart the set lists `ok`
again until a download finds the change again. No download delivers the trailer
of a body that does not match.

**Project deletion.** The tracking action `delete_project` also removes the
project's sets. Before the deletion is journaled, the project folder is renamed
to `stakeout.sets/<project_id>.deleting`, the intent to delete it; the journal append
follows without a yield in between. Once the deletion is journaled it is never
reported as failed: the set-aside folder is removed file by file, with a yield
after each, and the `200` answer carries `"stakeout_cleanup": "done"`, or
`"pending"` when something is left (a `WARN` is logged and the next start
removes the rest). While such a folder is left, imports and set deletions of a
project with the same ID answer `409 stakeout_cleanup_pending`, so a new folder
never meets the old one; the next start settles it. A project without sets, or
whose ID is no stakeout project ID (a restored survey may carry any ID), answers
`"done"` without touching the stakeout folder. After the answer
the flash is measured again when set files were removed. Besides the tracker's
own refusals, a deletion answers:

| Status | Code | Meaning |
|---|---|---|
| 409 | `storage_busy` | An import runs or a download of the project is open |
| 409 | `stakeout_cleanup_pending` | A folder set aside by an earlier deletion of this project ID is not removed yet; the next start removes it |
| 409 | `stakeout_recovery_pending` | The project is blocked (see below) |
| 507 | `stakeout_delete_failed` | The folder could not be renamed; nothing changed. When the outcome of the rename cannot be determined, the project is blocked until the next start (see below) |
| 507 | `tracking_storage_failed` | The journal append failed. When the project is still in the survey the folder is renamed back and the answer is as before; when it is gone the append may be durable, so the folder stays set aside, the answer adds `"stakeout_cleanup": "pending"`, and the next start decides from the journal |

If the folder cannot be renamed back after a failed append, the project is
blocked until the next start: its stakeout requests answer `503
stakeout_recovery_pending`, and `archive_project`, `delete_project` of it,
`restore`, `reactivate_archive`, and `POST /api/tracking/import` answer `409
stakeout_recovery_pending` (archiving or restoring it away would let the next
start delete sets that are meant to stay). The start-up settles every set-aside
folder before the survey counts as loaded, by the loaded survey: the folder of
a project that is gone is removed; that of a live project is renamed back, and
when its project folder exists as well both stay (`WARN`) and the project is
blocked again. When the stakeout folder cannot be listed at start-up (an I/O
error other than a missing folder), it is unknown which projects have a folder
set aside: every project counts as blocked until the next start, with the
answers above, and the survey still loads. Nothing else is swept: an archived project keeps its folder and
its sets are visible again after `reactivate_archive`.

**Timing.** The import answer carries `timing_ms` with the phases `parse`,
`validate`, `reserve`, `write`, `verify`, and `rename` (`size` stays and is
always `0`: the set is serialized once, while it is written);
`longest_block`, the longest stretch between two yields to other tasks from
the collection before the parse to the commit (the import yields after the
parse, while listing the
project, during the validation, after making the folders, after the flash
reserve, after every 4 KiB written, before the trailer, and after every 4 KiB
read back); and `capacity_refresh`, the time to charge the committed file
against the flash sample of the reserve check (an import samples the flash
once; a deletion measures it again after its answer, after a yield of its
own). Before the parse the import yields, collects garbage, and yields again; the
collection is timed as `gc` and is part of `longest_block`, because it is a
real block. The answer carries further diagnostics, all integers in
milliseconds or counts: `validate_work`, `validate_wait`, `write_work`,
`write_wait`, `verify_work`, `verify_wait` (a phase's wall time split into local
work and time suspended in service pauses), `pauses` and `max_pause` (the
number of service pauses and the longest actual suspension of one), and the
slowest single step of each kind: `max_serialize` (one part, target, or the
trailer), `max_read`, `max_write`, `max_flush`, `max_close`, `max_dir`
(listing, folder, rename), and `max_capacity` (the flash probe). A slow single
call therefore shows up here and in `longest_block`; it is not averaged away.
The overview carries `load_ms`, the wall time of a listing that read the
trailers (yields included), and `load_block_ms`, its longest stretch without
a yield (both `0` when the summaries are cached). A value above 1000 ms in
`longest_block` or `load_block_ms` is a defect to be fixed, because the
receiver's fix age must stay within one second.

### Stakeout session and live guidance

The device keeps one active stakeout target and judges every published fix
against it, without touching flash per fix. The module `stakeout_session.py`
holds the state machine; `state/receiver.py` calls it for each published GGA fix and for
each GST enrichment of that fix (`fix_seq` counts publications, `fix_rev`
enrichments). The session does no file, network or log work per fix, and it
knows neither the web layer nor the survey.

**Selecting a target.** `POST /api/stakeout` with `{"action": "select_target",
"tid": "so-xxxxxxxx/<part>/<seq>"}` makes that target active and answers
`{"selected": <target>, "changed": <bool>}`; `"tid": null` clears the
selection (a missing `tid` is an error, not a deselection). The target carries
`tid`, `set_id`, `part_id`, `seq`, `name`, `lat_e8`, `lon_e8` and `alt_import`.
The device looks the target up in the set file: through the set listing first,
then by reading the file in blocks of at most 4096 bytes with a pause after
each, from `targets_offset` up to the target line. The first lookup in a large
set after it was listed reads the whole body once, from byte 0, and checks its
length and SHA-256 against the trailer (as a download does); later lookups in
that set stop at the target. A set that fails the check is `damaged`. Selecting
the target that is already active keeps its quality window (a browser that
reconnects selects it again); any other target starts a new window. A selection
is refused with `409 recording_active` while a line records (a paused line does
not block), and with `409 project_not_active` for a project that is not the
active one.

**Part order.** `{"action": "set_order", "part_id": "so-xxxxxxxx/<n>",
"reversed": true|false}` notes whether the browser walks that part of a set in
reverse and answers `{"part_id": ..., "reversed": ...}`. The order is guidance
for the browser's list only; it is checked against the set listing (no set file
read) and kept in RAM, as a bit per part, for at most four sets of the active
project. A part number beyond the set's `parts` answers `404 part_not_found`.
Unlike a selection, an order change is allowed while a line records.

**Lifetime.** The active target and the orders live in RAM only. They do not
survive a restart. Tracking actions that change the active project or replace
the survey clear the session before and after the action: `create_project`,
`select_project`, `delete_project`, `archive_project`, `reactivate_archive`,
`restore`, and the upload of a survey backup (`POST /api/tracking/import`).
The actions that stay within the active project keep it: `start_line`,
`pause`, `resume`, `finish`, `add_vertex`, `add_asset`, `undo`,
`cancel_measurement`, `rename_project`, `verify_archive` and `compact`. A
`delete_set` of the target's set, a set found damaged and a project whose
`created_at` changed also clear the target. Each such change raises a runtime
revision; a `select_target` or `set_order` that waited for flash while the
project changed meanwhile answers `409 project_changed`, or `404
project_not_found` when the project's identity changed, and changes nothing.

**States.** `guidance.state` is one of:

| State | Meaning |
|---|---|
| `no_target` | No target is selected; nothing is computed |
| `no_position` | No integer position, `qual` 0, a fix older than the freshness limit (`stale`), or no fix yet since the selection (`awaiting_fix`) |
| `far` | The rover is more than 50 000 m from the target; no distances are reported |
| `coarse` | Farther than the fine radius |
| `fine` | Within the fine radius, but outside the tolerance (`outside_tolerance`), or the acceptance gate rejects the fix (`gate_rejected`) |
| `settling` | Within the tolerance with an accepted fix (or one whose GST is on its way), but the quality window is not complete or does not hold |
| `ready` | Within the tolerance and the quality window holds |

`guidance.reasons` carries at most one reason from the session, then
`recording_active` while a line records. Session reasons: `awaiting_fix`,
`outside_tolerance`, `gate_rejected`, `gst_pending` (the gate waits for this
fix's GST), `mean_outside_tolerance` and `horizontal_spread_too_high` (the full
window's mean is outside the tolerance, or its spread above
`measurement_max_horizontal_spread_m`), `config_changed` (a limit changed since
the fix was judged) and `session_error` (the call for this fix did not run
through; the result is never trusted). The gate's own reasons (including
`corrections_too_old`) are in `gate.reasons`.

**Quality window.** Within the tolerance, the session keeps the fix dicts of the
last `measurement_quality_window_samples` epochs (3 to 30, 5 by default), so a
later confirmation uses exactly the fixes the guidance judged. `ready` needs a
full window of accepted fixes (GST settled) whose mean, computed with the same
integer functions as the survey's averaging, lies within the tolerance and whose
largest deviation from that mean is at most `measurement_max_horizontal_spread_m`;
both limits are inclusive. One publication counts once: the GST enrichment of a
fix (same `fix_seq`) or a repeat of the same UTC within 2500 ms replaces the
newest element and never moves its epoch time. The window resets when the
fix is farther than the tolerance, when the gate rejects it, when a GST never
arrives, when more than 2500 ms pass between epochs, when the position is
missing, when the target changes, or when any of the eight gate limits or the
two stakeout settings change. A read never changes the window: a changed
configuration reads as at most `settling` with `config_changed` until new fixes
fill the window again.

**Signs, bearing, rounding.** `dn_m` and `de_m` are the target minus the rover
(north and east, in metres), so a positive `dn_m` means the target lies north of
the rover. `bearing_deg` points from the rover to the target against geographic
north, in `[0, 360)`. Up to 10 000 m the values are rounded to three decimals
(millimetres, bearing to a tenth of a degree); beyond that to whole metres and
whole degrees. In `far` the four values are `null`. `motion` comes from the
last two epochs of RTK fixes (`qual` 4 or 5) at most 2500 ms apart: `speed_mps`
(two decimals), `course_deg` (a tenth of a degree) and `valid`, which is true
only at 0.5 m/s or faster. There is no compass: the course is derived from
positions only, so it is `null` and `valid` false when standing still.

**Live packet.** `GET /api/stakeout/live` answers one JSON object per call, built
from the same sources as `/api/ui/live` (the fix and its freshness, the gate
verdict, the NTRIP summary and the link health of one clock reading) and reads
RAM only. It never samples the flash: `header.storage_level` is `null` until the
survey has a flash sample. Reading does not change the session.

```json
{"schema": 1, "device_time": 1760000123, "fix_seq": 4711, "fix_rev": 2,
 "fix_age_ms": 340, "stale": false,
 "position": {"lat_e8": 5100008991, "lon_e8": 1000000012, "alt_mm": 412345,
              "qual": 4, "fix_status_text": "RTK_FIXED", "sats": 21, "hdop": 0.6,
              "correction_age_sec": 1.2, "gst_h_sigma_m": 0.011,
              "gst_v_sigma_m": 0.018},
 "corrections": {"ntrip_state": "connected", "rtcm_age_sec": 1.0, "link": "ok"},
 "gate": {"accepted": true, "reasons": []},
 "motion": {"speed_mps": null, "course_deg": null, "valid": false},
 "header": {"access_state": "STA_CONNECTED", "storage_level": "ok"},
 "target": {"tid": "so-3f9a1c2b/1/3", "lat_e8": 5100009261,
            "lon_e8": 999999552, "status": "open"},
 "guidance": {"state": "settling", "dn_m": 0.3, "de_m": -0.29, "dist_m": 0.42,
              "bearing_deg": 316.0, "tolerance_m": 0.1, "fine_radius_m": 5.0,
              "window": {"have": 3, "need": 5, "spread_m": 0.012},
              "reasons": []}}
```

`position` is `null` without an integer position and `target` is `null`
without a selection. `gate.reasons` are the gate's reasons plus the display
reasons `fix_stale` (when `stale`) and `corrections_missing` (when the
correction link is not `ok`); display reasons never reset the window.
`guidance.window.have` counts accepted epochs with a settled GST, `spread_m` is
set once the window is full. `target` carries no name (the browser looks it up by
`tid` in the set file) and `status` is `open`, `confirmed`, `skipped`, or
`unknown` while the progress of the project is not loaded.
Staleness is the live fix's own (`stale`, from `fix_stale_after_ms`); the packet
has no second freshness rule, and a stale fix always reads `no_position`.

**Packet size is a contract.** The packet stays within 900 bytes for ordinary
values (the example above is 893 bytes in the device's JSON encoding) and within
1400 bytes in the worst case (every gate reason, extreme numbers, the longest
`tid`). Browser code polling at
one hertz relies on this budget; a change that grows the packet beyond it is a
contract change.

**Settings.** `GET /api/stakeout/settings` answers `{"values": {"tolerance_m":
0.1, "fine_radius_m": 5.0}, "defaults": {...}}`. `PUT` takes a JSON object with
either or both keys, validates them and saves them live, without a restart, and
answers like `GET`.

| Setting | Factory value | Range |
|---|---|---|
| `tolerance_m` | 0.1 | 0.02 to 5 m |
| `fine_radius_m` | 5.0 | 1 to 50 m |

Values are decimal numbers or numeric strings with at most three decimals
(millimetres). The pair must satisfy `tolerance_m < fine_radius_m`, also when
only one of the two is sent (it is checked against the saved other value). A
violation answers `400 invalid_stakeout_settings` with a message and changes
nothing. The same validator and pair rule load the saved configuration: a pair
that violates the rule in `config.json` resets both values to the factory
values with a warning and leaves the rest alone. Both values are part of the
configuration backup (schema 3, see *Configuration backup*). The overview
`GET /api/stakeout` carries `settings` (the same two values) and `session`
(`target`, `orders` as `{set_id: [part numbers walked in reverse]}`,
`on_fix_us_max`, the longest time of one session call in microseconds since
start, and `fixes_seen`).

**Errors.** In addition to the table under *Stakeout target sets*:

| Status | Code | Meaning |
|---|---|---|
| 400 | `invalid_stakeout_settings` | A setting is missing, not a number, outside its range, unknown, or breaks `tolerance_m < fine_radius_m` |
| 400 | `invalid_request` | `select_target` without a valid `tid` (or `null`), `set_order` without `part_id` and a boolean `reversed` |
| 404 | `target_not_found` | The set has no such target (also a `part` beyond the set's parts) |
| 404 | `part_not_found` | `set_order` names a part the set does not have |
| 404 | `set_not_found` | `select_target` names a set that is not listed |
| 409 | `project_not_active` | `select_target` or `set_order` for a project that is not the active one |
| 409 | `project_changed` | The active project or the session changed while the request waited for flash; select again |
| 409 | `recording_active` | `select_target` while a line records |
| 409 | `set_damaged` | The target's set is damaged (also found by the lookup) |
| 500 | `save_failed` | `PUT /api/stakeout/settings` could not save |

### Stakeout progress and confirmation

The status of a target (`open`, `confirmed`, `skipped`) comes from two sources
that never disagree about who owns what. A confirmation is a survey point of
the type `stakeout`; it is the only record of a confirmation. A skip, a
reopening, the active target and the part order are events in the per-project
file `stakeout.sets/<project_id>/progress.jsonl`. Both live in
`stakeout_progress.py` (the fold, the point checker, the decision rule) and are
driven by `web.py`; `stakeout_export.py` is imported only by the export
handler.

**Stakeout point.** `Tracker.add_asset(object_type, name, note, fix,
link_to_active_line, stakeout)` takes `object_type` `"stakeout"` only together
with a `stakeout` dictionary (`tid`, `gen`, `cmd`, `mode`, `tolerance_m`,
`dn_m`, `de_m`, `dist_m`); `POST /api/tracking` with that type answers `400`.
The point stores `stakeout_tid`, `stakeout_gen`, `stakeout_cmd`,
`stakeout_tolerance_m`, `stakeout_dn_m`, `stakeout_de_m` and
`stakeout_dist_m`, and its `measurement.source` is `stakeout_verified` or
`stakeout_manual`. One checker, `valid_point_fields`, decides whether a point
is valid; `add_asset`, the backup validator and the point index use it, so a
point that fails it is refused on write and restore, and ignored and counted
(`ignored_points`) when it is already stored. A point is exported like every
survey point; the project GeoJSON and CSV carry its fields and the type.

**Generations and the decision rule.** A target has a generation, a number
that grows by one with every reopening. Its effective generation is the
highest of the logged generation, the floor of its set (raised by a set
removal), the floor of the project (raised by a survey restore) and the
generation of its highest stakeout point. The status follows from that: a
valid stakeout point of the effective generation confirms the target, a logged
skip of the effective generation skips it, otherwise it is open. Every action
carries `expect_gen`, the generation the caller saw, and the rule is:

| Action | Situation | Answer |
|---|---|---|
| `confirm` | `expect_gen` differs from the generation | `409 target_changed` |
| | confirmed in the same mode | `200` with `repeated: true` |
| | confirmed in another mode, or skipped | `409 target_not_open` |
| | open | checks, then one stakeout point |
| `skip` | `expect_gen` differs | `409 target_changed` |
| | skipped | `200` with `repeated: true` |
| | confirmed | `409 target_not_open` |
| | open | one `skip` event |
| `reopen` | generation above `expect_gen` and open | `200` with `repeated: true` |
| | generation above `expect_gen` and not open, or below | `409 target_changed` |
| | `expect_gen` equals the generation, open | `409 target_open` |
| | `expect_gen` equals the generation, confirmed or skipped | one `reopen` event with the next generation |

A repeated request therefore gets the same answer again, also after a restart,
and a late `confirm` that was sent before a reopening, an undo or a restore
carries an old generation and is refused. A reopening leaves the old stakeout
point in the survey as history; it no longer counts.

**Actions.** `POST /api/stakeout` takes `confirm`, `skip` and `reopen` with
`{"action", "project_id"?, "tid", "cmd", "expect_gen", "mode"?, "note"?}`.
`cmd` is exactly 16 lowercase hex digits and is only written to the log; it is
not an identity. `expect_gen` is an integer from 0 up. `mode` (confirm only) is
`verified` (the default) or `manual`; `note` has at most 500 characters. A
malformed field answers `400 invalid_request`. `verified` judges the packet of
the live session: the target must be the selected one (otherwise `409
not_ready` with `reasons` `target_not_selected`), and the session must be
`ready`, otherwise `409 not_ready` carries the `state` and `reasons` of the
packet. `manual` ("found on site") stores the current position of a fresh fix
of any quality; without one the answer is `409 no_position`, and the gate
verdict stays in `measurement.quality`. A running measurement answers `409
measurement_active`, a rejected measurement `409 quality_rejected` with
`reasons`, a full point store `409 point_limit_reached`, and a full flash `507
storage_reserve`. The answer of every action is `{"persisted": true,
"repeated", "tid", "gen", "status", "mode", "point_id", "dn_m", "de_m",
"dist_m", "tolerance_m"}`; a field without a value is left out. `persisted` is
only true after the write finished without an exception. The error extras
`status`, `mode`, `gen`, `state` and `reasons` are merged into the envelope
and never replace `error` or `message`.

**Events and write steps.** `progress.jsonl` starts with a head `{"kind":
"stakeout_progress", "schema": 1, "project_id", "project_created_at"}`, then
one event per line: `skip`, `reopen`, `active`, `order`, `set_removed` and
`restored`. Each event is one `open("ab")`, one `write`, a `flush` and a
`close`; the first line of a new file goes out with the head in one `write`. A
line that is cut off, longer than 4096 bytes, unknown or out of range is
skipped and counted (`damaged_lines`) when the file loads, and a file without a
final line break gets one before the next append. A head of another project or
creation time moves the file to `progress.jsonl.foreign` and starts empty. At
most one flash write step runs per synchronous block, so a slow flash never
starves the watchdog.

**One change lock and the latch.** Every stakeout change (import, set removal,
confirm, skip, reopen, undo of a stakeout point, survey restore, loading or
compacting a fold, clearing leftovers) runs under one lock. A second change
while it is held answers `409 storage_busy` at once; nothing waits. Readers
never wait either: the live packet reads only the cached fold and shows the
status `unknown` while the progress of the project is not loaded, and a cold
load for a listing needs a free lock, otherwise it answers `503
stakeout_busy`. When a write step throws after it began (the append from the
`open` on, or `add_asset` after the event sequence grew), the outcome is
unknown, and the **latch** is set: from then on every change answers `503
stakeout_write_uncertain` ("A stakeout change may not be stored; restart the
device.") until the next start, which reads the flash and decides. While it is
set `select_target` and `set_order` work in RAM only (`remembered: false`),
compaction rests, and the reads report the warning: `write_uncertain` in the
live `guidance.reasons`, in the overview's `progress` and in the progress list.
Not locked are survey actions without a stakeout link, `delete_project`,
archiving and the export.

**Undo and restore reopen.** Undoing the stakeout point that confirms its
target first appends `reopen` with the next generation (`by: "undo"`), then
runs the undo; if the undo fails afterwards, the target stays open and the old
point does not count. Undoing any other point works as before. A survey restore
(the `restore` action and the NDJSON import, not `reactivate_archive`) writes
`restored {floor}` for every project that lives before the restore and has
stakeout data, with a floor above every generation the project has seen, and
only then publishes the restore. After it all targets of those projects are
open, even if the backup contains their stakeout points: they stay in the
survey as history, and a late request of the old generation answers `409
target_changed`. If a floor cannot be written the restore is not published.

**Compaction.** When `progress.jsonl` reaches 32 KiB and twice the size of
its last compacted form, loading writes the folded state as
`progress.jsonl.tmp` in blocks of at most 4096 bytes with a running SHA-256,
reads it back, compares the digest and renames it over the file. A leftover
`.tmp` is removed first. A missing reserve or an error only logs a warning;
the old file stays in use. Compaction never runs under the latch.

**Set removal.** `delete_set` first appends `set_removed {set_id,
imported_at, floor}` (the commit; it needs no flash reserve), then hides the
set, then removes `<set_id>.json` and its `.tmp` with a pause after each
step. A failure after the commit answers `200` with `"cleanup": "pending"`;
the remains stay hidden and are removed by the next listing of the project or
before the next import. The set floor keeps old stakeout points of the removed
set from counting after a reimport (the same content has the same `set_id`).
`expected_confirmed` is compared with the confirmed targets of the fold.

**Orphaned data.** Stakeout data disappear only through `delete_set`,
`delete_project` and the replacement of an older `progress.jsonl.foreign`.
What a survey restore or a crash leaves behind stays on the flash: sets and
progress with another creation time are hidden, and
`stakeout_progress.unowned_folders` counts the folders that belong to no
project and no archive (one `os.listdir`, cached until the folds are reset).
The count is reported as `unowned_folders` in `GET /api/stakeout` and logged
at INFO when it is above zero. Nothing is deleted automatically.

**Target numbers.** `seq` in a target ID runs from 1 to 1 000 000 (the import
limit of coordinates); the numeric key of a target is `part << 20 | seq`.

**Progress list.** `GET /api/stakeout/progress` (with `project_id`) streams
`{"schema": 1, "project_id", "active_tid", "write_uncertain", "orders",
"floors", "counts", "states"}`. `floors` has one entry per visible set (the
effective floor), `counts` has `confirmed`, `verified`, `manual` and `skipped`
per set, and `states` lists `[tid, status, gen, mode, point_id]` for every
target whose state differs from "open at the floor generation"; a target that
is not listed is exactly that. `GET /api/stakeout` gains `progress`
(`active_tid`, `damaged_lines`, `write_uncertain`) in every answer, `progress`
with the four counts per set, and `unowned_folders`. `select_target` and
`set_order` answer `remembered` (true when the choice was written), and the
active target and the part orders are restored when the progress of the active
project loads; no target is selected automatically after a restart. The
archive metadata carry `stakeout_data` (true when the project folder held a
set or a progress file at archiving time); archives made before V16.0.0
have no such field.

**Export.** `GET /api/stakeout/export` (or `HEAD`) with `project_id`,
`format=geojson|csv` and an optional `set_id` streams the whole project or one set (default
`geojson`; another format or an invalid `set_id` answers `400
invalid_request`, an unknown or hidden set `404 set_not_found`). It shows the
state at export start: the fold and the point list are copied in one
synchronous block before the first byte, so undo, restore, compaction and new
confirmations during the download do not change it, and the project is held
against deletion until the stream ends. The GeoJSON is a FeatureCollection
with `kind` `stakeout_export`, the `stakeout_sets` (with the part lines
byte for byte under `parts`), and one Point feature per target with `status`,
`mode`, `gen`, `source` and an `observation` taken from the stakeout point
(or `null`); coordinates are the target coordinates as number text, so a
reimport gives identical coordinates, and lines come back as single points. The
CSV has one row per target with 30 columns, quoted and protected against
formula injection like the survey CSV. A set whose body digest does not match
is marked damaged and the stream breaks off.

**Errors.** In addition to the tables above:

| Status | Code | Meaning |
|---|---|---|
| 400 | `invalid_request` | An action field is malformed (`cmd`, `expect_gen`, `mode`, `note`, `format`, `set_id`) |
| 409 | `target_changed` | `expect_gen` is not the target's generation; `status`, `mode` and `gen` say what is |
| 409 | `target_not_open`, `target_open` | The action does not fit the status (see the rule above) |
| 409 | `not_ready` | `verified` without a selected target or a `ready` packet; `state` and `reasons` |
| 409 | `measurement_active`, `quality_rejected`, `point_limit_reached`, `no_position` | A running measurement; a rejected measurement; a full point store; no fresh position |
| 409 | `undo_candidate_changed` | The undo candidate is not the expected one |
| 503 | `stakeout_write_uncertain` | The latch is set; restart the device |
| 507 | `storage_reserve`, `tracking_storage_failed` | Flash reserve; the point could not be written (the target stays open) |


### Stakeout in the field app

The field app shows the stakeout of a project below the project's own route:
`#projects/<id>/stakeout` (overview), `…/import`, `…/targets` and
`…/target/<tid>`, the target identifier as one escaped path segment
(`so-xxxxxxxx%2F<part>%2F<seq>`, part 1 to 500, seq 1 to 1 000 000; the
browser only compares identifiers and never computes keys). Any other
sub-path shows the overview and replaces the address without a new history
entry. The project panel links there, first thing below the project map and
before the lists of lines and points; opening the page does not change the
active project. A project that is not active shows its sets, its list and the
import, and instead of the guidance a hint with **Activate for stakeout**, which
sends `select_project` on that tap only and then refreshes the sections of the
page without building it again.

**One live request per second.** Every stakeout page sends exactly one periodic
request, `GET /api/stakeout/live`, 1000 ms after the previous one ended, with a
3000 ms timeout and never two at a time. It rests while the tab is hidden and
during the restart hold of a guided apply. The general `/api/ui/live` poll, the
event stream and the map poll are off on these routes. A display ticker every
250 ms makes no request; it only lets the shown data age, so a view turns stale
four seconds after the last usable packet even when no further answer comes.

**Packet check and header.** Before anything uses a packet, the app checks its
shape: `schema` 1, `fix_seq` and `fix_rev` as non-negative integers, `stale`,
`fix_age_ms`, `position` (null or integer `lat_e8`/`lon_e8`), `gate`, `guidance`
and `target` with their types. Unknown extra fields and unknown state or reason
values are allowed and shown with a general text. A packet that fails is not
taken: it renews neither the receive time nor the live state, the guidance is
no longer usable, and the quality bar says "Unexpected device data". A valid
packet is the only stakeout input of the shared live state: it sets the header's
connection to connected, writes the fix, the gate (without the two display
reasons `fix_stale` and `corrections_missing`), the RTCM age and the NTRIP state
into it and drops older polls (`invalidateLive`). The view keeps its own
freshness latch with the rules of the device: a stale packet or `no_position`
latches its `fix_seq`, a gap of more than 4 s latches the last one, and only a
larger `fix_seq` frees the guidance. A smaller `fix_seq` than the last one is a
device restart (the poll is sequential): the view and the header's latch both
start again, so the guidance is not blocked for good after a restart.

**Calm answers.** `503 stakeout_busy` and `503 stakeout_write_uncertain` mean
that the device answered but is not ready. The pages then show one calm notice
and no error message; the connection counts as up, the pace of the poll stays,
and no request is repeated by itself (the overview offers "Load again"). While
the write latch of the device is set (see *Stakeout progress and confirmation*),
the live packet carries the guidance reason `write_uncertain` and the overview
`progress.write_uncertain: true`; both show the same notice, the header stays
"Live", and the guidance is never ready. A lost connection stays an error.

**Set files.** The overview is read once per page. Set files are read only when
a page needs their targets, one after the other, and kept in the page's memory
by project and `imported_at` (a set never changes); a change of the project
empties the cache. The guidance page reads only the set of its target.

**Import in the browser.** The browser reads the GeoJSON file (up to 4 MiB, at
most 5000 features, checked before parsing), shows a preview and sends only the
chosen targets as the import package of the device (see *Stakeout target
sets*). Coordinates become e8 integers from the parsed numbers
(`Math.round(x·1e8)`, heights in millimetres). Point, MultiPoint, LineString and
MultiLineString are taken; every other geometry, a missing geometry and too few
coordinates are listed with the feature number and the reason. A `crs` other
than CRS84 or EPSG:4326, a position that is not a list of finite numbers, a
longitude or latitude outside the degree ranges and a height outside −20 000 to
100 000 m block the file with the path of the offending value. Names come from
`name`, `Name`, `NAME`, `title`, `label` or `id`, then `Feature.id`, then
"Point n"/"Line n" in the language of the page; vertices are named
`<line> / P001`. Texts and properties follow the device rules: at most 80
characters with the suffix kept whole, control characters become spaces, single
surrogates become U+FFFD, the edges are stripped as the device strips them, at
most 8 properties with keys of up to 40 characters (the first key wins after
cleaning) and values as text of up to 80 characters; every shortening or
omission is a hint. Further hints (repeated end vertex, zero-length segments,
lines with more than 100 vertices, positions with more than three values,
targets of different parts less than 2 cm apart, the extent, the distance to a
fresh rover position, swapped axes) never block. All positions are chosen while
they are at most 500, otherwise only the point positions; lines from 100
vertices on offer "every n-th vertex". A line keeps at most 2000 stored
vertices per set: each kept line first gets its two end vertices, the rest of
the budget is shared in proportion to the length. If the package exceeds the
device's body limit (`limits.import_bytes`), the budget falls to 1000, 500 and
250 vertices; a line thinned this way keeps the selected targets with their own
coordinates. A refusal of the device names its field: the request wrapper keeps
the `path` of an error answer (`error.path`), so the message ends with, for
example, "(targets[3].lat_e8)".

**Progress model and docking points.** The target list and the overview read
the status of a target from one model, `stakeoutProgress(overview)` → status,
mode, generation and counters per set. Without the `progress` field of the
overview every target is `open`; with it (the device has the progress store)
every target is `unknown` until the browser loads the progress list, and the
counters come from `sets[i].progress`. `unknown` is never counted or offered as
open. For the selected target the status in the live packet wins. Removing a
set sends the confirmed count of the set as `expected_confirmed`; while that
count is unknown, removing is locked. The confirmation actions
(`src/97-stakeout-actions.js`) sit in the guidance's action dock
(`[data-stakeout-actions]`); the core's `stakeoutProgress` and
`stakeoutConfirmActions` delegate to `stakeoutProgressView` and
`stakeoutActionButtons`, `loadStakeoutOverview` loads the progress list with
the overview, and the export has one project-wide slot in the overview.

**Confirming in the field app.** The action dock of a target offers Confirm
(`mode` `verified`, against the live packet), Found on site (`manual`, with an
optional one-line note of up to 500 characters), Skip and Reopen, each with the
target's generation as `expect_gen`. Without an answer the page sends the same
request body again after a second, at most three times in all, also after a
busy answer; it shows "Saved" only for `persisted: true` and otherwise says
"Not confirmed – check the list" and reads the progress list again. A result
that arrives after leaving the page appears only as a message. When the
progress list cannot be read, every target shows "? Unknown", no action is
offered, and the page offers "Load again"; while the list is missing it is
read again with the next live packet, at most every 10 s. The overview offers
the project-wide export as GeoJSON or CSV, the project map shows a "Stakeout"
button in its lower right corner besides the entry below the map, the archive
list marks archives with `stakeout_data`, and stakeout points are listed in the
project view with the type "Stakeout check".

**Guidance.** The page selects its target on the device only when the device has
another one, so a reload, a reconnect or a language change does not select it
again. A refusal offers the one action that helps (pause the line, activate,
select again once after `project_changed`, the overview); a target that is no
longer selected on the device (another browser, a deleted set) is reported with
"Select again" and is never selected silently. Above 5 m the search view shows
distance, bearing and a north-up map of the set; below 3 m the fine view shows
the walking direction ("0.30 m north / 0.29 m west", target minus the rover)
and a dial with the target in the middle, the tolerance ring and the rover
marker. The mode can be switched by hand until the next target. The map draws
the set without the survey geometry; following keeps the rover in the middle at
every zoom. All numbers come from the packet; a value the packet does not carry
is "—".

## Storage

### Files

All survey state lives in the filesystem root, prefixed with `tracking`; the
stakeout target sets have a folder `stakeout.sets/` of their own. No name at
the root may equal a firmware module name (see *Store folder* in
[Stakeout target sets](#stakeout-target-sets)):

| Path | Role |
|---|---|
| `tracking.index.json` | Atomically written pointer record: global sequence, checkpoint order, active segment, active project and line, feature count, point-store high-water mark and size |
| `tracking.checkpoint.json` | Line-oriented metadata checkpoint: header with order, segment, byte offset, and quality profiles; one record per project, line, and asset point; a footer with the active selection |
| `tracking.segment-NNNNNN.jsonl` | Append-only event journal, one segment at a time, at most 128 KiB |
| `tracking.points-v1.jsonl.g<generation>-<n>.pjs` | Point-store segments, at most 128 KiB each |
| `tracking.points-v1.jsonl.segments.json` | Point-store manifest naming exactly one validated generation |
| `stakeout.sets/<project_id>/<set_id>.json` | One immutable stakeout target set of a project, in a folder per project; see [Stakeout target sets](#stakeout-target-sets) |
| `stakeout.sets/<project_id>/progress.jsonl` | The stakeout progress log of a project (skips, reopenings, active target, part orders), compacted through `progress.jsonl.tmp`; a log of another project creation time is set aside as `progress.jsonl.foreign`; see [Stakeout progress and confirmation](#stakeout-progress-and-confirmation) |
| `stakeout.sets/<project_id>.deleting/` | The folder of a project whose deletion is in progress, removed after the journal commit or settled by the next start; see *Project deletion* in [Stakeout target sets](#stakeout-target-sets) |
| `tracking.archives/index.json` | Archive index (metadata only) |
| `tracking.archives/<archive_id>.ndjson` | One archived project, SHA-256 verified |

The index, the checkpoint, the point-store manifest, and the archive index are
written through the same `.tmp` / `.previous` sequence used for the
configuration, and each is parsed back and checked for its expected `kind` and
schema version before the rename is completed. The journal segments and the
point-store segments are append-only and are never rewritten in place.

A stakeout set file is not written through that sequence. The set is checked
completely in memory first; the first import makes the folders `stakeout.sets/` and
`stakeout.sets/<project_id>/`. The file is then written to a `.tmp` beside its
final name in 4 KiB pieces while a SHA-256 runs over the body, followed by the
trailer with the summary, read back in 4 KiB blocks and compared by body
digest, trailer, and length, and only then renamed onto its final name. The
final name never exists beforehand, because a set is never rewritten; this is
checked again right before the rename, since a rename would replace it. The
import yields to other tasks between all of these steps and after every
piece, and checks again right before the write, after every piece written or
read back, and right before the rename that no recording, measurement,
storage maintenance, or parallel benchmark has started and that the project
still exists (with the same `created_at`); otherwise
it stops after at most one more piece, the `.tmp` is removed, and no set is
stored. A listing never promotes or removes anything: a leftover `.tmp`
without a set file is `incomplete`, a set file whose trailer does not check
out is `damaged` (see [Stakeout target sets](#stakeout-target-sets)); both stay
visible and can be removed with `delete_set`. Each file
records the `created_at` of its project; a set whose project has a different
`created_at` is treated as orphaned and neither listed, read, nor deleted.

### The point store is the point journal

Since schema 4 the point store is both the compact storage format and the
canonical append-only journal for measured points. Full `vertex_added` copies
are no longer duplicated into the event journal. The event journal keeps every
other event: project, line, and state events, `asset_added`,
`feature_removed`, `project_frame_assigned` (a frame assignment or its
revocation, see [Reference frames](#reference-frames)), and the rare
`profile_registered` event.

Every journal event kind has one handler in `tracking.EVENT_HANDLERS`.
`Tracker._apply` runs it live, on replay and while a restore is staged, then
records the project's activity (kinds in `ACTIVITY_EVENTS`) and, outside a
replay, the live map change (`MAP_CHANGE_BUILDERS`). A kind that is not text is
treated as unknown. State an event changes must be part of the checkpoint
(project and line metadata, object points), because vertex measurements live in
the append-only point store.

Quality thresholds repeat for every point of a measurement session, so they are
deduplicated behind a stable profile ID derived from the threshold values
themselves. When a point would reference a profile that is not yet durable, the
profile definition is written to the journal and flushed *first*; only then is
the point written to the store and flushed. That ordering is what makes a
restart unable to find a point whose quality limits it cannot resolve.

The store keeps one packed 32-bit locator per point in RAM — a segment number
and a 20-bit offset within that segment — and an expansion cache bounded at 32
records. Full dictionaries are built only for the detail view, the API, and
exports. A line change starts a new segment, so archiving a whole line can
release whole segments instead of rewriting the store.

### Checkpoint and journal retirement

Compaction writes a checkpoint, writes the index, then opens an independent
`Tracker` and reads the checkpoint back. Only if the re-read reports the
expected order are journal segments up to and including the checkpointed
segment deleted. After that the point store is rewritten to drop records of
deleted lines, and only if the estimated temporary space still respects the
flash reserve.

`compact_async()` does the same in scheduler-sized phases, re-checking between
every phase that the event sequence, checkpoint order, checkpoint segment, and
point-store verification state have not changed. If anything moved, the
remaining journal is retained and the operation fails safely. A service window
of `CHECKPOINT_IO_SERVICE_MS` (100 ms) is inserted between the long flash
commits: accepting a socket, reading its request, and draining its response
takes several scheduler turns, and a one-millisecond yield could expire before
that chain finished. `compact_async()` refuses to run while the survey is not
loaded, so a tracker whose start-up failed never writes a checkpoint or
rewrites the point store.

`compact()` runs `compact_async()` to completion without the service windows;
it is for tools and tests outside the event loop and is never called from a
running task. The firmware itself only awaits `compact_async()` (background
checkpoint, archiving, the `compact` action).

### Recovery after a power failure

Start-up first settles an interrupted restore: a published one is completed,
an unpublished staging is deleted (see [Archives and restore](#archives-and-restore)).
It then loads the point store and does not replay everything. `_load_steps()`:

1. recovers `tracking.index.json` if its atomic write was interrupted,
2. loads the checkpoint, which gives the last covered event order, journal
   segment, and byte offset,
3. scans the point store to rebuild offsets and the maximum record order,
4. replays only journal segments at or after the checkpointed segment, seeking
   to the checkpointed byte offset in the first of them, and applies only
   events whose order exceeds the checkpoint.

A journal segment larger than `SEGMENT_HARD_LIMIT_BYTES` (512 KiB) is renamed
to `.oversize` and skipped rather than parsed. A segment containing malformed
lines is renamed to `.corrupt` and rewritten from its valid records, so one bad
line cannot cost the rest of the segment. A truncated final record in the point
store is cut back to the last complete record.

Because points are durable in the store before they are acknowledged, a restart
recovers from the store's high-water mark without losing a confirmed point.

A power cut, an aborted store rewrite or an interrupted cleanup can leave whole
point-store segment files (`<store>.g<generation>-<number>.pjs`) that no
manifest names any more; they only cost flash. After a successful load the
start-up deletes every such file of the store's own prefix that neither the
active manifest nor a `.previous` manifest names, one file per scheduler turn
with the watchdog fed in between, and a stale `<store>.segments.json.tmp` (a
reset between writing it and the switch; the main manifest still rules, and as
a later fallback the `.tmp` would revive a state that never became active). It
deletes nothing when the manifest is missing or invalid, or when the store had
to be recovered from `.tmp` or `.previous`: the unnamed files may then be the
only copy of newer data. Other stores (for example the parallel benchmark) and
the restore staging have their own prefixes and are never touched. The log
records `Removed <n> unreferenced point-store files (<bytes> bytes).`, and
`/status` reports the result as `stats.storage_recovery`. Only the boot does
this: a writer creates a new segment before the manifest names it, so no other
reader of the same store may delete unnamed files.

**Quarantine after a manifest recovery.** A recovery from `.tmp` or `.previous`
renames the fallback into the main manifest, so from the next start-up on the
store looks like an ordinary one. Before that rename it therefore writes
`<store>.recovered.json` (on the device `tracking.points-v1.jsonl.recovered.json`)
listing every segment file of the prefix that the fallback does not name; if
that write fails, the load fails and nothing is renamed. Every later sweep keeps
the listed files, and a marker that cannot be read keeps every unnamed file.
The quarantine ends only by an explicit decision: removing the marker (the next
start-up then deletes the files), or removing all listed files, after which the
next start-up removes the marker. A store rewrite and a restore never reuse a
generation that has listed files, and recording never starts a segment under a
listed name (it skips to the next number), because writing them would replace
the files. A marker that exists but cannot be read counts as present. The
choice of a marker over "never delete a generation newer than the active one"
is deliberate: generations are not monotonic, since a fresh store and a legacy
migration take theirs from the clock, which can be unsynced or stepped. While a
quarantine exists, a stale `.tmp` is kept as evidence. `/status` reports
`stats.storage_recovery.quarantined_point_files` and `quarantined_point_bytes`
(`null` without a quarantine), and each start-up logs a `WARN` naming the
marker. See [known-limitations.md](known-limitations.md), *Point-store files are
kept after a manifest recovery*.

### Flash reserve

Writing survey data is blocked while less than `MIN_FLASH_RESERVE_PERCENT`
(20 %) of the filesystem is free. Filling a device to the last byte is how
LittleFS operations start failing in ways that are hard to recover from in the
field.

The reserve is never evaluated against the raw free space alone. Each check
adds `OPERATIONAL_FLASH_HEADROOM_BYTES` — 278 528 bytes, being one new journal
segment and one new point-store segment at 128 KiB each plus two bounded
error-log generations at 8 KiB each — on top of whatever the caller estimates
it needs. Existing files are already accounted for by `statvfs`; this headroom
covers their possible growth between two checks.

`statvfs` is comparatively slow, so the normal point path samples it at most
every `FLASH_RECHECK_WRITES` (128) writes and, in between, tracks estimated
usage against the last real measurement: every recorded event adds
`max(2048, journal_bytes * 2)` to the running estimate, and on the
`vertex_added` path — where the point itself is not written to the journal —
`journal_bytes` is 0, so each point simply adds the 2048-byte floor. The
cache is a conservative approximation, never a licence to fall below the
threshold: a line start, a journal or point-store rotation, a checkpoint, an
archive operation, and a store rewrite all force a fresh synchronous
measurement.

When an automatic recording hits the reserve, the line is paused with the
reason `storage_reserve` instead of failing silently.

### Archives and restore

Archiving streams one project to
`tracking.archives/<archive_id>.ndjson`, verifies it by parsing it back and
comparing its SHA-256 digest, and only then removes the active source. The
archive index holds metadata only, so start-up loads zero archived points.
Reactivating an archive re-checks the point limit, the integrity digest, and
the flash reserve before it touches the active store.

A restore — from the API or from an uploaded NDJSON backup — never writes into
the live store. It builds a separate staging tracker under `tracking.restore`,
validates fields, projects, lines, and points there, and activates it only
after the whole payload has passed. JSON payloads and NDJSON records go through
the same staging path one record at a time, so both formats produce the same
survey. A restore stores each project's features in backup order (lines with
their vertices, then object points) and renumbers `record_order` accordingly.
A JSON backup without an active project restores without one, and a line's
pause reason is kept. A profile ID in a backup that neither derives from its
limits nor has the device form is not stored: measurements that refer to it get
the ID derived from their limits, and compact archive rows without limits are
refused.

Activation survives a reset at any write. The staging store is verified, then
published by one atomic rename of `tracking.restore.commit.json`, a small
record that names the staged point segments, their new generation, the string
table and the staged journal segments. Before that rename nothing of the live
survey has changed, and the next start-up deletes the half-finished staging.
After it, the restore is installed step by step: the point segments move to
the new generation and the point manifest switches, the journal segments,
index and checkpoint move into place, files of the old survey are deleted,
and the record goes last. Every step can run again, so a start-up that finds
the record repeats the installation, even after a second reset. The
checkpoint and the point store therefore switch together, and after any reset
the device holds either the complete original survey or the complete restore,
never a mix. A cancelled or failing installation after the rename finishes at
once instead of rolling back; if flash refuses even that, the survey becomes
unavailable (`tracking_startup` `error` with the code `restore_install_pending`)
until the next start-up completes it, and `POST /api/tracking/import` answers
`409 restore_install_pending` asking for a restart.
Until then nothing writes the store: queued points and pending index or
checkpoint writes of the old survey are dropped, and the background worker
waits, so it cannot write the old survey over files already installed.
A record that cannot be read (damaged flash) stops the start-up with
`Survey recovery failed` and `tracking_startup` code `restore_commit_unreadable`,
and changes no file, so nothing is lost before it is examined; an installation
that keeps failing reports `restore_install_failed`. Both repeat on every
start-up until resolved by hand. Only "file not found" counts as absent during the installation: any
other read error, or an old journal segment, stale `.tmp`/`.previous` or the
record itself that cannot be removed, stops it with an error instead of
being taken for an already completed step.
`stats.storage_recovery.restore` reports `completed` or `discarded` when a
start-up had to do either. Backups are written under schema 5. A
JSON restore also reads schemas 1 to 4; NDJSON backups, archives and the
journal also read schemas 2 to 4 (`POST /api/tracking/import` takes schemas 4
and 5, and the field app sends older NDJSON files as a JSON restore). Legacy
values are converted per value during staging, and a legacy restore that would
keep no point at all is refused. Schema 4 and schema 5 backups must also match
their SHA-256 integrity record and their declared record count; an integrity
record in an older backup is checked as well.

**Points recorded before V15.9.0.** Schemas 2 to 4 stored float degrees and
metres. On load, import, or restore the coordinates become integer triples and
every measurement is tagged `precision: "legacy_float32"`; a stored integer is
never converted twice. Such a point has no geoid separation, ellipsoid height,
antenna height, reference frame, epoch, or mountpoint. In the stored data and
in a backup those keys are absent or `null`; the loader adds only the
`precision` tag and fills in nothing else. Only the exports normalise them,
as described under [Exports](#exports): `null` or empty cells, and `unknown`
for the frame. Its `ground_height_msl_m` is `null` in the exports. The chainage
and lateral offset of older object points keep the values of the former
spherical distance model. A value that cannot be converted (for example an
extrapolated projected height out of range) is dropped or nulled on its own,
counted in `legacy_conversion_errors`, and logged; one bad value never costs
the rest of the file. The first load keeps a copy of the former checkpoint as
`tracking.checkpoint.json.v4` when flash allows, and a factory reset does not
remove it.

**No downgrade.** Files written under schema 5 cannot be read correctly by
older firmware (see the upgrade notes of V16.0.0 in the changelog). Export a backup before
installing an older version. The point store keeps its schema 3 manifest until
the first point is recorded under V15.9.0 (`_ensure_writer` relabels it before
that write), so only after that does V15.8.1 show line points as missing;
object points are affected as soon as a V15.9.0 checkpoint exists.

## HTTP API

### Server behaviour

The portal listens on `http_port` (80) on both interfaces, over plain HTTP.
There is no HTTPS: a device with no trusted name and no certificate authority
on a local network cannot obtain a certificate a browser would accept.
Accordingly, the portal is only as private as the network it is on, and the
[transport and security model](transport-security.md) describes the
consequences.

`web_routing.py` holds the transport policy — allowed methods, the 405 message,
and the body limit — as a table. Authentication and domain decisions stay in
the handlers on purpose: a route table must not be able to silently widen the
device's trust boundary.

| Property | Value |
|---|---|
| Concurrent connections | 6, further connections get `503` with `Retry-After: 2` and an empty body, for every method |
| Event streams | 3 active, 4 open including evicted ones still closing; the oldest gives way; outside the connection limit |
| Header deadline | 10 s |
| Body deadline | 10 s |
| Default body limit | 4096 bytes |
| Body limit for `/api/tracking` | 262 144 bytes |
| Body limit for `/api/tracking/import` | 8 MiB |

Responses are HTTP/1.0 with `Connection: close`, so every request uses its own
connection. The one exception is `GET /api/events`, which answers
`text/event-stream` and stays open (see [Event stream](#event-stream)). Each response carries `X-Content-Type-Options: nosniff`,
`Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`, and a content security
policy that permits scripts, styles, images, and connections only from the
device itself. JSON responses and portal pages are sent `Cache-Control:
no-store`; the versioned browser assets are sent
`public, max-age=31536000, immutable`, which is why `ASSET_VERSION` has to be
raised whenever they change.

Errors from JSON endpoints use one stable envelope,
`{"error": "<code>", "message": "<fallback>"}`, and stay English regardless of
the portal language. The same holds for every machine-readable field: NMEA,
RTCM, `$PESPS`, logs, UUIDs, SSIDs, and receiver replies are never translated.

Every refusal and failure has one documented status:

| Status | When | Codes and format |
|---|---|---|
| 400 | Invalid request: a broken request line, an incomplete header or body, or a value the domain rejects | `text/plain` "Invalid HTTP request." for a broken request line or header; JSON codes such as `invalid_tracking_request` for domain validation |
| 401 | No valid session, or an expired one, also for writes | `admin_session_required`; portal pages answer with the sign-in page, `/rtcm` and `/errors` with plain text, `/save` with a text message, `GET /api/session` with `{"authenticated": false}` |
| 403 | A valid session, but the CSRF token or the `Origin` was refused | `csrf_rejected`, `origin_rejected`; the sign-in itself answers `403` "Origin rejected" (plain text, no session involved) |
| 404 | Unknown path or object | `text/plain` for unknown paths; JSON codes such as `project_not_found` |
| 405 | Method not allowed for the path | `text/plain`, message from `web_routing.py` |
| 409 | A state the user has to resolve first | `storage_busy` (changes during storage maintenance), `recording_active`, `benchmark_active`, `restore_install_pending`, `open_change_pending`, … |
| 413, 431 | Body or header too large | `text/plain`, no envelope |
| 429 | Sign-in blocked after repeated failures | `text/plain` |
| 500 | An unexpected error in a request handler | `internal_error`, only while no answer has begun; after that the connection is closed and nothing is appended |
| 503 | Transient; try again | `survey_loading`, `storage_busy` (readers during storage maintenance), `device_busy` (not enough memory), and too many connections (empty body, `Retry-After: 2`) |
| 507 | Flash reserve or flash write/read failure | `storage_reserve`, and the endpoint-specific write and read failure codes |

`HEAD` answers carry the same status line and headers as `GET`, including
`Content-Length`, and never a body; this also holds for early refusals such as
`400`, `431`, and the connection limit. A request line must have exactly three
fields separated by single spaces: a method of upper-case letters `A`–`Z`, a
target starting with `/`, and the version `HTTP/1.0` or `HTTP/1.1`; anything
else gets `400`.

### Authentication

There is exactly one credential for the portal: the **device code** printed on
the device label. `POST /api/session` exchanges it for a session. Sign-in is
rate-limited per client address — after 5 failures within 10 minutes, further
attempts are blocked for 60 seconds — and the comparison is constant-time.

A session is a random token in an `HttpOnly; SameSite=Strict` cookie named
`rtk_session`, held in RAM only. It expires after 30 minutes of inactivity or
8 hours in total, whichever comes first. Sessions do not survive a restart.

Each session also carries a **CSRF token**, returned by `GET /api/session`.
Every state-changing request must present it in the `X-CSRF-Token` header; the
one HTML form endpoint, `/save`, also accepts it as the `csrf_token` field.
State-changing requests additionally require the `Origin` header, when present,
to match the request host. `Origin: null` is accepted only on the setup access
point, only while the access state is `SETUP`, `RECOVERY`, or `APPLYING`, and
only when the `Host` header is the AP address or the device hostname — some
browsers send `null` for local HTTP pages, and the setup portal has to work
there.

The same session is required on the access point and on the station interface.
Being on the setup network grants no extra rights.

A request without a valid session gets `401 admin_session_required`, also when
it changes state. A request with a valid session but a missing or wrong CSRF
token gets `403 csrf_rejected`, and one whose `Origin` is refused gets
`403 origin_rejected`; the session stays valid in both cases. The Field App
returns to the sign-in page only on `401`; on `403` it reports the refused
action and fetches a fresh CSRF token for the next attempt.

The `config_token` key is obsolete. It is still read from an old
`config.json` and persists across a firmware update - only a factory reset
clears it - but no endpoint authorizes on it. `status_token` no longer exists:
an old value is ignored on load and disappears with the next configuration save.

In the table below, **session** means a valid `rtk_session` cookie, and
**session + CSRF** additionally means a matching CSRF token and an acceptable
`Origin`.

### Field App routes

The Field App is ordered by field task: **Measure**, **Projects**, **Device**.
`resolveRoute` (`src/20-navigation.js`) maps a hash to one canonical path
without touching the DOM or the history; the app replaces an old hash with the
canonical one by `history.replaceState`, so Back never returns to an alias.
Each segment is decoded on its own (`projects/a%2Fb` is the project `a/b`).

| View | Canonical path | Area |
|---|---|---|
| Measure | `measure` | Measure |
| Field setup | `measure/setup` (focus segments `method`, `rules`, `frame`) | Measure |
| Projects | `projects` | Projects |
| Archive | `archive` | Projects |
| Project | `projects/:id` | Projects |
| Stakeout | `projects/:id/stakeout` (`import`, `targets`, `target/<tid>`) | Projects |
| Device overview | `device` | Device |
| Connections | `device/connections` (`internet`, `access`) | Device |
| Operation & display | `device/display` | Device |
| Diagnostics | `device/diagnostics` | Device |
| RTCM details | `device/diagnostics/rtcm` | Device |
| Log | `device/diagnostics/log` | Device |
| Receiver console | `device/diagnostics/console` | Device |
| Maintenance | `device/maintenance` | Device |

Aliases (exact path comparison; a target is never an alias itself):

| Old hash | Opens |
|---|---|
| `status` | `measure` |
| `device/settings` | `device/display` |
| `device/configuration` | `device/connections/internet` |
| `device/access`, `device/label` | `device/connections/access` |
| `device/rtcm` | `device/diagnostics/rtcm` |
| `device/log` | `device/diagnostics/log` |
| `device/module` | `device/diagnostics/console` |

Fallbacks: an unknown path under an area opens the area root (`device/x` opens
`device`, `measure/x` opens `measure`); any other unknown hash opens `measure`.
The stakeout path opens the stakeout pages of the project (see *Stakeout in the
field app*); an unknown part below it opens the stakeout overview. The captive-portal
entry `/#device/configuration` and printed or bookmarked links keep working
through the aliases. After an expired session the sign-in page returns to the
page the user was on (from `?return=` or the hash); a target that does not match
the sign-in target pattern, for example a project ID with capital letters or
`%`, lands on **Measure**.

The measurement method is a device setting (`measurement_defaults.samples` in
`/api/ui/live`, written through `PUT /api/measurement-gate`). The browser no
longer keeps its own value; a legacy `rtk-samples` entry in `localStorage` is
removed at start. The reminder about an unknown frame (#38) shows on **Measure**
for an RTK fix (quality 4 or 5) with an empty `reference_frame` in
`measurement_defaults`; it can be hidden per browser tab session
(`sessionStorage["rtk-frame-reminder"]`).

#### Route cache and live bindings

`src/17-route-cache.js` keeps an in-memory route cache, keyed by the canonical
`route.path`, with at most 6 entries (least recently used first out). Only an
allow list is cached: **Projects** (`projects`), **Archive** (`archive`) and
the **project** view (`projects/:id`). Nothing else is ever cached: not the
device views (the overview shows quality and state from `/status`, which would
mislead as old data), not **Measure** or the field setup (live-bound), not
Connections (form, secrets), display, diagnostics, RTCM, log, console,
maintenance or the stakeout path. A new view is therefore uncached unless it is
added to the list. Nothing is written to `localStorage` or IndexedDB; a reload
starts empty.

`swrMount` renders a cache hit synchronously, before the first `await`, and
loads fresh data afterwards (stale while revalidate). The cached render carries
a stamp "As of hh:mm:ss, updating ..." (browser clock at the time the entry was
saved). The stamp owns its place: after a successful fetch in this mount it
turns invisible but keeps its box, so the page does not move; if the fresh data
differs, the view is rendered again with the same stamp node. "Differs" means
only what the page shows: the library and the archive compare project ids,
names, counts, progress, update times, frame notices, the active state, the
journal size and the archive entries, not the fix, the quality streak, the
measurement progress or the active line's timing that `/api/tracking` also
carries; unchanged data keeps the page's nodes, open sections and focus. A failed update
keeps the view, shows "Update failed" with "Try again" and no error page. A
cached view never shows a position, a quality chip or a "connected" message
taken from the cache; "Projects updated" and "Map updated" appear only after a
network fetch.

Own changes forget cached data in two levels. `forgetTrackingCache(projectId)`
drops the library, the archive and that project's entry (rename, delete,
archive, reactivate, frame assignment and revoke, a confirmed "Project not
found"). `forgetTrackingCache()` without an argument drops the library, the
archive and every `projects/<id>` entry (restore, compaction, create, activate,
capacity choice and every full map replacement). Recording points and lines
forgets nothing; the map fetches them as a delta.

The project map revalidates through the revision delta. `mapRefreshKind(map,
live, bootId)` answers `"none"`, `"delta"` or `"full"`: a revision that went
back (restore), a changed `boot_id` (device restart) or an unreadable map
revision gives `"full"`. A delta replaces only the SVG graphic; the tools, the
map menu, the chip, the legend, the selection and the open detail view keep
their nodes, and the lists and counters follow. A full answer, also one the
server sends despite `since_revision` after the change window ran out, replaces
the whole map, closes the open detail view, rebuilds the map once and drops the
whole project cache. The map gestures live in `bindMapGestures` (`src/65-map.js`).
A cached project view reads `/api/tracking` once per mount. If what the view
shows changed (`projectViewSignature`; of the frame tally only the "unknown"
frame and, in the mixed notice, the counts), it renders once more from that
confirmed state, after a running map round so that no map request is aborted,
and that render does not read again. Points recorded meanwhile keep arriving
through the map delta, not through further revalidations.

`skeleton(label)` shows after 1 s (`LOADING_TEXT_DELAY_MS`) what is loading and
the elapsed seconds. Only a skeleton that is a direct child of `main` (a whole
page) offers "Cancel loading": it aborts the route signal, changes nothing and
offers "Try again". A section skeleton shows text only, because the section sits
next to content that can already be edited.

New live snapshots arrive in one place, `acceptLive(live)` (`src/90-live-router.js`):
connection state, `store`, the global status and the views bound with
`bindLive(signal, fn)`. The poll in `pollLive` and the map poll call it; a push
channel can call it the same way. A binding ends with the route signal. A view
mount is an entry of `VIEW_MOUNTS` with `(route, signal)`; disposing a view is
aborting its route signal (before every new mount, on a hidden tab, offline and
on "Cancel loading"); there is no separate dispose hook.

### Endpoints

| Method | Path | Authentication | Purpose |
|---|---|---|---|
| GET | `/` | session | Field application (measure, projects, device); returns the sign-in page with `401` otherwise |
| GET | `/ui` | none | Redirect to `/` |
| GET | `/ui-v2` | session | Same application as `/`, kept as a stable alias |
| GET | `/setup` | session | Basic fallback configuration page (the field app is the default) |
| GET | `/label` | session | Device label page with the Wi-Fi and portal QR codes |
| GET | `/api/label` | session | Label data as JSON: names, device code, stream token, BLE passkey, QR payloads |
| GET | `/api/access` | none | Connection contract: hostname, ports, NMEA TCP authentication mode, BLE service and characteristic UUIDs |
| GET | `/api/captive` | none | RFC 8908 captive-portal status and portal URL of the field app |
| GET | `/api/events` | session | Server-sent event stream with the live snapshot, heartbeats and a closing `bye`; see [Event stream](#event-stream) |
| GET | `/api/ui/live` | session | Compact live snapshot used by the field loop: current fix with its freshness, quality verdict, `gate_deviations`, `frame_suggestion` (the rated RTCM 1300 frame or `null`, see [Field setup](#field-setup)), tracking state, RTCM ages, network, NTRIP states, diagnostics |
| GET | `/status` | session | Full status: services, counters, memory, network, identity; the fix with its freshness, the RTCM ages, `gate_deviations` |
| GET | `/health` | session | Alias of `/status`; one handler serves both |
| GET | `/rtcm` | session | RTCM message statistics and MSM families, with `service_crs`, `rtcm_crs` and `crs_frames` at the top level |
| GET | `/errors` | session | Plain-text RTC flight recorder plus the persistent error log |
| GET | `/api/log` | session | Incremental error-log entries; `cursor`, `limit` (max 200), `level` |
| GET | `/diagnostics` | session | Diagnostics page |
| GET | `/api/diagnostics` | session | Diagnostics snapshot; `download=1` sends it as an attachment |
| GET | `/api/wifi-scan` | session | Visible networks; refuses with `409` while a line is recording (not paused) or a measurement runs |
| GET, POST | `/api/field-diagnostics` | session (+ CSRF on POST) | Field recorder status; `start`, `stop`, and `mark` actions |
| GET | `/api/field-diagnostics/export` | session | Streams the recorded NDJSON; refuses while the recorder runs |
| GET | `/api/config` | session | Current configuration with passwords replaced by `*_set` booleans and `staging` (`revision`, `keys`, `restart_required`, no values) |
| PUT | `/api/config` | session + CSRF | Validate; apply live and caster keys now, stage Wi-Fi keys until apply; returns `pending`, `restart_required`, `applied`, `staged`, `staging_revision`; optional query `staging_revision` returns `409 staging_changed` when the staging changed |
| POST | `/api/config/apply` | session + CSRF | Arm the staged change and restart; `409` when there is nothing to apply or a change is still being verified; optional body `apply_id`, `staging_revision` (`409 staging_changed`); answers `apply_id` |
| POST | `/api/config/restore` | session + CSRF | Validate a schema 1, 2 or 3 configuration backup (body up to `CONFIG_RESTORE_BODY_LIMIT`, 16384 B), then apply it as one change and restart; optional query `apply_id`; answers `schema_version`, `sections`, `kept`, `apply_id` |
| GET | `/api/config/backup` | session | Download the schema 3 configuration backup (settings, measurement rules and method, field setup, stakeout settings), credentials included |
| GET | `/api/config-result` | session | Outcome of the last configuration attempt, with `boot_id` and `apply_id` |
| POST | `/save` | session + CSRF | Legacy HTML form path: validates like `PUT /api/config`, then applies and restarts |
| GET | `/api/session` | none | Reports whether the caller is signed in and returns the CSRF token |
| POST | `/api/session` | device code | Sign in and set the session cookie |
| DELETE | `/api/session` | session + CSRF | Sign out and clear the cookie |
| GET | `/api/measurement-gate` | session | Current and factory acceptance thresholds |
| PUT | `/api/measurement-gate` | session + CSRF | Replace all eight thresholds, the measurement method, or both |
| DELETE | `/api/measurement-gate` | session + CSRF | Reset the eight thresholds to the factory values; the measurement method stays |
| GET | `/api/field-setup` | session | Antenna height and reference-frame labels, with the defaults; see [Field setup](#field-setup) |
| PUT | `/api/field-setup` | session + CSRF | Validate and save any subset of the three field-setup keys at once, without a restart |
| GET | `/api/tcp-auth` | session | Whether NMEA TCP clients must authenticate |
| PUT | `/api/tcp-auth` | session + CSRF | Enable or disable that requirement |
| POST | `/api/stream-token/rotate` | session + CSRF | Issue a new stream token and close all NMEA TCP client connections |
| POST | `/api/ble-maintenance` | session + CSRF | Allow BLE writes to reach the receiver for 600 s |
| POST | `/api/reboot` | session + CSRF | Deferred soft restart; optional body `apply_id`, answered with `apply_id` |
| GET | `/gnss` | session | Buffered replies to receiver commands |
| POST | `/gnss` | session + CSRF | Send one validated `$PQTM` or `$PAIR` command as text; answers 503 when the receiver input stays busy with a correction frame for 5 s or the command queue is full |
| GET, POST | `/api/gnss-command` | session (+ CSRF on POST) | The same, with a JSON body `{"sentence": "..."}`, and the same 503 |
| GET | `/api/tracking` | session | Survey status, capacity, current fix and quality; `view=progress` and `view=name_check` |
| POST | `/api/tracking` | session + CSRF | All survey actions (see below) |
| GET | `/api/tracking/map` | session | Bounded map geometry; `limit` (max 500), `project_id`, `since_revision` |
| GET | `/api/tracking/line` | session | Paged line vertices with their quality details; `line_id`, `offset`, `limit` |
| GET, POST | `/api/tracking/export` | session (+ CSRF on POST) | GeoJSON, CSV, backup, or archive export |
| POST | `/api/tracking/import` | session + CSRF | Upload an NDJSON survey backup |
| GET | `/api/stakeout` | session | Target set overview of a project; `set_id` answers one set file |
| POST | `/api/stakeout` | session + CSRF | Stakeout actions: `delete_set`, `select_target`, `set_order`, `confirm`, `skip`, `reopen`; see [Stakeout progress and confirmation](#stakeout-progress-and-confirmation) |
| GET | `/api/stakeout/progress` | session | Target states, counts, floors, orders and the active target of a project |
| GET, HEAD | `/api/stakeout/export` | session | Streamed GeoJSON or CSV of the stakeout targets (whole project or one `set_id`) with their confirmation state at export start |
| GET | `/api/stakeout/live` | session | One-second stakeout packet: fix, corrections, gate, motion, active target and guidance; see [Stakeout session and live guidance](#stakeout-session-and-live-guidance) |
| GET, PUT | `/api/stakeout/settings` | session (+ CSRF on PUT) | The stakeout tolerance and fine radius in metres; `PUT` takes either or both |
| POST | `/api/stakeout/import` | session + CSRF | Upload a normalized stakeout target package (JSON, up to 128 KiB); see [Stakeout target sets](#stakeout-target-sets) |
| GET, POST, DELETE | `/api/parallel-benchmark` | session (+ CSRF on POST and DELETE) | Arm, stop, and inspect the isolated load test |
| GET | `/api/parallel-benchmark/export` | session | Export the benchmark result |
| GET | `/base.css`, `/pages.css`, `/pages.js`, `/i18n.js`, `/app.css`, `/app.js`, `/qr.js` | none | Immutable versioned browser assets; `app.css`, `app.js` and `i18n.js` are also served pre-compressed |
| GET | `/generate_204`, `/gen_204`, `/hotspot-detect.html`, `/library/test/success.html`, `/ncsi.txt`, `/connecttest.txt`, `/redirect` | none | Captive-portal probes, redirected to `http://<ap_ip>/#device/configuration` |

`HEAD` is accepted on every path whose table row starts with `GET` alone, and
additionally on `/api/tracking`, `/api/tracking/export`, `/api/tracking/line`,
and `/api/tracking/map`. It answers `405` on `/api/config`, `/api/session`,
`/api/measurement-gate`, `/api/field-setup`, `/api/tcp-auth`, `/api/field-diagnostics`,
`/api/gnss-command`, `/api/parallel-benchmark`, `/api/stakeout`,
`/api/stakeout/settings`, `/gnss`, and every
`POST`-only path.

### Live fix and freshness fields

`GET /api/ui/live` (`current_fix`), `GET /api/tracking` and
`GET /api/tracking?view=progress` (`current_fix`), and `GET /status` (`fix`)
serve the same fix object, built by one function, so that they cannot
contradict each other. It holds the last published fix and the fields below.
A GGA becomes the published fix when more than `fix_update_interval_ms` has
passed since the previous published one; a GGA that arrives sooner is not published as a fix (the NMEA output is not
affected).

| Field | Content |
|---|---|
| `lat_e8`, `lon_e8`, `alt_mm`, `geoid_sep_mm` | the exact position and heights as integers (1e-8 degree, millimetre), taken from the receiver's text. `lat_e8` and `lon_e8` are `null` at fix quality 0 or when the receiver's text is not a valid NMEA coordinate (no hemisphere, minutes of 60 or more, a sign, a space, an exponent); `alt_mm` and `geoid_sep_mm` are `null` when the field is empty or not a plain decimal number. Before the first GGA has been parsed these keys are absent (see below) |
| `lat`, `lon`, `alt` | the same values as floats for display, derived from the integers (on the device these are 32-bit and not exact). Each float is `null` exactly when its integer is, so `lat` and `lon` are `null` at fix quality 0 or for an unreadable coordinate |
| `altitude_msl_m`, `geoid_sep_m`, `altitude_ellipsoid_m` | float heights from GGA fields 9 and 11, derived from `alt_mm` and `geoid_sep_mm` (so a height with more than three decimals is rounded to the millimetre); the ellipsoid height is their sum |
| `qual`, `fix_status_text`, `sats`, `hdop`, `utc` | GGA fields 6 and 7, the quality as text, HDOP, and the UTC time field |
| `correction_age_sec`, `station_id` | GGA fields 13 and 14 (nullable, the station ID is text) |
| `receiver_accuracy` | the GST estimate of the same epoch, once it has arrived |
| `fix_age_ms` | milliseconds on the device tick clock since the fix was published; `null` before the first fix |
| `fix_seq` | counter of published fixes since start-up; it grows by one per published fix |
| `fix_rev` | counter that grows by one each time a GST estimate is attached to the current fix; `(fix_seq, fix_rev)` identifies one state of the fix |
| `stale` | `true` once `fix_age_ms` reached `fix_stale_after_ms` (3000 ms by default); stays `true` until a new fix is published, even across a tick-counter wrap |
| `pending_gst` | `true` while the verdict of a GGA is held for its GST (up to 2 s); the verdict shown meanwhile is the previous one |
| `held_receiver_accuracy` | only while `pending_gst` is `true`: the accuracy the held verdict was judged on |

Before the first GGA of a boot has been parsed the object holds
`fix_status_text` (`NO_FIX`), `qual` (0), `sats` (0), the freshness fields, and
`pending_gst` (with `held_receiver_accuracy` while it is pending): the position
and other GGA-derived keys are absent, not `null`. A client therefore has to check `qual`
first and read the position keys only when it is not 0. A stale fix keeps its
position fields; the browser greys it out. Its verdict is
the one for no fix, and starting a measurement fails. `GET /status` also
carries `fix_age_sec`, the age rounded to 0.1 s. `current_quality` holds the
gate verdict (`accepted`, `reasons`, `limits`), and `quality_streak` how long
the fix quality and the verdict have been unchanged and when RTK FIXED was
last seen.

The app also measures the age of the last device data with a fix itself, on
the browser's monotonic clock. The measure screen polls every 2 s; answers to
recording actions do not count, because they carry no fix. From 3 s without
such data the age is marked ("Updated N s ago"). From 10 s the screen turns
grey, locks all recording actions and open measurement dialogs, and says "No
status data from the device for N s" (or "... yet" when no data arrived since
the app was opened). The first fresh answer restores the normal state. On
pages with a 5 s poll both thresholds are 3 s later. The header pill shows the
fix and one connection word with a symbol (Live, Delayed, No data); the symbol
and its colour follow only the data link, never the gate. The fix label names
the gate: red while the device blocks measuring, with the word "blocked" after
the connection word (unless a recording or measurement holds the line) and
"measurement blocked" in its title and accessible name; muted while there is no
verdict, the position is outdated or no fresh data arrives. A dedicated live region announces state changes only, and stays
silent when switching pages or returning to the tab.

Recording actions report in the action bar of the measure screen; on other
screens the messages form one block at the end of the page flow. The bar shows
a running state and the result. **Undo** is offered for 6 s and is sent with
the candidate check `expect` (see [Tracking actions](#tracking-actions)). A
rejection stays visible with one line per gate reason from `reasons` in the
error envelope. A cancel counts only once the device confirms it. After a lost
answer, or after a 500 or 503, the browser asks the device for its own
`request_id` and reports "saved" only on a match; otherwise it says that the
result is unclear and links to the project. The capacity display is compact
while the level is `ok`, shows a message from `notice` and stays on screen
from `critical`. When no corrections reach the receiver, the measure screen,
including the measurement progress, names the effective cause from
`network.ntrip_state`, `network.ntrip_config` and
`network.last_ntrip_error_code` and offers the matching settings. The sheet
"Why not?" on the measure screen checks app connection, receiver, Wi-Fi,
caster, corrections and accuracy step by step and names every gate reason.

The other fields added by the data-basis work:

| Field | Where | Content |
|---|---|---|
| `rtcm.arrival_age_sec`, `rtcm.last_msg_age_sec` | `rtcm` in `/rtcm`, `/status`, `/api/ui/live` | the arrival age (same value in both) |
| `rtcm.epoch_age_sec`, `rtcm.epoch_source`, `rtcm.stale` | same | the epoch age, the constellation of the last decoded MSM epoch, and whether the epoch age exceeds `ntrip_max_epoch_age_sec`; definitions in [Correction ages](#correction-ages) |
| `rtcm.messages`, `rtcm.msm`, `rtcm.bytes_total`, `rtcm.mount` | same | message counts by number, MSM families, byte total, configured mountpoint |
| `network.ntrip_state`, `network.ntrip_substate` | `/api/ui/live` | the connection state and `stale` or `null` |
| `stats.ntrip_substate`, `stats.last_ntrip_error_code` | `/status` | the same substate; `last_ntrip_error_code` holds one of the codes in *Status line handling* |
| `link.receiver` | `/api/ui/live`, `/status`, `/health`, `/api/diagnostics` | `ok`, `silent` or `unknown`. `unknown` while the GNSS handler is not running (safe mode), `silent` once `link.uart_silence_ms` reaches `fix_stale_after_ms` (3000 ms by default), otherwise `ok`; see [Silent-stream detection](#silent-stream-detection) |
| `link.uart_silence_ms` | same | milliseconds on the tick clock since the last UART read that delivered bytes (reads that return nothing do not count), or since the GNSS handler started while no byte has arrived yet; `null` while the handler is not running. It stays at or above 2^29 once silence was reported, until a byte arrives, even across a tick-counter wrap |
| `link.corrections` | same | `off` when `network.ntrip_state` is `disabled`; `ok` or `stale` while it is `streaming`, `stale` exactly when `rtcm.stale` of the same snapshot is `true` (a single rule, see [Correction ages](#correction-ages)); `down` in every other state (`waiting_wifi`, `connecting`, `backoff`, `auth_error`, `mount_error`) |
| `link.rtcm_silence_ms` | same | milliseconds since the last recognised RTCM message number, the same reference point as `rtcm.arrival_age_sec`, but measured on the uptime counter, so it keeps growing instead of wrapping with the tick clock; `null` before the first message |
| `network.last_ntrip_error_code` | `/api/ui/live` | the last NTRIP connection error code (one of the codes in *Status line handling*), `null` when there is none |
| `network.ntrip_config` | `/api/ui/live` | `switched_off` (`ntrip_enabled` is false), `incomplete` (enabled, but no caster with host and mountpoint) or `ready` (at least one caster), `null` before the NTRIP client has started (for example in safe mode). It is the state seen by the running client, which reads it again after every reload of the caster settings, so a saved change shows here without a restart. `ntrip_state` `disabled` alone does not say which of the first two applies |
| `gnss_accuracy.error` | `/api/ui/live` | the error text of the last failed GST output request, reduced to printable ASCII without `"` and `\` (other characters become `?`) and cut to 80 characters; `null` without an error |
| `gate_deviations` | `/api/ui/live`, `/status`, `/api/tracking` | limits looser than the factory values; see [Acceptance gate](#acceptance-gate) |
| `measurement_defaults` | `/api/ui/live` | device-wide defaults for new measurements: `antenna_height_m`, `reference_frame`, `reference_frame_epoch` and `samples` (the method, see [Acceptance gate](#acceptance-gate)) |
| `config_warnings` | `/api/ui/live`, `/status`, `/api/tracking` | `{key, message}` for each field-setup, NTRIP port, `measurement_samples` or `wdt_timeout` value in `config.json` that the loader rejected and replaced by its default; see [Field setup](#field-setup). `key` `config_file` while an unreadable `config.json` is protected from writes; see [Storage and recovery](#storage-and-recovery) |
| `stats.http_rejected_requests` | `/status` | HTTP requests answered with 400 or 431 because they could not be read (malformed or header too large), counted from 0 at each start, including those whose log line was throttled; see [Persistent error log](#persistent-error-log) |
| `system.boot_count` | `/api/ui/live`, `/status` | consecutive starts that look like a crash; it clears after a power-on, a planned reset or any other non-crash reset, or once an uptime of `SAFE_MODE_UPTIME_SEC` is reached, and `SAFE_MODE_BOOTS` such starts in a row start safe mode (see [Safe mode](#safe-mode)) |
| `stats.storage_recovery` | `/status` | what the last start-up repaired: `restore` (`completed`, `discarded` or `null`), `unreferenced_point_files` and `unreferenced_point_bytes`, and `quarantined_point_files` and `quarantined_point_bytes` still kept after a manifest recovery (`null` without one); see [Recovery after a power failure](#recovery-after-a-power-failure) |
| `stats.tracking_startup` | `/status`, `/api/ui/live` (`tracking_startup`) | `state` (`loading`, `ready`, `error`); with `error` also `code` and `message`: `restore_commit_unreadable`, `restore_install_failed`, `restore_install_pending` or `survey_recovery_failed`; see [known-limitations.md](known-limitations.md), *A survey restore stops every start-up* |
| `control_checks[].vertical_reference` | `/api/tracking` | `ground` or `phase_centre`; see [Asset points and control points](#asset-points-and-control-points) |

`/status`, `/health` and `/api/diagnostics` carry the `link` block as well, and
`/api/diagnostics` takes its `fix` and `current_quality` from the same function as
the live status (before the first GGA of a boot its `fix` therefore shows the
placeholders described above). `checks[]` is not provided; clients derive the
condition rows from `current_quality.reasons` and `limits`. The live snapshot is
built by one function and is the `live` frame of the [event stream](#event-stream).

### Event stream

`GET /api/events` carries the live snapshot to the browser without a request per
update. It needs a valid session like every read route (otherwise `401` with the
usual JSON refusal); `HEAD` answers the header only and takes no place in the
limits. The answer is HTTP/1.0 with `Connection: close` and `Content-Type:
text/event-stream; charset=utf-8`, `Cache-Control: no-store` and
`X-Content-Type-Options: nosniff`, followed by `retry: 2000` (the browser's
reconnect delay) and a comment line. Every event is
`event: <name>`, `data: <json>` and an empty line, without `id:`. There are only
three events:

| Event | Data | When |
|---|---|---|
| `live` | exactly the JSON of `GET /api/ui/live`, no extra fields | see the cadence below |
| `heartbeat` | `{"t": <uptime seconds>}` | the stream has written nothing for 10 s |
| `bye` | `{"reason": "session"}`, `"evicted"` or `"disabled"` | last event before the device closes the stream |

There is no progress event: an open measurement dialog still reads
`GET /api/tracking?view=progress` every 650 ms. The snapshot carries no
capability flag; a client detects a firmware without the stream by the failed
request (`404`) and keeps polling.

**How the field app uses it.** The field app opens one stream on the pages that
poll the live snapshot (Measure, Device and the other non-project pages) and
stops that poll while the stream carries data. The project pages keep their own
timers and open no stream. A `live` frame goes through the same intake as a poll
answer, so the stream has no freshness rule of its own: the age of the data is
still the arrival of a new fix object, and a `heartbeat` only keeps the
connection watch alive. The app falls back to polling when the request fails
with a final status (`401`, `404`, `503`, a wrong content type), after two
errors within 10 s, after 22 s without any event, and after `bye` with
`evicted` or `disabled`. It tries the stream again after 30 s, after 300 s
following `evicted` or `disabled`, so that four open tabs do not take the
stream from each other in turn. After `bye` with `session` it falls back to
polling in the same way and tries the stream again after 30 s; the next poll
answered `401` leads to the sign-in, also when a poll was already running at the
`bye`, and polling goes on when the sign-in is declined. A hidden tab, a lost
connection and the hold state of a portal restart close the stream; the app
opens it again through the polling start when the tab is visible, the
connection is back or the hold ends. A tab that loads hidden in the background
or offline opens no stream until it becomes visible or the connection is back
(a hidden tab does not poll either), so it holds none of the three active
streams.

One ordering rule decides between poll, stream and the app's own changes. Every
poll takes a serial number from `requestSerial` when it starts and is dropped
when a newer number was taken before its answer arrived. An accepted `live`
frame takes a number too, so it outdates any poll that started earlier, and a
frame from a stream that was closed since is dropped. After a change of its own
that the device has confirmed (a measurement method, a line point, an import),
the app takes a number, closes the stream and reopens the stream, then adopts
the confirmed state: the first frame of the new connection is built after the
change and cannot bring back older data. That costs one new connection per
confirmed change.

An open measurement dialog keeps its own `GET /api/tracking?view=progress` poll
every 650 ms and stays the only source of live data while it runs: the app
ignores `live` frames during a measurement, while the stream stays open and
its events still reset the 22 s watch.

**Cadence.** A hub task (`sse`) wakes every 200 ms and compares
`(fix_seq, fix_rev)`. A `live` frame is built once and shared by all streams
when a stream has just joined, when 1 s has passed since the last one, or when
the fix version changed and at least 400 ms have passed since the last one. A
stream therefore gets at least one `live` frame per second and at most 2.5 per
second. A stream keeps only the newest unwritten frame; a slow reader skips
frames instead of queueing them. The frame is at most 4608 bytes including its
lines.

**Limits.** At most 3 streams are active. A fourth one evicts the oldest, which
receives `bye` with `evicted`. At most 4 streams are open in total, counting an
evicted stream until its connection is closed; beyond that the answer is
`503 events_busy`. With `events.MAX_STREAMS = 0` the route answers
`503 events_disabled` and open streams receive `bye` with `disabled`. A stream
that is handed out does not count against the 6 HTTP connections, so at most
6 + 4 sockets are in use.

**Session.** The stream checks the portal session on its own timer, every 1 s,
whether data flows or not, and before every write. A valid check extends the
idle window of the session as each poll does. After a sign-out or expiry the
stream says `bye` with `session` within 1 s while the client reads, and in any
case ends within 5 s of the sign-out. All writes between two session checks,
the header, the frames and the final `bye` included, share one write budget of
5 s. A client that does not accept data within that budget loses only its own
stream. Rotating the NMEA stream token does not end event streams.

**Flush.** Python's `StreamWriter.write()` only buffers, and `wait_closed()`
discards unsent bytes, so every response of the route, the final `bye`
included, is drained within a deadline before the connection closes.

**Errors.** No error ends the hub or reaches the general error answer. An
exception while building the snapshot is counted in `sse_snapshot_errors`, logged
at most once per minute, and the tick continues; streams then carry heartbeats
only. An exception in the route or in a stream closes that connection without
further writes, and counts as `sse_write_timeouts` (write budget exceeded), as
nothing (client gone) or as `sse_stream_errors` (anything else, logged at most
once per minute).

**Counters** in `/status` `stats`: `http_connections_total` (every accepted TCP
connection, also those answered `503`), `sse_streams` (active), `sse_open`
(open, evicted included), `sse_evictions`, `sse_refusals` (`events_busy`),
`sse_write_timeouts`, `sse_stream_errors`, `sse_frames` (frames written),
`sse_frame_bytes_max`, `sse_tick_max_ms` (longest hub tick with work) and
`sse_snapshot_errors`.

**Emergency switch.** To close all streams and refuse new ones without a
restart, set `events.MAX_STREAMS = 0` from the REPL. The clients fall back to
polling.

### Tracking actions

`POST /api/tracking` takes a JSON object with an `action` field:

| Action | Effect |
|---|---|
| `create_project`, `rename_project`, `delete_project`, `select_project` | Project lifecycle; `delete_project` also removes the project's stakeout sets and answers `stakeout_cleanup` (see [Stakeout target sets](#stakeout-target-sets), *Project deletion*) |
| `start_line` | Start a line with `name`, `auto_distance_m`, `properties`, `samples`, and `recording_mode` |
| `add_vertex` | Measure and append one line vertex, averaging over `samples` |
| `add_asset` | Measure and store one asset point with `object_type`, `name`, `note`, and `link_to_active_line` |
| `pause`, `resume`, `finish` | Line state; `finish` also records a diagnostics snapshot |
| `undo` | Remove the last recorded feature; the optional `expect` object (`inventory_epoch`, `kind`, `feature_id`, `index`, `record_order`) names the candidate the caller saw. Without `expect` the old behaviour applies; with `expect` as `null` or a mismatch (including an `inventory_epoch` from before a restart or restore) the answer is `409 undo_candidate_changed` and nothing is removed |
| `cancel_measurement` | Stop a running averaging loop |
| `compact` | Checkpoint, verify, retire covered journal segments, rewrite the point store |
| `archive_project`, `verify_archive`, `reactivate_archive` | Archive lifecycle |
| `restore` | Replace the survey from a validated backup object |
| `frame_assignment_preview`, `assign_reference_frame`, `revoke_reference_frame` | Frame assignment for a project's RTK points without a frame, see [Frame assignment](#frame-assignment) |

The frame actions take `project_id`. `frame_assignment_preview` has no other
field. `assign_reference_frame` adds `reference_frame`, `reference_frame_epoch`
(optional), `expected_count` (integer) and `scope_token` from the preview.
`revoke_reference_frame` adds `assignment_token` from the status. Assignment and
revocation answer `project_id`, the new `frame_assignment` (`null` after a
revocation), the project's frame fields (`frames`, `mixed_frames`,
`unknown_frames`) and `tracking_live`. A malformed field answers `400`. State
refusals answer `409` with the code in `error`: `frame_scope_changed`,
`frame_assignment_changed`, `nothing_to_assign`, `no_frame_assignment`,
`recording_active` and, for an archive whose project changed while it was
exported, `frame_export_changed`. A request that is cancelled before the journal
append writes nothing; once the event is appended the assignment stands even if
the answer is lost, and the client reads the status again.

`GET /api/tracking` lists each project with `frame_assignment` (`reference_frame`,
`reference_frame_epoch`, `assigned_at`, `assigned_at_utc`, `count`, `token`) when
one is set, and `/api/ui/live` carries the same object (or `null`) as
`active_project.frame_assignment`, so other open devices update their frame
warning at once. The result of `undo` includes it as well when it changed.

A measurement refused by the acceptance gate carries `reasons`, the gate reason codes, in the error envelope next to the unchanged message. `start_line`, `add_vertex` and `add_asset` take an optional `request_id` (1 to 32 characters of `[A-Za-z0-9_-]`, otherwise 400). The answer mirrors it, and `undo_candidate.request_id` carries it until the next restart (RAM only). `undo_candidate.inventory_epoch` is 8 hex characters, held in RAM only; it is new after every restart and after every successful restore or archive reactivation, and all `request_id` marks are empty afterwards. A 500 or 503 after a recording action does not prove a rejection: the point may have been stored, so a caller checks `undo_candidate.request_id` before it retries. `GET /api/tracking?view=progress` carries `network`.

Exports, map reads, and line reads count as readers regardless of HTTP method. Maintenance waits for them, and while maintenance runs they answer `503 storage_busy`.

A mutating action is refused with `409` while storage maintenance is running,
with `507 storage_reserve` when the flash reserve would be violated, and with
`409 restore_install_pending` while a published restore waits for the next
start. Reads are refused with `503` while the survey is still loading after a
restart.

## Hardware

The tested hardware, the wiring table, and the pin assignment are in
[../README.md](../README.md). This section adds only what matters for the
receiver integration.

The firmware talks to the LC29H over UART 1 at 115 200 baud, reading on
GPIO 17 and writing on GPIO 18. RTCM corrections and `$PQTM` or `$PAIR`
commands share the transmit direction; NMEA and command replies share the
receive direction.

**Commands and corrections on one transmit line.** Every write to the receiver
UART goes through one writer, `UartTx` in `gnss.py`. It keeps a frame cursor
over the RTCM bytes the UART has actually accepted, following the length field
of each frame (a 3-byte header, a 10-bit length, payload and 3 CRC bytes); it
does not check the CRC, so after a bit error in a length field the boundary can
be wrong for up to one frame (at most 1029 bytes). A receiver command (the
automatic GST re-enable, `/gnss`, BLE) starts only when no frame is open, that
is, when no stream is running or the cursor stands between two frames. While a
frame is open the command waits in a queue of at most four; the writer splits
the running chunk at the frame end and writes the waiting commands between the
frames. A pause in reception in the middle of a frame is not a boundary: a
command that has not started after 5 s is dropped and never written into the
frame.

When the UART accepts only part of a correction chunk or command, the writer
writes the rest as soon as the UART takes it, before anything else, instead of
discarding it. Each write in progress has a progress deadline of 1 s (since the
last call that took at least one byte) and a total deadline of 2 s, both
checked before every further write attempt. A correction write that misses a
deadline ends the caster connection as a counted failed attempt
(`UartStalled`, error code `unexpected_error`): the rest of the chunk is
dropped and counted in `ntrip_flow.uart_dropped_bytes`, and the frame cursor
stays where it is until a real boundary or the end of the stream. A command
that misses a deadline is answered with an incomplete-write error. When a
stream ends for any reason (end of data, idle, stale, reload, error,
shutdown), the cursor returns to the idle state, so a waiting command is
released; the frame that was cut off stays incomplete on the wire. A single
blocking `uart.write` call into the driver is not limited by these deadlines.

`ntrip_flow` carries the writer's counters: `uart_writes` (correction chunks),
`uart_bytes` (accepted correction bytes), `uart_short_writes` (writes that
accepted less than offered; the rest is now completed), `uart_write_ms` and
`uart_write_max_ms` (latest and longest single write call),
`uart_dropped_bytes`, `uart_commands_sent`, `uart_commands_deferred` (commands
that had to wait), `uart_commands_dropped` and `uart_command_wait_max_ms`.

Only the sentence types in `NMEA_WHITELIST` — GGA, RMC, VTG, GSA, GSV, GST —
are forwarded to BLE and TCP. Replies to `$PQTM` and `$PAIR` commands travel
through the same stream, are recognised by their prefix, and are buffered for
`GET /gnss` instead of being fanned out. A sentence with a bad checksum is
counted and dropped; a whitelisted sentence is never "repaired".

**GST is the constraint that shapes the field workflow.** The acceptance gate
requires the receiver's own error estimate, and the LC29H does not keep the GST
output setting across a power cycle. `gnss.py` therefore re-sends
`$PQTMCFGMSGRATE,W,GST,1*0B` whenever no GST sentence has been seen for five
seconds, with an increasing delay between attempts of 2 s, 5 s, 15 s, and then
60 s. The attempt counter resets as soon as a GST sentence arrives. The
command waits for a frame boundary like any other receiver command;
`gst_enable_attempts` counts the write attempts on the UART, not the requests,
and the state reads `waiting` from the moment GST stops until the command has
been written. A request that is dropped after waiting 5 s changes neither the
state nor the counter.
`app.stats["gst_output_state"]` and `gst_enable_attempts` are published under
`gnss_accuracy` in the live endpoint, so a receiver that never starts emitting
GST is visible rather than silently blocking every measurement.

A GST sentence is attached to a fix only when its UTC field matches the GGA's,
so an estimate is never paired with the wrong epoch. Two paths attach it. When
the GST arrived before the GGA, the cached sentence is attached to the new fix
only if it is at most two seconds old (`gnss.py`). When the GST arrives after
the GGA, `state.attach_gst` attaches it as long as the UTC fields match and the
published fix is not stale, that is, for as long as it is younger than
`fix_stale_after_ms` (3 s by default). The two-second limit that applies there
is `GST_GRACE_MS`, the time a verdict waits for a missing GST before it is
applied without one; it is not a limit on attachment.

Writes to the BLE receive characteristic are accepted only from an *encrypted*
connection, and are forwarded to the receiver only while `ble_commands_enabled`
is set or a maintenance window is open. The firmware ships with
`ble_commands_enabled` off, which makes BLE a read-only transport by default.
The supported way to send a receiver command is the authenticated HTTP
endpoint; `POST /api/ble-maintenance` opens a 600-second window for the BLE
path when it is genuinely needed. The window runs on the device uptime, so
clock corrections do not shorten or extend it.

The ESP32-S3 has no Bluetooth Classic, so SPP cannot be added by configuration
or by a different MicroPython build. A stable supply matters more than it
looks: simultaneous Wi-Fi and BLE peaks on a weak supply present as unexplained
restarts.

## Development

The host test suite runs without hardware:

```sh
python3 -m unittest discover -s tests
```

Deploying to a board first builds `app.js` from `src/*.js` and the `.gz`
copies, then transfers only the files on the `push.py` allowlist, verifies
each one by SHA-256, and restarts the board:

```sh
python3 push.py . /dev/ttyACM0 --skip-unchanged
python3 push.py . --build-only           # build only, never opens a port
python3 push.py . --build-only --check   # write nothing; exit 1 if the committed build is stale
```

`--build-only` builds only a build project (a folder with `src/`, `assets.py`,
and `push.py`); any other folder, such as a backup with a stray `src/`, is
refused with exit code 2 and left untouched.

`app.js` is the concatenation of `src/NN-name.js` in file-name order inside one
IIFE (the first SPDX line of each fragment is dropped). File names must match
`NN-[a-z0-9-]+.js`, slots must be unique, and any other entry in `src/`
(except dotfiles) is a build error. The stakeout code takes slots 93 to 95 (95 is its core),
slot 97 is kept for the stakeout confirmation buttons, and `99-boot.js` always runs last. The build rejects
duplicate top-level names, but only declarations at the start of a line; a
declaration behind another statement on the same line or an indented one is
not detected. `app.js.gz`, `app.css.gz` and `i18n.js.gz` are regenerated from
their sources on every run, so the compressed copies cannot drift.

A build project (a folder with `src/`, `assets.py` and `push.py`) is always
pushed through the allowlist; other folders (backups, rollback trees) are
transferred byte for byte and never written to. Unknown options abort with
exit code 2. Files identical on the board are always skipped, and `/assets.py`
is written last and only after every earlier file checked out, so an
interrupted push leaves the old version and can be repeated.

The `state/` package is pushed completely or not at all. Before the first board
command, `push.py` aborts when some but not all files of a package in
`BOARD_FILES` exist locally. Files of a package are written first and
`state/__init__.py` last; the first package file that fails its check stops the
push, so the initializer and `/assets.py` are held back. After a complete push
of a build project, every file listed in `RETIRED_BOARD_FILES` (today `/state.py`,
replaced by `/state/__init__.py`) is removed from the board when its successor
was part of the push and matches the local checksum; a failed removal makes the
push exit with code 2. A single package file pushed on its own (for example
`push.py state/link.py`) keeps its folder on the board. The same rules apply
to the `tracking/` package (14 files, `tracking/__init__.py` last), which
replaces `/tracking.py`.

The board imports a folder `state/` before the old file `/state.py`. A push that is
interrupted between creating `/state/` and writing the complete package can
therefore leave a device that does not boot until the same push is run again
(see [known-limitations.md](known-limitations.md)); the device stays reachable
over USB. To roll back to a version from before the package, delete the `/state/`
folder and its files on the board by hand before pushing the older tree,
otherwise the folder wins over the older `/state.py`. The same holds for
`/tracking/` and the older `/tracking.py`: delete the `/tracking/` folder by
hand before pushing an older tree.

After any change to JavaScript or CSS, raise `ASSET_VERSION` in `assets.py`.
The browser caches those assets immutably under their version query, so a
device will otherwise keep serving the old files. When a checkout with `src/`
is pushed, `push.py` aborts before writing anything if an uncompressed browser
asset differs from the board while `ASSET_VERSION` equals the board's, or if
the local `ASSET_VERSION` cannot be read while assets changed; an unreadable or
missing board version lets the push through unchecked;
`--allow-stale-asset-version` overrides this for development pushes. Known
limits: the guard is a safety net against a forgotten bump, not against
deliberate bypass; single-file pushes and folders without `src/` are not
checked (`push.py` prints a note); a `.gz` that exists only on the board
(for example `qr.js.gz`) is never deleted and stays stale, because `push.py`
removes nothing.

The test suite writes some runtime files into the working directory; use a
temporary working copy for isolated runs.

Contribution rules, commit conventions, the review checklist, and the release
procedure are in [../CONTRIBUTING.md](../CONTRIBUTING.md). Known gaps and
things that are deliberately not solved are in
[known-limitations.md](known-limitations.md).
