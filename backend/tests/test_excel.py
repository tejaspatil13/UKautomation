from datetime import datetime, timezone

import openpyxl
import pytest

from app.excel import ExportRow, clean_filename, export_filename, safe_cell, write_workbook
from app.models import ContactStatus, ResultRecord
from tests.conftest import TEMPLATE


def rec(**kw):
    base = dict(
        uarn="300675222", address="13, Trinity Street, Coventry, CV1 1FJ", street="Trinity Street",
        number_or_name="13", town="Coventry", postcode="CV1 1FJ", description="Shop and Premises", rv=22000,
        business_name="Games Workshop", phone="+44 24 7622 7311", contact_status=ContactStatus.VERIFIED,
        match_score=95, match_reasons=["postcode matches"], contact_source="overture",
        contact_source_url="https://example.com/gw", collected_at=datetime(2026, 10, 26, tzinfo=timezone.utc),
    )
    return ResultRecord(**(base | kw))


@pytest.mark.parametrize("value", ["=HYPERLINK(\"x\")", "+1", "-2+3", "@SUM(A1)", "\t=1", "\r=1"])
def test_safe_cell_neutralises_formulas(value):
    assert safe_cell(value).startswith("'")


def test_safe_cell_leaves_normal_values():
    assert safe_cell("Games Workshop") == "Games Workshop"
    assert safe_cell(22000) == 22000


def test_export_filename_is_safe():
    name = export_filename("../../etc/passwd", "CV1 1FJ", datetime(2026, 10, 26, 23, 15))
    assert name == "UK-Businesses_etc-passwd_CV1-1FJ_2026-10-26_2315.xlsx"
    assert "/" not in export_filename("a/b\\c", "", datetime(2026, 1, 1))


def test_clean_filename():
    assert clean_filename("Sara - My sheet", "x.xlsx") == "Sara - My sheet.xlsx"
    assert clean_filename("../../etc/passwd.xlsx", "x.xlsx") == "etc passwd.xlsx"
    assert clean_filename("   ", "default.xlsx") == "default.xlsx"
    assert clean_filename(None, "default.xlsx") == "default.xlsx"


def test_workbook_matches_template_and_opens_at_the_top(tmp_path):
    out = tmp_path / "out.xlsx"
    rows = [
        ExportRow(rec(other_numbers=["Warhammer: +44 24 7622 7311"], match_note="Same building number and postcode"),
                  "COVENTRY · CV1 1FJ"),
        ExportRow(rec(uarn="1", business_name="=cmd|' /C calc'!A0", phone="")),
    ]
    write_workbook(TEMPLATE, out, rows, "RV (2026 list)")

    tpl = openpyxl.load_workbook(TEMPLATE).active
    ws = openpyxl.load_workbook(out).active
    # Opens at the top (the client's sample was saved scrolled to row ~2,870)
    assert ws.sheet_view.topLeftCell == "A1"
    assert not [i for i in ws.row_dimensions if i > 1 and ws.row_dimensions[i].height]
    # Template columns A, B, D, E keep their exact headers; C carries the RV label; F.. mirror the dashboard
    assert [ws.cell(1, c).value for c in (1, 2, 4, 5)] == [tpl.cell(1, c).value for c in (1, 2, 4, 5)]
    assert ws["C1"].value == "RV (2026 list)"
    assert [ws.cell(1, c).value for c in range(6, 13)] == [
        "Status", "Other numbers at this address", "Note", "Description (VO)", "Postcode", "Google Maps",
        "Gov.uk record"]
    assert ws.cell(1, 15).value == "Search" and ws.cell(1, 16).value == "Added"
    # Formatting carried over from the template's data row
    assert ws["C2"].number_format == tpl["C2"].number_format == "#,##0"
    assert ws.column_dimensions["B"].width == tpl.column_dimensions["B"].width
    assert ws["A1"].font.b
    # Values
    assert ws["A2"].value == "Games Workshop" and ws["C2"].value == 22000
    assert ws["D2"].value == "+44 24 7622 7311" and ws["D2"].data_type == "s"   # "+" kept, not escaped
    assert ws["B2"].value == "13, Trinity Street, Coventry, CV1 1FJ"            # VO address when no listed one
    assert ws["E2"].value is None and ws["F2"].value is None                     # comments + Status left empty
    assert ws["G2"].value == "Warhammer: +44 24 7622 7311"
    assert ws["H2"].value == "Same building number and postcode"
    assert ws["O2"].value == "COVENTRY · CV1 1FJ"
    assert ws["L2"].hyperlink.target.endswith("/valuations/start/300675222")
    assert ws["K2"].hyperlink.target == (
        "https://www.google.com/maps/search/?api=1&query=Games+Workshop%2C+13%2C+Trinity+Street%2C+Coventry%2C+CV1+1FJ")
    assert ws["A3"].value.startswith("'=")           # formula injection neutralised
    assert ws.auto_filter.ref.startswith("A1:")
    assert len(ws["A"]) == 3


