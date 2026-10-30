# Known limitations

This is the list of things that are known to be rough, wrong or unfinished in
the firmware as it stands. It exists so that nobody has to discover them in the
field.

Each entry says the same three things: **what you observe**, **why** it happens
in one sentence, and **what to do about it**. There are no promises here about
when or whether any of it changes, and an entry being on this list does not
mean a fix is planned. Where a limitation is a consequence of a design decision
that is explained elsewhere, the entry links instead of repeating the argument.

Everything below was checked against the source. Where an item is a gap in
*evidence* rather than in the code, it is marked as such.

## Survey data and handing a device on

### A factory reset does not remove measured positions

**You observe:** after a factory reset, the projects, the measured points and
any field-diagnostic recordings are still on the device.

**Why:** the reset routine deletes the survey event journal and its checkpoint
by name, but the names it uses for the point store do not match the ones the
tracker actually writes, and the archive directory and the field-diagnostics
files are not in its list at all (`cfg_factory.py`; the tracker's own paths are
in `tracking/common.py` and `pointstore.py`).

**What to do:** treat a factory reset as a credential reset, not a data wipe.
Delete the projects in the portal before handing a device on. Field-diagnostic
recordings cannot be deleted from the portal at all — the recorder's endpoints
only start, stop, mark and export it — so they leave only through the
recorder's own oldest-first rotation. To be certain a device carries no
positions, erase the filesystem or reflash it. The one-time copy of the former
checkpoint that the upgrade to schema 5 keeps, `tracking.checkpoint.json.v4`,
also holds positions and is never removed by the firmware: delete it by hand
over USB once the upgrade is confirmed. See
[onboarding.md](onboarding.md), *Reset*, and [technical.md](technical.md),
*Factory reset*.

### Stakeout target sets are kept apart from the survey

**You observe:** a survey backup does not contain the stakeout target sets
that were imported into a project, and a restore brings none back. A factory
reset leaves them on the device. Deleting a project in the portal removes its
sets.

**Why:** stakeout sets live in their own folders, `stakeout.sets/<project_id>/`,
outside the survey journal, the checkpoint, and the survey backup format. A set
records the `created_at` of its project, and a set whose project has a
different `created_at` is treated as orphaned: it is not listed, not readable,
and not deleted, but it keeps its flash. Deleting a project sets its folder
aside and removes it after the deletion is journaled. There is no automatic cleanup:
the sets of a project that left the survey in another way (a survey restore
that does not contain it, a project deleted by a development build that kept
its sets) keep their
flash, stay hidden, and are counted as `unowned_folders` in `GET /api/stakeout`.
They can no longer be removed through the API, because every stakeout request
needs the project (`delete_set` answers `404 project_not_found`); remove such a
folder from the REPL (`os.remove` of its files, then `os.rmdir`); there is no
cleanup tool.

Earlier firmware kept the sets in `stakeout/`, a name that MicroPython imports
in place of `stakeout.py`; the start renames that folder to `stakeout.sets/`.
If both folders exist, the old one is renamed to `stakeout.conflict-1/` (or
the next free number up to 9) and the log shows a `WARN` with that name. Its
sets are not merged and not shown in the app; the folder is kept for
inspection by hand and can be removed from the REPL once it is no longer
needed.

Several cases are not told apart by the `created_at` check:

* Projects created before `created_at` existed have no value to compare, so
  they share the sets of any project with the same ID.
* A project ID is derived from the clock and a counter that restarts at boot.
  After a restart with an unset clock, a new project can get an ID and a
  `created_at` that equal those of a deleted one, and it then sees that
  project's sets.
* Sets in the state `damaged` or `incomplete` are visible under every project
  with the same ID and do not count in `totals.orphaned`.
* A project ID outside `A-Z`, `a-z`, `0-9`, `-`, `_` (1 to 64 characters),
  as it can come from a foreign restore, cannot hold target sets; stakeout
  requests with it are refused with `400`. Deleting such a project works as
  before.

Further limits of the store:

* **A power cut before the rename.** An import that is cut after it began
  writing and before the rename leaves a `.tmp` file, which lists as
  `incomplete`; the device never promotes it. Import the package again; that
  replaces the leftover. A cut during the rename itself gives either no set or
  the complete one.
* **A damage mark from a download is RAM-only.** A download that finds the
  body changed (same length, different bytes) refuses the set from then on, but
  only until the next restart. A listing reads the 4 KiB trailer only and does
  not hash bodies, so the set lists `ok` again after a restart and is marked
  again by the next download. No download delivers the trailer of a changed body.
* **The flash reserve is conservative.** An import needs 819 200 bytes
  (about 800 KiB) of free flash before it starts, because the file size is not
  known beforehand, although a set file is at most 384 KiB. With nearly full
  flash a small set can be refused with `507 storage_reserve`.
* **A stray non-empty subdirectory keeps a project deletion pending.** The store
  never makes subdirectories, but if one with files in it is left inside a
  project folder (by hand, over the REPL), the removal after a deletion stops
  there: the answer says `"stakeout_cleanup": "pending"`, a `WARN` is logged at
  every start, and the folder `stakeout.sets/<project_id>.deleting` stays until the
  subdirectory is removed by hand. Imports and set deletions of a project with
  that ID answer `409 stakeout_cleanup_pending` meanwhile. The removal deletes
  each name in the folder with `os.remove`; whether an empty subdirectory is
  removed that way depends on the filesystem and is checked on the device
  (LittleFS removes an empty directory, the host tests follow CPython, which
  does not).
* **A survey restore and a pending deletion.** A restore that brings back a
  project whose committed deletion is still pending makes that project live
  again, so the next start renames its folder back and the old sets reappear.
  A restore that does not contain such a project lets the next start remove the
  set-aside folder, as the deletion requested.
* **A blocked project blocks every survey restore.** If the folder of a project
  could not be renamed back after a failed deletion (a double fault), the project is
  blocked until the next start: its stakeout requests answer `503
  stakeout_recovery_pending`, and archiving or deleting it answers `409`. A
  restore replaces the whole survey and cannot be limited to one project, so
  while any project is blocked every restore entry (the `restore` action,
  `reactivate_archive`, and the NDJSON upload) answers `409
  stakeout_recovery_pending`. Restart the device; the start-up settles the folder.
  When the stakeout folder cannot be listed at start-up (an I/O error), the
  start-up cannot tell which projects have a folder set aside, so every project
  counts as blocked until the next start, with the same answers; the survey
  still loads.
* **A project whose `created_at` the set trailer cannot hold has no sets.** A
  set records its project's `created_at` as a whole number of seconds from 0 to
  4 294 967 295, or null for projects from before `created_at` existed. A
  restored survey can carry another value (a fraction, a negative number, a
  text); an import into such a project answers
  `409 unsupported_project_fingerprint` before anything is written. Import the
  sets into a project created on the device.
* **A permanent read or I/O error while listing** a project's sets answers `507`
  `stakeout_read_failed` for the overview, imports, downloads, and `delete_set`
  of that project. Deleting the project removes the folder regardless.

**What to do:** delete a project in the portal to remove its sets; keep the
original GeoJSON file, because the survey backup does not protect the sets, and
before handing the device on delete the projects and erase or reflash the
device, since a factory reset does not clear the sets. An import is refused with
`409 recording_active` while a line is recording or a measurement runs; pause
the line or finish the measurement first. After an interrupted import, import
the package again.

