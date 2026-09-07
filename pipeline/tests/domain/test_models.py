from datetime import date, datetime, timezone

import pytest
from market_pipeline.domain.models import (
    AssetClass,
    Instrument,
    MetricState,
    MetricValue,
    Observation,
    SourceArtifact,
)


def test_states_are_distinct() -> None:
    assert MetricValue.missing().state is MetricState.MISSING
    assert MetricValue.not_applicable().state is MetricState.NOT_APPLICABLE
    assert MetricValue.present(0).state is MetricState.PRESENT


def test_instrument_identity_is_stable_and_asset_scoped() -> None:
    first = Instrument.from_provider("nse", "INFY", AssetClass.EQUITY, symbol="INFY")
    second = Instrument.from_provider("nse", "INFY", AssetClass.EQUITY, symbol="INFY")
    etf = Instrument.from_provider("nse", "INFY", AssetClass.ETF, symbol="INFY")

    assert first.instrument_id == second.instrument_id
    assert first.instrument_id != etf.instrument_id
    assert first.instrument_id.version == 5


def test_observation_keeps_provenance_and_effective_date() -> None:
    artifact = SourceArtifact(
        source_id="nse-eod",
        source_url="https://www.nseindia.com/all-reports",
        retrieved_at=datetime(2026, 9, 7, tzinfo=timezone.utc),
        effective_date=date(2026, 9, 4),
        checksum="a" * 64,
        adapter_version="1.0.0",
        terms_url="https://www.nseindia.com/terms-of-use",
        filename="bhavcopy.csv",
    )
    instrument = Instrument.from_provider("nse", "INFY", AssetClass.EQUITY)
    observation = Observation(
        instrument_id=instrument.instrument_id,
        effective_date=date(2026, 9, 4),
        metric="close",
        value=MetricValue.present(1_500.25),
        source_artifact_id=artifact.artifact_id,
    )

    assert observation.value.value == 1_500.25
    assert observation.source_artifact_id == artifact.artifact_id


def test_incomplete_identity_metadata_is_a_validation_error() -> None:
    with pytest.raises(ValueError):
        Instrument.model_validate({"provider": "nse", "asset_class": AssetClass.EQUITY})
    with pytest.raises(ValueError):
        SourceArtifact.model_validate({"source_id": "nse-eod", "terms_url": "https://example.test"})
