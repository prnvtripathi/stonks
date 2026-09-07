from datetime import date, datetime, timezone
from hashlib import sha256
from pathlib import Path

import pytest
from market_pipeline.domain.models import SourceArtifact
from market_pipeline.sources.registry import SourcePolicyError
from market_pipeline.storage.raw_store import (
    ImmutableRawStoreError,
    LocalRawStore,
    R2RawStore,
)


def make_artifact(body: bytes, filename: str = "report.csv") -> SourceArtifact:
    return SourceArtifact(
        source_id="amfi-nav",
        source_url="https://www.amfiindia.com/net-asset-value/nav-download",
        retrieved_at=datetime(2026, 9, 7, tzinfo=timezone.utc),
        effective_date=date(2026, 9, 4),
        checksum=sha256(body).hexdigest(),
        adapter_version="1.0.0",
        terms_url="https://www.amfiindia.com/terms-and-conditions",
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
    assert (tmp_path / f"{key}.metadata.json").is_file()


def test_local_store_rejects_conflicting_bytes(tmp_path: Path) -> None:
    body = b"a"
    artifact = make_artifact(body)
    store = LocalRawStore(tmp_path)
    key = store.put(artifact, body)

    with pytest.raises(ImmutableRawStoreError):
        store.put(artifact, b"b")
    assert store.get(key) == body


def test_metadata_is_per_artifact_filename(tmp_path: Path) -> None:
    body = b"a"
    first = make_artifact(body, "first.csv")
    second = make_artifact(body, "second.csv")
    store = LocalRawStore(tmp_path)

    first_key = store.put(first, body)
    second_key = store.put(second, body)
    assert first_key != second_key
    assert (tmp_path / f"{first_key}.metadata.json").is_file()
    assert (tmp_path / f"{second_key}.metadata.json").is_file()


def test_metadata_conflict_is_rejected_before_new_body_write(tmp_path: Path) -> None:
    body = b"a"
    first = make_artifact(body, "same.csv")
    second = first.model_copy(update={"retrieved_at": datetime(2026, 9, 8, tzinfo=timezone.utc)})
    store = LocalRawStore(tmp_path)
    key = store.put(first, body)

    with pytest.raises(ImmutableRawStoreError):
        store.put(second, body)
    assert store.get(key) == body


def test_local_metadata_conflict_is_rejected_before_body_creation(tmp_path: Path) -> None:
    body = b"a"
    artifact = make_artifact(body, "new.csv")
    key = f"raw/{artifact.source_id}/{artifact.effective_date}/{artifact.checksum}/new.csv"
    metadata_path = tmp_path / f"{key}.metadata.json"
    metadata_path.parent.mkdir(parents=True)
    metadata_path.write_bytes(b"conflicting metadata")

    with pytest.raises(ImmutableRawStoreError):
        LocalRawStore(tmp_path).put(artifact, body)
    assert not (tmp_path / key).exists()


def test_local_metadata_race_leaves_body_uncommitted(tmp_path: Path) -> None:
    body = b"a"
    artifact = make_artifact(body, "race.csv")

    class MetadataRaceStore(LocalRawStore):
        def _put_if_absent(self, path: Path, content: bytes) -> None:
            if path.name.endswith(".metadata.json") and not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"raced metadata")
            super()._put_if_absent(path, content)

    store = MetadataRaceStore(tmp_path)
    key = f"raw/{artifact.source_id}/{artifact.effective_date}/{artifact.checksum}/race.csv"
    with pytest.raises(ImmutableRawStoreError):
        store.put(artifact, body)
    assert not (tmp_path / f"{key}.commit.json").exists()
    with pytest.raises(KeyError):
        store.get(key)


def test_local_listing_excludes_uncommitted_bodies(tmp_path: Path) -> None:
    body = b"a"
    artifact = make_artifact(body, "listed.csv")
    store = LocalRawStore(tmp_path)
    key = store.put(artifact, body)
    orphan = tmp_path / "raw/amfi-nav/2026-09-04/orphan/orphan.csv"
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(body)

    assert store.list() == [key]


