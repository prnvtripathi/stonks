from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from market_pipeline.publication.remote_usage import (
    DEFAULT_MAX_OBSERVATION_AGE_SECONDS,
    RemotePublicationBudgetError,
    RemoteUsage,
    ReservationLedgerEntry,
    append_reservation_ledger_entry,
    load_reservation_ledger,
    plan_monthly_attempts,
    reserved_since,
    scheduled_dates_in_month,
    validate_remote_usage,
)

NOW = datetime(2026, 9, 12, 8, 0, 0, tzinfo=timezone.utc)
EXPECTED_RESOURCES = {"account_id": "acct-1", "database_id": "db-1", "bucket_name": "stonks-private-history"}


def _payload(**overrides: object) -> dict:
    payload = {
        "errors": None,
        "data": {
            "viewer": {
                "accounts": [
                    {
                        "accountTag": "acct-1",
                        "observedAt": "2026-09-12T07:30:00Z",
                        "d1": {
                            "databaseId": "db-1",
                            "databaseSizeBytes": 123_456,
                            "rowsWritten24h": 4200,
                        },
                        "r2": {
                            "bucketName": "stonks-private-history",
                            "retainedBytes": 987_654_321,
                            "windowStart": "2026-09-01T00:00:00Z",
                            "classAOperationsMonthToDate": 12_000,
                            "classBOperationsMonthToDate": 45_000,
                        },
                    }
                ]
            }
        },
    }
    payload.update(overrides)
    return payload


def test_validate_remote_usage_accepts_a_well_formed_fresh_response() -> None:
    usage = validate_remote_usage(_payload(), EXPECTED_RESOURCES, NOW)
    assert usage == RemoteUsage(
        account_id="acct-1",
        database_id="db-1",
        bucket_name="stonks-private-history",
        observed_at=datetime(2026, 9, 12, 7, 30, tzinfo=timezone.utc),
        window_start=datetime(2026, 9, 1, tzinfo=timezone.utc),
        d1_database_bytes=123_456,
        d1_rows_written_24h=4200,
        r2_retained_bytes=987_654_321,
        r2_class_a_operations_month_to_date=12_000,
        r2_class_b_operations_month_to_date=45_000,
    )


def test_validate_remote_usage_accepts_valid_zero_usage() -> None:
    payload = _payload()
    payload["data"]["viewer"]["accounts"][0]["d1"]["databaseSizeBytes"] = 0
    payload["data"]["viewer"]["accounts"][0]["d1"]["rowsWritten24h"] = 0
    payload["data"]["viewer"]["accounts"][0]["r2"]["retainedBytes"] = 0
    payload["data"]["viewer"]["accounts"][0]["r2"]["classAOperationsMonthToDate"] = 0
    payload["data"]["viewer"]["accounts"][0]["r2"]["classBOperationsMonthToDate"] = 0
    usage = validate_remote_usage(payload, EXPECTED_RESOURCES, NOW)
    assert usage.d1_database_bytes == 0
    assert usage.r2_class_a_operations_month_to_date == 0


def test_validate_remote_usage_rejects_graphql_errors() -> None:
    with pytest.raises(RemotePublicationBudgetError):
        validate_remote_usage({"errors": [{"message": "denied"}]}, EXPECTED_RESOURCES, NOW)


def test_validate_remote_usage_calls_out_missing_analytics_permission() -> None:
    payload = {"errors": [{"message": "Authentication error: token lacks required permission"}]}
    with pytest.raises(RemotePublicationBudgetError, match="Account Analytics Read"):
        validate_remote_usage(payload, EXPECTED_RESOURCES, NOW)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p["data"]["viewer"].__setitem__("accounts", []),
        lambda p: p["data"]["viewer"]["accounts"][0].__delitem__("d1"),
        lambda p: p["data"]["viewer"]["accounts"][0].__delitem__("r2"),
        lambda p: p["data"]["viewer"]["accounts"][0]["d1"].__delitem__("databaseId"),
        lambda p: p["data"]["viewer"]["accounts"][0]["r2"].__delitem__("bucketName"),
    ],
)
def test_validate_remote_usage_rejects_absent_account_or_database(mutate) -> None:
    payload = _payload()
    mutate(payload)
    with pytest.raises(RemotePublicationBudgetError):
        validate_remote_usage(payload, EXPECTED_RESOURCES, NOW)


