# Changelog

Notable changes to the firmware, web interface, and device behaviour.
The development history before the first public release is not published.

## V16.0.0 – 2026-10-30

V16.0.0 is the first public release since V15.8.1 and contains the internal
versions V15.9.0 to V15.10.4, whose changes are described here. It adds
stakeout: target sets imported from GeoJSON, guidance to each target, and a
confirmation that is stored as a survey point. The field app is reorganised
into Measure · Projects · Device, receives its live data over an event stream
instead of polling, and releases a measurement through a checklist that names
every reason and the next step. Positions, heights and times are stored as
exact integers together with their reference frame, epoch, antenna height and
correction ages, and connections, configuration changes, storage and updates
now hold up against stalls, lost answers and power cuts.

### Upgrade notes

- **Survey data schema 5.** Positions and heights are stored as exact integers
  (`lat_e8`, `lon_e8` in 1e-8 degree, `alt_mm` in millimetres). The first start
  converts the survey once; points recorded by older firmware are marked
  `legacy_float32` and stay on their former grid of about 0.4 m. The change is
  one-way: files written by this firmware are not readable by older firmware.
  Download a survey backup in the portal before you update. The first load
  keeps a one-time copy of the former checkpoint as
  `tracking.checkpoint.json.v4`; delete it by hand over USB once the upgrade is
  confirmed, because neither the firmware nor a factory reset removes it.
- **Configuration backup schema 3.** The backup now also carries the
  measurement rules and method, the field setup and the stakeout settings.
  Older firmware refuses a schema 3 backup, while backups made by older
  firmware still restore and leave the new settings unchanged. Keep a
  configuration backup made before the update if you may go back.
- **Firmware packages `state/` and `tracking/`.** The runtime state and the
  survey store are now packages instead of the single files `state.py` and
  `tracking.py`. Let the update with `push.py` run to the end: it writes each
  package completely or not at all and removes the replaced single files only
  after a complete push. If an update is interrupted, the device may not start
  until you run the same push again. Going back to an earlier version needs
  the folders `/state/` and `/tracking/` removed by hand.
- **API contract.** A protected endpoint answers a request without a valid
  session with `401`; `403` now only means that a signed-in request was refused
  (`csrf_rejected`, `origin_rejected`). Survey actions refused for the 20 %
  flash reserve answer `507 storage_reserve` (was `400
  invalid_tracking_request`). Counters have one meaning each: `gga_parsed` and
  `wifi_ssid_active` replace `gnss_msgs` and `wifi_ssid_aktiv`, which remain as
  aliases. New endpoints are `GET /api/events` (Server-Sent Events) and the
  stakeout API under `/api/stakeout`. Check integrations that sign in again on
  `403` or expect `400` for a full device, and move to the new counter names
  (`docs/integration.md`, `docs/technical.md`).
- **The measurement release reads exact values.** The gate judges the exact
  position and height that a point stores. A fix whose coordinates or height
  the receiver sends in a form that cannot be read exactly is not released and
  is refused with `no_position` or `altitude_required`. If the release stays
  closed with one of these reasons while the receiver reports a fix, check
  that the receiver sends standard GGA sentences.
- **New operating structure.** The field app is organised as Measure ·
  Projects · Device. The measurement method (default: average of 5 samples) and
  the field setup (measurement rules, antenna height, reference frame and
  epoch) are device-wide settings under Measure → Field setup; a method chosen
  earlier in one browser no longer applies, so check the field setup once after
  the update. Joining the setup Wi-Fi opens the field app through the captive
  portal; the basic setup page `/setup` stays as a marked fallback for phones
  whose captive-portal window cannot run the field app. Old links and
  bookmarks open the matching new page.
- **Stakeout sets in `stakeout.sets/`.** Imported target sets are stored in
  `stakeout.sets/<project_id>/`, outside the survey. They are not part of the
  survey backup and a factory reset does not remove them: keep the original
  GeoJSON files, and delete a project in the field app to remove its sets.

### Data basis & accuracy

- Positions are parsed and averaged as exact integers (`lat_e8`, `lon_e8`,
  `alt_mm`, `geoid_sep_mm`) in the live fix and in measurements, because the
  stock MicroPython float is only 32 bits wide; the float values in the live
  status are derived from them, and a coordinate or height the receiver sends
  in an unreadable form is left empty instead of shown approximately.
- Stored points measured by this firmware are shown with eight decimals; points
  from older schemas carry a legacy badge, keep seven decimals, and are marked
  `legacy_float32` in details and exports. Legacy values that cannot be
  converted are dropped value by value and counted in
  `legacy_conversion_errors`, and the field app warns after a restore or an
  archive reactivation that hit such values.
