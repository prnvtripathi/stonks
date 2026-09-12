from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from market_pipeline.analytics.momentum import NormalizedMomentumInput, momentum_score
from market_pipeline.jobs.publish import DatasetBuild, publish_checkpointed_dataset
from market_pipeline.publication.bundle import BundleObject, build_bundle
from market_pipeline.storage.d1_publisher import D1Publisher, ReconciliationError, publish
from market_pipeline.storage.history_store import HistoryStore, HistoryStoreError


def test_forward_migrations_upgrade_existing_and_fresh_databases() -> None:
    migration_dir = Path(__file__).resolve().parents[3] / "db" / "migrations"
    legacy = sqlite3.connect(":memory:")
    legacy.executescript((migration_dir / "0001_market_schema.sql").read_text(encoding="utf-8"))
    legacy.execute("INSERT INTO datasets(dataset_id, status, created_at) VALUES ('legacy', 'active', '2026-09-01T00:00:00Z')")
    legacy.execute("INSERT INTO saved_screens(dataset_id, screen_id, name, expression, created_at, updated_at) VALUES ('legacy', 'screen-1', 'Legacy', 'Volume > 1', '2026-09-01', '2026-09-01')")
    assert legacy.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='corporate_actions'").fetchone() is None
    D1Publisher(legacy).initialize_schema()
    assert legacy.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='corporate_actions'").fetchone() is not None
    assert legacy.execute("SELECT name, expression FROM saved_screens WHERE screen_id='screen-1'").fetchone() == ("Legacy", "Volume > 1")
    assert "dataset_id" not in {row[1] for row in legacy.execute("PRAGMA table_info(saved_screens)").fetchall()}
    assert {"source", "language_version"} <= {row[1] for row in legacy.execute("PRAGMA table_info(screen_runs)").fetchall()}
    assert {"symbol", "name", "asset_class"} <= {row[1] for row in legacy.execute("PRAGMA table_info(screen_matches)").fetchall()}
    legacy.execute("INSERT INTO active_dataset(singleton, dataset_id, changed_at) VALUES (1, 'legacy', '2026-09-01T00:00:00Z')")
    legacy.execute("INSERT INTO instrument_snapshots(instrument_id, dataset_id, symbol, name, asset_class, active) VALUES ('old', 'legacy', 'OLD', 'Old instrument', 'equity', 1)")
    legacy.execute("INSERT INTO screen_runs(dataset_id, run_id, screen_id, effective_date, status, result_count, source, language_version, completed_at) VALUES ('legacy', 'run-1', 'screen-1', '2026-09-01', 'complete', 1, 'Volume > 100', 'v1', '2026-09-01T12:00:00Z')")
    legacy.execute("INSERT INTO screen_matches(dataset_id, run_id, ordinal, instrument_id, score, symbol, name, asset_class, explanation_json, entered, exited) VALUES ('legacy', 'run-1', 1, 'old', 10, 'OLD', 'Old instrument', 'equity', '{\"matched\":true}', 1, 0)")
    legacy.commit()

    D1Publisher(legacy).initialize_schema()

    assert legacy.execute("SELECT symbol, name FROM instrument_snapshots WHERE instrument_id='old'").fetchone() == ("OLD", "Old instrument")
    assert legacy.execute("SELECT source, language_version FROM screen_runs WHERE run_id='run-1'").fetchone() == ("Volume > 100", "v1")
    assert legacy.execute("SELECT symbol, explanation_json FROM screen_matches WHERE run_id='run-1'").fetchone() == ("OLD", '{"matched":true}')

    fresh = sqlite3.connect(":memory:")
    D1Publisher(fresh).initialize_schema()
    assert fresh.execute("PRAGMA table_info(corporate_actions)").fetchall()
    assert fresh.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_corporate_actions_instrument'").fetchone() is not None
    assert {"source", "language_version"} <= {row[1] for row in fresh.execute("PRAGMA table_info(screen_runs)").fetchall()}
    assert {"symbol", "name", "asset_class"} <= {row[1] for row in fresh.execute("PRAGMA table_info(screen_matches)").fetchall()}


