import { FormEvent, ReactNode, useEffect, useRef, useState } from "react";
import { api, ApiError, DataUpdate } from "./api";

export const gbp = (n: number) => "£" + n.toLocaleString("en-GB");
export const when = (iso: string) =>
  new Date(iso).toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
export const today = () => new Date().toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });

/** Filesystem-friendly version of a label for a default file name. */
export const fileSafe = (s: string) => s.replace(/[^A-Za-z0-9 ._()&'-]+/g, " ").replace(/\s+/g, " ").trim();

export const storage = {
  get(key: string): string {
    try {
      return localStorage.getItem(key) ?? "";
    } catch {
      return "";
    }
  },
  set(key: string, value: string) {
    try {
      localStorage.setItem(key, value);
    } catch {
      /* private mode: keep working without persistence */
    }
  },
};

/** Modal built on the native <dialog>: focus trapping, Esc to close, backdrop. */
function Dialog({ open, onClose, title, children }: { open: boolean; onClose?: () => void; title: string; children: ReactNode }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) d.showModal();
    if (!open && d.open) d.close();
  }, [open]);
  return (
    <dialog
      ref={ref}
      className="dialog"
      aria-label={title}
      onCancel={(e) => {
        if (!onClose) e.preventDefault();
        else onClose();
      }}
    >
      <h2 className="dialog-title">{title}</h2>
      {children}
    </dialog>
  );
}

