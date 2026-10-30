#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Run the whole browser acceptance harness (Playwright, headless Chromium) with one command.

Use: python3 tests/browser_acceptance.py [--install [--with-deps]] [--suite NAME ...] [--list] [--html PATH]

It builds app.js and the .gz copies like push.py does, renders the UI shell
(ui.APP_V2) into a temporary file, and runs every suite one after another
with node from the repository root. A suite passes if node exits with 0 and
prints its success marker; the run continues after a failure and exits 1 if
any suite failed.

Playwright is pinned to PLAYWRIGHT_VERSION. --install installs it with npm into
$RTK_PLAYWRIGHT_DIR (default ~/.cache/esp-rtk/playwright-<version>) together
with Chromium (--with-deps also installs the system libraries, which may need
root). Without --install, a Playwright found through NODE_PATH or in that
folder is used.

--suite NAME run only this suite (repeatable); --list print the suite names.
--html PATH write the UI shell to PATH (missing folders are created) and keep it
(default: temporary file).
Exit codes: 0 all suites passed, 1 a suite or the build failed, 2 usage or setup error.
"""
import argparse
import re
import os
import shutil
import subprocess
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import push
import ui

PLAYWRIGHT_VERSION = "1.62.1"
APP = "tests/app_v2_acceptance.js"
# (name, node arguments, success marker printed on stdout)
SUITES = (
    ("capacity-estimate", ("tests/capacity_estimate.js",), "capacity-estimate=ok"),
    ("api-request", ("tests/api_request_acceptance.js",), "PASS: header/body deadlines"),
    ("app", (APP,), "app-v2-local-acceptance=ok"),
    ("capacity", (APP, "--capacity-only"), "capacity-browser-acceptance=ok"),
    ("a11y", (APP, "--a11y-only"), "a11y-browser=ok"),
    ("theme", (APP, "--theme-only"), "theme-browser=ok"),
    ("i18n", (APP, "--i18n-only"), "i18n-browser=ok"),
    ("label", (APP, "--label-only"), "label-browser=ok"),
    ("log", (APP, "--log-only"), "log-browser=ok"),
    ("start-line", (APP, "--start-line-only"), "start-line-browser=ok"),
    ("project-loading", (APP, "--project-loading-only"), "project-loading=ok"),
    ("field-diagnostics", (APP, "--field-diagnostics-only"), "field-diagnostics-browser=ok"),
    ("mobile-layout", (APP, "--mobile-layout-only"), "mobile-layout-browser=ok"),
    ("status-gate", (APP, "--status-gate-only"), "status-gate-browser=ok"),
    ("frame-assignment", (APP, "--frame-assignment-only"), "frame-assignment-browser=ok"),
    ("feedback", (APP, "--feedback-only"), "feedback-browser=ok"),
    ("navigation", (APP, "--navigation-only"), "navigation-browser=ok"),
    ("field-flow", (APP, "--field-flow-only"), "field-flow-browser=ok"),
    ("frame-suggestion", (APP, "--frame-suggestion-only"), "frame-suggestion-browser=ok"),
    ("router-cache", (APP, "--router-cache-only"), "router-cache-browser=ok"),
    ("export", (APP, "--export-only"), "export-browser=ok"),
    ("live-stream", (APP, "--live-stream-only"), "live-stream-browser=ok"),
    ("stakeout", (APP, "--stakeout-only"), "stakeout-browser=ok"),
    ("stakeout-actions", (APP, "--stakeout-actions-only"), "stakeout-actions-browser=ok"),
    ("captive", ("tests/captive_acceptance.js",), "captive-browser=ok"),
    ("guided-apply", ("tests/guided_apply_acceptance.js",), "guided-apply-browser=ok"),
)
FAILURE_TAIL_LINES = 80
# Rough local run time per suite in seconds, only to balance --shard; a suite
# missing here counts as SHARD_DEFAULT_SECONDS.
SUITE_SECONDS = {
    "feedback": 705, "guided-apply": 365, "status-gate": 292, "router-cache": 290,
    "stakeout": 290, "app": 235, "frame-suggestion": 224, "live-stream": 139,
    "stakeout-actions": 120, "frame-assignment": 100, "field-flow": 85, "a11y": 66,
    "project-loading": 60, "export": 57, "navigation": 34, "capacity": 25,
    "log": 23, "mobile-layout": 20, "i18n": 12, "start-line": 9,
}
SHARD_DEFAULT_SECONDS = 60


def shard_of(suites, index, count):
    """The suites of shard `index` (1-based) out of `count`: longest first onto the least loaded shard."""
    loads = [0] * count
    owner = {}
    ordered = sorted(suites, key=lambda suite: (-SUITE_SECONDS.get(suite[0], SHARD_DEFAULT_SECONDS), suite[0]))
    for suite in ordered:
        target = min(range(count), key=lambda k: (loads[k], k))
        owner[suite[0]] = target
        loads[target] += SUITE_SECONDS.get(suite[0], SHARD_DEFAULT_SECONDS)
    return [suite for suite in suites if owner[suite[0]] == index - 1]


def parse_shard(text):
    """'K/N' -> (K, N) with 1 <= K <= N, else None."""
    match = re.fullmatch(r"([1-9][0-9]*)/([1-9][0-9]*)", text or "")
    if not match:
        return None
    index, count = int(match.group(1)), int(match.group(2))
    return (index, count) if index <= count else None


def install_dir(env=os.environ):
    """Folder holding node_modules/playwright for the pinned version."""
    return env.get("RTK_PLAYWRIGHT_DIR") or os.path.join(
        os.path.expanduser("~"), ".cache", "esp-rtk", "playwright-" + PLAYWRIGHT_VERSION)


def node_env(base_env, prefix, prefer_prefix):
    """Copy of base_env whose NODE_PATH also reaches prefix/node_modules (first if prefer_prefix)."""
    env = dict(base_env)
    modules = os.path.join(prefix, "node_modules")
    existing = env.get("NODE_PATH")
    if not existing:
        env["NODE_PATH"] = modules
    elif prefer_prefix:
        env["NODE_PATH"] = modules + os.pathsep + existing
    else:
        env["NODE_PATH"] = existing + os.pathsep + modules
    return env


def playwright_version(env):
    """Version of the Playwright node resolves with env, or None if it cannot be loaded."""
    try:
        done = subprocess.run(
            ["node", "-e", 'process.stdout.write(require("playwright/package.json").version)'],
            env=env, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode != 0:
        return None
    return done.stdout.strip() or None


def install(prefix, with_deps):
    """npm-install the pinned Playwright into prefix, then fetch Chromium."""
    subprocess.run(["npm", "install", "--prefix", prefix, "--no-save", "--no-package-lock",
                    "--no-audit", "--no-fund", "playwright@" + PLAYWRIGHT_VERSION], check=True)
    cli = [] if not with_deps else ["--with-deps"]
    subprocess.run(["node", os.path.join(prefix, "node_modules", "playwright", "cli.js"),
                    "install"] + cli + ["chromium"], check=True)


def run_suites(suites, env, html_path, timeout=1200, progress=None):
    """Run each suite with node from REPO; [(name, ok, seconds, output)]. Goes on after a failure.

    ok means exit code 0 and the success marker in the output. progress(result)
    is called as each suite finishes.
    """
    run_env = dict(env)
    run_env["RTK_APP_HTML"] = html_path
    results = []
    for name, args, marker in suites:
        started = time.monotonic()
        try:
            done = subprocess.run(["node", *args], cwd=REPO, env=run_env, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, timeout=timeout)
            output = done.stdout.decode("utf-8", "replace")
            ok = done.returncode == 0 and marker in output
        except subprocess.TimeoutExpired as exc:
            output = (exc.stdout or b"").decode("utf-8", "replace") + "\ntimeout after %s s\n" % timeout
            ok = False
        result = (name, ok, time.monotonic() - started, output)
        results.append(result)
        if progress:
            progress(result)
    return results


def _report(result):
    name, ok, seconds, output = result
    print("%s %s (%.1f s)" % ("PASS" if ok else "FAIL", name, seconds), flush=True)
    if not ok:
        print("\n".join(output.splitlines()[-FAILURE_TAIL_LINES:]), flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the browser acceptance suites.")
    parser.add_argument("--install", action="store_true", help="npm-install the pinned Playwright and Chromium first")
    parser.add_argument("--with-deps", action="store_true", help="with --install: also install system libraries")
    parser.add_argument("--suite", action="append", default=[], metavar="NAME", help="run only this suite (repeatable)")
    parser.add_argument("--list", action="store_true", help="print the suite names and exit")
    parser.add_argument("--html", metavar="PATH", help="write the UI shell to PATH and keep it")
    parser.add_argument("--shard", metavar="K/N", help="run only shard K of N (balanced by SUITE_SECONDS)")
    opts = parser.parse_args(argv)
    names = [name for name, _args, _marker in SUITES]
    if opts.list:
        print("\n".join(names))
        return 0
    unknown = [name for name in opts.suite if name not in names]
    if unknown:
        print("Unknown suite: %s (known: %s)" % (", ".join(unknown), ", ".join(names)), file=sys.stderr)
        return 2
    if shutil.which("node") is None:
        print("node not found: install Node.js 20 or newer", file=sys.stderr)
        return 2
    selected = [suite for suite in SUITES if not opts.suite or suite[0] in opts.suite]
    if opts.shard:
        shard = parse_shard(opts.shard)
        if shard is None:
            print("Bad --shard %r: expected K/N with 1 <= K <= N" % opts.shard, file=sys.stderr)
            return 2
        selected = shard_of(selected, *shard)
    prefix = install_dir()
    if opts.install:
        try:
            install(prefix, opts.with_deps)
        except (OSError, subprocess.CalledProcessError) as exc:
            print("Playwright installation failed: %s" % exc, file=sys.stderr)
            return 2
    env = node_env(os.environ, prefix, opts.install)
    found = playwright_version(env)
    if found is None:
        print("Playwright %s not found: run with --install or set NODE_PATH" % PLAYWRIGHT_VERSION, file=sys.stderr)
        return 2
    if found != PLAYWRIGHT_VERSION:
        print("WARNING: Playwright %s found, %s expected" % (found, PLAYWRIGHT_VERSION), file=sys.stderr)
    try:
        push.build_assets(REPO)
    except push.BuildError as exc:
        print("Build failed: %s" % exc, file=sys.stderr)
        return 1
    tmp = None if opts.html else tempfile.mkdtemp(prefix="esp-rtk-acceptance-")
    try:
        html_path = os.path.abspath(opts.html) if opts.html else os.path.join(tmp, "app-v2.html")
        try:
            os.makedirs(os.path.dirname(html_path), exist_ok=True)
            with open(html_path, "w", encoding="utf-8") as handle:
                handle.write(ui.APP_V2)
        except OSError as exc:
            print("Cannot write the UI shell to %s: %s" % (html_path, exc), file=sys.stderr)
            return 2
        results = run_suites(selected, env, html_path, progress=_report)
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
    failed = [name for name, ok, _s, _o in results if not ok]
    if failed:
        print("FAILED: %s" % ", ".join(failed), file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
