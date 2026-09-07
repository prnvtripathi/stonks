# Task 3 report: NSE EOD and instrument master

## Implementation

- Added operator-supplied NSE CSV parsing for legacy and current column names,
  including symbol, series, instrument type, ISIN, report date, and close-price
  aliases.
- Added safe ZIP handling with traversal/absolute-path checks, duplicate-member
  checks, encrypted-member rejection, member-count and compressed/uncompressed
  size bounds, and CSV/TXT selection.
- Added `NseEodAdapter` on the governed `SourceAdapter` interface. It accepts an
  injected operator artifact provider but remains unable to fetch under the
  canonical disabled `nse-eod` policy.
- Added V1 NSE normalization: only `EQ` series is eligible; explicitly identified
  ETFs are classified as `AssetClass.ETF`, and all other eligible rows become
  `AssetClass.EQUITY`. SME, REIT/InvIT, preference/partly-paid, special-series,
  malformed, duplicate, and date-inconsistent rows are rejected with their raw
  series/type and original row retained for audit.
- Extended `Instrument` with optional provider-native `raw_series` and `raw_type`
  fields while keeping UUID identity based on provider, stable provider
  identifier (ISIN where present), and asset class.
- Rejected raw-artifact filenames ending in `.metadata.json` or `.commit.json`
  so user artifacts cannot collide with immutable-store sidecars.

## TDD evidence

### RED

The first focused contract run was:

```text
UV_CACHE_DIR=/private/tmp/stonks-uv-cache uv run pytest pipeline/tests/sources/test_nse_eod.py pipeline/tests/normalization/test_nse.py pipeline/tests/storage/test_raw_store.py -q
```

It failed during collection because the requested `market_pipeline.sources.nse_eod`
and `market_pipeline.normalization` modules did not exist. This was the intended
missing-contract failure. The reserved-sidecar tests were added to the existing
raw-store contract at the same time.

### GREEN

After the parser, adapter, normalization, fixture, and raw-store changes:

```text
UV_CACHE_DIR=/private/tmp/stonks-uv-cache uv run pytest pipeline/tests/sources/test_nse_eod.py pipeline/tests/normalization/test_nse.py pipeline/tests/storage/test_raw_store.py -q
...........................                                              [100%]
27 passed in 0.13s
```

The focused suite covers EQ equity, identified ETF, SME, REIT, preference,
duplicate, malformed, stable ISIN enrichment, mixed dates, ZIP traversal, and
reserved sidecar suffixes.

## Final verification

```text
UV_CACHE_DIR=/private/tmp/stonks-uv-cache uv run pytest -q
.........................................                                [100%]
41 passed in 0.12s
```

```text
UV_CACHE_DIR=/private/tmp/stonks-uv-cache uv run ruff check pipeline/market_pipeline pipeline/tests
All checks passed!
```

```text
UV_CACHE_DIR=/private/tmp/stonks-uv-cache uv run mypy pipeline
Success: no issues found in 17 source files
```

## Self-review and concerns

- The adapter's public fetch is still fail-closed by the existing registry and
  never performs network collection; parsing is available for manually supplied
  official downloads.
- Current ZIP selection intentionally requires exactly one CSV/TXT member. A
  future multi-file licensed NSE bundle should select members by an explicit
  caller-provided name rather than guessing.
- ETF inference from legacy bhavcopies is deliberately narrow (explicit ETF type,
  explicit ETF name marker, or the known `BEES` suffix); a security master can be
  supplied to enrich type and ISIN where bhavcopy columns omit them.
