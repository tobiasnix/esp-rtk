// SPDX-License-Identifier: AGPL-3.0-only
(function (root) {
  "use strict";
  const STORAGE_KEY = "rtk-language";
  const THEME_KEY = "rtk-theme";
  let theme = "default";
  try { theme = root.localStorage.getItem(THEME_KEY) === "high-contrast" ? "high-contrast" : "default"; }
  catch (_) {}
  const dictionaries = {
    en: {
      "language.de": "DE", "language.en": "EN",
      "app.field_app": "RTK Field App", "app.status": "Status",
      "app.projects": "Projects", "app.device": "Device",
      "nav.portal": "Portal", "nav.configuration": "Configuration",
      "nav.diagnostics": "Diagnostics", "nav.label": "Label", "ui.log": "Log",
      "action.refresh": "Refresh",
      "action.save": "Save and check connection", "action.login": "Sign in", "action.cancel": "Cancel",
      "action.logout": "Sign out", "action.download": "Download",
      "state.loading": "Loading …", "state.saved": "Saved.",
      "state.empty": "No data available.", "state.failed": "Operation failed.",
      "error.admin_session_required": "Please sign in first.",
      "error.invalid_device_code": "The device code is invalid.",
      "error.csrf_required": "An administrator session and CSRF token are required.",
      "error.invalid_request": "The request is invalid.",
      "error.internal_error": "The device encountered an internal error.",
      "ui.configuration": "Configuration", "ui.logout": "Sign out",
      "ui.high_contrast": "High contrast", "ui.settings": "Settings",
      "ui.settings_help": "Language, display and measurement preferences",
      "ui.language": "Language", "ui.display": "Display", "ui.version": "Version",
      "ui.sample_single": "Single shot", "ui.sample_avg": "Average {count} samples",
      "ui.last_point": "Last point", "ui.seconds_ago": "{seconds} s ago", "ui.storage": "Storage",
      "ui.mean_values": "Mean H {h} m · V {v} m · height {alt} m",
      "ui.name_optional": "Optional", "ui.checking_name": "Checking …",
      "ui.name_available": "available", "ui.name_taken": "already used",
      "ui.name_taken_detail": "Already used: {name}", "ui.more": "More",
      "ui.just_now": "just now", "ui.minutes_ago": "{count} min ago",
      "ui.last_activity": "last {when}", "ui.export_project": "Export · Project {name}",
      "ui.project_map": "Lines on the map", "ui.share_geojson": "Share GeoJSON", "ui.control": "Control",
      "ui.control_pending": "Second measurement missing",
      "ui.documented": "documented", "ui.deviating": "deviating", "ui.module_diagnostics": "Module & diagnostics",
      "ui.diagnostics_label": "Diagnostics & label", "ui.support_identification": "Support package and identification",
      "ui.language_outdoor": "Language · outdoor mode", "ui.auth_errors": "Auth errors", "ui.start": "Start",
      "ui.restart_hint": "Restart only if streams or the receiver no longer respond. Survey data and configuration are retained.",
      "ui.reboot_preparing": "Restart is being prepared …", "ui.reboot_waiting": "Device is offline — waiting for it to return …",
      "ui.reboot_restored": "Restart successful — connection restored.", "ui.reboot_timeout": "The device is still restarting. Check Wi-Fi and reload when its network returns.",
      "ui.gga_ntrip": "GGA sent: {gga} · NTRIP {kb} KB", "ui.connected_clients": "Connected clients",
      "ui.protocol": "Protocol", "ui.search_log_example": "Search log — e.g. WIFI",
      "ui.measurement_gate": "Measurement gate", "ui.required_fix": "Required fix",
      "ui.horizontal_limit": "Horizontal 1σ limit", "ui.vertical_limit": "Vertical 1σ limit", "ui.all_limits": "All limits",
      "ui.target": "target", "ui.measurement_released": "Measurement released",
      "ui.gnss_status": "GNSS status", "ui.paused": "Paused", "ui.recording": "Recording",
      "ui.stable_summary": "Age {age} s · stable for {duration} · {mount}",
      "ui.blocked_duration": "Required: RTK FIXED. Not reached for {duration} in this session.",
      "ui.correction_stream_stands": "correction stream stands", "ui.keep_antenna_still": "keep the antenna still",
      "ui.height_position": "Height {alt} m · {lat} / {lon}",
      "ui.conditions": "Conditions", "ui.missing_conditions": "What is still missing",
      "ui.start_line_blocked": "Start line — blocked", "ui.continue_survey": "Continue survey",
      "ui.lines": "lines", "ui.asset_points": "asset points", "ui.survey_data": "Survey data",
      "ui.no_projects": "No projects yet. Create the first project to start surveying.",
      "ui.project_settings": "Project settings", "ui.exports": "Exports",
      "ui.data_management": "Data management", "ui.active_project": "Active project",
      "ui.all_projects_backup": "Back up or restore all survey projects.",
      "ui.message_overview": "Message overview",
      "ui.device_functions": "Device functions", "ui.wifi_ntrip": "Wi-Fi and NTRIP",
      "ui.qr_pin": "QR codes and PIN", "ui.stream_active": "Stream active",
      "ui.stream_stale": "Stream interrupted", "ui.rtcm_message": "RTCM message",
      "ui.device_status": "Device status", "ui.not_connected": "Not connected",
      "ui.chip": "Chip", "ui.free": "free", "ui.uptime": "Uptime",
      "ui.keep_screen_awake": "Keep screen awake",
      "ui.wake_lock_unavailable": "This browser does not support keeping the screen awake.",
      "ui.receiver": "Receiver", "ui.firmware": "Firmware", "ui.share": "Share",
      "ui.open_support_package": "Open support package",
      "ui.active": "active",
      "ui.averaging": "Averaging",
      "ui.new_points_blocked": "New points blocked",
      "ui.recording_continues": "The line remains active. Wait for RTK FIXED before adding another point.",
      "ui.total": "total",
      "ui.horizontal_accuracy": "Horizontal accuracy", "ui.vertical_accuracy": "Vertical accuracy",
      "ui.device_sections": "Device sections", "ui.project_sections": "Project sections",
      "ui.lines_quality": "Lines and quality", "ui.data": "Data",
      "ui.project_name_example": "e.g. Field road east", "ui.creating_project": "Creating project …",
      "ui.project_overview": "Project overview",
      "ui.no_recorded_lines": "No recorded lines yet. Start the first line under Field survey.",
      "ui.quality_limits": "Quality limits (fixed)",
      "ui.quality_limits_readonly": "Safety limits are defined by the receiver and cannot be changed here.",
      "ui.correction_age": "Correction age", "ui.horizontal_spread": "Horizontal spread",
      "ui.connecting": "Connecting …", "ui.overview": "Overview",
      "ui.access": "Access", "ui.module": "Module", "ui.position": "Position",
      "ui.no_position": "No position", "ui.web_portal": "Web portal",
      "ui.bluetooth": "Bluetooth LE", "ui.last_errors": "RECENT ERRORS",
      "ui.websites": "WEB PAGES", "ui.responses": "MODULE RESPONSES",
      "ui.all_levels": "All levels", "ui.automatic": "automatic",
      "ui.load_now": "Load now", "ui.send": "Send", "ui.show": "Show",
      "ui.hide": "Hide", "ui.copy": "Copy", "ui.device_access": "Device access",
      "ui.device_code": "Device code:", "ui.status_page": "Go to status page",
      "ui.enter_code": "Enter the device code from the label.",
      "ui.secure_field_receiver": "Secure field receiver",
      "ui.signing_in": "Signing in …", "ui.signin_failed": "Sign-in failed:",
      "ui.checking_qr": "Checking QR access …", "ui.qr_access_failed": "QR access failed:",
      "ui.wifi_connection": "Wi-Fi connection (STA)",
      "ui.scan_wifi": "Scan visible Wi-Fi networks",
      "ui.choose_wifi": "Choose Wi-Fi network …", "ui.password": "Password:",
      "ui.choose_wifi_dots": "Choose Wi-Fi network ...",
      "ui.fallbacks": "Fallback networks (one network per line):",
      "ui.backup_restore": "Backup and restore", "ui.download_config": "Download configuration",
      "ui.open_diagnostics": "Open device diagnostics", "ui.restore": "Restore backup",
      "ui.username": "Username:", "ui.current_hotspot": "Current hotspot:",
      "ui.label_codes": "Show label and direct-access QR codes",
      "ui.device_code_retained": "This code belongs on the physical label and is retained after a factory reset.",
      "ui.wifi_ntrip_description": "Wi-Fi network used to obtain NTRIP corrections (must have Internet access).",
      "ui.backup_secret_warning": "The backup contains Wi-Fi and NTRIP passwords in clear text. Store the file securely.",
      "ui.ntrip_caster": "NTRIP Caster",
      "ui.qr_local": "The QR codes are generated entirely on this device.",
      "ui.device_label": "Device label", "ui.direct_portal": "Direct web portal",
      "ui.device_label_access": "Device label and access",
      "ui.direct_portal_detail": "For devices already on the same Wi-Fi network. The access code remains in the URL fragment and is then removed.",
      "ui.bluetooth_pairing_pin": "Bluetooth pairing PIN",
      "ui.bluetooth_pairing_detail": "Enter this PIN when Android requests Bluetooth pairing.",
      "ui.diagnostics_title": "Device diagnostics", "ui.download_diagnostics": "Download diagnostics package"
      ,"ui.login_required": "Sign-in required", "ui.enter_device_code": "Enter device code",
      "ui.auth_required": "Authentication required", "ui.error_log": "Error log",
      "ui.reference_station": "Reference station", "ui.gga_sent": "GGA sent:",
      "ui.start": "Start:", "ui.fix_quality_time": "Time at each fix quality",
      "ui.recent_errors": "Recent errors", "ui.after": "after",
      "ui.source": "Source", "ui.message": "Message",
      "ui.no_rtcm_detail": "No correction data received yet. Without a Wi-Fi connection to the caster, this remains empty.",
      "ui.datasheet_specifies": "The Quectel LC29H data sheet specifies",
      "ui.and": "and", "ui.matches_datasheet": "This matches the data sheet.",
      "ui.other_msm_work": "These are not the documented families. Validate them against the caster and a known control point.",
      "ui.type": "Type", "ui.count": "Count", "ui.family": "Family", "ui.meaning": "Meaning",
      "ui.ports": "Ports", "ui.auth_first_line": "Send as the first line of every TCP connection:",
      "ui.ble_pin_detail": "Enter this PIN when Android requests pairing. Bonded reconnects are automatic.",
      "ui.name": "Name", "ui.module_help": "Sends a sentence to the GNSS receiver. Allowed commands are",
      "ui.app_continues": "The application continues running.",
      "ui.bluetooth_nus": "Bluetooth NUS", "ui.optimize_storage": "Optimize recording storage",
      "ui.diagnostics_package": "Device diagnostics and support package",
      "ui.label_qr": "Label and QR codes", "ui.rtcm_diagnostics": "RTCM diagnostics",
      "ui.gnss_module": "GNSS module", "ui.status_dashboard": "Status dashboard",
      "ui.no_corrections": "No correction data received yet.",
      "ui.no_msm": "no MSM messages", "ui.received": "Received:",
      "ui.entries": "entries", "ui.unreachable": "unreachable",
      "ui.channel": "ch.", "ui.auth_errors": "authentication errors",
      "ui.login_at": "Sign in at", "ui.label": "label",
      "ui.stream_hidden": "The stream key is visible only after sign-in.",
      "ui.no_matching_entries": "No matching entries.",
      "ui.refreshing": "Refreshing …", "ui.error": "Error",
      "ui.back_overview": "Back to overview", "ui.back": "Back",
      "ui.empty_password": "empty = unchanged", "ui.saving": "Saving …",
      "ui.save_again": "Save again", "ui.saved_checking": "Saved — checking connection",
      "ui.searching": "Searching …", "ui.scan_failed": "Scan failed:",
      "ui.networks_found": "Wi-Fi networks found", "ui.secured": "secured", "ui.open": "open",
      "ui.choose_backup": "Please select a backup file first.",
      "ui.signout_failed": "Sign-out failed", "ui.confirm_restore": "Confirm restore",
      "ui.confirm_restore_detail": "This replaces Wi-Fi and NTRIP settings and restarts the device. Continue?",
      "ui.checking_backup": "Checking backup …", "ui.restored_reboot": "Restored. Device is restarting.",
      "ui.restore_failed": "Restore failed:", "ui.save_failed": "Save failed:",
      "ui.qr_checking": "Checking QR access …", "ui.qr_failed": "QR access failed:",
      "ui.setup_network": "Connects to", "ui.setup_after": "The captive portal then opens setup.",
      "ui.same_wifi": "For devices already on the same Wi-Fi network."
      ,"ui.boot_wifi_hint": "If the Wi-Fi network is not visible, hold BOOT for three seconds."
      ,"ui.factory_reset": "factory reset", "ui.device_code_word": "Device code",
      "ui.stream_key": "Stream key", "ui.rotate_stream": "Rotate stream key",
      "ui.area": "Area", "ui.address": "Address", "ui.meaning": "Meaning",
      "ui.count": "Count", "ui.family": "Family", "ui.source": "Source",
      "ui.message": "Message", "ui.after": "after", "ui.fix_duration": "TIME AT EACH FIX QUALITY"
      ,"ui.ram_free": "Free RAM", "ui.file_system": "File system",
      "ui.tx_power": "Transmit power", "ui.uptime": "Uptime",
      "ui.restarts": "Restarts", "ui.failed_boots": "failed boots",
      "ui.satellites": "Satellites", "ui.correction_data": "Correction data",
      "ui.receiver_accuracy": "Receiver accuracy", "ui.no_gst_estimate": "No GST estimate",
      "ui.height": "Height", "ui.network": "Network", "ui.age": "Age",
      "ui.reference_station": "Reference station", "ui.gga_sent": "GGA sent:",
      "ui.start": "Start:", "ui.kb_received": "KB received",
      "ui.last_message_ago": "last message", "ui.type": "Type",
      "ui.observations": "observations", "ui.station_coordinates": "Station coordinates",
      "ui.station_coordinates_height": "Station coordinates + height",
      "ui.antenna_description": "Antenna description", "ui.system_parameters": "System parameters",
      "ui.receiver_description": "Receiver description", "ui.glonass_bias": "GLONASS code bias",
      "ui.nmea_tcp": "NMEA over TCP", "ui.http_no_https": "HTTP, no HTTPS",
      "ui.http_api": "HTTP API and technical endpoints",
      "ui.log_source_placeholder": "Source, e.g. WIFI", "ui.search_log": "Search log",
      "ui.updated": "updated", "ui.copied": "Copied",
      "ui.offline_waiting": "offline — waiting for connection",
      "ui.connection_restored": "connection restored — updating …",
      "ui.compact_confirm": "Replace the event history with a verified current snapshot? All projects, lines, points, and measurements are retained.",
      "ui.compact_success": "Recording storage optimized: {before} KB → {after} KB. All survey data was retained.",
      "ui.ago": "ago", "ui.unknown": "unknown", "ui.clients": "Clients",
      "ui.tasks": "tasks", "ui.channel_word": "channel",
      "ui.status_unavailable": "Device status unavailable",
      "ui.loading_device_status": "Loading device status …",
      "ui.ap_clients": "AP clients",
      "ui.device_diagnostics": "Device diagnostics", "ui.open_label_qr": "Open label and QR codes",
      "ui.device_code_qr_after_login": "The device code and QR codes are shown after signing in under Configuration or Label.",
      "ui.rotate_confirm": "Rotate the stream key? All TCP clients will be disconnected.",
      "ui.receiver_command_prefix": "Allowed commands are", "ui.valid_checksum": "with a valid checksum.",
      "ui.application_continues": "The application continues running.",
      "ui.no_fix": "no fix", "ui.estimated": "estimated", "ui.manual": "manual",
      "ui.loading_short": "loading ...", "ui.diagnostics_short": "RTK diagnostics",
      "ui.connect_internet_wifi": "Connect to the Internet Wi-Fi and then open",
      "ui.setup_hotspot_warning": "If the setup hotspot is still visible after about 30 seconds, the credentials could not be used.",
      "ui.not_documented_families": "Not the documented families —",
      "ui.works_anyway": "still works", "ui.with_msm_measured": "Validate the stream against the caster and a known control point.",
      "ui.device_code_qr_prefix": "The device code and QR codes are shown after signing in under",
      "ui.or_label_shown": "or Label.",
      "ui.network_format_help": "Format: Name:Password. If only the name is provided, the saved password is retained. When connecting, the device scans and tries the strongest reachable network first. Enter the Wi-Fi network normally used above as SSID and password; this field is only for additional alternatives.",
      "ui.fixed_session_hint": "RTK FIXED has not been reached in this session. Experience shows that it requires an unobstructed sky view, uninterrupted correction data, and one or two minutes — check the RTCM tab to confirm that data is flowing.",
      "ui.ble_usage": "Pair with the six-digit device PIN and enable TX notifications. The encrypted connection provides NMEA and $PESPS without an application-level AUTH command. Other RX commands are disabled during normal operation. If the device is missing during scanning, disconnect other BLE apps first.",
      "ui.ble_pairing_pin": "Bluetooth pairing PIN",
      "ui.ble_pin_prompt": "Enter this PIN when Android requests pairing.",
      "ui.ble_bond_reconnect": "Bonded reconnects are automatic.",
      "ui.encrypted_pairing": "Encrypted pairing",
      "ui.rtcm_received_intro": "Received:",
      "ui.quectel_datasheet": "The Quectel LC29H data sheet specifies",
      "ui.matches_datasheet": "This matches the data sheet.",
      "ui.other_families_work": "These are not the documented families — but they still work: Validate the stream against the caster and a known control point.",
      "ui.write_auth": "Write operations require an administrator session and CSRF token.",
      "ui.no_stream_positions": "The TCP stream key is visible only after sign-in.",
      "ui.device_diagnostics_support": "Device diagnostics and support package",
      "ui.label_qr_codes": "Label and QR codes", "ui.rtcm_diagnostics": "RTCM diagnostics"
      ,"ui.login_first": "Please sign in under Configuration first",
      "ui.no_caster": "Without a Wi-Fi connection to the caster, this remains empty.",
      "ui.fixed_hint": "RTK FIXED has not been reached yet. Experience shows that an unobstructed sky view, a suitable antenna, and matching MSM corrections are required.",
      "ui.stream_first_line": "Send as the first line of every TCP connection:",
      "ui.send_receiver": "Sends a sentence to the GNSS receiver.",
      "ui.wifi_help": "Wi-Fi network used to obtain NTRIP corrections (must have Internet access).",
      "ui.code_retained": "This code belongs on the physical label and is retained after a factory reset.",
      "ui.backup_warning": "The backup contains Wi-Fi and NTRIP passwords in clear text. Store the file securely.",
      "ui.save_progress": "Configuration is being transferred. The setup Wi-Fi may briefly disappear during verification.",
      "ui.save_success": "The device is restarting and checking all Wi-Fi networks.",
      "ui.replace_confirm": "Replace the existing Wi-Fi and NTRIP configuration and restart?",
      "ui.portal_code_fragment": "The access code remains in the URL fragment and is then removed.",
      "ui.diagnostics_warning": "Contains no Wi-Fi or NTRIP passwords. Position data and technical identifiers are sensitive; share them only when necessary."
      ,"ui.survey": "Survey", "ui.gnss_quality": "GNSS quality",
      "ui.active_line": "Active line", "ui.no_active_line": "No active line",
      "ui.field_survey": "Field survey",
      "ui.field_survey_help": "Record a line continuously and add related asset points without stopping it.",
      "ui.control_help": "For a before/after stability check, record the same known point twice as type control with exactly the same name.",
      "ui.project": "Project", "ui.choose_project": "Choose project …",
      "ui.line": "Line", "ui.status": "Status", "ui.satellites_lower": "satellites",
      "ui.new_project": "New project", "ui.start_line": "Start line",
      "ui.add_vertex": "Add line point", "ui.add_asset": "Add asset point",
      "ui.pause": "Pause", "ui.resume": "Resume", "ui.finish_line": "Finish line",
      "ui.download_geojson": "Download GeoJSON", "ui.download_csv": "Download point CSV",
      "ui.project_name": "Project name", "ui.line_name": "Line name", "ui.create_project": "Create project",
      "ui.auto_distance": "Automatic point distance in metres (0 = manual only)",
      "ui.auto_distance_help": "Use 0 to add every line point manually.",
      "ui.auto_distance_range": "Enter 0 or a distance between 0.2 and 100 metres.",
      "ui.asset_type": "Asset type: tap, valve, hydrant, branch, transition, repair, control, tree, line_start, line_end, other",
      "ui.asset_name": "Asset name", "ui.note": "Note",
      "ui.finish_confirm": "Finish the active line?", "ui.vertices": "vertices"
      ,"ui.measurement": "Measurement", "ui.instant": "Instant",
      "ui.average_5": "Average 5 samples", "ui.average_10": "Average 10 samples",
      "ui.line_active": "Line active", "ui.undo_last": "Undo last point",
      "ui.recorded_lines": "Recorded lines", "ui.line_points": "Line points",
      "ui.length": "Length", "ui.quality": "Quality", "ui.valid": "valid",
      "ui.line_point_quality": "Line-point quality",
      "ui.line_point_number": "Line point {number}",
      "ui.segment": "Segment", "ui.vertical_step": "Vertical step",
      "ui.line_detail_failed": "Could not load line details: {error}",
      "ui.load_more_line_points": "Load more line points",
      "ui.control_checks": "Control-point checks", "ui.measurements": "Measurements",
      "ui.horizontal_delta": "Horizontal delta", "ui.vertical_delta": "Vertical delta",
      "ui.undo_confirm": "Undo the last recorded point?",
      "ui.survey_preview": "Survey preview",
      "ui.no_geometry": "No surveyed geometry yet."
      ,"ui.ready_survey": "Ready for accurate survey", "ui.wait": "Wait:",
      "ui.collecting_samples": "GNSS samples — keep the antenna still …",
      "ui.measurement_starting": "Starting measurement — keep the antenna still …",
      "ui.measurement_progress": "Measurement {collected} of {total} — keep the antenna still …",
      "ui.measurement_progress_status": "{status} · {satellites} satellites · HDOP {hdop} · measurement {collected} of {total}",
      "ui.measurement_saved": "Point saved: {samples} samples in {duration} s · {fixed} RTK FIXED · max. horizontal spread {spread} m · GST 1σ H/V {gst_h}/{gst_v} m",
      "ui.measurement_failed": "Measurement failed: {error}",
      "ui.pipe_material": "Pipe material", "ui.pipe_diameter": "Outside diameter in millimetres",
      "ui.burial_depth": "Nominal burial depth in metres", "ui.rename": "Rename",
      "ui.delete_project": "Delete project", "ui.delete_project_confirm": "Delete this complete survey project?",
      "ui.download_backup": "Download complete backup", "ui.survey_storage": "Survey storage:",
      "ui.compact_storage": "Optimize recording storage", "ui.restore_backup": "Restore backup",
      "ui.choose_backup_first": "Choose a backup first.",
      "ui.backup_checking": "Checking survey backup …",
      "ui.backup_restoring": "Restoring survey backup …",
      "ui.restore_success": "Survey backup restored successfully.",
      "ui.restore_failed": "Restore failed: {error}",
      "ui.restore_cancelled": "Restore cancelled.",
      "ui.replace_survey_confirm": "Replace all survey projects with this backup?",
      "ui.invalid_backup_short": "Invalid backup:"
      ,"ui.rename_project": "Rename project", "ui.delete_project_detail": "Project {name} with {lines} lines and {points} asset points will be permanently deleted. Download a backup first if the data is still needed.",
      "ui.asset_note_help": "Optional field note; maximum 500 characters.", "ui.save_asset": "Save asset point",
      "ui.asset_association": "Line association", "ui.asset_independent": "Independent object point",
      "ui.asset_active_line": "Associate with active line",
      "ui.replace_survey_detail": "All survey projects will be replaced by {file}. This cannot be undone.",
      "ui.invalid_backup": "Invalid backup: {error}", "ui.rotate_stream_key": "Rotate stream key",
      "ui.rotate_stream_key_detail": "All TCP and BLE clients will be disconnected. The current key stops working immediately.",
      "ui.device_restart": "Restart device",
      "ui.device_restart_detail": "Restarts firmware without deleting configuration or survey data.",
      "ui.device_restart_confirm": "The web app and data streams will be unavailable briefly. Survey data and configuration are retained.",
      "ui.action_required": "Action recommended", "ui.measurement_blocked": "Measurement blocked",
      "ui.action_rtk_float": "RTK FLOAT: keep the antenna still with a clear sky view and check that correction data continues to arrive in the RTCM tab.",
      "ui.action_stale_rtcm": "Correction data is stale: check Wi-Fi and the NTRIP connection before recording points.",
      "ui.action_data_drops": "Data was dropped: pause the survey, disconnect unused clients and open Diagnostics to check queues and UART load.",
      "ui.action_low_storage": "Storage is almost full: export survey data and compact storage before continuing.",
      "quality_action.no_position": "Move the antenna into a clear sky view and wait for a valid position.",
      "quality_action.rtk_fixed_required": "Wait for RTK FIXED; verify correction flow in the RTCM tab.",
      "quality_action.too_few_satellites": "Improve the sky view and wait for more satellites.",
      "quality_action.hdop_too_high": "Keep the antenna still and improve the sky view until HDOP decreases.",
      "quality_action.corrections_too_old": "Check Wi-Fi and NTRIP; wait for fresh correction data.",
      "quality_action.horizontal_spread_too_high": "Keep the antenna still and repeat the averaged measurement.",
      "quality_action.gst_required": "Wait until the receiver reports an accuracy estimate.",
      "quality_action.gst_horizontal_error_too_high": "Keep the antenna still and wait for better horizontal accuracy.",
      "quality_action.gst_vertical_error_too_high": "Keep the antenna still and wait for better vertical accuracy.",
      "asset.tap": "Tap", "asset.valve": "Valve", "asset.hydrant": "Hydrant", "asset.branch": "Branch",
      "asset.transition": "Transition", "asset.repair": "Repair", "asset.control": "Control point",
      "asset.tree": "Tree",
      "asset.line_start": "Line start", "asset.line_end": "Line end", "asset.other": "Other"
      ,"quality.no_position": "no position", "quality.rtk_fixed_required": "RTK FIXED required",
      "quality.too_few_satellites": "too few satellites", "quality.hdop_too_high": "HDOP too high",
      "quality.corrections_too_old": "corrections too old",
      "quality.not_all_samples_rtk_fixed": "not all samples are RTK FIXED",
      "quality.horizontal_spread_too_high": "horizontal spread too high"
      ,"quality.gst_required": "GST receiver estimate required",
      "quality.not_all_samples_have_gst": "not all samples have a GST estimate",
      "quality.gst_horizontal_error_too_high": "GST horizontal error too high",
      "quality.gst_vertical_error_too_high": "GST vertical error too high",
      "survey.not_enough_line_points": "at least two line points required",
      "survey.overlapping_line_points": "line points overlap or are less than 5 cm apart",
      "survey.line_has_no_horizontal_length": "line has no horizontal length"
      ,"survey.large_line_gaps": "one or more line gaps exceed 2 m",
      "survey.large_vertical_steps": "one or more vertical steps exceed 0.5 m"
    },
    de: {
      "language.de": "DE", "language.en": "EN",
      "app.field_app": "RTK Feld-App", "app.status": "Status",
      "app.projects": "Projekte", "app.device": "Gerät",
      "nav.portal": "Portal", "nav.configuration": "Konfiguration",
      "nav.diagnostics": "Diagnose", "nav.label": "Etikett", "ui.log": "Protokoll",
      "action.refresh": "Aktualisieren",
      "action.save": "Speichern und Verbindung prüfen", "action.login": "Anmelden", "action.cancel": "Abbrechen",
      "action.logout": "Abmelden", "action.download": "Herunterladen",
      "state.loading": "Wird geladen …", "state.saved": "Gespeichert.",
      "state.empty": "Keine Daten verfügbar.", "state.failed": "Vorgang fehlgeschlagen.",
      "error.admin_session_required": "Bitte zuerst anmelden.",
      "error.invalid_device_code": "Der Gerätecode ist ungültig.",
      "error.csrf_required": "Administratorsitzung und CSRF-Token sind erforderlich.",
      "error.invalid_request": "Die Anfrage ist ungültig.",
      "error.internal_error": "Im Gerät ist ein interner Fehler aufgetreten.",
      "ui.configuration": "Konfiguration", "ui.logout": "Abmelden",
      "ui.high_contrast": "Hoher Kontrast", "ui.settings": "Einstellungen",
      "ui.settings_help": "Sprache, Anzeige und Messvorgaben",
      "ui.language": "Sprache", "ui.display": "Anzeige", "ui.version": "Version",
      "ui.sample_single": "Sofortmessung", "ui.sample_avg": "{count} Messungen mitteln",
      "ui.last_point": "Letzter Punkt", "ui.seconds_ago": "vor {seconds} s", "ui.storage": "Speicher",
      "ui.mean_values": "Mittelwert H {h} m · V {v} m · Höhe {alt} m",
      "ui.name_optional": "frei wählbar", "ui.checking_name": "Wird geprüft …",
      "ui.name_available": "frei", "ui.name_taken": "bereits vergeben",
      "ui.name_taken_detail": "Bereits vergeben: {name}", "ui.more": "Mehr",
      "ui.just_now": "gerade eben", "ui.minutes_ago": "vor {count} Min.",
      "ui.last_activity": "zuletzt {when}", "ui.export_project": "Export · Projekt {name}",
      "ui.project_map": "Linien auf der Karte", "ui.share_geojson": "GeoJSON teilen", "ui.control": "Kontrolle",
      "ui.control_pending": "Zweite Messung fehlt",
      "ui.documented": "dokumentiert", "ui.deviating": "abweichend", "ui.module_diagnostics": "Modul & Diagnose",
      "ui.diagnostics_label": "Diagnose & Etikett", "ui.support_identification": "Supportpaket und Gerätekennung",
      "ui.language_outdoor": "Sprache · Außenmodus", "ui.auth_errors": "Auth-Fehler", "ui.start": "Start",
      "ui.restart_hint": "Nur neu starten, wenn Streams oder Empfänger nicht mehr reagieren. Messdaten und Konfiguration bleiben erhalten.",
      "ui.reboot_preparing": "Neustart wird vorbereitet …", "ui.reboot_waiting": "Gerät ist offline — warte auf die Wiederverbindung …",
      "ui.reboot_restored": "Neustart erfolgreich — Verbindung wiederhergestellt.", "ui.reboot_timeout": "Das Gerät startet noch. WLAN prüfen und neu laden, sobald sein Netzwerk wieder erreichbar ist.",
      "ui.gga_ntrip": "GGA gesendet: {gga} · NTRIP {kb} KB", "ui.connected_clients": "Verbundene Clients",
      "ui.protocol": "Protokoll", "ui.search_log_example": "Log durchsuchen — z. B. WIFI",
      "ui.measurement_gate": "Messfreigabe", "ui.required_fix": "Erforderlicher Fix",
      "ui.horizontal_limit": "Horizontal-1σ-Limit", "ui.vertical_limit": "Vertikal-1σ-Limit", "ui.all_limits": "Alle Grenzwerte",
      "ui.target": "Ziel", "ui.measurement_released": "Messung freigegeben",
      "ui.gnss_status": "GNSS-Status", "ui.paused": "Pausiert", "ui.recording": "Aufnahme läuft",
      "ui.stable_summary": "Alter {age} s · seit {duration} stabil · {mount}",
      "ui.blocked_duration": "Erforderlich: RTK FIXED. Seit {duration} in dieser Sitzung nicht erreicht.",
      "ui.correction_stream_stands": "Korrekturstrom steht", "ui.keep_antenna_still": "Antenne ruhig halten",
      "ui.height_position": "Höhe {alt} m · {lat} / {lon}",
      "ui.conditions": "Bedingungen", "ui.missing_conditions": "Was noch fehlt",
      "ui.start_line_blocked": "Linie starten — gesperrt", "ui.continue_survey": "Messung fortsetzen",
      "ui.lines": "Linien", "ui.asset_points": "Objektpunkte", "ui.survey_data": "Vermessungsdaten",
      "ui.no_projects": "Noch keine Projekte. Lege das erste Projekt an, um mit der Vermessung zu beginnen.",
      "ui.project_settings": "Projekteinstellungen", "ui.exports": "Exporte",
      "ui.data_management": "Datenverwaltung", "ui.active_project": "Aktives Projekt",
      "ui.all_projects_backup": "Alle Vermessungsprojekte sichern oder wiederherstellen.",
      "ui.message_overview": "Nachrichtenübersicht",
      "ui.device_functions": "Gerätefunktionen", "ui.wifi_ntrip": "WLAN und NTRIP",
      "ui.qr_pin": "QR-Codes und PIN", "ui.stream_active": "Strom steht",
      "ui.stream_stale": "Strom unterbrochen", "ui.rtcm_message": "RTCM-Nachricht",
      "ui.device_status": "Gerätezustand", "ui.not_connected": "Nicht verbunden",
      "ui.chip": "Chip", "ui.free": "frei", "ui.uptime": "Laufzeit",
      "ui.keep_screen_awake": "Bildschirm wach halten",
      "ui.wake_lock_unavailable": "Dieser Browser kann den Bildschirm nicht dauerhaft wach halten.",
      "ui.receiver": "Empfänger", "ui.firmware": "Firmware", "ui.share": "Teilen",
      "ui.open_support_package": "Supportpaket öffnen",
      "ui.active": "aktiv",
      "ui.averaging": "Mittelung",
      "ui.new_points_blocked": "Neue Punkte gesperrt",
      "ui.recording_continues": "Die Linie bleibt aktiv. Vor dem nächsten Punkt auf RTK FIXED warten.",
      "ui.total": "gesamt",
      "ui.horizontal_accuracy": "Horizontale Genauigkeit", "ui.vertical_accuracy": "Vertikale Genauigkeit",
      "ui.device_sections": "Gerätebereiche", "ui.project_sections": "Projektbereiche",
      "ui.lines_quality": "Linien und Qualität", "ui.data": "Daten",
      "ui.project_name_example": "z. B. Feldweg Ost", "ui.creating_project": "Projekt wird angelegt …",
      "ui.project_overview": "Projektübersicht",
      "ui.no_recorded_lines": "Noch keine Linien aufgezeichnet. Starte die erste Linie unter Feldaufnahme.",
      "ui.quality_limits": "Qualitätsgrenzen (fest)",
      "ui.quality_limits_readonly": "Die Sicherheitsgrenzen sind durch den Empfänger vorgegeben und können hier nicht geändert werden.",
      "ui.correction_age": "Alter der Korrektur", "ui.horizontal_spread": "Horizontale Streuung",
      "ui.connecting": "verbinde ...", "ui.overview": "Übersicht",
      "ui.access": "Zugänge", "ui.module": "Modul", "ui.position": "Position",
      "ui.no_position": "keine Position", "ui.web_portal": "Webportal",
      "ui.bluetooth": "Bluetooth LE", "ui.last_errors": "LETZTE FEHLER",
      "ui.websites": "WEBSEITEN", "ui.responses": "ANTWORTEN DES MODULS",
      "ui.all_levels": "Alle Stufen", "ui.automatic": "automatisch",
      "ui.load_now": "Jetzt laden", "ui.send": "Senden", "ui.show": "Anzeigen",
      "ui.hide": "Verbergen", "ui.copy": "Kopieren", "ui.device_access": "Gerätezugang",
      "ui.device_code": "Gerätecode:", "ui.status_page": "Zur Statusseite",
      "ui.enter_code": "Gerätecode vom Etikett eingeben.",
      "ui.secure_field_receiver": "Sicherer Feldempfänger",
      "ui.signing_in": "Anmeldung läuft …", "ui.signin_failed": "Anmeldung fehlgeschlagen:",
      "ui.checking_qr": "QR-Zugang wird geprüft …", "ui.qr_access_failed": "QR-Zugang fehlgeschlagen:",
      "ui.wifi_connection": "WLAN-Verbindung (STA)",
      "ui.scan_wifi": "Sichtbare WLANs suchen",
      "ui.choose_wifi": "WLAN auswählen …", "ui.password": "Passwort:",
      "ui.choose_wifi_dots": "WLAN auswählen ...",
      "ui.fallbacks": "Ausweichnetze (eine Zeile je Netz):",
      "ui.backup_restore": "Sicherung und Wiederherstellung", "ui.download_config": "Konfiguration herunterladen",
      "ui.open_diagnostics": "Gerätediagnose öffnen", "ui.restore": "Sicherung wiederherstellen",
      "ui.username": "Benutzername:", "ui.current_hotspot": "Aktueller Hotspot:",
      "ui.label_codes": "QR-Codes für Etikett und Direktzugang anzeigen",
      "ui.device_code_retained": "Dieser Code gehört auf das Geräteetikett und bleibt nach einem Werksreset erhalten.",
      "ui.wifi_ntrip_description": "WLAN für NTRIP-Korrekturen; ein Internetzugang ist erforderlich.",
      "ui.backup_secret_warning": "Die Sicherung enthält WLAN- und NTRIP-Passwörter im Klartext. Datei sicher aufbewahren.",
      "ui.ntrip_caster": "NTRIP-Caster",
      "ui.qr_local": "Die QR-Codes werden vollständig lokal erzeugt.",
      "ui.device_label": "Geräteetikett", "ui.direct_portal": "Webportal direkt",
      "ui.device_label_access": "Geräteetikett und Zugang",
      "ui.direct_portal_detail": "Für Geräte im selben WLAN. Der Zugangscode bleibt im URL-Fragment und wird anschließend entfernt.",
      "ui.bluetooth_pairing_pin": "Bluetooth-Kopplungs-PIN",
      "ui.bluetooth_pairing_detail": "Diese PIN eingeben, wenn Android die Bluetooth-Kopplung anfordert.",
      "ui.diagnostics_title": "Gerätediagnose", "ui.download_diagnostics": "Diagnosepaket herunterladen"
      ,"ui.login_required": "Anmeldung erforderlich", "ui.enter_device_code": "Gerätecode eingeben",
      "ui.auth_required": "Authentifizierung erforderlich", "ui.error_log": "Fehlerprotokoll",
      "ui.reference_station": "Referenzstation", "ui.gga_sent": "GGA gesendet:",
      "ui.start": "Start:", "ui.fix_quality_time": "Zeit je Fixqualität",
      "ui.recent_errors": "Letzte Fehler", "ui.after": "nach",
      "ui.source": "Quelle", "ui.message": "Meldung",
      "ui.no_rtcm_detail": "Noch keine Korrekturdaten empfangen. Ohne WLAN-Verbindung zum Caster bleibt diese Ansicht leer.",
      "ui.datasheet_specifies": "Das Datenblatt des Quectel LC29H nennt",
      "ui.and": "und", "ui.matches_datasheet": "Dies entspricht dem Datenblatt.",
      "ui.other_msm_work": "Dies sind nicht die dokumentierten Familien. Den Strom am Caster und auf einem bekannten Kontrollpunkt validieren.",
      "ui.type": "Typ", "ui.count": "Anzahl", "ui.family": "Familie", "ui.meaning": "Bedeutung",
      "ui.ports": "Ports", "ui.auth_first_line": "Als erste Zeile jeder TCP-Verbindung senden:",
      "ui.ble_pin_detail": "Diese PIN bei der Android-Kopplungsanfrage eingeben. Gebundene Geräte verbinden sich danach automatisch.",
      "ui.name": "Name", "ui.module_help": "Sendet einen Satz an den GNSS-Empfänger. Zulässige Befehle sind",
      "ui.app_continues": "Die Anwendung läuft dabei weiter.",
      "ui.bluetooth_nus": "Bluetooth NUS", "ui.optimize_storage": "Aufnahmespeicher optimieren",
      "ui.diagnostics_package": "Gerätediagnose und Supportpaket",
      "ui.label_qr": "Etikett und QR-Codes", "ui.rtcm_diagnostics": "RTCM-Diagnose",
      "ui.gnss_module": "GNSS-Modul", "ui.status_dashboard": "Status-Dashboard",
      "ui.no_corrections": "Noch keine Korrekturdaten empfangen.",
      "ui.no_msm": "keine MSM-Nachrichten", "ui.received": "Empfangen:",
      "ui.entries": "Einträge", "ui.unreachable": "nicht erreichbar",
      "ui.channel": "Kan.", "ui.auth_errors": "Auth-Fehler",
      "ui.login_at": "Anmeldung unter", "ui.label": "Etikett",
      "ui.stream_hidden": "Der Streamschlüssel ist nur nach der Anmeldung sichtbar.",
      "ui.no_matching_entries": "Keine passenden Einträge.",
      "ui.refreshing": "Aktualisieren …", "ui.error": "Fehler",
      "ui.back_overview": "Zur Übersicht", "ui.back": "Zurück",
      "ui.empty_password": "leer = unverändert", "ui.saving": "Speichere ...",
      "ui.save_again": "Erneut speichern", "ui.saved_checking": "Gespeichert – Verbindung wird geprüft",
      "ui.searching": "Suche läuft ...", "ui.scan_failed": "Scan fehlgeschlagen:",
      "ui.networks_found": "WLANs gefunden", "ui.secured": "gesichert", "ui.open": "offen",
      "ui.choose_backup": "Bitte zuerst eine Sicherungsdatei auswählen.",
      "ui.signout_failed": "Abmeldung fehlgeschlagen", "ui.confirm_restore": "Wiederherstellung bestätigen",
      "ui.confirm_restore_detail": "Dies ersetzt die WLAN- und NTRIP-Einstellungen und startet das Gerät neu. Fortfahren?",
      "ui.checking_backup": "Sicherung wird geprüft ...", "ui.restored_reboot": "Wiederhergestellt. Gerät startet neu.",
      "ui.restore_failed": "Wiederherstellung fehlgeschlagen:", "ui.save_failed": "Speichern fehlgeschlagen:",
      "ui.qr_checking": "QR-Zugang wird geprueft...", "ui.qr_failed": "QR-Zugang fehlgeschlagen:",
      "ui.setup_network": "Verbindet mit", "ui.setup_after": "Das Captive Portal öffnet danach die Einrichtung.",
      "ui.same_wifi": "Für Geräte, die bereits im selben WLAN sind."
      ,"ui.boot_wifi_hint": "Falls das WLAN nicht sichtbar ist, BOOT drei Sekunden halten."
      ,"ui.factory_reset": "Werkseinstellung", "ui.device_code_word": "Gerätecode",
      "ui.stream_key": "Streamschlüssel", "ui.rotate_stream": "Streamschlüssel erneuern",
      "ui.area": "Bereich", "ui.address": "Adresse", "ui.meaning": "Bedeutung",
      "ui.count": "Anzahl", "ui.family": "Familie", "ui.source": "Quelle",
      "ui.message": "Meldung", "ui.after": "nach", "ui.fix_duration": "VERWEILDAUER JE FIXQUALITÄT"
      ,"ui.ram_free": "RAM frei", "ui.file_system": "Dateisystem",
      "ui.tx_power": "Sendeleistung", "ui.uptime": "Laufzeit",
      "ui.restarts": "Neustarts", "ui.failed_boots": "Fehlstarts",
      "ui.satellites": "Satelliten", "ui.correction_data": "Korrekturdaten",
      "ui.receiver_accuracy": "Empfängergenauigkeit", "ui.no_gst_estimate": "Keine GST-Schätzung",
      "ui.height": "Höhe", "ui.network": "Netz", "ui.age": "Alter",
      "ui.reference_station": "Referenzstation", "ui.gga_sent": "GGA gesendet:",
      "ui.start": "Start:", "ui.kb_received": "KB empfangen",
      "ui.last_message_ago": "letzte Nachricht", "ui.type": "Typ",
      "ui.observations": "Beobachtungen", "ui.station_coordinates": "Stationskoordinaten",
      "ui.station_coordinates_height": "Stationskoordinaten + Höhe",
      "ui.antenna_description": "Antennenbeschreibung", "ui.system_parameters": "Systemparameter",
      "ui.receiver_description": "Empfängerbeschreibung", "ui.glonass_bias": "GLONASS-Code-Bias",
      "ui.nmea_tcp": "NMEA über TCP", "ui.http_no_https": "HTTP, kein HTTPS",
      "ui.http_api": "HTTP-API und technische Endpunkte",
      "ui.log_source_placeholder": "Quelle, z. B. WIFI", "ui.search_log": "Log durchsuchen",
      "ui.updated": "aktualisiert", "ui.copied": "Kopiert",
      "ui.offline_waiting": "offline — warte auf Verbindung",
      "ui.connection_restored": "Verbindung wiederhergestellt — aktualisiere …",
      "ui.compact_confirm": "Den Ereignisverlauf durch einen geprüften aktuellen Stand ersetzen? Alle Projekte, Linien, Punkte und Messwerte bleiben erhalten.",
      "ui.compact_success": "Aufzeichnungsspeicher optimiert: {before} KB → {after} KB. Alle Aufnahmedaten wurden beibehalten.",
      "ui.ago": "vor", "ui.unknown": "unbekannt", "ui.clients": "Clients",
      "ui.tasks": "Tasks", "ui.channel_word": "Kan.",
      "ui.status_unavailable": "Gerätestatus nicht verfügbar",
      "ui.loading_device_status": "Gerätestatus wird geladen …",
      "ui.ap_clients": "AP-Clients",
      "ui.device_diagnostics": "Gerätediagnose", "ui.open_label_qr": "Etikett und QR-Codes öffnen",
      "ui.device_code_qr_after_login": "Der Gerätecode und die QR-Codes werden nach der Anmeldung unter Konfiguration bzw. Etikett angezeigt.",
      "ui.rotate_confirm": "Streamschlüssel erneuern? Alle TCP-Clients werden getrennt.",
      "ui.receiver_command_prefix": "Erlaubt sind", "ui.valid_checksum": "mit gültiger Prüfsumme.",
      "ui.application_continues": "Die Anwendung läuft dabei weiter.",
      "ui.no_fix": "kein Fix", "ui.estimated": "geschaetzt", "ui.manual": "manuell",
      "ui.loading_short": "lade ...", "ui.diagnostics_short": "RTK Diagnose",
      "ui.connect_internet_wifi": "Mit dem Internet-WLAN verbinden und danach",
      "ui.setup_hotspot_warning": "Bleibt der Setup-Hotspot nach etwa 30 Sekunden sichtbar, konnten die Zugangsdaten nicht verwendet werden.",
      "ui.not_documented_families": "Nicht die dokumentierten Familien —",
      "ui.works_anyway": "funktioniert aber trotzdem", "ui.with_msm_measured": "Den Strom am Caster und auf einem bekannten Kontrollpunkt validieren.",
      "ui.device_code_qr_prefix": "Der Gerätecode und die QR-Codes werden nach der Anmeldung unter",
      "ui.or_label_shown": "bzw. Etikett angezeigt.",
      "ui.network_format_help": "Schreibweise Name:Passwort. Steht nur der Name da, bleibt das gespeicherte Passwort erhalten. Beim Verbinden wird gescannt und das stärkste erreichbare Netz zuerst probiert. Das WLAN, das normalerweise verwendet werden soll, bitte oben als SSID und Passwort eintragen; dieses Feld ist nur für zusätzliche Alternativen.",
      "ui.fixed_session_hint": "In dieser Sitzung noch nie RTK FIXED erreicht. Erfahrungsgemäß braucht es freie Sicht, ununterbrochene Korrekturdaten und ein bis zwei Minuten Zeit — siehe Reiter RTCM, ob der Strom steht.",
      "ui.ble_usage": "Mit der sechsstelligen Geräte-PIN koppeln und TX-Benachrichtigungen aktivieren. Die verschlüsselte Verbindung liefert NMEA und $PESPS ohne zusätzlichen AUTH-Befehl. Andere RX-Befehle sind im Normalbetrieb deaktiviert. Falls das Gerät beim Scan fehlt, andere BLE-Apps zuerst trennen.",
      "ui.ble_pairing_pin": "Bluetooth-Kopplungs-PIN",
      "ui.ble_pin_prompt": "Diese PIN eingeben, wenn Android zur Bluetooth-Kopplung auffordert.",
      "ui.ble_bond_reconnect": "Gekoppelte Geräte verbinden sich danach automatisch.",
      "ui.encrypted_pairing": "Verschlüsselte Kopplung",
      "ui.rtcm_received_intro": "Empfangen:",
      "ui.quectel_datasheet": "Der Quectel LC29H nennt im Datenblatt",
      "ui.matches_datasheet": "Das deckt sich mit dem Datenblatt.",
      "ui.other_families_work": "Nicht die dokumentierten Familien — funktioniert aber trotzdem: Den Strom am Caster und auf einem bekannten Kontrollpunkt validieren.",
      "ui.write_auth": "Schreibzugriffe benötigen Admin-Sitzung und CSRF-Token.",
      "ui.no_stream_positions": "Der TCP-Streamschlüssel ist nur nach der Anmeldung sichtbar.",
      "ui.device_diagnostics_support": "Gerätediagnose und Supportpaket",
      "ui.label_qr_codes": "Etikett und QR-Codes", "ui.rtcm_diagnostics": "RTCM-Diagnose"
      ,"ui.login_first": "Bitte zuerst unter Konfiguration anmelden",
      "ui.no_caster": "Ohne WLAN-Verbindung zum Caster bleibt das leer.",
      "ui.fixed_hint": "RTK FIXED noch nicht erreicht. Erfahrungsgemäß braucht es freie Sicht, eine geeignete Antenne und passende MSM-Korrekturen.",
      "ui.stream_first_line": "Als erste Zeile jeder TCP-Verbindung senden:",
      "ui.send_receiver": "Sendet einen Satz an den GNSS-Empfänger.",
      "ui.wifi_help": "WLAN, über das die NTRIP-Korrekturen bezogen werden (muss Internetzugang haben).",
      "ui.code_retained": "Dieser Code gehört auf das physische Etikett und bleibt bei einer Werkseinstellung erhalten.",
      "ui.backup_warning": "Die Sicherung enthält WLAN- und NTRIP-Passwörter im Klartext. Datei sicher verwahren.",
      "ui.save_progress": "Konfiguration wird übertragen. Das Setup-WLAN kann während der Prüfung kurz verschwinden.",
      "ui.save_success": "Das Gerät startet neu und prüft alle WLANs.",
      "ui.replace_confirm": "Bestehende WLAN- und NTRIP-Konfiguration ersetzen und neu starten?",
      "ui.portal_code_fragment": "Der Zugangscode bleibt im URL-Fragment und wird danach entfernt.",
      "ui.diagnostics_warning": "Enthält keine WLAN- oder NTRIP-Passwörter. Positionsdaten und technische Kennungen sind sensibel; nur gezielt weitergeben."
      ,"ui.survey": "Aufnahme", "ui.gnss_quality": "GNSS-Qualität",
      "ui.active_line": "Aktive Linie", "ui.no_active_line": "Keine aktive Linie",
      "ui.field_survey": "Geländeaufnahme",
      "ui.field_survey_help": "Eine Linie fortlaufend aufnehmen und zugehörige Objektpunkte setzen, ohne sie anzuhalten.",
      "ui.control_help": "Für eine Stabilitätsprüfung vorher und nachher denselben bekannten Punkt zweimal mit Typ control und exakt gleichem Namen aufnehmen.",
      "ui.project": "Projekt", "ui.choose_project": "Projekt auswählen …",
      "ui.line": "Linie", "ui.status": "Status", "ui.satellites_lower": "Satelliten",
      "ui.new_project": "Neues Projekt", "ui.start_line": "Linie starten",
      "ui.add_vertex": "Linienpunkt setzen", "ui.add_asset": "Objektpunkt setzen",
      "ui.pause": "Pause", "ui.resume": "Fortsetzen", "ui.finish_line": "Linie abschließen",
      "ui.download_geojson": "GeoJSON herunterladen", "ui.download_csv": "Punkt-CSV herunterladen",
      "ui.project_name": "Projektname", "ui.line_name": "Linienname", "ui.create_project": "Projekt anlegen",
      "ui.auto_distance": "Automatischer Punktabstand in Metern (0 = nur manuell)",
      "ui.auto_distance_help": "Mit 0 wird jeder Linienpunkt manuell gesetzt.",
      "ui.auto_distance_range": "0 oder einen Abstand zwischen 0,2 und 100 Metern eingeben.",
      "ui.asset_type": "Objekttyp: Anschluss, Armatur, Hydrant, Abzweig, Übergang, Reparatur, Kontrollpunkt, Baum, Linienanfang, Linienende, Sonstiges",
      "ui.asset_name": "Objektname", "ui.note": "Notiz",
      "ui.finish_confirm": "Aktive Linie abschließen?", "ui.vertices": "Linienpunkte"
      ,"ui.measurement": "Messung", "ui.instant": "Sofortmessung",
      "ui.average_5": "5 Messungen mitteln", "ui.average_10": "10 Messungen mitteln",
      "ui.line_active": "Linie aktiv", "ui.undo_last": "Letzten Punkt rückgängig",
      "ui.recorded_lines": "Aufgezeichnete Linien", "ui.line_points": "Linienpunkte",
      "ui.length": "Länge", "ui.quality": "Qualität", "ui.valid": "gültig",
      "ui.line_point_quality": "Qualität der Linienpunkte",
      "ui.line_point_number": "Linienpunkt {number}",
      "ui.segment": "Segment", "ui.vertical_step": "Höhensprung",
      "ui.line_detail_failed": "Liniendetails konnten nicht geladen werden: {error}",
      "ui.load_more_line_points": "Weitere Linienpunkte laden",
      "ui.control_checks": "Kontrollpunktprüfungen", "ui.measurements": "Messungen",
      "ui.horizontal_delta": "Horizontale Abweichung", "ui.vertical_delta": "Vertikale Abweichung",
      "ui.undo_confirm": "Den zuletzt aufgenommenen Punkt rückgängig machen?",
      "ui.survey_preview": "Aufnahmevorschau",
      "ui.no_geometry": "Noch keine aufgenommene Geometrie."
      ,"ui.ready_survey": "Bereit für genaue Aufnahme", "ui.wait": "Warten:",
      "ui.collecting_samples": "GNSS-Messungen — Antenne ruhig halten …",
      "ui.measurement_starting": "Messung startet — Antenne ruhig halten …",
      "ui.measurement_progress": "Messung {collected} von {total} — Antenne ruhig halten …",
      "ui.measurement_progress_status": "{status} · {satellites} Satelliten · HDOP {hdop} · Messung {collected} von {total}",
      "ui.measurement_saved": "Punkt gespeichert: {samples} Messungen in {duration} s · {fixed} RTK FIXED · maximale horizontale Streuung {spread} m · GST 1σ H/V {gst_h}/{gst_v} m",
      "ui.measurement_failed": "Messung fehlgeschlagen: {error}",
      "ui.pipe_material": "Rohrmaterial", "ui.pipe_diameter": "Außendurchmesser in Millimetern",
      "ui.burial_depth": "Nennverlegetiefe in Metern", "ui.rename": "Umbenennen",
      "ui.delete_project": "Projekt löschen", "ui.delete_project_confirm": "Dieses vollständige Aufnahmeprojekt löschen?",
      "ui.download_backup": "Vollständige Sicherung herunterladen", "ui.survey_storage": "Aufnahmespeicher:",
      "ui.compact_storage": "Aufzeichnungsspeicher optimieren", "ui.restore_backup": "Sicherung wiederherstellen",
      "ui.choose_backup_first": "Zuerst eine Sicherung auswählen.",
      "ui.backup_checking": "Aufnahmesicherung wird geprüft …",
      "ui.backup_restoring": "Aufnahmesicherung wird wiederhergestellt …",
      "ui.restore_success": "Aufnahmesicherung erfolgreich wiederhergestellt.",
      "ui.restore_failed": "Wiederherstellung fehlgeschlagen: {error}",
      "ui.restore_cancelled": "Wiederherstellung abgebrochen.",
      "ui.replace_survey_confirm": "Alle Aufnahmeprojekte durch diese Sicherung ersetzen?",
      "ui.invalid_backup_short": "Ungültige Sicherung:"
      ,"ui.rename_project": "Projekt umbenennen", "ui.delete_project_detail": "Das Projekt {name} mit {lines} Linien und {points} Objektpunkten wird dauerhaft gelöscht. Falls die Daten noch benötigt werden, zuerst eine Sicherung herunterladen.",
      "ui.asset_note_help": "Optionale Feldnotiz; maximal 500 Zeichen.", "ui.save_asset": "Objektpunkt speichern",
      "ui.asset_association": "Linienzuordnung", "ui.asset_independent": "Unabhängiger Objektpunkt",
      "ui.asset_active_line": "Der aktiven Linie zuordnen",
      "ui.replace_survey_detail": "Alle Aufnahmeprojekte werden durch {file} ersetzt. Dies kann nicht rückgängig gemacht werden.",
      "ui.invalid_backup": "Ungültige Sicherung: {error}", "ui.rotate_stream_key": "Stream-Schlüssel erneuern",
      "ui.rotate_stream_key_detail": "Alle TCP- und BLE-Clients werden getrennt. Der bisherige Schlüssel ist sofort ungültig.",
      "ui.device_restart": "Gerät neu starten",
      "ui.device_restart_detail": "Startet die Firmware neu, ohne Konfiguration oder Vermessungsdaten zu löschen.",
      "ui.device_restart_confirm": "Web-App und Datenströme sind kurzzeitig nicht erreichbar. Vermessungsdaten und Konfiguration bleiben erhalten.",
      "ui.action_required": "Handlung empfohlen", "ui.measurement_blocked": "Messung gesperrt",
      "ui.action_rtk_float": "RTK FLOAT: Antenne bei freier Sicht ruhig halten und im RTCM-Reiter prüfen, ob weiterhin Korrekturdaten eintreffen.",
      "ui.action_stale_rtcm": "Korrekturdaten sind veraltet: vor der Aufnahme WLAN und NTRIP-Verbindung prüfen.",
      "ui.action_data_drops": "Daten wurden verworfen: Aufnahme pausieren, ungenutzte Clients trennen und unter Diagnose Queue- und UART-Last prüfen.",
      "ui.action_low_storage": "Speicher fast voll: Aufnahmedaten exportieren und den Speicher vor dem Fortsetzen verdichten.",
      "quality_action.no_position": "Antenne mit freier Sicht aufstellen und auf eine gültige Position warten.",
      "quality_action.rtk_fixed_required": "Auf RTK FIXED warten und den Korrekturdatenstrom im RTCM-Reiter prüfen.",
      "quality_action.too_few_satellites": "Freie Sicht verbessern und auf weitere Satelliten warten.",
      "quality_action.hdop_too_high": "Antenne ruhig halten und freie Sicht verbessern, bis der HDOP sinkt.",
      "quality_action.corrections_too_old": "WLAN und NTRIP prüfen und auf aktuelle Korrekturdaten warten.",
      "quality_action.horizontal_spread_too_high": "Antenne ruhig halten und die gemittelte Messung wiederholen.",
      "quality_action.gst_required": "Warten, bis der Empfänger eine Genauigkeitsschätzung liefert.",
      "quality_action.gst_horizontal_error_too_high": "Antenne ruhig halten und auf bessere horizontale Genauigkeit warten.",
      "quality_action.gst_vertical_error_too_high": "Antenne ruhig halten und auf bessere vertikale Genauigkeit warten.",
      "asset.tap": "Anschluss", "asset.valve": "Armatur", "asset.hydrant": "Hydrant", "asset.branch": "Abzweig",
      "asset.transition": "Übergang", "asset.repair": "Reparatur", "asset.control": "Kontrollpunkt",
      "asset.tree": "Baum",
      "asset.line_start": "Linienanfang", "asset.line_end": "Linienende", "asset.other": "Sonstiges"
      ,"quality.no_position": "keine Position", "quality.rtk_fixed_required": "RTK FIXED erforderlich",
      "quality.too_few_satellites": "zu wenige Satelliten", "quality.hdop_too_high": "HDOP zu hoch",
      "quality.corrections_too_old": "Korrekturdaten zu alt",
      "quality.not_all_samples_rtk_fixed": "nicht alle Messungen sind RTK FIXED",
      "quality.horizontal_spread_too_high": "horizontale Streuung zu groß"
      ,"quality.gst_required": "GST-Empfängerschätzung erforderlich",
      "quality.not_all_samples_have_gst": "nicht alle Messungen enthalten eine GST-Schätzung",
      "quality.gst_horizontal_error_too_high": "horizontaler GST-Fehler zu groß",
      "quality.gst_vertical_error_too_high": "vertikaler GST-Fehler zu groß",
      "survey.not_enough_line_points": "mindestens zwei Linienpunkte erforderlich",
      "survey.overlapping_line_points": "Linienpunkte überlagern sich oder liegen weniger als 5 cm auseinander",
      "survey.line_has_no_horizontal_length": "Linie hat keine horizontale Länge"
      ,"survey.large_line_gaps": "mindestens eine Linienlücke ist größer als 2 m",
      "survey.large_vertical_steps": "mindestens ein Höhensprung ist größer als 0,5 m"
    }
  };

  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, function (c) {
      return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];
    });
  }
  function selectedLanguage() {
    let saved = null;
    try { saved = root.localStorage.getItem(STORAGE_KEY); } catch (_) {}
    if (saved === "de" || saved === "en") return saved;
    return String(root.navigator && root.navigator.language || "en").toLowerCase().indexOf("de") === 0 ? "de" : "en";
  }
  let language = selectedLanguage();
  const labelKeys = {};
  Object.keys(dictionaries.en).forEach(function (key) {
    labelKeys[dictionaries.en[key]] = key;
  });
  function translate(key, values) {
    let output = dictionaries[language][key] || dictionaries.en[key] || key;
    Object.keys(values || {}).forEach(function (name) {
      output = output.split("{" + name + "}").join(escapeHtml(values[name]));
    });
    return output;
  }
  function label(source) {
    const key = labelKeys[String(source)];
    return key ? translate(key) : String(source);
  }
  function locale() { return language === "de" ? "de-DE" : "en-US"; }
  function formatTime(value) {
    const date = value instanceof Date ? value : new Date(value);
    return date.toLocaleTimeString(locale(), language === "de" ? {hour12:false} : {});
  }
  function formatDateTime(value) {
    const date = value instanceof Date ? value : new Date(value);
    return date.toLocaleString(locale(), language === "de" ? {hour12:false} : {});
  }
  function apply() {
    if (!root.document) return;
    root.document.documentElement.lang = language;
    root.document.querySelectorAll("[data-i18n]").forEach(function (node) {
      node.textContent = translate(node.getAttribute("data-i18n"));
    });
    root.document.querySelectorAll("[data-i18n-placeholder]").forEach(function (node) {
      node.setAttribute("placeholder", translate(node.getAttribute("data-i18n-placeholder")));
    });
    root.document.querySelectorAll("[data-i18n-value]").forEach(function (node) {
      node.value = translate(node.getAttribute("data-i18n-value"));
    });
    root.document.querySelectorAll("[data-language]").forEach(function (node) {
      node.setAttribute("aria-pressed", String(node.getAttribute("data-language") === language));
    });
  }
  function setLanguage(next) {
    if (next !== "de" && next !== "en") return;
    language = next;
    try { root.localStorage.setItem(STORAGE_KEY, next); } catch (_) {}
    apply();
    if (root.document && root.CustomEvent) root.document.dispatchEvent(new root.CustomEvent("rtk-language-change", {detail:{language:next}}));
  }
  function applyTheme() {
    if (!root.document || !root.document.documentElement) return;
    if (theme === "high-contrast")
      root.document.documentElement.setAttribute("data-theme", "high-contrast");
    else root.document.documentElement.removeAttribute("data-theme");
    root.document.querySelectorAll("[data-theme-toggle]").forEach(function (button) {
      button.setAttribute("aria-pressed", String(theme === "high-contrast"));
    });
  }
  function toggleTheme() {
    theme = theme === "high-contrast" ? "default" : "high-contrast";
    try { root.localStorage.setItem(THEME_KEY, theme); } catch (_) {}
    applyTheme();
  }
  function selector() {
    return '<div class="language-selector"><div role="group" aria-label="Language"><button type="button" data-language="de">DE</button><span aria-hidden="true"> | </span><button type="button" data-language="en">EN</button></div><button type="button" data-theme-toggle data-i18n="ui.high_contrast" aria-pressed="false">High contrast</button></div>';
  }
  function mount() {
    if (!root.document) return;
    if (!root.document.querySelector(".language-selector") && root.document.body.dataset.page === "login")
      root.document.body.insertAdjacentHTML("afterbegin", selector());
    root.document.querySelectorAll("[data-language]").forEach(function (button) {
      button.addEventListener("click", function () { setLanguage(button.getAttribute("data-language")); });
    });
    root.document.querySelectorAll("[data-theme-toggle]").forEach(function (button) {
      button.addEventListener("click", toggleTheme);
    });
    applyTheme();
    apply();
    if (root.MutationObserver) new root.MutationObserver(function (changes) {
      changes.forEach(function (change) {
        change.addedNodes.forEach(function (node) {
          if (node.nodeType === 1) {
            if (node.matches && node.matches("[data-i18n]"))
              node.textContent = translate(node.getAttribute("data-i18n"));
            node.querySelectorAll && node.querySelectorAll("[data-i18n]").forEach(function (child) {
              child.textContent = translate(child.getAttribute("data-i18n"));
            });
          }
        });
      });
    }).observe(root.document.body, {childList:true, subtree:true});
  }
  const v2de={"RTCM stream":"RTCM-Strom","Correction data":"Korrekturdaten","Access":"Zugänge","Connections and keys":"Verbindungen und Schlüssel","Log":"Protokoll","Device events":"Geräteereignisse","Module & diagnostics":"Modul & Diagnose","Receiver console":"Empfängerkonsole","Configuration":"Konfiguration","Networks and NTRIP":"Netzwerke und NTRIP","Diagnostics":"Diagnose","Support data":"Supportdaten","Label & QR codes":"Etikett & QR-Codes","Direct access":"Direktzugang","Settings":"Einstellungen","Display and measurement":"Anzeige und Messung","Checking …":"Prüfe …","Waiting for receiver state":"Warte auf Empfängerstatus","Can I measure?":"Darf ich messen?","Project":"Projekt","Measurement":"Messung","Choose project":"Projekt wählen","Start line":"Linie starten","Object point":"Objektpunkt","Add line point":"Linienpunkt setzen","Resume":"Fortsetzen","Pause":"Pausieren","Finish line":"Linie abschließen","Start a new line":"Neue Linie starten","Line name":"Linienname","Automatic point distance (m)":"Automatischer Punktabstand (m)","Material":"Material","Diameter":"Durchmesser","Depth":"Tiefe","Optional":"Optional","Start recording":"Aufnahme starten","Set object point":"Objektpunkt setzen","Type":"Typ","Name":"Name","Note":"Notiz","More":"Mehr","Associate with active line":"Aktiver Linie zuordnen","Save point":"Punkt speichern","Available":"frei","Keep the antenna still":"Antenne ruhig halten","Starting measurement …":"Messung startet …","Horizontal mean":"Mittelwert H","Vertical mean":"Mittelwert V","Elevation mean":"Mittelwert Höhe","New project":"Neues Projekt","Project name":"Projektname","Create project":"Projekt anlegen","No projects yet":"Noch keine Projekte","Create the first project before starting a survey.":"Vor der ersten Aufnahme ein Projekt anlegen.","Complete backup":"Vollständige Sicherung","Data management":"Datenverwaltung","Back up, restore or optimize all survey projects.":"Alle Vermessungsprojekte sichern, wiederherstellen oder optimieren.","Restore backup":"Sicherung wiederherstellen","Optimize storage":"Speicher optimieren","Rename":"Umbenennen","Delete":"Löschen","Share GeoJSON":"GeoJSON teilen","Download point CSV":"Punkt-CSV laden","No surveyed geometry yet.":"Noch keine Geometrie aufgenommen.","Second measurement missing":"Zweite Messung fehlt","Required fix":"Erforderlicher Fix","Averaging":"Mittelung","All limits":"Alle Grenzwerte","Sign out":"Abmelden","High contrast on":"Außenmodus an","High contrast off":"Außenmodus aus","Device":"Gerät","Version":"Version","Password":"Passwort","Username":"Benutzername","Field network":"Feldnetzwerk","Correction source":"Korrekturquelle","Scan networks":"Netzwerke suchen","Download configuration":"Konfiguration laden","Restore configuration":"Konfiguration wiederherstellen","Save and verify configuration":"Konfiguration speichern und prüfen","Restart device":"Gerät neu starten","Load now":"Jetzt laden","Share":"Teilen","Search log — e.g. WIFI":"Protokoll durchsuchen — z. B. WIFI","All":"Alle","Error":"Fehler","Connected clients":"Verbundene Clients","Recording":"Aufnahme läuft","Ready to measure":"Bereit zum Messen","Measurement blocked":"Messung gesperrt","Stable for":"Stabil seit","Required":"Erforderlich","points":"Punkte","lines":"Linien","No project selected":"Kein Projekt ausgewählt","Updated":"Aktualisiert"};
  Object.assign(v2de,{"Sign in · RTK Field App":"Anmelden · RTK Feld-App","Ready when the receiver is.":"Bereit, wenn der Empfänger es ist.","Sign in locally with the device code printed on the receiver label.":"Lokal mit dem Geräte-Code vom Empfängeretikett anmelden.","SECURE DEVICE ACCESS":"SICHERER GERÄTEZUGANG","Sign in":"Anmelden","Device code":"Geräte-Code","Show":"Zeigen","Hide":"Verbergen","Open Field App":"Feld-App öffnen","The code never leaves this device.":"Der Code verlässt dieses Gerät nicht.","not accepted":"nicht erreicht"});
  Object.assign(v2de,{"Project map":"Projektkarte","Survey data":"Vermessungsdaten","Back to projects":"Zurück zu Projekte","Position":"Position","Last point":"Letzter Punkt","Storage":"Speicher","line points":"Linienpunkte","object points":"Objektpunkte","For a before/after stability check, record the same known point twice as type control with exactly the same name.":"Für eine Vorher-/Nachher-Stabilitätsprüfung denselben bekannten Punkt zweimal mit Typ Kontrolle und exakt gleichem Namen aufnehmen.","Diameter (mm)":"Durchmesser (mm)","Burial depth (m)":"Verlegetiefe (m)","Recorded vertices":"Aufgenommene Linienpunkte","Accepted quality":"Akzeptierte Qualität","Load more line points":"Weitere Linienpunkte laden","Network":"Netzwerk","Not connected":"Nicht verbunden","Chip":"Chip","File system":"Dateisystem","Clients":"Clients","Authentication errors":"Authentifizierungsfehler","documented":"dokumentiert","deviating":"abweichend","entries":"Einträge","Diagnostics & label":"Diagnose & Etikett","Support package and identification":"Supportpaket und Identifikation","Wi-Fi and NTRIP":"WLAN und NTRIP","Language · outdoor mode":"Sprache · Außenmodus","Uptime":"Laufzeit","Start":"Start","Restart only if streams or the receiver no longer respond. Survey data and configuration are retained.":"Nur neu starten, wenn Streams oder Empfänger nicht mehr reagieren. Vermessungsdaten und Konfiguration bleiben erhalten.","Keep screen awake on":"Bildschirm wach halten an","Keep screen awake off":"Bildschirm wach halten aus","This browser does not support keeping the screen awake.":"Dieser Browser unterstützt das Wachhalten des Bildschirms nicht.","Address":"Adresse","Open label & QR codes":"Etikett & QR-Codes öffnen","Download support package":"Supportpaket herunterladen","Configuration applied and connection restored.":"Konfiguration übernommen und Verbindung wiederhergestellt.","Configuration failed; the previous working settings were restored.":"Konfiguration fehlgeschlagen; die vorherigen funktionierenden Einstellungen wurden wiederhergestellt.","Receiver is restarting — waiting for the connection …":"Gerät startet neu — warte auf die Verbindung …","The receiver is still reconnecting. Reopen this page after joining its network.":"Das Gerät verbindet sich noch. Diese Seite nach dem Beitritt zum Netzwerk erneut öffnen.","Fallback networks":"Ausweichnetzwerke","Existing passwords are retained when only the SSID is entered.":"Bestehende Passwörter bleiben erhalten, wenn nur die SSID eingetragen wird.","Technical connection details":"Technische Verbindungsdetails","Pairing protection":"Kopplungsschutz","Use the stream key only with trusted field clients.":"Den Stream-Schlüssel nur mit vertrauenswürdigen Feld-Clients verwenden."});
  Object.assign(v2de,{"+ New project":"+ Neues Projekt","ACTIVE PROJECT EXPORT":"EXPORT DES AKTIVEN PROJEKTS","All data interfaces remain local to the device.":"Alle Datenschnittstellen bleiben lokal auf dem Gerät.","BACKUP & RESTORE":"SICHERN & WIEDERHERSTELLEN","CONNECTED CLIENTS":"VERBUNDENE CLIENTS","CONNECTIONS":"VERBINDUNGEN","CORRECTION STREAM":"KORREKTURSTROM","Changes are validated before the receiver restarts.":"Änderungen werden vor dem Neustart des Empfängers geprüft.","Changing Wi-Fi may temporarily disconnect this browser.":"Beim Wechsel des WLANs kann die Verbindung dieses Browsers kurz unterbrochen werden.","DEVICE EVENTS":"GERÄTEEREIGNISSE","DIRECT ACCESS":"DIREKTZUGANG","Device hotspot":"Geräte-Hotspot","Encrypted pairing PIN":"Verschlüsselte Kopplungs-PIN","FIELD CONDITIONS":"FELDBEDINGUNGEN","FIELD RECEIVER":"FELDEMPFÄNGER","Filter the current and persistent device history.":"Aktuelle und gespeicherte Geräteereignisse filtern.","GNSS command":"GNSS-Befehl","How can clients reach this receiver?":"Wie erreichen Clients diesen Empfänger?","LANGUAGE":"SPRACHE","MEASUREMENT":"MESSUNG","MEASUREMENT GATE":"MESSFREIGABE","MESSAGE FAMILIES":"NACHRICHTENFAMILIEN","NETWORKS & CORRECTIONS":"NETZWERKE & KORREKTUREN","NTRIP CASTER":"NTRIP-CASTER","OUTDOOR DISPLAY":"AUSSENANZEIGE","One network per line":"Ein Netzwerk pro Zeile","Only new entries are transferred while this page is open.":"Solange diese Seite geöffnet ist, werden nur neue Einträge übertragen.","PROJECT WORKSPACE":"PROJEKTBEREICH","Point CSV":"Punkt-CSV","RECEIVER CONSOLE":"EMPFÄNGERKONSOLE","SESSION":"SITZUNG","SSID or SSID:password, one network per line":"SSID oder SSID:Passwort, ein Netzwerk pro Zeile","SUPPORT SNAPSHOT":"SUPPORT-SNAPSHOT","SURVEY LIBRARY":"VERMESSUNGSPROJEKTE","Send checks to the Quectel receiver without stopping the field application.":"Prüfbefehle an den Quectel-Empfänger senden, ohne die Feldanwendung anzuhalten.","Show and copy stream key":"Stream-Schlüssel anzeigen und kopieren","Survey map":"Vermessungskarte","This read-only package contains receiver, network and runtime facts.":"Dieses schreibgeschützte Paket enthält Empfänger-, Netzwerk- und Laufzeitdaten.","Use these codes only on the physical receiver or in a trusted field team.":"Diese Codes nur direkt am Empfänger oder in einem vertrauenswürdigen Feldteam verwenden.","WI-FI CONNECTION":"WLAN-VERBINDUNG","What happened?":"Was ist passiert?","e.g. Water line 01":"z. B. Wasserleitung 01"});
  Object.assign(v2de,{"Scanning for Wi-Fi networks …":"WLAN-Netzwerke werden gesucht …","Network selected. Save to apply it.":"Netzwerk ausgewählt. Zum Übernehmen speichern.","Wi-Fi scan complete.":"WLAN-Suche abgeschlossen.","Visible Wi-Fi networks":"Sichtbare WLAN-Netzwerke","Use network":"Netzwerk verwenden","No Wi-Fi networks were found.":"Keine WLAN-Netzwerke gefunden.","Pause the active recording before scanning for Wi-Fi networks.":"Die aktive Aufnahme vor der WLAN-Suche pausieren."});
  Object.assign(v2de,{"NTRIP client":"NTRIP-Client","Enabled":"Aktiviert","Disabled":"Deaktiviert","Primary caster":"Primärer Caster","Fallback caster (optional)":"Alternativer Caster (optional)","The fallback is used automatically when configured and the primary caster is unavailable.":"Der alternative Caster wird automatisch verwendet, wenn er konfiguriert und der primäre Caster nicht erreichbar ist."});
  Object.assign(v2de,{"project":"Projekt","projects":"Projekte","line":"Linie","lines":"Linien","point":"Punkt","object point":"Objektpunkt","message":"Nachricht","messages":"Nachrichten","entry":"Eintrag","satellite":"Satellit","satellites":"Satelliten","Stream is live":"Strom steht","Stream interrupted":"Strom unterbrochen","Last message":"Letzte Nachricht","No recent RTCM message reached the receiver.":"Keine aktuelle RTCM-Nachricht hat den Empfänger erreicht.","No MSM family":"Keine MSM-Familie","GGA sent":"GGA gesendet","Corrections live":"Korrekturen aktiv","Corrections interrupted":"Korrekturen unterbrochen","Administrator session":"Administrator-Sitzung","DEVICE ACCESS":"GERÄTEZUGANG","BLE pairing PIN":"BLE-Kopplungs-PIN","Ports":"Ports","A password is saved.":"Ein Passwort ist gespeichert.","No password is saved.":"Kein Passwort ist gespeichert.","Mountpoint":"Mountpunkt","Vertical":"Vertikal","System":"System","Ready":"Bereit","Attention":"Achtung","Safe mode":"Sicherheitsmodus","GNSS":"GNSS","Corrections":"Korrekturen","Memory":"Speicher","free RAM":"RAM frei","Firmware":"Firmware","Wi-Fi reconnects":"WLAN-Neuverbindungen","NTRIP retries":"NTRIP-Wiederholungen","Queue overflows":"Queue-Überläufe","HTTP errors":"HTTP-Fehler","This read-only overview contains receiver, network and runtime facts.":"Diese schreibgeschützte Übersicht enthält Empfänger-, Netzwerk- und Laufzeitdaten.","Refresh":"Aktualisieren","Open support package":"Supportpaket öffnen","Task restarts":"Task-Neustarts","Try again":"Erneut versuchen"});
  Object.assign(v2de,{"Cancel measurement":"Messung abbrechen","Cancelling measurement …":"Messung wird abgebrochen …","Waiting for a valid GNSS sample …":"Warte auf eine gültige GNSS-Probe …","Measurement":"Messung","of":"von","Line recording started":"Linienaufnahme gestartet","Line point saved":"Linienpunkt gespeichert","Line finished":"Linie abgeschlossen","Object point saved":"Objektpunkt gespeichert","Recording paused":"Aufnahme pausiert","Measurement actions unlock when all field conditions pass.":"Messaktionen werden freigegeben, sobald alle Feldbedingungen erfüllt sind.","Resume the line before recording another point.":"Die Linie vor einer weiteren Punktaufnahme fortsetzen.","Wait for RTK FIXED or check the correction stream.":"Auf RTK FIXED warten oder den Korrekturstrom prüfen.","Keep the antenna still and wait for horizontal accuracy.":"Antenne ruhig halten und auf horizontale Genauigkeit warten.","Keep the antenna still and wait for vertical accuracy.":"Antenne ruhig halten und auf vertikale Genauigkeit warten.","Move to a clearer view of the sky.":"Zu einem Standort mit freierer Sicht zum Himmel wechseln.","Wait for all field conditions.":"Auf alle Feldbedingungen warten.","not ready for measurement":"nicht messbereit","Active for measurement":"Aktiv zum Messen","Active project for field recording":"Aktives Projekt für die Feldaufnahme","Activate for measurement":"Zum Messen aktivieren","Project activated for measurement":"Projekt zum Messen aktiviert","Project not found":"Projekt nicht gefunden","It may have been deleted or restored from another backup.":"Es wurde eventuell gelöscht oder aus einer anderen Sicherung wiederhergestellt.","Back to projects":"Zurück zu Projekten","All object points":"Alle Objektpunkte","Unnamed point":"Unbenannter Punkt","Horizontal accuracy":"Horizontale Genauigkeit","Chainage":"Stationierung","Survey lines":"Vermessungslinien","Map preview is simplified because this project contains more than 500 features.":"Die Kartenvorschau ist vereinfacht, weil dieses Projekt mehr als 500 Objekte enthält.","Survey line":"Vermessungslinie","Receiver":"Empfänger","Fit project":"Projekt einpassen","Center position":"Position zentrieren","Dismiss":"Schließen","Discard unsaved changes?":"Ungespeicherte Änderungen verwerfen?","The Wi-Fi and NTRIP values on this page have not been saved.":"Die WLAN- und NTRIP-Werte auf dieser Seite wurden noch nicht gespeichert.","Discard changes":"Änderungen verwerfen","Connection identifiers are included in the downloadable support package.":"Verbindungskennungen sind im herunterladbaren Supportpaket enthalten.","Device restarting — waiting for it to return":"Gerät startet neu — warte auf die Wiederverbindung","Device is still restarting. Reload when its network returns.":"Das Gerät startet noch. Neu laden, sobald sein Netzwerk wieder erreichbar ist."});
  Object.assign(v2de,{"Storage is running low":"Speicher wird knapp","free":"frei","Receiver console":"Empfängerkonsole","Diagnostics & identification":"Diagnose & Identifikation","MEASUREMENT PROFILE":"MESSPROFIL","ACTIVE MEASUREMENT LIMITS":"AKTIVE MESSGRENZEN","Hide stream key":"Stream-Schlüssel verbergen","Stream key copied":"Stream-Schlüssel kopiert"});
  Object.assign(v2de,{"Waiting for GNSS time":"Warte auf GNSS-Zeit","Waiting for usable position":"Warte auf brauchbare Position","Stabilizing position":"Position stabilisiert sich"});
  Object.assign(v2de,{"Open status":"Status öffnen","Tap for status":"Für Status antippen","Connected":"Verbunden"});
  Object.assign(v2de,{"measurement points":"Messpunkte","Point limit reached. Export and archive a project before continuing.":"Punktelimit erreicht. Vor dem Fortsetzen ein Projekt exportieren und archivieren.","Download restorable project archive":"Wiederherstellbares Projektarchiv laden"});
  Object.assign(v2de,{"Measuring":"Messung läuft","Measurement running":"Messung läuft","valid samples":"gültige Messwerte","discarded":"verworfen","conditions already met":"Bedingungen bereits erfüllt","Waiting for RTK FIXED and valid GST":"Warte auf RTK FIXED und gültiges GST","Collecting valid samples":"Sammle gültige Messwerte","Checking measurement spread":"Prüfe Messwertstreuung","Delete empty project":"Leeres Projekt löschen","quality accepted":"Qualität akzeptiert","last quality rejected":"Letzte Qualität abgelehnt","empty":"leer","complete":"abgeschlossen","recording":"Aufnahme","paused":"pausiert","Lines":"Linien","Points":"Punkte","Full screen":"Vollbild","GST accuracy age":"Alter der GST-Genauigkeit","Correction age":"Korrekturdatenalter","Correction stream":"Korrekturstrom"});
  Object.assign(v2de,{"Receiver details":"Empfängerdetails","Fix age":"Fixalter","RTK FIXED for":"RTK FIXED seit","Reference station":"Referenzstation","Last NTRIP error":"Letzter NTRIP-Fehler","Loading":"Laden","Undo last point":"Letzten Punkt zurücknehmen","Remove the most recently recorded line or object point from the active project?":"Den zuletzt aufgenommenen Linien- oder Objektpunkt aus dem aktiven Projekt entfernen?","Last point removed":"Letzter Punkt entfernt","Wait for the current measurement to finish or cancel it first.":"Die laufende Messung zuerst abschließen oder abbrechen.","Enter 0 or a distance between 0.2 and 100 metres.":"0 oder einen Abstand zwischen 0,2 und 100 Metern eingeben.","Use 0 to add every line point manually.":"Mit 0 wird jeder Linienpunkt manuell gesetzt.","Source":"Quelle","Technical stream details":"Technische Stromdetails","Caster host":"Caster-Host","Received":"Empfangen","Station coordinates":"Stationskoordinaten","Station coordinates and height":"Stationskoordinaten und Höhe","Antenna description":"Antennenbeschreibung","System parameters":"Systemparameter","Receiver description":"Empfängerbeschreibung","GLONASS code bias":"GLONASS-Code-Bias","Satellite":"Satellit","observations":"Beobachtungen","RTCM message":"RTCM-Nachricht","The received MSM family is documented for the Quectel LC29H.":"Die empfangene MSM-Familie ist für den Quectel LC29H dokumentiert.","The received family differs from the documented MSM4/MSM5/MSM7 families. Corrections may still work; verify RTK FIXED stability.":"Die empfangene Familie weicht von den dokumentierten MSM4/MSM5/MSM7-Familien ab. Korrekturen können dennoch funktionieren; RTK-FIXED-Stabilität prüfen.","This local portal uses HTTP, not HTTPS. Use it only on a trusted device network; stream authentication remains protected by the device key.":"Dieses lokale Portal verwendet HTTP, nicht HTTPS. Nur in einem vertrauenswürdigen Gerätenetz verwenden; die Stream-Authentifizierung bleibt durch den Geräteschlüssel geschützt.","TCP clients send AUTH <stream_token> as their first line. For BLE, pair with the six-digit PIN and enable encrypted Nordic UART Service notifications.":"TCP-Clients senden AUTH <stream_token> als erste Zeile. Für BLE mit der sechsstelligen PIN koppeln und verschlüsselte Nordic-UART-Service-Benachrichtigungen aktivieren.","BLE service UUID":"BLE-Service-UUID","BLE TX / Notify":"BLE TX / Notify","BLE RX / Write":"BLE RX / Write","Network mode":"Netzwerkmodus","CPU clock":"CPU-Takt","IDF heap free":"IDF-Heap frei","IDF largest block":"Größter IDF-Block","Chip temperature":"Chiptemperatur","Wi-Fi channel":"WLAN-Kanal","Wi-Fi transmit power":"WLAN-Sendeleistung","Hotspot clients":"Hotspot-Clients","Failed boots":"Fehlstarts","Automatic on":"Automatik an","Automatic off":"Automatik aus","Time since start":"Zeit seit Start","Clock time":"Uhrzeit","Share filtered":"Gefilterte teilen","filtered entries shared":"gefilterte Einträge geteilt"});
  Object.assign(v2de,{"Until quality is OK":"Bis Qualität OK","valid samples checked":"gültige Messwerte geprüft","checking stability …":"prüfe Stabilität …"});
  Object.assign(v2de,{"Export or optimize survey data before recording more points.":"Vor weiteren Aufnahmen Vermessungsdaten exportieren oder den Speicher optimieren.","Recording journal":"Aufnahmejournal","Only valid $PQTM and $PAIR commands with a checksum are accepted. The field application continues running.":"Nur gültige $PQTM- und $PAIR-Befehle mit Prüfsumme werden akzeptiert. Die Feldanwendung läuft weiter.","Measurement could not be completed.":"Die Messung konnte nicht abgeschlossen werden."});
  Object.assign(v2de,{"Check antenna and corrections":"Antenne und Korrekturen prüfen"});
  Object.assign(v2de,{"Loading survey storage":"Messablage wird geladen","Survey storage unavailable":"Messablage nicht verfügbar","Measurement actions unlock when survey storage is ready.":"Messaktionen werden nach dem Laden der Messablage freigegeben."});
  Object.assign(v2de,{"Configure acceptance rules":"Abnahmeregeln konfigurieren","Configure measurement acceptance":"Messabnahme konfigurieren","These limits apply immediately to status, line points and object points.":"Diese Grenzwerte gelten sofort für Status, Linienpunkte und Objektpunkte.","Save acceptance rules":"Abnahmeregeln speichern","Restore defaults":"Standard wiederherstellen","Reset acceptance rules?":"Abnahmeregeln zurücksetzen?","Minimum satellites":"Mindestanzahl Satelliten","Maximum HDOP":"Maximaler HDOP","Maximum correction age (s)":"Maximales Korrekturdatenalter (s)","Maximum horizontal 1σ (m)":"Maximales horizontales 1σ (m)","Maximum vertical 1σ (m)":"Maximales vertikales 1σ (m)","Maximum horizontal spread (m)":"Maximale horizontale Streuung (m)","RTK FLOAT or better":"RTK FLOAT oder besser","DGPS or better":"DGPS oder besser","Changes apply immediately to every new measurement.":"Änderungen gelten sofort für jede neue Messung.","Measurement acceptance rules saved":"Messabnahmeregeln gespeichert","Default acceptance rules restored":"Standard-Abnahmeregeln wiederhergestellt","Wait for the required fix or check the correction stream.":"Auf den erforderlichen Fix warten oder den Korrekturstrom prüfen."});
  v2de["Wait for the required fix or check the correction stream."]="Fix abwarten oder Korrekturen prüfen.";
  Object.assign(v2de,{"Quality accepted":"Qualität akzeptiert","Quality rejected":"Qualität abgelehnt","Horizontal 1σ":"Horizontal 1σ","Vertical 1σ":"Vertikal 1σ","Fix":"Fix","Samples":"Messwerte","Measurement duration":"Messdauer","Reference station":"Referenzstation","GST source":"GST-Quelle"});
  Object.assign(v2de,{"NMEA TCP SECURITY":"NMEA-TCP-SICHERHEIT","Authentication before streaming":"Authentifizierung vor der Übertragung","TCP authentication on":"TCP-Authentifizierung an","TCP authentication off":"TCP-Authentifizierung aus","Clients must send AUTH <stream_token> before the first NMEA byte.":"Clients müssen vor dem ersten NMEA-Byte AUTH <stream_token> senden.","NMEA starts immediately after connecting. No stream key is required.":"NMEA startet direkt nach dem Verbinden. Ein Stream-Schlüssel ist nicht erforderlich.","Disable TCP authentication?":"TCP-Authentifizierung deaktivieren?","Every client on the local network will receive the NMEA stream without a key. Use this only on a trusted isolated network.":"Jeder Client im lokalen Netzwerk erhält den NMEA-Strom ohne Schlüssel. Nur in einem vertrauenswürdigen isolierten Netzwerk verwenden.","Disable authentication":"Authentifizierung deaktivieren","TCP authentication enabled":"TCP-Authentifizierung aktiviert","TCP authentication disabled":"TCP-Authentifizierung deaktiviert","TCP authentication is disabled. Any client on this network can read the position stream.":"TCP-Authentifizierung ist deaktiviert. Jeder Client in diesem Netzwerk kann den Positionsstrom lesen."});
  Object.assign(v2de,{"These limits apply immediately to status, line points and object points. Restore defaults at any time from Settings.":"Diese Grenzwerte gelten sofort für Status, Linienpunkte und Objektpunkte. Die Standardwerte können jederzeit in den Einstellungen wiederhergestellt werden.","Quality OK sample window":"Stichprobenfenster Qualität OK","Default: 5 consecutive accepted samples.":"Standard: 5 aufeinanderfolgende akzeptierte Messwerte."});
  Object.assign(v2de,{"Recording mode":"Aufzeichnungsmodus","Survey — accepted precision points only":"Vermessung — nur akzeptierte Präzisionspunkte","Route — preserve every valid position":"Route — jede vollständige Position sichern","Wi-Fi error: ":"WLAN-Fehler: ","Hotspot not found":"Hotspot nicht gefunden","Wi-Fi authentication failed":"WLAN-Anmeldung fehlgeschlagen","Wi-Fi association failed":"WLAN-Verbindung fehlgeschlagen","Wi-Fi connection timed out":"Zeitüberschreitung der WLAN-Verbindung","Wi-Fi connection lost":"WLAN-Verbindung verloren"});
  Object.assign(v2de,{"FIELD TEST": "FELDTEST", "Field diagnostics": "Felddiagnose", "Start field diagnostics": "Felddiagnose starten", "Stop field diagnostics": "Felddiagnose stoppen", "Field marker": "Feldmarkierung", "Marker": "Markierung", "Stationary": "Stillstand", "Walking": "Gehen", "Hotspot off": "Hotspot aus", "Hotspot on": "Hotspot an", "Power off": "Strom aus", "Add marker": "Markierung setzen", "Download field log": "Feldprotokoll herunterladen", "Stop field diagnostics before downloading.": "Vor dem Herunterladen die Felddiagnose stoppen.", "Records fix, accuracy, satellite signals and connection events on the receiver, including without Wi-Fi. Resumes after a restart until stopped.": "Zeichnet Fix, Genauigkeit, Satellitensignale und Verbindungsereignisse im Empfänger auf, auch ohne WLAN. Läuft nach einem Neustart weiter, bis sie gestoppt wird.", "Oldest entries are overwritten when the bounded log fills. Pending entries can be lost on power interruption. Stop and download soon after the test.": "Ist der begrenzte Protokollspeicher voll, werden die ältesten Einträge überschrieben. Noch ungesicherte Einträge können bei Stromausfall verloren gehen. Nach dem Test zeitnah stoppen und herunterladen.", "Field diagnostics: storage error": "Felddiagnose: Speicherfehler", "Field diagnostics running": "Felddiagnose läuft", "Field diagnostics stopped": "Felddiagnose gestoppt", "samples since restart": "Messintervalle seit Neustart", "Dropped entries": "Verworfene Einträge", "Field marker saved": "Feldmarkierung gespeichert", "DIAG error · ": "DIAG Fehler · ", "DIAG on · ": "DIAG an · "});
  dictionaries.en["diag.field_link"]="Open field diagnostics";
  dictionaries.de["diag.field_link"]="Felddiagnose öffnen";
  Object.assign(v2de,{"Loading project map":"Projektkarte wird geladen","Loading live status":"Live-Status wird geladen","Live status unavailable":"Live-Status nicht verfügbar","Request timed out. Please try again.":"Zeitüberschreitung. Bitte erneut versuchen."});
  const api = {dictionaries:dictionaries, v2de:v2de, t:translate, label:label,
               apply:apply, setLanguage:setLanguage,
               applyTheme:applyTheme, toggleTheme:toggleTheme,
               formatTime:formatTime, formatDateTime:formatDateTime,
               getLanguage:function(){return language;}, getTheme:function(){return theme;},
               escapeHtml:escapeHtml, mount:mount};
  root.RtkI18n = api;
  applyTheme();
  if (root.document) {
    if (root.document.readyState === "loading") root.document.addEventListener("DOMContentLoaded", mount);
    else mount();
  }
  if (typeof module !== "undefined" && module.exports) module.exports = api;
}(typeof window !== "undefined" ? window : globalThis));
