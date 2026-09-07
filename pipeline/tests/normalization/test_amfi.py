from datetime import date
from decimal import Decimal
from pathlib import Path

from market_pipeline.normalization.amfi import normalize_amfi_schemes
from market_pipeline.sources.amfi_nav import parse_amfi_nav

FIXTURE = Path(__file__).parents[1] / "fixtures" / "amfi" / "nav_all.txt"


def test_scheme_id_is_stable_when_display_name_changes() -> None:
    first = normalize_amfi_schemes(parse_amfi_nav(FIXTURE.read_bytes())).schemes[0]
    changed = first.model_copy(update={"scheme_name": "Renamed Large Cap Fund"})
    assert first.scheme_id == changed.scheme_id
    assert str(first.scheme_id)


def test_normalizer_preserves_raw_fields_and_builds_nav_observation() -> None:
    batch = normalize_amfi_schemes(parse_amfi_nav(FIXTURE.read_bytes()))
    scheme = batch.schemes[0]
    assert scheme.scheme_code == "120503"
    assert scheme.asset_class.value == "mutual_fund"
    assert scheme.raw_amc == "Acme Mutual Fund"
    assert scheme.raw_category == "Open Ended Schemes (Equity Scheme - Large Cap)"
    assert scheme.nav == Decimal("42.1234")
    assert scheme.nav_date == date(2026, 9, 7)


def test_ambiguous_provider_isin_column_is_not_guessed_as_growth() -> None:
    schemes = normalize_amfi_schemes(parse_amfi_nav(FIXTURE.read_bytes())).schemes
    growth = next(scheme for scheme in schemes if scheme.scheme_code == "120503")
    distribution = next(scheme for scheme in schemes if scheme.scheme_code == "120504")
    assert growth.isin_growth == "INF000A01001"
    assert distribution.isin_growth is None
    assert distribution.isin_div_payout_growth == "INF000A01002"


def test_correction_supersedes_original_with_lineage() -> None:
    original = normalize_amfi_schemes(parse_amfi_nav(FIXTURE.read_bytes())).schemes[0]
    corrected = parse_amfi_nav(
        b"Scheme Code;Scheme Name;Net Asset Value;Date\n120503;Acme Large Cap Fund - Direct Plan - Growth;42.9999;07-Sep-2026\n"
    )[0]
    batch = normalize_amfi_schemes([original], corrections=[corrected])
    assert batch.active()[0].nav == Decimal("42.9999")
    assert batch.active()[0].supersedes_id == original.record_id
    assert original.record_id in batch.superseded_ids


def test_absence_requires_configurable_consecutive_files_before_inactive() -> None:
    current = normalize_amfi_schemes(parse_amfi_nav(FIXTURE.read_bytes())).schemes
    one_absence = normalize_amfi_schemes([], previous=current, consecutive_absence_window=2)
    assert all(s.active for s in one_absence.schemes)
    two_absence = normalize_amfi_schemes([], previous=one_absence.schemes, consecutive_absence_window=2)
    assert not all(s.active for s in two_absence.schemes)


def test_current_file_date_is_validated_against_requested_date() -> None:
    records = parse_amfi_nav(FIXTURE.read_bytes())
    try:
        normalize_amfi_schemes(records, effective_date=date(2026, 9, 6))
    except ValueError as exc:
        assert "date" in str(exc).lower()
    else:
        raise AssertionError("expected effective-date validation")


def test_bonus_is_not_mistaken_for_distribution() -> None:
    row = parse_amfi_nav(
        b"Scheme Code;Scheme Name;Net Asset Value;Date\n120503;Acme Bonus Option;1;07-Sep-2026\n"
    )[0]
    scheme = normalize_amfi_schemes([row]).schemes[0]
    assert scheme.option == "Bonus"
    assert not scheme.is_distribution


def test_reappearing_scheme_revives_after_absence() -> None:
    current = normalize_amfi_schemes(parse_amfi_nav(FIXTURE.read_bytes())).schemes
    absent = normalize_amfi_schemes([], previous=current, consecutive_absence_window=1)
    assert not absent.schemes[0].active
    revived = normalize_amfi_schemes(
        parse_amfi_nav(FIXTURE.read_bytes()), previous=absent.schemes, consecutive_absence_window=1
    )
    assert revived.schemes[0].active
    assert revived.schemes[0].consecutive_absences == 0
