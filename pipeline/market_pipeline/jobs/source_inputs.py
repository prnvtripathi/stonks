"""Role-tagged, mode-aware provenance for one job run's contributing artifacts.

A source ID alone (e.g. "nse-eod") does not say what kind of data an artifact
carries for a given run: NSE publishes a security master, EOD observations,
filings, and corporate actions from related but distinct official downloads,
and a benchmark provider (Nifty Indices) contributes index-level observations
that stand apart from any single instrument's data. `SourceInputRole` makes
that distinction explicit instead of leaving callers to infer it from the
source-name string.

`SourceInput` records one admitted artifact's role, expected/loaded coverage
dates, and lineage (artifact ID, checksum, raw-store object key, adapter
version) alongside its `acquisition_mode` ("supplied" or "network"), so a job
can tell, after admission, exactly how each contributing artifact was
obtained without re-deriving it from the artifact body.

`resolve_supplied_source_input` is the supplied-file counterpart to a network
adapter's `SourceAdapter.fetch()`: it loads one operator-supplied file
strictly from within an explicit manifest root, builds and admits its
`SourceArtifact` through `sources.registry.admit`, and returns a `SourceInput`
recording the result. It never fetches over the network and never grants a
new permission -- admission still fails unless the resolved `SourcePolicy`
already has `supplied_use_allowed=True` for that source, and it fails closed
on a forged/mismatched URL, a wrong effective date, a checksum mismatch, or a
manifest entry whose file is missing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import Literal
from uuid import UUID

from market_pipeline.domain.models import SourceArtifact, SourcePolicy
from market_pipeline.sources.registry import SourcePolicyError, admit
from market_pipeline.storage.raw_store import _object_key

AcquisitionMode = Literal["supplied", "network"]


class SourceInputRole(StrEnum):
    """Distinguishes what an artifact contains; not inferable from source_id alone."""

    SECURITY_MASTER = "security_master"
    EOD_OBSERVATIONS = "eod_observations"
    FILINGS = "filings"
    CORPORATE_ACTIONS = "corporate_actions"
    BENCHMARK_OBSERVATIONS = "benchmark_observations"


class SourceInputManifestError(SourcePolicyError):
    """Raised when a supplied-file manifest entry is invalid or unresolvable."""


@dataclass(frozen=True)
class SourceInput:
    """One admitted artifact's role, coverage, and acquisition lineage."""

    source_id: str
    role: SourceInputRole
    expected_date: date
    loaded_date: date
    artifact_id: UUID
    checksum: str
    object_key: str
    adapter_version: str
    acquisition_mode: AcquisitionMode

    def __post_init__(self) -> None:
        if self.acquisition_mode not in ("supplied", "network"):
            raise ValueError(f"unsupported acquisition mode: {self.acquisition_mode}")
        if self.artifact_id is None:
            raise ValueError("source input requires an artifact ID")


def _resolve_within_root(manifest_root: Path, relative_path: str) -> Path:
    root = manifest_root.resolve()
    candidate = (manifest_root / relative_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise SourceInputManifestError(
            f"supplied file escapes the manifest root: {relative_path}"
        ) from exc
    if not candidate.is_file():
        raise SourceInputManifestError(f"missing supplied file for manifest entry: {relative_path}")
    return candidate


def resolve_supplied_source_input(
    *,
    manifest_root: Path,
    source_id: str,
    role: SourceInputRole,
    relative_path: str,
    expected_date: date,
    effective_date: date,
    source_url: str,
    terms_url: str,
    adapter_version: str,
    policy: SourcePolicy,
    permission_record_id: str,
    filename: str | None = None,
    allow_unregistered_source: bool = False,
) -> tuple[SourceArtifact, bytes, SourceInput]:
    """Load, admit, and role-tag one operator-supplied artifact.

    Resolves `relative_path` only from within `manifest_root` (rejecting any
    path that escapes it or does not exist), builds the artifact's
    `SourceArtifact` provenance from the actual file bytes, and admits it via
    `sources.registry.admit` against the given `policy` -- which must be a
    policy the caller resolved deliberately (the canonical registry entry in
    production, or an explicit test fixture). `expected_date` is the job's
    requested coverage date and `effective_date` is the manifest entry's own
    declared date; a mismatch between them is rejected the same way
    `AmfiNavAdapter._fetch` already rejects a mismatched network artifact.
    Admission fails closed on any forged URL, checksum mismatch, or
    unauthorized acquisition mode; this function grants no new permission
    itself.

    `allow_unregistered_source` is forwarded to `admit()` unchanged and
    defaults to `False`: a manifest entry for a source ID absent from
    `SOURCE_POLICIES` is rejected by default, exactly like `admit()`. Pass
    `True` only for a deliberate, isolated test fixture -- never in
    production job wiring.
    """

    if effective_date != expected_date:
        raise SourceInputManifestError(
            f"supplied artifact effective date does not match the requested date: {source_id}"
        )
    candidate = _resolve_within_root(manifest_root, relative_path)
    body = candidate.read_bytes()
    artifact = SourceArtifact(
        source_id=source_id,
        source_url=source_url,
        retrieved_at=datetime.now(timezone.utc),
        effective_date=effective_date,
        checksum=sha256(body).hexdigest(),
        adapter_version=adapter_version,
        terms_url=terms_url,
        filename=filename or candidate.name,
        acquisition_mode="supplied",
        permission_record_id=permission_record_id,
    )
    admitted = admit(artifact, policy, allow_unregistered_source=allow_unregistered_source)
    assert admitted.artifact_id is not None  # admit() never clears artifact_id
    source_input = SourceInput(
        source_id=source_id,
        role=role,
        expected_date=expected_date,
        loaded_date=effective_date,
        artifact_id=admitted.artifact_id,
        checksum=admitted.checksum,
        object_key=_object_key(admitted),
        adapter_version=adapter_version,
        acquisition_mode="supplied",
    )
    return admitted, body, source_input


def require_roles(inputs: list[SourceInput], required_roles: frozenset[SourceInputRole]) -> None:
    """Validate that every required role is present before checkpointing.

    A job must not check-point a run that silently dropped a required
    artifact role (e.g. filings without a security master to key against).
    """

    present = {source_input.role for source_input in inputs}
    missing = required_roles - present
    if missing:
        missing_names = ", ".join(sorted(role.value for role in missing))
        raise SourceInputManifestError(f"manifest is missing required source input roles: {missing_names}")


__all__ = [
    "AcquisitionMode",
    "SourceInput",
    "SourceInputManifestError",
    "SourceInputRole",
    "require_roles",
    "resolve_supplied_source_input",
]
