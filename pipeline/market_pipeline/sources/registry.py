"""Allowlist of official public market-data sources."""

from __future__ import annotations

from typing import TYPE_CHECKING

from market_pipeline.domain.models import SourceArtifact, SourcePolicy

if TYPE_CHECKING:
    from market_pipeline.sources.base import SourceAdapter


class SourcePolicyError(RuntimeError):
    """Raised when an adapter is not permitted to automate a source."""


SOURCE_POLICIES: dict[str, SourcePolicy] = {
    "nse-eod": SourcePolicy(
        source_id="nse-eod",
        source_url="https://www.nseindia.com/all-reports",
        terms_url="https://www.nseindia.com/terms-of-use",
        automation_allowed=False,
        approved_url_prefixes=(
            "https://www.nseindia.com/all-reports",
            "https://archives.nseindia.com/content/",
        ),
        description="Official NSE daily reports for end-of-day market data.",
    ),
    "nse-filings-xbrl": SourcePolicy(
        source_id="nse-filings-xbrl",
        source_url="https://www.nseindia.com/companies-listing/corporate-filings-application",
        terms_url="https://www.nseindia.com/terms-of-use",
        automation_allowed=False,
        approved_url_prefixes=(
            "https://www.nseindia.com/companies-listing/corporate-filings-application",
            "https://archives.nseindia.com/content/",
        ),
        description="Official NSE corporate filings and available CSV/XBRL artifacts.",
    ),
    "amfi-nav": SourcePolicy(
        source_id="amfi-nav",
        source_url="https://www.amfiindia.com/net-asset-value/nav-download",
        terms_url="https://www.amfiindia.com/terms-and-conditions",
        automation_allowed=True,
        permission_reference="https://www.amfiindia.com/terms-and-conditions",
        approved_url_prefixes=(
            "https://www.amfiindia.com/net-asset-value/nav-download",
            "https://www.amfiindia.com/spages/",
        ),
        description="Official AMFI daily and historical mutual-fund NAV data.",
    ),
    "nifty-500": SourcePolicy(
        source_id="nifty-500",
        source_url="https://www.niftyindices.com/reports/historical-data",
        terms_url="https://www.niftyindices.com/terms-conditions",
        automation_allowed=True,
        permission_reference="https://www.niftyindices.com/terms-conditions",
        approved_url_prefixes=("https://www.niftyindices.com/reports/historical-data",),
        description="Official Nifty Indices Nifty 500 closing values.",
    ),
}

OFFICIAL_SOURCE_IDS: tuple[str, ...] = tuple(SOURCE_POLICIES)


def get_source_policy(source_id: str) -> SourcePolicy:
    try:
        return SOURCE_POLICIES[source_id]
    except KeyError as exc:
        raise SourcePolicyError(f"source is not on the official allowlist: {source_id}") from exc


def assert_source_enabled(policy: SourcePolicy) -> None:
    """Check whether a live network fetch may run for this source.

    This governs `SourceAdapter.fetch()` only. It is unrelated to whether a
    separately-supplied (operator-provided, never network-fetched by this
    pipeline) artifact for the same source may be admitted; see
    `_assert_supplied_use_permitted` for that decision.
    """

    canonical = SOURCE_POLICIES.get(policy.source_id)
    if canonical is None:
        raise SourcePolicyError(f"source is not on the official allowlist: {policy.source_id}")
    if policy != canonical:
        raise SourcePolicyError(f"source policy does not match canonical registry entry: {policy.source_id}")
    _assert_network_permitted(canonical)


def _assert_network_permitted(policy: SourcePolicy) -> None:
    """Validate a policy's live-network-fetch permission fields only."""

    if not policy.automation_allowed or not policy.retention_allowed:
        raise SourcePolicyError(f"automation is disabled for source: {policy.source_id}")
    if not policy.terms_url.startswith("https://") or policy.permission_reference is None:
        raise SourcePolicyError(f"source terms reference is required: {policy.source_id}")


def _assert_supplied_use_permitted(policy: SourcePolicy) -> None:
    """Validate a policy's supplied-file retention/use permission fields only.

    This is a distinct permission decision from `automation_allowed`: a
    source whose terms currently prohibit automated collection may still,
    separately, have recorded permission for an operator to supply and
    retain individual official files. No source in the canonical registry
    has this permission recorded today (see `SOURCE_POLICIES`), so a real
    supplied artifact for `nse-eod`/`nse-filings-xbrl` remains denied until
    an operator/owner explicitly records that decision.
    """

    if not policy.supplied_use_allowed or not policy.retention_allowed:
        raise SourcePolicyError(f"supplied-file use/retention is not authorized for source: {policy.source_id}")
    if not policy.terms_url.startswith("https://") or policy.supplied_use_permission_reference is None:
        raise SourcePolicyError(f"supplied-use permission reference is required: {policy.source_id}")


