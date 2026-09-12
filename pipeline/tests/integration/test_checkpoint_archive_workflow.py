"""Static contracts for the daily workflow's F12 checkpoint-archive wiring.

Like ``test_cloudflare_publication.py``, this inspects workflow text only:
no Cloudflare account, credentials, or network call. The archive's own
backup/restore/promote behavior is unit-tested against a fake object store
in ``test_checkpoint_restore.py``.
"""

from pathlib import Path


def _workflow_text() -> str:
    return (Path(__file__).resolve().parents[3] / ".github/workflows/daily-data.yml").read_text(encoding="utf-8")


def test_checkpoint_archive_restore_runs_before_the_pipeline_and_backup_after_remote_verification() -> None:
    workflow = _workflow_text()

    cache_restore = workflow.index("Restore market database from previous run")
    archive_restore = workflow.index("Restore checkpoint archive on cache miss")
    pipeline_run = workflow.index("Run market-pipeline refresh")
    cache_save = workflow.index("Save market database for next run")
    verify_remote = workflow.index("Verify remote active dataset")
    archive_backup = workflow.index("Back up checkpoint archive for recovery")
    ledger = workflow.index("Record this run's reservation-ledger entry")

    # The archive restore must run after the (fast-path) cache restore but
    # before the pipeline touches market.db/raw, and the archive backup+
    # promote must run only after production D1's active dataset has been
    # independently verified -- never speculatively before that.
    assert cache_restore < archive_restore < pipeline_run < cache_save
    assert verify_remote < archive_backup < ledger

    assert "market_pipeline.publication.checkpoint_archive restore" in workflow
    assert "market_pipeline.publication.checkpoint_archive backup" in workflow
    assert "--check-identity-only" in workflow
    assert "--promote --remote-active-dataset-id" in workflow


def test_checkpoint_archive_backup_only_promotes_after_reading_the_expected_dataset_id_file() -> None:
    workflow = _workflow_text()

    backup_start = workflow.index("Back up checkpoint archive for recovery")
    ledger_start = workflow.index("Record this run's reservation-ledger entry")
    backup_step = workflow[backup_start:ledger_start]

    assert "active-dataset-id.txt" in backup_step
    assert "set -euo pipefail" in backup_step


def test_checkpoint_archive_restore_never_masks_a_genuine_failure_as_bootstrap() -> None:
    """The restore step's shell wrapper never swallows a nonzero exit.

    ``set -euo pipefail`` at the top of the step is the whole safety
    boundary here -- a corrupted/interrupted remote archive must fail this
    step outright (per ``checkpoint_archive.restore_checkpoint_archive``),
    not be caught and silently treated as "start blank" by workflow-level
    shell logic. Only the CLI's own internal `NoCheckpointArchiveError`
    handling may treat "no archive" as non-fatal (it never raises for that
    case in the first place).
    """

    workflow = _workflow_text()
    restore_start = workflow.index("Restore checkpoint archive on cache miss")
    run_start = workflow.index("Run market-pipeline refresh")
    restore_step = workflow[restore_start:run_start]

    assert "set -euo pipefail" in restore_step
    assert "|| true" not in restore_step
    assert "continue-on-error" not in restore_step
