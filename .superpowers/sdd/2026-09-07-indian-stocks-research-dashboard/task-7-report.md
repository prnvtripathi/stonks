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

`git diff --check` passed and the worktree was clean after the initial implementation commit `7c9164b`.

## Concerns

- A real Cloudflare R2/D1 SDK is intentionally not required; production wiring
  should adapt its conditional object and SQLite-compatible query clients to the
  injected protocols.
- The CLI reports missing dates until an operator-supplied artifact fetcher is
  integrated by the scheduled workflow. It does not enable automated NSE fetches.
- `uv run` attempted an editable package rebuild after the new script entry point
  and was blocked by the sandbox's unavailable PyPI DNS. Equivalent checks ran
  successfully through the existing worktree virtualenv with `PYTHONPATH=pipeline`.

## Fix round 1/5

### Findings addressed

- Dataset IDs are now insert-only: any previously used ID is rejected before
  staging, preventing active rows or the active pointer from being mutated.
- Candidates require a non-empty instruments snapshot and latest metrics, an
  effective date, verified status, and complete expected source runs. These
  invariants are checked before staging and again against staged SQLite rows
  inside the promotion transaction.
- Checkpoints now key by source, effective date, and stable artifact ID while
  retaining checksum and object key, so multiple artifacts on one date are
  independently idempotent and order-independent.
- Fetch, policy, lineage, checksum, and persistence failures raise typed blocking
  errors immediately. Missing dates remain governed by `strict_coverage`.
- Daily CLI execution requires an explicit committed-artifact manifest/provider,
  runs strict coverage, and returns machine-readable nonzero errors when input is
  missing or source dates are unavailable. Explicit `--source` values replace the
  default source rather than appending to it.
- Added configurable `StorageBudget` telemetry with used/limit/ratio/warning at
  the 80% threshold for D1 and R2 history stores; warnings flow into job results
  and CLI JSON output.

### Fix-round RED

Command:

```text
PYTHONPATH=pipeline .venv/bin/pytest pipeline/tests/integration/test_publication.py pipeline/tests/integration/test_backfill.py pipeline/tests/integration/test_budgets.py pipeline/tests/integration/test_cli.py -q
```

Initial result before fixes:

```text
ImportError: cannot import name 'BackfillIntegrityError'
ModuleNotFoundError: No module named 'market_pipeline.storage.budgets'
```

The missing contracts represented the requested regression coverage for identity,
completeness, artifact checkpoints, integrity blocking, CLI configuration, and
budgets.

### Fix-round GREEN and final verification

```text
PYTHONPATH=pipeline .venv/bin/pytest pipeline/tests/integration -q
21 passed in 0.17s
```

```text
PYTHONPATH=pipeline .venv/bin/pytest -q
135 passed in 0.23s
```

```text
PYTHONPATH=pipeline .venv/bin/ruff check pipeline
All checks passed!

PYTHONPATH=pipeline .venv/bin/mypy pipeline
Success: no issues found in 51 source files
```

Implementation fixes committed as `102ef91 fix: harden publication backfill and storage budgets`.

### Fix-round self-review

- Restaging an active ID cannot reach the database upsert path; staged rows are
  never replaced because a dataset ID can be inserted only once.
- Promotion reads and validates the persisted candidate metadata and counts,
  preserving the prior active pointer on every reconciliation failure.
- A changed checksum for an already checkpointed artifact is blocking; distinct
  artifact IDs on the same source/date produce separate checkpoint rows.
- The daily CLI has no network fallback and cannot silently report success from an
  empty fetcher. NSE automation remains disabled.
- Budget math has no hard-coded provider limit; callers configure byte limits and
  warning thresholds while stores measure local/injected-client usage.

## Fix round 2/5

### Findings addressed

- JSON manifests now require an explicit `{"artifacts": [...]}` schema. Each entry
  validates `SourceArtifact` metadata, base64-decodes its body, verifies the body
  checksum, and produces a typed `FetchedArtifact` for the injected job fetcher.
- CLI backfill now uses strict coverage, returning nonzero machine-readable errors
  for incomplete manifests just like daily runs.
