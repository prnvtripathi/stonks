"""Allowlist of official public market-data sources."""

from __future__ import annotations

from typing import TYPE_CHECKING

from market_pipeline.domain.models import SourcePolicy

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
    canonical = SOURCE_POLICIES.get(policy.source_id)
    if canonical is None:
        raise SourcePolicyError(f"source is not on the official allowlist: {policy.source_id}")
    if policy != canonical:
        raise SourcePolicyError(f"source policy does not match canonical registry entry: {policy.source_id}")
    if not canonical.automation_allowed or not canonical.retention_allowed:
        raise SourcePolicyError(f"automation is disabled for source: {policy.source_id}")
    if not canonical.terms_url.startswith("https://") or canonical.permission_reference is None:
        raise SourcePolicyError(f"source terms reference is required: {policy.source_id}")


def assert_artifact_policy(*, source_id: str, source_url: str, terms_url: str) -> None:
    """Reject artifact provenance that is not the complete canonical policy."""

    canonical = SOURCE_POLICIES.get(source_id)
    if canonical is None:
        raise SourcePolicyError(f"source is not on the official allowlist: {source_id}")
    assert_source_url_allowed(source_id, source_url)
    if terms_url != canonical.terms_url:
        raise SourcePolicyError(f"artifact provenance does not match canonical source: {source_id}")
    assert_source_enabled(canonical)


def assert_source_url_allowed(source_id: str, source_url: str) -> None:
    """Validate an artifact URL against exact official HTTPS origins and paths."""

    canonical = SOURCE_POLICIES.get(source_id)
    if canonical is None:
        raise SourcePolicyError(f"source is not on the official allowlist: {source_id}")
    from urllib.parse import urlsplit

    actual = urlsplit(source_url)
    if actual.scheme != "https" or not actual.netloc or actual.username or actual.password:
        raise SourcePolicyError(f"source URL must use an official HTTPS origin: {source_url}")
    for prefix in canonical.approved_url_prefixes:
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
