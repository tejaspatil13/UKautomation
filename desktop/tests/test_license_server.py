"""The real licence server (license-server/, run with `npm run dev`) with the real app client.

Skipped unless COPILOT_TEST_LICENSE_URL, COPILOT_TEST_LICENSE_PUBKEY and COPILOT_TEST_ADMIN_TOKEN are
set. Covers the six test scenarios: minutes, an hour, 48 hours, expired, revoked, paid/permanent.
"""
import os
import secrets
import time

import httpx
import pytest

from desktop.license import LicenseClient

URL = os.environ.get("COPILOT_TEST_LICENSE_URL", "")
PUB = os.environ.get("COPILOT_TEST_LICENSE_PUBKEY", "")
ADMIN = os.environ.get("COPILOT_TEST_ADMIN_TOKEN", "")
pytestmark = pytest.mark.skipif(not (URL and PUB and ADMIN), reason="local licence server not configured")


@pytest.fixture
def machine():
    m = secrets.token_hex(32)  # a fresh "computer" per test
    yield m
    # Forget it afterwards, so test runs don't fill the admin panel with pretend PCs.
    httpx.post(f"{URL}/admin/licenses/{m}", json={"action": "reset"}, headers={"Authorization": f"Bearer {ADMIN}"})


def app_client(tmp_path, machine, url=None):
    return LicenseClient(url or URL, PUB, "test", machine, tmp_path / "license.json", "1.0.0-test")


def admin(machine, **body):
    r = httpx.post(f"{URL}/admin/licenses/{machine}", json=body, headers={"Authorization": f"Bearer {ADMIN}"})
    assert r.status_code == 200, r.text
    return r.json()


def check(c):
    time.sleep(2.1)  # the server refuses checks from one machine less than 2 s apart
    return c.check_online()


def test_first_start_activates_a_trial_with_the_server_clock(tmp_path, machine):
    st = app_client(tmp_path, machine).check_online()
    assert st.kind == "active" and st.plan == "trial"
    assert abs(st.expires_at - time.time() - 48 * 3600) < 30  # test environment: TRIAL_SECONDS = 172800


@pytest.mark.parametrize("minutes", [5, 60, 2880])
def test_admin_sets_trial_length(tmp_path, machine, minutes):
    c = app_client(tmp_path, machine)
    c.check_online()
    admin(machine, action="trial_minutes", minutes=minutes)
    st = check(c)
    assert st.allowed and abs(st.expires_at - time.time() - minutes * 60) < 30


def test_short_trial_really_expires(tmp_path, machine):
    c = app_client(tmp_path, machine)
    c.check_online()
    admin(machine, action="trial_minutes", minutes=0.05)  # 3 seconds
    time.sleep(3.5)
    st = c.check_online()
    assert st.kind == "expired" and st.title == "Your subscription has been completed"


def test_expire_extend_revoke_paid(tmp_path, machine):
    c = app_client(tmp_path, machine)
    c.check_online()
    admin(machine, action="expire")
    assert check(c).kind == "expired"
    admin(machine, action="extend_hours", hours=24)
    st = check(c)
    assert st.allowed and abs(st.expires_at - time.time() - 86400) < 60
    admin(machine, action="revoke")
    assert check(c).kind == "revoked"
    admin(machine, action="paid")  # paying also lifts a revocation
    st = check(c)
    assert st.allowed and st.plan == "paid" and st.expires_at is None


def test_reinstall_does_not_reset_but_admin_reset_does(tmp_path, machine):
    c = app_client(tmp_path, machine)
    c.check_online()
    admin(machine, action="expire")
    (tmp_path / "license.json").unlink()  # "reinstall": local licence state gone
    assert check(app_client(tmp_path, machine)).kind == "expired"
    admin(machine, action="reset")
    assert check(app_client(tmp_path, machine)).kind == "active"


def test_offline_after_activation_keeps_working(tmp_path, machine):
    app_client(tmp_path, machine).check_online()
    st = app_client(tmp_path, machine, url="http://127.0.0.1:9").check_online()  # nothing listens there
    assert st.kind == "active"