### The active stakeout target and its progress

**You observe:** after a restart the device remembers the active stakeout
target and the part order of the active project, but it does not select the
target again by itself: `GET /api/stakeout/live` answers `no_target` until you
select one. The same happens after a switch of the active project, a survey
restore, an archive or reactivation, and after the target's set is deleted or
found damaged. While the progress of a project is loading, the status of a
target is `unknown`, not `open`.

**Why:** the confirmation of a target is a survey point; skips, reopenings, the
active target and the order are stored in `stakeout.sets/<project_id>/progress.jsonl`
and are read when the project's progress loads. The selection is bound to the
project and its `created_at`, so a switch and back, or a restore that replaces
the survey, would otherwise show a target of another survey state.

Limits of confirming and of the progress:

* **Stakeout changes need the active project and no recording line.**
  Selecting, confirming, skipping and reopening a target answer
  `409 project_not_active` for a project that is not the active one and
  `409 recording_active` while a line records; a confirmation also answers
  `409 measurement_active` while a measurement runs. Pause the line or finish
  the measurement first.
* **A confirmation is a survey point.** It counts against the active point
  limit (`409 point_limit_reached`) and needs the flash reserve like any other
  point (`507 storage_reserve`).

* **A repeated action is judged by the state.** The device keeps no list of
  commands. A repeat of an action that already took effect answers `200` with
  `repeated: true`; a repeat or a late action after a reopening, an undo or a
  restore carries an old generation and answers `409 target_changed`.
  An action that arrives while another stakeout change runs is refused with
  `409 storage_busy` and has to be sent again.
* **"Found on site" needs a current position.** Without a fresh fix with an
  altitude the action answers `409 no_position`; a point is always stored.
* **After a write with an unclear outcome, every stakeout change is locked
  until the next restart.** When a write throws after it began, the device
  cannot tell whether it reached the flash. It answers `503
  stakeout_write_uncertain` to every confirm, skip, reopen, import, set
  removal, undo of a stakeout point and survey restore until the device is
  restarted; the restart reads the flash and decides. The selection of a
  target works meanwhile but is not remembered (`remembered: false`).
* **A survey restore on this device reopens all targets of the affected
  projects.** After it all targets of the affected projects are open again,
  even when the backup contains their stakeout points; the points stay in the
  survey as history. The same applies when the restore fails to publish after
  the floor was written.
* **Undo of a confirming stakeout point reopens the target.** If the undo
  fails after the reopening was stored, the target stays open and the old point
  no longer counts.
* **Orphaned stakeout data stay hidden.** The stakeout data of a project that a
  restore removes, or that a crash leaves behind, stay hidden on the flash and
  are counted (`unowned_folders` in `GET /api/stakeout`); there is no cleanup
  tool, and sets with another `created_at` and `progress.jsonl.foreign` are not
  deleted either.
* **The export shows the state at the start of the export.** Changes made
  while the download runs are not part of it. Reimporting the export reads lines
  as single points.
* **The progress file is not part of the survey backup.** A restore from a
  backup brings the stakeout points back but not the skips, the active target or
  the orders.
* **Old stakeout points of a removed set do not count after a reimport.** The
  set floor raised by the removal outranks them; they stay in the survey.
* **Archives made before V16.0.0 carry no stakeout flag.** Only new
  archives record whether the project had stakeout data.

### Stakeout in the field app

**You observe:** the stakeout map and the dial are always north-up, the
numbers change once a second, and the movement arrow appears only while you
walk.

**Why:** the device has no compass, so the app cannot turn the view to your
heading. The device sends one stakeout packet per second, and it reports a
direction of movement only from 0.5 m/s on.

Further limits:

* **Guidance needs the active project.** A project that is not active shows its
  sets, its list and the import; the guidance starts after "Activate for
  stakeout".
* **The import preview lives in the open page.** Reloading the page or leaving
  it drops the preview and the selection; nothing is sent before the import
  button.
* **Every set is read whole.** The list and the overview download each set once
  per page, up to 384 KiB per set; on a slow link the list fills set by set.
* **The status is "unknown" while the progress list cannot be read.** The
  pages read the progress list together with the overview. If the device is
  busy with another stakeout change or the list cannot be read, every target
  shows "? Unknown", none is offered as the next open target, no action button
  is shown, and only the counters of the overview are known. The page and the
  action dock show "The stakeout progress could not be loaded" with "Load
  again"; while the list is missing, the page also reads it again on its own
  with the next live packet, at most every 10 s.
* **Confirm, Skip and Reopen are sent at most three times.** Without an answer
  the app sends the same request again after a second (also when the device is
  busy) and then says "Not confirmed – check the list" and reads the list again;
  it says "Saved" only after the device answered that the change is stored.
  After leaving the page the result appears only as a message.
* **The note of "Found on site" is one line** of at most 500 characters.
* **The stakeout export covers the whole project.** The page offers GeoJSON or
  CSV of all sets of the project; one set alone can be exported only through
  `GET /api/stakeout/export` with `set_id`.
* **Two stakeout entries in the project view.** A "Stakeout" button in the lower
  right corner of the project map is always in view; the entry with its
  explanation sits right below the map and needs a short scroll on a small
  phone (360 × 640), because the map fills the screen above the navigation bar.
* **After a write with an unclear outcome, the pages only show a notice.** Until
  the device is restarted, the stakeout pages say "Stakeout locked until the
  device restarts", the guidance is never ready, and imports and set removals
  are refused.

Further limits of the live guidance:

* **The first selection in a large set reads its body once.** The first target
  lookup in a set after its listing was cached reads the whole body (up to
  384 KiB) in blocks of at most 4096 bytes with pauses and checks its SHA-256,
  so that first selection takes noticeably longer than the next ones; other
  requests are served meanwhile. Later selections in that set stop at the
  target.
* **There is no compass.** The bearing to the target is exact, but the
  direction of movement (`motion.course_deg`) comes from the positions of the
  last two RTK epochs only, so it is `null` below 0.5 m/s and while the rover
  stands still or the fix is not RTK. Turning on the spot does not change it.
* **The live packet has no target name.** It carries `tid` and the position only;
  the browser takes the name from the set file it already holds.
* **The quality window is not kept across a configuration change.** Changing the
  tolerance, the fine radius or one of the eight gate limits empties the window;
  the guidance needs fresh epochs to reach `ready` again.

**What to do:** select the target again after a restart or a project change.
Keep the set files; they are not affected.

### A survey restore stops every start-up

**You observe:** the portal shows *Survey storage unavailable* after every
restart, all project and map requests answer `503`, and `/status` reports
`stats.tracking_startup` as `error` with the code `restore_commit_unreadable`
or `restore_install_failed`. A restore upload answers `503 survey_unavailable`.

**Why:** a survey restore was published (its commit record
`tracking.restore.commit.json` was renamed into place) and each start-up must
finish it before it may open the survey; when that record cannot be read, or a
step of the installation keeps failing on flash, the start-up stops instead of
guessing, and the portal offers no action that could decide in its place.

**What to do:** restart once; a transient flash error clears that way. If the
code stays, connect over USB and open the MicroPython REPL (for example
`mpremote connect <port> repl`). First copy every `tracking*` file off the
device (`mpremote fs cp :<name> .`), so that nothing is lost. Then, in the REPL:

- `import os; print(os.statvfs("/"))` — `restore_install_failed` on a full
  file system: free space and restart.
