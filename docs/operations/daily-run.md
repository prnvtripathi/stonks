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
3. **Restore `market.db` from the previous run's cache** (`actions/cache/restore`,
   key `market-db-${{ github.run_id }}` with `restore-keys: market-db-`, so
   the most recently saved entry is used). This GitHub Actions runner is
   ephemeral -- without this step every run would start from an empty
   database and look like a first-ever run to the CLI. See "Coverage
   baseline persistence" below for why this matters.
3a. **Restore the checkpoint archive on a cache miss** (F12; see "Durable
   checkpoint-archive recovery" below). `actions/cache` is documented as
   evictable, and a miss previously meant the three years of `raw/`
   artifact bodies and `market.db`'s checkpoint/publication history were
   simply gone, with no way back. This step checks whether the cache
   restore above actually produced a `market.db`: if not, it restores the
   last verified checkpoint archive from private R2 storage instead; if it
   did, it validates that cached database's active dataset identity, and
   the local `raw/` store's completeness, against the authoritative remote
   manifest (cheap: one small object plus local existence checks, no bulk
   download) -- and if a stale or partially-saved cache entry is detected,
   it discards that local state and falls through to the same restore a
   cache miss would have run, rather than merely warning and proceeding.
4. **Skip cleanly if no manifest is present.** The scheduled trigger looks for
   `manifests/daily.json`. No automated NSE/AMFI fetch is wired (see
   `docs/operations/source-policy.md`), so producing that file is an operator
   step and it is normally absent. The "Resolve run parameters" step sets a
   `manifest_present` output; when it is `false` every subsequent step is
   skipped, the job posts a `::notice::` and a job summary explaining that no
   manifest was supplied, and the job **succeeds**. This is deliberate: a
   missing manifest is "nothing to do today", not an incident, and failing the
   job every weekday would train the operator to ignore this workflow's
   alerts. A run that *does* have a manifest and then fails still goes red.
5. Invoke the existing `market-pipeline` CLI (`daily` or `backfill`). This
   repository does not run automated NSE fetch (see
   `docs/operations/source-policy.md` -- NSE's Terms of Use currently
   disallow it), so the CLI is always invoked against a pre-fetched artifact
   manifest, exactly as it is in local/manual use. Producing that manifest
   (AMFI NAV automation, or committed/uploaded official artifacts for other
   sources) is outside this workflow; supply its path via `manifest_path`.
6. The CLI itself now runs the backfill/daily job with `strict_coverage`
   disabled -- a source missing some or all of its requested dates does not
   abort the run before reconciliation gets to see it -- and then runs a
   **reconciliation and budget safety gate** before printing its result:
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
     own missing-date accounting: `failed` means every requested date is
     missing for that source, `delayed` means some (but not all) dates are
     missing. A `failed` source blocks publication; `delayed`/`not_expected`
     are reported but do not block on their own, since one source's expected
     absence or lag must never mask another source's problem.
   - `monitoring/budgets.py` reports the D1 database's storage usage/limit
     ratio; a budget at or past its hard limit (`exceeded`) also blocks.
   - The combined verdict is `evaluate_pre_promotion(...)`, surfaced in the
     CLI's JSON output as `safe_to_promote` and `blocking_reasons`, and as the
     process exit code: `0` = safe **and published**, `3` = blocking failure
     (coverage drop, failed source, or exceeded budget) -- nothing was staged,
     `4` = the gate passed but the dataset could not be built or staged, `2` =
     input error (unreadable/invalid manifest, or a bad date range) raised
     before the job or reconciliation even run.
   - **Only a safe-to-promote (`exit 0`) run is recorded as a future
     baseline** -- see "Coverage baseline persistence" below.
   - **Publication.** Only when the gate passes does the CLI run
     `market_pipeline/jobs/publish.py`: it re-reads the checkpointed artifact
     bodies out of the immutable raw store, normalizes them, computes returns,
     risk, benchmark RS and momentum, and calls `D1Publisher.stage` followed
     by `promote` (an atomic `active_dataset` pointer swap). The outcome is
     reported under `publication` in the CLI's JSON output (`promoted`,
     `dataset_id`, `instrument_count`, `metric_count`, `warnings`). Only
     `amfi-nav` has a wired normalization path; artifacts from a source
     without one are retained and reported as a warning rather than silently
     dropped.
