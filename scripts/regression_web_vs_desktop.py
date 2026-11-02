"""Regression check: the same searches through (1) the existing web app and (2) the desktop server
(real uvicorn on 127.0.0.1 with the session cookie and licence wrapper), on real datasets. Every
result field and every Excel cell must be identical. Run from the repository root:

    .venv/bin/python scripts/regression_web_vs_desktop.py <data folder> <empty work folder>

<data folder> holds voa.sqlite, places.sqlite and companies.sqlite (e.g. ./data on the Mac, or
%LOCALAPPDATA%\Copilot\data on the Windows test PC). Each test search is deleted again once
compared, so the search history is left as it was. No network access is used. Starting the app runs
its normal housekeeping on that folder (searches older than 72 hours are removed).
"""
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path.cwd()
sys.path[:0] = [str(ROOT), str(ROOT / "backend")]
data, work = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
os.environ["COPILOT_HOME"] = str(work / "home")
os.environ["COPILOT_DATA_DIR"] = str(data)
os.environ["UKA_AUTO_REFRESH"] = "false"  # no network downloads during the test

from desktop.config import Paths, backend_environment  # noqa: E402

paths = Paths.resolve()
paths.create()
missing = [n for n in ("voa.sqlite", "places.sqlite", "companies.sqlite") if not (data / n).exists()]
if missing:
    sys.exit(f"{data} has no {', '.join(missing)}")
os.environ.update(backend_environment(paths))

import httpx  # noqa: E402
import openpyxl  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.main  # noqa: E402
from desktop.license import LicenseManager  # noqa: E402
from desktop.server import BackendServer, DesktopState, build_app  # noqa: E402

SEARCHES = [
    {"town": "Coventry", "postcode": "CV1 1"},
    {"town": "", "postcode": "CV1 1FJ"},
    {"town": "Grays", "postcode": "", "description_code": "CS", "rv_from": 10000, "rv_to": 30000},
    {"town": "Wimborne", "postcode": "BH21"},
]


def cells(content: bytes) -> dict:
    """Every cell of a downloaded workbook (the 'Generated' time is the only time-dependent value)."""
    import io

    wb = openpyxl.load_workbook(io.BytesIO(content))
    generated = lambda v: isinstance(v, str) and v[:2].isdigit() and v.endswith((" GMT", " BST"))  # noqa: E731
    return {ws.title: [[str(v) for v in row if not generated(v)] for row in ws.iter_rows(values_only=True)]
            for ws in wb.worksheets}


def run(client, label):
    out = []
    for req in SEARCHES:
        t = time.monotonic()
        job = client.post("/api/jobs?employee=Regression", json=req).json()
        while job["state"] in ("queued", "running"):
            time.sleep(0.2)
            job = client.get(f"/api/jobs/{job['id']}").json()
        recs = client.get(f"/api/jobs/{job['id']}/records?limit=1000").json()
        xlsx = client.get(f"/api/jobs/{job['id']}/download?filename=t") if job["download_ready"] else None
        out.append({"req": req, "state": job["state"], "summary": job["summary"], "message": job["message"],
                    "records": recs, "excel": cells(xlsx.content) if xlsx else None,
                    "seconds": round(time.monotonic() - t, 2)})
        client.delete(f"/api/jobs/{job['id']}")  # leave the search history as it was
        print(f"{label}: {req} -> {job['state']}, {recs['total']} businesses, {recs['properties']} properties, "
              f"{out[-1]['seconds']}s", flush=True)
    sugg = client.get("/api/suggest/postcodes?town=Coventry&q=CV1").json()
    meta = client.get("/api/meta").json()
    return out, sugg, {k: meta[k] for k in ("voa_dataset", "places_dataset", "companies_dataset", "contact_providers")}


# 1. the existing web app, exactly as deployed (no wrapper)
with TestClient(app.main.app) as web:
    web_out = run(web, "web    ")

# 2. the desktop server: real HTTP on 127.0.0.1, session cookie, licence wrapper
state = DesktopState("regression", "dev", LicenseManager(None), paths.logs)


def make(port):
    state.port = port
    return build_app(app.main.app, state)


server = BackendServer(make)
server.start()
try:
    with httpx.Client(base_url=f"http://127.0.0.1:{server.port}", timeout=120) as desk:
        desk.get(f"/desktop/start?token={state.session_token}", follow_redirects=True).raise_for_status()
        desk_out = run(desk, "desktop")
        page = desk.get("/")
        assert '<div id="root">' in page.text
finally:
    server.stop()


VOLATILE = {"seconds", "collected_at"}  # time-dependent


def strip(o):
    if isinstance(o, dict):
        return {k: strip(v) for k, v in o.items() if k not in VOLATILE}
    if isinstance(o, list):
        return [strip(v) for v in o]
    return o


same = strip(web_out[0]) == strip(desk_out[0]) and web_out[1:] == desk_out[1:]
print("IDENTICAL RESULTS" if same else "DIFFERENT RESULTS")
(work / "result.json").write_text(json.dumps({"web": web_out, "desktop": desk_out}, indent=1, default=str))
sys.exit(0 if same else 1)
