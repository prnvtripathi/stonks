# Task 2 report: domain, source governance, and immutable raw storage

## Implementation

- Added frozen Pydantic domain models for `AssetClass`, `MetricState`, `MetricValue`,
  `Instrument`, `Observation`, `SourcePolicy`, `SourceArtifact`, and `FetchedArtifact`.
  Instrument and artifact identities are deterministic UUIDv5 values; observations
  retain effective-date and source-artifact lineage.
- Added `SourceAdapter` and `RawStore` protocols. R2 storage is represented by an
  injected S3-compatible `ObjectClient`, so tests do not need Cloudflare access.
- Added an official-source allowlist containing only `nse-eod`, `nse-filings-xbrl`,
  `amfi-nav`, and `nifty-500`. Unknown, disabled, non-retainable, or missing-terms
  policies fail closed with `SourcePolicyError`.
- Added local and injected-client R2 immutable stores. Object keys use
  `raw/{source}/{date}/{sha256}/{filename}` with sibling `metadata.json`; checksum
  mismatches, conflicting bytes, conflicting metadata, unsafe source IDs, unsafe
  filenames, and escaping local reads are rejected.
- Added the source policy operations document at `docs/operations/source-policy.md`.

## Files

- `pipeline/market_pipeline/domain/models.py`
- `pipeline/market_pipeline/domain/__init__.py`
- `pipeline/market_pipeline/sources/base.py`
- `pipeline/market_pipeline/sources/registry.py`
- `pipeline/market_pipeline/sources/__init__.py`
- `pipeline/market_pipeline/storage/raw_store.py`
- `pipeline/tests/domain/test_models.py`
- `pipeline/tests/sources/test_registry.py`
- `pipeline/tests/storage/test_raw_store.py`
- `docs/operations/source-policy.md`

## TDD evidence

### RED

Command:

```text
UV_CACHE_DIR=/private/tmp/stonks-uv-cache uv run pytest pipeline/tests/domain pipeline/tests/sources pipeline/tests/storage -q
```

Output:

```text
ERROR collecting ... ModuleNotFoundError: No module named 'market_pipeline.domain'
ERROR collecting ... ModuleNotFoundError: No module named 'market_pipeline.sources'
ERROR collecting ... ModuleNotFoundError: No module named 'market_pipeline.domain'
!!!!!!!!!!!!!!!!!!!! Interrupted: 3 errors during collection !!!!!!!!!!!!!!!!!!!!
```

This was the expected missing-contract failure before production implementation.

### GREEN

After implementing the contracts and stores:

```text
UV_CACHE_DIR=/private/tmp/stonks-uv-cache uv run pytest pipeline/tests/domain pipeline/tests/sources pipeline/tests/storage -q
.............                                                            [100%]
13 passed in 0.10s
```

The later hardening tests for invalid model inputs and escaping local reads brought
the focused suite to 15 tests; all remained green.

## Final verification

```text
UV_CACHE_DIR=/private/tmp/stonks-uv-cache uv run pytest -q
...............                                                          [100%]
15 passed in 0.10s
```

```text
UV_CACHE_DIR=/private/tmp/stonks-uv-cache uv run ruff check pipeline/market_pipeline pipeline/tests
All checks passed!
```

```text
UV_CACHE_DIR=/private/tmp/stonks-uv-cache uv run mypy pipeline
Success: no issues found in 12 source files
```

## Self-review

- Confirmed frozen models reject mutation and keep `missing` distinct from
  `not_applicable`.
- Confirmed UUIDv5 identity changes when the asset class changes but is stable for
  the same provider tuple.
- Confirmed repeated local and injected-client R2 writes are idempotent, while body
  and metadata conflicts fail without replacing the original.
- Confirmed local reads resolve and constrain paths beneath the configured root.
- Confirmed registry hostnames are limited to NSE, AMFI, and Nifty Indices official
  domains and that unknown source IDs fail closed.

## Concerns

- The registry records the approved official terms-reference URLs and enables the
  four approved source IDs. Production should re-review each source's current usage
  and retention terms before scheduled automation; setting either policy flag false
  disables it without a fallback feed.
- The injected R2 client intentionally exposes a small conditional `put_if_absent/get`
  contract. The
  deployment adapter will need to wrap the chosen Cloudflare S3-compatible SDK to
  this contract.

## Fix round 1/5

### Findings addressed

- `nse-eod` and `nse-filings-xbrl` are now disabled in the canonical registry by
  default because systematic NSE website collection is not permitted by the current
  terms. They can still be parsed as user-supplied official downloads by a future
  explicit workflow. Automation is enabled only when the canonical policy contains
  a recorded `permission_reference`/written license; AMFI and Nifty entries currently
  carry their official terms references.
- `assert_source_enabled` now resolves the canonical registry entry and requires a
  complete policy equality match. `assert_adapter_enabled`/`SourceAdapter.fetch` gate
  adapter fetches, and raw stores validate artifact URL/terms provenance against the
  canonical entry before persistence. Forged terms, URLs, and unknown IDs fail closed.
- Metadata is now keyed per artifact filename (`{object_key}.metadata.json`). Local
  storage preflights body and metadata before creating either file; R2 preflights
  metadata before body creation, preventing a known metadata conflict from leaving
  a body behind.
