"""Local, provenance-validated parsers for operator-supplied NSE filings.

NSE corporate-filings automation is deliberately unavailable while the source
terms prohibit it.  These helpers parse only an artifact supplied by an
operator and retain its immutable :class:`SourceArtifact` lineage.
"""

from __future__ import annotations

import csv
import io
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping
from datetime import date, datetime, timezone
from hashlib import sha256
from typing import Any

from market_pipeline.domain.models import FetchedArtifact, SourceArtifact
from market_pipeline.normalization.fundamentals import (
    V1_FINANCIAL_FIELDS,
    FundamentalPeriod,
)
from market_pipeline.sources.base import SourceAdapter
from market_pipeline.sources.registry import (
    SOURCE_POLICIES,
    SourcePolicyError,
    assert_source_url_allowed,
    get_source_policy,
)


class NseFilingsProvenanceError(ValueError):
    """Raised when a filing is not demonstrably from the official source."""


def _validate_provenance(artifact: SourceArtifact) -> None:
    policy = SOURCE_POLICIES["nse-filings-xbrl"]
    try:
        if artifact.source_id != policy.source_id:
            raise SourcePolicyError("filing artifact has an unexpected source")
        assert_source_url_allowed(artifact.source_id, artifact.source_url)
        if artifact.terms_url != policy.terms_url:
            raise SourcePolicyError("filing artifact terms reference is not canonical")
    except (KeyError, SourcePolicyError) as exc:
        raise NseFilingsProvenanceError(str(exc)) from exc


def _artifact_from_args(
    body: bytes,
    *,
    source_artifact: SourceArtifact | None,
    artifact: SourceArtifact | None,
    source_url: str | None,
    terms_url: str | None,
    effective_date: date | None,
) -> SourceArtifact:
    supplied = source_artifact or artifact
    if source_artifact is not None and artifact is not None and source_artifact != artifact:
        raise NseFilingsProvenanceError("source_artifact and artifact disagree")
    if supplied is None:
        raise NseFilingsProvenanceError("a SourceArtifact or FetchedArtifact is required")
    if source_url is not None and supplied.source_url != source_url:
        raise NseFilingsProvenanceError("source URL does not match SourceArtifact")
    if terms_url is not None and supplied.terms_url != terms_url:
        raise NseFilingsProvenanceError("terms URL does not match SourceArtifact")
    if effective_date is not None and supplied.effective_date != effective_date:
        raise NseFilingsProvenanceError("effective date does not match SourceArtifact")
    _validate_provenance(supplied)
    if supplied.checksum.lower() != sha256(body).hexdigest().lower():
        raise NseFilingsProvenanceError("filing artifact checksum does not match supplied bytes")
    return supplied


_ALIASES: dict[str, tuple[str, ...]] = {
    "instrument": ("instrument_id", "instrument", "symbol", "SYMBOL", "ISIN"),
    "period_end": ("period_end", "period", "PERIOD_END", "PERIOD"),
    "period_type": ("period_type", "PERIOD_TYPE", "PERIOD TYPE"),
    "filing_id": ("filing_id", "FILING_ID", "FILING ID", "FILING"),
    "filed_at": ("filed_at", "FILED_AT", "FILED DATE", "FILING DATE"),
    "restates_id": ("restates_id", "RESTATES_ID", "RESTATES ID", "RESTATEMENT_OF"),
}

_METRIC_ALIASES: dict[str, str] = {
    "sales": "revenue",
    "sales_revenue": "revenue",
    "revenue_from_operations": "revenue",
    "operating_profit_loss": "operating_profit",
    "operating_profit_loss_before_tax": "operating_profit",
    "net_profit_loss": "net_profit",
    "profit_loss": "net_profit",
    "earnings_per_share": "eps",
    "basic_eps": "eps",
    "total_equity": "equity",
    "borrowings": "debt",
    "cash_flow_from_operating_activities": "operating_cash_flow",
    "cash_flow_from_operations": "operating_cash_flow",
    "debt_to_equity": "debt_equity",
    "price_to_earnings": "pe",
    "market_capitalisation": "market_cap",
    "market_capitalization": "market_cap",
}


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def _column(row: Mapping[str, str], field: str) -> str | None:
    normalized = {_key(key): key for key in row}
    for alias in _ALIASES[field]:
        if _key(alias) in normalized:
            return normalized[_key(alias)]
    return None


