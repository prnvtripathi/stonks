-- Saved-screen runs are historical records. They must retain the query and
-- instrument identity used at execution even after the serving projection is
-- replaced or the saved screen is edited.
ALTER TABLE screen_runs ADD COLUMN source TEXT;
ALTER TABLE screen_runs ADD COLUMN language_version TEXT;

ALTER TABLE screen_matches ADD COLUMN symbol TEXT;
ALTER TABLE screen_matches ADD COLUMN name TEXT;
ALTER TABLE screen_matches ADD COLUMN asset_class TEXT;
