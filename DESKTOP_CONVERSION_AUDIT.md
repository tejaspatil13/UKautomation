# Desktop Conversion Audit — Copilot

Phase 0 / Milestone 1 of the Windows desktop conversion. **No application code has been changed.**
Audited on 2026-11-02 against the working tree on `main` (commit `43479a0` plus uncommitted changes, see §0).

Everything below was verified by reading the code, running the test suite, and inspecting the real datasets in
`data/`. Items that could not be verified on macOS are marked **[verify on Windows]**.

---

## 0. Before anything else: uncommitted work

`git status` shows 23 modified and 8 untracked files (including the whole `backend/app/companies/` package,
`owners.py`, `units.py`, `grouping.py`, `CustomersView.tsx` and their tests). The last commit predates the
Companies House / Potential customers features.

The desktop build will be produced by GitHub Actions **from what is committed and pushed**. Uncommitted work would
silently be missing from the client's installer. **This work must be committed before Milestone 2.**

---

## 1. Existing application architecture

```
Browser ──HTTP──▶ uvicorn (1 worker) ──▶ FastAPI app (backend/app/main.py)
                                         ├─ /api/*            JSON API
                                         ├─ /                 StaticFiles → frontend/dist (React build)
                                         ├─ JobManager        in-process asyncio search jobs
                                         ├─ OwnerFinder       background Companies House owner lookups
                                         ├─ refresh_loop      daily dataset-update check
                                         └─ subprocesses      dataset imports (python -m app.<importer>)
                                                 │
                                   SQLite files in UKA_DATA_DIR
```

| Layer | Technology | Notes |
|---|---|---|
| Frontend | React 18.3.1 + TypeScript 5.6, Vite 5.4 | 8 source files, ~1,600 lines. No router, no UI library, no frontend tests. |
| Backend | FastAPI 0.162, uvicorn 0.54, pydantic 2.14 | ~4,000 lines of Python. Python 3.12 in Docker, 3.14.7 in local `.venv`. |
| Matching | RapidFuzz, phonenumbers | Pure local computation. |
| Excel | openpyxl, template `templates/output_template.xlsx` | |
| Import | httpx (downloads), DuckDB 2.0 + `httpfs` extension (Overture parquet scan) | |
| Storage | SQLite (4 files) | No external database. |
| Hosting today | Render, Docker, 20 GB disk, 2 GB RAM ("standard") | `render.yaml`, `Dockerfile` |

**Screens (tabs):** Search · My sheet · Potential customers, plus the name dialog (employee name), the data
updates dialog, confirm dialogs, and toasts. The footer shows dataset versions and "Check for updates".

---

## 2. Current startup sequence

