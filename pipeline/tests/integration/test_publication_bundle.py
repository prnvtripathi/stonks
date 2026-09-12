from __future__ import annotations

from datetime import date

import pytest
from market_pipeline.publication.bundle import (
    BundleError,
    BundleObject,
    PublicationBundle,
    build_bundle,
    plan_garbage_collection,
    serving_years,
    sql_checksum,
)


def _history_entry(instrument: str, year: int, digest: str) -> BundleObject:
    return BundleObject(
        key=f"history/mutual_fund/{instrument}/{year}/{digest}.parquet",
        sha256=digest,
        bytes=100,
        kind="history",
    )


def _chart_entry(dataset_id: str, instrument: str, digest: str) -> BundleObject:
    return BundleObject(key=f"charts/{dataset_id}/{instrument}.json.gz", sha256=digest, bytes=10, kind="chart")


def test_serving_years_is_a_rolling_three_calendar_year_window() -> None:
    assert serving_years(date(2026, 9, 1)) == (2024, 2025, 2026)
    assert serving_years(date(2026, 1, 1)) == (2024, 2025, 2026)


def test_rolling_three_calendar_year_bundle_excludes_older_history_and_unrelated_charts() -> None:
    digest = "a" * 64
    candidate_entries = [
        _history_entry("i1", 2021, digest),  # outside window: calculation-boundary observation only
        _history_entry("i1", 2024, digest),
        _history_entry("i1", 2025, digest),
        _history_entry("i1", 2026, digest),
        _chart_entry("amfi-nav-2026-09-01-abc", "i1", digest),
    ]
    unrelated_old_chart = _chart_entry("amfi-nav-2026-08-31-old", "i1", digest).key

    bundle = build_bundle(
        dataset_id="amfi-nav-2026-09-01-abc",
        input_manifest_hash="f" * 64,
        source_dates=["2026-09-01"],
        projection_version="d1-projection-v1",
        candidate_entries=candidate_entries,
        effective_date=date(2026, 9, 1),
    )

    expected_candidate_keys = tuple(sorted(
        entry.key for entry in candidate_entries if entry.key != _history_entry("i1", 2021, digest).key
    ))
    assert bundle.object_keys == expected_candidate_keys
    assert unrelated_old_chart not in bundle.object_keys
    assert _history_entry("i1", 2021, digest).key not in bundle.object_keys


def test_build_bundle_rejects_missing_identity_fields() -> None:
    entries = [_chart_entry("d", "i1", "a" * 64)]
    with pytest.raises(BundleError, match="dataset_id"):
        build_bundle(
            dataset_id="", input_manifest_hash="f" * 64, source_dates=["2026-09-01"],
            projection_version="v1", candidate_entries=entries, effective_date=date(2026, 9, 1),
        )


def test_build_bundle_rejects_an_empty_object_set() -> None:
    with pytest.raises(BundleError, match="no reachable objects"):
        build_bundle(
            dataset_id="d", input_manifest_hash="f" * 64, source_dates=["2026-09-01"],
            projection_version="v1", candidate_entries=[], effective_date=date(2026, 9, 1),
        )


def test_bundle_round_trips_through_as_dict_and_from_dict() -> None:
    bundle = build_bundle(
        dataset_id="d", input_manifest_hash="f" * 64, source_dates=["2026-09-01", "2026-08-31"],
        projection_version="v1", candidate_entries=[_chart_entry("d", "i1", "a" * 64)],
        effective_date=date(2026, 9, 1),
    )
    restored = PublicationBundle.from_dict(bundle.as_dict())
    assert restored == bundle


def test_bundle_finalizes_with_a_sql_checksum_computed_from_the_export_text() -> None:
    bundle = build_bundle(
        dataset_id="d", input_manifest_hash="f" * 64, source_dates=["2026-09-01"],
        projection_version="v1", candidate_entries=[_chart_entry("d", "i1", "a" * 64)],
        effective_date=date(2026, 9, 1),
    )
    assert bundle.sql_checksum is None
    finalized = bundle.with_sql_checksum(sql_checksum("INSERT INTO x VALUES (1);"))
    assert finalized.sql_checksum == sql_checksum("INSERT INTO x VALUES (1);")
    assert finalized.object_keys == bundle.object_keys


def test_garbage_collection_is_a_dry_run_listing_only_unreachable_derived_objects() -> None:
    active = build_bundle(
        dataset_id="active", input_manifest_hash="f" * 64, source_dates=["2026-09-01"],
        projection_version="v1", candidate_entries=[_chart_entry("active", "i1", "a" * 64)],
        effective_date=date(2026, 9, 1),
    )
    previous = build_bundle(
        dataset_id="previous", input_manifest_hash="e" * 64, source_dates=["2026-08-31"],
        projection_version="v1", candidate_entries=[_chart_entry("previous", "i1", "b" * 64)],
        effective_date=date(2026, 8, 31),
    )
    existing_objects = {
        active.object_keys[0]: 10,
        previous.object_keys[0]: 10,
        "charts/very-old-dataset/i1.json.gz": 10,
        "history/mutual_fund/i1/2020/" + "c" * 64 + ".parquet": 100,
        "raw/amfi-nav/2020-01-01/checkpoint.bin": 999,
    }

    plan = plan_garbage_collection([active, previous], existing_objects)

    assert plan.candidate_keys == ("charts/very-old-dataset/i1.json.gz", "history/mutual_fund/i1/2020/" + "c" * 64 + ".parquet")
    assert plan.candidate_bytes == 110
    assert "raw/amfi-nav/2020-01-01/checkpoint.bin" not in plan.candidate_keys
    assert active.object_keys[0] in plan.reachable
    as_dict = plan.as_dict()
    assert as_dict["dry_run"] is True
    assert as_dict["candidate_count"] == 2
