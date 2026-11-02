"""Windows compatibility: swapping in new data while the app has the old file open, imports from the
packaged app, time zones. The locked-file tests run for real on Windows (CI) and are simulated elsewhere."""
import os
import sqlite3
import sys
from zoneinfo import ZoneInfo

import pytest

from app import dbswap, refresh
from app.companies.local import LocalCompanies
from app.contacts.local_places import LocalPlacesProvider
from app.contacts.overture_import import SCHEMA as PLACES_SCHEMA
from app.models import PropertyRecord
from app.refresh import Dataset

REC = PropertyRecord(uarn="1", address="13 Trinity Street", street="Trinity Street", number_or_name="13",
                     town="Coventry", postcode="CV1 1FJ", description="Shop", rv=1)


def places_db(path, name):
    con = sqlite3.connect(path)
    con.executescript(PLACES_SCHEMA)
    con.execute("INSERT INTO places VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("a", name, "", "13 Trinity St", "Coventry", "CV1 1FJ", "02476000000", "", 0.9, None, 52.4, -1.5, ""))
    con.commit()
    con.close()


def locked_on_this_os():
    """Make os.replace fail as Windows does when the target is in use, on every system, until released."""
    real = os.replace

    def fake(src, dst):
        if str(dst).endswith(".sqlite") and not getattr(fake, "released", False):
            raise PermissionError(32, "The process cannot access the file because it is being used")
        return real(src, dst)
    return fake


def test_swap_in_replaces_when_free(tmp_path):
    db, new = tmp_path / "places.sqlite", tmp_path / "places.importing"
    places_db(db, "Old")
    places_db(new, "New")
    assert dbswap.swap_in(new, db)
    assert not new.exists() and not dbswap.ready_path(db).exists()


def test_swap_in_keeps_new_file_ready_when_live_file_is_in_use(tmp_path, monkeypatch):
    db, new = tmp_path / "places.sqlite", tmp_path / "places.importing"
    places_db(db, "Old")
    places_db(new, "New")
    monkeypatch.setattr(dbswap.os, "replace", locked_on_this_os())
    monkeypatch.setattr(dbswap.time, "sleep", lambda s: None)
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)  # the running app's connection
    try:
        assert dbswap.swap_in(new, db) is False
    finally:
        con.close()
    assert dbswap.ready_path(db).exists()
    with sqlite3.connect(db) as check:  # the old data is untouched and still usable
        assert check.execute("SELECT name FROM places").fetchone()[0] == "Old"


async def test_install_ready_releases_connections_then_swaps(tmp_path, monkeypatch):
    """The real Windows sequence: the provider holds the file open; release() closes it; the swap succeeds."""
    db = tmp_path / "places.sqlite"
    places_db(db, "Old")
    places_db(dbswap.ready_path(db), "New")
    provider = LocalPlacesProvider(db)
    assert (await provider.candidates(REC))[0].name == "Old"  # connection now open

    fake = locked_on_this_os()
    monkeypatch.setattr(dbswap.os, "replace", fake)

    def release():
        provider.close()
        fake.released = True

    ds = Dataset("Business listings", "app.contacts.overture_import", db, lambda m: "", lambda s: "")
    assert await refresh.install_ready(ds, release, attempts=3)
    assert not dbswap.ready_path(db).exists()
    assert (await provider.candidates(REC))[0].name == "New"  # reopened on the next lookup


async def test_install_ready_gives_up_safely_and_keeps_old_data(tmp_path, monkeypatch):
    db = tmp_path / "places.sqlite"
    places_db(db, "Old")
    places_db(dbswap.ready_path(db), "New")
    monkeypatch.setattr(dbswap.os, "replace", locked_on_this_os())  # never released
    monkeypatch.setattr(refresh.asyncio, "sleep", _no_sleep)
    ds = Dataset("Business listings", "app.contacts.overture_import", db, lambda m: "", lambda s: "")
    assert await refresh.install_ready(ds, None, attempts=3) is False
    assert dbswap.ready_path(db).exists()  # installed at the next start instead
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT name FROM places").fetchone()[0] == "Old"


async def _no_sleep(_):
    return None


def test_leftover_ready_file_is_installed_at_startup(tmp_path):
    db = tmp_path / "places.sqlite"
    places_db(db, "Old")
    places_db(dbswap.ready_path(db), "New")
    refresh.install_ready_at_startup([Dataset("x", "m", db, lambda m: "", lambda s: "")])
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT name FROM places").fetchone()[0] == "New"


@pytest.mark.skipif(sys.platform != "win32", reason="real Windows file locking")
def test_windows_refuses_to_replace_an_open_database_until_it_is_closed(tmp_path):
    db, new = tmp_path / "places.sqlite", tmp_path / "places.importing"
    places_db(db, "Old")
    places_db(new, "New")
    provider = LocalPlacesProvider(db)
    assert dbswap.replace(new, db, attempts=1) is False  # what broke updates on Windows
    provider.close()
    assert dbswap.replace(new, db, attempts=1) is True


def test_local_companies_close_and_reopen(tmp_path):
    db = tmp_path / "companies.sqlite"
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE companies (number, name, postcode, address, status, category, incorporated, sic)")
        con.execute("INSERT INTO companies VALUES ('1', 'A Ltd', 'CV1 1FJ', 'x', 'Active', '', '', '')")
    companies = LocalCompanies(db)
    assert companies.at_postcode("CV1 1FJ")
    companies.close()
    companies.close()  # harmless twice
    assert companies.at_postcode("CV1 1FJ")


def test_import_command_in_packaged_app(monkeypatch):
    assert refresh.import_command("app.voa.importer")[1:] == ["-m", "app.voa.importer"]
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert refresh.import_command("app.voa.importer")[1:] == ["--run-import", "app.voa.importer"]


async def test_import_output_goes_to_the_log_file(tmp_path, monkeypatch):
    log_file = tmp_path / "import.log"
    monkeypatch.setattr(refresh, "import_command", lambda m: [sys.executable, "-c", "print('imported 50,000 rows')"])
    assert await refresh.run_import("app.voa.importer", log_file)
    assert "imported 50,000 rows" in log_file.read_text()
    assert not refresh._imports


def test_uk_time_zone_is_available():
    """Windows has no time-zone database of its own: needs the tzdata package (requirements.txt)."""
    assert ZoneInfo("Europe/London").key == "Europe/London"