@pytest.mark.parametrize(
    "path, value",
    [
        (("d1", "databaseSizeBytes"), "not-a-number"),
        (("d1", "databaseSizeBytes"), -1),
        (("d1", "rowsWritten24h"), None),
        (("r2", "retainedBytes"), 3.5),
        (("r2", "classAOperationsMonthToDate"), -5),
        (("r2", "classBOperationsMonthToDate"), True),
    ],
)
def test_validate_remote_usage_rejects_malformed_metrics(path, value) -> None:
    payload = _payload()
    section, key = path
    payload["data"]["viewer"]["accounts"][0][section][key] = value
    with pytest.raises(RemotePublicationBudgetError):
        validate_remote_usage(payload, EXPECTED_RESOURCES, NOW)


def test_validate_remote_usage_rejects_stale_observation() -> None:
    payload = _payload()
    stale_time = NOW - timedelta(seconds=DEFAULT_MAX_OBSERVATION_AGE_SECONDS + 1)
    payload["data"]["viewer"]["accounts"][0]["observedAt"] = stale_time.isoformat().replace("+00:00", "Z")
    with pytest.raises(RemotePublicationBudgetError, match="stale"):
        validate_remote_usage(payload, EXPECTED_RESOURCES, NOW)


def test_validate_remote_usage_rejects_future_observation() -> None:
    payload = _payload()
    future_time = NOW + timedelta(hours=1)
    payload["data"]["viewer"]["accounts"][0]["observedAt"] = future_time.isoformat().replace("+00:00", "Z")
    with pytest.raises(RemotePublicationBudgetError, match="future"):
        validate_remote_usage(payload, EXPECTED_RESOURCES, NOW)


def test_validate_remote_usage_rejects_resource_mismatch() -> None:
    payload = _payload()
    payload["data"]["viewer"]["accounts"][0]["d1"]["databaseId"] = "some-other-db"
    with pytest.raises(RemotePublicationBudgetError, match="different account, database, or bucket"):
        validate_remote_usage(payload, EXPECTED_RESOURCES, NOW)

    payload = _payload()
    payload["data"]["viewer"]["accounts"][0]["r2"]["bucketName"] = "some-other-bucket"
    with pytest.raises(RemotePublicationBudgetError):
        validate_remote_usage(payload, EXPECTED_RESOURCES, NOW)

    payload = _payload()
    payload["data"]["viewer"]["accounts"][0]["accountTag"] = "some-other-account"
    with pytest.raises(RemotePublicationBudgetError):
        validate_remote_usage(payload, EXPECTED_RESOURCES, NOW)


def test_validate_remote_usage_rejects_a_month_to_date_window_from_a_prior_month() -> None:
    """A provider returning last month's MTD window after rollover must not be trusted."""

    payload = _payload()
    payload["data"]["viewer"]["accounts"][0]["r2"]["windowStart"] = "2026-08-01T00:00:00Z"
    with pytest.raises(RemotePublicationBudgetError, match="month boundary"):
        validate_remote_usage(payload, EXPECTED_RESOURCES, NOW)


def test_validate_remote_usage_is_idempotent_across_sdk_retries() -> None:
    """An HTTP client transparently retrying the same request must not change the result."""

    payload = _payload()
    first = validate_remote_usage(payload, EXPECTED_RESOURCES, NOW)
    second = validate_remote_usage(payload, EXPECTED_RESOURCES, NOW)
    assert first == second


# --- Monthly scheduled-run accounting -------------------------------------------------


def _brute_force_weekday_count(year: int, month: int) -> int:
    days_in_month = calendar.monthrange(year, month)[1]
    return sum(1 for day in range(1, days_in_month + 1) if date(year, month, day).weekday() < 5)


@pytest.mark.parametrize("year, month", [(2026, 2), (2026, 9), (2027, 3), (2024, 2)])
def test_scheduled_dates_in_month_matches_an_independent_calendar_computation(year: int, month: int) -> None:
    assert scheduled_dates_in_month(year, month) == _brute_force_weekday_count(year, month)


