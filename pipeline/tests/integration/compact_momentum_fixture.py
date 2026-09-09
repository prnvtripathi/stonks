"""Emit a real compact-export row for the Worker publication contract test."""

from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

from market_pipeline.publication.d1_export import export_active_dataset
from market_pipeline.storage.d1_publisher import D1Publisher, publish


def compact_momentum_fixture() -> dict[str, object]:
    """Build, export, and re-import the compact row used by the API contract test."""

    local = sqlite3.connect(":memory:")
    publisher = D1Publisher(local)
    publisher.initialize_schema()
    publish(
        {
            "dataset_id": "momentum-contract",
            "effective_date": "2026-09-07",
            "metadata": {"required_sources": ["amfi-nav"]},
            "tables": {
                "sources": [{"source_id": "amfi-nav", "source_url": "https://example.test/nav", "terms_url": "https://example.test/terms", "status": "complete", "effective_date": "2026-09-07"}],
                "source_runs": [{"run_id": "amfi-run", "source_id": "amfi-nav", "effective_date": "2026-09-07", "status": "complete"}],
                "instruments": [{"instrument_id": "mf-1", "provider": "amfi", "provider_identifier": "scheme-1", "asset_class": "mutual_fund", "symbol": "MF1", "name": "Momentum Fund"}],
                "latest_metrics": [
                    {"instrument_id": "mf-1", "effective_date": "2026-09-07", "metric": "momentum_score", "value": 0.6, "state": "present", "formula_version": "momentum-v2-cohort", "metadata": {"cohort": "mutual_fund:equity", "coverage": 0.85, "source_date": "2026-09-07", "warning": "Thin category coverage"}},
                    {"instrument_id": "mf-1", "effective_date": "2026-09-07", "metric": "momentum_three_month_return", "value": 12.0, "state": "present", "raw_value": "12.0", "normalized_value": 0.6, "formula_version": "momentum-v2-cohort", "metadata": {"unit": "percent", "raw": 12.0, "normalized": 0.6, "weight": 0.2, "contribution": 0.12, "cohort": "mutual_fund:equity"}},
                ],
            },
        },
        publisher,
    )
    with tempfile.TemporaryDirectory() as directory:
        sql_path = Path(directory) / "active.sql"
        export_active_dataset(local, sql_path)
        remote = sqlite3.connect(":memory:")
        D1Publisher(remote).initialize_schema()
        remote.executescript(sql_path.read_text(encoding="utf-8"))
        row = remote.execute(
            "SELECT instrument_id, symbol, name, asset_class, active, metadata_json, metric_rows_json, fundamental_periods_json, corporate_actions_json FROM instrument_snapshots WHERE dataset_id = ? AND instrument_id = ?",
            ("momentum-contract", "mf-1"),
        ).fetchone()
    assert row is not None
    return {
        "instrument_id": row[0], "symbol": row[1], "name": row[2], "asset_class": row[3], "active": row[4],
        "metadata_json": row[5], "metric_rows_json": row[6], "fundamental_periods_json": row[7], "corporate_actions_json": row[8],
    }


if __name__ == "__main__":
    print(json.dumps(compact_momentum_fixture(), sort_keys=True))
