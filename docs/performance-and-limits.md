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

Where a constant is already explained in depth elsewhere, this document gives
the number and links rather than repeating the mechanism.

## Point capacity

| Limit | Value | Constant |
|---|---|---|
| Active features per device (line vertices plus asset points) | 5000 | `tracking.py` `MAX_FEATURES` |
| Design target of the compact schema | 10000 | `tracking.py` `TARGET_MAX_FEATURES` |
| Projects | 20 | `tracking.py` `MAX_PROJECTS` |
| Lines per project | 500 | `tracking.py` `MAX_LINES_PER_PROJECT` |
| Custom properties per project or line | 32 | `tracking.py` `MAX_PROPERTIES` |
| Uploaded backup | 32 MiB | `tracking.py` `MAX_BACKUP_BYTES` |

`GET /api/tracking` reports `used`, `limit`, `remaining`, `percent` and a
`level`. The level is derived from the active limit: `notice` at 80 %,
`warning` at 90 %, `critical` at 98 % — each rounded up — and `full` once the
limit itself is reached. At a limit of exactly 10000 the fixed values 8000,
9000 and 9800 are used instead (`tracking.py` `capacity()`). Below the notice
threshold the level is `ok`.

`full` is not a warning, it is a stop. Creating a line vertex or an asset point
raises "Tracking point limit reached", and an automatic recording is paused
rather than failing silently, so the line stays recoverable. The same check
runs before a restore, before reactivating an archive and before importing a
backup, so none of those paths can push a device past the limit.

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
(`tracking.py` `MIN_FLASH_RESERVE_PERCENT`), and the check is made against the
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
samples it at most every 128 writes (`tracking.py` `FLASH_RECHECK_WRITES`) and
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
- **Correction age.** With a healthy correction link the age reported with a
  fix sits around a couple of seconds. It is not bounded: values well over a
  minute have been observed while the Wi-Fi link stayed up. This is why the
  acceptance gate checks the correction age (default 5 s) in addition to the
  fix type — an RTK FIXED indicator on stale corrections is exactly the case
  the gate exists for.

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
running (`web.py`), and answers concurrent survey reads and writes
`storage_busy` while maintenance is in progress.

## Concurrent clients and connections

| Transport | Limit | Defined in |
|---|---|---|
| HTTP connections in parallel | 6, then `503` with `Retry-After: 2` | `web.py` `HTTP_MAX_CONNECTIONS` |
| HTTP header deadline | 10 s | `web.py` `HTTP_HEADER_DEADLINE_SEC` |
| HTTP body deadline | 10 s | `web.py` `HTTP_BODY_DEADLINE_SEC` |
| NMEA TCP clients | 4 | `cfg.py` `nmea_tcp_max_clients` |
| Queued NMEA sentences | 512 | `cfg.py` `nmea_queue_size` |

Responses are **HTTP/1.0 with `Connection: close`**. Every request therefore
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
[known-limitations.md](known-limitations.md).

The practical consequence for integrators: **a client timeout must cover
connection setup, not just the response body.** A budget of a few seconds that
assumes an instant connect will fail on a device that is answering perfectly
well. The portal itself allows 20 s per project read for this reason.

NMEA TCP connections that are still authenticating count against
`nmea_tcp_max_clients` while they are pending, which is a second reason to keep
the client count low on a shared network; see
[transport-security.md](transport-security.md) and
[integration.md](integration.md).

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
  checkpoint. Segment sizes can be compared in isolation.
- `device_tracking_realtime_benchmark.py` — the same synthetic workload at
  exactly one point per second, reporting append percentiles and event-loop
  lag, which is what makes a stall visible rather than just a slow average.
- `tests/benchmark_tracking_scale.py` — a host-side flash-backed scaling
  benchmark that needs no device.

Start-up and portal timings come from ordinary, uninstrumented runs on the
device: the boot log carries the milestones, and the field-diagnostics recorder
carries the sample cadence, write durations and correction counters described
in [field-diagnostics.md](field-diagnostics.md).

## Related documents

- [technical.md](technical.md) - the constants in context, and the storage model
- [boot-sequence.md](boot-sequence.md) - what runs before the survey store is ready
- [field-diagnostics.md](field-diagnostics.md) - the recorder behind the field timings
- [integration.md](integration.md) - client-side contract for the connection limits
- [known-limitations.md](known-limitations.md) - what is known to be rough
