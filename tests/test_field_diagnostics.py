# SPDX-License-Identifier: AGPL-3.0-only
"""Durability, bounded diagnostic work, frame validation and access control."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from support import state, net, gnss, web, sentence, access, FakeUART
from field_diagnostics import (FieldRecorder, CorrectionProbe, MAX_RTCM_QUEUE,
                               MAX_NMEA, SAMPLE_INTERVAL_MS, FLUSH_INTERVAL_MS,
                               FLUSH_SLICE_BYTES)
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

    def test_sample_carries_tick_based_fix_age_and_stale_flag(self):
        self.recorder.start()
        fix = {'utc': '120000.00', 'qual': 4, 'lat': 1., 'lon': 2., 'alt': 3.}
        with mock.patch.object(state.app, '_last_fix', fix), \
                mock.patch.object(state.app, '_fix_ticks_ms', 47000), \
                mock.patch.object(state.app, '_fix_expired', False), \
                mock.patch.object(state.time, 'ticks_ms', return_value=50000), \
                mock.patch.dict(state._instances, {}, clear=True):
            self.recorder.sample(); self.recorder.flush()
        saved = self.records()[-1]
        self.assertEqual(saved['data']['fix_age_ms'], 3000)
        self.assertIs(saved['data']['fix_stale'], True)

    def test_sample_carries_the_exact_integer_position_fields(self):
        self.recorder.start()
        fix = {'utc': '120000.00', 'qual': 4, 'lat': 1., 'lon': 2., 'alt': 3.,
               'lat_e8': 12345600, 'lon_e8': -65432100,
               'alt_mm': 123456, 'geoid_sep_mm': 12300}
        with mock.patch.object(state.app, '_last_fix', fix), mock.patch.dict(state._instances, {}, clear=True):
            self.recorder.sample()
            self.recorder.flush()
        saved = self.records()[-1]['data']['fix']
        for key in ('lat_e8', 'lon_e8', 'alt_mm', 'geoid_sep_mm'):
            self.assertEqual(saved[key], fix[key], key)

    def test_sample_derives_the_ellipsoid_height_from_alt_and_geoid_separation(self):
        self.recorder.start()
        fix = {'utc': '120000.00', 'qual': 4, 'alt_mm': 123456, 'geoid_sep_mm': 12300}
        with mock.patch.object(state.app, '_last_fix', fix), mock.patch.dict(state._instances, {}, clear=True):
            self.recorder.sample()
            self.recorder.flush()
        self.assertEqual(self.records()[-1]['data']['fix']['alt_ellipsoid_mm'], 135756)
        for partial in ({'utc': '120000.00', 'qual': 4, 'alt_mm': 123456},
                        {'utc': '120000.00', 'qual': 4, 'alt_mm': 123456, 'geoid_sep_mm': None}):
            with mock.patch.object(state.app, '_last_fix', partial), mock.patch.dict(state._instances, {}, clear=True):
                self.recorder.sample()
                self.recorder.flush()
            saved = self.records()[-1]['data']['fix']
            self.assertIn('alt_ellipsoid_mm', saved)
            self.assertIsNone(saved['alt_ellipsoid_mm'])

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
        client = net.NtripClient(gnss.UartTx(FakeUART()))
        client._flow.update({'read_timeouts': 2, 'rx_chunks': 100,
                             'connections': 1,
                             'dns_ms': 12, 'dns_max_ms': 900, 'connect_ms': 30,
                             'connect_max_ms': 4700, 'dns_unseparated': 0})
        client.tx.counters['uart_write_max_ms'] = 7
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
        self.assertEqual(data['ntrip']['flow']['dns_max_ms'], 900)
        self.assertEqual(data['ntrip']['flow']['connect_max_ms'], 4700)
        self.assertEqual(data['ntrip']['flow']['dns_unseparated'], 0)
        self.assertEqual(data['ntrip']['flow']['uart_write_max_ms'], 7)   # from UartTx (#28b)
        self.assertEqual(data['storage'], {'flush_count': before,
                                          'last_write_ms': 2500, 'max_write_ms': 3100})
        self.assertNotIn('host', data['ntrip']['flow'])
        self.assertEqual(self.recorder.flush_count, before + 1)
        self.assertIsNone(data['ntrip']['epoch_age_sec'])
        self.assertIsNone(data['ntrip']['substate'])

    def loop_lag_samples(self):
        self.recorder.flush()
        return [r['data']['loop_lag_max_ms'] for r in self.records() if r['kind'] == 'sample']

    def test_sample_reports_loop_lag_since_the_previous_sample(self):
        with mock.patch.dict(state.app.stats, {'loop_lag_ms': 0, 'loop_lag_max_ms': 0}), \
                mock.patch.object(state.app, '_loop_lag_window_max_ms', None), \
                mock.patch.dict(state._instances, {}, clear=True):
            self.recorder.start()
            for lag in (40, 900, 10):
                state.app.note_loop_lag(lag)
            self.recorder.sample(); self.recorder.sample()
            lags = self.loop_lag_samples()
        self.assertEqual(lags, [900, None])

    def test_recording_start_discards_lag_from_before_the_recording(self):
        with mock.patch.dict(state.app.stats, {'loop_lag_ms': 0, 'loop_lag_max_ms': 0}), \
                mock.patch.object(state.app, '_loop_lag_window_max_ms', None), \
                mock.patch.dict(state._instances, {}, clear=True):
            state.app.note_loop_lag(3000)                  # boot phase before the first start
            self.recorder.start(); state.app.note_loop_lag(50); self.recorder.sample()
            self.recorder.stop(); state.app.note_loop_lag(2500)   # pause between recordings
            self.recorder.start(); self.recorder.sample()
            state.app.note_loop_lag(70); self.recorder.sample()
            lags = self.loop_lag_samples()
        self.assertEqual(lags, [50, None, 70])

    def test_first_sample_after_start_waits_one_interval_and_carries_ticker_lag(self):
        # run() was idle long enough for its sampling deadline to have elapsed; the
        # 100 ms ticker notes lag on every tick. start() discards the lag window, so a
        # sample taken right after it would report null although the ticker is healthy.
        clock, stop, taken = [0], [False], []
        start_at = 60000

        async def fake_sleep(ms):
            before = clock[0]
            clock[0] += ms
            if clock[0] // 100 != before // 100:
                state.app.note_loop_lag(5)
            if not self.recorder.enabled and clock[0] >= start_at and not taken:
                self.recorder.start()
            if clock[0] >= start_at + 2 * SAMPLE_INTERVAL_MS:
                stop[0] = True

        real_sample = self.recorder.sample

        def sample():
            taken.append(clock[0])
            real_sample()

        with mock.patch.dict(state.app.stats, {'loop_lag_ms': 0, 'loop_lag_max_ms': 0}), \
                mock.patch.object(state.app, '_loop_lag_window_max_ms', None), \
                mock.patch.dict(state._instances, {}, clear=True), \
                mock.patch.object(state, 'shutdown_event', SimpleNamespace(is_set=lambda: stop[0])), \
                mock.patch.object(time, 'ticks_ms', side_effect=lambda: clock[0]), \
                mock.patch('field_diagnostics.asyncio.sleep_ms', fake_sleep), \
                mock.patch.object(self.recorder, 'sample', sample):
            asyncio.run(self.recorder.run())
            lags = self.loop_lag_samples()
        self.assertTrue(taken)
        self.assertGreaterEqual(taken[0], start_at + SAMPLE_INTERVAL_MS)
        self.assertEqual(lags[0], 5)
        self.assertNotIn(None, lags)

    def statvfs_with_headroom(self, headroom):
        """statvfs tuple whose reserve headroom (free - total // 5) is exactly headroom."""
        return (1, 1, 1000000, 200000 + headroom, 200000 + headroom, 0, 0, 0, 0, 255)

    def big_recorder(self):
        return FieldRecorder(self.prefix, segment_bytes=65536)

    def test_reserve_budget_skips_statvfs_for_small_batches_in_one_segment(self):
        recorder = self.big_recorder()
        with mock.patch('field_diagnostics.os.statvfs', wraps=os.statvfs) as statvfs:
            recorder.start()
            for i in range(5):
                recorder.event('marker', {'number': i})
                recorder.flush()
        self.assertEqual(statvfs.call_count, 1)
        self.assertEqual([r['data']['number'] for r in self.records(recorder) if r['kind'] == 'marker'],
                         list(range(5)))

    def test_every_new_segment_gets_a_real_reserve_check(self):
        with mock.patch('field_diagnostics.os.statvfs', wraps=os.statvfs) as statvfs:
            self.recorder.start()
            for i in range(20):
                self.recorder.event('test', {'padding': 'a' * 800, 'number': i})
                self.recorder.flush()
        self.assertGreaterEqual(self.recorder.sequence, 3)
        self.assertGreaterEqual(statvfs.call_count, self.recorder.sequence)

    def test_exhausted_reserve_budget_checks_again(self):
        recorder = self.big_recorder()
        # Headroom just above what the start batch (header + start record) requires.
        with mock.patch('field_diagnostics.os.statvfs',
                        return_value=self.statvfs_with_headroom(8192 + 400)) as statvfs:
            recorder.start()
            self.assertEqual(statvfs.call_count, 1)
            self.assertIsNone(recorder.error)
            recorder.event('marker')
            recorder.flush()
        self.assertEqual(statvfs.call_count, 2)
        self.assertIsNone(recorder.error)
        self.assertEqual(self.records(recorder)[-1]['kind'], 'marker')

    def test_reserve_shortfall_without_removable_segments_reports_error_and_clears_budget(self):
        recorder = self.big_recorder()
        with mock.patch('field_diagnostics.os.statvfs',
                        return_value=self.statvfs_with_headroom(8192 + 400)) as statvfs:
            recorder.start()
            self.assertGreater(recorder._reserve_budget, 0)
            statvfs.return_value = self.statvfs_with_headroom(100)
            recorder.event('marker')
            recorder.flush()
        self.assertEqual(recorder.status()['error'], 'storage_reserve')
        self.assertEqual(recorder._reserve_budget, 0)
        self.assertEqual(len(recorder.files), 1)
        self.assertEqual(recorder.status()['pending_records'], 1)

    def test_sliced_flush_writes_one_batch_up_to_the_slice_size(self):
        recorder = self.big_recorder()
        recorder.start()
        for i in range(3):
            recorder.event('test', {'padding': 'a' * 1400, 'number': i})
        recorder.flush(FLUSH_SLICE_BYTES)
        self.assertEqual(len(recorder.events), 1)
        self.assertEqual(recorder.events[0]['data']['number'], 2)
        self.assertEqual([r['data']['number'] for r in self.records(recorder) if r['kind'] == 'test'], [0, 1])
        self.assertIsNone(recorder.error)

    def test_sliced_flush_writes_a_record_larger_than_the_slice_alone(self):
        recorder = self.big_recorder()
        recorder.start()
        recorder.event('test', {'padding': 'a' * 10000, 'number': 0})
        recorder.event('test', {'number': 1})
        recorder.flush(FLUSH_SLICE_BYTES)
        self.assertIsNone(recorder.error)
        self.assertEqual(len(recorder.events), 1)
        self.assertEqual(recorder.events[0]['data']['number'], 1)
        self.assertEqual([r['data']['number'] for r in self.records(recorder) if r['kind'] == 'test'], [0])

    def drive_run(self, stop_at, statvfs=os.statvfs):
        """Run run() on a fake clock until stop_at; log flush calls, sleeps,
        statvfs calls and segment opens in order."""
        clock, log = [0], []
        recorder = self.recorder
        real_flush = recorder.flush

        async def fake_sleep(ms):
            log.append(('sleep', clock[0], ms))
            clock[0] += ms

        def flush(max_bytes=None):
            log.append(('flush', clock[0], max_bytes, [r['kind'] for r in recorder.events]))
            real_flush(max_bytes)

        def logged_statvfs(path):
            log.append(('statvfs', clock[0]))
            return statvfs(path)

        def logged_open(path, mode='r'):
            log.append(('open', clock[0], os.path.basename(path), mode))
            return open(path, mode)

        with mock.patch.dict(state.app.stats, {'loop_lag_ms': 0, 'loop_lag_max_ms': 0}), \
                mock.patch.object(state.app, '_loop_lag_window_max_ms', None), \
                mock.patch.dict(state._instances, {}, clear=True), \
                mock.patch.object(state, 'shutdown_event', SimpleNamespace(is_set=lambda: clock[0] >= stop_at)), \
                mock.patch.object(time, 'ticks_ms', side_effect=lambda: clock[0]), \
                mock.patch('field_diagnostics.asyncio.sleep_ms', fake_sleep), \
                mock.patch('field_diagnostics.os.statvfs', logged_statvfs), \
                mock.patch('field_diagnostics.open', logged_open, create=True), \
                mock.patch.object(recorder, 'flush', flush):
            asyncio.run(recorder.run())
        return log

    def test_run_drains_due_records_in_slices_between_loop_iterations(self):
        self.recorder = self.big_recorder()
        self.recorder.start()
        for i in range(8):
            self.recorder.event('test', {'padding': 'a' * 1400, 'number': i})
        # Samples (every 5 s) queue behind the test records; the final flush must
        # find no test record left, so only the drain can have written them.
        log = self.drive_run(FLUSH_INTERVAL_MS + 2000)
        sliced = [index for index, entry in enumerate(log)
                  if entry[0] == 'flush' and entry[2] == FLUSH_SLICE_BYTES]
        # 8 test records and the 6 samples up to the due time, all about 1.6 KB,
        # go two per 4 KB slice into the one 64 KB segment: 4 + 3 slices.
        self.assertEqual([log[index][3][0] for index in sliced], ['test'] * 4 + ['sample'] * 3)
        self.assertTrue(all(log[index][1] >= FLUSH_INTERVAL_MS for index in sliced))
        for first, second in zip(sliced, sliced[1:]):
            self.assertTrue(any(entry[0] == 'sleep' for entry in log[first + 1:second]))
        final = log[-1]
        self.assertEqual(final[:3], ('flush', FLUSH_INTERVAL_MS + 2000, None))
        self.assertNotIn('test', final[3])
        self.assertEqual([r['data']['number'] for r in self.records() if r['kind'] == 'test'],
                         list(range(8)))
        self.assertIsNone(self.recorder.error)

    def test_run_stops_draining_after_a_write_error_until_the_next_interval(self):
        self.recorder = self.big_recorder()
        self.recorder.start()
        self.recorder.event('marker')
        with mock.patch.object(self.recorder, '_space', side_effect=OSError('storage_reserve')):
            log = self.drive_run(2 * FLUSH_INTERVAL_MS + 1000)
        sliced = [entry[1] for entry in log if entry[0] == 'flush' and entry[2] == FLUSH_SLICE_BYTES]
        # Exactly one slice per due interval, not one per 50 ms loop iteration.
        self.assertEqual(sliced, [FLUSH_INTERVAL_MS, 2 * FLUSH_INTERVAL_MS])
        self.assertEqual(self.recorder.status()['error'], 'storage_reserve')

    def segment_creations(self, log):
        return [index for index, entry in enumerate(log) if entry[0] == 'open' and entry[3] == 'wb']

    def test_run_checks_the_reserve_for_a_new_segment_one_pass_before_writing_it(self):
        self.recorder.start()                       # 4096-byte segments, ring of 3
        for i in range(4):
            self.recorder.event('test', {'padding': 'a' * 1400, 'number': i})
        log = self.drive_run(FLUSH_INTERVAL_MS + 2000)
        created = self.segment_creations(log)
        self.assertGreaterEqual(len(created), 3)    # including ring removals
        # One statvfs per new segment and none for the slices in between.
        self.assertEqual(sum(entry[0] == 'statvfs' for entry in log), len(created))
        for index in created:
            before = [i for i in range(index) if log[i][0] in ('open', 'statvfs')]
            check = before[-1]
            # The check comes after the previous segment's last write, in an
            # earlier loop pass than the header ...
            self.assertEqual(log[check][0], 'statvfs')
            self.assertEqual(log[before[-2]][0], 'open')
            self.assertTrue(any(entry[0] == 'sleep' for entry in log[check + 1:index]))
            # ... and the header pass writes the slice too, without a new check.
            following = log[index + 1]
            self.assertEqual(following[:3], ('open', log[index][1], log[index][2]))
            self.assertEqual(following[3], 'ab')
        self.assertGreater(self.recorder.rotations, 0)
        self.assertEqual(log[-1][:3], ('flush', FLUSH_INTERVAL_MS + 2000, None))
        self.assertNotIn('test', log[-1][3])
        self.assertIsNone(self.recorder.error)

    def test_run_stops_draining_after_an_error_in_the_reserve_pass_until_the_next_interval(self):
        self.recorder = FieldRecorder(self.prefix, segment_bytes=4096)   # no ring removal
        self.recorder.start()
        for i in range(2):                          # nearly fill the first segment
            self.recorder.event('test', {'padding': 'a' * 1400, 'number': i})
            self.recorder.flush()
        self.recorder.event('marker')
        self.recorder.event('test', {'padding': 'a' * 1400, 'number': 2})
        calls, real_statvfs = [], os.statvfs     # drive_run patches os.statvfs itself

        def statvfs(path):
            calls.append(path)
            if len(calls) == 1:
                raise OSError(5)
            return real_statvfs(path)

        log = self.drive_run(2 * FLUSH_INTERVAL_MS + 1000, statvfs)
        sliced = [entry[1] for entry in log if entry[0] == 'flush' and entry[2] == FLUSH_SLICE_BYTES]
        # The failed check ends the drain: one slice in the first interval.
        self.assertEqual([t for t in sliced if t < 2 * FLUSH_INTERVAL_MS], [FLUSH_INTERVAL_MS])
        first_open = self.segment_creations(log)[0]
        # The next interval checks again in its first pass and writes in the second.
        self.assertEqual(log[first_open][1], 2 * FLUSH_INTERVAL_MS + 50)
        self.assertEqual([entry[1] for entry in log if entry[0] == 'statvfs'][:2],
                         [FLUSH_INTERVAL_MS, 2 * FLUSH_INTERVAL_MS])
        self.assertEqual([r['kind'] for r in self.records() if r['kind'] in ('marker', 'test')],
                         ['test', 'test', 'marker', 'test'])
        self.assertIsNone(self.recorder.error)

    def test_synchronous_flush_across_a_rotation_checks_once_and_writes_in_the_same_call(self):
        self.recorder.start()
        for i in range(3):                          # one record per 2 KB batch
            self.recorder.event('test', {'padding': 'a' * 1400, 'number': i})
        with mock.patch('field_diagnostics.os.statvfs', wraps=os.statvfs) as statvfs:
            self.recorder.flush()
        self.assertEqual(self.recorder.sequence, 2)
        self.assertEqual(statvfs.call_count, 1)
        self.assertEqual(self.recorder.events, [])
        with open(self.recorder.files[-1][1]) as handle:
            kinds = [json.loads(line)['kind'] for line in handle]
        self.assertEqual(kinds, ['segment', 'test'])
        self.assertIsNone(self.recorder.error)

    def test_sample_carries_the_epoch_age_and_stale_substate(self):
        from test_rtcm import SUNDAY_0001, msm_epoch
        self.recorder.start()
        client = net.NtripClient(gnss.UartTx(FakeUART()))
        with mock.patch.object(time, 'gmtime', return_value=SUNDAY_0001):
            client.rtcm.feed(msm_epoch(1075, 18_000))          # 60 s old
            with mock.patch.dict(state._instances, {'ntrip': client}, clear=True), \
                    mock.patch.dict(state.app.stats, {'ntrip_substate': 'stale'}):
                self.recorder.sample()
        self.recorder.flush()
        ntrip = self.records()[-1]['data']['ntrip']
        self.assertEqual(ntrip['epoch_age_sec'], 60.0)
        self.assertEqual(ntrip['substate'], 'stale')

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
                self.assertIn(b'401 Unauthorized', writer.buf)
                self.assertEqual(allowed.call_args.args[2], method)
        self.assertFalse(self.recorder.initialized)
        self.assertFalse(self.recorder.enabled)


if __name__ == '__main__': unittest.main()