- Backups and archives of schemas 2 to 4 remain importable. After a downgrade
  to V15.8.1, object points show meaningless coordinates and, once a point has
  been recorded under the new firmware, no line points at all, and its first
  checkpoint write discards the newer records; restore a backup made before
  the update instead of downgrading in place.
- Exports write 8-decimal coordinates and a `precision` column; distances,
  chainage and line lengths use WGS84 local scale factors instead of a sphere
  and wrap at the antimeridian.
- Every published fix carries its age (`fix_age_ms`, `stale`); a fix older than
  3 s is judged like no fix, measuring refuses it with "GNSS position is
  stale", and the field app greys out the position until a new fix arrives.
- The measurement gate gives one verdict per fix to every endpoint, keeps the
  receiver accuracy for up to 2 s when a GGA arrives before its GST, lists limits looser than the factory
  values in `gate_deviations`, and records its profile with every point
  (`gate_profile`, and `gate_limits` in the GeoJSON export).
- The device decodes the epoch of incoming corrections and reports their real
  age as `epoch_age_sec` next to the arrival age; `ntrip_substate` `stale`
  flags a caster that delivers old epochs, and `ntrip_reconnect_on_stale_sec`
  (off by default) can drop such a backlog with the error code `stale_backlog`.
  The epoch age is a diagnostic and does not enter the gate.
- Every point records the geoid separation, the ellipsoid height and
  `antenna_height_m`; exports state the heights unambiguously
  (`altitude_msl_m`, `altitude_ellipsoid_m`, `ground_height_msl_m`,
  `height_reference`), and the control check compares ground heights when both
  points carry an antenna height.
- Every point records `reference_frame`, `reference_frame_epoch`, the NTRIP
  mountpoint and the station; the project page warns when a project mixes
  frames or has RTK points without a frame, and the gate dialog warns when the
  gate admits DGPS.
- When the correction service sends RTCM 1300, Measure → Field setup offers its
  frame and epoch with "Use reported frame"; nothing changes until you confirm,
  and Measure warns when the reported frame differs from the field setup.
- With an RTK fix and no frame in the field setup, Measure reminds you before
  recording that new RTK points would be saved with the frame "unknown".
- Recording times count seconds since 2000-01-01 on every platform; exports,
  backups and the point details add `recorded_at_utc` and `clock_unsynced`.
- The point CSV counts a single-shot point as RTK fixed or as a GST sample only
  when it really was one.

### Measure

- Measure starts with the active project, the measurement release, the
  measurement method and a short summary of the field setup, then the actions,
  the checklist, the position, storage and the receiver details.
- The checklist has six fixed rows judged only by the device's gate reasons;
  every gate reason has an action text, and the headline names the first
  reason and the next step.
- A "Why not?" sheet checks app connection, receiver, Wi-Fi, caster,
  corrections and accuracy step by step and links to the matching setting.
- The screen names why NTRIP delivers no corrections (switched off, not set up,
  waiting for Wi-Fi, a caster error with its reason, or stopped after repeated
  failures) and offers the matching settings.
- The gate values turn red only when their own check fails; an unknown
  correction age is shown amber as unknown instead of passed.
- The header pill shows the fix and one connection word (Live, Delayed, No
  data); the fix label turns red and reads "blocked" while measuring is
  blocked.
- The screen shows how old the device state is; after 10 s without status data
  it turns grey, locks all recording actions and returns with the first fresh
  answer, and open point dialogs keep their entered values meanwhile.
- Recording actions report in the action bar, with Undo for 6 s after a line or
  object point; undo removes a point only if it is still the last one, and a
  lost answer is checked with the device instead of guessed.
- Cancelling a measurement counts only once the device confirms it, and the
  measurement sheet shows the time left or "no time limit".
- "Start line" stays locked while the gate is closed, with the hint that route
  recording remains possible; route recording reports ready only for a
  position whose height can be stored.
- Messages appear in a block at the end of the page instead of over the
  content, and the capacity warning no longer covers the measurement status.
- The screen updates in place: a focused button keeps its focus and the page
  does not jump.
- Touch targets are 48 px with visible focus and a shape per point type, user
  data is never translated, the German interface is complete, and numbers
  follow the language (decimal comma).
- Screen readers hear each state change once from a dedicated announcer.

### Stakeout (new)

- Each project has a stakeout page (Projects → project → Stakeout, also a
  "Stakeout" button on the project map) with its target sets, counters, the
  next open target and "Activate for stakeout"; opening it never changes the
  active project.
- GeoJSON files are checked and previewed in the browser before the import,
  with the choice of parts and vertices ("every n-th vertex" for long lines),
  the features that are left out with their reason, and hints such as
  overlapping targets, swapped axes or the distance to your position.
- The device checks every import completely, stores up to 4 sets per project
  (500 targets and 2000 line vertices per set, 128 KiB per import), verifies
  each written file, and names the offending field when it refuses a package.
- The target list shows every target in walking order with its status and
  distance, and a part can be walked in reverse.
