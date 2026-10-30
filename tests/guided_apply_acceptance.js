// SPDX-License-Identifier: AGPL-3.0-only
// Guided apply (#23c), part A: the configuration page checks changes before saving, saves without a restart,
// shows 400 answers at the field, keeps the dirty state honest and makes earlier staged changes visible.
// Part B: the verified restart after saving - hold, watch by boot id, sign-in, result and the ways out of the watch.
// Part C: restore and "Restart device" through the same watch, with the warning during a measurement.
// Part D: first setup from the captive portal to the field setup, the QR entry and the layout of every new element.
// The device model below answers page.route() requests or a local server on 127.0.0.1; it never talks to a real address.
// Run through tests/browser_acceptance.py.
const { chromium } = require("playwright");
const fs = require("fs"),
  assert = require("assert"),
  childProcess = require("child_process"),
  http = require("http"),
  root = process.cwd();
const i18n = require(root + "/i18n.js");
const appHtml = fs.readFileSync(process.env.RTK_APP_HTML || "/tmp/app-v2-local.html", "utf8");
// The sign-in page the device serves without a session (ui.APP_V2_LOGIN), from the repository.
const loginHtml = childProcess.execFileSync("python3", ["-c", "import sys\nsys.path.insert(0, 'tests')\nimport ui\nsys.stdout.write(ui.APP_V2_LOGIN)"],
  { cwd: root, encoding: "utf8" });
const ORIGIN = "http://rtk-test.invalid";
const TYPES = { ".js": "application/javascript", ".css": "text/css" };
// /qr.js: the connections page (#24-UI) draws the access QR codes beside the form.
const ASSETS = ["/app.js", "/i18n.js", "/app.css", "/pages.js", "/base.css", "/pages.css", "/qr.js"];
const text = (language, key) => i18n.dictionaries[language][key];
const literal = (language, value) => language === "de" ? i18n.v2de[value] : value;
const clone = value => JSON.parse(JSON.stringify(value));
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

const FIELD = { wifi_ssid: "Field", wifi_pass_set: true, wifi_networks: [{ ssid: "Fallback", password_set: true }],
  ntrip_enabled: true, ntrip_host: "caster.example", ntrip_port: 2101, ntrip_mount: "MOUNT", ntrip_user: "user",
  ntrip_pass_set: true, ntrip_fallback_host: "", ntrip_fallback_port: 2101, ntrip_fallback_mount: "",
  ntrip_fallback_user: "", ntrip_fallback_pass_set: false };
const FACTORY = { wifi_ssid: "", wifi_pass_set: false, wifi_networks: [], ntrip_enabled: true, ntrip_host: "",
  ntrip_port: 2101, ntrip_mount: "", ntrip_user: "", ntrip_pass_set: false, ntrip_fallback_host: "",
  ntrip_fallback_port: 2101, ntrip_fallback_mount: "", ntrip_fallback_user: "", ntrip_fallback_pass_set: false };
const REBOOT_KEYS = ["wifi_ssid", "wifi_pass", "wifi_networks"];
const SECRET_FLAGS = { wifi_pass: "wifi_pass_set", ntrip_pass: "ntrip_pass_set", ntrip_fallback_pass: "ntrip_fallback_pass_set" };

// The device: configuration, staging with revision (rules of cfg.stage_config), live snapshot, sessions, restarts with a
// new boot id, the configuration result and request records. deviceAnswer() is transport independent; routeAdapter()
// serves it through page.route, serverAdapter() through a local HTTP server (only B18 needs a body sent after the head).
function newDevice(options = {}) {
  const device = Object.assign({ config: clone(FIELD), bootId: "a1b2c3d4", staged: {}, stagingSerial: 0, restartFlag: false,
    puts: [], putDelayMs: 0, applyCalls: [], configGets: 0, failConfigGets: 0, withoutStaging: false, putAnswer: null,
    ntripState: "streaming", ntripTimer: null, unmocked: [],
    auth: false, sessionValid: true, down: false, neverBack: false, holdWindow: false, loginLoads: 0, signIns: 0,
    authedDuringHold: 0, requests: [], refused: [], probes: [], liveCalls: 0, rebootCount: 0, bootAt: Date.now() - 120000,
    applyMode: "ok", applyAnswer: null, restartOnApply: true, downAfterMs: 1000, downMs: 3000, pendingMs: 2000,
    loadingMs: 1000, accessAfter: "ONLINE", ntripAfter: "streaming", ntripConfigAfter: "ready", configWarnings: null,
    outcome: { status: "connected" }, measuring: false, activeLine: null, failLiveNext: false, failLiveAfterPut: false,
    holdLiveNext: false, heldLive: null, releaseHeld: null, holdStatusNext: false, heldStatus: null, releaseHeldStatus: null,
    restoreCalls: [], rebootCalls: [], restoreSections: ["connections", "measurement", "field_setup"], restoreKept: [],
    restoreAnswer: null, rebootAnswer: null, timers: [], headerSentMs: null, bodySentMs: null,
    hostname: "rtk-test.local", accessBefore: "ONLINE", signInBodies: [] }, options);
  device.result = options.result || { schema_version: 2, status: "none", details: {}, uptime_sec: 0, boot_id: device.bootId, apply_id: null };
  device.later = (ms, fn) => { device.timers.push(setTimeout(fn, ms)); };
  device.revision = () => device.bootId + "-" + device.stagingSerial;
  device.restartRequired = () => Object.keys(device.staged).length > 0 || device.restartFlag;
  device.maskedNetworks = list => list.map(item => ({ ssid: item.ssid,
    password_set: typeof item.password === "string" ? item.password !== "" : Boolean(item.password_set) }));
  device.sameAsCurrent = (key, value) => key === "wifi_ssid" ? device.config.wifi_ssid === value
    : key === "wifi_networks" ? !value.some(item => typeof item.password === "string")
      && JSON.stringify(device.maskedNetworks(value)) === JSON.stringify(device.config.wifi_networks) : false;
  device.stage = body => {
    const before = JSON.stringify(device.staged), applied = [];
    for (const [key, value] of Object.entries(body)) {
      if (REBOOT_KEYS.includes(key)) {
        if (device.sameAsCurrent(key, value)) delete device.staged[key]; else device.staged[key] = value;
      } else {
        applied.push(key);
        if (SECRET_FLAGS[key]) device.config[SECRET_FLAGS[key]] = true; else device.config[key] = value;
      }
    }
    if (JSON.stringify(device.staged) !== before) device.stagingSerial += 1;
    if (applied.some(key => key.startsWith("ntrip_"))) {
      device.ntripState = "connecting";
      clearTimeout(device.ntripTimer);
      device.ntripTimer = setTimeout(() => { device.ntripState = "streaming"; }, 3000);
    }
    return { applied: applied.sort(), staged: Object.keys(device.staged).sort(), pending: Object.keys(device.staged).length > 0,
      restart_required: device.restartRequired(), staging_revision: device.revision() };
  };
  device.configBody = () => {
    const body = clone(device.config);
    if (!device.withoutStaging) body.staging = { revision: device.revision(), keys: Object.keys(device.staged).sort(),
      restart_required: device.restartRequired() };
    return body;
  };
  // A new boot: the staged Wi-Fi values take effect only with a connected outcome; sessions and staging are gone.
  device.boot = applyId => {
    device.down = false; device.sessionValid = false; device.rebootCount += 1;
    device.bootId = "b000000" + device.rebootCount; device.stagingSerial = 0; device.restartFlag = false;
    const outcome = device.outcome;
    if (applyId !== undefined && outcome.status === "connected") {
      if ("wifi_ssid" in device.staged) device.config.wifi_ssid = device.staged.wifi_ssid;
      if ("wifi_pass" in device.staged) device.config.wifi_pass_set = true;
      if ("wifi_networks" in device.staged) device.config.wifi_networks = device.maskedNetworks(device.staged.wifi_networks);
    }
    device.staged = {}; device.bootAt = Date.now();
    if (applyId === undefined) return;
    device.result = { schema_version: 2, status: "pending", details: {}, uptime_sec: 3, boot_id: device.bootId, apply_id: applyId };
    device.later(device.pendingMs, () => {
      const details = Object.assign(outcome.status === "connected" ? { ssid: device.config.wifi_ssid } : {}, outcome.details || {});
      device.result = { schema_version: 2, status: outcome.status, details, uptime_sec: 5, boot_id: device.bootId,
        apply_id: outcome.apply_id !== undefined ? outcome.apply_id : applyId };
    });
  };
  // reboot(): down after downAfterMs for downMs, then a new boot. From going down until the next sign-in the window counts
  // every request that carries the session cookie (refused ones too), except the session probe and the order itself.
  device.reboot = applyId => {
    device.later(device.downAfterMs, () => { device.down = true; device.holdWindow = true; });
    if (!device.neverBack) device.later(device.downAfterMs + device.downMs, () => device.boot(applyId));
  };
  // The device restarted before the page loaded (resume scenarios): new boot id, a final result after pendingMs.
  device.booted = applyId => { device.staged = device.staged || {}; device.boot(applyId); device.sessionValid = true; device.holdWindow = false; };
  device.live = () => {
    const since = Date.now() - device.bootAt, booted = device.rebootCount > 0;
    const live = { schema_version: 1,
      current_fix: { qual: 1, fix_status_text: "GPS_FIX", sats: 12, hdop: 0.8, lat: 50, lon: 8, alt: 100 },
      quality_streak: { gate_streak_sec: 20 }, measurement_progress: { active: device.measuring, collected: 0, total: 5 },
      board: { fs_total_bytes: 1000000, fs_free_bytes: 600000, ram_free_bytes: 7000000, wifi_rssi: -55, mcu_temp_c: 42, cpu_hz: 240000000 },
      capacity: { used: 31, limit: 5000, remaining: 4969, percent: 1, level: "ok", writable: true },
      active_project: null, active_line: device.activeLine,
      system: { boot_id: device.bootId, uptime_sec: booted ? 3 + Math.floor(since / 1000) : 120, version: "VTEST", display_name: "RTK-TEST" },
      network: { access_state: booted ? (typeof device.accessAfter === "function" ? device.accessAfter(since) : device.accessAfter) : device.accessBefore,
        ntrip_state: booted ? device.ntripAfter : device.ntripState, ntrip_config: booted ? (typeof device.ntripConfigAfter === "function" ? device.ntripConfigAfter(since) : device.ntripConfigAfter) : "ready" },
      tracking_startup: { state: booted && since < device.loadingMs ? "loading" : "ready" } };
    if (live.network.access_state === undefined) delete live.network.access_state;
    if (booted && device.configWarnings) live.config_warnings = device.configWarnings;
    return live;
  };
  device.handler = routeAdapter(device);
  return device;
}
const ORDERS = ["/api/config/apply", "/api/config/restore", "/api/reboot"];
const json = (body, status = 200, headers = {}) => ({ status, headers: Object.assign({ "content-type": "application/json" }, headers), body: JSON.stringify(body) });
// The answer to an accepted order, as applyMode wants it: whole, lost, cut, or with the body after the head (server only).
function orderAnswer(device, text, cut = '{"applying": tr') {
  if (device.applyMode === "lost") return "refuse";
  if (device.applyMode === "truncated") return { status: 202, headers: { "content-type": "application/json" }, body: cut };
  if (device.applyMode === "slow_body") return { status: 202, headers: { "content-type": "application/json" }, bodyLater: sleep(5000).then(() => text) };
  if (device.applyMode === "slow_body_destroyed") return { status: 202, headers: { "content-type": "application/json" }, bodyLater: sleep(1000).then(() => "destroy") };
  return { status: 202, headers: { "content-type": "application/json" }, body: text };
}
async function deviceAnswer(device, { method, path, query, body, raw, cookie }) {
  const hasCookie = /(^|;\s*)rtk_session=t(;|$)/.test(cookie || ""), api = path.startsWith("/api/") || path === "/status";
  if (device.holdWindow && hasCookie && api && path !== "/api/session" && !(method === "POST" && ORDERS.includes(path))) device.authedDuringHold += 1;
  if (device.down) { device.refused.push(method + " " + path); return "refuse"; }
  if (ASSETS.includes(path)) return { status: 200, headers: { "content-type": TYPES[path.slice(path.lastIndexOf("."))] }, body: fs.readFileSync(root + path, "utf8") };
  const authed = !device.auth || hasCookie && device.sessionValid;
  device.requests.push({ method, path, at: Date.now(), authed });
  if (path === "/") {
    if (authed) return { status: 200, headers: { "content-type": "text/html" }, body: appHtml };
    device.loginLoads += 1;
    return { status: 401, headers: { "content-type": "text/html" }, body: loginHtml };
  }
  if (path === "/api/session" && method === "POST") {
    device.sessionValid = true; device.holdWindow = false; device.signIns += 1; device.signInBodies.push(body);
    return json({ csrf_token: "c", redirect: "/" }, 200, { "set-cookie": "rtk_session=t; Path=/" });
  }
  if (path === "/api/session" && method === "GET") {
    device.probes.push(Date.now());
    return authed ? json({ authenticated: true, csrf_token: "c" }) : json({ authenticated: false }, 401);
  }
  if (!authed && api) return json({ error: "unauthorized", message: "Sign in required." }, 401);
  if (path === "/api/config" && method === "GET") {
    device.configGets += 1;
    if (device.failConfigGets > 0) { device.failConfigGets -= 1; return json({ error: "internal", message: "Failed" }, 500); }
    return json(device.configBody());
  }
  if (path === "/api/config" && method === "PUT") {
    device.puts.push({ body, query });
    if (device.putDelayMs) await sleep(device.putDelayMs);
    if (device.failLiveAfterPut) device.failLiveNext = true;
    if (device.putAnswer) { const answer = device.putAnswer(body, query); if (answer) return json(answer.body, answer.status); }
    const asked = new URLSearchParams(query || "").get("staging_revision");
    if (asked !== null && asked !== device.revision()) {
      return json({ error: "staging_changed", message: "The changes waiting in the device memory changed. Check them again." }, 409);
    }
    return json(device.stage(body));
  }
  if (path === "/api/config/apply" && method === "POST") {
    device.applyCalls.push(body);
    if (device.applyAnswer) { const answer = device.applyAnswer(body, device.applyCalls.length); if (answer) return json(answer.body, answer.status); }
    if (body && body.staging_revision !== undefined && body.staging_revision !== device.revision()) {
      return json({ error: "staging_changed", message: "The changes waiting in the device memory changed. Check them again." }, 409);
    }
    if (!device.restartRequired()) return json({ error: "nothing_to_apply", message: "There is no staged change to apply." }, 409);
    const id = body && body.apply_id;
    if (device.restartOnApply) device.reboot(id);
    return orderAnswer(device, JSON.stringify({ applying: true, apply_id: id }));
  }
  if (path === "/api/config/restore" && method === "POST") {
    const id = new URLSearchParams(query || "").get("apply_id");
    device.restoreCalls.push({ query, raw, id });
    if (device.restoreAnswer) { const answer = device.restoreAnswer(raw, id); if (answer) return answer.text !== undefined
      ? { status: answer.status, headers: { "content-type": "text/plain" }, body: answer.text } : json(answer.body, answer.status); }
    if (device.restartOnApply) device.reboot(id);
    return orderAnswer(device, JSON.stringify({ restored: true, schema_version: 2, sections: device.restoreSections, kept: device.restoreKept, apply_id: id }),
      '{"restored": true, "sch');
  }
  if (path === "/api/reboot" && method === "POST") {
    device.rebootCalls.push(body);
    if (device.rebootAnswer) { const answer = device.rebootAnswer(body); if (answer) return json(answer.body, answer.status); }
    if (device.restartOnApply) device.reboot(undefined);
    return orderAnswer(device, JSON.stringify({ restarting: true, apply_id: body && body.apply_id }), '{"restarting": tr');
  }
  if (path === "/api/config-result") return json(device.result);
  if (path === "/api/wifi-scan") return json({ status: "ready", networks: [{ ssid: "Visible", rssi: -48 }] });
  if (path === "/api/ui/live") {
    device.liveCalls += 1;
    if (device.failLiveNext) { device.failLiveNext = false; return json({ error: "internal", message: "Failed" }, 500); }
    if (device.holdLiveNext) {
      device.holdLiveNext = false;
      device.heldLive = Date.now();
      return new Promise(resolve => { device.releaseHeld = status => resolve(status === 401 ? json({ error: "unauthorized" }, 401) : json(device.live())); });
    }
    return json(device.live());
  }
  if (path === "/api/access") return json({ hostname: device.hostname });
  if (path === "/api/label") return json({ schema_version: 1, display_name: "RTK-TEST", device_code: "ABCDEF123456", stream_token: "token",
    ble_pin: "123456", hostname: device.hostname, wifi_qr: "WIFI:T:WPA;S:RTK-TEST-SETUP;P:setup-pass;;" });
  if (path === "/status" && device.holdStatusNext) {
    device.holdStatusNext = false;
    device.heldStatus = Date.now();
    return new Promise(resolve => { device.releaseHeldStatus = code => resolve(code === 401 ? json({ error: "unauthorized" }, 401) : json({ system: { display_name: "RTK-TEST", version: "VTEST", uptime_sec: 120 } })); });
  }
  if (path === "/status") return json({ system: { display_name: "RTK-TEST", version: "VTEST", uptime_sec: 120 },
    board: device.live().board, stats: { wifi_ssid_aktiv: "Field" }, fix: { qual: 1, fix_status_text: "GPS_FIX", sats: 12 } });
  if (path === "/gnss" && method === "GET") return { status: 200, headers: { "content-type": "text/plain" }, body: "$GNTXT,ok*00" };
  if (path === "/api/tracking") return json({ projects: [], active_project_lines: [] });
  if (path === "/api/field-setup") return json({ values: { antenna_height_m: 2, reference_frame: "", reference_frame_epoch: "" },
    defaults: { antenna_height_m: 1.8, reference_frame: "", reference_frame_epoch: "" } });
  if (path === "/api/measurement-gate") return json({ deviations: [], values: {}, defaults: {} });
  // The live stream (#27) is refused like the firmware of an older release does; the app polls instead.
  if (path === "/api/events") return json({ error: "not_found" }, 404);
  if (api) { device.unmocked.push(method + " " + path); return json({ error: "not_found" }, 404); }
  return { status: 404, headers: {}, body: "" };
}
function routeAdapter(device) {
  return async route => {
    const request = route.request(), url = new URL(request.url()), raw = request.postData();
    let body = null;
    if (raw) { try { body = JSON.parse(raw); } catch (_) { body = raw; } }
    const answer = await deviceAnswer(device, { method: request.method(), path: url.pathname, query: url.search.replace(/^\?/, ""),
      body, raw, cookie: request.headers().cookie || "" });
    if (answer === "refuse") return route.abort("connectionrefused").catch(() => {});
    if (answer.bodyLater) throw new Error("a body after the head needs the server adapter");
    return route.fulfill({ status: answer.status, headers: answer.headers, body: answer.body }).catch(() => {});
  };
}
// The same device behind a local HTTP server; a body after the head is flushed as its own write.
async function serverAdapter(device) {
  const sockets = new Set();
  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on("data", chunk => chunks.push(chunk));
    req.on("end", async () => {
      const url = new URL(req.url, "http://127.0.0.1"), raw = Buffer.concat(chunks).toString("utf8");
      let body = null;
      if (raw) { try { body = JSON.parse(raw); } catch (_) { body = raw; } }
      const answer = await deviceAnswer(device, { method: req.method, path: url.pathname, query: url.search.replace(/^\?/, ""),
        body, raw, cookie: req.headers.cookie || "" });
      if (answer === "refuse") { req.socket.destroy(); return; }
      res.writeHead(answer.status, Object.assign({ "cache-control": "no-store" }, answer.headers));
      if (!answer.bodyLater) { res.end(answer.body); return; }
      res.flushHeaders();
      device.headerSentMs = Date.now();
      const later = await answer.bodyLater;
      if (later === "destroy") { res.socket.destroy(); return; }
      device.bodySentMs = Date.now();
      res.end(later);
    });
  });
  server.on("connection", socket => { sockets.add(socket); socket.on("close", () => sockets.delete(socket)); });
  await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
  return { origin: "http://127.0.0.1:" + server.address().port,
    close: () => new Promise(resolve => { for (const socket of sockets) socket.destroy(); server.close(() => resolve()); }) };
}

