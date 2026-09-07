"""Shared input coercion for analytics modules.

Analytics deliberately accepts plain date/value mappings as well as normalized
observation-like records.  This keeps the calculation layer independent from a
storage engine while retaining the no-forward-fill rule.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, TypeVar

from market_pipeline.domain.models import Observation

T = TypeVar("T")


@dataclass(frozen=True)
class Point:
    effective_date: date
    value: Decimal
    volume: Decimal | None = None


def as_date(value: date | datetime | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def as_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _mapping_point(item: Mapping[str, Any], value_key: str) -> Point | None:
    raw_date = item.get("effective_date", item.get("date"))
    if raw_date is None:
        return None
    try:
        point_date = as_date(raw_date)
    except (TypeError, ValueError):
        return None
    raw_value = item.get(value_key)
    if raw_value is None:
        for alias in ("adjusted_close", "close", "price", "nav", "value"):
            if alias != value_key and alias in item:
                raw_value = item[alias]
                break
    if isinstance(raw_value, Mapping):
        raw_value = raw_value.get("value")
    if hasattr(raw_value, "value") and not isinstance(raw_value, (str, bytes)):
        raw_value = getattr(raw_value, "value")
    decimal_value = as_decimal(raw_value)
    if decimal_value is None:
        return None
    return Point(point_date, decimal_value, as_decimal(item.get("volume")))


def coerce_points(values: Any, *, value_key: str = "price") -> tuple[Point, ...]:
    """Coerce common observation shapes into sorted, unique dated points."""

    points: list[Point] = []
    if isinstance(values, Mapping):
        for raw_date, raw_value in values.items():
            if isinstance(raw_value, Mapping):
                item = dict(raw_value)
                item.setdefault("effective_date", raw_date)
                point = _mapping_point(item, value_key)
            else:
                decimal_value = as_decimal(raw_value)
                point = (
                    Point(as_date(raw_date), decimal_value)
                    if decimal_value is not None
                    else None
                )
            if point is not None:
                points.append(point)
    else:
        try:
            iterator: Iterable[Any] = values
        except TypeError:
            iterator = ()
        for item in iterator:
            if isinstance(item, Observation):
                raw_value = item.value.value
                decimal_value = as_decimal(raw_value)
                if decimal_value is not None:
                    points.append(Point(item.effective_date, decimal_value))
            elif isinstance(item, Mapping):
                point = _mapping_point(item, value_key)
                if point is not None:
                    points.append(point)
            elif isinstance(item, (tuple, list)) and len(item) >= 2:
                try:
                    point_date = as_date(item[0])
                except (TypeError, ValueError):
                    continue
                decimal_value = as_decimal(item[1])
                if decimal_value is not None:
                    volume = as_decimal(item[2]) if len(item) > 2 else None
                    points.append(Point(point_date, decimal_value, volume))
    # Duplicate observations are not silently averaged.  Keep the final input
    # record for a date, mirroring a corrected EOD observation.
    by_date = {point.effective_date: point for point in points}
    return tuple(sorted(by_date.values(), key=lambda point: point.effective_date))


def endpoint_index(points: tuple[Point, ...], effective_date: date | datetime | str | None) -> int | None:
    if not points:
        return None
    if effective_date is None:
        return len(points) - 1
    endpoint = as_date(effective_date)
    indices = [index for index, point in enumerate(points) if point.effective_date <= endpoint]
    return indices[-1] if indices else None


def ratio(current: Decimal, prior: Decimal) -> Decimal | None:
    if prior == 0:
        return None
    # Subtract before dividing to avoid losing low-order digits when current
    # and prior are close (and to make Decimal results agree with direct
    # percentage-change calculations).
    return (current - prior) / prior
