import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, BusinessRow, Job, Meta, PhoneFilter, SearchRequest } from "./api";
import { Combobox } from "./Combobox";
import { RecordsTable } from "./RecordsTable";
import { ConfirmDialog, DownloadDialog, fileSafe, today, when } from "./ui";

const ACTIVE = new Set(["queued", "running"]);
const PAGE = 100;
const POSTCODE = /^([A-Z]{1,2}[0-9][A-Z0-9]?)(\s*[0-9]([A-Z]{2})?)?$/i;
const STAGES = ["Searching VO rating list", "Normalising and de-duplicating", "Finding contact details", "Generating Excel file"];

const blank = { town: "", postcode: "", description_code: "", rv_from: "", rv_to: "" };
type Form = typeof blank;

function validate(f: Form): string[] {
  const e: string[] = [];
  if (!f.town.trim() && !f.postcode.trim()) e.push("Enter a town, a postcode, or both.");
  if (f.postcode.trim() && !POSTCODE.test(f.postcode.trim())) e.push("Postcode must look like CV1 1FJ, CV1 1 or CV1.");
  for (const [k, label] of [["rv_from", "Rateable value from"], ["rv_to", "Rateable value to"]] as const)
    if (f[k] !== "" && !/^\d+$/.test(f[k].replace(/[£,\s]/g, ""))) e.push(`${label} must be a whole number of pounds.`);
  return e;
}

const money = (s: string) => (s === "" ? null : Number(s.replace(/[£,\s]/g, "")));

