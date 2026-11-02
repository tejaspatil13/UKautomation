"""FastAPI application: JSON API under /api, built dashboard served at /."""
import asyncio
import base64
import contextlib
import logging
import secrets
import shutil
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app.companies.house import CompaniesHouse
from app.companies.local import LocalCompanies
from app.config import get_settings
from app.contacts.registry import build_providers
from app.excel import ATTRIBUTION, ExportRow, clean_filename, write_workbook
from app.grouping import group_records, group_rows, row_json, split
from app.jobs import JobManager, is_valid_job_id
from app.models import JobState, JobStatus, ResultRecord, SearchRequest, clean_employee
from app.owners import OwnerFinder
from app.sheets import SheetStore
from app.refresh import check, datasets, install_pending, install_ready_at_startup, read_meta, refresh_loop
from app.voa.description_codes import description_codes
from app.voa.repository import DatasetMissingError, VoaRepository

settings = get_settings()
logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("app")

MAX_ACTIVE_JOBS = 10  # queued + running, across all users
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MAX_PARTS = 100
UK_TZ = ZoneInfo("Europe/London")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.updates = datasets(settings)
    install_ready_at_startup(app.state.updates)  # before anything opens the datasets
    manager = JobManager(settings, build_providers(settings))
    app.state.jobs = manager
    app.state.sheets = SheetStore(settings.app_db_path)
    companies = CompaniesHouse(settings.companies_house_api_key)
    app.state.owners = OwnerFinder(settings.app_db_path, companies, LocalCompanies(settings.companies_db_path))
    asyncio.get_running_loop().run_in_executor(None, manager.voa.warm_up)  # town/postcode lists for type-ahead
    manager.cleanup_exports()
    manager.resume_interrupted()

    async def periodic_cleanup():
        while True:
            await asyncio.sleep(3600)
            manager.cleanup_exports()

    def places_updated():
        manager.providers = build_providers(settings)

    def voa_updated():
        # fresh repository = fresh town/postcode/description caches for the new list
        manager.voa = VoaRepository(settings.voa_db_path)
        asyncio.get_running_loop().run_in_executor(None, manager.voa.warm_up)

    def release_places():
        for provider in manager.providers:
            if close := getattr(provider, "close", None):
                close()

    def release_companies():
        manager.companies.close()
        app.state.owners.local.close()

    app.state.update_callbacks = {"app.voa.importer": voa_updated, "app.contacts.overture_import": places_updated}
    # Close the app's own connections so a new file can be swapped in (needed on Windows).
    app.state.release_callbacks = {"app.contacts.overture_import": release_places,
                                   "app.companies.bulk_import": release_companies}
    tasks = [asyncio.create_task(periodic_cleanup())]
    if settings.auto_refresh:
        tasks.append(asyncio.create_task(refresh_loop(settings, app.state.updates, app.state.update_callbacks,
                                                      app.state.release_callbacks)))
    yield
    for task in tasks:
        task.cancel()
    await companies.aclose()


app = FastAPI(title="Copilot", lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")


# ---------- middleware ----------
@app.middleware("http")
async def security(request: Request, call_next):
    if settings.app_password and request.url.path != "/api/health":
        if not _authorised(request.headers.get("authorization", "")):
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Copilot"'})
    response = await call_next(request)
    response.headers.update({
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:",
    })
    return response


def _authorised(header: str) -> bool:
    if not header.startswith("Basic "):
        return False
    try:
        _, _, password = base64.b64decode(header[6:]).decode().partition(":")
    except Exception:
        return False
    return secrets.compare_digest(password.encode(), settings.app_password.encode())


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    messages = []
    for err in exc.errors():
        field = ".".join(str(p) for p in err["loc"] if p != "body")
        msg = err["msg"].removeprefix("Value error, ")
        messages.append(f"{field.replace('_', ' ').capitalize()}: {msg}" if field else msg)
    return JSONResponse(status_code=422, content={"detail": messages})


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.exception("unhandled error on %s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": ["Internal server error."]})


# ---------- API ----------
def _jobs(request: Request) -> JobManager:
    return request.app.state.jobs


def _job_or_404(request: Request, job_id: str) -> JobStatus:
    status = _jobs(request).get(job_id) if is_valid_job_id(job_id) else None
    if status is None:
        raise HTTPException(404, detail=["Search not found (it may have expired)."])
    return status


