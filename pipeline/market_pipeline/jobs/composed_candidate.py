"""S04: compose AMFI + S02's NSE equity/ETF build + S03's filings/benchmark
reference rows into ONE ``DatasetBuild``, with independently-persisted
per-source health/acquisition status.

Why a separate module (merge strategy)
---------------------------------------
``jobs/publish.py``'s ``build_candidate`` (AMFI), ``jobs/nse_candidate.py``'s
``build_nse_candidate`` (NSE equity/ETF), and ``jobs/reference_candidate.py``'s
``build_reference_rows`` (filings/benchmark) were each deliberately built as
separate, independently-testable entry points (S02/S03's own module
docstrings say so explicitly): none of them merges into one dataset. This
module is where that merge finally happens, for real multi-source
publication:

* AMFI's mutual-fund instruments and NSE's equity/ETF instruments occupy
  disjoint instrument-ID spaces by construction (``Instrument.instrument_id``
  is a UUID5 of ``provider|identifier|asset_class``, and ``provider`` is
  literally ``"amfi"`` vs ``"nse"``), so a plain union of both builds'
  ``tables.instruments``/``histories``/``momentum`` is safe -- but this
  module still checks for a genuine collision defensively rather than
  trusting that invariant blindly (:class:`CompositionError`).
* S03's ``ReferenceRows.fundamental_metrics``/``benchmark_metrics`` are keyed
  to the exact same ``(instrument_id, metric)`` shape S02's NSE build already
  publishes as honest ``missing``/``not_applicable`` placeholders for
  ``fundamental_*``/``benchmark_rs_*``. Composing therefore means *replacing*
  those specific placeholder rows with S03's real computed rows -- never
  appending a second, conflicting row for the same instrument/metric/date.
* ``ReferenceRows.fundamental_periods`` feeds the existing
  ``fundamental_periods`` D1 table (already in ``D1Publisher._TABLES``, see
  ``storage/d1_publisher.py``); the NSE build's own ``corporate_actions`` rows
  feed the existing ``corporate_actions`` table the same way. Both tables
  already exist in the compact schema R07 exports -- this module is simply
  the first caller to route S02/S03 output into them for a *composed*
  dataset (S02/S03's own single-component test suites only ever checked
  those rows on the *component* ``DatasetBuild``).
* ``ReferenceRows.benchmark_observations`` has **no** matching D1 table
  (confirmed against ``D1Publisher._TABLES``/``db/migrations/0001-0008``) --
  it is lineage-only today and is intentionally dropped rather than smuggled
  into an unrelated table; this is a documented, known scope limit, not an
  oversight (see the module's ``ComposedDatasetBuild`` docstring and the S04
  task report).
* Filings/benchmark sources are never added to ``required_sources``: a
  missing/delayed filing or benchmark feed must not block promotion of an
  otherwise-complete equity/ETF/mutual-fund dataset (the reconciliation
  module's own stated invariant that one source's delay never masks -- or is
  masked by -- another's).
* One combined input manifest is built from the union of every component's
  own already-canonical ``inputs`` list (duplicates -- e.g. NSE EOD inputs
  that both ``build_nse_candidate`` and ``build_reference_rows`` consumed --
  are de-duplicated by exact content match before fingerprinting, since
  ``publication.input_manifest.canonical_inputs`` treats *any* repeated
  ``(source_id, effective_date)`` pair as an ambiguous revision regardless of
  whether the two entries are identical).

Per-source health persistence
------------------------------
:class:`SourceStatus` is a thin, persisted composition of R01's own
``SourceFreshness`` (complete/delayed/failed/not_expected) and
``CandidateCoverage`` -- this module never reimplements that state machine,
only calls the existing ``validation.reconcile.reconcile`` function and reads
its ``source_freshness``/``candidate_coverage`` back out.
:func:`run_scheduled_refresh` is the daily-execution entry point that takes a
source schedule with an independent expected date and acquisition mode per
source (:class:`SourceScheduleEntry`), and persists the resulting per-source
:class:`SourceStatus` values separately from whether the run actually got
promoted -- so a failed refresh is visible without ever publishing partial
data, and a day with nothing scheduled reports ``"skipped"``, never a false
completed refresh.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Mapping, Sequence

from market_pipeline.jobs.nse_candidate import build_nse_candidate
from market_pipeline.jobs.publish import (
    DatasetBuild,
    PublicationInputError,
    PublicationResult,
    build_candidate,
    publish_checkpointed_dataset,
)
from market_pipeline.jobs.reference_candidate import (
    DEFAULT_MAPPINGS_PATH,
    ReferenceRows,
    build_reference_rows,
)
from market_pipeline.jobs.source_inputs import AcquisitionMode, SourceInput
from market_pipeline.monitoring.budgets import budget_report
from market_pipeline.publication.input_manifest import InputManifestError, manifest_with_fingerprint
from market_pipeline.storage.d1_publisher import D1Publisher
from market_pipeline.storage.history_store import HistoryStore
from market_pipeline.validation.reconcile import (
    CandidateCoverage,
    SourceObservation,
    evaluate_pre_promotion,
    reconcile,
)

COMPOSITION_VERSION = "composed-dataset-v1"

# Filings/benchmark sources are supplementary reference data: a delayed or
# missing feed must never block promotion of an otherwise-complete
# equity/ETF/mutual-fund dataset. Only these two component builds' own
# `required_sources` are ever merged into the composed candidate's own
# `required_sources`.
_REQUIRED_SOURCE_COMPONENTS = ("amfi", "nse")


class CompositionError(PublicationInputError):
    """Raised when component builds cannot be safely merged into one dataset."""


@dataclass(frozen=True)
class ComposedDatasetBuild(DatasetBuild):
    """A ``DatasetBuild`` composed from one or more component builds.

    ``asset_classes`` is the real, *observed* set of asset classes actually
    published in ``candidate["tables"]["instruments"]`` -- never a hardcoded
    literal -- so a source-selection bug that silently drops NSE or AMFI from
    the composed run shows up directly as a shrunken set instead of passing
    unnoticed (the plan's own oracle:
    ``mixed_candidate.asset_classes == {'equity', 'etf', 'mutual_fund'}``).

    ``component_dataset_ids`` names which component builds (by label:
    ``"amfi"``, ``"nse"``) actually contributed to this composed dataset, for
    operator-facing traceability.
    """

    asset_classes: frozenset[str] = field(default_factory=frozenset)
    component_dataset_ids: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class SourceStatus:
    """Persisted per-source health/acquisition status.

    A thin composition of R01's own :class:`~market_pipeline.validation.reconcile.SourceFreshness`
    (``state`` -- complete/delayed/failed/not_expected) and
    :class:`~market_pipeline.validation.reconcile.CandidateCoverage`
    (``coverage``) -- this dataclass never reimplements that state machine.

    Dates are recorded per source, independently: one source's delay never
    overwrites, or is masked by, another source's ``loaded_date``.
    """

    source_id: str
    category: str
    acquisition_mode: str
    expected_date: date | None
    loaded_date: date | None
    state: str
    detail: str = ""
    coverage: CandidateCoverage | None = None
    max_delay_days: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "category": self.category,
            "acquisition_mode": self.acquisition_mode,
            "expected_date": self.expected_date.isoformat() if self.expected_date else None,
            "loaded_date": self.loaded_date.isoformat() if self.loaded_date else None,
            "state": self.state,
            "detail": self.detail,
            "coverage": self.coverage.as_dict() if self.coverage is not None else None,
            "max_delay_days": self.max_delay_days,
        }


@dataclass(frozen=True)
class SourceScheduleEntry:
    """One source's independent expected date and acquisition mode for a run.

    ``expected_date`` is ``None`` on a non-publication day for this specific
    source (e.g. a quarterly filings source is not expected on an ordinary
    trading weekday) -- this is how non-publication days are resolved via an
    explicit per-source calendar rather than a single global assumption.
    """

    source_id: str
    category: str
    acquisition_mode: AcquisitionMode
    expected_date: date | None
    max_delay_days: int = 0


def _dedupe_rows(rows: Sequence[Mapping[str, Any]], key_fields: tuple[str, ...]) -> list[dict[str, Any]]:
    seen: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        materialized = dict(row)
        key = tuple(materialized.get(field_name) for field_name in key_fields)
        seen.setdefault(key, materialized)
    return list(seen.values())


def _merge_instrument_tables(builds: Sequence[DatasetBuild]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for build in builds:
        for row in build.candidate["tables"]["instruments"]:
            instrument_id = str(row["instrument_id"])
            if instrument_id in merged:
                raise CompositionError(
                    f"instrument_id collision across composed builds: {instrument_id!r} "
                    "is published by more than one component"
                )
            merged[instrument_id] = dict(row)
    return list(merged.values())


def _merge_metric_tables(
    builds: Sequence[DatasetBuild], reference: ReferenceRows | None
) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for build in builds:
        for row in build.candidate["tables"]["latest_metrics"]:
            key = (str(row["instrument_id"]), str(row["metric"]))
            copied = dict(row)
            copied["metadata"] = dict(copied.get("metadata") or {})
            merged[key] = copied
    if reference is not None:
        # Replaces S02's honest missing/not_applicable placeholder for this
        # exact (instrument, metric) with S03's real computed row -- never
        # appended alongside it, which would publish two rows for the same
        # instrument/metric/effective_date.
        for row in (*reference.fundamental_metrics, *reference.benchmark_metrics):
            key = (str(row["instrument_id"]), str(row["metric"]))
            copied = dict(row)
            copied["metadata"] = dict(copied.get("metadata") or {})
            merged[key] = copied
    return list(merged.values())


def _merge_mapping_no_collision(mappings: Sequence[Mapping[str, Any]], what: str) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for mapping in mappings:
        for key, value in mapping.items():
            if key in merged and merged[key] != value:
                raise CompositionError(f"{what} collision across composed builds for key {key!r}")
            merged[key] = value
    return merged


def _combine_manifests(components: Sequence[tuple[str, Mapping[str, Any]]]) -> dict[str, Any]:
    """Combine each component's already-canonical manifest into one fingerprint.

    Duplicate identical input entries (e.g. NSE EOD inputs consumed by both
    ``build_nse_candidate`` and ``build_reference_rows``) are de-duplicated by
    exact content match before fingerprinting: ``canonical_inputs`` treats
    *any* repeated ``(source_id, effective_date)`` pair as an ambiguous
    revision, regardless of whether the two entries are identical.
    """

    seen_inputs: dict[tuple[Any, ...], dict[str, Any]] = {}
    version_parts: dict[str, list[str]] = {"normalization": [], "analytics": [], "projection": []}
    for label, manifest in components:
        for entry in manifest.get("inputs", ()):
            key = tuple(
                entry.get(field_name)
                for field_name in ("source_id", "effective_date", "artifact_id", "checksum", "adapter_version", "raw_object_key")
            )
            existing = seen_inputs.get(key)
            materialized = dict(entry)
            if existing is not None and existing != materialized:
                raise CompositionError(
                    f"conflicting manifest input entries across composed builds for {key[0]!r}/{key[1]!r}"
                )
            seen_inputs[key] = materialized
        versions = manifest.get("versions", {})
        for field_name in ("normalization", "analytics", "projection"):
            value = versions.get(field_name)
            if value:
                version_parts[field_name].append(f"{label}:{value}")
    versions_merged = {
        field_name: "|".join(parts) if parts else "none"
        for field_name, parts in version_parts.items()
    }
    versions_merged["composition"] = COMPOSITION_VERSION
    try:
        return manifest_with_fingerprint(list(seen_inputs.values()), versions_merged)
    except InputManifestError as exc:
        raise CompositionError(str(exc)) from exc


def compose_datasets(
    *,
    amfi: DatasetBuild | None = None,
    nse: DatasetBuild | None = None,
    reference: ReferenceRows | None = None,
    effective_date: date,
) -> ComposedDatasetBuild:
    """Merge component builds into ONE composed ``DatasetBuild``.

    ``amfi`` is ``jobs/publish.py``'s AMFI-only build; ``nse`` is
    ``jobs/nse_candidate.py``'s equity/ETF build; ``reference`` is
    ``jobs/reference_candidate.py``'s filings/benchmark rows (requires
    ``nse`` to be present, since it is keyed to that build's instrument-ID
    space). At least one of ``amfi``/``nse`` is required.

    When only ``amfi`` is supplied, this is a pure passthrough (the AMFI
    candidate's own tables/histories/momentum/manifest are reused verbatim,
    not rebuilt), preserving the exact, already-hardened AMFI-only shape
    byte-for-byte -- this is what keeps R01-R13's own AMFI-only test suite
    passing unchanged for that path.
    """

    if amfi is None and nse is None:
        raise CompositionError("composed dataset requires at least one of the amfi or nse builds")
    if reference is not None and nse is None:
        raise CompositionError(
            "reference rows require the NSE build's instrument-ID space and cannot be composed alone"
        )

    if nse is None and reference is None:
        assert amfi is not None  # for mypy: the two guards above exhaust the alternative
        instruments = amfi.candidate["tables"]["instruments"]
        asset_classes = frozenset(str(row["asset_class"]) for row in instruments)
        return ComposedDatasetBuild(
            candidate=amfi.candidate,
            momentum=amfi.momentum,
            source_artifact_ids=amfi.source_artifact_ids,
            histories=amfi.histories,
            warnings=amfi.warnings,
            input_manifest=amfi.input_manifest,
            asset_classes=asset_classes,
            component_dataset_ids={"amfi": str(amfi.candidate["dataset_id"])},
        )

    base_builds = [build for build in (amfi, nse) if build is not None]

    instruments = _merge_instrument_tables(base_builds)
    metrics = _merge_metric_tables(base_builds, reference)

    sources_rows_all: list[Mapping[str, Any]] = []
    source_runs_rows_all: list[Mapping[str, Any]] = []
    for build in base_builds:
        sources_rows_all.extend(build.candidate["tables"]["sources"])
        source_runs_rows_all.extend(build.candidate["tables"]["source_runs"])
    if reference is not None:
        sources_rows_all.extend(reference.sources)
    sources_rows = _dedupe_rows(sources_rows_all, ("source_id",))
    source_runs_rows = _dedupe_rows(source_runs_rows_all, ("source_id", "effective_date"))

    corporate_actions_rows = list(nse.candidate["tables"].get("corporate_actions", ())) if nse is not None else []
    fundamental_period_rows = list(reference.fundamental_periods) if reference is not None else []

    histories = _merge_mapping_no_collision([build.histories for build in base_builds], "history series")
    momentum = _merge_mapping_no_collision([build.momentum for build in base_builds], "momentum score")
    source_artifact_ids = _merge_mapping_no_collision(
        [build.source_artifact_ids for build in base_builds], "source artifact id"
    )

    warnings: list[str] = []
    for build in base_builds:
        warnings.extend(build.warnings)
    if reference is not None:
        warnings.extend(reference.warnings)

    # Filings/benchmark sources are supplementary: never added to
    # required_sources, so a missing/delayed reference feed cannot block
    # promotion of an otherwise-complete equity/ETF/mutual-fund dataset.
    #
    # A component build's own `required_sources` is only carried into the
    # COMPOSED candidate's `required_sources` when that component's own
    # candidate effective_date matches the composed effective_date. D1's
    # per-dataset `effective_date` is singular, but this pipeline's sources
    # are honestly independent: a source that is delayed (its build's own
    # effective_date is an older date than the day being composed, because
    # `build_candidate` reuses its last available artifact rather than
    # inventing a current one) is still retained -- its instruments, metrics,
    # and history all survive the merge below -- but is not treated as a
    # hard, current requirement for *this* composed dataset. Without this
    # distinction, `D1Publisher.promote()`'s own required-source-run
    # completeness check (which demands an exact effective_date match, with
    # no delayed/complete distinction of its own) would block every
    # otherwise-healthy composed publish merely because one source was
    # honestly stale -- exactly the "one source's delay masks another
    # source's success" failure mode this task exists to prevent.
    required_sources = sorted({
        str(source_id)
        for build in base_builds
        if str(build.candidate["effective_date"]) == effective_date.isoformat()
        for source_id in (build.candidate["metadata"].get("required_sources") or [])
    })

    manifest_components: list[tuple[str, Mapping[str, Any]]] = []
    if amfi is not None:
        manifest_components.append(("amfi", amfi.input_manifest))
    if nse is not None:
        manifest_components.append(("nse", nse.input_manifest))
    if reference is not None and reference.input_manifest:
        manifest_components.append(("reference", reference.input_manifest))
    combined_manifest = _combine_manifests(manifest_components)
    fingerprint = str(combined_manifest["fingerprint"])

    for row in metrics:
        # Composed lineage always wins: a metric's authoritative manifest
        # hash is now the composed one, not whichever single component last
        # touched it.
        row["metadata"]["input_manifest_sha256"] = fingerprint

    component_dataset_ids: dict[str, str] = {}
    if amfi is not None:
        component_dataset_ids["amfi"] = str(amfi.candidate["dataset_id"])
    if nse is not None:
        component_dataset_ids["nse"] = str(nse.candidate["dataset_id"])

    candidate: dict[str, Any] = {
        "dataset_id": f"composed-{effective_date.isoformat()}-{fingerprint[:12]}",
        "effective_date": effective_date.isoformat(),
        "metadata": {
            "required_sources": required_sources,
            "asset_universe": "composed",
            "input_manifest_sha256": fingerprint,
            "component_dataset_ids": component_dataset_ids,
        },
        "tables": {
            "sources": sources_rows,
            "source_runs": source_runs_rows,
            "instruments": instruments,
            "latest_metrics": metrics,
            **({"corporate_actions": corporate_actions_rows} if corporate_actions_rows else {}),
            **({"fundamental_periods": fundamental_period_rows} if fundamental_period_rows else {}),
        },
    }
    asset_classes = frozenset(str(row["asset_class"]) for row in instruments)
    return ComposedDatasetBuild(
        candidate=candidate,
        momentum=momentum,
        source_artifact_ids=source_artifact_ids,
        histories=histories,
        warnings=tuple(warnings),
        input_manifest=combined_manifest,
        asset_classes=asset_classes,
        component_dataset_ids=component_dataset_ids,
    )


def build_composed_candidate(
    connection: Any,
    raw_store: Any,
    *,
    amfi_source_ids: Sequence[str] = (),
    nse_inputs: Sequence[SourceInput] = (),
    reference_inputs: Sequence[SourceInput] = (),
    effective_date: date,
    mappings_path: Any = DEFAULT_MAPPINGS_PATH,
) -> ComposedDatasetBuild:
    """Build whichever component candidates were requested and compose them.

    ``amfi_source_ids`` (possibly empty) drives ``jobs/publish.py``'s
    existing checkpoint-table-based AMFI path unchanged; ``nse_inputs``/
    ``reference_inputs`` (possibly empty) drive S01-admitted ``SourceInput``
    sequences through S02/S03. Requesting only ``amfi_source_ids`` reproduces
    the exact pre-S04 AMFI-only behavior (see :func:`compose_datasets`).
    """

    if not amfi_source_ids and not nse_inputs:
        raise CompositionError("composed candidate requires at least one of amfi_source_ids or nse_inputs")

    amfi_build = (
        build_candidate(connection, raw_store, list(amfi_source_ids), effective_date=effective_date)
        if amfi_source_ids
        else None
    )
    nse_build = build_nse_candidate(list(nse_inputs), raw_store, effective_date) if nse_inputs else None
    reference_rows: ReferenceRows | None = None
    if reference_inputs:
        if nse_build is None:
            raise CompositionError(
                "reference inputs were supplied without any NSE build to align their instrument-ID space against"
            )
        instrument_ids = {
            str(row["instrument_id"]): str(row["asset_class"])
            for row in nse_build.candidate["tables"]["instruments"]
        }
        # Pass S02's own corporate-action-adjusted close series through
        # verbatim (its `DatasetBuild.histories` is already keyed exactly
        # `str(instrument_id) -> (asset_class, ((date, adjusted_close), ...))`)
        # so `benchmark_rs_*` is computed from the SAME adjusted basis as
        # `return_*`/other S02 metrics for the same instrument, never a
        # second, independently-re-derived UNADJUSTED series -- see
        # `build_reference_rows`'s own `adjusted_price_series` docstring.
        adjusted_price_series = {
            instrument_id: dict(points) for instrument_id, (_, points) in nse_build.histories.items()
        }
        reference_rows = build_reference_rows(
            list(reference_inputs),
            raw_store,
            instrument_ids,
            effective_date,
            mappings_path=mappings_path,
            adjusted_price_series=adjusted_price_series,
        )
    return compose_datasets(amfi=amfi_build, nse=nse_build, reference=reference_rows, effective_date=effective_date)


def resolve_source_statuses(
    schedule: Sequence[SourceScheduleEntry],
    build: DatasetBuild | None,
    build_error: str | None,
    effective_date: date,
    *,
    provider_by_category: Mapping[str, str] = {},
) -> dict[str, SourceStatus]:
    """Evaluate one independent :class:`SourceStatus` per scheduled source.

    Composes R01's own ``reconcile()`` (which already implements the
    complete/delayed/failed/not_expected state machine via
    ``SourceFreshness``/``CandidateCoverage``) rather than reimplementing it.
    One source's delay/failure never masks, or is masked by, another's --
    every entry is evaluated from its own ``expected_date``/``loaded_date``
    pair alone.
    """

    loaded_dates: dict[str, date] = {}
    if build is not None:
        for row in build.candidate["tables"]["sources"]:
            loaded_dates[str(row["source_id"])] = date.fromisoformat(str(row["effective_date"]))
    instrument_counts: dict[str, int] = {}
    if build is not None:
        for entry in schedule:
            provider = provider_by_category.get(entry.category)
            if provider is None:
                continue
            instrument_counts[entry.source_id] = sum(
                1 for row in build.candidate["tables"]["instruments"] if row.get("provider") == provider
            )

    observations: dict[str, SourceObservation] = {}
    coverage_rows: list[CandidateCoverage] = []
    for entry in schedule:
        expected = entry.expected_date
        loaded = loaded_dates.get(entry.source_id)
        count = instrument_counts.get(entry.source_id, 0)
        if expected is None:
            observations[entry.source_id] = SourceObservation(expected=False)
            coverage_rows.append(CandidateCoverage(entry.source_id, None, loaded, count, {}))
            continue
        if loaded is None:
            observations[entry.source_id] = SourceObservation(
                expected=True,
                failed=True,
                delay_days=1,
                max_delay_days=entry.max_delay_days,
                detail=f"{entry.source_id}: no candidate artifact was loaded for the expected date {expected.isoformat()}",
            )
            reasons = (f"{entry.source_id}: no candidate artifact is available for the expected date",)
            coverage_rows.append(CandidateCoverage(entry.source_id, expected, None, 0, {}, reasons))
            continue
        delay_days = 0 if loaded == expected else abs((expected - loaded).days)
        observations[entry.source_id] = SourceObservation(
            expected=True,
            failed=False,
            delay_days=delay_days,
            max_delay_days=entry.max_delay_days,
            detail=(
                ""
                if delay_days == 0
                else f"{entry.source_id}: loaded date {loaded.isoformat()} vs expected {expected.isoformat()}"
            ),
        )
        coverage_rows.append(CandidateCoverage(entry.source_id, expected, loaded, count, {}))

    report = reconcile(previous=0, candidate=0, sources=observations, candidate_coverage=coverage_rows)
    coverage_by_source = {coverage.source_id: coverage for coverage in report.candidate_coverage}
    categories = {entry.source_id: entry.category for entry in schedule}
    modes = {entry.source_id: entry.acquisition_mode for entry in schedule}

    max_delay_by_source = {entry.source_id: entry.max_delay_days for entry in schedule}
    statuses: dict[str, SourceStatus] = {}
    for freshness in report.source_freshness:
        coverage = coverage_by_source.get(freshness.source_id)
        statuses[freshness.source_id] = SourceStatus(
            source_id=freshness.source_id,
            category=categories.get(freshness.source_id, freshness.source_id),
            acquisition_mode=str(modes.get(freshness.source_id, "supplied")),
            expected_date=coverage.expected_date if coverage else None,
            loaded_date=coverage.loaded_date if coverage else None,
            state=freshness.status,
            detail=freshness.detail,
            coverage=coverage,
            max_delay_days=max_delay_by_source.get(freshness.source_id, 0),
        )
    return statuses


@dataclass(frozen=True)
class ScheduledRefreshResult:
    """The outcome of one daily-execution attempt against a source schedule.

    Persisted separately from the active market snapshot: ``status`` and
    ``source_status`` are always populated (even on a blocked/failed run), so
    a failure is visible to an operator without ever publishing partial data.
    """

    status: str  # "skipped" | "published" | "blocked" | "failed"
    active_dataset_id: str | None
    dataset_id: str | None
    source_status: Mapping[str, SourceStatus]
    warnings: tuple[str, ...] = ()
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "active_dataset_id": self.active_dataset_id,
            "dataset_id": self.dataset_id,
            "source_status": {key: value.as_dict() for key, value in self.source_status.items()},
            "warnings": list(self.warnings),
            "reason": self.reason,
        }


def run_scheduled_refresh(
    connection: Any,
    publisher: D1Publisher,
    raw_store: Any,
    schedule: Sequence[SourceScheduleEntry],
    effective_date: date,
    *,
    amfi_source_ids: Sequence[str] = (),
    nse_inputs: Sequence[SourceInput] = (),
    reference_inputs: Sequence[SourceInput] = (),
    mappings_path: Any = DEFAULT_MAPPINGS_PATH,
    history_store: HistoryStore | None = None,
    min_coverage_ratio: float = 0.90,
    previous_count: int | None = None,
    source_baselines: Sequence[CandidateCoverage] = (),
    provider_by_category: Mapping[str, str] = {},
) -> ScheduledRefreshResult:
    """Run one composed daily/backfill attempt against an explicit source schedule.

    A day with no source scheduled to publish at all (every entry's
    ``expected_date`` differs from ``effective_date`` -- a weekend, a holiday
    for every configured source, or simply no schedule configured yet) is
    reported ``status="skipped"`` and never touches the publisher: this is
    the "never silently label a no-op schedule a completed refresh"
    requirement, made structural rather than left to caller discipline.

    A required component build failure (composed candidate could not be
    built at all) always blocks promotion, even if per-source freshness
    alone would not have caught it -- this is a defense-in-depth backstop,
    not the primary blocking mechanism (which is per-source freshness via
    ``reconcile()``, exactly as the rest of this pipeline already works).
    """

    expected_today = [entry for entry in schedule if entry.expected_date == effective_date]
    if not expected_today:
        return ScheduledRefreshResult(
            status="skipped",
            active_dataset_id=publisher.active_dataset_id(),
            dataset_id=None,
            source_status={},
            reason="no source is scheduled to publish on this date",
        )

    build: ComposedDatasetBuild | None
    build_error: str | None
    try:
        build = build_composed_candidate(
            connection,
            raw_store,
            amfi_source_ids=amfi_source_ids,
            nse_inputs=nse_inputs,
            reference_inputs=reference_inputs,
            effective_date=effective_date,
            mappings_path=mappings_path,
        )
        build_error = None
    except PublicationInputError as exc:
        build = None
        build_error = str(exc)

    statuses = resolve_source_statuses(
        schedule, build, build_error, effective_date, provider_by_category=provider_by_category
    )
    observations = {
        source_id: SourceObservation(
            expected=status.state != "not_expected",
            failed=status.state == "failed",
            # Use the real computed expected-vs-loaded gap (already resolved
            # once, correctly, inside resolve_source_statuses/reconcile), not
            # a hardcoded 1: a source stale by 30 days must not be treated
            # identically to one stale by 1 day just because both happen to
            # exceed a 1-day tolerance.
            delay_days=(
                abs((status.expected_date - status.loaded_date).days)
                if status.expected_date is not None and status.loaded_date is not None
                else (status.max_delay_days + 1 if status.state == "failed" else 0)
            ),
            max_delay_days=status.max_delay_days,
            detail=status.detail,
        )
        for source_id, status in statuses.items()
    }
    coverage_rows = [status.coverage for status in statuses.values() if status.coverage is not None]
    candidate_total = sum(item.instrument_count for item in coverage_rows)
    if previous_count is not None:
        baseline_total = previous_count
    elif source_baselines:
        baseline_total = sum(item.instrument_count for item in source_baselines)
    else:
        baseline_total = candidate_total
    report = reconcile(
        previous=baseline_total,
        candidate=candidate_total,
        min_coverage_ratio=min_coverage_ratio,
        sources=observations,
        candidate_coverage=coverage_rows,
        source_baselines=source_baselines,
    )
    # R09 D1 storage-budget promotion gate: this must be applied BEFORE
    # publication happens on the compose path too, exactly as `daily`'s own
    # main() already does -- not merely computed afterwards for the JSON
    # report, by which point promotion would already have occurred.
    storage = publisher.budget_report()
    storage_report = budget_report(
        used=storage.used_bytes,
        limit=storage.limit_bytes,
        warning_threshold=storage.warning_threshold,
        source="d1",
    )
    safe_to_promote, blocking_reasons = evaluate_pre_promotion(report, [storage_report])
    if build_error is not None:
        # Defense-in-depth: a composition failure not otherwise reflected by
        # a single source's own freshness state (e.g. an instrument_id
        # collision, or an ambiguous combined manifest) must still block
        # promotion outright.
        safe_to_promote = False
        blocking_reasons = (*blocking_reasons, f"composed candidate build failed: {build_error}")

    publication: PublicationResult = publish_checkpointed_dataset(
        connection,
        publisher,
        raw_store,
        list(amfi_source_ids),
        effective_date=effective_date,
        safe_to_promote=safe_to_promote,
        blocking_reasons=blocking_reasons,
        history_store=history_store,
        build=build,
    )
    if publication.promoted:
        status_label = "published"
    elif not safe_to_promote:
        status_label = "blocked"
    else:
        status_label = "failed"
    return ScheduledRefreshResult(
        status=status_label,
        active_dataset_id=publisher.active_dataset_id(),
        dataset_id=publication.dataset_id,
        source_status=statuses,
        warnings=tuple(publication.warnings),
        reason=publication.reason,
    )


__all__ = [
    "COMPOSITION_VERSION",
    "ComposedDatasetBuild",
    "CompositionError",
    "ScheduledRefreshResult",
    "SourceScheduleEntry",
    "SourceStatus",
    "build_composed_candidate",
    "compose_datasets",
    "resolve_source_statuses",
    "run_scheduled_refresh",
]
