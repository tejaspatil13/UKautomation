import sqlite3

import pytest

from app.contacts.local_places import LocalPlacesProvider
from app.contacts.overture_import import SCHEMA
from app.matching import choose
from app.models import ContactStatus, PropertyRecord

REC = PropertyRecord(
    uarn="300675222", address="13, Trinity Street, Coventry, CV1 1FJ", street="Trinity Street",
    number_or_name="13", town="Coventry", postcode="CV1 1FJ", description="Shop and Premises", rv=22000,
)

PLACES = [
    # id, name, category, freeform, locality, postcode, phone, website, confidence, status, lat, lon, sources
    ("a", "Games Workshop", "hobby_store", "13 Trinity St", "Coventry", "CV1 1FJ", "+442476227311", "", 0.95, "open", 52.4093, -1.5097, "meta"),
    ("b", "Old Cafe", "cafe", "27 Trinity St", "Coventry", "CV1 1FJ", "02476000000", "", 0.9, "permanently_closed", 52.4094, -1.5096, "meta"),
    ("c", "No Postcode Barber", "barber", "15 Trinity Street", "Coventry", "", "02476111111", "", 0.8, None, 52.4095, -1.5098, "meta"),
    ("d", "Far Away Ltd", "office", "1 Other Rd", "Coventry", "", "02476222222", "", 0.9, None, 52.4300, -1.5000, "meta"),
]


@pytest.fixture
def provider(tmp_path):
    db = tmp_path / "places.sqlite"
    con = sqlite3.connect(db)
    con.executescript(SCHEMA)
    con.executemany("INSERT INTO places VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", PLACES)
    con.commit()
    con.close()
    return LocalPlacesProvider(db)


async def test_candidates_by_postcode_and_proximity(provider):
    cands = await provider.candidates(REC)
    names = {c.name: c for c in cands}
    assert set(names) == {"Games Workshop", "Old Cafe", "No Postcode Barber"}   # far place excluded
    assert names["Old Cafe"].closed
    assert names["No Postcode Barber"].distance_m is not None
    assert names["Games Workshop"].source_url.startswith("https://explore.overturemaps.org/")


async def test_end_to_end_match(provider):
    status, best, phone, *_ = choose(REC, await provider.candidates(REC))
    assert status == ContactStatus.VERIFIED and best.name == "Games Workshop" and phone == "+44 24 7622 7311"


async def test_repeat_lookups_are_cached(provider):
    await provider.candidates(REC)
    await provider.candidates(REC)
    assert provider._for_postcode.cache_info().hits >= 1


async def test_record_without_postcode_returns_nothing(provider):
    assert await provider.candidates(REC.model_copy(update={"postcode": ""})) == []


async def test_provider_reopens_after_dataset_swap(provider, tmp_path):
    import os

    await provider.candidates(REC)
    new = tmp_path / "new.sqlite"
    con = sqlite3.connect(new)
    con.executescript(SCHEMA)
    con.execute("INSERT INTO places VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("z", "New Tenant", "shop", "13 Trinity St", "Coventry", "CV1 1FJ", "02476999999", "", 0.9, None, 52.4093, -1.5097, "meta"))
    con.commit()
    con.close()
    if os.name == "nt":  # Windows can't replace an open file; the app releases it first (refresh.install_ready)
        provider.close()
    os.replace(new, provider.db_path)
    assert [c.name for c in await provider.candidates(REC)] == ["New Tenant"]
