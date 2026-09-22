# SPDX-License-Identifier: AGPL-3.0-only
"""Captive DNS and lightweight mDNS service discovery. No external packages: both protocols require only small UDP responses for this use case. DHCP Option 114 is activated if the MicroPython build used knows the ``captive_portal`` extension on the AP interface.
"""
import socket
import time
import uasyncio as asyncio

from cfg import CONFIG, log
from state import app, shutdown_event


MDNS_GROUP = "224.0.0.251"
MDNS_PORT = 5353
DNS_PORT = 53


def _u16(value):
    return bytes(((value >> 8) & 255, value & 255))


def _u32(value):
    return bytes(((value >> 24) & 255, (value >> 16) & 255,
                  (value >> 8) & 255, value & 255))


def _encode_name(name):
    out = bytearray()
    for label in name.rstrip(".").split("."):
        data = label.encode("utf-8")
        if not data or len(data) > 63:
            raise ValueError("Invalid DNS name")
        out.append(len(data))
        out.extend(data)
    out.append(0)
    return bytes(out)


def _decode_name(packet, offset):
    labels = []
    seen = 0
    while offset < len(packet) and seen < 128:
        length = packet[offset]
        offset += 1
        seen += 1
        if length == 0:
            return ".".join(labels).lower(), offset
        if length & 0xC0 == 0xC0:
            if offset >= len(packet):
                break
            pointer = ((length & 0x3F) << 8) | packet[offset]
            offset += 1
            suffix, _ = _decode_name(packet, pointer)
            if suffix:
                labels.append(suffix)
            return ".".join(labels).lower(), offset
        if offset + length > len(packet):
            break
        labels.append(packet[offset:offset + length].decode("utf-8", "ignore"))
        offset += length
        seen += length
    raise ValueError("DNS-Name abgeschnitten")


def _questions(packet):
    """Read all DNS questions; resolvers often bundle AAAA and A."""
    if len(packet) < 12:
        return None
    try:
        count = (packet[4] << 8) | packet[5]
        if count < 1 or count > 16:
            return None
        result = []
        offset = 12
        for _ in range(count):
            name, end = _decode_name(packet, offset)
            if end + 4 > len(packet):
                return None
            qtype = (packet[end] << 8) | packet[end + 1]
            qclass = (packet[end + 2] << 8) | packet[end + 3]
            offset = end + 4
            result.append((name, qtype, qclass, offset))
        return result
    except Exception:
        return None


def _question(packet):
    """Compatible access to the first question."""
    questions = _questions(packet)
    if not questions:
        return None
    name, qtype, _qclass, end = questions[0]
    return name, qtype, end


def _ipv4(ip):
    parts = [int(part) for part in ip.split(".")]
    if len(parts) != 4 or any(part < 0 or part > 255 for part in parts):
        raise ValueError("Invalid IPv4 address")
    return bytes(parts)


def active_mdns_ip(stats, portal, ap_ip):
    """Active interface for mDNS; when network change, the value changes."""
    sta_ip = stats.get("sta_ip")
    if sta_ip and sta_ip not in ("N/A", "Failed"):
        return sta_ip
    return ap_ip if portal else None


def _rr(name, rrtype, data, ttl=120, cache_flush=False):
    rrclass = 0x8001 if cache_flush else 1
    return (_encode_name(name) + _u16(rrtype) + _u16(rrclass) + _u32(ttl) +
            _u16(len(data)) + data)


def captive_dns_response(query, ip):
    """Point any A/ANY request to the local portal address."""
    parsed = _question(query)
    if parsed is None or parsed[1] not in (1, 255):
        return None
    name, _qtype, question_end = parsed
    question = query[12:question_end]
    answer = _rr(name, 1, _ipv4(ip), ttl=0)
    return query[:2] + b"\x81\x80" + _u16(1) + _u16(1) + b"\0\0\0\0" + question + answer


def mdns_response(query, identity, ip, http_port=80, nmea_port=10110):
    """A, PTR, SRV and TXT for WebUI and NMEA."""
    questions = _questions(query)
    if not questions:
        return None
    host = identity["hostname"] + ".local"
    display = identity["display_name"]
    services = (
        ("_http._tcp.local", display + "._http._tcp.local", http_port),
        ("_nmea-0183._tcp.local", display + "._nmea-0183._tcp.local", nmea_port),
    )
    answers = []
    for name, qtype, _qclass, _end in questions:
        if name == host and qtype in (1, 255):
            answer = _rr(host, 1, _ipv4(ip), cache_flush=True)
            if answer not in answers:
                answers.append(answer)
        elif name == "_services._dns-sd._udp.local" and qtype in (12, 255):
            for service, _instance, _port in services:
                answer = _rr(name, 12, _encode_name(service))
                if answer not in answers:
                    answers.append(answer)
        else:
            for service, instance, port in services:
                if name == service and qtype in (12, 255):
                    answer = _rr(service, 12, _encode_name(instance))
                    if answer not in answers:
                        answers.append(answer)
                if name == instance.lower() and qtype in (16, 33, 255):
                    if qtype in (33, 255):
                        answer = _rr(instance, 33,
                            b"\0\0\0\0" + _u16(port) + _encode_name(host),
                            cache_flush=True)
                        if answer not in answers:
                            answers.append(answer)
                    if qtype in (16, 255):
                        txt = ("id=" + identity["device_id"]).encode()
                        answer = _rr(instance, 16, bytes((len(txt),)) + txt,
                                     cache_flush=True)
                        if answer not in answers:
                            answers.append(answer)
    if not answers:
        return None
    question_end = questions[-1][3]
    question = query[12:question_end]
    return (b"\0\0\x84\0" + _u16(len(questions)) + _u16(len(answers)) +
            b"\0\0\0\0" + question + b"".join(answers))


