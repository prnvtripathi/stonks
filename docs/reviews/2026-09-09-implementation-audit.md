# Implementation audit — 9 September 2026

Reviewed branch: `codex/indian-research-dashboard`, commit `1469b1c75e3ba194ae8b20caafb5c4e617f8330b`, including the existing untracked `publication/r2_sync.py` and `test_r2_sync.py`. No implementation files were changed. This is a correctness/integration review, not a visual redesign or a live Cloudflare deployment audit.

**Verdict:** the application has useful, tested components, but neither the original stock-dashboard scope nor the implemented AMFI publication path is release-ready. Passing isolated tests has concealed errors at ingestion, publication, API, and deployment boundaries. Continue through the new plans, not an open-ended continuation of old Task 15.

- [Correctness and publication remediation](../superpowers/plans/2026-09-09-correctness-and-publication-remediation.md): fix the existing path in 12 bounded tasks.
- [Original source-scope completion](../superpowers/plans/2026-09-09-original-source-scope-completion.md): four separately gated tasks for the missing stock/ETF/filings/benchmark integration. This is unfinished original scope, not an optional product expansion.

## Verification performed

| Check | Result |
| --- | --- |
| Python suite via `.venv/bin/python -m pytest -q` | 197 passed, including 3 tests in the existing untracked R2 work |
| TypeScript suite | 70 tests passed initially; four loopback tests were blocked by sandbox `listen EPERM`. All four passed when rerun with localhost access |
| Browser journeys | Both existing Chromium journeys passed with localhost access |
| TypeScript typecheck and ESLint | Passed |
| Ruff on publication code and untracked R2 test | Passed |
| Python mypy | 9 errors in the existing untracked `r2_sync.py`; includes missing boto3/botocore dependency and type errors |
| Focused reproductions | Confirmed the reconciliation, malformed-input, dataset-fingerprint, risk-window, API metadata, historical-run/date, shell failure, and pointer-only rollback issues below |
| Live remote import, real source fetch, production privacy, full-volume latency | Not performed; these remain distinct acceptance checks |

The initial uv invocation could not access its default cache; the installed virtual environment was used instead. Neither that restriction nor the initial socket restriction is classified as a product defect. Browser tests use their existing fixtures and do not establish that deployed D1 receives correct production data.

## Findings

### F01 — P1: reconciliation measures downloads instead of the published universe

Evidence: `pipeline/market_pipeline/cli.py:245` passes `result.completed` to `reconcile`; line 262 records a successful baseline before publication. The value counts newly checkpointed artifacts, not normalized instruments, and excludes verified skipped artifacts.

Reproduced: a three-scheme daily file followed by a one-scheme file publishes with 100% reported coverage. Repeating an identical daily manifest instead returns exit 3 because zero new downloads are interpreted as zero coverage. Build and reconcile the actual candidate before promotion; record baselines after success. Include per-source counts, missingness, and expected dates. **Remediation R01.**

### F02 — P1: required corrupt inputs become warnings and an older date is published

Evidence: `pipeline/market_pipeline/jobs/publish.py:117`–150 skips unreadable/checksum-invalid/parse-invalid artifacts; line 253 chooses the latest surviving parsed date.

Reproduced: a valid September 7 artifact plus a checksum-valid malformed September 8 artifact, requested through September 8, exits 0 and publishes September 7 as complete. Required input failures must block publication, including missing intermediate observations needed by calculations. A documented source holiday/delay is a separate state, not a parser fallback. **R01.**

### F03 — P1: changed historical inputs do not change dataset identity

Evidence: `jobs/publish.py:363` hashes only the latest artifact ID; lines 458–469 short-circuit an existing dataset.

Reproduced: publish a latest-day-only artifact, then backfill the five previous observations at the same ending date. The second call says the dataset was already published; the one-week return remains null and chart points do not expand. Fingerprint all contributing inputs and formula/normalizer versions; retain an input manifest for lineage. **R02.**

### F04 — P2: one-year risk values include all available history

Evidence: `analytics/risk.py:67` selects every point through the effective date; `jobs/publish.py:312`–318 labels the resulting values `volatility_1y` and `max_drawdown_1y`.

Reproduced with 300 observations: an initial 200→100 drop followed by a flat last 253 observations produces −50% drawdown and approximately 45.9% volatility; the trailing window yields zero for both. This also distorts MF momentum. **R03.**

### F05 — P2: MF benchmark RS is a category average, not an official benchmark

