# SPDX-License-Identifier: AGPL-3.0-only
"""#38b Task 3: the service CRS of the open stream as a field-setup suggestion.

RtcmCounter.frame_suggestion(setup) rates the decoded 1300 against the field setup;
web.frame_suggestion_view() offers it in /api/ui/live (frame_suggestion) and
/api/field-setup (suggestion); /status (under rtcm) and /rtcm (top level) carry the
raw CRS fields. Nothing is saved without a PUT. The suggestion lives only as long as
the stream that announced it and its endpoint stay (E7)."""
import asyncio
import json
import tempfile
import unittest
from unittest import mock

from support import cfg, net, state, web
from field_diagnostics import FieldRecorder
from test_live_snapshot import FakeWriter, contract_env, response_body
from test_rtcm import msm_epoch
from test_rtcm_service_crs import crs_1300, crs_1302, todays_caster_stream
import test_ntrip_flow

ETRS89_2010 = crs_1300("ETRS89", 11000)
SUGGESTED = {"reference_frame": "ETRS89", "reference_frame_epoch": "2010.0"}
SUMMARY_KEYS = {"messages", "msm", "bytes_total", "last_msg_age_sec", "arrival_age_sec",
                "epoch_age_sec", "epoch_source", "stale"}


def call(handler, *args):
    writer = FakeWriter()
    asyncio.run(handler(writer, *args))
    return writer


def field_setup_request(method="GET", payload=None):
    body = b"" if payload is None else json.dumps(payload).encode()
    return call(web._handle_field_setup_request, method, body, False, {})


def status_request(path):
    with mock.patch.object(web, "_read_allowed", return_value=True):
        return response_body(call(web._handle_status_request, path, "", False, {}))


