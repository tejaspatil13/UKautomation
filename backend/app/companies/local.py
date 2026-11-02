"""Match a business to its registered company using the local Companies House bulk data.

Same rule as the online lookup — an active company with a similar name whose registered office
postcode is the premises' postcode — but answered from SQLite in a millisecond, with no requests
to Companies House. Owners (people with significant control) come from the same file.
"""
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from rapidfuzz import fuzz

from app.companies.house import MIN_NAME_SIMILARITY, CompanyMatch, Person, label, nice_date, search_query
from app.normalize import name_key


def control_labels(codes: str) -> list[str]:
    return [label("psc_control", c) for c in codes.split(",") if c]


class LocalCompanies:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._con: sqlite3.Connection | None = None
        self._inode = None

    def available(self) -> bool:
        return self.db_path.exists()

    def _connection(self) -> sqlite3.Connection:
        inode = os.stat(self.db_path).st_ino
        if self._con is None or inode != self._inode:  # first use, closed, or an update swapped in a new file
            if self._con:
                self._con.close()
            self._con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, check_same_thread=False)
            self._inode = inode
        return self._con

    def close(self) -> None:
        """Release the file (Windows can't replace an open file); the next lookup opens it again."""
        with self._lock:
            if self._con:
                self._con.close()
            self._con = None

    def at_postcode(self, postcode: str) -> list[tuple[str, str, str]]:
        """(number, name, registered address) of every active company registered at the postcode."""
        if not self.available():
            return []
        with self._lock:
            return self._connection().execute(
                "SELECT number, name, address FROM companies WHERE postcode = ? AND status LIKE 'Active%'", (postcode,),
            ).fetchall()

    def find(self, business_name: str, postcodes: set[str]) -> CompanyMatch | None:
        key = name_key(search_query(business_name))
        if not key or not postcodes:
            return None
        with self._lock:
            con = self._connection()
            marks = ",".join("?" * len(postcodes))
            rows = con.execute(
                f"SELECT number, name, address, status, category, incorporated, sic FROM companies "
                f"WHERE postcode IN ({marks}) AND status LIKE 'Active%'", sorted(postcodes),
            ).fetchall()
            best, best_score = None, 0
            for row in rows:
                score = fuzz.token_set_ratio(key, name_key(row[1]))
                if score >= MIN_NAME_SIMILARITY and score > best_score:
                    best, best_score = row, score
            if best is None:
                return None
            owners = con.execute(
                "SELECT name, corporate, since, control, nationality FROM psc WHERE company_number = ?", (best[0],),
            ).fetchall()
        number, name, address, status, category, incorporated, sic = best
        return CompanyMatch(
            company_name=name, company_number=number, registered_address=address,
            incorporated=nice_date(incorporated), status=status, company_type=category,
            nature_of_business=[s for s in (sic or "").split("; ") if s],
            owners=[Person(name=n, role="Owner", since=nice_date(since), control=control_labels(control),
                           nationality=nat or "", corporate=bool(corp))
                    for n, corp, since, control, nat in owners],
            details_checked=True, checked_at=datetime.now(timezone.utc),
        )
