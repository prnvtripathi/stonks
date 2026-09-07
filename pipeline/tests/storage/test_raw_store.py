from datetime import date, datetime, timezone
from hashlib import sha256
from pathlib import Path

import pytest
from market_pipeline.domain.models import SourceArtifact
from market_pipeline.storage.raw_store import (
    ImmutableRawStoreError,
    LocalRawStore,
    R2RawStore,
)


def make_artifact(body: bytes, filename: str = "report.csv") -> SourceArtifact:
    return SourceArtifact(
        source_id="nse-eod",
        source_url="https://www.nseindia.com/all-reports",
        retrieved_at=datetime(2026, 9, 7, tzinfo=timezone.utc),
        effective_date=date(2026, 9, 4),
        checksum=sha256(body).hexdigest(),
        adapter_version="1.0.0",
        terms_url="https://www.nseindia.com/terms-of-use",
        filename=filename,
    )


def test_local_store_is_idempotent_and_writes_metadata(tmp_path: Path) -> None:
    body = b"symbol,close\nINFY,1500\n"
    artifact = make_artifact(body)
    store = LocalRawStore(tmp_path)

    key = store.put(artifact, body)
    assert store.put(artifact, body) == key
    assert store.get(key) == body
    assert (tmp_path / key).is_file()
    assert (tmp_path / key.rsplit("/", 1)[0] / "metadata.json").is_file()


def test_local_store_rejects_conflicting_bytes(tmp_path: Path) -> None:
    body = b"a"
    artifact = make_artifact(body)
    store = LocalRawStore(tmp_path)
    key = store.put(artifact, body)

    with pytest.raises(ImmutableRawStoreError):
        store.put(artifact, b"b")
    assert store.get(key) == body


def test_local_store_rejects_keys_that_escape_the_root(tmp_path: Path) -> None:
    artifact = make_artifact(b"a", "../../outside.csv")
    with pytest.raises(ImmutableRawStoreError):
        LocalRawStore(tmp_path).put(artifact, b"a")


def test_local_store_rejects_unsafe_source_segments(tmp_path: Path) -> None:
    artifact = make_artifact(b"a")
    artifact = artifact.model_copy(update={"source_id": "../../outside"})
    with pytest.raises(ImmutableRawStoreError):
        LocalRawStore(tmp_path).put(artifact, b"a")


def test_local_store_rejects_escaping_get_keys(tmp_path: Path) -> None:
    with pytest.raises(ImmutableRawStoreError):
        LocalRawStore(tmp_path).get("raw/../../outside")


class MemoryClient:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put(self, key: str, body: bytes) -> None:
        self.objects[key] = body

    def get(self, key: str) -> bytes | None:
        return self.objects.get(key)


def test_r2_store_uses_injected_client_and_same_key_layout() -> None:
    body = b"a"
    artifact = make_artifact(body, "report.json")
    client = MemoryClient()
    store = R2RawStore(client)

    key = store.put(artifact, body)
    assert key.startswith("raw/nse-eod/2026-09-04/")
    assert client.objects[key] == body
    assert store.get(key) == body
