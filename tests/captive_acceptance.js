// SPDX-License-Identifier: AGPL-3.0-only
// Captive entry (#19c): sign-in keeps the requested route, the fallback page is reachable (also without
// JavaScript) and its Wi-Fi scan waits for a pending answer. Run through tests/browser_acceptance.py.
const { chromium } = require("playwright");
const fs = require("fs"),
  assert = require("assert"),
  childProcess = require("child_process"),
  http = require("http"),
  root = process.cwd();
const i18n = require(root + "/i18n.js");
const appHtml = fs.readFileSync(process.env.RTK_APP_HTML || "/tmp/app-v2-local.html", "utf8");
// Pages come from the repository, with the identity the tests use elsewhere (hostname rtk-d4e5f6).
const pagesOutput = childProcess.execFileSync("python3", ["-c", [
  "import sys, json",
  "sys.path.insert(0, 'tests')",
  "from support import state, web",
  "import ui",
  "state.app.identity = {'device_id': 'A1B2C3D4E5F6', 'display_name': 'RTK-D4E5F6', 'ble_name': 'RTK-D4E5F6',",
  "                      'hostname': 'rtk-d4e5f6', 'device_code': 'ABCDEF123456'}",
  "sys.stdout.write('\\n@@pages@@' + json.dumps({'login': ui.APP_V2_LOGIN, 'setup': web.render_setup(csrf='CSRF'),",
  "                             'legacyLogin': web.render_login('/setup')}))",
].join("\n")], { cwd: root, encoding: "utf8" });
// The firmware modules log to stdout while they load; the page JSON follows the marker.
const pages = JSON.parse(pagesOutput.slice(pagesOutput.indexOf("@@pages@@") + 9));
const AP = "http://192.168.4.1";
const TYPES = { ".js": "application/javascript", ".css": "text/css" };
const ASSETS = ["/app.js", "/i18n.js", "/app.css", "/pages.js", "/base.css", "/pages.css"];
const configuration = { wifi_ssid: "Field", wifi_pass_set: true, wifi_networks: [], ntrip_enabled: true,
  ntrip_host: "caster.example", ntrip_port: 2101, ntrip_mount: "MOUNT", ntrip_user: "user", ntrip_pass_set: true,
  ntrip_fallback_host: "", ntrip_fallback_port: 2101, ntrip_fallback_mount: "", ntrip_fallback_user: "",
  ntrip_fallback_pass_set: false, staging: { revision: "c0ffee01-0", keys: [], restart_required: false } };
const status = { system: { display_name: "RTK-TEST", version: "VTEST", uptime_sec: 120 },
  board: { fs_total_bytes: 1000000, fs_free_bytes: 600000, ram_free_bytes: 7000000, wifi_rssi: -55,
    mcu_temp_c: 42, cpu_hz: 240000000 },
  stats: { wifi_ssid_aktiv: "Field" }, fix: { qual: 1, fix_status_text: "GPS_FIX", sats: 12 } };
const live = { schema_version: 1,
  current_fix: { qual: 1, fix_status_text: "GPS_FIX", sats: 12, hdop: 0.8, lat: 50, lon: 8, alt: 100 },
  quality_streak: { gate_streak_sec: 20 }, measurement_progress: { active: false, collected: 0, total: 5 },
  board: status.board, capacity: { used: 31, limit: 5000, remaining: 4969, percent: 1, level: "ok", writable: true },
  active_project: null, active_line: null,
  system: { version: "VTEST", uptime_sec: 120, display_name: "RTK-TEST", boot_id: "c0ffee01" } };
// No configuration result of this device start (#23c): the configuration page shows no last-change notice.
const configResult = { schema_version: 2, status: "none", details: {}, uptime_sec: 0, boot_id: "c0ffee01", apply_id: null };
const networks = [{ ssid: "Barn", rssi: -50, secured: true }, { ssid: "Yard", rssi: -70, secured: false }];
// The connections page (#24-UI) shows device access beside the form: answers in the shape of web.py access_info() and /api/label.
const access = { schema_version: 2, device_id: "A1B2C3D4E5F6", display_name: "RTK-D4E5F6", access_state: "AP_SETUP",
  hostname: "rtk-d4e5f6.local", http_port: 80,
  nmea_tcp: { canonical_port: 10110, legacy_ports: [2947], authentication_required: true, authentication: "AUTH <stream_token>\\r\\n" },
  ble_nus: { name: "RTK-D4E5F6", service_uuid: "6e400001-b5a3-f393-e0a9-e50e24dcca9e", tx_uuid: "6e400003-b5a3-f393-e0a9-e50e24dcca9e",
    rx_uuid: "6e400002-b5a3-f393-e0a9-e50e24dcca9e", single_client: true, pairing_required: true, bonding: true, encrypted: true,
    mitm_protection: "passkey" } };