- `import os; os.remove("tracking.restore.commit.json")` and restart, for
  `restore_commit_unreadable` or an installation that cannot finish: the next
  start-up then drops the staged `tracking.restore.*` files and opens the live
  survey files. If an earlier start-up had already begun the installation, the
  survey can be a mix of the original and the restored one, so check it in the
  portal and, if needed, run the restore again from its backup.

`restore_install_pending` (*a survey restore is published but not installed*)
needs only a restart: that start-up completes the installation.

### Point-store files are kept after a manifest recovery

**You observe:** each start-up logs `WARN Point store was recovered from a
fallback manifest; <n> unnamed point-store files (<bytes> bytes) are kept until
tracking.points-v1.jsonl.recovered.json is removed.`, `/status` reports
`stats.storage_recovery.quarantined_point_files` above zero, and free flash is
lower than the survey explains.

**Why:** the main point-store manifest was damaged or missing and the store came
back from its `.previous` or `.tmp` copy, so the segment files that copy does
not name may hold the only copy of points recorded later, and the firmware
cannot tell; see [technical.md](technical.md), *Quarantine after a manifest
recovery*.

**What to do:** check the survey in the portal. Over USB, copy the marker and the
files it lists (`tracking.points-v1.jsonl.g<generation>-<number>.pjs`) off the
device if anything is missing, for a manual recovery. To give the flash back,
remove the marker in the REPL, `import os;
os.remove("tracking.points-v1.jsonl.recovered.json")`, and restart: that
start-up deletes the unnamed files. Removing the listed files yourself also
ends the quarantine.

## Firmware updates

### An interrupted update can stop the device from booting

**What you observe:** after a `push.py` run was aborted (cable pulled, laptop
asleep, a file failing its check), the device does not start the application.
It stays reachable over USB, and the console shows an `ImportError` for `state`.

**Why:** since the state moved into the `state/` package, the board imports the
folder `/state/` before the old file `/state.py`. Once `/state/` exists, the
device only boots when the whole package is there.

**What to do:** run the same `push.py` command again; it writes the missing
package files and removes the old `/state.py`. To go back to a version from before
the package, first remove the `/state/` folder and its files from the board by
hand, then push the older tree.

The same holds for the survey store, which moved into the `tracking/` package
(14 files, so the window in which an interrupted push is dangerous is longer):
the board imports the folder `/tracking/` before the old file `/tracking.py`,
and running the same `push.py` command again restores the device. To go back to
a version from before the package, remove the `/tracking/` folder by hand too.

The peak memory the device needs to compile the `tracking/` modules at start-up
was not measured on the device. The host tests only bound each compilation unit
(at most 41 000 bytes of source and 14 500 bytes of bytecode per module), which
limits that peak, but the heap during the import itself is not verified.

## Credentials in logs and support data

### The generated access-point password is written to the log

**You observe:** the setup access-point password appears in clear text in the
serial console, in `/errors` and inside a support package.

**Why:** when the firmware replaces a missing, weak or legacy access-point
password it logs the new value at `INFO` level (`net.py`), and every log line
at that level also reaches the RTC flight recorder that those two outputs serve
(`cfg_logging.py`, `web.py`).

**What to do:** treat the log as a credential. Never attach `/errors` output or
a support package to a public issue or send it to a third party unreviewed.

### Support packages and configuration backups are not safe to share

**You observe:** a support package looks redacted, but the access-point
password is still in it; a configuration backup contains the access-point
password, the device code and the stored Wi-Fi and NTRIP credentials in clear
text.

**Why:** the support package redacts a fixed list of values — the Wi-Fi and
NTRIP credentials, the device code and the stream token — and the
access-point password is not on that list; the configuration backup is
deliberately a complete operational copy (`web.py`). The BLE passkey is in
neither one: it is disclosed only by the `/label` page and `/api/label`,
behind the same administrator session as the rest of the portal, by design.

**What to do:** handle the support package and the configuration backup
exactly like the credentials themselves. If you need to share a support
package, read it first and remove what you do not want to disclose. Treat the
label page the same way. See [transport-security.md](transport-security.md).

## Transport and access

### There is no HTTPS

**You observe:** the portal is served over plain HTTP on both interfaces, and
the browser marks it as not secure.

**Why:** a device with no globally resolvable name and no certificate authority
on a local network cannot obtain a certificate a browser would accept, so no
attempt is made to pretend otherwise (`web.py`).

**What to do:** administer the device only on a network you control — the setup
access point, a field hotspot of your own, or a private LAN — and treat a
shared or public Wi-Fi as unsuitable. The full consequence list is in
[transport-security.md](transport-security.md).

### A stream-token rotation during a handshake ends that connection

**You observe:** a TCP client that began its `AUTH` handshake with the old
token just before the token was rotated receives `OK AUTH` and then the
connection ends.

**Why:** the token is checked when the line arrives; the rotation happens while
the server's `OK AUTH` reply is still being written. The connection is then not
admitted (`fanout.py`).

**What to do:** nothing; the client reconnects and authenticates with the new
token.

## Configuration changes

### A caster change can take a while to reach a connection that is being set up

**You observe:** after you save new caster settings, the client needs longer
than a few seconds to try them.

**Why:** an open connection to a caster that is no longer configured is closed
at once, and waiting slices end within 5 s. A connection setup already in
progress finishes first, at most `ntrip_connect_timeout_sec` (15 s by
default), and the name resolution inside it is synchronous and covered by no
deadline. A connection to an unchanged caster is left alone and takes the
change over at its next I/O return, without a general time bound (see
[technical.md](technical.md), *Reloading the configuration*).

**What to do:** wait; the state and `ntrip_config` follow the saved settings
once the attempt ends.

### Apply without a change through the API

**You observe:** `POST /api/config/apply` answers `409 nothing_to_apply` when
nothing is staged and every caster change has reached the NTRIP client.

**Why:** there is nothing to arm. The portal no longer calls apply for a save
that took effect at once.

**What to do:** nothing; change a value first.

### Apply is refused while the device connects to Wi-Fi or a change is open

**You observe:** `POST /api/config/apply`, `POST /save`, and the restore answer
`409 config_verification_running` or `409 open_change_pending`. The field app
names the answer and offers to try again later.

**Why:** a change must not be armed while the network manager connects (each
connection round takes at most about a minute; a failed verification boot
takes up to two rounds plus ten seconds), and only
one change may be open at a time. `open_change_pending` lasts until a
verification has ended, including a rollback that is not complete yet.

**What to do:** try again after a minute or after the restart, and check
`/api/config-result`.

### A failed `/save` or restore can still keep the new caster settings

**You observe:** `/save` shows "Save failed!" or the restore answers `500
stage_failed`, but `GET /api/config` and `config.json` already hold the new
caster host, mountpoint, or credentials from that request, and the response
of a later change may report `restart_required`.

**Why:** `/save` and the restore save the caster keys first (policy
`task_restart`, through `apply_live`) and only then arm the Wi-Fi part for the
verified restart; when arming fails, the caster values are not taken back.
The field app names `500 stage_failed` and says that parts of the change may
already be in effect; the legacy `/save` path is unchanged. They are already in effect, because the NTRIP client reloads them when
they are saved; `restart_required` stays only while that handover has failed
(no client, or the reload raised an error).

**What to do:** check `GET /api/config` after a failed save; send the previous
caster values again, or retry the save once the cause in `errors.log` is gone.

