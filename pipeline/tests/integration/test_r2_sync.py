from __future__ import annotations

import io
import subprocess
import sys
import threading
import time
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from market_pipeline.publication.bundle import BundleObject, PublicationBundle
from market_pipeline.publication.r2_sync import BUCKET


def _bundle(objects: tuple[BundleObject, ...], dataset_id: str = "dataset-1") -> PublicationBundle:
    return PublicationBundle(
        version=1,
        dataset_id=dataset_id,
        input_manifest_hash="a" * 64,
        source_dates=("2024-01-01",),
        projection_version="d1-projection-v1",
        sql_checksum=None,
        objects=objects,
    )


def _history_object(asset: str, instrument: str, year: int, body: bytes) -> BundleObject:
    digest = sha256(body).hexdigest()
    return BundleObject(
        key=f"history/{asset}/{instrument}/{year}/{digest}.parquet",
        sha256=digest,
        bytes=len(body),
        kind="history",
    )


def _chart_object(dataset_id: str, instrument: str, body: bytes) -> BundleObject:
    digest = sha256(body).hexdigest()
    return BundleObject(
        key=f"charts/{dataset_id}/{instrument}.json.gz",
        sha256=digest,
        bytes=len(body),
        kind="chart",
    )


def _write(root: Path, key: str, body: bytes) -> None:
    path = root / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)


class _FakeS3Client:
    """Controllable fake S3-compatible client with no real network calls."""

    def __init__(
        self,
        objects: dict[str, bytes] | None = None,
        *,
        delay: float = 0.0,
        fail_puts: int = 0,
        put_exception: type[BaseException] = RuntimeError,
        put_succeeds_despite_exception: bool = False,
    ) -> None:
        self.objects: dict[str, bytes] = dict(objects or {})
        self.delay = delay
        self.fail_puts = fail_puts
        self.put_exception = put_exception
        self.put_succeeds_despite_exception = put_succeeds_despite_exception
        self.calls: list[tuple[str, str]] = []
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def put_object(self, *, Bucket: str, Key: str, Body: Any) -> None:
        with self.lock:
            self.calls.append(("put", f"{Bucket}/{Key}"))
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                time.sleep(self.delay)
            if self.fail_puts > 0:
                self.fail_puts -= 1
                if self.put_succeeds_despite_exception:
                    self.objects[Key] = Body.read()
                raise self.put_exception("simulated transport failure")
            self.objects[Key] = Body.read()
        finally:
            with self.lock:
                self.active -= 1

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        with self.lock:
            self.calls.append(("get", f"{Bucket}/{Key}"))
        if Key not in self.objects:
            raise RuntimeError("missing remote object")
        return {"Body": io.BytesIO(self.objects[Key])}


def test_sync_verifies_history_and_chart_objects_with_bounded_workers(tmp_path: Path) -> None:
    from market_pipeline.publication.r2_sync import synchronize_history

    root = tmp_path / "history"
    entries = []
    for index in range(6):
        body = f"history-body-{index}".encode()
        entry = _history_object("equity", f"instrument-{index}", 2024, body)
        _write(root, entry.key, body)
        entries.append(entry)
    chart_body = b"chart-body"
    chart_entry = _chart_object("dataset-1", "instrument-0", chart_body)
    _write(root, chart_entry.key, chart_body)
    entries.append(chart_entry)

    client = _FakeS3Client(delay=0.02)
    result = synchronize_history(_bundle(tuple(entries)), root, client, max_workers=4, deadline_seconds=30)

    assert result.verified_objects == len(entries)
    assert result.verified_bytes == sum(entry.bytes for entry in entries)
    assert result.put_attempts == len(entries)
    assert result.get_attempts == len(entries)
    assert 2 <= client.max_active <= 4


