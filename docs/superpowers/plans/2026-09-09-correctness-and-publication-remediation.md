# Correctness and Publication Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Use superpowers:subagent-driven-development only when the owner chooses delegated execution. Steps use checkbox (`- [ ]`) syntax for tracking. This document is a plan, not authorization to deploy.

**Goal:** Make the existing AMFI-to-dashboard path correct, repeatable, recoverable, and bounded before completing the remaining original source integrations.

**Architecture:** Keep the Python pipeline, compact D1 serving projection, private R2 storage, Worker, and React client. Build and validate a complete candidate before promotion; identify datasets by their contributing inputs; keep immutable run results and verified publication bundles independent of the active projection. Use one bounded R2 transport and an explicit success gate for remote import.

**Tech Stack:** Existing Python 3.13/uv/Pydantic/PyArrow/pytest, TypeScript/pnpm/React/Vitest/Playwright, Cloudflare D1/R2/Access, GitHub Actions. Add the S3 SDK only with the uploader task, using the existing dependency/lockfile process.

**Spec:** `docs/superpowers/specs/2026-09-07-indian-stocks-research-dashboard-design.md`; defects and reproductions: `docs/reviews/2026-09-09-implementation-audit.md`.

## Global constraints

- A failed or incomplete run never replaces the last known-good production dataset.
- The value states `present`, `missing`, and `not applicable` remain distinct.
- No automated NSE collection is enabled by this plan. Supplied-source capability is addressed separately with explicit provenance and permission checks.
- Keep every page, API, archive, and R2 object private; no new anonymous route.
- Preserve saved screens, historical runs, existing untracked work, and immutable raw artifacts.
- Keep heavy calculation in the pipeline. Result pagination must be bounded before returning data to the Worker/browser.
- Continue saying “top matches”; do not add trading, portfolios, public sharing, US-market functionality, or generative AI.
- The original all-equity/ETF/AMFI scope is not reduced. This plan repairs existing behavior; the [source completion plan](2026-09-09-original-source-scope-completion.md) addresses the missing capability.

## Baseline and execution rules

Baseline: branch `codex/indian-research-dashboard`, HEAD `1469b1c75e3ba194ae8b20caafb5c4e617f8330b`. Existing untracked `pipeline/market_pipeline/publication/r2_sync.py` and `pipeline/tests/integration/test_r2_sync.py` belong to R08; preserve and inspect them before editing. Do not create another similarly named branch or worktree without a reason. Recheck status/HEAD if the branch has advanced.

Use this plan as the completion record; the old Task 15 is superseded by R07–R12. Mark historical implementation work as such rather than treating old checkmarks as acceptance evidence. Create `docs/reviews/2026-09-09-remediation-progress.md` on execution, with columns `task`, `finding IDs`, `base`, `head`, `focused checks`, `review round`, `open findings`, and `status`.

For each task: add its regression first, observe the relevant failure, implement only that deliverable, run the listed focused checks, commit that task, and review only its diff and named findings. One initial review and one targeted correction/re-review are the default. If a structural dependency remains, record the smallest separately named task before continuing; do not silently expand a task or restart a whole-branch review. Never waive a confirmed data-integrity/authentication defect to meet a round limit. Ordinary independent cosmetic observations go to a deferred list.

Run the full suite at the phase acceptance gate, not after every local edit. Reviewers reuse verified output for unchanged code. Briefs include task text, constraints, relevant files, and the delta; they do not include the whole conversation or whole-branch diff.

## Ordering and acceptance

`R01 → R02 → R03`; R04 is independent. `R05 → R06`. `R02 → R07 → R08 → R09 → R10 → R11`. R12 is independent of data publication. Execute sequentially by default; these dependencies describe safe ordering, not an instruction to spawn agents.

Completion requires all R tasks plus the phase gate below. Completing R tasks does **not** establish original stock-dashboard scope or authorize live production deployment.

### R01: validate the candidate and reconcile actual source coverage

**Findings:** F01, F02. **Files:** modify `pipeline/market_pipeline/cli.py`, `jobs/publish.py`, `validation/reconcile.py`, relevant CLI/integration tests; add `pipeline/tests/integration/test_candidate_gate.py`.