Evidence: `jobs/publish.py:187`–203 and 327–340 derive benchmark fields from an equal-weight average of available category members, including the subject scheme. The spec's benchmark section requires an official mapping, otherwise unavailable values.

Remove this substitution. Preserve category rank as a separate metric; expose benchmark RS only with explicit official mapping and aligned benchmark observations. **R03; mapped data integration S03.**

### F06 — P1: production momentum explanations lose their metadata

Evidence: `apps/api/src/repositories.ts:178`–184. `parseMetricRows` decodes metadata to an object; `buildMomentum` passes that object to `parseMetadata`, which accepts only strings.

Reproduced: weight 0.2/contribution 0.12/coverage 0.85 become 0/0/1, percent becomes ratio, and the MF cohort becomes unknown. Test the actual Python export → compact row → Worker decoder contract. **R04.**

### F07 — P2: historical results disappear, and their date/query can be wrong

Evidence: `apps/api/src/index.ts:207` restricts results to the active dataset even for an explicit historical run ID; line 235 uses execution date as the data date; line 224 returns today's saved screen alongside old matches. `apps/web/src/research.tsx:98` presents that screen source as the filter behind those matches.

Reproduced: after A→B publication, a run on A returns 404; running September 4 data on September 9 labels it September 9. Editing `Volume > 100` to `Volume > 1000` also relabels earlier matches without rerunning. Snapshot run query/version, market effective date, and match identity independently of the current serving projection. **R05.**

### F08 — P1 at target volume: pagination loads all history and performs N+1 lookups

Evidence: `apps/api/src/repositories.ts:111` loads historical match payloads via `listRuns`; `apps/api/src/index.ts:207`–224 enriches every match before slicing to a page.

