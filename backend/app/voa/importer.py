"""Import the VO compiled rating list (bulk download) into a local SQLite database.

Source: https://voaratinglists.blob.core.windows.net/html/rlidata.htm
Format: asterisk-delimited "CSV", one list entry per line, field order per the VO
"Compiled Rating List and Summary Valuation Data Specification" (list entry items 1-28).

Usage:
    python -m app.voa.importer                 # download configured epoch file and import
    python -m app.voa.importer --zip path.zip  # import an already-downloaded zip
"""
import argparse
import csv
import io
import logging
import os
import re
import sqlite3
import time
import zipfile
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.config import get_settings
from app.dbswap import swap_in
from app.normalize import clean_ws, normalize_postcode

log = logging.getLogger(__name__)

# 0-based column positions in a list entry record (spec item number - 1).
F_BA_CODE, F_BA_REF, F_DESC_CODE, F_DESC_TEXT, F_UARN, F_FULL_ADDR, F_FIRM = 1, 3, 4, 5, 6, 7, 8
F_NUMBER, F_STREET, F_TOWN, F_DISTRICT, F_COUNTY, F_POSTCODE = 9, 10, 11, 12, 13, 14
F_EFFECTIVE, F_COMPOSITE, F_RV, F_ASSESSMENT_REF, F_ALTERED, F_SCAT = 15, 16, 17, 19, 20, 21
F_SUB3, F_SUB2, F_SUB1 = 22, 23, 24
MIN_FIELDS = 25

SCHEMA = """
CREATE TABLE properties (
    uarn TEXT PRIMARY KEY,
    ba_code TEXT, ba_ref TEXT,
    desc_code TEXT, desc_text TEXT,
    full_address TEXT, firm_name TEXT, number_or_name TEXT,
    sub_street_3 TEXT, sub_street_2 TEXT, sub_street_1 TEXT,
    street TEXT, town TEXT, postal_district TEXT, county TEXT,
    postcode TEXT, outward TEXT, sector TEXT,
    rv INTEGER NOT NULL,
    effective_date TEXT, composite TEXT, scat_code TEXT,
    assessment_ref TEXT, list_alteration_date TEXT
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""
INDEXES = """
CREATE INDEX ix_postcode ON properties(postcode);
CREATE INDEX ix_sector ON properties(sector);
CREATE INDEX ix_outward ON properties(outward);
CREATE INDEX ix_town ON properties(town);
CREATE INDEX ix_district ON properties(postal_district);
"""
COLUMNS = (
    "uarn", "ba_code", "ba_ref", "desc_code", "desc_text", "full_address", "firm_name", "number_or_name",
    "sub_street_3", "sub_street_2", "sub_street_1", "street", "town", "postal_district", "county",
    "postcode", "outward", "sector", "rv", "effective_date", "composite", "scat_code",
    "assessment_ref", "list_alteration_date",
)


def parse_record(fields: list[str]) -> tuple | None:
    """Convert one raw list-entry record into a DB row, or None for unusable/deleted entries."""
    if len(fields) < MIN_FIELDS:
        return None
    rv = fields[F_RV].strip()
    uarn = fields[F_UARN].strip()
    if not rv.isdigit() or not uarn:
        return None  # deleted entries carry a null RV
    f = [clean_ws(x) for x in fields]
    postcode = normalize_postcode(f[F_POSTCODE])
    outward, sector = (postcode.split(" ")[0], postcode[:-2]) if postcode else (None, None)
    return (
        uarn, f[F_BA_CODE], f[F_BA_REF], f[F_DESC_CODE], f[F_DESC_TEXT], f[F_FULL_ADDR], f[F_FIRM],
        f[F_NUMBER], f[F_SUB3], f[F_SUB2], f[F_SUB1], f[F_STREET], f[F_TOWN].upper(),
        f[F_DISTRICT].upper(), f[F_COUNTY], postcode or f[F_POSTCODE].upper(), outward, sector,
        int(rv), f[F_EFFECTIVE], f[F_COMPOSITE], f[F_SCAT], f[F_ASSESSMENT_REF], f[F_ALTERED],
    )


def iter_records(lines: Iterable[str]) -> Iterator[tuple]:
    for fields in csv.reader(lines, delimiter="*", quoting=csv.QUOTE_NONE):
        if row := parse_record(fields):
            yield row


def _open_list_entries(zip_path: Path) -> tuple[str, io.TextIOWrapper]:
    zf = zipfile.ZipFile(zip_path)
    names = [n for n in zf.namelist() if n.endswith(".csv") and "historic" not in n.lower()]
    if len(names) != 1:
        raise ValueError(f"Expected one current list-entries CSV in {zip_path.name}, found {names}")
    return names[0], io.TextIOWrapper(zf.open(names[0]), encoding="latin-1", newline="")


def import_zip(
    zip_path: Path, db_path: Path, source_url: str = "", list_year: int | None = None, version: str = "",
) -> int:
    """Build a fresh DB next to db_path and atomically swap it in. Returns rows imported."""
    tmp_path = db_path.with_suffix(".importing")
    tmp_path.unlink(missing_ok=True)
    name, text = _open_list_entries(zip_path)
    con = sqlite3.connect(tmp_path)
    try:
        con.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;" + SCHEMA)
        insert = f"INSERT OR REPLACE INTO properties ({','.join(COLUMNS)}) VALUES ({','.join('?' * len(COLUMNS))})"
        count, batch, started = 0, [], time.monotonic()
        for row in iter_records(text):
            batch.append(row)
            if len(batch) >= 50_000:
                con.executemany(insert, batch)
                count += len(batch)
                batch.clear()
                log.info("imported %s rows (%.0fs)", f"{count:,}", time.monotonic() - started)
        con.executemany(insert, batch)
        count += len(batch)
        con.executescript(INDEXES)
        meta = {
            "source_file": name,
            "source_url": source_url,
            "list_year": str(list_year or ""),
            "imported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "row_count": str(count),
            "remote_version": version,  # server ETag/Last-Modified, used to spot republished files
        }
        con.executemany("INSERT INTO meta VALUES (?, ?)", meta.items())
        con.commit()
    finally:
        con.close()
        text.close()
    swap_in(tmp_path, db_path)
    log.info("VO import complete: %s rows -> %s", f"{count:,}", db_path)
    return count


def remote_version(url: str) -> str:
    """The server's ETag (or Last-Modified) for a file: changes whenever the VO republishes it."""
    r = httpx.head(url, timeout=30, follow_redirects=True)
    r.raise_for_status()
    return r.headers.get("etag") or r.headers.get("last-modified") or ""


