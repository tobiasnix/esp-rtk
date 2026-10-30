# Performance and limits

This document collects two different kinds of number, and it is worth keeping
them apart.

**Enforced limits** are constants in the firmware. They are exact, they are
checked on every relevant code path, and they do not depend on the hardware a
device happens to run on. Every one of them below was read out of the source.

**Measured timings** are orders of magnitude observed on the tested hardware —
an ESP32-S3 at 240 MHz with 16 MiB of flash and PSRAM, driving an LC29H(DA)
receiver, with Wi-Fi, NTRIP, BLE, the NMEA fan-out and the portal all running.
They are rounded, they come from a small number of runs, and they are not
guarantees. A slower link, a fuller filesystem or a busier device will change
them. They are here because "it takes a while" is not useful to anyone
planning a field session.

A third kind is labelled **host-emulated**: a size or count computed by running
the firmware's own code on a development computer instead of on the board.
It is exact for the code path, but it says nothing about the board's timing or
about anything the host runs differently. Every such number below says so.

Where a constant is already explained in depth elsewhere, this document gives
the number and links rather than repeating the mechanism.

## Point capacity

| Limit | Value | Constant |
|---|---|---|
| Active features per device (line vertices plus asset points) | 5000 | `tracking/common.py` `MAX_FEATURES` |
| Design target of the compact schema | 10000 | `tracking/common.py` `TARGET_MAX_FEATURES` |
| Projects | 20 | `tracking/common.py` `MAX_PROJECTS` |
| Lines per project | 500 | `tracking/common.py` `MAX_LINES_PER_PROJECT` |
| Custom properties per project or line | 32 | `tracking/common.py` `MAX_PROPERTIES` |
| Uploaded backup, HTTP request body | 8 MiB | `web_routing.py` `TRACKING_IMPORT_LIMIT` (`POST /api/tracking/import`) |
| Backup parser, serialised payload | 32 MiB | `tracking/common.py` `MAX_BACKUP_BYTES` |

`GET /api/tracking` reports `used`, `limit`, `remaining`, `percent` and a
`level`. The level is derived from the active limit: `notice` at 80 %,
`warning` at 90 %, `critical` at 98 % — each rounded up — and `full` once the
limit itself is reached. At a limit of exactly 10000 the fixed values 8000,
9000 and 9800 are used instead (`tracking/summary.py` `capacity()`). Below the notice
threshold the level is `ok`.

`full` is not a warning, it is a stop. Creating a line vertex or an asset point
raises "Tracking point limit reached", and an automatic recording is paused
rather than failing silently, so the line stays recoverable. The same check
runs before a restore, before reactivating an archive and before importing a
backup, so none of those paths can push a device past the limit.

**Size in bytes per point (host-emulated).** The sizes below were measured
against the V15.9.0 code by writing a line vertex on a development
host and counting the bytes as written. The host's JSON writer separates with
`", "` and `": "`, the same layout that a stock MicroPython 1.28 build produced
when it was checked; no row size has been measured on the board itself, so
treat them as close estimates. With 24-character frame and mount texts a
point-store vertex record takes about 350 bytes for a single-sample point and
468 bytes for an averaged one, and a compact archive row (including its line
terminator) 402 and 520 bytes. With frame, epoch and mount texts at 80 characters each, the longest that a
stored measurement accepts (the field-setup endpoint allows at most 40 and 16
characters for frame and epoch, and the configuration at most 64 for the
mountpoint, so this is an upper bound), an averaged archive row reaches about
710 bytes. At
the 5000-feature limit that is roughly 1.7 to 2.3 MB of point store for line
vertices (5000 times 350 to 468 bytes). The archive reserve estimate assumes
768 bytes per vertex (`tracking/common.py` `ARCHIVE_VERTEX_BYTES`), plus 32 KiB and the
25 % file-system margin. Asset points are recorded as `asset_added` events in the event journal and
kept in the checkpoint, not in the point store, and their row size under the
current schema has not been measured.

Archived projects do not count against it. The archive index holds metadata
only and start-up loads zero archived points, which is what makes archiving the
practical answer to a full device.

