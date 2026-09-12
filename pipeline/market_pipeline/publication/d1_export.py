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
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence, cast

from market_pipeline.publication.bundle import (
    BundleError,
    PublicationBundle,
    plan_garbage_collection,
)
from market_pipeline.publication.bundle import (
    sql_checksum as bundle_sql_checksum,
)


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
_MAX_R2_CLASS_A_PER_MONTH = 800_000
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


def _dataset_row(connection: sqlite3.Connection, dataset_id: str) -> sqlite3.Row:
    """Fetch one dataset row by ID, regardless of its current status.

    Shared by the active-pointer export path and R11's selective restore
    path (``export_dataset``): both need the same reconciliation/effective-
    date sanity checks, differing only in *which* dataset_id is selected.
    """

    dataset = connection.execute(
        "SELECT dataset_id,status,created_at,promoted_at,effective_date,metadata_json "
        "FROM datasets WHERE dataset_id = ?",
        (dataset_id,),
    ).fetchone()
    if dataset is None:
        raise DatasetExportError(f"dataset not found: {dataset_id}")
    if not dataset[4]:
        raise DatasetExportError("active dataset has no effective date")
    try:
        metadata = json.loads(str(dataset[5]))
    except json.JSONDecodeError as exc:
        raise DatasetExportError("active dataset metadata is invalid") from exc
    if not isinstance(metadata, dict) or not metadata.get("valid", True) or not metadata.get("reconciliation_ok", True):
        raise DatasetExportError("active dataset did not pass reconciliation")
    return cast(sqlite3.Row, dataset)


def _active_dataset(connection: sqlite3.Connection) -> tuple[str, sqlite3.Row]:
    pointer = connection.execute(
        "SELECT dataset_id, changed_at FROM active_dataset WHERE singleton = 1"
    ).fetchone()
    if pointer is None:
        raise DatasetExportError("active_dataset pointer is missing")
    dataset = _dataset_row(connection, str(pointer[0]))
    if str(dataset[1]) != "active":
        raise DatasetExportError("active_dataset pointer does not reference an active dataset")
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
    snapshot_ids: Sequence[str],
    source_count: int,
    source_run_count: int,
    manifest: Mapping[str, Sequence[str]],
    *,
    d1_import_bytes: int,
    snapshot_bytes: int,
    retained_objects: int = 0,
    retained_bytes: int = 0,
    garbage_collection: Mapping[str, Any] | None = None,
    weekday_runs_per_month: int | None = None,
) -> dict[str, Any]:
    mutable = list(manifest["mutable"])
    immutable = list(manifest["immutable"])
    immutable_by_instrument: dict[str, int] = {}
    for key in immutable:
        parts = key.split("/")
        if parts[0] != "history" or len(parts) not in (4, 5):
            raise DatasetExportError("immutable manifest key is invalid")
        immutable_by_instrument[parts[2]] = immutable_by_instrument.get(parts[2], 0) + 1
    return {
        # Six control writes cover two indexed dataset-status updates and the
        # indexed active pointer. An UPSERT changes each current snapshot once;
        # only IDs absent from the candidate are deleted as stale rows.
        "d1_mutations": 6 + _WRITE_AMPLIFICATION["datasets"] + source_count * _WRITE_AMPLIFICATION["sources"] + source_run_count * _WRITE_AMPLIFICATION["source_runs"] + len(snapshot_ids) * _WRITE_AMPLIFICATION["instrument_snapshots"],
        "d1_import_bytes": d1_import_bytes,
        "snapshot_bytes": snapshot_bytes,
        "snapshot_ids": list(snapshot_ids),
        "r2_mutable_objects": len(mutable),
        "r2_immutable_objects": len(immutable),
        "immutable_objects_by_instrument": immutable_by_instrument,
        # Objects kept reachable for rollback (the active bundle plus the
        # previous successfully-promoted bundle) and a dry-run cleanup plan
        # for everything else this run found unreferenced. See R07/F14 and
        # docs/operations/retention.md.
        "retained_objects": retained_objects,
        "retained_bytes": retained_bytes,
        "garbage_collection": dict(garbage_collection) if garbage_collection is not None else {
            "dry_run": True, "candidate_count": 0, "candidate_bytes": 0, "candidates": [],
        },
        # F11/F14: the caller (the CLI's own default computes this from the
        # actual calendar month via ``remote_usage.plan_monthly_attempts``)
        # may override the stable module constant used by existing,
        # deterministic unit tests below.
        "weekday_runs_per_month": weekday_runs_per_month if weekday_runs_per_month is not None else _WEEKDAY_RUNS_PER_MONTH,
        "max_d1_mutations_per_run": _MAX_D1_MUTATIONS_PER_RUN,
        "max_r2_class_a_per_month": _MAX_R2_CLASS_A_PER_MONTH,
        "max_r2_class_b_per_month": _MAX_R2_CLASS_B_PER_MONTH,
    }