1. `uvicorn app.main:app --host 0.0.0.0 --port $PORT` (cwd `backend/`), single worker.
2. Module import: `get_settings()` reads `UKA_*` env vars / `.env`. `logging.basicConfig` logs to stderr.
3. `lifespan` startup ([main.py:44](backend/app/main.py#L44)):
   - `JobManager`: creates `data/` and `data/exports/`, checks `app.sqlite` integrity (moves a corrupt file
     aside), creates the jobs tables.
   - `build_providers`: opens `places.sqlite` if it exists.
   - `SheetStore`, `OwnerFinder` (+ `CompaniesHouse` httpx client, `LocalCompanies`).
   - Warms the town/postcode suggestion caches in a thread.
   - `cleanup_exports()` (72 h retention), `resume_interrupted()` (restarts queued/running jobs).
   - Hourly export cleanup task; `refresh_loop` if `auto_refresh`.
4. StaticFiles serves `frontend/dist` at `/` if it exists.
5. The React app calls `/api/meta`, then asks for the employee name (stored in `localStorage`).

---

## 3. API endpoints (complete)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | Status + VO dataset meta. Exempt from password. |
| GET | `/api/meta` | Datasets, providers, limits, description codes, update status |
| GET | `/api/suggest/towns?q=` | Town type-ahead |
| GET | `/api/suggest/postcodes?town=&q=` | Postcode type-ahead |
| POST | `/api/jobs?employee=` | Start a search (202). Max 10 active. |
| GET | `/api/jobs?employee=` | Recent searches |
| GET | `/api/jobs/{id}` | Job status |
| DELETE | `/api/jobs/{id}` | Delete a finished search |
| GET | `/api/jobs/{id}/records?has_phone=&offset=&limit=` | Result rows (grouped per business; re-matches stale rows) |
| POST | `/api/jobs/{id}/retry` | Re-run failed lookups |
| GET | `/api/jobs/{id}/download?filename=` | Search Excel |
| GET / DELETE | `/api/sheet?employee=` | My sheet / clear it |
| POST | `/api/sheet/add` · `/api/sheet/remove` · `/api/sheet/download` | Sheet edits; Excel or .zip of parts |
| POST | `/api/owners/run` | Start the owner lookup |
| GET | `/api/owners/status?employee=` | Owner lookup progress |
| GET / DELETE | `/api/owners?employee=` | Potential customers / clear |
| POST | `/api/owners/remove` · `/api/owners/download` | Edit / Excel or .zip |
| POST | `/api/updates/check` · `/api/updates/install` | Dataset update check / install |
| GET | `/api/docs`, `/api/openapi.json` | OpenAPI UI |
| GET | `/*` | React build |

---

## 4. Datasets and storage

Location: `Settings.data_dir` = `UKA_DATA_DIR`, default `<repo>/data` ([config.py:13](backend/app/config.py#L13)).
Every path is derived from `data_dir`; nothing depends on the current working directory except the import
subprocess `cwd` (§10, W2).

Measured on this Mac:

| File | Size | Contents | Version in use |
|---|---|---|---|
| `voa.sqlite` | 685 MB | `properties` (2,143,536 rows), `meta`; 5 indexes | 2026 list, epoch 0004 |
| `places.sqlite` | 1.25 GB | `places` (4,002,696), `meta`; 2 indexes | Overture 2026-10-21.0 |
| `companies.sqlite` | 2.46 GB | `companies` (5,602,478), `psc` (6,687,138), `meta`; 2 indexes | CH 2026-10-01, PSC 2026-10-28 |
| `app.sqlite` (+WAL) | ~28 MB | jobs, job_records, sheet_rows, company_lookups, officer_lookups, customers | user data |
| `exports/` | small | generated .xlsx, deleted after 72 h | |
| **Total** | **≈ 4.4 GB** | | |

`app.sqlite` holds the **user's own work**: My sheet and Potential customers. It must never be lost by an update
or a reinstall. The other three files can always be rebuilt from the public sources.

---

## 5. First-time download and initialisation

- `auto_import_on_start` is **False by default** (True only on Render). With it on, `refresh_loop` waits 30 s, then
  imports each missing dataset **one after another** in a subprocess: VO → Overture → Companies House.
- Each importer builds `<name>.importing` and then `os.replace()`s it over the live file.
- Network sources: VO blob storage (zip ≈ 95 MB), Overture S3 (remote parquet scan via DuckDB), Companies House
  (7 + ~33 zips, ≈ 3 GB downloaded).
- **Measured duration:** the Companies House import alone took **35 min** on this Mac (log `data/companies_import.log`:
  20:57 → 21:32). VO and Overture together took roughly 1 hour on 26 Oct (judging from `imported_at`). **On a typical
  client PC and connection, first setup should be quoted as 1–3 hours, not 5–60 minutes.**
- Progress is only visible in log lines. The UI shows "not loaded yet" in the footer. There is no progress bar.
- Resumability: none. An interrupted download restarts that dataset from the beginning (Companies House retries each
  part 4 times). A stale `.importing` file is deleted at the start of the next run.

---

## 6. Existing automatic dataset updates

[refresh.py](backend/app/refresh.py): every `refresh_check_hours` (24) it compares the installed version with the
published one (VO URL + ETag, Overture release, CH release date). It installs only inside `refresh_window` (01–05
local time), or on demand via the footer "Check for updates" dialog. It checks free disk first (2.0 / 3.0 / 3.5 GB).
On failure the old file stays in place. This is the "safe update" pattern the brief asks for, **but see W1 and D3**.

---

## 7. Configuration and secrets

| Setting | Today | Sensitive | Desktop handling (proposed) |
|---|---|---|---|
| `UKA_DATA_DIR` | `/data` | no | `%LOCALAPPDATA%\Copilot\data`, set by the launcher |
| `UKA_APP_PASSWORD` | generated by Render | yes | See decision D2 |
| `UKA_COMPANIES_HOUSE_API_KEY` | Render dashboard (optional) | yes | See decision D4. Never in the repo or the exe. |
| `UKA_AUTO_IMPORT_ON_START` | true on Render | no | true |
| `UKA_AUTO_REFRESH`, `_REFRESH_CHECK_HOURS`, `_REFRESH_WINDOW` | defaults | no | See D3 |
| `UKA_VOA_LIST_YEAR` / `_URL`, limits, retention, `UKA_LOG_LEVEL` | defaults | no | defaults |
| `UKA_DUCKDB_MEMORY` (raw env var) | 256MB | no | default |

No secret is sent to the frontend today ([README Security](README.md)). That stays true.

---

## 8. Frontend build

`npm ci && npm run build` (`tsc --noEmit && vite build`) → `frontend/dist`. All API calls are **relative**
(`fetch("/api/…")`), so the same build works unchanged from `http://127.0.0.1:<any port>`. The dev proxy
targets `127.0.0.1:8000`. The CSP is `default-src 'self'`, which is compatible with a local origin.

## 9. Backend startup

`cd backend && uvicorn app.main:app`. Imports run as `python -m app.voa.importer`, `app.contacts.overture_import`
and `app.companies.bulk_import`. Manual tools: `python -m app.refresh --check | --now`.

---

## 10. Windows compatibility issues found

Ordered by severity. Each one needs a code change, and each change will be covered by a test.

| # | Issue | Evidence | Impact if not fixed | Minimal fix |
|---|---|---|---|---|
| **W1** | **Dataset swap fails on Windows while the app is running.** `os.replace()` cannot overwrite a file another handle has open. `LocalPlacesProvider` and `LocalCompanies` keep connections open permanently; VO connections are open during every search. | [local_places.py:31](backend/app/contacts/local_places.py#L31), [local.py:38](backend/app/companies/local.py#L38), [importer.py:136](backend/app/voa/importer.py#L136) | Every places/companies update fails ("previous data is still in use"). The data never updates on the client's PC. (Safe, but silent.) | The importer builds the new file. The **main process** closes its connections, swaps (with retry), then reopens. The existing inode check becomes an explicit reopen hook. |
| **W2** | **Imports run `sys.executable -m <module>`.** In a PyInstaller build, `sys.executable` is `Copilot.exe`, which has no `-m`. | [refresh.py:140](backend/app/refresh.py#L140) | No dataset can ever be downloaded. The app is unusable on a fresh install. | When frozen, launch `Copilot.exe --run-import <module>`, which dispatches to that module's `main()`. Use `CREATE_NO_WINDOW`. Unchanged when not frozen. |
| **W3** | **Windowed exe has no stdout/stderr** (`sys.stderr is None`). uvicorn's default log config calls `isatty()` on it. | `logging.basicConfig` in [main.py:35](backend/app/main.py#L35) | Crash on startup or lost logs. | The launcher configures rotating file logs in `%LOCALAPPDATA%\Copilot\logs` before importing the app, and passes `log_config` to uvicorn. |
| **W4** | **Time zones:** `ZoneInfo("Europe/London")` at import time. Windows has no IANA database, so Python needs the `tzdata` package. It is in `.venv` only as a transitive dependency and is **not pinned** in `requirements.txt`. | [main.py:41](backend/app/main.py#L41), [jobs.py:36](backend/app/jobs.py#L36) | `ZoneInfoNotFoundError` means the app won't start. | Pin `tzdata`; PyInstaller `--collect-data tzdata`. |
| **W5** | **DuckDB `httpfs` extension** is fetched at runtime into `~/.duckdb` (Docker pre-installs it). | [overture_import.py:53](backend/app/contacts/overture_import.py#L53), Dockerfile | Overture import fails behind firewalls that block `extensions.duckdb.org`. | Ship the Windows `httpfs` extension with the app and point DuckDB's `extension_directory` at it. |
| **W6** | **pywebview defaults:** `private_mode=True` (localStorage wiped on every launch) and `ALLOW_DOWNLOADS=False` (confirmed in pywebview docs). | [ui.tsx:15](frontend/src/ui.tsx#L15), [api.ts:185](frontend/src/api.ts#L185) | The employee name is asked on every launch, and **every Excel download silently does nothing**. | `private_mode=False`, `storage_path=%LOCALAPPDATA%\Copilot\webview`, `ALLOW_DOWNLOADS=True`. Blob + `<a download>` through WebView2 **[verify on Windows]**; a launcher-side "Save as" bridge is the fallback. No UI change either way. |
| **W7** | **HTTP Basic password prompt** relies on the browser's native dialog. | [main.py:84](backend/app/main.py#L84) | Behaviour inside WebView2 is unverified. | See decision D2. |
| **W8** | **Server binds `0.0.0.0` with a fixed port.** On a desktop, any other device on the office LAN could reach it, and the port can clash with other software. | Dockerfile CMD | Data exposed on the LAN; startup failure on a port clash. | Bind `127.0.0.1` on a free port chosen at launch. A per-launch token cookie plus a `Host` header check stops other local software and web pages (DNS rebinding) from calling the API. No frontend change. |
| **W9** | **No single-instance guard.** Two windows means two backends, two refresh loops and two importers on the same files. | — | Corrupt imports, duplicate jobs. | Named mutex / lock file in the launcher. A second launch focuses the existing window. |
| **W10** | `description_codes.json` is read with no `encoding=`. Windows defaults to cp1252. The file is currently pure ASCII. | [description_codes.py:12](backend/app/voa/description_codes.py#L12) | None today; breaks if a "£" or "–" is ever added. | Add `encoding="utf-8"` (one-word change). |
| **W11** | Python 3.14 (local `.venv`): pythonnet/pywebview support on 3.14 is unconfirmed. | — | Build or runtime failure. | Build Windows on **Python 3.12**, the same as the current production Docker image. |
| **W12** | `.importing`, `.part` and `*.download` work files and the `exports/` temp dirs use `with_suffix`/`mkdtemp(dir=…)` inside `data_dir`. | — | None (all paths absolute, under `data_dir`). | None; regression-test only. |

**Not issues** (checked): every data path is derived from `data_dir` with `pathlib`; template and JSON resources are
found via `__file__` (works with PyInstaller onedir); zip/CSV reads specify encodings; downloads are binary; job IDs
and filenames are validated server-side; `os.stat().st_ino` is populated on Windows/NTFS.

---

## 11. Behaviour changes the desktop model forces — decisions needed

> **Resolved (2 Nov 2026), per the final implementation instructions:**
> D1: one user on one PC; no sharing. D2: no password (`UKA_APP_PASSWORD` is forced empty; the
> server is reachable only from the Copilot window). D3: the existing update behaviour is kept
> unchanged (night window + footer button). D4: no Companies House API key (website mode, as now).
> D5: Cloudflare Workers + D1, separate test and production. D6: unsigned for now. The name
> prompt is skipped by seeding the Windows user name. W1–W12 are implemented; see
> docs/desktop/DEVELOPER_GUIDE.md and docs/desktop/TEST_REPORT.md.

These are not bugs. Moving from one shared server to one install per PC changes how the product works, and only the
product owner can decide them.

- **D1 — Shared team vs per-PC data.** Today the whole team shares one server: one dataset, and each employee's
  "My sheet" lives on that server. As desktop software, **every PC downloads its own 4.4 GB, and sheets are not
  shared between PCs.** Does the client have one user, or several people who expect to share?
- **D2 — Dashboard password.** On a localhost-only app (W8), the HTTP Basic password protects nothing that the Windows
  login doesn't already protect, and the license becomes the access control. Proposal: keep the feature, but off by
  default on desktop. Explicit approval is needed, since the brief says not to remove access controls silently.
- **D3 — Night-time update window (01:00–05:00).** Office PCs are usually off at night, so the data would never
  update. Proposal for desktop: install pending data updates whenever the app is open and no search is running, and
  keep the footer button. This changes the existing behaviour, so it needs approval.
- **D4 — Companies House API key.** Anything inside the exe can be extracted. Options: (a) website mode, no key (works
  today, but rate-limited); (b) the client enters their own free key once, stored in Windows Credential Manager.
  Proposal: (b), with (a) as the default until a key is entered. This adds one small settings field.
- **D5 — Licensing server hosting.** It needs a small always-on service. Proposal: **Cloudflare Workers + D1 (free
  tier, $0/month)**. It issues Ed25519-signed tokens; the private key lives only in Cloudflare secrets. The server
  remembers each machine (a hash of the Windows `MachineGuid`), so reinstalling or deleting local files doesn't
  restart the trial. Admin: a command-line script (`activate`, `extend`, `revoke`) for you. Needs your Cloudflare
  account.
- **D6 — Code signing.** Without a certificate, SmartScreen shows "Unknown publisher" on first install. It's fine for
  the trial; plan a certificate (about $100–400/yr, or Azure Trusted Signing about $10/month) before selling widely.
- **D7 — Data licence.** The README already notes the VO licence limits use to non-domestic rating purposes.
  Distributing the tool to a paying client doesn't change that. Each install downloads the data directly from the
  source, so no data passes through you. Worth confirming with the client.

---

## 12. Desktop conversion plan (refined from the brief)

| Milestone | Work | Verification |
|---|---|---|
| 2 | `desktop/launcher.py`: single instance, LocalAppData paths, file logging, free port, start uvicorn in-process (thread), wait for `/api/health`, pywebview window (persistent storage, downloads on), clean shutdown. Loading/progress screen is a separate launcher page; the React UI is untouched. | Runs on macOS today; pytest for launcher logic |
| 3 | Fixes W2–W5 and W10; PyInstaller **onedir** spec (Velopack requires onedir), bundling dist, template, JSON, tzdata, httpfs. | macOS bundle smoke test; Windows build in CI |
| 4 | Fix W1 (safe swap in main process) + first-run flow + import progress reported to the loading screen; free-disk check before first download. | New tests simulating locked files, interrupted and corrupt imports |
| 5 | Velopack `Setup.exe` (Start Menu + desktop shortcuts, Apps & Features entry, WebView2 bootstrap). **Pack ID `CopilotApp`, so the install folder `%LOCALAPPDATA%\CopilotApp` is separate from the data in `%LOCALAPPDATA%\Copilot`. Uninstalling never deletes the 4.4 GB data or the user's sheets.** | Clean Windows VM |
| 6 | Licensing: Cloudflare Worker + client module + expired / offline / revoked screens (launcher pages). A test-clock build flag that release builds reject. | Unit tests for every state in the brief (§D) |
| 7 | Velopack updates (`velopack.App().run()` at startup; check on launch and every 4 h; download in background; apply on restart, never during a job). | v1.0.0 → v1.0.1 on a VM |
| 8 | GitHub Actions `windows-latest`: test → build → pack → artifact; publish to the **`test` channel** automatically, and **promote to `stable` (client) manually**. | CI green |
| 9–10 | Full regression, install, licensing, update and performance runs on a clean Windows 11 VM with the full dataset; the documentation set from the brief. | `TEST_REPORT.md` with real numbers only |

Note on Velopack's install location: Velopack installs per-user with no admin rights and does not ask for an install
folder. That is the trade-off for silent background updates.

---

## 13. Regression test list

Existing automated baseline (run 2026-11-02, macOS, Python 3.14.7): **`pytest`: 141 passed, 0 failed.**
**`tsc --noEmit`: clean.** There are no frontend tests.

These will be re-run on the Windows CI runner, plus the new checks below against the packaged app:

1. App opens; `/api/health` ok; the React UI loads with no console errors.
2. Name dialog; the name persists across restarts (W6).
3. Town and postcode type-ahead (district → sector → full postcode).
4. Search by town, postcode, both; description code; RV range; validation messages; no-results hint; >5,000 truncation.
5. Job progress, retry, delete, history per employee; interrupted job resumes after restart.
6. Results table: business name, phone, other numbers, address, RV, Gov.uk links, Google Maps links (open in the
   system browser), shared-building and business-centre notes.
7. Search Excel download; My sheet add/remove/clear/download (single file and multi-part .zip); Potential customers
   run/status/remove/clear/download (W6).
8. Data updates dialog: check and install; footer versions.
9. Same inputs and same dataset versions give **identical results** on the web build and the desktop build. A golden
   comparison of the JSON from `/api/jobs/{id}/records` for a fixed set of searches.
10. Exports deleted after 72 h; corrupt `app.sqlite` set aside.
