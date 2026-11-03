-- For databases created before download limits (3 Nov 2026). New databases get this from schema.sql.
ALTER TABLE licenses ADD COLUMN limits TEXT;
CREATE TABLE IF NOT EXISTS downloads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    machine TEXT NOT NULL,
    at INTEGER NOT NULL,
    kind TEXT NOT NULL,
    rows INTEGER NOT NULL,
    new_rows INTEGER NOT NULL,
    filename TEXT NOT NULL DEFAULT '',
    allowed INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_downloads_machine ON downloads(machine, at);
CREATE TABLE IF NOT EXISTS download_keys (
    machine TEXT NOT NULL,
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    PRIMARY KEY (machine, kind, key)
) WITHOUT ROWID;
