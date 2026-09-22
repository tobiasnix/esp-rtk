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
| Firmware version string | `ESP-RTK V15.8.0` | `cfg.py` `CONFIG["version"]` |
| Configuration schema | 3 | `cfg.py` `CONFIG["config_schema_version"]` |
| Survey data schema | 4 (schemas 2 and 3 are still readable) | `tracking.py` `SCHEMA_VERSION`, `LEGACY_SCHEMA_VERSIONS` |
| Point-store schema | 3 | `pointstore.py` `POINT_STORE_SCHEMA` |
| Device identity schema | 3 | `identity.py` `IDENTITY_SCHEMA_VERSION` |
| Browser asset version | `15.8.0` | `assets.py` `ASSET_VERSION` |

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
| `cfg.py` | `CONFIG` defaults, constants, and the public configuration and logging API; the lowest layer, with no dependency on other application modules. |
| `cfg_validation.py` | Pure validation and presentation helpers: settings validation, Wi-Fi network lists, HTML escaping, subnet comparison. |
| `cfg_store.py` | Transactional persistence: atomic JSON writes, crash recovery, schema migration, pending and last-good staging. |
| `cfg_boot.py` | Boot health: exponential backoff, boot counter, planned-reset marker, reset-cause naming. |
| `cfg_factory.py` | Factory reset, BOOT-button hold evaluation, access-point password generation. |
| `cfg_logging.py` | RTC flight recorder and the wear-limited persistent error log. |
| `identity.py` | Stable device identity, device code, stream token, BLE passkey, and the names derived from the factory MAC. |
| `access.py` | Short-lived administrator sessions: login throttling, session and CSRF tokens, cookie parsing. |
| `state.py` | Shared runtime state: `app` statistics, the bounded `SimpleQueue`, task heartbeats, the `supervise()` restart wrapper, and `shutdown_event`. |
| `net.py` | Wi-Fi station and access point management plus the NTRIP client and the RTCM message counter. |
| `discovery.py` | Captive DNS during setup and a small mDNS responder for the portal and the NMEA service. |
| `gnss.py` | UART reader, NMEA checksum and whitelist filtering, GGA and GST parsing, GST re-enable retries, the route startup guard, and the status LED. |
| `fanout.py` | Distributes validated NMEA sentences into independent bounded BLE and TCP queues, and runs the NMEA TCP listeners including the optional `AUTH` handshake. |
| `ble.py` | Encrypted, bonded Nordic UART Service peripheral with passkey pairing and persisted bonds. |
| `esptel.py` | Builds the versioned `$PESPS` device-telemetry sentence and injects it into the NMEA fan-out. |
| `pointstore.py` | Generation-based segmented flash store for measured points, with compact encoding and a bounded expansion cache. |
| `tracking.py` | The survey domain: projects, lines, asset points, the acceptance gate, the event journal, checkpoints, archives, exports, and the flash reserve rules. |
| `parallel_benchmark.py` | Isolated synthetic tracking load used as a release gate; refuses to run while production survey data exists. |
| `field_diagnostics.py` | Optional bounded NDJSON field recorder with explicit UTC validity, boot and session IDs, and RTCM signal observations. |
| `web_routing.py` | Declarative transport policy per path: allowed methods, method-error text, and request body limit. |
| `web.py` | The HTTP server: request parsing, interface context, authentication, and every endpoint handler. |
| `ui.py` | The dependency-free portal markup, served from the device itself. |
| `assets.py` | The single cache-busting `ASSET_VERSION` substituted into served HTML. |
| `main.py` | Entry point: reset accounting, BOOT-button handling, safe mode, and the asyncio task set. |
| `app.js`, `app.css` | The field application (status, projects, device) served to the browser. |
| `app.js.gz`, `app.css.gz` | Pre-compressed copies of the above, rebuilt by `push.py` and served when the client accepts gzip. |
| `pages.js`, `pages.css` | Shared behaviour and styling for the support pages: sign-in, setup, diagnostics, label. |
| `base.css` | Base stylesheet loaded before `pages.css`. |
| `i18n.js` | Complete German and English dictionaries for the browser; machine-readable results stay English. |
| `qr.js` | Local QR code rendering for the Wi-Fi and portal codes on the label page. |

