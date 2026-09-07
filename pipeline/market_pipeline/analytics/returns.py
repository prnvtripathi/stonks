"""Adjusted-price returns and moving-window metrics."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from market_pipeline.analytics._utils import Point, coerce_points, endpoint_index, ratio

FORMULA_VERSION = "returns-v1"
LOOKBACKS = (("one_day", 1), ("one_week", 5), ("one_month", 21), ("three_month", 63), ("six_month", 126), ("twelve_month", 252))


@dataclass(frozen=True)
class ReturnMetrics:
    one_day: Decimal | None
    one_week: Decimal | None
    one_month: Decimal | None
    three_month: Decimal | None
    six_month: Decimal | None
    twelve_month: Decimal | None
    weekly_average_volume: Decimal | None
    moving_average_50: Decimal | None
    moving_average_200: Decimal | None
    fifty_two_week_high: Decimal | None
    proximity_to_52_week_high: Decimal | None
    effective_date: date | None
    coverage: Decimal
    formula_version: str = FORMULA_VERSION

    def __getitem__(self, key: str) -> Decimal | None:
        aliases = {
            "1d": "one_day", "1w": "one_week", "1m": "one_month",
            "3m": "three_month", "6m": "six_month", "12m": "twelve_month",
        }
        field = aliases.get(key, key)
        if not hasattr(self, field):
            raise KeyError(key)
        value = getattr(self, field)
        if not isinstance(value, (Decimal, type(None))):
            raise KeyError(key)
        return value

    @property
    def return_1d(self) -> Decimal | None:
        return self.one_day

    @property
    def return_1w(self) -> Decimal | None:
        return self.one_week

    @property
    def return_1m(self) -> Decimal | None:
        return self.one_month

    @property
    def return_3m(self) -> Decimal | None:
        return self.three_month

    @property
    def return_6m(self) -> Decimal | None:
        return self.six_month

    @property
    def return_12m(self) -> Decimal | None:
        return self.twelve_month

    @property
    def average_volume_1w(self) -> Decimal | None:
        return self.weekly_average_volume

    @property
    def ma_50(self) -> Decimal | None:
        return self.moving_average_50

    @property
    def ma_200(self) -> Decimal | None:
        return self.moving_average_200

    @property
    def high_52w(self) -> Decimal | None:
        return self.fifty_two_week_high


def _window_average(points: tuple[Point, ...], end: int | None, size: int, *, volume: bool = False) -> Decimal | None:
    if end is None or end + 1 < size:
        return None
    window = points[end - size + 1 : end + 1]
    if volume and any(point.volume is None for point in window):
        return None
    values = [point.volume if volume else point.value for point in window]
    if any(value is None for value in values):
        return None
    present = [value for value in values if value is not None]
    return sum(present, Decimal(0)) / Decimal(size)


def calculate_returns(
    observations: Any,
    effective_date: date | datetime | str | None = None,
    *,
    volume_observations: Any | None = None,
) -> ReturnMetrics:
    """Calculate session-based adjusted-price returns without filling dates."""

    points = coerce_points(observations)
    if volume_observations is not None:
        volumes = {
            point.effective_date: point.volume if point.volume is not None else point.value
            for point in coerce_points(volume_observations, value_key="volume")
        }
        points = tuple(
            Point(point.effective_date, point.value, volumes.get(point.effective_date, point.volume))
            for point in points
        )
    end = endpoint_index(points, effective_date)
    current = points[end].value if end is not None else None
    calculated: dict[str, Decimal | None] = {}
    for field, lookback in LOOKBACKS:
        prior = points[end - lookback].value if end is not None and end >= lookback else None
        calculated[field] = ratio(current, prior) if current is not None and prior is not None else None
    effective = points[end].effective_date if end is not None else None
    available = sum(value is not None for value in calculated.values())
    return ReturnMetrics(
        one_day=calculated["one_day"],
        one_week=calculated["one_week"],
        one_month=calculated["one_month"],
        three_month=calculated["three_month"],
        six_month=calculated["six_month"],
        twelve_month=calculated["twelve_month"],
        weekly_average_volume=_window_average(points, end, 5, volume=True),
        moving_average_50=_window_average(points, end, 50),
        moving_average_200=_window_average(points, end, 200),
        fifty_two_week_high=(
            max(point.value for point in points[max(0, end - 251) : end + 1])
            if end is not None and end + 1 >= 252
            else None
        ),
        proximity_to_52_week_high=(
            current / max(point.value for point in points[end - 251 : end + 1])
            if end is not None and current is not None and end + 1 >= 252 and max(point.value for point in points[end - 251 : end + 1]) != 0
            else None
        ),
        effective_date=effective,
        coverage=Decimal(available) / Decimal(len(LOOKBACKS)),
    )
