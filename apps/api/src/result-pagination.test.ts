import { describe, expect, it } from "vitest";
import { D1ResearchStore } from "./repositories";
import { createSqliteD1, explainQueryPlan, seedDataset, seedScreen } from "./test-support/sqlite-d1";

/**
 * R06: bound saved-screen execution and result retrieval to set-based D1
 * SQL. These tests seed a local SQLite-backed D1 (the real forward
 * migrations, not a hand-rolled fake) with a representative universe --
 * 10,000 matches and 100 historical runs -- and assert the repository
 * issues a small, constant number of SQL statements and materializes only
 * the requested page, regardless of how large the underlying run is. Before
 * this task, `listRuns`/`runScreen` fetched every match for every run (an
 * N+1: one query per run, each returning the run's full match set) and
 * `runScreen` read every matching instrument individually to build its
 * explanation. Both are the anti-pattern this file guards against.
 */

function seedMatches(sqlite: import("node:sqlite").DatabaseSync, datasetId: string, runId: string, count: number): void {
  const insert = sqlite.prepare(
    "INSERT INTO screen_matches (dataset_id, run_id, ordinal, instrument_id, score, symbol, name, asset_class, metrics_json, entered, exited) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
  );
  sqlite.exec("BEGIN");
  for (let index = 0; index < count; index += 1) {
    const ordinal = index + 1;
    const instrumentId = `inst-${String(index).padStart(6, "0")}`;
    const score = count - index; // already rank-ordered, descending
    insert.run(datasetId, runId, ordinal, instrumentId, score, instrumentId.toUpperCase(), `Instrument ${index}`, index % 2 === 0 ? "equity" : "etf", "[]", index === 0 ? 1 : 0, 0);
  }
  sqlite.exec("COMMIT");
}