@app.get("/api/health")
def health(request: Request):
    try:
        meta = _jobs(request).voa.meta()
    except DatasetMissingError:
        meta = None
    return {"status": "ok" if meta else "dataset_missing", "voa_dataset": meta}


@app.get("/api/meta")
def meta(request: Request):
    jobs = _jobs(request)
    try:
        voa = jobs.voa.meta()
    except DatasetMissingError:
        voa = None
    return {
        "voa_dataset": voa,
        "contact_providers": [p.name for p in jobs.providers],
        "max_results_per_search": settings.max_results_per_search,
        "export_retention_hours": settings.export_retention_hours,
        "description_codes": [{"code": c, "label": label} for c, label in description_codes().items()],
        "places_dataset": read_meta(settings.places_db_path),
        "companies_dataset": read_meta(settings.companies_db_path),
        "companies_house": "api" if settings.companies_house_api_key else "website",
        "data_updates": [d.status() for d in request.app.state.updates],
    }


def _employee(name: str | None) -> str:
    try:
        return clean_employee(name or "")
    except ValueError as e:
        raise HTTPException(422, detail=[str(e)]) from None


@app.get("/api/suggest/towns")
def suggest_towns(request: Request, q: str = Query("", max_length=60)):
    try:
        return _jobs(request).voa.suggest_towns(q)
    except DatasetMissingError:
        return []


@app.get("/api/suggest/postcodes")
def suggest_postcodes(request: Request, town: str = Query("", max_length=60), q: str = Query("", max_length=10)):
    try:
        return _jobs(request).voa.suggest_postcodes(town, q)
    except DatasetMissingError:
        return []


@app.post("/api/jobs", status_code=202, response_model=JobStatus)
async def create_job(req: SearchRequest, request: Request, employee: str = ""):
    jobs = _jobs(request)
    if sum(j.state in (JobState.QUEUED, JobState.RUNNING) for j in jobs.recent(50)) >= MAX_ACTIVE_JOBS:
        raise HTTPException(429, detail=["Too many searches are running. Please wait for one to finish."])
    return jobs.submit(req, employee=_employee(employee) if employee else "")


@app.get("/api/jobs", response_model=list[JobStatus])
def list_jobs(request: Request, employee: str | None = None):
    return _jobs(request).recent(30, employee=_employee(employee) if employee else None)


@app.delete("/api/jobs/{job_id}", status_code=204)
def delete_job(job_id: str, request: Request):
    _job_or_404(request, job_id)
    if not _jobs(request).delete(job_id):
        raise HTTPException(409, detail=["This search is still running. Delete it once it has finished."])
    return Response(status_code=204)


@app.get("/api/jobs/{job_id}", response_model=JobStatus)
def get_job(job_id: str, request: Request):
    return _job_or_404(request, job_id)


@app.get("/api/jobs/{job_id}/records")
async def get_records(
    job_id: str, request: Request,
    has_phone: bool | None = None,
    offset: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=1000),
):
    status = _job_or_404(request, job_id)
    jobs = _jobs(request)
    records: list[ResultRecord] = jobs.records(job_id)
    if status.state not in (JobState.QUEUED, JobState.RUNNING) and await jobs.rematch(records):
        jobs.save_records(job_id, records)  # made with older matching rules: refreshed
    if has_phone is not None:
        records = [r for r in records if r.is_lead == has_phone]
    rows = group_records(records, status.label)  # one row per business
    page = rows[offset: offset + limit]
    return {"total": len(rows), "properties": len(records), "items": [row_json(r) for r in page]}


@app.post("/api/jobs/{job_id}/retry", response_model=JobStatus)
async def retry_job(job_id: str, request: Request):
    _job_or_404(request, job_id)
    return _jobs(request).retry(job_id)


@app.get("/api/jobs/{job_id}/download")
def download(job_id: str, request: Request, filename: str | None = Query(None, max_length=120)):
    _job_or_404(request, job_id)
    found = _jobs(request).export(job_id)
    if not found:
        raise HTTPException(404, detail=["No spreadsheet is available for this search."])
    path, default_name = found
    return FileResponse(path, filename=clean_filename(filename, default_name), media_type=XLSX)


# ---------- My sheet ----------
def _sheets(request: Request) -> SheetStore:
    return request.app.state.sheets


class _EmployeeBody(BaseModel):
    employee: str = Field(max_length=60)

    @field_validator("employee")
    @classmethod
    def _valid(cls, v: str) -> str:
        return clean_employee(v)


class SheetAdd(_EmployeeBody):
    job_id: str
    only_with_phone: bool = True


