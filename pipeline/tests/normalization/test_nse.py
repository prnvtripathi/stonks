from pathlib import Path

from market_pipeline.domain.models import AssetClass
from market_pipeline.normalization.nse import normalize_nse_rows

FIXTURE = Path(__file__).parents[1] / "fixtures" / "nse" / "mixed_bhavcopy.csv"


def test_keeps_only_eq_and_identified_etf_rows() -> None:
    batch = normalize_nse_rows(FIXTURE.read_bytes())

    assert {(x.symbol, x.asset_class) for x in batch.instruments} == {
        ("INFY", AssetClass.EQUITY),
        ("NIFTYBEES", AssetClass.ETF),
    }


def test_rejected_rows_preserve_series_type_and_reason() -> None:
    batch = normalize_nse_rows(FIXTURE.read_bytes())

    by_symbol = {row.symbol: row for row in batch.rejected_rows}
    assert by_symbol["SMECO"].raw_series == "SM"
    assert by_symbol["REITCO"].raw_type == "REIT"
    assert "series" in by_symbol["SMECO"].reason.lower()
    assert by_symbol["BAD"].reason
    assert by_symbol["INVALID"].reason


def test_duplicate_symbol_is_rejected_without_replacing_first_row() -> None:
    batch = normalize_nse_rows(FIXTURE.read_bytes())

    duplicate = [row for row in batch.rejected_rows if row.symbol == "INFY"]
    assert len(duplicate) == 1
    assert "duplicate" in duplicate[0].reason.lower()


def test_security_master_supplies_stable_isin_and_etf_classification() -> None:
    bhavcopy = b"SYMBOL,SERIES,CLOSE,TIMESTAMP\nNIFTYBEES,EQ,251,07-Sep-2026\n"
    master = b"SYMBOL,SERIES,TYPE,NAME OF COMPANY,ISIN NUMBER\nNIFTYBEES,EQ,ETF,Nippon ETF,INF204KB14I2\n"

    batch = normalize_nse_rows(bhavcopy, security_master=master)

    assert batch.instruments[0].asset_class is AssetClass.ETF
    assert batch.instruments[0].provider_identifier == "INF204KB14I2"
    assert batch.instruments[0].raw_series == "EQ"
    assert batch.instruments[0].raw_type == "ETF"


def test_provider_series_and_type_are_preserved_exactly() -> None:
    batch = normalize_nse_rows(FIXTURE.read_bytes())

    etf = next(instrument for instrument in batch.instruments if instrument.symbol == "NIFTYBEES")
    assert etf.raw_series == "eq"
    assert etf.raw_type == "eTf"


def test_only_explicit_etf_classification_is_eligible() -> None:
    body = (
        b"SYMBOL,SERIES,TYPE,NAME OF COMPANY,ISIN,CLOSE,TIMESTAMP\n"
        b"ETPGEN,EQ,ETP,Generic ETP Company,INE000E01011,100,07-Sep-2026\n"
        b"NAMEETF,EQ,,Generic ETF Name Company,INE000E01012,100,07-Sep-2026\n"
        b"SUFFIXBEES,EQ,,Suffix BeES Company,INE000E01013,100,07-Sep-2026\n"
        b"EXPLICIT,EQ,ETF,Explicit ETF,INE000E01014,100,07-Sep-2026\n"
        b"EQPREF,EQ,Preference Shares,EQ Preference,INE000P01012,100,07-Sep-2026\n"
        b"EQPART,EQ,partly-paid,EQ Partly Paid,INE000P01013,100,07-Sep-2026\n"
    )
    batch = normalize_nse_rows(body)

    assert {instrument.symbol: instrument.asset_class for instrument in batch.instruments} == {
        "NAMEETF": AssetClass.EQUITY,
        "SUFFIXBEES": AssetClass.EQUITY,
        "EXPLICIT": AssetClass.ETF,
    }
    reasons = {row.symbol: row.reason for row in batch.rejected_rows}
    assert "ETP" in reasons["ETPGEN"]
    assert "preference" in reasons["EQPREF"].lower()
    assert "partly" in reasons["EQPART"].lower()


def test_empty_required_fields_are_rejected() -> None:
    body = b"SYMBOL,SERIES,CLOSE,TIMESTAMP\nEMPTY,EQ,,\n"

    batch = normalize_nse_rows(body)

    assert batch.instruments == ()
    assert "date" in batch.rejected_rows[0].reason
    assert "closing" in batch.rejected_rows[0].reason


def test_mixed_report_dates_are_rejected() -> None:
    body = (
        b"SYMBOL,SERIES,CLOSE,TIMESTAMP\n"
        b"INFY,EQ,1510,07-Sep-2026\n"
        b"TCS,EQ,3500,06-Sep-2026\n"
    )

    batch = normalize_nse_rows(body)

    assert [instrument.symbol for instrument in batch.instruments] == ["INFY"]
    assert batch.rejected_rows[0].symbol == "TCS"
    assert "date" in batch.rejected_rows[0].reason
