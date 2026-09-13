from __future__ import annotations

import gzip
import json
import re
from datetime import date
from hashlib import sha256
from pathlib import Path

import pytest
from market_pipeline.storage.history_store import HistoryStore, HistoryStoreError, history_key

_KEY_PATTERN = re.compile(r"history/([a-z0-9_]+)/([a-zA-Z0-9_-]+)/(\d{4})/([0-9a-f]{64})\.parquet")


def _key_match(key: str) -> re.Match[str]:
    match = _KEY_PATTERN.fullmatch(key)
    assert match is not None
    return match


def test_history_partitions_are_content_addressed_and_chart_bytes_are_deterministic(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path)
    rows = [
        {"effective_date": "2024-01-02", "close": 101.0},
        {"effective_date": "2023-12-29", "close": 99.0},
    ]
    written = store.write_history("equity", "INE123", rows)
    keys = [item.key for item in written]
    for key, item in zip(keys, written):
        match = _KEY_PATTERN.fullmatch(key)
        assert match is not None
        assert match.group(4) == item.sha256
        assert item.bytes == len(Path(tmp_path / key).read_bytes())
    assert [int(_key_match(k).group(3)) for k in keys] == [2023, 2024]
    assert store.read_history("equity", "INE123", 2024, written[1].sha256)[0]["close"] == 101.0

    first = store.write_chart("dataset-a", "INE123", {"z": 1, "a": [2, 3]})
    body_first = (tmp_path / first.key).read_bytes()
    second = store.write_chart("dataset-a", "INE123", {"a": [2, 3], "z": 1})
    assert first == second
    assert (tmp_path / second.key).read_bytes() == body_first
    assert json.loads(gzip.decompress(body_first)) == {"a": [2, 3], "z": 1}


def test_same_year_append_produces_a_new_key_and_preserves_the_old_object(tmp_path: Path) -> None:
    """Appending a new day to the open year must never touch the prior key's bytes."""

    store = HistoryStore(tmp_path)
    year = date.today().year
    first_day = {"effective_date": f"{year}-01-02", "close": 101.0}
    second_day = {"effective_date": f"{year}-01-03", "close": 102.0}

    [original] = store.write_history("equity", "INE123", [first_day])
    [appended] = store.write_history("equity", "INE123", [first_day, second_day])

    assert appended.key != original.key
    assert store.client.get(original.key) is not None
    original_body = store.client.get(original.key)
    assert store.client.get(original.key) == original_body
    assert store.read_history("equity", "INE123", year, original.sha256)[0]["close"] == 101.0
    assert len(store.read_history("equity", "INE123", year, appended.sha256)) == 2


def test_closed_year_correction_produces_a_new_key_and_preserves_the_original(tmp_path: Path) -> None:
    """Correcting a value in an already-closed year must not overwrite the old key."""

    store = HistoryStore(tmp_path)
    year = date.today().year - 2

    [original] = store.write_history("equity", "INE123", [{"effective_date": f"{year}-06-15", "close": 100.0}])
    original_body = store.client.get(original.key)

    [corrected] = store.write_history("equity", "INE123", [{"effective_date": f"{year}-06-15", "close": 105.0}])

    assert corrected.key != original.key
    assert store.client.get(original.key) == original_body
    assert store.read_history("equity", "INE123", year, original.sha256)[0]["close"] == 100.0
    assert store.read_history("equity", "INE123", year, corrected.sha256)[0]["close"] == 105.0


def test_year_boundary_writes_are_partitioned_by_calendar_year(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path)
    written = store.write_history(
        "equity",
        "INE123",
        [
            {"effective_date": "2023-12-31", "close": 99.0},
            {"effective_date": "2024-01-01", "close": 101.0},
        ],
    )
    years = sorted(int(_key_match(item.key).group(3)) for item in written)
    assert years == [2023, 2024]
    by_year = {int(_key_match(item.key).group(3)): item for item in written}
    assert store.read_history("equity", "INE123", 2023, by_year[2023].sha256)[0]["close"] == 99.0
    assert store.read_history("equity", "INE123", 2024, by_year[2024].sha256)[0]["close"] == 101.0


def test_exact_retry_of_the_same_candidate_resolves_to_the_same_key(tmp_path: Path) -> None:
    """A retry with byte-identical inputs is a safe no-op, not a conflict."""

    store = HistoryStore(tmp_path)
    rows = [{"effective_date": "2025-03-01", "close": 50.0}]
    [first] = store.write_history("equity", "INE123", rows)
    [second] = store.write_history("equity", "INE123", rows)
    assert first == second
    assert store.client.get(first.key) is not None


def test_a_changed_body_under_a_client_supplied_fixed_key_is_rejected(tmp_path: Path) -> None:
    """The immutability guard still fires if a caller reuses a raw key directly."""

    store = HistoryStore(tmp_path)
    store.put_if_absent("history/equity/INE123/2024/" + ("a" * 64) + ".parquet", b"first")
    with pytest.raises(HistoryStoreError, match="immutable"):
        store.put_if_absent("history/equity/INE123/2024/" + ("a" * 64) + ".parquet", b"second")


def test_history_key_requires_a_valid_sha256_digest() -> None:
    with pytest.raises(HistoryStoreError, match="hex"):
        history_key("equity", "INE123", 2024, "not-a-digest")


def test_history_key_is_the_deterministic_hash_of_the_written_bytes(tmp_path: Path) -> None:
    store = HistoryStore(tmp_path)
    [written] = store.write_history("equity", "INE123", [{"effective_date": "2024-01-02", "close": 101.0}])
    body = (tmp_path / written.key).read_bytes()
    assert written.sha256 == sha256(body).hexdigest()
    assert written.key == history_key("equity", "INE123", 2024, written.sha256)
