"""R11/F13: real selective bundle restoration replaces pointer-only rollback.

``db/migrations/0006_bounded_instrument_snapshots.sql`` collapsed
``instrument_snapshots`` to one compact row per instrument, and every
promotion (``publication/d1_export.py``) deletes every other dataset's
serving rows. ``docs/operations/recovery.md`` used to instruct a
pointer-only rollback (flip ``active_dataset``/``datasets.status`` directly);
this test file first reproduces that being broken, then exercises
``market_pipeline.publication.restore`` as the real fix.

``_FakeObjectStore`` below is the same private, in-memory stand-in for the
R2-compatible ``S3ObjectClient`` protocol
(``market_pipeline.publication.r2_sync.S3ObjectClient``) that
``test_checkpoint_restore.py``/``test_r2_sync.py`` already use. No network
call, no real Cloudflare account, anywhere in this file.
"""

from __future__ import annotations

import io
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from market_pipeline.publication.bundle import BundleObject, build_bundle
from market_pipeline.publication.d1_export import export_active_dataset
from market_pipeline.publication.restore import (
    RestoreError,
    list_restorable_datasets,
    restore_dataset,
    select_restore_bundle,
)
from market_pipeline.storage.d1_publisher import D1Publisher, publish
from market_pipeline.storage.history_store import HistoryStore


