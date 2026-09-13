"""S04: compose AMFI + NSE equity/ETF + filings/benchmark into ONE
``DatasetBuild`` with independently-persisted per-source health, and prove
the composed candidate round-trips through R07's real bundle/export path
into a compact, Worker-queryable D1 schema.

Scenario coverage (the plan's own oracle, adapted to this repo's real
module shapes -- see ``task-S04-report.md`` for the exact mapping):

* ``mixed_candidate.asset_classes == {'equity', 'etf', 'mutual_fund'}`` --
  :func:`compose_datasets` merging one AMFI mutual-fund build with one NSE
  equity+ETF build.
* ``status['amfi-nav'].loaded_date < status['nse-eod'].loaded_date`` and
  ``status['amfi-nav'].state == 'delayed'`` -- a day where NSE published on
  time but no fresh AMFI file was supplied (AMFI's checkpoint reuses its
  last available artifact, which is now older than the requested date).
* ``failed_required_source.active_dataset_id == last_good_dataset_id`` -- a
  day where NSE (a required source) has no EOD input at all: the composed
  build fails, promotion is blocked, and the previously active dataset stays
  active.
* ``no_manifest_refresh.status == 'skipped'`` -- a day nothing is scheduled
  to publish at all (a weekend, with no source's calendar expecting
  anything): the run never touches the publisher and is reported
  ``"skipped"``, never a false completed refresh.

Admission for NSE/reference inputs goes through the same test-only,
unregistered fixture source ID/policy pattern S01/S02/S03 already
established (real NSE supplied-use permission stays denied in production).
AMFI goes through the real, registered ``amfi-nav`` checkpoint/raw-store path
(``run_daily``/``LocalRawStore``), exactly as the existing CLI integration
tests do -- this is deliberately the one component of this test that is NOT
a bespoke fixture double, to prove composition works against the pipeline's
own real, already-hardened AMFI path, not just doubles all the way down.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from market_pipeline.domain.models import (
    AssetClass,
    FetchedArtifact,
    Instrument,
    SourceArtifact,
    SourcePolicy,
)
from market_pipeline.jobs.composed_candidate import (
    ComposedDatasetBuild,
    CompositionError,
    SourceScheduleEntry,
    build_composed_candidate,
    compose_datasets,
    run_scheduled_refresh,
)
from market_pipeline.jobs.daily import run_daily
from market_pipeline.jobs.publish import build_candidate
from market_pipeline.jobs.source_inputs import (
    SourceInput,
    SourceInputRole,
    resolve_supplied_source_input,
)
from market_pipeline.publication.bundle import BundleObject, build_bundle
from market_pipeline.publication.d1_export import export_active_dataset
from market_pipeline.sources.registry import admit, get_source_policy
from market_pipeline.storage.d1_publisher import D1Publisher, publish
from market_pipeline.storage.history_store import HistoryStore
from market_pipeline.storage.raw_store import LocalRawStore, _object_key

AMFI_TERMS_URL = "https://www.amfiindia.com/terms-and-conditions"
AMFI_SOURCE_URL = "https://www.amfiindia.com/spages/NAVAll.txt"

FIXTURE_SOURCE_ID = "test-composed-fixture"
FIXTURE_SOURCE_URL = "https://fixture.test.invalid/official/composed"
FIXTURE_TERMS_URL = "https://fixture.test.invalid/terms"
FIXTURE_PERMISSION_REFERENCE = "test-composed-permission-001"

T0 = date(2026, 9, 10)  # Thursday
T1 = date(2026, 9, 11)  # Friday
WEEKEND = date(2026, 9, 12)  # Saturday -- nothing is scheduled
T2 = date(2026, 9, 14)  # Monday -- NSE (required) has no input at all


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
        description="Test-only fixture proving S04 composed publication end to end.",
    )
    defaults.update(overrides)
    return SourcePolicy.model_validate(defaults)


class _FakeRawStore:
    """Minimal ``RawStore``-Protocol double for admitted NSE/reference bodies."""

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}

    def seed(self, source_input: SourceInput, body: bytes) -> None:
        self._objects[source_input.object_key] = body

    def put(self, artifact: SourceArtifact, body: bytes) -> str:  # pragma: no cover - unused
        raise NotImplementedError("test double is seeded directly from admitted SourceInputs")

    def get(self, object_key: str) -> bytes:
        return self._objects[object_key]


class _UnionRawStore:
    """Test-only shim: reads/writes AMFI through the real store, NSE/reference
    through the fixture double -- ``build_composed_candidate`` only ever
    takes one ``raw_store`` argument, but this test's two component paths
    (a real registered source vs. a test-only unregistered one) legitimately
    use two different concrete stores in this repo's existing test suite.
    """

    def __init__(self, primary: LocalRawStore, fallback: _FakeRawStore) -> None:
        self._primary = primary
        self._fallback = fallback

    def put(self, artifact: SourceArtifact, body: bytes) -> str:
        return self._primary.put(artifact, body)

    def get(self, object_key: str) -> bytes:
        try:
            return self._primary.get(object_key)
        except (FileNotFoundError, OSError, KeyError):
            return self._fallback.get(object_key)


def _nav_report(effective_date: date, nav: str = "100.5") -> bytes:
    day = effective_date.strftime("%d-%b-%Y")
    return (
        "Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;"
        "Scheme Name;Net Asset Value;Date\n"
        "\n"
        "Alpha Asset Management Mutual Fund\n"
        "\n"
        "Open Ended Schemes(Equity Scheme - Large Cap Fund)\n"
        f"119551;INF119551AA1;INF119551AB9;Alpha Bluechip Fund - Direct Plan - Growth;{nav};{day}\n"
    ).encode("utf-8")


def _amfi_fetcher(bodies_by_date: dict[date, bytes]) -> Any:
    import hashlib

    fetched_by_key: dict[tuple[str, str], list[FetchedArtifact]] = {}
    for effective, body in bodies_by_date.items():
        artifact = SourceArtifact(
            source_id="amfi-nav",
            source_url=AMFI_SOURCE_URL,
            retrieved_at=datetime.now(UTC),
            effective_date=effective,
            checksum=hashlib.sha256(body).hexdigest(),
            adapter_version="v1",
            terms_url=AMFI_TERMS_URL,
            filename="NAVAll.txt",
        )
        fetched_by_key[("amfi-nav", effective.isoformat())] = [FetchedArtifact(artifact=artifact, body=body)]

    def fetch(source: str, effective: date) -> list[FetchedArtifact]:
        return list(fetched_by_key.get((source, effective.isoformat()), ()))

    return fetch


def _instrument_id(isin: str, asset_class: AssetClass) -> str:
    return str(Instrument.from_provider("nse", isin, asset_class).instrument_id)


EQUITY_ID = _instrument_id("INE0COMPOSEDEQ1", AssetClass.EQUITY)
ETF_ID = _instrument_id("INF0COMPOSEDET1", AssetClass.ETF)


def _bhavcopy_row(symbol: str, isin: str, close: str, day: date) -> str:
    return f"{symbol},EQ,,{symbol} Limited,{isin},{close},10000,{day:%d-%b-%Y}"


def _bhavcopy_csv(day: date) -> bytes:
    header = "SYMBOL,SERIES,TYPE,NAME OF COMPANY,ISIN,CLOSE,VOLUME,TIMESTAMP"
    rows = [
        _bhavcopy_row("CEQUITY", "INE0COMPOSEDEQ1", "110" if day == T0 else "100", day),
        _bhavcopy_row("CETF", "INF0COMPOSEDET1", "55" if day == T0 else "50", day),
    ]
    return ("\n".join([header, *rows]) + "\n").encode("utf-8")


def _security_master_csv() -> bytes:
    header = "SYMBOL,SERIES,TYPE,NAME OF COMPANY,ISIN NUMBER"
    rows = [
        "CEQUITY,EQ,,Composed Equity Limited,INE0COMPOSEDEQ1",
        "CETF,EQ,ETF,Composed ETF,INF0COMPOSEDET1",
    ]
    return ("\n".join([header, *rows]) + "\n").encode("utf-8")


def _filings_csv() -> bytes:
    header = "SYMBOL,PERIOD_END,PERIOD_TYPE,FILING_ID,FILED_AT,RESTATES_ID,REVENUE,NET_PROFIT,EPS"
    row = f"{EQUITY_ID},2026-06-30,quarter,composed-eq-q1-fy27,2026-07-15T00:00:00+00:00,,250.00,30.00,4.10"
    return ("\n".join([header, row]) + "\n").encode("utf-8")


RS_WINDOW_DAYS = 91
RS_START_DATE = T0 - timedelta(days=RS_WINDOW_DAYS)


def _benchmark_csv() -> bytes:
    header = "Date,Close"
    rows = [f"{RS_START_DATE.isoformat()},10000.00", f"{T0.isoformat()},11000.00"]
    return ("\n".join([header, *rows]) + "\n").encode("utf-8")


def _mappings_json(path: Path) -> Path:
    path.write_text(
        f"""
        [
          {{
            "identifier": "{EQUITY_ID}",
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


def _admit_benchmark_input(raw_store: _FakeRawStore, body: bytes, effective_date: date) -> SourceInput:
    """Admit a real ``nifty-500`` artifact through the genuine ``admit()`` gate
    (its registry entry already has ``automation_allowed=True`` -- see
    S03's own test for the identical pattern and rationale)."""

    policy = get_source_policy("nifty-500")
    filename = f"nifty500-{effective_date.isoformat()}.csv"
    from hashlib import sha256

    artifact = SourceArtifact(
        source_id="nifty-500",
        source_url=f"{policy.source_url}/{filename}",
        retrieved_at=datetime.now(UTC),
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


def _admit_filings_input(raw_store: _FakeRawStore, body: bytes, effective_date: date) -> SourceInput:
    """Hand-construct a real-source-ID FILINGS input (mirrors S03's own test:
    real NSE filings admission is fully closed today, so this tests
    composition in isolation from S01's separately-tested admission gate)."""

    from hashlib import sha256
    from uuid import NAMESPACE_URL, uuid5

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


def _admit_nse(
    manifest_root: Path,
    raw_store: _FakeRawStore,
    policies: dict[SourceInputRole, SourcePolicy],
    source_ids: dict[SourceInputRole, str],
    *,
    role: SourceInputRole,
    relative_path: str,
    body: bytes,
    effective: date,
) -> SourceInput:
    (manifest_root / relative_path).parent.mkdir(parents=True, exist_ok=True)
    (manifest_root / relative_path).write_bytes(body)
    _, resolved_body, source_input = resolve_supplied_source_input(
        manifest_root=manifest_root,
        source_id=source_ids[role],
        role=role,
        relative_path=relative_path,
        expected_date=effective,
        effective_date=effective,
        source_url=f"{FIXTURE_SOURCE_URL}/{relative_path}",
        terms_url=FIXTURE_TERMS_URL,
        adapter_version="1.0.0",
        policy=policies[role],
        permission_record_id=FIXTURE_PERMISSION_REFERENCE,
        allow_unregistered_source=True,
    )
    raw_store.seed(source_input, resolved_body)
    return source_input


@pytest.fixture()
def composed_universe(tmp_path: Path) -> dict[str, Any]:
    """One compact universe: AMFI mutual fund + NSE equity/ETF + filings +
    benchmark, all dated T0. Used for composition-shape and R07 cross-boundary
    assertions (not the multi-day schedule scenarios -- see ``schedule_env``).
    """

    connection = sqlite3.connect(":memory:")
    publisher = D1Publisher(connection)
    publisher.initialize_schema()
    local_raw = LocalRawStore(tmp_path / "raw")
    fixture_raw = _FakeRawStore()
    raw_store = _UnionRawStore(local_raw, fixture_raw)

    fetcher = _amfi_fetcher({T0: _nav_report(T0, "100.50")})
    run_daily(connection, fetcher, ["amfi-nav"], effective_date=T0, raw_store=local_raw, strict_coverage=False)

    manifest_root = tmp_path / "nse-manifest"
    source_ids = {
        SourceInputRole.EOD_OBSERVATIONS: f"{FIXTURE_SOURCE_ID}-eod",
        SourceInputRole.SECURITY_MASTER: f"{FIXTURE_SOURCE_ID}-master",
    }
    policies = {role: _fixture_policy(source_id=sid) for role, sid in source_ids.items()}
    nse_inputs = [
        _admit_nse(
            manifest_root, fixture_raw, policies, source_ids,
            role=SourceInputRole.EOD_OBSERVATIONS, relative_path="eod-t0.csv", body=_bhavcopy_csv(T0), effective=T0,
        ),
        _admit_nse(
            manifest_root, fixture_raw, policies, source_ids,
            role=SourceInputRole.SECURITY_MASTER, relative_path="master-t0.csv", body=_security_master_csv(), effective=T0,
        ),
    ]

    filings_input = _admit_filings_input(fixture_raw, _filings_csv(), T0)
    benchmark_input = _admit_benchmark_input(fixture_raw, _benchmark_csv(), T0)
    # The RS window's start-date EOD observation must also be admitted so
    # S02's own NSE build has a price point at RS_START_DATE too -- reference
    # rows now consume S02's corporate-action-adjusted series verbatim (see
    # the final-review fix for price-basis consistency) rather than
    # re-deriving one independently, so this date must be part of the NSE
    # build's own `nse_inputs`, not only `reference_inputs`.
    rs_start_eod = _admit_nse(
        manifest_root, fixture_raw, policies, source_ids,
        role=SourceInputRole.EOD_OBSERVATIONS, relative_path="eod-rs-start.csv",
        body=_bhavcopy_csv(RS_START_DATE), effective=RS_START_DATE,
    )
    nse_inputs.append(rs_start_eod)
    reference_inputs = [filings_input, benchmark_input, rs_start_eod, nse_inputs[0]]

    mappings_path = _mappings_json(tmp_path / "mappings.json")

    return {
        "connection": connection,
        "publisher": publisher,
        "raw_store": raw_store,
        "amfi_source_ids": ["amfi-nav"],
        "nse_inputs": nse_inputs,
        "reference_inputs": reference_inputs,
        "mappings_path": mappings_path,
    }


def _build(universe: dict[str, Any]) -> ComposedDatasetBuild:
    return build_composed_candidate(
        universe["connection"],
        universe["raw_store"],
        amfi_source_ids=universe["amfi_source_ids"],
        nse_inputs=universe["nse_inputs"],
        reference_inputs=universe["reference_inputs"],
        effective_date=T0,
        mappings_path=universe["mappings_path"],
    )


# --- TDD anchor --------------------------------------------------------------


def test_build_composed_candidate_is_importable() -> None:
    assert callable(build_composed_candidate)
    assert callable(compose_datasets)
    assert callable(run_scheduled_refresh)


# --- Oracle: mixed_candidate.asset_classes -----------------------------------


def test_composed_asset_classes_are_equity_etf_and_mutual_fund(composed_universe: dict[str, Any]) -> None:
    build = _build(composed_universe)
    assert build.asset_classes == {"equity", "etf", "mutual_fund"}


def test_composed_instruments_include_every_component(composed_universe: dict[str, Any]) -> None:
    build = _build(composed_universe)
    instrument_ids = {row["instrument_id"] for row in build.candidate["tables"]["instruments"]}
    assert EQUITY_ID in instrument_ids
    assert ETF_ID in instrument_ids
    assert len(instrument_ids) == 3  # equity + etf + the one AMFI scheme


def test_reference_rows_replace_nse_placeholder_metrics_not_append(composed_universe: dict[str, Any]) -> None:
    build = _build(composed_universe)
    metrics = build.candidate["tables"]["latest_metrics"]
    fundamental_rows = [
        row for row in metrics if row["instrument_id"] == EQUITY_ID and row["metric"] == "fundamental_revenue"
    ]
    # Exactly one row per (instrument, metric) -- never a duplicate NSE
    # placeholder alongside S03's real computed row.
    assert len(fundamental_rows) == 1
    assert fundamental_rows[0]["state"] == "present"
    assert Decimal(fundamental_rows[0]["raw_value"]) == Decimal("250.00")

    benchmark_rows = [
        row for row in metrics if row["instrument_id"] == EQUITY_ID and row["metric"] == "benchmark_rs_3m"
    ]
    assert len(benchmark_rows) == 1
    assert benchmark_rows[0]["state"] == "present"
    expected_rs = (Decimal("110") / Decimal("100") - 1) - (Decimal("11000.00") / Decimal("10000.00") - 1)
    assert Decimal(benchmark_rows[0]["raw_value"]).quantize(Decimal("0.00000001")) == expected_rs.quantize(
        Decimal("0.00000001")
    )

    # The ETF never gets a fundamental value fabricated for it.
    etf_fundamental = next(
        row for row in metrics if row["instrument_id"] == ETF_ID and row["metric"] == "fundamental_revenue"
    )
    assert etf_fundamental["state"] == "not_applicable"


def test_composed_tables_carry_fundamental_periods_and_corporate_actions(composed_universe: dict[str, Any]) -> None:
    build = _build(composed_universe)
    tables = build.candidate["tables"]
    assert any(row["instrument_id"] == EQUITY_ID for row in tables["fundamental_periods"])
    # No corporate actions were supplied in this universe; the key must still
    # be a valid (possibly empty) table, never absent in a way that would
    # confuse a caller expecting the standard shape.
    assert "corporate_actions" not in tables or tables["corporate_actions"] == []


def test_composed_required_sources_never_include_supplementary_reference_sources(
    composed_universe: dict[str, Any],
) -> None:
    build = _build(composed_universe)
    required = set(build.candidate["metadata"]["required_sources"])
    assert "amfi-nav" in required
    assert any(sid.endswith("-eod") for sid in required)
    assert "nse-filings-xbrl" not in required
    assert "nifty-500" not in required


def test_amfi_only_composition_is_a_pure_passthrough(composed_universe: dict[str, Any]) -> None:
    amfi_build = build_candidate(
        composed_universe["connection"], composed_universe["raw_store"], ["amfi-nav"], effective_date=T0
    )
    composed = build_composed_candidate(
        composed_universe["connection"], composed_universe["raw_store"], amfi_source_ids=["amfi-nav"], effective_date=T0
    )
    def _without_timestamps(tables: dict[str, Any]) -> dict[str, Any]:
        return {
            name: [
                {k: v for k, v in row.items() if k not in ("completed_at", "started_at")}
                for row in rows
            ]
            for name, rows in tables.items()
        }

    assert composed.candidate["dataset_id"] == amfi_build.candidate["dataset_id"]
    assert _without_timestamps(composed.candidate["tables"]) == _without_timestamps(amfi_build.candidate["tables"])
    composed_metadata = {k: v for k, v in composed.candidate["metadata"].items() if k != "generated_at"}
    amfi_metadata = {k: v for k, v in amfi_build.candidate["metadata"].items() if k != "generated_at"}
    assert composed_metadata == amfi_metadata
    assert composed.histories == amfi_build.histories
    assert composed.momentum == amfi_build.momentum
    assert composed.asset_classes == {"mutual_fund"}


def test_compose_datasets_rejects_instrument_id_collision(composed_universe: dict[str, Any]) -> None:
    amfi_build = build_candidate(
        composed_universe["connection"], composed_universe["raw_store"], ["amfi-nav"], effective_date=T0
    )
    with pytest.raises(CompositionError):
        # Composing the same build against itself as both "amfi" and "nse"
        # is nonsensical, but it proves the collision guard fires on any
        # true duplicate instrument_id rather than trusting disjoint
        # provider namespaces blindly.
        compose_datasets(amfi=amfi_build, nse=amfi_build, effective_date=T0)


def test_reference_without_nse_is_rejected(composed_universe: dict[str, Any]) -> None:
    with pytest.raises(CompositionError):
        build_composed_candidate(
            composed_universe["connection"],
            composed_universe["raw_store"],
            amfi_source_ids=["amfi-nav"],
            reference_inputs=composed_universe["reference_inputs"],
            effective_date=T0,
        )


def test_compose_datasets_requires_at_least_one_component() -> None:
    with pytest.raises(CompositionError):
        build_composed_candidate(sqlite3.connect(":memory:"), _FakeRawStore(), effective_date=T0)


# --- R07 export -> real Worker (D1) schema -> query round trip --------------


def test_composed_candidate_exports_and_queries_through_the_real_d1_schema(
    composed_universe: dict[str, Any], tmp_path: Path
) -> None:
    build = _build(composed_universe)
    history_root = tmp_path / "history"
    history_store = HistoryStore(history_root)
    object_entries = []
    for instrument_id, (asset_class, points) in build.histories.items():
        records = [{"effective_date": d.isoformat(), "value": str(v)} for d, v in points]
        for partition in history_store.write_history(asset_class, instrument_id, records):
            object_entries.append(
                BundleObject(key=partition.key, sha256=partition.sha256, bytes=partition.bytes, kind="history")
            )
        chart = history_store.write_chart(
            build.candidate["dataset_id"], instrument_id,
            {"points": [{"date": r["effective_date"], "value": float(r["value"])} for r in records]},
        )
        object_entries.append(BundleObject(key=chart.key, sha256=chart.sha256, bytes=chart.bytes, kind="chart"))

    bundle = build_bundle(
        dataset_id=build.candidate["dataset_id"],
        input_manifest_hash=build.candidate["metadata"]["input_manifest_sha256"],
        source_dates=[T0.isoformat(), RS_START_DATE.isoformat()],
        projection_version="composed-dataset-v1",
        candidate_entries=object_entries,
        effective_date=T0,
    )
    build.candidate["metadata"]["publication_bundle"] = bundle.as_dict()

    local = sqlite3.connect(":memory:")
    local_publisher = D1Publisher(local)
    local_publisher.initialize_schema()
    publish(build.candidate, local_publisher, momentum_scores=build.momentum, source_artifact_ids=build.source_artifact_ids)

    sql_path = tmp_path / "export.sql"
    export_active_dataset(local, sql_path, history_root=history_root, object_manifest=tmp_path / "manifest.json")

    remote = sqlite3.connect(":memory:")
    remote_publisher = D1Publisher(remote)
    remote_publisher.initialize_schema()
    remote.executescript(sql_path.read_text(encoding="utf-8"))

    assert remote_publisher.active_dataset_id() == build.candidate["dataset_id"]

    # Representative screen query across all three asset classes, matching
    # D1ResearchStore's own active=1/asset_class-IN idiom.
    rows = remote.execute(
        "SELECT instrument_id, asset_class FROM instrument_snapshots WHERE dataset_id = ? AND active = 1",
        (build.candidate["dataset_id"],),
    ).fetchall()
    remote_asset_classes = {str(row[1]) for row in rows}
    assert remote_asset_classes == {"equity", "etf", "mutual_fund"}

    # Independently verify the composed fundamental_revenue value survived
    # the export/import round trip through the compact JSON snapshot column.
    equity_row = remote.execute(
        "SELECT json_extract(metric_values_json, '$.fundamental_revenue') FROM instrument_snapshots "
        "WHERE dataset_id = ? AND instrument_id = ?",
        (build.candidate["dataset_id"], EQUITY_ID),
    ).fetchone()
    assert equity_row is not None
    assert Decimal(str(equity_row[0])) == Decimal("250.00")

    # S03's fundamental_periods (compacted into the snapshot's own JSON
    # column for the remote/compact schema -- the raw normalized table is
    # local-only, per publication.d1_export's own compaction design) also
    # made it through the export unchanged.
    snapshot_row = remote.execute(
        "SELECT fundamental_periods_json FROM instrument_snapshots WHERE dataset_id = ? AND instrument_id = ?",
        (build.candidate["dataset_id"], EQUITY_ID),
    ).fetchone()
    assert snapshot_row is not None
    assert "revenue" in snapshot_row[0]


# --- Multi-day schedule: source health/acquisition orchestration ------------


@pytest.fixture()
def schedule_env(tmp_path: Path) -> dict[str, Any]:
    """A three-day environment for the schedule-driven refresh scenarios:

    * T0: AMFI + NSE both publish on time -> first composed publication.
    * T1: NSE publishes on time; AMFI does not supply a new file (its
      checkpoint reuses T0's artifact) -> AMFI reports "delayed", NSE
      reports "complete", and the run still safely publishes (AMFI is not
      required to be exactly current for promotion -- only present).
    * T2: NSE (a required source) has no EOD input in the schedule at all
      -> the composed build fails, promotion is blocked, and the dataset
      that was active after T1 remains active.
    * ``WEEKEND``: no source's calendar expects anything -> "skipped".
    """

    connection = sqlite3.connect(":memory:")
    publisher = D1Publisher(connection)
    publisher.initialize_schema()
    local_raw = LocalRawStore(tmp_path / "raw")
    fixture_raw = _FakeRawStore()
    raw_store = _UnionRawStore(local_raw, fixture_raw)

    fetcher = _amfi_fetcher({T0: _nav_report(T0, "100.50")})  # T1/T2 deliberately absent
    run_daily(connection, fetcher, ["amfi-nav"], effective_date=T0, raw_store=local_raw, strict_coverage=False)

    manifest_root = tmp_path / "nse-manifest"
    source_ids = {
        SourceInputRole.EOD_OBSERVATIONS: f"{FIXTURE_SOURCE_ID}-eod",
        SourceInputRole.SECURITY_MASTER: f"{FIXTURE_SOURCE_ID}-master",
    }
    policies = {role: _fixture_policy(source_id=sid) for role, sid in source_ids.items()}

    def _nse_inputs_for(effective: date) -> list[SourceInput]:
        return [
            _admit_nse(
                manifest_root, fixture_raw, policies, source_ids,
                role=SourceInputRole.EOD_OBSERVATIONS, relative_path=f"eod-{effective.isoformat()}.csv",
                body=_bhavcopy_csv(effective), effective=effective,
            ),
            _admit_nse(
                manifest_root, fixture_raw, policies, source_ids,
                role=SourceInputRole.SECURITY_MASTER, relative_path=f"master-{effective.isoformat()}.csv",
                body=_security_master_csv(), effective=effective,
            ),
        ]

    return {
        "connection": connection,
        "publisher": publisher,
        "raw_store": raw_store,
        "local_raw": local_raw,
        "nse_inputs_t0": _nse_inputs_for(T0),
        "nse_inputs_t1": _nse_inputs_for(T1),
        "amfi_fetcher_state": {T0: _nav_report(T0, "100.50")},
    }


def _schedule(expected_amfi: date | None, expected_nse: date | None, effective: date) -> list[SourceScheduleEntry]:
    entries = []
    entries.append(
        SourceScheduleEntry("amfi-nav", "amfi-nav", "supplied", expected_amfi if expected_amfi == effective else None)
    )
    entries.append(
        SourceScheduleEntry(
            f"{FIXTURE_SOURCE_ID}-eod", "nse-eod", "supplied", expected_nse if expected_nse == effective else None
        )
    )
    return entries


def test_first_composed_refresh_publishes_and_all_sources_are_complete(schedule_env: dict[str, Any]) -> None:
    result = run_scheduled_refresh(
        schedule_env["connection"], schedule_env["publisher"], schedule_env["raw_store"],
        _schedule(T0, T0, T0), T0,
        amfi_source_ids=["amfi-nav"], nse_inputs=schedule_env["nse_inputs_t0"],
    )
    assert result.status == "published"
    assert result.source_status["amfi-nav"].state == "complete"
    assert result.source_status[f"{FIXTURE_SOURCE_ID}-eod"].state == "complete"
    assert result.active_dataset_id == result.dataset_id


def test_delayed_amfi_source_is_independent_of_a_complete_nse_source(schedule_env: dict[str, Any]) -> None:
    # Establish T0 first so there is a known-good baseline dataset.
    run_scheduled_refresh(
        schedule_env["connection"], schedule_env["publisher"], schedule_env["raw_store"],
        _schedule(T0, T0, T0), T0,
        amfi_source_ids=["amfi-nav"], nse_inputs=schedule_env["nse_inputs_t0"],
    )
    result = run_scheduled_refresh(
        schedule_env["connection"], schedule_env["publisher"], schedule_env["raw_store"],
        _schedule(T1, T1, T1), T1,
        amfi_source_ids=["amfi-nav"], nse_inputs=schedule_env["nse_inputs_t1"],
    )
    status = result.source_status
    amfi_loaded_date = status["amfi-nav"].loaded_date
    nse_loaded_date = status[f"{FIXTURE_SOURCE_ID}-eod"].loaded_date
    assert amfi_loaded_date is not None
    assert nse_loaded_date is not None
    # Oracle (verbatim intent from the plan, adapted to this repo's real
    # per-source category labels):
    assert amfi_loaded_date < nse_loaded_date
    assert status["amfi-nav"].state == "delayed"
    assert status[f"{FIXTURE_SOURCE_ID}-eod"].state == "complete"
    assert result.status == "published"


def test_a_corrected_retry_clears_the_delayed_state(schedule_env: dict[str, Any]) -> None:
    run_scheduled_refresh(
        schedule_env["connection"], schedule_env["publisher"], schedule_env["raw_store"],
        _schedule(T0, T0, T0), T0,
        amfi_source_ids=["amfi-nav"], nse_inputs=schedule_env["nse_inputs_t0"],
    )
    # First (delayed) attempt at T1.
    run_scheduled_refresh(
        schedule_env["connection"], schedule_env["publisher"], schedule_env["raw_store"],
        _schedule(T1, T1, T1), T1,
        amfi_source_ids=["amfi-nav"], nse_inputs=schedule_env["nse_inputs_t1"],
    )
    # Operator supplies the missing AMFI file for T1 and retries with an
    # identical schedule and the now-corrected source input.
    fetcher = _amfi_fetcher({T0: _nav_report(T0, "100.50"), T1: _nav_report(T1, "101.00")})
    run_daily(
        schedule_env["connection"], fetcher, ["amfi-nav"], effective_date=T1,
        raw_store=schedule_env["local_raw"], strict_coverage=False,
    )
    retried = run_scheduled_refresh(
        schedule_env["connection"], schedule_env["publisher"], schedule_env["raw_store"],
        _schedule(T1, T1, T1), T1,
        amfi_source_ids=["amfi-nav"], nse_inputs=schedule_env["nse_inputs_t1"],
    )
    assert retried.source_status["amfi-nav"].state == "complete"
    assert retried.source_status["amfi-nav"].loaded_date == T1


def test_failed_required_source_preserves_the_last_good_dataset(schedule_env: dict[str, Any]) -> None:
    first = run_scheduled_refresh(
        schedule_env["connection"], schedule_env["publisher"], schedule_env["raw_store"],
        _schedule(T0, T0, T0), T0,
        amfi_source_ids=["amfi-nav"], nse_inputs=schedule_env["nse_inputs_t0"],
    )
    last_good_dataset_id = first.active_dataset_id
    assert last_good_dataset_id is not None

    # T2: NSE is scheduled to publish but no NSE input was supplied at all.
    failed_required_source = run_scheduled_refresh(
        schedule_env["connection"], schedule_env["publisher"], schedule_env["raw_store"],
        _schedule(T2, T2, T2), T2,
        amfi_source_ids=[],  # AMFI is not expected on T2 in this schedule
        nse_inputs=[],
    )
    assert failed_required_source.status in ("blocked", "failed")
    assert failed_required_source.active_dataset_id == last_good_dataset_id


# --- Final-review fix: S02/S03 must share ONE adjusted price basis --------


def test_composed_benchmark_rs_uses_the_same_adjusted_series_as_return_metrics(tmp_path: Path) -> None:
    """A 2:1 split inside the RS window must be reflected identically in
    `benchmark_rs_*` (S03) and `return_*` (S02) for the same instrument --
    never a raw, unadjusted price basis for one and an adjusted basis for
    the other.

    The raw close jumps 220 -> 110 across the split (a fake ~-50% "return"
    if left unadjusted); the corporate-action-adjusted close is flat
    110 -> 110 (a genuine 0% return). If `benchmark_rs_3m` were ever computed
    from a second, independently re-derived RAW series (the pre-fix bug),
    its value would reflect the -50% move; this asserts it reflects the
    adjusted, genuinely-flat one instead.
    """

    connection = sqlite3.connect(":memory:")
    raw_store = _FakeRawStore()
    manifest_root = tmp_path / "nse-manifest"

    isin = "INE0SPLITRS0001"
    equity_id = _instrument_id(isin, AssetClass.EQUITY)

    source_ids = {
        SourceInputRole.EOD_OBSERVATIONS: f"{FIXTURE_SOURCE_ID}-split-eod",
        SourceInputRole.SECURITY_MASTER: f"{FIXTURE_SOURCE_ID}-split-master",
        SourceInputRole.CORPORATE_ACTIONS: f"{FIXTURE_SOURCE_ID}-split-actions",
    }
    policies = {role: _fixture_policy(source_id=sid) for role, sid in source_ids.items()}

    def _bhavcopy(day: date, close: str) -> bytes:
        header = "SYMBOL,SERIES,TYPE,NAME OF COMPANY,ISIN,CLOSE,VOLUME,TIMESTAMP"
        return ("\n".join([header, _bhavcopy_row("SPLITRS", isin, close, day)]) + "\n").encode("utf-8")

    def _security_master() -> bytes:
        header = "SYMBOL,SERIES,TYPE,NAME OF COMPANY,ISIN NUMBER"
        return ("\n".join([header, f"SPLITRS,EQ,,Split RS Equity Limited,{isin}"]) + "\n").encode("utf-8")

    def _actions() -> bytes:
        return json.dumps({
            "actions": [
                {"isin": isin, "action_date": T0.isoformat(), "action_type": "split", "numerator": 2, "denominator": 1}
            ]
        }).encode("utf-8")

    nse_inputs = [
        _admit_nse(
            manifest_root, raw_store, policies, source_ids,
            role=SourceInputRole.EOD_OBSERVATIONS, relative_path="eod-t0.csv", body=_bhavcopy(T0, "110"), effective=T0,
        ),
        _admit_nse(
            manifest_root, raw_store, policies, source_ids,
            role=SourceInputRole.SECURITY_MASTER, relative_path="master-t0.csv", body=_security_master(), effective=T0,
        ),
        _admit_nse(
            manifest_root, raw_store, policies, source_ids,
            role=SourceInputRole.EOD_OBSERVATIONS, relative_path="eod-rs-start.csv",
            body=_bhavcopy(RS_START_DATE, "220"), effective=RS_START_DATE,
        ),
        _admit_nse(
            manifest_root, raw_store, policies, source_ids,
            role=SourceInputRole.CORPORATE_ACTIONS, relative_path="actions-t0.json", body=_actions(), effective=T0,
        ),
    ]

    mappings_path = tmp_path / "mappings.json"
    mappings_path.write_text(
        json.dumps([{
            "identifier": equity_id,
            "identifier_type": "instrument",
            "benchmark_id": "NIFTY500",
            "valid_from": "2020-01-01",
            "valid_to": None,
            "source_reference": "https://www.niftyindices.com/reports/historical-data",
        }]),
        encoding="utf-8",
    )
    benchmark_input = _admit_benchmark_input(raw_store, _benchmark_csv(), T0)

    build = build_composed_candidate(
        connection, raw_store,
        nse_inputs=nse_inputs,
        reference_inputs=[benchmark_input],
        effective_date=T0,
        mappings_path=mappings_path,
    )

    # S02's own adjusted history: the split factor (0.5) applied to the
    # pre-split RS_START_DATE close (220) makes the adjusted series flat.
    adjusted_points = dict(build.histories[equity_id][1])
    assert adjusted_points[RS_START_DATE] == Decimal("110")
    assert adjusted_points[T0] == Decimal("110")

    benchmark_rows = [
        row for row in build.candidate["tables"]["latest_metrics"]
        if row["instrument_id"] == equity_id and row["metric"] == "benchmark_rs_3m"
    ]
    assert len(benchmark_rows) == 1
    assert benchmark_rows[0]["state"] == "present"

    asset_return = Decimal("110") / Decimal("110") - 1  # 0%, from the ADJUSTED series
    benchmark_return = Decimal("11000.00") / Decimal("10000.00") - 1  # 10%
    expected_rs = (Decimal(1) + asset_return) / (Decimal(1) + benchmark_return) - Decimal(1)
    actual_rs = Decimal(benchmark_rows[0]["raw_value"])
    assert actual_rs.quantize(Decimal("0.00000001")) == expected_rs.quantize(Decimal("0.00000001"))

    # The value a still-broken re-derivation from RAW (unadjusted) prices
    # would have produced -- proving this isn't accidentally still that.
    wrong_raw_return = Decimal("110") / Decimal("220") - 1  # -50%
    wrong_rs = (Decimal(1) + wrong_raw_return) / (Decimal(1) + benchmark_return) - Decimal(1)
    assert actual_rs != wrong_rs


def test_no_source_scheduled_reports_skipped_not_completed(schedule_env: dict[str, Any]) -> None:
    active_before = schedule_env["publisher"].active_dataset_id()
    no_manifest_refresh = run_scheduled_refresh(
        schedule_env["connection"], schedule_env["publisher"], schedule_env["raw_store"],
        _schedule(None, None, WEEKEND), WEEKEND,
        amfi_source_ids=[], nse_inputs=[],
    )
    assert no_manifest_refresh.status == "skipped"
    assert no_manifest_refresh.active_dataset_id == active_before
    assert no_manifest_refresh.source_status == {}
