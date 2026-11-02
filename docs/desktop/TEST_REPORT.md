# Copilot desktop conversion: test report

**Status (2 Nov 2026): everything that can run on macOS has run and passed. The automatic Windows
build (CI, C1–C5) has run and passed. Nothing has yet run on a real Windows PC.** Per the acceptance criteria, **the installer must not go to the client** until the Windows
sections below are completed and pass.

Only results that were actually observed are recorded here.

## 1. Executed on macOS (MacBook M1, 8 GB, macOS 26, Python 3.14.7)

### Automated suites

| Suite | Result |
|---|---|
| Backend (`backend/`, existing 141 tests + 9 new Windows-compatibility tests) | **150 passed**, 1 skipped (real Windows file locking: runs in CI on Windows) |
| Desktop (`desktop/`: launcher config, server wrapper, licence client, single instance, logs) | **46 passed**, 1 skipped (Windows path) |
| Licence server, live: real Cloudflare Worker (`wrangler dev`, local D1) + real app client | **10 passed** (included in the desktop run of 56) |
| Frontend type-check (`tsc --noEmit`) | clean |

### Regression: existing web app vs desktop app, on the real 4.4 GB datasets

`scripts/regression_web_vs_desktop.py` ran the same four searches (town + sector, full postcode,
town + description code + RV range, and a no-results case) through the unchanged web app and through
the desktop server (real uvicorn on 127.0.0.1, session cookie, licence wrapper).

- **Identical results:** over 40,000 compared values (every result field, summaries, messages,
  postcode suggestions, dataset metadata). The only differences were collection timestamps.
- **Identical Excel files:** three workbooks compared cell by cell (5,076 + 208 + 3,778 cells).
- The dashboard page is served by the desktop server with only the update-notice script added.

### Packaged app (PyInstaller build on macOS)

| Check | Result |
|---|---|
| PyInstaller one-folder build | builds, 123 MB |
| `Copilot --self-test` on the packaged build: time zones, bundled files, description codes, DuckDB httpfs from the bundled zip, **dataset import through the exe (`--run-import`)**, local server, cookie/Host security, dashboard | **all 8 passed** |
| App start with window, real datasets, licence against the local licence server | trial activated at first start |
| Admin sets trial to 1 minute | app showed **expired** 10 s after the deadline, by itself |
| Admin marks it **paid** | app unlocked within 60 s, with no restart or reinstall |
| Admin **revokes** it | app showed **revoked** within 60 s |
| Errors in the app log during this run | none |

### Other checks

