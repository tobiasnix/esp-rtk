# Contributing to ESP-RTK

## Release model

Development happens in a private upstream repository. This public repository
receives one commit per release, produced by an export step that copies the
released tree across. There is no direct history here to rebase onto.

Pull requests are still welcome. A pull request is reviewed here, applied as a
patch in the private upstream, and appears in this repository with the next
release commit — so do not expect your commit itself to show up; expect its
effect to show up once a release is exported.

## Setup

You need Python 3 and Node.js 22. Install the host-side test and tooling
dependencies with:

```sh
python3 -m pip install -r requirements.txt
```

Node is used only to check the JavaScript files with `node --check`; no
`npm install` step is required.

## Running the tests

The full test suite runs on the host, without any hardware attached:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests
```

A single test module can be run from inside `tests/` with the repository root
on the import path:

```sh
cd tests && PYTHONPATH=.. PYTHONDONTWRITEBYTECODE=1 python3 -m unittest test_ui -v
```

## Language policy

Code, comments, and commit messages are English. `tests/test_language_policy.py`
enforces this: it rejects German identifiers and German prose in the firmware
and web sources.

User-facing text is the one exception, and it is not hardcoded into the pages.
Every UI string goes through `i18n.js`, which holds one dictionary per
language (currently English and German) keyed by a `namespace.name` string
such as `"action.save"`. Markup emitted by `web.py` references a string by its
key with a `data-i18n="..."` attribute, and the current field app
(`app.js`) looks a key up at render time with `tr("ui.some_key",
"fallback text")`. Neither reads well with a literal string hardcoded in
place of the key, so keep new UI text behind a key in both dictionaries.

## UI string contracts

`tests/test_ui.py` checks the web interface at the level of what a user or an
integrator can observe: that both language dictionaries in `i18n.js` define
the same keys, that pages do not inline styles or scripts that would violate
the Content Security Policy, and that the product is named ESP-RTK
consistently (an old product name must never reappear). Any change to a page,
a translation key, or the navigation should keep these tests passing, and add
to them where the new behaviour is not already covered.

## Building the field app

`app.js` is build output. **Never edit it directly.** The sources are the
files `src/NN-name.js`; `push.py` concatenates them in file-name order (the
two-digit prefix is the slot) into one shared IIFE, so the result is a single
script. Slot 95 is reserved for the stakeout code and `99-boot.js` always
comes last. After changing a source, rebuild and commit `src/`, `app.js` and
`app.js.gz` together:

```sh
python3 push.py . --build-only          # writes app.js and the .gz copies
python3 push.py . --build-only --check  # writes nothing; exit 1 if stale (as in CI)
```

`tests/test_build.py` and the CI step run the same check, so a direct edit of
`app.js` or a forgotten rebuild fails there.

## Browser acceptance

The Playwright mock-browser suites run with one command, locally and in CI:

```sh
python3 tests/browser_acceptance.py --install   # once: pinned Playwright + Chromium
python3 tests/browser_acceptance.py             # all suites
python3 tests/browser_acceptance.py --list      # suite names
python3 tests/browser_acceptance.py --suite NAME
```

On a fresh Linux machine add `--with-deps` to `--install` to install the
system libraries (needs root). The Playwright version is pinned in the runner.
The runner builds the app first and writes the UI shell to a temporary file
(`--html PATH` keeps it).

## Deploying to a board

`push.py` builds `app.js` and the `.gz` copies first, then transfers only the
files listed in its `BOARD_FILES` allowlist to a connected board over the
serial port, verifies every write by SHA-256, and restarts the board:

```sh
python3 push.py . /dev/ttyACM0 --skip-unchanged
```

Files that are already identical on the board are always skipped
(`--skip-unchanged` is accepted for compatibility), and `assets.py` is written
last, so an interrupted push can simply be repeated. Unknown options are
rejected with exit code 2 instead of being ignored.

After changing any JavaScript or CSS file, raise `ASSET_VERSION` in
`assets.py` — the device caches those files long-term and immutably, and
browsers will otherwise keep serving the old version. As a safety net, when a
checkout is pushed `push.py` aborts if browser assets changed but the
`ASSET_VERSION` equals the one on the board. Pass
`--allow-stale-asset-version` only for development builds. Pushes of a single
file or of a folder without `src/` (such as backups) are not checked.

## Never attach credentials or diagnostic exports with real positions

Wi-Fi passwords, NTRIP credentials, device codes, BLE passkeys, stream
tokens, configuration backups, support packages, and field-diagnostic exports
can all contain real secrets or real measured positions. Do not attach any of
them — or logs that quote them — to an issue, a pull request, or a comment.

Before reporting a problem, read
[docs/known-limitations.md](docs/known-limitations.md) for what is already
known about credential handling and data on the device, and
[docs/field-diagnostics.md](docs/field-diagnostics.md) for what a field
diagnostic export contains and how to share one privately if a maintainer
asks for it. If you find a security issue rather than a bug, see
[SECURITY.md](SECURITY.md) instead of opening a public issue.
