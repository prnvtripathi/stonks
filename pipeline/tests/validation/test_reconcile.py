from __future__ import annotations

from datetime import date

import pytest
from market_pipeline.monitoring.budgets import budget_report
from market_pipeline.validation.reconcile import (
    CandidateCoverage,
    SourceObservation,
    evaluate_pre_promotion,
    reconcile,
)


def test_coverage_drop_blocks_publish() -> None:
    assert not reconcile(previous=2500, candidate=1800).publishable


def test_source_candidate_coverage_cannot_be_masked_by_other_source_growth() -> None:
    previous = (
        CandidateCoverage("amfi-nav", date(2026, 9, 1), date(2026, 9, 1), 100, {}, ()),
        CandidateCoverage("nse-eod", date(2026, 9, 1), date(2026, 9, 1), 100, {}, ()),
    )
    candidate = (
        CandidateCoverage("amfi-nav", date(2026, 9, 2), date(2026, 9, 2), 50, {}, ()),
        CandidateCoverage("nse-eod", date(2026, 9, 2), date(2026, 9, 2), 200, {}, ()),
    )

    report = reconcile(
        previous=200,
        candidate=250,
        source_baselines=previous,
        candidate_coverage=candidate,
    )

    assert not report.publishable
    assert any(check.name == "source:amfi-nav:coverage" and not check.passed for check in report.checks)


def test_healthy_coverage_is_publishable() -> None:
    report = reconcile(previous=2500, candidate=2450)
    assert report.publishable
    assert report.coverage_ratio == pytest.approx(0.98)


def test_zero_previous_count_does_not_divide_by_zero() -> None:
    report = reconcile(previous=0, candidate=0)
    assert report.publishable
    assert report.coverage_ratio == 1.0


def test_missing_ratio_over_threshold_blocks_publish() -> None:
    report = reconcile(previous=100, candidate=100, missing_ratio=0.10)
    assert not report.publishable
    assert any(check.name == "missing_ratio" for check in report.checks)


def test_missing_ratio_within_threshold_is_publishable() -> None:
    report = reconcile(previous=100, candidate=100, missing_ratio=0.01)
    assert report.publishable


def test_duplicates_block_publish() -> None:
    report = reconcile(previous=100, candidate=100, duplicate_count=3)
    assert not report.publishable


def test_zero_duplicates_is_publishable() -> None:
    report = reconcile(previous=100, candidate=100, duplicate_count=0)
    assert report.publishable


def test_outliers_over_threshold_block_publish() -> None:
    report = reconcile(previous=100, candidate=100, outlier_count=5, max_outlier_count=2)
    assert not report.publishable


def test_outliers_without_threshold_are_informational_only() -> None:
    report = reconcile(previous=100, candidate=100, outlier_count=5)
    assert report.publishable


def test_low_corporate_action_coverage_blocks_publish() -> None:
    report = reconcile(previous=100, candidate=100, corporate_action_coverage=0.5)
    assert not report.publishable


def test_healthy_corporate_action_coverage_is_publishable() -> None:
    report = reconcile(previous=100, candidate=100, corporate_action_coverage=0.99)
    assert report.publishable


def test_stale_benchmark_blocks_publish() -> None:
    report = reconcile(previous=100, candidate=100, benchmark_staleness_days=5)
    assert not report.publishable


def test_fresh_benchmark_is_publishable() -> None:
    report = reconcile(previous=100, candidate=100, benchmark_staleness_days=0)
    assert report.publishable


def test_rs_scores_outside_bounds_block_publish() -> None:
    report = reconcile(previous=100, candidate=100, rs_scores=[10, 50, 150])
    assert not report.publishable


def test_rs_scores_within_bounds_are_publishable() -> None:
    report = reconcile(previous=100, candidate=100, rs_scores=[10, 50, 99])
    assert report.publishable


def test_source_freshness_is_independent_per_source() -> None:
    report = reconcile(
        previous=100,
        candidate=100,
        sources={
            "nse-eod": SourceObservation(expected=True, failed=False, delay_days=0),
            "nse-filings-xbrl": SourceObservation(expected=False),
            "amfi-nav": SourceObservation(expected=True, failed=True, detail="fetch error"),
        },
    )
    statuses = {source.source_id: source.status for source in report.source_freshness}
    assert statuses == {
        "nse-eod": "complete",
        "nse-filings-xbrl": "not_expected",
        "amfi-nav": "failed",
    }
    assert not report.publishable  # amfi-nav failure blocks even though coverage looks fine


def test_delayed_source_does_not_block_but_is_reported() -> None:
    report = reconcile(
        previous=100,
        candidate=100,
        sources={"nse-eod": SourceObservation(expected=True, delay_days=2, max_delay_days=1)},
    )
    assert report.publishable
    assert report.source_freshness[0].status == "delayed"


def test_report_as_dict_is_json_serializable_shape() -> None:
    report = reconcile(previous=100, candidate=100)
    payload = report.as_dict()
    assert payload["publishable"] is True
    assert payload["previous_count"] == 100
    assert payload["candidate_count"] == 100
    assert isinstance(payload["checks"], list)
    assert isinstance(payload["source_freshness"], list)


def test_pre_promotion_check_combines_reconciliation_and_budgets() -> None:
    healthy = reconcile(previous=100, candidate=100)
    ok, reasons = evaluate_pre_promotion(healthy, [budget_report(used=1, limit=100, source="d1")])
    assert ok
    assert reasons == ()

    unhealthy = reconcile(previous=2500, candidate=1800)
    ok, reasons = evaluate_pre_promotion(unhealthy, [budget_report(used=100, limit=100, source="d1")])
    assert not ok
    assert any("coverage" in reason for reason in reasons)
    assert any("budget" in reason for reason in reasons)
