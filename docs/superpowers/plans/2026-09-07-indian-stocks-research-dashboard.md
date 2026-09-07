# Indian Markets Research Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a private EOD Indian-market dashboard that ingests official data, computes explainable momentum factors, runs saved Screener-style queries, and teaches each metric.

**Architecture:** A Python pipeline in GitHub Actions validates and calculates data, retaining immutable/history artifacts in R2 and publishing versioned query snapshots to D1. A TypeScript Cloudflare Worker serves an authenticated API; a React client provides the research UI; a shared TypeScript package safely parses and compiles query expressions.

**Tech Stack:** Python 3.13, uv, Pydantic, Polars, PyArrow, pytest, Hypothesis; Node 24, pnpm 11, TypeScript, Hono, React, Vite, TanStack Query/Table, lightweight-charts, Vitest, Testing Library, Playwright; Cloudflare Workers, D1, R2, Access; GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-09-07-indian-stocks-research-dashboard-design.md`

## Global Constraints

- Support only NSE `EQ` ordinary shares, NSE ETFs, and all AMFI schemes in V1.
- Retain three years of daily prices/NAVs and available fundamentals.
- Use only official public artifacts permitted for private automation; no undocumented endpoints or third-party scraping.
- Store immutable raw artifacts with URL, retrieval time, effective date, checksum, adapter version, and terms reference.
- Keep all pages and APIs private behind Cloudflare Access.
- A failed pipeline run must not replace the last known-good dataset.
- Keep `present`, `missing`, and `not_applicable` distinct.
- Keep heavy compute in GitHub Actions, not request-time Workers.
- Say "top matches," never "recommended buys." Do not add trading, portfolios, real-time data, US markets, public sharing, or generative AI.
- Follow TDD and commit after every task.

## Repository Map

```text
.github/workflows/       CI, EOD refresh, deployment
apps/api/                Hono Worker, Access middleware, D1/R2 repositories
apps/web/                React dashboard and Playwright journeys
packages/contracts/      shared DTOs and metric catalog
packages/query/          lexer, parser, checker, safe D1 compiler
pipeline/market_pipeline source adapters, normalization, analytics, publishing
pipeline/tests/          fixtures, unit, contract, integration tests
db/migrations/           versioned D1 schema
content/glossary/        reviewed educational content
docs/operations/         source, deployment, recovery, launch runbooks
```

---

### Task 1: Reproducible Workspace and CI

**Files:** Create `package.json`, `pnpm-workspace.yaml`, `tsconfig.base.json`, `vitest.workspace.ts`, `pyproject.toml`, `.python-version`, `.editorconfig`, `.gitignore`, workspace package manifests, `pipeline/market_pipeline/__init__.py`, `pipeline/tests/test_bootstrap.py`, `.github/workflows/ci.yml`.

**Interfaces:** Produces root commands `pnpm lint`, `pnpm typecheck`, `pnpm test`; Python commands `uv run ruff check .`, `uv run mypy pipeline`, `uv run pytest`.

- [ ] **Step 1: Write failing smoke tests**

```python
from market_pipeline import __version__
def test_version() -> None:
    assert __version__ == "0.1.0"
```

```ts
import { expect, it } from "vitest";
import { API_VERSION } from "./index";
it("exports API version", () => expect(API_VERSION).toBe("v1"));
```

- [ ] **Step 2: Verify both tests fail**

```bash
uv run pytest pipeline/tests/test_bootstrap.py -q
pnpm --filter @stonks/contracts test
```

Expected: missing Python and TypeScript exports.

- [ ] **Step 3: Add minimal exports and strict configuration**

```python
__version__ = "0.1.0"
```

```ts
export const API_VERSION = "v1" as const;
```

Set Python `>=3.13,<3.14`, Node `>=24,<25`, pnpm `>=11,<12`; enable Ruff, strict mypy, TypeScript `strict`, `noUncheckedIndexedAccess`, and `exactOptionalPropertyTypes`.

- [ ] **Step 4: Add CI, lock dependencies, and verify**

```bash
uv sync && pnpm install
uv run ruff check . && uv run mypy pipeline && uv run pytest
pnpm lint && pnpm typecheck && pnpm test
```

Expected: all checks pass from clean lock files.

- [ ] **Step 5: Commit**

```bash
git add .github .editorconfig .gitignore .python-version apps packages pipeline package.json pnpm-workspace.yaml pnpm-lock.yaml pyproject.toml uv.lock tsconfig.base.json vitest.workspace.ts
git commit -m "build: establish dashboard workspaces and CI"
```

### Task 2: Domain, Source Governance, and Immutable Raw Storage

**Files:** Create `pipeline/market_pipeline/domain/models.py`, `sources/base.py`, `sources/registry.py`, `storage/raw_store.py`; tests under `pipeline/tests/{domain,sources,storage}/`; `docs/operations/source-policy.md`.

**Interfaces:** Produces `AssetClass`, `MetricState`, `Instrument`, `Observation`, `SourcePolicy`, `SourceArtifact`, `SourceAdapter.fetch(date)`, `RawStore.put(artifact, body)`.

- [ ] **Step 1: Write failing state and policy tests**

```python
def test_states_are_distinct() -> None:
    assert MetricValue.missing().state is MetricState.MISSING
    assert MetricValue.not_applicable().state is MetricState.NOT_APPLICABLE