### Tasks

`main.py` starts one asyncio task per concern. All long-running tasks except
the shutdown handler are wrapped in `state.supervise()`, which logs a crash,
increments `task_restarts`, waits two seconds, and starts the coroutine again
from its factory.

| Task | Started as | Role |
|---|---|---|
| `field_diagnostics` | `recorder.run` | Periodic sampling and bounded flushing of the field recorder. |
| `watchdog` | `watchdog_task` | Feeds the hardware watchdog only while every watched task is alive. |
| `gc` | `gc_task` | Periodic `gc.collect()` on `gc_interval_ms`. |
| `led` | `StatusLED.run` | Status LED: fast blink without a fix, one-second blink with any other fix, steady on at RTK FIXED. |
| `network` | `NetworkManager.run` | Station and access-point lifecycle, reconnects, pending-configuration verdict. |
| `ntrip` | `NtripClient.run` | Caster connection, failover, backoff, RTCM to UART, periodic GGA upload. |
| `http` | `http_server_task` | The local portal and API on `http_port`. |
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
empty. If a watched task stalls, the feed stops and the hardware watchdog
resets the board within `wdt_timeout`, with the cause recorded in the
persistent error log.

Wi-Fi is created before BLE. Both radios allocate from the same ESP-IDF heap,
and creating BLE first left too little for the Wi-Fi driver.

### Safe mode

`bump_boot_count()` counts consecutive starts whose reset cause looks like a
crash. Power-on and soft resets clear the counter, and a caller that restarts
on purpose leaves a marker first, because `machine.reset()` reports as a hard
reset on the ESP32-S3 and is otherwise indistinguishable from a panic. After
`SAFE_MODE_BOOTS` (5) starts that never reached `SAFE_MODE_UPTIME_SEC` (300 s)
of uptime, the device starts in safe mode: only the field recorder, watchdog,
GC, network, HTTP, and discovery tasks run. NTRIP, BLE, GNSS, and survey
storage stay down, so the portal remains reachable for repair.

### Data flow

```text
LC29H --UART1--> GNSSHandler --checksum + whitelist--> nmea_queue (SimpleQueue)
                      |                                      |
                      | parsed GGA/GST                       | NmeaSenderTask.route()
                      v                                      v
                  app.last_fix                        ble_queue      tcp_queue
                      |                                   |              |
        tracker.enqueue_fix() (no flash)                  v              v
                      |                            BLE NUS notify   TCP broadcast
                      v
            tracker.run_worker() --> journal + point store on flash
                      |
                      v
              HTTP API (/api/tracking, /api/ui/live, exports)
```

Sentences reach the transports through bounded queues only. The shared input
queue holds `nmea_queue_size` sentences; each output queue holds 100. A slow
BLE client or a stalled socket therefore cannot block UART consumption or the
other transport — the affected queue overflows and counts its own drops in
`app.stats`. `$PESPS` telemetry is injected into the same input queue, so it
follows the identical path.

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
and the temporary file is renamed into place. A failure at the last step
restores the previous file. On load, `recover_atomic()` repairs a file whose
rename lost power in the middle by promoting whichever of `.tmp` or
`.previous` still parses as a JSON object. Without this, a brownout during a
plain `open(..., "w")` left a truncated file, `load_config()` silently fell
back to the defaults, and the device came up with no Wi-Fi and no NTRIP.

Values are coerced to the type of their default, because HTTP form data
arrives as strings and old configuration files stored ports as strings.

Loading also migrates: a file written under schema 1 that still uses port 2947
as the primary NMEA port is moved to 10110 with 2947 kept as the legacy
listener, and a file below schema 3 adopts the renamed access-point channel
key and is rewritten as schema 3.

