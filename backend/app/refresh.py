"""Keep the datasets up to date automatically.

Once a day the app asks each source whether a newer version has been published (a small request,
nothing big is downloaded). Only when there is something new does it download and import it, and
only inside the night-time window, so office use is never slowed down. Imports run as
subprocesses and build a new SQLite file that is swapped in atomically: searches keep working
throughout, and if anything fails the current data simply stays in place.

Run by hand:
    python -m app.refresh --check   # report what is installed and what is available
    python -m app.refresh --now     # update anything that is out of date, right now
"""
import argparse
import asyncio
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app import dbswap, progress
from app.config import Settings, get_settings

log = logging.getLogger(__name__)
BACKEND_DIR = Path(__file__).resolve().parents[1]
_imports: set[asyncio.subprocess.Process] = set()  # running import subprocesses


def read_meta(db_path: Path) -> dict[str, str] | None:
    if not db_path.exists():
        return None
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as con:
            return dict(con.execute("SELECT key, value FROM meta"))
    except sqlite3.Error:
        return None


def voa_latest(settings: Settings) -> str:
    """Identity of the newest published VO file: its URL plus the server's ETag / Last-Modified."""
    from app.voa.importer import latest_baseline_url, remote_version

    url = settings.voa_list_url or latest_baseline_url(settings.voa_list_year)
    return f"{url}#{remote_version(url)}"


def voa_installed(meta: dict[str, str]) -> str:
    return f"{meta.get('source_url', '')}#{meta.get('remote_version', '')}"


def overture_latest(settings: Settings) -> str:
    from app.contacts.overture_import import latest_release

    return latest_release()


def companies_latest(settings: Settings) -> str:
    from app.companies.bulk_import import latest_release

    return latest_release()


@dataclass
class Dataset:
    name: str
    module: str
    db_path: Path
    installed: Callable[[dict[str, str]], str]
    latest: Callable[[Settings], str]
    disk_needed_gb: float = 3.0  # free space an import needs (download + the new database beside the old)
    log_file: Path | None = None  # where the import subprocess writes its output (None = inherit)
    # status, shown on the dashboard
    installed_version: str = ""
    available_version: str = ""
    last_checked: str = ""
    last_error: str = ""
    updating: bool = False
    started_at: float = 0.0  # time.time() when the running update began
    extra: dict = field(default_factory=dict)
    _progress: dict | None = None  # last progress read, kept if one read fails mid-write

    def work_files(self) -> list[Path]:
        """Files this dataset's import writes while it runs (deleted when it finishes)."""
        files = [self.db_path.with_suffix(".importing")]
        if self.module == "app.companies.bulk_import":
            files.append(self.db_path.with_name("companies-part.zip.download"))
        return files

    @property
    def progress_file(self) -> Path:
        return self.db_path.with_name(self.db_path.stem + ".progress.json")

    def progress(self) -> dict | None:
        """What the running import is doing, for the dashboard (None when not updating)."""
        if not self.updating:
            return None
        self._progress = progress.read(self.progress_file) or self._progress
        p = dict(self._progress or {"stage": "Starting"})
        now = time.time()
        p["elapsed_s"] = int(now - self.started_at)
        # Seconds since the import last reported anything: a long silence means it may be stuck.
        p["idle_s"] = int(now - max(p.pop("at", 0) or 0, self.started_at))
        return p

    def needs_update(self) -> bool:
        if not self.available_version:
            return False  # could not check: keep what we have
        if not self.installed_version:
            return True  # nothing installed yet
        if self.module == "app.voa.importer":
            # Older imports didn't record the server version: only a new file name counts then.
            installed_url, _, installed_ver = self.installed_version.partition("#")
            latest_url, _, latest_ver = self.available_version.partition("#")
            return installed_url != latest_url or (bool(installed_ver) and installed_ver != latest_ver)
        return self.installed_version != self.available_version

    def status(self) -> dict:
        return {
            "name": self.name, "installed": self.installed_version.split("#")[0].rsplit("/", 1)[-1],
            "available": self.available_version.split("#")[0].rsplit("/", 1)[-1],
            "update_pending": self.needs_update(), "updating": self.updating, "progress": self.progress(),
            "last_checked": self.last_checked, "last_error": self.last_error,
        }