### A failed verification reports restored settings that a caster change kept

**You observe:** after you apply a change that only touched caster settings
and the Wi-Fi verification fails, the portal says "Configuration failed; the
previous working settings were restored.", but the new caster values stay
in `config.json`. `/api/config-result` shows `failed` with
`details.restored: []`.

**Why:** a failed verification restores only the Wi-Fi keys the change
touched; caster keys were saved before the restart and are not part of the
rollback. The field app shows `details.restored` as the keys marked to be set
back, next to the settings the device reports and the Wi-Fi state.

**What to do:** read `details.restored` in `/api/config-result`; an empty list
means no setting was changed back. Send the previous caster values again if
you want them.

### A rollback that cannot be written keeps the device off Wi-Fi

**You observe:** after a failed Wi-Fi change the setup access point stays on,
the device makes no station attempt, and apply, `/save`, and restore are
refused with `409 open_change_pending`; `PUT /api/config` and the live PUTs
still save (Wi-Fi keys are only staged in RAM). `errors.log` shows the cause of the failed
write.

**Why:** the previous values could not be written back to the file system, so
the device refuses to join with the unconfirmed candidate and retries the
rollback every minute (`net.py`, `cfg.py`). With a permanently defective file
system this never ends.

**What to do:** join the access point; the portal stays reachable. If the
retries do not succeed, run a factory reset, which deletes the staging files.

### No restart lock during a measurement

**You observe:** the API, `/save` and `/setup` restart the device even while a
measurement or a storage maintenance is running; that work is lost or resumes
after the restart. The field app warns before apply, restore and restart while
a measurement or line recording runs and asks for "Restart anyway".

**Why:** no endpoint checks for running work before it restarts the device.

**What to do:** finish or stop the measurement before you apply a change.

### You sign in again after every restart

**You observe:** after apply, restore or "Restart device" the field app asks for
the device code again.

**Why:** sessions live in RAM. The progress panel continues after the sign-in,
but only in the tab that started it.

**What to do:** sign in; the panel resumes with the result.

### A lost restart answer is only observed

**You observe:** without an answer to "Restart device", the field app reports
only an observed restart; a result without a matching `apply_id` appears
neutral.

**Why:** the page never repeats a request on its own and cannot prove that the
restart came from its request.

**What to do:** check the network and the portal address named in the panel.

### Staged Wi-Fi changes from another tab

**You observe:** when **Device -> Connections** opens, it lists Wi-Fi changes that
another tab or client staged, without their values.

**Why:** staged keys wait in the device memory until apply, and a restart before
apply drops them.

**What to do:** apply them as seen (the device refuses with `staging_changed`
if they change meanwhile) or restart the device to discard them.

### The rollback is not reported as finished

**You observe:** after a failed change the field app names the keys marked to be
set back, the settings the device reports and the Wi-Fi state, but not that the
rollback is complete.

**Why:** the device records the decision before the rollback finishes and
reports no completion.

**What to do:** read the Wi-Fi state; reload after a minute if the access point
is still on.

### The correction step can show the old stream for a moment

**You observe:** after you save caster settings, the correction step may still
show the previous stream for a short time.

**Why:** the NTRIP client takes the settings over at its next I/O return.

**What to do:** wait; the step follows the saved settings.

## Field App

### The measurement method is a device setting

**You observe:** a measurement method that you chose earlier only in one
browser (5 or 10 samples, "until quality is OK") is not used any more after the
update; the portal measures with the device value, by default the average of 5.

**Why:** since V16.0.0 the method belongs to the device
(`measurement_samples`), so that every browser and every operator measures the
same way. The old browser-only value is deleted from the browser at start and is
not copied to the device, because that would change the method of all browsers
silently.

**What to do:** choose the method once under **Measure -> Field setup**. Recorded
points keep the values they were recorded with.

### The frame reminder is hidden per browser tab session

**You observe:** the reminder that new RTK points would be saved with the frame
"unknown" comes back when you open the portal in a new tab or after closing the
browser, although you hid it before.

**Why:** hiding is kept in the session storage of the tab (`rtk-frame-reminder`),
not on the device, so that a forgotten frame cannot be silenced for good. Without
session storage (a private window with blocked storage) it applies until the
page is reloaded.

**What to do:** set the reference frame under **Measure -> Field setup -> Frame**;
the reminder then stops by itself.

### Some sign-in targets land on Measure

**You observe:** after a session expired, the sign-in page returns to **Measure**
instead of the project you were on.

**Why:** the sign-in target must match a fixed pattern. A project ID with capital
letters or `%` (possible for projects restored from a backup) does not match.

**What to do:** open the project from **Projects**.

### The support pages stay light

**You observe:** `/setup`, `/label` and `/diagnostics` do not turn dark, although
the appearance is Dark or Automatic with a dark phone or computer.

**Why:** until these pages are removed they know only Light and High contrast.
"Automatic" follows the system setting in the Field App only.

**What to do:** nothing; use the Field App for a dark view, or High contrast if
the support page is hard to read in the sun.

### A chosen appearance can switch briefly while a page loads

**You observe:** with Light, Dark or High contrast chosen, the Field App can
appear for a moment in the appearance of the phone or computer when it loads.

**Why:** the choice is stored in the browser and read after the page has started.
Until then the system setting applies. Automatic is not affected.

**What to do:** nothing, or choose Automatic under **Device -> Operation &
display**.

### Cached project views are old until the device has answered

**You observe:** when you return to **Projects**, the **Archive** or a project
map, the last loaded view appears at once with "As of hh:mm:ss, updating ...".
A change made in another browser shows only after the update has finished. If
the device does not answer, the view stays with "Update failed" and "Try again".

**Why:** the views are kept in the memory of the page (at most 6) so that a slow
device does not block the way back. The time is the clock of the browser at the
moment the view was loaded, not a device time. Nothing is stored on disk, so a
reload starts empty. A restore made by another browser that happens to leave the
same map revision, without a device restart, is noticed only at the next delta
or full map fetch.

**What to do:** wait for the stamp to disappear before you rely on a name,
a count or the geometry; use "Try again" after a failure. Cancelling is offered
only while a whole page loads; a section that loads next to a form cannot be
cancelled and ends at the read time limit.

### The GeoJSON export lives in the export sheet

**You observe:** closing the export sheet while a GeoJSON export runs asks
"Cancel export?". When nothing is running, or after the export has ended, the
sheet closes without a question. Leaving the view ends a running export without
a question. A finished file is gone once you discard it, close the sheet or
leave the view.

**Why:** the export belongs to the sheet; the file is kept in the memory of the
browser until then, and a closed sheet cannot show a running export. The portal
can say that it handed the file to the share dialog or the browser, not whether
it was saved. The direct CSV and archive links and the full backup do not detect
a truncated download; a restore checks a backup.

**What to do:** keep the sheet open until the export is ready, then share or
download the file. For CSV and archives compare the size or export again if the
connection broke; test a backup by restoring it.

## Corrections

### A parked NTRIP client does not retry on its own

**You observe:** with a non-zero `ntrip_max_retries`, the client stops
connecting after the limit. `ntrip_state` is `backoff` (or `auth_error` or
`mount_error` when the last attempt was rejected with 401, 404 or a
sourcetable), `ntrip_backoff_sec` is 0, `last_ntrip_error_code` is `retries_exhausted`, and the log says "client
parked until restart or Wi-Fi loss". The measure screen names this state
"NTRIP stopped" together with the restart.