// A page on the configuration route; beforeunload dialogs are counted and dismissed (the page stays).
// auth: the browser holds the session cookie; init: a script for every document (before the app); wait: for the form
// (or the selector it names).
async function openPage(browser, device, { language = "en", width = 390, origin = ORIGIN, route = true, auth = device.auth,
  init = null, clock = false, wait = true, hash = "#device/configuration" } = {}) {
  const context = await browser.newContext({ viewport: { width, height: 900 } });
  await context.addInitScript(value => localStorage.setItem("rtk-language", value), language);
  if (init) await context.addInitScript(init.fn, init.arg);
  if (auth) await context.addCookies([{ name: "rtk_session", value: "t", url: origin }]);
  const page = await context.newPage(), errors = [], counts = { beforeunload: 0 };
  page.on("pageerror", error => errors.push(error.message));
  page.on("dialog", dialog => { if (dialog.type() === "beforeunload") counts.beforeunload += 1; return dialog.dismiss(); });
  if (route) await page.route(origin + "/**", device.handler);
  if (clock) await page.clock.install();
  await page.goto(origin + "/" + hash);
  if (wait) await page.locator(typeof wait === "string" ? wait : "form.configuration-layout").waitFor();
  return { context, page, errors, counts, language, origin };
}
const finish = async (view, device, label) => {
  for (const timer of device.timers) clearTimeout(timer);
  clearTimeout(device.ntripTimer);
  assert.deepStrictEqual(view.errors, [], label + ": page errors");
  assert.deepStrictEqual(device.unmocked, [], label + ": every API request is mocked");
  await view.context.close();
};
const dirty = page => page.locator("form.configuration-layout").getAttribute("data-dirty");
const status = page => page.locator("form.configuration-layout p.notice[role=status]");
const button = (page, name) => page.getByRole("button", { name, exact: true });
const statusText = async page => (await status(page).textContent()).trim();
const waitStatus = (page, wanted, timeout = 2000) => page.waitForFunction(value =>
  document.querySelector("form.configuration-layout p.notice[role=status]").textContent.includes(value), wanted, { timeout });
const check = (view) => button(view.page, text(view.language, "ui.guided_check_changes"));

// A1: dirty follows the difference to the saved values; the link guard and the marker follow it.
async function a1Dirty(browser, language) {
  const device = newDevice(), view = await openPage(browser, device, { language }), page = view.page, label = "A1 " + language;
  assert.strictEqual(await dirty(page), "false", label + ": clean on open");
  assert.strictEqual(await page.locator(".unsaved-marker").isVisible(), false, label + ": marker hidden");
  await page.locator('input[name="ntrip_mount"]').fill("OTHER");
  assert.strictEqual(await dirty(page), "true", label + ": edit marks dirty");
  assert.strictEqual(await page.locator(".unsaved-marker").isVisible(), true, label + ": marker visible");
  assert.strictEqual((await page.locator(".unsaved-marker").textContent()).trim(), text(language, "ui.guided_unsaved"));
  await page.locator('input[name="ntrip_mount"]').fill("MOUNT");
  assert.strictEqual(await dirty(page), "false", label + ": back to the saved value is clean");
  assert.strictEqual(await page.locator(".unsaved-marker").isVisible(), false, label + ": marker hidden again");
  await button(page, literal(language, "Scan networks")).click();
  await page.waitForSelector("dialog[open]");
  await button(page, literal(language, "Use network")).click();
  await page.waitForFunction(() => document.querySelector('input[name="wifi_ssid"]').value === "Visible");
  assert.strictEqual(await dirty(page), "true", label + ": a scanned network marks dirty");
  await page.locator('a[href="/#device"]:visible').first().click();
  await page.waitForSelector("dialog[open]");
  assert.strictEqual((await page.locator("dialog[open] h2").textContent()).trim(), literal(language, "Discard unsaved changes?"));
  await button(page, text(language, "action.cancel")).click();
  await page.waitForFunction(() => !document.querySelector("dialog[open]"));
  assert.strictEqual(await page.evaluate(() => location.hash), CONFIG_HASH, label + ": still on the page");
  assert.strictEqual(await dirty(page), "true", label + ": cancel keeps the edit");
  await finish(view, device, label);
}

// A1b: the harness sees beforeunload, which the other scenarios rely on.
async function a1bBeforeUnload(browser) {
  const device = newDevice(), view = await openPage(browser, device), page = view.page;
  await page.evaluate(() => { window.__still_here = true; });
  await page.locator('input[name="ntrip_mount"]').click();
  await page.locator('input[name="ntrip_mount"]').fill("OTHER");
  assert.strictEqual(await dirty(page), "true");
  await page.evaluate(() => { setTimeout(() => location.reload(), 0); });
  for (let step = 0; step < 40 && view.counts.beforeunload === 0; step++) await sleep(50);
  assert.strictEqual(view.counts.beforeunload, 1, "A1b: the unsaved form asks before the page reloads");
  assert.strictEqual(await page.locator('input[name="ntrip_mount"]').inputValue(), "OTHER", "A1b: the page and the input stay");
  assert.strictEqual(await page.evaluate(() => window.__still_here), true, "A1b: no reload happened");
  await page.locator('input[name="ntrip_mount"]').fill("MOUNT");
  await page.evaluate(() => { window.__still_here = true; });
  await page.reload();
  await page.locator("form.configuration-layout").waitFor();
  assert.strictEqual(view.counts.beforeunload, 1, "A1b: a clean form reloads without a question");
  assert.strictEqual(await page.evaluate(() => window.__still_here), undefined, "A1b: the clean page really reloaded");
  await finish(view, device, "A1b");
}

// A2: factory state; the review names the Wi-Fi fields only; a half-filled caster blocks the submit.
async function a2Factory(browser) {
  const device = newDevice({ config: clone(FACTORY) }), view = await openPage(browser, device), page = view.page;
  await page.locator('input[name="wifi_ssid"]').fill("Barn");
  await page.locator('input[name="wifi_pass"]').fill("secret123");
  await check(view).click();
  const review = page.locator('.config-review[data-restart="true"]');
  await review.waitFor({ state: "visible" });
  const reviewText = await review.textContent();
  assert(reviewText.includes("Wi-Fi name (SSID)") && reviewText.includes("Wi-Fi password"), "A2: the review names the Wi-Fi fields");
  assert(!/caster/i.test(reviewText), "A2: no caster field in the review");
  assert(!reviewText.includes("secret123") && !reviewText.includes("Barn"), "A2: no value in the review");
  assert.strictEqual(await review.evaluate(node => getComputedStyle(node).position), "static", "A2: the review stays in the page flow");
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "A2: no horizontal scrolling");
  assert.strictEqual(device.puts.length, 0, "A2: nothing sent yet");
  await button(page, text("en", "ui.guided_back_to_editing")).click();
  await page.locator(".config-review").waitFor({ state: "hidden" });
  await page.locator('input[name="ntrip_host"]').fill("x");
  assert.strictEqual(await page.locator('input[name="ntrip_mount"]').evaluate(node => node.matches(":invalid")), true, "A2: the mountpoint becomes required");
  await check(view).click();
  await sleep(300);
  assert.strictEqual(device.puts.length, 0, "A2: the submit is blocked");
  assert.strictEqual(await page.locator(".config-review").isVisible(), false, "A2: no review for an invalid form");
  await finish(view, device, "A2");
}

// A3: no change, no request.
async function a3NoChange(browser) {
  const device = newDevice(), view = await openPage(browser, device), page = view.page;
  await check(view).click();
  await waitStatus(page, "No changes to save.");
  assert.strictEqual(device.puts.length, 0, "A3: no PUT");
  await finish(view, device, "A3");
}

// A4: a caster change takes effect at once; the NTRIP step is watched; the page is clean afterwards.
async function a4CasterOnly(browser, language) {
  const device = newDevice(), view = await openPage(browser, device, { language }), page = view.page, label = "A4 " + language;
  await page.locator('input[name="ntrip_mount"]').fill("OTHER");
  await check(view).click();
  const review = page.locator('.config-review[data-restart="false"]');
  await review.waitFor({ state: "visible" });
  assert((await review.textContent()).includes(text(language, "ui.guided_review_now")), label + ": 'takes effect at once' text");
  assert.strictEqual(await review.getByRole("button").count(), 2, label + ": Save and Back to editing");
  await button(page, text(language, "ui.guided_save")).click();
  await waitStatus(page, literal(language, "Configuration saved and in effect; no restart needed."));
  assert.deepStrictEqual(device.puts.map(item => item.body), [{ ntrip_mount: "OTHER" }], label + ": only the changed value is sent");
  assert.strictEqual(device.puts[0].query, "staging_revision=a1b2c3d4-0", label + ": the seen revision is sent");
  assert.strictEqual(device.applyCalls.length, 0, label + ": no apply");
  await page.locator('.config-step[data-step="ntrip"][data-state="ok"]').waitFor({ timeout: 9000 });
  assert.strictEqual(await dirty(page), "false", label + ": clean after the save");
  await page.locator('a[href="/#device"]:visible').first().click();
  await page.waitForFunction(() => location.hash === "#device");
  assert.strictEqual(await page.locator("dialog[open]").count(), 0, label + ": no discard question");
  await finish(view, device, label);
}

