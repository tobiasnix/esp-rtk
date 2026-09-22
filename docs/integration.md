# Interface contract

This document defines the stable device contracts for integrations:
discovery, NMEA over TCP and Bluetooth LE, and the proprietary `$PESPS`
telemetry sentence. Machine-readable states and error codes stay English and
are never localised. JSON errors always use one envelope:

```json
{"error": "<code>", "message": "<English fallback>"}
```

Not every interface described here has to be enabled in every installation.
The complete endpoint list, including the survey API, is in
[technical.md](technical.md).

## Discovery

`GET /api/access` needs no session and is the entry point for a client. It
returns `schema_version` 2 and:

| Field | Content |
|---|---|
| `device_id` | Twelve uppercase hex characters derived from the factory MAC |
| `display_name` | For example `RTK-A1B2C3` |
| `access_state` | `CONNECTING`, `ONLINE`, `SETUP`, `RECOVERY`, `APPLYING`, or `ERROR` |
| `hostname` | For example `rtk-a1b2c3.local`, or `null` |
| `http_port` | Portal port, 80 by default |
| `nmea_tcp.canonical_port` | 10110 by default |
| `nmea_tcp.legacy_ports` | The compatibility ports, 2947 by default |
| `nmea_tcp.authentication_required` | Whether the `AUTH` handshake is in force |
| `nmea_tcp.authentication` | `AUTH <stream_token>\r\n`, or `null` when it is off |
| `ble_nus` | Advertising name, service, TX and RX UUIDs, and the pairing properties |

The device also answers mDNS on `224.0.0.251:5353` for the interface currently
in use. Besides the `A` record for `<hostname>.local` it serves `PTR`, `SRV`,
and `TXT` records for `_http._tcp.local` and for `_nmea-0183._tcp.local`,
including the `_services._dns-sd._udp.local` enumeration. The instance name is
the display name, the `SRV` target is `<hostname>.local` with the configured
port, and the `TXT` record carries `id=<device_id>`. Details and the captive
portal are in [technical.md](technical.md), *Discovery*.

Routers and phone hotspots do not always forward mDNS. When `.local` does not
resolve, use the current IP address; it is not stable without a DHCP
reservation.

## NMEA

Port 10110 is the canonical raw NMEA port; port 2947 is a compatibility
listener that can be switched off with `nmea_tcp_legacy_enabled`. Both TCP
ports and BLE receive the same checksum-validated NMEA stream with CRLF line
endings, including the `$PESPS` sentence below. At most
`nmea_tcp_max_clients` (4) TCP clients are served at once; a further
connection is closed.

Clients should recognise the device by the NUS service UUID rather than by an
exact device name, because the BLE advertising name can change (see below).

### TCP authentication

While `nmea_tcp_auth_required` is active - the factory setting - the server
sends, immediately after the connection is established:

```text
AUTH REQUIRED\r\n
```

The client must then send exactly:

```text
AUTH <stream_token>\r\n
```

Rules enforced by the server:

- each byte of the line must arrive within five seconds of the previous one
  (there is no total deadline for the handshake), and the line must not
  exceed 96 bytes,
- the token is compared without early termination on a wrong character,
- success is confirmed with `OK AUTH\r\n`, and only then does NMEA begin,
- on failure the server sends `ERR AUTH\r\n` where it still can and closes the
  connection.

The device-specific `stream_token` is neither the device code nor the BLE
passkey. It is a separate 128-bit secret and is served only to an
authenticated session, through `GET /api/label`.

Administrators change the mode with `GET`/`PUT /api/tcp-auth` or under
**Device -> Access**. With authentication switched off, a new connection
receives **no greeting and no protocol bytes at all**: the first byte belongs
to the raw NMEA stream. Existing connections are not renegotiated. Clients
must read `authentication_required` from `/api/access` instead of inferring
the mode from a timeout.

`POST /api/stream-token/rotate` issues a new token and disconnects the
currently connected TCP clients. BLE bonds are unchanged.

### Bluetooth LE

BLE NUS uses LE Secure Connections with MITM-protected passkey pairing. Both
characteristics require an encrypted link, and the firmware notifies only
connections it has seen become encrypted *and* authenticated.

| Characteristic | UUID |
|---|---|
| Service | `6e400001-b5a3-f393-e0a9-e50e24dcca9e` |
| TX (notify, device to client) | `6e400003-b5a3-f393-e0a9-e50e24dcca9e` |
| RX (write, client to device) | `6e400002-b5a3-f393-e0a9-e50e24dcca9e` |

The advertising payload carries the service UUID; the local name is in the
scan response. When an IP address is available, the firmware appends it to the
advertising name for discoverability, as long as the whole name stays within
29 ASCII bytes. That name is not a stable identity - `device_id` is.

Bonds are stored on the device, so later connections normally need no passkey
again. A factory reset deletes all bonds. If a bond was removed on one side
only, delete it on the other side as well before pairing again.

BLE has no additional `AUTH` command. The ESP32-S3 supports neither Bluetooth
Classic nor SPP; integrations use BLE NUS, TCP, or USB.

## `$PESPS` version 1

Once per `telemetry_interval_sec` (1 s by default) the device emits one
proprietary sentence through the normal BLE and TCP fan-out:

```text
$PESPS,1,<device_id>,<uptime_s>,<ntrip_state>,<rtcm_stream_age_s>,<rtcm_bytes>,<msm>,<rssi>,<wifi_state>,<ble_drops>*HH\r\n
```

A synthetic example with a valid XOR checksum:

```text
$PESPS,1,A1B2C3D4E5F6,123,streaming,0.8,4711,4+7,-63,sta,0*4B\r\n
```

Rules:

- `device_id`: twelve uppercase hex characters, no separators;
- `ntrip_state`: `disabled`, `waiting_wifi`, `connecting`, `streaming`,
  `backoff`, `auth_error`, or `mount_error` - no other value is publishable;
- `rtcm_stream_age_s`: age of the last RTCM frame received by the device,
  rounded to 0.1 s. It is not GGA field 13, and it stays empty until the first
  frame of this boot has arrived;
- `rtcm_bytes`: monotonically increasing since start-up;
- `msm`: the received MSM families, sorted and joined with `+`, for
  example `4+7`;
- `rssi`: the station RSSI, or empty when no station link exists;
- `wifi_state`: `down`, `ap`, `sta`, or `sta_ap`;
- unknown values are empty fields, never localised placeholders;
- `HH`: two uppercase hex digits, the XOR over the bytes between `$` and `*`;
- the complete line stays at 120 bytes or shorter; the firmware refuses to emit
  a longer one;
- compatible fields may only be appended. Reordering or changing the meaning
  of a field requires a new integer sentence version;
- the producer enqueues with a non-blocking operation. When the queue is full,
  telemetry is dropped and counted; GNSS never waits for it.

`telemetry_interval_sec = 0` disables the sentence.

In GGA-derived data, the correction age and the station ID stay nullable, and
a station ID keeps its leading zeros because it is carried as a string.

## Related documents

- [technical.md](technical.md) - full HTTP API, NTRIP behaviour, data model
- [transport-security.md](transport-security.md) - what these transports protect
- [../README.md](../README.md) - hardware, install, field use
- [known-limitations.md](known-limitations.md) - known rough edges
