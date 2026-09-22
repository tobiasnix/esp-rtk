# Transport and security model

This document states what each transport of the device protects, and what it
does not. The endpoint table and the configuration keys are in
[technical.md](technical.md); this one explains the trust boundary.

The short version: the device protects **who may administer it** and **who may
read its position stream**, using secrets printed on its own label. It does
not protect the network path itself, because it speaks plain HTTP.

## The web portal speaks HTTP, not HTTPS

The portal listens on `http_port` (80) on both interfaces, over plain HTTP.
There is no HTTPS and no plan for one on the device itself: a receiver with no
globally resolvable name and no certificate authority on a local network
cannot obtain a certificate that a browser accepts.

The consequences are real and must be accepted before the device is used:

- Anyone who can observe the local network sees the portal traffic, including
  the device code while it is being submitted and the session cookie.
- Anyone who can modify that traffic can modify portal pages.
- The setup access point is the one interface where this is bounded, because
  joining it already requires the WPA password from the label.

Use the portal on a network you trust - the setup access point, a field
hotspot you control, or a private LAN - and treat a shared or public Wi-Fi as
unsuitable for administration.

## Sessions and the device code

There is exactly one portal credential: the **device code** on the label, 12
hexadecimal characters from 48 bits of hardware randomness, stored in NVS
where available. `POST /api/session` exchanges it for a session; the
comparison is constant-time (`access.py`).

| Property | Value | Defined in |
|---|---|---|
| Session cookie | `rtk_session`, `HttpOnly`, `SameSite=Strict`, `Path=/` | `web.py` |
| Idle expiry | 30 minutes | `access.py` `SESSION_IDLE_SEC` |
| Absolute expiry | 8 hours | `access.py` `SESSION_MAX_SEC` |
| Failed sign-ins tolerated | 5 within 10 minutes, per client address | `access.py` `FAIL_LIMIT`, `FAIL_WINDOW_SEC` |
| Block after that | 60 seconds, answered `429` | `access.py` `BLOCK_SEC` |

Sessions live in RAM only and do not survive a restart. Session ages are
measured with a monotonic tick counter, so an NTP step cannot expire a session
retroactively. The session cookie and the CSRF token never appear in a URL.
The one credential that does is the device code, which the portal QR code
encodes as `http://<hostname>.local/setup#code=<device code>` (`web.py`); the
web app reads it from the URL **fragment**, not a query string, so browsers
neither send it to the server nor place it in a `Referer` header. This is why
the label and its QR codes must not be published (see
[onboarding.md](onboarding.md)).

State-changing requests additionally need the CSRF token from
`GET /api/session` in the `X-CSRF-Token` header - the one HTML form endpoint,
`/save`, also accepts it as a field - and an `Origin` header that matches the
request host when the browser sends one. `Origin: null` is accepted only on
the setup access point, only while the access state is `SETUP`, `RECOVERY`, or
`APPLYING`, and only for the AP address or the device hostname, because some
browsers send `null` for local HTTP pages.

The same session is required on the access point and on the station
interface. Being on the setup network grants no extra rights.

Honest limits:

- The rate limit is per client address. It slows a single guessing client; it
  does not stop an attacker who can vary the source address.
- `status_token` and `config_token` are obsolete. They are still read from an
  old `config.json` and persist across a firmware update - only a factory
  reset clears them - and **no endpoint authorizes on a `?token=` parameter
  today** - the `401` text on `/rtcm` that still mentions one is stale (see
  [known-limitations.md](known-limitations.md)).

## The setup access point

The access point uses `network.AUTH_WPA_WPA2_PSK` with a device-specific
password. At start-up, a missing password, a password that equals the device
code, and a legacy MAC-derived password are each replaced by a random one: 48
bits from `os.urandom` behind a fixed recognisable prefix (`cfg_factory.py`,
`net.py`). A password is no longer derived from the MAC, because the AP MAC is
the BSSID, it travels in every beacon, and the derivation is public in this
repository.

Honest limit: a configuration that still carries the publicly known factory
password is not replaced automatically. It only produces a warning in the log,
and it has to be changed in the portal.

The access-point password is **not** cleared by a factory reset, so the Wi-Fi
QR code on the label stays valid.

