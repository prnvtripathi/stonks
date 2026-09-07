from __future__ import annotations

import sqlite3

import pytest
from market_pipeline.storage.budgets import StorageBudget
from market_pipeline.storage.d1_publisher import D1Publisher
from market_pipeline.storage.history_store import R2HistoryStore


@pytest.mark.parametrize("used, warning", [(79, False), (80, True), (81, True)])
def test_storage_budget_warning_threshold(used: int, warning: bool) -> None:
    budget = StorageBudget(used_bytes=used, limit_bytes=100)
    assert budget.used_bytes == used
    assert budget.limit_bytes == 100
    assert budget.ratio == used / 100
    assert budget.warning is warning


def test_publisher_exposes_configurable_budget() -> None:
    publisher = D1Publisher(sqlite3.connect(":memory:"), budget_limit_bytes=100)
    publisher.initialize_schema()
    report = publisher.budget_report()
    assert report.limit_bytes == 100
    assert report.used_bytes >= 0


def test_history_store_exposes_injected_object_usage() -> None:
    class Objects:
        def __init__(self) -> None:
            self.objects = {"charts/d/i.json.gz": b"1234567890"}

        def put_if_absent(self, key: str, body: bytes) -> bool:
            return False

        def get(self, key: str) -> bytes | None:
            return self.objects.get(key)

    report = R2HistoryStore(Objects(), budget_limit_bytes=10).budget_report()
    assert report.used == 10
    assert report.warning
