"""Explainable cohort-relative momentum composites."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from market_pipeline.analytics._utils import as_date, as_decimal

FORMULA_VERSION = "momentum-v1"
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


def _decimal_components(values: Mapping[str, Any]) -> dict[str, Decimal]:
    result: dict[str, Decimal] = {}
    for key, value in values.items():
        decimal_value = as_decimal(value)
        if decimal_value is not None:
            result[key] = decimal_value
    return result


def _normalise(value: Decimal) -> Decimal:
    # Direct callers commonly provide percentages (0..100), while pipeline
    # callers may already provide unit values. Support both representations.
    result = value / Decimal(100) if value > 1 or value < -1 else value
    return max(Decimal(0), min(Decimal(1), result))


def _lookup(raw: Mapping[str, Decimal], key: str) -> Decimal | None:
    aliases = {
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
    for alias in aliases[key]:
        if alias in raw:
            return raw[alias]
    if key == "inverse_volatility" and "volatility" in raw:
        return Decimal(1) - _normalise(raw["volatility"])
    if key == "inverse_max_drawdown" and "max_drawdown" in raw:
        return Decimal(1) - _normalise(abs(raw["max_drawdown"]))
    return None


def momentum_score(
    components: Mapping[str, Any],
    *,
    asset_class: str = "equity",
    category: str | None = None,
    effective_date: date | str | None = None,
    cohort: str | None = None,
) -> MomentumScore:
    """Calculate a fully covered equity/ETF or mutual-fund momentum score."""

    normalized_asset_class = asset_class.value if hasattr(asset_class, "value") else str(asset_class)
    normalized_asset_class = normalized_asset_class.lower()
    is_mf = normalized_asset_class in {"mutual_fund", "mutual-fund", "mf"}
    weights = MF_WEIGHTS if is_mf else EQUITY_WEIGHTS
    raw = _decimal_components(components)
    selected: dict[str, Decimal] = {}
    for key in weights:
        value = _lookup(raw, key)
        if value is not None:
            selected[key] = value
    emitted_raw: dict[str, Decimal | None] = {key: selected.get(key) for key in weights}
    normalized: dict[str, Decimal | None] = {
        key: (_normalise(value) if value is not None else None)
        for key, value in emitted_raw.items()
    }
    required = len(weights)
    coverage = Decimal(len(selected)) / Decimal(required)
    complete = len(selected) == required
    if is_mf and ("twelve_month_return" not in selected or category is None):
        complete = False
    score: Decimal | None = None
    if complete:
        total = Decimal(0)
        for key, weight in weights.items():
            value = normalized[key]
            if value is not None:
                total += value * weight
        score = total
    result_cohort = cohort or (f"mutual_fund:{category}" if is_mf else normalized_asset_class)
    return MomentumScore(
        score=score,
        raw_components=emitted_raw,
        normalized_components=normalized,
        cohort=result_cohort,
        effective_date=as_date(effective_date) if effective_date is not None else None,
        coverage=coverage,
    )
