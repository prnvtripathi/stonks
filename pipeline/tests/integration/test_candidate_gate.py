"""Regression coverage for the candidate-data publication gate."""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from market_pipeline import cli
from market_pipeline.cli import main
from market_pipeline.jobs.backfill import BackfillResult
from market_pipeline.jobs.publish import DatasetBuild

TERMS_URL = "https://www.amfiindia.com/terms-and-conditions"
SOURCE_URL = "https://www.amfiindia.com/spages/NAVAll.txt"
SCHEMES = (
    ("119551", "Alpha Bluechip Fund - Direct Plan - Growth", 100.0),
    ("119552", "Beta Bluechip Fund - Direct Plan - Growth", 250.0),
    ("119553", "Gamma Bluechip Fund - Direct Plan - Growth", 75.0),
)


def _nav_report(effective: date, scheme_count: int = 3, *, malformed: bool = False) -> bytes:
    if malformed:
        return b"this is not an AMFI NAV report\n"
    lines = [
        "Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Net Asset Value;Date",
        "",
        "Alpha Asset Management Mutual Fund",
        "",
        "Open Ended Schemes(Equity Scheme - Large Cap Fund)",
    ]
    for code, name, nav in SCHEMES[:scheme_count]:
        lines.append(f"{code};INF{code}01AA1;INF{code}01AB9;{name};{nav};{effective.strftime('%d-%b-%Y')}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _artifact(effective: date, scheme_count: int = 3, *, malformed: bool = False) -> dict[str, Any]:
    body = _nav_report(effective, scheme_count, malformed=malformed)
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
        "body_base64": base64.b64encode(body).decode("ascii"),
    }


def _run_daily(
    tmp_path: Path, capsys: Any, effective: date, artifacts: list[dict[str, Any]], *, db: Path | None = None
) -> tuple[int, dict[str, Any], Path]:
    database = db or tmp_path / "market.db"
    manifest = tmp_path / f"manifest-{effective.isoformat()}.json"
    manifest.write_text(json.dumps({"artifacts": artifacts}), encoding="utf-8")
    exit_code = main([
        "--db", str(database), "--raw-store", str(tmp_path / "raw"), "--history-store", str(tmp_path / "history"),
        "daily", "--date", effective.isoformat(), "--manifest", str(manifest),
    ])
    return exit_code, json.loads(capsys.readouterr().out), database


def _active_dataset_id(db: Path) -> str | None:
    connection = sqlite3.connect(db)
    try:
        row = connection.execute("SELECT dataset_id FROM active_dataset WHERE singleton = 1").fetchone()
        return None if row is None else str(row[0])
    finally:
        connection.close()


def test_identical_retry_reuses_the_same_successful_candidate(tmp_path: Path, capsys: Any) -> None:
    effective = date(2026, 9, 1)
    first_exit, first, db = _run_daily(tmp_path, capsys, effective, [_artifact(effective)])
    retry_exit, retry, _ = _run_daily(tmp_path, capsys, effective, [_artifact(effective)], db=db)

    assert first_exit == 0, first
    assert retry_exit == 0, retry
    assert retry["publication"]["dataset_id"] == first["publication"]["dataset_id"]
    assert _active_dataset_id(db) == first["publication"]["dataset_id"]


def test_identical_retry_does_not_inflate_the_next_candidate_baseline(tmp_path: Path, capsys: Any) -> None:
    first_date = date(2026, 9, 1)
    first_exit, first, db = _run_daily(tmp_path, capsys, first_date, [_artifact(first_date)])
    retry_exit, retry, _ = _run_daily(tmp_path, capsys, first_date, [_artifact(first_date)], db=db)
    next_date = first_date + timedelta(days=1)
    next_exit, next_run, _ = _run_daily(tmp_path, capsys, next_date, [_artifact(next_date)], db=db)

    assert first_exit == 0, first
    assert retry_exit == 0, retry
    assert next_exit == 0, next_run
    assert next_run["reconciliation"]["previous_count"] == 3
    assert _active_dataset_id(db) == next_run["publication"]["dataset_id"]


