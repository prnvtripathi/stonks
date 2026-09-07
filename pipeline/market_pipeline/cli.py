"""Command-line entry points for operator-supplied backfill and daily runs."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import date
from typing import Sequence

from market_pipeline.jobs.backfill import BackfillJob
from market_pipeline.storage.d1_publisher import D1Publisher


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="market-pipeline")
    parser.add_argument("--db", default="market.db", help="local SQLite database path")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("backfill", "daily"):
        sub = subparsers.add_parser(command)
        sub.add_argument("--date", type=_date, help="effective date (daily only)")
        sub.add_argument("--start", type=_date, help="inclusive backfill start")
        sub.add_argument("--end", type=_date, help="inclusive backfill end")
        sub.add_argument("--source", action="append", dest="sources", default=["amfi-nav"], help="source ID; repeatable")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    connection = sqlite3.connect(args.db)
    publisher = D1Publisher(connection)
    publisher.initialize_schema()
    if args.command == "daily":
        if args.date is None:
            _parser().error("daily requires --date")
        start = end = args.date
    else:
        start, end = args.start, args.end

    # Network adapters are deliberately not wired into this command. The
    # operator/job runner supplies committed official artifacts via a fetcher.
    result = BackfillJob(connection, args.sources, lambda _source, _date: []).run(start, end)
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
    }, sort_keys=True))
    connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
