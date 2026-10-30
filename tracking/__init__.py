# SPDX-License-Identifier: AGPL-3.0-only
"""Crash-tolerant field mapping with lines and related asset points.

This package facade re-exports the names other modules use; the modules of
tracking/ hold the code.
"""
import os
import time
import ujson
import geo
from pointstore import FlashPointStore
try:
    import hashlib
except ImportError:  # pragma: no cover
    import uhashlib as hashlib
from cfg import CONFIG, log
from tracking.common import (ARCHIVE_VERTEX_BYTES, CHECKPOINT_BATCH_EVENTS,
                             CHECKPOINT_IO_SERVICE_MS, FLASH_RECHECK_WRITES,
                             LEGACY_SCHEMA_VERSIONS, MAX_FEATURES, MAX_LINES_PER_PROJECT,
                             MAX_LINE_GAP_M, MAX_PROJECTS, MAX_VERTICAL_STEP_M,
                             MEASUREMENT_GATE_KEYS, MIN_FLASH_RESERVE_PERCENT, OBJECT_TYPES,
                             OPERATIONAL_FLASH_HEADROOM_BYTES, SCHEMA_VERSION,
                             SEGMENT_HARD_LIMIT_BYTES, SEGMENT_MAX_BYTES, TARGET_MAX_FEATURES,
                             _EPOCH_OFFSET, _add_time_fields, _expanded_archive_record,
                             _now_seconds, _profile_id, _profile_id_valid, _sample_counts,
                             _utc_fields)
from tracking.guards import (MeasurementRejected, REQUEST_TAG_LIMIT, StorageBusy,
                             StorageReserveError, UNDO_UNCHECKED, UndoCandidateChanged,
                             _ReaderGuard)
from tracking.frames import (CRS_NOTE, FrameAssignmentError, HEIGHT_KEYS, RESERVED_FRAME_NAMES,
                             SUMMARY_REFRESH_PASSES, SUMMARY_SCAN_BATCH,
                             _FRAME_ASSIGNMENT_MESSAGES, _clean_assigned_frame, _count_frame,
                             _effective_frame, _frame_assignment, _frame_fields, _height_fields,
                             _line_frame)
from tracking.validation import (_COORDINATE_BOUNDS, _exact_component_ok, _legacy_coordinate,
                                 _upgrade_event, _validate_coordinate, _validate_legacy_coordinate,
                                 _validate_measurement, upgrade_restore_backup, validate_backup)
from tracking.fixes import (FIELD_SETUP_KEYS, _configured_antenna_height_mm, _distance_m,
                            _gate_current, _ntrip_mount, _reference_frame, _reference_frame_epoch,
                            _setup_stamps, average_fixes, field_setup, measurement_gate,
                            measurement_gate_deviations, project_to_line, quality_check,
                            receiver_sigma_stats, validate_field_setup, validate_measurement_gate)
from tracking.model import ACTIVITY_EVENTS, EVENT_HANDLERS, MAP_CHANGE_BUILDERS, _event_activity
from tracking.core import Tracker


tracker = Tracker(autoload=False, point_store_autoload=False)
