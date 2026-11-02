"""Progress of a running dataset import, shown on the dashboard while new data installs.

Each import runs in its own process (refresh.run_import). It writes its progress to a small JSON file
named by UKA_PROGRESS_FILE; the app reads that file for /api/meta. Progress is for display only, so
nothing here may stop an import: every file error is ignored.
"""
import json
import os
import time
from pathlib import Path

_last_write = 0.0


def report(stage: str, done: int | None = None, total: int | None = None, unit: str = "",
           step: int | None = None, steps: int | None = None, force: bool = False, quiet_s: int = 0) -> None:
    """stage: what is happening ("Downloading"); done/total in unit ("bytes", "rows", "places");
    step/steps for imports made of several files; quiet_s: how long this stage may go without a report
    before the dashboard warns it may be stuck (0 = the default). Written at most once a second unless force."""
    global _last_write
    path = os.environ.get("UKA_PROGRESS_FILE")
    if not path:
        return
    now = time.monotonic()
    if not force and now - _last_write < 1:
        return
    _last_write = now
    data = {"stage": stage, "done": done, "total": total, "unit": unit, "step": step, "steps": steps,
            "quiet_s": quiet_s, "at": time.time()}
    tmp = Path(path + ".tmp")
    try:
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, path)  # whole file or nothing: the app never reads half a write
    except OSError:
        pass  # e.g. Windows: the app was reading it at that moment; the next report gets through


def read(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
