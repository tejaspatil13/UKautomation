"""`Copilot.exe --self-test`: proves the packaged app works on this computer, without opening a window.

Run by the Windows and Mac builds after packaging (no Python on the runner's PATH is used). Checks
every Windows-specific fix: time zones, bundled files, the DuckDB extension, imports through the exe,
the local server, its session cookie and Host checks, and the dashboard files; then runs a real search
end to end (rating list, business listings, phone match, Excel file) on a tiny synthetic dataset, in a
temporary data folder, never the user's data. Results are written to logs/selftest.json; the exit code
is 0 only if everything passed.
"""
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

import httpx

from desktop.config import BuildConfig, Paths, backend_environment, resource_dir

log = logging.getLogger(__name__)


SEARCH_LIMIT_S = 60  # the tiny test search takes about a second; a slow database query shows up here


def run(cfg: BuildConfig, paths: Paths) -> int:
    os.environ.update(backend_environment(paths))
    # The search check needs datasets: a throwaway folder, so the self-test never touches real data.
    data = Path(tempfile.mkdtemp(prefix="copilot-selftest-"))
    os.environ.update({"UKA_DATA_DIR": str(data), "UKA_AUTO_IMPORT_ON_START": "false", "UKA_AUTO_REFRESH": "false"})
    results: dict[str, str] = {}

    def check(name, fn):
        try:
            fn()
            results[name] = "ok"
        except Exception as e:
            results[name] = f"FAILED: {type(e).__name__}: {e}"
            log.error("self-test %s failed:\n%s", name, traceback.format_exc())

    check("time_zone", _time_zone)
    check("bundled_files", _bundled_files)
    check("description_codes", _description_codes)
    check("duckdb_httpfs", _duckdb_httpfs)
    check("import_subprocess", lambda: _import_subprocess(paths))
    check("test_datasets", lambda: _test_datasets(data))
    check("server_and_search", _server)
    check("velopack", lambda: __import__("velopack"))
    check("webview", _webview)
    results["build"] = f"{cfg.version} ({cfg.channel})"
    (paths.logs / "selftest.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    failed = [k for k, v in results.items() if v.startswith("FAILED")]
    log.info("self-test: %s", "all passed" if not failed else f"failed: {failed}")
    if sys.stdout:
        print(json.dumps(results, indent=2))
    return 1 if failed else 0


def _time_zone():
    from zoneinfo import ZoneInfo

    ZoneInfo("Europe/London")


def _bundled_files():
    env = backend_environment(Paths.resolve())
    assert (Path(env["UKA_FRONTEND_DIST"]) / "index.html").exists(), "dashboard build missing"
    assert Path(env["UKA_TEMPLATE_PATH"]).exists(), "Excel template missing"
    assert (resource_dir() / "desktop" / "pages" / "desktop.html").exists(), "desktop pages missing"


def _description_codes():
    from app.voa.description_codes import description_codes
    from app.companies.house import labels

    assert len(description_codes()) > 100
    assert labels()


def _duckdb_httpfs():
    import duckdb

    con = duckdb.connect()
    if ext := os.environ.get("UKA_DUCKDB_EXTENSIONS"):
        con.execute("SET extension_directory = '%s'" % ext.replace("'", "''"))
    con.execute("LOAD httpfs")  # offline: must come from the files shipped with the app


def _import_subprocess(paths: Paths):
    """A dataset import is started exactly as the app starts one (exe --run-import when packaged)."""
    from app.refresh import import_command

    marker = Path(tempfile.mkdtemp()) / "ran.txt"
    env = os.environ | {"COPILOT_SELFTEST_MARKER": str(marker)}
    kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    cwd = None if getattr(sys, "frozen", False) else str(resource_dir() / "backend")
    if not getattr(sys, "frozen", False):
        env["PYTHONPATH"] = os.pathsep.join([str(resource_dir()), str(resource_dir() / "backend")])
    subprocess.run(import_command("desktop.selftest_child"), env=env, cwd=cwd, timeout=120, check=True, **kwargs)
    assert marker.read_text(encoding="utf-8") == "ok", "import subprocess did not run"


def _server():
    import app.main
    from desktop.license import LicenseManager
    from desktop.server import BackendServer, DesktopState, build_app

    state = DesktopState("selftest", "selftest", LicenseManager(None), Paths.resolve().logs)

    def make(port):
        state.port = port
        return build_app(app.main.app, state)

    server = BackendServer(make)
    server.start()
    try:
        base = f"http://127.0.0.1:{server.port}"
        with httpx.Client(base_url=base, timeout=20) as c:
            assert c.get("/api/health").status_code == 403, "API reachable without the session cookie"
            assert c.get("/api/health", headers={"Host": "evil.example"}).status_code == 403, "Host not checked"
            assert c.get(f"/desktop/start?token={state.session_token}", follow_redirects=True).status_code == 200
            assert c.get("/api/health").status_code == 200
            assert c.get("/api/meta").status_code == 200
            assert '<div id="root">' in c.get("/").text, "dashboard not served"
            assert c.get("/desktop/status").json()["backend_ready"]
            _search(c)
    finally:
        server.stop()
        time.sleep(0.2)


def _vo_line(n, uarn, number, street, postcode, rv, desc="SHOP AND PREMISES", code="CS"):
    """One rating-list entry in the VO asterisk-delimited layout (28 fields)."""
    f = [""] * 28
    f[0], f[1], f[3], f[4], f[5], f[6], f[7] = str(n), "4610", f"REF{n}", code, desc, uarn, f"{number}, {street}, COVENTRY"
    f[9], f[10], f[11], f[14], f[17], f[19] = number, street, "COVENTRY", postcode, str(rv), str(n)
    return "*".join(f)


def _test_datasets(data: Path):
    """A tiny rating list and business-listings database, built the way the real imports build them."""
    import sqlite3
    import zipfile

    from app.contacts.overture_import import SCHEMA
    from app.voa.importer import import_zip

    data.mkdir(parents=True, exist_ok=True)
    zip_path = data / "vo-test.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("uk-englandwales-ndr-2026-listentries-test-csv.csv", "\n".join([
            _vo_line(1, "300675222", "13", "TRINITY STREET", "CV1 1FJ", 22000),
            _vo_line(2, "113420222", "15", "TRINITY STREET", "CV1 1FJ", 10750, "HAIRDRESSING SALON AND PREMISES", "CS3"),
        ]) + "\n")
    import_zip(zip_path, data / "voa.sqlite", list_year=2026)
    zip_path.unlink()
    con = sqlite3.connect(data / "places.sqlite")
    con.executescript(SCHEMA)
    con.executemany("INSERT INTO places VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", [
        ("a", "Games Workshop", "hobby_store", "13 Trinity St", "Coventry", "CV1 1FJ", "+442476227311", "", 0.95, "open",
         52.4093, -1.5097, "meta"),
        ("b", "Trinity Barbers", "barber", "15 Trinity Street", "Coventry", "", "02476111111", "", 0.8, None,
         52.4094, -1.5098, "meta"),
    ])
    # The same indexes as the real import (overture_import): the nearby lookup must use the partial one.
    con.execute("CREATE INDEX ix_places_postcode ON places(postcode)")
    con.execute("CREATE INDEX ix_places_nopc_latlon ON places(lat, lon) WHERE postcode = ''")
    con.execute("INSERT INTO meta VALUES ('release', 'selftest')")
    con.commit()
    from app.contacts.local_places import _NEARBY_HINTED

    plan = " ".join(r[-1] for r in con.execute("EXPLAIN QUERY PLAN " + _NEARBY_HINTED, (0, 1, 0, 1)))
    con.close()
    assert "ix_places_nopc_latlon" in plan, f"nearby lookup would scan the table on this SQLite: {plan}"


