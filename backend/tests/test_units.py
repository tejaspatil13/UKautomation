"""Business centres: each unit's rateable value goes to its real occupier, or to nobody."""
from app.contacts.base import ContactCandidate
from app.models import ResultRecord
from app.units import occupiers, resolve_unit, unit_numbers
from tests.test_api import make_client  # noqa: F401  (fixture)

GUESS = "39 other business(es) also at this address — confirm who answers"
COMPANIES = [
    ("05199502", "ACCOLADE (UK) LIMITED", "UNIT 308 TOWER HILL BUSINESS CENTRE, 46-48 EAST SMITHFIELD, LONDON, E1W 1AW"),
    ("13000000", "KOME CARE LTD", "408 TOWER BRIDGE BUSINESS CENTRE, EAST SMITHFIELD, LONDON, E1W 1AW"),
    ("14000000", "OTHER BUILDING LTD", "UNIT 601, 66-68 EAST SMITHFIELD, LONDON, E1W 1AW"),
    ("15000000", "NO UNIT LTD", "TOWER BRIDGE BUSINESS CENTRE, 46-48 EAST SMITHFIELD, LONDON, E1W 1AW"),
]


def cand(name, phone):
    return ContactCandidate(name=name, address="46-48 East Smithfield, London", postcode="E1W 1AW", phone=phone,
                            source="overture")


CANDIDATES = [cand("Accolade Security", "02077093056"), cand("Tower Bridge Business Centre", "02079538690"),
              cand("Merlinco", "08702646329"), cand("Plumbtel Ltd.", "08001114464"), cand("Get Licensed", "02079657453")]


def unit(number_or_name, rv=10000, **kw):
    base = dict(uarn=number_or_name, address=f"{number_or_name}, East Smithfield, London, E1W 1AW", street="East Smithfield",
                number_or_name=number_or_name, town="London", postcode="E1W 1AW", description="Offices and Premises", rv=rv,
                business_name="Accolade Security", phone="+44 20 7709 3056", match_reasons=["postcode matches", GUESS],
                building_businesses=40)
    return ResultRecord(**(base | kw))


def test_unit_numbers_are_compared_without_dots():
    assert unit_numbers("UNIT 3.08 3RD FLR 46-48") == {"308"}
    assert unit_numbers("Units 2.12-2.14 2nd Flr 46-48") == {"212", "213", "214"}
    assert unit_numbers("SUITE 308,TOWER BRIDGE BUSINESS CENTRE") == {"308"}
    assert unit_numbers("408 TOWER BRIDGE BUSINESS CENTRE, EAST SMITHFIELD") == {"408"}
    assert unit_numbers("5th Flr 46-48") == set()


def test_confirmed_unit_gets_its_business_and_phone():
    rec = unit("UNIT 3.08 3RD FLR 46-48", rv=7500)
    resolve_unit(rec, CANDIDATES, COMPANIES)
    assert (rec.business_name, rec.phone, rec.unit_company) == ("Accolade Security", "+44 20 7709 3056", "ACCOLADE (UK) LIMITED")
    assert rec.shared_building == 0 and rec.is_lead and not rec.occupier_unknown


def test_other_units_are_not_credited_to_the_guess():
    rec = unit("Suite 6.01 6th Flr 46-48", rv=42250)
    resolve_unit(rec, CANDIDATES, COMPANIES)  # "UNIT 601" is in a different building (66-68)
    assert rec.occupier_unknown and rec.business_name == ""
    assert rec.phone == "+44 20 7953 8690" and rec.phone_label == "Building reception: Tower Bridge Business Centre"
    assert not rec.is_lead and rec.shared_building == 40  # no name: left out of the sheet and Excel


def test_confirmed_company_without_a_listing_uses_the_building_number():
    rec = unit("Unit 4.08 4th Flr 46-48")
    resolve_unit(rec, CANDIDATES, COMPANIES)
    assert (rec.business_name, rec.unit_company) == ("Kome Care Ltd", "KOME CARE LTD")
    assert rec.phone_label.startswith("Building reception")


def test_small_shared_buildings_keep_the_best_guess():
    rec = unit("Unit 3.08", building_businesses=3)
    resolve_unit(rec, CANDIDATES, COMPANIES)
    assert rec.business_name == "Accolade Security" and not rec.unit_company and not rec.occupier_unknown


