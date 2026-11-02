import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError, BusinessRow, OwnerRun } from "./api";
import { RecordsTable } from "./RecordsTable";
import { ConfirmDialog, DownloadDialog, fileSafe, today } from "./ui";

export function SheetView({ employee, toast, onSheetChanged, onCustomersChanged, onGoSearch, onGoCustomers }: {
  employee: string; toast: (t: string, tone?: "good" | "bad") => void;
  onSheetChanged: (total: number) => void; onCustomersChanged: () => void;
  onGoSearch: () => void; onGoCustomers: () => void;
}) {
  const [rows, setRows] = useState<BusinessRow[] | null>(null);
  const [run, setRun] = useState<OwnerRun | null>(null);
  const [text, setText] = useState("");
  const [desc, setDesc] = useState("");
  const [search, setSearch] = useState("");
  const [downloadOpen, setDownloadOpen] = useState(false);
  const [clearOpen, setClearOpen] = useState(false);

  const load = useCallback(() => {
    api.sheet(employee)
      .then((d) => { setRows(d.items); setRun(d.owners); onSheetChanged(d.total); })
      .catch(() => setRows([]));
  }, [employee, onSheetChanged]);
  useEffect(load, [load]);

  // While the owner search runs, follow its progress; reload once it finishes.
  const notify = useRef({ toast, onCustomersChanged });
  notify.current = { toast, onCustomersChanged };
  useEffect(() => {
    if (!run?.running) return;
    const t = setInterval(() => {
      api.ownersStatus(employee).then((s) => {
        setRun(s);
        if (!s.running) {
          load();
          notify.current.onCustomersChanged();
          notify.current.toast(s.message, s.found ? "good" : undefined);
        }
      }).catch(() => undefined);
    }, 2000);
    return () => clearInterval(t);
  }, [run?.running, employee, load]);

  const descriptions = useMemo(() => [...new Set((rows ?? []).map((r) => r.description))].sort(), [rows]);
  const searches = useMemo(() => [...new Set((rows ?? []).map((r) => r.search))], [rows]);
  const shown = useMemo(() => {
    const t = text.trim().toLowerCase();
    return (rows ?? []).filter((r) =>
      (!desc || r.description === desc) && (!search || r.search === search) &&
      (!t || `${r.business_name} ${r.business_address} ${r.address} ${r.phone} ${r.postcode}`.toLowerCase().includes(t)));
  }, [rows, text, desc, search]);
  const filtered = rows !== null && shown.length !== rows.length;
  const unchecked = shown.filter((r) => !r.owner_checked && r.business_name).length;

  async function remove(row: BusinessRow) {
    try {
      const r = await api.sheetRemove(employee, row.uarns);
      setRows((xs) => (xs ?? []).filter((x) => x.key !== row.key));
      onSheetChanged(r.total);
    } catch (err) {
      toast(err instanceof ApiError ? err.messages[0] : "Could not remove the row.", "bad");
    }
  }

  async function clear() {
    try {
      await api.sheetClear(employee);
      setRows([]);
      onSheetChanged(0);
      toast("Your sheet is now empty.");
    } catch (err) {
      toast(err instanceof ApiError ? err.messages[0] : "Could not clear the sheet.", "bad");
    }
  }

  async function findOwners() {
    try {
      setRun(await api.ownersRun(employee, filtered ? shown.flatMap((r) => r.uarns) : null));
    } catch (err) {
      toast(err instanceof ApiError ? err.messages[0] : "Could not start the owner search.", "bad");
    }
  }

  if (rows === null) return <p className="muted loading">Loading your sheet…</p>;

  return (
    <section className="sheet">
      <header className="sheet-head">
        <div>
          <h2 className="sheet-title">My sheet</h2>
          <p className="tally">
            <span className="mono">{rows.length.toLocaleString()}</span> rows
            <span className="sep">/</span>
            <span className="mono">{searches.length}</span> {searches.length === 1 ? "search" : "searches"}
          </p>
        </div>
        {rows.length > 0 && (
          <div className="job-actions">
            <button type="button" className="btn btn-primary" onClick={() => setDownloadOpen(true)}>
              {filtered ? `Download ${shown.length} shown` : `Download all ${rows.length}`}
            </button>
            <button type="button" className="btn" onClick={findOwners} disabled={run?.running}
              title="Look up each business on Companies House; matches go to Potential customers">
              Get business owner names
            </button>
            <button type="button" className="btn btn-quiet" onClick={() => setClearOpen(true)}>Clear sheet</button>
          </div>
        )}
      </header>

      {run?.running && (
        <div className="progress">
          <p className="progress-title">Finding owners on Companies House</p>
          <div className="bar"><div style={{ width: `${Math.max(3, Math.round((run.done / Math.max(run.total, 1)) * 100))}%` }} /></div>
          <p className="muted small">{run.message} · you can keep working, it runs in the background.</p>
        </div>
      )}
      {!run?.running && run?.message && run.total > 0 && (
        <p className={run.blocked ? "notice" : "notice done-notice"}>
          {run.message}{" "}
          {run.found > 0 && <button type="button" className="link-btn" onClick={onGoCustomers}>Open Potential customers</button>}
        </p>
      )}

      {rows.length === 0 ? (
        <div className="empty-sheet">
          <p>Your sheet is empty.</p>
          <p className="muted">Run a search, then choose <strong>Add to My sheet</strong>. Each search you add is appended here.</p>
          <button type="button" className="btn btn-primary" onClick={onGoSearch}>Start a search</button>
        </div>
      ) : (
        <>
          <div className="filters">
            <label className="field">
              <span className="field-label">Find</span>
              <input value={text} onChange={(e) => setText(e.target.value)} placeholder="Name, address, phone…" />
            </label>
            <label className="field">
              <span className="field-label">Description</span>
              <select value={desc} onChange={(e) => setDesc(e.target.value)}>
                <option value="">All descriptions</option>
                {descriptions.map((d) => <option key={d}>{d}</option>)}
              </select>
            </label>
            <label className="field">
              <span className="field-label">Added from</span>
              <select value={search} onChange={(e) => setSearch(e.target.value)}>
                <option value="">All searches</option>
                {searches.map((s) => <option key={s}>{s}</option>)}
              </select>
            </label>
            {filtered && (
              <button type="button" className="btn btn-quiet reset" onClick={() => { setText(""); setDesc(""); setSearch(""); }}>
                Show all
              </button>
            )}
          </div>
          <p className="muted small shown-line">
            Showing <span className="mono">{shown.length}</span> of <span className="mono">{rows.length}</span>
            {unchecked > 0 && <> · <span className="mono">{unchecked}</span> not checked for owners yet</>}
          </p>
          <RecordsTable items={shown} onRemove={remove} empty="No rows match these filters." />
        </>
      )}

      <DownloadDialog
        open={downloadOpen}
        defaultName={fileSafe(`${employee} - My sheet - ${today()}`)}
        rows={shown.length}
        allowSplit
        summary={filtered
          ? `${shown.length} of ${rows.length} rows (only the ones matching your filters).`
          : `All ${rows.length} rows in your sheet.`}
        onDownload={(name, parts) => api.sheetDownload(employee, name, filtered ? shown.flatMap((r) => r.uarns) : null, parts)}
        onClose={() => setDownloadOpen(false)}
      />
      <ConfirmDialog
        open={clearOpen}
        title="Clear your sheet?"
        body={`All ${rows.length} rows will be removed from ${employee}’s sheet. Download it first if you need a copy. Potential customers are kept.`}
        confirm="Clear sheet"
        onConfirm={clear}
        onClose={() => setClearOpen(false)}
      />
    </section>
  );
}
