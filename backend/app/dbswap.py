"""Put a freshly built dataset in place of the live one.

On Linux and macOS os.replace() works even while the app has the old file open. Windows refuses
while any connection to the file is open, so the importer then leaves the finished file beside the
live one as <name>.ready, and the running app installs it once it has closed its own connections
(app.refresh.install_ready). Either way the old file stays in use until the new one is complete.
"""
import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)


def ready_path(db_path: Path) -> Path:
    return db_path.with_suffix(".ready")


def replace(src: Path, db_path: Path, attempts: int = 5, delay: float = 1.0) -> bool:
    """os.replace with retries while the target is in use. True when db_path is now the new file."""
    for attempt in range(attempts):
        try:
            os.replace(src, db_path)
            return True
        except PermissionError:
            if attempt < attempts - 1:
                time.sleep(delay)
    return False


def swap_in(new: Path, db_path: Path) -> bool:
    """Used by the importers. True = installed now; False = kept as <name>.ready for the app to install."""
    if replace(new, db_path):
        ready_path(db_path).unlink(missing_ok=True)  # an older pending file is now stale
        return True
    os.replace(new, ready_path(db_path))
    log.warning("%s is in use; the new data was saved as %s and is installed as soon as the app releases it",
                db_path.name, ready_path(db_path).name)
    return False
