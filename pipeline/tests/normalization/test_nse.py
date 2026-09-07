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