Honest limit: when the password is generated, it is written to the log at
`INFO` level. That line reaches the serial console and the RTC flight
recorder, and the flight recorder is part of `/errors` and of the support
package. The support package masks the Wi-Fi and NTRIP credentials, the device
code, and the stream token, but not the access-point password.

## Response headers

Every portal response carries:

| Header | Value |
|---|---|
| `X-Content-Type-Options` | `nosniff` |
| `Referrer-Policy` | `no-referrer` |
| `X-Frame-Options` | `DENY` |
| `Content-Security-Policy` | `default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'` |

The policy allows no external origin at all. The portal ships its own
stylesheets, its translation dictionary, and its QR generator; nothing is
fetched from a CDN. JSON responses and portal pages are sent
`Cache-Control: no-store`; only the versioned browser assets are cacheable.

Responses are HTTP/1.0 with `Connection: close`, and the server accepts six
concurrent connections before answering `503` with `Retry-After: 2`.

## NMEA over TCP

TCP NMEA requires the `AUTH <stream token>` handshake by default
(`nmea_tcp_auth_required`). The stream token is a separate 128-bit secret; it
is neither the device code nor the BLE passkey, and it is served only to an
authenticated session through `GET /api/label`. The handshake itself and its
byte limit are specified in [integration.md](integration.md).

An administrator can switch the requirement off under **Device -> Access**,
for clients that cannot perform the handshake. Every participant on the local
network with access to port 10110 or 2947 can then read the position stream.
That mode is meant for a trusted, isolated network only, and it applies to new
connections; existing connections are not renegotiated.

`POST /api/stream-token/rotate` issues a new token and disconnects the
currently connected TCP clients. BLE bonds are unaffected.

Honest limit: the handshake authenticates the client once, in clear text, on a
plain TCP connection. It keeps casual readers off the stream on a shared
network; it is not confidentiality, and an observer of the connection learns
the token.

Honest limit: the five-second timeout applies to each byte, not to the
handshake as a whole, so there is no total deadline for a slow client to
finish it. While a connection is unauthenticated it still counts against
`nmea_tcp_max_clients`, so clients that send bytes just often enough to avoid
the per-byte timeout can occupy every pending-authentication slot and block
new connections from authenticating (see
[known-limitations.md](known-limitations.md)).

## Bluetooth LE

The Nordic UART Service is configured with `bond=True, mitm=True,
le_secure=True` and the *display only* IO capability, so pairing uses LE
Secure Connections with the six-digit passkey from the label. Both
characteristics are marked as requiring encryption, and the firmware
additionally sends NMEA notifications only to connections it has seen become
both encrypted and authenticated.

Bonds are stored on the device in `ble-bonds.json` and survive a restart, so a
later connection normally needs no passkey again. A factory reset deletes
them. If a bond was removed on one side only, delete it on the other side too
before pairing again.

BLE has no additional `AUTH` command. Writes to the RX characteristic are
forwarded to the receiver only on an authenticated encrypted connection, and
only while `ble_commands_enabled` is set or the 600-second maintenance window
from `POST /api/ble-maintenance` is open.

The ESP32-S3 has no Bluetooth Classic and no SPP. Integrations use BLE NUS,
TCP, or USB.

## What is outside the boundary

- **Physical and USB access.** The stored bonds, the configuration, the
  identity file, and the survey data are readable over the USB REPL. Physical
  possession of the device is full access.
- **Backups.** A configuration backup contains the access-point password, the
  device code, and the stored Wi-Fi and NTRIP credentials in clear text. Treat
  it exactly like the credentials themselves.
- **Diagnostic downloads.** See [field-diagnostics.md](field-diagnostics.md).
- **Payloads.** NMEA, RTCM, and `$PESPS` pass through unchanged. The firmware
  does not sign, encrypt, or rewrite them.

## Related documents

- [technical.md](technical.md) - endpoints, authentication table, configuration
- [integration.md](integration.md) - the client-facing contract
- [field-diagnostics.md](field-diagnostics.md) - what a diagnostic export holds
- [known-limitations.md](known-limitations.md) - known rough edges
- [../SECURITY.md](../SECURITY.md) - how to report a finding