class _FakeObjectStore:
    """Controllable, in-memory private object store. No network calls."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.put_calls: list[str] = []
        self.get_calls: list[str] = []

    def put_object(self, *, Bucket: str, Key: str, Body: Any) -> None:
        self.put_calls.append(Key)
        self.objects[Key] = Body.read()

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        self.get_calls.append(Key)
        if Key not in self.objects:
            raise KeyError(Key)
        return {"Body": io.BytesIO(self.objects[Key])}

    def tamper(self, key: str, body: bytes) -> None:
        assert key in self.objects
        self.objects[key] = body

    def delete(self, key: str) -> None:
        del self.objects[key]


def _seed_remote_from(client: _FakeObjectStore, history_root: Path) -> None:
    """Populate the fake R2 store with every object under ``history_root``."""

    for path in history_root.rglob("*"):
        if path.is_file():
            client.objects[path.relative_to(history_root).as_posix()] = path.read_bytes()


def _candidate(dataset_id: str, *, symbol: str, name: str, effective_date: str = "2026-08-01") -> dict[str, Any]:
    return {
        "dataset_id": dataset_id,
        "effective_date": effective_date,
        "metadata": {"required_sources": ["amfi-nav"]},
        "tables": {
            "sources": [
                {
                    "source_id": "amfi-nav",
                    "source_url": "https://www.amfiindia.com/nav",
                    "terms_url": "https://www.amfiindia.com/terms",
                    "status": "complete",
                    "effective_date": effective_date,
                }
            ],
            "source_runs": [
                {"run_id": f"run-{dataset_id}", "source_id": "amfi-nav", "effective_date": effective_date, "status": "complete"}
            ],
            "instruments": [
                {
                    "instrument_id": "instrument-1",
                    "provider": "amfi",
                    "provider_identifier": "scheme-1",
                    "asset_class": "mutual_fund",
                    "symbol": symbol,
                    "name": name,
                }
            ],
            "latest_metrics": [
                {"instrument_id": "instrument-1", "effective_date": effective_date, "metric": "return_1d", "value": 0.01, "state": "present"}
            ],
        },
    }


def _publish_with_bundle(
    local: sqlite3.Connection,
    local_publisher: D1Publisher,
    history_root: Path,
    *,
    dataset_id: str,
    symbol: str,
    name: str,
    input_manifest_hash: str,
) -> None:
    store = HistoryStore(history_root)
    [history_object] = store.write_history(
        "mutual_fund", "instrument-1", [{"effective_date": "2026-08-01", "value": "1"}]
    )
    chart_object = store.write_chart(dataset_id, "instrument-1", {"points": []})
    bundle = build_bundle(
        dataset_id=dataset_id,
        input_manifest_hash=input_manifest_hash,
        source_dates=["2026-08-01"],
        projection_version="d1-projection-v1",
        candidate_entries=[
            BundleObject(key=history_object.key, sha256=history_object.sha256, bytes=history_object.bytes, kind="history"),
            BundleObject(key=chart_object.key, sha256=chart_object.sha256, bytes=chart_object.bytes, kind="chart"),
        ],
        effective_date=date(2026, 8, 1),
    )
    candidate = _candidate(dataset_id, symbol=symbol, name=name)
    candidate["metadata"]["publication_bundle"] = bundle.as_dict()
    publish(candidate, local_publisher)


def _apply_export(local: sqlite3.Connection, remote: sqlite3.Connection, tmp_path: Path, dataset_id: str, history_root: Path) -> None:
    sql_path = tmp_path / f"{dataset_id}-export.sql"
    export_active_dataset(local, sql_path, history_root=history_root, object_manifest=tmp_path / f"{dataset_id}-manifest.json")
    remote.executescript(sql_path.read_text(encoding="utf-8"))


def test_pointer_only_rollback_returns_zero_serving_rows(tmp_path: Path) -> None:
    """Reproduce F13: the old runbook procedure is now broken."""

    history_root = tmp_path / "history"
    local = sqlite3.connect(":memory:")
    local_publisher = D1Publisher(local)
    local_publisher.initialize_schema()
    remote = sqlite3.connect(":memory:")
    remote_publisher = D1Publisher(remote)
    remote_publisher.initialize_schema()

    _publish_with_bundle(local, local_publisher, history_root, dataset_id="A", symbol="ALPHA", name="Alpha Fund", input_manifest_hash="a" * 64)
    _apply_export(local, remote, tmp_path, "A", history_root)

    _publish_with_bundle(local, local_publisher, history_root, dataset_id="B", symbol="BETA", name="Beta Fund", input_manifest_hash="b" * 64)
    _apply_export(local, remote, tmp_path, "B", history_root)

    # Sanity: B is active and A's serving rows are already gone (0006's DELETE).
    assert remote_publisher.active_dataset_id() == "B"
    assert remote.execute("SELECT COUNT(*) FROM instrument_snapshots WHERE dataset_id='A'").fetchone()[0] == 0

    # The OLD, now-broken runbook procedure: flip the pointer/status directly.
    remote.execute("BEGIN IMMEDIATE")
    remote.execute("UPDATE datasets SET status='superseded' WHERE status='active'")
    remote.execute("UPDATE datasets SET status='active', promoted_at='2026-08-02T00:00:00Z' WHERE dataset_id='A'")
    remote.execute(
        "INSERT INTO active_dataset(singleton, dataset_id, changed_at) VALUES (1, 'A', '2026-08-02T00:00:00Z') "
        "ON CONFLICT(singleton) DO UPDATE SET dataset_id=excluded.dataset_id, changed_at=excluded.changed_at"
    )
    remote.commit()

    assert remote_publisher.active_dataset_id() == "A"
    # This is the F13 bug: the pointer says A, but serving reads nothing.
    assert remote.execute("SELECT COUNT(*) FROM instrument_snapshots WHERE dataset_id='A'").fetchone()[0] == 0


def test_selective_restore_reimports_projection_and_preserves_post_promotion_product_state(tmp_path: Path) -> None:
    """The real fix: restore reimports A's rows and never disturbs B-era product state."""

    history_root = tmp_path / "history"
    local = sqlite3.connect(":memory:")
    local_publisher = D1Publisher(local)
    local_publisher.initialize_schema()
    remote = sqlite3.connect(":memory:")
    remote_publisher = D1Publisher(remote)
    remote_publisher.initialize_schema()

    _publish_with_bundle(local, local_publisher, history_root, dataset_id="A", symbol="ALPHA", name="Alpha Fund", input_manifest_hash="a" * 64)
    _apply_export(local, remote, tmp_path, "A", history_root)

    # Product state created against A: a saved screen and a run (owned by the
    # Worker; here inserted directly to stand in for that side of the system).
    remote.execute(
        "INSERT INTO saved_screens(screen_id, name, expression, created_at, updated_at) VALUES "
        "('screen-1', 'Momentum', 'return_1d > 0', '2026-08-01T00:00:00Z', '2026-08-01T00:00:00Z')"
    )
    remote.execute(
        "INSERT INTO screen_runs(dataset_id, run_id, screen_id, effective_date, status, result_count, completed_at) VALUES "
        "('A', 'run-before-b', 'screen-1', '2026-08-01', 'complete', 1, '2026-08-01T00:00:00Z')"
    )
    remote.commit()

    _publish_with_bundle(local, local_publisher, history_root, dataset_id="B", symbol="BETA", name="Beta Fund", input_manifest_hash="b" * 64)
    _apply_export(local, remote, tmp_path, "B", history_root)
    assert remote.execute("SELECT COUNT(*) FROM instrument_snapshots WHERE dataset_id='A'").fetchone()[0] == 0

    # Post-B-promotion product-state edit: editing the saved screen, and a
    # new run recorded against B.
    remote.execute(
        "UPDATE saved_screens SET expression='return_1d > 0.005', updated_at='2026-08-03T00:00:00Z' WHERE screen_id='screen-1'"
    )
    remote.execute(
        "INSERT INTO screen_runs(dataset_id, run_id, screen_id, effective_date, status, result_count, completed_at) VALUES "
        "('B', 'run-after-b', 'screen-1', '2026-08-02', 'complete', 1, '2026-08-02T00:00:00Z')"
    )
    remote.commit()

    client = _FakeObjectStore()
    _seed_remote_from(client, history_root)

    restorable = {item.dataset_id for item in list_restorable_datasets(local)}
    assert restorable == {"A", "B"}

    restore_sql_path = tmp_path / "restore-A.sql"
    result = restore_dataset(
        local, "A", output=restore_sql_path, history_root=history_root, client=client,
        object_manifest=tmp_path / "restore-A-manifest.json",
    )
    assert result.dataset_id == "A"
    assert result.r2_sync is not None
    assert result.r2_sync.verified_objects == len(result.bundle.objects)

    remote.executescript(restore_sql_path.read_text(encoding="utf-8"))

    assert remote_publisher.active_dataset_id() == "A"
    assert remote.execute("SELECT dataset_id, symbol, name FROM instrument_snapshots").fetchall() == [
        ("A", "ALPHA", "Alpha Fund")
    ]
    # The B-era screen edit and both runs (recorded before and after the
    # restore point) survive untouched: restore never reaches into product
    # state.
    assert remote.execute("SELECT expression FROM saved_screens WHERE screen_id='screen-1'").fetchone() == (
        "return_1d > 0.005",
    )
    assert set(row[0] for row in remote.execute("SELECT run_id FROM screen_runs ORDER BY run_id")) == {
        "run-after-b",
        "run-before-b",
    }
    # B itself is preserved (superseded, not deleted) for diagnosis.
    assert remote.execute("SELECT status FROM datasets WHERE dataset_id='B'").fetchone() == ("superseded",)


