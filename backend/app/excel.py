"""Write results (a single search, or an employee's whole sheet) into the client's spreadsheet template."""
import re
from copy import copy
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import openpyxl
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from app.companies.house import CompanyMatch
from app.models import ResultRecord

# A-E are the client's original columns (B = the address as the business lists it). The rest mirror
# the dashboard. "comments" and "Status" are left empty for the caller to fill in. One row is one
# business: when it occupies several rated entries, every RV and Gov.uk record is listed.
BASE_COLUMNS = (
    ("name", "business name", None), ("address", "add", None), ("rv", "RV", None),
    ("phone", "Contact no", None), ("comments", "comments", None), ("status", "Status", 16),
)
OWNER_COLUMNS = (
    ("owner", "Owner / director", 30), ("company", "Company name", 32), ("company_number", "Company number", 15),
    ("company_status", "Company status", 14), ("incorporated", "Incorporated", 14),
    ("nature", "Nature of business", 40), ("owners_detail", "Owners (shareholding, since)", 50),
    ("directors_detail", "People (role, since)", 50), ("ch_link", "Companies House", 16),
    ("registered", "Registered office", 45),
)
DETAIL_COLUMNS = (
    ("others", "Other numbers at this address", 48), ("note", "Note", 40),
    ("description", "Description (VO)", 28), ("postcode", "Postcode", 10), ("maps", "Google Maps", 13),
    ("gov", "Gov.uk record", 15), ("vo_address", "VO address", 45), ("uarn", "VO UARN", 14),
    ("search", "Search", 30), ("added", "Added", 17),
)
LINK_KEYS = {"maps", "gov", "ch_link"}
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r", "\n")
_LINK = Font(name="Calibri", size=11, color="0563C1", underline="single")
_FILENAME_BAD = re.compile(r"[^A-Za-z0-9 ._()&'-]+")


@dataclass
class ExportRow:
    record: ResultRecord  # the business's main entry (highest RV)
    search: str = ""  # which search the row came from, e.g. "LONDON · EC1V 9LT · CO"
    added: datetime | None = None
    properties: list[ResultRecord] = field(default_factory=list)  # every VO entry for this business
    key: str = ""
    company: CompanyMatch | None = None

    def __post_init__(self):
        if not self.properties:
            self.properties = [self.record]


def safe_cell(value):
    """Neutralise spreadsheet formula injection for any text that came from an external source."""
    if isinstance(value, str):
        value = value.replace("\x00", "")
        if value.startswith(_FORMULA_START):
            return "'" + value
    return value


def export_filename(town: str, postcode: str, when: datetime) -> str:
    """Default download name, e.g. UK-Businesses_Coventry_CV1-1FJ_2026-10-26_2315.xlsx"""
    parts = [p for p in (town, postcode) if p] or ["search"]
    slug = "_".join(re.sub(r"[^A-Za-z0-9]+", "-", p).strip("-") for p in parts)[:60]
    return f"UK-Businesses_{slug}_{when:%Y-%m-%d_%H%M}.xlsx"


def clean_filename(name: str | None, fallback: str) -> str:
    """User-chosen download name -> safe "<name>.xlsx" (no paths, no odd characters)."""
    base = _FILENAME_BAD.sub(" ", re.sub(r"\.xlsx$", "", (name or "").strip(), flags=re.I))
    base = re.sub(r"\s+", " ", base).strip(" .")[:80]
    return f"{base}.xlsx" if base else fallback


ATTRIBUTION = (
    ("Rateable values & addresses",
     "Valuation Office (HMRC) compiled rating list, © Crown copyright. Supplied under the VO rating list "
     "terms: use is restricted to non-domestic rating purposes; onward disclosure only to parties using "
     "it for those purposes; delete when no longer needed."),
    ("Business names & phone numbers",
     "Overture Maps Foundation places (CDLA-Permissive-2.0; includes data from OpenStreetMap contributors, "
     "Meta, Microsoft, Foursquare and others — see https://docs.overturemaps.org/attribution/)."),
    ("Shared buildings",
     "The business listings say which businesses are in a building, not which unit. A unit is given to a "
     "business only when Companies House lists that company at the same unit number ('Unit confirmed' in Note). "
     "In business centres (4+ businesses) units whose occupier is not known are left out. In buildings with 2-3 "
     "businesses the most likely one is shown and Note says 'Shared building' - ask who is in that unit. Every "
     "number in the building is in 'Other numbers at this address'."),
    ("Owners / directors",
     "Companies House register and people-with-significant-control data (Contains public sector information "
     "licensed under the Open Government Licence v3.0). A company is included only when it is active, its name "
     "is similar and its registered office postcode is the premises' postcode. 'Owner / director' is the company's "
     "active officers (directors, partners, members, secretaries), never a resigned one or another company."),
    ("Before calling",
     "Unsolicited sales/marketing calls must be screened against TPS and CTPS (Privacy and Electronic "
     "Communications Regulations). This file has NOT been screened."),
)


def _rv_text(props: list[ResultRecord]):
    return props[0].rv if len(props) == 1 else " / ".join(f"£{p.rv:,}" for p in props)