const label = { schema_version: 1, display_name: "RTK-D4E5F6", device_id: "A1B2C3D4E5F6", device_code: "ABCDEF123456",
  stream_token: "stream-token-1", ble_pin: "123456", hostname: "rtk-d4e5f6.local",
  wifi_qr: "WIFI:T:WPA;S:RTK-D4E5F6-SETUP;P:setup-pass;;", portal_qr: "http://rtk-d4e5f6.local/setup#code=ABCDEF123456" };
const json = (route, body, statusCode) => route.fulfill({ status: statusCode || 200, contentType: "application/json",
  body: JSON.stringify(body) });
const asset = (route, path) => route.fulfill({ contentType: TYPES[path.slice(path.lastIndexOf("."))],
  body: fs.readFileSync(root + path, "utf8") });
const SESSION_COOKIE = "rtk_session=t; Path=/";
const hasSession = request => /(^|;\s*)rtk_session=t(;|$)/.test(request.headers().cookie || "");

// A device model for one host: GET / answers 401 + login page without the cookie, POST /api/session sets it.
// seen collects every unmocked /api request and every sign-in body.
const device = (seen, extra) => async route => {
  const request = route.request(), url = new URL(request.url()), path = url.pathname, method = request.method();
  if (ASSETS.includes(path)) return asset(route, path);
  if (extra) { const handled = await extra(route, path, method); if (handled !== false) return; }
  if (path === "/api/session" && method === "POST") {
    seen.signIn.push(request.postDataJSON());
    return route.fulfill({ status: 200, contentType: "application/json",
      headers: { "Set-Cookie": SESSION_COOKIE }, body: '{"csrf_token":"c","redirect":"/"}' });
  }
  if (path === "/") {
    return hasSession(request) ? route.fulfill({ contentType: "text/html", body: appHtml })
      : route.fulfill({ status: 401, contentType: "text/html", body: pages.login });
  }
  if (path === "/api/config") return json(route, configuration);
  if (path === "/api/config-result") return json(route, configResult);
  if (path === "/api/ui/live") return json(route, live);
  if (path === "/status") return json(route, status);
  if (path === "/api/tracking") return json(route, { projects: [], active_project_lines: [] });
  if (path === "/api/access") return json(route, access);
  if (path === "/api/label") return json(route, label);
  if (path === "/qr.js") return route.fulfill({ contentType: "text/javascript", body: fs.readFileSync(root + "/qr.js", "utf8") });
  // The live stream (#27) is refused like the firmware of an older release does; the app polls instead.
  if (path === "/api/events") return json(route, { error: "not_found" }, 404);
  if (path.startsWith("/api/")) { seen.unmocked.push(method + " " + path); return json(route, { error: "not_found" }, 404); }
  return route.fulfill({ status: 404, body: "" });
};
const newSeen = () => ({ signIn: [], unmocked: [] });

// Contrast of the fallback link against the sign-in panel, from the browser's computed colours.
const contrast = (page) => page.evaluate(() => {
  const canvas = document.createElement("canvas").getContext("2d", { willReadFrequently: true });
  const rgb = color => { canvas.clearRect(0, 0, 1, 1); canvas.fillStyle = "#000"; canvas.fillStyle = color;
    canvas.fillRect(0, 0, 1, 1); return Array.from(canvas.getImageData(0, 0, 1, 1).data).slice(0, 3); };
  const luminance = ([r, g, b]) => [r, g, b].map(value => { value /= 255;
    return value <= 0.03928 ? value / 12.92 : Math.pow((value + 0.055) / 1.055, 2.4); })
    .reduce((sum, value, index) => sum + value * [0.2126, 0.7152, 0.0722][index], 0);
  const link = document.querySelector(".login-fallback a"), panel = document.querySelector(".login-panel");
  const light = Math.max(luminance(rgb(getComputedStyle(link).color)), luminance(rgb(getComputedStyle(panel).backgroundColor))),
    dark = Math.min(luminance(rgb(getComputedStyle(link).color)), luminance(rgb(getComputedStyle(panel).backgroundColor)));
  return (light + 0.05) / (dark + 0.05);
});

