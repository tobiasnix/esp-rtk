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

## Deploying to a board

`push.py` transfers only the files listed in its `BOARD_FILES` allowlist to a
connected board over the serial port, verifies every write by SHA-256, and
restarts the board afterwards:

```sh
python3 push.py . /dev/ttyACM0 --skip-unchanged
```

`--skip-unchanged` skips files whose checksum on the board already matches, so
repeated pushes during development are fast. After changing any JavaScript or
CSS file, raise `ASSET_VERSION` in `assets.py` — the device caches those files
long-term and immutably, and browsers will otherwise keep serving the old
version.

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