def test_previous_count_cannot_mask_a_multisource_truncation(
    tmp_path: Path, capsys: Any, monkeypatch: Any
) -> None:
    db = tmp_path / "market.db"
    connection = sqlite3.connect(db)
    try:
        cli._ensure_run_history_table(connection)
        connection.executemany(
            "INSERT INTO published_candidate_coverage("
            "scope, source_id, expected_date, loaded_date, instrument_count, missing_ratios_json, dataset_id, published_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            [
                ('{"sources": ["amfi-nav", "nse-eod"]}', "amfi-nav", "2026-09-01", "2026-09-01", 100, "{}", "baseline", "2026-09-01T00:00:00Z"),
                ('{"sources": ["amfi-nav", "nse-eod"]}', "nse-eod", "2026-09-01", "2026-09-01", 100, "{}", "baseline", "2026-09-01T00:00:00Z"),
            ],
        )
        connection.commit()
    finally:
        connection.close()

    candidate = {
        "dataset_id": "candidate",
        "effective_date": "2026-09-02",
        "tables": {
            "sources": [
                {"source_id": "amfi-nav", "effective_date": "2026-09-02"},
                {"source_id": "nse-eod", "effective_date": "2026-09-02"},
            ],
            "instruments": (
                [{"provider": "amfi", "metadata": {"source_id": "amfi-nav"}} for _ in range(50)]
                + [{"provider": "nse", "metadata": {"source_id": "nse-eod"}} for _ in range(200)]
            ),
        },
    }
    monkeypatch.setattr(
        cli, "run_daily", lambda *args, **kwargs: BackfillResult(date(2026, 9, 2), date(2026, 9, 2), 0, 0)
    )
    monkeypatch.setattr(cli, "build_candidate", lambda *args, **kwargs: DatasetBuild(candidate, {}, {}, {}, ()))
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"artifacts": []}), encoding="utf-8")

    exit_code = cli.main([
        "--db", str(db), "daily", "--date", "2026-09-02", "--manifest", str(manifest),
        "--source", "amfi-nav", "--source", "nse-eod", "--previous-count", "200",
    ])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code != 0, payload
    assert any("source amfi-nav coverage" in reason for reason in payload["blocking_reasons"])


def test_instrument_truncation_blocks_and_preserves_the_active_dataset(tmp_path: Path, capsys: Any) -> None:
    first_date = date(2026, 9, 1)
    first_exit, first, db = _run_daily(tmp_path, capsys, first_date, [_artifact(first_date, scheme_count=3)])
    truncated_date = first_date + timedelta(days=1)
    truncated_exit, truncated, _ = _run_daily(
        tmp_path, capsys, truncated_date, [_artifact(truncated_date, scheme_count=1)], db=db
    )

    assert first_exit == 0, first
    assert truncated_exit != 0, truncated
    assert truncated["publication"]["promoted"] is False
    assert _active_dataset_id(db) == first["publication"]["dataset_id"]


def test_malformed_final_artifact_blocks_and_preserves_the_active_dataset(tmp_path: Path, capsys: Any) -> None:
    first_date = date(2026, 9, 1)
    first_exit, first, db = _run_daily(tmp_path, capsys, first_date, [_artifact(first_date)])
    malformed_date = first_date + timedelta(days=1)
    malformed_exit, malformed, _ = _run_daily(
        tmp_path, capsys, malformed_date, [_artifact(malformed_date, malformed=True)], db=db
    )

    assert first_exit == 0, first
    assert malformed_exit != 0, malformed
    assert malformed["publication"]["promoted"] is False
    assert _active_dataset_id(db) == first["publication"]["dataset_id"]


