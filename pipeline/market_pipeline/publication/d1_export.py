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

# Cloudflare's daily D1 write billing includes indexed-row updates. Keep the
# import at a single serving row per instrument rather than one EAV row per
# metric; the multipliers below count the primary and secondary index writes.
_REMOTE_TABLES = ("sources", "source_runs")
_WRITE_AMPLIFICATION = {"datasets": 2, "sources": 2, "source_runs": 3, "instrument_snapshots": 3}
_WEEKDAY_RUNS_PER_MONTH = 22
_MAX_D1_MUTATIONS_PER_RUN = 50_000
_MAX_R2_CLASS_A_PER_MONTH = 500_000
_MAX_R2_CLASS_B_PER_MONTH = 5_000_000
# D1's remote executor caps a single SQL statement at 100,000 bytes. Keep a
# margin for transport/implementation differences and reject before any R2
# upload can begin. A 100 MB import cap is also deliberately well below the
# 500 MB free database allowance; operators still monitor remote storage.
_MAX_D1_STATEMENT_BYTES = 90_000
_MAX_D1_IMPORT_BYTES = 100_000_000


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


def _upsert_snapshot(columns: Sequence[str], values: Sequence[Any]) -> str:
    names = ", ".join(columns)
    encoded = ", ".join(_literal(value) for value in values)
    updates = ", ".join(f"{column} = excluded.{column}" for column in columns if column != "instrument_id")
    return (
        f"INSERT INTO instrument_snapshots ({names}) VALUES ({encoded}) "
        f"ON CONFLICT(instrument_id) DO UPDATE SET {updates};"
    )


def _assert_sql_size(statements: Sequence[str]) -> int:
    total = 0
    for statement in statements:
        size = len(statement.encode("utf-8"))
        if size > _MAX_D1_STATEMENT_BYTES:
            raise DatasetExportError(
                f"remote D1 statement exceeds {_MAX_D1_STATEMENT_BYTES} UTF-8 bytes"
            )
        total += size + 1
    if total > _MAX_D1_IMPORT_BYTES:
        raise DatasetExportError(f"remote D1 import exceeds {_MAX_D1_IMPORT_BYTES} UTF-8 bytes")
    return total


