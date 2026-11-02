"""Application updates with Velopack, from GitHub Releases.

Only the program is updated, never the datasets: they live in %LOCALAPPDATA%\\Copilot\\data, outside
the install folder Velopack replaces. Each build updates only from its own channel ("test" builds
from test pre-releases, "stable" builds from full releases), so the client never receives a build
that hasn't been approved. Velopack checks every package's SHA-256 before installing it.

An update is downloaded in the background and installed when the user chooses Restart now, or
automatically the next time Copilot is closed.
"""
import logging
import threading
import time

log = logging.getLogger(__name__)
CHECK_EVERY_S = 4 * 3600  # GitHub allows 60 unauthenticated API calls per hour per IP


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
        self._pending = None
        self._um = None
        self._stop = threading.Event()
        if not repo or channel == "dev":
            return
        try:
            import velopack

            source = velopack.GithubSource(repo, None, channel == "test")  # test builds read pre-releases
            self._um = velopack.UpdateManager(source)
            self.current = self._um.get_current_version()
            self.status = "idle"
        except Exception as e:  # e.g. run from the build folder instead of the installed copy
            log.info("updates disabled: %s", e)

    @property
    def enabled(self) -> bool:
        return self._um is not None

    def as_dict(self) -> dict:
        return {"status": self.status, "version": self.version, "progress": self.progress, "error": self.error}

    def check_and_download(self) -> None:
        if not self.enabled or self.status in ("checking", "downloading", "ready"):
            return
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
            self.status, self.error = "error", "Couldn't check for updates."

    def _on_progress(self, *args) -> None:
        try:  # display only: never let an unexpected callback value stop the download
            self.progress = int(args[0])
        except (IndexError, TypeError, ValueError):
            pass

    def run(self) -> None:
        """Background thread: check at start, then every 4 hours."""
        while self.enabled and not self._stop.is_set():
            self.check_and_download()
            if self.status == "ready":
                return
            self._stop.wait(CHECK_EVERY_S)

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
