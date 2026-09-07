# Launch checklist

Maps every acceptance criterion in
`docs/superpowers/specs/2026-09-07-indian-stocks-research-dashboard-design.md`
("Acceptance Criteria") and every bullet in Task 14's "Final Verification
Gate" to either a named automated test or an explicit manual verification
step. Nothing here is claimed as automated unless a command is given that
actually exercises it.

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