def test_restore_rejects_a_dataset_outside_the_retained_set(tmp_path: Path) -> None:
    history_root = tmp_path / "history"
    local = sqlite3.connect(":memory:")
    local_publisher = D1Publisher(local)
    local_publisher.initialize_schema()

    for dataset_id, symbol, hash_ in (("A", "ALPHA", "a" * 64), ("B", "BETA", "b" * 64), ("C", "GAMMA", "c" * 64)):
        _publish_with_bundle(local, local_publisher, history_root, dataset_id=dataset_id, symbol=symbol, name=f"{symbol} Fund", input_manifest_hash=hash_)

    # Only C (active) and B (previous) are retained; A fell out of the window.
    restorable = {item.dataset_id for item in list_restorable_datasets(local)}
    assert restorable == {"B", "C"}

    with pytest.raises(RestoreError, match="not a currently-retained"):
        select_restore_bundle(local, "A")

    with pytest.raises(RestoreError, match="not a currently-retained"):
        restore_dataset(local, "A", output=tmp_path / "should-not-exist.sql", history_root=history_root)
    assert not (tmp_path / "should-not-exist.sql").exists()


def test_restore_fails_closed_before_mutation_when_local_object_is_corrupt(tmp_path: Path) -> None:
    history_root = tmp_path / "history"
    local = sqlite3.connect(":memory:")
    local_publisher = D1Publisher(local)
    local_publisher.initialize_schema()
    remote = sqlite3.connect(":memory:")
    remote_publisher = D1Publisher(remote)
    remote_publisher.initialize_schema()

    _publish_with_bundle(local, local_publisher, history_root, dataset_id="A", symbol="ALPHA", name="Alpha Fund", input_manifest_hash="a" * 64)
    _apply_export(local, remote, tmp_path, "A", history_root)
    _publish_with_bundle(local, local_publisher, history_root, dataset_id="B", symbol="BETA", name="Beta Fund", input_manifest_hash="b" * 64)
    _apply_export(local, remote, tmp_path, "B", history_root)
    assert remote_publisher.active_dataset_id() == "B"

    bundle = select_restore_bundle(local, "A")
    history_entry = next(item for item in bundle.objects if item.kind == "history")
    (history_root / history_entry.key).write_bytes(b"corrupted")

    sql_path = tmp_path / "restore-A.sql"
    with pytest.raises(RestoreError, match="local object verification failed"):
        restore_dataset(local, "A", output=sql_path, history_root=history_root)

    # Nothing was generated or applied; the previously-active dataset is untouched.
    assert not sql_path.exists()
    assert remote_publisher.active_dataset_id() == "B"


