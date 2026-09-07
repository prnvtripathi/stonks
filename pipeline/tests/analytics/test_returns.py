from datetime import date, timedelta
from decimal import Decimal

from market_pipeline.analytics.returns import calculate_returns


def _prices(count: int = 253) -> dict[date, Decimal]:
    start = date(2025, 1, 1)
    return {start + timedelta(days=i): Decimal(i + 1) for i in range(count)}


def test_session_returns_use_observation_lookbacks() -> None:
    result = calculate_returns(_prices(), effective_date=date(2025, 9, 10))

    assert result.one_day == Decimal("1") / Decimal("252")
    assert result.one_week == Decimal("5") / Decimal("248")
    assert result.one_month == Decimal("21") / Decimal("232")
    assert result.twelve_month == Decimal("252")
    assert result["1w"] == result.one_week
    assert result.proximity_to_52_week_high == Decimal("1")


def test_missing_effective_endpoint_is_not_forward_filled() -> None:
    values = {date(2025, 1, 1): Decimal("10"), date(2025, 1, 3): Decimal("12")}

    result = calculate_returns(values, effective_date=date(2025, 1, 2))

    assert result.effective_date == date(2025, 1, 1)
    assert result.one_day is None


def test_weekly_average_volume_keeps_zero_volume_observations() -> None:
    start = date(2025, 1, 1)
    observations = {
        start + timedelta(days=i): {"price": Decimal("10"), "volume": Decimal(value)}
        for i, value in enumerate((0, 10, 20, 30, 40))
    }

    result = calculate_returns(observations)

    assert result.weekly_average_volume == Decimal("20")
