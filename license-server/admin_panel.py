"""Copilot admin panel: every PC that has installed Copilot, with buttons to stop, extend or shorten it.

Runs on your Mac only, in your browser, at 127.0.0.1 (nothing else on the network can reach it). It uses
the admin tokens in ~/.copilot-license/secrets.env (or the same names in the environment); they never
leave this Mac except to the licence servers, and are never put in the app you give to people.

Start it from the "Copilot Admin" app (made by scripts/make-admin-app.sh), "scripts/Copilot Admin.command", or:
    .venv/bin/python license-server/admin_panel.py
"""
import os
import secrets
import socket
import threading
import time
import webbrowser
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

HERE = Path(__file__).resolve().parent
SECRETS = Path.home() / ".copilot-license" / "secrets.env"
ENVS = ("test", "production")
ACTIONS = {"trial_minutes", "extend_hours", "set_expiry", "expire", "paid", "revoke", "unrevoke", "reset", "note"}


def load_secrets() -> dict[str, str]:
    values = {}
    if SECRETS.exists():
        for line in SECRETS.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and not key.startswith("#"):
                values[key] = value
    return values | {k: v for k, v in os.environ.items() if k.startswith("COPILOT_")}


def server_for(env: str, conf: dict[str, str]) -> tuple[str, str]:
    e = env.upper()
    return conf.get(f"COPILOT_LICENSE_URL_{e}", ""), conf.get(f"COPILOT_ADMIN_TOKEN_{e}", "")


IDLE_EXIT_S = 120  # the open page asks for data every 10 s: this long without a request = tab closed


def build_app(session: str, port_ref: dict, seen: dict | None = None) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    seen = seen if seen is not None else {}
    conf = load_secrets()
    headers = {"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
               "Cache-Control": "no-store",
               "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'"}

    @app.middleware("http")
    async def guard(request: Request, call_next):
        # Only this Mac, only the page this launch opened: other web pages can't send the session header.
        if request.headers.get("host") != f"127.0.0.1:{port_ref['port']}":
            return Response("Forbidden", status_code=403)
        if request.url.path.startswith("/api/") and not secrets.compare_digest(
                request.headers.get("x-admin-session", ""), session):
            return Response("Forbidden", status_code=403)
        seen["at"] = time.monotonic()
        return await call_next(request)

    @app.get("/")
    def page():
        return FileResponse(HERE / "admin_panel.html", headers=headers)

    @app.get("/admin_panel.js")
    def script():
        return FileResponse(HERE / "admin_panel.js", media_type="text/javascript", headers=headers)

    def call(env: str, method: str, path: str, body: dict | None = None):
        if env not in ENVS:
            return JSONResponse({"error": "unknown environment"}, 400)
        url, token = server_for(env, conf)
        if not (url and token):
            return JSONResponse({"error": f"The {env} licence server isn't set up on this Mac."}, 400)
        try:
            r = httpx.request(method, f"{url}{path}", json=body, timeout=20,
                              headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as e:
            return JSONResponse({"error": f"Couldn't reach the {env} licence server: {type(e).__name__}"}, 502)
        return JSONResponse(r.json() if r.headers.get("content-type", "").startswith("application/json")
                            else {"error": r.text[:200]}, r.status_code)

    @app.get("/api/{env}/devices")
    def devices(env: str):
        return call(env, "GET", "/admin/licenses")

    @app.post("/api/{env}/devices/{machine}")
    async def act(env: str, machine: str, request: Request):
        body = await request.json()
        if body.get("action") not in ACTIONS or not (len(machine) == 64 and all(c in "0123456789abcdef" for c in machine)):
            return JSONResponse({"error": "bad request"}, 400)
        return call(env, "POST", f"/admin/licenses/{machine}", body)

    return app


def main() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port_ref = {"port": sock.getsockname()[1]}
    session = secrets.token_urlsafe(24)
    seen = {"at": time.monotonic() + 180}  # allow time for the browser to open the page
    server = uvicorn.Server(uvicorn.Config(build_app(session, port_ref, seen), log_level="warning"))

    def stop_when_closed():  # the panel's tab was closed: don't leave a server running in the background
        while time.monotonic() - seen["at"] < IDLE_EXIT_S:
            time.sleep(15)
        server.should_exit = True

    threading.Thread(target=stop_when_closed, daemon=True).start()
    url = f"http://127.0.0.1:{port_ref['port']}/#{session}"  # after '#': never sent to any server or log
    threading.Thread(target=lambda: (time.sleep(1), webbrowser.open(url)), daemon=True).start()
    print(f"Copilot admin panel: {url.split('#')[0]}  (opened in your browser; stops 2 minutes after its tab is closed)", flush=True)
    server.run(sockets=[sock])


if __name__ == "__main__":
    main()
