import { execFileSync } from "node:child_process";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { D1ResearchStore, type D1Database, type D1Statement } from "./repositories";

type CompactRow = Record<string, unknown>;

function compactMomentumRow(): CompactRow {
  const root = resolve(import.meta.dirname, "../../..");
  return JSON.parse(execFileSync(resolve(root, ".venv/bin/python3"), ["pipeline/tests/integration/compact_momentum_fixture.py"], {
    cwd: root,
    env: { ...process.env, PYTHONPATH: "pipeline" },
    encoding: "utf8",
  })) as CompactRow;
}

function storeFor(row: CompactRow): D1ResearchStore {
  const db: D1Database = {
    prepare: () => ({
      bind: () => statement,
      first: async <T extends Record<string, unknown>>(): Promise<T | null> => row as T,
      all: async <T extends Record<string, unknown>>(): Promise<{ results: readonly T[] }> => ({ results: [] }),
      run: async () => ({ success: true }),
    }),
  };
  const statement: D1Statement = db.prepare("");
  return new D1ResearchStore(db);
}

describe("compact publication contract", () => {
  it("preserves Python-exported momentum metadata through the D1 repository", async () => {
    const instrument = await storeFor(compactMomentumRow()).instrument("momentum-contract", "mf-1");

    expect(instrument?.momentum?.components[0]).toMatchObject({ raw: 12, normalized: 0.6, weight: 0.2, contribution: 0.12, unit: "percent" });
    expect(instrument?.momentum).toMatchObject({ cohort: "mutual_fund:equity", coverage: 0.85, sourceDate: "2026-09-07", formulaVersion: "momentum-v2-cohort", warning: "Thin category coverage" });
  });

  it("decodes JSON-string metric metadata through the D1 repository", async () => {
    const row = compactMomentumRow();
    const metrics = JSON.parse(String(row.metric_rows_json)) as Array<Record<string, unknown>>;
    for (const metric of metrics) metric.metadata = JSON.stringify(metric.metadata);
    row.metric_rows_json = JSON.stringify(metrics);

    const instrument = await storeFor(row).instrument("momentum-contract", "mf-1");

    expect(instrument?.momentum?.components[0]).toMatchObject({ raw: 12, normalized: 0.6, weight: 0.2, contribution: 0.12, unit: "percent" });
    expect(instrument?.momentum).toMatchObject({ cohort: "mutual_fund:equity", coverage: 0.85, sourceDate: "2026-09-07", formulaVersion: "momentum-v2-cohort", warning: "Thin category coverage" });
  });

  it("rejects malformed null and array metric metadata", async () => {
    const row = compactMomentumRow();
    const metrics = JSON.parse(String(row.metric_rows_json)) as Array<Record<string, unknown>>;
    metrics[0]!.metadata = null;
    metrics[1]!.metadata = ["not", "a", "record"];
    row.metric_rows_json = JSON.stringify(metrics);

    const instrument = await storeFor(row).instrument("momentum-contract", "mf-1");

    expect(instrument?.momentum?.components[0]).toMatchObject({ unit: "ratio", weight: 0, contribution: 0 });
    expect(instrument?.momentum).toMatchObject({ cohort: "mutual_fund:unknown", coverage: 1, sourceDate: "2026-09-07" });
  });
});
