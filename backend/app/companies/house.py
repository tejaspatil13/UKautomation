"""Companies House client.

With an API key (free: https://developer.company-information.service.gov.uk/) the official JSON
API is used — this is what Companies House asks automated tools to use. Without one, the public
"Find and update company information" website is read; Companies House blocks that (HTTP 403)
after a few hundred automated requests, so the website mode is only good for small batches.
Requests are paced under the published API limit (600 per 5 minutes); a 429 waits and retries,
and a 403 stops the run straight away (retrying would only prolong the block).

A company only counts as a match when its registered office postcode equals the premises'
postcode (or the postcode the business itself lists) and its name is similar to the business name.
"""
import asyncio
import html
import json
import re
import time
from datetime import date, datetime, timezone
from functools import cache
from pathlib import Path

import httpx
from pydantic import BaseModel, computed_field, field_validator
from rapidfuzz import fuzz

from app.normalize import clean_ws, name_key, normalize_postcode, postcode_in_text

WEB = "https://find-and-update.company-information.service.gov.uk"
API = "https://api.company-information.service.gov.uk"
MIN_NAME_SIMILARITY = 60
_TAGS = re.compile(r"<[^>]+>")


class CompaniesHouseError(Exception):
    pass


class CompaniesHouseBlocked(CompaniesHouseError):
    """403: Companies House refuses requests from this connection (website mode) or the key is wrong (API)."""


@cache
def labels() -> dict[str, dict[str, str]]:
    """Companies House's own wording for codes (SIC, company type/status, officer role, PSC control).

    From github.com/companieshouse/api-enumerations (constants.yml, psc_descriptions.yml).
    """
    return json.loads((Path(__file__).parent / "labels.json").read_text(encoding="utf-8"))


def label(kind: str, code: str) -> str:
    return labels()[kind].get(code, code.replace("-", " ").capitalize() if code else "")


def nice_date(value: str | None) -> str:
    """"2003-01-29" or "29 January 2003" -> "29 Jan 2003" (one style everywhere)."""
    if not value:
        return ""
    value = clean_ws(value)
    for parse in (date.fromisoformat, lambda v: datetime.strptime(v, "%d %B %Y").date()):
        try:
            d = parse(value)
            return f"{d.day} {d:%b %Y}"
        except ValueError:
            continue
    return value


_CORPORATE = re.compile(r"\b(LTD|LIMITED|LLP|PLC|HOLDINGS?|GROUP|TRUSTEES?|NOMINEES?|SERVICES|SECRETARIES|"
                        r"PARTNERSHIP|COMPANY|CORPORATION|INC|L\.?L\.?C)\b", re.I)


def looks_corporate(name: str) -> bool:
    """A company (not a person) holding a role, e.g. "The Clove Club Holdings Ltd"."""
    return bool(_CORPORATE.search(name))


class Person(BaseModel):
    name: str
    role: str = ""  # "Director", "LLP Member", "Owner" ...
    since: str = ""  # appointed / became an owner
    control: list[str] = []  # owners: "Ownership of shares – 75% or more", ...
    occupation: str = ""
    nationality: str = ""
    corporate: bool = False  # a company, not a person

    @property
    def summary(self) -> str:
        """"Timothy Godsmark (Director since 29 Jan 2003; Architect)"."""
        bits = [f"{self.role} since {self.since}" if self.role and self.since else self.role or self.since]
        bits += [self.occupation] + self.control
        bits = [b for b in bits if b]
        return f"{self.name} ({'; '.join(bits)})" if bits else self.name


class CompanyHit(BaseModel):
    number: str
    name: str
    active: bool
    address: str
    incorporated: str = ""

    @property
    def postcode(self) -> str:
        return normalize_postcode(postcode_in_text(self.address)) or ""


