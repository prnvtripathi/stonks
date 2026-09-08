"""Static contracts for the scheduled Cloudflare publication bridge.

These tests deliberately inspect workflow text only: they must not need a
Cloudflare account, credentials, or a network call to prove the safety order.
"""

from pathlib import Path


def test_daily_workflow_uploads_and_verifies_history_before_d1_pointer_switch() -> None:
    workflow = (Path(__file__).resolve().parents[3] / ".github/workflows/daily-data.yml").read_text(
        encoding="utf-8"
    )

    install = workflow.index("Install pinned Wrangler dependency")
    export = workflow.index("Export locally active dataset for D1")
    r2 = workflow.index("Upload and verify private R2 history objects")
    d1 = workflow.index("Import active dataset into production D1")
    verify = workflow.index("Verify remote active dataset")
    assert install < export < r2 < d1 < verify
    assert "pnpm install --frozen-lockfile" in workflow
    assert "--object-manifest active-history-objects.json" in workflow
    assert "active-history-objects.json" in workflow
    assert 'remote_object="stonks-private-history/${object_key}"' in workflow
    assert 'wrangler r2 object put "${remote_object}" --file "${history_file}" --remote --env production' in workflow
    assert 'wrangler r2 object get "${remote_object}" --file "${verified_file}" --remote --env production' in workflow
    assert "cmp --silent" in workflow
    assert "find history -type f" not in workflow
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
