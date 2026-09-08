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
MAX_R2_CLASS_A_PER_MONTH = 500_000
MAX_R2_CLASS_B_PER_MONTH = 5_000_000


class RemotePublicationBudgetError(RuntimeError):
    """Raised when a plan would exceed the deliberately conservative envelope."""


def assert_plan_within_free_tier(
    plan: Mapping[str, Any],
    *,
    d1_info: Mapping[str, Any],
    max_d1_mutations_per_run: int = MAX_D1_MUTATIONS_PER_RUN,
    max_d1_rows_written_24h: int = MAX_D1_ROWS_WRITTEN_24H,
    max_d1_database_bytes: int = MAX_D1_DATABASE_BYTES,
    max_r2_class_a_per_month: int = MAX_R2_CLASS_A_PER_MONTH,
    max_r2_class_b_per_month: int = MAX_R2_CLASS_B_PER_MONTH,
    weekday_runs_per_month: int = WEEKDAY_RUNS_PER_MONTH,
) -> None:
    required = (
        "d1_mutations",
        "r2_class_a",
        "r2_class_b",
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
    if (
        int(plan["d1_mutations"]) > max_d1_mutations_per_run
        or int(plan["r2_class_a"]) * weekday_runs_per_month > max_r2_class_a_per_month
        or int(plan["r2_class_b"]) * weekday_runs_per_month > max_r2_class_b_per_month
    ):
        raise RemotePublicationBudgetError("remote publication plan exceeds the free-tier safety envelope")
    # The active snapshot is bounded and replaces prior rows, but `d1 info`
    # does not expose its reclaimable logical size. Give it no storage credit
    # and reserve a second copy of the SQL payload for table/index overhead.
    if int(d1_info["database_size"]) + int(plan["d1_import_bytes"]) * _D1_IMPORT_STORAGE_MULTIPLIER > max_d1_database_bytes:
        raise RemotePublicationBudgetError("remote D1 storage would exceed the safety envelope")
    if int(d1_info["rows_written_24h"]) + int(plan["d1_mutations"]) > max_d1_rows_written_24h:
        raise RemotePublicationBudgetError("remote D1 same-day write usage would exceed the safety envelope")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="validate a local-only remote publication plan")
    parser.add_argument("--plan", required=True)
    parser.add_argument("--d1-info", required=True, help="read-only JSON from wrangler d1 info")
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
        if not isinstance(plan, dict) or not isinstance(d1_info, dict):
            raise RemotePublicationBudgetError("invalid remote publication plan")
        assert_plan_within_free_tier(
            plan,
            d1_info=d1_info,
            max_d1_mutations_per_run=args.max_d1_mutations_per_run,
            max_d1_rows_written_24h=args.max_d1_rows_written_24h,
            max_d1_database_bytes=args.max_d1_database_bytes,
            max_r2_class_a_per_month=args.max_r2_class_a_per_month,
            max_r2_class_b_per_month=args.max_r2_class_b_per_month,
            weekday_runs_per_month=args.weekday_runs_per_month,
        )
    except (OSError, json.JSONDecodeError, RemotePublicationBudgetError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["RemotePublicationBudgetError", "assert_plan_within_free_tier", "main"]
