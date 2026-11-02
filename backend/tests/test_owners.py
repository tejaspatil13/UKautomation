"""Companies House matching (local bulk data and online), potential customers, split downloads, update button."""
import io
import sqlite3
import time
import zipfile

import httpx
import openpyxl
import pytest

from app.companies.house import CompaniesHouse, parse_officers, parse_psc, parse_search, search_query
from app.companies.bulk_import import SCHEMA as COMPANIES_SCHEMA, read_companies, read_psc
from app.companies.house import CompanyMatch, Person
from app.companies.local import LocalCompanies
from app.models import ResultRecord
from tests.test_api import GW, FakeProvider, make_client, wait  # noqa: F401  (fixture re-export)

SEARCH_PAGE = """
<ul id='results' class="results-list">
<li class="type-company">
    <h3><a href="{{url}}"title="View company" class="govuk-link"> {{{title}}}</a></h3>
    <p>{{{ address_snippet }}}</p>
</li>
<li class="type-company">
    <h3><a class="govuk-link" href="/company/01467092" title="View company">GAMES WORKSHOP LIMITED</a></h3>
    <p class="meta crumbtrail"> 01467092 - Incorporated on 14 December 1979 </p>
    <p>13 Trinity Street, Coventry, England, CV1 1FJ</p>
</li>
<li class="type-company">
    <h3><a class="govuk-link" href="/company/09999999" title="View company">GAMES WORKSHOP &amp; CO LTD</a></h3>
    <p class="meta crumbtrail"> 09999999 - Dissolved on 1 May 2020 </p>
    <p>13 Trinity Street, Coventry, CV1 1FJ</p>
</li>
<li class="type-company">
    <h3><a class="govuk-link" href="/company/08888888" title="View company">GAMES WORKSHOP RETAIL LTD</a></h3>
    <p class="meta crumbtrail"> 08888888 - Incorporated on 1 May 2020 </p>
    <p>Willow Road, Nottingham, NG7 2WS</p>
</li>
</ul>
"""
OFFICERS_PAGE = """
<div class="appointments-list">
<div class="appointment-1"><h2><span id="officer-name-1"><a href="/officers/x">ROUNTREE, Kevin David</a></span></h2>
<dt>Role <span id="officer-status-tag-1" class="status-tag">Active</span></dt><dd id="officer-role-1">Director</dd>
<dd id="officer-appointed-on-1" class="data">
    1 March 2015
</dd></div>
<div class="appointment-2"><h2><span id="officer-name-2"><a href="/officers/y">SMITH, Jane</a></span></h2>
<dt>Role <span id="officer-status-tag-2" class="status-tag ceased-tag">Resigned</span></dt><dd id="officer-role-2">Director</dd></div>
<div class="appointment-3"><h2><span id="officer-name-3"><a href="/officers/z">JONES, Ann</a></span></h2>
<dt>Role <span id="officer-status-tag-3" class="status-tag">Active</span></dt><dd id="officer-role-3">Secretary</dd></div>
</div>
"""
PROFILE_PAGE = """
<dd class="text data" id="company-status">
    Active
</dd>
<span id="sic0">
    47650 - Retail sale of games and toys in specialised stores
</span>
"""
PSC_PAGE = """
<div class="appointment-1"><h2><span id="psc-name-1"><span><b>Mr Kevin Rountree</b></span></span>
<span id="psc-status-tag-1" class="status-tag">Active</span></h2>
<dd id="psc-notified-on-1" class="data">6 April 2016</dd>
<dd id="psc-noc-1-ownership-of-shares-75-to-100-percent" class="data">Ownership of shares</dd></div>
<div class="appointment-2"><h2><span id="psc-name-2"><span><b>Old Owner Ltd</b></span></span>
<span id="psc-status-tag-2" class="status-tag ceased-tag">Ceased</span></h2></div>
"""


