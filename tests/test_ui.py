# SPDX-License-Identifier: AGPL-3.0-only
"""Web interface: delivery and access protection."""
import os
import re
import unittest
import web_routing

from support import cfg, ui, web
import assets

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(ROOT, "base.css"), encoding="utf-8") as source:
    BASE_CSS = source.read()
with open(os.path.join(ROOT, "pages.js"), encoding="utf-8") as source:
    PAGES_JS = source.read()
with open(os.path.join(ROOT, "pages.css"), encoding="utf-8") as source:
    PAGES_CSS = source.read()
with open(os.path.join(ROOT, "app.js"), encoding="utf-8") as source:
    APP_JS = source.read()
with open(os.path.join(ROOT, "app.css"), encoding="utf-8") as source:
    APP_CSS = source.read()
SETUP_SOURCE = web.SETUP_HTML + PAGES_JS + BASE_CSS + PAGES_CSS


class TestPage(unittest.TestCase):

    def test_asset_version_follows_the_firmware_version(self):
        # A release bump must move the cache-buster too, or browsers keep stale assets.
        self.assertEqual(cfg.CONFIG["version"].rsplit(" V", 1)[1], assets.ASSET_VERSION)

    def test_current_shell_is_external_and_responsive(self):
        self.assertIn('id="app-main"', ui.APP_V2)
        self.assertNotIn('id="view-back"', ui.APP_V2)
        self.assertIn('id="live-state" class="live-state" type="button"', ui.APP_V2)
        self.assertIn('<link rel="icon" href="data:image/svg+xml,', ui.APP_V2)
        self.assertIn('<link rel="icon" href="data:image/svg+xml,', ui.APP_V2_LOGIN)
        self.assertIn('/app.css?v=%s' % assets.ASSET_VERSION, ui.APP_V2)
        self.assertIn('/app.js?v=%s' % assets.ASSET_VERSION, ui.APP_V2)
        self.assertIn('.view-heading{display:flex;width:0;min-width:0;flex:1;', APP_CSS)
        self.assertIn('.module-console{overflow:auto;width:100%;min-width:0;max-width:100%', APP_CSS)
        self.assertIn('class:"device-metrics diagnostics-summary"', APP_JS)
        self.assertNotIn('JSON.stringify(data,null,2)', APP_JS)
        self.assertIn('@media(min-width:900px)', APP_CSS)
        for contract in ('const store=', 'const api=', 'async function renderRoute(retry=0)',
                         'function mountStatus()', 'async function mountProjects',
                         'async function mountDevice'):
            self.assertIn(contract, APP_JS)
        self.assertNotIn('main.innerHTML', APP_JS)
        for screen in ('mountRtcm', 'mountAccess', 'mountLog', 'mountModule',
                       'mountConfiguration', 'mountDiagnostics', 'mountLabel',
                       'mountSettings'):
            self.assertIn('function %s' % screen, APP_JS)
        self.assertIn('const screens={"device/rtcm"', APP_JS)
        self.assertIn('/api/log?limit=200&cursor=', APP_JS)
        self.assertIn('/ui-v2', web_routing.ROUTES)

    def test_v2_login_uses_the_same_design_and_session_contract(self):
        self.assertIn('data-page="app-v2-login"', ui.APP_V2_LOGIN)
        self.assertIn('id="v2-login-form"', ui.APP_V2_LOGIN)
        self.assertIn('device_code:code,next:"/"', APP_JS)
        self.assertIn('.v2-login{', APP_CSS)
        for page in (ui.APP_V2, ui.APP_V2_LOGIN):
            self.assertNotIn('<style', page)
            self.assertNotRegex(page, r'<script(?![^>]+\bsrc=)')

    def test_v2_configuration_exposes_ntrip_switch_and_fallback(self):
        for field in ("ntrip_enabled", "ntrip_fallback_host",
                      "ntrip_fallback_port", "ntrip_fallback_mount",
                      "ntrip_fallback_user", "ntrip_fallback_pass"):
            self.assertIn(field, APP_JS)
        self.assertIn('values.ntrip_enabled=values.ntrip_enabled==="1"', APP_JS)
        self.assertIn('values.ntrip_fallback_port=Number(values.ntrip_fallback_port)',
                      APP_JS)
        self.assertIn("Fallback caster (optional)", APP_JS)
        self.assertIn("No password is saved.", APP_JS)

    def test_v2_exposes_route_recording_and_wifi_errors(self):
        for value in ('recording_mode', 'Route — preserve every valid position',
                      'values.recording_mode==="route"', 'live.network',
                      'last_wifi_error', 'Wi-Fi error:'):
            self.assertIn(value, APP_JS)

    def test_start_line_response_does_not_serialize_flash_sequences(self):
        with open(os.path.join(ROOT, "web.py"), encoding="utf-8") as source:
            web_source = source.read()
        self.assertIn('line = tracker.start_line(', web_source)
        self.assertIn('result = {"id": line["id"], "name": line["name"]',
                      web_source)
        self.assertNotIn('result = tracker.start_line(', web_source)

    def test_v2_restart_button_uses_explicit_translation_key(self):
        self.assertIn('label==="Restart device"', APP_JS)
        self.assertIn('tr("ui.device_restart","Restart device")', APP_JS)
        self.assertIn('tr("ui.device_restart_confirm"', APP_JS)

    def test_v2_keeps_complete_navigation_and_field_contracts(self):
        for contract in ('projectChild=route.path.startsWith("projects/")',
                         'function updateGlobalStatus(live)',
                         '[hidden]{display:none!important}', 'line.last_point',
                         'data-context="storage"', 'diameter_mm', 'burial_depth_m',
                         'Load more line points', 'refreshMap=async',
                         'navigator.clipboard&&navigator.clipboard.writeText',
                         'waitForConfigResult', 'password_set:Boolean',
                         'keepAwake=localStorage'):
            self.assertIn(contract, APP_JS + APP_CSS)
        self.assertNotIn('back.href=projectChild', APP_JS)
        self.assertIn('const types=["tap","valve","hydrant","branch","transition","control","tree","repair","line_start","line_end","other"]', APP_JS)
        self.assertNotIn('"meter","junction","bend","reducer"', APP_JS)
        self.assertIn('globalThis.File?new File([blob],name', APP_JS)

    def test_v2_restores_field_and_diagnostic_parity(self):
        for contract in ('async function undoLast()', 'trackingAction("undo")',
                         'min:"0",max:"100",step:"0.01"', 'value:"tap"',
                         'vertical_delta_m', 'vertex.vertical_step_m', 'vertex.source',
                         'Reference station', 'Last NTRIP error', 'fix_quality_seconds',
                         'Recording journal', 'Caster host', 'BLE service UUID',
                         'CPU clock', 'IDF heap free', 'Wi-Fi transmit power',
                         'Automatic on', 'Clock time', 'Share filtered'):
            self.assertIn(contract, APP_JS)
        self.assertIn('measurementStarted=false', APP_JS)
        self.assertIn('if(progress.active)measurementStarted=true', APP_JS)
        self.assertIn('if(measurementStarted){', APP_JS)
        self.assertIn('details.open&&Date.now()-statusDetailsUpdatedAt>=30000', APP_JS)
        self.assertIn('Math.max(.5,Math.min(4', APP_JS)
        self.assertIn('minus.disabled=zoom<=.5', APP_JS)
        self.assertIn('class","map-world"', APP_JS)
        self.assertIn('scale(${1/zoom})', APP_JS)
        self.assertIn('mapGraphic.scaleMetres/zoom', APP_JS)
        self.assertIn('class:"map-layers"', APP_JS)
        self.assertIn('requestFullscreen', APP_JS)
        self.assertIn('[1,5,10,"quality"]', APP_JS)
        self.assertIn('progress.mode==="quality"', APP_JS)
        self.assertIn('qualityMode?null:undefined', APP_JS)
        self.assertIn('.readiness-hero .metric-grid{', APP_CSS)
        self.assertIn('.map-world{', APP_CSS)
        self.assertIn('.quality-history-row', APP_CSS)
        self.assertIn('progress::-webkit-progress-value', APP_CSS)

    def test_restart_waits_for_a_new_boot_before_reloading(self):
        for source in (APP_JS,):
            self.assertIn('/status?_reboot=', source)
            self.assertIn('uptime<15', source)
            self.assertIn('location.reload()', source)
        self.assertIn('setTimeout(resolve,8000)', APP_JS)
        self.assertNotIn('fetch("/",{cache:"no-store"})', APP_JS)

    def test_field_actions_use_the_compact_performance_path(self):
        self.assertIn('/api/tracking?view=progress', APP_JS)
        self.assertIn('setTimeout(update,650)', APP_JS)
        self.assertIn('measurementRequestActive&&!force', APP_JS)
        self.assertIn('if(measurementRequestActive)return', APP_JS)
        self.assertIn('function applyTrackingMutation(result)', APP_JS)
        self.assertIn('result.tracking_live', APP_JS)
        self.assertIn('button.classList.add("busy")', APP_JS)
        self.assertIn('.btn.busy::after', APP_CSS)
        self.assertNotIn('setTimeout(update,350)', APP_JS)
        self.assertNotIn('if(result)showSuccess("Line point saved");await pollLive(true)', APP_JS)
        self.assertIn('function mergeMapDelta(map,delta)', APP_JS)
        self.assertIn('since_revision=${Number(map.revision)||0}', APP_JS)

    def test_main_app_assets_have_precompressed_variants(self):
        for name in ("app.js", "app.css"):
            with open(os.path.join(ROOT, name), "rb") as source:
                plain = source.read()
            with open(os.path.join(ROOT, name + ".gz"), "rb") as source:
                compressed = source.read()
            self.assertLess(len(compressed), len(plain) // 2)
        self.assertIn('Content-Encoding: gzip', open(
            os.path.join(ROOT, "web.py"), encoding="utf-8").read())
        self.assertIn('"app.css.gz"', open(
            os.path.join(ROOT, "push.py"), encoding="utf-8").read())
        self.assertIn('gzip.compress(handle.read(), compresslevel=9, mtime=0)', open(
            os.path.join(ROOT, "push.py"), encoding="utf-8").read())

    def test_assets_are_external_and_csp_compatible(self):
        pages = (ui.APP_V2, web.SETUP_HTML, web.DIAGNOSTICS_HTML,
                 web.LOGIN_HTML, web.LABEL_HTML)
        for page in pages:
            self.assertNotIn("<style", page)
            self.assertNotRegex(page, r"<script(?![^>]+\bsrc=)")
            self.assertNotRegex(page, r"\sstyle=")
            self.assertNotRegex(page, r"\sonclick=")


    def test_high_contrast_theme_is_explicit_persistent_and_accessible(self):
        with open(os.path.join(ROOT, "i18n.js"), encoding="utf-8") as source:
            i18n_source = source.read()
        for value in ('const THEME_KEY = "rtk-theme"',
                      'data-theme-toggle', 'aria-pressed="false"',
                      'setAttribute("data-theme", "high-contrast")',
                      'localStorage.setItem(THEME_KEY, theme)',
                      '"ui.high_contrast": "High contrast"',
                      '"ui.high_contrast": "Hoher Kontrast"'):
            self.assertIn(value, i18n_source)
        self.assertIn(':root[data-theme="high-contrast"]', BASE_CSS)

    def test_every_page_loads_the_shared_style_layer_first(self):
        # Two sides that separately define their colors and mass run apart - that's
        # exactly what happened. base.css is the only source, and each side loads them
        # in front of its own sheet.
        for page in (web.SETUP_HTML, web.DIAGNOSTICS_HTML,
                     web.LOGIN_HTML, web.LABEL_HTML):
            self.assertIn('href="/base.css', page)
            own = "/pages.css"
            self.assertLess(page.index("/base.css"), page.index(own), own)
        self.assertIn(("/base.css", ("base.css", "text/css; charset=utf-8")),
                      web.STATIC_ASSETS.items())

    def test_design_tokens_exist_only_in_the_shared_layer(self):
        for token in ("--radius:10px", "--space-4:16px", "--tap:46px",
                      "--shadow:0 1px 3px", "--surface:#fff", "--accent:#075fb8"):
            self.assertIn(token, BASE_CSS, token)
        # Neither side is allowed to bring its own color; otherwise it falls out of the
        # row in the external contrast theme.
        for name, sheet in (("pages.css", PAGES_CSS),):
            self.assertNotRegex(sheet, r"#[0-9a-fA-F]{3,8}", name)
            self.assertNotIn(":root{", sheet, name)
        for sheet in (BASE_CSS, PAGES_CSS):
            self.assertNotIn("100% - 2*", sheet)

    def test_pages_share_one_visual_component_language(self):
        for value in ("button:hover:not(:disabled)", "border-radius:var(--radius)",
                      "background:var(--surface)", "@media(max-width:600px)"):
            self.assertIn(value, BASE_CSS, value)
        for page in (web.SETUP_HTML, web.DIAGNOSTICS_HTML,
                     web.LOGIN_HTML, web.LABEL_HTML):
            self.assertIn('class="product-header', page)
            self.assertIn('class="product-name"', page)
        for page in (web.SETUP_HTML, web.DIAGNOSTICS_HTML, web.LABEL_HTML):
            self.assertIn('class="product-nav app-navigation"', page)
            self.assertIn('class="page-content', page)
        self.assertNotIn('class="product-nav"', web.LOGIN_HTML)

    def test_navigation_and_surfaces_use_one_idiom(self):
        # Tabs in the dashboard and side links looked like two products.
        self.assertIn('.product-nav a,.product-nav button', BASE_CSS)
        self.assertIn('[aria-selected="true"]', BASE_CSS)
        self.assertIn('[aria-current="page"]', BASE_CSS)
        # Each page builds on the same two surfaces: panel carries page content, card
        # the tiles in it.
        for page in (web.SETUP_HTML, web.DIAGNOSTICS_HTML,
                     web.LOGIN_HTML, web.LABEL_HTML):
            self.assertRegex(page, r'class="[^"]*\b(panel|card)\b', page[:80])
        self.assertIn(".panel{", BASE_CSS)
        self.assertIn(".card{", BASE_CSS)

    def test_mobile_navigation_keeps_every_destination_visible(self):
        self.assertIn('grid-template-columns:repeat(3,minmax(0,1fr))', BASE_CSS)
        self.assertIn('@media(max-width:380px)', BASE_CSS)
        self.assertIn('grid-template-columns:repeat(2,minmax(0,1fr))', BASE_CSS)
        mobile = BASE_CSS[BASE_CSS.index('@media(max-width:600px)'):]
        self.assertNotIn('overflow-x:auto', mobile)
        self.assertIn('overflow-wrap:anywhere', mobile)


    def test_product_is_named_esp_rtk_everywhere(self):
        self.assertTrue(cfg.CONFIG["version"].startswith("ESP-RTK V"))
        self.assertEqual(cfg.FACTORY_AP_SSID, "ESP-RTK-Setup")
        self.assertEqual(cfg.CONFIG["ap_ssid"], "ESP-RTK-Setup")
        # Built by concatenation so the old product name is not a literal
        # substring anywhere in the exported public tree.
        old_suffix_name = "GNSS-" + "PRO"
        old_prefix_name = "ESP32-" + "GNSS"
        for page in (web.SETUP_HTML, web.render_setup(csrf="CSRF"), ui.APP_V2, ui.APP_V2_LOGIN):
            self.assertNotIn(old_suffix_name, page)
            self.assertNotIn(old_prefix_name, page)

    def test_authenticated_shell_order_and_logout_are_identical(self):
        pages = (web.SETUP_HTML, web.DIAGNOSTICS_HTML, web.LABEL_HTML)
        for page in pages:
            header = page.index('class="product-header"')
            navigation = page.index('class="product-nav app-navigation')
            status = page.index('class="device-status-bar"')
            content = page.index('class="page-content')
            self.assertLess(header, navigation)
            self.assertLess(navigation, status)
            self.assertLess(status, content)
            self.assertIn("logout", page)
        self.assertNotIn("app-navigation", web.LOGIN_HTML)
        self.assertNotIn("page-logout", web.LOGIN_HTML)
        for value in ('querySelectorAll(".page-logout")', 'method:"DELETE"',
                      '"X-CSRF-Token":csrf', 'location.replace("/")'):
            self.assertIn(value, PAGES_JS)


class TestZugriff(unittest.TestCase):
    def setUp(self):
        self.saved = dict(cfg.CONFIG)

    def tearDown(self):
        cfg.CONFIG.clear()
        cfg.CONFIG.update(self.saved)

    def test_without_token_not_from_the_sta_network(self):
        cfg.CONFIG["config_token"] = ""
        self.assertFalse(web._config_allowed(False, "", b""))

    def test_with_tokens_from_the_sta_network(self):
        cfg.CONFIG["config_token"] = "secret"
        self.assertTrue(web._config_allowed(False, "token=secret", b""))

    def test_always_through_the_ap(self):
        cfg.CONFIG["config_token"] = "secret"
        self.assertTrue(web._config_allowed(True, "", b""))


if __name__ == "__main__":
    unittest.main()


class TestNavigation(unittest.TestCase):

    def test_side_pages_link_all_portal_areas(self):
        for page in (web.SETUP_HTML, web.DIAGNOSTICS_HTML, web.LABEL_HTML):
            for route in ("#status", "#projects", "#device"):
                self.assertIn(route, page)


    def test_protected_side_pages_show_the_status_line(self):
        for page in (web.SETUP_HTML, web.DIAGNOSTICS_HTML, web.LABEL_HTML):
            self.assertIn('class="device-status-bar"', page)
        self.assertNotIn('class="device-status-bar"', web.LOGIN_HTML)
        for value in ('fetch("/status")', 'setInterval(() =>', 'board.cpu_hz',
                      'board.ram_free_bytes', 'board.fs_free_bytes',
                      'stats.wifi_ssid_aktiv', 'stats.task_restarts'):
            self.assertIn(value, PAGES_JS)

    def test_config_page_links_to_overview(self):
        # Check on the rendered HTML, not on the template - there is still the
        # placeholder for the token.
        self.assertIn('href="/', web.render_setup())


class TestTokenInLink(unittest.TestCase):
    def setUp(self):
        self.saved = dict(cfg.CONFIG)

    def tearDown(self):
        cfg.CONFIG.clear()
        cfg.CONFIG.update(self.saved)

    def test_without_tokens_the_link_remains_simple(self):
        html = web.render_setup()
        self.assertIn('href="/#status"', html)
        self.assertNotIn("{token_query}", html)


    def test_old_token_will_not_be_attached(self):
        html = web.render_setup(token="s3cret")
        self.assertNotIn("token=s3cret", html)

    def test_tokens_in_the_link_are_masked(self):
        html = web.render_setup(token='a"b')
        self.assertNotIn('href="/?token=a"b"', html)

    def test_no_placeholders_remain_standing(self):
        for html in (web.render_setup(), web.render_setup(token="x")):
            self.assertNotIn("{token_query}", html)
            self.assertNotIn("{config_token_field}", html)


class TestTokenEncoding(unittest.TestCase):
    """html_escape is for HTML attributes, not for URLs. a token with '&' or '#' was cut off in the link, one decoded with '+' as space, one with '%' made the URL unruly.
    """

    def test_kodiert_trennzeichen(self):
        self.assertEqual(web.url_encode("a&b"), "a%26b")
        self.assertEqual(web.url_encode("a#b"), "a%23b")
        self.assertEqual(web.url_encode("a b"), "a%20b")
        self.assertEqual(web.url_encode("a+b"), "a%2Bb")
        self.assertEqual(web.url_encode("100%"), "100%25")

    def test_leaves_unobjectionable(self):
        self.assertEqual(web.url_encode("ee34faa987c0fd3c"), "ee34faa987c0fd3c")
        self.assertEqual(web.url_encode("a-b_c.d~e"), "a-b_c.d~e")

    def test_utf8(self):
        self.assertEqual(web.url_encode("café"), "caf%C3%A9")

    def test_umkehrbar(self):
        for value in ("a&b", "a#b", "a b", "a+b", "100%", "café", "naïve"):
            self.assertEqual(web.url_decode(web.url_encode(value)), value)

    def test_token_is_not_taken_over_in_link(self):
        for token in ("a&b", "a#b", "a b", "a+b", "100%"):
            html = web.render_setup(token=token)
            self.assertNotIn("token=", html)

    def test_token_does_not_arrive_at_the_server(self):
        for token in ("a&b", "a#b", "a b", "100%"):
            html = web.render_setup(token=token)
            self.assertNotIn(web.url_encode(token), html)
