"""Stable identity and explicit metadata normalization for AMFI schemes."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, model_validator

from market_pipeline.domain.models import INSTRUMENT_NAMESPACE, AssetClass
from market_pipeline.sources.amfi_nav import AmfiNavRow


NORMALIZATION_VERSION = "amfi-normalization-v1"


def _scheme_id(code: str) -> UUID:
    return uuid5(INSTRUMENT_NAMESPACE, f"amfi|{code}|{AssetClass.MUTUAL_FUND.value}")


def _record_id(code: str, nav_date: date, nav: Decimal, name: str, amc: str | None) -> UUID:
    identity = f"{code}|{nav_date.isoformat()}|{nav}|{name}|{amc or ''}"
    return uuid5(NAMESPACE_URL, "stonks/amfi-record/" + identity)


def _classify_plan(name: str) -> str | None:
    if re.search(r"\bdirect\s+plan\b", name, re.IGNORECASE):
        return "Direct"
    if re.search(r"\bregular\s+plan\b", name, re.IGNORECASE):
        return "Regular"
    return None


def _classify_option(name: str) -> str | None:
    if re.search(r"\bgrowth\b", name, re.IGNORECASE):
        return "Growth"
    if re.search(r"\bidcw\b", name, re.IGNORECASE):
        return "IDCW"
    if re.search(r"\bdividend\b", name, re.IGNORECASE):
        return "Dividend"
    if re.search(r"\bbonus\b", name, re.IGNORECASE):
        return "Bonus"
    return None


def _classify_distribution(name: str) -> str | None:
    if re.search(r"\bpayout\b", name, re.IGNORECASE):
        return "Payout"
    if re.search(r"\breinvestment\b", name, re.IGNORECASE):
        return "Reinvestment"
    return None


class AmfiScheme(BaseModel):
    """A scheme snapshot whose stable identity is only its AMFI code."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scheme_id: UUID
    record_id: UUID
    scheme_code: str
    provider: str = "amfi"
    provider_identifier: str
    asset_class: AssetClass = AssetClass.MUTUAL_FUND
    scheme_name: str
    amc: str | None = None
    category: str | None = None
    plan: str | None = None
    option: str | None = None
    distribution: str | None = None
    # AMFI labels this column "ISIN Div Payout/ ISIN Growth". Keep its neutral
    # provider meaning and expose a growth value only when the name says Growth.
    isin_div_payout_growth: str | None = None
    isin_growth: str | None = None
    isin_div_reinvestment: str | None = None
    nav: Decimal
    nav_date: date
    active: bool = True
    consecutive_absences: int = 0
    supersedes_id: UUID | None = None
    source_artifact_id: UUID | None = None
    # Provider-native fields are retained independently of normalized metadata.
    raw_scheme_code: str = ""
    raw_scheme_name: str = ""
    raw_amc: str | None = None
    raw_category: str | None = None
    raw_isin_div_payout_growth: str = ""
    raw_isin_div_reinvestment: str = ""
    raw_nav: str = ""
    raw_date: str = ""

    @model_validator(mode="before")
    @classmethod
    def assign_ids_and_identifier(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        values = dict(data)
        code = str(values.get("scheme_code", "")).strip()
        if not code:
            return values
        values.setdefault("scheme_id", _scheme_id(code))
        values.setdefault("provider_identifier", code)
        if "record_id" not in values and {"nav_date", "nav", "scheme_name"}.issubset(values):
            values["record_id"] = _record_id(
                code,
                values["nav_date"],
                values["nav"],
                str(values["scheme_name"]),
                values.get("amc"),
            )
        return values

    @model_validator(mode="after")
    def validate_identity(self) -> AmfiScheme:
        if self.scheme_id != _scheme_id(self.scheme_code):
            raise ValueError("AMFI scheme_id must be derived from scheme_code")
        if self.provider != "amfi" or self.provider_identifier != self.scheme_code:
            raise ValueError("AMFI provider identity must be keyed by scheme_code")
        if self.asset_class is not AssetClass.MUTUAL_FUND:
            raise ValueError("AMFI records must be mutual-fund schemes")
        if self.consecutive_absences < 0:
            raise ValueError("consecutive_absences cannot be negative")
        return self

    @property
    def id(self) -> UUID:
        return self.scheme_id

    @property
    def nav_effective_date(self) -> date:
        return self.nav_date

    @property
    def is_direct(self) -> bool:
        return self.plan == "Direct"

    @property
    def is_regular(self) -> bool:
        return self.plan == "Regular"

    @property
    def is_growth(self) -> bool:
        return self.option == "Growth"

    @property
    def is_distribution(self) -> bool:
        return self.option in {"IDCW", "Dividend"}

    @property
    def distribution_type(self) -> str | None:
        return self.distribution

    @classmethod
    def from_nav_row(cls, row: AmfiNavRow, *, supersedes_id: UUID | None = None) -> AmfiScheme:
        amc = row.amc.strip() if row.amc else None
        category = row.category.strip() if row.category else None
        return cls(
            scheme_id=_scheme_id(row.scheme_code),
            record_id=_record_id(row.scheme_code, row.effective_date, row.nav, row.scheme_name, amc),
            scheme_code=row.scheme_code,
            scheme_name=row.scheme_name,
            provider_identifier=row.scheme_code,
            amc=amc,
            category=category,
            plan=_classify_plan(row.scheme_name),
            option=_classify_option(row.scheme_name),
            distribution=_classify_distribution(row.scheme_name),
            isin_div_payout_growth=row.isin_div_payout_growth,
            isin_growth=(row.isin_div_payout_growth if _classify_option(row.scheme_name) == "Growth" else None),
            isin_div_reinvestment=row.isin_div_reinvestment,
            nav=row.nav,
            nav_date=row.effective_date,
            supersedes_id=supersedes_id,
            raw_scheme_code=row.raw_scheme_code,
            raw_scheme_name=row.raw_scheme_name,
            raw_amc=row.raw_amc,
            raw_category=row.raw_category,
            raw_isin_div_payout_growth=row.raw_isin_div_payout_growth,
            raw_isin_div_reinvestment=row.raw_isin_div_reinvestment,
            raw_nav=row.raw_nav,
            raw_date=row.raw_date,
        )


@dataclass(frozen=True)
class AmfiSchemeBatch:
    schemes: tuple[AmfiScheme, ...]
    superseded_ids: tuple[UUID, ...] = ()

    @property
    def records(self) -> tuple[AmfiScheme, ...]:
        return self.schemes

    def active(self) -> tuple[AmfiScheme, ...]:
        return tuple(scheme for scheme in self.schemes if scheme.active)


def _as_scheme(value: AmfiNavRow | AmfiScheme) -> AmfiScheme:
    scheme = value if isinstance(value, AmfiScheme) else AmfiScheme.from_nav_row(value)
    if scheme.scheme_id != _scheme_id(scheme.scheme_code):
        raise ValueError(f"AMFI scheme {scheme.scheme_code} has an unstable scheme_id")
    if scheme.provider_identifier != scheme.scheme_code:
        raise ValueError(f"AMFI scheme {scheme.scheme_code} has an invalid provider identifier")
    return scheme


def _validate_date(scheme: AmfiScheme, effective_date: date | None) -> None:
    if effective_date is not None and scheme.nav_date != effective_date:
        raise ValueError(
            f"AMFI scheme {scheme.scheme_code} NAV date {scheme.nav_date.isoformat()} "
            f"does not match requested date {effective_date.isoformat()}"
        )


def normalize_amfi_schemes(
    rows: Iterable[AmfiNavRow | AmfiScheme],
    *,
    effective_date: date | None = None,
    previous: Iterable[AmfiScheme] | None = None,
    corrections: Iterable[AmfiNavRow | AmfiScheme] = (),
    consecutive_absence_window: int = 2,
) -> AmfiSchemeBatch:
    """Normalize a current file and optionally reconcile prior snapshots.

    A correction is explicit input and creates a new record with a
    ``supersedes_id`` link.  Missing schemes are retained as active until they
    have been absent from the configured number of *consecutive current files*.
    """

    if consecutive_absence_window < 1:
        raise ValueError("consecutive_absence_window must be at least one")
    latest: dict[str, AmfiScheme] = {}
    if previous is not None:
        for scheme in previous:
            _validate_date(scheme, None)
            if scheme.scheme_code in latest:
                raise ValueError(f"duplicate previous AMFI scheme code: {scheme.scheme_code}")
            latest[scheme.scheme_code] = scheme
    current: dict[str, AmfiScheme] = {}
    for value in rows:
        scheme = _as_scheme(value)
        _validate_date(scheme, effective_date)
        if scheme.scheme_code in current:
            if scheme != current[scheme.scheme_code]:
                raise ValueError(f"conflicting duplicate AMFI scheme code: {scheme.scheme_code}")
            continue
        current[scheme.scheme_code] = scheme
    superseded: list[UUID] = []
    for value in corrections:
        correction = _as_scheme(value)
        _validate_date(correction, effective_date)
        prior = current.get(correction.scheme_code) or latest.get(correction.scheme_code)
        if prior is not None:
            if correction.nav_date != prior.nav_date:
                raise ValueError(
                    f"AMFI correction date {correction.nav_date.isoformat()} does not match "
                    f"prior record date {prior.nav_date.isoformat()}"
                )
            correction = correction.model_copy(update={"supersedes_id": prior.record_id})
            superseded.append(prior.record_id)
        current[correction.scheme_code] = correction

    result: dict[str, AmfiScheme] = {}
    for code, scheme in current.items():
        # A current observation always revives a scheme and resets absence.
        result[code] = scheme.model_copy(update={"active": True, "consecutive_absences": 0})
    for code, prior in latest.items():
        if code in current:
            continue
        count = prior.consecutive_absences + 1
        result[code] = prior.model_copy(
            update={
                "consecutive_absences": count,
                "active": count < consecutive_absence_window,
            }
        )
    return AmfiSchemeBatch(
        tuple(result[code] for code in sorted(result)),
        tuple(dict.fromkeys(superseded)),
    )


__all__ = ["AmfiScheme", "AmfiSchemeBatch", "normalize_amfi_schemes"]
