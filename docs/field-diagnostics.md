# Field diagnostics

The optional recorder in `field_diagnostics.py` stores a nominal five-second
diagnostic sample on the device, independently of survey points, distance
filtering, and Wi-Fi availability. Enable it on **Device -> Diagnostics**
before a controlled test. The portal header then adds `DIAG on` next to the
current fix. Stop the recorder and download the log soon after the test.

The ordinary support package (`GET /api/diagnostics`, *Download support
package*) contains the recorder status; the separate **Download field log**
button streams the recorded NDJSON history.

> **Do not attach either download to a public issue.** The field log contains
> measured positions, and the support package additionally contains configured
> and visible network names, the flight recorder, and the persistent error log.
> Strip or summarise them before sharing, or send them privately as described
> in [../SECURITY.md](../SECURITY.md).

A recorder that was enabled before a power cycle resumes after the restart,
keeping its session ID and creating a new random boot ID. Stop clears that
resume marker. Starting an already enabled recorder is idempotent; starting
after a stop creates a new session without erasing the retained earlier
sessions. A new installation starts with the recorder off.

## Recorded evidence

Every record carries a UTC string or `null` with an explicit `clock_valid`
flag, a monotonic `up_ms`, the boot ID, the session ID, a `kind`, and a `data`
object. UTC is never invented for an invalid clock - the clock counts as valid
only from the year 2024 on. A `boot` record includes the reset cause; never
read a reset cause alone as proof of an electrical defect.

| `kind` | Content |
|---|---|
| `segment` | First line of every file: schema, firmware version, sequence number, sample and flush interval, format notes |
| `boot`, `start`, `stop` | Session lifecycle; `boot` also carries the reset cause |
| `sample` | The periodic snapshot described below |
| `marker` | One of `marker`, `stationary`, `walking`, `hotspot_off`, `hotspot_on`, `power_off` |
| `fix_change` | Previous and new GGA quality, plus the GGA UTC field |
| `access_state`, `ntrip_state` | Transitions of the two published state machines |
| `wifi_attempt`, `wifi_result`, `wifi_lost`, `wifi_restored`, `wifi_backoff` | Station attempts and their outcome |
| `ntp` | `synchronized` or `failed` |
| `ntrip_first_data`, `ntrip_end`, `ntrip_error`, `ntrip_backoff` | Correction stream boundaries; `ntrip_end` carries `eof` or `idle_timeout` |

A `sample` record holds:

- the current GGA fix: UTC, quality, satellites, HDOP, latitude, longitude,
  altitude, correction age, station ID, and the same-epoch GST horizontal and
  altitude standard deviations,
- the latest independent GST with its own epoch and a monotonic `gst_age_ms`,
  even while the GGA/GST pairing is still pending, plus `fix_age_ms` and the
  actual interval as `sample_gap_ms`,
- `signal_summary`: checksum-valid GSV and GSA sentences compacted in RAM
  outside the GNSS receive path. `observations` keeps the latest talker,
  signal ID, satellite ID, elevation, azimuth, and C/N0 per satellite within
  the interval; `used_satellites` keeps the latest GSA membership per system.
  An empty interval means nothing was captured; stale observations are not
  repeated,
- `rtcm`: frame counts by message type, validated with CRC24Q, plus CRC
  failures, truncated frames, and the age of the last valid frame. Connection
  boundaries discard and separately count a partial frame instead of joining
  two unrelated TCP streams into a false CRC error. MSM records carry station,
  satellite and signal masks, the raw constellation-specific epoch, the
  multiple-message flag, and the active-cell count; distinct masks of the same
  type within one interval are retained,
- `wifi` (access state, profile, link state, radio RSSI and channel) and
  `ntrip` (state, endpoint label, byte and GGA counters, receive age, read
  timeouts, connection and chunk counters, receive-gap and UART write maxima,
  GGA failure and gap counters),
- `storage`: flush count and the last and maximum write duration,
- `drops`: dropped events, dropped NMEA lines, dropped RTCM bytes,
- `runtime`: CRC errors, GGA parse errors, queue overflows, task restarts.

Comparing the NTRIP and storage counters between samples distinguishes a
silent transport from a pause caused by a diagnostic write.