class CompanyMatch(BaseModel):
    company_name: str
    company_number: str
    registered_address: str
    incorporated: str = ""
    status: str = ""
    company_type: str = ""
    nature_of_business: list[str] = []
    owners: list[Person] = []  # people / companies with significant control (active)
    directors: list[Person] = []  # every active officer: directors, members, partners, secretaries
    details_checked: bool = False  # False for matches saved before company details were collected
    checked_at: datetime | None = None

    @field_validator("owners", "directors", mode="before")
    @classmethod
    def _names_to_people(cls, v):
        # Matches saved before company details were collected hold plain names.
        return [{"name": p} if isinstance(p, str) else p for p in v or []]

    @computed_field
    @property
    def url(self) -> str:
        return f"{WEB}/company/{self.company_number}"

    @computed_field
    @property
    def owner_names(self) -> str:
        """Who to ask for: the active officers who are people (directors, partners ...), never a
        company name; failing that the individual owners, failing that whoever is listed."""
        def people(xs: list[Person]) -> list[str]:
            return [p.name for p in xs if not p.corporate and not looks_corporate(p.name)]
        names = people(self.directors) or people(self.owners) or [p.name for p in self.directors + self.owners]
        return ", ".join(dict.fromkeys(names))

    @property
    def has_details(self) -> bool:
        return self.details_checked


def _text(fragment: str) -> str:
    return clean_ws(html.unescape(_TAGS.sub(" ", fragment)))


_TITLES = {"MR", "MRS", "MS", "MISS", "MX", "DR", "SIR", "DAME", "LORD", "LADY", "PROF", "PROFESSOR", "REV"}


def _person_name(name: str) -> str:
    """"GODSMARK, Timothy John" -> "Timothy John Godsmark"; "LO FIEGO, Giancarlo, Mr." -> "Giancarlo Lo Fiego".

    "Mr Timothy Godsmark" (no comma) stays as it is.
    """
    if "," in name:
        last, *rest = (p.strip() for p in name.split(","))
        first = " ".join(w for w in " ".join(rest).split() if w.strip(".").upper() not in _TITLES)
        name = f"{first} {last.title()}"
    return clean_ws(name)


def search_query(business_name: str) -> str:
    """Drop branch suffixes like "Costa Coffee - Coventry" / "Boots | Trinity St"."""
    return clean_ws(re.split(r"\s[-–|]\s", business_name, maxsplit=1)[0])


# ---------- website parsing ----------
def _field(block: str, field_id: str) -> str:
    m = re.search(rf'id="{field_id}"[^>]*>(.*?)</(?:dd|span|p)>', block, re.S)
    return _text(m.group(1)) if m else ""


def _appointments(page: str) -> list[tuple[str, str]]:
    return re.findall(r'<div class="appointment-(\d+)">(.*?)(?=<div class="appointment-\d+">|$)', page, re.S)


def parse_search(page: str) -> list[CompanyHit]:
    hits = []
    for block in re.findall(r'<li class="type-company">(.*?)</li>', page, re.S):
        link = re.search(r'href="/company/([A-Z0-9]+)"[^>]*>(.*?)</a>', block, re.S)
        if not link:
            continue  # the page's own result template
        paras = re.findall(r"<p[^>]*>(.*?)</p>", block, re.S)
        meta = next((_text(p) for p in paras if " - " in _text(p) and link.group(1) in _text(p)), "")
        address = _text(paras[-1]) if paras else ""
        incorporated = re.search(r"Incorporated on (.+)$", meta)
        hits.append(CompanyHit(
            number=link.group(1), name=_text(link.group(2)), active="Dissolved" not in meta, address=address,
            incorporated=incorporated.group(1) if incorporated else "",
        ))
    return hits


def parse_profile(page: str) -> dict:
    """Status and nature of business (SIC) from a company's overview page."""
    sic = [_text(s) for s in re.findall(r'<span id="sic\d+">(.*?)</span>', page, re.S)]
    return {"status": _field(page, "company-status"), "nature_of_business": [s for s in sic if s]}


