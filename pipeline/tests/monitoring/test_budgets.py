from __future__ import annotations

from market_pipeline.monitoring.budgets import budget_report, budget_reports


def test_eighty_percent_warns() -> None:
    assert budget_report(used=4_000_000_000, limit=5_000_000_000).warning


def test_below_threshold_does_not_warn() -> None:
    assert not budget_report(used=1, limit=5_000_000_000).warning


def test_exceeded_flags_over_limit() -> None:
    report = budget_report(used=6_000_000_000, limit=5_000_000_000)
    assert report.exceeded
    assert report.warning


def test_no_limit_never_warns_or_exceeds() -> None:
    report = budget_report(used=10**12, limit=None)
    assert not report.warning
    assert not report.exceeded


def test_budget_reports_labels_multiple_sources() -> None:
    reports = budget_reports({"d1": (10, 100), "r2": (90, 100)})
    by_source = {report.source: report for report in reports}
    assert not by_source["d1"].warning
    assert by_source["r2"].warning


def test_as_dict_includes_source_and_exceeded() -> None:
    payload = budget_report(used=100, limit=100, source="d1").as_dict()
    assert payload["source"] == "d1"
    assert payload["exceeded"] is True
