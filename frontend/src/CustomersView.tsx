import { useCallback, useEffect, useMemo, useState } from "react";
import { api, ApiError, BusinessRow, Person } from "./api";
import { Phone, RateableValues, SharedTag } from "./RecordsTable";
import { ConfirmDialog, DownloadDialog, fileSafe, today } from "./ui";

/** Businesses whose registered company and owner were found on Companies House. */
export function CustomersView({ employee, toast, onChanged, onGoSheet }: {
  employee: string; toast: (t: string, tone?: "good" | "bad") => void;
  onChanged: (total: number) => void; onGoSheet: () => void;
}) {
  const [rows, setRows] = useState<BusinessRow[] | null>(null);
  const [text, setText] = useState("");
  const [downloadOpen, setDownloadOpen] = useState(false);
  const [clearOpen, setClearOpen] = useState(false);

  const load = useCallback(() => {
    api.customers(employee)
      .then((d) => { setRows(d.items); onChanged(d.total); })
      .catch(() => setRows([]));
  }, [employee, onChanged]);
  useEffect(load, [load]);

  const shown = useMemo(() => {
    const t = text.trim().toLowerCase();
    return (rows ?? []).filter((r) => !t ||
      `${r.business_name} ${r.company?.owner_names} ${r.company?.company_name} ${r.business_address} ${r.phone}`.toLowerCase().includes(t));
  }, [rows, text]);
  const filtered = rows !== null && shown.length !== rows.length;

  async function remove(row: BusinessRow) {
    try {
      const r = await api.customersRemove(employee, [row.key]);
      setRows((xs) => (xs ?? []).filter((x) => x.key !== row.key));
      onChanged(r.total);
    } catch (err) {
      toast(err instanceof ApiError ? err.messages[0] : "Could not remove the row.", "bad");
    }
  }

  async function clear() {
    try {
      await api.customersClear(employee);
      setRows([]);
      onChanged(0);
    } catch (err) {
      toast(err instanceof ApiError ? err.messages[0] : "Could not clear the list.", "bad");
    }
  }

  if (rows === null) return <p className="muted loading">Loading…</p>;

  return (
    <section className="sheet">
      <header className="sheet-head">
        <div>
          <h2 className="sheet-title">Potential customers</h2>
          <p className="tally"><span className="mono">{rows.length.toLocaleString()}</span> businesses with a named owner</p>
        </div>
        {rows.length > 0 && (
          <div className="job-actions">
            <button type="button" className="btn btn-primary" onClick={() => setDownloadOpen(true)}>
              {filtered ? `Download ${shown.length} shown` : `Download all ${rows.length}`}
            </button>
            <button type="button" className="btn btn-quiet" onClick={() => setClearOpen(true)}>Clear list</button>
          </div>
        )}
      </header>

      {rows.length === 0 ? (
        <div className="empty-sheet">
          <p>No potential customers yet.</p>
          <p className="muted">
            In <strong>My sheet</strong>, choose <strong>Get business owner names</strong>. Each business is looked up on
            Companies House, and the ones whose registered postcode matches are added here with the owner’s name.
          </p>
          <button type="button" className="btn btn-primary" onClick={onGoSheet}>Go to My sheet</button>
        </div>
      ) : (
        <>
          <div className="filters filters-one">
            <label className="field">
              <span className="field-label">Find</span>
              <input value={text} onChange={(e) => setText(e.target.value)} placeholder="Business, owner, company, phone…" />
            </label>
          </div>
          <CustomersTable items={shown} onRemove={remove} />
        </>
      )}

      <DownloadDialog
        open={downloadOpen}
        defaultName={fileSafe(`${employee} - Potential customers - ${today()}`)}
        rows={shown.length}
        allowSplit
        summary={filtered ? `${shown.length} of ${rows.length} potential customers.` : `All ${rows.length} potential customers, with owner names.`}
        onDownload={(name, parts) => api.customersDownload(employee, name, filtered ? shown.map((r) => r.key) : null, parts)}
        onClose={() => setDownloadOpen(false)}
      />
      <ConfirmDialog
        open={clearOpen}
        title="Clear Potential customers?"
        body={`All ${rows.length} rows will be removed from this list. Your sheet is not affected.`}
        confirm="Clear list"
        onConfirm={clear}
        onClose={() => setClearOpen(false)}
      />
    </section>
  );
}

