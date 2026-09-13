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
from market_pipeline.jobs.backfill import BackfillJob, BackfillResult, CoverageError
from market_pipeline.jobs.composed_candidate import (
    SourceScheduleEntry,
    build_composed_candidate,
    run_scheduled_refresh,
)
from market_pipeline.jobs.daily import run_daily
from market_pipeline.jobs.publish import (
    PublicationInputError,
    publish_checkpointed_dataset,
)
from market_pipeline.monitoring.budgets import budget_report
from market_pipeline.sources.registry import SourcePolicyError
from market_pipeline.storage.d1_publisher import D1Publisher
from market_pipeline.storage.history_store import HistoryStore, LocalHistoryStore
from market_pipeline.storage.raw_store import LocalRawStore
from market_pipeline.validation.reconcile import (
    CandidateCoverage,
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
    parser.add_argument(
        "--history-store",
        help=(
            "root directory for immutable yearly history and chart objects; "
            "defaults to a 'history' directory beside --db"
        ),
    )
    parser.add_argument(
        "--raw-store",
        help=(
            "root directory for the immutable raw-artifact store; "
            "defaults to a 'raw' directory beside --db"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("backfill", "daily", "compose"):
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
    """Create the local baseline table for successfully published candidates."""
    connection.execute(
        "CREATE TABLE IF NOT EXISTS published_candidate_coverage ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL, source_id TEXT NOT NULL, "
        "expected_date TEXT, loaded_date TEXT, instrument_count INTEGER NOT NULL, "
        "missing_ratios_json TEXT NOT NULL, dataset_id TEXT NOT NULL, published_at TEXT NOT NULL)"
    )
    # Earlier R01 builds may have recorded repeated rows for a retry. Retain
    # the newest snapshot before enforcing one row per candidate/source.
    connection.execute(
        "DELETE FROM published_candidate_coverage WHERE id NOT IN ("
        "SELECT MAX(id) FROM published_candidate_coverage GROUP BY scope, dataset_id, source_id)"
    )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_published_candidate_coverage_snapshot "
        "ON published_candidate_coverage(scope, dataset_id, source_id)"
    )
    connection.commit()


def _candidate_scope(sources: Sequence[str]) -> str:
    return json.dumps({"sources": sorted(sources)}, sort_keys=True)


def _last_published_candidate_coverage(
    connection: sqlite3.Connection, sources: Sequence[str]
) -> tuple[CandidateCoverage, ...]:
    scope = _candidate_scope(sources)
    rows = connection.execute(
        "SELECT source_id, expected_date, loaded_date, instrument_count, missing_ratios_json "
        "FROM published_candidate_coverage WHERE scope = ? AND dataset_id = ("
        "SELECT dataset_id FROM published_candidate_coverage WHERE scope = ? ORDER BY id DESC LIMIT 1)",
        (scope, scope),
    ).fetchall()
    return tuple(
        CandidateCoverage(
            str(source_id),
            date.fromisoformat(str(expected)) if expected else None,
            date.fromisoformat(str(loaded)) if loaded else None,
            int(instrument_count),
            json.loads(str(missing_ratios)),
        )
        for source_id, expected, loaded, instrument_count, missing_ratios in rows
    )


def _record_published_candidate_coverage(
    connection: sqlite3.Connection, sources: Sequence[str], coverage: Sequence[CandidateCoverage], dataset_id: str
) -> None:
    now = datetime.now(UTC).isoformat()
    connection.executemany(
        "INSERT INTO published_candidate_coverage("
        "scope, source_id, expected_date, loaded_date, instrument_count, missing_ratios_json, dataset_id, published_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(scope, dataset_id, source_id) DO UPDATE SET "
        "expected_date=excluded.expected_date, loaded_date=excluded.loaded_date, "
        "instrument_count=excluded.instrument_count, missing_ratios_json=excluded.missing_ratios_json, "
        "published_at=excluded.published_at",
        [
            (
                _candidate_scope(sources), item.source_id,
                item.expected_date.isoformat() if item.expected_date else None,
                item.loaded_date.isoformat() if item.loaded_date else None,
                item.instrument_count, json.dumps(item.missing_ratios, sort_keys=True), dataset_id, now,
            )
            for item in coverage
        ],
    )
    connection.commit()


def _expected_date(source_id: str, effective_date: date) -> date | None:
    """Return the explicit publication expectation for each wired source."""

    if source_id == "amfi-nav":
        return effective_date if effective_date.weekday() < 5 else None
    return effective_date


def _candidate_coverage(
    sources: Sequence[str], effective_date: date, build: Any | None, build_error: str | None
) -> tuple[CandidateCoverage, ...]:
    loaded_dates: dict[str, date] = {}
    counts: dict[str, int] = {source: 0 for source in sources}
    if build is not None:
        for source in build.candidate["tables"]["sources"]:
            loaded_dates[str(source["source_id"])] = date.fromisoformat(str(source["effective_date"]))
        for instrument in build.candidate["tables"]["instruments"]:
            metadata = instrument.get("metadata", {})
            source_id = instrument.get("source_id") or metadata.get("source_id")
            if source_id is None and instrument.get("provider") == "amfi":
                source_id = "amfi-nav"
            if source_id in counts:
                counts[str(source_id)] += 1
    coverage: list[CandidateCoverage] = []
    for source_id in sources:
        expected = _expected_date(source_id, effective_date)
        loaded = loaded_dates.get(source_id)
        reasons: list[str] = []
        missing_ratios: dict[str, float] = {}
        if expected is None and loaded != effective_date:
            # A holiday is not missing source data. It still cannot reuse an
            # older candidate as if it were a new run, however: that would
            # incorrectly report a stale dataset as freshly published.
            reasons.append(f"{source_id}: no candidate artifact is available for the not-applicable date")
        elif expected is not None and build_error is not None:
            reasons.append(f"{source_id}: candidate build failed: {build_error}")
        elif expected is not None and loaded != expected:
            # A delayed source is operationally visible but does not on its
            # own invalidate a candidate; coverage/count gates decide whether
            # its prior data can still be safely served.
            missing_ratios["expected_date"] = 1.0
        coverage.append(CandidateCoverage(source_id, expected, loaded, counts[source_id], missing_ratios, tuple(reasons)))
    return tuple(coverage)


def _run_compose(
    connection: sqlite3.Connection,
    publisher: D1Publisher,
    raw_store: LocalRawStore,
    history_store: HistoryStore,
    sources: Sequence[str],
    result: BackfillResult,
    args: argparse.Namespace,
) -> int:
    """S04's schedule-aware entry point: ``market-pipeline compose``.

    This is a real, separately reachable production invocation of
    ``jobs.composed_candidate.run_scheduled_refresh`` -- not only a library
    function exercised by tests. Today, every requested source is admitted
    as ``acquisition_mode="supplied"`` through this same manifest-based
    checkpoint step ``daily``/``backfill`` already use (no NSE/reference
    ``SourceInput`` can be supplied via this CLI yet -- that remains gated on
    the still-pending NSE supplied-use permission, unchanged by this
    command), so a schedule built purely from ``sources`` and this pipeline's
    own explicit per-source calendar (:func:`_expected_date`) is the honest
    schedule this CLI can express today. Requesting only ``amfi-nav`` behaves
    identically to ``daily``'s own promotion outcome (both build through
    :func:`~market_pipeline.jobs.composed_candidate.build_composed_candidate`,
    whose AMFI-only path is a proven byte-for-byte passthrough of
    ``build_candidate``) -- this command additionally reports the
    per-source :class:`~market_pipeline.jobs.composed_candidate.SourceStatus`
    the plan's daily-execution interface asks for.
    """

    schedule = [
        SourceScheduleEntry(
            source_id=source_id,
            category=source_id,
            acquisition_mode="supplied",
            expected_date=_expected_date(source_id, result.end),
        )
        for source_id in sources
    ]
    refresh = run_scheduled_refresh(
        connection,
        publisher,
        raw_store,
        schedule,
        result.end,
        amfi_source_ids=sources,
        history_store=history_store,
        min_coverage_ratio=args.min_coverage_ratio,
        previous_count=args.previous_count,
    )
    storage = publisher.budget_report()
    storage_report = budget_report(
        used=storage.used_bytes,
        limit=storage.limit_bytes,
        warning_threshold=storage.warning_threshold,
        source="d1",
    )
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
        "budget": storage_report.as_dict(),
        "refresh": refresh.as_dict(),
    }, sort_keys=True))
    connection.close()
    if refresh.status == "skipped":
        # Never silently label a no-op schedule a completed refresh, but a
        # day with nothing scheduled is not itself a failure either.
        return 0
    if refresh.status == "blocked":
        return 3
    if refresh.status == "failed":
        return 4
    return 0


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
    # Raw artifacts are persisted immutably (URL, retrieval time, effective
    # date, checksum, adapter version, terms reference) *before* they are
    # normalized, and the publication stage re-reads them from here. Without a
    # raw store the pipeline would have no auditable record and no history to
    # compute a twelve-month return from.
    raw_root = Path(args.raw_store) if args.raw_store else Path(args.db).resolve().parent / "raw"
    raw_store = LocalRawStore(raw_root)
    history_root = Path(args.history_store) if args.history_store else Path(args.db).resolve().parent / "history"
    history_store = LocalHistoryStore(history_root)
    publisher = D1Publisher(connection, budget_limit_bytes=args.d1_budget_limit_bytes)
    publisher.initialize_schema()
    _ensure_run_history_table(connection)
    if args.command in ("daily", "compose"):
        if args.date is None:
            _parser().error(f"{args.command} requires --date")
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
        if args.command in ("daily", "compose"):
            result = run_daily(connection, fetcher, sources, effective_date=start, raw_store=raw_store, strict_coverage=False, budget_sources=(publisher,))
        else:
            result = BackfillJob(connection, sources, fetcher, raw_store=raw_store, strict_coverage=False, budget_sources=(publisher,)).run(start, end)
    except (CoverageError, SourcePolicyError, ValueError, RuntimeError) as exc:
        connection.close()
        print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2

    if args.command == "compose":
        return _run_compose(connection, publisher, raw_store, history_store, sources, result, args)

    # Safety gate: before any future promotion step runs, reconcile the
    # candidate run's coverage against the last known-good baseline and check
    # storage budget.
    try:
        # S04: every daily/backfill candidate is now built through the
        # composed-candidate seam (jobs/composed_candidate.py), not
        # build_candidate directly. When only amfi-nav (or any set of
        # sources with no admitted NSE/reference SourceInputs, which this
        # command has no way to supply yet) is requested, this is a proven
        # byte-for-byte passthrough of build_candidate's own output -- see
        # jobs.composed_candidate.compose_datasets's own docstring and
        # pipeline/tests/integration/test_multi_source_publication.py's
        # `test_amfi_only_composition_is_a_pure_passthrough`. This is what
        # gives the composed-build machinery a real, always-exercised
        # production entry point rather than only a library function
        # reachable from tests.
        build = build_composed_candidate(connection, raw_store, amfi_source_ids=sources, effective_date=result.end)
        build_error = None
    except PublicationInputError as exc:
        build = None
        build_error = str(exc)
    coverage = _candidate_coverage(sources, result.end, build, build_error)
    total_days = (result.end - result.start).days + 1
    source_observations = {
        source: SourceObservation(
            expected=_expected_date(source, result.end) is not None,
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
    source_baselines = _last_published_candidate_coverage(connection, sources)
    if args.previous_count is not None:
        previous_count = args.previous_count
        # Keep the legacy aggregate override for a one-source invocation, but
        # never let it hide a per-source drop in a multi-source candidate.
        if len(coverage) <= 1:
            source_baselines = ()
    else:
        previous_count = (
            sum(item.instrument_count for item in source_baselines)
            if source_baselines else sum(item.instrument_count for item in coverage)
        )
    reconciliation = reconcile(
        previous=previous_count,
        candidate=sum(item.instrument_count for item in coverage),
        min_coverage_ratio=args.min_coverage_ratio,
        sources=source_observations,
        candidate_coverage=coverage,
        source_baselines=source_baselines,
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
    # Normalization -> analytics -> stage/promote. This only runs after the
    # Task 13 gate says the candidate is safe; a blocked run leaves the last
    # known-good dataset active and untouched.
    publication = publish_checkpointed_dataset(
        connection,
        publisher,
        raw_store,
        sources,
        effective_date=result.end,
        safe_to_promote=safe_to_promote,
        blocking_reasons=blocking_reasons,
        history_store=history_store,
        build=build,
    )
    if publication.promoted and publication.dataset_id is not None:
        _record_published_candidate_coverage(connection, sources, coverage, publication.dataset_id)

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
        "publication": publication.as_dict(),
    }, sort_keys=True))
    connection.close()
    if not safe_to_promote:
        # Reconciliation/budget blocked the candidate: nothing was staged or
        # promoted, so the previously active dataset remains the served one.
        return 3
    if not publication.promoted:
        # The gate passed but the dataset could not be built or staged. That is
        # a real failure to surface, and still leaves the last known-good
        # dataset active.
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
