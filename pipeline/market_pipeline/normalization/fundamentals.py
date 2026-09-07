"""Provider-neutral company fundamentals with filing and restatement lineage.

The normalizer intentionally stores reported values as numerator/denominator
pairs.  This keeps a filed EPS or ratio auditable and avoids silently turning
quarterly values into annual figures.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from datetime import date as Date
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, overload
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from market_pipeline.domain.models import SourceArtifact

V1_FINANCIAL_FIELDS: tuple[str, ...] = (
    "revenue",
    "operating_profit",
    "net_profit",
    "eps",
    "equity",
    "debt",
    "operating_cash_flow",
    "capital_employed",
    "roe",
    "roce",
    "debt_equity",
    "sales_growth",
    "profit_growth",
    "pe",
    "market_cap",
)


def _decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError(f"financial value is not a decimal: {value!r}") from exc


class FundamentalValue(BaseModel):
    """A reported quantity or ratio represented without losing its components."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    numerator: Decimal
    denominator: Decimal = Decimal("1")
    unit: str | None = None

    @model_validator(mode="before")
    @classmethod
    def coerce_decimal_components(cls, data: Any) -> Any:
        if isinstance(data, FundamentalValue):
            return data
        if isinstance(data, Mapping):
            result = dict(data)
            result["numerator"] = _decimal(result.get("numerator", result.get("value")))
            result["denominator"] = _decimal(result.get("denominator", "1"))
            return result
        return {"numerator": _decimal(data), "denominator": Decimal("1")}

    @property
    def value(self) -> Decimal:
        """Calculate the ratio using stable, explicit rounding."""

        if self.denominator == 0:
            raise ZeroDivisionError("financial denominator cannot be zero")
        return (self.numerator / self.denominator).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


def _period_identity(data: Mapping[str, Any]) -> UUID:
    instrument = str(data.get("instrument_id", ""))
    period_end = str(data.get("period_end", ""))
    period_type = str(data.get("period_type", ""))
    filing = str(data.get("filing_id", data.get("filing", "")))
    return uuid5(NAMESPACE_URL, f"stonks/fundamental/{instrument}|{period_end}|{period_type}|{filing}")


