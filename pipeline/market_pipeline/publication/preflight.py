"""Fail closed before a workflow can mutate remote D1 or R2 resources."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from market_pipeline.publication.remote_usage import (
    DEFAULT_ACCOUNT_BUDGET_ALLOCATION,
    DEFAULT_RESERVED_MANUAL_ATTEMPTS,
    MonthlyAttemptPlan,
    RemotePublicationBudgetError,
    RemoteUsage,
    ReservedTotals,
    load_reservation_ledger,
    plan_monthly_attempts,
    reserved_since,
    validate_remote_usage,
)

WEEKDAY_RUNS_PER_MONTH = 22
MAX_D1_MUTATIONS_PER_RUN = 50_000
MAX_D1_ROWS_WRITTEN_24H = 90_000
MAX_D1_DATABASE_BYTES = 400_000_000
_D1_IMPORT_STORAGE_MULTIPLIER = 2
MAX_R2_CLASS_A_PER_MONTH = 800_000
MAX_R2_CLASS_B_PER_MONTH = 5_000_000
#: Cloudflare R2's free tier stores up to 10 GB; reserve 80% of that for this
#: publisher's own retained (reachable) objects -- same 80%-of-stated-limit
#: style already used for MAX_D1_DATABASE_BYTES (400MB of D1's 500MB) --
#: leaving headroom for measurement slop and any other bucket usage. Fixing
#: F14's "Include R2 bytes ... in this task's budget" gap: this is compared
#: against validated, observed `RemoteUsage.r2_retained_bytes` (plus
#: not-yet-observed reservation-ledger bytes), never against raw/untrusted
#: telemetry.
MAX_R2_BYTES = 8_000_000_000


def _default_weekday_runs_per_month(reference_date: date | None = None) -> int:
    """The actual month's scheduled-attempt budget, not a hard-coded constant.

    Finding F11/F14: a fixed ``WEEKDAY_RUNS_PER_MONTH = 22`` silently
    undercounts a 23-weekday month and never accounts for manual
    ``workflow_dispatch`` reruns. This is used as the CLI's own default
    (``main``'s ``--weekday-runs-per-month``) rather than the deliberately
    stable ``WEEKDAY_RUNS_PER_MONTH``/``assert_plan_within_free_tier``
    default kept above for backward-compatible, deterministic unit tests
    that pass a fixed plan literal.
    """

    return plan_monthly_attempts(reference_date or date.today()).monthly_attempts


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


@dataclass(frozen=True)
class RemoteBudgetDecision:
    """An authorized publication attempt, gated by validated remote telemetry."""

    resolved_plan: dict[str, Any]
    mutation_calls: tuple[str, ...]


def assert_plan_within_remote_budget(
    plan: Mapping[str, Any],
    *,
    remote_usage: RemoteUsage,
    remote_snapshot_ids: set[str],
    remote_active_dataset_id: str | None,
    monthly_attempt_plan: MonthlyAttemptPlan,
    reserved: ReservedTotals | None = None,
    max_d1_mutations_per_run: int = MAX_D1_MUTATIONS_PER_RUN,
    max_d1_rows_written_24h: int = MAX_D1_ROWS_WRITTEN_24H,
    max_d1_database_bytes: int = MAX_D1_DATABASE_BYTES,
    max_r2_class_a_per_month: int = MAX_R2_CLASS_A_PER_MONTH,
    max_r2_class_b_per_month: int = MAX_R2_CLASS_B_PER_MONTH,
    max_r2_bytes: int = MAX_R2_BYTES,
    account_budget_allocation: float = DEFAULT_ACCOUNT_BUDGET_ALLOCATION,
) -> RemoteBudgetDecision:
    """Gate a plan using validated Cloudflare telemetry (F11/F14).

    This is the entry point ``main()`` uses once a validated
    :class:`~market_pipeline.publication.remote_usage.RemoteUsage` is
    available. Unlike :func:`assert_plan_within_free_tier`, which trusts
    whatever ``d1_info`` mapping it is handed, this function *requires* an
    already-validated ``RemoteUsage`` -- raw Wrangler zero-default JSON (or
    any other unchecked payload) is rejected outright, never silently
    treated as "zero usage". It keeps ``assert_plan_within_free_tier``'s
    D1 mutation/row/byte and R2 class A/B math completely intact (modulo the
    ``account_budget_allocation`` scaling described below), sourcing its
    numbers from the validated object instead, and adds checks that raw
    ``d1_info`` alone cannot make:

    * the plan's monthly attempt envelope must match this month's
      independently computed schedule (:func:`~market_pipeline.publication
      .remote_usage.plan_monthly_attempts`) plus reserved manual attempts,
      so a stale hard-coded weekday constant can never silently drift from
      reality;
    * this publisher's own reservation-ledger totals recorded since the
      telemetry's observation (bridging analytics ingestion lag) are folded
      into the same-day D1 write and month-to-date R2 operation totals, so
      operations already in flight are never invisible to the gate;
    * validated, observed ``r2_retained_bytes`` (plus not-yet-observed
      ledger bytes) is checked against ``max_r2_bytes`` -- the pre-R09
      preflight never read live R2 storage usage at all; and
    * every cap this function enforces (D1 mutations/rows/bytes, R2 class
      A/B operations, R2 bytes) is scaled down by
      ``account_budget_allocation`` before use. D1 and R2 free-tier limits
      are account-wide, not per-database/per-bucket, so this publisher
      deliberately reserves only a fraction of the account's stated limits
      for itself -- see ``docs/operations/security-checklist.md`` section
      7. This scaling applies only in this validated-telemetry entry point;
      :func:`assert_plan_within_free_tier`'s own defaults are untouched so
      its existing direct unit tests keep exercising literal thresholds.
    """

    if not isinstance(remote_usage, RemoteUsage):
        raise RemotePublicationBudgetError(
            "remote_usage must be a validated RemoteUsage (see "
            "market_pipeline.publication.remote_usage.validate_remote_usage); "
            "raw Wrangler or GraphQL JSON is not an accepted source"
        )
    if not isinstance(account_budget_allocation, (int, float)) or isinstance(account_budget_allocation, bool) or not 0 < account_budget_allocation <= 1:
        raise RemotePublicationBudgetError("account_budget_allocation must be a fraction in (0, 1]")
    reserved_totals = reserved if reserved is not None else ReservedTotals()
    weekday_runs_per_month = monthly_attempt_plan.monthly_attempts

    def _scale(cap: int) -> int:
        return int(cap * account_budget_allocation)

    effective_max_d1_mutations_per_run = _scale(max_d1_mutations_per_run)
    effective_max_d1_rows_written_24h = _scale(max_d1_rows_written_24h)
    effective_max_d1_database_bytes = _scale(max_d1_database_bytes)
    effective_max_r2_class_a_per_month = _scale(max_r2_class_a_per_month)
    effective_max_r2_class_b_per_month = _scale(max_r2_class_b_per_month)
    effective_max_r2_bytes = _scale(max_r2_bytes)

    d1_info = {
        "database_size": remote_usage.d1_database_bytes,
        "rows_written_24h": remote_usage.d1_rows_written_24h + reserved_totals.d1_mutations,
    }
    resolved = assert_plan_within_free_tier(
        plan,
        d1_info=d1_info,
        remote_snapshot_ids=remote_snapshot_ids,
        remote_active_dataset_id=remote_active_dataset_id,
        max_d1_mutations_per_run=effective_max_d1_mutations_per_run,
        max_d1_rows_written_24h=effective_max_d1_rows_written_24h,
        max_d1_database_bytes=effective_max_d1_database_bytes,
        max_r2_class_a_per_month=effective_max_r2_class_a_per_month,
        max_r2_class_b_per_month=effective_max_r2_class_b_per_month,
        weekday_runs_per_month=weekday_runs_per_month,
    )
    # `assert_plan_within_free_tier`'s class_a/class_b figures are a
    # worst-case projection for the whole month, derived only from this
    # plan; they say nothing about operations the account has already
    # spent this month. Guard against that blind spot directly using the
    # validated, observed month-to-date counts (plus this publisher's own
    # not-yet-observed reservation-ledger activity) -- this is exactly the
    # live R2 usage read the pre-R09 preflight never made.
    observed_class_a = remote_usage.r2_class_a_operations_month_to_date + reserved_totals.r2_class_a_operations
    observed_class_b = remote_usage.r2_class_b_operations_month_to_date + reserved_totals.r2_class_b_operations
    if observed_class_a >= effective_max_r2_class_a_per_month:
        raise RemotePublicationBudgetError(
            f"observed R2 Class A operations this month ({observed_class_a}) already meet or exceed "
            f"the safety envelope ({effective_max_r2_class_a_per_month}); refusing to add this run's operations"
        )
    if observed_class_b >= effective_max_r2_class_b_per_month:
        raise RemotePublicationBudgetError(
            f"observed R2 Class B operations this month ({observed_class_b}) already meet or exceed "
            f"the safety envelope ({effective_max_r2_class_b_per_month}); refusing to add this run's operations"
        )
    # F14: budget R2 *storage* bytes too, not just operation counts -- the
    # observed, validated retained-bytes figure plus this publisher's own
    # not-yet-observed reservation-ledger bytes, compared against the
    # account-scaled R2 storage envelope.
    observed_r2_bytes = remote_usage.r2_retained_bytes + reserved_totals.r2_bytes
    if observed_r2_bytes > effective_max_r2_bytes:
        raise RemotePublicationBudgetError(
            f"observed R2 retained storage ({observed_r2_bytes} bytes) would exceed "
            f"the safety envelope ({effective_max_r2_bytes} bytes)"
        )
    return RemoteBudgetDecision(resolved_plan=resolved, mutation_calls=("d1_import", "r2_upload"))


@dataclass(frozen=True)
class PublicationAttemptOutcome:
    """A non-raising view of :func:`assert_plan_within_remote_budget`'s decision.

    ``mutation_calls`` lists the remote mutation operations authorized for
    this attempt (``"d1_import"``, ``"r2_upload"``); it is always empty
    whenever ``rejection_reason`` is set. Rejection happens *before* any
    mutation call is made -- an oversized or otherwise over-budget plan
    never reaches the R2 sync or D1 import steps, so an empty
    ``mutation_calls`` here is a direct, testable statement that no remote
    write was ever authorized, not just that one later failed.
    """

    mutation_calls: tuple[str, ...]
    rejection_reason: str | None = None
    resolved_plan: dict[str, Any] | None = None

    @property
    def authorized(self) -> bool:
        return self.rejection_reason is None


def evaluate_publication_attempt(
    plan: Mapping[str, Any],
    *,
    remote_usage: RemoteUsage,
    remote_snapshot_ids: set[str],
    remote_active_dataset_id: str | None,
    monthly_attempt_plan: MonthlyAttemptPlan,
    reserved: ReservedTotals | None = None,
    max_d1_mutations_per_run: int = MAX_D1_MUTATIONS_PER_RUN,
    max_d1_rows_written_24h: int = MAX_D1_ROWS_WRITTEN_24H,
    max_d1_database_bytes: int = MAX_D1_DATABASE_BYTES,
    max_r2_class_a_per_month: int = MAX_R2_CLASS_A_PER_MONTH,
    max_r2_class_b_per_month: int = MAX_R2_CLASS_B_PER_MONTH,
    max_r2_bytes: int = MAX_R2_BYTES,
    account_budget_allocation: float = DEFAULT_ACCOUNT_BUDGET_ALLOCATION,
) -> PublicationAttemptOutcome:
    """Gate one publication attempt without raising -- for reporting and tests."""

    try:
        decision = assert_plan_within_remote_budget(
            plan,
            remote_usage=remote_usage,
            remote_snapshot_ids=remote_snapshot_ids,
            remote_active_dataset_id=remote_active_dataset_id,
            monthly_attempt_plan=monthly_attempt_plan,
            reserved=reserved,
            max_d1_mutations_per_run=max_d1_mutations_per_run,
            max_d1_rows_written_24h=max_d1_rows_written_24h,
            max_d1_database_bytes=max_d1_database_bytes,
            max_r2_class_a_per_month=max_r2_class_a_per_month,
            max_r2_class_b_per_month=max_r2_class_b_per_month,
            max_r2_bytes=max_r2_bytes,
            account_budget_allocation=account_budget_allocation,
        )
    except RemotePublicationBudgetError as exc:
        return PublicationAttemptOutcome(mutation_calls=(), rejection_reason=str(exc))
    return PublicationAttemptOutcome(mutation_calls=decision.mutation_calls, resolved_plan=decision.resolved_plan)


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
    parser.add_argument("--max-r2-bytes", type=int, default=MAX_R2_BYTES, help="used only with --remote-usage")
    parser.add_argument(
        "--account-budget-allocation",
        type=float,
        default=DEFAULT_ACCOUNT_BUDGET_ALLOCATION,
        help="fraction of each account-wide cap this publisher reserves for itself; used only with --remote-usage",
    )
    parser.add_argument("--weekday-runs-per-month", type=int, default=None, help="defaults to the actual current month's scheduled attempts (see plan_monthly_attempts)")
    parser.add_argument(
        "--remote-usage",
        help=(
            "validated GraphQL Analytics API response JSON (see "
            "market_pipeline.publication.remote_usage). When given, this "
            "supersedes --d1-info as the source of D1/R2 capacity numbers "
            "(F11): raw Wrangler zero-default output is not an accepted "
            "source for the gate itself."
        ),
    )
    parser.add_argument("--expected-account-id", help="required with --remote-usage")
    parser.add_argument("--expected-database-id", help="required with --remote-usage")
    parser.add_argument("--expected-bucket-name", help="required with --remote-usage")
    parser.add_argument("--reservation-ledger", help="durable reservation-ledger JSON path (optional, used with --remote-usage)")
    parser.add_argument("--reserved-manual-attempts", type=int, default=DEFAULT_RESERVED_MANUAL_ATTEMPTS)
    args = parser.parse_args(argv)
    try:
        plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        d1_info = json.loads(Path(args.d1_info).read_text(encoding="utf-8"))
        remote_state = json.loads(Path(args.remote_publication_state).read_text(encoding="utf-8"))
        if not isinstance(plan, dict) or not isinstance(d1_info, dict):
            raise RemotePublicationBudgetError("invalid remote publication plan")
        remote_ids, active = parse_remote_publication_state(remote_state)
        if args.remote_usage:
            if not (args.expected_account_id and args.expected_database_id and args.expected_bucket_name):
                raise RemotePublicationBudgetError(
                    "--expected-account-id, --expected-database-id, and --expected-bucket-name "
                    "are required with --remote-usage"
                )
            now = datetime.now(timezone.utc)
            raw_usage = json.loads(Path(args.remote_usage).read_text(encoding="utf-8"))
            remote_usage = validate_remote_usage(
                raw_usage,
                {
                    "account_id": args.expected_account_id,
                    "database_id": args.expected_database_id,
                    "bucket_name": args.expected_bucket_name,
                },
                now,
            )
            ledger = load_reservation_ledger(Path(args.reservation_ledger)) if args.reservation_ledger else ()
            reserved = reserved_since(ledger, remote_usage.observed_at)
            monthly_attempt_plan = plan_monthly_attempts(now.date(), reserved_manual_attempts=args.reserved_manual_attempts)
            decision = assert_plan_within_remote_budget(
                plan,
                remote_usage=remote_usage,
                remote_snapshot_ids=remote_ids,
                remote_active_dataset_id=active,
                monthly_attempt_plan=monthly_attempt_plan,
                reserved=reserved,
                max_d1_mutations_per_run=args.max_d1_mutations_per_run,
                max_d1_rows_written_24h=args.max_d1_rows_written_24h,
                max_d1_database_bytes=args.max_d1_database_bytes,
                max_r2_class_a_per_month=args.max_r2_class_a_per_month,
                max_r2_class_b_per_month=args.max_r2_class_b_per_month,
                max_r2_bytes=args.max_r2_bytes,
                account_budget_allocation=args.account_budget_allocation,
            )
            resolved = decision.resolved_plan
        else:
            weekday_runs_per_month = (
                args.weekday_runs_per_month if args.weekday_runs_per_month is not None else _default_weekday_runs_per_month()
            )
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
                weekday_runs_per_month=weekday_runs_per_month,
            )
        Path(args.resolved_plan).write_text(json.dumps(resolved, sort_keys=True) + "\n", encoding="utf-8")
    except (OSError, json.JSONDecodeError, RemotePublicationBudgetError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "PublicationAttemptOutcome",
    "RemoteBudgetDecision",
    "RemotePublicationBudgetError",
    "assert_plan_within_free_tier",
    "assert_plan_within_remote_budget",
    "evaluate_publication_attempt",
    "main",
    "parse_remote_publication_state",
]