A 25-row page can load 10,000 full instrument snapshots plus historical results. `D1ResearchStore.runScreen` at lines 118–128 repeats the all-history read, per-instrument fetch, and per-match statement pattern when creating a run. Cost grows with both matches and days retained. Cloudflare documents a [50-query limit per free Worker invocation](https://developers.cloudflare.com/d1/platform/limits/); thousands of independent instrument reads are therefore a functional deployment problem, not only slow pagination. Fetch selected result pages directly, and persist matching inputs using bounded set-based database work. Add query-count and returned-row tests, plus a representative-volume benchmark. **R06.**

### F09 — P1: the R2 verification gate can succeed after its manifest producer fails

Evidence: `.github/workflows/daily-data.yml:354` consumes validation output using Bash process substitution. `set -euo pipefail` does not propagate that producer's failure to the loop.

Reproduced without network: a Python producer exiting 2 allows the parent shell to reach an `D1_IMPORT_WOULD_RUN` marker. A missing/invalid manifest entry can therefore skip required verification and still permit D1 import. Validate the complete manifest synchronously before any mutation and require an explicit successful transfer result. **R08.**

### F10 — P1: upload runtime is unbounded in practice; the partial replacement is unwired

Evidence: `.github/workflows/daily-data.yml:342`–350 starts pnpm/Wrangler serially for each PUT and GET. For the prior review's 12,000-instrument / three-partition scenario, a normal run requires 72,000 CLI starts. The prior reviewer measured about 0.61 seconds per start (~12.2 hours); that timing was not rerun in this audit. A rolling three-calendar-year range can cross four yearly partitions, making the three-partition example optimistic.

GitHub-hosted jobs have a [six-hour execution limit](https://docs.github.com/en/enterprise-cloud@latest/actions/reference/limits). The existing untracked `r2_sync.py` is not called by the workflow, imports an undeclared SDK, and fails mypy. Its cancellation also does not stop already-running thread requests; a timeout must not be advertised as proof that all remote activity stopped. Reconcile and finish this work rather than starting another uploader. **R08.**

### F11 — P1: the capacity gate accepts unavailable analytics as zero usage

Evidence: workflow line 306 uses `wrangler d1 info`; the pinned Wrangler's `cli.js:224228` defaults metrics to zero and does not reject GraphQL `errors`. `publication/preflight.py:74` validates the resulting integer, losing whether it was genuinely observed. `docs/operations/security-checklist.md:137`–139 omits the analytics permission.

[Cloudflare's Analytics token instructions](https://developers.cloudflare.com/analytics/graphql-api/getting-started/authentication/api-token-auth/) require Account Analytics Read. Add explicit validated telemetry, reject errors/absent account or database data, and distinguish a successfully observed zero from unknown. Document actual permission scopes supported by the provider; do not claim finer D1 token isolation than is available. **R09.**

### F12 — P1: raw inputs and recovery state depend on an evictable cache

Evidence: `cli.py:181`–182 constructs `LocalRawStore`; workflow lines 167–213 persist `market.db`, `raw`, and `history` only through Actions cache. The export manifest contains charts/history, not the raw body/metadata/commit-marker triples required by the approved architecture. An R2 raw-store abstraction exists but is not wired into this workflow.

[GitHub documents cache eviction](https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching). A cache miss can therefore lose the only recoverable source/checkpoint history and baseline; a current-day manifest cannot reconstruct the past three years. Cache may accelerate restoration, but remote verified artifacts and recoverable checkpoint state must be authoritative. **R10.**

### F13 — P1: pointer-only rollback is incompatible with the bounded projection

Evidence: migration `0006_bounded_instrument_snapshots.sql` retains one serving row per instrument; `publication/d1_export.py:355` deletes rows belonging to the previous dataset. `docs/operations/recovery.md:71`–75 still instructs operators to perform pointer-only rollback in production.

Reproduced against the migrated SQLite schema: after pointing to the old dataset, a dataset-scoped serving query returns zero rows. Keep a verified market-data publication bundle and reimport it on rollback; do not restore the entire DB over newer saved screens/runs. **R11.**

### F14 — P2: history/retention and quota assumptions do not cover repeated operation

Evidence: `jobs/publish.py:268` retains all points; `_manifest_keys` in `publication/d1_export.py:289` includes every yearly file. `HistoryStore.write_history:197` makes only the newest year mutable; corrected closed-year data conflicts with existing bytes. Dataset-scoped charts accumulate without cleanup. Preflight checks operation estimates but no R2 byte budget or actual month-to-date usage; it assumes 22 weekday runs without budgeting manual retries or a longer month.

Define versioned historical partitions, an explicit three-year serving range, reachability-based retention of derived objects, and conservative run/retry/storage budgets. Protect raw inputs and any referenced history from garbage collection; raw retention must follow the recorded source policy. **R07, R09, R10.**

### F15 — P1: authenticated deployment smoke tests accept a broken login path

Evidence: `.github/workflows/deploy.yml:160`–162 and its production equivalent accept 401 as success. Comments acknowledge that the service-token identity is unsupported. `scripts/ci/assert-anonymous-denied.sh:34` rejects only 200, accepting unrelated errors as denial evidence.

Validate the exact signed service identity for read-only smoke routes, require authenticated 200 plus the expected response schema, and separately require genuine anonymous denial for API and static assets. Missing credentials or arbitrary 5xx/404 responses must not count as successful authentication/privacy verification. **R12.**

### F16 — missing original scope: only AMFI is connected to publication

Evidence: `jobs/publish.py:38` defines `WIRED_SOURCES = ("amfi-nav",)`; lines 237–244 merely warn about other sources. The production candidate has no equity/ETF prices, Nifty benchmark inputs, company filings or corporate-action integration. The registry's source-policy check also applies the automation-enabled requirement to supplied raw artifacts (`sources/registry.py:85`–94), so a local NSE manifest is not a supported workaround today.

The network automation restriction is an intentional constraint, not a bug to bypass. The missing integration is nevertheless unfinished original scope. Explicitly separate authorized supplied-file admission from network fetching, then connect the existing normalizers and calculations using fixtures. Real data remains gated by the recorded permissions. **S01–S04.**

## Planning/process defects

- Original Tasks 7/13/14 passed component reviews without proving the source → production serving path. Most workflow publication tests assert strings and order, not failure propagation.
- Task 15's tracked plan, ignored brief, and report describe different scopes. The ignored progress ledger has no Task 15 rounds; the original plan still leaves earlier completed tasks unchecked.
- New schema decisions invalidated rollback and historical-result assumptions without revisiting their acceptance tests.
- Full reviews and large overlapping diff reads repeatedly rediscovered requirements. The new plans use stable finding IDs, targeted tests, delta-only review, and a bounded review process.
- Source-policy, owner configuration, and live acceptance dependencies must be shown separately from implementable code defects. A dry-run deploy is packaging evidence, not live readiness.

## Boundaries of this review

No recommendation to replace the framework, rewrite the UI, remove the privacy boundary, purchase a service, or silently narrow the approved instrument universe. Existing parser/normalizer/unit tests remain valuable. Other defects may surface at real-source or remote scale; this report distinguishes measured reproductions from those unperformed gates instead of claiming an exhaustive proof of correctness.
