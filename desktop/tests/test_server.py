"""The thin wrapper around the existing app: only the Copilot window can use it, and an inactive licence
stops new work but not reading what was already found."""
import contextlib
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from desktop.license import LicenseManager, LicenseState
from desktop.server import DesktopState, _busy, build_app, default_name

PORT = 51234


def inner_app(updates=None, jobs=None):
    @contextlib.asynccontextmanager
    async def lifespan(app):
        app.state.updates = updates if updates is not None else []
        if jobs is not None:
            app.state.jobs = jobs
        yield

    inner = FastAPI(lifespan=lifespan)
    inner.get("/")(lambda: {"page": "dashboard"})
    inner.get("/api/health")(lambda: {"status": "ok"})
    inner.get("/api/jobs/" + "a" * 32)(lambda: {"state": "completed"})
    inner.post("/api/jobs")(lambda: {"id": "new"})
    inner.post("/api/jobs/" + "a" * 32 + "/retry")(lambda: {"id": "retry"})
    inner.post("/api/owners/run")(lambda: {"running": True})
    inner.post("/api/updates/install")(lambda: [])
    inner.post("/api/sheet/download")(lambda: {"file": "xlsx"})
    return inner


@pytest.fixture
def make(tmp_path):
    def factory(license_state=None, updater=None, **inner_kw):
        lic = LicenseManager(None)
        if license_state:
            lic.state = license_state
        state = DesktopState("1.0.0", "test", lic, tmp_path, updater, port=PORT, focus_secret="focus-secret")
        client = TestClient(build_app(inner_app(**inner_kw), state), base_url=f"http://127.0.0.1:{PORT}")
        return client, state
    return factory


def login(client, state):
    r = client.get(f"/desktop/start?token={state.session_token}", follow_redirects=False)
    assert r.status_code == 200 and 'url=/desktop/"' in r.text  # a page, so the cookie is stored first
    assert "httponly" in r.headers["set-cookie"].lower() and "samesite=lax" in r.headers["set-cookie"].lower()


def test_the_window_can_use_the_session_header_instead_of_the_cookie(make):
    client, state = make()
    with client:
        assert client.get("/api/health", headers={"X-Copilot-Session": "guess"}).status_code == 403
        assert client.get("/api/health", headers={"X-Copilot-Session": state.session_token}).json() == {"status": "ok"}
        assert not client.cookies


def test_only_the_window_with_the_session_cookie_gets_in(make):
    client, state = make()
    with client:
        assert client.get("/api/health").status_code == 403
        assert client.get("/desktop/start?token=guess").status_code == 403
        login(client, state)
        assert client.get("/api/health").json() == {"status": "ok"}
        page = client.get("/")
        assert '<div id="root">' in page.text  # the existing dashboard, plus the update notice:
        assert page.text.count('<script src="/desktop/banner.js" defer></script>') == 1
        assert "default-src 'self'" in page.headers["content-security-policy"]


def test_other_hostnames_are_refused_dns_rebinding(make):
    client, state = make()
    with client:
        login(client, state)
        assert client.get("/api/health", headers={"Host": f"evil.example:{PORT}"}).status_code == 403
        assert client.get("/api/health", headers={"Host": f"localhost:{PORT}"}).status_code == 200


EXPIRED = LicenseState("expired", "trial", 1.0, "Your free trial has ended", "Thank you for trying Copilot.")


@pytest.mark.parametrize("path", ["/api/jobs", "/api/jobs/" + "a" * 32 + "/retry", "/api/owners/run",
                                  "/api/updates/install"])
def test_expired_licence_blocks_new_work(make, path):
    client, state = make(EXPIRED)
    with client:
        login(client, state)
        r = client.post(path)
        assert r.status_code == 403 and r.json()["detail"] == ["Thank you for trying Copilot."]


def test_expired_licence_still_reads_and_downloads_existing_results(make):
    client, state = make(EXPIRED)
    with client:
        login(client, state)
        assert client.get("/api/jobs/" + "a" * 32).status_code == 200
        assert client.post("/api/sheet/download").status_code == 200


