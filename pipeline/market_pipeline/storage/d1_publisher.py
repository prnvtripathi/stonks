"""Versioned SQLite/D1 publication with a transactional active pointer."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence, cast


class ReconciliationError(RuntimeError):
    """Raised when a candidate dataset cannot be safely promoted."""


_TABLES = {
    "sources",
    "source_runs",
    "instruments",
    "instrument_aliases",
    "latest_metrics",
    "fundamental_periods",
    "saved_screens",
    "screen_runs",
    "screen_matches",
    "glossary_entries",
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class DatasetCandidate:
    dataset_id: str
    tables: Mapping[str, Sequence[Mapping[str, Any]]] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    effective_date: date | str | None = None
    valid: bool = True
    reconciliation_ok: bool = True

    @classmethod
    def from_value(cls, value: DatasetCandidate | Mapping[str, Any]) -> DatasetCandidate:
        if isinstance(value, cls):
            return value
        raw = dict(cast(Mapping[str, Any], value))
        tables = raw.pop("tables", raw.pop("data", {}))
        metadata = raw.pop("metadata", raw.pop("metadata_json", {}))
        dataset_id = raw.pop("dataset_id", raw.pop("id", None))
        if not dataset_id:
            raise ReconciliationError("candidate dataset_id is required")
        if not isinstance(tables, Mapping):
            raise ReconciliationError("candidate tables must be a mapping")
        if not isinstance(metadata, Mapping):
            metadata = {"value": metadata}
        return cls(
            dataset_id=str(dataset_id),
            tables={str(key): tuple(rows) for key, rows in tables.items()},
            metadata=dict(metadata),
            effective_date=raw.pop("effective_date", None),
            valid=bool(raw.pop("valid", True)),
            reconciliation_ok=bool(raw.pop("reconciliation_ok", raw.pop("reconciled", True))),
        )


class D1Publisher:
    """Publish datasets to a local SQLite database or SQLite-compatible D1 client.

    A candidate is first loaded as ``staging``.  Promotion updates exactly one
    singleton row in ``active_dataset`` in the same transaction that marks the
    candidate active, so readers see either the old or the new complete view.
    """

    def __init__(self, database: sqlite3.Connection | str | Path) -> None:
        if isinstance(database, sqlite3.Connection):
            self.connection = database
            self._owns_connection = False
        else:
            self.connection = sqlite3.connect(str(database))
            self._owns_connection = True
        self.connection.execute("PRAGMA foreign_keys = ON")

    def close(self) -> None:
        if self._owns_connection:
            self.connection.close()

    def initialize_schema(self) -> None:
        migration = Path(__file__).resolve().parents[3] / "db" / "migrations" / "0001_market_schema.sql"
        self.connection.executescript(migration.read_text(encoding="utf-8"))
        self.connection.commit()

    def active_dataset_id(self) -> str | None:
        row = self.connection.execute(
            "SELECT dataset_id FROM active_dataset WHERE singleton = 1"
        ).fetchone()
        return None if row is None else str(row[0])

    def seed_active(self, dataset_id: str) -> None:
        """Test/operator helper to establish a known-good initial dataset."""

        now = _now()
        self.connection.execute(
            "INSERT OR IGNORE INTO datasets(dataset_id,status,created_at,promoted_at,metadata_json) "
            "VALUES(?, 'active', ?, ?, '{}')",
            (dataset_id, now, now),
        )
        self.connection.execute(
            "INSERT INTO active_dataset(singleton,dataset_id,changed_at) VALUES(1,?,?) "
            "ON CONFLICT(singleton) DO UPDATE SET dataset_id=excluded.dataset_id, changed_at=excluded.changed_at",
            (dataset_id, now),
        )
        self.connection.commit()

    @staticmethod
    def _validate_candidate(candidate: DatasetCandidate) -> None:
        if not candidate.valid or not candidate.reconciliation_ok:
            raise ReconciliationError(f"dataset candidate {candidate.dataset_id!r} failed reconciliation")
        if not candidate.dataset_id or any(char in candidate.dataset_id for char in "\x00\n\r"):
            raise ReconciliationError("dataset ID is invalid")
        unknown = set(candidate.tables) - _TABLES
        if unknown:
            raise ReconciliationError(f"candidate contains unknown tables: {sorted(unknown)}")
        for table, rows in candidate.tables.items():
            if not isinstance(rows, Sequence):
                raise ReconciliationError(f"candidate rows for {table} must be a sequence")
            for row in rows:
                if not isinstance(row, Mapping):
                    raise ReconciliationError(f"candidate row in {table} must be a mapping")

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)

    def stage(self, value: DatasetCandidate | Mapping[str, Any]) -> str:
        candidate = DatasetCandidate.from_value(value)
        self._validate_candidate(candidate)
        metadata = self._json(candidate.metadata)
        effective = candidate.effective_date.isoformat() if isinstance(candidate.effective_date, date) else candidate.effective_date
        try:
            self.connection.execute("BEGIN")
            self.connection.execute(
                "INSERT INTO datasets(dataset_id,status,created_at,effective_date,metadata_json) VALUES(?, 'staging', ?, ?, ?) "
                "ON CONFLICT(dataset_id) DO UPDATE SET status='staging', effective_date=excluded.effective_date, metadata_json=excluded.metadata_json",
                (candidate.dataset_id, _now(), effective, metadata),
            )
            for table, rows in candidate.tables.items():
                self._replace_rows(table, candidate.dataset_id, rows)
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return candidate.dataset_id

    def _replace_rows(self, table: str, dataset_id: str, rows: Sequence[Mapping[str, Any]]) -> None:
        columns = [str(row_key) for row in rows for row_key in row.keys()]
        ordered = list(dict.fromkeys(["dataset_id", *columns]))
        available = {
            str(item[1])
            for item in self.connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        columns = [column for column in ordered if column in available]
        self.connection.execute(f"DELETE FROM {table} WHERE dataset_id = ?", (dataset_id,))
        if not rows or len(columns) <= 1:
            return
        placeholders = ",".join("?" for _ in columns)
        sql = f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})"
        for row in rows:
            values = [dataset_id if column == "dataset_id" else self._db_value(row.get(column)) for column in columns]
            self.connection.execute(sql, values)

    @classmethod
    def _db_value(cls, value: Any) -> Any:
        if isinstance(value, (Mapping, list, tuple)):
            return cls._json(value)
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        if isinstance(value, bool):
            return int(value)
        return value

    def promote(self, value: DatasetCandidate | Mapping[str, Any] | str) -> str:
        if isinstance(value, str):
            dataset_id = value
            row = self.connection.execute(
                "SELECT metadata_json FROM datasets WHERE dataset_id = ? AND status = 'staging'", (dataset_id,)
            ).fetchone()
            if row is None:
                raise ReconciliationError(f"staged dataset not found: {dataset_id}")
            try:
                metadata = json.loads(row[0])
            except json.JSONDecodeError as exc:
                raise ReconciliationError("staged dataset metadata is invalid") from exc
            candidate = DatasetCandidate(dataset_id=dataset_id, metadata=metadata)
        else:
            candidate = DatasetCandidate.from_value(value)
            self._validate_candidate(candidate)
            dataset_id = candidate.dataset_id
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                "SELECT status, metadata_json FROM datasets WHERE dataset_id = ?", (dataset_id,)
            ).fetchone()
            if row is None or row[0] != "staging":
                raise ReconciliationError(f"dataset is not staged: {dataset_id}")
            if not candidate.valid or not candidate.reconciliation_ok:
                raise ReconciliationError(f"dataset candidate {dataset_id!r} failed reconciliation")
            # A row missing dataset_id is impossible through this publisher; the
            # check guards manually altered staging rows from being promoted.
            for table in _TABLES:
                bad = self.connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE dataset_id = ?", (dataset_id,)
                ).fetchone()
                if bad is None:
                    raise ReconciliationError(f"cannot reconcile table {table}")
            now = _now()
            self.connection.execute(
                "UPDATE datasets SET status='superseded' WHERE status='active' AND dataset_id <> ?", (dataset_id,)
            )
            self.connection.execute(
                "UPDATE datasets SET status='active', promoted_at=? WHERE dataset_id=?", (now, dataset_id)
            )
            self.connection.execute(
                "INSERT INTO active_dataset(singleton,dataset_id,changed_at) VALUES(1,?,?) "
                "ON CONFLICT(singleton) DO UPDATE SET dataset_id=excluded.dataset_id,changed_at=excluded.changed_at",
                (dataset_id, now),
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return dataset_id


def publish(candidate: DatasetCandidate | Mapping[str, Any], publisher: D1Publisher) -> str:
    """Stage and atomically promote a candidate dataset."""

    dataset_id = publisher.stage(candidate)
    return publisher.promote(dataset_id)


__all__ = ["DatasetCandidate", "D1Publisher", "ReconciliationError", "publish"]
