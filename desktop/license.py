"""Licence check against the Copilot licence server (license-server/, a Cloudflare Worker).

The server decides everything (trial start, expiry, paid, revoked) and returns a token signed with
its private Ed25519 key. The app only holds the public key, so it can check a token but never make
one. A token is bound to this machine and to the server environment (test or production).

Time: when online, the server's own clock decides; the local clock is never trusted to extend
access. Offline, the app keeps working until the token's offline_until (trial: at most 24 h since the
last check and never past the trial's end; paid: 14 days). The latest time ever seen (server time or
local time) is remembered, so turning the clock back doesn't buy time either.
"""
import base64
import hashlib
import json
import logging
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

log = logging.getLogger(__name__)

CHECK_EVERY_S = {"stable": 5 * 60, "test": 60}  # how soon an admin change (expire, paid, revoke) shows
CLOCK_SLACK_S = 10 * 60  # small differences between this PC's clock and the server's are normal
CONTACT = "Please contact the developer, Tejas Patil, on 7709288629."


@dataclass
class LicenseState:
    kind: str  # active | expired | revoked | invalid | offline | server_error | clock | checking | disabled
    plan: str = ""  # trial | paid
    expires_at: float | None = None  # unix seconds; None = permanent (paid) or unknown
    title: str = ""
    message: str = ""
    checked_online_at: float | None = None

    @property
    def allowed(self) -> bool:
        return self.kind in ("active", "disabled")

    def as_dict(self) -> dict:
        return asdict(self) | {"allowed": self.allowed}


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class TokenError(ValueError):
    pass


def verify_token(token: str, public_key_b64: str, env: str, machine: str) -> dict:
    """The token's payload, if it is genuine, for this machine and from the expected server."""
    try:
        body, sig = token.split(".")
        payload_bytes = _b64d(body)
        Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64)).verify(_b64d(sig), payload_bytes)
        payload = json.loads(payload_bytes)
    except (ValueError, InvalidSignature) as e:
        raise TokenError("signature check failed") from e
    if payload.get("v") != 1 or payload.get("env") != env:
        raise TokenError(f"token is for the {payload.get('env')!r} server, expected {env!r}")
    if payload.get("machine") != machine:
        raise TokenError("token belongs to another computer")
    return payload


def evaluate(payload: dict, now: float) -> LicenseState:
    """What a verified token allows at `now` (already corrected for clock rollback)."""
    status, plan = payload["status"], payload.get("plan", "")
    expires = payload.get("expires_at")
    if status == "revoked":
        return LicenseState("revoked", plan, expires, "Access has been turned off",
                            f"This copy of Copilot is no longer licensed. {CONTACT}")
    if status == "expired" or (expires is not None and now >= expires):
        # Trial or paid period over: the same message, so the user knows whom to call.
        return LicenseState("expired", plan, expires, "Your subscription has been completed",
                            f"{CONTACT} Thank you. Your data and saved sheets are kept.")
    if now > payload["offline_until"]:
        return LicenseState("offline", plan, expires, "Copilot needs to check its licence",
                            "Copilot couldn't reach the licence server for too long. Connect this computer to "
                            "the internet, then choose Check again.")
    return LicenseState("active", plan, expires)


def machine_fingerprint(fallback_dir: Path) -> str:
    """A stable, anonymous id for this computer (a hash; the raw id never leaves the PC).

    Windows: the MachineGuid set when Windows was installed. It survives app reinstalls and hardware
    changes; only reinstalling Windows changes it. No hardware fingerprinting.
    """
    raw = ""
    try:
        if sys.platform == "win32":
            import winreg

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0,
                                winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
                raw = winreg.QueryValueEx(key, "MachineGuid")[0]
        elif sys.platform == "darwin":
            out = subprocess.run(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"], capture_output=True,
                                 text=True, timeout=5).stdout
            raw = next((ln.split('"')[-2] for ln in out.splitlines() if "IOPlatformUUID" in ln), "")
    except Exception as e:
        log.warning("could not read the machine id: %s", e)
    if not raw:
        stored = fallback_dir / "machine-id"
        if not stored.exists():
            stored.write_text(uuid.uuid4().hex, encoding="utf-8")
        raw = stored.read_text(encoding="utf-8").strip()
    return hashlib.sha256(f"copilot-licence-v1:{raw}".encode()).hexdigest()