def test_expired_licence_shows_the_licence_screen_instead_of_the_dashboard(make):
    client, state = make(EXPIRED)
    with client:
        login(client, state)
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/desktop/"
        st = client.get("/desktop/status").json()
        assert st["license"]["kind"] == "expired" and not st["license"]["allowed"]


def test_active_licence_allows_work(make):
    client, state = make(LicenseState("active", "paid"))
    with client:
        login(client, state)
        assert client.post("/api/jobs").json() == {"id": "new"}


def test_status_and_pages(make):
    client, state = make()
    with client:
        login(client, state)
        page = client.get("/desktop/")
        assert "<title>Copilot</title>" in page.text and "script-src" not in page.headers["content-security-policy"]
        assert client.get("/desktop/desktop.js").status_code == 200
        assert client.get("/desktop/banner.js").status_code == 200
        assert client.get("/desktop/../../etc.js").status_code in (403, 404)
        st = client.get("/desktop/status").json()
        assert st["version"] == "1.0.0" and st["backend_ready"] is False and st["update"]["status"] == "disabled"


def test_update_restart_waits_for_running_work(make):
    ready = SimpleNamespace(status="ready", as_dict=lambda: {"status": "ready"})
    busy_dataset = SimpleNamespace(name="x", db_path=SimpleNamespace(exists=lambda: True), updating=True, last_error="")
    client, state = make(updater=ready, updates=[busy_dataset])
    restarted = []
    state.on_restart_to_update = lambda: restarted.append(True)
    with client:
        login(client, state)
        r = client.post("/desktop/update/restart")
        assert r.status_code == 409 and "still running" in r.json()["detail"][0] and r.json()["busy"]
        assert restarted == []
        # The user confirmed "Restart anyway": the data download starts again after the update.
        assert client.post("/desktop/update/restart", json={"force": True}).status_code == 202
        busy_dataset.updating = False
        assert client.post("/desktop/update/restart").status_code == 202
        assert restarted == [True, True]


def test_check_for_app_updates_button(make):
    import threading

    checked = threading.Event()
    updater = SimpleNamespace(enabled=True, status="idle", as_dict=lambda: {"status": "idle"},
                              check_and_download=checked.set)
    client, state = make(updater=updater)
    with client:
        login(client, state)
        assert client.post("/desktop/update/check").status_code == 202
        assert checked.wait(5)


def test_check_for_app_updates_from_source_explains(make):
    client, state = make()  # no updater: running from source
    with client:
        login(client, state)
        r = client.post("/desktop/update/check")
        assert r.status_code == 409 and "installed Copilot app" in r.json()["detail"][0]


def test_focus_needs_the_instance_secret(make):
    client, state = make()
    focused = []
    state.on_focus = lambda: focused.append(True)
    with client:
        assert client.post("/desktop/focus").status_code == 403
        assert client.post("/desktop/focus", headers={"X-Copilot-Focus": "focus-secret"}).status_code == 204
        assert focused == [True]


def test_busy_detects_running_searches():
    from app.models import JobState

    jobs = SimpleNamespace(recent=lambda n: [SimpleNamespace(state=JobState.RUNNING)])
    assert _busy(SimpleNamespace(state=SimpleNamespace(jobs=jobs, updates=[])))
    idle = SimpleNamespace(recent=lambda n: [SimpleNamespace(state=JobState.COMPLETED)])
    assert not _busy(SimpleNamespace(state=SimpleNamespace(jobs=idle, updates=[])))


def test_default_name_is_safe(monkeypatch):
    import desktop.server as server

    monkeypatch.setattr(server.getpass, "getuser", lambda: "Tejas Patil")
    assert default_name() == "Tejas Patil"
    monkeypatch.setattr(server.getpass, "getuser", lambda: "DESKTOP-01$<script>")
    assert default_name() == "Copilot"
