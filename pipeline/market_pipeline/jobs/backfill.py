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


class BackfillIntegrityError(RuntimeError):
    """Raised immediately when an artifact violates lineage or checksum integrity."""


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
    warnings: tuple[str, ...] = ()

    @property
    def missing(self) -> Mapping[str, tuple[date, ...]]:
        return self.missing_dates


def upgrade_checkpoint_schema(connection: sqlite3.Connection) -> None:
    """Apply the 0002 forward migration when an old primary key is present."""

    table_exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='backfill_checkpoints'"
    ).fetchone() is not None
    if table_exists:
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(backfill_checkpoints)")}
        if "artifact_id" not in columns:
            connection.execute("ALTER TABLE backfill_checkpoints RENAME TO backfill_checkpoints_legacy")
            connection.execute(
                "CREATE TABLE backfill_checkpoints (source_id TEXT NOT NULL, effective_date TEXT NOT NULL, artifact_id TEXT NOT NULL, checksum TEXT NOT NULL, object_key TEXT, completed_at TEXT NOT NULL, PRIMARY KEY(source_id,effective_date,artifact_id))"
            )
            rows = connection.execute(
                "SELECT source_id,effective_date,checksum,object_key,completed_at FROM backfill_checkpoints_legacy"
            ).fetchall()
            for source, effective, checksum, object_key, completed_at in rows:
                identity = sha256(f"{source}|{effective}|{checksum}|{object_key or ''}".encode()).hexdigest()
                connection.execute(
                    "INSERT INTO backfill_checkpoints VALUES (?,?,?,?,?,?)",
                    (source, effective, f"legacy-{identity}", checksum, object_key, completed_at),
                )
            connection.execute("DROP TABLE backfill_checkpoints_legacy")
            connection.commit()
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(backfill_checkpoints)")}
        if "adapter_version" not in columns:
            # Historical checkpoints did not retain this semantic input. Keep
            # their uncertainty explicit so a current adapter cannot silently
            # share the same dataset fingerprint.
            connection.execute(
                "ALTER TABLE backfill_checkpoints ADD COLUMN adapter_version TEXT NOT NULL DEFAULT 'legacy-unknown'"
            )
            connection.commit()


