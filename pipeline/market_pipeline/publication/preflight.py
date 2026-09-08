"""Fail closed before a workflow can mutate remote D1 or R2 resources."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

WEEKDAY_RUNS_PER_MONTH = 22
MAX_D1_MUTATIONS_PER_RUN = 50_000
MAX_D1_ROWS_WRITTEN_24H = 90_000
MAX_D1_DATABASE_BYTES = 400_000_000
_D1_IMPORT_STORAGE_MULTIPLIER = 2
MAX_R2_CLASS_A_PER_MONTH = 800_000
MAX_R2_CLASS_B_PER_MONTH = 5_000_000


class RemotePublicationBudgetError(RuntimeError):
    """Raised when a plan would exceed the deliberately conservative envelope."""


def parse_remote_publication_state(payload: Any) -> tuple[set[str], str | None]:
    """Strictly decode Wrangler's successful snapshot/active-state query."""

    result = payload[0] if isinstance(payload, list) and len(payload) == 1 else payload
    if not isinstance(result, Mapping) or result.get("success") is not True:
        raise RemotePublicationBudgetError("invalid remote publication state")
    rows = result.get("results")
    if not isinstance(rows, list):
        raise RemotePublicationBudgetError("invalid remote publication state")
    ids: set[str] = set()
    active: str | None = None
    for row in rows:
        if not isinstance(row, Mapping) or row.get("kind") not in {"snapshot", "active_dataset"}:
            raise RemotePublicationBudgetError("invalid remote publication state")
        value = row.get("value")
        if not isinstance(value, str) or not value or any(char in value for char in "\x00\n\r"):
            raise RemotePublicationBudgetError("invalid remote publication state")
        if row["kind"] == "snapshot":
            if value in ids:
                raise RemotePublicationBudgetError("invalid remote publication state")
            ids.add(value)
        elif active is not None:
            raise RemotePublicationBudgetError("invalid remote publication state")
        else:
            active = value
    return ids, active


