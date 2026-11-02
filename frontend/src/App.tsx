import { useCallback, useEffect, useState } from "react";
import { api, Meta } from "./api";
import { SearchView } from "./SearchView";
import { CustomersView } from "./CustomersView";
import { SheetView } from "./SheetView";
import { looksStuck, NameDialog, progressText, storage, Toasts, UpdatesDialog, useToasts, when } from "./ui";

type Tab = "search" | "sheet" | "customers";
const TABS: Tab[] = ["search", "sheet", "customers"];

function updateNote(meta: Meta | null): string {
  const u = meta?.data_updates ?? [];
  const busy = u.find((d) => d.updating);
  if (busy) {
    const p = busy.progress;
    if (p && looksStuck(p)) return `${busy.name}: no progress for a while — open Check for updates for details`;
    return `Installing ${busy.name}${p ? `: ${progressText(p)}` : ""}`;
  }
  if (u.some((d) => d.update_pending)) return "New data available — install it now or it installs tonight";
  const checked = u.map((d) => d.last_checked).filter(Boolean).sort().pop();
  return checked ? `Checked for new data ${when(checked)}` : "Checks for new data daily";
}

export default function App() {
  const [meta, setMeta] = useState<Meta | null>(null);
  const [employee, setEmployee] = useState(() => storage.get("ukbf.employee"));
  const [renaming, setRenaming] = useState(false);
  const [tab, setTab] = useState<Tab>(() => {
    const saved = storage.get("ukbf.tab") as Tab;
    return TABS.includes(saved) ? saved : "search";
  });
  const [sheetCount, setSheetCount] = useState<number | null>(null);
  const [customerCount, setCustomerCount] = useState<number | null>(null);
  const [updatesOpen, setUpdatesOpen] = useState(false);
  const toasts = useToasts();

  const loadMeta = useCallback(() => api.meta().then(setMeta).catch(() => setMeta(null)), []);
  useEffect(() => { loadMeta(); }, [loadMeta]);
  // While new data is being installed, refresh the footer so it shows when it is done.
  const installing = meta?.data_updates.some((d) => d.updating) ?? false;
  useEffect(() => {
    if (!installing) return;
    const t = setInterval(loadMeta, 3000);  // live progress in the footer
    return () => clearInterval(t);
  }, [installing, loadMeta]);

  const loadCustomerCount = useCallback(() => {
    if (employee) api.customers(employee).then((d) => setCustomerCount(d.total)).catch(() => undefined);
  }, [employee]);
  useEffect(() => {
    if (employee) api.sheet(employee).then((d) => setSheetCount(d.total)).catch(() => undefined);
    loadCustomerCount();
  }, [employee, loadCustomerCount]);
  useEffect(() => storage.set("ukbf.tab", tab), [tab]);

  async function installUpdates() {
    await api.installUpdates();
    toasts.push("Installing new data in the background. Searches keep working.", "good");
    await loadMeta();
  }

  const saveName = (name: string) => {
    storage.set("ukbf.employee", name);
    setEmployee(name);
    setRenaming(false);
  };
  const onSheetChanged = useCallback((n: number) => setSheetCount(n), []);
  const onCustomersChanged = useCallback((n: number) => setCustomerCount(n), []);

  return (
    <div className="app">
      <header className="masthead">
        <div className="masthead-inner">
          <div className="brand">
            <span className="brand-mark" aria-hidden="true" />
            <span className="brand-name">Copilot</span>
          </div>
          <nav className="tabs" aria-label="Sections">
            <button type="button" className={tab === "search" ? "on" : ""} aria-current={tab === "search"} onClick={() => setTab("search")}>
              Search
            </button>
            <button type="button" className={tab === "sheet" ? "on" : ""} aria-current={tab === "sheet"} onClick={() => setTab("sheet")}>
              My sheet{sheetCount != null && <span className="count mono">{sheetCount}</span>}
            </button>
            <button type="button" className={tab === "customers" ? "on" : ""} aria-current={tab === "customers"} onClick={() => setTab("customers")}>
              Potential customers{customerCount != null && <span className="count mono">{customerCount}</span>}
            </button>
          </nav>
          {employee && (
            <button type="button" className="who" onClick={() => setRenaming(true)} title="Change name">
              <span className="who-initial" aria-hidden="true">{employee.slice(0, 1).toUpperCase()}</span>
              <span className="who-name">{employee}</span>
            </button>
          )}
        </div>
      </header>

      <main className="page">
        {!employee ? null : tab === "search" ? (
          <SearchView meta={meta} employee={employee} toast={toasts.push} onSheetChanged={onSheetChanged} />
        ) : tab === "sheet" ? (
          <SheetView employee={employee} toast={toasts.push} onSheetChanged={onSheetChanged}
            onCustomersChanged={loadCustomerCount} onGoSearch={() => setTab("search")} onGoCustomers={() => setTab("customers")} />
        ) : (
          <CustomersView employee={employee} toast={toasts.push} onChanged={onCustomersChanged} onGoSheet={() => setTab("sheet")} />
        )}
      </main>

      <footer className="foot">
        {meta?.voa_dataset ? (
          <span>
            Rating list ({meta.voa_dataset.list_year}): {Number(meta.voa_dataset.row_count).toLocaleString()} properties,
            updated {when(meta.voa_dataset.imported_at)}
            {meta.places_dataset ? (
              <> · Business listings: release {meta.places_dataset.release}, updated {when(meta.places_dataset.imported_at)}</>
            ) : (
              <strong className="warn"> · phone data not loaded yet</strong>
            )}
            {meta.companies_dataset
              ? <> · Companies House: {meta.companies_dataset.release}</>
              : <> · Companies House data: not loaded yet</>}
          </span>
        ) : (
          <span className="warn">The rating list hasn’t been loaded on this server yet.</span>
        )}
        <span className="foot-updates">
          {updateNote(meta)}
          <button type="button" className="link-btn" onClick={() => setUpdatesOpen(true)}>Check for updates</button>
        </span>
      </footer>

      <UpdatesDialog open={updatesOpen} onInstall={installUpdates} onClose={() => { setUpdatesOpen(false); loadMeta(); }} />

      <NameDialog open={!employee || renaming} initial={employee} onSave={saveName} onClose={employee ? () => setRenaming(false) : undefined} />
      <Toasts items={toasts.items} />
    </div>
  );
}
