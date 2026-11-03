-- One row per computer (the app sends a hash of the Windows MachineGuid, never the raw id).
CREATE TABLE IF NOT EXISTS licenses (
    machine TEXT PRIMARY KEY,
    plan TEXT NOT NULL DEFAULT 'trial',          -- 'trial' or 'paid'
    revoked INTEGER NOT NULL DEFAULT 0,
    trial_started_at INTEGER NOT NULL,           -- unix seconds, set once, on first activation
    expires_at INTEGER,                          -- unix seconds; NULL = never (permanent paid licence)
    created_at INTEGER NOT NULL,
    last_seen_at INTEGER,
    last_version TEXT,
    checks INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    limits TEXT,                                 -- JSON row limits per download kind; missing = the defaults
    user_name TEXT NOT NULL DEFAULT '',          -- the name the user entered in Copilot (top right)
    computer TEXT NOT NULL DEFAULT '',           -- the computer's own name, e.g. DESKTOP-4KQ2
    os TEXT NOT NULL DEFAULT ''                  -- Windows / macOS
);
-- Every Excel download (and refused attempt) from the app.
CREATE TABLE IF NOT EXISTS downloads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    machine TEXT NOT NULL,
    at INTEGER NOT NULL,
    kind TEXT NOT NULL,                          -- 'search', 'sheet' or 'customers'
    rows INTEGER NOT NULL,                       -- rows in the file
    new_rows INTEGER NOT NULL,                   -- rows this PC had not downloaded before (count towards the limit)
    filename TEXT NOT NULL DEFAULT '',
    allowed INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_downloads_machine ON downloads(machine, at);
-- Which rows each PC has downloaded: a hash per row, never the business data itself.
CREATE TABLE IF NOT EXISTS download_keys (
    machine TEXT NOT NULL,
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    PRIMARY KEY (machine, kind, key)
) WITHOUT ROWID;
-- Every administrator change, for the record.
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at INTEGER NOT NULL,
    machine TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
