"""Stable, provider-neutral records shared by market-data adapters."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AssetClass(StrEnum):
    """Asset classes supported by the first version of the pipeline."""

    EQUITY = "equity"
    ETF = "etf"
    MUTUAL_FUND = "mutual_fund"


class MetricState(StrEnum):
    """A metric can be available, absent, or not meaningful for an asset."""

    PRESENT = "present"
    MISSING = "missing"
    NOT_APPLICABLE = "not_applicable"


MetricScalar = int | float | str | bool | date | datetime


class MetricValue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    state: MetricState
    value: MetricScalar | None = None

    @model_validator(mode="after")
    def validate_state(self) -> MetricValue:
        if self.state is MetricState.PRESENT and self.value is None:
            raise ValueError("present metrics must have a value")
        if self.state is not MetricState.PRESENT and self.value is not None:
            raise ValueError("missing and not-applicable metrics cannot have a value")
        return self

    @classmethod
    def present(cls, value: MetricScalar) -> MetricValue:
        return cls(state=MetricState.PRESENT, value=value)

    @classmethod
    def missing(cls) -> MetricValue:
        return cls(state=MetricState.MISSING)

    @classmethod
    def not_applicable(cls) -> MetricValue:
        return cls(state=MetricState.NOT_APPLICABLE)


INSTRUMENT_NAMESPACE: UUID = uuid5(NAMESPACE_URL, "stonks/market-pipeline/instrument")


class Instrument(BaseModel):
    """An asset with a stable ID independent of a changeable display symbol."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    instrument_id: UUID
    provider: str
    provider_identifier: str
    asset_class: AssetClass
    symbol: str | None = None
    name: str | None = None
    active: bool = True
    # Provider-native values are retained for source reconciliation/audit. They
    # do not participate in the stable UUID identity.
    raw_series: str | None = None
    raw_type: str | None = None

    @model_validator(mode="before")
    @classmethod
    def assign_stable_id(cls, data: Any) -> Any:
        if (
            isinstance(data, dict)
            and not data.get("instrument_id")
            and {"provider", "provider_identifier", "asset_class"}.issubset(data)
        ):
            provider = str(data["provider"])
            identifier = str(data["provider_identifier"])
            asset_class = data["asset_class"]
            asset_value = asset_class.value if isinstance(asset_class, AssetClass) else str(asset_class)
            data = dict(data)
            data["instrument_id"] = uuid5(
                INSTRUMENT_NAMESPACE,
                f"{provider}|{identifier}|{asset_value}",
            )
        return data

    @classmethod
    def from_provider(
        cls,
        provider: str,
        provider_identifier: str,
        asset_class: AssetClass,
        *,
        symbol: str | None = None,
        name: str | None = None,
        active: bool = True,
        raw_series: str | None = None,
        raw_type: str | None = None,
    ) -> Instrument:
        return cls.model_validate(
            {
                "provider": provider,
                "provider_identifier": provider_identifier,
                "asset_class": asset_class,
                "symbol": symbol,
                "name": name,
                "active": active,
                "raw_series": raw_series,
                "raw_type": raw_type,
            }
        )

    @property
    def id(self) -> UUID:
        return self.instrument_id


class SourcePolicy(BaseModel):
    """Usage decision and provenance metadata for one official source."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str
    automation_allowed: bool = False
    source_url: str = ""
    terms_url: str
    retention_allowed: bool = True
    permission_reference: str | None = None
    approved_url_prefixes: tuple[str, ...] = ()
    description: str = ""


class SourceArtifact(BaseModel):
    """Metadata for an immutable downloaded artifact; the body lives in a RawStore."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str
    source_url: str
    retrieved_at: datetime
    effective_date: date
    checksum: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-fA-F]{64}$")
    adapter_version: str
    terms_url: str
    filename: str = "artifact.bin"
    artifact_id: UUID | None = None

    @model_validator(mode="before")
    @classmethod
    def assign_artifact_id(cls, data: Any) -> Any:
        if (
            isinstance(data, dict)
            and not data.get("artifact_id")
            and {"source_id", "effective_date", "checksum"}.issubset(data)
        ):
            data = dict(data)
            effective_date = data["effective_date"]
            date_value = (
                effective_date.isoformat()
                if isinstance(effective_date, date)
                else str(effective_date)
            )
            identity = (
                f"{data['source_id']}|{date_value}|{str(data['checksum']).lower()}|"
                f"{data.get('filename', 'artifact.bin')}"
            )
            data["artifact_id"] = uuid5(NAMESPACE_URL, "stonks/artifact/" + identity)
        return data

    @property
    def retrieval_timestamp(self) -> datetime:
        return self.retrieved_at


class Observation(BaseModel):
    """One dated metric value and its source artifact lineage."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    instrument_id: UUID
    effective_date: date
    metric: str
    value: MetricValue
    source_artifact_id: UUID | None = None

    @model_validator(mode="before")
    @classmethod
    def coerce_scalar_value(cls, data: Any) -> Any:
        if isinstance(data, dict) and "value" in data and not isinstance(data["value"], MetricValue):
            data = dict(data)
            data["value"] = MetricValue.present(data["value"])
        return data

    @property
    def metric_name(self) -> str:
        return self.metric


class FetchedArtifact(BaseModel):
    """Artifact metadata plus downloaded bytes returned by a source adapter."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    artifact: SourceArtifact
    body: bytes

    @property
    def source_artifact(self) -> SourceArtifact:
        return self.artifact
