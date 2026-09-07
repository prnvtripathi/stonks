from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from market_pipeline.cli import main


def test_daily_without_committed_artifact_input_is_nonzero(capsys: Any) -> None:
    result = main(["daily", "--date", "2026-09-01"])
    assert result != 0
    assert json.loads(capsys.readouterr().err)["error"] == "artifact input/provider is required"


def test_daily_with_empty_manifest_reports_source_failed_and_blocks_promotion(tmp_path: Path, capsys: Any) -> None:
    # A source missing for every requested date must be surfaced as a
    # per-source "failed" status by reconciliation -- not a hard CoverageError
    # abort that short-circuits before reconciliation ever runs (see
    # task-13-report.md Fix Round 1: strict_coverage is off in the CLI so the
    # reconcile()/per-source freshness checks are the actual blocking gate).
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"artifacts": []}))
    result = main(["--db", str(tmp_path / "market.db"), "daily", "--date", "2026-09-01", "--manifest", str(manifest)])
    payload = json.loads(capsys.readouterr().out)
    assert result == 3
    assert payload["safe_to_promote"] is False
    assert payload["reconciliation"]["source_freshness"] == [
        {"source_id": "amfi-nav", "status": "failed", "detail": "amfi-nav reported failure"}
    ]
    assert any("amfi-nav failed" in reason for reason in payload["blocking_reasons"])


def test_real_manifest_deserializes_and_runs_daily(tmp_path: Path, capsys: Any) -> None:
    body = b"official artifact"
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"artifacts": [{
        "artifact": {
            "source_id": "amfi-nav",
            "source_url": "https://www.amfiindia.com/spages/NAVAll.txt",
            "retrieved_at": datetime.now(UTC).isoformat(),
            "effective_date": "2026-09-01",
            "checksum": hashlib.sha256(body).hexdigest(),
            "adapter_version": "v1",
            "terms_url": "https://www.amfiindia.com/terms.html",
            "filename": "nav.txt",
        },
        "body_base64": base64.b64encode(body).decode(),
    }]}))
    result = main(["--db", str(tmp_path / "market.db"), "daily", "--date", "2026-09-01", "--manifest", str(manifest)])
    assert result == 0
    assert json.loads(capsys.readouterr().out)["completed"] == 1


def test_backfill_with_incomplete_manifest_reports_source_failed_and_blocks_promotion(tmp_path: Path, capsys: Any) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"artifacts": []}))
    result = main(["--db", str(tmp_path / "market.db"), "backfill", "--start", "2026-09-01", "--end", "2026-09-01", "--manifest", str(manifest)])
    payload = json.loads(capsys.readouterr().out)
    assert result == 3
    assert payload["safe_to_promote"] is False
    assert payload["reconciliation"]["source_freshness"] == [
        {"source_id": "amfi-nav", "status": "failed", "detail": "amfi-nav reported failure"}
    ]


def test_backfill_source_missing_only_some_dates_is_delayed_not_failed_and_does_not_block(
    tmp_path: Path, capsys: Any
) -> None:
    # Only one of the two requested dates has an artifact: this must surface
    # as "delayed" (partial lag), never "failed" (which is reserved for a
    # source missing on every requested date), and delayed alone must not
    # block promotion.
    body = b"official artifact"
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"artifacts": [{
        "artifact": {
            "source_id": "amfi-nav",
            "source_url": "https://www.amfiindia.com/spages/NAVAll.txt",
            "retrieved_at": datetime.now(UTC).isoformat(),
            "effective_date": "2026-09-01",
            "checksum": hashlib.sha256(body).hexdigest(),
            "adapter_version": "v1",
            "terms_url": "https://www.amfiindia.com/terms.html",
            "filename": "nav.txt",
        },
        "body_base64": base64.b64encode(body).decode(),
    }]}))
    result = main([
        "--db", str(tmp_path / "market.db"),
        "backfill", "--start", "2026-09-01", "--end", "2026-09-02",
        "--manifest", str(manifest),
    ])
    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["safe_to_promote"] is True
    assert payload["reconciliation"]["source_freshness"] == [
        {"source_id": "amfi-nav", "status": "delayed", "detail": "amfi-nav delayed by 1 day(s)"}
    ]


def test_explicit_sources_replace_default() -> None:
    from market_pipeline.cli import _parser

    args = _parser().parse_args(["backfill", "--source", "nse-eod"])
    assert args.sources == ["nse-eod"]


