"""Compose NSE bhavcopy/security-master normalization, corporate-action price
adjustment, and existing analytics into a real equity/ETF ``DatasetBuild``.

This is the equity/ETF counterpart to ``jobs/publish.py``'s AMFI-only
``build_candidate``. It deliberately lives in its own module and produces its
own, separately publishable ``DatasetBuild`` (the same frozen dataclass
``jobs/publish.py`` defines): S04 is responsible for composing the AMFI and
NSE builds into one combined dataset, not this task.

Every normalization/analytics primitive used here already exists and is
asset-class-agnostic (``normalization.nse``, ``normalization.corporate_actions``,
``analytics.returns/risk/rs/momentum``). This module's job is composition and
sequencing:

1. Read every checkpointed NSE artifact back out of the immutable raw store,
   through the S01 :class:`~market_pipeline.jobs.source_inputs.SourceInput`
   admission contract (never a hand-built ``SourceArtifact``).
2. Parse each day's bhavcopy, classify eligibility/asset class via
   :func:`~market_pipeline.normalization.nse.normalize_nse_rows`, and key every
   observation by the instrument's *stable* identity (its NSE-namespaced UUID,
   derived from ISIN) rather than by the mutable ``SYMBOL`` column -- this is
   what keeps a renamed instrument's history and current metrics attached to
   one stable ID across the rename.
3. Apply :func:`~market_pipeline.normalization.corporate_actions.adjustment_factors`
   to each instrument's own raw close series *before* any return/risk/RS
   calculation runs, while leaving the raw observations themselves untouched
   (the raw store keeps the original bytes; the adjustment only touches the
   in-memory series this module builds for analytics).
4. Feed the adjusted series into the existing, unmodified analytics functions,
   restricting the equity RS cohort to ``asset_class == "equity"`` (ETFs and
   mutual funds never enter it) via ``equity_rs_rating``'s own
   ``asset_classes`` filter.
5. Publish only instruments with an observation on the requested
   ``effective_date`` to the *current* screen (``tables.instruments`` /
   ``tables.latest_metrics``); an instrument that stops reporting (delisted,
   suspended) keeps its stable ID and full price history in
   ``DatasetBuild.histories`` but drops out of the current screen.

Benchmark RS and fundamentals are always published as ``missing`` (never
invented from price data): S03 has not wired an official benchmark feed or
NSE filings normalization yet.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence
from uuid import UUID

from market_pipeline.analytics import momentum as momentum_analytics
from market_pipeline.analytics import returns as returns_analytics
from market_pipeline.analytics import risk as risk_analytics
from market_pipeline.analytics import rs as rs_analytics
from market_pipeline.analytics.momentum import MomentumScore, momentum_scores
from market_pipeline.analytics.returns import ReturnMetrics, calculate_returns
from market_pipeline.analytics.risk import RiskMetrics, calculate_risk
from market_pipeline.analytics.rs import equity_rs_rating
from market_pipeline.domain.models import Instrument
from market_pipeline.jobs.publish import (
    BENCHMARK_WINDOWS,
    RETURN_METRICS,
    DatasetBuild,
    PublicationInputError,
)
from market_pipeline.jobs.source_inputs import (
    SourceInput,
    SourceInputManifestError,
    SourceInputRole,
    require_roles,
)
from market_pipeline.normalization import fundamentals as fundamentals_normalization
from market_pipeline.normalization.corporate_actions import CorporateAction, adjustment_factors
from market_pipeline.normalization.nse import (
    NseParsedRow,
    NseRowError,
    normalize_nse_rows,
    parse_nse_bhavcopy,
    parse_nse_security_master,
)
from market_pipeline.publication.input_manifest import InputManifestError, manifest_with_fingerprint
from market_pipeline.sources.base import RawStore
from market_pipeline.sources.registry import SourcePolicyError, get_source_policy

NSE_NORMALIZATION_VERSION = "nse-eq-etf-normalization-v1"
CORPORATE_ACTION_FORMULA_VERSION = "nse-corporate-action-adjustment-v1"
PROJECTION_VERSION = "nse-eq-etf-projection-v1"

# S03 has not wired NSE filings normalization yet: every fundamental field is
# published `missing` (equities) or `not_applicable` (ETFs, which do not file
# these), never derived from price data. Reusing the real V1 field catalog
# (rather than inventing separate metric names here) keeps this list in sync
# with whatever S03 eventually populates.
FUNDAMENTAL_METRICS: tuple[str, ...] = fundamentals_normalization.V1_FINANCIAL_FIELDS

_REQUIRED_ROLES = frozenset({SourceInputRole.EOD_OBSERVATIONS, SourceInputRole.SECURITY_MASTER})
_VOLUME_COLUMNS = ("VOLUME", "TOTTRDQTY", "TOT_TRD_QTY", "TTL_TRD_QNTY")


@dataclass
class _InstrumentSeries:
    instrument: Instrument
    prices: dict[date, Decimal] = field(default_factory=dict)
    volumes: dict[date, Decimal] = field(default_factory=dict)


def _extract_volume(row: NseParsedRow) -> Decimal | None:
    """Read a traded-quantity column the shared NSE parser does not model.

    ``normalization/nse.py`` deliberately has no volume concept (Task 6's
    ``NseParsedRow`` only carries ``close``); this reads the provider's raw
    row dict it *does* preserve, rather than reimplementing bhavcopy parsing.
    """

    for key in _VOLUME_COLUMNS:
        if key in row.row:
            raw = row.row[key].strip()
            if not raw:
                continue
            try:
                return Decimal(raw)
            except InvalidOperation:
                continue
    return None


def _parse_corporate_actions(body: bytes | str) -> list[dict[str, Any]]:
    """Parse this module's own supplied corporate-action JSON payload.

    No existing normalizer parses an NSE corporate-action file (only the
    price-adjustment math in ``normalization/corporate_actions.py`` already
    exists); this is new, minimal composition-layer glue, not a duplicate of
    existing normalization.
    """

    text = body.decode("utf-8") if isinstance(body, bytes) else body
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PublicationInputError(f"corporate actions payload is not valid JSON: {exc}") from exc
    actions = data.get("actions") if isinstance(data, Mapping) else data
    if not isinstance(actions, list):
        raise PublicationInputError("corporate actions payload must be a list of actions")
    return actions


def _metric_row(
    instrument_id: str,
    effective: date,
    metric: str,
    value: Decimal | None,
    *,
    state: str = "auto",
    formula_version: str,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    resolved_state = state if state != "auto" else ("present" if value is not None else "missing")
    return {
        "instrument_id": instrument_id,
        "effective_date": effective.isoformat(),
        "metric": metric,
        "value": float(value) if value is not None and resolved_state == "present" else None,
        "state": resolved_state,
        "raw_value": str(value) if value is not None and resolved_state == "present" else None,
        "formula_version": formula_version,
        "source_artifact_id": None,
        "metadata": dict(metadata or {}),
    }


def _sources_table(source_ids: Sequence[str], effective_date: date) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for source_id in sorted(set(source_ids)):
        try:
            policy = get_source_policy(source_id)
            source_url, terms_url = policy.source_url, policy.terms_url
        except SourcePolicyError:
            # A test/fixture source ID absent from the canonical registry
            # (real production NSE admission stays gated by S01 until a
            # supplied-use permission is recorded); describe it honestly
            # rather than fabricating an official URL.
            source_url, terms_url = "", ""
        rows.append({
            "source_id": source_id,
            "source_url": source_url,
            "terms_url": terms_url,
            "status": "complete",
            "effective_date": effective_date.isoformat(),
            "adapter_version": "v1",
        })
    return rows


def build_nse_candidate(
    inputs: Sequence[SourceInput],
    raw_store: RawStore,
    effective_date: date,
) -> DatasetBuild:
    """Assemble an equity/ETF ``DatasetBuild`` from admitted NSE source inputs.

    Every ``inputs`` entry must already have passed S01's ``admit()`` gate
    (via ``resolve_supplied_source_input`` or a future network adapter); this
    function only reads bodies back out of ``raw_store`` by each input's
    recorded ``object_key`` -- it never constructs a ``SourceArtifact`` itself.
    """

    inputs = list(inputs)
    try:
        require_roles(inputs, _REQUIRED_ROLES)
    except SourceInputManifestError as exc:
        # F16: a missing required role (no security master / no EOD
        # observations at all) is a hard publication failure, never a
        # silently-empty-but-technically-successful universe.
        raise PublicationInputError(str(exc)) from exc

    eod_inputs = sorted(
        (item for item in inputs if item.role is SourceInputRole.EOD_OBSERVATIONS),
        key=lambda item: item.loaded_date,
    )
    master_inputs = [item for item in inputs if item.role is SourceInputRole.SECURITY_MASTER]
    action_inputs = [item for item in inputs if item.role is SourceInputRole.CORPORATE_ACTIONS]

    master_for_effective = next((item for item in master_inputs if item.loaded_date == effective_date), None)
    if master_for_effective is None:
        # F16: a missing security master for the requested effective date
        # must never silently fall back to an (incorrectly classified, or
        # simply empty) universe -- fail closed instead.
        raise PublicationInputError(
            "no security master is available for the requested effective date; "
            "refusing to publish a universe with unverified instrument classification"
        )
    if not any(item.loaded_date == effective_date for item in eod_inputs):
        raise PublicationInputError("no NSE EOD observations are available for the requested effective date")

    warnings: list[str] = []
    if not action_inputs:
        warnings.append(
            "no corporate-action coverage was supplied for this run; "
            "published returns/RS reflect unadjusted prices for every instrument"
        )

    manifest_inputs = [
        {
            "source_id": item.source_id,
            "effective_date": item.loaded_date.isoformat(),
            "artifact_id": str(item.artifact_id),
            "checksum": item.checksum.lower(),
            "adapter_version": item.adapter_version,
            "raw_object_key": item.object_key,
        }
        for item in inputs
    ]
    versions = {
        "normalization": NSE_NORMALIZATION_VERSION,
        "analytics": "|".join((
            returns_analytics.FORMULA_VERSION,
            risk_analytics.FORMULA_VERSION,
            rs_analytics.FORMULA_VERSION,
            momentum_analytics.FORMULA_VERSION,
            CORPORATE_ACTION_FORMULA_VERSION,
        )),
        "projection": PROJECTION_VERSION,
    }
    try:
        manifest = manifest_with_fingerprint(manifest_inputs, versions)
    except InputManifestError as exc:
        raise PublicationInputError(str(exc)) from exc
    fingerprint = str(manifest["fingerprint"])

    try:
        master_rows = parse_nse_security_master(raw_store.get(master_for_effective.object_key))
    except NseRowError as exc:
        raise PublicationInputError(f"nse security master could not be parsed: {exc}") from exc

    series: dict[UUID, _InstrumentSeries] = {}
    for eod_item in eod_inputs:
        try:
            parsed_rows = parse_nse_bhavcopy(raw_store.get(eod_item.object_key))
        except NseRowError as exc:
            raise PublicationInputError(f"nse-eod/{eod_item.loaded_date.isoformat()}: {exc}") from exc
        master_arg = master_rows if eod_item.loaded_date == effective_date else None
        batch = normalize_nse_rows(parsed_rows, report_date=eod_item.loaded_date, security_master=master_arg)
        rows_by_symbol = {parsed.symbol: parsed for parsed in parsed_rows}
        for instrument in batch.instruments:
            row = rows_by_symbol.get(instrument.symbol or "")
            if row is None or row.close is None or row.report_date != eod_item.loaded_date:
                continue
            entry = series.setdefault(instrument.instrument_id, _InstrumentSeries(instrument))
            entry.instrument = instrument  # newest classification/display fields win
            entry.prices[eod_item.loaded_date] = Decimal(str(row.close))
            volume = _extract_volume(row)
            if volume is not None:
                entry.volumes[eod_item.loaded_date] = volume
    if not series:
        raise PublicationInputError("checkpointed NSE artifacts produced no normalized instrument rows")

    actions_by_instrument: dict[UUID, list[CorporateAction]] = {}
    action_rows_for_lineage: list[dict[str, Any]] = []
    if action_inputs:
        isin_to_id = {entry.instrument.provider_identifier: instrument_id for instrument_id, entry in series.items()}
        symbol_to_id = {entry.instrument.symbol: instrument_id for instrument_id, entry in series.items()}
        for action_item in action_inputs:
            raw_actions = _parse_corporate_actions(raw_store.get(action_item.object_key))
            for raw in raw_actions:
                if not isinstance(raw, Mapping):
                    raise PublicationInputError("corporate action entry must be a mapping")
                instrument_id = isin_to_id.get(str(raw.get("isin"))) or symbol_to_id.get(str(raw.get("symbol")))
                if instrument_id is None:
                    warnings.append(f"corporate action references an unknown instrument: {dict(raw)}")
                    continue
                try:
                    action = CorporateAction.model_validate(
                        {key: value for key, value in raw.items() if key not in ("isin", "symbol")}
                    )
                except Exception as exc:  # noqa: BLE001 - surfaced as a hard publication failure
                    raise PublicationInputError(f"corporate action is malformed: {exc}") from exc
                actions_by_instrument.setdefault(instrument_id, []).append(action)
                action_rows_for_lineage.append({
                    "instrument_id": str(instrument_id),
                    "action_id": f"{instrument_id}-{action.action_date.isoformat()}-{action.action_type}",
                    "action_date": action.action_date.isoformat(),
                    "action_type": action.action_type,
                    "numerator": float(action.numerator),
                    "denominator": float(action.denominator),
                    "metadata": {"source": "nse-corporate-actions"},
                    "source_artifact_id": None,
                })

    returns_by_instrument: dict[UUID, ReturnMetrics] = {}
    risk_by_instrument: dict[UUID, RiskMetrics] = {}
    histories: dict[str, tuple[str, tuple[tuple[date, Decimal], ...]]] = {}
    adjusted_by_instrument: dict[UUID, tuple[tuple[date, Decimal], ...]] = {}
    volume_by_instrument: dict[UUID, tuple[tuple[date, Decimal], ...]] = {}
    for instrument_id, entry in series.items():
        dated = sorted(entry.prices)
        actions = actions_by_instrument.get(instrument_id, ())
        # Corporate-action adjustment happens here, before any return/risk/RS
        # calculation reads this series: every downstream analytics call in
        # this function only ever sees adjusted prices. The raw observations
        # themselves are untouched in `entry.prices` and in the immutable raw
        # store; only this in-memory adjusted copy is derived from them.
        factors = adjustment_factors(actions, dated) if actions else {day: Decimal(1) for day in dated}
        adjusted = tuple((day, entry.prices[day] * factors.get(day, Decimal(1))) for day in dated)
        adjusted_by_instrument[instrument_id] = adjusted
        volume_by_instrument[instrument_id] = tuple(
            (day, entry.volumes[day]) for day in dated if day in entry.volumes
        )
        histories[str(instrument_id)] = (entry.instrument.asset_class.value, adjusted)
        returns_by_instrument[instrument_id] = calculate_returns(
            adjusted, effective_date, volume_observations=volume_by_instrument[instrument_id] or None
        )
        risk_by_instrument[instrument_id] = calculate_risk(adjusted, effective_date)

    asset_classes = {str(instrument_id): entry.instrument.asset_class.value for instrument_id, entry in series.items()}
    rs_ratings = equity_rs_rating(
        {str(instrument_id): adjusted_by_instrument[instrument_id] for instrument_id in series},
        asset_classes=asset_classes,
        effective_date=effective_date,
    )

    current_ids = {instrument_id for instrument_id, entry in series.items() if effective_date in entry.prices}

    momentum_records: list[dict[str, Any]] = []
    for instrument_id in current_ids:
        entry = series[instrument_id]
        returns = returns_by_instrument[instrument_id]
        risk = risk_by_instrument[instrument_id]
        components: dict[str, Decimal] = {}
        if returns.six_month is not None:
            components["six_month_performance"] = returns.six_month
        if returns.three_month is not None:
            components["three_month_performance"] = returns.three_month
        proximity = returns.proximity_to_52_week_high or risk.proximity_to_52_week_high
        if proximity is not None:
            components["proximity_to_52_week_high"] = proximity
        if risk.moving_average_50 is not None and risk.moving_average_200:
            components["trend_strength"] = risk.moving_average_50 / risk.moving_average_200 - Decimal(1)
        current_volume = entry.volumes.get(effective_date)
        if current_volume is not None and returns.weekly_average_volume:
            components["volume_confirmation"] = current_volume / returns.weekly_average_volume - Decimal(1)
        rating = rs_ratings.get(str(instrument_id))
        if rating is not None:
            components["weighted_12m_rs_percentile"] = (Decimal(rating) - Decimal(1)) / Decimal(98)
        momentum_records.append({
            "identifier": str(instrument_id),
            "asset_class": entry.instrument.asset_class.value,
            "effective_date": effective_date,
            "history_complete": returns.twelve_month is not None,
            "components": components,
        })
    scores: dict[str, MomentumScore] = momentum_scores(momentum_records)

    instruments_table: list[dict[str, Any]] = []
    metrics: list[dict[str, Any]] = []
    for instrument_id in sorted(current_ids, key=str):
        entry = series[instrument_id]
        instrument = entry.instrument
        instrument_key = str(instrument_id)
        instruments_table.append({
            "instrument_id": instrument_key,
            "provider": "nse",
            "provider_identifier": instrument.provider_identifier,
            "asset_class": instrument.asset_class.value,
            "symbol": instrument.symbol,
            "name": instrument.name,
            "active": True,
            "metadata": {
                "raw_series": instrument.raw_series,
                "raw_type": instrument.raw_type,
                "raw_close": str(entry.prices[effective_date]),
                "adjusted_close": str(dict(adjusted_by_instrument[instrument_id])[effective_date]),
            },
        })
        returns = returns_by_instrument[instrument_id]
        risk = risk_by_instrument[instrument_id]
        is_etf = instrument.asset_class.value == "etf"
        for metric, field_name in RETURN_METRICS:
            metrics.append(_metric_row(
                instrument_key, effective_date, metric, getattr(returns, field_name),
                formula_version=returns.formula_version,
                metadata={"unit": "percent", "coverage": float(returns.coverage)},
            ))
        metrics.append(_metric_row(
            instrument_key, effective_date, "volatility_1y", risk.annualized_volatility,
            formula_version=risk.formula_version, metadata={"unit": "percent", "coverage": float(risk.coverage)},
        ))
        metrics.append(_metric_row(
            instrument_key, effective_date, "max_drawdown_1y", risk.max_drawdown,
            formula_version=risk.formula_version, metadata={"unit": "percent", "coverage": float(risk.coverage)},
        ))
        metrics.append(_metric_row(
            instrument_key, effective_date, "average_volume_1w", returns.weekly_average_volume,
            formula_version=returns.formula_version, metadata={"unit": "shares"},
        ))
        rating = rs_ratings.get(instrument_key)
        metrics.append(_metric_row(
            instrument_key, effective_date, "rs_rating",
            Decimal(rating) if rating is not None else None,
            state="not_applicable" if is_etf else "auto",
            formula_version=rs_analytics.FORMULA_VERSION,
            metadata={"unit": "rank_1_99", "cohort": "nse-equity"},
        ))
        for metric, _ in BENCHMARK_WINDOWS:
            metrics.append(_metric_row(
                instrument_key, effective_date, metric, None,
                formula_version=rs_analytics.FORMULA_VERSION,
                metadata={"unit": "percent", "reason": "official benchmark mapping is unavailable until S03"},
            ))
        for metric in FUNDAMENTAL_METRICS:
            metrics.append(_metric_row(
                instrument_key, effective_date, f"fundamental_{metric}", None,
                state="not_applicable" if is_etf else "missing",
                formula_version="fundamentals-v1",
                metadata={"reason": "NSE filings normalization is not available until S03"},
            ))
    # Return/risk/RS/momentum values each consume a whole time series; the
    # persisted manifest (not a single day's artifact) is their authoritative
    # lineage, matching jobs/publish.py's AMFI build.
    for metric_entry in metrics:
        metric_entry["metadata"]["input_manifest_sha256"] = fingerprint
    if not instruments_table:
        raise PublicationInputError("no NSE instrument reported an observation on the published effective date")

    required_source_ids = sorted({item.source_id for item in eod_inputs})
    now = datetime.now(UTC).isoformat()
    dataset_id = f"nse-eq-etf-{effective_date.isoformat()}-{fingerprint[:12]}"
    candidate: dict[str, Any] = {
        "dataset_id": dataset_id,
        "effective_date": effective_date.isoformat(),
        "metadata": {
            "required_sources": required_source_ids,
            "generated_at": now,
            "asset_universe": "nse-equity-etf",
            "input_manifest_sha256": fingerprint,
        },
        "tables": {
            "sources": _sources_table([item.source_id for item in inputs], effective_date),
            "source_runs": [
                {
                    "run_id": f"{source_id}-{effective_date.isoformat()}",
                    "source_id": source_id,
                    "effective_date": effective_date.isoformat(),
                    "status": "complete",
                    "started_at": now,
                    "completed_at": now,
                }
                for source_id in required_source_ids
            ],
            "instruments": instruments_table,
            "latest_metrics": metrics,
            "corporate_actions": action_rows_for_lineage,
        },
    }
    return DatasetBuild(candidate, scores, {}, histories, tuple(warnings), input_manifest=manifest)


__all__ = ["FUNDAMENTAL_METRICS", "build_nse_candidate"]