def test_publish_attaches_equity_and_mutual_fund_momentum_provenance() -> None:
    publisher = D1Publisher(sqlite3.connect(":memory:"))
    publisher.initialize_schema()
    candidate = _complete_candidate("momentum-dataset")
    candidate["tables"]["instruments"] = [
        {"instrument_id": "equity-1", "provider": "nse", "provider_identifier": "EQ1", "asset_class": "equity"},
        {"instrument_id": "mf-1", "provider": "amfi", "provider_identifier": "MF1", "asset_class": "mutual_fund"},
    ]
    candidate["tables"]["latest_metrics"] = []
    equity = momentum_score(NormalizedMomentumInput.from_mapping({key: 0.5 for key in (
        "weighted_12m_rs_percentile", "six_month_performance", "three_month_performance",
        "trend_strength", "proximity_to_52_week_high", "volume_confirmation",
    )}, asset_class="equity"), effective_date="2026-09-01")
    mutual_fund = momentum_score(NormalizedMomentumInput.from_mapping({key: 0.5 for key in (
        "three_month_return", "six_month_return", "twelve_month_return", "category_rank",
        "inverse_volatility", "inverse_max_drawdown",
    )}, asset_class="mutual_fund", category="large-cap"), effective_date="2026-09-01")
    publish(candidate, publisher, momentum_scores={"equity-1": equity, "mf-1": mutual_fund}, source_artifact_ids={"equity-1": "nse-artifact", "mf-1": "amfi-artifact"})
    rows = publisher.connection.execute("SELECT instrument_id, metric, normalized_value, formula_version, source_artifact_id, metadata_json FROM latest_metrics ORDER BY instrument_id, metric").fetchall()
    assert len(rows) == 14
    assert rows[0][0] == "equity-1"
    assert all(row[3] == "momentum-v2-cohort" for row in rows)
    assert {row[4] for row in rows} == {"nse-artifact", "amfi-artifact"}
    assert any(json.loads(row[5])["cohort"] == "mutual_fund:large-cap" for row in rows if row[0] == "mf-1")


def test_bad_candidate_does_not_replace_active() -> None:
    connection = sqlite3.connect(":memory:")
    publisher = D1Publisher(connection)
    publisher.initialize_schema()
    publisher.seed_active("good")

    with pytest.raises(ReconciliationError):
        publish({"dataset_id": "bad", "valid": False}, publisher)

    assert publisher.active_dataset_id() == "good"


def test_history_failure_leaves_previous_dataset_active(monkeypatch: pytest.MonkeyPatch) -> None:
    """A new active pointer must never refer to history that failed to persist."""

    connection = sqlite3.connect(":memory:")
    publisher = D1Publisher(connection)
    publisher.initialize_schema()
    publisher.seed_active("known-good")
    candidate = _complete_candidate("candidate")
    build = DatasetBuild(
        candidate=candidate,
        momentum={},
        source_artifact_ids={},
        histories={"instrument-1": ("mutual_fund", ())},
        warnings=(),
    )
    monkeypatch.setattr("market_pipeline.jobs.publish.build_candidate", lambda *args, **kwargs: build)

    class FailingHistoryStore:
        def write_history(self, *args: Any, **kwargs: Any) -> list[str]:
            raise HistoryStoreError("R2 write failed")

        def write_chart(self, *args: Any, **kwargs: Any) -> str:
            raise AssertionError("chart writing must not follow a failed history write")

    result = publish_checkpointed_dataset(
        connection,
        publisher,
        raw_store=object(),
        source_ids=["amfi-nav"],
        effective_date=__import__("datetime").date(2026, 9, 1),
        safe_to_promote=True,
        history_store=FailingHistoryStore(),  # type: ignore[arg-type]
    )

    assert result.promoted is False
    assert "history" in (result.reason or "")
    assert publisher.active_dataset_id() == "known-good"


