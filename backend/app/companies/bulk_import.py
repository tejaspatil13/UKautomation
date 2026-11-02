"""Import the Companies House free bulk data into a local SQLite DB (no per-business web requests).

Two products from https://download.companieshouse.gov.uk/ :
- Basic Company Data (monthly, ~470 MB in 7 parts): every live company's name, number, registered
  office, status, type, incorporation date and nature of business (SIC).
- People with significant control snapshot (daily, ~2.5 GB in ~33 parts): who owns / controls
  each company. Only active (not ceased) people and entities are kept.

Each part is downloaded, read straight from the zip, and deleted before the next one, so the
extra disk needed is one part (~80 MB) plus the new database. The new database is built beside
the old one and swapped in at the end. Contains public sector information licensed under the
Open Government Licence v3.0.

Usage:
    python -m app.companies.bulk_import
"""
import csv
import io
import json
import logging
import re
import sqlite3
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.config import get_settings
from app import progress
from app.dbswap import swap_in
from app.normalize import clean_ws, normalize_postcode

log = logging.getLogger(__name__)

BASE = "https://download.companieshouse.gov.uk"
SCHEMA = """
CREATE TABLE companies (
    number TEXT NOT NULL, name TEXT NOT NULL, postcode TEXT NOT NULL,
    address TEXT, status TEXT, category TEXT, incorporated TEXT, sic TEXT
);
CREATE TABLE psc (
    company_number TEXT NOT NULL, name TEXT NOT NULL, corporate INTEGER NOT NULL,
    since TEXT, control TEXT, nationality TEXT
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""
PSC_KINDS = {
    "individual-person-with-significant-control": 0,
    "corporate-entity-person-with-significant-control": 1,
    "legal-person-person-with-significant-control": 1,
}


def _links(page: str, pattern: str) -> list[str]:
    r = httpx.get(f"{BASE}/{page}", timeout=30)
    r.raise_for_status()
    return sorted(set(re.findall(pattern, r.text)))


def latest_files() -> tuple[str, list[str], list[str]]:
    """(release date of Basic Company Data, its part files, the newest PSC snapshot's part files)."""
    basic = _links("en_output.html", r'href="(BasicCompanyData-(\d{4}-\d{2}-\d{2})-part\d+_\d+\.zip)"')
    psc = _links("en_pscdata.html", r'href="(psc-snapshot-(\d{4}-\d{2}-\d{2})_\d+of\d+\.zip)"')
    if not basic or not psc:
        raise RuntimeError("Could not find the Companies House bulk files")
    release = max(d for _, d in basic)
    psc_date = max(d for _, d in psc)
    return release, [f for f, d in basic if d == release], [f for f, d in psc if d == psc_date]


def latest_release() -> str:
    """The Basic Company Data release date (monthly). The PSC snapshot is refreshed along with it."""
    return latest_files()[0]


def _download(name: str, dest: Path, step: int | None = None, steps: int | None = None) -> None:
    for attempt in range(4):  # slow office connections drop now and then: retry the part
        try:
            with httpx.stream("GET", f"{BASE}/{name}", timeout=httpx.Timeout(60, read=300)) as r:
                r.raise_for_status()
                total, done = int(r.headers.get("content-length") or 0) or None, 0
                progress.report("Downloading", 0, total, "bytes", step, steps, force=True)
                with open(dest, "wb") as f:
                    for chunk in r.iter_bytes(1 << 20):
                        f.write(chunk)
                        done += len(chunk)
                        progress.report("Downloading", done, total, "bytes", step, steps)
            return
        except httpx.HTTPError as e:
            log.warning("download of %s failed (%s), retrying", name, e)
            progress.report(f"Connection dropped; retrying in {10 * (attempt + 1)} s", None, None, "", step, steps,
                            force=True)
            time.sleep(10 * (attempt + 1))
    raise RuntimeError(f"Could not download {name}")


def _uk_date(value: str) -> str:
    """"11/09/2012" -> "2012-09-11" (ISO, so it sorts and formats cleanly)."""
    try:
        return datetime.strptime(value.strip(), "%d/%m/%Y").date().isoformat()
    except ValueError:
        return ""


def read_companies(zip_path: Path):
    with zipfile.ZipFile(zip_path) as zf:
        with zf.open(zf.namelist()[0]) as raw:
            reader = csv.reader(io.TextIOWrapper(raw, encoding="utf-8", errors="replace", newline=""))
            header = [h.strip() for h in next(reader)]
            col = {h: i for i, h in enumerate(header)}
            for row in reader:
                if len(row) < len(header):
                    continue
                postcode = normalize_postcode(row[col["RegAddress.PostCode"]])
                if not postcode:
                    continue  # matching is by postcode
                address = ", ".join(p for p in (clean_ws(row[col[k]]) for k in (
                    "RegAddress.CareOf", "RegAddress.POBox", "RegAddress.AddressLine1", "RegAddress.AddressLine2",
                    "RegAddress.PostTown", "RegAddress.County")) if p)
                sic = "; ".join(s for s in (row[col[f"SICCode.SicText_{i}"]].strip() for i in range(1, 5))
                                if s and s != "None Supplied")
                name = clean_ws(row[col["CompanyName"]])
                yield (row[col["CompanyNumber"]].strip(), name, postcode,
                       f"{address}, {postcode}" if address else postcode, row[col["CompanyStatus"]].strip(),
                       row[col["CompanyCategory"]].strip(), _uk_date(row[col["IncorporationDate"]]), sic)


def read_psc(zip_path: Path, wanted: set[str]):
    with zipfile.ZipFile(zip_path) as zf:
        with zf.open(zf.namelist()[0]) as raw:
            for line in io.TextIOWrapper(raw, encoding="utf-8", errors="replace"):
                if '"ceased_on"' in line or '"name"' not in line:
                    continue  # ceased, or a statement / exemption / totals line
                rec = json.loads(line)
                number, data = rec.get("company_number"), rec.get("data", {})
                if number not in wanted or data.get("kind") not in PSC_KINDS:
                    continue
                yield (number, clean_ws(data["name"]), PSC_KINDS[data["kind"]], data.get("notified_on", ""),
                       ",".join(data.get("natures_of_control", [])), data.get("nationality", ""))


def import_companies(db_path: Path) -> tuple[int, int]:
    started = time.monotonic()
    release, basic_parts, psc_parts = latest_files()
    tmp = db_path.with_suffix(".importing")
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    con.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;" + SCHEMA)
    work = db_path.with_name("companies-part.zip.download")
    try:
        steps = len(basic_parts) + len(psc_parts)
        for i, name in enumerate(basic_parts, 1):
            _download(name, work, i, steps)
            progress.report("Reading companies", None, None, "", i, steps, force=True)
            con.executemany("INSERT INTO companies VALUES (?,?,?,?,?,?,?,?)", read_companies(work))
            con.commit()
            log.info("%s imported (%.0fs)", name, time.monotonic() - started)
        wanted = {r[0] for r in con.execute("SELECT number FROM companies")}
        for i, name in enumerate(psc_parts, len(basic_parts) + 1):
            _download(name, work, i, steps)
            progress.report("Reading owners", None, None, "", i, steps, force=True)
            con.executemany("INSERT INTO psc VALUES (?,?,?,?,?,?)", read_psc(work, wanted))
            con.commit()
            log.info("%s imported (%.0fs)", name, time.monotonic() - started)
    finally:
        work.unlink(missing_ok=True)
    progress.report("Building the search index", force=True)
    # Indexes are built once at the end: compact, and much faster than growing them row by row.
    con.execute("CREATE INDEX ix_companies_postcode ON companies(postcode)")
    con.execute("CREATE INDEX ix_psc_company ON psc(company_number)")
    companies = con.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
    people = con.execute("SELECT COUNT(*) FROM psc").fetchone()[0]
    meta = {
        "source": "Companies House bulk data (Basic Company Data + PSC snapshot)",
        "release": release,
        "psc_snapshot": re.search(r"\d{4}-\d{2}-\d{2}", psc_parts[0]).group(0),
        "licence": "Open Government Licence v3.0",
        "imported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "row_count": str(companies),
        "psc_count": str(people),
    }
    con.executemany("INSERT INTO meta VALUES (?, ?)", meta.items())
    con.commit()
    con.close()
    swap_in(tmp, db_path)
    return companies, people


def main() -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(message)s")
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    companies, people = import_companies(settings.companies_db_path)
    log.info("Companies House import complete: %s companies, %s owners -> %s",
             f"{companies:,}", f"{people:,}", settings.companies_db_path)


if __name__ == "__main__":
    main()
