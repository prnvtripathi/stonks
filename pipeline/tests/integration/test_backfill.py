from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, date, datetime
from typing import Any

import pytest
from market_pipeline.domain.models import SourceArtifact
from market_pipeline.jobs.backfill import (
    BackfillIntegrityError,
    BackfillJob,
    default_backfill_range,
)


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


def test_two_artifacts_on_one_date_have_independent_checkpoints() -> None:
    connection = sqlite3.connect(":memory:")
    body_a, body_b = b"a", b"b"

    def artifact(body: bytes, filename: str) -> SourceArtifact:
        return SourceArtifact(
            source_id="amfi-nav", source_url="https://www.amfiindia.com/spages/NAVAll.txt",
            retrieved_at=datetime.now(UTC), effective_date=date(2026, 9, 1),
            checksum=hashlib.sha256(body).hexdigest(), adapter_version="v1",
            terms_url="https://www.amfiindia.com/terms.html", filename=filename,
        )

    records = [(artifact(body_a, "nav.csv"), body_a), (artifact(body_b, "meta.csv"), body_b)]
    first = BackfillJob(connection, ["amfi-nav"], lambda _source, _date: records).run(date(2026, 9, 1), date(2026, 9, 1))
    second = BackfillJob(connection, ["amfi-nav"], lambda _source, _date: list(reversed(records))).run(date(2026, 9, 1), date(2026, 9, 1))
    assert (first.completed, second.skipped) == (2, 2)
    assert connection.execute("SELECT COUNT(*) FROM backfill_checkpoints").fetchone()[0] == 2


def test_checksum_integrity_error_is_blocking() -> None:
    body = b"actual"
    artifact = SourceArtifact(
        source_id="amfi-nav", source_url="https://www.amfiindia.com/spages/NAVAll.txt",
        retrieved_at=datetime.now(UTC), effective_date=date(2026, 9, 1),
        checksum=hashlib.sha256(b"expected").hexdigest(), adapter_version="v1",
        terms_url="https://www.amfiindia.com/terms.html",
    )
    with pytest.raises(BackfillIntegrityError):
        BackfillJob(sqlite3.connect(":memory:"), ["amfi-nav"], lambda _source, _date: [(artifact, body)]).run(date(2026, 9, 1), date(2026, 9, 1))


def test_backfill_result_includes_storage_budget_warnings() -> None:
    class BudgetSource:
        def budget_report(self) -> Any:
            class Report:
                warning = True

                @staticmethod
                def as_dict() -> dict[str, object]:
                    return {"used": 80, "limit": 100, "ratio": 0.8, "warning": True}

            return Report()

    result = BackfillJob(
        sqlite3.connect(":memory:"), ["amfi-nav"], lambda _source, _date: [], budget_sources=(BudgetSource(),)
    ).run(date(2026, 9, 1), date(2026, 9, 1))
    assert result.warnings


def test_legacy_checkpoint_schema_is_upgraded_without_losing_rows() -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE backfill_checkpoints (source_id TEXT NOT NULL, effective_date TEXT NOT NULL, checksum TEXT NOT NULL, object_key TEXT, completed_at TEXT NOT NULL, PRIMARY KEY(source_id,effective_date))")
    connection.execute("INSERT INTO backfill_checkpoints VALUES ('amfi-nav','2026-09-01','abc','raw/key','2026-09-02T00:00:00Z')")
    BackfillJob(connection, ["amfi-nav"], lambda _source, _date: []).run(date(2026, 9, 2), date(2026, 9, 2))
    columns = {row[1] for row in connection.execute("PRAGMA table_info(backfill_checkpoints)")}
    assert "artifact_id" in columns
    row = connection.execute("SELECT source_id,effective_date,artifact_id,checksum,object_key FROM backfill_checkpoints").fetchone()
    assert row[:2] == ("amfi-nav", "2026-09-01")
    assert row[2].startswith("legacy-")
    assert row[3:] == ("abc", "raw/key")


def test_matching_retry_skips_migrated_legacy_checkpoint() -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE backfill_checkpoints (source_id TEXT NOT NULL, effective_date TEXT NOT NULL, checksum TEXT NOT NULL, object_key TEXT, completed_at TEXT NOT NULL, PRIMARY KEY(source_id,effective_date))")
    body = b"legacy body"
    checksum = hashlib.sha256(body).hexdigest()
    connection.execute("INSERT INTO backfill_checkpoints VALUES (?,?,?,?,?)", ("amfi-nav", "2026-09-01", checksum, "raw/legacy", "2026-09-02T00:00:00Z"))
    artifact = SourceArtifact(
        source_id="amfi-nav", source_url="https://www.amfiindia.com/spages/NAVAll.txt",
        retrieved_at=datetime.now(UTC), effective_date=date(2026, 9, 1), checksum=checksum,
        adapter_version="v1", terms_url="https://www.amfiindia.com/terms.html", filename="nav.txt",
    )
    result = BackfillJob(connection, ["amfi-nav"], lambda _source, _date: [(artifact, body)]).run(date(2026, 9, 1), date(2026, 9, 1))
    assert (result.completed, result.skipped) == (0, 1)


def test_changed_checksum_cannot_bypass_migrated_legacy_checkpoint() -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE backfill_checkpoints (source_id TEXT NOT NULL, effective_date TEXT NOT NULL, checksum TEXT NOT NULL, object_key TEXT, completed_at TEXT NOT NULL, PRIMARY KEY(source_id,effective_date))")
    old_body, new_body = b"old", b"new"
    connection.execute("INSERT INTO backfill_checkpoints VALUES (?,?,?,?,?)", ("amfi-nav", "2026-09-01", hashlib.sha256(old_body).hexdigest(), "raw/legacy", "2026-09-02T00:00:00Z"))
    artifact = SourceArtifact(
        source_id="amfi-nav", source_url="https://www.amfiindia.com/spages/NAVAll.txt",
        retrieved_at=datetime.now(UTC), effective_date=date(2026, 9, 1), checksum=hashlib.sha256(new_body).hexdigest(),
        adapter_version="v1", terms_url="https://www.amfiindia.com/terms.html", filename="nav.txt",
    )
    with pytest.raises(BackfillIntegrityError):
        BackfillJob(connection, ["amfi-nav"], lambda _source, _date: [(artifact, new_body)]).run(date(2026, 9, 1), date(2026, 9, 1))
