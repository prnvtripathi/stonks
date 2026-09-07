from __future__ import annotations

import sqlite3
from typing import Any

import pytest
from market_pipeline.storage.d1_publisher import D1Publisher, ReconciliationError, publish


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