// A5: a 400 for a field is shown at the field; the next key press clears it.
async function a5FieldError(browser) {
  const message = "The configuration is invalid: ntrip_mount must not contain a slash.";
  const device = newDevice({ putAnswer: () => ({ status: 400, body: { error: "invalid_configuration", message } }) });
  const view = await openPage(browser, device), page = view.page;
  const mount = page.locator('input[name="ntrip_mount"]');
  await mount.fill("A/B");
  assert.strictEqual(await mount.evaluate(node => node.validity.patternMismatch), true, "A5: the browser itself rejects a slash in the mountpoint");
  await mount.evaluate(node => node.removeAttribute("pattern"));
  await check(view).click();
  await button(page, text("en", "ui.guided_save")).click();
  await page.locator('input[name="ntrip_mount"][aria-invalid="true"]').waitFor();
  assert((await page.locator("#ntrip_mount-error").textContent()).includes("must not contain a slash"), "A5: reason at the field");
  assert.strictEqual(await page.evaluate(() => document.activeElement && document.activeElement.name), "ntrip_mount", "A5: focus on the field");
  assert.strictEqual(await dirty(page), "true", "A5: still dirty");
  await page.keyboard.press("x");
  assert.strictEqual(await mount.getAttribute("aria-invalid"), null, "A5: typing clears the error");
  assert.strictEqual(await page.locator("#ntrip_mount-error").count(), 0, "A5: the message is gone");
  await finish(view, device, "A5");
}

// A6: masked fallback networks keep their password; a new one is sent in full.
async function a6MaskedNetworks(browser) {
  const device = newDevice(), view = await openPage(browser, device), page = view.page;
  const textarea = page.locator('textarea[name="wifi_networks"]');
  assert.strictEqual(await textarea.inputValue(), "Fallback", "A6: only the SSID is shown");
  await textarea.fill("Fallback\nSecond:pass12345");
  await check(view).click();
  await button(page, text("en", "ui.guided_save_later")).click();
  await waitStatus(page, text("en", "ui.guided_kept_memory"));
  assert.strictEqual(device.puts.length, 1);
  assert.deepStrictEqual(device.puts[0].body, { wifi_networks: [{ ssid: "Fallback", password_set: true }, { ssid: "Second", password: "pass12345" }] });
  assert(!("wifi_ssid" in device.puts[0].body), "A6: no wifi_ssid");
  await finish(view, device, "A6");
}

// A7: help text comes from the dictionaries; the backup left the form for Maintenance (#24-UI E19), the form keeps a link.
async function a7Texts(browser, language) {
  const device = newDevice(), view = await openPage(browser, device, { language }), page = view.page, label = "A7 " + language;
  assert.strictEqual((await page.locator('textarea[name="wifi_networks"] ~ small').textContent()).trim(), text(language, "ui.network_format_help"), label + ": network help");
  assert.strictEqual(await page.locator(".config-backup").count(), 0, label + ": no backup section in the form");
  assert.strictEqual(await page.locator('form.configuration-layout a[href="/api/config/backup"]').count(), 0, label + ": no download in the form");
  assert.strictEqual(await button(page, text(language, "ui.guided_backup_restore")).count(), 0, label + ": no restore in the form");
  const link = page.locator('.connections-backup-link a[href="/#device/maintenance"]');
  assert.strictEqual((await link.textContent()).trim(), text(language, "ui.connections_backup_link"), label + ": link to Maintenance");
  await link.click();
  const backup = page.locator('[data-card="config-backup"]');
  await backup.waitFor();
  assert.strictEqual(new URL(page.url()).hash, "#device/maintenance", label + ": on Maintenance");
  assert.strictEqual((await backup.locator(".kicker").textContent()).trim(), text(language, "ui.guided_backup_kicker"), label + ": kicker");
  assert.strictEqual((await backup.locator("h2").textContent()).trim(), text(language, "ui.maintenance_backup_title"), label + ": title");
  assert.deepStrictEqual((await backup.locator("p.muted").allTextContents()).map(item => item.trim()),
    [text(language, "ui.guided_backup_help"), text(language, "ui.maintenance_secrets"), text(language, "ui.maintenance_survey_hint")], label + ": scope, secrets, survey data");
  assert(text("en", "ui.guided_backup_help").includes("measurement rules"), label + ": the English scope names the measurement rules");
  assert.strictEqual(await backup.locator('a[href="/#projects"]').count(), 1, label + ": link to the survey data backup");
  assert.deepStrictEqual(await backup.locator(".btn").allTextContents(), [text(language, "ui.guided_backup_download"), text(language, "ui.guided_backup_restore")], label + ": buttons");
  assert.strictEqual(await backup.locator('a.btn[href="/api/config/backup"]').count(), 1, label + ": download link");
  const restart = page.locator('[data-card="restart"]');
  assert.strictEqual((await restart.locator("h2").textContent()).trim(), text(language, "ui.maintenance_restart_title"), label + ": restart title");
  assert.strictEqual((await restart.locator("p").textContent()).trim(), text(language, "ui.maintenance_restart_scope"), label + ": restart consequences");
  assert.deepStrictEqual(await restart.locator(".btn").allTextContents(), [text(language, "ui.device_restart")], label + ": restart button");
  await finish(view, device, label);
}

// A8: a saved password leaves an empty diff, also when the follow-up GET fails.
async function a8SavedPassword(browser, failingGet) {
  const device = newDevice(), view = await openPage(browser, device), page = view.page, label = "A8" + (failingGet ? " (GET fails)" : "");
  await page.locator('input[name="ntrip_pass"]').fill("newsecret1");
  await check(view).click();
  device.failConfigGets = failingGet ? 1 : 0;
  await button(page, text("en", "ui.guided_save")).click();
  await waitStatus(page, "Configuration saved and in effect; no restart needed.");
  assert.strictEqual(await page.locator('input[name="ntrip_pass"]').inputValue(), "", label + ": the password field is empty");
  assert.strictEqual(await dirty(page), "false", label + ": clean");
  if (!failingGet) {
    await check(view).click();
    await waitStatus(page, "No changes to save.");
    assert.strictEqual(device.puts.length, 1, label + ": nothing more sent");
  }
  await finish(view, device, label);
}

// A9: no typing while the request runs.
async function a9LockedWhileSaving(browser) {
  const device = newDevice({ putDelayMs: 2000 }), view = await openPage(browser, device), page = view.page;
  await page.locator('input[name="ntrip_mount"]').fill("OTHER");
  await check(view).click();
  await button(page, text("en", "ui.guided_save")).click();
  assert.strictEqual(await page.locator("fieldset.config-fields").evaluate(node => node.disabled), true, "A9: fields locked");
  assert.strictEqual(await page.locator('input[name="ntrip_host"]').isEditable(), false, "A9: input not editable");
  await waitStatus(page, "Configuration saved and in effect; no restart needed.", 6000);
  assert.strictEqual(await page.locator('input[name="ntrip_host"]').isEditable(), true, "A9: editable again");
  assert.strictEqual(await dirty(page), "false", "A9: clean");
  await finish(view, device, "A9");
}

// A10: saved fallback network passwords leave an empty diff, also a changed one.
async function a10FallbackPasswords(browser) {
  for (const [entered, shown, sent] of [
    ["Fallback\nSecond:pass12345", "Fallback\nSecond", [{ ssid: "Fallback", password_set: true }, { ssid: "Second", password: "pass12345" }]],
    ["Fallback:newpass1", "Fallback", [{ ssid: "Fallback", password: "newpass1" }]]]) {
    const device = newDevice(), view = await openPage(browser, device), page = view.page, label = "A10 " + entered.replace("\n", "|");
    await page.locator('textarea[name="wifi_networks"]').fill(entered);
    await check(view).click();
    await page.locator('.config-review[data-restart="true"]').waitFor();
    await button(page, text("en", "ui.guided_save_later")).click();
    await waitStatus(page, text("en", "ui.guided_kept_memory"));
    assert.deepStrictEqual(device.puts.map(item => item.body), [{ wifi_networks: sent }], label + ": request");
    assert.strictEqual(await page.locator('textarea[name="wifi_networks"]').inputValue(), shown, label + ": masked text");
    assert.strictEqual(await dirty(page), "false", label + ": clean");
    await check(view).click();
    await waitStatus(page, "No changes to save.");
    assert.strictEqual(device.puts.length, 1, label + ": nothing more sent");
    await finish(view, device, label);
  }
}

// A11: an earlier staged change is named when the page opens and with the next save; the revision goes with the PUT.
async function a11StagedOnOpen(browser) {
  const device = newDevice({ staged: { wifi_ssid: "Other" }, stagingSerial: 3 }), view = await openPage(browser, device), page = view.page;
  const staged = page.locator("section.config-staged");
  await staged.waitFor({ state: "visible" });
  assert.strictEqual(await staged.getAttribute("data-revision"), "a1b2c3d4-3");
  assert((await staged.textContent()).includes("Wi-Fi name (SSID)"), "A11: the staged key is named");
  assert(!(await page.content()).includes("Other"), "A11: the staged value is nowhere in the DOM");
  await page.locator('input[name="ntrip_mount"]').fill("OTHER");
  await check(view).click();
  const review = page.locator('.config-review[data-restart="true"]');
  await review.waitFor({ state: "visible" });
  const earlier = text("en", "ui.guided_review_earlier").split("{labels}")[0];
  assert((await review.textContent()).includes(earlier + "Wi-Fi name (SSID)"), "A11: listed as applied with this restart");
  await button(page, text("en", "ui.guided_save_later")).click();
  await waitStatus(page, text("en", "ui.guided_kept_memory"));
  assert.strictEqual(device.puts[0].query, "staging_revision=a1b2c3d4-3", "A11: the seen revision");
  assert.strictEqual(device.applyCalls.length, 0, "A11: nothing applied");
  await finish(view, device, "A11");
}

// A12: the staging changes between opening and saving: 409, nothing saved, review again with the new state.
async function a12StagingChanged(browser) {
  const device = newDevice({ staged: { wifi_ssid: "Other" }, stagingSerial: 3 }), view = await openPage(browser, device), page = view.page;
  device.staged.wifi_ssid = "Third";
  device.stagingSerial = 4;
  await page.locator('input[name="ntrip_mount"]').fill("OTHER");
  await check(view).click();
  await page.locator(".config-review").waitFor({ state: "visible" });
  const gets = device.configGets;
  await button(page, text("en", "ui.guided_save_later")).click();
  await waitStatus(page, text("en", "ui.guided_staging_changed"));
  assert.strictEqual(device.puts.length, 1);
  assert.strictEqual(device.puts[0].query, "staging_revision=a1b2c3d4-3");
  assert.strictEqual(device.config.ntrip_mount, "MOUNT", "A12: nothing was saved");
  assert.strictEqual(device.configGets, gets + 1, "A12: the state is read again");
  await page.locator('.config-review[data-restart="true"]').waitFor({ state: "visible" });
  assert.strictEqual(await page.locator('input[name="ntrip_mount"]').inputValue(), "OTHER", "A12: the form stays");
  assert.strictEqual(await dirty(page), "true");
  await button(page, text("en", "ui.guided_save_later")).click();
  await waitStatus(page, text("en", "ui.guided_kept_memory"));
  assert.deepStrictEqual(device.puts.map(item => item.query), ["staging_revision=a1b2c3d4-3", "staging_revision=a1b2c3d4-4"]);
  assert.strictEqual(device.config.ntrip_mount, "OTHER", "A12: saved with the new revision");
  await finish(view, device, "A12");
}

// A13: a device answer without a staging block (older firmware) opens and saves without a revision.
async function a13WithoutStaging(browser) {
  const device = newDevice({ withoutStaging: true }), view = await openPage(browser, device), page = view.page;
  assert.strictEqual(await page.locator("section.config-staged").count(), 0, "A13: no staged block");
  await page.locator('input[name="ntrip_mount"]').fill("OTHER");
  await check(view).click();
  await button(page, text("en", "ui.guided_save")).click();
  await waitStatus(page, "Configuration saved and in effect; no restart needed.");
  assert.strictEqual(device.puts[0].query, "", "A13: no revision in the request");
  await finish(view, device, "A13");
}

// ---- Part B: the verified restart after saving ----
const MARKER_KEY = "rtk-restart-watch", OWN_ID = "0123456789abcdef";
const panel = page => page.locator("section.restart-watch");
const panelText = async page => ((await panel(page).count()) ? await panel(page).textContent() : "") || "";
const step = (page, name, state) => page.locator(`section.restart-watch li[data-step="${name}"][data-state="${state}"]`);
const marker = page => page.evaluate(key => JSON.parse(sessionStorage.getItem(key)), MARKER_KEY);
const panelButton = (view, key) => panel(view.page).getByRole("button", { name: text(view.language, key), exact: true });
const resultText = async page => (await page.locator("section.restart-watch .restart-result").textContent()).trim();
const fill = (template, values) => template.replace(/\{(\w+)\}/g, (_, key) => values[key]);
const until = async (test, label, timeout = 15000) => {
  for (const started = Date.now(); !test();) { if (Date.now() - started > timeout) throw new Error("timeout: " + label); await sleep(50); }
};
const waitPanelText = (page, wanted, timeout = 15000) => page.waitForFunction(value => {
  const node = document.querySelector("section.restart-watch"); return Boolean(node && node.textContent.includes(value)); }, wanted, { timeout });
// Wi-Fi change -> review -> "Save and restart"; the review must predict the restart.
async function saveWifi(view, ssid = "NewNet", { click = true } = {}) {
  const page = view.page;
  await page.locator('input[name="wifi_ssid"]').fill(ssid);
  await page.locator('input[name="wifi_pass"]').fill("secret123");
  await check(view).click();
  await page.locator('.config-review[data-restart="true"]').waitFor({ state: "visible" });
  if (click) await button(page, text(view.language, "ui.guided_save_restart")).click();
}
// Old links (#device/configuration) still open the form; the app replaces the alias by the canonical route in place.
const CONFIG_HASH = "#device/connections/internet", CONFIG_HASHES = ["#device/configuration", CONFIG_HASH];
async function assertConfigRoute(page, message) {
  assert(CONFIG_HASHES.includes(new URL(page.url()).hash), message + ": " + page.url());
  await page.waitForFunction(hash => location.hash === hash, CONFIG_HASH, { timeout: 5000 });
}
async function signIn(page) {
  await page.locator("#v2-device-code").waitFor({ timeout: 20000 });
  await page.locator("#v2-device-code").fill("ABC");
  await page.locator("button[type=submit]").click();
  await panel(page).waitFor({ timeout: 15000 });
}
// A marker planted once per tab before the app starts, as a reload between request and watcher leaves it.
const plant = fields => ({ fn: ([key, value]) => {
  if (sessionStorage.getItem("__planted")) return;
  sessionStorage.setItem("__planted", "1");
  sessionStorage.setItem(key, JSON.stringify(Object.assign({ started_ms: Date.now() }, value)));
}, arg: [MARKER_KEY, Object.assign({ v: 1, kind: "apply", route: "device/configuration", phase: "requested", apply_id: OWN_ID,
  boot_id: "a1b2c3d4", host: "rtk-test.local", target_ssid: "NewNet", attempt: { keys: ["wifi_pass", "wifi_ssid"], wifi_ssid: "NewNet" } }, fields)] });
const runClock = async (page, total, chunk = 1000) => { for (let done = 0; done < total; done += chunk) await page.clock.runFor(chunk); };
const NEWNET = { status: "connected", details: { ssid: "NewNet" } };

