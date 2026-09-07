# Task 5 implementation report

## Scope

- Added local-only, provenance-validated CSV/XBRL NSE filing parsers and a
  policy-gated adapter. NSE automation remains disabled by the canonical
  source registry.
- Added Decimal-backed `FundamentalValue` numerator/denominator records,
  bounded V1 financial-field mapping, filing metadata, source-artifact
  lineage, and explicit restatement supersession.
- Added split, consolidation, bonus, and dividend corporate actions. Price
  adjustment factors are kept separate from raw closes; dividends do not alter
  V1 price-return factors.

## RED evidence

Before implementation, the new focused suite failed during collection with
`ModuleNotFoundError` for `fundamentals`, `corporate_actions`, and
`nse_filings`.

## GREEN evidence

- `UV_CACHE_DIR=/private/tmp/stonks-uv-cache uv run pytest pipeline/tests/normalization/test_fundamentals.py pipeline/tests/normalization/test_corporate_actions.py pipeline/tests/sources/test_nse_filings.py -q` — 8 passed.
- `UV_CACHE_DIR=/private/tmp/stonks-uv-cache uv run pytest -q` — 79 passed.
- `UV_CACHE_DIR=/private/tmp/stonks-uv-cache uv run ruff check pipeline` — all checks passed.
- `UV_CACHE_DIR=/private/tmp/stonks-uv-cache uv run mypy pipeline` — no issues found.

## Concerns

NSE filings parsing is intentionally limited to operator-supplied artifacts;
the parser does not fetch from NSE. XBRL support covers the compact fact/context
shape required by V1 and ignores unknown taxonomy facts.

## Fix round 1 evidence

- Added context-ID-aware XBRL selection. Duration and instant facts are
  selected independently for the target filing end; comparative contexts are
  ignored, period type is inferred from duration length or explicit metadata,
  and ambiguous contexts are rejected.
- Removed parser-side provenance minting. CSV/XBRL parsing now requires a
  supplied `SourceArtifact` or `FetchedArtifact`, validates checksum, optional
  expected effective date, approved URL path, and canonical terms URL.
- Added incremental restatement lookup via `existing_periods`,
  `prior_periods`, `existing_lookup`, and `prior_lookup`, including unresolved
  and cross-company/period rejection.
- Added annual, quarterly/comparative, instant-balance-sheet, ambiguous-XBRL,
  and successful committed-CSV fixture coverage.

Fix-round verification:

- `UV_CACHE_DIR=/private/tmp/stonks-uv-cache uv run pytest pipeline/tests/normalization/test_fundamentals.py pipeline/tests/normalization/test_corporate_actions.py pipeline/tests/sources/test_nse_filings.py -q` — 20 passed.
- `UV_CACHE_DIR=/private/tmp/stonks-uv-cache uv run ruff check pipeline` — all checks passed.
- `UV_CACHE_DIR=/private/tmp/stonks-uv-cache uv run mypy pipeline` — no issues found in 28 source files.
- `UV_CACHE_DIR=/private/tmp/stonks-uv-cache uv run pytest -q` — 91 passed.

## Fix round 1/5 evidence

- XBRL parsing now indexes context IDs and independently selects compatible
  duration/instant contexts ending at the target period end. Comparative
  contexts cannot contribute facts; duration length or explicit metadata
  determines quarter, half-year, nine-month, annual, or instant period type;
  ambiguous contexts are rejected.
- Parser provenance minting was removed. Parsing requires a supplied
  `SourceArtifact` or `FetchedArtifact`, validates checksum, optional expected
  effective date, canonical NSE URL path, and canonical terms URL.
- Incremental restatements resolve against batch and prior records through
  `existing_periods`, `prior_periods`, `existing_lookup`, or `prior_lookup`,
  with unresolved and cross-company/period links rejected.
- Added and exercised quarterly/comparative, annual, instant-balance-sheet,
  and ambiguous XBRL fixtures plus the committed CSV fixture.

Fix-round verification:

- `UV_CACHE_DIR=/private/tmp/stonks-uv-cache uv run pytest pipeline/tests/normalization/test_fundamentals.py pipeline/tests/normalization/test_corporate_actions.py pipeline/tests/sources/test_nse_filings.py -q` — 20 passed.
- `UV_CACHE_DIR=/private/tmp/stonks-uv-cache uv run ruff check pipeline` — all checks passed.
- `UV_CACHE_DIR=/private/tmp/stonks-uv-cache uv run mypy pipeline` — no issues found in 28 source files.
- `UV_CACHE_DIR=/private/tmp/stonks-uv-cache uv run pytest -q` — 91 passed.
