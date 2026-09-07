from __future__ import annotations

import sqlite3

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


def test_valid_candidate_promotes_as_one_dataset_view() -> None:
    connection = sqlite3.connect(":memory:")
    publisher = D1Publisher(connection)
    publisher.initialize_schema()
    publisher.seed_active("old")
    dataset_id = publish(
        {
            "dataset_id": "new",
            "tables": {
                "instruments": [
                    {
                        "instrument_id": "instrument-1",
                        "provider": "nse",
                        "provider_identifier": "INE1",
                        "asset_class": "equity",
                        "symbol": "ONE",
                    }
                ],
                "latest_metrics": [
                    {
                        "instrument_id": "instrument-1",
                        "effective_date": "2026-09-01",
                        "metric": "return_1d",
                        "value": 0.01,
                        "state": "present",
                    }
                ],
            },
        },
        publisher,
    )
    assert dataset_id == "new"
    assert publisher.active_dataset_id() == "new"
    assert connection.execute("SELECT status FROM datasets WHERE dataset_id='old'").fetchone()[0] == "superseded"
    assert connection.execute("SELECT dataset_id FROM latest_metrics").fetchone()[0] == "new"