def datasets(settings: Settings) -> list[Dataset]:
    log_file = settings.import_log_path
    return [
        Dataset("Government rating list", "app.voa.importer", settings.voa_db_path, voa_installed, voa_latest, 2.0,
                log_file),
        Dataset("Business listings", "app.contacts.overture_import", settings.places_db_path,
                lambda m: m.get("release", ""), overture_latest, 3.0, log_file),
        Dataset("Companies House data", "app.companies.bulk_import", settings.companies_db_path,
                lambda m: m.get("release", ""), companies_latest, 3.5, log_file),
    ]


def free_gb(path: Path) -> float:
    return shutil.disk_usage(path).free / 1e9


def check(settings: Settings, ds: Dataset) -> None:
    """Fill in installed/available versions. Network problems are recorded, never raised."""
    meta = read_meta(ds.db_path)
    ds.installed_version = ds.installed(meta) if meta else ""
    ds.last_checked = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        ds.available_version = ds.latest(settings)
        ds.last_error = ""
    except Exception as e:  # offline, site changed, etc.
        ds.available_version = ""
        ds.last_error = f"Could not check for updates: {type(e).__name__}"
        log.warning("update check for %s failed: %s", ds.name, e)


def in_window(window: str, now: datetime | None = None) -> bool:
    """True when the local hour is inside "HH-HH" (e.g. "01-05"; wraps past midnight, e.g. "22-04")."""
    start, end = (int(x) for x in window.split("-"))
    hour = (now or datetime.now()).hour
    return start <= hour < end if start <= end else hour >= start or hour < end


def import_command(module: str) -> list[str]:
    if getattr(sys, "frozen", False):  # packaged desktop app: there is no python, the exe runs the importer
        return [sys.executable, "--run-import", module]
    return [sys.executable, "-m", module]


async def run_import(module: str, log_file: Path | None = None, progress_file: Path | None = None) -> bool:
    kwargs: dict = {} if getattr(sys, "frozen", False) else {"cwd": BACKEND_DIR}
    if progress_file:
        kwargs["env"] = {**os.environ, "UKA_PROGRESS_FILE": str(progress_file)}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    out = log_file.open("ab") if log_file else None
    try:
        proc = await asyncio.create_subprocess_exec(*import_command(module), stdout=out, stderr=out, **kwargs)
        _imports.add(proc)
        try:
            code = await proc.wait()
        finally:
            _imports.discard(proc)
            if proc.returncode is None:  # cancelled (app closing): don't leave the import running on its own
                proc.kill()
    finally:
        if out:
            out.close()
    (log.info if code == 0 else log.error)("dataset import %s finished with exit code %s", module, code)
    return code == 0


def stop_imports() -> None:
    """Kill running import subprocesses (app shutdown). The current data stays; the import runs again later."""
    for proc in list(_imports):
        if proc.returncode is None:
            proc.kill()


async def install_ready(ds: Dataset, release: Callable[[], None] | None = None, attempts: int = 60) -> bool:
    """Install a finished import the importer could not swap in itself (Windows: the file was in use).

    release() closes the app's own connections to the dataset; it runs right before each attempt so
    nothing can reopen the file in between. A search still reading it delays the swap by a few seconds.
    """
    ready = dbswap.ready_path(ds.db_path)
    if not ready.exists():
        return False
    for _ in range(attempts):
        if release:
            release()
        if dbswap.replace(ready, ds.db_path, attempts=1):
            log.info("%s: new data installed", ds.name)
            return True
        await asyncio.sleep(2)
    log.error("%s: the new data could not be installed because the file stayed in use; it is kept as %s "
              "and installed when the app next starts", ds.name, ready.name)
    return False


