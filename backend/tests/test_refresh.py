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