7. **Save `market.db`** back to the cache (`actions/cache/save`, same
   `market-db-${{ github.run_id }}` key, `if: always()` so it runs even if
   the CLI step failed) so the next scheduled or manual run can read it back.
8. The workflow redacts the run's JSON output (stripping any key that looks
   like a token/secret/password/authorization/API key) and publishes it both
   as a build artifact (`daily-data-reconciliation-report`, 90-day retention)
   and as the job's step summary.
9. If the CLI's exit code is non-zero, the workflow step "Stop before
   promotion on blocking failure" fails the job with that same exit code.
   **No remote publication step runs after this point.**
10. On a successful local publication only, export the complete locally active
    dataset to `active-dataset.sql`; this contains only dataset-scoped market
    tables and ends with the active-pointer change. It never exports the
    global `saved_screens`, `screen_runs`, or `screen_matches` tables.
11. Export a deterministic active-object manifest: the current dataset's
    chart objects plus every yearly history object for its active instruments.
    Upload and byte-verify only those entries with explicit `wrangler r2
    object ... --remote` commands before D1 import. The workflow rejects a
    missing, malformed, or root-escaping manifest path; cached charts for old
    datasets are not re-uploaded on every run. Every active-manifest object,
    including the mutable newest history partition, is PUT and then byte-verified. A history or chart
    upload/verification failure stops the job before D1 can change. This is
    deliberately not a remote-existence probe: the newest history partition
    changes as daily points are appended, so it must be overwritten and
    verified just like a chart.
12. Import `active-dataset.sql` with `wrangler d1 execute --remote --file`,
    then query production D1 and require its `active_dataset` to equal the
    local dataset ID. The SQL importer deliberately has no `BEGIN`/`COMMIT`
    wrapper: a failed Cloudflare D1 file import restores the database to its
    original state, and the generated SQL performs the status/pointer switch
    as its final statements.
12a. **Only now** -- after that independent remote read-back confirms
    production D1's active dataset really is the one this run built --
    back up and promote this run's checkpoint archive (F12; see "Durable
    checkpoint-archive recovery" below). Building the archive earlier would
    let local checkpoint progress be mistaken for confirmed remote success;
    this ordering is the same distinction R09's reservation ledger already
    draws between "attempted" and "observed" usage.

Before any R2 or D1 **mutation**, the workflow captures production D1's
read-only `wrangler d1 info --json` response. The local preflight accepts only
non-negative numeric `database_size` and `rows_written_24h` values, never logs
the response or credentials, and validates the exported operation plan. It
counts D1 indexed-row amplification (not merely SQL statements): each
`source_runs` write includes its table, primary-key, and date-index effects;
each compact snapshot UPSERT accounts for its table/primary-key/secondary-index
write, and only the exact stale ID set contributes deletes. It rejects
more than 50,000 planned mutations per run and rejects planned mutations plus
the current 24-hour remote write count above 90,000, preserving room below the
100,000 free daily allowance for same-day retries.

The preflight also rejects a projected remote database size above 400 MB, below
the 500 MB free per-database limit. `database_size` already includes global
saved screens/runs/matches; because D1 does not reveal the reclaimable bytes of
the old active snapshot, the projection conservatively adds twice the new SQL
import (payload plus table/index overhead) and takes no storage credit for the
bounded snapshot replacement. It also projects 22 weekday runs/month and caps
R2 Class A at 800,000/month and Class B at 5,000,000/month, each below the
free-tier monthly allowance. The
remote import uses one `instrument_snapshots` JSON row per instrument rather
than the local EAV metric rows, so a typical full universe is near one snapshot
write per instrument instead of roughly a dozen metric writes per instrument.
The Worker executes saved-screen filters with checked JSON extraction from that
snapshot and retains saved screens/runs as global D1 tables.

Before preflight, the workflow reads the remote snapshot IDs and active pointer
with a production D1 read-only query. Preflight strictly validates Wrangler's
successful JSON response, derives stale deletes from the remote-minus-candidate
ID set, and rejects malformed, failed, duplicate, or non-string values. The
object manifest separates mutable charts/newest history partitions from closed
history: mutable objects always PUT+GET/compare; closed history is GET/compare
for instruments already present remotely. A true no-active-pointer bootstrap,
or a newly introduced instrument identified from the remote snapshot-ID set,
PUTs that instrument's closed history once and then GETs/compares it. Any
established-object GET failure stops publication; it is never treated as a
missing object.

