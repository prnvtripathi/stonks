"""Regression tests for mode-aware (supplied vs network) artifact admission.

F16 (second half): the registry's `assert_artifact_policy` used to apply the
same `automation_allowed` check to every admitted artifact, so a legitimately
supplied (operator-provided, non-network) NSE file had no path to admission
at all -- the same gate that (correctly) blocks live NSE network automation
also blocked a supplied file, even though they are different permission
questions.

These tests prove:
  * a "network" NSE artifact remains denied (automation_allowed=False, and
    this task does not flip that flag);
  * a "supplied" NSE artifact is *also* denied while no supplied-use
    permission is recorded for that source in the canonical registry --
    real NSE supplied-file admission stays blocked in production;
  * a supplied artifact *can* be admitted once a policy explicitly records
    supplied-use permission -- demonstrated only through a test-injected
    fixture `SourcePolicy`/source ID, never a change to `SOURCE_POLICIES`;
  * forged URLs, wrong dates, checksum mismatches, and missing companion
    manifest files are all rejected.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from hashlib import sha256
from pathlib import Path

import pytest
from market_pipeline.domain.models import SourceArtifact, SourcePolicy
from market_pipeline.jobs.source_inputs import (
    SourceInput,
    SourceInputManifestError,
    SourceInputRole,
    require_roles,
    resolve_supplied_source_input,
)
from market_pipeline.sources.registry import (
    SOURCE_POLICIES,
    SourcePolicyError,
    admit,
    get_source_policy,
)
from market_pipeline.storage.raw_store import ImmutableRawStoreError, LocalRawStore, _check_body

NSE_EOD_POLICY = get_source_policy("nse-eod")

FIXTURE_SOURCE_ID = "test-supplied-fixture"
FIXTURE_SOURCE_URL = "https://fixture.test.invalid/official/downloads"
FIXTURE_TERMS_URL = "https://fixture.test.invalid/terms"
FIXTURE_PERMISSION_REFERENCE = "test-permission-record-001"


def _supplied_use_fixture_policy(**overrides: object) -> SourcePolicy:
    """A test-only injected policy; never a change to SOURCE_POLICIES."""

    assert FIXTURE_SOURCE_ID not in SOURCE_POLICIES, "fixture must not shadow a real registry entry"
    defaults: dict[str, object] = dict(
        source_id=FIXTURE_SOURCE_ID,
        source_url=FIXTURE_SOURCE_URL,
        terms_url=FIXTURE_TERMS_URL,
        automation_allowed=False,
        retention_allowed=True,
        supplied_use_allowed=True,
        supplied_use_permission_reference=FIXTURE_PERMISSION_REFERENCE,
        approved_url_prefixes=(FIXTURE_SOURCE_URL,),
        description="Test-only fixture proving the supplied-use admission path.",
    )
    defaults.update(overrides)
    return SourcePolicy.model_validate(defaults)


def _artifact(**overrides: object) -> SourceArtifact:
    body = overrides.pop("body", b"fixture body")
    assert isinstance(body, bytes)
    defaults: dict[str, object] = dict(
        source_id=FIXTURE_SOURCE_ID,
        source_url=f"{FIXTURE_SOURCE_URL}/2026-09-07.csv",
        retrieved_at=datetime(2026, 9, 7, tzinfo=timezone.utc),
        effective_date=date(2026, 9, 7),
        checksum=sha256(body).hexdigest(),
        adapter_version="1.0.0",
        terms_url=FIXTURE_TERMS_URL,
        filename="fixture.csv",
        acquisition_mode="supplied",
        permission_record_id=FIXTURE_PERMISSION_REFERENCE,
    )
    defaults.update(overrides)
    return SourceArtifact.model_validate(defaults)


def _nse_artifact(**overrides: object) -> SourceArtifact:
    defaults: dict[str, object] = dict(
        source_id="nse-eod",
        source_url="https://archives.nseindia.com/content/historical/EQUITIES/2026/SEP/cm07SEP2026bhav.csv.zip",
        retrieved_at=datetime(2026, 9, 7, tzinfo=timezone.utc),
        effective_date=date(2026, 9, 7),
        checksum=sha256(b"nse body").hexdigest(),
        adapter_version="1.0.0",
        terms_url="https://www.nseindia.com/terms-of-use",
        filename="cm07SEP2026bhav.csv.zip",
    )
    defaults.update(overrides)
    return SourceArtifact.model_validate(defaults)


# --- Core admission-mode regressions (verbatim from the plan's oracle) -----


def test_network_nse_artifact_remains_denied() -> None:
    network_nse_artifact = _nse_artifact(acquisition_mode="network")
    with pytest.raises(SourcePolicyError):
        admit(network_nse_artifact, NSE_EOD_POLICY)


def test_supplied_nse_without_recorded_permission_remains_denied() -> None:
    supplied_nse_without_permission = _nse_artifact(
        acquisition_mode="supplied", permission_record_id="whatever-someone-claims"
    )
    with pytest.raises(SourcePolicyError):
        admit(supplied_nse_without_permission, NSE_EOD_POLICY)


def test_authorized_supplied_use_admits_valid_provenance() -> None:
    valid_supplied_fixture = _artifact()
    supplied_use_fixture_policy = _supplied_use_fixture_policy()
    admitted = admit(valid_supplied_fixture, supplied_use_fixture_policy, allow_unregistered_source=True)
    assert admitted.acquisition_mode == "supplied"


def test_admit_rejects_an_unregistered_source_id_by_default_regardless_of_claimed_permission() -> None:
    """admit() itself must never admit a source ID absent from SOURCE_POLICIES.

    This must hold even when the caller-supplied policy claims every
    permission a real registry entry could have -- `allow_unregistered_source`
    is an explicit, narrow opt-in, never the default.
    """

    assert FIXTURE_SOURCE_ID not in SOURCE_POLICIES
    generous_policy = _supplied_use_fixture_policy(automation_allowed=True)
    network_artifact = _artifact(acquisition_mode="network")
    supplied_artifact = _artifact()

    with pytest.raises(SourcePolicyError):
        admit(network_artifact, generous_policy)
    with pytest.raises(SourcePolicyError):
        admit(supplied_artifact, generous_policy)
    # The identical artifact/policy pair *is* admitted once the caller
    # explicitly opts in -- proving the rejection above is really about the
    # unregistered source ID, not some other mismatch.
    admitted = admit(supplied_artifact, generous_policy, allow_unregistered_source=True)
    assert admitted.acquisition_mode == "supplied"


def test_nse_registry_still_grants_no_supplied_use_permission_in_production() -> None:
    """Guard against this task accidentally flipping a production flag."""

    for source_id in ("nse-eod", "nse-filings-xbrl"):
        canonical = get_source_policy(source_id)
        assert canonical.automation_allowed is False
        assert canonical.supplied_use_allowed is False
        assert canonical.supplied_use_permission_reference is None


# --- Forged/incorrect provenance rejections --------------------------------


def test_forged_source_url_is_rejected() -> None:
    policy = _supplied_use_fixture_policy()
    forged = _artifact(source_url="https://attacker.example/looks-official.csv")
    with pytest.raises(SourcePolicyError):
        admit(forged, policy, allow_unregistered_source=True)


def test_forged_terms_url_is_rejected() -> None:
    policy = _supplied_use_fixture_policy()
    forged = _artifact(terms_url="https://attacker.example/terms")
    with pytest.raises(SourcePolicyError):
        admit(forged, policy, allow_unregistered_source=True)


def test_missing_permission_record_id_is_rejected_even_when_policy_allows_supplied_use() -> None:
    policy = _supplied_use_fixture_policy()
    artifact = _artifact(permission_record_id=None)
    with pytest.raises(SourcePolicyError):
        admit(artifact, policy, allow_unregistered_source=True)


def test_mismatched_permission_record_id_is_rejected() -> None:
    policy = _supplied_use_fixture_policy()
    artifact = _artifact(permission_record_id="a-different-permission-entirely")
    with pytest.raises(SourcePolicyError):
        admit(artifact, policy, allow_unregistered_source=True)


def test_policy_source_id_mismatch_is_rejected() -> None:
    policy = _supplied_use_fixture_policy(source_id="another-fixture-source")
    artifact = _artifact()
    with pytest.raises(SourcePolicyError):
        admit(artifact, policy)


def test_unregistered_source_id_forging_a_real_registry_entrys_shape_is_still_checked() -> None:
    """A policy for a real registry source ID must equal the canonical entry."""

    forged_policy = NSE_EOD_POLICY.model_copy(
        update={"supplied_use_allowed": True, "supplied_use_permission_reference": "forged"}
    )
    artifact = _nse_artifact(acquisition_mode="supplied", permission_record_id="forged")
    with pytest.raises(SourcePolicyError):
        admit(artifact, forged_policy)


def test_wrong_checksum_is_rejected_after_a_supplied_artifact_is_otherwise_admitted() -> None:
    """Passing policy admission (mode/URL/permission) never bypasses checksum integrity.

    `admit()` validates provenance/permission only; the raw store's own
    checksum verification (`_check_body`) is a separate, still-mandatory
    gate applied to every artifact regardless of acquisition mode. A real
    NSE source cannot demonstrate this combination (no registry entry has
    `supplied_use_allowed=True`), so this uses the same test-only fixture
    policy that proves the "authorized supplied use" admission path.
    """

    body = b"the real bytes"
    artifact = _artifact(checksum=sha256(b"different bytes").hexdigest())
    policy = _supplied_use_fixture_policy()

    admitted = admit(artifact, policy, allow_unregistered_source=True)  # policy/provenance checks pass

    with pytest.raises(ImmutableRawStoreError):
        _check_body(admitted, body)


def test_wrong_checksum_is_rejected_by_the_raw_store_for_a_real_registered_source(
    tmp_path: Path,
) -> None:
    """Regression: the mode-aware refactor must not weaken checksum enforcement."""

    body = b"symbol,close\nINFY,1500\n"
    artifact = SourceArtifact.model_validate(
        dict(
            source_id="amfi-nav",
            source_url="https://www.amfiindia.com/net-asset-value/nav-download",
            retrieved_at=datetime(2026, 9, 7, tzinfo=timezone.utc),
            effective_date=date(2026, 9, 4),
            checksum=sha256(b"not the real body").hexdigest(),
            adapter_version="1.0.0",
            terms_url="https://www.amfiindia.com/terms-and-conditions",
            filename="report.csv",
        )
    )
    with pytest.raises(ImmutableRawStoreError):
        LocalRawStore(tmp_path).put(artifact, body)


# --- Role-tagged supplied manifest resolution ------------------------------


@pytest.fixture
def manifest_root(tmp_path: Path) -> Path:
    root = tmp_path / "manifest"
    root.mkdir()
    (root / "nse-eod-2026-09-07.csv").write_bytes(b"SYMBOL,CLOSE\nINFY,1500\n")
    return root


def _resolve(manifest_root: Path, **overrides: object) -> tuple[SourceArtifact, bytes, SourceInput]:
    defaults: dict[str, object] = dict(
        manifest_root=manifest_root,
        source_id=FIXTURE_SOURCE_ID,
        role=SourceInputRole.EOD_OBSERVATIONS,
        relative_path="nse-eod-2026-09-07.csv",
        expected_date=date(2026, 9, 7),
        effective_date=date(2026, 9, 7),
        source_url=f"{FIXTURE_SOURCE_URL}/2026-09-07.csv",
        terms_url=FIXTURE_TERMS_URL,
        adapter_version="1.0.0",
        policy=_supplied_use_fixture_policy(),
        permission_record_id=FIXTURE_PERMISSION_REFERENCE,
        allow_unregistered_source=True,
    )
    defaults.update(overrides)
    return resolve_supplied_source_input(**defaults)  # type: ignore[arg-type]


def test_resolve_supplied_source_input_admits_a_valid_manifest_entry(manifest_root: Path) -> None:
    artifact, body, source_input = _resolve(manifest_root)
    assert artifact.acquisition_mode == "supplied"
    assert body == b"SYMBOL,CLOSE\nINFY,1500\n"
    assert source_input.role is SourceInputRole.EOD_OBSERVATIONS
    assert source_input.acquisition_mode == "supplied"
    assert source_input.object_key.startswith(f"raw/{FIXTURE_SOURCE_ID}/2026-09-07/")


def test_resolve_supplied_source_input_rejects_a_missing_companion_file(manifest_root: Path) -> None:
    with pytest.raises(SourceInputManifestError):
        _resolve(manifest_root, relative_path="does-not-exist.csv")


def test_resolve_supplied_source_input_rejects_paths_escaping_the_manifest_root(
    manifest_root: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.csv"
    outside.write_bytes(b"not from the manifest")
    with pytest.raises(SourceInputManifestError):
        _resolve(manifest_root, relative_path="../outside.csv")


def test_resolve_supplied_source_input_rejects_a_mismatched_effective_date(manifest_root: Path) -> None:
    with pytest.raises(SourceInputManifestError):
        _resolve(manifest_root, expected_date=date(2026, 9, 8))


def test_resolve_supplied_source_input_still_enforces_admission_policy(manifest_root: Path) -> None:
    """A manifest entry cannot bypass admission by pointing at a real NSE ID."""

    with pytest.raises(SourcePolicyError):
        _resolve(
            manifest_root,
            source_id="nse-eod",
            source_url="https://archives.nseindia.com/content/x.csv",
            terms_url="https://www.nseindia.com/terms-of-use",
            policy=NSE_EOD_POLICY,
        )


def test_require_roles_rejects_a_missing_required_role() -> None:
    from uuid import uuid4

    inputs = [
        SourceInput(
            source_id=FIXTURE_SOURCE_ID,
            role=SourceInputRole.EOD_OBSERVATIONS,
            expected_date=date(2026, 9, 7),
            loaded_date=date(2026, 9, 7),
            artifact_id=uuid4(),
            checksum=sha256(b"x").hexdigest(),
            object_key="raw/test-supplied-fixture/2026-09-07/deadbeef/fixture.csv",
            adapter_version="1.0.0",
            acquisition_mode="supplied",
        )
    ]
    with pytest.raises(SourceInputManifestError):
        require_roles(inputs, frozenset({SourceInputRole.EOD_OBSERVATIONS, SourceInputRole.SECURITY_MASTER}))


def test_require_roles_accepts_a_complete_set(manifest_root: Path) -> None:
    _, _, eod_input = _resolve(manifest_root)
    security_master_input = SourceInput(
        source_id=FIXTURE_SOURCE_ID,
        role=SourceInputRole.SECURITY_MASTER,
        expected_date=date(2026, 9, 7),
        loaded_date=date(2026, 9, 7),
        artifact_id=eod_input.artifact_id,
        checksum=eod_input.checksum,
        object_key=eod_input.object_key,
        adapter_version="1.0.0",
        acquisition_mode="supplied",
    )
    require_roles(
        [eod_input, security_master_input],
        frozenset({SourceInputRole.EOD_OBSERVATIONS, SourceInputRole.SECURITY_MASTER}),
    )