Signal IDs in the RTCM section are **RTCM MSM IDs**, not NMEA GSV IDs. They
describe the content of the correction stream, not proof that the rover used
every signal or resolved its ambiguities; the parser decodes no phase
observations and no cycle slips. MSM header offsets follow the primary
implementation in
[RTKLIB, decode_msm_head](https://github.com/tomojitakasu/RTKLIB/blob/master/src/rtcm3.c).
CRC validation proves frame integrity, not the correctness or completeness of
the correction service.

Network names, passwords, URLs, and raw exception messages do not enter the
event payloads. Wi-Fi events name the profile as `network-1`, `network-2`, and
so on - the effective configured order - or `unconfigured`; NTRIP events name
the endpoint as `primary` or `fallback`, and an `ntrip_error` carries the
exception class name and errno rather than its message. An `ntrip_end` with
reason `idle_timeout` includes the measured monotonic silence and the
configured threshold (`ntrip_idle_timeout_sec`, 15 s by default).

Mark a fixed action just before performing it. A browser connected over Wi-Fi
cannot mark an action on the receiver while it is disconnected.

## Storage and timing limits

The recorder owns at most `MAX_SEGMENTS` (32) files of `SEGMENT_BYTES`
(128 KiB) each, named `field-diagnostics-<8 digits>.jsonl`, plus the resume
marker `field-diagnostics.state`. That is at most 4 MiB, outside the survey
storage. A new file is started when the next batch would exceed the segment
size; before it is created, files in excess of the limit are removed, oldest
owned file first. Every write additionally keeps a 20 % filesystem reserve
free, which can shorten retention further. No survey or configuration file is
ever removed to make room; if space cannot be reclaimed, the status reports
the error `storage_reserve` and the portal shows a storage error.

Retention therefore depends on satellite and correction traffic. It is a
rolling window, not a guaranteed multi-hour archive.

Producers enqueue bounded data and never touch flash: at most 32 pending
records, 48 queued NMEA lines (lines longer than 160 bytes are dropped and
counted), and 8 KiB of queued RTCM. Corrections reach the GNSS UART before the
diagnostic capture. The worker turn handles one RTCM chunk and up to four
queued GSV/GSA sentences and then yields, storing compact observations instead
of raw sentences. Samples are buffered in RAM and flushed nominally every 30
seconds; `start`, `stop`, a resumed `boot`, and a marker are flushed
immediately. Flash operations and other tasks can delay the cadence;
`sample_gap_ms`, the drop counters, and `max_write_ms` expose those limits.

A power interruption can lose the pending interval and events and leave an
incomplete last line. Recovery starts a new segment and never appends to that
torn tail. Readers should ignore blank lines and report a malformed final line
instead of treating it as a complete record. Per-boot counters restart at
boot, so both UTC and uptime are needed around an RTC reset.

## Control and export

| Method | Path | Effect |
|---|---|---|
| GET | `/api/field-diagnostics` | Recorder status: enabled, session, boot, error, samples this boot, stored and limit bytes, segments, rotations, drop counters, `max_write_ms`, intervals, pending records |
| POST | `/api/field-diagnostics` | `{"action": "start"}`, `{"action": "stop"}`, or `{"action": "mark", "label": "..."}` |
| GET | `/api/field-diagnostics/export` | Streams the concatenated NDJSON as `field-diagnostics.ndjson` |

Every one of them requires a device-code session, and the `POST` additionally
requires the CSRF token and an acceptable `Origin`. The export refuses with
`409 diagnostics_busy` while the recorder runs or while another export is in
flight, and a start is refused with `409 diagnostics_refused` while an export
is running. The export preserves the raw bytes, separates two files with a
newline, and therefore isolates a torn tail from the next file.

## A controlled field test

1. Enable field diagnostics, obtain RTK FIXED, mark `stationary`, and wait
   five minutes.
2. Mark `walking`, walk 20-30 m, mark `stationary`, and wait two minutes.
3. Mark `hotspot_off`, switch the hotspot off, restore it, and mark
   `hotspot_on` once the browser has reconnected. Record the phone's own
   action times separately.
4. Mark `power_off`, briefly remove device power, and restore it. Confirm
   `DIAG on` after the restart, then wait for correction reception and a
   stable position.
5. Stop field diagnostics and download the field log and the support package;
   the export is refused with `409 diagnostics_busy` while the recorder is
   still running.

Keep the acceptance gate unchanged during the test. Comparing FIXED to FLOAT
transitions with GSV C/N0, correction signal masks, CRC and drop counters, and
receive gaps separates a missing correction stream from a loss of satellite
tracking. It cannot by itself identify every antenna, multipath, or
carrier-ambiguity problem.

## Related documents

- [technical.md](technical.md) - HTTP API, NTRIP behaviour, storage layout
- [boot-sequence.md](boot-sequence.md) - where the recorder starts during boot
- [transport-security.md](transport-security.md) - who may read these endpoints
- [../SECURITY.md](../SECURITY.md) - how to report a finding privately
