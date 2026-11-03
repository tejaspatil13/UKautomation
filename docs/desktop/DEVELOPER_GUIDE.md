# Copilot desktop app: developer guide

The Windows app runs the **existing** FastAPI backend and React dashboard, unchanged, inside a
native window. This guide covers how that works, how to develop and test from a Mac, and how
releases reach the client.

## How it fits together

```
Copilot.exe (PyInstaller, one folder)
 ├─ desktop/launcher.py   Velopack hooks → logs → one copy only → licence → local server → window
 ├─ desktop/server.py     wraps app.main.app: 127.0.0.1 + free port, session cookie, Host check,
 │                        licence gate, /desktop/* start-up & licence screens, update notice
 ├─ backend/app/...       the existing application (unchanged apart from the Windows fixes below)
 ├─ frontend/dist         the existing dashboard build
 └─ pywebview window      WebView2 on Windows (WKWebView on a Mac)

%LOCALAPPDATA%\Copilot\      data\ (voa, places, companies, app.sqlite, exports)  logs\  license\  webview\
%LOCALAPPDATA%\CopilotApp\   the installed program (Velopack). Updates replace only this folder.
```

| File | Purpose |
|---|---|
| `desktop/launcher.py` | Entry point. Also runs `Copilot.exe --run-import <module>`, which replaces `python -m`. |
| `desktop/server.py` | The wrapper around `app.main.app`, plus uvicorn in a background thread |
| `desktop/license.py` | Licence client: token checks, offline rules, background re-checks |
| `desktop/updater.py` | Velopack: check every 15 min (and on **Check for app updates**), download in the background, install on restart or exit. An update left waiting 24 h (test builds: 1 h) blocks new searches until Copilot restarts to install it |
| `desktop/config.py` | Build channel (dev/test/stable), file locations, backend settings |
| `desktop/pages/` | Start-up/first-run/licence screen (`desktop.html` + `.js`) and the update notice (`banner.js`) |
| `desktop/selftest.py` | `Copilot.exe --self-test`: checks the packaged app (run by CI on Windows) |
| `desktop/copilot.spec`, `prepare_build.py` | PyInstaller build and per-channel configuration |
| `license-server/` | Cloudflare Worker licence server + `admin.py` (see LICENSING_GUIDE.md) |
| `.github/workflows/windows-build.yml` | Windows build, tests, installer, releases |
| `scripts/` | Live development (`dev-mac.sh`, `windows-dev.cmd`, `windows-run-from-source.ps1`) and the regression check |

### Changes to the existing code (Windows compatibility only)

Each change is covered by `backend/tests/test_windows_compat.py`. The web/Docker deployment
behaves as before.

| Change | Why |
|---|---|
| `app/dbswap.py`; importers call `swap_in()` instead of `os.replace()` | Windows can't replace a database file the app has open, so dataset updates always failed while Copilot ran. The finished file is now kept as `<name>.ready`. |
| `refresh.install_ready()`, `install_ready_at_startup()`; `close()` on `LocalPlacesProvider` / `LocalCompanies`; release hooks in `main.py` | The app closes its own connections, swaps in the `.ready` file, then reopens. The old data stays in use if anything fails. |
| `refresh.import_command()` | A packaged app has no `python -m`; it runs `Copilot.exe --run-import <module>` instead. |
| `refresh.run_import()`: log file, no console window, child killed on shutdown | Shows import progress on the first-run screen; no flashing console; no orphaned imports. |
| `tzdata` in `requirements.txt` | Windows has no time-zone database, so `ZoneInfo("Europe/London")` failed. |
| `UKA_DUCKDB_EXTENSIONS` in `overture_import.py` | DuckDB's httpfs extension ships with the app instead of being downloaded at import time. |
| `encoding="utf-8"` in `description_codes.py` | Windows defaults to cp1252. |
| `import_log_path` setting | Where import output goes (desktop app only). |
| `frontend/vite.config.ts`: `COPILOT_API_PORT` | Development only: the dev proxy's backend port (default unchanged, 8000). |

The dashboard's React code is **unchanged**. The desktop app seeds the saved name (Windows user
name) before the dashboard loads, so the "Who's working today?" prompt doesn't appear. It also
serves the dashboard's `index.html` with one extra `<script src="/desktop/banner.js">` for the
update notice.

## Daily development

**On the Mac, as before:** `cd backend && ../.venv/bin/python -m uvicorn app.main:app --reload`
plus `cd frontend && npm run dev`. To run the desktop app from source on the Mac:

