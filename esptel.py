# SPDX-License-Identifier: AGPL-3.0-only
"""Versioned device telemetry in the existing NMEA stream."""
import time
import uasyncio as asyncio

from cfg import CONFIG, log
from state import app, shutdown_event


def xor_checksum(payload):
    value = 0
    for char in payload:
        value ^= ord(char)
    return "%02X" % value


def _field(value):
    return "" if value is None else str(value)


def msm_field(families):
    values = []
    for family in families or ():
        value = str(family).upper().replace("MSM", "")
        if value and value not in values:
            values.append(value)
    return "+".join(sorted(values))


def wifi_state(net_mode):
    value = str(net_mode or "").upper()
    if "STA" in value and "AP" in value:
        return "sta_ap"
    if "STA" in value:
        return "sta"
    if "AP" in value or value in ("SETUP", "RECOVERY"):
        return "ap"
    return "down"


def build_pesps(device_id, uptime_s, ntrip_state, rtcm_age_s, rtcm_bytes,
                msm, rssi, network_state, ble_drops):
    fields = ("PESPS", 1, device_id, int(uptime_s or 0), ntrip_state,
              rtcm_age_s, int(rtcm_bytes or 0), msm_field(msm), rssi,
              wifi_state(network_state), int(ble_drops or 0))
    payload = ",".join(_field(value) for value in fields)
    sentence = "$%s*%s\r\n" % (payload, xor_checksum(payload))
    if len(sentence.encode("ascii")) > 120:
        raise ValueError("PESPS sentence exceeds 120 bytes")
    return sentence


def _rssi():
    try:
        import network
        sta = network.WLAN(network.STA_IF)
        return sta.status("rssi") if sta.isconnected() else None
    except Exception:
        return None


class EspTelemetryTask:
    def __init__(self, queue, ntrip):
        self.queue = queue
        self.ntrip = ntrip

    def sentence(self):
        rtcm = self.ntrip.rtcm.summary() if self.ntrip else {}
        identity = app.identity or {}
        return build_pesps(
            identity.get("device_id"), app.stats.get("uptime_sec", 0),
            app.stats.get("ntrip_state", "disabled"),
            rtcm.get("last_msg_age_sec"), rtcm.get("bytes_total", 0),
            rtcm.get("msm", ()), _rssi(), app.stats.get("net_mode"),
            app.stats.get("ble_drops", 0))

    async def run(self):
        while not shutdown_event.is_set():
            interval = CONFIG.get("telemetry_interval_sec", 1)
            if not interval:
                await asyncio.sleep(5)
                continue
            try:
                payload = self.sentence().encode("ascii")
                if not self.queue.put_nowait(payload):
                    app.stats["telemetry_drops"] += 1
            except Exception as error:
                log("WARN", "TEL", "Could not generate telemetry: %s" % error)
            await asyncio.sleep(interval)
