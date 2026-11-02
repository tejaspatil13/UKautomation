"""Potential customers: My-sheet businesses whose registered company (and owner) was found.

"Get business owner names" runs in the background per employee. With the Companies House bulk
data imported (app.companies.bulk_import) the company, its details and its owners are found
locally — no requests at all. Only the active officers (directors, partners ...) of each matched
company are fetched from Companies House, one request per company, remembered for 30 days.
Without the bulk data, each business is searched on Companies House instead (the old way, which
Companies House blocks after a few hundred requests).

Matches are copied into the employee's Potential customers table, which stays even if the row is
later removed from My sheet.
"""
import asyncio
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.companies.house import CompaniesHouse, CompaniesHouseBlocked, CompaniesHouseError, CompanyMatch, Person
from app.companies.local import LocalCompanies
from app.excel import ExportRow
from app.models import ResultRecord
from app.normalize import name_key, normalize_postcode, postcode_in_text
from app.sheets import employee_key

log = logging.getLogger(__name__)
CACHE_DAYS = 30
_NOT_SAVED = object()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS company_lookups (
    lookup_key TEXT PRIMARY KEY, company_json TEXT, checked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS officer_lookups (
    company_number TEXT PRIMARY KEY, people_json TEXT NOT NULL, checked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS customers (
    employee_key TEXT NOT NULL, business_key TEXT NOT NULL, records_json TEXT NOT NULL,
    company_json TEXT NOT NULL, search TEXT NOT NULL, added_at TEXT NOT NULL,
    PRIMARY KEY (employee_key, business_key)
);
"""


def postcodes_for(row: ExportRow) -> set[str]:
    """Postcodes the registered office may match: every VO entry's, plus the business's own."""
    pcs = {p.postcode for p in row.properties if p.postcode}
    if own := normalize_postcode(postcode_in_text(row.record.business_address)):
        pcs.add(own)
    return pcs


def lookup_key(row: ExportRow) -> str:
    return f"{name_key(row.record.business_name)}|{','.join(sorted(postcodes_for(row)))}"


@dataclass
class RunState:
    running: bool = False
    done: int = 0
    total: int = 0
    found: int = 0
    failed: int = 0
    blocked: bool = False  # Companies House refused us (403): only saved results were used after that
    skipped: int = 0  # not checked because of the block
    officers_missing: int = 0  # matched, but officer names could not be fetched this time
    message: str = ""

    def as_dict(self) -> dict:
        return self.__dict__.copy()


class OwnerFinder:
    def __init__(self, db_path: Path, client: CompaniesHouse, local: LocalCompanies | None = None):
        self.db_path = db_path
        self.client = client
        self.local = local
        self._runs: dict[str, RunState] = {}
        self._tasks: set[asyncio.Task] = set()
        with self._db() as con:
            con.executescript(_SCHEMA)
            # Officers already fetched by the old per-business search: reuse them instead of asking again.
            con.execute(
                "INSERT OR IGNORE INTO officer_lookups SELECT json_extract(company_json, '$.company_number'), "
                "json_extract(company_json, '$.directors'), checked_at FROM company_lookups "
                "WHERE company_json IS NOT NULL AND json_extract(company_json, '$.details_checked')"
            )

    def _db(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=30)
        con.execute("PRAGMA journal_mode=WAL")
        return con

    # ---------- background run ----------
    def status(self, employee: str) -> RunState:
        return self._runs.get(employee_key(employee), RunState())

    def start(self, employee: str, rows: list[ExportRow]) -> RunState:
        key = employee_key(employee)
        if self._runs.get(key, RunState()).running:
            return self._runs[key]
        rows = [r for r in rows if r.record.business_name and postcodes_for(r)]
        state = self._runs[key] = RunState(running=True, total=len(rows), message="Starting…")
        task = asyncio.create_task(self._run(key, rows, state))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return state

    async def _run(self, employee: str, rows: list[ExportRow], state: RunState) -> None:
        try:
            local = self.local is not None and self.local.available()
            for row in rows:
                try:
                    if local:
                        company = await self._local_lookup(row, state)
                    else:
                        company = await self._lookup(row, offline=state.blocked)
                except CompaniesHouseBlocked:
                    # Stop asking Companies House (retrying only prolongs a block), but keep going
                    # through the rest so every owner found earlier is still restored from the cache.
                    state.blocked = True
                    log.warning("Companies House refused requests (403) in %s mode; using saved results only",
                                self.client.mode)
                    company = await self._lookup(row, offline=True)
                except CompaniesHouseError as e:
                    state.failed += 1
                    log.warning("owner lookup failed for %r: %s", row.record.business_name, e)
                    company = None
                if company is _NOT_SAVED:
                    state.skipped += 1
                elif company:
                    state.found += 1
                    self._save_customer(employee, row, company)
                state.done += 1
                state.message = f"{state.done:,} of {state.total:,} checked · {state.found:,} owners found"
            state.message = self._final_message(state)
        except Exception:
            log.exception("owner search crashed")
            state.message = "The owner search stopped unexpectedly. Results found so far were kept."
        finally:
            state.running = False

    async def _local_lookup(self, row: ExportRow, state: RunState) -> CompanyMatch | None:
        company = await asyncio.to_thread(self.local.find, row.record.business_name, postcodes_for(row))
        if company:
            officers = await self._officers(company.company_number, state)
            if officers is None:
                state.officers_missing += 1
            else:
                company.directors = officers
        with self._db() as con:  # remembered so My sheet can show which rows have been checked
            con.execute("INSERT OR REPLACE INTO company_lookups VALUES (?, ?, ?)",
                        (lookup_key(row), company.model_dump_json() if company else None,
                         datetime.now(timezone.utc).isoformat(timespec="seconds")))
        return company

    async def _officers(self, number: str, state: RunState) -> list[Person] | None:
        """Active officers of a company: remembered for 30 days; None when they can't be fetched now."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=CACHE_DAYS)).isoformat()
        with self._db() as con:
            hit = con.execute("SELECT people_json FROM officer_lookups WHERE company_number = ? AND checked_at > ?",
                              (number, cutoff)).fetchone()
        if hit:
            return [Person.model_validate(p) for p in json.loads(hit[0])]
        if state.blocked:
            return None
        try:
            people = await self.client.officers(number)
        except CompaniesHouseBlocked:
            state.blocked = True
            log.warning("Companies House refused requests (403); officer names will be added on a later run")
            return None
        except CompaniesHouseError as e:
            log.warning("officers of %s could not be fetched: %s", number, e)
            return None
        with self._db() as con:
            con.execute("INSERT OR REPLACE INTO officer_lookups VALUES (?, ?, ?)",
                        (number, json.dumps([p.model_dump() for p in people]),
                         datetime.now(timezone.utc).isoformat(timespec="seconds")))
        return people

    def _final_message(self, state: RunState) -> str:
        if self.local is not None and self.local.available():
            msg = f"Finished: {state.found:,} of {state.total:,} businesses matched on the Companies House register."
            if state.officers_missing:
                msg += (f" Director names for {state.officers_missing:,} of them couldn't be fetched right now "
                        "(Companies House busy or blocking); run it again later to add them.")
            return msg
        if state.blocked:
            msg = (f"{state.found:,} owners found. {state.skipped:,} businesses could not be checked: ")
            if self.client.mode == "api":
                return msg + "Companies House rejected the API key. Check UKA_COMPANIES_HOUSE_API_KEY in .env."
            return msg + ("Companies House has temporarily blocked automatic lookups from this internet connection. "
                          "Add the free Companies House API key to fix this for good, or try again later.")
        msg = f"Finished: {state.found:,} of {state.total:,} businesses matched on Companies House."
        if state.failed:
            msg += f" {state.failed:,} could not be checked — run it again later to retry those."
        return msg

    async def _lookup(self, row: ExportRow, offline: bool = False):
        """The matched company, None (checked, no match) or _NOT_SAVED (offline and never checked)."""
        key = lookup_key(row)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=CACHE_DAYS)).isoformat()
        with self._db() as con:
            hit = con.execute("SELECT company_json FROM company_lookups WHERE lookup_key = ? AND checked_at > ?",
                              (key, cutoff)).fetchone()
        if hit and not hit[0]:
            return None
        if hit:
            company = CompanyMatch.model_validate_json(hit[0])
            if company.has_details or offline:
                return company
            company = await self.client.refresh(company)  # matched before details were collected
        elif offline:
            return _NOT_SAVED
        else:
            company = await self.client.find(row.record.business_name, postcodes_for(row))
        with self._db() as con:
            con.execute("INSERT OR REPLACE INTO company_lookups VALUES (?, ?, ?)",
                        (key, company.model_dump_json() if company else None,
                         datetime.now(timezone.utc).isoformat(timespec="seconds")))
        return company

    # ---------- potential customers table ----------
    def _save_customer(self, employee: str, row: ExportRow, company: CompanyMatch) -> None:
        records = json.dumps([p.model_dump(mode="json") for p in row.properties])
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._db() as con:
            con.execute(
                "INSERT INTO customers VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(employee_key, business_key) DO UPDATE "
                "SET records_json = excluded.records_json, company_json = excluded.company_json",
                (employee, row.key, records, company.model_dump_json(), row.search, now),
            )

    def customers(self, employee: str, sheet: dict[str, ResultRecord] | None = None) -> list[ExportRow]:
        """The employee's potential customers.

        With `sheet` (uarn -> the current My-sheet result), each customer keeps only the rated units
        that My sheet still gives to that business: a unit later found to belong to someone else (or
        to an unknown occupier in a business centre) is dropped, and a customer left with no units is
        removed. Units no longer in My sheet are kept as they were.
        """
        from app.grouping import business_key

        with self._db() as con:
            found = con.execute(
                "SELECT business_key, records_json, company_json, search, added_at FROM customers "
                "WHERE employee_key = ? ORDER BY added_at, rowid", (employee_key(employee),),
            ).fetchall()
        rows, changed, gone = [], [], []
        for key, records, company, search, added in found:
            saved = [ResultRecord.model_validate(r) for r in json.loads(records)]
            props = saved
            if sheet is not None:
                props = [sheet.get(p.uarn, p) for p in saved
                         if p.uarn not in sheet or (sheet[p.uarn].is_lead
                                                    and business_key(sheet[p.uarn], merge_shared=True) == key)]
                if not props:
                    gone.append(key)
                    continue
                if [p.model_dump() for p in props] != [p.model_dump() for p in saved]:
                    changed.append((json.dumps([p.model_dump(mode="json") for p in props]), employee_key(employee), key))
            props.sort(key=lambda r: -r.rv)
            rows.append(ExportRow(props[0], search, datetime.fromisoformat(added), props, key=key,
                                  company=CompanyMatch.model_validate_json(company)))
        if changed or gone:
            with self._db() as con:
                con.executemany("UPDATE customers SET records_json = ? WHERE employee_key = ? AND business_key = ?", changed)
                con.executemany("DELETE FROM customers WHERE employee_key = ? AND business_key = ?",
                                [(employee_key(employee), k) for k in gone])
        return rows

    def checked_keys(self, rows: list[ExportRow]) -> set[str]:
        """Which of these rows have already been looked up (within the cache period)."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=CACHE_DAYS)).isoformat()
        with self._db() as con:
            done = {r[0] for r in con.execute("SELECT lookup_key FROM company_lookups WHERE checked_at > ?", (cutoff,))}
        return {r.key for r in rows if lookup_key(r) in done}

    def remove(self, employee: str, keys: list[str]) -> int:
        with self._db() as con:
            return sum(con.execute("DELETE FROM customers WHERE employee_key = ? AND business_key = ?",
                                   (employee_key(employee), k)).rowcount for k in keys)

    def clear(self, employee: str) -> int:
        with self._db() as con:
            return con.execute("DELETE FROM customers WHERE employee_key = ?", (employee_key(employee),)).rowcount