def test_building_names_and_mailbox_codes_are_not_units():
    turks = unit("2nd Flr Fnt Left the Turks Head Studio 1")
    assert occupiers(turks, [("1", "LONDON MESOPOTAMIAN CIC", "HAPPINESS AT WORK STUDIO 1 GREEN BANK, WAPPING, E1W 2PA")]) == []
    intl = unit("Pt Grd Flr Suite 5 International House 1")
    assert occupiers(intl, [("2", "ALTA GRATIA LTD", "1 ST KATHARINE'S WAY, OFFICE 5-D179, LONDON, E1W 1UN")]) == []
    assert occupiers(intl, [("3", "REAL LTD", "SUITE 5 INTERNATIONAL HOUSE, 1 ST KATHARINE'S WAY, E1W 1UN")]) == ["REAL LTD"]
    thames = unit("Unit 104 1st Flr Thames House 566 Cable Street")
    assert occupiers(thames, [("4", "BAD TABLE MANNERS LTD", "UNIT 104, 566 CABLE STREET, LONDON, E1W 3HB"),
                              ("5", "ELSEWHERE LTD", "UNIT 104 OCEAN HOUSE, E1W 3HB")]) == ["BAD TABLE MANNERS LTD"]
    courtyard = unit("Unit B Gnd Flr Courtyard Offices 566 Cable Street")
    assert occupiers(courtyard, [("6", "3D AUDIO LAB LTD", "566 CABLE STREET, OFFICE 566, E1W 3HB")]) == []


def test_occupiers_need_the_same_building():
    assert occupiers(unit("Suite 6.01 6th Flr 46-48"), COMPANIES) == []
    assert occupiers(unit("Unit 6.01 66-68"), COMPANIES) == ["OTHER BUILDING LTD"]


def test_old_sheet_rows_are_rematched_and_stale_customers_cleaned(make_client):
    import json
    import sqlite3

    from app.companies.house import CompanyMatch
    from app.excel import ExportRow
    from app.grouping import group_rows
    from app.sheets import employee_key
    from tests.test_api import GW, FakeProvider, wait

    with make_client(FakeProvider({"300675222": [GW]})) as client:
        job = wait(client, client.post("/api/jobs", json={"postcode": "CV1 1FJ"}).json()["id"])
        client.post("/api/sheet/add", json={"employee": "Sara", "job_id": job["id"]})
        state = client.app.state
        # Saved with older rules (version 1): Games Workshop credited with 13 Trinity St
        rows = state.sheets.rows("Sara")
        for r in rows:
            r.record.match_version = 1
        state.sheets.update("Sara", [r.record for r in rows])
        gw_row = next(g for g in group_rows(rows, merge_shared=True) if g.record.business_name == "Games Workshop")
        state.owners._save_customer(employee_key("Sara"), gw_row, CompanyMatch(
            company_name="GAMES WORKSHOP LIMITED", company_number="01467092", registered_address="x"))
        assert len(state.owners.customers("Sara")) == 1  # (reading via the API would already re-match)

        # Now the building turns out to be a business centre: five equally likely businesses, unit unknown
        tenants = [GW.model_copy(update={"name": f"Tenant {i}", "phone": f"024 7600 00{i}0"}) for i in range(5)]
        state.jobs.providers = [FakeProvider({"300675222": tenants})]
        sheet = client.get("/api/sheet", params={"employee": "Sara"}).json()["items"]
        assert not any("300675222" in r["uarns"] for r in sheet)  # occupier unknown: not shown
        saved = sqlite3.connect(state.sheets.db_path).execute(
            "SELECT record_json FROM sheet_rows WHERE uarn = '300675222'").fetchone()[0]
        from app.jobs import MATCH_VERSION
        assert json.loads(saved)["match_version"] == MATCH_VERSION  # re-match was saved
        # ... so Games Workshop no longer gets that unit, and with nothing left it is no longer a customer
        assert client.get("/api/owners", params={"employee": "Sara"}).json()["total"] == 0


def test_excel_uses_the_unit_address_and_leads_need_a_name_and_number():
    from app.excel import address_for

    confirmed = unit("UNIT 3.08 3RD FLR 46-48", business_address="46-48 East Smithfield, London, E1W 1AW")
    resolve_unit(confirmed, CANDIDATES, COMPANIES)
    assert address_for(confirmed) == confirmed.address and confirmed.address.startswith("UNIT 3.08")
    no_phone = unit("Unit 1", phone="", unit_company="X LTD")
    assert not no_phone.is_lead  # nothing to call
    plain = unit("12", match_reasons=["postcode matches"], building_businesses=1,
                 business_address="12 High St, London, E1W 1AW")
    assert address_for(plain) == "12 High St, London, E1W 1AW" and plain.is_lead
