"""Resumable three-year source backfill using operator-supplied artifacts."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from typing import Any, Callable, Iterable, Mapping

from market_pipeline.domain.models import FetchedArtifact, SourceArtifact
from market_pipeline.sources.registry import SourcePolicyError


class CoverageError(RuntimeError):
    """Raised when a required source/date is missing and strict coverage is on."""


ArtifactFetcher = Callable[
    [str, date],
    Iterable[FetchedArtifact | SourceArtifact | tuple[SourceArtifact, bytes]],
]


def default_backfill_range(
    execution_date: date | None = None,
    latest_complete_date: date | None = None,
) -> tuple[date, date]:
    execution = execution_date or date.today()
    try:
        start = execution.replace(year=execution.year - 3)
    except ValueError:
        # Keep a true calendar-year lookback for leap-day execution dates.
        start = execution.replace(year=execution.year - 3, day=28)
    end = latest_complete_date or (execution - timedelta(days=1))
    if end < start:
        raise ValueError("latest complete date precedes three-year backfill start")
    return start, end


@dataclass(frozen=True)
class BackfillResult:
    start: date
    end: date
    completed: int
    skipped: int
    missing_dates: Mapping[str, tuple[date, ...]] = field(default_factory=dict)
    errors: tuple[str, ...] = ()

    @property
    def missing(self) -> Mapping[str, tuple[date, ...]]:
        return self.missing_dates


def _ensure_checkpoint_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS backfill_checkpoints ("
        "source_id TEXT NOT NULL, effective_date TEXT NOT NULL, checksum TEXT NOT NULL, "
        "object_key TEXT, completed_at TEXT NOT NULL, PRIMARY KEY(source_id,effective_date))"
    )
    connection.commit()


class BackfillJob:
    def __init__(
        self,
        database: sqlite3.Connection,
        source_ids: Iterable[str],
        fetcher: ArtifactFetcher,
        *,
        raw_store: Any | None = None,
        rate_limit_seconds: float = 0.0,
        sleeper: Callable[[float], None] = time.sleep,
        strict_coverage: bool = False,
    ) -> None:
        self.database = database
        self.source_ids = tuple(str(source) for source in source_ids)
        self.fetcher = fetcher
        self.raw_store = raw_store
        self.rate_limit_seconds = max(0.0, rate_limit_seconds)
        self.sleeper = sleeper
        self.strict_coverage = strict_coverage
        _ensure_checkpoint_table(database)

    def _checkpoint(self, source_id: str, effective_date: date) -> tuple[str, str | None] | None:
        row = self.database.execute(
            "SELECT checksum, object_key FROM backfill_checkpoints WHERE source_id=? AND effective_date=?",
            (source_id, effective_date.isoformat()),
        ).fetchone()
        return None if row is None else (str(row[0]), row[1])

    @staticmethod
    def _coerce_artifact(value: FetchedArtifact | SourceArtifact | tuple[SourceArtifact, bytes]) -> tuple[SourceArtifact, bytes | None]:
        if isinstance(value, FetchedArtifact):
            return value.artifact, value.body
        if isinstance(value, SourceArtifact):
            return value, None
        if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], SourceArtifact):
            return value[0], value[1]
        raise TypeError("fetcher must return FetchedArtifact, SourceArtifact, or (artifact, body)")

    def _save_checkpoint(self, source: str, effective: date, checksum: str, object_key: str | None) -> None:
        self.database.execute(
            "INSERT INTO backfill_checkpoints(source_id,effective_date,checksum,object_key,completed_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(source_id,effective_date) DO UPDATE SET checksum=excluded.checksum,object_key=excluded.object_key,completed_at=excluded.completed_at",
            (source, effective.isoformat(), checksum.lower(), object_key, datetime.now(UTC).isoformat()),
        )
        self.database.commit()

    def run(
        self,
        start: date | None = None,
        end: date | None = None,
        *,
        execution_date: date | None = None,
        latest_complete_date: date | None = None,
    ) -> BackfillResult:
        if start is None or end is None:
            default_start, default_end = default_backfill_range(execution_date, latest_complete_date)
            start = default_start if start is None else start
            end = default_end if end is None else end
        if end < start:
            raise ValueError("backfill end precedes start")
        completed = skipped = 0
        missing: dict[str, list[date]] = {source: [] for source in self.source_ids}
        errors: list[str] = []
        current = start
        first_fetch = True
        while current <= end:
            for source in self.source_ids:
                if not first_fetch and self.rate_limit_seconds:
                    self.sleeper(self.rate_limit_seconds)
                first_fetch = False
                try:
                    artifacts = [self._coerce_artifact(item) for item in self.fetcher(source, current)]
                except SourcePolicyError:
                    # In particular, disabled NSE automation must remain disabled;
                    # callers can retry with a provider for committed user files.
                    raise
                except Exception as exc:
                    errors.append(f"{source}/{current.isoformat()}: {exc}")
                    continue
                matching = [item for item in artifacts if item[0].effective_date == current]
                if not matching:
                    missing[source].append(current)
                    continue
                for artifact, body in matching:
                    if artifact.source_id != source:
                        errors.append(f"{source}/{current.isoformat()}: artifact source lineage mismatch")
                        continue
                    checksum = artifact.checksum.lower()
                    if body is not None and sha256(body).hexdigest() != checksum:
                        errors.append(f"{source}/{current.isoformat()}: checksum mismatch")
                        continue
                    prior = self._checkpoint(source, current)
                    if prior is not None and prior[0].lower() == checksum:
                        skipped += 1
                        continue
                    key = self.raw_store.put(artifact, body) if self.raw_store is not None and body is not None else None
                    self._save_checkpoint(source, current, checksum, key)
                    completed += 1
            current += timedelta(days=1)
        self.database.commit()
        frozen_missing = {source: tuple(dates) for source, dates in missing.items() if dates}
        if self.strict_coverage and frozen_missing:
            raise CoverageError(f"missing source dates: {frozen_missing}")
        return BackfillResult(start, end, completed, skipped, frozen_missing, tuple(errors))


def run_backfill(
    database: sqlite3.Connection,
    fetcher: ArtifactFetcher,
    source_ids: Iterable[str],
    *,
    start: date | None = None,
    end: date | None = None,
    raw_store: Any | None = None,
    execution_date: date | None = None,
    latest_complete_date: date | None = None,
    strict_coverage: bool = False,
) -> BackfillResult:
    return BackfillJob(database, source_ids, fetcher, raw_store=raw_store, strict_coverage=strict_coverage).run(
        start, end, execution_date=execution_date, latest_complete_date=latest_complete_date
    )


__all__ = ["ArtifactFetcher", "BackfillJob", "BackfillResult", "CoverageError", "default_backfill_range", "run_backfill"]
