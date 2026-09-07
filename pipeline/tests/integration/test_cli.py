from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from market_pipeline.cli import main


def test_daily_without_committed_artifact_input_is_nonzero(capsys: Any) -> None:
    result = main(["daily", "--date", "2026-09-01"])
    assert result != 0
    assert json.loads(capsys.readouterr().err)["error"] == "artifact input/provider is required"


def test_daily_with_empty_manifest_reports_missing_and_is_nonzero(tmp_path: Path, capsys: Any) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}")
    result = main(["--db", str(tmp_path / "market.db"), "daily", "--date", "2026-09-01", "--manifest", str(manifest)])
    assert result != 0
    assert "missing source dates" in json.loads(capsys.readouterr().err)["error"]


def test_explicit_sources_replace_default() -> None:
    from market_pipeline.cli import _parser

    args = _parser().parse_args(["backfill", "--source", "nse-eod"])
    assert args.sources == ["nse-eod"]
