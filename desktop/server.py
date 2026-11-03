"""Runs the existing FastAPI app (app.main) inside the desktop app, unchanged, behind a thin wrapper:

- listens on 127.0.0.1 only, on a free port: nothing else on the network can reach it;
- every request needs this launch's session token, which only the Copilot window has: as a header the
  window adds to each request, or as the cookie set by /desktop/start?token=...; and the Host must
  be 127.0.0.1: other programs and web pages on this PC
  (including DNS-rebinding attacks) can't use the API;
- while the licence isn't active, new searches and processing are refused and the dashboard shows
  the licence screen instead;
- /desktop/... serves the start-up, first-run and licence screens and the update banner.
"""
import contextlib
import getpass
import logging
import re
import secrets
import socket
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

from desktop import logs
from desktop.license import LicenseManager

log = logging.getLogger(__name__)
PAGES = Path(__file__).with_name("pages")
COOKIE = "copilot_session"
SESSION_HEADER = "X-Copilot-Session"  # added by the window on Windows, where WebView2 dropped the cookie
# Starting new work needs a licence. Reading what was already found does not.
PROCESSING = re.compile(r"^/api/(jobs(/[0-9a-f]{32}/retry)?|owners/run|updates/install)$")
_NAME_OK = re.compile(r"^[\w '.-]{1,60}$")
PAGE_HEADERS = {"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'"}
# The dashboard's own headers (app.main), for the copy of its index.html served below.
DASHBOARD_HEADERS = PAGE_HEADERS | {
    "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:",
    "Cache-Control": "no-store"}
START_PAGE = ('<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0;url=/desktop/">'
              '<title>Copilot</title><p style="font-family:system-ui">Starting Copilot…</p>')
BANNER_TAG = '<script src="/desktop/banner.js" defer></script>'


@dataclass
class DesktopState:
    version: str
    channel: str
    license: LicenseManager
    log_dir: Path
    updater: object | None = None
    session_token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    focus_secret: str = ""
    port: int = 0
    on_focus: Callable[[], None] | None = None
    on_restart_to_update: Callable[[], None] | None = None
    window_connected: bool = False  # the window has passed the cookie check (logged once; CI looks for it)


def default_name() -> str:
    """The name the dashboard uses to label searches and default file names (one user: no name prompt)."""
    try:
        name = getpass.getuser().strip()
    except Exception:
        name = ""
    return name if _NAME_OK.match(name) and any(c.isalpha() for c in name) else "Copilot"


