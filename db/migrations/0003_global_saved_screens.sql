-- Saved screens are private owner configuration, not a dataset snapshot.
-- Runs and matches retain dataset_id so historical results remain reproducible.
-- This unique-index preflight aborts before any table rewrite if legacy snapshots
-- reused a screen_id. The migration runner must execute each migration
-- transactionally; no conflicting owner configuration is silently discarded.
CREATE UNIQUE INDEX IF NOT EXISTS saved_screens_migration_unique_id ON saved_screens(screen_id);

CREATE TABLE IF NOT EXISTS saved_screens_v2 (
    screen_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    expression TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- The preflight above guarantees this insert is one-to-one. Any unexpected
-- conflict still aborts rather than silently losing an owner configuration.
INSERT INTO saved_screens_v2 (screen_id, name, expression, created_at, updated_at)
SELECT screen_id, name, expression, created_at, updated_at FROM saved_screens;

DROP TABLE IF EXISTS saved_screens;
ALTER TABLE saved_screens_v2 RENAME TO saved_screens;
