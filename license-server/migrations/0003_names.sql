-- For databases created before PCs reported their user's name (3 Nov 2026). New databases get this from schema.sql.
ALTER TABLE licenses ADD COLUMN user_name TEXT NOT NULL DEFAULT '';
ALTER TABLE licenses ADD COLUMN computer TEXT NOT NULL DEFAULT '';
ALTER TABLE licenses ADD COLUMN os TEXT NOT NULL DEFAULT '';
