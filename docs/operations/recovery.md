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

**A pointer-only flip is no longer sufficient against production D1.**
`db/migrations/0006_bounded_instrument_snapshots.sql` collapsed
`instrument_snapshots` to a *bounded serving projection* -- one row per
`instrument_id`, not one row per dataset -- and every promotion
(`pipeline/market_pipeline/publication/d1_export.py`'s
`DELETE FROM instrument_snapshots WHERE dataset_id <> :new_dataset_id`)
physically deletes every other dataset's serving rows as part of going live.
Concretely: if dataset `A` was promoted, then dataset `B` superseded it, `A`'s
`instrument_snapshots` rows are gone by the time `B` is active -- only
`datasets.status='superseded'` (bookkeeping) still remembers `A` existed.
Repointing `active_dataset` back to `A` after that (the old procedure this
section used to document) makes `active_dataset.dataset_id = 'A'` again, but
a dataset-scoped serving query for `A` now returns **zero rows**, because
there is nothing left in `instrument_snapshots` for it to return. Use
`market_pipeline.publication.restore` instead -- it reimports the compact
projection from a retained, checksum-verified publication bundle rather than
only moving the pointer, and it never touches `saved_screens`, `screen_runs`,
or `screen_matches` (owner/product state the Worker owns, not the pipeline's
market-data tables).

**Step 1: identify the target dataset.** Only a dataset whose publication
bundle is still *retained* can be restored -- by design, that is the current
active dataset and the immediately-previous successful promotion (R07's
`reachable_bundles`; anything older may already have had its derived R2
objects garbage-collected). List what is currently restorable:

```bash
uv run python -c "
import sqlite3
from market_pipeline.publication.restore import list_restorable_datasets
connection = sqlite3.connect('market.db')
for item in list_restorable_datasets(connection):
    print(item.dataset_id)
"
```

Pick the `dataset_id` of the last dataset you know was good. If it is not
printed above, it is not restorable this way -- there is no separate
mechanism to reach further back, by design (see R07/F14's retention model).

**Step 2: generate and verify the restore import.** This selects, checksum-
verifies (input-manifest identity, then every object's bytes), and
re-synchronizes the chosen dataset's objects through the same bounded,
byte-verified R2 transport (`publication/r2_sync.py`) a normal publish uses,
then produces a D1 import SQL file with the identical shape/scope a normal
publish's own export produces (market-data tables only):

```bash
export CLOUDFLARE_ACCOUNT_ID=... R2_ACCESS_KEY_ID=... R2_SECRET_ACCESS_KEY=...
uv run python -m market_pipeline.publication.restore \
  --db market.db \
  --dataset-id <GOOD_DATASET_ID> \
  --output restore-dataset.sql \
  --history-root history \
  --object-manifest restore-history-objects.json \
  --publication-plan restore-publication-plan.json \
  --verify-remote > restore-dataset-id.txt
```

A dataset that is not currently retained, whose recorded bundle is missing
required identity fields, whose local objects fail checksum verification, or
whose remote objects cannot be verified/re-synchronized all fail this step
closed -- **before** any SQL is generated and before `active_dataset` is
touched, so the currently-active (possibly bad) dataset stays exactly as
usable as it was.

**Step 3: run the same preflight/budget gate a normal publish uses**, against
the generated plan, exactly as `.github/workflows/daily-data.yml`'s
"Preflight remote publication budget" step does for a normal publication (see
that workflow for the full command, including `--remote-usage` telemetry).
There is no separate, weaker gate for a restore.

**Step 4: apply the verified import to production D1**, then verify:

```bash
pnpm --dir apps/api exec wrangler d1 execute stonks-research --env production --remote --file restore-dataset.sql

expected_dataset_id="$(tr -d '\r\n' < restore-dataset-id.txt)"
pnpm --dir apps/api exec wrangler d1 execute stonks-research --env production --remote \
  --command "SELECT dataset_id FROM active_dataset WHERE singleton = 1" --json
# The returned dataset_id must equal ${expected_dataset_id}.
pnpm --dir apps/api exec wrangler d1 execute stonks-research --env production --remote \
  --command "SELECT COUNT(*) AS n FROM instrument_snapshots WHERE dataset_id = '${expected_dataset_id}'" --json
# n must be > 0 -- this is exactly the check that a pointer-only flip fails.
```

As with a normal promotion, `active-dataset.sql`-style imports never contain
a transaction wrapper: Cloudflare D1 rolls a failed
`wrangler d1 execute --remote --file` import back to its original state on
the *remote* side. This runbook (and `restore.py`'s own tests) verify the
generated SQL's *local* behavior via `sqlite3.executescript` against a
schema-equivalent database, but that is not proof of D1's own remote
transactional rollback -- confirming a rejected remote import truly leaves
the previous dataset active/serving is a live-account acceptance drill, not
something this pipeline's local tests can establish on their own.

Do **not** delete the previously-active (bad) dataset's rows -- keep it as
`superseded` for audit/debugging; restore never deletes any `datasets` row,
only reads them. A subsequent good daily run will supersede the restored
dataset again through the normal `stage`/`promote` path once the original
run's root cause is fixed.

**Local-only exception.** The local SQLite `market.db` this pipeline builds
locally never itself serves reads from `instrument_snapshots` -- that table
is populated only by *applying* a generated D1 import SQL file (locally, in
a test, or for real against production D1), never by
`D1Publisher.stage`/`promote`. Flipping `active_dataset`/`datasets.status`
directly in `market.db` with `sqlite3` (the old Step 2 above) is therefore
still harmless there and remains a valid **local-only** bookkeeping fixup --
it just does nothing to any production-serving data, and must never be
treated as a substitute for the restore procedure above against production
D1.

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