### Keys

Only these keys can be set over HTTP (`SETTABLE_KEYS`):

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
| `ntrip_connect_timeout_sec` | `15` | TCP connect deadline |
| `ntrip_header_timeout_sec` | `10` | Deadline per response header line |
| `ntrip_read_timeout_sec` | `5` | Deadline for one read from the stream |
| `ntrip_idle_timeout_sec` | `15` | Silence after which the stream is reconnected |
| `ntrip_retry_delay_sec` | `5` | Base of the reconnect backoff |
| `ntrip_retry_max_sec` | `300` | Cap of the reconnect backoff |
| `ntrip_max_retries` | `0` | `0` means unlimited |
| `ntrip_gga_interval_sec` | `10` | Rover position upload interval; `0` disables it |
| `wdt_enabled`, `wdt_timeout` | `true`, `15000` | Hardware watchdog |
| `heartbeat_timeout_sec` | `60` | Per-task liveness deadline; `0` watches only the event loop |
| `gc_interval_ms` | `10000` | Garbage-collection interval |
| `fix_update_interval_ms` | `950` | Minimum interval between published fixes |
| `gsv_limit_per_sec` | `8` | Cap on forwarded GSV sentences |
| `telemetry_interval_sec` | `1` | `$PESPS` interval; `0` disables it |
| `log_level`, `log_mute` | `INFO`, `GC` | Severity filter and muted subsystem |
| `ntp_sync` | `true` | Without it, timestamps count from the year 2000 |
| `hostname` | `""` | Overwritten at boot from the identity |
| `status_token`, `config_token` | `""` | Obsolete; read from an old config and kept until a factory reset, never used for authorization |

The measurement acceptance keys are listed in
[Survey data](#survey-data), because they are validated and exposed as one
group.

### Pending and last-good configuration

A change that could cut the device off the network is never committed
immediately. `stage_config()` performs three atomic writes in order:

1. the currently persisted configuration to `config.last_good.json`,
2. the candidate to `config.pending.json`,
3. the candidate to `config.json`.

If any step fails, the files written so far are removed and the call reports
failure, so the device never boots from a half-applied change. A successful
staging records the result `pending` and the portal asks for a restart.

After the restart, `NetworkManager.run()` decides. If a pending configuration
exists and the station connects, the link must additionally survive ten further
one-second checks — a short association flap must not confirm wrong
credentials. Only then does `commit_pending_config()` delete both staging files
and record `connected` together with the SSID that worked. If the connection
fails or drops during those ten seconds, `rollback_pending_config()` restores
`config.last_good.json` into `config.json` and `CONFIG`, records `failed`, and
the network manager retries with the restored configuration.

The machine-readable outcome lives in `config.apply.result.json` and is served
by `GET /api/config-result`. It carries `schema_version`, `status`
(`none`, `pending`, `connected`, or `failed`), and `details`. Human-readable
text is deliberately not stored: presentation belongs to the browser's
translation dictionary.

### Configuration backup

`GET /api/config/backup` returns a JSON document of kind
`esp-rtk-config-backup` containing the settable keys, `wifi_networks`, and a
`credentials` block with the access-point password and the device code.
`POST /api/config/restore` validates a backup through exactly the same
validators as a normal write and then stages it like any other change.

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
each when several are configured, so a round takes at most about a minute.

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
the BOOT button requested it; `RECOVERY` means configured networks exist but
none could be joined, and the AP is up so the operator can fix it; `APPLYING`
is the short window between a staged change and the restart.

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
`/connecttest.txt`, `/redirect`) are redirected to `/setup` rather than
claiming that Internet access exists, and `/api/captive` answers the RFC 8908
shape. If the build supports it, the AP also advertises the portal through
DHCP option 114.

Routers and phone hotspots do not always forward mDNS. When `.local` does not
resolve, use the current DHCP address instead; it is not stable without a
reservation.

## NTRIP

### Endpoints

