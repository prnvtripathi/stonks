from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, date, datetime

from market_pipeline.domain.models import SourceArtifact
from market_pipeline.jobs.backfill import BackfillJob, default_backfill_range


def test_default_backfill_is_three_calendar_years_to_latest_complete_date() -> None:
    assert default_backfill_range(date(2026, 9, 7)) == (
        date(2023, 9, 7),
        date(2026, 9, 6),
    )


def test_backfill_checkpoints_make_retries_idempotent() -> None:
    connection = sqlite3.connect(":memory:")
    body = b"amfi"
    artifact = SourceArtifact(
        source_id="amfi-nav",
        source_url="https://www.amfiindia.com/spages/NAVAll.txt",
        retrieved_at=datetime.now(UTC),
        effective_date=date(2026, 9, 1),
        checksum=hashlib.sha256(body).hexdigest(),
        adapter_version="v1",
        terms_url="https://www.amfiindia.com/terms.html",
    )
    calls = 0

    def fetch(source: str, effective: date) -> list[tuple[SourceArtifact, bytes]]:
        nonlocal calls
        calls += 1
        return [(artifact, body)]

    first = BackfillJob(connection, ["amfi-nav"], fetch).run(date(2026, 9, 1), date(2026, 9, 1))
    second = BackfillJob(connection, ["amfi-nav"], fetch).run(date(2026, 9, 1), date(2026, 9, 1))
    assert (first.completed, first.skipped) == (1, 0)
    assert (second.completed, second.skipped) == (0, 1)
    assert calls == 2


def test_backfill_reports_missing_source_dates() -> None:
    result = BackfillJob(sqlite3.connect(":memory:"), ["amfi-nav"], lambda _source, _date: []).run(
        date(2026, 9, 1), date(2026, 9, 2)
    )
    assert result.missing_dates == {"amfi-nav": (date(2026, 9, 1), date(2026, 9, 2))}