describe("pageRunMatches bounds result retrieval", () => {
  function seedLargeRun() {
    const { db, sqlite, probe } = createSqliteD1();
    seedDataset(sqlite, "dataset-1");
    seedScreen(sqlite, "screen-1", "Momentum", "Volume > 0");
    sqlite.prepare("INSERT INTO screen_runs (dataset_id, run_id, screen_id, effective_date, status, result_count, source, language_version, completed_at) VALUES (?,?,?,?,?,?,?,?,?)")
      .run("dataset-1", "run-current", "screen-1", "2026-09-07", "complete", 10_000, "Volume > 0", "v1", "2026-09-07T00:00:00.000Z");
    seedMatches(sqlite, "dataset-1", "run-current", 10_000);
    // 100 historical runs for the same screen, each with its own (small) match set,
    // so listing run history never has to touch a match table at all.
    const insertRun = sqlite.prepare("INSERT INTO screen_runs (dataset_id, run_id, screen_id, effective_date, status, result_count, source, language_version, completed_at) VALUES (?,?,?,?,?,?,?,?,?)");
    for (let index = 0; index < 100; index += 1) {
      const runId = `run-history-${index}`;
      insertRun.run("dataset-1", runId, "screen-1", `2026-08-${String((index % 28) + 1).padStart(2, "0")}`, "complete", 5, "Volume > 0", "v1", `2026-08-${String((index % 28) + 1).padStart(2, "0")}T00:00:00.000Z`);
    }
    probe.reset();
    return { db, sqlite, probe };
  }

  it("returns exactly the requested page and total for a 10,000-match run within a small, fixed statement budget", async () => {
    const { db, probe } = seedLargeRun();
    const store = new D1ResearchStore(db);

    const page = await store.pageRunMatches("run-current", { sort: "rank", direction: "asc", limit: 25, offset: 0 });

    expect(page.matches).toHaveLength(25);
    expect(probe.statementCount).toBeLessThanOrEqual(5);
    expect(probe.materializedRows).toBeLessThanOrEqual(25);
    expect(page.total).toBe(10_000);
    expect(page.matches.map((match) => match.instrumentId)).toEqual(["inst-000000", "inst-000001", "inst-000002", "inst-000003", "inst-000004", "inst-000005", "inst-000006", "inst-000007", "inst-000008", "inst-000009", "inst-000010", "inst-000011", "inst-000012", "inst-000013", "inst-000014", "inst-000015", "inst-000016", "inst-000017", "inst-000018", "inst-000019", "inst-000020", "inst-000021", "inst-000022", "inst-000023", "inst-000024"]);
  });

  it("bounds a later page the same way as the first", async () => {
    const { db, probe } = seedLargeRun();
    const store = new D1ResearchStore(db);

    const page = await store.pageRunMatches("run-current", { sort: "rank", direction: "asc", limit: 25, offset: 9_975 });

    expect(page.matches).toHaveLength(25);
    expect(probe.statementCount).toBeLessThanOrEqual(5);
    expect(probe.materializedRows).toBeLessThanOrEqual(25);
    expect(page.total).toBe(10_000);
    expect(page.matches[0]?.instrumentId).toBe("inst-009975");
    expect(page.matches[24]?.instrumentId).toBe("inst-009999");
  });

  it("sorts by score descending and ascending with a deterministic instrument-ID tiebreak on ties", async () => {
    const { db, sqlite } = createSqliteD1();
    seedDataset(sqlite, "dataset-1");
    seedScreen(sqlite, "screen-1", "Momentum", "Volume > 0");
    sqlite.prepare("INSERT INTO screen_runs (dataset_id, run_id, screen_id, effective_date, status, result_count, source, language_version, completed_at) VALUES ('dataset-1','run-1','screen-1','2026-09-07','complete',3,'Volume > 0','v1','2026-09-07T00:00:00.000Z')").run();
    const insert = sqlite.prepare("INSERT INTO screen_matches (dataset_id, run_id, ordinal, instrument_id, score, symbol, name, asset_class, metrics_json, entered, exited) VALUES (?,?,?,?,?,?,?,?,?,?,?)");
    insert.run("dataset-1", "run-1", 1, "b", 5, "B", "B", "equity", "[]", 0, 0);
    insert.run("dataset-1", "run-1", 2, "a", 5, "A", "A", "equity", "[]", 0, 0);
    insert.run("dataset-1", "run-1", 3, "c", 1, "C", "C", "equity", "[]", 0, 0);
    const store = new D1ResearchStore(db);

    const desc = await store.pageRunMatches("run-1", { sort: "score", direction: "desc", limit: 10, offset: 0 });
    expect(desc.matches.map((match) => match.instrumentId)).toEqual(["a", "b", "c"]);

    const asc = await store.pageRunMatches("run-1", { sort: "score", direction: "asc", limit: 10, offset: 0 });
    expect(asc.matches.map((match) => match.instrumentId)).toEqual(["c", "a", "b"]);

    const bySymbol = await store.pageRunMatches("run-1", { sort: "symbol", direction: "asc", limit: 10, offset: 0 });
    expect(bySymbol.matches.map((match) => match.instrumentId)).toEqual(["a", "b", "c"]);

    const byAssetClass = await store.pageRunMatches("run-1", { sort: "assetClass", direction: "asc", limit: 10, offset: 0 });
    expect(byAssetClass.matches).toHaveLength(3);
  });

  it("rejects an invalid sort field and an invalid direction", async () => {
    const { db, sqlite } = createSqliteD1();
    seedDataset(sqlite, "dataset-1");
    seedScreen(sqlite, "screen-1", "Momentum", "Volume > 0");
    sqlite.prepare("INSERT INTO screen_runs (dataset_id, run_id, screen_id, effective_date, status, result_count, source, language_version, completed_at) VALUES ('dataset-1','run-1','screen-1','2026-09-07','complete',0,'Volume > 0','v1','2026-09-07T00:00:00.000Z')").run();
    const store = new D1ResearchStore(db);

    await expect(store.pageRunMatches("run-1", { sort: "unknown" as never, direction: "asc", limit: 10, offset: 0 })).rejects.toThrow();
    await expect(store.pageRunMatches("run-1", { sort: "rank", direction: "sideways" as never, limit: 10, offset: 0 })).rejects.toThrow();
  });

  it("excludes exited matches from both the page and the total", async () => {
    const { db, sqlite } = createSqliteD1();
    seedDataset(sqlite, "dataset-1");
    seedScreen(sqlite, "screen-1", "Momentum", "Volume > 0");
    sqlite.prepare("INSERT INTO screen_runs (dataset_id, run_id, screen_id, effective_date, status, result_count, source, language_version, completed_at) VALUES ('dataset-1','run-1','screen-1','2026-09-07','complete',1,'Volume > 0','v1','2026-09-07T00:00:00.000Z')").run();
    const insert = sqlite.prepare("INSERT INTO screen_matches (dataset_id, run_id, ordinal, instrument_id, score, symbol, name, asset_class, metrics_json, entered, exited) VALUES (?,?,?,?,?,?,?,?,?,?,?)");
    insert.run("dataset-1", "run-1", 1, "live", 5, "LIVE", "Live", "equity", "[]", 1, 0);
    insert.run("dataset-1", "run-1", 2, "gone", 4, "GONE", "Gone", "equity", "[]", 0, 1);
    const store = new D1ResearchStore(db);

    const page = await store.pageRunMatches("run-1", { sort: "rank", direction: "asc", limit: 10, offset: 0 });

    expect(page.total).toBe(1);
    expect(page.matches.map((match) => match.instrumentId)).toEqual(["live"]);
    expect(page.matches[0]).toMatchObject({ entered: true, exited: false });
  });

  it("returns identity as it was recorded at run time, not the current instrument row", async () => {
    const { db, sqlite } = createSqliteD1();
    seedDataset(sqlite, "dataset-1");
    seedScreen(sqlite, "screen-1", "Momentum", "Volume > 0");
    sqlite.prepare("INSERT INTO screen_runs (dataset_id, run_id, screen_id, effective_date, status, result_count, source, language_version, completed_at) VALUES ('dataset-1','run-1','screen-1','2026-09-07','complete',1,'Volume > 0','v1','2026-09-07T00:00:00.000Z')").run();
    sqlite.prepare("INSERT INTO screen_matches (dataset_id, run_id, ordinal, instrument_id, score, symbol, name, asset_class, metrics_json, entered, exited) VALUES ('dataset-1','run-1',1,'old',10,'OLD_SYMBOL','Old Company Ltd','equity','[]',1,0)").run();
    // The current serving projection has since been overwritten with a renamed instrument (0006 retains only the active dataset).
    sqlite.prepare("INSERT INTO instrument_snapshots (instrument_id, dataset_id, symbol, name, asset_class, active) VALUES ('old','dataset-1','NEW_SYMBOL','New Company Ltd','equity',1)").run();
    const store = new D1ResearchStore(db);

    const page = await store.pageRunMatches("run-1", { sort: "rank", direction: "asc", limit: 10, offset: 0 });

    expect(page.matches[0]).toMatchObject({ symbol: "OLD_SYMBOL", name: "Old Company Ltd", assetClass: "equity" });
  });

  it("offsets past the end returns an empty page with the correct total", async () => {
    const { db, sqlite } = createSqliteD1();
    seedDataset(sqlite, "dataset-1");
    seedScreen(sqlite, "screen-1", "Momentum", "Volume > 0");
    sqlite.prepare("INSERT INTO screen_runs (dataset_id, run_id, screen_id, effective_date, status, result_count, source, language_version, completed_at) VALUES ('dataset-1','run-1','screen-1','2026-09-07','complete',1,'Volume > 0','v1','2026-09-07T00:00:00.000Z')").run();
    sqlite.prepare("INSERT INTO screen_matches (dataset_id, run_id, ordinal, instrument_id, score, symbol, name, asset_class, metrics_json, entered, exited) VALUES ('dataset-1','run-1',1,'one',1,'ONE','One','equity','[]',1,0)").run();
    const store = new D1ResearchStore(db);

    const page = await store.pageRunMatches("run-1", { sort: "rank", direction: "asc", limit: 25, offset: 500 });

    expect(page.matches).toEqual([]);
    expect(page.total).toBe(1);
  });
});

