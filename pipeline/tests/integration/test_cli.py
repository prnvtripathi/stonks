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


def test_daily_with_empty_manifest_reports_missing_and_is_nonzero(tmp_path: Path, capsys: Any) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"artifacts": []}))
    result = main(["--db", str(tmp_path / "market.db"), "daily", "--date", "2026-09-01", "--manifest", str(manifest)])
    assert result != 0
    assert "missing source dates" in json.loads(capsys.readouterr().err)["error"]


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


def test_backfill_with_incomplete_manifest_is_nonzero(tmp_path: Path, capsys: Any) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"artifacts": []}))
    result = main(["--db", str(tmp_path / "market.db"), "backfill", "--start", "2026-09-01", "--end", "2026-09-01", "--manifest", str(manifest)])
    assert result != 0
    assert "missing source dates" in json.loads(capsys.readouterr().err)["error"]


def test_explicit_sources_replace_default() -> None:
    from market_pipeline.cli import _parser

    args = _parser().parse_args(["backfill", "--source", "nse-eod"])
    assert args.sources == ["nse-eod"]
