"""Business centres: work out which business is in which unit.

The business listings say which businesses are in a building, never which suite. In a building
with many businesses every unit therefore "matches" the same most-likely business, and that one
business would collect every unit's rateable value. Instead:

1. If Companies House has a company registered at the same unit number (the rating list writes
   "Unit 3.08", Companies House "Unit 308"), that company is the occupier. Its phone comes from
   the listings when the names match.
2. Otherwise the occupier is not known. The unit stays as a lead, with the building's own number
   (its reception) when the listings have one, so a caller can ask who is in that unit.

Buildings with only two or three businesses keep the normal best guess (tagged "shared building").
"""
import re

from rapidfuzz import fuzz

from app.contacts.base import ContactCandidate
from app.matching import normalize_phone, score_candidate
from app.models import ResultRecord
from app.normalize import house_numbers, name_key

BUSINESS_CENTRE_MIN = 4  # businesses listed in one building
NAME_MATCH = 80
# "Studio" and "Offices" are left out: they are usually part of a building's name ("The Turks Head
# Studio 1", "Courtyard Offices 566"). A number followed by "-letter" is a virtual-office mailbox code
# ("Office 5-D179"), not a unit.
_UNIT = re.compile(
    r"\b(?:UNITS?|SUITES?|OFFICE|ROOMS?)\s*(?:NO\.?\s*)?([0-9]+(?:\.[0-9]+)?[A-Z]?)(?!\s*-\s*[A-Z])"
    r"(?:\s*(?:-|TO|&)\s*([0-9]+(?:\.[0-9]+)?[A-Z]?))?\b", re.I)
_LEADING_UNIT = re.compile(r"^\s*([0-9]{3,4}[A-Z]?)\s+[A-Z]", re.I)  # "408 TOWER BRIDGE BUSINESS CENTRE, ..."
_RECEPTION = re.compile(
    r"business (?:centre|center)|serviced offices?|office (?:centre|center)|work ?space|co-?working|"
    r"\bregus\b|\bwework\b|\biwg\b|\bspaces\b|\bhub\b", re.I)


def _norm(token: str) -> str:
    """"3.08" -> "308", "06" -> "6": the rating list and Companies House write units differently."""
    return token.replace(".", "").upper().lstrip("0") or "0"


def unit_numbers(text: str) -> set[str]:
    """Unit numbers in an address: "Units 2.12-2.14 2nd Flr" -> {"212", "213", "214"}."""
    found = set()
    for a, b in _UNIT.findall(text or ""):
        found.add(_norm(a))
        if b:
            lo, hi = _norm(a), _norm(b)
            found.add(hi)
            if lo.isdigit() and hi.isdigit() and 0 < int(hi) - int(lo) <= 20:
                found.update(str(n) for n in range(int(lo), int(hi) + 1))
    if m := _LEADING_UNIT.match(text or ""):
        found.add(_norm(m.group(1)))
    return found


def _building_numbers(text: str) -> set[str]:
    return house_numbers(_UNIT.sub(" ", _LEADING_UNIT.sub(" ", text or "")))


# Words that say nothing about which building it is.
_GENERIC = set("""
UNIT UNITS SUITE SUITES OFFICE OFFICES ROOM ROOMS STUDIO STUDIOS FLR FLOOR FLOORS GRD GND GROUND BASEMENT BST LGF UGF
MEZZ MEZZANINE PT PART FNT FRONT REAR LEFT RIGHT AND THE OF AT IN ON TO WITH NO BLOCK WING EAST WEST NORTH SOUTH
HOUSE BUILDING BUILDINGS CENTRE CENTER BUSINESS COURT YARD LTD LIMITED
""".split())
_WORD = re.compile(r"[A-Z][A-Z']{2,}")


def building_words(text: str) -> set[str]:
    """Distinctive words of a building name: "Unit 104 1st Flr Thames House 5" -> {"THAMES"}."""
    return {w for w in _WORD.findall((text or "").upper()) if w not in _GENERIC and not re.match(r"\d", w)}


def occupiers(rec: ResultRecord, companies: list[tuple[str, str, str]]) -> list[str]:
    """Names of companies registered at this unit (same unit number, same building) at the postcode."""
    units = unit_numbers(rec.number_or_name)
    if not units:
        return []
    words = building_words(rec.number_or_name)
    building = _building_numbers(rec.number_or_name)
    found = []
    for _, name, address in companies:
        if not unit_numbers(address) & units:
            continue
        # Same building: the company's address names it ("Thames House"), or has the same building
        # number ("566 Cable Street"). With neither to compare, the postcode and unit number decide.
        theirs = _building_numbers(address)
        if words & building_words(address) or (building and theirs and building & theirs):
            same_building = True
        else:
            same_building = not words and (not building or not theirs)
        if same_building:
            found.append(name)
    return found


def _in_building(rec: ResultRecord, candidates: list[ContactCandidate]) -> list[tuple[ContactCandidate, str]]:
    out = []
    for c in candidates:
        phone = normalize_phone(c.phone)
        if phone and "building number differs" not in score_candidate(rec, c)[1]:
            out.append((c, phone))
    return out


def _set_business(rec: ResultRecord, cand: ContactCandidate, phone: str) -> None:
    rec.business_name, rec.phone = cand.name, phone
    rec.business_address = ", ".join(p for p in (cand.address, cand.postcode) if p)
    rec.contact_source, rec.contact_source_url = cand.source, cand.source_url
    rec.contact_address = ", ".join(p for p in (cand.name, cand.address, cand.postcode) if p)


def is_business_centre(rec: ResultRecord) -> bool:
    guessed = any("also at this address" in r for r in rec.match_reasons)
    return guessed and rec.building_businesses >= BUSINESS_CENTRE_MIN


def resolve_unit(rec: ResultRecord, candidates: list[ContactCandidate], companies: list[tuple[str, str, str]]) -> None:
    """Replace the building-level guess in a business centre with the unit's real (or unknown) occupier."""
    if not is_business_centre(rec):
        return
    in_building = _in_building(rec, candidates)
    unit = rec.number_or_name
    names = occupiers(rec, companies)
    if names:
        rec.unit_company = names[0]
        for cand, phone in in_building:
            key = name_key(cand.name)
            hit = next((n for n in names if fuzz.token_set_ratio(key, name_key(n)) >= NAME_MATCH), None)
            if hit:
                _set_business(rec, cand, phone)
                rec.unit_company = hit
                rec.match_note = f"Unit confirmed: Companies House lists {hit} at {unit}"
                return
        # Registered at this unit, but no phone listed for them: name them, call via the building.
        rec.business_name, rec.business_address = names[0].title(), ""
        rec.phone, rec.phone_label = _reception(rec, in_building)
        rec.match_note = (f"Unit confirmed: Companies House lists {names[0]} at {unit}; "
                          f"no phone listed for them" + (" - call the building and ask for them" if rec.phone else ""))
        return
    rec.occupier_unknown = True
    rec.business_name, rec.business_address = "", ""
    rec.phone, rec.phone_label = _reception(rec, in_building)
    rec.match_note = (f"Business centre: {rec.building_businesses} businesses listed here, the occupier of {unit} "
                      f"is not known." + (" Call the building and ask who is in this unit." if rec.phone else ""))


def _reception(rec: ResultRecord, in_building: list[tuple[ContactCandidate, str]]) -> tuple[str, str]:
    """The building's own number (e.g. "Tower Bridge Business Centre"), if the listings have one."""
    for cand, phone in in_building:
        if _RECEPTION.search(cand.name):
            return phone, f"Building reception: {cand.name}"
    return "", ""