class FundamentalPeriod(BaseModel):
    """One reported financial period and its immutable source lineage."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID | None = None
    instrument_id: UUID
    period_end: Date
    period_type: str
    filing_id: str
    filed_at: datetime
    metrics: Mapping[str, Any] = Field(default_factory=dict)
    reported: Mapping[str, Any] | None = None
    source_artifact: SourceArtifact | None = None
    source_artifact_id: UUID | None = None
    restates_id: UUID | None = None
    supersedes_id: UUID | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_input(cls, data: Any) -> Any:
        if isinstance(data, FundamentalPeriod):
            return data
        result = dict(data)
        if result.get("source_artifact") is not None and result.get("source_artifact_id") is None:
            artifact = result["source_artifact"]
            if isinstance(artifact, SourceArtifact):
                result["source_artifact_id"] = artifact.artifact_id
            else:
                result["source_artifact_id"] = SourceArtifact.model_validate(artifact).artifact_id
        if not result.get("id"):
            result["id"] = _period_identity(result)
        if "filing_id" not in result and "filing" in result:
            result["filing_id"] = result["filing"]
        if "filed_at" not in result:
            result["filed_at"] = result.get("filing_date", datetime.min)
        reported = result.pop("reported", None)
        raw_metrics = result.get("metrics")
        metrics = raw_metrics if raw_metrics is not None else (reported or {})
        # Accept a flat row too; the canonical output remains the nested,
        # numerator/denominator representation.
        flat_metrics = {
            key: result.pop(key)
            for key in tuple(result)
            if key in V1_FINANCIAL_FIELDS and key not in {"metrics", "reported"}
        }
        if flat_metrics:
            metrics = {**dict(metrics), **flat_metrics}
        result["metrics"] = {
            str(name): FundamentalValue.model_validate(value)
            for name, value in dict(metrics).items()
            if str(name) in V1_FINANCIAL_FIELDS
        }
        result["reported"] = reported
        return result

    @property
    def filing(self) -> str:
        return self.filing_id

    @property
    def period(self) -> Date:
        return self.period_end


class FundamentalPeriods(Sequence[FundamentalPeriod]):
    """Normalized periods; singleton attribute forwarding keeps the API concise."""

    def __init__(self, periods: Iterable[FundamentalPeriod]) -> None:
        self._periods = tuple(periods)

    def __iter__(self) -> Iterator[FundamentalPeriod]:
        return iter(self._periods)

    def __len__(self) -> int:
        return len(self._periods)

    @overload
    def __getitem__(self, index: int) -> FundamentalPeriod: ...

    @overload
    def __getitem__(self, index: slice) -> Sequence[FundamentalPeriod]: ...

    def __getitem__(self, index: int | slice) -> FundamentalPeriod | Sequence[FundamentalPeriod]:
        return self._periods[index]

    def __getattr__(self, name: str) -> Any:
        if len(self._periods) == 1:
            return getattr(self._periods[0], name)
        raise AttributeError(name)

    def active(self) -> "FundamentalPeriods":
        return self


def normalize_financial_results(
    periods: Iterable[FundamentalPeriod | Mapping[str, Any]],
    *,
    existing_periods: Iterable[FundamentalPeriod | Mapping[str, Any]] | None = None,
    prior_periods: Iterable[FundamentalPeriod | Mapping[str, Any]] | None = None,
    existing_lookup: Mapping[Any, FundamentalPeriod | Mapping[str, Any]] | None = None,
    prior_lookup: Mapping[Any, FundamentalPeriod | Mapping[str, Any]] | None = None,
) -> FundamentalPeriods:
    """Normalize bounded V1 financial fields and mark superseded filings inactive."""

    normalized = [FundamentalPeriod.model_validate(period) for period in periods]
    prior_inputs: list[FundamentalPeriod | Mapping[str, Any]] = []
    if existing_periods is not None:
        prior_inputs.extend(existing_periods)
    if prior_periods is not None:
        prior_inputs.extend(prior_periods)
    if existing_lookup is not None:
        prior_inputs.extend(existing_lookup.values())
    if prior_lookup is not None:
        prior_inputs.extend(prior_lookup.values())
    prior = [FundamentalPeriod.model_validate(period) for period in prior_inputs]
    by_id: dict[UUID, FundamentalPeriod] = {
        period.id: period for period in [*prior, *normalized] if period.id is not None
    }
    superseded: set[UUID] = set()
    updated: list[FundamentalPeriod] = []
    for period in normalized:
        predecessor = period.restates_id
        if predecessor is not None:
            previous = by_id.get(predecessor)
            if previous is None:
                raise ValueError(f"unresolved restatement predecessor: {predecessor}")
            if (
                previous.instrument_id != period.instrument_id
                or previous.period_end != period.period_end
                or previous.period_type != period.period_type
            ):
                raise ValueError("restatement predecessor does not match restated period")
            if predecessor in {item.id for item in normalized}:
                superseded.add(predecessor)
            period = period.model_copy(update={"supersedes_id": predecessor})
        updated.append(period)
    return FundamentalPeriods(period for period in updated if period.id not in superseded)


def financial_value(numerator: Any, denominator: Any = 1, unit: str | None = None) -> FundamentalValue:
    """Construct a deterministic Decimal-backed reported value."""

    return FundamentalValue(numerator=_decimal(numerator), denominator=_decimal(denominator), unit=unit)


__all__ = [
    "FundamentalPeriod",
    "FundamentalPeriods",
    "FundamentalValue",
    "V1_FINANCIAL_FIELDS",
    "financial_value",
    "normalize_financial_results",
]
