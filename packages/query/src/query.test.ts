import { describe, expect, it } from "vitest";
import {
  DEFAULT_METRIC_CATALOG,
  compileQuery,
  compileEavQuery,
  evaluateQuery,
  parseQuery,
  printAst,
  typecheckQuery,
} from "./index";
import { spawnSync } from "node:child_process";

const row = (values: Record<string, number | null>) => values;

describe("safe screener language", () => {
  it("parses arithmetic before comparison and longest-match aliases", () => {
    const parsed = parseQuery("Volume > Volume 1week average * 1.5");
    expect(parsed.diagnostics).toEqual([]);
    expect(printAst(parsed.value!)).toBe("(Volume > (Volume 1week average * 1.5))");
  });

  it("is case insensitive and respects boolean precedence", () => {
    const parsed = parseQuery("volume > 100 AND NOT (Market capitalization < 500 OR volume = 0)");
    expect(parsed.diagnostics).toEqual([]);
    expect(printAst(parsed.value!)).toBe(
      "((Volume > 100) AND (NOT ((Market Capitalization < 500) OR (Volume = 0))))",
    );
  });

  it("propagates unknown for division by zero", () => {
    const parsed = parseQuery("Volume / 0 > 1");
    expect(parsed.diagnostics).toEqual([]);
    expect(evaluateQuery(parsed.value!, row({ volume: 10 }))).toBe("unknown");
  });

  it("compiles only checked fields and binds literals", () => {
    const parsed = parseQuery("Return over 1day > 3% AND Volume > Volume 1week average * 1.5");
    const checked = typecheckQuery(parsed.value!, DEFAULT_METRIC_CATALOG, ["equity"]);
    expect(checked.valid).toBe(true);
    const compiled = compileQuery(checked.ast, DEFAULT_METRIC_CATALOG);
    expect(compiled.params).toEqual([0.03, 1.5]);
    expect(compiled.whereSql).toContain("IS TRUE");
    expect(compiled.whereSql).not.toMatch(/Return over|Volume 1week/);
    expect(compiled.referencedMetricIds).toEqual(["return_1d", "volume", "volume_1w_avg"]);
  });

  it("compiles checked metrics for the compact remote snapshot", () => {
    const checked = typecheckQuery(parseQuery("Return over 1day > 3%").value!, DEFAULT_METRIC_CATALOG, ["equity"]);
    expect(checked.valid).toBe(true);
    const compiled = compileQuery(checked.ast, DEFAULT_METRIC_CATALOG, { relation: "snapshot", includeDatasetFilter: false });
    expect(compiled.whereSql).toContain("json_extract(s.metric_values_json, '$.return_1d')");
    expect(compiled.params).toEqual([0.03]);
  });

  it("enforces percentage-point units while allowing dimensionless thresholds", () => {
    const invalid = typecheckQuery(parseQuery("Volume > 10%").value!, DEFAULT_METRIC_CATALOG, ["equity"]);
    expect(invalid.valid).toBe(false);
    expect(invalid.diagnostics.some((diagnostic) => diagnostic.code === "UNIT_MISMATCH")).toBe(true);

    const valid = typecheckQuery(parseQuery("Return over 1day > 3%").value!, DEFAULT_METRIC_CATALOG, ["equity"]);
    expect(valid.valid).toBe(true);
    expect(valid.diagnostics).toEqual([]);
    expect(compileQuery(valid.ast, DEFAULT_METRIC_CATALOG).params).toEqual([0.03]);
    expect(evaluateQuery(valid.ast, { return_1d: 0.03 })).toBe("false");
  });

  it("requires the percent suffix when comparing a percent metric to a bare number", () => {
    // Percent metrics are stored as fractions, so a bare `3` means 300%.
    // Accepting it silently is a 100x screening error with no diagnostic.
    const checked = typecheckQuery(parseQuery("Return over 1day > 3").value!, DEFAULT_METRIC_CATALOG, ["equity"]);
    expect(checked.valid).toBe(false);
    const mismatch = checked.diagnostics.find((diagnostic) => diagnostic.code === "UNIT_MISMATCH");
    expect(mismatch).toBeDefined();
    expect(mismatch?.suggestions).toContain("3%");

    // A negated literal is the same mistake and gets the same suggestion.
    const negated = typecheckQuery(parseQuery("Maximum Drawdown 1year < -20").value!, DEFAULT_METRIC_CATALOG, ["equity"]);
    expect(negated.valid).toBe(false);
    expect(negated.diagnostics.find((diagnostic) => diagnostic.code === "UNIT_MISMATCH")?.suggestions).toContain("-20%");

    // Dimensionless metrics keep accepting bare thresholds.
    expect(typecheckQuery(parseQuery("Volume > 500000").value!, DEFAULT_METRIC_CATALOG, ["equity"]).valid).toBe(true);
    expect(typecheckQuery(parseQuery("RS Rating > 90").value!, DEFAULT_METRIC_CATALOG, ["equity"]).valid).toBe(true);
    // And a percent-to-percent comparison remains valid.
    expect(typecheckQuery(parseQuery("Return over 1day > Return over 1week").value!, DEFAULT_METRIC_CATALOG, ["equity"]).valid).toBe(true);
  });

  it("rejects unknown fields with a suggestion", () => {
    const parsed = parseQuery("Volum > 500");
    expect(parsed.value).toBeNull();
    expect(parsed.diagnostics[0]?.code).toBe("UNKNOWN_METRIC");
    expect(parsed.diagnostics[0]?.suggestions).toContain("Volume");
  });

  it("does not allow SQL or arbitrary functions", () => {
    for (const source of [
      "Volume > 1; DROP TABLE latest_metrics",
      "Volume > (SELECT 1)",
      "exec('DROP TABLE latest_metrics')",
      "Volume /* comment */ > 1",
    ]) {
      const parsed = parseQuery(source);
      expect(parsed.value).toBeNull();
      expect(parsed.diagnostics.length).toBeGreaterThan(0);
    }
  });

  it("refuses a catalog column that is not a SQL identifier", () => {
    const catalog = [{ ...DEFAULT_METRIC_CATALOG[0]!, column: "return_1d) OR 1=1 --" }];
    const parsed = parseQuery("Return over 1day > 3", catalog);
    expect(() => compileQuery(parsed.value!, catalog)).toThrowError(/checked catalog/);
  });

  it("keeps SQL arithmetic unknown when a value is unavailable", () => {
    const parsed = parseQuery("Volume / 2 > 1");
    const checked = typecheckQuery(parsed.value!, DEFAULT_METRIC_CATALOG, ["equity"]);
    const compiled = compileQuery(checked.ast, DEFAULT_METRIC_CATALOG);
    expect(compiled.whereSql).toContain("NULLIF");
    expect(evaluateQuery(parsed.value!, row({ volume: null }))).toBe("unknown");
  });

  it("executes EAV SQL against SQLite with the same tri-state results", () => {
    const parsed = parseQuery("Volume > 500 AND (Return over 1day > 0% OR Volume / 0 > 1)");
    const checked = typecheckQuery(parsed.value!, DEFAULT_METRIC_CATALOG, ["equity"]);
    const compiled = compileEavQuery(checked.ast, DEFAULT_METRIC_CATALOG, "dataset-1");
    expect(compiled.params[0]).toBe("dataset-1");
    expect(compiled.whereSql).toContain("i.dataset_id = ?");
    const rows: readonly { dataset: string; id: string; volume: number | null; return_1d: number | null }[] = [
      { dataset: "dataset-1", id: "i0", volume: 100, return_1d: 0.03 },
      { dataset: "dataset-2", id: "i0", volume: 900, return_1d: 0.03 },
      { dataset: "dataset-1", id: "i1", volume: 600, return_1d: 0.03 },
      { dataset: "dataset-1", id: "i2", volume: 600, return_1d: -0.01 },
      { dataset: "dataset-1", id: "i3", volume: null, return_1d: 0.03 },
      { dataset: "dataset-1", id: "i4", volume: 600, return_1d: null },
    ];
    const expected = rows.filter((item) => item.dataset === "dataset-1" && evaluateQuery(parsed.value!, {
      volume: item.volume,
      return_1d: item.return_1d,
    }) === "true").map((item) => item.id);
    const params = compiled.params.map((value, index) => `.parameter set ?${index + 1} ${typeof value === "string" ? `'${value.replaceAll("'", "''")}'` : value === null ? "NULL" : value}`).join("\n");
    const inserts = rows.flatMap((item) => [
      `INSERT INTO instruments VALUES ('${item.dataset}','${item.id}');`,
      item.volume === null ? `INSERT INTO latest_metrics VALUES ('${item.dataset}','${item.id}','volume','missing',NULL);` : `INSERT INTO latest_metrics VALUES ('${item.dataset}','${item.id}','volume','present',${item.volume});`,
      item.return_1d === null ? `INSERT INTO latest_metrics VALUES ('${item.dataset}','${item.id}','return_1d','missing',NULL);` : `INSERT INTO latest_metrics VALUES ('${item.dataset}','${item.id}','return_1d','present',${item.return_1d});`,
    ]).join("\n");
    const script = [
      ".parameter init",
      params,
      "CREATE TABLE instruments (dataset_id TEXT, instrument_id TEXT, PRIMARY KEY (dataset_id, instrument_id));",
      "CREATE TABLE latest_metrics (dataset_id TEXT, instrument_id TEXT, metric TEXT, state TEXT, value REAL);",
      inserts,
      ".mode list",
      `SELECT i.instrument_id FROM instruments AS i WHERE ${compiled.whereSql} ORDER BY i.instrument_id;`,
      ".quit",
    ].join("\n");
    const result = spawnSync("sqlite3", [":memory:"], { input: script, encoding: "utf8" });
    expect(result.status).toBe(0);
    expect(result.stderr).toBe("");
    expect(result.stdout.trim() ? result.stdout.trim().split("\n") : []).toEqual(expected);

    const division = parseQuery("Volume / 0 > 1");
    const divisionCompiled = compileEavQuery(division.value!, DEFAULT_METRIC_CATALOG, "dataset-1");
    const divisionParams = divisionCompiled.params.map((value, index) => `.parameter set ?${index + 1} ${typeof value === "string" ? `'${value}'` : value}`).join("\n");
    const divisionResult = spawnSync("sqlite3", [":memory:"], {
      input: [
        ".parameter init",
        divisionParams,
        "CREATE TABLE instruments (dataset_id TEXT, instrument_id TEXT);",
        "CREATE TABLE latest_metrics (dataset_id TEXT, instrument_id TEXT, metric TEXT, state TEXT, value REAL);",
        "INSERT INTO instruments VALUES ('dataset-1','i1');",
        "INSERT INTO latest_metrics VALUES ('dataset-1','i1','volume','present',900);",
        `.mode list\nSELECT i.instrument_id FROM instruments AS i WHERE ${divisionCompiled.whereSql};`,
        ".quit",
      ].join("\n"),
      encoding: "utf8",
    });
    expect(divisionResult.status).toBe(0);
    expect(divisionResult.stdout.trim()).toBe("");
  });

  it("rejects bounded malformed and identifier payloads", () => {
    const payloads = ["'", '"', "`", "\\", "\u0000", "DROP", "SELECT", "/*", "*/", ";", "$", "@"];
    for (let index = 0; index < 32; index += 1) {
      const payload = `${payloads[index % payloads.length]}${index}`;
      const parsed = parseQuery(`Volume${payload} > 1`);
      expect(parsed.value).toBeNull();
      expect(parsed.diagnostics.length).toBeGreaterThan(0);
    }
    for (const column of ["x) OR 1=1 --", "x;DROP", "x\" || 1=1"]) {
      const catalog = [{ ...DEFAULT_METRIC_CATALOG[0]!, column }];
      const parsed = parseQuery("Return over 1day > 1", catalog);
      expect(() => compileQuery(parsed.value!, catalog)).toThrow();
    }
  });
});