describe("listRuns and getRun return bounded summaries", () => {
  it("lists a full page of historical run summaries with a small statement budget and no embedded matches", async () => {
    const { db, sqlite, probe } = createSqliteD1();
    seedDataset(sqlite, "dataset-1");
    seedScreen(sqlite, "screen-1", "Momentum", "Volume > 0");
    const insertRun = sqlite.prepare("INSERT INTO screen_runs (dataset_id, run_id, screen_id, effective_date, status, result_count, source, language_version, completed_at) VALUES (?,?,?,?,?,?,?,?,?)");
    for (let index = 0; index < 100; index += 1) {
      insertRun.run("dataset-1", `run-${index}`, "screen-1", `2026-08-${String((index % 28) + 1).padStart(2, "0")}`, "complete", 5, "Volume > 0", "v1", `2026-08-${String((index % 28) + 1).padStart(2, "0")}T00:00:00.000Z`);
    }
    probe.reset();
    const store = new D1ResearchStore(db);

    const page = await store.listRuns("screen-1", { limit: 100, offset: 0 });

    expect(page.runs).toHaveLength(100);
    expect(page.total).toBe(100);
    expect(page.runs.every((run) => !("matches" in run))).toBe(true);
    expect(probe.statementCount).toBeLessThanOrEqual(2);
    expect(probe.materializedRows).toBe(100);
  });

  it("bounds listRuns to a default page size even when a caller passes no options, and reports the full total", async () => {
    const { db, sqlite } = createSqliteD1();
    seedDataset(sqlite, "dataset-1");
    seedScreen(sqlite, "screen-1", "Momentum", "Volume > 0");
    const insertRun = sqlite.prepare("INSERT INTO screen_runs (dataset_id, run_id, screen_id, effective_date, status, result_count, source, language_version, completed_at) VALUES (?,?,?,?,?,?,?,?,?)");
    for (let index = 0; index < 100; index += 1) {
      insertRun.run("dataset-1", `run-${index}`, "screen-1", `2026-08-${String((index % 28) + 1).padStart(2, "0")}`, "complete", 5, "Volume > 0", "v1", `2026-08-${String((index % 28) + 1).padStart(2, "0")}T00:00:00.000Z`);
    }
    const store = new D1ResearchStore(db);

    const defaultPage = await store.listRuns("screen-1");
    expect(defaultPage.runs.length).toBeLessThanOrEqual(50);
    expect(defaultPage.total).toBe(100);

    // `screen_runs` is append-only and grows one row per execution forever;
    // a caller-supplied limit above the maximum is clamped, not honored.
    const oversized = await store.listRuns("screen-1", { limit: 10_000, offset: 0 });
    expect(oversized.runs.length).toBeLessThanOrEqual(100);
    expect(oversized.total).toBe(100);
  });

  it("getRun fetches one summary without touching screen_matches", async () => {
    const { db, sqlite, probe } = createSqliteD1();
    seedDataset(sqlite, "dataset-1");
    seedScreen(sqlite, "screen-1", "Momentum", "Volume > 0");
    sqlite.prepare("INSERT INTO screen_runs (dataset_id, run_id, screen_id, effective_date, status, result_count, source, language_version, completed_at) VALUES ('dataset-1','run-1','screen-1','2026-09-07','complete',10000,'Volume > 0','v1','2026-09-07T00:00:00.000Z')").run();
    probe.reset();
    const store = new D1ResearchStore(db);

    const run = await store.getRun("screen-1", "run-1");

    expect(run).toMatchObject({ id: "run-1", matchCount: 10_000 });
    expect(probe.statementCount).toBe(1);
    expect(probe.materializedRows).toBe(0);
  });

  it("getRun defaults to the latest complete run when no runId is given", async () => {
    const { db, sqlite } = createSqliteD1();
    seedDataset(sqlite, "dataset-1");
    seedScreen(sqlite, "screen-1", "Momentum", "Volume > 0");
    sqlite.prepare("INSERT INTO screen_runs (dataset_id, run_id, screen_id, effective_date, status, result_count, source, language_version, completed_at) VALUES ('dataset-1','older','screen-1','2026-09-01','complete',1,'Volume > 0','v1','2026-09-01T00:00:00.000Z')").run();
    sqlite.prepare("INSERT INTO screen_runs (dataset_id, run_id, screen_id, effective_date, status, result_count, source, language_version, completed_at) VALUES ('dataset-1','newer','screen-1','2026-09-05','complete',1,'Volume > 0','v1','2026-09-05T00:00:00.000Z')").run();
    const store = new D1ResearchStore(db);

    expect((await store.getRun("screen-1"))?.id).toBe("newer");
    expect((await store.getRun("screen-1", "older"))?.id).toBe("older");
    expect(await store.getRun("screen-1", "missing")).toBeNull();
  });
});

