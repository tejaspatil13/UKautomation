"""Import Overture Maps places (England & Wales) into a local, postcode-indexed SQLite DB.

Overture places are published as GeoParquet under CDLA-Permissive-2.0 (some sources Apache-2.0),
which permits copying the data into exports with attribution. https://docs.overturemaps.org/

Usage:
    python -m app.contacts.overture_import                       # latest release
    python -m app.contacts.overture_import --release 2026-10-21.0
"""
import argparse
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timezone

import duckdb
import httpx

from app.config import get_settings
from app import progress
from app.dbswap import swap_in
from app.normalize import normalize_postcode

log = logging.getLogger(__name__)

BUCKET = "https://overturemaps-us-west-2.s3.amazonaws.com"
# Bounding box generously covering England & Wales (Scotland/NI places are filtered by VO join anyway).
BBOX = dict(xmin=-6.5, xmax=1.8, ymin=49.8, ymax=55.9)
MIN_CONFIDENCE = 0.2  # below this Overture records are mostly mislocated or junk

SCHEMA = """
CREATE TABLE places (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, category TEXT, freeform TEXT, locality TEXT,
    postcode TEXT, phone TEXT, website TEXT, confidence REAL, operating_status TEXT,
    lat REAL, lon REAL, sources TEXT
);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
"""


def latest_release() -> str:
    r = httpx.get(f"{BUCKET}/?list-type=2&prefix=release/&delimiter=/", timeout=30)
    r.raise_for_status()
    releases = re.findall(r"<Prefix>release/([^<]+)/</Prefix>", r.text)
    if not releases:
        raise RuntimeError("Could not list Overture releases")
    return sorted(releases)[-1]


def import_release(release: str, db_path) -> int:
    src = f"s3://overturemaps-us-west-2/release/{release}/theme=places/type=place/*"
    duck = duckdb.connect()
    if ext_dir := os.environ.get("UKA_DUCKDB_EXTENSIONS"):  # desktop app: httpfs ships with the app
        duck.execute("SET extension_directory = '%s'" % ext_dir.replace("'", "''"))
    duck.execute("INSTALL httpfs; LOAD httpfs; SET s3_region='us-west-2';")
    # Keep the remote scan inside a small instance's RAM (DuckDB spills to disk past this).
    duck.execute(f"SET memory_limit='{os.environ.get('UKA_DUCKDB_MEMORY', '256MB')}'; SET threads=2;")
    started = time.monotonic()
    log.info("querying Overture %s for England & Wales (this scans remote parquet; expect several minutes)", release)
    progress.report("Searching the map data for England & Wales (can take 10-30 minutes)", force=True, quiet_s=3600)
    tmp = db_path.with_suffix(".importing")
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    con.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;" + SCHEMA)
    cursor = duck.execute(
        f"""
        SELECT id, names.primary, basic_category, addresses[1].freeform, addresses[1].locality,
               addresses[1].postcode, phones[1], websites[1], confidence, operating_status,
               (bbox.ymin + bbox.ymax) / 2, (bbox.xmin + bbox.xmax) / 2,
               array_to_string(list_distinct(list_transform(sources, lambda s: s.dataset)), ',')
        FROM read_parquet('{src}', hive_partitioning = 1)
        WHERE bbox.xmin BETWEEN ? AND ? AND bbox.ymin BETWEEN ? AND ?
          AND addresses[1].country = 'GB' AND confidence >= ? AND names.primary IS NOT NULL
        """,
        [BBOX["xmin"], BBOX["xmax"], BBOX["ymin"], BBOX["ymax"], MIN_CONFIDENCE],
    )
    count = 0
    while rows := cursor.fetchmany(50_000):  # stream: keeps memory flat on small hosts
        con.executemany(
            "INSERT OR REPLACE INTO places VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ((r[0], r[1], r[2], r[3], r[4], normalize_postcode(r[5]) or "", *r[6:]) for r in rows),
        )
        count += len(rows)
        log.info("stored %s places (%.0fs)", f"{count:,}", time.monotonic() - started)
        progress.report("Downloading businesses", count, None, "places", force=True)
    progress.report("Building the search index", force=True)
    con.execute("CREATE INDEX ix_places_postcode ON places(postcode)")
    # Partial index: the proximity lookup only ever reads places that lack a postcode.
    con.execute("CREATE INDEX ix_places_nopc_latlon ON places(lat, lon) WHERE postcode = ''")
    con.execute("ANALYZE")
    meta = {
        "source": "Overture Maps Foundation places",
        "release": release,
        "licence": "CDLA-Permissive-2.0 (see https://docs.overturemaps.org/attribution/)",
        "imported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "row_count": str(count),
    }
    con.executemany("INSERT INTO meta VALUES (?, ?)", meta.items())
    con.commit()
    con.close()
    swap_in(tmp, db_path)
    return count


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--release", help="Overture release, e.g. 2026-10-21.0 (default: latest)")
    args = parser.parse_args()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(message)s")
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    release = args.release or latest_release()
    count = import_release(release, settings.places_db_path)
    log.info("Overture import complete: %s places -> %s", f"{count:,}", settings.places_db_path)


if __name__ == "__main__":
    main()
