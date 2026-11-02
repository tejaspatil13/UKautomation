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
    note TEXT NOT NULL DEFAULT ''
);
-- Every administrator change, for the record.
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at INTEGER NOT NULL,
    machine TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
