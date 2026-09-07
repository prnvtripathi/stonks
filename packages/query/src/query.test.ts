import { describe, expect, it } from "vitest";
import {
  DEFAULT_METRIC_CATALOG,
  compileQuery,
  evaluateQuery,
  parseQuery,
  printAst,
  typecheckQuery,
} from "./index";

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
    const parsed = parseQuery("Return over 1day > 3 AND Volume > Volume 1week average * 1.5");
    const checked = typecheckQuery(parsed.value!, DEFAULT_METRIC_CATALOG, ["equity"]);
    expect(checked.valid).toBe(true);
    const compiled = compileQuery(checked.ast, DEFAULT_METRIC_CATALOG);
    expect(compiled.params).toEqual([3, 1.5]);
    expect(compiled.whereSql).toContain("IS TRUE");
    expect(compiled.whereSql).not.toMatch(/Return over|Volume 1week/);
    expect(compiled.referencedMetricIds).toEqual(["return_1d", "volume", "volume_1w_avg"]);
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
});
