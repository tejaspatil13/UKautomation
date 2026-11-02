"""`Copilot.exe --self-test`: proves the packaged app works on this computer, without opening a window.

Run by the Windows build after packaging (no Python on the runner's PATH is used). Checks every
Windows-specific fix: time zones, bundled files, the DuckDB extension, imports through the exe,
the local server, its session cookie and Host checks, and the dashboard files. Results are written
to logs/selftest.json; the exit code is 0 only if everything passed.
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


def run(cfg: BuildConfig, paths: Paths) -> int:
    os.environ.update(backend_environment(paths))
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
    check("server", _server)
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
    finally:
        server.stop()
        time.sleep(0.2)


def _webview():
    import webview  # noqa: F401

    if sys.platform == "win32":
        import clr  # noqa: F401  pythonnet: what pywebview uses for the Windows window and WebView2
