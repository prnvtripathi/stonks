"""F16/S03: connect real NSE filings and official Nifty 500 benchmark
observations into fundamental-metric and benchmark-RS reference rows.

Scenario coverage:

* ``EQUITY_A`` -- an equity with an original quarterly filing and a later
  restatement of the same period: the restated period must supersede the
  original, and the published fundamental metric must reflect the restated
  value, never a blend of both.
* ``EQUITY_B`` -- an equity with no filing at all: every fundamental metric
  is honestly ``missing``, never fabricated from price data. It also has no
  benchmark mapping, so its benchmark RS is honestly ``missing`` too.
* ``ETF_C`` -- an ETF: fundamentals are ``not_applicable`` (ETFs do not file
  company financial results), distinct from ``missing``.
* ``MUTUAL_FUND_D`` -- a mutual-fund scheme: fundamentals are also
  ``not_applicable``, matching R03's constraint that MF/company fundamentals
  are a different concept entirely.
* ``EQUITY_F`` -- an equity with a real reviewed benchmark mapping and real
  Nifty 500 observations, but its own price series is missing the RS
  window's start-date observation: benchmark RS must stay ``missing``
  (a misaligned endpoint), never interpolated or approximated.

Admission goes through the same test-only, unregistered fixture source
ID/policy pattern S01/S02 already established (real NSE/Nifty-500
supplied-use permission stays denied in production).
"""

from __future__ import annotations

from dataclasses import replace as dataclass_replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import pytest
from market_pipeline.domain.models import AssetClass, Instrument, SourceArtifact, SourcePolicy
from market_pipeline.jobs.publish import PublicationInputError
from market_pipeline.jobs.reference_candidate import (
    FUNDAMENTAL_METRICS,
    ReferenceRows,
    build_reference_rows,
)
from market_pipeline.jobs.source_inputs import (
    SourceInput,
    SourceInputRole,
    resolve_supplied_source_input,
)
from market_pipeline.sources.registry import admit, get_source_policy
from market_pipeline.storage.raw_store import _object_key

EFFECTIVE_DATE = date(2026, 9, 11)
RS_WINDOW_DAYS = 91  # matches BENCHMARK_WINDOWS_DAYS's "benchmark_rs_3m" entry
RS_START_DATE = EFFECTIVE_DATE - timedelta(days=RS_WINDOW_DAYS)

FIXTURE_SOURCE_ID = "test-reference-candidate-fixture"
FIXTURE_SOURCE_URL = "https://fixture.test.invalid/official/reference"
FIXTURE_TERMS_URL = "https://fixture.test.invalid/terms"
FIXTURE_PERMISSION_REFERENCE = "test-reference-candidate-permission-001"


def _fixture_policy(**overrides: object) -> SourcePolicy:
    defaults: dict[str, object] = dict(
        source_id=FIXTURE_SOURCE_ID,
        source_url=FIXTURE_SOURCE_URL,
        terms_url=FIXTURE_TERMS_URL,
        automation_allowed=False,
        retention_allowed=True,
        supplied_use_allowed=True,
        supplied_use_permission_reference=FIXTURE_PERMISSION_REFERENCE,
        approved_url_prefixes=(FIXTURE_SOURCE_URL,),
        description="Test-only fixture proving reference-candidate composition end to end.",
    )
    defaults.update(overrides)
    return SourcePolicy.model_validate(defaults)


class _FakeRawStore:
    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}

    def seed(self, source_input: SourceInput, body: bytes) -> None:
        self._objects[source_input.object_key] = body

    def put(self, artifact: SourceArtifact, body: bytes) -> str:  # pragma: no cover - unused
        raise NotImplementedError("test double is seeded directly from admitted SourceInputs")

    def get(self, object_key: str) -> bytes:
        return self._objects[object_key]


def _instrument_id(isin: str, asset_class: AssetClass) -> str:
    return str(Instrument.from_provider("nse", isin, asset_class).instrument_id)


EQUITY_A_ID = _instrument_id("INE0EQUITYA01", AssetClass.EQUITY)
EQUITY_B_ID = _instrument_id("INE0EQUITYB01", AssetClass.EQUITY)
EQUITY_F_ID = _instrument_id("INE0EQUITYF01", AssetClass.EQUITY)
ETF_C_ID = _instrument_id("INF0ETFC00001", AssetClass.ETF)
MUTUAL_FUND_D_ID = _instrument_id("MFSCHEME00001", AssetClass.MUTUAL_FUND)

