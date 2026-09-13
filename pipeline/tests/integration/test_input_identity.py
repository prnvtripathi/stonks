"""Dataset identity must cover every raw input and calculation revision."""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from market_pipeline.cli import main
from market_pipeline.jobs import publish as publish_job
from market_pipeline.publication.input_manifest import dataset_fingerprint
from market_pipeline.storage.history_store import LocalHistoryStore


def _artifact(effective: date, nav: float) -> dict[str, Any]:
    body = (
        "Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Net Asset Value;Date\n\n"
        "Example Asset Management Mutual Fund\n\n"
        "Open Ended Schemes(Equity Scheme - Large Cap Fund)\n"
        f"119551;INF11955101AA1;INF11955101AB9;Alpha Bluechip Fund - Direct Plan - Growth;{nav};{effective.strftime('%d-%b-%Y')}\n"
        f"119552;INF11955201AA1;INF11955201AB9;Beta Bluechip Fund - Direct Plan - Growth;{nav * 2};{effective.strftime('%d-%b-%Y')}\n"
    ).encode("utf-8")
    return {
        "artifact": {
            "source_id": "amfi-nav",
            "source_url": "https://www.amfiindia.com/spages/NAVAll.txt",
            "retrieved_at": datetime.now(UTC).isoformat(),
            "effective_date": effective.isoformat(),
            "checksum": hashlib.sha256(body).hexdigest(),
            "adapter_version": "v1",
            "terms_url": "https://www.amfiindia.com/terms-and-conditions",
            "filename": "NAVAll.txt",
        },
        "body_base64": base64.b64encode(body).decode("ascii"),
    }


def _run(
    tmp_path: Path, capsys: Any, command: list[str], artifacts: list[dict[str, Any]], db: Path
) -> tuple[int, dict[str, Any]]:
    manifest = tmp_path / f"manifest-{len(artifacts)}-{command[0]}.json"
    manifest.write_text(json.dumps({"artifacts": artifacts}), encoding="utf-8")
    exit_code = main([
        "--db", str(db), "--raw-store", str(tmp_path / "raw"), "--history-store", str(tmp_path / "history"),
        *command, "--manifest", str(manifest),
    ])
    return exit_code, json.loads(capsys.readouterr().out)


def test_dataset_fingerprint_is_order_independent_and_tracks_raw_and_formula_inputs() -> None:
    inputs = [
        {"source_id": "amfi-nav", "effective_date": "2026-09-01", "artifact_id": "a", "checksum": "a" * 64, "adapter_version": "v1", "raw_object_key": "raw/a"},
        {"source_id": "amfi-nav", "effective_date": "2026-09-08", "artifact_id": "b", "checksum": "b" * 64, "adapter_version": "v1", "raw_object_key": "raw/b"},
    ]
    versions = {"normalization": "amfi-v1", "analytics": "returns-v1", "projection": "d1-v1"}

    assert dataset_fingerprint(inputs, versions) == dataset_fingerprint(list(reversed(inputs)), versions)
    assert dataset_fingerprint(inputs, versions) != dataset_fingerprint(inputs[:1], versions)
    assert dataset_fingerprint(inputs, versions) != dataset_fingerprint(inputs, {**versions, "analytics": "returns-v2"})


def test_dataset_fingerprint_ignores_execution_metadata() -> None:
    input_record = {
        "source_id": "amfi-nav", "effective_date": "2026-09-08", "artifact_id": "b",
        "checksum": "b" * 64, "adapter_version": "v1", "raw_object_key": "raw/b",
    }
    versions = {"normalization": "amfi-v1", "analytics": "returns-v1", "projection": "d1-v1"}

    assert dataset_fingerprint([input_record], versions) == dataset_fingerprint([
        {**input_record, "retrieved_at": "2026-09-08T09:00:00Z", "execution_attempt_id": "retry-2"}
    ], versions)


def test_changed_production_projection_version_creates_a_new_dataset(tmp_path: Path, capsys: Any, monkeypatch: Any) -> None:
    effective = date(2026, 9, 8)
    db = tmp_path / "market.db"
    first_exit, first = _run(tmp_path, capsys, ["daily", "--date", effective.isoformat()], [_artifact(effective, 108.0)], db)

    monkeypatch.setattr(publish_job, "PROJECTION_VERSION", "d1-projection-v2")
    changed_exit, changed = _run(tmp_path, capsys, ["daily", "--date", effective.isoformat()], [_artifact(effective, 108.0)], db)

    assert first_exit == 0, first
    assert changed_exit == 0, changed
    assert changed["publication"]["dataset_id"] != first["publication"]["dataset_id"]
    connection = sqlite3.connect(db)
    try:
        assert connection.execute("SELECT adapter_version FROM backfill_checkpoints").fetchone()[0] == "v1"
    finally:
        connection.close()


def test_historical_backfill_changes_dataset_identity_and_metric_lineage(tmp_path: Path, capsys: Any) -> None:
    latest = date(2026, 9, 8)
    db = tmp_path / "market.db"
    latest_exit, latest_only = _run(tmp_path, capsys, ["daily", "--date", latest.isoformat()], [_artifact(latest, 108.0)], db)
    historical = [_artifact(latest - timedelta(days=offset), 108.0 - offset) for offset in range(1, 6)]
    updated_exit, updated = _run(
        tmp_path, capsys,
        ["backfill", "--start", (latest - timedelta(days=5)).isoformat(), "--end", latest.isoformat()],
        historical + [_artifact(latest, 108.0)], db,
    )

    assert latest_exit == 0, latest_only
    assert updated_exit == 0, updated
    assert updated["publication"]["dataset_id"] != latest_only["publication"]["dataset_id"]
    connection = sqlite3.connect(db)
    try:
        metadata = json.loads(connection.execute(
            "SELECT metadata_json FROM datasets WHERE dataset_id=?", (updated["publication"]["dataset_id"],)
        ).fetchone()[0])
        one_week = connection.execute(
            "SELECT value FROM latest_metrics WHERE dataset_id=? AND metric='return_1w' LIMIT 1",
            (updated["publication"]["dataset_id"],),
        ).fetchone()[0]
    finally:
        connection.close()
    assert one_week is not None
    # R13/F16: the full manifest body is no longer embedded in
    # `metadata_json` -- only its hash is. Dataset identity is unaffected
    # (still R02's `dataset_fingerprint`); the full lineage record is
    # resolved through that hash via the content-addressed manifest object.
    assert "input_manifest" not in metadata
    assert metadata["input_manifest_sha256"]
    store = LocalHistoryStore(tmp_path / "history")
    manifest_body = store.read_manifest(metadata["input_manifest_sha256"])
    assert len(manifest_body["inputs"]) == 6
    assert {item["effective_date"] for item in manifest_body["inputs"]} == {
        (latest - timedelta(days=offset)).isoformat() for offset in range(6)
    }
