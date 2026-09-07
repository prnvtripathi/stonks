"""Corporate actions and price-only adjustment factors."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date as Date
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator


def _decimal(value: Any) -> Decimal:
    try:
        return value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"corporate-action value is not a decimal: {value!r}") from exc


class CorporateAction(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    action_date: Date
    action_type: str
    numerator: Decimal = Decimal("1")
    denominator: Decimal = Decimal("1")
    amount: Decimal | None = None
    instrument_id: UUID | None = None
    source_artifact_id: UUID | None = None

    @model_validator(mode="before")
    @classmethod
    def coerce_input(cls, data: Any) -> Any:
        if isinstance(data, CorporateAction):
            return data
        result = dict(data)
        if "action_date" not in result and "date" in result:
            result["action_date"] = result.pop("date")
        result["action_type"] = str(result.get("action_type", result.get("type", ""))).lower()
        if "numerator" in result:
            result["numerator"] = _decimal(result["numerator"])
        if "denominator" in result:
            result["denominator"] = _decimal(result["denominator"])
        if "amount" in result and result["amount"] is not None:
            result["amount"] = _decimal(result["amount"])
        return result

    @property
    def date(self) -> Date:
        return self.action_date

    @classmethod
    def split(cls, action_date: Date, *, numerator: Any, denominator: Any) -> "CorporateAction":
        return cls(action_date=action_date, action_type="split", numerator=numerator, denominator=denominator)

    @classmethod
    def consolidation(cls, action_date: Date, *, numerator: Any, denominator: Any) -> "CorporateAction":
        return cls(action_date=action_date, action_type="consolidation", numerator=numerator, denominator=denominator)

    @classmethod
    def bonus(cls, action_date: Date, *, numerator: Any, denominator: Any) -> "CorporateAction":
        return cls(action_date=action_date, action_type="bonus", numerator=numerator, denominator=denominator)

    @classmethod
    def dividend(cls, action_date: Date, *, amount: Any) -> "CorporateAction":
        return cls(action_date=action_date, action_type="dividend", amount=amount)

    def price_factor(self) -> Decimal:
        if self.action_type == "dividend":
            return Decimal("1")
        if self.numerator <= 0 or self.denominator <= 0:
            raise ValueError("corporate-action ratios must be positive")
        if self.action_type == "bonus":
            factor = self.denominator / (self.denominator + self.numerator)
        elif self.action_type in {"split", "consolidation"}:
            factor = self.denominator / self.numerator
        else:
            raise ValueError(f"unsupported corporate-action type: {self.action_type}")
        return factor.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


def _previous_business_day(value: Date) -> Date:
    result = value - timedelta(days=1)
    while result.weekday() >= 5:
        result -= timedelta(days=1)
    return result


def adjustment_factors(
    actions: Iterable[CorporateAction | Mapping[str, Any]],
    dates: Iterable[Date] | None = None,
) -> dict[Date, Decimal]:
    """Return price-only factors for dates preceding each corporate action.

    Factors are cumulative future adjustments: a close dated before three
    actions includes all three event factors; the event date's close is already
    on the post-action share basis and is not adjusted.
    """

    normalized = sorted(
        (CorporateAction.model_validate(action) for action in actions),
        key=lambda action: action.action_date,
    )
    if dates is None:
        dates = (_previous_business_day(action.action_date) for action in normalized)
    result: dict[Date, Decimal] = {}
    for observed in dates:
        factor = Decimal("1")
        for action in normalized:
            if action.action_date > observed:
                factor *= action.price_factor()
        result[observed] = factor.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    return result


__all__ = ["CorporateAction", "adjustment_factors"]