class LicenseClient:
    def __init__(self, url: str, public_key: str, env: str, machine: str, store: Path, version: str,
                 http: httpx.Client | None = None, clock: Callable[[], float] = time.time):
        self.url = url.rstrip("/")
        self.public_key, self.env, self.machine, self.version = public_key, env, machine, version
        self.store = store  # license.json: the last token and the latest time seen
        self.http = http or httpx.Client(timeout=10)
        self.clock = clock

    # ---------- local state ----------
    def _load(self) -> dict:
        try:
            return json.loads(self.store.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict) -> None:
        tmp = self.store.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(self.store)

    def _effective_now(self, saved: dict) -> float:
        return max(self.clock(), float(saved.get("last_seen", 0)))

    def evaluate_saved(self) -> LicenseState:
        """Offline decision from the last token received."""
        saved = self._load()
        if not saved.get("token"):
            return LicenseState("offline", title="Connect to the internet to start",
                                message="Copilot needs an internet connection the first time it starts, to "
                                        "activate your licence. Connect, then choose Check again.")
        try:
            payload = verify_token(saved["token"], self.public_key, self.env, self.machine)
        except TokenError as e:
            log.error("saved licence rejected: %s", e)
            return LicenseState("invalid", title="Licence not valid",
                                message=f"The licence on this computer isn't valid. Connect to the internet and "
                                        f"choose Check again. {CONTACT}")
        if self.clock() < payload["issued_at"] - CLOCK_SLACK_S:
            return LicenseState("clock", payload.get("plan", ""), payload.get("expires_at"),
                                "Check your computer's date and time",
                                "This computer's clock is behind the time the licence was last checked. Correct "
                                "the date and time in Windows settings, then choose Check again.")
        now = self._effective_now(saved)
        self._save(saved | {"last_seen": now})
        state = evaluate(payload, now)
        state.checked_online_at = saved.get("checked_at")
        return state

    # ---------- online ----------
    def check_online(self) -> LicenseState:
        """Ask the server. Falls back to the saved token when the server can't be reached."""
        try:
            r = self.http.post(f"{self.url}/v1/license", json={"machine": self.machine, "version": self.version})
        except httpx.TransportError as e:
            log.warning("licence server unreachable: %s", type(e).__name__)
            return self._fallback("offline")
        if r.status_code >= 500 or r.status_code == 429:
            log.error("licence server error %s", r.status_code)
            return self._fallback("server_error")
        try:
            token = r.json()["token"]
            payload = verify_token(token, self.public_key, self.env, self.machine)
        except (ValueError, KeyError, TokenError) as e:
            log.error("licence server answer rejected (%s): %s", r.status_code, e)
            return LicenseState("invalid", title="Licence not valid",
                                message=f"The licence server's answer couldn't be verified. {CONTACT}")
        saved = self._load()
        last_seen = max(float(saved.get("last_seen", 0)), payload["issued_at"])
        self._save({"token": token, "last_seen": last_seen, "checked_at": payload["issued_at"]})
        state = evaluate(payload, payload["issued_at"])  # the server's clock, not this PC's
        state.checked_online_at = payload["issued_at"]
        log.info("licence: %s %s, expires %s", state.kind, state.plan, payload.get("expires_at"))
        return state

    def _fallback(self, reason: str) -> LicenseState:
        state = self.evaluate_saved()
        if state.kind != "offline" or reason == "offline":
            return state  # a usable (or definitely ended) saved licence wins over the network problem
        return LicenseState("server_error", title="The licence server isn't responding",
                            message="Copilot couldn't check its licence because the licence server had a problem. "
                                    "This is usually temporary: choose Check again in a few minutes.")


class LicenseManager:
    """Keeps the current licence state up to date in the background and reports changes."""

    def __init__(self, client: LicenseClient | None, on_change: Callable[[LicenseState], None] | None = None,
                 check_every: float = CHECK_EVERY_S["stable"]):
        self.client = client
        self.on_change = on_change
        self.check_every = check_every
        self.state = LicenseState("disabled") if client is None else LicenseState(
            "checking", title="Checking your licence…")
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()

    def check_now(self) -> LicenseState:
        if self.client is None:
            return self.state
        with self._lock:
            self._set(self.client.check_online())
        return self.state

    def _set(self, state: LicenseState) -> None:
        changed = (state.kind, state.plan, state.expires_at) != (self.state.kind, self.state.plan, self.state.expires_at)
        self.state = state
        if changed and self.on_change:
            self.on_change(state)

    def run(self) -> None:
        """Background thread: online check every 30 min (test builds: 1 min), local re-evaluation every 30 s
        (catches expiry offline)."""
        if self.client is None:
            return
        last_online = 0.0
        while not self._stop.is_set():
            if time.monotonic() - last_online >= self.check_every or self._wake.is_set():
                self._wake.clear()
                self.check_now()
                last_online = time.monotonic()
            elif self.state.kind == "active":
                with self._lock:
                    self._set(self.client.evaluate_saved())
            self._wake.wait(min(30, self.check_every))

    def request_check(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