def test_denied_source_cannot_run() -> None:
    with pytest.raises(SourcePolicyError):
        assert_source_enabled(SourcePolicy(source_id="x", automation_allowed=False, terms_url="https://example.test"))
```

- [ ] **Step 2: Run and observe missing types**

```bash
uv run pytest pipeline/tests/domain pipeline/tests/sources pipeline/tests/storage -q
```

- [ ] **Step 3: Implement frozen Pydantic models and protocols**

```python
class SourceAdapter(Protocol):
    source_id: str
    adapter_version: str
    policy: SourcePolicy
    def fetch(self, effective_date: date) -> list[FetchedArtifact]: ...

class RawStore(Protocol):
    def put(self, artifact: SourceArtifact, body: bytes) -> str: ...
    def get(self, object_key: str) -> bytes: ...
```

Use UUIDv5 identities from provider, provider identifier, and asset class.

- [ ] **Step 4: Implement immutable local/R2 stores**

Use `raw/{source}/{date}/{sha256}/{filename}` plus `metadata.json`; identical writes are idempotent and conflicting bytes are rejected. Inject the S3-compatible client for tests.

- [ ] **Step 5: Record NSE/AMFI/Nifty sources and verify**

```bash
uv run pytest pipeline/tests/domain pipeline/tests/sources pipeline/tests/storage -q
```

- [ ] **Step 6: Commit**

```bash
git add pipeline docs/operations/source-policy.md
git commit -m "feat: define governed market data sources"
```

### Task 3: NSE EOD and Instrument Master

**Files:** Create `sources/nse_eod.py`, `normalization/nse.py`, `pipeline/tests/fixtures/nse/`, and matching source/normalization tests.

**Interfaces:** Produces `NseEodAdapter.fetch(date)`, `parse_nse_security_master`, `parse_nse_bhavcopy`, `normalize_nse_rows`.

- [ ] **Step 1: Write a failing contract test with EQ, ETF, SME, REIT, duplicate, and malformed rows**

```python
def test_keeps_only_eq_and_etf(fixture: bytes) -> None:
    batch = normalize_nse_rows(fixture)
    assert {(x.symbol, x.asset_class) for x in batch.instruments} == {
        ("INFY", AssetClass.EQUITY), ("NIFTYBEES", AssetClass.ETF)
    }
```

- [ ] **Step 2: Verify failure**

```bash
uv run pytest pipeline/tests/sources/test_nse_eod.py pipeline/tests/normalization/test_nse.py -q
```

- [ ] **Step 3: Implement safe archive parsing and normalization**

Validate ZIP members, sizes, required columns, report dates, uniqueness, and row-count thresholds. Reject traversal and schema drift; preserve rejected rows and original series/type for audit.

- [ ] **Step 4: Run tests and commit**

```bash
uv run pytest pipeline/tests/sources/test_nse_eod.py pipeline/tests/normalization/test_nse.py -q
git add pipeline
git commit -m "feat: ingest and normalize NSE end-of-day data"
```

### Task 4: AMFI NAV and Scheme Identity

**Files:** Create `sources/amfi_nav.py`, `normalization/amfi.py`, AMFI fixtures and tests.

**Interfaces:** Produces `AmfiNavAdapter.fetch(date)`, `parse_amfi_nav`, stable scheme IDs keyed by AMFI scheme code.

- [ ] **Step 1: Write failing format/hierarchy tests**

```python
def test_preserves_scheme_identity_and_nav() -> None:
    scheme = parse_amfi_nav(FIXTURE)[0]
    assert (scheme.scheme_code, scheme.nav) == ("120503", Decimal("42.1234"))
