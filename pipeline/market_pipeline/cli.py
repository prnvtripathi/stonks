"""Command-line entry points for operator-supplied backfill and daily runs."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import sqlite3
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Sequence

from market_pipeline.domain.models import FetchedArtifact, SourceArtifact
from market_pipeline.jobs.backfill import BackfillJob, CoverageError
from market_pipeline.jobs.daily import run_daily
from market_pipeline.monitoring.budgets import budget_report
from market_pipeline.storage.d1_publisher import D1Publisher
from market_pipeline.validation.reconcile import (
    SourceObservation,
    evaluate_pre_promotion,
    reconcile,
)


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="market-pipeline")
    parser.add_argument("--db", default="market.db", help="local SQLite database path")
    parser.add_argument("--d1-budget-limit-bytes", type=int, help="optional D1 storage budget")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("backfill", "daily"):
        sub = subparsers.add_parser(command)
        sub.add_argument("--date", type=_date, help="effective date (daily only)")
        sub.add_argument("--start", type=_date, help="inclusive backfill start")
        sub.add_argument("--end", type=_date, help="inclusive backfill end")
        sub.add_argument("--source", action="append", dest="sources", default=None, help="source ID; repeatable")
        sub.add_argument("--manifest", help="JSON manifest of committed official artifacts")
        sub.add_argument(
            "--previous-count",
            type=int,
            default=None,
            help="known-good artifact count from the last successful run, for coverage reconciliation",
        )
        sub.add_argument(
            "--min-coverage-ratio",
            type=float,
            default=0.90,
            help="minimum candidate/previous coverage ratio before a run is flagged non-publishable",
        )
    return parser


def _manifest_fetcher(path: str) -> Any:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"artifact manifest is unreadable: {exc}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("artifacts"), list):
        raise ValueError("artifact manifest must contain an artifacts array")
    by_date: dict[tuple[str, str], list[FetchedArtifact]] = {}
    for number, entry in enumerate(document["artifacts"], start=1):
        if not isinstance(entry, dict) or not isinstance(entry.get("artifact"), dict):
            raise ValueError(f"manifest artifact {number} must contain an artifact object")
        encoded = entry.get("body_base64")
        if not isinstance(encoded, str) or not encoded:
            raise ValueError(f"manifest artifact {number} requires body_base64")
        try:
            body = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"manifest artifact {number} has invalid body_base64") from exc
        try:
            artifact = SourceArtifact.model_validate(entry["artifact"])
            fetched = FetchedArtifact(artifact=artifact, body=body)
        except ValueError as exc:
            raise ValueError(f"manifest artifact {number} has invalid artifact metadata: {exc}") from exc
        if artifact.checksum.lower() != hashlib.sha256(body).hexdigest():
            raise ValueError(f"manifest artifact {number} checksum does not match body")
        key = (artifact.source_id, artifact.effective_date.isoformat())
        by_date.setdefault(key, []).append(fetched)

    def fetch(source: str, effective: date) -> list[Any]:
        return list(by_date.get((source, effective.isoformat()), ()))

    return fetch


def _ensure_run_history_table(connection: sqlite3.Connection) -> None:
    """Create the local run-history table used to derive a real coverage baseline.

    Every `daily`/`backfill` invocation that turns out to be safe-to-promote
    records its completed-artifact count here, scoped by (command, sorted
    sources). The next run against the same database/scope that does not
    receive an explicit `--previous-count` uses the most recent safe row as
    its baseline instead of comparing a candidate against itself. This is
    intentionally a plain local table (not a D1-published table) since it is
    CLI bookkeeping, not published dataset content.
    """

    connection.execute(
        "CREATE TABLE IF NOT EXISTS pipeline_run_history ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, command TEXT NOT NULL, scope TEXT NOT NULL, "
        "completed INTEGER NOT NULL, safe_to_promote INTEGER NOT NULL, created_at TEXT NOT NULL)"
    )
    connection.commit()


def _run_scope(command: str, sources: Sequence[str]) -> str:
    return json.dumps({"command": command, "sources": sorted(sources)}, sort_keys=True)


def _last_known_good_completed(
    connection: sqlite3.Connection, command: str, sources: Sequence[str]
) -> int | None:
    row = connection.execute(
        "SELECT completed FROM pipeline_run_history WHERE command = ? AND scope = ? "
        "AND safe_to_promote = 1 ORDER BY id DESC LIMIT 1",
        (command, _run_scope(command, sources)),
    ).fetchone()
    return None if row is None else int(row[0])


def _record_run_history(
    connection: sqlite3.Connection,
    command: str,
    sources: Sequence[str],
    completed: int,
    safe_to_promote: bool,
) -> None:
    connection.execute(
        "INSERT INTO pipeline_run_history(command, scope, completed, safe_to_promote, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (command, _run_scope(command, sources), completed, 1 if safe_to_promote else 0, datetime.now(UTC).isoformat()),
    )
    connection.commit()


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    sources = args.sources or ["amfi-nav"]
    if not args.manifest:
        print(json.dumps({"error": "artifact input/provider is required"}, sort_keys=True), file=sys.stderr)
        return 2
    try:
        fetcher = _manifest_fetcher(args.manifest)
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2
    connection = sqlite3.connect(args.db)
    publisher = D1Publisher(connection, budget_limit_bytes=args.d1_budget_limit_bytes)
    publisher.initialize_schema()
    _ensure_run_history_table(connection)
    if args.command == "daily":
        if args.date is None:
            _parser().error("daily requires --date")
        start = end = args.date
    else:
        start, end = args.start, args.end

    try:
        # Network adapters are deliberately not wired into this command. The
        # operator/job runner supplies committed official artifacts via a manifest.
        #
        # strict_coverage is deliberately off here: a source missing some or
        # all of its requested dates must not raise before reconciliation
        # ever runs (which would report exit 2 and skip the per-source
        # freshness / coverage / budget checks below entirely). Instead the
        # job always returns a BackfillResult with missing_dates populated,
        # and reconcile()'s per-source complete/delayed/failed/not_expected
        # evaluation below is the actual blocking mechanism (via
        # evaluate_pre_promotion), so every configured source gets an
        # independent freshness verdict instead of one aborting the whole run.
        if args.command == "daily":
            result = run_daily(connection, fetcher, sources, effective_date=start, strict_coverage=False, budget_sources=(publisher,))
        else:
            result = BackfillJob(connection, sources, fetcher, strict_coverage=False, budget_sources=(publisher,)).run(start, end)
    except (CoverageError, ValueError, RuntimeError) as exc:
        connection.close()
        print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2

    # Safety gate: before any future promotion step runs, reconcile the
    # candidate run's coverage against the last known-good baseline and check
    # storage budget.
    total_days = (result.end - result.start).days + 1
    source_observations = {
        source: SourceObservation(
            expected=True,
            failed=len(result.missing_dates.get(source, ())) == total_days,
            delay_days=len(result.missing_dates.get(source, ())),
            max_delay_days=0,
        )
        for source in sources
    }
    # An explicit --previous-count always wins (this is how the daily-data
    # workflow's manual workflow_dispatch path and historical re-runs supply
    # a known baseline). Otherwise, use the most recent safe-to-promote run
    # recorded in this database for the same command/source scope -- this is
    # what makes the coverage-drop check meaningful for the scheduled
    # (cron-triggered) path, which has no `inputs` context to source a
    # baseline from. Only when this database has never seen a successful run
    # for this scope (a genuine first-ever run) do we fall back to comparing
    # the candidate against itself (ratio 1.0), which still leaves source
    # failures and the budget check as protection.
    if args.previous_count is not None:
        previous_count = args.previous_count
    else:
        baseline = _last_known_good_completed(connection, args.command, sources)
        previous_count = baseline if baseline is not None else result.completed
    reconciliation = reconcile(
        previous=previous_count,
        candidate=result.completed,
        min_coverage_ratio=args.min_coverage_ratio,
        sources=source_observations,
    )
    storage = publisher.budget_report()
    storage_report = budget_report(
        used=storage.used_bytes,
        limit=storage.limit_bytes,
        warning_threshold=storage.warning_threshold,
        source="d1",
    )
    safe_to_promote, blocking_reasons = evaluate_pre_promotion(reconciliation, [storage_report])
    # Only a safe-to-promote run may become a future baseline: a blocked
    # candidate (coverage drop, failed source, exceeded budget) must never
    # poison the next run's comparison point.
    _record_run_history(connection, args.command, sources, result.completed, safe_to_promote)

    print(json.dumps({
        "start": result.start.isoformat(),
        "end": result.end.isoformat(),
        "completed": result.completed,
        "skipped": result.skipped,
        "missing_dates": {
            source: [item.isoformat() for item in dates]
            for source, dates in result.missing_dates.items()
        },
        "errors": list(result.errors),
        "warnings": list(result.warnings),
        "reconciliation": reconciliation.as_dict(),
        "budget": storage_report.as_dict(),
        "safe_to_promote": safe_to_promote,
        "blocking_reasons": list(blocking_reasons),
    }, sort_keys=True))
    connection.close()
    if not safe_to_promote:
        # This CLI does not itself call D1Publisher.promote; this exit code is
        # the enforcement point a future publish step, or the daily-data
        # workflow, must treat as "do not promote this candidate".
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
