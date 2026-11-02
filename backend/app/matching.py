"""Explainable scoring of contact candidates against a VO premises.

A premises is identified by postcode + house number/unit + street. Business names are mostly
absent from VO data, so name similarity can only ever add a small bonus: a name match alone
never produces a verified result.
"""
from typing import NamedTuple

import phonenumbers
from rapidfuzz import fuzz

from app.contacts.base import ContactCandidate
from app.models import ContactStatus, PropertyRecord
from app.normalize import house_numbers, name_key, normalize_postcode, postcode_in_text, street_key

VERIFIED_MIN = 90
PROBABLE_MIN = 70
REVIEW_MIN = 50
AMBIGUITY_GAP = 10  # a runner-up this close to the best (with a different phone) means "needs review"

# Rateable but not a business you can phone: never attach a number to these.
NON_BUSINESS = (
    "ADVERTISING", "ADVERT ", "CAR PARK", "PARKING SPACE", "COMMUNICATION STATION", "TELECOMMUNICATION",
    "ELECTRICITY", "SUBSTATION", "SUB STATION", "MOORING", "BEACH HUT", "CASH MACHINE", "ATM SITE",
    "WIND TURBINE", "SOLAR", "SEWAGE", "PUMPING STATION", "PUBLIC CONVENIENCE", "TELEPHONE KIOSK",
    "ELECTRIC VEHICLE", "BUS SHELTER", "LAND USED FOR STORAGE",
)

# Broad "kind" of premises, from the VO description and from the source's category.
# Checked in order; first match wins.
_VO_KINDS = (
    ("office", ("OFFICE",)),
    ("food", ("RESTAURANT", "CAFE", "PUBLIC HOUSE", "WINE BAR", "TAKEAWAY", "HOT FOOD", "CLUB", "HOTEL")),
    ("beauty", ("HAIRDRESSING", "BEAUTY", "SALON")),
    ("industrial", ("WAREHOUSE", "WORKSHOP", "FACTORY", "WORKS", "INDUSTRIAL", "STORE AND PREMISES", "DEPOT")),
    ("retail", ("SHOP", "SHOWROOM", "KIOSK", "SUPERSTORE", "RETAIL", "BANK", "SUPERMARKET", "PETROL")),
)
_CAND_KINDS = (
    ("food", ("restaurant", "eatery", "cafe", "coffee", "bar", "pub", "bakery", "dessert", "ice_cream", "food_court",
              "takeaway", "hotel", "lodging")),
    ("beauty", ("beauty", "hair", "barber", "nail", "salon", "spa")),
    ("retail", ("store", "shop", "shopping", "retail", "pharmacy", "auto_dealer", "supermarket", "florist",
                "market", "travel_service")),
    ("industrial", ("manufacturer", "supplier", "distributor", "automotive_service", "storage", "transportation",
                    "shipping", "construction", "wholesale")),
    ("office", ("professional_service", "b2b", "corporate", "office", "real_estate", "financial_service",
                "design_service", "technical_service", "media_service", "insurance", "legal", "accounting",
                "employment", "housing_or_property", "home_service", "social_or_community", "civic_organization")),
)
# (VO kind, candidate kind) pairs that contradict each other, e.g. an office business at a shop premises.
_CONFLICTS = {
    ("retail", "office"), ("retail", "industrial"),
    ("food", "office"), ("food", "industrial"), ("food", "beauty"),
    ("beauty", "office"), ("beauty", "industrial"), ("beauty", "food"),
    ("office", "food"), ("office", "retail"), ("office", "beauty"),
    ("industrial", "food"), ("industrial", "beauty"),
}
# Kinds that fit each other; a shop unit is often trading as a takeaway or a salon.
_FITS = {
    ("retail", "retail"), ("retail", "food"), ("retail", "beauty"),
    ("food", "food"), ("beauty", "beauty"), ("office", "office"),
    ("industrial", "industrial"), ("industrial", "retail"),
}


def _kind(text: str, table) -> str | None:
    for kind, words in table:
        if any(w in text for w in words):
            return kind
    return None


def is_non_business(rec: PropertyRecord) -> bool:
    text = f"{rec.description} {rec.number_or_name}".upper()
    return any(k in text for k in NON_BUSINESS)


def normalize_phone(raw: str) -> str | None:
    """Return a validated UK number with country code ("+44 24 7655 9900") or None."""
    if not raw:
        return None
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not raw.strip().startswith(("+", "0")) and digits.startswith("44") and len(digits) == 12:
        raw = "+" + digits  # some sources drop the "+": "442073752600"
    try:
        num = phonenumbers.parse(raw, "GB")
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(num) or num.country_code != 44:
        return None
    return phonenumbers.format_number(num, phonenumbers.PhoneNumberFormat.INTERNATIONAL)


