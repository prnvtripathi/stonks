# Original Source-Scope Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Use superpowers:subagent-driven-development only when the owner chooses delegated execution. Steps use checkbox (`- [ ]`) syntax for tracking. Do not enable network collection or deployment merely by reading this plan.

**Goal:** Connect the original NSE equity/ETF, corporate-filings/action, and official-benchmark requirements to the already implemented application without pretending that standalone normalizers are a complete stock dashboard.

**Architecture:** Reuse the existing parsers, normalizers, analytics, and R-plan publication bundle. Add explicit supplied-artifact admission and source-specific candidate builders that compose into one validated dataset with independent source dates. A source capability, its acquisition permission, and its current availability are three separate facts.

**Tech Stack:** Existing Python/TypeScript/Cloudflare stack; no replacement framework or third-party market-data service.

**Spec:** `docs/superpowers/specs/2026-09-07-indian-stocks-research-dashboard-design.md`; audit finding F16 in `docs/reviews/2026-09-09-implementation-audit.md`; depends on contracts established by [the remediation plan](2026-09-09-correctness-and-publication-remediation.md).

## Global constraints

- Support NSE `EQ` ordinary shares, NSE ETFs, and AMFI schemes; retain three years of supported price/NAV history and available fundamentals.
- Use only permitted official inputs. Disabled source automation stays disabled; do not substitute undocumented endpoints or fabricated permissions.
- Apply corporate-action adjustments before equity return/RS calculations; preserve original observations and action lineage.
- Benchmark RS uses aligned official benchmark endpoints. MF comparison is unavailable without an official mapping.
- Equity RS cohorts never contain ETFs or mutual funds. Fields that do not apply to an asset class remain `not_applicable`, distinct from `missing`.
- Failed/incomplete required input does not replace the last good dataset. Dates and health remain independent per source.
- Keep the application private and preserve the original exclusions: no trading, portfolio, real-time, public-sharing, US-market, or generative-AI features.

## Dependencies and review boundaries

S01 defines acquisition/admission and source input contracts. S02 and S03 consume them; S04 composes the completed capabilities. R01–R03 define candidate validation/identity/formulas; R04–R06 cover serving fidelity; R07–R11 provide publication/recovery. Do not implement a parallel publisher or duplicate metric catalog.

Record these tasks in the same tracked remediation progress document, using `S01`–`S04`, commit SHAs, focused checks, and open finding IDs. Use one task review plus one scoped correction/re-review by default. Split a newly discovered architectural dependency into an explicit named task; do not grow an untracked “final fix” loop.

Fixture-backed functionality can be completed without actual NSE network collection. Real-source acceptance remains blocked until the recorded source permission allows the particular acquisition and retention mode. Do not claim original scope is complete while that acceptance is pending.

### S01: define and enforce supplied-file versus network acquisition

**Finding:** F16. **Files:** modify `pipeline/market_pipeline/domain/models.py`, `sources/registry.py`, `sources/base.py`, `storage/raw_store.py`, CLI/manifest handling and source policy docs; add `pipeline/tests/integration/test_supplied_source_admission.py`.

**Interface:** add `acquisition_mode: Literal['supplied', 'network']` and a permission-record identifier to admitted artifact provenance. Policies distinguish automation permission from authorized supplied-file retention/use. `assert_artifact_policy` validates the specific mode, exact official URL/provenance, checksum, and retention permission; network adapters separately require `automation_allowed`. Legacy records retain their original strict interpretation rather than automatically gaining a new permission.

Define `SourceInput` in a focused new `jobs/source_inputs.py`: source ID, artifact role, expected/loaded dates, artifact ID/checksum/object key, adapter version, and acquisition mode. Roles distinguish NSE security master, EOD observations, filings, actions, and benchmark observations; these cannot be inferred solely from a source-name string.

- [ ] Add tests proving network NSE remains denied; supplied NSE without a recorded allowed-use/retention decision also remains denied; a fixture policy explicitly authorizing supplied use admits valid provenance; forged URLs, wrong dates/checksums, and missing companion files are rejected.
- [ ] Run `.venv/bin/python -m pytest pipeline/tests/integration/test_supplied_source_admission.py -q` and record the admission failure.
- [ ] Implement the mode-aware contract without changing production permission flags. Resolve supplied files only from the explicit manifest root and validate all roles before checkpointing.
- [ ] Document exact manifest examples and the existing source acquisition limitations. Do not interpret “manual” as automatically licensed. Fixture policy overrides are injected test objects, never production registry edits.
- [ ] Verify the existing source/registry/raw-store tests and commit.