def _ensure_checkpoint_table(connection: sqlite3.Connection) -> None:
    upgrade_checkpoint_schema(connection)
    connection.execute(
        "CREATE TABLE IF NOT EXISTS backfill_checkpoints ("
        "source_id TEXT NOT NULL, effective_date TEXT NOT NULL, artifact_id TEXT NOT NULL, checksum TEXT NOT NULL, "
        "object_key TEXT, adapter_version TEXT NOT NULL, completed_at TEXT NOT NULL, PRIMARY KEY(source_id,effective_date,artifact_id))"
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
        budget_sources: Iterable[Any] = (),
    ) -> None:
        self.database = database
        self.source_ids = tuple(str(source) for source in source_ids)
        self.fetcher = fetcher
        self.raw_store = raw_store
        self.rate_limit_seconds = max(0.0, rate_limit_seconds)
        self.sleeper = sleeper
        self.strict_coverage = strict_coverage
        self.budget_sources = tuple(budget_sources)
        _ensure_checkpoint_table(database)

    def _checkpoint(
        self,
        source_id: str,
        effective_date: date,
        artifact_id: str,
        checksum: str,
    ) -> tuple[str, str | None, str] | None:
        row = self.database.execute(
            "SELECT checksum, object_key, adapter_version FROM backfill_checkpoints WHERE source_id=? AND effective_date=? AND artifact_id=?",
            (source_id, effective_date.isoformat(), artifact_id),
        ).fetchone()
        if row is not None:
            return str(row[0]), row[1], str(row[2])
        # A pre-0002 row cannot be mapped to the canonical artifact ID because
        # old checkpoints did not retain filename/identity. Match only a single
        # legacy row by checksum; never guess when multiple legacy objects exist.
        legacy = self.database.execute(
            "SELECT checksum, object_key, adapter_version FROM backfill_checkpoints WHERE source_id=? AND effective_date=? AND artifact_id LIKE 'legacy-%'",
            (source_id, effective_date.isoformat()),
        ).fetchall()
        matches = [item for item in legacy if str(item[0]).lower() == checksum.lower()]
        if len(matches) == 1:
            return str(matches[0][0]), matches[0][1], str(matches[0][2])
        if legacy:
            if len(matches) > 1:
                raise BackfillIntegrityError(
                    f"{source_id}/{effective_date}: ambiguous legacy artifact checkpoint"
                )
            raise BackfillIntegrityError(
                f"{source_id}/{effective_date}: artifact checksum conflicts with legacy checkpoint"
            )
        return None

    @staticmethod
    def _coerce_artifact(value: FetchedArtifact | SourceArtifact | tuple[SourceArtifact, bytes]) -> tuple[SourceArtifact, bytes | None]:
        if isinstance(value, FetchedArtifact):
            return value.artifact, value.body
        if isinstance(value, SourceArtifact):
            return value, None
        if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], SourceArtifact):
            return value[0], value[1]
        raise TypeError("fetcher must return FetchedArtifact, SourceArtifact, or (artifact, body)")

    def _save_checkpoint(self, source: str, effective: date, artifact_id: str, checksum: str, object_key: str | None, adapter_version: str) -> None:
        self.database.execute(
            "INSERT INTO backfill_checkpoints(source_id,effective_date,artifact_id,checksum,object_key,adapter_version,completed_at) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(source_id,effective_date,artifact_id) DO UPDATE SET checksum=excluded.checksum,object_key=excluded.object_key,adapter_version=excluded.adapter_version,completed_at=excluded.completed_at",
            (source, effective.isoformat(), artifact_id, checksum.lower(), object_key, adapter_version, datetime.now(UTC).isoformat()),
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
                    raise BackfillIntegrityError(f"{source}/{current.isoformat()}: fetch failed: {exc}") from exc
                matching = [item for item in artifacts if item[0].effective_date == current]
                if not matching:
                    missing[source].append(current)
                    continue
                for artifact, body in matching:
                    if artifact.source_id != source:
                        raise BackfillIntegrityError(f"{source}/{current.isoformat()}: artifact source lineage mismatch")
                    checksum = artifact.checksum.lower()
                    if body is not None and sha256(body).hexdigest() != checksum:
                        raise BackfillIntegrityError(f"{source}/{current.isoformat()}: checksum mismatch")
                    artifact_id = str(artifact.artifact_id)
                    prior = self._checkpoint(source, current, artifact_id, checksum)
                    if prior is not None and prior[0].lower() == checksum and prior[2] in {artifact.adapter_version, "legacy-unknown"}:
                        skipped += 1
                        continue
                    if prior is not None and prior[0].lower() != checksum:
                        raise BackfillIntegrityError(f"{source}/{current.isoformat()}: artifact checksum changed for {artifact_id}")
                    key: str | None = None
                    if self.raw_store is not None and body is not None:
                        try:
                            key = self.raw_store.put(artifact, body)
                        except SourcePolicyError:
                            raise
                        except Exception as exc:
                            raise BackfillIntegrityError(f"{source}/{current.isoformat()}: artifact persistence failed: {exc}") from exc
                    if key is None:
                        key = f"raw/{source}/{current.isoformat()}/{checksum}/{artifact.filename}"
                    self._save_checkpoint(source, current, artifact_id, checksum, key, artifact.adapter_version)
                    completed += 1
            current += timedelta(days=1)
        self.database.commit()
        frozen_missing = {source: tuple(dates) for source, dates in missing.items() if dates}
        if self.strict_coverage and frozen_missing:
            raise CoverageError(f"missing source dates: {frozen_missing}")
        warnings: list[str] = []
        for source in self.budget_sources:
            report_method = getattr(source, "budget_report", None)
            if callable(report_method):
                report = report_method()
                if report.warning:
                    warnings.append(f"storage budget warning: {report.as_dict()}")
        return BackfillResult(start, end, completed, skipped, frozen_missing, tuple(errors), tuple(warnings))


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
    budget_sources: Iterable[Any] = (),
) -> BackfillResult:
    return BackfillJob(database, source_ids, fetcher, raw_store=raw_store, strict_coverage=strict_coverage, budget_sources=budget_sources).run(
        start, end, execution_date=execution_date, latest_complete_date=latest_complete_date
    )


__all__ = ["ArtifactFetcher", "BackfillIntegrityError", "BackfillJob", "BackfillResult", "CoverageError", "default_backfill_range", "run_backfill", "upgrade_checkpoint_schema"]
