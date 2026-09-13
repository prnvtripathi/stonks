# Launch checklist

Maps every acceptance criterion in
`docs/superpowers/specs/2026-09-07-indian-stocks-research-dashboard-design.md`
("Acceptance Criteria") and every bullet in Task 14's "Final Verification
Gate" to either a named automated test or an explicit manual verification
step. Nothing here is claimed as automated unless a command is given that
actually exercises it.

**9 September 2026 update:** the sections below this point were added by the
Correctness and publication remediation plan's phase acceptance gate, run
once after all twelve R01-R12 tasks
(`docs/superpowers/plans/2026-09-09-correctness-and-publication-remediation.md`)
were complete and reviewed clean. They map that plan's F01-F15 findings to
evidence, record the chained-scenario and capacity-measurement decisions the
gate's brief required, and list every gate item that still needs a live
Cloudflare account and cannot be satisfied from this environment. The
original Task 14 content above is left unmodified as a historical record;
where the two disagree (for example acceptance criterion 11's "authenticated
deployment smoke test" row), the F01-F15 table below is the current, more
complete evidence -- R12 hardened exactly that check (F15). Full command
output and pass counts are in
`.superpowers/sdd/2026-09-09-correctness-and-publication-remediation/task-PhaseGate-report.md`.

## Specification acceptance criteria

| # | Criterion | Automated test | Manual verification |
| - | --- | --- | --- |
| 1 | Three years of supported history are loaded within documented source permissions | `pipeline/tests/integration/test_backfill.py::test_default_backfill_is_three_calendar_years_to_latest_complete_date` covers only the **date-range arithmetic** of the default three-year window, not data loading. Actual loading is covered by `pipeline/tests/integration/test_end_to_end_publication.py::test_manifest_flows_from_raw_artifact_to_a_promoted_queryable_dataset`, which drives 300 dated artifacts from a manifest through the raw store, normalization, and analytics into a promoted dataset with real twelve-month returns | Manual, pre-launch: a full three calendar years of real AMFI artifacts has to be supplied by the operator (no automated fetch is wired -- see `docs/operations/source-policy.md`). Run `uv run market-pipeline --db market.db backfill --start <today-3y> --end <yesterday> --manifest <manifest>` and confirm `publication.instrument_count` and the resulting `latest_metrics` coverage against the AMFI file |
| 2 | Every active NSE `EQ` ordinary share and NSE-listed ETF has a stable instrument record | `pipeline/tests/domain/test_models.py::test_instrument_identity_is_stable_and_asset_scoped` | -- |
| 3 | Every current AMFI scheme has a stable scheme record and current available NAV | `pipeline/tests/normalization/test_amfi.py::test_correction_supersedes_original_with_lineage`, `pipeline/tests/sources/test_amfi_nav.py` | -- |
| 4 | The representative screening query returns independently verified matches | `packages/query/src/query.test.ts` (parses/typechecks the plan's representative query: `Return over 1day > 3% AND Volume > 500000 AND Volume > Volume 1week average * 1.5 AND Market Capitalization > 500`), `apps/api/src/index.test.ts::"returns active-dataset results with clause and momentum explanations"` | **Manual, pre-launch.** The CLI does not run screens or emit matches -- it ingests, computes, and promotes a dataset; screens are executed by the Worker (`POST /api/v1/screens/:id/runs`). To verify by eye: run `uv run market-pipeline --db market.db daily --date <fixture date> --manifest <fixture manifest>` to promote a hand-verified fixture dataset, then run the screen through the API against that dataset and compare the returned match list with the source file (see docs/operations/recovery.md for the CLI invocation shape) |
| 5 | Both RS calculations match reference fixtures, and every score exposes its component values | `pipeline/tests/analytics/test_rs.py` (`test_benchmark_rs_returns_a_fraction_like_every_other_percent_metric`, `test_recent_quarter_has_double_weight`, `test_equity_ratings_are_one_to_ninety_nine_with_stable_ties`, `test_equity_rating_uses_last_price_on_or_before_effective_date`), `pipeline/tests/analytics/test_momentum.py::test_serializes_metric_rows_with_auditable_component_provenance` | -- |
| 6 | Every displayed market or fundamental value has an effective date and source lineage | `pipeline/tests/domain/test_models.py::test_observation_keeps_provenance_and_effective_date`; UI: `apps/web/src/app.tsx`'s `SourceCard` component (Expected/Loaded `<dt>`/`<dd>` pairs) and `apps/web/src/research.tsx`'s momentum breakdown (`Source date`, `Formula` fields) | -- |
| 7 | Failed or incomplete ingestion cannot replace the last known-good data | `pipeline/tests/integration/test_publication.py::test_incomplete_candidate_cannot_promote`, `pipeline/tests/integration/test_cli.py::test_coverage_drop_against_previous_count_blocks_promotion` | Force a rollback per `docs/operations/recovery.md` section 2 against a scratch `market.db` and confirm `active_dataset` still points at the prior good dataset after an interrupted run (see "Verification run" below for the exact commands executed for this task) |
| 8 | A typical saved screen returns within two seconds under personal-use load | -- (no load-test harness exists; personal-use traffic on a single-owner Worker plus D1 has no meaningful queueing to model) | Manual, post-launch: time `GET /api/v1/screens/:id/results` against production once real data is loaded (e.g. `curl -w '%{time_total}\n'` with a valid Access session) and confirm it is comfortably under 2s |
| 9 | Desktop and mobile views are usable and accessible by keyboard | `apps/web/src/app.test.tsx` (keyboard-driven interactions), `apps/web/e2e/task11.spec.ts` (390px viewport), `apps/web/e2e/task14.spec.ts::"screens work at 320px"` (new, this task -- 320px is CSS's conventional minimum-supported mobile width) | -- |
| 10 | Monitored daily work remains inside Cloudflare and GitHub free-tier allowances | `pipeline/tests/monitoring/test_budgets.py` | Review `.github/workflows/daily-data.yml`'s job-summary budget line (`Storage budget: ... of limit`) after each real run; GitHub Actions minutes are visible in the repository's Settings > Billing (this repo's daily job runs well under the free 2,000 min/month) |
| 11 | Every route rejects unauthenticated access | `apps/api/src/index.test.ts::"rejects missing Access identity"` (in-process unit test), `apps/api/e2e/access-denial.test.ts` (genuine loopback HTTP request, no Authorization/Access header, real socket), and, as of Fix Round 1, `.github/workflows/deploy.yml`'s preview and production smoke-test steps each run `scripts/ci/assert-anonymous-denied.sh` (covered by `apps/api/e2e/assert-anonymous-denied.test.ts`) against the just-deployed hostname on every push to `main` -- this fails the deploy if a genuinely anonymous request to the live, deployed environment ever gets a bare 200, catching a fail-open regression (Access misconfigured to allow-all, or `accessVerifier()` accidentally removed) that the pre-existing unit/loopback tests cannot see because they never touch a real deployed hostname | Manual, pre-launch (needs a live Access application, not available in this environment): confirm an anonymous browser request to the production hostname is redirected to Access's login page for both `/` (SPA shell, served via Workers Static Assets) and `/api/v1/status` (Worker code) |

## Final Verification Gate (Task 14 brief)

| Item | Status |
| --- | --- |
| Map every acceptance criterion to a test or manual result | This document, table above |
| `git diff --check` shows only intentional changes | Run as part of "Verification run" below; see that section's output |
| Every enabled adapter uses a current official documented URL and reviewed source policy | Cross-checked `docs/operations/source-policy.md` against `pipeline/tests/sources/test_registry.py::test_registry_contains_only_approved_official_sources` (which asserts every registered source's URL host is one of `www.nseindia.com`, `www.amfiindia.com`, `www.niftyindices.com`, and that NSE automation stays disabled pending written permission) -- both agree: `amfi-nav` and `nifty-500` automation enabled, `nse-eod`/`nse-filings-xbrl` disabled pending permission. This is now also re-run as a named gate step in `.github/workflows/deploy.yml` ("Validate source policy") before every deploy. |
| Independent NSE, filings, AMFI, and benchmark dates appear in production | `apps/web/src/app.tsx`'s `Overview` component renders one `SourceCard` per entry in `status.sources` (line ~145, `<h2 id="health-title">Source health</h2>` / `"Independent freshness by source"`), each showing its own `Expected`/`Loaded` dates and coverage independently -- a delayed AMFI file cannot borrow NSE's date, per `StatusDto.sources` being a per-source array end to end from `apps/api/src/index.ts`'s `/api/v1/status` handler. Verified by reading the component; full independence across all four source IDs in a live dataset needs a populated production D1 (**manual, post-first-real-run**). |
| Confirm anonymous page, API, and R2 requests fail | **API:** automated, `apps/api/e2e/access-denial.test.ts` (new, this task) -- genuine anonymous HTTP request over a real loopback socket returns 401. **Page:** configuration-dependent on a live Cloudflare Access application bound to the production hostname (see security-checklist.md section 4); cannot be exercised without one. **R2:** configuration-dependent on the bucket's "Public access" setting remaining disabled (security-checklist.md section 4); `apps/api/src/repositories.ts`'s `D1ResearchStore` only ever reaches R2 through the Worker's own `CHARTS` binding, never a public URL, so the application code has no path that could leak a public R2 URL even if the bucket setting were misconfigured -- but the bucket setting itself is an account-console toggle with no file this repo can lint. |
| Copy consistently says "top matches" and displays the research disclaimer | Confirmed by reading: `apps/web/src/app.tsx:119` renders `<Disclosure effectiveDate={status.effectiveDate} />`; `apps/web/src/app.tsx:177` is `Disclosure`'s definition -- `"Research tool, not investment advice."` plus `"Mechanical matches do not guarantee returns. Review source dates before acting."`, shown on every view (it is rendered once, outside the view switch). "Top matches" appears at `apps/web/src/app.tsx:142` (saved-screen summary: `"{totalMatches} top matches"`) and `apps/web/src/app.tsx:158` (`ScreenCard`'s `"Top matches"` stat label) and `apps/web/src/research.tsx:92` (results header: `"{result.run.matchCount} top matches"`). `grep -rn "recommended buy\|buy recommendation" apps/web/src apps/api/src` returns no matches. |
| No portfolio, trading, real-time, public-sharing, US-market, or generative-AI capability entered scope | `grep -rniE "portfolio|brokerage|order|trade execution|real-?time|intraday|public.shar|generative|openai|anthropic|gpt" apps/web/src apps/api/src pipeline/market_pipeline` was run for this task and returns no matches other than this checklist's own wording and the spec's explicit exclusions section; the query language (`packages/query/src`) has no fields for order size/price, the API has no `/portfolio` or `/orders` route (`apps/api/src/index.ts`'s `allowedMethodsFor` is an exhaustive allowlist), and the glossary content is static, reviewed markdown-shaped data (`apps/api/src/repositories.ts`), never a model call. |

## Recorded deviations from the plan's declared tech stack

The plan's **Tech Stack** line names Hono, TanStack Query/Table, and
lightweight-charts. None of the three is used. These were deliberate
substitutions, recorded here so they are decisions rather than gaps found
later:

| Declared | Used instead | Why |
| --- | --- | --- |
| Hono | Hand-rolled routing in `apps/api/src/index.ts` (`allowedMethodsFor` plus an explicit path/method match chain) | The API is ~15 routes with no middleware stack beyond Access verification and security headers. An exhaustive, hand-written method allowlist is directly auditable against the security checklist -- a property that matters more here than router ergonomics -- and it removes a dependency from the request path of a Worker that must fail closed. |
| TanStack Query / TanStack Table | Direct `fetch` through `apps/web/src/api.ts` with local `useState`/`useEffect`, and plain `<table>` markup in `apps/web/src/research.tsx` | The dataset is end-of-day and immutable per published dataset ID, so there is nothing to invalidate, refetch on focus, or reconcile optimistically -- the caching that justifies TanStack Query has no work to do. Sorting and pagination are server-side (`?sort=`/`?limit=`/`?offset=`), so the table is a render of an already-ordered list; a semantic `<table>` with real `<th scope>` headers is also easier to keep keyboard- and screen-reader-accessible than a virtualized headless table. |
| lightweight-charts | Hand-written inline SVG polyline in `apps/web/src/research.tsx`'s `PriceChart` | Recorded in the Task 11 ruling: the pipeline publishes a single adjusted close/NAV per session, not OHLC bars. A financial charting library's candlestick/volume affordances would imply intraday precision this data does not have. The SVG line chart renders exactly the series that exists, and is paired with the underlying data table for non-visual access. |

Revisiting any of these is a scoped change, not a rewrite: the API's routing
is isolated in one file, the web client's data access is isolated in
`apps/web/src/api.ts`, and `PriceChart` is a single component.

## Verification run

Commands actually executed for this task, in this environment (no live
Cloudflare account -- see deploy.md for what that excludes):

```bash
uv run ruff check . && uv run mypy pipeline && uv run pytest
pnpm lint && pnpm typecheck && pnpm test   # `pnpm lint` is now ESLint, not a second tsc pass
pnpm --filter @stonks/web test:e2e
cd apps/api && npx vitest run --config vitest.config.ts   # includes the new e2e/access-denial.test.ts
python3 -c "import yaml; yaml.safe_load(open('.github/workflows/deploy.yml'))"
cd apps/api && npx wrangler deploy --dry-run --outdir /tmp/wr-preview --env preview
cd apps/api && npx wrangler deploy --dry-run --outdir /tmp/wr-production --env production
git diff --check
```

See this task's report (`.superpowers/sdd/2026-09-07-indian-stocks-research-dashboard/task-14-report.md`)
for full output and the rollback-drill transcript.

## Phase acceptance gate (9 September 2026) — F01-F15 evidence map

Source of the F01-F15 finding IDs:
`docs/reviews/2026-09-09-implementation-audit.md`. F16 belongs to the
companion original-source-scope-completion plan (S01-S04), not this gate.

| Finding | Remediation | Test/evidence | Status |
| --- | --- | --- | --- |
| F01 reconciliation measured downloads, not the published universe | R01 | `pipeline/tests/integration/test_candidate_gate.py`, `test_cli.py`, `test_publication.py`, `pipeline/tests/validation/test_reconcile.py` (59 passed as a group; full suite 299 passed) | Closed |
| F02 required corrupt inputs became warnings; an older date published | R01 | `test_candidate_gate.py::test_malformed_final_artifact_blocks_and_preserves_the_active_dataset`, `test_malformed_intermediate_artifact_blocks_and_preserves_the_active_dataset`, `test_corrupt_checkpoint_checksum_blocks_and_preserves_the_active_dataset` | Closed |
| F03 changed historical inputs did not change dataset identity | R02 | `pipeline/tests/integration/test_input_identity.py::test_historical_backfill_changes_dataset_identity_and_metric_lineage`, `test_dataset_fingerprint_is_order_independent_and_tracks_raw_and_formula_inputs` | Closed |
| F04 one-year risk values used all available history | R03 | `pipeline/tests/analytics` risk-window tests, re-verified against `pipeline/tests/integration/test_end_to_end_publication.py`'s real 300-session dataset | Closed |
| F05 MF benchmark RS was a category average passed off as an official benchmark | R03 | `pipeline/tests/analytics/test_rs.py`; `test_end_to_end_publication.py` asserts every `benchmark_rs_*` row is `state == "missing"` with `reason == "official benchmark mapping is unavailable"` (real official mapping deferred to S03) | Closed for this plan's scope; official mapping is separate scope (S03) |
| F06 production momentum explanations lost weight/contribution/unit/coverage metadata | R04 | `apps/api/src/publication-contract.test.ts` (compact export → D1 schema → Worker decode, real `export_active_dataset` via `compact_momentum_fixture.py`); extended this gate by `pipeline/tests/integration/test_end_to_end_publication.py`'s new assertion block (raw AMFI files → real analytics → real `export_active_dataset` → fresh D1 schema, asserting `momentum_three_month_return`'s `unit`/`weight`/`contribution`/`coverage`) -- see "Cross-boundary fixture" below | Closed |
| F07 historical results could 404, mislabel their date, or relabel under an edited screen | R05 | `pnpm --filter @stonks/api test`, `pnpm --filter @stonks/web test` (immutable saved-run tests) | Closed |
| F08 pagination loaded all history and performed N+1 lookups | R06 | `apps/api/src/result-pagination.test.ts` (17 tests, incl. the 12,000-instrument warmed timing + `EXPLAIN QUERY PLAN` baseline) | Closed |
| F09 the R2 verification gate could succeed after its manifest producer failed | R08 | `pipeline/tests/integration/test_r2_sync.py::test_cli_failure_never_lets_a_subsequent_d1_import_step_run` | Closed |
| F10 upload runtime was unbounded; the partial replacement was unwired | R08 | `test_r2_sync.py::test_sync_at_scale_bounds_concurrency_for_many_instruments_and_partitions` (12,000 instruments × 4 partitions = 48,000 objects) | Closed |
| F11 the capacity gate accepted unavailable analytics as zero usage | R09 | `pipeline/tests/integration/test_remote_usage.py` (validated-telemetry rejection cases, 12,000-instrument rolling-three-year authorization case) | Closed |
| F12 raw inputs and recovery state depended on an evictable Actions cache | R10 | `pipeline/tests/integration/test_checkpoint_restore.py::test_checkpoint_archive_restores_full_history_after_total_local_loss`, `test_cache_hit_still_validates_local_identity_against_the_authoritative_manifest` | Closed |
| F13 pointer-only rollback was incompatible with the bounded projection | R11 | `pipeline/tests/integration/test_remote_restore.py::test_pointer_only_rollback_returns_zero_serving_rows` (reproduces the break), `test_selective_restore_reimports_projection_and_preserves_post_promotion_product_state` (the fix, against real D1 migrations 0001-0008) | Closed |
| F14 history/retention/quota assumptions did not cover repeated operation | R07, R09, R10 | `pipeline/tests/integration/test_history_store.py`, `test_publication_bundle.py`, `test_remote_usage.py`'s retained-objects/retained-bytes accounting | Closed |
| F15 authenticated deployment smoke tests accepted a broken login path | R12 | `apps/api/e2e/assert-anonymous-denied.test.ts` (9 tests), `apps/api/src/middleware/access.test.ts` (7 tests); workflow smoke-test steps require strict 200 + `jq`-validated status-JSON shape | Closed |

## Cross-boundary fixture: raw files → candidate → R07 export → D1 → Worker

The brief required an executable fixture spanning raw source files →
normalized candidate → R07 bundle/export → local D1-compatible repository →
Worker status/screen/result/chart, including the R04 momentum metadata
specifically. No single existing test spanned every hop with the exact R04
fields, so this gate extended
`pipeline/tests/integration/test_end_to_end_publication.py::test_manifest_flows_from_raw_artifact_to_a_promoted_queryable_dataset`
(previously: raw AMFI files → normalization → analytics → local D1Publisher
promotion, asserting `momentum_score`'s cohort but not the per-component
weight/contribution/unit/coverage, and never calling
`d1_export.export_active_dataset`) with a new block that:

1. backfills a second, smaller (70-day) real AMFI manifest through the same
   raw-store/normalization/analytics path,
2. calls the real `market_pipeline.publication.d1_export.export_active_dataset`
   against that promoted local database,
3. replays the generated SQL into a fresh `D1Publisher().initialize_schema()`
   database (the same production schema R11's restore tests use), and
4. asserts the `momentum_three_month_return` metric row's `metadata_json`
   still has `unit == "percent"`, `weight == 0.15`,
   `contribution == normalized * weight`, and `0 <= coverage <= 1` after
   that export/decode round trip.

That closes the raw-file → real-export leg. The export → Worker-TypeScript
decode leg (the actual F06 regression site) was already fully covered by
`apps/api/src/publication-contract.test.ts`, which feeds a real
`export_active_dataset` output (via `compact_momentum_fixture.py`) into
`D1ResearchStore.instrument()` and asserts the identical fields survive the
Worker's decoder, including the malformed/legacy-string-metadata edge cases.
Between the two, every hop in the brief's chain is exercised with real
production code and the specific R04 fields are asserted at both boundary
crossings.

A separate 300-artifact fixture in the same test file, used for the
file's original assertions (twelve-month returns, chart objects, etc.),
was deliberately **not** reused for this new export call: doing so
surfaces a genuine, previously-undetected defect (see "New finding" below),
so the extension uses its own smaller manifest instead of masking that
defect by construction.

## New finding: dataset metadata can exceed the D1 per-statement export limit

While building the cross-boundary fixture above, exporting the *existing*
300-artifact end-to-end fixture (one artifact per calendar day) through the
real `export_active_dataset` failed with `DatasetExportError: remote D1
statement exceeds 90000 UTF-8 bytes`. Root cause: R02's dataset fingerprint
(`market_pipeline/jobs/publish.py`'s `manifest_with_fingerprint`) embeds the
**full** input manifest -- every contributing artifact's id, checksum,
adapter version, and raw object key -- verbatim into
`datasets.metadata_json`, and `d1_export.py`'s pre-existing
`_MAX_D1_STATEMENT_BYTES = 90_000` guard (predates this remediation plan)
rejects the resulting `INSERT ... INTO datasets` statement once artifact
count is in the hundreds. A genuine three-calendar-year daily backfill
(acceptance criterion 1, ~750+ daily AMFI artifacts) will contain far more
artifacts than the 300 in this fixture and, on today's code, would very
likely trip this same guard on every export -- blocking the exact backfill
this dashboard exists to serve.

This was found by this gate's own composition testing, is not one of
R01-R12's tested scenarios, and was not introduced by this gate (the
manifest-embedding behavior is R02's; the byte guard predates the whole
remediation plan). Per the phase-gate brief ("do not re-implement
functionality R01-R12 already built and tested" / this is a
verification-and-documentation pass, not a new fix-loop), this gate does
not attempt a fix here. **This is a blocking pre-launch defect against
acceptance criterion 1** and must be resolved (for example: store only the
manifest's hash plus a pointer to an R2-held manifest object, rather than
the manifest body, in `datasets.metadata_json`) before a real three-year
backfill is attempted. Tracked here rather than silently worked around.

## Workflow fragility fixed during this gate (no product behavior change)

Running the full Python suite together (rather than each R-task's scoped
subset) surfaced 3 failing tests in
`pipeline/tests/integration/test_checkpoint_archive_workflow.py`. Cause: R12
added a comment to `.github/workflows/daily-data.yml`'s "Resolve run
parameters" step that happened to contain the literal string "Run
market-pipeline refresh" (the name of a *later* step), before that step's
own heading appears in the file. The tests locate step boundaries with
`str.index(...)` without a search-start offset, so `workflow.index("Run
market-pipeline refresh")` matched the earlier comment instead of the real
step heading, producing an empty slice and failing three order/content
assertions that were never actually violated. Fixed by rewording the R12
comment (`.github/workflows/daily-data.yml`, "Resolve run parameters" step)
to reference "the pipeline invocation step further below" instead of
repeating the exact heading text -- no workflow behavior changed. This is
exactly the class of composition-only defect the phase gate exists to catch:
each of R10 and R12 passed their own scoped test runs cleanly in isolation.

## Chained scenario: two dates, retry, corruption, cache/R2/D1 failure, rollback

The brief's most demanding item asks for one scenario exercising, in
sequence: two consecutive publish dates, an identical retry, added
historical inputs, a required-input corruption, local cache loss, an R2
failure, a D1 import failure, and selective rollback -- asserting active
dataset, source dates, raw/input lineage, saved-run source/date, and match
identity throughout.

**Decision: route (b), a composition argument, not a new chained test.**
Each stage already has direct, real-code coverage (cited below), each
using the same shared in-memory fake object-store class the R08/R10/R11
test files explicitly document sharing (`test_remote_restore.py`'s
docstring: "the same private, in-memory stand-in ... that
`test_checkpoint_restore.py`/`test_r2_sync.py` already use"). Chaining all
eight stages into one literal test would require driving the CLI's
`backfill`/`daily` commands, `checkpoint_archive`'s
backup/restore/promote CLI, `r2_sync`'s CLI, and `restore.py`'s Python API
against one shared SQLite database and one shared fake object store across
every stage transition -- four different entry-point shapes that were each
independently designed and reviewed to be exercised through their own
harness. Building and maintaining a fifth, combined harness duplicating all
four would itself be a large new implementation (roughly the size of a
13th remediation task) with its own bug surface, for marginal additional
assurance: the properties the brief wants proven (active dataset, source
dates, lineage, saved-run identity, match identity) are each already proven
at the exact state transition where they could break, using database state
that persists to disk between test steps the same way it would in
production (not mocked away) -- so passing them independently already
proves each transition preserves those invariants; the only additional
thing a combined test could show is that unrelated fixtures don't corrupt
shared Python-process globals, and none of these modules hold that kind of
shared mutable state (each opens its own `sqlite3` connection to a
`tmp_path`-scoped database file).

Stage-by-stage evidence:

| Stage | Test(s) | File |
| --- | --- | --- |
| Two consecutive publish dates | `test_historical_backfill_changes_dataset_identity_and_metric_lineage` (publishes date A, then backfills earlier dates, re-verifying A's lineage) | `pipeline/tests/integration/test_input_identity.py` |
| Identical retry | `test_identical_retry_reuses_the_same_successful_candidate`, `test_identical_retry_does_not_inflate_the_next_candidate_baseline` | `pipeline/tests/integration/test_candidate_gate.py` |
| Identical retry (checkpoint idempotency) | `test_backfill_checkpoints_make_retries_idempotent`, `test_matching_retry_skips_migrated_legacy_checkpoint` | `pipeline/tests/integration/test_backfill.py` |
| Identical retry after a failed remote publish | `test_exact_retry_after_a_failed_remote_publish_never_disturbs_prior_success` | `pipeline/tests/integration/test_checkpoint_restore.py` |
| Added historical inputs change dataset identity | `test_historical_backfill_changes_dataset_identity_and_metric_lineage`, `test_dataset_fingerprint_is_order_independent_and_tracks_raw_and_formula_inputs` | `pipeline/tests/integration/test_input_identity.py` |
| Required-input corruption blocks and preserves the active dataset | `test_malformed_final_artifact_blocks_and_preserves_the_active_dataset`, `test_malformed_intermediate_artifact_blocks_and_preserves_the_active_dataset`, `test_corrupt_checkpoint_checksum_blocks_and_preserves_the_active_dataset` | `pipeline/tests/integration/test_candidate_gate.py` |
| Local cache loss → remote-checkpoint restore | `test_checkpoint_archive_restores_full_history_after_total_local_loss`, `test_cache_hit_still_validates_local_identity_against_the_authoritative_manifest`, `test_restored_raw_artifacts_preserve_exact_source_provenance` | `pipeline/tests/integration/test_checkpoint_restore.py` |
| R2 failure fails closed before any D1 mutation | `test_sync_fails_closed_on_persistent_transport_error`, `test_sync_exhausts_bounded_retries_and_reports_the_underlying_cause`, `test_cli_failure_never_lets_a_subsequent_d1_import_step_run` | `pipeline/tests/integration/test_r2_sync.py` |
| D1 import / restore fails closed on a corrupt or unverifiable object | `test_restore_fails_closed_before_mutation_when_local_object_is_corrupt`, `test_restore_fails_closed_before_mutation_when_remote_object_verification_fails`, `test_restore_rejects_a_transport_failure_via_wrapped_r2sync_error` | `pipeline/tests/integration/test_remote_restore.py` |
| Selective rollback (not pointer-only) preserves saved-run/product state | `test_pointer_only_rollback_returns_zero_serving_rows` (reproduces the old break), `test_selective_restore_reimports_projection_and_preserves_post_promotion_product_state` (import A → saved screen/run → import B → edit screen/rerun → restore A → A's data back and B's screen edit/run survive, against real D1 migrations 0001-0008) | `pipeline/tests/integration/test_remote_restore.py` |
| Match identity survives across dataset changes | `apps/api/src` immutable-saved-run tests (R05) cover match identity under a live dataset change. **`screen_matches` surviving a restore specifically is an uncovered gap, corrected here after review**: `test_remote_restore.py::test_selective_restore_reimports_projection_and_preserves_post_promotion_product_state` only seeds and asserts `saved_screens`/`screen_runs` post-restore; it never seeds a `screen_matches` row and never re-reads one after `restore_dataset`. The only existing `screen_matches` evidence is *structural*, not a round-trip: `test_publication.py`'s `export_active_dataset` test asserts the literal string `"screen_matches"` never appears in the generated export SQL (i.e. export/restore cannot touch the table at all, by construction), and R11's own ledger entry (`docs/reviews/2026-09-09-remediation-progress.md`) already recorded this same gap as a deferred minor ("screen_matches non-interference proven structurally ... rather than by a dedicated round-trip test"). No existing test seeds `screen_matches`, performs a restore, and re-verifies those rows. This is a real, currently-open coverage gap, not a proven guarantee. | `apps/api/src/index.test.ts`; gap: `test_remote_restore.py`, `test_publication.py` (structural only) |

If a future task does chain these into one scenario, it should build on
`test_remote_restore.py`'s shared `_FakeObjectStore` and `_candidate()`
helpers rather than reimplementing them, and should budget for the
`datasets.metadata_json` size defect noted above once historical-input
manifests get large enough to hit it.

## Consolidated capacity record: 12,000 instruments, four calendar partitions

Aggregated from R06/R08/R09/R10's own measurements (not re-measured here,
per the brief); all figures are from fake/local harnesses unless noted, and
none are claimed as live Cloudflare account measurements.

**Local (Worker/D1) query latency** — `apps/api/src/result-pagination.test.ts`:
- `runScreen` over 12,000 warmed instrument snapshots: 50.35ms, 5 SQL
  statements, `EXPLAIN QUERY PLAN` shows indexed `SEARCH` (no full scans) —
  committed baseline, re-verified this gate.
- `pageRunMatches` over 12,000 matches: 0.63ms.

**Remote (R2) transport throughput/scheduling** —
`pipeline/tests/integration/test_r2_sync.py::test_sync_at_scale_bounds_concurrency_for_many_instruments_and_partitions`:
- 12,000 instruments × 4 calendar-year partitions (2021-2024) = 48,000
  objects, `max_workers=16`, `deadline_seconds=120`.
- Scheduler bookkeeping alone (near-zero fake network latency): ~11.14s
  standalone per R08's fix-round-1 note; this gate's full-suite run
  completed the whole 299-test Python suite (including this case) in
  24.55s.
- This proves the sliding-window scheduler/thread-pool bookkeeping does not
  degrade at 48,000 objects; it is not a live-network throughput estimate
  (documented explicitly in the test itself).

**Remote budget/quota accounting, including retries/archive/retention** —
`pipeline/tests/integration/test_remote_usage.py::test_evaluate_publication_attempt_authorizes_a_12000_instrument_rolling_three_year_plan`:
- Rolling-three-year plan for 12,000 instruments: current-year mutable
  chart+history per instrument, plus two prior immutable history years
  (48,000 retained history/chart objects total, modeled at 64KB/object ≈
  3.0GB retained bytes).
- Modeled `d1_import_bytes = 12,000 × 2,048 = 24,576,000` bytes (~24.6MB);
  `snapshot_bytes = 12,000 × 180 = 2,160,000` bytes (~2.16MB).
- Authorized outcome's `resolved_plan["d1_mutations"] < 50,000` (Cloudflare
  D1's per-invocation/db-side statement ceiling headroom).
- Monthly attempt budget = actual scheduled weekday count for the month
  (`scheduled_dates_in_month`, not a hard-coded 22) **plus a fixed
  `reserved_manual_attempts` reservation** for manual `workflow_dispatch`
  reruns/retries -- this is where retry overhead is budgeted.
- **Archive overhead**: R10's checkpoint backup/restore put/get counts are
  folded into the same reservation ledger R09 built (`docs/reviews/2026-09-09-remediation-progress.md`'s
  R10 entry: "both folding their put/get counts into R09's reservation
  ledger"), so archive operations are not a separate, unbudgeted cost.
- **Retention overhead**: retained-object/byte accounting above comes from
  R07's `plan_garbage_collection`/`reachable_bundles` (reachability-based
  retention, not unbounded accumulation), consumed directly by the same
  `_publication_plan` this test exercises.

**Gap check**: the brief asked to flag anything not already covering
"retries/archive/retention overhead together." None found — retries are
budgeted via `reserved_manual_attempts` plus r2_sync's own real
attempted-put/get counts (R09 fix round 1), archive backup/restore
consumes the same ledger (R10), and retention is reachability-bounded (R07)
and fed into the same plan this capacity test authorizes. No new
measurement was taken for this gate.

## Live acceptance gates — explicitly pending

None of the following can be performed from this environment (no real
Cloudflare account, credentials, or deployed Worker). They are named
external dependencies, not defects, and must be completed by someone with
account access before a real production launch:

1. **Preview exercise with real Cloudflare bindings** (brief's final
   bullet): authenticated success, anonymous denial, verified data import,
   failed-import rollback, and actual telemetry/throughput against a real
   preview deployment and a small permitted dataset. Dry-runs (below)
   cannot satisfy this — it requires a real deployed Worker, real D1/R2
   bindings, and a real Cloudflare Access application.
2. **`wrangler deploy --dry-run`** (preview and production) was run this
   gate as packaging evidence only (`cd apps/api && npx wrangler deploy
   --dry-run --outdir <dir> --env preview|production`); this repo has no
   CI-wired dry-run step (`.github/workflows/deploy.yml` deploys for real
   on push) — a dry-run job is not currently established as an
   independent CI gate, only as an ad hoc local command, same as Task 14
   recorded.
3. **Live Cloudflare Access service-token identity** (R12/F15): the scoped
   service-token identity, hardened denial script, and smoke-test JSON
   checks are unit/local-fixture tested only; a real signed service token
   against a real deployed Worker has never been exercised (documented
   already in `docs/operations/deploy.md` per R12's own report).
4. **Real three-year AMFI backfill at production scale**: acceptance
   criterion 1 requires an operator-supplied three-year AMFI file set; no
   automated fetch exists. This gate's largest fixture (300 real daily
   artifacts) is smaller than a real ~750-day backfill and, per the new
   finding above, a real-scale backfill is expected to trip the
   `datasets.metadata_json` export-size guard until that defect is fixed.
5. **Independent NSE/filings/benchmark source integration** (F16, S01-S04):
   explicitly out of scope for this gate.