**Why:** this is the intended stop, with no timed probe attempts. The client
resumes after a restart, a configuration apply (which reboots the device), a
change of the caster settings, or a loss of the Wi-Fi connection. A Wi-Fi interruption shorter than about five
seconds can go unnoticed, so the client stays parked.

**What to do:** change the caster settings, restart the device, or apply the configuration again. The key
`ntrip_max_retries` is only settable in `config.json`; the default `0` never
parks. See [technical.md](technical.md), *Failover and backoff*.

### `stale_backlog` in the error code is not a caster failure

`stale_backlog` can appear in `last_ntrip_error_code` (`/status`, `stats`). It
is not a connection state. The client sets it when it closes a stream on
purpose because the correction epochs were stale for longer than
`ntrip_reconnect_on_stale_sec` (`net.py` `NtripMachine.check_stale`; off by
default), and it is cleared when the new connection delivers data. Seeing it
means a backlog was dropped, not that the caster failed. See
[technical.md](technical.md), *Stale stream and backlog reconnect*.

### The receiver keeps RTK FIXED on old corrections

**You observe:** the fix reads RTK FIXED (GGA quality 4), yet the receiver's own
error estimate (GST) is tens of centimetres to a metre, or the accuracy figures
jump after a gap in the correction stream.

**Why:** the LC29H keeps reporting quality 4 for up to 120 s of correction age,
while its own GST error estimate reacts from about 20 s; in recordings made
with V15.7.3 the horizontal sigma went from about
0.01 m to about 0.8 m from a correction age of roughly 20 to 26 s, with the flag
unchanged. The flag alone
is therefore not evidence of quality. The acceptance gate never uses it alone:
`tracking/fixes.py` `quality_check()` requires the GST estimate (`gst_required`) and
compares GGA field 13 with the correction-age limit (`corrections_too_old`,
5 s by default, configurable up to 120 s).

**What to do:** keep the gate at its factory values for survey work and read
the gate verdict, not the fix label. Do not raise
`measurement_max_correction_age_sec` towards 120 s: at that setting the age
rule stops protecting you and only the GST limits remain. A receiver that reports no
correction age at all (empty GGA field 13) passes the age rule, so the GST
limits are the remaining protection there. The measurement checklist then marks
the **Corrections** row as unknown (amber "?", "corrections age unknown")
instead of passed; it does not block measuring. See [technical.md](technical.md),
*Acceptance gate*, and [performance-and-limits.md](performance-and-limits.md),
*Correction ages and link latency*.

### Corrections can arrive late without the link looking broken

**You observe:** corrections keep arriving, the link looks healthy, and yet the
accuracy degrades or the receiver falls back to RTK FLOAT. In the checklist the
**Correction link** line of the Corrections row is amber or red.

**Why:** the arrival age and the epoch age are different things: a stream can
deliver bytes every second while every message in it is minutes old. The
single definition of the three ages (arrival age, epoch age, and the receiver's
correction age) is in [technical.md](technical.md#correction-ages). Recorded
epoch ages reached a median of 55 s and a maximum of 407 s with V15.7.3,
with the arrival age showing the stream as fresh;
the figures are in [performance-and-limits.md](performance-and-limits.md). The
device measures the epoch age from the MSM message epochs (`net.py`
`RtcmCounter.epoch_age_ms`) and flags it as `rtcm.stale` above
`ntrip_max_epoch_age_sec`. The **Correction link** line of the Corrections row
(`src/50-status.js` `correctionLinkState`) is amber while no epoch has been
decoded (invalid clock, or a stream without MSM messages) and from 5 s of epoch
age, red when the device reports `rtcm.stale`; when NTRIP has no endpoint to use
it says that NTRIP is currently not delivering corrections and names the
effective cause (switched off, not set up, waiting for Wi-Fi, connection error
with the last error code, or stopped after repeated failures); it stays
neutral as long as the NTRIP client has not run. The epoch age is a diagnostic: it does not enter
the acceptance gate.

**What to do:** treat an amber or red Correction link line as a reason to wait
or to fix the link, not to measure. Set `ntrip_reconnect_on_stale_sec` in
`config.json` so that a replayed backlog is dropped by a fresh connection, and
prefer a hotspot and a mountpoint that stay fresh. A missing epoch age is not
the same as a stale one: it can mean that the device clock is not set.

### The epoch age drifts with the device clock

**You observe:** the epoch age shows a steady offset, or creeps upward over a
long session, although the corrections are current; or the Correction link line
says "No epoch decoded yet" because the clock is invalid.

**Why:** the epoch age is the difference between the epoch inside an MSM
message and the device clock, which has a resolution of one second. The clock
is set from the receiver's RMC sentence while it is still invalid, re-stepped
from RMC when it deviates by more than 1 s and checked against RMC once an hour
(`gnss.py` `_sync_rmc_clock`), and set by NTP when the Wi-Fi station connects
or reconnects (`net.py` `_zeit_holen`, if `ntp_sync` is on; the sync is
deferred while a recording runs). Between steps the
oscillator's drift (estimated at 0.5 to 1 s per day for this kind of board; an
estimate, not a measurement of this project) adds to the epoch age, but it stays
within about a second of GNSS time while RMC is received. The leap-second count
used to convert GPS time is `gps_utc_offset_sec` (18 by default); a stale value
shifts the epoch age by whole seconds.

**What to do:** read an epoch age of a few seconds as "fresh"
rather than as a measurement. With NTP disabled and no Wi-Fi, the clock comes
only from the receiver and the age is valid only after the first RMC sentence
with a date.

### The receiver and the base do not share all signals

**You observe:** the correction stream lists satellites of every system, yet
only some of them contribute to a dual-frequency RTK solution, so fixing can be
slower or less robust than the number of tracked satellites suggests.

**Why:** the LC29H(DA) tracks GPS L1 and L5, Galileo E1 and E5a, BeiDou B1I and
B2a, and GLONASS L1 only; it has no L2. A base station whose stream is
MSM5 (1075, 1085, 1095, 1125) with GPS signals 1C, 1W, 2W, 2L and 5Q, Galileo
1C, 7Q, 8Q and 5Q, BeiDou 2I, 6I and 7I (no B2a), and GLONASS 1C, 1P, 2C and 2P
therefore gives the rover dual-frequency RTK only for GPS and Galileo: BeiDou
matches on B1I only, and GLONASS on L1 only. This comes from analysing one
service's stream in the V15.7.3 recordings; your mountpoint may differ. The
firmware does not choose signals; the `rtcm` section of the field log lists the
signal masks the stream actually carries (`field_diagnostics.py`).

**What to do:** prefer a mountpoint that carries L5, E5a and B2a signals, and
check the mask in a field-diagnostics recording before relying on a new
mountpoint. See [field-diagnostics.md](field-diagnostics.md).

### Accuracy depends on the distance to the base

**You observe:** a position that is fixed and passes the gate, yet differs by
centimetres from a known point when the base is far away.

**Why:** RTK accuracy degrades with the baseline, by about 1 ppm per km (1 mm
for each kilometre) on top of the accuracy at zero distance. This is general
RTK behaviour, not something measured on this device. The firmware does not
know the baseline length and does not check it; the acceptance gate has no
distance rule.