**Interface:** `publish_checkpointed_dataset` accepts an optional prebuilt `DatasetBuild` so the CLI builds once, validates once, and publishes that same candidate. Introduce `CandidateCoverage` in `validation/reconcile.py` with `source_id`, `expected_date`, `loaded_date`, `instrument_count`, `missing_ratios`, and `blocking_reasons`. Previous values come from the last successfully published candidate in the same source scope; checkpoint/download counts remain separate operational statistics.

- [ ] Add CLI regressions using existing manifest fixture builders: identical retry succeeds; 3→1 instrument truncation blocks; malformed final and intermediate artifacts block; an explicitly expected holiday is distinct from missing data. Use temporary DB/raw/history directories.
- [ ] Run `.venv/bin/python -m pytest pipeline/tests/integration/test_candidate_gate.py -q` and record the expected behavioral failures.
- [ ] Make required raw read/checksum/schema errors fatal to that candidate. Keep source-specific expected dates explicit. Build candidate before reconciliation, compare normalized counts and missingness, and persist a baseline only after successful local publication. Track remote publication success separately in R10.
- [ ] Preserve the old pointer and history references on all failures. Do not “fix” retries by merely changing `completed` to `completed + skipped`: that still counts files rather than instruments.
- [ ] Verify the new suite and existing CLI/publication tests, commit, and record the review result.

Regression oracle:

```python
# Use CLI output from the existing fixture-backed daily runner.
assert retry.exit_code == 0
assert retry.dataset_id == first.dataset_id
assert truncated.exit_code != 0
assert truncated.active_dataset_id == first.dataset_id
assert malformed.active_dataset_id == first.dataset_id
```

### R02: fingerprint every contributing input and preserve lineage

**Finding:** F03. **Files:** modify `jobs/publish.py`, `storage/d1_publisher.py` only as needed for metadata; add `pipeline/market_pipeline/publication/input_manifest.py` and `pipeline/tests/integration/test_input_identity.py`.

**Interface:** `dataset_fingerprint(inputs: Sequence[Mapping[str, str]], versions: Mapping[str, str]) -> str`. Each input records source ID, effective date, artifact ID, checksum, adapter version, and raw object key; `versions` identifies normalization, analytics, and projection versions. Canonical serialization sorts inputs and version keys and excludes generation/retrieval timestamps unrelated to input meaning. Store the manifest in candidate metadata or a referenced immutable JSON object, with its hash on the dataset.

- [ ] Add a latest-day-only publication followed by historical backfill with the same end date; add reordered-input, exact-retry, and formula-version cases.
- [ ] Verify failures with `.venv/bin/python -m pytest pipeline/tests/integration/test_input_identity.py -q`.
- [ ] Derive dataset identity from all contributing inputs plus versions. Define deterministic correction selection by source/date and explicit revision metadata; conflicting revisions without an ordering rule block rather than using database row order.
- [ ] Make exact-input retries reuse their dataset, while historical additions/corrections or calculation changes create a new dataset. Resolve metric lineage through the stored input manifest instead of suggesting that one latest artifact explains a whole return series.
- [ ] Verify and commit with the identity/provenance review only.

```python
assert dataset_fingerprint(inputs, versions) == dataset_fingerprint(list(reversed(inputs)), versions)
assert dataset_fingerprint(inputs, versions) != dataset_fingerprint(inputs + [older_input], versions)
assert updated_run.return_1w is not None
assert updated_run.dataset_id != latest_only_run.dataset_id
```

### R03: correct the published risk window and MF benchmark semantics

**Findings:** F04, F05. **Files:** modify `analytics/risk.py`, `jobs/publish.py`, corresponding analytics/integration tests, and metric/glossary descriptions only where they describe these formulas.

**Interface:** preserve `calculate_risk(observations, effective_date) -> RiskMetrics`; define `risk-v2-252-session` as the trailing 252 valid-session return window with its preceding price endpoint (253 observations). Short histories expose unavailable one-year volatility/drawdown with coverage; moving averages and 52-week high retain their independently specified windows. R02 includes the version change.

