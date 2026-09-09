"""Normalize checkpointed raw artifacts, compute analytics, and publish to D1.

This module is the seam between the fetch/checkpoint jobs (Task 7) and the
versioned publication layer (``D1Publisher``).  It deliberately re-reads bodies
from the immutable :mod:`market_pipeline.storage.raw_store` rather than holding
fetched bytes in memory: the raw store is the auditable record, a daily run
needs the *previous* three years of artifacts to compute a twelve-month return,
and re-reading proves the stored artifact is the one the analytics saw.

Source coverage
---------------
``amfi-nav`` is wired end to end.  ``nse-eod`` and ``nse-filings-xbrl`` are not,
and cannot be under the current source-policy ruling: automation is disabled for
both, so :func:`assert_artifact_policy` refuses to admit their bytes to the raw
store at all (see ``docs/operations/source-policy.md``).  Artifacts from an
unwired source are reported as a warning instead of being silently dropped.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from hashlib import sha256
from typing import Any, Iterable, Mapping, Sequence

from market_pipeline.analytics.momentum import MomentumScore, momentum_scores
from market_pipeline.analytics.returns import ReturnMetrics, calculate_returns
from market_pipeline.analytics.risk import RiskMetrics, calculate_risk
from market_pipeline.analytics.rs import benchmark_rs
from market_pipeline.normalization.amfi import AmfiScheme, normalize_amfi_schemes
from market_pipeline.publication.input_manifest import InputManifestError, manifest_with_fingerprint
from market_pipeline.sources.amfi_nav import AmfiNavError, parse_amfi_nav
from market_pipeline.sources.registry import get_source_policy
from market_pipeline.storage.d1_publisher import D1Publisher, ReconciliationError, publish
from market_pipeline.storage.history_store import HistoryStore, HistoryStoreError

WIRED_SOURCES = ("amfi-nav",)
RETURN_METRICS = (
    ("return_1d", "one_day"),
    ("return_1w", "one_week"),
    ("return_1m", "one_month"),
    ("return_3m", "three_month"),
    ("return_6m", "six_month"),
    ("return_12m", "twelve_month"),
)
BENCHMARK_WINDOWS = (("benchmark_rs_3m", "three_month"), ("benchmark_rs_6m", "six_month"), ("benchmark_rs_12m", "twelve_month"))


class PublicationInputError(RuntimeError):
    """Raised when checkpointed artifacts cannot be normalized for publication."""


@dataclass(frozen=True)
class PublicationResult:
    promoted: bool
    dataset_id: str | None = None
    reason: str | None = None
    effective_date: date | None = None
    instrument_count: int = 0
    metric_count: int = 0
    history_objects: int = 0
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "promoted": self.promoted,
            "dataset_id": self.dataset_id,
            "reason": self.reason,
            "effective_date": self.effective_date.isoformat() if self.effective_date else None,
            "instrument_count": self.instrument_count,
            "metric_count": self.metric_count,
            "history_objects": self.history_objects,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class DatasetBuild:
    """Everything a publication run derived from the immutable raw artifacts."""

    candidate: dict[str, Any]
    momentum: dict[str, MomentumScore]
    source_artifact_ids: dict[str, str]
    histories: dict[str, tuple[str, tuple[tuple[date, Decimal], ...]]]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class _Artifact:
    source_id: str
    effective_date: date
    artifact_id: str
    checksum: str
    adapter_version: str
    raw_object_key: str
    body: bytes


@dataclass
class _SchemeSeries:
    scheme: AmfiScheme
    navs: dict[date, Decimal] = field(default_factory=dict)


def _checkpointed_artifacts(
    connection: sqlite3.Connection,
    raw_store: Any,
    source_ids: Sequence[str],
) -> tuple[list[_Artifact], list[str]]:
    """Read every checkpointed artifact body back out of the immutable store."""

    warnings: list[str] = []
    if not source_ids:
        return [], warnings
    placeholders = ",".join("?" for _ in source_ids)
    rows = connection.execute(
        "SELECT source_id, effective_date, artifact_id, checksum, object_key FROM backfill_checkpoints "
        f"WHERE source_id IN ({placeholders}) ORDER BY effective_date, source_id",
        tuple(source_ids),
    ).fetchall()
    artifacts: list[_Artifact] = []
    for source_id, effective, artifact_id, checksum, object_key in rows:
        if not object_key:
            raise PublicationInputError(f"{source_id}/{effective}: checkpoint has no object key; artifact was not retained")
        try:
            body = raw_store.get(str(object_key))
        except Exception as exc:  # noqa: BLE001 - raw-store errors make the candidate unusable
            raise PublicationInputError(f"{source_id}/{effective}: raw artifact is unreadable ({exc})") from exc
        if sha256(body).hexdigest() != str(checksum).lower():
            raise PublicationInputError(f"{source_id}/{effective}: stored artifact checksum does not match its checkpoint")
        artifacts.append(_Artifact(
            str(source_id), date.fromisoformat(str(effective)), str(artifact_id), str(checksum),
            "v1", str(object_key), body,
        ))
    return artifacts, warnings


def _normalize_amfi(
    artifacts: Sequence[_Artifact],
) -> tuple[dict[str, _SchemeSeries], dict[date, str], list[str]]:
    """Turn dated AMFI artifacts into per-scheme NAV series plus latest metadata."""

    warnings: list[str] = []
    series: dict[str, _SchemeSeries] = {}
    artifact_by_date: dict[date, str] = {}
    for artifact in sorted(artifacts, key=lambda item: item.effective_date):
        try:
            rows = parse_amfi_nav(artifact.body, expected_date=artifact.effective_date)
        except AmfiNavError as exc:
            raise PublicationInputError(f"amfi-nav/{artifact.effective_date.isoformat()}: {exc}") from exc
        batch = normalize_amfi_schemes(rows, effective_date=artifact.effective_date)
        artifact_by_date[artifact.effective_date] = artifact.artifact_id
        for scheme in batch.active():
            entry = series.get(scheme.scheme_code)
            if entry is None:
                entry = _SchemeSeries(scheme)
                series[scheme.scheme_code] = entry
            # The newest observation always wins for display metadata.
            entry.scheme = scheme
            entry.navs[scheme.nav_date] = scheme.nav
    return series, artifact_by_date, warnings


def _metric_row(
    instrument_id: str,
    effective: date,
    metric: str,
    value: Decimal | None,
    *,
    formula_version: str,
    source_artifact_id: str | None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "instrument_id": instrument_id,
        "effective_date": effective.isoformat(),
        "metric": metric,
        "value": float(value) if value is not None else None,
        "state": "present" if value is not None else "missing",
        "raw_value": str(value) if value is not None else None,
        "formula_version": formula_version,
        "source_artifact_id": source_artifact_id,
        "metadata": dict(metadata or {}),
    }


def _category_composite(
    returns_by_instrument: Mapping[str, ReturnMetrics],
    categories: Mapping[str, str],
    field_name: str,
) -> dict[str, Decimal]:
    """Equal-weighted composite return per AMFI category, used as the benchmark."""

    grouped: dict[str, list[Decimal]] = {}
    for instrument_id, metrics in returns_by_instrument.items():
        value = getattr(metrics, field_name)
        if value is None:
            continue
        grouped.setdefault(categories[instrument_id], []).append(value)
    return {
        category: sum(values, Decimal(0)) / Decimal(len(values))
        for category, values in grouped.items()
        if len(values) >= 2
    }


def _category_ranks(
    returns_by_instrument: Mapping[str, ReturnMetrics],
    categories: Mapping[str, str],
) -> dict[str, int]:
    """Rank 1..N by twelve-month return inside each category (1 is best)."""

    ranks: dict[str, int] = {}
    grouped: dict[str, list[tuple[str, Decimal]]] = {}
    for instrument_id, metrics in returns_by_instrument.items():
        if metrics.twelve_month is None:
            continue
        grouped.setdefault(categories[instrument_id], []).append((instrument_id, metrics.twelve_month))
    for members in grouped.values():
        for position, (instrument_id, _) in enumerate(
            sorted(members, key=lambda item: (-item[1], item[0])), start=1
        ):
            ranks[instrument_id] = position
    return ranks


def build_candidate(
    connection: sqlite3.Connection,
    raw_store: Any,
    source_ids: Sequence[str],
    *,
    effective_date: date,
) -> DatasetBuild:
    """Assemble a ``DatasetCandidate`` payload from immutable raw artifacts."""

    artifacts, warnings = _checkpointed_artifacts(connection, raw_store, source_ids)
    unwired = sorted({artifact.source_id for artifact in artifacts} - set(WIRED_SOURCES))
    warnings.extend(
        f"{source_id}: no normalization path is wired for this source; its artifacts were retained but not published"
        for source_id in unwired
    )
    usable = [
        artifact for artifact in artifacts
        if artifact.source_id == "amfi-nav" and artifact.effective_date <= effective_date
    ]
    if not usable:
        raise PublicationInputError("no normalizable artifacts are available for the requested effective date")
    versions = {
        "normalization": "amfi-v1",
        "analytics": "returns-v1+risk-v1+rs-v1+category-rank-v1+momentum-v1",
        "projection": "d1-v1",
    }
    try:
        manifest = manifest_with_fingerprint([
            {
                "source_id": artifact.source_id,
                "effective_date": artifact.effective_date.isoformat(),
                "artifact_id": artifact.artifact_id,
                "checksum": artifact.checksum.lower(),
                "adapter_version": artifact.adapter_version,
                "raw_object_key": artifact.raw_object_key,
            }
            for artifact in usable
        ], versions)
    except InputManifestError as exc:
        raise PublicationInputError(str(exc)) from exc
    fingerprint = str(manifest["fingerprint"])

    series, artifact_by_date, parse_warnings = _normalize_amfi(usable)
    warnings.extend(parse_warnings)
    if not series or not artifact_by_date:
        raise PublicationInputError("checkpointed artifacts produced no normalized instrument rows")
    effective = max(artifact_by_date)
    artifact_id = artifact_by_date[effective]

    instruments: list[dict[str, Any]] = []
    categories: dict[str, str] = {}
    returns_by_instrument: dict[str, ReturnMetrics] = {}
    risk_by_instrument: dict[str, RiskMetrics] = {}
    histories: dict[str, tuple[str, tuple[tuple[date, Decimal], ...]]] = {}
    for code in sorted(series):
        entry = series[code]
        if effective not in entry.navs:
            # A scheme absent from the published effective-date file is retained
            # by the normalizer but has no current observation to publish.
            continue
        instrument_id = str(entry.scheme.scheme_id)
        points = sorted(item for item in entry.navs.items() if item[0] <= effective)
        histories[instrument_id] = (entry.scheme.asset_class.value, tuple(points))
        returns_by_instrument[instrument_id] = calculate_returns(points, effective)
        risk_by_instrument[instrument_id] = calculate_risk(points, effective)
        categories[instrument_id] = entry.scheme.category or "uncategorized"
        instruments.append({
            "instrument_id": instrument_id,
            "provider": "amfi",
            "provider_identifier": entry.scheme.scheme_code,
            "asset_class": entry.scheme.asset_class.value,
            "symbol": entry.scheme.scheme_code,
            "name": entry.scheme.scheme_name,
            "active": entry.scheme.active,
            "metadata": {
                "amc": entry.scheme.amc,
                "category": entry.scheme.category,
                "plan": entry.scheme.plan,
                "option": entry.scheme.option,
                "nav": str(entry.navs[effective]),
            },
        })
    if not instruments:
        raise PublicationInputError("no instrument reported an observation on the published effective date")

    composites = {
        metric: _category_composite(returns_by_instrument, categories, field_name)
        for metric, field_name in BENCHMARK_WINDOWS
    }
    ranks = _category_ranks(returns_by_instrument, categories)

    metrics: list[dict[str, Any]] = []
    momentum_records: list[dict[str, Any]] = []
    for instrument in instruments:
        instrument_id = str(instrument["instrument_id"])
        returns = returns_by_instrument[instrument_id]
        risk = risk_by_instrument[instrument_id]
        category = categories[instrument_id]
        for metric, field_name in RETURN_METRICS:
            metrics.append(_metric_row(
                instrument_id, effective, metric, getattr(returns, field_name),
                formula_version=returns.formula_version, source_artifact_id=artifact_id,
                metadata={"unit": "percent", "coverage": float(returns.coverage)},
            ))
        metrics.append(_metric_row(
            instrument_id, effective, "volatility_1y", risk.annualized_volatility,
            formula_version=risk.formula_version, source_artifact_id=artifact_id,
            metadata={"unit": "percent", "coverage": float(risk.coverage)},
        ))
        metrics.append(_metric_row(
            instrument_id, effective, "max_drawdown_1y", risk.max_drawdown,
            formula_version=risk.formula_version, source_artifact_id=artifact_id,
            metadata={"unit": "percent", "coverage": float(risk.coverage)},
        ))
        metrics.append(_metric_row(
            instrument_id, effective, "category_rank",
            Decimal(ranks[instrument_id]) if instrument_id in ranks else None,
            formula_version="category-rank-v1", source_artifact_id=artifact_id,
            metadata={"unit": "count", "cohort": f"amfi-category:{category}"},
        ))
        for metric, field_name in BENCHMARK_WINDOWS:
            asset = getattr(returns, field_name)
            benchmark = composites[metric].get(category)
            value: Decimal | None = None
            if asset is not None and benchmark is not None and Decimal(1) + benchmark != 0:
                value = benchmark_rs(asset, benchmark)
            metrics.append(_metric_row(
                instrument_id, effective, metric, value,
                formula_version="rs-v1", source_artifact_id=artifact_id,
                metadata={
                    "unit": "percent",
                    "benchmark": f"amfi-category-composite:{category}",
                    "benchmark_return": str(benchmark) if benchmark is not None else None,
                },
            ))
        momentum_components: dict[str, Any] = {
            "three_month_return": returns.three_month,
            "six_month_return": returns.six_month,
            "twelve_month_return": returns.twelve_month,
            "volatility": risk.annualized_volatility,
            "max_drawdown": risk.max_drawdown,
        }
        if instrument_id in ranks:
            momentum_components["category_rank"] = Decimal(ranks[instrument_id])
        momentum_records.append({
            "identifier": instrument_id,
            "asset_class": "mutual_fund",
            "category": category,
            "effective_date": effective,
            "history_complete": returns.twelve_month is not None,
            "components": {key: value for key, value in momentum_components.items() if value is not None},
        })

    # Return/risk/momentum values consume a whole time series. The persisted
    # manifest, rather than the final daily file, is their authoritative
    # lineage; attaching only ``artifact_id`` here would be misleading.
    for metric in metrics:
        metric["source_artifact_id"] = None
        metric["metadata"]["input_manifest_sha256"] = fingerprint
    scores = momentum_scores(momentum_records)
    policy = get_source_policy("amfi-nav")
    now = datetime.now(UTC).isoformat()
    candidate = {
        "dataset_id": f"amfi-nav-{effective.isoformat()}-{fingerprint[:12]}",
        "effective_date": effective.isoformat(),
        "metadata": {
            "required_sources": ["amfi-nav"],
            "generated_at": now,
            "artifact_dates": len(artifact_by_date),
            "unwired_sources": unwired,
            "input_manifest": manifest,
            "input_manifest_sha256": fingerprint,
        },
        "tables": {
            "sources": [{
                "source_id": "amfi-nav",
                "source_url": policy.source_url,
                "terms_url": policy.terms_url,
                "status": "complete",
                "effective_date": effective.isoformat(),
                "adapter_version": "v1",
            }],
            "source_runs": [{
                "run_id": f"amfi-nav-{effective.isoformat()}",
                "source_id": "amfi-nav",
                "effective_date": effective.isoformat(),
                "status": "complete",
                "started_at": now,
                "completed_at": now,
            }],
            "instruments": instruments,
            "latest_metrics": metrics,
        },
    }
    artifact_ids: dict[str, str] = {}
    return DatasetBuild(candidate, scores, artifact_ids, histories, tuple(warnings))


def _write_history(
    history_store: HistoryStore,
    dataset_id: str,
    build: DatasetBuild,
) -> tuple[int, list[str]]:
    """Persist yearly Parquet history and the gzip chart object the API serves.

    History objects are immutable and content-addressed by (asset class,
    instrument, year), so re-publishing the same observations is a no-op and a
    changed observation surfaces as an explicit conflict rather than a silent
    overwrite.
    """

    written = 0
    warnings: list[str] = []
    for instrument_id, (asset_class, points) in sorted(build.histories.items()):
        records = [
            {"effective_date": point_date.isoformat(), "value": str(value)}
            for point_date, value in points
        ]
        try:
            written += len(history_store.write_history(asset_class, instrument_id, records))
            history_store.write_chart(
                dataset_id,
                instrument_id,
                {"points": [{"date": row["effective_date"], "value": float(row["value"])} for row in records]},
            )
            written += 1
        except (HistoryStoreError, OSError) as exc:
            warnings.append(f"{instrument_id}: history object was not written ({exc})")
    return written, warnings


def publish_checkpointed_dataset(
    connection: sqlite3.Connection,
    publisher: D1Publisher,
    raw_store: Any,
    source_ids: Sequence[str],
    *,
    effective_date: date,
    safe_to_promote: bool,
    blocking_reasons: Iterable[str] = (),
    history_store: HistoryStore | None = None,
    build: DatasetBuild | None = None,
) -> PublicationResult:
    """Build and atomically promote a dataset, or explain why it was not promoted.

    A run that failed the Task 13 pre-promotion gate never reaches ``stage``, so
    the last known-good dataset stays active. Any failure while building or
    staging is reported, never raised into the caller's exit path.
    """

    if not safe_to_promote:
        reasons = ", ".join(blocking_reasons) or "pre-promotion checks failed"
        return PublicationResult(promoted=False, reason=f"promotion blocked: {reasons}")
    if build is None:
        try:
            build = build_candidate(connection, raw_store, source_ids, effective_date=effective_date)
        except PublicationInputError as exc:
            return PublicationResult(promoted=False, reason=str(exc))
    candidate, scores, artifact_ids, warnings = (
        build.candidate, build.momentum, build.source_artifact_ids, list(build.warnings)
    )
    dataset_id = str(candidate["dataset_id"])
    already = connection.execute("SELECT status FROM datasets WHERE dataset_id = ?", (dataset_id,)).fetchone()
    if already is not None:
        # Re-running the same artifacts is a no-op, not a failure: the dataset
        # ID is a deterministic function of the published effective date and
        # its source artifact.
        return PublicationResult(
            promoted=str(already[0]) == "active",
            dataset_id=dataset_id,
            reason="dataset for these artifacts has already been published",
            warnings=tuple(warnings),
        )
    history_objects = 0
    if history_store is not None:
        history_objects, history_warnings = _write_history(history_store, dataset_id, build)
        warnings.extend(history_warnings)
        if history_warnings:
            # History/chart objects are what the Worker serves alongside the
            # active D1 rows. They must exist before the pointer can move;
            # otherwise readers could observe an active dataset with missing
            # charts. Immutable objects written before this failure are safe
            # orphans and make a retry deterministic.
            return PublicationResult(
                promoted=False,
                dataset_id=dataset_id,
                reason="history publication failed; active dataset was left untouched",
                warnings=tuple(warnings),
            )
    try:
        publish(candidate, publisher, momentum_scores=scores, source_artifact_ids=artifact_ids)
    except ReconciliationError as exc:
        return PublicationResult(promoted=False, dataset_id=dataset_id, reason=f"publication rejected: {exc}", warnings=tuple(warnings))
    tables = candidate["tables"]
    return PublicationResult(
        promoted=True,
        dataset_id=dataset_id,
        effective_date=date.fromisoformat(str(candidate["effective_date"])),
        instrument_count=len(tables["instruments"]),
        metric_count=int(
            connection.execute("SELECT COUNT(*) FROM latest_metrics WHERE dataset_id = ?", (dataset_id,)).fetchone()[0]
        ),
        history_objects=history_objects,
        warnings=tuple(warnings),
    )


__all__ = [
    "DatasetBuild",
    "PublicationInputError",
    "PublicationResult",
    "WIRED_SOURCES",
    "build_candidate",
    "publish_checkpointed_dataset",
]