- The injected R2 client now requires atomic `put_if_absent`. Existing objects are
  compared after a conditional-create race, and different bytes are rejected without
  any overwrite.

### Fix-round RED evidence

Command:

```text
UV_CACHE_DIR=/private/tmp/stonks-uv-cache uv run pytest pipeline/tests/sources/test_registry.py pipeline/tests/storage/test_raw_store.py -q
```

Output before the fixes:

```text
8 failed, 8 passed in 0.12s
```

Failures covered enabled NSE policies, forged policy acceptance, shared metadata,
and the old unconditional R2 client method.

### Fix-round GREEN evidence

```text
UV_CACHE_DIR=/private/tmp/stonks-uv-cache uv run pytest pipeline/tests/sources/test_registry.py pipeline/tests/storage/test_raw_store.py -q
.................                                                        [100%]
17 passed in 0.10s
```

After adding adapter-fetch and preflight conflict coverage:

```text
UV_CACHE_DIR=/private/tmp/stonks-uv-cache uv run pytest pipeline/tests/sources/test_registry.py pipeline/tests/storage/test_raw_store.py -q
...................                                                      [100%]
19 passed in 0.11s
```

### Fix-round final verification

```text
UV_CACHE_DIR=/private/tmp/stonks-uv-cache uv run pytest -q
........................                                                 [100%]
24 passed in 0.12s
```

```text
UV_CACHE_DIR=/private/tmp/stonks-uv-cache uv run ruff check pipeline/market_pipeline pipeline/tests
All checks passed!
```

```text
UV_CACHE_DIR=/private/tmp/stonks-uv-cache uv run mypy pipeline
Success: no issues found in 12 source files
```

No existing Python tests were intentionally removed.

### Fix-round self-review and concerns

- Verified disabled NSE policies cannot pass adapter or raw-store gates, while policy
  equality rejects same-ID objects with changed URL, terms, or permission metadata.
- Verified same-content artifacts with different filenames receive independent
  metadata, and pre-existing metadata conflicts do not create a body.
- Verified R2 writes use conditional create for both body and metadata and compare
  the winner's bytes after a race; no unconditional overwrite path remains.
- Remaining concern: production must obtain and record an actual written permission or
  license before changing a disabled NSE canonical entry to enabled. The registry is
  deliberately fail-closed until that evidence exists.

## Fix round 2/5

### Findings addressed

- Replaced the directly callable fetch protocol with a governed `SourceAdapter`
  wrapper. Its public `fetch()` checks the complete canonical policy before invoking
  only the implementation's private `_fetch()` method; no direct-fetch helper is
  exported.
- Added immutable commit markers alongside each body and per-artifact metadata. A
  marker records the body and metadata SHA-256 values and is created conditionally
  only after both objects. `get()` and `list()` expose an artifact only when the
  marker and both hashes match. Local creation uses exclusive file creation; R2 uses
  injected conditional `put_if_absent` for body, metadata, and marker. Metadata-race
  tests confirm failed writes remain uncommitted and are not visible.
- Added canonical approved HTTPS origin/path-prefix rules to each source policy.
  Fetched artifact URLs can vary across official download paths while sibling hosts,
  deceptive hostnames, HTTP, and disallowed paths fail closed. Exact fetched URLs
  remain in artifact metadata.

### Fix-round RED evidence

```text
UV_CACHE_DIR=/private/tmp/stonks-uv-cache uv run pytest pipeline/tests/sources/test_registry.py pipeline/tests/storage/test_raw_store.py -q
8 failed, 8 passed in 0.12s
```

The failures demonstrated direct-fetch/forged-policy acceptance, enabled NSE entries,
shared metadata, non-conditional R2 writes, and missing race/URL protections.

The later listing test independently failed before its implementation:

```text
1 failed, 1 passed in 0.10s
AttributeError: 'R2RawStore' object has no attribute 'list'
```

### Fix-round GREEN and final verification

```text
UV_CACHE_DIR=/private/tmp/stonks-uv-cache uv run pytest pipeline/tests/sources/test_registry.py pipeline/tests/storage/test_raw_store.py -q
........................                                                 [100%]
24 passed in 0.11s
```

```text
UV_CACHE_DIR=/private/tmp/stonks-uv-cache uv run pytest -q
.............................                                            [100%]
29 passed in 0.10s
```

```text
UV_CACHE_DIR=/private/tmp/stonks-uv-cache uv run ruff check pipeline/market_pipeline pipeline/tests
All checks passed!
```

```text
UV_CACHE_DIR=/private/tmp/stonks-uv-cache uv run mypy pipeline
Success: no issues found in 12 source files
```

### Fix-round self-review and concerns

- Verified adapter callers can only use the governed public wrapper, while private
  implementations are policy-checked before their `_fetch()` method runs.
- Verified marker creation is conditional and immutable for local and R2 stores;
  adversarial metadata races leave bodies uncommitted, and listing excludes orphans.
- Verified URL validation compares exact HTTPS hostnames plus path boundaries, so a
  deceptive hostname or sibling path cannot satisfy an approved prefix.
- Remaining concern: the deployment wrapper must map the Cloudflare S3-compatible SDK
  to conditional `put_if_absent`; NSE automation remains disabled pending written
  permission/license evidence.
