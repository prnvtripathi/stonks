"""Durable remote checkpoint archive for a clean/cache-missed runner (F12).

``.github/workflows/daily-data.yml`` relies entirely on ``actions/cache`` to
carry ``market.db`` (backfill checkpoints and the local publication history),
``raw/`` (immutable fetched artifact bodies), and ``history/`` (derived
Parquet/chart objects) between ephemeral runner instances. GitHub documents
cache eviction; a cache miss today means those three years of source and
checkpoint state are gone with no recovery path -- exactly the gap this
module closes.

This module adds a private, versioned checkpoint archive in the same R2
account R08 already publishes history to (a distinct ``checkpoints/``/
``state/`` key prefix, never the public serving keys under ``history/`` or
``charts/``), plus a durable ``state/latest-success.json`` pointer. It
deliberately reuses two pieces already built and tested elsewhere rather than
inventing parallel machinery:

* :func:`market_pipeline.publication.r2_sync.put_and_verify_object` /
  :func:`~market_pipeline.publication.r2_sync.get_and_verify_object` for the
  actual bounded, checksum-verified PUT/GET against the same
  ``S3ObjectClient`` R08 defined -- no second S3 SDK client.
* :class:`market_pipeline.storage.raw_store.LocalRawStore`'s own
  body/metadata/commit-marker integrity rule as the final, authoritative
  check that a restored raw triple is intact: this module downloads the
  three files a raw artifact is made of and then calls
  ``LocalRawStore.get(key)`` on the freshly written files, so "is this a
  valid immutable triple" is answered in exactly one place in the codebase.

``history/`` (derived Parquet/chart objects) is deliberately *not* part of
this archive: :mod:`market_pipeline.jobs.publish` already rebuilds it
deterministically from ``raw/`` on every run, so archiving it durably would
duplicate storage without adding recoverability. Likewise the reservation
ledger (``.cache/publication-ledger.json``) is a same-day accounting margin
(``LEDGER_ENTRY_RELEVANCE_SECONDS`` = 24h in
:mod:`market_pipeline.publication.remote_usage`) whose loss affects only that
margin, not correctness, so it is out of scope for durable restore.

Distinguishing checkpoint progress from remote success
--------------------------------------------------------
A checkpoint archive can be built and uploaded the moment a *local* candidate
exists -- that is durable evidence of fetch/normalization progress, nothing
more. It must never be mistaken for confirmation that the corresponding
dataset was actually promoted in production D1/R2. Two separate, private
objects enforce this:

* ``checkpoints/<dataset_id>/manifest-<sha256>.json`` -- the complete,
  content-addressed description of one checkpoint archive. Uploading it only
  asserts "this local state was durably archived", not "this was published".
* ``state/latest-success.json`` -- written only by :func:`promote_latest_success`,
  and only after the caller supplies an independently, remotely verified
  active dataset ID (the same kind of check
  ``docs/operations/daily-run.md`` describes D1 import verification doing).
  A failed or not-yet-attempted remote publish leaves this pointer exactly
  where it was; it can never be advanced speculatively.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from time import monotonic
from typing import Any, Mapping, Sequence

from market_pipeline.publication.r2_sync import (
    BUCKET,
    S3ObjectClient,
    get_and_verify_object,
    put_and_verify_object,
)
from market_pipeline.publication.remote_usage import ReservationLedgerEntry
from market_pipeline.storage.raw_store import ImmutableRawStoreError, LocalRawStore

ARCHIVE_VERSION = 1

#: Distinct from R08's public serving keys (``history/``, ``charts/``): this
#: is a private checkpoint/state namespace in the same bucket.
CHECKPOINT_PREFIX = "checkpoints"
STATE_LATEST_SUCCESS_KEY = "state/latest-success.json"

DEFAULT_DEADLINE_SECONDS = 4 * 60 * 60


class CheckpointArchiveError(RuntimeError):
    """Raised when a checkpoint archive cannot be built, verified, or restored."""


class NoCheckpointArchiveError(CheckpointArchiveError):
    """Raised only when no prior success has ever been recorded (a legitimate bootstrap).

    Callers must treat this differently from every other
    :class:`CheckpointArchiveError`: it means "nothing to restore yet", not
    "the remote archive is missing or corrupt". Any other failure --
    including a *present* pointer whose referenced archive fails
    verification -- must never be treated as this bootstrap case, or a
    genuinely lost/corrupted archive would silently look like day one.
    """


@dataclass(frozen=True)
class ObjectRef:
    key: str
    sha256: str
    bytes: int

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "sha256": self.sha256, "bytes": self.bytes}

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> "ObjectRef":
        try:
            key = str(data["key"])
            digest = str(data["sha256"])
            size = int(data["bytes"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CheckpointArchiveError("checkpoint archive object entry is malformed") from exc
        if not key or not digest or size < 0:
            raise CheckpointArchiveError("checkpoint archive object entry is malformed")
        return ObjectRef(key=key, sha256=digest.lower(), bytes=size)


@dataclass(frozen=True)
class RawTripleRef:
    """One immutable raw artifact, identified the same way ``raw_store.py`` does.

    Only the body's key/checksum/size are recorded here; the metadata and
    commit-marker sidecar keys are always derived the same way
    ``storage/raw_store.py`` derives them (``<key>.metadata.json`` /
    ``<key>.commit.json``), so there is exactly one place that defines that
    shape.
    """

    object_key: str
    body_sha256: str
    body_bytes: int

    @property
    def metadata_key(self) -> str:
        return f"{self.object_key}.metadata.json"

    @property
    def commit_key(self) -> str:
        return f"{self.object_key}.commit.json"

    def as_dict(self) -> dict[str, Any]:
        return {"object_key": self.object_key, "body_sha256": self.body_sha256, "body_bytes": self.body_bytes}

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> "RawTripleRef":
        try:
            key = str(data["object_key"])
            digest = str(data["body_sha256"])
            size = int(data["body_bytes"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CheckpointArchiveError("checkpoint archive raw triple entry is malformed") from exc
        if not key or not digest or size < 0:
            raise CheckpointArchiveError("checkpoint archive raw triple entry is malformed")
        return RawTripleRef(object_key=key, body_sha256=digest.lower(), body_bytes=size)


@dataclass(frozen=True)
class CheckpointArchiveManifest:
    """A versioned, checksum-linked description of one checkpoint archive.

    Building or uploading this manifest is checkpoint *progress*, never
    remote publication success -- see the module docstring.
    """

    version: int
    dataset_id: str
    input_manifest_hash: str
    source_dates: tuple[str, ...]
    created_at: str
    sqlite_backup: ObjectRef
    raw_triples: tuple[RawTripleRef, ...]

    @property
    def manifest_key(self) -> str:
        # Content-addressed, like every other immutable object this
        # publisher writes: two builds for the same dataset_id (an exact
        # retry, seconds apart, differing only in `created_at`) must never
        # overwrite each other's manifest object, or a pointer already
        # promoted against the first one would find its checksum broken by
        # the second build's PUT to the same mutable key.
        return f"{CHECKPOINT_PREFIX}/{self.dataset_id}/manifest-{self.manifest_sha256}.json"

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "dataset_id": self.dataset_id,
            "input_manifest_hash": self.input_manifest_hash,
            "source_dates": list(self.source_dates),
            "created_at": self.created_at,
            "sqlite_backup": self.sqlite_backup.as_dict(),
            "raw_triples": [item.as_dict() for item in self.raw_triples],
        }

    def canonical_bytes(self) -> bytes:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

    @property
    def manifest_sha256(self) -> str:
        return sha256(self.canonical_bytes()).hexdigest()

    @staticmethod
    def from_dict(data: Mapping[str, Any]) -> "CheckpointArchiveManifest":
        try:
            version = int(data["version"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CheckpointArchiveError("checkpoint archive manifest version is missing or invalid") from exc
        if version != ARCHIVE_VERSION:
            raise CheckpointArchiveError(f"unsupported checkpoint archive version: {version}")
        try:
            dataset_id = str(data["dataset_id"])
            input_manifest_hash = str(data["input_manifest_hash"])
            source_dates = tuple(str(item) for item in data["source_dates"])
            created_at = str(data["created_at"])
            sqlite_backup = ObjectRef.from_dict(data["sqlite_backup"])
            raw_triples = tuple(RawTripleRef.from_dict(item) for item in data["raw_triples"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CheckpointArchiveError("checkpoint archive manifest is malformed") from exc
        if not dataset_id or not input_manifest_hash:
            raise CheckpointArchiveError("checkpoint archive manifest is missing required identity fields")
        return CheckpointArchiveManifest(
            version=version,
            dataset_id=dataset_id,
            input_manifest_hash=input_manifest_hash,
            source_dates=source_dates,
            created_at=created_at,
            sqlite_backup=sqlite_backup,
            raw_triples=raw_triples,
        )


@dataclass(frozen=True)
class CheckpointUploadResult:
    manifest: CheckpointArchiveManifest
    put_attempts: int
    get_attempts: int
    verified_bytes: int


@dataclass(frozen=True)
class CheckpointRestoreResult:
    manifest: CheckpointArchiveManifest
    get_attempts: int
    verified_bytes: int


@dataclass(frozen=True)
class IdentityCheckResult:
    """Result of validating a cache-hit runner's local state against the
    authoritative remote manifest, without downloading any bulk object."""

    matches: bool
    local_dataset_id: str | None
    remote_dataset_id: str
    local_input_manifest_hash: str | None
    remote_input_manifest_hash: str
    raw_store_complete: bool | None
    raw_triples_expected: int
    raw_triples_missing: int


def _read_local_dataset_identity(db_path: Path) -> tuple[str, str] | None:
    """Return (dataset_id, input_manifest_hash) for the locally active dataset, if any."""

    if not db_path.exists():
        return None
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT d.dataset_id, d.metadata_json FROM datasets d "
            "JOIN active_dataset a ON a.dataset_id = d.dataset_id WHERE a.singleton = 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        connection.close()
    if row is None:
        return None
    dataset_id, metadata_json = row
    try:
        metadata = json.loads(metadata_json) if metadata_json else {}
    except json.JSONDecodeError:
        metadata = {}
    input_manifest_hash = str(metadata.get("input_manifest_sha256") or "")
    return str(dataset_id), input_manifest_hash


def _read_source_dates(db_path: Path) -> tuple[str, ...]:
    connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT DISTINCT effective_date FROM backfill_checkpoints ORDER BY effective_date"
        ).fetchall()
    except sqlite3.OperationalError:
        return ()
    finally:
        connection.close()
    return tuple(str(row[0]) for row in rows)


def backup_sqlite_database(source_db: Path, dest_path: Path) -> ObjectRef:
    """Use SQLite's own backup API to produce a consistent, checksummed snapshot.

    The backup API (not a raw file copy) is used deliberately: it produces a
    transactionally consistent snapshot even while the source connection has
    pending WAL frames, which a plain byte copy of ``source_db`` could not
    guarantee.
    """

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    if dest_path.exists():
        dest_path.unlink()
    source = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)
    dest = sqlite3.connect(dest_path)
    try:
        source.backup(dest)
    finally:
        dest.close()
        source.close()
    body = dest_path.read_bytes()
    digest = sha256(body).hexdigest()
    key = f"{CHECKPOINT_PREFIX}/sqlite/{digest}/market.db"
    return ObjectRef(key=key, sha256=digest, bytes=len(body))


def _ensure_object(
    client: S3ObjectClient,
    *,
    key: str,
    local_path: Path,
    sha256_hex: str,
    size: int,
    deadline_at: float,
) -> tuple[int, int]:
    """Upload one object, reusing an already-correct remote copy when present.

    Content-addressed checkpoint objects (raw triples, the sqlite backup) are
    identical across an exact retry: checking first with a single GET avoids
    re-uploading -- and re-counting against R09's operation budget -- bytes
    that already made it to the remote store on a prior, interrupted attempt.
    """

    try:
        get_and_verify_object(client, key=key, sha256_hex=sha256_hex, size=size)
        return (0, 1)
    except Exception:  # noqa: BLE001 - any failure to confirm reuse falls through to a normal PUT
        pass
    result = put_and_verify_object(
        client, key=key, local_path=local_path, sha256_hex=sha256_hex, size=size, deadline_at=deadline_at
    )
    return (result.put_attempts, result.get_attempts)


def build_and_upload_checkpoint_archive(
    *,
    db_path: Path,
    raw_root: Path,
    client: S3ObjectClient,
    deadline_seconds: int = DEFAULT_DEADLINE_SECONDS,
    now: datetime | None = None,
) -> CheckpointUploadResult:
    """Back up SQLite, upload every verified raw triple, then write the manifest.

    Every referenced object is uploaded and byte-verified before the
    manifest itself is written, and the manifest is uploaded before this
    function returns -- but nothing here ever writes
    ``state/latest-success.json``. That is a distinct, explicit step
    (:func:`promote_latest_success`) gated on independent remote
    verification, never a side effect of a successful archive upload.
    """

    identity = _read_local_dataset_identity(db_path)
    if identity is None:
        raise CheckpointArchiveError("no locally active dataset to archive")
    dataset_id, input_manifest_hash = identity
    if not input_manifest_hash:
        raise CheckpointArchiveError(f"active dataset {dataset_id} has no recorded input manifest hash")
    source_dates = _read_source_dates(db_path)

    raw_store = LocalRawStore(raw_root)
    raw_keys = raw_store.list()

    deadline_at = monotonic() + deadline_seconds
    put_attempts = 0
    get_attempts = 0
    verified_bytes = 0

    raw_triples: list[RawTripleRef] = []
    for key in raw_keys:
        body_path = raw_root / key
        metadata_path = raw_root / f"{key}.metadata.json"
        commit_path = raw_root / f"{key}.commit.json"
        body = body_path.read_bytes()
        metadata = metadata_path.read_bytes()
        commit = commit_path.read_bytes()
        body_digest = sha256(body).hexdigest()

        for object_key, path, data in (
            (key, body_path, body),
            (f"{key}.metadata.json", metadata_path, metadata),
            (f"{key}.commit.json", commit_path, commit),
        ):
            digest = sha256(data).hexdigest()
            puts, gets = _ensure_object(
                client, key=object_key, local_path=path, sha256_hex=digest, size=len(data), deadline_at=deadline_at
            )
            put_attempts += puts
            get_attempts += gets
            verified_bytes += len(data)
        raw_triples.append(RawTripleRef(object_key=key, body_sha256=body_digest, body_bytes=len(body)))

    with tempfile.TemporaryDirectory() as tmp:
        backup_path = Path(tmp) / "market-backup.db"
        sqlite_ref = backup_sqlite_database(db_path, backup_path)
        puts, gets = _ensure_object(
            client,
            key=sqlite_ref.key,
            local_path=backup_path,
            sha256_hex=sqlite_ref.sha256,
            size=sqlite_ref.bytes,
            deadline_at=deadline_at,
        )
        put_attempts += puts
        get_attempts += gets
        verified_bytes += sqlite_ref.bytes

    manifest = CheckpointArchiveManifest(
        version=ARCHIVE_VERSION,
        dataset_id=dataset_id,
        input_manifest_hash=input_manifest_hash,
        source_dates=source_dates,
        created_at=(now or datetime.now(UTC)).astimezone(UTC).isoformat(),
        sqlite_backup=sqlite_ref,
        raw_triples=tuple(raw_triples),
    )

    with tempfile.TemporaryDirectory() as tmp:
        manifest_path = Path(tmp) / "manifest.json"
        manifest_bytes = manifest.canonical_bytes()
        manifest_path.write_bytes(manifest_bytes)
        digest = sha256(manifest_bytes).hexdigest()
        result = put_and_verify_object(
            client,
            key=manifest.manifest_key,
            local_path=manifest_path,
            sha256_hex=digest,
            size=len(manifest_bytes),
            deadline_at=deadline_at,
        )
        put_attempts += result.put_attempts
        get_attempts += result.get_attempts
        verified_bytes += len(manifest_bytes)

    return CheckpointUploadResult(
        manifest=manifest, put_attempts=put_attempts, get_attempts=get_attempts, verified_bytes=verified_bytes
    )


def _read_body(response: Mapping[str, Any]) -> bytes:
    body = response.get("Body")
    read = getattr(body, "read", None)
    if not callable(read):
        raise CheckpointArchiveError("malformed R2 response body")
    data = read()
    close = getattr(body, "close", None)
    if callable(close):
        close()
    if not isinstance(data, (bytes, bytearray)):
        raise CheckpointArchiveError("malformed R2 response body")
    return bytes(data)


def _fetch_bytes(client: S3ObjectClient, key: str) -> bytes:
    """Fetch one object's bytes.

    Contract with ``client.get_object``: it must raise :class:`KeyError`
    (and only :class:`KeyError`) for a genuinely absent key -- this is the
    one signal :func:`_load_pointer` treats as "no archive was ever
    promoted" (a legitimate bootstrap). Any other exception (a transport
    error, a permissions failure, a malformed response) is a hard failure
    and must never be mistaken for "the pointer does not exist yet".
    """

    try:
        response = client.get_object(Bucket=BUCKET, Key=key)
    except KeyError:
        raise
    except Exception as exc:  # noqa: BLE001 - any other transport failure is a hard failure, not "missing"
        raise CheckpointArchiveError(f"could not read {key}: {exc}") from exc
    return _read_body(response)


def _fetch_json(client: S3ObjectClient, key: str) -> Any:
    data = _fetch_bytes(client, key)
    try:
        return json.loads(data)
    except json.JSONDecodeError as exc:
        raise CheckpointArchiveError(f"{key} is not valid JSON") from exc


def _load_pointer(client: S3ObjectClient) -> dict[str, Any]:
    try:
        raw = _fetch_json(client, STATE_LATEST_SUCCESS_KEY)
    except KeyError as exc:
        raise NoCheckpointArchiveError("no checkpoint archive has ever been promoted") from exc
    if (
        not isinstance(raw, Mapping)
        or not isinstance(raw.get("dataset_id"), str)
        or not raw["dataset_id"]
        or not isinstance(raw.get("manifest_key"), str)
        or not raw["manifest_key"]
        or not isinstance(raw.get("manifest_sha256"), str)
        or not raw["manifest_sha256"]
    ):
        raise CheckpointArchiveError("state/latest-success.json is malformed")
    return dict(raw)


def _load_and_verify_manifest(client: S3ObjectClient, pointer: Mapping[str, Any]) -> CheckpointArchiveManifest:
    manifest_key = str(pointer["manifest_key"])
    raw_bytes = _fetch_bytes(client, manifest_key)
    # Hash the exact bytes fetched from the remote object -- not a
    # re-serialization of the parsed JSON -- so any byte-level corruption of
    # the stored manifest is caught even if it still happens to parse.
    if sha256(raw_bytes).hexdigest() != str(pointer["manifest_sha256"]).lower():
        raise CheckpointArchiveError("checkpoint archive manifest checksum does not match state/latest-success.json")
    try:
        raw = json.loads(raw_bytes)
    except json.JSONDecodeError as exc:
        raise CheckpointArchiveError(f"{manifest_key} is not valid JSON") from exc
    if not isinstance(raw, Mapping):
        raise CheckpointArchiveError("checkpoint archive manifest is malformed")
    manifest = CheckpointArchiveManifest.from_dict(raw)
    if manifest.dataset_id != str(pointer["dataset_id"]):
        raise CheckpointArchiveError("checkpoint archive manifest dataset ID does not match state/latest-success.json")
    return manifest


def load_latest_success(client: S3ObjectClient) -> CheckpointArchiveManifest:
    """Load and fully verify the archive behind ``state/latest-success.json``.

    Raises :class:`NoCheckpointArchiveError` only when no pointer has ever
    been written (a legitimate bootstrap). Any other problem -- a malformed
    pointer, a missing or corrupt manifest, or a manifest whose own checksum
    does not match the pointer -- raises the plain, non-bootstrap
    :class:`CheckpointArchiveError`: a caller must never treat those cases as
    "start from empty state".
    """

    pointer = _load_pointer(client)
    return _load_and_verify_manifest(client, pointer)


def promote_latest_success(
    client: S3ObjectClient,
    manifest: CheckpointArchiveManifest,
    *,
    remote_active_dataset_id: str,
    now: datetime | None = None,
) -> None:
    """Advance ``state/latest-success.json`` only after independent remote proof.

    ``remote_active_dataset_id`` must be read from production (e.g. the same
    ``SELECT dataset_id FROM active_dataset WHERE singleton = 1`` D1 query
    ``docs/operations/daily-run.md`` already documents verifying after D1
    import) -- never derived from this process's own belief that publication
    succeeded. A mismatch means the manifest describes a candidate that was
    never actually confirmed live, and is rejected rather than promoted.

    Calling this twice for the *same* dataset ID (an exact retry, or a
    recoverable commit-marker mismatch reconciled after D1 already
    succeeded) is idempotent: the pointer is simply rewritten to the same
    values, never treated as a conflicting second publication.
    """

    if remote_active_dataset_id != manifest.dataset_id:
        raise CheckpointArchiveError(
            "refusing to promote: remote active dataset "
            f"({remote_active_dataset_id!r}) does not match this archive's dataset ({manifest.dataset_id!r})"
        )
    pointer = {
        "dataset_id": manifest.dataset_id,
        "manifest_key": manifest.manifest_key,
        "manifest_sha256": manifest.manifest_sha256,
        "promoted_at": (now or datetime.now(UTC)).astimezone(UTC).isoformat(),
    }
    body = json.dumps(pointer, sort_keys=True, separators=(",", ":")).encode("utf-8")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "latest-success.json"
        path.write_bytes(body)
        digest = sha256(body).hexdigest()
        deadline_at = monotonic() + DEFAULT_DEADLINE_SECONDS
        put_and_verify_object(
            client,
            key=STATE_LATEST_SUCCESS_KEY,
            local_path=path,
            sha256_hex=digest,
            size=len(body),
            deadline_at=deadline_at,
        )


def restore_checkpoint_archive(
    client: S3ObjectClient,
    *,
    db_path: Path,
    raw_root: Path,
    deadline_seconds: int = DEFAULT_DEADLINE_SECONDS,
) -> CheckpointRestoreResult:
    """Restore the last verified success archive onto a clean/cache-missed runner.

    Every object is downloaded and byte-verified in memory before anything
    is written to disk. The SQLite backup is restored into a scratch file
    (sanity-opened with SQLite) and atomically renamed into place before any
    raw triple is even requested. Every raw triple is then downloaded and
    written into a *staging* directory next to ``raw_root`` -- never
    ``raw_root`` itself -- and only after every single triple in the
    manifest has been downloaded, verified, and staged does this function
    atomically replace ``raw_root`` with the fully staged directory in one
    rename. A missing, malformed, or checksum-mismatched object anywhere --
    including a failure on the very last triple -- aborts the whole restore
    with :class:`CheckpointArchiveError` and leaves ``raw_root`` completely
    untouched (whatever was there before, or nothing at all); it can never
    observe a partially populated ``raw_root``. Callers must never fall back
    to initializing a blank ``market.db``/``raw/`` on that path; the only
    case that legitimately means "start blank" is
    :class:`NoCheckpointArchiveError` (no archive was ever promoted).
    """

    manifest = load_latest_success(client)
    get_attempts = 0
    verified_bytes = 0

    raw_root.parent.mkdir(parents=True, exist_ok=True)
    staging_root = Path(tempfile.mkdtemp(dir=raw_root.parent, prefix=f".{raw_root.name}.restoring-"))
    try:
        try:
            backup_bytes = get_and_verify_object(
                client,
                key=manifest.sqlite_backup.key,
                sha256_hex=manifest.sqlite_backup.sha256,
                size=manifest.sqlite_backup.bytes,
            )
            get_attempts += 1
            verified_bytes += len(backup_bytes)
            # Verify (in memory, in a scratch location) fully before
            # touching db_path at all: a corrupt/interrupted archive must
            # never leave a half-restored or invalid database at the real
            # path. The scratch file lives next to db_path (not the system
            # temp dir) so the final replace is an atomic same-filesystem
            # rename, not a cross-device copy that could itself be
            # interrupted.
            db_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_db = db_path.parent / f"{db_path.name}.restoring-{sha256(backup_bytes[:64]).hexdigest()[:8]}"
            try:
                tmp_db.write_bytes(backup_bytes)
                probe = sqlite3.connect(tmp_db)
                try:
                    probe.execute("PRAGMA schema_version").fetchone()
                except sqlite3.DatabaseError as exc:
                    raise CheckpointArchiveError("restored SQLite backup is not a valid database") from exc
                finally:
                    probe.close()
                tmp_db.replace(db_path)
            finally:
                tmp_db.unlink(missing_ok=True)

            # Every raw triple is downloaded, verified, and written into
            # `staging_root` -- a directory nothing else reads from -- so a
            # failure on any triple (including the last one) never leaves a
            # partial `raw_root` on disk for a later run to pick up (e.g.
            # via `actions/cache/save`'s `if: always()`) and silently trust.
            for triple in manifest.raw_triples:
                body = get_and_verify_object(
                    client, key=triple.object_key, sha256_hex=triple.body_sha256, size=triple.body_bytes
                )
                get_attempts += 1
                verified_bytes += len(body)
                # The manifest records only the body's checksum/size;
                # metadata and commit-marker bytes are cross-checked below
                # by LocalRawStore.get() itself, reusing raw_store.py's
                # existing triple-integrity rule rather than a second
                # checksum scheme.
                metadata_key = triple.metadata_key
                commit_key = triple.commit_key
                metadata = _read_body(client.get_object(Bucket=BUCKET, Key=metadata_key))
                commit = _read_body(client.get_object(Bucket=BUCKET, Key=commit_key))
                get_attempts += 2
                verified_bytes += len(metadata) + len(commit)

                body_path = staging_root / triple.object_key
                metadata_path = staging_root / metadata_key
                commit_path = staging_root / commit_key
                body_path.parent.mkdir(parents=True, exist_ok=True)
                body_path.write_bytes(body)
                metadata_path.write_bytes(metadata)
                commit_path.write_bytes(commit)
                try:
                    restored = LocalRawStore(staging_root).get(triple.object_key)
                except (KeyError, ImmutableRawStoreError) as exc:
                    raise CheckpointArchiveError(f"restored raw triple failed integrity check: {triple.object_key}") from exc
                if restored != body:
                    raise CheckpointArchiveError(f"restored raw triple body mismatch: {triple.object_key}")
        except CheckpointArchiveError:
            raise
        except Exception as exc:  # noqa: BLE001 - any transport/decode failure here must fail the restore, never continue
            raise CheckpointArchiveError(f"checkpoint archive restore failed: {exc}") from exc

        # Every triple verified: atomically swap the fully staged directory
        # into place. `os.replace`/`Path.replace` on a directory is a single
        # rename syscall on the same filesystem (guaranteed here since
        # `staging_root` was created as a sibling of `raw_root`), so this
        # is the one moment `raw_root` changes, and it changes completely.
        if raw_root.exists():
            shutil.rmtree(raw_root)
        staging_root.replace(raw_root)
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)

    return CheckpointRestoreResult(manifest=manifest, get_attempts=get_attempts, verified_bytes=verified_bytes)


def _raw_store_completeness(raw_root: Path, manifest: CheckpointArchiveManifest) -> tuple[bool, int]:
    """Cheaply check that every manifest-listed raw triple's three files exist.

    This is deliberately a existence check (``Path.exists()``), not a full
    re-read/re-hash of every raw artifact: :func:`build_and_upload_checkpoint_archive`
    already pays that cost once per archive, and re-paying it on every
    cache-hit run would defeat the point of a cheap identity check. A
    missing file is exactly the symptom a partial/interrupted local
    ``raw/`` (the Critical finding this function was added to catch) would
    leave behind, so presence alone is a strong, cheap signal; the byte-level
    integrity of a *present* triple is still authoritatively re-checked the
    moment it is actually read for publication (``LocalRawStore.get``).
    """

    missing = 0
    for triple in manifest.raw_triples:
        body_path = raw_root / triple.object_key
        metadata_path = raw_root / triple.metadata_key
        commit_path = raw_root / triple.commit_key
        if not (body_path.is_file() and metadata_path.is_file() and commit_path.is_file()):
            missing += 1
    return missing == 0, missing


def check_local_identity_against_manifest(
    client: S3ObjectClient, *, db_path: Path, raw_root: Path | None = None
) -> IdentityCheckResult:
    """Validate a cache-hit runner's local state against the authoritative manifest.

    A GitHub Actions cache *hit* means local files are present, but says
    nothing about whether they are the checkpoint this publisher actually
    last archived (a stale or previously-corrupted cache entry can still be
    "hit"). This performs only the small manifest fetch and, when
    ``raw_root`` is given, a cheap local existence check per raw triple --
    never the bulk raw/sqlite download restore does -- and reports whether
    local identity (and, if checked, raw-store completeness) matches; it
    does not repair a mismatch itself.

    ``raw_root`` should always be passed by a caller deciding whether to
    trust a cache hit: dataset-ID/input-hash agreement alone cannot detect
    a partially restored or partially evicted ``raw/`` directory (the F12
    Critical finding this parameter closes) -- only checking that every
    archived raw triple is actually present locally can.
    """

    manifest = load_latest_success(client)
    local = _read_local_dataset_identity(db_path)
    local_dataset_id = local[0] if local else None
    local_input_manifest_hash = local[1] if local else None
    identity_matches = (
        local is not None and local_dataset_id == manifest.dataset_id and local_input_manifest_hash == manifest.input_manifest_hash
    )

    raw_store_complete: bool | None = None
    raw_triples_missing = 0
    if raw_root is not None:
        raw_store_complete, raw_triples_missing = _raw_store_completeness(raw_root, manifest)

    matches = identity_matches and (raw_store_complete is not False)
    return IdentityCheckResult(
        matches=matches,
        local_dataset_id=local_dataset_id,
        remote_dataset_id=manifest.dataset_id,
        local_input_manifest_hash=local_input_manifest_hash,
        remote_input_manifest_hash=manifest.input_manifest_hash,
        raw_store_complete=raw_store_complete,
        raw_triples_expected=len(manifest.raw_triples),
        raw_triples_missing=raw_triples_missing,
    )


def checkpoint_archive_ledger_entry(
    result: CheckpointUploadResult | CheckpointRestoreResult, *, now: datetime | None = None
) -> ReservationLedgerEntry:
    """Fold one archive operation's attempt counts into R09's reservation ledger shape.

    Mirrors how the daily workflow records ``r2_sync``'s attempted put/get
    counts (see ``docs/reviews/2026-09-09-remediation-progress.md`` row R09):
    archive operations spend the same account-wide R2 budget and must be
    visible to :func:`market_pipeline.publication.preflight.assert_plan_within_remote_budget`
    the same way.
    """

    put_attempts = getattr(result, "put_attempts", 0)
    get_attempts = result.get_attempts
    return ReservationLedgerEntry(
        recorded_at=(now or datetime.now(UTC)).astimezone(UTC),
        d1_mutations=0,
        r2_class_a_operations=put_attempts,
        r2_class_b_operations=get_attempts,
        r2_bytes=result.verified_bytes,
    )


def main(argv: Sequence[str] | None = None) -> int:
    import argparse
    import os

    from market_pipeline.publication.r2_sync import DEFAULT_MAX_WORKERS, build_s3_client
    from market_pipeline.publication.remote_usage import append_reservation_ledger_entry

    parser = argparse.ArgumentParser(description="durable checkpoint-archive backup/restore for a clean/cache-missed runner (F12)")
    subparsers = parser.add_subparsers(dest="action", required=True)

    backup = subparsers.add_parser("backup", help="back up the local checkpoint/publication state")
    backup.add_argument("--db", required=True)
    backup.add_argument("--raw-root", required=True)
    backup.add_argument("--deadline-seconds", type=int, default=DEFAULT_DEADLINE_SECONDS)
    backup.add_argument("--promote", action="store_true", help="also advance state/latest-success.json")
    backup.add_argument("--remote-active-dataset-id", help="required with --promote: the independently D1-verified active dataset ID")
    backup.add_argument("--reservation-ledger", help="append this run's put/get counts to R09's reservation ledger")
    backup.add_argument("--result", help="write the upload result JSON here")

    restore = subparsers.add_parser("restore", help="restore checkpoint state from the last verified success")
    restore.add_argument("--db", required=True)
    restore.add_argument("--raw-root", required=True)
    restore.add_argument("--deadline-seconds", type=int, default=DEFAULT_DEADLINE_SECONDS)
    restore.add_argument("--reservation-ledger", help="append this run's get counts to R09's reservation ledger")
    restore.add_argument("--result", help="write the restore result JSON here")
    restore.add_argument(
        "--check-identity-only",
        action="store_true",
        help=(
            "a cache hit already has local files; only validate their identity "
            "against the authoritative remote manifest instead of restoring"
        ),
    )

    for sub in (backup, restore):
        sub.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS)

    args = parser.parse_args(argv)

    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
    access_key = os.environ.get("R2_ACCESS_KEY_ID")
    secret_key = os.environ.get("R2_SECRET_ACCESS_KEY")
    if not account_id or not access_key or not secret_key:
        parser.error("R2 publication credentials are unavailable")
    client = build_s3_client(account_id, access_key, secret_key, args.max_workers)

    try:
        if args.action == "backup":
            if args.promote and not args.remote_active_dataset_id:
                raise CheckpointArchiveError("--remote-active-dataset-id is required with --promote")
            result = build_and_upload_checkpoint_archive(
                db_path=Path(args.db), raw_root=Path(args.raw_root), client=client, deadline_seconds=args.deadline_seconds
            )
            if args.promote:
                promote_latest_success(client, result.manifest, remote_active_dataset_id=args.remote_active_dataset_id)
            if args.reservation_ledger:
                append_reservation_ledger_entry(
                    Path(args.reservation_ledger), checkpoint_archive_ledger_entry(result), now=datetime.now(UTC)
                )
            payload = {
                "dataset_id": result.manifest.dataset_id,
                "put_attempts": result.put_attempts,
                "get_attempts": result.get_attempts,
                "verified_bytes": result.verified_bytes,
                "promoted": bool(args.promote),
            }
        elif args.check_identity_only:
            try:
                identity = check_local_identity_against_manifest(
                    client, db_path=Path(args.db), raw_root=Path(args.raw_root)
                )
            except NoCheckpointArchiveError:
                payload = {"matches": None, "reason": "no_prior_success"}
            else:
                payload = {
                    "matches": identity.matches,
                    "local_dataset_id": identity.local_dataset_id,
                    "remote_dataset_id": identity.remote_dataset_id,
                    "raw_store_complete": identity.raw_store_complete,
                    "raw_triples_expected": identity.raw_triples_expected,
                    "raw_triples_missing": identity.raw_triples_missing,
                }
        else:
            try:
                restored = restore_checkpoint_archive(
                    client, db_path=Path(args.db), raw_root=Path(args.raw_root), deadline_seconds=args.deadline_seconds
                )
            except NoCheckpointArchiveError:
                payload = {"restored": False, "reason": "no_prior_success"}
            else:
                if args.reservation_ledger:
                    append_reservation_ledger_entry(
                        Path(args.reservation_ledger), checkpoint_archive_ledger_entry(restored), now=datetime.now(UTC)
                    )
                payload = {
                    "restored": True,
                    "dataset_id": restored.manifest.dataset_id,
                    "get_attempts": restored.get_attempts,
                    "verified_bytes": restored.verified_bytes,
                }
    except CheckpointArchiveError as exc:
        parser.error(str(exc))
        return 2  # pragma: no cover - argparse.error already raises SystemExit

    text = json.dumps(payload, sort_keys=True) + "\n"
    if args.result:
        Path(args.result).write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ARCHIVE_VERSION",
    "CHECKPOINT_PREFIX",
    "STATE_LATEST_SUCCESS_KEY",
    "CheckpointArchiveError",
    "CheckpointArchiveManifest",
    "CheckpointRestoreResult",
    "CheckpointUploadResult",
    "IdentityCheckResult",
    "NoCheckpointArchiveError",
    "ObjectRef",
    "RawTripleRef",
    "backup_sqlite_database",
    "build_and_upload_checkpoint_archive",
    "check_local_identity_against_manifest",
    "checkpoint_archive_ledger_entry",
    "load_latest_success",
    "main",
    "promote_latest_success",
    "restore_checkpoint_archive",
]
