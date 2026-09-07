from __future__ import annotations

import gzip
import json
from pathlib import Path

from market_pipeline.storage.history_store import HistoryStore


def test_history_is_year_partitioned_and_chart_bytes_are_deterministic(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path)
    rows = [
        {"effective_date": "2024-01-02", "close": 101.0},
        {"effective_date": "2023-12-29", "close": 99.0},
    ]
    keys = store.write_history("equity", "INE123", rows)
    assert keys == [
        "history/equity/INE123/2023.parquet",
        "history/equity/INE123/2024.parquet",
    ]
    assert store.read_history("equity", "INE123", 2024)[0]["close"] == 101.0

    first = store.write_chart("dataset-a", "INE123", {"z": 1, "a": [2, 3]})
    body_first = (tmp_path / first).read_bytes()
    second = store.write_chart("dataset-a", "INE123", {"a": [2, 3], "z": 1})
    assert first == second
    assert (tmp_path / second).read_bytes() == body_first
    assert json.loads(gzip.decompress(body_first)) == {"a": [2, 3], "z": 1}