// B1: save and restart, hold through the restart, one reload to the sign-in page, then steps and the own result.
async function b1Success(browser, language) {
  const device = newDevice({ auth: true }), view = await openPage(browser, device, { language }), page = view.page, label = "B1 " + language;
  await saveWifi(view);
  await panel(page).waitFor();
  assert.strictEqual(device.puts.length, 1, label + ": one PUT");
  assert.strictEqual(device.applyCalls.length, 1, label + ": one apply");
  const apply = device.applyCalls[0], stored = await marker(page);
  assert.match(apply.apply_id, /^[0-9a-f]{16}$/, label + ": apply id of 16 hex characters");
  assert.strictEqual(apply.staging_revision, "a1b2c3d4-1", label + ": the revision from the PUT answer");
  assert.strictEqual(stored.apply_id, apply.apply_id, label + ": the marker keeps the apply id");
  assert.strictEqual(stored.boot_id, "a1b2c3d4", label + ": the marker keeps the boot id before the request");
  await step(page, "http", "wait").waitFor();
  const since = page.locator("section.restart-watch .restart-since"), first = await since.textContent();
  await sleep(1300);
  assert.notStrictEqual(await since.textContent(), first, label + ": the elapsed time runs");
  await page.locator('section.restart-watch[data-phase="back"]').waitFor({ timeout: 15000 });
  assert.strictEqual(device.loginLoads, 0, label + ": no sign-in page while the device restarts");
  await page.locator("#v2-device-code").waitFor({ timeout: 10000 });
  assert.strictEqual(device.loginLoads, 1, label + ": one reload to the sign-in page");
  assert(CONFIG_HASHES.includes(new URL(page.url()).hash), label + ": the sign-in page keeps the route: " + page.url());
  await signIn(page);
  await assertConfigRoute(page, label + ": back on the configuration");
  await step(page, "http", "ok").waitFor({ timeout: 15000 });
  for (const name of ["wifi", "ntrip", "storage"]) await step(page, name, "ok").waitFor({ timeout: 15000 });
  await page.locator('section.restart-watch[data-phase="final"][data-outcome="connected"]').waitFor({ timeout: 15000 });
  assert.strictEqual(await resultText(page), fill(literal(language, "Applied: connected to ‘{ssid}’. Portal: http://{host}/"), { ssid: "NewNet", host: "rtk-test.local" }));
  await waitPanelText(page, text(language, "ui.guided_reported_settings") + " " + fill(literal(language, "Wi-Fi ‘{ssid}’"), { ssid: "NewNet" }));
  if (language === "en") {
    const layout = await page.evaluate(() => {
      const watch = document.querySelector("section.restart-watch"), dock = document.getElementById("feedback-dock"), style = getComputedStyle(watch);
      const item = document.createElement("div");
      item.className = "feedback-item"; item.textContent = "dock"; dock.append(item); dock.hidden = false;
      scrollTo(0, document.documentElement.scrollHeight);
      const d = dock.getBoundingClientRect(), w = watch.getBoundingClientRect();
      const seen = { position: style.position, z: Number(style.zIndex), dockBottom: d.bottom, panelTop: w.top,
        fits: document.documentElement.scrollWidth <= innerWidth };
      item.remove(); dock.hidden = true; scrollTo(0, 0);
      return seen;
    });
    assert.strictEqual(layout.position, "fixed", label + ": the panel stays in view");
    assert(layout.z < 80, label + ": below the toast (z-index 80), got " + layout.z);
    assert(layout.dockBottom <= layout.panelTop + 0.5, label + ": the feedback dock stays above the panel " + JSON.stringify(layout));
    assert(layout.fits, label + ": no horizontal scrolling");
  }
  await panelButton(view, "ui.guided_action_field_setup").click();
  await page.waitForFunction(() => location.hash === "#measure/setup");
  await page.locator('.field-setup-layout [data-card="field-setup"]').waitFor({ state: "visible" });
  assert.strictEqual(await panel(page).count(), 0, label + ": the panel is closed");
  assert.strictEqual(await marker(page), null, label + ": the marker is gone");
  await page.locator('a[href="/#device"]:visible').first().click();
  await page.waitForFunction(() => location.hash === "#device");
  assert.strictEqual(await page.locator("dialog[open]").count(), 0, label + ": no discard question");
  await finish(view, device, label);
}

// B2: the device keeps the previous Wi-Fi; decision, reported settings and connection are separate notes.
async function b2Failure(browser) {
  const device = newDevice({ auth: true, outcome: { status: "failed", details: { code: "access_point_not_found", restored: ["wifi_ssid"] } } });
  const view = await openPage(browser, device), page = view.page, en = i18n.dictionaries.en;
  await saveWifi(view);
  await page.locator('section.restart-watch[data-phase="back"]').waitFor({ timeout: 15000 });
  await signIn(page);
  await page.locator('section.restart-watch[data-outcome="failed"]').waitFor({ timeout: 15000 });
  const result = await resultText(page);
  assert(result.includes(en["ui.guided_cause_access_point_not_found"]) && result.includes("returns to the previous Wi-Fi settings"), "B2: cause and decision");
  await until(() => device.configGets >= 2, "B2: the reported settings are read");
  await page.waitForFunction(() => document.querySelectorAll("section.restart-watch .restart-notes li").length >= 3);
  const notes = (await page.locator("section.restart-watch .restart-notes li").allTextContents()).map(item => item.trim());
  assert.deepStrictEqual(notes.slice(0, 3), [en["ui.guided_marked_set_back"] + " Wi-Fi name (SSID)",
    en["ui.guided_reported_settings"] + " Wi-Fi ‘Field’ · caster caster.example:2101/MOUNT · " + en["ui.guided_ntrip_on"],
    en["ui.guided_wifi_now_connected"]], "B2: notes in order");
  const all = await panelText(page);
  for (const claim of ["are active", "complete", "Set back:", "Saved on the device now"]) assert(!all.includes(claim), "B2: never " + claim);
  await panelButton(view, "ui.guided_action_edit").click();
  await page.locator("form.configuration-layout").waitFor();
  await page.waitForFunction(() => { const input = document.querySelector('input[name="wifi_ssid"]'); return Boolean(input) && input.value === "NewNet"; });
  assert.strictEqual(await page.locator('input[name="wifi_pass"]').inputValue(), "", "B2: no password prefilled");
  assert.strictEqual(await page.evaluate(() => document.activeElement && document.activeElement.name), "wifi_ssid", "B2: focus on the SSID");
  assert.strictEqual(await dirty(page), "true", "B2: the edit is dirty");
  assert.strictEqual(await marker(page), null, "B2: no marker");
  assert.strictEqual(await panel(page).count(), 0, "B2: no panel");
  await finish(view, device, "B2");
}

// B2 variants: resumed after the sign-in (phase back) on a device that already restarted.
async function b2Variants(browser) {
  const en = i18n.dictionaries.en, failed = restored => ({ status: "failed", details: { code: "access_point_not_found", restored } });
  for (const [name, setup, wifiKey, check] of [
    ["config_file", { configWarnings: [{ key: "config_file", code: "unreadable" }] }, "ui.guided_wifi_now_connected", notes =>
      assert.strictEqual(notes[2], en["ui.guided_config_file_unread"], "after the settings note")],
    ["recovery", { accessAfter: "RECOVERY" }, "ui.guided_wifi_now_recovery"],
    ["setup", { accessAfter: "SETUP" }, "ui.guided_wifi_now_setup"],
    ["connecting", { accessAfter: "CONNECTING" }, "ui.guided_wifi_now_connecting"],
    ["missing", { accessAfter: undefined }, "ui.guided_wifi_now_unknown"],
    ["restored_empty", { outcome: failed([]) }, "ui.guided_wifi_now_connected", notes =>
      assert(!notes.some(note => note.startsWith(en["ui.guided_marked_set_back"])), "no set-back note")],
    ["authentication_failed", { outcome: { status: "failed", details: { code: "authentication_failed", restored: ["wifi_pass"] } } }, "ui.guided_wifi_now_connected"]]) {
    const label = "B2 " + name, device = newDevice(Object.assign({ auth: true, pendingMs: 0, outcome: failed(["wifi_ssid"]) }, setup));
    device.booted(OWN_ID);
    const view = await openPage(browser, device, { init: plant({ phase: "back" }) }), page = view.page;
    await page.locator('section.restart-watch[data-outcome="failed"]').waitFor({ timeout: 15000 });
    await page.waitForFunction(() => Array.from(document.querySelectorAll("section.restart-watch .restart-notes li")).some(item => item.textContent.includes("caster")));
    const notes = (await page.locator("section.restart-watch .restart-notes li").allTextContents()).map(item => item.trim());
    assert(notes.includes(en[wifiKey]), label + ": " + wifiKey + " in " + JSON.stringify(notes));
    if (check) check(notes);
    const all = await panelText(page);
    for (const claim of ["are active", "complete", "Set back:", "Saved on the device now"]) assert(!all.includes(claim), label + ": never " + claim);
    if (name === "authentication_failed") {
      await panelButton(view, "ui.guided_action_edit").click();
      await page.waitForFunction(() => { const input = document.querySelector('input[name="wifi_ssid"]'); return Boolean(input) && input.value === "NewNet"; });
      assert.strictEqual(await page.evaluate(() => document.activeElement && document.activeElement.name), "wifi_pass", label + ": focus on the password");
    }
    await finish(view, device, label);
  }
}

// B3: the device never comes back: the watch ends after 90 s without a reload and polling runs again.
async function b3Timeout(browser) {
  const device = newDevice({ auth: true, neverBack: true }), view = await openPage(browser, device, { clock: true }), page = view.page;
  await saveWifi(view);
  await panel(page).waitFor();
  await until(() => device.down, "B3: the device goes down");
  await runClock(page, 91000, 2000);
  await page.locator('section.restart-watch[data-outcome="timeout"]').waitFor({ timeout: 10000 });
  assert.strictEqual(await resultText(page), "No answer after 90 s – connect the phone to ‘NewNet’ and reload the page.");
  assert.strictEqual(await panel(page).locator('a[href="http://rtk-test.local/#device/connections/internet"]').count(), 1, "B3: portal link");
  assert.strictEqual(device.loginLoads, 0, "B3: no reload");
  const tried = device.refused.filter(item => item === "GET /api/ui/live").length;
  // After the watch the app tries the live stream (#27) first; against the unreachable device it falls back to polling on the
  // browser's second connection error (real-time reconnect) or at the latest at the 22 s watchdog (page clock). Keep the
  // page clock moving while waiting, so a slow reconnect under load cannot leave the fallback waiting on a frozen clock.
  const polledAgain = () => device.refused.filter(item => item === "GET /api/ui/live").length > tried;
  for (let advanced = 0; !polledAgain() && advanced < 30000; advanced += 1000) { await page.clock.runFor(1000); await sleep(100); }
  await until(polledAgain, "B3: polling runs again", 5000);
  await panelButton(view, "ui.guided_action_close").click();
  assert.strictEqual(await panel(page).count(), 0, "B3: closed");
  await finish(view, device, "B3");
}

// B4: the apply is refused; the values are saved, the form is clean, "Apply again" reads the staging and confirms.
async function b4Refused(browser) {
  const device = newDevice({ auth: true, applyAnswer: (body, n) => n === 1 ? { status: 409, body: { error: "open_change_pending",
    message: "An earlier configuration change is still being checked." } } : null });
  const view = await openPage(browser, device), page = view.page;
  await saveWifi(view);
  await waitStatus(page, text("en", "ui.guided_kept_memory"), 5000);
  assert((await statusText(page)).includes("An earlier configuration change is still being checked."), "B4: the device message");
  assert.strictEqual(await dirty(page), "false", "B4: the staged values count as saved");
  assert.strictEqual(await page.locator('input[name="wifi_pass"]').inputValue(), "", "B4: the password field is empty");
  assert.strictEqual(await panel(page).count(), 0, "B4: no panel");
  assert.strictEqual(await marker(page), null, "B4: no marker");
  const lives = device.liveCalls;
  await until(() => device.liveCalls > lives, "B4: polling runs", 7000);
  const gets = device.configGets, puts = device.puts.length;
  await button(page, text("en", "ui.guided_action_retry")).click();
  const block = page.locator("section.config-apply");
  await block.waitFor({ state: "visible" });
  assert.strictEqual(device.configGets, gets + 1, "B4: the staging is read");
  assert.strictEqual(device.puts.length, puts, "B4: no empty PUT");
  assert((await block.textContent()).includes("Wi-Fi name (SSID)"), "B4: the staged key is named");
  await block.getByRole("button", { name: text("en", "ui.guided_apply_all"), exact: true }).click();
  await panel(page).waitFor();
  assert.strictEqual(device.applyCalls.length, 2);
  assert.strictEqual(device.applyCalls[1].staging_revision, "a1b2c3d4-1", "B4: the read revision");
  await page.locator('a[href="/#device"]:visible').first().click();
  await page.waitForFunction(() => location.hash === "#device");
  assert.strictEqual(await page.locator("dialog[open]").count(), 0, "B4: no discard question");
  await page.locator("main .restart-hold").waitFor();
  await finish(view, device, "B4");
}

// B5: an older result of this boot never shows in the panel.
async function b5OldResult(browser) {
  const device = newDevice({ auth: true, pendingMs: 4000, result: { schema_version: 2, status: "connected", details: { ssid: "Old" },
    uptime_sec: 60, boot_id: "a1b2c3d4", apply_id: "00000000" } });
  const watchOld = { fn: () => { new MutationObserver(() => { const node = document.querySelector("section.restart-watch");
    if (node && node.textContent.includes("Old")) sessionStorage.setItem("__saw_old", "1"); })
    .observe(document, { subtree: true, childList: true, characterData: true }); }, arg: null };
  const view = await openPage(browser, device, { init: watchOld }), page = view.page;
  await saveWifi(view);
  await page.locator('section.restart-watch[data-phase="back"]').waitFor({ timeout: 15000 });
  await signIn(page);
  await page.locator('section.restart-watch[data-outcome="connected"]').waitFor({ timeout: 20000 });
  assert(!(await panelText(page)).includes("Old"), "B5: not in the final panel");
  assert.strictEqual(await page.evaluate(() => sessionStorage.getItem("__saw_old")), null, "B5: never in the panel");
  await finish(view, device, "B5");
}

// B6: the device answers 202 but does not restart: after 20 s the panel says so; polling runs again.
async function b6NoRestart(browser) {
  const device = newDevice({ auth: true, restartOnApply: false }), view = await openPage(browser, device, { clock: true }), page = view.page;
  await saveWifi(view);
  await panel(page).waitFor();
  // The watch reloads the page without asking once the session is gone: no new edit may start while it runs.
  await runClock(page, 4000);
  assert.strictEqual(await page.locator("fieldset.config-fields").evaluate(node => node.disabled), true, "B6: fields locked during the watch");
  assert.strictEqual(await page.locator('input[name="ntrip_host"]').isEditable(), false, "B6: input not editable during the watch");
  assert.strictEqual(await check(view).isDisabled(), true, "B6: no new check during the watch");
  await runClock(page, 18000);
  await page.locator('section.restart-watch[data-outcome="not_restarted"]').waitFor({ timeout: 10000 });
  assert.strictEqual(await resultText(page), text("en", "ui.guided_outcome_not_restarted"));
  await page.waitForFunction(() => !document.querySelector("fieldset.config-fields").disabled, null, { timeout: 5000 });
  assert.strictEqual(await page.locator('input[name="ntrip_host"]').isEditable(), true, "B6: editable after the final outcome");
  assert.strictEqual(await panelButton(view, "ui.guided_action_retry").count(), 1, "B6: Apply again");
  const lives = device.liveCalls;
  await runClock(page, 6000);
  await until(() => device.liveCalls > lives, "B6: polling runs again", 5000);
  await finish(view, device, "B6");
}

