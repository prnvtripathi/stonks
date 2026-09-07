from datetime import date, datetime, timezone
from hashlib import sha256
from pathlib import Path

import pytest
from market_pipeline.domain.models import FetchedArtifact, SourceArtifact
from market_pipeline.sources.nse_filings import (
    NseFilingsAdapter,
    NseFilingsProvenanceError,
    parse_financial_results_csv,
    parse_financial_results_xbrl,
)

OFFICIAL_URL = "https://www.nseindia.com/companies-listing/corporate-filings-application"
TERMS_URL = "https://www.nseindia.com/terms-of-use"


def _artifact(body: bytes, *, effective_date: date = date(2026, 6, 1)) -> SourceArtifact:
    return SourceArtifact(
        source_id="nse-filings-xbrl",
        source_url=OFFICIAL_URL,
        retrieved_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        effective_date=effective_date,
        checksum=sha256(body).hexdigest(),
        adapter_version="1.0.0",
        terms_url=TERMS_URL,
        filename="fixture.csv",
    )


def test_parse_official_csv_requires_provenance() -> None:
    body = b"SYMBOL,PERIOD_END,PERIOD_TYPE,REVENUE\nABC,2026-03-31,quarter,100\n"

    with pytest.raises(NseFilingsProvenanceError, match="SourceArtifact"):
        parse_financial_results_csv(body)


def test_parse_committed_csv_fixture_with_official_provenance() -> None:
    body = Path("pipeline/tests/fixtures/nse/financial_results.csv").read_bytes()

    periods = parse_financial_results_csv(body, source_artifact=_artifact(body))

    assert periods[0].period_type == "quarter"
    assert periods[0].metrics["revenue"].numerator == 100
    assert periods[0].source_artifact_id == _artifact(body).artifact_id


def test_fetched_artifact_carries_provenance_to_parser() -> None:
    body = b"SYMBOL,PERIOD_END,PERIOD_TYPE,REVENUE\nABC,2026-03-31,quarter,100\n"

    periods = parse_financial_results_csv(FetchedArtifact(artifact=_artifact(body), body=body))

    assert periods[0].source_artifact_id == _artifact(body).artifact_id


def test_parse_rejects_checksum_mismatch() -> None:
    body = b"SYMBOL,PERIOD_END,PERIOD_TYPE,REVENUE\nABC,2026-03-31,quarter,100\n"
    artifact = _artifact(body).model_copy(update={"checksum": "0" * 64})

    with pytest.raises(NseFilingsProvenanceError, match="checksum"):
        parse_financial_results_csv(body, source_artifact=artifact)


def test_parse_rejects_effective_date_mismatch() -> None:
    body = b"SYMBOL,PERIOD_END,PERIOD_TYPE,REVENUE\nABC,2026-03-31,quarter,100\n"

    with pytest.raises(NseFilingsProvenanceError, match="effective date"):
        parse_financial_results_csv(
            body,
            source_artifact=_artifact(body),
            effective_date=date(2026, 6, 2),
        )


def test_parse_rejects_unapproved_official_path() -> None:
    body = b"SYMBOL,PERIOD_END,PERIOD_TYPE,REVENUE\nABC,2026-03-31,quarter,100\n"
    artifact = _artifact(body).model_copy(update={"source_url": "https://www.nseindia.com/evil"})

    with pytest.raises(NseFilingsProvenanceError, match="approved path"):
        parse_financial_results_csv(body, source_artifact=artifact)


def test_parse_xbrl_uses_matching_current_contexts_without_comparative_mixing() -> None:
    body = Path("pipeline/tests/fixtures/nse/financial_results_quarterly.xbrl").read_bytes()

    periods = parse_financial_results_xbrl(body, source_artifact=_artifact(body))

    metrics = periods[0].metrics
    assert periods[0].period_type == "quarter"
    assert metrics["revenue"].numerator == 100
    assert metrics["equity"].numerator == 500


def test_parse_xbrl_rejects_ambiguous_duration_contexts() -> None:
    body = Path("pipeline/tests/fixtures/nse/financial_results_ambiguous.xbrl").read_bytes()

    with pytest.raises(ValueError, match="ambiguous duration contexts"):
        parse_financial_results_xbrl(body, source_artifact=_artifact(body))


def test_parse_xbrl_infers_annual_period_and_ignores_comparative() -> None:
    body = Path("pipeline/tests/fixtures/nse/financial_results_annual.xbrl").read_bytes()

    periods = parse_financial_results_xbrl(body, source_artifact=_artifact(body))

    assert periods[0].period_type == "annual"
    assert periods[0].metrics["revenue"].numerator == 400


def test_parse_xbrl_supports_instant_balance_sheet_without_duration() -> None:
    body = Path("pipeline/tests/fixtures/nse/financial_results_instant.xbrl").read_bytes()

    periods = parse_financial_results_xbrl(body, source_artifact=_artifact(body))

    assert periods[0].period_type == "instant"
    assert periods[0].metrics["equity"].numerator == 500


def test_filings_adapter_never_automates_disabled_nse_source() -> None:
    adapter = NseFilingsAdapter(lambda _effective_date: b"SYMBOL,REVENUE\nABC,1\n")

    with pytest.raises(Exception):
        adapter.fetch(date(2026, 6, 1))
