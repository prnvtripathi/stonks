"""Versioned, content-hashed publication bundles (R07 / finding F14).

A publication bundle is the *authoritative* description of exactly which
derived R2 objects a published dataset needs in order to serve correctly.
Earlier code (see ``d1_export._manifest_keys`` before this change) discovered
those objects by globbing the local history root at export time -- a
mechanism that both (a) could not distinguish "objects this candidate wrote"
from "objects some unrelated older dataset left lying around", and (b)
depended on the historical partition being addressed by a fixed
``{asset}/{instrument}/{year}.parquet`` key, so a corrected or retried
candidate could overwrite the last known-good object under the same key.

This module fixes both problems:

* :func:`build_bundle` records only the object entries a candidate itself
  wrote (``{key, sha256, bytes, kind}``), restricted to a rolling
  three-calendar-year serving window. The exporter (``publication/d1_export``)
  reads this bundle instead of scanning the filesystem.
* Every historical object entry's key already embeds its own content hash
  (:func:`market_pipeline.storage.history_store.history_key`), so two
  different candidates for the same (asset class, instrument, year) can never
  collide on a key.
* :func:`plan_garbage_collection` only ever proposes *derived* (``history/``
  or ``charts/``) objects that are unreachable from every bundle the caller
  says must stay reachable (typically: the active dataset's bundle and the
  previous successfully-promoted dataset's bundle, for rollback). It never
  proposes raw artifacts, and it only ever produces a dry-run list -- nothing
  in this module deletes anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace as _replace
from datetime import date
from hashlib import sha256
from typing import Any, Mapping, Sequence

BUNDLE_VERSION = 1

#: The bundle serves the effective date's calendar year and the two calendar
#: years before it. Analytics may still read further back within one build
#: (a "calculation boundary observation"), but that older data is not part of
#: what this bundle asks the export/sync layers to serve or keep warm.
SERVING_WINDOW_YEARS = 3

_DERIVED_PREFIXES = ("history/", "charts/")


class BundleError(ValueError):
    """Raised when a publication bundle cannot be constructed or is malformed."""


@dataclass(frozen=True)
class BundleObject:
    """One object entry: ``{key, sha256, bytes, kind}`` per the R07 interface."""

    key: str
    sha256: str
    bytes: int
    kind: str  # "history" | "chart"

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "sha256": self.sha256, "bytes": self.bytes, "kind": self.kind}

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> "BundleObject":
        try:
            key, digest, size, kind = str(data["key"]), str(data["sha256"]), int(data["bytes"]), str(data["kind"])
        except (KeyError, TypeError, ValueError) as exc:
            raise BundleError("bundle object entry is malformed") from exc
        if kind not in ("history", "chart"):
            raise BundleError(f"bundle object entry has an unsupported kind: {kind}")
        if size < 0:
            raise BundleError("bundle object entry has a negative size")
        return BundleObject(key=key, sha256=digest, bytes=size, kind=kind)


@dataclass(frozen=True)
class PublicationBundle:
    """A versioned, content-hashed description of one candidate's objects."""

    version: int
    dataset_id: str
    input_manifest_hash: str
    source_dates: tuple[str, ...]
    projection_version: str
    sql_checksum: str | None
    objects: tuple[BundleObject, ...]

    @property
    def object_keys(self) -> tuple[str, ...]:
        return tuple(item.key for item in self.objects)

    def with_sql_checksum(self, checksum: str) -> "PublicationBundle":
        """Return a copy of this bundle with its SQL checksum finalized.

        The bundle is built once at candidate time (before the D1 import SQL
        exists) and finalized once at export time, once the exact SQL text is
        known. Every other field is fixed at build time.
        """

        if not checksum:
            raise BundleError("sql_checksum must not be empty")
        return _replace(self, sql_checksum=checksum)

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "dataset_id": self.dataset_id,
            "input_manifest_hash": self.input_manifest_hash,
            "source_dates": list(self.source_dates),
            "projection_version": self.projection_version,
            "sql_checksum": self.sql_checksum,
            "objects": [item.as_dict() for item in self.objects],
        }

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> "PublicationBundle":
        try:
            version = int(data["version"])
        except (KeyError, TypeError, ValueError) as exc:
            raise BundleError("publication bundle version is missing or invalid") from exc
        if version != BUNDLE_VERSION:
            raise BundleError(f"unsupported publication bundle version: {version}")
        try:
            dataset_id = str(data["dataset_id"])
            input_manifest_hash = str(data["input_manifest_hash"])
            source_dates = tuple(str(item) for item in data["source_dates"])
            projection_version = str(data["projection_version"])
            sql_checksum = data.get("sql_checksum")
            objects = tuple(BundleObject.from_dict(item) for item in data["objects"])
        except (KeyError, TypeError, ValueError) as exc:
            raise BundleError("publication bundle is malformed") from exc
        if not dataset_id or not input_manifest_hash or not projection_version:
            raise BundleError("publication bundle is missing required identity fields")
        return PublicationBundle(
            version=version,
            dataset_id=dataset_id,
            input_manifest_hash=input_manifest_hash,
            source_dates=source_dates,
            projection_version=projection_version,
            sql_checksum=(str(sql_checksum) if sql_checksum is not None else None),
            objects=objects,
        )


