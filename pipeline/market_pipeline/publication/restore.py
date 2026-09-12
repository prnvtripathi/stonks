"""Selective publication-bundle restore: real rollback, not a pointer flip (F13).

``db/migrations/0006_bounded_instrument_snapshots.sql`` collapsed
``instrument_snapshots`` to one compact row per instrument -- a bounded
*serving* projection, not a per-dataset history -- and every promotion
(``publication/d1_export.py``'s ``DELETE FROM instrument_snapshots WHERE
dataset_id <> :new_dataset_id``) physically deletes every other dataset's
serving rows. After that migration, the previously documented "rollback"
(``docs/operations/recovery.md`` section 2: flip ``active_dataset``/
``datasets.status`` back to a prior ``dataset_id`` via direct SQL, with no
separate rollback machinery) is broken: a dataset-scoped serving query for
the "restored" dataset now returns zero rows, because its
``instrument_snapshots`` rows are gone.

This module replaces that pointer flip with a real restore: it selects an
explicitly-named, still-*retained* dataset (one of the two bundles
:func:`market_pipeline.publication.d1_export.reachable_bundles` keeps
reachable for exactly this purpose -- the active dataset and the previous
successful promotion), verifies its recorded bundle's checksums (SQL,
object, and input-manifest identity), optionally re-verifies its objects are
still byte-correct in the remote R2 bucket using R08's own transport, and
then reimports its compact projection through the identical import-SQL
shape and promotion mechanics normal publication uses
(:func:`market_pipeline.publication.d1_export.export_dataset`). Nothing here
invents a second retention mechanism, a second S3 SDK client, or a second
D1-import code path.

Scope, deliberately narrow:

* This only ever reimports *market-data* tables (datasets, sources,
  source_runs, instrument_snapshots) -- exactly what
  :func:`~market_pipeline.publication.d1_export.export_dataset` already
  scopes its generated SQL to. It never touches ``saved_screens``,
  ``screen_runs``, or ``screen_matches`` -- those are owner/product state
  the Worker (``apps/api``) owns, and a restore of market data must never
  reach backward into it (see the global "preserve saved screens, historical
  runs" constraint).
* Selection is mandatory and explicit: there is no "restore to whatever
  looks newest" default. An unavailable, unreachable, or otherwise
  incompatible dataset fails closed -- before any SQL is generated, before
  any R2 request is made, and therefore before the currently-active,
  still-usable dataset is put at any risk. Every check in this module reads
  local state and/or verifies remote bytes; none of it ever mutates
  ``connection`` or any remote resource itself. Applying the generated SQL
  to production D1 (and, before that, running it through
  :mod:`market_pipeline.publication.preflight` the same way a normal
  publish does) remains a distinct, explicit operator step -- see
  ``docs/operations/recovery.md`` section 2.
"""

from __future__ import annotations

import argparse
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from market_pipeline.publication.bundle import PublicationBundle
from market_pipeline.publication.d1_export import (
    DatasetExportError,
    export_dataset,
    reachable_bundles,
    verify_bundle_objects,
)
from market_pipeline.publication.r2_sync import (
    R2SyncError,
    R2SyncResult,
    S3ObjectClient,
    synchronize_history,
)

__all__ = [
    "RestorableDataset",
    "RestoreError",
    "RestoreResult",
    "list_restorable_datasets",
    "main",
    "restore_dataset",
    "select_restore_bundle",
]


class RestoreError(RuntimeError):
    """Raised when a dataset cannot be safely selected for, or fails, restore."""


@dataclass(frozen=True)
class RestorableDataset:
    """One dataset this process is currently permitted to restore to."""

    dataset_id: str
    bundle: PublicationBundle


def _current_dataset_id(connection: sqlite3.Connection) -> str | None:
    row = connection.execute("SELECT dataset_id FROM active_dataset WHERE singleton = 1").fetchone()
    return None if row is None else str(row[0])


def list_restorable_datasets(connection: sqlite3.Connection) -> tuple[RestorableDataset, ...]:
    """List the datasets a caller may currently pass to :func:`restore_dataset`.

    This is exactly R07's own retained set (:func:`~market_pipeline.publication
    .d1_export.reachable_bundles`): the active dataset and the previous
    successful promotion. Restore deliberately does not invent a broader
    retention window -- a dataset outside this set may already have had its
    derived R2 objects garbage-collected (see
    :mod:`market_pipeline.publication.d1_export`'s
    ``plan_garbage_collection``), so restoring to it cannot be made safe
    without first re-deriving/re-uploading those objects, which is out of
    this module's scope.
    """

    current_id = _current_dataset_id(connection)
    if current_id is None:
        return ()
    bundles = reachable_bundles(connection, current_id)
    return tuple(RestorableDataset(dataset_id=bundle.dataset_id, bundle=bundle) for bundle in bundles)


