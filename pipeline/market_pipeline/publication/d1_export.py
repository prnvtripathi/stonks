"""Create a deterministic, retry-safe D1 import for the local active dataset.

The resulting file intentionally contains no transaction wrapper. Cloudflare D1
rolls a failed ``wrangler d1 execute --remote --file`` import back to its
original state, while this exporter keeps the active-pointer change as the
last statements. R2 history/chart uploads are therefore a workflow precondition
for invoking this exporter remotely.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Sequence

from market_pipeline.storage.history_store import HistoryStoreError, chart_key, history_key


class DatasetExportError(RuntimeError):
    """Raised when the local active dataset is not safe to publish remotely."""


_DATASET_TABLES = (
    "sources",
    "source_runs",
    "instruments",
    "instrument_aliases",
    "latest_metrics",
    "fundamental_periods",
    "corporate_actions",
)


def _literal(value: Any) -> str:
    """Return a SQLite literal for values read from our local SQLite database."""

    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise DatasetExportError("active dataset contains a non-finite numeric value")
        return repr(value)
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    if isinstance(value, bytes):
        return "X'" + value.hex() + "'"
    raise DatasetExportError(f"active dataset contains unsupported SQL value {type(value).__name__}")


def _columns(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    columns = tuple(str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})"))
    if not columns:
        raise DatasetExportError(f"required table is absent: {table}")
    return columns


def _active_dataset(connection: sqlite3.Connection) -> tuple[str, sqlite3.Row]:
    pointer = connection.execute(
        "SELECT dataset_id, changed_at FROM active_dataset WHERE singleton = 1"
    ).fetchone()
    if pointer is None:
        raise DatasetExportError("active_dataset pointer is missing")
    dataset = connection.execute(
        "SELECT dataset_id,status,created_at,promoted_at,effective_date,metadata_json "
        "FROM datasets WHERE dataset_id = ?",
        (pointer[0],),
    ).fetchone()
    if dataset is None or str(dataset[1]) != "active":
        raise DatasetExportError("active_dataset pointer does not reference an active dataset")
    if not dataset[4]:
        raise DatasetExportError("active dataset has no effective date")
    try:
        metadata = json.loads(str(dataset[5]))
    except json.JSONDecodeError as exc:
        raise DatasetExportError("active dataset metadata is invalid") from exc
    if not isinstance(metadata, dict) or not metadata.get("valid", True) or not metadata.get("reconciliation_ok", True):
        raise DatasetExportError("active dataset did not pass reconciliation")
    return str(pointer[0]), dataset


def _assert_complete(connection: sqlite3.Connection, dataset_id: str, dataset: Sequence[Any]) -> None:
    for table in _DATASET_TABLES:
        _columns(connection, table)
    for table in ("sources", "source_runs", "instruments", "latest_metrics"):
        count = connection.execute(
            f"SELECT COUNT(*) FROM {table} WHERE dataset_id = ?", (dataset_id,)
        ).fetchone()[0]
        if not count:
            raise DatasetExportError(f"active dataset is incomplete: no {table}")
    try:
        metadata = json.loads(str(dataset[5]))
    except json.JSONDecodeError as exc:  # guarded in _active_dataset; narrows type here
        raise DatasetExportError("active dataset metadata is invalid") from exc
    required = metadata.get("required_sources", metadata.get("required_source_ids"))
    if isinstance(required, str):
        required = [required]
    if required is None:
        required = [row[0] for row in connection.execute(
            "SELECT source_id FROM sources WHERE dataset_id = ?", (dataset_id,)
        )]
    if not isinstance(required, list) or not required:
        raise DatasetExportError("active dataset has no required sources")
    effective = str(dataset[4])[:10]
    for source_id in required:
        completed = connection.execute(
            "SELECT 1 FROM source_runs WHERE dataset_id = ? AND source_id = ? "
            "AND effective_date = ? AND lower(status) IN ('complete', 'success') LIMIT 1",
            (dataset_id, str(source_id), effective),
        ).fetchone()
        if completed is None:
            raise DatasetExportError(f"active dataset has no completed source run: {source_id}/{effective}")


def _insert(table: str, columns: Sequence[str], values: Sequence[Any]) -> str:
    names = ", ".join(columns)
    encoded = ", ".join(_literal(value) for value in values)
    return f"INSERT OR IGNORE INTO {table} ({names}) VALUES ({encoded});"


def _rows(connection: sqlite3.Connection, table: str, dataset_id: str) -> Iterable[tuple[Any, ...]]:
    columns = _columns(connection, table)
    order = ", ".join(columns)
    yield from connection.execute(
        f"SELECT {order} FROM {table} WHERE dataset_id = ? ORDER BY {order}", (dataset_id,)
    )


def _manifest_keys(connection: sqlite3.Connection, dataset_id: str, history_root: Path) -> list[str]:
    """Return exactly the active dataset's chart and active instruments' history keys."""

    root = history_root.resolve()
    if not root.is_dir():
        raise DatasetExportError("history root is missing")
    keys: list[str] = []
    instruments = connection.execute(
        "SELECT instrument_id, asset_class FROM instruments WHERE dataset_id = ? "
        "ORDER BY asset_class, instrument_id",
        (dataset_id,),
    )
    try:
        for instrument_id, asset_class in instruments:
            chart = chart_key(dataset_id, str(instrument_id))
            chart_path = (root / chart).resolve()
            if not chart_path.is_file() or not chart_path.is_relative_to(root):
                raise DatasetExportError(f"active dataset chart is missing: {chart}")
            keys.append(chart)
            history_dir = (root / Path(history_key(str(asset_class), str(instrument_id), 2000)).parent).resolve()
            if not history_dir.is_relative_to(root):
                raise DatasetExportError(f"history directory escapes configured root: {instrument_id}")
            try:
                history_files = sorted(history_dir.glob("*.parquet"))
            except OSError as exc:
                raise DatasetExportError(f"active instrument history is unreadable: {instrument_id}") from exc
            if not history_files:
                raise DatasetExportError(f"active instrument has no history: {instrument_id}")
            for item in history_files:
                try:
                    key = history_key(str(asset_class), str(instrument_id), int(item.stem))
                except (HistoryStoreError, ValueError) as exc:
                    raise DatasetExportError(f"active instrument history key is invalid: {item}") from exc
                expected = (root / key).resolve()
                if not expected.is_file() or expected != item.resolve() or not expected.is_relative_to(root):
                    raise DatasetExportError(f"history object escapes configured root: {item}")
                keys.append(key)
    except HistoryStoreError as exc:
        raise DatasetExportError("active dataset contains an unsafe history key") from exc
    return sorted(keys)


def export_active_dataset(
    connection: sqlite3.Connection,
    output: str | Path,
    *,
    history_root: str | Path | None = None,
    object_manifest: str | Path | None = None,
) -> str:
    """Write the complete active snapshot and return its dataset ID.

    Only dataset-scoped market tables are serialized. Owner-managed saved
    screens and their historical runs/matches are deliberately omitted.
    """

    dataset_id, dataset = _active_dataset(connection)
    _assert_complete(connection, dataset_id, dataset)
    if (history_root is None) != (object_manifest is None):
        raise DatasetExportError("history_root and object_manifest must be supplied together")
    manifest: list[str] | None = None
    if history_root is not None:
        manifest = _manifest_keys(connection, dataset_id, Path(history_root))
    pointer = connection.execute(
        "SELECT changed_at FROM active_dataset WHERE singleton = 1"
    ).fetchone()
    if pointer is None:
        raise DatasetExportError("active_dataset pointer is missing")

    statements = [
        "-- Generated from a locally reconciled active dataset. Do not add transaction wrappers.",
        _insert(
            "datasets",
            ("dataset_id", "status", "created_at", "promoted_at", "effective_date", "metadata_json"),
            (dataset[0], "staging", dataset[2], None, dataset[4], dataset[5]),
        ),
    ]
    for table in _DATASET_TABLES:
        columns = _columns(connection, table)
        statements.extend(_insert(table, columns, row) for row in _rows(connection, table, dataset_id))
    statements.extend((
        f"UPDATE datasets SET status = 'superseded' WHERE status = 'active' AND dataset_id <> {_literal(dataset_id)};",
        f"UPDATE datasets SET status = 'active', promoted_at = {_literal(dataset[3])} WHERE dataset_id = {_literal(dataset_id)};",
        "INSERT INTO active_dataset (singleton, dataset_id, changed_at) VALUES "
        f"(1, {_literal(dataset_id)}, {_literal(pointer[0])}) "
        "ON CONFLICT(singleton) DO UPDATE SET "
        "dataset_id = excluded.dataset_id, changed_at = excluded.changed_at;",
        "",
    ))
    Path(output).write_text("\n".join(statements), encoding="utf-8")
    if manifest is not None and object_manifest is not None:
        Path(object_manifest).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return dataset_id


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="export the local active dataset as a D1 import")
    parser.add_argument("--db", required=True, help="local market SQLite database")
    parser.add_argument("--output", required=True, help="generated SQL path")
    parser.add_argument("--history-root", help="local root containing history/ and charts/ objects")
    parser.add_argument("--object-manifest", help="write the active dataset's verified R2 object keys here")
    args = parser.parse_args(argv)
    connection = sqlite3.connect(args.db)
    try:
        dataset_id = export_active_dataset(
            connection,
            args.output,
            history_root=args.history_root,
            object_manifest=args.object_manifest,
        )
    except DatasetExportError as exc:
        parser.error(str(exc))
    finally:
        connection.close()
    print(dataset_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["DatasetExportError", "export_active_dataset", "main"]
