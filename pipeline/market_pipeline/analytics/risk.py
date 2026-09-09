"""Volatility, drawdown, and risk-window metrics."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from market_pipeline.analytics._utils import Point, coerce_points, endpoint_index, ratio

FORMULA_VERSION = "risk-v2-252-session"
RISK_RETURN_SESSIONS = 252


@dataclass(frozen=True)
class RiskMetrics:
    annualized_volatility: Decimal | None
    max_drawdown: Decimal | None
    moving_average_50: Decimal | None
    moving_average_200: Decimal | None
    fifty_two_week_high: Decimal | None
    proximity_to_52_week_high: Decimal | None
    weekly_average_volume: Decimal | None
    effective_date: date | None
    coverage: Decimal
    formula_version: str = FORMULA_VERSION

    def __getitem__(self, key: str) -> Decimal | None:
        aliases = {
            "volatility": "annualized_volatility", "drawdown": "max_drawdown",
            "52_week_high": "fifty_two_week_high", "high_proximity": "proximity_to_52_week_high",
        }
        field = aliases.get(key, key)
        if not hasattr(self, field):
            raise KeyError(key)
        value = getattr(self, field)
        if not isinstance(value, (Decimal, type(None))):
            raise KeyError(key)
        return value

    @property
    def volatility(self) -> Decimal | None:
        return self.annualized_volatility

    @property
    def drawdown(self) -> Decimal | None:
        return self.max_drawdown


def _window_average(points: tuple[Point, ...], end: int | None, size: int, *, volume: bool = False) -> Decimal | None:
    if end is None or end + 1 < size:
        return None
    window = points[end - size + 1 : end + 1]
    values = [point.volume if volume else point.value for point in window]
    if any(value is None for value in values):
        return None
    present = [value for value in values if value is not None]
    return sum(present, Decimal(0)) / Decimal(size)


def calculate_risk(
    observations: Any,
    effective_date: date | datetime | str | None = None,
) -> RiskMetrics:
    points = coerce_points(observations)
    end = endpoint_index(points, effective_date)
    window = points[: end + 1] if end is not None else ()
    risk_window = window[-(RISK_RETURN_SESSIONS + 1):]
    daily_returns = [ratio(current.value, prior.value) for prior, current in zip(risk_window, risk_window[1:])]
    valid_returns = [value for value in daily_returns if value is not None]
    coverage = Decimal(len(valid_returns)) / Decimal(RISK_RETURN_SESSIONS)
    has_full_risk_window = len(risk_window) == RISK_RETURN_SESSIONS + 1 and coverage == Decimal(1)
    volatility: Decimal | None = None
    if has_full_risk_window:
        mean = sum(valid_returns, Decimal(0)) / Decimal(len(valid_returns))
        variance = sum((value - mean) ** 2 for value in valid_returns) / Decimal(len(valid_returns) - 1)
        volatility = variance.sqrt() * Decimal(252).sqrt()
    drawdown: Decimal | None = None
    if has_full_risk_window:
        peak = risk_window[0].value
        drawdowns: list[Decimal] = []
        for point in risk_window:
            peak = max(peak, point.value)
            if peak != 0:
                drawdowns.append(point.value / peak - Decimal(1))
        drawdown = min(drawdowns) if drawdowns else None
    high = (
        max(point.value for point in window[-252:])
        if len(window) >= 252
        else None
    )
    current = window[-1].value if window else None
    return RiskMetrics(
        annualized_volatility=volatility,
        max_drawdown=drawdown,
        moving_average_50=_window_average(window, len(window) - 1 if window else None, 50),
        moving_average_200=_window_average(window, len(window) - 1 if window else None, 200),
        fifty_two_week_high=high,
        proximity_to_52_week_high=(
            current / high if current is not None and high is not None and high != 0 else None
        ),
        weekly_average_volume=_window_average(window, len(window) - 1 if window else None, 5, volume=True),
        effective_date=window[-1].effective_date if window else None,
        coverage=coverage,
    )