def _date(value: str) -> date:
    value = value.strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    return datetime.fromisoformat(value.replace("Z", "+00:00")).date()


def _infer_period_type(days: int) -> str:
    # Filing periods vary by leap years and whether the first/last day is
    # included, so use deliberately narrow, deterministic bands.
    if 80 <= days <= 100:
        return "quarter"
    if 170 <= days <= 200:
        return "half_year"
    if 250 <= days <= 290:
        return "nine_month"
    if 330 <= days <= 380:
        return "annual"
    raise ValueError(f"unsupported duration length for period type: {days} days")


def _normalize_period_type(value: str) -> str:
    candidate = value.strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "q": "quarter",
        "q1": "quarter",
        "q2": "quarter",
        "q3": "quarter",
        "q4": "quarter",
        "quarterly": "quarter",
        "6m": "half_year",
        "halfyear": "half_year",
        "half_yearly": "half_year",
        "9m": "nine_month",
        "ninem": "nine_month",
        "fy": "annual",
        "full_year": "annual",
    }
    return aliases.get(candidate, candidate)


def _period_from_row(row: Mapping[str, str], artifact: SourceArtifact) -> FundamentalPeriod:
    instrument_col = _column(row, "instrument")
    end_col = _column(row, "period_end")
    if not instrument_col or not end_col:
        raise ValueError("filing row requires instrument and period_end")
    instrument = row[instrument_col].strip()
    period_end = _date(row[end_col])
    period_type_col = _column(row, "period_type")
    filing_col = _column(row, "filing_id")
    filed_col = _column(row, "filed_at")
    reported: dict[str, str] = {}
    for name, value in row.items():
        field = _METRIC_ALIASES.get(_key(name), _key(name))
        if field in V1_FINANCIAL_FIELDS and value.strip() != "":
            reported[field] = value.strip()
        elif field.endswith("_numerator") and value.strip() != "":
            reported.setdefault(field.removesuffix("_numerator"), value.strip())
    data: dict[str, Any] = {
        "instrument_id": instrument,
        "period_end": period_end,
        "period_type": _normalize_period_type(row[period_type_col])
        if period_type_col
        else "unknown",
        "filing_id": row[filing_col].strip() if filing_col else artifact.filename,
        "filed_at": datetime.fromisoformat(row[filed_col].strip().replace("Z", "+00:00"))
        if filed_col
        else artifact.retrieved_at,
        "source_artifact_id": artifact.artifact_id,
        "source_artifact": artifact,
        "reported": reported,
    }
    restates_col = _column(row, "restates_id")
    if restates_col and row[restates_col].strip():
        data["restates_id"] = row[restates_col].strip()
    # CSV filing identifiers are not UUIDs; only UUID lineage is accepted by
    # the domain model.  Retain the textual filing ID and omit non-UUID links.
    if not isinstance(data.get("restates_id"), str) or not re.fullmatch(
        r"[0-9a-fA-F-]{36}", str(data.get("restates_id", ""))
    ):
        data.pop("restates_id", None)
    # Symbol/ISIN strings are not stable UUIDs.  Use a deterministic UUID that
    # is still stable across repeated local parsing of the same official file.
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", instrument):
        from uuid import NAMESPACE_URL, uuid5

        data["instrument_id"] = uuid5(NAMESPACE_URL, "stonks/nse-instrument/" + instrument)
    return FundamentalPeriod.model_validate(data)


def parse_financial_results_csv(
    body: bytes | str | FetchedArtifact,
    *,
    source_artifact: SourceArtifact | None = None,
    artifact: SourceArtifact | None = None,
    source_url: str | None = None,
    terms_url: str | None = None,
    effective_date: date | None = None,
) -> list[FundamentalPeriod]:
    """Parse a user-supplied official NSE financial-results CSV."""

    if isinstance(body, FetchedArtifact):
        if source_artifact is not None or artifact is not None:
            raise NseFilingsProvenanceError("artifact provided twice")
        source_artifact = body.artifact
        raw = body.body
    else:
        raw = body.encode() if isinstance(body, str) else body
    provenance = _artifact_from_args(
        raw,
        source_artifact=source_artifact,
        artifact=artifact,
        source_url=source_url,
        terms_url=terms_url,
        effective_date=effective_date,
    )
    try:
        text = raw.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            raise ValueError("filing CSV is missing a header")
        return [_period_from_row({str(k): (v or "") for k, v in row.items()}, provenance) for row in reader]
    except (UnicodeDecodeError, csv.Error, ValueError) as exc:
        if isinstance(exc, NseFilingsProvenanceError):
            raise
        raise ValueError(f"invalid NSE filing CSV: {exc}") from exc


