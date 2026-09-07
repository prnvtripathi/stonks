from decimal import Decimal
from typing import Any

import pytest
from market_pipeline.analytics.momentum import (
    NormalizedMomentumInput,
    momentum_score,
    momentum_scores,
)


def test_equity_momentum_uses_all_approved_weights() -> None:
    values = {
        "weighted_12m_rs_percentile": Decimal("1"),
        "six_month_performance": Decimal("1"),
        "three_month_performance": Decimal("1"),
        "trend_strength": Decimal("1"),
        "proximity_to_52_week_high": Decimal("1"),
        "volume_confirmation": Decimal("1"),
    }

    normalized = NormalizedMomentumInput.from_mapping(values, asset_class="equity")
    result = momentum_score(normalized, effective_date="2026-09-07")

    assert result.score == Decimal("1")
    assert result.coverage == Decimal("1")
    assert result.formula_version
    assert result.normalized_components == values


def _equity(identifier: str, value: str, asset_class: str = "equity") -> dict[str, Any]:
    return {
        "identifier": identifier,
        "asset_class": asset_class,
        "components": {
            "weighted_12m_rs_percentile": Decimal(value),
            "six_month_performance": Decimal(value),
            "three_month_performance": Decimal(value),
            "trend_strength": Decimal(value),
            "proximity_to_52_week_high": Decimal(value),
            "volume_confirmation": Decimal(value),
        },
    }


def test_batch_normalizes_raw_values_and_does_not_treat_one_percent_as_one() -> None:
    results = momentum_scores([_equity("one", "0.01"), _equity("two", "0.02")])

    assert results["one"].raw_components["three_month_performance"] == Decimal("0.01")
    assert results["one"].normalized_components["three_month_performance"] == Decimal("0")
    assert results["two"].normalized_components["three_month_performance"] == Decimal("1")


def test_batch_accepts_a_two_hundred_percent_raw_return() -> None:
    results = momentum_scores([_equity("base", "0.01"), _equity("large", "2")])

    assert results["large"].raw_components["three_month_performance"] == Decimal("2")
    assert results["large"].normalized_components["three_month_performance"] == Decimal("1")


def test_batch_ties_are_stable_in_input_order() -> None:
    results = momentum_scores([_equity("first", "0.01"), _equity("second", "0.01")])

    assert results["first"].normalized_components["three_month_performance"] == Decimal("1")
    assert results["second"].normalized_components["three_month_performance"] == Decimal("0")


def test_batch_keeps_equity_and_etf_cohorts_separate() -> None:
    results = momentum_scores([_equity("eq", "0.01"), _equity("etf", "0.02", "etf")])

    assert results["eq"].cohort == "equity"
    assert results["etf"].cohort == "etf"
    assert results["eq"].normalized_components["three_month_performance"] == Decimal("0.5")
    assert results["etf"].normalized_components["three_month_performance"] == Decimal("0.5")


def test_batch_uses_explicit_mf_categories_and_ignores_spoofed_cohort() -> None:
    def record(identifier: str, category: str, value: str) -> dict[str, object]:
        return {
            "identifier": identifier,
            "asset_class": "mutual_fund",
            "category": category,
            "cohort": "equity",  # untrusted and ignored
            "components": {
                "three_month_return": Decimal(value),
                "six_month_return": Decimal(value),
                "twelve_month_return": Decimal(value),
                "category_rank": Decimal("1"),
                "volatility": Decimal("0.1"),
                "max_drawdown": Decimal("-0.1"),
            },
        }

    results = momentum_scores([record("a", "large", "0.01"), record("b", "small", "0.02")])

    assert results["a"].cohort == "mutual_fund:large"
    assert results["b"].cohort == "mutual_fund:small"


def test_batch_inverts_volatility_and_drawdown() -> None:
    low_risk = {
        **_equity("low", "0.01"),
        "asset_class": "mutual_fund",
        "category": "balanced",
        "components": {
            "three_month_return": Decimal("0.1"),
            "six_month_return": Decimal("0.1"),
            "twelve_month_return": Decimal("0.1"),
            "category_rank": Decimal("1"),
            "volatility": Decimal("0.1"),
            "max_drawdown": Decimal("-0.1"),
        },
    }
    high_risk = {**low_risk, "identifier": "high", "components": {**low_risk["components"], "volatility": Decimal("0.2"), "max_drawdown": Decimal("-0.2")}}

    results = momentum_scores([low_risk, high_risk])

    assert results["low"].normalized_components["inverse_volatility"] == Decimal("1")
    assert results["high"].normalized_components["inverse_volatility"] == Decimal("0")
    assert results["low"].normalized_components["inverse_max_drawdown"] == Decimal("1")
    assert results["high"].normalized_components["inverse_max_drawdown"] == Decimal("0")


def test_batch_rejects_out_of_domain_normalized_values_and_missing_history() -> None:
    with pytest.raises(ValueError, match="0..1"):
        NormalizedMomentumInput.from_mapping({"three_month_performance": Decimal("2")}, asset_class="equity")

    with pytest.raises(ValueError, match="0..1"):
        NormalizedMomentumInput({"three_month_performance": Decimal("2")}, "equity")

    incomplete = _equity("new", "0.01")
    incomplete_components = incomplete["components"]
    assert isinstance(incomplete_components, dict)
    del incomplete_components["weighted_12m_rs_percentile"]
    result = momentum_scores([incomplete])["new"]
    assert result.score is None


def test_missing_equity_component_is_insufficient() -> None:
    normalized = NormalizedMomentumInput.from_mapping(
        {"three_month_performance": Decimal("1")}, asset_class="etf"
    )
    result = momentum_score(normalized)

    assert result.score is None
    assert result.coverage == Decimal("0.1666666666666666666666666667")


def test_mutual_fund_requires_twelve_month_history_and_category() -> None:
    normalized = NormalizedMomentumInput.from_mapping(
        {
            "three_month_return": Decimal("1"),
            "six_month_return": Decimal("1"),
            "twelve_month_return": Decimal("1"),
            "category_rank": Decimal("1"),
            "inverse_volatility": Decimal("1"),
            "inverse_max_drawdown": Decimal("1"),
        },
        asset_class="mutual_fund",
        category="Equity",
    )
    result = momentum_score(normalized)

    assert result.score == Decimal("1")
    assert result.cohort == "mutual_fund:Equity"