export function SearchView({ meta, employee, toast, onSheetChanged }: {
  meta: Meta | null; employee: string; toast: (t: string, tone?: "good" | "bad") => void; onSheetChanged: (total: number) => void;
}) {
  const [form, setForm] = useState<Form>(blank);
  const [errors, setErrors] = useState<string[]>([]);
  const [job, setJob] = useState<Job | null>(null);
  const [history, setHistory] = useState<Job[]>([]);
  const [busy, setBusy] = useState(false);
  const [downloadOpen, setDownloadOpen] = useState(false);
  const [deleting, setDeleting] = useState<Job | null>(null);
  const timer = useRef<number>();

  const loadHistory = useCallback(() => {
    api.jobs(employee).then(setHistory).catch(() => undefined);
  }, [employee]);
  useEffect(loadHistory, [loadHistory]);
  useEffect(() => () => window.clearTimeout(timer.current), []);

  const poll = useCallback((id: string) => {
    window.clearTimeout(timer.current);
    const tick = async () => {
      try {
        const j = await api.job(id);
        setJob(j);
        if (ACTIVE.has(j.state)) timer.current = window.setTimeout(tick, 700);
        else loadHistory();
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) return;
        timer.current = window.setTimeout(tick, 2500);
      }
    };
    tick();
  }, [loadHistory]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    const errs = validate(form);
    setErrors(errs);
    if (errs.length) return;
    const req: SearchRequest = {
      town: form.town.trim(), postcode: form.postcode.trim(), description_code: form.description_code,
      rv_from: money(form.rv_from), rv_to: money(form.rv_to),
    };
    setBusy(true);
    try {
      const j = await api.create(req, employee);
      setJob(j);
      poll(j.id);
    } catch (err) {
      setErrors(err instanceof ApiError ? err.messages : ["Something went wrong."]);
    } finally {
      setBusy(false);
    }
  }

  async function addToSheet() {
    if (!job) return;
    try {
      const r = await api.sheetAdd(employee, job.id);
      onSheetChanged(r.total);
      const extra = r.already_in_sheet ? ` (${r.already_in_sheet} already there)` : "";
      toast(r.added ? `Added ${r.added} to your sheet${extra}. Your sheet now has ${r.total}.` : `All ${r.already_in_sheet} were already in your sheet.`, "good");
    } catch (err) {
      toast(err instanceof ApiError ? err.messages[0] : "Could not add to your sheet.", "bad");
    }
  }

  async function removeSearch(j: Job) {
    try {
      await api.deleteJob(j.id);
      if (job?.id === j.id) setJob(null);
      loadHistory();
      toast("Search deleted.");
    } catch (err) {
      toast(err instanceof ApiError ? err.messages[0] : "Could not delete the search.", "bad");
    }
  }

  const set = (k: keyof Form) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setForm({ ...form, [k]: e.target.value });
  const running = job !== null && ACTIVE.has(job.state);

  return (
    <div className="search-layout">
      <div className="search-main">
        <form className="survey" onSubmit={submit} noValidate aria-label="Search the rating list">
          <Combobox
            label="Town"
            value={form.town}
            onChange={(v) => setForm((f) => ({ ...f, town: v }))}
            load={api.suggestTowns}
            placeholder="Type or choose a town"
          />
          <Combobox
            label="Postcode"
            value={form.postcode}
            onChange={(v) => setForm((f) => ({ ...f, postcode: v.toUpperCase() }))}
            load={(q) => api.suggestPostcodes(form.town, q)}
            placeholder={form.town ? "Choose a postcode" : "e.g. CV1 1FJ"}
            onPick={(v) => !/^[A-Z]{1,2}\d[A-Z\d]?\s\d[A-Z]{2}$/.test(v)}
          />
          <label className="field field-wide">
            <span className="field-label">Description code</span>
            <select value={form.description_code} onChange={set("description_code")}>
              <option value="">Any</option>
              {meta?.description_codes.map((d) => <option key={d.code} value={d.code}>{d.label}</option>)}
            </select>
          </label>
          <label className="field">
            <span className="field-label">Rateable value from (£)</span>
            <input className="mono" inputMode="numeric" value={form.rv_from} onChange={set("rv_from")} placeholder="12,000" />
          </label>
          <label className="field">
            <span className="field-label">Rateable value to (£)</span>
            <input className="mono" inputMode="numeric" value={form.rv_to} onChange={set("rv_to")} placeholder="No limit" />
          </label>
          <button className="btn btn-primary survey-go" disabled={busy || running}>
            {running ? "Searching…" : "Search"}
          </button>
          {errors.length > 0 && (
            <ul className="form-errors" role="alert">{errors.map((m) => <li key={m}>{m}</li>)}</ul>
          )}
        </form>

        {job ? (
          <JobPanel
            job={job}
            onAdd={addToSheet}
            onDownload={() => setDownloadOpen(true)}
            onRetry={async () => { setJob(await api.retry(job.id)); poll(job.id); }}
          />
        ) : (
          <section className="intro">
            <h2 className="intro-title">Find businesses by rateable premises</h2>
            <p>
              Search exactly as on the VO Find-a-property site. Properties removed from the rating list are skipped.
              Every listed address is matched to the business there and its phone number.
            </p>
            <p className="muted">Add the results to <strong>My sheet</strong> to build one list across many searches.</p>
          </section>
        )}
      </div>

      <aside className="history" aria-label="Your recent searches">
        <h2 className="rail-title">Your searches</h2>
        {history.length === 0 && <p className="muted small">Searches you run appear here.</p>}
        <ul>
          {history.map((h) => (
            <li key={h.id} className={job?.id === h.id ? "current" : ""}>
              <button type="button" className="history-open" onClick={() => { setJob(h); if (ACTIVE.has(h.state)) poll(h.id); }}>
                <span className="history-label">{h.label || "Search"}</span>
                <span className="history-meta">
                  {when(h.created_at)} · <span className="mono">{h.summary.phones_found}</span> with phone
                </span>
              </button>
              <button type="button" className="icon-btn" aria-label={`Delete search ${h.label}`} title="Delete search"
                onClick={() => setDeleting(h)} disabled={ACTIVE.has(h.state)}>×</button>
            </li>
          ))}
        </ul>
      </aside>

      {job && (
        <DownloadDialog
          open={downloadOpen}
          defaultName={fileSafe(`${employee} - ${[job.request.town, job.request.postcode, job.request.description_code].filter(Boolean).join(" ").toUpperCase()} - ${today()}`)}
          summary={`${(job.summary.businesses || job.summary.phones_found).toLocaleString()} leads from ${job.label}.`}
          onDownload={(name) => api.downloadJob(job.id, name)}
          onClose={() => setDownloadOpen(false)}
        />
      )}
      <ConfirmDialog
        open={deleting !== null}
        title="Delete this search?"
        body={`“${deleting?.label}” and its results will be removed from your searches. Rows you already added to My sheet stay there.`}
        confirm="Delete search"
        onConfirm={() => deleting && removeSearch(deleting)}
        onClose={() => setDeleting(null)}
      />
    </div>
  );
}