def fake_companies_house(calls: list[str]) -> CompaniesHouse:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/search/companies":
            return httpx.Response(200, text=SEARCH_PAGE if "games" in request.url.params["q"].lower() else "")
        if request.url.path.endswith("/officers"):
            return httpx.Response(200, text=OFFICERS_PAGE)
        if request.url.path.endswith("/persons-with-significant-control"):
            return httpx.Response(200, text=PSC_PAGE)
        if request.url.path.startswith("/company/"):
            return httpx.Response(200, text=PROFILE_PAGE)
        return httpx.Response(404)

    return CompaniesHouse(min_interval=0, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


# ---------- Companies House ----------
def test_parsers_read_the_public_pages():
    hits = parse_search(SEARCH_PAGE)
    assert [(h.number, h.active, h.postcode) for h in hits] == [
        ("01467092", True, "CV1 1FJ"), ("09999999", False, "CV1 1FJ"), ("08888888", True, "NG7 2WS")]
    assert hits[1].name == "GAMES WORKSHOP & CO LTD" and hits[0].incorporated == "14 December 1979"
    officers = parse_officers(OFFICERS_PAGE)
    assert [p.name for p in officers] == ["Kevin David Rountree", "Ann Jones"]  # resigned left out
    assert officers[0].summary == "Kevin David Rountree (Director since 1 Mar 2015)"
    owners = parse_psc(PSC_PAGE)
    assert [p.name for p in owners] == ["Mr Kevin Rountree"]
    assert owners[0].summary == "Mr Kevin Rountree (Owner since 6 Apr 2016; Ownership of shares – 75% or more)"
    assert search_query("Costa Coffee - Coventry") == "Costa Coffee"


async def test_find_requires_matching_postcode_and_active_company():
    calls = []
    ch = fake_companies_house(calls)
    match = await ch.find("Games Workshop", {"CV1 1FJ"})
    assert match.company_number == "01467092" and match.owner_names == "Kevin David Rountree, Ann Jones"
    assert [p.name for p in match.directors] == ["Kevin David Rountree", "Ann Jones"]
    assert match.status == "Active" and match.incorporated == "14 December 1979"
    assert match.nature_of_business == ["47650 - Retail sale of games and toys in specialised stores"]
    assert await ch.find("Games Workshop", {"B1 1AA"}) is None  # no registered office there
    assert await ch.find("Totally Different", {"CV1 1FJ"}) is None


# ---------- local bulk data ----------
BASIC_CSV = (
    'CompanyName, CompanyNumber,RegAddress.CareOf,RegAddress.POBox,RegAddress.AddressLine1, RegAddress.AddressLine2,'
    'RegAddress.PostTown,RegAddress.County,RegAddress.Country,RegAddress.PostCode,CompanyCategory,CompanyStatus,'
    'CountryOfOrigin,DissolutionDate,IncorporationDate,SICCode.SicText_1,SICCode.SicText_2,SICCode.SicText_3,SICCode.SicText_4\n'
    '"GAMES WORKSHOP LIMITED","01467092","","","13 TRINITY STREET","","COVENTRY","","ENGLAND","CV1 1FJ",'
    '"Private Limited Company","Active","United Kingdom","","14/12/1979","47650 - Retail sale of games and toys","","",""\n'
    '"GAMES WORKSHOP RETAIL LTD","08888888","","","WILLOW ROAD","","NOTTINGHAM","","ENGLAND","NG7 2WS",'
    '"Private Limited Company","Active","United Kingdom","","01/05/2020","None Supplied","","",""\n'
    '"NO POSTCODE LTD","07777777","","","SOMEWHERE","","","","","",'
    '"Private Limited Company","Active","United Kingdom","","01/05/2020","","","",""\n'
)
PSC_LINES = [
    {"company_number": "01467092", "data": {"kind": "individual-person-with-significant-control",
     "name": "Mr Kevin Rountree", "notified_on": "2016-04-06", "natures_of_control": ["ownership-of-shares-75-to-100-percent"]}},
    {"company_number": "01467092", "data": {"kind": "corporate-entity-person-with-significant-control",
     "name": "Games Workshop Group Plc", "notified_on": "2016-04-06", "natures_of_control": []}},
    {"company_number": "01467092", "data": {"kind": "individual-person-with-significant-control",
     "name": "Old Owner", "ceased_on": "2019-01-01", "natures_of_control": []}},
    {"company_number": "01467092", "data": {"kind": "persons-with-significant-control-statement", "statement": "x"}},
    {"company_number": "99999999", "data": {"kind": "individual-person-with-significant-control", "name": "Elsewhere"}},
]


@pytest.fixture
def companies_db(settings, tmp_path):
    basic, psc = tmp_path / "basic.zip", tmp_path / "psc.zip"
    with zipfile.ZipFile(basic, "w") as zf:
        zf.writestr("BasicCompanyData.csv", BASIC_CSV)
    with zipfile.ZipFile(psc, "w") as zf:
        zf.writestr("psc.txt", "\n".join(__import__("json").dumps(x) for x in PSC_LINES) + "\n")
    con = sqlite3.connect(settings.companies_db_path)
    con.executescript(COMPANIES_SCHEMA)
    con.executemany("INSERT INTO companies VALUES (?,?,?,?,?,?,?,?)", read_companies(basic))
    wanted = {r[0] for r in con.execute("SELECT number FROM companies")}
    con.executemany("INSERT INTO psc VALUES (?,?,?,?,?,?)", read_psc(psc, wanted))
    con.commit()
    con.close()
    return settings.companies_db_path


def test_bulk_files_are_read(companies_db):
    con = sqlite3.connect(companies_db)
    assert [r[0] for r in con.execute("SELECT number FROM companies ORDER BY number")] == ["01467092", "08888888"]
    assert con.execute("SELECT address, incorporated, sic FROM companies WHERE number = '01467092'").fetchone() == (
        "13 TRINITY STREET, COVENTRY, CV1 1FJ", "1979-12-14", "47650 - Retail sale of games and toys")
    assert con.execute("SELECT sic FROM companies WHERE number = '08888888'").fetchone() == ("",)
    assert sorted(con.execute("SELECT name, corporate FROM psc")) == [("Games Workshop Group Plc", 1), ("Mr Kevin Rountree", 0)]


def test_local_match_by_postcode_and_name(companies_db):
    local = LocalCompanies(companies_db)
    m = local.find("Games Workshop", {"CV1 1FJ"})
    assert (m.company_number, m.status, m.incorporated) == ("01467092", "Active", "14 Dec 1979")
    assert m.nature_of_business == ["47650 - Retail sale of games and toys"]
    assert [(p.name, p.corporate) for p in m.owners] == [("Mr Kevin Rountree", False), ("Games Workshop Group Plc", True)]
    assert m.owners[0].control == ["Ownership of shares – 75% or more"]
    assert local.find("Games Workshop", {"B1 1AA"}) is None
    assert local.find("Totally Different", {"CV1 1FJ"}) is None


def test_owner_name_is_the_active_officer_not_a_company():
    # The Clove Club: owned by "The Clove Club Holdings Ltd", run by Isaac Mchale
    m = CompanyMatch(company_name="THE CLOVE CLUB LTD", company_number="15009450", registered_address="x",
                     owners=[Person(name="The Clove Club Holdings Ltd", role="Owner", corporate=True)],
                     directors=[Person(name="Isaac Mchale", role="Director"),
                                Person(name="ACME SECRETARIES LIMITED", role="Secretary")])
    assert m.owner_names == "Isaac Mchale"
    two = m.model_copy(update={"directors": [Person(name="Ann Lee", role="LLP Member"), Person(name="Raj Patel", role="LLP Member")]})
    assert two.owner_names == "Ann Lee, Raj Patel"
    # no officers fetched yet: individual owners; old saved matches (plain names) still load
    assert m.model_copy(update={"directors": [], "owners": [Person(name="Mr Kevin Rountree")]}).owner_names == "Mr Kevin Rountree"
    assert CompanyMatch.model_validate({"company_name": "X", "company_number": "1", "registered_address": "",
                                        "owners": ["A Person"], "directors": []}).owner_names == "A Person"


def test_owner_search_uses_local_data_and_only_fetches_officers(make_client, companies_db):
    calls = []
    with make_client(FakeProvider({"300675222": [GW]})) as client:
        client.app.state.owners.client = fake_companies_house(calls)
        job = wait(client, client.post("/api/jobs", json={"postcode": "CV1 1FJ"}).json()["id"])
        client.post("/api/sheet/add", json={"employee": "Sara", "job_id": job["id"]})
        for _ in range(2):  # second run: officers come from the 30-day memory
            client.post("/api/owners/run", json={"employee": "Sara"})
            while (st := client.get("/api/owners/status", params={"employee": "Sara"}).json())["running"]:
                time.sleep(0.02)
        assert st["found"] == 1 and "register" in st["message"]
        assert calls == ["/company/01467092/officers"]  # no searches, one officers request in total
        co = client.get("/api/owners", params={"employee": "Sara"}).json()["items"][0]["company"]
        assert co["owner_names"] == "Kevin David Rountree, Ann Jones" and co["status"] == "Active"
        assert [p["name"] for p in co["directors"]] == ["Kevin David Rountree", "Ann Jones"]  # secretary is active too


# ---------- API: grouping, owners, split download, updates ----------
def test_same_business_at_several_entries_is_one_row(make_client):
    # 13 and 15 Trinity St (CV1 1FJ) both resolve to the same business and number
    gw_15 = GW.model_copy(update={"address": "13-15 Trinity St, Coventry"})
    with make_client(FakeProvider({"300675222": [GW], "113420222": [gw_15]})) as client:
        job = wait(client, client.post("/api/jobs", json={"postcode": "CV1 1FJ"}).json()["id"])
        body = client.get(f"/api/jobs/{job['id']}/records", params={"has_phone": True}).json()
        assert body["total"] == 1 and body["properties"] == 2
        row = body["items"][0]
        assert [p["rv"] for p in row["properties"]] == [22000, 10750] and row["rv"] == 22000
        assert sorted(row["uarns"]) == ["113420222", "300675222"]
        ws = openpyxl.load_workbook(io.BytesIO(client.get(f"/api/jobs/{job['id']}/download").content)).active
        assert len(ws["A"]) == 2 and ws["C2"].value == "£22,000 / £10,750"

        r = client.post("/api/sheet/add", json={"employee": "Sara", "job_id": job["id"]}).json()
        assert (r["added"], r["total"]) == (1, 1)
        sheet = client.get("/api/sheet", params={"employee": "Sara"}).json()
        assert sheet["total"] == 1 and sheet["items"][0]["owner_checked"] is False


def test_owner_search_fills_potential_customers(make_client, monkeypatch):
    calls = []
    with make_client(FakeProvider({"300675222": [GW]})) as client:
        client.app.state.owners.client = fake_companies_house(calls)
        job = wait(client, client.post("/api/jobs", json={"postcode": "CV1 1FJ"}).json()["id"])
        client.post("/api/sheet/add", json={"employee": "Sara", "job_id": job["id"]})
        state = client.post("/api/owners/run", json={"employee": "Sara"}).json()
        assert state["running"] and state["total"] == 1
        deadline = time.time() + 5
        while client.get("/api/owners/status", params={"employee": "Sara"}).json()["running"]:
            assert time.time() < deadline
            time.sleep(0.02)
        assert client.get("/api/owners/status", params={"employee": "Sara"}).json()["found"] == 1

        customers = client.get("/api/owners", params={"employee": "Sara"}).json()
        assert customers["total"] == 1
        co = customers["items"][0]["company"]
        assert co["company_number"] == "01467092" and co["owner_names"] == "Kevin David Rountree, Ann Jones"
        assert client.get("/api/sheet", params={"employee": "Sara"}).json()["items"][0]["owner_checked"]

        # second run uses the cache: no new Companies House requests
        n = len(calls)
        client.post("/api/owners/run", json={"employee": "Sara"})
        time.sleep(0.1)
        assert len(calls) == n

        r = client.post("/api/owners/download", json={"employee": "Sara", "filename": "Leads"})
        assert r.status_code == 200
        ws = openpyxl.load_workbook(io.BytesIO(r.content)).active
        assert ws["G1"].value == "Owner / director" and ws["G2"].value == "Kevin David Rountree, Ann Jones"

        key = customers["items"][0]["key"]
        assert client.post("/api/owners/remove", json={"employee": "Sara", "keys": [key]}).json()["total"] == 0


def test_sheet_split_into_parts(make_client):
    big = GW.model_copy(update={"name": "Big Shop", "phone": "024 7600 1111", "address": "9-11 Trinity St, Coventry"})
    cafe = GW.model_copy(update={"name": "Trinity Cafe", "phone": "024 7600 2222", "categories": ["cafe"],
                                 "address": "27-29 Trinity St, Coventry"})
    with make_client(FakeProvider({"300675222": [GW], "235950222": [big], "11602154000": [cafe]})) as client:
        job = wait(client, client.post("/api/jobs", json={"postcode": "CV1 1FJ"}).json()["id"])
        client.post("/api/sheet/add", json={"employee": "Sara", "job_id": job["id"]})
        r = client.post("/api/sheet/download", json={"employee": "Sara", "filename": "Monday", "parts": 2})
        assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
        zf = zipfile.ZipFile(io.BytesIO(r.content))
        assert zf.namelist() == ["Monday - part 1 of 2.xlsx", "Monday - part 2 of 2.xlsx"]
        sizes = [len(openpyxl.load_workbook(io.BytesIO(zf.read(n))).active["A"]) - 1 for n in zf.namelist()]
        assert sizes == [2, 1]
        # more parts than rows: one file per row
        r = client.post("/api/sheet/download", json={"employee": "Sara", "filename": "x", "parts": 10})
        assert len(zipfile.ZipFile(io.BytesIO(r.content)).namelist()) == 3


def test_update_button_checks_then_installs(make_client, monkeypatch):
    import app.main as main
    import app.refresh as refresh

    installed = []

    async def fake_import(module, log_file=None):
        installed.append(module)
        return True

    monkeypatch.setattr(refresh, "run_import", fake_import)
    monkeypatch.setattr(refresh, "free_gb", lambda path: 100.0)  # independent of this machine's disk
    with make_client() as client:
        for ds in client.app.state.updates:
            monkeypatch.setattr(ds, "latest", lambda s, name=ds.name: f"{name}-v2")
            monkeypatch.setattr(ds, "installed", lambda m, name=ds.name: f"{name}-v1")
        with sqlite3.connect(main.settings.places_db_path) as con:
            con.execute("CREATE TABLE meta (key TEXT, value TEXT)")
        statuses = client.post("/api/updates/check").json()
        assert [s["update_pending"] for s in statuses] == [True, True, True]
        assert client.post("/api/updates/install").status_code == 202
        deadline = time.time() + 5
        while len(installed) < 3:
            assert time.time() < deadline
            time.sleep(0.02)
        assert installed == ["app.voa.importer", "app.contacts.overture_import", "app.companies.bulk_import"]


def test_install_refused_when_disk_is_nearly_full(make_client, monkeypatch):
    import app.refresh as refresh

    monkeypatch.setattr(refresh, "free_gb", lambda path: 0.5)
    with make_client() as client:
        ds = client.app.state.updates[0]
        ds.installed_version, ds.available_version = "a", "b"
        import asyncio
        assert asyncio.run(refresh.update(ds)) is False
        assert "Not enough free disk space" in ds.last_error


async def test_api_mode_reads_the_official_service():
    responses = {
        "/search/companies": {"items": [{"company_number": "01467092", "title": "GAMES WORKSHOP LIMITED",
                                         "company_status": "active", "date_of_creation": "1979-12-14",
                                         "address_snippet": "13 Trinity Street, Coventry, CV1 1FJ"}]},
        "/company/01467092": {"company_status": "active", "type": "ltd", "date_of_creation": "1979-12-14",
                              "sic_codes": ["47650"]},
        "/company/01467092/persons-with-significant-control": {"items": [
            {"name": "Mr Kevin Rountree", "notified_on": "2016-04-06",
             "natures_of_control": ["ownership-of-shares-75-to-100-percent"]},
            {"name": "Old Owner", "ceased_on": "2020-01-01"}]},
        "/company/01467092/officers": {"items": [
            {"name": "ROUNTREE, Kevin", "officer_role": "director", "appointed_on": "2015-03-01", "occupation": "COMPANY DIRECTOR"},
            {"name": "SMITH, Jo", "officer_role": "secretary"}]},
    }
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization", ""))
        return httpx.Response(200, json=responses[request.url.path])

    ch = CompaniesHouse(api_key="k", min_interval=0, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    m = await ch.find("Games Workshop", {"CV1 1FJ"})
    assert (m.status, m.company_type, m.incorporated) == ("Active", "Private limited company", "14 Dec 1979")
    assert m.nature_of_business == ["47650 - Retail sale of games and toys in specialised stores"]
    assert [p.summary for p in m.owners] == ["Mr Kevin Rountree (Owner since 6 Apr 2016; Ownership of shares – 75% or more)"]
    assert [p.summary for p in m.directors] == ["Kevin Rountree (Director since 1 Mar 2015; Company Director)",
                                                "Jo Smith (Secretary)"]


def test_block_stops_lookups_but_restores_saved_owners(make_client):
    calls = []
    with make_client(FakeProvider({"300675222": [GW]})) as client:
        owners = client.app.state.owners
        owners.client = fake_companies_house(calls)
        job = wait(client, client.post("/api/jobs", json={"postcode": "CV1 1FJ"}).json()["id"])
        client.post("/api/sheet/add", json={"employee": "Sara", "job_id": job["id"]})
        client.post("/api/owners/run", json={"employee": "Sara"})
        while client.get("/api/owners/status", params={"employee": "Sara"}).json()["running"]:
            time.sleep(0.02)
        client.delete("/api/owners", params={"employee": "Sara"})  # list cleared by the user

        # Companies House now refuses every request
        def blocked(request):
            calls.append("blocked")
            return httpx.Response(403)
        owners.client = CompaniesHouse(min_interval=0, client=httpx.AsyncClient(transport=httpx.MockTransport(blocked)))
        # one more never-checked business, so there is something that needs the network
        cafe = GW.model_copy(update={"name": "Trinity Cafe", "phone": "024 7600 2222", "address": "27-29 Trinity St, Coventry"})
        client.app.state.jobs.providers = [FakeProvider({"300675222": [GW], "11602154000": [cafe]})]
        job2 = wait(client, client.post("/api/jobs", json={"postcode": "CV1 1FJ"}).json()["id"])
        client.post("/api/sheet/add", json={"employee": "Sara", "job_id": job2["id"]})
        n = len(calls)
        client.post("/api/owners/run", json={"employee": "Sara"})
        while (st := client.get("/api/owners/status", params={"employee": "Sara"}).json())["running"]:
            time.sleep(0.02)
        assert st["blocked"] and st["found"] == 1 and st["skipped"] >= 1
        assert "temporarily blocked" in st["message"]
        assert calls[n:] == ["blocked"]  # stopped asking after the first 403
        assert client.get("/api/owners", params={"employee": "Sara"}).json()["total"] == 1  # restored from saved


def test_names_and_dates_are_tidied():
    from app.companies.house import _person_name, nice_date

    assert _person_name("LO FIEGO, Giancarlo, Mr.") == "Giancarlo Lo Fiego"
    assert _person_name("GODSMARK, Timothy John") == "Timothy John Godsmark"
    assert _person_name("Mr Timothy Godsmark") == "Mr Timothy Godsmark"
    assert nice_date("1 March 2009") == nice_date("2009-03-01") == "1 Mar 2009"
    assert nice_date("sometime") == "sometime"