describe("runScreen creation is bounded", () => {
  it("creates a run over 10,000 matching instruments with no per-match Worker reads/writes, within ten set-based SQL statements", async () => {
    const { db, sqlite, probe } = createSqliteD1();
    seedDataset(sqlite, "dataset-1");
    seedScreen(sqlite, "screen-1", "Everything", "Volume > 0");
    const insertInstrument = sqlite.prepare(
      "INSERT INTO instrument_snapshots (instrument_id, dataset_id, symbol, name, asset_class, active, metric_values_json, metric_rows_json) VALUES (?,?,?,?,?,1,?,?)",
    );
    sqlite.exec("BEGIN");
    for (let index = 0; index < 10_000; index += 1) {
      const instrumentId = `inst-${String(index).padStart(6, "0")}`;
      insertInstrument.run(
        instrumentId,
        "dataset-1",
        instrumentId.toUpperCase(),
        `Instrument ${index}`,
        index % 2 === 0 ? "equity" : "etf",
        JSON.stringify({ volume: index + 1, momentum_score: index }),
        JSON.stringify([{ metric: "volume", value: index + 1, state: "present" }, { metric: "momentum_score", value: index, state: "present" }]),
      );
    }
    sqlite.exec("COMMIT");

    const store = new D1ResearchStore(db);
    const screen = { id: "screen-1", name: "Everything", source: "Volume > 0", languageVersion: "v1", createdAt: "2026-01-01", updatedAt: "2026-01-01" } as const;
    probe.reset();
    const started = performance.now();
    const run = await store.runScreen("dataset-1", screen, "2026-09-07");
    const elapsedMs = performance.now() - started;

    expect(run.matchCount).toBe(10_000);
    expect(probe.statementCount).toBeLessThanOrEqual(10);
    // Warmed local baseline, not the two-second live personal-use target (recorded separately).
    expect(elapsedMs).toBeLessThan(5_000);

    const matchRowCount = sqlite.prepare("SELECT COUNT(*) AS n FROM screen_matches WHERE run_id = ?").get(run.id) as { n: number };
    expect(matchRowCount.n).toBe(10_000);
  });

  it("keeps the statement count constant whether 100 or 10,000 instruments match", async () => {
    const build = (count: number) => {
      const { db, sqlite, probe } = createSqliteD1();
      seedDataset(sqlite, "dataset-1");
      seedScreen(sqlite, "screen-1", "Everything", "Volume > 0");
      const insert = sqlite.prepare("INSERT INTO instrument_snapshots (instrument_id, dataset_id, symbol, name, asset_class, active, metric_values_json, metric_rows_json) VALUES (?,?,?,?,?,1,?,?)");
      sqlite.exec("BEGIN");
      for (let index = 0; index < count; index += 1) {
        const instrumentId = `inst-${index}`;
        insert.run(instrumentId, "dataset-1", instrumentId, `Instrument ${index}`, "equity", JSON.stringify({ volume: index + 1 }), JSON.stringify([{ metric: "volume", value: index + 1, state: "present" }]));
      }
      sqlite.exec("COMMIT");
      return { db, probe };
    };
    const small = build(100);
    const large = build(10_000);
    const screen = { id: "screen-1", name: "Everything", source: "Volume > 0", languageVersion: "v1", createdAt: "2026-01-01", updatedAt: "2026-01-01" } as const;

    small.probe.reset();
    await new D1ResearchStore(small.db).runScreen("dataset-1", screen, "2026-09-07");
    large.probe.reset();
    await new D1ResearchStore(large.db).runScreen("dataset-1", screen, "2026-09-07");

    expect(small.probe.statementCount).toBe(large.probe.statementCount);
  });

  it("marks entered/exited deterministically against only the previous successful run, and only current matches count toward the total", async () => {
    const { db, sqlite } = createSqliteD1();
    seedDataset(sqlite, "dataset-1");
    seedScreen(sqlite, "screen-1", "Volume", "Volume > 5");
    const upsert = (id: string, volume: number) => {
      sqlite.prepare("DELETE FROM instrument_snapshots WHERE instrument_id = ?").run(id);
      sqlite.prepare("INSERT INTO instrument_snapshots (instrument_id, dataset_id, symbol, name, asset_class, active, metric_values_json, metric_rows_json) VALUES (?,?,?,?,?,1,?,?)")
        .run(id, "dataset-1", id.toUpperCase(), id, "equity", JSON.stringify({ volume, momentum_score: volume }), JSON.stringify([{ metric: "volume", value: volume, state: "present" }, { metric: "momentum_score", value: volume, state: "present" }]));
    };
    const remove = (id: string) => sqlite.prepare("DELETE FROM instrument_snapshots WHERE instrument_id = ?").run(id);
    const store = new D1ResearchStore(db);
    const screen = { id: "screen-1", name: "Volume", source: "Volume > 5", languageVersion: "v1", createdAt: "2026-01-01", updatedAt: "2026-01-01" } as const;

    upsert("one", 10);
    const first = await store.runScreen("dataset-1", screen, "2026-01-01");
    const firstPage = await store.pageRunMatches(first.id, { sort: "rank", direction: "asc", limit: 10, offset: 0 });
    expect(firstPage.matches.map((match) => ({ instrumentId: match.instrumentId, entered: match.entered }))).toEqual([{ instrumentId: "one", entered: true }]);

    upsert("two", 20);
    const second = await store.runScreen("dataset-1", screen, "2026-01-02");
    const secondPage = await store.pageRunMatches(second.id, { sort: "rank", direction: "asc", limit: 10, offset: 0 });
    expect(secondPage.matches.map((match) => ({ instrumentId: match.instrumentId, entered: match.entered }))).toEqual([
      { instrumentId: "two", entered: true },
      { instrumentId: "one", entered: false },
    ]);
    expect(second.matchCount).toBe(2);

    remove("one");
    const third = await store.runScreen("dataset-1", screen, "2026-01-03");
    const thirdPage = await store.pageRunMatches(third.id, { sort: "rank", direction: "asc", limit: 10, offset: 0 });
    expect(thirdPage.matches.map((match) => match.instrumentId)).toEqual(["two"]);
    expect(third.matchCount).toBe(1);
  });

  /**
   * Warmed local baseline for the brief's "Record a warmed local
   * 12,000-instrument representative-query timing and SQL query plan"
   * requirement. This is a regression baseline, not the live personal-use
   * timing check the spec's two-second target still needs separately: the
   * assertions below are deliberately generous (an order of magnitude above
   * the measured ~40-65ms) so this stays a signal for a real regression
   * (e.g. an index dropped, or a per-row correlated subquery reintroduced)
   * rather than a source of local/CI flakiness.
   */
  it("records a warmed 12,000-instrument representative-query timing and query plan baseline", async () => {
    const { db, sqlite, probe } = createSqliteD1();
    seedDataset(sqlite, "dataset-1");
    seedScreen(sqlite, "screen-1", "Everything", "Volume > 0");
    const insertInstrument = sqlite.prepare(
      "INSERT INTO instrument_snapshots (instrument_id, dataset_id, symbol, name, asset_class, active, metric_values_json, metric_rows_json) VALUES (?,?,?,?,?,1,?,?)",
    );
    sqlite.exec("BEGIN");
    for (let index = 0; index < 12_000; index += 1) {
      const instrumentId = `inst-${String(index).padStart(6, "0")}`;
      insertInstrument.run(
        instrumentId,
        "dataset-1",
        instrumentId.toUpperCase(),
        `Instrument ${index}`,
        index % 2 === 0 ? "equity" : "etf",
        JSON.stringify({ volume: index + 1, momentum_score: index }),
        JSON.stringify([{ metric: "volume", value: index + 1, state: "present" }, { metric: "momentum_score", value: index, state: "present" }]),
      );
    }
    sqlite.exec("COMMIT");

    const store = new D1ResearchStore(db);
    const screen = { id: "screen-1", name: "Everything", source: "Volume > 0", languageVersion: "v1", createdAt: "2026-01-01", updatedAt: "2026-01-01" } as const;
    probe.reset();
    const started = performance.now();
    const run = await store.runScreen("dataset-1", screen, "2026-09-07");
    const runElapsedMs = performance.now() - started;

    expect(run.matchCount).toBe(12_000);
    // Warmed local baseline: measured ~40-65ms locally. Ten seconds is a
    // smoke-test ceiling to catch a gross regression, not a target.
    expect(runElapsedMs).toBeLessThan(10_000);
    console.info(`[R06 baseline] runScreen over 12,000 instruments: ${runElapsedMs.toFixed(2)}ms, ${probe.statementCount} statements`);

    // The representative query is the current-match `INSERT ... SELECT`
    // (identified by its ROW_NUMBER() window function). Its query plan must
    // not show a per-candidate-row scan of `screen_runs` -- the previous-run
    // lookup is resolved once, up front, and bound as a literal -- or of
    // `instrument_snapshots` outside its covering index.
    const representative = probe.executed.find((statement) => statement.sql.includes("ROW_NUMBER()") && statement.sql.includes("INSERT INTO screen_matches"));
    expect(representative).toBeDefined();
    const plan = explainQueryPlan(sqlite, representative!);
    const planText = plan.map((row) => row.detail).join("\n");
    console.info(`[R06 baseline] EXPLAIN QUERY PLAN:\n${planText}`);
    expect(planText).toContain("USING INDEX idx_instrument_snapshots_active");
    expect(planText).not.toContain("SCAN screen_runs");
    expect(planText).not.toMatch(/SCAN i\b/); // full scan of instrument_snapshots, as opposed to a SEARCH via the index

    const pageStarted = performance.now();
    const page = await store.pageRunMatches(run.id, { sort: "rank", direction: "asc", limit: 25, offset: 0 });
    const pageElapsedMs = performance.now() - pageStarted;
    expect(page.matches).toHaveLength(25);
    expect(pageElapsedMs).toBeLessThan(1_000);
    console.info(`[R06 baseline] pageRunMatches over 12,000 matches: ${pageElapsedMs.toFixed(2)}ms`);
  });
});