def download(url: str, dest: Path) -> Path:
    """Stream the VO zip to disk (≈95 MB). Resumable runs simply re-download."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(".part")
    with httpx.stream("GET", url, timeout=httpx.Timeout(60, read=300), follow_redirects=True) as r:
        r.raise_for_status()
        with part.open("wb") as fh:
            for chunk in r.iter_bytes(1 << 20):
                fh.write(chunk)
    os.replace(part, dest)
    return dest


INDEX_PAGE = "https://voaratinglists.blob.core.windows.net/html/rlidata.htm"


def latest_baseline_url(list_year: int, page_html: str | None = None) -> str:
    """Find the newest compiled-epoch baseline list-entries zip for a list year on the VO download page."""
    html = page_html if page_html is not None else httpx.get(INDEX_PAGE, timeout=30).text
    pattern = re.compile(
        rf'href="(https://[^"]*ndr-{list_year}-listentries-compiled-epoch-(\d+)-baseline-csv\.zip)"'
    )
    found = sorted(((int(epoch), url) for url, epoch in pattern.findall(html)), reverse=True)
    if not found:
        raise RuntimeError(f"No {list_year} list-entries baseline download found on {INDEX_PAGE}")
    return found[0][1]


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--zip", type=Path, help="Use an already-downloaded VO list-entries zip")
    parser.add_argument("--url", default=settings.voa_list_url or None,
                        help="Download URL (default: newest epoch listed on the VO download page)")
    args = parser.parse_args()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(message)s")

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    url = args.url or ("" if args.zip else latest_baseline_url(settings.voa_list_year))
    if args.zip:
        zip_path = args.zip
    else:
        zip_path = download(url, settings.data_dir / "downloads" / Path(url).name)
    version = "" if args.zip else remote_version(url)
    import_zip(zip_path, settings.voa_db_path, source_url=url, list_year=settings.voa_list_year, version=version)
    if not args.zip:
        zip_path.unlink(missing_ok=True)  # the SQLite copy is all we need; keeps the disk small


if __name__ == "__main__":
    main()