class SheetRemove(_EmployeeBody):
    uarns: list[str] = Field(max_length=5000)


class SheetDownload(_EmployeeBody):
    filename: str = Field("", max_length=120)
    uarns: list[str] | None = Field(None, max_length=20000)  # None = whole sheet
    parts: int = Field(1, ge=1, le=MAX_PARTS)  # >1 = a .zip of that many Excel files


class OwnerRun(_EmployeeBody):
    uarns: list[str] | None = Field(None, max_length=20000)  # None = whole sheet


class CustomerRemove(_EmployeeBody):
    keys: list[str] = Field(max_length=5000)


class CustomerDownload(_EmployeeBody):
    filename: str = Field("", max_length=120)
    keys: list[str] | None = Field(None, max_length=20000)
    parts: int = Field(1, ge=1, le=MAX_PARTS)


def _owners(request: Request) -> OwnerFinder:
    return request.app.state.owners


async def _sheet_rows(request: Request, employee: str) -> list[ExportRow]:
    """My-sheet rows; rows matched with older rules are matched again (and saved) first."""
    rows = _sheets(request).rows(employee)
    if changed := await _jobs(request).rematch([r.record for r in rows]):
        _sheets(request).update(employee, changed)
    return rows


async def _sheet_groups(request: Request, employee: str) -> list[ExportRow]:
    # Only rows that are still leads after re-matching (a named business with a number): a business-centre
    # unit whose occupier turned out to be unknown, for example, is not shown.
    return group_rows([r for r in await _sheet_rows(request, employee) if r.record.is_lead])


async def _customers(request: Request, employee: str) -> list[ExportRow]:
    """Potential customers, keeping only the units My sheet still gives to each business."""
    sheet = {r.record.uarn: r.record for r in await _sheet_rows(request, employee)}
    return _owners(request).customers(employee, sheet)


def _excel_response(rows: list[ExportRow], filename: str, default: str, about: list, parts: int,
                    background: BackgroundTasks, owners: bool = False) -> FileResponse:
    """One Excel file, or (parts > 1) a .zip of `parts` files to hand out to different callers."""
    workdir = Path(tempfile.mkdtemp(dir=settings.export_dir))
    background.add_task(shutil.rmtree, workdir, ignore_errors=True)
    rv_header = f"RV ({settings.voa_list_year} list)"
    name = clean_filename(filename, default)
    chunks = split(rows, parts)
    if len(chunks) == 1:
        out = workdir / "sheet.xlsx"
        write_workbook(settings.template_path, out, rows, rv_header, about, owners=owners)
        return FileResponse(out, filename=name, media_type=XLSX)
    base = name.removesuffix(".xlsx")
    out = workdir / "parts.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, chunk in enumerate(chunks, start=1):
            part = workdir / f"part{i}.xlsx"
            part_about = [("Part", f"{i} of {len(chunks)} ({len(chunk):,} rows)"), *about]
            write_workbook(settings.template_path, part, chunk, rv_header, part_about, owners=owners)
            zf.write(part, f"{base} - part {i:0{len(str(len(chunks)))}d} of {len(chunks)}.xlsx")
            part.unlink()
    return FileResponse(out, filename=f"{base}.zip", media_type="application/zip")


@app.get("/api/sheet")
async def get_sheet(request: Request, employee: str):
    rows = await _sheet_groups(request, _employee(employee))
    checked = _owners(request).checked_keys(rows)
    return {
        "total": len(rows),
        "items": [row_json(r) | {"owner_checked": r.key in checked} for r in rows],
        "owners": _owners(request).status(employee).as_dict(),
    }


@app.post("/api/sheet/add")
async def add_to_sheet(body: SheetAdd, request: Request):
    status = _job_or_404(request, body.job_id)
    if status.state in (JobState.QUEUED, JobState.RUNNING):
        raise HTTPException(409, detail=["Wait for the search to finish before adding it to your sheet."])
    records = _jobs(request).records(body.job_id)
    if body.only_with_phone:
        records = [r for r in records if r.is_lead]
    before = len(await _sheet_groups(request, body.employee))
    _sheets(request).add(body.employee, records, status.label)
    total = len(await _sheet_groups(request, body.employee))
    found = len(group_records(records))
    return {"added": total - before, "already_in_sheet": found - (total - before), "total": total}


@app.post("/api/sheet/remove")
async def remove_from_sheet(body: SheetRemove, request: Request):
    removed = _sheets(request).remove(body.employee, body.uarns)
    return {"removed": removed, "total": len(await _sheet_groups(request, body.employee))}


