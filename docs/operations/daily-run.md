# Daily data refresh: schedule, checks, and manual runs

This describes `.github/workflows/daily-data.yml`, the scheduled job that runs
`market-pipeline daily` (or `backfill`) against an operator-supplied artifact
manifest, gated by the reconciliation and storage-budget checks in
`pipeline/market_pipeline/validation/reconcile.py` and
`pipeline/market_pipeline/monitoring/budgets.py`.

## Schedule

- Cron: `30 13 * * 1-5` (UTC). Asia/Kolkata is UTC+5:30, so this is **19:00
  IST**, after NSE's official EOD bhavcopy is typically published.
- `1-5` restricts the run to Monday-Friday **in UTC**. GitHub Actions cron is
  always evaluated in UTC, including the day-of-week field. At 13:30 UTC the
  UTC calendar day and the IST calendar day are the same (IST is only ~5.5
  hours ahead, and 13:30 UTC + 5:30 = 19:00 IST on the same date), so a
  Mon-Fri UTC restriction correctly matches NSE's Mon-Fri IST trading week for
  this run time.
- The effective date used for a scheduled run is "today" in Asia/Kolkata at
  run time, computed with `TZ=Asia/Kolkata date +%Y-%m-%d` inside the
  workflow.

## What runs, and in what order

1. Checkout, Python/uv setup, `uv sync --locked` (mirrors `ci.yml`).
2. Resolve run parameters (mode, date/range, manifest path, reconciliation
   thresholds) from either the schedule defaults or `workflow_dispatch`
   inputs.
3. Invoke the existing `market-pipeline` CLI (`daily` or `backfill`). This
   repository does not run automated NSE fetch (see
   `docs/operations/source-policy.md` -- NSE's Terms of Use currently
   disallow it), so the CLI is always invoked against a pre-fetched artifact
   manifest, exactly as it is in local/manual use. Producing that manifest
   (AMFI NAV automation, or committed/uploaded official artifacts for other
   sources) is outside this workflow; supply its path via `manifest_path`.
4. The CLI itself now runs a **reconciliation and budget safety gate** after
   the backfill/daily job completes and before printing its result:
   - `reconcile(previous=<baseline>, candidate=<completed count>, ...)` from
     `validation/reconcile.py` compares the new run's completed-artifact
     count against a known-good baseline. **The default blocking threshold is
     a coverage ratio of 90%** (`min_coverage_ratio=0.90`, overridable via
     `--min-coverage-ratio` / the `min_coverage_ratio` workflow input): if the
     candidate has fewer than 90% of the previous baseline's rows, the run is
     marked `publishable: false`. This is deliberately strict -- a genuine
     one-day change in the investable universe is a small fraction of a
     percent; a coverage drop of this size almost always means a source fetch
     silently truncated.
   - Each requested source is independently evaluated as `complete`,
     `delayed`, `failed`, or `not_expected`, derived from the backfill job's
     own missing-date accounting. A `failed` source (100% of requested dates
     missing for that source) blocks publication; `delayed`/`not_expected`
     are reported but do not block on their own, since one source's expected
     absence or lag must never mask another source's problem.
   - `monitoring/budgets.py` reports the D1 database's storage usage/limit
     ratio; a budget at or past its hard limit (`exceeded`) also blocks.
   - The combined verdict is `evaluate_pre_promotion(...)`, surfaced in the
     CLI's JSON output as `safe_to_promote` and `blocking_reasons`, and as the
     process exit code: `0` = safe, `3` = blocking failure, `2` = input error
     (bad manifest/missing dates raised before reconciliation even runs).
5. The workflow redacts the run's JSON output (stripping any key that looks
   like a token/secret/password/authorization/API key) and publishes it both
   as a build artifact (`daily-data-reconciliation-report`, 90-day retention)
   and as the job's step summary.
6. If the CLI's exit code is non-zero, the workflow step "Stop before
   promotion on blocking failure" fails the job with that same exit code.
   **No promotion step runs after this point** -- see the note below on the
   current scope of promotion.

## What "blocking failure" means operationally

A blocking failure means the candidate data for that date/range must **not**
be treated as the new known-good dataset. Concretely:

- The job goes red in GitHub Actions; the step summary and the
  `daily-data-reconciliation-report` artifact show `safe_to_promote: false`
  and the specific `blocking_reasons` (a coverage drop, a failed source, or
  an exceeded storage budget).
- The already-published (previously promoted) dataset in D1 is untouched --
  `D1Publisher.promote` performs an atomic pointer swap and is simply never
  reached on a blocking failure, so readers keep seeing the last known-good
  data (see the Global Constraint: a failed run must never replace the last
  known-good dataset).
- Raw artifacts fetched/checkpointed during the run are retained (the
  backfill job's checkpoint table is idempotent), so a retry does not need to
  re-fetch anything that already succeeded -- see
  `docs/operations/recovery.md` for the retry command.

Note on scope: as of this task, `market-pipeline daily`/`backfill` do not yet
call `D1Publisher.promote` themselves -- they populate raw-artifact
checkpoints and report reconciliation/budget status. The `safe_to_promote`
gate defined here is the interlock a future publish step (or an operator
running the D1 publish path manually) must consult before calling `promote`;
this workflow already fails closed on it today.

## Concurrency lock

```yaml
concurrency:
  group: daily-data
  cancel-in-progress: false
```

A scheduled run and a manual `workflow_dispatch` run share the same
concurrency group, so a second run queues behind a first rather than racing
it against the same SQLite/D1 database. `cancel-in-progress: false` ensures a
run is never killed mid-write, which could otherwise leave checkpoints in an
inconsistent state.

## Triggering a manual run

Use **Actions -> Daily Data Refresh -> Run workflow** and fill in:

| Input | Meaning |
| --- | --- |
| `mode` | `daily` for a single date, or `backfill` for a date range |
| `date` | Required for `daily`: `YYYY-MM-DD` |
| `start` / `end` | Required for `backfill`: inclusive `YYYY-MM-DD` range |
| `manifest_path` | Path (in the checked-out repo) to the pre-fetched artifact manifest JSON |
| `previous_count` | Known-good baseline completed-artifact count for the coverage check (omit to skip the coverage check, e.g. for a first-ever run) |
| `min_coverage_ratio` | Override the default `0.90` threshold if a run has a known, deliberate coverage change |

This is also how you re-run a specific historical date or range -- see
`docs/operations/recovery.md` for the exact commands.