def shared_note(row: ExportRow) -> str:
    n = row.record.shared_building
    if not n or row.record.occupier_unknown:
        return ""  # unknown occupiers carry their own note
    unit = row.record.number_or_name if len(row.properties) == 1 else "these units"
    n = f"{n}" if row.record.building_businesses else f"{n} or more"
    return (f"Shared building: {n} businesses listed here. The business name is the most likely one, "
            f"not confirmed - ask who is in {unit}.")


def address_for(rec: ResultRecord) -> str:
    """The address as the business lists it; for a unit in a shared building, the unit's own address."""
    if rec.unit_company or rec.shared_building:
        return rec.address
    return rec.business_address or rec.address


def _values(row: ExportRow) -> dict:
    rec, props, co = row.record, row.properties, row.company
    added = row.added or rec.collected_at
    values = {
        "name": rec.business_name or ("Occupier not known" if rec.occupier_unknown else ""), "address": address_for(rec), "rv": _rv_text(props),
        "phone": rec.phone, "comments": "", "status": "",
        "others": "; ".join(rec.other_numbers), "note": "; ".join(p for p in (shared_note(row) or rec.match_note, rec.phone_label) if p),
        "description": "; ".join(dict.fromkeys(p.description for p in props)), "postcode": rec.postcode,
        "maps": "Open map", "gov": "Open record" if len(props) == 1 else f"£{props[0].rv:,} record",
        "vo_address": "; ".join(p.address for p in props), "uarn": ", ".join(p.uarn for p in props),
        "search": row.search, "added": added.strftime("%Y-%m-%d %H:%M") if added else "",
    }
    if co:
        values |= {"owner": co.owner_names, "company": co.company_name, "company_number": co.company_number,
                   "company_status": co.status, "incorporated": co.incorporated,
                   "nature": "; ".join(co.nature_of_business),
                   "owners_detail": "\n".join(p.summary for p in co.owners),
                   "directors_detail": "\n".join(p.summary for p in co.directors),
                   "ch_link": "Open company", "registered": co.registered_address}
    return values


def write_workbook(
    template: Path, out_path: Path, rows: list[ExportRow], rv_header: str,
    about: list[tuple[str, str]] | None = None, owners: bool = False,
) -> None:
    wb = openpyxl.load_workbook(template)
    ws = wb.active
    style_row = [copy(c._style) for c in ws[2][:5]]
    header_style = copy(ws["A1"]._style)

    # Always open at the top-left (the client's original workbook was saved scrolled to row ~2,870).
    ws.sheet_view.topLeftCell = "A1"
    for sel in ws.sheet_view.selection:
        sel.activeCell, sel.sqref = "A2", "A2"
    for idx in [i for i in ws.row_dimensions if i > 1]:
        del ws.row_dimensions[idx]

    columns = list(BASE_COLUMNS + (OWNER_COLUMNS if owners else ()) + DETAIL_COLUMNS)
    # Businesses with several rated entries get one extra Gov.uk link column per further entry.
    extra_records = max((len(r.properties) for r in rows), default=1) - 1
    columns += [(f"gov{n}", f"Gov.uk record {n}", 15) for n in range(2, extra_records + 2)]
    col_of = {key: i for i, (key, _, _) in enumerate(columns, start=1)}

    for i, (key, title, width) in enumerate(columns, start=1):
        cell = ws.cell(row=1, column=i, value=rv_header if key == "rv" else title)
        if i > 5:
            cell._style = copy(header_style)
            ws.column_dimensions[get_column_letter(i)].width = width

    for r, row in enumerate(rows, start=2):
        values = _values(row)
        for key, c in col_of.items():
            value = values.get(key, "")
            value = "" if value is None else value
            # The phone is our own validated "+44 ..." string, never a formula; keep the "+" visible.
            cell = ws.cell(row=r, column=c, value=value if key == "phone" else safe_cell(value))
            if c <= 5:
                cell._style = copy(style_row[c - 1])
        ws.cell(row=r, column=col_of["phone"]).number_format = "@"
        links = {"maps": row.record.google_maps_url, "gov": row.properties[0].source_url}
        links |= {f"gov{n}": p.source_url for n, p in enumerate(row.properties[1:], start=2)}
        if row.company and owners:
            links["ch_link"] = row.company.url
        for n, p in enumerate(row.properties[1:], start=2):
            ws.cell(row=r, column=col_of[f"gov{n}"], value=f"£{p.rv:,} record")
        for key, url in links.items():
            link = ws.cell(row=r, column=col_of[key])
            link.hyperlink, link.font = url, _LINK

    ws.auto_filter.ref = f"A1:{get_column_letter(len(columns))}{max(len(rows) + 1, 2)}"
    ws.freeze_panes = "A2"

    if about:
        info = wb.create_sheet("About")
        info.column_dimensions["A"].width = 28
        info.column_dimensions["B"].width = 110
        for i, (key, value) in enumerate(about, start=1):
            info.cell(row=i, column=1, value=safe_cell(key)).font = Font(bold=True)
            info.cell(row=i, column=2, value=safe_cell(value))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".tmp")
    wb.save(tmp)
    tmp.replace(out_path)