INSTRUMENT_IDS = {
    EQUITY_A_ID: "equity",
    EQUITY_B_ID: "equity",
    EQUITY_F_ID: "equity",
    ETF_C_ID: "etf",
    MUTUAL_FUND_D_ID: "mutual_fund",
}


def _period_id(instrument_id: str, period_end: str, period_type: str, filing_id: str) -> str:
    """Mirror ``normalization.fundamentals._period_identity``'s formula exactly."""

    return str(uuid5(NAMESPACE_URL, f"stonks/fundamental/{instrument_id}|{period_end}|{period_type}|{filing_id}"))


ORIGINAL_FILING_ID = "equity-a-q2-fy27-original"
RESTATED_FILING_ID = "equity-a-q2-fy27-restated"
ORIGINAL_PERIOD_ID = _period_id(EQUITY_A_ID, "2026-03-31", "quarter", ORIGINAL_FILING_ID)


def _filings_csv() -> bytes:
    header = "SYMBOL,PERIOD_END,PERIOD_TYPE,FILING_ID,FILED_AT,RESTATES_ID,REVENUE,NET_PROFIT,EPS"
    rows = [
        f"{EQUITY_A_ID},2026-03-31,quarter,{ORIGINAL_FILING_ID},2026-05-01T00:00:00+00:00,,100.00,12.00,2.00",
        f"{EQUITY_A_ID},2026-03-31,quarter,{RESTATED_FILING_ID},2026-06-01T00:00:00+00:00,"
        f"{ORIGINAL_PERIOD_ID},110.00,14.00,2.30",
    ]
    return ("\n".join([header, *rows]) + "\n").encode("utf-8")


def _benchmark_csv() -> bytes:
    header = "Date,Close"
    rows = [
        f"{RS_START_DATE.isoformat()},10000.00",
        f"{EFFECTIVE_DATE.isoformat()},11000.00",
    ]
    return ("\n".join([header, *rows]) + "\n").encode("utf-8")


def _bhavcopy_row(symbol: str, isin: str, close: str, day: date) -> str:
    return f"{symbol},EQ,,{symbol} Limited,{isin},{close},10000,{day:%d-%b-%Y}"


def _bhavcopy_csv(day: date, *, include_equity_f: bool) -> bytes:
    header = "SYMBOL,SERIES,TYPE,NAME OF COMPANY,ISIN,CLOSE,VOLUME,TIMESTAMP"
    rows = [
        _bhavcopy_row("EQUITYA", "INE0EQUITYA01", "500" if day == EFFECTIVE_DATE else "400", day),
        _bhavcopy_row("EQUITYB", "INE0EQUITYB01", "50", day),
    ]
    if include_equity_f:
        rows.append(_bhavcopy_row("EQUITYF", "INE0EQUITYF01", "200", day))
    return ("\n".join([header, *rows]) + "\n").encode("utf-8")


def _mappings_json(path: Path) -> Path:
    path.write_text(
        f"""
        [
          {{
            "identifier": "{EQUITY_A_ID}",
            "identifier_type": "instrument",
            "benchmark_id": "NIFTY500",
            "valid_from": "2020-01-01",
            "valid_to": null,
            "source_reference": "https://www.niftyindices.com/reports/historical-data"
          }},
          {{
            "identifier": "{EQUITY_F_ID}",
            "identifier_type": "instrument",
            "benchmark_id": "NIFTY500",
            "valid_from": "2020-01-01",
            "valid_to": null,
            "source_reference": "https://www.niftyindices.com/reports/historical-data"
          }}
        ]
        """,
        encoding="utf-8",
    )
    return path


def _empty_mappings_json(path: Path) -> Path:
    path.write_text("[]", encoding="utf-8")
    return path


