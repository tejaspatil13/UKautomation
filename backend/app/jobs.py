"""Background search jobs: VO search -> dedupe -> contact discovery -> Excel.

Jobs run in-process on the event loop (bounded by a semaphore). State and per-record results
are persisted to SQLite after every stage, so a restart or a provider outage never loses
completed work, and a retry only re-runs the lookups that failed.
"""
import asyncio
import contextlib
import logging
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app.config import Settings
from app.contacts.base import ContactProvider, ProviderError, ProviderRateLimited
from app.excel import ATTRIBUTION, export_filename, write_workbook
from app.grouping import group_records
from app.matching import choose
from app.models import (
    ContactStatus, JobState, JobStatus, JobSummary, PropertyRecord, ResultRecord, SearchRequest,
)
from app.normalize import house_numbers, street_key
from app.companies.local import LocalCompanies
from app.units import is_business_centre, resolve_unit
from app.voa.repository import DatasetMissingError, VoaRepository

log = logging.getLogger(__name__)

JOB_ID_LEN = 32
# Bump when the matching rules change: saved results made with older rules are matched again
# (locally, in about a millisecond each) the next time they are read, so sheets never keep stale picks.
MATCH_VERSION = 5  # 2: business-centre units go to their confirmed occupier only; 3: building count;
# 4-5: unit matching also requires the same building
UK_TZ = ZoneInfo("Europe/London")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, status_json TEXT NOT NULL, export_name TEXT
);
CREATE TABLE IF NOT EXISTS job_records (
    job_id TEXT NOT NULL, seq INTEGER NOT NULL, uarn TEXT NOT NULL, record_json TEXT NOT NULL,
    PRIMARY KEY (job_id, seq)
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def mark_duplicates(records: list[PropertyRecord]) -> int:
    """Flag entries that share building number + street + postcode with an earlier entry.

    They are kept (separate rateable units can be separate businesses) but linked so a reviewer
    can see them together. Returns how many were flagged.
    """
    seen: dict[tuple, str] = {}
    flagged = 0
    for rec in records:
        nums = tuple(sorted(house_numbers(rec.number_or_name)))
        if not nums:
            continue
        key = (nums, street_key(rec.street), rec.postcode)
        if key in seen:
            rec.possible_duplicate_of = seen[key]
            flagged += 1
        else:
            seen[key] = rec.uarn
    return flagged


class JobManager:
    def __init__(self, settings: Settings, providers: list[ContactProvider]):
        self.settings = settings
        self.providers = providers
        self.voa = VoaRepository(settings.voa_db_path)
        self.companies = LocalCompanies(settings.companies_db_path)
        self._slots = asyncio.Semaphore(settings.max_concurrent_jobs)
        self._lookup_slots = asyncio.Semaphore(8)
        self._tasks: dict[str, asyncio.Task] = {}
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        settings.export_dir.mkdir(parents=True, exist_ok=True)
        self._recover_if_corrupt()
        with self._db() as con:
            con.executescript(_SCHEMA)

    def _recover_if_corrupt(self) -> None:
        """Search history is disposable: if its file is damaged, set it aside instead of failing every search."""
        path = self.settings.app_db_path
        if not path.exists():
            return
        try:
            # closed before the rename below: Windows can't rename a file that is still open
            with contextlib.closing(sqlite3.connect(path, timeout=30)) as con:
                ok = con.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        except sqlite3.DatabaseError:
            ok = False
        if ok:
            return
        stamp = utcnow().strftime("%Y%m%d%H%M%S")
        for suffix in ("", "-wal", "-shm"):
            f = path.with_name(path.name + suffix)
            if f.exists():
                f.rename(f.with_name(f"{f.name}.corrupt-{stamp}"))
        log.error("search history database was damaged; moved aside as %s.corrupt-%s and started fresh", path.name, stamp)

    # ---------- persistence ----------
    def _db(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.settings.app_db_path, timeout=30)
        con.execute("PRAGMA journal_mode=WAL")
        return con

    def _save_status(self, status: JobStatus, export_name: str | None = None) -> None:
        with self._db() as con:
            con.execute(
                "INSERT INTO jobs (id, created_at, status_json, export_name) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET status_json = excluded.status_json, "
                "export_name = COALESCE(excluded.export_name, jobs.export_name)",
                (status.id, status.created_at.isoformat(), status.model_dump_json(), export_name),
            )

    def save_records(self, job_id: str, records: list[ResultRecord]) -> None:
        with self._db() as con:
            con.execute("DELETE FROM job_records WHERE job_id = ?", (job_id,))
            con.executemany(
                "INSERT INTO job_records VALUES (?, ?, ?, ?)",
                [(job_id, i, r.uarn, r.model_dump_json()) for i, r in enumerate(records)],
            )

    def get(self, job_id: str) -> JobStatus | None:
        with self._db() as con:
            row = con.execute("SELECT status_json FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return JobStatus.model_validate_json(row[0]) if row else None

    def records(self, job_id: str) -> list[ResultRecord]:
        with self._db() as con:
            rows = con.execute(
                "SELECT record_json FROM job_records WHERE job_id = ? ORDER BY seq", (job_id,)
            ).fetchall()
        return [ResultRecord.model_validate_json(r[0]) for r in rows]

    def export(self, job_id: str) -> tuple[Path, str] | None:
        with self._db() as con:
            row = con.execute("SELECT export_name FROM jobs WHERE id = ?", (job_id,)).fetchone()
        path = self.settings.export_dir / f"{job_id}.xlsx"
        if not row or not row[0] or not path.exists():
            return None
        return path, row[0]

    def recent(self, limit: int = 20, employee: str | None = None) -> list[JobStatus]:
        with self._db() as con:
            rows = con.execute("SELECT status_json FROM jobs ORDER BY created_at DESC").fetchall()
        jobs = [JobStatus.model_validate_json(r[0]) for r in rows]
        if employee is not None:
            jobs = [j for j in jobs if j.employee.casefold() == employee.casefold()]
        return jobs[:limit]

    def delete(self, job_id: str) -> bool:
        """Remove a finished search, its results and its spreadsheet. Running searches can't be deleted."""
        status = self.get(job_id)
        if status is None or status.state in (JobState.QUEUED, JobState.RUNNING):
            return False
        with self._db() as con:
            con.execute("DELETE FROM job_records WHERE job_id = ?", (job_id,))
            con.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        (self.settings.export_dir / f"{job_id}.xlsx").unlink(missing_ok=True)
        return True

    # ---------- lifecycle ----------
    def submit(self, req: SearchRequest, employee: str = "") -> JobStatus:
        status = JobStatus(
            id=uuid.uuid4().hex, state=JobState.QUEUED, stage="Queued", request=req, created_at=utcnow(),
            employee=employee,
        )
        self._save_status(status)
        self._start(status, retry=False)
        return status

    def retry(self, job_id: str) -> JobStatus | None:
        status = self.get(job_id)
        if status is None or status.state in (JobState.QUEUED, JobState.RUNNING):
            return status
        status.state, status.stage, status.errors = JobState.QUEUED, "Queued for retry", []
        self._save_status(status)
        self._start(status, retry=True)
        return status

    def _start(self, status: JobStatus, retry: bool) -> None:
        task = asyncio.create_task(self._run(status, retry))
        self._tasks[status.id] = task
        task.add_done_callback(lambda _: self._tasks.pop(status.id, None))

    def resume_interrupted(self) -> None:
        """Called at startup: jobs that were running when the process stopped are resumed."""
        for status in self.recent(limit=200):
            if status.state in (JobState.QUEUED, JobState.RUNNING):
                log.info("resuming interrupted job %s", status.id)
                self._start(status, retry=bool(self.records(status.id)))

    def cleanup_exports(self) -> int:
        """Delete exports older than the retention period (VO terms: delete when no longer needed)."""
        cutoff = time.time() - self.settings.export_retention_hours * 3600
        removed = 0
        for f in self.settings.export_dir.glob("*.xlsx"):
            if f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
                removed += 1
        with self._db() as con:
            old = (utcnow() - timedelta(hours=self.settings.export_retention_hours)).isoformat()
            ids = [r[0] for r in con.execute("SELECT id FROM jobs WHERE created_at < ?", (old,))]
            con.executemany("DELETE FROM job_records WHERE job_id = ?", [(i,) for i in ids])
            con.executemany("DELETE FROM jobs WHERE id = ?", [(i,) for i in ids])
        return removed

    # ---------- pipeline ----------
    async def _run(self, status: JobStatus, retry: bool) -> None:
        started = time.monotonic()
        async with self._slots:
            status.state = JobState.RUNNING
            try:
                records = self.records(status.id) if retry else []
                if not records:
                    records = await self._stage_search(status)
                    if not records:
                        return
                await self._stage_contacts(status, records)
                await self._stage_excel(status, records)
            except DatasetMissingError as e:
                self._fail(status, str(e))
            except Exception:
                log.exception("job %s failed", status.id)
                self._fail(status, "Unexpected error while processing this search. Completed results were kept; try Retry.")
            finally:
                status.duration_seconds = round(time.monotonic() - started, 2)
                self._save_status(status)

    def _progress(self, status: JobStatus, stage: str, progress: float, message: str = "") -> None:
        status.stage, status.progress, status.message = stage, round(progress, 3), message
        self._save_status(status)

    def _fail(self, status: JobStatus, message: str) -> None:
        status.state, status.stage, status.finished_at = JobState.FAILED, "Failed", utcnow()
        status.errors.append(message)
        status.message = message

    async def _stage_search(self, status: JobStatus) -> list[ResultRecord]:
        req = status.request
        self._progress(status, "Searching VO rating list", 0.02)
        props, truncated = await asyncio.to_thread(self.voa.search, req, self.settings.max_results_per_search)
        if not props:
            hint = ""
            if req.town and not await asyncio.to_thread(self.voa.town_exists, req.town):
                hint = f" The VO list has no town or locality called '{req.town}' — check the spelling."
            status.state, status.stage, status.finished_at = JobState.NO_RESULTS, "No results", utcnow()
            status.message = "No rateable premises matched this search." + hint
            return []

        self._progress(status, "Normalising and de-duplicating", 0.08)
        dupes = mark_duplicates(props)
        records = [ResultRecord(**p.model_dump()) for p in props]
        status.summary = JobSummary(source_records=len(records), unique_premises=len(records) - dupes)
        if truncated:
            status.errors.append(
                f"More than {self.settings.max_results_per_search:,} premises matched; only the first "
                f"{self.settings.max_results_per_search:,} were processed. Narrow the search (postcode sector or RV range)."
            )
        self.save_records(status.id, records)
        return records

    async def _stage_contacts(self, status: JobStatus, records: list[ResultRecord]) -> None:
        if not self.providers:
            raise DatasetMissingError(
                "The business/phone dataset has not been imported yet. Run: python -m app.contacts.overture_import"
            )
        todo = [r for r in records if r.contact_status in (ContactStatus.NOT_SEARCHED, ContactStatus.LOOKUP_FAILED)]
        done = 0
        total = max(len(todo), 1)

        async def one(rec: ResultRecord) -> None:
            nonlocal done
            async with self._lookup_slots:
                await self._lookup(rec)
            done += 1
            if done % 25 == 0 or done == len(todo):
                self._progress(status, "Finding contact details", 0.1 + 0.8 * done / total,
                               f"{done:,} of {len(todo):,} premises checked")
            if done % 200 == 0:
                self.save_records(status.id, records)  # checkpoint for crash recovery

        self._progress(status, "Finding contact details", 0.1, f"0 of {len(todo):,} premises checked")
        await asyncio.gather(*(one(r) for r in todo))
        self.save_records(status.id, records)

    async def _lookup(self, rec: ResultRecord) -> None:
        candidates, failures = [], []
        for provider in self.providers:
            try:
                candidates += await provider.candidates(rec)
            except ProviderRateLimited as e:
                failures.append(f"{provider.name}: rate limited")
                log.warning("provider %s rate limited (retry_after=%s)", provider.name, e.retry_after)
            except ProviderError as e:
                failures.append(f"{provider.name}: {e}")
            except Exception:
                log.exception("provider %s crashed on %s", provider.name, rec.uarn)
                failures.append(f"{provider.name}: unexpected error")

        rec.collected_at = utcnow()
        if failures and not candidates:
            rec.contact_status, rec.match_reasons = ContactStatus.LOOKUP_FAILED, failures
            return
        match = choose(rec, candidates)
        rec.match_version = MATCH_VERSION
        cand = match.candidate
        rec.contact_status, rec.match_score = match.status, match.score
        rec.match_reasons, rec.phone, rec.other_numbers = match.reasons + failures, match.phone, match.others
        rec.match_note = match.note
        rec.building_businesses = match.building_businesses
        if cand:
            rec.business_name = cand.name
            rec.business_address = ", ".join(p for p in (cand.address, cand.postcode) if p)
            rec.contact_source, rec.contact_source_url = cand.source, cand.source_url
            rec.contact_address = ", ".join(p for p in (cand.name, cand.address, cand.postcode) if p)
        if is_business_centre(rec):
            companies = await asyncio.to_thread(self.companies.at_postcode, rec.postcode)
            resolve_unit(rec, candidates, companies)

    async def rematch(self, records: list[ResultRecord]) -> list[ResultRecord]:
        """Match again the records made with older rules (in place). Returns the ones that changed."""
        stale = [r for r in records if r.match_version < MATCH_VERSION
                 and r.contact_status not in (ContactStatus.NOT_SEARCHED, ContactStatus.LOOKUP_FAILED)]
        if not stale or not self.providers:
            return []

        async def one(rec: ResultRecord) -> None:
            _reset_match(rec)
            async with self._lookup_slots:
                await self._lookup(rec)

        await asyncio.gather(*(one(r) for r in stale))
        return stale

    async def _stage_excel(self, status: JobStatus, records: list[ResultRecord]) -> None:
        s = status.summary
        s.phones_found = sum(bool(r.phone) for r in records)
        for field, st in (("verified", ContactStatus.VERIFIED), ("probable", ContactStatus.PROBABLE),
                          ("needs_review", ContactStatus.REVIEW), ("no_contact", ContactStatus.NO_CONTACT),
                          ("lookup_failed", ContactStatus.LOOKUP_FAILED)):
            setattr(s, field, sum(r.contact_status == st for r in records))

        # Only properties with a contact number go into the spreadsheet.
        # Named businesses with a contact number only (business-centre units with no known occupier are left out).
        with_phone = [r for r in records if r.is_lead]
        unknown = sum(r.occupier_unknown for r in records)
        failed_note = f" {s.lookup_failed:,} lookups failed — use Retry to re-run only those." if s.lookup_failed else ""
        if not with_phone:
            status.state, status.stage, status.finished_at = JobState.NO_RESULTS, "Done", utcnow()
            status.progress, status.download_ready = 1.0, False
            status.message = (
                f"{s.source_records:,} listed properties found, but none has a reliable phone number, "
                f"so no spreadsheet was created.{failed_note}"
            )
            return

        self._progress(status, "Generating Excel file", 0.95)
        rows = group_records(with_phone, status.label)
        s.businesses = len(rows)
        req = status.request
        now = datetime.now(UK_TZ)
        name = export_filename(req.town, req.postcode, now)
        criteria = ", ".join(f"{k}={v}" for k, v in req.model_dump().items() if v not in ("", None))
        about = [
            ("Search", criteria),
            ("Generated", now.strftime("%d %b %Y %H:%M %Z")),
            ("Counts", f"{s.source_records:,} listed properties; {len(with_phone):,} with a phone number, "
                       f"from {len(rows):,} businesses (one row each, in this file); {s.no_contact:,} no contact; "
                       f"{s.lookup_failed:,} lookup failed"),
            *ATTRIBUTION,
        ]
        try:
            await asyncio.to_thread(
                write_workbook, self.settings.template_path, self.settings.export_dir / f"{status.id}.xlsx",
                rows, f"RV ({self.settings.voa_list_year} list)", about,
            )
        except Exception:
            log.exception("excel generation failed for job %s", status.id)
            self._fail(status, "Excel generation failed. The results are kept and shown below; try Retry.")
            return
        partial = s.lookup_failed > 0 or bool(status.errors)
        status.state = JobState.PARTIAL if partial else JobState.COMPLETED
        status.stage, status.progress, status.finished_at = "Done", 1.0, utcnow()
        status.download_ready = True
        status.message = (
            f"{s.source_records:,} listed properties searched; {len(rows):,} leads are in the Excel file"
            + (f"; {unknown:,} business-centre units left out because their occupier is not known" if unknown else "")
            + f".{failed_note}"
        )
        self._save_status(status, export_name=name)


def _reset_match(rec: ResultRecord) -> None:
    """Clear everything a previous match filled in, keeping the VO property itself."""
    fresh = ResultRecord(**PropertyRecord.model_validate(rec.model_dump()).model_dump(exclude={"source_url"}))
    for field in ResultRecord.model_fields:
        if field not in PropertyRecord.model_fields:
            setattr(rec, field, getattr(fresh, field))


def is_valid_job_id(job_id: str) -> bool:
    return len(job_id) == JOB_ID_LEN and all(c in "0123456789abcdef" for c in job_id)