// B7: a reload while the device checks the new connection keeps the watch.
async function b7ReloadWhileVerifying(browser) {
  const device = newDevice({ auth: true, pendingMs: 8000 }), view = await openPage(browser, device), page = view.page;
  await saveWifi(view);
  await page.locator('section.restart-watch[data-phase="back"]').waitFor({ timeout: 15000 });
  await signIn(page);
  await page.locator('section.restart-watch[data-phase="verifying"]').waitFor({ timeout: 10000 });
  await page.reload();
  await panel(page).waitFor();
  await page.locator('section.restart-watch[data-outcome="connected"]').waitFor({ timeout: 20000 });
  assert((await resultText(page)).startsWith("Applied: connected to ‘NewNet’."), "B7: the own result");
  // A reload after the final outcome: the marker keeps the outcome only, so the panel shows no step list beside it.
  await page.reload();
  await page.locator('section.restart-watch[data-phase="final"][data-outcome="connected"]').waitFor({ timeout: 10000 });
  assert((await resultText(page)).startsWith("Applied: connected to ‘NewNet’."), "B7: the outcome after the reload");
  assert.strictEqual(await page.locator("section.restart-watch .restart-steps li").count(), 0, "B7: no step list after the reload");
  assert.strictEqual(await page.locator("section.restart-watch .restart-steps").isVisible(), false, "B7: the empty list is hidden");
  assert(!(await panelText(page)).includes(text("en", "ui.guided_step_unreachable")), "B7: no 'not reachable' beside the outcome");
  assert.strictEqual(view.counts.beforeunload, 0, "B7: no unload question");
  await finish(view, device, "B7");
}

// B8: without a marker only a neutral result of this device start; without a boot id before the request nothing is sent.
async function b8WithoutMarker(browser) {
  const en = i18n.dictionaries.en, record = bootId => ({ schema_version: 2, status: "failed", uptime_sec: 4, boot_id: bootId,
    apply_id: OWN_ID, details: { code: "access_point_not_found", restored: ["wifi_ssid"] } });
  for (const [bootId, start] of [["a1b2c3d4", "Configuration result since the last device start"], [null, "Last recorded configuration result (time unknown)"], ["ffff0000", null]]) {
    const label = "B8 " + bootId, device = newDevice({ auth: true, result: record(bootId) }), view = await openPage(browser, device), page = view.page;
    await until(() => device.requests.some(item => item.path === "/api/config-result"), label + ": result read");
    await sleep(500);
    const notice = page.locator('.notice[data-notice="last-change"]');
    if (start === null) assert.strictEqual(await notice.count(), 0, label + ": no notice for another boot");
    else {
      const content = (await notice.textContent()).trim();
      assert(content.startsWith(start), label + ": " + content);
      assert(content.includes(en["ui.guided_cause_access_point_not_found"]), label + ": the cause");
      assert(!content.includes("Not applied"), label + ": no verdict");
      await notice.getByRole("button", { name: text("en", "ui.guided_action_close"), exact: true }).click();
      assert.strictEqual(await notice.count(), 0, label + ": closed");
    }
    await finish(view, device, label);
  }
  const device = newDevice({ auth: true, failLiveAfterPut: true }), view = await openPage(browser, device), page = view.page;
  await saveWifi(view);
  await waitStatus(page, "No answer from the device; nothing was sent.", 6000);
  assert.strictEqual(device.applyCalls.length, 0, "B8: nothing sent");
  assert.strictEqual(await panel(page).count(), 0, "B8: no panel");
  await finish(view, device, "B8 no boot id");
}

// B9: navigation during the restart shows the hold, starts no route request; a held live answer with 401 changes nothing.
async function b9LateUnauthorized(browser) {
  const device = newDevice({ auth: true }), view = await openPage(browser, device), page = view.page;
  await saveWifi(view, "NewNet", { click: false });
  device.holdLiveNext = true;
  await until(() => device.heldLive !== null, "B9: a poll is held", 7000);
  await button(page, text("en", "ui.guided_save_restart")).click();
  await panel(page).waitFor();
  await until(() => device.down, "B9: the device goes down");
  const mark = device.requests.length;
  await page.evaluate(() => { location.hash = "#device/diagnostics/log"; });
  await page.locator("main .restart-hold").waitFor();
  await page.evaluate(() => { location.hash = "#projects/p1"; });
  await page.waitForFunction(() => location.hash === "#projects/p1" && document.querySelector("main .restart-hold"));
  await until(() => device.rebootCount === 1, "B9: the device is back");
  device.releaseHeld(401);
  await page.locator('section.restart-watch[data-phase="back"]').waitFor({ timeout: 15000 });
  const later = device.requests.slice(mark).map(item => item.path);
  assert(!later.some(path => path.startsWith("/api/log") || path.startsWith("/api/tracking")), "B9: no route request " + JSON.stringify(later));
  assert.strictEqual(device.loginLoads, 0, "B9: no sign-in page before the device is back");
  assert.strictEqual(device.authedDuringHold, 0, "B9: no request with the session during the restart");
  await finish(view, device, "B9");
}

