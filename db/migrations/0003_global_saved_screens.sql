-- Saved screens are private owner configuration, not a dataset snapshot.
-- Runs and matches retain dataset_id so historical results remain reproducible.
CREATE TABLE IF NOT EXISTS saved_screens_v2 (
    screen_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    expression TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

INSERT OR IGNORE INTO saved_screens_v2 (screen_id, name, expression, created_at, updated_at)
SELECT screen_id, name, expression, created_at, updated_at FROM saved_screens;

DROP TABLE IF EXISTS saved_screens;
ALTER TABLE saved_screens_v2 RENAME TO saved_screens;
