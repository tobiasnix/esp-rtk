// SPDX-License-Identifier: AGPL-3.0-only
(function (root) {
  "use strict";
  const STORAGE_KEY = "rtk-language";
  const THEME_KEY = "rtk-theme";
  const THEMES = Object.freeze(["auto", "light", "dark", "high-contrast"]);
  // Values of the app.css --surface and --hero tokens per theme (kept equal by tests/test_theme.py).
  const THEME_COLORS = {light:{surface:"#ffffff", hero:"#111820"}, dark:{surface:"#161e24", hero:"#24313a"},
                        "high-contrast":{surface:"#ffffff", hero:"#000000"}};
  function normalTheme(value) { return THEMES.indexOf(value) >= 0 ? value : "auto"; }
  let theme = "auto";
  try { theme = normalTheme(root.localStorage.getItem(THEME_KEY)); }
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
      "ui.precision_legacy": "Legacy point: position grid about 0.4 m",
      "ui.legacy_vertices": "{count} line points recorded before V15.9 are quantised to about 0.4 m",
      "ui.legacy_vertex": "One line point recorded before V15.9 is quantised to about 0.4 m",
      "ui.precision_legacy_short": "Legacy",
      "ui.recorded_utc": "Recorded (UTC)",
      "ui.clock_unsynced": "Clock was not synchronised when this point was recorded",
      "ui.clock_unsynced_short": "Clock unsynced",
      "ui.legacy_values_skipped": "{count} legacy values could not be converted and were left out; see the device log",
      "ui.legacy_value_skipped": "One legacy value could not be converted and was left out; see the device log",
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
      "ui.field_readiness": "Field readiness", "ui.receiver_access": "Receiver and access",
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
      "ui.no_current_position": "No current position", "ui.fix_stale": "Position outdated for {seconds} s",
      "ui.route_without_approval": "Route recording is possible without measurement approval.", "ui.start_line_survey_blocked": "Survey lines need measurement approval. Choose Route to record without it.",
      "ui.phase_waiting_fix": "Waiting for {fix} and valid GST", "ui.fix_and_corrections": "{fix} + corrections",
      "ui.start_line_route_hint": "Route recording is possible without measurement approval. Survey lines and points unlock when all field conditions pass.", "ui.no_project_hint": "No project selected yet. Create or choose a project first, then start a line.", "ui.action_refused_reload": "Action refused. Reload the page and try again.",
      "ui.fix_stale_short": "Position outdated", "ui.receiver_position": "Receiver position",
      "ui.correction_link": "Correction link", "ui.corrections_age": "corrections {seconds} s old",
      "ui.corrections_age_unknown": "corrections age unknown", "ui.link_latency": "Link latency {seconds} s",
      "ui.arrival_age_short": "arrival {seconds} s", "ui.corrections_stale": "Corrections stale: epochs {seconds} s old",
      "ui.epoch_age": "Epoch age", "ui.arrival_age": "Arrival age", "ui.no_epoch_yet": "No epoch decoded yet",
      "ui.gate_softened": "Gate softened", "ui.gate_profile": "Gate profile",
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
      "ui.confirm_restore_detail": "This replaces Wi-Fi and NTRIP settings, the measurement rules and method, and the field setup, then restarts the device. Backups from older firmware contain only Wi-Fi and NTRIP settings. Continue?",
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
      "ui.frame_mix_warning": "This project contains points in more than one reference frame: {frames}", "ui.frame_unknown_warning": "The reference frame of this project's RTK points is unknown. The field setup applies to new measurements only; assign a frame to the existing points in the project.", "ui.frame_assign_action": "Assign reference frame", "ui.frame_assign_title": "Assign a reference frame to RTK points without one", "ui.frame_assign_submit": "Assign frame", "ui.frame_revoke_action": "Undo assignment", "ui.frame_revoke_title": "Undo the frame assignment", "ui.frame_unknown_key": "unknown", "ui.frame_mix_hint": "DGPS fixes are in WGS84/ITRF, RTK fixes in the correction service's frame (about 0.7 to 0.9 m apart in Europe). Accepting DGPS mixes frames within a project.", "ui.field_setup": "Field setup", "ui.field_setup_unavailable": "Field setup could not be loaded.", "ui.vertical_ground": "ground", "ui.vertical_phase_centre": "phase centre", "ui.field_setup_save": "Save field setup", "ui.field_setup_saved": "Field setup saved",
      "ui.antenna_height": "Antenna height above the ground point",
      "ui.antenna_height_help": "Distance from the ground point to the antenna phase centre (pole length plus the antenna's phase-centre offset), 0 to 3 m. Stored with every measurement; the raw receiver heights are never changed.",
      "ui.height_msl": "Height (MSL, phase centre)", "ui.height_ellipsoid": "Ellipsoid height",
      "ui.geoid_separation": "Geoid separation", "ui.ground_height": "Ground height (MSL)",
      "ui.reference_frame": "Reference frame of the correction service",
      "ui.reference_frame_help": "Frame of the RTK base or network (for example ETRS89 for a European national service). Stored with every RTK measurement; standalone and SBAS fixes are WGS84/ITRF. Leave empty if unknown.",
      "ui.reference_frame_epoch": "Frame epoch (optional)",
      "ui.reference_frame_ntrip_link": "NTRIP settings (configuration page)",
      "ui.antenna_height_zero": "Antenna height configured as 0; the ground height equals the phase-centre height.",
      "ui.antenna_height_missing": "No antenna height was recorded with this point (older firmware); no ground height.",
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
      "ui.hotspot_unconfigured": "Hotspot not configured",
      "ui.setup_hotspot_warning": "If the setup hotspot is still visible after about 30 seconds, the credentials could not be used.",
      "ui.not_documented_families": "Not the documented families —",
      "ui.works_anyway": "still works", "ui.with_msm_measured": "Validate the stream against the caster and a known control point.",
      "ui.device_code_qr_prefix": "The device code and QR codes are shown after signing in under",
      "ui.or_label_shown": "or Label.",
      "ui.network_format_help": "Format: Name:Password. If only the name is provided, the saved password is retained. The device tries the networks in the order entered: the Wi-Fi network above first, then these lines from top to bottom. A scan only skips networks that are not visible; if it sees none of them, the device tries all of them. Enter the Wi-Fi network normally used above as SSID and password; this field is only for additional alternatives.",
      "ui.scan_still_running": "The scan is still running. Try again in a moment.",
      "ui.scan_no_answer": "The device did not answer in time.",
      "ui.legacy_setup_notice": "This is the basic fallback page. The field app offers the full setup under Device > Configuration.",
      "ui.open_field_app_setup": "Open the field app setup",
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
      "ui.measurement_no_time_limit": "no time limit", "ui.measurement_time_left": "{seconds} s left",
      "ui.measurement_stopped_stale": "Measurement stopped: position outdated for {seconds} s",
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
      "gate.no_position": "No position – wait for a clear view of the sky",
      "gate.rtk_fixed_required": "{fix} instead of RTK FIXED – wait for RTK FIXED or check the corrections",
      "gate.required_fix_not_met": "{fix} does not reach {required} – wait for a better fix or check the corrections",
      "gate.not_all_samples_rtk_fixed": "Not every sample was RTK FIXED – measure again",
      "gate.too_few_satellites": "{count} satellites (min. {limit}) – move to a clearer view of the sky",
      "gate.hdop_too_high": "HDOP {value} (max. {limit}) – wait for a better satellite geometry",
      "gate.altitude_required": "Position without height – wait for a complete position",
      "gate.corrections_too_old": "Corrections {age} s old (max. {limit} s) – check caster/internet",
      "gate.horizontal_spread_too_high": "Samples spread too far horizontally – keep the antenna still and measure again",
      "gate.gst_required": "Receiver sends no accuracy estimate (GST). Device is requesting it (attempt {attempt}).",
      "gate.gst_required_waiting": "Receiver sends no accuracy estimate (GST) yet. Device is about to request it.",
      "gate.gst_required_uart_error": "Receiver sends no accuracy estimate (GST); the request failed – check the receiver connection.",
      "gate.gst_required_receiving": "Accuracy estimate (GST) missing for this position – wait; check the receiver if it persists.",
      "gate.not_all_samples_have_gst": "Not every sample had an accuracy estimate (GST) – measure again",
      "gate.gst_horizontal_error_too_high": "Horizontal accuracy {value} m (max. {limit} m) – keep the antenna still and wait",
      "gate.gst_vertical_error_too_high": "Vertical accuracy {value} m (max. {limit} m) – keep the antenna still and wait",
      "gate.unknown": "Blocked by the device: {code}",
      "gate.no_reason": "The device blocks measuring without naming a reason",
      "gate.no_verdict": "Waiting for the device's measurement verdict",
      "quality.required_fix_not_met": "required fix not reached",
      "quality.altitude_required": "height required",
      "ui.gate_row_fix": "Fix type",
      "ui.gate_row_corrections": "Corrections",
      "ui.gate_row_accuracy": "Accuracy H/V",
      "ui.gate_row_satellites": "Satellites and geometry",
      "ui.gate_row_gst": "GST output",
      "ui.gate_row_position": "Position and height",
      "ui.gate_row_other": "Further condition",
      "ui.gate_fix_required": "required: {fix}",
      "ui.gate_max": "max. {value}",
      "ui.gate_min": "min. {value}",
      "ui.gate_gst_present": "Accuracy estimate present",
      "ui.gate_position_ok": "Position and height present",
      "ui.gst_output": "GST output",
      "ui.gst_state_receiving": "receiving",
      "ui.gst_state_requested": "requested (attempt {attempt})",
      "ui.gst_state_waiting": "waiting for the receiver",
      "ui.gst_state_uart_error": "request failed (UART)",
      "ui.gst_pending": "this epoch pending (≤ 2 s)",
      "ui.corrections_unavailable": "NTRIP is currently not delivering corrections",
      "ui.switch_project": "Switch project",
      "ui.diagnostic_ok": "OK",
      "ui.diagnostic_warn": "Warning",
      "ui.diagnostic_fail": "Failed",
      "ui.link_live": "Live",
      "ui.link_delayed": "Delayed",
      "ui.link_lost": "No data",
      "ui.link_connecting": "Connecting",
      "ui.pill_gate_blocked": "blocked",
      "ui.pill_measurement_blocked": "measurement blocked",
      "ui.freshness_age": "Updated {seconds} s ago",
      "ui.freshness_fix_age": "position {seconds} s old",
      "ui.live_lost_title": "No status data from the device for {seconds} s",
      "ui.live_lost_hint": "Check the Wi-Fi connection to the device. The values shown are from the last answer.",
      "ui.live_never_title": "No status data from the device yet",
      "ui.live_never_hint": "Check the Wi-Fi connection to the device. Values appear once the device answers.",
      "ui.live_lost_action": "Check the connection – measuring unlocks with fresh device data.",
      "ui.live_restored": "Status data from the device again",
      "ui.feedback_dismiss": "Dismiss", "ui.feedback_region": "Messages",
      "ui.capture_starting_line": "Starting line …", "ui.capture_saving_vertex": "Saving line point …",
      "ui.capture_saving_asset": "Saving object point …", "ui.capture_pausing": "Pausing line …",
      "ui.capture_resuming": "Resuming line …", "ui.capture_finishing": "Finishing line …",
      "ui.capture_undoing": "Removing last point …", "ui.line_paused": "Line paused", "ui.line_resumed": "Line resumed",
      "ui.undo_point": "Undo", "ui.undo_changed": "The last point changed – nothing was removed.",
      "ui.undo_nothing": "Nothing to undo – no recorded point found",
      "ui.capture_checking": "Result unknown – checking the device …", "ui.capture_confirmed": "Saved – confirmed by the device",
      "ui.capture_unclear": "Result unclear – check the project", "ui.capture_open_project": "Open project",
      "ui.restoring_survey": "Restoring survey data …", "ui.optimizing_storage": "Optimizing recording storage …",
      "ui.restore_done": "Survey backup restored: {projects} projects, {points} points", "ui.restore_done_plain": "Survey backup restored",
      "ui.optimize_done": "Recording storage optimized",
      "ui.capture_cancelled": "Measurement cancelled",
      "ui.cause_parked_short": "NTRIP stopped", "ui.cause_parked_text": "NTRIP stopped after repeated failures – it starts again when the caster settings change, the device restarts or Wi-Fi reconnects",
      "ui.cause_off_short": "NTRIP off", "ui.cause_off_text": "NTRIP switched off – no correction data via NTRIP",
      "ui.cause_incomplete_short": "NTRIP not set up", "ui.cause_incomplete_text": "NTRIP is on, but no caster with host and mountpoint is configured",
      "ui.cause_unavailable_short": "NTRIP inactive",
      "ui.cause_waiting_wifi_short": "NTRIP waits for Wi-Fi", "ui.cause_waiting_wifi_text": "Waiting for Wi-Fi before connecting to the caster",
      "ui.cause_auth_short": "NTRIP login refused", "ui.cause_auth_text": "The caster refuses the NTRIP user or password",
      "ui.cause_mount_short": "NTRIP mountpoint missing", "ui.cause_mount_text": "The caster does not offer this mountpoint",
      "ui.cause_failed_short": "NTRIP error", "ui.cause_failed_text": "Caster connection failed ({error}) – the device retries automatically",
      "ui.cause_reconnecting_short": "NTRIP reconnecting", "ui.cause_reconnecting_text": "Connecting to the caster again after: {error}",
      "ui.cause_unknown_error": "unknown error",
      "ui.cause_action_check_ntrip": "Check NTRIP settings", "ui.cause_action_open_ntrip": "Open NTRIP settings", "ui.cause_action_setup": "Set up NTRIP",
      "ui.cause_action_diagnostics": "Open diagnostics", "ui.cause_action_wifi": "Check Wi-Fi",
      "ntrip_error.timeout": "timeout", "ntrip_error.network_error": "network error", "ntrip_error.unexpected_error": "unexpected error",
      "ntrip_error.stale_backlog": "stale data backlog", "ntrip_error.idle_timeout": "no data from the caster", "ntrip_error.eof": "connection closed by the caster",
      "ntrip_error.retries_exhausted": "retries exhausted", "ntrip_error.protocol_error": "protocol error", "ntrip_error.caster_error": "caster error",
      "ntrip_error.auth_error": "login refused", "ntrip_error.mount_error": "mountpoint not found",
      "ui.why_title": "Why can't I measure?", "ui.why_open": "Why not? Check step by step", "ui.why_close": "Close",
      "ui.why_no_data": "No current data", "ui.why_connecting": "Connecting to the device …",
      "ui.why_step_app": "App and device", "ui.why_step_receiver": "Receiver", "ui.why_step_wifi": "Wi-Fi",
      "ui.why_step_caster": "NTRIP caster", "ui.why_step_corrections": "Corrections", "ui.why_step_accuracy": "Accuracy",
      "ui.why_state_off": "Off", "ui.why_state_unknown": "Unknown",
      "ui.why_uart_silent": "No data from the receiver for {seconds} s – check cable and power",
      "ui.why_no_gga": "The receiver sends data, but no positions for {seconds} s – check its NMEA output",
      "ui.why_receiver_unknown": "Receiver state unknown (safe mode)",
      "ui.why_wifi_online": "Wi-Fi connected", "ui.why_wifi_connecting": "Connecting to Wi-Fi",
      "ui.why_wifi_setup": "Device is in setup mode – no internet for corrections",
      "ui.why_caster_ok": "Caster connected", "ui.why_caster_connecting": "Connecting to the caster …",
      "ui.why_corrections_none": "No corrections via NTRIP",
      "ui.why_corrections_late": "Corrections arrive late (link latency {seconds} s)",
      "ui.why_corrections_ok": "Corrections {seconds} s old", "ui.why_corrections_age_unknown": "Correction age unknown",
      // M2 #24-UI/#38a: navigation, field setup, device areas
      "app.measure": "Measure",
      "ui.route_field_setup": "Field setup", "ui.route_field_setup_kicker": "For new measurements",
      "ui.route_archive": "Archived projects",
      "ui.route_connections": "Connections", "ui.route_connections_kicker": "Internet, corrections and device access",
      "ui.route_display": "Operation & display", "ui.route_display_kicker": "This browser only",
      "ui.route_diagnostics": "Diagnostics", "ui.route_diagnostics_kicker": "RTCM, log, receiver console, support",
      "ui.route_rtcm": "RTCM message details", "ui.route_log": "Log", "ui.route_console": "Receiver console",
      "ui.route_maintenance": "Maintenance", "ui.route_maintenance_kicker": "Configuration backup and restart",
      "ui.route_device_overview": "Overview",
      "ui.context_nav_measure": "Measure sections", "ui.context_nav_projects": "Project sections", "ui.context_nav_device": "Device sections",
      "ui.setup_scope": "These settings apply to new measurements on this device, in every browser. Recorded points keep their own values.",
      "ui.setup_browser_hint": "Language, contrast and keeping the screen awake apply to this browser only: Device → Operation & display.",
      "ui.new_measurements_only": "For new measurements",
      "ui.method_title": "Measurement method",
      "ui.method_quality_help": "“Until quality is OK” keeps a sliding window of the last {count} accepted samples and records as soon as the average of that window also meets the measurement rules (rule “Quality OK sample window”). Rejected fixes do not restart the window.",
      "ui.method_saved": "Measurement method saved for new measurements",
      "ui.rules_title": "Measurement rules",
      "ui.rules_reset": "Restore factory values (measurement rules)",
      "ui.rules_reset_keeps_method": "The measurement method stays unchanged.",
      "ui.field_setup_points_keep": "Recorded points keep their antenna height and frame. To give existing RTK points a frame, use the project's frame action.",
      "ui.frame_epoch_help": "Frame epoch: for a plate-fixed frame such as ETRS89, enter the epoch your correction service states for its coordinates. With an ITRF-based service, coordinates refer to the time of observation, which every point already records (recorded_at_utc).",
      "ui.frame_reminder": "RTK fix without a reference frame: new RTK points are saved with the frame “unknown”. Enter the frame of the correction service in the field setup; it applies to new measurements.",
      "ui.frame_reminder_open": "Open field setup", "ui.frame_reminder_dismiss": "Hide for this session",
      "ui.frame_not_set": "not set", "ui.setup_summary_edit": "Edit field setup",
      "ui.method_short": "Method", "ui.antenna_height_short": "Antenna height", "ui.frame_short": "Reference frame",
      "ui.open_active_project": "Open map",
      "ui.group_internet": "Internet & corrections", "ui.group_access": "Device access",
      "ui.connections_backup_link": "Back up or restore the configuration: Maintenance",
      "ui.access_unavailable": "Device access could not be loaded. Internet & corrections still work.",
      "ui.qr_unavailable": "The QR codes could not be loaded.",
      "ui.label_unavailable": "Device code and QR codes are not available right now.",
      "ui.device_link_maintenance": "Configuration backup · restart",
      "ui.stream_key_rotated": "Stream key rotated",
      "ui.sign_out": "Sign out", "ui.sign_in_again": "Sign in again", "ui.sign_in_unsaved": "The session has ended. Signing in again discards the Wi-Fi and NTRIP values on this page that have not been saved.", "ui.display_scope": "Settings on this page apply to this browser only.",
      "ui.moved_to_measure": "Measurement method, measurement rules and field setup are under Measure → Field setup and apply to the whole device.",
      "ui.maintenance_backup_title": "Device configuration",
      "ui.maintenance_secrets": "The file contains Wi-Fi and NTRIP passwords in plain text. Keep it like a key.",
      "ui.maintenance_survey_hint": "Survey data (projects and points) are not part of it; back them up under Projects → Data management.",
      "ui.maintenance_restart_title": "Restart",
      "ui.maintenance_restart_scope": "The web app and the data streams pause until the device is back. Survey data and configuration are kept.",
      "ui.expert_only": "For experts: commands to the receiver",
      "ui.console_restart_link": "Restart the device: Maintenance",
      "ui.archive_entry": "Archived projects ({count})", "ui.archive_empty": "No archived projects.",
      "ui.data_management_title": "Survey data backup & data management",
      "ui.data_management_scope": "Backs up and restores all projects with their points. The device configuration (Wi-Fi, NTRIP, measurement settings) is backed up separately under Device → Maintenance.",
      "ui.survey_backup_download": "Download survey data backup",
      "ui.project_actions": "Project actions", "ui.project_export": "Export …", "ui.export_title": "Export project",
      "ui.export_share_hint": "Share hands the file to another app; Download saves it through the browser.",
      "ui.export_geojson": "GeoJSON – lines and object points", "ui.export_csv": "Point CSV – object points",
      "ui.export_archive": "Project archive – can be restored", "ui.export_share": "Share file", "ui.export_download": "Download file",
      // M2 #46: visible GeoJSON export flow
      "ui.export_flow_region": "GeoJSON export",
      "ui.export_flow_start": "Export GeoJSON",
      "ui.export_flow_retry": "Export again",
      "ui.export_flow_cancel": "Cancel export",
      "ui.export_flow_discard": "Discard file",
      "ui.export_flow_preparing": "The device is preparing the export …",
      "ui.export_flow_transferring": "The device is sending the export …",
      "ui.export_flow_meta": "{elapsed} elapsed · {size} received",
      "ui.export_flow_meta_done": "Took {elapsed} · {size}",
      "ui.export_flow_ready": "Export complete and checked ({size}). Share or download the file.",
      "ui.export_flow_shared": "File handed to the share dialog.",
      "ui.export_flow_share_cancelled": "Sharing cancelled. The file is still ready.",
      "ui.export_flow_share_unavailable": "Sharing is not possible here. Download the file instead.",
      "ui.export_flow_downloaded": "File handed to the browser. The browser shows where and whether it was saved.",
      "ui.export_flow_cancelled": "Export cancelled. No file was created.",
      "ui.export_flow_stalled": "No data from the device for {seconds} s. Check the connection and try again.",
      "ui.export_flow_lost": "The connection to the device was interrupted. Try again.",
      "ui.export_flow_refused": "Export failed: {message}",
      "ui.export_flow_incomplete": "The export ended early and is incomplete. No file was offered. Try again.",
      "ui.export_flow_close_title": "Cancel export?",
      "ui.export_flow_close_text": "The export is still running. If you close this sheet, it is cancelled and no file is created.",
      "ui.export_flow_close_stay": "Keep exporting",
      "ui.export_flow_close_confirm": "Cancel export",
      "ui.project_open_not_active": "Opening a project does not change the project you measure into.",
      "ui.go_measure": "Go to Measure", "ui.project_danger_title": "Delete", "ui.project_delete": "Delete project",
      "ui.map_menu": "Layers & full screen",
      // M2 #29c: theme choice
      "ui.theme_title": "Appearance", "ui.theme_auto": "Automatic", "ui.theme_light": "Light", "ui.theme_dark": "Dark",
      "ui.theme_help": "Automatic follows the light or dark setting of this phone or computer. High contrast is meant for bright sunlight.",
      // M2 #38b: frame suggestions from the correction service
      "ui.frame_suggestion_title": "Reported by the correction service",
      "ui.frame_suggestion_adopt": "Use reported frame", "ui.frame_suggestion_dismiss": "Dismiss",
      "ui.frame_suggestion_matches": "Confirmed by the correction service (RTCM 1300).",
      "ui.frame_suggestion_saved": "Reference frame saved for new measurements.",
      "ui.frame_anchor_plate_fixed": "plate-fixed", "ui.frame_anchor_global": "global",
      "ui.frame_epoch_none": "no epoch",
      // M2 #29b: route cache, loading time, stale stamp
      "ui.loading_elapsed": "… {seconds} s", "ui.loading_cancel": "Cancel loading",
      "ui.loading_cancelled": "Loading cancelled. Nothing was changed.",
      "ui.loading_project_map": "Loading project map (up to 500 points)",
      "ui.cached_as_of": "As of {time}", "ui.cached_refreshing": "updating …", "ui.cached_failed": "update failed",
      "survey.not_enough_line_points": "at least two line points required",
      "survey.overlapping_line_points": "line points overlap or are less than 5 cm apart",
      "survey.line_has_no_horizontal_length": "line has no horizontal length"
      ,"survey.large_line_gaps": "one or more line gaps exceed 2 m",
      "survey.large_vertical_steps": "one or more vertical steps exceed 0.5 m",
      // Stakeout WP3: import, targets, guidance
      "stakeout.go_north": "{value} m north", "stakeout.go_south": "{value} m south",
      "stakeout.go_east": "{value} m east", "stakeout.go_west": "{value} m west",
      "stakeout.on_target": "On target",
      "stakeout.cardinal_n": "N", "stakeout.cardinal_ne": "NE", "stakeout.cardinal_e": "E", "stakeout.cardinal_se": "SE",
      "stakeout.cardinal_s": "S", "stakeout.cardinal_sw": "SW", "stakeout.cardinal_w": "W", "stakeout.cardinal_nw": "NW"
      ,"stakeout.default_point": "Point {n}", "stakeout.default_line": "Line {n}",
      "import.unsupported_geometry": "Geometry not supported – not imported",
      "import.crs_not_wgs84": "The file uses a coordinate system other than WGS84 (CRS84 or EPSG:4326). Export it with WGS84 longitude and latitude.",
      "import.coordinate_out_of_range": "A coordinate is out of range – no WGS84 degree values (projected?)",
      "import.too_many_targets": "Too many targets or vertices for one target set",
      "import.already_imported": "This target set is already imported",
      "import.storage_reserve": "Not enough free device storage for this target set",
      "stakeout.block_invalid_json": "The file is not valid JSON",
      "stakeout.block_not_geojson": "The file is not GeoJSON (FeatureCollection, Feature or geometry)",
      "stakeout.block_invalid_position": "A position is not a list of finite numbers",
      "stakeout.block_too_many_features": "The file has more than 5000 features",
      "stakeout.block_file_too_large": "The file is larger than 4 MiB",
      "stakeout.skip_unsupported_geometry": "not imported – geometry not supported",
      "stakeout.skip_null_geometry": "not imported – no geometry",
      "stakeout.skip_empty_coordinates": "not imported – too few coordinates",
      "stakeout.hint_extra_position_elements": "Positions with more than three values: the extra values are ignored",
      "stakeout.hint_props_trimmed": "Property shortened or left out:",
      "stakeout.hint_closed_line": "Closed line: the last vertex repeats the first",
      "stakeout.hint_zero_segments": "{count} zero-length segments (repeated vertices)",
      "stakeout.hint_long_line": "{count} vertices – a recorded track? Use the selection help",
      "stakeout.hint_overlap": "Targets less than 2 cm apart:",
      "stakeout.hint_extent": "Extent {north} m north–south × {east} m east–west",
      "stakeout.hint_rover_range": "Nearest target {nearest} m, farthest {farthest} m from your position",
      "stakeout.hint_axis_swap": "All targets are more than 50 km away, with longitude and latitude swapped they would be close – check the axis order"
      ,"stakeout.status.open": "Open"
      ,"stakeout.status.confirmed": "Confirmed"
      ,"stakeout.status.skipped": "Skipped"
      ,"stakeout.status.unknown": "Unknown"
      ,"stakeout.calm.stakeout_busy": "The device is busy right now – again in a moment"
      ,"stakeout.calm.stakeout_write_uncertain": "Stakeout locked until the device restarts"
      ,"stakeout.error.project_not_active": "This project is not active. Activate it for stakeout first."
      ,"stakeout.error.recording_active": "A line is recording or a measurement runs. Pause the line first."
      ,"stakeout.error.project_changed": "The project changed meanwhile. Choose the target again."
      ,"stakeout.error.target_not_found": "This target no longer exists."
      ,"stakeout.error.set_not_found": "This target set no longer exists."
      ,"stakeout.error.set_damaged": "The target set is damaged. Remove it and import it again."
      ,"stakeout.error.storage_busy": "The device storage is in use right now. Try again in a moment."
      ,"stakeout.error.stakeout_busy": "The device is busy right now. Try again in a moment."
      ,"stakeout.error.stakeout_write_uncertain": "A stakeout change may not have been saved. Stakeout changes are locked until the device restarts."
      ,"stakeout.error.survey_loading": "The survey is still loading. Try again in a moment."
      ,"stakeout.error.stakeout_recovery_pending": "The target sets of this project wait for a device restart."
      ,"stakeout.error.confirmed_changed": "The progress of this set changed meanwhile. Check the overview and try again."
      ,"stakeout.error.too_many_sets": "The project already holds 4 target sets."
      ,"stakeout.error.import_too_large": "The import is too large for the device."
      ,"stakeout.error.orphaned_set_conflict": "A target set of an earlier project with the same ID is in the way."
      ,"stakeout.error.import_committed_response_failed": "The target set was stored, but the answer was cut off."
      ,"stakeout.error.stakeout_write_failed": "The device could not write the stakeout data."
      ,"stakeout.error.invalid_stakeout_settings": "These stakeout settings are not valid."
      ,"stakeout.error.part_not_found": "This part no longer exists."
      ,"stakeout.error.project_not_found": "This project no longer exists."
      ,"stakeout.error.invalid_stakeout_import": "The device refused the import package."
      ,"stakeout.error.stakeout_cleanup_pending": "A removed project of the same ID is still being cleaned up. Restart the device."
      ,"stakeout.error.benchmark_active": "A benchmark runs on the device right now."
      ,"stakeout.loading": "Loading stakeout"
      ,"stakeout.kicker_overview": "Stakeout"
      ,"stakeout.kicker_import": "Stakeout · import"
      ,"stakeout.kicker_targets": "Stakeout · targets"
      ,"stakeout.kicker_target": "Stakeout · guidance"
      ,"stakeout.entry": "Stakeout"
      ,"stakeout.entry_text": "Import target points and find them in the field."
      ,"stakeout.not_active": "This project is not active. Stakeout guidance needs the active project; the list and the import work without it."
      ,"stakeout.activate": "Activate for stakeout"
      ,"stakeout.activated": "Project active for stakeout"
      ,"stakeout.settings_title": "Stakeout settings"
      ,"stakeout.settings_line": "Tolerance {tolerance} m · fine radius {fine} m"
      ,"stakeout.settings_change": "Change"
      ,"stakeout.settings_save": "Save"
      ,"stakeout.settings_saved": "Stakeout settings saved"
      ,"stakeout.settings_tolerance": "Tolerance (m)"
      ,"stakeout.settings_fine": "Fine radius (m)"
      ,"stakeout.settings_tolerance_range": "Tolerance from 0.02 to 5.0 m"
      ,"stakeout.settings_fine_range": "Fine radius from 1 to 50 m"
      ,"stakeout.settings_pair": "The tolerance must be smaller than the fine radius"
      ,"stakeout.set_state_ok": "OK"
      ,"stakeout.set_state_damaged": "Damaged"
      ,"stakeout.set_state_incomplete": "Incomplete"
      ,"stakeout.set_state_recovery_pending": "Waits for a restart"
      ,"stakeout.set_targets": "{count} targets"
      ,"stakeout.delete": "Remove"
      ,"stakeout.delete_title": "Remove target set?"
      ,"stakeout.delete_text": "Remove {count} targets from the device:"
      ,"stakeout.delete_done": "Target set removed"
      ,"stakeout.delete_blocked_unknown": "Removing needs the progress of this set, which is unknown right now."
      ,"stakeout.import": "Import GeoJSON"
      ,"stakeout.import_blocked_sets": "The project already holds 4 target sets. Remove one to import another."
      ,"stakeout.next_open": "Next open target"
      ,"stakeout.resume": "Continue with the last target"
      ,"stakeout.target_list": "Target list"
      ,"stakeout.back_to_project": "Back to the project"
      ,"stakeout.all_done": "No open target"
      ,"stakeout.retry_overview": "Load again"
      ,"stakeout.calm_hint": "The stakeout overview could not be read yet."
      ,"stakeout.counts": "{targets} targets · {open} open · {confirmed} confirmed · {skipped} skipped"
      ,"stakeout.counts_unknown": "{count} unknown"
      ,"stakeout.no_sets": "No target set yet. Import a GeoJSON file."
      ,"stakeout.project_missing": "Project not found"
      ,"stakeout.line_paused": "Line paused"
      ,"stakeout.import_kind_line": "Line"
      ,"stakeout.import_kind_multipoint": "Point group"
      ,"stakeout.import_kind_point": "Point"
      ,"stakeout.import_feature": "Feature {n}"
      ,"stakeout.import_use_part": "Use this part"
      ,"stakeout.import_part_count": "{count} positions"
      ,"stakeout.import_apply": "Apply"
      ,"stakeout.import_every": "Every n-th vertex"
      ,"stakeout.import_from": "From"
      ,"stakeout.import_to": "To"
      ,"stakeout.import_head": "{kb} kB · {features} features"
      ,"stakeout.import_skipped_title": "Not imported"
      ,"stakeout.import_hints_title": "Hints"
      ,"stakeout.import_file": "GeoJSON file (WGS84, up to 4 MiB)"
      ,"stakeout.import_blocked_none": "Choose at least one target."
      ,"stakeout.import_blocked_many": "At most 500 targets per set."
      ,"stakeout.import_counter": "{count} of 500 targets selected"
      ,"stakeout.import_submit": "Import {count} targets"
      ,"stakeout.import_too_large": "The package has {bytes} bytes even with 250 vertices per line; the device takes up to {limit}."
      ,"stakeout.pause_line": "Pause the line"
      ,"stakeout.import_go_overview": "To the overview"
      ,"stakeout.import_done": "Target set imported"
      ,"stakeout.reverse": "Reversed"
      ,"stakeout.back_to_overview": "Back to the stakeout overview"
      ,"stakeout.loading_set": "Loading the targets …"
      ,"stakeout.retry_set": "Try again"
      ,"stakeout.state_no_target": "No target selected"
      ,"stakeout.state_no_position": "No position"
      ,"stakeout.state_far": "Too far – check the coordinates"
      ,"stakeout.state_coarse": "Walk towards the target"
      ,"stakeout.state_fine": "Close – fine guidance"
      ,"stakeout.state_settling": "Hold still {have}/{need}"
      ,"stakeout.state_ready": "Ready"
      ,"stakeout.state_unknown": "Unknown state"
      ,"stakeout.state_stale": "Data outdated – wait"
      ,"stakeout.reason.awaiting_fix": "Waiting for a usable position"
      ,"stakeout.reason.gst_pending": "Waiting for the accuracy of this epoch"
      ,"stakeout.reason.outside_tolerance": "Outside the tolerance"
      ,"stakeout.reason.gate_rejected": "The measurement gate refuses this position"
      ,"stakeout.reason.mean_outside_tolerance": "The mean of the window lies outside the tolerance"
      ,"stakeout.reason.horizontal_spread_too_high": "The positions of the window spread too much"
      ,"stakeout.reason.config_changed": "The settings changed – the window starts again"
      ,"stakeout.reason.session_error": "The stakeout session had an error"
      ,"stakeout.reason.recording_active": "A line is recording"
      ,"stakeout.reason.fix_stale": "The position is outdated"
      ,"stakeout.reason.corrections_missing": "Corrections missing"
      ,"stakeout.reason.write_uncertain": "Stakeout changes are locked until the device restarts"
      ,"stakeout.reason_unknown": "The device names a reason this page does not know"
      ,"stakeout.quality": "Data quality"
      ,"stakeout.quality_fix": "Fix"
      ,"stakeout.quality_sigma": "Accuracy"
      ,"stakeout.quality_correction": "Corrections"
      ,"stakeout.quality_age": "Fix age"
      ,"stakeout.quality_link": "Link"
      ,"stakeout.quality_ok": "ok"
      ,"stakeout.quality_corrections_missing": "Corrections missing"
      ,"stakeout.quality_corrections_old": "Corrections old"
      ,"stakeout.quality_delayed": "delayed"
      ,"stakeout.unexpected_data": "Unexpected device data"
      ,"stakeout.no_data_since": "No data for {seconds} s"
      ,"stakeout.stale_since": "Position outdated for {seconds} s"
      ,"stakeout.dial": "Target dial, north up"
      ,"stakeout.motion_direction": "Direction of movement"
      ,"stakeout.next_target": "Next target"
      ,"stakeout.all_done_short": "All done"
      ,"stakeout.target_missing": "This target is not in the project's target sets."
      ,"stakeout.reselect": "Select again"
      ,"stakeout.target_lost": "This target is no longer selected on the device."
      ,"stakeout.follow": "Follow"
      ,"stakeout.zoom_out": "Zoom out"
      ,"stakeout.zoom_in": "Zoom in"
      ,"stakeout.mode_search": "Search"
      ,"stakeout.mode_fine": "Fine"
      ,"stakeout.remaining": "{value} m to go"
      ,"stakeout.tolerance_window": "Tolerance {tolerance} m, quality window {have}/{need}"
      // Stakeout WP4: actions
      ,"stakeout.action.confirm": "Confirm"
      ,"stakeout.action.found": "Found on site"
      ,"stakeout.action.skip": "Skip"
      ,"stakeout.action.reopen": "Reopen"
      ,"stakeout.found_hint": "Saves a stakeout point at the current position without the quality window; its quality is recorded as measured."
      ,"stakeout.note": "Note (optional)"
      ,"stakeout.saving": "Saving…"
      ,"stakeout.saved": "Saved"
      ,"stakeout.unconfirmed": "Not confirmed – check the list"
      ,"stakeout.changed": "The target changed meanwhile"
      ,"stakeout.error.target_changed": "The target changed meanwhile. Check its state in the list."
      ,"stakeout.error.target_not_open": "This target is no longer open."
      ,"stakeout.error.target_open": "This target is already open."
      ,"stakeout.error.not_ready": "Not ready to confirm yet."
      ,"stakeout.error.measurement_active": "A measurement is running. Wait until it ends."
      ,"stakeout.error.quality_rejected": "The position quality was rejected."
      ,"stakeout.error.point_limit_reached": "The project already holds the maximum number of points."
      ,"stakeout.error.no_position": "No current position. Wait for a fresh fix."
      ,"stakeout.error.storage_reserve": "Not enough free device storage to save this."
      ,"stakeout.error.tracking_storage_failed": "The device could not save the point. The target stays open."
      ,"stakeout.error.invalid_request": "The device refused the request."
      ,"stakeout.error.undo_candidate_changed": "The last point changed meanwhile. Load the project again."
      ,"asset.stakeout": "Stakeout check"
      ,"stakeout.progress_failed": "The stakeout progress could not be loaded. Actions wait until it is loaded."
      ,"stakeout.export": "Export stakeout"
      ,"stakeout.export_hint": "The file shows the state when the export starts."
      ,"stakeout.export_geojson": "GeoJSON"
      ,"stakeout.export_geojson_text": "Targets with status and stakeout points; can be imported again"
      ,"stakeout.export_csv": "CSV"
      ,"stakeout.export_csv_text": "One row per target"
      ,"archive.stakeout_data": "Stakeout data present"
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
      "ui.precision_legacy": "Altpunkt: Positionsraster etwa 0,4 m",
      "ui.legacy_vertices": "{count} Linienpunkte vor V15.9 sind auf etwa 0,4 m gerastert",
      "ui.legacy_vertex": "Ein Linienpunkt vor V15.9 ist auf etwa 0,4 m gerastert",
      "ui.precision_legacy_short": "Altpunkt",
      "ui.recorded_utc": "Aufgenommen (UTC)",
      "ui.clock_unsynced": "Die Uhr war bei der Aufnahme nicht synchronisiert",
      "ui.clock_unsynced_short": "Uhr nicht synchron",
      "ui.legacy_values_skipped": "{count} Altwerte konnten nicht umgerechnet werden und fehlen; siehe Gerätelog",
      "ui.legacy_value_skipped": "Ein Altwert konnte nicht umgerechnet werden und fehlt; siehe Gerätelog",
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
      "ui.field_readiness": "Feldbereitschaft", "ui.receiver_access": "Empfänger und Zugang",
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
      "ui.no_current_position": "Keine aktuelle Position", "ui.fix_stale": "Position veraltet seit {seconds} s",
      "ui.route_without_approval": "Routenaufnahme ist ohne Messfreigabe möglich.", "ui.start_line_survey_blocked": "Vermessungslinien brauchen die Messfreigabe. Für eine Aufnahme ohne Freigabe Route wählen.",
      "ui.phase_waiting_fix": "Warte auf {fix} und gültiges GST", "ui.fix_and_corrections": "{fix} + Korrekturen",
      "ui.start_line_route_hint": "Routenaufnahme ist ohne Messfreigabe möglich. Vermessungslinien und -punkte werden frei, sobald alle Feldbedingungen erfüllt sind.", "ui.no_project_hint": "Noch kein Projekt gewählt – zuerst ein Projekt anlegen oder wählen, dann Linie starten.", "ui.action_refused_reload": "Aktion abgelehnt – Seite neu laden und erneut versuchen.",
      "ui.fix_stale_short": "Position veraltet", "ui.receiver_position": "Empfängerposition",
      "ui.correction_link": "Korrekturverbindung", "ui.corrections_age": "Korrekturen {seconds} s alt",
      "ui.corrections_age_unknown": "Korrekturalter unbekannt", "ui.link_latency": "Link-Latenz {seconds} s",
      "ui.arrival_age_short": "Ankunft {seconds} s", "ui.corrections_stale": "Korrekturen veraltet: Epochen {seconds} s alt",
      "ui.epoch_age": "Epochenalter", "ui.arrival_age": "Ankunftsalter", "ui.no_epoch_yet": "Noch keine Epoche dekodiert",
      "ui.gate_softened": "Gate gelockert", "ui.gate_profile": "Gate-Profil",
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
      "ui.confirm_restore_detail": "Dies ersetzt die WLAN- und NTRIP-Einstellungen, Messregeln und Messmethode sowie die Feldeinrichtung und startet das Gerät neu. Sicherungen älterer Firmware enthalten nur WLAN- und NTRIP-Einstellungen. Fortfahren?",
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
      "ui.frame_mix_warning": "Dieses Projekt enthält Punkte in mehr als einem Bezugsrahmen: {frames}", "ui.frame_unknown_warning": "Der Bezugsrahmen der RTK-Punkte dieses Projekts ist unbekannt. Die Feldeinrichtung gilt nur für neue Messungen; bestehenden Punkten im Projekt einen Rahmen zuordnen.", "ui.frame_assign_action": "Bezugsrahmen zuordnen", "ui.frame_assign_title": "Bezugsrahmen für RTK-Punkte ohne Rahmen zuordnen", "ui.frame_assign_submit": "Rahmen zuordnen", "ui.frame_revoke_action": "Zuordnung zurücknehmen", "ui.frame_revoke_title": "Rahmenzuordnung zurücknehmen", "ui.frame_unknown_key": "unbekannt", "ui.frame_mix_hint": "DGPS-Fixe liegen in WGS84/ITRF, RTK-Fixe im Rahmen des Korrekturdienstes (in Europa etwa 0,7 bis 0,9 m Versatz). Wer DGPS akzeptiert, mischt Rahmen innerhalb eines Projekts.", "ui.field_setup": "Feldeinrichtung", "ui.field_setup_unavailable": "Feldeinrichtung konnte nicht geladen werden.", "ui.vertical_ground": "Boden", "ui.vertical_phase_centre": "Phasenzentrum", "ui.field_setup_save": "Feldeinrichtung speichern", "ui.field_setup_saved": "Feldeinrichtung gespeichert",
      "ui.antenna_height": "Antennenhöhe über dem Bodenpunkt",
      "ui.antenna_height_help": "Abstand vom Bodenpunkt zum Phasenzentrum der Antenne (Stablänge plus Phasenzentrumsversatz der Antenne), 0 bis 3 m. Wird mit jeder Messung gespeichert; die Rohhöhen des Empfängers bleiben unverändert.",
      "ui.height_msl": "Höhe (MSL, Phasenzentrum)", "ui.height_ellipsoid": "Ellipsoidhöhe",
      "ui.geoid_separation": "Geoidundulation", "ui.ground_height": "Bodenhöhe (MSL)",
      "ui.reference_frame": "Bezugsrahmen des Korrekturdienstes",
      "ui.reference_frame_help": "Rahmen der RTK-Basis oder des Netzes (zum Beispiel ETRS89 bei einem europäischen Landesdienst). Wird mit jeder RTK-Messung gespeichert; Standalone- und SBAS-Fixe sind WGS84/ITRF. Leer lassen, wenn unbekannt.",
      "ui.reference_frame_epoch": "Rahmenepoche (optional)",
      "ui.reference_frame_ntrip_link": "NTRIP-Einstellungen (Konfigurationsseite)",
      "ui.antenna_height_zero": "Antennenhöhe mit 0 konfiguriert; die Bodenhöhe entspricht der Höhe des Phasenzentrums.",
      "ui.antenna_height_missing": "Für diesen Punkt wurde keine Antennenhöhe gespeichert (ältere Firmware); keine Bodenhöhe.",
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
      "ui.hotspot_unconfigured": "Hotspot nicht eingerichtet",
      "ui.setup_hotspot_warning": "Bleibt der Setup-Hotspot nach etwa 30 Sekunden sichtbar, konnten die Zugangsdaten nicht verwendet werden.",
      "ui.not_documented_families": "Nicht die dokumentierten Familien —",
      "ui.works_anyway": "funktioniert aber trotzdem", "ui.with_msm_measured": "Den Strom am Caster und auf einem bekannten Kontrollpunkt validieren.",
      "ui.device_code_qr_prefix": "Der Gerätecode und die QR-Codes werden nach der Anmeldung unter",
      "ui.or_label_shown": "bzw. Etikett angezeigt.",
      "ui.network_format_help": "Schreibweise Name:Passwort. Steht nur der Name da, bleibt das gespeicherte Passwort erhalten. Das Gerät probiert die Netze in der eingetragenen Reihenfolge: zuerst das WLAN oben, dann diese Zeilen von oben nach unten. Ein Scan überspringt nur Netze, die nicht sichtbar sind; sieht er keines davon, probiert das Gerät alle. Das WLAN, das normalerweise verwendet werden soll, bitte oben als SSID und Passwort eintragen; dieses Feld ist nur für zusätzliche Alternativen.",
      "ui.scan_still_running": "Die Suche läuft noch. Bitte gleich erneut versuchen.",
      "ui.scan_no_answer": "Das Gerät hat nicht rechtzeitig geantwortet.",
      "ui.legacy_setup_notice": "Dies ist die einfache Ersatzseite. Die vollständige Einrichtung bietet die Feld-App unter Gerät > Konfiguration.",
      "ui.open_field_app_setup": "Einrichtung in der Feld-App öffnen",
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
      "ui.measurement_no_time_limit": "ohne Zeitlimit", "ui.measurement_time_left": "noch {seconds} s",
      "ui.measurement_stopped_stale": "Messung abgebrochen: Position veraltet seit {seconds} s",
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
      "gate.no_position": "Keine Position – freie Sicht zum Himmel abwarten",
      "gate.rtk_fixed_required": "{fix} statt RTK FIXED – auf RTK FIXED warten oder Korrekturen prüfen",
      "gate.required_fix_not_met": "{fix} erreicht {required} nicht – auf besseren Fix warten oder Korrekturen prüfen",
      "gate.not_all_samples_rtk_fixed": "Nicht alle Messwerte waren RTK FIXED – erneut messen",
      "gate.too_few_satellites": "{count} Satelliten (mind. {limit}) – Standort mit freierer Sicht zum Himmel wählen",
      "gate.hdop_too_high": "HDOP {value} (max. {limit}) – auf bessere Satellitengeometrie warten",
      "gate.altitude_required": "Position ohne Höhe – auf vollständige Position warten",
      "gate.corrections_too_old": "Korrekturen {age} s alt (max. {limit} s) – Caster/Internet prüfen",
      "gate.horizontal_spread_too_high": "Messwerte streuen horizontal zu stark – Antenne ruhig halten und erneut messen",
      "gate.gst_required": "Empfänger liefert keine Genauigkeitsangabe (GST). Das Gerät fordert sie an (Versuch {attempt}).",
      "gate.gst_required_waiting": "Empfänger liefert noch keine Genauigkeitsangabe (GST). Das Gerät fordert sie gleich an.",
      "gate.gst_required_uart_error": "Empfänger liefert keine Genauigkeitsangabe (GST); die Anforderung ist fehlgeschlagen – Empfängerverbindung prüfen.",
      "gate.gst_required_receiving": "Genauigkeitsangabe (GST) fehlt für diese Position – abwarten; bei Dauer den Empfänger prüfen.",
      "gate.not_all_samples_have_gst": "Nicht alle Messwerte hatten eine Genauigkeitsangabe (GST) – erneut messen",
      "gate.gst_horizontal_error_too_high": "Horizontale Genauigkeit {value} m (max. {limit} m) – Antenne ruhig halten und abwarten",
      "gate.gst_vertical_error_too_high": "Vertikale Genauigkeit {value} m (max. {limit} m) – Antenne ruhig halten und abwarten",
      "gate.unknown": "Vom Gerät gesperrt: {code}",
      "gate.no_reason": "Das Gerät sperrt das Messen ohne Grundangabe",
      "gate.no_verdict": "Warte auf das Messurteil des Geräts",
      "quality.required_fix_not_met": "erforderlicher Fix nicht erreicht",
      "quality.altitude_required": "Höhe erforderlich",
      "ui.gate_row_fix": "Fix-Typ",
      "ui.gate_row_corrections": "Korrekturen",
      "ui.gate_row_accuracy": "Genauigkeit H/V",
      "ui.gate_row_satellites": "Satelliten und Geometrie",
      "ui.gate_row_gst": "GST-Ausgabe",
      "ui.gate_row_position": "Position und Höhe",
      "ui.gate_row_other": "Weitere Bedingung",
      "ui.gate_fix_required": "erforderlich: {fix}",
      "ui.gate_max": "max. {value}",
      "ui.gate_min": "mind. {value}",
      "ui.gate_gst_present": "Genauigkeitsangabe vorhanden",
      "ui.gate_position_ok": "Position und Höhe vorhanden",
      "ui.gst_output": "GST-Ausgabe",
      "ui.gst_state_receiving": "empfangen",
      "ui.gst_state_requested": "angefordert (Versuch {attempt})",
      "ui.gst_state_waiting": "warte auf Empfänger",
      "ui.gst_state_uart_error": "Anforderung fehlgeschlagen (UART)",
      "ui.gst_pending": "aktuelle Epoche ausstehend (≤ 2 s)",
      "ui.corrections_unavailable": "NTRIP liefert derzeit keine Korrekturen",
      "ui.switch_project": "Projekt wechseln",
      "ui.diagnostic_ok": "in Ordnung",
      "ui.diagnostic_warn": "Warnung",
      "ui.diagnostic_fail": "fehlgeschlagen",
      "ui.link_live": "Live",
      "ui.link_delayed": "Verzögert",
      "ui.link_lost": "Keine Daten",
      "ui.link_connecting": "Verbinde",
      "ui.pill_gate_blocked": "gesperrt",
      "ui.pill_measurement_blocked": "Messung gesperrt",
      "ui.freshness_age": "Stand: vor {seconds} s",
      "ui.freshness_fix_age": "Position {seconds} s alt",
      "ui.live_lost_title": "Keine Statusdaten vom Gerät seit {seconds} s",
      "ui.live_lost_hint": "WLAN-Verbindung zum Gerät prüfen. Die angezeigten Werte stammen aus der letzten Antwort.",
      "ui.live_never_title": "Noch keine Statusdaten vom Gerät",
      "ui.live_never_hint": "WLAN-Verbindung zum Gerät prüfen. Werte erscheinen, sobald das Gerät antwortet.",
      "ui.live_lost_action": "Verbindung prüfen – Messen wird mit aktuellen Gerätedaten wieder freigegeben.",
      "ui.live_restored": "Wieder Statusdaten vom Gerät",
      "ui.feedback_dismiss": "Schließen", "ui.feedback_region": "Meldungen",
      "ui.capture_starting_line": "Linie wird gestartet …", "ui.capture_saving_vertex": "Linienpunkt wird gespeichert …",
      "ui.capture_saving_asset": "Objektpunkt wird gespeichert …", "ui.capture_pausing": "Linie wird pausiert …",
      "ui.capture_resuming": "Linie wird fortgesetzt …", "ui.capture_finishing": "Linie wird beendet …",
      "ui.capture_undoing": "Letzter Punkt wird entfernt …", "ui.line_paused": "Linie pausiert", "ui.line_resumed": "Linie fortgesetzt",
      "ui.undo_point": "Rückgängig", "ui.undo_changed": "Der letzte Punkt hat sich geändert – nichts entfernt.",
      "ui.undo_nothing": "Nichts rückgängig zu machen – kein aufgezeichneter Punkt gefunden",
      "ui.capture_checking": "Ergebnis unbekannt – Gerät wird geprüft …", "ui.capture_confirmed": "Gespeichert – vom Gerät bestätigt",
      "ui.capture_unclear": "Ergebnis unklar – im Projekt prüfen", "ui.capture_open_project": "Projekt öffnen",
      "ui.restoring_survey": "Vermessungsdaten werden wiederhergestellt …", "ui.optimizing_storage": "Aufnahmespeicher wird optimiert …",
      "ui.restore_done": "Sicherung wiederhergestellt: {projects} Projekte, {points} Punkte", "ui.restore_done_plain": "Sicherung wiederhergestellt",
      "ui.optimize_done": "Aufnahmespeicher optimiert",
      "ui.capture_cancelled": "Messung abgebrochen",
      "ui.cause_parked_short": "NTRIP gestoppt", "ui.cause_parked_text": "NTRIP nach wiederholten Fehlern gestoppt – startet erst wieder nach geänderten Caster-Einstellungen, Neustart oder neuer WLAN-Verbindung",
      "ui.cause_off_short": "NTRIP aus", "ui.cause_off_text": "NTRIP deaktiviert – keine Korrekturdaten über NTRIP",
      "ui.cause_incomplete_short": "NTRIP nicht eingerichtet", "ui.cause_incomplete_text": "NTRIP ist eingeschaltet, aber kein Caster mit Host und Mountpoint eingerichtet",
      "ui.cause_unavailable_short": "NTRIP inaktiv",
      "ui.cause_waiting_wifi_short": "NTRIP wartet auf WLAN", "ui.cause_waiting_wifi_text": "Wartet auf WLAN, bevor der Caster verbunden wird",
      "ui.cause_auth_short": "NTRIP-Anmeldung abgelehnt", "ui.cause_auth_text": "Der Caster lehnt NTRIP-Benutzer oder -Passwort ab",
      "ui.cause_mount_short": "NTRIP-Mountpoint fehlt", "ui.cause_mount_text": "Der Caster bietet diesen Mountpoint nicht an",
      "ui.cause_failed_short": "NTRIP-Fehler", "ui.cause_failed_text": "Caster-Verbindung fehlgeschlagen ({error}) – das Gerät versucht es selbst erneut",
      "ui.cause_reconnecting_short": "NTRIP verbindet neu", "ui.cause_reconnecting_text": "Erneuter Verbindungsaufbau zum Caster nach: {error}",
      "ui.cause_unknown_error": "unbekannter Fehler",
      "ui.cause_action_check_ntrip": "NTRIP-Einstellungen prüfen", "ui.cause_action_open_ntrip": "NTRIP-Einstellungen öffnen", "ui.cause_action_setup": "NTRIP einrichten",
      "ui.cause_action_diagnostics": "Diagnose öffnen", "ui.cause_action_wifi": "WLAN prüfen",
      "ntrip_error.timeout": "Zeitüberschreitung", "ntrip_error.network_error": "Netzwerkfehler", "ntrip_error.unexpected_error": "unerwarteter Fehler",
      "ntrip_error.stale_backlog": "veralteter Datenstau", "ntrip_error.idle_timeout": "keine Daten vom Caster", "ntrip_error.eof": "Verbindung vom Caster beendet",
      "ntrip_error.retries_exhausted": "Versuche aufgebraucht", "ntrip_error.protocol_error": "Protokollfehler", "ntrip_error.caster_error": "Casterfehler",
      "ntrip_error.auth_error": "Anmeldung abgelehnt", "ntrip_error.mount_error": "Mountpoint nicht gefunden",
      "ui.why_title": "Warum kann ich nicht messen?", "ui.why_open": "Warum nicht? Schritt für Schritt prüfen", "ui.why_close": "Schließen",
      "ui.why_no_data": "Keine aktuellen Daten", "ui.why_connecting": "Verbindung zum Gerät wird aufgebaut …",
      "ui.why_step_app": "App und Gerät", "ui.why_step_receiver": "Empfänger", "ui.why_step_wifi": "WLAN",
      "ui.why_step_caster": "NTRIP-Caster", "ui.why_step_corrections": "Korrekturen", "ui.why_step_accuracy": "Genauigkeit",
      "ui.why_state_off": "Aus", "ui.why_state_unknown": "Unbekannt",
      "ui.why_uart_silent": "Seit {seconds} s keine Daten vom Empfänger – Kabel und Stromversorgung prüfen",
      "ui.why_no_gga": "Der Empfänger sendet Daten, aber seit {seconds} s keine Positionen – seine NMEA-Ausgabe prüfen",
      "ui.why_receiver_unknown": "Empfängerzustand unbekannt (abgesicherter Modus)",
      "ui.why_wifi_online": "WLAN verbunden", "ui.why_wifi_connecting": "Verbindung zum WLAN wird aufgebaut",
      "ui.why_wifi_setup": "Gerät ist im Einrichtungsmodus – kein Internet für Korrekturen",
      "ui.why_caster_ok": "Caster verbunden", "ui.why_caster_connecting": "Verbindung zum Caster wird aufgebaut …",
      "ui.why_corrections_none": "Keine Korrekturen über NTRIP",
      "ui.why_corrections_late": "Korrekturen kommen verspätet (Verbindungslatenz {seconds} s)",
      "ui.why_corrections_ok": "Korrekturen {seconds} s alt", "ui.why_corrections_age_unknown": "Korrekturalter unbekannt",
      // M2 #24-UI/#38a: navigation, field setup, device areas
      "app.measure": "Messen",
      "ui.route_field_setup": "Feldeinrichtung", "ui.route_field_setup_kicker": "Für neue Messungen",
      "ui.route_archive": "Archivierte Projekte",
      "ui.route_connections": "Verbindungen", "ui.route_connections_kicker": "Internet, Korrekturen und Gerätezugriff",
      "ui.route_display": "Bedienung & Anzeige", "ui.route_display_kicker": "Nur dieser Browser",
      "ui.route_diagnostics": "Diagnose", "ui.route_diagnostics_kicker": "RTCM, Protokoll, Empfängerkonsole, Support",
      "ui.route_rtcm": "RTCM-Nachrichtendetails", "ui.route_log": "Protokoll", "ui.route_console": "Empfängerkonsole",
      "ui.route_maintenance": "Wartung", "ui.route_maintenance_kicker": "Konfigurationssicherung und Neustart",
      "ui.route_device_overview": "Übersicht",
      "ui.context_nav_measure": "Bereiche Messen", "ui.context_nav_projects": "Bereiche Projekte", "ui.context_nav_device": "Bereiche Gerät",
      "ui.setup_scope": "Diese Vorgaben gelten für neue Messungen dieses Geräts, in allen Browsern. Aufgezeichnete Punkte behalten ihre eigenen Werte.",
      "ui.setup_browser_hint": "Sprache, Kontrast und Bildschirm wach halten gelten nur für diesen Browser: Gerät → Bedienung & Anzeige.",
      "ui.new_measurements_only": "Für neue Messungen",
      "ui.method_title": "Messmethode",
      "ui.method_quality_help": "„Bis Qualität OK“ führt ein gleitendes Fenster der letzten {count} akzeptierten Proben und nimmt auf, sobald auch der Mittelwert dieses Fensters die Messregeln erfüllt (Messregel „Stichprobenfenster Qualität OK“). Verworfene Fixes starten das Fenster nicht neu.",
      "ui.method_saved": "Messmethode für neue Messungen gespeichert",
      "ui.rules_title": "Messregeln",
      "ui.rules_reset": "Werkswerte wiederherstellen (Messregeln)",
      "ui.rules_reset_keeps_method": "Die Messmethode bleibt unverändert.",
      "ui.field_setup_points_keep": "Aufgezeichnete Punkte behalten Antennenhöhe und Rahmen. Bestehenden RTK-Punkten einen Rahmen geben: Rahmenaktion im Projekt.",
      "ui.frame_epoch_help": "Rahmenepoche: Bei einem plattenfesten Rahmen wie ETRS89 die Epoche eintragen, die der Korrekturdienst für seine Koordinaten angibt. Bei einem ITRF-basierten Dienst beziehen sich die Koordinaten auf die Beobachtungszeit, die jeder Punkt bereits speichert (recorded_at_utc).",
      "ui.frame_reminder": "RTK-Fix ohne Bezugsrahmen: Neue RTK-Punkte werden mit Rahmen „unbekannt“ gespeichert. Trage den Rahmen des Korrekturdienstes in der Feldeinrichtung ein; er gilt für neue Messungen.",
      "ui.frame_reminder_open": "Feldeinrichtung öffnen", "ui.frame_reminder_dismiss": "Für diese Sitzung ausblenden",
      "ui.frame_not_set": "nicht festgelegt", "ui.setup_summary_edit": "Feldeinrichtung bearbeiten",
      "ui.method_short": "Messmethode", "ui.antenna_height_short": "Antennenhöhe", "ui.frame_short": "Bezugsrahmen",
      "ui.open_active_project": "Karte öffnen",
      "ui.group_internet": "Internet & Korrekturen", "ui.group_access": "Gerätezugriff",
      "ui.connections_backup_link": "Konfiguration sichern oder wiederherstellen: Wartung",
      "ui.access_unavailable": "Der Gerätezugriff konnte nicht geladen werden. Internet & Korrekturen funktionieren weiter.",
      "ui.qr_unavailable": "Die QR-Codes konnten nicht geladen werden.",
      "ui.label_unavailable": "Gerätecode und QR-Codes sind gerade nicht verfügbar.",
      "ui.device_link_maintenance": "Konfigurationssicherung · Neustart",
      "ui.stream_key_rotated": "Stream-Schlüssel erneuert",
      "ui.sign_out": "Abmelden", "ui.sign_in_again": "Erneut anmelden", "ui.sign_in_unsaved": "Die Sitzung ist beendet. Beim erneuten Anmelden gehen die noch nicht gespeicherten WLAN- und NTRIP-Werte dieser Seite verloren.", "ui.display_scope": "Die Einstellungen dieser Seite gelten nur für diesen Browser.",
      "ui.moved_to_measure": "Messmethode, Messregeln und Feldeinrichtung liegen unter Messen → Feldeinrichtung und gelten für das ganze Gerät.",
      "ui.maintenance_backup_title": "Gerätekonfiguration",
      "ui.maintenance_secrets": "Die Datei enthält WLAN- und NTRIP-Passwörter im Klartext. Wie einen Schlüssel aufbewahren.",
      "ui.maintenance_survey_hint": "Messdaten (Projekte und Punkte) gehören nicht dazu; sie sichern Sie unter Projekte → Datenverwaltung.",
      "ui.maintenance_restart_title": "Neustart",
      "ui.maintenance_restart_scope": "Web-App und Datenströme pausieren, bis das Gerät wieder da ist. Messdaten und Konfiguration bleiben erhalten.",
      "ui.expert_only": "Für Experten: Befehle an den Empfänger",
      "ui.console_restart_link": "Gerät neu starten: Wartung",
      "ui.archive_entry": "Archivierte Projekte ({count})", "ui.archive_empty": "Keine archivierten Projekte.",
      "ui.data_management_title": "Messdatensicherung & Datenverwaltung",
      "ui.data_management_scope": "Sichert und stellt alle Projekte mit ihren Punkten wieder her. Die Gerätekonfiguration (WLAN, NTRIP, Messvorgaben) sichern Sie getrennt unter Gerät → Wartung.",
      "ui.survey_backup_download": "Messdatensicherung herunterladen",
      "ui.project_actions": "Projektaktionen", "ui.project_export": "Exportieren …", "ui.export_title": "Projekt exportieren",
      "ui.export_share_hint": "Teilen übergibt die Datei an eine andere App; Herunterladen speichert sie über den Browser.",
      "ui.export_geojson": "GeoJSON – Linien und Objektpunkte", "ui.export_csv": "Punkt-CSV – Objektpunkte",
      "ui.export_archive": "Projektarchiv – wiederherstellbar", "ui.export_share": "Datei teilen", "ui.export_download": "Datei herunterladen",
      // M2 #46: visible GeoJSON export flow
      "ui.export_flow_region": "GeoJSON-Export",
      "ui.export_flow_start": "GeoJSON exportieren",
      "ui.export_flow_retry": "Erneut exportieren",
      "ui.export_flow_cancel": "Export abbrechen",
      "ui.export_flow_discard": "Datei verwerfen",
      "ui.export_flow_preparing": "Das Gerät bereitet den Export vor …",
      "ui.export_flow_transferring": "Das Gerät überträgt den Export …",
      "ui.export_flow_meta": "Laufzeit {elapsed} · {size} empfangen",
      "ui.export_flow_meta_done": "Dauer {elapsed} · {size}",
      "ui.export_flow_ready": "Export vollständig und geprüft ({size}). Datei teilen oder herunterladen.",
      "ui.export_flow_shared": "Datei an den Teilen-Dialog übergeben.",
      "ui.export_flow_share_cancelled": "Teilen abgebrochen. Die Datei liegt weiter bereit.",
      "ui.export_flow_share_unavailable": "Teilen ist hier nicht möglich. Bitte die Datei herunterladen.",
      "ui.export_flow_downloaded": "Datei an den Browser übergeben. Ob und wo sie gespeichert wurde, zeigt der Browser.",
      "ui.export_flow_cancelled": "Export abgebrochen. Es wurde keine Datei erstellt.",
      "ui.export_flow_stalled": "Seit {seconds} s keine Daten vom Gerät. Verbindung prüfen und erneut versuchen.",
      "ui.export_flow_lost": "Die Verbindung zum Gerät wurde unterbrochen. Bitte erneut versuchen.",
      "ui.export_flow_refused": "Export fehlgeschlagen: {message}",
      "ui.export_flow_incomplete": "Der Export endete vorzeitig und ist unvollständig. Es wurde keine Datei angeboten. Bitte erneut versuchen.",
      "ui.export_flow_close_title": "Export abbrechen?",
      "ui.export_flow_close_text": "Der Export läuft noch. Beim Schließen wird er abgebrochen und es entsteht keine Datei.",
      "ui.export_flow_close_stay": "Bleiben",
      "ui.export_flow_close_confirm": "Abbrechen",
      "ui.project_open_not_active": "Das Öffnen ändert nicht das Projekt, in das gemessen wird.",
      "ui.go_measure": "Zum Messen", "ui.project_danger_title": "Löschen", "ui.project_delete": "Projekt löschen",
      "ui.map_menu": "Ebenen & Vollbild",
      // M2 #29c: theme choice
      "ui.theme_title": "Darstellung", "ui.theme_auto": "Automatisch", "ui.theme_light": "Hell", "ui.theme_dark": "Dunkel",
      "ui.theme_help": "Automatisch folgt der Hell-/Dunkel-Einstellung dieses Telefons oder Computers. Hoher Kontrast ist für helles Sonnenlicht gedacht.",
      // M2 #38b: frame suggestions from the correction service
      "ui.frame_suggestion_title": "Vom Korrekturdienst gemeldet",
      "ui.frame_suggestion_adopt": "Gemeldeten Rahmen übernehmen", "ui.frame_suggestion_dismiss": "Verwerfen",
      "ui.frame_suggestion_matches": "Vom Korrekturdienst bestätigt (RTCM 1300).",
      "ui.frame_suggestion_saved": "Bezugsrahmen für neue Messungen gespeichert.",
      "ui.frame_anchor_plate_fixed": "plattenfest", "ui.frame_anchor_global": "global",
      "ui.frame_epoch_none": "ohne Epoche",
      // M2 #29b: route cache, loading time, stale stamp
      "ui.loading_elapsed": "… {seconds} s", "ui.loading_cancel": "Laden abbrechen",
      "ui.loading_cancelled": "Laden abgebrochen. Es wurde nichts geändert.",
      "ui.loading_project_map": "Projektkarte wird geladen (bis 500 Punkte)",
      "ui.cached_as_of": "Stand: {time}", "ui.cached_refreshing": "wird aktualisiert …", "ui.cached_failed": "Aktualisierung fehlgeschlagen",
      "survey.not_enough_line_points": "mindestens zwei Linienpunkte erforderlich",
      "survey.overlapping_line_points": "Linienpunkte überlagern sich oder liegen weniger als 5 cm auseinander",
      "survey.line_has_no_horizontal_length": "Linie hat keine horizontale Länge"
      ,"survey.large_line_gaps": "mindestens eine Linienlücke ist größer als 2 m",
      "survey.large_vertical_steps": "mindestens ein Höhensprung ist größer als 0,5 m",
      // Stakeout WP3: import, targets, guidance
      "stakeout.go_north": "{value} m nach Norden", "stakeout.go_south": "{value} m nach Süden",
      "stakeout.go_east": "{value} m nach Osten", "stakeout.go_west": "{value} m nach Westen",
      "stakeout.on_target": "Am Ziel",
      "stakeout.cardinal_n": "N", "stakeout.cardinal_ne": "NO", "stakeout.cardinal_e": "O", "stakeout.cardinal_se": "SO",
      "stakeout.cardinal_s": "S", "stakeout.cardinal_sw": "SW", "stakeout.cardinal_w": "W", "stakeout.cardinal_nw": "NW"
      ,"stakeout.default_point": "Punkt {n}", "stakeout.default_line": "Linie {n}",
      "import.unsupported_geometry": "Geometrie nicht unterstützt – nicht übernommen",
      "import.crs_not_wgs84": "Die Datei nutzt ein anderes Koordinatensystem als WGS84 (CRS84 oder EPSG:4326). Bitte mit WGS84-Länge und -Breite exportieren.",
      "import.coordinate_out_of_range": "Eine Koordinate liegt außerhalb des Bereichs – keine WGS84-Gradwerte (projiziert?)",
      "import.too_many_targets": "Zu viele Ziele oder Stützpunkte für ein Ziel-Set",
      "import.already_imported": "Dieses Ziel-Set ist bereits importiert",
      "import.storage_reserve": "Zu wenig freier Gerätespeicher für dieses Ziel-Set",
      "stakeout.block_invalid_json": "Die Datei ist kein gültiges JSON",
      "stakeout.block_not_geojson": "Die Datei ist kein GeoJSON (FeatureCollection, Feature oder Geometrie)",
      "stakeout.block_invalid_position": "Eine Position ist keine Liste endlicher Zahlen",
      "stakeout.block_too_many_features": "Die Datei hat mehr als 5000 Features",
      "stakeout.block_file_too_large": "Die Datei ist größer als 4 MiB",
      "stakeout.skip_unsupported_geometry": "nicht übernommen – Geometrie nicht unterstützt",
      "stakeout.skip_null_geometry": "nicht übernommen – keine Geometrie",
      "stakeout.skip_empty_coordinates": "nicht übernommen – zu wenige Koordinaten",
      "stakeout.hint_extra_position_elements": "Positionen mit mehr als drei Werten: die zusätzlichen Werte werden ignoriert",
      "stakeout.hint_props_trimmed": "Eigenschaft gekürzt oder ausgelassen:",
      "stakeout.hint_closed_line": "Geschlossene Linie: der letzte Stützpunkt wiederholt den ersten",
      "stakeout.hint_zero_segments": "{count} Segmente ohne Länge (wiederholte Stützpunkte)",
      "stakeout.hint_long_line": "{count} Stützpunkte – eine aufgezeichnete Strecke? Auswahlhilfe nutzen",
      "stakeout.hint_overlap": "Ziele weniger als 2 cm voneinander entfernt:",
      "stakeout.hint_extent": "Ausdehnung {north} m Nord–Süd × {east} m Ost–West",
      "stakeout.hint_rover_range": "Nächstes Ziel {nearest} m, fernstes {farthest} m von deiner Position",
      "stakeout.hint_axis_swap": "Alle Ziele sind mehr als 50 km entfernt, mit vertauschter Länge und Breite lägen sie nah – Achsenreihenfolge prüfen"
      ,"stakeout.status.open": "Offen"
      ,"stakeout.status.confirmed": "Bestätigt"
      ,"stakeout.status.skipped": "Übersprungen"
      ,"stakeout.status.unknown": "Unbekannt"
      ,"stakeout.calm.stakeout_busy": "Gerät ist gerade beschäftigt – gleich wieder"
      ,"stakeout.calm.stakeout_write_uncertain": "Absteckung bis zum Neustart des Geräts gesperrt"
      ,"stakeout.error.project_not_active": "Dieses Projekt ist nicht aktiv. Aktiviere es zuerst zum Abstecken."
      ,"stakeout.error.recording_active": "Eine Linie nimmt auf oder eine Messung läuft. Pausiere zuerst die Linie."
      ,"stakeout.error.project_changed": "Das Projekt hat sich inzwischen geändert. Wähle das Ziel erneut."
      ,"stakeout.error.target_not_found": "Dieses Ziel gibt es nicht mehr."
      ,"stakeout.error.set_not_found": "Dieses Ziel-Set gibt es nicht mehr."
      ,"stakeout.error.set_damaged": "Das Ziel-Set ist beschädigt. Entferne es und importiere es erneut."
      ,"stakeout.error.storage_busy": "Der Gerätespeicher ist gerade in Benutzung. Gleich erneut versuchen."
      ,"stakeout.error.stakeout_busy": "Das Gerät ist gerade beschäftigt. Gleich erneut versuchen."
      ,"stakeout.error.stakeout_write_uncertain": "Eine Absteckänderung wurde vielleicht nicht gespeichert. Absteckänderungen sind bis zum Neustart des Geräts gesperrt."
      ,"stakeout.error.survey_loading": "Die Vermessung wird noch geladen. Gleich erneut versuchen."
      ,"stakeout.error.stakeout_recovery_pending": "Die Ziel-Sets dieses Projekts warten auf einen Neustart des Geräts."
      ,"stakeout.error.confirmed_changed": "Der Fortschritt dieses Sets hat sich inzwischen geändert. Übersicht prüfen und erneut versuchen."
      ,"stakeout.error.too_many_sets": "Das Projekt hat bereits 4 Ziel-Sets."
      ,"stakeout.error.import_too_large": "Der Import ist zu groß für das Gerät."
      ,"stakeout.error.orphaned_set_conflict": "Ein Ziel-Set eines früheren Projekts mit derselben ID steht im Weg."
      ,"stakeout.error.import_committed_response_failed": "Das Ziel-Set wurde gespeichert, aber die Antwort brach ab."
      ,"stakeout.error.stakeout_write_failed": "Das Gerät konnte die Absteckdaten nicht schreiben."
      ,"stakeout.error.invalid_stakeout_settings": "Diese Absteckeinstellungen sind nicht gültig."
      ,"stakeout.error.part_not_found": "Diesen Teil gibt es nicht mehr."
      ,"stakeout.error.project_not_found": "Dieses Projekt gibt es nicht mehr."
      ,"stakeout.error.invalid_stakeout_import": "Das Gerät hat das Importpaket abgelehnt."
      ,"stakeout.error.stakeout_cleanup_pending": "Ein entferntes Projekt mit derselben ID wird noch aufgeräumt. Gerät neu starten."
      ,"stakeout.error.benchmark_active": "Auf dem Gerät läuft gerade ein Benchmark."
      ,"stakeout.loading": "Absteckung wird geladen"
      ,"stakeout.kicker_overview": "Absteckung"
      ,"stakeout.kicker_import": "Absteckung · Import"
      ,"stakeout.kicker_targets": "Absteckung · Ziele"
      ,"stakeout.kicker_target": "Absteckung · Führung"
      ,"stakeout.entry": "Abstecken"
      ,"stakeout.entry_text": "Zielpunkte importieren und im Gelände finden."
      ,"stakeout.not_active": "Dieses Projekt ist nicht aktiv. Die Zielführung braucht das aktive Projekt; Liste und Import gehen auch ohne."
      ,"stakeout.activate": "Zum Abstecken aktivieren"
      ,"stakeout.activated": "Projekt zum Abstecken aktiv"
      ,"stakeout.settings_title": "Absteckeinstellungen"
      ,"stakeout.settings_line": "Toleranz {tolerance} m · Feinradius {fine} m"
      ,"stakeout.settings_change": "Ändern"
      ,"stakeout.settings_save": "Speichern"
      ,"stakeout.settings_saved": "Absteckeinstellungen gespeichert"
      ,"stakeout.settings_tolerance": "Toleranz (m)"
      ,"stakeout.settings_fine": "Feinradius (m)"
      ,"stakeout.settings_tolerance_range": "Toleranz von 0,02 bis 5,0 m"
      ,"stakeout.settings_fine_range": "Feinradius von 1 bis 50 m"
      ,"stakeout.settings_pair": "Die Toleranz muss kleiner als der Feinradius sein"
      ,"stakeout.set_state_ok": "In Ordnung"
      ,"stakeout.set_state_damaged": "Beschädigt"
      ,"stakeout.set_state_incomplete": "Unvollständig"
      ,"stakeout.set_state_recovery_pending": "Wartet auf Neustart"
      ,"stakeout.set_targets": "{count} Ziele"
      ,"stakeout.delete": "Entfernen"
      ,"stakeout.delete_title": "Ziel-Set entfernen?"
      ,"stakeout.delete_text": "{count} Ziele vom Gerät entfernen:"
      ,"stakeout.delete_done": "Ziel-Set entfernt"
      ,"stakeout.delete_blocked_unknown": "Zum Entfernen wird der Fortschritt dieses Sets gebraucht, der gerade unbekannt ist."
      ,"stakeout.import": "GeoJSON importieren"
      ,"stakeout.import_blocked_sets": "Das Projekt hat bereits 4 Ziel-Sets. Entferne eines, um ein weiteres zu importieren."
      ,"stakeout.next_open": "Nächstes offenes Ziel"
      ,"stakeout.resume": "Weiter mit dem letzten Ziel"
      ,"stakeout.target_list": "Zielliste"
      ,"stakeout.back_to_project": "Zurück zum Projekt"
      ,"stakeout.all_done": "Kein offenes Ziel"
      ,"stakeout.retry_overview": "Erneut laden"
      ,"stakeout.calm_hint": "Die Absteckübersicht konnte noch nicht gelesen werden."
      ,"stakeout.counts": "{targets} Ziele · {open} offen · {confirmed} bestätigt · {skipped} übersprungen"
      ,"stakeout.counts_unknown": "{count} unbekannt"
      ,"stakeout.no_sets": "Noch kein Ziel-Set. Importiere eine GeoJSON-Datei."
      ,"stakeout.project_missing": "Projekt nicht gefunden"
      ,"stakeout.line_paused": "Linie pausiert"
      ,"stakeout.import_kind_line": "Linie"
      ,"stakeout.import_kind_multipoint": "Punktgruppe"
      ,"stakeout.import_kind_point": "Punkt"
      ,"stakeout.import_feature": "Feature {n}"
      ,"stakeout.import_use_part": "Diesen Teil übernehmen"
      ,"stakeout.import_part_count": "{count} Positionen"
      ,"stakeout.import_apply": "Anwenden"
      ,"stakeout.import_every": "Jeden n-ten Stützpunkt"
      ,"stakeout.import_from": "Von"
      ,"stakeout.import_to": "Bis"
      ,"stakeout.import_head": "{kb} kB · {features} Features"
      ,"stakeout.import_skipped_title": "Nicht übernommen"
      ,"stakeout.import_hints_title": "Hinweise"
      ,"stakeout.import_file": "GeoJSON-Datei (WGS84, bis 4 MiB)"
      ,"stakeout.import_blocked_none": "Wähle mindestens ein Ziel."
      ,"stakeout.import_blocked_many": "Höchstens 500 Ziele je Set."
      ,"stakeout.import_counter": "{count} von 500 Zielen gewählt"
      ,"stakeout.import_submit": "{count} Ziele übernehmen"
      ,"stakeout.import_too_large": "Das Paket hat auch mit 250 Stützpunkten je Linie {bytes} Bytes; das Gerät nimmt bis {limit}."
      ,"stakeout.pause_line": "Linie pausieren"
      ,"stakeout.import_go_overview": "Zur Übersicht"
      ,"stakeout.import_done": "Ziel-Set importiert"
      ,"stakeout.reverse": "Umgekehrt"
      ,"stakeout.back_to_overview": "Zurück zur Absteckübersicht"
      ,"stakeout.loading_set": "Ziele werden geladen …"
      ,"stakeout.retry_set": "Erneut versuchen"
      ,"stakeout.state_no_target": "Kein Ziel gewählt"
      ,"stakeout.state_no_position": "Keine Position"
      ,"stakeout.state_far": "Zu weit – Koordinaten prüfen"
      ,"stakeout.state_coarse": "Zum Ziel gehen"
      ,"stakeout.state_fine": "Nah – Feinführung"
      ,"stakeout.state_settling": "Ruhig halten {have}/{need}"
      ,"stakeout.state_ready": "Bereit"
      ,"stakeout.state_unknown": "Unbekannter Zustand"
      ,"stakeout.state_stale": "Daten veraltet – warten"
      ,"stakeout.reason.awaiting_fix": "Warte auf eine verwendbare Position"
      ,"stakeout.reason.gst_pending": "Warte auf die Genauigkeit dieser Epoche"
      ,"stakeout.reason.outside_tolerance": "Außerhalb der Toleranz"
      ,"stakeout.reason.gate_rejected": "Das Messgate lehnt diese Position ab"
      ,"stakeout.reason.mean_outside_tolerance": "Der Mittelwert des Fensters liegt außerhalb der Toleranz"
      ,"stakeout.reason.horizontal_spread_too_high": "Die Positionen des Fensters streuen zu stark"
      ,"stakeout.reason.config_changed": "Die Einstellungen haben sich geändert – das Fenster beginnt neu"
      ,"stakeout.reason.session_error": "Die Absteck-Sitzung hatte einen Fehler"
      ,"stakeout.reason.recording_active": "Eine Linie nimmt auf"
      ,"stakeout.reason.fix_stale": "Die Position ist veraltet"
      ,"stakeout.reason.corrections_missing": "Korrekturen fehlen"
      ,"stakeout.reason.write_uncertain": "Absteckänderungen sind bis zum Neustart des Geräts gesperrt"
      ,"stakeout.reason_unknown": "Das Gerät nennt einen Grund, den diese Seite nicht kennt"
      ,"stakeout.quality": "Datenqualität"
      ,"stakeout.quality_fix": "Fix"
      ,"stakeout.quality_sigma": "Genauigkeit"
      ,"stakeout.quality_correction": "Korrekturen"
      ,"stakeout.quality_age": "Fix-Alter"
      ,"stakeout.quality_link": "Verbindung"
      ,"stakeout.quality_ok": "ok"
      ,"stakeout.quality_corrections_missing": "Korrekturen fehlen"
      ,"stakeout.quality_corrections_old": "Korrekturen alt"
      ,"stakeout.quality_delayed": "verzögert"
      ,"stakeout.unexpected_data": "Unerwartete Gerätedaten"
      ,"stakeout.no_data_since": "Keine Daten seit {seconds} s"
      ,"stakeout.stale_since": "Position veraltet seit {seconds} s"
      ,"stakeout.dial": "Zielscheibe, Norden oben"
      ,"stakeout.motion_direction": "Bewegungsrichtung"
      ,"stakeout.next_target": "Nächstes Ziel"
      ,"stakeout.all_done_short": "Alle erledigt"
      ,"stakeout.target_missing": "Dieses Ziel ist in keinem Ziel-Set des Projekts."
      ,"stakeout.reselect": "Wieder wählen"
      ,"stakeout.target_lost": "Dieses Ziel ist am Gerät nicht mehr gewählt."
      ,"stakeout.follow": "Folgen"
      ,"stakeout.zoom_out": "Verkleinern"
      ,"stakeout.zoom_in": "Vergrößern"
      ,"stakeout.mode_search": "Suche"
      ,"stakeout.mode_fine": "Fein"
      ,"stakeout.remaining": "noch {value} m"
      ,"stakeout.tolerance_window": "Toleranz {tolerance} m, Qualitätsfenster {have}/{need}"
      // Stakeout WP4: actions
      ,"stakeout.action.confirm": "Bestätigen"
      ,"stakeout.action.found": "Vor Ort gefunden"
      ,"stakeout.action.skip": "Überspringen"
      ,"stakeout.action.reopen": "Wieder öffnen"
      ,"stakeout.found_hint": "Speichert einen Absteckpunkt an der aktuellen Position ohne Qualitätsfenster; seine Qualität wird wie gemessen vermerkt."
      ,"stakeout.note": "Notiz (optional)"
      ,"stakeout.saving": "Wird gespeichert…"
      ,"stakeout.saved": "Gespeichert"
      ,"stakeout.unconfirmed": "Unbestätigt – Liste prüfen"
      ,"stakeout.changed": "Das Ziel wurde inzwischen geändert"
      ,"stakeout.error.target_changed": "Das Ziel wurde inzwischen geändert. Seinen Stand in der Liste prüfen."
      ,"stakeout.error.target_not_open": "Dieses Ziel ist nicht mehr offen."
      ,"stakeout.error.target_open": "Dieses Ziel ist bereits offen."
      ,"stakeout.error.not_ready": "Noch nicht bereit zum Bestätigen."
      ,"stakeout.error.measurement_active": "Eine Messung läuft. Bis zu ihrem Ende warten."
      ,"stakeout.error.quality_rejected": "Die Positionsqualität wurde abgelehnt."
      ,"stakeout.error.point_limit_reached": "Das Projekt enthält bereits die höchste Punktzahl."
      ,"stakeout.error.no_position": "Keine aktuelle Position. Auf einen frischen Fix warten."
      ,"stakeout.error.storage_reserve": "Zu wenig freier Gerätespeicher zum Speichern."
      ,"stakeout.error.tracking_storage_failed": "Das Gerät konnte den Punkt nicht speichern. Das Ziel bleibt offen."
      ,"stakeout.error.invalid_request": "Das Gerät hat die Anfrage abgelehnt."
      ,"stakeout.error.undo_candidate_changed": "Der letzte Punkt hat sich inzwischen geändert. Projekt neu laden."
      ,"asset.stakeout": "Absteckkontrolle"
      ,"stakeout.progress_failed": "Der Absteckfortschritt konnte nicht geladen werden. Aktionen warten, bis er geladen ist."
      ,"stakeout.export": "Absteckung exportieren"
      ,"stakeout.export_hint": "Die Datei zeigt den Stand bei Exportbeginn."
      ,"stakeout.export_geojson": "GeoJSON"
      ,"stakeout.export_geojson_text": "Ziele mit Status und Absteckpunkten; wieder importierbar"
      ,"stakeout.export_csv": "CSV"
      ,"stakeout.export_csv_text": "Eine Zeile je Ziel"
      ,"archive.stakeout_data": "Absteck-Daten vorhanden"
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
    if (theme === "auto") root.document.documentElement.removeAttribute("data-theme");
    else root.document.documentElement.setAttribute("data-theme", theme);
    root.document.querySelectorAll("[data-theme-toggle]").forEach(function (button) {
      button.setAttribute("aria-pressed", String(theme === "high-contrast"));
    });
    root.document.querySelectorAll('meta[name="theme-color"]').forEach(function (meta) {
      const dark = /dark/.test(meta.getAttribute("media") || "");
      const colors = THEME_COLORS[theme === "auto" ? (dark ? "dark" : "light") : theme];
      meta.setAttribute("content", colors[meta.getAttribute("data-theme-surface") || "surface"]);
    });
  }
  // Applies a changed theme and announces it; a repeated value changes nothing and stays silent.
  function commitTheme(next, persist) {
    if (next === theme) return;
    theme = next;
    if (persist) { try { root.localStorage.setItem(THEME_KEY, theme); } catch (_) {} }
    applyTheme();
    if (root.document && root.CustomEvent) root.document.dispatchEvent(new root.CustomEvent("rtk-theme-change", {detail:{theme:theme}}));
  }
  function setTheme(name) {
    if (THEMES.indexOf(name) < 0) return false;
    commitTheme(name, true);
    return true;
  }
  function toggleTheme() { setTheme(theme === "high-contrast" ? "auto" : "high-contrast"); }
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
  const v2de={"RTCM stream":"RTCM-Strom","Correction data":"Korrekturdaten","Access":"Zugänge","Connections and keys":"Verbindungen und Schlüssel","Log":"Protokoll","Device events":"Geräteereignisse","Module & diagnostics":"Modul & Diagnose","Receiver console":"Empfängerkonsole","Configuration":"Konfiguration","Networks and NTRIP":"Netzwerke und NTRIP","Diagnostics":"Diagnose","Support data":"Supportdaten","Label & QR codes":"Etikett & QR-Codes","Direct access":"Direktzugang","Settings":"Einstellungen","Display and measurement":"Anzeige und Messung","Checking …":"Prüfe …","Waiting for receiver state":"Warte auf Empfängerstatus","Can I measure?":"Darf ich messen?","Project":"Projekt","Measurement":"Messung","Choose project":"Projekt wählen","Start line":"Linie starten","Object point":"Objektpunkt","Add line point":"Linienpunkt setzen","Resume":"Fortsetzen","Pause":"Pausieren","Finish line":"Linie abschließen","Start a new line":"Neue Linie starten","Line name":"Linienname","Automatic point distance (m)":"Automatischer Punktabstand (m)","Material":"Material","Diameter":"Durchmesser","Depth":"Tiefe","Optional":"Optional","Start recording":"Aufnahme starten","Set object point":"Objektpunkt setzen","Type":"Typ","Name":"Name","Note":"Notiz","More":"Mehr","Associate with active line":"Aktiver Linie zuordnen","Save point":"Punkt speichern","Available":"frei","Keep the antenna still":"Antenne ruhig halten","Starting measurement …":"Messung startet …","Horizontal mean":"Mittelwert H","Vertical mean":"Mittelwert V","Elevation mean":"Mittelwert Höhe","New project":"Neues Projekt","Project name":"Projektname","Create project":"Projekt anlegen","No projects yet":"Noch keine Projekte","Create the first project before starting a survey.":"Vor der ersten Aufnahme ein Projekt anlegen.","Complete backup":"Vollständige Sicherung","Data management":"Datenverwaltung","Back up, restore or optimize all survey projects.":"Alle Vermessungsprojekte sichern, wiederherstellen oder optimieren.","Restore backup":"Sicherung wiederherstellen","Optimize storage":"Speicher optimieren","Rename":"Umbenennen","Delete":"Löschen","Share GeoJSON":"GeoJSON teilen","Download point CSV":"Punkt-CSV laden","No surveyed geometry yet.":"Noch keine Geometrie aufgenommen.","Second measurement missing":"Zweite Messung fehlt","Required fix":"Erforderlicher Fix","Averaging":"Mittelung","All limits":"Alle Grenzwerte","Sign out":"Abmelden","Device":"Gerät","Version":"Version","Password":"Passwort","Username":"Benutzername","Field network":"Feldnetzwerk","Correction source":"Korrekturquelle","Scan networks":"Netzwerke suchen","Download configuration":"Konfiguration laden","Restore configuration":"Konfiguration wiederherstellen","Save and verify configuration":"Konfiguration speichern und prüfen","Restart device":"Gerät neu starten","Load now":"Jetzt laden","Share":"Teilen","Search log — e.g. WIFI":"Protokoll durchsuchen — z. B. WIFI","All":"Alle","Error":"Fehler","Connected clients":"Verbundene Clients","Recording":"Aufnahme läuft","Ready to measure":"Bereit zum Messen","Measurement blocked":"Messung gesperrt","Stable for":"Stabil seit","Required":"Erforderlich","points":"Punkte","lines":"Linien","No project selected":"Kein Projekt ausgewählt","Updated":"Aktualisiert"};
  Object.assign(v2de,{"Sign in · RTK Field App":"Anmelden · RTK Feld-App","Ready when the receiver is.":"Bereit, wenn der Empfänger es ist.","Sign in locally with the device code printed on the receiver label.":"Lokal mit dem Geräte-Code vom Empfängeretikett anmelden.","SECURE DEVICE ACCESS":"SICHERER GERÄTEZUGANG","Sign in":"Anmelden","Device code":"Geräte-Code","Show":"Zeigen","Hide":"Verbergen","Open Field App":"Feld-App öffnen","The code never leaves this device.":"Der Code verlässt dieses Gerät nicht.","not accepted":"nicht erreicht","Basic setup page (fallback)":"Einfache Einrichtungsseite (Ersatz)"});
  Object.assign(v2de,{"Project map":"Projektkarte","Survey data":"Vermessungsdaten","Back to projects":"Zurück zu Projekte","Position":"Position","Last point":"Letzter Punkt","Storage":"Speicher","line points":"Linienpunkte","object points":"Objektpunkte","For a before/after stability check, record the same known point twice as type control with exactly the same name.":"Für eine Vorher-/Nachher-Stabilitätsprüfung denselben bekannten Punkt zweimal mit Typ Kontrolle und exakt gleichem Namen aufnehmen.","Diameter (mm)":"Durchmesser (mm)","Burial depth (m)":"Verlegetiefe (m)","Recorded vertices":"Aufgenommene Linienpunkte","Accepted quality":"Akzeptierte Qualität","Load more line points":"Weitere Linienpunkte laden","Network":"Netzwerk","Not connected":"Nicht verbunden","Chip":"Chip","File system":"Dateisystem","Clients":"Clients","Authentication errors":"Authentifizierungsfehler","documented":"dokumentiert","deviating":"abweichend","entries":"Einträge","Diagnostics & label":"Diagnose & Etikett","Support package and identification":"Supportpaket und Identifikation","Wi-Fi and NTRIP":"WLAN und NTRIP","Language · outdoor mode":"Sprache · Außenmodus","Uptime":"Laufzeit","Start":"Start","Restart only if streams or the receiver no longer respond. Survey data and configuration are retained.":"Nur neu starten, wenn Streams oder Empfänger nicht mehr reagieren. Vermessungsdaten und Konfiguration bleiben erhalten.","Keep screen awake on":"Bildschirm wach halten an","Keep screen awake off":"Bildschirm wach halten aus","This browser does not support keeping the screen awake.":"Dieser Browser unterstützt das Wachhalten des Bildschirms nicht.","Address":"Adresse","Open label & QR codes":"Etikett & QR-Codes öffnen","Download support package":"Supportpaket herunterladen","Configuration applied and connection restored.":"Konfiguration übernommen und Verbindung wiederhergestellt.","Configuration failed; the previous working settings were restored.":"Konfiguration fehlgeschlagen; die vorherigen funktionierenden Einstellungen wurden wiederhergestellt.","Receiver is restarting — waiting for the connection …":"Gerät startet neu — warte auf die Verbindung …","The receiver is still reconnecting. Reopen this page after joining its network.":"Das Gerät verbindet sich noch. Diese Seite nach dem Beitritt zum Netzwerk erneut öffnen.","Fallback networks":"Ausweichnetzwerke","Existing passwords are retained when only the SSID is entered.":"Bestehende Passwörter bleiben erhalten, wenn nur die SSID eingetragen wird.","Technical connection details":"Technische Verbindungsdetails","Pairing protection":"Kopplungsschutz","Use the stream key only with trusted field clients.":"Den Stream-Schlüssel nur mit vertrauenswürdigen Feld-Clients verwenden."});
  Object.assign(v2de,{"+ New project":"+ Neues Projekt","ACTIVE PROJECT EXPORT":"EXPORT DES AKTIVEN PROJEKTS","All data interfaces remain local to the device.":"Alle Datenschnittstellen bleiben lokal auf dem Gerät.","BACKUP & RESTORE":"SICHERN & WIEDERHERSTELLEN","CONNECTED CLIENTS":"VERBUNDENE CLIENTS","CONNECTIONS":"VERBINDUNGEN","CORRECTION STREAM":"KORREKTURSTROM","Changes are validated before the receiver restarts.":"Änderungen werden vor dem Neustart des Empfängers geprüft.","Changing Wi-Fi may temporarily disconnect this browser.":"Beim Wechsel des WLANs kann die Verbindung dieses Browsers kurz unterbrochen werden.","DEVICE EVENTS":"GERÄTEEREIGNISSE","DIRECT ACCESS":"DIREKTZUGANG","Device hotspot":"Geräte-Hotspot","Encrypted pairing PIN":"Verschlüsselte Kopplungs-PIN","FIELD CONDITIONS":"FELDBEDINGUNGEN","FIELD RECEIVER":"FELDEMPFÄNGER","Filter the current and persistent device history.":"Aktuelle und gespeicherte Geräteereignisse filtern.","GNSS command":"GNSS-Befehl","How can clients reach this receiver?":"Wie erreichen Clients diesen Empfänger?","LANGUAGE":"SPRACHE","MEASUREMENT":"MESSUNG","MEASUREMENT GATE":"MESSFREIGABE","MESSAGE FAMILIES":"NACHRICHTENFAMILIEN","NETWORKS & CORRECTIONS":"NETZWERKE & KORREKTUREN","NTRIP CASTER":"NTRIP-CASTER","One network per line":"Ein Netzwerk pro Zeile","Only new entries are transferred while this page is open.":"Solange diese Seite geöffnet ist, werden nur neue Einträge übertragen.","PROJECT WORKSPACE":"PROJEKTBEREICH","Point CSV":"Punkt-CSV","RECEIVER CONSOLE":"EMPFÄNGERKONSOLE","SESSION":"SITZUNG","SSID or SSID:password, one network per line":"SSID oder SSID:Passwort, ein Netzwerk pro Zeile","SUPPORT SNAPSHOT":"SUPPORT-SNAPSHOT","SURVEY LIBRARY":"VERMESSUNGSPROJEKTE","Send checks to the Quectel receiver without stopping the field application.":"Prüfbefehle an den Quectel-Empfänger senden, ohne die Feldanwendung anzuhalten.","Show and copy stream key":"Stream-Schlüssel anzeigen und kopieren","Survey map":"Vermessungskarte","This read-only package contains receiver, network and runtime facts.":"Dieses schreibgeschützte Paket enthält Empfänger-, Netzwerk- und Laufzeitdaten.","Use these codes only on the physical receiver or in a trusted field team.":"Diese Codes nur direkt am Empfänger oder in einem vertrauenswürdigen Feldteam verwenden.","WI-FI CONNECTION":"WLAN-VERBINDUNG","What happened?":"Was ist passiert?","e.g. Water line 01":"z. B. Wasserleitung 01"});
  Object.assign(v2de,{"Scanning for Wi-Fi networks …":"WLAN-Netzwerke werden gesucht …","Network selected. Save to apply it.":"Netzwerk ausgewählt. Zum Übernehmen speichern.","Wi-Fi scan complete.":"WLAN-Suche abgeschlossen.","Visible Wi-Fi networks":"Sichtbare WLAN-Netzwerke","Use network":"Netzwerk verwenden","No Wi-Fi networks were found.":"Keine WLAN-Netzwerke gefunden.","Pause the active recording before scanning for Wi-Fi networks.":"Die aktive Aufnahme vor der WLAN-Suche pausieren."});
  Object.assign(v2de,{"NTRIP client":"NTRIP-Client","Enabled":"Aktiviert","Disabled":"Deaktiviert","Primary caster":"Primärer Caster","Fallback caster (optional)":"Alternativer Caster (optional)","The fallback is used automatically when configured and the primary caster is unavailable.":"Der alternative Caster wird automatisch verwendet, wenn er konfiguriert und der primäre Caster nicht erreichbar ist."});
  Object.assign(v2de,{"project":"Projekt","projects":"Projekte","line":"Linie","lines":"Linien","point":"Punkt","object point":"Objektpunkt","message":"Nachricht","messages":"Nachrichten","entry":"Eintrag","satellite":"Satellit","satellites":"Satelliten","Stream is live":"Strom steht","Stream interrupted":"Strom unterbrochen","Last message":"Letzte Nachricht","No recent RTCM message reached the receiver.":"Keine aktuelle RTCM-Nachricht hat den Empfänger erreicht.","No MSM family":"Keine MSM-Familie","GGA sent":"GGA gesendet","Corrections live":"Korrekturen aktiv","Corrections interrupted":"Korrekturen unterbrochen","Administrator session":"Administrator-Sitzung","DEVICE ACCESS":"GERÄTEZUGANG","BLE pairing PIN":"BLE-Kopplungs-PIN","Ports":"Ports","A password is saved.":"Ein Passwort ist gespeichert.","No password is saved.":"Kein Passwort ist gespeichert.","Mountpoint":"Mountpunkt","Vertical":"Vertikal","System":"System","Ready":"Bereit","Attention":"Achtung","Safe mode":"Sicherheitsmodus","GNSS":"GNSS","Corrections":"Korrekturen","Memory":"Speicher","free RAM":"RAM frei","Firmware":"Firmware","Wi-Fi reconnects":"WLAN-Neuverbindungen","NTRIP retries":"NTRIP-Wiederholungen","Queue overflows":"Queue-Überläufe","HTTP errors":"HTTP-Fehler","This read-only overview contains receiver, network and runtime facts.":"Diese schreibgeschützte Übersicht enthält Empfänger-, Netzwerk- und Laufzeitdaten.","Refresh":"Aktualisieren","Open support package":"Supportpaket öffnen","Task restarts":"Task-Neustarts","Try again":"Erneut versuchen"});
  Object.assign(v2de,{"Cancel measurement":"Messung abbrechen","Measurement cancelled.":"Messung abgebrochen.","Cancelling measurement …":"Messung wird abgebrochen …","Waiting for a valid GNSS sample …":"Warte auf eine gültige GNSS-Probe …","Measurement":"Messung","of":"von","Line recording started":"Linienaufnahme gestartet","Line point saved":"Linienpunkt gespeichert","Line finished":"Linie abgeschlossen","Object point saved":"Objektpunkt gespeichert","Recording paused":"Aufnahme pausiert","Measurement actions unlock when all field conditions pass.":"Messaktionen werden freigegeben, sobald alle Feldbedingungen erfüllt sind.","Resume the line before recording another point.":"Die Linie vor einer weiteren Punktaufnahme fortsetzen.","Wait for RTK FIXED or check the correction stream.":"Auf RTK FIXED warten oder den Korrekturstrom prüfen.","Keep the antenna still and wait for horizontal accuracy.":"Antenne ruhig halten und auf horizontale Genauigkeit warten.","Keep the antenna still and wait for vertical accuracy.":"Antenne ruhig halten und auf vertikale Genauigkeit warten.","Move to a clearer view of the sky.":"Zu einem Standort mit freierer Sicht zum Himmel wechseln.","Wait for all field conditions.":"Auf alle Feldbedingungen warten.","not ready for measurement":"nicht messbereit","Active for measurement":"Aktiv zum Messen","Active project for field recording":"Aktives Projekt für die Feldaufnahme","Activate for measurement":"Zum Messen aktivieren","Project activated for measurement":"Projekt zum Messen aktiviert","Project not found":"Projekt nicht gefunden","It may have been deleted or restored from another backup.":"Es wurde eventuell gelöscht oder aus einer anderen Sicherung wiederhergestellt.","Back to projects":"Zurück zu Projekten","All object points":"Alle Objektpunkte","Unnamed point":"Unbenannter Punkt","Horizontal accuracy":"Horizontale Genauigkeit","Chainage":"Stationierung","Survey lines":"Vermessungslinien","Map preview is simplified because this project contains more than 500 features.":"Die Kartenvorschau ist vereinfacht, weil dieses Projekt mehr als 500 Objekte enthält.","Survey line":"Vermessungslinie","Receiver":"Empfänger","Fit project":"Projekt einpassen","Center position":"Position zentrieren","Dismiss":"Schließen","Discard unsaved changes?":"Ungespeicherte Änderungen verwerfen?","The Wi-Fi and NTRIP values on this page have not been saved.":"Die WLAN- und NTRIP-Werte auf dieser Seite wurden noch nicht gespeichert.","Discard changes":"Änderungen verwerfen","Connection identifiers are included in the downloadable support package.":"Verbindungskennungen sind im herunterladbaren Supportpaket enthalten.","Device restarting — waiting for it to return":"Gerät startet neu — warte auf die Wiederverbindung","Device is still restarting. Reload when its network returns.":"Das Gerät startet noch. Neu laden, sobald sein Netzwerk wieder erreichbar ist."});
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
  // Literals that used to be translated only by the removed reverse dictionary lookup; the German values are the ones it produced.
  Object.assign(v2de,{"unknown":"unbekannt","label":"Etikett","Vertical accuracy":"Vertikale Genauigkeit","Satellites":"Satelliten","Cancel":"Abbrechen","Optimize recording storage":"Aufzeichnungsspeicher optimieren","Download":"Herunterladen","e.g. Field road east":"z. B. Feldweg Ost","Rename project":"Projekt umbenennen","Delete project":"Projekt löschen","Device functions":"Gerätefunktionen","Stream key":"Streamschlüssel","Rotate stream key":"Stream-Schlüssel erneuern","Send":"Senden","Restart is being prepared …":"Neustart wird vorbereitet …","Device is offline — waiting for it to return …":"Gerät ist offline — warte auf die Wiederverbindung …","Restart successful — connection restored.":"Neustart erfolgreich — Verbindung wiederhergestellt.","The device is still restarting. Check Wi-Fi and reload when its network returns.":"Das Gerät startet noch. WLAN prüfen und neu laden, sobald sein Netzwerk wieder erreichbar ist.","Direct web portal":"Webportal direkt","ground":"Boden","Search log":"Log durchsuchen","GNSS module":"GNSS-Modul","Encrypted pairing":"Verschlüsselte Kopplung","NMEA over TCP":"NMEA über TCP","Web portal":"Webportal"});
  Object.assign(v2de,{"Check the correction link or wait for fresh correction epochs.":"Korrekturverbindung prüfen oder auf aktuelle Korrekturepochen warten.","Link latency":"Link-Latenz","Corrections stale":"Korrekturen veraltet","stale":"veraltet"});
  Object.assign(v2de,{"Quality accepted":"Qualität akzeptiert","Quality rejected":"Qualität abgelehnt","Horizontal 1σ":"Horizontal 1σ","Vertical 1σ":"Vertikal 1σ","Fix":"Fix","Samples":"Messwerte","Measurement duration":"Messdauer","Reference station":"Referenzstation","GST source":"GST-Quelle"});
  Object.assign(v2de,{"NMEA TCP SECURITY":"NMEA-TCP-SICHERHEIT","Authentication before streaming":"Authentifizierung vor der Übertragung","TCP authentication on":"TCP-Authentifizierung an","TCP authentication off":"TCP-Authentifizierung aus","Clients must send AUTH <stream_token> before the first NMEA byte.":"Clients müssen vor dem ersten NMEA-Byte AUTH <stream_token> senden.","NMEA starts immediately after connecting. No stream key is required.":"NMEA startet direkt nach dem Verbinden. Ein Stream-Schlüssel ist nicht erforderlich.","Disable TCP authentication?":"TCP-Authentifizierung deaktivieren?","Every client on the local network will receive the NMEA stream without a key. Use this only on a trusted isolated network.":"Jeder Client im lokalen Netzwerk erhält den NMEA-Strom ohne Schlüssel. Nur in einem vertrauenswürdigen isolierten Netzwerk verwenden.","Disable authentication":"Authentifizierung deaktivieren","TCP authentication enabled":"TCP-Authentifizierung aktiviert","TCP authentication disabled":"TCP-Authentifizierung deaktiviert","TCP authentication is disabled. Any client on this network can read the position stream.":"TCP-Authentifizierung ist deaktiviert. Jeder Client in diesem Netzwerk kann den Positionsstrom lesen."});
  Object.assign(v2de,{"These limits apply immediately to status, line points and object points. Restore defaults at any time from Settings.":"Diese Grenzwerte gelten sofort für Status, Linienpunkte und Objektpunkte. Die Standardwerte können jederzeit in den Einstellungen wiederhergestellt werden.","Quality OK sample window":"Stichprobenfenster Qualität OK","Default: 5 consecutive accepted samples.":"Standard: 5 aufeinanderfolgende akzeptierte Messwerte."});
  Object.assign(v2de,{"Recording mode":"Aufzeichnungsmodus","Survey — accepted precision points only":"Vermessung — nur akzeptierte Präzisionspunkte","Route — preserve every valid position":"Route — jede vollständige Position sichern","Wi-Fi error: ":"WLAN-Fehler: ","Hotspot not found":"Hotspot nicht gefunden","Wi-Fi authentication failed":"WLAN-Anmeldung fehlgeschlagen","Wi-Fi association failed":"WLAN-Verbindung fehlgeschlagen","Wi-Fi connection timed out":"Zeitüberschreitung der WLAN-Verbindung","Wi-Fi connection lost":"WLAN-Verbindung verloren"});
  Object.assign(v2de,{"FIELD TEST": "FELDTEST", "Field diagnostics": "Felddiagnose", "Start field diagnostics": "Felddiagnose starten", "Stop field diagnostics": "Felddiagnose stoppen", "Field marker": "Feldmarkierung", "Marker": "Markierung", "Stationary": "Stillstand", "Walking": "Gehen", "Hotspot off": "Hotspot aus", "Hotspot on": "Hotspot an", "Power off": "Strom aus", "Add marker": "Markierung setzen", "Download field log": "Feldprotokoll herunterladen", "Stop field diagnostics before downloading.": "Vor dem Herunterladen die Felddiagnose stoppen.", "Records fix, accuracy, satellite signals and connection events on the receiver, including without Wi-Fi. Resumes after a restart until stopped.": "Zeichnet Fix, Genauigkeit, Satellitensignale und Verbindungsereignisse im Empfänger auf, auch ohne WLAN. Läuft nach einem Neustart weiter, bis sie gestoppt wird.", "Oldest entries are overwritten when the bounded log fills. Pending entries can be lost on power interruption. Stop and download soon after the test.": "Ist der begrenzte Protokollspeicher voll, werden die ältesten Einträge überschrieben. Noch ungesicherte Einträge können bei Stromausfall verloren gehen. Nach dem Test zeitnah stoppen und herunterladen.", "Field diagnostics: storage error": "Felddiagnose: Speicherfehler", "Field diagnostics running": "Felddiagnose läuft", "Field diagnostics stopped": "Felddiagnose gestoppt", "samples since restart": "Messintervalle seit Neustart", "Dropped entries": "Verworfene Einträge", "Field marker saved": "Feldmarkierung gespeichert", "DIAG error · ": "DIAG Fehler · ", "DIAG on · ": "DIAG an · "});
  dictionaries.en["diag.field_link"]="Open field diagnostics";
  dictionaries.de["diag.field_link"]="Felddiagnose öffnen";
  Object.assign(v2de,{"Loading project map":"Projektkarte wird geladen","Loading live status":"Live-Status wird geladen","Live status unavailable":"Live-Status nicht verfügbar","Request timed out. Please try again.":"Zeitüberschreitung. Bitte erneut versuchen."});
  Object.assign(v2de,{
  "There are no projects with recorded points to archive.": "Es gibt keine Projekte mit aufgezeichneten Punkten zum Archivieren.",
  "Recording capacity": "Aufzeichnungskapazität",
  "Point limit usage": "Auslastung des Punktelimits",
  "Loading recording capacity": "Aufzeichnungskapazität wird geladen",
  "Recording capacity unavailable": "Aufzeichnungskapazität nicht verfügbar",
  "Remaining time cannot currently be estimated": "Restzeit derzeit nicht abschätzbar",
  "All non-archived projects combined": "Alle nicht archivierten Projekte zusammen",
  "Manage projects": "Projekte verwalten",
  "Choose project to archive": "Projekt zum Archivieren auswählen",
  "{count} points available": "Noch {count} Punkte verfügbar",
  "Only {count} points available": "Nur noch {count} Punkte verfügbar",
  "About {minutes} min until the point limit at the current recording rate": "Bei aktueller Aufzeichnung etwa {minutes} Min. bis zum Punktelimit",
  "Point limit reached": "Punktelimit erreicht",
  "Recording paused — point limit reached": "Aufzeichnung pausiert – Punktelimit erreicht",
  "Recording capacity is running low": "Aufzeichnungskapazität wird knapp",
  "Archive a project soon": "Bald ein Projekt archivieren",
  "Your recorded points are saved. Archive another project to continue this line.": "Deine bisherigen Punkte sind gespeichert. Archiviere ein anderes Projekt, um diese Linie fortzusetzen.",
  "Archive a project before recording more points.": "Archiviere ein Projekt, bevor du weitere Punkte aufzeichnest.",
  "Point limit reached. Archive another project to continue this line.": "Punktelimit erreicht. Archiviere ein anderes Projekt, um diese Linie fortzusetzen.",
  "Device storage reserve reached": "Speicherreserve des Geräts erreicht",
  "Device storage reserve reached. Export and manage stored data before continuing.": "Speicherreserve des Geräts erreicht. Vor dem Fortsetzen gespeicherte Daten exportieren und verwalten.",
  "Capacity is available again. Resume the recording when ready.": "Es sind wieder Punkte verfügbar. Du kannst die Aufzeichnung jetzt fortsetzen.",
  "Free device storage": "Freier Gerätespeicher",
  "No other project can be archived. Back up this project, finish its active line, then archive it and start a new project.": "Es kann kein anderes Projekt archiviert werden. Sichere dieses Projekt, schließe seine aktive Linie ab und archiviere es anschließend. Danach kannst du ein neues Projekt starten.",
  "Open projects": "Projekte öffnen",
  "Archiving preserves a verified copy on the device and frees its points from the active limit. Download an additional backup from the project page.": "Beim Archivieren bleibt eine geprüfte Kopie auf dem Gerät erhalten. Die Punkte zählen anschließend nicht mehr zum aktiven Limit. Eine zusätzliche Sicherung kannst du auf der Projektseite herunterladen.",
  "{name} · free {count} points": "{name} · {count} Punkte freigeben",
  "Continue": "Weiter",
  "Archive": "Archivieren",
  "Archive project": "Projekt archivieren",
  "Create and verify an archive of “{name}”, then free {count} points? An existing pause remains in place; recording never resumes automatically.": "Ein Archiv von „{name}“ erstellen und prüfen, danach {count} Punkte freigeben? Eine pausierte Linie bleibt pausiert; die Aufzeichnung wird nicht automatisch fortgesetzt.",
  "Project “{name}” archived": "Projekt „{name}“ archiviert",
  "Finish the active line before archiving this project.": "Schließe die aktive Linie ab, bevor du dieses Projekt archivierst.",
  "Finish the active line before deleting this project.": "Schließe die aktive Linie ab, bevor du dieses Projekt löschst."
});
  // Remaining field-app texts (#17): status messages, backup parser errors, attributes and composed sentences.
  Object.assign(v2de,{
  "Connecting": "Verbinde",
  "Verify": "Prüfen",
  "Reactivate": "Reaktivieren",
  "Optimize": "Optimieren",
  "Save": "Speichern",
  "Status": "Status",
  "Health": "Zustand",
  "Info": "Info",
  "Warn": "Warnung",
  "Debug": "Debug",
  "archive": "Archiv",
  "archives": "Archive",
  "line point": "Linienpunkt",
  "condition already met": "Bedingung bereits erfüllt",
  "Checking device code …": "Gerätecode wird geprüft …",
  "Sign-in failed": "Anmeldung fehlgeschlagen",
  "Request cancelled": "Anfrage abgebrochen",
  "Session expired": "Sitzung abgelaufen",
  "Invalid value": "Ungültiger Wert",
  "Action failed": "Aktion fehlgeschlagen",
  "Device unreachable": "Gerät nicht erreichbar",
  "Offline — waiting": "Offline — warte",
  "Connection restored": "Verbindung wiederhergestellt",
  "Loading projects": "Projekte werden geladen",
  "Loading device": "Gerät wird geladen",
  "Loading RTCM stream": "RTCM-Strom wird geladen",
  "Loading access information": "Zugangsdaten werden geladen",
  "Loading module console": "Modulkonsole wird geladen",
  "Loading diagnostics": "Diagnose wird geladen",
  "Loading label": "Etikett wird geladen",
  "Loading settings": "Einstellungen werden geladen",
  "Loading configuration": "Konfiguration wird geladen",
  "Loading device log": "Geräteprotokoll wird geladen",
  "Projects updated": "Projekte aktualisiert",
  "Map updated": "Karte aktualisiert",
  "Map update failed": "Kartenaktualisierung fehlgeschlagen",
  "Device updated": "Gerät aktualisiert",
  "Access updated": "Zugänge aktualisiert",
  "Module updated": "Modul aktualisiert",
  "Diagnostics updated": "Diagnose aktualisiert",
  "Label ready": "Etikett bereit",
  "Settings ready": "Einstellungen bereit",
  "Maintenance ready": "Wartung bereit",
  "Configuration loaded": "Konfiguration geladen",
  "Log updated": "Protokoll aktualisiert",
  "Log update failed": "Protokollaktualisierung fehlgeschlagen",
  "Deleting project …": "Projekt wird gelöscht …",
  "Project was not deleted": "Projekt wurde nicht gelöscht",
  "Restoring survey data …": "Vermessungsdaten werden wiederhergestellt …",
  "Validating and staging configuration …": "Konfiguration wird geprüft und vorbereitet …",
  "Configuration staged. Applying and reconnecting …": "Konfiguration übernommen. Wird angewendet, Verbindung wird neu aufgebaut …",
  "Configuration saved and in effect; no restart needed.": "Konfiguration gespeichert und wirksam; kein Neustart nötig.",
  "Receiver details could not be loaded.": "Empfängerdetails konnten nicht geladen werden.",
  "QR code module could not be loaded.": "Das QR-Code-Modul konnte nicht geladen werden.",
  "Access secrets require a fresh administrator session.": "Zugangsdaten erfordern eine frische Administrator-Sitzung.",
  "The project name does not match.": "Der Projektname stimmt nicht überein.",
  "Archive integrity check failed.": "Die Integritätsprüfung des Archivs ist fehlgeschlagen.",
  "The backup file is empty.": "Die Sicherungsdatei ist leer.",
  "Invalid backup record on line {line}.": "Ungültiger Sicherungseintrag in Zeile {line}.",
  "The backup contains too many records.": "Die Sicherung enthält zu viele Einträge.",
  "The survey backup header is invalid.": "Der Kopfeintrag der Vermessungssicherung ist ungültig.",
  "A project record is invalid.": "Ein Projekteintrag ist ungültig.",
  "A line record is invalid.": "Ein Linieneintrag ist ungültig.",
  "A vertex precedes its line.": "Ein Linienpunkt steht vor seiner Linie.",
  "An object point precedes its project.": "Ein Objektpunkt steht vor seinem Projekt.",
  "The backup has more than one footer.": "Die Sicherung hat mehr als einen Abschlusseintrag.",
  "The backup contains an unknown record.": "Die Sicherung enthält einen unbekannten Eintrag.",
  "The survey backup is incomplete.": "Die Vermessungssicherung ist unvollständig.",
  "Restore survey backup": "Vermessungssicherung wiederherstellen",
  "All projects will be replaced. This cannot be undone.": "Alle Projekte werden ersetzt. Das lässt sich nicht rückgängig machen.",
  "Rewrite the recording journal without changing survey data?": "Aufzeichnungsjournal neu schreiben, ohne Vermessungsdaten zu ändern?",
  "ARCHIVED PROJECTS": "ARCHIVIERTE PROJEKTE",
  "{points} · {lines} · schema {version}": "{points} · {lines} · Schema {version}",
  "Archive “{name}” verified": "Archiv „{name}“ geprüft",
  "Reactivate project": "Projekt reaktivieren",
  "Restore “{name}” to active memory? Capacity and flash reserve are checked first.": "„{name}“ in den aktiven Speicher zurückholen? Kapazität und Flash-Reserve werden zuerst geprüft.",
  "Project “{name}” reactivated": "Projekt „{name}“ reaktiviert",
  "Project “{name}” deleted": "Projekt „{name}“ gelöscht",
  "{name} and all contained survey data will be permanently deleted. Enter the project name to confirm.": "{name} und alle enthaltenen Vermessungsdaten werden endgültig gelöscht. Zur Bestätigung den Projektnamen eingeben.",
  "{name} · {points} · {length} m. Recording cannot be resumed after finishing.": "{name} · {points} · {length} m. Nach dem Abschließen lässt sich die Aufzeichnung nicht fortsetzen.",
  "Remove object point “{label}”?": "Objektpunkt „{label}“ entfernen?",
  "Remove line point “{label}”?": "Linienpunkt „{label}“ entfernen?",
  "{label} removed": "{label} entfernt",
  "Already used: {name}": "Bereits vergeben: {name}",
  "Control · {name}": "Kontrolle · {name}",
  "Filter object points": "Objektpunkte filtern",
  "Survey map with surveyed lines, object points and receiver position": "Vermessungskarte mit aufgenommenen Linien, Objektpunkten und Empfängerposition",
  "Portal QR code": "Portal-QR-Code",
  "Wi-Fi QR code": "WLAN-QR-Code",
  "Leave empty to keep saved value": "Leer lassen, um den gespeicherten Wert zu behalten",
  "RTK receiver": "RTK-Empfänger",
  "Ports {ports}": "Ports {ports}",
  "{count} task restarts": "{count} Task-Neustarts",
  "Satellites and geometry": "Satelliten und Geometrie",
  "Satellites ≥": "Satelliten ≥",
  "Correction age ≤": "Korrekturalter ≤",
  "Horizontal spread ≤": "Horizontale Streuung ≤",
  "Support package": "Support-Paket",
  "The measurement has taken more than one minute. Check the antenna view and correction stream.": "Die Messung dauert länger als eine Minute. Antennenumgebung und Korrekturstrom prüfen.",
  "The measurement is still waiting for stable values.": "Die Messung wartet noch auf stabile Werte.",
  "Rotate key": "Schlüssel erneuern",
  "All TCP and BLE clients will be disconnected. The old key stops working immediately.": "Alle TCP- und BLE-Clients werden getrennt. Der alte Schlüssel funktioniert sofort nicht mehr.",
  "Restore and restart": "Wiederherstellen und neu starten",
  "This replaces Wi-Fi and NTRIP settings, the measurement rules and method, and the field setup (antenna height, reference frame, epoch), then restarts the receiver. Backups from older firmware contain only Wi-Fi and NTRIP settings.": "Dies ersetzt die WLAN- und NTRIP-Einstellungen, Messregeln und Messmethode sowie die Feldeinrichtung (Antennenhöhe, Bezugsrahmen, Epoche) und startet den Empfänger neu. Sicherungen älterer Firmware enthalten nur WLAN- und NTRIP-Einstellungen.",
  "Restore the factory defaults: Quality OK uses 5 samples; RTK FIXED, 10 satellites, HDOP 1.5, correction age 5 s, H 0.05 m, V 0.10 m and spread 0.05 m.": "Werkseinstellungen wiederherstellen: Qualität OK nutzt 5 Messwerte; RTK FIXED, 10 Satelliten, HDOP 1,5, Korrekturalter 5 s, H 0,05 m, V 0,10 m und Streuung 0,05 m.",
  "TCP sends NMEA immediately without authentication. For BLE, pair with the six-digit PIN and enable encrypted Nordic UART Service notifications.": "TCP sendet NMEA sofort ohne Authentifizierung. Für BLE mit der sechsstelligen PIN koppeln und verschlüsselte Nordic-UART-Service-Benachrichtigungen aktivieren.",
  "This local portal uses HTTP, not HTTPS. Use it only on a trusted device network; network access follows the TCP authentication setting above.": "Dieses lokale Portal nutzt HTTP, nicht HTTPS. Nur in einem vertrauenswürdigen Gerätenetz verwenden; der Netzzugriff folgt der TCP-Authentifizierung oben."
});
  Object.assign(v2de,{
  "Assigned afterwards: {frame} for {count} points ({date})": "Nachträglich zugeordnet: {frame} für {count} Punkte ({date})",
  "{frame}, epoch {epoch}": "{frame}, Epoche {epoch}",
  "{count} RTK points of this project have no recorded reference frame ({parts}).": "{count} RTK-Punkte dieses Projekts haben keinen gespeicherten Bezugsrahmen ({parts}).",
  "{count} more lines": "{count} weitere Linien",
  "object points: {count}": "Objektpunkte: {count}",
  "{count} points without an RTK solution keep the frame \u201cunknown\u201d.": "{count} Punkte ohne RTK-Lösung behalten den Rahmen \u201eunbekannt\u201c.",
  "Replaces the current assignment {frame} ({count} points); {new} of the points are not assigned yet.": "Ersetzt die bisherige Zuordnung {frame} ({count} Punkte); {new} der Punkte sind noch nicht zugeordnet.",
  "Points with a recorded frame keep it. Coordinates are not transformed; the frame you enter is your statement about the correction service used then.": "Punkte mit gespeichertem Rahmen behalten ihn. Koordinaten werden nicht umgerechnet; der eingegebene Rahmen ist Ihre Angabe zum damals genutzten Korrekturdienst.",
  "This project has no RTK points without a reference frame.": "Dieses Projekt hat keine RTK-Punkte ohne Bezugsrahmen.",
  "Reference frame assigned to {count} points": "Bezugsrahmen für {count} Punkte zugeordnet",
  "Frame assignment undone": "Rahmenzuordnung zurückgenommen",
  "{count} points go back to the frame \u201cunknown\u201d. Recorded frames and coordinates do not change.": "{count} Punkte gehen zurück auf den Rahmen \u201eunbekannt\u201c. Gespeicherte Rahmen und Koordinaten ändern sich nicht.",
  "The points of this project changed during the check; review the assignment again.": "Die Punkte dieses Projekts haben sich während der Prüfung geändert; die Zuordnung erneut prüfen.",
  "The frame assignment of this project changed meanwhile; check it again.": "Die Rahmenzuordnung dieses Projekts hat sich inzwischen geändert; bitte erneut prüfen.",
  "This project has no frame assignment.": "Dieses Projekt hat keine Rahmenzuordnung.",
  "Pause the line of this project before assigning a reference frame.": "Die Linie dieses Projekts pausieren, bevor ein Bezugsrahmen zugeordnet wird.",
  "A reference frame is required.": "Ein Bezugsrahmen ist erforderlich.",
  "The frame names \"unknown\" and \"mixed\" are reserved.": "Die Rahmennamen \"unknown\" und \"mixed\" sind reserviert.",
  "The frame assignment of this project changed during the export; export again.": "Die Rahmenzuordnung dieses Projekts hat sich während des Exports geändert; erneut exportieren."
});
  // Form field names of the guided apply (#23c); "Caster host", "Mountpoint", "Fallback networks" and "NTRIP client" exist above.
  Object.assign(v2de,{
    "Wi-Fi name (SSID)": "WLAN-Name (SSID)",
    "Wi-Fi password": "WLAN-Passwort",
    "Caster port": "Caster-Port",
    "Caster user name": "Caster-Benutzername",
    "Caster password": "Caster-Passwort",
    "Fallback caster host": "Alternativer Caster-Host",
    "Fallback caster port": "Alternativer Caster-Port",
    "Fallback mountpoint": "Alternativer Mountpunkt",
    "Fallback caster user name": "Alternativer Caster-Benutzername",
    "Fallback caster password": "Alternativer Caster-Passwort"
  });
  // Restart and result texts of the guided apply (#23c): fixed texts by key, templates with user data in v2de.
  Object.assign(dictionaries.en,{
    "ui.guided_cause_unknown": "unknown cause ({code})",
    "ui.guided_step_wifi": "Wi-Fi",
    "ui.guided_step_storage": "Survey storage",
    "ui.guided_step_streaming": "Receiving corrections",
    "ui.guided_step_credentials_rejected": "Credentials rejected",
    "ui.guided_step_mount_not_found": "Mountpoint not found",
    "ui.guided_step_retry_limit": "Stopped after the retry limit",
    "ui.guided_step_switched_off": "Switched off",
    "ui.guided_step_not_set_up": "Not set up",
    "ui.guided_step_not_started": "Not started yet",
    "ui.guided_step_waiting_wifi": "Waiting for Wi-Fi",
    "ui.guided_step_connecting": "Connecting",
    "ui.guided_step_retrying": "Retrying shortly",
    "ui.guided_step_waiting": "Waiting",
    "ui.guided_step_reachable": "Reachable",
    "ui.guided_step_unreachable": "Not reachable yet",
    "ui.guided_step_waiting_device": "Waiting for the device",
    "ui.guided_step_connected": "Connected",
    "ui.guided_step_hotspot_only": "Hotspot only",
    "ui.guided_step_no_wifi": "No Wi-Fi; hotspot on",
    "ui.guided_step_ready": "Ready",
    "ui.guided_step_error": "Reports an error",
    "ui.guided_step_loading": "Loading",
    "ui.guided_marked_set_back": "Marked to be set back:",
    "ui.guided_wifi_not_set": "Wi-Fi not set",
    "ui.guided_no_caster": "no caster",
    "ui.guided_reported_settings": "Settings the device reports now:",
    "ui.guided_ntrip_on": "NTRIP on",
    "ui.guided_ntrip_off": "NTRIP off",
    "ui.guided_config_file_unread": "The device could not read its saved configuration file at start-up; the settings shown may be default values.",
    "ui.guided_wifi_now_connected": "Wi-Fi now: connected.",
    "ui.guided_wifi_now_connecting": "Wi-Fi now: connecting …",
    "ui.guided_wifi_now_setup": "Wi-Fi now: no connection – only the setup hotspot is on.",
    "ui.guided_wifi_now_recovery": "Wi-Fi now: no connection to a Wi-Fi network – the rescue hotspot is on and the device keeps trying.",
    "ui.guided_wifi_now_unknown": "Wi-Fi now: unknown.",
    "ui.guided_note_ntrip_credentials": "NTRIP: credentials rejected – check the caster settings.",
    "ui.guided_note_ntrip_mount": "NTRIP: mountpoint not found – check the caster settings.",
    "ui.guided_note_ntrip_parked": "NTRIP: the client stopped after its retry limit – check the caster settings and save them; saving, a Wi-Fi loss or a restart starts a new round.",
    "ui.guided_note_storage_error": "Survey storage reports an error – open Status for details.",
    "ui.guided_warn_measurement": "A measurement is running. The restart cancels it; points already saved are kept.",
    "ui.guided_restore_older_backup": "This backup comes from older firmware and contained only the Wi-Fi and NTRIP settings.",
    "ui.guided_restore_kept_wifi": "The backup contains no Wi-Fi network; the device kept its Wi-Fi access.",
    "ui.guided_caster_kept": "The caster changes from this save already took effect and stay in effect.",
    "ui.guided_timeout_device_network": "No answer after 90 s – connect the phone to the device's network and reload the page.",
    "ui.guided_outcome_not_restarted_uncertain": "It is not clear whether the device received the request; it did not restart.",
    "ui.guided_outcome_not_restarted": "The device did not restart; the change is not applied yet.",
    "ui.guided_outcome_done": "Restart complete.",
    "ui.guided_outcome_restart_observed": "A restart was observed. The answer to the request was lost, so it is not certain that this request caused it.",
    "ui.guided_outcome_open": "No final result yet. The steps above show the current state; reload this page later.",
    "ui.guided_phase_resuming": "Checking whether the device restarted …",
    "ui.guided_phase_back": "The device is reachable again – sign in to see the result.",
    "ui.guided_phase_verifying_reboot": "Waiting for the device to start …",
    "ui.guided_phase_verifying": "Checking the new Wi-Fi connection (the device must hold it for 10 s) …",
    "ui.guided_phase_settling": "Restart detected – checking whether the device is ready …",
    "ui.guided_phase_restarting": "The device is restarting …",
    "ui.guided_cause_access_point_not_found": "the Wi-Fi network was not found",
    "ui.guided_cause_authentication_failed": "the Wi-Fi password was rejected",
    "ui.guided_cause_association_failed": "the device could not join the Wi-Fi network",
    "ui.guided_cause_connection_timeout": "the connection attempt timed out",
    "ui.guided_cause_connection_lost": "the connection was lost during the check",
    "ui.guided_cause_apply_interrupted": "a restart interrupted the check",
    "ui.guided_cause_setup_mode": "the device started in setup mode before the check",
    "ui.guided_cause_storage_error": "the device could not store the change"
  });
  Object.assign(dictionaries.de,{
    "ui.guided_cause_unknown": "unbekannte Ursache ({code})",
    "ui.guided_step_wifi": "WLAN",
    "ui.guided_step_storage": "Messablage",
    "ui.guided_step_streaming": "Korrekturdaten kommen an",
    "ui.guided_step_credentials_rejected": "Zugangsdaten abgelehnt",
    "ui.guided_step_mount_not_found": "Mountpunkt nicht gefunden",
    "ui.guided_step_retry_limit": "Nach dem Wiederholungslimit gestoppt",
    "ui.guided_step_switched_off": "Ausgeschaltet",
    "ui.guided_step_not_set_up": "Nicht eingerichtet",
    "ui.guided_step_not_started": "Noch nicht gestartet",
    "ui.guided_step_waiting_wifi": "Warte auf WLAN",
    "ui.guided_step_connecting": "Verbindet",
    "ui.guided_step_retrying": "Neuer Versuch in Kürze",
    "ui.guided_step_waiting": "Warte",
    "ui.guided_step_reachable": "Erreichbar",
    "ui.guided_step_unreachable": "Noch nicht erreichbar",
    "ui.guided_step_waiting_device": "Warte auf das Gerät",
    "ui.guided_step_connected": "Verbunden",
    "ui.guided_step_hotspot_only": "Nur Hotspot",
    "ui.guided_step_no_wifi": "Kein WLAN; Hotspot an",
    "ui.guided_step_ready": "Bereit",
    "ui.guided_step_error": "Meldet einen Fehler",
    "ui.guided_step_loading": "Lädt",
    "ui.guided_marked_set_back": "Zur Rücknahme vorgesehen:",
    "ui.guided_wifi_not_set": "WLAN nicht eingerichtet",
    "ui.guided_no_caster": "kein Caster",
    "ui.guided_reported_settings": "Vom Gerät derzeit gemeldete Einstellungen:",
    "ui.guided_ntrip_on": "NTRIP an",
    "ui.guided_ntrip_off": "NTRIP aus",
    "ui.guided_config_file_unread": "Das Gerät konnte seine gespeicherte Konfigurationsdatei beim Start nicht lesen; die angezeigten Einstellungen können Standardwerte sein.",
    "ui.guided_wifi_now_connected": "WLAN jetzt: verbunden.",
    "ui.guided_wifi_now_connecting": "WLAN jetzt: verbindet …",
    "ui.guided_wifi_now_setup": "WLAN jetzt: keine Verbindung – nur der Einrichtungs-Hotspot ist an.",
    "ui.guided_wifi_now_recovery": "WLAN jetzt: keine Verbindung zu einem WLAN – der Rettungs-Hotspot ist an und das Gerät versucht es weiter.",
    "ui.guided_wifi_now_unknown": "WLAN jetzt: unbekannt.",
    "ui.guided_note_ntrip_credentials": "NTRIP: Zugangsdaten abgelehnt – Caster-Einstellungen prüfen.",
    "ui.guided_note_ntrip_mount": "NTRIP: Mountpunkt nicht gefunden – Caster-Einstellungen prüfen.",
    "ui.guided_note_ntrip_parked": "NTRIP: Der Client hat nach seinem Wiederholungslimit aufgehört – Caster-Einstellungen prüfen und speichern; Speichern, ein WLAN-Verlust oder ein Neustart startet eine neue Runde.",
    "ui.guided_note_storage_error": "Die Messablage meldet einen Fehler – Details unter Status.",
    "ui.guided_warn_measurement": "Eine Messung läuft. Der Neustart bricht sie ab; bereits gespeicherte Punkte bleiben erhalten.",
    "ui.guided_restore_older_backup": "Diese Sicherung stammt von älterer Firmware und enthielt nur die WLAN- und NTRIP-Einstellungen.",
    "ui.guided_restore_kept_wifi": "Die Sicherung enthält kein WLAN; das Gerät hat seinen WLAN-Zugang behalten.",
    "ui.guided_caster_kept": "Die Caster-Änderungen aus dieser Speicherung wirken bereits und bleiben wirksam.",
    "ui.guided_timeout_device_network": "Keine Antwort nach 90 s – das Telefon mit dem Netz des Geräts verbinden und die Seite neu laden.",
    "ui.guided_outcome_not_restarted_uncertain": "Es ist unklar, ob das Gerät den Auftrag erhalten hat; es ist nicht neu gestartet.",
    "ui.guided_outcome_not_restarted": "Das Gerät ist nicht neu gestartet; die Änderung ist noch nicht übernommen.",
    "ui.guided_outcome_done": "Neustart abgeschlossen.",
    "ui.guided_outcome_restart_observed": "Ein Neustart wurde beobachtet. Die Antwort auf den Auftrag ging verloren; daher ist nicht sicher, dass dieser Auftrag ihn ausgelöst hat.",
    "ui.guided_outcome_open": "Noch kein endgültiges Ergebnis. Die Schritte oben zeigen den aktuellen Zustand; diese Seite später neu laden.",
    "ui.guided_phase_resuming": "Prüfe, ob das Gerät neu gestartet ist …",
    "ui.guided_phase_back": "Das Gerät ist wieder erreichbar – zum Anzeigen des Ergebnisses anmelden.",
    "ui.guided_phase_verifying_reboot": "Warte auf den Start des Geräts …",
    "ui.guided_phase_verifying": "Prüfe die neue WLAN-Verbindung (das Gerät muss sie 10 s halten) …",
    "ui.guided_phase_settling": "Neustart erkannt – prüfe, ob das Gerät bereit ist …",
    "ui.guided_phase_restarting": "Das Gerät startet neu …",
    "ui.guided_cause_access_point_not_found": "das WLAN wurde nicht gefunden",
    "ui.guided_cause_authentication_failed": "das WLAN-Passwort wurde abgelehnt",
    "ui.guided_cause_association_failed": "das Gerät konnte dem WLAN nicht beitreten",
    "ui.guided_cause_connection_timeout": "der Verbindungsversuch hat zu lange gedauert",
    "ui.guided_cause_connection_lost": "die Verbindung ging während der Prüfung verloren",
    "ui.guided_cause_apply_interrupted": "ein Neustart hat die Prüfung unterbrochen",
    "ui.guided_cause_setup_mode": "das Gerät startete vor der Prüfung im Einrichtungsmodus",
    "ui.guided_cause_storage_error": "das Gerät konnte die Änderung nicht speichern"
  });
  Object.assign(v2de,{
    "Applied: connected to ‘{ssid}’. Portal: http://{host}/": "Übernommen: verbunden mit ‚{ssid}‘. Portal: http://{host}/",
    "Applied: connected to ‘{ssid}’.": "Übernommen: verbunden mit ‚{ssid}‘.",
    "Backup restored: connected to ‘{ssid}’.": "Sicherung wiederhergestellt: verbunden mit ‚{ssid}‘.",
    "Backup restored without its Wi-Fi settings: {cause}. The device returns to the previous Wi-Fi settings; the other settings from the backup apply.": "Sicherung ohne ihre WLAN-Einstellungen wiederhergestellt: {cause}. Das Gerät kehrt zu den vorherigen WLAN-Einstellungen zurück; die übrigen Einstellungen aus der Sicherung gelten.",
    "Not applied: {cause}. The device returns to the previous Wi-Fi settings.": "Nicht übernommen: {cause}. Das Gerät kehrt zu den vorherigen WLAN-Einstellungen zurück.",
    "The device reports: {reported}. This result does not belong to this request for certain.": "Das Gerät meldet: {reported}. Dieses Ergebnis gehört nicht sicher zu diesem Auftrag.",
    "connected to ‘{ssid}’": "verbunden mit ‚{ssid}‘",
    "not applied: {cause}": "nicht übernommen: {cause}",
    "Configuration result since the last device start: {reported}.": "Konfigurationsergebnis seit dem letzten Gerätestart: {reported}.",
    "Last recorded configuration result (time unknown): {reported}.": "Zuletzt gespeichertes Konfigurationsergebnis (Zeitpunkt unbekannt): {reported}.",
    "No answer after 90 s – connect the phone to ‘{ssid}’ and reload the page.": "Keine Antwort nach 90 s – das Telefon mit ‚{ssid}‘ verbinden und die Seite neu laden.",
    "Portal: http://{host}/": "Portaladresse: http://{host}/",
    "Wi-Fi ‘{ssid}’": "WLAN ‚{ssid}‘",
    "caster {host}:{port}/{mount}": "Caster {host}:{port}/{mount}",
    "{step} still waiting after 60 s – state: {state}.": "{step} wartet nach 60 s noch – Zustand: {state}.",
    "Line ‘{name}’ is recording. The restart interrupts the recording; points already saved are kept.": "Linie ‚{name}‘ zeichnet auf. Der Neustart unterbricht die Aufzeichnung; bereits gespeicherte Punkte bleiben erhalten."
  });
  // Configuration page of the guided apply (#23c): review, save, staged changes, backup section.
  Object.assign(dictionaries.en,{
    "ui.guided_check_changes": "Check changes",
    "ui.guided_unsaved": "Unsaved changes",
    "ui.guided_no_changes": "No changes to save.",
    "ui.guided_fields_legend": "Configuration settings",
    "ui.guided_review_title": "Review before saving",
    "ui.guided_review_now": "Takes effect at once without a restart; the correction connection is rebuilt.",
    "ui.guided_review_restart_wifi": "The device restarts. The new Wi-Fi settings stay only if it holds the connection for 10 s; otherwise it returns to the previous ones.",
    "ui.guided_review_restart_staged": "The device restarts to apply the changes waiting in its memory.",
    "ui.guided_review_earlier": "Also applied with this restart (values not shown): {labels}",
    "ui.guided_back_to_editing": "Back to editing",
    "ui.guided_save": "Save",
    "ui.guided_save_restart": "Save and restart",
    "ui.guided_save_later": "Save without restart",
    "ui.guided_saving": "Saving …",
    "ui.guided_staging_changed": "The changes waiting in the device memory changed. Check them again.",
    "ui.guided_uncertain": "No answer from the device. Reload the page and check whether the change was saved.",
    "ui.guided_field_rejected": "Not saved: the device did not accept a value. See the marked field.",
    "ui.guided_kept_memory": "Kept in the device memory only. A restart before ‘Apply’ drops these changes.",
    "ui.guided_saved_later": "Saved. The settings take effect after the next restart.",
    "ui.guided_staged_title": "Changes waiting in the device memory",
    "ui.guided_staged_waiting": "Waiting in the device memory from an earlier save (values not shown): {labels}. ‘Apply now’ restarts the device with them; a restart without ‘Apply’ drops them.",
    "ui.guided_ntrip_no_data": "no correction data yet – state: {state}",
    "ui.guided_backup_kicker": "DEVICE CONFIGURATION BACKUP",
    "ui.guided_backup_download": "Download device configuration",
    "ui.guided_backup_restore": "Restore device configuration",
    "ui.guided_backup_help": "Contains Wi-Fi, NTRIP, measurement rules and method and the field setup."
  });
  Object.assign(dictionaries.de,{
    "ui.guided_check_changes": "Änderungen prüfen",
    "ui.guided_unsaved": "Nicht gespeicherte Änderungen",
    "ui.guided_no_changes": "Keine Änderungen zu speichern.",
    "ui.guided_fields_legend": "Konfigurationseinstellungen",
    "ui.guided_review_title": "Vor dem Speichern prüfen",
    "ui.guided_review_now": "Gilt sofort ohne Neustart; die Korrekturverbindung wird neu aufgebaut.",
    "ui.guided_review_restart_wifi": "Das Gerät startet neu. Die neuen WLAN-Einstellungen bleiben nur bestehen, wenn es die Verbindung 10 s hält; sonst kehrt es zu den vorherigen zurück.",
    "ui.guided_review_restart_staged": "Das Gerät startet neu, um die im Gerätespeicher wartenden Änderungen zu übernehmen.",
    "ui.guided_review_earlier": "Wird mit diesem Neustart ebenfalls übernommen (Werte werden nicht angezeigt): {labels}",
    "ui.guided_back_to_editing": "Zurück zum Bearbeiten",
    "ui.guided_save": "Speichern",
    "ui.guided_save_restart": "Speichern und neu starten",
    "ui.guided_save_later": "Ohne Neustart speichern",
    "ui.guided_saving": "Wird gespeichert …",
    "ui.guided_staging_changed": "Die im Gerätespeicher wartenden Änderungen haben sich geändert. Bitte erneut prüfen.",
    "ui.guided_uncertain": "Keine Antwort vom Gerät. Die Seite neu laden und prüfen, ob die Änderung gespeichert wurde.",
    "ui.guided_field_rejected": "Nicht gespeichert: Das Gerät hat einen Wert nicht angenommen. Siehe das markierte Feld.",
    "ui.guided_kept_memory": "Nur im Gerätespeicher gehalten. Ein Neustart vor ‚Übernehmen‘ verwirft diese Änderungen.",
    "ui.guided_saved_later": "Gespeichert. Die Einstellungen gelten nach dem nächsten Neustart.",
    "ui.guided_staged_title": "Im Gerätespeicher wartende Änderungen",
    "ui.guided_staged_waiting": "Wartet im Gerätespeicher aus einer früheren Speicherung (Werte werden nicht angezeigt): {labels}. ‚Jetzt übernehmen‘ startet das Gerät damit neu; ein Neustart ohne ‚Übernehmen‘ verwirft sie.",
    "ui.guided_ntrip_no_data": "noch keine Korrekturdaten – Zustand: {state}",
    "ui.guided_backup_kicker": "SICHERUNG DER GERÄTEKONFIGURATION",
    "ui.guided_backup_download": "Gerätekonfiguration herunterladen",
    "ui.guided_backup_restore": "Gerätekonfiguration wiederherstellen",
    "ui.guided_backup_help": "Enthält WLAN, NTRIP, Messregeln und -verfahren sowie die Feldeinrichtung."
  });
  Object.assign(v2de,{
    "Not accepted by the device: {reason}": "Vom Gerät nicht angenommen: {reason}"
  });
  // Guided restart (#23c): hold, restart panel, staged apply, restart confirmation.
  Object.assign(dictionaries.en,{
    "ui.guided_hold_placeholder": "The device is restarting – this page continues when it is back.",
    "ui.guided_panel_title": "Restart and check",
    "ui.guided_restart_anyway": "Restart anyway",
    "ui.guided_action_edit": "Edit again",
    "ui.guided_action_field_setup": "Check field setup",
    "ui.guided_action_retry": "Apply again",
    "ui.guided_action_close": "Close",
    "ui.guided_action_portal": "Open the portal",
    "ui.guided_apply_now": "Apply now",
    "ui.guided_apply_all": "Apply all and restart",
    "ui.guided_later": "Later",
    "ui.guided_apply_title": "Apply the waiting changes",
    "ui.guided_apply_staged": "Apply the changes waiting in the device memory and restart (values not shown): {labels}",
    "ui.guided_staging_changed_meanwhile": "The changes waiting in the device memory changed in the meantime (values not shown): {labels}",
    "ui.guided_nothing_left": "Nothing left to apply; a restart dropped the staged change. Edit again.",
    "ui.guided_not_sent": "No answer from the device; nothing was sent. Try again.",
    "ui.guided_already_in_effect": "Already in effect.",
    "ui.guided_restart_started": "Saved. The device restarts now – the panel shows each step.",
    "ui.guided_restore_discards_edits": "Unsaved changes on this page are discarded.",
    "ui.guided_restore_busy": "The device is still checking an earlier change. Nothing was changed; try again after the restart.",
    "ui.guided_restore_too_large": "The file is too large for a configuration backup. Nothing was changed.",
    "ui.guided_restore_stage_failed": "The device could not store the whole backup. Some settings (measurement rules, field setup, caster) may already apply; check the device before you try again."
  });
  Object.assign(dictionaries.de,{
    "ui.guided_hold_placeholder": "Das Gerät startet neu – diese Seite geht weiter, sobald es wieder erreichbar ist.",
    "ui.guided_panel_title": "Neustart und Prüfung",
    "ui.guided_restart_anyway": "Trotzdem neu starten",
    "ui.guided_action_edit": "Erneut bearbeiten",
    "ui.guided_action_field_setup": "Feldeinrichtung prüfen",
    "ui.guided_action_retry": "Erneut übernehmen",
    "ui.guided_action_close": "Schließen",
    "ui.guided_action_portal": "Portal öffnen",
    "ui.guided_apply_now": "Jetzt übernehmen",
    "ui.guided_apply_all": "Alle übernehmen und neu starten",
    "ui.guided_later": "Später",
    "ui.guided_apply_title": "Wartende Änderungen übernehmen",
    "ui.guided_apply_staged": "Die im Gerätespeicher wartenden Änderungen übernehmen und neu starten (Werte werden nicht angezeigt): {labels}",
    "ui.guided_staging_changed_meanwhile": "Die im Gerätespeicher wartenden Änderungen haben sich inzwischen geändert (Werte werden nicht angezeigt): {labels}",
    "ui.guided_nothing_left": "Nichts mehr zu übernehmen; ein Neustart hat die gespeicherte Änderung verworfen. Erneut bearbeiten.",
    "ui.guided_not_sent": "Keine Antwort vom Gerät; es wurde nichts gesendet. Bitte erneut versuchen.",
    "ui.guided_already_in_effect": "Bereits wirksam.",
    "ui.guided_restart_started": "Gespeichert. Das Gerät startet jetzt neu – die Anzeige zeigt jeden Schritt.",
    "ui.guided_restore_discards_edits": "Nicht gespeicherte Änderungen auf dieser Seite werden verworfen.",
    "ui.guided_restore_busy": "Das Gerät prüft noch eine frühere Änderung. Es wurde nichts geändert; nach dem Neustart erneut versuchen.",
    "ui.guided_restore_too_large": "Die Datei ist für eine Konfigurationssicherung zu groß. Es wurde nichts geändert.",
    "ui.guided_restore_stage_failed": "Das Gerät konnte die Sicherung nicht vollständig speichern. Einige Einstellungen (Messregeln, Feldeinrichtung, Caster) können bereits wirksam sein; das Gerät prüfen, bevor es erneut versucht wird."
  });
  Object.assign(v2de,{
    "since {n} s": "seit {n} s",
    "Restart needed": "Neustart erforderlich",
    "The device must restart for these settings to take effect.": "Das Gerät muss neu starten, damit diese Einstellungen wirksam werden.",
    "Restart now": "Jetzt neu starten",
    "Session lost during restart": "Sitzung beim Neustart beendet",
    "Backup not restored: {reason} Nothing was changed.": "Sicherung nicht wiederhergestellt: {reason} Es wurde nichts geändert."
  });
  // M2 #38b: templates around service data (frame names and epochs go through capacityText only)
  Object.assign(v2de,{
    "{frame}, epoch {epoch} – use it for new measurements?": "{frame}, Epoche {epoch} – für neue Messungen übernehmen?",
    "{frame} – use it for new measurements?": "{frame} – für neue Messungen übernehmen?",
    "The correction service reports {frame} ({epoch}), the field setup says {current} ({current_epoch}). New RTK points are saved with the field setup value.": "Der Korrekturdienst meldet {frame} ({epoch}), in der Feldeinrichtung steht {current} ({current_epoch}). Neue RTK-Punkte werden mit dem Wert der Feldeinrichtung gespeichert.",
    "epoch {epoch}": "Epoche {epoch}",
    "The correction service reports {frame}.": "Der Korrekturdienst meldet {frame}."
  });
  const api = {dictionaries:dictionaries, v2de:v2de, t:translate, label:label,
               apply:apply, setLanguage:setLanguage,
               applyTheme:applyTheme, toggleTheme:toggleTheme, setTheme:setTheme, themes:THEMES,
               formatTime:formatTime, formatDateTime:formatDateTime,
               getLanguage:function(){return language;}, getTheme:function(){return theme;},
               escapeHtml:escapeHtml, mount:mount};
  root.RtkI18n = api;
  applyTheme();
  // Another tab changed the choice: follow it without writing it back.
  if (root.addEventListener) root.addEventListener("storage", function (event) {
    if (event.key === THEME_KEY || event.key === null) commitTheme(normalTheme(event.newValue), false);
  });
  if (root.document) {
    if (root.document.readyState === "loading") root.document.addEventListener("DOMContentLoaded", mount);
    else mount();
  }
  if (typeof module !== "undefined" && module.exports) module.exports = api;
}(typeof window !== "undefined" ? window : globalThis));
