from datetime import date
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from market_pipeline.normalization.nse import NseRowError
from market_pipeline.sources.nse_eod import (
    NseArchiveError,
    NseEodAdapter,
    parse_nse_bhavcopy,
    parse_nse_security_master,
)
from market_pipeline.sources.registry import SourcePolicyError


def test_disabled_adapter_cannot_fetch() -> None:
    with pytest.raises(SourcePolicyError):
        NseEodAdapter().fetch(date(2026, 9, 7))


def test_bhavcopy_parser_accepts_legacy_report_date() -> None:
    rows = parse_nse_bhavcopy(
        b"SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,TIMESTAMP\nINFY,EQ,1,2,0.5,1.5,07-Sep-2026\n"
    )
    assert rows[0].symbol == "INFY"
    assert rows[0].report_date == date(2026, 9, 7)


def test_security_master_rejects_missing_required_columns() -> None:
    with pytest.raises(NseArchiveError, match="required"):
        parse_nse_security_master(b"SYMBOL,SERIES\nINFY,EQ\n")


def test_archive_parser_rejects_path_traversal() -> None:
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("../report.csv", b"SYMBOL,SERIES,CLOSE,TIMESTAMP\nINFY,EQ,1,07-Sep-2026\n")

    with pytest.raises(NseArchiveError, match="unsafe"):
        parse_nse_bhavcopy(output.getvalue())


def test_archive_parser_reads_single_csv_member() -> None:
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr("cm07SEP2026bhav.csv", b"SYMBOL,SERIES,CLOSE,TIMESTAMP\nINFY,EQ,1,07-Sep-2026\n")

    rows = parse_nse_bhavcopy(output.getvalue())
    assert rows[0].symbol == "INFY"


def test_security_master_duplicate_symbols_fail_schema_validation() -> None:
    body = (
        b"SYMBOL,SERIES,NAME OF COMPANY,ISIN NUMBER\n"
        b"INFY,EQ,Infosys,INE009A01021\n"
        b"INFY,EQ,Infosys renamed,INE009A01021\n"
    )

    with pytest.raises(NseRowError, match="duplicate"):
        from market_pipeline.normalization.nse import parse_nse_security_master as parse_master

        parse_master(body)