export function NameDialog({ open, initial, onSave, onClose }: {
  open: boolean; initial: string; onSave: (name: string) => void; onClose?: () => void;
}) {
  const [name, setName] = useState(initial);
  const [error, setError] = useState("");
  useEffect(() => { if (open) { setName(initial); setError(""); } }, [open, initial]);

  function submit(e: FormEvent) {
    e.preventDefault();
    const n = name.trim().replace(/\s+/g, " ");
    if (!n) return setError("Enter your name.");
    if (n.length > 60 || !/^[\p{L}\p{N} '.-]+$/u.test(n)) return setError("Use letters, numbers, spaces, hyphens or apostrophes (up to 60).");
    onSave(n);
  }

  return (
    <Dialog open={open} onClose={onClose} title={initial ? "Change name" : "Who's working today?"}>
      <form onSubmit={submit} noValidate>
        <p className="dialog-copy">
          Your name labels your searches and your sheet, and becomes the default name for your downloads.
        </p>
        <label className="field">
          <span className="field-label">Your name</span>
          <input autoFocus value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Sara Khan" maxLength={60} />
        </label>
        {error && <p className="inline-error" role="alert">{error}</p>}
        <div className="dialog-actions">
          {onClose && <button type="button" className="btn btn-quiet" onClick={onClose}>Cancel</button>}
          <button className="btn btn-primary">{initial ? "Save name" : "Start"}</button>
        </div>
      </form>
    </Dialog>
  );
}

export function DownloadDialog({ open, defaultName, summary, rows, allowSplit = false, onDownload, onClose }: {
  open: boolean; defaultName: string; summary: string; rows?: number; allowSplit?: boolean;
  onDownload: (filename: string, parts: number) => Promise<void>; onClose: () => void;
}) {
  const [name, setName] = useState(defaultName);
  const [parts, setParts] = useState("1");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => { if (open) { setName(defaultName); setParts("1"); setError(""); setBusy(false); } }, [open, defaultName]);

  const n = Math.max(1, Math.min(Number.parseInt(parts, 10) || 1, 100, rows ?? 100));
  const each = rows ? Math.ceil(rows / n) : 0;

  async function submit(e: FormEvent) {
    e.preventDefault();
    const file = fileSafe(name.replace(/\.(xlsx|zip)$/i, ""));
    if (!file) return setError("Enter a file name.");
    setBusy(true);
    try {
      await onDownload(file, allowSplit ? n : 1);
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Download failed.");
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onClose={onClose} title="Download Excel">
      <form onSubmit={submit} noValidate>
        <p className="dialog-copy">{summary}</p>
        <label className="field">
          <span className="field-label">File name</span>
          <span className="suffix-input">
            <input autoFocus value={name} onChange={(e) => setName(e.target.value)} maxLength={80}
              onFocus={(e) => e.currentTarget.select()} />
            <span className="suffix">{allowSplit && n > 1 ? ".zip" : ".xlsx"}</span>
          </span>
        </label>
        {allowSplit && (
          <>
            <label className="field split-field">
              <span className="field-label">Split into</span>
              <span className="split-row">
                <input type="number" min={1} max={100} value={parts} onChange={(e) => setParts(e.target.value)} />
                <span className="muted small">{n === 1 ? "one file" : `${n} files, about ${each.toLocaleString()} rows each`}</span>
              </span>
            </label>
            <div className="split-presets" role="group" aria-label="Quick split">
              {[1, 5, 10, 15].map((k) => (
                <button key={k} type="button" className={`btn btn-small ${n === k ? "on" : ""}`} onClick={() => setParts(String(k))}>
                  {k === 1 ? "No split" : k}
                </button>
              ))}
            </div>
          </>
        )}
        {error && <p className="inline-error" role="alert">{error}</p>}
        <div className="dialog-actions">
          <button type="button" className="btn btn-quiet" onClick={onClose}>Cancel</button>
          <button className="btn btn-primary" disabled={busy}>{busy ? "Preparing…" : n > 1 && allowSplit ? `Download ${n} files` : "Download"}</button>
        </div>
      </form>
    </Dialog>
  );
}

export function UpdatesDialog({ open, onInstall, onClose }: {
  open: boolean; onInstall: () => Promise<void>; onClose: () => void;
}) {
  const [items, setItems] = useState<DataUpdate[] | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!open) return;
    setItems(null); setError(""); setBusy(false);
    api.checkUpdates().then(setItems).catch((err) => setError(err instanceof ApiError ? err.messages[0] : "Could not check."));
  }, [open]);

  const pending = (items ?? []).filter((d) => d.update_pending && !d.updating);
  const updating = (items ?? []).some((d) => d.updating);

  async function install() {
    setBusy(true);
    try {
      await onInstall();
      onClose();
    } catch (err) {
      setError(err instanceof ApiError ? err.messages[0] : "Could not start the update.");
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onClose={onClose} title="Data updates">
      {!items && !error && <p className="dialog-copy">Checking the government site and the map data for new versions…</p>}
      {items && (
        <ul className="update-list">
          {items.map((d) => (
            <li key={d.name}>
              <span className="update-name">{d.name}</span>
              <span className={d.update_pending ? "update-new" : d.last_error ? "warn" : "update-ok"}>
                {d.updating ? "Installing now…"
                  : d.last_error ? d.last_error
                  : d.update_pending ? (d.installed ? `New version ${d.available}` : "Not installed yet")
                  : "Up to date"}
              </span>
            </li>
          ))}
        </ul>
      )}
      {items && pending.length > 0 && !updating && (
        <p className="dialog-copy">
          Install now? The new data downloads in the background and replaces the old when it’s ready.
          Searches keep working. On a slow connection this can take an hour or more.
        </p>
      )}
      {error && <p className="inline-error" role="alert">{error}</p>}
      <div className="dialog-actions">
        <button type="button" className="btn btn-quiet" onClick={onClose}>{pending.length && !updating ? "Not now" : "Close"}</button>
        {pending.length > 0 && !updating && (
          <button type="button" className="btn btn-primary" disabled={busy} onClick={install}>
            {busy ? "Starting…" : `Install ${pending.length === 1 ? "update" : `${pending.length} updates`}`}
          </button>
        )}
      </div>
    </Dialog>
  );
}

export function ConfirmDialog({ open, title, body, confirm, onConfirm, onClose }: {
  open: boolean; title: string; body: string; confirm: string; onConfirm: () => void; onClose: () => void;
}) {
  return (
    <Dialog open={open} onClose={onClose} title={title}>
      <p className="dialog-copy">{body}</p>
      <div className="dialog-actions">
        <button type="button" className="btn btn-quiet" onClick={onClose}>Cancel</button>
        <button type="button" className="btn btn-danger" onClick={() => { onConfirm(); onClose(); }}>{confirm}</button>
      </div>
    </Dialog>
  );
}

export type ToastMsg = { id: number; text: string; tone?: "good" | "bad" };

export function Toasts({ items }: { items: ToastMsg[] }) {
  return (
    <div className="toasts" role="status" aria-live="polite">
      {items.map((t) => (
        <div key={t.id} className={`toast ${t.tone ?? ""}`}>{t.text}</div>
      ))}
    </div>
  );
}

let toastId = 0;
export function useToasts() {
  const [items, setItems] = useState<ToastMsg[]>([]);
  const push = (text: string, tone?: ToastMsg["tone"]) => {
    const id = ++toastId;
    setItems((xs) => [...xs, { id, text, tone }]);
    setTimeout(() => setItems((xs) => xs.filter((x) => x.id !== id)), 4200);
  };
  return { items, push };
}
