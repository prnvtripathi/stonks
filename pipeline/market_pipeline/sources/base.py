"""Contracts implemented by provider adapters."""

from __future__ import annotations

from datetime import date
from typing import Protocol

from market_pipeline.domain.models import FetchedArtifact, SourceArtifact, SourcePolicy


class _SourceAdapterImplementation(Protocol):
    source_id: str
    adapter_version: str
    policy: SourcePolicy

    def _fetch(self, effective_date: date) -> list[FetchedArtifact]: ...


class SourceAdapter:
    """Governed public adapter wrapper; direct implementation fetch is private."""

    def __init__(self, implementation: _SourceAdapterImplementation) -> None:
        self._implementation = implementation

    @property
    def source_id(self) -> str:
        return self._implementation.source_id

    @property
    def adapter_version(self) -> str:
        return self._implementation.adapter_version

    @property
    def policy(self) -> SourcePolicy:
        return self._implementation.policy

    def fetch(self, effective_date: date) -> list[FetchedArtifact]:
        from market_pipeline.sources.registry import assert_adapter_enabled

        assert_adapter_enabled(self)
        return self._implementation._fetch(effective_date)


class RawStore(Protocol):
    def put(self, artifact: SourceArtifact, body: bytes) -> str: ...

    def get(self, object_key: str) -> bytes: ...