def parse_financial_results_xbrl(
    body: bytes | FetchedArtifact,
    *,
    source_artifact: SourceArtifact | None = None,
    artifact: SourceArtifact | None = None,
    source_url: str | None = None,
    terms_url: str | None = None,
    effective_date: date | None = None,
    period_end: date | None = None,
    period_type: str | None = None,
) -> list[FundamentalPeriod]:
    """Parse simple fact/context XBRL supplied by an operator.

    The parser supports the compact XBRL fixture shape used by the pipeline;
    unknown tags remain ignored rather than being mistaken for V1 fields.
    """

    if isinstance(body, FetchedArtifact):
        if source_artifact is not None or artifact is not None:
            raise NseFilingsProvenanceError("artifact provided twice")
        source_artifact = body.artifact
        body = body.body
    provenance = _artifact_from_args(
        body,
        source_artifact=source_artifact,
        artifact=artifact,
        source_url=source_url,
        terms_url=terms_url,
        effective_date=effective_date,
    )
    root = ET.fromstring(body)
    contexts: dict[str, dict[str, Any]] = {}
    for context in root.findall(".//{*}context"):
        context_id = context.attrib.get("id")
        if not context_id:
            raise ValueError("XBRL context is missing an ID")
        instant_element = context.find("./{*}period/{*}instant")
        start = context.find("./{*}period/{*}startDate")
        end = context.find("./{*}period/{*}endDate")
        if instant_element is not None and instant_element.text:
            contexts[context_id] = {
                "id": context_id,
                "kind": "instant",
                "start": None,
                "end": _date(instant_element.text),
                "period_type": "instant",
                "entity": context.find("./{*}entity/{*}identifier"),
            }
        elif start is not None and start.text and end is not None and end.text:
            start_date = _date(start.text)
            end_date = _date(end.text)
            contexts[context_id] = {
                "id": context_id,
                "kind": "duration",
                "start": start_date,
                "end": end_date,
                "period_type": _infer_period_type((end_date - start_date).days + 1),
                "entity": context.find("./{*}entity/{*}identifier"),
            }
        else:
            raise ValueError(f"XBRL context has unsupported period shape: {context_id}")
    if not contexts:
        raise ValueError("XBRL filing is missing a context")

    metadata_period_type = period_type
    if metadata_period_type is None:
        for element in root.iter():
            local = element.tag.rsplit("}", 1)[-1].lower().replace("_", "")
            if local in {"periodtype", "reportperiod", "filingperiod"} and element.text:
                candidate = _normalize_period_type(element.text)
                if candidate in {"quarter", "half_year", "nine_month", "annual", "instant"}:
                    metadata_period_type = candidate
                    break
    metadata_period_type = (
        _normalize_period_type(metadata_period_type) if metadata_period_type is not None else None
    )
    target_end = period_end or max(item["end"] for item in contexts.values())
    duration = [
        item for item in contexts.values() if item["kind"] == "duration" and item["end"] == target_end
    ]
    instant_contexts = [
        item for item in contexts.values() if item["kind"] == "instant" and item["end"] == target_end
    ]
    selected_duration: dict[str, Any] | None = None
    if duration:
        if metadata_period_type is not None and metadata_period_type != "instant":
            duration = [item for item in duration if item["period_type"] == metadata_period_type]
            if not duration:
                raise ValueError("filing period type does not match any duration context")
        elif len({item["period_type"] for item in duration}) > 1:
            raise ValueError("ambiguous duration contexts for filing period")
        if len(duration) != 1:
            raise ValueError("ambiguous duration contexts for filing period")
        selected_duration = duration[0]
    elif metadata_period_type not in {None, "instant"}:
        raise ValueError("filing period type requires a matching duration context")
    if len(instant_contexts) > 1:
        raise ValueError("ambiguous instant contexts for filing period")
    selected_instant = instant_contexts[0] if instant_contexts else None
    selected_ids = {
        item["id"] for item in (selected_duration, selected_instant) if item is not None
    }
    if not selected_ids:
        raise ValueError("XBRL filing has no context ending on filing period end")
    inferred_type = metadata_period_type or (
        selected_duration["period_type"] if selected_duration is not None else "instant"
    )
    xbrl_metric_aliases = {
        "revenuefromoperations": "revenue",
        "revenue": "revenue",
        "operatingprofit": "operating_profit",
        "profitloss": "net_profit",
        "netprofit": "net_profit",
        "earningspersharebasic": "eps",
        "earningspershare": "eps",
        "equity": "equity",
        "borrowings": "debt",
        "cashflowsfromusedinoperatingactivities": "operating_cash_flow",
        "capitalemployed": "capital_employed",
        "returnonequity": "roe",
        "returnoncapitalemployed": "roce",
        "debtequityratio": "debt_equity",
        "salesgrowth": "sales_growth",
        "profitgrowth": "profit_growth",
        "pricetoearnings": "pe",
        "marketcapitalisation": "market_cap",
        "marketcapitalization": "market_cap",
    }
    balance_fields = {"equity", "debt", "capital_employed", "market_cap"}
    facts: dict[str, str] = {}
    for element in root.iter():
        name = element.tag.rsplit("}", 1)[-1].lower().replace("_", "")
        metric = xbrl_metric_aliases.get(name)
        context_ref = element.attrib.get("contextRef")
        expected_context = selected_instant if metric in balance_fields else selected_duration
        if expected_context is None:
            expected_context = selected_instant
        if expected_context is None:
            continue
        if metric is not None and element.text and context_ref == expected_context["id"]:
            facts[metric] = element.text.strip()
    context_for_entity = selected_duration or selected_instant
    if context_for_entity is None:
        raise ValueError("XBRL filing has no selected context")
    selected_entities = {
        item["entity"].text.strip()
        for item in (selected_duration, selected_instant)
        if item is not None and item["entity"] is not None and item["entity"].text
    }
    if len(selected_entities) > 1:
        raise ValueError("selected XBRL contexts belong to different entities")
    entity = context_for_entity["entity"]
    instrument = entity.text.strip() if entity is not None and entity.text else "unknown"
    from uuid import NAMESPACE_URL, uuid5

    return [
        FundamentalPeriod(
            instrument_id=uuid5(NAMESPACE_URL, "stonks/nse-instrument/" + instrument),
            period_end=target_end,
            period_type=inferred_type,
            filing_id=provenance.filename,
            filed_at=provenance.retrieved_at,
            source_artifact_id=provenance.artifact_id,
            metrics=facts,
        )
    ]