def test_local_store_rejects_keys_that_escape_the_root(tmp_path: Path) -> None:
    artifact = make_artifact(b"a", "../../outside.csv")
    with pytest.raises(ImmutableRawStoreError):
        LocalRawStore(tmp_path).put(artifact, b"a")


@pytest.mark.parametrize("filename", ["report.metadata.json", "report.commit.json"])
def test_local_store_rejects_reserved_sidecar_filenames(tmp_path: Path, filename: str) -> None:
    artifact = make_artifact(b"a", filename)
    with pytest.raises(ImmutableRawStoreError):
        LocalRawStore(tmp_path).put(artifact, b"a")


def test_local_store_rejects_unsafe_source_segments(tmp_path: Path) -> None:
    artifact = make_artifact(b"a")
    artifact = artifact.model_copy(update={"source_id": "../../outside"})
    with pytest.raises(SourcePolicyError):
        LocalRawStore(tmp_path).put(artifact, b"a")


def test_local_store_rejects_escaping_get_keys(tmp_path: Path) -> None:
    with pytest.raises(ImmutableRawStoreError):
        LocalRawStore(tmp_path).get("raw/../../outside")


class MemoryClient:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.put_if_absent_calls = 0
        self.race_on_metadata = False

    def put_if_absent(self, key: str, body: bytes) -> bool:
        self.put_if_absent_calls += 1
        if self.race_on_metadata and key.endswith(".metadata.json") and key not in self.objects:
            self.objects[key] = b"raced metadata"
            return False
        if key in self.objects:
            return False
        self.objects[key] = body
        return True

    def get(self, key: str) -> bytes | None:
        return self.objects.get(key)

    def list(self, prefix: str = "") -> list[str]:
        return sorted(key for key in self.objects if key.startswith(prefix))


def test_r2_store_uses_injected_client_and_same_key_layout() -> None:
    body = b"a"
    artifact = make_artifact(body, "report.json")
    client = MemoryClient()
    store = R2RawStore(client)

    key = store.put(artifact, body)
    assert key.startswith("raw/amfi-nav/2026-09-04/")
    assert client.objects[key] == body
    assert store.get(key) == body


def test_r2_conditional_create_rejects_existing_conflicting_bytes() -> None:
    body = b"a"
    artifact = make_artifact(body, "report.json")
    client = MemoryClient()
    store = R2RawStore(client)
    key = store.put(artifact, body)
    client.objects[key] = b"different"

    with pytest.raises(ImmutableRawStoreError):
        store.put(artifact, body)
    assert client.put_if_absent_calls == 4


def test_r2_metadata_conflict_is_rejected_before_body_creation() -> None:
    body = b"a"
    artifact = make_artifact(body, "new.json")
    key = f"raw/{artifact.source_id}/{artifact.effective_date}/{artifact.checksum}/new.json"
    client = MemoryClient()
    client.objects[f"{key}.metadata.json"] = b"conflicting metadata"

    with pytest.raises(ImmutableRawStoreError):
        R2RawStore(client).put(artifact, body)
    assert key not in client.objects


def test_r2_metadata_race_leaves_body_uncommitted() -> None:
    body = b"a"
    artifact = make_artifact(body, "race.json")
    client = MemoryClient()
    client.race_on_metadata = True
    store = R2RawStore(client)
    key = f"raw/{artifact.source_id}/{artifact.effective_date}/{artifact.checksum}/race.json"

    with pytest.raises(ImmutableRawStoreError):
        store.put(artifact, body)
    assert f"{key}.commit.json" not in client.objects
    with pytest.raises(KeyError):
        store.get(key)


def test_r2_listing_excludes_uncommitted_bodies() -> None:
    body = b"a"
    artifact = make_artifact(body, "listed.json")
    client = MemoryClient()
    store = R2RawStore(client)
    key = store.put(artifact, body)
    orphan = "raw/amfi-nav/2026-09-04/orphan/orphan.json"
    client.objects[orphan] = body

    assert store.list() == [key]