def assert_artifact_policy(
    *,
    source_id: str,
    source_url: str,
    terms_url: str,
    acquisition_mode: str = "network",
) -> None:
    """Reject artifact provenance that is not the complete canonical policy.

    `acquisition_mode` selects which permission is checked: "network" (the
    default, matching this function's original, sole pre-existing behavior)
    requires `automation_allowed`; "supplied" requires the separate
    `supplied_use_allowed` permission instead. Both modes still require
    exact-match official URL/provenance.
    """

    canonical = SOURCE_POLICIES.get(source_id)
    if canonical is None:
        raise SourcePolicyError(f"source is not on the official allowlist: {source_id}")
    assert_source_url_allowed(source_id, source_url)
    if terms_url != canonical.terms_url:
        raise SourcePolicyError(f"artifact provenance does not match canonical source: {source_id}")
    if acquisition_mode == "network":
        assert_source_enabled(canonical)
    elif acquisition_mode == "supplied":
        _assert_supplied_use_permitted(canonical)
    else:
        raise SourcePolicyError(f"unsupported acquisition mode: {acquisition_mode}")


def admit(
    artifact: SourceArtifact,
    policy: SourcePolicy,
    *,
    allow_unregistered_source: bool = False,
) -> SourceArtifact:
    """Validate one artifact's full provenance against an explicit policy.

    This is the mode-aware admission entry point: it does not fetch bytes,
    only decides whether `artifact` may be admitted under `policy`. By
    default this restores the original `assert_artifact_policy` invariant
    that an unregistered source ID (one absent from `SOURCE_POLICIES`) is
    never admitted, regardless of what the caller's `policy` object claims --
    this is enforced by `admit()` itself, not merely by convention that every
    caller happens to resolve `policy` via `get_source_policy` first.

    Callers that resolve a source's policy from the canonical registry (e.g.
    `assert_artifact_policy`, used by the raw store) additionally get full
    canonical-equality protection against a forged policy object for a
    *registered* source ID.

    `allow_unregistered_source=True` is an explicit, narrow opt-in for tests
    that must prove the mode-aware admission logic in isolation using a
    wholly fictional source ID (never a real registry entry) -- e.g. a
    fixture demonstrating the "authorized supplied use" pathway without
    touching `SOURCE_POLICIES`. No production call site sets this; it must
    never be passed for a source ID that could plausibly collide with a real
    or future registry entry.
    """

    if artifact.source_id != policy.source_id:
        raise SourcePolicyError("artifact source ID does not match the supplied policy")
    canonical = SOURCE_POLICIES.get(artifact.source_id)
    if canonical is None:
        if not allow_unregistered_source:
            raise SourcePolicyError(f"source is not on the official allowlist: {artifact.source_id}")
    elif policy != canonical:
        raise SourcePolicyError(f"artifact policy does not match canonical registry entry: {artifact.source_id}")
    _assert_url_allowed_for_policy(policy, artifact.source_url)
    if artifact.terms_url != policy.terms_url:
        raise SourcePolicyError(f"artifact provenance does not match canonical source: {artifact.source_id}")
    if artifact.acquisition_mode == "network":
        _assert_network_permitted(policy)
    elif artifact.acquisition_mode == "supplied":
        _assert_supplied_use_permitted(policy)
        expected_reference = policy.supplied_use_permission_reference
        if not artifact.permission_record_id or artifact.permission_record_id != expected_reference:
            raise SourcePolicyError(
                f"supplied artifact does not cite the recorded supplied-use permission: {artifact.source_id}"
            )
    else:
        raise SourcePolicyError(f"unsupported acquisition mode: {artifact.acquisition_mode}")
    return artifact


def assert_source_url_allowed(source_id: str, source_url: str) -> None:
    """Validate an artifact URL against exact official HTTPS origins and paths."""

    canonical = SOURCE_POLICIES.get(source_id)
    if canonical is None:
        raise SourcePolicyError(f"source is not on the official allowlist: {source_id}")
    _assert_url_allowed_for_policy(canonical, source_url)


def _assert_url_allowed_for_policy(policy: SourcePolicy, source_url: str) -> None:
    """Validate a URL against a specific policy's own approved prefixes.

    Unlike `assert_source_url_allowed`, this never looks the policy up by
    source ID in `SOURCE_POLICIES` -- it trusts the `policy` object the
    caller already resolved (canonically, for production call sites; via an
    injected test fixture otherwise). This lets `admit()` validate a
    fixture policy for a source ID that is intentionally absent from the
    canonical registry.
    """

    from urllib.parse import urlsplit

    actual = urlsplit(source_url)
    if actual.scheme != "https" or not actual.netloc or actual.username or actual.password:
        raise SourcePolicyError(f"source URL must use an official HTTPS origin: {source_url}")
    for prefix in policy.approved_url_prefixes:
        expected = urlsplit(prefix)
        if actual.netloc != expected.netloc or actual.scheme != expected.scheme:
            continue
        expected_path = expected.path.rstrip("/")
        actual_path = actual.path.rstrip("/")
        if actual_path == expected_path or actual_path.startswith(expected_path + "/"):
            return
    raise SourcePolicyError(f"source URL is outside the approved path prefixes: {source_url}")


def assert_adapter_enabled(adapter: SourceAdapter) -> None:
    """Check an adapter's full policy before allowing its network fetch."""

    canonical = SOURCE_POLICIES.get(adapter.source_id)
    if canonical is None:
        raise SourcePolicyError(f"source is not on the official allowlist: {adapter.source_id}")
    if adapter.policy != canonical:
        raise SourcePolicyError(
            f"adapter policy does not match canonical registry entry: {adapter.source_id}"
        )
    assert_source_enabled(canonical)
