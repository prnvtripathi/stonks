from __future__ import annotations

import gzip
import json
from datetime import date
from pathlib import Path

import pytest
from market_pipeline.storage.history_store import HistoryStore, HistoryStoreError


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


def test_current_year_history_replaces_the_first_day_with_a_two_day_series(tmp_path: Path) -> None:
    """The current year grows each session, unlike closed history partitions."""

    store = HistoryStore(tmp_path)
    year = date.today().year
    first_day = {"effective_date": f"{year}-01-02", "close": 101.0}
    second_day = {"effective_date": f"{year}-01-03", "close": 102.0}

    store.write_history("equity", "INE123", [first_day])
    store.write_history("equity", "INE123", [first_day, second_day])

    assert store.read_history("equity", "INE123", year) == [
        {"effective_date": first_day["effective_date"], "close": 101.0},
        {"effective_date": second_day["effective_date"], "close": 102.0},
    ]


def test_newest_history_year_in_the_supplied_series_is_mutable(tmp_path: Path) -> None:
    """Manual/backfill series may advance a historical year deterministically."""

    store = HistoryStore(tmp_path)
    newest_year = date.today().year - 1
    earlier_year = newest_year - 1
    earlier = {"effective_date": f"{earlier_year}-12-31", "close": 99.0}
    first_latest = {"effective_date": f"{newest_year}-01-02", "close": 101.0}
    second_latest = {"effective_date": f"{newest_year}-01-03", "close": 102.0}

    store.write_history("equity", "INE123", [earlier, first_latest])
    store.write_history("equity", "INE123", [earlier, first_latest, second_latest])
    assert len(store.read_history("equity", "INE123", newest_year)) == 2

    with pytest.raises(HistoryStoreError, match="immutable"):
        store.write_history(
            "equity",
            "INE123",
            [{"effective_date": f"{earlier_year}-12-31", "close": 98.0}, first_latest, second_latest],
        )


def test_current_year_history_requires_a_replacing_object_client() -> None:
    class ImmutableOnlyClient:
        def put_if_absent(self, key: str, body: bytes) -> bool:
            return True

        def get(self, key: str) -> bytes | None:
            return None

    with pytest.raises(HistoryStoreError, match="replacement"):
        HistoryStore(ImmutableOnlyClient()).write_history(  # type: ignore[arg-type]
            "equity",
            "INE123",
            [{"effective_date": f"{date.today().year}-01-02", "close": 101.0}],
        )