The remote serving projection is intentionally bounded: `instrument_snapshots`
is keyed by `instrument_id`, and each import UPSERTs by that key then removes
only rows outside the candidate dataset before the final active-pointer
change. It therefore retains only the active universe; historical screen
matches remain in `screen_matches` and historical charts/history remain in R2.
The exporter rejects any individual SQL statement above 90,000 UTF-8 bytes
(below D1's 100,000-byte ceiling) and rejects an import above 100 MB. Before
enabling production publication, the owner must size a representative full
universe export and keep the D1 storage alert below the 500 MB free per-database
limit; monitor that usage as retained global screen history grows.

The newest local history partition in the supplied series is deliberately
mutable: the pipeline atomically replaces its yearly Parquet after rebuilding
the full series for that run. Earlier years and dataset-scoped charts retain
immutable put-if-absent behavior. The workflow PUTs and byte-verifies every
mutable active-manifest object, so the growing newest R2 object is verified
before D1's active pointer can change. Closed partitions are byte-verified on
every run and re-uploaded only for bootstrap/new-instrument initialization.

## Durable checkpoint-archive recovery

`market_pipeline.publication.checkpoint_archive` (finding F12) closes a gap
`actions/cache` alone cannot: GitHub documents cache eviction, and a miss on
this ephemeral runner previously meant the three years of `raw/` artifact
bodies and `market.db`'s checkpoint/publication history were gone with no
recovery path. This module adds a private, versioned archive in the same R2
account R08 already publishes history to (a distinct `checkpoints/`/`state/`
key prefix -- never the public `history/`/`charts/` serving keys), plus a
`state/latest-success.json` pointer.

What is archived, and what is not:

- **SQLite backup.** `market.db` is copied with SQLite's own backup API (a
  transactionally consistent snapshot, not a raw file copy) and the copy's
  checksum is recorded.
- **Immutable raw triples.** Every artifact body/metadata/commit-marker
  triple `storage/raw_store.py` already validates is uploaded and
  byte-verified under its existing key.
- **`history/` is deliberately not archived.** `jobs/publish.py` already
  rebuilds it deterministically from `raw/` on every run, so durably
  archiving it too would duplicate storage without adding recoverability.
- **The reservation ledger is deliberately not archived.** It is a same-day
  accounting margin (24-hour relevance window; see
  `market_pipeline.publication.remote_usage`) -- losing it affects only that
  margin, not correctness.

Checkpoint progress vs. remote success: uploading an archive only proves
"this local state was durably archived", never "this was published".
`state/latest-success.json` is written only by `promote_latest_success`,
and only after the caller supplies the same independently, remotely
verified active dataset ID step 12 above reads back from production D1. A
failed or not-yet-attempted remote publish leaves that pointer exactly
where it was -- it is never advanced speculatively, and reloading it after
such a failure still returns the previously verified success.

Restore behavior on the next run (workflow step 3a above): every archived
object is downloaded and byte-verified in memory before anything is written
to disk. The SQLite backup is restored into a scratch file, sanity-opened
with SQLite, and atomically renamed into place before any raw triple is
even requested. Every raw triple is then downloaded, verified, and written
into a *staging* directory next to `raw/` -- never `raw/` itself -- and
only after every single triple in the archive's manifest has been staged
does the restore atomically replace `raw/` with that fully staged directory
in one rename. This means a corrupted or interrupted archive -- including a
failure on the very last raw triple -- fails the restore step outright and
leaves `raw/` exactly as it was before the attempt (absent, on a genuine
cache miss); it can never observe a partially populated `raw/` on disk, and
so it can never be picked up and silently trusted by a later
`actions/cache/save` (`if: always()`) or restore. The one case treated as
non-fatal is `NoCheckpointArchiveError` -- no archive has ever been
promoted -- which is a legitimate first-run bootstrap, not a sign of
corruption.