- Guidance uses a north-up map in the search view and a target dial with the
  walking direction in the fine view, from a device packet once a second
  (`GET /api/stakeout/live`); outdated data greys the guidance and hides the
  marker after four seconds.
- The stakeout tolerance (0.1 m) and fine radius (5 m) are device settings
  that apply without a restart.
- A target is confirmed against the live position, or as "Found on site" with
  an optional note, skipped, or reopened; "Saved" appears only after the device
  stored the change, and a repeated or late request cannot change a target
  twice.
- A confirmation is stored as a survey point of the type "Stakeout check" with
  its offsets, tolerance and mode, and is marked in the project GeoJSON and CSV
  export; undoing it reopens the target.
- Skips, reopenings, the active target and the part order survive restarts;
  the active target is remembered but not selected again automatically.
- The stakeout overview exports the state of all targets of a project as
  GeoJSON or CSV, as it was at the start of the export.
- Deleting a project or removing a set removes its stakeout files in small
  steps; a survey restore on the device reopens all targets of the affected
  projects, and archives record whether a project had stakeout data.

### Projects

- The project library has its own archive view; the survey data backup is
  labelled apart from the device configuration backup, and a project's
  export, rename and archive are grouped as project actions, with deleting set
  apart.
- Opening a project never changes the project you measure into.
- The project map keeps zoom, fit and centre at hand and moves layers and full
  screen into a menu; new points and lines appear without rebuilding the map,
  and zoom, layers and the selection stay.
- Returning to the library, the archive or a map shows the last loaded view at
  once, marked "As of <time>" until fresh data arrives; long loads show what is
  loading and the elapsed time, with "Cancel loading".
- RTK points recorded without a reference frame can be given a frame and epoch
  afterwards; the dialog names the affected points, coordinates are not
  transformed, the assignment can be undone, and exports name it in
  `reference_frame_source`, `frame_assignment` and `crs_note`.
- The GeoJSON export is several times faster, shows its progress with the
  elapsed time and a Cancel button, checks that the file arrived complete, and
  hands it over with separate Share file and Download file buttons.
- Each line is exported as it was when its output began, so a point added or
  undone during an export cannot mix two points, and an export to a client that
  stopped receiving ends after 120 s.
- Survey restore and storage optimization show their progress and result; JSON
  and NDJSON backups go through one restore path, a paused line keeps its pause
  reason, and a restore is published atomically, so a power cut leaves either
  the old or the new survey.
- Archived projects and empty projects keep readable cards on phones, and the
  project view no longer scrolls sideways on narrow screens.

### Device & configuration

- Device has four areas: Connections (Internet & corrections, device access
  with Bluetooth LE, TCP, device code, stream key and QR codes), Operation &
  display, Diagnostics and Maintenance (configuration backup, restore and
  restart); "Sign out" is in the header.
- Operation & display offers the appearance Automatic, Light, Dark or High
  contrast for this browser; Automatic follows the phone or computer and is the
  default, and the log, the receiver console and the sign-in page follow it.
- Saving starts with "Check changes", which lists the changed settings and
  whether they take effect at once or restart the device, and sends only the
  changed values; a rejected value is marked at its field with the reason.
- Applying Wi-Fi settings, restoring a configuration backup and restarting
  share one progress panel that continues through the sign-in after the
  restart and ends with the result of this request, or says which network to
  join after 90 s without an answer.
- A restart from the field app during a measurement or line recording has to
  be confirmed with "Restart anyway".
- Caster settings take effect without a restart, and a parked or switched-off
  NTRIP client starts again when they change.
- Wi-Fi changes are kept in memory until they are applied, verified after the
  restart, and rolled back when the device cannot join; a rollback restores
  only the Wi-Fi keys of that change, and a power cut during apply or rollback
  never leaves an unconfirmed change in place.
- A configuration backup restores on a device without NTRIP or Wi-Fi set up or
  in its factory state (Wi-Fi access is then kept), and the restore reports
  refused, too large or partly saved backups.
- Invalid ports and watchdog timeouts load as the default with a warning
  instead of being truncated; an unreadable `config.json` is never
  overwritten in the same boot, and a file that does not parse is kept as
  `<file>.unreadable`.
- Setup mode (BOOT 3 s, or no network configured) keeps the setup access point
  for ten minutes, and the Wi-Fi scan works there.
- After signing in, the field app opens the page that was requested, and the
  QR code on the device label opens the sign-in.
- The help text for fallback networks describes the real order (as entered,
  the main network first), and `/save` refuses more than eight fallback
  networks or fallback passwords shorter than eight characters.

### Connection, performance & robustness

- `GET /api/events` streams the live snapshot at least once a second; the
  field app keeps one stream open on the measure and device pages, falls back
  to polling when the stream is missing, silent for 22 s or refused, and
  reopens it after a confirmed change of your own.
