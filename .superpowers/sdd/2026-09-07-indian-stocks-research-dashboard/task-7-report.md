# Task 7 Report: D1/R2 Publication and Three-Year Backfill

## Implementation

- Added `db/migrations/0001_market_schema.sql` with versioned datasets, a singleton
  active pointer, source/run lineage, instruments and aliases, latest metrics,
  fundamental periods, screens/runs/matches, glossary entries, and resumable
  source/date checkpoints. Query tables carry `dataset_id`.
- Added `D1Publisher.stage` and `D1Publisher.promote` for SQLite/D1-compatible
  publication. Candidate validation happens before staging and promotion uses one
  `BEGIN IMMEDIATE` transaction; a failed candidate leaves the active pointer and
  prior dataset unchanged.
- Added `HistoryStore`, `LocalHistoryStore`, and injected-client
  `R2HistoryStore`. History is partitioned as
  `history/{asset_class}/{instrument_id}/{year}.parquet` with Zstandard compression;
  chart snapshots are deterministic, gzip-compressed
  `charts/{dataset_id}/{instrument_id}.json.gz` objects with no public URL helper.
- Added `BackfillJob`/`run_backfill` and `run_daily`. Backfill defaults to execution
  date minus three calendar years through the latest complete date, checkpoints
  source/date/checksum, skips verified checksums, reports missing dates, supports
  rate limiting, and can fail on strict coverage. Fetching is injected so disabled
  NSE network automation is never bypassed; committed operator artifacts can be
  supplied through the fetcher.
- Added the `market-pipeline backfill` and `market-pipeline daily` entry points and
  JSON status output. The CLI intentionally has no network adapter wiring.

## TDD evidence

### RED

Command before implementation:

```text
UV_CACHE_DIR=/private/tmp/stonks-uv-cache uv run pytest pipeline/tests/integration/test_publication.py pipeline/tests/integration/test_backfill.py -q
```

Result:

```text
ModuleNotFoundError: No module named 'market_pipeline.storage.d1_publisher'
ModuleNotFoundError: No module named 'market_pipeline.jobs'
```

This was the expected missing-contract failure for the atomic publication and
backfill-range tests.

### GREEN

After the minimal publisher, migration, jobs, and history implementations:

```text
PYTHONPATH=pipeline .venv/bin/pytest pipeline/tests/integration -q
......  [100%]
6 passed in 0.15s
```

The focused tests cover rollback-safe invalid publication, successful pointer
promotion, deterministic/year-partitioned history, retry idempotency, default
three-calendar-year range, and missing-date reporting.

## Final verification

```text
PYTHONPATH=pipeline .venv/bin/pytest -q
120 passed in 0.21s
```

```text
PYTHONPATH=pipeline .venv/bin/ruff check pipeline
All checks passed!
```

```text
PYTHONPATH=pipeline .venv/bin/mypy pipeline
Success: no issues found in 48 source files
```

`git diff --check` passed and the worktree is clean after commit `e6a9395`.

## Concerns

- A real Cloudflare R2/D1 SDK is intentionally not required; production wiring
  should adapt its conditional object and SQLite-compatible query clients to the
  injected protocols.
- The CLI reports missing dates until an operator-supplied artifact fetcher is
  integrated by the scheduled workflow. It does not enable automated NSE fetches.
- `uv run` attempted an editable package rebuild after the new script entry point
  and was blocked by the sandbox's unavailable PyPI DNS. Equivalent checks ran
  successfully through the existing worktree virtualenv with `PYTHONPATH=pipeline`.
