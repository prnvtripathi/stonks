"""Compose real NSE filings and official Nifty 500 benchmark observations
into fundamental-metric and benchmark-RS reference rows.

This is the filings/benchmark counterpart to S02's ``jobs/nse_candidate.py``:
that module always publishes ``fundamental_*``/``benchmark_rs_*`` fields as
honestly ``missing``/``not_applicable`` (see its own module docstring) because
no filings normalization or official benchmark feed was wired yet. S03 wires
both, without touching S02's price/return/risk/RS composition at all -- a
later task (S04, out of scope here) is responsible for merging this module's
``ReferenceRows`` into S02's ``DatasetBuild``.

Design notes
------------
* ``instrument_ids: Mapping[str, str]`` mirrors exactly the
  ``asset_classes`` dict S02's ``build_nse_candidate`` already builds
  internally (``{str(instrument_id): asset_class.value}``): it both bounds
  which instruments this run considers in scope, and says which asset class
  each one belongs to, so this module can tell a genuinely missing equity
  filing from an ETF/mutual-fund company metric that never applies.
* ``SourceInput`` (S01) only records one admitted artifact's lineage
  (source_id, checksum, object key, ...), not the full ``SourceArtifact``
  ``sources/nse_filings.py`` and ``sources/benchmark.py`` require for their
  own provenance re-validation. This module reconstructs an equivalent
  canonical ``SourceArtifact`` (real ``nse-filings-xbrl``/``nifty-500``
  source/terms URLs, the input's own recorded checksum/effective date) before
  delegating to those existing parsers -- the actual admission permission
  gate remains S01's ``admit()`` at admission time, called by whatever
  resolved these ``SourceInput`` values in the first place; this
  reconstruction only lets the parse-layer's own bytes-are-really-official
  check run against the checksum the pipeline already verified.
  ``SourceInput.role`` is an independent, caller-assigned dimension from
  ``source_id`` (per its own docstring), so ``_require_role_source_id`` looks
  the policy up *by the input's own recorded* ``source_id`` and rejects any
  role/source-ID combination other than the one official pairing --
  otherwise a wiring bug that attached the wrong role to an admitted
  artifact (e.g. a real ``nifty-500`` artifact mislabeled ``role=FILINGS``)
  would be silently treated as official data of the wrong kind.
* Benchmark RS here only ever runs when both the asset's own price
  observation and the mapped benchmark's observation exist on the *exact*
  same calendar date at both ends of the window -- never a nearest-available
  approximation. A single day's misalignment (on either side) means
  ``missing``, not a slightly-wrong number.
* Company fundamentals never mix values from two different filings/periods
  into one derived ratio: every published metric comes from exactly one
  chosen filed period, so there is no standalone/consolidated cross-mixing
  to guard against structurally.
* Benchmark mappings today only resolve ``identifier_type == "instrument"``
  entries (see ``_resolve_mapping``). Real-world MF benchmark mappings are
  conventionally category-based (a fund's *category* maps to a benchmark,
  not each individual scheme), so no mutual fund's benchmark RS can resolve
  to a present value under this implementation yet -- it correctly falls
  back to ``missing`` (never a fabricated substitute, per R03), but
  category-level mapping resolution (the practical MF case) is explicitly
  tracked future work, not something this task completes. This task
  completes F05 for instrument-level mappings only.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from market_pipeline.analytics import rs as rs_analytics
from market_pipeline.domain.models import SourceArtifact, SourcePolicy
from market_pipeline.jobs.publish import PublicationInputError
from market_pipeline.jobs.source_inputs import SourceInput, SourceInputRole
from market_pipeline.normalization import fundamentals as fundamentals_normalization
from market_pipeline.normalization.fundamentals import (
    FundamentalPeriod,
    normalize_financial_results,
)
from market_pipeline.normalization.nse import normalize_nse_rows, parse_nse_bhavcopy
from market_pipeline.publication.input_manifest import InputManifestError, manifest_with_fingerprint
from market_pipeline.sources.base import RawStore
from market_pipeline.sources.benchmark import (
    BENCHMARK_SOURCE_ID,
    BenchmarkMapping,
    BenchmarkObservation,
    BenchmarkProvenanceError,
    load_benchmark_mappings,
    parse_nifty500_observations,
)
from market_pipeline.sources.nse_filings import (
    NseFilingsProvenanceError,
    parse_financial_results_csv,
    parse_financial_results_xbrl,
)
from market_pipeline.sources.registry import SourcePolicyError, get_source_policy

FILINGS_SOURCE_ID = "nse-filings-xbrl"

# The one registered source ID this module trusts for each role. `SourceInput
# .role` is an independent, caller-assigned dimension from `source_id` (see
# `jobs/source_inputs.py`'s own docstring) -- nothing about admission itself
# guarantees a `role=FILINGS` input's `source_id` is really `nse-filings-xbrl`
# rather than, say, a benchmark artifact mislabeled by a wiring bug. Every
# canonical-artifact reconstruction below checks this explicitly, so a
# role/source-ID mismatch fails closed instead of being silently trusted.
_ROLE_CANONICAL_SOURCE_IDS: dict[SourceInputRole, str] = {
    SourceInputRole.FILINGS: FILINGS_SOURCE_ID,
    SourceInputRole.BENCHMARK_OBSERVATIONS: BENCHMARK_SOURCE_ID,
}

FUNDAMENTAL_METRICS: tuple[str, ...] = fundamentals_normalization.V1_FINANCIAL_FIELDS

FILINGS_NORMALIZATION_VERSION = "reference-filings-normalization-v1"
BENCHMARK_ANALYTICS_VERSION = "reference-benchmark-rs-v1"
REFERENCE_PROJECTION_VERSION = "reference-projection-v1"

# Field name -> approximate calendar-day window width, matching
# jobs.publish.BENCHMARK_WINDOWS' field names for the same metrics.
BENCHMARK_WINDOWS_DAYS: tuple[tuple[str, int], ...] = (
    ("benchmark_rs_3m", 91),
    ("benchmark_rs_6m", 182),
    ("benchmark_rs_12m", 365),
)

DEFAULT_MAPPINGS_PATH = Path(__file__).resolve().parents[3] / "content" / "benchmarks" / "mappings.json"

_EQUITY_ASSET_CLASS = "equity"


@dataclass(frozen=True)
class ReferenceRows:
    """Fundamental periods/metrics and benchmark observations/metrics, keyed
    to the same instrument-ID space as ``instrument_ids``.

    Every row is a plain, JSON-shaped dict (matching the rest of the
    pipeline's ``DatasetBuild.candidate["tables"]`` convention) so this can
    feed the same compact projection/export path unchanged.
    """

    fundamental_periods: tuple[dict[str, Any], ...]
    fundamental_metrics: list[dict[str, Any]]
    benchmark_observations: tuple[dict[str, Any], ...]
    benchmark_metrics: list[dict[str, Any]]
    sources: list[dict[str, Any]]
    warnings: tuple[str, ...] = ()
    input_manifest: Mapping[str, Any] = field(default_factory=dict)


def _metric_row(
    instrument_id: str,
    effective: date,
    metric: str,
    value: Decimal | None,
    *,
    state: str,
    formula_version: str,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "instrument_id": instrument_id,
        "effective_date": effective.isoformat(),
        "metric": metric,
        "value": float(value) if value is not None and state == "present" else None,
        "state": state,
        "raw_value": str(value) if value is not None and state == "present" else None,
        "formula_version": formula_version,
        "source_artifact_id": None,
        "metadata": dict(metadata or {}),
    }


def _looks_like_xbrl(body: bytes) -> bool:
    stripped = body.lstrip()
    return stripped.startswith(b"<?xml") or stripped.startswith(b"<")


def _require_role_source_id(source_input: SourceInput, role: SourceInputRole) -> SourcePolicy:
    """Verify ``source_input.source_id`` really is the one official source for ``role``.

    Looks the policy up *by the input's own recorded ``source_id``* (never by
    a fixed literal), then checks that the resolved policy is the one this
    role is allowed to trust. This closes the gap a fixed
    ``get_source_policy("nse-filings-xbrl")``/``get_source_policy("nifty-500")``
    call would otherwise leave open: if ``source_input.source_id`` were, say,
    an unregistered ID or another role's real official source, this raises
    instead of silently reconstructing a canonical artifact around it.
    """

    expected = _ROLE_CANONICAL_SOURCE_IDS[role]
    try:
        policy = get_source_policy(source_input.source_id)
    except SourcePolicyError as exc:
        raise PublicationInputError(
            f"{role.value} input cites source_id {source_input.source_id!r}, "
            f"which is not on the official source allowlist"
        ) from exc
    if policy.source_id != expected:
        raise PublicationInputError(
            f"{role.value} input has source_id {source_input.source_id!r}, but only "
            f"{expected!r} is treated as official {role.value} data"
        )
    return policy


def _canonical_filing_artifact(source_input: SourceInput) -> SourceArtifact:
    """Reconstruct the canonical ``nse-filings-xbrl`` artifact for provenance re-validation.

    See the module docstring: ``SourceInput`` never carries the full
    ``SourceArtifact`` (only lineage metadata); this rebuilds one shaped
    exactly like the real, permanently-fixed official source so
    ``sources/nse_filings.py``'s own checksum/URL provenance check still runs
    against the pipeline's already-verified checksum. ``_require_role_source_id``
    guarantees ``source_input.source_id`` really is ``nse-filings-xbrl`` before
    this reconstruction happens at all.
    """

    policy = _require_role_source_id(source_input, SourceInputRole.FILINGS)
    return SourceArtifact(
        source_id=policy.source_id,
        source_url=policy.source_url,
        retrieved_at=datetime.combine(source_input.loaded_date, datetime.min.time(), tzinfo=UTC),
        effective_date=source_input.loaded_date,
        checksum=source_input.checksum,
        adapter_version=source_input.adapter_version,
        terms_url=policy.terms_url,
        filename=f"{source_input.source_id}-{source_input.loaded_date.isoformat()}",
    )


def _canonical_benchmark_artifact(source_input: SourceInput) -> SourceArtifact:
    policy = _require_role_source_id(source_input, SourceInputRole.BENCHMARK_OBSERVATIONS)
    return SourceArtifact(
        source_id=policy.source_id,
        source_url=policy.source_url,
        retrieved_at=datetime.combine(source_input.loaded_date, datetime.min.time(), tzinfo=UTC),
        effective_date=source_input.loaded_date,
        checksum=source_input.checksum,
        adapter_version=source_input.adapter_version,
        terms_url=policy.terms_url,
        filename=f"{source_input.source_id}-{source_input.loaded_date.isoformat()}",
    )


def _parse_filings(
    filing_inputs: Sequence[SourceInput], raw_store: RawStore
) -> list[FundamentalPeriod]:
    periods: list[FundamentalPeriod] = []
    for source_input in filing_inputs:
        body = raw_store.get(source_input.object_key)
        artifact = _canonical_filing_artifact(source_input)
        try:
            if _looks_like_xbrl(body):
                parsed = parse_financial_results_xbrl(body, source_artifact=artifact)
            else:
                parsed = parse_financial_results_csv(body, source_artifact=artifact)
        except (NseFilingsProvenanceError, SourcePolicyError, ValueError) as exc:
            raise PublicationInputError(
                f"nse-filings/{source_input.loaded_date.isoformat()}: {exc}"
            ) from exc
        periods.extend(parsed)
    return periods


def _fundamental_period_dto(period: FundamentalPeriod, *, active: bool) -> dict[str, Any]:
    return {
        "period_id": str(period.id),
        "instrument_id": str(period.instrument_id),
        "period_end": period.period_end.isoformat(),
        "period_type": period.period_type,
        "filing_id": period.filing_id,
        "filed_at": period.filed_at.isoformat(),
        "restates_id": str(period.restates_id) if period.restates_id is not None else None,
        "supersedes_id": str(period.supersedes_id) if period.supersedes_id is not None else None,
        "source_artifact_id": str(period.source_artifact_id) if period.source_artifact_id is not None else None,
        "active": active,
    }


def _build_fundamental_rows(
    filing_inputs: Sequence[SourceInput],
    raw_store: RawStore,
    instrument_ids: Mapping[str, str],
    effective_date: date,
) -> tuple[tuple[dict[str, Any], ...], list[dict[str, Any]]]:
    raw_periods = _parse_filings(filing_inputs, raw_store)
    try:
        active_periods = normalize_financial_results(raw_periods)
    except ValueError as exc:
        raise PublicationInputError(f"nse-filings: {exc}") from exc
    active_ids = {period.id for period in active_periods}
    period_dtos = [_fundamental_period_dto(period, active=True) for period in active_periods]
    period_dtos.extend(
        _fundamental_period_dto(period, active=False)
        for period in raw_periods
        if period.id not in active_ids
    )

    by_instrument: dict[str, list[FundamentalPeriod]] = {}
    for period in active_periods:
        by_instrument.setdefault(str(period.instrument_id), []).append(period)

    metrics: list[dict[str, Any]] = []
    for instrument_id, asset_class in instrument_ids.items():
        is_equity = asset_class == _EQUITY_ASSET_CLASS
        candidates = [
            period
            for period in by_instrument.get(instrument_id, ())
            if period.filed_at.date() <= effective_date
        ]
        for field_name in FUNDAMENTAL_METRICS:
            if not is_equity:
                metrics.append(_metric_row(
                    instrument_id, effective_date, f"fundamental_{field_name}", None,
                    state="not_applicable", formula_version="fundamentals-v1",
                    metadata={"reason": f"fundamentals do not apply to asset class {asset_class!r}"},
                ))
                continue
            reporting = [period for period in candidates if field_name in period.metrics]
            if not reporting:
                metrics.append(_metric_row(
                    instrument_id, effective_date, f"fundamental_{field_name}", None,
                    state="missing", formula_version="fundamentals-v1",
                    metadata={"reason": "no filed period reports this field as of the effective date"},
                ))
                continue
            chosen = max(reporting, key=lambda period: (period.period_end, period.filed_at))
            value = chosen.metrics[field_name].value
            metrics.append(_metric_row(
                instrument_id, effective_date, f"fundamental_{field_name}", value,
                state="present", formula_version="fundamentals-v1",
                metadata={
                    "period_end": chosen.period_end.isoformat(),
                    "period_type": chosen.period_type,
                    "filing_id": chosen.filing_id,
                    "unit": chosen.metrics[field_name].unit,
                },
            ))
    return tuple(period_dtos), metrics


def _asset_price_series(
    eod_inputs: Sequence[SourceInput],
    raw_store: RawStore,
    instrument_ids: Mapping[str, str],
) -> dict[str, dict[date, Decimal]]:
    """Build a minimal raw close-price series per in-scope instrument.

    This reuses the same asset-class-agnostic parser/normalizer S02 uses
    (``normalization.nse``) purely to align an instrument's own observation
    dates with the mapped benchmark's; it is not a substitute for S02's own
    corporate-action-adjusted series, which this module never touches.
    """

    series: dict[str, dict[date, Decimal]] = {}
    for source_input in eod_inputs:
        body = raw_store.get(source_input.object_key)
        parsed_rows = parse_nse_bhavcopy(body)
        batch = normalize_nse_rows(parsed_rows, report_date=source_input.loaded_date)
        rows_by_symbol = {row.symbol: row for row in parsed_rows}
        for instrument in batch.instruments:
            instrument_id = str(instrument.instrument_id)
            if instrument_id not in instrument_ids:
                continue
            row = rows_by_symbol.get(instrument.symbol or "")
            if row is None or row.close is None or row.report_date != source_input.loaded_date:
                continue
            series.setdefault(instrument_id, {})[source_input.loaded_date] = Decimal(str(row.close))
    return series


def _resolve_mapping(
    instrument_id: str, mappings: Sequence[BenchmarkMapping], effective_date: date
) -> BenchmarkMapping | None:
    """Resolve an instrument-level reviewed mapping, valid on ``effective_date``.

    ``instrument_ids`` (this module's own input) carries no category label,
    so a ``"category"``-typed mapping entry (the schema supports curating one
    for an MF category, e.g. "large-cap" -> a broad-market index) cannot be
    resolved here yet; it is accepted and validated by
    ``load_benchmark_mappings`` for forward compatibility, but every category
    mapping is currently unreachable and every instrument without its own
    ``"instrument"``-typed entry stays honestly ``missing`` -- never a
    category-composite substitute (see R03).
    """

    for mapping in mappings:
        if (
            mapping.identifier_type == "instrument"
            and mapping.identifier == instrument_id
            and mapping.covers(effective_date)
        ):
            return mapping
    return None


def _rs_metric(
    asset_prices: Mapping[date, Decimal],
    benchmark_prices: Mapping[date, Decimal],
    effective_date: date,
    window_days: int,
) -> tuple[Decimal | None, date, date, str | None]:
    end = effective_date
    start = effective_date - timedelta(days=window_days)
    if end not in asset_prices or end not in benchmark_prices:
        return None, start, end, "no aligned asset/benchmark observation on the RS window end date"
    if start not in asset_prices or start not in benchmark_prices:
        return None, start, end, "no aligned asset/benchmark observation on the RS window start date"
    asset_start, asset_end = asset_prices[start], asset_prices[end]
    benchmark_start, benchmark_end = benchmark_prices[start], benchmark_prices[end]
    if asset_start == 0 or benchmark_start == 0:
        return None, start, end, "window start observation is zero"
    asset_return = asset_end / asset_start - Decimal(1)
    benchmark_return = benchmark_end / benchmark_start - Decimal(1)
    try:
        value = rs_analytics.benchmark_rs(asset_return, benchmark_return)
    except ValueError:
        return None, start, end, "benchmark return of -100 percent makes the ratio undefined"
    return value, start, end, None


def _build_benchmark_rows(
    benchmark_inputs: Sequence[SourceInput],
    raw_store: RawStore,
    instrument_ids: Mapping[str, str],
    asset_prices: Mapping[str, Mapping[date, Decimal]],
    mappings: Sequence[BenchmarkMapping],
    effective_date: date,
) -> tuple[tuple[dict[str, Any], ...], list[dict[str, Any]]]:
    observations: list[BenchmarkObservation] = []
    for source_input in benchmark_inputs:
        body = raw_store.get(source_input.object_key)
        artifact = _canonical_benchmark_artifact(source_input)
        try:
            observations.extend(parse_nifty500_observations(body, source_artifact=artifact))
        except (BenchmarkProvenanceError, SourcePolicyError, ValueError) as exc:
            raise PublicationInputError(
                f"nifty-500/{source_input.loaded_date.isoformat()}: {exc}"
            ) from exc

    by_benchmark: dict[str, dict[date, Decimal]] = {}
    for observation in observations:
        by_benchmark.setdefault(observation.benchmark_id, {})[observation.observation_date] = observation.close

    observation_dtos = tuple(
        {
            "benchmark_id": observation.benchmark_id,
            "observation_date": observation.observation_date.isoformat(),
            "close": str(observation.close),
            "source_artifact_id": (
                str(observation.source_artifact_id) if observation.source_artifact_id is not None else None
            ),
        }
        for observation in sorted(observations, key=lambda item: (item.benchmark_id, item.observation_date))
    )

    metrics: list[dict[str, Any]] = []
    for instrument_id in instrument_ids:
        mapping = _resolve_mapping(instrument_id, mappings, effective_date)
        instrument_prices = asset_prices.get(instrument_id, {})
        for metric, window_days in BENCHMARK_WINDOWS_DAYS:
            if mapping is None:
                metrics.append(_metric_row(
                    instrument_id, effective_date, metric, None,
                    state="missing", formula_version=BENCHMARK_ANALYTICS_VERSION,
                    metadata={"unit": "percent", "reason": "no reviewed official benchmark mapping is available"},
                ))
                continue
            benchmark_prices = by_benchmark.get(mapping.benchmark_id, {})
            value, start, end, reason = _rs_metric(instrument_prices, benchmark_prices, effective_date, window_days)
            metrics.append(_metric_row(
                instrument_id, effective_date, metric, value,
                state="present" if value is not None else "missing",
                formula_version=BENCHMARK_ANALYTICS_VERSION,
                metadata={
                    "unit": "percent",
                    "benchmark_id": mapping.benchmark_id,
                    "mapping_source_reference": mapping.source_reference,
                    "rs_asset_start": start.isoformat(),
                    "rs_asset_end": end.isoformat(),
                    "rs_benchmark_start": start.isoformat(),
                    "rs_benchmark_end": end.isoformat(),
                    **({"reason": reason} if reason is not None else {}),
                },
            ))
    return observation_dtos, metrics


def _sources_table(source_ids: Sequence[str], effective_date: date) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for source_id in sorted(set(source_ids)):
        try:
            policy = get_source_policy(source_id)
            source_url, terms_url = policy.source_url, policy.terms_url
        except SourcePolicyError:
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


def build_reference_rows(
    inputs: Sequence[SourceInput],
    raw_store: RawStore,
    instrument_ids: Mapping[str, str],
    effective_date: date,
    *,
    mappings_path: str | Path = DEFAULT_MAPPINGS_PATH,
) -> ReferenceRows:
    """Compose real NSE filings and Nifty 500 benchmark observations.

    ``instrument_ids`` maps ``str(instrument_id) -> asset_class`` (equity,
    etf, mutual_fund, ...), exactly S02's own internal ``asset_classes``
    shape -- this bounds the instrument-ID space and lets fundamentals
    distinguish a genuinely missing equity filing from a metric that never
    applies to a non-equity asset class.

    ``mappings_path`` is an explicit, optional override of the reviewed
    ``content/benchmarks/mappings.json`` file (default), so tests can supply
    an isolated fixture mapping without mutating the shared reviewed file.
    """

    inputs = list(inputs)
    warnings: list[str] = []
    filing_inputs = sorted(
        (item for item in inputs if item.role is SourceInputRole.FILINGS),
        key=lambda item: item.loaded_date,
    )
    benchmark_inputs = sorted(
        (item for item in inputs if item.role is SourceInputRole.BENCHMARK_OBSERVATIONS),
        key=lambda item: item.loaded_date,
    )
    eod_inputs = [item for item in inputs if item.role is SourceInputRole.EOD_OBSERVATIONS]

    if not filing_inputs:
        warnings.append("no NSE filings were supplied for this run; every equity fundamental is missing")
    if not benchmark_inputs:
        warnings.append("no Nifty 500 benchmark observations were supplied for this run")

    try:
        mappings = load_benchmark_mappings(mappings_path)
    except Exception as exc:  # noqa: BLE001 - malformed reviewed mapping file blocks publication
        raise PublicationInputError(f"benchmark mapping file is invalid: {exc}") from exc
    if not mappings:
        warnings.append("the reviewed benchmark mapping file is empty; every benchmark RS is missing")

    period_dtos, fundamental_metrics = _build_fundamental_rows(
        filing_inputs, raw_store, instrument_ids, effective_date
    )
    asset_prices = _asset_price_series(eod_inputs, raw_store, instrument_ids)
    observation_dtos, benchmark_metrics = _build_benchmark_rows(
        benchmark_inputs, raw_store, instrument_ids, asset_prices, mappings, effective_date
    )

    # Every input that materially affects a published value must be part of
    # the fingerprint (R02) -- EOD observations only exist here to align the
    # RS asset endpoint, but they change `benchmark_rs_*` values just as much
    # as the filings/benchmark artifacts do, so they are included too.
    manifest_inputs = [
        {
            "source_id": item.source_id,
            "effective_date": item.loaded_date.isoformat(),
            "artifact_id": str(item.artifact_id),
            "checksum": item.checksum.lower(),
            "adapter_version": item.adapter_version,
            "raw_object_key": item.object_key,
        }
        for item in (*filing_inputs, *benchmark_inputs, *eod_inputs)
    ]
    versions = {
        "normalization": FILINGS_NORMALIZATION_VERSION,
        "analytics": "|".join((BENCHMARK_ANALYTICS_VERSION, rs_analytics.FORMULA_VERSION)),
        "projection": REFERENCE_PROJECTION_VERSION,
    }
    input_manifest: Mapping[str, Any] = {}
    if manifest_inputs:
        try:
            input_manifest = manifest_with_fingerprint(manifest_inputs, versions)
        except InputManifestError as exc:
            raise PublicationInputError(str(exc)) from exc

    sources = _sources_table(
        [item.source_id for item in (*filing_inputs, *benchmark_inputs, *eod_inputs)], effective_date
    )

    return ReferenceRows(
        fundamental_periods=period_dtos,
        fundamental_metrics=fundamental_metrics,
        benchmark_observations=observation_dtos,
        benchmark_metrics=benchmark_metrics,
        sources=sources,
        warnings=tuple(warnings),
        input_manifest=input_manifest,
    )


__all__ = [
    "BENCHMARK_ANALYTICS_VERSION",
    "BENCHMARK_WINDOWS_DAYS",
    "DEFAULT_MAPPINGS_PATH",
    "FILINGS_NORMALIZATION_VERSION",
    "FUNDAMENTAL_METRICS",
    "REFERENCE_PROJECTION_VERSION",
    "ReferenceRows",
    "build_reference_rows",
]
