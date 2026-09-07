"""Allowlist of official public market-data sources."""

from __future__ import annotations

from market_pipeline.domain.models import SourcePolicy


class SourcePolicyError(RuntimeError):
    """Raised when an adapter is not permitted to automate a source."""


SOURCE_POLICIES: dict[str, SourcePolicy] = {
    "nse-eod": SourcePolicy(
        source_id="nse-eod",
        source_url="https://www.nseindia.com/all-reports",
        terms_url="https://www.nseindia.com/terms-of-use",
        automation_allowed=True,
        description="Official NSE daily reports for end-of-day market data.",
    ),
    "nse-filings-xbrl": SourcePolicy(
        source_id="nse-filings-xbrl",
        source_url="https://www.nseindia.com/companies-listing/corporate-filings-application",
        terms_url="https://www.nseindia.com/terms-of-use",
        automation_allowed=True,
        description="Official NSE corporate filings and available CSV/XBRL artifacts.",
    ),
    "amfi-nav": SourcePolicy(
        source_id="amfi-nav",
        source_url="https://www.amfiindia.com/net-asset-value/nav-download",
        terms_url="https://www.amfiindia.com/terms-and-conditions",
        automation_allowed=True,
        description="Official AMFI daily and historical mutual-fund NAV data.",
    ),
    "nifty-500": SourcePolicy(
        source_id="nifty-500",
        source_url="https://www.niftyindices.com/reports/historical-data",
        terms_url="https://www.niftyindices.com/terms-conditions",
        automation_allowed=True,
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
    if policy.source_id not in SOURCE_POLICIES:
        raise SourcePolicyError(f"source is not on the official allowlist: {policy.source_id}")
    if not policy.automation_allowed or not policy.retention_allowed:
        raise SourcePolicyError(f"automation is disabled for source: {policy.source_id}")
    if not policy.terms_url.startswith("https://"):
        raise SourcePolicyError(f"source terms reference is required: {policy.source_id}")
