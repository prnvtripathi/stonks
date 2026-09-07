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

__all__ = ["ImmutableRawStoreError", "LocalRawStore", "ObjectClient", "R2RawStore", "RawStore"]


class ImmutableRawStoreError(RuntimeError):
    """Raised when an immutable object would be overwritten or fails integrity checks."""


class ObjectClient(Protocol):
    def put(self, key: str, body: bytes) -> None: ...

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
    return f"{key.rsplit('/', 1)[0]}/metadata.json"


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
        _check_body(artifact, body)
        key = _object_key(artifact)
        path = self.root / key
        metadata_path = path.parent / "metadata.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if path.read_bytes() != body:
                raise ImmutableRawStoreError(f"immutable object already contains different bytes: {key}")
        else:
            temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            temporary.write_bytes(body)
            temporary.replace(path)
        metadata = _metadata_body(artifact)
        if metadata_path.exists():
            if metadata_path.read_bytes() != metadata:
                raise ImmutableRawStoreError(f"immutable metadata already differs: {metadata_path}")
        else:
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
        _check_body(artifact, body)
        key = _object_key(artifact)
        existing = self.client.get(key)
        if existing is not None:
            if existing != body:
                raise ImmutableRawStoreError(f"immutable object already contains different bytes: {key}")
        else:
            self.client.put(key, body)
        metadata_key = _metadata_key(key)
        metadata = _metadata_body(artifact)
        existing_metadata = self.client.get(metadata_key)
        if existing_metadata is not None:
            if existing_metadata != metadata:
                raise ImmutableRawStoreError(f"immutable metadata already differs: {metadata_key}")
        else:
            self.client.put(metadata_key, metadata)
        return key

    def get(self, object_key: str) -> bytes:
        body = self.client.get(object_key)
        if body is None:
            raise KeyError(object_key)
        return body