**What to do:** with a physical base station, know its distance and add 1 mm
per km to your error budget; with a network mountpoint, check that the service
generates a virtual station near the rover from the position the device sends
(`ntrip_gga_interval_sec`, `net.py` `_sende_gga`).

## Position, heights and frames

### The device clock can step back and forward by a few seconds

**You observe:** the log shows pairs such as `Clock stepped by -7 s from GNSS
UTC (drift)` followed about a second later by `Clock stepped by +2 s`, mostly
in the first two minutes after a start and now and then in operation. Times
recorded around such a pair (`recorded_at`, event times) can be off by a few
seconds, and the correction age can be missing for a moment after each step.

**Why:** the clock is set from the receiver's RMC sentence whenever it differs
from the device clock by more than one second; an RMC sentence that is read
late, for example while the receiver input backs up during start-up or storage
work, carries an old second and moves the clock back until the next fresh
sentence moves it forward again (`gnss.py`). Durations use the tick counter
and are not affected.

**What to do:** treat recorded times as accurate to a few seconds, not to the
second. The measurement position, its quality values and the order of points
are not affected.

### Points recorded before V15.9.0 are on a coarse grid

**You observe:** a point from an older project shows seven decimals instead of
eight and carries a legacy badge, its position can differ from a new measurement
of the same spot by up to about 0.2 to 0.3 m, and its height, geoid separation, antenna
height, frame and mountpoint fields are empty. Its height above sea level
stays, as an approximate value. In exports its `precision` reads
`legacy_float32`.

**Why:** firmware before V15.9.0 parsed and stored positions as 32-bit floats,
which resolve a latitude only to a grid of the order of 0.4 m. Schema 5 stores
exact integers, but the upgrade can only convert the integers that were stored;
it cannot restore what the float had already lost (`geo.py`
`legacy_coordinate_to_e8`; `tracking/validation.py` `_upgrade_legacy_line` and
`_upgrade_legacy_point`, which tag the values `legacy_float32`). The fields the
old schema never held are absent or `null` in the stored data and in a
backup, and the exports show `null` or empty cells and the frame `unknown`. The
height that old firmware stored is kept: it is the MSL height of the antenna
phase centre, as approximate as the position, so a legacy point has no
ellipsoidal height and no ground height. Files written
by V15.9.0 and later (schema 5) are not read correctly by older firmware; see
the upgrade notes of V16.0.0 in the changelog.

**What to do:** treat a `legacy_float32` coordinate as approximate and
re-measure the points whose position matters. Do not average a legacy point
with new measurements as if it were exact. Back up before downgrading
firmware. See [integration.md](integration.md), *Survey coordinates and
heights*.

### The stored height is the antenna's height above mean sea level, not the ground's

**You observe:** the height of a point is higher than the ground it was
measured on by about the antenna height, and it can differ from a height from
another source by the difference between two geoid models.

**Why:** the stored height is GGA field 9, the height of the antenna phase
centre above mean sea level according to the receiver's own geoid model; the
firmware does not know which model that is and applies none of its own
(`gnss.py` `parse_gga`). The geoid separation (GGA field 11) is stored next to
it, the ellipsoidal height is their sum (`altitude_ellipsoid_m`), and the
antenna height from the field setup is stored per point; the ground height
(`ground_height_msl_m`) is derived only where an antenna height was recorded.
The antenna height is the distance from the ground point to the phase centre,
pole length plus the antenna's own offset, and a wrong value is a wrong ground
height. The GeoJSON `[lon, lat, alt]` carries the MSL height of the phase
centre where RFC 7946 expects an ellipsoidal height. The antenna and the ground
plane are part of the measuring system and affect the vertical accuracy most;
a good GST value does not replace a check on a known point.

**What to do:** set the antenna height in the field setup before measuring,
read `altitude_ellipsoid_m` where an ellipsoidal height is needed, and convert
to an official height system with the geoid model that system prescribes. Keep
the antenna, the ground plane, and the mount unchanged between the control
check on a known point and the survey, record the ground-plane diameter, the
antenna reference height, and the mount, and keep the pole vertical. See
[technical.md](technical.md), *Coordinates and heights* and *Field setup*.

### RTK and other positions are in different frames

**You observe:** points measured with RTK and points measured without
corrections (or with DGPS or SBAS) in the same project do not line up, by
roughly 0.7 to 0.9 m in the case that was analysed.

**Why:** RTK positions refer to the frame of the correction service, typically
a regional frame of the ETRS89 type, while other fixes are in WGS84/ITRF at the
time of observation; the two drift apart over the years. The firmware does not
transform anything: it stamps each point with the frame the operator
configured for RTK fixes (fix quality 4 or 5), `WGS84/ITRF` for any other
nonzero quality, and nothing where none was configured (`tracking/fixes.py`
`_reference_frame`). A gate profile that accepts DGPS therefore lets both kinds
of point into one project, and a line export then reports `mixed`. The label
is the operator's statement, not a verified fact. The GeoJSON is nominally
WGS84, because the format prescribes it, but its coordinates are those in the
frame of each point; the file's `crs_note` says so (`tracking/frames.py` `CRS_NOTE`).

**What to do:** declare the frame of your correction service in the field setup
before measuring, keep one frame per project, and do not mix RTK and non-RTK
points in a survey. RTK points that were recorded without a frame can be given
one afterwards in the project (from the frame notice of the project); the assignment can
be undone. Transform to the target frame in your GIS. See
[technical.md](technical.md), *Reference frames*.

### Frame suggestions need RTCM 1300 from the correction service

**You observe:** the field setup never offers a reference frame, although the
service is running and corrections arrive.

**Why:** the device can only read the frame from an RTCM 1300 message (service
CRS) in the correction stream. The caster in use today sends none, and the
sourcetable is not evaluated, because a second request costs a connection and
heap for tables of more than 50 KB and carries the frame only as free text. The
announced epoch is accepted from 1988.00 to 2100.00; a value outside that range
or zero gives a suggestion without an epoch. The suggestion belongs to the open
stream: it disappears at every end of the stream, at a change of the caster and
when NTRIP is switched off, and returns only with the next 1300 message. A frame
name that the field setup would refuse (non-ASCII, empty, `unknown`, `mixed`) is
never offered. Whether a frame is plate fixed or tied to ITRF at an epoch
matters for the coordinates: a service tied to ITRF moves against a plate fixed
frame by centimetres per year, so the epoch should be checked before accepting
(see the epoch notice of the field setup, where it exists).

**What to do:** declare the frame in the field setup by hand as before; the
suggestion is only a convenience and is never applied without your confirmation.
See [technical.md](technical.md), *Field setup* and *Service CRS*.

### A frame assignment covers only the points present when it was made

**You observe:** after assigning a frame to a project, points measured later with an
empty field setup are `unknown` again, and points without an RTK solution never
take the frame.

**Why:** the assignment names a count of the project's RTK points without a
frame at that moment. It does not change stored measurements and does not
transform coordinates. Points of other fix qualities are not RTK solutions, so
they stay `unknown`. Firmware older than the assignment ignores it and shows
`unknown` after a downgrade. A GeoJSON or backup download stops incomplete when
the project's assignment is changed, or an assigned point is undone, while it
runs.

**What to do:** declare the frame in the field setup before measuring, or assign
it again when you are done. Download again if an export stopped.

### Reference frame epoch

**You observe:** the field setup asks for a frame epoch, and it is unclear
whether to enter one.

**Why:** the epoch is a statement by the operator that the device cannot check.
It means different things for different services.