**Why the enforced limit is half the design target.** The compact schema, the
capacity thresholds and the file formats were all built for 10000 active
points. Isolated synthetic runs over the USB REPL have carried 10000 — and
experimentally several times that — through a full write, checkpoint, restart
and export cycle, and a run at a still higher target stopped in a controlled
way at the flash reserve rather than corrupting anything. What has *not* been
completed is
the acceptance run that matters: the same point count written at one point per
second while GNSS, NTRIP, Wi-Fi, BLE, the NMEA TCP fan-out and the portal are
all genuinely in use, followed by an export, a restart and a second export that
must match. Until such a run passes, the shipped limit stays 5000. Raising a
constant is not the same thing as having evidence, and this is the one place in
the firmware where the two are deliberately kept apart.

## Flash reserve

Writing survey data is refused while less than 20 % of the filesystem is free
(`tracking/common.py` `MIN_FLASH_RESERVE_PERCENT`), and the check is made against the
free space *minus* 278 528 bytes of operational headroom
(`OPERATIONAL_FLASH_HEADROOM_BYTES`) plus whatever the caller estimates it
needs. The mechanism, including the write-estimate cache, is described in
[technical.md](technical.md), *Flash reserve*.

Two operational consequences are worth stating here:

- An automatic recording that runs into the reserve is **paused** with the
  reason `storage_reserve`, not aborted. Free space — archive or delete a
  project, or remove field-diagnostic recordings — and resume the line.
- The reserve is global. The field-diagnostics recorder holds at most 4 MiB of
  its own (32 segments of 128 KiB) and honours the same 20 %, so a long
  recording session shortens both its own retention and the headroom available
  for survey writes. See [field-diagnostics.md](field-diagnostics.md).

The filesystem query itself is not free — on the tested hardware a single
`statvfs` call costs a few hundred milliseconds, which is why the point path
samples it at most every 128 writes (`tracking/common.py` `FLASH_RECHECK_WRITES`) and
estimates in between. Anything that changes the file layout — a line start, a
segment rotation, a checkpoint, an archive operation, a store rewrite — forces
a fresh measurement instead of trusting the estimate.

## Typical timings

All figures below are rounded orders of magnitude from runs on the tested
hardware, under the conditions named with them.

### Start-up

Times are software uptime from the boot log, measured from the start of the
firmware rather than from the moment power is applied. The order of the
milestones is fixed by the code and described in
[boot-sequence.md](boot-sequence.md); only the durations are measurements.

| Milestone | Typical |
|---|---|
| GNSS UART initialised | about 12 s |
| HTTP listener accepting connections | about 18 s |
| Wi-Fi station connected with an IP address | about 22 s |
| BLE advertising the web address | about 22 s |
| NTRIP stream connected (status 200) | about 23 s |
| Survey storage ready | tens of seconds more |

The last row is the one that varies. Recovery reads the point store,
replays the journal and recomputes stale line summaries, so it scales with the
stored point count: for a few thousand active points expect roughly 40 to 60 s
from reset, and rather longer as the store grows towards the limit. The portal,
the live fix, BLE and the correction stream are all available during that
window; only survey actions are held back, which is the entire point of the
cooperative recovery.

A device switched on in the field therefore shows its live fix in the portal
within about half a minute and accepts survey actions after roughly a minute.
That is firmware readiness only: acquiring satellites, receiving corrections and
converging to RTK FIXED are the receiver's business and take as long as the sky
view and the correction link allow.

### Recording and measurement

- **Route start-up guard.** After the GNSS handler starts, automatic route
  vertices are refused until the receiver has produced 10 s of continuous
  usable positions (`gnss.py` `ROUTE_SETTLE_MS`). In practice the release comes
  a little over ten seconds after the first usable fix, with the first stored
  vertex a few seconds after that. See [technical.md](technical.md),
  *Recording modes*.
- **GST recovery.** The receiver does not keep its GST setting across a power
  cycle, and the acceptance gate needs it. When GST stops arriving the firmware
  re-requests it (`gnss.py` `GST_ENABLE_COMMAND`, retry delays 2, 5, 15 and
  60 s); a receiver that is answering normally is back to `receiving` within a
  few seconds of a single request.

