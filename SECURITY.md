# Security Policy

The web portal is served over plain HTTP on the local network only, with no
HTTPS and no cloud service involved. A portal session starts with the device
code printed on the device label, exchanged for a session that lives in
memory and does not survive a restart. The factory setup access point uses a
randomly generated Wi-Fi password, distinct from the device code, so joining
it does not by itself disclose the portal credential. NMEA over Bluetooth Low
Energy requires LE Secure Connections pairing with a six-digit passkey and
bonding, and NMEA over TCP defaults to a token handshake before the first
byte, though an administrator can disable that handshake for a trusted,
isolated network. None of this protects a device once someone has physical or
USB access to it, or protects the network path itself, since the portal is
plain HTTP — see [docs/transport-security.md](docs/transport-security.md) for
the complete trust boundary and its limits.

## Reporting a vulnerability

Please report suspected security vulnerabilities privately, using GitHub's
built-in reporting rather than a public issue: open this repository's
**Security** tab and choose **Report a vulnerability** to start a private
security advisory.

When reporting, please do not attach a configuration backup, a support
package, or a field-diagnostic export — these can contain Wi-Fi and NTRIP
credentials, the device code, or measured positions in clear text. Describe
what you found instead, and a maintainer will ask for the specific evidence
needed, and how to share it, if any is required.