def _admit_filings_input(raw_store: _FakeRawStore, body: bytes, effective_date: date) -> SourceInput:
    """Hand-construct a ``role=FILINGS``/``source_id="nse-filings-xbrl"`` input.

    ``reference_candidate.py``'s new role/source-ID guard requires this exact
    literal source ID for the FILINGS role (see ``_require_role_source_id``).
    Real NSE filings admission is denied in production today (S01: neither
    ``automation_allowed`` nor ``supplied_use_allowed`` is set for
    ``nse-filings-xbrl``), so there is no legitimate way to exercise
    ``admit()`` for this source ID at all yet -- this directly constructs the
    ``SourceInput`` `build_reference_rows` expects as its own precondition
    (an already-admitted artifact), to test its composition logic in
    isolation from S01's separately-tested, currently-fully-closed admission
    gate for this specific source.
    """

    checksum = sha256(body).hexdigest()
    filename = f"nse-filings-xbrl-{effective_date.isoformat()}.csv"
    source_input = SourceInput(
        source_id="nse-filings-xbrl",
        role=SourceInputRole.FILINGS,
        expected_date=effective_date,
        loaded_date=effective_date,
        artifact_id=uuid5(NAMESPACE_URL, f"stonks/test-fixture-artifact/{filename}/{checksum}"),
        checksum=checksum,
        object_key=f"raw/nse-filings-xbrl/{effective_date.isoformat()}/{checksum}/{filename}",
        adapter_version="1.0.0",
        acquisition_mode="supplied",
    )
    raw_store.seed(source_input, body)
    return source_input


def _admit_benchmark_input(raw_store: _FakeRawStore, body: bytes, effective_date: date) -> SourceInput:
    """Admit a real ``nifty-500`` artifact through the genuine ``admit()`` gate.

    Unlike NSE filings, ``nifty-500``'s registry entry already has
    ``automation_allowed=True`` (see ``sources/registry.py``), so a
    network-mode artifact using the real, unmodified canonical policy is
    legitimately admittable today -- no test-only fixture policy is needed.
    This does not perform an actual network fetch; it only proves what a
    future network adapter's admitted output would look like.
    """

    policy = get_source_policy("nifty-500")
    filename = f"nifty500-{effective_date.isoformat()}.csv"
    artifact = SourceArtifact(
        source_id="nifty-500",
        source_url=f"{policy.source_url}/{filename}",
        retrieved_at=datetime.now(timezone.utc),
        effective_date=effective_date,
        checksum=sha256(body).hexdigest(),
        adapter_version="1.0.0",
        terms_url=policy.terms_url,
        filename=filename,
    )
    admitted = admit(artifact, policy)
    assert admitted.artifact_id is not None
    source_input = SourceInput(
        source_id="nifty-500",
        role=SourceInputRole.BENCHMARK_OBSERVATIONS,
        expected_date=effective_date,
        loaded_date=effective_date,
        artifact_id=admitted.artifact_id,
        checksum=admitted.checksum,
        object_key=_object_key(admitted),
        adapter_version="1.0.0",
        acquisition_mode="network",
    )
    raw_store.seed(source_input, body)
    return source_input