- [ ] Add the 300-observation old-crash regression, exact boundary, insufficient-history, and effective-date truncation cases. Add MF NAV-only cases with multiple category members and no benchmark mapping.
- [ ] Run the targeted analytics tests and observe failures.
- [ ] Apply the bounded risk window; update coverage/provenance. Do not use a partial-year statistic under an unqualified one-year label.
- [ ] Remove category-composite substitution from `benchmark_rs_*`. Without a reviewed official mapping and matching observations publish `missing` with a reason. Leave category rank available and do not introduce a new composite metric.
- [ ] Verify MF momentum receives the corrected risk values and unavailable inputs honestly; commit.

```python
points = [(date(2025, 1, 1) + timedelta(days=i), Decimal(200 if i == 0 else 100)) for i in range(300)]
risk = calculate_risk(points)
assert risk.annualized_volatility == Decimal(0)
assert risk.max_drawdown == Decimal(0)
# A NAV-only candidate must not manufacture an official benchmark.
assert all(row['state'] == 'missing' for row in metric_rows if row['metric'].startswith('benchmark_rs_'))
```

### R04: preserve momentum and metric metadata through the real compact export

**Finding:** F06. **Files:** modify `apps/api/src/repositories.ts`; add `apps/api/src/publication-contract.test.ts` and a small exported test fixture generator under `pipeline/tests/integration/`; keep production metric DTOs consistent.

**Interface:** metadata decoding accepts a validated record or a JSON string and rejects arrays/null. Existing `D1ResearchStore.instrument(datasetId, instrumentId)` returns unchanged `MomentumBreakdown` fields.

- [ ] Generate a compact row using the Python exporter with a percent component, raw/normalized values, weight 0.2, contribution 0.12, MF cohort, warning, and coverage 0.85. Feed that row through the actual D1 repository decoder; do not mock the decoder itself.
- [ ] Run `pnpm --filter @stonks/api exec vitest run src/publication-contract.test.ts` and record the metadata mismatch.
- [ ] Correct the decode boundary and cover malformed metadata without silently converting valid object metadata to empty records.
- [ ] Assert stored and displayed component units, weights, contributions, source date, formula version, and warnings; run the existing API/query tests and commit.

```typescript
expect(instrument.momentum?.components[0]).toMatchObject({ weight: 0.2, contribution: 0.12, unit: "percent" });
expect(instrument.momentum).toMatchObject({ cohort: "mutual_fund:equity", coverage: 0.85 });
```

### R05: make saved runs immutable historical records

**Finding:** F07. **Files:** add the next unused forward migration for run snapshots; modify `packages/contracts/src/index.ts`, `apps/api/src/repositories.ts`, `apps/api/src/index.ts`, `apps/web/src/api.ts`, `apps/web/src/research.tsx`, API/web tests.

**Interface:** extend persisted runs with nullable legacy-compatible `source` and `languageVersion`; new runs always store both. Persist `symbol`, `name`, and `assetClass` with each match so old results do not depend on retained active instrument snapshots. `effectiveDate` comes from the selected dataset; `completedAt` remains execution time. Results resolve an explicit run ID within its screen regardless of current dataset; default results return the latest successful run and expose whether it uses the current dataset/query.

- [ ] Add tests for A→B publication then retrieval of A's run; an edited screen without rerun; an old dataset run on today's date; and an instrument removed from the current projection.
- [ ] Observe failures with focused API/web tests.
- [ ] Add the forward migration and implement run/match snapshots atomically with the existing saved-run transaction. Preserve global screens and previously stored explanations.
- [ ] Render the run's source/date and a concise “screen changed since this run” or “previous dataset” indicator. Never backfill unknown historical query text with today's expression; legacy records display source unavailable.
- [ ] Verify fresh and upgrade migrations, date/query/history tests, commit.

```typescript
expect(oldResult.status).toBe(200);
expect(oldResult.run).toMatchObject({ datasetId: "A", effectiveDate: "2026-09-04", source: "Volume > 100" });
expect(oldResult.screen.source).toBe("Volume > 1000");
expect(oldResult.run.matches[0].symbol).toBe("OLD_SYMBOL");
```

### R06: bound saved-screen execution and result retrieval in D1

