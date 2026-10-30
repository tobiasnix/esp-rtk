# SPDX-License-Identifier: AGPL-3.0-only
"""The data-basis statements of docs/technical.md and docs/integration.md follow the code."""
import os
import tempfile
import unittest
from unittest import mock

from support import state
import cfg
import tracking

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FIX = {"lat": 50.0, "lon": 8.0, "alt": 123.4, "qual": 4,
       "lat_e8": 5000000000, "lon_e8": 800000000, "alt_mm": 123400,
       "geoid_sep_mm": 45600, "fix_status_text": "RTK_FIXED", "sats": 24,
       "hdop": 0.6, "correction_age_sec": 0.5, "station_id": "0054",
       "receiver_accuracy": {"source": "NMEA_GST", "semi_major_sigma_m": 0.02,
                             "altitude_sigma_m": 0.04}}


def _release_section(version):
    """The CHANGELOG.md entry of one release, from its heading to the next one."""
    with open(os.path.join(REPO, "CHANGELOG.md"), encoding="utf-8") as handle:
        changelog = handle.read()
    start = changelog.index("## %s" % version)
    return changelog[start:changelog.index("\n## V", start + 1)]


def _read(name):
    with open(os.path.join(REPO, "docs", name), encoding="utf-8") as handle:
        return handle.read()


class DocsContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.technical = _read("technical.md")
        cls.integration = _read("integration.md")
        cls.performance = _read("performance-and-limits.md")
        cls.diagnostics = _read("field-diagnostics.md")
        cls.limitations = _read("known-limitations.md")

    def test_guided_apply_is_documented(self):
        flat = " ".join(self.technical.split())
        for text in ("Guided apply in the field app", "only the changed keys", "`GET /api/session`",
                     "sign in again", "`sessionStorage`", "no password", "no caster user name",
                     "HTTP, Wi-Fi, NTRIP and survey storage", "`restart_required`", "90 s",
                     "settings the device reports", "marked to be set back", "`system.boot_id`", "`apply_id`",
                     "`staging_revision`", "`409 staging_changed`", "observed",
                     "never repeats", "no restart lock"):
            self.assertIn(text, flat, text)
        for stale in ("an empty `PUT /api/config`", "Uptime instead of `boot_count`",
                      "settings read back from the device", "Saved on the device now"):
            self.assertNotIn(stale, flat, stale)
        limits = " ".join(self.limitations.split())
        for stale in ("Saving an unchanged configuration in the portal", "The portal ignores both answers",
                      "the page does not read `details.restored` yet"):
            self.assertNotIn(stale, limits, stale)
        for text in ("You sign in again after every restart", "A lost restart answer is only observed",
                     "Staged Wi-Fi changes from another tab", "The rollback is not reported as finished"):
            self.assertIn(text, limits, text)

    def _tracker(self):
        directory = tempfile.TemporaryDirectory(prefix="esp-rtk-docs-")
        self.addCleanup(directory.cleanup)
        patch = mock.patch.object(tracking.Tracker, "storage_capacity", return_value={
            "free_flash_bytes": 8_000_000, "required_flash_reserve_bytes": 3_000_000,
            "estimated_temporary_bytes": 16_384, "writable": True})
        patch.start()
        self.addCleanup(patch.stop)
        tracker = tracking.Tracker(os.path.join(directory.name, "tracking.jsonl"))
        tracker.create_project("Docs")
        return tracker

    def test_coordinate_statements_match_schema_5(self):
        self.assertNotIn("stored as WGS84 decimal degrees", self.technical)
        for text in ("1e-8", "legacy_float32", "schema 5", "epoch age", "arrival age",
                     "gate_deviations", "/api/field-setup"):
            self.assertIn(text, self.technical, text)
        self.assertEqual(tracking.SCHEMA_VERSION, 5)

    def test_every_csv_column_is_documented(self):
        header = self._tracker().csv().splitlines()[0].split(",")
        # The exact sequence: every column, in order, joined the way the doc prints it.
        sequence = ", ".join("`%s`" % name for name in header[:-1])
        sequence += ", and `%s`" % header[-1]
        self.assertIn(" ".join(sequence.split()), " ".join(self.technical.split()))
        self.assertIn("%d columns" % len(header), self.technical)

    def test_every_stored_measurement_field_is_documented(self):
        tracker = self._tracker()
        measurement = tracker._measurement(FIX)
        measurement["source"] = "manual"
        missing = [name for name in measurement if "`%s`" % name not in self.technical]
        self.assertEqual(missing, [])

    def test_setup_keys_and_new_fields_are_documented(self):
        names = list(tracking.FIELD_SETUP_KEYS) + [
            "fix_stale_after_ms", "ntrip_max_epoch_age_sec", "ntrip_reconnect_on_stale_sec",
            "gps_utc_offset_sec", "ntrip_substate", "stale_backlog", "fix_age_ms", "fix_seq",
            "fix_rev", "pending_gst", "arrival_age_sec", "epoch_age_sec", "epoch_source",
            "vertical_reference", "crs_note", "gate_profile", "gate_limits", "boot_count",
            "recorded_at_utc", "precision", "altitude_ellipsoid_m", "ground_height_msl_m",
            "height_reference"]
        missing = [name for name in names if name not in self.technical]
        self.assertEqual(missing, [])
        for key in tracking.FIELD_SETUP_KEYS:
            self.assertNotIn(key, cfg.SETTABLE_KEYS)

    def test_integration_states_the_coordinate_contract(self):
        for text in ("lat_e8", "lon_e8", "alt_mm", "precision", "legacy_float32",
                     "Timestamps", "arrival age", "technical.md"):
            self.assertIn(text, self.integration, text)
        self.assertEqual(self.integration.count("## Timestamps"), 1)

    def test_measurement_method_and_backup_schema_2_are_documented(self):
        flat = " ".join(self.technical.split())
        for text in ("`measurement_samples`", "`samples_default`", "`measurement_defaults`",
                     "`schema_version` 2", "`sections`", "`kept`", "not part of a gate profile",
                     "older firmware refuses", "only `verified_reboot` keys are rolled back",
                     "`prepare_restore_change`", "`CONFIG_RESTORE_BODY_LIMIT`", "38 keys are `live`"):
            self.assertIn(text, flat, text)
        for stale in ("not part of the configuration backup", "37 keys are `live`"):
            self.assertNotIn(stale, flat, stale)

    def test_geojson_frame_statement_and_corrected_claims(self):
        flat = " ".join(self.integration.split())
        self.assertIn("nominally WGS84", flat)
        self.assertIn("CRS_NOTE", flat)
        self.assertTrue(tracking.CRS_NOTE)
        technical = " ".join(self.technical.split())
        # Claims that the code contradicted: the doc names the real mechanisms.
        for text in ("cfg_store.coerce", "_configured_antenna_height_mm", "GST_GRACE_MS",
                     "attach_gst", "`tracking.csv()`", "last in arrival order",
                     "`GET /rtcm` returns `host` and `mount`", "_sync_rmc_clock"):
            self.assertIn(text, technical, text)
        import cfg_store
        import state
        self.assertTrue(callable(cfg_store.coerce))
        self.assertTrue(callable(tracking._configured_antenna_height_mm))
        self.assertTrue(callable(state.AppState.attach_gst) if hasattr(state, "AppState")
                        else callable(type(state.app).attach_gst))
        self.assertEqual(state.GST_GRACE_MS, 2000)
        performance = " ".join(self.performance.split())
        self.assertIn("TRACKING_IMPORT_LIMIT", performance)
        self.assertIn("MAX_BACKUP_BYTES", performance)

    def test_public_docs_carry_no_field_test_dates(self):
        for name in ("performance-and-limits.md", "known-limitations.md"):
            text = _read(name)
            for date in ("2026-09-15", "2026-09-16", "2026-09-17"):
                self.assertNotIn(date, text, (name, date))

    def test_position_keys_are_documented_as_absent_or_null(self):
        for name, text in (("technical.md", self.technical), ("integration.md", self.integration)):
            flat = " ".join(text.split()).lower()
            self.assertIn("before the first gga", flat, name)
            self.assertIn("check `qual` first", flat, name)
            self.assertIn("fix quality 0", flat, name)

    def test_epoch_age_is_documented_as_expiring_with_the_tick_counter(self):
        flat = " ".join(self.technical.split())
        self.assertIn("6.2 days", flat)
        self.assertIn("tick counter wraps", flat)

    def test_new_config_keys_are_documented_as_not_settable_over_http(self):
        keys = ("ntrip_max_epoch_age_sec", "ntrip_reconnect_on_stale_sec",
                "gps_utc_offset_sec", "fix_stale_after_ms")
        for key in keys:
            self.assertNotIn(key, cfg.SETTABLE_KEYS)
            self.assertIn(key, cfg.DEFAULT_CONFIG)
        flat = " ".join(self.technical.split())
        self.assertIn("are not settable through `PUT /api/config`", flat)
        self.assertIn("editing `config.json`", flat)

    def test_known_limitations_cover_the_data_basis(self):
        flat = " ".join(self.limitations.split())
        for text in ("legacy_float32", "120 s", "epoch age", "arrival age", "phase centre",
                     "WGS84/ITRF", "ETRS89", "L5", "E5a", "B2a", "1 ppm", "2000-01-01",
                     "stale_backlog", "Correction link", "_sync_rmc_clock", "_zeit_holen",
                     "gps_utc_offset_sec", "technical.md#correction-ages",
                     "geo.py", "local_scale", "corrections_too_old", "gst_required"):
            self.assertIn(text, flat, text)
        self.assertNotIn("6371 km sphere is used", flat)

    def test_known_limitations_state_what_the_set_store_does_not_cover(self):
        start = self.limitations.index("### Stakeout target sets are kept apart")
        section = self.limitations[start:self.limitations.index("\n### ", start + 1)]
        flat = " ".join(section.split())
        for text in ("`stakeout.sets/<project_id>/`", "no automatic cleanup", "unowned_folders", "REPL", "factory reset",
                     "A power cut before the rename", "`incomplete`",
                     "A damage mark from a download is RAM-only",
                     "819 200 bytes",
                     "A stray non-empty subdirectory keeps a project deletion pending",
                     "depends on the filesystem and is checked on the device",
                     "stakeout_recovery_pending", "the old sets reappear",
                     "A blocked project blocks every survey restore",
                     "cannot be listed at start-up", "`409 unsupported_project_fingerprint`"):
            self.assertIn(text, flat, text)
        self.assertNotIn("tracking.stakeout-", flat)
        self.assertNotIn("remove_project", flat)
        self.assertNotRegex(flat, r"(?<!stakeout_)recovery_pending")

    def test_project_deletion_of_the_stakeout_sets_is_documented(self):
        flat = " ".join(self.technical.split())
        for text in ("`stakeout.sets/<project_id>.deleting`", '"stakeout_cleanup": "done"',
                     '"pending"', "`stakeout_cleanup_pending`", "`stakeout_recovery_pending`",
                     "before the survey counts as loaded", "Nothing else is swept",
                     "cannot be listed at start-up", "`unsupported_project_fingerprint`",
                     "does not take a piece within 10 s"):
            self.assertIn(text, flat, text)

    def test_set_download_is_documented_as_streamed_with_a_digest_gated_trailer(self):
        flat = " ".join(self.technical.split())
        self.assertNotIn("reads the whole set file into RAM", flat)
        self.assertNotRegex(flat, r"(?<!stakeout_)recovery_pending")
        for text in ("streams the set file from flash in 1 KiB pieces",
                     "The trailer is sent only when the digest equals `body_sha256`",
                     "`stakeout.sets/<project_id>/<set_id>.json`", "`load_block_ms`",
                     "a listing never renames or removes anything"):
            self.assertIn(text, flat, text)

    def test_stakeout_recheck_names_every_refusing_condition(self):
        flat = " ".join(self.technical.split())
        start = flat.index("A stakeout set file is not written through that sequence.")
        paragraph = flat[start:flat.index("no set is stored.", start)]
        for text in ("recording", "measurement", "storage maintenance",
                     "parallel benchmark", "project still exists"):
            self.assertIn(text, paragraph, text)

    def test_stakeout_session_and_live_guidance_are_documented(self):
        flat = " ".join(self.technical.split())
        for text in ("### Stakeout session and live guidance", "`GET /api/stakeout/live`",
                     "`/api/stakeout/settings`", "`project_not_active`", "`project_changed`",
                     "`target_not_found`", "`part_not_found`", "`invalid_stakeout_settings`",
                     "within 900 bytes", "1400 bytes", "target minus the rover",
                     "`schema_version` 3", "`stakeout` section"):
            self.assertIn(text, flat, text)
        for state in ("no_target", "no_position", "far", "coarse", "fine", "settling", "ready"):
            self.assertIn("| `%s` |" % state, flat, state)
        limits = " ".join(self.limitations.split())
        for text in ("### The active stakeout target and its progress", "no compass",
                     "no target name"):
            self.assertIn(text, limits, text)

    def test_stakeout_confirmation_and_progress_are_documented(self):
        flat = " ".join(self.technical.split())
        for text in ("### Stakeout progress and confirmation", "`stakeout_progress.py`",
                     "`stakeout_export.py`", "`progress.jsonl`", "`expect_gen`",
                     "`target_changed`", "`target_not_open`", "`target_open`",
                     "`stakeout_write_uncertain`", "`GET /api/stakeout/progress`",
                     "`GET /api/stakeout/export`", "state at export start",
                     "`unowned_folders`", "`stakeout_data`", "1 000 000",
                     "`no_position`", "`quality_rejected`"):
            self.assertIn(text, flat, text)
        limits = " ".join(self.limitations.split())
        for text in ("until the next restart", "all targets of the affected projects are open again",
                     "Undo of a confirming stakeout point reopens the target",
                     "stay hidden", "`unowned_folders`", "state at the start of the export",
                     "single points", "not part of the survey backup",
                     "before V16.0.0 carry no stakeout flag"):
            self.assertIn(text, limits, text)
        self.assertNotIn("kept in RAM only", limits)

    def test_field_app_use_of_the_event_stream_is_documented(self):
        flat = " ".join(self.technical.split())
        for text in ("How the field app uses it", "22 s", "30 s", "300 s",
                     "has no freshness rule of its own", "`requestSerial`",
                     "reopens the stream", "650 ms", "ignores `live` frames"):
            self.assertIn(text, flat, text)
        section = " ".join(_release_section("V16.0.0").split())
        self.assertIn("keeps one stream open on the measure and device pages", section)
        self.assertIn("reopens it after a confirmed change of your own", section)

    def test_performance_doc_references_the_age_definition_and_labels_measurements(self):
        flat = " ".join(self.performance.split())
        for text in ("arrival age", "epoch age", "bytes per point", "host-emulated",
                     "technical.md#correction-ages", "1 ppm", "V15.7.3", "V15.7.4",
                     "ARCHIVE_VERTEX_BYTES"):
            self.assertIn(text, flat, text)
        self.assertEqual(tracking.ARCHIVE_VERTEX_BYTES, 768)
        self.assertIn("768", flat)

    def test_field_diagnostics_documents_the_recorder_fields(self):
        flat = " ".join(self.diagnostics.split())
        for text in ("fix_stale", "epoch_age_sec", "substate", "ntrip_substate", "stale_backlog",
                     "alt_ellipsoid_mm", "fix_age_ms", "lat_e8", "technical.md#correction-ages",
                     "a stream boundary"):
            self.assertIn(text, flat, text)
        # The names the doc relies on exist in the recorder.
        with open(os.path.join(REPO, "field_diagnostics.py"), encoding="utf-8") as handle:
            source = handle.read()
        for name in ("alt_ellipsoid_mm", "epoch_age_sec", "substate", "fix_stale", "fix_age_ms"):
            self.assertIn('"%s"' % name, source, name)

    def test_correction_age_is_defined_once(self):
        # The three ages are defined in technical.md only; the other documents link to it.
        self.assertEqual(self.technical.count("### Correction ages"), 1)
        for name in ("performance-and-limits.md", "field-diagnostics.md", "known-limitations.md"):
            self.assertNotIn("**Arrival age**", _read(name), name)
            self.assertNotIn("**Epoch age**", _read(name), name)

    def test_readmes_and_changelog_state_the_data_basis(self):
        def read_root(name):
            with open(os.path.join(REPO, name), encoding="utf-8") as handle:
                return handle.read()

        for name in ("README.md", "README.de.md"):
            flat = " ".join(read_root(name).split())
            for text in ("legacy_float32", "V15.9.0", "docs/known-limitations.md"):
                self.assertIn(text, flat, (name, text))
        raw = _release_section("V16.0.0")
        section = " ".join(raw.split())
        for text in ("schema 5", "fix_age_ms", "epoch_age_sec", "antenna_height_m",
                     "reference_frame", "recorded_at_utc", "legacy_float32", "lat_e8",
                     "gate_deviations", "ntrip_substate", "stale_backlog",
                     "legacy_conversion_errors", "tracking.checkpoint.json.v4",
                     "gga_parse_us", "not readable by older firmware"):
            self.assertIn(text, section, text)
        # One coherent entry per theme, not one bullet per implementation step.
        groups = raw.split("\n### ")[1:]
        self.assertGreaterEqual(len(groups), 2)
        for group in groups:
            self.assertLessEqual(group.count("\n- "), 16, group.splitlines()[0])
        self.assertTrue(groups[0].startswith("Upgrade notes"))
        self.assertIn("docs/known-limitations.md", groups[-1])
        # Internal versions are folded into the release entry, not listed on their own.
        changelog = read_root("CHANGELOG.md")
        for internal in ("## V15.9.", "## V15.10."):
            self.assertNotIn(internal, changelog, internal)


if __name__ == "__main__":
    unittest.main()
