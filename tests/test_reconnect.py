# SPDX-License-Identifier: AGPL-3.0-only
"""Automatic Wi-Fi driver reconnection. Observed in the field: after a demolition, the ESP32 reconnected on its own, but NTRIP remained silent for three hours. The maintenance loop saw isconnected()==True and kept everything in order - without setting wifi_connected_event for the NTRIP task.
"""
import asyncio
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

from support import cfg, net, state, web
import cfg_store


class FakeWLAN:
    def __init__(self, connected=True, ip="203.0.113.195"):
        self._verbunden = connected
        self._ip = ip

    def isconnected(self):
        return self._verbunden

    def ifconfig(self, cfg=None):
        return (self._ip, "255.255.255.0", "203.0.113.225", "203.0.113.225")

    def active(self, v=None):
        return True

    def config(self, *a, **kw):
        return b"\xa1\xb2\xc3\xd4\xe5\xf6"


class TestRecordingProbe(unittest.TestCase):
    def test_without_a_probe_nothing_records(self):
        self.assertFalse(state.AppState().recording_active())

    def test_probe_result_is_returned_as_bool(self):
        app = state.AppState()
        app.set_recording_probe(lambda: 1)
        self.assertIs(app.recording_active(), True)
        app.set_recording_probe(lambda: None)
        self.assertIs(app.recording_active(), False)

    def test_a_failing_probe_counts_as_not_recording(self):
        app = state.AppState()

        def broken():
            raise RuntimeError("probe")

        app.set_recording_probe(broken)
        self.assertFalse(app.recording_active())


class TestAdoptConnection(unittest.TestCase):
    def setUp(self):
        self.nm = net.NetworkManager.__new__(net.NetworkManager)
        self.nm.wlan_sta = FakeWLAN()
        state.app.wifi_connected_event.clear()
        state.app.stats["sta_ip"] = "Failed"
        self.saved = dict(cfg.CONFIG)
        cfg.CONFIG["ntp_sync"] = False        # No network access in the test

    def tearDown(self):
        state.app.wifi_connected_event.clear()
        cfg.CONFIG.clear()
        cfg.CONFIG.update(self.saved)

    def test_set_the_event(self):
        """Without that, the NTRIP task is always waiting for you."""
        self.assertTrue(self.nm._adopt_restored_connection())
        self.assertTrue(state.app.wifi_connected_event.is_set())

    def test_takes_over_the_address(self):
        self.nm._adopt_restored_connection()
        self.assertEqual(state.app.stats["sta_ip"], "203.0.113.195")

    def test_reports_the_network_mode(self):
        self.nm._adopt_restored_connection()
        self.assertEqual(state.app.stats["net_mode"], "STA+AP")

    def test_it_works_only_once(self):
        self.assertTrue(self.nm._adopt_restored_connection())
        self.assertFalse(self.nm._adopt_restored_connection())

    def test_when_the_event_is_already_set_nothing_happens(self):
        state.app.wifi_connected_event.set()
        state.app.stats["sta_ip"] = "unchanged"
        self.assertFalse(self.nm._adopt_restored_connection())
        self.assertEqual(state.app.stats["sta_ip"], "unchanged")

    def test_do_not_take_over_without_ip(self):
        # ifconfig can still deliver 0.0.0.0 immediately after the association.
        self.nm.wlan_sta = FakeWLAN(ip="0.0.0.0")
        self.assertFalse(self.nm._adopt_restored_connection())
        self.assertFalse(state.app.wifi_connected_event.is_set())


class TestWifiDiagnostics(unittest.TestCase):
    def test_last_link_values_survive_a_disconnect(self):
        nm = net.NetworkManager.__new__(net.NetworkManager)
        nm.wlan_sta = FakeWLAN()
        nm._last_sta_ssid = ""
        nm._last_sta_rssi = None
        nm._last_sta_channel = None
        nm._remember_sta_link("Field hotspot")
        text = nm._sta_diagnostics()
        self.assertIn("status unknown", text)
        self.assertNotIn("Field hotspot", text)

    def test_smartphone_hotspots_receive_a_longer_connection_window(self):
        with open("net.py") as source:
            text = source.read()
        self.assertIn("seconds = 15 if len(networks) == 1 else 12", text)
        self.assertIn('"access_point_not_found"', text)
        self.assertIn("STA connection timed out (status %s, %s)", text)
        self.assertNotIn("STA connection to %s timed out", text)


class TestLoopRuftEsAuf(unittest.TestCase):
    def test_maintenance_loop_takes_over_the_connection(self):
        with open("net.py") as fh:
            source = fh.read()
        location = source[source.find("# 3. MAINTENANCE LOOP"):]
        location = location[:location.find("Network Manager task stopped")]
        i = location.find("if self.wlan_sta.isconnected():")
        self.assertGreater(i, 0)
        # Window until the next continue - the comment before it is long.
        block = location[i:]
        block = block[:block.find("continue") + 8]
        self.assertIn("_adopt_restored_connection", block)

    def test_maintenance_loop_catches_up_ntp_only_on_a_connected_station(self):
        with open("net.py") as fh:
            source = fh.read()
        location = source[source.find("# 3. MAINTENANCE LOOP"):]
        location = location[:location.find("Network Manager task stopped")]
        i = location.find("if self.wlan_sta.isconnected():")
        self.assertGreater(i, 0)
        block = location[i:]
        block = block[:block.find("continue") + 8]
        self.assertIn("self._ntp_catch_up()", block)
        self.assertLess(block.index("_adopt_restored_connection"),
                        block.index("self._ntp_catch_up()"))
        self.assertEqual(location.count("_ntp_catch_up"), 1)