def test_listed_business_address_goes_in_column_b(tmp_path):
    out = tmp_path / "out.xlsx"
    write_workbook(TEMPLATE, out, [ExportRow(rec(business_address="13 Trinity St, Coventry, CV1 1FJ"))], "RV")
    ws = openpyxl.load_workbook(out).active
    assert ws["B2"].value == "13 Trinity St, Coventry, CV1 1FJ" and ws["M2"].value == "13, Trinity Street, Coventry, CV1 1FJ"


def test_google_maps_link_uses_business_listing():
    assert rec(business_name="").google_maps_url.endswith("query=13%2C+Trinity+Street%2C+Coventry%2C+CV1+1FJ")
    assert rec(business_address="13 Trinity St, Coventry, CV1 1FJ").google_maps_url.endswith(
        "query=Games+Workshop%2C+13+Trinity+St%2C+Coventry%2C+CV1+1FJ")


def test_about_sheet_is_added_after_results(tmp_path):
    out = tmp_path / "out.xlsx"
    write_workbook(TEMPLATE, out, [ExportRow(rec())], "RV", about=[("Search", "town=Coventry"), ("Note", "=1+1")])
    wb = openpyxl.load_workbook(out)
    assert wb.sheetnames == ["Results", "About"]
    assert wb["About"]["B1"].value == "town=Coventry" and wb["About"]["B2"].value == "'=1+1"


def test_business_with_several_entries_is_one_row_with_every_rv_and_record(tmp_path):
    from app.grouping import group_records

    out = tmp_path / "out.xlsx"
    rows = group_records([
        rec(uarn="1", rv=13500), rec(uarn="2", rv=37750), rec(uarn="3", rv=18500),
        rec(uarn="4", business_name="Other Shop", phone="+44 24 7600 1111", rv=9000),
    ], "LONDON")
    assert [len(r.properties) for r in rows] == [3, 1]
    write_workbook(TEMPLATE, out, rows, "RV")
    ws = openpyxl.load_workbook(out).active
    assert len(ws["A"]) == 3
    assert ws["C2"].value == "£37,750 / £18,500 / £13,500" and ws["C3"].value == 9000
    assert ws["L2"].value == "£37,750 record" and ws["L2"].hyperlink.target.endswith("/start/2")
    headers = [c.value for c in ws[1]]
    g2, g3 = headers.index("Gov.uk record 2") + 1, headers.index("Gov.uk record 3") + 1
    assert ws.cell(2, g2).hyperlink.target.endswith("/start/3") and ws.cell(2, g3).value == "£13,500 record"
    assert ws.cell(3, g2).value is None
    assert ws["N2"].value == "2, 3, 1"


def test_owner_columns(tmp_path):
    from app.companies.house import CompanyMatch
    from app.excel import ExportRow

    out = tmp_path / "out.xlsx"
    co = CompanyMatch(company_name="GAMES WORKSHOP LIMITED", company_number="01467092",
                      registered_address="Willow Road, Nottingham, NG7 2WS", owners=[], directors=["Kevin Rountree"])
    write_workbook(TEMPLATE, out, [ExportRow(rec(), company=co)], "RV", owners=True)
    ws = openpyxl.load_workbook(out).active
    headers = [c.value for c in ws[1]]
    assert headers[5:9] == ["Status", "Owner / director", "Company name", "Company number"]
    assert ws["G2"].value == "Kevin Rountree" and ws["I2"].value == "01467092"
    link = ws.cell(2, headers.index("Companies House") + 1)
    assert link.hyperlink.target.endswith("/company/01467092")
    assert ws.cell(2, headers.index("People (role, since)") + 1).value == "Kevin Rountree"


def test_business_centre_units_stay_separate_and_are_labelled(tmp_path):
    from app.grouping import group_records

    guess = dict(match_reasons=["postcode matches", "3 other business(es) also at this address — confirm who answers"],
                 building_businesses=40, address="Suite 6.01 6th Flr 46-48, East Smithfield, London, E1W 1AW")
    units = [rec(uarn="1", rv=42250, number_or_name="Suite 6.01 6th Flr 46-48", **guess),
             rec(uarn="2", rv=35250, number_or_name="Suite 6.02 6th Flr 46-48", **guess)]
    assert units[0].shared_building == 40
    rows = group_records(units)
    assert [len(r.properties) for r in rows] == [1, 1]  # one row per unit on screen and in Excel
    from app.grouping import group_rows
    from app.excel import ExportRow
    assert [len(r.properties) for r in group_rows([ExportRow(u) for u in units], merge_shared=True)] == [2]

    out = tmp_path / "out.xlsx"
    write_workbook(TEMPLATE, out, rows, "RV")
    ws = openpyxl.load_workbook(out).active
    assert ws["H2"].value == ("Shared building: 40 businesses listed here. The business name is the most likely one, "
                              "not confirmed - ask who is in Suite 6.01 6th Flr 46-48.")


def test_shared_building_count_recovered_for_older_records():
    old = rec(match_reasons=["2 other business(es) also at this address — confirm who answers"],
              match_note="Same building number and postcode; 5 other business(es) at this address")
    assert old.shared_building == 6
    assert rec(match_note="Same building number and postcode; 5 other business(es) at this address").shared_building == 0