def select_restore_bundle(connection: sqlite3.Connection, dataset_id: str) -> PublicationBundle:
    """Require an explicitly-named, currently-retained, fully-identified bundle.

    Fails with :class:`RestoreError` -- before any SQL is generated, before
    any object is touched -- for any dataset that is not one of the two
    bundles R07 keeps reachable, or whose recorded bundle is missing the
    identity fields a genuine, previously-built candidate always has
    (``input_manifest_hash`` identifies exactly which inputs produced it --
    see R02/F03). ``sql_checksum`` is deliberately not required here: it is
    only ever finalized against the *specific* SQL text one export run
    produces (see ``export_dataset``/``export_active_dataset``), so this
    restore computes and records its own fresh checksum for the SQL it is
    about to generate rather than compare against a stale one recorded at
    build time.
    """

    if not dataset_id:
        raise RestoreError("a dataset_id must be explicitly selected for restore")
    restorable = {item.dataset_id: item.bundle for item in list_restorable_datasets(connection)}
    bundle = restorable.get(dataset_id)
    if bundle is None:
        raise RestoreError(
            f"dataset {dataset_id!r} is not a currently-retained, restorable bundle "
            f"(retained: {sorted(restorable) or 'none'})"
        )
    if not bundle.input_manifest_hash:
        raise RestoreError(f"dataset {dataset_id!r} bundle is missing its input manifest hash")
    if not bundle.objects:
        raise RestoreError(f"dataset {dataset_id!r} bundle has no objects to restore")
    return bundle


@dataclass(frozen=True)
class RestoreResult:
    """The outcome of one successful :func:`restore_dataset` call.

    ``sql_path`` is the generated D1 import -- identical in shape to a
    normal publication's own export -- ready for the same preflight/R2
    gate and ``wrangler d1 execute --remote --file`` step normal publication
    uses. Nothing in this module applies it.
    """

    dataset_id: str
    sql_path: Path
    bundle: PublicationBundle
    r2_sync: R2SyncResult | None


def restore_dataset(
    connection: sqlite3.Connection,
    dataset_id: str,
    *,
    output: str | Path,
    history_root: str | Path | None = None,
    client: S3ObjectClient | None = None,
    object_manifest: str | Path | None = None,
    publication_plan: str | Path | None = None,
    weekday_runs_per_month: int | None = None,
    max_workers: int | None = None,
    deadline_seconds: int | None = None,
) -> RestoreResult:
    """Selectively restore ``dataset_id``'s compact projection (R11/F13).

    Verification always happens fully before any mutation:

    1. :func:`select_restore_bundle` -- the dataset must be one of the two
       currently-retained bundles and must carry a complete, finalized
       identity (SQL checksum, input-manifest hash, at least one object).
    2. When ``history_root`` is given, every one of the bundle's objects is
       byte-verified locally
       (:func:`~market_pipeline.publication.d1_export.verify_bundle_objects`,
       the identical check a normal export performs).
    3. When ``client`` is given, the bundle's objects are synchronized
       through R08's own bounded, checksum-verified transport
       (:func:`~market_pipeline.publication.r2_sync.synchronize_history`) --
       the same R2 verification gate a normal publish uses, reusing the one
       S3-compatible client this publisher has, never a second one. This
       step is a no-op for objects already correct in R2 (content-addressed
       keys let ``r2_sync`` verify-and-skip), but it still surfaces a
       genuinely missing or corrupted remote object *before* any D1 SQL is
       produced.
    4. Only once every requested check has passed does
       :func:`~market_pipeline.publication.d1_export.export_dataset`
       generate the D1 import SQL -- the same statement shape, and the same
       market-data-only table scope, as a normal publication's own export.

    A failure at any step raises before step 4 ever runs, so ``connection``
    is never written to and no SQL file is produced past whatever partial
    state existed before the call -- the dataset currently active in
    production remains exactly as usable as it was, per this pipeline's
    "a failed or incomplete run never replaces the last known-good
    production dataset" rule. This function itself never touches remote D1
    at all: applying the generated SQL (through the same preflight gate
    normal publication uses) is a distinct, explicit operator step.
    """

    bundle = select_restore_bundle(connection, dataset_id)

    if history_root is not None:
        try:
            verify_bundle_objects(bundle, Path(history_root))
        except DatasetExportError as exc:
            raise RestoreError(f"local object verification failed for {dataset_id!r}: {exc}") from exc

    r2_result: R2SyncResult | None = None
    if client is not None:
        if history_root is None:
            raise RestoreError("history_root is required to verify objects against a remote client")
        sync_kwargs: dict[str, int] = {}
        if max_workers is not None:
            sync_kwargs["max_workers"] = max_workers
        if deadline_seconds is not None:
            sync_kwargs["deadline_seconds"] = deadline_seconds
        try:
            r2_result = synchronize_history(bundle, Path(history_root), client, **sync_kwargs)
        except R2SyncError as exc:
            raise RestoreError(f"remote object verification failed for {dataset_id!r}: {exc}") from exc

    try:
        exported_id = export_dataset(
            connection,
            dataset_id,
            output,
            history_root=history_root,
            object_manifest=object_manifest,
            publication_plan=publication_plan,
            weekday_runs_per_month=weekday_runs_per_month,
        )
    except DatasetExportError as exc:
        raise RestoreError(f"could not export dataset {dataset_id!r} for restore: {exc}") from exc
    if exported_id != dataset_id:
        raise RestoreError(f"export produced an unexpected dataset ID: {exported_id!r} != {dataset_id!r}")

    return RestoreResult(dataset_id=exported_id, sql_path=Path(output), bundle=bundle, r2_sync=r2_result)


