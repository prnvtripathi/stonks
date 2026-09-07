from datetime import date
from decimal import Decimal

from market_pipeline.normalization.corporate_actions import (
    CorporateAction,
    adjustment_factors,
)


def test_two_for_one_split() -> None:
    action = CorporateAction.split(date(2026, 6, 1), numerator=2, denominator=1)

    assert adjustment_factors([action])[date(2026, 5, 29)] == Decimal("0.5")


def test_consolidation_and_bonus_compose_before_action() -> None:
    actions = [
        CorporateAction.split(date(2026, 6, 1), numerator=2, denominator=1),
        CorporateAction.consolidation(date(2026, 7, 1), numerator=1, denominator=2),
        CorporateAction.bonus(date(2026, 8, 1), numerator=1, denominator=1),
    ]

    factors = adjustment_factors(actions)
    assert factors[date(2026, 5, 29)] == Decimal("0.5")
    assert factors[date(2026, 6, 30)] == Decimal("1")
    assert factors[date(2026, 7, 31)] == Decimal("0.5")


def test_dividend_is_visible_but_does_not_change_price_factor() -> None:
    action = CorporateAction.dividend(date(2026, 6, 1), amount="2.50")

    assert action.amount == Decimal("2.50")
    assert adjustment_factors([action])[date(2026, 5, 29)] == Decimal("1")

