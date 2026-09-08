"""Fail closed before a workflow can mutate remote D1 or R2 resources."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

WEEKDAY_RUNS_PER_MONTH = 22
MAX_D1_MUTATIONS_PER_RUN = 50_000
MAX_R2_CLASS_A_PER_MONTH = 500_000
MAX_R2_CLASS_B_PER_MONTH = 5_000_000


class RemotePublicationBudgetError(RuntimeError):
    """Raised when a plan would exceed the deliberately conservative envelope."""


def assert_plan_within_free_tier(
    plan: Mapping[str, Any],
    *,
    max_d1_mutations_per_run: int = MAX_D1_MUTATIONS_PER_RUN,
    max_r2_class_a_per_month: int = MAX_R2_CLASS_A_PER_MONTH,
    max_r2_class_b_per_month: int = MAX_R2_CLASS_B_PER_MONTH,
    weekday_runs_per_month: int = WEEKDAY_RUNS_PER_MONTH,
) -> None:
    required = ("d1_mutations", "r2_class_a", "r2_class_b", "weekday_runs_per_month")
    if any(not isinstance(plan.get(key), int) or isinstance(plan.get(key), bool) or int(plan[key]) < 0 for key in required):
        raise RemotePublicationBudgetError("invalid remote publication plan")
    if int(plan["weekday_runs_per_month"]) != weekday_runs_per_month:
        raise RemotePublicationBudgetError("remote publication plan has an unexpected weekday envelope")
    if (
        int(plan["d1_mutations"]) > max_d1_mutations_per_run
        or int(plan["r2_class_a"]) * weekday_runs_per_month > max_r2_class_a_per_month
        or int(plan["r2_class_b"]) * weekday_runs_per_month > max_r2_class_b_per_month
    ):
        raise RemotePublicationBudgetError("remote publication plan exceeds the free-tier safety envelope")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="validate a local-only remote publication plan")
    parser.add_argument("--plan", required=True)
    parser.add_argument("--max-d1-mutations-per-run", type=int, default=MAX_D1_MUTATIONS_PER_RUN)
    parser.add_argument("--max-r2-class-a-per-month", type=int, default=MAX_R2_CLASS_A_PER_MONTH)
    parser.add_argument("--max-r2-class-b-per-month", type=int, default=MAX_R2_CLASS_B_PER_MONTH)
    parser.add_argument("--weekday-runs-per-month", type=int, default=WEEKDAY_RUNS_PER_MONTH)
    args = parser.parse_args(argv)
    try:
        plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        if not isinstance(plan, dict):
            raise RemotePublicationBudgetError("invalid remote publication plan")
        assert_plan_within_free_tier(
            plan,
            max_d1_mutations_per_run=args.max_d1_mutations_per_run,
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
