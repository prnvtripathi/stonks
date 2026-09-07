"""Contracts implemented by provider adapters."""

from __future__ import annotations

from datetime import date
from typing import Protocol

from market_pipeline.domain.models import FetchedArtifact, SourceArtifact, SourcePolicy


class SourceAdapter(Protocol):
    source_id: str
    adapter_version: str
    policy: SourcePolicy

    def fetch(self, effective_date: date) -> list[FetchedArtifact]: ...


class RawStore(Protocol):
    def put(self, artifact: SourceArtifact, body: bytes) -> str: ...

    def get(self, object_key: str) -> bytes: ...
