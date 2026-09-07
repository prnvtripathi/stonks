"""Parsing and V1 eligibility rules for user-supplied NSE reports.

This module deliberately has no network client.  NSE files are accepted as
bytes supplied by an operator and converted into the provider-neutral domain
models only after the source-specific eligibility checks below.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable, Mapping

from market_pipeline.domain.models import AssetClass, Instrument


class NseRowError(ValueError):
    """Raised when an NSE report's schema cannot be interpreted."""


@dataclass(frozen=True)
class RejectedNseRow:
    row_number: int
    symbol: str | None
    raw_series: str | None
    raw_type: str | None
    reason: str
    row: Mapping[str, str]


@dataclass(frozen=True)
class NseInstrumentBatch:
    instruments: tuple[Instrument, ...]
    rejected_rows: tuple[RejectedNseRow, ...]


@dataclass(frozen=True)
class NseParsedRow:
    row_number: int
    symbol: str
    series: str
    instrument_type: str
    name: str
    isin: str
    report_date: date | None
    close: float | None
    row: Mapping[str, str]

    @property
    def raw_series(self) -> str:
        return self.series

    @property
    def raw_type(self) -> str:
        return self.instrument_type


_ALIASES: dict[str, tuple[str, ...]] = {
    "symbol": ("SYMBOL", "TckrSymb", "TICKER", "SECURITY SYMBOL"),
    "series": ("SERIES", "SctySrs", "SECURITY SERIES"),
    "type": ("TYPE", "INSTRUMENT TYPE", "SECURITY TYPE", "FinInstrmTp"),
    "name": ("NAME OF COMPANY", "NAME", "FinInstrmNm", "SECURITY NAME"),
    "isin": ("ISIN", "ISIN NUMBER", "ISIN Code", "ISIN_CODE"),
    "date": ("TIMESTAMP", "DATE", "TradDt", "BizDt", "REPORT DATE"),
    "close": ("CLOSE", "ClsPric", "CLOSING PRICE", "CLOSE PRICE"),
}


def _header(value: str) -> str:
    return " ".join(value.replace("\ufeff", "").strip().split()).upper()


def _read_csv(body: bytes | str) -> tuple[list[dict[str, str]], dict[str, str]]:
    try:
        text = body.decode("utf-8-sig") if isinstance(body, bytes) else body
    except UnicodeDecodeError as exc:
        raise NseRowError("NSE report is not valid UTF-8 text") from exc
    if not text.strip():
        raise NseRowError("NSE report is empty")
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;|\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if reader.fieldnames is None:
        raise NseRowError("NSE report is missing a header")
    headers = {_header(name): name for name in reader.fieldnames if name is not None}
    rows = [
        {_header(key): (value or "").strip() for key, value in row.items() if key is not None}
        for row in reader
    ]
    if not rows:
        raise NseRowError("NSE report has fewer rows than the minimum row-count threshold")
    if len(rows) > 1_000_000:
        raise NseRowError("NSE report exceeds the maximum row-count threshold")
    return rows, headers


def _column(headers: Mapping[str, str], field: str, *, required: bool) -> str | None:
    normalized = {_header(key): key for key in headers}
    for alias in _ALIASES[field]:
        if _header(alias) in normalized:
            return _header(normalized[_header(alias)])
    if required:
        aliases = ", ".join(_ALIASES[field][:3])
        raise NseRowError(f"NSE report is missing required column ({aliases})")
    return None


def _parse_date(value: str) -> date | None:
    value = value.strip()
    if not value:
        return None
    for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    # ISO timestamps occur in newer NSE reports.
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError as exc:
        raise ValueError(f"invalid report date: {value!r}") from exc


def _value(row: Mapping[str, str], column: str | None) -> str:
    return row.get(column or "", "").strip()


def parse_nse_security_master(body: bytes | str) -> list[NseParsedRow]:
    """Parse an NSE security-master CSV while retaining native fields."""

    rows, headers = _read_csv(body)
    symbol_col = _column(headers, "symbol", required=True)
    series_col = _column(headers, "series", required=True)
    name_col = _column(headers, "name", required=True)
    isin_col = _column(headers, "isin", required=False)
    type_col = _column(headers, "type", required=False)
    parsed: list[NseParsedRow] = []
    for number, row in enumerate(rows, start=2):
        symbol = _value(row, symbol_col).upper()
        parsed.append(
            NseParsedRow(
                row_number=number,
                symbol=symbol,
                series=_value(row, series_col).upper(),
                instrument_type=_value(row, type_col).upper(),
                name=_value(row, name_col),
                isin=_value(row, isin_col).upper(),
                report_date=None,
                close=None,
                row=row,
            )
        )
    return parsed