`_configured_endpoints()` returns an ordered list. It is empty when
`ntrip_enabled` is false, and an endpoint is included only when both its host
and its mountpoint are set. The primary endpoint uses the `ntrip_` keys, the
optional fallback uses the independent `ntrip_fallback_` keys with its own
host, port, mountpoint, user, and password.

The request is a plain HTTP/1.0 `GET /<mountpoint>` with
`User-Agent: NTRIP ESP32-S3/1.0`, `Connection: close`, and HTTP Basic
authorization. `Ntrip-Version: Ntrip/2.0` is added only when `ntrip_version`
is 2 or higher, because some casters require it and others reject it.

Logs, status, and diagnostics name only `primary` or `fallback` — never the
host, the mountpoint, the user, or the password.

### Status line handling

The first line from the caster is classified once:

| Response | Error code | Treated as |
|---|---|---|
| `ICY 200` or `HTTP/1.x 200` | — | stream accepted |
| `401` | `auth_error` | permanent |
| `404`, or a `SOURCETABLE` reply | `mount_error` | permanent |
| any other numeric status | `caster_error` | transient |
| unparsable first line | `protocol_error` | transient |

A permanent error does not heal by retrying. Once the round over all configured
endpoints is complete, it goes straight to the slowest interval
(`ntrip_retry_max_sec`) instead of hammering the caster every five seconds. Both
permanent errors are also the only two states published as
`app.stats["ntrip_state"]` and the only two values ever stored in
`app.stats["last_ntrip_error_code"]`: `caster_error` and `protocol_error` are
computed here but never recorded (see
[known limitations](known-limitations.md)). The endpoint sequence itself
appears as `disabled`, `waiting_wifi`, `connecting`, `streaming`, or `backoff`.

### Failover and backoff

With two endpoints configured, a failure moves to the other endpoint after
`ntrip_retry_delay_sec`; the exponential backoff applies only once a full round
over all configured endpoints has failed. `backoff_delay(attempt, base, cap)`
returns `base` for the first attempt and doubles from there, capped at
`ntrip_retry_max_sec`. Every wait is slept in slices of at most five seconds so
the task keeps producing heartbeats.

A stream that reached status 200 resets the endpoint index, so the next
reconnect starts at the primary caster again. This is a controlled failback: a
working fallback is never silently made permanent.

`ntrip_max_retries` is a per-endpoint cap; the effective limit is that value
times the number of configured endpoints. The default `0` means unlimited. When
a non-zero limit is reached, the task restarts and its retry counter resets, so
reconnection continues rather than stopping; the error code
`retries_exhausted` is never recorded (see
[known limitations](known-limitations.md)).

### Silent-stream detection

A caster that accepts the TCP connection and then says nothing is the failure
mode that a plain socket read cannot detect. Three bounded deadlines cover it:
`ntrip_connect_timeout_sec` around the connection setup,
`ntrip_header_timeout_sec` per header line, and `ntrip_read_timeout_sec` per
read from the stream. A read timeout is not an error by itself — the task
records it, emits a heartbeat, and keeps the GGA upload alive, because a VRS
caster may be waiting for the next rover position. Only when the silence
reaches `ntrip_idle_timeout_sec` is the connection torn down and rebuilt. A
clean EOF reconnects immediately.

### Rover position upload

While a stream is running, the last complete GGA sentence is written back to
the caster every `ntrip_gga_interval_sec` and once immediately after the
request, because VRS and nearest-mountpoint services deliver nothing without a
position. A sentence is sent only when it carries a fix: a position without one,
or a literal 0/0, would make the caster build a virtual station in the wrong
place.

### RTCM accounting