**Finding:** F08. **Files:** modify repository/API result and run-history methods, contracts/client consumers, add `apps/api/src/result-pagination.test.ts`; add a forward index migration only when the measured query plan needs it.

**Interface:** replace result-route `listRuns` use with `getRun(screenId, runId?)` for one summary and `pageRunMatches(runId, {sort, direction, limit, offset})` for one page and total. The accepted sort allowlist remains `rank`, `score`, `symbol`, `assetClass`; instrument ID is the deterministic tie-breaker. Historical identity comes from R05, not current instruments. `listRuns` returns paginated summaries without embedded match bodies.

- [ ] Seed 10,000 matches and 100 historical runs through a local SQLite-backed D1 test adapter. Instrument query calls/returned rows for a 25-row result page.
- [ ] Observe current N+1/unbounded materialization, then move selection, ordering, and pagination into parameterized repository SQL. Count queries may scan an index but must not materialize historical payloads into the Worker.
- [ ] Remove the same N+1 path from `D1ResearchStore.runScreen`: use set-based `INSERT ... SELECT` to snapshot matching IDs, score/rank, identity and the necessary metric/provenance JSON, with entered/exited transitions derived against only the previous successful run. Persist the run source/version from R05 and derive detailed clause explanations for the requested page from those immutable inputs. Mark the run complete only in the same successful atomic operation as its matches; do not return a partially built run as complete.
- [ ] Change the run-creation response to a summary plus the first requested result page rather than every match payload, updating client/contracts/tests together. Do not merely batch thousands of per-instrument reads or move unlimited work into `Promise.all`. Verify the actual statement count, bound-parameter count, daily write accounting, and Worker CPU/payload profile for execution as well as retrieval.
- [ ] Verify all sort directions, ties, invalid sort input, offsets, entered/exited filtering, and historical identities. Update memory and D1 repositories to the same contract.
- [ ] Assert at most five repository SQL calls and at most the requested page's match payloads returned for result retrieval. Run-history retrieval must return only its requested summaries.
- [ ] Add a 10,000-match creation test with no per-match Worker reads/writes, deterministic transitions and all-or-nothing failure behavior. Keep execution within ten set-based SQL statements and the configured resource envelope; count its writes in the account budget R09 uses. If a representative query cannot meet the synchronous envelope, record that measured architecture decision as a separate task before implementation continues; do not silently cap the universe or drop matches.
- [ ] Record a warmed local 12,000-instrument representative-query timing and SQL query plan. Treat it as a regression baseline; the spec's two-second target still needs the separately recorded live personal-use check. Commit.

```typescript
expect(page.matches).toHaveLength(25);
expect(probe.statementCount).toBeLessThanOrEqual(5);
expect(probe.materializedMatchRows).toBeLessThanOrEqual(25);
expect(page.total).toBe(10000);
```

### R07: version history partitions and define derived-data retention

**Finding:** F14. **Files:** modify `storage/history_store.py`, `jobs/publish.py`, `publication/d1_export.py`; add `publication/bundle.py`, history/export tests and `docs/operations/retention.md`.

**Interface:** publication bundle version 1 contains `dataset_id`, `input_manifest_hash`, source dates, projection version, SQL checksum, and object entries `{key, sha256, bytes, kind}`. Historical partition keys include a content hash: `history/{asset_class}/{instrument_id}/{year}/{sha256}.parquet`. Chart keys stay dataset-scoped. The bundle is authoritative; export does not glob every historical file to infer required objects.

- [ ] Add same-year append, closed-year correction, year-boundary, rolling-three-calendar-year, exact retry, and failed-publication tests.
- [ ] Verify current fixed-key conflicts and overbroad manifest selection fail the tests.
- [ ] Select the three-calendar-year serving range, retaining any calculation boundary observation separately when needed; hash each partition's deterministic bytes. Preserve older raw inputs according to source policy. Never rewrite a closed object under the same key.
- [ ] Generate an immutable manifest for the exact candidate. Keep active and previous-successful bundles reachable for rollback; retain additional derived history referenced by saved runs where the product actually exposes it. Plan garbage collection as a dry-run list first, deleting only derived objects proven unreferenced; never delete raw artifacts as a quota workaround.
- [ ] Include counts/bytes for retained objects and cleanup operations in the publication budget. Verify the failed candidate cannot change the old bundle or chart objects; commit.