def parse_nse_bhavcopy(body: bytes | str) -> list[NseParsedRow]:
    """Parse legacy and current NSE CM bhavcopy CSV layouts."""

    rows, headers = _read_csv(body)
    symbol_col = _column(headers, "symbol", required=True)
    series_col = _column(headers, "series", required=True)
    date_col = _column(headers, "date", required=True)
    close_col = _column(headers, "close", required=False)
    type_col = _column(headers, "type", required=False)
    name_col = _column(headers, "name", required=False)
    isin_col = _column(headers, "isin", required=False)
    parsed: list[NseParsedRow] = []
    for number, row in enumerate(rows, start=2):
        symbol = _value(row, symbol_col).upper()
        raw_date = _value(row, date_col)
        try:
            report_date = _parse_date(raw_date)
        except ValueError:
            report_date = None
        close: float | None
        try:
            close = float(_value(row, close_col)) if _value(row, close_col) else None
        except ValueError:
            close = None
        parsed.append(
            NseParsedRow(
                row_number=number,
                symbol=symbol,
                series=_value(row, series_col).upper(),
                instrument_type=_value(row, type_col).upper(),
                name=_value(row, name_col),
                isin=_value(row, isin_col).upper(),
                report_date=report_date,
                close=close,
                row=row,
            )
        )
    return parsed


def _looks_like_etf(row: NseParsedRow) -> bool:
    native_type = row.instrument_type.replace("-", " ").replace("/", " ")
    if native_type in {"ETF", "ETP", "EXCHANGE TRADED FUND", "ETF ETP"}:
        return True
    # Legacy bhavcopies do not carry instrument type.  Keep this narrow and
    # only infer from an explicit ETF name marker or the well-known BeES suffix.
    name = f" {row.name.upper()} "
    return " ETF " in name or row.symbol.endswith("BEES")


def _rejected(row: NseParsedRow, reason: str) -> RejectedNseRow:
    return RejectedNseRow(
        row_number=row.row_number,
        symbol=row.symbol or None,
        raw_series=row.raw_series or None,
        raw_type=row.raw_type or None,
        reason=reason,
        row=row.row,
    )


def normalize_nse_rows(
    body: bytes | str | Iterable[NseParsedRow],
    *,
    report_date: date | None = None,
    security_master: bytes | str | Iterable[NseParsedRow] | None = None,
) -> NseInstrumentBatch:
    """Create canonical instruments and retain every row rejected by V1 policy."""

    if isinstance(body, bytes) and body[:2] == b"PK":
        # Keep the normalizer convenient for operator-supplied ZIP artifacts;
        # the source helper performs member traversal/size validation.
        from market_pipeline.sources.nse_eod import read_nse_archive

        body = read_nse_archive(body)
    rows = parse_nse_bhavcopy(body) if isinstance(body, (bytes, str)) else list(body)
    master_by_symbol: dict[str, NseParsedRow] = {}
    if security_master is not None:
        from market_pipeline.normalization.nse import parse_nse_security_master

        master_rows = (
            parse_nse_security_master(security_master)
            if isinstance(security_master, (bytes, str))
            else list(security_master)
        )
        master_by_symbol = {row.symbol: row for row in master_rows if row.symbol}
    instruments: list[Instrument] = []
    rejected: list[RejectedNseRow] = []
    seen: set[str] = set()
    inferred_date: date | None = report_date
    for row in rows:
        if not row.symbol or not re.fullmatch(r"[A-Z0-9&._-]+", row.symbol):
            rejected.append(_rejected(row, "malformed row: symbol is missing or invalid"))
            continue
        master = master_by_symbol.get(row.symbol)
        # Bhavcopies often omit the company name, ISIN, and ETF classification;
        # enrich those fields from the same-date security master when supplied.
        if master is not None:
            row = NseParsedRow(
                row_number=row.row_number,
                symbol=row.symbol,
                series=row.series or master.series,
                instrument_type=row.instrument_type or master.instrument_type,
                name=row.name or master.name,
                isin=row.isin or master.isin,
                report_date=row.report_date,
                close=row.close,
                row=row.row,
            )
        if not row.series:
            rejected.append(_rejected(row, "malformed row: series is missing"))
            continue
        if report_date is not None and row.report_date != report_date:
            rejected.append(_rejected(row, "report date does not match requested effective date"))
            continue
        if row.report_date is not None:
            if inferred_date is None:
                inferred_date = row.report_date
            elif row.report_date != inferred_date:
                rejected.append(_rejected(row, "report date mismatch within report"))
                continue
        if row.close is None and row.report_date is not None:
            rejected.append(_rejected(row, "malformed row: closing price is missing or invalid"))
            continue
        if row.report_date is None and row.close is not None:
            rejected.append(_rejected(row, "malformed row: report date is missing or invalid"))
            continue
        if row.symbol in seen:
            rejected.append(_rejected(row, "duplicate symbol"))
            continue
        if row.series != "EQ":
            rejected.append(_rejected(row, f"ineligible series: {row.raw_series}"))
            continue
        native_type = row.raw_type.replace("-", " ").replace("/", " ")
        if native_type in {"SME", "REIT", "INVIT", "IN VIT", "PREFERENCE", "PARTLY PAID"}:
            rejected.append(_rejected(row, f"ineligible instrument type: {row.raw_type}"))
            continue
        asset_class = AssetClass.ETF if _looks_like_etf(row) else AssetClass.EQUITY
        if not row.isin and not row.symbol:
            rejected.append(_rejected(row, "malformed row: stable identifier is missing"))
            continue
        provider_identifier = row.isin or row.symbol
        instruments.append(
            Instrument.from_provider(
                "nse",
                provider_identifier,
                asset_class,
                symbol=row.symbol,
                name=row.name or None,
                raw_series=row.raw_series,
                raw_type=row.raw_type or None,
            )
        )
        seen.add(row.symbol)
    return NseInstrumentBatch(tuple(instruments), tuple(rejected))