def test_restore_fails_closed_before_mutation_when_remote_object_verification_fails(tmp_path: Path) -> None:
    """A remote object store that persistently corrupts every PUT can never
    be verified into a matching state -- r2_sync's PUT-then-GET-verify retry
    exhausts its bounded attempts and raises, which restore must surface
    before generating or applying any SQL."""

    class _PersistentlyCorruptingObjectStore:
        def __init__(self) -> None:
            self.objects: dict[str, bytes] = {}

        def put_object(self, *, Bucket: str, Key: str, Body: Any) -> None:
            Body.read()
            self.objects[Key] = b"corrupted-on-write"

        def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
            if Key not in self.objects:
                raise KeyError(Key)
            return {"Body": io.BytesIO(self.objects[Key])}

    history_root = tmp_path / "history"
    local = sqlite3.connect(":memory:")
    local_publisher = D1Publisher(local)
    local_publisher.initialize_schema()
    remote = sqlite3.connect(":memory:")
    remote_publisher = D1Publisher(remote)
    remote_publisher.initialize_schema()

    _publish_with_bundle(local, local_publisher, history_root, dataset_id="A", symbol="ALPHA", name="Alpha Fund", input_manifest_hash="a" * 64)
    _apply_export(local, remote, tmp_path, "A", history_root)
    _publish_with_bundle(local, local_publisher, history_root, dataset_id="B", symbol="BETA", name="Beta Fund", input_manifest_hash="b" * 64)
    _apply_export(local, remote, tmp_path, "B", history_root)
    assert remote_publisher.active_dataset_id() == "B"

    sql_path = tmp_path / "restore-A.sql"
    with pytest.raises(RestoreError, match="remote object verification failed"):
        restore_dataset(
            local, "A", output=sql_path, history_root=history_root,
            client=_PersistentlyCorruptingObjectStore(), deadline_seconds=5,
        )

    assert not sql_path.exists()
    assert remote_publisher.active_dataset_id() == "B"


def test_restore_rejects_a_transport_failure_via_wrapped_r2sync_error(tmp_path: Path) -> None:
    """A hard R2 transport failure during verification/re-upload surfaces as RestoreError."""

    class _AlwaysFailingObjectStore:
        def put_object(self, *, Bucket: str, Key: str, Body: Any) -> None:
            raise RuntimeError("simulated transport failure")

        def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
            raise RuntimeError("simulated transport failure")

    history_root = tmp_path / "history"
    local = sqlite3.connect(":memory:")
    local_publisher = D1Publisher(local)
    local_publisher.initialize_schema()

    _publish_with_bundle(local, local_publisher, history_root, dataset_id="A", symbol="ALPHA", name="Alpha Fund", input_manifest_hash="a" * 64)

    bundle = select_restore_bundle(local, "A")
    assert bundle.objects

    with pytest.raises(RestoreError, match="remote object verification failed"):
        restore_dataset(
            local, "A", output=tmp_path / "unused.sql", history_root=history_root,
            client=_AlwaysFailingObjectStore(), deadline_seconds=5,
        )
    assert not (tmp_path / "unused.sql").exists()


def test_restore_requires_history_root_to_verify_against_a_remote_client(tmp_path: Path) -> None:
    local = sqlite3.connect(":memory:")
    local_publisher = D1Publisher(local)
    local_publisher.initialize_schema()
    _publish_with_bundle(local, local_publisher, tmp_path / "history", dataset_id="A", symbol="ALPHA", name="Alpha Fund", input_manifest_hash="a" * 64)

    with pytest.raises(RestoreError, match="history_root is required"):
        restore_dataset(local, "A", output=tmp_path / "unused.sql", client=_FakeObjectStore())


def test_local_market_db_pointer_flip_is_harmless_because_it_never_serves_snapshots(tmp_path: Path) -> None:
    """The local sqlite `market.db` never serves reads from instrument_snapshots
    (only the remote SQL import populates that table -- see d1_export.py), so a
    local-only pointer flip (still documented as local-only in the runbook)
    does not reproduce F13's data-loss symptom the way the same flip against
    production D1 does."""

    local = sqlite3.connect(":memory:")
    local_publisher = D1Publisher(local)
    local_publisher.initialize_schema()
    publish(_candidate("A", symbol="ALPHA", name="Alpha Fund"), local_publisher)
    publish(_candidate("B", symbol="BETA", name="Beta Fund"), local_publisher)
    assert local_publisher.active_dataset_id() == "B"
    assert local.execute("SELECT COUNT(*) FROM instrument_snapshots").fetchone()[0] == 0

    local.execute("BEGIN IMMEDIATE")
    local.execute("UPDATE datasets SET status='superseded' WHERE status='active'")
    local.execute("UPDATE datasets SET status='active', promoted_at='2026-08-02T00:00:00Z' WHERE dataset_id='A'")
    local.execute(
        "INSERT INTO active_dataset(singleton, dataset_id, changed_at) VALUES (1, 'A', '2026-08-02T00:00:00Z') "
        "ON CONFLICT(singleton) DO UPDATE SET dataset_id=excluded.dataset_id, changed_at=excluded.changed_at"
    )
    local.commit()

    assert local_publisher.active_dataset_id() == "A"
    # No local serving projection ever existed to lose in the first place.
    assert local.execute("SELECT COUNT(*) FROM instrument_snapshots").fetchone()[0] == 0