```python
assert corrected.key != original.key
assert store.get(original.key) == original.body
assert bundle.object_keys == expected_candidate_keys
assert unrelated_old_chart not in bundle.object_keys
assert active_bundle_id == before_failure_bundle_id
```

### R08: finish one bounded R2 transport and make failure propagation explicit

**Findings:** F09, F10. **Files:** reconcile the existing untracked `publication/r2_sync.py` and `test_r2_sync.py`; modify `pyproject.toml`, `uv.lock`, `.github/workflows/daily-data.yml`, publication workflow tests and credential docs.

**Interface:** `synchronize_history(bundle, history_root, client, *, max_workers=16, deadline_seconds=14400) -> R2SyncResult` consumes R07's checksum/size manifest; result includes verified object count, bytes, and attempted request counts by operation. Maximum concurrency is configurable up to 32. The CLI exits nonzero unless every required object is verified and workers are quiescent. S3 credentials are explicit, private-bucket scoped, and separate from the D1 API token.

- [ ] Preserve useful existing validation/concurrency tests; add malformed-final-entry, missing-file, wrong bytes, transport error, retry exhaustion, and deadline tests. Add a subprocess-level orchestration test proving D1 import is never invoked after any of these failures.
- [ ] Validate the entire bundle and all local checksums synchronously before the first mutation. Remove Bash process substitution as the safety boundary. The workflow invokes the Python transfer process once and requires its successful exit/result before import.
- [ ] Finish the existing SDK transport with locked dependencies, bounded in-flight work, connection reuse, closed response streams, explicit connect/read timeouts, bounded retries, and a shared deadline. Do not submit an unlimited future queue. On error stop submitting, cancel queued work, and wait for bounded in-flight requests to finish before exiting; acknowledge that timed-out PUT outcomes can be unknown and must be verified on retry.
- [ ] Fix the nine existing mypy errors. Adapt fixed-key assumptions to R07's bundle. Verified immutable objects can be reused; all attempted operations, including retries, are counted for R09.
- [ ] Use a fake S3 client with controlled latency for 12,000-instrument / four-calendar-partition scheduling and no per-object subprocesses. Include a smaller injected-latency test proving concurrency and deadline behavior. Record predicted network throughput separately from actual remote throughput; no fake-client timing is production evidence.
- [ ] Run `.venv/bin/python -m pytest pipeline/tests/integration/test_r2_sync.py pipeline/tests/integration/test_cloudflare_publication.py -q` plus mypy and commit.

```python
assert max_observed_concurrency <= configured_workers
assert import_calls == []  # malformed bundle, corruption, timeout, or transfer failure
assert completed.verified_objects == len(bundle.objects)
assert active_worker_count == 0  # before returning either success or failure
```

### R09: validate capacity telemetry and budget the whole publication attempt

**Findings:** F11, F14. **Files:** modify `publication/preflight.py`, `publication/d1_export.py`, workflow, token/budget documentation; add `publication/remote_usage.py` and `pipeline/tests/integration/test_remote_usage.py`.

**Interface:** `RemoteUsage` records account/database/bucket identity, observation time, D1 bytes and observed writes, R2 retained bytes, and month-to-date operation accounting with a declared observation window. `validate_remote_usage(payload, expected_resources, now)` rejects errors, missing dimensions, stale observations, and wrong resources. `assert_plan_within_free_tier` consumes this validated object plus R07/R08 counts; raw Wrangler zero-default output is not an accepted source.