def serving_years(effective_date: date, *, window: int = SERVING_WINDOW_YEARS) -> tuple[int, ...]:
    """Return the rolling calendar-year serving window, oldest year first."""

    if window < 1:
        raise BundleError("serving window must include at least one calendar year")
    end = effective_date.year
    return tuple(range(end - window + 1, end + 1))


def select_object_entries(
    candidate_entries: Sequence[BundleObject],
    *,
    effective_date: date,
    window: int = SERVING_WINDOW_YEARS,
) -> tuple[BundleObject, ...]:
    """Restrict a candidate's written objects to the rolling serving window.

    Chart objects are dataset-scoped, not year-partitioned, so every chart
    entry the candidate wrote is always in scope. History entries older than
    the window are still valid, durably written objects -- they are simply
    not part of what *this* bundle asks the export/sync layers to serve.
    """

    years = set(serving_years(effective_date, window=window))
    selected: list[BundleObject] = []
    for entry in candidate_entries:
        if entry.kind == "chart":
            selected.append(entry)
            continue
        if entry.kind != "history":
            raise BundleError(f"unrecognized bundle object kind: {entry.kind}")
        parts = entry.key.split("/")
        if len(parts) != 5 or parts[0] != "history":
            raise BundleError(f"history object key has an unexpected shape: {entry.key}")
        try:
            year = int(parts[3])
        except ValueError as exc:
            raise BundleError(f"history object key has no calendar year: {entry.key}") from exc
        if year in years:
            selected.append(entry)
    return tuple(sorted(selected, key=lambda item: item.key))


def build_bundle(
    *,
    dataset_id: str,
    input_manifest_hash: str,
    source_dates: Sequence[str],
    projection_version: str,
    candidate_entries: Sequence[BundleObject],
    effective_date: date,
    sql_checksum: str | None = None,
    window: int = SERVING_WINDOW_YEARS,
) -> PublicationBundle:
    """Build the immutable, authoritative object manifest for one candidate."""

    if not dataset_id:
        raise BundleError("dataset_id is required")
    if not input_manifest_hash:
        raise BundleError("input_manifest_hash is required")
    if not projection_version:
        raise BundleError("projection_version is required")
    objects = select_object_entries(candidate_entries, effective_date=effective_date, window=window)
    if not objects:
        raise BundleError("publication bundle has no reachable objects")
    return PublicationBundle(
        version=BUNDLE_VERSION,
        dataset_id=dataset_id,
        input_manifest_hash=input_manifest_hash,
        source_dates=tuple(sorted(set(source_dates))),
        projection_version=projection_version,
        sql_checksum=sql_checksum,
        objects=objects,
    )


def sql_checksum(sql_text: str) -> str:
    """Return the checksum recorded as a finalized bundle's ``sql_checksum``."""

    return sha256(sql_text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class GarbageCandidate:
    key: str
    bytes: int


@dataclass(frozen=True)
class GarbageCollectionPlan:
    """A dry-run list of derived objects unreferenced by any reachable bundle.

    Nothing in this module deletes anything: this is a plan to review (and,
    separately, to act on) -- never an execution.
    """

    reachable: tuple[str, ...]
    candidates: tuple[GarbageCandidate, ...]

    @property
    def candidate_keys(self) -> tuple[str, ...]:
        return tuple(item.key for item in self.candidates)

    @property
    def candidate_bytes(self) -> int:
        return sum(item.bytes for item in self.candidates)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": True,
            "reachable_count": len(self.reachable),
            "candidate_count": len(self.candidates),
            "candidate_bytes": self.candidate_bytes,
            "candidates": list(self.candidate_keys),
        }


def plan_garbage_collection(
    reachable_bundles: Sequence[PublicationBundle],
    existing_objects: Mapping[str, int],
) -> GarbageCollectionPlan:
    """List existing derived objects that no reachable bundle references.

    ``existing_objects`` maps every object key currently present in the store
    to its size in bytes. ``reachable_bundles`` must include every bundle that
    must stay retrievable -- at minimum the active dataset's bundle and the
    previous successfully-promoted dataset's bundle (for rollback), and any
    additional bundle a saved run still references. Only keys under
    ``history/`` or ``charts/`` are ever candidates: raw artifacts are never
    proposed for deletion here, regardless of reachability, because quota
    pressure must never be solved by discarding the auditable raw record.
    """

    reachable: set[str] = set()
    for bundle in reachable_bundles:
        reachable.update(bundle.object_keys)
    candidates = tuple(
        GarbageCandidate(key=key, bytes=int(size))
        for key, size in sorted(existing_objects.items())
        if key.startswith(_DERIVED_PREFIXES) and key not in reachable
    )
    return GarbageCollectionPlan(reachable=tuple(sorted(reachable)), candidates=candidates)


__all__ = [
    "BUNDLE_VERSION",
    "SERVING_WINDOW_YEARS",
    "BundleError",
    "BundleObject",
    "GarbageCandidate",
    "GarbageCollectionPlan",
    "PublicationBundle",
    "build_bundle",
    "plan_garbage_collection",
    "select_object_entries",
    "serving_years",
    "sql_checksum",
]