- Added `0002_checkpoint_artifact_identity.sql` and an upgrade-safe conditional
  runner migration. Legacy source/date checkpoints are preserved with a
  deterministic `legacy-<sha256>` artifact ID and their checksum/object key.
  Publisher schema initialization invokes the same upgrade path.
- R2 history budget telemetry supports an injected usage provider or an opaque
  client's `usage_bytes`/`get_usage_bytes` method. It no longer introspects test
  `.objects`; local filesystem accounting remains available for local stores.

### Fix-round RED

Command:

```text
PYTHONPATH=pipeline .venv/bin/pytest pipeline/tests/integration/test_cli.py pipeline/tests/integration/test_backfill.py pipeline/tests/integration/test_budgets.py -q
```

Initial result before fixes:

```text
4 failed, 13 passed
```

Failures were: valid manifest returned missing dates; incomplete backfill exited
zero; legacy checkpoint lacked `artifact_id`; and opaque R2 usage measured zero.

### Fix-round GREEN and final verification

```text
PYTHONPATH=pipeline .venv/bin/pytest pipeline/tests/integration/test_cli.py pipeline/tests/integration/test_backfill.py pipeline/tests/integration/test_budgets.py -q
17 passed in 0.15s
```

```text
PYTHONPATH=pipeline .venv/bin/pytest -q
138 passed in 0.27s
```

```text
PYTHONPATH=pipeline .venv/bin/ruff check pipeline
All checks passed!

PYTHONPATH=pipeline .venv/bin/mypy pipeline
Success: no issues found in 51 source files
```

Implementation fixes committed as `cb1c5c4 fix: support artifact manifests and checkpoint migration`.

### Fix-round self-review and concerns

- Manifest validation is intentionally strict and only accepts committed artifact
  bytes represented as base64; malformed metadata, unknown fields, invalid base64,
  and checksum drift fail before any job run.
- Backfill still reports missing dates in its result API, but the CLI treats them as
  a strict coverage failure; integrity failures remain immediate everywhere.
- The SQL migration file documents the conditional rewrite, while the executable
  SQLite rewrite lives in the shared runner so it works on both fresh and legacy
  databases without relying on unsupported SQLite conditional DDL.
- Production R2 clients should provide a usage method or be paired with an
  injected usage provider; absent telemetry is represented as zero usage and does
  not invent a provider limit.

## Fix round 3/5

### Finding addressed

Migrated `legacy-*` checkpoints are now reconciled when an incoming canonical
artifact ID is unavailable. Lookup first honors an exact canonical ID; otherwise
it matches exactly one legacy row by source/date/checksum. A matching row is
skipped as already processed, a changed checksum is a blocking
`BackfillIntegrityError`, and multiple matching legacy rows are rejected as
ambiguous rather than guessed.

### Fix-round RED

Command:

```text
PYTHONPATH=pipeline .venv/bin/pytest pipeline/tests/integration/test_backfill.py -q
```

Initial result before the fallback:

```text
2 failed, 7 passed
```

The matching migrated retry reprocessed (`completed=1, skipped=0`) and changed
checksum did not raise the required blocking error.

### Fix-round GREEN and final verification

```text
PYTHONPATH=pipeline .venv/bin/pytest pipeline/tests/integration/test_backfill.py -q
9 passed in 0.10s
```

```text
PYTHONPATH=pipeline .venv/bin/pytest -q
140 passed in 0.24s
```

```text
PYTHONPATH=pipeline .venv/bin/ruff check pipeline
All checks passed!

PYTHONPATH=pipeline .venv/bin/mypy pipeline
Success: no issues found in 51 source files
```

Implementation committed as `6b2602b fix: reconcile legacy artifact checkpoints safely`.

### Fix-round self-review

- Exact canonical IDs remain authoritative and still detect checksum drift through
  the existing post-lookup check.
- Legacy fallback is checksum-based only when canonical lookup misses, and does
  not collapse distinct canonical artifacts on the same source/date.
- Multiple legacy rows matching the same checksum produce a blocking ambiguity;
  no legacy state is silently overwritten or marked successful.