def test_admin_needs_the_token(machine):
    r = httpx.get(f"{URL}/admin/licenses", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401
    assert httpx.get(f"{URL}/admin/licenses").status_code == 401


def test_bad_requests_are_refused():
    assert httpx.post(f"{URL}/v1/license", json={"machine": "../etc"}).status_code == 400
    assert httpx.post(f"{URL}/v1/license", content=b"not json").status_code == 400


# ---------- Excel downloads: recorded, with row limits ----------
def downloads(machine):
    r = httpx.get(f"{URL}/admin/licenses/{machine}/downloads", headers={"Authorization": f"Bearer {ADMIN}"})
    assert r.status_code == 200, r.text
    return r.json()["downloads"]


def usage(machine):
    r = httpx.get(f"{URL}/admin/licenses/{machine}", headers={"Authorization": f"Bearer {ADMIN}"})
    return r.json()["license"]["downloads"]


def test_downloads_are_recorded_and_only_new_rows_count(tmp_path, machine):
    c = app_client(tmp_path, machine)
    c.check_online()
    assert c.record_download("sheet", [f"row{i}" for i in range(150)], "Monday.xlsx") is None
    assert c.record_download("sheet", [f"row{i}" for i in range(100, 200)], "Tuesday.xlsx") is None  # 50 new
    assert c.record_download("search", [f"s{i}" for i in range(5000)], "Coventry.xlsx") is None  # no limit
    u = usage(machine)
    assert u["sheet"] == {"used": 200, "limit": 2000} and u["customers"] == {"used": 0, "limit": 200}
    assert u["search"] == {"used": 5000, "limit": -1}
    hist = downloads(machine)
    assert [(d["kind"], d["rows"], d["new_rows"], d["filename"], d["allowed"]) for d in hist] == [
        ("search", 5000, 5000, "Coventry.xlsx", 1), ("sheet", 100, 50, "Tuesday.xlsx", 1),
        ("sheet", 150, 150, "Monday.xlsx", 1)]


def test_a_download_past_the_limit_is_refused_and_recorded(tmp_path, machine):
    c = app_client(tmp_path, machine)
    c.check_online()
    assert c.record_download("customers", [f"c{i}" for i in range(150)], "First.xlsx") is None
    msg = c.record_download("customers", [f"c{i}" for i in range(100, 260)], "Second.xlsx")  # 110 new, 50 left
    assert "110 new Potential customers rows, but only 50 of your 200 are left" in msg and "Tejas Patil" in msg
    assert usage(machine)["customers"]["used"] == 150  # nothing counted for the refused file
    assert downloads(machine)[0]["allowed"] == 0
    assert c.record_download("customers", [f"c{i}" for i in range(50)], "Again.xlsx") is None  # old rows: free


def test_admin_changes_limits_and_resets_usage(tmp_path, machine):
    c = app_client(tmp_path, machine)
    c.check_online()
    admin(machine, action="set_limits", limits={"customers": 10, "sheet": -1})
    assert c.record_download("customers", [f"c{i}" for i in range(11)], "x.xlsx") is not None
    assert c.record_download("sheet", [f"r{i}" for i in range(5000)], "big.xlsx") is None  # no limit now
    admin(machine, action="set_limits", limits={"sheet": None})  # back to the default
    assert usage(machine)["sheet"] == {"used": 5000, "limit": 2000}
    admin(machine, action="reset_downloads", kind="sheet")
    assert usage(machine)["sheet"]["used"] == 0
    assert c.record_download("sheet", ["r1"], "y.xlsx") is None


def test_a_stopped_pc_cannot_download(tmp_path, machine):
    c = app_client(tmp_path, machine)
    c.check_online()
    admin(machine, action="expire")
    assert "isn't active" in c.record_download("sheet", ["r1"], "z.xlsx")


def test_downloads_need_the_licence_server(tmp_path, machine):
    c = app_client(tmp_path, machine, url="http://127.0.0.1:9")
    assert "internet connection" in c.record_download("sheet", ["r1"], "z.xlsx")
