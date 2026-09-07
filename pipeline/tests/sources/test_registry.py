from datetime import date

import pytest
from market_pipeline.domain.models import FetchedArtifact, SourcePolicy
from market_pipeline.sources.base import fetch_with_policy
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
        assert policy.automation_allowed is (source_id in {"amfi-nav", "nifty-500"})
        assert policy.source_url.startswith("https://")
        assert policy.terms_url.startswith("https://")
        if policy.automation_allowed:
            assert policy.permission_reference is not None
        assert policy.source_url.split("/")[2] in {
            "www.nseindia.com",
            "www.amfiindia.com",
            "www.niftyindices.com",
        }


def test_nse_automation_is_disabled_without_written_permission() -> None:
    for source_id in ("nse-eod", "nse-filings-xbrl"):
        policy = get_source_policy(source_id)
        assert policy.automation_allowed is False
        with pytest.raises(SourcePolicyError):
            assert_source_enabled(policy)


def test_forged_policy_cannot_enable_a_registered_source() -> None:
    canonical = get_source_policy("amfi-nav")
    forged = canonical.model_copy(update={"terms_url": "https://attacker.example/terms"})
    with pytest.raises(SourcePolicyError):
        assert_source_enabled(forged)


def test_forged_policy_cannot_enable_unknown_source() -> None:
    with pytest.raises(SourcePolicyError):
        assert_source_enabled(
            SourcePolicy(
                source_id="amfi-nav",
                source_url="https://attacker.example/nav",
                automation_allowed=True,
                terms_url="https://attacker.example/terms",
            )
        )


def test_adapter_policy_is_checked_before_fetch() -> None:
    class Adapter:
        source_id = "nse-eod"
        adapter_version = "1.0.0"
        policy = get_source_policy("nse-eod")

        def fetch(self, effective_date: date) -> list[FetchedArtifact]:
            raise AssertionError("fetch must not run for a disabled source")

    with pytest.raises(SourcePolicyError):
        fetch_with_policy(Adapter(), date(2026, 9, 7))


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
