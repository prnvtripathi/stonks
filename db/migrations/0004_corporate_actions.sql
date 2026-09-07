-- Forward migration for installations created before governed corporate-action research.
-- IF NOT EXISTS keeps local/D1 upgrade retries idempotent.
CREATE TABLE IF NOT EXISTS corporate_actions (
    dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id),
    action_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    action_date TEXT NOT NULL,
    action_type TEXT NOT NULL,
    numerator REAL,
    denominator REAL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    source_artifact_id TEXT,
    PRIMARY KEY (dataset_id, action_id)
);

CREATE INDEX IF NOT EXISTS idx_corporate_actions_instrument
    ON corporate_actions(dataset_id, instrument_id, action_date);