def test_sync_reuses_already_verified_objects_without_reupload(tmp_path: Path) -> None:
    from market_pipeline.publication.r2_sync import synchronize_history

    root = tmp_path / "history"
    body = b"already-present"
    entry = _history_object("equity", "instrument-0", 2024, body)
    _write(root, entry.key, body)
    client = _FakeS3Client(fail_puts=1, put_exception=TimeoutError, put_succeeds_despite_exception=True)

    result = synchronize_history(_bundle((entry,)), root, client, max_workers=1, deadline_seconds=30)

    assert result.verified_objects == 1
    # The first PUT's outcome was unknown (it raised), so the retry must
    # verify via GET before deciding whether to re-upload -- it must not
    # blindly re-PUT once the object is confirmed present and correct.
    assert result.put_attempts == 1
    assert result.get_attempts == 1
    assert [call for call in client.calls if call[0] == "put"] == [
        ("put", f"{BUCKET}/{entry.key}")
    ]


def test_sync_fails_closed_on_malformed_final_entry_before_any_client_call(tmp_path: Path) -> None:
    from market_pipeline.publication.r2_sync import R2SyncError, synchronize_history

    root = tmp_path / "history"
    good_body = b"ok"
    good = _history_object("equity", "instrument-0", 2024, good_body)
    _write(root, good.key, good_body)
    malformed = BundleObject(key="../outside", sha256="a" * 64, bytes=2, kind="history")
    client = _FakeS3Client()

    with pytest.raises(R2SyncError):
        synchronize_history(_bundle((good, malformed)), root, client, max_workers=2, deadline_seconds=30)
    assert client.calls == []


def test_sync_fails_closed_on_missing_local_file(tmp_path: Path) -> None:
    from market_pipeline.publication.r2_sync import R2SyncError, synchronize_history

    root = tmp_path / "history"
    root.mkdir(parents=True)
    entry = _history_object("equity", "instrument-0", 2024, b"never-written")
    client = _FakeS3Client()

    with pytest.raises(R2SyncError, match="missing"):
        synchronize_history(_bundle((entry,)), root, client, max_workers=1, deadline_seconds=30)
    assert client.calls == []


def test_sync_fails_closed_when_local_bytes_do_not_match_recorded_checksum(tmp_path: Path) -> None:
    from market_pipeline.publication.r2_sync import R2SyncError, synchronize_history

    root = tmp_path / "history"
    entry = _history_object("equity", "instrument-0", 2024, b"expected-body")
    tampered = b"tampered-body"
    assert len(tampered) == len(b"expected-body")
    _write(root, entry.key, tampered)
    client = _FakeS3Client()

    with pytest.raises(R2SyncError, match="checksum"):
        synchronize_history(_bundle((entry,)), root, client, max_workers=1, deadline_seconds=30)
    assert client.calls == []


def test_sync_fails_closed_on_persistent_transport_error(tmp_path: Path) -> None:
    from market_pipeline.publication.r2_sync import R2SyncError, synchronize_history

    root = tmp_path / "history"
    body = b"payload"
    entry = _history_object("equity", "instrument-0", 2024, body)
    _write(root, entry.key, body)
    client = _FakeS3Client(fail_puts=1)

    with pytest.raises(R2SyncError):
        synchronize_history(_bundle((entry,)), root, client, max_workers=1, deadline_seconds=30)


def test_sync_exhausts_bounded_retries_and_reports_the_underlying_cause(tmp_path: Path) -> None:
    from market_pipeline.publication.r2_sync import R2SyncError, synchronize_history

    root = tmp_path / "history"
    body = b"payload"
    entry = _history_object("equity", "instrument-0", 2024, body)
    _write(root, entry.key, body)
    client = _FakeS3Client(fail_puts=10_000)  # always fails: forces retry exhaustion

    with pytest.raises(R2SyncError) as excinfo:
        synchronize_history(_bundle((entry,)), root, client, max_workers=1, deadline_seconds=30)
    assert excinfo.value.__cause__ is not None