def load_recorded_bundle(connection: sqlite3.Connection, dataset_id: str) -> PublicationBundle:
    """Read this dataset's own recorded publication bundle.

    The bundle is the authoritative record of exactly what this candidate
    wrote (see ``jobs/publish.py``); the exporter never re-derives it by
    scanning the object store.
    """

    row = connection.execute("SELECT metadata_json FROM datasets WHERE dataset_id = ?", (dataset_id,)).fetchone()
    if row is None:
        raise DatasetExportError("active dataset row is missing")
    try:
        metadata = json.loads(str(row[0]))
    except json.JSONDecodeError as exc:
        raise DatasetExportError("active dataset metadata is invalid") from exc
    data = metadata.get("publication_bundle") if isinstance(metadata, dict) else None
    if not data:
        raise DatasetExportError("active dataset has no recorded publication bundle")
    try:
        bundle = PublicationBundle.from_dict(data)
    except BundleError as exc:
        raise DatasetExportError(f"active dataset publication bundle is invalid: {exc}") from exc
    if bundle.dataset_id != dataset_id:
        raise DatasetExportError("publication bundle dataset_id does not match the active dataset")
    return bundle


def verify_bundle_objects(bundle: PublicationBundle, history_root: Path) -> None:
    """Byte-verify exactly the bundle's own objects; never glob for extras."""

    root = history_root.resolve()
    if not root.is_dir():
        raise DatasetExportError("history root is missing")
    for entry in bundle.objects:
        path = (root / entry.key).resolve()
        if not path.is_relative_to(root):
            raise DatasetExportError(f"bundle object escapes configured root: {entry.key}")
        try:
            body = path.read_bytes()
        except OSError as exc:
            raise DatasetExportError(f"bundle object is missing: {entry.key}") from exc
        if len(body) != entry.bytes or sha256(body).hexdigest() != entry.sha256:
            raise DatasetExportError(f"bundle object does not match its recorded checksum: {entry.key}")


def _bundle_object_groups(bundle: PublicationBundle) -> dict[str, list[str]]:
    """Classify a bundle's objects into write-cadence groups for the R09 budget.

    Chart objects and each instrument's newest (currently open) history year
    are written on every run; a closed year's object already exists from an
    earlier run and is only ever read back. This grouping is a cost estimate
    for ``_publication_plan``, not a claim about mutability: every object
    here is written exactly once, under a content-addressed key.
    """

    newest_year_by_instrument: dict[str, int] = {}
    for entry in bundle.objects:
        if entry.kind != "history":
            continue
        parts = entry.key.split("/")
        if len(parts) != 5 or parts[0] != "history":
            raise DatasetExportError(f"bundle history object key has an unexpected shape: {entry.key}")
        instrument_id, year = parts[2], int(parts[3])
        newest_year_by_instrument[instrument_id] = max(year, newest_year_by_instrument.get(instrument_id, year))
    mutable: list[str] = []
    immutable: list[str] = []
    for entry in bundle.objects:
        if entry.kind == "chart":
            mutable.append(entry.key)
            continue
        instrument_id, year = entry.key.split("/")[2], int(entry.key.split("/")[3])
        (mutable if year == newest_year_by_instrument[instrument_id] else immutable).append(entry.key)
    return {"mutable": sorted(mutable), "immutable": sorted(immutable)}


