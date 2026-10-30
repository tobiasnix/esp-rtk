# Set the device up and accept it

This checklist is for the person who prepares a new or reset device for field
work. It assumes the firmware is already on the board; flashing, wiring, and
the first quick start are in [../README.md](../README.md).

## Prepare

Device ID, hostname, device code, BLE passkey, setup network name, and stream
token belong in a protected device record. All of them are served only to an
authenticated session, by the label page `/label` and by `GET /api/label`
(`web.py`). The label page renders two QR codes: one for joining the setup
access point and one for opening the portal (`wifi_qr_payload()`,
`portal_qr_payload()` in `web.py`).

Neither the QR codes nor a configuration backup may be published. A backup
from `GET /api/config/backup` contains the access-point password and the
device code in clear text, next to the stored Wi-Fi and NTRIP credentials
(see [technical.md](technical.md), *Configuration backup*).

## Set up

1. Switch the device on. If the setup network is not visible, press RST and
   then hold the **BOOT button for three seconds** (`SETUP_MODE_HOLD_MS` in
   `cfg.py`). The button must be pressed *after* power is applied: holding it
   while power arrives starts the ROM bootloader, and the firmware never runs.
2. Join the setup access point. It is named after the device, for example
   `RTK-A1B2C3-SETUP`, and is protected with WPA/WPA2-PSK and a random
   device-specific password (`net.py`, `cfg_factory.py`).
3. Most phones open the sign-in page by themselves (captive portal) and show
   **Device -> Connections -> Internet & corrections** after signing in.
   Otherwise open `http://192.168.4.1/#device/configuration` (an old link that
   the portal forwards to that page) and sign in with the device code from the
   label.
4. Under **Device -> Connections -> Internet & corrections**, enter the primary Wi-Fi network, any
   fallback networks (one `SSID` or `SSID:password` per line), and the NTRIP
   caster host, port, mountpoint, and credentials. A caster host looks like
   `caster.example.net`.
5. Choose **Check changes**. The portal lists what you changed, what takes
   effect at once (caster settings), what restarts the device (Wi-Fi), and what
   already waits in the device memory from an earlier save. Nothing is sent
   before you confirm.
6. Choose **Save and restart**. A progress panel shows the steps HTTP, Wi-Fi,
   NTRIP and survey storage. The Wi-Fi settings are held back until the
   restart, and the device keeps them only if it joins the network; otherwise
   it restores the previous ones. After the restart, sign in again with the
   device code (sessions do not survive a restart); the panel continues and ends
   with the result: the connected network and the portal address, or the cause of
   a failure. If there is no answer after 90 s, put the phone into the network
   the panel names and open `http://rtk-a1b2c3.local/` (or the current IP address
   if `.local` is not resolved).
7. Open **Check field setup** and then **Measure**, and confirm that the receiver
   reports a fix, that the correction stream is live, and that the solution
   reaches **RTK FIXED** under a clear sky.

## Acceptance before field work

- The label and both QR codes work.
- Sign-in, sign-out, and sign-in again work.
- German and English, and the appearance (Automatic, Light, Dark and High
  contrast), are usable (**Device -> Operation & display**).
- BLE pairs with the six-digit passkey, the link is encrypted and bonded, and
  NMEA arrives.
- TCP port 10110 asks for the `AUTH <stream token>` handshake by default and
  delivers NMEA afterwards.
- Switching TCP authentication off for a test (**Device -> Connections ->
  Device access**) makes a new connection start with NMEA immediately; switch
  it back on afterwards.
- A test project with one line and one asset point can be created, displayed,
  exported, and deleted again.
- **Device -> Diagnostics** and **Device -> Diagnostics -> Log** show no
  unexplained errors, and the free RAM on **Device -> Diagnostics** and the filesystem figures on
  the **Device** overview are plausible.
- **Measure -> Field setup** holds the measurement method, the measurement
  rules, the antenna height, the reference frame and its epoch; a change shows
  in a second browser, too. With an RTK fix and no reference frame, **Measure**
  reminds you before you record. If the correction service announces its frame
  (RTCM 1300), the field setup offers it as a suggestion: confirm it with
  **Use reported frame** or dismiss it; nothing changes until you confirm.
- A restart keeps the configuration and the projects.
- A test point shows a running state and its result in the action bar of the
  measure screen, and **Undo** removes it within 6 seconds.
- With NTRIP switched off, the measure screen and the header say so and offer
  the NTRIP settings.

Bluetooth Classic and SPP are not part of the acceptance: the ESP32-S3
supports Bluetooth LE only.

## Reset

A factory reset is triggered by holding **BOOT for ten seconds**
(`FACTORY_RESET_HOLD_MS` in `cfg.py`). It clears both Wi-Fi credential fields,
the configured network list, both NTRIP user and password pairs, and the two
obsolete tokens; it rotates the stream token, and it deletes the BLE bonds,
the staged configuration files, and the survey event journal.

Three things it does **not** do:

- It does not delete the point store or the archived projects. Measured points
  survive a factory reset; delete the projects in the portal before handing a
  device on (see [known-limitations.md](known-limitations.md)).
- It does not delete the field-diagnostics recordings, `field-diagnostics-*.jsonl`
  and `field-diagnostics.state` (`field_diagnostics.py`), which also contain
  measured positions. The portal offers no way to delete or export-then-delete
  them: the field-diagnostics endpoints only start, stop, and mark the
  recorder, or export it (`web.py`). The only way recordings leave the device
  on their own is the recorder's oldest-first rotation, once 32 segments of
  128 KiB exist or sooner if the flash reserve is tight, so existing
  recordings can still be present when a device is handed on. To be sure
  they are gone, erase the filesystem or reflash the device (see
  [known-limitations.md](known-limitations.md)).
- It does not change the device identity or the access-point password, so the
  printed label, the device code, and both QR codes stay valid.

After a reset, the full setup and acceptance above are required again. The
details of what is cleared are in [technical.md](technical.md),
*Factory reset*.

## Related documents

- [../README.md](../README.md) - hardware, install, field use
- [technical.md](technical.md) - configuration keys and HTTP API
- [transport-security.md](transport-security.md) - what the transports protect
- [field-diagnostics.md](field-diagnostics.md) - the recorder for a controlled test
- [known-limitations.md](known-limitations.md) - what is known to be rough