def test_scheduled_dates_in_month_finds_a_23_weekday_month() -> None:
    """At least one month in this range must have exactly 23 weekdays, proving no fixed constant is used."""

    found = [
        (year, month)
        for year in range(2024, 2029)
        for month in range(1, 13)
        if scheduled_dates_in_month(year, month) == 23
    ]
    assert found, "expected at least one 23-weekday month in the sampled range"
    for year, month in found:
        assert scheduled_dates_in_month(year, month) == _brute_force_weekday_count(year, month) == 23


def test_plan_monthly_attempts_covers_month_boundaries() -> None:
    first_of_month = plan_monthly_attempts(date(2026, 9, 1), reserved_manual_attempts=4)
    last_of_month = plan_monthly_attempts(date(2026, 9, 30), reserved_manual_attempts=4)
    assert first_of_month.scheduled_dates_in_month == last_of_month.scheduled_dates_in_month
    assert first_of_month.monthly_attempts == first_of_month.scheduled_dates_in_month + 4


def test_plan_monthly_attempts_reserves_manual_reruns_on_top_of_the_schedule() -> None:
    reference = date(2026, 9, 12)
    scheduled_dates_in_month_value = scheduled_dates_in_month(reference.year, reference.month)
    reserved_manual_attempts = 6
    plan = plan_monthly_attempts(reference, reserved_manual_attempts=reserved_manual_attempts)
    assert plan.monthly_attempts >= scheduled_dates_in_month_value + reserved_manual_attempts


def test_plan_monthly_attempts_rejects_negative_manual_reservation() -> None:
    with pytest.raises(RemotePublicationBudgetError):
        plan_monthly_attempts(date(2026, 9, 1), reserved_manual_attempts=-1)


# --- Reservation ledger ------------------------------------------------------------------


