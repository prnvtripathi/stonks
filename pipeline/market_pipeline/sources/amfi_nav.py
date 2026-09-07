"""Parser and governed adapter for AMFI's official daily NAV report.

AMFI publishes a semi-colon separated text report.  It is not a conventional
CSV: AMC and category labels are interspersed with data rows.  The parser keeps
those labels and every provider value available for audit while exposing
canonical values to the normalization layer.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Final

from market_pipeline.domain.models import FetchedArtifact, SourceArtifact
from market_pipeline.sources.base import SourceAdapter
from market_pipeline.sources.registry import assert_artifact_policy, get_source_policy


class AmfiNavError(ValueError):
    """Raised when an AMFI artifact cannot be interpreted safely."""


@dataclass(frozen=True)
class AmfiNavRow:
    """One validated AMFI NAV row, retaining raw provider cells."""

    row_number: int
    scheme_code: str
    scheme_name: str
    nav: Decimal
    effective_date: date
    amc: str | None
    category: str | None
    isin_div_payout_growth: str | None
    isin_div_reinvestment: str | None
    raw_scheme_code: str
    raw_scheme_name: str
    raw_nav: str
    raw_date: str
    raw_amc: str | None
    raw_category: str | None
    raw_isin_div_payout_growth: str
    raw_isin_div_reinvestment: str

    @property
    def nav_date(self) -> date:
        return self.effective_date

    @property
    def date(self) -> date:
        return self.effective_date

    @property
    def isin_growth(self) -> str | None:
        return self.isin_div_payout_growth

    @property
    def plan(self) -> str | None:
        if re.search(r"\bdirect\s+plan\b", self.scheme_name, re.IGNORECASE):
            return "Direct"
        if re.search(r"\bregular\s+plan\b", self.scheme_name, re.IGNORECASE):
            return "Regular"
        return None

    @property
    def option(self) -> str | None:
        if re.search(r"\bgrowth\b", self.scheme_name, re.IGNORECASE):
            return "Growth"
        if re.search(r"\bidcw\b", self.scheme_name, re.IGNORECASE):
            return "IDCW"
        if re.search(r"\bdividend\b", self.scheme_name, re.IGNORECASE):
            return "Dividend"
        if re.search(r"\bbonus\b", self.scheme_name, re.IGNORECASE):
            return "Bonus"
        return None


_HEADER_ALIASES: Final[dict[str, tuple[str, ...]]] = {
    "code": ("SCHEME CODE", "SCHEMECODE"),
    "isin_growth": (
        "ISIN DIV PAYOUT/ ISIN GROWTH",
        "ISIN DIV PAYOUT / ISIN GROWTH",
        "ISIN DIV PAYOUT/ISIN GROWTH",
        "ISIN GROWTH",
    ),
    "isin_reinvestment": ("ISIN DIV REINVESTMENT", "ISIN REINVESTMENT"),
    "name": ("SCHEME NAME", "SCHEMENAME"),
    "nav": ("NET ASSET VALUE", "NAV"),
    "date": ("DATE", "NAV DATE"),
}


def _header(value: str) -> str:
    return " ".join(value.replace("\ufeff", "").strip().split()).upper()


def _canonical(value: str) -> str:
    return " ".join(re.sub(r"[^A-Z0-9]+", " ", _header(value)).split())


def _decode(body: bytes | str) -> str:
    if isinstance(body, str):
        return body.lstrip("\ufeff")
    encodings = ["utf-8-sig"]
    if body.startswith((b"\xff\xfe", b"\xfe\xff")):
        encodings.append("utf-16")
    encodings.extend(("cp1252", "latin-1"))
    for encoding in encodings:
        try:
            text = body.decode(encoding)
        except UnicodeDecodeError:
            continue
        if encoding == "utf-16" and "\x00" in text:
            continue
        return text.lstrip("\ufeff")
    raise AmfiNavError("AMFI report is not a documented text encoding")


def _is_header(row: list[str]) -> bool:
    values = {_header(value) for value in row}
    return any(alias in values for alias in _HEADER_ALIASES["code"]) and any(
        alias in values for alias in _HEADER_ALIASES["name"]
    )


def _delimiter(header: str) -> str:
    counts = {delimiter: header.count(delimiter) for delimiter in (";", "|", "\t", ",")}
    selected = max(counts, key=lambda delimiter: counts[delimiter])
    if counts[selected] == 0:
        raise AmfiNavError("AMFI report is missing its delimited header")
    return selected


def _column_indices(header: list[str]) -> dict[str, int | None]:
    normalized = {_header(value): index for index, value in enumerate(header)}

    def find(field: str, required: bool) -> int | None:
        for alias in _HEADER_ALIASES[field]:
            if alias in normalized:
                return normalized[alias]
        if required:
            raise AmfiNavError(f"AMFI report is missing required column: {field}")
        return None

    return {
        "code": find("code", True),
        "isin_growth": find("isin_growth", False),
        "isin_reinvestment": find("isin_reinvestment", False),
        "name": find("name", True),
        "nav": find("nav", True),
        "date": find("date", True),
    }


def _cell(row: list[str], index: int | None) -> str:
    return row[index] if index is not None and index < len(row) else ""


def _parse_date(value: str) -> date:
    stripped = value.strip()
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(stripped, fmt).date()
        except ValueError:
            continue
    raise AmfiNavError(f"invalid AMFI date: {value.strip()!r}")


def _parse_nav(value: str) -> Decimal:
    stripped = value.strip()
    try:
        parsed = Decimal(stripped)
    except InvalidOperation as exc:
        raise AmfiNavError(f"invalid AMFI NAV: {value.strip()!r}") from exc
    if not parsed.is_finite() or parsed < 0:
        raise AmfiNavError(f"invalid AMFI NAV: {value.strip()!r}")
    return parsed


def _looks_like_category(value: str) -> bool:
    canonical = _canonical(value)
    if not canonical:
        return False
    # These are labels used by AMFI's hierarchy.  A label is never inferred
    # from the scheme name; it must occur as its own provider row.
    return any(
        token in canonical
        for token in (
            "OPEN ENDED",
            "CLOSE ENDED",
            "INTERVAL",
            "EQUITY SCHEME",
            "DEBT SCHEME",
            "HYBRID SCHEME",
            "SOLUTION ORIENTED",
            "OTHER SCHEMES",
            "INDEX FUNDS",
            "FUND OF FUNDS",
            "ELSS",
        )
    )


def parse_amfi_nav(body: bytes | str, *, expected_date: date | None = None) -> list[AmfiNavRow]:
    """Parse and strictly validate one official AMFI NAV text report."""

    text = _decode(body)
    if not text.strip():
        raise AmfiNavError("AMFI report is empty")
    # The delimiter is discoverable from the first header row, which may not
    # be line one in a hand-supplied artifact.
    raw_lines = text.splitlines()
    header_line = next(
        (
            line
            for line in raw_lines
            if "SCHEME CODE" in _header(line) and any(delimiter in line for delimiter in (";", "|", "\t", ","))
        ),
        next((line for line in raw_lines if line.strip()), ""),
    )
    if "\x00" in header_line:
        raise AmfiNavError("AMFI report contains unsupported binary text")
    delimiter = _delimiter(header_line)
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    header_index = next((index for index, row in enumerate(rows) if _is_header(row)), None)
    if header_index is None:
        raise AmfiNavError("AMFI report is missing its recognized header")
    indices = _column_indices(rows[header_index])
    current_amc: str | None = None
    current_amc_raw: str | None = None
    current_category: str | None = None
    current_category_raw: str | None = None
    parsed: list[AmfiNavRow] = []
    by_code: dict[str, AmfiNavRow] = {}
    report_date: date | None = None
    for number, row in enumerate(rows[header_index + 1 :], start=header_index + 2):
        if not any(value.strip() for value in row):
            continue
        code_raw = _cell(row, indices["code"])
        code = code_raw.strip()
        # A hierarchy line has exactly one meaningful cell and no scheme code.
        nonempty = [value for value in row if value.strip()]
        if len(nonempty) == 1 and not re.fullmatch(r"[0-9]{4,10}", code):
            label = nonempty[0]
            if _looks_like_category(label):
                current_category = label.strip()
                current_category_raw = label
            else:
                current_amc = label.strip()
                current_amc_raw = label
                current_category = None
                current_category_raw = None
            continue
        if not re.fullmatch(r"[0-9]{4,10}", code):
            raise AmfiNavError(f"invalid AMFI scheme code at row {number}: {code_raw.strip()!r}")
        if len(row) != len(rows[header_index]):
            raise AmfiNavError(f"AMFI row {number} has a different field count than its header")
        name_raw = _cell(row, indices["name"])
        nav_raw = _cell(row, indices["nav"])
        date_raw = _cell(row, indices["date"])
        name = name_raw.strip()
        if not name:
            raise AmfiNavError(f"AMFI scheme name is missing at row {number}")
        nav = _parse_nav(nav_raw)
        effective_date = _parse_date(date_raw)
        if expected_date is not None and effective_date != expected_date:
            raise AmfiNavError(
                f"AMFI NAV date {effective_date.isoformat()} does not match requested "
                f"date {expected_date.isoformat()}"
            )
        if report_date is None:
            report_date = effective_date
        elif report_date != effective_date:
            raise AmfiNavError("AMFI report contains multiple NAV dates")
        growth_raw = _cell(row, indices["isin_growth"])
        reinvest_raw = _cell(row, indices["isin_reinvestment"])
        record = AmfiNavRow(
            row_number=number,
            scheme_code=code,
            scheme_name=name,
            nav=nav,
            effective_date=effective_date,
            amc=current_amc,
            category=current_category,
            isin_div_payout_growth=growth_raw.strip() or None,
            isin_div_reinvestment=reinvest_raw.strip() or None,
            raw_scheme_code=code_raw,
            raw_scheme_name=name_raw,
            raw_nav=nav_raw,
            raw_date=date_raw,
            raw_amc=current_amc_raw,
            raw_category=current_category_raw,
            raw_isin_div_payout_growth=growth_raw,
            raw_isin_div_reinvestment=reinvest_raw,
        )
        previous = by_code.get(code)
        if previous is not None:
            if record != previous:
                raise AmfiNavError(f"conflicting duplicate AMFI scheme code: {code}")
            continue
        by_code[code] = record
        parsed.append(record)
    if not parsed:
        raise AmfiNavError("AMFI report contains no scheme rows")
    return parsed


class _AmfiNavImplementation:
    source_id = "amfi-nav"
    adapter_version = "1.0.0"
    policy = get_source_policy("amfi-nav")

    def __init__(self, provider: Callable[[date], bytes | FetchedArtifact] | None) -> None:
        self.provider = provider

    def _fetch(self, effective_date: date) -> list[FetchedArtifact]:
        if self.provider is None:
            raise AmfiNavError("AMFI NAV requires an injected official artifact provider")
        supplied = self.provider(effective_date)
        if isinstance(supplied, FetchedArtifact):
            artifact = supplied.artifact
            assert_artifact_policy(
                source_id=self.source_id,
                source_url=artifact.source_url,
                terms_url=artifact.terms_url,
            )
            if artifact.effective_date != effective_date:
                raise AmfiNavError("AMFI artifact effective date does not match requested date")
            parse_amfi_nav(supplied.body, expected_date=effective_date)
            return [supplied]
        body = supplied
        parse_amfi_nav(body, expected_date=effective_date)
        artifact = SourceArtifact(
            source_id=self.source_id,
            source_url=self.policy.source_url,
            retrieved_at=datetime.now(timezone.utc),
            effective_date=effective_date,
            checksum=sha256(body).hexdigest(),
            adapter_version=self.adapter_version,
            terms_url=self.policy.terms_url,
            filename=f"amfi-nav-{effective_date.isoformat()}.txt",
        )
        return [FetchedArtifact(artifact=artifact, body=body)]


class AmfiNavAdapter(SourceAdapter):
    """Policy-gated AMFI adapter using an injected official download provider."""

    def __init__(self, provider: Callable[[date], bytes | FetchedArtifact] | None = None) -> None:
        super().__init__(_AmfiNavImplementation(provider))


__all__ = ["AmfiNavAdapter", "AmfiNavError", "AmfiNavRow", "parse_amfi_nav"]
