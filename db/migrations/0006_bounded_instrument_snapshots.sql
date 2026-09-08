-- Retain only the active serving projection. Historical screen outcomes live
-- in screen_runs/screen_matches and historical charts in R2, so keeping every
-- prior full metric snapshot would exhaust D1 Free storage.
CREATE TABLE IF NOT EXISTS instrument_snapshots_v2 (
    instrument_id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id),
    symbol TEXT,
    name TEXT,
    asset_class TEXT NOT NULL,
    active INTEGER NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    metric_values_json TEXT NOT NULL DEFAULT '{}',
    metric_rows_json TEXT NOT NULL DEFAULT '[]',
    aliases_json TEXT NOT NULL DEFAULT '[]',
    fundamental_periods_json TEXT NOT NULL DEFAULT '[]',
    corporate_actions_json TEXT NOT NULL DEFAULT '[]'
);

INSERT OR REPLACE INTO instrument_snapshots_v2
SELECT s.instrument_id, s.dataset_id, s.symbol, s.name, s.asset_class, s.active,
       s.metadata_json, s.metric_values_json, s.metric_rows_json, s.aliases_json,
       s.fundamental_periods_json, s.corporate_actions_json
FROM instrument_snapshots AS s
JOIN active_dataset AS a ON a.singleton = 1 AND a.dataset_id = s.dataset_id;

DROP TABLE IF EXISTS instrument_snapshots;
ALTER TABLE instrument_snapshots_v2 RENAME TO instrument_snapshots;
CREATE INDEX IF NOT EXISTS idx_instrument_snapshots_active
    ON instrument_snapshots(dataset_id, asset_class, active);