```python
with pytest.raises(SourcePolicyError):
    admit(network_nse_artifact, canonical_policy)
with pytest.raises(SourcePolicyError):
    admit(supplied_nse_without_permission, canonical_policy)
assert admit(valid_supplied_fixture, supplied_use_fixture_policy).acquisition_mode == 'supplied'
```

`admit(artifact: SourceArtifact, policy: SourcePolicy) -> SourceArtifact` is the proposed validated admission entry point, implemented in `sources/registry.py`; it does not fetch bytes.

### S02: publish equity/ETF observations and corporate-action-adjusted analytics

**Finding:** F16. **Files:** add `jobs/nse_candidate.py`; modify `jobs/publish.py` only to compose builders; reuse `normalization/nse.py`, `normalization/corporate_actions.py`, `analytics/returns.py`, `analytics/risk.py`, `analytics/rs.py`, `analytics/momentum.py`; add `pipeline/tests/integration/test_nse_candidate.py` and role-specific fixtures.

**Interface:** `build_nse_candidate(inputs: Sequence[SourceInput], raw_store: RawStore, effective_date: date) -> DatasetBuild` uses S01 inputs and the existing `DatasetBuild` contract with R02 input identity. Use `normalize_nse_rows(..., security_master=..., report_date=...)` and `adjustment_factors(actions, dates)`; preserve stable internal IDs and aliases. Do not turn a missing security master/action-coverage input into an empty successful universe.

- [ ] Add a small synthetic official-format fixture containing an ordinary share, an ETF, an excluded security, symbol rename, split/bonus action, short-history instrument, and a formerly active instrument. Include enough generated session observations for each declared metric window.
- [ ] Run `.venv/bin/python -m pytest pipeline/tests/integration/test_nse_candidate.py -q` and observe missing publication capability.
- [ ] Compose price/volume/master/action data with exact source dates; adjust price history before returns, risk, and equity RS. Keep raw prices and action provenance. Keep asset-class/category cohorts separate, exclude inactive securities from current screens, and retain their historical run identities.
- [ ] Populate only supported metrics from the metric catalog with consistent units and states. Benchmark fields remain missing until S03 provides actual official benchmark data; fundamentals remain missing until S03 filings are available, never invented from prices.
- [ ] Export the candidate using R07, load it through the real Worker repository, and evaluate the representative volume/return query against an independently calculated expected match set. Verify SQL/evaluator parity and corporate-action-adjusted returns.
- [ ] Verify normalization/analytics/publication tests and commit this equity/ETF capability separately from filings/benchmark work.

```python
assert published_asset_classes == {'equity', 'etf'}
assert renamed_instrument_id == original_instrument_id
assert split_adjusted_return == Decimal('0')  # 100 before 2:1 split, 50 after
assert excluded_security_id not in current_screen_ids
assert equity_rs_cohort_ids.isdisjoint(etf_ids)
```

### S03: connect filings and official benchmark inputs

**Finding:** F16 and completion of F05. **Files:** add `jobs/reference_candidate.py`, `sources/benchmark.py`, `content/benchmarks/mappings.json`; reuse `sources/nse_filings.py` and `normalization/fundamentals.py`; modify candidate composition and add `pipeline/tests/integration/test_reference_candidate.py`.

**Interface:** `build_reference_rows(inputs: Sequence[SourceInput], raw_store: RawStore, instrument_ids: Mapping[str, str], effective_date: date) -> ReferenceRows`. Define `ReferenceRows` in the same module with fundamental periods, latest applicable fundamental metrics, benchmark observations, and source/provenance metadata. Benchmark mapping records contain instrument/category identifier, official benchmark ID, valid-from/to dates, and a reviewed official source reference. An empty reviewed mapping file is valid and produces missing MF benchmark RS.

