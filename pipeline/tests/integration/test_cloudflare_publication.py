"""Static contracts for the scheduled Cloudflare publication bridge.

These tests deliberately inspect workflow text only: they must not need a
Cloudflare account, credentials, or a network call to prove the safety order.

R08/F09/F10: the R2 upload step used to be a per-object ``wrangler r2 object
put``/``get`` loop fed by Bash process substitution
(``< <(python3 - ... <<'PY' ... PY)``). If that embedded producer failed,
``set -euo pipefail`` did not propagate the failure into the enclosing
``while`` loop's exit code, so a corrupted or partial publish could still
fall through into the D1 import step. That whole loop is now replaced by one
synchronously-checked invocation of
``market_pipeline.publication.r2_sync`` -- its own bounded concurrency,
retry, deadline, and reuse behavior is unit-tested directly in
``pipeline/tests/integration/test_r2_sync.py``, not re-derived from workflow
text here.
"""

from pathlib import Path


def _workflow_text() -> str:
    return (Path(__file__).resolve().parents[3] / ".github/workflows/daily-data.yml").read_text(encoding="utf-8")


def test_daily_workflow_uploads_and_verifies_history_before_d1_pointer_switch() -> None:
    workflow = _workflow_text()

    install = workflow.index("Install pinned Wrangler dependency")
    export = workflow.index("Export locally active dataset for D1")
    d1_info = workflow.index("Read production D1 publication capacity")
    remote_state = workflow.index("Read production snapshot publication state")
    preflight = workflow.index("Preflight remote publication budget")
    r2 = workflow.index("Upload and verify private R2 history objects")
    d1 = workflow.index("Import active dataset into production D1")
    verify = workflow.index("Verify remote active dataset")
    assert install < export < d1_info < remote_state < preflight < r2 < d1 < verify
    assert "pnpm install --frozen-lockfile" in workflow
    assert "--object-manifest active-history-objects.json" in workflow
    assert "active-history-objects.json" in workflow
    assert "MAX_D1_MUTATIONS_PER_RUN=50000" in workflow
    assert "WEEKDAY_RUNS_PER_MONTH=22" in workflow
    assert "market_pipeline.publication.preflight --plan active-publication-plan.json" in workflow
    assert "--d1-info d1-publication-info.json" in workflow
    assert "wrangler d1 info stonks-research --env production --json > d1-publication-info.json" in workflow
    assert "remote-publication-state.json" in workflow
    assert "SELECT 'snapshot' AS kind, instrument_id AS value FROM instrument_snapshots" in workflow
    assert "MAX_R2_CLASS_A_PER_MONTH=800000" in workflow
    assert "remote publication plan exceeds the free-tier safety envelope" in workflow
    assert "wrangler d1 execute stonks-research --env production --remote --file active-dataset.sql" in workflow
    assert "SELECT dataset_id FROM active_dataset WHERE singleton = 1" in workflow
    assert "CLOUDFLARE_API_TOKEN: ${{ secrets.CLOUDFLARE_API_TOKEN }}" in workflow
    assert "CLOUDFLARE_ACCOUNT_ID: ${{ vars.CLOUDFLARE_ACCOUNT_ID }}" in workflow
    assert "MARKET_PIPELINE_PUBLISH_TOKEN" not in workflow
    publication = workflow[export:]
    assert "--env production" in publication
    assert "--env preview" not in publication
    assert "stonks-research-preview" not in publication
    assert "stonks-private-history-preview" not in publication


def test_daily_workflow_r2_upload_step_is_one_python_process_with_no_process_substitution() -> None:
    """F09/F10: no per-object subshell, no process substitution, one exit code."""

    workflow = _workflow_text()

    r2_start = workflow.index("Upload and verify private R2 history objects")
    d1_start = workflow.index("Import active dataset into production D1")
    upload_step = workflow[r2_start:d1_start]

    assert "set -euo pipefail" in upload_step
    assert "python -m market_pipeline.publication.r2_sync" in upload_step
    assert "--bundle active-history-objects.json" in upload_step
    assert "--history-root history" in upload_step
    # The historical unsafe pattern must be fully gone, not merely reduced.
    assert "<(" not in upload_step  # no process substitution
    assert "while IFS" not in upload_step  # no per-object read loop
    assert "wrangler r2 object" not in upload_step
    assert "resolved-publication-plan.json" not in upload_step

    # The R2 transport credential is distinct from the D1/Wrangler token.
    assert "R2_ACCESS_KEY_ID: ${{ secrets.R2_ACCESS_KEY_ID }}" in workflow
    assert "R2_SECRET_ACCESS_KEY: ${{ secrets.R2_SECRET_ACCESS_KEY }}" in workflow


def test_daily_workflow_does_not_gate_d1_import_with_always() -> None:
    """The D1 import step must not opt back into running after a failed upload."""

    workflow = _workflow_text()
    d1_start = workflow.index("Import active dataset into production D1")
    d1_end = workflow.index("Verify remote active dataset")
    d1_step = workflow[d1_start:d1_end]

    assert "if: always()" not in d1_step
    assert "continue-on-error" not in d1_step