def parse_officers(page: str) -> list[Person]:
    """Every active officer (director, secretary, LLP member ...) from a company's officers page.

    Resigned officers are left out.
    """
    people = []
    for i, block in _appointments(page):
        name = re.search(rf'id="officer-name-{i}">(.*?)</span>', block, re.S)
        status = re.search(rf'id="officer-status-tag-{i}"[^>]*>(.*?)<', block, re.S)
        role = _field(block, f"officer-role-{i}")
        if name and status and _text(status.group(1)) == "Active":
            full = _text(name.group(1))
            people.append(Person(
                name=full if looks_corporate(full) else _person_name(full), role=role or "Director",
                corporate=looks_corporate(full),
                since=nice_date(_field(block, f"officer-appointed-on-{i}")), occupation=_field(block, f"officer-occupation-{i}"),
                nationality=_field(block, f"officer-nationality-{i}"),
            ))
    return people


def parse_psc(page: str) -> list[Person]:
    people = []
    for i, block in _appointments(page):
        name = re.search(rf'id="psc-name-{i}">(.*?)</span>\s*</span>', block, re.S) or \
            re.search(rf'id="psc-name-{i}">(.*?)</span>', block, re.S)
        status = re.search(rf'id="psc-status-tag-{i}"[^>]*>(.*?)<', block, re.S)
        if name and (not status or _text(status.group(1)) == "Active"):
            control = re.findall(rf'id="psc-noc-{i}-([a-z0-9-]+)"', block)
            full = _text(name.group(1))
            people.append(Person(
                name=_person_name(full), role="Owner", corporate=looks_corporate(full),
                since=nice_date(_field(block, f"psc-notified-on-{i}")), nationality=_field(block, f"psc-nationality-{i}"),
                control=[label("psc_control", c) for c in control],
            ))
    return people


# ---------- API parsing ----------
def api_officers(items: list[dict]) -> list[Person]:
    """Every active officer; resigned ones are left out."""
    people = []
    for i in items:
        if not i.get("name") or i.get("resigned_on"):
            continue
        corporate = i.get("officer_role", "").startswith("corporate") or looks_corporate(i["name"])
        people.append(Person(
            name=i["name"] if corporate else _person_name(i["name"]), role=label("officer_role", i.get("officer_role", "")),
            since=nice_date(i.get("appointed_on")), occupation=(i.get("occupation") or "").title(),
            nationality=i.get("nationality") or "", corporate=corporate))
    return people


def api_psc(items: list[dict]) -> list[Person]:
    return [
        Person(name=_person_name(i["name"]), role="Owner", since=nice_date(i.get("notified_on")),
               nationality=i.get("nationality") or "", corporate="individual" not in i.get("kind", "individual"),
               control=[label("psc_control", c) for c in i.get("natures_of_control", [])])
        for i in items if i.get("name") and not i.get("ceased_on")
    ]