A cache *hit* runs a cheap local check against the authoritative manifest:
it compares `market.db`'s active dataset ID and input-manifest hash, and
additionally confirms every raw triple the manifest lists is actually
present on disk (a fast existence check per triple, not a full re-hash --
the byte-level integrity of a present triple is still authoritatively
re-checked the moment it is read, via `LocalRawStore.get`). A detected
mismatch -- a stale cache entry from an unrelated dataset, or a `raw/`
missing artifacts from a partial cache save -- is not merely logged: the
workflow discards the untrusted local `market.db`/`raw/` outright and falls
through to the same restore a genuine cache miss would have run, so the
pipeline is never invoked against local state this check could not itself
vouch for.

Archive operations (every PUT/GET attempt, including reuse-verification
GETs for an exact retry) are folded into R09's reservation ledger the same
way `r2_sync`'s attempted counts already are, so they are visible to
`preflight.assert_plan_within_remote_budget` on the next run.

## Coverage baseline persistence

The scheduled (cron) trigger has no `workflow_dispatch` `inputs` context, so
it can never supply a `--previous-count` value the way a manual run can.
Relying on that would make the coverage-drop check a permanent no-op for
every automated run -- exactly the run this check exists to protect.

Instead, `market_pipeline/cli.py` keeps its own small `pipeline_run_history`
table inside `market.db` (scoped by command and the sorted source list).
Every invocation that ends up `safe_to_promote: true` appends its completed
count to that table. When `--previous-count` is **not** supplied, the CLI
looks up the most recent safe row for the same command/source scope in that
table and uses its completed count as the baseline; a blocked run is never
recorded, so a bad candidate can never poison the next comparison. Only when
the table has no matching row yet (a genuine first-ever run for that
command/source scope) does the CLI fall back to comparing the candidate
against itself (ratio 1.0) -- source failures and the budget check still
protect that bootstrap case.

This is why the workflow restores/saves `market.db` via `actions/cache`
around the CLI invocation (step 3 and step 7 above): the CLI's own baseline
bookkeeping only works if the database it reads from actually carries state
from the previous run. `--previous-count` remains available for manual
`workflow_dispatch` runs and historical re-runs where an operator wants to
assert a specific known-good baseline (e.g. after a rollback) rather than
trust the database's own history.

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
- Raw artifacts fetched/checkpointed during the run are retained in the
  immutable raw store (the backfill job's checkpoint table is idempotent), so
  a retry does not need to re-fetch anything that already succeeded -- see
  `docs/operations/recovery.md` for the retry command.

Note on scope: `market-pipeline daily`/`backfill` now perform the local
publication first. `safe_to_promote` is the interlock: required history/chart
objects must finish before `D1Publisher.stage` is reached, so a history
failure, blocked candidate, or failed local publication leaves the previous
local active dataset untouched. The workflow's caches therefore carry three
things between runs -- `market.db` (checkpoints and the coverage baseline),
`raw/` (immutable artifact bodies, re-read to rebuild the three-year series a
twelve-month return needs), and `history/` (published Parquet history and
chart objects). `market.db` and `raw/` also have a durable fallback beyond
that cache -- see "Durable checkpoint-archive recovery" above.

## The `compose` subcommand (S04)

`market-pipeline compose --date ... --manifest ...` is a third subcommand
alongside `daily`/`backfill`, built by S04's `jobs/composed_candidate.py`. It
is the real, reachable production entry point for that module's composed
multi-source build (`build_composed_candidate`) and schedule-driven refresh
(`run_scheduled_refresh`) -- previously that machinery existed only as a
library function exercised by its own test suite, which a task review
correctly flagged as not actually wired into anything runnable.

What it does today: it checkpoints artifacts from the same manifest format
`daily` uses (identical `--source`/`--manifest`/`--previous-count`/
`--min-coverage-ratio` flags), builds an explicit per-source schedule from
the requested `--source` list and this pipeline's own source calendar
(`_expected_date`), and publishes through `run_scheduled_refresh` -- which
persists each source's `SourceStatus` (`complete`/`delayed`/`failed`/
`not_expected`, with its own `expected_date`/`loaded_date`) independently,
so one source's delay never masks, or is blocked by, another source's
health. Its JSON report's `refresh` key carries `status`
(`"published"`/`"blocked"`/`"failed"`/`"skipped"` -- `"skipped"` only when
nothing in the schedule is expected to publish that day at all, e.g. a
weekend) and `source_status`, alongside the same `start`/`end`/`completed`/
`missing_dates`/`warnings`/`budget` fields `daily`/`backfill` already report.