```bash
COPILOT_DATA_DIR="$PWD/data" .venv/bin/python -m desktop     # licensing is off in source runs
```

**On the Windows test PC, live** (hot reload, no rebuilding):

1. Mac: `scripts/dev-mac.sh`. It starts the backend with auto-restart and the dashboard dev server
   on your local network, and prints the Mac's address. If port 8010 is in use, run
   `COPILOT_API_PORT=8020 scripts/dev-mac.sh`.
2. Windows: `scripts\windows-dev.cmd 192.168.1.36`, using the address it printed. The installed
   **test** build opens a window showing the Mac's dev servers. Saving a `.tsx` file updates the
   window instantly; saving a `.py` file restarts the backend.

The backend then runs on the Mac. To test backend changes **on Windows itself** (Windows paths,
file locking, imports), push from the Mac, then run on the test PC:
`powershell -ExecutionPolicy Bypass -File scripts\windows-run-from-source.ps1`.
That needs Git, Python 3.12 and Node 22 on the test PC only. Note that under `uvicorn --reload` on
Windows, dataset imports can't start (uvicorn's reload loop doesn't support subprocesses there). The
packaged app and `windows-run-from-source.ps1` don't have this limitation.

Hot reload proves the code works, not the installer. Before approving a client release, always
install and test the packaged **test** build.

## Tests

```bash
cd backend && ../.venv/bin/python -m pytest -q          # existing suite + Windows compatibility
cd desktop && ../.venv/bin/python -m pytest -q          # launcher, server wrapper, licence, config
.venv/bin/python scripts/regression_web_vs_desktop.py data /tmp/regress   # web vs desktop on real data
```

To run the licence tests against a real local licence server:

```bash
cd license-server && npm run db:init:local && npm run dev      # needs license-server/.dev.vars:
#   SIGNING_KEY=<test key from admin.py keygen>
#   ADMIN_TOKEN=<anything>
COPILOT_TEST_LICENSE_URL=http://127.0.0.1:8787 COPILOT_TEST_LICENSE_PUBKEY=<public key> \
COPILOT_TEST_ADMIN_TOKEN=<same token> .venv/bin/python -m pytest -q desktop/tests/test_license_server.py
```

CI runs both suites **on Windows**, builds the app, and runs `Copilot.exe --self-test`, which
covers time zones, bundled files, the DuckDB extension, imports through the exe, the local server
and its security checks. A failure stops the release.

## Releasing

| Step | What happens |
|---|---|
| Push to `main` | Windows build + tests + self-test. If all pass, a **test** pre-release `v1.0.0-test.<run>` is published. Only test builds update from it. |
| Test it | On the Windows PC, install the `…-Setup.exe` attached to the pre-release (or let the installed test build update itself). Run through `TEST_REPORT.md`. |
| Approve for the client | Raise `desktop/VERSION` if needed (e.g. `1.0.1`) and push. Then **Actions → Windows desktop build → Run workflow → stable**. With the production environment's reviewer rule set, you approve it in GitHub. |
| Client receives it | The client's app finds the new **stable** release within 15 minutes (at its next start, or straight away with **Check for app updates**), downloads it in the background, and installs it on restart or when closed. If they keep choosing Later, new searches stop after 24 hours until they restart. |

- **Versions:** `desktop/VERSION` holds the next client version. Test builds are
  `<VERSION>-test.<n>`. Velopack sorts these *below* `<VERSION>`, so after releasing `1.0.0` to the
  client, raise VERSION to `1.0.1` before further test builds.
- **First delivery to the client:** send them the `…-Setup.exe` attached to the stable
  release (also kept as a build artifact). After that, updates are automatic.
- A release whose tests or self-test fail is never published. Velopack checks each package's SHA-256
  before installing, and a failed install leaves the current version in place.
- App updates never touch `%LOCALAPPDATA%\Copilot` (data, sheets, licence).

## Troubleshooting

| Symptom | Look at |
|---|---|
| Any problem on a client PC | Ask for **Save diagnostics** (or the `logs` folder): `copilot.log` (app, licence, updates), `import.log` (dataset downloads) |
| A data update "still in use" | `copilot.log`: the new file is kept as `<name>.ready` and installed at the next start |
| CI self-test fails | The "Self-test" step prints `selftest.json` and the end of `copilot.log` |
| The licence won't activate | `copilot.log` lines from `desktop.license`; check the server with `admin.py list` and `<url>/health` |
| `.venv/bin/uvicorn` says "bad interpreter" | The venv was created at the project's old path. Use `.venv/bin/python -m uvicorn`, or recreate `.venv`. |