- [ ] Add fixture cases for quarterly/annual results, a restatement, a missing filing, an ETF/MF inapplicable company metric, and aligned/misaligned benchmark endpoints.
- [ ] Run `.venv/bin/python -m pytest pipeline/tests/integration/test_reference_candidate.py -q` and observe missing integration.
- [ ] Normalize filings with `normalize_financial_results`, retaining filing time, period, restatement/supersession identity and raw artifact lineage. Only use facts available by the dataset's declared as-of policy; missing fundamentals do not become zero. Do not silently mix standalone and consolidated statements in a ratio.
- [ ] Parse admitted official Nifty 500 observations with strict date/schema validation. Compute benchmark RS on identical instrument/benchmark endpoints. No matching benchmark or MF mapping means `missing` with the unavailable reason; category rank stays separate.
- [ ] Feed reference rows into the same compact projection and test research-page DTOs for periods, source dates, states, and formula units. Bump the fingerprint's builder/version inputs through R02.
- [ ] Verify existing filings/analytics and new reference tests; commit. If filing support and benchmark parsing require independently large changes, execute them as two explicit child tasks with separate commits rather than one expanding review cycle.

```python
assert restated_period.supersedes_id == original_period.period_id
assert mutual_fund_company_metric.state == 'not_applicable'
assert missing_equity_filing_metric.state == 'missing'
assert benchmark_rs_without_mapping.state == 'missing'
assert rs_asset_start == rs_benchmark_start
assert rs_asset_end == rs_benchmark_end
```

### S04: connect source health/acquisition and prove the original vertical path

**Finding:** F16 and original acceptance coverage. **Files:** modify CLI/daily/backfill source orchestration, `.github/workflows/daily-data.yml`, source status persistence/API tests, operation/launch docs; add `pipeline/tests/integration/test_multi_source_publication.py`.

**Interface:** daily execution takes a source schedule with independent expected dates and acquisition mode per source. `SourceStatus` persists expected date, loaded date, complete/delayed/failed/not-expected state, coverage, and a concise category. One composed `DatasetBuild` contains the validated configured universe; source selection cannot accidentally replace an equity+MF dataset with only one slice.

- [ ] Add one cross-boundary test with equity, ETF, MF, filings/actions and official benchmark fixtures → candidate → R07 bundle → compact serving schema → Worker screen/research/status/chart. Run a second date with only one source delayed, then identical retry and corrected source input.
- [ ] Run `.venv/bin/python -m pytest pipeline/tests/integration/test_multi_source_publication.py -q` and record missing source health/composition behavior.
- [ ] Wire supplied sources through S01 and existing allowed adapter capabilities. For permitted automatic sources, implement only a documented, currently verified official download path with source policy, rate limits, timeouts, and schema checks. If no such path is established, keep explicit supplied-file mode and record that automatic-source acceptance is pending; never silently label a no-op schedule a completed refresh.
- [ ] Resolve non-publication days using explicit source calendars; do not require a quarterly filing every weekday. Required-current-source failures preserve the old dataset. Retained delayed sources show their true loaded dates and stale state, never the newest other source's date.
- [ ] Persist last attempted refresh status separately from the active market snapshot so failure is visible without publishing partial data. Keep messages free of source secrets and private saved-query text.
- [ ] Run the full acceptance path and update each original spec criterion with test name or explicit real-source/live gate. Verify source policy and actual coverage before declaring all NSE/AMFI instruments or three years loaded. Commit.

```python
assert status['amfi-nav'].loaded_date < status['nse-eod'].loaded_date
assert status['amfi-nav'].state == 'delayed'
assert failed_required_source.active_dataset_id == last_good_dataset_id
assert mixed_candidate.asset_classes == {'equity', 'etf', 'mutual_fund'}
assert no_manifest_refresh.status == 'skipped'
```

## Original-scope acceptance

- [ ] Each original source has an implemented admission/normalization/publication path and an explicit acquisition permission/status.
- [ ] Permitted real datasets establish the target universe and three-year coverage; synthetic fixtures establish behavior but do not stand in for actual source coverage.
- [ ] The representative query and both RS definitions match independent fixtures; each displayed value has source/effective-date lineage and the correct unavailable state.
- [ ] R-plan retry, corruption, recovery, saved-run, privacy and capacity gates remain passing with all asset classes enabled.
- [ ] The live personal-use screen query meets the two-second target, remote publication fits the configured runtime/budget, and owner login plus anonymous denial are observed in the configured environment.
- [ ] Any remaining source permission, official endpoint, owner configuration, or live-evidence dependency is recorded as pending. Do not mark the dashboard fully implemented merely because unit tests or packaging dry-runs pass.