def test_reservation_ledger_round_trips_and_prunes_stale_entries(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    append_reservation_ledger_entry(
        path,
        ReservationLedgerEntry(recorded_at=NOW - timedelta(days=3), d1_mutations=10, r2_class_a_operations=5, r2_class_b_operations=5, r2_bytes=1000),
        now=NOW - timedelta(days=3),
    )
    updated = append_reservation_ledger_entry(
        path,
        ReservationLedgerEntry(recorded_at=NOW, d1_mutations=3, r2_class_a_operations=2, r2_class_b_operations=1, r2_bytes=500),
        now=NOW,
    )
    # The three-day-old entry is older than the default 24h relevance window and is pruned.
    assert len(updated) == 1
    assert updated[0].d1_mutations == 3
    reloaded = load_reservation_ledger(path)
    assert reloaded == updated


def test_reserved_since_only_counts_activity_after_the_observation() -> None:
    entries = (
        ReservationLedgerEntry(recorded_at=NOW - timedelta(hours=2), d1_mutations=100, r2_class_a_operations=100, r2_class_b_operations=100, r2_bytes=100),
        ReservationLedgerEntry(recorded_at=NOW - timedelta(minutes=10), d1_mutations=7, r2_class_a_operations=2, r2_class_b_operations=1, r2_bytes=999),
    )
    since = NOW - timedelta(hours=1)
    totals = reserved_since(entries, since)
    assert totals.d1_mutations == 7
    assert totals.r2_class_a_operations == 2
    assert totals.r2_bytes == 999


def test_load_reservation_ledger_missing_file_is_empty(tmp_path: Path) -> None:
    assert load_reservation_ledger(tmp_path / "missing.json") == ()


def test_load_reservation_ledger_rejects_malformed_json(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(RemotePublicationBudgetError):
        load_reservation_ledger(path)


# --- End-to-end budget gating with validated telemetry --------------------------------


def _twelve_thousand_instrument_rolling_three_year_plan(*, weekday_runs_per_month: int) -> dict:
    """A realistic-scale plan: 12,000 instruments, a rolling three-year history window.

    Payload sizes are modeled on this publisher's actual serialized shapes
    (see ``d1_export._publication_plan`` and its own 12,000-instrument
    fixture in ``pipeline/tests/integration/test_publication.py``): one
    current-year mutable history parquet plus a current compact chart per
    instrument, and two prior years of immutable history parquet per
    instrument (the "rolling three year" window: current + two retained
    years).
    """

    from market_pipeline.publication.d1_export import _publication_plan

    ids = [f"instrument-{index}" for index in range(12_000)]
    mutable = [f"charts/current/{item}.json.gz" for item in ids] + [f"history/equity/{item}/2026.parquet" for item in ids]
    immutable = (
        [f"history/equity/{item}/2024.parquet" for item in ids]
        + [f"history/equity/{item}/2025.parquet" for item in ids]
    )
    # Realistic serialized sizes: ~180 bytes/row snapshot upsert statement,
    # ~2KB per instrument for the full SQL import (metrics + aliases +
    # fundamentals + corporate actions JSON blobs included).
    return _publication_plan(
        snapshot_ids=ids,
        source_count=4,
        source_run_count=4,
        manifest={"mutable": mutable, "immutable": immutable},
        d1_import_bytes=12_000 * 2_048,
        snapshot_bytes=12_000 * 180,
        retained_objects=len(mutable) + len(immutable),
        retained_bytes=(len(mutable) + len(immutable)) * 65_536,
        weekday_runs_per_month=weekday_runs_per_month,
    )


def test_evaluate_publication_attempt_authorizes_a_12000_instrument_rolling_three_year_plan() -> None:
    """The brief's oracle scale case: a validated, in-budget attempt is authorized."""

    from market_pipeline.publication.preflight import evaluate_publication_attempt

    monthly_attempt_plan = plan_monthly_attempts(NOW.date(), reserved_manual_attempts=4)
    plan = _twelve_thousand_instrument_rolling_three_year_plan(weekday_runs_per_month=monthly_attempt_plan.monthly_attempts)
    remote_usage = validate_remote_usage(_payload(), EXPECTED_RESOURCES, NOW)

    outcome = evaluate_publication_attempt(
        plan,
        remote_usage=remote_usage,
        remote_snapshot_ids=set(),
        remote_active_dataset_id=None,
        monthly_attempt_plan=monthly_attempt_plan,
    )

    assert outcome.authorized
    assert outcome.rejection_reason is None
    assert outcome.mutation_calls == ("d1_import", "r2_upload")
    assert outcome.resolved_plan is not None
    assert outcome.resolved_plan["d1_mutations"] < 50_000


def test_evaluate_publication_attempt_rejects_an_oversized_plan_before_any_mutation() -> None:
    """The brief's oracle: ``assert oversized.mutation_calls == []``.

    An over-budget plan is rejected *before* any mutation is authorized --
    the caller never sees a populated ``mutation_calls`` to accidentally
    act on, and the rejection is a plain return value, not just an
    exception a caller might forget to catch.
    """

    from market_pipeline.publication.preflight import evaluate_publication_attempt

    monthly_attempt_plan = plan_monthly_attempts(NOW.date(), reserved_manual_attempts=4)
    plan = _twelve_thousand_instrument_rolling_three_year_plan(weekday_runs_per_month=monthly_attempt_plan.monthly_attempts)
    oversized_plan = {**plan, "r2_mutable_objects": 10_000_000}
    remote_usage = validate_remote_usage(_payload(), EXPECTED_RESOURCES, NOW)

    oversized = evaluate_publication_attempt(
        oversized_plan,
        remote_usage=remote_usage,
        remote_snapshot_ids=set(),
        remote_active_dataset_id=None,
        monthly_attempt_plan=monthly_attempt_plan,
    )

    assert not oversized.authorized
    assert oversized.mutation_calls == ()
    assert oversized.resolved_plan is None
    assert oversized.rejection_reason is not None
    assert "safety envelope" in oversized.rejection_reason


def test_evaluate_publication_attempt_rejects_raw_untrusted_usage() -> None:
    """``remote_usage`` must be a validated ``RemoteUsage``, never a raw mapping."""

    from market_pipeline.publication.preflight import evaluate_publication_attempt

    monthly_attempt_plan = plan_monthly_attempts(NOW.date(), reserved_manual_attempts=4)
    plan = _twelve_thousand_instrument_rolling_three_year_plan(weekday_runs_per_month=monthly_attempt_plan.monthly_attempts)

    outcome = evaluate_publication_attempt(
        plan,
        remote_usage={"database_size": 0, "rows_written_24h": 0},  # type: ignore[arg-type]
        remote_snapshot_ids=set(),
        remote_active_dataset_id=None,
        monthly_attempt_plan=monthly_attempt_plan,
    )

    assert outcome.mutation_calls == ()
    assert outcome.rejection_reason is not None and "validated RemoteUsage" in outcome.rejection_reason
