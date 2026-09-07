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