class _NseFilingsImplementation:
    source_id = "nse-filings-xbrl"
    adapter_version = "1.0.0"
    policy = get_source_policy(source_id)

    def __init__(self, provider: Callable[[date], bytes | FetchedArtifact] | None) -> None:
        self.provider = provider

    def _fetch(self, effective_date: date) -> list[FetchedArtifact]:
        if self.provider is None:
            raise NseFilingsProvenanceError("NSE filings require an operator-supplied artifact")
        supplied = self.provider(effective_date)
        if isinstance(supplied, FetchedArtifact):
            return [supplied]
        artifact = SourceArtifact(
            source_id=self.source_id,
            source_url=self.policy.source_url,
            retrieved_at=datetime.now(timezone.utc),
            effective_date=effective_date,
            checksum=sha256(supplied).hexdigest(),
            adapter_version=self.adapter_version,
            terms_url=self.policy.terms_url,
            filename=f"nse-filings-{effective_date.isoformat()}.csv",
        )
        return [FetchedArtifact(artifact=artifact, body=supplied)]


class NseFilingsAdapter(SourceAdapter):
    """Policy-gated wrapper; public fetch remains disabled by design."""

    def __init__(self, provider: Callable[[date], bytes | FetchedArtifact] | None = None) -> None:
        super().__init__(_NseFilingsImplementation(provider))


__all__ = [
    "NseFilingsAdapter",
    "NseFilingsProvenanceError",
    "parse_financial_results_csv",
    "parse_financial_results_xbrl",
    "parse_nse_filings_csv",
    "parse_nse_filings_xbrl",
]

# Descriptive aliases retained for callers that name the file format first.
parse_nse_filings_csv = parse_financial_results_csv
parse_nse_filings_xbrl = parse_financial_results_xbrl