def _rows(connection: sqlite3.Connection, table: str, dataset_id: str) -> Iterable[tuple[Any, ...]]:
    columns = _columns(connection, table)
    order = ", ".join(columns)
    yield from connection.execute(
        f"SELECT {order} FROM {table} WHERE dataset_id = ? ORDER BY {order}", (dataset_id,)
    )


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _snapshot_rows(connection: sqlite3.Connection, dataset_id: str) -> list[tuple[Any, ...]]:
    """Collapse local EAV/supporting rows into the remote serving projection."""

    groups: dict[str, dict[str, Any]] = {}
    for row in connection.execute(
        "SELECT instrument_id,symbol,name,asset_class,active,metadata_json FROM instruments "
        "WHERE dataset_id=? ORDER BY instrument_id",
        (dataset_id,),
    ):
        instrument_id = str(row[0])
        groups[instrument_id] = {
            "base": (instrument_id, dataset_id, row[1], row[2], row[3], row[4], row[5]),
            "values": {}, "metrics": [], "aliases": [], "periods": [], "actions": [],
        }
    for row in connection.execute(
        "SELECT instrument_id,metric,value,state,effective_date,raw_value,normalized_value,formula_version,"
        "source_artifact_id,metadata_json FROM latest_metrics WHERE dataset_id=? ORDER BY instrument_id,metric",
        (dataset_id,),
    ):
        group = groups.get(str(row[0]))
        if group is None:
            raise DatasetExportError("metric references an unknown active instrument")
        metric = str(row[1])
        group["values"][metric] = row[2] if row[3] == "present" else None
        group["metrics"].append({"metric": metric, "value": row[2], "state": row[3], "effectiveDate": row[4], "rawValue": row[5], "normalizedValue": row[6], "formulaVersion": row[7], "sourceArtifactId": row[8], "metadata": _json_value(row[9])})
    for table, columns, destination in (
        ("instrument_aliases", "instrument_id,provider,alias,valid_from,valid_to", "aliases"),
        ("fundamental_periods", "instrument_id,period_id,period_end,period_type,filing_id,filed_at,metrics_json,source_artifact_id,restates_id,supersedes_id", "periods"),
        ("corporate_actions", "instrument_id,action_id,action_date,action_type,numerator,denominator,metadata_json,source_artifact_id", "actions"),
    ):
        for row in connection.execute(f"SELECT {columns} FROM {table} WHERE dataset_id=? ORDER BY instrument_id", (dataset_id,)):
            group = groups.get(str(row[0]))
            if group is None:
                raise DatasetExportError(f"{table} references an unknown active instrument")
            if destination == "aliases":
                group[destination].append({"provider": row[1], "alias": row[2], "validFrom": row[3], "validTo": row[4]})
            elif destination == "periods":
                group[destination].append({"periodId": row[1], "periodEnd": row[2], "periodType": row[3], "filingId": row[4], "filedAt": row[5], "metrics": _json_value(row[6]), "sourceArtifactId": row[7], "restatesId": row[8], "supersedesId": row[9]})
            else:
                group[destination].append({"actionId": row[1], "actionDate": row[2], "actionType": row[3], "numerator": row[4], "denominator": row[5], "metadata": _json_value(row[6]), "sourceArtifactId": row[7]})
    return [
        (*group["base"], json.dumps(group["values"], sort_keys=True, separators=(",", ":")), json.dumps(group["metrics"], sort_keys=True, separators=(",", ":")), json.dumps(group["aliases"], sort_keys=True, separators=(",", ":")), json.dumps(group["periods"], sort_keys=True, separators=(",", ":")), json.dumps(group["actions"], sort_keys=True, separators=(",", ":")))
        for _, group in sorted(groups.items())
    ]


def _publication_plan(
    snapshot_count: int,
    stale_snapshot_count: int,
    source_count: int,
    source_run_count: int,
    history_keys: Sequence[str],
    *,
    d1_import_bytes: int,
    snapshot_bytes: int,
) -> dict[str, int]:
    history_count = sum(key.startswith("history/") for key in history_keys)
    chart_count = len(history_keys) - history_count
    # Current-year history grows on every session, so every manifest object is
    # PUT and then read back once for byte verification.
    return {
        # Six control writes cover two indexed dataset-status updates and the
        # indexed active pointer. An UPSERT changes each current snapshot once;
        # only IDs absent from the candidate are deleted as stale rows.
        "d1_mutations": 6 + _WRITE_AMPLIFICATION["datasets"] + source_count * _WRITE_AMPLIFICATION["sources"] + source_run_count * _WRITE_AMPLIFICATION["source_runs"] + snapshot_count * _WRITE_AMPLIFICATION["instrument_snapshots"] + stale_snapshot_count * _WRITE_AMPLIFICATION["instrument_snapshots"],
        "d1_import_bytes": d1_import_bytes,
        "snapshot_bytes": snapshot_bytes,
        "stale_snapshot_rows": stale_snapshot_count,
        "r2_class_a": history_count + chart_count,
        "r2_class_b": history_count + chart_count,
        "weekday_runs_per_month": _WEEKDAY_RUNS_PER_MONTH,
        "max_d1_mutations_per_run": _MAX_D1_MUTATIONS_PER_RUN,
        "max_r2_class_a_per_month": _MAX_R2_CLASS_A_PER_MONTH,
        "max_r2_class_b_per_month": _MAX_R2_CLASS_B_PER_MONTH,
    }


