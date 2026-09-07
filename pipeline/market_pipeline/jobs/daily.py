"""Single-date daily refresh built on the resumable backfill runner."""

from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any, Iterable

from market_pipeline.jobs.backfill import ArtifactFetcher, BackfillResult, run_backfill


def run_daily(
    database: sqlite3.Connection,
    fetcher: ArtifactFetcher,
    source_ids: Iterable[str],
    *,
    effective_date: date,
    raw_store: Any | None = None,
    strict_coverage: bool = True,
    budget_sources: Iterable[Any] = (),
) -> BackfillResult:
    return run_backfill(
        database,
        fetcher,
        source_ids,
        start=effective_date,
        end=effective_date,
        raw_store=raw_store,
        strict_coverage=strict_coverage,
        budget_sources=budget_sources,
    )


__all__ = ["run_daily"]
