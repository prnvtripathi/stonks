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
- The injected R2 client intentionally exposes a small `put/get` contract. The
  deployment adapter will need to wrap the chosen Cloudflare S3-compatible SDK to
  this contract.