**What to do:** for a plate-fixed frame such as ETRS89, enter the epoch that your
correction service states for its coordinates. With an ITRF-based service,
leave it empty: the coordinates then refer to the time of observation, which
every point stores as `recorded_at_utc`. See [technical.md](technical.md),
*Reference frames*.

### Recorded times count from the year 2000

**You observe:** a stored `recorded_at` such as `841057764` is not a Unix time,
and points recorded before the clock was set carry a date in the year 2000.

**Why:** the device counts seconds from 2000-01-01 on every platform, and the
clock is set only after NTP or the receiver's time arrives (`tracking/common.py`
`_utc_fields`). Exports add `recorded_at_utc` and `clock_unsynced` to make
this visible.

**What to do:** read `recorded_at_utc` and ignore points with
`clock_unsynced` set as to their date. The conversion to Unix time is in
[integration.md](integration.md), *Timestamps*.

### Distances and line lengths use a local ellipsoidal scale

**You observe:** lengths, chainage and offsets are exact on a local scale but
are not geodesics over long distances; older lengths differ slightly.

**Why:** since V15.9.0 distances use WGS84 local scale factors at the mean
latitude of the two positions, accurate to a few millimetres within 10 km
(`geo.py` `local_scale`, `offsets_e8_m`, `distance_e8_m`). Earlier versions
used a 6371 km sphere, which differs by a few tenths of a percent depending on
latitude and direction (for example about 0.25 % east-west at 40° latitude, 0.3
to 0.4 % at 50° to 60°, and up to about 0.6 % north-south near the equator). The upgrade rebuilds the cached line lengths from
the stored coordinates (`_upgrade_legacy_line` drops the old summary), but
chainage and lateral offset already stored with older object points keep the
values of the former model.

**What to do:** do not use the device's distances for lines longer than a few
kilometres as geodetic lengths, and expect legacy chainage values to differ by
that order of magnitude from newly computed ones.

## Capacity and performance

### The active point limit is 5000, not the 10000 the schema targets

**You observe:** the capacity display warns and finally stops at 5000 active
features, although the documentation describes a 10000-point design.

**Why:** 10000 is the value the compact schema and the warning thresholds were
built for; 5000 is what the firmware enforces, because the full acceptance run
under simultaneous GNSS, NTRIP, Wi-Fi, BLE, TCP and portal load has not been
completed at the higher level (`tracking/common.py`).

**What to do:** archive completed projects. Archives are not loaded into RAM at
start-up and do not count against the active limit, so a device can hold far
more total survey data than the active limit suggests. The reasoning is in
[performance-and-limits.md](performance-and-limits.md), *Point capacity*.

### Connection setup can stall for seconds

**You observe:** an occasional request to the device takes several seconds
before anything happens, even though the device is idle and the same request
usually completes immediately. Switching between projects in the portal can
take noticeably longer than the map response itself.

**Why:** the delay occurs during TCP connection setup, before the HTTP request
is sent, with SYN retransmissions visible on the client side. It has been
reproduced from a plain socket client as well as from a browser, so it is not a
browser artefact — the cause in the network path is not established.

**What to do:** size client timeouts so that they cover connection setup and
not just the response, and do not treat a single slow request as a device
fault. Because responses are HTTP/1.0 with `Connection: close`, every request
pays this cost, so prefer few large requests to many small ones. See
[performance-and-limits.md](performance-and-limits.md), *Concurrent clients and
connections*.

The device reports its event-loop lag (`stats.loop_lag_max_ms` in `/status`,
`loop_lag_max_ms` in each field-diagnostics sample) and the duration of each
NTRIP name resolution and connect. If a slow connection has no event-loop lag at
the same time, that argues against a long synchronous block of the event loop;
other causes remain open and are still being measured.

The field app keeps one event stream open on the measure and device pages
instead of polling, so there it sets up hardly any connections and meets this
delay mainly on page loads and actions. A tab that has fallen back to polling
still pays it on every poll, and so do the stakeout pages, which close the
stream and ask for the stakeout packet once a second with a 3 s timeout: a
stall longer than that greys the guidance until the next answer arrives.

### A fourth browser tab falls back to polling

**You observe:** with four tabs of the field app open, the oldest one stops
receiving a live stream and updates only every 2 s or 5 s, like before.

**Why:** at most three event streams are active; a fourth evicts the oldest
(`bye` with `evicted`), and that tab does not try again for 5 minutes so that
tabs do not take the stream from each other (`events.py`). Beyond four open
streams, including evicted ones still closing, the device answers
`503 events_busy`.

**What to do:** keep at most three tabs of the portal open; close the others.

### A stream that stops reading is dropped after 5 s

**You observe:** a phone that leaves the Wi-Fi or a frozen browser tab loses its
event stream; the app polls instead and tries the stream again after 30 s.

**Why:** a client that does not accept data within the 5 s write budget would
otherwise hold a stream for ever. The device ends that stream only
(`sse_write_timeouts` in `/status`).

**What to do:** nothing.

### Sign-out ends an open stream within a second, or within 5 s when the client has stopped reading

**You observe:** after you sign out in one tab, the stream of another tab ends
within about a second; with a client that does not read, it can take up to 5 s.

**Why:** the stream checks the session once per second and shares one 5 s write
budget between two checks (`events.py`). Rotating the NMEA stream token does not
end it.

**What to do:** nothing.

### An open measurement still polls its progress every 650 ms

**You observe:** while a measurement dialog is open, the app opens a connection
about every 650 ms although the event stream is open.

**Why:** the stream carries the live snapshot, `heartbeat` and `bye`, but no
measurement progress. During the measurement the app ignores the stream's `live`
frames so that the progress poll stays the only source.

**What to do:** nothing; the connection count returns to normal after the
measurement.

### Repeated warnings are summarised in the log

**You observe:** the error log shows a line such as `----- 40 more WARN HTTP lines suppressed (rate limit)`, or a warning that ends with `[N more since the last report]`, and not one line for every event.

**Why:** the code that reports a frequent fault (NMEA-TCP send failures, unexpected NTRIP errors, rejected HTTP requests, NMEA queue overflows) writes only the first occurrence and then at most one line per minute and condition; separately, the logger lets each severity and tag put 6 lines into flash as a burst and then one every 10 seconds, and replaces the rest with a summary line.

**What to do:** read the totals from `/status` (`tcp_write_errors`, `tcp_write_timeouts`, `nmea_tcp_drops`, `queue_overflows`, `http_rejected_requests`, `ntrip_retries`). Individual suppressed lines can be read in `/errors` only while they are still in the RTC history (about 1.4 to 1.9 KB, older lines are pushed out, and a power cut clears it). Details in [technical.md](technical.md), *Persistent error log*.

### A hanging storage maintenance pauses the task watchdog

**You observe:** survey actions keep answering `409` or `503 storage_busy`
long after a restore, archiving, compaction or background checkpoint should
have finished, and the device does not restart on its own even if GNSS, NTRIP
or network work stops in the meantime.

**Why:** while a storage maintenance is registered, the watchdog's heartbeat
check is paused so that a long restore or compaction is not mistaken for a
stalled task (see [technical.md](technical.md), *Tasks*). If a maintenance
hangs, for example because it waits for a reader (an export, a map or line
read) that is never released, the check stays paused for as long as the
maintenance stays registered. The hardware watchdog still resets the board when
the event loop itself is blocked; it does not see a single stalled task during
that time.