(async () => {
  const browser = await chromium.launch({ headless: true });
  const errorsOf = page => { const list = []; page.on("pageerror", error => list.push(error.message)); return list; };
  try {
    // Part A: the captive web view opens the field app configuration, signs in and lands there.
    for (const language of ["de", "en"]) {
      const v2de = i18n.v2de;
      for (const width of [320, 360, 390, 480, 1280]) {
        const label = language + "/" + width;
        const context = await browser.newContext({ viewport: { width, height: 800 } });
        await context.addInitScript(value => localStorage.setItem("rtk-language", value), language);
        const page = await context.newPage(), errors = errorsOf(page), seen = newSeen();
        await page.route(AP + "/**", device(seen));
        await page.goto(AP + "/#device/configuration");
        const link = page.locator(".login-fallback a");
        await link.waitFor({ state: "visible" });
        assert.strictEqual(await link.getAttribute("href"), "/setup", label + ": fallback link target");
        const wanted = "Basic setup page (fallback)";
        assert.strictEqual((await link.textContent()).trim(), language === "de" ? v2de[wanted] : wanted, label + ": fallback link text");
        const box = await link.boundingBox(), panel = await page.locator(".login-panel").boundingBox(),
          submit = await page.locator("button[type=submit]").boundingBox();
        assert(box.height >= 48, label + ": fallback link is at least 48 px high, got " + box.height);
        assert(box.x >= panel.x - 0.5 && box.y >= panel.y - 0.5 && box.x + box.width <= panel.x + panel.width + 0.5
          && box.y + box.height <= panel.y + panel.height + 0.5, label + ": link lies inside the sign-in panel");
        assert(box.y >= submit.y + submit.height - 0.5, label + ": link sits below the submit button");
        if (width === 1280) assert(box.y + box.height <= 800, label + ": link is visible without scrolling");
        const ratio = await contrast(page);
        assert(ratio >= 4.5, label + ": link contrast " + ratio.toFixed(2));
        let focused = false;
        for (let step = 0; step < 6 && !focused; step++) {
          await page.keyboard.press("Tab");
          focused = await page.evaluate(() => document.activeElement === document.querySelector(".login-fallback a"));
        }
        assert(focused, label + ": link is reachable with Tab");
        assert.notStrictEqual(await link.evaluate(element => getComputedStyle(element).outlineStyle), "none", label + ": focus is visible");
        assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), label + ": no horizontal scrolling");
        await page.locator("#v2-device-code").fill("ABC");
        await page.locator("button[type=submit]").click();
        // The captive entry keeps its old link; the app replaces it by the canonical route in place.
        await page.waitForURL(url => /^\?signed_in=1$/.test(new URL(url).search) && ["#device/configuration", "#device/connections/internet"].includes(new URL(url).hash));
        await page.locator(".configuration-layout").waitFor({ state: "visible" });
        assert.strictEqual(await page.evaluate(() => location.hash), "#device/connections/internet", label + ": canonical route");
        assert.deepStrictEqual(seen.signIn.map(item => item.device_code), ["ABC"], label + ": one sign-in with the typed code");
        assert.deepStrictEqual(errors, [], label + ": page errors");
        assert.deepStrictEqual(seen.unmocked, [], label + ": every API request is mocked");
        await context.close();
      }
    }

    // Part B: the fallback page marks itself and its scan waits for the pending answer, then gives up on a silent one.
    {
      const de = i18n.dictionaries.de;
      const context = await browser.newContext({ viewport: { width: 360, height: 800 } });
      await context.addInitScript(() => localStorage.setItem("rtk-language", "de"));
      const page = await context.newPage(), errors = errorsOf(page), seen = newSeen(), held = [];
      let scanCalls = 0, mode = "pending";
      await page.clock.install();
      await page.route(AP + "/**", device(seen, async (route, path) => {
        if (path === "/setup") { await route.fulfill({ contentType: "text/html", body: pages.setup }); return true; }
        if (path === "/api/wifi-scan") {
          scanCalls += 1;
          if (mode === "silent") { held.push(route); return true; }
          if (scanCalls <= 2) await json(route, { status: "pending" }, 202);
          else await json(route, { networks });
          return true;
        }
        return false;
      }));
      await context.addCookies([{ name: "rtk_session", value: "t", url: AP + "/" }]);
      await page.goto(AP + "/setup");
      assert.strictEqual(await page.locator(".fallback-notice a").getAttribute("href"), "/#device/configuration");
      assert.strictEqual((await page.locator("#wifi-scan-state").textContent()).trim(), "");
      await page.locator("#wifi-scan").click();
      for (let step = 0; step < 40 && await page.locator("#wifi-results option").count() < 3; step++) {
        if (scanCalls === 2) {
          assert.strictEqual(await page.locator("#wifi-scan-state").textContent(), de["ui.searching"], "state while the second request is pending");
        }
        await page.clock.runFor(800);
        await page.waitForTimeout(40);
      }
      assert.strictEqual(scanCalls, 3, "two pending answers, then the result");
      assert.strictEqual(await page.locator("#wifi-results option").count(), 3);
      assert(await page.locator("#wifi-results").isVisible(), "result list is visible");
      assert.strictEqual(await page.locator(".network-help").textContent(), de["ui.network_format_help"]);
      // A request that never gets an answer ends with the deadline, keeps the list and unlocks the button.
      mode = "silent";
      await page.locator("#wifi-scan").click();
      for (let step = 0; step < 40 && held.length === 0; step++) await page.waitForTimeout(25);
      assert.strictEqual(held.length, 1, "the silent scan request reached the mock");
      await page.clock.runFor(8000);
      await page.waitForFunction(text => document.getElementById("wifi-scan-state").textContent === text,
        de["ui.scan_failed"] + " " + de["ui.scan_no_answer"]);
      assert.strictEqual(await page.locator("#wifi-scan").isDisabled(), false, "scan button is usable again");
      assert.strictEqual(await page.locator("#wifi-results option").count(), 3, "earlier results stay");
      assert.deepStrictEqual(errors, [], "legacy page errors");
      await Promise.all(held.map(route => route.abort().catch(() => {})));
      await context.close();
    }

    // Part C: the portal QR opens the field app and signs in without leaving the code in the address.
    {
      const context = await browser.newContext({ viewport: { width: 390, height: 800 } });
      const page = await context.newPage(), errors = errorsOf(page), seen = newSeen();
      await page.route("http://rtk-d4e5f6.local/**", device(seen));
      await page.goto("http://rtk-d4e5f6.local/#code=ABC");
      await page.waitForURL(url => new URL(url).search === "?signed_in=1" && ["#status", "#measure"].includes(new URL(url).hash));
      await page.waitForFunction(() => location.hash === "#measure");
      assert.deepStrictEqual(seen.signIn.map(item => item.device_code), ["ABC"], "QR code is sent once");
      assert(!page.url().includes("code="), "the code is gone from the address");
      assert.deepStrictEqual(errors, [], "QR page errors");
      await context.close();
    }

    // Part D: without JavaScript the fallback link leads to the legacy sign-in and on to /setup. Playwright does not
    // follow a redirect that route.fulfill() answers, so this part talks to a real local server for the 303.
    {
      const forms = [];
      const server = http.createServer((request, response) => {
        const path = request.url.split("?")[0], signedIn = /(^|;\s*)rtk_session=t(;|$)/.test(request.headers.cookie || "");
        const send = (code, type, body, headers) => { response.writeHead(code, { "Content-Type": type, ...headers }); response.end(body); };
        if (ASSETS.includes(path)) return send(200, TYPES[path.slice(path.lastIndexOf("."))], fs.readFileSync(root + path, "utf8"));
        if (path === "/") return send(401, "text/html", pages.login);
        if (path === "/setup") return signedIn ? send(200, "text/html", pages.setup) : send(401, "text/html", pages.legacyLogin);
        if (path === "/api/session" && request.method === "POST") {
          let body = "";
          request.on("data", chunk => { body += chunk; });
          return request.on("end", () => { forms.push(body); send(303, "text/plain", "", { Location: "/setup", "Set-Cookie": SESSION_COOKIE }); });
        }
        return send(404, "text/plain", "");
      });
      await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
      const origin = "http://127.0.0.1:" + server.address().port;
      try {
        const context = await browser.newContext({ viewport: { width: 360, height: 800 }, javaScriptEnabled: false });
        const page = await context.newPage();
        await page.goto(origin + "/#device/configuration");
        const link = page.locator(".login-fallback a");
        assert(await link.isVisible(), "fallback link is visible without JavaScript");
        await link.click();
        await page.waitForURL(origin + "/setup");
        assert(await page.locator("#login-device-code").isVisible(), "the legacy sign-in page opens");
        await page.locator("#login-device-code").fill("ABC");
        await page.locator(".login-submit").click();
        await page.locator("#setup-form").waitFor({ state: "visible" });
        assert.strictEqual(forms.length, 1, "one form post");
        const fields = new URLSearchParams(forms[0]);
        assert.strictEqual(fields.get("device_code"), "ABC");
        assert.strictEqual(fields.get("next"), "/setup");
        assert(await page.locator(".fallback-notice").isVisible(), "the fallback notice is shown after the sign-in");
        assert.strictEqual(new URL(page.url()).pathname, "/setup");
        await context.close();
      } finally { await new Promise(resolve => server.close(resolve)); }
    }
  } finally { await browser.close(); }
  console.log("captive-browser=ok");
})().catch(error => { console.error(error); process.exitCode = 1; });