Final-review fix (this fix wave): `compose` previously never supplied real
per-source instrument counts into its own coverage check, so the R01
coverage-drop gate was either structurally inert (no `--previous-count`:
ratio always compared the candidate against itself) or a guaranteed false
block (`--previous-count` supplied: every run compared a real baseline
against a candidate count of zero). The R09 D1 storage-budget gate was also
only computed for the JSON report, after publication would already have
happened, rather than gating it. Both are now real: `compose` reads/writes
the same per-source coverage baseline table `daily`/`backfill` use, computes
real instrument counts from the composed build's own instrument table, and
applies the storage-budget check inside `run_scheduled_refresh` before
`publish_checkpointed_dataset` runs -- `compose` now has the same coverage/
budget promotion-gate guarantees `daily` already had.

Requesting only `--source amfi-nav` (the default, and today's only
production source) behaves identically to `daily`'s own promotion outcome:
both build through `build_composed_candidate`, whose AMFI-only path is a
proven byte-for-byte passthrough of the pre-existing, unmodified
`build_candidate` (see
`pipeline/tests/integration/test_multi_source_publication.py::test_amfi_only_composition_is_a_pure_passthrough`).
`daily`/`backfill` themselves were also changed to build every candidate
through `build_composed_candidate` rather than calling `build_candidate`
directly (again, an equivalent-for-amfi-only passthrough) -- see
`pipeline/tests/integration/test_cli.py::test_daily_command_actually_invokes_build_composed_candidate`.

**What remains pending**: this command cannot yet admit real NSE
(`nse-eod`/`nse-filings-xbrl`) or compose a real filings/benchmark reference
build, because those sources' `SOURCE_POLICIES` entries still have
`supplied_use_allowed=False`/`automation_allowed=False` (unchanged by S04,
correctly -- see `docs/operations/source-policy.md`). This command and the
underlying `SourceInput`-based admission path (S01) are what a future change
would wire real NSE inputs through, once an operator/owner records a real
supplied-use permission or a verified official automatic download path; no
such change is made here. `.github/workflows/daily-data.yml` still invokes
`daily`/`backfill` only -- wiring the scheduled workflow itself to `compose`
(and to a real multi-source manifest format) is deliberately left as a
separate, larger, separately-reviewable change.

## Production Cloudflare publication: one-time operator setup

This implementation made **no real Cloudflare API call, deployment, D1
import, or R2 upload**. Remote publication begins only after the owner creates
the following production resources and supplies environment-scoped account
configuration:

| Item | Exact value / minimum scope |
| --- | --- |
| D1 database | Create `stonks-research`; replace the production `database_id` placeholder in `apps/api/wrangler.jsonc` with its real ID before running the workflow. |
| R2 bucket | Create the private bucket `stonks-private-history`; keep public access disabled. It is the production `CHARTS` binding. |
| GitHub Environment | Use `daily-data-refresh`, with `CLOUDFLARE_API_TOKEN` as a secret and `CLOUDFLARE_ACCOUNT_ID` as an environment variable. Do not put either in a manifest, repository variable, or workflow output. |
| API token | Scope it to this account and only `stonks-research` / `stonks-private-history`: `Account.D1:Edit` on that D1 database, `Account.R2:Edit` on that R2 bucket, and `Account:Read` for Wrangler account resolution. It needs no Worker Scripts, Zone, Access, KV, or unrelated database/bucket permission. |

The workflow always passes `--env production`, uploads through the literal
`stonks-private-history` bucket path, and imports into the literal
`stonks-research` D1 name. Preview resources (`stonks-research-preview` and
`stonks-private-history-preview`) are not in this data-publication path.
This job still has no automated NSE collection: it can publish only artifacts
the governed local pipeline admitted from an operator-supplied manifest.

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
| `previous_count` | Known-good baseline completed-artifact count for the coverage check. Optional: omitting it does not skip the check -- the CLI falls back to the last safe-to-promote run recorded in `market.db` for the same command/source scope (see "Coverage baseline persistence" above), or to a self-consistent ratio of 1.0 only on a genuine first-ever run |
| `min_coverage_ratio` | Override the default `0.90` threshold if a run has a known, deliberate coverage change |

This is also how you re-run a specific historical date or range -- see
`docs/operations/recovery.md` for the exact commands.