- NTRIP reconnects back off for real (5 s up to 300 s), alternate between the
  configured casters, and return to the primary caster after a stable stream;
  a client that used up `ntrip_max_retries` parks instead of crashing.
- The caster receives the rover position only while the published fix is
  fresh, so it never gets an old position after the receiver goes silent.
- Receiver commands are no longer written into the middle of an RTCM frame, a
  partial correction write is completed, and a UART that stalls ends the
  caster connection as a counted failed attempt.
- Each NMEA-TCP client has its own bounded queue, so a client that stops
  reading loses only its own sentences and is disconnected after 5 s; the
  `AUTH` handshake must finish within 10 s, and a stream token rotation closes
  the connected sockets.
- Wi-Fi scans and NTP clock setting never block while a line records or a
  measurement runs.
- An unexpected error in a request handler is answered `500 internal_error`,
  a lack of memory `503 device_busy`, a malformed request line `400`, and
  `HEAD` works on every endpoint, instead of closing the connection without an
  answer.
- The supervisor restarts a failed portal or NMEA-TCP server, and a task that
  keeps crashing restarts after a growing wait of up to 60 s.
- GGA parsing, NMEA splitting and the gate check cost far less CPU time per
  epoch, and long storage work (undo, export, stakeout file removal) yields to
  other tasks in small steps.
- Repeated warnings are summarised in the log, and the persistent error log
  limits flash writes per severity and tag.
- Point-store segment files that no manifest names are cleaned up after a
  verified load, or kept in quarantine after a manifest recovery; new segments
  are flushed before a manifest names them, and the boot counter is written
  atomically.
- The device clock is re-synchronised against the receiver on a deviation of
  more than 1 s, and every step is logged as `clock_resync`.
- A receiver that stops sending is detected within 3 s, logged once, and shown
  in the live status; the status LED shows "no fix".

### Diagnostics

- `/api/ui/live`, `/status` and the diagnostics package carry a `link` block
  that says whether the receiver delivers data and whether corrections arrive,
  plus `network.ntrip_config` and `network.last_ntrip_error_code`.
- `/status` reports the GGA parse cost (`gga_parse_us`, `gga_parse_us_avg`),
  event-loop lag (`loop_lag_ms`, `loop_lag_max_ms`), NTRIP name resolution and
  connect times, UART command and drop counters in `ntrip_flow`,
  `http_connections_total`, the event stream counters (`sse_*`), and the
  service CRS decoded from RTCM 1300.
- `ble_notify_drops` and `ble_queue_overflows` add up to `ble_drops`, failed
  BLE pairings count as authentication errors, and `nmea_tcp_drops` counts
  lost sentences only.
- `uart_rx_rate_bytes_sec` drops to 0 within about 2 s when the receiver stops
  sending.
- The field diagnostics record receiver silence, configuration reloads of the
  NTRIP client, clock steps and the service CRS, every sample carries the
  largest loop lag, and the recorder writes its buffers in small slices.
- The device log view keeps up to 1000 entries and its scroll position, and
  every GeoJSON export writes one log line with its result, size and timing.

### Tools

- `app.js` is built from the ordered sources in `src/`; `python3 push.py .
  --build-only` builds without a board, `--check` verifies the committed build,
  and unknown options are refused.
- `push.py` refuses changed browser assets under the asset version already on
  the board, skips identical files, and writes `assets.py` last, so an
  interrupted push can simply be repeated.
- `python3 tests/browser_acceptance.py` runs the mock-browser acceptance with a
  pinned Playwright version, and CI runs it with the build check.
- `device_nmea_benchmark.py` measures the NMEA and gate hot path on the device;
  `tools/gst_cycle.py` prints the UART command counters, and
  `device_tracking_benchmark.py` measures the checkpoint through the verified
  compaction (not comparable with earlier values).

Limits that remain are listed in
[docs/known-limitations.md](docs/known-limitations.md).

## V15.8.1 – 2026-09-24

- Keep total points, the active point limit and remaining capacity visible on
  the status screen, with warnings at 80, 90, 98 and 100 percent.
- Estimate time to the point limit from recent recorded points; suppress the
  estimate during pauses, stale data and insufficient observation.
- Select another project to archive directly from the capacity display, showing
  how many points it frees. Preserve the paused line for explicit resumption.
- Allow archiving and deleting other projects while a line remains active;
  continue protecting the project that owns that line.
- Distinguish the point limit from the device's free storage and reserve.

## V15.8.0 – 2026-09-22

First public release.

- Product name ESP-RTK; the factory fallback network is ESP-RTK-Setup.
- English documentation: README, technical reference, onboarding,
  integration contract, security model, performance limits, and known
  limitations.
- See docs/known-limitations.md for the weaknesses that are documented
  rather than fixed in this release.
