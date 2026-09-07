from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4

from market_pipeline.domain.models import SourceArtifact
from market_pipeline.normalization.fundamentals import (
    FundamentalPeriod,
    normalize_financial_results,
)

INSTRUMENT_ID = uuid4()
ORIGINAL = FundamentalPeriod(
    instrument_id=INSTRUMENT_ID,
    period_end=date(2026, 3, 31),
    period_type="quarter",
    filing_id="filing-original",
    filed_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
    metrics={"revenue": {"numerator": Decimal("100.00")}},
)
RESTATED = FundamentalPeriod(
    instrument_id=INSTRUMENT_ID,
    period_end=date(2026, 3, 31),
    period_type="quarter",
    filing_id="filing-restated",
    filed_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    restates_id=ORIGINAL.id,
    metrics={"revenue": {"numerator": Decimal("110.00")}},
)


def test_restatement_supersedes_original() -> None:
    result = normalize_financial_results([ORIGINAL, RESTATED])

    active = result.active()
    assert len(active) == 1
    assert active[0].id == RESTATED.id
    assert active[0].supersedes_id == ORIGINAL.id


def test_normalizer_preserves_reported_components_and_does_not_annualize_quarter() -> None:
    result = normalize_financial_results(
        [
            {
                "instrument_id": INSTRUMENT_ID,
                "period_end": date(2026, 3, 31),
                "period_type": "quarter",
                "filing_id": "filing-1",
                "filed_at": datetime(2026, 5, 1, tzinfo=timezone.utc),
                "reported": {
                    "revenue": {"numerator": "100.00", "denominator": "1"},
                    "eps": {"numerator": "2.00", "denominator": "1"},
                },
            }
        ]
    )

    period = result.active()[0]
    assert period.period_type == "quarter"
    assert period.metrics["revenue"].numerator == Decimal("100.00")
    assert period.metrics["revenue"].denominator == Decimal("1")
    assert period.metrics["eps"].numerator == Decimal("2.00")
    assert "annualized_revenue" not in period.metrics


def test_normalizer_requires_official_artifact_provenance() -> None:
    source = SourceArtifact(
        source_id="nse-filings-xbrl",
        source_url="https://www.nseindia.com/companies-listing/corporate-filings-application",
        retrieved_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        effective_date=date(2026, 6, 1),
        checksum="0" * 64,
        adapter_version="1.0.0",
        terms_url="https://www.nseindia.com/terms-of-use",
    )

    result = normalize_financial_results(
        [
            {
                "instrument_id": INSTRUMENT_ID,
                "period_end": date(2026, 3, 31),
                "period_type": "annual",
                "filing_id": "filing-1",
                "filed_at": datetime(2026, 5, 1, tzinfo=timezone.utc),
                "source_artifact": source,
                "reported": {"market_cap": "1000"},
            }
        ]
    )
    assert result.active()[0].source_artifact_id == source.artifact_id