def test_malformed_intermediate_artifact_blocks_and_preserves_the_active_dataset(tmp_path: Path, capsys: Any) -> None:
    first_date = date(2026, 9, 1)
    first_exit, first, db = _run_daily(tmp_path, capsys, first_date, [_artifact(first_date)])
    intermediate = first_date + timedelta(days=1)
    final = intermediate + timedelta(days=1)
    manifest = tmp_path / "malformed-intermediate.json"
    manifest.write_text(json.dumps({"artifacts": [_artifact(intermediate, malformed=True), _artifact(final)]}), encoding="utf-8")
    exit_code = main([
        "--db", str(db), "--raw-store", str(tmp_path / "raw"), "--history-store", str(tmp_path / "history"),
        "backfill", "--start", intermediate.isoformat(), "--end", final.isoformat(), "--manifest", str(manifest),
    ])
    payload = json.loads(capsys.readouterr().out)

    assert first_exit == 0, first
    assert exit_code != 0, payload
    assert payload["publication"]["promoted"] is False
    assert _active_dataset_id(db) == first["publication"]["dataset_id"]


def test_expected_amfi_holiday_is_not_reported_as_missing_data(tmp_path: Path, capsys: Any) -> None:
    # Sunday is explicitly not an AMFI publication date.
    holiday = date(2026, 9, 6)
    exit_code, payload, _ = _run_daily(tmp_path, capsys, holiday, [])

    assert exit_code != 0, payload
    assert payload["reconciliation"]["candidate_coverage"] == [{
        "source_id": "amfi-nav",
        "expected_date": None,
        "loaded_date": None,
        "instrument_count": 0,
        "missing_ratios": {},
        "blocking_reasons": ["amfi-nav: no candidate artifact is available for the not-applicable date"],
    }]


def _run_with_corrupt_checkpoint(
    tmp_path: Path, capsys: Any, mutate: Any
) -> tuple[int, dict[str, Any], dict[str, Any], Path]:
    first_date = date(2026, 9, 1)
    first_exit, first, db = _run_daily(tmp_path, capsys, first_date, [_artifact(first_date)])
    assert first_exit == 0, first
    mutate(db, tmp_path)
    next_date = first_date + timedelta(days=1)
    exit_code, payload, _ = _run_daily(tmp_path, capsys, next_date, [_artifact(next_date)], db=db)
    return exit_code, payload, first, db


def test_corrupt_checkpoint_checksum_blocks_and_preserves_the_active_dataset(tmp_path: Path, capsys: Any) -> None:
    def corrupt_checksum(db: Path, _: Path) -> None:
        connection = sqlite3.connect(db)
        try:
            connection.execute("UPDATE backfill_checkpoints SET checksum = '0' * 64")
            connection.commit()
        finally:
            connection.close()

    exit_code, payload, first, db = _run_with_corrupt_checkpoint(tmp_path, capsys, corrupt_checksum)

    assert exit_code != 0, payload
    assert payload["publication"]["promoted"] is False
    assert _active_dataset_id(db) == first["publication"]["dataset_id"]


def test_absent_raw_object_blocks_and_preserves_the_active_dataset(tmp_path: Path, capsys: Any) -> None:
    def remove_raw_object(db: Path, root: Path) -> None:
        connection = sqlite3.connect(db)
        try:
            object_key = connection.execute("SELECT object_key FROM backfill_checkpoints").fetchone()[0]
        finally:
            connection.close()
        (root / "raw" / str(object_key)).unlink()

    exit_code, payload, first, db = _run_with_corrupt_checkpoint(tmp_path, capsys, remove_raw_object)

    assert exit_code != 0, payload
    assert payload["publication"]["promoted"] is False
    assert _active_dataset_id(db) == first["publication"]["dataset_id"]


def test_checkpoint_without_raw_object_key_blocks_and_preserves_the_active_dataset(tmp_path: Path, capsys: Any) -> None:
    def remove_object_key(db: Path, _: Path) -> None:
        connection = sqlite3.connect(db)
        try:
            connection.execute("UPDATE backfill_checkpoints SET object_key = NULL")
            connection.commit()
        finally:
            connection.close()

    exit_code, payload, first, db = _run_with_corrupt_checkpoint(tmp_path, capsys, remove_object_key)

    assert exit_code != 0, payload
    assert payload["publication"]["promoted"] is False
    assert _active_dataset_id(db) == first["publication"]["dataset_id"]