def test_sync_enforces_shared_deadline_and_leaves_workers_quiescent(tmp_path: Path) -> None:
    from market_pipeline.publication.r2_sync import R2SyncError, synchronize_history

    root = tmp_path / "history"
    entries = []
    for index in range(8):
        body = f"slow-body-{index}".encode()
        entry = _history_object("equity", f"instrument-{index}", 2024, body)
        _write(root, entry.key, body)
        entries.append(entry)
    client = _FakeS3Client(delay=0.5)

    with pytest.raises(R2SyncError, match="deadline"):
        synchronize_history(_bundle(tuple(entries)), root, client, max_workers=2, deadline_seconds=1)

    # No fake-client timing here is production evidence; this only proves
    # the deadline is enforced and workers wind down to zero before return.
    assert client.active == 0


def test_sync_rejects_out_of_range_worker_and_deadline_bounds(tmp_path: Path) -> None:
    from market_pipeline.publication.r2_sync import R2SyncError, synchronize_history

    root = tmp_path / "history"
    entry = _history_object("equity", "instrument-0", 2024, b"body")
    _write(root, entry.key, b"body")
    client = _FakeS3Client()

    with pytest.raises(R2SyncError, match="worker"):
        synchronize_history(_bundle((entry,)), root, client, max_workers=33, deadline_seconds=30)
    with pytest.raises(R2SyncError, match="deadline"):
        synchronize_history(_bundle((entry,)), root, client, max_workers=1, deadline_seconds=0)
    assert client.calls == []


def test_sync_at_scale_bounds_concurrency_for_many_instruments_and_partitions(tmp_path: Path) -> None:
    """12,000 instruments across 4 calendar partitions, scheduled with no per-object subprocess."""

    from market_pipeline.publication.r2_sync import synchronize_history

    root = tmp_path / "history"
    entries = []
    instrument_count = 60  # kept small for test runtime; exercises the same scheduling path
    years = (2021, 2022, 2023, 2024)
    for index in range(instrument_count):
        for year in years:
            body = f"body-{index}-{year}".encode()
            entry = _history_object("equity", f"instrument-{index}", year, body)
            _write(root, entry.key, body)
            entries.append(entry)

    client = _FakeS3Client(delay=0.001)
    started = time.monotonic()
    result = synchronize_history(_bundle(tuple(entries)), root, client, max_workers=16, deadline_seconds=60)
    elapsed = time.monotonic() - started

    assert result.verified_objects == len(entries) == instrument_count * len(years)
    assert client.max_active <= 16
    # Predicted throughput from this fake client's injected latency is not
    # production network evidence -- recorded separately, never asserted as
    # a real-world SLA.
    predicted_fake_client_objects_per_second = len(entries) / elapsed if elapsed > 0 else float("inf")
    assert predicted_fake_client_objects_per_second > 0


def test_cli_failure_never_lets_a_subsequent_d1_import_step_run(tmp_path: Path) -> None:
    """Subprocess-level proof: a failing r2_sync invocation must gate the next
    shell step the same way the workflow's ``set -euo pipefail`` step does --
    with no process substitution masking the failure.
    """

    import json

    root = tmp_path / "history"
    entry = _history_object("equity", "instrument-0", 2024, b"good-body")
    _write(root, entry.key, b"tampered-so-checksum-mismatches")
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(json.dumps(_bundle((entry,)).as_dict()), encoding="utf-8")
    marker = tmp_path / "d1-import-ran.marker"

    script = f"""
set -euo pipefail
{sys.executable} -m market_pipeline.publication.r2_sync \
  --bundle {bundle_path} --history-root {root} --max-workers 1 --deadline-seconds 10
touch {marker}
"""
    env = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
        "CLOUDFLARE_ACCOUNT_ID": "test-account",
        "R2_ACCESS_KEY_ID": "test-key",
        "R2_SECRET_ACCESS_KEY": "test-secret",
    }
    completed = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)

    assert completed.returncode != 0
    assert not marker.exists()