```

- [ ] **Step 2: Verify failure, implement, and verify success**

Handle BOM/documented encodings and AMC/category headers; strictly validate scheme code, NAV, and date; never invent absent categories. Corrections supersede with lineage; inactivity requires consecutive current-file absences.

```bash
uv run pytest pipeline/tests/sources/test_amfi_nav.py pipeline/tests/normalization/test_amfi.py -q
```

- [ ] **Step 3: Commit**

```bash
git add pipeline
git commit -m "feat: ingest and normalize AMFI NAV data"
```

### Task 5: Fundamentals and Corporate Actions

**Files:** Create `sources/nse_filings.py`, `normalization/fundamentals.py`, `normalization/corporate_actions.py`, filing fixtures and tests.

**Interfaces:** Produces `FundamentalPeriod`, `CorporateAction`, `normalize_financial_results`, `adjustment_factors`.

- [ ] **Step 1: Write failing restatement and split tests**

```python
def test_restatement_supersedes_original() -> None:
    assert normalize_financial_results([ORIGINAL, RESTATED]).active().supersedes_id == ORIGINAL.id

def test_two_for_one_split() -> None:
    action = CorporateAction.split(date(2026, 6, 1), numerator=2, denominator=1)
    assert adjustment_factors([action])[date(2026, 5, 29)] == Decimal("0.5")
```

- [ ] **Step 2: Verify failure**

```bash
uv run pytest pipeline/tests/normalization/test_fundamentals.py pipeline/tests/normalization/test_corporate_actions.py -q
```

- [ ] **Step 3: Normalize bounded V1 financial fields**

Map revenue, operating/net profit, EPS, equity, debt, operating cash flow, capital employed, ROE, ROCE, debt/equity, sales/profit growth, PE, and market cap. Preserve reported numerators, denominators, period type, filing, and restatement lineage; do not annualize incomplete quarters.

- [ ] **Step 4: Implement splits, consolidations, and bonuses**

Keep raw close and adjustment factor separate. Dividends are visible actions; V1 returns are adjusted price returns, not total returns.

- [ ] **Step 5: Verify and commit**

```bash
uv run pytest pipeline/tests/normalization/test_fundamentals.py pipeline/tests/normalization/test_corporate_actions.py -q
git add pipeline
git commit -m "feat: normalize fundamentals and corporate actions"
```

### Task 6: Returns, Risk, RS, and Momentum

**Files:** Create `analytics/{returns,risk,rs,momentum}.py` and golden tests under `pipeline/tests/analytics/`.

**Interfaces:** Produces `calculate_returns`, `calculate_risk`, `benchmark_rs`, `equity_rs_rating`, `momentum_score`.

- [ ] **Step 1: Write failing golden tests**

```python
def test_benchmark_rs() -> None:
    assert benchmark_rs(Decimal("0.20"), Decimal("0.10")) == Decimal("9.090909")

def test_recent_quarter_has_double_weight() -> None:
    values = [Decimal(".10"), Decimal(".05"), Decimal("0"), Decimal("-.05")]
    assert weighted_rs_score(values) == Decimal(".04")
```

Cover insufficient history, ties, zero volume, suspended days, missing endpoints, ETF cohorts, and MF categories.

- [ ] **Step 2: Verify failure**

```bash
uv run pytest pipeline/tests/analytics -q
```

- [ ] **Step 3: Implement endpoint and risk metrics**

Use the last observation on/before each endpoint. Compute 1-day, 1-week, 1/3/6/12-month returns, weekly average volume, 50/200-day averages, 52-week-high proximity, annualized volatility, and drawdown without forward filling.

- [ ] **Step 4: Implement approved RS and momentum formulas**

```python
def benchmark_rs(asset: Decimal, benchmark: Decimal) -> Decimal:
    return ((Decimal(1) + asset) / (Decimal(1) + benchmark) - Decimal(1)) * Decimal(100)
```

Create deterministic 1–99 equity percentiles with stable ties. Apply equity/ETF weights `35/20/15/10/10/10`; emit components, normalized values, coverage, cohort, date, and formula version. Implement separate MF 3/6/12-month, category, volatility, and drawdown scoring.

- [ ] **Step 5: Verify and commit**

```bash
uv run pytest pipeline/tests/analytics -q
git add pipeline
git commit -m "feat: calculate explainable momentum analytics"
```

### Task 7: D1/R2 Publication and Three-Year Backfill

**Files:** Create `db/migrations/0001_market_schema.sql`, `storage/history_store.py`, `storage/d1_publisher.py`, `jobs/backfill.py`, `jobs/daily.py`, integration tests.

**Interfaces:** Produces `market-pipeline backfill --start DATE --end DATE`, `market-pipeline daily --date DATE`, `D1Publisher.stage`, `D1Publisher.promote`.

- [ ] **Step 1: Write failing atomic-publication test**

```python
def test_bad_candidate_does_not_replace_active(db: TestD1) -> None:
    db.seed_active("good")
    with pytest.raises(ReconciliationError): publish(INVALID, db)
    assert db.active_dataset_id() == "good"