def test_successful_run_reports_publishable_reconciliation(tmp_path: Path, capsys: Any) -> None:
    body = b"official artifact"
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"artifacts": [{
        "artifact": {
            "source_id": "amfi-nav",
            "source_url": "https://www.amfiindia.com/spages/NAVAll.txt",
            "retrieved_at": datetime.now(UTC).isoformat(),
            "effective_date": "2026-09-01",
            "checksum": hashlib.sha256(body).hexdigest(),
            "adapter_version": "v1",
            "terms_url": "https://www.amfiindia.com/terms.html",
            "filename": "nav.txt",
        },
        "body_base64": base64.b64encode(body).decode(),
    }]}))
    result = main(["--db", str(tmp_path / "market.db"), "daily", "--date", "2026-09-01", "--manifest", str(manifest)])
    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["safe_to_promote"] is True
    assert payload["reconciliation"]["publishable"] is True
    assert payload["blocking_reasons"] == []


def test_coverage_drop_against_previous_count_blocks_promotion(tmp_path: Path, capsys: Any) -> None:
    body = b"official artifact"
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"artifacts": [{
        "artifact": {
            "source_id": "amfi-nav",
            "source_url": "https://www.amfiindia.com/spages/NAVAll.txt",
            "retrieved_at": datetime.now(UTC).isoformat(),
            "effective_date": "2026-09-01",
            "checksum": hashlib.sha256(body).hexdigest(),
            "adapter_version": "v1",
            "terms_url": "https://www.amfiindia.com/terms.html",
            "filename": "nav.txt",
        },
        "body_base64": base64.b64encode(body).decode(),
    }]}))
    result = main([
        "--db", str(tmp_path / "market.db"),
        "daily", "--date", "2026-09-01", "--manifest", str(manifest),
        "--previous-count", "2500",
    ])
    payload = json.loads(capsys.readouterr().out)
    assert result == 3
    assert payload["safe_to_promote"] is False
    assert payload["reconciliation"]["publishable"] is False
    assert any("coverage" in reason for reason in payload["blocking_reasons"])


def _manifest_with_one_artifact(tmp_path: Path, name: str, effective_date: str) -> Path:
    body = f"official artifact {name}".encode()
    manifest = tmp_path / name
    manifest.write_text(json.dumps({"artifacts": [{
        "artifact": {
            "source_id": "amfi-nav",
            "source_url": "https://www.amfiindia.com/spages/NAVAll.txt",
            "retrieved_at": datetime.now(UTC).isoformat(),
            "effective_date": effective_date,
            "checksum": hashlib.sha256(body).hexdigest(),
            "adapter_version": "v1",
            "terms_url": "https://www.amfiindia.com/terms.html",
            "filename": "nav.txt",
        },
        "body_base64": base64.b64encode(body).decode(),
    }]}))
    return manifest


def test_omitted_previous_count_uses_last_successful_run_from_same_db_not_self_compare(
    tmp_path: Path, capsys: Any
) -> None:
    # This is the scheduled-workflow scenario: no --previous-count is passed
    # (the cron trigger has no `inputs` context to source one from). Without
    # a real baseline, the CLI used to default previous_count to the
    # candidate's own completed count, so coverage_ratio was always 1.0 and a
    # genuine drop could never be detected. The CLI must instead persist each
    # safe-to-promote run's completed count in its own database and use the
    # most recent one as the default baseline for the next run against the
    # same db/source scope.
    db = tmp_path / "market.db"
    good_manifest = _manifest_with_one_artifact(tmp_path, "good.json", "2026-09-01")
    first = main(["--db", str(db), "daily", "--date", "2026-09-01", "--manifest", str(good_manifest)])
    first_payload = json.loads(capsys.readouterr().out)
    assert first == 0
    assert first_payload["completed"] == 1
    assert first_payload["safe_to_promote"] is True

    # Second run: the source is entirely absent from the manifest, a genuine
    # coverage collapse to 0. No --previous-count is supplied.
    empty_manifest = tmp_path / "empty.json"
    empty_manifest.write_text(json.dumps({"artifacts": []}))
    second = main(["--db", str(db), "daily", "--date", "2026-09-02", "--manifest", str(empty_manifest)])
    second_payload = json.loads(capsys.readouterr().out)
    assert second == 3
    assert second_payload["completed"] == 0
    # The baseline used must be the first run's completed count (1), not a
    # self-compare against this run's own completed count (0).
    assert second_payload["reconciliation"]["previous_count"] == 1
    assert second_payload["safe_to_promote"] is False
