// SPDX-License-Identifier: AGPL-3.0-only
const { chromium } = require("playwright");
const assert = require("assert");
const expectedVersion = require("fs").readFileSync("assets.py", "utf8").match(/ASSET_VERSION = "([^"]+)"/)[1];

const base = process.env.RTK_DEVICE_URL;
if (!base) throw new Error("Set RTK_DEVICE_URL to the device HTTP origin");
const code = process.env.RTK_DEVICE_CODE;
if (!code) throw new Error("RTK_DEVICE_CODE is required");

async function settle(page, hash, selector) {
  await page.evaluate((value) => { location.hash = value; }, hash);
  await page.waitForSelector(selector, { timeout: 10000 });
  await page.waitForFunction(() => document.querySelector("#app-main").getAttribute("aria-busy") === "false");
}

(async () => {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 360, height: 900 }, acceptDownloads: true });
  const page = await context.newPage(), errors = [], failures = [];
  page.on("pageerror", error => errors.push(error.message));
  page.on("requestfailed", request => {
    const failure = request.failure().errorText;
    if (failure === "net::ERR_ABORTED" && ["/api/ui/live", "/status"].some(path => request.url().includes(path))) return;
    failures.push(`${request.method()} ${request.url()} ${failure}`);
  });
  await page.goto(base + "/", { waitUntil: "domcontentloaded" });
  if (await page.locator("#v2-login-form").count()) {
    await page.locator("#v2-device-code").fill(code);
    await page.getByRole("button", { name: /Open Field App|F\u0065ld-App \u00f6ffnen/ }).click();
  }
  await page.waitForSelector(".readiness-hero", { timeout: 15000 });
  assert.strictEqual(await page.locator("#view-back").count(), 0);
  assert.strictEqual(await page.locator('#mobile-nav [data-route="status"]').getAttribute("aria-current"), "page");
  const contract = await page.evaluate(async () => {
    const [status, tracking, live] = await Promise.all([
      fetch("/status").then(response => response.json()),
      fetch("/api/tracking").then(response => response.json()),
      fetch("/api/ui/live").then(response => response.json()),
    ]);
    return {
      asset: Array.from(document.scripts).map(script => script.src).find(src => src.includes("/app.js?v="))?.split("?v=")[1],
      status: ["quality_streak", "log_entries", "rtcm"].every(key => key in status),
      tracking: ["device_time", "projects", "measurement_progress", "quality_streak"].every(key => key in tracking),
      live: ["current_fix", "current_quality", "measurement_progress", "board"].every(key => key in live),
      project: tracking.active_project_id,
      projectHasGeometry: Boolean((tracking.projects || []).find(project => project.id === tracking.active_project_id && (project.line_count || project.point_count))),
      inactiveProject: (tracking.projects || []).find(project => project.id !== tracking.active_project_id)?.id || null,
    };
  });
  assert.strictEqual(contract.asset, expectedVersion);
  assert(contract.status && contract.tracking && contract.live);
  const cancellation = await page.evaluate(async () => {
    const session = await fetch("/api/session").then(response => response.json());
    const response = await fetch("/api/tracking", { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": session.csrf_token }, body: JSON.stringify({ action: "cancel_measurement" }) });
    return response.json();
  });
  assert.strictEqual(cancellation.cancelled, true);
  const tcpAuthRoundTrip = await page.evaluate(async () => {
    const session = await fetch("/api/session").then(response => response.json());
    const headers = { "Content-Type": "application/json", "X-CSRF-Token": session.csrf_token };
    const initial = await fetch("/api/tcp-auth").then(response => response.json());
    const disabled = await fetch("/api/tcp-auth", { method: "PUT", headers, body: JSON.stringify({ required: false }) }).then(response => response.json());
    const accessDisabled = await fetch("/api/access").then(response => response.json());
    const restored = await fetch("/api/tcp-auth", { method: "PUT", headers, body: JSON.stringify({ required: true }) }).then(response => response.json());
    return { initial, disabled, accessDisabled: accessDisabled.nmea_tcp, restored };
  });
  assert.strictEqual(tcpAuthRoundTrip.initial.required, true);
  assert.strictEqual(tcpAuthRoundTrip.disabled.required, false);
  assert.strictEqual(tcpAuthRoundTrip.accessDisabled.authentication_required, false);
  assert.strictEqual(tcpAuthRoundTrip.accessDisabled.authentication, null);
  assert.strictEqual(tcpAuthRoundTrip.restored.required, true);
  const routes = [
    ["status", ".readiness-hero"], ["projects", ".project-list"], ["device", ".device-links"],
    ["device/rtcm", ".stream-hero"], ["device/access", ".access-grid"], ["device/log", ".log-console"],
    ["device/module", ".command-panel"], ["device/configuration", ".configuration-layout"],
    ["device/diagnostics", ".diagnostics-summary"], ["device/label", ".label-grid"], ["device/settings", ".settings-layout"],
  ];
  assert.strictEqual(await page.locator('script[src*="/qr.js?"]').count(), 0);
  for (const width of [320, 360, 480, 1024, 1440]) {
    await page.setViewportSize({ width, height: width < 700 ? 900 : 1000 });
    for (const [hash, selector] of routes) {
      await settle(page, hash, selector);
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${hash} overflows at ${width}px`);
      assert.notStrictEqual(await page.locator("#live-state b").innerText(), "Connecting");
    }
  }
  await page.setViewportSize({ width: 360, height: 900 });
  await settle(page, "status", ".readiness-hero");
  assert((await page.locator(".readiness-hero").boundingBox()).height < 260);
  assert.strictEqual(await page.locator(".readiness-hero .metric").count(), 4);
  const compact = await page.evaluate(() => ({ header: document.querySelector(".app-header").getBoundingClientRect().height, footer: document.querySelector(".mobile-nav").getBoundingClientRect().height, status: document.querySelector("#live-state").getBoundingClientRect().height }));
  assert(compact.header <= 65 && compact.footer <= 61 && compact.status >= 48);
  assert.strictEqual(await page.locator(`script[src*="/qr.js?v=${expectedVersion}"]`).count(), 1);
  await settle(page, "device", ".device-links");
  assert.strictEqual(await page.locator(".device-links a").count(), 7);
  assert((await page.locator(".device-identity").innerText()).includes("V" + expectedVersion));
  await settle(page, "device/configuration", ".configuration-layout");
  assert(await page.locator('textarea[name="wifi_networks"]').inputValue() !== "");
  await page.getByRole("button", { name: "Scan networks" }).click();
  await page.waitForSelector("dialog[open]", { timeout: 20000 });
  await page.getByRole("button", { name: "Cancel" }).click();
  await settle(page, "device/settings", ".settings-layout");
  assert.strictEqual(await page.getByRole("button", { name: "English" }).getAttribute("aria-pressed"), "true");
  assert.strictEqual(await page.locator(".measurement-choice button").count(), 4);
  assert(await page.getByRole("button", { name: "Until quality is OK" }).isVisible());
  const gateRoundTrip = await page.evaluate(async () => {
    const session = await fetch("/api/session").then(response => response.json());
    const headers = { "Content-Type": "application/json", "X-CSRF-Token": session.csrf_token };
    const original = await fetch("/api/measurement-gate").then(response => response.json());
    const changed = Object.assign({}, original.values, { max_hdop: original.values.max_hdop + .1 });
    const saved = await fetch("/api/measurement-gate", { method: "PUT", headers, body: JSON.stringify(changed) }).then(response => response.json());
    const reset = await fetch("/api/measurement-gate", { method: "DELETE", headers: { "X-CSRF-Token": session.csrf_token } }).then(response => response.json());
    return { original: original.values, defaults: original.defaults, saved: saved.values, reset: reset.values };
  });
  assert.strictEqual(gateRoundTrip.saved.max_hdop, gateRoundTrip.original.max_hdop + .1);
  assert.deepStrictEqual(gateRoundTrip.reset, gateRoundTrip.defaults);
  const contrast = page.getByRole("button", { name: "High contrast off" });
  assert.strictEqual(await contrast.getAttribute("aria-pressed"), "false");
  await contrast.click();
  assert.strictEqual(await page.getByRole("button", { name: "High contrast on" }).getAttribute("aria-pressed"), "true");
  await page.getByRole("button", { name: "Deutsch" }).click();
  assert.strictEqual(await page.getByRole("button", { name: "Deutsch" }).getAttribute("aria-pressed"), "true");
  await settle(page, "projects", ".project-list");
  const projectText = await page.locator(".project-list").innerText();
  assert(!projectText.includes("1970") && !projectText.includes("1996"));
  await settle(page, "device/rtcm", ".stream-hero");
  assert(["Strom steht", "Strom unterbrochen"].includes(await page.locator(".stream-hero h2").innerText()));
  await settle(page, "device/diagnostics", ".diagnostics-summary");
  assert.strictEqual(await page.locator(".diagnostics-console").count(), 0);
  if (contract.inactiveProject) {
    await settle(page, `projects/${contract.inactiveProject}`, ".map-workspace");
    assert(await page.locator(".map-panel > .btn.primary").isVisible());
    assert.strictEqual(await page.locator(".map-tools .btn").count(), 5);
    const stillActive = await page.evaluate(() => fetch("/api/tracking").then(response => response.json()).then(value => value.active_project_id));
    assert.strictEqual(stillActive, contract.project);
  }
  if (contract.project) {
    await settle(page, `projects/${contract.project}`, ".map-workspace");
    assert.strictEqual(await page.locator('#mobile-nav [data-route="projects"]').getAttribute("aria-current"), "page");
    if (contract.projectHasGeometry) {
      assert.strictEqual(await page.locator(".survey-map").evaluate(node => node.namespaceURI), "http://www.w3.org/2000/svg");
      assert((await page.locator(".map-line, .map-point").count()) > 0);
      assert((await page.locator(".map-line, .map-point").first().evaluate(node => {
        const box = node.getBBox(); return box.width > 0 || box.height > 0;
      })));
    }
  }
  await page.waitForTimeout(2500);
  assert.deepStrictEqual(errors, []);
  assert.deepStrictEqual(failures, []);
  const removed = await context.request.get(base + "/v0");
  assert.strictEqual(removed.status(), 404);
  await browser.close();
  console.log(JSON.stringify({ device: base, asset: contract.asset, widths: 5, routes: routes.length, project: Boolean(contract.project), removedV0Status: removed.status() }));
})().catch(error => { console.error(error); process.exit(1); });