class CompaniesHouse:
    def __init__(self, api_key: str = "", min_interval: float = 0.6, client: httpx.AsyncClient | None = None):
        self.api_key = api_key
        self.min_interval = min_interval
        self._client = client or httpx.AsyncClient(
            timeout=20, follow_redirects=True,
            headers={"User-Agent": "Copilot business lookup (office tool)"},
            auth=(api_key, "") if api_key else None,
        )
        self._pace = asyncio.Lock()
        self._last = 0.0

    @property
    def mode(self) -> str:
        return "api" if self.api_key else "website"

    async def _get(self, url: str, params: dict | None = None) -> httpx.Response | None:
        """GET with pacing and 429 back-off. 404 -> None (e.g. no PSC register)."""
        for attempt in range(4):
            async with self._pace:
                wait = self._last + self.min_interval - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last = time.monotonic()
                try:
                    r = await self._client.get(url, params=params)
                except httpx.HTTPError as e:
                    if attempt == 3:
                        raise CompaniesHouseError(f"Companies House could not be reached ({type(e).__name__})") from e
                    continue
            if r.status_code == 429:
                await asyncio.sleep(min(float(r.headers.get("retry-after", 60)), 300))
                continue
            if r.status_code == 404:
                return None
            if r.status_code == 401 or r.status_code == 403:
                raise CompaniesHouseBlocked(str(r.status_code))
            if r.status_code >= 400:
                raise CompaniesHouseError(f"Companies House returned an error ({r.status_code})")
            return r
        raise CompaniesHouseError("Companies House is limiting requests; try again later")

    async def search(self, query: str) -> list[CompanyHit]:
        if self.api_key:
            r = await self._get(f"{API}/search/companies", {"q": query, "items_per_page": 20})
            items = r.json().get("items", []) if r else []
            return [CompanyHit(
                number=i.get("company_number", ""), name=i.get("title", ""),
                active=i.get("company_status") not in ("dissolved", "converted-closed"),
                address=i.get("address_snippet", ""), incorporated=nice_date(i.get("date_of_creation")),
            ) for i in items if i.get("company_number")]
        r = await self._get(f"{WEB}/search/companies", {"q": query})
        return parse_search(r.text) if r else []

    async def officers(self, number: str) -> list[Person]:
        """Active officers of one company (one request)."""
        if self.api_key:
            r = await self._get(f"{API}/company/{number}/officers", {"items_per_page": 100})
            return api_officers(r.json().get("items", []) if r else [])
        r = await self._get(f"{WEB}/company/{number}/officers")
        return parse_officers(r.text) if r else []

    async def details(self, hit: CompanyHit) -> CompanyMatch:
        """Everything worth showing about a matched company: profile, owners and directors."""
        n = hit.number
        if self.api_key:
            profile = await self._get(f"{API}/company/{n}")
            psc = await self._get(f"{API}/company/{n}/persons-with-significant-control")
            p = profile.json() if profile else {}
            info = {
                "status": label("company_status", p.get("company_status", "")),
                "company_type": label("company_type", p.get("type", "")),
                "incorporated": nice_date(p.get("date_of_creation")) or hit.incorporated,
                "nature_of_business": [f"{c} - {label('sic_descriptions', c)}" for c in p.get("sic_codes", [])],
            }
            owners = api_psc(psc.json().get("items", []) if psc else [])
        else:
            profile = await self._get(f"{WEB}/company/{n}")
            psc = await self._get(f"{WEB}/company/{n}/persons-with-significant-control")
            info = parse_profile(profile.text) if profile else {}
            info["incorporated"] = hit.incorporated
            owners = parse_psc(psc.text) if psc else []
        directors = await self.officers(n)
        return CompanyMatch(
            company_name=hit.name, company_number=n, registered_address=hit.address,
            owners=owners, directors=directors, details_checked=True, checked_at=datetime.now(timezone.utc), **info,
        )

    async def find(self, business_name: str, postcodes: set[str]) -> CompanyMatch | None:
        """The active registered company with a similar name whose registered postcode matches."""
        query = search_query(business_name)
        if not query or not postcodes:
            return None
        key = name_key(query)
        best, best_score = None, 0
        for hit in await self.search(query):
            if not hit.active or hit.postcode not in postcodes:
                continue
            score = fuzz.token_set_ratio(key, name_key(hit.name))
            if score >= MIN_NAME_SIMILARITY and score > best_score:
                best, best_score = hit, score
        return await self.details(best) if best else None

    async def refresh(self, match: CompanyMatch) -> CompanyMatch:
        """Re-read a previously matched company (adds details to matches saved before they were collected)."""
        hit = CompanyHit(number=match.company_number, name=match.company_name, active=True,
                         address=match.registered_address, incorporated=match.incorporated)
        return await self.details(hit)

    async def aclose(self) -> None:
        await self._client.aclose()
