from datetime import date, timedelta
from decimal import Decimal

from market_pipeline.analytics.rs import benchmark_rs, equity_rs_rating, weighted_rs_score


def test_benchmark_rs() -> None:
    assert benchmark_rs(Decimal("0.20"), Decimal("0.10")) == Decimal("9.090909")


def test_recent_quarter_has_double_weight() -> None:
    values = [Decimal(".10"), Decimal(".05"), Decimal("0"), Decimal("-.05")]
    assert weighted_rs_score(values) == Decimal(".04")


def test_equity_ratings_are_one_to_ninety_nine_with_stable_ties() -> None:
    ratings = equity_rs_rating({"A": Decimal(".1"), "B": Decimal(".1"), "C": Decimal(".2")})

    assert ratings == {"A": 1, "B": 50, "C": 99}


def test_equity_rating_uses_last_price_on_or_before_effective_date() -> None:
    start = date(2025, 1, 1)
    prices = {start + timedelta(days=i): Decimal(i + 1) for i in range(253)}
    prices[start + timedelta(days=253)] = Decimal("999")

    ratings = equity_rs_rating({"A": {"prices": prices}}, effective_date=start + timedelta(days=252))

    assert ratings == {"A": 50}


def test_equity_rating_accepts_stable_record_sequences() -> None:
    ratings = equity_rs_rating([("A", Decimal(".1")), ("B", Decimal(".2"))])

    assert ratings == {"A": 1, "B": 99}
