from datetime import date, timedelta
from decimal import Decimal
from typing import cast

from market_pipeline.analytics.risk import calculate_risk


def test_risk_annualizes_daily_volatility_and_drawdown() -> None:
    prices = {
        date(2025, 1, 1) + timedelta(days=i): Decimal(value)
        for i, value in enumerate((100, 110, 90, 95))
    }

    result = calculate_risk(prices)

    daily = [Decimal("0.1"), Decimal("-2") / Decimal("11"), Decimal("1") / Decimal("18")]
    mean = sum(daily, Decimal(0)) / len(daily)
    variance = cast(Decimal, sum((value - mean) ** 2 for value in daily) / (len(daily) - 1))
    expected = variance.sqrt() * Decimal("252").sqrt()
    assert result.annualized_volatility == expected
    assert result.max_drawdown == Decimal("-0.1818181818181818181818181818")


def test_risk_metrics_require_full_moving_average_windows() -> None:
    result = calculate_risk({date(2025, 1, 1): Decimal("10")})

    assert result.moving_average_50 is None
    assert result.moving_average_200 is None
    assert result.proximity_to_52_week_high is None
