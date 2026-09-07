"""Benchmark-relative and cohort-relative strength calculations."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from market_pipeline.analytics._utils import coerce_points, endpoint_index, ratio

FORMULA_VERSION = "rs-v1"


def benchmark_rs(asset: Decimal, benchmark: Decimal) -> Decimal:
    """Return benchmark-relative performance as a fraction.

    Both inputs and the result use the Task 8 fractional-percent convention
    (``0.0909`` means 9.09%), matching returns, volatility, and drawdown. The
    UI renders every ``unit: "percent"`` metric by multiplying by 100, so
    returning percentage points here would display as "909%".
    """

    if Decimal(1) + benchmark == 0:
        raise ValueError("benchmark return cannot be -100 percent")
    value = (Decimal(1) + asset) / (Decimal(1) + benchmark) - Decimal(1)
    return value.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_EVEN)


def weighted_rs_score(values: Sequence[Decimal]) -> Decimal:
    """Weight four quarter returns, newest first, at 40/20/20/20 percent."""

    if len(values) != 4:
        raise ValueError("weighted RS requires four non-overlapping quarter returns")
    weights = (Decimal("0.40"), Decimal("0.20"), Decimal("0.20"), Decimal("0.20"))
    return sum((value * weight for value, weight in zip(values, weights)), Decimal(0))


def _score(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)) if value is not None else None
    except (ArithmeticError, ValueError):
        return None


def _weighted_from_prices(value: Any, effective_date: Any = None) -> Decimal | None:
    if isinstance(value, Mapping) and "quarter_returns" in value:
        quarter_values = [_score(item) for item in value["quarter_returns"]]
        if len(quarter_values) == 4 and all(item is not None for item in quarter_values):
            return weighted_rs_score([item for item in quarter_values if item is not None])
    points = coerce_points(value)
    if effective_date is not None:
        end = endpoint_index(points, effective_date)
        points = points[: end + 1] if end is not None else ()
    if len(points) < 253:
        return None
    end = len(points) - 1
    quarter_returns: list[Decimal] = []
    for quarter in range(4):
        newer = end - quarter * 63
        older = newer - 63
        if older < 0:
            return None
        performance = ratio(points[newer].value, points[older].value)
        if performance is None:
            return None
        quarter_returns.append(performance)
    return weighted_rs_score(quarter_returns)


def equity_rs_rating(
    values: Mapping[str, Any] | Iterable[tuple[str, Any]],
    *,
    asset_classes: Mapping[str, str] | None = None,
    effective_date: Any = None,
) -> dict[str, int]:
    """Rank eligible ordinary equities on a deterministic 1--99 scale.

    Values may be weighted scores or dated price sequences. Mapping order is
    retained for exact ties, so repeated runs produce stable ratings.
    """

    scores: list[tuple[str, Decimal]] = []
    items = values.items() if isinstance(values, Mapping) else values
    for identifier, raw in items:
        if asset_classes is not None and asset_classes.get(identifier, "equity").lower() != "equity":
            continue
        if isinstance(raw, Mapping) and str(raw.get("asset_class", "equity")).lower() != "equity":
            continue
        weighted = _score(raw) if not isinstance(raw, (Mapping, list, tuple)) else None
        if weighted is None:
            if isinstance(raw, Mapping):
                weighted = _score(raw.get("weighted_score"))
                if weighted is None:
                    weighted = _weighted_from_prices(raw.get("prices", ()), effective_date)
            else:
                weighted = _weighted_from_prices(raw, effective_date)
        if weighted is not None:
            scores.append((identifier, weighted))
    ordered = sorted(enumerate(scores), key=lambda item: (item[1][1], item[0]))
    ratings: dict[str, int] = {}
    count = len(ordered)
    for position, (_, (identifier, _)) in enumerate(ordered):
        if count == 1:
            rating = 50
        else:
            exact = Decimal(1) + Decimal(position * 98) / Decimal(count - 1)
            rating = int(exact.quantize(Decimal(1), rounding=ROUND_HALF_EVEN))
        ratings[identifier] = max(1, min(99, rating))
    return ratings
