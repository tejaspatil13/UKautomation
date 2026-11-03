"""Only one Copilot at a time: two would run two backends and two data imports on the same files.

The lock is a file lock on <home>/copilot.lock (released by the OS even if the app crashes). The
running app records its port and a random secret there, so a second launch can ask it to come to
the front instead of starting again.
"""
import json
import logging
import secrets
import sys
from pathlib import Path

import httpx

log = logging.getLogger(__name__)


class SingleInstance:
    def __init__(self, home: Path):
        self.lock_path = home / "copilot.lock"
        self.info_path = home / "instance.json"
        self.focus_secret = secrets.token_urlsafe(24)
        self._fh = None

    def acquire(self) -> bool:
        fh = open(self.lock_path, "a+")
        try:
            if sys.platform == "win32":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        return True

    def publish(self, port: int) -> None:
        self.info_path.write_text(json.dumps({"port": port, "secret": self.focus_secret}), encoding="utf-8")

    def release(self) -> None:
        self.info_path.unlink(missing_ok=True)
        if self._fh:
            self._fh.close()
            self._fh = None

    def focus_running(self) -> bool:
        """Ask the copy that holds the lock to show its window. False if it couldn't be reached."""
        try:
            info = json.loads(self.info_path.read_text(encoding="utf-8"))
            r = httpx.post(f"http://127.0.0.1:{info['port']}/desktop/focus",
                           headers={"X-Copilot-Focus": info["secret"]}, timeout=10)
            return r.status_code == 204
        except Exception as e:
            log.warning("could not reach the running copy: %s", e)
            return False
