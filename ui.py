# SPDX-License-Identifier: AGPL-3.0-only
"""Dependency-free web UI backed by the device's local HTTP endpoints.

Protected data is requested exclusively with the HttpOnly session cookie;
secrets never appear in URLs. CSS and JavaScript are served as local assets.
The field app uses app.css; support pages load base.css before pages.css.
"""

from assets import version_html

APP_V2 = version_html(r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#111820">
<title>RTK Field App</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='7' fill='%23111820'/%3E%3Ctext x='16' y='23' text-anchor='middle' font-family='sans-serif' font-weight='700' font-size='20' fill='white'%3ER%3C/text%3E%3C/svg%3E">
<link rel="stylesheet" href="/app.css?v=ASSET_VERSION">
</head>
<body data-page="app-v2">
<div id="app" class="app-shell" aria-busy="true">
  <a class="skip-link" href="#app-main">Skip to content</a>
  <aside class="app-sidebar" aria-label="Primary navigation">
    <a class="app-brand" href="/#status"><span class="brand-mark" aria-hidden="true">R</span><span><b>RTK</b><small>Field App</small></span></a>
    <nav id="desktop-nav" class="primary-nav"></nav>
    <nav id="desktop-context-nav" class="context-nav" aria-label="Device sections"></nav>
    <div id="device-summary" class="sidebar-device" aria-live="polite"></div>
  </aside>
  <div class="app-workspace">
    <header class="app-header">
      <div class="view-heading"><div><p id="view-kicker" class="kicker"></p><h1 id="view-title">Status</h1></div></div>
      <button id="live-state" class="live-state" type="button" aria-live="polite" aria-label="Open receiver status"><span aria-hidden="true"></span><span><b>Connecting</b><small></small></span></button>
    </header>
    <main id="app-main" tabindex="-1"><div class="initial-loader"><span></span><p>Loading field state …</p></div></main>
  </div>
  <nav id="mobile-nav" class="mobile-nav" aria-label="Primary navigation"></nav>
</div>
<div id="overlay-root"></div>
<script defer src="/i18n.js?v=ASSET_VERSION"></script>
<script defer src="/app.js?v=ASSET_VERSION"></script>
</body>
</html>
""")

APP_V2_LOGIN = version_html(r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#111820">
<title>Sign in · RTK Field App</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='7' fill='%23111820'/%3E%3Ctext x='16' y='23' text-anchor='middle' font-family='sans-serif' font-weight='700' font-size='20' fill='white'%3ER%3C/text%3E%3C/svg%3E">
<link rel="stylesheet" href="/app.css?v=ASSET_VERSION">
</head>
<body data-page="app-v2-login">
<main class="v2-login">
  <section class="login-intro"><span class="brand-mark" aria-hidden="true">R</span><p class="kicker">RTK FIELD APP</p><h1>Ready when the receiver is.</h1><p>Sign in locally with the device code printed on the receiver label.</p></section>
  <form id="v2-login-form" class="login-panel">
    <p class="kicker">SECURE DEVICE ACCESS</p><h2>Sign in</h2>
    <label class="field"><span>Device code</span><div class="secret-input"><input id="v2-device-code" type="password" required autocomplete="current-password" inputmode="text"><button id="v2-show-code" type="button" aria-pressed="false">Show</button></div></label>
    <button class="btn primary" type="submit">Open Field App</button>
    <p id="v2-login-state" class="muted" role="status" aria-live="polite">The code never leaves this device.</p>
  </form>
</main>
<script defer src="/i18n.js?v=ASSET_VERSION"></script>
<script defer src="/app.js?v=ASSET_VERSION"></script>
</body>
</html>
""")