def test_filesystem_history_failure_leaves_previous_dataset_active(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = sqlite3.connect(":memory:")
    publisher = D1Publisher(connection)
    publisher.initialize_schema()
    publisher.seed_active("known-good")
    build = DatasetBuild(_complete_candidate("candidate"), {}, {}, {"instrument-1": ("mutual_fund", ())}, ())
    monkeypatch.setattr("market_pipeline.jobs.publish.build_candidate", lambda *args, **kwargs: build)

    class PermissionDeniedHistoryStore:
        def write_history(self, *args: Any, **kwargs: Any) -> list[str]:
            raise PermissionError("history root is read-only")

        def write_chart(self, *args: Any, **kwargs: Any) -> str:
            raise AssertionError("chart writing must not follow a filesystem write failure")

    result = publish_checkpointed_dataset(
        connection, publisher, object(), ["amfi-nav"], effective_date=__import__("datetime").date(2026, 9, 1),
        safe_to_promote=True, history_store=PermissionDeniedHistoryStore(),  # type: ignore[arg-type]
    )

    assert result.promoted is False
    assert "history" in (result.reason or "")
    assert publisher.active_dataset_id() == "known-good"


def test_failed_publication_cannot_change_the_active_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed candidate must never move the active dataset's own recorded bundle."""

    connection = sqlite3.connect(":memory:")
    publisher = D1Publisher(connection)
    publisher.initialize_schema()

    known_good = _complete_candidate("known-good")
    known_good_bundle = build_bundle(
        dataset_id="known-good", input_manifest_hash="a" * 64, source_dates=["2026-08-31"],
        projection_version="d1-projection-v1",
        candidate_entries=[BundleObject(key="charts/known-good/instrument-1.json.gz", sha256="b" * 64, bytes=1, kind="chart")],
        effective_date=date(2026, 8, 31),
    )
    known_good["metadata"]["publication_bundle"] = known_good_bundle.as_dict()
    publish(known_good, publisher)
    before_failure_bundle_id = publisher.active_dataset_id()
    assert before_failure_bundle_id == "known-good"

    candidate = _complete_candidate("candidate")
    build = DatasetBuild(candidate, {}, {}, {"instrument-1": ("mutual_fund", ())}, ())
    monkeypatch.setattr("market_pipeline.jobs.publish.build_candidate", lambda *args, **kwargs: build)

    class FailingHistoryStore:
        def write_history(self, *args: Any, **kwargs: Any) -> list[Any]:
            raise HistoryStoreError("R2 write failed")

        def write_chart(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("chart writing must not follow a failed history write")

    result = publish_checkpointed_dataset(
        connection, publisher, raw_store=object(), source_ids=["amfi-nav"],
        effective_date=date(2026, 9, 1), safe_to_promote=True,
        history_store=FailingHistoryStore(),  # type: ignore[arg-type]
    )

    assert result.promoted is False
    active_bundle_id = publisher.active_dataset_id()
    assert active_bundle_id == before_failure_bundle_id
    row = connection.execute(
        "SELECT metadata_json FROM datasets WHERE dataset_id = ?", (active_bundle_id,)
    ).fetchone()
    assert json.loads(row[0])["publication_bundle"]["dataset_id"] == "known-good"


def test_active_dataset_export_imports_complete_snapshot_and_preserves_global_screens(
    tmp_path: Path,
) -> None:
    """The remote import is repeatable and switches the active pointer last."""

    from market_pipeline.publication.d1_export import export_active_dataset

    local = sqlite3.connect(":memory:")
    local_publisher = D1Publisher(local)
    local_publisher.initialize_schema()
    candidate = _complete_candidate("published")
    candidate["tables"]["instruments"][0]["name"] = "O'Connor Fund"
    publish(candidate, local_publisher)
    sql_path = tmp_path / "active-dataset.sql"
    assert export_active_dataset(local, sql_path) == "published"
    sql = sql_path.read_text(encoding="utf-8")

    assert "BEGIN" not in sql
    assert "COMMIT" not in sql
    assert "INSERT OR IGNORE INTO datasets" in sql
    assert "INSERT INTO active_dataset" in sql
    assert "O''Connor Fund" in sql
    assert "saved_screens" not in sql
    assert "screen_runs" not in sql
    assert "screen_matches" not in sql
    assert "INSERT INTO instrument_snapshots" in sql
    assert "ON CONFLICT(instrument_id) DO UPDATE SET" in sql
    assert "DELETE FROM instrument_snapshots WHERE dataset_id <>" in sql
    assert "INSERT OR IGNORE INTO latest_metrics" not in sql

    remote = sqlite3.connect(":memory:")
    remote_publisher = D1Publisher(remote)
    remote_publisher.initialize_schema()
    remote_publisher.seed_active("remote-old")
    remote.execute(
        "INSERT INTO saved_screens(screen_id,name,expression,created_at,updated_at) VALUES(?,?,?,?,?)",
        ("screen-1", "Keep me", "return_1d > 0", "2026-09-01", "2026-09-01"),
    )
    remote.commit()

    remote.executescript(sql)
    remote.executescript(sql)

    assert remote_publisher.active_dataset_id() == "published"
    assert remote.execute("SELECT status FROM datasets WHERE dataset_id='published'").fetchone() == ("active",)
    assert remote.execute("SELECT name FROM saved_screens WHERE screen_id='screen-1'").fetchone() == ("Keep me",)
    assert remote.execute("SELECT name FROM instrument_snapshots WHERE dataset_id='published'").fetchone() == ("O'Connor Fund",)


def test_active_dataset_export_fails_closed_for_incomplete_active_data(tmp_path: Path) -> None:
    from market_pipeline.publication.d1_export import DatasetExportError, export_active_dataset

    connection = sqlite3.connect(":memory:")
    publisher = D1Publisher(connection)
    publisher.initialize_schema()
    publisher.seed_active("incomplete")

    with pytest.raises(DatasetExportError, match="effective date"):
        export_active_dataset(connection, tmp_path / "must-not-exist.sql")


def test_active_dataset_object_manifest_excludes_old_dataset_charts(tmp_path: Path) -> None:
    """The bundle -- not a filesystem glob -- decides what the export requires."""

    from market_pipeline.publication.d1_export import export_active_dataset

    connection = sqlite3.connect(":memory:")
    publisher = D1Publisher(connection)
    publisher.initialize_schema()

    history_root = tmp_path / "history"
    store = HistoryStore(history_root)
    [history_object] = store.write_history("mutual_fund", "instrument-1", [{"effective_date": "2026-09-01", "value": "1"}])
    chart_object = store.write_chart("published", "instrument-1", {"points": []})
    unrelated_old_chart = store.write_chart("old-dataset", "instrument-1", {"points": []})

    candidate = _complete_candidate("published")
    bundle = build_bundle(
        dataset_id="published",
        input_manifest_hash="f" * 64,
        source_dates=["2026-09-01"],
        projection_version="d1-projection-v1",
        candidate_entries=[
            BundleObject(key=history_object.key, sha256=history_object.sha256, bytes=history_object.bytes, kind="history"),
            BundleObject(key=chart_object.key, sha256=chart_object.sha256, bytes=chart_object.bytes, kind="chart"),
        ],
        effective_date=date(2026, 9, 1),
    )
    candidate["metadata"]["publication_bundle"] = bundle.as_dict()
    publish(candidate, publisher)
    manifest_path = tmp_path / "active-history-objects.json"

    export_active_dataset(
        connection,
        tmp_path / "active-dataset.sql",
        history_root=history_root,
        object_manifest=manifest_path,
    )

    written = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert written["dataset_id"] == "published"
    assert written["sql_checksum"]
    object_keys = {item["key"] for item in written["objects"]}
    assert object_keys == {history_object.key, chart_object.key}
    assert unrelated_old_chart.key not in object_keys


def test_export_bundle_serves_a_rolling_three_calendar_year_window(tmp_path: Path) -> None:
    """History older than the rolling window is written but not part of the bundle."""

    from market_pipeline.publication.d1_export import export_active_dataset

    connection = sqlite3.connect(":memory:")
    publisher = D1Publisher(connection)
    publisher.initialize_schema()

    history_root = tmp_path / "history"
    store = HistoryStore(history_root)
    written = store.write_history(
        "mutual_fund",
        "instrument-1",
        [
            {"effective_date": "2021-06-15", "value": "1"},  # outside the 2024-2026 window
            {"effective_date": "2024-01-05", "value": "2"},
            {"effective_date": "2025-01-05", "value": "3"},
            {"effective_date": "2026-09-01", "value": "4"},
        ],
    )
    chart_object = store.write_chart("published", "instrument-1", {"points": []})
    in_window_keys = {item.key for item in written if "/2021/" not in item.key}
    out_of_window_key = next(item.key for item in written if "/2021/" in item.key)

    candidate = _complete_candidate("published")
    bundle = build_bundle(
        dataset_id="published",
        input_manifest_hash="f" * 64,
        source_dates=["2026-09-01"],
        projection_version="d1-projection-v1",
        candidate_entries=[
            *(BundleObject(key=item.key, sha256=item.sha256, bytes=item.bytes, kind="history") for item in written),
            BundleObject(key=chart_object.key, sha256=chart_object.sha256, bytes=chart_object.bytes, kind="chart"),
        ],
        effective_date=date(2026, 9, 1),
    )
    candidate["metadata"]["publication_bundle"] = bundle.as_dict()
    publish(candidate, publisher)

    manifest_path = tmp_path / "active-history-objects.json"
    export_active_dataset(
        connection, tmp_path / "active-dataset.sql", history_root=history_root, object_manifest=manifest_path,
    )

    object_keys = {item["key"] for item in json.loads(manifest_path.read_text(encoding="utf-8"))["objects"]}
    assert in_window_keys <= object_keys
    assert out_of_window_key not in object_keys
    # The 2021 partition is still durably readable even though it is outside
    # what this bundle serves -- retention never destroys it.
    out_of_window_path = history_root / out_of_window_key
    assert out_of_window_path.is_file()


def test_publication_plan_reports_retained_objects_and_a_garbage_collection_dry_run(
    tmp_path: Path,
) -> None:
    from market_pipeline.publication.d1_export import export_active_dataset

    connection = sqlite3.connect(":memory:")
    publisher = D1Publisher(connection)
    publisher.initialize_schema()

    history_root = tmp_path / "history"
    store = HistoryStore(history_root)
    [history_object] = store.write_history("mutual_fund", "instrument-1", [{"effective_date": "2026-09-01", "value": "1"}])
    chart_object = store.write_chart("published", "instrument-1", {"points": []})
    # An object nothing references any more -- a prior candidate's orphan.
    unreferenced = store.write_chart("very-old-dataset", "instrument-1", {"points": [1]})

    candidate = _complete_candidate("published")
    bundle = build_bundle(
        dataset_id="published", input_manifest_hash="f" * 64, source_dates=["2026-09-01"],
        projection_version="d1-projection-v1",
        candidate_entries=[
            BundleObject(key=history_object.key, sha256=history_object.sha256, bytes=history_object.bytes, kind="history"),
            BundleObject(key=chart_object.key, sha256=chart_object.sha256, bytes=chart_object.bytes, kind="chart"),
        ],
        effective_date=date(2026, 9, 1),
    )
    candidate["metadata"]["publication_bundle"] = bundle.as_dict()
    publish(candidate, publisher)

    plan_path = tmp_path / "active-publication-plan.json"
    export_active_dataset(
        connection,
        tmp_path / "active-dataset.sql",
        history_root=history_root,
        object_manifest=tmp_path / "active-history-objects.json",
        publication_plan=plan_path,
    )

    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan["retained_objects"] == 2
    assert plan["retained_bytes"] > 0
    gc = plan["garbage_collection"]
    assert gc["dry_run"] is True
    assert gc["candidates"] == [unreferenced.key]
    assert gc["candidate_bytes"] > 0
    # Raw artifacts are never garbage-collection candidates, regardless of
    # what is reachable; there is nothing under raw/ in this fixture at all,
    # which is itself the point -- this function only ever looks at
    # history/ and charts/.
    assert all(not key.startswith("raw/") for key in gc["candidates"])


def test_remote_publication_preflight_rejects_a_weekday_month_over_budget() -> None:
    from market_pipeline.publication.preflight import (
        RemotePublicationBudgetError,
        assert_plan_within_free_tier,
    )

    with pytest.raises(RemotePublicationBudgetError, match="free-tier safety envelope"):
        assert_plan_within_free_tier(
            {
                "d1_mutations": 1,
                "r2_mutable_objects": 40_000,
                "r2_immutable_objects": 0,
                "weekday_runs_per_month": 22,
                "d1_import_bytes": 1,
                "snapshot_bytes": 1,
                "snapshot_ids": ["instrument-1"],
                "immutable_objects_by_instrument": {},
            },
            d1_info={"database_size": 1, "rows_written_24h": 1},
            remote_snapshot_ids=set(), remote_active_dataset_id="old",
        )


def test_remote_publication_preflight_rejects_current_remote_storage_and_same_day_writes() -> None:
    from market_pipeline.publication.preflight import (
        RemotePublicationBudgetError,
        assert_plan_within_free_tier,
    )

    plan = {
        "d1_mutations": 10_000,
        "r2_mutable_objects": 1,
        "r2_immutable_objects": 0,
        "snapshot_ids": ["instrument-1"],
        "immutable_objects_by_instrument": {},
        "weekday_runs_per_month": 22,
        "d1_import_bytes": 60_000_000,
        "snapshot_bytes": 40_000_000,
    }
    with pytest.raises(RemotePublicationBudgetError, match="storage"):
        assert_plan_within_free_tier(
            plan,
            d1_info={"database_size": 290_000_000, "rows_written_24h": 0},
            remote_snapshot_ids=set(), remote_active_dataset_id="old",
        )
    with pytest.raises(RemotePublicationBudgetError, match="same-day"):
        assert_plan_within_free_tier(
            plan,
            d1_info={"database_size": 1, "rows_written_24h": 85_000},
            remote_snapshot_ids=set(), remote_active_dataset_id="old",
        )


def test_active_export_plan_counts_snapshot_replacement_stale_delete_and_source_run_index(
    tmp_path: Path,
) -> None:
    from market_pipeline.publication.d1_export import export_active_dataset

    connection = sqlite3.connect(":memory:")
    publisher = D1Publisher(connection)
    publisher.initialize_schema()
    publish(_complete_candidate("previous"), publisher)
    publish(_complete_candidate("planned"), publisher)
    plan_path = tmp_path / "active-publication-plan.json"

    export_active_dataset(
        connection,
        tmp_path / "active-dataset.sql",
        publication_plan=plan_path,
    )

    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    assert plan["d1_mutations"] == 16  # dataset/pointer control + source-run index + in-place snapshot update
    assert plan["snapshot_ids"] == ["instrument-1"]
    assert 0 < plan["snapshot_bytes"] <= plan["d1_import_bytes"]


def test_stable_large_compact_universe_stays_within_daily_write_budget() -> None:
    from market_pipeline.publication.d1_export import _publication_plan
    from market_pipeline.publication.preflight import (
        RemotePublicationBudgetError,
        assert_plan_within_free_tier,
    )

    ids = [f"instrument-{index}" for index in range(12_000)]
    mutable = [f"charts/current/{item}.json.gz" for item in ids] + [f"history/equity/{item}/2026.parquet" for item in ids]
    immutable = [f"history/equity/{item}/2024.parquet" for item in ids] + [f"history/equity/{item}/2025.parquet" for item in ids]
    stable = _publication_plan(
        snapshot_ids=ids,
        source_count=1,
        source_run_count=1,
        manifest={"mutable": mutable, "immutable": immutable},
        d1_import_bytes=1,
        snapshot_bytes=1,
    )
    assert stable["r2_mutable_objects"] == 24_000
    assert stable["r2_immutable_objects"] == 24_000
    assert stable["d1_mutations"] < 50_000
    assert_plan_within_free_tier(stable, d1_info={"database_size": 1, "rows_written_24h": 1}, remote_snapshot_ids=set(ids), remote_active_dataset_id="old")
    assert_plan_within_free_tier(stable, d1_info={"database_size": 1, "rows_written_24h": 1}, remote_snapshot_ids=set(), remote_active_dataset_id=None)
    with pytest.raises(RemotePublicationBudgetError):
        assert_plan_within_free_tier({**stable, "r2_mutable_objects": 40_000}, d1_info={"database_size": 1, "rows_written_24h": 1}, remote_snapshot_ids=set(ids), remote_active_dataset_id="old")


def test_established_remote_new_instrument_bootstraps_only_its_closed_history() -> None:
    from market_pipeline.publication.preflight import assert_plan_within_free_tier

    plan = {"d1_mutations": 16, "d1_import_bytes": 1, "snapshot_bytes": 1, "snapshot_ids": ["old", "new"], "immutable_objects_by_instrument": {"new": 2}, "r2_mutable_objects": 4, "r2_immutable_objects": 2, "weekday_runs_per_month": 22}
    resolved = assert_plan_within_free_tier(plan, d1_info={"database_size": 1, "rows_written_24h": 1}, remote_snapshot_ids={"old"}, remote_active_dataset_id="old-dataset")
    assert resolved["new_snapshot_ids"] == ["new"]
    assert resolved["r2_class_a_monthly"] == 4 * 22 + 2


def test_remote_publication_state_rejects_duplicate_or_failed_wrangle_output() -> None:
    from market_pipeline.publication.preflight import (
        RemotePublicationBudgetError,
        parse_remote_publication_state,
    )

    with pytest.raises(RemotePublicationBudgetError):
        parse_remote_publication_state(
            [{"success": True, "results": [{"kind": "snapshot", "value": "duplicate"}, {"kind": "snapshot", "value": "duplicate"}]}]
        )
    with pytest.raises(RemotePublicationBudgetError):
        parse_remote_publication_state([{"success": False, "results": []}])


def test_active_dataset_export_rejects_a_remote_d1_statement_over_90kb(tmp_path: Path) -> None:
    from market_pipeline.publication.d1_export import DatasetExportError, export_active_dataset

    connection = sqlite3.connect(":memory:")
    publisher = D1Publisher(connection)
    publisher.initialize_schema()
    candidate = _complete_candidate("oversized")
    candidate["tables"]["instruments"][0]["name"] = "x" * 90_000
    publish(candidate, publisher)

    with pytest.raises(DatasetExportError, match="statement exceeds"):
        export_active_dataset(connection, tmp_path / "active-dataset.sql")


def test_remote_compact_projection_replaces_the_prior_dataset_snapshot(tmp_path: Path) -> None:
    from market_pipeline.publication.d1_export import export_active_dataset

    remote = sqlite3.connect(":memory:")
    remote_publisher = D1Publisher(remote)
    remote_publisher.initialize_schema()
    for dataset_id in ("first", "second"):
        local = sqlite3.connect(":memory:")
        local_publisher = D1Publisher(local)
        local_publisher.initialize_schema()
        publish(_complete_candidate(dataset_id), local_publisher)
        sql_path = tmp_path / f"{dataset_id}.sql"
        export_active_dataset(local, sql_path)
        remote.executescript(sql_path.read_text(encoding="utf-8"))

    assert remote.execute("SELECT dataset_id FROM instrument_snapshots").fetchall() == [("second",)]


def _complete_candidate(dataset_id: str) -> dict[str, Any]:
    return {
        "dataset_id": dataset_id,
        "effective_date": "2026-09-01",
        "metadata": {"required_sources": ["amfi-nav"]},
        "tables": {
            "sources": [{"source_id": "amfi-nav", "source_url": "https://www.amfiindia.com/nav", "terms_url": "https://www.amfiindia.com/terms", "status": "complete", "effective_date": "2026-09-01"}],
            "source_runs": [{"run_id": "run-1", "source_id": "amfi-nav", "effective_date": "2026-09-01", "status": "complete"}],
            "instruments": [{"instrument_id": "instrument-1", "provider": "amfi", "provider_identifier": "scheme-1", "asset_class": "mutual_fund"}],
            "latest_metrics": [{"instrument_id": "instrument-1", "effective_date": "2026-09-01", "metric": "return_1d", "value": 0.01, "state": "present"}],
        },
    }


def test_valid_candidate_promotes_as_one_dataset_view() -> None:
    connection = sqlite3.connect(":memory:")
    publisher = D1Publisher(connection)
    publisher.initialize_schema()
    publisher.seed_active("old")
    dataset_id = publish(_complete_candidate("new"), publisher)
    assert dataset_id == "new"
    assert publisher.active_dataset_id() == "new"
    assert connection.execute("SELECT status FROM datasets WHERE dataset_id='old'").fetchone()[0] == "superseded"
    assert connection.execute("SELECT dataset_id FROM latest_metrics").fetchone()[0] == "new"


def test_dataset_id_cannot_be_restaged_or_mutate_active_rows() -> None:
    connection = sqlite3.connect(":memory:")
    publisher = D1Publisher(connection)
    publisher.initialize_schema()
    publish(_complete_candidate("stable"), publisher)
    with pytest.raises(ReconciliationError):
        publisher.stage(_complete_candidate("stable"))
    assert publisher.active_dataset_id() == "stable"
    assert connection.execute("SELECT COUNT(*) FROM instruments WHERE dataset_id='stable'").fetchone()[0] == 1


@pytest.mark.parametrize(
    "candidate",
    [
        {"dataset_id": "empty", "tables": {}},
        {"dataset_id": "partial", "tables": {"instruments": [{"instrument_id": "i"}]}},
        {**_complete_candidate("missing-source"), "tables": {**_complete_candidate("missing-source")["tables"], "source_runs": []}},
    ],
)
def test_incomplete_candidate_cannot_promote(candidate: dict[str, Any]) -> None:
    publisher = D1Publisher(sqlite3.connect(":memory:"))
    publisher.initialize_schema()
    with pytest.raises(ReconciliationError):
        publish(candidate, publisher)