/** "Mr Timothy Godsmark" (owner) and "Timothy John Godsmark" (director) are the same person. */
function samePerson(a: string, b: string): boolean {
  const parts = (n: string) => n.toLowerCase().replace(/^(mr|mrs|ms|miss|dr)\.?\s+/, "").split(/\s+/);
  const [x, y] = [parts(a), parts(b)];
  return x[0] === y[0] && x[x.length - 1] === y[y.length - 1];
}

/** "Ownership of shares – 75% or more" -> "75% or more of shares"; keeps the list short. */
function controlText(p: Person): string {
  const shares = p.control.find((c) => c.startsWith("Ownership of shares"));
  if (shares) return shares.replace("Ownership of shares – ", "") + " of shares";
  return p.control[0] ?? "";
}

function PersonLine({ p, kind }: { p: Person; kind: "owner" | "director" }) {
  const detail = kind === "owner"
    ? [controlText(p), p.since && `since ${p.since}`]
    : [p.role, p.since && `since ${p.since}`, p.occupation];
  const text = detail.filter(Boolean).join(" · ");
  return (
    <li>
      <span className="person">{p.name}</span>
      {text && <span className="sub person-detail">{text}</span>}
    </li>
  );
}

function CustomersTable({ items, onRemove }: { items: BusinessRow[]; onRemove: (row: BusinessRow) => void }) {
  return (
    <div className="table-wrap">
      <table className="records customers">
        <thead>
          <tr>
            <th>Business</th>
            <th>Owner / people</th>
            <th>Company</th>
            <th>Phone</th>
            <th>Address</th>
            <th className="num">Rateable value</th>
            <th><span className="sr-only">Remove</span></th>
          </tr>
        </thead>
        <tbody>
          {items.map((r) => {
            const c = r.company;
            // Active officers (directors, partners ...) are who to ask for; owners add the shareholding.
            const people = c?.directors ?? [];
            const owners = (c?.owners ?? []).filter((o) => !people.some((d) => samePerson(o.name, d.name)));
            return (
              <tr key={r.key}>
                <td data-label="Business">
                  <div className="biz">{r.business_name}</div>
                  <div className="sub">{r.description}</div>
                  <SharedTag row={r} />
                  {r.shared_building > 0 && <div className="sub">Rateable values not confirmed as theirs</div>}
                </td>
                <td data-label="Owner / people">
                  {people.length > 0 && (
                    <ul className="people">
                      {people.map((p) => <PersonLine key={p.name} p={p} kind="director" />)}
                    </ul>
                  )}
                  {owners.length > 0 && (
                    <>
                      {people.length > 0 && <div className="people-label">Owned by</div>}
                      <ul className="people">
                        {owners.map((p) => <PersonLine key={p.name} p={p} kind="owner" />)}
                      </ul>
                    </>
                  )}
                  {!people.length && !owners.length && <span className="dash">—</span>}
                </td>
                <td data-label="Company">
                  {c && (
                    <>
                      <a className="company-name" href={c.url} target="_blank" rel="noreferrer">{c.company_name} ↗</a>
                      <div className="sub">
                        {[`No. ${c.company_number}`, c.status, c.incorporated && `since ${c.incorporated}`].filter(Boolean).join(" · ")}
                      </div>
                      {c.nature_of_business.length > 0 && (
                        <div className="sub">{c.nature_of_business.map((n) => n.replace(/^\d+ - /, "")).join("; ")}</div>
                      )}
                      <div className="sub" title="Registered office">Reg. office: {c.registered_address}</div>
                    </>
                  )}
                </td>
                <td data-label="Phone"><Phone value={r.phone} /></td>
                <td data-label="Address">
                  <div>{r.business_address || r.address}</div>
                  <a href={r.google_maps_url} target="_blank" rel="noreferrer" className="sub chip-link">Google Maps ↗</a>
                </td>
                <td data-label="Rateable value" className="num"><RateableValues row={r} /></td>
                <td className="remove-cell">
                  <button type="button" className="icon-btn" onClick={() => onRemove(r)} aria-label={`Remove ${r.business_name}`} title="Remove">×</button>
                </td>
              </tr>
            );
          })}
          {items.length === 0 && <tr className="empty-row"><td colSpan={7}>No rows match.</td></tr>}
        </tbody>
      </table>
    </div>
  );
}
