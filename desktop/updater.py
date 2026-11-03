"""Application updates with Velopack, from GitHub Releases.

Only the program is updated, never the datasets: they live in %LOCALAPPDATA%\\Copilot\\data, outside
the install folder Velopack replaces. Each build updates only from its own channel ("test" builds
from test pre-releases, "stable" builds from full releases), so the client never receives a build
that hasn't been approved. Velopack checks every package's SHA-256 before installing it.

An update is downloaded in the background and installed when the user chooses Restart now, or
automatically the next time Copilot is closed.
"""
import logging
import os
import threading
import time

log = logging.getLogger(__name__)
# GitHub allows 60 unauthenticated API calls per hour per IP. Test builds check often so fixes arrive quickly.
CHECK_EVERY_S = {"stable": 4 * 3600, "test": 15 * 60}


def run_hooks() -> None:
    """Velopack's install/update/uninstall hooks. Must run first thing; returns immediately otherwise."""
    try:
        import velopack

        velopack.App().run()
    except Exception as e:  # not installed by Velopack (running from source): nothing to do
        log.debug("velopack hooks skipped: %s", e)


class Updater:
    def __init__(self, repo: str, channel: str):
        self.status = "disabled"  # disabled | idle | checking | downloading | ready | error
        self.version = ""  # version ready to install
        self.progress = 0
        self.error = ""
        self.checked_at = 0.0  # last finished check (time.time()), shown as "You have the latest version"
        self._pending = None
        self._lock = threading.Lock()  # the background check and the "Check for app updates" button
        self._um = None
        self._stop = threading.Event()
        self._every = CHECK_EVERY_S.get(channel, 4 * 3600)
        if not repo or channel == "dev":
            return
        try:
            import velopack

            # Test builds read pre-releases. No token: the releases are public. CI sets one only to avoid
            # GitHub's rate limit on its shared machines.
            source = velopack.GithubSource(repo, os.environ.get("COPILOT_GITHUB_TOKEN") or None, channel == "test")
            self._um = velopack.UpdateManager(source)
            self.current = self._um.get_current_version()
            self.status = "idle"
        except Exception as e:  # e.g. run from the build folder instead of the installed copy
            log.info("updates disabled: %s", e)

    @property
    def enabled(self) -> bool:
        return self._um is not None

    def as_dict(self) -> dict:
        return {"status": self.status, "version": self.version, "progress": self.progress, "error": self.error,
                "checked_at": self.checked_at}

    def check_and_download(self) -> None:
        if not self.enabled or self.status == "ready" or not self._lock.acquire(blocking=False):
            return  # nothing to do, or a check is already running
        try:
            self.status, self.error = "checking", ""
            info = self._um.check_for_updates()
            if not info:
                self.status = "idle"
                return
            self.version, self.status, self.progress = info.TargetFullRelease.Version, "downloading", 0
            log.info("downloading update %s", self.version)
            self._um.download_updates(info, self._on_progress)
            self._pending, self.status = info, "ready"
            log.info("update %s downloaded; installs on restart", self.version)
        except Exception as e:  # offline, GitHub rate limit, bad package: try again later, keep running
            log.warning("update check failed: %s", e)
            self.status, self.error = "error", "Couldn't check for updates. Check the internet connection."
        finally:
            self.checked_at = time.time()
            self._lock.release()

    def _on_progress(self, *args) -> None:
        try:  # display only: never let an unexpected callback value stop the download
            self.progress = int(args[0])
        except (IndexError, TypeError, ValueError):
            pass

    def run(self) -> None:
        """Background thread: check at start, then every 15 minutes (test) or 4 hours (stable)."""
        while self.enabled and not self._stop.is_set():
            self.check_and_download()
            if self.status == "ready":
                return
            self._stop.wait(self._every)

    def stop(self) -> None:
        self._stop.set()

    def apply_and_restart(self) -> None:
        """Exits this process; Velopack installs the update and starts the new version."""
        if self._pending is not None:
            self._um.apply_updates_and_restart(self._pending)

    def apply_on_exit(self) -> None:
        """Install a downloaded update after Copilot has closed (silently, without restarting it)."""
        if self._pending is not None:
            try:
                self._um.wait_exit_then_apply_updates(self._pending, True, False)
            except Exception as e:
                log.error("could not schedule the update: %s", e)
            time.sleep(0.2)