- [ ] Add payload tests for GraphQL `errors`, absent account/database, malformed metrics, valid zero, stale data, resource mismatch, and missing analytics permission. Test month boundaries, 23-weekday months, manual reruns, and SDK retries.
- [ ] Read authenticated usage directly with explicit response validation. Document Account Analytics Read and the exact supported resource scope; do not treat adding a permission as sufficient to fix silent-zero responses. Account for applicable shared account limits, including preview/other usage if the allowance is account-wide.
- [ ] Keep the existing conservative D1 thresholds configurable; include maintenance, stale deletes, and restore operations. Include R2 bytes, transfer retries, raw archive uploads, publication bundles, and checkpoint backups. Calculate the actual month's scheduled-run count instead of constant 22.
- [ ] When timely exact telemetry is unavailable, use a durable conservative reservation ledger for this publisher plus an explicitly allocated account budget; state what it cannot measure. Unknown capacity blocks safely with a useful reason. Do not represent estimates as measured usage.
- [ ] Include a 12,000-instrument / rolling-three-year case using realistic serialized payload sizes, plus an oversized case that is rejected before mutation. Budget runtime below four hours for publication, leaving time for the rest of the hosted job; configure an overall workflow timeout below six hours.
- [ ] Verify parser/preflight tests and commit. Do not tune envelopes upward just to make a fixture pass.

```python
with pytest.raises(RemotePublicationBudgetError):
    validate_remote_usage({'errors': [{'message': 'denied'}]}, expected_resources, now)
assert plan.monthly_attempts >= scheduled_dates_in_month + reserved_manual_attempts
assert oversized.mutation_calls == []
```

### R10: restore durable source/checkpoint state on a clean runner

**Finding:** F12. **Files:** add `publication/checkpoint_archive.py`, `pipeline/tests/integration/test_checkpoint_restore.py`; modify CLI/workflow and `docs/operations/daily-run.md`. Reuse `storage/raw_store.py` body/metadata/commit-marker integrity rules and R08's transport rather than adding a second SDK client.

**Interface:** a versioned checkpoint archive manifest identifies immutable raw triples, a consistent SQLite backup checksum, input manifests, and publication attempt state. A private `state/latest-success.json` points to a fully verified archive for the last confirmed remote publication. Distinguish candidate/checkpoint progress from remote success; neither may masquerade as the other.

- [ ] Add a test that publishes two dates, deletes all local directories/cache, restores from a fake private object store, then publishes a third date with historical returns and source baseline intact.
- [ ] Back up SQLite using its backup API into an immutable archive; upload admitted raw bodies and metadata/commit markers and verify all checksums. Write a complete manifest before updating any successful-state pointer.
- [ ] Restore and verify remote state on cache miss. A cache hit still validates its checkpoint identity against the authoritative manifest. Do not initialize a blank history on an unexpected missing/corrupt remote archive.
- [ ] Save attempt state for retry after R2/D1 failures, and advance remote-success state only after remote active-ID verification. A recoverable commit-marker mismatch after D1 success must reconcile by dataset ID instead of duplicating publication blindly.
- [ ] Include archive operations in R09 accounting; test corrupted archives, interrupted backup, exact retries, and source provenance. Verify and commit.

```python
assert clean_runner.return_1w == continuous_runner.return_1w
assert clean_runner.input_manifest_hash == continuous_runner.input_manifest_hash
assert failed_remote_attempt.latest_success == previously_verified_success
assert corrupt_archive.started_publication is False
```

### R11: restore market snapshots instead of only switching a pointer

**Finding:** F13. **Files:** add `publication/restore.py`, `pipeline/tests/integration/test_remote_restore.py`; modify `docs/operations/recovery.md` and publication bundle/archive handling from R07/R10.

**Interface:** restore accepts an explicit retained dataset bundle, verifies its SQL/object/input checksums, applies the same preflight and R2 verification gate, then reimports the market-only compact projection and asserts the active ID. It never imports saved screens, screen runs, or matches from an older database backup. Keep one compact active projection; do not reintroduce unbounded D1 snapshots.

- [ ] Add a local production-schema test: import A, create a saved screen/run, import B, edit the screen, restore A, then retrieve A's instruments/chart and verify the later screen edit/run survives.
- [ ] Observe that pointer-only rollback returns zero serving rows.
- [ ] Implement selective bundle restoration through the same orchestration as publication. Require an explicitly selected known-good bundle; unavailable/incompatible bundles fail before mutation. Preserve B for diagnosis while retention allows it.
- [ ] Replace the obsolete production rollback commands and false backfill statements in the recovery runbook with verified CLI arguments and expected checks. Keep any local-only pointer procedure clearly local-only.
- [ ] Inject object, import, and verification failures; assert the previously usable dataset remains usable whenever the remote import transaction rejects. Verify local SQL behavior but record that real D1 import atomicity needs the remote acceptance drill; do not infer it from `sqlite3.executescript` alone.
- [ ] Verify and commit.

