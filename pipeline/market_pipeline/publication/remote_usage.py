"""Validate Cloudflare's authenticated capacity telemetry before it can gate a publish.

Finding F11 (see ``docs/reviews/2026-09-09-implementation-audit.md``): the
pre-R09 preflight trusted whatever JSON ``wrangler d1 info`` handed it, as
long as two integer fields were present and non-negative. Per the audit's
citation of Wrangler's own source, ``wrangler d1 info`` silently defaults
missing metrics to zero rather than raising -- so an expired token, a wrong
account, or a GraphQL Analytics API permission failure all look identical to
"genuinely zero usage" and would have let a budget check pass when it had
observed nothing at all.

This module is the validated boundary between that raw, untrusted telemetry
and the free-tier budget math in :mod:`market_pipeline.publication.preflight`.
It does not call Cloudflare's GraphQL Analytics API itself (the CLI accepts
an already-fetched JSON response file, the same way ``preflight``'s ``main``
already accepts pre-fetched ``wrangler`` JSON) -- it validates the *shape and
freshness* of that response before anything downstream is allowed to treat
its numbers as real usage.

Expected payload shape (modeled on Cloudflare's documented GraphQL Analytics
API, simplified to exactly the dimensions/metrics this publisher needs)::

    {
      "errors": null,                      # or a non-empty list of {"message": ...}
      "data": {
        "viewer": {
          "accounts": [
            {
              "accountTag": "<cloudflare account id>",
              "observedAt": "2026-09-12T08:00:00Z",   # when this snapshot was taken
              "d1": {
                "databaseId": "<D1 database id>",
                "databaseSizeBytes": 12345678,
                "rowsWritten24h": 4200
              },
              "r2": {
                "bucketName": "<R2 bucket name>",
                "retainedBytes": 987654321,
                "windowStart": "2026-09-01T00:00:00Z",  # start of the MTD accounting window
                "classAOperationsMonthToDate": 12000,
                "classBOperationsMonthToDate": 45000
              }
            }
          ]
        }
      }
    }

A GraphQL Analytics API token needs the ``Account Analytics Read``
permission to return this data at all; see
``docs/operations/security-checklist.md`` section 7. Adding that permission
alone is not a fix for silent-zero responses -- a request denied for any
other reason (wrong account, expired token, wrong resource scope) still
returns a well-formed-looking response unless this module's checks reject
it, which is exactly why ``errors``, missing dimensions, and identity
mismatches are all checked explicitly below rather than assumed away by
"the permission exists now".
"""

from __future__ import annotations

import json
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

#: A live analytics observation older than this is not trusted as "current
#: usage" for a same-day gate. Cloudflare's GraphQL Analytics API has its own
#: ingestion lag; three hours is deliberately generous relative to that lag
#: while still catching a genuinely stale/cached response (a bug, or a
#: silently-failed refetch) before it gates a real publish.
DEFAULT_MAX_OBSERVATION_AGE_SECONDS = 3 * 60 * 60

#: A future-dated observation (beyond ordinary clock skew) is never valid --
#: it means the caller's ``now`` or the API response's clock is wrong, and
#: trusting it would silently *reduce* the staleness guard's effectiveness.
_FUTURE_OBSERVATION_TOLERANCE_SECONDS = 60

#: Manual `workflow_dispatch` reruns (backfills, one-off republishes) are not
#: on the weekday cron schedule but still spend the same account-wide D1/R2
#: budget. Reserve a fixed number of them per month rather than assuming the
#: schedule is the only source of runs.
DEFAULT_RESERVED_MANUAL_ATTEMPTS = 4

#: The scheduled job (see ``.github/workflows/daily-data.yml``) runs on
#: weekdays. Kept as a module-level default so both the plan writer
#: (``d1_export``) and the plan checker (``preflight``) derive the same
#: schedule from the same place.
DEFAULT_SCHEDULE_WEEKDAYS = frozenset({0, 1, 2, 3, 4})  # Monday=0 .. Sunday=6

#: D1 and R2 free-tier limits are account-wide, not per-database/per-bucket
#: (Cloudflare bills/limits the whole account). This publisher explicitly
#: reserves only a fraction of the account's stated limit for itself,
#: leaving headroom for the preview environment and any other usage sharing
#: the same account -- see security-checklist.md section 7.
DEFAULT_ACCOUNT_BUDGET_ALLOCATION = 0.9

#: How long a reservation-ledger entry stays relevant. An entry records this
#: publisher's own attempted operations at the moment they happened; once a
#: live analytics observation is *newer* than the entry, the entry's activity
#: is already reflected in that observation and would double-count if kept.
LEDGER_ENTRY_RELEVANCE_SECONDS = 24 * 60 * 60


