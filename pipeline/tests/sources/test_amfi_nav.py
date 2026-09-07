from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

import pytest
from market_pipeline.domain.models import FetchedArtifact, SourceArtifact
from market_pipeline.sources.amfi_nav import (
    AmfiNavAdapter,
    AmfiNavError,
    parse_amfi_nav,
)
from market_pipeline.sources.registry import SourcePolicyError

FIXTURE = Path(__file__).parents[1] / "fixtures" / "amfi" / "nav_all.txt"


def _artifact(
    body: bytes,
    *,
    effective_date: date = date(2026, 9, 7),
    source_url: str = "https://www.amfiindia.com/net-asset-value/nav-download",
    checksum: str | None = None,
) -> FetchedArtifact:
    return FetchedArtifact(
        artifact=SourceArtifact(
            source_id="amfi-nav",
            source_url=source_url,
            retrieved_at=datetime.now(timezone.utc),
            effective_date=effective_date,
            checksum=checksum or sha256(body).hexdigest(),
            adapter_version="1.0.0",
            terms_url="https://www.amfiindia.com/terms-and-conditions",
        ),
        body=body,
    )


def test_preserves_scheme_identity_and_nav() -> None:
    scheme = parse_amfi_nav(FIXTURE.read_bytes())[0]
    assert (scheme.scheme_code, scheme.nav) == ("120503", Decimal("42.1234"))
    assert scheme.effective_date == date(2026, 9, 7)


def test_parses_amc_and_nested_category_headers_without_inventing_values() -> None:
    schemes = parse_amfi_nav(FIXTURE.read_bytes())
    assert schemes[0].amc == "Acme Mutual Fund"
    assert schemes[0].category == "Open Ended Schemes (Equity Scheme - Large Cap)"
    assert schemes[0].plan == "Direct"
    assert schemes[0].option == "Growth"
    assert schemes[1].plan == "Regular"
    assert schemes[1].option == "IDCW"
    assert schemes[2].plan is None
    assert schemes[2].option is None


def test_hierarchy_rows_with_empty_delimited_cells_are_supported_and_raw_values_remain() -> None:
    body = (
        b"Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Net Asset Value;Date\n"
        b"Acme AMC \n"
        b"Open Ended Schemes (Debt Scheme);;;;;\n"
        b"120503;INF000A01001;;Acme Fund;1.20;07-Sep-2026\n"
    )
    scheme = parse_amfi_nav(body)[0]
    assert scheme.amc == "Acme AMC"
    assert scheme.category == "Open Ended Schemes (Debt Scheme)"
    assert scheme.raw_amc == "Acme AMC "
    assert scheme.raw_category == "Open Ended Schemes (Debt Scheme)"


def test_bom_and_latin1_are_supported() -> None:
    body = "Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Net Asset Value;Date\nAcme AMC\n120503;INF000A01001;;Fönd;1.2;07-Sep-2026\n".encode("latin-1")
    assert parse_amfi_nav(body)[0].scheme_name == "Fönd"


@pytest.mark.parametrize(
    "body, message",
    [
        (b"Scheme Code;Scheme Name;Net Asset Value;Date\nabc;Fund;1;07-Sep-2026\n", "scheme code"),
        (b"Scheme Code;Scheme Name;Net Asset Value;Date\n120503;Fund;-1;07-Sep-2026\n", "NAV"),
        (b"Scheme Code;Scheme Name;Net Asset Value;Date\n120503;Fund;1;bad\n", "date"),
    ],
)
def test_invalid_code_nav_or_date_fails_closed(body: bytes, message: str) -> None:
    with pytest.raises(AmfiNavError, match=message):
        parse_amfi_nav(body)


def test_conflicting_duplicate_codes_fail_deterministically() -> None:
    body = (
        b"Scheme Code;Scheme Name;Net Asset Value;Date\n"
        b"120503;Fund A;1;07-Sep-2026\n"
        b"120503;Fund B;2;07-Sep-2026\n"
    )
    with pytest.raises(AmfiNavError, match="duplicate"):
        parse_amfi_nav(body)


def test_adapter_fetch_is_policy_governed_and_preserves_artifact_provenance() -> None:
    adapter = AmfiNavAdapter(lambda _: _artifact(FIXTURE.read_bytes()))
    result = adapter.fetch(date(2026, 9, 7))
    assert len(result) == 1
    assert result[0].artifact.source_id == "amfi-nav"
    assert result[0].artifact.effective_date == date(2026, 9, 7)
    assert result[0].artifact.source_url.startswith("https://www.amfiindia.com/")


def test_adapter_rejects_raw_bytes_without_provenance() -> None:
    adapter = AmfiNavAdapter(lambda _: FIXTURE.read_bytes())  # type: ignore[arg-type,return-value]
    with pytest.raises(AmfiNavError, match="provenance"):
        adapter.fetch(date(2026, 9, 7))


def test_adapter_rejects_deceptive_artifact_url() -> None:
    adapter = AmfiNavAdapter(
        lambda _: _artifact(FIXTURE.read_bytes(), source_url="https://www.amfiindia.com.evil.test/nav")
    )
    with pytest.raises(SourcePolicyError, match="official|canonical|source"):
        adapter.fetch(date(2026, 9, 7))


def test_adapter_rejects_artifact_date_or_checksum_mismatch() -> None:
    with pytest.raises(AmfiNavError, match="effective date"):
        AmfiNavAdapter(lambda _: _artifact(FIXTURE.read_bytes(), effective_date=date(2026, 9, 6))).fetch(
            date(2026, 9, 7)
        )
    with pytest.raises(AmfiNavError, match="checksum"):
        AmfiNavAdapter(lambda _: _artifact(FIXTURE.read_bytes(), checksum="0" * 64)).fetch(
            date(2026, 9, 7)
        )


@pytest.mark.parametrize("nav", [b"N.A.", b""])
def test_non_present_nav_is_rejected(nav: bytes) -> None:
    body = b"Scheme Code;Scheme Name;Net Asset Value;Date\n120503;Fund;" + nav + b";07-Sep-2026\n"
    with pytest.raises(AmfiNavError, match="NAV"):
        parse_amfi_nav(body)


def test_adapter_without_provider_fails_closed() -> None:
    with pytest.raises(AmfiNavError):
        AmfiNavAdapter().fetch(date(2026, 9, 7))
