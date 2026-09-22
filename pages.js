// SPDX-License-Identifier: AGPL-3.0-only
(() => {
  "use strict";
  const page = document.body.dataset.page;
  const byId = id => document.getElementById(id);
  const tr = (key, fallback) => window.RtkI18n ? window.RtkI18n.t(key) : fallback;
  // Status text is spaced by CSS, not by leading blanks, and failures are
  // marked with the shared status colour instead of reading as plain text.
  const setStatus = (element, text, level) => {
    if (!element) return;
    element.textContent = text;
    element.className = level ? "muted " + level : "muted";
  };
  const confirmAction = message => new Promise(resolve => {
    const dialog = document.createElement("dialog"), form = document.createElement("form");
    dialog.className = "confirm-dialog";
    form.method = "dialog"; form.className = "survey-form";
    const text = document.createElement("p"); text.textContent = message;
    const actions = document.createElement("div"); actions.className = "survey-actions";
    const cancel = document.createElement("button"); cancel.type = "button";
    cancel.textContent = tr("action.cancel", "Cancel");
    const accept = document.createElement("button"); accept.type = "submit";
    accept.className = "danger"; accept.textContent = tr("ui.confirm_restore", "Confirm restore");
    cancel.onclick = () => { resolve(false); dialog.close(); };
    form.onsubmit = event => { event.preventDefault(); resolve(true); dialog.close(); };
    dialog.onclose = () => dialog.remove();
    actions.append(cancel, accept); form.append(text, actions); dialog.append(form);
    document.body.append(dialog); dialog.showModal();
  });

  const toggleSecret = button => {
    const input = byId(button.getAttribute("aria-controls"));
    if (!input) return;
    const show = input.type === "password";
    input.type = show ? "text" : "password";
    button.textContent = show ? tr("ui.hide", "Hide") : tr("ui.show", "Show");
    button.setAttribute("aria-pressed", show ? "true" : "false");
  };
  document.querySelectorAll("[data-secret-toggle]").forEach(button => {
    button.addEventListener("click", () => toggleSecret(button));
  });
  document.querySelectorAll(".page-logout").forEach(button => {
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        const session = await fetch("/api/session");
        if (!session.ok) { location.replace("/"); return; }
        const csrf = (await session.json()).csrf_token;
        const response = await fetch("/api/session", {method:"DELETE",
          headers:{"X-CSRF-Token":csrf}});
        if (!response.ok) throw new Error("HTTP " + response.status);
        location.replace("/");
      } catch (error) {
        button.disabled = false;
        if (statusBar) {
          statusBar.textContent = tr("ui.signout_failed", "Sign-out failed") + " (" + error.message + ")";
          statusBar.className = "device-status-bar critical";
        }
      }
    });
  });

  const statusBar = document.querySelector(".device-status-bar");
  const formatDuration = seconds => {
    seconds = Math.max(0, Math.floor(Number(seconds || 0)));
    const days = Math.floor(seconds / 86400), hours = Math.floor(seconds % 86400 / 3600),
          minutes = Math.floor(seconds % 3600 / 60);
    return days ? days + "d " + hours + "h" : hours ? hours + "h " + minutes + "m" :
      minutes ? minutes + "m" : seconds + "s";
  };
  const loadDeviceStatus = async () => {
    if (!statusBar) return;
    try {
      const response = await fetch("/status");
      if (!response.ok) throw new Error("HTTP " + response.status);
      const data = await response.json(), board = data.board || {}, system = data.system || {},
            stats = data.stats || {}, totalRam = Number(board.ram_free_bytes || 0) +
              Number(board.ram_alloc_bytes || 0);
      const mb = bytes => bytes == null ? "?" : (Number(bytes) / 1048576).toFixed(1) + " MB";
      const rssiLevel = value => value == null || value >= -67 ? "" : value >= -78 ? "warn" : "critical";
      const fields = [
        ["CPU", board.cpu_hz ? (Number(board.cpu_hz) / 1e6).toFixed(0) + " MHz" : "?", ""],
        ["Free RAM", mb(board.ram_free_bytes) + " / " + mb(totalRam),
          totalRam && Number(board.ram_free_bytes) / totalRam < .15 ? "critical" : ""],
        ["IDF-Heap", mb(board.idf_heap_free_bytes), ""],
        ["Flash", mb(board.flash_bytes), ""],
        ["File system", mb(board.fs_free_bytes) + " / " + mb(board.fs_total_bytes),
          board.fs_total_bytes && Number(board.fs_free_bytes) / Number(board.fs_total_bytes) < .1 ? "critical" : ""],
        ["Chip", board.mcu_temp_c == null ? "-" : board.mcu_temp_c + " °C",
          board.mcu_temp_c >= 80 ? "critical" : board.mcu_temp_c >= 65 ? "warn" : ""],
        ["SSID", stats.wifi_ssid_aktiv || "-", ""],
        ["WLAN", board.wifi_rssi == null ? "-" : board.wifi_rssi + " dBm", rssiLevel(board.wifi_rssi)],
        ["Transmit power", board.wifi_txpower_dbm == null ? "-" : board.wifi_txpower_dbm + " dBm", ""],
        ["AP clients", board.ap_clients == null ? "-" : board.ap_clients, ""],
        ["Uptime", formatDuration(system.uptime_sec), ""],
        ["Restarts", Number(stats.task_restarts || 0) + " " + tr("ui.tasks", "tasks") + " / " +
          Number(system.boot_count || 0) + " " + tr("ui.failed_boots", "failed boots"),
          stats.task_restarts || system.boot_count ? "warn" : ""]
      ];
      statusBar.className = "device-status-bar";
      statusBar.replaceChildren();
      fields.forEach(field => {
        const item = document.createElement("span"), value = document.createElement("b");
        if (field[2]) item.className = field[2];
        item.append(document.createTextNode((window.RtkI18n ? window.RtkI18n.label(field[0]) : field[0]) + " "));
        value.textContent = String(field[1]); item.append(value); statusBar.append(item);
      });
      document.querySelectorAll(".product-name").forEach(name => {
        if (system.display_name) name.textContent = system.display_name;
      });
    } catch (error) {
      statusBar.textContent = tr("ui.status_unavailable", "Device status unavailable") + " (" + error.message + ")";
      statusBar.className = "device-status-bar critical";
    }
  };
  if (statusBar) {
    loadDeviceStatus();
    setInterval(() => { if (!document.hidden) loadDeviceStatus(); }, 10000);
    document.addEventListener("rtk-language-change", loadDeviceStatus);
  }

  if (page === "diagnostics") {
    const load = async () => {
      const state = byId("state"), data = byId("data");
      setStatus(state, tr("state.loading", "Loading …"));
      const response = await fetch("/api/diagnostics"), text = await response.text();
      if (!response.ok) { data.textContent = text; setStatus(state, tr("ui.error","Error"), "critical"); return; }
      data.textContent = JSON.stringify(JSON.parse(text), null, 2);
      setStatus(state, window.RtkI18n
        ? window.RtkI18n.formatTime(new Date()) : new Date().toLocaleTimeString());
    };
    byId("refresh").addEventListener("click", load);
    load().catch(error => { setStatus(byId("state"), tr("ui.error","Error") + ": " + error.message, "critical"); });
  }

  if (page === "setup") {
    const form = byId("setup-form");
    form.addEventListener("submit", async event => {
      event.preventDefault();
      const button = byId("save-submit"), status = byId("save-status");
      if (button.disabled) return;
      button.disabled = true; button.value = tr("ui.saving","Saving …");
      setStatus(status, tr("ui.save_progress","Configuration is being transferred. The setup Wi-Fi may briefly disappear during verification."));
      try {
        const response = await fetch("/save", {method:"POST", body:new URLSearchParams(new FormData(form))});
        const text = await response.text();
        if (!response.ok) throw new Error(text || ("HTTP " + response.status));
        setStatus(status, tr("ui.save_success","The device is restarting and checking all Wi-Fi networks.") + " http://" + document.body.dataset.hostname + ".local/");
        button.value = tr("ui.saved_checking","Saved — checking connection");
      } catch (error) {
        setStatus(status, tr("ui.save_failed","Save failed:") + " " + error.message, "critical");
        button.disabled = false; button.value = tr("ui.save_again","Save again");
      }
    });
    byId("wifi-scan").addEventListener("click", async event => {
      const button = event.currentTarget, state = byId("wifi-scan-state"), select = byId("wifi-results");
      button.disabled = true; setStatus(state, tr("ui.searching","Searching …"));
      try {
        const response = await fetch("/api/wifi-scan"), result = await response.json();
        if (!response.ok) throw new Error(result.error || response.status);
        select.replaceChildren(new Option(tr("ui.choose_wifi_dots","Choose Wi-Fi network ..."), ""));
        result.networks.forEach(network => select.add(new Option(network.ssid + " (" + network.rssi + " dBm, " + tr(network.secured ? "ui.secured" : "ui.open", network.secured ? "secured" : "open") + ")", network.ssid)));
        select.hidden = false; setStatus(state, result.networks.length + " " + tr("ui.networks_found","Wi-Fi networks found"));
      } catch (error) { setStatus(state, tr("ui.scan_failed","Scan failed:") + " " + error.message, "critical"); }
      finally { button.disabled = false; }
    });
    byId("wifi-results").addEventListener("change", event => {
      if (event.currentTarget.value) byId("ssid").value = event.currentTarget.value;
    });
    byId("restore-submit").addEventListener("click", async event => {
      const button = event.currentTarget, file = byId("restore-file").files[0], state = byId("restore-state");
      if (!file) { setStatus(state, tr("ui.choose_backup","Please select a backup file first."), "warn"); return; }
      if (!await confirmAction(tr("ui.confirm_restore_detail",
          "This replaces Wi-Fi and NTRIP settings and restarts the device. Continue?"))) return;
      button.disabled = true; setStatus(state, tr("ui.checking_backup","Checking backup …"));
      try {
        const csrf = document.querySelector("[name=csrf_token]");
        const headers = {"Content-Type":"application/json"};
        if (csrf) headers["X-CSRF-Token"] = csrf.value;
        const response = await fetch("/api/config/restore", {method:"POST", headers, body:await file.text()});
        const text = await response.text();
        if (!response.ok) {
          let message = text || String(response.status);
          try { const payload = JSON.parse(text); message = payload.message || payload.error || message; }
          catch (_) {}
          throw new Error(message);
        }
        setStatus(state, tr("ui.restored_reboot","Restored. Device is restarting."), "ok");
      } catch (error) {
        setStatus(state, tr("ui.restore_failed","Restore failed:") + " " + error.message, "critical");
        button.disabled = false;
      }
    });
  }

  if (page === "login") {
    const form = byId("login-form"), status = byId("login-status"),
          next = byId("login-next").value;
    const signIn = async code => {
      setStatus(status, tr("ui.signing_in", "Signing in …"));
      const response = await fetch("/api/session", {method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({device_code:code,next})});
      if (!response.ok) throw new Error(await response.text());
      const result = await response.json();
      location.replace(result.redirect || next || "/setup");
    };
    form.addEventListener("submit", event => {
      event.preventDefault();
      signIn(byId("login-device-code").value)
        .catch(error => { setStatus(status, tr("ui.signin_failed", "Sign-in failed:") + " " + error.message, "critical"); });
    });
    const params = new URLSearchParams(location.hash.slice(1)), code = params.get("code");
    if (code) {
      history.replaceState(null, "", location.pathname + location.search);
      setStatus(status, tr("ui.checking_qr", "Checking QR access …"));
      signIn(code).catch(error => { setStatus(status, tr("ui.qr_access_failed", "QR access failed:") + " " + error.message, "critical"); });
    }
  }

  if (page === "label") {
    const drawQr = id => {
      const element = byId(id), qr = new QRCode(0, QRErrorCorrectLevel.M);
      qr.addData(element.dataset.value); qr.make();
      const count = qr.getModuleCount(), paths = [];
      for (let y=0; y<count; y++) for (let x=0; x<count; x++)
        if (qr.isDark(y,x)) paths.push(`M${x+4} ${y+4}h1v1h-1z`);
      element.innerHTML = `<svg role="img" aria-label="QR-Code" viewBox="0 0 ${count+8} ${count+8}" xmlns="http://www.w3.org/2000/svg"><rect width="100%" height="100%" fill="white"/><path d="${paths.join("")}" fill="black"/></svg>`;
    };
    drawQr("wifi-qr"); drawQr("portal-qr");
  }
})();