| Check | Result |
|---|---|
| Build preparation refuses mixed-up channels (stable on the test server, pre-release version as stable, …) | refused, as designed |
| Live development: `scripts/dev-mac.sh` serves the dashboard on the LAN and proxies `/api` to a backend reachable only on 127.0.0.1 | verified (`/api/meta` returned this project's datasets over the LAN; the backend port was unreachable from the LAN) |
| Workflow file `windows-build.yml` | valid YAML. **Not yet run.** |

### Performance measured on the Mac (not Windows figures)

| Measure | Result |
|---|---|
| Packaged app start (self-test, warm) | about 2 s. First launch of a new build took 31 s while macOS scanned the unsigned app; Windows Defender may do something similar |
| Search: Coventry + CV1 1 (728 properties) | 1.9 s desktop / 2.1 s web |
| Search: Grays, shops, £10k–30k (265 properties) | 6.7 s desktop / 6.9 s web |
| Search: one postcode (18 properties) | 0.22 s |
| Peak memory, backend running all searches on the full dataset | 126 MB |
| Packaged app on disk | 123 MB (macOS build) |
| First-time data download | not re-measured. The existing Companies House import log shows 35 min on this Mac; VO + Overture took roughly 1 h on 26 Oct |

## 2. Not yet run: needs Windows (CI, then your Windows test PC)

Fill in this table as each item is done. **All must pass before the client release.**

### CI (automatic on push)

| # | Check | Result |
|---|---|---|
| C1 | Backend suite on Windows, including the real file-locking test | **151 passed**, 0 skipped ([run](https://github.com/tejaspatil13/UKautomation/actions/runs/41410031876)). The first run found 3 Windows failures; see below |
| C2 | Desktop suite on Windows | **47 passed**, 10 skipped (the live licence-server tests: no server configured in CI) |
| C3 | PyInstaller Windows build | passed |
| C4 | `Copilot.exe --self-test` on the Windows runner (pythonnet/WebView2 import, tzdata, httpfs.dll, `--run-import`) | **all 8 passed** (build 1.0.0-test.3) |
| C5 | `vpk pack` (installer, WebView2 bootstrap) and `vpk upload github` (test pre-release) | **passed**: first test release `v1.0.0-test.8` published (Setup.exe, full package, releases.test.json) |

| C6 | Install `Setup.exe` silently, open the installed app's real window, screenshot | **passed** ([run](https://github.com/tejaspatil13/UKautomation/actions/runs/41419650183)): shortcuts created; the window shows the Copilot start-up screen |
| C7 | Second launch brings the open window to the front and exits | **passed** |
| C8 | Uninstall removes the program and keeps `%LOCALAPPDATA%\Copilot` | **passed** |
| C9 | Installed app activates against the real **test** licence server | **passed** (`licence state: active`, build test.8) |
| C10 | The previous test release, installed, downloads the new one by itself and installs it when closed | see the next build |

Windows failures found by CI, all fixed:

- **App bug:** Copilot couldn't start on Windows if its search-history file was damaged. Python's
  `with sqlite3.connect()` doesn't close the file, so Windows refused to set it aside. Fixed in
  `jobs.py`.
- Two tests assumed Mac behaviour (replacing a file that is still open); fixed in the tests.
- Velopack packaged the app as x86 by default; it's now packaged as 64-bit (`--runtime win-x64`).
- **App bug (also seen on the test PC):** the installed app showed only "Open Copilot from the Start
  menu or the desktop shortcut". WebView2 on Windows didn't send the session cookie back. The window
  now sends its session token as a header on every request. C6 reproduced the failure, then
  confirmed the fix.

### Windows test PC: installation

| # | Check | Result |
|---|---|---|
| W1 | Fresh install from the test pre-release, on a PC with no Python or Node | not run |
| W2 | SmartScreen prompt → More info → Run anyway works | not run |
| W3 | Start menu and desktop shortcuts open Copilot | not run |
| W4 | First start: licence activates (test server), first-run screen shows the download progress | not run |
| W5 | Full first-time data download completes; time and peak memory noted: ____ | not run |
| W6 | Opening Copilot a second time brings the open window to the front | not run |
| W7 | Close during the data download asks first; reopen restarts the download | not run |
| W8 | Uninstall keeps `%LOCALAPPDATA%\Copilot`; reinstall reuses the data (no 4 GB download) | not run |

### Windows test PC: the application (compare with the web version)

| # | Check | Result |
|---|---|---|
| A1 | No name prompt; the header shows the Windows user name; renaming works and persists after restart | not run |
| A2 | Search: town, postcode, sector, description code, RV range, validation messages, no-results hint | not run |
| A3 | Results: business, phone, other numbers, address, RV, notes; type-ahead suggestions | not run |
| A4 | **Excel download opens a Save dialog** and the file opens in Excel (search, My sheet, Potential customers, multi-part .zip) | not run |
| A5 | Google Maps / Gov.uk / Companies House links open in the default browser | not run |
| A6 | My sheet add/remove/clear; Potential customers run/remove/clear | not run |
| A7 | Footer **Check for updates** → install: data swapped in **while Copilot is open** (the Windows file-locking fix) | not run |
| A8 | `scripts/regression_web_vs_desktop.py` run from source on the PC → IDENTICAL RESULTS | not run |

### Windows test PC: licensing (LICENSING_GUIDE.md, test environment)

| # | Check | Result |
|---|---|---|
| L1 | Trial 5 minutes → expired screen; searches refused; data and sheets kept | not run |
| L2 | Trial 60 minutes / 2,880 minutes show the right time | not run |
| L3 | Expire → paid: unlocks without reinstall; still paid after restart | not run |
| L4 | Revoke → "Access has been turned off"; unrevoke restores | not run |
| L5 | Offline (Wi-Fi off) after activation keeps working; first start offline shows "Connect to the internet" | not run |
| L6 | Windows clock moved back a day → "Check your computer's date and time"; forward past the end → expired | not run |
| L7 | Uninstall + reinstall does **not** start a new trial | not run |

### Windows test PC: application updates

| # | Check | Result |
|---|---|---|
| U1 | Push a small change → new test pre-release → installed test build shows the update notice within 15 min (or at the next start) | not run |
| U2 | **Restart now** during a search is refused with a message; after the search, it installs and reopens | not run |
| U3 | **Later**, then close Copilot → the update installs; the next start shows the new version | not run |
| U4 | After the update: datasets, sheets, saved name and licence all intact; no data re-download | not run |
| U5 | Network cut during the update download → the app keeps running; it retries later | not run |

## 3. Known limitations and open points

- **Never run on Windows yet:** pythonnet/pywebview on Windows, WebView2's Save dialog for blob
  downloads, the Velopack CLI flags and installer file names, and the bring-to-front behaviour.
  Each is covered by the checks above.
- The Velopack download-progress callback is assumed to receive a percentage. It is only used for
  display; any other value is ignored, so it can't stop an update.
- The installer is unsigned, so SmartScreen warns on first install. A code-signing certificate
  removes this.
- Licensing can't stop someone who modifies the program itself (see LICENSING_GUIDE.md). The
  repository is public.
- On macOS, pywebview logs a harmless message on pages with a strict content-security policy.
  It wasn't seen in the final run.