// B10: online and visibility events during the restart keep the hold.
async function b10Events(browser) {
  const device = newDevice({ auth: true }), view = await openPage(browser, device), page = view.page;
  await saveWifi(view);
  await panel(page).waitFor();
  await until(() => device.down, "B10: the device goes down");
  await page.evaluate(() => {
    dispatchEvent(new Event("online"));
    Object.defineProperty(document, "hidden", { value: true, configurable: true });
    document.dispatchEvent(new Event("visibilitychange"));
    Object.defineProperty(document, "hidden", { value: false, configurable: true });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await page.locator("main .restart-hold").waitFor();
  assert.strictEqual(await page.locator("main .notice.critical").count(), 0, "B10: no error message");
  await page.locator('section.restart-watch[data-phase="back"]').waitFor({ timeout: 15000 });
  assert.strictEqual(device.authedDuringHold, 0, "B10: no request with the session");
  await finish(view, device, "B10");
}

// B11: navigation during the restart, then a second apply: one panel, one watcher.
async function b11Second(browser) {
  const device = newDevice({ auth: true }), view = await openPage(browser, device), page = view.page;
  await saveWifi(view);
  await panel(page).waitFor();
  await until(() => device.down, "B11: the device goes down");
  await page.locator('a[href="/#device"]:visible').first().click();
  await page.waitForFunction(() => location.hash === "#device" && document.querySelector("main .restart-hold"));
  await page.locator('section.restart-watch[data-phase="back"]').waitFor({ timeout: 15000 });
  assert.strictEqual(device.authedDuringHold, 0, "B11: no request with the session");
  await signIn(page);
  assert.strictEqual(new URL(page.url()).hash, "#device");
  await page.locator('section.restart-watch[data-outcome="connected"]').waitFor({ timeout: 20000 });
  await page.evaluate(() => { location.hash = "#device/configuration"; });
  await page.locator("form.configuration-layout").waitFor();
  await saveWifi(view, "Second");
  await page.locator('section.restart-watch[data-phase="restarting"]').waitFor();
  const from = Date.now();
  await page.locator('section.restart-watch[data-phase="back"]').waitFor({ timeout: 15000 });
  assert.strictEqual(await panel(page).count(), 1, "B11: one panel");
  const probes = device.probes.filter(at => at >= from);
  for (let i = 1; i < probes.length; i++) assert(probes[i] - probes[i - 1] >= 1500, "B11: one watcher " + JSON.stringify(probes));
  await finish(view, device, "B11");
}

// B12/B13: the apply answer is lost or cut: watch anyway, one reload without a question, the result by apply id, no retry.
async function b12LostAnswer(browser, mode) {
  const label = "B12 " + mode, device = newDevice({ auth: true, applyMode: mode }), view = await openPage(browser, device), page = view.page;
  await saveWifi(view, "NewNet", { click: false });
  assert.strictEqual(await dirty(page), "true", label + ": edited");
  await button(page, text("en", "ui.guided_save_restart")).click();
  await panel(page).waitFor();
  assert.strictEqual((await marker(page)).uncertain, true, label + ": the outcome is uncertain");
  assert.strictEqual(await dirty(page), "false", label + ": clean");
  assert.strictEqual(await page.locator('input[name="wifi_pass"]').inputValue(), "", label + ": password field empty");
  await page.locator("#v2-device-code").waitFor({ timeout: 20000 });
  assert.strictEqual(device.loginLoads, 1, label + ": one reload");
  assert.strictEqual(view.counts.beforeunload, 0, label + ": no unload question");
  await signIn(page);
  await page.locator('section.restart-watch[data-outcome="connected"]').waitFor({ timeout: 20000 });
  assert((await resultText(page)).startsWith("Applied: connected to ‘NewNet’."), label + ": the own result");
  assert.strictEqual(device.applyCalls.length, 1, label + ": no repetition");
  await finish(view, device, label);
}

// B14: a reload between request and watcher (phase requested): the boot id decides first.
async function b14Requested(browser) {
  const wanted = { apply: "Applied: connected to ‘NewNet’. Portal: http://rtk-test.local/", restore: "Backup restored: connected to ‘NewNet’.",
    reboot: text("en", "ui.guided_outcome_restart_observed") };
  for (const kind of ["apply", "restore", "reboot"]) {
    const fields = kind === "apply" ? {} : { kind, target_ssid: null, attempt: null };
    for (const restarted of [true, false]) {
      const label = "B14 " + kind + (restarted ? " restarted" : " not restarted");
      const device = newDevice({ auth: true, pendingMs: 0, outcome: NEWNET, holdLiveNext: true });
      if (restarted) device.booted(kind === "reboot" ? undefined : OWN_ID);
      const view = await openPage(browser, device, { init: plant(fields), wait: false, clock: !restarted }), page = view.page;
      await until(() => device.heldLive !== null, label + ": the boot check runs");
      await page.locator("main .restart-hold").waitFor();
      const first = device.requests.filter(item => item.authed && item.path !== "/")[0];
      assert.strictEqual(first && first.path, "/api/ui/live", label + ": the first request is the boot check");
      device.releaseHeld();
      if (restarted) {
        await page.locator('section.restart-watch[data-phase="final"]').waitFor({ timeout: 15000 });
        assert.strictEqual(await resultText(page), wanted[kind], label);
      } else {
        await runClock(page, 22000);
        await page.locator('section.restart-watch[data-outcome="not_restarted"]').waitFor({ timeout: 10000 });
        assert.strictEqual(await resultText(page), text("en", "ui.guided_outcome_not_restarted_uncertain"), label);
        assert.strictEqual(await panelButton(view, "ui.guided_action_retry").count(), kind === "apply" ? 1 : 0, label + ": Apply again");
        assert.strictEqual(await panelButton(view, "ui.guided_action_close").count(), 1, label + ": Close");
      }
      assert(!(await panelText(page)).includes("did not restart") || !restarted, label + ": never 'did not restart'");
      await finish(view, device, label);
    }
  }
}

// B15: the result comes before the device is ready: shown at once, the steps keep running.
async function b15EarlyResult(browser) {
  for (const ntrip of ["streaming", "auth_error"]) {
    const label = "B15 " + ntrip, device = newDevice({ auth: true, pendingMs: 0, loadingMs: 6000, ntripAfter: ntrip, outcome: NEWNET });
    device.booted(OWN_ID);
    const view = await openPage(browser, device, { init: plant({ phase: "back" }) }), page = view.page;
    await page.locator('section.restart-watch[data-outcome="connected"]').waitFor({ timeout: 10000 });
    assert.strictEqual(await step(page, "storage", "wait").count(), 1, label + ": the storage still loads");
    assert((await resultText(page)).startsWith("Applied: connected to ‘NewNet’."), label + ": the result is shown");
    await step(page, "storage", "ok").waitFor({ timeout: 15000 });
    await page.locator('section.restart-watch[data-phase="final"]').waitFor({ timeout: 10000 });
    if (ntrip === "auth_error") await waitPanelText(page, text("en", "ui.guided_note_ntrip_credentials"));
    await finish(view, device, label);
  }
}

// B16: the staged value is replaced under the same key before the confirmation: 409, read again, confirm again.
async function b16ReplacedStaging(browser) {
  const device = newDevice({ auth: true, staged: { wifi_ssid: "Barn" }, stagingSerial: 1 }), view = await openPage(browser, device), page = view.page;
  const staged = page.locator("section.config-staged");
  await staged.waitFor({ state: "visible" });
  await staged.getByRole("button", { name: text("en", "ui.guided_apply_now"), exact: true }).click();
  const block = page.locator("section.config-apply");
  await block.waitFor({ state: "visible" });
  assert((await block.textContent()).includes("Wi-Fi name (SSID)"), "B16: the staged key is named");
  device.staged.wifi_ssid = "Other"; device.stagingSerial = 2;
  await block.getByRole("button", { name: text("en", "ui.guided_apply_all"), exact: true }).click();
  await page.waitForFunction(() => { const node = document.querySelector("section.config-apply"); return node && node.dataset.changed === "true"; });
  assert.deepStrictEqual(device.applyCalls.map(item => item.staging_revision), ["a1b2c3d4-1"], "B16: refused with the old revision");
  assert.strictEqual(device.rebootCount + Number(device.down), 0, "B16: no restart");
  const changed = page.locator("section.config-apply");
  assert((await changed.textContent()).includes(fill(text("en", "ui.guided_staging_changed_meanwhile"), { labels: "Wi-Fi name (SSID)" })), "B16: changed in the meantime");
  await changed.getByRole("button", { name: text("en", "ui.guided_apply_all"), exact: true }).click();
  await panel(page).waitFor();
  assert.deepStrictEqual(device.applyCalls.map(item => item.staging_revision), ["a1b2c3d4-1", "a1b2c3d4-2"], "B16: the new revision");
  assert.strictEqual(device.applyCalls.length, 2, "B16: no apply without a click");
  await finish(view, device, "B16");
}

// B17: Close, Edit again and Check field setup while the storage still loads end the watch for good.
async function b17Actions(browser) {
  for (const [action, outcome] of [["ui.guided_action_close", NEWNET], ["ui.guided_action_edit", { status: "failed", details: { code: "access_point_not_found", restored: [] } }],
    ["ui.guided_action_field_setup", NEWNET]]) {
    const label = "B17 " + action, device = newDevice({ auth: true, pendingMs: 0, loadingMs: 8000, outcome });
    device.booted(OWN_ID);
    const view = await openPage(browser, device, { init: plant({ phase: "back" }) }), page = view.page;
    await page.locator('section.restart-watch[data-phase="settling"]').waitFor({ timeout: 10000 });
    await step(page, "storage", "wait").waitFor();
    await panelButton(view, action).click();
    assert.strictEqual(await panel(page).count(), 0, label + ": panel gone");
    assert.strictEqual(await marker(page), null, label + ": marker gone");
    const results = device.requests.filter(item => item.path === "/api/config-result").length;
    await sleep(10000);
    assert.strictEqual(await panel(page).count(), 0, label + ": no panel later");
    assert.strictEqual(device.requests.filter(item => item.path === "/api/config-result").length, results, label + ": no watch request");
    if (action === "ui.guided_action_edit") await page.locator("form.configuration-layout").waitFor();
    if (action === "ui.guided_action_field_setup") {
      await page.locator('.field-setup-layout [data-card="field-setup"]').waitFor({ state: "visible" });
      await page.evaluate(() => { location.hash = "#device/configuration"; });
      await page.locator("form.configuration-layout").waitFor();
      await saveWifi(view, "Third");
      await page.locator('section.restart-watch[data-phase="restarting"]').waitFor();
      const from = Date.now();
      await page.locator('section.restart-watch[data-phase="back"]').waitFor({ timeout: 15000 });
      assert.strictEqual(await panel(page).count(), 1, label + ": one panel");
      const probes = device.probes.filter(at => at >= from);
      for (let i = 1; i < probes.length; i++) assert(probes[i] - probes[i - 1] >= 1500, label + ": one watcher");
    }
    await finish(view, device, label);
  }
}

// B18: the apply head arrives, the body only after the restart began (or never): a real local server.
async function b18OpenBody(browser, mode) {
  const label = "B18 " + mode, device = newDevice({ auth: true, applyMode: mode }), server = await serverAdapter(device);
  try {
    const view = await openPage(browser, device, { origin: server.origin, route: false }), page = view.page;
    await saveWifi(view, "NewNet", { click: false });
    device.holdLiveNext = true;
    await until(() => device.heldLive !== null, label + ": a poll is held", 7000);
    const gets = device.configGets;
    await button(page, text("en", "ui.guided_save_restart")).click();
    await until(() => device.headerSentMs !== null, label + ": head sent");
    device.later(2000, () => device.releaseHeld(401));
    if (mode === "slow_body") {
      await until(() => device.bodySentMs !== null, label + ": body sent", 10000);
      assert(device.bodySentMs - device.headerSentMs >= 4500, label + ": body after the head");
      assert.strictEqual(device.loginLoads, 0, label + ": no sign-in page meanwhile");
      assert.strictEqual(device.authedDuringHold, 0, label + ": no request with the session meanwhile");
      assert.strictEqual(device.configGets, gets, label + ": no follow-up GET");
    }
    await panel(page).waitFor({ timeout: 10000 });
    if (mode === "slow_body_destroyed") assert.strictEqual((await marker(page)).uncertain, true, label + ": uncertain");
    await page.locator("#v2-device-code").waitFor({ timeout: 20000 });
    assert.strictEqual(device.loginLoads, 1, label + ": one reload");
    assert.strictEqual(view.counts.beforeunload, 0, label + ": no unload question");
    await signIn(page);
    await page.locator('section.restart-watch[data-outcome="connected"]').waitFor({ timeout: 20000 });
    assert((await resultText(page)).startsWith("Applied: connected to ‘NewNet’."), label + ": the own result");
    assert.strictEqual(device.applyCalls.length, 1, label + ": one apply");
    await finish(view, device, label);
  } finally { await server.close(); }
}

// B19: a result of another request after an intermediate boot is only observed.
async function b19Foreign(browser) {
  const device = newDevice({ auth: true, pendingMs: 0, outcome: { status: "connected", details: { ssid: "NewNet" }, apply_id: "fedcba9876543210" } });
  device.booted(OWN_ID);
  const view = await openPage(browser, device, { init: plant({ phase: "back" }) }), page = view.page;
  await page.locator('section.restart-watch[data-outcome="observed"]').waitFor({ timeout: 10000 });
  assert.strictEqual(await resultText(page), "The device reports: connected to ‘NewNet’. This result does not belong to this request for certain.");
  assert(!(await panelText(page)).includes("Applied:"), "B19: no verdict");
  await finish(view, device, "B19");
}

// B21: a caster-only save answers restart_required: the shared restart confirmation reads live again and warns.
async function b21Unexpected(browser, variant) {
  const language = variant === "de" ? "de" : "en", label = "B21 " + variant;
  const device = newDevice({ auth: true, putAnswer: () => ({ status: 200, body: { applied: ["ntrip_mount"], staged: [], pending: false,
    restart_required: true, staging_revision: "a1b2c3d4-0" } }) });
  const view = await openPage(browser, device, { language }), page = view.page;
  await page.locator('input[name="ntrip_mount"]').fill("OTHER");
  await check(view).click();
  const review = page.locator('.config-review[data-restart="false"]');
  await review.waitFor({ state: "visible" });
  assert(!(await review.textContent()).includes(text(language, "ui.guided_warn_measurement")), label + ": no warning in the review");
  // The caster hand-over fails on the device: the save leaves a restart pending (cfg._RESTART_REQUIRED).
  device.measuring = variant !== "plain";
  device.restartFlag = true;
  const lives = device.liveCalls;
  await button(page, text(language, "ui.guided_save")).click();
  const dialog = page.locator("dialog[open]");
  await dialog.waitFor();
  assert(device.liveCalls > lives, label + ": live read when the dialog opens");
  assert.strictEqual(await dirty(page), "false", label + ": saved");
  assert.strictEqual((await dialog.locator("h2").textContent()).trim(), literal(language, "Restart needed"));
  const description = (await dialog.locator("p").first().textContent()).trim();
  const go = literal(language, "The device must restart for these settings to take effect.");
  if (variant === "plain") {
    assert.strictEqual(description, go, label + ": no warning");
    await dialog.getByRole("button", { name: literal(language, "Restart now"), exact: true }).click();
    await panel(page).waitFor();
    assert.strictEqual(device.applyCalls.length, 1);
    assert.strictEqual(device.applyCalls[0].staging_revision, "a1b2c3d4-0", label + ": the PUT revision");
    assert.match(device.applyCalls[0].apply_id, /^[0-9a-f]{16}$/);
    return finish(view, device, label);
  }
  assert.strictEqual(description, go + " " + text(language, "ui.guided_warn_measurement"), label + ": text and warning");
  assert.strictEqual(await dialog.getByRole("button", { name: text(language, "ui.guided_restart_anyway"), exact: true }).count(), 1, label + ": Restart anyway");
  await dialog.getByRole("button", { name: text(language, "action.cancel"), exact: true }).click();
  await waitStatus(page, text(language, "ui.guided_saved_later"));
  assert.strictEqual(device.applyCalls.length, 0, label + ": nothing applied");
  assert.strictEqual(await panel(page).count(), 0, label + ": no panel");
  assert.strictEqual(await marker(page), null, label + ": no marker");
  const after = device.liveCalls;
  await until(() => device.liveCalls > after, label + ": polling runs", 7000);
  await finish(view, device, label);
}

// Part C: restore and "Restart device" through the same watch, with the warning during a measurement.
const RESTORE_EN = "This replaces Wi-Fi and NTRIP settings, the measurement rules and method, and the field setup (antenna height, reference frame, epoch), then restarts the receiver. Backups from older firmware contain only Wi-Fi and NTRIP settings.";
const RECORDING_EN = "Line ‘{name}’ is recording. The restart interrupts the recording; points already saved are kept.";
const RESTORE_FILE = JSON.stringify({ schema_version: 2, connections: { wifi_ssid: "Field" }, measurement: { samples: 5 } }, null, 2) + "\n";
const dialogOf = page => page.locator("dialog[open]");
const dialogText = async page => (await dialogOf(page).locator("p").first().textContent()).trim();
const restoreNotice = page => page.locator('div.notice[data-notice="restore"]');
// "Restore device configuration" -> file chooser -> the dialog opens.
async function chooseBackup(view) {
  const chooser = view.page.waitForEvent("filechooser");
  await button(view.page, text(view.language, "ui.guided_backup_restore")).click();
  await (await chooser).setFiles({ name: "backup.json", mimeType: "application/json", buffer: Buffer.from(RESTORE_FILE) });
  await dialogOf(view.page).waitFor();
}
const confirmDialog = (view, name) => dialogOf(view.page).getByRole("button", { name, exact: true }).click();
const restoreSubmit = language => literal(language, "Restore and restart");
// Restore and "Restart device" live under Maintenance only (#24-UI E9, E19): there is no form on that page.
const deviceHash = { hash: "#device/maintenance" };
const MAINTENANCE = { hash: "#device/maintenance", wait: '[data-card="config-backup"]' };
// Restore from Maintenance; the device restarts and the result is the own one.
async function c1RestoreSuccess(browser, language) {
  const label = "C1 " + language, device = newDevice({ auth: true }), view = await openPage(browser, device, Object.assign({ language }, MAINTENANCE)), page = view.page;
  await chooseBackup(view);
  assert.strictEqual(await dialogText(page), literal(language, RESTORE_EN), label + ": the scope text of the backup");
  await confirmDialog(view, restoreSubmit(language));
  await panel(page).waitFor();
  assert.strictEqual(device.restoreCalls.length, 1, label + ": one restore request");
  const call = device.restoreCalls[0], stored = await marker(page);
  assert.match(call.id, /^[0-9a-f]{16}$/, label + ": apply id of 16 hex characters");
  assert.strictEqual(call.raw, RESTORE_FILE, label + ": the file text is sent unchanged");
  assert.strictEqual(stored.apply_id, call.id, label + ": the marker keeps the apply id");
  assert.strictEqual(stored.kind, "restore", label + ": kind restore");
  assert.deepStrictEqual(stored.sections, ["connections", "measurement", "field_setup"], label + ": sections from the answer");
  assert.strictEqual(await restoreNotice(page).count(), 0, label + ": no refusal notice");
  await page.locator('section.restart-watch[data-phase="back"]').waitFor({ timeout: 15000 });
  assert.strictEqual(device.loginLoads, 0, label + ": no sign-in page while the device restarts");
  await page.locator("#v2-device-code").waitFor({ timeout: 10000 });
  assert.strictEqual(device.loginLoads, 1, label + ": one reload");
  await signIn(page);
  await page.locator('section.restart-watch[data-phase="final"][data-outcome="connected"]').waitFor({ timeout: 20000 });
  assert.strictEqual(await resultText(page), fill(literal(language, "Backup restored: connected to ‘{ssid}’."), { ssid: "Field" }), label + ": the own result");
  const notes = await page.locator("section.restart-watch .restart-notes").textContent();
  assert(!notes.includes(text(language, "ui.guided_restore_older_backup")) && !notes.includes(text(language, "ui.guided_restore_kept_wifi")), label + ": no scope notes");
  assert.strictEqual(device.restoreCalls.length, 1, label + ": no repetition");
  await finish(view, device, label);
}
// An old backup (connections only) that kept the Wi-Fi access of the device.
async function c2RestoreOld(browser) {
  const device = newDevice({ auth: true, restoreSections: ["connections"], restoreKept: ["wifi"] }), view = await openPage(browser, device, MAINTENANCE), page = view.page;
  await chooseBackup(view);
  await confirmDialog(view, restoreSubmit("en"));
  await panel(page).waitFor();
  await page.locator("#v2-device-code").waitFor({ timeout: 20000 });
  await signIn(page);
  await page.locator('section.restart-watch[data-phase="final"][data-outcome="connected"]').waitFor({ timeout: 20000 });
  const notes = await page.locator("section.restart-watch .restart-notes").textContent();
  assert(notes.includes(text("en", "ui.guided_restore_older_backup")), "C2: older firmware note");
  assert(notes.includes(text("en", "ui.guided_restore_kept_wifi")), "C2: kept Wi-Fi note");
  await finish(view, device, "C2");
}
// Definite refusals end in a notice in the backup card of Maintenance: no panel, no marker, polling again.
async function c3c4RestoreRefused(browser) {
  const reason = "measurement: samples must be 1, 5, 10 or quality.";
  const cases = [
    ["C3 400", { status: 400, body: { error: "invalid_backup", message: "The backup is invalid: " + reason } },
      fill("Backup not restored: {reason} Nothing was changed.", { reason }), ["Nothing was changed."]],
    ["C4 409", { status: 409, body: { error: "config_verification_running", message: "The device is connecting to Wi-Fi. Try again in a minute." } },
      text("en", "ui.guided_restore_busy"), ["earlier change"]],
    ["C4 413", { status: 413, text: "Request body is too large." }, text("en", "ui.guided_restore_too_large"), ["too large"]],
    ["C4 500", { status: 500, body: { error: "stage_failed", message: "Could not stage the configuration." } },
      text("en", "ui.guided_restore_stage_failed"), ["may already apply"]]];
  for (const [label, answer, wanted, parts] of cases) {
    const device = newDevice({ auth: true, restoreAnswer: () => answer }), view = await openPage(browser, device, MAINTENANCE), page = view.page;
    await chooseBackup(view);
    await confirmDialog(view, restoreSubmit("en"));
    await restoreNotice(page).waitFor({ state: "visible" });
    const shown = (await restoreNotice(page).textContent()).trim();
    assert.strictEqual(shown, wanted, label + ": the notice text");
    for (const part of parts) assert(shown.includes(part), label + ": mentions " + part);
    assert.strictEqual(await panel(page).count(), 0, label + ": no panel");
    assert.strictEqual(await marker(page), null, label + ": no marker");
    assert.strictEqual(await page.locator("main .restart-hold").count(), 0, label + ": the hold is over");
    assert.strictEqual(device.rebootCount + Number(device.down), 0, label + ": no restart");
    assert.strictEqual(await page.locator('[data-card="config-backup"] [data-notice="restore"]').count(), 1, label + ": the notice stands in the backup card");
    assert.strictEqual(await page.locator('[data-card="config-backup"] .btn').count(), 2, label + ": download and restore stay usable");
    const lives = device.liveCalls;
    await until(() => device.liveCalls > lives, label + ": polling runs", 8000);
    await finish(view, device, label);
  }
}
// Warning before a restart during a measurement or a recording; cancelling sends nothing.
async function c6Warnings(browser, language) {
  const label = "C6 " + language, warn = text(language, "ui.guided_warn_measurement"), anyway = text(language, "ui.guided_restart_anyway");
  const recording = fill(literal(language, RECORDING_EN), { name: "Line 7" });
  {
    const device = newDevice({ auth: true, measuring: true }), view = await openPage(browser, device, Object.assign({ language, wait: false }, deviceHash)), page = view.page;
    await button(page, text(language, "ui.device_restart")).click();
    await dialogOf(page).waitFor();
    assert.strictEqual(await dialogText(page), text(language, "ui.device_restart_confirm") + " " + warn, label + ": restart dialog text and warning");
    assert.strictEqual(await dialogOf(page).getByRole("button", { name: anyway, exact: true }).count(), 1, label + ": Restart anyway");
    await dialogOf(page).getByRole("button", { name: text(language, "action.cancel"), exact: true }).click();
    await page.waitForFunction(() => !document.querySelector("dialog[open]"));
    assert.strictEqual(device.rebootCalls.length, 0, label + ": cancelling sends nothing");
    assert.strictEqual(await marker(page), null, label + ": no marker");
    await finish(view, device, label + " measurement");
  }
  {
    const device = newDevice({ auth: true, activeLine: { state: "recording", name: "Line 7" } }), view = await openPage(browser, device, Object.assign({ language }, MAINTENANCE)), page = view.page;
    await chooseBackup(view);
    const restore = literal(language, RESTORE_EN);
    assert.strictEqual(await dialogText(page), restore + " " + recording, label + ": restore dialog text and recording warning");
    assert.strictEqual(await dialogOf(page).getByRole("button", { name: anyway, exact: true }).count(), 1, label + ": Restart anyway");
    await dialogOf(page).getByRole("button", { name: text(language, "action.cancel"), exact: true }).click();
    await page.waitForFunction(() => !document.querySelector("dialog[open]"));
    assert.strictEqual(device.restoreCalls.length, 0, label + ": cancelling sends nothing");
    await finish(view, device, label + " recording");
  }
  if (language !== "en") return;
  // The review predicts a restart for a Wi-Fi change (warning) but not for a caster-only change with an empty staging.
  for (const wifi of [true, false]) {
    const device = newDevice({ auth: true, measuring: true }), view = await openPage(browser, device), page = view.page;
    if (wifi) await page.locator('input[name="wifi_ssid"]').fill("NewNet"); else await page.locator('input[name="ntrip_mount"]').fill("OTHER");
    await check(view).click();
    const review = page.locator(".config-review");
    await review.waitFor({ state: "visible" });
    assert.strictEqual((await review.textContent()).includes(warn), wifi, label + ": review warning " + wifi);
    await finish(view, device, label + " review " + wifi);
  }
}
// "Restart device": confirmation, one order with the apply id, the steps up to the readiness of the survey storage.
async function c5Reboot(browser) {
  const device = newDevice({ auth: true, loadingMs: Infinity, ntripAfter: "disabled", ntripConfigAfter: null }), view = await openPage(browser, device, Object.assign({ wait: false }, deviceHash)), page = view.page;
  await button(page, text("en", "ui.device_restart")).click();
  await dialogOf(page).waitFor();
  assert.strictEqual(await dialogText(page), text("en", "ui.device_restart_confirm"), "C5: no warning without a measurement");
  await confirmDialog(view, text("en", "ui.device_restart"));
  await panel(page).waitFor();
  assert.strictEqual(device.rebootCalls.length, 1, "C5: one order");
  assert.deepStrictEqual(Object.keys(device.rebootCalls[0]), ["apply_id"], "C5: the body carries the apply id only");
  assert.match(device.rebootCalls[0].apply_id, /^[0-9a-f]{16}$/);
  assert.strictEqual((await marker(page)).kind, "reboot", "C5: kind reboot");
  await page.locator('section.restart-watch[data-phase="back"]').waitFor({ timeout: 15000 });
  assert.strictEqual(device.loginLoads, 0, "C5: no sign-in page while the device restarts");
  await page.locator("#v2-device-code").waitFor({ timeout: 10000 });
  assert.strictEqual(new URL(page.url()).hash, deviceHash.hash, "C5: the sign-in keeps the fragment");
  await signIn(page);
  assert.strictEqual(new URL(page.url()).hash, deviceHash.hash, "C5: back on the device page");
  await step(page, "http", "ok").waitFor({ timeout: 15000 });
  await step(page, "wifi", "ok").waitFor({ timeout: 15000 });
  await step(page, "storage", "wait").waitFor({ timeout: 15000 });
  await step(page, "ntrip", "wait").waitFor({ timeout: 15000 });
  assert.strictEqual((await step(page, "ntrip", "wait").textContent()).includes(text("en", "ui.guided_step_not_started")), true, "C5: neutral before the caster state is known");
  await waitPanelText(page, text("en", "ui.guided_phase_settling"));
  assert(!(await panelText(page)).includes(text("en", "ui.guided_outcome_done")), "C5: no completion while the storage loads");
  await sleep(1500);
  assert(!(await panelText(page)).includes(text("en", "ui.guided_outcome_done")), "C5: still no completion");
  device.ntripConfigAfter = "switched_off";
  device.loadingMs = 0;
  await step(page, "ntrip", "off").waitFor({ timeout: 15000 });
  assert((await step(page, "ntrip", "off").textContent()).includes(text("en", "ui.guided_step_switched_off")), "C5: switched off");
  await step(page, "storage", "ok").waitFor({ timeout: 15000 });
  await page.locator('section.restart-watch[data-phase="final"][data-outcome="done"]').waitFor({ timeout: 15000 });
  assert.strictEqual(await resultText(page), text("en", "ui.guided_outcome_done"), "C5: done");
  assert.strictEqual(device.rebootCalls.length, 1, "C5: no repetition");
  await finish(view, device, "C5");
}
// A lost restore answer: watched, the result by apply id, exactly one request.
async function c7RestoreLost(browser) {
  const device = newDevice({ auth: true, applyMode: "lost", restoreSections: ["connections"], restoreKept: ["wifi"] }), view = await openPage(browser, device, MAINTENANCE), page = view.page;
  await chooseBackup(view);
  await confirmDialog(view, restoreSubmit("en"));
  await panel(page).waitFor();
  assert.strictEqual((await marker(page)).uncertain, true, "C7: the outcome is uncertain");
  await page.locator("#v2-device-code").waitFor({ timeout: 20000 });
  await signIn(page);
  await page.locator('section.restart-watch[data-outcome="connected"]').waitFor({ timeout: 20000 });
  assert.strictEqual(await resultText(page), fill(literal("en", "Backup restored: connected to ‘{ssid}’."), { ssid: "Field" }), "C7: the own result by apply id");
  const notes = await page.locator("section.restart-watch .restart-notes").textContent();
  assert(!notes.includes(text("en", "ui.guided_restore_older_backup")), "C7: no scope notes after a lost answer");
  assert.strictEqual(device.restoreCalls.length, 1, "C7: exactly one restore request");
  await finish(view, device, "C7");
}
// A lost restart answer: the restart is observed (new boot id) or it did not happen (same boot id, 20 s).
async function c8RebootLost(browser, restarts) {
  const label = "C8 " + (restarts ? "restarts" : "does not restart");
  const device = newDevice({ auth: true, applyMode: "lost", restartOnApply: restarts }), view = await openPage(browser, device, Object.assign({ wait: false }, deviceHash)), page = view.page;
  await button(page, text("en", "ui.device_restart")).click();
  await dialogOf(page).waitFor();
  await confirmDialog(view, text("en", "ui.device_restart"));
  await panel(page).waitFor();
  assert.strictEqual((await marker(page)).uncertain, true, label + ": uncertain");
  if (restarts) {
    await page.locator("#v2-device-code").waitFor({ timeout: 20000 });
    await signIn(page);
    await page.locator('section.restart-watch[data-phase="final"]').waitFor({ timeout: 20000 });
    assert.strictEqual(await resultText(page), text("en", "ui.guided_outcome_restart_observed"), label + ": observed, not completed");
    assert(!(await panelText(page)).includes(text("en", "ui.guided_outcome_done")), label + ": never 'Restart complete.'");
  } else {
    await page.locator('section.restart-watch[data-outcome="not_restarted"]').waitFor({ timeout: 40000 });
    assert.strictEqual(await resultText(page), text("en", "ui.guided_outcome_not_restarted_uncertain"), label + ": unclear text");
    assert.strictEqual(await panelButton(view, "ui.guided_action_close").count(), 1, label + ": Close");
    assert.strictEqual(await panelButton(view, "ui.guided_action_retry").count(), 0, label + ": no Apply again");
  }
  assert.strictEqual(device.rebootCalls.length, 1, label + ": no second order");
  await finish(view, device, label);
}
// Restart from Maintenance with a request still open: held answers become 401 after the restart, navigation during the restart.
// Maintenance itself asks the device for nothing (no remount request), so the open request is the live poll.
async function c9OwnRequests(browser, route) {
  const label = "C9 " + route, device = newDevice({ auth: true }), view = await openPage(browser, device, { hash: "#" + route, wait: false }), page = view.page;
  await button(page, text("en", "ui.device_restart")).click();
  await dialogOf(page).waitFor();
  device.holdLiveNext = true;
  await page.evaluate(() => dispatchEvent(new Event("online")));
  await until(() => device.heldLive !== null, label + ": a poll is held", 9000);
  await confirmDialog(view, text("en", "ui.device_restart"));
  await panel(page).waitFor();
  assert.strictEqual(device.rebootCalls.length, 1, label + ": one order");
  await until(() => device.down, label + ": the device goes down");
  const mark = device.requests.length;
  await page.evaluate(() => { location.hash = "#device/diagnostics/log"; });
  await page.locator("main .restart-hold").waitFor();
  await page.evaluate(() => { location.hash = "#projects/p1"; });
  await page.waitForFunction(() => location.hash === "#projects/p1" && document.querySelector("main .restart-hold"));
  await until(() => device.rebootCount === 1, label + ": the device is back");
  device.releaseHeld(401);
  await page.locator('section.restart-watch[data-phase="back"]').waitFor({ timeout: 15000 });
  const later = device.requests.slice(mark).map(item => item.path);
  assert(!later.some(path => path.startsWith("/api/log") || path.startsWith("/api/tracking")), label + ": no route request " + JSON.stringify(later));
  assert.strictEqual(device.loginLoads, 0, label + ": no sign-in page before the device is back");
  assert.strictEqual(device.authedDuringHold, 0, label + ": no request with the session during the restart");
  await page.locator("#v2-device-code").waitFor({ timeout: 10000 });
  assert.strictEqual(device.loginLoads, 1, label + ": exactly one reload");
  assert.strictEqual(new URL(page.url()).hash, "#projects/p1", label + ": the last route stays");
  await signIn(page);
  assert.strictEqual(new URL(page.url()).hash, "#projects/p1", label + ": back on the last route");
  await page.locator('section.restart-watch[data-phase="final"][data-outcome="done"]').waitFor({ timeout: 20000 });
  assert.strictEqual(await resultText(page), text("en", "ui.guided_outcome_done"), label + ": complete once the device is ready");
  await finish(view, device, label);
}
// The restore answer is accepted but its body cannot be read: observed as uncertain, no notes, no refusal notice.
async function c10Truncated(browser) {
  const device = newDevice({ auth: true, applyMode: "truncated", restoreSections: ["connections"], restoreKept: ["wifi"] }), view = await openPage(browser, device, MAINTENANCE), page = view.page;
  await chooseBackup(view);
  await confirmDialog(view, restoreSubmit("en"));
  await panel(page).waitFor();
  assert.strictEqual((await marker(page)).uncertain, true, "C10: uncertain");
  assert.strictEqual(await restoreNotice(page).count(), 0, "C10: no refusal notice");
  await page.locator("#v2-device-code").waitFor({ timeout: 20000 });
  await signIn(page);
  await page.locator('section.restart-watch[data-outcome="connected"]').waitFor({ timeout: 20000 });
  assert.strictEqual(await resultText(page), fill(literal("en", "Backup restored: connected to ‘{ssid}’."), { ssid: "Field" }), "C10: the own result");
  const notes = await page.locator("section.restart-watch .restart-notes").textContent();
  assert(!notes.includes(text("en", "ui.guided_restore_older_backup")) && !notes.includes(text("en", "ui.guided_restore_kept_wifi")), "C10: no scope notes");
  assert.strictEqual(device.restoreCalls.length, 1, "C10: exactly one restore request");
  await finish(view, device, "C10");
}
// Restore from Maintenance after editing the form (#24-UI): leaving the dirty form asks first; the restore dialog then has no
// edits to discard. An accepted restore reloads once without an unload question; a refusal leaves the page usable.
async function c11EditedForm(browser) {
  for (const mode of ["ok", "lost"]) {
    const label = "C11 " + mode, device = newDevice({ auth: true, applyMode: mode }), view = await openPage(browser, device), page = view.page;
    await page.locator('input[name="ntrip_mount"]').fill("OTHER");
    assert.strictEqual(await dirty(page), "true", label + ": edited");
    await page.locator('.connections-backup-link a[href="/#device/maintenance"]').click();
    await page.waitForSelector("dialog[open]");
    assert.strictEqual((await page.locator("dialog[open] h2").textContent()).trim(), literal("en", "Discard unsaved changes?"), label + ": the guard asks before leaving the form");
    await confirmDialog(view, literal("en", "Discard changes"));
    await page.locator('[data-card="config-backup"]').waitFor();
    await chooseBackup(view);
    assert.strictEqual(await dialogText(page), literal("en", RESTORE_EN), label + ": nothing left to discard");
    await confirmDialog(view, restoreSubmit("en"));
    await panel(page).waitFor();
    await page.locator("#v2-device-code").waitFor({ timeout: 20000 });
    assert.strictEqual(device.loginLoads, 1, label + ": one reload");
    assert.strictEqual(view.counts.beforeunload, 0, label + ": no unload question");
    await finish(view, device, label);
  }
  {
    const device = newDevice({ auth: true, restoreAnswer: () => ({ status: 400, body: { error: "invalid_backup", message: "The backup is invalid: x." } }) });
    const view = await openPage(browser, device, MAINTENANCE), page = view.page;
    await chooseBackup(view);
    await confirmDialog(view, restoreSubmit("en"));
    await restoreNotice(page).waitFor({ state: "visible" });
    await page.locator('a[href="/#device"]:visible').first().click();
    await page.locator("nav.device-links").waitFor();
    assert.strictEqual(await page.locator("dialog[open]").count(), 0, "C11 400: no guard without a form");
    await finish(view, device, "C11 400");
  }
  for (const language of ["en", "de"]) {
    const label = "C11 cancel " + language, device = newDevice({ auth: true }), view = await openPage(browser, device, Object.assign({ language }, MAINTENANCE)), page = view.page;
    await chooseBackup(view);
    assert.strictEqual(await dialogText(page), literal(language, RESTORE_EN), label + ": the dialog has no sentence about discarded edits");
    await dialogOf(page).getByRole("button", { name: text(language, "action.cancel"), exact: true }).click();
    await page.waitForFunction(() => !document.querySelector("dialog[open]"));
    assert.strictEqual(device.restoreCalls.length, 0, label + ": no request");
    await finish(view, device, label);
  }
}

// ---- Part D: first setup end to end and the layout matrix ----
const AP = "http://192.168.4.1", MDNS = "http://rtk-test.local";
// Sign-in page of a context without a session: the device code, then the route that was asked for.
async function signInTo(page, ready) {
  await page.locator("#v2-device-code").waitFor({ timeout: 20000 });
  await page.locator("#v2-device-code").fill("ABC");
  await page.locator("button[type=submit]").click();
  await ready.waitFor({ timeout: 20000 });
}
const noSetupPage = (device, label) => assert(!device.requests.some(item => item.path === "/setup"), label + ": the legacy /setup page is never requested");

// D1: a factory device on the captive hotspot -> Wi-Fi only -> restart -> result by apply id -> field setup; later the
// portal under its own name only reports the neutral result of this device start.
async function d1Captive(browser, language) {
  const label = "D1 " + language, device = newDevice({ auth: true, config: clone(FACTORY), accessBefore: "SETUP", accessAfter: "ONLINE" });
  const view = await openPage(browser, device, { language, width: 360, origin: AP, auth: false, wait: false }), page = view.page;
  await signInTo(page, page.locator("form.configuration-layout"));
  await assertConfigRoute(page, label + ": the sign-in keeps the route");
  assert.strictEqual(await page.locator('input[name="ntrip_host"]').inputValue(), "", label + ": factory state");
  await saveWifi(view, "Barn");
  await panel(page).waitFor();
  assert.strictEqual(device.puts.length, 1, label + ": one PUT");
  assert.deepStrictEqual(device.puts[0].body, { wifi_pass: "secret123", wifi_ssid: "Barn" }, label + ": only the Wi-Fi fields are sent");
  assert.strictEqual(device.puts[0].query, "staging_revision=a1b2c3d4-0", label + ": the revision seen on opening");
  assert.strictEqual(device.applyCalls.length, 1, label + ": one apply");
  await page.locator('section.restart-watch[data-phase="back"]').waitFor({ timeout: 15000 });
  await signInTo(page, panel(page));
  await assertConfigRoute(page, label + ": the second sign-in keeps the route");
  await page.locator('section.restart-watch[data-phase="final"][data-outcome="connected"]').waitFor({ timeout: 20000 });
  assert.strictEqual(await resultText(page), fill(literal(language, "Applied: connected to ‘{ssid}’. Portal: http://{host}/"), { ssid: "Barn", host: "rtk-test.local" }), label + ": the own result");
  await panelButton(view, "ui.guided_action_field_setup").click();
  await page.waitForFunction(() => location.hash === "#measure/setup");
  await page.locator('.field-setup-layout [data-card="field-setup"]').waitFor({ state: "visible" });
  assert.strictEqual(await panel(page).count(), 0, label + ": the panel is closed");
  assert.strictEqual(await marker(page), null, label + ": the marker is gone");
  assert.strictEqual(device.signIns, 2, label + ": two sign-ins");
  noSetupPage(device, label);
  assert.deepStrictEqual(view.errors, [], label + ": page errors");
  await view.context.close();
  // The portal under its own name: a new origin has no marker, so the page only reports what this device start recorded.
  const later = await openPage(browser, device, { language, width: 360, origin: MDNS, auth: false, wait: false });
  await signInTo(later.page, later.page.locator("form.configuration-layout"));
  const notice = later.page.locator('.notice[data-notice="last-change"]');
  await notice.waitFor({ state: "visible" });
  const reported = fill(literal(language, "connected to ‘{ssid}’"), { ssid: "Barn" });
  assert.strictEqual((await notice.locator("p").first().textContent()).trim(), fill(literal(language, "Configuration result since the last device start: {reported}."), { reported }), label + ": neutral result");
  assert.strictEqual(await panel(later.page).count(), 0, label + ": no panel in the new context");
  assert.strictEqual(device.applyCalls.length, 1, label + ": nothing applied again");
  noSetupPage(device, label + " later");
  await finish(later, device, label);
}

// D2: the QR code opens the portal with the device code in the fragment: automatic sign-in, then the Field App form.
async function d2QrEntry(browser) {
  const device = newDevice({ auth: true }), view = await openPage(browser, device, { origin: MDNS, auth: false, wait: false, hash: "#code=CODE" }), page = view.page;
  await page.waitForURL(/signed_in=1/, { timeout: 20000 });
  await page.locator("#app-main").waitFor();
  assert.deepStrictEqual(device.signInBodies.map(item => item.device_code), ["CODE"], "D2: one sign-in with the code of the QR");
  assert.strictEqual(device.loginLoads, 1, "D2: the sign-in page loaded once");
  await page.evaluate(() => { location.hash = "#device/configuration"; });
  await page.locator("form.configuration-layout").waitFor();
  assert.strictEqual(await page.locator('input[name="wifi_ssid"]').count(), 1, "D2: the Field App form");
  noSetupPage(device, "D2");
  await finish(view, device, "D2");
}

// D3: no horizontal scrolling, 48 px targets, a panel that stays inside the view above the navigation and the feedback dock,
// and a symbol plus a word for every step.
async function layoutProblems(page) {
  return page.evaluate(() => {
    const out = [], seen = { buttons: 0, steps: 0, panel: 0, dock: 0 }, visible = node => node.getClientRects().length > 0 && getComputedStyle(node).visibility !== "hidden";
    if (document.documentElement.scrollWidth > innerWidth) out.push("horizontal scrolling " + document.documentElement.scrollWidth + " > " + innerWidth);
    for (const area of document.querySelectorAll(".config-review, .config-staged, .config-apply, .restart-watch")) {
      for (const control of area.querySelectorAll("button, .btn")) {
        if (!visible(control)) continue;
        seen.buttons += 1;
        const box = control.getBoundingClientRect();
        if (box.width < 47.99 || box.height < 47.99) out.push("target " + (control.textContent || "").trim() + " " + Math.round(box.width) + "x" + Math.round(box.height));
      }
    }
    const watch = document.querySelector(".restart-watch");
    if (watch) {
      seen.panel = 1;
      const box = watch.getBoundingClientRect(), nav = document.querySelector(".mobile-nav");
      if (box.left < -0.5 || box.top < -0.5 || box.right > innerWidth + 0.5 || box.bottom > innerHeight + 0.5) out.push("panel outside the view " + JSON.stringify(box));
      if (innerWidth < 900 && nav && getComputedStyle(nav).display !== "none" && box.bottom > nav.getBoundingClientRect().top + 0.5) out.push("panel covers the navigation");
      const dock = document.getElementById("feedback-dock");
      if (dock) {
        seen.dock = 1;
        const item = document.createElement("div");
        item.className = "feedback-item"; item.textContent = "dock"; dock.append(item); dock.hidden = false;
        scrollTo(0, document.documentElement.scrollHeight);
        const d = dock.getBoundingClientRect();
        if (d.left < box.right - 0.5 && d.right > box.left + 0.5 && d.top < box.bottom - 0.5 && d.bottom > box.top + 0.5) out.push("panel covers the feedback dock " + JSON.stringify([d, box]));
        item.remove(); dock.hidden = true; scrollTo(0, 0);
      }
      for (const item of watch.querySelectorAll("li[data-state]")) {
        seen.steps += 1;
        const mark = item.querySelector(".restart-mark"), word = item.querySelector("b");
        if (!mark || !visible(mark) || mark.textContent.trim().length < 1) out.push("step without a symbol: " + item.dataset.step);
        if (!word || !visible(word) || word.textContent.trim().length <= 1) out.push("step without a word: " + item.dataset.step);
      }
    }
    return { out, seen };
  });
}
// The check must have looked at something: targets in every state with actions (a running watch has none), and steps and the dock wherever the panel is.
async function expectLayout(view, label, { targets = true } = {}) {
  const { out, seen } = await layoutProblems(view.page), panelShown = (await panel(view.page).count()) > 0;
  assert.deepStrictEqual(out, [], label + ": layout");
  if (targets) assert(seen.buttons > 0, label + ": targets were measured");
  if (panelShown) assert(seen.panel === 1 && seen.dock === 1 && seen.steps >= 3, label + ": panel, dock and steps were measured " + JSON.stringify(seen));
}
const notesOf = async page => (await page.locator("section.restart-watch .restart-notes li").allTextContents()).map(item => item.trim());

// The form states: staged on opening, the open review with its warning and the earlier list, "changed in the meantime".
async function d3Forms(browser, language, width) {
  const label = "D3 " + language + "/" + width;
  {
    const device = newDevice({ auth: true, staged: { wifi_networks: [{ ssid: "Fallback" }] }, stagingSerial: 1, measuring: true,
      activeLine: { state: "recording", name: "Line 7" } });
    const view = await openPage(browser, device, { language, width }), page = view.page;
    await page.locator("section.config-staged").waitFor({ state: "visible" });
    await page.locator('input[name="wifi_ssid"]').fill("NewNet");
    await check(view).click();
    const review = page.locator('.config-review[data-restart="true"]');
    await review.waitFor({ state: "visible" });
    const content = await review.textContent();
    assert(content.includes(text(language, "ui.guided_warn_measurement")), label + ": the review warns");
    assert(content.includes(text(language, "ui.guided_review_earlier").split("{labels}")[0]), label + ": the earlier list");
    assert.strictEqual(await page.locator("section.config-staged").isVisible(), true, label + ": staged notice beside the review");
    await expectLayout(view, label + " review");
    await finish(view, device, label + " review");
  }
  {
    const device = newDevice({ auth: true, staged: { wifi_ssid: "Barn" }, stagingSerial: 1 });
    const view = await openPage(browser, device, { language, width }), page = view.page;
    await page.locator("section.config-staged").waitFor({ state: "visible" });
    await expectLayout(view, label + " staged");
    await page.locator("section.config-staged").getByRole("button", { name: text(language, "ui.guided_apply_now"), exact: true }).click();
    const block = page.locator("section.config-apply");
    await block.waitFor({ state: "visible" });
    device.staged.wifi_ssid = "Other"; device.stagingSerial = 2;
    await block.getByRole("button", { name: text(language, "ui.guided_apply_all"), exact: true }).click();
    await page.waitForFunction(() => { const node = document.querySelector("section.config-apply"); return node && node.dataset.changed === "true"; });
    await expectLayout(view, label + " changed in the meantime");
    await finish(view, device, label + " changed");
  }
}

// The panel states: failed with every note, observed, the settling of a restart, and the hold with its placeholder.
async function d3Panels(browser, language, width) {
  const label = "D3 " + language + "/" + width, open = (device, init) => openPage(browser, device, { language, width, init });
  {
    const failure = { status: "failed", details: { code: "authentication_failed", restored: ["wifi_ssid"] } };
    const device = newDevice({ auth: true, pendingMs: 0, outcome: failure, ntripAfter: "auth_error",
      configWarnings: [{ key: "config_file", code: "unreadable" }] });
    device.booted(OWN_ID);
    const view = await open(device, plant({ phase: "back", attempt: { keys: ["ntrip_mount", "wifi_pass", "wifi_ssid"], wifi_ssid: "NewNet", ntrip_mount: "MOUNT" } })), page = view.page;
    await page.locator('section.restart-watch[data-outcome="failed"]').waitFor({ timeout: 15000 });
    // Decision, reported settings, unreadable file, connection now and the caster note.
    await page.waitForFunction(() => document.querySelectorAll("section.restart-watch .restart-notes li").length >= 5);
    await step(page, "ntrip", "fail").waitFor();
    await expectLayout(view, label + " failed");
    await finish(view, device, label + " failed");
  }
  {
    const device = newDevice({ auth: true, pendingMs: 0, outcome: { status: "connected", details: { ssid: "NewNet" }, apply_id: "fedcba9876543210" } });
    device.booted(OWN_ID);
    const view = await open(device, plant({ phase: "back" })), page = view.page;
    await page.locator('section.restart-watch[data-outcome="observed"]').waitFor({ timeout: 15000 });
    await expectLayout(view, label + " observed");
    await finish(view, device, label + " observed");
  }
  {
    const device = newDevice({ auth: true, loadingMs: 30000 });
    device.booted(undefined);
    const view = await open(device, plant({ kind: "reboot", phase: "settling", attempt: null, target_ssid: null })), page = view.page;
    await page.locator('section.restart-watch[data-phase="settling"]').waitFor({ timeout: 15000 });
    await step(page, "storage", "wait").waitFor({ timeout: 15000 });
    await expectLayout(view, label + " settling", { targets: false });
    await finish(view, device, label + " settling");
  }
  {
    // The boot check of a resumed watch is held, so the placeholder and the panel stay on screen.
    const device = newDevice({ auth: true, holdLiveNext: true });
    const view = await openPage(browser, device, { language, width, init: plant({ phase: "restarting" }), wait: false }), page = view.page;
    await until(() => device.heldLive !== null, label + ": the boot check runs");
    await page.locator("main .restart-hold").waitFor({ state: "visible" });
    await panel(page).waitFor({ state: "visible" });
    await expectLayout(view, label + " hold", { targets: false });
    device.releaseHeld();
    await finish(view, device, label + " hold");
  }
}
const WIDTHS = [320, 360, 390, 480, 1280];

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    for (const language of ["en", "de"]) await a1Dirty(browser, language);
    await a1bBeforeUnload(browser);
    await a2Factory(browser);
    await a3NoChange(browser);
    for (const language of ["en", "de"]) await a4CasterOnly(browser, language);
    await a5FieldError(browser);
    await a6MaskedNetworks(browser);
    for (const language of ["en", "de"]) await a7Texts(browser, language);
    await a8SavedPassword(browser, false);
    await a8SavedPassword(browser, true);
    await a9LockedWhileSaving(browser);
    await a10FallbackPasswords(browser);
    await a11StagedOnOpen(browser);
    await a12StagingChanged(browser);
    await a13WithoutStaging(browser);
    for (const language of ["en", "de"]) await b1Success(browser, language);
    await b2Failure(browser);
    await b2Variants(browser);
    await b3Timeout(browser);
    await b4Refused(browser);
    await b5OldResult(browser);
    await b6NoRestart(browser);
    await b7ReloadWhileVerifying(browser);
    await b8WithoutMarker(browser);
    await b9LateUnauthorized(browser);
    await b10Events(browser);
    await b11Second(browser);
    for (const mode of ["lost", "truncated"]) await b12LostAnswer(browser, mode);
    await b14Requested(browser);
    await b15EarlyResult(browser);
    await b16ReplacedStaging(browser);
    await b17Actions(browser);
    for (const mode of ["slow_body", "slow_body_destroyed"]) await b18OpenBody(browser, mode);
    await b19Foreign(browser);
    for (const variant of ["warn", "plain", "de"]) await b21Unexpected(browser, variant);
    for (const language of ["en", "de"]) await c1RestoreSuccess(browser, language);
    await c2RestoreOld(browser);
    await c3c4RestoreRefused(browser);
    await c5Reboot(browser);
    for (const language of ["en", "de"]) await c6Warnings(browser, language);
    await c7RestoreLost(browser);
    for (const restarts of [true, false]) await c8RebootLost(browser, restarts);
    // Restarting lives under Maintenance only (#24-UI E9, E19).
    await c9OwnRequests(browser, "device/maintenance");
    await c10Truncated(browser);
    await c11EditedForm(browser);
    for (const language of ["en", "de"]) await d1Captive(browser, language);
    await d2QrEntry(browser);
    for (const language of ["en", "de"]) {
      await Promise.all(WIDTHS.map(width => d3Forms(browser, language, width)));
      await Promise.all(WIDTHS.map(width => d3Panels(browser, language, width)));
    }
  } finally { await browser.close(); }
  console.log("guided-apply-browser=ok");
})().catch(error => { console.error(error); process.exitCode = 1; });
