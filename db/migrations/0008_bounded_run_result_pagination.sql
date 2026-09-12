-- R06: bound saved-screen execution and result retrieval to set-based D1 SQL.
--
-- `instrument_snapshots` (0006) retains only the active dataset's projection,
-- so a historical run can no longer join back to it for the metric values
-- its explanation needs. Each match therefore carries a compact snapshot of
-- just the metric rows its screen's predicates (plus the momentum
-- breakdown) reference, captured at run time by a single set-based
-- `INSERT ... SELECT`. Explanations for a requested page are then derived
-- from this immutable column, not from live instrument data.
ALTER TABLE screen_matches ADD COLUMN metrics_json TEXT NOT NULL DEFAULT '[]';

-- Supports `pageRunMatches`: fetch and count one run's live (non-exited)
-- matches without scanning exited/other-run rows first.
CREATE INDEX IF NOT EXISTS idx_screen_matches_by_run
    ON screen_matches(run_id, exited, ordinal);
