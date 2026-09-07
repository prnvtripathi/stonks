"""Immutable local and S3-compatible raw-artifact stores."""

from __future__ import annotations

import json
import os
import re
from hashlib import sha256
from pathlib import Path
from typing import Protocol

from market_pipeline.domain.models import SourceArtifact
from market_pipeline.sources.base import RawStore
from market_pipeline.sources.registry import assert_artifact_policy

__all__ = ["ImmutableRawStoreError", "LocalRawStore", "ObjectClient", "R2RawStore", "RawStore"]


class ImmutableRawStoreError(RuntimeError):
    """Raised when an immutable object would be overwritten or fails integrity checks."""


class ObjectClient(Protocol):
    def put_if_absent(self, key: str, body: bytes) -> bool: ...

    def get(self, key: str) -> bytes | None: ...


def _object_key(artifact: SourceArtifact) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", artifact.source_id):
        raise ImmutableRawStoreError("artifact source ID contains unsupported characters")
    filename = Path(artifact.filename).name
    if not filename or filename in {".", ".."} or filename != artifact.filename:
        raise ImmutableRawStoreError("artifact filename must be a simple filename")
    if not re.fullmatch(r"[a-zA-Z0-9._-]+", filename):
        raise ImmutableRawStoreError("artifact filename contains unsupported characters")
    return (
        f"raw/{artifact.source_id}/{artifact.effective_date.isoformat()}"
        f"/{artifact.checksum.lower()}/{filename}"
    )


def _metadata_key(key: str) -> str:
    return f"{key}.metadata.json"


def _metadata_body(artifact: SourceArtifact) -> bytes:
    return json.dumps(artifact.model_dump(mode="json"), sort_keys=True, indent=2).encode("utf-8") + b"\n"


def _check_body(artifact: SourceArtifact, body: bytes) -> None:
    actual = sha256(body).hexdigest()
    if actual != artifact.checksum.lower():
        raise ImmutableRawStoreError(
            f"artifact checksum mismatch: expected {artifact.checksum.lower()}, got {actual}"
        )


class LocalRawStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def put(self, artifact: SourceArtifact, body: bytes) -> str:
        assert_artifact_policy(
            source_id=artifact.source_id,
            source_url=artifact.source_url,
            terms_url=artifact.terms_url,
        )
        _check_body(artifact, body)
        key = _object_key(artifact)
        path = self.root / key
        metadata_path = self.root / _metadata_key(key)
        # Preflight both immutable objects before writing either one. This makes a
        # metadata conflict failure-safe even when the body does not yet exist.
        if path.exists():
            if path.read_bytes() != body:
                raise ImmutableRawStoreError(f"immutable object already contains different bytes: {key}")
        metadata = _metadata_body(artifact)
        if metadata_path.exists() and metadata_path.read_bytes() != metadata:
            raise ImmutableRawStoreError(f"immutable metadata already differs: {metadata_path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            temporary.write_bytes(body)
            temporary.replace(path)
        if not metadata_path.exists():
            metadata_path.parent.mkdir(parents=True, exist_ok=True)
            metadata_path.write_bytes(metadata)
        return key

    def get(self, object_key: str) -> bytes:
        root = self.root.resolve()
        path = (self.root / object_key).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ImmutableRawStoreError("object key escapes store root") from exc
        try:
            return path.read_bytes()
        except FileNotFoundError as exc:
            raise KeyError(object_key) from exc


class R2RawStore:
    """Raw store backed by an injected S3-compatible object client."""

    def __init__(self, client: ObjectClient) -> None:
        self.client = client

    def put(self, artifact: SourceArtifact, body: bytes) -> str:
        assert_artifact_policy(
            source_id=artifact.source_id,
            source_url=artifact.source_url,
            terms_url=artifact.terms_url,
        )
        _check_body(artifact, body)
        key = _object_key(artifact)
        metadata_key = _metadata_key(key)
        metadata = _metadata_body(artifact)
        # Check metadata before creating the body. A conflicting metadata object
        # must not leave a newly-created, uncommitted body behind.
        existing_metadata = self.client.get(metadata_key)
        if existing_metadata is not None and existing_metadata != metadata:
            raise ImmutableRawStoreError(f"immutable metadata already differs: {metadata_key}")
        self._put_immutable(key, body)
        self._put_immutable(metadata_key, metadata)
        return key

    def _put_immutable(self, key: str, body: bytes) -> None:
        """Use a conditional create; never overwrite an object after a race."""

        if self.client.put_if_absent(key, body):
            return
        existing = self.client.get(key)
        if existing is None:
            raise ImmutableRawStoreError(f"object creation raced with an unavailable object: {key}")
        if existing != body:
            raise ImmutableRawStoreError(f"immutable object already contains different bytes: {key}")

    def get(self, object_key: str) -> bytes:
        body = self.client.get(object_key)
        if body is None:
            raise KeyError(object_key)
        return body
