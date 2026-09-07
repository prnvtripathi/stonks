from datetime import date

import pytest
from market_pipeline.sources.nse_filings import (
    NseFilingsAdapter,
    NseFilingsProvenanceError,
    parse_financial_results_csv,
)


def test_parse_official_csv_requires_provenance() -> None:
    body = b"SYMBOL,PERIOD_END,PERIOD_TYPE,REVENUE\nABC,2026-03-31,quarter,100\n"

    with pytest.raises(NseFilingsProvenanceError):
        parse_financial_results_csv(body, source_url="https://evil.example/file.csv")


def test_filings_adapter_never_automates_disabled_nse_source() -> None:
    adapter = NseFilingsAdapter(lambda _effective_date: b"SYMBOL,REVENUE\nABC,1\n")

    with pytest.raises(Exception):
        adapter.fetch(date(2026, 6, 1))

