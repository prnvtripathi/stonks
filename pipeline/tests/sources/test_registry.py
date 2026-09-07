import pytest
from market_pipeline.domain.models import SourcePolicy
from market_pipeline.sources.registry import (
    OFFICIAL_SOURCE_IDS,
    SourcePolicyError,
    assert_source_enabled,
    get_source_policy,
)


def test_denied_source_cannot_run() -> None:
    with pytest.raises(SourcePolicyError):
        assert_source_enabled(
            SourcePolicy(
                source_id="x",
                automation_allowed=False,
                terms_url="https://example.test",
            )
        )


def test_registry_contains_only_approved_official_sources() -> None:
    assert set(OFFICIAL_SOURCE_IDS) == {"nse-eod", "nse-filings-xbrl", "amfi-nav", "nifty-500"}
    for source_id in OFFICIAL_SOURCE_IDS:
        policy = get_source_policy(source_id)
        assert policy.automation_allowed
        assert policy.source_url.startswith("https://")
        assert policy.terms_url.startswith("https://")
        assert policy.source_url.split("/")[2] in {
            "www.nseindia.com",
            "www.amfiindia.com",
            "www.niftyindices.com",
        }


def test_unknown_source_is_not_enabled() -> None:
    with pytest.raises(SourcePolicyError):
        get_source_policy("yahoo-finance")


def test_retention_denial_also_disables_automation() -> None:
    with pytest.raises(SourcePolicyError):
        assert_source_enabled(
            SourcePolicy(
                source_id="x",
                automation_allowed=True,
                retention_allowed=False,
                terms_url="https://example.test",
            )
        )
