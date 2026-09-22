# SPDX-License-Identifier: AGPL-3.0-only
"""Durability, bounded diagnostic work, frame validation and access control."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from support import state, net, gnss, web, sentence, access
from field_diagnostics import (FieldRecorder, CorrectionProbe, MAX_RTCM_QUEUE,
                               MAX_NMEA, SAMPLE_INTERVAL_MS, FLUSH_INTERVAL_MS)
from test_http import FakeWriter


def crc24(data):
    result = 0
    for byte in data:
        result ^= byte << 16
        for _ in range(8):
            result <<= 1
            if result & 0x1000000:
                result ^= 0x1864cfb
    return result


def frame(payload):
    prefix = b'\xd3' + len(payload).to_bytes(2, 'big') + payload
    return prefix + crc24(prefix).to_bytes(3, 'big')


def msm(number=1077, station=54, satellites=(1, 12), signals=(2, 22)):
    values = format(number, '012b') + format(station, '012b') + format(345000, '030b')
    values += '0' * 19
    values += ''.join('1' if i in satellites else '0' for i in range(1, 65))
    values += ''.join('1' if i in signals else '0' for i in range(1, 33))
    values += '1' * (len(satellites) * len(signals))
    values += '0' * ((-len(values)) % 8)
    return frame(int(values, 2).to_bytes(len(values) // 8, 'big'))


class ProbeTests(unittest.TestCase):
    def test_fragmented_crc_verified_masks_and_station(self):
        probe = CorrectionProbe()
        encoded = msm()
        for byte in b'noise' + encoded:
            probe.feed(bytes([byte]))
        result = probe.snapshot()
        self.assertEqual(result['types'], {'1077': 1})
        self.assertEqual(result['msm'][0]['signal_ids'], [2, 22])
        self.assertEqual(result['msm'][0]['satellite_ids'], [1, 12])
        self.assertEqual(result['msm'][0]['station'], 54)
        self.assertEqual(result['msm'][0]['epoch_raw'], 345000)
        self.assertEqual(result['msm'][0]['active_cells'], 4)
        self.assertEqual(probe.snapshot()['types'], {})

    def test_bad_crc_is_rejected_and_next_frame_recovers(self):
        probe = CorrectionProbe()
        bad = bytearray(msm());bad[-1] ^= 1
        probe.feed(bad + msm(1097))
        result = probe.snapshot()
        self.assertEqual(result['types'], {'1097': 1})
        self.assertEqual(result['crc_failures'], 1)

    def test_multiple_masks_same_type_survive_and_memory_is_bounded(self):
        probe = CorrectionProbe()
        for i in range(1, 65):
            probe.feed(msm(satellites=(i,)))
        report = probe.snapshot()
        self.assertEqual(report['types'], {'1077': 64})
        self.assertEqual(len(report['msm']), 32)
        self.assertEqual(report['mask_drops'], 32)
        probe.feed(b'\xd3\x03\xff' + b'a' * 2000)
        self.assertLessEqual(len(probe.buffer), 1029)

    def test_reconnect_does_not_join_frames_from_separate_tcp_streams(self):
        probe = CorrectionProbe()
        encoded = msm()
        probe.feed(encoded[:12])
        probe.reset_stream()
        probe.feed(encoded)
        result = probe.snapshot()
        self.assertEqual(result['types'], {'1077': 1})
        self.assertEqual(result['crc_failures'], 0)
        self.assertEqual(result['truncated_frames'], 1)

    def test_short_msm_does_not_invent_signals(self):
        probe = CorrectionProbe()
        probe.feed(frame(b'\x43\x50'))
        self.assertEqual(probe.snapshot()['msm'], [])


class RecorderTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.prefix = self.folder.name + '/diagnostic'
        self.recorder = FieldRecorder(self.prefix, segment_bytes=4096, max_segments=3)

    def tearDown(self):
        self.folder.cleanup()

    def records(self, recorder=None):
        result = []
        for item in (recorder or self.recorder).files:
            with open(item[1]) as handle:
                for line in handle:
                    try: result.append(json.loads(line))
                    except ValueError: pass
        return result

    def test_off_means_no_flash_and_no_capture(self):
        self.recorder.event('ignored')
        self.recorder.observe_rtcm(msm())
        self.recorder.observe_nmea('$GPGSV')
        self.recorder.sample()
        self.assertEqual(os.listdir(self.folder.name), [])
        self.assertEqual(self.recorder.rtcm_bytes, 0)
        self.assertEqual(self.recorder.nmea, [])

    def test_resume_after_torn_tail_uses_new_boot_and_same_session(self):
        first = self.recorder
        first.start();first.event('marker', {'label': 'walking'});first.flush()
        path = first.current[1]
        with open(path, 'ab') as handle: handle.write(b'{"incomplete":')
        old_bytes = Path(path).read_bytes()
        resumed = FieldRecorder(self.prefix, segment_bytes=4096, max_segments=3)
        resumed.initialize();resumed.flush()
        self.assertTrue(resumed.enabled)
        self.assertEqual(resumed.session, first.session)
        self.assertNotEqual(resumed.boot, first.boot)
        self.assertNotEqual(resumed.current[1], path)
        self.assertEqual(Path(path).read_bytes(), old_bytes)
        self.assertEqual(self.records(resumed)[-1]['kind'], 'boot')
        resumed.stop()
        stopped = FieldRecorder(self.prefix);stopped.initialize()
        self.assertFalse(stopped.enabled)

    def test_ring_removes_only_owned_old_segments(self):
        protected = self.folder.name + '/diagnostic-user.jsonl'
        with open(protected, 'w') as handle: handle.write('keep')
        self.recorder.start()
        for i in range(80):
            self.recorder.event('test', {'padding': 'a' * 800, 'number': i})
            self.recorder.flush()
        self.assertLessEqual(len(self.recorder.files), 3)
        self.assertLessEqual(self.recorder.status()['stored_bytes'], 3 * 4096)
        self.assertEqual(Path(protected).read_text(), 'keep')
        self.assertEqual(self.records()[-1]['data']['number'], 79)
        self.assertGreater(self.recorder.rotations, 0)

    def test_connection_burst_uses_one_reserve_check_per_flash_batch(self):
        self.recorder.start()
        for _ in range(5): self.recorder.event('wifi_attempt', {'profile': 'network-1'})
        with mock.patch.object(self.recorder, '_space', wraps=self.recorder._space) as space:
            self.recorder.flush()
        self.assertEqual(space.call_count, 1)
        self.assertEqual(sum(r['kind'] == 'wifi_attempt' for r in self.records()), 5)

    def test_storage_error_is_visible_and_stop_disables_resume(self):
        self.recorder.start()
        with mock.patch.object(self.recorder, '_space', side_effect=OSError('storage_reserve')):
            self.recorder.event('marker');self.recorder.flush()
            self.assertEqual(self.recorder.status()['error'], 'storage_reserve')
            self.recorder.stop()
        resumed = FieldRecorder(self.prefix);resumed.initialize()
        self.assertFalse(resumed.enabled)

    def test_clock_reset_explicit_and_uptime_monotonic(self):
        self.recorder.start()
        before = self.recorder.stamp('before', {})
        with mock.patch('field_diagnostics.time.gmtime', return_value=(2000, 1, 1, 0, 0, 0, 0, 1)):
            invalid = self.recorder.stamp('invalid', {})
        after = self.recorder.stamp('after', {})
        self.assertIsNone(invalid['utc'])
        self.assertFalse(invalid['clock_valid'])
        self.assertTrue(after['clock_valid'])
        self.assertLessEqual(before['up_ms'], invalid['up_ms'])
        self.assertLessEqual(invalid['up_ms'], after['up_ms'])

    def test_queue_limits_report_evidence_loss(self):
        self.recorder.start()
        for _ in range(100):
            self.recorder.observe_nmea(sentence('GPGSV,1,1,01,01,30,120,40,1'))
            self.recorder.event('marker')
        self.assertEqual(len(self.recorder.nmea), MAX_NMEA)
        self.assertGreater(self.recorder.dropped_events, 0)
        self.assertEqual(self.recorder.dropped_nmea, 100 - MAX_NMEA)
        self.recorder.observe_rtcm(b'x' * MAX_RTCM_QUEUE)
        self.recorder.observe_rtcm(b'x')
        self.assertEqual(self.recorder.rtcm_bytes, 0)
        self.assertEqual(self.recorder.dropped_rtcm_bytes, MAX_RTCM_QUEUE + 1)

    def test_stationary_snapshot_contains_actual_gst_and_correction_fields(self):
        self.recorder.start()
        fix = {'utc': '120000.00', 'qual': 4, 'sats': 20, 'hdop': .5,
               'lat': 1., 'lon': 2., 'alt': 3., 'station_id': '0054', 'correction_age_sec': 2.,
               'receiver_accuracy': {'utc': '120000.00', 'horizontal_sigma_m': .003, 'altitude_sigma_m': .005}}
        with mock.patch.object(state.app, '_last_fix', fix), mock.patch.dict(state._instances, {}, clear=True):
            self.recorder.sample();self.recorder.flush()
        saved = self.records()[-1]
        self.assertEqual(saved['kind'], 'sample')
        self.assertEqual(saved['data']['fix']['correction_age_sec'], 2.)
        self.assertEqual(saved['data']['fix']['station_id'], '0054')
        self.assertEqual(saved['data']['fix']['receiver_accuracy']['altitude_sigma_m'], .005)
        self.assertEqual(saved['data']['signal_summary']['observations'], [])

    def test_nmea_is_compacted_outside_producer_path(self):
        self.recorder.start()
        self.recorder.observe_nmea(sentence('GPGSV,1,1,02,01,30,120,40,02,20,200,35,1'))
        self.recorder.observe_nmea(sentence('GNGSA,A,3,01,02,,,,,,,,,,,1.0,0.5,0.8,1'))
        self.assertEqual(len(self.recorder.nmea), 2)
        self.recorder._consume_nmea()
        self.assertEqual(self.recorder.nmea, [])
        self.recorder.sample();self.recorder.flush()
        summary = self.records()[-1]['data']['signal_summary']
        self.assertEqual(summary['observations'][0], ['GP', '1', 1, 30, 120, 40])
        self.assertEqual(summary['observations'][1], ['GP', '1', 2, 20, 200, 35])
        self.assertEqual(summary['used_satellites']['1'], [1, 2])

    def test_segment_advertises_reduced_cadence_and_batched_flush(self):
        self.recorder.start()
        header = next(r for r in self.records() if r['kind'] == 'segment')
        self.assertEqual(header['data']['schema'], 2)
        self.assertEqual(header['data']['sample_interval_ms'], SAMPLE_INTERVAL_MS)
        self.assertEqual(header['data']['flush_interval_ms'], FLUSH_INTERVAL_MS)

    def test_latest_gst_retains_its_own_epoch_before_gga_attachment(self):
        self.recorder.start()
        latest = {'utc': '120001.00', 'horizontal_sigma_m': .004, 'altitude_sigma_m': .008}
        handler = SimpleNamespace(last_gst=latest, last_gst_time=1)
        with mock.patch.object(state.app, '_last_fix', {'utc': '120000.00'}), mock.patch.dict(state._instances, {'gnss': handler}, clear=True):
            self.recorder.sample();self.recorder.flush()
        data = self.records()[-1]['data']
        self.assertIsNone(data['fix']['receiver_accuracy'])
        self.assertEqual(data['latest_gst'], latest)

    def test_sample_separates_network_silence_from_storage_delay(self):
        self.recorder.start()
        client = net.NtripClient(SimpleNamespace())
        client._flow.update({'read_timeouts': 2, 'rx_chunks': 100,
                             'connections': 1, 'uart_write_max_ms': 7})
        self.recorder.last_write_ms = 2500
        self.recorder.max_write_ms = 3100
        before = self.recorder.flush_count
        with mock.patch.dict(state._instances, {'ntrip': client}, clear=True):
            self.recorder.sample()
        self.assertEqual(self.recorder.flush_count, before)
        self.recorder.flush()
        data = self.records()[-1]['data']
        self.assertEqual(data['ntrip']['flow']['read_timeouts'], 2)
        self.assertEqual(data['ntrip']['flow']['rx_chunks'], 100)
        self.assertIsNone(data['ntrip']['flow']['rx_age_ms'])
        self.assertEqual(data['storage'], {'flush_count': before,
                                          'last_write_ms': 2500, 'max_write_ms': 3100})
        self.assertNotIn('host', data['ntrip']['flow'])
        self.assertEqual(self.recorder.flush_count, before + 1)

    def test_network_transitions_have_profile_without_credentials(self):
        self.recorder.start()
        with mock.patch.dict(state._instances, {'field_diagnostics': self.recorder}):
            old = dict(state.app.stats)
            try:
                state.app.stats['wifi_profile'] = 'network-2'
                state.app.set_access_state('CONNECTING')
                state.app.set_access_state('ONLINE')
                state.app.set_ntrip_state('connecting')
                state.app.set_ntrip_state('streaming')
            finally:
                state.app.stats.clear();state.app.stats.update(old)
        self.recorder.flush()
        transitions = [r for r in self.records() if r['kind'].endswith('_state')]
        self.assertTrue(transitions)
        self.assertTrue(all(r['data']['profile'] == 'network-2' for r in transitions))

    def test_download_excludes_active_files_and_blocks_restart_while_streaming(self):
        self.recorder.start()
        writer = FakeWriter()
        with mock.patch.object(web, 'field_recorder', self.recorder), mock.patch.object(web, '_admin_allowed', return_value=True):
            asyncio.run(web._handle_field_diagnostics(writer, '/api/field-diagnostics/export', 'GET', b'', False, {}))
        self.assertIn(b'409 Conflict', writer.buf)
        self.recorder.stop()
        recorder = self.recorder
        class RacingWriter(FakeWriter):
            async def drain(inner):
                with self.assertRaises(ValueError): recorder.start()
        writer = RacingWriter()
        with mock.patch.object(web, 'field_recorder', self.recorder), mock.patch.object(web, '_admin_allowed', return_value=True):
            asyncio.run(web._handle_field_diagnostics(writer, '/api/field-diagnostics/export', 'GET', b'', False, {}))
        self.assertIn(b'200 OK', writer.buf)
        self.assertIn(b'"kind": "stop"', writer.buf)
        self.assertEqual(self.recorder.export_readers, 0)
        self.recorder.start()

    def test_real_session_requires_csrf_and_same_origin_for_controls(self):
        access.reset()
        session, error = access.login("test-code", "test-code")
        self.assertIsNone(error)
        headers = {"cookie": "rtk_session=" + session["token"], "host": "local.test"}
        with mock.patch.object(web, 'field_recorder', self.recorder):
            def call(method, extra):
                writer = FakeWriter()
                asyncio.run(web._handle_field_diagnostics(writer, '/api/field-diagnostics', method,
                    b'{"action":"start"}', False, dict(headers, **extra)))
                return writer.buf
            self.assertIn(b'200 OK', call('GET', {}))
            self.assertIn(b'403 Forbidden', call('POST', {}))
            self.assertIn(b'403 Forbidden', call('POST', {'x-csrf-token': session['csrf'], 'origin': 'http://external.test'}))
            self.assertFalse(self.recorder.enabled)
            self.assertIn(b'200 OK', call('POST', {'x-csrf-token': session['csrf'], 'origin': 'http://local.test'}))
            self.assertTrue(self.recorder.enabled)
        access.reset()

    def test_api_authorization_and_csrf_checks_precede_initialization(self):
        with mock.patch.object(web, 'field_recorder', self.recorder), mock.patch.object(web, '_admin_allowed', return_value=False) as allowed:
            for path, method in [('/api/field-diagnostics', 'GET'), ('/api/field-diagnostics', 'POST'), ('/api/field-diagnostics/export', 'HEAD')]:
                writer = FakeWriter()
                asyncio.run(web._handle_field_diagnostics(writer, path, method, b'{"action":"start"}', False, {}))
                self.assertIn(b'403 Forbidden', writer.buf)
                self.assertEqual(allowed.call_args.args[2], method)
        self.assertFalse(self.recorder.initialized)
        self.assertFalse(self.recorder.enabled)


if __name__ == '__main__': unittest.main()
