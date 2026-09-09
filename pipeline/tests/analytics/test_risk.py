from datetime import date, timedelta
from decimal import Decimal

from market_pipeline.analytics.risk import calculate_risk


def test_risk_does_not_label_partial_history_as_one_year() -> None:
    prices = {
        date(2025, 1, 1) + timedelta(days=i): Decimal(value)
        for i, value in enumerate((100, 110, 90, 95))
    }

    result = calculate_risk(prices)

    assert result.annualized_volatility is None
    assert result.max_drawdown is None
    assert result.coverage == Decimal(3) / Decimal(252)


def test_risk_uses_only_the_trailing_252_return_sessions_after_an_old_crash() -> None:
    prices = {
        date(2025, 1, 1) + timedelta(days=i): Decimal(200 if i == 0 else 100)
        for i in range(300)
    }

    result = calculate_risk(prices)

    assert result.annualized_volatility == Decimal(0)
    assert result.max_drawdown == Decimal(0)
    assert result.coverage == Decimal(1)


def test_risk_accepts_exactly_253_observations_for_a_one_year_window() -> None:
    result = calculate_risk({
        date(2025, 1, 1) + timedelta(days=i): Decimal(100)
        for i in range(253)
    })

    assert result.annualized_volatility == Decimal(0)
    assert result.max_drawdown == Decimal(0)
    assert result.coverage == Decimal(1)


def test_risk_effective_date_truncation_does_not_use_later_sessions_to_complete_the_window() -> None:
    prices = {
        date(2025, 1, 1) + timedelta(days=i): Decimal(100)
        for i in range(253)
    }

    result = calculate_risk(prices, date(2025, 1, 1) + timedelta(days=251))

    assert result.annualized_volatility is None
    assert result.max_drawdown is None
    assert result.coverage == Decimal(251) / Decimal(252)


def test_risk_metrics_require_full_moving_average_windows() -> None:
    result = calculate_risk({date(2025, 1, 1): Decimal("10")})

    assert result.moving_average_50 is None
    assert result.moving_average_200 is None
    assert result.proximity_to_52_week_high is None