def install_ready_at_startup(state: list[Dataset]) -> None:
    """Before anything opens the datasets: install files a previous run finished but could not swap in,
    and clear what an import cut short by closing the app left behind (no import runs before start-up;
    the leftovers would otherwise look like an import still running and block the download)."""
    for ds in state:
        for f in [*ds.work_files(), ds.progress_file]:
            if f.exists():
                f.unlink(missing_ok=True)
                log.info("%s: removed %s left by an interrupted download", ds.name, f.name)
        ready = dbswap.ready_path(ds.db_path)
        if ready.exists() and dbswap.replace(ready, ds.db_path, attempts=3):
            log.info("%s: installed the data downloaded last time", ds.name)


async def update(ds: Dataset, on_updated: Callable[[], None] | None = None,
                 release: Callable[[], None] | None = None) -> bool:
    if ds.updating:
        return False
    # Work files of an import started from the command line, still being written.
    if any(f.exists() and time.time() - f.stat().st_mtime < 900 for f in ds.work_files()):
        ds.last_error = "Another data update is already running; try again when it has finished."
        return False
    free = free_gb(ds.db_path.parent)
    if free < ds.disk_needed_gb:
        ds.last_error = (f"Not enough free disk space to update (needs {ds.disk_needed_gb:.1f} GB, "
                         f"{free:.1f} GB free). The current data is still in use.")
        log.error("%s: %s", ds.name, ds.last_error)
        return False
    ds.progress_file.unlink(missing_ok=True)
    ds._progress, ds.started_at = None, time.time()
    ds.updating = True
    ds.last_error = ""
    try:
        log.info("updating %s: %s -> %s", ds.name, ds.installed_version or "nothing", ds.available_version)
        ok = await run_import(ds.module, ds.log_file, ds.progress_file)
        if ok:
            ds._progress = {"stage": "Installing the new data", "at": time.time()}
            ds.progress_file.unlink(missing_ok=True)
            await install_ready(ds, release)
            ds.installed_version = ds.installed(read_meta(ds.db_path) or {})
            if on_updated:
                on_updated()
        else:
            ds.last_error = "The last update failed; the previous data is still in use."
        return ok
    finally:
        ds.updating = False
        ds.progress_file.unlink(missing_ok=True)


async def install_pending(state: list[Dataset], callbacks: dict[str, Callable[[], None]],
                          releases: dict[str, Callable[[], None]] | None = None) -> None:
    """Install every dataset that has a newer version, one after another (used by the dashboard button)."""
    for ds in state:
        if ds.needs_update():
            await update(ds, callbacks.get(ds.module), (releases or {}).get(ds.module))


async def refresh_loop(settings: Settings, state: list[Dataset], callbacks: dict[str, Callable[[], None]],
                       releases: dict[str, Callable[[], None]] | None = None) -> None:
    """Daily check; imports new data at night (or straight away when a dataset is missing)."""
    await asyncio.sleep(30)  # let the server finish starting
    while True:
        for ds in state:
            await asyncio.to_thread(check, settings, ds)
            missing = not ds.installed_version
            if ds.needs_update() and ((missing and settings.auto_import_on_start) or
                                      (not missing and in_window(settings.refresh_window))):
                await update(ds, callbacks.get(ds.module), (releases or {}).get(ds.module))
        # Missing data (first download failed or was cut short): try again in 10 minutes. A pending
        # update: re-check hourly so it starts promptly in the night window. Otherwise once per interval.
        missing = settings.auto_import_on_start and any(not d.installed_version for d in state)
        pending = any(d.needs_update() for d in state)
        await asyncio.sleep(600 if missing else 3600 if pending else settings.refresh_check_hours * 3600)


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="report installed and available versions")
    group.add_argument("--now", action="store_true", help="download and install anything out of date now")
    args = parser.parse_args()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(message)s")
    for ds in datasets(settings):
        check(settings, ds)
        s = ds.status()
        state = "UPDATE AVAILABLE" if s["update_pending"] else ("up to date" if not s["last_error"] else s["last_error"])
        print(f"{ds.name}: installed {s['installed'] or '(none)'} | latest {s['available'] or '?'} | {state}")
        if args.now and ds.needs_update():
            ok = asyncio.run(update(ds))
            print(f"  -> {'updated' if ok else 'update FAILED (old data kept)'}")


if __name__ == "__main__":
    main()