- **NMEA hot path.** Every GGA sentence is parsed (up to 5 Hz); every published
  fix (at most about 1 Hz, `fix_update_interval_ms` = 950) is judged by the
  acceptance gate. The cost of one epoch, a GGA with its GST enrichment, is
  `K = t(parse_gga) + n * t(quality_check)`, where `n` is the number of gate
  judgements per epoch including queueing and the worker's store. Measured
  on the host with the real `AppState` and `Tracker`, `n` was 5 on the surveying
  line and 3 on the route line; it is now 2 on both, one judgement per
  published fix version. A GST that completes the same fix keeps the verdict
  for the stored point, a different GGA version of the same UTC time does not.
  The gate limits are cached instead of rebuilt per check, the GGA coordinate
  and height parsers create far fewer temporary strings, and the line scanner
  no longer copies the rest of the receive buffer for every sentence. Host
  factors (CPython, loaded machine, three-run median): `quality_check`
  about 1.25 times faster, `K` for the surveying line 33 to 20 microseconds
  and for the route line 25 to 20 microseconds. Host times are not an
  acceptance measure; the device values are in the validation record
  (steps 28c.1 and 36.1) and are pending at the time of writing.
- **Queues.** Bounded `SimpleQueue` instances (the NMEA queue holds 512 items,
  the fan-out queues 100) are backed by a `deque`; an unbounded one stays
  list-based. Small bounded lists such as replies, errors, map changes and
  pending fixes keep `pop(0)` by design, because their length is at most a few
  dozen items and a `deque` would not pay for itself there.

### Corrections and reconnection

- NTRIP throughput for a multi-constellation MSM5 stream runs at roughly
  1 to 1.5 kB/s.
- After a short Wi-Fi drop with the network still in range, corrections
  typically resume within about ten seconds.
- After losing the access point entirely and failing over to another stored
  network, corrections have resumed about twenty seconds after the loss was
  detected.
- A caster that accepts the connection and then goes silent is detected after
  `ntrip_idle_timeout_sec` (15 s); an isolated test against a deliberately
  silent server had data flowing again about 20 s after the stall began,
  including the reconnect.

### Correction ages and link latency

