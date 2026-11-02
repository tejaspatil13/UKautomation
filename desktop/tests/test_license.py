"""Licence client logic with locally made tokens (no server): signature, machine/env binding, expiry,
offline grace, clock rollback, and the distinct error states."""
import base64
import json

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from desktop.license import LicenseClient, LicenseManager, TokenError, evaluate, verify_token

MACHINE = "a" * 64
T0 = 1_800_000_000.0
KEY = Ed25519PrivateKey.generate()
PUB = base64.b64encode(KEY.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()


def b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def token(key=KEY, **over) -> str:
    payload = {"v": 1, "env": "test", "machine": MACHINE, "status": "active", "plan": "trial",
               "expires_at": T0 + 48 * 3600, "issued_at": T0, "offline_until": T0 + 24 * 3600} | over
    raw = json.dumps(payload).encode()
    return f"{b64(raw)}.{b64(key.sign(raw))}"


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def client(tmp_path, handler, clock):
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return LicenseClient("https://licence.test", PUB, "test", MACHINE, tmp_path / "license.json", "1.0.0", http, clock)


def answer(tok):
    return lambda req: httpx.Response(200, json={"token": tok})


def offline(req):
    raise httpx.ConnectError("no network", request=req)


# ---------- token checks ----------
def test_genuine_token_verifies():
    assert verify_token(token(), PUB, "test", MACHINE)["plan"] == "trial"


@pytest.mark.parametrize("tok,why", [
    (token(key=Ed25519PrivateKey.generate()), "signed by someone else"),
    (token(env="production"), "from the other licence server"),
    (token(machine="b" * 64), "copied from another computer"),
    ("garbage", "not a token"),
])
def test_bad_tokens_are_rejected(tok, why):
    with pytest.raises(TokenError):
        verify_token(tok, PUB, "test", MACHINE)


def test_edited_token_is_rejected():
    body, sig = token().split(".")
    payload = json.loads(base64.urlsafe_b64decode(body + "=="))
    payload["plan"], payload["expires_at"] = "paid", None
    forged = f"{b64(json.dumps(payload).encode())}.{sig}"
    with pytest.raises(TokenError):
        verify_token(forged, PUB, "test", MACHINE)


# ---------- what a token allows ----------
def test_states():
    p = json.loads(base64.urlsafe_b64decode(token().split(".")[0] + "=="))
    assert evaluate(p, T0 + 60).kind == "active"
    assert evaluate(p, T0 + 48 * 3600).kind == "expired"
    assert evaluate(p | {"status": "revoked"}, T0).kind == "revoked"
    assert evaluate(p | {"status": "expired"}, T0).kind == "expired"
    assert evaluate(p, T0 + 25 * 3600).kind == "offline"  # trial: must check in daily
    paid = p | {"plan": "paid", "expires_at": None, "offline_until": T0 + 14 * 86400}
    assert evaluate(paid, T0 + 13 * 86400).kind == "active"
    assert evaluate(paid, T0 + 15 * 86400).kind == "offline"


def test_expired_trial_message_is_clear_and_keeps_data():
    p = json.loads(base64.urlsafe_b64decode(token().split(".")[0] + "=="))
    st = evaluate(p, T0 + 49 * 3600)
    assert st.title == "Your free trial has ended" and "data" in st.message and not st.allowed


# ---------- client: online, offline, clock ----------
def test_online_check_saves_token_and_uses_server_time(tmp_path):
    clock = Clock(T0 + 100 * 86400)  # this PC's clock is far in the future: irrelevant online
    st = client(tmp_path, answer(token()), clock).check_online()
    assert st.kind == "active" and st.plan == "trial"
    assert json.loads((tmp_path / "license.json").read_text())["token"]


def test_offline_uses_saved_token_until_trial_ends(tmp_path):
    clock = Clock(T0)
    client(tmp_path, answer(token()), clock).check_online()
    c = client(tmp_path, offline, clock)
    clock.t = T0 + 3600
    assert c.check_online().kind == "active"
    clock.t = T0 + 23 * 3600
    assert c.check_online().kind == "active"
    clock.t = T0 + 24 * 3600 + 1
    assert c.check_online().kind == "offline"  # too long without a check


def test_offline_trial_still_ends_on_time(tmp_path):
    clock = Clock(T0)
    tok = token(expires_at=T0 + 600, offline_until=T0 + 600)
    client(tmp_path, answer(tok), clock).check_online()
    clock.t = T0 + 601
    assert client(tmp_path, offline, clock).check_online().kind == "expired"


def test_turning_the_clock_back_does_not_buy_time(tmp_path):
    clock = Clock(T0)
    tok = token(expires_at=T0 + 600, offline_until=T0 + 600)
    c = client(tmp_path, answer(tok), clock)
    c.check_online()
    clock.t = T0 + 590
    assert client(tmp_path, offline, clock).check_online().kind == "active"
    clock.t = T0 + 700  # trial over ...
    assert client(tmp_path, offline, clock).check_online().kind == "expired"
    clock.t = T0 + 100  # ... clock moved back: still over (latest time seen is remembered)
    assert client(tmp_path, offline, clock).check_online().kind == "expired"


def test_clock_before_last_check_is_flagged(tmp_path):
    clock = Clock(T0)
    client(tmp_path, answer(token()), clock).check_online()
    clock.t = T0 - 86400
    st = client(tmp_path, offline, clock).check_online()
    assert st.kind == "clock" and not st.allowed


def test_reinstall_deleting_local_file_does_not_reset_trial(tmp_path):
    """The trial lives on the server, keyed by the machine: a fresh local state just asks again."""
    clock = Clock(T0)
    server_token = token(status="expired", expires_at=T0 - 1, offline_until=T0)
    client(tmp_path, answer(server_token), clock).check_online()
    (tmp_path / "license.json").unlink()
    assert client(tmp_path, answer(server_token), clock).check_online().kind == "expired"


@pytest.mark.parametrize("handler,kind", [
    (offline, "offline"),  # first start without internet
    (lambda r: httpx.Response(503), "server_error"),
    (lambda r: httpx.Response(429), "server_error"),
    (lambda r: httpx.Response(200, json={"token": token(env="production")}), "invalid"),
    (lambda r: httpx.Response(200, text="<html>captive portal</html>"), "invalid"),
])
def test_distinct_failure_states_without_saved_licence(tmp_path, handler, kind):
    st = client(tmp_path, handler, Clock(T0)).check_online()
    assert st.kind == kind and not st.allowed and st.message


def test_server_error_with_good_saved_token_keeps_working(tmp_path):
    clock = Clock(T0)
    client(tmp_path, answer(token()), clock).check_online()
    assert client(tmp_path, lambda r: httpx.Response(500), clock).check_online().kind == "active"


def test_paid_activation_unlocks_without_reinstall(tmp_path):
    clock = Clock(T0)
    c = client(tmp_path, answer(token(status="expired", expires_at=T0 - 1, offline_until=T0)), clock)
    changes = []
    mgr = LicenseManager(c, changes.append)
    assert mgr.check_now().kind == "expired"
    paid = token(plan="paid", expires_at=None, offline_until=T0 + 14 * 86400)
    c.http = httpx.Client(transport=httpx.MockTransport(answer(paid)))
    assert mgr.check_now().allowed and mgr.state.plan == "paid"
    assert [s.kind for s in changes] == ["expired", "active"]


def test_no_licensing_in_development_runs():
    assert LicenseManager(None).state.allowed