```

- [ ] **Step 2: Verify failure and create schema**

Create `datasets`, `active_dataset`, `sources`, `source_runs`, `instruments`, `instrument_aliases`, `latest_metrics`, `fundamental_periods`, `saved_screens`, `screen_runs`, `screen_matches`, and `glossary_entries`. All query data carries `dataset_id`; promotion transactionally changes one pointer.

- [ ] **Step 3: Implement private history objects**

Store `history/{asset_class}/{instrument_id}/{year}.parquet` and `charts/{dataset_id}/{instrument_id}.json.gz`; expose chart objects only through the authenticated Worker.

- [ ] **Step 4: Implement resumable backfill/daily jobs**

Default backfill is execution date minus three calendar years through latest complete date. Checkpoint source/date/checksum, skip verified work, rate-limit, and stop on policy/schema/coverage errors.

- [ ] **Step 5: Verify idempotency, rollback, and budgets**

```bash
uv run pytest pipeline/tests/integration/test_publication.py pipeline/tests/integration/test_backfill.py -q
```

- [ ] **Step 6: Commit**

```bash
git add db pipeline
git commit -m "feat: publish versioned datasets and backfill history"
```

### Task 8: Safe Screener-Style Language

**Files:** Create `packages/contracts/src/{metrics,screens}.ts`; `packages/query/src/{token,lexer,ast,parser,typecheck,compile,index}.ts` and tests.

**Interfaces:** Produces `parseQuery(source)`, `typecheckQuery(ast,catalog,classes)`, `compileQuery(ast,catalog): CompiledQuery`, and a test/reference `evaluateQuery(ast,row)` where `CompiledQuery = {whereSql, params, referencedMetricIds}`.

- [ ] **Step 1: Write failing precedence and tri-state tests**

```ts
it("parses arithmetic before comparison", () => {
  expect(printAst(parseQuery("Volume > Volume 1week average * 1.5").value))
    .toBe("(Volume > (Volume 1week average * 1.5))");
});
it("division by zero is unknown", () => {
  const ast = parseQuery("Volume / 0 > 1").value;
  expect(evaluateQuery(ast, row())).toBe("unknown");
});
```

- [ ] **Step 2: Verify failure**

```bash
pnpm --filter @stonks/query test
```

- [ ] **Step 3: Implement lexer, Pratt parser, spans, and diagnostics**

Support only approved operators and longest-match case-insensitive catalog aliases. Diagnostics contain code, message, offsets, expected tokens, and field suggestions.

- [ ] **Step 4: Implement type checking and parameterized compilation**

Metric columns come only from the checked catalog; all literals are bindings. Unknown arithmetic stays SQL `NULL`, and the final predicate matches only `IS TRUE`. Implement `evaluateQuery` as a small tri-state reference evaluator used to prove compiled D1 behavior against generated rows.

- [ ] **Step 5: Add generated injection tests, verify, and commit**

```bash
pnpm --filter @stonks/query test
git add packages
git commit -m "feat: add safe Screener-style query language"
```

### Task 9: Authenticated Worker API and Saved Screens

**Files:** Create `apps/api/src/index.ts`, `env.ts`, `middleware/access.ts`, routes and repositories for status, metrics, screens, instruments; tests; `apps/api/wrangler.jsonc`.

**Interfaces:** Produces `/api/v1/status`, `/metrics`, `/screens`, `/screens/:id/runs`, `/instruments/:id`, `/instruments/:id/chart` DTOs.

- [ ] **Step 1: Write failing authorization and lifecycle tests**

```ts
it("rejects missing Access identity", async () => expect((await request("/api/v1/status")).status).toBe(401));
it("saves a valid screen", async () => expect((await authPost("/api/v1/screens", SAMPLE)).status).toBe(201));
```

- [ ] **Step 2: Verify failure and implement Access JWT checks**

Verify issuer, audience, signature, expiry, and allowlisted email. Enforce origin, method, content type, body size, and payload schemas before mutations.

- [ ] **Step 3: Implement version-aware repositories/routes**

Every market query joins `active_dataset`; pagination/sorting are allowlisted. Screen runs persist dataset/date, ordered matches, explanations, and entries/exits against the prior successful run.

- [ ] **Step 4: Stream private R2 charts and test failures**

Use `Cache-Control: private`; never disclose object URLs. Cover invalid query/sort, missing metric, stale source, 404, and D1 failure.

```bash
pnpm --filter @stonks/api test
```

- [ ] **Step 5: Commit**

```bash
git add apps/api
git commit -m "feat: expose private research and screening API"
```

### Task 10: Overview and Screen Editor

**Files:** Create React entry/shell/styles, API client, overview/screens routes, data-status, editor, metric-browser, disclosure components and tests under `apps/web/src/`.

**Interfaces:** Produces responsive overview and create/edit/duplicate/run screen flows.

- [ ] **Step 1: Write failing freshness/editor tests**

```tsx
it("shows independent source dates", async () => {
  render(<Overview />, { wrapper: TestApp });
  expect(await screen.findByText("AMFI NAV delayed")).toBeVisible();
});
it("suggests known fields", async () => {
  render(<ScreenEditor initialValue="Volum" />);
  expect(await screen.findByText("Volume")).toBeVisible();
});
```

- [ ] **Step 2: Verify failure, then build accessible responsive shell**

Use semantic navigation, skip links, visible focus, keyboard controls, high-contrast statuses, 320px support, and persistent effective-date/disclosure context.

- [ ] **Step 3: Implement overview/editor**

Overview shows source health, screen counts, top matches, entries, and exits. Editor uses debounced validation, autocomplete, units/applicability, save/duplicate/run, and unsaved-change protection.

- [ ] **Step 4: Verify and commit**

```bash
pnpm --filter @stonks/web test && pnpm --filter @stonks/web build
git add apps/web
git commit -m "feat: build overview and screen editor"
```

### Task 11: Results, Comparison, and Research Views

**Files:** Create results/instrument routes; results-table, momentum-breakdown, chart, comparison, metric-value components; unit and Playwright tests.

**Interfaces:** Produces paginated results, clause explanations, comparison, and asset-specific detail pages.

- [ ] **Step 1: Write failing semantic-state and browser tests**

```tsx
it("distinguishes missing from not applicable", () => {
  render(<><MetricValue value={missing} /><MetricValue value={notApplicable} /></>);
  expect(screen.getByText("Missing data")).toBeVisible();
  expect(screen.getByText("Not applicable")).toBeVisible();
});
```

```ts
test("explains a saved-screen match", async ({ page }) => {
  await page.goto("/screens/volume-breakout");
  await page.getByRole("button", { name: "Run screen" }).click();
  await page.getByRole("link", { name: /view first match/i }).click();
  await expect(page.getByRole("heading", { name: /momentum breakdown/i })).toBeVisible();
});
```

- [ ] **Step 2: Implement result explanations**

Show matching clauses, raw/normalized components, weight, contribution, cohort, formula version, source date, and coverage warning. Use server pagination and allowlisted sorting.

- [ ] **Step 3: Implement asset-specific research and comparison**

Show three-year adjusted price/NAV, actions, benchmark, share fundamentals, ETF metadata, or MF category/risk as applicable. Explicitly label incomparable values.

- [ ] **Step 4: Verify desktop/mobile and commit**

```bash
pnpm --filter @stonks/web test && pnpm --filter @stonks/web test:e2e
git add apps/web
git commit -m "feat: add explainable research result views"
```

### Task 12: Curated Learn Service

**Files:** Create `content/glossary/schema.json` and entries; shared glossary contract; API glossary route/tests; Learn route, term-popover, and tests.

**Interfaces:** Produces `{slug,term,aliases,summary,formula,interpretation,pitfalls,assetClasses,sources,reviewedAt}` and `/api/v1/glossary`.

- [ ] **Step 1: Write failing content validation test**

```ts
it("requires an authoritative source and review date", () => {
  expect(() => parseGlossaryEntry({ ...ENTRY, sources: [], reviewedAt: undefined })).toThrow();
});
```

Seed return, volume, average volume, market cap, moving average, 52-week high, both RS forms, volatility, drawdown, NAV, ROE, ROCE, debt/equity, PE, and cash flow.

- [ ] **Step 2: Implement validated content/API**

Require HTTPS sources, formula provenance when present, and review dates. Serve only curated content; never call a model.

- [ ] **Step 3: Implement Learn search and keyboard-accessible popovers**

Short popovers link to full meaning, formula, interpretation, pitfalls, applicability, review date, and descriptive authoritative links.

- [ ] **Step 4: Verify and commit**

```bash
pnpm --filter @stonks/api test && pnpm --filter @stonks/web test
git add content packages/contracts apps
git commit -m "feat: add reviewed financial glossary"
```

### Task 13: Scheduled Refresh, Reconciliation, and Recovery

**Files:** Create `.github/workflows/daily-data.yml`, `validation/reconcile.py`, `monitoring/budgets.py`, tests, `docs/operations/daily-run.md`, `recovery.md`.

**Interfaces:** Produces `ReconciliationReport`, `BudgetReport`, weekday workflow, manual date/range inputs, recovery commands.

- [ ] **Step 1: Write failing reconciliation/budget tests**

```python
def test_coverage_drop_blocks_publish() -> None:
    assert not reconcile(previous=2500, candidate=1800).publishable
