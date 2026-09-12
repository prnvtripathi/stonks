"""End-to-end proof that a manifest reaches a promoted D1 dataset.

This is the seam Task 7 named but never wired: fetch/checkpoint -> immutable
raw storage -> normalization -> analytics -> ``D1Publisher.stage``/``promote``.
Every assertion below reads the *promoted* dataset through the active pointer,
so a run that merely computes numbers in memory cannot satisfy it.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from market_pipeline.cli import main

TERMS_URL = "https://www.amfiindia.com/terms-and-conditions"
SOURCE_URL = "https://www.amfiindia.com/spages/NAVAll.txt"
SCHEMES = (
    ("119551", "Alpha Bluechip Fund - Direct Plan - Growth", 100.0, 1.004),
    ("119552", "Beta Bluechip Fund - Direct Plan - Growth", 250.0, 1.002),
    ("119553", "Gamma Bluechip Fund - Direct Plan - Growth", 75.0, 0.999),
)


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


def _active_rows(db: Path, table: str, columns: str) -> list[tuple[Any, ...]]:
    connection = sqlite3.connect(db)
    try:
        return connection.execute(
            f"SELECT {columns} FROM {table} WHERE dataset_id = "
            "(SELECT dataset_id FROM active_dataset WHERE singleton = 1)"
        ).fetchall()
    finally:
        connection.close()


def test_manifest_flows_from_raw_artifact_to_a_promoted_queryable_dataset(tmp_path: Path, capsys: Any) -> None:
    db = tmp_path / "market.db"
    raw_root = tmp_path / "raw"
    manifest = tmp_path / "manifest.json"
    # 300 calendar sessions is enough history for the full twelve-month
    # lookback (252 sessions), so returns/risk/momentum are real values.
    start, end = _write_manifest(manifest, date(2025, 11, 3), 300)

    exit_code = main([
        "--db", str(db), "--raw-store", str(raw_root),
        "backfill", "--start", start.isoformat(), "--end", end.isoformat(),
        "--manifest", str(manifest),
    ])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0, payload
    assert payload["safe_to_promote"] is True
    assert payload["publication"]["promoted"] is True
    dataset_id = payload["publication"]["dataset_id"]
    assert dataset_id

    # Raw artifacts were persisted immutably with their provenance sidecars.
    assert (raw_root / "raw" / "amfi-nav").exists()
    assert len(list((raw_root / "raw" / "amfi-nav").glob("*/*/NAVAll.txt"))) == 300

    # The active pointer moved to this dataset and a real query returns rows.
    connection = sqlite3.connect(db)
    try:
        assert connection.execute("SELECT dataset_id FROM active_dataset").fetchone()[0] == dataset_id
        assert connection.execute("SELECT status FROM datasets WHERE dataset_id=?", (dataset_id,)).fetchone()[0] == "active"
    finally:
        connection.close()

    instruments = _active_rows(db, "instruments", "instrument_id, provider, asset_class, symbol")
    assert len(instruments) == len(SCHEMES)
    assert {row[1] for row in instruments} == {"amfi"}
    assert {row[2] for row in instruments} == {"mutual_fund"}
    assert {row[3] for row in instruments} == {code for code, _, _, _ in SCHEMES}

    metrics = {
        (row[0], row[1]): (row[2], row[3])
        for row in _active_rows(db, "latest_metrics", "instrument_id, metric, value, state")
    }
    assert metrics, "promoted dataset must expose real metric rows"
    published_metrics = {metric for _, metric in metrics}
    for expected in ("return_1d", "return_12m", "volatility_1y", "max_drawdown_1y", "benchmark_rs_12m", "momentum_score"):
        assert expected in published_metrics, expected

    instrument_ids = {row[0] for row in instruments}
    for instrument_id in instrument_ids:
        value, state = metrics[(instrument_id, "return_12m")]
        assert state == "present"
        assert isinstance(value, float)
    # Alpha compounds fastest, so its twelve-month return must beat Gamma's.
    by_symbol = {row[3]: row[0] for row in instruments}
    assert metrics[(by_symbol["119551"], "return_12m")][0] > metrics[(by_symbol["119553"], "return_12m")][0]
    # Analytics use the Task 8 fractional convention, never percentage points.
    assert metrics[(by_symbol["119553"], "return_12m")][0] < 0

    benchmark_rows = _active_rows(db, "latest_metrics", "metric, state, metadata_json")
    benchmark_rows = [row for row in benchmark_rows if row[0].startswith("benchmark_rs_")]
    assert len(benchmark_rows) == len(SCHEMES) * 3
    assert all(row[1] == "missing" for row in benchmark_rows)
    assert all(json.loads(row[2])["reason"] == "official benchmark mapping is unavailable" for row in benchmark_rows)

    # Momentum provenance was attached through the publication seam.
    momentum = [row for row in _active_rows(db, "latest_metrics", "instrument_id, metric, metadata_json") if row[1] == "momentum_score"]
    assert len(momentum) == len(SCHEMES)
    assert all(json.loads(row[2])["cohort"].startswith("mutual_fund:") for row in momentum)

    runs = _active_rows(db, "source_runs", "source_id, effective_date, status")
    assert runs == [("amfi-nav", end.isoformat(), "complete")]

    # Yearly history and the gzip chart object the Worker serves were written.
    history_root = tmp_path / "history"
    assert payload["publication"]["history_objects"] > 0
    assert list(history_root.glob("history/mutual_fund/*/*/*.parquet"))
    assert len(list(history_root.glob(f"charts/{dataset_id}/*.json.gz"))) == len(SCHEMES)


def test_blocked_run_never_replaces_the_last_known_good_dataset(tmp_path: Path, capsys: Any) -> None:
    db = tmp_path / "market.db"
    raw_root = tmp_path / "raw"
    good_manifest = tmp_path / "good.json"
    start, end = _write_manifest(good_manifest, date(2025, 11, 3), 300)
    assert main([
        "--db", str(db), "--raw-store", str(raw_root),
        "backfill", "--start", start.isoformat(), "--end", end.isoformat(),
        "--manifest", str(good_manifest),
    ]) == 0
    good = json.loads(capsys.readouterr().out)["publication"]["dataset_id"]

    # A later run whose source is entirely absent must be blocked before any
    # promotion happens, leaving the previous dataset active and queryable.
    empty_manifest = tmp_path / "empty.json"
    empty_manifest.write_text(json.dumps({"artifacts": []}), encoding="utf-8")
    exit_code = main([
        "--db", str(db), "--raw-store", str(raw_root),
        "daily", "--date", (end + timedelta(days=1)).isoformat(), "--manifest", str(empty_manifest),
    ])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 3
    assert payload["safe_to_promote"] is False
    assert payload["publication"]["promoted"] is False
    assert "blocked" in (payload["publication"]["reason"] or "")

    connection = sqlite3.connect(db)
    try:
        assert connection.execute("SELECT dataset_id FROM active_dataset").fetchone()[0] == good
        assert connection.execute(
            "SELECT COUNT(*) FROM latest_metrics WHERE dataset_id = ?", (good,)
        ).fetchone()[0] > 0
    finally:
        connection.close()