Incoming bytes are written to the receiver's UART first and counted afterwards,
so corrections reach the module without a detour. `RtcmCounter` parses RTCM3
framing and reports message types, totals, and the age of the last message.
MSM families are derived from the message number — 1071–1077 GPS, 1081–1087
GLONASS, 1091–1097 Galileo, 1101–1107 SBAS, 1111–1117 QZSS, 1121–1127 BeiDou —
and the offset inside each block of ten gives the family MSM1 to MSM7. The
summary is served by `GET /rtcm` and summarised in `$PESPS`. It describes what
was received, not what the receiver managed to use.

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
| Object types | `tap`, `valve`, `hydrant`, `branch`, `transition`, `control`, `tree`, `repair`, `line_start`, `line_end`, `other` |

`TARGET_MAX_FEATURES` (10000) is the value the compact schema is designed for;
`MAX_FEATURES` (5000) is what the firmware enforces. See
[performance-and-limits.md](performance-and-limits.md) for the measured
behaviour behind that distinction.

`GET /api/tracking` reports capacity as `used`, `limit`, `remaining`,
`percent`, and a `level` of `ok`, `notice`, `warning`, `critical`, or `full`,
together with the free flash, the required reserve, the estimated temporary
requirement, and the active and archived project counts. The thresholds are 80,
90, and 98 percent of the active limit, except at a limit of exactly 10000,
where the fixed values 8000, 9000, and 9800 are used.

### Coordinates and heights

Longitude and latitude are stored as WGS84 decimal degrees.
`altitude_msl_m` is the orthometric height above mean sea level as reported in
GGA field 9, according to the receiver's geoid model. `geoid_sep_m` is the
geoid separation from GGA field 11, and `altitude_ellipsoid_m` is their sum
when both fields are present. An altitude is never manufactured: a fix without
one is rejected rather than stored with a zero that would look like a
measurement in an export.

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
`PUT`, and reset to the factory values by `DELETE`. A `PUT` must supply all
eight fields.

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

Two rules apply only to averaged sets. Every sample must carry a GST error
estimate; a set with missing estimates fails with `not_all_samples_have_gst`.
Every sample must meet the required fix type — the stricter
`not_all_samples_rtk_fixed` is raised only when the configured profile actually
demands RTK FIXED, so lowering the profile genuinely lowers the requirement.

These factory values are starting points. They must be validated against known
control points for the actual antenna and mounting before the results are
trusted.

### Measurement modes

`averaged_current_fix(samples)` drives the measurement:

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

Every stored measurement — averaged or not — carries the recording timestamp,
fix quality and its text, satellite count, HDOP, correction age, reference
station ID, and the receiver accuracy reported by GST.

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

`GET /api/tracking?view=name_check&kind=point|line&name=...` reports whether a
name is still free within the active project, so the portal can warn before a
duplicate is recorded.

### Exports

`GET /api/tracking/export` serves four formats, all streamed so that a large
project never has to be materialised in RAM:

| `format` | Content type | Content |
|---|---|---|
| *(omitted)* | `application/geo+json` | GeoJSON `FeatureCollection` of lines and asset points, with the recording mode and the full measurement record per feature |
| `csv` | `text/csv` | One row per asset point, 24 columns from `id` through `note`, including chainage, offset, side, fix status, satellites, HDOP, correction age, the averaging statistics, and the GST sigmas |
| `backup` | `application/x-ndjson` | Complete NDJSON survey backup with a header record, one record per project, line, vertex, and asset point, and a SHA-256 integrity record |
| `archive` | `application/x-ndjson` | One previously archived project, identified by `archive_id` |

Fields that are internally stored in compact form are fully expanded in every
export. CSV values starting with `=`, `+`, `-`, or `@` are prefixed with an
apostrophe so that a spreadsheet does not interpret them as formulas.

