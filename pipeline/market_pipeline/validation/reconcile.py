"""Pre-publication reconciliation checks for a daily dataset candidate.

Every daily/backfill run produces a *candidate* dataset that must be checked
against the *previous* known-good published dataset before promotion. This
module implements those checks as small, composable, independently-testable
functions that all feed one :class:`ReconciliationReport`. The report's
``publishable`` flag is the single gate the workflow (and, eventually, the
publish CLI path) must consult before calling ``D1Publisher.promote`` --
never promote when it is False.

Chosen thresholds (all overridable by keyword, defaults chosen to be strict
enough to catch real incidents but tolerant of normal single-day noise):

* ``min_coverage_ratio`` = 0.90 -- candidate row/instrument count must be at
  least 90% of the previous published count. A drop below this usually means
  a source fetch silently truncated (e.g. NSE bhavcopy partially downloaded,
  AMFI NAV file truncated) rather than a genuine market event, since the
  investable universe does not shrink by more than ~10% day to day. The
  brief's own example (1800 of a previous 2500 = 72%) is well below this and
  must fail; 2450 of 2500 = 98% must pass.
* ``max_missing_ratio`` = 0.05 -- at most 5% of expected observations may be
  missing (as opposed to `not_applicable`) before we consider the candidate
  incomplete.
* ``max_duplicate_count`` = 0 -- a correctly normalized daily dataset must
  never contain duplicate (instrument, date) rows; any duplicate indicates a
  normalization or merge bug.
* ``min_corporate_action_coverage`` = 0.95 -- at least 95% of instruments
  with a corporate action expected on/near the effective date must have it
  recorded, so momentum/RS adjustments are not silently wrong.
* ``max_benchmark_staleness_days`` = 1 -- the benchmark (Nifty 500) close
  used for relative-strength scoring must be from the effective date or the
  immediately preceding day at the very latest.

Per-source freshness (``complete`` / ``delayed`` / ``failed`` /
``not_expected``) is evaluated independently per source: one source being
delayed or not expected on a given day must never mask, or be masked by,
another source's failure. Only ``failed`` sources block publication;
``delayed`` and ``not_expected`` are surfaced for operator visibility but do
not by themselves stop the run (a source can legitimately be delayed by a
holiday-adjacent settlement without invalidating the rest of the dataset).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable, Mapping, Sequence

DEFAULT_MIN_COVERAGE_RATIO = 0.90
DEFAULT_MAX_MISSING_RATIO = 0.05
DEFAULT_MAX_DUPLICATE_COUNT = 0
DEFAULT_MIN_CORPORATE_ACTION_COVERAGE = 0.95
DEFAULT_MAX_BENCHMARK_STALENESS_DAYS = 1


@dataclass(frozen=True)
class SourceObservation:
    """Freshness inputs for one source on one run.

    ``max_delay_days`` is the number of days of lag that is still considered
    on-time for this source (e.g. a filings source that only expects to see
    something within 2 trading days).
    """

    expected: bool = True
    failed: bool = False
    delay_days: int = 0
    max_delay_days: int = 0
    detail: str = ""


@dataclass(frozen=True)
class SourceFreshness:
    source_id: str
    status: str
    detail: str = ""

    @property
    def blocking(self) -> bool:
        return self.status == "failed"


@dataclass(frozen=True)
class CandidateCoverage:
    """Observed source coverage in the candidate that is about to publish."""

    source_id: str
    expected_date: date | None
    loaded_date: date | None
    instrument_count: int
    missing_ratios: Mapping[str, float]
    blocking_reasons: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "expected_date": self.expected_date.isoformat() if self.expected_date else None,
            "loaded_date": self.loaded_date.isoformat() if self.loaded_date else None,
            "instrument_count": self.instrument_count,
            "missing_ratios": dict(self.missing_ratios),
            "blocking_reasons": list(self.blocking_reasons),
        }


def _evaluate_source(source_id: str, observation: SourceObservation) -> SourceFreshness:
    if not observation.expected:
        return SourceFreshness(source_id, "not_expected", observation.detail)
    if observation.failed:
        return SourceFreshness(source_id, "failed", observation.detail or f"{source_id} reported failure")
    if observation.delay_days > observation.max_delay_days:
        detail = observation.detail or f"{source_id} delayed by {observation.delay_days} day(s)"
        return SourceFreshness(source_id, "delayed", detail)
    return SourceFreshness(source_id, "complete", observation.detail)


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True)
class ReconciliationReport:
    previous_count: int
    candidate_count: int
    coverage_ratio: float
    min_coverage_ratio: float
    checks: tuple[CheckResult, ...] = ()
    source_freshness: tuple[SourceFreshness, ...] = ()
    candidate_coverage: tuple[CandidateCoverage, ...] = ()

    @property
    def publishable(self) -> bool:
        if any(not check.passed for check in self.checks):
            return False
        if any(coverage.blocking_reasons for coverage in self.candidate_coverage):
            return False
        return not any(source.blocking for source in self.source_freshness)

    @property
    def failures(self) -> tuple[str, ...]:
        reasons = [check.detail or check.name for check in self.checks if not check.passed]
        reasons.extend(
            f"source {source.source_id} failed: {source.detail}".strip()
            for source in self.source_freshness
            if source.blocking
        )
        reasons.extend(reason for coverage in self.candidate_coverage for reason in coverage.blocking_reasons)
        return tuple(reasons)

    def as_dict(self) -> dict[str, Any]:
        return {
            "previous_count": self.previous_count,
            "candidate_count": self.candidate_count,
            "coverage_ratio": self.coverage_ratio,
            "min_coverage_ratio": self.min_coverage_ratio,
            "publishable": self.publishable,
            "failures": list(self.failures),
            "checks": [{"name": c.name, "passed": c.passed, "detail": c.detail} for c in self.checks],
            "source_freshness": [
                {"source_id": s.source_id, "status": s.status, "detail": s.detail}
                for s in self.source_freshness
            ],
            "candidate_coverage": [coverage.as_dict() for coverage in self.candidate_coverage],
        }


def reconcile(
    previous: int,
    candidate: int,
    *,
    min_coverage_ratio: float = DEFAULT_MIN_COVERAGE_RATIO,
    missing_ratio: float | None = None,
    max_missing_ratio: float = DEFAULT_MAX_MISSING_RATIO,
    duplicate_count: int = 0,
    max_duplicate_count: int = DEFAULT_MAX_DUPLICATE_COUNT,
    outlier_count: int | None = None,
    max_outlier_count: int | None = None,
    corporate_action_coverage: float | None = None,
    min_corporate_action_coverage: float = DEFAULT_MIN_CORPORATE_ACTION_COVERAGE,
    benchmark_staleness_days: int | None = None,
    max_benchmark_staleness_days: int = DEFAULT_MAX_BENCHMARK_STALENESS_DAYS,
    rs_scores: Sequence[float] | None = None,
    rs_score_bounds: tuple[float, float] = (0.0, 100.0),
    sources: Mapping[str, SourceObservation] | None = None,
    candidate_coverage: Sequence[CandidateCoverage] = (),
    source_baselines: Sequence[CandidateCoverage] = (),
) -> ReconciliationReport:
    """Compare a candidate dataset against the previous published dataset.

    Only ``previous`` and ``candidate`` are required (matching the brief's
    literal ``reconcile(previous=2500, candidate=1800).publishable`` usage);
    every other check is opt-in and only contributes to the verdict when its
    corresponding input is supplied, so a minimal call never fails on data
    the caller hasn't measured yet.
    """

    if previous < 0 or candidate < 0:
        raise ValueError("counts cannot be negative")

    ratio = 1.0 if previous == 0 else candidate / previous
    checks: list[CheckResult] = [
        CheckResult(
            "coverage",
            ratio >= min_coverage_ratio,
            f"coverage ratio {ratio:.1%} is below minimum {min_coverage_ratio:.0%} "
            f"({candidate} of {previous} previously published rows)",
        ),
        CheckResult(
            "duplicates",
            duplicate_count <= max_duplicate_count,
            f"found {duplicate_count} duplicate row(s); maximum allowed is {max_duplicate_count}",
        ),
    ]

    if missing_ratio is not None:
        checks.append(
            CheckResult(
                "missing_ratio",
                missing_ratio <= max_missing_ratio,
                f"missing ratio {missing_ratio:.1%} exceeds maximum {max_missing_ratio:.0%}",
            )
        )
    if outlier_count is not None and max_outlier_count is not None:
        checks.append(
            CheckResult(
                "outliers",
                outlier_count <= max_outlier_count,
                f"found {outlier_count} outlier value(s); maximum allowed is {max_outlier_count}",
            )
        )
    if corporate_action_coverage is not None:
        checks.append(
            CheckResult(
                "corporate_action_coverage",
                corporate_action_coverage >= min_corporate_action_coverage,
                f"corporate action coverage {corporate_action_coverage:.1%} is below minimum "
                f"{min_corporate_action_coverage:.0%}",
            )
        )
    if benchmark_staleness_days is not None:
        checks.append(
            CheckResult(
                "benchmark_freshness",
                benchmark_staleness_days <= max_benchmark_staleness_days,
                f"benchmark data is {benchmark_staleness_days} day(s) stale; maximum allowed is "
                f"{max_benchmark_staleness_days}",
            )
        )
    if rs_scores is not None:
        low, high = rs_score_bounds
        out_of_bounds = [score for score in rs_scores if not (low <= score <= high)]
        checks.append(
            CheckResult(
                "rs_score_distribution",
                not out_of_bounds,
                f"{len(out_of_bounds)} RS score(s) fall outside expected bounds [{low}, {high}]",
            )
        )

    baseline_by_source = {coverage.source_id: coverage for coverage in source_baselines}
    for coverage in candidate_coverage:
        baseline = baseline_by_source.get(coverage.source_id)
        if baseline is None:
            continue
        source_ratio = 1.0 if baseline.instrument_count == 0 else coverage.instrument_count / baseline.instrument_count
        checks.append(
            CheckResult(
                f"source:{coverage.source_id}:coverage",
                source_ratio >= min_coverage_ratio,
                f"source {coverage.source_id} coverage ratio {source_ratio:.1%} is below minimum "
                f"{min_coverage_ratio:.0%} ({coverage.instrument_count} of {baseline.instrument_count} previously published instruments)",
            )
        )
        for name, ratio in coverage.missing_ratios.items():
            if name == "expected_date":
                # Source freshness reports delayed expected dates separately;
                # this mapping is for normalized-data missingness only.
                continue
            checks.append(
                CheckResult(
                    f"source:{coverage.source_id}:missing_ratio:{name}",
                    ratio <= max_missing_ratio,
                    f"source {coverage.source_id} missing ratio for {name} {ratio:.1%} exceeds maximum "
                    f"{max_missing_ratio:.0%}",
                )
            )

    freshness = tuple(
        _evaluate_source(source_id, observation) for source_id, observation in (sources or {}).items()
    )
    return ReconciliationReport(
        previous_count=previous,
        candidate_count=candidate,
        coverage_ratio=ratio,
        min_coverage_ratio=min_coverage_ratio,
        checks=tuple(checks),
        source_freshness=freshness,
        candidate_coverage=tuple(candidate_coverage),
    )


def evaluate_pre_promotion(
    report: ReconciliationReport, budgets: Iterable[Any] = ()
) -> tuple[bool, tuple[str, ...]]:
    """Combine a reconciliation verdict with storage-budget state.

    This is the single choke point a publish path (CLI or workflow) must
    call before invoking ``D1Publisher.promote``: if it returns ``False``,
    promotion must not happen, and the returned reasons are safe to print or
    upload as the operator-facing failure report.
    """

    reasons = list(report.failures)
    for budget in budgets:
        if getattr(budget, "exceeded", False):
            source = getattr(budget, "source", "unknown")
            used = getattr(budget, "used", "?")
            limit = getattr(budget, "limit", "?")
            reasons.append(f"storage budget exceeded for {source}: {used}/{limit} bytes")
    return (len(reasons) == 0, tuple(reasons))


__all__ = [
    "CandidateCoverage",
    "CheckResult",
    "ReconciliationReport",
    "SourceFreshness",
    "SourceObservation",
    "evaluate_pre_promotion",
    "reconcile",
]