def _existing_object_sizes(history_root: Path) -> dict[str, int]:
    """Inventory derived objects already present in the store, for GC dry-run only.

    This is the one place this module intentionally scans the store: garbage
    collection is, by definition, a question about what exists that no
    reachable bundle references, and that cannot be answered without looking.
    It never decides what a candidate *needs* -- that always comes from a
    bundle.
    """

    root = history_root.resolve()
    sizes: dict[str, int] = {}
    for prefix in ("history", "charts"):
        base = root / prefix
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if path.is_file():
                sizes[path.relative_to(root).as_posix()] = path.stat().st_size
    return sizes


def reachable_bundles(connection: sqlite3.Connection, dataset_id: str) -> list[PublicationBundle]:
    """Bundles that must stay reachable: the active dataset and the previous promotion.

    Keeping the previous successfully-promoted dataset's bundle reachable is
    what makes rollback possible after a bad promotion. Rows are never
    deleted by this module; datasets simply move from ``active`` to
    ``superseded``.
    """

    rows = connection.execute(
        "SELECT metadata_json FROM datasets WHERE status IN ('active','superseded') "
        "ORDER BY CASE WHEN dataset_id = ? THEN 0 ELSE 1 END, promoted_at DESC LIMIT 2",
        (dataset_id,),
    ).fetchall()
    bundles: list[PublicationBundle] = []
    for (metadata_json,) in rows:
        try:
            metadata = json.loads(str(metadata_json))
            data = metadata.get("publication_bundle") if isinstance(metadata, dict) else None
            if data:
                bundles.append(PublicationBundle.from_dict(data))
        except (json.JSONDecodeError, BundleError):
            continue
    return bundles


def export_active_dataset(
    connection: sqlite3.Connection,
    output: str | Path,
    *,
    history_root: str | Path | None = None,
    object_manifest: str | Path | None = None,
    publication_plan: str | Path | None = None,
    weekday_runs_per_month: int | None = None,
) -> str:
    """Write the complete active snapshot and return its dataset ID.

    Only dataset-scoped market tables are serialized. Owner-managed saved
    screens and their historical runs/matches are deliberately omitted.
    """

    dataset_id, dataset = _active_dataset(connection)
    return _export_dataset_sql(
        connection,
        dataset_id,
        dataset,
        output,
        history_root=history_root,
        object_manifest=object_manifest,
        publication_plan=publication_plan,
        weekday_runs_per_month=weekday_runs_per_month,
    )


def export_dataset(
    connection: sqlite3.Connection,
    dataset_id: str,
    output: str | Path,
    *,
    history_root: str | Path | None = None,
    object_manifest: str | Path | None = None,
    publication_plan: str | Path | None = None,
    weekday_runs_per_month: int | None = None,
) -> str:
    """Export an explicitly-selected, still-retained dataset as a D1 import (R11/F13).

    This is the same import-SQL shape and re-promotion mechanics as
    :func:`export_active_dataset` -- the dataset is simply selected by an
    explicit ``dataset_id`` (which must currently be ``active`` or
    ``superseded``) instead of always the current active pointer. This is
    what :mod:`market_pipeline.publication.restore` uses to reimport a
    retained, previously-promoted dataset's compact projection: a real
    selective restore rather than a pointer-only flip, which
    ``db/migrations/0006_bounded_instrument_snapshots.sql`` made unsafe (a
    prior promotion has already physically deleted every other dataset's
    ``instrument_snapshots`` rows).
    """

    dataset = _dataset_row(connection, dataset_id)
    if str(dataset[1]) not in ("active", "superseded"):
        raise DatasetExportError(
            f"dataset is not eligible for restore (status={dataset[1]!r}): {dataset_id}"
        )
    return _export_dataset_sql(
        connection,
        dataset_id,
        dataset,
        output,
        history_root=history_root,
        object_manifest=object_manifest,
        publication_plan=publication_plan,
        weekday_runs_per_month=weekday_runs_per_month,
    )