```python
assert restored.active_dataset_id == 'A'
assert restored.instrument_count == original_a.instrument_count
assert restored.saved_screen.expression == edited_after_b.expression
assert restored.screen_run_ids == runs_before_restore
```

### R12: make deployment authentication and privacy gates truthful

**Finding:** F15. **Files:** modify `apps/api/src/middleware/access.ts`, `apps/api/src/env.ts`, relevant API tests, `.github/workflows/deploy.yml`, `scripts/ci/assert-anonymous-denied.sh`, smoke tests and deployment/security docs.

**Interface:** retain signature/issuer/audience/expiry verification for all principals. Add a separate exact allowlist for signed Access service-token identities used only for `GET /api/v1/status` smoke checks. Do not map service identities into owner emails or authorize mutations. The authenticated smoke command requires 200 and valid status JSON; anonymous checks accept only the intended denial behavior, including a validated Access-login redirect where appropriate.

- [ ] Add signed service-token tests for exact identity, wrong audience, expired token, unknown identity and denied mutation; add smoke-script tests for 401/403/login redirect versus unexpected 200/404/500 and transport failure.
- [ ] Implement the scoped service principal and require a real authenticated success in both deployment environments. Validate JSON structure rather than HTTP code alone. Bootstrap `datasetId: null` may be explicit healthy-empty state; the first-data acceptance check must separately require a real dataset ID.
- [ ] Probe anonymous `/`, a built static asset, and `/api/v1/status`; a 5xx or arbitrary redirect is not proof of privacy. Keep an owner-authenticated manual check because service auth does not prove owner login configuration.
- [ ] Move workflow-dispatch values through environment variables into quoted shell arguments; validate date/mode/numeric/path values before use. Do not interpolate arbitrary manifest/date inputs directly into shell program text. Coordinate production migration/publication jobs with a shared mutation lock so they cannot race under the currently separate concurrency groups.
- [ ] Run focused API/shell tests, then both browser journeys; verify and commit.

```typescript
expect(await smokeWithApprovedServiceIdentity()).toMatchObject({ status: 200 });
expect(await mutateWithServiceIdentity()).toMatchObject({ status: 403 });
expect(await smokeWithUnknownServiceIdentity()).toMatchObject({ status: 401 });
expect(await anonymousProbeWithServerError()).toMatchObject({ passed: false });
```

## Phase acceptance gate — run once after the R tasks

- [ ] Run the existing full Python suite, Ruff, mypy, TypeScript suite, ESLint, typecheck, browser journeys, build and Wrangler preview/production dry-runs. Use existing runtime/cache permissions; distinguish environment failures from product failures.
- [ ] Run an executable cross-boundary fixture: raw source files → normalized candidate → R07 bundle/export → local D1-compatible repository → Worker status/screen/result/chart, including R04 metadata. Do not replace this with mock DTOs or workflow string checks.
- [ ] Execute two consecutive dates, identical retry, added historical inputs, required-input corruption, cache loss, R2 failure, D1 import failure and selective rollback. Assert active dataset, source dates, raw/input lineage, saved-run source/date, and match identity throughout.
- [ ] Record deterministic capacity counts for 12,000 instruments spanning four calendar partitions; include retries/archive/retention overhead. Record local latency separately from remote throughput and account billing observations.
- [ ] Update `docs/operations/launch-checklist.md` to list each F01–F15 against its test/evidence and leave live gates explicitly pending. Update the tracked progress record and old Task 15 with a link to this plan; do not erase historical reports or existing source-policy constraints.
- [ ] Before any production publication, perform a separately authorized preview exercise with real configured bindings and a small permitted dataset: authenticated success, anonymous denial, verified data import, failed-import rollback, and actual telemetry/throughput. Dry-runs cannot satisfy these checks.

Stop implementation at a reviewable local result if owner resources or permitted source inputs are absent. That is a named external dependency, not a reason to launch another general code-review loop. Original scope completion continues through S01–S04 in the companion plan.