@app.delete("/api/sheet")
def clear_sheet(request: Request, employee: str):
    return {"removed": _sheets(request).clear(_employee(employee)), "total": 0}


@app.post("/api/sheet/download")
async def download_sheet(body: SheetDownload, request: Request, background: BackgroundTasks):
    rows = await _sheet_groups(request, body.employee)
    if body.uarns is not None:
        wanted = set(body.uarns)
        rows = [r for r in rows if any(p.uarn in wanted for p in r.properties)]
    if not rows:
        raise HTTPException(404, detail=["There are no rows to download."])
    now = datetime.now(UK_TZ)
    about = [
        ("Employee", body.employee),
        ("Generated", now.strftime("%d %b %Y %H:%M %Z")),
        ("Rows", f"{len(rows):,} rows from {len({r.search for r in rows}):,} search(es)"),
        *ATTRIBUTION,
    ]
    return await asyncio.to_thread(_excel_response, rows, body.filename,
                                   f"{body.employee} - My sheet - {now:%d %b %Y}.xlsx", about, body.parts, background)


# ---------- Potential customers (owner names from Companies House) ----------
@app.post("/api/owners/run")
async def run_owner_search(body: OwnerRun, request: Request):
    # One lookup per business, even when its units in a shared building are shown as separate rows.
    rows = group_rows(await _sheet_rows(request, body.employee), merge_shared=True)
    if body.uarns is not None:
        wanted = set(body.uarns)
        rows = [r for r in rows if any(p.uarn in wanted for p in r.properties)]
    if not rows:
        raise HTTPException(404, detail=["There are no rows to check."])
    return _owners(request).start(body.employee, rows).as_dict()


@app.get("/api/owners/status")
def owner_search_status(request: Request, employee: str):
    return _owners(request).status(_employee(employee)).as_dict()


@app.get("/api/owners")
async def list_customers(request: Request, employee: str):
    rows = await _customers(request, _employee(employee))
    return {"total": len(rows), "items": [row_json(r) for r in rows]}


@app.post("/api/owners/remove")
async def remove_customers(body: CustomerRemove, request: Request):
    removed = _owners(request).remove(body.employee, body.keys)
    return {"removed": removed, "total": len(await _customers(request, body.employee))}


@app.delete("/api/owners")
def clear_customers(request: Request, employee: str):
    return {"removed": _owners(request).clear(_employee(employee)), "total": 0}


@app.post("/api/owners/download")
async def download_customers(body: CustomerDownload, request: Request, background: BackgroundTasks):
    rows = await _customers(request, body.employee)
    if body.keys is not None:
        wanted = set(body.keys)
        rows = [r for r in rows if r.key in wanted]
    if not rows:
        raise HTTPException(404, detail=["There are no rows to download."])
    now = datetime.now(UK_TZ)
    about = [
        ("Employee", body.employee),
        ("Generated", now.strftime("%d %b %Y %H:%M %Z")),
        ("Rows", f"{len(rows):,} potential customers (registered company and owner found on Companies House)"),
        *ATTRIBUTION,
    ]
    return await asyncio.to_thread(_excel_response, rows, body.filename,
                                   f"{body.employee} - Potential customers - {now:%d %b %Y}.xlsx",
                                   about, body.parts, background, True)


# ---------- data updates ----------
@app.post("/api/updates/check")
async def check_updates(request: Request):
    """Ask each source whether newer data has been published (small requests only)."""
    state = request.app.state.updates
    await asyncio.gather(*(asyncio.to_thread(check, settings, ds) for ds in state if not ds.updating))
    return [d.status() for d in state]


@app.post("/api/updates/install", status_code=202)
async def install_updates(request: Request):
    """Install the updates found by the last check. Runs in the background; searches keep working."""
    state = request.app.state.updates
    if not any(d.needs_update() for d in state):
        raise HTTPException(409, detail=["Everything is already up to date. Check for updates first."])
    if any(d.updating for d in state):
        raise HTTPException(409, detail=["An update is already being installed."])
    task = asyncio.create_task(install_pending(state, request.app.state.update_callbacks,
                                               request.app.state.release_callbacks))
    request.app.state.install_task = task  # keep a reference so it is not garbage-collected
    return [d.status() for d in state]


# ---------- dashboard ----------
if settings.frontend_dist.exists():
    app.mount("/", StaticFiles(directory=settings.frontend_dist, html=True), name="dashboard")
