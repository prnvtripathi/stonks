"""Command-line entry points for operator-supplied backfill and daily runs."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path
from typing import Any, Sequence

from market_pipeline.jobs.backfill import BackfillJob, CoverageError
from market_pipeline.jobs.daily import run_daily
from market_pipeline.storage.d1_publisher import D1Publisher


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
    return parser


def _manifest_fetcher(path: str) -> Any:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"artifact manifest is unreadable: {exc}") from exc
    if not isinstance(document, dict):
        raise ValueError("artifact manifest must be a JSON object")

    def fetch(source: str, effective: date) -> list[Any]:
        source_entries = document.get(source, {})
        if not isinstance(source_entries, dict):
            return []
        entries = source_entries.get(effective.isoformat(), [])
        return entries if isinstance(entries, list) else []

    return fetch


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
    if args.command == "daily":
        if args.date is None:
            _parser().error("daily requires --date")
        start = end = args.date
    else:
        start, end = args.start, args.end

    try:
        # Network adapters are deliberately not wired into this command. The
        # operator/job runner supplies committed official artifacts via a manifest.
        if args.command == "daily":
            result = run_daily(connection, fetcher, sources, effective_date=start, strict_coverage=True, budget_sources=(publisher,))
        else:
            result = BackfillJob(connection, sources, fetcher, budget_sources=(publisher,)).run(start, end)
    except (CoverageError, ValueError, RuntimeError) as exc:
        connection.close()
        print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2
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
    }, sort_keys=True))
    connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