The firmware distinguishes three ages: the arrival age, the epoch age and the
receiver's correction age. They are defined once, in
[technical.md](technical.md#correction-ages), and are not repeated here. What
matters for planning a session is that the arrival age proves only that bytes
reach the device, while the epoch age says how old the content is, and the two
can differ by minutes.

Orders of magnitude from the recorded sessions:

| Session | Firmware | Observation |
|---|---|---|
| Field recording | V15.7.3 | The epoch age, reconstructed offline from the MSM epochs in the field logs, had a median of 55 s, a 90th percentile of 205 s and a maximum of 407 s. The receiver's correction age was that latency plus about 4 s. The device's only age then, the arrival age, showed the stream as fresh throughout. The delay was in the network path or at the caster, not in the device; the logs cannot say which |
| Field recording | V15.7.4 | The receiver's correction age had a median of 2 s and a maximum of 77 s. 42 of the 192 samples that reported RTK FIXED had a correction age above 5 s |
| Field recording | V15.7.3 | The receiver kept reporting GGA quality 4 for up to 120 s of correction age, while the GST horizontal sigma jumped from about 0.01 m to about 0.8 m from a correction age of roughly 20 to 26 s |

Two rules follow. The fix flag alone does not tell you the corrections are
current, which is why the acceptance gate combines the GST estimate with the
correction age in GGA field 13 (`corrections_too_old`, default 5 s; see
[technical.md](technical.md), *Acceptance gate*). And a healthy link looks like
an epoch age of a couple of seconds; anything that keeps growing means the
stream is delayed even though bytes arrive.

Since V15.9.0 the device measures the epoch age itself. The **Correction link**
line of the Corrections row of the measurement checklist shows it together with
the arrival age: green
below 5 s, amber from 5 s up to `ntrip_max_epoch_age_sec` (15 s by default) or
while no epoch has been decoded yet (invalid clock, or a stream without MSM
messages), red when the device reports `rtcm.stale`. When NTRIP has no endpoint to use,
the line says that NTRIP is currently not delivering corrections and names the effective cause (switched off, not set up, waiting for Wi-Fi, connection error with the last error code, or stopped after repeated failures); it stays neutral as long as the NTRIP client has not run. The same value is recorded in the field log; see
[field-diagnostics.md](field-diagnostics.md).

Advice, derived from those sessions rather than guaranteed:

- **Hotspot.** When the Correction link line turns amber or red while the
  arrival age stays small, the delay is upstream of the Wi-Fi link: in the
  phone's mobile data connection or at the caster. Move the phone to a spot with
  better mobile reception, or try another hotspot device or mobile network.
- **Mountpoint.** Prefer a mountpoint whose correction stream you have seen
  stay fresh; if one mountpoint of the service is repeatedly late, try another
  of the same service. A mountpoint that carries L5, E5a and B2a signals is also
  the better choice for the receiver's signals; see
  [known-limitations.md](known-limitations.md), *The receiver and the base do
  not share all signals*.
- **Backlog.** Setting `ntrip_reconnect_on_stale_sec` (default `0`, off) makes
  the device reconnect once the stream has been stale for that long; a fresh
  connection is intended to drop whatever the caster had queued. It is a
  configuration-file key; see [technical.md](technical.md), *Keys*.

### Distance to the base station

RTK accuracy degrades with the distance between the rover and the base
station: as a general rule of RTK positioning (not a measurement of this
device), allow about 1 ppm per km, that is 1 mm for every kilometre of
baseline on top of the accuracy at zero distance, so about 1 cm more at
10 km. The firmware neither knows nor checks the baseline length. With a
physical base station, find out its distance; with a network mountpoint that
generates a virtual station near the rover, the rover's position is what the
periodic GGA upload is for (`ntrip_gga_interval_sec`, 10 s by default).

### The portal

Measured over the device's own Wi-Fi, with GNSS and NTRIP running:

| Request | Typical |
|---|---|
| Project map, up to 500 preview vertices per line | a few seconds |
| Line detail page, 25 vertices | well under a second |
| Project overview, warm status cache | a fraction of a second |
| Project overview, first call after a restart | a few seconds |

The map request is the expensive one: it reads the selected vertices out of the
flash point store, and it yields to the event loop between steps so that GNSS
and NTRIP keep running while it does. The browser allows 20 s per project read
(`app.js` `PROJECT_READ_TIMEOUT`); the stored geometry is rendered as soon as it
arrives and the live overlay follows, so a slow live request no longer discards
a map that has already loaded.

Opening a large project is a multi-second operation. That is the honest
expectation to set for a field user; it is not a fault to be reported.

### Maintenance

Archiving and compaction run tens of seconds on a device with a few thousand
active points; reactivating an archived project of that size back into the
active store is a minutes-scale operation. They run cooperatively, yielding
between records, so GNSS, NTRIP and the portal continue during them — but
individual flash and manifest steps still block for a couple of seconds at a
time, and the watchdog budget is what limits how long any one of them may be.
Plan maintenance for a moment when nothing is being recorded — the firmware
refuses archiving, reactivation, archive verification, compaction and restore
with `409 recording_active` while a line is active or a measurement is
running (`web.py`; the tracker's maintenance guard re-checks only the open line and fixes still being recorded), and answers concurrent survey reads and writes
`storage_busy` while maintenance is in progress.

## Concurrent clients and connections

| Transport | Limit | Defined in |
|---|---|---|
| HTTP connections in parallel | 6, then `503` with `Retry-After: 2` | `web.py` `HTTP_MAX_CONNECTIONS` |
| Event streams (`GET /api/events`) | 3 active, 4 open including evicted ones still closing; outside the 6 HTTP connections | `events.py` `MAX_STREAMS`, `MAX_OPEN_STREAMS` |
| HTTP header deadline | 10 s | `web.py` `HTTP_HEADER_DEADLINE_SEC` |
| HTTP body deadline | 10 s | `web.py` `HTTP_BODY_DEADLINE_SEC` |
| NMEA TCP clients | 4 | `cfg.py` `nmea_tcp_max_clients` |
| Queued NMEA sentences | 512 | `cfg.py` `nmea_queue_size` |

Responses are **HTTP/1.0 with `Connection: close`**, except the event stream. Every other request therefore
costs a fresh TCP connection — there is no keep-alive to amortise the setup
over. A client that issues many small requests pays that price every time, and
a client that opens six connections at once will see the seventh rejected with
`503` rather than queued. Poll serially; respect `Retry-After`.

**Connection setup can stall for seconds.** On a device that is otherwise
healthy, a TCP connection is normally established in a few milliseconds, but
individual connections have repeatedly taken several seconds before the HTTP
request could even be sent, with SYN retransmissions visible on the client
side. It has been reproduced both from a browser and from a plain socket
client, so it is not a browser artefact; the cause in the network path is not
established, and it is listed as an open item in
[known-limitations.md](known-limitations.md). The device reports its event-loop
lag, so a slow connection with no lag at the same time argues against a long
synchronous block of the event loop; other causes remain open.

The practical consequence for integrators: **a client timeout must cover
connection setup, not just the response body.** A budget of a few seconds that
assumes an instant connect will fail on a device that is answering perfectly
well. The portal itself allows 20 s per project read for this reason.

**One stream per browser tab replaces the 2 s poll.** A tab that keeps
`GET /api/events` open receives the live snapshot at least once per second over
one connection, so it no longer opens a new TCP connection for every poll. The
stream is outside the limit of 6 HTTP connections; instead at most 3 streams are
active and 4 open (an evicted one counts until it is closed). A fourth tab
replaces the oldest stream, and that tab polls again. A stream whose client
stops reading is dropped after 5 s. An open measurement still polls its progress
every 650 ms. `stats.http_connections_total` in `/status` counts every accepted
TCP connection and shows the effect; the `sse_*` counters describe the streams
(see [technical.md](technical.md), *Event stream*).

NMEA TCP connections that are still authenticating share `nmea_tcp_max_clients`
with connected clients and are closed after at most ten seconds, which is a
second reason to keep the client count low on a shared network. Every TCP
client has its own queue of at most 100 entries or 8 KB, so the memory bound is
`nmea_tcp_max_clients` x 8 KB of queue, plus one in-flight write (up to about
2 KB) per client; payloads are shared between clients. A client is disconnected when a single
write (drain) does not finish within five seconds; a client that merely reads
slowly loses only its own sentences. Its drop count can be too high by at most
one batch (up to 16 sentences) that was already sent but not yet
acknowledged. See
[transport-security.md](transport-security.md) and
[integration.md](integration.md).

## GeoJSON export

The GeoJSON export reads each line's points sequentially from flash and hands
over to other work every 20 ms (see [Exports](technical.md#exports)). The table
compares it with the backup export on the host, through the real senders and a
synthetic project (`tests/benchmark_export.py`, never field data). The base is
`fafbb93`, before the change; the other rows are the state after the sequential
read and the time-paced sender (`3810d4e`). The cases are `large` (3531
features: four lines of 850 vertices and 131 asset points) and `long` (one line
of 3400 vertices). **Host-emulated:** the seconds say nothing about the board;
the counts of `open()` calls and decodes and the heap peaks (`tracemalloc`) are
exact for the code path.

| Case | Format | State | Bytes | Seconds | `open()` | Decodes | Heap peak | Seconds vs backup | Heap vs backup |
|---|---|---|---|---|---|---|---|---|---|
| large | GeoJSON | base | 5 187 091 | 2.768 | 6800 | 6800 | 106 671 | 2.14 | 3.44 |
| large | GeoJSON | after | 5 187 091 | 0.734 | 24 | 6800 | 35 833 | 0.54 | 1.15 |
| large | backup | after | 4 726 770 | 1.366 | 12 | 3400 | 31 284 | | |
| long | GeoJSON | base | 4 900 806 | 2.658 | 6800 | 6800 | 103 075 | 2.10 | 1.99 |
| long | GeoJSON | after | 4 900 806 | 0.758 | 24 | 6800 | 56 323 | 0.58 | 1.08 |
| long | backup | after | 4 527 628 | 1.315 | 12 | 3400 | 52 136 | | |

The exported bytes are identical before and after. The decodes stay at two per
vertex (one pass counts the frames, one writes the line); only the flash
accesses change, from one `open()` per point to a few per line.

Targets on the device, not measured yet (they belong to the device acceptance
check):

| Target | Value | Status |
|---|---|---|
| Export time | at most 2 times the backup export | open |
| Event-loop lag, median during the export | at most 400 ms | open |
| Heap peak of the GeoJSON export | at most 1.25 times that of the backup | open |

The device run is `python3 device_export_benchmark.py large --port <port>` (or
`long`). It writes: it builds a synthetic project under its own file prefix
(`export-benchmark-v1`) on the device and removes it afterwards, so run it only
after a full backup. Its result splits the GeoJSON time into flash read, decode
and serialization and gives both heap peaks.

## How the numbers were measured

The enforced limits in this document were read from the source, not from a
measurement. The timings come from runs on real hardware with the repository's
own instrumentation:

- `parallel_tracking_test.py` — the host-side driver for the full acceptance
  run. It arms the in-firmware benchmark over an authenticated HTTP session,
  writes synthetic points at 1 Hz into an isolated tracker while the chosen
  service profile (`local`, `ble`, `tcp` or the default `parallel`) is
  continuously verified as live, and then checks the NDJSON export, a planned
  restart and a second export against each other. Only a report that says
  `qualified: true` counts as evidence for a capacity level.
- `parallel_benchmark.py` — the firmware side of that test, running on the
  device. It keeps its synthetic data under a fixed prefix, refuses to start
  while a line is recording, and aborts without qualifying a level if a
  required service stalls for more than ten seconds.
- `device_tracking_benchmark.py` — a resumable synthetic scaling run over the
  USB REPL that reports the append path split by stage: JSON encoding, journal
  and point-store I/O, flushes, `statvfs`, the quality check, index and
  checkpoint (the verified compaction the device runs in operation). Segment
  sizes can be compared in isolation.
- `device_tracking_realtime_benchmark.py` — the same synthetic workload at
  exactly one point per second, reporting append percentiles and event-loop
  lag, which is what makes a stall visible rather than just a slow average.
- `tests/benchmark_tracking_scale.py` — a host-side flash-backed scaling
  benchmark that needs no device.
- `device_nmea_benchmark.py` — a USB Raw REPL program that times the NMEA and
  gate hot path on the device: `parse_gga` and its coordinate and height
  parsers, `quality_check`, the checksum, the line scanner (real
  `GNSSHandler.run()` on a patched UART, with a fixed corpus whose queue
  content must not change) and the RTCM feed. It counts `gc.mem_alloc()` per
  call. It stops the application while it runs, so it is separate from a field
  check with NTRIP connected. Components missing on older firmware are
  reported as unavailable instead of aborting.
- `tests/benchmark_nmea_hotpath.py` — the host side of the same measurement.
  It runs the identical program on CPython and adds what only the host can
  count: how many times one epoch calls `quality_check` on the surveying and
  the route path, the `tracemalloc` peak per component, and `K` for both
  paths. `--baseline FILE` prints the factors against an earlier result.
- `tests/benchmark_export.py` and `device_export_benchmark.py` — the host and
  device runs behind *GeoJSON export*.

The point-record sizes under *Point capacity* are host-emulated, as stated
there. The age figures under *Correction ages and link latency* come from the
field-diagnostics recordings of the dates named in that table, analysed
afterwards.

The firmware itself measures two transport quantities, in ordinary operation and
without any host tool:

- **Event-loop lag.** A 100 ms ticker records how much later than scheduled each
  tick ran. `/status` reports the latest value as `stats.loop_lag_ms` and the
  largest value since boot as `stats.loop_lag_max_ms`. Every field-diagnostics
  sample carries `loop_lag_max_ms`, the largest lag since the previous sample (or
  since the recording started), so it is the maximum of a window of about five
  seconds. These window maxima are not comparable with percentiles over
  individual ticks, as reported by `device_tracking_realtime_benchmark.py`; only
  the maximum of such a run is on the same footing.
- **NTRIP connection timing.** `ntrip_flow.dns_ms` and `ntrip_flow.connect_ms`
  hold the duration of the latest name resolution and TCP connect, and
  `dns_max_ms` and `connect_max_ms` the largest ones. The `ntrip_error`,
  `ntrip_first_data` and `ntrip_end` field-diagnostics events carry the same
  times for their connection attempt. Resolution and connect still run under one
  common deadline; the measurement does not change the timeout or the retry
  behaviour.
- **Receiver UART transmit.** `ntrip_flow.uart_write_max_ms` is the longest
  single `uart.write` call; the call blocks until the 2 KB transmit ring has
  taken the bytes. `uart_commands_sent`, `uart_commands_deferred`,
  `uart_commands_dropped`, `uart_command_wait_max_ms` and `uart_dropped_bytes`
  show how receiver commands and corrections shared the line. A correction
  write that makes no progress for 1 s or takes longer than 2 s ends the
  connection with the error `UartStalled`. To measure a run, read these counters from `/status`
  before and after it and compare the deltas.

Start-up and portal timings come from ordinary runs without host tools on the
device: the boot log carries the milestones, and the field-diagnostics recorder
carries the sample cadence, write durations and correction counters described
in [field-diagnostics.md](field-diagnostics.md).

## Related documents

- [technical.md](technical.md) - the constants in context, and the storage model
- [boot-sequence.md](boot-sequence.md) - what runs before the survey store is ready
- [field-diagnostics.md](field-diagnostics.md) - the recorder behind the field timings
- [integration.md](integration.md) - client-side contract for the connection limits
- [known-limitations.md](known-limitations.md) - what is known to be rough
