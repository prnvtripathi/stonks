from decimal import Decimal

from market_pipeline.analytics.momentum import momentum_score


def test_equity_momentum_uses_all_approved_weights() -> None:
    values = {
        "weighted_12m_rs_percentile": Decimal("1"),
        "six_month_performance": Decimal("1"),
        "three_month_performance": Decimal("1"),
        "trend_strength": Decimal("1"),
        "proximity_to_52_week_high": Decimal("1"),
        "volume_confirmation": Decimal("1"),
    }

    result = momentum_score(values, asset_class="equity", effective_date="2026-09-07")

    assert result.score == Decimal("1")
    assert result.coverage == Decimal("1")
    assert result.formula_version
    assert result.normalized_components == values


def test_missing_equity_component_is_insufficient() -> None:
    result = momentum_score({"three_month_performance": Decimal("1")}, asset_class="etf")

    assert result.score is None
    assert result.coverage == Decimal("0.1666666666666666666666666667")


def test_mutual_fund_requires_twelve_month_history_and_category() -> None:
    result = momentum_score(
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

    assert result.score == Decimal("1")
    assert result.cohort == "mutual_fund:Equity"

