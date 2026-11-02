"""Copilot licence administration (run on your Mac).

    python license-server/admin.py keygen                      # once per environment: make a signing key pair
    python license-server/admin.py --env test list
    python license-server/admin.py --env test trial 3f2a 5     # trial ends 5 minutes from now
    python license-server/admin.py --env test trial 3f2a 60    # ... 1 hour
    python license-server/admin.py --env test trial 3f2a 2880  # ... 48 hours
    python license-server/admin.py --env test expire 3f2a
    python license-server/admin.py --env test revoke 3f2a      # (unrevoke to undo)
    python license-server/admin.py --env production paid 3f2a  # permanent paid licence
    python license-server/admin.py --env production extend 3f2a 24
    python license-server/admin.py --env production set-expiry 3f2a 2026-12-31T23:59:00Z
    python license-server/admin.py --env production note 3f2a "ACME Ltd, invoice 1042"
    python license-server/admin.py --env test reset 3f2a       # forget the PC: next start = new trial

A computer is named by the start of its id (as shown by `list`). The URL and admin token come from
environment variables (never stored in the repository):
    COPILOT_LICENSE_URL_TEST, COPILOT_ADMIN_TOKEN_TEST
    COPILOT_LICENSE_URL_PRODUCTION, COPILOT_ADMIN_TOKEN_PRODUCTION
Changes on production ask for confirmation. The app picks a change up at its next check (every
30 minutes, at start-up, or straight away when the user chooses Check again).
"""
import argparse
import base64
import os
import sys
from datetime import datetime, timezone

import httpx

ACTIONS = {"trial": "trial_minutes", "extend": "extend_hours", "set-expiry": "set_expiry", "expire": "expire",
           "paid": "paid", "revoke": "revoke", "unrevoke": "unrevoke", "reset": "reset", "note": "note"}


def keygen() -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    print("SIGNING_KEY (secret: paste into `npx wrangler secret put SIGNING_KEY --env <env>`, keep nowhere else):")
    print(base64.b64encode(private).decode())
    print("\nPUBLIC KEY (not secret: GitHub repository variable LICENSE_PUBLIC_KEY_<ENV>):")
    print(base64.b64encode(public).decode())


def when(ts) -> str:
    if ts is None:
        return "never"
    return datetime.fromtimestamp(ts, timezone.utc).astimezone().strftime("%d %b %Y %H:%M")


def left(seconds) -> str:
    if seconds is None:
        return "permanent"
    if seconds <= 0:
        return "ended"
    h, m = divmod(int(seconds) // 60, 60)
    return f"{h}h {m:02d}m left" if h else f"{m}m left"


def show(lic: dict) -> None:
    print(f"{lic['machine'][:12]}  {lic['plan']:<5}  {lic['status']:<7}  {left(lic['seconds_left']):<14}"
          f"  ends {when(lic['expires_at']):<17}  started {when(lic['trial_started_at'])}"
          f"  last seen {when(lic['last_seen_at'])}  v{lic.get('last_version') or '?'}  {lic.get('note') or ''}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--env", choices=("test", "production"), default="test")
    p.add_argument("command", choices=("keygen", "list", "show", *ACTIONS))
    p.add_argument("machine", nargs="?", help="start of the computer id (from `list`)")
    p.add_argument("value", nargs="?", help="minutes / hours / date / note")
    args = p.parse_args()
    if args.command == "keygen":
        keygen()
        return 0

    suffix = args.env.upper()
    url = os.environ.get(f"COPILOT_LICENSE_URL_{suffix}", "").rstrip("/")
    token = os.environ.get(f"COPILOT_ADMIN_TOKEN_{suffix}", "")
    if not url or not token:
        print(f"Set COPILOT_LICENSE_URL_{suffix} and COPILOT_ADMIN_TOKEN_{suffix} first.", file=sys.stderr)
        return 2
    http = httpx.Client(base_url=url, headers={"Authorization": f"Bearer {token}"}, timeout=20)

    licenses = http.get("/admin/licenses").raise_for_status().json()["licenses"]
    if args.command == "list":
        print(f"{args.env}: {len(licenses)} computer(s)")
        for lic in licenses:
            show(lic)
        return 0
    if not args.machine:
        p.error("name the computer (the start of its id, from `list`)")
    matches = [lic for lic in licenses if lic["machine"].startswith(args.machine.lower())]
    if len(matches) != 1:
        print(f"{len(matches)} computers match {args.machine!r}; give more of the id.", file=sys.stderr)
        return 2
    lic = matches[0]
    if args.command == "show":
        show(lic)
        return 0

    body: dict = {"action": ACTIONS[args.command]}
    need = {"trial": "minutes", "extend": "hours", "set-expiry": "expires_at", "note": "note"}
    if args.command in need:
        if args.value is None:
            p.error(f"{args.command} needs a value ({need[args.command]})")
        body[need[args.command]] = args.value if args.command in ("set-expiry", "note") else float(args.value)
    elif args.command == "paid" and args.value:
        body["expires_at"] = args.value
    if args.env == "production":
        show(lic)
        if input(f"Apply '{args.command}' to this PRODUCTION computer? Type yes: ").strip().lower() != "yes":
            print("Cancelled.")
            return 1
    r = http.post(f"/admin/licenses/{lic['machine']}", json=body)
    if r.status_code != 200:
        print(f"Refused ({r.status_code}): {r.text}", file=sys.stderr)
        return 1
    data = r.json()
    if "license" in data:
        show(data["license"])
    else:
        print(f"Reset {lic['machine'][:12]}: its next start begins a new trial.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