def _search(c: httpx.Client):
    """A real search through the app, as the dashboard does it: premises, phone match, Excel file."""
    r = c.post("/api/jobs?employee=Selftest", json={"postcode": "CV1 1FJ"})
    assert r.status_code in (200, 202), f"search refused: {r.status_code} {r.text[:200]}"
    job_id, started = r.json()["id"], time.monotonic()
    while True:
        job = c.get(f"/api/jobs/{job_id}").json()
        if job["state"] not in ("queued", "running"):
            break
        assert time.monotonic() - started < SEARCH_LIMIT_S, f"search still running after {SEARCH_LIMIT_S} s"
        time.sleep(0.5)
    assert job["state"] == "completed", f"search ended as {job['state']}: {job.get('message')}"
    phones = {r.get("phone") for r in c.get(f"/api/jobs/{job_id}/records").json()["items"]}
    assert {"+44 24 7622 7311", "+44 24 7611 1111"} <= phones, f"phone numbers not matched: {phones}"
    xlsx = c.get(f"/api/jobs/{job_id}/download")
    assert xlsx.status_code == 200 and xlsx.content[:2] == b"PK", "Excel download failed"
    log.info("self-test search: %s premises in %.1f s", len(phones), time.monotonic() - started)


def _webview():
    import webview  # noqa: F401

    if sys.platform == "win32":
        import clr  # noqa: F401  pythonnet: what pywebview uses for the Windows window and WebView2