`POST /api/tracking/import` takes an NDJSON survey backup with
`Content-Type: application/x-ndjson`, up to 8 MiB. It is staged and verified
before anything active is touched; see [Storage](#storage).

## Storage

### Files

All survey state lives in the filesystem root, prefixed with `tracking`:

| Path | Role |
|---|---|
| `tracking.index.json` | Atomically written pointer record: global sequence, checkpoint order, active segment, active project and line, feature count, point-store high-water mark and size |
| `tracking.checkpoint.json` | Line-oriented metadata checkpoint: header with order, segment, byte offset, and quality profiles; one record per project, line, and asset point; a footer with the active selection |
| `tracking.segment-NNNNNN.jsonl` | Append-only event journal, one segment at a time, at most 128 KiB |
| `tracking.points-v1.jsonl.g<generation>-<n>.pjs` | Point-store segments, at most 128 KiB each |
| `tracking.points-v1.jsonl.segments.json` | Point-store manifest naming exactly one validated generation |
| `tracking.archives/index.json` | Archive index (metadata only) |
| `tracking.archives/<archive_id>.ndjson` | One archived project, SHA-256 verified |

The index, the checkpoint, the point-store manifest, and the archive index are
written through the same `.tmp` / `.previous` sequence used for the
configuration, and each is parsed back and checked for its expected `kind` and
schema version before the rename is completed. The journal segments and the
point-store segments are append-only and are never rewritten in place.

### The point store is the point journal

Since schema 4 the point store is both the compact storage format and the
canonical append-only journal for measured points. Full `vertex_added` copies
are no longer duplicated into the event journal. The event journal keeps every
other event: project, line, and state events, `asset_added`,
`feature_removed`, and the rare `profile_registered` event.

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

`compact()` writes a checkpoint, writes the index, then opens an independent
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
that chain finished.

### Recovery after a power failure

Start-up does not replay everything. `_load_steps()`:

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
after the whole payload has passed. Backups written under schema 2 or 3 are
converted losslessly into schema 4 during staging. A schema-4 backup must also
match its SHA-256 integrity record and its declared record count.

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
| Concurrent connections | 6, further connections get `503` with `Retry-After: 2` |
| Header deadline | 10 s |
| Body deadline | 10 s |
| Default body limit | 4096 bytes |
| Body limit for `/api/tracking` | 262 144 bytes |
| Body limit for `/api/tracking/import` | 8 MiB |

Responses are HTTP/1.0 with `Connection: close`, so every request uses its own
connection. Each response carries `X-Content-Type-Options: nosniff`,
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

The `status_token` and `config_token` keys are obsolete. They are still read
from an old `config.json` and persist across a firmware update - only a
factory reset clears them - but no endpoint authorizes on them.

In the table below, **session** means a valid `rtk_session` cookie, and
**session + CSRF** additionally means a matching CSRF token and an acceptable
`Origin`.

### Endpoints

| Method | Path | Authentication | Purpose |
|---|---|---|---|
| GET | `/` | session | Field application (status, projects, device); returns the sign-in page with `401` otherwise |
| GET | `/ui` | none | Redirect to `/` |
| GET | `/ui-v2` | session | Same application as `/`, kept as a stable alias |
| GET | `/setup` | session | Configuration form for Wi-Fi and NTRIP |
| GET | `/label` | session | Device label page with the Wi-Fi and portal QR codes |
| GET | `/api/label` | session | Label data as JSON: names, device code, stream token, BLE passkey, QR payloads |
| GET | `/api/access` | none | Connection contract: hostname, ports, NMEA TCP authentication mode, BLE service and characteristic UUIDs |
| GET | `/api/captive` | none | RFC 8908 captive-portal status and portal URL |
| GET | `/api/ui/live` | session | Compact live snapshot used by the field loop: current fix, quality verdict, tracking state, network, NTRIP, diagnostics |
| GET | `/status` | session | Full status: services, counters, memory, network, identity |
| GET | `/health` | session | Alias of `/status`; one handler serves both |
| GET | `/rtcm` | session | RTCM message statistics and MSM families |
| GET | `/errors` | session | Plain-text RTC flight recorder plus the persistent error log |
| GET | `/api/log` | session | Incremental error-log entries; `cursor`, `limit` (max 200), `level` |
| GET | `/diagnostics` | session | Diagnostics page |
| GET | `/api/diagnostics` | session | Diagnostics snapshot; `download=1` sends it as an attachment |
| GET | `/api/wifi-scan` | session | Visible networks; refuses with `409` while a recording is active |
| GET, POST | `/api/field-diagnostics` | session (+ CSRF on POST) | Field recorder status; `start`, `stop`, and `mark` actions |
| GET | `/api/field-diagnostics/export` | session | Streams the recorded NDJSON; refuses while the recorder runs |
| GET | `/api/config` | session | Current configuration with passwords replaced by `*_set` booleans |
| PUT | `/api/config` | session + CSRF | Validate and stage a configuration change |
| POST | `/api/config/apply` | session + CSRF | Restart so the staged change can prove itself |
| POST | `/api/config/restore` | session + CSRF | Validate and stage a configuration backup |
| GET | `/api/config/backup` | session | Download the configuration backup, credentials included |
| GET | `/api/config-result` | session | Outcome of the last configuration attempt |
| POST | `/save` | session + CSRF | Legacy HTML form path for the same staging |
| GET | `/api/session` | none | Reports whether the caller is signed in and returns the CSRF token |
| POST | `/api/session` | device code | Sign in and set the session cookie |
| DELETE | `/api/session` | session + CSRF | Sign out and clear the cookie |
| GET | `/api/measurement-gate` | session | Current and factory acceptance thresholds |
| PUT | `/api/measurement-gate` | session + CSRF | Replace all eight thresholds |
| DELETE | `/api/measurement-gate` | session + CSRF | Reset them to the factory values |
| GET | `/api/tcp-auth` | session | Whether NMEA TCP clients must authenticate |
| PUT | `/api/tcp-auth` | session + CSRF | Enable or disable that requirement |
| POST | `/api/stream-token/rotate` | session + CSRF | Issue a new stream token and drop all TCP clients |
| POST | `/api/ble-maintenance` | session + CSRF | Allow BLE writes to reach the receiver for 600 s |
| POST | `/api/reboot` | session + CSRF | Deferred soft restart |
| GET | `/gnss` | session | Buffered replies to receiver commands |
| POST | `/gnss` | session + CSRF | Send one validated `$PQTM` or `$PAIR` command as text |
| GET, POST | `/api/gnss-command` | session (+ CSRF on POST) | The same, with a JSON body `{"sentence": "..."}` |
| GET | `/api/tracking` | session | Survey status, capacity, current fix and quality; `view=progress` and `view=name_check` |
| POST | `/api/tracking` | session + CSRF | All survey actions (see below) |
| GET | `/api/tracking/map` | session | Bounded map geometry; `limit` (max 500), `project_id`, `since_revision` |
| GET | `/api/tracking/line` | session | Paged line vertices with their quality details; `line_id`, `offset`, `limit` |
| GET, POST | `/api/tracking/export` | session (+ CSRF on POST) | GeoJSON, CSV, backup, or archive export |
| POST | `/api/tracking/import` | session + CSRF | Upload an NDJSON survey backup |
| GET, POST, DELETE | `/api/parallel-benchmark` | session (+ CSRF on POST and DELETE) | Arm, stop, and inspect the isolated load test |
| GET | `/api/parallel-benchmark/export` | session | Export the benchmark result |
| GET | `/base.css`, `/pages.css`, `/pages.js`, `/i18n.js`, `/app.css`, `/app.js`, `/qr.js` | none | Immutable versioned browser assets; `app.css` and `app.js` are also served pre-compressed |
| GET | `/generate_204`, `/gen_204`, `/hotspot-detect.html`, `/library/test/success.html`, `/ncsi.txt`, `/connecttest.txt`, `/redirect` | none | Captive-portal probes, redirected to `/setup` |

`HEAD` is accepted on every path whose table row starts with `GET` alone, and
additionally on `/api/tracking`, `/api/tracking/export`, `/api/tracking/line`,
and `/api/tracking/map`. It answers `405` on `/api/config`, `/api/session`,
`/api/measurement-gate`, `/api/tcp-auth`, `/api/field-diagnostics`,
`/api/gnss-command`, `/api/parallel-benchmark`, `/gnss`, and every
`POST`-only path.

The `401` response on `/rtcm` still advertises `?token=...` access; no
endpoint authorizes by token today, and the ordinary session rule above
applies to `/rtcm` as well (see [known limitations](known-limitations.md)).

### Tracking actions

`POST /api/tracking` takes a JSON object with an `action` field:

| Action | Effect |
|---|---|
| `create_project`, `rename_project`, `delete_project`, `select_project` | Project lifecycle |
| `start_line` | Start a line with `name`, `auto_distance_m`, `properties`, `samples`, and `recording_mode` |
| `add_vertex` | Measure and append one line vertex, averaging over `samples` |
| `add_asset` | Measure and store one asset point with `object_type`, `name`, `note`, and `link_to_active_line` |
| `pause`, `resume`, `finish` | Line state; `finish` also records a diagnostics snapshot |
| `undo` | Remove the last recorded feature |
| `cancel_measurement` | Stop a running averaging loop |
| `compact` | Checkpoint, verify, retire covered journal segments, rewrite the point store |
| `archive_project`, `verify_archive`, `reactivate_archive` | Archive lifecycle |
| `restore` | Replace the survey from a validated backup object |

A mutating action is refused with `409` while storage maintenance is running,
and with `507` when the flash reserve would be violated. Reads are refused with
`503` while the survey is still loading after a restart.

## Hardware

The tested hardware, the wiring table, and the pin assignment are in
[../README.md](../README.md). This section adds only what matters for the
receiver integration.

The firmware talks to the LC29H over UART 1 at 115 200 baud, reading on
GPIO 17 and writing on GPIO 18. RTCM corrections and `$PQTM` or `$PAIR`
commands share the transmit direction; NMEA and command replies share the
receive direction.

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
60 s. The attempt counter resets as soon as a GST sentence arrives.
`app.stats["gst_output_state"]` and `gst_enable_attempts` are published under
`gnss_accuracy` in the live endpoint, so a receiver that never starts emitting
GST is visible rather than silently blocking every measurement.

A GST sentence is attached to a fix only when its UTC field matches the GGA's
and it arrived within two seconds, so an estimate is never paired with the
wrong epoch.

Writes to the BLE receive characteristic are accepted only from an *encrypted*
connection, and are forwarded to the receiver only while `ble_commands_enabled`
is set or a maintenance window is open. The firmware ships with
`ble_commands_enabled` off, which makes BLE a read-only transport by default.
The supported way to send a receiver command is the authenticated HTTP
endpoint; `POST /api/ble-maintenance` opens a 600-second window for the BLE
path when it is genuinely needed.

The ESP32-S3 has no Bluetooth Classic, so SPP cannot be added by configuration
or by a different MicroPython build. A stable supply matters more than it
looks: simultaneous Wi-Fi and BLE peaks on a weak supply present as unexplained
restarts.

## Development

The host test suite runs without hardware:

```sh
python3 -m unittest discover -s tests
```

Deploying to a board transfers only the files on the `push.py` allowlist,
verifies each one by SHA-256, and restarts the board:

```sh
python3 push.py . /dev/ttyACM0 --skip-unchanged
```

After any change to JavaScript or CSS, raise `ASSET_VERSION` in `assets.py`.
The browser caches those assets immutably under their version query, so a
device will otherwise keep serving the old files. `push.py` regenerates
`app.js.gz` and `app.css.gz` from their sources on every run, so the compressed
copies cannot drift.

The test suite writes some runtime files into the working directory; use a
temporary working copy for isolated runs.

Contribution rules, commit conventions, the review checklist, and the release
procedure are in [../CONTRIBUTING.md](../CONTRIBUTING.md). Known gaps and
things that are deliberately not solved are in
[known-limitations.md](known-limitations.md).