def build_app(main_app: FastAPI, state: DesktopState) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(_):
        async with main_app.router.lifespan_context(main_app):  # the existing app's own start-up/shutdown
            yield

    outer = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @outer.middleware("http")
    async def guard(request: Request, call_next):
        host = request.headers.get("host", "")
        if host not in (f"127.0.0.1:{state.port}", f"localhost:{state.port}"):
            return Response("Forbidden", status_code=403)
        path = request.url.path
        if path == "/desktop/start":
            if not secrets.compare_digest(request.query_params.get("token", ""), state.session_token):
                return Response("Forbidden", status_code=403)
            # A normal page, not a redirect: WebView2 must store the cookie before the next request.
            # Lax, not Strict: Strict can be withheld on the window's first, app-initiated navigation.
            resp = HTMLResponse(START_PAGE, headers=PAGE_HEADERS | {"Cache-Control": "no-store"})
            resp.set_cookie(COOKIE, state.session_token, httponly=True, samesite="lax", path="/")
            return resp
        if path == "/desktop/focus":
            return await call_next(request)  # authorised by the instance secret instead
        if not (secrets.compare_digest(request.headers.get(SESSION_HEADER, ""), state.session_token)
                or secrets.compare_digest(request.cookies.get(COOKIE, ""), state.session_token)):
            log.warning("refused %s %s: session header %s, cookie %s", request.method, path,
                        "wrong" if SESSION_HEADER in request.headers else "missing",
                        "wrong" if COOKIE in request.cookies else "missing")
            return Response("Open Copilot from the Start menu or the desktop shortcut.", status_code=403)
        if not state.license.state.allowed:
            if request.method == "POST" and PROCESSING.match(path):
                lic = state.license.state
                return JSONResponse({"detail": [lic.message or lic.title or "Copilot is not activated."]},
                                    status_code=403)
            if request.method == "GET" and not path.startswith(("/api/", "/desktop/")):
                return RedirectResponse("/desktop/", status_code=303)  # the dashboard: show the licence screen
        if getattr(state.updater, "required", False) and request.method == "POST" and PROCESSING.match(path):
            return JSONResponse({"detail": [f"A new version of Copilot ({state.updater.version}) must be installed "
                                            "first. Choose Restart now at the bottom right. Your data and sheets "
                                            "are kept."]}, status_code=403)
        return await call_next(request)

    @outer.get("/")
    @outer.get("/index.html")
    def dashboard():
        """The existing dashboard page with one addition: the desktop update notice (banner.js)."""
        from app.main import settings

        html = (settings.frontend_dist / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html.replace("</body>", f"  {BANNER_TAG}\n  </body>", 1), headers=DASHBOARD_HEADERS)

    @outer.get("/desktop/")
    def page():
        return FileResponse(PAGES / "desktop.html", headers=PAGE_HEADERS | {"Cache-Control": "no-store"})

    @outer.get("/desktop/{script}.js")
    def script(script: str):
        if script not in ("desktop", "banner"):
            return Response(status_code=404)
        return FileResponse(PAGES / f"{script}.js", media_type="text/javascript", headers={"Cache-Control": "no-store"})

    @outer.get("/desktop/status")
    def status():
        if not state.window_connected:
            state.window_connected = True
            log.info("window connected")
        return _status(main_app, state)

    @outer.post("/desktop/user")
    async def user_name(request: Request):
        """The name entered in the dashboard (top right): sent with licence checks for the admin panel."""
        try:
            name = str((await request.json()).get("name", "")).strip()
        except ValueError:
            name = ""
        client = getattr(state.license, "client", None)
        if not _NAME_OK.match(name) or client is None:
            return Response(status_code=204)
        if name != client.user_name:
            client.user_name = name
            state.license.request_check()  # tell the licence server now, not at the next minute
        return Response(status_code=204)

    @outer.post("/desktop/license/check")
    async def license_check():
        import asyncio

        await asyncio.to_thread(state.license.check_now)
        return _status(main_app, state)

    @outer.get("/desktop/diagnostics")
    def diagnostics():
        return Response(logs.diagnostics_zip(state.log_dir), media_type="application/zip",
                        headers={"Content-Disposition": 'attachment; filename="Copilot diagnostics.zip"'})

    @outer.post("/desktop/update/check")
    def check_for_update():
        """The "Check for app updates" button: check now instead of waiting for the next automatic check."""
        updater = state.updater
        if not updater or not getattr(updater, "enabled", False):
            return JSONResponse({"detail": ["App updates work in the installed Copilot app."]}, status_code=409)
        threading.Thread(target=updater.check_and_download, name="update-check", daemon=True).start()
        return Response(status_code=202)

    @outer.post("/desktop/update/restart")
    async def restart_to_update(request: Request):
        updater = state.updater
        if not updater or getattr(updater, "status", "") != "ready":
            return JSONResponse({"detail": ["No update is ready to install."]}, status_code=409)
        try:
            force = bool((await request.json()).get("force"))
        except ValueError:
            force = False
        if _busy(main_app) and not force:  # the user is asked first: a data download starts again after
            return JSONResponse({"detail": ["A search or data download is still running. If you restart now, it "
                                            "starts again from the beginning after the update."], "busy": True},
                                status_code=409)
        if state.on_restart_to_update:
            state.on_restart_to_update()
        return Response(status_code=202)

    @outer.post("/desktop/focus")
    def focus(request: Request):
        if not state.focus_secret or not secrets.compare_digest(
                request.headers.get("x-copilot-focus", ""), state.focus_secret):
            return Response(status_code=403)
        log.info("another launch asked this window to come to the front")
        if state.on_focus:
            state.on_focus()
        return Response(status_code=204)

    outer.mount("/", main_app)
    return outer


def _busy(main_app: FastAPI) -> bool:
    """A search, an owner lookup or a dataset import is running: don't restart now."""
    from app.models import JobState

    st = main_app.state
    jobs = getattr(st, "jobs", None)
    if jobs and any(j.state in (JobState.QUEUED, JobState.RUNNING) for j in jobs.recent(50)):
        return True
    owners = getattr(st, "owners", None)
    if owners and any(r.running for r in getattr(owners, "_runs", {}).values()):
        return True
    return any(d.updating for d in getattr(st, "updates", []))


def _status(main_app: FastAPI, state: DesktopState) -> dict:
    from app.main import settings

    updates = getattr(main_app.state, "updates", [])
    datasets = [{"name": d.name, "installed": d.db_path.exists(), "updating": d.updating, "error": d.last_error}
                for d in updates]
    progress = ""
    if any(d.updating for d in updates) and settings.import_log_path:
        progress = logs.last_line(settings.import_log_path).split(" ", 5)[-1][:200]  # drop the timestamp etc.
    updater = state.updater
    return {
        "version": state.version,
        "channel": state.channel,
        "license": state.license.state.as_dict(),
        "backend_ready": bool(updates),
        "datasets": datasets,
        "import_progress": progress,
        "update": updater.as_dict() if updater else {"status": "disabled"},
        "default_name": default_name(),
    }


class BackendServer:
    """uvicorn in a background thread, on a socket bound to 127.0.0.1 with a free port."""

    def __init__(self, app_factory: Callable[[int], FastAPI]):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        config = uvicorn.Config(app_factory(self.port), log_config=None, access_log=False, loop="asyncio",
                                lifespan="on", timeout_graceful_shutdown=10)
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [self.sock]},
                                       name="backend", daemon=True)

    def start(self, timeout: float = 60) -> None:
        self.thread.start()
        deadline = threading.Event()
        for _ in range(int(timeout * 20)):
            if self.server.started:
                return
            if not self.thread.is_alive():
                raise RuntimeError("The Copilot engine stopped while starting. See the log for details.")
            deadline.wait(0.05)
        raise RuntimeError("The Copilot engine took too long to start.")

    def stop(self, timeout: float = 20) -> None:
        self.server.should_exit = True
        self.thread.join(timeout)
        if self.thread.is_alive():
            log.error("backend did not stop within %ss", timeout)
