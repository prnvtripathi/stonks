from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from market_pipeline.analytics.momentum import NormalizedMomentumInput, momentum_score
from market_pipeline.jobs.publish import DatasetBuild, publish_checkpointed_dataset
from market_pipeline.storage.d1_publisher import D1Publisher, ReconciliationError, publish
from market_pipeline.storage.history_store import (
    HistoryStore,
    HistoryStoreError,
    chart_key,
    history_key,
)


def test_corporate_actions_forward_migration_upgrades_existing_and_fresh_databases() -> None:
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

    fresh = sqlite3.connect(":memory:")
    D1Publisher(fresh).initialize_schema()
    assert fresh.execute("PRAGMA table_info(corporate_actions)").fetchall()
    assert fresh.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_corporate_actions_instrument'").fetchone() is not None


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
    from market_pipeline.publication.d1_export import export_active_dataset

    connection = sqlite3.connect(":memory:")
    publisher = D1Publisher(connection)
    publisher.initialize_schema()
    publish(_complete_candidate("published"), publisher)
    history_root = tmp_path / "history"
    store = HistoryStore(history_root)
    store.write_history("mutual_fund", "instrument-1", [{"effective_date": "2026-09-01", "value": "1"}])
    store.write_chart("published", "instrument-1", {"points": []})
    store.write_chart("old-dataset", "instrument-1", {"points": []})
    manifest_path = tmp_path / "active-history-objects.json"

    export_active_dataset(
        connection,
        tmp_path / "active-dataset.sql",
        history_root=history_root,
        object_manifest=manifest_path,
    )

    assert json.loads(manifest_path.read_text(encoding="utf-8")) == [
        chart_key("published", "instrument-1"),
        history_key("mutual_fund", "instrument-1", 2026),
    ]


def test_remote_publication_preflight_rejects_a_weekday_month_over_budget() -> None:
    from market_pipeline.publication.preflight import (
        RemotePublicationBudgetError,
        assert_plan_within_free_tier,
    )

    with pytest.raises(RemotePublicationBudgetError, match="free-tier safety envelope"):
        assert_plan_within_free_tier(
            {
                "d1_mutations": 1,
                "r2_class_a": 30_000,
                "r2_class_b": 1,
                "weekday_runs_per_month": 22,
                "d1_import_bytes": 1,
                "snapshot_bytes": 1,
            },
            d1_info={"database_size": 1, "rows_written_24h": 1},
        )


def test_remote_publication_preflight_rejects_current_remote_storage_and_same_day_writes() -> None:
    from market_pipeline.publication.preflight import (
        RemotePublicationBudgetError,
        assert_plan_within_free_tier,
    )

    plan = {
        "d1_mutations": 10_000,
        "r2_class_a": 1,
        "r2_class_b": 1,
        "weekday_runs_per_month": 22,
        "d1_import_bytes": 60_000_000,
        "snapshot_bytes": 40_000_000,
    }
    with pytest.raises(RemotePublicationBudgetError, match="storage"):
        assert_plan_within_free_tier(
            plan,
            d1_info={"database_size": 290_000_000, "rows_written_24h": 0},
        )
    with pytest.raises(RemotePublicationBudgetError, match="same-day"):
        assert_plan_within_free_tier(
            plan,
            d1_info={"database_size": 1, "rows_written_24h": 85_000},
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
    assert plan["stale_snapshot_rows"] == 0
    assert 0 < plan["snapshot_bytes"] <= plan["d1_import_bytes"]


def test_stable_large_compact_universe_stays_within_daily_write_budget() -> None:
    from market_pipeline.publication.d1_export import _publication_plan

    stable = _publication_plan(
        snapshot_count=12_000,
        stale_snapshot_count=0,
        source_count=1,
        source_run_count=1,
        history_keys=(),
        d1_import_bytes=1,
        snapshot_bytes=1,
    )
    stale = _publication_plan(
        snapshot_count=12_000,
        stale_snapshot_count=2,
        source_count=1,
        source_run_count=1,
        history_keys=(),
        d1_import_bytes=1,
        snapshot_bytes=1,
    )

    assert stable["d1_mutations"] < 50_000
    assert stale["d1_mutations"] == stable["d1_mutations"] + 6
    assert stale["stale_snapshot_rows"] == 2


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
