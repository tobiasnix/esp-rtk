// SPDX-License-Identifier: AGPL-3.0-only
const { chromium } = require("playwright");
const fs = require("fs"),
  assert = require("assert"),
  root = process.cwd();
const html = fs.readFileSync("/tmp/app-v2-local.html", "utf8");
const status = {
  system: {
    display_name: "RTK-TEST",
    version: "VTEST",
    uptime_sec: 120,
    reset_cause: "HARD",
    ip: "192.0.2.1",
  },
  board: {
    fs_total_bytes: 1000000,
    fs_free_bytes: 600000,
    ram_free_bytes: 7000000,
    wifi_rssi: -55,
    mcu_temp_c: 42,
    cpu_hz: 240000000,
  },
  stats: {
    wifi_ssid_aktiv: "Field",
    ble_auth_failures: 1,
    tcp_auth_failures: 2,
    ntrip_gga_sent: 3,
    ntrip_bytes: 4096,
  },
  fix: { qual: 1, fix_status_text: "GPS_FIX", sats: 12 },
  ble: { clients: 1 },
  nmea_tcp: { clients: 2, port: 10110 },
  rtcm: { msm: ["MSM5"], mount: "TEST" },
  log_entries: 4,
};
const tracking = {
  device_time: 2000000,
  active_project_id: "p1",
  capacity: {used:31,limit:5000,remaining:4969,percent:1,level:"ok",writable:true},
  projects: [
    {
      id: "p1",
      name: "Project",
      line_count: 1,
      point_count: 1,
      created_at: 900,
      updated_at: 990,
    },
    { id: "p2", name: "Archive", line_count: 0, point_count: 0, created_at: 800, updated_at: 850 },
  ],
  active_project_lines: [
    {
      id: "l1",
      name: "Line",
      state: "finished",
      vertex_count: 30,
      length_m: 12.3,
    },
  ],
  control_checks: [{ name: "C1", measurements: 2, horizontal_delta_m: 0.01 }],
  active_line: null,
  current_quality: {
    accepted: false,
    limits: {
      fix_quality: "RTK_FIXED",
      max_gst_horizontal_sigma_m: 0.05,
      max_gst_vertical_sigma_m: 0.1,
      min_satellites: 10,
      max_hdop: 1.5,
      max_correction_age_sec: 5,
      max_horizontal_spread_m: 0.03,
    },
  },
};
const live = {
  schema_version: 1,
  current_fix: {
    qual: 1,
    fix_status_text: "GPS_FIX",
    sats: 12,
    hdop: 0.8,
    lat: 50,
    lon: 8,
    alt: 100,
    receiver_accuracy: { horizontal_sigma_m: 0.2, altitude_sigma_m: 0.3 },
  },
  current_quality: tracking.current_quality,
  quality_streak: { gate_streak_sec: 20 },
  measurement_progress: { active: false, collected: 0, total: 5 },
  rtcm: { mount: "TEST", last_msg_age_sec: 9 },
  board: status.board,
  capacity: tracking.capacity,
  active_project: { id: "p1", name: "Project", line_count: 1, point_count: 1 },
  active_line: null,
};
const fieldStatus={enabled:false,error:null,samples_this_boot:0,stored_bytes:0,limit_bytes:4194304};
const fieldMarkers=[];
let wifiScanCalls = 0, failNextStatus = false, failLive = false, failProgress = false, finishMeasurement = null;
let projectDelay=0,mapDelay=0,mapRequests=0;
async function fixture(route) {
  const url = new URL(route.request().url()),
    path = url.pathname,
    method = route.request().method();
  if (path === "/")
    return route.fulfill({ contentType: "text/html", body: html });
  if (["/app.js", "/app.css", "/i18n.js", "/qr.js"].includes(path))
    return route.fulfill({
      contentType: path.endsWith(".js") ? "application/javascript" : "text/css",
      body: fs.readFileSync(root + path, "utf8"),
    });
  if(path==="/api/field-diagnostics"){
    if(method==="POST"){
      assert.strictEqual(route.request().headers()["x-csrf-token"],"csrf");
      const value=route.request().postDataJSON();
      if(value.action==="start")fieldStatus.enabled=true;
      if(value.action==="stop")fieldStatus.enabled=false;
      if(value.action==="mark")fieldMarkers.push(value.label);
      live.field_diagnostics=fieldStatus;
    }
    return route.fulfill({contentType:"application/json",body:JSON.stringify(fieldStatus)});
  }
  if(path==="/api/tracking"&&method==="GET"&&projectDelay)await new Promise(resolve=>setTimeout(resolve,projectDelay));
  if(path==="/api/tracking/map"){mapRequests++;if(mapDelay)await new Promise(resolve=>setTimeout(resolve,mapDelay));}
  let body = {};
  if (path === "/api/ui/live" && failLive) return route.abort("connectionfailed");
  if (path === "/api/ui/live") body = live;
  else if (path === "/status" && failNextStatus) {
    failNextStatus = false;
    return route.fulfill({ status: 503, contentType: "application/json", body: '{"message":"temporary"}' });
  }
  else if (path === "/status") body = status;
  else if (path === "/api/tracking/map")
    body = url.searchParams.get("project_id") === "p2" ? { lines: [], points: [], truncated: false } : {
      lines: [
        {
          id: "l1",
          name: "Line",
          coordinates: [
            [8, 50, 100],
            [8.001, 50.001, 101],
          ],
        },
      ],
      points: [{ id: "a1", type: "valve", name: "Valve 1", note: "North side", coordinate: [8, 50, 100], chainage_m: 2.5,
        receiver_accuracy: { horizontal_sigma_m: 0.012, altitude_sigma_m: .024 },
        measurement: { fix_quality: 4, fix_status: "RTK_FIXED", satellites: 18,
          hdop: .7, correction_age_sec: .8, station_id: "0054",
          receiver_accuracy: { source: "NMEA_GST", horizontal_sigma_mean_m: .012,
            vertical_sigma_mean_m: .024, samples: 5, expected_samples: 5 },
          accuracy_observation: { samples: 5, duration_sec: 4.2 },
          quality: { accepted: true, reasons: [] } } }],
    };
  else if (path === "/api/tracking/line") {
    const offset = Number(url.searchParams.get("offset") || 0);
    body = {
      id: "l1",
      name: "Line",
      vertex_count: 30,
      quality_accepted_count: 30,
      quality_total_count: 30,
      warnings: [],
      vertices: [
        {
          number: offset + 1,
          fix_status: "RTK_FIXED",
          satellites: 18,
          hdop: 0.7,
          correction_age_sec: 1,
          segment_length_m: 1,
          receiver_accuracy: {
            horizontal_sigma_m: 0.01,
            altitude_sigma_m: 0.02,
          },
        },
      ],
      has_more_vertices: offset === 0,
    };
  } else if (path === "/api/tracking" && method === "POST" && route.request().postDataJSON().action === "add_vertex") {
    await new Promise(resolve => { finishMeasurement = resolve; });
    body = { tracking_live: live };
  } else if (path === "/api/tracking" && url.searchParams.get("view") === "progress") {
    if (failProgress) return route.abort("connectionfailed");
    body = { current_fix: live.current_fix, current_quality: live.current_quality,
      measurement_progress: live.measurement_progress };
  } else if (path === "/api/tracking") body = tracking;
  else if (path === "/api/session") body = { csrf_token: "csrf" };
  else if (path === "/api/config")
    body = {
      wifi_ssid: "Field",
      wifi_pass_set: true,
      wifi_networks: [{ ssid: "Fallback", password_set: true }],
      ntrip_host: "caster",
      ntrip_port: 2101,
      ntrip_mount: "MOUNT",
      ntrip_user: "user",
      ntrip_pass_set: true,
    };
  else if (path === "/api/config-result") body = { status: "connected" };
  else if (path === "/api/measurement-gate") body = {
    values: { required_fix: "RTK_FIXED", min_satellites: 10, max_hdop: 1.5,
      max_correction_age_sec: 5, max_horizontal_spread_m: .05,
      max_gst_horizontal_sigma_m: .05, max_gst_vertical_sigma_m: .1 },
    defaults: { required_fix: "RTK_FIXED", min_satellites: 10, max_hdop: 1.5,
      max_correction_age_sec: 5, max_horizontal_spread_m: .05,
      max_gst_horizontal_sigma_m: .05, max_gst_vertical_sigma_m: .1 },
  };
  else if (path === "/api/tcp-auth") body = { required: true, default: true };
  else if (path === "/api/config/apply") body = { accepted: true };
  else if (path === "/api/wifi-scan") {
    wifiScanCalls += 1;
    body = wifiScanCalls === 1 ? { status: "pending" } : {
      status: "ready", networks: [{ ssid: "Visible", rssi: -48 }],
    };
  }
  else if (path === "/api/log")
    body = {
      entries: [
        { elapsed_sec: 1, level: "INFO", source: "SYS", message: "ready" },
      ],
      cursor: "0:1",
      generation: 0,
    };
  else if (path === "/api/access")
    body = {
      hostname: "rtk.local",
      nmea_tcp: {
        canonical_port: 10110,
        legacy_ports: [2947],
        authentication_required: true,
        authentication: "AUTH token",
      },
      ble_nus: {
        name: "RTK",
        service_uuid: "service",
        rx_uuid: "rx",
        tx_uuid: "tx",
        mitm_protection: "passkey",
      },
    };
  else if (path === "/api/label")
    body = {
      device_code: "CODE",
      ble_pin: "123456",
      stream_token: "TOKEN",
      ap_ssid: "RTK",
      ap_password: "PASSWORD",
    };
  else if (path === "/rtcm")
    body = {
      last_msg_age_sec: 1,
      mount: "MOUNT",
      msm: ["MSM5"],
      messages: { 1075: 2 },
    };
  else if (path === "/api/diagnostics") body = { healthy: true };
  else if (path === "/gnss")
    return route.fulfill({ contentType: "text/plain", body: "receiver ready" });
  else if (path === "/errors")
    return route.fulfill({
      contentType: "text/plain",
      body: "[1] INFO SYS ready",
    });
  else if (path.includes("/export") || path.includes("/backup"))
    return route.fulfill({ contentType: "application/json", body: "{}" });
  return route.fulfill({
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}
async function go(page, hash, selector) {
  await page.evaluate((value) => (location.hash = value), hash);
  await page.waitForSelector(selector);
  await page.waitForFunction(
    () =>
      document.querySelector("#app-main").getAttribute("aria-busy") === "false",
  );
}
(async () => {
  const browser = await chromium.launch({ headless: true }),
    page = await browser.newPage({
      viewport: { width: 360, height: 900 },
      acceptDownloads: true,
    });
  await page.addInitScript(() => {
    Object.defineProperty(navigator, "wakeLock", {
      value: {
        request: async () => ({
          release: async () => {},
          addEventListener: () => {},
        }),
      },
    });
  });
  await page.route("http://local.test/**", fixture);
  await page.goto("http://local.test/");
  await page.waitForSelector(".readiness-hero");
  if(process.argv.includes("--project-loading-only")){
    const errors=[];page.on("pageerror",error=>errors.push(error.message));
    failLive=true;projectDelay=8500;mapDelay=8500;
    await go(page,"projects/p1",".map-workspace");
    assert.strictEqual(mapRequests,1,"slow project reads must complete without restarting");
    await page.waitForFunction(()=>document.querySelector(".quality-chip").textContent==="Live status unavailable");
    assert.strictEqual(await page.locator(".map-line").count(),1);
    assert.strictEqual(await page.locator(".map-point").count(),1);
    assert(await page.locator(".map-position").evaluate(node=>node.hidden));
    await page.locator(".line-row").click();
    await page.waitForSelector(".line-quality");
    failLive=false;live.current_quality.accepted=true;live.current_fix.qual=4;
    await page.waitForFunction(()=>document.querySelector(".quality-chip").textContent==="RTK FIXED");
    projectDelay=0;mapDelay=1000;
    await go(page,"projects",".project-list");
    const pending=page.waitForRequest(request=>request.url().includes("/api/tracking/map"));
    await page.evaluate(()=>location.hash="projects/p1");await pending;
    await go(page,"device",".device-links");
    await page.waitForTimeout(1300);
    assert.strictEqual(await page.locator(".map-workspace").count(),0,"cancelled map must not replace the new page");
    assert.strictEqual(await page.locator(".device-links").count(),1);
    assert.deepStrictEqual(errors,[]);
    await browser.close();console.log("project-loading=ok (8.5s overview + 8.5s map, independent live failure/recovery, details, navigation cancellation)");return;
  }
  if(process.argv.includes("--field-diagnostics-only")){
    const errors=[];page.on("pageerror",error=>errors.push(error.message));
    await page.evaluate(()=>localStorage.setItem("rtk-language","de"));
    await page.reload();
    await go(page,"device/diagnostics",".field-diagnostics-card");
    const card=page.locator(".field-diagnostics-card");
    for(const width of [320,360,1280]){
      await page.setViewportSize({width,height:900});
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),`diagnostics overflow ${width}`);
    }
    await page.setViewportSize({width:360,height:900});
    await card.getByRole("button",{name:"Felddiagnose starten",exact:true}).click();
    await page.waitForFunction(()=>document.querySelector("#live-state small").textContent.includes("DIAG an"));
    assert(await card.getByRole("button",{name:"Felddiagnose starten",exact:true}).isDisabled());
    await card.locator("select").selectOption("stationary");
    await card.getByRole("button",{name:"Markierung setzen",exact:true}).click();
    assert.deepStrictEqual(fieldMarkers,["stationary"]);
    live.active_line={id:"test",name:"Route",state:"recording",recording_mode:"route",recording_sec:12};
    live.current_fix.qual=5;live.current_fix.fix_status_text="RTK_FLOAT";
    await page.reload();await page.waitForSelector(".field-diagnostics-card");
    await page.waitForFunction(()=>document.querySelector("#live-state b").textContent==="RTK FLOAT");
    assert(await page.locator("#live-state b").evaluate(node=>node.scrollWidth<=node.clientWidth));
    assert((await page.locator("#live-state small").innerText()).startsWith("DIAG an"));
    fieldStatus.error="storage_reserve";
    await page.reload();await page.waitForSelector(".field-diagnostics-card .critical");
    assert((await card.innerText()).includes("Speicherfehler"));
    fieldStatus.error=null;
    await card.getByRole("button",{name:"Felddiagnose stoppen",exact:true}).click();
    await page.waitForFunction(()=>!document.querySelector("#live-state small").textContent.includes("DIAG an"));
    await page.waitForFunction(()=>document.querySelector('.field-diagnostics-card a[aria-disabled]').getAttribute("aria-disabled")==="false");
    assert.deepStrictEqual(errors,[]);
    await page.screenshot({path:"/tmp/esp-rtk-field-diagnostics.png",fullPage:true});
    await browser.close();console.log("field-diagnostics-browser=ok (start/stop/marker, CSRF, storage error, recording fix, 320/360/1280px)");return;
  }
  live.active_line = { id: "l2", name: "Cold start", state: "recording", recording_mode: "route",
    recording_sec: 0, vertex_count: 0, length_m: 0 };
  for (const [state,label] of [["loading","Loading survey storage"],["error","Survey storage unavailable"]]) {
    live.tracking_startup={state};
    await page.reload();
    await page.waitForFunction(expected=>document.querySelector("#live-state small").textContent===expected,label);
    assert.strictEqual(await page.locator(".hero-state h2").innerText(),label);
    assert.strictEqual(await page.locator(".field-actions button").count(),0);
  }
  delete live.tracking_startup;
  live.active_line = { id: "l2", name: "Cold start", state: "recording", recording_mode: "route",
    recording_sec: 0, vertex_count: 0, length_m: 0 };
  for (const [reason, label] of [["waiting_clock", "Waiting for GNSS time"],
    ["waiting_position", "Waiting for usable position"], ["settling", "Stabilizing position 4/10 s"]]) {
    live.route_startup = { ready: false, reason, stable_sec: 4, required_sec: 10 };
    await page.reload();
    await page.waitForFunction(expected => document.querySelector("#live-state small").textContent === expected, label);
    assert.strictEqual(await page.locator("#live-state b").innerText(), "GPS FIX");
    assert.strictEqual(await page.locator(".hero-state h2").innerText(), label);
  }
  live.route_startup.ready = true;
  await page.reload();
  await page.waitForFunction(() => document.querySelector("#live-state small").textContent.includes("Recording"));
  live.active_line = null;
  delete live.route_startup;
  const initialLive = structuredClone(live);
  live.active_line = { id: "l2", name: "Field route with a long name", state: "recording",
    recording_sec: 125, vertex_count: 12, length_m: 5 };
  for (const width of [320, 360, 1280]) {
    await page.setViewportSize({ width, height: 900 });
    for (const [qual, label] of [[4, "RTK FIXED"], [5, "RTK FLOAT"], [2, "DGPS"], [1, "GPS FIX"], [0, "NO FIX"]]) {
      live.current_fix.qual = qual;
      live.current_quality.accepted = qual === 4;
      await page.reload();
      await page.waitForFunction(expected => document.querySelector("#live-state b").textContent === expected, label);
      const badge = page.locator("#live-state");
      assert((await badge.locator("small").innerText()).includes("02:05"));
      assert((await badge.getAttribute("aria-label")).includes(label));
      assert((await badge.getAttribute("class")).endsWith(qual === 4 ? "ok" : qual > 0 ? "warn" : "error"));
      assert(await badge.locator("b").evaluate(node => node.scrollWidth <= node.clientWidth), `${label} clips at ${width}px`);
      assert((await page.locator(".hero-state p").innerText()).includes(label));
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    }
  }
  live.current_fix.qual = 5;
  live.active_line.state = "paused";
  await page.reload();
  await page.waitForFunction(() => document.querySelector("#live-state small").textContent === "Recording paused");
  assert.strictEqual(await page.locator("#live-state b").innerText(), "RTK FLOAT");
  live.measurement_progress = { active: true, elapsed_sec: 42, collected: 2, total: 5 };
  await page.reload();
  await page.waitForFunction(() => document.querySelector("#live-state small").textContent.includes("Measuring"));
  assert.strictEqual(await page.locator("#live-state b").innerText(), "RTK FLOAT");
  for (let outage = 0; outage < 3; outage++) {
    failLive = true;
    await page.evaluate(() => dispatchEvent(new Event("offline")));
    await page.waitForFunction(() => document.querySelector("#live-state b").textContent.includes("Offline"));
    assert(!(await page.locator("#live-state").getAttribute("aria-label")).includes("RTK FLOAT"));
    assert(!(await page.locator("#live-state").getAttribute("title")).includes("Measuring"));
    failLive = false;
    await page.evaluate(() => dispatchEvent(new Event("online")));
    await page.waitForFunction(() => document.querySelector("#live-state b").textContent === "RTK FLOAT");
  }
  live.active_line.state = "recording";
  live.current_fix.qual = 4;
  live.current_quality.accepted = true;
  live.measurement_progress.active = false;
  await page.reload();
  await page.getByRole("button", { name: "Add line point", exact: true }).click();
  await page.waitForSelector(".measurement-sheet[open]");
  live.current_fix.qual = 1;
  live.current_quality.accepted = false;
  live.measurement_progress.active = true;
  await page.waitForFunction(() => document.querySelector("#live-state b").textContent === "GPS FIX");
  assert((await page.locator("#live-state small").innerText()).includes("Measuring"));
  failProgress = true;
  await page.waitForFunction(() => document.querySelector("#live-state b").textContent === "Device unreachable");
  assert(!(await page.locator("#live-state").getAttribute("title")).includes("GPS FIX"));
  failProgress = false;
  live.current_fix.qual = 5;
  await page.waitForFunction(() => document.querySelector("#live-state b").textContent === "RTK FLOAT");
  live.measurement_progress.active = false;
  assert(finishMeasurement, "measurement request must be in flight");
  finishMeasurement();
  await page.waitForSelector(".measurement-sheet", { state: "detached" });
  await page.setViewportSize({ width: 360, height: 900 });
  await page.evaluate(() => { localStorage.setItem("rtk-language", "de"); });
  await page.reload();
  await page.waitForFunction(() => document.querySelector("#live-state b").textContent === "RTK FLOAT");
  assert((await page.locator("#live-state small").innerText()).includes(require(root + "/i18n.js").v2de["Recording"]));
  assert(await page.locator("#live-state b").evaluate(node => node.scrollWidth <= node.clientWidth));
  assert(await page.locator(".readiness-hero").evaluate(node => getComputedStyle(node).color === "rgb(180, 35, 24)"));
  await page.screenshot({ path: "/tmp/esp-rtk-recording-fix.png" });
  if (process.argv.includes("--status-only")) {
    await browser.close();
    console.log("app-status-acceptance=ok (fix transitions, pause, measurement progress, three reconnects, 320/360/1280px)");
    return;
  }
  Object.assign(live, initialLive);
  await page.evaluate(() => { localStorage.setItem("rtk-language", "en"); });
  await page.setViewportSize({ width: 360, height: 900 });
  await page.reload();
  await page.waitForFunction(() => document.querySelector("#live-state b").textContent === "GPS FIX");
  assert.strictEqual(await page.locator("#view-back").count(), 0);
  assert.strictEqual(await page.locator('#mobile-nav [data-route="status"]').getAttribute("aria-current"), "page");
  assert.strictEqual(
    await page.locator('[data-context="storage"] b').innerText(),
    "31 / 5000 measurement points · 1%",
  );
  assert(await page.getByRole("button", { name: "Object point" }).isDisabled());
  await go(page, "projects", ".project-list");
  assert(!(await page.locator(".project-list").innerText()).includes("1970"));
  assert.strictEqual((await page.locator(".active-project").innerText()).toLowerCase(), "active for measurement");
  await go(page, "projects/p1", ".map-workspace");
  assert.strictEqual(
    await page.locator(".survey-map").evaluate(node => node.namespaceURI),
    "http://www.w3.org/2000/svg",
  );
  assert((await page.locator(".map-line").evaluate(node => node.getBBox().width)) > 0);
  assert.strictEqual(await page.locator(".map-point").count(), 1);
  const northBefore = await page.locator(".map-north").boundingBox();
  const scaleBefore = await page.locator(".map-label").textContent();
  await page.locator(".map-tools .btn").first().click();
  assert((await page.locator(".map-world").getAttribute("style")).includes("scale(0.5)"));
  assert.strictEqual(await page.locator(".survey-map").getAttribute("style"), null);
  assert.deepStrictEqual(await page.locator(".map-north").boundingBox(), northBefore);
  assert.notStrictEqual(await page.locator(".map-label").textContent(), scaleBefore);
  assert.strictEqual(await page.locator(".map-zoom").innerText(), "50 %");
  assert.strictEqual(await page.locator(".map-layers .btn").count(), 3);
  assert(await page.locator(".map-point").isVisible());
  assert((await page.locator(".map-point").boundingBox()).width >= 7);
  await page.getByRole("button", { name: "Fit project" }).click();
  assert.strictEqual(await page.locator('#mobile-nav [data-route="projects"]').getAttribute("aria-current"), "page");
  assert.strictEqual(await page.locator('#mobile-nav [data-route="projects"]').getAttribute("aria-current"), "page");
  await page.locator(".line-row").click();
  await page.waitForSelector(".vertex-row");
  await page.getByRole("button", { name: "Load more line points" }).click();
  await page.waitForFunction(
    () => document.querySelectorAll(".vertex-row").length === 2,
  );
  await page.locator(".point-row").click();
  const pointDetail = await page.locator(".point-detail").innerText();
  for (const value of ["North side", "QUALITY ACCEPTED", "Horizontal 1σ", "0.012 m",
    "Vertical 1σ", "0.024 m", "RTK FIXED", "18", "0.70", "0.8 s", "5", "4.2 s", "0054", "NMEA_GST"])
    assert(pointDetail.includes(value), `object-point detail misses ${value}: ${pointDetail}`);
  await go(page, "projects/p2", ".map-workspace");
  assert(await page.locator(".map-empty").isVisible());
  assert(await page.getByRole("button", { name: "Activate for measurement" }).isVisible());
  await go(page, "device", ".device-links");
  assert.strictEqual(await page.locator(".device-links a").count(), 7);
  assert((await page.locator(".device-identity").innerText()).includes("HARD"));
  assert((await page.locator("#live-state").innerText()).includes("GPS FIX"));
  assert((await page.locator("#live-state").innerText()).includes("H 0.200 m"));
  const liveLabelBox = await page.locator("#live-state > span:last-child").boundingBox();
  assert(liveLabelBox && liveLabelBox.width > 60, "global receiver label must not collapse to the dot width");
  failNextStatus = true;
  await go(page, "device/settings", ".settings-layout");
  assert.strictEqual(await page.getByRole("button", { name: "English" }).getAttribute("aria-pressed"), "true");
  assert.strictEqual(await page.locator(".measurement-choice button").count(), 4);
  await page.getByRole("button", { name: "Until quality is OK" }).click();
  assert.strictEqual(await page.getByRole("button", { name: "Until quality is OK" }).getAttribute("aria-pressed"), "true");
  await page.getByRole("button", { name: "Configure acceptance rules" }).click();
  await page.waitForSelector("dialog[open]");
  await page.locator('input[name="max_hdop"]').fill("2.0");
  const gatePut = page.waitForRequest(request => request.url().endsWith("/api/measurement-gate") && request.method() === "PUT");
  await page.getByRole("button", { name: "Save acceptance rules" }).click();
  const gatePayload = (await gatePut).postDataJSON();
  await page.waitForFunction(() => !document.querySelector("dialog[open]"));
  assert.strictEqual(gatePayload.max_hdop, 2);
  await page.getByRole("button", { name: "Restore defaults" }).click();
  await page.getByRole("button", { name: "Restore defaults" }).last().click();
  await page.waitForFunction(() => !document.querySelector("dialog[open]"));
  const contrast = page.getByRole("button", { name: "High contrast off" });
  assert.strictEqual(await contrast.getAttribute("aria-pressed"), "false");
  await contrast.click();
  await page.waitForSelector('html[data-theme="high-contrast"]');
  assert.strictEqual(await page.getByRole("button", { name: "High contrast on" }).getAttribute("aria-pressed"), "true");
  assert(
    await page.getByRole("button", { name: /Keep screen awake/ }).isEnabled(),
  );
  await go(page, "device/configuration", ".configuration-layout");
  await page.getByRole("button", { name: "Scan networks" }).click();
  await page.waitForSelector("dialog[open]");
  await page.getByRole("button", { name: "Use network" }).click();
  assert.strictEqual(await page.locator('input[name="wifi_ssid"]').inputValue(), "Visible");
  let putPayload = null;
  page.on("request", (request) => {
    if (request.url().endsWith("/api/config") && request.method() === "PUT")
      putPayload = request.postDataJSON();
  });
  await page
    .getByRole("button", { name: "Save and verify configuration" })
    .click();
  await page.waitForFunction(
    () =>
      document
        .querySelector(".configuration-layout .notice")
        .textContent.includes("connection restored"),
    null,
    { timeout: 8000 },
  );
  assert.deepStrictEqual(putPayload.wifi_networks, [
    { ssid: "Fallback", password_set: true },
  ]);
  await go(page, "device/log", ".log-console");
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Share" }).click();
  assert.strictEqual((await download).suggestedFilename(), "rtk-log-filtered.txt");
  await go(page, "device/settings", ".settings-layout");
  await page.getByRole("button", { name: "Deutsch" }).click();
  await page.waitForSelector(".settings-layout");
  assert.strictEqual(await page.getByRole("button", { name: "Deutsch" }).getAttribute("aria-pressed"), "true");
  assert.strictEqual(await page.getByRole("button", { name: "Au\u00dfenmodus an" }).getAttribute("aria-pressed"), "true");
  await go(page, "status", ".readiness-hero");
  const compactHero = await page.locator(".readiness-hero").boundingBox();
  assert(compactHero.height < 260, `status hero is ${compactHero.height}px high`);
  assert.strictEqual(await page.locator(".readiness-hero .metric").count(), 4);
  assert((await page.locator(".readiness-hero").innerText()).includes("Messung gesperrt"));
  await go(page, "device", ".device-links");
  assert((await page.locator(".device-links").innerText()).includes("Diagnose & Identifikation"));
  assert((await page.locator(".device-identity").innerText()).includes("FELDEMPF\u00c4NGER"));
  await go(page, "device/access", ".access-grid");
  assert((await page.locator("#app-main").innerText()).includes("Technische Verbindungsdetails"));
  assert.strictEqual(await page.locator(".tcp-auth-card .switch").getAttribute("aria-pressed"), "true");
  await page.locator(".tcp-auth-card .switch").click();
  await page.waitForSelector("dialog[open]");
  const accessRefresh = page.waitForResponse(response => response.url().endsWith("/api/access") && response.request().method() === "GET");
  const tcpPut = page.waitForRequest(request => request.url().endsWith("/api/tcp-auth") && request.method() === "PUT");
  await page.getByRole("button", { name: /Authentifizierung deaktivieren|Disable authentication/ }).click();
  assert.strictEqual((await tcpPut).postDataJSON().required, false);
  await accessRefresh;
  await page.waitForFunction(() => document.querySelector("#app-main").getAttribute("aria-busy") === "false");
  await go(page, "device/log", ".log-console");
  assert((await page.locator("#app-main").innerText()).includes("Was \u0069st passiert?"));
  await page.locator("#live-state").click();
  await page.waitForSelector(".readiness-hero");
  assert.strictEqual(await page.evaluate(() => location.hash), "#status");
  const compact = await page.evaluate(() => ({ header: document.querySelector(".app-header").getBoundingClientRect().height, footer: document.querySelector(".mobile-nav").getBoundingClientRect().height, status: document.querySelector("#live-state").getBoundingClientRect().height }));
  assert(compact.header <= 65 && compact.footer <= 61 && compact.status >= 48);
  await page.setViewportSize({ width: 1280, height: 900 });
  for (const [hash, selector] of [
    ["status", ".readiness-hero"],
    ["projects", ".project-list"],
    ["projects/p1", ".map-workspace"],
    ["device", ".device-links"],
    ["device/settings", ".settings-layout"],
  ]) {
    await go(page, hash, selector);
    assert.strictEqual(
      await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
      true,
      `${hash} overflows at desktop width`,
    );
  }
  await browser.close();
  console.log("app-v2-local-acceptance=ok");
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