**What to do:** restart the device; a restart releases the hanging maintenance
and the reader.

### Gate profiles from earlier versions may be listed twice

Up to V15.9.0 a device derived a gate profile's ID from its limits in the
key order of MicroPython's dictionaries, while a host sorted the keys. The same
limits could therefore sit under two IDs in a device's profile table, and a
host restore of a device backup would have mapped measurements onto the other
ID. Stored IDs are kept as they are (nothing is rewritten, and the limits of
both entries are identical, so no measurement changes). New measurements and
restores reuse the profile that already holds their limits, so the duplicate
does not grow; it only appears as two equal entries in the backup header.
A stored ID is accepted by its shape (`profile-` plus eight hex digits), so a
doctored ID of that shape is not detected; the limits are the authoritative data.

## Web interface

### The 48 px touch targets crowd the project map

**You observe:** on a small phone the map tools take three rows of 48 px above the
map. Where points lie on top of each other, the 48 px touch area of the later
point covers the earlier one, so the earlier point cannot be tapped on the map.
A pan gesture cannot start on the 48 px corridor around a line.

**Why:** every map point and line has a touch area of at least 48 px so that it
can be hit with gloves, and the areas are not merged or reordered.

**What to do:** open stacked points from the point list, which reaches every
point. Start a pan beside the line instead of on it.

### On a short screen the action bar covers the readiness card

**You observe:** on a short phone screen such as 320 × 480, the action bar of the
measuring screen covers the title of the readiness card until you scroll the
card into view.

**Why:** the action bar stays above the navigation so that the measuring actions
are always in reach; it is 134 px high without messages at 320 × 480 and grows
by up to two capture messages.

**What to do:** scroll the readiness card into view; the actions stay usable
while you scroll.

## Field behaviour that is not yet verified

The entries in this section are gaps in evidence. The code paths exist and
behave as described in controlled tests; what is missing is confirmation under
real field conditions.

### Return to a phone hotspot after an outage

**Status: not yet verified in the field.**

**You observe:** when the configured hotspot disappears, the device falls over
to another stored network and corrections resume. What happens when the hotspot
comes back has not been confirmed.

**Why:** the network manager tries visible networks in the stored priority
order and keeps a working connection rather than forcing a return to a
higher-priority network that has become visible again (`net.py`). Every
observed outage so far ended with a fallback network in range, so the return
path itself was never exercised.

**What to do:** if you depend on the hotspot specifically, verify the return
yourself out of range of any stored fallback network, or restart the network
connection after the hotspot is back. Do not assume an automatic switch back.

### A cold power interruption of the whole device

**Status: not yet verified in the field for the current start-up path.**

**You observe:** nothing unusual is expected — survey data is committed to
flash as it is recorded, and a restart resumes a recording line.

**Why:** the current start-up path, which brings up Wi-Fi, BLE, HTTP and the
correction stream before the survey storage is recovered, has been verified
with planned restarts rather than with a real power interruption. Recovery
after an interrupted write is covered by tests and by
[technical.md](technical.md), *Recovery after a power failure*; for a restore
the tests cut the power before every write on the host and before every
block write on a LittleFS image, but not yet on the device itself.

**What to do:** after an unplanned power loss, check that the portal reports
the survey storage as ready and that the point count matches what you expect
before recording further. A power-cycle restart is visible as the reset cause
`PWRON` on **Device** and under **Device -> Diagnostics**.

### Power supply

**You observe:** unexplained restarts, or a device that is simply off when you
come back to it.

**Why:** this is a hardware property, not a firmware one. A receiver of this
kind draws little current, and many portable power banks switch themselves off
below a current threshold; separately, simultaneous Wi-Fi and BLE transmit
peaks on a marginal supply can pull the rail low enough to reset the board. The
firmware cannot distinguish either case from any other power-on.

**What to do:** use a supply that stays on under a small constant load and can
deliver the peaks, and check the reset cause and the boot counter on **Device**
or under **Device -> Diagnostics** after an unexpected restart. Repeated `PWRON` entries point at the
supply or the cable, not at the firmware.

## Prerequisites and safeguards, not limitations

Two things that are often reported as defects are working as designed. They are
listed here so that the distinction is on the record.

### GST is required, and the firmware keeps it enabled

The acceptance gate needs the receiver's own error estimate, so a measurement
without a GST sentence is rejected with the reason `gst_required`. The receiver
does not keep its GST output setting across a power cycle, and the firmware
therefore re-sends the enable command whenever no GST has been seen for five
seconds, backing off over 2, 5, 15 and 60 s, and resets the attempt counter as
soon as GST arrives (`gnss.py`). GST is also in the NMEA whitelist, so BLE and
TCP clients receive it too (`cfg.py`).

A receiver that never starts emitting GST will block every measurement, and
that is intentional — but it is visible rather than silent: `gst_output_state`
and `gst_enable_attempts` are published in the live status, and the state is
shown in the **GST output** row of the checklist and in *Receiver details*. If
measurements are rejected for `gst_required`, look at those two fields first. The gate rules and
their configurable bounds are in [technical.md](technical.md), *Acceptance
gate*.

### Receiver commands wait for the end of a correction frame

Commands to the receiver (the GST re-enable, the module console, BLE) are never
written into the middle of an RTCM correction frame; they wait for the end of
the frame. The frame boundary is derived only from the length fields of the
frames actually written, without checking the CRC, so after a bit error in a
length field it can be wrong for up to 1029 bytes. A command that has not been
able to start within 5 s is dropped: the GST re-enable is repeated after its
next back-off step, and the module console answers `503`. When a caster
connection ends, the frame in progress is cut off on the wire, and a command
that follows at once may be swallowed by the receiver as the rest of that
frame; the console then reports "sent" without a reply. A blocking call into
the UART driver is not limited by the write deadlines (1 s without progress,
2 s in total); only the hardware watchdog bounds it.

### Automatic recording waits for the receiver to settle

Automatic route vertices are refused after a start until the device clock is
set and the receiver has delivered 10 s of continuous usable positions; a gap
of more than 3 s or an implied speed above 60 m/s resets the wait
(`gnss.py` `RouteStartupGuard`). Once that guard is satisfied, route mode
stores every automatic point regardless of the acceptance-gate verdict — a
quality-rejected sample is kept with its rejection reasons rather than
discarded (`tracking/core.py` `allow_rejected`). Non-route automatic recording
applies the full acceptance gate to every sample instead (`tracking/core.py`).
Either way, a line stays in the `recording` state while it waits and the
portal shows the reason, so the pause before the first vertex is expected
behaviour.

Honest limit: the guard filters the start-up transient, not every positioning
error. Slow RTK convergence and genuine slow movement cannot be told apart from
consecutive positions, so it is a safeguard against a cold-start jump and not a
guarantee of correctness for every recorded vertex. It also does not touch data
that is already stored: outliers recorded before it existed are still in the
survey data.

## Related documents

- [performance-and-limits.md](performance-and-limits.md) - the measured side of these limits
- [transport-security.md](transport-security.md) - what the transports do and do not protect
- [technical.md](technical.md) - the mechanisms behind each entry
- [onboarding.md](onboarding.md) - setup, acceptance and reset
- [../SECURITY.md](../SECURITY.md) - how to report a security finding
- [../CONTRIBUTING.md](../CONTRIBUTING.md) - how to propose a change