def assert_plan_within_free_tier(
    plan: Mapping[str, Any],
    *,
    d1_info: Mapping[str, Any],
    remote_snapshot_ids: set[str],
    remote_active_dataset_id: str | None,
    max_d1_mutations_per_run: int = MAX_D1_MUTATIONS_PER_RUN,
    max_d1_rows_written_24h: int = MAX_D1_ROWS_WRITTEN_24H,
    max_d1_database_bytes: int = MAX_D1_DATABASE_BYTES,
    max_r2_class_a_per_month: int = MAX_R2_CLASS_A_PER_MONTH,
    max_r2_class_b_per_month: int = MAX_R2_CLASS_B_PER_MONTH,
    weekday_runs_per_month: int = WEEKDAY_RUNS_PER_MONTH,
) -> dict[str, Any]:
    required = (
        "d1_mutations",
        "r2_mutable_objects",
        "r2_immutable_objects",
        "weekday_runs_per_month",
        "d1_import_bytes",
        "snapshot_bytes",
    )
    if any(not isinstance(plan.get(key), int) or isinstance(plan.get(key), bool) or int(plan[key]) < 0 for key in required):
        raise RemotePublicationBudgetError("invalid remote publication plan")
    info_fields = ("database_size", "rows_written_24h")
    if any(not isinstance(d1_info.get(key), int) or isinstance(d1_info.get(key), bool) or int(d1_info[key]) < 0 for key in info_fields):
        raise RemotePublicationBudgetError("invalid remote D1 capacity information")
    if int(plan["weekday_runs_per_month"]) != weekday_runs_per_month:
        raise RemotePublicationBudgetError("remote publication plan has an unexpected weekday envelope")
    if int(plan["snapshot_bytes"]) > int(plan["d1_import_bytes"]):
        raise RemotePublicationBudgetError("invalid remote publication plan")
    snapshot_ids = plan.get("snapshot_ids")
    if not isinstance(snapshot_ids, list) or any(not isinstance(item, str) or not item for item in snapshot_ids) or len(snapshot_ids) != len(set(snapshot_ids)):
        raise RemotePublicationBudgetError("invalid remote publication plan")
    stale_count = len(remote_snapshot_ids - set(snapshot_ids))
    new_snapshot_ids = set(snapshot_ids) - remote_snapshot_ids
    immutable_by_instrument = plan.get("immutable_objects_by_instrument")
    if not isinstance(immutable_by_instrument, Mapping) or any(
        not isinstance(key, str) or key not in set(snapshot_ids)
        or not isinstance(value, int) or isinstance(value, bool) or value < 0
        for key, value in immutable_by_instrument.items()
    ):
        raise RemotePublicationBudgetError("invalid remote publication plan")
    new_immutable = sum(int(immutable_by_instrument.get(item, 0)) for item in new_snapshot_ids)
    mutations = int(plan["d1_mutations"]) + stale_count * 3
    mutable = int(plan["r2_mutable_objects"])
    immutable = int(plan["r2_immutable_objects"])
    class_a = mutable * weekday_runs_per_month + (immutable if remote_active_dataset_id is None else new_immutable)
    class_b = (mutable + immutable) * weekday_runs_per_month + (immutable if remote_active_dataset_id is None else 0)
    if (
        mutations > max_d1_mutations_per_run
        or class_a > max_r2_class_a_per_month
        or class_b > max_r2_class_b_per_month
    ):
        raise RemotePublicationBudgetError("remote publication plan exceeds the free-tier safety envelope")
    # The active snapshot is bounded and replaces prior rows, but `d1 info`
    # does not expose its reclaimable logical size. Give it no storage credit
    # and reserve a second copy of the SQL payload for table/index overhead.
    if int(d1_info["database_size"]) + int(plan["d1_import_bytes"]) * _D1_IMPORT_STORAGE_MULTIPLIER > max_d1_database_bytes:
        raise RemotePublicationBudgetError("remote D1 storage would exceed the safety envelope")
    if int(d1_info["rows_written_24h"]) + mutations > max_d1_rows_written_24h:
        raise RemotePublicationBudgetError("remote D1 same-day write usage would exceed the safety envelope")
    return {**plan, "d1_mutations": mutations, "stale_snapshot_rows": stale_count, "new_snapshot_ids": sorted(new_snapshot_ids), "remote_active_dataset_id": remote_active_dataset_id, "r2_class_a_monthly": class_a, "r2_class_b_monthly": class_b}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="validate a local-only remote publication plan")
    parser.add_argument("--plan", required=True)
    parser.add_argument("--d1-info", required=True, help="read-only JSON from wrangler d1 info")
    parser.add_argument("--remote-publication-state", required=True)
    parser.add_argument("--resolved-plan", required=True)
    parser.add_argument("--max-d1-mutations-per-run", type=int, default=MAX_D1_MUTATIONS_PER_RUN)
    parser.add_argument("--max-d1-rows-written-24h", type=int, default=MAX_D1_ROWS_WRITTEN_24H)
    parser.add_argument("--max-d1-database-bytes", type=int, default=MAX_D1_DATABASE_BYTES)
    parser.add_argument("--max-r2-class-a-per-month", type=int, default=MAX_R2_CLASS_A_PER_MONTH)
    parser.add_argument("--max-r2-class-b-per-month", type=int, default=MAX_R2_CLASS_B_PER_MONTH)
    parser.add_argument("--weekday-runs-per-month", type=int, default=WEEKDAY_RUNS_PER_MONTH)
    args = parser.parse_args(argv)
    try:
        plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        d1_info = json.loads(Path(args.d1_info).read_text(encoding="utf-8"))
        remote_state = json.loads(Path(args.remote_publication_state).read_text(encoding="utf-8"))
        if not isinstance(plan, dict) or not isinstance(d1_info, dict):
            raise RemotePublicationBudgetError("invalid remote publication plan")
        remote_ids, active = parse_remote_publication_state(remote_state)
        resolved = assert_plan_within_free_tier(
            plan,
            d1_info=d1_info,
            remote_snapshot_ids=remote_ids,
            remote_active_dataset_id=active,
            max_d1_mutations_per_run=args.max_d1_mutations_per_run,
            max_d1_rows_written_24h=args.max_d1_rows_written_24h,
            max_d1_database_bytes=args.max_d1_database_bytes,
            max_r2_class_a_per_month=args.max_r2_class_a_per_month,
            max_r2_class_b_per_month=args.max_r2_class_b_per_month,
            weekday_runs_per_month=args.weekday_runs_per_month,
        )
        Path(args.resolved_plan).write_text(json.dumps(resolved, sort_keys=True) + "\n", encoding="utf-8")
    except (OSError, json.JSONDecodeError, RemotePublicationBudgetError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["RemotePublicationBudgetError", "assert_plan_within_free_tier", "main"]
