-- Query-ready market schema.  Dataset IDs make every published row immutable
-- and allow an active pointer to be swapped without mutating the prior view.
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS datasets (
    dataset_id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('staging', 'active', 'superseded', 'failed')),
    created_at TEXT NOT NULL,
    promoted_at TEXT,
    effective_date TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS active_dataset (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id),
    changed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id),
    source_id TEXT NOT NULL,
    source_url TEXT NOT NULL,
    terms_url TEXT NOT NULL,
    status TEXT NOT NULL,
    effective_date TEXT,
    checksum TEXT,
    adapter_version TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (dataset_id, source_id)
);

CREATE TABLE IF NOT EXISTS source_runs (
    dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id),
    run_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    effective_date TEXT NOT NULL,
    checksum TEXT,
    status TEXT NOT NULL,
    error TEXT,
    started_at TEXT,
    completed_at TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (dataset_id, run_id)
);

CREATE TABLE IF NOT EXISTS instruments (
    dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id),
    instrument_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    provider_identifier TEXT NOT NULL,
    asset_class TEXT NOT NULL,
    symbol TEXT,
    name TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    raw_series TEXT,
    raw_type TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (dataset_id, instrument_id)
);

CREATE TABLE IF NOT EXISTS instrument_aliases (
    dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id),
    instrument_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    alias TEXT NOT NULL,
    valid_from TEXT,
    valid_to TEXT,
    PRIMARY KEY (dataset_id, provider, alias)
);

CREATE TABLE IF NOT EXISTS latest_metrics (
    dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id),
    instrument_id TEXT NOT NULL,
    effective_date TEXT NOT NULL,
    metric TEXT NOT NULL,
    value REAL,
    state TEXT NOT NULL CHECK (state IN ('present', 'missing', 'not_applicable')),
    raw_value TEXT,
    normalized_value REAL,
    formula_version TEXT,
    source_artifact_id TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (dataset_id, instrument_id, metric)
);

CREATE TABLE IF NOT EXISTS fundamental_periods (
    dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id),
    period_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    period_end TEXT NOT NULL,
    period_type TEXT NOT NULL,
    filing_id TEXT NOT NULL,
    filed_at TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    source_artifact_id TEXT,
    restates_id TEXT,
    supersedes_id TEXT,
    PRIMARY KEY (dataset_id, period_id)
);

CREATE TABLE IF NOT EXISTS saved_screens (
    dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id),
    screen_id TEXT NOT NULL,
    name TEXT NOT NULL,
    expression TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (dataset_id, screen_id)
);

CREATE TABLE IF NOT EXISTS screen_runs (
    dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id),
    run_id TEXT NOT NULL,
    screen_id TEXT NOT NULL,
    effective_date TEXT NOT NULL,
    status TEXT NOT NULL,
    result_count INTEGER NOT NULL DEFAULT 0,
    query_version TEXT,
    error TEXT,
    started_at TEXT,
    completed_at TEXT,
    PRIMARY KEY (dataset_id, run_id)
);

CREATE TABLE IF NOT EXISTS screen_matches (
    dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id),
    run_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    instrument_id TEXT NOT NULL,
    score REAL,
    explanation_json TEXT NOT NULL DEFAULT '{}',
    entered INTEGER,
    exited INTEGER,
    PRIMARY KEY (dataset_id, run_id, ordinal)
);

-- NOTE: dead schema as of Task 12. The reviewed glossary is served entirely
-- from `packages/contracts/src/glossary.ts`, whose `GlossaryEntry` contract
-- (slug, term, aliases, summary, ...) does not match these columns, and no
-- code reads or writes this table. It is retained rather than dropped because
-- 0001 is the applied base migration on existing databases; drop it in a
-- forward migration if the glossary is ever moved back into D1.
CREATE TABLE IF NOT EXISTS glossary_entries (
    dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id),
    term TEXT NOT NULL,
    definition TEXT NOT NULL,
    source_url TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (dataset_id, term)
);

CREATE TABLE IF NOT EXISTS backfill_checkpoints (
    source_id TEXT NOT NULL,
    effective_date TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    checksum TEXT NOT NULL,
    object_key TEXT,
    completed_at TEXT NOT NULL,
    PRIMARY KEY (source_id, effective_date, artifact_id)
);

CREATE INDEX IF NOT EXISTS idx_metrics_active ON latest_metrics(dataset_id, metric, effective_date);
CREATE INDEX IF NOT EXISTS idx_instruments_active ON instruments(dataset_id, asset_class, active);
CREATE INDEX IF NOT EXISTS idx_source_runs_date ON source_runs(source_id, effective_date);
CREATE INDEX IF NOT EXISTS idx_fundamentals_instrument ON fundamental_periods(dataset_id, instrument_id, period_end);
CREATE INDEX IF NOT EXISTS idx_screen_matches_run ON screen_matches(dataset_id, run_id, ordinal);