def test_eighty_percent_warns() -> None:
    assert budget_report(used=4_000_000_000, limit=5_000_000_000).warning
```

- [ ] **Step 2: Implement source-specific freshness and reconciliation**

Compare counts, missing ratios, extremes, duplicates, action coverage, benchmark date, and RS distribution. Mark each source complete/delayed/failed/not_expected independently.

- [ ] **Step 3: Add scheduled/manual workflow**

Run after expected NSE EOD availability in Asia/Kolkata, accept manual date/range, lock concurrent publication, use environment secrets, publish redacted reports, and stop before promotion on blocking failures.

- [ ] **Step 4: Document exact retry/rollback/restore commands, verify, and commit**

```bash
uv run pytest pipeline/tests/validation pipeline/tests/monitoring -q
git add .github/workflows/daily-data.yml pipeline docs/operations
git commit -m "ops: schedule and safeguard daily refresh"
```

### Task 14: Secure Deployment and Acceptance

**Files:** Create `.github/workflows/deploy.yml`, private/mobile E2E tests, `docs/operations/{deploy,security-checklist,launch-checklist}.md`; modify `apps/api/wrangler.jsonc`.

**Interfaces:** Produces preview/production deployment, migration gate, Access verification, smoke checks, launch evidence.

- [ ] **Step 1: Write failing privacy/mobile acceptance tests**

```ts
test("anonymous API is denied", async ({ request }) => {
  expect((await request.get("/api/v1/status")).status()).toBe(401);
});
test("screens work at 320px", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 800 });
  await page.goto("/screens");
  await expect(page.getByRole("heading", { name: "Saved screens" })).toBeVisible();
});
```

- [ ] **Step 2: Implement gated preview-to-production deployment**

Order: validate source policy, run all tests, migrate preview, deploy preview, authenticated smoke test, migrate production, deploy production, read-only production smoke test. Pin actions and scope Cloudflare tokens.

- [ ] **Step 3: Configure/document Cloudflare Access and security headers**

Allow one owner identity; bind exact Access audience; use a pipeline service token; permit no bypass routes. Set CSP, HSTS, frame denial, strict referrer policy, private caching, and secret rotation. Protect static assets, API, and R2 objects.

- [ ] **Step 4: Run the complete verification suite**

```bash
uv run ruff check . && uv run mypy pipeline && uv run pytest
pnpm lint && pnpm typecheck && pnpm test
pnpm --filter @stonks/web test:e2e
```

Also run a fixture-backed pipeline, force and verify rollback, execute the representative volume screen, compare both RS outputs with golden fixtures, inspect provenance/glossary links, and review free-tier budgets.

- [ ] **Step 5: Record acceptance evidence and commit**

```bash
git add .github apps docs/operations
git commit -m "ops: secure and verify production deployment"
```

## Final Verification Gate

- [ ] Map every specification acceptance criterion to an automated test or recorded manual result in `docs/operations/launch-checklist.md`.
- [ ] Run `git diff --check` and confirm only intentional changes remain.
- [ ] Confirm every enabled adapter uses a current official documented URL and reviewed source policy.
- [ ] Confirm independent NSE, filings, AMFI, and benchmark dates appear in production.
- [ ] Confirm anonymous page, API, and R2 requests fail.
- [ ] Confirm copy consistently says "top matches" and displays the research disclaimer.
- [ ] Confirm no portfolio, trading, real-time, public-sharing, US-market, or generative-AI capability entered scope.
