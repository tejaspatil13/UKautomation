"""Automatic-update decisions, with the remote checks mocked (no network)."""
from datetime import datetime

import pytest

from app.refresh import Dataset, check, in_window


def make(installed: str, latest, module="app.contacts.overture_import"):
    def latest_fn(_):
        if isinstance(latest, Exception):
            raise latest
        return latest
    return Dataset("test", module, None, lambda m: installed, latest_fn)


@pytest.mark.parametrize("installed,latest,expected", [
    ("2026-10-21.0", "2026-10-21.0", False),   # same release: nothing to do
    ("2026-10-21.0", "2026-11-18.0", True),    # Overture published a new release
    ("", "2026-11-18.0", True),                # nothing installed yet
])
def test_overture_update_decision(tmp_path, settings, installed, latest, expected):
    ds = make(installed, latest)
    ds.db_path = settings.voa_db_path  # any existing DB with a meta table
    check(settings, ds)
    assert ds.needs_update() is expected


@pytest.mark.parametrize("installed,latest,expected", [
    ("https://x/epoch-0004.zip#0xAAA", "https://x/epoch-0004.zip#0xAAA", False),
    ("https://x/epoch-0004.zip#0xAAA", "https://x/epoch-0004.zip#0xBBB", True),   # same file, republished
    ("https://x/epoch-0004.zip#0xAAA", "https://x/epoch-0005.zip#0xCCC", True),   # new epoch file
    ("https://x/epoch-0004.zip#", "https://x/epoch-0004.zip#0xBBB", False),       # old import without version info
])
def test_voa_update_decision(settings, installed, latest, expected):
    ds = make(installed, latest, module="app.voa.importer")
    ds.db_path = settings.voa_db_path
    check(settings, ds)
    assert ds.needs_update() is expected


def test_offline_check_keeps_current_data(settings):
    ds = make("2026-10-21.0", ConnectionError("offline"))
    ds.db_path = settings.voa_db_path
    check(settings, ds)
    assert not ds.needs_update() and "Could not check" in ds.last_error


@pytest.mark.parametrize("window,hour,expected", [
    ("01-05", 3, True), ("01-05", 5, False), ("01-05", 14, False), ("22-04", 23, True), ("22-04", 2, True), ("22-04", 12, False),
])
def test_night_window(window, hour, expected):
    assert in_window(window, datetime(2026, 10, 27, hour)) is expected


async def test_import_progress_reaches_the_dashboard(tmp_path, monkeypatch):
    """The import process writes its progress; the dataset status shows it while updating."""
    import sys

    from app import refresh

    code = "from app import progress; progress.report('Downloading', 5_000_000, 20_000_000, 'bytes', 2, 40, force=True)"
    monkeypatch.setattr(refresh, "import_command", lambda m: [sys.executable, "-c", code])
    ds = Dataset("Companies House data", "app.companies.bulk_import", tmp_path / "companies.sqlite",
                 lambda m: "", lambda s: "")
    assert await refresh.run_import(ds.module, None, ds.progress_file)

    ds.updating, ds.started_at = True, __import__("time").time() - 90
    p = ds.status()["progress"]
    assert (p["stage"], p["done"], p["total"], p["unit"], p["step"], p["steps"]) == \
        ("Downloading", 5_000_000, 20_000_000, "bytes", 2, 40)
    assert p["elapsed_s"] >= 90 and p["idle_s"] < 30

    ds.progress_file.write_text("{half a wri")  # read mid-write: keep showing the last good progress
    assert ds.status()["progress"]["done"] == 5_000_000
    ds.updating = False
    assert ds.status()["progress"] is None


def test_progress_is_silent_without_a_progress_file(monkeypatch, tmp_path):
    from app import progress

    monkeypatch.delenv("UKA_PROGRESS_FILE", raising=False)
    progress.report("Downloading", 1, 2, "bytes", force=True)  # command-line import: nothing to write
    monkeypatch.setenv("UKA_PROGRESS_FILE", str(tmp_path / "missing-dir" / "x.json"))
    progress.report("Downloading", 1, 2, "bytes", force=True)  # never raises