describe("historical explanations survive the 0008 upgrade", () => {
  it("falls back to a pre-migration row's explanation_json when metrics_json is empty", async () => {
    const { db, sqlite } = createSqliteD1();
    seedDataset(sqlite, "dataset-1");
    seedScreen(sqlite, "screen-1", "Volume", "Volume > 100");
    sqlite.prepare("INSERT INTO screen_runs (dataset_id, run_id, screen_id, effective_date, status, result_count, source, language_version, completed_at) VALUES ('dataset-1','run-1','screen-1','2026-09-07','complete',1,'Volume > 100','v1','2026-09-07T00:00:00.000Z')").run();
    // Simulates a row written before the 0008 migration: `metrics_json`
    // takes its column default ('[]') because the column didn't exist yet
    // when this row was written, but `explanation_json` (populated by the
    // pre-R06 write path) still holds the real explanation.
    const legacyExplanation = { matched: true, text: "Matched Volume > 100", metrics: ["volume"], clauses: [{ clause: "Volume > 100", metric: "volume", result: "Matched" }] };
    sqlite.prepare("INSERT INTO screen_matches (dataset_id, run_id, ordinal, instrument_id, score, symbol, name, asset_class, explanation_json, entered, exited) VALUES ('dataset-1','run-1',1,'old',200,'OLD','Old Co','equity',?,1,0)")
      .run(JSON.stringify(legacyExplanation));
    const store = new D1ResearchStore(db);

    const page = await store.pageRunMatches("run-1", { sort: "rank", direction: "asc", limit: 10, offset: 0 });

    expect(page.matches).toHaveLength(1);
    expect(page.matches[0]).toMatchObject({
      instrumentId: "old",
      explanation: { matched: true, text: "Matched Volume > 100", clauses: [{ clause: "Volume > 100", metric: "volume", result: "Matched" }] },
    });
  });

  it("still rebuilds the explanation from metrics_json when a post-migration row legitimately has no legacy explanation_json", async () => {
    const { db, sqlite } = createSqliteD1();
    seedDataset(sqlite, "dataset-1");
    seedScreen(sqlite, "screen-1", "Volume", "Volume > 5");
    sqlite.prepare("INSERT INTO screen_runs (dataset_id, run_id, screen_id, effective_date, status, result_count, source, language_version, completed_at) VALUES ('dataset-1','run-1','screen-1','2026-09-07','complete',1,'Volume > 5','v1','2026-09-07T00:00:00.000Z')").run();
    const metricsJson = JSON.stringify([{ metric: "volume", value: 10, state: "present" }]);
    sqlite.prepare("INSERT INTO screen_matches (dataset_id, run_id, ordinal, instrument_id, score, symbol, name, asset_class, metrics_json, entered, exited) VALUES ('dataset-1','run-1',1,'new',10,'NEW','New Co','equity',?,1,0)")
      .run(metricsJson);
    const store = new D1ResearchStore(db);

    const page = await store.pageRunMatches("run-1", { sort: "rank", direction: "asc", limit: 10, offset: 0 });

    expect(page.matches[0]?.explanation.clauses?.[0]).toMatchObject({ metric: "volume", result: "Matched" });
  });
});
