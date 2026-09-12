"""F12: durable checkpoint-archive restore for a clean/cache-missed runner.

These tests never call a real Cloudflare API. ``_FakeObjectStore`` below is a
private, in-memory stand-in for the R2-compatible ``S3ObjectClient`` protocol
(see ``market_pipeline.publication.r2_sync.S3ObjectClient``), the same style
``test_r2_sync.py`` already uses for its own fake client.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import shutil
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from market_pipeline.cli import main
from market_pipeline.publication.checkpoint_archive import (
    CheckpointArchiveError,
    NoCheckpointArchiveError,
    build_and_upload_checkpoint_archive,
    check_local_identity_against_manifest,
    checkpoint_archive_ledger_entry,
    load_latest_success,
    promote_latest_success,
    restore_checkpoint_archive,
)

TERMS_URL = "https://www.amfiindia.com/terms-and-conditions"
SOURCE_URL = "https://www.amfiindia.com/spages/NAVAll.txt"
SCHEMES = (
    ("119551", "Alpha Bluechip Fund - Direct Plan - Growth", 100.0, 1.0006),
    ("119552", "Beta Bluechip Fund - Direct Plan - Growth", 250.0, 1.0002),
)


class _FakeObjectStore:
    """Controllable, in-memory private object store. No network calls.

    Contract required by ``checkpoint_archive.py``:  ``get_object`` raises
    ``KeyError`` for a genuinely absent key (the one signal treated as "no
    archive was ever promoted"), and any other injected failure is a
    different exception type entirely.
    """

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.put_calls: list[str] = []
        self.get_calls: list[str] = []
        self.fail_keys: dict[str, int] = {}

    def put_object(self, *, Bucket: str, Key: str, Body: Any) -> None:
        self.put_calls.append(Key)
        if self.fail_keys.get(Key, 0) > 0:
            self.fail_keys[Key] -= 1
            raise RuntimeError(f"simulated PUT failure for {Key}")
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


def _nav_report(effective: date, session: int) -> bytes:
    lines = [
        "Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Net Asset Value;Date",
        "",
        "Alpha Asset Management Mutual Fund",
        "",
        "Open Ended Schemes(Equity Scheme - Large Cap Fund)",
    ]
    for code, name, base, drift in SCHEMES:
        nav = round(base * (drift**session), 4)
        lines.append(f"{code};INF{code}01AA1;INF{code}01AB9;{name};{nav};{effective.strftime('%d-%b-%Y')}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _artifact_entry(effective: date, session: int) -> dict[str, Any]:
    body = _nav_report(effective, session)
    return {
        "artifact": {
            "source_id": "amfi-nav",
            "source_url": SOURCE_URL,
            "retrieved_at": datetime.now(UTC).isoformat(),
            "effective_date": effective.isoformat(),
            "checksum": hashlib.sha256(body).hexdigest(),
            "adapter_version": "v1",
            "terms_url": TERMS_URL,
            "filename": "NAVAll.txt",
        },
        "body_base64": base64.b64encode(body).decode(),
    }


def _write_manifest(path: Path, start: date, sessions: int) -> tuple[date, date]:
    artifacts = [_artifact_entry(start + timedelta(days=offset), offset) for offset in range(sessions)]
    path.write_text(json.dumps({"artifacts": artifacts}), encoding="utf-8")
    return start, start + timedelta(days=sessions - 1)


def _dataset_identity(db: Path) -> tuple[str, str]:
    connection = sqlite3.connect(db)
    try:
        row = connection.execute(
            "SELECT d.dataset_id, d.metadata_json FROM datasets d "
            "JOIN active_dataset a ON a.dataset_id = d.dataset_id WHERE a.singleton = 1"
        ).fetchone()
    finally:
        connection.close()
    dataset_id, metadata_json = row
    metadata = json.loads(metadata_json)
    return str(dataset_id), str(metadata["input_manifest_sha256"])


def _return_1w(db: Path, symbol: str) -> float:
    connection = sqlite3.connect(db)
    try:
        row = connection.execute(
            "SELECT m.value FROM latest_metrics m "
            "JOIN instruments i ON i.instrument_id = m.instrument_id AND i.dataset_id = m.dataset_id "
            "JOIN active_dataset a ON a.dataset_id = m.dataset_id "
            "WHERE a.singleton = 1 AND m.metric = 'return_1w' AND i.symbol = ?",
            (symbol,),
        ).fetchone()
    finally:
        connection.close()
    assert row is not None, f"no return_1w row for {symbol}"
    return float(row[0])


def _run(db: Path, raw_root: Path, args: list[str], capsys: Any) -> dict[str, Any]:
    db.parent.mkdir(parents=True, exist_ok=True)
    exit_code = main(["--db", str(db), "--raw-store", str(raw_root), *args])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0, payload
    assert payload["publication"]["promoted"] is True, payload
    return payload


SESSIONS = 60


def test_checkpoint_archive_restores_full_history_after_total_local_loss(tmp_path: Path, capsys: Any) -> None:
    manifest = tmp_path / "manifest.json"
    start, end = _write_manifest(manifest, date(2025, 1, 2), SESSIONS)
    date1 = start + timedelta(days=SESSIONS - 3)
    date2 = start + timedelta(days=SESSIONS - 2)
    date3 = end

    # "Continuous" runner: one uninterrupted backfill over the whole range,
    # used only as the independent oracle for what date3's numbers should be.
    continuous_db = tmp_path / "continuous" / "market.db"
    continuous_raw = tmp_path / "continuous" / "raw"
    _run(
        continuous_db, continuous_raw,
        ["backfill", "--start", start.isoformat(), "--end", date3.isoformat(), "--manifest", str(manifest)],
        capsys,
    )
    continuous_dataset_id, continuous_input_manifest_hash = _dataset_identity(continuous_db)
    continuous_return_1w = _return_1w(continuous_db, SCHEMES[0][0])

    # "Clean" runner: publishes two dates, archives, then loses everything.
    work = tmp_path / "work"
    db = work / "market.db"
    raw_root = work / "raw"

    _run(db, raw_root, ["backfill", "--start", start.isoformat(), "--end", date1.isoformat(), "--manifest", str(manifest)], capsys)
    payload2 = _run(db, raw_root, ["daily", "--date", date2.isoformat(), "--manifest", str(manifest)], capsys)
    dataset2 = payload2["publication"]["dataset_id"]

    store = _FakeObjectStore()
    upload = build_and_upload_checkpoint_archive(db_path=db, raw_root=raw_root, client=store, deadline_seconds=30)
    assert upload.manifest.dataset_id == dataset2
    promote_latest_success(store, upload.manifest, remote_active_dataset_id=dataset2)

    # Total local loss: exactly what a GitHub Actions cache miss looks like.
    shutil.rmtree(work)
    assert not db.exists()
    assert not raw_root.exists()

    restore = restore_checkpoint_archive(store, db_path=db, raw_root=raw_root, deadline_seconds=30)
    assert restore.manifest.dataset_id == dataset2
    assert db.exists()
    restored_dataset_id, restored_input_manifest_hash = _dataset_identity(db)
    assert restored_dataset_id == dataset2

    # Publish the third date on the restored state.
    _run(db, raw_root, ["daily", "--date", date3.isoformat(), "--manifest", str(manifest)], capsys)
    clean_dataset_id, clean_input_manifest_hash = _dataset_identity(db)
    clean_return_1w = _return_1w(db, SCHEMES[0][0])

    assert clean_return_1w == continuous_return_1w
    assert clean_input_manifest_hash == continuous_input_manifest_hash


def test_restore_rejects_a_corrupted_archive_and_never_writes_a_blank_database(tmp_path: Path, capsys: Any) -> None:
    manifest = tmp_path / "manifest.json"
    start, end = _write_manifest(manifest, date(2025, 3, 3), 20)
    db = tmp_path / "work" / "market.db"
    raw_root = tmp_path / "work" / "raw"
    payload = _run(db, raw_root, ["backfill", "--start", start.isoformat(), "--end", end.isoformat(), "--manifest", str(manifest)], capsys)
    dataset_id = payload["publication"]["dataset_id"]

    store = _FakeObjectStore()
    upload = build_and_upload_checkpoint_archive(db_path=db, raw_root=raw_root, client=store, deadline_seconds=30)
    promote_latest_success(store, upload.manifest, remote_active_dataset_id=dataset_id)

    # Corrupt the SQLite backup object in the remote store directly.
    store.tamper(upload.manifest.sqlite_backup.key, b"not a valid sqlite backup at all")

    shutil.rmtree(tmp_path / "work")
    restore_db = tmp_path / "restored" / "market.db"
    restore_raw = tmp_path / "restored" / "raw"

    with pytest.raises(CheckpointArchiveError):
        restore_checkpoint_archive(store, db_path=restore_db, raw_root=restore_raw, deadline_seconds=30)

    # A corrupt archive must never let a caller silently proceed against a
    # freshly initialized, blank database -- and here, none was ever written.
    assert not restore_db.exists()


def test_interrupted_backup_leaves_no_success_pointer_and_no_partial_manifest(tmp_path: Path, capsys: Any) -> None:
    manifest = tmp_path / "manifest.json"
    start, end = _write_manifest(manifest, date(2025, 4, 1), 20)
    db = tmp_path / "work" / "market.db"
    raw_root = tmp_path / "work" / "raw"
    _run(db, raw_root, ["backfill", "--start", start.isoformat(), "--end", end.isoformat(), "--manifest", str(manifest)], capsys)

    store = _FakeObjectStore()
    # Force every PUT to fail persistently -- simulates a connection lost
    # partway through uploading raw triples, before the manifest is reached.
    real_put = store.put_object

    def _always_fail(*, Bucket: str, Key: str, Body: Any) -> None:
        raise RuntimeError("simulated interrupted transport")

    store.put_object = _always_fail  # type: ignore[method-assign]

    with pytest.raises(Exception):
        build_and_upload_checkpoint_archive(db_path=db, raw_root=raw_root, client=store, deadline_seconds=5)

    # Nothing was ever written -- not the manifest, and certainly not a
    # promoted success pointer.
    assert store.objects == {}
    store.put_object = real_put  # type: ignore[method-assign]
    with pytest.raises(NoCheckpointArchiveError):
        load_latest_success(store)


def test_exact_retry_after_a_failed_remote_publish_never_disturbs_prior_success(tmp_path: Path, capsys: Any) -> None:
    manifest = tmp_path / "manifest.json"
    start, end = _write_manifest(manifest, date(2025, 5, 5), 20)
    date_b = end + timedelta(days=1)
    db = tmp_path / "work" / "market.db"
    raw_root = tmp_path / "work" / "raw"
    payload_a = _run(db, raw_root, ["backfill", "--start", start.isoformat(), "--end", end.isoformat(), "--manifest", str(manifest)], capsys)
    dataset_a = payload_a["publication"]["dataset_id"]

    store = _FakeObjectStore()
    upload_a = build_and_upload_checkpoint_archive(db_path=db, raw_root=raw_root, client=store, deadline_seconds=30)
    promote_latest_success(store, upload_a.manifest, remote_active_dataset_id=dataset_a)
    previously_verified_success = load_latest_success(store)

    # Exact retry of the very same local state: archiving again must be
    # idempotent (identical manifest) and must reuse already-uploaded,
    # content-addressed objects instead of blindly re-uploading everything.
    puts_before = len(store.put_calls)
    upload_retry = build_and_upload_checkpoint_archive(db_path=db, raw_root=raw_root, client=store, deadline_seconds=30)
    # `created_at` legitimately differs between two build calls; the
    # content that actually identifies the archive (dataset, inputs, the
    # exact set of immutable raw triples, and the SQLite backup's own
    # checksum) must be byte-for-byte identical on an exact retry.
    assert upload_retry.manifest.dataset_id == upload_a.manifest.dataset_id
    assert upload_retry.manifest.input_manifest_hash == upload_a.manifest.input_manifest_hash
    assert upload_retry.manifest.raw_triples == upload_a.manifest.raw_triples
    assert upload_retry.manifest.sqlite_backup == upload_a.manifest.sqlite_backup
    puts_during_retry = len(store.put_calls) - puts_before
    assert puts_during_retry == 1, "only the manifest (which embeds a fresh created_at) should be re-PUT on an exact retry"

    # A second local candidate (dataset B) is built and archived, but its
    # remote D1/R2 publication fails -- this must never touch the pointer.
    manifest2 = tmp_path / "manifest2.json"
    _write_manifest(manifest2, start, 20)
    with open(manifest2, "r+", encoding="utf-8") as handle:
        data = json.loads(handle.read())
        data["artifacts"].append(_artifact_entry(date_b, 20))
        handle.seek(0)
        handle.write(json.dumps(data))
        handle.truncate()
    payload_b = _run(db, raw_root, ["daily", "--date", date_b.isoformat(), "--manifest", str(manifest2)], capsys)
    dataset_b = payload_b["publication"]["dataset_id"]
    upload_b = build_and_upload_checkpoint_archive(db_path=db, raw_root=raw_root, client=store, deadline_seconds=30)
    assert upload_b.manifest.dataset_id == dataset_b

    # The remote publish for dataset B is rejected (its active dataset id
    # never actually became "dataset_b" in production) -- a recoverable
    # mismatch must reconcile by dataset ID rather than blindly duplicating
    # publication, and it must never advance the success pointer.
    with pytest.raises(CheckpointArchiveError):
        promote_latest_success(store, upload_b.manifest, remote_active_dataset_id=dataset_a)

    failed_remote_attempt_latest_success = load_latest_success(store)
    assert failed_remote_attempt_latest_success.dataset_id == previously_verified_success.dataset_id
    assert failed_remote_attempt_latest_success.manifest_sha256 == previously_verified_success.manifest_sha256

    # Once D1 genuinely confirms dataset B, promoting is idempotent-safe.
    promote_latest_success(store, upload_b.manifest, remote_active_dataset_id=dataset_b)
    reconciled = load_latest_success(store)
    assert reconciled.dataset_id == dataset_b
    # And promoting the exact same, already-current dataset again (a retried
    # verification step) must not raise or duplicate anything.
    promote_latest_success(store, upload_b.manifest, remote_active_dataset_id=dataset_b)


def test_restored_raw_artifacts_preserve_exact_source_provenance(tmp_path: Path, capsys: Any) -> None:
    manifest = tmp_path / "manifest.json"
    start, end = _write_manifest(manifest, date(2025, 6, 2), 15)
    db = tmp_path / "work" / "market.db"
    raw_root = tmp_path / "work" / "raw"
    payload = _run(db, raw_root, ["backfill", "--start", start.isoformat(), "--end", end.isoformat(), "--manifest", str(manifest)], capsys)
    dataset_id = payload["publication"]["dataset_id"]

    from market_pipeline.storage.raw_store import LocalRawStore

    original_store = LocalRawStore(raw_root)
    original_bodies = {key: original_store.get(key) for key in original_store.list()}
    assert original_bodies

    store = _FakeObjectStore()
    upload = build_and_upload_checkpoint_archive(db_path=db, raw_root=raw_root, client=store, deadline_seconds=30)
    promote_latest_success(store, upload.manifest, remote_active_dataset_id=dataset_id)

    shutil.rmtree(tmp_path / "work")
    restored_raw = tmp_path / "restored-raw"
    restore_checkpoint_archive(store, db_path=tmp_path / "restored" / "market.db", raw_root=restored_raw, deadline_seconds=30)

    restored_store = LocalRawStore(restored_raw)
    assert sorted(restored_store.list()) == sorted(original_bodies)
    for key, body in original_bodies.items():
        assert restored_store.get(key) == body


def test_cache_hit_still_validates_local_identity_against_the_authoritative_manifest(tmp_path: Path, capsys: Any) -> None:
    manifest = tmp_path / "manifest.json"
    start, end = _write_manifest(manifest, date(2025, 7, 7), 15)
    db = tmp_path / "work" / "market.db"
    raw_root = tmp_path / "work" / "raw"
    payload = _run(db, raw_root, ["backfill", "--start", start.isoformat(), "--end", end.isoformat(), "--manifest", str(manifest)], capsys)
    dataset_id = payload["publication"]["dataset_id"]

    store = _FakeObjectStore()
    upload = build_and_upload_checkpoint_archive(db_path=db, raw_root=raw_root, client=store, deadline_seconds=30)
    promote_latest_success(store, upload.manifest, remote_active_dataset_id=dataset_id)

    # A "cache hit": the local database is present and matches.
    result = check_local_identity_against_manifest(store, db_path=db)
    assert result.matches is True
    assert result.local_dataset_id == dataset_id

    # A stale/corrupted cache entry from an unrelated older dataset must be
    # detected even though the local files are present ("cache hit").
    stale_db = tmp_path / "stale" / "market.db"
    stale_manifest = tmp_path / "stale-manifest.json"
    stale_start, stale_end = _write_manifest(stale_manifest, date(2020, 1, 1), 15)
    _run(stale_db, tmp_path / "stale" / "raw", ["backfill", "--start", stale_start.isoformat(), "--end", stale_end.isoformat(), "--manifest", str(stale_manifest)], capsys)
    stale_result = check_local_identity_against_manifest(store, db_path=stale_db)
    assert stale_result.matches is False
    assert stale_result.remote_dataset_id == dataset_id


def test_checkpoint_archive_ledger_entry_reports_attempted_operation_counts(tmp_path: Path, capsys: Any) -> None:
    manifest = tmp_path / "manifest.json"
    start, end = _write_manifest(manifest, date(2025, 8, 8), 10)
    db = tmp_path / "work" / "market.db"
    raw_root = tmp_path / "work" / "raw"
    _run(db, raw_root, ["backfill", "--start", start.isoformat(), "--end", end.isoformat(), "--manifest", str(manifest)], capsys)

    store = _FakeObjectStore()
    upload = build_and_upload_checkpoint_archive(db_path=db, raw_root=raw_root, client=store, deadline_seconds=30)
    entry = checkpoint_archive_ledger_entry(upload)
    assert entry.r2_class_a_operations == upload.put_attempts
    assert entry.r2_class_b_operations == upload.get_attempts
    assert entry.r2_bytes == upload.verified_bytes
    assert entry.d1_mutations == 0
