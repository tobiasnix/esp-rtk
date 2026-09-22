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
in `tracking.py` and `pointstore.py`).

**What to do:** treat a factory reset as a credential reset, not a data wipe.
Delete the projects in the portal before handing a device on. Field-diagnostic
recordings cannot be deleted from the portal at all — the recorder's endpoints
only start, stop, mark and export it — so they leave only through the
recorder's own oldest-first rotation. To be certain a device carries no
positions, erase the filesystem or reflash it. See
[onboarding.md](onboarding.md), *Reset*, and [technical.md](technical.md),
*Factory reset*.

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

### The `/rtcm` rejection advertises a token that nothing accepts

**You observe:** an unauthenticated request to `/rtcm` is answered
`401 Unauthorized` with the text `Token required: /rtcm?token=...`, but adding
a `token` parameter changes nothing.

**Why:** the message is left over from an older access model; the only
authorization the endpoint actually performs today is the ordinary
administrator session, and the two token helpers that remain in the source are
no longer called from any route (`web.py`).

**What to do:** ignore the hint and sign in. `status_token` and `config_token`
are obsolete and unused by every endpoint; they are cleared only by a factory
reset, so a value from an old configuration otherwise persists across a
firmware update.

### TCP NMEA authentication has no total deadline

**You observe:** slow clients that never finish the `AUTH` handshake can
occupy every connection slot, and new clients are dropped.

**Why:** the five-second timeout is applied to each byte read rather than to
the handshake as a whole, and a connection that is still authenticating already
counts against `nmea_tcp_max_clients` (`fanout.py`).

**What to do:** keep the TCP fan-out on a trusted network, keep the client
count low, and rotate the stream token
(`POST /api/stream-token/rotate`) if you need to clear the current clients. See
[transport-security.md](transport-security.md) and
[integration.md](integration.md).

## Corrections

### The NTRIP retry limit does not stop reconnecting

**You observe:** with a non-zero `ntrip_max_retries`, the client does not park
itself when the limit is reached — it keeps reconnecting indefinitely. The
status codes `retries_exhausted`, `caster_error` and `protocol_error` never
appear anywhere.

**Why:** reaching the limit sets an NTRIP state that the state validator does
not accept, so the task raises instead of parking; the supervisor restarts it
and the retry counter starts from zero again (`net.py`, `state.py`,
`main.py`). The same validator rejects the two transient error states before
they can be recorded, which is why only `auth_error` and `mount_error` are ever
stored.

**What to do:** treat `ntrip_max_retries` as having no stopping effect and use
the NTRIP enable switch if you genuinely want the client to stop. To diagnose a
caster problem, read the log or `/errors` rather than `ntrip_state`: the status
line handling and the failover behaviour that *are* observable are described in
[technical.md](technical.md), *NTRIP*.

## Capacity and performance

### The active point limit is 5000, not the 10000 the schema targets

**You observe:** the capacity display warns and finally stops at 5000 active
features, although the documentation describes a 10000-point design.

**Why:** 10000 is the value the compact schema and the warning thresholds were
built for; 5000 is what the firmware enforces, because the full acceptance run
under simultaneous GNSS, NTRIP, Wi-Fi, BLE, TCP and portal load has not been
completed at the higher level (`tracking.py`).

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
[technical.md](technical.md), *Recovery after a power failure*.

**What to do:** after an unplanned power loss, check that the portal reports
the survey storage as ready and that the point count matches what you expect
before recording further. A power-cycle restart is visible as the reset cause
`PWRON` on the device status page.

### Power supply

**You observe:** unexplained restarts, or a device that is simply off when you
come back to it.

**Why:** this is a hardware property, not a firmware one. A receiver of this
kind draws little current, and many portable power banks switch themselves off
below a current threshold; separately, simultaneous Wi-Fi and BLE transmit
peaks on a marginal supply can pull the rail low enough to reset the board. The
firmware cannot distinguish either case from any other power-on.

**What to do:** use a supply that stays on under a small constant load and can
deliver the peaks, and check the reset cause and the boot counter on the device
status page after an unexpected restart. Repeated `PWRON` entries point at the
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
and `gst_enable_attempts` are published in the live status. If measurements are
rejected for `gst_required`, look at those two fields first. The gate rules and
their configurable bounds are in [technical.md](technical.md), *Acceptance
gate*.

### Automatic recording waits for the receiver to settle

Automatic route vertices are refused after a start until the device clock is
set and the receiver has delivered 10 s of continuous usable positions; a gap
of more than 3 s or an implied speed above 60 m/s resets the wait
(`gnss.py` `RouteStartupGuard`). Once that guard is satisfied, route mode
stores every automatic point regardless of the acceptance-gate verdict — a
quality-rejected sample is kept with its rejection reasons rather than
discarded (`tracking.py` `allow_rejected`). Non-route automatic recording
applies the full acceptance gate to every sample instead (`tracking.py`).
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
