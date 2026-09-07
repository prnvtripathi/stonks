"""Command-line entry points for operator-supplied backfill and daily runs."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path
from typing import Any, Sequence

from market_pipeline.domain.models import FetchedArtifact, SourceArtifact
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
            result = BackfillJob(connection, sources, fetcher, strict_coverage=True, budget_sources=(publisher,)).run(start, end)
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
