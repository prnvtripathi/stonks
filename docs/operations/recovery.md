# Recovery: retry, rollback, and historical re-run

Exact, copy-pasteable operator commands for the three recovery scenarios this
pipeline needs to support. All commands assume you are at the repository
root with `uv sync --locked` already run.

## 1. Retry a failed daily run

The backfill job (`pipeline/market_pipeline/jobs/backfill.py`) records a
checkpoint per `(source_id, effective_date, artifact_id)` and skips any
artifact whose checksum already matches a saved checkpoint. Re-running the
exact same command is therefore safe and cheap: already-completed sources for
that date are skipped, and only what failed is retried.

```bash
# Re-run the same date with the same manifest that failed.
uv run market-pipeline --db market.db daily \
  --date 2026-09-07 \
  --manifest manifests/2026-09-07.json

# If the failure was reconciliation (exit code 3, not a fetch/coverage
# error), re-check the printed JSON's "blocking_reasons" before assuming a
# bare retry will help -- a genuine coverage drop or failed source needs a
# corrected manifest, not just a re-run.
```

Check the exit code and act on it:

```bash
uv run market-pipeline --db market.db daily --date 2026-09-07 --manifest manifests/2026-09-07.json; echo "exit=$?"
# 0 = succeeded and safe to promote
# 2 = input/coverage error before reconciliation ran (bad manifest, missing artifacts)
# 3 = ran, but reconciliation/budget blocked promotion -- inspect blocking_reasons
```

## 2. Roll back to the last known-good published dataset

`D1Publisher` (`pipeline/market_pipeline/storage/d1_publisher.py`) tracks
exactly one active dataset via the singleton `active_dataset` table, and
every previously-active dataset is retained with `status='superseded'` (never
deleted) in the `datasets` table. Rolling back means repointing
`active_dataset` at a prior `dataset_id` and flipping the two rows' statuses
back -- the same two statements `D1Publisher.promote` itself runs, executed
directly rather than through a new rollback method (there isn't, and should
not be, separate rollback machinery).

**Step 1: identify the target dataset.**

```bash
sqlite3 market.db "SELECT dataset_id, status, effective_date, promoted_at FROM datasets ORDER BY promoted_at DESC LIMIT 10;"
```

Pick the `dataset_id` of the last dataset you know was good (its `status`
will currently be `superseded`).

**Step 2: swap the active pointer inside a single transaction** (so readers
never observe a half-updated state):

```bash
sqlite3 market.db <<'SQL'
BEGIN IMMEDIATE;
UPDATE datasets SET status='superseded' WHERE status='active';
UPDATE datasets SET status='active', promoted_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE dataset_id='<GOOD_DATASET_ID>';
INSERT INTO active_dataset(singleton, dataset_id, changed_at)
  VALUES (1, '<GOOD_DATASET_ID>', strftime('%Y-%m-%dT%H:%M:%fZ','now'))
  ON CONFLICT(singleton) DO UPDATE SET dataset_id=excluded.dataset_id, changed_at=excluded.changed_at;
COMMIT;
SQL
```

Replace `<GOOD_DATASET_ID>` with the dataset ID chosen in Step 1. For a
production Cloudflare D1 database, run the same two `UPDATE`/`INSERT`
statements via `wrangler d1 execute <DB_NAME> --command "..."` (or
`--file`), against the same schema -- the statements are database-engine
agnostic SQL, unchanged from what `D1Publisher.promote` runs internally.

**Step 3: verify.**

```bash
sqlite3 market.db "SELECT dataset_id FROM active_dataset WHERE singleton=1;"
# Should print <GOOD_DATASET_ID>
```

Do **not** delete the bad dataset's rows -- keep it as `superseded` for
audit/debugging. A subsequent good daily run will supersede it again through
the normal `stage`/`promote` path once its root cause is fixed.

## 3. Restore or re-run a specific historical date range

Use the existing backfill CLI (`pipeline/market_pipeline/jobs/backfill.py`
via `pipeline/market_pipeline/cli.py`) exactly as for a fresh backfill, with
`--start`/`--end` bounding the range to restore. This retains the "three
years of history" retention constraint automatically: checkpoints for dates
outside the requested range are left untouched.

```bash
# Re-run (or backfill for the first time) an inclusive historical range.
uv run market-pipeline --db market.db backfill \
  --start 2026-08-01 \
  --end 2026-08-31 \
  --manifest manifests/2026-08-backfill.json \
  --previous-count <BASELINE_COUNT> \
  --min-coverage-ratio 0.90
```

- `--manifest` must contain every artifact for every requested
  `(source, date)` pair; a manifest with gaps causes the run to fail closed
  (`CoverageError`, exit code 2) rather than silently publish a partial
  range, per this pipeline's strict-coverage default.
- `--previous-count` is optional but recommended for a re-run: supply the
  completed-artifact count from the last known-good run over the same range
  so the reconciliation coverage check (`docs/operations/daily-run.md`) is
  meaningful rather than trivially self-consistent.
- This command only repopulates raw-artifact checkpoints (and, if you extend
  it to publish, would stage/promote through `D1Publisher`); it does not by
  itself touch `active_dataset`. To make a restored range live, follow the
  rollback/promotion steps above once the run's `safe_to_promote` is `true`.