def _stale_snapshot_count(
    connection: sqlite3.Connection,
    dataset_id: str,
    snapshots: Sequence[Sequence[Any]],
) -> int:
    """Count exact stale IDs against the last locally promoted universe."""

    previous = connection.execute(
        "SELECT dataset_id FROM datasets WHERE status = 'superseded' AND dataset_id <> ? "
        "ORDER BY promoted_at DESC, created_at DESC LIMIT 1",
        (dataset_id,),
    ).fetchone()
    if previous is None:
        return 0
    prior_ids = {
        str(row[0])
        for row in connection.execute(
            "SELECT instrument_id FROM instruments WHERE dataset_id = ?",
            (previous[0],),
        )
    }
    current_ids = {str(row[0]) for row in snapshots}
    return len(prior_ids - current_ids)


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
    publication_plan: str | Path | None = None,
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

    snapshots = _snapshot_rows(connection, dataset_id)
    snapshot_columns = (
        "instrument_id", "dataset_id", "symbol", "name", "asset_class", "active", "metadata_json",
        "metric_values_json", "metric_rows_json", "aliases_json", "fundamental_periods_json", "corporate_actions_json",
    )
    statements = [
        "-- Generated from a locally reconciled active dataset. Do not add transaction wrappers.",
        _insert(
            "datasets",
            ("dataset_id", "status", "created_at", "promoted_at", "effective_date", "metadata_json"),
            (dataset[0], "staging", dataset[2], None, dataset[4], dataset[5]),
        ),
    ]
    for table in _REMOTE_TABLES:
        columns = _columns(connection, table)
        statements.extend(_insert(table, columns, row) for row in _rows(connection, table, dataset_id))
    snapshot_statements = [_upsert_snapshot(snapshot_columns, row) for row in snapshots]
    statements.extend(snapshot_statements)
    statements.extend((
        f"DELETE FROM instrument_snapshots WHERE dataset_id <> {_literal(dataset_id)};",
        f"UPDATE datasets SET status = 'superseded' WHERE status = 'active' AND dataset_id <> {_literal(dataset_id)};",
        f"UPDATE datasets SET status = 'active', promoted_at = {_literal(dataset[3])} WHERE dataset_id = {_literal(dataset_id)};",
        "INSERT INTO active_dataset (singleton, dataset_id, changed_at) VALUES "
        f"(1, {_literal(dataset_id)}, {_literal(pointer[0])}) "
        "ON CONFLICT(singleton) DO UPDATE SET "
        "dataset_id = excluded.dataset_id, changed_at = excluded.changed_at;",
        "",
    ))
    import_bytes = _assert_sql_size(statements)
    Path(output).write_text("\n".join(statements), encoding="utf-8")
    if manifest is not None and object_manifest is not None:
        Path(object_manifest).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    if publication_plan is not None:
        source_count = sum(1 for _ in _rows(connection, "sources", dataset_id))
        source_run_count = sum(1 for _ in _rows(connection, "source_runs", dataset_id))
        stale_snapshot_count = _stale_snapshot_count(connection, dataset_id, snapshots)
        snapshot_bytes = sum(len(statement.encode("utf-8")) + 1 for statement in snapshot_statements)
        plan = _publication_plan(
            len(snapshots),
            stale_snapshot_count,
            source_count,
            source_run_count,
            manifest or (),
            d1_import_bytes=import_bytes,
            snapshot_bytes=snapshot_bytes,
        )
        Path(publication_plan).write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return dataset_id


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="export the local active dataset as a D1 import")
    parser.add_argument("--db", required=True, help="local market SQLite database")
    parser.add_argument("--output", required=True, help="generated SQL path")
    parser.add_argument("--history-root", help="local root containing history/ and charts/ objects")
    parser.add_argument("--object-manifest", help="write the active dataset's verified R2 object keys here")
    parser.add_argument("--publication-plan", help="write the local-only remote-operation budget plan here")
    args = parser.parse_args(argv)
    connection = sqlite3.connect(args.db)
    try:
        dataset_id = export_active_dataset(
            connection,
            args.output,
            history_root=args.history_root,
            object_manifest=args.object_manifest,
            publication_plan=args.publication_plan,
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