class TestNtpWhileRecording(unittest.TestCase):
    def setUp(self):
        self.recording = False
        self.settime = mock.Mock()
        patches = [
            mock.patch.object(net, "_ntp_deferred", False),
            mock.patch.dict(sys.modules, {"ntptime": types.SimpleNamespace(settime=self.settime)}),
            mock.patch.dict(net.CONFIG, {"ntp_sync": True}),
            mock.patch.object(net, "log"),
            mock.patch.object(net.app, "recording_active", side_effect=lambda: self.recording),
            mock.patch.object(net.app, "wifi_connected_event", asyncio.Event()),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        event_patch = mock.patch.object(net.field_recorder, "event")
        self.event = event_patch.start()
        self.addCleanup(event_patch.stop)
        self.nm = net.NetworkManager.__new__(net.NetworkManager)
        self.nm.wlan_sta = FakeWLAN(connected=True)

    def test_no_settime_while_recording(self):
        self.recording = True
        net._zeit_holen()
        self.settime.assert_not_called()
        self.assertTrue(net._ntp_deferred)
        self.event.assert_called_once_with("ntp", {"result": "deferred"})

    def test_restored_connection_during_a_recording_defers_the_sync(self):
        # real path: the driver reconnected on its own while a line records
        self.recording = True
        self.assertTrue(self.nm._adopt_restored_connection())
        self.settime.assert_not_called()
        self.assertTrue(net._ntp_deferred)

    def test_successful_attempt_during_a_recording_defers_the_sync(self):
        # real path: the end of a successful _single_attempt
        self.recording = True
        wlan = FakeWLAN(connected=True)
        wlan.connect = lambda ssid, password: None
        wlan.disconnect = lambda: None
        self.nm.wlan_sta = wlan
        self.nm._hostname_setzen = mock.Mock()
        self.nm._remember_sta_link = mock.Mock()
        self.nm._last_sta_rssi = self.nm._last_sta_channel = None
        with mock.patch.object(net, "configured_networks",
                               return_value=[("Field", "password-1")]):
            self.assertTrue(asyncio.run(
                self.nm._single_attempt("Field", "password-1", 1)))
        self.settime.assert_not_called()
        self.assertTrue(net._ntp_deferred)
        self.assertEqual(net.app.stats["wifi_ssid_active"], "Field")
        self.assertEqual(net.app.stats["wifi_ssid_aktiv"], net.app.stats["wifi_ssid_active"])

    def test_deferral_is_recorded_once(self):
        self.recording = True
        net._zeit_holen()
        net._zeit_holen()
        self.assertEqual([c.args for c in self.event.call_args_list],
                         [("ntp", {"result": "deferred"})])

    def test_deferred_sync_runs_once_the_recording_ends(self):
        self.recording = True
        net._zeit_holen()
        self.recording = False
        net.app.wifi_connected_event.set()
        self.assertTrue(self.nm._ntp_catch_up())
        self.settime.assert_called_once_with()
        self.assertFalse(net._ntp_deferred)
        self.assertFalse(self.nm._ntp_catch_up())      # nothing left to catch up
        self.settime.assert_called_once_with()

    def test_catch_up_waits_while_recording_or_offline(self):
        self.recording = True
        net._zeit_holen()
        net.app.wifi_connected_event.set()
        self.assertFalse(self.nm._ntp_catch_up())      # still recording
        self.recording = False
        net.app.wifi_connected_event.clear()
        self.assertFalse(self.nm._ntp_catch_up())      # event cleared: offline
        self.settime.assert_not_called()
        self.assertTrue(net._ntp_deferred)

    def test_catch_up_trusts_the_driver_not_a_stale_event(self):
        # recording ended between two passes, the link is gone, the loop has not
        # cleared the event yet
        self.recording = True
        net._zeit_holen()
        self.recording = False
        net.app.wifi_connected_event.set()
        self.nm.wlan_sta = FakeWLAN(connected=False)
        self.assertFalse(self.nm._ntp_catch_up())
        self.settime.assert_not_called()
        self.assertTrue(net._ntp_deferred)

    def test_ntp_sync_off_never_defers(self):
        self.recording = True
        with mock.patch.dict(net.CONFIG, {"ntp_sync": False}):
            net._zeit_holen()
        self.assertFalse(net._ntp_deferred)
        self.event.assert_not_called()

    def test_disabling_ntp_drops_a_pending_deferral(self):
        self.recording = True
        net._zeit_holen()
        self.recording = False
        net.app.wifi_connected_event.set()
        with mock.patch.dict(net.CONFIG, {"ntp_sync": False}):
            self.assertFalse(self.nm._ntp_catch_up())
            self.assertFalse(net._ntp_deferred)
            self.assertFalse(self.nm._ntp_catch_up())
        self.settime.assert_not_called()

    def test_failed_catch_up_is_not_retried(self):
        self.settime.side_effect = OSError("timeout")
        self.recording = True
        net._zeit_holen()
        self.recording = False
        net.app.wifi_connected_event.set()
        self.assertTrue(self.nm._ntp_catch_up())
        self.assertFalse(net._ntp_deferred)
        self.event.assert_any_call("ntp", {"result": "failed"})
        self.assertFalse(self.nm._ntp_catch_up())
        self.assertEqual(self.settime.call_count, 1)

    def test_caught_up_step_of_a_valid_clock_is_still_a_resync(self):
        before = state.app.clock_steps
        self.recording = True
        net._zeit_holen()
        self.recording = False
        net.app.wifi_connected_event.set()
        with mock.patch.object(net, "_clock_valid", return_value=True), \
                mock.patch.object(net.time, "time", side_effect=[1000, 1003]), \
                mock.patch.object(net.time, "ticks_ms", side_effect=[0, 0]):
            self.assertTrue(self.nm._ntp_catch_up())
        self.event.assert_any_call("clock_resync", {"reason": "ntp", "delta_s": 3})
        self.assertEqual(state.app.clock_steps, before + 1)


if __name__ == "__main__":
    unittest.main()


class TestTreiberZurRuheBringen(unittest.TestCase):
    """wlan.connect() turns on the automatic reconnection. After a failed lap, the driver in the background tries on endlessly and jumps over the canal - STA, AP and BLE share a radio part, so everything becomes unattainable."""

    def test_failed_round_separates_the_station(self):
        with open("net.py") as fh:
            source = fh.read()
        location = source[source.find("app.stats[\"wifi_ssid_aktiv\"] = \"\""):]
        location = location[:location.find("return False")]
        self.assertIn("disconnect()", location)
        self.assertIn("app.stats[\"wifi_ssid_active\"] = \"\"", source)
        failed = source[source.find("app.stats[\"wifi_ssid_active\"] = \"\""):]
        failed = failed[:failed.find("return False")]
        self.assertIn("app.stats[\"wifi_ssid_aktiv\"] = \"\"", failed)
        self.assertIn("disconnect()", failed)

    def test_switching_off_the_ap_is_logged(self):
        # Otherwise, you only see "AP started" in the log and hold the get_element in between
        # for a failure.
        with open("net.py") as fh:
            source = fh.read()
        i = source.find("disable_ap = self.wlan_ap.active()")
        self.assertIn("AP stopped for the connection attempt",
                      source[i:i + 700])


class FakeAP:
    def __init__(self, stations=0, active=True):
        self._stationen = [("mac",)] * stations
        self._aktiv = active

    def active(self, v=None):
        return self._aktiv

    def status(self, field=None):
        if field == "stations":
            return self._stationen
        raise ValueError(field)


class TestApNichtUnterLaufendemClient(unittest.TestCase):
    """An active configuration client must not lose the AP mid-form."""

    def setUp(self):
        self.nm = net.NetworkManager.__new__(net.NetworkManager)
        self.nm._sta_versuche = 5              # Not the first attempt
        self.nm._last_channel_switch = -10 ** 9   # interval expired long ago
        self.saved = dict(cfg.CONFIG)

    def tearDown(self):
        cfg.CONFIG.clear()
        cfg.CONFIG.update(self.saved)

    def test_without_a_client_the_ap_may_give_way(self):
        self.nm.wlan_ap = FakeAP(stations=0)
        self.assertTrue(self.nm._may_disable_ap())

    def test_with_client_he_remains_standing(self):
        self.nm.wlan_ap = FakeAP(stations=1)
        self.assertFalse(self.nm._may_disable_ap())

    def test_even_on_the_first_attempt(self):
        # Even the first attempt must not throw anyone out.
        self.nm._sta_versuche = 0
        self.nm.wlan_ap = FakeAP(stations=1)
        self.assertFalse(self.nm._may_disable_ap())

    def test_firmware_without_station_list_is_not_blocked(self):
        class Alt(FakeAP):
            def status(self, field=None):
                raise AttributeError("does not implement status()")
        self.nm.wlan_ap = Alt()
        self.assertTrue(self.nm._may_disable_ap())


class TestApHatVorrang(unittest.TestCase):
    """A zero channel-switch interval means the AP is never stopped. Previously, 0 caused the opposite - the interval had expired immediately, the AP was switched off on EVERY attempt.
    """

    def setUp(self):
        self.nm = net.NetworkManager.__new__(net.NetworkManager)
        self.nm.wlan_ap = FakeAP(stations=0)
        self.nm._sta_versuche = 5
        self.nm._last_channel_switch = -10 ** 9
        self.saved = dict(cfg.CONFIG)

    def tearDown(self):
        cfg.CONFIG.clear()
        cfg.CONFIG.update(self.saved)

    def test_zero_means_never(self):
        cfg.CONFIG["ap_channel_switch_interval_sec"] = 0
        self.assertFalse(self.nm._may_disable_ap())

    def test_zero_also_applies_on_the_first_attempt(self):
        cfg.CONFIG["ap_channel_switch_interval_sec"] = 0
        self.nm._sta_versuche = 0
        self.assertFalse(self.nm._may_disable_ap())

    def test_positiver_wert_erlaubt_es_weiterhin(self):
        cfg.CONFIG["ap_channel_switch_interval_sec"] = 60
        self.assertTrue(self.nm._may_disable_ap())

    def test_setup_without_configured_network_takes_precedence(self):
        # V10 decides according to state: without network immediately SETUP. The pure
        # channel change rule is retained for RECOVERY attempts.
        self.assertFalse(cfg.configured_networks())


class FakeStaDriver:
    """STA driver that refuses scans while inactive, as MicroPython 1.28 does on the ESP32."""

    def __init__(self, active=False, networks=(), fail_activation=False):
        self._active, self.networks, self.fail_activation = active, list(networks), fail_activation
        self.connects, self.scans = [], 0
        # link: optional callable that reports the radio link; disconnect() drops it.
        self.link, self.linked, self.disconnects = None, True, 0

    def active(self, value=None):
        if value is None:
            return self._active
        if value and self.fail_activation:
            raise OSError("Wifi Internal Error")
        self._active = bool(value)

    def scan(self):
        if not self._active:
            raise OSError("STA must be active")
        self.scans += 1
        return list(self.networks)

    def connect(self, *args):
        self.connects.append(args)

    def disconnect(self):
        self.disconnects += 1
        self.linked = False

    def isconnected(self):
        return bool(self.link is not None and self.linked and self.link())

    def config(self, *args, **kwargs):
        return b"\xa1\xb2\xc3\xd4\xe5\xf6"


class TestNoScanWhileRecording(unittest.TestCase):
    """A scan blocks the event loop for about three seconds; never while recording."""

    def setUp(self):
        self.nm = net.NetworkManager.__new__(net.NetworkManager)
        self.nm.wlan_sta = FakeWLAN(connected=False)
        self.nm._sta_versuche = 0
        self.nm.scan_requested = False
        self.nm.scan_results = self.nm.scan_error = self.nm.scan_completed_ms = None
        self.nm._scan = mock.AsyncMock(return_value=[(b"B", b"m", 1, -50, 3, False)])
        self.nm._single_attempt = mock.AsyncMock(return_value=True)
        for patcher in (
                mock.patch.object(net, "configured_networks",
                                  return_value=[("A", "password-a"), ("B", "password-b")]),
                mock.patch.object(net, "log")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _recording(self, value):
        patcher = mock.patch.object(net.app, "recording_active", return_value=value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_reconnect_round_skips_the_scan_while_recording(self):
        self._recording(True)
        self.nm._single_attempt = mock.AsyncMock(side_effect=[False, True])
        self.assertTrue(asyncio.run(self.nm._connect_sta_versuch()))
        self.nm._scan.assert_not_awaited()
        self.assertEqual([c.args[0] for c in self.nm._single_attempt.await_args_list],
                         ["A", "B"])

    def test_reconnect_round_scans_when_not_recording(self):
        self._recording(False)
        self.assertTrue(asyncio.run(self.nm._connect_sta_versuch()))
        self.nm._scan.assert_awaited_once_with()
        self.assertEqual([c.args[0] for c in self.nm._single_attempt.await_args_list],
                         ["B"])

    def test_requested_scan_is_dropped_while_recording(self):
        self._recording(True)
        self.nm.scan_requested = True
        asyncio.run(self.nm._service_requested_scan())
        self.nm._scan.assert_not_awaited()
        self.assertFalse(self.nm.scan_requested)
        self.assertIsNone(self.nm.scan_completed_ms)
        self.assertEqual(self.nm.request_scan(), ("pending", None, None))

    def test_requested_scan_runs_when_not_recording(self):
        self._recording(False)
        self.nm._scan = mock.AsyncMock(return_value=[(b"Field", b"m", 1, -40, 3, False)])
        self.nm.scan_requested = True
        self.nm._ensure_sta_for_scan = mock.AsyncMock(return_value=True)
        asyncio.run(self.nm._service_requested_scan())
        self.assertEqual(self.nm.scan_results,
                         [{"ssid": "Field", "rssi": -40, "secured": True}])
        self.assertIsNotNone(self.nm.scan_completed_ms)


class TestRequestedScanStation(unittest.TestCase):
    """A requested scan must work with an inactive station and leave the AP alone."""

    def setUp(self):
        self.nm = net.NetworkManager.__new__(net.NetworkManager)
        self.nm.wlan_ap = mock.Mock(wraps=FakeAP(stations=1, active=True))
        self.nm.scan_requested = True
        self.nm.scan_results = self.nm.scan_error = self.nm.scan_completed_ms = None

        async def no_sleep(_ms):
            return None

        for patcher in (
                mock.patch.object(net.asyncio, "sleep_ms", no_sleep, create=True),
                mock.patch.object(net, "log")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _recording(self, **kwargs):
        patcher = mock.patch.object(net.app, "recording_active", **kwargs)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_requested_scan_activates_an_inactive_station_without_connecting(self):
        self._recording(return_value=False)
        self.nm.wlan_sta = FakeStaDriver(networks=[(b"Field", b"m", 1, -40, 3, False)])
        asyncio.run(self.nm._service_requested_scan())
        self.assertEqual(self.nm.scan_results,
                         [{"ssid": "Field", "rssi": -40, "secured": True}])
        self.assertIsNone(self.nm.scan_error)
        self.assertTrue(self.nm.wlan_sta.active())            # stays active (decision N)
        self.assertEqual(self.nm.wlan_sta.connects, [])
        self.assertNotIn(mock.call(False), self.nm.wlan_ap.active.call_args_list)
        self.assertTrue(self.nm.wlan_ap.active())

    def test_recording_started_during_activation_drops_the_scan(self):
        self._recording(side_effect=[False, True])
        self.nm.wlan_sta = FakeStaDriver(networks=[(b"Field", b"m", 1, -40, 3, False)])
        asyncio.run(self.nm._service_requested_scan())
        self.assertEqual(self.nm.wlan_sta.scans, 0)
        self.assertFalse(self.nm.scan_requested)
        self.assertIsNone(self.nm.scan_completed_ms)

    def test_station_activation_failure_reports_scan_failed(self):
        self._recording(return_value=False)
        self.nm.wlan_sta = FakeStaDriver(fail_activation=True)
        asyncio.run(self.nm._service_requested_scan())
        self.assertIsNone(self.nm.scan_results)
        self.assertEqual(self.nm.scan_error, "scan_failed")
        self.assertIsNotNone(self.nm.scan_completed_ms)
        self.assertEqual(self.nm.wlan_sta.connects, [])


class TestSetupHold(unittest.TestCase):
    """BOOT 3 s and the unconfigured boot keep the setup AP for SETUP_HOLD_MS."""

    def run_manager(self, passes, force_setup=True, networks=(("Field", "password-1"),),
                    connect_result=True, start_ms=0, period=None, scan_requested=False,
                    pending=False, connect_record=None, connect_sequence=(),
                    ap_effects=False, sta_link=None, ap_start_failures=0):
        """Runs NetworkManager.run() for `passes` fully handled maintenance passes.

        A pass is complete once the loop reaches the next asyncio.sleep(5); the
        sleep after the last pass sets the shutdown event without advancing the
        clock, so the partial pass ends at the loop's own shutdown check. The ten
        sleep(1) of the verdict count as sleeps as well.

        pending=False/True replaces the configuration transaction with mocks
        (self.rollback, self.commit, self.retry); pending="real" keeps the real cfg
        transaction in the working directory. connect_record collects the
        credentials each station attempt would use; connect_sequence gives the
        first results, connect_result the rest. With ap_effects, the first
        ap_start_failures calls of _start_ap leave the AP off (a swallowed error).
        """
        nm = net.NetworkManager.__new__(net.NetworkManager)
        nm.wlan_sta = FakeStaDriver(active=False,
                                    networks=[(b"Field", b"m", 1, -40, 3, False)])
        nm.wlan_ap = mock.Mock(wraps=FakeAP(stations=0, active=True))
        nm.scan_requested = scan_requested
        nm.scan_results = nm.scan_error = nm.scan_completed_ms = None
        nm._setup_since_ms = None
        nm._start_ap = mock.Mock()
        nm._stop_ap = mock.Mock()
        if ap_effects:
            fake_ap = FakeAP(stations=0, active=False)
            nm.wlan_ap = mock.Mock(wraps=fake_ap)
            failures = [ap_start_failures]

            def start_ap():
                if failures[0] > 0:
                    failures[0] -= 1
                else:
                    fake_ap._aktiv = True
            nm._start_ap = mock.Mock(side_effect=start_ap)
            nm._stop_ap = mock.Mock(side_effect=lambda: setattr(fake_ap, "_aktiv", False))
        if sta_link is not None:
            nm.wlan_sta.link = lambda: sta_link(self.sleeps)
        results = list(connect_sequence)

        def attempt():
            if connect_record is not None:
                connect_record.append((cfg.CONFIG["wifi_ssid"], cfg.CONFIG["wifi_pass"]))
            return results.pop(0) if results else connect_result
        nm._connect_sta = mock.AsyncMock(side_effect=attempt)
        nm._adopt_restored_connection = mock.Mock()
        nm._remember_sta_link = mock.Mock()
        nm._ntp_catch_up = mock.Mock(return_value=False)

        clock = [start_ms]
        self.sleeps = 0
        self.log = mock.Mock()
        self.rollback = mock.Mock(return_value=True)
        self.commit = mock.Mock(return_value=True)
        self.retry = mock.Mock(return_value=True)

        async def no_sleep_ms(_ms):
            return None

        async def scenario():
            stop = asyncio.Event()

            async def sleep(seconds):
                self.sleeps += 1
                if self.sleeps > passes:
                    stop.set()
                else:
                    clock[0] += int(seconds * 1000)

            if period is None:
                ticks_ms = lambda: clock[0]
            else:
                ticks_ms = lambda: clock[0] % period
            patchers = [
                mock.patch.object(net.asyncio, "sleep", sleep),
                mock.patch.object(net.asyncio, "sleep_ms", no_sleep_ms, create=True),
                mock.patch.object(net, "shutdown_event", stop),
                mock.patch.object(net.time, "ticks_ms", ticks_ms),
                mock.patch.object(net, "configured_networks", return_value=list(networks)),
                mock.patch.object(net, "log", self.log),
                mock.patch.object(net.field_recorder, "event"),
                mock.patch.object(net.app, "recording_active", return_value=False),
                mock.patch.object(net.app, "wifi_connected_event", asyncio.Event()),
                mock.patch.object(net.app, "log_error"),
            ]
            if pending != "real":
                patchers += [
                    mock.patch.object(net, "pending_config_active", return_value=pending),
                    mock.patch.object(net, "rollback_open", return_value=False),
                    mock.patch.object(net, "retry_rollback", self.retry),
                    mock.patch.object(net, "rollback_pending_config", self.rollback),
                    mock.patch.object(net, "commit_pending_config", self.commit),
                ]
            if period is not None:
                patchers.append(mock.patch.object(
                    net.time, "ticks_diff",
                    lambda a, b: (a - b + period // 2) % period - period // 2))
            for patcher in patchers:
                patcher.start()
            try:
                await nm.run()
            finally:
                for patcher in reversed(patchers):
                    patcher.stop()

        with mock.patch.dict(state.app.stats, {"force_setup": force_setup,
                                               "access_state": "CONNECTING",
                                               "net_mode": "INIT",
                                               "wifi_reconnects": 0}):
            asyncio.run(scenario())
            stats = dict(state.app.stats)
        self.assertEqual(self.sleeps, passes + 1)
        return nm, stats

    def test_forced_setup_makes_no_station_attempt_for_three_passes(self):
        nm, stats = self.run_manager(3)
        nm._connect_sta.assert_not_awaited()
        nm._stop_ap.assert_not_called()
        nm._start_ap.assert_called_once_with()
        self.assertEqual(stats["access_state"], "SETUP")

    def test_forced_setup_holds_at_595_seconds(self):
        nm, stats = self.run_manager(119)              # last pass at 595 s
        nm._connect_sta.assert_not_awaited()
        self.assertEqual(stats["access_state"], "SETUP")
        self.assertIs(stats["force_setup"], True)

    def test_forced_setup_connects_at_600_seconds(self):
        nm, stats = self.run_manager(120)              # last pass at 600 s = SETUP_HOLD_MS
        nm._connect_sta.assert_awaited_once_with()
        nm._stop_ap.assert_called_once_with()
        self.assertEqual(stats["access_state"], "ONLINE")

    def test_window_expiry_clears_the_forced_setup(self):
        _, stats = self.run_manager(120)
        self.assertIs(stats["force_setup"], False)

    def test_window_expiry_is_logged(self):
        self.run_manager(120)
        self.log.assert_any_call(
            "INFO", "WIFI", "Setup window over; resuming Wi-Fi connection attempts.")

    def test_window_expiry_survives_a_tick_wrap(self):
        period = 1 << 30
        nm, stats = self.run_manager(120, start_ms=period - 2000, period=period)
        nm._connect_sta.assert_awaited_once_with()
        self.assertEqual(stats["access_state"], "ONLINE")
        self.assertIs(stats["force_setup"], False)

    def test_unconfigured_setup_stays_setup_after_the_window(self):
        nm, stats = self.run_manager(125, force_setup=False, networks=())
        nm._connect_sta.assert_not_awaited()
        self.assertEqual(stats["access_state"], "SETUP")
        self.assertIs(stats["force_setup"], False)

    def test_failed_attempt_after_the_window_goes_to_recovery(self):
        _, stats = self.run_manager(120, connect_result=False)
        self.assertEqual(stats["access_state"], "RECOVERY")

    def test_setup_hold_survives_a_tick_wrap(self):
        period = 1 << 30
        nm, stats = self.run_manager(3, start_ms=period - 2000, period=period)
        nm._connect_sta.assert_not_awaited()
        self.assertEqual(stats["access_state"], "SETUP")

    def test_requested_scan_is_served_during_setup(self):
        nm, stats = self.run_manager(1, scan_requested=True)
        self.assertEqual(nm.scan_results, [{"ssid": "Field", "rssi": -40, "secured": True}])
        self.assertIsNone(nm.scan_error)
        self.assertEqual(nm.wlan_sta.connects, [])
        nm._connect_sta.assert_not_awaited()
        nm._stop_ap.assert_not_called()
        self.assertNotIn(mock.call(False), nm.wlan_ap.active.call_args_list)
        self.assertEqual(stats["access_state"], "SETUP")

    def test_unconfigured_setup_stays_setup(self):
        nm, stats = self.run_manager(3, force_setup=False, networks=())
        nm._connect_sta.assert_not_awaited()
        self.assertEqual(stats["access_state"], "SETUP")

    def test_normal_boot_has_no_setup_hold(self):
        nm, stats = self.run_manager(1, force_setup=False, connect_result=False)
        self.assertEqual(nm._connect_sta.await_count, 2)   # boot attempt + first pass
        self.assertIsNone(nm._setup_since_ms)
        self.assertEqual(stats["access_state"], "RECOVERY")

    def _armed_candidate(self):
        # Working directory with a confirmed "Old" network and an armed "Invalid" candidate.
        cwd, saved = os.getcwd(), dict(cfg.CONFIG)
        os.chdir(tempfile.mkdtemp(prefix="esp-rtk-guard-"))

        def restore():
            os.chdir(cwd)
            cfg.CONFIG.clear(); cfg.CONFIG.update(saved)
            cfg._ROLLBACK_OPEN, cfg._ROLLBACK_CODE, cfg._STAGING_UNRECOVERED = False, None, False
            cfg.discard_staged_config()
            del cfg.CONFIG_WARNINGS[:]
        self.addCleanup(restore)
        cfg._ROLLBACK_OPEN, cfg._ROLLBACK_CODE, cfg._STAGING_UNRECOVERED = False, None, False
        cfg.discard_staged_config()
        cfg.CONFIG["wifi_ssid"], cfg.CONFIG["wifi_pass"] = "Old", "legacy-password"
        self.assertTrue(cfg.save_config())
        cfg.stage_config({"wifi_ssid": "Invalid"})
        self.assertTrue(cfg.apply_staged_config())
        cfg.load_config()

    def _apply_over_http(self, access_state):
        # POST /api/config/apply through the config handler, signed in, without a real reboot.
        class Writer:
            def __init__(self):
                self.data = bytearray()

            def write(self, data):
                self.data.extend(data)

            def get_extra_info(self, _key):
                return None
        writer, reboots = Writer(), []

        async def fake_reboot():
            reboots.append("/api/config/apply")
        with mock.patch.object(web, "_admin_allowed", return_value=True), \
                mock.patch.object(web, "do_soft_reboot_deferred", fake_reboot), \
                mock.patch.dict(state.app.stats, {"access_state": access_state}):
            asyncio.run(web._handle_config_request(
                writer, "/api/config/apply", "POST", "", b"{}", False, {}))
        head, _, payload = bytes(writer.data).partition(b"\r\n\r\n")
        return head.split(b"\r\n", 1)[0], payload, reboots

    def _fail_config_json(self, always=False, times=0, result_always=False):
        # side_effect for cfg._atomic_dump: config.json writes fail (always or the first
        # `times`), with result_always every result write fails too; the rest is real.
        real, failed = cfg._atomic_dump, [0]

        def dump(target, data):
            if result_always and target == cfg.CONFIG_RESULT_FILE:
                return False
            if target == "config.json" and (always or failed[0] < times):
                failed[0] += 1
                return False
            return real(target, data)
        return dump

    def test_setup_entry_rolls_back_an_unverified_candidate(self):
        for kwargs in ({}, {"force_setup": False, "networks": ()}):   # BOOT 3 s, no network
            with self.subTest(**kwargs):
                nm, stats = self.run_manager(1, pending=True, **kwargs)
                self.rollback.assert_called_once_with("setup_mode")
                self.commit.assert_not_called()
                nm._connect_sta.assert_not_awaited()
                self.assertEqual(stats["access_state"], "SETUP")

    def test_setup_entry_without_pending_does_not_roll_back(self):
        self.run_manager(1)
        self.rollback.assert_not_called()

    def test_failed_setup_rollback_never_joins_the_candidate(self):
        self._armed_candidate()
        used = []
        with mock.patch.object(cfg, "_atomic_dump", side_effect=self._fail_config_json(always=True)):
            nm, stats = self.run_manager(150, pending="real", connect_record=used)   # window over at pass 120
        self.assertEqual(used, [])
        self.assertEqual(stats["access_state"], "RECOVERY")             # window over, still blocked
        self.assertIs(stats["force_setup"], False)
        self.assertIsNone(nm._setup_since_ms)
        self.assertTrue(nm.wlan_ap.active())
        self.assertTrue(cfg.rollback_incomplete())
        self.assertEqual(cfg.CONFIG["wifi_ssid"], "Invalid")
        self.assertLessEqual(sum(1 for c in self.log.call_args_list if c.args[0] == "WARN"), 14)   # ≤ 1 per minute

    def test_setup_rollback_retry_connects_with_the_confirmed_network(self):
        self._armed_candidate()
        used = []
        with mock.patch.object(cfg, "_atomic_dump", side_effect=self._fail_config_json(times=20)):
            nm, stats = self.run_manager(300, pending="real", connect_record=used)
        self.assertTrue(used)
        self.assertEqual(set(used), {("Old", "legacy-password")})
        self.assertFalse(cfg.pending_config_active())
        self.assertEqual(cfg.config_result_read()["details"]["code"], "setup_mode")
        self.assertEqual(stats["access_state"], "ONLINE")

    def test_failed_verdict_rollback_never_joins_the_candidate(self):
        self._armed_candidate()
        used = []
        with mock.patch.object(cfg, "_atomic_dump", side_effect=self._fail_config_json(times=3)):
            nm, stats = self.run_manager(60, force_setup=False, pending="real",
                                         connect_record=used, connect_sequence=(False,))
        self.assertEqual(used[0], ("Invalid", "legacy-password"))   # the verdict attempt
        self.assertTrue(used[1:])                                   # connected after the retry succeeded
        self.assertEqual(set(used[1:]), {("Old", "legacy-password")})
        self.assertFalse(cfg.rollback_incomplete())
        self.assertEqual(stats["access_state"], "ONLINE")

    def test_failed_completion_in_commit_keeps_the_rescue_ap(self):             # I1
        self._armed_candidate()
        with open("config.json", "w") as fh:        # apply interrupted before config.json: Old is active
            json.dump(dict(cfg._persisted_config(), wifi_ssid="Old"), fh)
        cfg.load_config()
        link = {"up": True}
        with mock.patch.object(cfg, "_atomic_dump", side_effect=self._fail_config_json(always=True)):
            nm, stats = self.run_manager(
                30, force_setup=False, pending="real", ap_effects=True,
                sta_link=lambda sleeps: link["up"] and sleeps < 15)      # stable through the 10 verdict seconds, then lost
        self.assertTrue(nm.wlan_ap.active())                            # rescue AP on and kept
        self.assertEqual(stats["access_state"], "RECOVERY")
        self.assertGreaterEqual(nm.wlan_sta.disconnects, 1)             # guard dropped the station link
        self.assertEqual(nm._connect_sta.await_count, 1)                # only the boot attempt
        self.assertTrue(cfg.rollback_open())

    def test_failed_result_and_config_writes_never_join_the_candidate(self):    # I2
        for kwargs, passes in (({"force_setup": False, "connect_sequence": (False,)}, 60),
                               ({"force_setup": True}, 130)):
            with self.subTest(**kwargs):
                self._armed_candidate()
                used = []
                with mock.patch.object(cfg, "_atomic_dump", side_effect=self._fail_config_json(
                        times=3, result_always=True)):
                    nm, stats = self.run_manager(passes, pending="real", connect_record=used, **kwargs)
                later = used[1:] if not kwargs["force_setup"] else used
                if not kwargs["force_setup"]:
                    self.assertEqual(used[0], ("Invalid", "legacy-password"))   # the verdict attempt
                self.assertTrue(later)
                self.assertEqual(set(later), {("Old", "legacy-password")})
                self.assertFalse(cfg.rollback_open())

    def test_recovery_error_at_import_blocks_but_reaches_the_ap(self):         # I3, network side
        self._armed_candidate()
        os.rename(cfg.PENDING_CONFIG_FILE, cfg.PENDING_CONFIG_FILE + ".tmp")
        real_rename, fail = cfg_store.os.rename, {"on": True}
        def eio(src, dst):
            if fail["on"] and src.endswith(".tmp"):
                raise OSError(5, "EIO")
            return real_rename(src, dst)
        used = []
        with mock.patch.object(cfg_store.os, "rename", side_effect=eio):
            cfg.resolve_interrupted_apply()                              # the import-time call, no raise
            nm, stats = self.run_manager(20, force_setup=False, pending="real",
                                         ap_effects=True, connect_record=used)
            self.assertEqual(used, [])
            self.assertTrue(nm.wlan_ap.active())
            self.assertEqual(stats["access_state"], "RECOVERY")
            self.assertTrue(os.path.exists(cfg.PENDING_CONFIG_FILE + ".tmp"))
            status, payload, reboots = self._apply_over_http(stats["access_state"])
            self.assertIn(b"409", status)
            self.assertEqual(json.loads(payload)["error"], "open_change_pending")
            self.assertEqual(reboots, [])
        fail["on"] = False
        nm, stats = self.run_manager(20, force_setup=False, pending="real", connect_record=used)
        self.assertEqual(set(used), {("Old", "legacy-password")})

    def test_incomplete_rollback_at_boot_skips_the_first_connect(self):
        self._armed_candidate()
        self.assertTrue(cfg.config_result_write("failed", details={"code": "authentication_failed",
                                                                   "restored": ["wifi_ssid"]}))
        used = []
        with mock.patch.object(cfg, "_atomic_dump", side_effect=self._fail_config_json(times=2)):
            cfg.resolve_interrupted_apply()                     # import time: first attempt fails
            self.assertTrue(cfg.rollback_incomplete())
            nm, stats = self.run_manager(40, force_setup=False, pending="real", connect_record=used)
        self.assertTrue(used)
        self.assertEqual(set(used), {("Old", "legacy-password")})
        self.assertFalse(cfg.rollback_incomplete())

    def _result_tmp_fault(self):
        # The result file lost its target mid-rename; renaming its valid .tmp back fails with EIO.
        os.rename(cfg.CONFIG_RESULT_FILE, cfg.CONFIG_RESULT_FILE + ".tmp")
        real = cfg_store.os.rename

        def eio(src, dst):
            if src == cfg.CONFIG_RESULT_FILE + ".tmp":
                raise OSError(5, "EIO")
            return real(src, dst)
        return mock.patch.object(cfg_store.os, "rename", side_effect=eio)

    def test_unrecoverable_result_file_never_crashes_the_network_manager(self):   # I1/I3
        for kwargs, passes in (({"force_setup": True}, 5), ({"force_setup": False}, 30)):
            with self.subTest(**kwargs):
                self._armed_candidate()
                used = []
                with self._result_tmp_fault():
                    cfg.resolve_interrupted_apply()                  # the import-time call, no raise
                    nm, stats = self.run_manager(passes, pending="real", ap_effects=True,
                                                 connect_record=used, **kwargs)
                if kwargs["force_setup"]:
                    self.assertEqual(used, [])
                    self.assertTrue(nm.wlan_ap.active())
                    self.assertEqual(stats["access_state"], "SETUP")
                else:
                    self.assertEqual(used[0], ("Invalid", "legacy-password"))   # the verdict attempt
                    self.assertTrue(used[1:])
                    self.assertEqual(set(used[1:]), {("Old", "legacy-password")})
                    self.assertEqual(stats["access_state"], "ONLINE")
                self.assertFalse(cfg.rollback_open())

    def test_unrecoverable_result_file_without_a_change_never_blocks_the_station(self):
        cwd, saved = os.getcwd(), dict(cfg.CONFIG)
        os.chdir(tempfile.mkdtemp(prefix="esp-rtk-guard-"))

        def restore():
            os.chdir(cwd)
            cfg.CONFIG.clear(); cfg.CONFIG.update(saved)
            cfg._ROLLBACK_OPEN, cfg._ROLLBACK_CODE, cfg._STAGING_UNRECOVERED = False, None, False
        self.addCleanup(restore)
        cfg._ROLLBACK_OPEN, cfg._ROLLBACK_CODE, cfg._STAGING_UNRECOVERED = False, None, False
        cfg.CONFIG["wifi_ssid"], cfg.CONFIG["wifi_pass"] = "Old", "legacy-password"
        self.assertTrue(cfg.save_config())
        self.assertTrue(cfg.config_result_write("connected", details={"ssid": "Old"}))
        used = []
        with self._result_tmp_fault():
            cfg.resolve_interrupted_apply()
            self.assertFalse(cfg._STAGING_UNRECOVERED)
            nm, stats = self.run_manager(20, force_setup=False, pending="real", connect_record=used)
        self.assertTrue(used)
        self.assertEqual(set(used), {("Old", "legacy-password")})
        self.assertEqual(stats["access_state"], "ONLINE")

    def test_rescue_ap_comes_back_after_a_failed_start(self):                   # I1
        self._armed_candidate()
        self.assertTrue(cfg.config_result_write("failed", details={"code": "authentication_failed",
                                                                   "restored": ["wifi_ssid"]}))
        used = []
        with mock.patch.object(cfg, "_atomic_dump", side_effect=self._fail_config_json(always=True)):
            cfg.resolve_interrupted_apply()
            nm, stats = self.run_manager(1, force_setup=False, pending="real", ap_effects=True,
                                         ap_start_failures=1, connect_record=used)
        self.assertTrue(nm.wlan_ap.active())                          # healed in the next pass
        self.assertEqual(nm._start_ap.call_count, 2)
        self.assertEqual(used, [])
        self.assertEqual(stats["access_state"], "RECOVERY")

    def test_a_rescue_ap_that_never_starts_is_retried_at_most_once_a_minute(self):  # I1
        self._armed_candidate()
        self.assertTrue(cfg.config_result_write("failed", details={"code": "authentication_failed",
                                                                   "restored": ["wifi_ssid"]}))
        used = []
        with mock.patch.object(cfg, "_atomic_dump", side_effect=self._fail_config_json(always=True)):
            cfg.resolve_interrupted_apply()
            nm, stats = self.run_manager(24, force_setup=False, pending="real", ap_effects=True,
                                         ap_start_failures=1000, connect_record=used)   # 2 min
        self.assertFalse(nm.wlan_ap.active())
        self.assertGreaterEqual(nm._start_ap.call_count, 2)       # the guard and the next pass
        self.assertLessEqual(nm._start_ap.call_count, 3)          # then once a minute
        self.assertEqual(used, [])
        self.assertEqual(stats["access_state"], "RECOVERY")

    def test_setup_window_ap_comes_back_after_a_failed_start(self):            # #18 I-1
        nm, stats = self.run_manager(1, ap_effects=True, ap_start_failures=1)
        self.assertTrue(nm.wlan_ap.active())                          # healed in the next pass
        self.assertEqual(nm._start_ap.call_count, 2)
        nm._connect_sta.assert_not_awaited()
        self.assertEqual(stats["access_state"], "SETUP")

    def test_unconfigured_setup_ap_comes_back_after_a_failed_start(self):     # #18 I-1
        nm, stats = self.run_manager(1, force_setup=False, networks=(), ap_effects=True,
                                     ap_start_failures=1)
        self.assertTrue(nm.wlan_ap.active())
        self.assertEqual(nm._start_ap.call_count, 2)
        nm._connect_sta.assert_not_awaited()
        self.assertEqual(stats["access_state"], "SETUP")

    def test_a_setup_ap_that_never_starts_is_retried_at_most_once_a_minute(self):  # #18 I-1
        nm, _ = self.run_manager(24, ap_effects=True, ap_start_failures=1000)   # 2 min
        self.assertFalse(nm.wlan_ap.active())
        self.assertGreaterEqual(nm._start_ap.call_count, 2)       # the entry and the next pass
        self.assertLessEqual(nm._start_ap.call_count, 3)          # then once a minute
        nm._connect_sta.assert_not_awaited()

    def test_unconfigured_ap_retry_continues_after_the_window(self):          # #18 I-1
        # 150 passes = 750 s: retries in pass 1 and every 12th pass after it, also
        # once the 600-s window is over (passes 121, 133, 145).
        nm, stats = self.run_manager(150, force_setup=False, networks=(), ap_effects=True,
                                     ap_start_failures=1000)
        self.assertEqual(nm._start_ap.call_count, 1 + 13)
        nm._connect_sta.assert_not_awaited()
        self.assertEqual(stats["access_state"], "SETUP")

    def test_window_expiry_while_blocked_says_attempts_stay_blocked(self):
        self._armed_candidate()
        with mock.patch.object(cfg, "_atomic_dump", side_effect=self._fail_config_json(always=True)):
            nm, stats = self.run_manager(121, pending="real")        # window over at pass 120
        self.log.assert_any_call(
            "INFO", "WIFI", "Setup window over; station attempts stay blocked until the "
                            "configuration rollback completes.")
        self.assertNotIn(
            mock.call("INFO", "WIFI", "Setup window over; resuming Wi-Fi connection attempts."),
            self.log.call_args_list)
        self.assertEqual(stats["access_state"], "RECOVERY")

    def test_apply_while_the_verdict_connect_runs_is_refused(self):           # r1-3
        cwd, saved = os.getcwd(), dict(cfg.CONFIG)
        os.chdir(tempfile.mkdtemp(prefix="esp-rtk-guard-"))

        def restore():
            os.chdir(cwd)
            cfg.CONFIG.clear(); cfg.CONFIG.update(saved)
            cfg._ROLLBACK_OPEN, cfg._ROLLBACK_CODE, cfg._STAGING_UNRECOVERED = False, None, False
            cfg.discard_staged_config()
        self.addCleanup(restore)
        cfg._ROLLBACK_OPEN, cfg._ROLLBACK_CODE, cfg._STAGING_UNRECOVERED = False, None, False
        cfg.discard_staged_config()
        cfg.CONFIG["wifi_ssid"], cfg.CONFIG["wifi_pass"] = "Old", "legacy-password"
        self.assertTrue(cfg.save_config())
        self.assertTrue(cfg.config_result_write("connected", details={"ssid": "Old"}))
        cfg.stage_config({"wifi_ssid": "Fixed"})                 # the user's correction, RAM only
        seen = []

        class Writer:
            def __init__(self):
                self.data = bytearray()

            def write(self, data):
                self.data.extend(data)

        async def fake_reboot():
            seen.append("reboot")

        class ApplyOnFirstAttempt(list):
            # The first station attempt of run() fires the apply handler (inside the
            # running loop, with the state the network manager set).
            def append(self, item):
                super().append(item)
                if len(self) == 1:
                    writer = Writer()
                    with mock.patch.object(web, "do_soft_reboot_deferred", fake_reboot):
                        web._handle_config_apply(writer, b"{}")
                    head, _, payload = bytes(writer.data).partition(b"\r\n\r\n")
                    seen.append((state.app.stats.get("access_state"),
                                 head.split(b"\r\n", 1)[0], payload))
        used = ApplyOnFirstAttempt()
        nm, stats = self.run_manager(2, force_setup=False, pending="real",
                                     connect_result=False, connect_record=used)
        self.assertEqual(len(seen), 1)                            # answered, no reboot
        access_state, status, payload = seen[0]
        self.assertEqual(access_state, "CONNECTING")
        self.assertIn(b"409", status)
        self.assertEqual(json.loads(payload)["error"], "config_verification_running")
        self.assertEqual(cfg.config_result_read()["details"], {"ssid": "Old"})   # unchanged
        self.assertEqual(cfg.config_result_read()["status"], "connected")
        self.assertEqual(cfg.staged_config(), {"wifi_ssid": "Fixed"})            # staging stays
        self.assertFalse(cfg.pending_config_active())
        self.assertEqual(set(used), {("Old", "legacy-password")})
