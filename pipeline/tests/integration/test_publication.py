from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from market_pipeline.analytics.momentum import NormalizedMomentumInput, momentum_score
from market_pipeline.storage.d1_publisher import D1Publisher, ReconciliationError, publish


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
