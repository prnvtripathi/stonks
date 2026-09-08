-- Remote publication uses one compact row per instrument so a full universe
-- stays safely below D1's daily write allowance. Local pipeline analytics
-- remain normalized in latest_metrics; only the Cloudflare import writes this
-- denormalized serving projection. Saved screens/runs/matches remain global.
CREATE TABLE IF NOT EXISTS instrument_snapshots (
    dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id),
    instrument_id TEXT NOT NULL,
    symbol TEXT,
    name TEXT,
    asset_class TEXT NOT NULL,
    active INTEGER NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    metric_values_json TEXT NOT NULL DEFAULT '{}',
    metric_rows_json TEXT NOT NULL DEFAULT '[]',
    aliases_json TEXT NOT NULL DEFAULT '[]',
    fundamental_periods_json TEXT NOT NULL DEFAULT '[]',
    corporate_actions_json TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (dataset_id, instrument_id)
);

CREATE INDEX IF NOT EXISTS idx_instrument_snapshots_active
    ON instrument_snapshots(dataset_id, asset_class, active);