@pytest.fixture(scope="module")
def reference_universe(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    manifest_root = tmp_path_factory.mktemp("reference-candidate-manifest")
    raw_store = _FakeRawStore()
    source_ids = {
        SourceInputRole.EOD_OBSERVATIONS: f"{FIXTURE_SOURCE_ID}-eod",
    }
    policies = {role: _fixture_policy(source_id=source_id) for role, source_id in source_ids.items()}
    inputs: list[SourceInput] = []

    def _admit(*, role: SourceInputRole, relative_path: str, effective: date, filename: str) -> SourceInput:
        _, body, source_input = resolve_supplied_source_input(
            manifest_root=manifest_root,
            source_id=source_ids[role],
            role=role,
            relative_path=relative_path,
            expected_date=effective,
            effective_date=effective,
            source_url=f"{FIXTURE_SOURCE_URL}/{filename}",
            terms_url=FIXTURE_TERMS_URL,
            adapter_version="1.0.0",
            policy=policies[role],
            permission_record_id=FIXTURE_PERMISSION_REFERENCE,
            allow_unregistered_source=True,
        )
        raw_store.seed(source_input, body)
        return source_input

    inputs.append(_admit_filings_input(raw_store, _filings_csv(), EFFECTIVE_DATE))
    inputs.append(_admit_benchmark_input(raw_store, _benchmark_csv(), EFFECTIVE_DATE))

    # EQUITY_A/EQUITY_B get both RS-window endpoints; EQUITY_F only gets the
    # effective-date session (its start-of-window observation is missing).
    (manifest_root / "eod-start.csv").write_bytes(_bhavcopy_csv(RS_START_DATE, include_equity_f=False))
    inputs.append(
        _admit(role=SourceInputRole.EOD_OBSERVATIONS, relative_path="eod-start.csv", effective=RS_START_DATE, filename="eod-start.csv")
    )
    (manifest_root / "eod-effective.csv").write_bytes(_bhavcopy_csv(EFFECTIVE_DATE, include_equity_f=True))
    inputs.append(
        _admit(
            role=SourceInputRole.EOD_OBSERVATIONS,
            relative_path="eod-effective.csv",
            effective=EFFECTIVE_DATE,
            filename="eod-effective.csv",
        )
    )

    mappings_path = _mappings_json(tmp_path_factory.mktemp("reference-candidate-mappings") / "mappings.json")
    empty_mappings_path = _empty_mappings_json(
        tmp_path_factory.mktemp("reference-candidate-empty-mappings") / "mappings.json"
    )

    return {
        "inputs": inputs,
        "raw_store": raw_store,
        "mappings_path": mappings_path,
        "empty_mappings_path": empty_mappings_path,
    }


def _build(universe: dict[str, Any], *, mappings_path: Path | None = None) -> ReferenceRows:
    return build_reference_rows(
        universe["inputs"],
        universe["raw_store"],
        INSTRUMENT_IDS,
        EFFECTIVE_DATE,
        mappings_path=mappings_path or universe["mappings_path"],
    )


# --- TDD anchor -------------------------------------------------------------


def test_build_reference_rows_is_importable() -> None:
    assert callable(build_reference_rows)


# --- Oracle assertions (verbatim from the plan) -----------------------------


def test_restatement_supersedes_original(reference_universe: dict[str, Any]) -> None:
    rows = _build(reference_universe)
    by_filing_id = {period["filing_id"]: period for period in rows.fundamental_periods}
    original_period = by_filing_id[ORIGINAL_FILING_ID]
    restated_period = by_filing_id[RESTATED_FILING_ID]
    assert original_period["active"] is False
    assert restated_period["active"] is True
    assert restated_period["supersedes_id"] == original_period["period_id"]


def test_restated_value_is_published_not_the_original(reference_universe: dict[str, Any]) -> None:
    rows = _build(reference_universe)
    metrics = {m["metric"]: m for m in rows.fundamental_metrics if m["instrument_id"] == EQUITY_A_ID}
    assert metrics["fundamental_revenue"]["state"] == "present"
    assert Decimal(metrics["fundamental_revenue"]["raw_value"]) == Decimal("110.00")


def test_missing_equity_filing_metric_is_honestly_missing(reference_universe: dict[str, Any]) -> None:
    rows = _build(reference_universe)
    metrics = {m["metric"]: m for m in rows.fundamental_metrics if m["instrument_id"] == EQUITY_B_ID}
    missing_equity_filing_metric = metrics["fundamental_revenue"]
    assert missing_equity_filing_metric["state"] == "missing"
    assert missing_equity_filing_metric["value"] is None


def test_etf_company_metric_is_not_applicable(reference_universe: dict[str, Any]) -> None:
    rows = _build(reference_universe)
    metrics = {m["metric"]: m for m in rows.fundamental_metrics if m["instrument_id"] == ETF_C_ID}
    for field in FUNDAMENTAL_METRICS:
        assert metrics[f"fundamental_{field}"]["state"] == "not_applicable"


def test_mutual_fund_company_metric_is_not_applicable(reference_universe: dict[str, Any]) -> None:
    rows = _build(reference_universe)
    metrics = {m["metric"]: m for m in rows.fundamental_metrics if m["instrument_id"] == MUTUAL_FUND_D_ID}
    mutual_fund_company_metric = metrics["fundamental_revenue"]
    assert mutual_fund_company_metric["state"] == "not_applicable"


def test_benchmark_rs_present_with_aligned_endpoints(reference_universe: dict[str, Any]) -> None:
    rows = _build(reference_universe)
    metrics = {m["metric"]: m for m in rows.benchmark_metrics if m["instrument_id"] == EQUITY_A_ID}
    row = metrics["benchmark_rs_3m"]
    assert row["state"] == "present"
    rs_asset_start = date.fromisoformat(row["metadata"]["rs_asset_start"])
    rs_asset_end = date.fromisoformat(row["metadata"]["rs_asset_end"])
    rs_benchmark_start = date.fromisoformat(row["metadata"]["rs_benchmark_start"])
    rs_benchmark_end = date.fromisoformat(row["metadata"]["rs_benchmark_end"])
    assert rs_asset_start == rs_benchmark_start
    assert rs_asset_end == rs_benchmark_end
    # Hand-verified: asset 400 -> 500 (+25%), benchmark 10000 -> 11000 (+10%).
    from decimal import ROUND_HALF_EVEN

    expected = ((Decimal("1.25")) / (Decimal("1.10")) - Decimal(1)).quantize(
        Decimal("0.00000001"), rounding=ROUND_HALF_EVEN
    )
    assert Decimal(row["raw_value"]) == expected


def test_benchmark_rs_without_mapping_is_missing(reference_universe: dict[str, Any]) -> None:
    rows = _build(reference_universe)
    metrics = {m["metric"]: m for m in rows.benchmark_metrics if m["instrument_id"] == EQUITY_B_ID}
    benchmark_rs_without_mapping = metrics["benchmark_rs_3m"]
    assert benchmark_rs_without_mapping["state"] == "missing"
    assert "mapping" in benchmark_rs_without_mapping["metadata"]["reason"]


def test_benchmark_rs_with_misaligned_asset_endpoint_is_missing(reference_universe: dict[str, Any]) -> None:
    rows = _build(reference_universe)
    metrics = {m["metric"]: m for m in rows.benchmark_metrics if m["instrument_id"] == EQUITY_F_ID}
    row = metrics["benchmark_rs_3m"]
    assert row["state"] == "missing"
    assert row["value"] is None
    assert "start" in row["metadata"]["reason"]


def test_empty_mapping_file_is_valid_and_produces_missing_benchmark_rs(reference_universe: dict[str, Any]) -> None:
    rows = _build(reference_universe, mappings_path=reference_universe["empty_mappings_path"])
    assert all(row["state"] == "missing" for row in rows.benchmark_metrics)
    assert any("empty" in warning for warning in rows.warnings)


def test_benchmark_observations_are_recorded(reference_universe: dict[str, Any]) -> None:
    rows = _build(reference_universe)
    dates = {obs["observation_date"] for obs in rows.benchmark_observations}
    assert RS_START_DATE.isoformat() in dates
    assert EFFECTIVE_DATE.isoformat() in dates


def test_input_manifest_records_new_reference_versions(reference_universe: dict[str, Any]) -> None:
    rows = _build(reference_universe)
    versions = rows.input_manifest["versions"]
    assert "reference-filings-normalization-v1" == versions["normalization"]
    assert "reference-benchmark-rs-v1" in versions["analytics"]
    assert versions["projection"] == "reference-projection-v1"


# --- Fail-closed: malformed reviewed mapping file ---------------------------


def test_malformed_mapping_file_fails_closed(reference_universe: dict[str, Any], tmp_path: Path) -> None:
    bad_path = tmp_path / "bad-mappings.json"
    bad_path.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(PublicationInputError):
        _build(reference_universe, mappings_path=bad_path)


# --- Fail-closed: a role attached to the wrong official source ID ----------


def test_role_source_id_mismatch_fails_closed(reference_universe: dict[str, Any]) -> None:
    """A wiring bug that mislabels an admitted artifact's role must not be
    silently trusted as data from that role's official source.

    Takes the genuinely-admitted, real ``nifty-500`` `SourceInput` and
    relabels its `role` as `FILINGS` -- a caller bug that could otherwise
    happen independently of `source_id` (per `SourceInputRole`'s own
    docstring: role is "not inferable from source_id alone"). This must be
    rejected by `_require_role_source_id`, not silently treated as an
    official NSE filing.
    """

    universe = reference_universe
    real_benchmark_input = next(
        item for item in universe["inputs"] if item.role is SourceInputRole.BENCHMARK_OBSERVATIONS
    )
    mislabeled = dataclass_replace(real_benchmark_input, role=SourceInputRole.FILINGS)

    with pytest.raises(PublicationInputError, match="source_id"):
        build_reference_rows(
            [mislabeled],
            universe["raw_store"],
            INSTRUMENT_IDS,
            EFFECTIVE_DATE,
            mappings_path=universe["mappings_path"],
        )