def _udp_socket(port, multicast=False, interface_ip="0.0.0.0"):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    except Exception:
        pass
    sock.bind(("0.0.0.0", port))
    if multicast:
        # MicroPython does not provide socket.inet_aton() depending on the build.
        membership = _ipv4(MDNS_GROUP) + _ipv4(interface_ip)
        sock.setsockopt(getattr(socket, "IPPROTO_IP", 0),
                        getattr(socket, "IP_ADD_MEMBERSHIP", 3), membership)
        multicast_if = getattr(socket, "IP_MULTICAST_IF", None)
        if multicast_if is not None:
            try:
                sock.setsockopt(getattr(socket, "IPPROTO_IP", 0),
                                multicast_if, _ipv4(interface_ip))
            except Exception:
                pass
    sock.setblocking(False)
    return sock


async def discovery_task():
    """mDNS always, captive DNS only during Setup/Recovery operation."""
    mdns_sock = captive_sock = None
    mdns_interface_ip = None
    mdns_last_failure = None
    native_mdns = False
    captive_last_failure = None
    try:
        while not shutdown_event.is_set():
            portal = app.stats.get("access_state") in ("SETUP", "RECOVERY", "APPLYING")
            now = time.ticks_ms()
            retry_due = (captive_last_failure is None or
                         time.ticks_diff(now, captive_last_failure) >= 30000)

            desired_mdns_ip = active_mdns_ip(app.stats, portal, CONFIG["ap_ip"])
            mdns_retry_due = (mdns_last_failure is None or
                              time.ticks_diff(now, mdns_last_failure) >= 30000)
            if desired_mdns_ip != mdns_interface_ip and not native_mdns:
                if mdns_sock is not None:
                    mdns_sock.close()
                    mdns_sock = None
                mdns_interface_ip = None
            if (desired_mdns_ip and mdns_sock is None and mdns_retry_due and
                    not native_mdns):
                try:
                    mdns_sock = _udp_socket(MDNS_PORT, True, desired_mdns_ip)
                    mdns_interface_ip = desired_mdns_ip
                    mdns_last_failure = None
                    log("INFO", "WIFI", "mDNS active on %s." % desired_mdns_ip)
                except Exception as e:
                    errno = e.args[0] if getattr(e, "args", None) else None
                    if errno in (98, 112) and hasattr(__import__("network"), "hostname"):
                        # ESP32-MicroPython reserves 5353 for its own responder.
                        # network.hostname() delivers .local there.
                        native_mdns = True
                        mdns_interface_ip = desired_mdns_ip
                        log("INFO", "WIFI", "Native mDNS service active: %s.local"
                            % ((app.identity or {}).get("hostname", "rtk")))
                    else:
                        log("WARN", "WIFI", "mDNS unavailable on %s: %s"
                            % (desired_mdns_ip, e))
                        mdns_last_failure = now
            if portal and captive_sock is None and retry_due:
                try:
                    captive_sock = _udp_socket(DNS_PORT)
                    log("INFO", "WIFI", "Captive DNS active.")
                except Exception as e:
                    log("WARN", "WIFI", "Captive-DNS unavailable: %s" % e)
                    captive_last_failure = now
            elif not portal and captive_sock is not None:
                captive_sock.close()
                captive_sock = None

            if captive_sock is not None:
                try:
                    packet, address = captive_sock.recvfrom(512)
                    response = captive_dns_response(packet, CONFIG["ap_ip"])
                    if response:
                        captive_sock.sendto(response, address)
                except OSError:
                    pass

            if mdns_sock is not None and app.identity:
                try:
                    packet, _address = mdns_sock.recvfrom(512)
                    if mdns_interface_ip:
                        response = mdns_response(packet, app.identity, mdns_interface_ip,
                            CONFIG["http_port"], CONFIG["nmea_tcp_port"])
                        if response:
                            mdns_sock.sendto(response, (MDNS_GROUP, MDNS_PORT))
                except OSError:
                    pass
            await asyncio.sleep_ms(50)
    finally:
        for sock in (captive_sock, mdns_sock):
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