class TestFrameSuggestion(unittest.TestCase):
    """The deterministic contract environment with its real NtripClient."""

    def setUp(self):
        self.now = self.enterContext(contract_env())
        self.client = web._instances["ntrip"]
        self.dump = self.enterContext(mock.patch.object(cfg, "_atomic_dump",
                                                        return_value=True))
        self.enterContext(mock.patch.object(web, "_admin_allowed", return_value=True))

    def view_for(self, frame, epoch):
        with mock.patch.dict(cfg.CONFIG, {"reference_frame": frame,
                                          "reference_frame_epoch": epoch}):
            return web.frame_suggestion_view()

    def test_suggestion_states(self):
        self.client.rtcm.feed(ETRS89_2010)
        for (frame, epoch), expected in ((("", ""), "new"),
                                         (("ETRS89", "2010.0"), "matches"),
                                         (("ETRS89", ""), "differs"),
                                         (("ETRS89", "2020.0"), "differs"),
                                         (("etrs89", "2010.0"), "differs"),
                                         (("ITRF2014", ""), "differs")):
            with self.subTest(frame=frame, epoch=epoch):
                view = self.view_for(frame, epoch)
                self.assertEqual(view, dict(SUGGESTED, anchor=None, state=expected))
        # The setup is compared stripped.
        self.assertEqual(self.client.rtcm.frame_suggestion(
            {"reference_frame": " ETRS89 ", "reference_frame_epoch": "2010.0 "})["state"],
            "matches")
        self.client.rtcm.feed(crs_1300("ETRS89", 0))
        view = self.view_for("ETRS89", "2010.0")
        self.assertEqual((view["state"], view["reference_frame_epoch"]), ("matches", ""))

    def test_anchor_comes_from_a_matching_1302_only(self):
        rtcm = self.client.rtcm
        rtcm.feed(ETRS89_2010 + crs_1302("ETRS89", 1, 7))
        self.assertEqual(self.view_for("", "")["anchor"], "plate_fixed")
        rtcm.feed(crs_1302("ITRF2014", 0, 0))
        self.assertIsNone(self.view_for("", "")["anchor"])
        only_1302 = net.RtcmCounter()
        only_1302.feed(crs_1302("ETRS89", 1, 7))
        self.assertIsNotNone(only_1302.rtcm_crs)
        self.assertIsNone(only_1302.frame_suggestion({"reference_frame": "",
                                                      "reference_frame_epoch": ""}))

    def test_field_setup_get_offers_the_suggestion_and_changes_nothing(self):
        self.client.rtcm.feed(ETRS89_2010)
        body = response_body(field_setup_request())
        self.assertEqual(body["suggestion"], dict(SUGGESTED, anchor=None, state="new"))
        self.assertEqual(body["values"]["reference_frame"], "")
        self.assertEqual(cfg.CONFIG["reference_frame"], "")
        self.dump.assert_not_called()

    def test_live_snapshot_offers_the_suggestion_without_saving(self):
        self.client.rtcm.feed(ETRS89_2010)
        snap = web.ui_live_snapshot(self.now)
        self.assertEqual(snap["frame_suggestion"]["state"], "new")
        self.assertEqual(snap["measurement_defaults"]["reference_frame"], "")
        self.dump.assert_not_called()

    def test_put_with_the_suggested_values_adopts_them(self):
        self.client.rtcm.feed(ETRS89_2010)
        body = response_body(field_setup_request("PUT", SUGGESTED))
        self.assertEqual({key: body["values"][key] for key in SUGGESTED}, SUGGESTED)
        self.assertEqual(body["suggestion"]["state"], "matches")
        self.assertEqual(web.ui_live_snapshot(self.now)["frame_suggestion"]["state"],
                         "matches")
        body = response_body(field_setup_request("PUT", {"antenna_height_m": 1.5}))
        self.assertEqual((body["values"]["antenna_height_m"],
                          body["values"]["reference_frame"],
                          body["values"]["reference_frame_epoch"]), (1.5, "ETRS89", "2010.0"))
        self.assertEqual(body["suggestion"]["state"], "matches")

    def test_failed_save_keeps_values_and_suggestion(self):
        self.client.rtcm.feed(ETRS89_2010)
        announced = self.client.rtcm.service_crs
        with mock.patch.object(cfg, "_atomic_dump", return_value=False):
            raw = bytes(field_setup_request("PUT", SUGGESTED).buf)
        self.assertIn(b"500 Internal Server Error", raw)
        self.assertIn(b"save_failed", raw)
        self.assertEqual(cfg.CONFIG["reference_frame"], "")
        self.assertEqual(response_body(field_setup_request())["suggestion"]["state"], "new")
        self.assertEqual(web.ui_live_snapshot(self.now)["frame_suggestion"]["state"], "new")
        self.assertIs(self.client.rtcm.service_crs, announced)

    def test_no_1300_means_no_suggestion_and_the_reminder_inputs_stay(self):
        self.client.rtcm.feed(todays_caster_stream(38, rounds=40))
        self.assertEqual(self.client.rtcm.crs_summary()["crs_frames"],
                         {"crc_failures": 0, "rejected": 0})
        snap = web.ui_live_snapshot(self.now)
        self.assertIsNone(snap["frame_suggestion"])
        self.assertEqual(snap["measurement_defaults"]["reference_frame"], "")
        self.assertEqual(snap["current_fix"]["qual"], 4)
        self.assertIsNone(response_body(field_setup_request())["suggestion"])
        with mock.patch.dict(web._instances, {}, clear=True):
            self.assertIsNone(web.frame_suggestion_view())
            self.assertIsNone(web.ui_live_snapshot(self.now)["frame_suggestion"])
            self.assertIsNone(response_body(field_setup_request())["suggestion"])

    def test_status_and_rtcm_report_the_decoded_crs(self):
        self.client.rtcm.feed(ETRS89_2010 + crs_1302("ETRS89", 1, 7))
        rtcm = status_request("/status")["rtcm"]
        self.assertEqual(set(rtcm["service_crs"]), {"name", "epoch", "epoch_raw", "age_sec"})
        self.assertEqual(set(rtcm["rtcm_crs"]),
                         {"name", "anchor", "plate", "database_links", "age_sec"})
        self.assertEqual((rtcm["service_crs"]["name"], rtcm["service_crs"]["epoch"],
                          rtcm["rtcm_crs"]["anchor"]), ("ETRS89", "2010.0", "plate_fixed"))
        self.assertEqual(rtcm["crs_frames"], {"crc_failures": 0, "rejected": 0})
        report = status_request("/rtcm")
        self.assertNotIn("rtcm", report)
        self.assertEqual((report["service_crs"], report["rtcm_crs"], report["crs_frames"]),
                         (rtcm["service_crs"], rtcm["rtcm_crs"], rtcm["crs_frames"]))
        self.assertNotIn("age_sec", self.client.rtcm.service_crs)
        self.assertNotIn("age_sec", self.client.rtcm.rtcm_crs)
        none = {"service_crs": None, "rtcm_crs": None,
                "crs_frames": {"crc_failures": 0, "rejected": 0}}
        with mock.patch.dict(web._instances, {}, clear=True):
            rtcm = status_request("/status")["rtcm"]
            report = status_request("/rtcm")
        self.assertEqual({key: rtcm[key] for key in none}, none)
        self.assertEqual({key: report[key] for key in none}, none)

    def test_summary_keys_are_unchanged(self):
        counter = net.RtcmCounter()
        self.assertEqual(set(counter.summary()), SUMMARY_KEYS)
        counter.feed(ETRS89_2010 + crs_1302("ETRS89", 1, 7))
        self.assertIsNotNone(counter.service_crs)
        self.assertEqual(set(counter.summary()), SUMMARY_KEYS)

    def test_field_diagnostic_sample_names_the_service_crs(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        recorder = FieldRecorder(folder + "/diagnostic", segment_bytes=4096, max_segments=3)
        recorder.start()                            # enabled, with its probe
        with mock.patch.dict(state._instances, {"ntrip": self.client}, clear=True):
            recorder.sample()
            self.assertIsNone(recorder.events[-1]["data"]["ntrip"]["service_crs"])
            self.client.rtcm.feed(ETRS89_2010)
            recorder.sample()
        sampled = recorder.events[-1]["data"]["ntrip"]["service_crs"]
        self.assertIs(sampled, self.client.rtcm.service_crs)
        self.assertEqual(sampled, {"name": "ETRS89", "epoch": "2010.0", "epoch_raw": 11000})
        with mock.patch.dict(state._instances, {}, clear=True):
            recorder.sample()
        self.assertIsNone(recorder.events[-1]["data"]["ntrip"]["service_crs"])


# Stream A announces ETRS89 (2010.0) in its first chunk; fresh MSM epochs follow.
STREAM_A = {"chunks": [(0, ETRS89_2010 + msm_epoch(1075, 78_000)),
                       (1000, msm_epoch(1075, 79_000)), (1000, msm_epoch(1075, 80_000))]}
STREAM_B_WITHOUT_1300 = {"chunks": [(0, msm_epoch(1075, 81_000)),
                                    (1000, msm_epoch(1075, 82_000))]}
UNREACHABLE = {"connect_error": OSError(113)}
NO_FRAME = {"reference_frame": "", "reference_frame_epoch": ""}


class TestSuggestionFollowsTheStream(unittest.IsolatedAsyncioTestCase):
    """The real NTRIP loop and #23b reload: a suggestion never outlives its stream."""

    # The scripted caster of TestNtripFlow and its helpers, without running its tests twice.
    run_caster = test_ntrip_flow.TestNtripFlow.run_caster
    end_events = test_ntrip_flow.TestNtripFlow.end_events
    backoff_delays = test_ntrip_flow.TestNtripFlow.backoff_delays
    hosts = test_ntrip_flow.TestNtripFlow.hosts

    def view(self):
        with mock.patch.dict(web._instances, {"ntrip": self.client}, clear=True):
            return web.frame_suggestion_view()

    def reasons(self):
        return [d["reason"] for d, _ in self.end_events()]

    def on_read(self, number, change, seen):
        """At read `number`: the view, change() (a saved config plus reload()), the view."""
        def hook(n):
            if n == number:
                seen.append(self.view())
                change()
                seen.append(self.view())
        return hook

    async def test_switching_ntrip_off_drops_the_suggestion_at_once(self):
        seen, idle = [], []

        def switch_off():
            cfg.CONFIG["ntrip_enabled"] = False
            self.client.reload()

        def stop_when():
            if self.states[-1] == "disabled":
                idle.append((self.view(), self.client.rtcm.service_crs))
                return True
            return False
        await self.run_caster(require_gga=False, extra_config=NO_FRAME,
                              on_read=self.on_read(2, switch_off, seen),
                              attempts=[STREAM_A], stop_when=stop_when)
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0]["state"], "new")
        self.assertIsNone(seen[1])                  # in the reload's own call
        self.assertEqual(idle[:1], [(None, None)])  # the first idle slice
        self.assertEqual(self.reasons(), ["reload"])

    async def test_switching_to_a_failing_caster_drops_the_suggestion(self):
        seen, backoff = [], []

        def reload_new():
            cfg.CONFIG.update(test_ntrip_flow.NEW)
            self.client.reload()

        def stop_when():
            if self.backoff_delays():
                backoff.append(self.view())
                return True
            return False
        await self.run_caster(require_gga=False, extra_config=NO_FRAME,
                              on_read=self.on_read(2, reload_new, seen),
                              attempts=[STREAM_A, UNREACHABLE], stop_when=stop_when)
        self.assertEqual(seen[0]["state"], "new")
        self.assertIsNone(seen[1])
        self.assertEqual(backoff, [None])
        self.assertEqual(self.hosts(), ["caster.invalid", "new.invalid"])

    async def test_an_unchanged_endpoint_reload_keeps_the_suggestion(self):
        seen, announced, later = [], [], []

        def change_fallback():
            announced.append(self.client.rtcm.service_crs)
            cfg.CONFIG.update(test_ntrip_flow.OTHER_FALLBACK)
            self.client.reload()
            announced.append(self.client.rtcm.service_crs)

        def on_read(n):
            hook(n)
            if n == 3:
                later.append(self.view())
        hook = self.on_read(2, change_fallback, seen)
        await self.run_caster(require_gga=False, extra_config=NO_FRAME, on_read=on_read,
                              attempts=[STREAM_A, UNREACHABLE],
                              stop_when=lambda: len(self.backoff_delays()) == 2)
        self.assertEqual(seen[0]["state"], "new")
        self.assertEqual(seen[1], seen[0])
        self.assertIsNotNone(announced[0])
        self.assertIs(announced[1], announced[0])
        self.assertEqual(later, [seen[0]])          # the stream goes on with it
        self.assertEqual(self.reasons(), ["eof"])
        self.assertEqual(self.hosts(), ["caster.invalid", "other.invalid"])

    async def test_a_stream_end_drops_the_suggestion(self):
        seen, backoff = [], []

        def stop_when():
            if self.backoff_delays():
                backoff.append(self.view())
            return len(self.backoff_delays()) == 2
        await self.run_caster(require_gga=False, extra_config=NO_FRAME,
                              on_read=lambda n: seen.append(self.view()) if n == 2 else None,
                              attempts=[STREAM_A, UNREACHABLE], stop_when=stop_when)
        self.assertEqual(seen[0]["state"], "new")
        self.assertEqual(self.reasons(), ["eof"])
        self.assertTrue(backoff)
        self.assertEqual(backoff, [None] * len(backoff))

    async def test_a_new_stream_must_announce_again(self):
        seen, in_b = [], []

        def reload_new():
            cfg.CONFIG.update(test_ntrip_flow.NEW)
            self.client.reload()

        def on_read(n):
            hook(n)
            if n == 4:                              # the second read of stream B
                in_b.append((self.view(), self.client.rtcm.counts.get(1075)))
        hook = self.on_read(2, reload_new, seen)
        await self.run_caster(require_gga=False, extra_config=NO_FRAME, on_read=on_read,
                              attempts=[STREAM_A, STREAM_B_WITHOUT_1300],
                              stop_when=lambda: len(self.backoff_delays()) == 1)
        self.assertEqual(seen[0]["state"], "new")
        self.assertEqual(self.hosts(), ["caster.invalid", "new.invalid"])
        self.assertEqual(self.reasons(), ["reload", "eof"])
        self.assertEqual(len(in_b), 1)
        self.assertIsNone(in_b[0][0])
        self.assertEqual(in_b[0][1], 2)             # A's first MSM and B's first


if __name__ == "__main__":
    unittest.main()