def score_candidate(rec: PropertyRecord, cand: ContactCandidate) -> tuple[int, list[str]]:
    score, why = 0, []
    cand_pc = normalize_postcode(cand.postcode) or postcode_in_text(cand.address)
    if cand_pc and cand_pc == rec.postcode:
        score += 50
        why.append("postcode matches")
    elif cand_pc and rec.postcode and cand_pc.split()[0] == rec.postcode.split()[0]:
        score += 10
        why.append(f"same postcode district only ({cand_pc})")
    elif cand_pc:
        why.append(f"different postcode ({cand_pc})")
    elif cand.distance_m is not None:
        score += 35  # location evidence, but weaker than an explicit postcode
        why.append(f"no postcode on source; {cand.distance_m:.0f} m from postcode centre")

    rec_nums = house_numbers(rec.number_or_name)
    cand_nums = house_numbers(cand.address.split(",")[0]) or house_numbers(cand.address)
    if rec_nums and cand_nums:
        if rec_nums & cand_nums:
            score += 30
            why.append("building number matches")
        else:
            score -= 25
            why.append("building number differs")
    elif not rec_nums:
        building = name_key(rec.number_or_name)
        if building and fuzz.partial_ratio(building, name_key(f"{cand.name} {cand.address}")) >= 85:
            score += 20
            why.append("building name matches")
        else:
            why.append("no building number on VO record")

    rs, cs = street_key(rec.street), street_key(cand.address)
    if rs and rs in cs:
        score += 15
        why.append("street matches")
    elif rs and fuzz.partial_ratio(rs, cs) >= 85:
        score += 10
        why.append("street similar")
    elif rs:
        score -= 10
        why.append("street differs")

    if rec.firm_name and fuzz.token_set_ratio(name_key(rec.firm_name), name_key(cand.name)) >= 85:
        score += 10
        why.append("VO firm name matches")

    vo_kind = _kind(rec.description.upper(), _VO_KINDS)
    cand_kind = _kind(" ".join(cand.categories).lower(), _CAND_KINDS)
    if vo_kind and cand_kind:
        if (vo_kind, cand_kind) in _FITS:
            score += 10
            why.append(f"business type fits VO description ({cand.categories[0]})")
        elif (vo_kind, cand_kind) in _CONFLICTS:
            score -= 15
            why.append(f"business type unlikely for a {rec.description.lower()} ({cand.categories[0]})")

    if cand.closed:
        score -= 40
        why.append("marked closed by source")
    if cand.source_confidence is not None and cand.source_confidence < 0.6:
        score -= 10
        why.append(f"low source confidence ({cand.source_confidence:.2f})")
    return score, why


MAX_OTHERS = 40  # business centres: keep every number in the building, none is lost as a lead


class Match(NamedTuple):
    status: ContactStatus
    candidate: ContactCandidate | None
    phone: str
    score: int | None
    reasons: list[str]
    others: list[str]  # "Name: phone" for other plausible businesses at the address
    note: str = ""  # one short, plain-English line for the dashboard / Excel
    building_businesses: int = 0  # businesses listed in the same building (incl. the pick)


def _note(why: list[str], phone: str, others: list[str]) -> str:
    if not phone:
        return "Business found, but no phone number listed"
    if "building number matches" in why:
        base = "Same building number and postcode"
    elif "building name matches" in why:
        base = "Same building name and postcode"
    elif "postcode matches" in why:
        base = "Same postcode"
    else:
        base = "Near this postcode"
    return base + (f"; {len(others)} other business(es) at this address" if others else "")


def choose(rec: PropertyRecord, candidates: list[ContactCandidate]) -> Match:
    """Pick the business (and phone) most likely to occupy the premises.

    A phone number is always returned when any plausible candidate has one; uncertain picks are
    labelled "needs review" and the other businesses at the address are listed as alternatives.
    """
    if is_non_business(rec):
        return Match(ContactStatus.NO_CONTACT, None, "", None, [f"not a trading business ({rec.description})"], [],
                     "Not a business (advertising right, car park, mast, etc.)")
    scored = []
    for c in candidates:
        s, why = score_candidate(rec, c)
        scored.append((s, why, c, normalize_phone(c.phone) or ""))
    # Highest score first; on equal scores prefer a listing that has a phone, then the more confident source.
    scored.sort(key=lambda t: (t[0], bool(t[3]), t[2].source_confidence or 0), reverse=True)
    if not scored or scored[0][0] < REVIEW_MIN:
        top = scored[0][0] if scored else None
        return Match(ContactStatus.NO_CONTACT, None, "", top, ["no business listed at this address"], [],
                     "No business found at this address")

    plausible = [t for t in scored if t[0] >= REVIEW_MIN]
    top_score = plausible[0][0]
    close = [t for t in plausible if t[0] >= top_score - AMBIGUITY_GAP]
    # Within the close group, a listing with a phone beats an equally likely one without.
    best_t = next((t for t in close if t[3]), None) or next((t for t in plausible if t[3]), None) or plausible[0]
    score, why, best, phone = best_t
    why = list(why)

    others, seen = [], {phone} if phone else set()
    for _, w, c, p in plausible:
        # same building only: a business at a different number on the same postcode is not an alternative
        if c is not best and p and p not in seen and "building number differs" not in w:
            others.append(f"{c.name}: {p}")
            seen.add(p)
    # Every business listed in the same building, with or without a phone (including the pick).
    building_businesses = len({name_key(c.name) for _, w, c, _ in plausible if "building number differs" not in w})
    others = others[:MAX_OTHERS]
    rivals = [c for s, _, c, p in close if c is not best and name_key(c.name) != name_key(best.name)
              and p != phone]
    if rivals:
        why.append(f"{len(rivals)} other business(es) also at this address — confirm who answers")

    if not phone:
        return Match(ContactStatus.NO_CONTACT, best, "", score, [*why, "no phone listed for any business here"], [], _note(why, "", []))
    if best_t not in close:
        why.append("highest-scoring business has no phone; using the next best listing")
        return Match(ContactStatus.REVIEW, best, phone, score, why, others, _note(why, phone, others), building_businesses)
    if rivals or score < PROBABLE_MIN:
        return Match(ContactStatus.REVIEW, best, phone, score, why, others, _note(why, phone, others), building_businesses)
    status = ContactStatus.VERIFIED if score >= VERIFIED_MIN else ContactStatus.PROBABLE
    return Match(status, best, phone, score, why, others, _note(why, phone, others), building_businesses)