class RemotePublicationBudgetError(RuntimeError):
    """Raised when telemetry cannot be trusted or a plan would exceed its envelope."""


@dataclass(frozen=True)
class RemoteUsage:
    """Validated Cloudflare capacity telemetry for one publication attempt.

    Every field here has passed :func:`validate_remote_usage` -- it is never
    constructed directly from raw, unchecked API output.
    """

    account_id: str
    database_id: str
    bucket_name: str
    observed_at: datetime
    window_start: datetime
    d1_database_bytes: int
    d1_rows_written_24h: int
    r2_retained_bytes: int
    r2_class_a_operations_month_to_date: int
    r2_class_b_operations_month_to_date: int


def _require_mapping(value: Any, message: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RemotePublicationBudgetError(message)
    return value


def _nonneg_int(container: Mapping[str, Any], key: str, *, context: str) -> int:
    value = container.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RemotePublicationBudgetError(f"remote usage response has a malformed {context} metric: {key}")
    return value


def _nonempty_str(container: Mapping[str, Any], key: str, *, context: str) -> str:
    value = container.get(key)
    if not isinstance(value, str) or not value:
        raise RemotePublicationBudgetError(f"remote usage response is missing the {context} dimension: {key}")
    return value


def _parse_timestamp(raw: Any, *, context: str) -> datetime:
    if not isinstance(raw, str) or not raw:
        raise RemotePublicationBudgetError(f"remote usage response is missing the {context} timestamp")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RemotePublicationBudgetError(f"remote usage response has an unparseable {context} timestamp") from exc
    if parsed.tzinfo is None:
        raise RemotePublicationBudgetError(f"remote usage response {context} timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def validate_remote_usage(
    payload: Any,
    expected_resources: Mapping[str, str],
    now: datetime,
    *,
    max_observation_age_seconds: int = DEFAULT_MAX_OBSERVATION_AGE_SECONDS,
) -> RemoteUsage:
    """Strictly decode and validate a GraphQL Analytics API response.

    Rejects (all as :class:`RemotePublicationBudgetError`):

    * a GraphQL ``errors`` array (including one whose message indicates a
      missing/insufficient permission -- the exception message calls this
      out explicitly so an operator does not mistake it for "zero usage");
    * a response with no single, well-formed account entry, or missing
      D1/R2 dimensions within it (an absent account or database);
    * malformed metrics (wrong type, negative, or missing);
    * an observation whose timestamp is stale (older than
      ``max_observation_age_seconds``) or from the future beyond a small
      clock-skew allowance;
    * an R2 month-to-date accounting window that does not start at the
      current calendar month's boundary (a provider returning last month's
      window after rollover would otherwise silently undercount);
    * a response that identifies a different account, database, or bucket
      than ``expected_resources`` names.

    Calling this twice with the same, valid payload (e.g. because the HTTP
    client underneath retried the request) is safe and deterministic: it has
    no side effects and returns an equal :class:`RemoteUsage` both times.
    """

    if now.tzinfo is None:
        raise RemotePublicationBudgetError("now must be timezone-aware")
    now_utc = now.astimezone(timezone.utc)

    root = _require_mapping(payload, "invalid remote usage response")
    errors = root.get("errors")
    if errors:
        if not isinstance(errors, list):
            raise RemotePublicationBudgetError("invalid remote usage response")
        first = errors[0] if errors else {}
        message = str(first.get("message", "")) if isinstance(first, Mapping) else ""
        lowered = message.lower()
        hint = ""
        if any(term in lowered for term in ("permission", "unauthorized", "auth", "forbidden", "denied", "scope")):
            hint = " -- verify the token has the Account Analytics Read permission on this exact account/database/bucket (see docs/operations/security-checklist.md section 7)"
        raise RemotePublicationBudgetError(f"remote usage API returned an error: {message or 'unknown error'}{hint}")

    data = _require_mapping(root.get("data"), "remote usage response is missing data")
    viewer = _require_mapping(data.get("viewer"), "remote usage response is missing the viewer")
    accounts = viewer.get("accounts")
    if not isinstance(accounts, list) or len(accounts) != 1:
        raise RemotePublicationBudgetError("remote usage response has no single account dimension (absent or ambiguous account)")
    account = _require_mapping(accounts[0], "remote usage response account entry is malformed")

    account_id = _nonempty_str(account, "accountTag", context="account")
    d1 = _require_mapping(account.get("d1"), "remote usage response is missing the D1 dimension (absent database)")
    r2 = _require_mapping(account.get("r2"), "remote usage response is missing the R2 dimension (absent bucket)")

    database_id = _nonempty_str(d1, "databaseId", context="D1 database")
    bucket_name = _nonempty_str(r2, "bucketName", context="R2 bucket")

    d1_database_bytes = _nonneg_int(d1, "databaseSizeBytes", context="D1")
    d1_rows_written_24h = _nonneg_int(d1, "rowsWritten24h", context="D1")
    r2_retained_bytes = _nonneg_int(r2, "retainedBytes", context="R2")
    r2_class_a = _nonneg_int(r2, "classAOperationsMonthToDate", context="R2")
    r2_class_b = _nonneg_int(r2, "classBOperationsMonthToDate", context="R2")

    observed_at = _parse_timestamp(account.get("observedAt"), context="observation")
    age_seconds = (now_utc - observed_at).total_seconds()
    if age_seconds < -_FUTURE_OBSERVATION_TOLERANCE_SECONDS:
        raise RemotePublicationBudgetError("remote usage response observation timestamp is in the future")
    if age_seconds > max_observation_age_seconds:
        raise RemotePublicationBudgetError(
            f"remote usage response is stale ({age_seconds:.0f}s old; max {max_observation_age_seconds}s) -- "
            "refusing to treat it as current usage"
        )

    window_start = _parse_timestamp(r2.get("windowStart"), context="month-to-date window start")
    expected_window_start = datetime(now_utc.year, now_utc.month, 1, tzinfo=timezone.utc)
    if window_start != expected_window_start:
        raise RemotePublicationBudgetError(
            "remote usage response's R2 month-to-date window does not start at the current calendar "
            f"month boundary (got {window_start.isoformat()}, expected {expected_window_start.isoformat()})"
        )

    expected_account = expected_resources.get("account_id")
    expected_database = expected_resources.get("database_id")
    expected_bucket = expected_resources.get("bucket_name")
    if account_id != expected_account or database_id != expected_database or bucket_name != expected_bucket:
        raise RemotePublicationBudgetError(
            "remote usage response identifies a different account, database, or bucket than expected"
        )

    return RemoteUsage(
        account_id=account_id,
        database_id=database_id,
        bucket_name=bucket_name,
        observed_at=observed_at,
        window_start=window_start,
        d1_database_bytes=d1_database_bytes,
        d1_rows_written_24h=d1_rows_written_24h,
        r2_retained_bytes=r2_retained_bytes,
        r2_class_a_operations_month_to_date=r2_class_a,
        r2_class_b_operations_month_to_date=r2_class_b,
    )


@dataclass(frozen=True)
class MonthlyAttemptPlan:
    """How many publication attempts this calendar month is expected to make.

    ``scheduled_dates_in_month`` is computed directly from the calendar --
    never a hard-coded constant -- so a 23-weekday month is counted as 23,
    not silently rounded to some fixed assumption.
    """

    year: int
    month: int
    scheduled_dates_in_month: int
    reserved_manual_attempts: int

    @property
    def monthly_attempts(self) -> int:
        return self.scheduled_dates_in_month + self.reserved_manual_attempts


def scheduled_dates_in_month(year: int, month: int, *, schedule_weekdays: frozenset[int] = DEFAULT_SCHEDULE_WEEKDAYS) -> int:
    """Count this calendar month's dates that fall on a scheduled weekday.

    Computed from ``calendar.monthrange`` for the actual month given, not a
    constant -- correctly returns 23 for months that have 23 such weekdays,
    20 for a short February, etc.
    """

    _, days_in_month = monthrange(year, month)
    return sum(1 for day in range(1, days_in_month + 1) if date(year, month, day).weekday() in schedule_weekdays)


def plan_monthly_attempts(
    reference_date: date,
    *,
    reserved_manual_attempts: int = DEFAULT_RESERVED_MANUAL_ATTEMPTS,
    schedule_weekdays: frozenset[int] = DEFAULT_SCHEDULE_WEEKDAYS,
) -> MonthlyAttemptPlan:
    """The expected total publication-attempt budget for ``reference_date``'s month.

    This is the actual month's scheduled-run count (F11/F14 gap: previously
    a hard-coded ``WEEKDAY_RUNS_PER_MONTH = 22``), plus a fixed reservation
    for manual ``workflow_dispatch`` reruns, which are not on the cron
    schedule but still spend the same account-wide budget.
    """

    if reserved_manual_attempts < 0:
        raise RemotePublicationBudgetError("reserved_manual_attempts must not be negative")
    count = scheduled_dates_in_month(reference_date.year, reference_date.month, schedule_weekdays=schedule_weekdays)
    return MonthlyAttemptPlan(
        year=reference_date.year,
        month=reference_date.month,
        scheduled_dates_in_month=count,
        reserved_manual_attempts=reserved_manual_attempts,
    )


@dataclass(frozen=True)
class ReservationLedgerEntry:
    """One durable record of this publisher's own attempted remote operations.

    Live analytics telemetry lags real usage (ingestion delay, and an SDK
    that transparently retries a request the far side actually received).
    The ledger is this publisher's own conservative record of what it has
    *attempted*, kept only long enough to bridge that lag -- it never claims
    to be a measurement of the account's true remote usage, only a
    reservation against it. See :func:`reserved_since`.
    """

    recorded_at: datetime
    d1_mutations: int
    r2_class_a_operations: int
    r2_class_b_operations: int
    r2_bytes: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "recorded_at": self.recorded_at.astimezone(timezone.utc).isoformat(),
            "d1_mutations": self.d1_mutations,
            "r2_class_a_operations": self.r2_class_a_operations,
            "r2_class_b_operations": self.r2_class_b_operations,
            "r2_bytes": self.r2_bytes,
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> "ReservationLedgerEntry":
        try:
            recorded_at = _parse_timestamp(data.get("recorded_at"), context="ledger entry")
            return ReservationLedgerEntry(
                recorded_at=recorded_at,
                d1_mutations=_nonneg_int(data, "d1_mutations", context="ledger"),
                r2_class_a_operations=_nonneg_int(data, "r2_class_a_operations", context="ledger"),
                r2_class_b_operations=_nonneg_int(data, "r2_class_b_operations", context="ledger"),
                r2_bytes=_nonneg_int(data, "r2_bytes", context="ledger"),
            )
        except RemotePublicationBudgetError as exc:
            raise RemotePublicationBudgetError(f"reservation ledger entry is malformed: {exc}") from exc


@dataclass(frozen=True)
class ReservedTotals:
    d1_mutations: int = 0
    r2_class_a_operations: int = 0
    r2_class_b_operations: int = 0
    r2_bytes: int = 0


def load_reservation_ledger(path: Path) -> tuple[ReservationLedgerEntry, ...]:
    """Read a durable reservation ledger. A missing file is an empty ledger."""

    if not path.exists():
        return ()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RemotePublicationBudgetError("reservation ledger file is not valid JSON") from exc
    if not isinstance(raw, list):
        raise RemotePublicationBudgetError("reservation ledger file must contain a JSON list")
    return tuple(ReservationLedgerEntry.from_dict(item) for item in raw)


def append_reservation_ledger_entry(
    path: Path,
    entry: ReservationLedgerEntry,
    *,
    now: datetime,
    relevance_seconds: int = LEDGER_ENTRY_RELEVANCE_SECONDS,
) -> tuple[ReservationLedgerEntry, ...]:
    """Append ``entry`` and prune entries older than ``relevance_seconds``.

    Pruning keeps the ledger from growing without bound and drops entries
    old enough that any live analytics observation has almost certainly
    already caught up to them.
    """

    existing = load_reservation_ledger(path)
    now_utc = now.astimezone(timezone.utc)
    kept = tuple(
        item for item in existing
        if (now_utc - item.recorded_at).total_seconds() <= relevance_seconds
    )
    updated = kept + (entry,)
    path.write_text(json.dumps([item.as_dict() for item in updated], indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return updated


def reserved_since(entries: Sequence[ReservationLedgerEntry], since: datetime) -> ReservedTotals:
    """Sum ledger activity recorded strictly after ``since``.

    ``since`` is normally a validated :class:`RemoteUsage`'s ``observed_at``:
    activity recorded at or before that timestamp is already reflected in
    the live observation and must not be double-counted; only activity this
    publisher knows about that the live snapshot could not yet have seen is
    added as an extra safety margin.
    """

    since_utc = since.astimezone(timezone.utc)
    relevant = [item for item in entries if item.recorded_at > since_utc]
    return ReservedTotals(
        d1_mutations=sum(item.d1_mutations for item in relevant),
        r2_class_a_operations=sum(item.r2_class_a_operations for item in relevant),
        r2_class_b_operations=sum(item.r2_class_b_operations for item in relevant),
        r2_bytes=sum(item.r2_bytes for item in relevant),
    )


__all__ = [
    "DEFAULT_ACCOUNT_BUDGET_ALLOCATION",
    "DEFAULT_MAX_OBSERVATION_AGE_SECONDS",
    "DEFAULT_RESERVED_MANUAL_ATTEMPTS",
    "DEFAULT_SCHEDULE_WEEKDAYS",
    "LEDGER_ENTRY_RELEVANCE_SECONDS",
    "MonthlyAttemptPlan",
    "RemotePublicationBudgetError",
    "RemoteUsage",
    "ReservationLedgerEntry",
    "ReservedTotals",
    "append_reservation_ledger_entry",
    "load_reservation_ledger",
    "plan_monthly_attempts",
    "reserved_since",
    "scheduled_dates_in_month",
    "validate_remote_usage",
]
