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
    return secrets.token_hex(32)  # a fresh "computer" per test


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
    assert abs(st.expires_at - time.time() - 600) < 30  # test environment: TRIAL_SECONDS = 600


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