function JobPanel({ job, onAdd, onDownload, onRetry }: {
  job: Job; onAdd: () => void; onDownload: () => void; onRetry: () => void;
}) {
  const active = ACTIVE.has(job.state);
  const s = job.summary;
  const stage = Math.max(0, STAGES.indexOf(job.stage));
  return (
    <section className="job" aria-live="polite">
      <header className="job-head">
        <div>
          <h2 className="job-title">{job.label}</h2>
          {!active && s.source_records > 0 && (
            <p className="tally">
              <span className="mono">{s.source_records.toLocaleString()}</span> listed properties
              <span className="sep">/</span>
              <span className="mono good">{(s.businesses || s.phones_found).toLocaleString()}</span> leads
              {job.duration_seconds != null && <span className="muted"> · {job.duration_seconds.toFixed(1)}s</span>}
            </p>
          )}
        </div>
        {!active && s.phones_found > 0 && (
          <div className="job-actions">
            <button type="button" className="btn btn-primary" onClick={onAdd}>Add {s.businesses || s.phones_found} to My sheet</button>
            <button type="button" className="btn" onClick={onDownload}>Download Excel</button>
          </div>
        )}
      </header>

      {active ? (
        <div className="progress">
          <ol className="stages">
            {STAGES.map((st, i) => <li key={st} className={i < stage ? "done" : i === stage ? "now" : ""}>{st}</li>)}
          </ol>
          <div className="bar"><div style={{ width: `${Math.max(4, Math.round(job.progress * 100))}%` }} /></div>
          <p className="muted small">{job.message || job.stage}</p>
        </div>
      ) : (
        (job.state === "no_results" || job.state === "failed") && <p className={`notice ${job.state}`}>{job.message}</p>
      )}
      {!active && (job.state === "failed" || s.lookup_failed > 0) && (
        <button type="button" className="btn" onClick={onRetry}>Retry the failed part</button>
      )}

      {!active && s.source_records > 0 && <JobResults jobId={job.id} />}
    </section>
  );
}

function JobResults({ jobId }: { jobId: string }) {
  const [filter, setFilter] = useState<PhoneFilter>("with");
  const [page, setPage] = useState(0);
  const [data, setData] = useState<{ total: number; items: BusinessRow[] } | null>(null);
  useEffect(() => setPage(0), [filter, jobId]);
  useEffect(() => {
    api.records(jobId, filter, page * PAGE, PAGE).then(setData).catch(() => setData({ total: 0, items: [] }));
  }, [jobId, filter, page]);
  const pages = data ? Math.ceil(data.total / PAGE) : 0;
  const tabs: [PhoneFilter, string][] = [["with", "Leads"], ["without", "No contact found"], ["", "All"]];
  return (
    <>
      <div className="segmented" role="tablist" aria-label="Show">
        {tabs.map(([f, label]) => (
          <button key={label} role="tab" aria-selected={filter === f} onClick={() => setFilter(f)}>{label}</button>
        ))}
      </div>
      <RecordsTable items={data?.items ?? []} empty="Nothing here." />
      {pages > 1 && (
        <div className="pager">
          <button className="btn btn-quiet" disabled={page === 0} onClick={() => setPage(page - 1)}>Previous</button>
          <span className="mono small">{page + 1} / {pages}</span>
          <button className="btn btn-quiet" disabled={page + 1 >= pages} onClick={() => setPage(page + 1)}>Next</button>
        </div>
      )}
    </>
  );
}