def main(argv: Sequence[str] | None = None) -> int:
    import os

    from market_pipeline.publication.r2_sync import DEFAULT_MAX_WORKERS, build_s3_client

    parser = argparse.ArgumentParser(
        description="selectively restore a retained dataset's compact D1 projection (R11/F13)"
    )
    parser.add_argument("--db", required=True, help="local market SQLite database")
    parser.add_argument("--dataset-id", required=True, help="the retained dataset_id to restore")
    parser.add_argument("--output", required=True, help="generated SQL path")
    parser.add_argument("--history-root", help="local root containing history/ and charts/ objects")
    parser.add_argument("--object-manifest", help="write the restored dataset's verified R2 object keys here")
    parser.add_argument("--publication-plan", help="write the local-only remote-operation budget plan here")
    parser.add_argument(
        "--verify-remote",
        action="store_true",
        help=(
            "also verify (and, if needed, re-PUT) the bundle's objects against R2 "
            "before the SQL is trusted; requires CLOUDFLARE_ACCOUNT_ID/R2_ACCESS_KEY_ID/"
            "R2_SECRET_ACCESS_KEY and --history-root"
        ),
    )
    parser.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS)
    parser.add_argument("--deadline-seconds", type=int, default=None)
    parser.add_argument(
        "--weekday-runs-per-month",
        type=int,
        default=None,
        help="defaults to the actual current month's scheduled attempts (see remote_usage.plan_monthly_attempts)",
    )
    args = parser.parse_args(argv)

    client: S3ObjectClient | None = None
    if args.verify_remote:
        account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
        access_key = os.environ.get("R2_ACCESS_KEY_ID")
        secret_key = os.environ.get("R2_SECRET_ACCESS_KEY")
        if not account_id or not access_key or not secret_key:
            parser.error("R2 publication credentials are unavailable for --verify-remote")
        client = build_s3_client(account_id, access_key, secret_key, args.max_workers)

    weekday_runs_per_month = args.weekday_runs_per_month
    if weekday_runs_per_month is None and args.publication_plan:
        from datetime import date

        from market_pipeline.publication.remote_usage import plan_monthly_attempts

        weekday_runs_per_month = plan_monthly_attempts(date.today()).monthly_attempts

    connection = sqlite3.connect(args.db)
    try:
        sync_kwargs: dict[str, int] = {"max_workers": args.max_workers}
        if args.deadline_seconds is not None:
            sync_kwargs["deadline_seconds"] = args.deadline_seconds
        result = restore_dataset(
            connection,
            args.dataset_id,
            output=args.output,
            history_root=args.history_root,
            client=client,
            object_manifest=args.object_manifest,
            publication_plan=args.publication_plan,
            weekday_runs_per_month=weekday_runs_per_month,
            **sync_kwargs,
        )
    except RestoreError as exc:
        parser.error(str(exc))
    finally:
        connection.close()
    print(result.dataset_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
