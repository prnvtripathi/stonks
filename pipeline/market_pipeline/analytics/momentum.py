"""Explainable cohort-relative momentum composites."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from market_pipeline.analytics._utils import as_date, as_decimal

FORMULA_VERSION = "momentum-v2-cohort"
EQUITY_WEIGHTS = {
    "weighted_12m_rs_percentile": Decimal("0.35"),
    "six_month_performance": Decimal("0.20"),
    "three_month_performance": Decimal("0.15"),
    "trend_strength": Decimal("0.10"),
    "proximity_to_52_week_high": Decimal("0.10"),
    "volume_confirmation": Decimal("0.10"),
}
MF_WEIGHTS = {
    "three_month_return": Decimal("0.15"),
    "six_month_return": Decimal("0.25"),
    "twelve_month_return": Decimal("0.30"),
    "category_rank": Decimal("0.15"),
    "inverse_volatility": Decimal("0.075"),
    "inverse_max_drawdown": Decimal("0.075"),
}
_ALIASES = {
    "weighted_12m_rs_percentile": ("weighted_12m_rs_percentile", "weighted_12m_rs", "rs_percentile"),
    "six_month_performance": ("six_month_performance", "six_month_return", "return_6m"),
    "three_month_performance": ("three_month_performance", "three_month_return", "return_3m"),
    "trend_strength": ("trend_strength", "trend", "ma_trend"),
    "proximity_to_52_week_high": ("proximity_to_52_week_high", "high_proximity", "52_week_high_proximity"),
    "volume_confirmation": ("volume_confirmation", "volume", "volume_ratio"),
    "three_month_return": ("three_month_return", "three_month_performance", "return_3m"),
    "six_month_return": ("six_month_return", "six_month_performance", "return_6m"),
    "twelve_month_return": ("twelve_month_return", "twelve_month_performance", "return_12m"),
    "category_rank": ("category_rank", "category_relative_rank", "category_percentile"),
    "inverse_volatility": ("inverse_volatility",),
    "inverse_max_drawdown": ("inverse_max_drawdown",),
}


def _asset_class(value: Any) -> str:
    name = value.value if hasattr(value, "value") else str(value)
    normalized = name.lower().replace("-", "_")
    if normalized not in {"equity", "etf", "mutual_fund"}:
        raise ValueError(f"unsupported asset class: {name}")
    return normalized


@dataclass(frozen=True)
class NormalizedMomentumInput:
    """Validated unit-interval components used by the composite calculator."""

    components: Mapping[str, Decimal]
    asset_class: str
    category: str | None = None

    def __post_init__(self) -> None:
        _asset_class(self.asset_class)
        for key, value in self.components.items():
            if value < Decimal(0) or value > Decimal(1):
                raise ValueError(f"normalized component {key} must be in 0..1")

    @classmethod
    def from_mapping(
        cls,
        components: Mapping[str, Any],
        *,
        asset_class: Any = "equity",
        category: str | None = None,
    ) -> NormalizedMomentumInput:
        normalized_asset_class = _asset_class(asset_class)
        weights = MF_WEIGHTS if normalized_asset_class == "mutual_fund" else EQUITY_WEIGHTS
        converted: dict[str, Decimal] = {}
        for key in weights:
            if key not in components:
                continue
            value = as_decimal(components[key])
            if value is None or value < Decimal(0) or value > Decimal(1):
                raise ValueError(f"normalized component {key} must be in 0..1")
            converted[key] = value
        return cls(converted, normalized_asset_class, category)

    @property
    def cohort(self) -> str:
        if self.asset_class == "mutual_fund":
            if self.category is None:
                raise ValueError("mutual-fund normalized input requires an explicit category")
            return f"mutual_fund:{self.category}"
        return self.asset_class


@dataclass(frozen=True)
class MomentumScore:
    score: Decimal | None
    raw_components: Mapping[str, Decimal | None]
    normalized_components: Mapping[str, Decimal | None]
    cohort: str
    effective_date: date | None
    coverage: Decimal
    formula_version: str = FORMULA_VERSION

    @property
    def value(self) -> Decimal | None:
        return self.score


def serialize_momentum_provenance(
    instrument_id: str,
    score: MomentumScore,
    *,
    source_artifact_id: str | None = None,
    warning: str | None = None,
) -> list[dict[str, Any]]:
    """Serialize a score into ``latest_metrics`` rows for D1 publication.

    The score row carries the cohort-level provenance while one stable metric
    row is emitted for every asset-specific component. Raw component values
    retain Task 6's fractional return/risk units; ``normalized_value`` and
    component metadata are always unit-interval values. A missing score is
    represented as ``state=missing`` rather than as numeric zero.
    """

    source_date = score.effective_date.isoformat() if score.effective_date else None
    score_warning = warning or ("Insufficient component coverage." if score.score is None or score.coverage < Decimal("1") else None)
    common: dict[str, Any] = {
        "instrument_id": instrument_id,
        "effective_date": source_date,
        "formula_version": score.formula_version,
        "source_artifact_id": source_artifact_id,
    }
    score_metadata: dict[str, Any] = {
        "cohort": score.cohort,
        "coverage": float(score.coverage),
        "source_date": source_date,
        "component_ids": list(score.raw_components),
    }
    if score_warning is not None:
        score_metadata["warning"] = score_warning
    rows: list[dict[str, Any]] = [{
        **common,
        "metric": "momentum_score",
        "value": float(score.score) if score.score is not None else None,
        "state": "present" if score.score is not None else "missing",
        "normalized_value": float(score.score) if score.score is not None else None,
        "metadata": score_metadata,
    }]
    weights = MF_WEIGHTS if score.cohort.startswith("mutual_fund:") else EQUITY_WEIGHTS
    percentage_components = {"six_month_performance", "three_month_performance", "six_month_return", "three_month_return", "twelve_month_return"}
    for component_id, weight in weights.items():
        raw = score.raw_components.get(component_id)
        normalized = score.normalized_components.get(component_id)
        component_metadata: dict[str, Any] = {
            "component_id": component_id,
            "unit": "percent" if component_id in percentage_components else "ratio",
            "cohort": score.cohort,
            "coverage": float(score.coverage),
            "weight": float(weight),
            "normalized": float(normalized) if normalized is not None else None,
            "contribution": float(normalized * weight) if normalized is not None else None,
            "source_date": source_date,
        }
        if score_warning is not None:
            component_metadata["warning"] = score_warning
        rows.append({
            **common,
            "metric": f"momentum_{component_id}",
            "value": float(raw) if raw is not None else None,
            "state": "present" if raw is not None else "missing",
            "raw_value": str(raw) if raw is not None else None,
            "normalized_value": float(normalized) if normalized is not None else None,
            "metadata": component_metadata,
        })
    return rows


# Stable alias for publishers that name the contract after its destination.
momentum_metric_rows = serialize_momentum_provenance


def _decimal_components(values: Mapping[str, Any]) -> dict[str, Decimal]:
    return {
        key: decimal_value
        for key, value in values.items()
        if (decimal_value := as_decimal(value)) is not None
    }


def _lookup(raw: Mapping[str, Decimal], key: str) -> Decimal | None:
    for alias in _ALIASES[key]:
        if alias in raw:
            return raw[alias]
    if key == "inverse_volatility" and "volatility" in raw:
        return -raw["volatility"]
    if key == "inverse_max_drawdown" and "max_drawdown" in raw:
        return -abs(raw["max_drawdown"])
    return None


def _percentile(values: list[Decimal], *, higher_is_better: bool) -> list[Decimal]:
    indexed = list(enumerate(values))
    if higher_is_better:
        indexed.sort(key=lambda item: (-item[1], item[0]))
    else:
        indexed.sort(key=lambda item: (item[1], item[0]))
    count = len(values)
    if count == 1:
        ranks = [Decimal("0.5")]
    elif higher_is_better:
        ranks = [Decimal(1) - Decimal(index) / Decimal(count - 1) for index in range(count)]
    else:
        ranks = [Decimal(index) / Decimal(count - 1) for index in range(count)]
    output = [Decimal(0)] * count
    for position, (original, _) in enumerate(indexed):
        output[original] = ranks[position]
    return output


def _record_parts(record: Mapping[str, Any]) -> tuple[str, str, str | None, Mapping[str, Any], date | None, bool]:
    identifier_raw = record.get("identifier", record.get("instrument_id", record.get("id", record.get("symbol"))))
    if identifier_raw is None:
        raise ValueError("momentum record requires an identifier")
    asset_class = _asset_class(record.get("asset_class", "equity"))
    category = record.get("category")
    if category is not None:
        category = str(category)
    component_values = record.get("components", record)
    if not isinstance(component_values, Mapping):
        raise ValueError("momentum record components must be a mapping")
    effective_raw = record.get("effective_date", record.get("date"))
    effective = as_date(effective_raw) if effective_raw is not None else None
    complete_history = bool(record.get("history_complete", record.get("has_12_month_history", True)))
    return str(identifier_raw), asset_class, category, component_values, effective, complete_history


def momentum_score(
    normalized: NormalizedMomentumInput,
    *,
    effective_date: date | str | None = None,
) -> MomentumScore:
    """Compute a composite from validated, cohort-normalized components."""

    weights = MF_WEIGHTS if normalized.asset_class == "mutual_fund" else EQUITY_WEIGHTS
    raw_components: dict[str, Decimal | None] = {key: normalized.components.get(key) for key in weights}
    coverage = Decimal(len(normalized.components)) / Decimal(len(weights))
    complete = len(normalized.components) == len(weights)
    if normalized.asset_class == "mutual_fund" and (
        normalized.category is None or "twelve_month_return" not in normalized.components
    ):
        complete = False
    total = Decimal(0)
    if complete:
        for key, weight in weights.items():
            total += normalized.components[key] * weight
    return MomentumScore(
        total if complete else None,
        raw_components,
        dict(raw_components),
        normalized.cohort,
        as_date(effective_date) if effective_date is not None else None,
        coverage,
    )


def momentum_scores(records: Iterable[Mapping[str, Any]]) -> dict[str, MomentumScore]:
    """Normalize raw records inside separate typed cohorts and calculate scores."""

    parsed = [_record_parts(record) for record in records]
    groups: dict[
        tuple[str, str | None, date | None],
        list[tuple[int, tuple[str, str, str | None, Mapping[str, Any], date | None, bool], dict[str, Decimal]]],
    ] = {}
    for index, item in enumerate(parsed):
        _, asset_class, category, components, effective, _ = item
        raw = _decimal_components(components)
        weights = MF_WEIGHTS if asset_class == "mutual_fund" else EQUITY_WEIGHTS
        canonical = {
            key: value for key in weights if (value := _lookup(raw, key)) is not None
        }
        group = (asset_class, category if asset_class == "mutual_fund" else None, effective)
        groups.setdefault(group, []).append((index, item, canonical))

    results: dict[str, MomentumScore] = {}
    for (asset_class, category, _), members in groups.items():
        weights = MF_WEIGHTS if asset_class == "mutual_fund" else EQUITY_WEIGHTS
        normalized_by_index: dict[int, dict[str, Decimal | None]] = {
            index: {key: None for key in weights} for index, _, _ in members
        }
        for key in weights:
            available = [(index, canonical[key]) for index, _, canonical in members if key in canonical]
            values = [value for _, value in available]
            normalized_values = _percentile(values, higher_is_better=key != "category_rank") if values else []
            for (index, _), normalized_value in zip(available, normalized_values):
                normalized_by_index[index][key] = normalized_value
        cohort = f"mutual_fund:{category}" if asset_class == "mutual_fund" and category is not None else asset_class
        for index, item, canonical in members:
            identifier, _, _, _, effective, complete_history = item
            normalized_input = NormalizedMomentumInput(
                {key: value for key, value in normalized_by_index[index].items() if value is not None},
                asset_class,
                category,
            )
            composite = momentum_score(normalized_input, effective_date=effective)
            insufficient = not complete_history or (
                asset_class == "mutual_fund" and "twelve_month_return" not in canonical
            )
            results[identifier] = MomentumScore(
                None if insufficient else composite.score,
                {key: canonical.get(key) for key in weights},
                normalized_by_index[index],
                cohort,
                effective,
                composite.coverage,
            )
    return results
