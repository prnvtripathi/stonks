"""Bounded, checksum-verified R2 publication through its S3-compatible API.

This module is the single transport that moves a candidate's publication
bundle (see :mod:`market_pipeline.publication.bundle`) into the private R2
bucket and byte-verifies every object it touches. It replaces an earlier,
per-object CLI-invocation loop in ``.github/workflows/daily-data.yml`` that
relied on Bash process substitution as its safety boundary (findings F09,
F10): that pattern let a failing embedded producer keep the outer ``while``
loop's exit code at zero even under ``set -euo pipefail``, so a corrupted or
partial R2 publish could still fall through into the subsequent D1 import
step. This module's CLI has exactly one exit code, produced synchronously by
this process, with no subshell or process substitution in between.

Design constraints this module holds itself to:

* The entire bundle -- every object's key shape and every local file's size
  and checksum -- is validated synchronously, in full, before the first
  network mutation. A malformed manifest entry anywhere (including the last
  one) or a local file that does not match its recorded checksum aborts
  before any object is touched.
* In-flight work is bounded to ``max_workers`` at all times: objects are
  submitted in a sliding window, never all at once, so an early failure has
  a small, bounded amount of already-started work to wait for rather than an
  unbounded queue to unwind.
* A single deadline is shared by every object attempt. On any failure
  (including a deadline overrun) no further objects are submitted, queued
  work is cancelled, and the function blocks until every already-started
  worker has actually finished before it raises or returns -- so a caller
  can rely on "the process exited" meaning "no R2 request from this run is
  still in flight."
* A PUT that raises can have succeeded anyway (a timed-out response is an
  unknown outcome, not a known failure). A retry therefore always verifies
  via GET first before assuming the previous PUT failed and re-uploading --
  this both closes that correctness gap and lets an already-correct,
  content-addressed object be reused instead of re-sent.
* Every attempted request -- including ones spent on retries or on
  reuse-verification -- is counted, because R09's remote-operation budget
  needs the true attempted count, not just the successful one.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path, PurePosixPath
from time import monotonic
from typing import Any, Mapping, Protocol, Sequence, cast

from market_pipeline.publication.bundle import BundleError, BundleObject, PublicationBundle
from market_pipeline.storage.history_store import HistoryStoreError, chart_key, history_key

__all__ = [
    "BUCKET",
    "DEFAULT_DEADLINE_SECONDS",
    "DEFAULT_MAX_WORKERS",
    "MAX_ATTEMPTS_PER_OBJECT",
    "MAX_DEADLINE_SECONDS",
    "MAX_WORKERS",
    "NoSuchKeyTranslatingClient",
    "ObjectTransferResult",
    "R2SyncError",
    "R2SyncResult",
    "S3ObjectClient",
    "build_s3_client",
    "get_and_verify_object",
    "main",
    "put_and_verify_object",
    "synchronize_history",
]

BUCKET = "stonks-private-history"

#: Concurrency is configurable up to this many in-flight requests.
MAX_WORKERS = 32
DEFAULT_MAX_WORKERS = 16

#: Deadline is configurable up to this many seconds (24h); the default (4h)
#: is what the daily workflow actually budgets for one run.
MAX_DEADLINE_SECONDS = 24 * 60 * 60
DEFAULT_DEADLINE_SECONDS = 4 * 60 * 60

#: Bounded retries per object. A retry always re-verifies via GET first (see
#: module docstring), so this bounds total attempts, not just PUT attempts.
MAX_ATTEMPTS_PER_OBJECT = 3


class R2SyncError(RuntimeError):
    """Raised before D1 import when an R2 publication object is unsafe or unverified."""


class S3ObjectClient(Protocol):
    def put_object(self, *, Bucket: str, Key: str, Body: Any) -> Any: ...

    def get_object(self, *, Bucket: str, Key: str) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class _Object:
    key: str
    local: Path
    sha256_hex: str
    size: int
    kind: str


@dataclass(frozen=True)
class _Counts:
    puts: int
    gets: int


@dataclass(frozen=True)
class R2SyncResult:
    """The outcome of one :func:`synchronize_history` call.

    ``put_attempts``/``get_attempts`` count every attempted request,
    including ones spent on retries and reuse-verification -- this is the
    input R09's remote-operation budget needs, not just the successful path.
    """

    verified_objects: int
    verified_bytes: int
    put_attempts: int
    get_attempts: int

    @property
    def total_attempts(self) -> int:
        return self.put_attempts + self.get_attempts


def _invalid(key: str) -> R2SyncError:
    return R2SyncError(f"invalid object key: {key}")


def _canonical_key(entry: BundleObject) -> str:
    """Recompute and confirm an object's key is exactly what its own fields imply.

    This closes a spoofing gap the flat ``{key, sha256, bytes, kind}`` shape
    otherwise leaves open: nothing in the bundle format itself would stop a
    corrupted manifest from pairing a valid-looking key with an unrelated
    hash. History keys are content-addressed (the key's own trailing segment
    is the object's SHA-256), so the key and ``sha256`` field are re-derived
    and cross-checked against each other here, not merely pattern-matched.
    """

    key = entry.key
    if not isinstance(key, str) or not key or any(char in key for char in "\x00\n\r"):
        raise _invalid(str(key))
    path = PurePosixPath(key)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != key:
        raise _invalid(key)
    parts = path.parts
    try:
        if (
            entry.kind == "history"
            and len(parts) == 5
            and parts[0] == "history"
            and re.fullmatch(r"\d{4}", parts[3])
            and parts[4].endswith(".parquet")
        ):
            digest = parts[4][: -len(".parquet")]
            if digest.lower() != entry.sha256.lower():
                raise _invalid(key)
            canonical = history_key(parts[1], parts[2], int(parts[3]), digest)
            if canonical == key:
                return key
        elif (
            entry.kind == "chart"
            and len(parts) == 3
            and parts[0] == "charts"
            and parts[2].endswith(".json.gz")
        ):
            canonical = chart_key(parts[1], parts[2][: -len(".json.gz")])
            if canonical == key:
                return key
    except HistoryStoreError:
        pass
    raise _invalid(key)


def _prepare_objects(bundle: PublicationBundle, history_root: Path) -> list[_Object]:
    """Validate the whole bundle and every local checksum before any mutation."""

    if not isinstance(bundle, PublicationBundle):
        raise R2SyncError("invalid publication bundle")
    if not bundle.objects:
        raise R2SyncError("publication bundle has no objects")
    root = history_root.resolve()
    if not root.is_dir():
        raise R2SyncError("history root is missing")
    items: list[_Object] = []
    seen: set[str] = set()
    for entry in bundle.objects:
        if entry.kind not in ("history", "chart"):
            raise R2SyncError(f"unsupported object kind: {entry.kind}")
        key = _canonical_key(entry)
        if key in seen:
            raise R2SyncError(f"duplicate object key: {key}")
        seen.add(key)
        local = (root / key).resolve()
        if not local.is_relative_to(root) or not local.is_file():
            raise R2SyncError(f"bundle object is missing locally: {key}")
        data = local.read_bytes()
        if len(data) != entry.bytes:
            raise R2SyncError(f"local object does not match its recorded size: {key}")
        if sha256(data).hexdigest() != entry.sha256.lower():
            raise R2SyncError(f"local object does not match its recorded checksum: {key}")
        items.append(_Object(key=key, local=local, sha256_hex=entry.sha256.lower(), size=entry.bytes, kind=entry.kind))
    return items


def _verify_remote(client: S3ObjectClient, item: _Object) -> bool:
    """Return True only if the remote object exists and byte-matches ``item``."""

    response = client.get_object(Bucket=BUCKET, Key=item.key)
    body = response.get("Body")
    read = getattr(body, "read", None)
    if not callable(read):
        raise R2SyncError(f"R2 object response has no readable body: {item.key}")
    try:
        data = read()
    finally:
        close = getattr(body, "close", None)
        if callable(close):
            close()
    if not isinstance(data, (bytes, bytearray)):
        raise R2SyncError(f"R2 object response body is not bytes: {item.key}")
    return len(data) == item.size and sha256(data).hexdigest() == item.sha256_hex


def _sync_one(client: S3ObjectClient, item: _Object, deadline_at: float, max_attempts: int = MAX_ATTEMPTS_PER_OBJECT) -> _Counts:
    puts = 0
    gets = 0
    last_exc: BaseException | None = None
    verify_first = False  # True once a PUT's outcome is unknown (it raised)
    for _ in range(max_attempts):
        if monotonic() >= deadline_at:
            raise R2SyncError(f"R2 synchronization deadline exceeded for {item.key}")
        try:
            if verify_first:
                gets += 1
                if _verify_remote(client, item):
                    return _Counts(puts=puts, gets=gets)
            puts += 1
            with item.local.open("rb") as body:
                client.put_object(Bucket=BUCKET, Key=item.key, Body=body)
            gets += 1
            if _verify_remote(client, item):
                return _Counts(puts=puts, gets=gets)
            last_exc = R2SyncError(f"R2 object verification failed for {item.key}")
            verify_first = False
        except Exception as exc:  # noqa: BLE001 - any transport failure is retried, bounded above
            last_exc = exc
            verify_first = True
    raise R2SyncError(f"R2 object synchronization failed after {max_attempts} attempts: {item.key}") from last_exc


def synchronize_history(
    bundle: PublicationBundle,
    history_root: Path,
    client: S3ObjectClient,
    *,
    max_workers: int = DEFAULT_MAX_WORKERS,
    deadline_seconds: int = DEFAULT_DEADLINE_SECONDS,
) -> R2SyncResult:
    """Synchronize and byte-verify a candidate's bundle objects against R2.

    Validates the entire bundle and every local checksum synchronously
    before the first mutation, then transfers with bounded, deadline-shared
    concurrency. On any failure -- validation, transport, or deadline -- no
    further objects are submitted, queued work is cancelled, and this
    function blocks until in-flight work has actually finished before
    raising: a caller never observes this function return while an R2
    request from this run is still outstanding.
    """

    if not isinstance(max_workers, int) or isinstance(max_workers, bool) or not 1 <= max_workers <= MAX_WORKERS:
        raise R2SyncError(f"worker limit is invalid: must be an integer from 1 to {MAX_WORKERS}")
    if not isinstance(deadline_seconds, int) or isinstance(deadline_seconds, bool) or not 1 <= deadline_seconds <= MAX_DEADLINE_SECONDS:
        raise R2SyncError(f"deadline is invalid: must be an integer from 1 to {MAX_DEADLINE_SECONDS} seconds")

    items = _prepare_objects(bundle, history_root)
    deadline_at = monotonic() + deadline_seconds

    pending: deque[_Object] = deque(items)
    in_flight: dict[Future[_Counts], _Object] = {}
    put_attempts = 0
    get_attempts = 0
    verified_objects = 0
    verified_bytes = 0

    executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="r2-sync")
    try:
        while pending or in_flight:
            if monotonic() >= deadline_at:
                raise R2SyncError("R2 synchronization deadline exceeded")
            while pending and len(in_flight) < max_workers:
                item = pending.popleft()
                in_flight[executor.submit(_sync_one, client, item, deadline_at)] = item
            remaining = max(0.0, deadline_at - monotonic())
            done, _ = wait(in_flight.keys(), timeout=min(1.0, remaining) or 0.001, return_when=FIRST_COMPLETED)
            for future in done:
                item = in_flight.pop(future)
                counts = future.result()
                put_attempts += counts.puts
                get_attempts += counts.gets
                verified_objects += 1
                verified_bytes += item.size
        return R2SyncResult(
            verified_objects=verified_objects,
            verified_bytes=verified_bytes,
            put_attempts=put_attempts,
            get_attempts=get_attempts,
        )
    except BaseException:
        pending.clear()
        for future in list(in_flight):
            future.cancel()
        if in_flight:
            wait(in_flight.keys())
        for future in list(in_flight):
            if future.cancelled():
                continue
            try:
                counts = future.result()
            except Exception:  # noqa: BLE001 - failure already propagating; just fold in attempted counts
                continue
            put_attempts += counts.puts
            get_attempts += counts.gets
        raise
    finally:
        executor.shutdown(wait=True, cancel_futures=True)


@dataclass(frozen=True)
class ObjectTransferResult:
    """Attempt counts for one object moved through :func:`put_and_verify_object`."""

    put_attempts: int
    get_attempts: int


def put_and_verify_object(
    client: S3ObjectClient,
    *,
    key: str,
    local_path: Path,
    sha256_hex: str,
    size: int,
    deadline_at: float,
    max_attempts: int = MAX_ATTEMPTS_PER_OBJECT,
) -> ObjectTransferResult:
    """Bounded PUT-then-verify for exactly one object, reusable outside a bundle sync.

    This is the same retry/verify primitive :func:`synchronize_history` uses
    per object (:func:`_sync_one`), exposed directly so other publication
    transports (e.g. the checkpoint archive in
    :mod:`market_pipeline.publication.checkpoint_archive`) share this one
    PUT/GET-verify/retry implementation instead of a second one.
    """

    item = _Object(key=key, local=local_path, sha256_hex=sha256_hex.lower(), size=size, kind="generic")
    counts = _sync_one(client, item, deadline_at, max_attempts)
    return ObjectTransferResult(put_attempts=counts.puts, get_attempts=counts.gets)


def get_and_verify_object(client: S3ObjectClient, *, key: str, sha256_hex: str, size: int) -> bytes:
    """GET exactly one object and verify its bytes match the recorded checksum/size.

    Shares :func:`_verify_remote`'s body-reading logic so there is one place
    that knows how to safely drain and byte-verify an S3-compatible response.
    """

    response = client.get_object(Bucket=BUCKET, Key=key)
    body = response.get("Body")
    read = getattr(body, "read", None)
    if not callable(read):
        raise R2SyncError(f"R2 object response has no readable body: {key}")
    try:
        data = read()
    finally:
        close = getattr(body, "close", None)
        if callable(close):
            close()
    if not isinstance(data, (bytes, bytearray)):
        raise R2SyncError(f"R2 object response body is not bytes: {key}")
    if len(data) != size or sha256(data).hexdigest() != sha256_hex.lower():
        raise R2SyncError(f"R2 object verification failed: {key}")
    return bytes(data)


def build_s3_client(account_id: str, access_key: str, secret_key: str, max_workers: int) -> S3ObjectClient:
    """Public constructor for the one S3-compatible client this publisher uses.

    Exposed so other publication transports (e.g. the checkpoint archive in
    :mod:`market_pipeline.publication.checkpoint_archive`) build their client
    the same way ``r2_sync``'s own CLI does, instead of adding a second
    boto3 client construction path.
    """

    return _client(account_id, access_key, secret_key, max_workers)


class NoSuchKeyTranslatingClient:
    """Wrap a raw S3-compatible client so a missing-key GET raises ``KeyError``.

    Real R2/boto3 raises ``botocore.exceptions.ClientError`` (error code
    ``NoSuchKey``, or an HTTP 404) for a GET against an absent key --
    never ``KeyError``. :mod:`market_pipeline.publication.checkpoint_archive`
    depends on exactly ``KeyError`` to distinguish "no archive has ever been
    promoted yet" (a legitimate bootstrap) from every other failure (a
    corrupt or unreachable archive, which must hard-fail rather than be
    mistaken for day one). Every client this module constructs is wrapped
    with this translation so that contract holds against the real
    Cloudflare R2 endpoint, not only against tests' fake clients.

    ``put_object`` and every other error from ``get_object`` pass through
    unchanged.
    """

    def __init__(self, client: S3ObjectClient) -> None:
        self._client = client

    def put_object(self, *, Bucket: str, Key: str, Body: Any) -> Any:
        return self._client.put_object(Bucket=Bucket, Key=Key, Body=Body)

    def get_object(self, *, Bucket: str, Key: str) -> Mapping[str, Any]:
        from botocore.exceptions import ClientError  # type: ignore[import-untyped]

        try:
            return self._client.get_object(Bucket=Bucket, Key=Key)
        except ClientError as exc:
            error = exc.response.get("Error", {}) if isinstance(getattr(exc, "response", None), Mapping) else {}
            code = str(error.get("Code", ""))
            status = str(
                error.get("HTTPStatusCode")
                or (exc.response.get("ResponseMetadata", {}) if isinstance(getattr(exc, "response", None), Mapping) else {}).get("HTTPStatusCode", "")
            )
            if code == "NoSuchKey" or status == "404":
                raise KeyError(Key) from exc
            raise


def _client(account_id: str, access_key: str, secret_key: str, max_workers: int) -> S3ObjectClient:
    import boto3  # type: ignore[import-untyped]
    from botocore.client import BaseClient  # type: ignore[import-untyped]
    from botocore.config import Config  # type: ignore[import-untyped]

    client: BaseClient = boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto",
        config=Config(
            connect_timeout=15,
            read_timeout=60,
            max_pool_connections=max_workers,
            retries={"max_attempts": 1, "mode": "standard"},
        ),
    )
    return NoSuchKeyTranslatingClient(cast(S3ObjectClient, client))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="sync and byte-verify a publication bundle's objects against R2")
    parser.add_argument("--bundle", required=True, help="publication bundle JSON (see publication/bundle.py)")
    parser.add_argument("--history-root", required=True)
    parser.add_argument("--max-workers", type=int, default=DEFAULT_MAX_WORKERS)
    parser.add_argument("--deadline-seconds", type=int, default=DEFAULT_DEADLINE_SECONDS)
    args = parser.parse_args(argv)
    try:
        raw = json.loads(Path(args.bundle).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise R2SyncError("invalid publication bundle input")
        bundle = PublicationBundle.from_dict(raw)
        account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID")
        access_key = os.environ.get("R2_ACCESS_KEY_ID")
        secret_key = os.environ.get("R2_SECRET_ACCESS_KEY")
        if not account_id or not access_key or not secret_key:
            raise R2SyncError("R2 publication credentials are unavailable")
        result = synchronize_history(
            bundle,
            Path(args.history_root),
            _client(account_id, access_key, secret_key, args.max_workers),
            max_workers=args.max_workers,
            deadline_seconds=args.deadline_seconds,
        )
    except (OSError, json.JSONDecodeError, BundleError, R2SyncError) as exc:
        parser.error(str(exc))
    print(
        f"R2 sync verified {result.verified_objects} objects "
        f"({result.verified_bytes} bytes) with {result.total_attempts} attempted requests "
        f"({result.put_attempts} put, {result.get_attempts} get)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
