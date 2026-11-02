"""Logging for a windowed app: there is no console, so everything goes to rotating files in
%LOCALAPPDATA%\\Copilot\\logs. Secrets are never logged (the app has none; the licence token is not logged)."""
import io
import logging
import os
import sys
import threading
import zipfile
from logging.handlers import RotatingFileHandler
from pathlib import Path

FORMAT = "%(asctime)s %(levelname)s %(process)d %(name)s %(message)s"


def setup(log_dir: Path, filename: str = "copilot.log", level: str = "INFO", console: bool = False) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_dir / filename, maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter(FORMAT))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    if console and sys.stderr:
        stream = logging.StreamHandler()
        stream.setFormatter(logging.Formatter(FORMAT))
        root.addHandler(stream)
    root.setLevel(level)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per request otherwise

    # A windowed exe has no stdout/stderr (None): anything that prints would crash.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")

    def excepthook(kind, value, tb):
        logging.getLogger("copilot").critical("unhandled exception", exc_info=(kind, value, tb))

    sys.excepthook = excepthook
    threading.excepthook = lambda a: logging.getLogger("copilot").critical(
        "unhandled exception in thread %s", a.thread.name if a.thread else "?",
        exc_info=(a.exc_type, a.exc_value, a.exc_traceback))


def diagnostics_zip(log_dir: Path) -> bytes:
    """The log files, zipped, for the user to send when something goes wrong."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(log_dir.glob("*.log*")):
            zf.write(f, f.name)
    return buf.getvalue()


def last_line(path: Path, max_bytes: int = 8192) -> str:
    """Last non-empty line of a (possibly large) log file, for progress messages."""
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - max_bytes))
            lines = [ln for ln in fh.read().decode("utf-8", "replace").splitlines() if ln.strip()]
    except OSError:
        return ""
    return lines[-1] if lines else ""
