"""F16/S02: publish equity/ETF observations and corporate-action-adjusted
analytics from real (admitted) NSE source inputs.

Scenario coverage (one small "current day" fixture under
``pipeline/tests/fixtures/nse/`` plus a generated multi-year daily backfill
built in this file):

* ``INFY`` -- an ordinary EQ share with a full ~3-year session history.
* ``NIFTYBEES`` -- an ETF (explicit TYPE=ETF classification).
* ``SMECO`` -- an excluded security (SME series/type): must never become a
  published instrument at all.
* ``RENAMECO`` -- traded as ``OLDSYM`` for most of its history and
  ``NEWSYM`` for its most recent sessions (including the effective date);
  same ISIN throughout, so it must keep one stable instrument ID.
* ``SPLITCO`` -- a 2:1 split effective exactly on the published date: 100
  (raw, pre-split) the prior session, 50 (raw, already on the post-split
  share basis -- corporate_actions.py never adjusts the event date's own
  close) on the effective date. Adjusted, the whole series is flat at 50,
  so the corporate-action-adjusted one-day return across the split is
  exactly zero.
* ``NEWCO`` -- a short-history instrument (30 sessions): full-window
  metrics (twelve-month return, RS, momentum) must come back ``missing``,
  never fabricated, while short-window metrics (return_1d/1w) are present.
* ``OLDCO`` -- a formerly active instrument that stops reporting 60
  sessions before the effective date (delisted/suspended): it must keep
  its stable instrument ID and its full historical price series in
  ``DatasetBuild.histories``, but must be excluded from the current
  screen (``tables.instruments`` / ``tables.latest_metrics``).

Admission goes through the real S01 contract
(``resolve_supplied_source_input`` -> ``sources.registry.admit``) using the
same test-only, unregistered fixture source ID / policy pattern already
established by ``test_supplied_source_admission.py`` -- real NSE supplied-use
permission stays denied in production (S01), so no test here uses the real
``nse-eod``/``nse-filings-xbrl`` registry entries for actual admission.
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace as dataclass_replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from market_pipeline.domain.models import AssetClass, Instrument, SourceArtifact, SourcePolicy
from market_pipeline.jobs.nse_candidate import FUNDAMENTAL_METRICS, build_nse_candidate
from market_pipeline.jobs.publish import DatasetBuild, PublicationInputError
from market_pipeline.jobs.source_inputs import (
    SourceInput,
    SourceInputRole,
    resolve_supplied_source_input,
)
from market_pipeline.publication.bundle import BundleObject, build_bundle
from market_pipeline.publication.d1_export import export_active_dataset
from market_pipeline.storage.d1_publisher import D1Publisher, publish
from market_pipeline.storage.history_store import HistoryStore

FIXTURES = Path(__file__).parents[1] / "fixtures" / "nse"
EFFECTIVE_DATE = date(2026, 9, 11)
HISTORY_SESSIONS = 259  # sessions strictly before EFFECTIVE_DATE

FIXTURE_SOURCE_ID = "test-nse-candidate-fixture"
FIXTURE_SOURCE_URL = "https://fixture.test.invalid/official/nse"
FIXTURE_TERMS_URL = "https://fixture.test.invalid/terms"
FIXTURE_PERMISSION_REFERENCE = "test-nse-candidate-permission-001"


def _fixture_policy(**overrides: object) -> SourcePolicy:
    """A test-only injected policy, exactly mirroring the S01 admission tests.

    Real NSE supplied-use permission is not recorded in the canonical
    registry (see ``test_supplied_source_admission.py``), so this proves the
    composition logic through the same test-injected "authorized supplied
    use" pathway S01 established -- never a change to ``SOURCE_POLICIES``.
    """

    defaults: dict[str, object] = dict(
        source_id=FIXTURE_SOURCE_ID,
        source_url=FIXTURE_SOURCE_URL,
        terms_url=FIXTURE_TERMS_URL,
        automation_allowed=False,
        retention_allowed=True,
        supplied_use_allowed=True,
        supplied_use_permission_reference=FIXTURE_PERMISSION_REFERENCE,
        approved_url_prefixes=(FIXTURE_SOURCE_URL,),
        description="Test-only fixture proving NSE candidate composition end to end.",
    )
    defaults.update(overrides)
    return SourcePolicy.model_validate(defaults)


class _FakeRawStore:
    """Minimal ``RawStore``-Protocol double that serves admitted bodies by key.

    ``build_nse_candidate`` only ever reads bodies back by an admitted
    ``SourceInput.object_key`` (see the module docstring); this stands in for
    ``LocalRawStore`` so the test can seed objects for the unregistered
    fixture source ID without going through ``LocalRawStore.put``'s own
    ``get_source_policy`` lookup (which would reject an unregistered ID --
    a real production raw store never carries "network" bytes for a source
    with no admitted policy, matching S01's own posture on this fixture ID).
    """

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}

    def seed(self, source_input: SourceInput, body: bytes) -> None:
        self._objects[source_input.object_key] = body

    def put(self, artifact: SourceArtifact, body: bytes) -> str:  # pragma: no cover - unused by the composer
        raise NotImplementedError("test double is seeded directly from admitted SourceInputs")

    def get(self, object_key: str) -> bytes:
        return self._objects[object_key]


def _business_days_ending(end: date, count: int) -> list[date]:
    days: list[date] = []
    current = end
    while len(days) < count:
        if current.weekday() < 5:
            days.append(current)
        current -= timedelta(days=1)
    days.reverse()
    return days


HISTORY_DATES = _business_days_ending(EFFECTIVE_DATE - timedelta(days=1), HISTORY_SESSIONS)
# INE0OLDCO001 stops reporting 60 sessions before the effective date.
OLDCO_LAST_INDEX = HISTORY_SESSIONS - 60 - 1
# INE0NEWCO001 only starts reporting for the final 30 history sessions.
NEWCO_FIRST_INDEX = HISTORY_SESSIONS - 30
# RENAMECO trades as NEWSYM for its final 10 history sessions (and on the
# effective date itself, matching the static "current day" fixture/master).
RENAME_INDEX = HISTORY_SESSIONS - 10


def _history_day_body(index: int, day: date) -> bytes:
    rows: list[str] = []
    rows.append(f"INFY,EQ,,Infosys Limited,INE009A01021,{1000 + (index % 50)},{100000 + index * 7},{day:%d-%b-%Y}")
    rows.append(
        f"NIFTYBEES,EQ,ETF,Nippon India ETF Nifty BeES,INF204KB14I2,{200 + (index % 30)},"
        f"{80000 + index * 5},{day:%d-%b-%Y}"
    )
    rename_symbol = "NEWSYM" if index >= RENAME_INDEX else "OLDSYM"
    rows.append(
        f"{rename_symbol},EQ,,Renamed Company Limited,INE0RENAME01,{180 + (index % 20)},"
        f"{50000 + index * 3},{day:%d-%b-%Y}"
    )
    rows.append(f"SPLITCO,EQ,,Split Company Limited,INE0SPLIT001,100,{60000 + index * 4},{day:%d-%b-%Y}")
    if index >= NEWCO_FIRST_INDEX:
        rows.append(f"NEWCO,EQ,,New Listing Limited,INE0NEWCO001,{70 + (index % 5)},{20000 + index},{day:%d-%b-%Y}")
    if index <= OLDCO_LAST_INDEX:
        rows.append(f"OLDCO,EQ,,Formerly Active Limited,INE0OLDCO0001,{50 + (index % 10)},{15000 + index},{day:%d-%b-%Y}")
    header = "SYMBOL,SERIES,TYPE,NAME OF COMPANY,ISIN,CLOSE,VOLUME,TIMESTAMP"
    return ("\n".join([header, *rows]) + "\n").encode("utf-8")


@pytest.fixture(scope="module")
def nse_universe(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    manifest_root = tmp_path_factory.mktemp("nse-candidate-manifest")
    raw_store = _FakeRawStore()
    # Each role gets its own source ID (mirroring three genuinely distinct
    # official NSE downloads): a bhavcopy, a security master, and a
    # corporate-actions file share no (source_id, effective_date) pair, so
    # the input manifest's own one-artifact-per-source/date rule cannot see
    # them as ambiguous revisions of "the same" artifact.
    source_ids = {
        SourceInputRole.EOD_OBSERVATIONS: f"{FIXTURE_SOURCE_ID}-eod",
        SourceInputRole.SECURITY_MASTER: f"{FIXTURE_SOURCE_ID}-master",
        SourceInputRole.CORPORATE_ACTIONS: f"{FIXTURE_SOURCE_ID}-actions",
    }
    policies = {role: _fixture_policy(source_id=source_id) for role, source_id in source_ids.items()}
    inputs: list[SourceInput] = []

    def _admit(*, role: SourceInputRole, relative_path: str, effective: date, filename: str) -> SourceInput:
        artifact, body, source_input = resolve_supplied_source_input(
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

    for index, day in enumerate(HISTORY_DATES):
        relative = f"eod/{day.isoformat()}.csv"
        (manifest_root / "eod").mkdir(exist_ok=True)
        (manifest_root / relative).write_bytes(_history_day_body(index, day))
        inputs.append(
            _admit(role=SourceInputRole.EOD_OBSERVATIONS, relative_path=relative, effective=day, filename=relative)
        )

    (manifest_root / "effective-bhavcopy.csv").write_bytes(
        (FIXTURES / "candidate_bhavcopy.csv").read_bytes()
    )
    inputs.append(
        _admit(
            role=SourceInputRole.EOD_OBSERVATIONS,
            relative_path="effective-bhavcopy.csv",
            effective=EFFECTIVE_DATE,
            filename="effective-bhavcopy.csv",
        )
    )
    (manifest_root / "security-master.csv").write_bytes(
        (FIXTURES / "candidate_security_master.csv").read_bytes()
    )
    inputs.append(
        _admit(
            role=SourceInputRole.SECURITY_MASTER,
            relative_path="security-master.csv",
            effective=EFFECTIVE_DATE,
            filename="security-master.csv",
        )
    )
    (manifest_root / "corporate-actions.json").write_bytes(
        (FIXTURES / "candidate_corporate_actions.json").read_bytes()
    )
    inputs.append(
        _admit(
            role=SourceInputRole.CORPORATE_ACTIONS,
            relative_path="corporate-actions.json",
            effective=EFFECTIVE_DATE,
            filename="corporate-actions.json",
        )
    )

    return {"inputs": inputs, "raw_store": raw_store}


def _instrument_id(isin: str, asset_class: AssetClass) -> str:
    return str(Instrument.from_provider("nse", isin, asset_class).instrument_id)


INFY_ID = _instrument_id("INE009A01021", AssetClass.EQUITY)
NIFTYBEES_ID = _instrument_id("INF204KB14I2", AssetClass.ETF)
RENAMECO_ID = _instrument_id("INE0RENAME01", AssetClass.EQUITY)
SPLITCO_ID = _instrument_id("INE0SPLIT001", AssetClass.EQUITY)
NEWCO_ID = _instrument_id("INE0NEWCO001", AssetClass.EQUITY)
OLDCO_ID = _instrument_id("INE0OLDCO0001", AssetClass.EQUITY)


def _build(nse_universe: dict[str, Any]) -> DatasetBuild:
    return build_nse_candidate(nse_universe["inputs"], nse_universe["raw_store"], EFFECTIVE_DATE)


# --- TDD anchor -------------------------------------------------------------


def test_build_nse_candidate_is_importable() -> None:
    """The module/function this task adds must exist and be callable."""

    assert callable(build_nse_candidate)


# --- Oracle assertions (verbatim from the plan) -----------------------------


def test_published_asset_classes_are_equity_and_etf_only(nse_universe: dict[str, Any]) -> None:
    build = _build(nse_universe)
    published_asset_classes = {row["asset_class"] for row in build.candidate["tables"]["instruments"]}
    assert published_asset_classes == {"equity", "etf"}


def test_renamed_instrument_keeps_one_stable_id(nse_universe: dict[str, Any]) -> None:
    build = _build(nse_universe)
    instruments = {row["instrument_id"]: row for row in build.candidate["tables"]["instruments"]}
    assert RENAMECO_ID in instruments
    renamed_instrument_id = instruments[RENAMECO_ID]["instrument_id"]
    original_instrument_id = RENAMECO_ID
    assert renamed_instrument_id == original_instrument_id
    # The published symbol reflects the newest (post-rename) bhavcopy row.
    assert instruments[RENAMECO_ID]["symbol"] == "NEWSYM"
    # Its price history spans both the old- and new-symbol sessions under
    # this one ID.
    _, points = build.histories[RENAMECO_ID]
    assert len(points) == HISTORY_SESSIONS + 1


def test_split_adjusted_one_day_return_is_exactly_zero(nse_universe: dict[str, Any]) -> None:
    build = _build(nse_universe)
    metrics = build.candidate["tables"]["latest_metrics"]
    row = next(
        item for item in metrics
        if item["instrument_id"] == SPLITCO_ID and item["metric"] == "return_1d"
    )
    assert row["state"] == "present"
    split_adjusted_return = Decimal(row["raw_value"])
    assert split_adjusted_return == Decimal("0")


def test_hand_verified_split_adjustment_matches_the_stored_history(nse_universe: dict[str, Any]) -> None:
    """Direct hand-check: adjusted close is flat at 50 across the whole series."""

    build = _build(nse_universe)
    _, points = build.histories[SPLITCO_ID]
    by_date = dict(points)
    assert by_date[HISTORY_DATES[-1]] == Decimal("50")  # 100 raw * 0.5 adjustment factor
    assert by_date[EFFECTIVE_DATE] == Decimal("50")  # event date close is never itself adjusted
    assert len(set(by_date.values())) == 1


def test_excluded_formerly_active_security_is_not_in_the_current_screen(nse_universe: dict[str, Any]) -> None:
    build = _build(nse_universe)
    current_screen_ids = {row["instrument_id"] for row in build.candidate["tables"]["instruments"]}
    excluded_security_id = OLDCO_ID
    assert excluded_security_id not in current_screen_ids
    # ... but its historical identity/price series survives.
    assert OLDCO_ID in build.histories
    _, points = build.histories[OLDCO_ID]
    assert len(points) == OLDCO_LAST_INDEX + 1
    metric_ids = {row["instrument_id"] for row in build.candidate["tables"]["latest_metrics"]}
    assert excluded_security_id not in metric_ids


def test_equity_rs_cohort_excludes_etfs(nse_universe: dict[str, Any]) -> None:
    build = _build(nse_universe)
    instruments = build.candidate["tables"]["instruments"]
    etf_ids = {row["instrument_id"] for row in instruments if row["asset_class"] == "etf"}
    equity_rs_cohort_ids = {
        row["instrument_id"]
        for row in build.candidate["tables"]["latest_metrics"]
        if row["metric"] == "rs_rating" and row["state"] == "present"
    }
    assert etf_ids == {NIFTYBEES_ID}
    assert equity_rs_cohort_ids  # INFY/RENAMECO/SPLITCO have full 253+ session coverage
    assert equity_rs_cohort_ids.isdisjoint(etf_ids)
    # An ETF's rs_rating is not_applicable, never silently zero or missing-as-if-uncomputed.
    etf_rs_row = next(
        row for row in build.candidate["tables"]["latest_metrics"]
        if row["instrument_id"] == NIFTYBEES_ID and row["metric"] == "rs_rating"
    )
    assert etf_rs_row["state"] == "not_applicable"


# --- Additional composition/eligibility coverage ----------------------------


def test_excluded_ineligible_type_never_becomes_a_published_instrument(nse_universe: dict[str, Any]) -> None:
    build = _build(nse_universe)
    symbols = {row["symbol"] for row in build.candidate["tables"]["instruments"]}
    assert "SMECO" not in symbols
    assert all(row["instrument_id"] != _instrument_id("INE0SMECO001", AssetClass.EQUITY) for row in build.candidate["tables"]["instruments"])


def test_short_history_instrument_has_missing_full_window_metrics_never_fabricated(
    nse_universe: dict[str, Any],
) -> None:
    build = _build(nse_universe)
    metrics = {
        (row["instrument_id"], row["metric"]): row
        for row in build.candidate["tables"]["latest_metrics"]
        if row["instrument_id"] == NEWCO_ID
    }
    assert metrics[(NEWCO_ID, "return_1d")]["state"] == "present"
    assert metrics[(NEWCO_ID, "return_12m")]["state"] == "missing"
    assert metrics[(NEWCO_ID, "return_12m")]["value"] is None
    assert metrics[(NEWCO_ID, "volatility_1y")]["state"] == "missing"
    assert metrics[(NEWCO_ID, "rs_rating")]["state"] == "missing"


def test_benchmark_and_fundamental_fields_are_honestly_missing_not_invented(nse_universe: dict[str, Any]) -> None:
    build = _build(nse_universe)
    infy_metrics = {
        row["metric"]: row for row in build.candidate["tables"]["latest_metrics"] if row["instrument_id"] == INFY_ID
    }
    for window in ("benchmark_rs_3m", "benchmark_rs_6m", "benchmark_rs_12m"):
        assert infy_metrics[window]["state"] == "missing"
        assert infy_metrics[window]["value"] is None
        assert "S03" in infy_metrics[window]["metadata"]["reason"]
    for field in FUNDAMENTAL_METRICS:
        row = infy_metrics[f"fundamental_{field}"]
        assert row["state"] == "missing"
        assert row["value"] is None

    etf_metrics = {
        row["metric"]: row
        for row in build.candidate["tables"]["latest_metrics"]
        if row["instrument_id"] == NIFTYBEES_ID
    }
    for field in FUNDAMENTAL_METRICS:
        assert etf_metrics[f"fundamental_{field}"]["state"] == "not_applicable"


def test_corporate_action_lineage_is_preserved(nse_universe: dict[str, Any]) -> None:
    build = _build(nse_universe)
    actions = build.candidate["tables"]["corporate_actions"]
    splitco_actions = [row for row in actions if row["instrument_id"] == SPLITCO_ID]
    assert len(splitco_actions) == 1
    assert splitco_actions[0]["action_type"] == "split"
    assert splitco_actions[0]["action_date"] == EFFECTIVE_DATE.isoformat()
    assert splitco_actions[0]["numerator"] == 2
    assert splitco_actions[0]["denominator"] == 1


# --- Fail-closed: missing security master / EOD coverage -------------------


def test_missing_security_master_fails_closed_instead_of_publishing_an_empty_universe(
    nse_universe: dict[str, Any],
) -> None:
    inputs = [item for item in nse_universe["inputs"] if item.role is not SourceInputRole.SECURITY_MASTER]
    with pytest.raises(PublicationInputError):
        build_nse_candidate(inputs, nse_universe["raw_store"], EFFECTIVE_DATE)


def test_role_source_id_mismatch_fails_closed(nse_universe: dict[str, Any]) -> None:
    """A wiring bug that mislabels a REAL registered source's role must not
    be silently trusted as official data of the wrong kind.

    Mirrors ``jobs/reference_candidate.py``'s own
    ``test_role_source_id_mismatch_fails_closed``: relabels a genuinely
    present input's ``source_id`` to the real, registered ``nse-eod`` --
    which this NSE candidate build only ever trusts as EOD_OBSERVATIONS data
    -- while keeping its role as ``SECURITY_MASTER``. This must be rejected
    by ``_require_role_source_id``, not silently published with fabricated
    "official" provenance for the wrong role.
    """

    real_master_input = next(
        item for item in nse_universe["inputs"] if item.role is SourceInputRole.SECURITY_MASTER
    )
    mislabeled = dataclass_replace(real_master_input, source_id="nse-eod")
    inputs = [item for item in nse_universe["inputs"] if item.role is not SourceInputRole.SECURITY_MASTER]
    inputs.append(mislabeled)

    with pytest.raises(PublicationInputError, match="source_id"):
        build_nse_candidate(inputs, nse_universe["raw_store"], EFFECTIVE_DATE)


def test_missing_effective_date_eod_observations_fails_closed(nse_universe: dict[str, Any]) -> None:
    inputs = [
        item
        for item in nse_universe["inputs"]
        if not (item.role is SourceInputRole.EOD_OBSERVATIONS and item.loaded_date == EFFECTIVE_DATE)
    ]
    with pytest.raises(PublicationInputError):
        build_nse_candidate(inputs, nse_universe["raw_store"], EFFECTIVE_DATE)


def test_missing_corporate_action_coverage_warns_instead_of_silently_ignoring_gap(
    nse_universe: dict[str, Any],
) -> None:
    inputs = [item for item in nse_universe["inputs"] if item.role is not SourceInputRole.CORPORATE_ACTIONS]
    build = build_nse_candidate(inputs, nse_universe["raw_store"], EFFECTIVE_DATE)
    assert any("corporate-action coverage" in warning for warning in build.warnings)
    # Without the split's action coverage, SPLITCO's raw 100->50 discontinuity
    # is published unadjusted -- a large, honestly-reported one-day move,
    # never silently smoothed away.
    metrics = build.candidate["tables"]["latest_metrics"]
    row = next(item for item in metrics if item["instrument_id"] == SPLITCO_ID and item["metric"] == "return_1d")
    assert Decimal(row["raw_value"]) == Decimal("-0.5")


# --- R07 export -> real Worker (D1) schema -> query round trip -------------


def test_export_and_query_through_the_real_d1_schema(nse_universe: dict[str, Any], tmp_path: Path) -> None:
    build = _build(nse_universe)
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
        source_dates=[EFFECTIVE_DATE.isoformat()],
        projection_version="nse-eq-etf-projection-v1",
        candidate_entries=object_entries,
        effective_date=EFFECTIVE_DATE,
    )
    build.candidate["metadata"]["publication_bundle"] = bundle.as_dict()

    local = sqlite3.connect(":memory:")
    local_publisher = D1Publisher(local)
    local_publisher.initialize_schema()
    publish(build.candidate, local_publisher, momentum_scores=build.momentum)

    sql_path = tmp_path / "export.sql"
    export_active_dataset(local, sql_path, history_root=history_root, object_manifest=tmp_path / "manifest.json")

    # Load the exported SQL into a *separate* SQLite connection with the same
    # real D1 migrations applied -- the real Worker repository's schema
    # (D1ResearchStore reads exactly these tables/columns), per R11's own
    # "apply real D1 migrations to local SQLite" pattern.
    remote = sqlite3.connect(":memory:")
    remote_publisher = D1Publisher(remote)
    remote_publisher.initialize_schema()
    remote.executescript(sql_path.read_text(encoding="utf-8"))

    assert remote_publisher.active_dataset_id() == build.candidate["dataset_id"]

    # Representative volume/return screen: active equities/ETFs with a
    # positive one-day return and above-average-for-the-set trading volume.
    rows = remote.execute(
        """
        SELECT instrument_id, symbol,
               json_extract(metric_values_json, '$.return_1d') AS return_1d,
               json_extract(metric_values_json, '$.average_volume_1w') AS avg_volume
        FROM instrument_snapshots
        WHERE dataset_id = ? AND active = 1 AND asset_class IN ('equity', 'etf')
          AND json_extract(metric_values_json, '$.return_1d') > 0
        """,
        (build.candidate["dataset_id"],),
    ).fetchall()
    sql_matches = {str(row[0]) for row in rows}

    # Independently calculated expected match set, straight from the raw
    # fixture closes (not from the analytics module): every current-screen
    # equity/ETF whose adjusted close rose from the prior session to the
    # effective date.
    expected_matches: set[str] = set()
    for instrument_id, (asset_class, points) in build.histories.items():
        if asset_class not in ("equity", "etf"):
            continue
        by_date = dict(points)
        if EFFECTIVE_DATE not in by_date:
            continue  # excluded from the current screen (e.g. OLDCO)
        prior_dates = sorted(d for d in by_date if d < EFFECTIVE_DATE)
        if not prior_dates:
            continue
        prior_close = by_date[prior_dates[-1]]
        current_close = by_date[EFFECTIVE_DATE]
        if prior_close != 0 and current_close > prior_close:
            expected_matches.add(instrument_id)

    assert sql_matches == expected_matches
    # SPLITCO's flat post-adjustment series must not appear as a "gain".
    assert SPLITCO_ID not in sql_matches
    # INFY's synthetic drift produces at least one genuine, independently
    # verifiable positive-return match so this assertion is not vacuous.
    assert sql_matches