def _export_dataset_sql(
    connection: sqlite3.Connection,
    dataset_id: str,
    dataset: sqlite3.Row,
    output: str | Path,
    *,
    history_root: str | Path | None = None,
    object_manifest: str | Path | None = None,
    publication_plan: str | Path | None = None,
    weekday_runs_per_month: int | None = None,
) -> str:
    """Shared statement-building body for both export entry points above."""

    _assert_complete(connection, dataset_id, dataset)
    if (history_root is None) != (object_manifest is None):
        raise DatasetExportError("history_root and object_manifest must be supplied together")
    bundle: PublicationBundle | None = None
    manifest: dict[str, list[str]] | None = None
    gc_plan_dict: dict[str, Any] | None = None
    retained_objects = 0
    retained_bytes = 0
    if history_root is not None:
        root = Path(history_root)
        bundle = load_recorded_bundle(connection, dataset_id)
        verify_bundle_objects(bundle, root)
        manifest = _bundle_object_groups(bundle)
        reachable = reachable_bundles(connection, dataset_id)
        if bundle.dataset_id not in {item.dataset_id for item in reachable}:
            reachable = [bundle, *reachable]
        existing_sizes = _existing_object_sizes(root)
        gc_plan = plan_garbage_collection(reachable, existing_sizes)
        gc_plan_dict = gc_plan.as_dict()
        reachable_keys = {key for item in reachable for key in item.object_keys}
        retained_objects = len(reachable_keys)
        retained_bytes = sum(existing_sizes.get(key, 0) for key in reachable_keys)
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
    sql_text = "\n".join(statements)
    Path(output).write_text(sql_text, encoding="utf-8")
    if bundle is not None and object_manifest is not None:
        finalized = bundle.with_sql_checksum(bundle_sql_checksum(sql_text))
        Path(object_manifest).write_text(json.dumps(finalized.as_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if publication_plan is not None:
        source_count = sum(1 for _ in _rows(connection, "sources", dataset_id))
        source_run_count = sum(1 for _ in _rows(connection, "source_runs", dataset_id))
        snapshot_bytes = sum(len(statement.encode("utf-8")) + 1 for statement in snapshot_statements)
        plan = _publication_plan(
            [str(row[0]) for row in snapshots],
            source_count,
            source_run_count,
            manifest or {"mutable": [], "immutable": []},
            d1_import_bytes=import_bytes,
            snapshot_bytes=snapshot_bytes,
            retained_objects=retained_objects,
            retained_bytes=retained_bytes,
            garbage_collection=gc_plan_dict,
            weekday_runs_per_month=weekday_runs_per_month,
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
    parser.add_argument(
        "--weekday-runs-per-month",
        type=int,
        default=None,
        help="defaults to the actual current month's scheduled attempts (see remote_usage.plan_monthly_attempts)",
    )
    args = parser.parse_args(argv)
    weekday_runs_per_month = args.weekday_runs_per_month
    if weekday_runs_per_month is None and args.publication_plan:
        # F11/F14: compute the real month's schedule rather than trust a
        # hard-coded constant. This must match whatever
        # ``preflight``'s CLI independently computes for the same day, so
        # both derive it from the same ``remote_usage.plan_monthly_attempts``
        # rather than each hard-coding their own guess.
        from market_pipeline.publication.remote_usage import plan_monthly_attempts

        weekday_runs_per_month = plan_monthly_attempts(date.today()).monthly_attempts
    connection = sqlite3.connect(args.db)
    try:
        dataset_id = export_active_dataset(
            connection,
            args.output,
            history_root=args.history_root,
            object_manifest=args.object_manifest,
            publication_plan=args.publication_plan,
            weekday_runs_per_month=weekday_runs_per_month,
        )
    except DatasetExportError as exc:
        parser.error(str(exc))
    finally:
        connection.close()
    print(dataset_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DatasetExportError",
    "export_active_dataset",
    "export_dataset",
    "load_recorded_bundle",
    "main",
    "reachable_bundles",
    "verify_bundle_objects",
]
